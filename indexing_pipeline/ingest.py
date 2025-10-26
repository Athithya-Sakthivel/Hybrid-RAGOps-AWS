#!/usr/bin/env python3
"""
Refactored ingest pipeline using Ray for horizontal scaling without an external state DB.
- Uses a Coordinator actor to claim files (in-cluster singleton).
- Uses a QdrantWriter actor to serialize and batch vector upserts (single-writer safety).
- Uses a Neo4jWriter actor to batch metadata writes.
- Workers are Ray tasks that call Ray Serve embedder and push batches to writers.

Assumptions:
- Files are accessible from all cluster nodes (shared storage or mounted volume).
- This is an in-cluster design. For durable claims you should add an external DB or checkpointing.

Environment variables: same as original script. New options:
- RAY_WORKERS: number of parallel file workers (default: 4)
- QDRANT_FLUSH_SIZE: batch size to flush (defaults to BATCH_SIZE)
- NEO4J_FLUSH_SIZE: batch size for Neo4j writes (defaults to 1000)
"""
from __future__ import annotations
import os
import sys
import json
import time
import uuid
import logging
import signal
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

import ray
from ray import serve

# Config
RAY_ADDRESS = os.getenv("RAY_ADDRESS", "auto")
RAY_NAMESPACE = os.getenv("RAY_NAMESPACE", None)
DATA_IN_LOCAL = os.getenv("DATA_IN_LOCAL", "true").lower() in ("1", "true", "yes")
LOCAL_DIR_PATH = os.getenv("LOCAL_DIR_PATH", "data/chunked/")
EMBED_DEPLOYMENT = os.getenv("EMBED_DEPLOYMENT", "embed_onxx")
INDEXING_EMBEDDER_MAX_TOKENS = int(os.getenv("INDEXING_EMBEDDER_MAX_TOKENS", "512"))
EMBED_TIMEOUT = int(os.getenv("EMBED_TIMEOUT", "60"))
SNIPPET_MAX_CHARS = int(os.getenv("SNIPPET_MAX_CHARS", "512"))
VECTOR_DIM = int(os.getenv("VECTOR_DIM", "768"))

BATCH_SIZE = int(os.getenv("BATCH_SIZE", "64"))
EMBED_SUB_BATCH = int(os.getenv("EMBED_SUB_BATCH", "32"))
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "4"))
RAY_WORKERS = int(os.getenv("RAY_WORKERS", str(MAX_WORKERS)))

QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY") or None
PREFER_GRPC = os.getenv("PREFER_GRPC", "true").lower() in ("1", "true", "yes")
QDRANT_COLLECTION = os.getenv("COLLECTION", "my_collection")
QDRANT_ON_DISK_PAYLOAD = os.getenv("QDRANT_ON_DISK_PAYLOAD", "true").lower() in ("1", "true", "yes")

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "ReplaceWithStrongPass!")

RETRY_ATTEMPTS = int(os.getenv("RETRY_ATTEMPTS", "5"))
RETRY_BASE = float(os.getenv("RETRY_BASE_SECONDS", "0.5"))
RETRY_MAX_JITTER = float(os.getenv("RETRY_MAX_JITTER", "0.3"))

QDRANT_FLUSH_SIZE = int(os.getenv("QDRANT_FLUSH_SIZE", str(BATCH_SIZE)))
NEO4J_FLUSH_SIZE = int(os.getenv("NEO4J_FLUSH_SIZE", "1000"))

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("ingest")

# External libs (import lazily inside actors/functions to avoid serialization issues)

# Utilities

def deterministic_point_id(chunk_id: str) -> str:
    try:
        return str(uuid.uuid5(uuid.NAMESPACE_OID, str(chunk_id)))
    except Exception:
        return str(uuid.uuid4())

