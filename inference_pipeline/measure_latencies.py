#!/usr/bin/env python3
# measure_latencies.py
"""
Measure and summarize latencies for:
 - DNS/TCP/TLS/TTFB/TOTAL (via curl if present, else httpx approximations)
 - Qdrant: get_collection and a small vector search/query_points
 - Neo4j: verify_connectivity and a simple fulltext query
 - Embed HTTP endpoint (health-check)
 - Ray Serve handle lookup (best-effort)
Outputs a JSON summary and a small human-readable table.

Environment variables used (defaults shown):
  QDRANT_URL (http://127.0.0.1:6333)
  QDRANT_API_KEY
  COLLECTION (my_collection)
  VECTOR_DIM (768)
  NEO4J_URI (bolt://localhost:7687)
  NEO4J_USER (neo4j)
  NEO4J_PASSWORD ("")
  EMBED_HTTP_URL (http://127.0.0.1:8003/embed_onxx)
  RERANK_HTTP_URL (http://127.0.0.1:8003/rerank_onxx)
  REPEAT (1)
"""
from __future__ import annotations
import os
import time
import json
import shutil
import subprocess
import logging
import argparse
from typing import Optional, Dict, Any, List

LOG = logging.getLogger("measure")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

# env config
QDRANT_URL = os.getenv("QDRANT_URL", "http://127.0.0.1:6333")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY", None)
COLLECTION = os.getenv("COLLECTION", "my_collection")
VECTOR_DIM = int(os.getenv("VECTOR_DIM", "768"))
NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "")
EMBED_HTTP_URL = os.getenv("EMBED_HTTP_URL", "http://127.0.0.1:8003/embed_onxx")
RERANK_HTTP_URL = os.getenv("RERANK_HTTP_URL", "http://127.0.0.1:8003/rerank_onxx")
REPEAT = int(os.getenv("REPEAT", "1"))

# timeouts
SHORT_TO = 5.0
MED_TO = 10.0

# optional imports
try:
    import httpx
except Exception:
    httpx = None

try:
    from qdrant_client import QdrantClient
except Exception:
    QdrantClient = None

try:
    from neo4j import GraphDatabase
except Exception:
    GraphDatabase = None

try:
    from ray import serve
except Exception:
    serve = None

def run_curl_timing(url: str, method: str = "GET", data: Optional[str] = None, headers: Optional[List[str]] = None, timeout: float = SHORT_TO) -> Dict[str, Any]:
    """Use curl to get detailed timing. Returns dict with time_namelookup, time_connect, time_appconnect, time_starttransfer, time_total."""
    if not shutil.which("curl"):
        raise RuntimeError("curl not available")
    fmt = "DNS:%{time_namelookup}s TCP:%{time_connect}s TLS:%{time_appconnect}s TTFB:%{time_starttransfer}s TOTAL:%{time_total}s HTTP_CODE:%{http_code}\\n"
    cmd = ["curl", "-s", "-o", "/dev/null", "-w", fmt, "--max-time", str(int(timeout)), url]
    if headers:
        for h in headers:
            cmd += ["-H", h]
    if method.upper() != "GET":
        cmd += ["-X", method]
    if data:
        cmd += ["-d", data, "-H", "Content-Type: application/json"]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=timeout + 1)
        s = out.decode("utf-8", errors="replace").strip()
        parts = s.split()
        res = {}
        for p in parts:
            if ":" not in p:
                continue
            k, v = p.split(":", 1)
            if k == "HTTP_CODE":
                res["http_code"] = int(v)
            else:
                try:
                    res[k] = float(v.rstrip("s"))
                except Exception:
                    res[k] = v
        return res
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"curl failed: {e.output.decode('utf-8', errors='replace')}")
    except Exception as e:
        raise

def httpx_timing_simple(url: str, method: str = "GET", json_payload: Optional[dict] = None, timeout: float = SHORT_TO) -> Dict[str, Any]:
    """Fallback timing using httpx. Measures time-to-headers and total time."""
    if httpx is None:
        raise RuntimeError("httpx not available")
    t0 = time.time()
    with httpx.Client(timeout=timeout) as c:
        r = c.request(method, url, json=json_payload)
        t_headers = r.elapsed.total_seconds() if r.elapsed else None
        # measure total including reading content (though .text already read)
        content = r.content
    t1 = time.time()
    return {"TTFB": t_headers, "TOTAL": t1 - t0, "http_code": r.status_code, "content_len": len(content)}

