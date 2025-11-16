# Get started

## Prerequesities
 1. Docker enabled on boot and is running
 2. Vscode with `Dev Containers` extension installed
 3. AWS root account or IAM user with admin access for S3, EC2 and IAM role management
 
# STEP 0/3 environment setup

#### Clone the repo and build the devcontainer
```sh 
git clone https://github.com/Athithya-Sakthivel/RAG8s.git && cd RAG8s && code .
ctrl + shift + P -> paste `Dev containers: Rebuild Container` and enter
```

#### This will take 20-30 minutes. If the image matches your system, you are ready to proceed.
![alt text](.devcontainer/env_setup_success.png)

#### Open a new terminal and login to your gh account
```sh
git config --global user.name "Your Name" && git config --global user.email you@example.com
gh auth login

? What account do you want to log into? GitHub.com
? What is your preferred protocol for Git operations? SSH
? Generate a new SSH key to add to your GitHub account? No
? How would you like to authenticate GitHub CLI? Login with a web browser

! First copy your one-time code: <code>
- Press Enter to open github.com in your browser... 
✓ Authentication complete. Press Enter to continue...

```
#### Create a private repo in your gh account
```sh
export REPO_NAME="rag-45"

git remote remove origin 2>/dev/null || true
gh repo create "$REPO_NAME" --private >/dev/null 2>&1
REMOTE_URL="https://github.com/$(gh api user | jq -r .login)/$REPO_NAME.git"
git remote add origin "$REMOTE_URL" 2>/dev/null || true
git branch -M main 2>/dev/null || true
git push -u origin main
git pull
git remote -v
echo "[INFO] A private repo '$REPO_NAME' created and pushed. Only visible from your account."

```


```sh

# aws required
export AWS_ACCESS_KEY_ID="AKIA..."                        
export AWS_SECRET_ACCESS_KEY="..."    
export AWS_REGION="ap-south-1"                        # AWS region to deploy infrastructure (e.g., ap-south-1 for Mumbai)
export S3_BUCKET=e2e-rag-system-42                    # Set any globally unique complex name, Pulumi S3 backend -> s3://$S3_BUCKET/pulumi/
export PULUMI_CONFIG_PASSPHRASE="mypassword"          # Passphrase to encrypt Pulumi secrets (required for headless automation)
export MULTI_AZ_DEPLOYMENT="false"          # true = deploy across multiple AZs (HA, more cost) / false = single AZ (cheaper, simpler)



# aws optional
export VPC_CIDR="10.0.0.0/16"                         # VPC network range (all subnets must fall inside)
export PULUMI_PUBLIC_SUBNET_COUNT="2"                 # 2 or more if multi AZ required. Must match number of CIDRs
export PUBLIC_SUBNET_CIDRS="10.0.1.0/24,10.0.2.0/24"  # CIDR blocks for public subnets in the VPC (must match VPC_CIDR and AZ count)
export MY_SSH_CIDR="203.0.113.42/32"                  # Your IP allowed for SSH access (must be a /32 single IP)





```
### infra 

