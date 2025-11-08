"""
CPU-first Ray Serve deployment for:
 - ONNX embedder (ONNXEmbed)
 - ONNX cross-encoder reranker (ONNXRerank)
 - optional LLM powered by llama-cpp-python (LlamaServe)

This file contains a self-contained, production-oriented single-file deployment:
 - Early BLAS/OMP env tuning to avoid thread oversubscription.
 - One ONNXSession / Llama instance per actor process.
 - Offload blocking work (onnxruntime sess.run, tokenization, llama calls) to threadpool via run_in_executor.
 - Bounded concurrency inside actors via asyncio.Semaphore.
 - Defensive guards for outputs and env parsing.
 - Fully configurable via environment variables.
"""

from __future__ import annotations
import os, sys, time, json, logging, math, threading, queue, traceback
from typing import Dict, Any, Optional, List, Tuple
import numpy as np
import concurrent.futures
import asyncio
import ray
from ray import serve
from ray.serve import HTTPOptions
from transformers import PreTrainedTokenizerFast
try:
    import onnxruntime as ort
except Exception as e:
    raise ImportError("onnxruntime import error: " + str(e))
try:
    from llama_cpp import Llama
    _LLAMA_AVAILABLE = True
except Exception:
    Llama = None
    _LLAMA_AVAILABLE = False
