#!/usr/bin/env bash
set -euo pipefail

AWS_REGION="${AWS_REGION:-ap-south-1}"
PACKER_FILE="${PACKER_FILE:-packer.pkr.hcl}"
VOLUME_SIZE_GB="${VOLUME_SIZE_GB:-60}"
BUILD_ARCH="${BUILD_ARCH:-arm64}"  # "arm64" or "x86_64"

# Validate architecture
if [[ ! "$BUILD_ARCH" =~ ^(x86_64|arm64)$ ]]; then
  echo "ERROR: BUILD_ARCH must be 'x86_64' or 'arm64'"
  exit 1
fi

if [ "$BUILD_ARCH" = "arm64" ]; then
  INSTANCE_TYPE="${INSTANCE_TYPE:-c7g.xlarge}"
  AMI_NAME_PREFIX="cpu-ml-ami-arm64"
else
  INSTANCE_TYPE="${INSTANCE_TYPE:-c6i.xlarge}"
  AMI_NAME_PREFIX="cpu-ml-ami-x86_64"
fi

AMI_NAME="${AMI_NAME:-${AMI_NAME_PREFIX}-$(date +%Y%m%d-%H%M%S)}"
AMI_DESC="${AMI_DESC:-Ubuntu22.04 CPU AMI for MLOps workflows (${BUILD_ARCH})}"

# Validate dependencies
command -v aws >/dev/null 2>&1 || { echo "ERROR: aws CLI not found"; exit 1; }
command -v packer >/dev/null 2>&1 || { echo "ERROR: packer not found"; exit 1; }

# Get latest Ubuntu 22.04 AMI for the target architecture
if [ "$BUILD_ARCH" = "arm64" ]; then
  AMI_FILTER="ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-arm64-server-*"
  ARCH_FILTER="arm64"
else
  AMI_FILTER="ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"
  ARCH_FILTER="x86_64"
fi

BASE_AMI=$(aws ec2 describe-images \
  --owners 099720109477 \
  --filters "Name=name,Values=${AMI_FILTER}" \
            "Name=architecture,Values=${ARCH_FILTER}" \
  --query 'sort_by(Images,&CreationDate)[-1].ImageId' \
  --output text \
  --region "$AWS_REGION")

if [ -z "$BASE_AMI" ] || [ "$BASE_AMI" = "None" ]; then
  echo "ERROR: Failed to find Ubuntu 22.04 base AMI for ${BUILD_ARCH}"
  exit 1
fi

echo "Building ${BUILD_ARCH} AMI using base AMI: ${BASE_AMI}"

# Initialize & validate packer
packer init "$PACKER_FILE"
packer validate "$PACKER_FILE"

# Build AMI
packer build \
  -var "region=$AWS_REGION" \
  -var "architecture=$BUILD_ARCH" \
  -var "volume_size_gb=$VOLUME_SIZE_GB" \
  -var "ami_name=$AMI_NAME" \
  -var "ami_description=$AMI_DESC" \
  -var "source_ami=$BASE_AMI" \
  "$PACKER_FILE"

# Get and export the custom AMI ID
CUSTOM_AMI=$(aws ec2 describe-images \
  --owners self \
  --filters "Name=name,Values=$AMI_NAME" \
  --query 'sort_by(Images,&CreationDate)[-1].ImageId' \
  --output text \
  --region "$AWS_REGION")

if [ -z "$CUSTOM_AMI" ] || [ "$CUSTOM_AMI" = "None" ]; then
  echo "ERROR: Custom AMI creation failed"
  exit 1
fi

echo "Successfully created ${BUILD_ARCH} AMI: $CUSTOM_AMI"

# Export to environment and bashrc
export_var="CUSTOM_CPU_AMI_ID_${BUILD_ARCH^^}"
# avoid duplicate lines in .bashrc
if [ -f ~/.bashrc ]; then
  grep -qxF "export $export_var=$CUSTOM_AMI" ~/.bashrc 2>/dev/null || echo "export $export_var=$CUSTOM_AMI" >> ~/.bashrc
else
  echo "export $export_var=$CUSTOM_AMI" >> ~/.bashrc
fi
echo "export $export_var=$CUSTOM_AMI"

echo " ${BUILD_ARCH} AMI build completed successfully!"
echo "AMI ID: $CUSTOM_AMI"
echo "Exported as: $export_var"
