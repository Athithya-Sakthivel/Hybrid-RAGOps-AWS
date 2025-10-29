#!/usr/bin/env python3
"""
Resilient router for indexing_pipeline.

- Ensures import paths so both `indexing_pipeline.parse_chunk` and `parse_chunk`
  imports work when run from repo root or from the package.
- Auto-detects faster-whisper model location and sets FW_MODEL_PATH/FW_MODEL_BIN envs.
- Tries importlib first for format modules then falls back to loading the .py file
  directly from the repo/workdir using SourceFileLoader.
- Sanitizes AWS env vars before creating boto3 client to avoid invalid endpoints.
"""
from __future__ import annotations
import os
import sys
import time
import json
import uuid
import hashlib
import importlib
import importlib.machinery
import importlib.util
import mimetypes
import logging
import urllib.parse
from datetime import datetime
from typing import Optional, Tuple
from botocore.exceptions import ClientError
from pathlib import Path

# -------------------- import path / model discovery helpers -------------------
def ensure_import_paths():
    here = Path(__file__).resolve()
    # here -> .../indexing_pipeline/parse_chunk/router.py
    pkg_dir = here.parent.parent         # .../indexing_pipeline
    repo_root = pkg_dir.parent           # repository root
    candidates = [str(pkg_dir), str(repo_root), str(repo_root / "indexing_pipeline")]
    # insert pkg_dir first then repo_root
    for p in reversed(candidates):
        if p and p not in sys.path:
            sys.path.insert(0, p)

def find_faster_whisper_model(workdir: Path) -> Optional[Tuple[str,str]]:
    """
    Look for a faster-whisper model.bin in a few standard locations.
    Return (FW_MODEL_PATH, FW_MODEL_BIN) or None.
    """
    candidates = [
        Path(os.environ.get("FW_MODEL_PATH", "")),
        workdir / "models" / "faster_whisper" / "faster-whisper-base",
        workdir / "models" / "faster_whisper",
        Path("/indexing_pipeline/models/faster_whisper/faster-whisper-base"),
        Path("/opt/models/faster_whisper/faster-whisper-base"),
        workdir / "models",
    ]
    for c in candidates:
        if not c:
            continue
        c = Path(c)
        # model.bin in this folder or nested folder
        if (c / "model.bin").exists():
            return (str(c), str((c / "model.bin")))
        # sometimes model stored as folder with model.bin deeper
        for sub in c.glob("**/model.bin"):
            return (str(sub.parent), str(sub))
    return None

def load_module_from_path(module_name: str, path: Path):
    """
    Load a module from a file path and return the module object.
    """
    loader_name = f"local_formats_{module_name}"
    spec = importlib.util.spec_from_file_location(loader_name, str(path))
    if spec and spec.loader:
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    raise ImportError(f"Cannot load module {module_name} from {path}")

# ensure import paths before any local imports
ensure_import_paths()

# -------------------- logging setup -----------------------------------------
try:
    import colorama
    colorama.init()
except Exception:
    pass

RESET = "\033[0m"
COLORS = {
    logging.DEBUG: "\033[90m",
    logging.INFO: "\033[97m",
    logging.WARNING: "\033[33m",
    logging.ERROR: "\033[31m",
    logging.CRITICAL: "\033[1;41m"
}

class ColorFormatter(logging.Formatter):
    def format(self, record):
        color = COLORS.get(record.levelno, RESET)
        message = super().format(record)
        return f"{color}{message}{RESET}"

logger = logging.getLogger("router")
logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))
handler = logging.StreamHandler(sys.stdout)
fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
handler.setFormatter(ColorFormatter(fmt._fmt))
logger.handlers[:] = [handler]

def env_or_fail(var, default=None, mandatory=True):
    val = os.environ.get(var, default)
    if mandatory and val is None:
        print(f"ERROR: Required env var '{var}' not set.", file=sys.stderr)
        sys.exit(1)
    return val

# -------------------- AWS sanitization and client ----------------------------
def sanitize_aws_env():
    for k in ("AWS_REGION", "AWS_DEFAULT_REGION"):
        v = os.environ.get(k, None)
        if v is not None and str(v).strip() == "":
            logger.debug("Removing empty env %s", k)
            os.environ.pop(k, None)
    if os.environ.get("AWS_REGION") and not os.environ.get("AWS_DEFAULT_REGION"):
        os.environ["AWS_DEFAULT_REGION"] = os.environ["AWS_REGION"]
        logger.debug("Set AWS_DEFAULT_REGION from AWS_REGION: %s", os.environ["AWS_DEFAULT_REGION"])

sanitize_aws_env()

import boto3  # import after sanitization

# -------------------- config/env --------------------------------------------
S3_BUCKET = env_or_fail("S3_BUCKET")
S3_RAW_PREFIX = os.getenv("S3_RAW_PREFIX", "data/raw/").rstrip("/") + "/"
S3_CHUNKED_PREFIX = os.getenv("S3_CHUNKED_PREFIX", "data/chunked/").rstrip("/") + "/"
CHUNK_FORMAT = os.getenv("CHUNK_FORMAT", "json").lower()
FORCE_PROCESS = os.getenv("FORCE_PROCESS", "false").lower() == "true"

