#!/usr/bin/env python3
# query_and_diagnose.py
# Diagnostic retrieval pipeline for Qdrant + Neo4j + Ray Serve embed/rerank.
# Compatible with:
#   qdrant-client==1.15.1
#   neo4j==5.19.0
#   neo4j-graphrag==1.10.0
#
# Behavior:
# - Probes multiple Qdrant client signatures.
# - Defensive Neo4j Cypher (avoids param-in-MATCH issues).
# - Instrumentation, timeouts, retries.
# - SKIP_SERVE=true uses local stubs to avoid Ray Serve dependencies.

from __future__ import annotations
import os
import sys
import time
import json
import math
import uuid
import logging
import traceback
import concurrent.futures
import functools
from typing import List, Dict, Any, Optional, Tuple

# ---------- Configuration / envs ----------
LOG_LEVEL = os.getenv("LOG_LEVEL", "DEBUG")
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("query_diagnose")

# clients / deployments
RAY_ADDRESS = os.getenv("RAY_ADDRESS", "auto")
EMBED_DEPLOYMENT = os.getenv("EMBED_DEPLOYMENT", "embed_onxx")
RERANK_HANDLE_NAME = os.getenv("RERANK_HANDLE_NAME", "rerank_onxx")
SKIP_SERVE = os.getenv("SKIP_SERVE", "false").lower() in ("1", "true", "yes")

QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY", None)
QDRANT_COLLECTION = os.getenv("COLLECTION", "my_collection")
PREFER_GRPC = os.getenv("PREFER_GRPC", "true").lower() in ("1", "true", "yes")

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "")

VECTOR_DIM = int(os.getenv("VECTOR_DIM", "768"))

TOP_K = int(os.getenv("TOP_K", "5"))
TOP_VECTOR_CHUNKS = int(os.getenv("TOP_VECTOR_CHUNKS", "200"))
TOP_BM25_CHUNKS = int(os.getenv("TOP_BM25_CHUNKS", "100"))

ENABLE_CROSS_ENCODER = os.getenv("ENABLE_CROSS_ENCODER", "true").lower() in ("1", "true", "yes")
MAX_CHUNKS_TO_CROSSENCODER = int(os.getenv("MAX_CHUNKS_TO_CROSSENCODER", "64"))
MAX_CHUNKS_TO_LLM = int(os.getenv("MAX_CHUNKS_TO_LLM", "8"))

HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "30"))
EMBED_TIMEOUT = float(os.getenv("EMBED_TIMEOUT", "10"))
RETRY_ATTEMPTS = int(os.getenv("RETRY_ATTEMPTS", "3"))
RETRY_BASE_SECONDS = float(os.getenv("RETRY_BASE_SECONDS", "0.5"))

# ---------- Imports with early failure messages ----------
try:
    from qdrant_client import QdrantClient
    from qdrant_client.http import models as q_models  # optional
except Exception as e:
    log.exception("Failed to import qdrant-client. Install qdrant-client==1.15.1. Error:")
    raise

try:
    from neo4j import GraphDatabase, exceptions as neo4j_exceptions
except Exception as e:
    log.exception("Failed to import neo4j driver. Install neo4j==5.19.0. Error:")
    raise

if not SKIP_SERVE:
    try:
        import ray
        from ray import serve
    except Exception:
        log.exception("Ray Serve import failed. If you don't use Serve, set SKIP_SERVE=true.")
        raise

# ---------- Helpers ----------
def now_s():
    return time.time()

def pretty_ms(sec: float) -> str:
    return f"{sec * 1000:.1f}ms"

def run_with_timeout(fn, timeout: float = 10.0, *a, **k):
    """Run a sync callable in thread pool with timeout and return (result, elapsed, error)."""
    start = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(functools.partial(fn, *a, **k))
        try:
            res = fut.result(timeout=timeout)
            elapsed = time.time() - start
            return res, elapsed, None
        except concurrent.futures.TimeoutError as e:
            elapsed = time.time() - start
            return None, elapsed, RuntimeError(f"Timed out after {timeout}s running {getattr(fn,'__name__', fn)}")
        except Exception as e:
            elapsed = time.time() - start
            return None, elapsed, e

