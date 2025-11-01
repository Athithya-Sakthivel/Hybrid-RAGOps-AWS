#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'

export DEBIAN_FRONTEND=noninteractive
export TZ=Etc/UTC

export MODEL_HOME="/opt/models"
export HF_HOME="$MODEL_HOME/hf"
export HF_HUB_CACHE="$HF_HOME/hub"
export HF_ASSETS_CACHE="$HF_HOME/assets"
export RAPIDOCR_MODEL_DIR="$MODEL_HOME/rapidocr"
PULUMI_VERSION="3.196.0"

# ensure ~/.bashrc exists before appending
touch "${HOME}/.bashrc"

# sudo check (will prompt if needed)
if ! sudo -n true 2>/dev/null; then
  printf '%s %s\n' "$(date --iso-8601=seconds)" "sudo access required: you'll be prompted for password..."
fi

# docker binfmt (best-effort)
docker run --privileged --rm tonistiigi/binfmt --install all >/dev/null 2>&1 || true

printf '%s %s\n' "$(date --iso-8601=seconds)" "Updating apt and installing base packages"
sudo apt-get update -yq
sudo apt-get upgrade -yq || true
sudo apt-get install -yq --no-install-recommends \
  ca-certificates curl wget git gh sudo tree jq unzip vim make python3.10-venv python3-pip \
  build-essential gnupg lsb-release software-properties-common zip apt-transport-https \
  fonts-dejavu fonts-liberation dos2unix yamllint

printf '%s %s\n' "$(date --iso-8601=seconds)" "Installing yq"
sudo wget -q https://github.com/mikefarah/yq/releases/latest/download/yq_linux_amd64 -O /usr/local/bin/yq
sudo chmod +x /usr/local/bin/yq

printf '%s %s\n' "$(date --iso-8601=seconds)" "Installing git-lfs"
sudo curl -s https://packagecloud.io/install/repositories/github/git-lfs/script.deb.sh | sudo bash -s -- >/dev/null 2>&1 || true
sudo apt-get update -yq
sudo apt-get install -y git-lfs
sudo git lfs install || true

# aws cli v2
if ! command -v aws >/dev/null 2>&1; then
  printf '%s %s\n' "$(date --iso-8601=seconds)" "Installing aws CLI v2"
  TMP_AWS_ZIP="$(mktemp --suffix=.zip)"
  curl -fSL --retry 3 -o "${TMP_AWS_ZIP}" "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip"
  unzip -q "${TMP_AWS_ZIP}" -d /tmp || true
  sudo /tmp/aws/install --update || true
  rm -rf /tmp/aws "${TMP_AWS_ZIP}"
fi

# pulumi
if ! command -v pulumi >/dev/null 2>&1 || [[ "$(pulumi version 2>/dev/null || true)" != *"${PULUMI_VERSION}"* ]]; then
  printf '%s %s\n' "$(date --iso-8601=seconds)" "Installing pulumi ${PULUMI_VERSION}"
  URL="https://get.pulumi.com/releases/sdk/pulumi-v${PULUMI_VERSION}-linux-x64.tar.gz"
  TMP_FILE="$(mktemp -u)/pulumi.tgz.$$"
  TMP_FILE="$(mktemp)"
  curl -fSL --retry 5 -C - -o "${TMP_FILE}" "${URL}"
  sudo tar -xzf "${TMP_FILE}" -C /usr/local/bin --strip-components=1 || true
  rm -f "${TMP_FILE}"
fi

# tesseract (ppa)
if ! grep -q "^deb .*ppa.launchpadcontent.net/alex-p/tesseract-ocr5" /etc/apt/sources.list* 2>/dev/null; then
  sudo add-apt-repository -y ppa:alex-p/tesseract-ocr5 || true
fi
sudo apt-get update -yq
sudo apt-get install -y tesseract-ocr libtesseract-dev libleptonica-dev || true

# directories and permissions
sudo mkdir -p "${MODEL_HOME}/hf/hub" "${MODEL_HOME}/hf/assets" "${RAPIDOCR_MODEL_DIR}" /workspace/data
sudo chmod -R 0775 "${MODEL_HOME}" /workspace || true
sudo chown -R "$(id -u):$(id -g)" "${MODEL_HOME}" /workspace || true

# append exports to ~/.bashrc idempotently
for line in \
  "export MODEL_HOME=\"${MODEL_HOME}\"" \
  "export HF_HOME=\"${HF_HOME}\"" \
  "export HF_HUB_CACHE=\"${HF_HUB_CACHE}\"" \
  "export HF_ASSETS_CACHE=\"${HF_ASSETS_CACHE}\"" \
  "export RAPIDOCR_MODEL_DIR=\"${RAPIDOCR_MODEL_DIR}\"" \
  'export DEBIAN_FRONTEND=noninteractive' \
  'export PIP_ROOT_USER_ACTION=ignore'
do
  grep -Fxq "$line" "${HOME}/.bashrc" 2>/dev/null || printf '%s\n' "$line" >> "${HOME}/.bashrc"
done

