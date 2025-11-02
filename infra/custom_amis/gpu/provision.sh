#!/bin/bash
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then exec sudo bash "$0" "$@"; fi
export DEBIAN_FRONTEND=noninteractive
wait_for_apt(){
  for i in $(seq 1 120); do
    if ! pgrep -f "apt|apt-get|dpkg" >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  return 1
}
wait_for_apt || (sleep 2 && wait_for_apt) || { echo "apt/dpkg locked after timeout" >&2; exit 1; }
apt-get update -y
apt-get install -y --no-install-recommends ca-certificates curl wget gnupg lsb-release software-properties-common build-essential dkms git git-lfs python3.11 python3.11-venv python3.11-dev python3.11-distutils pkg-config unzip
add-apt-repository -y ppa:deadsnakes/ppa || true
apt-get update -y
apt-get install -y --no-install-recommends "linux-headers-$(uname -r)"
# ---------------- NVIDIA runfile (verified)
RUNFILE_URL="https://developer.download.nvidia.com/compute/cuda/12.4.1/local_installers/cuda_12.4.1_550.54.15_linux.run"
RUNFILE="/tmp/cuda_12.4.1_550.54.15_linux.run"
RUNFILE_SHA_EXPECTED="367d2299b3a4588ab487a6d27276ca5d9ead6e394904f18bccb9e12433b9c4fb"
curl -fSL --retry 5 --retry-delay 5 -o "${RUNFILE}" "${RUNFILE_URL}"
ACTUAL_SHA=$(sha256sum "${RUNFILE}" | awk '{print $1}')
if [ "${ACTUAL_SHA}" != "${RUNFILE_SHA_EXPECTED}" ]; then echo "SHA256 mismatch for NVIDIA runfile: expected ${RUNFILE_SHA_EXPECTED}, got ${ACTUAL_SHA}" >&2; exit 2; fi
chmod +x "${RUNFILE}"
sh "${RUNFILE}" --silent --driver --dkms
# ---------------- Python venv & pinned wheels
python3.11 -m venv /opt/venvs/ai
/opt/venvs/ai/bin/python -m pip install --upgrade pip==24.1.2 setuptools==68.0.0 wheel==0.40.0
/opt/venvs/ai/bin/pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cu124 \
  torch==2.6.0+cu124 torchvision==0.21.0+cu124 torchaudio==2.6.0 \
  vllm==0.6.6.post1 huggingface-hub==0.20.3 safetensors==0.4.3 requests tqdm
# ---------------- hf_sync downloader script (downloads model files into /workspace/models)
cat >/usr/local/bin/hf_sync.py <<'PY'
from __future__ import annotations
import argparse, logging, os, shutil, sys, tempfile
from pathlib import Path
from huggingface_hub import hf_hub_download, snapshot_download
logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO"),format="%(levelname)s:%(message)s")
logger=logging.getLogger("hf_sync")
REPOS=[{"repo_id":"Alibaba-NLP/gte-modernbert-base","name":"gte-modernbert-base"},{"repo_id":"cross-encoder/ms-marco-TinyBERT-L2-v2","name":"ms-marco-TinyBERT-L2-v2"}]
COMMON=["README.md","config.json","tokenizer.json","tokenizer_config.json","special_tokens_map.json"]
MODELS=[{"repo_id":"Qwen/Qwen3-4B-AWQ","name":"Qwen3-4B-AWQ","base":"qwen","items":["config.json","model.safetensors","tokenizer.json","README.md"]}]
TMP=Path(os.getenv("TMP_DIR_BASE",tempfile.gettempdir()))/"hf_download"
TMP.mkdir(parents=True,exist_ok=True)
def dl_file(repo,fname,target,force=False):
    if target.exists() and not force:
        logger.info("skip %s/%s",repo,fname); return True
    try:
        got=hf_hub_download(repo_id=repo,filename=fname,cache_dir=str(TMP),force_download=force)
        p=Path(got)
        if p.exists():
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.move(str(p), str(target))
            return True
    except Exception as e:
        logger.warning("hf_hub_download failed %s %s %s",repo,fname,e)
    return False
def dl_snapshot(repo,target_dir,allow_patterns=None,force=False):
    try:
        snapshot_download(repo_id=repo,local_dir=str(target_dir),local_dir_use_symlinks=False,allow_patterns=allow_patterns,force_download=force)
        return True
    except Exception as e:
        logger.warning("snapshot_download failed %s %s",repo,e)
        return False
def main():
    p=argparse.ArgumentParser()
    p.add_argument("--path","-p",default=os.getenv("WORKSPACE_MODELS","/workspace/models"))
    p.add_argument("--force","-f",action="store_true")
    args=p.parse_args()
    out=Path(args.path); out.mkdir(parents=True,exist_ok=True)
    ok=True
    for r in REPOS:
        for item in COMMON:
            if not dl_file(r["repo_id"], item, out/ r["name"]/ item, args.force): ok=False
    for m in MODELS:
        base=m.get("base","llm")
        model_dir=out/ base/ m["name"]
        model_dir.mkdir(parents=True,exist_ok=True)
        allow_patterns=[f"*{it}" for it in m.get("items",[])]
        if not dl_snapshot(m["repo_id"], model_dir, allow_patterns, args.force):
            for item in m.get("items",[]):
                if not dl_file(m["repo_id"], item, model_dir/ item, args.force): ok=False
    if not ok:
        logger.error("some downloads failed"); sys.exit(2)
if __name__=="__main__":
    main()
PY
chmod +x /usr/local/bin/hf_sync.py
# ensure HF token is honored if provided
if [ -n "${HUGGINGFACE_HUB_TOKEN:-}" ]; then export HUGGINGFACE_HUB_TOKEN="${HUGGINGFACE_HUB_TOKEN}"; fi
mkdir -p /workspace/models
chown root:root /workspace/models
chmod 0755 /workspace/models
# run downloader (this will exit non-zero if downloads fail)
echo "Starting model download to /workspace/models (this may take time)..."
 /opt/venvs/ai/bin/python /usr/local/bin/hf_sync.py --path /workspace/models
# ---------------- smoke tests
echo "Running smoke tests..."
/opt/venvs/ai/bin/python - <<'PY'
import torch, sys
print("torch:", torch.__version__)
print("cuda available:", torch.cuda.is_available(), "cuda version:", torch.version.cuda)
import importlib
if importlib.util.find_spec("vllm") is None:
    print("vllm missing", file=sys.stderr); sys.exit(3)
print("vllm import OK")
PY
# cleanup
apt-get -y autoremove
apt-get -y clean
rm -rf /var/lib/apt/lists/*
sync
echo "bootstrap completed"
