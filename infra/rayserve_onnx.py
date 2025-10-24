#!/usr/bin/env python3
# infra/rayserve_onnx.py
from __future__ import annotations

import os
import sys
import json
from typing import Dict, Any, Optional, List
import numpy as np
import ray
from ray import serve
from transformers import PreTrainedTokenizerFast

# ENV defaults (override by export)
RAY_ADDRESS = os.getenv("RAY_ADDRESS", None)  # e.g. "auto" or "127.0.0.1:6379"
RAY_NAMESPACE = os.getenv("RAY_NAMESPACE", None)  # None -> default Ray namespace
EMBED_DEPLOYMENT = os.getenv("EMBED_DEPLOYMENT", "embed_onxx")
RERANK_HANDLE_NAME = os.getenv("RERANK_HANDLE_NAME", "rerank_onxx")

ONNX_USE_CUDA = os.getenv("ONNX_USE_CUDA", "false").lower() in ("1", "true", "yes")
MODEL_DIR_EMBED = os.getenv("MODEL_DIR_EMBED", "/models/gte-modernbert-base")
MODEL_DIR_RERANK = os.getenv("MODEL_DIR_RERANK", "/models/gte-reranker-modernbert-base")
ONNX_EMBED_PATH = os.getenv("ONNX_EMBED_PATH", os.path.join(MODEL_DIR_EMBED, "onnx", "model_int8.onnx"))
ONNX_EMBED_TOKENIZER_PATH = os.getenv("ONNX_EMBED_TOKENIZER_PATH", os.path.join(MODEL_DIR_EMBED, "tokenizer.json"))
ONNX_RERANK_PATH = os.getenv("ONNX_RERANK_PATH", os.path.join(MODEL_DIR_RERANK, "onnx", "model_int8.onnx"))
ONNX_RERANK_TOKENIZER_PATH = os.getenv("ONNX_RERANK_TOKENIZER_PATH", os.path.join(MODEL_DIR_RERANK, "tokenizer.json"))

EMBED_REPLICAS = int(os.getenv("EMBED_REPLICAS", "1"))
RERANK_REPLICAS = int(os.getenv("RERANK_REPLICAS", "1"))
EMBED_GPU = int(os.getenv("EMBED_GPU_PER_REPLICA", "0")) if ONNX_USE_CUDA else 0
RERANK_GPU = int(os.getenv("RERANK_GPU_PER_REPLICA", "0")) if ONNX_USE_CUDA else 0

MAX_RERANK = int(os.getenv("MAX_RERANK", "256"))
ORT_INTRA_THREADS = int(os.getenv("ORT_INTRA_THREADS", "1"))
ORT_INTER_THREADS = int(os.getenv("ORT_INTER_THREADS", "1"))

# ONNX runtime import & helpers
try:
    import onnxruntime as ort
except Exception as e:
    raise ImportError("onnxruntime not importable: " + str(e))

if ONNX_USE_CUDA:
    providers_avail = ort.get_available_providers()
    if "CUDAExecutionProvider" not in providers_avail:
        raise RuntimeError("ONNX_USE_CUDA=true but CUDAExecutionProvider not available: " + str(providers_avail))


def make_session(path: str):
    so = ort.SessionOptions()
    so.intra_op_num_threads = ORT_INTRA_THREADS
    so.inter_op_num_threads = ORT_INTER_THREADS
    providers = ["CUDAExecutionProvider", "CPUExecutionProvider"] if ONNX_USE_CUDA else ["CPUExecutionProvider"]
    sess = ort.InferenceSession(path, sess_options=so, providers=providers)
    return sess


def mean_pool(last_hidden: np.ndarray, attention_mask: np.ndarray) -> np.ndarray:
    mask = attention_mask.astype(np.float32)
    mask = mask[:, :, None]
    summed = (last_hidden * mask).sum(axis=1)
    denom = np.maximum(mask.sum(axis=1), 1e-9)
    return summed / denom


def _ensure_file(path: str):
    if not os.path.exists(path):
        raise FileNotFoundError(path)


