#!/usr/bin/env python3
# inference_pipeline/query.py
# Hardened retrieval pipeline compatible with:
#   qdrant-client==1.15.1, neo4j==5.19.0
# - robust Ray Serve handle resolution + HTTP fallback
# - resilient qdrant search/query_points compatibility
# - neo4j fulltext + graph expansion
# - retries, timeouts, simple profiling

from __future__ import annotations
import os
import time
import math
import json
import logging
import random
import threading
import queue
import functools
from typing import Any, Dict, List, Optional, Tuple, Iterable

# optional heavy deps imported with friendly errors
try:
    import ray
    from ray import serve
except Exception:
    ray = None
    serve = None

try:
    from qdrant_client import QdrantClient
except Exception as e:
    raise RuntimeError("qdrant-client import failed: " + str(e))

try:
    from neo4j import GraphDatabase
    from neo4j.exceptions import Neo4jError
except Exception as e:
    raise RuntimeError("neo4j driver import failed: " + str(e))

try:
    import httpx
except Exception:
    httpx = None

# ---------- CONFIG ----------
LOG_LEVEL = os.getenv("LOG_LEVEL", "DEBUG")
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("query")

RAY_ADDRESS = os.getenv("RAY_ADDRESS", "auto")
RAY_NAMESPACE = os.getenv("RAY_NAMESPACE", None)

EMBED_DEPLOYMENT = os.getenv("EMBED_DEPLOYMENT", "embed_onxx")
RERANK_HANDLE_NAME = os.getenv("RERANK_HANDLE_NAME", "rerank_onxx")
EMBED_HTTP_URL = os.getenv("EMBED_HTTP_URL", "")  # explicit HTTP fallback for embed

QDRANT_URL = os.getenv("QDRANT_URL", "http://127.0.0.1:6333")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY", None)
PREFER_GRPC = os.getenv("PREFER_GRPC", "true").lower() in ("1", "true", "yes")
QDRANT_COLLECTION = os.getenv("COLLECTION", "my_collection")

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "")

VECTOR_DIM = int(os.getenv("VECTOR_DIM", "768"))

# retrieval tuning
TOP_K = int(os.getenv("TOP_K", "5"))
TOP_VECTOR_CHUNKS = int(os.getenv("TOP_VECTOR_CHUNKS", "200"))
TOP_BM25_CHUNKS = int(os.getenv("TOP_BM25_CHUNKS", "100"))
ENABLE_METADATA_CHUNKS = os.getenv("ENABLE_METADATA_CHUNKS", "false").lower() in ("1", "true", "yes")
MAX_METADATA_CHUNKS = int(os.getenv("MAX_METADATA_CHUNKS", "50"))
FIRST_STAGE_RRF_K = int(os.getenv("FIRST_STAGE_RRF_K", "60"))
SECOND_STAGE_RRF_K = int(os.getenv("SECOND_STAGE_RRF_K", "60"))
MAX_CHUNKS_FOR_GRAPH_EXPANSION = int(os.getenv("MAX_CHUNKS_FOR_GRAPH_EXPANSION", "20"))
GRAPH_EXPANSION_HOPS = int(os.getenv("GRAPH_EXPANSION_HOPS", "1"))
MAX_CHUNKS_TO_CROSSENCODER = int(os.getenv("MAX_CHUNKS_TO_CROSSENCODER", "64"))
MAX_CHUNKS_TO_LLM = int(os.getenv("MAX_CHUNKS_TO_LLM", "8"))
ENABLE_CROSS_ENCODER = os.getenv("ENABLE_CROSS_ENCODER", "true").lower() in ("1", "true", "yes")

INFERENCE_EMBEDDER_MAX_TOKENS = int(os.getenv("INFERENCE_EMBEDDER_MAX_TOKENS", "64"))
CROSS_ENCODER_MAX_TOKENS = int(os.getenv("CROSS_ENCODER_MAX_TOKENS", "600"))
MAX_PROMPT_TOKENS = int(os.getenv("MAX_PROMPT_TOKENS", "3000"))

HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "30"))
EMBED_TIMEOUT = float(os.getenv("EMBED_TIMEOUT", "10"))
CALL_TIMEOUT_SECONDS = float(os.getenv("CALL_TIMEOUT_SECONDS", "10"))

RETRY_ATTEMPTS = int(os.getenv("RETRY_ATTEMPTS", "3"))
RETRY_BASE_SECONDS = float(os.getenv("RETRY_BASE_SECONDS", "0.5"))
RETRY_JITTER = float(os.getenv("RETRY_JITTER", "0.3"))

# ---------- Utilities ----------
def retry(attempts: int = RETRY_ATTEMPTS, base: float = RETRY_BASE_SECONDS, jitter: float = RETRY_JITTER):
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **k):
            last = None
            for i in range(attempts):
                try:
                    return fn(*a, **k)
                except Exception as e:
                    last = e
                    wait = base * (2 ** i) + random.uniform(0, jitter)
                    log.warning("retry %d/%d %s failed: %s; sleeping %.2fs", i + 1, attempts, fn.__name__, e, wait)
                    time.sleep(wait)
            log.error("all retries failed for %s: %s", fn.__name__, last)
            raise last
        return wrapper
    return deco

def run_with_timeout(fn, args=(), kwargs=None, timeout: float = CALL_TIMEOUT_SECONDS):
    if kwargs is None:
        kwargs = {}
    q = queue.Queue()
    def target():
        try:
            res = fn(*args, **kwargs)
            q.put(("ok", res))
        except Exception as e:
            q.put(("err", e))
    t = threading.Thread(target=target, daemon=True)
    t.start()
    try:
        status, payload = q.get(timeout=timeout)
    except queue.Empty:
        raise TimeoutError(f"{fn.__name__} timed out after {timeout}s")
    if status == "err":
        raise payload
    return payload

def _ensure_ray_connected():
    if ray is None:
        return
    if not ray.is_initialized():
        ray.init(address=RAY_ADDRESS, namespace=RAY_NAMESPACE, ignore_reinit_error=True)

def _resolve_handle_response(resp_obj, timeout: float = EMBED_TIMEOUT):
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
            return resp_obj.result()
    except Exception:
        pass
    try:
        return ray.get(resp_obj, timeout=timeout)
    except Exception:
        pass
    try:
        if hasattr(resp_obj, "status_code"):
            try:
                return resp_obj.json()
            except Exception:
                return resp_obj.text
    except Exception:
        pass
    raise RuntimeError(f"Unable to resolve Serve handle response: {type(resp_obj)}")

# Robust _get_handle with HTTP fallback
def _get_handle(name: str, timeout: float = 30.0, poll: float = 1.0, app_name: Optional[str] = "default"):
    start = time.time()
    last_exc = None
    while time.time() - start < timeout:
        try:
            _ensure_ray_connected()
            if serve is not None:
                if hasattr(serve, "get_deployment_handle"):
                    try:
                        h = serve.get_deployment_handle(name, _check_exists=False)
                    except TypeError:
                        try:
                            h = serve.get_deployment_handle(name, app_name=app_name, _check_exists=False)
                        except Exception as e:
                            last_exc = e
                            h = None
                    if h is not None:
                        try:
                            resp = h.remote({"texts": ["health-check"], "max_length": INFERENCE_EMBEDDER_MAX_TOKENS})
                            _resolve_handle_response(resp, timeout=EMBED_TIMEOUT)
                            return h
                        except Exception as e:
                            last_exc = e
                if hasattr(serve, "get_deployment"):
                    try:
                        dep = serve.get_deployment(name)
                        h = dep.get_handle(sync=False)
                        resp = h.remote({"texts": ["health-check"], "max_length": INFERENCE_EMBEDDER_MAX_TOKENS})
                        _resolve_handle_response(resp, timeout=EMBED_TIMEOUT)
                        return h
                    except Exception as e:
                        last_exc = e
                if hasattr(serve, "get_handle"):
                    try:
                        h = serve.get_handle(name, sync=False)
                        resp = h.remote({"texts": ["health-check"], "max_length": INFERENCE_EMBEDDER_MAX_TOKENS})
                        _resolve_handle_response(resp, timeout=EMBED_TIMEOUT)
                        return h
                    except Exception as e:
                        last_exc = e
        except Exception as e:
            last_exc = e
        time.sleep(poll)

    # HTTP fallback(s)
    if httpx is None:
        raise RuntimeError(f"timed out getting handle {name}: {last_exc}")

    candidates = []
    if EMBED_HTTP_URL:
        candidates.append(EMBED_HTTP_URL.rstrip("/"))
    for port in range(8000, 8006):
        candidates.append(f"http://127.0.0.1:{port}/{name}".rstrip("/"))
        candidates.append(f"http://127.0.0.1:{port}/{name}/")
    candidates = list(dict.fromkeys(candidates))

    class HTTPHandle:
        def __init__(self, url: str, timeout: float = HTTP_TIMEOUT):
            self.url = url.rstrip("/")
            self.timeout = timeout
        def remote(self, payload: dict):
            r = httpx.post(self.url, json=payload, timeout=self.timeout)
            r.raise_for_status()
            try:
                return r.json()
            except Exception:
                return r.text

    for url in candidates:
        try:
            r = httpx.post(url, json={"texts": ["health-check"], "max_length": INFERENCE_EMBEDDER_MAX_TOKENS}, timeout=5.0)
            if r.status_code == 200:
                log.warning("Using HTTP fallback for %s -> %s", name, url)
                return HTTPHandle(url, timeout=HTTP_TIMEOUT)
        except Exception:
            continue

    raise RuntimeError(f"timed out getting handle {name}: {last_exc}")

