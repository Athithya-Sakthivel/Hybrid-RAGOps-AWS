#!/usr/bin/env python3
# inference_pipeline/query.py
# Retrieval pipeline with dual MODE: "vector_only" or "hybrid".
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
from typing import Any, Dict, List, Optional, Tuple

# lazy ray import handling
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

# ---------- CONFIG ----------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("query")

RAY_ADDRESS = os.getenv("RAY_ADDRESS", "auto")
RAY_NAMESPACE = os.getenv("RAY_NAMESPACE", None)
SERVE_APP_NAME = os.getenv("SERVE_APP_NAME", "default")

EMBED_DEPLOYMENT = os.getenv("EMBED_DEPLOYMENT", "embed_onxx")
RERANK_DEPLOYMENT = os.getenv("RERANK_HANDLE_NAME", "rerank_onxx")

QDRANT_URL = os.getenv("QDRANT_URL", "http://127.0.0.1:6333")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY", None)
PREFER_GRPC = os.getenv("PREFER_GRPC", "true").lower() in ("1", "true", "yes")
QDRANT_COLLECTION = os.getenv("COLLECTION", "my_collection")

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "")

VECTOR_DIM = int(os.getenv("VECTOR_DIM", "768"))

# Dual-mode
MODE = os.getenv("MODE", "hybrid").lower()  # "vector_only" or "hybrid"

# Retrieval tuning / environment toggles
TOP_K = int(os.getenv("TOP_K", "5"))
TOP_VECTOR_CHUNKS = int(os.getenv("TOP_VECTOR_CHUNKS", "200"))
TOP_BM25_CHUNKS = int(os.getenv("TOP_BM25_CHUNKS", "100"))

ENABLE_METADATA_CHUNKS = os.getenv("ENABLE_METADATA_CHUNKS", "false").lower() in ("1", "true", "yes")
MAX_METADATA_CHUNKS = int(os.getenv("MAX_METADATA_CHUNKS", "50"))

FIRST_STAGE_RRF_K = int(os.getenv("FIRST_STAGE_RRF_K", "60"))
SECOND_STAGE_RRF_K = int(os.getenv("SECOND_STAGE_RRF_K", "60"))

MAX_CHUNKS_FOR_GRAPH_EXPANSION = int(os.getenv("MAX_CHUNKS_FOR_GRAPH_EXPANSION", "20"))
GRAPH_EXPANSION_HOPS = int(os.getenv("GRAPH_EXPANSION_HOPS", "1"))

ENABLE_CROSS_ENCODER = os.getenv("ENABLE_CROSS_ENCODER", "false").lower() in ("1", "true", "yes")
MAX_CHUNKS_TO_CROSSENCODER = int(os.getenv("MAX_CHUNKS_TO_CROSSENCODER", "64"))
MAX_CHUNKS_TO_LLM = int(os.getenv("MAX_CHUNKS_TO_LLM", "8"))

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
        raise RuntimeError("ray not importable")
    if not ray.is_initialized():
        ray.init(address=RAY_ADDRESS, namespace=RAY_NAMESPACE, ignore_reinit_error=True)

