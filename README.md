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

# infra

```sh

export AWS_ACCESS_KEY_ID="AKIA..."                        
export AWS_SECRET_ACCESS_KEY="..."    


export AWS_REGION="ap-south-1"                        # AWS region to deploy infrastructure (e.g., ap-south-1 for Mumbai)
export S3_BUCKET=e2e-rag-system-42                    # Set any globally unique complex name, Pulumi S3 backend -> s3://$S3_BUCKET/pulumi/
export PULUMI_CONFIG_PASSPHRASE="mypassword"          # Passphrase to encrypt Pulumi secrets (required for headless automation)


export VPC_CIDR="10.0.0.0/16"                         # VPC network range (all subnets must fall inside)
export PULUMI_PUBLIC_SUBNET_COUNT="2"                 # 2 or more if multi AZ required. Must match number of CIDRs
export PUBLIC_SUBNET_CIDRS="10.0.1.0/24,10.0.2.0/24"  # CIDR blocks for public subnets in the VPC (must match VPC_CIDR and AZ count)
export MY_SSH_CIDR="203.0.113.42/32"                  # Your IP allowed for SSH access (must be a /32 single IP)

export MULTI_AZ_DEPLOYMENT="false"          # true = deploy across multiple AZs (HA, more cost) / false = single AZ (cheaper, simpler)
export WEAVIATE_PRIVATE_IP="10.0.1.10"               # Deterministic private IP for Weaviate if multi-az=false




```

