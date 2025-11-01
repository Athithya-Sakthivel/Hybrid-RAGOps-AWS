#!/usr/bin/env bash
set -euo pipefail
AWS_REGION="${AWS_REGION:-ap-south-1}"
AWS_PROFILE="${AWS_PROFILE:-default}"
PACKER_FILE="${PACKER_FILE:-packer.pkr.hcl}"
INSTANCE_TYPE="${INSTANCE_TYPE:-g4dn.xlarge}"
VOLUME_SIZE_GB="${VOLUME_SIZE_GB:-50}"
AMI_NAME="${AMI_NAME:-vllm-py311-offline-ami-$(date +%Y%m%d-%H%M%S)}"
AMI_DESC="${AMI_DESC:-Ubuntu22.04 GPU AMI with CUDA12.8, Python3.11, vLLM and baked HF models}"
CPU_AMI_ARCH="${CPU_AMI_ARCH:-amd}"
case "$CPU_AMI_ARCH" in amd) ARCH="amd64";; arm) ARCH="arm64";; *) echo "ERROR: CPU_AMI_ARCH must be 'amd' or 'arm'"; exit 2;; esac
export PACKER_LOG=1
export PACKER_LOG_PATH="${PWD}/packer_build.log"
echo "run.sh: region=${AWS_REGION} profile=${AWS_PROFILE} instance_type=${INSTANCE_TYPE} volume_gb=${VOLUME_SIZE_GB}"

# helper to run aws with chosen options
AWS_CLI=()

# 0) quick env debug
echo "---- Credential quick debug (env) ----"
echo "AWS_ACCESS_KEY_ID=${AWS_ACCESS_KEY_ID:-<unset>}"
echo "AWS_SECRET_ACCESS_KEY=${AWS_SECRET_ACCESS_KEY:+<set>}"
echo "AWS_SESSION_TOKEN=${AWS_SESSION_TOKEN:+<set>}"
echo "AWS_PROFILE env: ${AWS_PROFILE:-<unset>}"
echo "--------------------------------------"

# 1) Try env creds first
if [ -n "${AWS_ACCESS_KEY_ID:-}" ] && [ -n "${AWS_SECRET_ACCESS_KEY:-}" ]; then
  echo "Trying environment credentials..."
  if aws sts get-caller-identity --region "${AWS_REGION}" >/dev/null 2>&1; then
    echo "Using environment credentials."
    AWS_CLI=(aws --region "${AWS_REGION}")
  else
    echo "Environment credentials present but aws sts failed. Please verify keys are valid/active."
  fi
fi

