#!/usr/bin/env python3
from __future__ import annotations
import argparse
import json
import logging
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List
try:
    from huggingface_hub import hf_hub_download
except Exception as e:
    raise ImportError("huggingface_hub is required. Install with: pip install huggingface_hub")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("download_hf_models")
TMP_DIR_BASE = Path(tempfile.gettempdir()) / "hf_download"
TMP_DIR_BASE.mkdir(parents=True, exist_ok=True)
# Deterministic repo -> files mapping (hardcoded per user's instruction)
REPO_FILES: Dict[str, List[str]] = {

     "unsloth/Qwen3-0.6B-GGUF": [
        "Qwen3-0.6B-Q4_K_M.gguf",
    ],
    "unsloth/Qwen3-1.7B-GGUF": [
        "Qwen3-1.7B-Q4_K_M.gguf",
    ],
    "unsloth/Qwen3-4B-GGUF": [
        "Qwen3-4B-Q4_K_M.gguf",
    ],

    "Alibaba-NLP/gte-modernbert-base": [
        "onnx/model_int8.onnx",
        "tokenizer.json",
        "tokenizer_config.json",
        "special_tokens_map.json",
        "config.json",
        "README.md",
    ],
    "cross-encoder/ms-marco-TinyBERT-L2-v2": [
        "special_tokens_map.json",
        "tokenizer.json",
        "tokenizer_config.json",
        "vocab.txt",
        "onnx/model_quint8_avx2.onnx",
        "onnx/model_qint8_arm64.onnx",
        "onnx/model_O1.onnx",
        "onnx/model_O2.onnx",
        "onnx/model_O3.onnx",
        "onnx/model_O4.onnx",
    ],
}



# helpers
def parse_args():
    p = argparse.ArgumentParser(description="Deterministic download of specified HF repo files into local models directory.")
    p.add_argument("--out", "-o", dest="out_dir", type=Path, default=Path(os.getenv("WORKSPACE_MODELS", "/workspace/models")), help="Root directory to write models")
    p.add_argument("--force", "-f", action="store_true", help="Force re-download even if target exists")
    p.add_argument("--timeout", "-t", type=float, default=30.0, help="Per-file download timeout (seconds)")
    p.add_argument("--retries", "-r", type=int, default=3, help="Attempts per file")
    return p.parse_args()
def download_with_retry(repo_id: str, filename: str, tmp_dir: Path, force: bool, retries: int, timeout: float) -> Path | None:
    attempt = 0
    while attempt < retries:
        attempt += 1
        try:
            start = time.time()
            got = hf_hub_download(repo_id=repo_id, filename=filename, local_dir=str(tmp_dir), local_dir_use_symlinks=False, force_download=force)
            elapsed = time.time() - start
            logger.info("hf_hub_download OK %s:%s (%.1fs)", repo_id, filename, elapsed)
            return Path(got)
        except Exception as e:
            logger.warning("hf_hub_download attempt %d/%d failed for %s:%s -> %s", attempt, retries, repo_id, filename, e)
            if attempt < retries:
                time.sleep(1.0 * attempt)
            else:
                return None
def place_file(src: Path, dest: Path, force: bool):
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        if not force:
            logger.info("SKIP existing %s", dest)
            return True
        try:
            dest.unlink()
        except Exception:
            pass
    try:
        shutil.move(str(src), str(dest))
    except Exception:
        try:
            shutil.copyfile(str(src), str(dest))
            src.unlink(missing_ok=True)
        except Exception as e:
            logger.error("Failed to move/copy %s -> %s : %s", src, dest, e)
            return False
    try:
        os.chmod(str(dest), 0o444)
    except Exception:
        pass
    logger.info("Placed %s -> %s", src.name, dest)
    return True
def ensure_repo(repo_id: str, files: List[str], out_root: Path, force: bool, retries: int, timeout: float) -> bool:
    safe_name = repo_id.replace("/", "_")
    tmp_dir = TMP_DIR_BASE / safe_name
    tmp_dir.mkdir(parents=True, exist_ok=True)
    all_ok = True
    target_base = out_root / repo_id.split("/")[-1]
    for f in files:
        target = target_base / Path(f)
        if target.exists() and not force:
            logger.info("Exists (skip) %s", target)
            continue
        # Ensure parent exists in tmp for hf_hub. Use same relative path when possible.
        got = download_with_retry(repo_id, f, tmp_dir, force, retries, timeout)
        if not got:
            logger.error("Failed to download %s:%s after %d attempts", repo_id, f, retries)
            all_ok = False
            continue
        success = place_file(got, target, force)
        if not success:
            logger.error("Failed to place downloaded file %s -> %s", got, target)
            all_ok = False
    return all_ok
def main():
    args = parse_args()
    out_root = args.out_dir.expanduser().resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    force = bool(args.force)
    retries = max(1, int(args.retries))
    timeout = float(args.timeout)
    logger.info("Models root: %s", out_root)
    logger.info("Force: %s retries: %d timeout: %.1fs", force, retries, timeout)
    overall_ok = True
    for repo_id, files in REPO_FILES.items():
        logger.info("Ensuring repo %s -> files %d", repo_id, len(files))
        ok = ensure_repo(repo_id, files, out_root, force, retries, timeout)
        if not ok:
            overall_ok = False
    if not overall_ok:
        logger.error("Some files failed to download. Inspect logs and re-run with --force if needed.")
        sys.exit(2)
    logger.info("All requested artifacts are present under %s", out_root)
    print(json.dumps({"ok": True, "models_root": str(out_root)}, indent=2))
if __name__ == "__main__":
    main()
