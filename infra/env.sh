export LOG_LEVEL=INFO                     # recommended INFO; set DEBUG when debugging startup issues
export RAY_ADDRESS="auto"                        # empty for local head; set "auto" or "IP:6379" to connect to existing cluster head
export RAY_NAMESPACE=default               # optional namespace to isolate resources
export SERVE_HTTP_HOST=0.0.0.0             # 0.0.0.0 to accept external traffic; use 127.0.0.1 for dev
export SERVE_HTTP_PORT=8003                # Serve HTTP port; change if conflict

export EMBED_DEPLOYMENT=embed_onxx         # embed deployment name
export RERANK_HANDLE_NAME=rerank_onxx      # reranker deployment name
export ONNX_USE_CUDA=false                 # set true if onnxruntime-gpu installed and you want GPU inference
export MODEL_DIR_EMBED=/opt/models/embed   # local path on node with embed model (ensure s3 sync or AMI copy)
export MODEL_DIR_RERANK=/opt/models/rerank # local path for reranker model
export ONNX_EMBED_PATH=${MODEL_DIR_EMBED}/onnx/model_int8.onnx  # path to ONNX embed model
export ONNX_EMBED_TOKENIZER_PATH=${MODEL_DIR_EMBED}/tokenizer.json
export ONNX_RERANK_PATH=${MODEL_DIR_RERANK}/onnx/model_int8.onnx
export ONNX_RERANK_TOKENIZER_PATH=${MODEL_DIR_RERANK}/tokenizer.json

export EMBED_REPLICAS=1                    # recommended 1 per CPU node; increase if you have more CPU nodes
export RERANK_REPLICAS=1                   # recommended 0 or 1; set >1 if heavy load and many CPU nodes
export EMBED_GPU_PER_REPLICA=0             # 0 -> CPU; set 1.0 if you want embed to run on GPU nodes
export RERANK_GPU_PER_REPLICA=0            # 0 -> CPU; set 1.0 if reranker should use GPU

export MAX_RERANK=256                      # candidate limit for reranker; higher -> more compute/latency
export ORT_INTRA_THREADS=1                 # tune per-node; increase if many CPU cores and throughput needed
export ORT_INTER_THREADS=1                 # tune per-node; increase for high parallelism

export INDEXING_EMBEDDER_MAX_TOKENS=512    # token limit for indexing; lower if you need to cap memory
export CROSS_ENCODER_MAX_TOKENS=600        # token limit for reranker; increase for long passages
export ENABLE_CROSS_ENCODER=true           # true to deploy reranker; false to skip reranker

export EMBED_MAX_REPLICAS_PER_NODE=1       # recommended 1: ensures at most one embed actor per node (safe default)
export RERANKER_MAX_REPLICAS_PER_NODE=1    # recommended 1: at most one reranker actor per node
export LLM_MAX_REPLICAS_PER_NODE=1         # recommended 1 for large LLMs; increase only if GPU memory allows multiple copies

export LLM_ENABLE=true                     # true to enable LLM deployment; set false to skip LLMServer
export LLM_PATH=/opt/models/qwen-3-0.6b    # local path or cloud path accessible from worker nodes (must be present on GPU nodes)
export LLM_MODEL_ID=qwen-3-0.6b            # logical model id shown to ingress
export LLM_REPLICAS=1                      # number of LLM replicas; match number of GPU nodes you want to occupy
export LLM_GPU_PER_REPLICA=1.0             # GPUs per LLM replica (1.0 typical). Use >1 for multi-GPU replica with parallelism
export LLM_DEPLOYMENT_NAME=llm_server
export LLM_INGRESS_NAME=openai_ingress
export LLM_ENGINE_KWARGS='{}'              # engine kwargs for vLLM (e.g., tensor/pipeline parallelism)
export LLM_USE_RUNTIME_ENV=false           # false recommended when using custom AMI with vllm preinstalled; true for runtime pip installs

# node-role resource labels used by autoscaler (do not change unless you change YAML)
export CPU_NODE_LABEL=CPU_NODE            # label advertised by cpu worker nodes in YAML
export GPU_NODE_LABEL=GPU_NODE            # label advertised by gpu worker nodes in YAML

# quick change guidance:
# - If you want embed/rerank to use GPU nodes, set *_GPU_PER_REPLICA=1 and ensure GPU_NODE label present on worker type.
# - If you want more packing on a GPU (small models), increase EMBED_MAX_REPLICAS_PER_NODE and consider fractional GPU scheduling (advanced).
# - If you have fewer nodes than requested replicas, reduce replica counts or enable autoscaler max_workers to scale up.
