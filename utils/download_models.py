# python3 -m spacy download en_core_web_sm --target /models/spacy && rm -rf /workspace/models/gte-modernbert-base/onnx/model.onnx

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

# Repo-style entries (kept lightweight models here)
REPOS: List[Dict[str, str]] = [
    {"repo_id": "Alibaba-NLP/gte-modernbert-base", "name": "gte-modernbert-base"},
    # Replaced reranker with requested cross-encoder reranker
    {"repo_id": "cross-encoder/ms-marco-TinyBERT-L2-v2", "name": "ms-marco-TinyBERT-L2-v2"},
]

# Items we usually expect for repo-style models
COMMON_ITEMS = [
    "README.md",
    "config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
]

# Explicit ONNX and vocab files the user requested for the reranker repo
REQUIRED_ONNX_FILES = [
    "onnx/model_quint8_avx2.onnx",
    "onnx/model_O1.onnx",
    "onnx/model_O2.onnx",
    "onnx/model_O3.onnx",
    "onnx/model_O4.onnx",
]
REQUIRED_REPO_EXTRA = [
    "vocab.txt",
]

# Other models (unchanged except keep Qwen addition)
MODELS: List[Dict[str, Any]] = [
    {
        "repo_id": "Systran/faster-whisper-base",
        "name": "faster-whisper-base",
        "base": "faster_whisper",
        "items": ["model.bin", "config.json", "tokenizer.json", "vocabulary.txt", "README.md"],
    },
    {
        "repo_id": "Orion-zhen/Qwen3-0.6B-AWQ",
        "name": "Qwen3-0.6B-AWQ",
        "base": "qwen",
        "items": ["config.json", "model.safetensors", "tokenizer.json", "README.md"],
    },
    {
        "repo_id": "Qwen/Qwen3-0.6B",
        "name": "Qwen3-0.6B",
        "base": "qwen",
        "items": ["config.json", "model.safetensors", "tokenizer.json", "README.md"],
    },
    {
        "repo_id": "Qwen/Qwen3-4B-AWQ",
        "name": "Qwen3-4B-AWQ",
        "base": "qwen",
        "items": ["config.json", "model.safetensors", "tokenizer.json", "README.md"],
    },
]

TMP_DIR_BASE = Path(tempfile.gettempdir()) / "hf_download"
TMP_DIR_BASE.mkdir(parents=True, exist_ok=True)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Download model artifacts from Hugging Face repos into a local folder.")
    p.add_argument(
        "--path",
        "-p",
        dest="models_dir",
        type=Path,
        default=Path(os.getenv("WORKSPACE_MODELS", "/workspace/models")),
        help="Root directory for models",
    )
    p.add_argument("--force", "-f", action="store_true", help="Force re-download")
    p.add_argument("--repos", "-r", type=str, help="JSON file with list of repos (overrides built-in REPOS)")
    p.add_argument("--models-json", "-m", type=str, help="JSON file with list of models (overrides built-in MODELS)")
    return p.parse_args()


def download_one(repo_id: str, remote: str, target: Path, force: bool, tmp_dir_base: Path = TMP_DIR_BASE) -> bool:
    """
    Download a single file from HF repo into target path.
    Returns True if the file exists locally (either pre-existing or downloaded).
    """
    if target.exists() and not force:
        logger.info("SKIP exists %s", target)
        return True

    tmp_dir = tmp_dir_base / repo_id.replace("/", "_")
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


def ensure_repo_style(repo_id: str, name: str, model_root: Path, force: bool) -> bool:
    """
    Ensure a 'repo-style' model (shared files in model root) has required common files.
    Additionally, if this is the cross-encoder reranker requested, ensure extra ONNX/vocab files.
    """
    ok = True
    # common items
    for item in COMMON_ITEMS:
        target = model_root / item
        if not download_one(repo_id, item, target, force):
            ok = False
            logger.error("Missing required %s:%s", name, item)

    # onnx fallback/common handling: try multiple possible onnx filenames
    # general check for onnx/model.onnx or onnx/model_int8.onnx as backwards compatibility
    onnx_dir = model_root / "onnx"
    fallback_target = onnx_dir / "model.onnx"
    # try to download a common onnx file if present
    _ = download_one(repo_id, "onnx/model.onnx", fallback_target, force) or download_one(repo_id, "onnx/model_int8.onnx", fallback_target, force)

    # If the repo is the requested reranker then require the explicit files the user asked for
    if repo_id.lower().strip() in ("cross-encoder/ms-marco-tinybert-l2-v2", "cross-encoder/ms-marco-tinybert-l2-v2".lower()):
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
    """
    Ensure a model-style entry that maps files listed in model['items'] into workspace_models/<base>/<name>/
    """
    repo_id = model["repo_id"]
    name = model["name"]
    base = model.get("base", "llm")
    model_root = workspace_models / base / name
    ok = True
    for item in model.get("items", []):
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

    force = args.force or os.getenv("FORCE_DOWNLOAD", "0").lower() in ("1", "true", "yes")

    repos = REPOS
    if args.repos:
        repos_path = Path(args.repos).expanduser()
        try:
            with repos_path.open("r", encoding="utf-8") as fh:
                loaded = json.load(fh)
                if isinstance(loaded, list):
                    valid = all(isinstance(entry, dict) and "repo_id" in entry and "name" in entry for entry in loaded)
                    if valid:
                        repos = loaded
                    else:
                        logger.warning("Provided repos JSON did not match expected format; using built-in REPOS.")
                else:
                    logger.warning("Provided repos JSON is not a list; using built-in REPOS.")
        except Exception as e:
            logger.warning("Failed to load repos JSON file %s: %s -- using built-in REPOS", repos_path, e)

    models = MODELS
    if args.models_json:
        models_path = Path(args.models_json).expanduser()
        try:
            with models_path.open("r", encoding="utf-8") as fh:
                loaded = json.load(fh)
                if isinstance(loaded, list):
                    models = loaded
                else:
                    logger.warning("Provided models JSON is not a list; using built-in MODELS.")
        except Exception as e:
            logger.warning("Failed to load models JSON file %s: %s -- using built-in MODELS", models_path, e)

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
        logger.info("Ensuring model-style entry %s (repo: %s)", m.get("name", "<unknown>"), m.get("repo_id", "<unknown>"))
        if not ensure_model_style(m, workspace_models, force):
            all_ok = False

    if not all_ok:
        logger.error("Some required files failed to download under %s", workspace_models)
        sys.exit(2)

    logger.info("All required model artifacts are present under %s", workspace_models)

if __name__ == "__main__":
    main()
