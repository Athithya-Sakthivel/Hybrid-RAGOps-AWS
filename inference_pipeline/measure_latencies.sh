cat > /tmp/e2e_latency.py <<'PY'
#!/usr/bin/env python3
import os, time, json, subprocess, traceback

OUT = {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "results": {}, "diagnosis": ""}

QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333").rstrip("/")
COLLECTION = os.getenv("COLLECTION", "my_collection")
NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "")
VECTOR_DIM = int(os.getenv("VECTOR_DIM", "768"))
EMBED_DEPLOYMENT = os.getenv("EMBED_DEPLOYMENT", "embed_onxx")
EMBED_TIMEOUT = int(os.getenv("EMBED_TIMEOUT", "60"))
PREFER_GRPC_ENV = os.getenv("PREFER_GRPC", "false").lower() in ("1","true","yes")

def run(cmd):
    try:
        out = subprocess.check_output(cmd, shell=True, stderr=subprocess.STDOUT, text=True)
        return out.strip()
    except subprocess.CalledProcessError as e:
        return f"ERR: {e.output.strip()}"

# 1) DNS + ping (best-effort)
host = QDRANT_URL.split("://")[-1].split("/")[0]
OUT["results"]["ping"] = {}
OUT["results"]["ping"]["host"] = host
ping = run(f"ping -c 4 {host} 2>/dev/null || echo 'ping-not-allowed'")
OUT["results"]["ping"]["raw"] = ping

# 2) curl timings (connect / starttransfer / total)
curl_cmd = f'curl -sS -o /dev/null -w "%{{time_connect}} %{{time_starttransfer}} %{{time_total}}" "{QDRANT_URL}/collections/{COLLECTION}"'
curl_out = run(curl_cmd)
try:
    connect_t, starttransfer_t, total_t = [float(x) for x in curl_out.split()[:3]]
except Exception:
    connect_t = starttransfer_t = total_t = None
OUT["results"]["http_timing"] = {"connect_s": connect_t, "starttransfer_s": starttransfer_t, "total_s": total_t, "raw": curl_out}

# 3) Qdrant vector search (http vs grpc)
OUT["results"]["qdrant"] = {}
for prefer in (False, True):
    key = "grpc" if prefer else "http"
    try:
        from qdrant_client import QdrantClient
        c = QdrantClient(url=os.getenv("QDRANT_URL","http://localhost:6333"), api_key=os.getenv("QDRANT_API_KEY"), prefer_grpc=prefer)
        vec = [0.0]*VECTOR_DIM
        t0 = time.perf_counter()
        # using search; client may warn about deprecation but works for timing
        hits = c.search(collection_name=COLLECTION, query_vector=vec, limit=3, with_payload=True)
        t1 = time.perf_counter()
        OUT["results"]["qdrant"][key] = {"hits": len(hits), "elapsed_s": round(t1-t0,3)}
    except Exception as e:
        OUT["results"]["qdrant"][key] = {"error": str(e)}

# 4) Neo4j sample query timing
try:
    from neo4j import GraphDatabase
    drv = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    with drv.session() as s:
        t0 = time.perf_counter()
        rec = s.run("MATCH (c:Chunk) WHERE c.qdrant_id IS NOT NULL RETURN c.chunk_id LIMIT 1").single()
        t1 = time.perf_counter()
        summary = None
        try:
            summary = s.run("RETURN 1").consume()
        except Exception:
            summary = None
    OUT["results"]["neo4j"] = {"sample_row_present": bool(rec), "elapsed_s": round(t1-t0,3), "summary_meta": str(summary)}
    try:
        drv.close()
    except Exception:
        pass
except Exception as e:
    OUT["results"]["neo4j"] = {"error": str(e)}

# 5) Ray embedder probe (warm + single embed)
OUT["results"]["embedder"] = {}
try:
    import ray
    from ray import serve
    ray.init(address=os.getenv("RAY_ADDRESS","auto"), ignore_reinit_error=True)
    name = EMBED_DEPLOYMENT
    # get handle with fallbacks
    handle = None
    try:
        handle = serve.get_deployment_handle(name, app_name="default", _check_exists=False)
    except Exception:
        try:
            dep = serve.get_deployment(name)
            handle = dep.get_handle(sync=False)
        except Exception:
            handle = None
    if handle is None:
        OUT["results"]["embedder"]["error"] = "no serve handle"
    else:
        # warm
        for i in range(2):
            t0 = time.perf_counter()
            r = handle.remote({"texts":["warm-up"], "max_length": int(os.getenv("INFERENCE_EMBEDDER_MAX_TOKENS","64"))})
            try:
                out = r.result()
            except Exception:
                out = ray.get(r, timeout=EMBED_TIMEOUT)
            t1 = time.perf_counter()
            OUT["results"]["embedder"].setdefault("warm_times", []).append(round(t1-t0,3))
        # actual embed latency and vector dim check
        t0 = time.perf_counter()
        r = handle.remote({"texts":["health-check"], "max_length": int(os.getenv("INFERENCE_EMBEDDER_MAX_TOKENS","64"))})
        try:
            res = r.result()
        except Exception:
            res = ray.get(r, timeout=EMBED_TIMEOUT)
        t1 = time.perf_counter()
        vecs = res.get("vectors") if isinstance(res, dict) else None
        OUT["results"]["embedder"]["embed_elapsed_s"] = round(t1-t0,3)
        OUT["results"]["embedder"]["vectors_returned"] = bool(vecs)
        OUT["results"]["embedder"]["first_vector_len"] = len(vecs[0]) if vecs else None
except Exception as e:
    OUT["results"]["embedder"] = {"error": str(e)}

# 6) simple diagnosis
qc = OUT["results"].get("qdrant",{})
q_http = qc.get("http",{}).get("elapsed_s") or qc.get("grpc",{}).get("elapsed_s") or OUT["results"].get("qdrant",{}).get("http",{})
neo = OUT["results"].get("neo4j",{}).get("elapsed_s") or 0.0
embed = OUT["results"].get("embedder",{}).get("embed_elapsed_s") or 0.0
curl_total = OUT["results"].get("http_timing",{}).get("total_s") or 0.0

# pick best measured qdrant time
q_times = [v.get("elapsed_s") for v in qc.values() if isinstance(v,dict) and v.get("elapsed_s")]
q_best = min(q_times) if q_times else None

network_dominant = False
if q_best and curl_total and (q_best + (neo or 0.0) + curl_total) > max(0.0001, embed*4):
    network_dominant = True
OUT["diagnosis"] = "network-dominant" if network_dominant else "balanced"
OUT["meta"] = {"qdrant_best_s": q_best, "neo4j_s": neo, "embed_s": embed, "curl_total_s": curl_total}

print(json.dumps(OUT, indent=2, ensure_ascii=False))
PY

python3 /tmp/e2e_latency.py