_log_level_name = os.getenv("LOG_LEVEL", "INFO") or "INFO"
_log_level = getattr(logging, _log_level_name.upper(), logging.INFO)
logging.basicConfig(level=_log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("rayserve_models_cpu")
def _env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None:
        return default
    try:
        return int(v)
    except Exception:
        return default
def _env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    if v is None:
        return default
    try:
        return float(v)
    except Exception:
        return default
def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.lower() in ("1", "true", "yes", "on")
RAY_ADDRESS = os.getenv("RAY_ADDRESS") or None
RAY_NAMESPACE = os.getenv("RAY_NAMESPACE") or None
SERVE_HTTP_HOST = os.getenv("SERVE_HTTP_HOST", "127.0.0.1")
SERVE_HTTP_PORT = _env_int("SERVE_HTTP_PORT", 8003)
EMBED_DEPLOYMENT = os.getenv("EMBED_DEPLOYMENT", "embed_onxx_cpu")
RERANK_HANDLE_NAME = os.getenv("RERANK_HANDLE_NAME", "rerank_onxx_cpu")
LLM_DEPLOYMENT_NAME = os.getenv("LLM_DEPLOYMENT_NAME", "llm_server_cpu")
ONNX_EMBED_PATH = "/workspace/models/gte-modernbert-base/onnx/model_int8.onnx"
ONNX_EMBED_TOKENIZER_PATH = "/workspace/models/gte-modernbert-base/tokenizer.json"
ONNX_RERANK_PATH = "/workspace/models/ms-marco-TinyBERT-L2-v2/onnx/model_qint8_arm64.onnx"
ONNX_RERANK_TOKENIZER_PATH = "/workspace/models/ms-marco-TinyBERT-L2-v2/tokenizer.json"
LLM_PATH = "/workspace/models/Qwen3-0.6B-GGUF/Qwen3-0.6B-Q4_K_M.gguf"
EMBED_REPLICAS = _env_int("EMBED_REPLICAS", 1)
RERANK_REPLICAS = _env_int("RERANK_REPLICAS", 1)
EMBED_NUM_CPUS_PER_REPLICA = _env_float("EMBED_NUM_CPUS_PER_REPLICA", 1.0)
RERANK_NUM_CPUS_PER_REPLICA = _env_float("RERANK_NUM_CPUS_PER_REPLICA", 1.0)
EMBED_MAX_REPLICAS_PER_NODE = _env_int("EMBED_MAX_REPLICAS_PER_NODE", 1)
RERANK_MAX_REPLICAS_PER_NODE = _env_int("RERANK_MAX_REPLICAS_PER_NODE", 1)
ORT_INTRA_THREADS = _env_int("ORT_INTRA_THREADS", max(1, int(EMBED_NUM_CPUS_PER_REPLICA)))
ORT_INTER_THREADS = _env_int("ORT_INTER_THREADS", 1)
INDEXING_EMBEDDER_MAX_TOKENS = _env_int("INDEXING_EMBEDDER_MAX_TOKENS", 512)
INFERENCE_EMBEDDER_MAX_TOKENS = _env_int("INFERENCE_EMBEDDER_MAX_TOKENS", 64)
CROSS_ENCODER_MAX_TOKENS = _env_int("CROSS_ENCODER_MAX_TOKENS", 600)
ENABLE_CROSS_ENCODER = _env_bool("ENABLE_CROSS_ENCODER", True)
LLM_ENABLE = _env_bool("LLM_ENABLE", True)
LLM_REPLICAS = _env_int("LLM_REPLICAS", 1)
LLM_NUM_CPUS_PER_REPLICA = _env_float("LLM_NUM_CPUS_PER_REPLICA", 4.0)
LLM_N_THREADS = _env_int("LLM_N_THREADS", max(1, int(LLM_NUM_CPUS_PER_REPLICA)))
LLM_MAX_CONCURRENCY = _env_int("LLM_MAX_CONCURRENCY", max(1, int(LLM_NUM_CPUS_PER_REPLICA)))
LLM_EXECUTOR_WORKERS = _env_int("LLM_EXECUTOR_WORKERS", max(1, int(LLM_NUM_CPUS_PER_REPLICA)))
LLM_MAX_REPLICAS_PER_NODE = _env_int("LLM_MAX_REPLICAS_PER_NODE", max(1, (os.cpu_count() or 1)))
EMBED_BATCH_MAX_SIZE = _env_int("EMBED_BATCH_MAX_SIZE", 16)
EMBED_BATCH_WAIT_S = _env_float("EMBED_BATCH_WAIT_S", 0.05)
RERANK_BATCH_MAX_SIZE = _env_int("RERANK_BATCH_MAX_SIZE", 8)
RERANK_BATCH_WAIT_S = _env_float("RERANK_BATCH_WAIT_S", 0.05)
MAX_RERANK = _env_int("MAX_RERANK", 256)
try:
    ort.set_default_logger_severity(3)
except Exception:
    pass
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
def make_session(path: str, intra_threads: int = ORT_INTRA_THREADS, inter_threads: int = ORT_INTER_THREADS):
    so = ort.SessionOptions()
    so.intra_op_num_threads = int(max(1, intra_threads))
    so.inter_op_num_threads = int(max(1, inter_threads))
    try:
        so.enable_mem_pattern = True
        so.enable_cpu_mem_arena = True
    except Exception:
        pass
    providers = ["CPUExecutionProvider"]
    sess = ort.InferenceSession(path, sess_options=so, providers=providers)
    return sess
def mean_pool(last_hidden: np.ndarray, attention_mask: np.ndarray) -> np.ndarray:
    mask = attention_mask.astype(np.float32)
    mask = mask[:, :, None]
    summed = (last_hidden * mask).sum(axis=1)
    denom = np.maximum(mask.sum(axis=1), 1e-9)
    return summed / denom
class _FdSuppress:
    def __init__(self, fds=(1,2)):
        self._fds = fds
        self._devnull = None
        self._saved = {}
    def __enter__(self):
        self._devnull = open(os.devnull, "wb")
        try:
            sys.stdout.flush(); sys.stderr.flush()
        except Exception:
            pass
        for fd in self._fds:
            try:
                self._saved[fd] = os.dup(fd)
                os.dup2(self._devnull.fileno(), fd)
            except Exception:
                pass
        return self
    def __exit__(self, exc_type, exc, tb):
        for fd, saved in self._saved.items():
            try:
                os.dup2(saved, fd); os.close(saved)
            except Exception:
                pass
        try:
            if self._devnull: self._devnull.close()
        except Exception:
            pass
@serve.deployment(name=EMBED_DEPLOYMENT, num_replicas=EMBED_REPLICAS, ray_actor_options={"num_gpus": 0.0, "num_cpus": EMBED_NUM_CPUS_PER_REPLICA})
class ONNXEmbed:
    def __init__(self, onnx_path: str = ONNX_EMBED_PATH, tokenizer_path: str = ONNX_EMBED_TOKENIZER_PATH):
        _ensure_file(tokenizer_path); self.tokenizer = PreTrainedTokenizerFast(tokenizer_file=tokenizer_path)
        if getattr(self.tokenizer, "pad_token", None) is None:
            if getattr(self.tokenizer, "eos_token", None) is not None:
                self.tokenizer.pad_token = self.tokenizer.eos_token
            elif getattr(self.tokenizer, "sep_token", None) is not None:
                self.tokenizer.pad_token = self.tokenizer.sep_token
            else:
                self.tokenizer.add_special_tokens({"pad_token":"[PAD]"})
        _ensure_file(onnx_path)
        intra = max(1, int(max(1, EMBED_NUM_CPUS_PER_REPLICA)))
        inter = max(1, int(ORT_INTER_THREADS))
        self.sess = make_session(onnx_path, intra_threads=intra, inter_threads=inter)
        self.input_names = [inp.name for inp in self.sess.get_inputs()]
        self.max_concurrency = _env_int("EMBED_MAX_CONCURRENCY", max(1, intra))
        self._sem = asyncio.Semaphore(self.max_concurrency)
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, intra))
        self._batch_q: "queue.Queue[Tuple[str, concurrent.futures.Future]]" = queue.Queue()
        self._stop_worker = threading.Event()
        self._worker = threading.Thread(target=self._batch_worker, daemon=True)
        self._worker.start()
        try:
            toks = self.tokenizer(["health-check"], padding=True, truncation=True, return_tensors="np", max_length=min(8, INDEXING_EMBEDDER_MAX_TOKENS))
            ort_inputs = {}
            for k, v in toks.items():
                if k in self.input_names:
                    ort_inputs[k] = v.astype("int64")
                else:
                    if k == "input_ids":
                        for cand in ("input_ids","input","input.1"):
                            if cand in self.input_names:
                                ort_inputs[cand] = v.astype("int64"); break
            outputs = self.sess.run(None, ort_inputs)
            out = None
            for arr in outputs:
                arr = np.asarray(arr)
                if arr.ndim == 3:
                    attn = toks.get("attention_mask", np.ones(arr.shape[:2], dtype="int64"))
                    out = mean_pool(arr, attn); break
                if arr.ndim == 2:
                    out = arr; break
            if out is None:
                last = np.asarray(outputs[-1])
                if last.ndim > 2:
                    out = last.reshape((last.shape[0], -1))
                else:
                    out = last
            if out is None or out.ndim != 2:
                raise RuntimeError("Unable to infer embedding output shape from ONNX outputs.")
            self._embed_dim = int(out.shape[1])
            self.ready = True
            log.info("[%s] initialized embed_dim=%d intra=%d", EMBED_DEPLOYMENT, self._embed_dim, intra)
        except Exception:
            log.exception("ONNXEmbed probe failed"); raise
    def _batch_worker(self):
        while not self._stop_worker.is_set():
            try:
                item = self._batch_q.get(timeout=0.5)
            except Exception:
                continue
            items = [item]
            start = time.time()
            while len(items) < max(1, EMBED_BATCH_MAX_SIZE):
                try:
                    to_add = self._batch_q.get(timeout=max(0, EMBED_BATCH_WAIT_S - (time.time()-start)))
                    items.append(to_add)
                except Exception:
                    break
            texts = [t for t, fut in items]
            try:
                toks = self.tokenizer(texts, padding=True, truncation=True, return_tensors="np", max_length=INFERENCE_EMBEDDER_MAX_TOKENS)
                ort_inputs = {}
                for k, v in toks.items():
                    if k in self.input_names:
                        ort_inputs[k] = v.astype("int64")
                    else:
                        if k == "input_ids":
                            for cand in ("input_ids","input","input.1"):
                                if cand in self.input_names:
                                    ort_inputs[cand] = v.astype("int64"); break
                outputs = self.sess.run(None, ort_inputs)
                vecs = None
                for arr in outputs:
                    arr = np.asarray(arr)
                    if arr.ndim == 3 and arr.shape[0] == len(texts):
                        attn = toks.get("attention_mask", np.ones(arr.shape[:2], dtype="int64"))
                        vecs = mean_pool(arr, attn); break
                    if arr.ndim == 2 and arr.shape[0] == len(texts):
                        vecs = arr; break
                if vecs is None:
                    last = np.asarray(outputs[-1])
                    if last.ndim > 2 and last.shape[0] == len(texts):
                        vecs = last.reshape((last.shape[0], -1))
                    else:
                        raise RuntimeError("ONNX embed outputs invalid shapes")
                norms = np.linalg.norm(vecs, axis=1, keepdims=True)
                norms = np.maximum(norms, 1e-12)
                vecs = (vecs / norms).astype(float)
                for (_, fut), v in zip(items, vecs):
                    try:
                        fut.set_result(v.tolist())
                    except Exception:
                        pass
            except Exception as e:
                tb = traceback.format_exc()
                log.exception("embed batch worker error: %s", tb)
                for _, fut in items:
                    try:
                        fut.set_exception(e)
                    except Exception:
                        pass
    async def __call__(self, request_or_payload):
        try:
            if isinstance(request_or_payload, (dict, list)):
                body = request_or_payload
            else:
                try:
                    body = await request_or_payload.json()
                except Exception:
                    try:
                        raw = await request_or_payload.body(); body = json.loads(raw.decode("utf-8")) if raw else {}
                    except Exception:
                        body = {}
            texts = body.get("texts", []) if isinstance(body, dict) else []
            if not isinstance(texts, list):
                texts = [texts]
            loop = asyncio.get_running_loop()
            futures_list = []
            for txt in texts:
                fut = concurrent.futures.Future()
                self._batch_q.put((txt, fut))
                futures_list.append(fut)
            try:
                results = await asyncio.gather(*(asyncio.wrap_future(f) for f in futures_list))
            except Exception as e:
                log.exception("embed request gather error"); raise
            return {"vectors": [r for r in results], "max_length_used": int(INFERENCE_EMBEDDER_MAX_TOKENS)}
        except Exception:
            log.exception("embed call error"); raise
    async def health(self):
        return {"ready": getattr(self, "ready", False)}
    def __del__(self):
        try:
            self._stop_worker.set()
        except Exception:
            pass
        try:
            self._executor.shutdown(wait=False)
        except Exception:
            pass
