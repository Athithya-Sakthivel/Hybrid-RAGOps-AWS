#!/usr/bin/env python3
# query.py
# Minimal robust retrieval pipeline using Ray Serve, Qdrant and Neo4j.
# Drop-in replacement to test and run queries reliably.
from __future__ import annotations
import os
import time
import json
import math
import logging
import uuid
from typing import List, Dict, Any, Optional

import ray
from ray import serve

# ENV / CONFIG (kept comprehensive)
RAY_ADDRESS = os.getenv("RAY_ADDRESS", "auto")
RAY_NAMESPACE = os.getenv("RAY_NAMESPACE", None)
EMBED_DEPLOYMENT = os.getenv("EMBED_DEPLOYMENT", "embed_onxx")
RERANK_HANDLE_NAME = os.getenv("RERANK_HANDLE_NAME", "rerank_onxx")

QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY", None)
PREFER_GRPC = os.getenv("PREFER_GRPC", "true").lower() in ("1", "true", "yes")
QDRANT_COLLECTION = os.getenv("COLLECTION", "my_collection")
VECTOR_DIM = int(os.getenv("VECTOR_DIM", "768"))

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "")

TOP_K = int(os.getenv("TOP_K", "5"))
TOP_VECTOR_CHUNKS = int(os.getenv("TOP_VECTOR_CHUNKS", "100"))
INFERENCE_EMBEDDER_MAX_TOKENS = int(os.getenv("INFERENCE_EMBEDDER_MAX_TOKENS", "64"))

HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "30"))
EMBED_TIMEOUT = float(os.getenv("EMBED_TIMEOUT", "10"))
RETRY_ATTEMPTS = int(os.getenv("RETRY_ATTEMPTS", "3"))
RETRY_BASE_SECONDS = float(os.getenv("RETRY_BASE_SECONDS", "0.5"))

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Logging
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("query")

# External libs (fail early with clear error)
try:
    from qdrant_client import QdrantClient
    from qdrant_client.models import Filter, FieldCondition, MatchValue, MatchText
except Exception as e:
    raise RuntimeError("qdrant-client import failed: " + str(e))

try:
    from neo4j import GraphDatabase
except Exception as e:
    raise RuntimeError("neo4j driver import failed: " + str(e))

# --- Utilities ---
def retry(attempts: int = RETRY_ATTEMPTS, base: float = RETRY_BASE_SECONDS):
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

def _ensure_ray_connected():
    if not ray.is_initialized():
        ray.init(address=RAY_ADDRESS, namespace=RAY_NAMESPACE, ignore_reinit_error=True)

# Robust resolver for many Serve return types including DeploymentResponse
def _resolve_handle_response(resp_obj, timeout: float = EMBED_TIMEOUT):
    # direct python types
    if isinstance(resp_obj, (dict, list, str, int, float)):
        return resp_obj
    # ray ObjectRef types
    try:
        import ray as _ray
        ObjectRefType = getattr(_ray, "ObjectRef", None) or getattr(_ray, "_raylet.ObjectRef", None)
    except Exception:
        ObjectRefType = None
    # Serve DeploymentResponse wrapper often exposes .object_refs or ._object_refs
    try:
        # check for Serve wrapper containing object refs
        if hasattr(resp_obj, "object_refs"):
            refs = getattr(resp_obj, "object_refs")
            if refs:
                if isinstance(refs, (list, tuple)):
                    if len(refs) == 1:
                        return _ray.get(refs[0], timeout=timeout)
                    return [_ray.get(r, timeout=timeout) for r in refs]
        if hasattr(resp_obj, "_object_refs"):
            refs = getattr(resp_obj, "_object_refs")
            if refs:
                if isinstance(refs, (list, tuple)):
                    if len(refs) == 1:
                        return _ray.get(refs[0], timeout=timeout)
                    return [_ray.get(r, timeout=timeout) for r in refs]
    except Exception:
        # swallow and try other resolution
        pass
    # If it's an ObjectRef directly
    try:
        if ObjectRefType is not None and isinstance(resp_obj, ObjectRefType):
            return ray.get(resp_obj, timeout=timeout)
    except Exception:
        pass
    # If it offers .result()
    try:
        if hasattr(resp_obj, "result") and callable(getattr(resp_obj, "result")):
            try:
                return resp_obj.result()
            except Exception:
                pass
    except Exception:
        pass
    # last attempt: ray.get
    try:
        return ray.get(resp_obj, timeout=timeout)
    except Exception as e:
        raise RuntimeError(f"Unable to resolve Serve handle response: {type(resp_obj)} -> {e}")

