#!/usr/bin/env python3
"""
Final local runner that enforces a standard import path.

Behavior changes:
- Prepends repo root and workdir to sys.path so both:
    import indexing_pipeline.parse_chunk
  and
    import parse_chunk
  work reliably.
- Keeps AWS sanitization, auto-venv, preconvert controls, skip-s3, and robust in-process execution.
"""
from __future__ import annotations
import argparse
import logging
import os
import runpy
import shutil
import subprocess
import sys
import venv
from pathlib import Path
from typing import Optional

# Defaults
DEFAULT_WORKDIR = os.environ.get("WORKDIR") or str(Path(__file__).resolve().parent)
DEFAULT_ROUTER = os.environ.get("ROUTER", "parse_chunk/router.py")
DEFAULT_INDEX = os.environ.get("INDEX", "ingest.py")
DEFAULT_PRECONVERT = os.environ.get("PRECONVERT", "pre_conversions/convert_all.sh")

RESET = "\x1b[0m"
COLORS = {
    "DEBUG": "\x1b[38;20m",
    "INFO": "\x1b[32;20m",
    "WARNING": "\x1b[33;20m",
    "ERROR": "\x1b[31;20m",
    "CRITICAL": "\x1b[41;30;20m",
}


class ColorFormatter(logging.Formatter):
    def format(self, record):
        levelname = record.levelname
        color = COLORS.get(levelname, "")
        record.levelname = f"{color}{levelname}{RESET}"
        return super().format(record)


def setup_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    handler = logging.StreamHandler(sys.stdout)
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    handler.setFormatter(ColorFormatter(fmt=fmt, datefmt="%Y-%m-%d %H:%M:%S"))
    if root.handlers:
        root.handlers.clear()
    root.addHandler(handler)


def resolve_workdir(preferred: str) -> Path:
    p = Path(preferred)
    if p.exists():
        return p.resolve()
    here = Path(__file__).resolve().parent
    if here.exists():
        return here
    return Path.cwd()


def ensure_import_paths(workdir: Path) -> None:
    """
    Ensure repo-root and workdir are in sys.path and PYTHONPATH.
    repo_root := parent of workdir if parent contains other project files.
    """
    # determine repo root candidate: if workdir is a subfolder named 'indexing_pipeline'
    # then repo_root is parent; otherwise repo_root==workdir.parent
    repo_root = workdir.parent if workdir.name == "indexing_pipeline" else workdir.parent
    # canonical strings
    rp = str(repo_root.resolve())
    wd = str(workdir.resolve())
    # build ordered unique list
    new_paths = []
    for p in (wd, rp):
        if p not in sys.path:
            new_paths.append(p)
    # insert at front preserving earlier path0
    for p in reversed(new_paths):
        sys.path.insert(0, p)
    # sync PYTHONPATH (prepend)
    existing = os.environ.get("PYTHONPATH", "")
    parts = [wd, rp] + ([existing] if existing else [])
    # keep unique while preserving order
    seen = set()
    final = []
    for x in parts:
        if not x:
            continue
        if x not in seen:
            final.append(x)
            seen.add(x)
    os.environ["PYTHONPATH"] = os.pathsep.join(final)
    # optional debug output
    logging.getLogger("indexing_pipeline").debug("sys.path[0:5]=%s", sys.path[:5])
    logging.getLogger("indexing_pipeline").debug("PYTHONPATH=%s", os.environ["PYTHONPATH"])


def sanitize_aws_env(logger: logging.Logger) -> None:
    for k in ("AWS_REGION", "AWS_DEFAULT_REGION"):
        v = os.environ.get(k, None)
        if v is not None and str(v).strip() == "":
            logger.debug("Removing empty env %s", k)
            os.environ.pop(k, None)
    if os.environ.get("AWS_REGION") and not os.environ.get("AWS_DEFAULT_REGION"):
        os.environ["AWS_DEFAULT_REGION"] = os.environ["AWS_REGION"]
        logger.debug("Set AWS_DEFAULT_REGION from AWS_REGION: %s", os.environ["AWS_DEFAULT_REGION"])


