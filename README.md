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
export WEAVIATE_INSTANCE_TYPE="c8gd.medium"           # c8gd is the most appropriate local NVMe based ec2 type. Increase size if large data
export WEAVIATE_EBS_TYPE="gp3"                        # gp3 baseline 3000 iops is sufficient since storage is local NVMe based
export WEAVIATE_EBS_SIZE="8"                          # Root EBS volume size in GiB (set larger if storing any data outside NVMe)

export MULTI_AZ_WEAVIATE_DEPLOYMENT="false"          # true = deploy across multiple AZs (HA, more cost) / false = single AZ (cheaper, simpler)
export WEAVIATE_PRIVATE_IP="10.0.1.10"               # Deterministic private IP for Weaviate if multi-az=false
export WEAVIATE_PRIVATE_IPS="10.0.1.10,10.0.2.10"    # Private IP(s) for Weaviate (comma-separated, one per AZ if multi-AZ=true)

export BACKUP_PREFIX="weaviate/backups/"              # S3 folder prefix where Weaviate backups will be stored
export BACKUP_MANIFEST_KEY="latest_weaviate_backup.manifest.json" # S3 key name for the latest backup manifest file
export DELETE_OR_ARCHIVE_OLD_BACKUPS="archive"        # archive=move to cold storage, delete=remove after retention, none=disable lifecycle
export ARCHIVE_AFTER_DAYS="30"                        # Days after which backups transition to archive (only used if archive mode)
export ARCHIVE_STORAGE_CLASS="GLACIER"                # S3 storage class for archived backups (GLACIER/DEEP_ARCHIVE/GLACIER_IR/STANDARD_IA)
export RETENTION_DAYS="365"                           # Total days to keep backups before deletion (used in archive/delete modes)


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


export LOG_LEVEL="INFO"                              # python logging level, use DEBUG for troubleshooting, INFO for normal runs, WARNING/ERROR to reduce log volume in production
export RAY_ADDRESS="auto"                             # Ray address or 'auto', set to a specific redis address when connecting to a remote Ray cluster
export RAY_NAMESPACE="ragops"                         # Ray namespace used for actors, change to isolate multiple environments or teams on the same Ray cluster
export SERVE_APP_NAME="default"                       # Ray Serve application name, change if your Serve deployments run under a different app
export DATA_IN_LOCAL="false"                          # true to read raw inputs from LOCAL_DIR_PATH instead of S3, set true for local dev or CI
export LOCAL_DIR_PATH="./data"                        # local data base path, point to your repo/local mount when DATA_IN_LOCAL=true
export EMBED_DEPLOYMENT="embed_onxx"                  # name of the Ray Serve embed deployment, update if your embedder deployment uses another name
export INDEXING_EMBEDDER_MAX_TOKENS=512               # max tokens sent to embedder per chunk, lower to reduce embed cost or raise for longer chunks/semantic fidelity
export QDRANT_URL="$QDRANT_URL"            # Qdrant endpoint, use grpc://host:port or http(s) URL depending on your Qdrant setup
export QDRANT_API_KEY="$QDRANT_API_KEY"                              # Qdrant API key if your server requires authentication, leave empty for local unsecured Qdrant
export COLLECTION="my_collection"                     # Qdrant collection name, change per dataset to isolate vectors
export QDRANT_ON_DISK_PAYLOAD="true"                  # store payload on disk in Qdrant, set false to keep payload in-memory if you need faster writes and have RAM
export NEO4J_URI="$NEO4J_URI"              # Neo4j connection URI, change to bolt://host:port or neo4j+s://host for cloud instances
export NEO4J_USER="neo4j"                             # Neo4j username, update for different DB users or service accounts
export NEO4J_PASSWORD="$NEO4J_PASSWORD"                              # Neo4j password, populate in CI/production (use secrets manager instead of plain env in production)
export BATCH_SIZE=64                                  # qdrant upsert batch size, lower if Qdrant rejects large batches or raise for throughput if resources allow
export EMBED_BATCH=32                                 # embedder batch size, tune to fit embedder memory/latency constraints
export EMBED_TIMEOUT=60                               # embedder call timeout in seconds, increase for slower models or reduce to fail fast on issues
export FORCE_REHASH="0"                               # set to "1"/"true" to force recompute file hash and re-evaluate chunk files, use for debugging or repopulating manifests
export VECTOR_DIM=768                                 # vector dimension expected by Qdrant, must match your embedder output dimension
export AWS_DEFAULT_REGION="ap-south-1"                          # fallback AWS region if AWS_REGION unset, set to your default AWS region for boto3 clients
export BATCH_INPUTS=8                                 # parallel raw inputs per batch, increase for higher concurrency but watch service load
export NEO4J_WRITE_MAX_ATTEMPTS=3                     # retry attempts for Neo4j writes, raise for transient networks or lower to fail faster
export NEO4J_WRITE_BASE_BACKOFF=0.8                   # base backoff seconds for Neo4j retries, increase to reduce retry pressure during outages


export AWS_REGION=ap-south-1
export AWS_PROFILE=default

```

docker run --rm -it -p 8000:8000 \
  -e AWS_ACCESS_KEY_ID=$AWS_ACCESS_KEY_ID \
  -e AWS_SECRET_ACCESS_KEY=$AWS_SECRET_ACCESS_KEY \
    -e NEO4J_URI=$NEO4J_URI \
  -e NEO4J_USER=neo4j \
  -e NEO4J_PASSWORD=$NEO4J_PASSWORD \
    -e QDRANT_URL=$QDRANT_URL \
  -e QDRANT_API_KEY=$QDRANT_API_KEY \
    -e QDRANT_URL=$QDRANT_URL \
  -e DEBIAN_FRONTEND=noninteractive \
  indexing_pipeline:latest




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