def timer(fn):
    """Decorator to time function and capture exceptions."""
    @functools.wraps(fn)
    def wrapper(*a, **k):
        t0 = time.time()
        try:
            r = fn(*a, **k)
            t1 = time.time()
            return {"ok": True, "elapsed": t1 - t0, "result": r}
        except Exception as e:
            t1 = time.time()
            return {"ok": False, "elapsed": t1 - t0, "error": e, "trace": traceback.format_exc()}
    return wrapper

def retry_decorator(attempts: int = RETRY_ATTEMPTS, base: float = RETRY_BASE_SECONDS):
    def deco(fn):
        @functools.wraps(fn)
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
            log.error("all retries failed for %s: %s", fn.__name__, last)
            raise last
        return wrapper
    return deco

# resolver for various Serve handle return types (copied + hardened)
def _resolve_handle_response(resp_obj, timeout: float = EMBED_TIMEOUT):
    # direct python types
    if isinstance(resp_obj, (dict, list, str, int, float)):
        return resp_obj
    try:
        import ray as _ray
        ObjectRefType = getattr(_ray, "ObjectRef", None) or getattr(_ray, "_raylet.ObjectRef", None)
    except Exception:
        ObjectRefType = None
    try:
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
        pass
    try:
        if ObjectRefType is not None and isinstance(resp_obj, ObjectRefType):
            return ray.get(resp_obj, timeout=timeout)
    except Exception:
        pass
    try:
        if hasattr(resp_obj, "result") and callable(getattr(resp_obj, "result")):
            try:
                return resp_obj.result()
            except Exception:
                pass
    except Exception:
        pass
    try:
        return ray.get(resp_obj, timeout=timeout)
    except Exception as e:
        raise RuntimeError(f"Unable to resolve Serve handle response: {type(resp_obj)} -> {e}")

# ---------- Client creation (with diagnostics) ----------
@retry_decorator()
def make_clients() -> Tuple[QdrantClient, Any]:
    """Create Qdrant client and Neo4j driver. Return (qdrant_client, neo4j_driver)."""
    log.debug("creating qdrant client -> %s", QDRANT_URL)
    q = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY, prefer_grpc=PREFER_GRPC)
    try:
        info = q.get_collection(QDRANT_COLLECTION)
        log.debug("qdrant collection fetch ok (info: %s)", str(type(info)))
    except Exception as e:
        log.debug("qdrant get_collection check failed (ignored): %s", e)
    log.debug("creating neo4j driver -> %s", NEO4J_URI)
    neo = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    return q, neo

# ---------- Serve embed/rerank helpers ----------
def _get_handle(name: str, timeout: float = 30.0, app_name: Optional[str] = "default"):
    """Get a Ray Serve handle with health-check; fallback to HTTP endpoint if possible."""
    if SKIP_SERVE:
        raise RuntimeError("Serve is skipped via SKIP_SERVE env")
    start = time.time()
    last_exc = None
    while time.time() - start < timeout:
        try:
            _ensure_ray_connected()
            if hasattr(serve, "get_deployment_handle"):
                try:
                    h = serve.get_deployment_handle(name, app_name=app_name, _check_exists=False)
                    resp = h.remote({"texts": ["health-check"], "max_length": 32})
                    _resolve_handle_response(resp, timeout=EMBED_TIMEOUT)
                    return h
                except Exception as e:
                    last_exc = e
            if hasattr(serve, "get_deployment"):
                try:
                    dep = serve.get_deployment(name)
                    h = dep.get_handle(sync=False)
                    resp = h.remote({"texts": ["health-check"], "max_length": 32})
                    _resolve_handle_response(resp, timeout=EMBED_TIMEOUT)
                    return h
                except Exception as e:
                    last_exc = e
            if hasattr(serve, "get_handle"):
                try:
                    h = serve.get_handle(name, sync=False)
                    resp = h.remote({"texts": ["health-check"], "max_length": 32})
                    _resolve_handle_response(resp, timeout=EMBED_TIMEOUT)
                    return h
                except Exception as e:
                    last_exc = e
        except Exception as e:
            last_exc = e
        time.sleep(1.0)
    # HTTP fallback attempt
    try:
        import httpx
        url = f"http://127.0.0.1:8000/{name}"
        r = httpx.post(url, json={"texts": ["health-check"], "max_length": 32}, timeout=5.0)
        if r.status_code == 200:
            log.warning("Using HTTP fallback for %s -> %s", name, url)
            class _HTTPFallbackHandle:
                def __init__(self, url: str, timeout: float = HTTP_TIMEOUT):
                    self.url = url.rstrip("/")
                    self.timeout = timeout
                def remote(self, payload: dict):
                    r2 = httpx.post(self.url, json=payload, timeout=self.timeout)
                    r2.raise_for_status()
                    try:
                        return r2.json()
                    except Exception:
                        return r2.text
            return _HTTPFallbackHandle(url, timeout=HTTP_TIMEOUT)
    except Exception:
        pass
    raise RuntimeError(f"timed out getting handle {name}: {last_exc}")

