
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