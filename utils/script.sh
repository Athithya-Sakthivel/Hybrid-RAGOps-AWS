python3 -m .venv/bin/activate && source .venv/bin/activate && pip install -r indexing_pipeline/requirements.txt && \
pip install -r infra/requirements.txt && pip install -r requirements.txt

sudo mkdir -p /models
sudo chmod -R 777 /models

ray stop && ray start --head && python3 infra/rayserve_onnx.py

docker run -d --name ollama -v ollama_models:/root/.ollama -p 11434:11434 -e OLLAMA_HOST=0.0.0.0 ollama/ollama:latest
docker exec -it ollama ollama pull smollm:135m  # smollm:360m llama3.2

docker run -d --name qdrant -p 6333:6333 -p 6334:6334 qdrant/qdrant:latest

# compact final block — copy/paste (replace password)
docker rm -f neo4j 2>/dev/null || true
mkdir -p ./neo4j/data ./neo4j/logs
sudo chown -R "$(id -u):$(id -g)" ./neo4j


# (in a new terminal after Neo4j is up) export envs your ingest expects and run ingest
export NEO4J_URI="bolt://127.0.0.1:7687"
export NEO4J_USER="neo4j"
export NEO4J_PASSWORD="${NEO4J_PW}"
export QDRANT_URL="http://127.0.0.1:6333"

# optional performance knobs
export BATCH_SIZE=64
export EMBED_SUB_BATCH=16

export NEO4J_PW="ReplaceWithStrongPass!"   # <<-- REPLACE with a strong password
docker run -d --name neo4j \
  -p 7474:7474 -p 7687:7687 \
  -e NEO4J_AUTH="neo4j/${NEO4J_PW}" \
  -e NEO4J_server_memory_heap_initial__size=512M \
  -e NEO4J_server_memory_heap_max__size=1G \
  -e NEO4J_server_memory_pagecache_size=512M \
  -v "$PWD/neo4j/data:/data" \
  -v "$PWD/neo4j/logs:/logs" \
  neo4j:5.19.0

# run ingestion (same venv where Ray/Serve are running)
python3 indexing_pipeline/ingest.py



# list collections (REST)
curl -s 'http://localhost:6333/collections' | jq .

# check collection info
curl -s 'http://localhost:6333/collections/my_collection' | jq .


export RAY_ADDRESS="auto"
export ONNX_USE_CUDA="false"
export RAY_ACCEL_ENV_VAR_OVERRIDE_ON_ZERO=0

export MODEL_DIR_EMBED="/models/gte-modernbert-base"
export ONNX_EMBED_PATH="/models/gte-modernbert-base/onnx/model_int8.onnx"
export ONNX_EMBED_TOKENIZER_PATH="/models/gte-modernbert-base/tokenizer.json"
export ONNX_EMBED_TOKENIZER_CONFIG_PATH="/models/gte-modernbert-base/tokenizer_config.json"

export MODEL_DIR_RERANK="/models/gte-reranker-modernbert-base"
export ONNX_RERANK_PATH="/models/gte-reranker-modernbert-base/onnx/model_int8.onnx"
export ONNX_RERANK_TOKENIZER_PATH="/models/gte-reranker-modernbert-base/tokenizer.json"
export ONNX_RERANK_TOKENIZER_CONFIG_PATH="/models/gte-reranker-modernbert-base/tokenizer_config.json"

export EMBED_REPLICAS="1"
export RERANK_REPLICAS="1"
export EMBED_GPU_PER_REPLICA="0"
export RERANK_GPU_PER_REPLICA="0"
export MAX_RERANK="256"
export ORT_INTRA_THREADS="2"
export ORT_INTER_THREADS="2"
export EMBED_DEPLOYMENT="embed_onxx"
export EMBED_HANDLE_NAME="embed_onxx"

export INGEST_SOURCE=""
export NEO4J_URI="bolt://localhost:7687"
export NEO4J_USER="neo4j"
export NEO4J_PASSWORD="neo4j"
export SERVE_WAIT="60"
export RETRY_ATTEMPTS="3"
export RETRY_BASE_SECONDS="0.5"
export LOG_LEVEL="DEBUG"
export TOP_K="5"
export RERANK_TOP="20"
export HYBRID_ALPHA="0.7"
export LLM_URL="http://localhost:11434/api/generate"
export LLM_TYPE="ollama"
export LLM_MODEL="smollm:360m"
export HTTP_TIMEOUT="30"



