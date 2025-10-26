#!/usr/bin/env python3
from __future__ import annotations
import os
import time
import json
import math
import logging
from typing import List, Dict, Any, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

# Basic logging
logging.basicConfig(level=os.getenv("LOG_LEVEL", "DEBUG"))
log = logging.getLogger("query")

# Config (tune for interactive runs)
RAY_ADDRESS = os.getenv("RAY_ADDRESS", "auto")
RAY_NAMESPACE = os.getenv("RAY_NAMESPACE", None)
EMBED_HANDLE_NAME = os.getenv("EMBED_DEPLOYMENT", "embed_onxx")
RERANK_HANDLE_NAME = os.getenv("RERANK_HANDLE_NAME", "rerank_onxx")
QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY") or None
PREFER_GRPC = os.getenv("PREFER_GRPC", "true").lower() in ("1", "true", "yes")
QDRANT_COLLECTION = os.getenv("COLLECTION", "my_collection")
NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "neo4j")
NEO4J_FULLTEXT_INDEX = os.getenv("NEO4J_FULLTEXT_INDEX", "chunkTextIndex")

VECTOR_DIM = int(os.getenv("VECTOR_DIM", "768"))
TOP_K = int(os.getenv("TOP_K", "5"))
TOP_VECTOR_CHUNKS = int(os.getenv("TOP_VECTOR_CHUNKS", "100"))
TOP_BM25_CHUNKS = int(os.getenv("TOP_BM25_CHUNKS", "100"))
FIRST_STAGE_RRF_K = int(os.getenv("FIRST_STAGE_RRF_K", "60"))
MAX_CHUNKS_FOR_GRAPH_EXPANSION = int(os.getenv("MAX_CHUNKS_FOR_GRAPH_EXPANSION", "20"))
GRAPH_EXPANSION_HOPS = int(os.getenv("GRAPH_EXPANSION_HOPS", "1"))
SECOND_STAGE_RRF_K = int(os.getenv("SECOND_STAGE_RRF_K", "60"))
MAX_CHUNKS_TO_CROSSENCODER = int(os.getenv("MAX_CHUNKS_TO_CROSSENCODER", "64"))
MAX_CHUNKS_TO_LLM = int(os.getenv("MAX_CHUNKS_TO_LLM", "8"))
INFERENCE_EMBEDDER_MAX_TOKENS = int(os.getenv("INFERENCE_EMBEDDER_MAX_TOKENS", "64"))
CROSS_ENCODER_MAX_TOKENS = int(os.getenv("CROSS_ENCODER_MAX_TOKENS", "600"))
ENABLE_METADATA_CHUNKS = os.getenv("ENABLE_METADATA_CHUNKS", "false").lower() in ("1", "true", "yes")
MAX_METADATA_CHUNKS = int(os.getenv("MAX_METADATA_CHUNKS", "50"))
ENABLE_CROSS_ENCODER = os.getenv("ENABLE_CROSS_ENCODER", "true").lower() in ("1", "true", "yes")
RERANK_TOP = int(os.getenv("RERANK_TOP", "50"))

HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "8"))
RETRY_ATTEMPTS = int(os.getenv("RETRY_ATTEMPTS", "2"))
RETRY_BASE = float(os.getenv("RETRY_BASE_SECONDS", "0.25"))

# Concurrency for qdrant fetches
QDRANT_FETCH_WORKERS = int(os.getenv("QDRANT_FETCH_WORKERS", "12"))
QDRANT_FETCH_TIMEOUT = float(os.getenv("QDRANT_FETCH_TIMEOUT", "5.0"))

# Defensive imports
try:
    from qdrant_client import QdrantClient
    from qdrant_client.models import Filter, FieldCondition, MatchValue, MatchText
except Exception as e:
    raise RuntimeError("qdrant-client import failed: " + str(e))

try:
    from neo4j import GraphDatabase, exceptions as neo4j_exceptions
except Exception as e:
    raise RuntimeError("neo4j driver import failed: " + str(e))

# Ray Serve handles lazy resolved
try:
    import ray
    from ray import serve
except Exception:
    ray = None
    serve = None

# Simple retry decorator
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
            log.error("all retries failed for %s: %s", fn.__name__, last)
            raise last
        return wrapper
    return deco

# Create clients
@retry()
def make_clients():
    q = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY, prefer_grpc=PREFER_GRPC)
    drv = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    return q, drv