# ---------- Clients ----------
@retry()
def make_clients() -> Tuple[QdrantClient, Any]:
    q = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY, prefer_grpc=PREFER_GRPC)
    neo = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    # simple connectivity checks
    try:
        q.get_collection(collection_name=QDRANT_COLLECTION)
        log.debug("qdrant reachable")
    except Exception:
        log.debug("qdrant ping failed (collection may not exist or remote); continuing")
    try:
        neo.verify_connectivity()
        log.debug("neo4j reachable")
    except Exception:
        log.debug("neo4j verify_connectivity failed (continuing)")
    return q, neo

# ---------- Embedding + Rerank wrappers ----------
@retry()
def embed_text(embed_handle, text: str, max_length: int = INFERENCE_EMBEDDER_MAX_TOKENS) -> List[float]:
    if embed_handle is None:
        raise RuntimeError("embed handle missing")
    payload = {"texts": [text], "max_length": int(max_length)}
    resp_obj = embed_handle.remote(payload)
    resp = _resolve_handle_response(resp_obj, timeout=EMBED_TIMEOUT)
    if not isinstance(resp, dict) or "vectors" not in resp:
        raise RuntimeError("embed returned bad payload")
    vecs = resp["vectors"]
    if not isinstance(vecs, list) or len(vecs) != 1:
        raise RuntimeError("embed returned wrong shape")
    v = vecs[0]
    if VECTOR_DIM and len(v) != VECTOR_DIM:
        log.warning("embed dim mismatch %d != %d (continuing)", len(v), VECTOR_DIM)
    return [float(x) for x in v]

@retry()
def cross_rerank(rerank_handle, query: str, texts: List[str], max_length: int = CROSS_ENCODER_MAX_TOKENS) -> List[float]:
    if rerank_handle is None:
        raise RuntimeError("rerank handle missing")
    payload = {"query": query, "cands": texts, "max_length": int(max_length)}
    resp_obj = rerank_handle.remote(payload)
    resp = _resolve_handle_response(resp_obj, timeout=EMBED_TIMEOUT)
    if not isinstance(resp, dict) or "scores" not in resp:
        raise RuntimeError("rerank returned bad payload")
    return [float(s) for s in resp["scores"]]

