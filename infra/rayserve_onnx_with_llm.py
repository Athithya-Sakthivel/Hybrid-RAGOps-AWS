# infra/rayserve_three_independent.py
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

# Logging
_log_level_name = os.getenv("LOG_LEVEL", "INFO") or "INFO"
_log_level = getattr(logging, _log_level_name.upper(), logging.INFO)
logging.basicConfig(level=_log_level)
log = logging.getLogger("rayserve_three_independent")

# Ray / Serve env
RAY_ADDRESS = os.getenv("RAY_ADDRESS") or None
RAY_NAMESPACE = os.getenv("RAY_NAMESPACE") or None
SERVE_HTTP_HOST = os.getenv("SERVE_HTTP_HOST", "127.0.0.1")
SERVE_HTTP_PORT = int(os.getenv("SERVE_HTTP_PORT", "8003"))

# ONNX env
EMBED_DEPLOYMENT = os.getenv("EMBED_DEPLOYMENT", "embed_onxx")
RERANK_DEPLOYMENT = os.getenv("RERANK_DEPLOYMENT", "rerank_onxx")

ONNX_USE_CUDA = (os.getenv("ONNX_USE_CUDA", "false").lower() in ("1", "true", "yes"))
MODEL_DIR_EMBED = os.getenv("MODEL_DIR_EMBED", "/workspace/models/gte-modernbert-base/onnx")
MODEL_DIR_RERANK = os.getenv("MODEL_DIR_RERANK", "/workspace/models/ms-marco-TinyBERT-L2-v2/onnx/")
ONNX_EMBED_PATH = os.getenv("ONNX_EMBED_PATH", os.path.join(MODEL_DIR_EMBED, "onnx", "model_int8.onnx"))
ONNX_EMBED_TOKENIZER_PATH = os.getenv("ONNX_EMBED_TOKENIZER_PATH", os.path.join(MODEL_DIR_EMBED, "tokenizer.json"))
ONNX_RERANK_PATH = os.getenv("ONNX_RERANK_PATH", os.path.join(MODEL_DIR_RERANK, "onnx", "model_quint8_avx2.onnx"))
ONNX_RERANK_TOKENIZER_PATH = os.getenv("ONNX_RERANK_TOKENIZER_PATH", os.path.join(MODEL_DIR_RERANK, "tokenizer.json"))

EMBED_REPLICAS = int(os.getenv("EMBED_REPLICAS", "1"))
RERANK_REPLICAS = int(os.getenv("RERANK_REPLICAS", "1"))
EMBED_GPU = int(os.getenv("EMBED_GPU_PER_REPLICA", "0")) if ONNX_USE_CUDA else 0
RERANK_GPU = int(os.getenv("RERANK_GPU_PER_REPLICA", "0")) if ONNX_USE_CUDA else 0
ENABLE_CROSS_ENCODER = (os.getenv("ENABLE_CROSS_ENCODER", "true").lower() in ("1", "true", "yes"))
ORT_INTRA_THREADS = int(os.getenv("ORT_INTRA_THREADS", "1"))
ORT_INTER_THREADS = int(os.getenv("ORT_INTER_THREADS", "1"))
INDEXING_EMBEDDER_MAX_TOKENS = int(os.getenv("INDEXING_EMBEDDER_MAX_TOKENS", "512"))
CROSS_ENCODER_MAX_TOKENS = int(os.getenv("CROSS_ENCODER_MAX_TOKENS", "600"))
MAX_RERANK = int(os.getenv("MAX_RERANK", "256"))

