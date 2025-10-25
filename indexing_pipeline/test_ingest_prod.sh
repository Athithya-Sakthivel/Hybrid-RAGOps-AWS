


cat > /tmp/prod_ready_check.py <<'PY'
#!/usr/bin/env python3
import os, sys, json, urllib.request, urllib.parse, sqlite3, subprocess, traceback
from textwrap import indent

QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333").rstrip("/")
COLLECTION = os.getenv("QDRANT_COLLECTION", os.getenv("COLLECTION", "my_collection"))
SQLITE_DB = os.getenv("SQLITE_STATE_DB", "ingest_state.db")
LOCAL_DIR = os.getenv("LOCAL_DIR_PATH", "data/chunked/")
NEO4J_PW = os.getenv("NEO4J_PASSWORD", "ReplaceWithStrongPass!")

def http_get(path):
    url = QDRANT_URL + path
    req = urllib.request.Request(url, headers={"Accept":"application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)

def run_cmd(cmd):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as e:
        return 3, "", str(e)

def print_section(title, text):
    print("\n--- " + title + " ---")
    if isinstance(text, (dict, list)):
        print(indent(json.dumps(text, indent=2, ensure_ascii=False), "  "))
    else:
        print(indent(str(text), "  "))

ok = True
print("PROD-READINESS CHECK\n")

# Qdrant checks
try:
    coll_list = http_get("/collections")
    print_section("Qdrant: collections", coll_list.get("result"))
    info = http_get(f"/collections/{urllib.parse.quote(COLLECTION)}")
    res = info.get("result", {})
    print_section(f"Qdrant: collection '{COLLECTION}' summary", {
        "points_count": res.get("points_count"),
        "indexed_vectors_count": res.get("indexed_vectors_count"),
        "segments_count": res.get("segments_count"),
        "on_disk_payload": res.get("config",{}).get("params",{}).get("on_disk_payload"),
        "hnsw_config": res.get("config",{}).get("hnsw_config"),
        "optimizer_config": res.get("config",{}).get("optimizer_config"),
        "payload_schema": res.get("payload_schema")
    })
    pc = res.get("points_count") or 0
    iv = res.get("indexed_vectors_count") or 0
    hnsw = (res.get("config") or {}).get("hnsw_config") or {}
    opt = (res.get("config") or {}).get("optimizer_config") or {}
    if pc == 0:
        print("WARN: collection has zero points.")
        ok = False
    if iv == 0 and pc > 1000:
        print("WARN: indexed_vectors_count == 0 while points > 1000. Build HNSW or adjust optimizer.")
        ok = False
    if hnsw.get("m") == 0:
        print("WARN: hnsw_config.m == 0 (HNSW disabled). Recommended for bulk-ingest pattern only.")
    fst = hnsw.get("full_scan_threshold")
    if fst is not None and pc >= fst:
        print("INFO: points >= full_scan_threshold. Qdrant will use HNSW (good).")
    if opt.get("indexing_threshold") and opt.get("indexing_threshold") > pc * 2:
        print("NOTE: optimizer.indexing_threshold is high relative to points. Consider lowering after ingest to trigger indexing.")
except Exception as e:
    print_section("Qdrant ERROR", traceback.format_exc())
    ok = False

# Neo4j checks
neo_checks = {}
try:
    # prefer docker container 'neo4j' then local cypher-shell
    rc, out, err = run_cmd(["docker","ps","--filter","name=neo4j","--format","{{.Names}}"])
    use_docker = "neo4j" in out
    if use_docker:
        shell = ["docker","exec","-i","neo4j","cypher-shell","-u","neo4j","-p", NEO4J_PW]
    else:
        shell = ["cypher-shell","-u","neo4j","-p", NEO4J_PW]
    def cy(q):
        code, out, err = run_cmd(shell + [q])
        if code != 0:
            return {"error": err or out}
        return out
    neo_checks["SHOW INDEXES"] = cy("SHOW INDEXES;")
    neo_checks["SHOW CONSTRAINTS"] = cy("SHOW CONSTRAINTS;")
    neo_checks["COUNT Documents"] = cy("MATCH (d:Document) RETURN count(d) AS docs;")
    neo_checks["COUNT Chunks"] = cy("MATCH (c:Chunk) RETURN count(c) AS chunks;")
    neo_checks["Chunks with qdrant_id (non-null)"] = cy("MATCH (c:Chunk) WHERE c.qdrant_id IS NOT NULL RETURN count(c) AS with_id;")
    neo_checks["Chunks missing qdrant_id"] = cy("MATCH (c:Chunk) WHERE c.qdrant_id IS NULL RETURN count(c) AS missing_id;")
    print_section("Neo4j checks (raw outputs)", neo_checks)
    # interpret constraints
    cons = neo_checks.get("SHOW CONSTRAINTS")
    if isinstance(cons, str) and "document_id" not in cons.lower() and "chunk_id" not in cons.lower():
        print("WARN: uniqueness constraints for Document.document_id and Chunk.chunk_id not found. Add them for idempotency.")
        ok = False
except Exception as e:
    print_section("Neo4j ERROR", traceback.format_exc())
    ok = False

# SQLite state DB checks
try:
    if os.path.exists(SQLITE_DB):
        conn = sqlite3.connect(SQLITE_DB)
        cur = conn.cursor()
        def one(q): 
            try:
                r = cur.execute(q).fetchone()
                return r[0] if r else None
            except Exception as ex:
                return str(ex)
        total_chunks = one("SELECT count(*) FROM chunks;")
        qdrant_done = one("SELECT count(*) FROM chunks WHERE qdrant_status='done';")
        neo_done = one("SELECT count(*) FROM chunks WHERE neo4j_status='done';")
        pending = one("SELECT count(*) FROM chunks WHERE qdrant_status!='done';")
        missing_qid = one("SELECT count(*) FROM chunks WHERE qdrant_point_id IS NULL OR qdrant_point_id='';")
        print_section("SQLite state DB summary", {
            "db_file": SQLITE_DB,
            "total_chunks": total_chunks,
            "qdrant_done": qdrant_done,
            "neo4j_done": neo_done,
            "pending_qdrant": pending,
            "missing_qdrant_point_id": missing_qid
        })
        conn.close()
        if isinstance(total_chunks,int) and total_chunks>0 and (missing_qid and missing_qid>0):
            print("WARN: some chunks lack qdrant_point_id in state DB; reconciliation needed.")
            ok = False
    else:
        print_section("SQLite", f"{SQLITE_DB} not present. Durable state missing.")
        ok = False
except Exception as e:
    print_section("SQLite ERROR", traceback.format_exc())
    ok = False

# File system checks
try:
    files = []
    if os.path.isdir(LOCAL_DIR):
        files = sorted([f for f in os.listdir(LOCAL_DIR) if f.endswith(('.json','.jsonl'))])
    print_section("Local chunk files", {"local_dir": LOCAL_DIR, "file_count": len(files), "sample_files": files[:10]})
    if len(files)==0:
        print("WARN: no chunk files found in local dir.")
        ok = False
except Exception as e:
    print_section("FS ERROR", traceback.format_exc())
    ok = False

# Final verdict
print("\n=== SUMMARY ===")
if ok:
    print("PASS: Basic production readiness checks passed. Key items to confirm: HNSW index built, Neo4j constraints exist, backups and monitoring configured.")
else:
    print("FAIL: Issues detected. See warnings above for remediation steps.")
    print("\nSuggested next steps (minimal):")
    print(" - If Qdrant indexed_vectors_count == 0 and you have >1k points: enable/build HNSW (m>0, ef_construct) or lower optimizer.indexing_threshold.")
    print(" - Add Neo4j uniqueness constraints for Document.document_id and Chunk.chunk_id.")
    print(" - Reconcile chunks missing qdrant_point_id between SQLite, Qdrant and Neo4j.")
    print(" - Ensure backups and monitoring for Qdrant and Neo4j.")
print('\\nDone.')
PY
python3 /tmp/prod_ready_check.py



# set HNSW params (m, ef_construct) and lower indexing_threshold so optimizer builds index sooner
curl -sS -X PATCH "http://localhost:6333/collections/my_collection" -H "Content-Type: application/json" -d '{
  "hnsw_config": {"m":16, "ef_construct":200, "full_scan_threshold":10000, "on_disk": false},
  "optimizer_config": {"indexing_threshold": 1000, "vacuum_min_vector_number": 1000}
}' | jq

# Confirm HNSW query-time ef during searches, When querying, set ef on client or via request param to trade recall/latency
curl -sS -X POST "http://localhost:6333/collections/my_collection/points/search" \
  -H "Content-Type: application/json" \
  -d '{"vector":[0.0,0.0,0.0], "limit":10, "with_payload":true, "params": {"ef": 200}}' | jq

python3 /tmp/prod_ready_check.py



# Force Qdrant to build HNSW now (one-shot)
curl -sS -X PATCH "http://localhost:6333/collections/my_collection" -H "Content-Type: application/json" -d '{
  "optimizer_config": {"indexing_threshold": 1, "vacuum_min_vector_number": 1},
  "hnsw_config": {"m":16, "ef_construct":200, "on_disk": false, "max_indexing_threads": 4}
}' | jq

