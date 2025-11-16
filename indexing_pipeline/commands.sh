
echo "SHOW CONSTRAINTS;" | cypher-shell -a "$NEO4J_URI" -u "$NEO4J_USER" -p "$NEO4J_PASSWORD"

# Delete all nodes & relationships (this will remove everything)
echo "MATCH (n) DETACH DELETE n;" | cypher-shell -a "$NEO4J_URI" -u "$NEO4J_USER" -p "$NEO4J_PASSWORD"

# If you want to drop constraints you discovered above, run (replace <constraint_name>):
# echo "DROP CONSTRAINT <constraint_name> IF EXISTS;" | cypher-shell -a "$NEO4J_URI" -u "$NEO4J_USER" -p "$NEO4J_PASSWORD"

###############################################################################
# 2) Neo4j - Python (useful inside a venv; installs neo4j-driver)
###############################################################################
# pip install neo4j
python3 - <<'PY'
from neo4j import GraphDatabase
import os
uri = os.environ.get("NEO4J_URI")
user = os.environ.get("NEO4J_USER")
pwd  = os.environ.get("NEO4J_PASSWORD")
drv = GraphDatabase.driver(uri, auth=(user, pwd))
with drv.session() as s:
    print("Deleting all nodes/relationships...")
    s.run("MATCH (n) DETACH DELETE n")
    # list constraints for manual review
    try:
        res = s.run("SHOW CONSTRAINTS")
        print("CONSTRAINTS:")
        for r in res:
            print(r)
    except Exception as e:
        print("SHOW CONSTRAINTS failed:", e)
drv.close()
PY

###############################################################################
# 3) Qdrant - HTTP (curl + jq)
# - List collections
# - Delete a specific collection
# - Delete all collections (loop) -- useful for "wipe everything"
###############################################################################

# List collections
curl -sS -H "Api-Key: $QDRANT_API_KEY" "$QDRANT_URL/api/collections" | jq .

# Delete a single collection (replace my_collection with your name)
curl -sS -X DELETE -H "Api-Key: $QDRANT_API_KEY" "$QDRANT_URL/api/collections/my_collection" | jq .

# Delete ALL collections (loop). Requires jq. This will remove all collections.
for coll in $(curl -sS -H "Api-Key: $QDRANT_API_KEY" "$QDRANT_URL/api/collections" | jq -r '.collections[].name'); do
  echo "Deleting collection: $coll"
  curl -sS -X DELETE -H "Api-Key: $QDRANT_API_KEY" "$QDRANT_URL/api/collections/$coll" | jq .
done

###############################################################################
# 4) Qdrant - Python (preferred when you have qdrant-client installed)
###############################################################################
# pip install qdrant-client
python3 - <<'PY'
import os
from qdrant_client import QdrantClient
qurl = os.environ.get("QDRANT_URL")
qkey = os.environ.get("QDRANT_API_KEY") or None
client = QdrantClient(url=qurl, api_key=qkey)
info = client.get_collections()
print("Collections:", [c.name for c in info.collections])
for c in info.collections:
    name = c.name
    print("Deleting collection:", name)
    client.delete_collection(collection_name=name)
print("Done.")
PY

curl -X GET "$QDRANT_URL/collections" -H "api-key: $QDRANT_API_KEY" \
| jq -r '.result.collections[].name' \
| xargs -I{} curl -X DELETE "$QDRANT_URL/collections/{}" -H "api-key: $QDRANT_API_KEY"

cd indexing_pipeline
docker build -t indexing_pipeline_cpu:v1 -f Dockerfile .


URL="${NEO4J_URI%/}/db/neo4j/tx/commit"
BODY='{"statements":[{"statement":"MATCH (n) DETACH DELETE n"}]}'

if [ -n "${NEO4J_API_KEY:-}" ]; then
  curl -fsS -X POST "$URL" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $NEO4J_API_KEY" \
    -d "$BODY"
else
  if [ -z "${NEO4J_USER:-}" ] || [ -z "${NEO4J_PASSWORD:-}" ]; then
    echo "ERROR: No auth provided. Set NEO4J_API_KEY or both NEO4J_USER and NEO4J_PASSWORD."
    exit 1
  fi
  curl -fsS -X POST "$URL" \
    -H "Content-Type: application/json" \
    -u "${NEO4J_USER}:${NEO4J_PASSWORD}" \
    -d "$BODY"
fi

echo "Request sent. Check response above for success or errors."


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