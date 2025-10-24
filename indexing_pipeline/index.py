#!/usr/bin/env python3
from __future__ import annotations
import os
import sys
import json
import time
import uuid
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional
import ray
from ray import serve
RAY_ADDRESS = os.getenv("RAY_ADDRESS", "auto")
RAY_NAMESPACE = os.getenv("RAY_NAMESPACE", None)
DATA_IN_LOCAL = os.getenv("DATA_IN_LOCAL", "true").lower() in ("1", "true", "yes")
LOCAL_DIR_PATH = os.getenv("LOCAL_DIR_PATH", "data/chunked/")
EMBED_DEPLOYMENT = os.getenv("EMBED_DEPLOYMENT", "embed_onxx")
VECTOR_DIM = int(os.getenv("VECTOR_DIM", "768"))
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "64"))
EMBED_SUB_BATCH = int(os.getenv("EMBED_SUB_BATCH", "32"))
EMBED_TIMEOUT = int(os.getenv("EMBED_TIMEOUT", "60"))
QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY") or None
PREFER_GRPC = os.getenv("PREFER_GRPC", "true").lower() in ("1", "true", "yes")
QDRANT_COLLECTION = os.getenv("COLLECTION", "my_collection")
NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "neo4j")
RETRY_ATTEMPTS = int(os.getenv("RETRY_ATTEMPTS", "3"))
RETRY_BASE = float(os.getenv("RETRY_BASE_SECONDS", "0.5"))
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("ingest")
try:
    from qdrant_client import QdrantClient
    from qdrant_client.models import PointStruct, VectorParams, Distance
except Exception as e:
    raise RuntimeError("qdrant-client import failed: " + str(e))
try:
    from neo4j import GraphDatabase
except Exception as e:
    raise RuntimeError("neo4j driver import failed: " + str(e))
def retry(attempts: int = RETRY_ATTEMPTS, base: float = RETRY_BASE):
    def deco(fn):
        def wrapper(*a, **k):
            last = None
            for i in range(attempts):
                try:
                    return fn(*a, **k)
                except Exception as e:
                    last = e
                    wait = base * (2 ** i)
                    log.warning("retry %d/%d %s: %s (sleep %.2fs)", i + 1, attempts, fn.__name__, e, wait)
                    time.sleep(wait)
            log.error("all retries failed for %s", fn.__name__)
            raise last
        return wrapper
    return deco
def list_local_json_files(path: str) -> List[Path]:
    p = Path(path)
    if not p.exists() or not p.is_dir():
        raise RuntimeError(f"LOCAL_DIR_PATH does not exist: {path}")
    files = sorted([f for f in p.iterdir() if f.is_file() and f.suffix.lower() in (".json", ".jsonl")])
    return files
@retry()
def ensure_qdrant_collection(client: QdrantClient, collection: str, dim: int) -> None:
    existing = client.get_collections().collections
    names = [c.name for c in existing]
    if collection not in names:
        client.create_collection(collection_name=collection, vectors_config=VectorParams(size=dim, distance=Distance.COSINE))
        log.info("Created qdrant collection %s dim=%d", collection, dim)
    else:
        log.debug("Qdrant collection %s already exists", collection)
@retry()
def upsert_points(client: QdrantClient, collection: str, points: List[PointStruct], batch_size: int = 64):
    if not points:
        return
    for i in range(0, len(points), batch_size):
        client.upsert(collection_name=collection, points=points[i:i+batch_size])
        log.info("Upserted %d points", min(batch_size, len(points)-i))
@retry()
def ensure_document_and_chunks(driver, document_id: str, file_name: str, chunks_meta: List[Dict[str, Any]]) -> None:
    with driver.session() as session:
        tx = session.begin_transaction()
        tx.run("MERGE (d:Document {document_id:$doc_id}) SET d.file_name=$file_name, d.updated_at = datetime()", doc_id=document_id, file_name=file_name)
        for cm in chunks_meta:
            tx.run("MERGE (c:Chunk {chunk_id:$chunk_id}) SET c.token_count=$token_count, c.file_type=$file_type, c.source_url=$source_url, c.timestamp=$timestamp WITH c MATCH (d:Document {document_id:$doc_id}) MERGE (d)-[:HAS_CHUNK]->(c)", chunk_id=cm.get("chunk_id"), token_count=cm.get("token_count"), file_type=cm.get("file_type"), source_url=cm.get("source_url"), timestamp=cm.get("timestamp"), doc_id=document_id)
        tx.commit()
    log.debug("Neo4j metadata stored for %s (%d chunks)", document_id, len(chunks_meta))