def _ensure_ray_connected():
    if SKIP_SERVE:
        return
    try:
        import ray
        if not ray.is_initialized():
            ray.init(address=RAY_ADDRESS, ignore_reinit_error=True)
    except Exception as e:
        log.debug("ray init failed: %s", e)
        raise

@retry_decorator()
def embed_text(embed_handle, text: str, max_length: int = 64) -> List[float]:
    """Call embed handle and return a vector. Defensive to different Serve return formats."""
    if SKIP_SERVE or embed_handle is None:
        return [0.0] * VECTOR_DIM
    payload = {"texts": [text], "max_length": int(max_length)}
    resp_obj = embed_handle.remote(payload)
    resp = _resolve_handle_response(resp_obj, timeout=EMBED_TIMEOUT)
    if isinstance(resp, dict) and "vectors" in resp:
        vecs = resp["vectors"]
        if isinstance(vecs, list) and len(vecs) >= 1:
            v = vecs[0]
            if len(v) != VECTOR_DIM:
                log.warning("embed returned vector dim %d != expected %d", len(v), VECTOR_DIM)
            return [float(x) for x in v]
    if isinstance(resp, list) and len(resp) >= 1 and isinstance(resp[0], list):
        v = resp[0]
        return [float(x) for x in v]
    raise RuntimeError(f"embed returned unexpected payload: {type(resp)}")

@retry_decorator()
def cross_rerank(rerank_handle, query: str, texts: List[str], max_length: int = 600) -> List[float]:
    """Call cross-encoder reranker. Returns float scores aligned to texts."""
    if not ENABLE_CROSS_ENCODER:
        raise RuntimeError("Cross-encoder disabled by ENABLE_CROSS_ENCODER")
    if SKIP_SERVE or rerank_handle is None:
        return [float(len(t) % 10) for t in texts]
    payload = {"query": query, "cands": texts, "max_length": int(max_length)}
    resp_obj = rerank_handle.remote(payload)
    resp = _resolve_handle_response(resp_obj, timeout=EMBED_TIMEOUT)
    if not isinstance(resp, dict) or "scores" not in resp:
        raise RuntimeError("rerank returned bad payload")
    scores = resp["scores"]
    return [float(s) for s in scores]

# ---------- Qdrant search helpers with signature probing ----------
def _normalize_qdrant_hit(r) -> Dict[str, Any]:
    """Normalize whatever qdrant-client result type to a dict with id, score, payload, vector (if present)."""
    out = {"id": None, "score": 0.0, "payload": {}, "vector": None}
    try:
        if hasattr(r, "id"):
            out["id"] = str(getattr(r, "id"))
        elif isinstance(r, dict) and "id" in r:
            out["id"] = str(r["id"])
        if hasattr(r, "score"):
            out["score"] = float(getattr(r, "score") or 0.0)
        elif isinstance(r, dict) and "score" in r:
            out["score"] = float(r.get("score") or 0.0)
        if hasattr(r, "payload"):
            out["payload"] = getattr(r, "payload") or {}
        elif isinstance(r, dict) and "payload" in r:
            out["payload"] = r.get("payload") or {}
        if hasattr(r, "vector"):
            out["vector"] = getattr(r, "vector")
        elif isinstance(r, dict) and "vector" in r:
            out["vector"] = r.get("vector")
    except Exception:
        log.debug("failed normalize qdrant hit: %s", traceback.format_exc())
    return out