@serve.deployment(name=RERANK_HANDLE_NAME, num_replicas=RERANK_REPLICAS, ray_actor_options={"num_gpus": 0.0, "num_cpus": RERANK_NUM_CPUS_PER_REPLICA})
class ONNXRerank:
    def __init__(self, onnx_path: str = ONNX_RERANK_PATH, tokenizer_path: str = ONNX_RERANK_TOKENIZER_PATH):
        _ensure_file(tokenizer_path); self.tokenizer = PreTrainedTokenizerFast(tokenizer_file=tokenizer_path)
        if getattr(self.tokenizer, "pad_token", None) is None:
            if getattr(self.tokenizer, "eos_token", None) is not None:
                self.tokenizer.pad_token = self.tokenizer.eos_token
            elif getattr(self.tokenizer, "sep_token", None) is not None:
                self.tokenizer.pad_token = self.tokenizer.sep_token
            else:
                self.tokenizer.add_special_tokens({"pad_token":"[PAD]"})
        _ensure_file(onnx_path)
        intra = max(1, int(max(1, RERANK_NUM_CPUS_PER_REPLICA)))
        inter = max(1, int(ORT_INTER_THREADS))
        self.sess = make_session(onnx_path, intra_threads=intra, inter_threads=inter)
        self.input_names = [inp.name for inp in self.sess.get_inputs()]
        self.max_concurrency = _env_int("RERANK_MAX_CONCURRENCY", max(1, intra))
        self._sem = asyncio.Semaphore(self.max_concurrency)
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, intra))
        try:
            toks = self.tokenizer([("q","a"),("q","b")], padding=True, truncation='only_second', return_tensors="np", max_length=min(8, CROSS_ENCODER_MAX_TOKENS))
            ort_inputs = {}
            for k, v in toks.items():
                if k in self.input_names:
                    ort_inputs[k] = v.astype("int64")
                else:
                    if k == "input_ids":
                        for cand in ("input_ids","input","input.1"):
                            if cand in self.input_names:
                                ort_inputs[cand] = v.astype("int64"); break
            outs = self.sess.run(None, ort_inputs)
            derived = None
            for arr in outs:
                arr = np.asarray(arr)
                if arr.ndim == 1 and arr.shape[0] == 2:
                    derived = arr; break
                if arr.ndim == 2 and arr.shape[0] == 2:
                    derived = arr[:,0]; break
            if derived is None:
                derived = np.asarray(outs[-1]).reshape(2, -1)[:,0]
            self.ready = True
            log.info("[%s] rerank initialized sample_scores=%s intra=%d", RERANK_HANDLE_NAME, derived.shape, intra)
        except Exception:
            log.exception("ONNXRerank probe failed"); raise
    async def __call__(self, request_or_payload):
        try:
            if isinstance(request_or_payload, (dict, list)):
                body = request_or_payload
            else:
                try:
                    body = await request_or_payload.json()
                except Exception:
                    try:
                        raw = await request_or_payload.body(); body = json.loads(raw.decode("utf-8")) if raw else {}
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
            loop = asyncio.get_running_loop()
            def _tokenize():
                return self.tokenizer([(q, t) for t in cands], padding=True, truncation='only_second', return_tensors="np", max_length=eff_max)
            toks = await loop.run_in_executor(self._executor, _tokenize)
            ort_inputs = {}
            for k, v in toks.items():
                if k in self.input_names:
                    ort_inputs[k] = v.astype("int64")
                else:
                    if k == "input_ids":
                        for cand in ("input_ids","input","input.1"):
                            if cand in self.input_names:
                                ort_inputs[cand] = v.astype("int64"); break
            async with self._sem:
                outputs = await loop.run_in_executor(self._executor, lambda: self.sess.run(None, ort_inputs))
            scores = None
            for arr in outputs:
                arr = np.asarray(arr)
                if arr.ndim == 1 and arr.shape[0] == len(cands):
                    scores = arr; break
                if arr.ndim == 2 and arr.shape[0] == len(cands):
                    scores = arr[:,0]; break
            if scores is None:
                last = np.asarray(outputs[-1])
                try:
                    scores = last.reshape(len(cands), -1)[:,0]
                except Exception as e:
                    raise RuntimeError("unable to parse reranker outputs: " + str(e))
            return {"scores": [float(s) for s in np.asarray(scores).astype(float)], "max_length_used": int(eff_max)}
        except Exception:
            log.exception("rerank call error"); raise
    async def health(self):
        return {"ready": getattr(self, "ready", False)}
    def __del__(self):
        try:
            self._executor.shutdown(wait=False)
        except Exception:
            pass