def _resolve_handle_response(resp_obj, embed_timeout: int = EMBED_TIMEOUT):
    if isinstance(resp_obj, (dict, list)):
        return resp_obj
    try:
        import ray as _ray
        ObjectRefType = getattr(_ray, "ObjectRef", None) or getattr(_ray, "_raylet.ObjectRef", None)
    except Exception:
        ObjectRefType = None
    try:
        if ObjectRefType is not None and isinstance(resp_obj, ObjectRefType):
            return ray.get(resp_obj, timeout=embed_timeout)
    except Exception:
        pass
    try:
        if hasattr(resp_obj, "result") and callable(getattr(resp_obj, "result")):
            return resp_obj.result()
    except Exception:
        pass
    try:
        return ray.get(resp_obj, timeout=embed_timeout)
    except Exception as e:
        raise RuntimeError(f"Unable to resolve Serve handle response type: {type(resp_obj)} -> {e}")
def _ensure_ray_connected():
    if not ray.is_initialized():
        ray.init(address=RAY_ADDRESS, namespace=RAY_NAMESPACE, ignore_reinit_error=True)
def _get_embed_handle(name: str, timeout: float = 60.0, poll: float = 1.0, app_name: Optional[str] = "default"):
    start = time.time()
    last_exc = None
    while time.time() - start < timeout:
        try:
            _ensure_ray_connected()
            if hasattr(serve, "get_deployment_handle"):
                try:
                    handle = serve.get_deployment_handle(name, app_name=app_name, _check_exists=False)
                    resp_obj = handle.remote({"texts": ["health-check"]})
                    _resolve_handle_response(resp_obj, embed_timeout=EMBED_TIMEOUT)
                    return handle
                except Exception as e:
                    last_exc = e
            if hasattr(serve, "get_deployment"):
                try:
                    dep = serve.get_deployment(name)
                    handle = dep.get_handle(sync=False)
                    resp_obj = handle.remote({"texts": ["health-check"]})
                    _resolve_handle_response(resp_obj, embed_timeout=EMBED_TIMEOUT)
                    return handle
                except Exception as e:
                    last_exc = e
            if hasattr(serve, "get_handle"):
                try:
                    handle = serve.get_handle(name, sync=False)
                    resp_obj = handle.remote({"texts": ["health-check"]})
                    _resolve_handle_response(resp_obj, embed_timeout=EMBED_TIMEOUT)
                    return handle
                except Exception as e:
                    last_exc = e
            raise RuntimeError("serve handle APIs not available in this ray client")
        except Exception as e:
            last_exc = e
            log.debug("waiting for embed handle %s: %s", name, e)
            time.sleep(poll)
    try:
        import httpx
        r = httpx.post(f"http://127.0.0.1:8000/{name}", json={"texts": ["health-check"]}, timeout=10.0)
        if r.status_code == 200:
            log.warning("Using HTTP fallback for Serve deployment %s", name)
            class _HTTPHandle:
                def __init__(self, url):
                    self.url = url
                def remote(self, payload):
                    resp = httpx.post(self.url, json=payload, timeout=30.0)
                    resp.raise_for_status()
                    return resp.json()
            return _HTTPHandle(f"http://127.0.0.1:8000/{name}")
    except Exception:
        pass
    raise RuntimeError(f"Timed out waiting for Serve deployment {name}: {last_exc}")
def _normalize_chunk_obj(raw: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "document_id": raw.get("document_id"),
        "chunk_id": raw.get("chunk_id") or str(uuid.uuid4()),
        "text": raw.get("text", "") or "",
        "token_count": int(raw.get("token_count") or 0),
        "file_type": raw.get("file_type") or "",
        "file_name": raw.get("file_name") or "",
        "source_url": raw.get("source_url") or "",
        "timestamp": raw.get("timestamp") or "",
        "parser_version": raw.get("parser_version") or "",
    }
