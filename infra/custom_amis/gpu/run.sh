#!/usr/bin/env bash
set -euo pipefail

# Minimal, hardcoded packer launcher for Ubuntu 22.04 GPU AMI builds.
# Usage: edit constants below if you must; by default this will:
#  - pick the latest Canonical Ubuntu 22.04 AMI (x86_64 or arm64)
#  - run packer build with GPU-capable default instance_type g5.xlarge
#  - fail fast with clear errors

AWS_REGION="${AWS_REGION:-ap-south-1}"
AWS_PROFILE="${AWS_PROFILE:-default}"
PACKER_FILE="${PACKER_FILE:-packer.pkr.hcl}"
# Default GPU-capable instance type (change only if you know what you're doing)
INSTANCE_TYPE="${INSTANCE_TYPE:-g4dn.xlarge}"
VOLUME_SIZE_GB="${VOLUME_SIZE_GB:-50}"
AMI_NAME="${AMI_NAME:-minimal-vllm-py311-gpu-ami-$(date +%Y%m%d-%H%M%S)}"
AMI_DESC="${AMI_DESC:-Ubuntu22.04 GPU AMI (CUDA, Python3.11, pip venv, vLLM)}"
CPU_AMI_ARCH="${CPU_AMI_ARCH:-amd}"   # 'amd' -> amd64, 'arm' -> arm64

# map arch token used in AMI name patterns
case "$CPU_AMI_ARCH" in
  amd) ARCH="amd64";;
  arm) ARCH="arm64";;
  *) echo "ERROR: CPU_AMI_ARCH must be 'amd' or 'arm'"; exit 2;;
esac

# Logging / debug
export PACKER_LOG=1
export PACKER_LOG_PATH="${PWD}/packer_build.log"
mkdir -p "$(dirname "${PACKER_LOG_PATH}")"

echo "run.sh: region=${AWS_REGION} profile=${AWS_PROFILE} instance_type=${INSTANCE_TYPE} volume_gb=${VOLUME_SIZE_GB}"
echo "PACKER_FILE=${PACKER_FILE}"

# ---------- sanity checks ----------
command -v aws >/dev/null 2>&1 || { echo "ERROR: 'aws' CLI not found in PATH"; exit 1; }
command -v packer >/dev/null 2>&1 || { echo "ERROR: 'packer' not found in PATH"; exit 1; }

# ---------- credential selection ----------
AWS_CLI=()
# prefer explicit env creds if they authenticate
if [ -n "${AWS_ACCESS_KEY_ID:-}" ] && [ -n "${AWS_SECRET_ACCESS_KEY:-}" ]; then
  if aws sts get-caller-identity --region "${AWS_REGION}" >/dev/null 2>&1; then
    AWS_CLI=(aws --region "${AWS_REGION}")
  fi
fi