```sh


export LOG_LEVEL="INFO"                     # (REQUIRED) Logging level for boot/serve; set DEBUG to see detailed model init, WARN/ERROR for quieter production logs
export RAY_ADDRESS="auto"                       # (REQUIRED) Ray cluster address (leave empty for local single-node); set to head node address for multi-node clusters
export RAY_NAMESPACE="ragops"                     # (REQUIRED) Ray namespace to isolate deployments (set to match client code so handles resolve correctly)
export SERVE_HTTP_HOST="0.0.0.0"            # (REQUIRED) Host interface for Serve HTTP ingress; use 0.0.0.0 in containers, 127.0.0.1 for local-only access
export SERVE_HTTP_PORT="8003"               # (REQUIRED) Port Serve will bind to for incoming HTTP requests; change if port conflict exists
export ONNX_USE_CUDA="false"                # (REQUIRED) If "true", prefer CUDAExecutionProvider in ONNX Runtime (requires onnxruntime-gpu + drivers); "false" uses CPU provider
export ORT_INTRA_THREADS="1"                # (REQUIRED) Intra-op threads for ONNXRuntime session (increase to let each session use more CPU cores for faster ops)
export ORT_INTER_THREADS="1"                # (REQUIRED) Inter-op threads for ONNXRuntime session (tune for concurrency across sessions)
export INDEXING_EMBEDDER_MAX_TOKENS="512"   # (REQUIRED) Max tokens used by embedder for indexing operations (controls truncation for long docs)
export CROSS_ENCODER_MAX_TOKENS="600"       # (REQUIRED) Max tokens the cross-encoder/reranker uses for (query, passage) pairs during init and runtime
export ENABLE_CROSS_ENCODER="true"          # (REQUIRED) Enable deploying the reranker; set false to skip reranker deployment and save resources
export MAX_RERANK="256"                     # (REQUIRED) Upper limit on number of candidate passages accepted by reranker endpoint to avoid OOMs
export EMBED_REPLICAS="1"                   # (REQUIRED) Default fixed embed replicas used if per-model mode = fixed; increase to raise embed QPS
export RERANK_REPLICAS="1"                  # (REQUIRED) Default fixed rerank replicas used if per-model mode = fixed; increase if CPU-bound reranker needs more throughput
export EMBED_GPU_PER_REPLICA="0"            # (REQUIRED) GPUs allocated per embed replica (float allowed); set >0 to schedule on GPU nodes
export RERANK_GPU_PER_REPLICA="0"           # (REQUIRED) GPUs allocated per rerank replica (float allowed); set >0 for GPU-accelerated reranker
export LLM_REPLICAS="1"                     # (REQUIRED) Default fixed LLM replicas used if LLM mode = fixed; LLMs typically require GPUs so scale carefully
export LLM_GPU_PER_REPLICA="1.0"            # (REQUIRED) GPUs allocated per LLM replica (floating allowed); set to 1.0 for one-GPU-per-replica or fractional if supported
export EMBED_REPLICA_MODE="fixed"                               # (REQUIRED) "fixed" to use EMBED_REPLICAS/EMBED_REPLICAS_FIXED, or "auto" to use EMBED_AUTOSCALING_CONFIG JSON
export EMBED_REPLICAS_FIXED="1"                                  # (REQUIRED) Fixed replica count for embed when EMBED_REPLICA_MODE=fixed (use small numbers for CPU-only)
export EMBED_AUTOSCALING_CONFIG='{"min_replicas":1,"max_replicas":4,"target_num_ongoing_requests":8}'  # (REQUIRED) JSON autoscale config when EMBED_REPLICA_MODE=auto: min/max replicas and target concurrent requests
export EMBED_NUM_GPUS_PER_REPLICA="0"                            # (REQUIRED) Per-model override of GPUs-per-replica (float) for embed; takes precedence over EMBED_GPU_PER_REPLICA if set
export EMBED_NUM_CPUS_PER_REPLICA="0.5"                          # (REQUIRED) Per-model CPU allocation for embed replicas (use fractional vcores when appropriate)
export EMBED_MAX_QUEUED_REQUESTS="-1"                            # (REQUIRED) Backpressure cap for embed queued requests (-1 = unlimited; positive integer to limit queue)
export RERANK_REPLICA_MODE="fixed"                               # (REQUIRED) "fixed" or "auto" for reranker; autoscale requires RERANK_AUTOSCALING_CONFIG JSON
export RERANK_REPLICAS_FIXED="1"                                 # (REQUIRED) Fixed replica count used when RERANK_REPLICA_MODE=fixed
export RERANK_AUTOSCALING_CONFIG='{"min_replicas":1,"max_replicas":4,"target_num_ongoing_requests":6}'  # (REQUIRED) JSON autoscale config for reranker (min,max,target concurrent requests)
export RERANK_NUM_GPUS_PER_REPLICA="0"                           # (REQUIRED) GPUs per reranker replica override (float); set >0 to co-locate on GPU nodes
export RERANK_NUM_CPUS_PER_REPLICA="1.0"                         # (REQUIRED) CPUs per reranker replica (float); increase for CPU-bound cross-encoder workloads
export RERANK_MAX_QUEUED_REQUESTS="-1"                           # (REQUIRED) Backpressure cap for reranker (-1 unlimited), useful to avoid memory blowup on large batches
export LLM_REPLICA_MODE="auto"                                   # (REQUIRED) "auto" recommended for LLMs (use "fixed" to pin to LLM_REPLICAS_FIXED)
export LLM_REPLICAS_FIXED="1"                                    # (REQUIRED) Fixed replica count when LLM_REPLICA_MODE=fixed; LLMs often need GPUs so choose appropriately
export LLM_AUTOSCALING_CONFIG='{"min_replicas":1,"max_replicas":8,"target_num_ongoing_requests":10}'  # (REQUIRED) JSON autoscale config for LLM: ensures min warm replicas and max limit
export LLM_NUM_GPUS_PER_REPLICA="${LLM_GPU_PER_REPLICA:-1.0}"    # (REQUIRED) GPUs per LLM replica used by placement / ray_actor_options (defaults to LLM_GPU_PER_REPLICA)
export LLM_NUM_CPUS_PER_REPLICA="4.0"                            # (REQUIRED) CPUs per LLM replica (float) to reserve host CPU for preprocessing and vLLM threads
export LLM_MAX_QUEUED_REQUESTS="200"                             # (REQUIRED) Maximum queued requests for LLM ingress to protect memory under burst

# ------------------------------
# (OPTIONAL to overrirde but export required) 
# ------------------------------
export EMBED_DEPLOYMENT="embed_onxx"        # (OPTIONAL) Logical deployment name for embedder used by client bindings; keep consistent with ingest/query code
export RERANK_HANDLE_NAME="rerank_onxx"     # (OPTIONAL) Logical deployment name for reranker used by client bindings; must match client handle name
export MODEL_DIR_EMBED="/workspace/models/gte-modernbert-base"              # (OPTIONAL) Base directory for embedder model artifacts (tokenizer + onnx)
export MODEL_DIR_RERANK="/workspace/models/ms-marco-TinyBERT-L2-v2"        # (OPTIONAL) Base directory for reranker artifacts (tokenizer + onnx variants)
export ONNX_EMBED_PATH="/workspace/models/gte-modernbert-base/onnx/model_int8.onnx"   # (OPTIONAL) Exact embedder ONNX file path (point to the int8/optimized file you want to use)
export ONNX_EMBED_TOKENIZER_PATH="/workspace/models/gte-modernbert-base/tokenizer.json" # (OPTIONAL) Tokenizer JSON for embedder required to tokenize inputs
export ONNX_RERANK_PATH="/workspace/models/ms-marco-TinyBERT-L2-v2/onnx/model_quint8_avx2.onnx" # (OPTIONAL) Recommended CPU-friendly reranker ONNX; alternative: onnx/model_O4.onnx for different build
export ONNX_RERANK_TOKENIZER_PATH="/workspace/models/ms-marco-TinyBERT-L2-v2/tokenizer.json"     # (OPTIONAL) Reranker tokenizer JSON path required for init
export EMBED_MAX_REPLICAS_PER_NODE="1"      # (OPTIONAL) Safety cap limiting number of embed replicas placed on a single node to improve packing
export RERANKER_MAX_REPLICAS_PER_NODE="1"   # (OPTIONAL) Safety cap for reranker replicas per node to avoid resource overcommit
export LLM_MAX_REPLICAS_PER_NODE="1"        # (OPTIONAL) Safety cap for LLM replicas per node; set >1 only if node has enough GPUs/memory
export LLM_PATH="/workspace/models/Qwen3-4B-AWQ"   # (OPTIONAL) Local filesystem path or remote source for LLM model artifacts (use your AWQ artifact)
export LLM_MODEL_ID="Qwen3-4B-AWQ"                 # (OPTIONAL) Logical model id used by LLMConfig (not the model artifact filename) for tracing/metrics
export LLM_DEPLOYMENT_NAME="llm_server"            # (OPTIONAL) LLM server deployment name used by consumers if they call LLM ingress directly
export LLM_INGRESS_NAME="openai_ingress"           # (OPTIONAL) OpenAI-compatible ingress deployment name provided by Serve LLM helpers
export INFERENCE_EMBEDDER_MAX_TOKENS="64"   # (OPTIONAL) Max tokens used by embedder for interactive inference (smaller than indexing to reduce latency)
export EMBED_BATCH_MAX_SIZE="32"            # (OPTIONAL) Max batch size for embed batching (use with @serve.batch inside deployment)
export EMBED_BATCH_WAIT_S="0.02"            # (OPTIONAL) Max time to wait for a batch (seconds) before running the embed batch
export RERANK_BATCH_MAX_SIZE="8"            # (OPTIONAL) Max batch size for reranker batching if used
export RERANK_BATCH_WAIT_S="0.01"           # (OPTIONAL) Max wait time for rerank batch assembly (seconds)
export LLM_BATCH_MAX_SIZE="16"              # (OPTIONAL) Max batch size for LLM engine batching (engine param; also set via LLM_ENGINE_KWARGS)
export LLM_BATCH_WAIT_S="0.02"              # (OPTIONAL) Max wait time for LLM batching to form a batch before dispatch
export ORT_INTRA_THREADS_HIGH="4"           # (OPTIONAL if ONNX_USE_CUDA=false) Alternate higher intra-op thread setting you can swap in for throughput testing on CPU nodes
export ORT_INTER_THREADS_HIGH="2"           # (OPTIONAL if ONNX_USE_CUDA=false) Alternate inter-op thread count for higher-throughput scenarios on CPU nodes
export EMBED_AUTOSCALING_CONFIG_OVERRIDE=''    # (OPTIONAL) If non-empty, this JSON string will override EMBED_AUTOSCALING_CONFIG at startup
export RERANK_AUTOSCALING_CONFIG_OVERRIDE=''   # (OPTIONAL) If non-empty, this JSON string will override RERANK_AUTOSCALING_CONFIG at startup
export LLM_AUTOSCALING_CONFIG_OVERRIDE=''      # (OPTIONAL) If non-empty, this JSON string will override LLM_AUTOSCALING_CONFIG at startup


```

