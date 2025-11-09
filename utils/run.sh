ray stop --force || true && sudo rm -rf /tmp/ray/ && ray start --head && python3 infra/rayserve_models_cpu.py


curl -sS -X POST "http://127.0.0.1:8003/" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Explain very detailed about MLOps",
    "params": {
      "max_tokens": 1000,
      "temperature": 0.7,
      "top_p": 0.9
    }
  }' | jq .


# --- CPU / BLAS / OpenMP tuning (must be set BEFORE starting Python/Ray) ---
export OMP_NUM_THREADS=4
export OPENBLAS_NUM_THREADS=4
export MKL_NUM_THREADS=4
export VECLIB_MAXIMUM_THREADS=4

# --- ONNX Runtime threading ---
export ORT_INTRA_THREADS=4
export ORT_INTER_THREADS=1

# --- Embedding / Rerank (light-weight ONNX) ---
export EMBED_REPLICAS=1
export EMBED_NUM_CPUS_PER_REPLICA=1
export EMBED_MAX_REPLICAS_PER_NODE=1
export EMBED_BATCH_MAX_SIZE=16
export EMBED_BATCH_WAIT_S=0.05

export RERANK_REPLICAS=1
export RERANK_NUM_CPUS_PER_REPLICA=1
export RERANK_MAX_REPLICAS_PER_NODE=1
export RERANK_BATCH_MAX_SIZE=8
export RERANK_BATCH_WAIT_S=0.05

# --- LLM (llama-cpp-python) defaults: default to 1 replica, pin half the machine to LLM by default ---
export LLM_REPLICAS=1
export LLM_NUM_CPUS_PER_REPLICA=8
export LLM_N_THREADS=6
export LLM_MAX_CONCURRENCY=1
export LLM_MAX_REPLICAS_PER_NODE=2
export START_CORE=0
export CORES_PER_REPLICA=8

# --- Misc / behavior toggles ---
export ENABLE_CROSS_ENCODER=true
export LLM_ENABLE=true

# --- Ray / serve network (optional) ---
export SERVE_HTTP_HOST=127.0.0.1
export SERVE_HTTP_PORT=8003