qdrant, neo4j_driver = make_clients()

# Resolve various serve handle response shapes robustly
def _resolve_handle_response(resp_obj, timeout: int = int(HTTP_TIMEOUT)):
    if isinstance(resp_obj, (dict, list)):
        return resp_obj
    if ray is not None:
        try:
            ObjectRefType = getattr(ray, "ObjectRef", None) or getattr(ray, "_raylet.ObjectRef", None)
        except Exception:
            ObjectRefType = None
    else:
        ObjectRefType = None
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
        if ray is not None:
            return ray.get(resp_obj, timeout=timeout)
    except Exception as e:
        raise RuntimeError("Unable to resolve Serve handle response type: " + str(type(resp_obj)) + " -> " + str(e))
    raise RuntimeError("Unknown response object type: " + str(type(resp_obj)))

# Get serve handle with fallback to HTTP post if necessary
def _get_handle(name: str, timeout: float = 60.0, poll: float = 1.0, app_name: Optional[str] = "default"):
    if serve is not None:
        start = time.time()
        last_exc = None
        while time.time() - start < timeout:
            try:
                if hasattr(serve, "get_deployment_handle"):
                    handle = serve.get_deployment_handle(name, app_name=app_name, _check_exists=False)
                elif hasattr(serve, "get_deployment"):
                    dep = serve.get_deployment(name)
                    handle = dep.get_handle(sync=False)
                elif hasattr(serve, "get_handle"):
                    handle = serve.get_handle(name, sync=False)
                else:
                    raise RuntimeError("No serve API available")
                # health-check call
                resp = handle.remote({"texts": ["health-check"]})
                _resolve_handle_response(resp, timeout=int(HTTP_TIMEOUT))
                return handle
            except Exception as e:
                last_exc = e
                time.sleep(poll)
        log.warning("serve handle get timed out: %s", last_exc)

    # HTTP fallback local
    try:
        import httpx as _httpx
        r = _httpx.post(f"http://127.0.0.1:8000/{name}", json={"texts": ["health-check"]}, timeout=10.0)
        if r.status_code == 200:
            log.warning("Using HTTP fallback for Serve deployment %s", name)
            class _HTTPHandle:
                def __init__(self, url):
                    self.url = url
                def remote(self, payload):
                    rr = _httpx.post(self.url, json=payload, timeout=int(HTTP_TIMEOUT))
                    rr.raise_for_status()
                    return rr.json()
            return _HTTPHandle(f"http://127.0.0.1:8000/{name}")
    except Exception:
        pass
    raise RuntimeError(f"Timed out waiting for Serve deployment {name}")

# Acquire embed and rerank handles (best-effort)
embed_handle = None
rerank_handle = None
try:
    if ray is not None:
        ray.init(address=RAY_ADDRESS, namespace=RAY_NAMESPACE, ignore_reinit_error=True)
    embed_handle = _get_handle(EMBED_HANDLE_NAME)
    try:
        rerank_handle = _get_handle(RERANK_HANDLE_NAME)
    except Exception:
        log.warning("No rerank handle available, cross-encoder will be skipped")
except Exception as e:
    log.warning("Ray/Serve init or handles failed: %s", e)

# Helpers
def _minmax_norm(xs: List[float]) -> List[float]:
    if not xs:
        return []
    vals = [float(x) if x is not None else 0.0 for x in xs]
    mn, mx = min(vals), max(vals)
    if mx <= mn:
        return [0.0 for _ in vals]
    return [(v - mn) / (mx - mn) for v in vals]

@retry()
def embed_text(text: str, max_length: int = INFERENCE_EMBEDDER_MAX_TOKENS) -> List[float]:
    if embed_handle is None:
        raise RuntimeError("embed handle not available")
    resp_obj = embed_handle.remote({"texts": [text], "max_length": int(max_length)})
    resp = _resolve_handle_response(resp_obj, timeout=int(HTTP_TIMEOUT))
    if not isinstance(resp, dict) or "vectors" not in resp:
        raise RuntimeError("embed returned bad payload")
    vecs = resp["vectors"]
    if not isinstance(vecs, list) or len(vecs) != 1:
        raise RuntimeError("embed must return single vector")
    v = vecs[0]
    if len(v) != VECTOR_DIM:
        raise RuntimeError("embed dim mismatch")
    return [float(x) for x in v]

