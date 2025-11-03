from __future__ import annotations
import os, sys, json, logging, signal
from typing import Dict, Any, Optional
import numpy as np
import ray
from ray import serve
from ray.serve import HTTPOptions
from ray.serve.llm import LLMConfig
from ray.serve.llm.deployment import LLMServer
from ray.serve.llm.ingress import OpenAiIngress, make_fastapi_ingress
from transformers import PreTrainedTokenizerFast
try:
    import onnxruntime as ort
except Exception:
    ort = None
_log_level_name = os.getenv("LOG_LEVEL", "INFO") or "INFO"
_log_level = getattr(logging, _log_level_name.upper(), logging.INFO)
logging.basicConfig(level=_log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("serve_prod_final")
RAY_ADDRESS = os.getenv("RAY_ADDRESS") or None
RAY_NAMESPACE = os.getenv("RAY_NAMESPACE") or None
SERVE_HTTP_HOST = os.getenv("SERVE_HTTP_HOST", "0.0.0.0")
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
EMBED_GPU_PER_REPLICA = float(os.getenv("EMBED_GPU_PER_REPLICA", "0"))
RERANK_GPU_PER_REPLICA = float(os.getenv("RERANK_GPU_PER_REPLICA", "0"))
MAX_RERANK = int(os.getenv("MAX_RERANK", "256"))
ORT_INTRA_THREADS = int(os.getenv("ORT_INTRA_THREADS", "1"))
ORT_INTER_THREADS = int(os.getenv("ORT_INTER_THREADS", "1"))
INDEXING_EMBEDDER_MAX_TOKENS = int(os.getenv("INDEXING_EMBEDDER_MAX_TOKENS", "512"))
CROSS_ENCODER_MAX_TOKENS = int(os.getenv("CROSS_ENCODER_MAX_TOKENS", "600"))
ENABLE_CROSS_ENCODER = (os.getenv("ENABLE_CROSS_ENCODER", "true").lower() in ("1", "true", "yes"))
EMBED_MAX_REPLICAS_PER_NODE = int(os.getenv("EMBED_MAX_REPLICAS_PER_NODE", "1"))
RERANKER_MAX_REPLICAS_PER_NODE = int(os.getenv("RERANKER_MAX_REPLICAS_PER_NODE", "1"))
LLM_MAX_REPLICAS_PER_NODE = int(os.getenv("LLM_MAX_REPLICAS_PER_NODE", "1"))
LLM_ENABLE_ENV = (os.getenv("LLM_ENABLE", "true").lower() in ("1", "true", "yes"))
LLM_PATH = os.getenv("LLM_PATH", "/workspace/models/qwen/Qwen3-0.6B")
LLM_MODEL_ID = os.getenv("LLM_MODEL_ID", "qwen-3-0.6b")
LLM_REPLICAS = int(os.getenv("LLM_REPLICAS", "1"))
LLM_GPU_PER_REPLICA = float(os.getenv("LLM_GPU_PER_REPLICA", "1.0"))
LLM_DEPLOYMENT_NAME = os.getenv("LLM_DEPLOYMENT_NAME", "llm_server")
LLM_INGRESS_NAME = os.getenv("LLM_INGRESS_NAME", "openai_ingress")
LLM_ENGINE_KWARGS = json.loads(os.getenv("LLM_ENGINE_KWARGS", "{}") or "{}")
LLM_USE_RUNTIME_ENV = (os.getenv("LLM_USE_RUNTIME_ENV", "false").lower() in ("1", "true", "yes"))
_LLM_APIS_AVAILABLE = True
try:
    from ray.serve.llm import LLMConfig as _chk  # noqa: F401
except Exception:
    _LLM_APIS_AVAILABLE = False
LLM_ENABLE = LLM_ENABLE_ENV and _LLM_APIS_AVAILABLE
def exists_and_readable(p: str) -> bool:
    try:
        return os.path.exists(p) and os.access(p, os.R_OK)
    except Exception:
        return False
def make_session(path: str):
    if ort is None:
        raise RuntimeError("onnxruntime not available")
    so = ort.SessionOptions()
    so.intra_op_num_threads = ORT_INTRA_THREADS
    so.inter_op_num_threads = ORT_INTER_THREADS
    providers = (["CUDAExecutionProvider", "CPUExecutionProvider"] if ONNX_USE_CUDA else ["CPUExecutionProvider"])
    sess = ort.InferenceSession(path, sess_options=so, providers=providers)
    return sess
def mean_pool(last_hidden: np.ndarray, attention_mask: np.ndarray) -> np.ndarray:
    mask = attention_mask.astype(np.float32)
    mask = mask[:, :, None]
    summed = (last_hidden * mask).sum(axis=1)
    denom = np.maximum(mask.sum(axis=1), 1e-9)
    return summed / denom
def effective_max_length(tokenizer: PreTrainedTokenizerFast, requested: Optional[int], env_default: int, hard_cap: Optional[int] = None) -> int:
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
@serve.deployment
class EmbedDeployment:
    def __init__(self, onnx_path: str = ONNX_EMBED_PATH, tokenizer_path: str = ONNX_EMBED_TOKENIZER_PATH):
        if ort is None:
            raise RuntimeError("onnxruntime not available")
        if not exists_and_readable(tokenizer_path):
            raise FileNotFoundError(tokenizer_path)
        if not exists_and_readable(onnx_path):
            raise FileNotFoundError(onnx_path)
        self.tokenizer = PreTrainedTokenizerFast(tokenizer_file=tokenizer_path)
        if getattr(self.tokenizer, "pad_token", None) is None:
            if getattr(self.tokenizer, "eos_token", None) is not None:
                self.tokenizer.pad_token = self.tokenizer.eos_token
            elif getattr(self.tokenizer, "sep_token", None) is not None:
                self.tokenizer.pad_token = self.tokenizer.sep_token
            else:
                self.tokenizer.add_special_tokens({"pad_token": "[PAD]"})
        self.sess = make_session(onnx_path)
        self.input_names = [inp.name for inp in self.sess.get_inputs()]
        probe_len = effective_max_length(self.tokenizer, None, INDEXING_EMBEDDER_MAX_TOKENS)
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
        log.info("Embed ready dim=%d", self._embed_dim)
    async def __call__(self, request):
        body = {}
        if isinstance(request, (dict, list)):
            body = request
        else:
            try:
                body = await request.json()
            except Exception:
                try:
                    raw = await request.body()
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
        eff_max = effective_max_length(self.tokenizer, requested_max, INDEXING_EMBEDDER_MAX_TOKENS)
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
        vecs = None
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
@serve.deployment
class RerankDeployment:
    def __init__(self, onnx_path: str = ONNX_RERANK_PATH, tokenizer_path: str = ONNX_RERANK_TOKENIZER_PATH):
        if ort is None:
            raise RuntimeError("onnxruntime not available")
        if not exists_and_readable(tokenizer_path):
            raise FileNotFoundError(tokenizer_path)
        if not exists_and_readable(onnx_path):
            raise FileNotFoundError(onnx_path)
        self.tokenizer = PreTrainedTokenizerFast(tokenizer_file=tokenizer_path)
        if getattr(self.tokenizer, "pad_token", None) is None:
            if getattr(self.tokenizer, "eos_token", None) is not None:
                self.tokenizer.pad_token = self.tokenizer.eos_token
            elif getattr(self.tokenizer, "sep_token", None) is not None:
                self.tokenizer.pad_token = self.tokenizer.sep_token
            else:
                self.tokenizer.add_special_tokens({"pad_token": "[PAD]"})
        self.sess = make_session(onnx_path)
        self.input_names = [inp.name for inp in self.sess.get_inputs()]
        probe_len = effective_max_length(self.tokenizer, None, CROSS_ENCODER_MAX_TOKENS)
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
        log.info("Rerank ready sample-shape=%s", derived.shape)
    async def __call__(self, request):
        body = {}
        if isinstance(request, (dict, list)):
            body = request
        else:
            try:
                body = await request.json()
            except Exception:
                try:
                    raw = await request.body()
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
        eff_max = effective_max_length(self.tokenizer, requested_max, CROSS_ENCODER_MAX_TOKENS)
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
from fastapi import FastAPI
app = FastAPI()
GLOBAL_STATE = {"embed_ready": False, "rerank_ready": False, "llm_ready": False, "started": False}
@app.get("/healthz")
def healthz():
    return {"status": "ok", "started": GLOBAL_STATE["started"]}
@app.get("/ready")
def ready():
    ready_ok = GLOBAL_STATE["embed_ready"]
    if ENABLE_CROSS_ENCODER:
        ready_ok = ready_ok and GLOBAL_STATE["rerank_ready"]
    if LLM_ENABLE:
        ready_ok = ready_ok and GLOBAL_STATE["llm_ready"]
    return {"ready": bool(ready_ok), "components": {k: bool(v) for k, v in GLOBAL_STATE.items()}}
def deploy_all():
    if LLM_ENABLE_ENV and not _LLM_APIS_AVAILABLE:
        log.error("LLM requested but Serve LLM APIs unavailable")
        raise SystemExit(1)
    if ENABLE_CROSS_ENCODER:
        if not (exists_and_readable(ONNX_RERANK_PATH) and exists_and_readable(ONNX_RERANK_TOKENIZER_PATH)):
            log.error("Reranker files missing and ENABLE_CROSS_ENCODER=true")
            raise SystemExit(1)
    if not (exists_and_readable(ONNX_EMBED_PATH) and exists_and_readable(ONNX_EMBED_TOKENIZER_PATH)):
        log.error("Embed files missing")
        raise SystemExit(1)
    binds = []
    embed_resources = {"GPU_NODE": 1} if EMBED_GPU_PER_REPLICA and EMBED_GPU_PER_REPLICA > 0 else {"CPU_NODE": 1}
    embed_actor_opts = {"num_gpus": float(EMBED_GPU_PER_REPLICA), "resources": embed_resources}
    embed_bind = EmbedDeployment.options(name=EMBED_DEPLOYMENT, num_replicas=EMBED_REPLICAS, max_replicas_per_node=EMBED_MAX_REPLICAS_PER_NODE, ray_actor_options=embed_actor_opts).bind(ONNX_EMBED_PATH, ONNX_EMBED_TOKENIZER_PATH)
    binds.append(embed_bind)
    if ENABLE_CROSS_ENCODER:
        rerank_resources = {"GPU_NODE": 1} if RERANK_GPU_PER_REPLICA and RERANK_GPU_PER_REPLICA > 0 else {"CPU_NODE": 1}
        rerank_actor_opts = {"num_gpus": float(RERANK_GPU_PER_REPLICA), "resources": rerank_resources}
        rerank_bind = RerankDeployment.options(name=RERANK_HANDLE_NAME, num_replicas=RERANK_REPLICAS, max_replicas_per_node=RERANKER_MAX_REPLICAS_PER_NODE, ray_actor_options=rerank_actor_opts).bind(ONNX_RERANK_PATH, ONNX_RERANK_TOKENIZER_PATH)
        binds.append(rerank_bind)
    if LLM_ENABLE:
        model_loading = {"model_id": LLM_MODEL_ID, "model_source": LLM_PATH}
        deployment_cfg = {"autoscaling_config": {"min_replicas": max(1, LLM_REPLICAS), "max_replicas": max(1, LLM_REPLICAS)}}
        llm_runtime_env = {"pip": ["vllm==0.11.0"]} if LLM_USE_RUNTIME_ENV else None
        llm_config = LLMConfig(model_loading_config=model_loading, deployment_config=deployment_cfg, engine_kwargs=LLM_ENGINE_KWARGS or None, runtime_env=llm_runtime_env)
        server_options = LLMServer.get_deployment_options(llm_config) or {}
        ray_actor_opts = server_options.get("ray_actor_options", {})
        ray_actor_opts.setdefault("num_gpus", float(LLM_GPU_PER_REPLICA))
        # enforce placement label for GPU nodes
        ray_actor_opts.setdefault("resources", {})
        ray_actor_opts["resources"].setdefault("GPU_NODE", 1)
        server_options["ray_actor_options"] = ray_actor_opts
        server_options.setdefault("max_replicas_per_node", LLM_MAX_REPLICAS_PER_NODE)
        server_deployment = serve.deployment(LLMServer).options(**server_options).bind(llm_config)
        ingress_options = OpenAiIngress.get_deployment_options(llm_configs=[llm_config]) or {}
        ingress_cls = make_fastapi_ingress(OpenAiIngress)
        ingress_deployment = serve.deployment(ingress_cls).options(**ingress_options).bind([llm_config])
        binds.append(server_deployment)
        binds.append(ingress_deployment)
    health_bind = serve.deployment(name="health", num_replicas=1, route_prefix="/").bind(app)
    binds.append(health_bind)
    log.info("Starting serve.run with %d binds", len(binds))
    serve.run(*binds)
def main():
    ray_init_kwargs = {"ignore_reinit_error": True}
    if RAY_ADDRESS:
        ray_init_kwargs["address"] = RAY_ADDRESS
    if RAY_NAMESPACE:
        ray_init_kwargs["namespace"] = RAY_NAMESPACE
    ray.init(**ray_init_kwargs)
    http_opts = HTTPOptions(host=SERVE_HTTP_HOST, port=SERVE_HTTP_PORT)
    serve.start(http_options=http_opts, detached=False)
    def _handler(sig, frame):
        try:
            serve.shutdown()
        except Exception:
            pass
        try:
            ray.shutdown()
        except Exception:
            pass
        sys.exit(0)
    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)
    GLOBAL_STATE["started"] = True
    try:
        deploy_all()
    except Exception:
        log.exception("Deployment failed")
        try:
            serve.shutdown()
        except Exception:
            pass
        try:
            ray.shutdown()
        except Exception:
            pass
        raise
if __name__ == "__main__":
    main()