printf '%s %s\n' "$(date --iso-8601=seconds)" "Verification (versions):"
for cmd in aws pulumi git python3 pip3; do
  if command -v "${cmd}" >/dev/null 2>&1; then
    printf '  %-8s -> %s\n' "${cmd}" "$(${cmd} --version 2>/dev/null | head -n1 || echo 'version unknown')"
  else
    printf '  %-8s -> not installed\n' "${cmd}"
  fi
done

printf '%s %s\n' "$(date --iso-8601=seconds)" "Installing Python packages"
pip3 install --upgrade pip || true
pip3 install huggingface_hub==0.34.4 || true
pip3 install "pulumi==${PULUMI_VERSION}" pulumi-aws==7.7.0 || true

# rapidocr model download (best-effort)
sudo mkdir -p "${RAPIDOCR_MODEL_DIR}" && sudo chown -R "$(id -u):$(id -g)" "${MODEL_HOME}" || true
cd "${RAPIDOCR_MODEL_DIR}" || true
for url in \
  "https://huggingface.co/SWHL/RapidOCR/resolve/main/PP-OCRv4/ch_PP-OCRv4_det_infer.onnx" \
  "https://huggingface.co/SWHL/RapidOCR/resolve/main/PP-OCRv4/ch_PP-OCRv4_rec_infer.onnx"
do
  out="$(basename "$url")"
  if [ ! -f "$out" ]; then
    printf '%s %s\n' "$(date --iso-8601=seconds)" "Downloading $out"
    curl -fSL --retry 5 -C - -o "$out" "$url" || true
  fi
done
cd - >/dev/null 2>&1 || true

# ffmpeg static (best-effort)
if curl -I -s https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz >/dev/null 2>&1; then
  printf '%s %s\n' "$(date --iso-8601=seconds)" "Downloading ffmpeg static"
  TMP_FF="$(mktemp --suffix=.tar.xz)"
  sudo curl -L https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz -o "${TMP_FF}" || true
  if [ -f "${TMP_FF}" ]; then
    tar -xf "${TMP_FF}" -C /tmp || true
    sudo cp /tmp/ffmpeg-*-amd64-static/ffmpeg /usr/local/bin/ 2>/dev/null || true
    sudo cp /tmp/ffmpeg-*-amd64-static/ffprobe /usr/local/bin/ 2>/dev/null || true
    rm -rf /tmp/ffmpeg* "${TMP_FF}" || true
  fi
fi

# libreoffice headless + UNO bridge (best-effort)
printf '%s %s\n' "$(date --iso-8601=seconds)" "Installing LibreOffice (headless) + UNO bridge..."
sudo add-apt-repository ppa:libreoffice/ppa -y >/dev/null 2>&1 || true
sudo apt-get update -yq
sudo apt-get install -y \
  libreoffice-script-provider-python \
  libreoffice-core \
  libreoffice-writer \
  libreoffice-calc \
  python3-uno \
  --no-install-recommends || true

# pulumi aws plugin (best-effort)
pulumi plugin install resource aws v7.7.0 2>/dev/null || true

# spacy model (best-effort)
sudo mkdir -p /models/spacy || true
sudo chown -R "$(id -u):$(id -g)" /models || true
python3 -m spacy download en_core_web_sm --target /models/spacy >/dev/null 2>&1 || true

# docker buildx plugin (best-effort)
if ! sudo apt-get install -y docker-buildx-plugin >/dev/null 2>&1; then
  mkdir -p "${HOME}/.docker/cli-plugins"
  BUILDX_URL="$(curl -s https://api.github.com/repos/docker/buildx/releases/latest | grep 'browser_download_url' | grep 'linux-amd64' | head -n1 | cut -d '"' -f4 || true)"
  if [ -n "${BUILDX_URL}" ]; then
    curl -sL "${BUILDX_URL}" -o "${HOME}/.docker/cli-plugins/docker-buildx" || true
    chmod +x "${HOME}/.docker/cli-plugins/docker-buildx" || true
  fi
fi
sudo apt-get install -y uuid-runtime || true

# project pip requirements (best-effort)
if [ -f indexing_pipeline/requirements.txt ]; then
  pip3 install -r indexing_pipeline/requirements.txt || true
fi
if [ -f infra/requirements.txt ]; then
  pip3 install -r infra/requirements.txt || true
fi

# optional helper scripts (best-effort)
if [ -f utils/archive/download_faster_whisper.py ]; then
  sudo python3 utils/archive/download_faster_whisper.py /models || true
fi
if [ -f utils/download_onnx.py ]; then
  sudo python3 utils/download_onnx.py /models || true
fi

sudo apt-get update -y && sudo apt-get install -y wget unzip
wget -q https://releases.hashicorp.com/packer/1.11.2/packer_1.11.2_linux_amd64.zip
sudo unzip -o packer_1.11.2_linux_amd64.zip -d /usr/local/bin/
rm -rf packer_1.11.2_linux_amd64.zip
packer --version


clear || true
echo "Bootstrap completed. Open a new terminal or run: source ~/.bashrc"
