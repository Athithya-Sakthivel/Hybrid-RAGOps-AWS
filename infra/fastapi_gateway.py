from __future__ import annotations
import os
import json
import logging
import asyncio
from typing import Any, Dict, Optional
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse, HTMLResponse
from pydantic import BaseModel
try:
    import ray
    from ray import serve
except Exception:
    ray = None
    serve = None
import infra.query as query
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("fastapi_gateway")
EMBED_DEPLOYMENT = os.getenv("EMBED_DEPLOYMENT", "embed_onnx_cpu")
RERANK_DEPLOYMENT = os.getenv("RERANK_HANDLE_NAME", "rerank_onnx_cpu")
LLM_DEPLOYMENT = os.getenv("LLM_DEPLOYMENT_NAME", "llm_server_cpu")
app = FastAPI(title="RAG8s Gateway", docs_url="/docs", redoc_url=None)
class RetrieveRequest(BaseModel):
    query: str
    stream: Optional[bool] = False
    max_chunks: Optional[int] = None
    llm_params: Optional[Dict[str, Any]] = None
    do_presign: Optional[bool] = False
class HandleResolver:
    def __init__(self):
        self._embed = None
        self._rerank = None
        self._llm = None
    def embed(self):
        if self._embed is None:
            self._embed = query.get_strict_handle(EMBED_DEPLOYMENT, timeout=20.0)
        return self._embed
    def rerank(self):
        if self._rerank is None:
            try:
                self._rerank = query.get_strict_handle(RERANK_DEPLOYMENT, timeout=20.0)
            except Exception:
                self._rerank = None
        return self._rerank
    def llm(self):
        if self._llm is None:
            try:
                self._llm = query.get_strict_handle(LLM_DEPLOYMENT, timeout=20.0)
            except Exception:
                self._llm = None
        return self._llm
_resolver = HandleResolver()
@app.get("/", response_class=HTMLResponse)
async def index():
    html = """<!doctype html><html><head><meta charset="utf-8"><title>RAG Gateway</title></head><body><h3>RAG Gateway</h3><div>POST /retrieve with JSON {"query":"...","stream":true}</div></body></html>"""
    return HTMLResponse(html)
@app.get("/healthz")
async def healthz():
    try:
        _ = _resolver.embed()
        return {"ok": True}
    except Exception as e:
        log.warning("health check failed to resolve embed: %s", e)
        return {"ok": False, "error": str(e)}
@app.post("/retrieve")
async def retrieve(req: Request):
    body = await req.json()
    try:
        rreq = RetrieveRequest(**body)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"invalid payload: {e}")
    q_client, neo_driver = query.make_clients()
    try:
        embed_handle = _resolver.embed()
    except Exception as e:
        log.exception("failed to resolve embed handle")
        raise HTTPException(status_code=500, detail=f"embed handle resolution failed: {e}")
    rerank_handle = None
    try:
        rerank_handle = _resolver.rerank()
    except Exception:
        rerank_handle = None
    try:
        retrieval = query.retrieve_pipeline(embed_handle, rerank_handle, q_client, neo_driver, rreq.query, max_chunks=(rreq.max_chunks or query.MAX_CHUNKS_TO_LLM))
    except Exception as e:
        log.exception("retrieval pipeline failed")
        raise HTTPException(status_code=500, detail=f"retrieval failed: {e}")
    llm_handle = None
    try:
        llm_handle = _resolver.llm()
    except Exception:
        llm_handle = None
    prompt_json = retrieval.get("prompt", "")
    provenance = retrieval.get("provenance", [])
    records = retrieval.get("records", [])
    if rreq.stream:
        if llm_handle is None:
            try:
                text = query.call_llm_blocking(llm_handle, prompt_json, params=rreq.llm_params) if llm_handle else "(no llm available)"
            except Exception as e:
                text = f"(llm error: {e})"
            return JSONResponse({"answer": text, "provenance": provenance, "records": records})
        async def event_generator():
            try:
                async for token in query.call_llm_stream(llm_handle, prompt_json, params=rreq.llm_params):
                    if token is None:
                        continue
                    ev = json.dumps({"event": "token", "data": token}, ensure_ascii=False)
                    yield f"data: {ev}\n\n"
                final_ev = json.dumps({"event": "done", "provenance": provenance, "records": records}, ensure_ascii=False)
                yield f"data: {final_ev}\n\n"
            except Exception as e:
                log.exception("error while streaming llm")
                err_ev = json.dumps({"event": "error", "data": str(e)}, ensure_ascii=False)
                yield f"data: {err_ev}\n\n"
        return StreamingResponse(event_generator(), media_type="text/event-stream")
    else:
        if llm_handle is None:
            return JSONResponse({"answer": None, "provenance": provenance, "records": records})
        try:
            text = query.call_llm_blocking(llm_handle, prompt_json, params=rreq.llm_params, timeout=120.0)
            return JSONResponse({"answer": text, "provenance": provenance, "records": records})
        except Exception as e:
            log.exception("llm call failed")
            raise HTTPException(status_code=500, detail=f"llm call failed: {e}")
try:
    if serve is not None:
        @serve.deployment(name=os.getenv("GATEWAY_DEPLOYMENT_NAME", "gateway"), num_replicas=int(os.getenv("GATEWAY_REPLICAS", "1")), ray_actor_options={"num_gpus": 0.0, "num_cpus": float(os.getenv("GATEWAY_CPUS", "0.5"))})
        class GatewayIngress:
            def __init__(self):
                self.app = app
            async def __call__(self, request):
                return await self.app(request.scope, request.receive, request.send)
except Exception:
    pass
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("infra.fastapi_gateway:app", host="0.0.0.0", port=int(os.getenv("GATEWAY_PORT", "8000")), log_level="info")