# Serve deployments
@serve.deployment(name=EMBED_DEPLOYMENT, num_replicas=EMBED_REPLICAS, ray_actor_options={"num_gpus": EMBED_GPU})
class ONNXEmbed:
    def __init__(self, onnx_path: str = ONNX_EMBED_PATH, tokenizer_path: str = ONNX_EMBED_TOKENIZER_PATH):
        _ensure_file(tokenizer_path)
        self.tokenizer = PreTrainedTokenizerFast(tokenizer_file=tokenizer_path)
        if getattr(self.tokenizer, "pad_token", None) is None:
            if getattr(self.tokenizer, "eos_token", None) is not None:
                self.tokenizer.pad_token = self.tokenizer.eos_token
            elif getattr(self.tokenizer, "sep_token", None) is not None:
                self.tokenizer.pad_token = self.tokenizer.sep_token
            else:
                self.tokenizer.add_special_tokens({"pad_token": "[PAD]"})
        _ensure_file(onnx_path)
        self.sess = make_session(onnx_path)
        self.input_names = [inp.name for inp in self.sess.get_inputs()]

        # startup check to infer embedding dimension
        toks = self.tokenizer(["health-check"], padding=True, truncation=True, return_tensors="np", max_length=8)
        ort_inputs = {}
        for k, v in toks.items():
            if k in self.input_names:
                ort_inputs[k] = v.astype("int64")
            else:
                if k == "input_ids":
                    for cand in ("input_ids", "input", "input.1"):
                        if cand in self.input_names:
                            ort_inputs[cand] = v.astype("int64")
                            break
        outputs = self.sess.run(None, ort_inputs)
        out = None
        for arr in outputs:
            arr = np.asarray(arr)
            if arr.ndim == 3:
                attn = toks.get("attention_mask", np.ones(arr.shape[:2], dtype="int64"))
                out = mean_pool(arr, attn)
                break
            if arr.ndim == 2:
                out = arr
                break
        if out is None:
            last = np.asarray(outputs[-1])
            if last.ndim > 2:
                out = last.reshape((last.shape[0], -1))
            else:
                out = last
        if out is None or out.ndim != 2:
            raise RuntimeError("Unable to infer embedding output shape from ONNX outputs.")
        self._embed_dim = int(out.shape[1])
        print(f"[{EMBED_DEPLOYMENT}] startup OK: detected embed_dim={self._embed_dim}")

    async def __call__(self, request_or_payload):
        """
        Accept either:
         - Starlette Request (HTTP ingress) -> await request.json()
         - plain dict/list when invoked via handle.remote(...) -> use directly
         - raw bytes/str -> parse json
        """
        body = None
        # direct python payload (common when using handle.remote)
        if isinstance(request_or_payload, (dict, list)):
            body = request_or_payload
        else:
            # Starlette Request-like (has .json() or .body())
            try:
                # Some call-sites may provide Request; try await request.json()
                body = await request_or_payload.json()
            except Exception:
                try:
                    raw = await request_or_payload.body()
                    body = json.loads(raw.decode("utf-8")) if raw else {}
                except Exception:
                    # fallback empty
                    body = {}

        texts = body.get("texts", []) if isinstance(body, dict) else []
        if not isinstance(texts, list):
            texts = [texts]
        toks = self.tokenizer(texts, padding=True, truncation=True, return_tensors="np", max_length=512)
        batch = toks["input_ids"].shape[0]
        ort_inputs: Dict[str, Any] = {}
        for k, v in toks.items():
            if k in self.input_names:
                ort_inputs[k] = v.astype("int64")
            else:
                if k == "input_ids":
                    for cand in ("input_ids", "input", "input.1"):
                        if cand in self.input_names:
                            ort_inputs[cand] = v.astype("int64")
                            break
        outputs = self.sess.run(None, ort_inputs)
        vecs: Optional[np.ndarray] = None
        for arr in outputs:
            arr = np.asarray(arr)
            if arr.ndim == 3 and arr.shape[0] == batch:
                attn = toks.get("attention_mask", np.ones(arr.shape[:2], dtype="int64"))
                vecs = mean_pool(arr, attn)
                break
            if arr.ndim == 2 and arr.shape[0] == batch:
                vecs = arr
                break
        if vecs is None:
            last = np.asarray(outputs[-1])
            if last.ndim > 2 and last.shape[0] == batch:
                vecs = last.reshape((last.shape[0], -1))
            else:
                raise RuntimeError("ONNX embed outputs invalid shapes")
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        norms = np.maximum(norms, 1e-12)
        vecs = (vecs / norms).astype(float)
        return {"vectors": [v.tolist() for v in vecs]}