@serve.deployment(name=LLM_DEPLOYMENT_NAME, num_replicas=LLM_REPLICAS, ray_actor_options={"num_gpus": 0.0, "num_cpus": LLM_NUM_CPUS_PER_REPLICA})
class LlamaServe:
    def __init__(self, model_path: str = LLM_PATH, n_threads: int = LLM_N_THREADS, max_concurrency: int = LLM_MAX_CONCURRENCY):
        if not _LLAMA_AVAILABLE:
            raise RuntimeError("llama-cpp-python not available")
        _ensure_file(model_path)
        try:
            start_core = _env_int("START_CORE", 0)
            cores_per = _env_int("CORES_PER_REPLICA", max(1, int(LLM_NUM_CPUS_PER_REPLICA)))
            if hasattr(os, "sched_setaffinity"):
                try:
                    os.sched_setaffinity(0, set(range(start_core, start_core + cores_per)))
                except Exception:
                    pass
        except Exception:
            pass
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, int(LLM_EXECUTOR_WORKERS)))
        self._sem = asyncio.Semaphore(max(1, int(max_concurrency)))
        try:
            init_kwargs: Dict[str, Any] = {}
            init_kwargs["n_threads"] = int(max(1, n_threads))
            if "verbose" in getattr(Llama, "__init__", lambda *a, **k: None).__code__.co_varnames:
                init_kwargs["verbose"] = False
            with _FdSuppress():
                try:
                    self.llm = Llama(model_path=model_path, **init_kwargs)
                except TypeError:
                    self.llm = Llama(model_path=model_path)
            try:
                _ = self.llm("health-check", max_tokens=1)
            except Exception:
                pass
            self.ready = True
            log.info("[%s] loaded model %s n_threads=%d max_concurrency=%d", LLM_DEPLOYMENT_NAME, model_path, n_threads, max_concurrency)
        except Exception:
            log.exception("Failed loading LLM model"); raise
    async def generate(self, prompt: str, **kwargs) -> Dict[str, Any]:
        async with self._sem:
            loop = asyncio.get_running_loop()
            try:
                result = await loop.run_in_executor(self._executor, lambda: self.llm(prompt, **kwargs))
            except Exception as e:
                raise RuntimeError("llama-cpp-python call failed: " + str(e)) from e
            if hasattr(result, "to_dict"):
                try:
                    return result.to_dict()
                except Exception:
                    pass
            return result
    async def __call__(self, request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        prompt = body.get("prompt", "") or body.get("input", "") or ""
        params = body.get("params", {}) or {}
        if not isinstance(params, dict):
            params = {}
        res = await self.generate(prompt, **params)
        return res
    async def health(self):
        return {"ready": getattr(self, "ready", False)}
    def __del__(self):
        try:
            self._executor.shutdown(wait=False)
        except Exception:
            pass
def _build_deploy_options(prefix: str, fixed_replicas_env: int, default_num_gpus: float, default_num_cpus: float, max_replicas_per_node_env: int):
    mode = os.getenv(f"{prefix}_REPLICA_MODE", "fixed").lower()
    options: Dict[str, Any] = {}
    num_gpus = _env_float(f"{prefix}_NUM_GPUS_PER_REPLICA", default_num_gpus)
    if num_gpus != 0.0:
        num_gpus = 0.0
    num_cpus = _env_float(f"{prefix}_NUM_CPUS_PER_REPLICA", default_num_cpus)
    options["ray_actor_options"] = {"num_gpus": num_gpus, "num_cpus": num_cpus}
    options["max_replicas_per_node"] = _env_int(f"{prefix}_MAX_REPLICAS_PER_NODE", max_replicas_per_node_env)
    if mode == "auto":
        override = os.getenv(f"{prefix}_AUTOSCALING_CONFIG_OVERRIDE", "")
        cfg_str = override or os.getenv(f"{prefix}_AUTOSCALING_CONFIG", "")
        cfg = None
        try:
            if cfg_str:
                cfg = json.loads(cfg_str)
        except Exception:
            log.exception("Invalid autoscaling JSON: %s", cfg_str)
        if cfg is None:
            cfg = {"min_replicas": 1, "max_replicas": max(1, fixed_replicas_env), "target_num_ongoing_requests": 2}
        options["num_replicas"] = "auto"
        options["autoscaling_config"] = cfg
    else:
        fixed = _env_int(f"{prefix}_REPLICAS_FIXED", fixed_replicas_env)
        options["num_replicas"] = fixed
    return options
def deploy_binds(binds: List[Any]):
    try:
        serve.run(*binds); return
    except Exception:
        pass
    try:
        serve.run(list(binds)); return
    except Exception:
        pass
    for b in binds:
        try:
            if hasattr(b, "deploy"):
                b.deploy(); continue
        except Exception:
            pass
        try:
            serve.run(b)
        except Exception as e:
            log.exception("Failed to deploy binding %s: %s", type(b), e)
            raise
def main():
    ray.init(address=RAY_ADDRESS, namespace=RAY_NAMESPACE, ignore_reinit_error=True)
    http_opts = HTTPOptions(host=SERVE_HTTP_HOST, port=SERVE_HTTP_PORT)
    serve.start(detached=True, http_options=http_opts)
    binds: List[Any] = []
    embed_opts = _build_deploy_options("EMBED", EMBED_REPLICAS, 0.0, EMBED_NUM_CPUS_PER_REPLICA, EMBED_MAX_REPLICAS_PER_NODE)
    embed_bind = ONNXEmbed.options(**embed_opts).bind(ONNX_EMBED_PATH, ONNX_EMBED_TOKENIZER_PATH)
    binds.append(embed_bind)
    if ENABLE_CROSS_ENCODER:
        rerank_opts = _build_deploy_options("RERANK", RERANK_REPLICAS, 0.0, RERANK_NUM_CPUS_PER_REPLICA, RERANK_MAX_REPLICAS_PER_NODE)
        rerank_bind = ONNXRerank.options(**rerank_opts).bind(ONNX_RERANK_PATH, ONNX_RERANK_TOKENIZER_PATH)
        binds.append(rerank_bind)
    if LLM_ENABLE and _LLAMA_AVAILABLE:
        llm_opts = _build_deploy_options("LLM", LLM_REPLICAS, 0.0, LLM_NUM_CPUS_PER_REPLICA, LLM_MAX_REPLICAS_PER_NODE)
        llm_opts.setdefault("ray_actor_options", {})
        llm_opts["ray_actor_options"]["num_gpus"] = 0.0
        llm_opts["ray_actor_options"].setdefault("num_cpus", float(LLM_NUM_CPUS_PER_REPLICA))
        llm_bind = LlamaServe.options(**llm_opts).bind(LLM_PATH, LLM_N_THREADS, LLM_MAX_CONCURRENCY)
        binds.append(llm_bind)
    for b in binds:
        log.info("bind type: %s", type(b))
    deploy_binds(binds)
    names: List[str] = [EMBED_DEPLOYMENT] + ([RERANK_HANDLE_NAME] if ENABLE_CROSS_ENCODER else [])
    if LLM_ENABLE and _LLAMA_AVAILABLE:
        names += [LLM_DEPLOYMENT_NAME]
    log.info("Deployments started: %s", " ".join(names))
    log.info("Serve HTTP listening on %s:%d", SERVE_HTTP_HOST, SERVE_HTTP_PORT)
if __name__ == "__main__":
    main()