def create_and_install_venv(venv_path: Path, requirements: Optional[Path], logger: logging.Logger) -> int:
    if venv_path.exists() and (venv_path / "bin" / "python").exists():
        logger.info("Using existing venv: %s", venv_path)
    else:
        logger.info("Creating venv at %s", venv_path)
        try:
            venv.EnvBuilder(with_pip=True).create(str(venv_path))
        except Exception as e:
            logger.exception("Failed to create venv: %s", e)
            return 1
    pip = venv_path / "bin" / "pip"
    if not pip.exists():
        logger.error("pip not found in venv at %s", pip)
        return 2
    if requirements and requirements.exists():
        subprocess.run([str(pip), "install", "--upgrade", "pip", "setuptools", "wheel"],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        res = subprocess.run([str(pip), "install", "-r", str(requirements)],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if res.returncode != 0:
            logger.error("Failed to install requirements: %s", res.stderr.strip())
            return res.returncode
    return 0


def run_preconvert(script_path: Path, continue_on_fail: bool) -> int:
    logger = logging.getLogger("preconvert")
    if not script_path.exists():
        logger.info("No pre-conversion script at %s. Skipping.", script_path)
        return 0
    try:
        if not os.access(str(script_path), os.X_OK):
            script_path.chmod(script_path.stat().st_mode | 0o111)
    except Exception:
        logger.debug("Could not chmod %s; will try shell runner.", script_path)
    if os.access(str(script_path), os.X_OK):
        cmd = [str(script_path)]
    elif script_path.suffix == ".sh" and shutil.which("bash"):
        cmd = ["bash", str(script_path)]
    else:
        cmd = [str(script_path)]
    logger.info("Running pre-conversion: %s (cmd: %s)", script_path, cmd)
    try:
        res = subprocess.run(cmd, cwd=str(script_path.parent), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if res.stdout:
            logger.debug("preconvert stdout:\n%s", res.stdout.strip())
        if res.stderr:
            logger.debug("preconvert stderr:\n%s", res.stderr.strip())
        if res.returncode != 0:
            logger.error("pre-conversion exited with %d", res.returncode)
            if continue_on_fail:
                logger.warning("Continuing despite pre-convert failure due to flag.")
                return res.returncode
            return res.returncode
        logger.info("Pre-conversion completed successfully.")
        return 0
    except Exception as e:
        logger.exception("Failed to run pre-conversion: %s", e)
        if continue_on_fail:
            logger.warning("Continuing despite exception in pre-convert due to flag.")
            return 2
        return 2


def run_python_script(script_path: Path, workdir: Path) -> int:
    logger = logging.getLogger(script_path.stem if script_path.exists() else "script")
    if not script_path.exists():
        logger.error("Python script not found: %s", script_path)
        return 3
    logger.info("Executing %s in-process.", script_path)
    prev_cwd = Path.cwd()
    prev_path0: Optional[str] = sys.path[0] if sys.path else None
    try:
        os.chdir(str(workdir))
        # ensure workdir and repo root are in sys.path while running
        ensure_import_paths(workdir)
        workdir_str = str(workdir)
        if sys.path:
            if sys.path[0] != workdir_str:
                if workdir_str in sys.path:
                    sys.path.remove(workdir_str)
                sys.path.insert(0, workdir_str)
        else:
            sys.path.insert(0, workdir_str)
        runpy.run_path(str(script_path), run_name="__main__")
        logger.info("%s finished.", script_path)
        return 0
    except SystemExit as e:
        code = 0
        if isinstance(e.code, int):
            code = e.code
        elif e.code is None:
            code = 0
        else:
            try:
                code = int(e.code)
            except Exception:
                code = 1
        if code != 0:
            logger.error("%s exited with SystemExit(%s)", script_path, e.code)
        return code
    except Exception:
        logger.exception("Unhandled exception while running %s", script_path)
        return 4
    finally:
        try:
            os.chdir(prev_cwd)
        except Exception:
            pass
        if prev_path0 is not None and sys.path:
            sys.path[0] = prev_path0


def parse_args():
    p = argparse.ArgumentParser(description="Local runner for indexing_pipeline.")
    p.add_argument("--workdir", help="Working directory for pipeline.")
    p.add_argument("--router", help="Router script path relative to workdir.")
    p.add_argument("--index", help="Index/ingest script path relative to workdir.")
    p.add_argument("--preconvert", help="Pre-conversion script path relative to workdir.")
    p.add_argument("--skip-preconvert", action="store_true", help="Skip pre-conversion step.")
    p.add_argument("--force-preconvert", action="store_true", help="Run preconvert even if required envs missing.")
    p.add_argument("--continue-on-preconvert-fail", action="store_true", help="Proceed if pre-conversion exits non-zero.")
    p.add_argument("--auto-venv", action="store_true", help="Create venv and install requirements.txt into it before running.")
    p.add_argument("--venv-path", default=None, help="Path for auto venv (default: <workdir>/.venv).")
    p.add_argument("--skip-s3", action="store_true", help="Skip steps requiring S3 (router/index).")
    p.add_argument("--log-level", default="INFO", help="Log level (DEBUG, INFO, WARNING, ERROR).")
    return p.parse_args()


def main():
    args = parse_args()
    workdir = resolve_workdir(args.workdir or os.environ.get("WORKDIR") or DEFAULT_WORKDIR)
    router = Path(args.router or os.environ.get("ROUTER") or DEFAULT_ROUTER)
    index = Path(args.index or os.environ.get("INDEX") or DEFAULT_INDEX)
    preconvert = Path(args.preconvert or os.environ.get("PRECONVERT") or DEFAULT_PRECONVERT)

    log_level = getattr(logging, (args.log_level or os.environ.get("LOG_LEVEL", "INFO")).upper(), logging.INFO)
    setup_logging(log_level)
    logger = logging.getLogger("indexing_pipeline")

    # sanitize AWS envs before boto3 usage
    sanitize_aws_env(logger)

    # ensure import paths (repo root + workdir) so both package and plain imports work
    ensure_import_paths(Path(workdir))

    logger.info("Using workdir: %s", workdir)
    router_path = (workdir / router).resolve()
    index_path = (workdir / index).resolve()
    preconvert_path = (workdir / preconvert).resolve()

    # optional auto venv
    if args.auto_venv:
        venv_target = Path(args.venv_path) if args.venv_path else (Path(workdir) / ".venv")
        requirements = (Path(workdir) / "requirements.txt")
        rc = create_and_install_venv(venv_target, requirements if requirements.exists() else None, logger)
        if rc != 0:
            logger.error("Auto venv setup failed with code %d. Aborting.", rc)
            sys.exit(rc)
        bin_dir = str(venv_target / "bin")
        os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
        logger.info("Prepended venv bin to PATH: %s", bin_dir)

    # pre-conversion
    if args.skip_preconvert:
        logger.info("Skipping pre-conversion as requested.")
    else:
        rc = run_preconvert(preconvert_path, continue_on_fail=args.continue_on_preconvert_fail)
        if rc != 0 and not args.continue_on_preconvert_fail:
            logger.error("Pre-conversion failed with code %d. Aborting.", rc)
            sys.exit(rc)

    # S3 checks
    if args.skip_s3:
        logger.info("--skip-s3 set. Skipping S3-dependent steps (router & index).")
        logger.info("Done.")
        sys.exit(0)

    s3_bucket = os.environ.get("S3_BUCKET") or os.environ.get("BACKUP_S3_BUCKET")
    if not s3_bucket or str(s3_bucket).strip() == "":
        logger.error("S3_BUCKET not set. Set S3_BUCKET environment variable or run with --skip-s3.")
        sys.exit(1)

    # run router
    rc = run_python_script(router_path, workdir)
    if rc != 0:
        logger.error("Router failed with code %d. Aborting.", rc)
        sys.exit(rc)

    # run index/ingest
    rc = run_python_script(index_path, workdir)
    if rc != 0:
        logger.error("Index (ingest) failed with code %d. Aborting.", rc)
        sys.exit(rc)

    logger.info("Pipeline completed successfully.")
    sys.exit(0)


if __name__ == "__main__":
    main()