@retry()
def cross_rerank(query: str, texts: List[str], max_length: int = CROSS_ENCODER_MAX_TOKENS) -> List[float]:
    if not rerank_handle:
        raise RuntimeError("rerank handle not available")
    if not texts:
        return []
    payload = {"query": query, "cands": texts, "max_length": int(max_length)}
    resp_obj = rerank_handle.remote(payload)
    resp = _resolve_handle_response(resp_obj, timeout=int(HTTP_TIMEOUT))
    if not isinstance(resp, dict) or "scores" not in resp:
        raise RuntimeError("rerank returned bad payload")
    scores = resp["scores"]
    if len(scores) != len(texts):
        raise RuntimeError("rerank returned wrong number of scores")
    return [float(s) for s in scores]

@retry()
def qdrant_vector_search(q_vec: List[float], top_k: int) -> List[Dict[str, Any]]:
    try:
        results = qdrant.search(collection_name=QDRANT_COLLECTION, query_vector=q_vec, limit=top_k, with_payload=True, with_vector=True)
    except Exception as e:
        log.warning("qdrant vector search failed: %s", e)
        return []
    out = []
    for r in results:
        payload = getattr(r, "payload", {}) or {}
        vector = getattr(r, "vector", None)
        out.append({"id": str(getattr(r, "id", None)), "score": float(getattr(r, "score", 0.0) or 0.0), "payload": payload, "vector": vector})
    return out

@retry()
def qdrant_payload_keyword_search(q: str, top_k: int) -> List[Dict[str, Any]]:
    try:
        flt = Filter(must=[FieldCondition(key="snippet", match=MatchText(text=q))])
        results = qdrant.search(collection_name=QDRANT_COLLECTION, query_vector=None, limit=top_k, with_payload=True, with_vector=False, query_filter=flt)
    except Exception as e:
        log.debug("qdrant payload keyword search failed: %s", e)
        return []
    out = []
    for r in results:
        payload = getattr(r, "payload", {}) or {}
        out.append({"id": str(getattr(r, "id", None)), "score": float(getattr(r, "score", 0.0) or 0.0), "payload": payload})
    return out

@retry()
def ensure_neo4j_fulltext_index(driver, index_name: str = NEO4J_FULLTEXT_INDEX):
    with driver.session() as session:
        try:
            res = session.run("CALL db.index.fulltext.list() YIELD name RETURN collect(name) AS idxs")
            rec = res.single()
            names = rec["idxs"] if rec else []
            if index_name in names:
                return
        except Exception:
            pass
        try:
            session.run(f"CALL db.index.fulltext.createNodeIndex('{index_name}', ['Chunk'], ['text'])")
            log.info("Created Neo4j fulltext index %s", index_name)
        except Exception as e:
            log.debug("Could not create fulltext index (maybe exists): %s", e)

@retry()
def neo4j_fulltext_search(driver, q: str, top_k: int) -> List[Dict[str, Any]]:
    ensure_neo4j_fulltext_index(driver, NEO4J_FULLTEXT_INDEX)
    out = []
    with driver.session() as session:
        try:
            cy = (
                f"CALL db.index.fulltext.queryNodes('{NEO4J_FULLTEXT_INDEX}', $q) "
                "YIELD node, score RETURN node.chunk_id AS chunk_id, node.text AS text, score "
                "ORDER BY score DESC LIMIT $limit"
            )
            res = session.run(cy, q=q, limit=top_k)
            for r in res:
                out.append({"chunk_id": r["chunk_id"], "text": r["text"], "score": float(r["score"] or 0.0)})
        except neo4j_exceptions.ClientError as e:
            log.warning("Neo4j fulltext query failed: %s", e)
        except Exception as e:
            log.warning("Neo4j fulltext query unexpected error: %s", e)
    return out

@retry()
def neo4j_fetch_texts(driver, chunk_ids: List[str]) -> Dict[str, str]:
    if not chunk_ids:
        return {}
    mapping: Dict[str, str] = {}
    with driver.session() as session:
        try:
            res = session.run("MATCH (c:Chunk) WHERE c.chunk_id IN $ids RETURN c.chunk_id AS cid, c.text AS text", ids=list(chunk_ids))
            for r in res:
                try:
                    cid = r["cid"]
                    txt = r["text"] or ""
                    mapping[cid] = txt
                except Exception:
                    pass
        except Exception as e:
            log.debug("neo4j_fetch_texts failed: %s", e)
    return mapping

