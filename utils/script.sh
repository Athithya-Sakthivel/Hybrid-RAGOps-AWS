
docker run -d \
  --name qdrant \
  -p 6333:6333 \
  -v qdrant_storage:/qdrant/storage \
  qdrant/qdrant



docker run -d \
  --name neo4j \
  -p 7474:7474 -p 7687:7687 \
  -e NEO4J_AUTH=none \
  -v neo4j_data:/data \
  neo4j:5

docker rm -f qdrant && docker rm -f neo4j
docker run -d --name qdrant -p 6333:6333 -p 6334:6334 qdrant/qdrant:latest
docker run -d --name neo4j -p 7474:7474 -p 7687:7687 -e NEO4J_AUTH=neo4j/neo4j neo4j:5