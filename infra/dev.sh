# env_dev.sh - local dev, CPU-only
export RSV_RAY_ADDRESS="auto"                    # empty -> local Ray
export RSV_RAY_NAMESPACE="dev"
export RSV_SERVE_HOST="127.0.0.1"
export RSV_SERVE_PORT="8003"

# ONNX embed/rerank (CPU)
export RSV_ONNX_USE_CUDA="false"
export RSV_EMBED_DEPLOYMENT="embed_onxx"
export RSV_RERANK_DEPLOYMENT="rerank_onxx"
export RSV_MODEL_DIR_EMBED="/models/gte-modernbert-base"    # change to local path
export RSV_MODEL_DIR_RERANK="/models/gte-reranker-modernbert-base"
export RSV_ONNX_EMBED_PATH="$RSV_MODEL_DIR_EMBED/onnx/model_int8.onnx"
export RSV_ONNX_EMBED_TOKENIZER_PATH="$RSV_MODEL_DIR_EMBED/tokenizer.json"
export RSV_ONNX_RERANK_PATH="$RSV_MODEL_DIR_RERANK/onnx/model_int8.onnx"
export RSV_ONNX_RERANK_TOKENIZER_PATH="$RSV_MODEL_DIR_RERANK/tokenizer.json"
export RSV_EMBED_REPLICAS="1"
export RSV_RERANK_REPLICAS="1"   # skip reranker in quick dev
export RSV_EMBED_GPU="0"
export RSV_RERANK_GPU="0"
export RSV_ENABLE_CROSS_ENCODER="false"

# TGI LLM wrapper (CPU dev or stub)
export RSV_TGI_REPLICAS="1"
export RSV_TGI_GPUS_PER_REPLICA="0"
export RSV_TGI_DEVICE="cpu"
export RSV_TGI_LAUNCHER_CMD="text-generation-launcher"   # install TGI in dev environment or use a small stub binary
export RSV_TGI_HOST="127.0.0.1"
export RSV_TGI_PORT="8081"
export RSV_TGI_MODEL_ID="Qwen/Qwen3-0.6B"     # Orion-zhen/Qwen3-0.6B-AWQ,            
 # if download required set HF token separately
export RSV_TGI_EXTRA_ARGS="--max-input-tokens 128 --max-total-tokens 256 --max-batch-size 4 --max-batch-total-tokens 1024"

# per-node resource token name (DEV doesn't need GPU_NODE, but keep default)
export RSV_GPU_NODE_RESOURCE="GPU_NODE"

# tuning helpers
export RSV_ORT_INTRA_THREADS="1"
export RSV_ORT_INTER_THREADS="1"
export RAY_SERVE_QUEUE_LENGTH_RESPONSE_DEADLINE_S=5.0


docker run --rm --privileged --cap-add=sys_nice \
  --ipc=host --shm-size=16g \
  -p 8081:80 \
  -v "/workspace/models/qwen/Qwen3-0.6B-AWQ":/model:ro \
  -e OMP_NUM_THREADS=4 \
  -e MKL_NUM_THREADS=4 \
  -e ONEDNN_MAX_CPU_ISA=AVX2 \
  ghcr.io/huggingface/text-generation-inference:latest-intel-cpu \
  --model-id /model \
  --hostname 0.0.0.0 \
  --port 80 \
  --validation-workers 1 \
  --max-batch-size 1 \
  --max-input-tokens 128 \
  --max-total-tokens 256 \
  --max-batch-total-tokens 1024




ray stop --force || true && sudo rm -rf /tmp/ray/ && ray start --head && python3 infra/rayserve_models.py
