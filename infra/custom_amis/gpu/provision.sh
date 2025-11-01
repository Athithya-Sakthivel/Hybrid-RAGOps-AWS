#!/usr/bin/env bash
set -euo pipefail
# provision.sh - offline GPU AMI provisioning on Ubuntu 22.04 (no external repo/service at runtime)
# Usage: run via packer during image build. Export HF_AUTH_TOKEN if private HF models required.
HF_AUTH_TOKEN="${HF_AUTH_TOKEN:-}"
WORKSPACE_MODELS="${WORKSPACE_MODELS:-/workspace/models}"
CONDAPATH="${CONDAPATH:-/opt/miniconda}"
CONDAVENV="${CONDAVENV:-py311}"
PYTHON_VERSION="${PYTHON_VERSION:-3.11.12}"
TMP_DIR_BASE="${TMP_DIR_BASE:-/tmp/hf_download}"
PIP_PACKAGES="${PIP_PACKAGES:-ray[serve,llm]==2.50.1 vllm==0.11.0 huggingface_hub transformers tokenizers 'onnxruntime-gpu>=1.20.0,<1.24.0' spacy safetensors sentencepiece accelerate tqdm}"
echo "provision.sh: start"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends build-essential dkms linux-headers-$(uname -r) git curl wget unzip ca-certificates gnupg lsb-release jq python3-venv
# Install recommended NVIDIA driver via ubuntu-drivers (will pick recommended driver for GPU)
if command -v ubuntu-drivers >/dev/null 2>&1; then
  echo "Installing recommended NVIDIA driver via ubuntu-drivers..."
  ubuntu-drivers devices || true
  ubuntu-drivers autoinstall || true
else
  echo "ubuntu-drivers not found; skipping auto driver install. Ensure driver >=570 is present later."
fi
# Install CUDA 12.8 toolkit packages (attempt)
echo "Adding NVIDIA APT repo for CUDA 12.8"
distribution="ubuntu2204"
CUDA_REPO_KEYRING="/usr/share/keyrings/nvidia-archive-keyring.gpg"
set +e
wget -qO /tmp/cuda-keyring.deb https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2204/x86_64/cuda-keyring_1.0-1_all.deb
if [ -f /tmp/cuda-keyring.deb ]; then
  dpkg -i /tmp/cuda-keyring.deb || true
  rm -f /tmp/cuda-keyring.deb
  apt-get update -y
  apt-get install -y --no-install-recommends cuda-toolkit-12-8 || echo "cuda-toolkit-12-8 install failed; continue and hope runtime libs available"
else
  echo "Failed to fetch cuda-keyring; skipping explicit cuda-toolkit install"
fi
set -e
# Verify driver presence
if command -v nvidia-smi >/dev/null 2>&1; then
  echo "nvidia-smi present:"
  nvidia-smi || true
else
  echo "WARNING: nvidia-smi not present or driver missing; ensure driver >=570 and CUDA runtime present on target"
fi
# Install NVIDIA Container Toolkit (nvidia-docker) so docker with GPU works on this AMI
distribution=$(. /etc/os-release; echo $ID$VERSION_ID)
curl -s -L https://nvidia.github.io/nvidia-docker/gpgkey | apt-key add - || true
curl -s -L https://nvidia.github.io/nvidia-docker/$distribution/nvidia-docker.list | tee /etc/apt/sources.list.d/nvidia-docker.list
apt-get update -y
apt-get install -y --no-install-recommends nvidia-docker2 || true
systemctl restart docker || true
# prepare models dir
mkdir -p "${WORKSPACE_MODELS}"
chmod 0775 "${WORKSPACE_MODELS}"
# install miniconda
if [ ! -d "${CONDAPATH}" ]; then
  echo "Installing Miniconda to ${CONDAPATH}"
  mkdir -p /tmp/miniconda
  wget -qO /tmp/miniconda/miniconda.sh "https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh"
  bash /tmp/miniconda/miniconda.sh -b -p "${CONDAPATH}"
  rm -f /tmp/miniconda/miniconda.sh
fi
export PATH="${CONDAPATH}/bin:${PATH}"
eval "$(${CONDAPATH}/bin/conda shell.bash hook)" || true
if ! conda info --envs | awk '{print $1}' | grep -q "^${CONDAVENV}$"; then
  conda create -y -n "${CONDAVENV}" python="${PYTHON_VERSION}"
