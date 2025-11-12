source .venv/bin/activate
ray stop --force || true
sudo rm -rf /tmp/ray/ || true
ray start --head --disable-usage-stats
python3 -m infra.rayserve_deployments
curl -sS http://127.0.0.1:8003/healthz | jq .
curl -sS -X POST http://127.0.0.1:8003/retrieve -H "Content-Type: application/json" -d '{"query":"What is MLOps?","stream":false}' | jq .




nohup .venv/bin/uvicorn infra.fastapi_gateway:app --host 0.0.0.0 --port $GATEWAY_PORT --log-config - > gateway.stdout.log 2>&1 &
export ONNX_EMBED_PATH=/opt/models/embed.onnx
export ONNX_EMBED_TOKENIZER_PATH=/opt/models/embed_tokenizer.json
export ONNX_RERANK_PATH=/opt/models/rerank.onnx
export ONNX_RERANK_TOKENIZER_PATH=/opt/models/rerank_tokenizer.json
export LLM_PATH=/opt/models/llm.gguf
export GATEWAY_BIND_AUTO=1
export LOG_LEVEL=INFO



#!/usr/bin/env bash
# export.sh — production and staging environment variables (edit before sourcing)

# Region / Stack
export AWS_REGION="ap-south-1"
export SOURCE_STACK_NAME="${SOURCE_STACK_NAME:-dev}"   # set to networking stack if separate
export STACK_NAME="${STACK_NAME:-dev}"

# Enable modules
export ENABLE_FILE_A=true
export ENABLE_FILE_B=true
export ENABLE_FILE_C=true
export ENABLE_FILE_D=true
export ENABLE_FILE_E=true

# Networking / VPC
export MULTI_AZ_DEPLOYMENT=true
export CREATE_VPC_ENDPOINTS=true
export NO_NAT=false
export VPC_CIDR="10.0.0.0/16"
export PUBLIC_SUBNET_CIDRS="10.0.1.0/24,10.0.2.0/24"
export PRIVATE_SUBNET_CIDRS="10.0.11.0/24,10.0.12.0/24"

# Pulumi backend (if not handled by pulumi_setup.sh)
# export PULUMI_STATE_BUCKET="pulumi-state-bucket"
# export PULUMI_DDB_LOCK_TABLE="pulumi-locks"

# Security / KMS
export KMS_ALIAS="alias/ray-ssm-key-${STACK_NAME}"

# ALB / Domain / ACM
export DOMAIN="app.example.com"
export PUBLIC_HOSTED_ZONE_ID="ZXXXXXXXXXXXX"     # set for prod
export ACM_VALIDATION_RECORD_TTL=300
export ALB_IDLE_TIMEOUT=300
export APP_PORT=8003
export ENABLE_COGNITO=true
export ENABLE_WAF=true
export WAF_RATE_LIMIT_THRESHOLD=2000

# Secrets (set with pulumi config --secret)
# pulumi config set --secret redisPassword "<redis>"
# pulumi config set --secret valkeyAuthToken "<valkey>"
# pulumi config set --secret qdrantApiKey "<qdrant>"
# pulumi config set --secret neo4jPassword "<neo4j>"
export REDIS_SSM_PARAM="/ray/prod/redis_password"

# ElastiCache (Valkey)
export ENABLE_ELASTICACHE=true
export ELASTICACHE_NODE_TYPE="cache.t3.micro"
export ELASTICACHE_NUM_NODES=1
# Optional vault auth token. Better use pulumi config secret 'valkeyAuthToken'

# Head / Worker AMIs and profiles (fill these)
export HEAD_AMI="ami-0abcdef1234567890"
export HEAD_INSTANCE_TYPE="m5.large"
export RAY_HEAD_INSTANCE_PROFILE="ray-head-instance-profile-prod"
export RAY_CPU_AMI="ami-0abcdef1234567890"
export RAY_CPU_INSTANCE="m5.xlarge"
export RAY_CPU_INSTANCE_PROFILE="ray-worker-instance-profile-prod"
export KEY_NAME="my-prod-keypair"

# Observability
export ENABLE_PROMETHEUS=false
export CW_LOG_RETENTION_DAYS=30
export ALARM_SNS_TOPIC_NAME="ray-ops-alerts"
export GATEWAY_5XX_ALARM_THRESHOLD=10
export VALKEY_P99_LATENCY_MS_THRESHOLD=50


# Ray / redis
export RAY_REDIS_PORT=6379
export REDIS_SSM_PARAM="/ray/prod/redis_password"

# Misc toggles
export ENABLE_RATE_LIMITER=false
export MODE="hybrid"

echo "export.sh loaded. Edit values before sourcing for prod use."




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
