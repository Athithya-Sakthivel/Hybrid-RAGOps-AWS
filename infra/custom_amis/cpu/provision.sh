#!/usr/bin/env bash
set -euo pipefail
export HOME="${HOME:-/root}"
export XDG_CACHE_HOME="${XDG_CACHE_HOME:-/tmp/.cache/pip}"
mkdir -p "$XDG_CACHE_HOME"
ARCH="${ARCHITECTURE:-$(uname -m)}"
case "$ARCH" in aarch64|arm64) ARCH=arm64;; x86_64|amd64) ARCH=x86_64;; *) echo "Unsupported architecture: $ARCH"; exit 1;; esac
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a
VENV_PATH="${VENV_PATH:-/opt/venv}"
MODEL_HOME="${MODEL_HOME:-/opt/models}"
RAPIDOCR_MODEL_DIR="${RAPIDOCR_MODEL_DIR:-${MODEL_HOME}/rapidocr}"
PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH"
apt_update_retry(){ local i; for i in 1 6; do if apt-get update -y; then return 0; else sleep $((i*2)); fi; done; return 1; }
apt_install_retry(){ local retries=${1:-3}; shift; local pkgs=("$@"); local i; for i in $(seq 1 $retries); do if apt-get install -y --no-install-recommends -o Dpkg::Options::="--force-confdef" -o Dpkg::Options::="--force-confold" "${pkgs[@]}"; then return 0; else sleep $((i*2)); fi; done; return 1; }
pkg_candidate_available(){ local p="$1"; apt-cache policy "$p" 2>/dev/null | awk '/Candidate:/ {print $2}' | grep -qx "(none)" && return 1 || return 0; }
filter_available_pkgs(){ local -a out=(); for p in "$@"; do if pkg_candidate_available "$p"; then out+=("$p"); fi; done; echo "${out[@]}"; }
pip_install_retry(){ local retries=${1:-3}; shift || true; local args=( "$@" ); local i; for i in $(seq 1 $retries); do if "$VENV_PATH/bin/pip" install --no-cache-dir "${args[@]}"; then return 0; else echo "pip install failed (attempt $i) for: ${args[*]}, retrying..."; sleep $((i*2)); fi; done; return 1; }
CORE_PKGS=(build-essential git curl ca-certificates pkg-config zlib1g-dev libxml2-dev libxslt1-dev libpng-dev libfreetype6-dev libharfbuzz-dev libxcb1-dev libx11-dev libxext-dev libxrender-dev libfontconfig1 libopenjp2-7-dev poppler-utils ffmpeg libmagic1 libgl1-mesa-glx libglib2.0-0 libsm6 libxrender1 libxext6 tesseract-ocr libleptonica-dev libtesseract-dev jq fonts-dejavu-core unzip default-jre-headless tini python3-pip python3-dev ca-certificates)
PYTHON_VENV_PKG_CANDIDATES=(python3-venv python3.10-venv python3.11-venv)
apt_update_retry || { echo "apt-get update failed"; exit 1; }
read -ra CORE_FILTERED <<< "$(filter_available_pkgs "${CORE_PKGS[@]}")"
[ "${#CORE_FILTERED[@]}" -gt 0 ] || { echo "No core packages available; aborting"; exit 1; }
apt_install_retry 3 "${CORE_FILTERED[@]}" || { echo "Failed installing core packages"; exit 1; }
read -ra VENV_FILTERED <<< "$(filter_available_pkgs "${PYTHON_VENV_PKG_CANDIDATES[@]}")"
[ "${#VENV_FILTERED[@]}" -gt 0 ] && apt_install_retry 2 "${VENV_FILTERED[@]}" || true
if ! python3 -m venv "$VENV_PATH"; then TMPPY="$(mktemp -d)"; curl -fsSL "https://bootstrap.pypa.io/get-pip.py" -o "${TMPPY}/get-pip.py" || true; python3 "${TMPPY}/get-pip.py" --disable-pip-version-check || true; python3 -m pip install --upgrade pip setuptools wheel || true; python3 -m venv "$VENV_PATH" || { echo "Failed to create venv after fallback"; exit 1; }; fi
"$VENV_PATH/bin/python" -m pip install --no-cache-dir --upgrade pip setuptools wheel
if ! command -v aws >/dev/null 2>&1; then
  printf '%s %s\n' "$(date --iso-8601=seconds)" "Installing aws CLI v2"
  TMP_AWS_ZIP="$(mktemp --suffix=.zip)"
  curl -fSL --retry 3 -o "${TMP_AWS_ZIP}" "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip"
  unzip -q "${TMP_AWS_ZIP}" -d /tmp || true
  sudo /tmp/aws/install --update || true
  rm -rf /tmp/aws "${TMP_AWS_ZIP}"