### indexing pipeline 
```sh
export DATA_IN_LOCAL="false"       # whether data/chunks live locally (true) or in S3 (false); flip to true for dev local runs
export LOCAL_DIR_PATH="./data"     # local data root when DATA_IN_LOCAL=true; point to your disk path containing raw/ chunked dirs
export S3_RAW_PREFIX=data/raw/                        # raw ingest prefix (change to isolate datasets)
export S3_CHUNKED_PREFIX=data/chunked/                # chunked output prefix (change to separate processed data)
export OVERWRITE_DOC_DOCX_TO_PDF=true                 # true to delete and replace docx with PDF, false to keep the originals
export OVERWRITE_ALL_AUDIO_FILES=true     # true to delete and replace .mp3, .m4a, .aac, etc as .mav 16khz, false to keep the originals
export OVERWRITE_SPREADSHEETS_WITH_CSV=true  # true to delete and replace .xls, .xlsx, .ods, etc as .csv files, false to keep the originals
export OVERWRITE_PPT_WITH_PPTS=true                   # true to delete and replace .ppt files as .pptx, false to keep the originals
export CHUNK_FORMAT=json                              # 'json' (readable) or 'jsonl' (stream/space efficient)
export MAX_TOKENS_PER_CHUNK=512        # Cummulatively append text sentences of .pdf, .html, .mp3, .png ,etc as a chunk till max token limit  
export MIN_TOKENS_PER_CHUNK=100        # If a chunk less than min token limit, it will be appended to previous chunk even if max tokens slightly exceeds
export NUMBER_OF_OVERLAPPING_SENTENCES=2 # Overlap text btw chunks for better embedding similarity, increase for retrival,decrease for cost
export PDF_DISABLE_OCR=false                          # true to skip OCR (very fast) or false to extract text from images(but not embedded due to noise)
export PDF_OCR_ENGINE=rapidocr                        # 'tesseract' (faster/multilingual) or 'rapidocr' (high accuracy, slightly slower)
export PDF_TESSERACT_LANG=eng                         # only considered if PDF_OCR_ENGINE=tesseract
export PDF_FORCE_OCR=false                            # true to always OCR(use if only scanned pdfs but not recommended for scaling)
export PDF_OCR_RENDER_DPI=400                         # increase for detecting tiny/complex text; lower for speed/cost
export PDF_MIN_IMG_SIZE_BYTES=3072                    # ignore images smaller than 3KB (often unneccessary black images)
export IMAGE_OCR_ENGINE=rapidocr                  # OR 'tesseract' (faster/multilingual), 'rapidocr' (high english accuracy, slightly slower)
export IMAGE_TESSERACT_LANG="eng"                # if PDF_OCR_ENGINE=tesseract. Only 1 language to avoid noise
export TESSERACT_CONFIG="--oem 1 --psm 6"        # OR '--oem 1 --psm 3' if full image ocr instead of cropped boxes ocr
export IMAGE_MIN_IMG_SIZE_BYTES=3072             # ignore images smaller than 3KB (often unneccessary black images)
export IMAGE_RENDER_DPI=600                      # increase for detecting tiny/complex text with rapidocr; lower for speed/cost
export IMAGE_UPSCALE_FACTOR=2.0         # controls how much the image is enlarged for small/complex text detection , lower for speed/cost
export CSV_TARGET_TOKENS_PER_CHUNK=600      # (Including header)Increase if very large .csv or Decrease if higher precision required
export JSONL_TARGET_TOKENS_PER_CHUNK=600    # (Including header)Increase if very large .jsonl or Decrease if higher precision required
export PPTX_SLIDES_PER_CHUNK=4                        # Number of slides per chunk. Increase or decrease based on text 
export PPTX_OCR_ENGINE=rapidocr                       # 'tesseract' (faster), 'rapidocr' (high accuracy , slightly slower)
export PYTHONUNBUFFERED=1                             # To force Python to display logs/output immediately instead of buffering
export S3_BUCKET="e2e-rag-system-42"


export INDEXING_EMBEDDER_MAX_TOKENS="612" # max tokens passed to embedder during indexing; reduce for memory/latency constraints
export COLLECTION="my_collection"  # Qdrant collection name; change per dataset / tenant to isolate vectors
export QDRANT_ON_DISK_PAYLOAD="true" # store payload on disk (reduce RAM); true to minimize RAM usage for large payloads, false for fastest payload reads
export QDRANT_HNSW_M="16"          # HNSW graph M parameter (index complexity); increase for higher recall at cost of memory/index build time
export QDRANT_HNSW_EF_CONSTRUCTION="200" # ef_construction (build-time index quality); raise for better index quality (slower build)
export QDRANT_HNSW_EF_SEARCH="50"  # ef_search (query-time window); increase to improve recall at runtime (higher latency/CPU)
export QDRANT_HNSW_FULL_SCAN_THRESHOLD="10000" # threshold to force scan instead of HNSW; tune if you have many small vectors or lots of updates
export BATCH_SIZE="64"             # upsert / processing batch size; increase to improve throughput but watch memory usage
export EMBED_BATCH="32"            # number of texts per embed call; tune based on embedder memory and latency tradeoffs
export EMBED_TIMEOUT="60"          # seconds to wait for embed call; increase for slower embed backends
export FORCE_REHASH="0"            # set to "1"/"true" to force recompute of file hash and reprocess everything; use for full re-ingest
export VECTOR_DIM="768"            # expected vector dimension (sanity check); set to your embedder's output size
export LEASE_TTL_SECONDS="300"     # lease TTL for single-writer locking per file_hash; increase if embedding is slow to finish
export BATCH_INPUTS="8"            # number of files processed in parallel groups; increase to parallelize across workers but watch CPU
export NEO4J_WRITE_MAX_ATTEMPTS="3" # retry attempts for Neo4j writes on transient errors; raise for flaky networks
export NEO4J_WRITE_BASE_BACKOFF="0.8" # base backoff (s) for Neo4j retries; exponential backoff multiplies this

```