# 2) Try configured profile (if not using env creds)
if [ ${#AWS_CLI[@]} -eq 0 ] && [ -n "${AWS_PROFILE:-}" ]; then
  echo "Trying profile ${AWS_PROFILE}..."
  if aws sts get-caller-identity --profile "${AWS_PROFILE}" --region "${AWS_REGION}" >/dev/null 2>&1; then
    echo "Using profile ${AWS_PROFILE}"
    AWS_CLI=(aws --profile "${AWS_PROFILE}" --region "${AWS_REGION}")
  else
    echo "Profile ${AWS_PROFILE} failed 'sts get-caller-identity'. It may require 'aws sso login --profile ${AWS_PROFILE}' if it is an SSO profile, or the profile credentials may be expired/missing."
  fi
fi

# 3) Try any profile in ~/.aws/credentials or ~/.aws/config
if [ ${#AWS_CLI[@]} -eq 0 ]; then
  echo "Searching for any usable profile in ~/.aws..."
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
  FOUND=""
  for p in "${PROFILES[@]}"; do
    echo "Trying profile '$p'..."
    if aws sts get-caller-identity --profile "$p" --region "${AWS_REGION}" >/dev/null 2>&1; then
      FOUND="$p"
      AWS_CLI=(aws --profile "$p" --region "${AWS_REGION}")
      echo "Selected profile $p"
      break
    fi
  done
  if [ -z "$FOUND" ]; then
    echo "No usable profile found in ~/.aws (or they require SSO login)."
  fi
fi

# 4) Final check
if [ ${#AWS_CLI[@]} -eq 0 ]; then
  cat <<'ERR'
ERROR: No valid AWS credentials found for region. Fix options:
  - export AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY (and AWS_SESSION_TOKEN if temporary)
  - or run: aws sso login --profile <profile>  (if your profile uses SSO)
  - or run: aws configure --profile <profile> and re-run
  - or run on an EC2 with an instance role granting EC2 actions
Then re-run this script.
ERR
  exit 10
fi

# 5) From here reuse the AMI-finder + packer flow (same logic as previous robust script)
OWNER="099720109477"
PATTERNS=(
  "ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-${ARCH}-server-*"
  "ubuntu/images/hvm-ssd-gp3/ubuntu-jammy-22.04-${ARCH}-server-*"
  "ubuntu/images/hvm-ssd/ubuntu-jammy*${ARCH}*server*"
)
AMI=""
for p in "${PATTERNS[@]}"; do
  echo "Searching pattern: $p"
  COUNT="$("${AWS_CLI[@]}" ec2 describe-images --owners "${OWNER}" --filters "Name=name,Values=${p}" --query 'length(Images)' --output text 2>/dev/null || true)"
  echo "  matches: ${COUNT:-0}"
  if [ "${COUNT:-0}" -gt 0 ]; then
    CAND="$("${AWS_CLI[@]}" ec2 describe-images --owners "${OWNER}" --filters "Name=name,Values=${p}" --query 'sort_by(Images,&CreationDate)[-1].ImageId' --output text 2>/dev/null || true)"
    if [ -n "$CAND" ] && [ "$CAND" != "None" ]; then AMI="$CAND"; echo "  picked: $AMI"; break; fi
  fi
done
if [ -z "$AMI" ]; then
  echo "Falling back to broad jammy search..."
  COUNT="$("${AWS_CLI[@]}" ec2 describe-images --owners "${OWNER}" --filters "Name=name,Values=ubuntu-jammy*" "Name=architecture,Values=${ARCH}" --query 'length(Images)' --output text 2>/dev/null || true)"
  echo "  broad matches: ${COUNT:-0}"
  if [ "${COUNT:-0}" -gt 0 ]; then
    AMI="$("${AWS_CLI[@]}" ec2 describe-images --owners "${OWNER}" --filters "Name=name,Values=ubuntu-jammy*" "Name=architecture,Values=${ARCH}" --query 'sort_by(Images,&CreationDate)[-1].ImageId' --output text 2>/dev/null || true)"
    echo "  picked broad: ${AMI}"
  fi
fi
if [ -z "$AMI" ]; then
  echo "ERROR: Could not find Canonical Ubuntu 22.04 AMI. Run the suggested debug aws CLI command and paste output to me."
  echo "Suggested debug:"
  echo "${AWS_CLI[*]} ec2 describe-images --owners ${OWNER} --filters Name=architecture,Values=${ARCH} Name=name,Values='ubuntu-jammy*' --query 'Images[?contains(Name, `jammy`)] | sort_by(@,&CreationDate)[-10:].{Name:Name,Id:ImageId,Date:CreationDate}' --output table"
  exit 11
fi
echo "Found base AMI: $AMI"
export BASE_UBUNTU22_04_AMI_ID="$AMI"

# validate packer file and build
if [ ! -f "$PACKER_FILE" ]; then echo "ERROR: $PACKER_FILE not found"; exit 12; fi
packer init "$PACKER_FILE"
packer validate "$PACKER_FILE"
if ! packer build -var "region=${AWS_REGION}" -var "instance_type=${INSTANCE_TYPE}" -var "volume_size_gb=${VOLUME_SIZE_GB}" -var "ami_name=${AMI_NAME}" -var "ami_description=${AMI_DESC}" -var "source_ami=${AMI}" "$PACKER_FILE"; then
  echo "ERROR: packer build failed; see ${PACKER_LOG_PATH}"; tail -n 200 "${PACKER_LOG_PATH}" || true; exit 13
fi
CUSTOM_AMI="$("${AWS_CLI[@]}" ec2 describe-images --owners self --filters "Name=name,Values=${AMI_NAME}" --query 'sort_by(Images,&CreationDate)[-1].ImageId' --output text || true)"
if [ -z "$CUSTOM_AMI" ] || [ "$CUSTOM_AMI" = "None" ]; then echo "ERROR: could not find custom AMI"; exit 14; fi
echo "Created AMI: $CUSTOM_AMI"
export CUSTOM_GPU_AMI_ID="$CUSTOM_AMI"
LINE1="export BASE_UBUNTU22_04_AMI_ID=${AMI}"
LINE2="export CUSTOM_GPU_AMI_ID=${CUSTOM_AMI}"
for line in \
"$LINE1" \
"$LINE2" \
; do
  grep -Fxq "$line" "${HOME}/.bashrc" 2>/dev/null || printf '%s\n' "$line" >> "${HOME}/.bashrc"
done
echo "Exports appended to ~/.bashrc"
exit 0
