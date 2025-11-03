# Custom GPU AMI for **Ray VM Autoscaler** — concise handbook (version-pinned)

**Audience:** platform engineers / SREs who build, test and operate a minimal, deterministic GPU AMI used by Ray EC2 Autoscaler (G4dn, G5, G6).
**Goal:** produce an immutable AMI with kernel + NVIDIA driver + pinned Python ML stack + baked RAG model artifacts so Ray worker VMs boot fast and run GPU workloads without any on-boot downloads.

---

## 1 — Facts & pinned components (baseline, exact pins)

* **Base OS:** **Ubuntu 22.04.4 LTS (Jammy)** — kernel **5.15.0-105.115 (linux-aws / 5.15.0-105)**. ([Launchpad][1])
* **Kernel & build tools (package versions from Jammy):**

  * `build-essential=12.9ubuntu3`. ([Launchpad][2])
  * `dkms=2.8.7-2ubuntu2.2`. ([ubuntuupdates.org][3])
  * `linux-headers-5.15.0-105.115` (install `linux-headers-$(uname -r)` during image build so headers match kernel). ([Launchpad][4])
* **NVIDIA driver (exact):** **Data Center Driver 550.54.15** installed from the CUDA 12.4.1 runfile `cuda_12.4.1_550.54.15_linux.run` (downloaded from NVIDIA and verified by SHA256). URL pattern: `https://developer.download.nvidia.com/compute/cuda/12.4.1/local_installers/cuda_12.4.1_550.54.15_linux.run`. ([NVIDIA Developer][5])
* **No containers:** **no containerd / Docker / nvidia-container-toolkit** — this AMI runs workloads directly on the host VM (Ray tasks import torch/vLLM directly).
* **Python & ML (exact wheels / versions):**

  * **Python runtime:** `python3.11` (Jammy package; build with the system `python3.11` or install `python3.11` from deadsnakes if required; common Jammy package shows `python3.11` packaging around `3.11.9-1+jammy1`). ([Discussions on Python.org][6])
  * `pip` (upgrade in venv): pin `pip` in build to a fixed microversion if you require boot-to-boot reproducibility (e.g. `pip==24.1.2` — set in CI artifact manifest).
  * **PyTorch / CUDA wheels (exact):**
    `torch==2.6.0+cu124`
    `torchvision==0.21.0+cu124`
    `torchaudio==2.6.0`
    Install via the official CUDA-12.4 PyTorch wheel index: `--index-url https://download.pytorch.org/whl/cu124`. ([PyTorch][7])
  * **vLLM (exact):** `vllm==0.6.6.post1`. (pin this exact wheel built for CUDA 12.4 / Python 3.11). ([deps.dev][8])
  * **HF tooling & safetensors (exact):** `huggingface-hub==0.20.3`, `safetensors==0.4.3`. ([deps.dev][9])
* **Models baked into AMI:** files placed under `/workspace/models` — see §4 for exact model repo_ids and path layout (manifest required, see §4).

---

## 2 — Why this exact, pinned approach

* **Immutable & repeatable:** pinned OS kernel + headers + driver runfile + exact wheel versions → identical runtime across all worker VMs.
* **Fast boot & predictable availability:** driver binary and modules are present (installed with DKMS at build-time from a verified artifact), so no large downloads or unpredictable DKMS builds at first boot.
* **Simpler runtime surface:** Ray workers run directly on VM host Python (no container runtime to configure), reducing Ops surface area.
* **Air-gap friendly:** all external artifacts (driver runfile, wheels, model files) are mirrored to internal S3 with SHA256; CI injects artifacts at build time.

---

## 3 — Build-time checklist (CI / Packer) — exact steps & pins

1. Start from **Ubuntu 22.04.4 LTS (Jammy)** AMI with kernel `5.15.0-105.115` (or an AWS `linux-aws` image matching that kernel). ([Launchpad][1])
2. Install exact system packages from Jammy (pin apt package versions in your Packer script or mirror):

   * `build-essential=12.9ubuntu3`, `dkms=2.8.7-2ubuntu2.2`, `linux-headers-5.15.0-105.115` (install `linux-headers-$(uname -r)`). ([Launchpad][2])
