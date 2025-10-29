from __future__ import annotations
import os
import json
import logging
from typing import Dict, Any, Optional, List
import numpy as np
import ray
from ray import serve
from ray.serve import HTTPOptions
from transformers import PreTrainedTokenizerFast

_log_level_name = os.getenv("LOG_LEVEL", "INFO") or "INFO"
_log_level = getattr(logging, _log_level_name.upper(), logging.INFO)
logging.basicConfig(level=_log_level)
log = logging.getLogger("rayserve_onnx")

RAY_ADDRESS = os.getenv("RAY_ADDRESS") or None
RAY_NAMESPACE = os.getenv("RAY_NAMESPACE") or None

SERVE_HTTP_HOST = os.getenv("SERVE_HTTP_HOST", "127.0.0.1")
SERVE_HTTP_PORT = int(os.getenv("SERVE_HTTP_PORT", "8003"))

EMBED_DEPLOYMENT = os.getenv("EMBED_DEPLOYMENT", "embed_onxx")
RERANK_HANDLE_NAME = os.getenv("RERANK_HANDLE_NAME", "rerank_onxx")

ONNX_USE_CUDA = (os.getenv("ONNX_USE_CUDA", "false").lower() in ("1", "true", "yes"))
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

INDEXING_EMBEDDER_MAX_TOKENS = int(os.getenv("INDEXING_EMBEDDER_MAX_TOKENS", "512"))
INFERENCE_EMBEDDER_MAX_TOKENS = int(os.getenv("INFERENCE_EMBEDDER_MAX_TOKENS", "64"))
CROSS_ENCODER_MAX_TOKENS = int(os.getenv("CROSS_ENCODER_MAX_TOKENS", "600"))
ENABLE_CROSS_ENCODER = (os.getenv("ENABLE_CROSS_ENCODER", "true").lower() in ("1", "true", "yes"))

try:
    import onnxruntime as ort
except Exception as e:
    raise ImportError("onnxruntime not importable: " + str(e))

if ONNX_USE_CUDA:
    try:
        providers_avail = ort.get_available_providers()
    except Exception:
        providers_avail = []
    if "CUDAExecutionProvider" not in providers_avail:
        raise RuntimeError(
            "ONNX_USE_CUDA=true but CUDAExecutionProvider not available: "
            f"{providers_avail}. Make sure onnxruntime with CUDA support is installed and CUDA drivers are present."
        )

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

def _effective_max_length(tokenizer: PreTrainedTokenizerFast, requested: Optional[int], env_default: int, hard_cap: Optional[int] = None) -> int:
    if requested is None:
        requested = env_default
    try:
        model_max = int(getattr(tokenizer, "model_max_length", 0) or 0)
    except Exception:
        model_max = 0
    caps = [int(requested), int(env_default)]
    if hard_cap:
        caps.append(int(hard_cap))
    if model_max and model_max > 0:
        caps.append(model_max)
    candidates = [c for c in caps if c and c > 0]
    eff = int(min(candidates)) if candidates else int(env_default)
    return max(1, eff)

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

        probe_len = _effective_max_length(self.tokenizer, INDEXING_EMBEDDER_MAX_TOKENS, INDEXING_EMBEDDER_MAX_TOKENS)
        toks = self.tokenizer(["health-check"], padding=True, truncation=True, return_tensors="np", max_length=min(8, probe_len))

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
        log.info("[%s] startup OK: detected embed_dim=%d model_max_length=%s env_index_max=%d",
                 EMBED_DEPLOYMENT, self._embed_dim, getattr(self.tokenizer, "model_max_length", None), INDEXING_EMBEDDER_MAX_TOKENS)

    async def __call__(self, request_or_payload):
        try:
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
            texts = body.get("texts", []) if isinstance(body, dict) else []
            if not isinstance(texts, list):
                texts = [texts]

            requested_max = None
            if isinstance(body, dict):
                try:
                    requested_max = int(body.get("max_length", None)) if body.get("max_length", None) is not None else None
                except Exception:
                    requested_max = None

            eff_max = _effective_max_length(self.tokenizer, requested_max, INDEXING_EMBEDDER_MAX_TOKENS)
            toks = self.tokenizer(texts, padding=True, truncation=True, return_tensors="np", max_length=eff_max)
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
            return {"vectors": [v.tolist() for v in vecs], "max_length_used": int(eff_max)}
        except Exception as e:
            log.exception("embed call error: %s", e)
            raise

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

        probe_len = _effective_max_length(self.tokenizer, CROSS_ENCODER_MAX_TOKENS, CROSS_ENCODER_MAX_TOKENS)
        toks = self.tokenizer([("q", "a"), ("q", "b")], padding=True, truncation='only_second', return_tensors="np", max_length=min(8, probe_len))
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

        log.info("[%s] startup OK: sample scores shape %s model_max_length=%s env_cross_max=%d",
                 RERANK_HANDLE_NAME, derived.shape, getattr(self.tokenizer, "model_max_length", None), CROSS_ENCODER_MAX_TOKENS)

    async def __call__(self, request_or_payload):
        try:
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

            requested_max = None
            if isinstance(body, dict):
                try:
                    requested_max = int(body.get("max_length", None)) if body.get("max_length", None) is not None else None
                except Exception:
                    requested_max = None

            eff_max = _effective_max_length(self.tokenizer, requested_max, CROSS_ENCODER_MAX_TOKENS)
            toks = self.tokenizer([(q, t) for t in cands], padding=True, truncation='only_second', return_tensors="np", max_length=eff_max)

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
            return {"scores": [float(s) for s in np.asarray(scores).astype(float)], "max_length_used": int(eff_max)}
        except Exception as e:
            log.exception("rerank call error: %s", e)
            raise

def main():
    ray.init(address=RAY_ADDRESS, namespace=RAY_NAMESPACE, ignore_reinit_error=True)

    http_opts = HTTPOptions(host=SERVE_HTTP_HOST, port=SERVE_HTTP_PORT)
    serve.start(detached=True, http_options=http_opts)

    binds = [ONNXEmbed.bind()]
    if ENABLE_CROSS_ENCODER:
        binds.append(ONNXRerank.bind())
        log.info("ENABLE_CROSS_ENCODER=true -> deploying embed + rerank")
    else:
        log.info("ENABLE_CROSS_ENCODER=false -> deploying only embed (reranker skipped)")

    serve.run(*binds)
    names = [EMBED_DEPLOYMENT] + ([RERANK_HANDLE_NAME] if ENABLE_CROSS_ENCODER else [])
    log.info("ONNX Serve deployments started: %s", " ".join(names))
    log.info("Serve HTTP listening on %s:%d", SERVE_HTTP_HOST, SERVE_HTTP_PORT)

if __name__ == "__main__":
    main()
