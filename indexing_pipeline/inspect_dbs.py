from __future__ import annotations
import os, sys, json, time, traceback
from textwrap import indent

QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY", None)
PREFER_GRPC = os.getenv("PREFER_GRPC", "false").lower() in ("1", "true", "yes")
COLLECTION = os.getenv("COLLECTION", "my_collection")

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "")

SQLITE_DB = os.getenv("SQLITE_STATE_DB", "ingest_state.db")
LOCAL_DIR = os.getenv("LOCAL_DIR_PATH", "data/chunked/")

EMBED_DEPLOYMENT = os.getenv("EMBED_DEPLOYMENT", "embed_onxx")
EMBED_TIMEOUT = int(os.getenv("EMBED_TIMEOUT", "60"))
VECTOR_DIM = int(os.getenv("VECTOR_DIM", "768"))

OUT = {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "ok": True, "qdrant": {}, "neo4j": {}, "sqlite": {}, "embedder": {}, "local_files": {}}

def safe_val(v):
    try:
        json.dumps(v)
        return v
    except Exception:
        try:
            if hasattr(v, "__dict__"):
                return {k: safe_val(getattr(v, k)) for k in sorted(v.__dict__.keys())}
        except Exception:
            pass
        return repr(v)

# Qdrant
try:
    from qdrant_client import QdrantClient
    qc = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY, prefer_grpc=PREFER_GRPC)
    cols = [c.name for c in qc.get_collections().collections]
    OUT["qdrant"]["connected"] = True
    OUT["qdrant"]["collections"] = cols
    if COLLECTION in cols:
        OUT["qdrant"]["collection_exists"] = True
        try:
            info = qc.get_collection(collection_name=COLLECTION)
            # defensive extraction
            def g(o, *names):
                for n in names:
                    val = getattr(o, n, None)
                    if val is not None:
                        return val
                return None
            points = g(info, "points_count") or g(getattr(info, "result", None), "points_count")
            indexed = g(info, "indexed_vectors_count") or g(getattr(info, "result", None), "indexed_vectors_count")
            optimizer = g(info, "optimizer_status") or g(getattr(info, "result", None), "optimizer_status")
            segments = g(info, "segments_count") or g(getattr(info, "result", None), "segments_count")
            cfg = g(info, "config") or g(getattr(info, "result", None), "config")
            hnsw = None
            if cfg is not None:
                h = getattr(cfg, "hnsw_config", None) or cfg
                hnsw = {"m": getattr(h, "m", None), "ef_construct": getattr(h, "ef_construct", None), "full_scan_threshold": getattr(h, "full_scan_threshold", None), "on_disk": getattr(h, "on_disk", None)}
            OUT["qdrant"]["collection_info"] = {"points_count": points, "indexed_vectors_count": indexed, "optimizer_status": optimizer, "segments_count": segments, "hnsw": hnsw}
        except Exception:
            OUT["qdrant"]["collection_info_error"] = traceback.format_exc()
        # samples
        try:
            samples = []
            try:
                for p in qc.scroll(collection_name=COLLECTION, with_payload=True, limit=5):
                    payload = getattr(p, "payload", None) or (p.payload if hasattr(p, "payload") else None) or p
                    samples.append(safe_val(payload))
            except Exception:
                try:
                    hits = qc.search(collection_name=COLLECTION, query_vector=[0.0]*VECTOR_DIM, limit=5, with_payload=True)
                    for h in hits:
                        payload = getattr(h, "payload", None) or (h.payload if hasattr(h, "payload") else None) or h
                        samples.append(safe_val(payload))
                except Exception as e:
                    samples.append({"error": "no sample API", "exc": str(e)})
            OUT["qdrant"]["sample_payloads"] = samples
        except Exception:
            OUT["qdrant"]["sample_payloads_error"] = traceback.format_exc()
    else:
        OUT["qdrant"]["collection_exists"] = False
except Exception:
    OUT["qdrant"]["connected"] = False
    OUT["qdrant"]["error"] = traceback.format_exc()
    OUT["ok"] = False

# Neo4j
try:
    from neo4j import GraphDatabase
    drv = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    with drv.session() as s:
        try:
            constraints = [dict(r) for r in s.run("SHOW CONSTRAINTS")]
        except Exception:
            constraints = traceback.format_exc()
        try:
            indexes = [dict(r) for r in s.run("SHOW INDEXES")]
        except Exception:
            indexes = traceback.format_exc()
        try:
            docs_count = s.run("MATCH (d:Document) RETURN count(d) AS c").single()["c"]
        except Exception:
            docs_count = None
        try:
            chunks_count = s.run("MATCH (c:Chunk) RETURN count(c) AS c").single()["c"]
        except Exception:
            chunks_count = None
        def sample_node(label):
            try:
                rec = s.run(f"MATCH (n:{label}) RETURN keys(n) AS k, n AS n LIMIT 1").single()
                if not rec:
                    return {"exists": False}
                keys = rec["k"]
                node = rec["n"]
                vals = {}
                for k in keys:
                    try:
                        vals[k] = node.get(k)
                    except Exception:
                        vals[k] = "<unreadable>"
                return {"exists": True, "keys": keys, "sample_values": vals}
            except Exception:
                return {"error": traceback.format_exc()}
        doc_sample = sample_node("Document")
        chunk_sample = sample_node("Chunk")
        try:
            rels = s.run("MATCH (d:Document)-[:HAS_CHUNK]->(c:Chunk) RETURN d.document_id AS doc, c.chunk_id AS chunk LIMIT 5").data()
        except Exception:
            rels = traceback.format_exc()
    drv.close()
    OUT["neo4j"] = {"connected": True, "documents": docs_count, "chunks": chunks_count, "constraints": safe_val(constraints), "indexes": safe_val(indexes), "Document_sample": doc_sample, "Chunk_sample": chunk_sample, "sample_relations": rels}