3. Obtain the NVIDIA driver artifact **`cuda_12.4.1_550.54.15_linux.run`** into CI (recommended: internal S3). Verify SHA256 exactly before install. Download pattern: `https://developer.download.nvidia.com/compute/cuda/12.4.1/local_installers/cuda_12.4.1_550.54.15_linux.run`. Install with DKMS mode: `sh cuda_12.4.1_550.54.15_linux.run --silent --driver --dkms`. ([NVIDIA Developer][5])
4. Create Python venv: `/opt/venvs/ai` using system `python3.11` (Jammy). Verify `python3.11` package used in build. ([Discussions on Python.org][6])
5. Inside venv, pin and install exact Python packages (no `>=`):

   ```bash
   /opt/venvs/ai/bin/python -m pip install --upgrade pip setuptools wheel
   /opt/venvs/ai/bin/pip install --no-cache-dir \
     --index-url https://download.pytorch.org/whl/cu124 \
     torch==2.6.0+cu124 torchvision==0.21.0+cu124 torchaudio==2.6.0 \
     vllm==0.6.6.post1 \
     huggingface-hub==0.20.3 \
     safetensors==0.4.3
   ```

   (install exact wheel filenames if you mirror them; otherwise pin exactly as above and mirror the wheel index). ([PyTorch][7])
6. Bake models into image (see §4). Ensure `models-manifest.json` lists every file with SHA256 and size. Store `models-manifest.json` in the AMI metadata / artifact store.
7. Run smoke tests (see §6) — fail build on any mismatch.
8. Clean apt caches, produce AMI snapshot, tag with `version`, `driver=550.54.15`, `models-manifest-sha256`, `build-date`, etc.

---

## 4 — Baking models (exact repos & recommended flow)

**Model manifest (required file):** `models-manifest.json` — an array of objects with keys `{ "repo":"<repo_id>", "path":"<relative-path>", "sha256":"<hex>", "size":<bytes>, "hf_revision":"<rev-or-commit-or-tag-or-snapshot>" }`.

**Models included (exact repo_ids as baked):**

* `Alibaba-NLP/gte-modernbert-base` → baked files: `config.json`, `tokenizer.json`, `README.md`, `tokenizer_config.json`, `special_tokens_map.json`.
* `cross-encoder/ms-marco-TinyBERT-L2-v2` → baked files: `config.json`, `tokenizer.json`, `README.md`.
* `Qwen/Qwen3-4B-AWQ` → baked files: `model.safetensors`, `config.json`, `tokenizer.json`, `README.md` — **must** have `hf_revision` set to the exact Hub revision/commit (or download the artifacts into internal S3 and record SHA256).

**Two allowed bake methods (explicit):**

1. **Offline / fully deterministic (recommended):** CI uploads the exact model artifact files to internal S3, publishes `models-manifest.json` (with SHA256), and the AMI build performs:

   ```bash
   aws s3 cp s3://internal-models/qwen/Qwen3-4B-AWQ/model.safetensors /tmp/qwen/ && \
   sha256sum -c --status <(echo "<sha256>  /tmp/qwen/model.safetensors") || exit 1 && \
   mv /tmp/qwen /workspace/models/qwen/Qwen3-4B-AWQ
   ```

   This guarantees determinism and air-gap compatibility.
2. **Transient HF access (only when S3 is not available):** CI injects `HUGGINGFACE_HUB_TOKEN` (short-lived), build runs pinned `huggingface-hub==0.20.3` and a downloader script that calls `snapshot_download(repo_id=..., revision="<exact>", allow_patterns=[...])` or `hf_hub_download` for each file. Ensure `huggingface-hub==0.20.3` is recorded in `models-manifest.json`. ([deps.dev][9])

**Filesystem layout (exact):**

```
/workspace/models/
  gte-modernbert-base/{config.json,tokenizer.json,tokenizer_config.json,special_tokens_map.json,README.md}
  ms-marco-TinyBERT-L2-v2/{config.json,tokenizer.json,README.md}
  qwen/Qwen3-4B-AWQ/{model.safetensors,config.json,tokenizer.json,README.md}
```

---

## 5 — Minimal provision / build snippet (exact pins — conceptual; put in `bootstrap.sh` in repo)