# Robust Serve handle caller that unwraps DeploymentResponse-like wrappers
def call_handle(handle, payload, timeout: float = EMBED_TIMEOUT):
    if handle is None:
        raise RuntimeError("handle is None")
    try:
        if hasattr(handle, "remote"):
            resp = handle.remote(payload)
        else:
            resp = handle(payload)
    except Exception as e:
        raise RuntimeError(f"serve invocation failed: {e}") from e

    # try common unwrappers
    try:
        # Ray ObjectRef
        ObjectRefType = getattr(ray, "ObjectRef", None) or getattr(ray, "_raylet.ObjectRef", None)
    except Exception:
        ObjectRefType = None
    try:
        if ObjectRefType is not None and isinstance(resp, ObjectRefType):
            return ray.get(resp, timeout=timeout)
    except Exception:
        pass

    try:
        # DeploymentResponse-like: object_refs attribute
        if hasattr(resp, "object_refs"):
            ors = getattr(resp, "object_refs")
            if isinstance(ors, list):
                if len(ors) == 1:
                    return ray.get(ors[0], timeout=timeout)
                return ray.get(ors, timeout=timeout)
    except Exception:
        pass

    try:
        # .result()
        if hasattr(resp, "result") and callable(getattr(resp, "result")):
            try:
                return resp.result(timeout=timeout)
            except TypeError:
                return resp.result()
            except Exception:
                pass
    except Exception:
        pass

    try:
        # .get()
        if hasattr(resp, "get") and callable(getattr(resp, "get")):
            try:
                return resp.get(timeout=timeout)
            except TypeError:
                return resp.get()
            except Exception:
                pass
    except Exception:
        pass

    try:
        # .body() / .get_body() / .read()
        if hasattr(resp, "body") and callable(getattr(resp, "body")):
            return resp.body()
        if hasattr(resp, "get_body") and callable(getattr(resp, "get_body")):
            return resp.get_body()
        if hasattr(resp, "read") and callable(getattr(resp, "read")):
            return resp.read()
    except Exception:
        pass

    # fallback: try ray.get (last attempt)
    try:
        return ray.get(resp, timeout=timeout)
    except Exception:
        pass

    # return as-is
    return resp

# ---------- Clients ----------
@retry()
def make_clients() -> Tuple[QdrantClient, Any]:
    q = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY, prefer_grpc=PREFER_GRPC)
    neo = None
    try:
        neo = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    except Exception:
        log.debug("neo4j driver init failed; will error later if hybrid mode requires it")
    try:
        q.get_collection(collection_name=QDRANT_COLLECTION)
        log.debug("qdrant reachable")
    except Exception:
        log.debug("qdrant get_collection failed (collection may not exist)")
    if neo:
        try:
            neo.verify_connectivity()
            log.debug("neo4j reachable")
        except Exception:
            log.debug("neo4j verify_connectivity failed")
    return q, neo

# ---------- Embedding + Rerank wrappers ----------
@retry()
def embed_text(embed_handle, text: str, max_length: int = INFERENCE_EMBEDDER_MAX_TOKENS) -> Optional[List[float]]:
    if embed_handle is None:
        raise RuntimeError("embed handle missing")
    payload = {"texts": [text], "max_length": int(max_length)}
    resp = call_handle(embed_handle, payload, timeout=EMBED_TIMEOUT)
    vecs = None
    if isinstance(resp, dict):
        vecs = resp.get("vectors") or resp.get("embeddings") or resp.get("data") or resp.get("outputs")
    elif isinstance(resp, list):
        vecs = resp
    else:
        try:
            if hasattr(resp, "get"):
                vecs = resp.get("vectors") or resp.get("embeddings") or resp.get("data")
        except Exception:
            vecs = None
    if vecs is None:
        raise RuntimeError(f"embed returned bad payload type {type(resp)}")
    if isinstance(vecs, dict):
        try:
            vecs = list(vecs.values())
        except Exception:
            pass
    if not isinstance(vecs, list) or len(vecs) < 1:
        raise RuntimeError("embed returned wrong shape")
    v = vecs[0]
    if v is None:
        raise RuntimeError("embed returned empty vector")
    if VECTOR_DIM and len(v) != VECTOR_DIM:
        log.warning("embed dim mismatch %d != %d (continuing)", len(v), VECTOR_DIM)
    return [float(x) for x in v]