fi
conda activate "${CONDAVENV}"
python -m pip install -U pip setuptools wheel
# Install PyTorch + CUDA (conda preferred)
set +e
conda install -y -c pytorch -c nvidia pytorch torchvision torchaudio pytorch-cuda=12.8
CONDA_RC=$?
set -e
if [ "${CONDA_RC}" -ne 0 ]; then
  echo "Conda PyTorch install failed; attempting pip fallback (may fail for cp311 wheels)"
  python -m pip install --index-url https://download.pytorch.org/whl/cu128 "torch>=2.8.0" torchvision torchaudio || true
fi
# Install pip packages into env
python -m pip install --no-cache-dir ${PIP_PACKAGES}
# make tmp download dir
mkdir -p "${TMP_DIR_BASE}"
chmod 0775 "${TMP_DIR_BASE}"
# write downloader script (deterministic location)
HF_DOWNLOADER_DIR="/opt/hf_downloader"
mkdir -p "${HF_DOWNLOADER_DIR}"
cat > "${HF_DOWNLOADER_DIR}/download_hf.py" <<'PY'
#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, logging, os, shutil, sys, tempfile
from pathlib import Path
from typing import List, Dict, Any
try:
    from huggingface_hub import hf_hub_download
except Exception as e:
    raise ImportError("huggingface_hub is required. Install with: pip install huggingface_hub") from e
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(levelname)s: %(message)s")
logger = logging.getLogger("download_hf")
REPOS: List[Dict[str, str]] = [
    {"repo_id": "Alibaba-NLP/gte-modernbert-base", "name": "gte-modernbert-base"},
    {"repo_id": "cross-encoder/ms-marco-TinyBERT-L2-v2", "name": "ms-marco-TinyBERT-L2-v2"},
]
COMMON_ITEMS = ["README.md","config.json","tokenizer.json","tokenizer_config.json","special_tokens_map.json"]
REQUIRED_ONNX_FILES = ["onnx/model_quint8_avx2.onnx","onnx/model_O1.onnx","onnx/model_O2.onnx","onnx/model_O3.onnx","onnx/model_O4.onnx"]
REQUIRED_REPO_EXTRA = ["vocab.txt"]
MODELS: List[Dict[str, Any]] = [
  {"repo_id":"Qwen/Qwen3-4B-AWQ","name":"Qwen3-4B-AWQ","base":"qwen","items":["config.json","model.safetensors","tokenizer.json","README.md"]},
]
TMP_DIR_BASE = Path(os.getenv("TMP_DIR_BASE", tempfile.gettempdir())) / "hf_download"
TMP_DIR_BASE.mkdir(parents=True, exist_ok=True)
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Download model artifacts from Hugging Face repos into a local folder.")
    p.add_argument("--path","-p",dest="models_dir",type=Path,default=Path(os.getenv("WORKSPACE_MODELS","/workspace/models")),help="Root directory for models")
    p.add_argument("--force","-f",action="store_true",help="Force re-download")
    p.add_argument("--repos","-r",type=str,help="JSON file list of repos (overrides built-in)")
    p.add_argument("--models-json","-m",type=str,help="JSON file list of models (overrides built-in)")
    return p.parse_args()
def download_one(repo_id: str, remote: str, target: Path, force: bool, tmp_dir_base: Path = TMP_DIR_BASE) -> bool:
    if target.exists() and not force:
        logger.info("SKIP exists %s", target)
        return True
    tmp_dir = tmp_dir_base / repo_id.replace("/","_")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    try:
        got = hf_hub_download(repo_id=repo_id, filename=remote, local_dir=str(tmp_dir), local_dir_use_symlinks=False, force_download=force)
        got_path = Path(got)
        if got_path.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                try:
                    target.unlink()
                except Exception:
                    pass
            shutil.move(str(got_path), str(target))
            try:
                os.chmod(str(target), 0o444)
            except Exception:
                pass
            logger.info("Downloaded %s:%s -> %s", repo_id, remote, target)
            return True
    except Exception as e:
        logger.warning("Failed to download %s:%s (%s)", repo_id, remote, e)
    return False
def ensure_repo_style(repo_id: str, name: str, model_root: Path, force: bool) -> bool:
    ok = True
    for item in COMMON_ITEMS:
        target = model_root / item
        if not download_one(repo_id, item, target, force):
            ok = False
            logger.error("Missing required %s:%s", name, item)
    onnx_dir = model_root / "onnx"
    fallback_target = onnx_dir / "model.onnx"
    _ = download_one(repo_id, "onnx/model.onnx", fallback_target, force) or download_one(repo_id, "onnx/model_int8.onnx", fallback_target, force)
    if repo_id.lower().strip() in ("cross-encoder/ms-marco-tinybert-l2-v2",):
        for onnx_file in REQUIRED_ONNX_FILES:
            target = model_root / onnx_file
            if not download_one(repo_id, onnx_file, target, force):
                ok = False
                logger.error("Missing required %s:%s", name, onnx_file)
        for extra in REQUIRED_REPO_EXTRA:
            target = model_root / extra
            if not download_one(repo_id, extra, target, force):
                ok = False
                logger.error("Missing required %s:%s", name, extra)
    return ok
