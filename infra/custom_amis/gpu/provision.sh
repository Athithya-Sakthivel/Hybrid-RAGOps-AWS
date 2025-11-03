#!/bin/bash
set -e

# prerequisites
sudo apt update
sudo apt install -y wget ca-certificates

# Python (non-interactive)
sudo apt install -y python3.11 python3.11-venv python3.11-dev python3.11-distutils python3-pip
sudo update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1
sudo update-alternatives --set python3 /usr/bin/python3.11

# NVIDIA repo keyring + driver (single download)
wget -q https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2204/x86_64/cuda-keyring_1.1-1_all.deb
sudo dpkg -i cuda-keyring_1.1-1_all.deb
sudo apt update

# ensure DKMS and matching headers so modules can be built at first boot
sudo apt install -y dkms linux-headers-$(uname -r) cuda-drivers

# attempt to load driver now (may fail if kernel differs)
sudo modprobe nvidia

# use the selected python to install vllm
python3.11 -m pip install pip==25.3 setuptools==80.9.0 wheel==0.45.1
ray==2.51.0
onnxruntime==1.23.2
transformers==4.57.1
numpy==2.2.6
httpx==0.28.1
typing==3.7.4.3
ray[serve,llm]==2.51.0


python3.11 -m pip install --no-cache-dir aiohappyeyeballs==2.6.1 aiohttp==3.13.2 aiosignal==1.4.0 annotated-doc==0.0.3 annotated-types==0.7.0 anyio==4.11.0 astor==0.8.1 attrs==25.4.0 blake3==1.0.8 cachetools==6.2.1 cbor2==5.7.1 certifi==2025.10.5 cffi==2.0.0 charset-normalizer==3.4.4 click==8.2.1 cloudpickle==3.1.1 compressed-tensors==0.11.0 cupy-cuda12x==13.6.0 depyf==0.19.0 dill==0.4.0 diskcache==5.6.3 distro==1.9.0 dnspython==2.8.0 einops==0.8.1 email-validator==2.3.0 fastapi==0.120.4 fastapi-cli==0.0.14 fastapi-cloud-cli==0.3.1 fastrlock==0.8.3 filelock==3.20.0 frozendict==2.4.6 frozenlist==1.8.0 fsspec==2025.10.0 gguf==0.17.1 h11==0.16.0 hf-xet==1.2.0 httpcore==1.0.9 httptools==0.7.1 httpx==0.28.1 huggingface-hub==0.36.0 idna==3.11 interegular==0.3.3 Jinja2==3.1.6 jiter==0.11.1 jsonschema==4.25.1 jsonschema-specifications==2025.9.1 lark==1.2.2 llguidance==0.7.30 llvmlite==0.44.0 lm-format-enforcer==0.11.3 markdown-it-py==4.0.0 MarkupSafe==3.0.3 mdurl==0.1.2 mistral_common==1.8.5 mpmath==1.3.0 msgpack==1.1.2 msgspec==0.19.0 multidict==6.7.0 networkx==3.5 ninja==1.13.0 numba==0.61.2 numpy==2.2.6 nvidia-cublas-cu12==12.8.4.1 nvidia-cuda-cupti-cu12==12.8.90 nvidia-cuda-nvrtc-cu12==12.8.93 nvidia-cuda-runtime-cu12==12.8.90 nvidia-cudnn-cu12==9.10.2.21 nvidia-cufft-cu12==11.3.3.83 nvidia-cufile-cu12==1.13.1.3 nvidia-curand-cu12==10.3.9.90 nvidia-cusolver-cu12==11.7.3.90 nvidia-cusparse-cu12==12.5.8.93 nvidia-cusparselt-cu12==0.7.1 nvidia-nccl-cu12==2.27.3 nvidia-nvjitlink-cu12==12.8.93 nvidia-nvtx-cu12==12.8.90 openai==2.6.1 openai-harmony==0.0.4 opencv-python-headless==4.12.0.88 outlines_core==0.2.11 packaging==25.0 partial-json-parser==0.2.1.1.post6 pillow==12.0.0 prometheus-fastapi-instrumentator==7.1.0 prometheus_client==0.23.1 propcache==0.4.1 protobuf==6.33.0 psutil==7.1.3 py-cpuinfo==9.0.0 pybase64==1.4.2 pycountry==24.6.1 pycparser==2.23 pydantic==2.12.3 pydantic-extra-types==2.10.6 pydantic_core==2.41.4 Pygments==2.19.2 python-dotenv==1.2.1 python-json-logger==4.0.0 python-multipart==0.0.20 PyYAML==6.0.3 pyzmq==27.1.0 ray==2.51.1 referencing==0.37.0 regex==2025.10.23 requests==2.32.5 rich==14.2.0 rich-toolkit==0.15.1 rignore==0.7.3 rpds-py==0.28.0 safetensors==0.6.2 scipy==1.16.3 sentencepiece==0.2.1 sentry-sdk==2.43.0 setproctitle==1.3.7 shellingham==1.5.4 sniffio==1.3.1 soundfile==0.13.1 soxr==1.0.0 starlette==0.49.3 sympy==1.14.0 tiktoken==0.12.0 tokenizers==0.22.1 torch==2.8.0 torchaudio==2.8.0 torchvision==0.23.0 tqdm==4.67.1 transformers==4.57.1 triton==3.4.0 typer==0.20.0 typing-inspection==0.4.2 typing==3.7.4.3 typing_extensions==4.15.0 urllib3==2.5.0 uvicorn==0.38.0 uvloop==0.22.1 vllm==0.11.0 watchfiles==1.1.1 websockets==15.0.1 xformers==0.0.32.post1 xgrammar==0.1.25 yarl==1.22.0

