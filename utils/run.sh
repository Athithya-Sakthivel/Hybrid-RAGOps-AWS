#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"
if [ -f .venv/bin/activate ]; then
  # activate virtualenv if present (no error if not)
  # shellcheck disable=SC1091
  . .venv/bin/activate
fi
export LOG_LEVEL="${LOG_LEVEL:-INFO}"
export EMBED_DEPLOYMENT="${EMBED_DEPLOYMENT:-embed_onxx}"
export RERANK_HANDLE_NAME="${RERANK_HANDLE_NAME:-rerank_onxx}"
export ONNX_USE_CUDA="${ONNX_USE_CUDA:-false}"
export MODEL_DIR_EMBED="${MODEL_DIR_EMBED:-/models/gte-modernbert-base}"
export MODEL_DIR_RERANK="${MODEL_DIR_RERANK:-/models/gte-reranker-modernbert-base}"
export ONNX_EMBED_PATH="${ONNX_EMBED_PATH:-${MODEL_DIR_EMBED}/onnx/model_int8.onnx}"
export ONNX_EMBED_TOKENIZER_PATH="${ONNX_EMBED_TOKENIZER_PATH:-${MODEL_DIR_EMBED}/tokenizer.json}"
export ONNX_RERANK_PATH="${ONNX_RERANK_PATH:-${MODEL_DIR_RERANK}/onnx/model_int8.onnx}"
export ONNX_RERANK_TOKENIZER_PATH="${ONNX_RERANK_TOKENIZER_PATH:-${MODEL_DIR_RERANK}/tokenizer.json}"
export EMBED_REPLICAS="${EMBED_REPLICAS:-1}"
export RERANK_REPLICAS="${RERANK_REPLICAS:-1}"
export EMBED_GPU_PER_REPLICA="${EMBED_GPU_PER_REPLICA:-0}"
export RERANK_GPU_PER_REPLICA="${RERANK_GPU_PER_REPLICA:-0}"
export MAX_RERANK="${MAX_RERANK:-256}"
export ORT_INTRA_THREADS="${ORT_INTRA_THREADS:-1}"
export ORT_INTER_THREADS="${ORT_INTER_THREADS:-1}"
export INDEXING_EMBEDDER_MAX_TOKENS="${INDEXING_EMBEDDER_MAX_TOKENS:-512}"
export INFERENCE_EMBEDDER_MAX_TOKENS="${INFERENCE_EMBEDDER_MAX_TOKENS:-64}"
export CROSS_ENCODER_MAX_TOKENS="${CROSS_ENCODER_MAX_TOKENS:-600}"
export ENABLE_CROSS_ENCODER="${ENABLE_CROSS_ENCODER:-true}"
export DATA_IN_LOCAL="${DATA_IN_LOCAL:-true}"
export LOCAL_DIR_PATH="${LOCAL_DIR_PATH:-data/chunked/}"
export SNIPPET_MAX_CHARS="${SNIPPET_MAX_CHARS:-512}"
export VECTOR_DIM="${VECTOR_DIM:-768}"
export BATCH_SIZE="${BATCH_SIZE:-64}"
export EMBED_SUB_BATCH="${EMBED_SUB_BATCH:-32}"
export EMBED_TIMEOUT="${EMBED_TIMEOUT:-60}"
export QDRANT_URL="${QDRANT_URL:-http://localhost:6333}"
export QDRANT_API_KEY="${QDRANT_API_KEY:-}"
export PREFER_GRPC="${PREFER_GRPC:-true}"
export COLLECTION="${COLLECTION:-my_collection}"
export NEO4J_URI="${NEO4J_URI:-bolt://localhost:7687}"
export NEO4J_USER="${NEO4J_USER:-neo4j}"
export NEO4J_PW="${NEO4J_PW:-ReplaceWithStrongPass!}"
export RETRY_ATTEMPTS="${RETRY_ATTEMPTS:-3}"
export RETRY_BASE_SECONDS="${RETRY_BASE_SECONDS:-0.5}"
export TOP_K="${TOP_K:-5}"
export TOP_VECTOR_CHUNKS="${TOP_VECTOR_CHUNKS:-100}"
export TOP_BM25_CHUNKS="${TOP_BM25_CHUNKS:-100}"
export FIRST_STAGE_RRF_K="${FIRST_STAGE_RRF_K:-60}"
export MAX_CHUNKS_FOR_GRAPH_EXPANSION="${MAX_CHUNKS_FOR_GRAPH_EXPANSION:-20}"
export GRAPH_EXPANSION_HOPS="${GRAPH_EXPANSION_HOPS:-1}"
export SECOND_STAGE_RRF_K="${SECOND_STAGE_RRF_K:-60}"
export MAX_CHUNKS_TO_CROSSENCODER="${MAX_CHUNKS_TO_CROSSENCODER:-64}"
export MAX_CHUNKS_TO_LLM="${MAX_CHUNKS_TO_LLM:-8}"
export ENABLE_METADATA_CHUNKS="${ENABLE_METADATA_CHUNKS:-false}"
export MAX_METADATA_CHUNKS="${MAX_METADATA_CHUNKS:-50}"
export RERANK_TOP="${RERANK_TOP:-50}"
export HTTP_TIMEOUT="${HTTP_TIMEOUT:-30}"
export RETRY_ATTEMPTS="${RETRY_ATTEMPTS:-3}"
export RETRY_BASE_SECONDS="${RETRY_BASE_SECONDS:-0.5}"
export RAY_ADDRESS="${RAY_ADDRESS:-auto}"
if [ -n "${RAY_NAMESPACE:-}" ]; then
  export RAY_NAMESPACE
