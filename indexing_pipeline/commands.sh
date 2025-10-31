
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