### retreival required 


export MODE="hybrid"                      # "hybrid" (qdrant+neo4j flow) or "vector_only" (Qdrant vectors only); pick for latency vs richness
export PREFER_GRPC="true"                 # if true prefer gRPC connection to Qdrant (faster), set false for HTTP-only endpoints
export COLLECTION="my_collection"         # Qdrant collection name to query; must match ingest collection
export VECTOR_DIM="768"                   # expected vector dim; used to sanity-check embed length


export TOP_K="5"                          # final number of answers returned to caller; typically small (1-10)
export TOP_VECTOR_CHUNKS="200"            # ANN search window (first-stage); larger for higher recall at expense of latency
export TOP_BM25_CHUNKS="100"              # BM25 window returned from Neo4j fulltext; tune for textual recall
export INFERENCE_EMBEDDER_MAX_TOKENS="64" # embedder max tokens for query embedding; small to reduce cost/latency
export CROSS_ENCODER_MAX_TOKENS="600"     # max tokens for cross-encoder input; increase for long contexts but watch memory
export MAX_PROMPT_TOKENS="3000"           # token budget for prompt assembly; ensures LLM context limit not exceeded
export HTTP_TIMEOUT="30"                  # generic HTTP/IO timeout (s) used by some clients; raise for slow networks
export EMBED_TIMEOUT="10"                 # timeout (s) for embed handle calls; increase if embed service is slow
export CALL_TIMEOUT_SECONDS="10"          # generic call timeout for internal helper wrappers; tune to your cluster
export RETRY_ATTEMPTS="3"                 # number of retries for network/handle calls; increase for unreliable infra
export RETRY_BASE_SECONDS="0.5"           # base jitter/backoff seconds used when retrying; small default
export RETRY_JITTER="0.3"                 # jitter added to retry backoff to avoid thundering herd
export ENABLE_CROSS_ENCODER="true"        # whether to run cross-encoder re-ranker; enable for high-precision ranking (costly)
export MAX_CHUNKS_TO_LLM="8"              # max chunks to include in final LLM prompt; lower for strict token budgets
export ENABLE_METADATA_CHUNKS="false"     # when true run Qdrant payload MatchText on metadata and include as ranked list; use when payload contains useful keywords
export MAX_METADATA_CHUNKS="50"           # cap for metadata/payload keyword hits; limit to bound RRF input size
export FIRST_STAGE_RRF_K="60"             # RRF k parameter for stability of first-stage fusion; tweak small/large to change weighting of top ranks
export MAX_CHUNKS_FOR_GRAPH_EXPANSION="20" # number of seeds to expand via graph; lower to limit expansion cost
export GRAPH_EXPANSION_HOPS="1"           # number of hops for graph expansion in Neo4j; increase to broaden candidate set (costly)
export SECOND_STAGE_RRF_K="60"            # RRF k parameter for second-stage fusion; same semantics as FIRST_STAGE_RRF_K
export MAX_CHUNKS_TO_CROSSENCODER="64"    # max candidates sent to cross-encoder; limit based on reranker capacity and latency
export LLM_SYSTEM_PROMPT="You are a helpful knowledge assistant who answers user queries with provenance using only the provided context chunks below.\nAnnotate the essential fact or statement inline in brackets with all available non-null fields from the chunk: (source_url file_name, page_number, row_range, token_range, audio_range, headings/headings_path).\nMerge information from the appropriate chunk(s) naturally, placing inline annotations after each relevant sentence or fact.\nDo not hallucinate any information or sources, if unsure say so.\nKeep the answer concise, readable, and factual.\nAlways end your response with a confidence percentage."
export MAX_CHUNKS_TO_LLM="8"              # final cap sent to LLM; duplicate kept for clarity — ensure it matches runtime limits




