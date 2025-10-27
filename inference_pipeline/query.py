#!/usr/bin/env python3
# inference_pipeline/query.py
# Handle-only hardened retrieval pipeline for Ray Serve.
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

# optional heavy deps imported lazily
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

# HTTP client is allowed but not used for fallback in this file.
try:
    import httpx
except Exception:
    httpx = None

# ---------- CONFIG ----------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("query")

RAY_ADDRESS = os.getenv("RAY_ADDRESS", "auto")
RAY_NAMESPACE = os.getenv("RAY_NAMESPACE", None)

EMBED_DEPLOYMENT = os.getenv("EMBED_DEPLOYMENT", "embed_onxx")
RERANK_DEPLOYMENT = os.getenv("RERANK_HANDLE_NAME", "rerank_onxx")
SERVE_APP_NAME = os.getenv("SERVE_APP_NAME", "default")  # ensure correct app name if using Serve apps

QDRANT_URL = os.getenv("QDRANT_URL", "http://127.0.0.1:6333")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY", None)
PREFER_GRPC = os.getenv("PREFER_GRPC", "true").lower() in ("1", "true", "yes")
QDRANT_COLLECTION = os.getenv("COLLECTION", "my_collection")

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "")

VECTOR_DIM = int(os.getenv("VECTOR_DIM", "768"))

TOP_K = int(os.getenv("TOP_K", "5"))
TOP_VECTOR_CHUNKS = int(os.getenv("TOP_VECTOR_CHUNKS", "200"))
TOP_BM25_CHUNKS = int(os.getenv("TOP_BM25_CHUNKS", "100"))

INFERENCE_EMBEDDER_MAX_TOKENS = int(os.getenv("INFERENCE_EMBEDDER_MAX_TOKENS", "64"))
CROSS_ENCODER_MAX_TOKENS = int(os.getenv("CROSS_ENCODER_MAX_TOKENS", "600"))
MAX_PROMPT_TOKENS = int(os.getenv("MAX_PROMPT_TOKENS", "3000"))

HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "30"))
EMBED_TIMEOUT = float(os.getenv("EMBED_TIMEOUT", "10"))
CALL_TIMEOUT_SECONDS = float(os.getenv("CALL_TIMEOUT_SECONDS", "10"))

RETRY_ATTEMPTS = int(os.getenv("RETRY_ATTEMPTS", "3"))
RETRY_BASE_SECONDS = float(os.getenv("RETRY_BASE_SECONDS", "0.5"))
RETRY_JITTER = float(os.getenv("RETRY_JITTER", "0.3"))

ENABLE_CROSS_ENCODER = os.getenv("ENABLE_CROSS_ENCODER", "false").lower() in ("1", "true", "yes")
MAX_CHUNKS_TO_LLM = int(os.getenv("MAX_CHUNKS_TO_LLM", "8"))

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

def _resolve_ray_response(obj, timeout: float):
    """Resolve possible Ray ObjectRef or return direct value."""
    # If it's a Ray ObjectRef, use ray.get
    try:
        # detect object ref by attribute name to be robust across versions
        ObjectRefType = getattr(ray, "ObjectRef", None) or getattr(ray, "_raylet.ObjectRef", None)
    except Exception:
        ObjectRefType = None
    try:
        if ObjectRefType is not None and isinstance(obj, ObjectRefType):
            return ray.get(obj, timeout=timeout)
    except Exception:
        # fall through and try ray.get generically
        pass
    try:
        # generic attempt
        return ray.get(obj, timeout=timeout)
    except Exception:
        pass
    # not an ObjectRef or ray.get failed; return as-is
    return obj

# ---------- Serve handle helpers (handle-only) ----------
def get_strict_handle(name: str, timeout: float = 30.0, poll: float = 0.5, app_name: Optional[str] = SERVE_APP_NAME):
    """
    Strict handle resolution. No HTTP fallback.
    Tries multiple Serve handle APIs and performs a health probe by calling the handle.
    Raises on timeout or failure.
    """
    start = time.time()
    last_exc = None
    _ensure_ray_connected()
    if serve is None:
        raise RuntimeError("ray.serve not importable")

    while time.time() - start < timeout:
        try:
            # Preferred: serve.get_deployment_handle (Ray >= x)
            if hasattr(serve, "get_deployment_handle"):
                try:
                    # try both with and without app_name parameter depending on Serve version
                    try:
                        handle = serve.get_deployment_handle(name, app_name=app_name, _check_exists=False)
                    except TypeError:
                        handle = serve.get_deployment_handle(name, _check_exists=False)
                    # quick health call
                    resp_obj = handle.remote({"texts": ["__health_check__"], "max_length": INFERENCE_EMBEDDER_MAX_TOKENS})
                    resp = _resolve_ray_response(resp_obj, timeout=EMBED_TIMEOUT)
                    # expect dict or list with vectors for embed; treat any non-exception as success
                    if resp is not None:
                        log.info("Resolved serve handle %s via get_deployment_handle", name)
                        return handle
                except Exception as e:
                    last_exc = e
            # Try legacy get_handle / get_deployment
            if hasattr(serve, "get_handle"):
                try:
                    handle = serve.get_handle(name, sync=False)
                    resp_obj = handle.remote({"texts": ["__health_check__"], "max_length": INFERENCE_EMBEDDER_MAX_TOKENS})
                    resp = _resolve_ray_response(resp_obj, timeout=EMBED_TIMEOUT)
                    if resp is not None:
                        log.info("Resolved serve handle %s via get_handle", name)
                        return handle
                except Exception as e:
                    last_exc = e
            if hasattr(serve, "get_deployment"):
                try:
                    dep = serve.get_deployment(name)
                    handle = dep.get_handle(sync=False)
                    resp_obj = handle.remote({"texts": ["__health_check__"], "max_length": INFERENCE_EMBEDDER_MAX_TOKENS})
                    resp = _resolve_ray_response(resp_obj, timeout=EMBED_TIMEOUT)
                    if resp is not None:
                        log.info("Resolved serve handle %s via get_deployment", name)
                        return handle
                except Exception as e:
                    last_exc = e
        except Exception as e:
            last_exc = e
        time.sleep(poll)
    raise RuntimeError(f"timed out getting serve handle '{name}': {last_exc}")