except Exception:
    OUT["neo4j"] = {"connected": False, "error": traceback.format_exc()}
    OUT["ok"] = False

# SQLite state
try:
    import sqlite3
    if os.path.exists(SQLITE_DB):
        conn = sqlite3.connect(SQLITE_DB)
        cur = conn.cursor()
        try:
            total_chunks = cur.execute("SELECT count(*) FROM chunks").fetchone()[0]
        except Exception:
            total_chunks = None
        try:
            q_done = cur.execute("SELECT count(*) FROM chunks WHERE qdrant_status='done'").fetchone()[0]
        except Exception:
            q_done = None
        try:
            n_done = cur.execute("SELECT count(*) FROM chunks WHERE neo4j_status='done'").fetchone()[0]
        except Exception:
            n_done = None
        conn.close()
        OUT["sqlite"] = {"path": SQLITE_DB, "exists": True, "total_chunks": total_chunks, "qdrant_done": q_done, "neo4j_done": n_done}
    else:
        OUT["sqlite"] = {"path": SQLITE_DB, "exists": False}
        OUT["ok"] = False
except Exception:
    OUT["sqlite"] = {"error": traceback.format_exc()}
    OUT["ok"] = False

# Ray Serve embedder
try:
    import ray
    from ray import serve
    ray.init(address=os.getenv("RAY_ADDRESS", "auto"), ignore_reinit_error=True)
    handle = None
    if hasattr(serve, "get_deployment_handle"):
        handle = serve.get_deployment_handle(EMBED_DEPLOYMENT, app_name="default", _check_exists=False)
    elif hasattr(serve, "get_deployment"):
        dep = serve.get_deployment(EMBED_DEPLOYMENT)
        handle = dep.get_handle(sync=False)
    elif hasattr(serve, "get_handle"):
        handle = serve.get_handle(EMBED_DEPLOYMENT, sync=False)
    if handle is None:
        raise RuntimeError("Serve handle API not available")
    payload = {"texts": ["health-check"], "max_length": int(os.getenv("INDEXING_EMBEDDER_MAX_TOKENS", "512"))}
    resp_obj = handle.remote(payload)
    resolved = None
    try:
        resolved = ray.get(resp_obj, timeout=EMBED_TIMEOUT)
    except Exception as e_get:
        try:
            if isinstance(resp_obj, (dict, list)):
                resolved = resp_obj
            elif hasattr(resp_obj, "result") and callable(getattr(resp_obj, "result")):
                resolved = resp_obj.result()
            else:
                resolved = {"unresolved": repr(resp_obj), "ray_get_error": str(e_get)}
        except Exception as e2:
            resolved = {"unresolved": repr(resp_obj), "ray_get_error": str(e_get), "result_error": str(e2)}
    if isinstance(resolved, dict) and "vectors" in resolved:
        vs = resolved.get("vectors") or []
        OUT["embedder"] = {"deployment": EMBED_DEPLOYMENT, "vectors_count": len(vs), "first_vector_length": len(vs[0]) if vs else None, "max_length_used": resolved.get("max_length_used")}
    else:
        OUT["embedder"] = {"deployment": EMBED_DEPLOYMENT, "response": safe_val(resolved)}
except Exception:
    OUT["embedder"] = {"connected": False, "error": traceback.format_exc()}
    OUT["ok"] = False

# Local files
try:
    if os.path.isdir(LOCAL_DIR):
        files = sorted([f for f in os.listdir(LOCAL_DIR) if f.endswith((".json", ".jsonl"))])
        OUT["local_files"] = {"dir": LOCAL_DIR, "count": len(files), "sample": files[:20]}
    else:
        OUT["local_files"] = {"dir": LOCAL_DIR, "exists": False}
except Exception:
    OUT["local_files"] = {"error": traceback.format_exc()}

# Print
def pretty_print(d):
    print("PROD INSPECT SUMMARY\n")
    print("OK:", d.get("ok", False), " time:", d.get("time"))
    print("\n-- Local files --")
    print(indent(json.dumps(d.get("local_files", {}), indent=2, default=str, ensure_ascii=False), "  "))
    print("\n-- Qdrant --")
    print(indent(json.dumps(d.get("qdrant", {}), indent=2, default=str, ensure_ascii=False), "  "))
    print("\n-- Neo4j --")
    print(indent(json.dumps(d.get("neo4j", {}), indent=2, default=str, ensure_ascii=False), "  "))
    print("\n-- SQLite --")
    print(indent(json.dumps(d.get("sqlite", {}), indent=2, default=str, ensure_ascii=False), "  "))
    print("\n-- Embedder --")
    print(indent(json.dumps(d.get("embedder", {}), indent=2, default=str, ensure_ascii=False), "  "))


pretty_print(OUT)
sys.exit(0 if OUT.get("ok", False) else 2)