```






## 🔗 **References & specialties of the default models**
---
### 🔹 **\[1] Alibaba-NLP/gte-modernbert-base**

* Embedding-only onnx model for dense retrieval in RAG pipelines
* Long-context support: up to **8192 tokens**
* Based on **ModernBERT** (FlashAttention 2, RoPE, no position embeddings)
* Embedding dimension: **768**
* Parameter size: **149M**

* Fast GPU inference with ONNX (FlashAttention 2)
  🔗 [https://huggingface.co/Alibaba-NLP/gte-modernbert-base](https://huggingface.co/Alibaba-NLP/gte-modernbert-base)

---
### 🔹 **\[2] cross-encoder/ms-marco-TinyBERT-L2-v2(Optional)**

* **Cross-encoder reranker** trained on the **MS MARCO Passage Ranking** task  
* Extremely lightweight (**4.39M parameters**) and optimized for **high‑throughput reranking**  
* Benchmark scores: **nDCG@10 = 69.84 (TREC DL 2019)**, **MRR@10 = 32.56 (MS MARCO Dev)**  
* Very fast inference — up to **~9000 docs/sec on V100 GPU**  
* Available in PyTorch, ONNX, and SentenceTransformers for easy integration  
  🔗 [https://huggingface.co/cross-encoder/ms-marco-TinyBERT-L2-v2](https://huggingface.co/cross-encoder/ms-marco-TinyBERT-L2-v2)

> **Use case**: Best suited for **low‑latency reranking of top‑k candidates** from BM25, vector, or graph retrieval. Ideal when **speed and scale** are more important than peak precision in RAG pipelines.

---

### 🔹 **[3] Qwen/Qwen3-4B-AWQ**

A compact, high-throughput **instruction-tuned LLM** quantized using **AWQ**. Built on **Qwen3-4B**, this variant supports **32,768-token context** natively and achieves performance comparable to models 10× its size (e.g., Qwen2.5-72B). Optimized for [TGI v3 inference](https://huggingface.co/docs/text-generation-inference/conceptual/chunking), it benefits from enhanced memory management techniques such as [PagedAttention](https://arxiv.org/abs/2309.06180), [prefix caching, and dynamic batching](https://deepwiki.com/huggingface/text-generation-inference/2.3-memory-management-and-optimization). These improvements deliver higher throughput and lower latency for long‑context workloads. The result is a balance of speed, memory efficiency, and accuracy, running seamlessly on GPUs like A10G, L4, and L40S.

* Architecture: **Transformer** (Qwen3 series, multilingual)  
* Context Length: **32k tokens**  
* Quantization: **AWQ**  
* VRAM Usage: **~9.5–10.5 GiB for 10K tokens on g5.xlarge (A10G, 24 GiB)**, leaving ample headroom for batching or longer contexts  

🔗 [Qwen/Qwen3-4B-AWQ](https://huggingface.co/Qwen/Qwen3-4B-AWQ)

> “Even a tiny model like Qwen3-4B can rival the performance of Qwen2.5-72B-Instruct.” — [Qwen3 Blog](https://qwenlm.github.io/blog/qwen3/)  

---

> "Performance leap: TGI processes 3x more tokens, 13x faster than vLLM on long prompts. Zero config !"
> — [TGI v3](https://huggingface.co/docs/text-generation-inference/conceptual/chunking) 
---
> **Use case**: Smaller models (e.g., Qwen3-4B-AWQ) **fit on a single VM**, making them well-suited for **TGI v3’s optimized batching and long-context inference**, while avoiding the complexity of tensor-parallel engines like vLLM.

> Qwen3-0.6B model answers accurately even from 20 noisy chunks: [Colab Demo](https://colab.research.google.com/drive/1aefiADR4pqXLkOL8WQXJMmiNJlJhzOnK?usp=sharing)

---