def measure_http_endpoint(url: str, post_json: Optional[dict] = None, timeout: float = MED_TO) -> Dict[str, Any]:
    """Try curl for detailed timing else httpx fallback."""
    out = {"url": url, "ok": False}
    try:
        if shutil.which("curl"):
            data = None
            headers = None
            method = "GET" if post_json is None else "POST"
            if post_json is not None:
                import json as _j
                data = _j.dumps(post_json)
            try:
                curl_res = run_curl_timing(url, method=method, data=data, headers=headers, timeout=timeout)
                out.update({"ok": True, "curl": curl_res})
                return out
            except Exception as e:
                LOG.debug("curl timing failed: %s", e)
        # fallback: httpx
        try:
            res = httpx_timing_simple(url, method="POST" if post_json else "GET", json_payload=post_json, timeout=timeout)
            out.update({"ok": True, "httpx": res})
            return out
        except Exception as e:
            out.update({"error": str(e)})
            return out
    except Exception as e:
        out.update({"error": str(e)})
        return out

def measure_qdrant_client(qurl: str, api_key: Optional[str], collection: str, vector_dim: int, timeout: float = MED_TO) -> Dict[str, Any]:
    res = {"qdrant_url": qurl, "ok": False}
    if QdrantClient is None:
        res["error"] = "qdrant-client not installed"
        return res
    try:
        t0 = time.time()
        client = QdrantClient(url=qurl, api_key=api_key)
        t1 = time.time()
        res["connect"] = t1 - t0
        # get collection info
        t0 = time.time()
        info = client.get_collection(collection_name=collection)
        t1 = time.time()
        res["get_collection_ms"] = (t1 - t0)
        res["collection_info"] = {
            "status": getattr(info, "status", None),
            "vectors_count": getattr(info, "vectors_count", None),
            "payload_schema_keys": list(getattr(info, "payload_schema", {}).keys()) if getattr(info, "payload_schema", None) else None
        }
        # small vector probe - build zero vector with correct dim but avoid huge payload on free tiers
        probe_vec = [0.0] * vector_dim
        t0 = time.time()
        # try query_points first if available
        try:
            if hasattr(client, "query_points"):
                outp = client.query_points(collection_name=collection, query_vector=probe_vec, limit=1)
            else:
                outp = client.search(collection_name=collection, query_vector=probe_vec, limit=1)
            t1 = time.time()
            res["search_ms"] = (t1 - t0)
            res["search_count"] = len(outp) if outp is not None else None
        except Exception as e:
            # record exception but continue
            t1 = time.time()
            res["search_ms"] = (t1 - t0)
            res["search_error"] = str(e)
        client.close()
        res["ok"] = True
    except Exception as e:
        res["error"] = str(e)
    return res

def measure_neo4j(uri: str, user: str, password: str, timeout: float = MED_TO) -> Dict[str, Any]:
    out = {"neo4j_uri": uri, "ok": False}
    if GraphDatabase is None:
        out["error"] = "neo4j driver not installed"
        return out
    try:
        t0 = time.time()
        driver = GraphDatabase.driver(uri, auth=(user, password))
        t1 = time.time()
        out["driver_create_ms"] = t1 - t0
        # verify_connectivity
        t0 = time.time()
        try:
            driver.verify_connectivity()
            out["verify_connectivity_ms"] = time.time() - t0
        except Exception as e:
            out["verify_connectivity_error"] = str(e)
            out["verify_connectivity_ms"] = time.time() - t0
        # run a simple query for timing (non-blocking if empty index returns quickly)
        t0 = time.time()
        try:
            with driver.session() as s:
                r = s.run("CALL db.index.fulltext.queryNodes($index, $q) YIELD node, score RETURN node.chunk_id AS id, score LIMIT 1", index="chunkFulltextIndex", q="health")
                _ = [row for row in r]
            out["sample_query_ms"] = time.time() - t0
        except Exception as e:
            out["sample_query_error"] = str(e)
            out["sample_query_ms"] = time.time() - t0
        driver.close()
        out["ok"] = True
    except Exception as e:
        out["error"] = str(e)
    return out

def measure_embed_health(url: str, timeout: float = MED_TO) -> Dict[str, Any]:
    payload = {"texts": ["health-check"], "max_length": 32}
    return measure_http_endpoint(url, post_json=payload, timeout=timeout)

def measure_rerank_health(url: str, timeout: float = MED_TO) -> Dict[str, Any]:
    payload = {"query": "health", "cands": ["a","b"], "max_length": 64}
    return measure_http_endpoint(url, post_json=payload, timeout=timeout)

