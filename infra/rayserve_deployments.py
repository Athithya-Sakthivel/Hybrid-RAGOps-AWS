# infra/rayserve_deployments.py
from __future__ import annotations
import os
import json
import logging
import time
import asyncio
import math
from typing import Any, Dict, List, Optional

# --- ENV defaults (staging-friendly, override with exports in prod) ---
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
SERVE_HTTP_HOST = os.getenv("SERVE_HTTP_HOST", "127.0.0.1")
SERVE_HTTP_PORT = int(os.getenv("SERVE_HTTP_PORT", "8003"))

# RAG behavior toggles
REQUIRE_LLM_ON_DEPLOY = os.getenv("REQUIRE_LLM_ON_DEPLOY", "false").lower() in ("1", "true", "yes")
ENABLE_CROSS_ENCODER = os.getenv("ENABLE_CROSS_ENCODER", "true").lower() in ("1", "true", "yes")
PREFER_GRPC = os.getenv("PREFER_GRPC", "true").lower() in ("1", "true", "yes")

# Retrieval / LLM defaults
MAX_CHUNKS_TO_LLM = int(os.getenv("MAX_CHUNKS_TO_LLM", "8"))
CALL_TIMEOUT_SECONDS = float(os.getenv("CALL_TIMEOUT_SECONDS", "30"))
STREAM_FALLBACK_TO_BLOCKING = True  # always try streaming, then fallback

# Model paths (safe defaults). If you set REQUIRE_LLM_ON_DEPLOY=true and LLM_PATH missing, deploy will abort.
LLM_PATH = os.getenv("LLM_PATH", "/workspace/models/llm.gguf")

