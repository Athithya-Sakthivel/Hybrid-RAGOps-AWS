#!/usr/bin/env bash
set -euo pipefail
# ---------- user-editable vars ----------
S3_MODEL_URI="s3://my-bucket/models/qwen-3-0.6b"   # set your S3 path or leave empty to skip sync
REPO_GIT="https://github.com/you/your-repo.git"    # repo containing serve_prod_final.py
REPO_DIR="/opt/serve"
CONDAPATH="/opt/miniconda"
CONDAVENV="py312"
PYTHON_VERSION="3.12.12"
CONDA_PACKAGES="pip setuptools wheel"
PIP_PACKAGES="ray[serve,llm]==2.50.1 vllm==0.11.0 'onnxruntime-gpu>=1.20.0,<1.24.0' awscli"
# ---------- end editable vars ----------

echo "provision.sh: starting"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends build-essential git curl unzip ca-certificates gnupg lsb-release jq awscli
# quick driver / cuda check (do not auto-install drivers here)
if command -v nvidia-smi >/dev/null 2>&1; then
  echo "nvidia-smi present:"
  nvidia-smi || true
else
  echo "WARNING: nvidia-smi not present - ensure base AMI provides NVIDIA driver compatible with CUDA 12.8"
fi

# install miniconda to /opt/miniconda (system-wide)
if [ ! -d "$CONDAPATH" ]; then
  echo "installing miniconda to $CONDAPATH"
  mkdir -p /tmp/miniconda
  curl -fsSL https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh -o /tmp/miniconda/miniconda.sh
  bash /tmp/miniconda/miniconda.sh -b -p "$CONDAPATH"
  rm -f /tmp/miniconda/miniconda.sh
fi
export PATH="$CONDAPATH/bin:$PATH"
# initialize conda for non-interactive shell
eval "$($CONDAPATH/bin/conda shell.bash hook)" || true

# create conda env with Python 3.12.12
if ! conda info --envs | awk '{print $1}' | grep -q "^${CONDAVENV}$"; then
  conda create -y -n "${CONDAVENV}" python="${PYTHON_VERSION}"
fi
conda activate "${CONDAVENV}"
python -m pip install -U pip setuptools wheel
# install PyTorch GPU via conda (preferred for cudatoolkit compatibility)
# This installs PyTorch + CUDA 12.8 toolkits from pytorch + nvidia channels
conda install -y -c pytorch -c nvidia pytorch torchvision torchaudio pytorch-cuda=12.8 || {
  echo "conda install pytorch+cuda failed; attempting pip fallback (may fail for cp312 wheels)"
}
# install pip packages inside env
pip install --no-cache-dir ${PIP_PACKAGES}

# create runtime directories
mkdir -p "${REPO_DIR}"
chown -R ubuntu:ubuntu "${REPO_DIR}" || true
# checkout repo and place serve script
if [ -n "$REPO_GIT" ]; then
  if [ ! -d "${REPO_DIR}/.git" ]; then
    git clone "${REPO_GIT}" "${REPO_DIR}"
  else
    (cd "${REPO_DIR}" && git fetch --all && git reset --hard origin/HEAD)
  fi
fi

# optional: sync model(s) from S3 to /opt/models
if [ -n "$S3_MODEL_URI" ]; then
  mkdir -p /opt/models
  aws s3 sync "$S3_MODEL_URI" /opt/models/$(basename "$S3_MODEL_URI") --no-progress || true
  chown -R ubuntu:ubuntu /opt/models || true
fi

# create systemd unit to run serve script (expects serve_prod_final.py in repo root)
SERVICE_PATH="/etc/systemd/system/serve_prod.service"
cat >"${SERVICE_PATH}" <<'UNIT'
[Unit]
Description=Ray Serve production runner
After=network.target
[Service]
Type=simple
User=ubuntu
WorkingDirectory=/opt/serve
Environment="PATH=/opt/miniconda/envs/py312/bin:/opt/miniconda/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin"
ExecStart=/opt/miniconda/envs/py312/bin/python /opt/serve/serve_prod_final.py
Restart=on-failure
RestartSec=5
LimitNOFILE=65536
[Install]
WantedBy=multi-user.target
UNIT

# enable & do not start service inside packer (start on first boot)
systemctl daemon-reload
systemctl enable serve_prod.service || true

# cleanup
conda clean -afy || true
apt-get autoremove -y
rm -rf /var/lib/apt/lists/* /tmp/*
echo "provision.sh: finished"