# HTTP fallback handle for local dev if Serve handle resolution fails
class _HTTPFallbackHandle:
    def __init__(self, url: str, timeout: float = HTTP_TIMEOUT):
        self.url = url.rstrip("/")
        self.timeout = timeout
    def remote(self, payload: dict):
        import httpx
        r = httpx.post(self.url, json=payload, timeout=self.timeout)
        r.raise_for_status()
        try:
            return r.json()
        except Exception:
            return r.text

# Get a Serve handle with health-check and fallback to local HTTP
def _get_handle(name: str, timeout: float = 30.0, poll: float = 1.0, app_name: Optional[str] = "default"):
    start = time.time()
    last_exc = None
    while time.time() - start < timeout:
        try:
            _ensure_ray_connected()
            # modern API
            if hasattr(serve, "get_deployment_handle"):
                try:
                    h = serve.get_deployment_handle(name, app_name=app_name, _check_exists=False)
                    resp = h.remote({"texts":["health-check"], "max_length": INFERENCE_EMBEDDER_MAX_TOKENS})
                    _resolve_handle_response(resp, timeout=EMBED_TIMEOUT)
                    return h
                except Exception as e:
                    last_exc = e
            # legacy API variants
            if hasattr(serve, "get_deployment"):
                try:
                    dep = serve.get_deployment(name)
                    h = dep.get_handle(sync=False)
                    resp = h.remote({"texts":["health-check"], "max_length": INFERENCE_EMBEDDER_MAX_TOKENS})
                    _resolve_handle_response(resp, timeout=EMBED_TIMEOUT)
                    return h
                except Exception as e:
                    last_exc = e
            if hasattr(serve, "get_handle"):
                try:
                    h = serve.get_handle(name, sync=False)
                    resp = h.remote({"texts":["health-check"], "max_length": INFERENCE_EMBEDDER_MAX_TOKENS})
                    _resolve_handle_response(resp, timeout=EMBED_TIMEOUT)
                    return h
                except Exception as e:
                    last_exc = e
        except Exception as e:
            last_exc = e
        time.sleep(poll)

    # HTTP fallback (useful when Serve is not available or handle types mismatch)
    try:
        import httpx
        url = f"http://127.0.0.1:8000/{name}"
        r = httpx.post(url, json={"texts":["health-check"], "max_length": INFERENCE_EMBEDDER_MAX_TOKENS}, timeout=5.0)
        if r.status_code == 200:
            log.warning("Using HTTP fallback for %s -> %s", name, url)
            return _HTTPFallbackHandle(url, timeout=HTTP_TIMEOUT)
    except Exception:
        pass

    raise RuntimeError(f"timed out getting handle {name}: {last_exc}")

# --- Clients creation (retry) ---
@retry()
def make_clients():
    q = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY, prefer_grpc=PREFER_GRPC)
    neo = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    return q, neo