@retry()
def cross_rerank(rerank_handle, query: str, texts: List[str], max_length: int = CROSS_ENCODER_MAX_TOKENS) -> List[float]:
    if rerank_handle is None:
        raise RuntimeError("rerank handle missing")
    payload = {"query": query, "cands": texts, "max_length": int(max_length)}
    resp = call_handle(rerank_handle, payload, timeout=EMBED_TIMEOUT)
    scores = None
    if isinstance(resp, dict):
        scores = resp.get("scores") or resp.get("scores_list")
    elif isinstance(resp, list):
        scores = resp
    else:
        try:
            if hasattr(resp, "get"):
                scores = resp.get("scores")
        except Exception:
            scores = None
    if not isinstance(scores, list) or len(scores) != len(texts):
        raise RuntimeError("rerank returned bad payload")
    return [float(s) for s in scores]

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
    # try common query APIs
    try:
        # newer client: query_points
        if hasattr(client, "query_points"):
            try:
                results = client.query_points(collection_name=QDRANT_COLLECTION, query_vector=q_vec, limit=top_k, **kw)
            except TypeError:
                results = client.query_points(collection_name=QDRANT_COLLECTION, vector=q_vec, limit=top_k, **kw)
    except Exception as e:
        last_exc = e
        results = None
    if results is None:
        tried = [
            {"query_vector": q_vec, **kw},
            {"vector": q_vec, **kw},
        ]
        for t in tried:
            try:
                results = client.search(collection_name=QDRANT_COLLECTION, **t, limit=top_k)
                break
            except Exception as e:
                last_exc = e
                continue
    if results is None:
        raise RuntimeError(f"qdrant vector search failed: {last_exc}")
    out: List[Dict[str, Any]] = []
    missing_vectors = []
    for r in results:
        payload = getattr(r, "payload", None) or (r.get("payload") if isinstance(r, dict) else {}) or {}
        vec = getattr(r, "vector", None) or (r.get("vector") if isinstance(r, dict) else None)
        rid = getattr(r, "id", None) or (r.get("id") if isinstance(r, dict) else None)
        score = getattr(r, "score", None) or (r.get("score") if isinstance(r, dict) else None) or 0.0
        out.append({"id": str(rid) if rid is not None else None, "score": float(score), "payload": payload or {}, "vector": vec})
        if vec is None and rid is not None:
            missing_vectors.append(str(rid))
    # try to fetch missing vectors
    if missing_vectors:
        try:
            for pid in missing_vectors:
                try:
                    p = client.get_point(collection_name=QDRANT_COLLECTION, id=pid, with_payload=False, with_vector=True)
                    vec = getattr(p, "vector", None) or (p.get("vector") if isinstance(p, dict) else None)
                    for entry in out:
                        if entry.get("id") == pid and vec is not None:
                            entry["vector"] = vec
                except Exception:
                    continue
        except Exception:
            pass
    return out

@retry()
def qdrant_payload_search(client: QdrantClient, keyword: str, max_results: int = 50) -> List[Tuple[str, float]]:
    """
    Best-effort metadata/payload keyword search: scroll through stored payloads and
    do substring match on common payload fields. Returns list of (chunk_id, score)
    where score is a simple length-normalized match count. This is slower but robust
    against differences in Qdrant server API versions.
    """
    if not keyword:
        return []
    kw = keyword.lower()
    out = []
    try:
        # use scroll to iterate payloads; stop after max_results matches or exhausted
        limit = 1000
        offset = 0
        while True:
            try:
                page = client.scroll(collection_name=QDRANT_COLLECTION, limit=limit, offset=offset)
            except TypeError:
                # some clients use different arg names
                page = client.scroll(collection_name=QDRANT_COLLECTION, limit=limit)
            if not page:
                break
            for p in page:
                payload = getattr(p, "payload", None) or (p.get("payload") if isinstance(p, dict) else {}) or {}
                pid = getattr(p, "id", None) or (p.get("id") if isinstance(p, dict) else None)
                if not pid:
                    continue
                # check common fields
                match_score = 0
                for fld in ("snippet", "text", "file_name", "source_url", "headings", "headings_path"):
                    val = payload.get(fld) if isinstance(payload, dict) else None
                    if not val:
                        continue
                    s = str(val).lower()
                    if kw in s:
                        match_score += s.count(kw)
                if match_score > 0:
                    out.append((str(pid), float(match_score)))
                    if len(out) >= max_results:
                        break
            if len(out) >= max_results:
                break
            # if page is generator/list with length < limit, we are done
            try:
                if len(page) < limit:
                    break
            except Exception:
                pass
            offset += limit
    except Exception:
        log.warning("qdrant_payload_search failed; returning what we have")
    # convert point ids to chunk_ids if payload has chunk_id inside - attempt best-effort mapping
    mapped = []
    for pid, sc in out:
        try:
            p = client.get_point(collection_name=QDRANT_COLLECTION, id=pid, with_payload=True, with_vector=False)
            payload = getattr(p, "payload", None) or (p.get("payload") if isinstance(p, dict) else {}) or {}
            cid = payload.get("chunk_id") or payload.get("chunkId") or pid
            mapped.append((str(cid), float(sc)))
        except Exception:
            mapped.append((pid, float(sc)))
    # merge scores for duplicate chunk_ids
    agg = {}
    for cid, sc in mapped:
        agg[cid] = agg.get(cid, 0.0) + sc
    ranked = sorted(agg.items(), key=lambda x: -x[1])
    return ranked[:max_results]