# try profile if env creds not usable
if [ ${#AWS_CLI[@]} -eq 0 ] && [ -n "${AWS_PROFILE:-}" ]; then
  if aws sts get-caller-identity --profile "${AWS_PROFILE}" --region "${AWS_REGION}" >/dev/null 2>&1; then
    AWS_CLI=(aws --profile "${AWS_PROFILE}" --region "${AWS_REGION}")
  fi
fi

# discover a working profile from ~/.aws if still none
if [ ${#AWS_CLI[@]} -eq 0 ]; then
  PROFILES=()
  if [ -f "${HOME}/.aws/credentials" ]; then
    while IFS= read -r line; do
      if [[ "$line" =~ ^\[(.+)\]$ ]]; then PROFILES+=("${BASH_REMATCH[1]}"); fi
    done < "${HOME}/.aws/credentials"
  fi
  if [ -f "${HOME}/.aws/config" ]; then
    while IFS= read -r line; do
      if [[ "$line" =~ ^\[profile[[:space:]]+(.+)\]$ ]]; then PROFILES+=("${BASH_REMATCH[1]}"); fi
    done < "${HOME}/.aws/config"
  fi
  for p in "${PROFILES[@]}"; do
    if aws sts get-caller-identity --profile "$p" --region "${AWS_REGION}" >/dev/null 2>&1; then
      AWS_CLI=(aws --profile "$p" --region "${AWS_REGION}")
      break
    fi
  done
fi

if [ ${#AWS_CLI[@]} -eq 0 ]; then
  cat <<'ERR'
ERROR: No valid AWS credentials found for region. Fix options:
  - export AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY (and AWS_SESSION_TOKEN if temp)
  - or run: aws sso login --profile <profile>
  - or run: aws configure --profile <profile>
  - or run on EC2 with instance role granting EC2 actions
ERR
  exit 10
fi

# ---------- find latest Canonical Ubuntu 22.04 AMI ----------
OWNER="099720109477"   # Canonical
PATTERNS=(
  "ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-${ARCH}-server-*"
  "ubuntu/images/hvm-ssd-gp3/ubuntu-jammy-22.04-${ARCH}-server-*"
  "ubuntu/images/hvm-ssd/ubuntu-jammy*${ARCH}*server*"
)
AMI=""
for p in "${PATTERNS[@]}"; do
  COUNT="$("${AWS_CLI[@]}" ec2 describe-images --owners "${OWNER}" --filters "Name=name,Values=${p}" --query 'length(Images)' --output text 2>/dev/null || true)"
  if [ "${COUNT:-0}" -gt 0 ]; then
    CAND="$("${AWS_CLI[@]}" ec2 describe-images --owners "${OWNER}" --filters "Name=name,Values=${p}" --query 'sort_by(Images,&CreationDate)[-1].ImageId' --output text 2>/dev/null || true)"
    if [ -n "$CAND" ] && [ "$CAND" != "None" ]; then AMI="$CAND"; break; fi
  fi
done

# fallback generic search if patterns failed
if [ -z "$AMI" ]; then
  COUNT="$("${AWS_CLI[@]}" ec2 describe-images --owners "${OWNER}" --filters "Name=name,Values=ubuntu-jammy*" "Name=architecture,Values=${ARCH}" --query 'length(Images)' --output text 2>/dev/null || true)"
  if [ "${COUNT:-0}" -gt 0 ]; then
    AMI="$("${AWS_CLI[@]}" ec2 describe-images --owners "${OWNER}" --filters "Name=name,Values=ubuntu-jammy*" "Name=architecture,Values=${ARCH}" --query 'sort_by(Images,&CreationDate)[-1].ImageId' --output text 2>/dev/null || true)"
  fi
fi

if [ -z "$AMI" ]; then
  echo "ERROR: Could not find Canonical Ubuntu 22.04 AMI."
  exit 11
fi
export BASE_UBUNTU22_04_AMI_ID="$AMI"
echo "Selected base AMI: ${BASE_UBUNTU22_04_AMI_ID}"

# ---------- verify packer file exists ----------
if [ ! -f "$PACKER_FILE" ]; then echo "ERROR: $PACKER_FILE not found"; exit 12; fi

# ---------- init/validate/build packer ----------
packer init "$PACKER_FILE"
packer validate "$PACKER_FILE"

# on failure tail the packer log to help debugging
trap 'echo "Packer build failed; tailing ${PACKER_LOG_PATH}"; tail -n 200 "${PACKER_LOG_PATH}" || true; exit 13' ERR

packer build \
  -var "region=${AWS_REGION}" \
  -var "instance_type=${INSTANCE_TYPE}" \
  -var "volume_size_gb=${VOLUME_SIZE_GB}" \
  -var "ami_name=${AMI_NAME}" \
  -var "ami_description=${AMI_DESC}" \
  -var "source_ami=${AMI}" \
  "$PACKER_FILE"

# remove trap before success
trap - ERR

CUSTOM_AMI="$("${AWS_CLI[@]}" ec2 describe-images --owners self --filters "Name=name,Values=${AMI_NAME}" --query 'sort_by(Images,&CreationDate)[-1].ImageId' --output text || true)"
if [ -z "$CUSTOM_AMI" ] || [ "$CUSTOM_AMI" = "None" ]; then echo "ERROR: could not find custom AMI"; exit 14; fi

echo "Created AMI: $CUSTOM_AMI"
LINE1="export BASE_UBUNTU22_04_AMI_ID=${AMI}"
LINE2="export CUSTOM_GPU_AMI_ID=${CUSTOM_AMI}"
for line in "$LINE1" "$LINE2"; do
  grep -Fxq "$line" "${HOME}/.bashrc" 2>/dev/null || printf '%s\n' "$line" >> "${HOME}/.bashrc"
done
echo "Exports appended to ~/.bashrc"
exit 0
