HYBRID_QUERY="${HYBRID_QUERY:-What is RAGOps?}" TOPK="${TOPK:-5}" python3 - <<'PY'
import os, json, traceback
QUERY = os.environ.get("HYBRID_QUERY", "What is RAGOps?")
TOPK = int(os.environ.get("TOPK", "5"))

QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY")
COLLECTION = os.environ.get("COLLECTION", "my_collection")
PREFER_GRPC = os.environ.get("PREFER_GRPC", "false").lower() in ("1","true","yes")

NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "")

EMBED_DEPLOYMENT = os.environ.get("EMBED_DEPLOYMENT", "embed_onxx")
EMBED_TIMEOUT = int(os.environ.get("EMBED_TIMEOUT", "60"))
INDEXING_EMBEDDER_MAX_TOKENS = int(os.environ.get("INDEXING_EMBEDDER_MAX_TOKENS", "512"))
VECTOR_DIM = int(os.environ.get("VECTOR_DIM", "768"))

def resolve_handle_response(resp_obj, timeout=EMBED_TIMEOUT):
    import ray
    # direct
    if isinstance(resp_obj, (dict, list)):
        return resp_obj
    # DeploymentResponse style (.result)
    try:
        if hasattr(resp_obj, "result") and callable(getattr(resp_obj, "result")):
            return resp_obj.result()
    except Exception:
        pass
    # ObjectRef
    try:
        return ray.get(resp_obj, timeout=timeout)
    except Exception:
        return {"_resolve_error": str(traceback.format_exc()), "repr": repr(resp_obj)}

def embed_text_with_serve(text):
    import ray
    from ray import serve
    ray.init(address=os.getenv("RAY_ADDRESS","auto"), ignore_reinit_error=True)
    if hasattr(serve, "get_deployment_handle"):
        handle = serve.get_deployment_handle(EMBED_DEPLOYMENT, app_name="default", _check_exists=False)
    elif hasattr(serve, "get_deployment"):
        dep = serve.get_deployment(EMBED_DEPLOYMENT)
        handle = dep.get_handle(sync=False)
    elif hasattr(serve, "get_handle"):
        handle = serve.get_handle(EMBED_DEPLOYMENT, sync=False)
    else:
        raise RuntimeError("Serve handle API not available")
    payload = {"texts":[text], "max_length": INDEXING_EMBEDDER_MAX_TOKENS}
    resp_obj = handle.remote(payload)
    resp = resolve_handle_response(resp_obj, timeout=EMBED_TIMEOUT)
    if not isinstance(resp, dict) or "vectors" not in resp:
        raise RuntimeError(f"embed returned unexpected response: {resp}")
    v = resp["vectors"]
    if not v:
        raise RuntimeError("embed returned empty vectors")
    return v[0]

def qdrant_search(vector, topk=TOPK):
    from qdrant_client import QdrantClient
    c = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY, prefer_grpc=PREFER_GRPC)
    # prefer simple search signature (no params kw that may not exist)
    try:
        hits = c.search(collection_name=COLLECTION, query_vector=vector, limit=topk, with_payload=True)
    except Exception as e_search:
        # fallback to query_points if available
        try:
            hits = c.query_points(collection_name=COLLECTION, query_vector=vector, limit=topk, with_payload=True)
        except Exception as e_qp:
            raise RuntimeError(f"Qdrant search/query_points failed: {e_search} / {e_qp}")
    out = []
    for h in hits:
        payload = getattr(h, "payload", None) or (h.payload if hasattr(h, "payload") else {}) 
        pid = getattr(h, "id", None) or getattr(h, "point_id", None) or payload.get("chunk_id")
        score = getattr(h, "score", None)
        out.append({"point_id": pid, "score": score, "payload": dict(payload) if isinstance(payload, dict) else str(payload)})
    return out

def fetch_full_texts_from_neo4j(hits):
    from neo4j import GraphDatabase
    drv = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    out = []
    with drv.session() as s:
        for h in hits:
            payload = h.get("payload", {}) or {}
            chunk_id = payload.get("chunk_id") or h.get("point_id")
            rec = None
            if chunk_id:
                rec = s.run("MATCH (c:Chunk {chunk_id:$cid}) RETURN c.chunk_id AS chunk_id, c.text AS text, c.qdrant_id AS qid, c.token_count AS token_count, c.file_name AS file_name LIMIT 1", cid=chunk_id).single()
            if not rec:
                rec = s.run("MATCH (c:Chunk {qdrant_id:$qid}) RETURN c.chunk_id AS chunk_id, c.text AS text, c.qdrant_id AS qid, c.token_count AS token_count, c.file_name AS file_name LIMIT 1", qid=h.get("point_id")).single()
            if rec:
                out.append({"chunk_id": rec["chunk_id"], "qdrant_id": rec["qid"], "text": rec["text"], "token_count": rec["token_count"], "file_name": rec["file_name"], "score": h.get("score"), "payload": payload})
            else:
                out.append({"chunk_id": chunk_id or h.get("point_id"), "qdrant_id": h.get("point_id"), "text": None, "score": h.get("score"), "payload": payload})
    drv.close()
    return out

def pretty(obj):
    return json.dumps(obj, indent=2, ensure_ascii=False)

try:
    vec = embed_text_with_serve(QUERY)
    results = qdrant_search(vec, topk=TOPK)
    enriched = fetch_full_texts_from_neo4j(results)
    print("HYBRID QUERY:", QUERY)
    print("\nQdrant hits (raw):\n", pretty(results))
    print("\nEnriched results (Neo4j full text):\n", pretty(enriched))
except Exception as e:
    print("ERROR:", str(e))
    traceback.print_exc()
    raise SystemExit(2)
PY
