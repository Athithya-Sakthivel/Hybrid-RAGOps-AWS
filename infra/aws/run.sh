#!/usr/bin/env bash
set -euo pipefail

export AWS_REGION="${AWS_REGION:-us-east-1}"                                    # AWS region to create resources in; change if you deploy to another region (affects AMI ids, endpoints, Route53 zones)
export PULUMI_CONFIG_PASSPHRASE="${PULUMI_CONFIG_PASSPHRASE:-}"                  # Pulumi stack encryption passphrase (optional); set when using encrypted config or local backends
export PULUMI_STACK="${PULUMI_STACK:-dev}"                                      # Pulumi stack name (logical environment); change per environment (dev/stage/prod) to isolate state
export KEY_NAME="${KEY_NAME:-my-ec2-keypair}"                                   # EC2 KeyPair name for SSH access (optional if using SSM); change to your account/keypair for debugging only
export HEAD_AMI="${HEAD_AMI:-ami-HEAD-AMI-ID}"                                  # AMI id used for the Ray head instances; MUST be set to a region-correct, Ray-ready AMI in production
export HEAD_INSTANCE_TYPE="${HEAD_INSTANCE_TYPE:-m5.large}"                     # EC2 instance type for head; choose based on control-plane load/CPU/memory needs
export VPC_ID="${VPC_ID:-}"                                                      # Existing VPC id to use; set to a pre-existing VPC for non-destructive deploys
export SUBNET_IDS="${SUBNET_IDS:-[\"subnet-priv-1\",\"subnet-priv-2\"]}"         # JSON array of private subnet ids (multi-AZ recommended); change to the private subnets where instances will run
export VPC_CIDR="${VPC_CIDR:-10.0.0.0/16}"                                       # VPC CIDR used when creating a new VPC; change only if you need a different IP range
export PUBLIC_SUBNET_CIDRS="${PUBLIC_SUBNET_CIDRS:-10.0.1.0/24,10.0.2.0/24}"     # Comma list of public subnet CIDRs used when creating VPC; adjust when you want custom subnet layout
export PRIVATE_SUBNET_CIDRS="${PRIVATE_SUBNET_CIDRS:-10.0.11.0/24,10.0.12.0/24}" # Comma list of private subnet CIDRs used when creating VPC; change for different AZ/subnet design
export MULTI_AZ_DEPLOYMENT="${MULTI_AZ_DEPLOYMENT:-false}"                      # true to distribute subnets across multiple AZs for resilience; set true for production multi-AZ
export NO_NAT="${NO_NAT:-false}"                                                # true to avoid creating NAT gateways (saves cost) but requires VPC endpoints for SSM/S3 if true
export CREATE_VPC_ENDPOINTS="${CREATE_VPC_ENDPOINTS:-false}"                     # true to create S3/SSM VPC endpoints automatically (recommended if NO_NAT=true)
export PRIVATE_HOSTED_ZONE_ID="${PRIVATE_HOSTED_ZONE_ID:-}"                      # Route53 private hosted zone id; required for internal DNS (ray-head.*); set to your VPC-associated private zone
export HOSTED_ZONE_NAME="${HOSTED_ZONE_NAME:-prod.internal.example.com}"        # The private Route53 zone name (used to build head DNS); change to your internal domain
export DOMAIN="${DOMAIN:-example.com}"                                           # Public domain used for ACM/certificates (only if you create public Route53 zone); change to your domain
export HOSTED_ZONE_ID="${HOSTED_ZONE_ID:-}"                                     # Public hosted zone id (optional) used for ACM DNS validation; set when you manage public DNS here
export AUTOSCALER_S3="${AUTOSCALER_S3:-s3://ray-configs/prod/autoscaler.yaml}"  # S3 path to the Ray autoscaler YAML that the head will fetch and run; upload your YAML to this path
export REDIS_SSM_PARAM="${REDIS_SSM_PARAM:-/ray/prod/redis_password}"            # SSM SecureString parameter name for Ray cluster secret/redis password; create and rotate securely
export RAY_CLUSTER_NAME="${RAY_CLUSTER_NAME:-ray-prod}"                         # Logical Ray cluster name used in generated YAML and logs; change per environment for clarity
export RSV_AWS_REGION="${RSV_AWS_REGION:-$AWS_REGION}"                          # Backward-compatible env var used by ray generator; keep equal to AWS_REGION unless required otherwise
export RSV_SSH_USER="${RSV_SSH_USER:-ubuntu}"                                   # Default SSH user to access AMIs; change per AMI distro (ubuntu/ec2-user/centos)
export RSV_SSH_PRIVATE_KEY="${RSV_SSH_PRIVATE_KEY:-~/.ssh/id_rsa}"              # Path to SSH private key used by Ray autoscaler for SSH; change to the key that matches EC2 KeyPair if using SSH
export RAY_SSH_PRIVATE_KEY="${RAY_SSH_PRIVATE_KEY:-$RSV_SSH_PRIVATE_KEY}"       # Alias used in generator; keep in sync with RSV_SSH_PRIVATE_KEY
export RAY_SSH_USER="${RAY_SSH_USER:-$RSV_SSH_USER}"                            # Alias for ssh user (keeps naming consistent); change if your AMI uses a different default user
export RAY_YAML_PATH="${RAY_YAML_PATH:-ray_ec2_autoscaler.yaml}"                # Local path to write the generated Ray autoscaler YAML; change to desired output location
export RAY_DEFAULT_PIP_PACKAGES="${RAY_DEFAULT_PIP_PACKAGES:-ray[default]==2.5.0 httpx transformers}" # Pip packages to install on CPU nodes; pin versions to ensure reproducible installs
export RAY_GPU_PIP_PACKAGES="${RAY_GPU_PIP_PACKAGES:-ray[default]==2.5.0 httpx transformers onnxruntime-gpu}" # Pip packages for GPU nodes; ensure GPU-compatible libs are used
export RAY_HEAD_INSTANCE="${RAY_HEAD_INSTANCE:-m5.large}"                       # Instance type used in generated Ray YAML for head node (informational only when head infra-managed)
export RAY_HEAD_AMI="${RAY_HEAD_AMI:-ami-HEAD-AMI-ID}"                          # Placeholder AMI id used in generated Ray YAML; replace with a production-grade AMI before ray up
export RAY_HEAD_KEYNAME="${RAY_HEAD_KEYNAME:-$KEY_NAME}"                        # KeyName referenced in Ray YAML for head (keeps worker configuration consistent); change to the EC2 keypair you want workers to use
export RAY_CPU_INSTANCE="${RAY_CPU_INSTANCE:-m5.xlarge}"                       # Default CPU worker instance type in generated YAML; tune by workload/price
export RAY_CPU_AMI="${RAY_CPU_AMI:-ami-CPU-AMI-ID}"                             # Placeholder CPU worker AMI id in generated YAML; replace with region-correct AMI with Python/Deps
export RAY_CPU_MIN_WORKERS="${RAY_CPU_MIN_WORKERS:-1}"                          # Minimum autoscaler CPU workers; set to desired baseline for availability
export RAY_CPU_MAX_WORKERS="${RAY_CPU_MAX_WORKERS:-6}"                          # Maximum autoscaler CPU workers; set cost/scale ceiling for the cluster
export RAY_GPU_INSTANCE="${RAY_GPU_INSTANCE:-p3.2xlarge}"                       # Default GPU worker instance type in generated YAML; choose GPU family needed for models
export RAY_GPU_AMI="${RAY_GPU_AMI:-ami-GPU-AMI-ID}"                             # Placeholder GPU AMI id; MUST include CUDA drivers and nvidia-smi for GPU nodes
export RAY_GPU_MIN_WORKERS="${RAY_GPU_MIN_WORKERS:-0}"                          # Minimum number of GPU workers (0 means none by default); set >0 if GPUs required at baseline
export RAY_GPU_MAX_WORKERS="${RAY_GPU_MAX_WORKERS:-2}"                          # Maximum number of GPU workers; cap to control spend
export RAY_IDLE_TIMEOUT_MINUTES="${RAY_IDLE_TIMEOUT_MINUTES:-10}"               # Ray autoscaler idle timeout to scale down workers; increase to keep workers warm longer
export RAY_DEFAULT_PIP_PACKAGES="${RAY_DEFAULT_PIP_PACKAGES:-ray[default]==2.5.0 httpx transformers}" # duplicate kept for clarity in subsystems; keep consistent with above
export RAY_GPU_PIP_PACKAGES="${RAY_GPU_PIP_PACKAGES:-ray[default]==2.5.0 httpx transformers onnxruntime-gpu}" # duplicate kept for clarity
export APP_PORT="${APP_PORT:-8080}"                                             # Application port that backend serves on (used by ALB target group & SGs); change if your app uses different port
export NO_NAT="${NO_NAT:-false}"                                                # duplicate to ensure shell presence; if true, ensure VPC endpoints exist for SSM/S3
export CREATE_VPC_ENDPOINTS="${CREATE_VPC_ENDPOINTS:-false}"                     # duplicate for subsystems; set true when NO_NAT=true to allow SSM/S3 access without NAT
export ALB_DOMAIN="${ALB_DOMAIN:-api.example.com}"                              # Public ALB domain (if you create a public ALB); change to your external DNS when exposing publicly
export CERT_DOMAIN="${CERT_DOMAIN:-$DOMAIN}"                                    # Domain for ACM certificate; must match HOSTED_ZONE_ID ownership for DNS validation
export SNS_ONCALL_EMAIL="${SNS_ONCALL_EMAIL:-oncall@example.com}"               # Email or endpoint to subscribe to SNS alerts for head/infra alarms; change to your on-call address
export CLOUDWATCH_ALARM_EVAL_PERIODS="${CLOUDWATCH_ALARM_EVAL_PERIODS:-3}"       # Number of evaluation periods for HeadReady alarm (3 * period = alarm firing window); adjust per RTO needs
export CLOUDWATCH_ALARM_PERIOD="${CLOUDWATCH_ALARM_PERIOD:-60}"                 # CloudWatch metric period in seconds for heartbeat/alarm evaluation (60s recommended)
export ASG_LIFECYCLE_HOOK_NAME="${ASG_LIFECYCLE_HOOK_NAME:-wait-for-ray-ready-prod}" # Lifecycle hook name used by ASG to wait for head readiness; change if you deploy multiple clusters
export ASG_HEARTBEAT_TIMEOUT="${ASG_HEARTBEAT_TIMEOUT:-1800}"                   # ASG lifecycle hook heartbeat timeout in seconds; must be long enough for boot and install (e.g., 1800s)
export RAY_HEAD_DNS="${RAY_HEAD_DNS:-ray-head.prod.internal.example.com}"        # Stable internal DNS name that workers and clients use to reach the head; must match Route53 record created by infra
export RAY_NLB_PORT="${RAY_NLB_PORT:-6379}"                                     # Port on the NLB used to reach Ray control plane (TCP); keep as Ray's control port unless customized
export RAY_SERVE_PORT="${RAY_SERVE_PORT:-8000}"                                 # Port for Ray Serve HTTP endpoint on head/worker (if Serve is used); change if your Serve binds elsewhere
export LOG_SHIP_BUCKET="${LOG_SHIP_BUCKET:-}"                                   # Optional S3 bucket to ship bootstrap logs for debugging; set to a bucket ARN if using log collection via user-data
export ADDITIONAL_TAGS="${ADDITIONAL_TAGS:-{\"team\":\"ml\",\"env\":\"prod\"}}"  # JSON map of tags applied to infra; adjust team/env for cost allocation and governance

# Pulumi config keys (note: these are normally set by `pulumi config set <key> <value>` rather than via env, but exporting makes scripts easier)
export PULUMI_CONFIG_vpcId="${PULUMI_CONFIG_vpcId:-$VPC_ID}"                     # Pulumi config key vpcId (for head_resilience.py); set with `pulumi config set vpcId <vpc-id>`
export PULUMI_CONFIG_subnetIds="${PULUMI_CONFIG_subnetIds:-$SUBNET_IDS}"         # Pulumi config key subnetIds (JSON array); set with `pulumi config set --path subnetIds '["subnet-1","subnet-2"]'`
export PULUMI_CONFIG_headAmi="${PULUMI_CONFIG_headAmi:-$HEAD_AMI}"               # Pulumi config key headAmi; set with `pulumi config set headAmi <ami-id>`
export PULUMI_CONFIG_privateHostedZoneId="${PULUMI_CONFIG_privateHostedZoneId:-$PRIVATE_HOSTED_ZONE_ID}" # Pulumi config key privateHostedZoneId; set to Route53 private zone id

# export complete
export_all_done="true"                                                           # marker variable to indicate this export block was evaluated; not used by infra but handy in scripts


pulumi config set aws:region "$AWS_REGION" || true
pulumi up --yes