@retry()
def qdrant_fetch_point_payload(client: QdrantClient, point_id: str, with_vector: bool = False) -> Dict[str, Any]:
    try:
        p = client.get_point(collection_name=QDRANT_COLLECTION, id=point_id, with_payload=True, with_vector=with_vector)
        payload = getattr(p, "payload", None) or (p.get("payload") if isinstance(p, dict) else {}) or {}
        vec = getattr(p, "vector", None) or (p.get("vector") if isinstance(p, dict) else None)
        return {"id": str(point_id), "payload": payload, "vector": vec}
    except Exception:
        return {"id": str(point_id), "payload": {}, "vector": None}

# ---------- Neo4j helpers ----------
@retry()
def neo4j_fulltext_search(driver, query: str, index_name: str = "chunkFulltextIndex", top_k: int = TOP_BM25_CHUNKS) -> List[Tuple[str, float]]:
    if not query or driver is None:
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
    """
    Fetch the full stored metadata for each chunk id from Neo4j.
    Returns mapping chunk_id -> fields including the user-requested provenance fields.
    """
    out = {}
    if not chunk_ids or driver is None:
        return out
    with driver.session() as s:
        cy = (
            "MATCH (c:Chunk) WHERE c.chunk_id IN $ids "
            "RETURN c.chunk_id AS cid, c.text AS text, c.token_count AS token_count, "
            "c.document_id AS document_id, c.source_url AS source_url, c.file_name AS file_name, "
            "c.page_number AS page_number, c.row_range AS row_range, c.token_range AS token_range, "
            "c.audio_range AS audio_range, c.headings AS headings, c.headings_path AS headings_path"
        )
        res = s.run(cy, ids=list(chunk_ids))
        for r in res:
            try:
                out[r["cid"]] = {
                    "text": r.get("text") or "",
                    "token_count": int(r.get("token_count") or 0),
                    "document_id": r.get("document_id"),
                    "source_url": r.get("source_url"),
                    "file_name": r.get("file_name"),
                    "page_number": r.get("page_number"),
                    "row_range": r.get("row_range"),
                    "token_range": r.get("token_range"),
                    "audio_range": r.get("audio_range"),
                    "headings": r.get("headings"),
                    "headings_path": r.get("headings_path"),
                }
            except Exception:
                continue
    return out

@retry()
def neo4j_expand_graph(driver, seeds: List[str], hops: int = 1, per_seed_limit: int = 5) -> List[str]:
    if not seeds or driver is None:
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