def list_local_json_files(path: str) -> List[Path]:
    p = Path(path)
    if not p.exists() or not p.is_dir():
        raise RuntimeError(f"LOCAL_DIR_PATH does not exist: {path}")
    files = sorted([f for f in p.iterdir() if f.is_file() and f.suffix.lower() in (".json", ".jsonl")])
    return files

# Simple retry decorator
import random
import time

def jitter_sleep(attempt: int):
    wait = RETRY_BASE * (2 ** attempt) + random.uniform(0, RETRY_MAX_JITTER)
    time.sleep(wait)

def retryable(attempts: int = RETRY_ATTEMPTS):
    def deco(fn):
        def wrapper(*a, **k):
            last = None
            for i in range(attempts):
                try:
                    return fn(*a, **k)
                except Exception as e:
                    last = e
                    log.warning("retry %d/%d %s: %s", i + 1, attempts, fn.__name__, e)
                    jitter_sleep(i)
            log.error("all retries failed for %s", fn.__name__)
            raise last
        return wrapper
    return deco

# Ray Coordinator actor: single in-cluster claim owner
@ray.remote
class Coordinator:
    def __init__(self, files: List[str]):
        self.files = list(files)
        self.index = 0
        self.claimed = set()

    def get_next(self) -> Optional[str]:
        # return next unclaimed file or None
        while self.index < len(self.files):
            f = self.files[self.index]
            self.index += 1
            if f not in self.claimed:
                self.claimed.add(f)
                return f
        return None

    def mark_done(self, file_path: str):
        # remove claim
        self.claimed.discard(file_path)

# Qdrant writer actor: single writer, batches upserts
@ray.remote
class QdrantWriter:
    def __init__(self, url: str, api_key: Optional[str], prefer_grpc: bool, collection: str, dim: int):
        from qdrant_client import QdrantClient
        from qdrant_client.models import PointStruct, VectorParams, Distance
        self.PointStruct = PointStruct
        self.client = QdrantClient(url=url, api_key=api_key, prefer_grpc=prefer_grpc)
        # ensure collection
        existing = self.client.get_collections().collections
        names = [c.name for c in existing]
        if collection not in names:
            self.client.create_collection(
                collection_name=collection,
                vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
                on_disk_payload=QDRANT_ON_DISK_PAYLOAD,
            )
        self.collection = collection
        self.buf: List[PointStruct] = []
        self.flush_size = QDRANT_FLUSH_SIZE

    def add_points(self, raw_points: List[Dict[str, Any]]):
        # raw_points are dicts with keys: id, vector (list), payload (dict)
        for rp in raw_points:
            p = self.PointStruct(id=rp["id"], vector=rp["vector"], payload=rp.get("payload"))
            self.buf.append(p)
            if len(self.buf) >= self.flush_size:
                self._flush()

    def _flush(self):
        if not self.buf:
            return
        try:
            # chunk native client call
            for i in range(0, len(self.buf), self.flush_size):
                self.client.upsert(collection_name=self.collection, points=self.buf[i:i + self.flush_size])
            log.info("QdrantWriter upserted %d points", len(self.buf))
        except Exception:
            log.exception("Qdrant upsert failed")
            raise
        finally:
            self.buf = []

    def flush_sync(self):
        self._flush()

