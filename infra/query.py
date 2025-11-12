from __future__ import annotations
import os
import time
import math
import json
import logging
import random
import functools
import queue
import threading
from typing import Any, Dict, List, Optional, Tuple
try:
    import ray
    from ray import serve
except Exception:
    ray = None
    serve = None
try:
    from qdrant_client import QdrantClient
except Exception:
    QdrantClient = None
try:
    from neo4j import GraphDatabase
    from neo4j.exceptions import Neo4jError
except Exception:
    GraphDatabase = None
    Neo4jError = Exception
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("query")
RAY_ADDRESS = os.getenv("RAY_ADDRESS", "auto")
RAY_NAMESPACE = os.getenv("RAY_NAMESPACE", None)
EMBED_DEPLOYMENT = os.getenv("EMBED_DEPLOYMENT", "embed_onnx_cpu")
RERANK_DEPLOYMENT = os.getenv("RERANK_HANDLE_NAME", "rerank_onnx_cpu")
LLM_DEPLOYMENT = os.getenv("LLM_DEPLOYMENT_NAME", "llm_server_cpu")
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
EMBED_TIMEOUT = float(os.getenv("EMBED_TIMEOUT", "10"))
CALL_TIMEOUT_SECONDS = float(os.getenv("CALL_TIMEOUT_SECONDS", "10"))
RETRY_ATTEMPTS = max(1, int(os.getenv("RETRY_ATTEMPTS", "3")))
RETRY_BASE_SECONDS = float(os.getenv("RETRY_BASE_SECONDS", "0.5"))
RETRY_JITTER = float(os.getenv("RETRY_JITTER", "0.3"))
ENABLE_CROSS_ENCODER = os.getenv("ENABLE_CROSS_ENCODER", "true").lower() in ("1", "true", "yes")
MAX_CHUNKS_TO_LLM = int(os.getenv("MAX_CHUNKS_TO_LLM", "8"))
MODE = os.getenv("MODE", "hybrid").lower()
ENABLE_METADATA_CHUNKS = os.getenv("ENABLE_METADATA_CHUNKS", "false").lower() in ("1", "true", "yes")
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
        ray.init(address=(RAY_ADDRESS if RAY_ADDRESS and RAY_ADDRESS != "auto" else None), namespace=RAY_NAMESPACE, ignore_reinit_error=True)
def _resolve_ray_response(obj, timeout: float):
    try:
        return ray.get(obj, timeout=timeout)
    except Exception:
        return obj
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
    try:
        klass = getattr(resp, "__class__", None)
        if klass is not None:
            modname = getattr(klass, "__module__", "")
            name = getattr(klass, "__name__", "")
            if "ray.serve" in modname or "DeploymentResponse" in name or name.startswith("DeploymentResponse"):
                try:
                    if hasattr(resp, "result") and callable(resp.result):
                        return resp.result(timeout=timeout)
                except Exception:
                    pass
                try:
                    if hasattr(resp, "get") and callable(resp.get):
                        return resp.get(timeout=timeout)
                except Exception:
                    pass
    except Exception:
        pass
    try:
        if hasattr(resp, "object_refs"):
            ors = getattr(resp, "object_refs")
            if isinstance(ors, list):
                if len(ors) == 1:
                    return ray.get(ors[0], timeout=timeout)
                return ray.get(ors, timeout=timeout)
        if hasattr(resp, "result") and callable(getattr(resp, "result")):
            try:
                return resp.result(timeout=timeout)
            except TypeError:
                return resp.result()
        if hasattr(resp, "get") and callable(getattr(resp, "get")):
            try:
                return resp.get(timeout=timeout)
            except TypeError:
                return resp.get()
    except Exception:
        pass
    try:
        return ray.get(resp, timeout=timeout)
    except Exception:
        pass
    return resp