@retry()
def neo4j_expand_neighbors(driver, chunk_ids: List[str], hops: int, max_additional_per_start: int) -> List[Dict[str, Any]]:
    if not chunk_ids:
        return []
    collected = []
    unique_start = list(dict.fromkeys(chunk_ids))
    with driver.session() as session:
        for cid in unique_start:
            try:
                cy = (
                    "MATCH (start:Chunk {chunk_id:$cid})<-[:HAS_CHUNK]-(d:Document)-[:HAS_CHUNK]->(n:Chunk) "
                    "RETURN DISTINCT n.chunk_id AS chunk_id, n.text AS text LIMIT $lim"
                )
                res = session.run(cy, cid=cid, lim=max_additional_per_start)
            except Exception:
                res = []
            for r in res:
                try:
                    cid2 = r["chunk_id"]
                    txt = r["text"]
                    if cid2 and cid2 != cid:
                        collected.append({"chunk_id": cid2, "text": txt})
                except Exception:
                    pass
    return collected

def _rrf_fuse(ranked_lists: List[List[Any]], k: int) -> Dict[Any, float]:
    scores: Dict[Any, float] = {}
    for lst in ranked_lists:
        for i, it in enumerate(lst):
            rank = i + 1
            key = it if isinstance(it, (str, int)) else (it.get("chunk_id") or it.get("id"))
            if key is None:
                continue
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
    return scores

# Parallelized qdrant fetch helper (major latency reduction)
def _fetch_qdrant_by_chunk_id_one(chunk_id: str) -> Optional[Dict[str, Any]]:
    try:
        flt = Filter(must=[FieldCondition(key="chunk_id", match=MatchValue(value=chunk_id))])
        res = qdrant.search(collection_name=QDRANT_COLLECTION, query_vector=None, limit=1, with_payload=True, with_vector=True, query_filter=flt)
        if res:
            r = res[0]
            payload = getattr(r, "payload", {}) or {}
            vector = getattr(r, "vector", None)
            return {"id": str(getattr(r, "id", None)), "payload": payload, "vector": vector, "score": float(getattr(r, "score", 0.0) or 0.0)}
    except Exception as e:
        log.debug("qdrant fetch chunk_id=%s failed: %s", chunk_id, e)
    return None

def _assemble_records_from_ids(chunk_ids: List[str], max_workers: int = QDRANT_FETCH_WORKERS, per_fetch_timeout: float = QDRANT_FETCH_TIMEOUT) -> List[Dict[str, Any]]:
    if not chunk_ids:
        return []
    unique = []
    seen = set()
    for cid in chunk_ids:
        if cid and cid not in seen:
            seen.add(cid)
            unique.append(cid)

    results: Dict[str, Dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(_fetch_qdrant_by_chunk_id_one, cid): cid for cid in unique}
        for fut in as_completed(futs):
            cid = futs[fut]
            try:
                rec = fut.result(timeout=per_fetch_timeout)
            except Exception as e:
                log.debug("qdrant fetch future failed for %s: %s", cid, e)
                rec = None
            if rec:
                payload = rec.get("payload", {}) or {}
                results[cid] = {"chunk_id": cid,
                                "qdrant_id": rec.get("id"),
                                "snippet": payload.get("snippet", ""),
                                "document_id": payload.get("document_id"),
                                "vector": rec.get("vector")}

    out: List[Dict[str, Any]] = []
    for cid in unique:
        rec = results.get(cid)
        if rec:
            out.append(rec)
        else:
            out.append({"chunk_id": cid, "qdrant_id": None, "snippet": "", "document_id": None, "vector": None})

    ids = [r["chunk_id"] for r in out if r.get("chunk_id")]
    texts_map = neo4j_fetch_texts(neo4j_driver, ids)
    for r in out:
        r["text"] = texts_map.get(r["chunk_id"], r.get("snippet", ""))
    return out

def _cos_sim(a: List[float], b: List[float]) -> float:
    if not a or not b:
        return 0.0
    sa = sum(x * x for x in a)
    sb = sum(x * x for x in b)
    if sa == 0 or sb == 0:
        return 0.0
    dp = sum(x * y for x, y in zip(a, b))
    return float(dp / (math.sqrt(sa) * math.sqrt(sb)))

