import argparse
import json
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import List, Dict

try:
    from huggingface_hub import hf_hub_download
except Exception as e:
    raise ImportError("huggingface_hub is required. Install with: pip install huggingface_hub") from e

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(levelname)s: %(message)s")
logger = logging.getLogger("download_hf")

REPOS: List[Dict[str, str]] = [
    {"repo_id": "Alibaba-NLP/gte-modernbert-base", "name": "gte-modernbert-base"},
    {"repo_id": "Alibaba-NLP/gte-reranker-modernbert-base", "name": "gte-reranker-modernbert-base"},
]

COMMON_ITEMS = [
    "README.md",
    "config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
]

TMP_DIR_BASE = Path(tempfile.gettempdir()) / "hf_download"
TMP_DIR_BASE.mkdir(parents=True, exist_ok=True)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Download model artifacts from Hugging Face repos into a local folder.")
    p.add_argument("models_dir", type=Path, help="Path to the root directory where models will be stored")
    p.add_argument("--force", "-f", action="store_true", help="Force re-download even if target files already exist")
    p.add_argument("--repos", "-r", type=str, help="Optional path to a JSON file containing a list of repos")
    return p.parse_args()


def download_one(repo_id: str, remote: str, target: Path, force: bool) -> bool:
    if target.exists() and not force:
        logger.info("SKIP exists %s", target)
        return True
    tmp_dir = TMP_DIR_BASE / repo_id.replace("/", "_")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    try:
        got = hf_hub_download(
            repo_id=repo_id,
            filename=remote,
            local_dir=str(tmp_dir),
            local_dir_use_symlinks=False,
            force_download=force,
        )
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


def ensure_model(repo_id: str, name: str, model_root: Path, force: bool) -> bool:
    ok = True
    for item in COMMON_ITEMS:
        target = model_root / item
        if not download_one(repo_id, item, target, force):
            ok = False
            logger.error("Missing required %s:%s", name, item)
    onnx_dir = model_root / "onnx"
    target_fp16 = onnx_dir / "model_int8.onnx"
    if not download_one(repo_id, "onnx/model_int8.onnx", target_fp16, force):
        if not download_one(repo_id, "onnx/model.onnx", target_fp16, force):
            ok = False
            logger.error("Missing required %s:onnx/model_int8.onnx (or fallback onnx/model.onnx)", name)
    return ok


def main() -> None:
    args = parse_args()
    workspace_models = args.models_dir.expanduser().resolve()
    workspace_models.mkdir(parents=True, exist_ok=True)
    force = args.force or os.getenv("FORCE_DOWNLOAD", "0").lower() in ("1", "true", "yes")
    repos = REPOS
    if args.repos:
        repos_path = Path(args.repos).expanduser()
        try:
            with repos_path.open("r", encoding="utf-8") as fh:
                loaded = json.load(fh)
                if isinstance(loaded, list):
                    valid = True
                    for entry in loaded:
                        if not isinstance(entry, dict) or "repo_id" not in entry or "name" not in entry:
                            valid = False
                            break
                    if valid:
                        repos = loaded
                    else:
                        logger.warning("Provided repos JSON did not match expected format; using built-in REPOS.")
                else:
                    logger.warning("Provided repos JSON is not a list; using built-in REPOS.")
        except Exception as e:
            logger.warning("Failed to load repos JSON file %s: %s -- using built-in REPOS", repos_path, e)
    logger.info("Using models root: %s", workspace_models)
    logger.info("Force download: %s", bool(force))
    logger.info("Temporary download directory: %s", TMP_DIR_BASE)
    all_ok = True
    for repo in repos:
        repo_id = repo["repo_id"]
        name = repo["name"]
        model_root = workspace_models / name
        logger.info("Ensuring model %s (repo: %s) -> %s", name, repo_id, model_root)
        if not ensure_model(repo_id, name, model_root, force):
            all_ok = False
    if not all_ok:
        logger.error("Some required files failed to download under %s", workspace_models)
        sys.exit(2)
    logger.info("All required model artifacts are present under %s", workspace_models)


if __name__ == "__main__":
    main()
