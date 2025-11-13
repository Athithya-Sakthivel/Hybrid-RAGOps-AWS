from __future__ import annotations
import os
import json
import logging
import asyncio
import traceback
from typing import Any, Dict, Optional
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
RAY_ADDRESS = os.getenv("RAY_ADDRESS", None)
RAY_NAMESPACE = os.getenv("RAY_NAMESPACE", None)
SERVE_HTTP_HOST = os.getenv("SERVE_HTTP_HOST", "127.0.0.1")
SERVE_HTTP_PORT = int(os.getenv("SERVE_HTTP_PORT", "8003"))
EMBED_DEPLOYMENT = os.getenv("EMBED_DEPLOYMENT", "embed_onnx_cpu")
RERANK_DEPLOYMENT = os.getenv("RERANK_HANDLE_NAME", "rerank_onnx_cpu")
LLM_DEPLOYMENT = os.getenv("LLM_DEPLOYMENT_NAME", "llm_server_cpu")
ONNX_EMBED_PATH = os.getenv("ONNX_EMBED_PATH", "/workspace/models/gte-modernbert-base/onnx/model_int8.onnx")
ONNX_EMBED_TOKENIZER = os.getenv("ONNX_EMBED_TOKENIZER", "/workspace/models/gte-modernbert-base/tokenizer.json")
ONNX_RERANK_PATH = os.getenv("ONNX_RERANK_PATH", "/workspace/models/ms-marco-TinyBERT-L2-v2/onnx/model_quint8_avx2.onnx")
ONNX_RERANK_TOKENIZER = os.getenv("ONNX_RERANK_TOKENIZER", "/workspace/models/ms-marco-TinyBERT-L2-v2/tokenizer.json")
LLM_MODEL_PATH = os.getenv("LLM_MODEL_PATH", "/workspace/models/Qwen3-0.6B-GGUF/Qwen3-0.6B-Q4_K_M.gguf")
EMBED_REPLICAS = int(os.getenv("EMBED_REPLICAS", "1"))
RERANK_REPLICAS = int(os.getenv("RERANK_REPLICAS", "1"))
LLM_REPLICAS = int(os.getenv("LLM_REPLICAS", "1"))
GATEWAY_REPLICAS = int(os.getenv("GATEWAY_REPLICAS", "1"))
EMBED_CPUS = float(os.getenv("EMBED_NUM_CPUS_PER_REPLICA", "1.0"))
RERANK_CPUS = float(os.getenv("RERANK_NUM_CPUS_PER_REPLICA", "1.0"))
LLM_CPUS = float(os.getenv("LLM_NUM_CPUS_PER_REPLICA", "4.0"))
GATEWAY_CPUS = float(os.getenv("GATEWAY_CPUS", "0.5"))
EMBED_GPUS = float(os.getenv("EMBED_NUM_GPUS_PER_REPLICA", "0.0"))
RERANK_GPUS = float(os.getenv("RERANK_NUM_GPUS_PER_REPLICA", "0.0"))
LLM_GPUS = float(os.getenv("LLM_NUM_GPUS_PER_REPLICA", "0.0"))
GATEWAY_HEAD_RESOURCE = float(os.getenv("GATEWAY_HEAD_RESOURCE", "0.01"))
ENABLE_CROSS_ENCODER = os.getenv("ENABLE_CROSS_ENCODER", "true").lower() in ("1", "true", "yes")
REQUIRE_LLM_ON_DEPLOY = os.getenv("REQUIRE_LLM_ON_DEPLOY", "true").lower() in ("1", "true", "yes")
REQUIRE_EMBED_ON_DEPLOY = os.getenv("REQUIRE_EMBED_ON_DEPLOY", "true").lower() in ("1", "true", "yes")
REQUIRE_RERANK_ON_DEPLOY = os.getenv("REQUIRE_RERANK_ON_DEPLOY", "false").lower() in ("1", "true", "yes")
REQUIRE_INDEX_BACKENDS = os.getenv("REQUIRE_INDEX_BACKENDS", "false").lower() in ("1", "true", "yes")
EMBED_TIMEOUT = float(os.getenv("EMBED_TIMEOUT", "10.0"))
CALL_TIMEOUT_SECONDS = float(os.getenv("CALL_TIMEOUT_SECONDS", "15.0"))
HANDLE_RESOLVE_TIMEOUT = float(os.getenv("HANDLE_RESOLVE_TIMEOUT", "30.0"))
MAX_STREAM_SECONDS = int(os.getenv("MAX_STREAM_SECONDS", "240"))
INFERENCE_EMBEDDER_MAX_TOKENS = int(os.getenv("INFERENCE_EMBEDDER_MAX_TOKENS", "64"))
CROSS_ENCODER_MAX_TOKENS = int(os.getenv("CROSS_ENCODER_MAX_TOKENS", "600"))
MAX_CHUNKS_TO_LLM = int(os.getenv("MAX_CHUNKS_TO_LLM", "8"))
MODE = os.getenv("MODE", "hybrid").lower()
logging.basicConfig(level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("rayserve_deployments_final")
import ray
from ray import serve
from ray.serve import HTTPOptions
from starlette.responses import StreamingResponse, JSONResponse, PlainTextResponse, Response
try:
    from infra import query as querylib
except Exception:
    log.exception("Failed to import infra.query. Ensure infra/query.py exists and is importable.")
    raise
def _ensure_file(path: str):
    if not os.path.exists(path):
        raise FileNotFoundError(path)
@serve.deployment(name=EMBED_DEPLOYMENT, num_replicas=EMBED_REPLICAS,ray_actor_options={"num_cpus": EMBED_CPUS, "num_gpus": EMBED_GPUS})
class ONNXEmbed:
    def __init__(self, onnx_path: str = ONNX_EMBED_PATH, tokenizer_path: str = ONNX_EMBED_TOKENIZER):
        from transformers import PreTrainedTokenizerFast
        import onnxruntime as ort
        import numpy as np
        _ensure_file(tokenizer_path)
        _ensure_file(onnx_path)
        self.np = np
        self.ort = ort
        self.tokenizer = PreTrainedTokenizerFast(tokenizer_file=tokenizer_path)
        if getattr(self.tokenizer, "pad_token", None) is None:
            if getattr(self.tokenizer, "eos_token", None) is not None:
                self.tokenizer.pad_token = self.tokenizer.eos_token
            elif getattr(self.tokenizer, "sep_token", None) is not None:
                self.tokenizer.pad_token = self.tokenizer.sep_token
            else:
                self.tokenizer.add_special_tokens({"pad_token": "[PAD]"})
        so = self.ort.SessionOptions()
        try:
            so.intra_op_num_threads = max(1, int(os.getenv("ORT_INTRA_THREADS", "2")))
            so.inter_op_num_threads = max(1, int(os.getenv("ORT_INTER_THREADS", "1")))
        except Exception:
            pass
        providers = ["CPUExecutionProvider"]
        self.sess = self.ort.InferenceSession(onnx_path, sess_options=so, providers=providers)
        self.input_names = [inp.name for inp in self.sess.get_inputs()]
        log.info("[%s] initialized", EMBED_DEPLOYMENT)
    async def __call__(self, request_or_payload):
        if isinstance(request_or_payload, (dict, list)):
            body = request_or_payload
        else:
            try:
                body = await request_or_payload.json()
            except Exception:
                raw = await request_or_payload.body()
                body = json.loads(raw.decode("utf-8")) if raw else {}
        texts = body.get("texts", []) if isinstance(body, dict) else []
        if not isinstance(texts, list):
            texts = [texts]
        requested_max = body.get("max_length", None) if isinstance(body, dict) else None
        max_length = int(requested_max) if requested_max else INFERENCE_EMBEDDER_MAX_TOKENS
        loop = asyncio.get_running_loop()
        def tokenize():
            return self.tokenizer(texts, padding=True, truncation=True, return_tensors="np", max_length=max_length)
        toks = await loop.run_in_executor(None, tokenize)
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
        outputs = await loop.run_in_executor(None, lambda: self.sess.run(None, ort_inputs))
        vecs = None
        for arr in outputs:
            arr = self.np.asarray(arr)
            if arr.ndim == 2 and arr.shape[0] == toks["input_ids"].shape[0]:
                vecs = arr
                break
            if arr.ndim == 3 and arr.shape[0] == toks["input_ids"].shape[0]:
                attn = toks.get("attention_mask", self.np.ones(arr.shape[:2], dtype="int64"))
                mask = attn.astype(self.np.float32)[:,:,None]
                summed = (arr * mask).sum(axis=1)
                denom = self.np.maximum(mask.sum(axis=1), 1e-9)
                vecs = summed / denom
                break
        if vecs is None:
            vecs = self.np.asarray(outputs[-1])
            if vecs.ndim > 2:
                vecs = vecs.reshape((vecs.shape[0], -1))
        norms = self.np.linalg.norm(vecs, axis=1, keepdims=True)
        norms = self.np.maximum(norms, 1e-12)
        vecs = (vecs / norms).astype(float)
        return {"vectors": [v.tolist() for v in vecs], "max_length_used": int(max_length)}
@serve.deployment(name=RERANK_DEPLOYMENT, num_replicas=RERANK_REPLICAS,ray_actor_options={"num_cpus": RERANK_CPUS, "num_gpus": RERANK_GPUS})
class ONNXRerank:
    def __init__(self, onnx_path: str = ONNX_RERANK_PATH, tokenizer_path: str = ONNX_RERANK_TOKENIZER):
        from transformers import PreTrainedTokenizerFast
        import onnxruntime as ort
        import numpy as np
        _ensure_file(tokenizer_path)
        _ensure_file(onnx_path)
        self.tokenizer = PreTrainedTokenizerFast(tokenizer_file=tokenizer_path)
        if getattr(self.tokenizer, "pad_token", None) is None:
            if getattr(self.tokenizer, "eos_token", None) is not None:
                self.tokenizer.pad_token = self.tokenizer.eos_token
            elif getattr(self.tokenizer, "sep_token", None) is not None:
                self.tokenizer.pad_token = self.tokenizer.sep_token
            else:
                self.tokenizer.add_special_tokens({"pad_token": "[PAD]"})
        so = ort.SessionOptions()
        try:
            so.intra_op_num_threads = max(1, int(os.getenv("ORT_INTRA_THREADS", "2")))
            so.inter_op_num_threads = max(1, int(os.getenv("ORT_INTER_THREADS", "1")))
        except Exception:
            pass
        providers = ["CPUExecutionProvider"]
        self.sess = ort.InferenceSession(onnx_path, sess_options=so, providers=providers)
        self.input_names = [inp.name for inp in self.sess.get_inputs()]
        import numpy as np
        self.np = np
        log.info("[%s] initialized", RERANK_DEPLOYMENT)
    async def __call__(self, request_or_payload):
        if isinstance(request_or_payload, (dict, list)):
            body = request_or_payload
        else:
            try:
                body = await request_or_payload.json()
            except Exception:
                raw = await request_or_payload.body()
                body = json.loads(raw.decode("utf-8")) if raw else {}
        q = body.get("query", "") if isinstance(body, dict) else ""
        cands = body.get("cands", []) if isinstance(body, dict) else []
        if not isinstance(cands, list):
            cands = [cands]
        MAX_RERANK = int(os.getenv("MAX_RERANK", "256"))
        cands = cands[:MAX_RERANK]
        if len(cands) == 0:
            return {"scores": []}
        requested_max = body.get("max_length", None) if isinstance(body, dict) else None
        eff_max = int(requested_max) if requested_max else CROSS_ENCODER_MAX_TOKENS
        loop = asyncio.get_running_loop()
        def tokenize():
            return self.tokenizer([(q, t) for t in cands], padding=True, truncation='only_second', return_tensors="np", max_length=eff_max)
        toks = await loop.run_in_executor(None, tokenize)
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
        outputs = await loop.run_in_executor(None, lambda: self.sess.run(None, ort_inputs))
        scores = None
        for arr in outputs:
            arr = self.np.asarray(arr)
            if arr.ndim == 1 and arr.shape[0] == len(cands):
                scores = arr
                break
            if arr.ndim == 2 and arr.shape[0] == len(cands):
                scores = arr[:, 0]
                break
        if scores is None:
            last = self.np.asarray(outputs[-1])
            try:
                scores = last.reshape(len(cands), -1)[:, 0]
            except Exception as e:
                raise RuntimeError("unable to parse reranker outputs: " + str(e))
        return {"scores": [float(s) for s in self.np.asarray(scores).astype(float)], "max_length_used": int(eff_max)}
@serve.deployment(name=LLM_DEPLOYMENT, num_replicas=LLM_REPLICAS,ray_actor_options={"num_cpus": LLM_CPUS, "num_gpus": LLM_GPUS})
class LlamaServe:
    def __init__(self, model_path: str = LLM_MODEL_PATH):
        try:
            from llama_cpp import Llama
        except Exception as e:
            log.exception("llama-cpp-python import failed: %s", e)
            raise RuntimeError("llama-cpp-python required for LLM deployment") from e
        _ensure_file(model_path)
        try:
            n_ctx = int(os.getenv("LLM_N_CTX", "2048"))
            n_threads = max(1, int(os.getenv("LLM_N_THREADS", str(max(1, int(LLM_CPUS))))))
            llm_kwargs = {"n_ctx": n_ctx, "n_threads": n_threads}
            self.llm = Llama(model_path=model_path, **llm_kwargs)
        except TypeError:
            self.llm = Llama(model_path=model_path)
        self._lock = asyncio.Lock()
        log.info("[%s] LLM loaded %s", LLM_DEPLOYMENT, model_path)
    async def generate(self, prompt: str, params: Optional[Dict[str, Any]] = None) -> Any:
        params = params or {}
        async with self._lock:
            loop = asyncio.get_running_loop()
            def run():
                try:
                    if hasattr(self.llm, "create_completion"):
                        return self.llm.create_completion(prompt, **(params or {}))
                    if hasattr(self.llm, "create_chat_completion"):
                        return self.llm.create_chat_completion(messages=[{"role":"user","content":prompt}], **(params or {}))
                    return self.llm(prompt, **(params or {}))
                except Exception as e:
                    raise
            return await loop.run_in_executor(None, run)
    async def stream(self, prompt: str, params: Optional[Dict[str, Any]] = None):
        params = params or {}
        async with self._lock:
            loop = asyncio.get_running_loop()
            def run_stream():
                out_iter = []
                try:
                    if hasattr(self.llm, "create_completion"):
                        gen = self.llm.create_completion(prompt, stream=True, **(params or {}))
                        for item in gen:
                            out_iter.append(item)
                        return out_iter
                    if hasattr(self.llm, "create_chat_completion"):
                        gen = self.llm.create_chat_completion(messages=[{"role":"user","content":prompt}], stream=True, **(params or {}))
                        for item in gen:
                            out_iter.append(item)
                        return out_iter
                    res = self.llm.create_completion(prompt, **(params or {})) if hasattr(self.llm, "create_completion") else self.llm(prompt, **(params or {}))
                    out_iter.append(res)
                    return out_iter
                except Exception:
                    return out_iter
            pieces = await loop.run_in_executor(None, run_stream)
            for p in pieces:
                try:
                    if isinstance(p, dict):
                        if "choices" in p:
                            for ch in p.get("choices", []):
                                if isinstance(ch, dict):
                                    txt = ch.get("text") or ch.get("delta") or ch.get("content") or ""
                                    if txt is None:
                                        txt = json.dumps(ch, default=str)
                                    yield str(txt)
                                else:
                                    yield str(ch)
                        elif "delta" in p:
                            yield str(p.get("delta") or "")
                        elif "content" in p:
                            yield str(p.get("content") or "")
                        else:
                            yield json.dumps(p, default=str)
                    else:
                        yield str(p)
                except Exception:
                    try:
                        yield str(p)
                    except Exception:
                        yield ""
    async def __call__(self, request_or_payload):
        if isinstance(request_or_payload, (dict, list)):
            body = request_or_payload
        else:
            try:
                body = await request_or_payload.json()
            except Exception:
                raw = await request_or_payload.body()
                body = json.loads(raw.decode("utf-8")) if raw else {}
        prompt = body.get("prompt", "") or body.get("input", "") or ""
        params = body.get("params", {}) or {}
        res = await self.generate(prompt, params)
        try:
            if hasattr(res, "to_dict"):
                return res.to_dict()
        except Exception:
            pass
        return res
@serve.deployment(name="gateway", num_replicas=GATEWAY_REPLICAS,ray_actor_options={"num_cpus": GATEWAY_CPUS, "resources": {"head": GATEWAY_HEAD_RESOURCE}, "num_gpus": 0.0})
class Gateway:
    def __init__(self, embed_handle: Optional[Any] = None, rerank_handle: Optional[Any] = None, llm_handle: Optional[Any] = None):
        self.embed_handle = embed_handle
        self.rerank_handle = rerank_handle
        self.llm_handle = llm_handle
        self.qdrant_client = None
        self.neo4j_driver = None
        self.streaming_default = True
        try:
            log.info("Gateway initializing; resolving model handles if not passed by bind()...")
            try:
                ray.init(address=RAY_ADDRESS if RAY_ADDRESS else None, namespace=RAY_NAMESPACE, ignore_reinit_error=True)
            except Exception:
                pass
            if self.embed_handle is None:
                if REQUIRE_EMBED_ON_DEPLOY:
                    self.embed_handle = querylib.get_strict_handle(EMBED_DEPLOYMENT, timeout=HANDLE_RESOLVE_TIMEOUT)
                else:
                    try:
                        self.embed_handle = querylib.get_strict_handle(EMBED_DEPLOYMENT, timeout=5.0)
                    except Exception:
                        log.warning("embed handle unresolved (optional)")
                        self.embed_handle = None
            if self.rerank_handle is None:
                if ENABLE_CROSS_ENCODER and REQUIRE_RERANK_ON_DEPLOY:
                    self.rerank_handle = querylib.get_strict_handle(RERANK_DEPLOYMENT, timeout=HANDLE_RESOLVE_TIMEOUT)
                else:
                    try:
                        self.rerank_handle = querylib.get_strict_handle(RERANK_DEPLOYMENT, timeout=5.0)
                    except Exception:
                        self.rerank_handle = None
            if self.llm_handle is None:
                if REQUIRE_LLM_ON_DEPLOY:
                    self.llm_handle = querylib.get_strict_handle(LLM_DEPLOYMENT, timeout=HANDLE_RESOLVE_TIMEOUT)
                else:
                    try:
                        self.llm_handle = querylib.get_strict_handle(LLM_DEPLOYMENT, timeout=5.0)
                    except Exception:
                        log.warning("llm handle unresolved (optional)")
                        self.llm_handle = None
            try:
                self.qdrant_client, self.neo4j_driver = querylib.make_clients()
                if REQUIRE_INDEX_BACKENDS and (self.qdrant_client is None and self.neo4j_driver is None):
                    raise RuntimeError("index backends required but unavailable")
            except Exception:
                log.exception("make_clients failed; continuing with None clients")
                self.qdrant_client, self.neo4j_driver = None, None
            log.info("Gateway initialized; routes available")
        except Exception:
            log.exception("Gateway initialization error")
            raise
    def _health(self) -> Dict[str, Any]:
        ok = {"status": "ok", "note": "ok"}
        ok["embed_handle"] = bool(self.embed_handle)
        ok["rerank_handle"] = bool(self.rerank_handle)
        ok["llm_handle"] = bool(self.llm_handle)
        ok["qdrant"] = bool(self.qdrant_client)
        ok["neo4j"] = bool(self.neo4j_driver)
        return ok
    async def _build_prompt_and_records(self, query_text: str, max_chunks: Optional[int] = None):
        try:
            res = querylib.retrieve_pipeline(self.embed_handle, self.rerank_handle, self.qdrant_client, self.neo4j_driver, query_text, max_chunks=max_chunks or MAX_CHUNKS_TO_LLM)
            return res
        except Exception:
            log.exception("retrieve_pipeline failed")
            prompt = json.dumps({"QUERY": query_text, "CONTEXT_CHUNKS": [], "YOUR_ROLE": "You are a helpful knowledge assistant who answers user queries with provenance using only the provided context chunks below."}, ensure_ascii=False)
            return {"prompt": prompt, "provenance": [], "records": [], "llm": None, "elapsed": 0.0}
    async def __call__(self, request):
        try:
            path = request.scope.get("path", "/")
            if request.method == "GET" and path == "/healthz":
                return {"status": "ok", "note": "ok", "embed": bool(self.embed_handle), "rerank": bool(self.rerank_handle), "llm": bool(self.llm_handle), "qdrant": bool(self.qdrant_client), "neo4j": bool(self.neo4j_driver)}
            if request.method == "GET" and path == "/":
                html = "<!doctype html><html><head><title>RAG Gateway</title></head><body><h3>RAG Gateway</h3><form id=\"qform\"><input id=\"q\" name=\"q\" placeholder=\"Ask a question\" size=60><label><input type=\"checkbox\" id=\"stream\" checked> stream</label><button type=\"submit\">Send</button></form><pre id=\"out\"></pre><script>const f=document.getElementById('qform'); const out=document.getElementById('out');f.onsubmit=function(e){ e.preventDefault(); out.textContent=''; const q=document.getElementById('q').value; const stream=document.getElementById('stream').checked; if(stream){ const es=new EventSource('/_sse?q='+encodeURIComponent(q)); es.onmessage=function(evt){ try{ const d=JSON.parse(evt.data); if(d.event==='token') out.textContent += d.data; if(d.event==='done'){ out.textContent += '\\n\\n[done] provenance=' + JSON.stringify(d.data.provenance || []); es.close(); } }catch(e){ out.textContent += evt.data; } }; es.onerror=function(){ es.close(); } } else { fetch('/retrieve', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({query:q, stream:false})}).then(r=>r.json()).then(j=> out.textContent = (j.answer||JSON.stringify(j))); } }; </script></body></html>"
                return Response(content=html, media_type="text/html", status_code=200)
            if request.method == "GET" and path in ("/_sse", "/stream"):
                q = request.query_params.get("q", "")
                if not q:
                    return PlainTextResponse("", status_code=400)
                pipe = await self._build_prompt_and_records(q)
                prompt_json = pipe.get("prompt", "")
                provenance = pipe.get("provenance", [])
                records = pipe.get("records", [])
                params = {}
                try:
                    async def sse_gen():
                        try:
                            async for chunk in querylib.call_llm_stream(self.llm_handle, prompt_json, params=params):
                                yield f"data: {json.dumps({'event':'token','data': str(chunk)}, ensure_ascii=False)}\n\n"
                            yield f"data: {json.dumps({'event':'done','data': {'provenance': provenance, 'records': records}}, ensure_ascii=False)}\n\n"
                        except Exception:
                            yield f"data: {json.dumps({'event':'done','data': {'provenance': provenance, 'records': records}}, ensure_ascii=False)}\n\n"
                    return StreamingResponse(sse_gen(), media_type="text/event-stream", headers={"Cache-Control":"no-cache"})
                except Exception:
                    try:
                        answer = querylib.call_llm_blocking(self.llm_handle, prompt_json, params=params, timeout=CALL_TIMEOUT_SECONDS)
                        final = {"answer": answer, "provenance": provenance, "records": records}
                        return JSONResponse(content=final, status_code=200)
                    except Exception:
                        return JSONResponse(content={"error":"LLM call failed"}, status_code=500)
            if request.method == "POST" and path == "/retrieve":
                try:
                    body = await request.json()
                except Exception:
                    raw = await request.body()
                    body = json.loads(raw.decode("utf-8")) if raw else {}
                query_text = body.get("query") or body.get("q") or ""
                stream = body.get("stream", self.streaming_default)
                pipeline = await self._build_prompt_and_records(query_text)
                prompt_json = pipeline.get("prompt", "")
                provenance = pipeline.get("provenance", [])
                records = pipeline.get("records", [])
                params = body.get("params", {}) or {}
                if stream:
                    try:
                        collected = []
                        async for piece in querylib.call_llm_stream(self.llm_handle, prompt_json, params=params):
                            collected.append(str(piece))
                        answer = "".join(collected)
                        return {"answer": answer, "provenance": provenance, "records": records}
                    except Exception:
                        log.exception("Streaming failed; fallback to blocking")
                        try:
                            answer = querylib.call_llm_blocking(self.llm_handle, prompt_json, params=params, timeout=CALL_TIMEOUT_SECONDS)
                            return {"answer": answer, "provenance": provenance, "records": records}
                        except Exception:
                            log.exception("Blocking LLM failed")
                            return {"answer": None, "provenance": provenance, "records": records}
                else:
                    try:
                        answer = querylib.call_llm_blocking(self.llm_handle, prompt_json, params=params, timeout=CALL_TIMEOUT_SECONDS)
                        return {"answer": answer, "provenance": provenance, "records": records}
                    except Exception:
                        log.exception("Blocking LLM failed")
                        return {"answer": None, "provenance": provenance, "records": records}
            return PlainTextResponse("Not Found", status_code=404)
        except Exception:
            log.exception("Gateway handle error for request path=%s", request.scope.get("path"))
            return JSONResponse(content={"error":"gateway error", "trace": traceback.format_exc()}, status_code=500)
def main():
    ray.init(address=RAY_ADDRESS if RAY_ADDRESS else None, namespace=RAY_NAMESPACE, ignore_reinit_error=True)
    http_opts = HTTPOptions(host=SERVE_HTTP_HOST, port=SERVE_HTTP_PORT)
    serve.start(detached=False, http_options=http_opts)
    embed_app = ONNXEmbed.options(name=EMBED_DEPLOYMENT, num_replicas=EMBED_REPLICAS,ray_actor_options={"num_cpus": EMBED_CPUS, "num_gpus": EMBED_GPUS}).bind(ONNX_EMBED_PATH, ONNX_EMBED_TOKENIZER)
    rerank_app = None
    if ENABLE_CROSS_ENCODER:
        rerank_app = ONNXRerank.options(name=RERANK_DEPLOYMENT, num_replicas=RERANK_REPLICAS,ray_actor_options={"num_cpus": RERANK_CPUS, "num_gpus": RERANK_GPUS}).bind(ONNX_RERANK_PATH, ONNX_RERANK_TOKENIZER)
    else:
        log.info("Cross encoder disabled; skipping rerank deployment")
    if REQUIRE_LLM_ON_DEPLOY:
        _ensure_file(LLM_MODEL_PATH)
    llm_app = LlamaServe.options(name=LLM_DEPLOYMENT, num_replicas=LLM_REPLICAS,ray_actor_options={"num_cpus": LLM_CPUS, "num_gpus": LLM_GPUS}).bind(LLM_MODEL_PATH)
    if rerank_app is not None:
        gateway_app = Gateway.options(name=os.getenv("GATEWAY_DEPLOYMENT_NAME", "gateway"),num_replicas=GATEWAY_REPLICAS,ray_actor_options={"num_cpus": GATEWAY_CPUS, "resources": {"head": GATEWAY_HEAD_RESOURCE}, "num_gpus": 0.0}).bind(embed_app, rerank_app, llm_app)
    else:
        gateway_app = Gateway.options(name=os.getenv("GATEWAY_DEPLOYMENT_NAME", "gateway"),num_replicas=GATEWAY_REPLICAS,ray_actor_options={"num_cpus": GATEWAY_CPUS, "resources": {"head": GATEWAY_HEAD_RESOURCE}, "num_gpus": 0.0}).bind(embed_app, None, llm_app)
    try:
        log.info("Starting Gateway application (serve.run)... HTTP at http://%s:%s", SERVE_HTTP_HOST, SERVE_HTTP_PORT)
        serve.run(gateway_app)
    except Exception:
        log.exception("serve.run failed for Gateway; rethrowing")
        raise
if __name__ == "__main__":
    main()