# --- Embedding + rerank wrappers ---
@retry()
def embed_text(embed_handle, text: str, max_length: int = INFERENCE_EMBEDDER_MAX_TOKENS) -> List[float]:
    payload = {"texts": [text], "max_length": int(max_length)}
    resp_obj = embed_handle.remote(payload)
    resp = _resolve_handle_response(resp_obj, timeout=EMBED_TIMEOUT)
    if not isinstance(resp, dict) or "vectors" not in resp:
        raise RuntimeError("embed returned bad payload")
    vecs = resp["vectors"]
    if not isinstance(vecs, list) or len(vecs) != 1:
        raise RuntimeError("embed returned wrong shape")
    v = vecs[0]
    if len(v) != VECTOR_DIM:
        raise RuntimeError(f"embed dim mismatch {len(v)} != {VECTOR_DIM}")
    return [float(x) for x in v]

@retry()
def cross_rerank(rerank_handle, query: str, texts: List[str], max_length: int = 600) -> List[float]:
    if rerank_handle is None:
        raise RuntimeError("rerank handle not present")
    if not texts:
        return []
    payload = {"query": query, "cands": texts, "max_length": int(max_length)}
    resp_obj = rerank_handle.remote(payload)
    resp = _resolve_handle_response(resp_obj, timeout=EMBED_TIMEOUT)
    if not isinstance(resp, dict) or "scores" not in resp:
        raise RuntimeError("rerank returned bad payload")
    scores = resp["scores"]
    return [float(s) for s in scores]

# --- Qdrant / Neo4j retrieval helpers ---
@retry()
def qdrant_vector_search(client: QdrantClient, q_vec: List[float], top_k: int) -> List[Dict[str, Any]]:
    try:
        results = client.search(collection_name=QDRANT_COLLECTION, query_vector=q_vec, limit=top_k, with_payload=True, with_vector=True)
    except Exception as e:
        log.warning("qdrant vector search failed: %s", e)
        return []
    out = []
    for r in results:
        payload = getattr(r, "payload", {}) or {}
        out.append({
            "id": str(getattr(r, "id", None)),
            "score": float(getattr(r, "score", 0.0) or 0.0),
            "payload": payload,
            "vector": getattr(r, "vector", None)
        })
    return out

@retry()
def neo4j_fetch_texts(driver, chunk_ids: List[str]) -> Dict[str, str]:
    if not chunk_ids:
        return {}
    out: Dict[str, str] = {}
    with driver.session() as s:
        cy = "MATCH (c:Chunk) WHERE c.chunk_id IN $ids RETURN c.chunk_id AS cid, c.text AS text"
        res = s.run(cy, ids=list(chunk_ids))
        for r in res:
            try:
                out[r["cid"]] = r["text"] or ""
            except Exception:
                pass
    return out

# --- simple fuse (RRF) used to merge lists ---
def _rrf_fuse(ranked_lists: List[List[str]], k: int = 60) -> Dict[str, float]:
    scores: Dict[str, float] = {}
    for lst in ranked_lists:
        for i, it in enumerate(lst):
            rank = i + 1
            key = it
            if key is None:
                continue
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
    return scores