# ---------- Ranking helpers ----------
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
    vec = None
    try:
        vec = embed_text(embed_handle, query_text, max_length=INFERENCE_EMBEDDER_MAX_TOKENS)
    except Exception as e:
        log.warning("embed failed, falling back to BM25/metadata-only retrieval: %s", e)
        vec = None

    # ANN window (if vector available)
    ann_hits = []
    if vec is not None:
        try:
            ann_hits = qdrant_vector_search(qdrant_client, vec, TOP_VECTOR_CHUNKS, with_payload=True, with_vectors=True)
        except Exception as e:
            log.warning("qdrant_vector_search failed: %s", e)
            ann_hits = []
    else:
        log.info("Skipping vector search (no embed available)")

    # build vec_rank from ann_hits: map payload chunk_id or fallback to point id
    vec_rank = []
    id_to_vec = {}
    point_to_chunk = {}
    for h in ann_hits:
        payload = h.get("payload") or {}
        point_id = h.get("id")
        cid = payload.get("chunk_id") or payload.get("chunkId") or point_id
        if cid:
            cid = str(cid)
            vec_rank.append(cid)
            point_to_chunk[point_id] = cid
            if h.get("vector"):
                id_to_vec[cid] = h.get("vector")

    # BM25 (if hybrid and neo4j available)
    bm25_list = []
    bm25_hits = []
    if MODE != "vector_only" and neo4j_driver is not None:
        try:
            bm25_hits = neo4j_fulltext_search(neo4j_driver, query_text, top_k=TOP_BM25_CHUNKS)
            bm25_list = [cid for cid, _ in bm25_hits]
        except Exception:
            bm25_list = []
    else:
        bm25_list = []

    # metadata/payload keyword search (optional)
    kw_list = []
    if ENABLE_METADATA_CHUNKS and qdrant_client is not None:
        try:
            meta_hits = qdrant_payload_search(qdrant_client, query_text, max_results=MAX_METADATA_CHUNKS)
            kw_list = [cid for cid, _ in meta_hits]
        except Exception:
            kw_list = []

    # FIRST-STAGE RRF
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

    # seeds -> graph expansion (hybrid only)
    seeds = deduped_fused[:MAX_CHUNKS_FOR_GRAPH_EXPANSION]
    expanded = []
    if MODE != "vector_only" and neo4j_driver is not None and seeds:
        try:
            expanded = neo4j_expand_graph(neo4j_driver, seeds, hops=GRAPH_EXPANSION_HOPS, per_seed_limit=5)
        except Exception:
            expanded = []

    combined_unique_candidates = stable_dedupe(deduped_fused + expanded + vec_rank + bm25_list)

    # assemble candidate details
    texts_map = {}
    qvec_map = {}
    # fetch from neo4j when hybrid for full metadata
    if MODE != "vector_only" and neo4j_driver is not None:
        try:
            texts_map = neo4j_fetch_texts(neo4j_driver, combined_unique_candidates)
        except Exception:
            texts_map = {}

    # For vector_only or fallback, try to fetch payloads from Qdrant (point ids -> chunk ids mapping) and extract snippet/payload
    if MODE == "vector_only" or not texts_map:
        # we need to map from chunk_id back to point id if possible. Try scanning ann_hits payloads
        # build mapping payload.chunk_id -> payload fields
        for h in ann_hits:
            payload = h.get("payload") or {}
            point_id = h.get("id")
            cid = payload.get("chunk_id") or payload.get("chunkId") or point_id
            if cid:
                cid = str(cid)
                entry = texts_map.get(cid, {})
                # fill fields from payload where available
                entry.setdefault("text", payload.get("snippet") or payload.get("text") or "")
                entry.setdefault("token_count", int(payload.get("token_count") or entry.get("token_count") or 0))
                entry.setdefault("document_id", payload.get("document_id"))
                entry.setdefault("source_url", payload.get("source_url"))
                entry.setdefault("file_name", payload.get("file_name"))
                entry.setdefault("page_number", payload.get("page_number"))
                entry.setdefault("row_range", payload.get("row_range"))
                entry.setdefault("token_range", payload.get("token_range"))
                entry.setdefault("audio_range", payload.get("audio_range"))
                entry.setdefault("headings", payload.get("headings"))
                entry.setdefault("headings_path", payload.get("headings_path"))
                texts_map[cid] = entry

    # fetch vectors for combined candidates (when available) from qdrant by looking up points via payloads
    # We'll attempt to find a point id for each chunk by scanning payloads; fallback: try deterministic mapping not possible here.
    # We'll loop ann_hits to fill id_to_vec already; for any candidate missing vector, try get_point by payload chunk_id match
    for cid in combined_unique_candidates:
        if cid in id_to_vec:
            qvec_map[cid] = id_to_vec[cid]
            continue
    # best-effort: for remaining candidates, attempt to fetch point by chunk_id via qdrant scroll/get (may be slow)
    try:
        missing = [cid for cid in combined_unique_candidates if cid not in qvec_map]
        if missing and qdrant_client is not None:
            # iterate scroll and try to match payload.chunk_id
            limit = 1000
            offset = 0
            found = 0
            while missing and found < len(missing):
                try:
                    page = qdrant_client.scroll(collection_name=QDRANT_COLLECTION, limit=limit, offset=offset)
                except TypeError:
                    page = qdrant_client.scroll(collection_name=QDRANT_COLLECTION, limit=limit)
                if not page:
                    break
                for p in page:
                    payload = getattr(p, "payload", None) or (p.get("payload") if isinstance(p, dict) else {}) or {}
                    pid = getattr(p, "id", None) or (p.get("id") if isinstance(p, dict) else None)
                    if not pid:
                        continue
                    cid_p = payload.get("chunk_id") or payload.get("chunkId")
                    if cid_p and str(cid_p) in missing:
                        vec = getattr(p, "vector", None) or (p.get("vector") if isinstance(p, dict) else None)
                        if vec:
                            qvec_map[str(cid_p)] = vec
                            missing.remove(str(cid_p))
                try:
                    if len(page) < limit:
                        break
                except Exception:
                    break
                offset += limit
    except Exception:
        pass

    # Build candidate objects
    bm25_map = {cid: sc for cid, sc in (bm25_hits or [])}
    kw_map = {}
    if ENABLE_METADATA_CHUNKS:
        try:
            mhits = qdrant_payload_search(qdrant_client, query_text, max_results=MAX_METADATA_CHUNKS)
            kw_map = {cid: float(sc) for cid, sc in mhits}
        except Exception:
            kw_map = {}

    candidates = []
    for cid in combined_unique_candidates:
        info = texts_map.get(cid, {})
        rec_text = info.get("text", "") or ""
        token_count = int(info.get("token_count", 0) or 0)
        rec = {
            "chunk_id": cid,
            "text": rec_text,
            "token_count": token_count,
            "document_id": info.get("document_id"),
            "source_url": info.get("source_url"),
            "file_name": info.get("file_name"),
            "page_number": info.get("page_number"),
            "row_range": info.get("row_range"),
            "token_range": info.get("token_range"),
            "audio_range": info.get("audio_range"),
            "headings": info.get("headings"),
            "headings_path": info.get("headings_path"),
            "vector_score": 0.0,
            "bm25_score": float(bm25_map.get(cid, 0.0) or 0.0),
            "kw_score": float(kw_map.get(cid, 0.0) or 0.0),
        }
        if cid in qvec_map:
            rec["vector"] = qvec_map[cid]
            # compute similarity to query vector if available
            if vec is not None and isinstance(vec, list) and isinstance(qvec_map[cid], list) and len(vec) == len(qvec_map[cid]):
                try:
                    rec["vec_sim"] = cosine(vec, qvec_map[cid])
                except Exception:
                    rec["vec_sim"] = 0.0
            else:
                rec["vec_sim"] = float(0.0)
        else:
            rec["vec_sim"] = 0.0
        candidates.append(rec)

    # second-stage fusion
    vec_rank2 = [c["chunk_id"] for c in sorted(candidates, key=lambda x: -x.get("vec_sim", 0.0))]
    bm25_rank2 = [c["chunk_id"] for c in sorted(candidates, key=lambda x: -x.get("bm25_score", 0.0))]
    kw_rank2 = [c["chunk_id"] for c in sorted(candidates, key=lambda x: -x.get("kw_score", 0.0))] if ENABLE_METADATA_CHUNKS else []

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

    # optional cross-encoder
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

    # final selection by token budget
    final_ids = final_order[: max_chunks * 4]
    final_records_list = [next((c for c in candidates if c["chunk_id"] == cid), None) for cid in final_ids]
    selected = token_budget_select([c for c in final_records_list if c], max_chunks=max_chunks, max_tokens=MAX_PROMPT_TOKENS)

    # prepare prompt pieces and final records with requested fields
    pieces = []
    provenance = []
    records_out = []
    for r in selected:
        pieces.append(f"CHUNK (doc={r.get('document_id')} chunk={r['chunk_id']}):\n{r['text']}")
        provenance.append({"document_id": r.get("document_id"), "chunk_id": r["chunk_id"], "score": fused2_scores.get(r["chunk_id"], r.get("vec_sim", 0.0))})
        rec = {
            "chunk_id": r["chunk_id"],
            "text": r.get("text"),
            "source_url": r.get("source_url"),
            "file_name": r.get("file_name"),
            "page_number": r.get("page_number"),
            "row_range": r.get("row_range"),
            "token_range": r.get("token_range"),
            "audio_range": r.get("audio_range"),
            "headings": r.get("headings"),
            "headings_path": r.get("headings_path"),
            "token_count": r.get("token_count"),
            "document_id": r.get("document_id"),
        }
        records_out.append(rec)

    context = "\n\n---\n\n".join(pieces)
    prompt = f"USE ONLY THE CONTEXT BELOW. Cite provenance by document_id.\n\nCONTEXT:\n{context}\n\nUSER QUERY:\n{query_text}\n"
    elapsed = time.time() - t0
    return {"prompt": prompt, "provenance": provenance, "records": records_out, "llm": None, "elapsed": elapsed}