# ---------- Qdrant helpers ----------
@retry()
def qdrant_vector_search(client: QdrantClient, q_vec: List[float], top_k: int,
                         with_payload: bool = True, with_vectors: bool = True) -> List[Dict[str, Any]]:
    kw = {}
    if with_payload:
        kw["with_payload"] = True
    if with_vectors:
        kw["with_vectors"] = True
    results = None
    last_exc = None
    if hasattr(client, "query_points"):
        try:
            try:
                results = client.query_points(collection_name=QDRANT_COLLECTION, query_vector=q_vec, limit=top_k, **kw)
            except TypeError:
                # fallback arg name differences
                alt = {k.replace("with_vectors", "with_vector"): v for k, v in kw.items()}
                results = client.query_points(collection_name=QDRANT_COLLECTION, vector=q_vec, limit=top_k, **alt)
        except Exception as e:
            last_exc = e
            results = None
    if results is None:
        tried = [
            {"query_vector": q_vec, **kw},
            {"vector": q_vec, **kw},
            {"query_vector": q_vec, "with_payload": kw.get("with_payload", True)},
            {"vector": q_vec, "with_payload": kw.get("with_payload", True)},
        ]
        for t in tried:
            try:
                results = client.search(collection_name=QDRANT_COLLECTION, **t, limit=top_k)
                break
            except AssertionError as ae:
                last_exc = ae
                log.debug("qdrant.search assertion: %s", ae)
                continue
            except Exception as e:
                last_exc = e
                log.debug("qdrant.search error: %s", e)
                continue
    if results is None:
        raise RuntimeError(f"qdrant vector search failed: {last_exc}")

    out: List[Dict[str, Any]] = []
    missing_vectors: List[str] = []
    for r in results:
        payload = getattr(r, "payload", None) or (r.get("payload") if isinstance(r, dict) else {}) or {}
        vec = getattr(r, "vector", None) or (r.get("vector") if isinstance(r, dict) else None)
        rid = getattr(r, "id", None) or (r.get("id") if isinstance(r, dict) else None)
        score = getattr(r, "score", None) or (r.get("score") if isinstance(r, dict) else None) or 0.0
        out.append({"id": str(rid) if rid is not None else None, "score": float(score), "payload": payload or {}, "vector": vec})
        if vec is None and rid is not None:
            missing_vectors.append(str(rid))
    if missing_vectors:
        for pid in missing_vectors:
            try:
                p = client.get_point(collection_name=QDRANT_COLLECTION, id=pid, with_payload=False, with_vector=True)
                vec = getattr(p, "vector", None) or (p.get("vector") if isinstance(p, dict) else None)
                for entry in out:
                    if entry.get("id") == pid and vec is not None:
                        entry["vector"] = vec
            except Exception:
                continue
    return out

# ---------- Neo4j helpers ----------
@retry()
def neo4j_fulltext_search(driver, query: str, index_name: str = "chunkFulltextIndex", top_k: int = TOP_BM25_CHUNKS) -> List[Tuple[str, float]]:
    if not query:
        return []
    try:
        with driver.session() as s:
            try:
                s.run(f"CREATE FULLTEXT INDEX {index_name} IF NOT EXISTS FOR (c:Chunk) ON EACH [c.text]")
            except Exception:
                pass
            cy = "CALL db.index.fulltext.queryNodes($index, $q) YIELD node, score RETURN node.chunk_id AS chunk_id, score ORDER BY score DESC LIMIT $k"
            res = s.run(cy, index=index_name, q=query, k=top_k)
            out = []
            for r in res:
                try:
                    out.append((r["chunk_id"], float(r["score"] or 0.0)))
                except Exception:
                    continue
            return out
    except Neo4jError as e:
        log.warning("neo4j_fulltext_search failed: %s", e)
        return []
    except Exception as e:
        log.warning("neo4j_fulltext_search unexpected: %s", e)
        return []

