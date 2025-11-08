#!/usr/bin/env bash
set -euo pipefail
ACTION="${1:-}"
AWS_REGION="${AWS_REGION:-ap-south-1}"
PACKER_FILE="${PACKER_FILE:-packer.pkr.hcl}"
VOLUME_SIZE_GB="${VOLUME_SIZE_GB:-50}"
BUILD_ARCH="${BUILD_ARCH:-arm64}"
AMI_NAME_PREFIX="${AMI_NAME_PREFIX:-cpu-ml-ami}"
TAG_KEY_CREATED_BY="${TAG_KEY_CREATED_BY:-CreatedBy}"
TAG_VALUE_CREATED_BY="${TAG_VALUE_CREATED_BY:-cpu-ami-builder}"
if [[ ! "${BUILD_ARCH}" =~ ^(x86_64|arm64)$ ]]; then echo "ERROR: BUILD_ARCH must be 'x86_64' or 'arm64'"; exit 2; fi
if ! command -v aws >/dev/null 2>&1; then echo "ERROR: aws CLI not found"; exit 3; fi
if ! command -v packer >/dev/null 2>&1; then echo "ERROR: packer not found"; exit 4; fi
timestamp() { date -u +"%Y%m%d-%H%M%SZ"; }
fail() { echo "ERROR: $*" >&2; exit 10; }
get_latest_base_ami() {
  local arch_filter="$1"
  local filter_name
  if [ "$arch_filter" = "arm64" ]; then filter_name="ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-arm64-server-*"; else filter_name="ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"; fi
  aws ec2 describe-images --owners 099720109477 --filters "Name=name,Values=${filter_name}" "Name=architecture,Values=${arch_filter}" --query 'sort_by(Images,&CreationDate)[-1].ImageId' --output text --region "$AWS_REGION"
}
run_create() {
  local base_ami
  base_ami="$(get_latest_base_ami "$BUILD_ARCH")"
  if [ -z "$base_ami" ] || [ "$base_ami" = "None" ]; then fail "No base AMI found for ${BUILD_ARCH}"; fi
  local ami_name="${AMI_NAME_PREFIX}-${BUILD_ARCH}-$(timestamp)"
  echo "Creating AMI: name=${ami_name} region=${AWS_REGION} base_ami=${base_ami}"
  packer init "$PACKER_FILE"
  packer validate "$PACKER_FILE"
  packer build -var "region=${AWS_REGION}" -var "architecture=${BUILD_ARCH}" -var "volume_size_gb=${VOLUME_SIZE_GB}" -var "ami_name=${ami_name}" -var "ami_description=Custom CPU AMI ${BUILD_ARCH}" -var "source_ami=${base_ami}" "$PACKER_FILE"
  local created_ami
  created_ami="$(aws ec2 describe-images --owners self --filters "Name=name,Values=${ami_name}" --query 'sort_by(Images,&CreationDate)[-1].ImageId' --output text --region "$AWS_REGION")"
  if [ -z "$created_ami" ] || [ "$created_ami" = "None" ]; then fail "Custom AMI not found after build"; fi
  aws ec2 create-tags --resources "$created_ami" --tags Key="${TAG_KEY_CREATED_BY}",Value="${TAG_VALUE_CREATED_BY}" Key="Architecture",Value="${BUILD_ARCH}" Key="ManagedBy",Value="script" --region "$AWS_REGION"
  local export_var="CUSTOM_CPU_AMI_ID_${BUILD_ARCH^^}"
  echo "export ${export_var}=${created_ami}" >> ~/.bashrc
  echo "${export_var}=${created_ami}"
  echo "Created AMI: ${created_ami}"
}
run_delete() {
  local arch_filter="${1:-}"
  local filters=( "Name=tag:${TAG_KEY_CREATED_BY},Values=${TAG_VALUE_CREATED_BY}" )
  if [ -n "$arch_filter" ]; then filters+=( "Name=tag:Architecture,Values=${arch_filter}" ); fi
  mapfile -t amis < <(aws ec2 describe-images --owners self --filters "${filters[@]}" --query 'Images[*].[ImageId,Name,CreationDate]' --output text --region "$AWS_REGION" || true)
  if [ "${#amis[@]}" -eq 0 ]; then echo "No AMIs found matching tag ${TAG_KEY_CREATED_BY}=${TAG_VALUE_CREATED_BY} ${arch_filter:+and Architecture=${arch_filter}}"; return 0; fi
  local i=0
  while [ $i -lt "${#amis[@]}" ]; do
    local ami_id="${amis[$i]}"
    local ami_name="${amis[$((i+1))]}"
    i=$((i+2))
    echo "Processing AMI ${ami_id} (${ami_name:-unknown})"
    mapfile -t snaps < <(aws ec2 describe-images --image-ids "$ami_id" --query 'Images[0].BlockDeviceMappings[].Ebs.SnapshotId' --output text --region "$AWS_REGION" || true)
    if [ -z "${snaps[*]:-}" ]; then echo "  No snapshots found for AMI ${ami_id}"; else
      echo "  Found snapshots: ${snaps[*]}"
    fi
    echo "  Deregistering AMI ${ami_id}"
    aws ec2 deregister-image --image-id "$ami_id" --region "$AWS_REGION"
    for s in "${snaps[@]:-}"; do
      if [ -n "$s" ] && [ "$s" != "None" ]; then
        echo "  Deleting snapshot $s"
        aws ec2 delete-snapshot --snapshot-id "$s" --region "$AWS_REGION" || echo "  Warning: failed to delete snapshot $s"
      fi
    done
  done
}
usage() { echo "Usage: $0 --create|--delete [--arch arm64|x86_64]"; exit 1; }
if [ -z "$ACTION" ]; then usage; fi
case "$ACTION" in
  --create) run_create ;;
  --delete) shift || true; ARG_ARCH=""; while [ $# -gt 0 ]; do case "$1" in --arch) ARG_ARCH="${2:-}"; shift 2 ;; *) shift ;; esac; done; run_delete "$ARG_ARCH" ;;
  -h|--help) usage ;;
  *) usage ;;
esac