# LLM env
ENABLE_LLM = (os.getenv("ENABLE_LLM", "true").lower() in ("1", "true", "yes"))
LLM_DEPLOYMENT = os.getenv("LLM_DEPLOYMENT", "llm_model")
LLM_REPLICAS = int(os.getenv("LLM_REPLICAS", "1"))
LLM_GPU_PER_REPLICA = int(os.getenv("LLM_GPU_PER_REPLICA", "0"))
LLM_PATH = os.getenv("LLM_PATH", "/workspace/models/qwen/Qwen3-0.6B-AWQ")
LLM_USE_CUDA = (os.getenv("LLM_USE_CUDA", "true").lower() in ("1", "true", "yes"))
LLM_MAX_INPUT = int(os.getenv("LLM_MAX_INPUT", "512"))
LLM_MAX_NEW_TOKENS = int(os.getenv("LLM_MAX_NEW_TOKENS", "64"))
LLM_DEVICE_OVERRIDE = os.getenv("LLM_DEVICE", None)
LLM_TRUST_REMOTE_CODE = (os.getenv("LLM_TRUST_REMOTE_CODE", "false").lower() in ("1", "true", "yes"))
LLM_LOW_CPU_MEM_USAGE = (os.getenv("LLM_LOW_CPU_MEM_USAGE", "true").lower() in ("1", "true", "yes"))

# ONNX runtime import and check
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
        raise RuntimeError("ONNX_USE_CUDA=true but CUDAExecutionProvider not available: " + str(providers_avail))

def make_session(path: str):
    so = ort.SessionOptions()
    so.intra_op_num_threads = ORT_INTRA_THREADS
    so.inter_op_num_threads = ORT_INTER_THREADS
    providers = ["CUDAExecutionProvider", "CPUExecutionProvider"] if ONNX_USE_CUDA else ["CPUExecutionProvider"]
    return ort.InferenceSession(path, sess_options=so, providers=providers)

def mean_pool(last_hidden: np.ndarray, attention_mask: np.ndarray) -> np.ndarray:
    mask = attention_mask.astype(np.float32)[:, :, None]
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

# --- Deployments: ONNX Embed ---
@serve.deployment(name=EMBED_DEPLOYMENT, num_replicas=EMBED_REPLICAS, ray_actor_options={"num_gpus": EMBED_GPU})
class ONNXEmbed:
    def __init__(self, onnx_path: str = ONNX_EMBED_PATH, tokenizer_path: str = ONNX_EMBED_TOKENIZER_PATH):
        _ensure_file(tokenizer_path)
        self.tokenizer = PreTrainedTokenizerFast(tokenizer_file=tokenizer_path)
        if getattr(self.tokenizer, "pad_token", None) is None:
            if getattr(self.tokenizer, "eos_token", None) is not None:
                self.tokenizer.pad_token = self.tokenizer.eos_token
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
        log.info("[%s] startup OK embed_dim=%d", EMBED_DEPLOYMENT, self._embed_dim)

    async def __call__(self, payload):
        body = payload if isinstance(payload, (dict, list)) else {}
        if not isinstance(body, dict):
            try:
                body = await payload.json()
            except Exception:
                try:
                    raw = await payload.body()
                    body = json.loads(raw.decode("utf-8")) if raw else {}
                except Exception:
                    body = {}
        texts = body.get("texts", []) if isinstance(body, dict) else []
        if not isinstance(texts, list):
            texts = [texts]
        eff_max = _effective_max_length(self.tokenizer, body.get("max_length", None), INDEXING_EMBEDDER_MAX_TOKENS)
        toks = self.tokenizer(texts, padding=True, truncation=True, return_tensors="np", max_length=eff_max)
        batch = toks["input_ids"].shape[0]
        ort_inputs = {}
        for k, v in toks.items():
            if k in self.input_names:
                ort_inputs[k] = v.astype("int64")
            else:
                if k == "input_ids":
                    for cand in ("input_ids", "input", "input.1"):
                        if cand in self.input_names:
                            ort_inputs[cand] = v.astype("int64"); break
        outputs = self.sess.run(None, ort_inputs)
        vecs = None
        for arr in outputs:
            arr = np.asarray(arr)
            if arr.ndim == 3 and arr.shape[0] == batch:
                attn = toks.get("attention_mask", np.ones(arr.shape[:2], dtype="int64"))
                vecs = mean_pool(arr, attn); break
            if arr.ndim == 2 and arr.shape[0] == batch:
                vecs = arr; break
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