assert CHUNK_FORMAT in ("json", "jsonl"), f"Invalid CHUNK_FORMAT '{CHUNK_FORMAT}'"

# set FW model envs if not present
WORKDIR = Path(os.environ.get("WORKDIR") or str(Path(__file__).resolve().parent.parent))
fw = find_faster_whisper_model(WORKDIR)
if fw:
    fw_path, fw_bin = fw
    os.environ.setdefault("FW_MODEL_PATH", fw_path)
    os.environ.setdefault("FW_MODEL_BIN", fw_bin)
    logger.info("Detected faster-whisper model: %s", fw_bin)
else:
    logger.debug("No faster-whisper model found in standard locations; audio parsing may skip or error if required.")

# create s3 client with clear error handling
def make_s3_client():
    try:
        return boto3.client("s3")
    except Exception as e:
        logger.error("Failed to create S3 client: %s", e)
        logger.error("Check AWS_REGION / AWS_DEFAULT_REGION and boto3 configuration.")
        raise

s3 = make_s3_client()

# -------------------- helpers & core ---------------------------------------
def log(*args, level="INFO", **kwargs):
    msg = " ".join(str(a) for a in args)
    lvl = level.upper()
    if lvl == "WARN":
        lvl = "WARNING"
    levelno = getattr(logging, lvl, logging.INFO)
    logger.log(levelno, msg, **kwargs)

def _is_not_found_client_error(e: ClientError) -> bool:
    try:
        code = e.response.get("Error", {}).get("Code", "")
        status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code in ("404", "NoSuchKey", "NotFound") or status == 404:
            return True
    except Exception:
        pass
    return False

def retry(func, retries=3, delay=2, backoff=2):
    for attempt in range(retries):
        try:
            return func()
        except ClientError as e:
            if _is_not_found_client_error(e):
                raise
            if attempt == retries - 1:
                raise
            log(f"Retry {attempt + 1}/{retries} after error: {e}", level="WARN")
            time.sleep(delay)
            delay *= backoff
        except Exception as e:
            if attempt == retries - 1:
                raise
            log(f"Retry {attempt + 1}/{retries} after error: {e}", level="WARN")
            time.sleep(delay)
            delay *= backoff

def list_raw_files():
    paginator = s3.get_paginator("list_objects_v2")
    pages = paginator.paginate(Bucket=S3_BUCKET, Prefix=S3_RAW_PREFIX)
    for page in pages:
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/"):
                continue
            if key.lower().endswith(".manifest.json"):
                continue
            yield key

def file_sha256(s3_key):
    h = hashlib.sha256()
    obj = retry(lambda: s3.get_object(Bucket=S3_BUCKET, Key=s3_key))
    stream = obj["Body"]
    for chunk in iter(lambda: stream.read(8192), b""):
        if not chunk:
            break
        h.update(chunk)
    return h.hexdigest()

def manifest_path(s3_key, file_hash=None):
    return f"{s3_key}.manifest.json"

def is_already_processed(file_hash):
    if FORCE_PROCESS:
        return False
    base_prefix = S3_CHUNKED_PREFIX.rstrip("/") + "/"
    search_prefix = f"{base_prefix}{file_hash}_"
    try:
        paginator = s3.get_paginator("list_objects_v2")
        pages = paginator.paginate(Bucket=S3_BUCKET, Prefix=search_prefix, PaginationConfig={"MaxItems": 1})
        for page in pages:
            if page.get("Contents"):
                return True
    except ClientError as e:
        log(f"S3 list_objects_v2 error while checking {search_prefix}: {e}", level="WARN")
    for ext in ("json", "jsonl"):
        test_key = f"{base_prefix}{file_hash}_1.{ext}"
        try:
            s3.head_object(Bucket=S3_BUCKET, Key=test_key)
            return True
        except ClientError as e:
            if _is_not_found_client_error(e):
                continue
            raise
    return False

def save_manifest(s3_key, manifest):
    key = manifest_path(s3_key, manifest.get("file_hash"))
    try:
        retry(lambda: s3.put_object(
            Bucket=S3_BUCKET,
            Key=key,
            Body=json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"),
            ContentType="application/json"
        ))
        log(f"Saved manifest to s3://{S3_BUCKET}/{key}")
        return True
    except Exception as e:
        log(f"Failed to save manifest: {e}", level="ERROR")
        return False

