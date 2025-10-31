#!/usr/bin/env bash
set -euo pipefail

# bash infra/custom_amis/gpu/find_base_ami.sh --region "ap-south-1" --arch amd

REGION=""
CPU_ARCH=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    --region) REGION="$2"; shift 2 ;;
    --arch) CPU_ARCH="$2"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "Unknown arg: $1" >&2; usage ;;
  esac
done

REGION="${REGION:-${AWS_REGION:-}}"
if [ -z "$REGION" ]; then
  REGION="$(aws configure get region 2>/dev/null || true)"
fi
if [ -z "$REGION" ]; then
  echo "ERROR: region not set. Use --region or set AWS_REGION or configure aws CLI." >&2
  exit 1
fi

case "${CPU_ARCH:-}" in
  "") ARCHS=("amd64" "arm64") ;;
  amd|x86|x86_64) ARCHS=("amd64") ;;
  arm|arm64|aarch64) ARCHS=("arm64") ;;
  *) echo "ERROR: --cpu-arch must be 'amd' or 'arm'." >&2; exit 1 ;;
esac

OWNER="099720109477" # Canonical
echo "[INFO] region=$REGION"

get_latest_ami() {
  local region="$1"
  local arch="$2"
  local pattern="ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-${arch}-server-*"
  aws ec2 describe-images \
    --region "$region" \
    --owners "$OWNER" \
    --filters "Name=name,Values=${pattern}" "Name=state,Values=available" \
    --query 'sort_by(Images,&CreationDate)[-1].ImageId' \
    --output text 2>/dev/null || true
}

update_bashrc() {
  local ami="$1"
  local rc="${HOME}/.bashrc"
  local line="export BASE_GPU_AMI=\"$ami\""
  # ensure file exists
  touch "$rc"
  if grep -q '^export BASE_GPU_AMI=' "$rc"; then
    # replace in-place, keep portable backup then remove
    sed -i.bak -E "s@^export BASE_GPU_AMI=.*@${line}@" "$rc"
    rm -f "${rc}.bak"
  else
    printf '\n# set by find_base_ami.sh\n%s\n' "$line" >> "$rc"
  fi
  echo "[INFO] set BASE_GPU_AMI in $rc"
}

FOUND_AMI=""
for a in "${ARCHS[@]}"; do
  ami="$(get_latest_ami "$REGION" "$a")"
  if [ -z "$ami" ] || [ "$ami" = "None" ] || [ "$ami" = "null" ]; then
    printf 'ARCH=%s: NOT FOUND\n' "$a"
  else
    printf 'ARCH=%s: %s\n' "$a" "$ami"
    # if only one arch requested then set env
    if [ "${#ARCHS[@]}" -eq 1 ]; then
      FOUND_AMI="$ami"
      update_bashrc "$ami"
    fi
  fi
done

# exit non-zero if single-arch requested but not found
if [ "${#ARCHS[@]}" -eq 1 ] && [ -z "$FOUND_AMI" ]; then
  echo "ERROR: AMI not found for requested arch." >&2
  exit 2
fi