def get_strict_handle(name: str, timeout: float = 30.0, poll: float = 0.5, app_name: Optional[str] = None):
    _ensure_ray_connected()
    start = time.time()
    last_exc = None
    app_name = app_name or os.getenv("SERVE_APP_NAME", "default")
    while time.time() - start < timeout:
        try:
            handle = None
            if hasattr(serve, "get_deployment_handle"):
                try:
                    handle = serve.get_deployment_handle(name, app_name=app_name, _check_exists=False)
                except TypeError:
                    handle = serve.get_deployment_handle(name, _check_exists=False)
                except Exception:
                    handle = None
            if handle is None and hasattr(serve, "get_handle"):
                try:
                    handle = serve.get_handle(name, sync=False)
                except Exception:
                    handle = None
            if handle is None and hasattr(serve, "get_deployment"):
                try:
                    dep = serve.get_deployment(name)
                    handle = dep.get_handle(sync=False)
                except Exception:
                    handle = None
            if handle is None:
                raise RuntimeError("no serve handle API available")
            try:
                ref = handle.remote({"texts": ["__health_check__"], "max_length": INFERENCE_EMBEDDER_MAX_TOKENS})
                _ = _resolve_ray_response(ref, timeout=10.0)
                return handle
            except Exception as e:
                last_exc = e
        except Exception as e:
            last_exc = e
        time.sleep(poll)
    raise RuntimeError(f"timed out resolving serve handle {name}: {last_exc}")
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
        if hasattr(resp, "__iter__"):
            try:
                vecs = list(resp)
            except Exception:
                pass
    if vecs is None:
        raise RuntimeError(f"embed returned bad payload type {type(resp)}")
    if isinstance(vecs, dict):
        try:
            vecs = list(vecs.values())
        except Exception:
            pass
    if isinstance(vecs, list) and len(vecs) >= 1 and isinstance(vecs[0], list):
        v = vecs[0]
    elif isinstance(vecs, list) and len(vecs) == 1 and not isinstance(vecs[0], (int, float)):
        v = vecs[0]
    elif isinstance(vecs, list) and len(vecs) == VECTOR_DIM:
        v = vecs
    else:
        try:
            v = vecs[0]
        except Exception:
            raise RuntimeError("embed returned wrong shape")
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
@retry()
def make_clients() -> Tuple[Any, Any]:
    q = None
    neo = None
    if QDRANT_CLIENT_AVAILABLE := (QdrantClient is not None):
        try:
            q = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY, prefer_grpc=PREFER_GRPC)
        except Exception as e:
            log.warning("qdrant client creation failed: %s", e)
            q = None
    try:
        if GraphDatabase is not None:
            neo = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
            try:
                neo.verify_connectivity()
            except Exception:
                log.debug("neo4j verify_connectivity failed")
    except Exception:
        neo = None
    return q, neo
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
@retry()
def qdrant_vector_search(client, q_vec: List[float], top_k: int, with_payload: bool = True, with_vectors: bool = True):
    if client is None:
        return []
    kw = {"with_payload": bool(with_payload), "with_vector": bool(with_vectors)}
    results = None
    last_exc = None
    try:
        if hasattr(client, "query_points"):
            try:
                results = client.query_points(collection_name=QDRANT_COLLECTION, query_vector=q_vec, limit=top_k, **kw)
            except TypeError:
                results = client.query_points(collection_name=QDRANT_COLLECTION, vector=q_vec, limit=top_k, **kw)
    except Exception as e:
        last_exc = e
    if results is None:
        tried = [{"query_vector": q_vec, **kw}, {"vector": q_vec, **kw}]
        for t in tried:
            try:
                results = client.search(collection_name=QDRANT_COLLECTION, **t, limit=top_k)
                break
            except Exception as e:
                last_exc = e
                continue
    if results is None:
        raise RuntimeError(f"qdrant vector search failed: {last_exc}")
    out = []
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
@retry()
def neo4j_fulltext_search(driver, query: str, index_name: str = "chunkFulltextIndex", top_k: int = TOP_BM25_CHUNKS):
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
def neo4j_fetch_texts(driver, chunk_ids: List[str]):
    if not chunk_ids or driver is None:
        return {}
    out = {}
    with driver.session() as s:
        cy = "MATCH (c:Chunk) WHERE c.chunk_id IN $ids RETURN c.chunk_id AS cid, c.text AS text, c.token_count AS token_count, c.document_id AS document_id, c.source_url AS source_url, c.file_name AS file_name, c.page_number AS page_number, c.row_range AS row_range, c.token_range AS token_range, c.audio_range AS audio_range, c.headings AS headings, c.headings_path AS headings_path"
        res = s.run(cy, ids=list(chunk_ids))
        for r in res:
            try:
                out[r["cid"]] = {"text": r.get("text") or "", "token_count": int(r.get("token_count") or 0), "document_id": r.get("document_id"), "source_url": r.get("source_url"), "file_name": r.get("file_name"), "page_number": r.get("page_number"), "row_range": r.get("row_range"), "token_range": r.get("token_range"), "audio_range": r.get("audio_range"), "headings": r.get("headings"), "headings_path": r.get("headings_path")}
            except Exception:
                continue
    return out