# Neo4j writer actor: batches metadata writes
@ray.remote
class Neo4jWriter:
    def __init__(self, uri: str, user: str, password: str):
        from neo4j import GraphDatabase
        self.driver = GraphDatabase.driver(uri, auth=(user, password))
        self.buf: List[Dict[str, Any]] = []
        self.flush_size = NEO4J_FLUSH_SIZE

    def add_chunks(self, chunks: List[Dict[str, Any]]):
        self.buf.extend(chunks)
        if len(self.buf) >= self.flush_size:
            self._flush()

    def _flush(self):
        if not self.buf:
            return
        cypher = """
        UNWIND $chunks AS c
        MERGE (d:Document {document_id: c.document_id})
          ON CREATE SET d.file_name = c.file_name, d.created_at = datetime()
          ON MATCH SET d.updated_at = datetime()
        WITH c, d
        MERGE (ch:Chunk {chunk_id: c.chunk_id})
          ON CREATE SET ch.text = c.text, ch.token_count = c.token_count, ch.file_type = c.file_type, ch.source_url = c.source_url, ch.timestamp = c.timestamp, ch.file_name = c.file_name, ch.qdrant_id = c.qdrant_point_id
          ON MATCH SET ch.updated_at = datetime(), ch.qdrant_id = c.qdrant_point_id
        MERGE (d)-[:HAS_CHUNK]->(ch)
        """
        try:
            for i in range(0, len(self.buf), self.flush_size):
                batch = self.buf[i:i + self.flush_size]
                with self.driver.session() as s:
                    s.execute_write(lambda tx: tx.run(cypher, chunks=batch))
            log.info("Neo4jWriter wrote %d chunks", len(self.buf))
        except Exception:
            log.exception("Neo4j bulk write failed")
            raise
        finally:
            self.buf = []

    def flush_sync(self):
        self._flush()

