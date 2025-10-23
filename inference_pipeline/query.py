from __future__ import annotations
import os, time, logging, ray, httpx
from typing import List, Dict, Any, TypedDict, Optional
from functools import wraps
from dotenv import load_dotenv
load_dotenv()

class Provenance(TypedDict):
    neo4j_id: Optional[str]
    chunk_id: Optional[str]
    score: Optional[float]

class QueryOutput(TypedDict):
    prompt: str
    provenance: List[Provenance]
    records: List[Dict[str, Any]]
    llm: Optional[Any]

RAY_ADDRESS = os.getenv("RAY_ADDRESS", None)
EMBED_HANDLE_NAME = os.getenv("EMBED_HANDLE_NAME", "embed_onxx")
RERANK_HANDLE_NAME = os.getenv("RERANK_HANDLE_NAME", "rerank_onxx")
QDRANT_COLLECTION = os.getenv("COLLECTION", "my_collection")
VECTOR_DIM = int(os.getenv("VECTOR_DIM", "768"))
TOP_K = int(os.getenv("TOP_K", "5"))
RERANK_TOP = int(os.getenv("RERANK_TOP", "20"))
HYBRID_ALPHA = float(os.getenv("HYBRID_ALPHA", "1.0"))
LLM_URL = os.getenv("LLM_URL", "").rstrip("/")
LLM_TYPE = os.getenv("LLM_TYPE", "mock")
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "30"))
RETRY_ATTEMPTS = int(os.getenv("RETRY_ATTEMPTS", "3"))
RETRY_BASE = float(os.getenv("RETRY_BASE_SECONDS", "0.5"))

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("query")

def retry(attempts: int = RETRY_ATTEMPTS, base: float = RETRY_BASE):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **k):
            last = None
            for i in range(attempts):
                try:
                    return fn(*a, **k)
                except Exception as e:
                    last = e
                    wait = base * (2 ** i)
                    log.warning("retry %d/%d for %s: %s - sleeping %.2fs", i + 1, attempts, fn.__name__, e, wait)
                    time.sleep(wait)
            log.error("all retries failed for %s", fn.__name__)
            raise last
        return wrapper
    return deco

@retry()
def make_clients_and_retriever():
    from qdrant_client import QdrantClient
    from neo4j import GraphDatabase
    from neo4j_graphrag.retrievers import QdrantNeo4jRetriever
    q = QdrantClient(url=os.getenv("QDRANT_URL", "http://localhost:6333"),
                     api_key=os.getenv("QDRANT_API_KEY") or None,
                     prefer_grpc=os.getenv("PREFER_GRPC", "true").lower() in ("1", "true", "yes"))
    drv = GraphDatabase.driver(os.getenv("NEO4J_URI", "bolt://localhost:7687"),
                               auth=(os.getenv("NEO4J_USER", "neo4j"), os.getenv("NEO4J_PASSWORD", "neo4j")))
    retr = QdrantNeo4jRetriever(driver=drv, client=q, collection_name=QDRANT_COLLECTION,
                                id_property_neo4j="neo4j_id", id_property_external="neo4j_id")
    return q, drv, retr

qdrant_client, neo4j_driver, retriever = make_clients_and_retriever()

ray.init(address=RAY_ADDRESS)
from ray import serve
_embed_handle = serve.get_deployment(EMBED_HANDLE_NAME).get_handle(sync=False)
_rerank_handle = serve.get_deployment(RERANK_HANDLE_NAME).get_handle(sync=False)

_http = httpx.Client(timeout=HTTP_TIMEOUT)

def _minmax_norm(xs: List[float]) -> List[float]:
    if not xs:
        return []
    vals = [float(x) if x is not None else 0.0 for x in xs]
    mn, mx = min(vals), max(vals)
    if mx <= mn:
        return [0.0 for _ in vals]
    return [(v - mn) / (mx - mn) for v in vals]

def _extract_score_from_record(rec: Any) -> float:
    try:
        d = rec.data()
    except Exception:
        return 0.0
    if isinstance(d, dict):
        if "score" in d and isinstance(d["score"], (int, float)):
            return float(d["score"])
        for v in d.values():
            if isinstance(v, dict) and isinstance(v.get("score"), (int, float)):
                return float(v.get("score"))
    return 0.0

@retry()
def embed_text(text: str) -> List[float]:
    ref = _embed_handle.remote({"texts": [text]})
    resp = ray.get(ref, timeout=HTTP_TIMEOUT)
    assert isinstance(resp, dict), f"embed serve returned non-dict: {type(resp)}"
    vecs = resp.get("vectors")
    assert isinstance(vecs, list) and len(vecs) == 1, f"embed serve must return 1 vector, got: {vecs}"
    vec = vecs[0]
    assert isinstance(vec, (list, tuple)), "embed vector must be list-like"
    if len(vec) != VECTOR_DIM:
        raise RuntimeError(f"embed vector dim mismatch: got {len(vec)} expected {VECTOR_DIM}")
    return [float(x) for x in vec]