```bash
#!/bin/bash
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then exec sudo bash "$0" "$@"; fi
export DEBIAN_FRONTEND=noninteractive

# 1) packages (exact jammy packages)
apt-get update -y
apt-get install -y --no-install-recommends \
  build-essential=12.9ubuntu3 dkms=2.8.7-2ubuntu2.2 \
  linux-headers-5.15.0-105.115 \
  git curl wget ca-certificates gnupg lsb-release python3.11 python3.11-venv python3.11-dev

# 2) NVIDIA driver (downloaded artifact must be present / verified)
cd /tmp
wget -q https://developer.download.nvidia.com/compute/cuda/12.4.1/local_installers/cuda_12.4.1_550.54.15_linux.run
sha256sum cuda_12.4.1_550.54.15_linux.run > cuda.sha256   # populate in CI with expected hash
sha256sum -c cuda.sha256
chmod +x cuda_12.4.1_550.54.15_linux.run
sh cuda_12.4.1_550.54.15_linux.run --silent --driver --dkms

# 3) venv & pinned wheels
python3.11 -m venv /opt/venvs/ai
/opt/venvs/ai/bin/python -m pip install --upgrade pip==24.1.2 setuptools==68.0.0 wheel==0.40.0
/opt/venvs/ai/bin/pip install --no-cache-dir \
  --index-url https://download.pytorch.org/whl/cu124 \
  torch==2.6.0+cu124 torchvision==0.21.0+cu124 torchaudio==2.6.0 \
  vllm==0.6.6.post1 \
  huggingface-hub==0.20.3 \
  safetensors==0.4.3

# 4) models: either copy from internal S3 (recommended) or run hf_sync.py (CI must ensure HUGGINGFACE_HUB_TOKEN only as env var)
mkdir -p /workspace/models
# Example S3 copy (replace with exact URIs and manifest)
# aws s3 cp s3://internal-models/gte-modernbert-base/config.json /workspace/models/gte-modernbert-base/config.json
# run SHA256 checks against models-manifest.json afterwards

# 5) smoke tests (fail build on any failure)
/opt/venvs/ai/bin/python -c "import torch; print(torch.cuda.is_available(), torch.version.cuda)"
/opt/venvs/ai/bin/python -c "import vllm; print('vllm import ok')"

# 6) clean & snapshot
apt-get -y autoremove
apt-get -y clean
rm -rf /var/lib/apt/lists/*
sync
```

---

## 6 — Smoke tests (exact commands to run in CI / Packer)

* Host driver check:

```bash
nvidia-smi --query-gpu=name,driver_version,memory.total,compute_capability --format=csv
```

* Python / PyTorch:

```bash
/opt/venvs/ai/bin/python -c "import torch; print('cuda_available', torch.cuda.is_available(), 'torch_cuda', torch.version.cuda)"
```

* vLLM import:

```bash
/opt/venvs/ai/bin/python -c "import vllm; print('vllm import ok')"
```

* Model smoke (small tokenizer load):

```bash
/opt/venvs/ai/bin/python -c "from transformers import AutoTokenizer; AutoTokenizer.from_pretrained('/workspace/models/gte-modernbert-base'); print('tokenizer ok')"
```

Fail the AMI build if any of these fail.

---

## 7 — Ray VM Autoscaler integration (exact notes)

* In `ray` cluster YAML, set `ami: GPU_CUSTOM_AMI_ID` for worker node spec. Use same AMI for Spot & On-Demand worker groups.
* Ensure startup scripts activate venv: `source /opt/venvs/ai/bin/activate && ray start --address=$RAY_HEAD_ADDR --num-gpus 1` (adjust per head config).
* Ray tasks reference models by absolute path (e.g. `/workspace/models/qwen/Qwen3-4B-AWQ/model.safetensors`). No run-time downloads.
* If using Spot, ensure fast reprovision: AMI contains everything required so a new worker is service-ready shortly after `ray start`.

---

## 8 — Troubleshooting quick hits (exact checks)