# --- Deployments: ONNX Rerank (optional) ---
@serve.deployment(name=RERANK_DEPLOYMENT, num_replicas=RERANK_REPLICAS, ray_actor_options={"num_gpus": RERANK_GPU})
class ONNXRerank:
    def __init__(self, onnx_path: str = ONNX_RERANK_PATH, tokenizer_path: str = ONNX_RERANK_TOKENIZER_PATH):
        _ensure_file(tokenizer_path)
        self.tokenizer = PreTrainedTokenizerFast(tokenizer_file=tokenizer_path)
        if getattr(self.tokenizer, "pad_token", None) is None:
            if getattr(self.tokenizer, "eos_token", None) is not None:
                self.tokenizer.pad_token = self.tokenizer.eos_token
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
                            ort_inputs[cand] = v.astype("int64"); break
        outs = self.sess.run(None, ort_inputs)
        derived = None
        for arr in outs:
            arr = np.asarray(arr)
            if arr.ndim == 1 and arr.shape[0] == 2:
                derived = arr; break
            if arr.ndim == 2 and arr.shape[0] == 2:
                derived = arr[:, 0]; break
        if derived is None:
            derived = np.asarray(outs[-1]).reshape(2, -1)[:, 0]
        log.info("[%s] startup OK rerank sample shape %s", RERANK_DEPLOYMENT, derived.shape)

    async def __call__(self, payload):
        body = payload if isinstance(payload, (dict, list)) else {}
        if not isinstance(body, dict):
            try:
                body = await payload.json()
            except Exception:
                try:
                    raw = await payload.body()
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
        eff_max = _effective_max_length(self.tokenizer, body.get("max_length", None), CROSS_ENCODER_MAX_TOKENS)
        toks = self.tokenizer([(q, t) for t in cands], padding=True, truncation='only_second', return_tensors="np", max_length=eff_max)
        ort_inputs = {}
        for k, v in toks.items():
            if k in self.input_names:
                ort_inputs[k] = v.astype("int64")
            else:
                if k == "input_ids":
                    for cand in ("input_ids", "input", "input.1"):
                        if cand in self.input_names:
                            ort_inputs[cand] = v.astype("int64"); break
        outputs = self.sess.run(None, ort_inputs)
        scores = None
        for arr in outputs:
            arr = np.asarray(arr)
            if arr.ndim == 1 and arr.shape[0] == len(cands):
                scores = arr; break
            if arr.ndim == 2 and arr.shape[0] == len(cands):
                scores = arr[:, 0]; break
        if scores is None:
            last = np.asarray(outputs[-1])
            try:
                scores = last.reshape(len(cands), -1)[:, 0]
            except Exception as e:
                raise RuntimeError("unable to parse reranker outputs: " + str(e))
        return {"scores": [float(s) for s in np.asarray(scores).astype(float)], "max_length_used": int(eff_max)}

