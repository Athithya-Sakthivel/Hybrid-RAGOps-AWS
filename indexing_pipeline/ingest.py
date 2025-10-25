#!/usr/bin/env python3
# ingest.py
# Production-ready, non-breaking ingestion for hybrid vector+graph RAG.
# Targets: ray==2.50.0, qdrant-client==1.15.1, neo4j==5.19.0

from __future__ import annotations
import os, sys, json, time, uuid, logging, random, signal, sqlite3
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

import ray
from ray import serve

# Config (env driven)
RAY_ADDRESS = os.getenv("RAY_ADDRESS", "auto")
RAY_NAMESPACE = os.getenv("RAY_NAMESPACE", None)
DATA_IN_LOCAL = os.getenv("DATA_IN_LOCAL", "true").lower() in ("1", "true", "yes")
LOCAL_DIR_PATH = os.getenv("LOCAL_DIR_PATH", "data/chunked/")
EMBED_DEPLOYMENT = os.getenv("EMBED_DEPLOYMENT", "embed_onxx")
INDEXING_EMBEDDER_MAX_TOKENS = int(os.getenv("INDEXING_EMBEDDER_MAX_TOKENS", "512"))
EMBED_TIMEOUT = int(os.getenv("EMBED_TIMEOUT", "60"))
SNIPPET_MAX_CHARS = int(os.getenv("SNIPPET_MAX_CHARS", "512"))
VECTOR_DIM = int(os.getenv("VECTOR_DIM", "768"))

BATCH_SIZE = int(os.getenv("BATCH_SIZE", "64"))            # Qdrant upsert batch
EMBED_SUB_BATCH = int(os.getenv("EMBED_SUB_BATCH", "32"))  # embed calls
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "4"))           # parallel upsert workers

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

SQLITE_STATE_DB = os.getenv("SQLITE_STATE_DB", "ingest_state.db")

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("ingest")

# external libs
try:
    from qdrant_client import QdrantClient
    from qdrant_client.models import PointStruct, VectorParams, Distance
except Exception as e:
    raise RuntimeError("qdrant-client import failed: " + str(e))

try:
    from neo4j import GraphDatabase, basic_auth
except Exception as e:
    raise RuntimeError("neo4j driver import failed: " + str(e))

# ---------- Utilities ----------
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

