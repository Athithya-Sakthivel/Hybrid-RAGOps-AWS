"""
rayserve_models.py

Deploys:
 - ONNX embedder (embed_onxx)
 - ONNX reranker (rerank_onxx)   [optional, controlled by ENABLE_CROSS_ENCODER]
 - TGI LLM wrapper (tgi_llm)

Key features:
 - Async-safe: ONNX inference runs in threadpool via asyncio.to_thread
 - TGI wrapper uses httpx.AsyncClient and supervises a local TGI subprocess with restart/backoff
 - Per-replica GPU reservation via num_gpus and GPU_NODE token
 - Health checks supported via {"_health": true} payload or HTTP route
"""

from __future__ import annotations
import os
import time
import json
import logging
import socket
import subprocess
import asyncio
import threading
from typing import Dict, Any, Optional, List

import numpy as np
import ray
from ray import serve
from ray.serve import HTTPOptions
from transformers import PreTrainedTokenizerFast

# Try imports that might be absent in dev
try:
    import onnxruntime as ort  # type: ignore
except Exception:
    ort = None

try:
    import httpx  # type: ignore
except Exception:
    httpx = None

# --- logging
_log_level_name = os.getenv("LOG_LEVEL", "INFO") or "INFO"
_log_level = getattr(logging, _log_level_name.upper(), logging.INFO)
logging.basicConfig(level=_log_level)
log = logging.getLogger("rayserve_models")

# --- environment keys (unique, meaningful names)
# Ray / Serve
RSV_RAY_ADDRESS = os.getenv("RSV_RAY_ADDRESS") or None
RSV_RAY_NAMESPACE = os.getenv("RSV_RAY_NAMESPACE") or None
RSV_SERVE_HOST = os.getenv("RSV_SERVE_HOST", "127.0.0.1")
RSV_SERVE_PORT = int(os.getenv("RSV_SERVE_PORT", "8003"))

# ONNX embedder/reranker envs
RSV_EMBED_DEPLOYMENT = os.getenv("RSV_EMBED_DEPLOYMENT", "embed_onxx")
RSV_RERANK_DEPLOYMENT = os.getenv("RSV_RERANK_DEPLOYMENT", "rerank_onxx")
RSV_ONNX_USE_CUDA = (os.getenv("RSV_ONNX_USE_CUDA", "false").lower() in ("1", "true", "yes"))
RSV_MODEL_DIR_EMBED = os.getenv("RSV_MODEL_DIR_EMBED", "/models/gte-modernbert-base")
RSV_MODEL_DIR_RERANK = os.getenv("RSV_MODEL_DIR_RERANK", "/models/gte-reranker-modernbert-base")
RSV_ONNX_EMBED_PATH = os.getenv("RSV_ONNX_EMBED_PATH", os.path.join(RSV_MODEL_DIR_EMBED, "onnx", "model_int8.onnx"))
RSV_ONNX_EMBED_TOKENIZER_PATH = os.getenv("RSV_ONNX_EMBED_TOKENIZER_PATH", os.path.join(RSV_MODEL_DIR_EMBED, "tokenizer.json"))
RSV_ONNX_RERANK_PATH = os.getenv("RSV_ONNX_RERANK_PATH", os.path.join(RSV_MODEL_DIR_RERANK, "onnx", "model_int8.onnx"))
RSV_ONNX_RERANK_TOKENIZER_PATH = os.getenv("RSV_ONNX_RERANK_TOKENIZER_PATH", os.path.join(RSV_MODEL_DIR_RERANK, "tokenizer.json"))

RSV_EMBED_REPLICAS = int(os.getenv("RSV_EMBED_REPLICAS", "1"))
RSV_RERANK_REPLICAS = int(os.getenv("RSV_RERANK_REPLICAS", "1"))
RSV_EMBED_GPU = int(os.getenv("RSV_EMBED_GPU", "0")) if RSV_ONNX_USE_CUDA else 0
RSV_RERANK_GPU = int(os.getenv("RSV_RERANK_GPU", "0")) if RSV_ONNX_USE_CUDA else 0

RSV_ORT_INTRA_THREADS = int(os.getenv("RSV_ORT_INTRA_THREADS", "1"))
RSV_ORT_INTER_THREADS = int(os.getenv("RSV_ORT_INTER_THREADS", "1"))