def measure_ray_handle(name: str, timeout: float = 10.0) -> Dict[str, Any]:
    out = {"deployment": name, "ok": False}
    if serve is None:
        out["error"] = "ray.serve not importable"
        return out
    t0 = time.time()
    try:
        # best-effort: measure get_deployment_handle path
        if hasattr(serve, "get_deployment_handle"):
            h = serve.get_deployment_handle(name, _check_exists=False)
            t1 = time.time()
            out["get_handle_ms"] = t1 - t0
            # try a quick remote health-check call if feasible
            try:
                t0 = time.time()
                resp = h.remote({"texts": ["health-check"], "max_length": 8})
                # attempt to resolve small
                try:
                    import ray as _ray
                    _ = _ray.get(resp, timeout=5)
                except Exception:
                    pass
                out["handle_call_ms"] = time.time() - t0
            except Exception as e:
                out["handle_call_error"] = str(e)
            out["ok"] = True
            return out
        out["error"] = "serve.get_deployment_handle missing"
        return out
    except Exception as e:
        out["error"] = str(e)
        return out

def run_all_once() -> Dict[str, Any]:
    tstart = time.time()
    summary: Dict[str, Any] = {"meta": {
        "qdrant_url": QDRANT_URL, "neo4j_uri": NEO4J_URI, "embed_url": EMBED_HTTP_URL, "vector_dim": VECTOR_DIM
    }}
    # http endpoints
    summary["embed_http"] = measure_embed_health(EMBED_HTTP_URL)
    summary["rerank_http"] = measure_rerank_health(RERANK_HTTP_URL)
    # qdrant
    summary["qdrant"] = measure_qdrant_client(QDRANT_URL, QDRANT_API_KEY, COLLECTION, VECTOR_DIM)
    # neo4j
    summary["neo4j"] = measure_neo4j(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
    # ray handle
    try:
        summary["ray_embed_handle"] = measure_ray_handle(os.getenv("EMBED_DEPLOYMENT", "embed_onxx"))
    except Exception as e:
        summary["ray_embed_handle"] = {"error": str(e)}
    summary["total_elapsed"] = time.time() - tstart
    return summary

def pretty_print(summary: Dict[str, Any]) -> None:
    print("\n---- LATENCY SUMMARY ----")
    print(json.dumps(summary, indent=2))
    print("\n---- HIGHLIGHTS ----")
    # quick highlights
    def safe(v, k, default="n/a"):
        return v.get(k, default) if isinstance(v, dict) else default
    print(f"Embed HTTP OK: {summary['embed_http'].get('ok')}, details: {summary['embed_http'].get('curl') or summary['embed_http'].get('httpx') or summary['embed_http'].get('error')}")
    print(f"Qdrant connect: {summary['qdrant'].get('connect', 'n/a'):.3f}s get_collection: {summary['qdrant'].get('get_collection_ms', 'n/a'):.3f}s search: {summary['qdrant'].get('search_ms', 'n/a'):.3f}s")
    print(f"Neo4j driver_create: {summary['neo4j'].get('driver_create_ms', 'n/a'):.3f}s verify: {summary['neo4j'].get('verify_connectivity_ms', 'n/a'):.3f}s sample_query: {summary['neo4j'].get('sample_query_ms', 'n/a'):.3f}s")
    print(f"Total measure elapsed: {summary.get('total_elapsed', 0.0):.3f}s")

def main():
    parser = argparse.ArgumentParser(description="Measure latencies for Qdrant/Neo4j/embed endpoints.")
    parser.add_argument("--repeat", "-r", type=int, default=REPEAT, help="Number of measurement iterations")
    parser.add_argument("--out", "-o", default=None, help="Write JSON summary to file")
    args = parser.parse_args()
    results = []
    for i in range(args.repeat):
        LOG.info("run %d/%d", i + 1, args.repeat)
        s = run_all_once()
        results.append(s)
        # small sleep between runs to avoid rate limits
        time.sleep(0.25)
    # aggregate simple stats (take last as canonical)
    final = {"runs": results, "last": results[-1] if results else None}
    if args.out:
        with open(args.out, "w") as f:
            json.dump(final, f, indent=2)
        LOG.info("wrote JSON to %s", args.out)
    pretty_print(final["last"] if final["last"] else {})
    # also print aggregated minimal JSON to stdout
    print("\nJSON-RESULT:")
    print(json.dumps(final, indent=2))

if __name__ == "__main__":
    main()