@serve.deployment(name=RERANK_HANDLE_NAME, num_replicas=RERANK_REPLICAS, ray_actor_options={"num_gpus": RERANK_GPU})
class ONNXRerank:
    def __init__(self, onnx_path: str = ONNX_RERANK_PATH, tokenizer_path: str = ONNX_RERANK_TOKENIZER_PATH):
        _ensure_file(tokenizer_path)
        self.tokenizer = PreTrainedTokenizerFast(tokenizer_file=tokenizer_path)
        if getattr(self.tokenizer, "pad_token", None) is None:
            if getattr(self.tokenizer, "eos_token", None) is not None:
                self.tokenizer.pad_token = self.tokenizer.eos_token
            elif getattr(self.tokenizer, "sep_token", None) is not None:
                self.tokenizer.pad_token = self.tokenizer.sep_token
            else:
                self.tokenizer.add_special_tokens({"pad_token": "[PAD]"})
        _ensure_file(onnx_path)
        self.sess = make_session(onnx_path)
        self.input_names = [inp.name for inp in self.sess.get_inputs()]

        toks = self.tokenizer([("q", "a"), ("q", "b")], padding=True, truncation=True, return_tensors="np", max_length=8)
        ort_inputs = {}
        for k, v in toks.items():
            if k in self.input_names:
                ort_inputs[k] = v.astype("int64")
            else:
                if k == "input_ids":
                    for cand in ("input_ids", "input", "input.1"):
                        if cand in self.input_names:
                            ort_inputs[cand] = v.astype("int64")
                            break
        outs = self.sess.run(None, ort_inputs)
        derived = None
        for arr in outs:
            arr = np.asarray(arr)
            if arr.ndim == 1 and arr.shape[0] == 2:
                derived = arr
                break
            if arr.ndim == 2 and arr.shape[0] == 2:
                derived = arr[:, 0]
                break
        if derived is None:
            derived = np.asarray(outs[-1]).reshape(2, -1)[:, 0]
        print(f"[{RERANK_HANDLE_NAME}] startup OK: sample scores shape {derived.shape}")

    async def __call__(self, request_or_payload):
        # normalize input (same logic as embed)
        body = None
        if isinstance(request_or_payload, (dict, list)):
            body = request_or_payload
        else:
            try:
                body = await request_or_payload.json()
            except Exception:
                try:
                    raw = await request_or_payload.body()
                    body = json.loads(raw.decode("utf-8")) if raw else {}
                except Exception:
                    body = {}

        q = body.get("query", "") if isinstance(body, dict) else ""
        cands = body.get("cands", []) if isinstance(body, dict) else []
        if not isinstance(cands, list):
            cands = [cands]
        cands = cands[:MAX_RERANK]
        if len(cands) == 0:
            return {"scores": []}
        toks = self.tokenizer([(q, t) for t in cands], padding=True, truncation=True, return_tensors="np", max_length=512)
        ort_inputs: Dict[str, Any] = {}
        for k, v in toks.items():
            if k in self.input_names:
                ort_inputs[k] = v.astype("int64")
            else:
                if k == "input_ids":
                    for cand in ("input_ids", "input", "input.1"):
                        if cand in self.input_names:
                            ort_inputs[cand] = v.astype("int64")
                            break
        outputs = self.sess.run(None, ort_inputs)
        scores = None
        for arr in outputs:
            arr = np.asarray(arr)
            if arr.ndim == 1 and arr.shape[0] == len(cands):
                scores = arr
                break
            if arr.ndim == 2 and arr.shape[0] == len(cands):
                scores = arr[:, 0]
                break
        if scores is None:
            last = np.asarray(outputs[-1])
            try:
                scores = last.reshape(len(cands), -1)[:, 0]
            except Exception as e:
                raise RuntimeError("unable to parse reranker outputs: " + str(e))
        return {"scores": [float(s) for s in np.asarray(scores).astype(float)]}


def main():
    ray.init(address=RAY_ADDRESS, namespace=RAY_NAMESPACE, ignore_reinit_error=True)
    serve.start(detached=True)
    serve.run(ONNXEmbed.bind(), ONNXRerank.bind())
    print("ONNX Serve deployments started:", EMBED_DEPLOYMENT, RERANK_HANDLE_NAME)


if __name__ == "__main__":
    main()
