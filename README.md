






# Ray Serve / ONNX deployment settings
export RAY_ADDRESS="auto"
export RAY_NAMESPACE="default"

# Deployment names (must match query/ingest)
export EMBED_DEPLOYMENT="embed_onxx"
export RERANK_HANDLE_NAME="rerank_onxx"

# ONNX / model locations
export ONNX_USE_CUDA="false"                      # "true" to use CUDAExecutionProvider
export MODEL_DIR_EMBED="/models/gte-modernbert-base"
export MODEL_DIR_RERANK="/models/gte-reranker-modernbert-base"
export ONNX_EMBED_PATH="${MODEL_DIR_EMBED}/onnx/model_int8.onnx"
export ONNX_EMBED_TOKENIZER_PATH="${MODEL_DIR_EMBED}/tokenizer.json"
export ONNX_RERANK_PATH="${MODEL_DIR_RERANK}/onnx/model_int8.onnx"
export ONNX_RERANK_TOKENIZER_PATH="${MODEL_DIR_RERANK}/tokenizer.json"

# Replicas / GPU / runtime knobs
export EMBED_REPLICAS="1"
export RERANK_REPLICAS="1"
export EMBED_GPU_PER_REPLICA="0"   # number of GPUs per embed replica (string)
export RERANK_GPU_PER_REPLICA="0"  # number of GPUs per rerank replica (string)

# ONNX/ORT tuning
export MAX_RERANK="256"
export ORT_INTRA_THREADS="1"
export ORT_INTER_THREADS="1"

# Logging / debug
export LOG_LEVEL="INFO"


# Ray / local data
export RAY_ADDRESS="auto"
export RAY_NAMESPACE="default"
export DATA_IN_LOCAL="true"
export LOCAL_DIR_PATH="data/chunked/"

# Deployments / embedding
export EMBED_DEPLOYMENT="embed_onxx"
export INDEXING_EMBEDDER_MAX_TOKENS="512"   # truncate/truncation for embedder during indexing

# Embedding / batching / vector dim
export VECTOR_DIM="768"
export BATCH_SIZE="64"
export EMBED_SUB_BATCH="32"
export EMBED_TIMEOUT="60"

# Qdrant settings
export QDRANT_URL="http://127.0.0.1:6333"
export QDRANT_API_KEY=""                    # set if you use Qdrant API key
export PREFER_GRPC="true"
export COLLECTION="my_collection"          # Qdrant collection name

# Neo4j settings (replace password!)
export NEO4J_URI="bolt://127.0.0.1:7687"
export NEO4J_USER="neo4j"
export NEO4J_PASSWORD="ReplaceWithStrongPass!"

# Retry / misc
export RETRY_ATTEMPTS="3"
export RETRY_BASE_SECONDS="0.5"
export LOG_LEVEL="INFO"

# Ray / handles
export RAY_ADDRESS="auto"
export RAY_NAMESPACE="default"
export EMBED_DEPLOYMENT="embed_onxx"        # used as EMBED_HANDLE_NAME in query
export RERANK_HANDLE_NAME="rerank_onxx"

# Qdrant
export QDRANT_URL="http://127.0.0.1:6333"
export QDRANT_API_KEY=""                    # set if required
export PREFER_GRPC="true"
export COLLECTION="my_collection"

# Neo4j
export NEO4J_URI="bolt://127.0.0.1:7687"
export NEO4J_USER="neo4j"
export NEO4J_PASSWORD="ReplaceWithStrongPass!"
export NEO4J_FULLTEXT_INDEX="chunkTextIndex"

# Vector / retrieval tuning
export VECTOR_DIM="768"
export TOP_K="5"
export TOP_VECTOR_CHUNKS="100"
export TOP_BM25_CHUNKS="100"

# Two-stage & graph expansion RRF knobs
export FIRST_STAGE_RRF_K="60"
export MAX_CHUNKS_FOR_GRAPH_EXPANSION="20"
export GRAPH_EXPANSION_HOPS="1"
export SECOND_STAGE_RRF_K="60"

# Cross-encoder / LLM limits
export MAX_CHUNKS_TO_CROSSENCODER="64"
export MAX_CHUNKS_TO_LLM="8"

# Token limits for inference
export INFERENCE_EMBEDDER_MAX_TOKENS="64"
export CROSS_ENCODER_MAX_TOKENS="600"

# Misc
export RERANK_TOP="50"
export HTTP_TIMEOUT="30"
export RETRY_ATTEMPTS="3"
export RETRY_BASE_SECONDS="0.5"
export LOG_LEVEL="INFO"