# indexing pipeline configs
```sh
export AWS_REGION="ap-south-1"                        # AWS region to deploy infrastructure (e.g., ap-south-1 for Mumbai)
export S3_BUCKET=e2e-rag-system-42                    # Set any globally unique complex name, Pulumi S3 backend -> s3://$S3_BUCKET/pulumi/
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




export LOG_LEVEL="INFO"            # logging verbosity (DEBUG/INFO/WARN/ERROR); set DEBUG while debugging ingest flow, keep INFO in prod
export RAY_ADDRESS="auto"          # Ray cluster address ("auto" to connect locally); set to cluster head URL to run against remote Ray
export RAY_NAMESPACE="ragops"      # Ray namespace for isolation; change per-environment to avoid actor/namespace collisions
export SERVE_APP_NAME="default"    # Ray Serve app name used when resolving handles; change if your Serve app is named differently
export DATA_IN_LOCAL="false"       # whether data/chunks live locally (true) or in S3 (false); flip to true for dev local runs
export LOCAL_DIR_PATH="./data"     # local data root when DATA_IN_LOCAL=true; point to your disk path containing raw/ chunked dirs
export S3_BUCKET=""                # S3 bucket for raw/chunked inputs; set when using S3 ingestion
export S3_RAW_PREFIX="data/raw/"   # S3 prefix that contains raw files; adjust to your bucket layout
export S3_CHUNKED_PREFIX="data/chunked/" # S3 prefix for pre-chunked JSONL files; adjust to ingestion pipeline output
export EMBED_DEPLOYMENT="embed_onxx" # Serve/Ray deployment name for embedder; change to match your deployment name
export INDEXING_EMBEDDER_MAX_TOKENS="512" # max tokens passed to embedder during indexing; reduce for memory/latency constraints
export QDRANT_URL="http://127.0.0.1:6333" # Qdrant endpoint; production cloud URL or local container address goes here
export QDRANT_API_KEY=""           # Qdrant API key if using cloud; leave empty for unauthenticated local instances
export COLLECTION="my_collection"  # Qdrant collection name; change per dataset / tenant to isolate vectors
export QDRANT_ON_DISK_PAYLOAD="true" # store payload on disk (reduce RAM); true to minimize RAM usage for large payloads, false for fastest payload reads
export QDRANT_HNSW_M="16"          # HNSW graph M parameter (index complexity); increase for higher recall at cost of memory/index build time
export QDRANT_HNSW_EF_CONSTRUCTION="200" # ef_construction (build-time index quality); raise for better index quality (slower build)
export QDRANT_HNSW_EF_SEARCH="50"  # ef_search (query-time window); increase to improve recall at runtime (higher latency/CPU)
export QDRANT_HNSW_FULL_SCAN_THRESHOLD="10000" # threshold to force scan instead of HNSW; tune if you have many small vectors or lots of updates
export NEO4J_URI="bolt://localhost:7687" # Neo4j bolt URI; set to your Neo4j host (use bolt+s for secure connections)
export NEO4J_USER="neo4j"          # Neo4j username; change for production creds
export NEO4J_PASSWORD=""           # Neo4j password; must be provided in secure env (avoid committing)
export BATCH_SIZE="64"             # upsert / processing batch size; increase to improve throughput but watch memory usage
export EMBED_BATCH="32"            # number of texts per embed call; tune based on embedder memory and latency tradeoffs
export EMBED_TIMEOUT="60"          # seconds to wait for embed call; increase for slower embed backends
export FORCE_REHASH="0"            # set to "1"/"true" to force recompute of file hash and reprocess everything; use for full re-ingest
export VECTOR_DIM="768"            # expected vector dimension (sanity check); set to your embedder's output size
export LEASE_TTL_SECONDS="300"     # lease TTL for single-writer locking per file_hash; increase if embedding is slow to finish
export AWS_REGION=""               # optional AWS region override used by boto; set for cross-region buckets to avoid region lookup
export AWS_DEFAULT_REGION=""       # fallback region env var for boto3; same as above
export BATCH_INPUTS="8"            # number of files processed in parallel groups; increase to parallelize across workers but watch CPU
export NEO4J_WRITE_MAX_ATTEMPTS="3" # retry attempts for Neo4j writes on transient errors; raise for flaky networks
export NEO4J_WRITE_BASE_BACKOFF="0.8" # base backoff (s) for Neo4j retries; exponential backoff multiplies this

export LLM_SYSTEM_PROMPT="You are a helpful knowledge assistant who answers user queries with provenance using only the provided context chunks below.\nAnnotate the essential fact or statement inline in brackets with all available non-null fields from the chunk: (source_url file_name, page_number, row_range, token_range, audio_range, headings/headings_path).\nMerge information from the appropriate chunk(s) naturally, placing inline annotations after each relevant sentence or fact.\nDo not hallucinate any information or sources, if unsure say so.\nKeep the answer concise, readable, and factual.\nAlways end your response with a confidence percentage."



export LOG_LEVEL="INFO"                   # logging verbosity; set DEBUG to trace retrieval decisions
export RAY_ADDRESS="auto"                 # Ray cluster address used to resolve Serve handles; set to remote head if not local
export RAY_NAMESPACE=""                   # Ray namespace for handles resolution; set to same namespace as deployments
export SERVE_APP_NAME="default"           # Serve app name for handle resolution; match your Serve configuration
export EMBED_DEPLOYMENT="embed_onxx"      # name of embedder Serve deployment; must match deployed name
export RERANK_HANDLE_NAME="rerank_onxx"   # name of reranker Serve deployment (cross-encoder); used if ENABLE_CROSS_ENCODER=true
export RERANK_DEPLOYMENT="rerank_onxx"    # legacy alias; keep in sync with RERANK_HANDLE_NAME
export MODE="hybrid"                      # "hybrid" (qdrant+neo4j flow) or "vector_only" (Qdrant vectors only); pick for latency vs richness
export QDRANT_URL="http://127.0.0.1:6333" # Qdrant endpoint for vector search; set cloud URL when using managed Qdrant
export QDRANT_API_KEY=""                  # Qdrant API key if required by cloud; leave empty for local
export PREFER_GRPC="true"                 # if true prefer gRPC connection to Qdrant (faster), set false for HTTP-only endpoints
export COLLECTION="my_collection"         # Qdrant collection name to query; must match ingest collection
export NEO4J_URI="bolt://localhost:7687"  # Neo4j URI used for BM25 and graph expansion; required in hybrid mode
export NEO4J_USER="neo4j"                 # Neo4j user; change for production
export NEO4J_PASSWORD=""                  # Neo4j password; always provide securely
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
export MAX_CHUNKS_TO_LLM="8"              # final cap sent to LLM; duplicate kept for clarity — ensure it matches runtime limits



export LOG_LEVEL="INFO"                     # logging level for model/serve bootstrap; DEBUG when loading models troubleshoots init
export RAY_ADDRESS=""                       # Ray address (set to head node if not local); leave empty for local single-node tests
export RAY_NAMESPACE=""                     # Ray namespace for deployments; set to match consumer scripts to resolve handles
export SERVE_HTTP_HOST="0.0.0.0"            # HTTP host for Serve ingress; bind to 0.0.0.0 in containers, 127.0.0.1 for local dev
export SERVE_HTTP_PORT="8003"               # HTTP port Serve will listen on; change if port conflict occurs
export EMBED_DEPLOYMENT="embed_onxx"        # embed deployment name used by bindings; keep consistent with ingest/query
export RERANK_HANDLE_NAME="rerank_onxx"     # reranker deployment name; must match query.py RERANK handle
export MODEL_DIR_EMBED="/models/gte-modernbert-base" # base path for embed model assets (tokenizer + onnx); change to point at your model bundle
export MODEL_DIR_RERANK="/models/gte-reranker-modernbert-base" # rerank model base path; change to your reranker bundle
export ONNX_EMBED_PATH="/models/gte-modernbert-base/onnx/model_int8.onnx" # exact ONNX path for embedder; point to actual onnx file
export ONNX_EMBED_TOKENIZER_PATH="/models/gte-modernbert-base/tokenizer.json" # tokenizer JSON for embedder; required to tokenise inputs
export ONNX_RERANK_PATH="/models/gte-reranker-modernbert-base/onnx/model_int8.onnx" # reranker ONNX path; required if ENABLE_CROSS_ENCODER=true
export ONNX_RERANK_TOKENIZER_PATH="/models/gte-reranker-modernbert-base/tokenizer.json" # reranker tokenizer path; must exist for reranker init
export EMBED_REPLICAS="1"                   # number of embed replicas; increase for higher QPS, ensure node resources available
export RERANK_REPLICAS="1"                  # number of rerank replicas; increase if cross-encoder QPS is high
export EMBED_GPU_PER_REPLICA="0"            # fractional GPUs per embed replica (Ray accepts floats); >0 to schedule GPU-backed replicas
export RERANK_GPU_PER_REPLICA="0"           # fractional GPUs per rerank replica; set >0 for GPU acceleration
export MAX_RERANK="256"                     # cap on rerank candidates accepted by reranker endpoint; prevents OOMs
export ONNX_USE_CUDA="false"                # whether to enable CUDAExecutionProvider for ONNXRuntime; true if GPUs available
export ORT_INTRA_THREADS="1"                # onnxruntime intra-op threads; increase to use more CPU cores per session for throughput
export ORT_INTER_THREADS="1"                # onnxruntime inter-op threads; tune depending on concurrency and CPU topology
export INDEXING_EMBEDDER_MAX_TOKENS="512"   # default max tokens used by embedder during indexing; same meaning as in ingest/query
export CROSS_ENCODER_MAX_TOKENS="600"       # cross-encoder max tokens used when initializing reranker; align with query.py
export ENABLE_CROSS_ENCODER="true"          # whether to deploy reranker; must match query.py to avoid handle resolution mismatch
export EMBED_MAX_REPLICAS_PER_NODE="1"      # safety cap for per-node embed replicas; limit to avoid overplacement
export RERANKER_MAX_REPLICAS_PER_NODE="1"   # safety cap per-node for reranker; tune for cluster packing
export LLM_MAX_REPLICAS_PER_NODE="1"        # safety cap for LLM serve instances per node; adjust for GPU memory constraints
export LLM_ENABLE="true"                    # whether to deploy LLM server (vLLM/Serve LLM APIs); set false when only embed/rerank are needed
export LLM_PATH="/workspace/models/qwen/Qwen3-0.6B" # local path or model source for LLM server; change to where model artifacts are stored
export LLM_MODEL_ID="qwen-3-0.6b"           # logical model id used by Serve LLMConfig; useful for tracing/deploy configs
export LLM_REPLICAS="1"                     # number of LLM replicas; increase for availability but needs GPUs
export LLM_GPU_PER_REPLICA="1.0"            # GPUs per LLM replica; set 1.0 for one-GPU-per-replica or fractional if supported
export LLM_DEPLOYMENT_NAME="llm_server"     # LLM deployment name used by consumer clients; keep consistent with query code if calling ingress
export LLM_INGRESS_NAME="openai_ingress"    # OpenAI-compatible ingress deployment name created for the LLM; used by external callers
export LLM_ENGINE_KWARGS="{}"               # JSON object of engine kwargs passed to LLM server (e.g. {"max_batch_size":16}); change to tune engine
export LLM_USE_RUNTIME_ENV="false"          # if true, install runtime deps when creating LLM (slower startup); use for isolated runtime needs

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