# Worker function (Ray task) - processes one file at a time
@ray.remote
def worker_task(file_path: str, embed_handle, q_writer, neo_writer):
    # local imports
    from qdrant_client.models import PointStruct

    log.info("worker processing %s", file_path)
    try:
        with open(file_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as e:
        log.exception("failed to load %s: %s", file_path, e)
        return False

    items = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
    normalized = []
    for raw in items:
        txt = raw.get("text", "") or ""
        normalized.append({
            "document_id": raw.get("document_id"),
            "chunk_id": raw.get("chunk_id") or str(uuid.uuid4()),
            "text": txt,
            # approximate token count by words; replace with tokenizer call if available
            "token_count": int(raw.get("token_count") or len(txt.split())),
            "file_type": raw.get("file_type") or "",
            "file_name": raw.get("file_name") or Path(file_path).name,
            "source_url": raw.get("source_url") or "",
            "timestamp": raw.get("timestamp") or "",
            "parser_version": raw.get("parser_version") or "",
        })

    if not normalized:
        log.info("no items in %s", file_path)
        return True

    # prepare embed inputs
    texts = [c["text"] for c in normalized]
    chunk_ids = [c["chunk_id"] for c in normalized]

    vectors: List[List[float]] = []
    # call embedder in minibatches
    for i in range(0, len(texts), EMBED_SUB_BATCH):
        sub = texts[i:i + EMBED_SUB_BATCH]
        payload = {"texts": sub, "max_length": INDEXING_EMBEDDER_MAX_TOKENS}
        try:
            resp_obj = embed_handle.remote(payload)
            # embed_handle.remote returns Ray ObjectRef; resolve by ray.get
            resp = ray.get(resp_obj, timeout=EMBED_TIMEOUT)
        except Exception as e:
            log.exception("embed failed for %s: %s", file_path, e)
            raise
        if not isinstance(resp, dict) or "vectors" not in resp:
            raise RuntimeError("embed returned unexpected response")
        sub_vecs = resp["vectors"]
        if len(sub_vecs) != len(sub):
            raise RuntimeError("embed returned mismatched vectors")
        vectors.extend(sub_vecs)

    # build raw points and neo payloads
    q_points = []
    neo_chunks = []
    for cid, vec, c in zip(chunk_ids, vectors, normalized):
        if len(vec) != VECTOR_DIM:
            raise RuntimeError(f"vector dim mismatch {len(vec)} != {VECTOR_DIM}")
        pid = deterministic_point_id(cid)
        payload = {
            "document_id": c.get("document_id"),
            "chunk_id": cid,
            "snippet": (c.get("text") or "")[:SNIPPET_MAX_CHARS],
            "token_count": c.get("token_count", 0),
            "file_name": c.get("file_name"),
            "file_type": c.get("file_type"),
            "source_url": c.get("source_url"),
            "timestamp": c.get("timestamp"),
            "parser_version": c.get("parser_version"),
        }
        q_points.append({"id": pid, "vector": [float(x) for x in vec], "payload": payload})
        neo_chunks.append({
            "chunk_id": cid,
            "document_id": c.get("document_id"),
            "text": c.get("text"),
            "token_count": c.get("token_count", 0),
            "file_name": c.get("file_name"),
            "file_type": c.get("file_type"),
            "source_url": c.get("source_url"),
            "timestamp": c.get("timestamp"),
            "qdrant_point_id": pid,
        })

    # send to writers
    # do best-effort: push points and metadata; writers will batch
    try:
        ray.get(q_writer.add_points.remote(q_points))
    except Exception:
        log.exception("failed to enqueue points for %s", file_path)
        raise
    try:
        ray.get(neo_writer.add_chunks.remote(neo_chunks))
    except Exception:
        log.exception("failed to enqueue neo4j chunks for %s", file_path)
        raise

    log.info("processed %s: %d chunks", file_path, len(normalized))
    return True

# Entrypoint
def main():
    if not DATA_IN_LOCAL:
        log.error("This script currently supports local file ingestion (shared storage). Set DATA_IN_LOCAL=true or modify to read from S3/GCS.")
        sys.exit(1)

    # init ray
    ray.init(address=RAY_ADDRESS, namespace=RAY_NAMESPACE, ignore_reinit_error=True)

    # get embed handle
    # use existing helper logic to resolve Serve handle
    def _get_embed_handle_local(name: str, timeout: float = 60.0, poll: float = 1.0):
        start = time.time()
        last_exc = None
        while time.time() - start < timeout:
            try:
                if hasattr(serve, "get_deployment_handle"):
                    handle = serve.get_deployment_handle(name, app_name="default", _check_exists=False)
                    # quick health call
                    resp_obj = handle.remote({"texts": ["health-check"], "max_length": INDEXING_EMBEDDER_MAX_TOKENS})
                    ray.get(resp_obj, timeout=EMBED_TIMEOUT)
                    return handle
            except Exception as e:
                last_exc = e
                log.debug("waiting for embed handle %s: %s", name, e)
                time.sleep(poll)
        raise RuntimeError(f"Timed out waiting for Serve deployment {name}: {last_exc}")

    handle = _get_embed_handle_local(EMBED_DEPLOYMENT)

    # create writers (named actors)
    q_writer = QdrantWriter.options(name="qdrant_writer", lifetime="detached").remote(QDRANT_URL, QDRANT_API_KEY, PREFER_GRPC, QDRANT_COLLECTION, VECTOR_DIM)
    neo_writer = Neo4jWriter.options(name="neo4j_writer", lifetime="detached").remote(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)

    # prepare coordinator with file list
    try:
        files = list_local_json_files(LOCAL_DIR_PATH)
    except Exception as e:
        log.exception("listing local files failed: %s", e)
        sys.exit(1)

    coord = Coordinator.options(name="coordinator", lifetime="detached").remote([str(p) for p in files])

    # spawn pool of workers that loop until no work
    worker_refs = []
    for _ in range(RAY_WORKERS):
        # each worker is a long-running task that polls coordinator
        @ray.remote
        def loop_worker(coord_ref, embed_h, qw, nw):
            while True:
                file_path = ray.get(coord_ref.get_next.remote())
                if not file_path:
                    break
                try:
                    success = ray.get(worker_task.remote(file_path, embed_h, qw, nw))
                    if success:
                        ray.get(coord_ref.mark_done.remote(file_path))
                except Exception:
                    log.exception("worker failed for %s", file_path)
            return True
        worker_refs.append(loop_worker.remote(coord, handle, q_writer, neo_writer))

    # wait for workers
    ray.get(worker_refs)

    # flush writers
    ray.get(q_writer.flush_sync.remote())
    ray.get(neo_writer.flush_sync.remote())

    log.info("ingest complete")

if __name__ == '__main__':
    main()