@retry()
def neo4j_find_chunk_ids_by_qdrant_ids(driver, qdrant_ids: List[str]):
    out = {}
    if not qdrant_ids or driver is None:
        return out
    with driver.session() as s:
        cy = "MATCH (c:Chunk) WHERE c.qdrant_id IN $qids RETURN c.qdrant_id AS qid, c.chunk_id AS chunk_id"
        res = s.run(cy, qids=list(qdrant_ids))
        for r in res:
            try:
                out[str(r["qid"])] = r.get("chunk_id")
            except Exception:
                continue
    return out
def prepare_record_for_llm(entry: Dict[str, Any]) -> Dict[str, Any]:
    return {"chunk_id": entry.get("chunk_id"), "text": entry.get("text", ""), "source_url": entry.get("source_url"), "file_name": entry.get("file_name"), "page_number": entry.get("page_number"), "row_range": entry.get("row_range"), "token_range": entry.get("token_range"), "audio_range": entry.get("audio_range"), "headings": entry.get("headings"), "headings_path": entry.get("headings_path")}
def retrieve_pipeline(embed_handle, rerank_handle, qdrant_client, neo4j_driver, query_text: str, max_chunks: int = MAX_CHUNKS_TO_LLM):
    t0 = time.time()
    vec = None
    try:
        vec = embed_text(embed_handle, query_text, max_length=INFERENCE_EMBEDDER_MAX_TOKENS)
    except Exception as e:
        log.warning("embed failed, falling back to BM25-only retrieval: %s", e)
        vec = None
    ann_hits = []
    if vec is not None:
        try:
            ann_hits = qdrant_vector_search(qdrant_client, vec, TOP_VECTOR_CHUNKS, with_payload=(MODE=="vector_only"), with_vectors=True)
        except Exception as e:
            log.warning("qdrant_vector_search failed: %s", e)
            ann_hits = []
    vec_rank = []
    id_to_vec = {}
    qdrant_ids_seen = []
    for h in ann_hits:
        payload = h.get("payload") or {}
        cid = payload.get("chunk_id") or payload.get("chunkId")
        rid = h.get("id")
        if cid:
            cid = str(cid)
            vec_rank.append(cid)
            if h.get("vector"):
                id_to_vec[cid] = h.get("vector")
        else:
            if rid is not None:
                qdrant_ids_seen.append(str(rid))
    if qdrant_ids_seen and neo4j_driver is not None:
        try:
            qid_map = neo4j_find_chunk_ids_by_qdrant_ids(neo4j_driver, qdrant_ids_seen)
        except Exception:
            qid_map = {}
        for h in ann_hits:
            payload = h.get("payload") or {}
            cid = payload.get("chunk_id")
            rid = h.get("id")
            if cid:
                continue
            if rid is not None:
                mapped = qid_map.get(str(rid))
                if mapped:
                    vec_rank.append(mapped)
                    if h.get("vector"):
                        id_to_vec[mapped] = h.get("vector")
    bm25_list = []
    bm25_map = {}
    if MODE == "hybrid" and neo4j_driver is not None:
        try:
            bm25_hits = neo4j_fulltext_search(neo4j_driver, query_text, top_k=TOP_BM25_CHUNKS)
            bm25_list = [cid for cid, _ in bm25_hits]
            bm25_map = {cid: sc for cid, sc in (bm25_hits or [])}
        except Exception:
            bm25_list = []
            bm25_map = {}
    metadata_list = []
    if ENABLE_METADATA_CHUNKS:
        try:
            if MODE == "vector_only":
                try:
                    meta_hits = qdrant_client.search(collection_name=QDRANT_COLLECTION, query_vector=None, with_payload=True, limit=50)
                    for mh in meta_hits:
                        mpayload = getattr(mh, "payload", None) or (mh.get("payload") if isinstance(mh, dict) else {}) or {}
                        mid = mpayload.get("chunk_id") or getattr(mh, "id", None)
                        if mid:
                            metadata_list.append(str(mid))
                except Exception:
                    metadata_list = []
            else:
                metadata_list = []
        except Exception:
            metadata_list = []
    ranked_lists = []
    if vec_rank:
        ranked_lists.append(vec_rank)
    if bm25_list:
        ranked_lists.append(bm25_list)
    if metadata_list:
        ranked_lists.append(metadata_list)
    fused_scores = _rrf_fuse(ranked_lists, k=60)
    fused_order = sorted(fused_scores.items(), key=lambda x: -x[1])
    fused_list = [cid for cid, _ in fused_order]
    deduped_fused = stable_dedupe(fused_list)
    seeds = deduped_fused[:20]
    expanded = []
    if MODE == "hybrid" and seeds and neo4j_driver is not None:
        try:
            expanded = []
        except Exception:
            expanded = []
    combined_unique_candidates = stable_dedupe(deduped_fused + expanded + vec_rank + bm25_list + metadata_list)
    texts_map = {}
    try:
        if MODE == "vector_only":
            for h in ann_hits:
                payload = h.get("payload") or {}
                cid = payload.get("chunk_id") or payload.get("chunkId")
                if cid:
                    cid = str(cid)
                    texts_map.setdefault(cid, {})["text"] = payload.get("text") or texts_map.get(cid, {}).get("text","")
                    texts_map[cid]["token_count"] = int(payload.get("token_count") or texts_map.get(cid, {}).get("token_count",0))
                    texts_map[cid]["source_url"] = payload.get("source_url") or texts_map.get(cid, {}).get("source_url")
                    texts_map[cid]["file_name"] = payload.get("file_name") or texts_map.get(cid, {}).get("file_name")
                    texts_map[cid]["page_number"] = payload.get("page_number") or texts_map.get(cid, {}).get("page_number")
                    texts_map[cid]["row_range"] = payload.get("row_range") or texts_map.get(cid, {}).get("row_range")
                    texts_map[cid]["token_range"] = payload.get("token_range") or texts_map.get(cid, {}).get("token_range")
                    texts_map[cid]["audio_range"] = payload.get("audio_range") or texts_map.get(cid, {}).get("audio_range")
                    texts_map[cid]["headings"] = payload.get("headings") or texts_map.get(cid, {}).get("headings")
                    texts_map[cid]["headings_path"] = payload.get("headings_path") or texts_map.get(cid, {}).get("headings_path")
                    texts_map[cid]["document_id"] = payload.get("document_id") or texts_map.get(cid, {}).get("document_id")
        else:
            texts_map = neo4j_fetch_texts(neo4j_driver, combined_unique_candidates)
    except Exception:
        texts_map = {}
    candidates = []
    vec_map = {}
    for h in ann_hits:
        payload = h.get("payload") or {}
        cid = payload.get("chunk_id") or payload.get("chunkId")
        if not cid:
            cid = None
            rid = h.get("id")
            if rid is not None and neo4j_driver is not None:
                try:
                    mapped = neo4j_find_chunk_ids_by_qdrant_ids(neo4j_driver, [str(rid)])
                    cid = mapped.get(str(rid))
                except Exception:
                    cid = None
        if cid:
            cid = str(cid)
            vec_map[cid] = float(h.get("score", 0.0) or 0.0)
    for cid in combined_unique_candidates:
        text_entry = texts_map.get(cid, {})
        token_count = int(text_entry.get("token_count", 0) or 0)
        rec = {"chunk_id": cid, "text": text_entry.get("text", ""), "token_count": token_count, "document_id": text_entry.get("document_id"), "vector_score": vec_map.get(cid, 0.0), "bm25_score": float(bm25_map.get(cid, 0.0) or 0.0), "source_url": text_entry.get("source_url"), "file_name": text_entry.get("file_name"), "page_number": text_entry.get("page_number"), "row_range": text_entry.get("row_range"), "token_range": text_entry.get("token_range"), "audio_range": text_entry.get("audio_range"), "headings": text_entry.get("headings"), "headings_path": text_entry.get("headings_path")}
        candidates.append(rec)
    for c in candidates:
        v = c.get("vector")
        if v and vec is not None and isinstance(vec, list):
            try:
                if len(vec) == len(v):
                    c["vec_sim"] = cosine(vec, v)
                else:
                    c["vec_sim"] = c.get("vector_score", 0.0)
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
    if ENABLE_CROSS_ENCODER and rerank_handle is not None and final_order:
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
    llm_records = [prepare_record_for_llm(r) for r in selected]
    provenance = []
    for r in selected:
        provenance.append({"document_id": r.get("document_id"), "chunk_id": r.get("chunk_id"), "score": fused2_scores.get(r.get("chunk_id"), r.get("vec_sim", 0.0))})
    prompt = json.dumps({"QUERY": query_text, "CONTEXT_CHUNKS": llm_records, "YOUR_ROLE": "You are a helpful knowledge assistant who answers user queries with provenance using only the provided context chunks below."}, ensure_ascii=False)
    elapsed = time.time() - t0
    return {"prompt": prompt, "provenance": provenance, "records": llm_records, "llm": None, "elapsed": elapsed}