@retry_decorator()
def qdrant_vector_search(client: QdrantClient, q_vec: List[float], top_k: int,
                         with_payload: bool = True, with_vector: bool = False) -> List[Dict[str, Any]]:
    """Probe multiple qdrant-client call signatures to remain compatible across versions."""
    log.debug("qdrant_vector_search: trying to query collection=%s top_k=%d", QDRANT_COLLECTION, top_k)
    probes = []

    def probe_search1():
        return client.search(collection_name=QDRANT_COLLECTION, query_vector=q_vec, limit=top_k, with_payload=with_payload)
    probes.append(("search(collection_name, query_vector, limit, with_payload)", probe_search1))

    def probe_search2():
        return client.search(collection_name=QDRANT_COLLECTION, vector=q_vec, limit=top_k, with_payload=with_payload)
    probes.append(("search(collection_name, vector, limit, with_payload)", probe_search2))

    def probe_search3():
        kwargs = {"with_payload": with_payload}
        if with_vector:
            kwargs["with_vector"] = True
        return client.search(collection_name=QDRANT_COLLECTION, query_vector=q_vec, limit=top_k, **kwargs)
    probes.append(("search(collection_name, query_vector, with_vector?)", probe_search3))

    def probe_query_points():
        if hasattr(client, "query_points"):
            return client.query_points(collection_name=QDRANT_COLLECTION, query_vector=q_vec, limit=top_k, with_payload=with_payload)
        raise RuntimeError("client has no query_points")
    probes.append(("query_points(...)", probe_query_points))

    last_exc = None
    for name, call in probes:
        try:
            log.debug("qdrant_vector_search: attempting probe: %s", name)
            res = call()
            hits = []
            if res is None:
                continue
            if isinstance(res, dict) and "result" in res and isinstance(res["result"], list):
                iterator = res["result"]
            elif isinstance(res, dict) and "data" in res and isinstance(res["data"], list):
                iterator = res["data"]
            else:
                iterator = res
            for r in iterator:
                hits.append(_normalize_qdrant_hit(r))
            log.debug("qdrant_vector_search: probe %s returned %d hits", name, len(hits))
            return hits
        except AssertionError as e:
            last_exc = e
            log.debug("qdrant probe %s AssertionError: %s", name, e)
            continue
        except TypeError as e:
            last_exc = e
            log.debug("qdrant probe %s TypeError: %s", name, e)
            continue
        except Exception as e:
            last_exc = e
            log.debug("qdrant probe %s failed: %s", name, traceback.format_exc())
            continue
    log.error("qdrant_vector_search: all probes failed; last error: %s", last_exc)
    raise RuntimeError(f"qdrant_vector_search failed: {last_exc}")

