#!/usr/bin/env bash
set -euo pipefail
ARCHITECTURE="${ARCHITECTURE:-$(uname -m)}"
case "$ARCHITECTURE" in aarch64|arm64) ARCHITECTURE=arm64;; x86_64|amd64) ARCHITECTURE=x86_64;; *) echo "Unsupported architecture: $ARCHITECTURE"; exit 1;; esac
export DEBIAN_FRONTEND="${DEBIAN_FRONTEND:-noninteractive}"
VENV_PATH="${VENV_PATH:-/opt/venv}"
MODEL_HOME="${MODEL_HOME:-/opt/models}"
RAPIDOCR_MODEL_DIR="${RAPIDOCR_MODEL_DIR:-${MODEL_HOME}/rapidocr}"
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH"
echo "Provision start for architecture: ${ARCHITECTURE}"
if [ "$(id -u)" -ne 0 ]; then echo "Provisioner must run as root"; exit 1; fi
apt_update_retry(){ local i; for i in 1 6; do if apt-get update -y; then return 0; else sleep $((i*2)); fi; done; return 1; }
apt_install_retry(){ local retries=${1:-3}; shift; local pkgs=("$@"); local i; for i in $(seq 1 $retries); do if apt-get install -y --no-install-recommends "${pkgs[@]}"; then return 0; else sleep $((i*2)); fi; done; return 1; }
pkg_candidate_available(){ local p="$1"; if apt-cache policy "$p" 2>/dev/null | awk '/Candidate:/ {print $2}' | grep -qx "(none)"; then return 1; else return 0; fi }
filter_available_pkgs(){ local -a out=(); for p in "$@"; do if pkg_candidate_available "$p"; then out+=("$p"); else echo "Skipping unavailable package: $p" >&2; fi; done; echo "${out[@]}"; }
CORE_PKGS=(build-essential git curl ca-certificates pkg-config zlib1g-dev libxml2-dev libxslt1-dev libpng-dev libfreetype6-dev libharfbuzz-dev libxcb1-dev libx11-dev libxext-dev libxrender-dev libfontconfig1 libopenjp2-7-dev poppler-utils ffmpeg libmagic1 libgl1-mesa-glx libglib2.0-0 libsm6 libxrender1 libxext6 tesseract-ocr libleptonica-dev libtesseract-dev jq fonts-dejavu-core unzip default-jre-headless tini python3-pip python3-dev ca-certificates)
OPTIONAL_DEB=(libjpeg62-turbo libjpeg-dev libreoffice-core libreoffice-writer libreoffice-calc)
PYTHON_VENV_PKG_CANDIDATES=(python3-venv python3.10-venv python3.11-venv)
apt_update_retry || { echo "apt-get update failed"; exit 1; }
read -ra CORE_FILTERED <<< "$(filter_available_pkgs "${CORE_PKGS[@]}")"
if [ "${#CORE_FILTERED[@]}" -eq 0 ]; then echo "No core packages available; aborting"; exit 1; fi
apt_install_retry 3 "${CORE_FILTERED[@]}" || { echo "Failed installing core packages"; exit 1; }
read -ra OPTIONAL_FILTERED <<< "$(filter_available_pkgs "${OPTIONAL_DEB[@]}")"
if [ "${#OPTIONAL_FILTERED[@]}" -gt 0 ]; then apt_install_retry 2 "${OPTIONAL_FILTERED[@]}" || echo "Non-fatal: optional deb install failed" >&2; fi
PYVENV_INSTALLED=false
for cand in "${PYTHON_VENV_PKG_CANDIDATES[@]}"; do
  if pkg_candidate_available "$cand"; then
    if apt_install_retry 2 "$cand"; then PYVENV_INSTALLED=true; break; fi
  fi
done
if [ "$PYVENV_INSTALLED" = false ]; then
  if command -v python3 >/dev/null 2>&1 && python3 -c "import ensurepip" >/dev/null 2>&1; then PYVENV_INSTALLED=true; fi
fi
if [ "$PYVENV_INSTALLED" = false ]; then echo "Warning: python3 venv support not available; attempting fallback install of python3-venv and python3.10-venv"; apt_install_retry 2 python3-venv || apt_install_retry 2 python3.10-venv || true; fi
if ! python3 -m venv "${VENV_PATH}"; then
  echo "python3 -m venv failed; attempting to install ensurepip via get-pip.py into a venv fallback"
  TMPPY="$(mktemp -d)"
  curl -fsSL "https://bootstrap.pypa.io/get-pip.py" -o "${TMPPY}/get-pip.py" || true
  python3 "${TMPPY}/get-pip.py" --disable-pip-version-check || true
  python3 -m pip install --upgrade pip setuptools wheel || true
  python3 -m venv "${VENV_PATH}" || { echo "Failed to create venv after fallback"; exit 1; }