* `nvidia-smi` missing → check `/var/log/nvidia-installer.log`, verify you installed `cuda_12.4.1_550.54.15_linux.run` and that `linux-headers-5.15.0-105.115` was present when driver installed. ([NVIDIA Docs][10])
* DKMS rebuild failure after kernel update → inspect `dkms status`; rebuild with matching `gcc` and headers or re-bake AMI for new kernel. ([ubuntuupdates.org][3])
* Model SHA mismatch → compare against `models-manifest.json` and re-copy from internal S3.

---

## 9 — Security & ops (exact instructions)

* **Never** bake secrets. Provide `HUGGINGFACE_HUB_TOKEN` only as a CI ephemeral env var; scrub logs immediately.
* Mirror driver runfile and wheels to internal S3; use SHA256 pre-checks. Record SHA256 in `models-manifest.json` and AMI metadata.
* Tag AMI metadata: `version`, `driver=550.54.15`, `models-manifest-sha256=<manifest-sha256>`, `build-date=<YYYY-MM-DD>`.

---

## 10 — Exact pins summary (one block you can copy)

```
OS: Ubuntu 22.04.4 LTS (Jammy) — kernel 5.15.0-105.115
build-essential=12.9ubuntu3
dkms=2.8.7-2ubuntu2.2
linux-headers-5.15.0-105.115
NVIDIA driver runfile: cuda_12.4.1_550.54.15_linux.run (driver 550.54.15)
Python: python3.11 (package around 3.11.9-1+jammy1)
pip==24.1.2 (upgrade in venv)
torch==2.6.0+cu124
torchvision==0.21.0+cu124
torchaudio==2.6.0
vllm==0.6.6.post1
huggingface-hub==0.20.3
safetensors==0.4.3
Models: Alibaba-NLP/gte-modernbert-base, cross-encoder/ms-marco-TinyBERT-L2-v2, Qwen/Qwen3-4B-AWQ (manifested, checksummed)
```

Key authoritative references: Ubuntu Jammy package pins for `build-essential` and `dkms`, Linux headers for `5.15.0-105.115`, NVIDIA CUDA 12.4.1 runfile + Data Center driver 550.54.15, PyTorch cu124 wheel index, vLLM 0.6.6.post1, `huggingface-hub==0.20.3` and `safetensors==0.4.3`. ([Launchpad][2])

---


[1]: https://launchpad.net/ubuntu/jammy/amd64/linux-image-unsigned-5.15.0-105-generic/5.15.0-105.115?utm_source=chatgpt.com "Ubuntu - linux-image-unsigned-5.15.0-105-generic - Launchpad"
[2]: https://launchpad.net/ubuntu/jammy/amd64/build-essential/12.9ubuntu3?utm_source=chatgpt.com "12.9ubuntu3 : build-essential : amd64 : Jammy (22.04) : Ubuntu"
[3]: https://www.ubuntuupdates.org/package/core/jammy/main/updates/dkms?utm_source=chatgpt.com "Package \"dkms\" (jammy 22.04)"
[4]: https://launchpad.net/ubuntu/jammy/%2Bpackage/linux-headers-5.15.0-105-generic?utm_source=chatgpt.com "linux-headers-5.15.0-105-generic : Jammy (22.04) - Launchpad"
[5]: https://developer.nvidia.com/cuda-12-4-1-download-archive?Distribution=Ubuntu&target_arch=x86_64&target_os=Linux&target_type=runfile_local&target_version=22.04&utm_source=chatgpt.com "CUDA Toolkit 12.4 Update 1 Downloads"
[6]: https://discuss.python.org/t/install-python-3-11-9-on-ubuntu/51093?utm_source=chatgpt.com "Install python 3.11.9 on ubuntu"
[7]: https://pytorch.org/get-started/locally/?utm_source=chatgpt.com "Get Started"
[8]: https://deps.dev/pypi/vllm/0.2.6/versions?utm_source=chatgpt.com "Versions | vllm | PyPI"
[9]: https://deps.dev/pypi/huggingface-hub/0.35.0/versions?utm_source=chatgpt.com "Versions | huggingface-hub | PyPI"
[10]: https://docs.nvidia.com/datacenter/tesla/tesla-release-notes-550-54-15/index.html?utm_source=chatgpt.com "Version 550.54.15(Linux)/551.78(Windows) :: NVIDIA Data ..."