# If you prefer the bulk-ingest pattern (safer for large datasets)
# Create collection with HNSW disabled or very high indexing_threshold, ingest all vectors, 
# then PATCH to enable HNSW with proper m/ef_construct. Docs recommend this for memory management. Example:

# create with indexing deferred (if recreating)
curl -sS -X PUT "http://localhost:6333/collections/my_collection" -H "Content-Type: application/json" -d '{
  "vectors": {"size":768,"distance":"Cosine","hnsw_config":{"m":0,"ef_construct":100}},
  "optimizer_config":{"indexing_threshold":100000}
}' | jq

# after ingest: enable HNSW
curl -sS -X PATCH "http://localhost:6333/collections/my_collection" -H "Content-Type: application/json" -d '{
  "hnsw_config": {"m":16, "ef_construct":200, "max_indexing_threads":4}
}' | jq


# stop DB (required for consistent dump in Community)
docker stop neo4j

# create backup dir on host
mkdir -p ./neo4j/backups

# run neo4j-admin from the official image that contains the CLI (match your version)
docker run --rm \
  -v "$PWD/neo4j/data:/data" \
  -v "$PWD/neo4j/backups:/backups" \
  neo4j:5.19.0 \
  bash -lc "bin/neo4j-admin database dump neo4j --to-path=/backups && ls -lh /backups"

# restart DB
docker start neo4j


# WARNING: deletes collection and all data. Delete first, then create with deferred indexing (bulk-ingest pattern):
curl -sS -X DELETE "http://localhost:6333/collections/my_collection" | jq

# create with HNSW disabled / indexing deferred
curl -sS -X PUT "http://localhost:6333/collections/my_collection" -H "Content-Type: application/json" -d '{
  "vectors": {"size":768,"distance":"Cosine","hnsw_config":{"m":0,"ef_construct":100}},
  "optimizer_config":{"indexing_threshold":100000},
  "params": {"on_disk_payload": true}
}' | jq