# --- Two-stage query: embed -> qdrant -> neo4j -> assemble prompt ---
def two_stage_query(embed_handle, rerank_handle, qdrant_client, neo4j_driver, query_text: str, top_k: int = TOP_K) -> Dict[str, Any]:
    t0 = time.time()
    q_vec = embed_text(embed_handle, query_text, max_length=INFERENCE_EMBEDDER_MAX_TOKENS)
    t1 = time.time()
    log.debug("embed elapsed %.3fs", t1 - t0)

    vec_hits = qdrant_vector_search(qdrant_client, q_vec, TOP_VECTOR_CHUNKS)
    t2 = time.time()
    log.debug("qdrant elapsed %.3fs hits=%d", t2 - t1, len(vec_hits))

    vec_ids = [h["payload"].get("chunk_id") for h in vec_hits if h.get("payload") and h["payload"].get("chunk_id")]
    # fallback: if no payload chunk_id found, treat qdrant ids as strings
    vec_ids = [str(x) for x in vec_ids if x]

    if not vec_ids:
        return {"prompt": "", "provenance": [], "records": [], "llm": None}

    # Get texts for top candidates from Neo4j
    texts_map = neo4j_fetch_texts(neo4j_driver, vec_ids)
    t3 = time.time()
    log.debug("neo4j fetch elapsed %.3fs", t3 - t2)

    # assemble records: prefer full text from neo4j, fallback to snippet in qdrant payload
    records = []
    for hit in vec_hits:
        payload = hit.get("payload", {}) or {}
        cid = payload.get("chunk_id") or payload.get("chunkId") or None
        if not cid:
            continue
        text = texts_map.get(cid) or payload.get("snippet") or ""
        score = float(hit.get("score", 0.0) or 0.0)
        records.append({"chunk_id": cid, "document_id": payload.get("document_id"), "text": text, "score": score})

    # simple re-rank with RRF if rerank_handle present and enough candidates
    final_order = [r["chunk_id"] for r in records]
    if rerank_handle and records:
        try:
            cands = [r["text"] for r in records[:min(len(records), 64)]]
            scores = cross_rerank(rerank_handle, query_text, cands)
            if scores and len(scores) == len(cands):
                merged = [{"chunk_id": records[i]["chunk_id"], "score": 0.8 * scores[i] + 0.2 * records[i]["score"]} for i in range(len(cands))]
                merged_sorted = sorted(merged, key=lambda x: -x["score"])
                ordered = [m["chunk_id"] for m in merged_sorted]
                # append any remaining ids not in merged
                remaining = [r["chunk_id"] for r in records if r["chunk_id"] not in set(ordered)]
                final_order = ordered + remaining
        except Exception as e:
            log.warning("cross-rerank failed: %s", e)

    # pick top_k
    final_ids = list(dict.fromkeys(final_order))[:top_k]
    final_records = [r for cid in final_ids for r in records if r["chunk_id"] == cid]

    # build prompt context
    pieces = []
    provenance = []
    for r in final_records:
        pieces.append(f"CHUNK (doc={r.get('document_id')} chunk={r['chunk_id']}):\n{r['text']}")
        provenance.append({"document_id": r.get("document_id"), "chunk_id": r['chunk_id'], "score": r.get("score", 0.0)})

    context = "\n\n---\n\n".join(pieces)
    prompt = f"USE ONLY THE CONTEXT BELOW. Cite provenance by document_id.\n\nCONTEXT:\n{context}\n\nUSER QUERY:\n{query_text}\n"

    return {"prompt": prompt, "provenance": provenance, "records": final_records, "llm": None}

# --- Main runnable test ---
def main():
    log.info("query.py starting")
    _ensure_ray_connected()
    embed_handle = None
    rerank_handle = None
    try:
        embed_handle = _get_handle(EMBED_DEPLOYMENT)
    except Exception as e:
        log.warning("embed handle not available via Serve: %s. HTTP fallback will be used if available.", e)
        # don't exit, HTTP fallback is attempted inside _get_handle; if not present, will error on use

    try:
        rerank_handle = _get_handle(RERANK_HANDLE_NAME)
    except Exception:
        log.info("no rerank handle found; cross-encoder disabled")

    # create qdrant + neo4j clients
    qdrant, neo4j_driver = make_clients()

    # quick smoke test
    q = "health-check"
    try:
        out = two_stage_query(embed_handle, rerank_handle, qdrant, neo4j_driver, q, top_k=TOP_K)
        print(json.dumps({"ok": True, "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "result_preview": {"prompt_len": len(out["prompt"]), "provenance_count": len(out["provenance"]) }}, indent=2))
    except Exception as e:
        log.exception("two_stage_query failed: %s", e)
        print(json.dumps({"ok": False, "error": str(e)}))

    # close clients cleanly
    try:
        qdrant.close()
    except Exception:
        pass
    try:
        neo4j_driver.close()
    except Exception:
        pass

if __name__ == "__main__":
    main()