else
  unset RAY_NAMESPACE || true
fi
mkdir -p logs run neo4j/data neo4j/logs
sudo chown -R "$(id -u):$(id -g)" ./neo4j || true
echo "[info] stopping any existing local ray cluster"
ray stop || true
echo "[info] starting ray head"
ray start --head --dashboard-host 127.0.0.1 || true
sleep 2
echo "[info] exported RAY_ADDRESS=${RAY_ADDRESS:-(unset)} RAY_NAMESPACE=${RAY_NAMESPACE:-(unset)}"
if [ -f run/rayserve.pid ] && kill -0 "$(cat run/rayserve.pid)" 2>/dev/null; then
  echo "[info] rayserve (python infra/rayserve_onnx.py) already running pid=$(cat run/rayserve.pid)"
else
  echo "[info] launching rayserve (infra/rayserve_onnx.py) in background"
  nohup python3 ./infra/rayserve_onnx.py > logs/rayserve.log 2>&1 &
  echo $! > run/rayserve.pid
  sleep 2
fi
echo "[info] qdrant docker container"
docker rm -f qdrant 2>/dev/null || true
docker run -d --name qdrant -p 6333:6333 -p 6334:6334 qdrant/qdrant:latest || true
echo "[info] neo4j docker container"
docker rm -f neo4j 2>/dev/null || true
docker run -d --name neo4j -p 7474:7474 -p 7687:7687 -e NEO4J_AUTH="neo4j/${NEO4J_PW}" -e NEO4J_server_memory_heap_initial__size=512M -e NEO4J_server_memory_heap_max__size=1G -e NEO4J_server_memory_pagecache_size=512M -v "$PWD/neo4j/data:/data" -v "$PWD/neo4j/logs:/logs" neo4j:5.19.0 || true
if [ "${ENABLE_CROSS_ENCODER:-true}" = "true" ]; then
  echo "[info] ollama container (optional)"
  docker rm -f ollama 2>/dev/null || true
  docker run -d --name ollama -v ollama_models:/root/.ollama -p 11434:11434 -e OLLAMA_HOST=0.0.0.0 ollama/ollama:latest || true
fi
echo "[info] waiting a few seconds for qdrant/neo4j to become ready (adjust if needed)"
sleep 6
echo "[info] running ingestion (this will wait for embed handle if Serve not ready)"
python3 ./indexing_pipeline/ingest.py || (echo "[error] ingest.py failed" && exit 1)
echo "[info] ingestion finished"
echo "[info] you can now run inference pipeline or tests"