# ---------- Serve handle helpers ----------
def get_strict_handle(name: str, timeout: float = 30.0, poll: float = 0.5, app_name: Optional[str] = SERVE_APP_NAME):
    start = time.time()
    last_exc = None
    _ensure_ray_connected()
    if serve is None:
        raise RuntimeError("ray.serve not importable")
    while time.time() - start < timeout:
        try:
            if hasattr(serve, "get_deployment_handle"):
                try:
                    handle = serve.get_deployment_handle(name, app_name=app_name, _check_exists=False)
                except TypeError:
                    handle = serve.get_deployment_handle(name, _check_exists=False)
                try:
                    _ = call_handle(handle, {"texts": ["__health_check__"], "max_length": INFERENCE_EMBEDDER_MAX_TOKENS}, timeout=EMBED_TIMEOUT)
                    log.info("Resolved serve handle %s via get_deployment_handle", name)
                    return handle
                except Exception as e:
                    last_exc = e
            if hasattr(serve, "get_handle"):
                try:
                    handle = serve.get_handle(name, sync=False)
                    try:
                        _ = call_handle(handle, {"texts": ["__health_check__"], "max_length": INFERENCE_EMBEDDER_MAX_TOKENS}, timeout=EMBED_TIMEOUT)
                        log.info("Resolved serve handle %s via get_handle", name)
                        return handle
                    except Exception as e:
                        last_exc = e
                except Exception as e:
                    last_exc = e
            if hasattr(serve, "get_deployment"):
                try:
                    dep = serve.get_deployment(name)
                    handle = dep.get_handle(sync=False)
                    try:
                        _ = call_handle(handle, {"texts": ["__health_check__"], "max_length": INFERENCE_EMBEDDER_MAX_TOKENS}, timeout=EMBED_TIMEOUT)
                        log.info("Resolved serve handle %s via get_deployment", name)
                        return handle
                    except Exception as e:
                        last_exc = e
                except Exception as e:
                    last_exc = e
        except Exception as e:
            last_exc = e
        time.sleep(poll)
    raise RuntimeError(f"timed out getting serve handle '{name}': {last_exc}")