@retry()
def rerank_batch(query: str, texts: List[str]) -> List[float]:
    if not texts:
        return []
    ref = _rerank_handle.remote({"query": query, "cands": texts})
    resp = ray.get(ref, timeout=HTTP_TIMEOUT)
    assert isinstance(resp, dict), f"rerank serve returned non-dict: {type(resp)}"
    scores = resp.get("scores", [])
    assert isinstance(scores, list), f"rerank scores must be list, got {type(scores)}"
    if len(scores) != len(texts):
        raise RuntimeError(f"reranker returned {len(scores)} scores for {len(texts)} candidates")
    return [float(s) for s in scores]

@retry()
def hybrid_query(query_text: str, top_k: int = TOP_K) -> QueryOutput:
    q_vec = embed_text(query_text)
    raw = retriever.get_search_results(query_vector=q_vec, top_k=max(top_k, RERANK_TOP))
    assert hasattr(raw, "records"), "retriever.get_search_results returned object missing 'records'"
    records = raw.records
    if not isinstance(records, list):
        raise RuntimeError("retriever returned unexpected records type")
    candidates: List[Dict[str, Any]] = []
    for rec in records:
        try:
            rd = rec.data()
        except Exception:
            rd = {"raw": str(rec)}
        node_props = {}
        if isinstance(rd, dict) and "n_props" in rd and isinstance(rd["n_props"], dict):
            node_props = rd["n_props"]
        else:
            for v in rd.values() if isinstance(rd, dict) else []:
                if isinstance(v, dict):
                    node_props = v
                    break
        neo4j_id = node_props.get("neo4j_id") or node_props.get("id")
        text = node_props.get("text") or node_props.get("title") or str(node_props)
        score = _extract_score_from_record(rec)
        candidates.append({"neo4j_id": neo4j_id, "text": text, "props": node_props, "vec_score": score})
    if not candidates:
        return {"prompt": "", "provenance": [], "records": [], "llm": None}
    vec_scores = [c["vec_score"] for c in candidates]
    vec_norm = _minmax_norm(vec_scores)
    for i, c in enumerate(candidates):
        c["_vec_norm"] = vec_norm[i]
    alpha = float(HYBRID_ALPHA) if HYBRID_ALPHA is not None else 1.0
    other_norm = [0.0] * len(candidates)
    hybrid_scores = [alpha * v + (1.0 - alpha) * o for v, o in zip(vec_norm, other_norm)]
    for i, c in enumerate(candidates):
        c["_hybrid"] = hybrid_scores[i]
    candidates.sort(key=lambda x: (-x["_hybrid"], str(x.get("neo4j_id", ""))))
    topN = min(len(candidates), RERANK_TOP)
    top_texts = [c["text"] for c in candidates[:topN]]
    try:
        cross_scores = rerank_batch(query_text, top_texts)
    except Exception as e:
        log.warning("rerank failed; continuing with hybrid scores: %s", e)
        cross_scores = []
    if cross_scores:
        cross_norm = _minmax_norm(cross_scores)
        for i in range(topN):
            candidates[i]["_cross"] = cross_scores[i]
            candidates[i]["_cross_norm"] = cross_norm[i]
            candidates[i]["_hybrid"] = 0.9 * cross_norm[i] + 0.1 * candidates[i].get("_vec_norm", 0.0)
        candidates.sort(key=lambda x: (-x["_hybrid"], str(x.get("neo4j_id", ""))))
    pieces: List[str] = []
    prov: List[Provenance] = []
    for c in candidates[:top_k]:
        nid = c.get("neo4j_id"); txt = c.get("text") or ""
        pieces.append(f"NODE (id={nid}):\n{txt}")
        prov.append({"neo4j_id": nid, "chunk_id": None, "score": float(c.get("_hybrid", c.get("_vec_norm", 0.0)))})
    context = "\n\n---\n\n".join(pieces)
    prompt = f"USE ONLY THE CONTEXT BELOW. Cite provenance as neo4j_id.\n\nCONTEXT:\n{context}\n\nUSER QUERY:\n{query_text}\n"
    llm_out = None
    if LLM_URL:
        try:
            if LLM_TYPE == "tgi":
                payload = {"inputs": prompt, "parameters": {"max_new_tokens": 512}}
                r = _http.post(LLM_URL, json=payload); r.raise_for_status(); d = r.json()
                if isinstance(d, dict) and "results" in d and d["results"]:
                    llm_out = d["results"][0].get("generated_text") or d["results"][0].get("text")
                elif isinstance(d, dict) and "generated_text" in d:
                    llm_out = d.get("generated_text")
                else:
                    llm_out = d
            else:
                r = _http.post(LLM_URL, json={"prompt": prompt, "max_tokens": 512}); r.raise_for_status(); llm_out = r.json()
        except Exception as e:
            log.exception("LLM call failed: %s", e); llm_out = {"error": str(e)}
    return {"prompt": prompt, "provenance": prov, "records": [{k: v for k, v in c.items() if k != "props"} for c in candidates[:top_k]], "llm": llm_out}

def close():
    try: _http.close()
    except: pass
    try: qdrant_client.close()
    except: pass
    try: neo4j_driver.close()
    except: pass

if __name__ == "__main__":
    out = hybrid_query("How do I install User manual B?", top_k=5)
    print("---- PROMPT ----\n", out["prompt"])
    print("\n---- PROVENANCE ----")
    for p in out["provenance"]:
        print(p)
    print("\n---- LLM ----\n", out["llm"])
    close()
