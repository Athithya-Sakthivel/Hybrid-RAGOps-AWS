from __future__ import annotations
import argparse
import logging
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import ray

DEFAULT_WORKDIR = "/indexing_pipeline"
ROUTER = "parse_chunk/router.py"
INDEX = "ingest.py"
PRECONVERT = "pre_conversions/convert_all.sh"

# logging setup
class ColoredFormatter(logging.Formatter):
    RESET = "\x1b[0m"
    COLORS = {
        "DEBUG": "\x1b[38;20m",
        "INFO": "\x1b[32;20m",
        "WARNING": "\x1b[33;20m",
        "ERROR": "\x1b[31;20m",
        "CRITICAL": "\x1b[41;30;20m"
    }
    def __init__(self, fmt=None, datefmt=None, use_colors: Optional[bool] = None):
        super().__init__(fmt=fmt, datefmt=datefmt)
        if use_colors is None:
            use_colors = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
        self.use_colors = use_colors
    def format(self, record):
        levelname = record.levelname
        if self.use_colors and levelname in self.COLORS:
            color = self.COLORS[levelname]
            record.levelname = f"{color}{levelname}{self.RESET}"
        return super().format(record)

handler = logging.StreamHandler(sys.stdout)
base_fmt = "%(asctime)s.%(msecs)03d %(levelname)s %(message)s"
formatter = ColoredFormatter(fmt=base_fmt)
handler.setFormatter(formatter)
root = logging.getLogger()
for h in list(root.handlers):
    root.removeHandler(h)
root.addHandler(handler)
root.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
logger = logging.getLogger("indexing_pipeline")

def log_and_exit(msg: str, code: int = 1, extra: Optional[Dict] = None):
    logger.error(msg)
    if extra:
        for k, v in extra.items():
            logger.error("%s: %s", k, v)
    for h in logger.handlers:
        try:
            h.flush()
        except Exception:
            pass
    # ensure Ray shuts down cleanly before exit
    try:
        ray.shutdown()
    except Exception:
        pass
    sys.exit(code)

def _stream_process(proc: subprocess.Popen, name: str):
    out_lines: List[str] = []
    err_lines: List[str] = []
    def _read(stream, collect, log_fn):
        try:
            for line in iter(stream.readline, ""):
                if not line:
                    break
                collect.append(line)
                log_fn(line.rstrip())
        except Exception:
            pass
    t_out = threading.Thread(target=_read, args=(proc.stdout, out_lines, lambda l: logger.info("[%s] %s", name, l)), daemon=True)
    t_err = threading.Thread(target=_read, args=(proc.stderr, err_lines, lambda l: logger.error("[%s:ERR] %s", name, l)), daemon=True)
    t_out.start()
    t_err.start()
    proc.wait()
    t_out.join(timeout=1.0)
    t_err.join(timeout=1.0)
    return proc.returncode, "".join(out_lines), "".join(err_lines)

@ray.remote
def run_cmd_remote(cmd: List[str], cwd: str) -> Dict:
    """
    Runs a command and streams stdout/stderr to logger.
    Returns dict with rc, stdout, stderr.
    """
    import subprocess, threading, logging
    log = logging.getLogger("indexing_pipeline.ray")
    try:
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    except Exception as e:
        return {"rc": 1, "stdout": "", "stderr": f"Failed to start {cmd}: {e}"}
    out_lines = []
    err_lines = []
    def reader(stream, collect, level):
        try:
            for line in iter(stream.readline, ""):
                if not line:
                    break
                collect.append(line)
                if level == "info":
                    log.info("[remote] %s", line.rstrip())
                else:
                    log.error("[remote] %s", line.rstrip())
        except Exception:
            pass
    t_out = threading.Thread(target=reader, args=(proc.stdout, out_lines, "info"), daemon=True)
    t_err = threading.Thread(target=reader, args=(proc.stderr, err_lines, "error"), daemon=True)
    t_out.start()
    t_err.start()
    rc = proc.wait()
    t_out.join(timeout=1.0)
    t_err.join(timeout=1.0)
    return {"rc": rc, "stdout": "".join(out_lines), "stderr": "".join(err_lines)}

def run_task(cmd: List[str], cwd: str, task_name: str) -> int:
    logger.info("Launching task %s: %s (cwd=%s)", task_name, " ".join(cmd), cwd)
    fut = run_cmd_remote.remote(cmd, cwd)
    res = ray.get(fut)
    rc = int(res.get("rc", 1))
    if rc != 0:
        logger.error("Task %s failed rc=%s", task_name, rc)
        logger.debug("stdout:\n%s", res.get("stdout", ""))
        logger.debug("stderr:\n%s", res.get("stderr", ""))
    else:
        logger.info("Task %s finished rc=%s", task_name, rc)
    return rc

def run_pipeline(workdir: str):
    workdir = str(Path(workdir).resolve())
    # start Ray in local_mode for simple orchestration
    logger.info("Starting Ray in local_mode for orchestration.")
    ray.init(local_mode=True, ignore_reinit_error=True)

    # pre-conversion
    pre_path = Path(workdir) / PRECONVERT
    if not pre_path.exists():
        log_and_exit(f"Pre-conversion script missing: {pre_path}", 1)
    # ensure script executable
    try:
        pre_path.chmod(pre_path.stat().st_mode | 0o111)
    except Exception:
        pass
    rc = run_task([str(pre_path)], workdir, "preconvert")
    if rc != 0:
        log_and_exit("Pre-conversion failed", rc)

    # router
    router_path = Path(workdir) / ROUTER
    if not router_path.exists():
        log_and_exit(f"Router missing: {router_path}", 1)
    rc = run_task([sys.executable, str(router_path)], workdir, "router")
    if rc != 0:
        log_and_exit("Router failed", rc)

    # index
    index_path = Path(workdir) / INDEX
    if not index_path.exists():
        log_and_exit(f"Index missing: {index_path}", 1)
    rc = run_task([sys.executable, str(index_path)], workdir, "index")
    if rc != 0:
        log_and_exit("Index failed", rc)

    logger.info("Pipeline completed successfully.")
    ray.shutdown()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workdir", default=os.getenv("WORKDIR", DEFAULT_WORKDIR))
    args = parser.parse_args()
    try:
        run_pipeline(args.workdir)
    except SystemExit as e:
        logger.error("Exiting with SystemExit: %s", getattr(e, "code", None))
        raise
    except Exception:
        logger.exception("Unhandled exception in main")
        raise

if __name__ == "__main__":
    def _handler(sig, frame):
        logger.info("Signal %s received, shutting down.", sig)
        try:
            ray.shutdown()
        except Exception:
            pass
        sys.exit(1)
    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)
    main()