RSV_INDEXING_EMBEDDER_MAX_TOKENS = int(os.getenv("RSV_INDEXING_EMBEDDER_MAX_TOKENS", "512"))
RSV_CROSS_ENCODER_MAX_TOKENS = int(os.getenv("RSV_CROSS_ENCODER_MAX_TOKENS", "600"))
RSV_ENABLE_CROSS_ENCODER = (os.getenv("RSV_ENABLE_CROSS_ENCODER", "true").lower() in ("1", "true", "yes"))
RSV_MAX_RERANK = int(os.getenv("RSV_MAX_RERANK", "256"))

# TGI env vars
RSV_TGI_REPLICAS = int(os.getenv("RSV_TGI_REPLICAS", "1"))
RSV_TGI_GPUS_PER_REPLICA = float(os.getenv("RSV_TGI_GPUS_PER_REPLICA", "0"))
RSV_TGI_DEVICE = os.getenv("RSV_TGI_DEVICE", "cpu")
RSV_TGI_LAUNCHER_CMD = os.getenv("RSV_TGI_LAUNCHER_CMD", "text-generation-launcher")
RSV_TGI_HOST = os.getenv("RSV_TGI_HOST", "127.0.0.1")
RSV_TGI_PORT = int(os.getenv("RSV_TGI_PORT", "8081"))
RSV_TGI_EXTRA_ARGS = os.getenv("RSV_TGI_EXTRA_ARGS", "")
RSV_TGI_MODEL_ID = os.getenv("RSV_TGI_MODEL_ID", "Qwen/Qwen3-0.6B")

# Per-node custom resource token name (must match autoscaler YAML)
RSV_GPU_NODE_RESOURCE = os.getenv("RSV_GPU_NODE_RESOURCE", "GPU_NODE")

# --- helpers
def _ensure_file(path: str):
    if not os.path.exists(path):
        raise FileNotFoundError(path)

def _wait_port(host: str, port: int, timeout: int = 60):
    start = time.time()
    while time.time() - start < timeout:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except Exception:
            time.sleep(0.5)
    return False

# --- ONNX utilities
if ort is not None:
    def make_session(path: str):
        so = ort.SessionOptions()
        so.intra_op_num_threads = RSV_ORT_INTRA_THREADS
        so.inter_op_num_threads = RSV_ORT_INTER_THREADS
        providers = ["CUDAExecutionProvider", "CPUExecutionProvider"] if RSV_ONNX_USE_CUDA else ["CPUExecutionProvider"]
        sess = ort.InferenceSession(path, sess_options=so, providers=providers)
        return sess
else:
    def make_session(path: str):
        raise RuntimeError("onnxruntime not installed in this environment")

def mean_pool(last_hidden: np.ndarray, attention_mask: np.ndarray) -> np.ndarray:
    mask = attention_mask.astype(np.float32)
    mask = mask[:, :, None]
    summed = (last_hidden * mask).sum(axis=1)
    denom = np.maximum(mask.sum(axis=1), 1e-9)
    return summed / denom

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

# ---------------- ONNX Embed deployment ----------------
@serve.deployment(
    name=RSV_EMBED_DEPLOYMENT,
    num_replicas=RSV_EMBED_REPLICAS,
    ray_actor_options={
        "num_gpus": RSV_EMBED_GPU,
        "resources": ({RSV_GPU_NODE_RESOURCE: 1} if (RSV_ONNX_USE_CUDA and RSV_EMBED_GPU > 0) else {})
    }
)
class ONNXEmbed:
    def __init__(self, onnx_path: str = RSV_ONNX_EMBED_PATH, tokenizer_path: str = RSV_ONNX_EMBED_TOKENIZER_PATH):
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

        # probe run to infer embedding dim (small)
        probe_len = _effective_max_length(self.tokenizer, RSV_INDEXING_EMBEDDER_MAX_TOKENS, RSV_INDEXING_EMBEDDER_MAX_TOKENS)
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
        log.info("[%s] startup OK: embed_dim=%d", RSV_EMBED_DEPLOYMENT, self._embed_dim)

    async def __call__(self, request_or_payload):
        try:
            # parse body
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
            # health probe support
            if isinstance(body, dict) and body.get("_health"):
                return {"ok": True, "embed_dim": getattr(self, "_embed_dim", None)}

            texts = body.get("texts", []) if isinstance(body, dict) else []
            if not isinstance(texts, list):
                texts = [texts]

            requested_max = None
            if isinstance(body, dict):
                try:
                    requested_max = int(body.get("max_length", None)) if body.get("max_length", None) is not None else None
                except Exception:
                    requested_max = None

            eff_max = _effective_max_length(self.tokenizer, requested_max, RSV_INDEXING_EMBEDDER_MAX_TOKENS)
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

            # run ONNX in threadpool to avoid blocking event loop
            outputs = await asyncio.to_thread(self.sess.run, None, ort_inputs)

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