# ---------- Clients ----------
@retry()
def make_clients() -> Tuple[QdrantClient, Any]:
    q = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY, prefer_grpc=PREFER_GRPC)
    neo = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    # lightweight checks
    try:
        q.get_collection(collection_name=QDRANT_COLLECTION)
        log.debug("qdrant reachable")
    except Exception:
        log.debug("qdrant get_collection failed (collection may not exist)")
    try:
        neo.verify_connectivity()
        log.debug("neo4j reachable")
    except Exception:
        log.debug("neo4j verify_connectivity failed")
    return q, neo

# ---------- Embedding + Rerank wrappers ----------
@retry()
def embed_text(embed_handle, text: str, max_length: int = INFERENCE_EMBEDDER_MAX_TOKENS) -> List[float]:
    if embed_handle is None:
        raise RuntimeError("embed handle missing")
    payload = {"texts": [text], "max_length": int(max_length)}
    resp_obj = embed_handle.remote(payload)
    resp = _resolve_ray_response(resp_obj, timeout=EMBED_TIMEOUT)
    if not isinstance(resp, dict):
        raise RuntimeError(f"embed returned bad payload type {type(resp)}")
    if "vectors" not in resp:
        raise RuntimeError("embed returned bad payload (missing 'vectors')")
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
    resp = _resolve_ray_response(resp_obj, timeout=EMBED_TIMEOUT)
    if not isinstance(resp, dict) or "scores" not in resp:
        raise RuntimeError("rerank returned bad payload")
    scores = resp["scores"]
    if not isinstance(scores, list) or len(scores) != len(texts):
        raise RuntimeError("rerank returned wrong shape")
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
    # prefer query_points
    if hasattr(client, "query_points"):
        try:
            try:
                results = client.query_points(collection_name=QDRANT_COLLECTION, query_vector=q_vec, limit=top_k, **kw)
            except TypeError:
                # older signature
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
    # 1) embed
    vec = embed_text(embed_handle, query_text, max_length=INFERENCE_EMBEDDER_MAX_TOKENS)
    t1 = time.time()
    log.debug("embed elapsed %.3fs", t1 - t0)

    # 2) ANN window
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

    # BM25
    try:
        bm25_hits = neo4j_fulltext_search(neo4j_driver, query_text, top_k=TOP_BM25_CHUNKS)
        bm25_list = [cid for cid, _ in bm25_hits]
    except Exception:
        bm25_list = []

    ranked_lists = []
    if vec_rank:
        ranked_lists.append(vec_rank)
    if bm25_list:
        ranked_lists.append(bm25_list)
    fused_scores = _rrf_fuse(ranked_lists, k=60)
    fused_order = sorted(fused_scores.items(), key=lambda x: -x[1])
    fused_list = [cid for cid, _ in fused_order]
    deduped_fused = stable_dedupe(fused_list)

    seeds = deduped_fused[:100]
    expanded = []
    try:
        expanded = neo4j_expand_graph(neo4j_driver, seeds, hops=1, per_seed_limit=5)
    except Exception:
        expanded = []

    combined_unique_candidates = stable_dedupe(deduped_fused + expanded + vec_rank + bm25_list)
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
    ranked2 = []
    if vec_rank2:
        ranked2.append(vec_rank2)
    if bm25_rank2:
        ranked2.append(bm25_rank2)
    fused2_scores = _rrf_fuse(ranked2, k=60)
    final_fused_order = sorted(fused2_scores.items(), key=lambda x: -x[1])
    final_order = stable_dedupe([cid for cid, _ in final_fused_order])

    if ENABLE_CROSS_ENCODER and rerank_handle and final_order:
        top_for_x = final_order[:min(len(final_order), 64)]
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
    log.info("query.py starting (handle-only mode)")
    _ensure_ray_connected()

    # resolve handles strictly. Fail fast if not present.
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
            # choose to fail or continue without reranker
            log.warning("Continuing without reranker")
            rerank_handle = None

    qdrant_client, neo4j_driver = make_clients()
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