# ---------- Durable state (SQLite) ----------
def init_state_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS files (
        file_path TEXT PRIMARY KEY,
        processed INTEGER DEFAULT 0,
        updated_at TEXT DEFAULT (datetime('now'))
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS chunks (
        chunk_id TEXT PRIMARY KEY,
        document_id TEXT,
        file_path TEXT,
        token_count INTEGER,
        text_snippet TEXT,
        qdrant_point_id TEXT,
        qdrant_status TEXT DEFAULT 'pending',
        neo4j_status TEXT DEFAULT 'pending',
        last_error TEXT,
        updated_at TEXT DEFAULT (datetime('now'))
    )""")
    conn.commit()
    return conn

# ---------- Qdrant helpers ----------
@retryable()
def ensure_qdrant_collection(client: QdrantClient, collection: str, dim: int):
    existing = client.get_collections().collections
    names = [c.name for c in existing]
    if collection not in names:
        client.create_collection(
            collection_name=collection,
            vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
            on_disk_payload=QDRANT_ON_DISK_PAYLOAD
        )
        log.info("Created qdrant collection %s dim=%d on_disk_payload=%s", collection, dim, QDRANT_ON_DISK_PAYLOAD)
    else:
        log.debug("Qdrant collection %s already exists", collection)

    for field in ("document_id", "chunk_id", "file_name", "source_url"):
        try:
            client.create_payload_index(collection_name=collection, field_name=field, field_schema="keyword")
            log.debug("Ensured payload index for %s", field)
        except Exception as e:
            log.debug("create_payload_index skipped/failed for %s: %s", field, e)

@retryable()
def qdrant_upsert_batch(client: QdrantClient, collection: str, points: List[PointStruct]):
    if not points:
        return
    client.upsert(collection_name=collection, points=points)
    log.info("Qdrant upserted %d points", len(points))

# ---------- Neo4j helpers ----------
@retryable()
def ensure_neo4j_constraints(driver):
    try:
        with driver.session() as s:
            s.execute_write(lambda tx: tx.run("CREATE CONSTRAINT IF NOT EXISTS FOR (d:Document) REQUIRE d.document_id IS UNIQUE;"))
            s.execute_write(lambda tx: tx.run("CREATE CONSTRAINT IF NOT EXISTS FOR (c:Chunk) REQUIRE c.chunk_id IS UNIQUE;"))
            log.debug("Ensured neo4j uniqueness constraints")
    except Exception as e:
        log.debug("Neo4j constraints creation skipped/failed: %s", e)

@retryable()
def neo4j_bulk_write(driver, chunks: List[Dict[str, Any]]):
    if not chunks:
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
    for i in range(0, len(chunks), 1000):
        batch = chunks[i:i+1000]
        with driver.session() as s:
            s.execute_write(lambda tx: tx.run(cypher, chunks=batch))
        log.info("Neo4j wrote %d chunks", len(batch))

# ---------- Ray Serve helpers (original logic) ----------
def _resolve_handle_response(resp_obj, embed_timeout: int = EMBED_TIMEOUT):
    if isinstance(resp_obj, (dict, list)):
        return resp_obj
    try:
        import ray as _ray
        ObjectRefType = getattr(_ray, "ObjectRef", None) or getattr(_ray, "_raylet.ObjectRef", None)
    except Exception:
        ObjectRefType = None
    try:
        if ObjectRefType is not None and isinstance(resp_obj, ObjectRefType):
            return ray.get(resp_obj, timeout=embed_timeout)
    except Exception:
        pass
    try:
        if hasattr(resp_obj, "result") and callable(getattr(resp_obj, "result")):
            return resp_obj.result()
    except Exception:
        pass
    try:
        return ray.get(resp_obj, timeout=embed_timeout)
    except Exception as e:
        raise RuntimeError(f"Unable to resolve Serve handle response type: {type(resp_obj)} -> {e}")

def _ensure_ray_connected():
    if not ray.is_initialized():
        ray.init(address=RAY_ADDRESS, namespace=RAY_NAMESPACE, ignore_reinit_error=True)

def _get_embed_handle(name: str, timeout: float = 60.0, poll: float = 1.0, app_name: Optional[str] = "default"):
    start = time.time()
    last_exc = None
    while time.time() - start < timeout:
        try:
            _ensure_ray_connected()
            if hasattr(serve, "get_deployment_handle"):
                try:
                    handle = serve.get_deployment_handle(name, app_name=app_name, _check_exists=False)
                    resp_obj = handle.remote({"texts": ["health-check"], "max_length": INDEXING_EMBEDDER_MAX_TOKENS})
                    _resolve_handle_response(resp_obj, embed_timeout=EMBED_TIMEOUT)
                    return handle
                except Exception as e:
                    last_exc = e
            if hasattr(serve, "get_deployment"):
                try:
                    dep = serve.get_deployment(name)
                    handle = dep.get_handle(sync=False)
                    resp_obj = handle.remote({"texts": ["health-check"], "max_length": INDEXING_EMBEDDER_MAX_TOKENS})
                    _resolve_handle_response(resp_obj, embed_timeout=EMBED_TIMEOUT)
                    return handle
                except Exception as e:
                    last_exc = e
            if hasattr(serve, "get_handle"):
                try:
                    handle = serve.get_handle(name, sync=False)
                    resp_obj = handle.remote({"texts": ["health-check"], "max_length": INDEXING_EMBEDDER_MAX_TOKENS})
                    _resolve_handle_response(resp_obj, embed_timeout=EMBED_TIMEOUT)
                    return handle
                except Exception as e:
                    last_exc = e
            raise RuntimeError("serve handle APIs not available in this ray client")
        except Exception as e:
            last_exc = e
            log.debug("waiting for embed handle %s: %s", name, e)
            time.sleep(poll)
    # no HTTP fallback added (keeps behavior consistent), raise
    raise RuntimeError(f"Timed out waiting for Serve deployment {name}: {last_exc}")

# ---------- Ingestor ----------
class Ingestor:
    def __init__(self, state_conn: sqlite3.Connection, qdrant: QdrantClient, neo4j_driver, embed_handle):
        self.state = state_conn
        self.qdrant = qdrant
        self.neo4j = neo4j_driver
        self.handle = embed_handle
        self._shutdown = False
        signal.signal(signal.SIGINT, self._on_sig)
        signal.signal(signal.SIGTERM, self._on_sig)

    def _on_sig(self, sig, frame):
        log.warning("received shutdown signal %s", sig)
        self._shutdown = True

    def persist_file_chunks(self, file_path: str, chunks: List[Dict[str, Any]]):
        cur = self.state.cursor()
        cur.execute("INSERT OR REPLACE INTO files(file_path, processed, updated_at) VALUES (?, ?, datetime('now'))",
                    (file_path, 0))
        for c in chunks:
            cur.execute("""
            INSERT OR IGNORE INTO chunks(chunk_id, document_id, file_path, token_count, text_snippet, qdrant_status, neo4j_status, updated_at)
            VALUES (?, ?, ?, ?, ?, 'pending', 'pending', datetime('now'))
            """, (c["chunk_id"], c.get("document_id"), file_path, c.get("token_count", 0), (c.get("text") or "")[:SNIPPET_MAX_CHARS]))
        self.state.commit()

    def _get_pending_chunks(self, file_path: str) -> List[Dict[str, Any]]:
        cur = self.state.cursor()
        rows = cur.execute("SELECT chunk_id, document_id, text_snippet FROM chunks WHERE file_path=? AND qdrant_status!='done'",
                           (file_path,)).fetchall()
        return [{"chunk_id": r[0], "document_id": r[1], "text": r[2]} for r in rows]

    def _mark_qdrant_done(self, mappings: List[Tuple[str, str]]):
        cur = self.state.cursor()
        for chunk_id, pid in mappings:
            cur.execute("UPDATE chunks SET qdrant_point_id=?, qdrant_status='done', updated_at=datetime('now') WHERE chunk_id=?",
                        (pid, chunk_id))
        self.state.commit()

    def _mark_qdrant_failed(self, chunk_ids: List[str], err: str):
        cur = self.state.cursor()
        for cid in chunk_ids:
            cur.execute("UPDATE chunks SET qdrant_status='failed', last_error=?, updated_at=datetime('now') WHERE chunk_id=?",
                        (err, cid))
        self.state.commit()

    def _collect_ready_for_neo4j(self, file_path: str) -> List[Dict[str, Any]]:
        cur = self.state.cursor()
        rows = cur.execute("""
        SELECT chunk_id, document_id, token_count, text_snippet, qdrant_point_id
        FROM chunks
        WHERE file_path=? AND qdrant_status='done' AND neo4j_status!='done'
        """, (file_path,)).fetchall()
        return [{"chunk_id": r[0], "document_id": r[1], "token_count": r[2], "text": r[3], "qdrant_point_id": r[4]} for r in rows]

    def _mark_neo4j_done(self, chunk_ids: List[str]):
        cur = self.state.cursor()
        for cid in chunk_ids:
            cur.execute("UPDATE chunks SET neo4j_status='done', updated_at=datetime('now') WHERE chunk_id=?", (cid,))
        self.state.commit()

    def ingest_file(self, file_path: str):
        if self._shutdown:
            log.info("shutdown in progress, skipping file %s", file_path)
            return
        log.info("processing %s", file_path)
        try:
            with open(file_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as e:
            log.exception("failed to load %s: %s", file_path, e)
            return

        items = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
        normalized = []
        for raw in items:
            normalized.append({
                "document_id": raw.get("document_id"),
                "chunk_id": raw.get("chunk_id") or str(uuid.uuid4()),
                "text": raw.get("text", "") or "",
                "token_count": int(raw.get("token_count") or 0),
                "file_type": raw.get("file_type") or "",
                "file_name": raw.get("file_name") or Path(file_path).name,
                "source_url": raw.get("source_url") or "",
                "timestamp": raw.get("timestamp") or "",
                "parser_version": raw.get("parser_version") or ""
            })
        if not normalized:
            log.info("no items in %s", file_path)
            return

        self.persist_file_chunks(file_path, normalized)
        pending = self._get_pending_chunks(file_path)
        if not pending:
            log.info("no pending chunks for %s", file_path)
            return

        # embed pending in sub-batches
        texts = [p["text"] for p in pending]
        chunk_by_index = [p["chunk_id"] for p in pending]

        vectors: List[List[float]] = []
        for i in range(0, len(texts), EMBED_SUB_BATCH):
            sub_texts = texts[i:i+EMBED_SUB_BATCH]
            payload = {"texts": sub_texts, "max_length": INDEXING_EMBEDDER_MAX_TOKENS}
            resp_obj = self.handle.remote(payload)
            resp = _resolve_handle_response(resp_obj, embed_timeout=EMBED_TIMEOUT)
            if not isinstance(resp, dict) or "vectors" not in resp:
                raise RuntimeError("embed returned unexpected response")
            sub_vecs = resp["vectors"]
            if len(sub_vecs) != len(sub_texts):
                raise RuntimeError("embed returned mismatched vectors")
            vectors.extend(sub_vecs)
            log.info("embedded sub-batch %d -> %d vectors", i, len(sub_vecs))

        # Build PointStructs and neo4j metas
        pts_meta: List[Tuple[PointStruct, Dict[str, Any]]] = []
        for chunk_id, vec, p in zip(chunk_by_index, vectors, pending):
            if len(vec) != VECTOR_DIM:
                raise RuntimeError(f"vector dim mismatch {len(vec)} != {VECTOR_DIM}")
            pid = deterministic_point_id(chunk_id)
            payload = {
                "document_id": p.get("document_id"),
                "chunk_id": chunk_id,
                "snippet": (p.get("text") or "")[:SNIPPET_MAX_CHARS],
                "token_count": p.get("token_count", 0),
                "file_name": p.get("file_name") or Path(file_path).name,
                "file_type": p.get("file_type") or "",
                "source_url": p.get("source_url") or "",
                "timestamp": p.get("timestamp") or "",
                "parser_version": p.get("parser_version") or ""
            }
            point = PointStruct(id=pid, vector=[float(x) for x in vec], payload=payload)
            meta = {
                "chunk_id": chunk_id,
                "document_id": p.get("document_id"),
                "text": p.get("text") or "",
                "token_count": p.get("token_count", 0),
                "file_type": p.get("file_type") or "",
                "source_url": p.get("source_url") or "",
                "timestamp": p.get("timestamp") or "",
                "file_name": p.get("file_name") or Path(file_path).name,
                "qdrant_point_id": pid
            }
            pts_meta.append((point, meta))

        # Parallel batched upserts
        batches = [pts_meta[i:i+BATCH_SIZE] for i in range(0, len(pts_meta), BATCH_SIZE)]
        mappings: List[Tuple[str, str]] = []
        failed_chunk_ids: List[str] = []

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            fut_map = {}
            for idx, batch in enumerate(batches):
                pts = [p for p, _ in batch]
                batch_id = f"{Path(file_path).name}-b{idx}"
                fut = ex.submit(self._safe_upsert, pts, batch_id)
                fut_map[fut] = batch
            for fut in as_completed(fut_map):
                batch = fut_map[fut]
                try:
                    fut.result()  # raises if failed after retries
                    for _, meta in batch:
                        mappings.append((meta["chunk_id"], meta["qdrant_point_id"]))
                except Exception as e:
                    log.exception("upsert batch failed permanently: %s", e)
                    for _, meta in batch:
                        failed_chunk_ids.append(meta["chunk_id"])

        if mappings:
            self._mark_qdrant_done(mappings)
        if failed_chunk_ids:
            self._mark_qdrant_failed(failed_chunk_ids, "upsert_failed")

        # Neo4j write for qdrant_done chunks
        ready = self._collect_ready_for_neo4j(file_path)
        if not ready:
            log.info("no chunks ready for neo4j for %s", file_path)
            return
        neo_payload = []
        for r in ready:
            neo_payload.append({
                "chunk_id": r["chunk_id"],
                "document_id": r["document_id"],
                "text": r["text"],
                "token_count": r["token_count"],
                "file_name": Path(file_path).name,
                "file_type": "",
                "source_url": "",
                "timestamp": "",
                "qdrant_point_id": r.get("qdrant_point_id")
            })

        ensure_neo4j_constraints(self.neo4j)
        try:
            neo4j_bulk_write(self.neo4j, neo_payload)
            self._mark_neo4j_done([r["chunk_id"] for r in ready])
        except Exception as e:
            log.exception("neo4j bulk write failed: %s", e)

    def _safe_upsert(self, pts: List[PointStruct], batch_id: str):
        last = None
        for attempt in range(RETRY_ATTEMPTS):
            try:
                for i in range(0, len(pts), BATCH_SIZE):
                    qdrant_upsert_batch(self.qdrant, QDRANT_COLLECTION, pts[i:i+BATCH_SIZE])
                return
            except Exception as e:
                last = e
                log.warning("upsert attempt %d/%d failed for %s: %s", attempt+1, RETRY_ATTEMPTS, batch_id, e)
                jitter_sleep(attempt)
        raise last

# ---------- Entrypoint ----------
def main():
    if not DATA_IN_LOCAL:
        log.error("This script currently supports local file ingestion only. Set DATA_IN_LOCAL=true.")
        sys.exit(1)

    state = init_state_db(SQLITE_STATE_DB)

    _ensure_ray_connected()
    handle = _get_embed_handle(EMBED_DEPLOYMENT)

    qdrant = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY, prefer_grpc=PREFER_GRPC)
    ensure_qdrant_collection(qdrant, QDRANT_COLLECTION, VECTOR_DIM)

    neo4j_driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

    ing = Ingestor(state, qdrant, neo4j_driver, handle)

    try:
        files = list_local_json_files(LOCAL_DIR_PATH)
    except Exception as e:
        log.exception("listing local files failed: %s", e)
        sys.exit(1)

    if not files:
        log.warning("No files under %s", LOCAL_DIR_PATH)
        sys.exit(0)

    for f in files:
        if ing._shutdown:
            break
        try:
            ing.ingest_file(str(f))
        except Exception as e:
            log.exception("ingest failed for %s: %s", f, e)

    try:
        qdrant.close()
    except Exception:
        log.debug("qdrant.close failed", exc_info=True)
    try:
        neo4j_driver.close()
    except Exception:
        log.debug("neo4j.close failed", exc_info=True)
    state.close()
    log.info("ingest complete")

if __name__ == "__main__":
    main()