@retry()
def ingest_files(local_dir: str = LOCAL_DIR_PATH):
    _ensure_ray_connected()
    handle = _get_embed_handle(EMBED_DEPLOYMENT)
    qdrant = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY, prefer_grpc=PREFER_GRPC)
    ensure_qdrant_collection(qdrant, QDRANT_COLLECTION, VECTOR_DIM)
    neo4j_driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    files = list_local_json_files(local_dir) if DATA_IN_LOCAL else []
    if not files:
        log.warning("No chunk files under %s", local_dir)
        return 0
    to_upsert: List[PointStruct] = []
    neo4j_batch_map: Dict[str, List[Dict[str, Any]]] = {}
    for f in files:
        log.info("Processing %s", f)
        try:
            with open(f, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as e:
            log.exception("Failed to load %s: %s", f, e)
            continue
        items = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
        batch_texts = []
        batch_metas = []
        for raw in items:
            norm = _normalize_chunk_obj(raw)
            batch_texts.append(norm["text"])
            batch_metas.append(norm)
            if len(batch_texts) >= BATCH_SIZE:
                _embed_and_store_batch(handle, qdrant, neo4j_driver, batch_texts, batch_metas, to_upsert, neo4j_batch_map)
                batch_texts = []
                batch_metas = []
        if batch_texts:
            _embed_and_store_batch(handle, qdrant, neo4j_driver, batch_texts, batch_metas, to_upsert, neo4j_batch_map)
    if to_upsert:
        upsert_points(qdrant, QDRANT_COLLECTION, to_upsert, batch_size=BATCH_SIZE)
        to_upsert = []
    for docid, metas in neo4j_batch_map.items():
        fn = metas[0].get("file_name", "")
        ensure_document_and_chunks(neo4j_driver, docid, fn, metas)
    try:
        qdrant.close()
    except Exception:
        log.debug("qdrant.close failed", exc_info=True)
    try:
        neo4j_driver.close()
    except Exception:
        log.debug("neo4j close failed", exc_info=True)
    log.info("Ingest complete")
    return 0
def _embed_and_store_batch(handle, qdrant_client, neo4j_driver, texts, metas, to_upsert, neo4j_batch_map):
    start_batch = time.time()
    for i in range(0, len(texts), EMBED_SUB_BATCH):
        sub_texts = texts[i:i+EMBED_SUB_BATCH]
        sub_metas = metas[i:i+EMBED_SUB_BATCH]
        t0 = time.time()
        resp_obj = handle.remote({"texts": sub_texts})
        resp = _resolve_handle_response(resp_obj, embed_timeout=EMBED_TIMEOUT)
        t1 = time.time()
        log.info("Embed sub-batch size=%d elapsed=%.3fs", len(sub_texts), t1 - t0)
        if not isinstance(resp, dict) or "vectors" not in resp:
            raise RuntimeError("embed returned unexpected response")
        vectors = resp["vectors"]
        if len(vectors) != len(sub_texts):
            raise RuntimeError("embed returned mismatched vectors")
        for meta, vec in zip(sub_metas, vectors):
            if len(vec) != VECTOR_DIM:
                raise RuntimeError(f"vector dim mismatch {len(vec)} != {VECTOR_DIM}")
            orig_cid = str(meta.get("chunk_id") or "")
            try:
                point_uuid = str(uuid.uuid5(uuid.NAMESPACE_OID, orig_cid if orig_cid else str(uuid.uuid4())))
            except Exception:
                point_uuid = str(uuid.uuid4())
            payload = {
                "document_id": meta.get("document_id"),
                "chunk_id": meta.get("chunk_id"),
                "text": meta.get("text"),
                "token_count": meta.get("token_count"),
                "file_name": meta.get("file_name"),
                "file_type": meta.get("file_type"),
                "source_url": meta.get("source_url"),
                "timestamp": meta.get("timestamp"),
                "parser_version": meta.get("parser_version"),
            }
            point = PointStruct(id=point_uuid, vector=[float(x) for x in vec], payload=payload)
            to_upsert.append(point)
            cm_for_neo4j = {
                "chunk_id": meta.get("chunk_id"),
                "token_count": meta.get("token_count"),
                "file_type": meta.get("file_type"),
                "source_url": meta.get("source_url"),
                "timestamp": meta.get("timestamp"),
                "file_name": meta.get("file_name"),
            }
            docid = meta.get("document_id")
            if docid:
                neo4j_batch_map.setdefault(docid, []).append(cm_for_neo4j)
        if len(to_upsert) >= BATCH_SIZE:
            upsert_points(qdrant_client, QDRANT_COLLECTION, to_upsert[:BATCH_SIZE], batch_size=BATCH_SIZE)
            del to_upsert[:BATCH_SIZE]
    total_elapsed = time.time() - start_batch
    log.info("Completed embedding batch of %d items in %.3fs", len(texts), total_elapsed)
if __name__ == "__main__":
    try:
        import ray as _r
        log.info("ray.__version__=%s", getattr(_r, "__version__", "unknown"))
        log.info("serve attrs: %s", sorted([a for a in dir(serve) if a.startswith("get") or "handle" in a]))
    except Exception:
        log.debug("ray import/version check failed", exc_info=True)
    try:
        rc = ingest_files(LOCAL_DIR_PATH if DATA_IN_LOCAL else "")
        log.info("ingest returned %s", rc)
    except Exception as e:
        log.exception("ingest failed: %s", e)
        sys.exit(2)
    sys.exit(0)