# ---------- Neo4j helpers (robust cypher) ----------
@retry_decorator()
def neo4j_fetch_texts(driver, chunk_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Fetch c.text, c.document_id, c.token_count for given chunk_ids. Returns map chunk_id -> record dict."""
    out: Dict[str, Dict[str, Any]] = {}
    if not chunk_ids:
        return out
    cy = """
    MATCH (c:Chunk)
    WHERE c.chunk_id IN $ids
    RETURN c.chunk_id AS cid, c.text AS text, c.token_count AS token_count, c.document_id AS document_id
    """
    with driver.session() as s:
        res = s.run(cy, ids=list(chunk_ids))
        for r in res:
            try:
                out[r["cid"]] = {"text": r.get("text") or "", "token_count": int(r.get("token_count") or 0), "document_id": r.get("document_id")}
            except Exception:
                out[r["cid"]] = {"text": r.get("text") or "", "token_count": 0, "document_id": r.get("document_id")}
    return out

@retry_decorator()
def neo4j_fulltext_search(driver, query: str, index_name: str = "chunkFulltextIndex", k: int = TOP_BM25_CHUNKS) -> List[Tuple[str, float]]:
    """Attempt to run fulltext query. If fulltext index missing, return empty with guidance."""
    log.debug("neo4j_fulltext_search: attempting index query '%s' k=%d", index_name, k)
    cy = "CALL db.index.fulltext.queryNodes($index, $q) YIELD node, score RETURN node.chunk_id AS chunk_id, score ORDER BY score DESC LIMIT $k"
    with driver.session() as s:
        try:
            res = s.run(cy, index=index_name, q=query, k=k)
            out = []
            for r in res:
                out.append((r["chunk_id"], float(r["score"])))
            log.debug("neo4j_fulltext_search: returned %d hits", len(out))
            return out
        except neo4j_exceptions.ClientError as e:
            log.warning("neo4j_fulltext_search ClientError: %s", e)
            raise
        except Exception:
            log.debug("neo4j_fulltext_search generic failure: %s", traceback.format_exc())
            raise

@retry_decorator()
def neo4j_expand_graph(driver, seeds: List[str], hops: int = 1, per_seed_limit: int = 5) -> List[str]:
    """Expand graph neighbors from seed chunk_ids for given hops.
    Build hops into query string safely to avoid parameter-in-MATCH issues.
    """
    if not seeds:
        return []
    hops_int = int(hops)
    cy = f"""
    UNWIND $seeds AS sid
    MATCH (s:Chunk {{chunk_id: sid}})
    CALL {{
      WITH s
      MATCH (s)-[*1..{hops_int}]-(n:Chunk)
      RETURN DISTINCT n.chunk_id AS nid
      LIMIT $limit
    }}
    RETURN DISTINCT nid
    """
    with driver.session() as s:
        res = s.run(cy, seeds=list(seeds), limit=per_seed_limit)
        out = []
        for r in res:
            out.append(r["nid"])
        log.debug("neo4j_expand_graph: returned %d neighbors", len(out))
        return out

# ---------- Utility: RRF and dedupe ----------
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

def stable_dedupe(ids: List[str]) -> List[str]:
    seen = set()
    out = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out

# ---------- Full retrieve pipeline with diagnostics ----------
def retrieve_pipeline(embed_handle, rerank_handle, qdrant_client, neo4j_driver, query_text: str,
                      max_chunks: int = MAX_CHUNKS_TO_LLM) -> Dict[str, Any]:
    """Full retrieval pipeline with instrumentation. Returns prompt, provenance, records, and a diagnostics map."""
    diag: Dict[str, Any] = {"steps": {}, "errors": []}
    overall_t0 = time.time()

    # 1) embed
    t0 = time.time()
    try:
        v, el, err = run_with_timeout(lambda: embed_text(embed_handle, query_text, max_length=64), timeout=EMBED_TIMEOUT)
        if err:
            raise err
        q_vec = v
        diag["steps"]["embed"] = {"ok": True, "elapsed": el}
    except Exception as e:
        diag["steps"]["embed"] = {"ok": False, "error": str(e), "trace": traceback.format_exc()}
        log.error("embed failed: %s", e)
        return {"prompt": "", "provenance": [], "records": [], "llm": None, "diag": diag}

    # 2) ANN fetch (Qdrant)
    ann_hits = []
    try:
        res, el, err = run_with_timeout(lambda: qdrant_vector_search(qdrant_client, q_vec, TOP_VECTOR_CHUNKS, with_payload=True, with_vector=False), timeout=15.0)
        if err:
            raise err
        ann_hits = res
        diag["steps"]["qdrant_ann"] = {"ok": True, "elapsed": el, "count": len(ann_hits)}
    except Exception as e:
        diag["steps"]["qdrant_ann"] = {"ok": False, "error": str(e), "trace": traceback.format_exc()}
        log.error("qdrant ANN fetch failed: %s", e)

    # extract ids
    vec_ids = []
    for h in ann_hits:
        pid = None
        try:
            payload = h.get("payload") or {}
            pid = payload.get("chunk_id") or payload.get("chunkId") or h.get("id")
            if pid:
                vec_ids.append(str(pid))
        except Exception:
            continue
    vec_ids = [str(x) for x in vec_ids if x]
    if not vec_ids:
        diag["steps"]["early_exit"] = "no ANN ids"
        return {"prompt": "", "provenance": [], "records": [], "llm": None, "diag": diag}

    # 3) BM25 fulltext (Neo4j) - best-effort
    bm25_hits = []
    try:
        res, el, err = run_with_timeout(lambda: neo4j_fulltext_search(neo4j_driver, query_text, k=TOP_BM25_CHUNKS), timeout=8.0)
        if err:
            raise err
        bm25_hits = res
        diag["steps"]["neo4j_bm25"] = {"ok": True, "elapsed": el, "count": len(bm25_hits)}
    except Exception as e:
        diag["steps"]["neo4j_bm25"] = {"ok": False, "error": str(e)}
        log.warning("neo4j fulltext search failed (index may be missing): %s", e)

    # 4) First-stage RRF fuse
    vec_rank = [h["payload"].get("chunk_id") if h.get("payload") else h.get("id") for h in ann_hits if h.get("payload") or h.get("id")]
    vec_rank = [str(x) for x in vec_rank if x]
    bm25_rank = [cid for (cid, s) in bm25_hits]
    first_rrf = _rrf_fuse([vec_rank, bm25_rank], k=60)
    fused_sorted = sorted(first_rrf.items(), key=lambda x: -x[1])
    fused_list = [x[0] for x in fused_sorted]
    diag["steps"]["first_rrf"] = {"ok": True, "counts": {"vec": len(vec_rank), "bm25": len(bm25_rank), "fused": len(fused_list)}}

    # 5) Deduplicate and choose seeds
    deduped = stable_dedupe(fused_list)
    seeds = deduped[:min(len(deduped), 20)]
    diag["steps"]["seeds"] = {"ok": True, "seed_count": len(seeds)}

    # 6) Graph expansion
    expanded = []
    try:
        res, el, err = run_with_timeout(lambda: neo4j_expand_graph(neo4j_driver, seeds, hops=1, per_seed_limit=5), timeout=8.0)
        if err:
            raise err
        expanded = res
        diag["steps"]["graph_expand"] = {"ok": True, "elapsed": el, "added": len(expanded)}
    except Exception as e:
        diag["steps"]["graph_expand"] = {"ok": False, "error": str(e)}
        log.warning("graph expansion failed: %s", e)

    combined = stable_dedupe(seeds + expanded + vec_rank + bm25_rank)
    diag["steps"]["combined_candidates"] = {"ok": True, "count": len(combined)}

    # 7) Assemble candidate details (texts + vectors)
    try:
        texts_map, el, err = run_with_timeout(lambda: neo4j_fetch_texts(neo4j_driver, combined), timeout=8.0)
        if err:
            raise err
        diag["steps"]["neo4j_fetch_texts"] = {"ok": True, "elapsed": el, "count": len(texts_map)}
    except Exception as e:
        texts_map = {}
        diag["steps"]["neo4j_fetch_texts"] = {"ok": False, "error": str(e)}
        log.warning("neo4j fetch texts failed: %s", e)

    qdrant_map = {}
    for h in ann_hits:
        try:
            cid = (h.get("payload") or {}).get("chunk_id") or h.get("id")
            if not cid:
                continue
            qdrant_map[str(cid)] = h
        except Exception:
            continue

    candidates = []
    for cid in combined:
        rec = {"chunk_id": cid, "text": "", "document_id": None, "token_count": None, "vec_score": 0.0}
        if cid in texts_map:
            rec.update({"text": texts_map[cid].get("text", ""), "token_count": texts_map[cid].get("token_count", 0), "document_id": texts_map[cid].get("document_id")})
        if cid in qdrant_map:
            rec["vec_score"] = float(qdrant_map[cid].get("score", 0.0))
            payload = qdrant_map[cid].get("payload") or {}
            if not rec["text"]:
                rec["text"] = payload.get("snippet") or payload.get("text") or ""
        candidates.append(rec)
    diag["steps"]["assemble_candidates"] = {"ok": True, "count": len(candidates)}

    # 8) Compute vec similarities if vectors present
    def _cos(v1, v2):
        if not v1 or not v2:
            return 0.0
        try:
            dot = sum(a*b for a,b in zip(v1, v2))
            nv1 = math.sqrt(sum(a*a for a in v1))
            nv2 = math.sqrt(sum(b*b for b in v2))
            if nv1 == 0 or nv2 == 0: return 0.0
            return dot / (nv1 * nv2)
        except Exception:
            return 0.0

    vecs_present = any(bool((qdrant_map.get(c["chunk_id"]) or {}).get("vector")) for c in candidates)
    if vecs_present:
        q_vec_local = q_vec
        for c in candidates:
            qh = qdrant_map.get(c["chunk_id"])
            if qh and qh.get("vector"):
                c["vec_sim"] = _cos(q_vec_local, qh.get("vector"))
            else:
                c["vec_sim"] = 0.0
        diag["steps"]["vec_sim"] = {"ok": True, "method": "qdrant_vectors", "count": sum(1 for c in candidates if c.get("vec_sim",0)>0)}
    else:
        for c in candidates:
            c["vec_sim"] = c.get("vec_score", 0.0)
        diag["steps"]["vec_sim"] = {"ok": True, "method": "fallback_vec_score"}

    # 9) Second-stage fusion
    vec_rank2 = [c["chunk_id"] for c in sorted(candidates, key=lambda x: -x.get("vec_sim", 0.0))]
    bm25_map = {cid: score for cid, score in bm25_hits}
    bm25_rank2 = sorted([cid for cid in bm25_map.keys() if cid in {c["chunk_id"] for c in candidates}], key=lambda x: -bm25_map.get(x, 0.0))
    second_rrf_map = _rrf_fuse([vec_rank2, bm25_rank2], k=60)
    final_fused = sorted(second_rrf_map.items(), key=lambda x: -x[1])
    final_order = [x[0] for x in final_fused]
    final_order = stable_dedupe(final_order)
    diag["steps"]["second_rrf"] = {"ok": True, "candidate_count": len(candidates), "final_count": len(final_order)}

    # 10) optional cross-encoder re-rank
    try:
        if ENABLE_CROSS_ENCODER and rerank_handle:
            top_for_cross = final_order[:min(len(final_order), MAX_CHUNKS_TO_CROSSENCODER)]
            texts = [next((c["text"] for c in candidates if c["chunk_id"] == cid), "") for cid in top_for_cross]
            scores = cross_rerank(rerank_handle, query_text, texts)
            if scores and len(scores) == len(top_for_cross):
                merged = [{"chunk_id": top_for_cross[i], "score": 0.8 * scores[i] + 0.2 * second_rrf_map.get(top_for_cross[i], 0.0)} for i in range(len(top_for_cross))]
                merged_sorted = sorted(merged, key=lambda x: -x["score"])
                ordered = [m["chunk_id"] for m in merged_sorted]
                remaining = [cid for cid in final_order if cid not in set(ordered)]
                final_order = ordered + remaining
                diag["steps"]["cross_rerank"] = {"ok": True, "used": len(ordered)}
            else:
                diag["steps"]["cross_rerank"] = {"ok": False, "error": "scores len mismatch"}
        else:
            diag["steps"]["cross_rerank"] = {"ok": False, "reason": "disabled_or_no_handle"}
    except Exception as e:
        diag["steps"]["cross_rerank"] = {"ok": False, "error": str(e), "trace": traceback.format_exc()}
        log.warning("cross rerank failed: %s", e)

    # 11) final selection for LLM with token budget enforcement
    selected = []
    token_sum = 0
    MAX_PROMPT_TOKENS = int(os.getenv("MAX_PROMPT_TOKENS", "1500"))
    for cid in final_order:
        rec = next((c for c in candidates if c["chunk_id"] == cid), None)
        if rec is None:
            continue
        tok = rec.get("token_count") or estimate_tokens_for_text(rec.get("text") or "")
        if token_sum + tok > MAX_PROMPT_TOKENS:
            log.debug("token budget reached: %d + %d > %d", token_sum, tok, MAX_PROMPT_TOKENS)
            continue
        selected.append(rec)
        token_sum += tok
        if len(selected) >= max_chunks:
            break
    diag["steps"]["final_selection"] = {"ok": True, "selected": len(selected), "token_sum": token_sum}

    # assemble prompt
    pieces = []
    provenance = []
    for r in selected:
        pieces.append(f"CHUNK (doc={r.get('document_id')} chunk={r['chunk_id']}):\n{r['text']}")
        provenance.append({"document_id": r.get("document_id"), "chunk_id": r["chunk_id"], "score": r.get("vec_sim", 0.0)})
    context = "\n\n---\n\n".join(pieces)
    prompt = f"USE ONLY THE CONTEXT BELOW. Cite provenance by document_id.\n\nCONTEXT:\n{context}\n\nUSER QUERY:\n{query_text}\n"

    overall_elapsed = time.time() - overall_t0
    diag["overall_elapsed"] = overall_elapsed
    return {"prompt": prompt, "provenance": provenance, "records": selected, "llm": None, "diag": diag}

# ---------- Utilities ----------
def estimate_tokens_for_text(text: str) -> int:
    if not text:
        return 0
    return max(1, int(len(text) / 4))

# ---------- Main runnable diagnostic entry ----------
def main():
    log.info("query_and_diagnose.py starting")
    embed_handle = None
    rerank_handle = None

    if SKIP_SERVE:
        log.info("SKIP_SERVE enabled. Using local stubs for embed/rerank.")
        class LocalEmbed:
            def remote(self, payload: dict):
                return {"vectors": [[0.0] * VECTOR_DIM]}
        class LocalRerank:
            def remote(self, payload: dict):
                cands = payload.get("cands", [])
                return {"scores": [float(len(t) % 10) for t in cands]}
        embed_handle = LocalEmbed()
        rerank_handle = LocalRerank() if ENABLE_CROSS_ENCODER else None
    else:
        try:
            _ensure_ray_connected()
        except Exception as e:
            log.warning("ray not connected: %s", e)
        try:
            embed_handle = _get_handle(EMBED_DEPLOYMENT)
            log.info("embed handle obtained")
        except Exception as e:
            log.warning("embed handle not obtained: %s", e)
            embed_handle = None
        try:
            rerank_handle = _get_handle(RERANK_HANDLE_NAME)
            log.info("rerank handle obtained")
        except Exception:
            log.info("no rerank handle found; cross-encoder disabled")
            rerank_handle = None

    # Create qdrant + neo4j clients
    try:
        clients_res, el, err = run_with_timeout(make_clients, timeout=15.0)
        if err:
            raise err
        qdrant_client, neo4j_driver = clients_res
        log.info("clients created (qdrant + neo4j) in %s", pretty_ms(el))
    except Exception as e:
        log.exception("make_clients failed: %s", e)
        sys.exit(1)

    q = os.getenv("DIAG_QUERY", "health-check")
    try:
        out = retrieve_pipeline(embed_handle, rerank_handle, qdrant_client, neo4j_driver, q, max_chunks=MAX_CHUNKS_TO_LLM)
        report = {
            "ok": True,
            "provenance_count": len(out.get("provenance", [])),
            "prompt_len": len(out.get("prompt", "")),
            "elapsed": out.get("diag", {}).get("overall_elapsed", None),
            "diag": out.get("diag", {})
        }
        print(json.dumps(report, indent=2))
    except Exception as e:
        log.exception("retrieve_pipeline failed: %s", e)
        print(json.dumps({"ok": False, "error": str(e), "trace": traceback.format_exc()}, indent=2))
    finally:
        # clean up clients safely
        try:
            qdrant_client.close()
        except Exception:
            log.debug("qdrant_client.close failed, ignored")
        try:
            neo4j_driver.close()
        except Exception:
            log.debug("neo4j_driver.close failed, ignored")

if __name__ == "__main__":
    main()
