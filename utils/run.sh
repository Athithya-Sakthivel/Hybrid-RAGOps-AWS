# Neo4j (set password, persistent volumes, JVM/DB memory tuned via env; adjust password and sizes to your host)
docker rm -f neo4j 2>/dev/null || true
mkdir -p ./neo4j/data ./neo4j/logs
sudo chown -R "$(id -u):$(id -g)" ./neo4j || true
docker run -d --name neo4j --restart unless-stopped -p 7474:7474 -p 7687:7687 \
  -e NEO4J_AUTH="neo4j/ReplaceWithStrongPass!" \
  -e NEO4J_dbms_memory_heap_initial__size=512M \
  -e NEO4J_dbms_memory_heap_max__size=1G \
  -e NEO4J_server_memory_pagecache_size=512M \
  -v "$PWD/neo4j/data:/data" -v "$PWD/neo4j/logs:/logs" neo4j:5.19.0

# Qdrant (persistent storage, ports 6333/6334, restart policy)
docker rm -f qdrant 2>/dev/null || true
mkdir -p ./qdrant/storage
docker run -d --name qdrant --restart unless-stopped -p 6333:6333 -p 6334:6334 -v "$PWD/qdrant/storage:/qdrant/storage" qdrant/qdrant:v1.15.0