# --- Deployment: LLM (lazy imports) ---
@serve.deployment(name=LLM_DEPLOYMENT, num_replicas=LLM_REPLICAS, ray_actor_options={"num_gpus": LLM_GPU_PER_REPLICA})
class LLMServe:
    def __init__(self, model_path: str = LLM_PATH, max_input: int = LLM_MAX_INPUT, max_new_tokens: int = LLM_MAX_NEW_TOKENS):
        try:
            import torch  # type: ignore
            from transformers import AutoTokenizer, AutoModelForCausalLM  # type: ignore
        except Exception as e:
            raise RuntimeError("transformers and torch must be installed to run LLMServe: " + str(e))

        if not os.path.exists(model_path):
            raise FileNotFoundError(f"LLM_PATH not found: {model_path}")

        self.model_path = model_path
        self.max_input = int(max_input)
        self.max_new_tokens = int(max_new_tokens)

        if LLM_DEVICE_OVERRIDE:
            device_str = LLM_DEVICE_OVERRIDE
        else:
            if LLM_USE_CUDA and torch.cuda.is_available() and LLM_GPU_PER_REPLICA > 0:
                device_str = "cuda"
            else:
                device_str = "cpu"
        self.device = torch.device(device_str)

        self.tokenizer = AutoTokenizer.from_pretrained(self.model_path, use_fast=True)
        if getattr(self.tokenizer, "pad_token", None) is None:
            if getattr(self.tokenizer, "eos_token", None) is not None:
                self.tokenizer.pad_token = self.tokenizer.eos_token
            else:
                self.tokenizer.add_special_tokens({"pad_token": "[PAD]"})

        dtype = torch.float16 if (self.device.type == "cuda") else torch.float32
        load_kwargs = {"torch_dtype": dtype}
        if LLM_LOW_CPU_MEM_USAGE:
            load_kwargs["low_cpu_mem_usage"] = True
        try:
            self.model = AutoModelForCausalLM.from_pretrained(self.model_path, trust_remote_code=LLM_TRUST_REMOTE_CODE, **load_kwargs)
        except Exception as e:
            log.exception("Failed to load LLM: %s", e)
            raise

        try:
            self.model.resize_token_embeddings(len(self.tokenizer))
        except Exception:
            pass

        try:
            self.model.to(self.device)
            self.model.eval()
        except Exception as e:
            log.warning("Could not move LLM to device %s: %s", self.device, e)

        log.info("[%s] loaded model=%s device=%s", LLM_DEPLOYMENT, self.model_path, self.device)

    async def __call__(self, payload):
        try:
            body = payload if isinstance(payload, (dict, list)) else {}
            if not isinstance(body, dict):
                try:
                    body = await payload.json()
                except Exception:
                    try:
                        raw = await payload.body()
                        body = json.loads(raw.decode("utf-8")) if raw else {}
                    except Exception:
                        body = {}
            prompt = body.get("prompt", "") if isinstance(body, dict) else (body if isinstance(body, str) else "")
            if not prompt:
                return {"error": "no prompt provided"}
            import torch  # local
            max_len = min(self.max_input, getattr(self.tokenizer, "model_max_length", self.max_input) or self.max_input)
            toks = self.tokenizer(prompt, truncation=True, padding=True, return_tensors="pt", max_length=max_len)
            input_ids = toks["input_ids"].to(self.device)
            attention_mask = toks.get("attention_mask")
            if attention_mask is not None:
                attention_mask = attention_mask.to(self.device)
            with torch.no_grad():
                out_ids = self.model.generate(
                    input_ids=input_ids,
                    attention_mask=attention_mask,
                    max_new_tokens=self.max_new_tokens,
                    do_sample=False,
                    pad_token_id=getattr(self.tokenizer, "pad_token_id", None) or getattr(self.tokenizer, "eos_token_id", None),
                )
            out_text = self.tokenizer.batch_decode(out_ids, skip_special_tokens=True)
            return {"generated": out_text, "model": self.model_path, "device": str(self.device)}
        except Exception as e:
            log.exception("LLM call error: %s", e)
            raise

# --- main: deploy three independent apps ---
def main():
    ray.init(address=RAY_ADDRESS, namespace=RAY_NAMESPACE, ignore_reinit_error=True)
    http_opts = HTTPOptions(host=SERVE_HTTP_HOST, port=SERVE_HTTP_PORT)
    serve.start(detached=True, http_options=http_opts)

    # Each bind() returns an Application. We pass one Application at a time to serve.run()
    embed_app = ONNXEmbed.bind()
    serve.run(embed_app, name=EMBED_DEPLOYMENT, route_prefix=f"/{EMBED_DEPLOYMENT}")
    log.info("Deployed %s", EMBED_DEPLOYMENT)

    if ENABLE_CROSS_ENCODER:
        rerank_app = ONNXRerank.bind()
        serve.run(rerank_app, name=RERANK_DEPLOYMENT, route_prefix=f"/{RERANK_DEPLOYMENT}")
        log.info("Deployed %s", RERANK_DEPLOYMENT)

    if ENABLE_LLM:
        llm_app = LLMServe.bind()
        serve.run(llm_app, name=LLM_DEPLOYMENT, route_prefix=f"/{LLM_DEPLOYMENT}")
        log.info("Deployed %s", LLM_DEPLOYMENT)

    log.info("All independent deployments started.")

# --- helpers to call handles from anywhere in cluster ---
def get_handle_and_call(name: str, payload: Dict[str, Any]):
    dep = serve.get_deployment(name)
    handle = dep.get_handle(sync=False)
    ref = handle.remote(payload)
    return ray.get(ref)

if __name__ == "__main__":
    main()
