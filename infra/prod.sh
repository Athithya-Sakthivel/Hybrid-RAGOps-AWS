# env_prod.sh - PRODUCTION, GPU per-replica enforced by GPU_NODE token
export RSV_RAY_ADDRESS="auto"                 # connect to cluster head
export RSV_RAY_NAMESPACE="prod"
export RSV_SERVE_HOST="0.0.0.0"
export RSV_SERVE_PORT="8003"

# ONNX embed/rerank - choose whether they need GPU. We reserve GPU_NODE only if true.
export RSV_ONNX_USE_CUDA="true"
export RSV_EMBED_DEPLOYMENT="embed_onxx"
export RSV_RERANK_DEPLOYMENT="rerank_onxx"
export RSV_MODEL_DIR_EMBED="/models/gte-modernbert-base"
export RSV_MODEL_DIR_RERANK="/models/gte-reranker-modernbert-base"
export RSV_ONNX_EMBED_PATH="$RSV_MODEL_DIR_EMBED/onnx/model_int8.onnx"
export RSV_ONNX_EMBED_TOKENIZER_PATH="$RSV_MODEL_DIR_EMBED/tokenizer.json"
export RSV_ONNX_RERANK_PATH="$RSV_MODEL_DIR_RERANK/onnx/model_int8.onnx"
export RSV_ONNX_RERANK_TOKENIZER_PATH="$RSV_MODEL_DIR_RERANK/tokenizer.json"
export RSV_EMBED_REPLICAS="1"
export RSV_RERANK_REPLICAS="1"
# set GPU reservation where needed; 1 ensures node-level placement (paired with GPU_NODE)
export RSV_EMBED_GPU="0"           # embedder often can run on CPU in prod
export RSV_RERANK_GPU="0"          # reranker can be CPU or GPU depending on throughput

export RSV_ENABLE_CROSS_ENCODER="true"

# TGI LLM wrapper - enforce one TGI replica per GPU node
export RSV_TGI_REPLICAS="1"
export RSV_TGI_GPUS_PER_REPLICA="1"
export RSV_TGI_DEVICE="cuda"
export RSV_TGI_LAUNCHER_CMD="text-generation-launcher"
export RSV_TGI_HOST="127.0.0.1"
export RSV_TGI_PORT="8081"
export RSV_TGI_MODEL_ID="Qwen/Qwen3-0.6B"
# tune these conservatively and measure. Example starting values for 24GB GPU:
export RSV_TGI_EXTRA_ARGS="--max-input-tokens 1024 --max-total-tokens 2048 --max-batch-size 8 --max-batch-total-tokens 16384 --batch-timeout-ms 10000 --enable-streaming --enable-tracing"
# --enable-tracing
# the per-node custom resource must match autoscaler YAML GPU_NODE
export RSV_GPU_NODE_RESOURCE="GPU_NODE"

# restart policy helpers
export RSV_TGI_MAX_RESTARTS="6"
export RSV_TGI_RESTART_BACKOFF="2.0"

# onnxruntime tuning
export RSV_ORT_INTRA_THREADS="1"
export RSV_ORT_INTER_THREADS="1"