def two_stage_query(query_text: str, top_k: int = TOP_K) -> Dict[str, Any]:
    start_total = time.perf_counter()
    # 1) embed
    t0 = time.perf_counter()
    q_vec = embed_text(query_text, max_length=INFERENCE_EMBEDDER_MAX_TOKENS)
    t1 = time.perf_counter()

    # 2) vector search
    vec_list = qdrant_vector_search(q_vec, TOP_VECTOR_CHUNKS)
    vec_chunk_ids = []
    vec_scores = {}
    for r in vec_list:
        payload = r.get("payload", {}) or {}
        cid = payload.get("chunk_id")
        vec = r.get("vector")
        if cid:
            vec_chunk_ids.append(cid)
            if vec:
                try:
                    vec_scores[cid] = _cos_sim(q_vec, [float(x) for x in vec])
                except Exception:
                    vec_scores[cid] = 0.0
            else:
                vec_scores[cid] = 0.0

    # 3) bm25 search
    bm25_list = neo4j_fulltext_search(neo4j_driver, query_text, TOP_BM25_CHUNKS)
    bm25_chunk_ids = [b["chunk_id"] for b in bm25_list]

    # 4) optional metadata keyword search
    if ENABLE_METADATA_CHUNKS:
        kw_list = qdrant_payload_keyword_search(query_text, MAX_METADATA_CHUNKS)
    else:
        kw_list = []
    kw_chunk_ids = [k["payload"].get("chunk_id") for k in kw_list if k.get("payload")]

    # 5) first-stage fusion
    first_stage_lists = []
    if vec_chunk_ids:
        first_stage_lists.append(vec_chunk_ids)
    if bm25_chunk_ids:
        first_stage_lists.append(bm25_chunk_ids)
    if ENABLE_METADATA_CHUNKS and kw_chunk_ids:
        first_stage_lists.append(kw_chunk_ids)
    if not first_stage_lists:
        return {"prompt": "", "provenance": [], "records": [], "llm": None}
    rrf_scores_first = _rrf_fuse(first_stage_lists, k=FIRST_STAGE_RRF_K)
    fused_sorted = sorted(rrf_scores_first.items(), key=lambda x: -x[1])
    fused_ids = [i for i, s in fused_sorted]
    if not fused_ids:
        return {"prompt": "", "provenance": [], "records": [], "llm": None}
    deduped_fused = list(dict.fromkeys(fused_ids))

    # 6) graph expansion (may add candidates)
    to_expand = deduped_fused[:MAX_CHUNKS_FOR_GRAPH_EXPANSION]
    expanded = neo4j_expand_neighbors(neo4j_driver, to_expand, GRAPH_EXPANSION_HOPS, max_additional_per_start=RERANK_TOP)
    expanded_ids = [e["chunk_id"] for e in expanded if e.get("chunk_id")]
    combined_unique = list(dict.fromkeys(deduped_fused + expanded_ids))

    # 7) assemble records (parallelized qdrant fetch + single neo4j fetch for texts)
    detailed = _assemble_records_from_ids(combined_unique)

    # 8) compute final fusion scores second stage
    query_vec = q_vec
    vec_scores_map: Dict[str, float] = {}
    for d in detailed:
        v = d.get("vector")
        cid = d.get("chunk_id")
        if cid:
            if v:
                try:
                    vec_scores_map[cid] = _cos_sim(query_vec, [float(x) for x in v])
                except Exception:
                    vec_scores_map[cid] = 0.0
            else:
                vec_scores_map[cid] = 0.0

    bm25_map = {item["chunk_id"]: item["score"] for item in bm25_list}
    kw_map: Dict[str, float] = {}
    for k in kw_list:
        pid = k.get("payload", {}).get("chunk_id")
        if pid:
            kw_map[pid] = kw_map.get(pid, 0.0) + float(k.get("score", 0.0))

    ranked_lists_for_second: List[List[str]] = []
    if vec_scores_map:
        vec_ranked = sorted(vec_scores_map.items(), key=lambda x: -x[1])
        ranked_lists_for_second.append([cid for cid, sc in vec_ranked])
    if bm25_map:
        ranked_lists_for_second.append(sorted(bm25_map.keys(), key=lambda c: -bm25_map.get(c, 0.0)))
    if ENABLE_METADATA_CHUNKS and kw_map:
        ranked_lists_for_second.append(sorted(kw_map.keys(), key=lambda c: -kw_map.get(c, 0.0)))

    if not ranked_lists_for_second:
        final_ids = [d["chunk_id"] for d in detailed][:top_k]
    else:
        rrf_scores_second = _rrf_fuse(ranked_lists_for_second, k=SECOND_STAGE_RRF_K)
        final_sorted = sorted(rrf_scores_second.items(), key=lambda x: -x[1])
        final_ids = [cid for cid, sc in final_sorted]

    # dedupe preserve order
    deduped = []
    seen = set()
    for cid in final_ids:
        if cid in seen:
            continue
        seen.add(cid)
        deduped.append(cid)
    final_after_cross = deduped

    # optional cross encoder rerank
    if ENABLE_CROSS_ENCODER and rerank_handle:
        cross_candidates = deduped[:MAX_CHUNKS_TO_CROSSENCODER]
        cross_texts_map = neo4j_fetch_texts(neo4j_driver, cross_candidates)
        cross_texts = []
        for cid in cross_candidates:
            txt = cross_texts_map.get(cid)
            if not txt:
                rec = _fetch_qdrant_by_chunk_id_one(cid)
                txt = (rec.get("payload", {}) or {}).get("snippet", "") if rec else ""
            cross_texts.append(txt or "")
        try:
            if cross_texts:
                cross_scores = cross_rerank(query_text, cross_texts, max_length=CROSS_ENCODER_MAX_TOKENS)
            else:
                cross_scores = []
        except Exception as e:
            log.warning("cross rerank failed: %s", e)
            cross_scores = []

        if cross_scores:
            merged = []
            for i, cid in enumerate(cross_candidates):
                base_score = float(rrf_scores_second.get(cid, 0.0)) if 'rrf_scores_second' in locals() else 0.0
                cross_score = float(cross_scores[i]) if i < len(cross_scores) else 0.0
                merged_score = 0.8 * cross_score + 0.2 * base_score
                merged.append({"chunk_id": cid, "score": merged_score})
            merged_sorted = sorted(merged, key=lambda x: -x["score"])
            final_after_cross = [m["chunk_id"] for m in merged_sorted] + [c for c in deduped if c not in {m["chunk_id"] for m in merged}]
        else:
            final_after_cross = deduped

    final_list = final_after_cross[:top_k]
    limited_for_llm = final_list[:MAX_CHUNKS_TO_LLM]
    final_texts_map = neo4j_fetch_texts(neo4j_driver, limited_for_llm)

    final_records = []
    for cid in limited_for_llm:
        qrec = None
        try:
            qrec = next((r for r in detailed if r.get("chunk_id") == cid), None)
        except Exception:
            qrec = None
        payload = (qrec.get("payload", {}) if qrec else {}) or {}
        snippet = qrec.get("snippet", "") if qrec else ""
        docid = qrec.get("document_id") if qrec else None
        text = final_texts_map.get(cid, snippet)
        base_score = 0.0
        if 'rrf_scores_second' in locals():
            base_score = float(rrf_scores_second.get(cid, 0.0))
        final_records.append({"document_id": docid, "chunk_id": cid, "text": text, "score": base_score})

    pieces = []
    provenance = []
    for r in final_records:
        pieces.append(f"CHUNK (doc={r['document_id']} chunk={r['chunk_id']}):\n{r['text']}")
        provenance.append({"document_id": r["document_id"], "chunk_id": r["chunk_id"], "score": r["score"]})

    context = "\n\n---\n\n".join(pieces)
    prompt = f"USE ONLY THE CONTEXT BELOW. Cite provenance by document_id.\n\nCONTEXT:\n{context}\n\nUSER QUERY:\n{query_text}\n"
    end_total = time.perf_counter()

    # timing summary
    timing = {
        "embed_s": round(t1 - t0, 3),
        "total_s": round(end_total - start_total, 3),
        "qdrant_candidates": len(vec_chunk_ids),
        "final_candidate_count": len(final_records),
    }

    return {"prompt": prompt, "provenance": provenance, "records": final_records, "llm": None, "timing": timing}

if __name__ == "__main__":
    q = os.environ.get("HYBRID_QUERY", "What is RAGOps?")
    topk = int(os.environ.get("TOPK", str(TOP_K)))
    start = time.perf_counter()
    try:
        out = two_stage_query(q, top_k=topk)
        end = time.perf_counter()
        summary = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "query": q,
            "topk": topk,
            "timing": out.get("timing", {}),
            "provenance": out.get("provenance", []),
            "records": out.get("records", []),
            "duration_s": round(end - start, 3)
        }
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    except Exception as e:
        log.exception("query failed: %s", e)
        print(json.dumps({"error": str(e)}, indent=2))