# ---------- Main ----------
def main():
    log.info("query.py starting (handle-only mode) MODE=%s", MODE)
    _ensure_ray_connected()

    embed_handle = None
    rerank_handle = None
    try:
        embed_handle = get_strict_handle(EMBED_DEPLOYMENT, timeout=30.0, app_name=SERVE_APP_NAME)
    except Exception as e:
        log.exception("embed handle resolution failed: %s", e)
        raise SystemExit(1)

    if ENABLE_CROSS_ENCODER:
        try:
            rerank_handle = get_strict_handle(RERANK_DEPLOYMENT, timeout=30.0, app_name=SERVE_APP_NAME)
        except Exception as e:
            log.exception("rerank handle resolution failed: %s", e)
            log.warning("Continuing without reranker")
            rerank_handle = None

    qdrant_client, neo4j_driver = make_clients()
    q = "health-check"
    try:
        res = retrieve_pipeline(embed_handle, rerank_handle, qdrant_client, neo4j_driver, q, max_chunks=TOP_K)
        print(json.dumps(res, default=str, indent=2))
    except Exception as e:
        log.exception("retrieve_pipeline failed: %s", e)
        print(json.dumps({"ok": False, "error": str(e)}, indent=2))
    finally:
        try:
            qdrant_client.close()
        except Exception:
            pass
        try:
            if neo4j_driver:
                neo4j_driver.close()
        except Exception:
            pass

if __name__ == "__main__":
    main()