mkdir -p /workspace/models
python3 <<EOF
from __future__ import annotations
import argparse, json, logging, os, shutil, sys, tempfile
from pathlib import Path
from huggingface_hub import hf_hub_download

logging.basicConfig(level="INFO", format="%(levelname)s: %(message)s")
log = logging.getLogger("hf_dl")
REPOS=[{"repo_id":"Alibaba-NLP/gte-modernbert-base","name":"gte-modernbert-base"},
       {"repo_id":"cross-encoder/ms-marco-TinyBERT-L2-v2","name":"ms-marco-TinyBERT-L2-v2"}]
COMMON=["README.md","config.json","tokenizer.json","tokenizer_config.json","special_tokens_map.json"]
ONNX=["onnx/model_quint8_avx2.onnx","onnx/model_O1.onnx","onnx/model_O2.onnx","onnx/model_O3.onnx","onnx/model_O4.onnx"]
MODELS = [
    {"repo_id": "Systran/faster-whisper-base", "name": "faster-whisper-base", "base": "faster_whisper",
     "items": ["model.bin", "config.json", "tokenizer.json", "vocabulary.txt", "README.md"]},
    {"repo_id": "Qwen/Qwen3-4B-AWQ", "name": "Qwen3-4B-AWQ", "base": "qwen",
     "items": ["config.json", "model.safetensors", "tokenizer.json", "README.md"]}
]

TMP=Path(tempfile.gettempdir())/"hf_dl";TMP.mkdir(exist_ok=True)

def args():
    p=argparse.ArgumentParser();p.add_argument("-p","--path",type=Path,default=Path("/workspace/models"))
    p.add_argument("-f","--force",action="store_true");return p.parse_args()

def dl(repo,f,tgt,force):
    if tgt.exists()and not force:return True
    try:got=hf_hub_download(repo_id=repo,filename=f,local_dir=str(TMP),force_download=force)
    except Exception as e:return log.warning("fail %s:%s %s",repo,f,e)or False
    tgt.parent.mkdir(parents=True,exist_ok=True);shutil.move(got,tgt);return True

def ensure_repo(r,root,force):
    ok=all(dl(r["repo_id"],f,root/f,force)for f in COMMON)
    if "TinyBERT" in r["name"]:ok&=all(dl(r["repo_id"],f,root/f,force)for f in ONNX+[ "vocab.txt"])
    return ok

def ensure_model(m,root,force):
    return all(dl(m["repo_id"],f,root/m["base"]/m["name"]/f,force)for f in m["items"])

def main():
    a=args();root=a.path;root.mkdir(parents=True,exist_ok=True);ok=True
    for r in REPOS:ok&=ensure_repo(r,root/r["name"],a.force)
    for m in MODELS:ok&=ensure_model(m,root,a.force)
    sys.exit(0 if ok else 2)

if __name__=="__main__":main()
EOF