from __future__ import annotations
import os
import json
import logging
import asyncio
from typing import Any, Dict, Optional
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse, HTMLResponse
from pydantic import BaseModel
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("fastapi_gateway")
GATEWAY_DEPLOYMENT_NAME = os.getenv("GATEWAY_DEPLOYMENT_NAME", "gateway")
GATEWAY_REPLICAS = int(os.getenv("GATEWAY_REPLICAS", "1"))
GATEWAY_CPUS = float(os.getenv("GATEWAY_CPUS", "0.5"))
DEFAULT_EMBED_NAME = os.getenv("EMBED_DEPLOYMENT", "embed_onnx_cpu")
DEFAULT_RERANK_NAME = os.getenv("RERANK_HANDLE_NAME", "rerank_onnx_cpu")
DEFAULT_LLM_NAME = os.getenv("LLM_DEPLOYMENT_NAME", "llm_server_cpu")
class RetrieveRequest(BaseModel):
    query: str
    stream: Optional[bool] = False
    max_chunks: Optional[int] = None
    llm_params: Optional[Dict[str, Any]] = None
    do_presign: Optional[bool] = False
def _safe_json(obj: Any) -> str:
    try:
        return json.dumps(obj, default=str, ensure_ascii=False)
    except Exception:
        return str(obj)
def create_app() -> FastAPI:
    app = FastAPI(title="RAG8s Gateway", docs_url="/docs", redoc_url=None)
    @app.get("/", response_class=HTMLResponse)
    async def index():
        html = "<!doctype html><html><head><meta charset='utf-8'><title>RAG Gateway</title></head><body><h3>RAG Gateway</h3><div>POST /retrieve with JSON {\"query\":\"...\",\"stream\":true}</div></body></html>"
        return HTMLResponse(html)
    @app.get("/healthz")
    async def healthz():
        try:
            import infra.query as query
            try:
                h = query.get_strict_handle(DEFAULT_EMBED_NAME, timeout=3.0)
                return JSONResponse({"ok": True})
            except Exception as e:
                log.warning("health embed handle not ready: %s", e)
                return JSONResponse({"ok": False, "error": str(e)}, status_code=503)
        except Exception as e:
            log.exception("health import query failed")
            return JSONResponse({"ok": False, "error": "query import failed: "+str(e)}, status_code=500)
    @app.post("/retrieve")
    async def retrieve(req: Request):
        try:
            body = await req.json()
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"invalid json body: {e}")
        try:
            rreq = RetrieveRequest(**body)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"invalid payload: {e}")
        import infra.query as query
        q_client, neo_driver = query.make_clients()
        try:
            embed_handle = None
            try:
                embed_handle = query.get_strict_handle(DEFAULT_EMBED_NAME, timeout=5.0)
            except Exception as ee:
                log.warning("embed handle resolution failed: %s", ee)
                embed_handle = None
        except Exception as e:
            log.exception("failed to resolve embed handle")
            raise HTTPException(status_code=500, detail=f"embed handle resolution failed: {e}")
        rerank_handle = None
        try:
            rerank_handle = query.get_strict_handle(DEFAULT_RERANK_NAME, timeout=3.0)
        except Exception:
            rerank_handle = None
        try:
            retrieval = query.retrieve_pipeline(embed_handle, rerank_handle, q_client, neo_driver, rreq.query, max_chunks=(rreq.max_chunks or query.MAX_CHUNKS_TO_LLM))
        except Exception as e:
            log.exception("retrieval pipeline failed")
            raise HTTPException(status_code=500, detail=f"retrieval failed: {e}")
        llm_handle = None
        try:
            llm_handle = query.get_strict_handle(DEFAULT_LLM_NAME, timeout=3.0)
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
    return app
if __name__ == "__main__":
    import uvicorn
    app = create_app()
    uvicorn.run(app, host=os.getenv("GATEWAY_HOST", "0.0.0.0"), port=int(os.getenv("GATEWAY_PORT", "8003")), log_level=os.getenv("LOG_LEVEL", "info"))