# ---------------- ONNX Reranker ----------------
@serve.deployment(
    name=RSV_RERANK_DEPLOYMENT,
    num_replicas=RSV_RERANK_REPLICAS,
    ray_actor_options={
        "num_gpus": RSV_RERANK_GPU,
        "resources": ({RSV_GPU_NODE_RESOURCE: 1} if (RSV_ONNX_USE_CUDA and RSV_RERANK_GPU > 0) else {})
    }
)
class ONNXRerank:
    def __init__(self, onnx_path: str = RSV_ONNX_RERANK_PATH, tokenizer_path: str = RSV_ONNX_RERANK_TOKENIZER_PATH):
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

        probe_len = _effective_max_length(self.tokenizer, RSV_CROSS_ENCODER_MAX_TOKENS, RSV_CROSS_ENCODER_MAX_TOKENS)
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

        log.info("[%s] startup OK: sample scores shape %s", RSV_RERANK_DEPLOYMENT, derived.shape)

    async def __call__(self, request_or_payload):
        try:
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

            # health probe
            if isinstance(body, dict) and body.get("_health"):
                return {"ok": True}

            q = body.get("query", "") if isinstance(body, dict) else ""
            cands = body.get("cands", []) if isinstance(body, dict) else []
            if not isinstance(cands, list):
                cands = [cands]
            cands = cands[:RSV_MAX_RERANK]
            if len(cands) == 0:
                return {"scores": []}

            requested_max = None
            if isinstance(body, dict):
                try:
                    requested_max = int(body.get("max_length", None)) if body.get("max_length", None) is not None else None
                except Exception:
                    requested_max = None

            eff_max = _effective_max_length(self.tokenizer, requested_max, RSV_CROSS_ENCODER_MAX_TOKENS)
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

            outputs = await asyncio.to_thread(self.sess.run, None, ort_inputs)
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