def ensure_model_style(model: Dict[str, Any], workspace_models: Path, force: bool) -> bool:
    repo_id = model["repo_id"]
    name = model["name"]
    base = model.get("base","llm")
    model_root = workspace_models / base / name
    ok = True
    for item in model.get("items",[]):
        remote_rel = str(item)
        target = model_root / Path(remote_rel)
        required = not remote_rel.endswith("special_tokens_map.json")
        success = download_one(repo_id, remote_rel, target, force)
        if not success and required:
            ok = False
            logger.error("Missing required %s:%s", name, remote_rel)
    return ok
def main() -> None:
    args = parse_args()
    workspace_models = args.models_dir.expanduser().resolve()
    workspace_models.mkdir(parents=True, exist_ok=True)
    force = args.force or os.getenv("FORCE_DOWNLOAD","0").lower() in ("1","true","yes")
    repos = REPOS
    if args.repos:
        repos_path = Path(args.repos).expanduser()
        try:
            with repos_path.open("r",encoding="utf-8") as fh:
                loaded = json.load(fh)
                if isinstance(loaded,list):
                    valid = all(isinstance(entry,dict) and "repo_id" in entry and "name" in entry for entry in loaded)
                    if valid:
                        repos = loaded
        except Exception as e:
            logger.warning("Failed to load repos JSON %s: %s", repos_path, e)
    models = MODELS
    if args.models_json:
        models_path = Path(args.models_json).expanduser()
        try:
            with models_path.open("r",encoding="utf-8") as fh:
                loaded = json.load(fh)
                if isinstance(loaded,list):
                    models = loaded
        except Exception as e:
            logger.warning("Failed to load models JSON %s: %s", models_path, e)
    logger.info("Using models root: %s", workspace_models)
    logger.info("Force download: %s", bool(force))
    logger.info("Temporary download directory: %s", TMP_DIR_BASE)
    all_ok = True
    for repo in repos:
        repo_id = repo["repo_id"]
        name = repo["name"]
        model_root = workspace_models / name
        logger.info("Ensuring repo-style model %s (repo: %s) -> %s", name, repo_id, model_root)
        if not ensure_repo_style(repo_id, name, model_root, force):
            all_ok = False
    for m in models:
        logger.info("Ensuring model-style entry %s (repo: %s)", m.get("name","<unknown>"), m.get("repo_id","<unknown>"))
        if not ensure_model_style(m, workspace_models, force):
            all_ok = False
    if not all_ok:
        logger.error("Some files failed to download under %s", workspace_models)
        sys.exit(2)
    logger.info("All required model artifacts are present under %s", workspace_models)
if __name__ == "__main__":
    main()
PY
chmod 0755 "${HF_DOWNLOADER_DIR}/download_hf.py"
# export HF token for downloader if provided
if [ -n "${HF_AUTH_TOKEN}" ]; then
  export HF_HOME="${HOME}/.cache/huggingface"
  mkdir -p "${HF_HOME}"
  export HUGGINGFACE_HUB_TOKEN="${HF_AUTH_TOKEN}"
  export HF_HUB_TOKEN="${HF_AUTH_TOKEN}"
fi
export TMP_DIR_BASE
echo "Running HF downloader into ${WORKSPACE_MODELS}"
python "${HF_DOWNLOADER_DIR}/download_hf.py" --path "${WORKSPACE_MODELS}" --force
# install spaCy small model into /models/spacy (deterministic)
SPACY_TARGET="/models/spacy"
mkdir -p "${SPACY_TARGET}"
python -m spacy download en_core_web_sm --target "${SPACY_TARGET}" || true
chmod -R a+r "${SPACY_TARGET}" || true
# remove possibly conflicting onnx file as user requested
if [ -f /workspace/models/gte-modernbert-base/onnx/model.onnx ]; then
  rm -f /workspace/models/gte-modernbert-base/onnx/model.onnx || true
fi
# final perms & cleanup
chown -R root:root "${WORKSPACE_MODELS}" || true
chmod -R a+r "${WORKSPACE_MODELS}" || true
conda clean -afy || true
python -m pip cache purge || true
apt-get autoremove -y || true
rm -rf /var/lib/apt/lists/* /tmp/* "${TMP_DIR_BASE}" || true
echo "provision.sh: finished. Models baked under ${WORKSPACE_MODELS}"