fi
"${VENV_PATH}/bin/python" -m pip install --upgrade pip setuptools wheel || true
PIP_COMMON=(pip setuptools wheel huggingface-hub)
"${VENV_PATH}/bin/pip" install --no-cache-dir "${PIP_COMMON[@]}" || true
PIP_PKGS=(ray[serve]==2.51.0 qdrant-client==1.15.1 neo4j==5.19.0 python-dotenv==1.1.1 typing-extensions==4.12.2 boto3==1.40.26 botocore==1.40.26 requests==2.32.5 trafilatura==2.0.0 tiktoken==0.11.0 PyMuPDF==1.26.4 pdfplumber==0.11.7 numpy==2.2.2 Pillow==11.3.0 pytesseract==0.3.13 lxml==5.4.0 lxml-html-clean==0.4.2 polars==1.33.1 pyarrow==21.0.0 ijson==3.4.0 markdown-it-py==4.0.0 colorama==0.4.6 soundfile==0.13.1 python-pptx==1.0.2 virtualenv httpx==0.28.1 spacy==3.8.7 pydantic==2.11.10 awscli==1.29.31)
"${VENV_PATH}/bin/pip" install --no-cache-dir "${PIP_PKGS[@]}" || echo "Non-fatal: some pip packages failed to install; check logs." >&2
if [ "${ARCHITECTURE}" = "arm64" ]; then
  "${VENV_PATH}/bin/pip" install --no-cache-dir torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cpu || echo "Warning: torch install failed on arm64 (non-fatal)." >&2
  "${VENV_PATH}/bin/pip" install --no-cache-dir opencv-python-headless==4.12.0.88 --no-binary opencv-python-headless || echo "Warning: opencv install failed on arm64 (non-fatal)." >&2
  "${VENV_PATH}/bin/pip" install --no-cache-dir rapidocr-onnxruntime==1.4.4 onnxruntime==1.23.2 || echo "Warning: rapidocr/onnxruntime install failed (non-fatal)." >&2
  "${VENV_PATH}/bin/pip" install --no-cache-dir faster-whisper==1.2.0 || echo "Warning: faster-whisper install failed (non-fatal)." >&2
else
  "${VENV_PATH}/bin/pip" install --no-cache-dir opencv-python-headless==4.12.0.88 rapidocr-onnxruntime==1.4.4 faster-whisper==1.2.0 || echo "Warning: optional x86 packages failed (non-fatal)." >&2
fi
mkdir -p "${MODEL_HOME}" "${RAPIDOCR_MODEL_DIR}" /workspace/models
getent passwd appuser >/dev/null 2>&1 || useradd --create-home --shell /bin/bash appuser || true
chmod -R 755 "${MODEL_HOME}" || true
chown -R appuser:appuser "${MODEL_HOME}" || true
if command -v curl >/dev/null 2>&1 && curl -sSfI https://huggingface.co >/dev/null 2>&1; then
python3 - <<'PY'
from __future__ import annotations
import json,os,tempfile,logging
from pathlib import Path
try:
    from huggingface_hub import hf_hub_download
except Exception as e:
    print("hf_hub not available:",e); raise SystemExit(0)
logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO"),format="%(asctime)s %(levelname)s: %(message)s")
REPO_FILES={
"unsloth/Qwen3-0.6B-GGUF":["Qwen3-0.6B-Q4_K_M.gguf"],
"unsloth/Qwen3-1.7B-GGUF":["Qwen3-1.7B-Q4_K_M.gguf"],
"unsloth/Qwen3-4B-GGUF":["Qwen3-4B-Q4_K_M.gguf"],
"Alibaba-NLP/gte-modernbert-base":["onnx/model_int8.onnx","tokenizer.json","tokenizer_config.json","special_tokens_map.json","config.json","README.md"],
"cross-encoder/ms-marco-TinyBERT-L2-v2":["special_tokens_map.json","tokenizer.json","tokenizer_config.json","vocab.txt","onnx/model_quint8_avx2.onnx","onnx/model_qint8_arm64.onnx","onnx/model_O1.onnx","onnx/model_O2.onnx","onnx/model_O3.onnx","onnx/model_O4.onnx"]
}
out_root=Path(os.getenv("WORKSPACE_MODELS","/workspace/models")).expanduser().resolve()
out_root.mkdir(parents=True,exist_ok=True)
for repo,files in REPO_FILES.items():
    target_base=out_root/repo.split("/")[-1]
    target_base.mkdir(parents=True,exist_ok=True)
    for f in files:
        try:
            got=hf_hub_download(repo,filename=f,local_dir=str(tempfile.gettempdir()),force_download=False)
            dest=target_base/Path(f)
            dest.parent.mkdir(parents=True,exist_ok=True)
            Path(got).replace(dest)
            dest.chmod(0o444)
            print("Downloaded",repo,f)
        except Exception as e:
            print("warning: failed to download",repo,f,":",e)
print(json.dumps({"ok":True,"models_root":str(out_root)}))
PY
else
  echo "Skipping HF downloads: no network or curl failed"
fi
rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/* || true
apt-get clean || true
echo "Provision completed successfully for ${ARCHITECTURE}"