# ---------------- TGI wrapper deployment ----------------
@serve.deployment(
    name="tgi_llm",
    num_replicas=RSV_TGI_REPLICAS,
    ray_actor_options={
        "num_gpus": RSV_TGI_GPUS_PER_REPLICA,
        "resources": ({RSV_GPU_NODE_RESOURCE: 1} if (RSV_TGI_GPUS_PER_REPLICA and float(RSV_TGI_GPUS_PER_REPLICA) > 0) else {})
    }
)
class TGIServerWrapper:
    def __init__(self):
        # Build TGI command
        self.tgi_cmd = [
            RSV_TGI_LAUNCHER_CMD,
            "--model-id", RSV_TGI_MODEL_ID,
            "--host", RSV_TGI_HOST,
            "--port", str(RSV_TGI_PORT),
            "--device", RSV_TGI_DEVICE,
        ]
        if RSV_TGI_EXTRA_ARGS:
            self.tgi_cmd += RSV_TGI_EXTRA_ARGS.split()

        if httpx is None:
            raise RuntimeError("httpx is required in this environment for TGI wrapper")

        self._start_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._proc: Optional[subprocess.Popen] = None
        self._restart_count = 0
        self._max_restarts = int(os.getenv("RSV_TGI_MAX_RESTARTS", "5"))
        self._backoff_base = float(os.getenv("RSV_TGI_RESTART_BACKOFF", "2.0"))
        self._start_and_supervise()

        # async client used in __call__
        self._client = httpx.AsyncClient(base_url=f"http://{RSV_TGI_HOST}:{RSV_TGI_PORT}", timeout=60.0)

    def _start_and_supervise(self):
        # start subprocess and a background thread to supervise and restart if it dies
        with self._start_lock:
            self._stop_event.clear()
            self._start_proc()
            t = threading.Thread(target=self._supervisor, daemon=True)
            t.start()

    def _start_proc(self):
        log.info("Starting TGI subprocess: %s", " ".join(self.tgi_cmd))
        self._proc = subprocess.Popen(self.tgi_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        # wait for port
        if not _wait_port(RSV_TGI_HOST, RSV_TGI_PORT, timeout=120):
            # capture a snippet of stderr
            stderr = ""
            try:
                _, stderr = self._proc.communicate(timeout=1)
            except Exception:
                pass
            raise RuntimeError(f"TGI did not start on {RSV_TGI_HOST}:{RSV_TGI_PORT}; stderr: {stderr}")

    def _supervisor(self):
        # monitor subprocess; restart on exit with backoff up to _max_restarts
        while not self._stop_event.is_set():
            if self._proc is None:
                time.sleep(1.0)
                continue
            ret = self._proc.poll()
            if ret is None:
                time.sleep(1.0)
                continue
            # process exited
            self._restart_count += 1
            log.error("TGI subprocess exited with returncode=%s", ret)
            if self._restart_count > self._max_restarts:
                log.error("TGI exceeded max restarts (%d). Giving up and letting actor die.", self._max_restarts)
                # let actor die by raising in a background thread is not possible; instead set stop flag and exit
                self._stop_event.set()
                # Terminate actor by raising in main thread next time __call__ invoked (we set proc=None)
                self._proc = None
                break
            # exponential backoff
            backoff = self._backoff_base ** (self._restart_count - 1)
            log.info("Restarting TGI in %.1f seconds (restart #%d)", backoff, self._restart_count)
            time.sleep(backoff)
            try:
                self._start_proc()
            except Exception as e:
                log.exception("TGI restart failed: %s", e)
                # loop and attempt again until max restarts reached

    async def __call__(self, request_or_payload):
        # parse body
        if isinstance(request_or_payload, (dict, list)):
            body = request_or_payload
        else:
            try:
                body = await request_or_payload.json()
            except Exception:
                body = {}

        # health probe
        if isinstance(body, dict) and body.get("_health"):
            proc_alive = (self._proc is not None and self._proc.poll() is None)
            return {"ok": proc_alive}

        prompt = body.get("prompt") or body.get("input") or ""
        params = body.get("params", {})

        payload = {"inputs": prompt, "parameters": params}
        if self._proc is None or (self._proc is not None and self._proc.poll() is not None):
            log.error("TGI subprocess not alive when request arrived")
            raise RuntimeError("TGI not available")

        # async HTTP call to local TGI
        try:
            resp = await self._client.post("/v1/generate", json=payload)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            log.exception("TGI call failed: %s", e)
            # on transient failures, optionally restart process by setting proc=None
            raise

    def __del__(self):
        try:
            self._stop_event.set()
            if getattr(self, "_client", None) is not None:
                try:
                    asyncio.get_event_loop().run_until_complete(self._client.aclose())
                except Exception:
                    pass
            if getattr(self, "_proc", None) is not None:
                try:
                    self._proc.terminate()
                except Exception:
                    pass
        except Exception:
            pass

# ----------------- main() --------------------
def main():
    # init ray
    ray.init(address=RSV_RAY_ADDRESS, namespace=RSV_RAY_NAMESPACE, ignore_reinit_error=True)
    http_opts = HTTPOptions(host=RSV_SERVE_HOST, port=RSV_SERVE_PORT)
    serve.start(detached=True, http_options=http_opts)

    binds = [ONNXEmbed.bind()]
    if RSV_ENABLE_CROSS_ENCODER:
        binds.append(ONNXRerank.bind())
    binds.append(TGIServerWrapper.bind())

    serve.run(*binds)

    names = [RSV_EMBED_DEPLOYMENT] + ([RSV_RERANK_DEPLOYMENT] if RSV_ENABLE_CROSS_ENCODER else []) + ["tgi_llm"]
    log.info("Deployments started: %s", " ".join(names))
    log.info("Serve HTTP listening on %s:%d", RSV_SERVE_HOST, RSV_SERVE_PORT)

if __name__ == "__main__":
    main()