@retry()
def neo4j_fetch_texts(driver, chunk_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    if not chunk_ids:
        return {}
    out = {}
    with driver.session() as s:
        cy = "MATCH (c:Chunk) WHERE c.chunk_id IN $ids RETURN c.chunk_id AS cid, c.text AS text, c.token_count AS token_count, c.document_id AS document_id"
        res = s.run(cy, ids=list(chunk_ids))
        for r in res:
            try:
                out[r["cid"]] = {"text": r.get("text") or "", "token_count": int(r.get("token_count") or 0), "document_id": r.get("document_id")}
            except Exception:
                continue
    return out

@retry()
def neo4j_expand_graph(driver, seeds: List[str], hops: int = 1, per_seed_limit: int = 5) -> List[str]:
    if not seeds:
        return []
    hops_val = int(max(1, hops))
    limit_val = int(max(1, per_seed_limit))
    cy = (
        "UNWIND $seeds AS sid\n"
        "MATCH (s:Chunk {chunk_id: sid})\n"
        "CALL {\n"
        "  WITH s\n"
        f"  MATCH (s)-[*1..{hops_val}]-(n:Chunk)\n"
        "  RETURN DISTINCT n.chunk_id AS nid LIMIT $limit\n"
        "}\n"
        "RETURN DISTINCT nid"
    )
    out = []
    with driver.session() as s:
        try:
            res = s.run(cy, seeds=list(seeds), limit=limit_val)
            for r in res:
                try:
                    out.append(r["nid"])
                except Exception:
                    continue
        except Exception:
            return []
    return list(dict.fromkeys([str(x) for x in out if x]))

# ---------- Helpers ----------
def _rrf_fuse(ranked_lists: List[List[str]], k: int = 60) -> Dict[str, float]:
    scores = {}
    for lst in ranked_lists:
        for i, it in enumerate(lst):
            rank = i + 1
            if it is None:
                continue
            scores[it] = scores.get(it, 0.0) + 1.0 / (k + rank)
    return scores

def stable_dedupe(ids: List[str]) -> List[str]:
    return list(dict.fromkeys([str(i) for i in ids if i is not None]))

def token_budget_select(candidates: List[Dict[str, Any]], max_chunks: int = MAX_CHUNKS_TO_LLM, max_tokens: int = MAX_PROMPT_TOKENS) -> List[Dict[str, Any]]:
    selected = []
    total = 0
    for c in candidates:
        if len(selected) >= max_chunks:
            break
        t = c.get("token_count") or 0
        if not t:
            t = max(1, int(len(c.get("text", "").split()) / 1.5))
        if total + t > max_tokens:
            break
        total += t
        selected.append(c)
    return selected

def cosine(a: List[float], b: List[float]) -> float:
    if not a or not b:
        return 0.0
    sa = sum(x*x for x in a)
    sb = sum(x*x for x in b)
    if sa == 0 or sb == 0:
        return 0.0
    dot = sum(x*y for x, y in zip(a, b))
    return float(dot / (math.sqrt(sa) * math.sqrt(sb)))

# ---------- Retrieval pipeline ----------
def retrieve_pipeline(embed_handle, rerank_handle, qdrant_client, neo4j_driver, query_text: str, max_chunks: int = MAX_CHUNKS_TO_LLM) -> Dict[str, Any]:
    t0 = time.time()
    vec = embed_text(embed_handle, query_text, max_length=INFERENCE_EMBEDDER_MAX_TOKENS)
    t1 = time.time()
    log.debug("embed elapsed %.3fs", t1 - t0)

    ann_hits = []
    try:
        ann_hits = qdrant_vector_search(qdrant_client, vec, TOP_VECTOR_CHUNKS, with_payload=True, with_vectors=True)
    except Exception as e:
        log.warning("qdrant_vector_search failed: %s", e)
        ann_hits = []

    vec_rank = []
    id_to_vec = {}
    for h in ann_hits:
        payload = h.get("payload") or {}
        cid = payload.get("chunk_id") or payload.get("chunkId") or h.get("id")
        if cid:
            cid = str(cid)
            vec_rank.append(cid)
            if h.get("vector"):
                id_to_vec[cid] = h.get("vector")

    bm25_list = []
    try:
        bm25_hits = neo4j_fulltext_search(neo4j_driver, query_text, top_k=TOP_BM25_CHUNKS)
        bm25_list = [cid for cid, _ in bm25_hits]
    except Exception:
        bm25_list = []

    kw_list = []
    if ENABLE_METADATA_CHUNKS:
        try:
            meta_hits = qdrant_vector_search(qdrant_client, vec, MAX_METADATA_CHUNKS, with_payload=True, with_vectors=False)
            kw_list = [str(h.get("payload", {}).get("chunk_id") or h.get("id")) for h in meta_hits if (h.get("payload") and (h.get("payload").get("chunk_id") or h.get("payload").get("chunkId"))) or h.get("id")]
        except Exception:
            kw_list = []

    ranked_lists = []
    if vec_rank:
        ranked_lists.append(vec_rank)
    if bm25_list:
        ranked_lists.append(bm25_list)
    if kw_list:
        ranked_lists.append(kw_list)
    fused_scores = _rrf_fuse(ranked_lists, k=FIRST_STAGE_RRF_K)
    fused_order = sorted(fused_scores.items(), key=lambda x: -x[1])
    fused_list = [cid for cid, _ in fused_order]
    deduped_fused = stable_dedupe(fused_list)

    seeds = deduped_fused[:MAX_CHUNKS_FOR_GRAPH_EXPANSION]
    expanded = []
    if seeds and MAX_CHUNKS_FOR_GRAPH_EXPANSION > 0 and GRAPH_EXPANSION_HOPS > 0:
        try:
            expanded = neo4j_expand_graph(neo4j_driver, seeds, hops=GRAPH_EXPANSION_HOPS, per_seed_limit=5)
        except Exception:
            expanded = []

    combined_unique_candidates = stable_dedupe(deduped_fused + expanded + vec_rank + bm25_list + (kw_list or []))
    texts_map = {}
    try:
        texts_map = neo4j_fetch_texts(neo4j_driver, combined_unique_candidates)
    except Exception:
        texts_map = {}

    candidates = []
    vec_map = {}
    for h in ann_hits:
        payload = h.get("payload") or {}
        cid = payload.get("chunk_id") or payload.get("chunkId") or h.get("id")
        if cid:
            cid = str(cid)
            vec_map[cid] = float(h.get("score", 0.0) or 0.0)
    bm25_map = {cid: sc for cid, sc in (bm25_hits or [])}

    for cid in combined_unique_candidates:
        rec_text = texts_map.get(cid, {}).get("text", "") or ""
        token_count = int(texts_map.get(cid, {}).get("token_count", 0) or 0)
        docid = texts_map.get(cid, {}).get("document_id")
        rec = {
            "chunk_id": cid,
            "text": rec_text,
            "token_count": token_count,
            "document_id": docid,
            "vector_score": vec_map.get(cid, 0.0),
            "bm25_score": float(bm25_map.get(cid, 0.0) or 0.0)
        }
        if cid in id_to_vec:
            rec["vector"] = id_to_vec[cid]
        candidates.append(rec)

    for c in candidates:
        v = c.get("vector")
        if v:
            try:
                c["vec_sim"] = cosine(vec, v)
            except Exception:
                c["vec_sim"] = c.get("vector_score", 0.0)
        else:
            c["vec_sim"] = c.get("vector_score", 0.0)

    vec_rank2 = [c["chunk_id"] for c in sorted(candidates, key=lambda x: -x.get("vec_sim", 0.0))]
    bm25_rank2 = [c["chunk_id"] for c in sorted(candidates, key=lambda x: -x.get("bm25_score", 0.0))]
    kw_rank2 = [c["chunk_id"] for c in candidates if c["chunk_id"] in (kw_list or [])] if kw_list else []
    ranked2 = []
    if vec_rank2:
        ranked2.append(vec_rank2)
    if bm25_rank2:
        ranked2.append(bm25_rank2)
    if kw_rank2:
        ranked2.append(kw_rank2)
    fused2_scores = _rrf_fuse(ranked2, k=SECOND_STAGE_RRF_K)
    final_fused_order = sorted(fused2_scores.items(), key=lambda x: -x[1])
    final_order = stable_dedupe([cid for cid, _ in final_fused_order])

    if ENABLE_CROSS_ENCODER and rerank_handle and final_order:
        top_for_x = final_order[:min(len(final_order), MAX_CHUNKS_TO_CROSSENCODER)]
        texts_for_x = [next((c["text"] for c in candidates if c["chunk_id"] == cid), "") for cid in top_for_x]
        try:
            x_scores = cross_rerank(rerank_handle, query_text, texts_for_x, max_length=CROSS_ENCODER_MAX_TOKENS)
            if x_scores and len(x_scores) == len(texts_for_x):
                merged = []
                for i, cid in enumerate(top_for_x):
                    base = fused2_scores.get(cid, 0.0)
                    combined = 0.8 * float(x_scores[i]) + 0.2 * base
                    merged.append({"chunk_id": cid, "score": combined})
                merged_sorted = sorted(merged, key=lambda x: -x["score"])
                ordered = [m["chunk_id"] for m in merged_sorted]
                remaining = [cid for cid in final_order if cid not in ordered]
                final_order = ordered + remaining
        except Exception as e:
            log.warning("cross-rerank failed: %s", e)

    final_ids = final_order[: max_chunks * 4]
    final_records = [next((c for c in candidates if c["chunk_id"] == cid), None) for cid in final_ids]
    selected = token_budget_select([c for c in final_records if c], max_chunks=max_chunks, max_tokens=MAX_PROMPT_TOKENS)

    pieces = []
    provenance = []
    for r in selected:
        pieces.append(f"CHUNK (doc={r.get('document_id')} chunk={r['chunk_id']}):\n{r['text']}")
        provenance.append({"document_id": r.get("document_id"), "chunk_id": r["chunk_id"], "score": fused2_scores.get(r["chunk_id"], r.get("vec_sim", 0.0))})

    context = "\n\n---\n\n".join(pieces)
    prompt = f"USE ONLY THE CONTEXT BELOW. Cite provenance by document_id.\n\nCONTEXT:\n{context}\n\nUSER QUERY:\n{query_text}\n"
    elapsed = time.time() - t0
    return {"prompt": prompt, "provenance": provenance, "records": selected, "llm": None, "elapsed": elapsed}

# ---------- Main ----------
def main():
    log.info("query.py starting")
    _ensure_ray_connected()
    embed_handle = None
    rerank_handle = None

    try:
        embed_handle = _get_handle(EMBED_DEPLOYMENT)
    except Exception as e:
        log.warning("embed handle not found via Serve: %s. Will attempt EMBED_HTTP_URL or local ports.", e)
        if EMBED_HTTP_URL and httpx is not None:
            try:
                class EH:
                    def __init__(self, u): self.u = u.rstrip("/")
                    def remote(self, payload):
                        r = httpx.post(self.u, json=payload, timeout=HTTP_TIMEOUT)
                        r.raise_for_status()
                        try:
                            return r.json()
                        except Exception:
                            return r.text
                h = EH(EMBED_HTTP_URL)
                _ = h.remote({"texts": ["health-check"], "max_length": INFERENCE_EMBEDDER_MAX_TOKENS})
                embed_handle = h
                log.info("Using EMBED_HTTP_URL %s", EMBED_HTTP_URL)
            except Exception:
                embed_handle = None

    try:
        rerank_handle = _get_handle(RERANK_HANDLE_NAME)
    except Exception:
        log.info("no rerank handle found; cross-encoder disabled")
        rerank_handle = None

    qdrant_client, neo4j_driver = make_clients()

    # Warm-up probes to pay TLS/DNS cost once
    try:
        if qdrant_client:
            try:
                qdrant_client.get_collection(collection_name=QDRANT_COLLECTION)
            except Exception:
                pass
    except Exception:
        pass
    try:
        if neo4j_driver:
            try:
                with neo4j_driver.session() as s:
                    s.run("RETURN 1").consume()
            except Exception:
                pass
    except Exception:
        pass

    q = "health-check"
    out = {"ok": False}
    try:
        res = retrieve_pipeline(embed_handle, rerank_handle, qdrant_client, neo4j_driver, q, max_chunks=TOP_K)
        out = {"ok": True, "provenance_count": len(res["provenance"]), "prompt_len": len(res["prompt"]), "elapsed": res.get("elapsed")}
        print(json.dumps(out, indent=2))
    except Exception as e:
        log.exception("retrieve_pipeline failed: %s", e)
        print(json.dumps({"ok": False, "error": str(e)}))
    finally:
        try:
            qdrant_client.close()
        except Exception:
            pass
        try:
            neo4j_driver.close()
        except Exception:
            pass

if __name__ == "__main__":
    main()