# Logging
logging.basicConfig(level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("rayserve_deployments_single")

# Try optional heavy imports and keep graceful fallbacks
try:
    import ray
    from ray import serve
    from ray.serve import HTTPOptions
except Exception as e:
    log.exception("ray/serve not importable; ensure ray is installed: %s", e)
    raise

# Keep your query.py unchanged; we will call its DB helpers when available.
try:
    import infra.query as querylib
except Exception:
    querylib = None
    log.info("infra.query not importable; retrieval will use local-only paths or return empty.")

# Try to import llama-cpp-python for LLM generation; fallback to simple template generator.
try:
    from llama_cpp import Llama
    _LLAMA_AVAILABLE = True
except Exception:
    _LLAMA_AVAILABLE = False

# -------------------------
# Minimal deterministic embedder (no external libs)
# -------------------------
def simple_text_embedding(text: str, dim: int = 128) -> List[float]:
    """
    Fast deterministic embedding. Not semantically deep.
    Used as safe fallback when dedicated embedder deployment is absent.
    """
    if not text:
        return [0.0] * dim
    # hash-based features: stable across runs
    h = [0.0] * dim
    for i, ch in enumerate(text):
        idx = (ord(ch) + i * 31) % dim
        h[idx] += (ord(ch) % 97) + 1
    # normalize
    norm = math.sqrt(sum(x * x for x in h)) or 1.0
    return [x / norm for x in h]

# -------------------------
# Small LLM wrapper (safe)
# -------------------------
class LocalLLM:
    def __init__(self, model_path: Optional[str] = None, n_threads: int = 2):
        self.model_path = model_path
        self.n_threads = n_threads
        self._use_llama = False
        self._llm = None
        if _LLAMA_AVAILABLE and model_path and os.path.exists(model_path):
            try:
                self._llm = Llama(model_path=model_path, n_threads=int(n_threads))
                self._use_llama = True
                log.info("LocalLLM: llama-cpp-python loaded model %s", model_path)
            except Exception:
                log.exception("LocalLLM: failed to init Llama; falling back to template generator")
                self._use_llama = False
                self._llm = None
        else:
            if model_path:
                log.info("LocalLLM: llama not available or model missing; using template fallback")

    def generate_blocking(self, prompt: str, params: Optional[Dict[str, Any]] = None) -> str:
        params = params or {}
        if self._use_llama and self._llm is not None:
            try:
                out = self._llm(prompt, **params)
                # llama-cpp-python returns object with 'text' often
                if isinstance(out, dict):
                    return str(out.get("text") or out.get("output") or json.dumps(out))
                try:
                    return str(getattr(out, "text", out))
                except Exception:
                    return str(out)
            except Exception:
                log.exception("LocalLLM llama call failed; falling back")
        # fallback deterministic response
        snippet = (prompt[:300] + "...") if len(prompt) > 300 else prompt
        return f"(fallback-llm) Answer based on provided context + prompt preview: {snippet}"

    async def generate_stream(self, prompt: str, params: Optional[Dict[str, Any]] = None):
        """
        Async generator yielding strings (tokens / fragments).
        Llama streaming not assumed. This yields token-like chunks for SSE.
        """
        params = params or {}
        if self._use_llama and self._llm is not None:
            # no streaming API guaranteed; call blocking then chunk
            text = self.generate_blocking(prompt, params)
            for i in range(0, len(text), 80):
                yield text[i:i+80]
                await asyncio.sleep(0.01)
            return
        # fallback: chunk the fallback response
        text = self.generate_blocking(prompt, params)
        for i in range(0, len(text), 80):
            await asyncio.sleep(0.01)
            yield text[i:i+80]

# -------------------------
# Single Serve deployment with FastAPI ingress
# -------------------------
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse

app = FastAPI(title="RAG Gateway (single-deployment)")

# Attach runtime state
class GatewayState:
    def __init__(self):
        # handles for external deployments (if you later add them)
        self.embed_handle = None
        self.rerank_handle = None
        self.llm_handle = None
        # local fallbacks
        self.local_llm = LocalLLM(model_path=LLM_PATH if os.path.exists(LLM_PATH) else None)
        # indicate whether we require LLM presence
        self.require_llm = REQUIRE_LLM_ON_DEPLOY

state = GatewayState()
app.state.gateway = state

# UI
_UI_HTML = """
<html>
  <body>
    <h3>Hybrid RAG (single-deployment) — Test UI</h3>
    <form method="post" action="/retrieve">
      <textarea name="query" rows="4" cols="60">What is MLOps?</textarea><br/>
      <label><input type="checkbox" name="stream" checked/> stream</label><br/>
      <button type="submit">Send</button>
    </form>
  </body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
async def root_ui():
    return HTMLResponse(_UI_HTML)

@app.get("/healthz")
async def healthz():
    # report simple readiness: LLM required check
    ok = True
    msg = "ok"
    if state.require_llm:
        if (not _LLAMA_AVAILABLE) and (state.local_llm and state.local_llm._use_llama is False):
            ok = False
            msg = "llm-missing"
    return {"status": "ok" if ok else "fail", "note": msg}

# Helper: build context using querylib if available, else basic stub
def build_context_and_prompt(query_text: str, top_k: int = 5) -> Dict[str, Any]:
    """
    Returns dict with keys: prompt (JSON string for LLM), provenance (list), records (list)
    This tries to use infra.query.retrieve_pipeline if available and if embed/rerank handles are present.
    Otherwise it builds a minimal prompt using no external chunks.
    """
    if querylib is not None:
        # attempt to use querylib.make_clients and querylib.retrieve_pipeline
        try:
            qdrant_client, neo4j_driver = querylib.make_clients()
            # prefer direct embed handle if present in state; else pass None to fallback BM25-only
            embed_handle = state.embed_handle
            rerank_handle = state.rerank_handle
            res = querylib.retrieve_pipeline(embed_handle, rerank_handle, qdrant_client, neo4j_driver, query_text, max_chunks=MAX_CHUNKS_TO_LLM)
            return res
        except Exception as e:
            log.exception("querylib.retrieve_pipeline failed; falling back to simple prompt: %s", e)
    # fallback minimal
    prompt = json.dumps({
        "QUERY": query_text,
        "CONTEXT_CHUNKS": [],
        "YOUR_ROLE": "You are a helpful assistant. Answer using only the provided context if present."
    }, ensure_ascii=False)
    return {"prompt": prompt, "provenance": [], "records": []}

# SSE encoder
def sse_event(data_obj: dict) -> str:
    return "data: " + json.dumps(data_obj, ensure_ascii=False) + "\n\n"

@app.post("/retrieve")
async def retrieve(req: Request):
    # accept JSON or form
    try:
        body = await req.json()
    except Exception:
        form = await req.form()
        body = dict(form)
    query_text = body.get("query") or body.get("input") or ""
    stream_flag = body.get("stream", True)
    # normalize stream flag
    if isinstance(stream_flag, str):
        stream_flag = stream_flag.lower() in ("1", "true", "yes", "on")
    elif isinstance(stream_flag, (int, float)):
        stream_flag = bool(stream_flag)
    # Build context (possibly heavy)
    ctx = build_context_and_prompt(query_text)
    prompt_json = ctx.get("prompt", json.dumps({"QUERY": query_text, "CONTEXT_CHUNKS": [], "YOUR_ROLE": "assistant"}))
    provenance = ctx.get("provenance", [])
    records = ctx.get("records", [])

    # choose LLM source: prefer remote handle if present, else local fallback
    llm_handle = state.llm_handle  # if later wired to a handle
    use_local_llm = True
    if llm_handle is not None:
        use_local_llm = False

    # Streaming path
    if stream_flag:
        async def stream_gen():
            # attempt streaming via handle if available and has streaming API
            try:
                if not use_local_llm:
                    # attempt to call querylib.call_llm_stream if present
                    if querylib is not None:
                        async for piece in querylib.call_llm_stream(llm_handle, prompt_json, params={}):
                            yield sse_event({"event": "token", "data": piece})
                        yield sse_event({"event": "done", "provenance": provenance, "records": records})
                        return
                    # fallback: try calling handle.remote(...) pattern via ray
                    try:
                        # best-effort: call llm_handle remotely if it supports streaming
                        maybe = llm_handle.remote({"prompt": prompt_json, "params": {}, "stream": True})
                        # ray.get of generator may succeed
                        try:
                            it = maybe
                            # if it's an iterator/list
                            for piece in it:
                                yield sse_event({"event": "token", "data": str(piece)})
                            yield sse_event({"event": "done", "provenance": provenance, "records": records})
                            return
                        except Exception:
                            pass
                    except Exception:
                        pass
                # either no remote or remote streaming failed => use local LLM streaming
                async for chunk in state.local_llm.generate_stream(prompt_json, params={}):
                    yield sse_event({"event": "token", "data": chunk})
                yield sse_event({"event": "done", "provenance": provenance, "records": records})
                return
            except Exception as e:
                log.exception("streaming path failed: %s", e)
                # fallback to blocking below
                try:
                    text = state.local_llm.generate_blocking(prompt_json, params={})
                    yield sse_event({"event": "token", "data": text})
                    yield sse_event({"event": "done", "provenance": provenance, "records": records})
                except Exception as e2:
                    log.exception("fallback blocking failed: %s", e2)
                    yield sse_event({"event": "error", "error": str(e2)})
        return StreamingResponse(stream_gen(), media_type="text/event-stream")

    # Blocking path
    try:
        if not use_local_llm and querylib is not None:
            # prefer querylib.call_llm_blocking if available
            try:
                text = querylib.call_llm_blocking(llm_handle, prompt_json, params={}, timeout=CALL_TIMEOUT_SECONDS)
                return {"answer": text, "provenance": provenance, "records": records}
            except Exception:
                log.exception("remote blocking llm failed; falling back to local")
        # local LLM blocking
        text = state.local_llm.generate_blocking(prompt_json, params={})
        return {"answer": text, "provenance": provenance, "records": records}
    except Exception as e:
        log.exception("final blocking call failed: %s", e)
        return {"error": str(e), "provenance": provenance, "records": records}

# --- Serve deployment wrapper ---
@serve.deployment(name=os.getenv("GATEWAY_DEPLOYMENT_NAME", "gateway"), num_replicas=int(os.getenv("GATEWAY_REPLICAS", "1")), ray_actor_options={"num_gpus": 0.0, "num_cpus": float(os.getenv("GATEWAY_CPUS", "0.5"))})
@serve.ingress(app)
class GatewayServe:
    def __init__(self):
        # nothing to init; the FastAPI app state already has the gateway state
        log.info("GatewayServe initialized; app routes available")

# -------------------------
# Deploy helper
# -------------------------
def main():
    # Start Ray/Serve (connect to cluster if RAY_ADDRESS is set in env)
    ray_address = os.getenv("RAY_ADDRESS", None)
    ray.init(address=(ray_address if ray_address and ray_address != "auto" else None), ignore_reinit_error=True)
    http_opts = HTTPOptions(host=SERVE_HTTP_HOST, port=SERVE_HTTP_PORT)
    # start serve if not started (idempotent)
    try:
        serve.start(detached=True, http_options=http_opts)
    except Exception:
        # if already started, connecting is fine
        pass

    # check LLM requirement
    if REQUIRE_LLM_ON_DEPLOY:
        if not _LLAMA_AVAILABLE and not (state.local_llm and state.local_llm._use_llama):
            raise RuntimeError("REQUIRE_LLM_ON_DEPLOY=true but no LLM available (llama-cpp missing or model file not present)")

    # Deploy single gateway (this creates the ingress)
    app_binding = GatewayServe.bind()
    # serve.run expects an Application object produced by bind, so pass it directly
    try:
        serve.run(app_binding)
    except Exception as e:
        log.exception("serve.run(app_binding) failed: %s", e)
        raise

    log.info("GatewayServe deployed. HTTP listening on %s:%s", SERVE_HTTP_HOST, SERVE_HTTP_PORT)

if __name__ == "__main__":
    main()