def get_format_module(ext, workdir: Path):
    """
    Return imported module for format ext.
    Try standard import first then fallback to loading file from workdir/repo.
    """
    module_map = {
        "pdf": "pdf",
        "pptx": "pptx",
        "html": "html",
        "md": "md",
        "markdown": "md",
        "txt": "txt",
        "wav": "wav",
        "jpg": "images",
        "jpeg": "images",
        "png": "images",
        "webp": "images",
        "tiff": "images",
        "tif": "images",
        "gif": "images",
        "bmp": "images",
        "csv": "csv",
        "jsonl": "jsonl",
        "ndjson": "jsonl"
    }
    module_name = module_map.get(ext.lower())
    if not module_name:
        return None, f"No mapping for ext {ext}"
    # try imports
    tried = []
    for pkg in ("indexing_pipeline.parse_chunk.formats", "parse_chunk.formats"):
        fq = f"{pkg}.{module_name}"
        try:
            mod = importlib.import_module(fq)
            return mod, None
        except Exception as e:
            tried.append(fq)
    # fallback: try load by file path from workdir and repo root
    candidates = [
        workdir / "parse_chunk" / "formats" / f"{module_name}.py",
        workdir / "indexing_pipeline" / "parse_chunk" / "formats" / f"{module_name}.py",
        Path(__file__).resolve().parent / "formats" / f"{module_name}.py",  # same dir
        Path(__file__).resolve().parent.parent / "parse_chunk" / "formats" / f"{module_name}.py",
    ]
    for p in candidates:
        try:
            p = p.resolve()
        except Exception:
            continue
        if p.exists():
            try:
                mod = load_module_from_path(module_name, p)
                return mod, None
            except Exception as e:
                tried.append(str(p))
    return None, f"Failed to import module for format '{module_name}', tried: {', '.join(tried)}"

def detect_mime(key):
    mime, _ = mimetypes.guess_type(key)
    return mime or "application/octet-stream"

def detect_ext_from_key(s3_client, bucket, key):
    k = urllib.parse.unquote(key.split("?", 1)[0].split("#", 1)[0])
    base, ext = os.path.splitext(k)
    ext = ext.lstrip(".").lower()
    if ext in ("markdown", "mdown"):
        ext = "md"
    if ext:
        return ext
    try:
        head = s3_client.head_object(Bucket=bucket, Key=key)
        ctype = (head.get("ContentType") or "").lower()
        metadata = head.get("Metadata") or {}
        meta_fn = metadata.get("filename") or metadata.get("originalname") or ""
        if meta_fn:
            _, mext = os.path.splitext(meta_fn)
            mext = mext.lstrip(".").lower()
            if mext in ("markdown", "mdown"):
                return "md"
            if mext:
                return mext
        if "markdown" in ctype or "text/markdown" in ctype:
            return "md"
        if ctype.startswith("text/"):
            return "txt"
    except Exception:
        pass
    return ""

def main():
    log("Router pipeline started")
    run_id = os.getenv("RUN_ID") or str(uuid.uuid4())
    parser_version = os.getenv("PARSER_VERSION", "2.42.1")
    keys = list(list_raw_files())
    log(f"Found {len(keys)} files")
    for key in keys:
        if key.lower().endswith(".manifest.json"):
            log(f"Skipping manifest file {key}")
            continue
        ext = detect_ext_from_key(s3, S3_BUCKET, key)
        module_name = get_format_module(ext, WORKDIR)
        if isinstance(module_name, tuple):
            mod, err = module_name
        else:
            # compatibility if earlier returned tuple
            mod, err = module_name, None
        if mod is None:
            log(f"Skipping unsupported '{ext or 'unknown'}': {key}. Reason: {err}", level="WARN")
            continue
        try:
            if not hasattr(mod, "parse_file"):
                log(f"No parse_file() in module for {ext}, skipping {key}", level="WARN")
                continue
        except Exception as e:
            log(f"Import error in module for {ext}: {e}", level="ERROR")
            continue
        try:
            file_hash = file_sha256(key)
        except Exception as e:
            log(f"Hash error for {key}: {e}", level="ERROR")
            continue
        if is_already_processed(file_hash):
            log(f"Already processed {file_hash}, skipping")
            continue
        sd = os.getenv("SOURCE_DATE_EPOCH")
        if sd:
            try:
                ts = datetime.utcfromtimestamp(int(sd)).isoformat() + "Z"
            except Exception:
                ts = datetime.utcnow().isoformat() + "Z"
        else:
            ts = datetime.utcnow().isoformat() + "Z"
        manifest = {
            "file_hash": file_hash,
            "s3_key": key,
            "pipeline_run_id": run_id,
            "mime_type": detect_mime(key),
            "timestamp": ts,
            "parser_version": parser_version
        }
        try:
            result = mod.parse_file(key, manifest)
            if not isinstance(result, dict) or "saved_chunks" not in result:
                raise ValueError("Invalid parse_file() return. Expected dict with 'saved_chunks'.")
        except Exception as e:
            log(f"Parse error for {key}: {e}", level="ERROR")
            continue
        log(f"Parsed and stored {result['saved_chunks']} chunks for {key}")
        save_manifest(key, manifest)

if __name__ == "__main__":
    main()