fi
export PIP_NO_CACHE_DIR=1
export PATH="$VENV_PATH/bin:$PATH"
"$VENV_PATH/bin/pip" install --no-cache-dir poetry>=1.8.0 poetry-plugin-export huggingface-hub || true
apt_install_retry 2 python3-opencv libopencv-dev || true
PIPELINE_PKGS=(ray[serve]==2.51.0 qdrant-client==1.15.1 neo4j==5.19.0 python-dotenv==1.1.1 typing-extensions==4.12.2 boto3==1.40.26 botocore==1.40.26 requests==2.32.5 trafilatura==2.0.0 tiktoken==0.11.0 PyMuPDF==1.26.4 pdfplumber==0.11.7 numpy==2.2.2 Pillow==11.3.0 pytesseract==0.3.13 lxml==5.4.0 lxml-html-clean==0.4.2 polars==1.33.1 pyarrow==21.0.0 ijson==3.4.0 markdown-it-py==4.0.0 colorama==0.4.6 soundfile==0.13.1 python-pptx==1.0.2 virtualenv httpx==0.28.1 spacy==3.8.7 pydantic==2.11.10)
if [ "${#PIPELINE_PKGS[@]}" -gt 0 ]; then pip_install_retry 3 "${PIPELINE_PKGS[@]}" || echo "Non-fatal: some pip packages failed to install; check logs." >&2; fi
mkdir -p "$MODEL_HOME" "$RAPIDOCR_MODEL_DIR" /workspace/models
getent passwd appuser >/dev/null 2>&1 || useradd --create-home --shell /bin/bash appuser || true
chmod -R 755 "$MODEL_HOME" || true
chown -R appuser:appuser "$MODEL_HOME" || true
"$VENV_PATH/bin/pip" install --no-cache-dir --only-binary=:all: opencv-python-headless==4.12.0.88 || echo "opencv wheel not available; using apt python3-opencv" >&2
"$VENV_PATH/bin/pip" install --no-cache-dir --only-binary=:all: onnxruntime || echo "onnxruntime binary not available; skip" >&2
"$VENV_PATH/bin/pip" install --no-cache-dir --only-binary=:all: rapidocr-onnxruntime==1.4.4 || echo "rapidocr binary not available; skip" >&2
free_mb(){ df --output=avail -m "$1" 2>/dev/null | tail -n1 | awk '{print $1+0}'; }
REPO_FILES={"unsloth/Qwen3-0.6B-GGUF":["Qwen3-0.6B-Q4_K_M.gguf"],"unsloth/Qwen3-1.7B-GGUF":["Qwen3-1.7B-Q4_K_M.gguf"],"unsloth/Qwen3-4B-GGUF":["Qwen3-4B-Q4_K_M.gguf"],"Alibaba-NLP/gte-modernbert-base":["onnx/model_int8.onnx","tokenizer.json","tokenizer_config.json","special_tokens_map.json","config.json","README.md"],"cross-encoder/ms-marco-TinyBERT-L2-v2":["special_tokens_map.json","tokenizer.json","tokenizer_config.json","vocab.txt","onnx/model_quint8_avx2.onnx","onnx/model_qint8_arm64.onnx","onnx/model_O1.onnx","onnx/model_O2.onnx","onnx/model_O3.onnx","onnx/model_O4.onnx"]}
if command -v curl >/dev/null 2>&1 && curl -sSfI https://huggingface.co >/dev/null 2>&1; then python3 - <<'PY'
from __future__ import annotations
import json,os,time,logging
from pathlib import Path
from huggingface_hub import hf_hub_download
logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO"),format="%(asctime)s %(levelname)s: %(message)s")
REPO_FILES=json.loads(os.getenv("REPO_FILES_JSON",'{}'))
out_root=Path(os.getenv("WORKSPACE_MODELS","/workspace/models")).expanduser().resolve()
out_root.mkdir(parents=True,exist_ok=True)
def free_mb(path): 
    import shutil
    total, used, free = shutil.disk_usage(str(path))
    return free//(1024*1024)
sizes_hint={"unsloth/Qwen3-0.6B-GGUF":{"Qwen3-0.6B-Q4_K_M.gguf":2600},"unsloth/Qwen3-1.7B-GGUF":{"Qwen3-1.7B-Q4_K_M.gguf":1700},"unsloth/Qwen3-4B-GGUF":{"Qwen3-4B-Q4_K_M.gguf":2500}}
for repo,files in REPO_FILES.items():
    target_base=out_root/repo.split("/")[-1]; target_base.mkdir(parents=True,exist_ok=True)
    for f in files:
        need_mb=sizes_hint.get(repo,{}).get(f,200)
        if free_mb(out_root) < (need_mb + 300):
            print("Skipping",repo,f,"due to insufficient disk space; need ~",need_mb,"MB free")
            continue
        ok=False
        for attempt in range(1,5):
            try:
                got=hf_hub_download(repo_id=repo, filename=f, local_dir=str(target_base), force_download=False)
                dest=target_base/Path(f)
                dest.chmod(0o444)
                print("Downloaded",repo,f)
                ok=True
                break
            except Exception as e:
                print("warning attempt",attempt,"failed for",repo,f,":",e)
                time.sleep(attempt*2)
        if not ok:
            print("warning: failed to download",repo,f,"after retries")
print(json.dumps({"ok":True,"models_root":str(out_root)}))
PY
else
  echo "Skipping HF downloads: no network"
fi
rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/* || true
apt-get clean || true
echo "Provision completed successfully for ${ARCH}"