def call_llm_blocking(llm_handle, prompt_json: str, params: Optional[Dict[str, Any]] = None, timeout: float = 60.0):
    if llm_handle is None:
        raise RuntimeError("LLM handle missing")
    payload = {"prompt": prompt_json, "params": params or {}, "stream": False}
    resp = call_handle(llm_handle, payload, timeout=timeout)
    if isinstance(resp, dict):
        if "text" in resp:
            return str(resp.get("text"))
        if "output" in resp:
            return str(resp.get("output"))
        return json.dumps(resp, default=str)
    return str(resp)
async def call_llm_stream(llm_handle, prompt_json: str, params: Optional[Dict[str, Any]] = None):
    if llm_handle is None:
        raise RuntimeError("LLM handle missing")
    params = params or {}
    try:
        if hasattr(llm_handle, "stream") and hasattr(llm_handle.stream, "remote"):
            try:
                resp = llm_handle.stream.remote(prompt_json, params)
            except Exception:
                resp = llm_handle.stream.remote(prompt_json)
        elif hasattr(llm_handle, "generate") and hasattr(llm_handle.generate, "remote"):
            try:
                resp = llm_handle.generate.remote(prompt_json, params)
            except Exception:
                resp = llm_handle.generate.remote(prompt_json)
        else:
            try:
                resp = llm_handle.remote({"prompt": prompt_json, "params": params, "stream": True})
            except Exception:
                resp = llm_handle.remote(prompt_json)
    except Exception:
        txt = call_llm_blocking(llm_handle, prompt_json, params=params)
        yield txt
        return
    iterable = None
    try:
        if hasattr(resp, "result") and callable(resp.result):
            try:
                iterable = resp.result(timeout=CALL_TIMEOUT_SECONDS)
            except Exception:
                iterable = resp.result()
        elif hasattr(resp, "get") and callable(resp.get):
            try:
                iterable = resp.get(timeout=CALL_TIMEOUT_SECONDS)
            except Exception:
                iterable = resp.get()
        else:
            try:
                iterable = ray.get(resp, timeout=CALL_TIMEOUT_SECONDS)
            except Exception:
                iterable = resp
    except Exception:
        iterable = resp
    if isinstance(iterable, list):
        for piece in iterable:
            yield str(piece)
        return
    try:
        iterator = iter(iterable)
        for piece in iterator:
            yield str(piece)
        return
    except Exception:
        try:
            yield str(iterable)
        except Exception:
            yield ""
