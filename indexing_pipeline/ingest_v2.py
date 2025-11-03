# MODE controls payload placement:
#   "hybrid"   -> qdrant stores minimal payload (chunk_id document_id); neo4j stores full text & metadata
#   "vector_only" -> qdrant stores full payload (text + metadata) so neo4j may be optional
from __future__ import annotations
import os
import time
import json
import uuid
import hashlib
import logging
import subprocess
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("ingest")

import ray
from ray import serve

# Config (preserve existing envs; add MODE)
RAY_ADDRESS = os.getenv("RAY_ADDRESS", "auto")
RAY_NAMESPACE = os.getenv("RAY_NAMESPACE", "ragops")
SERVE_APP_NAME = os.getenv("SERVE_APP_NAME", "default")

DATA_IN_LOCAL = os.getenv("DATA_IN_LOCAL", "false").lower() in ("1", "true", "yes")
LOCAL_DIR_PATH = os.getenv("LOCAL_DIR_PATH", "./data")
S3_BUCKET = os.getenv("S3_BUCKET", "").strip()
S3_RAW_PREFIX = os.getenv("S3_RAW_PREFIX", "data/raw/").rstrip("/") + "/"
S3_CHUNKED_PREFIX = os.getenv("S3_CHUNKED_PREFIX", "data/chunked/").rstrip("/") + "/"

EMBED_DEPLOYMENT = os.getenv("EMBED_DEPLOYMENT", "embed_onxx")
INDEXING_EMBEDDER_MAX_TOKENS = int(os.getenv("INDEXING_EMBEDDER_MAX_TOKENS", "512"))

QDRANT_URL = os.getenv("QDRANT_URL", "http://127.0.0.1:6333")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY", None)
QDRANT_COLLECTION = os.getenv("COLLECTION", "my_collection")
QDRANT_ON_DISK_PAYLOAD = os.getenv("QDRANT_ON_DISK_PAYLOAD", "true").lower() in ("1", "true", "yes")

# Optional Qdrant HNSW tuning (collection-level)
QDRANT_HNSW_M = int(os.getenv("QDRANT_HNSW_M", "16"))
QDRANT_HNSW_EF_CONSTRUCTION = int(os.getenv("QDRANT_HNSW_EF_CONSTRUCTION", "200"))
QDRANT_HNSW_EF_SEARCH = int(os.getenv("QDRANT_HNSW_EF_SEARCH", "50"))
QDRANT_HNSW_FULL_SCAN_THRESHOLD = int(os.getenv("QDRANT_HNSW_FULL_SCAN_THRESHOLD", "10000"))

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "")

BATCH_SIZE = int(os.getenv("BATCH_SIZE", "64"))
EMBED_BATCH = int(os.getenv("EMBED_BATCH", "32"))
EMBED_TIMEOUT = int(os.getenv("EMBED_TIMEOUT", "60"))

FORCE_REHASH = os.getenv("FORCE_REHASH", "0") in ("1", "true", "True")
VECTOR_DIM = int(os.getenv("VECTOR_DIM", "768"))

# lease TTL (seconds)
LEASE_TTL_SECONDS = int(os.getenv("LEASE_TTL_SECONDS", "300"))

MODE = os.getenv("MODE", "hybrid").lower()

# helpers
def deterministic_point_id(chunk_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_OID, str(chunk_id)))

def sha256_bytes_iter(stream, chunk_size=1 << 20) -> str:
    import hashlib as _h
    h = _h.sha256()
    while True:
        b = stream.read(chunk_size)
        if not b:
            break
        h.update(b)
    return h.hexdigest()

def compute_local_file_hash(path: str) -> str:
    with open(path, "rb") as fh:
        return sha256_bytes_iter(fh)

def _s3_client_for_bucket(bucket: str):
    import boto3
    session = boto3.session.Session()
    env_region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
    if env_region:
        return session.client("s3", region_name=env_region)
    client = session.client("s3")
    try:
        resp = client.get_bucket_location(Bucket=bucket)
        loc = resp.get("LocationConstraint") or "us-east-1"
        if loc == "EU":
            loc = "eu-west-1"
        return session.client("s3", region_name=loc)
    except Exception:
        fallback = session.region_name or "us-east-1"
        return session.client("s3", region_name=fallback)

def read_manifest_s3(bucket: str, key: str, client=None) -> Optional[Dict[str, Any]]:
    client = client or _s3_client_for_bucket(bucket)
    try:
        obj = client.get_object(Bucket=bucket, Key=key + ".manifest.json")
        return json.loads(obj["Body"].read().decode("utf-8"))
    except Exception:
        return None

def write_manifest_s3_atomic(bucket: str, key: str, manifest: Dict[str, Any], client=None):
    client = client or _s3_client_for_bucket(bucket)
    final_key = key + ".manifest.json"
    tmp_key = final_key + f".tmp.{int(time.time())}-{uuid.uuid4().hex[:8]}"
    client.put_object(Bucket=bucket, Key=tmp_key, Body=json.dumps(manifest, indent=2).encode("utf-8"), ContentType="application/json")
    client.copy_object(CopySource={"Bucket": bucket, "Key": tmp_key}, Bucket=bucket, Key=final_key)
    try:
        client.delete_object(Bucket=bucket, Key=tmp_key)
    except Exception:
        pass

def read_manifest_local(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path + ".manifest.json", "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None

def write_manifest_local_atomic(path: str, manifest: Dict[str, Any]):
    final = path + ".manifest.json"
    tmp = final + f".tmp.{int(time.time())}-{uuid.uuid4().hex[:8]}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    os.replace(tmp, final)

def list_chunked_files_s3_for_file_hash(bucket: str, file_hash: str, client=None) -> List[str]:
    client = client or _s3_client_for_bucket(bucket)
    prefix = S3_CHUNKED_PREFIX
    keys = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []) or []:
            k = obj["Key"]
            if k.startswith(prefix + file_hash) and (k.lower().endswith(".json") or k.lower().endswith(".jsonl")):
                keys.append(k)
                continue
            base = k.split("/")[-1]
            if base.startswith(file_hash) and (base.lower().endswith(".json") or base.lower().endswith(".jsonl")):
                keys.append(k)
    return keys

def read_json_objects_from_text(text: str) -> List[Dict[str, Any]]:
    text = (text or "").strip()
    if not text:
        return []
    out = []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [p for p in parsed if isinstance(p, dict)]
        except Exception:
            pass
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
            if isinstance(parsed, dict):
                out.append(parsed)
        except Exception:
            continue
    if not out:
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                out.append(parsed)
        except Exception:
            pass
    return out

# Actors
@ray.remote(num_cpus=0)
class QdrantWriter:
    def __init__(self, url: str, api_key: Optional[str], collection: str, vector_dim: int, on_disk_payload: bool = True):
        self.collection = collection
        self.vector_dim = vector_dim
        self.on_disk_payload = on_disk_payload
        self.client = None
        try:
            from qdrant_client import QdrantClient
            prefer_grpc = not (url.startswith("http://") or url.startswith("https://"))
            self.client = QdrantClient(url=url, api_key=api_key, prefer_grpc=prefer_grpc)
            try:
                cols = [c.name for c in self.client.get_collections().collections]
                if collection not in cols:
                    try:
                        from qdrant_client.http.models import VectorParams, Distance, HnswConfig
                        hnsw_cfg = HnswConfig(
                            m=int(os.getenv("QDRANT_HNSW_M", str(QDRANT_HNSW_M))),
                            ef_construct=int(os.getenv("QDRANT_HNSW_EF_CONSTRUCTION", str(QDRANT_HNSW_EF_CONSTRUCTION))),
                            ef_search=int(os.getenv("QDRANT_HNSW_EF_SEARCH", str(QDRANT_HNSW_EF_SEARCH))),
                            full_scan_threshold=int(os.getenv("QDRANT_HNSW_FULL_SCAN_THRESHOLD", str(QDRANT_HNSW_FULL_SCAN_THRESHOLD))),
                        )
                        self.client.create_collection(
                            collection_name=collection,
                            vectors_config=VectorParams(size=vector_dim, distance=Distance.COSINE),
                            hnsw_config=hnsw_cfg,
                            on_disk_payload=on_disk_payload,
                        )
                    except Exception:
                        try:
                            from qdrant_client.http.models import VectorParams, Distance
                            hnsw_dict = {
                                "m": int(os.getenv("QDRANT_HNSW_M", str(QDRANT_HNSW_M))),
                                "ef_construct": int(os.getenv("QDRANT_HNSW_EF_CONSTRUCTION", str(QDRANT_HNSW_EF_CONSTRUCTION))),
                                "ef_search": int(os.getenv("QDRANT_HNSW_EF_SEARCH", str(QDRANT_HNSW_EF_SEARCH))),
                                "full_scan_threshold": int(os.getenv("QDRANT_HNSW_FULL_SCAN_THRESHOLD", str(QDRANT_HNSW_FULL_SCAN_THRESHOLD))),
                            }
                            self.client.create_collection(
                                collection_name=collection,
                                vectors_config=VectorParams(size=vector_dim, distance=Distance.COSINE),
                                hnsw_config=hnsw_dict,
                                on_disk_payload=on_disk_payload,
                            )
                        except Exception as e:
                            try:
                                from qdrant_client.http.models import VectorParams, Distance
                                self.client.create_collection(collection_name=collection, vectors_config=VectorParams(size=vector_dim, distance=Distance.COSINE), on_disk_payload=on_disk_payload)
                            except Exception as e2:
                                log.debug("QdrantWriter: collection create fallback failed: %s / %s", e, e2)
                                raise
                    log.info("QdrantWriter: created collection %s", collection)
            except Exception as e:
                log.debug("QdrantWriter: collection ensure skipped/failed: %s", e)
        except Exception as e:
            log.warning("QdrantWriter init failed: %s", e)
            self.client = None

    def check_points_exist(self, ids: List[str]) -> Dict[str, bool]:
        out = {}
        if not self.client:
            for pid in ids:
                out[pid] = False
            return out
        for pid in ids:
            try:
                p = self.client.get_point(collection_name=self.collection, id=pid)
                out[pid] = p is not None
            except Exception:
                out[pid] = False
        return out

    def upsert_points(self, points: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not self.client:
            return {"ok": False, "error": "qdrant_client_unavailable"}
        try:
            from qdrant_client.http.models import PointStruct, VectorParams, Distance
        except Exception:
            return {"ok": False, "error": "qdrant_models_missing"}
        structs = []
        for p in points:
            try:
                vec = [float(x) for x in p["vector"]]
            except Exception as e:
                return {"ok": False, "error": f"invalid_vector:{e}"}
            structs.append(PointStruct(id=p["id"], vector=vec, payload=p.get("payload", {})))
        attempts = 0
        while attempts < 2:
            attempts += 1
            try:
                for i in range(0, len(structs), 256):
                    self.client.upsert(collection_name=self.collection, points=structs[i:i + 256])
                return {"ok": True, "inserted": len(structs)}
            except Exception as e:
                msg = str(e)
                log.warning("QdrantWriter upsert attempt %d failed: %s", attempts, msg)
                if ("doesn't exist" in msg) or ("Not found: Collection" in msg) or ("404" in msg) or ("NotFound" in msg):
                    try:
                        try:
                            from qdrant_client.http.models import VectorParams, Distance, HnswConfig
                            hnsw_cfg = HnswConfig(
                                m=int(os.getenv("QDRANT_HNSW_M", str(QDRANT_HNSW_M))),
                                ef_construct=int(os.getenv("QDRANT_HNSW_EF_CONSTRUCTION", str(QDRANT_HNSW_EF_CONSTRUCTION))),
                                ef_search=int(os.getenv("QDRANT_HNSW_EF_SEARCH", str(QDRANT_HNSW_EF_SEARCH))),
                                full_scan_threshold=int(os.getenv("QDRANT_HNSW_FULL_SCAN_THRESHOLD", str(QDRANT_HNSW_FULL_SCAN_THRESHOLD))),
                            )
                            self.client.create_collection(collection_name=self.collection, vectors_config=VectorParams(size=self.vector_dim, distance=Distance.COSINE), hnsw_config=hnsw_cfg, on_disk_payload=self.on_disk_payload)
                        except Exception:
                            try:
                                from qdrant_client.http.models import VectorParams, Distance
                                hnsw_dict = {
                                    "m": int(os.getenv("QDRANT_HNSW_M", str(QDRANT_HNSW_M))),
                                    "ef_construct": int(os.getenv("QDRANT_HNSW_EF_CONSTRUCTION", str(QDRANT_HNSW_EF_CONSTRUCTION))),
                                    "ef_search": int(os.getenv("QDRANT_HNSW_EF_SEARCH", str(QDRANT_HNSW_EF_SEARCH))),
                                    "full_scan_threshold": int(os.getenv("QDRANT_HNSW_FULL_SCAN_THRESHOLD", str(QDRANT_HNSW_FULL_SCAN_THRESHOLD))),
                                }
                                self.client.create_collection(collection_name=self.collection, vectors_config=VectorParams(size=self.vector_dim, distance=Distance.COSINE), hnsw_config=hnsw_dict, on_disk_payload=self.on_disk_payload)
                            except Exception as e2:
                                self.client.create_collection(collection_name=self.collection, vectors_config=VectorParams(size=self.vector_dim, distance=Distance.COSINE), on_disk_payload=self.on_disk_payload)
                        log.info("QdrantWriter: created collection on-the-fly: %s", self.collection)
                        continue
                    except Exception as e2:
                        log.exception("QdrantWriter: create_collection retry failed: %s", e2)
                        return {"ok": False, "error": f"create_collection_failed:{e2}"}
                return {"ok": False, "error": msg}
        return {"ok": False, "error": "upsert_retries_exhausted"}

@ray.remote(num_cpus=0)
class Neo4jWriter:
    def __init__(self, uri: str, user: str, password: str):
        self.driver = None
        self._log = logging.getLogger("Neo4jWriter")
        try:
            from neo4j import GraphDatabase
            self.driver = GraphDatabase.driver(uri, auth=(user, password))
            try:
                with self.driver.session() as s:
                    s.execute_write(lambda tx: tx.run("CREATE CONSTRAINT IF NOT EXISTS FOR (d:Document) REQUIRE d.document_id IS UNIQUE;"))
                    s.execute_write(lambda tx: tx.run("CREATE CONSTRAINT IF NOT EXISTS FOR (c:Chunk) REQUIRE c.chunk_id IS UNIQUE;"))
            except Exception as e:
                self._log.debug("neo4j constraint ensure skipped: %s", e)
        except Exception as e:
            self._log.warning("Neo4jWriter init failed: %s", e)
            self.driver = None

    def bulk_write_chunks(self, chunks: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not chunks:
            return {"ok": True, "written": 0}
        if not self.driver:
            return {"ok": False, "error": "neo4j_driver_unavailable"}
        cypher = """
        UNWIND $chunks AS c
        MERGE (d:Document {document_id: c.document_id})
          ON CREATE SET d.file_name = c.file_name, d.created_at = datetime()
          ON MATCH SET d.updated_at = datetime()
        MERGE (ch:Chunk {chunk_id: c.chunk_id})
          ON CREATE SET ch.text = c.text,
                        ch.token_count = c.token_count,
                        ch.file_type = c.file_type,
                        ch.source_url = c.source_url,
                        ch.timestamp = c.timestamp,
                        ch.file_name = c.file_name,
                        ch.page_number = c.page_number,
                        ch.row_range = c.row_range,
                        ch.token_range = c.token_range,
                        ch.audio_range = c.audio_range,
                        ch.headings = c.headings,
                        ch.headings_path = c.headings_path,
                        ch.content_hash = c.content_hash,
                        ch.qdrant_id = c.qdrant_point_id
          ON MATCH SET ch.updated_at = datetime(), ch.qdrant_id = c.qdrant_point_id, ch.text = c.text
        MERGE (d)-[:HAS_CHUNK]->(ch)
        """
        written = 0
        from neo4j.exceptions import ServiceUnavailable, SessionExpired, TransientError
        max_attempts = int(os.getenv("NEO4J_WRITE_MAX_ATTEMPTS", "3"))
        base_backoff = float(os.getenv("NEO4J_WRITE_BASE_BACKOFF", "0.8"))
        for i in range(0, len(chunks), 1000):
            batch = chunks[i:i + 1000]
            attempt = 0
            while attempt < max_attempts:
                attempt += 1
                try:
                    with self.driver.session() as s:
                        s.execute_write(lambda tx: tx.run(cypher, chunks=batch))
                    written += len(batch)
                    break
                except (ServiceUnavailable, SessionExpired, TransientError, TimeoutError) as e:
                    self._log.warning("neo4j transient error attempt %d/%d: %s", attempt, max_attempts, e)
                    time.sleep(base_backoff * (2 ** (attempt - 1)))
                    continue
                except Exception as e:
                    self._log.exception("neo4j write failed: %s", e)
                    return {"ok": False, "error": str(e)}
            else:
                return {"ok": False, "error": "neo4j_write_retries_exhausted"}
        return {"ok": True, "written": written}

    def get_chunks_info(self, chunk_ids: List[str]) -> Dict[str, Dict[str, Optional[str]]]:
        out = {cid: {"exists": False, "qdrant_id": None} for cid in chunk_ids}
        if not self.driver:
            return out
        try:
            with self.driver.session() as s:
                res = s.run(
                    "MATCH (c:Chunk) WHERE c.chunk_id IN $ids "
                    "RETURN c.chunk_id AS id, c.qdrant_id AS qdrant_id",
                    ids=chunk_ids,
                )
                for row in res:
                    cid = row["id"]
                    out[cid] = {"exists": True, "qdrant_id": row.get("qdrant_id")}
        except Exception as e:
            self._log.warning("neo4j get_chunks_info failed: %s", e)
        return out

    def try_acquire_file_lock(self, file_hash: str, owner: str, ttl_seconds: int) -> Dict[str, Any]:
        if not self.driver:
            return {"ok": False, "error": "neo4j_driver_unavailable"}
        try:
            cypher = """
            MERGE (f:FileLock {file_hash:$file_hash})
            ON CREATE SET f.owner = $owner, f.expires_at = datetime() + duration({seconds:$ttl})
            ON MATCH
              SET f.owner = CASE WHEN f.expires_at < datetime() THEN $owner ELSE f.owner END,
                  f.expires_at = CASE WHEN f.expires_at < datetime() THEN datetime() + duration({seconds:$ttl}) ELSE f.expires_at END
            RETURN f.owner AS owner, f.expires_at AS expires_at
            """
            with self.driver.session() as s:
                res = s.run(cypher, file_hash=file_hash, owner=owner, ttl=ttl_seconds)
                row = res.single()
                if row is None:
                    return {"ok": False, "error": "no_row_returned"}
                current_owner = row["owner"]
                return {"ok": True, "acquired": (current_owner == owner)}
        except Exception as e:
            self._log.warning("try_acquire_file_lock failed: %s", e)
            return {"ok": False, "error": str(e)}

    def release_file_lock(self, file_hash: str, owner: str) -> Dict[str, Any]:
        if not self.driver:
            return {"ok": False, "error": "neo4j_driver_unavailable"}
        try:
            cypher = """
            MATCH (f:FileLock {file_hash:$file_hash})
            WHERE f.owner = $owner
            DELETE f
            RETURN true AS removed
            """
            with self.driver.session() as s:
                res = s.run(cypher, file_hash=file_hash, owner=owner)
                row = res.single()
                return {"ok": True, "released": bool(row)}
        except Exception as e:
            self._log.warning("release_file_lock failed: %s", e)
            return {"ok": False, "error": str(e)}

# Ray helpers
def _ensure_ray_connected():
    if not ray.is_initialized():
        ray.init(address=(RAY_ADDRESS if RAY_ADDRESS and RAY_ADDRESS != "auto" else None), namespace=RAY_NAMESPACE, ignore_reinit_error=True)

def _resolve_ray_response(obj, timeout: float):
    try:
        return ray.get(obj, timeout=timeout)
    except Exception:
        return obj

def get_strict_handle(name: str, timeout: float = 30.0, poll: float = 0.5, app_name: Optional[str] = None):
    _ensure_ray_connected()
    start = time.time()
    last_exc = None
    app_name = app_name or SERVE_APP_NAME
    while time.time() - start < timeout:
        try:
            handle = None
            if hasattr(serve, "get_deployment_handle"):
                try:
                    handle = serve.get_deployment_handle(name, app_name=app_name, _check_exists=False)
                except TypeError:
                    handle = serve.get_deployment_handle(name, _check_exists=False)
            if handle is None and hasattr(serve, "get_handle"):
                try:
                    handle = serve.get_handle(name, sync=False)
                except Exception:
                    handle = None
            if handle is None and hasattr(serve, "get_deployment"):
                try:
                    dep = serve.get_deployment(name)
                    handle = dep.get_handle(sync=False)
                except Exception:
                    handle = None
            if handle is None:
                raise RuntimeError("no serve handle API available")
            try:
                ref = handle.remote({"texts": ["__health_check__"], "max_length": INDEXING_EMBEDDER_MAX_TOKENS})
                _ = _resolve_ray_response(ref, timeout=10.0)
                return handle
            except Exception as e:
                last_exc = e
        except Exception as e:
            last_exc = e
        time.sleep(poll)
    raise RuntimeError(f"timed out resolving serve handle {name}: {last_exc}")

def call_serve(handle, payload, timeout: float = EMBED_TIMEOUT):
    try:
        if hasattr(handle, "remote"):
            resp = handle.remote(payload)
        else:
            resp = handle(payload)
    except Exception as e:
        raise RuntimeError(f"serve invocation failed: {e}") from e

    if isinstance(resp, (dict, list, str, int, float, bool)) or resp is None:
        return resp

    try:
        return ray.get(resp, timeout=timeout)
    except Exception:
        pass

    if hasattr(resp, "object_refs"):
        obj_refs = getattr(resp, "object_refs")
        try:
            if isinstance(obj_refs, list):
                if len(obj_refs) == 1:
                    return ray.get(obj_refs[0], timeout=timeout)
                return ray.get(obj_refs, timeout=timeout)
        except Exception:
            pass

    if hasattr(resp, "result") and callable(getattr(resp, "result")):
        try:
            return resp.result(timeout=timeout)
        except Exception:
            try:
                return resp.result()
            except Exception:
                pass
    if hasattr(resp, "get") and callable(getattr(resp, "get")):
        try:
            return resp.get(timeout=timeout)
        except Exception:
            try:
                return resp.get()
            except Exception:
                pass

    try:
        return ray.get(resp, timeout=timeout)
    except Exception as e:
        raise RuntimeError(f"call_serve failed resolving response: {e}") from e

# actor ensure helper
def ensure_actor(actor_cls, name: str, required_methods: List[str], *args):
    try:
        return actor_cls.options(name=name, lifetime="detached").remote(*args)
    except Exception:
        try:
            existing = ray.get_actor(name, namespace=RAY_NAMESPACE)
        except Exception:
            return actor_cls.options(name=name, lifetime="detached").remote(*args)
        try:
            missing = False
            for m in required_methods:
                if not hasattr(existing, m):
                    missing = True
                    break
            if missing:
                log.info("Existing actor %s missing methods; killing and recreating", name)
                try:
                    ray.kill(existing)
                except Exception:
                    log.warning("Failed to kill actor %s; attempting recreate anyway", name)
                return actor_cls.options(name=name, lifetime="detached").remote(*args)
            return existing
        except Exception:
            try:
                ray.kill(existing)
            except Exception:
                pass
            return actor_cls.options(name=name, lifetime="detached").remote(*args)

# worker
@ray.remote
def ingest_file_worker(raw_key: str, mode: str, qdrant_actor, neo4j_actor, cfg: Dict[str, Any]) -> int:
    import boto3
    s3_bucket = cfg.get("s3_bucket")
    local_dir = cfg.get("local_dir")
    embed_batch = cfg.get("embed_batch")
    max_tokens = cfg.get("max_tokens")
    batch_size = cfg.get("batch_size")
    force_rehash = cfg.get("force_rehash")
    embed_timeout = cfg.get("embed_timeout")
    boto_client = _s3_client_for_bucket(s3_bucket) if s3_bucket else None

    owner = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
    acquired_lease = False

    try:
        embed_handle = get_strict_handle(EMBED_DEPLOYMENT, timeout=10.0)
    except Exception:
        embed_handle = None
        log.warning("worker: embed handle unresolved")

    manifest = {}
    try:
        if mode == "s3" and boto_client:
            manifest = read_manifest_s3(s3_bucket, raw_key, client=boto_client) or {}
        else:
            manifest = read_manifest_local(raw_key) or {}
    except Exception:
        manifest = {}

    if force_rehash:
        manifest["indexed_chunks"] = 0

    file_hash = manifest.get("file_hash", "")
    if not file_hash or force_rehash:
        try:
            if mode == "s3" and boto_client:
                obj = boto_client.get_object(Bucket=s3_bucket, Key=raw_key)
                file_hash = sha256_bytes_iter(obj["Body"])
            else:
                file_hash = compute_local_file_hash(raw_key)
            manifest["file_hash"] = file_hash
        except Exception as e:
            log.exception("hash compute failed for %s: %s", raw_key, e)
            return 0

    if neo4j_actor:
        try:
            lease_res = ray.get(neo4j_actor.try_acquire_file_lock.remote(file_hash, owner, LEASE_TTL_SECONDS))
            if isinstance(lease_res, dict) and lease_res.get("ok"):
                if not lease_res.get("acquired"):
                    log.info("Lease for %s held by another worker; skipping", raw_key)
                    return 0
                acquired_lease = True
            else:
                log.warning("Lease service reported error; proceeding without lease for %s: %s", raw_key, lease_res)
        except Exception as e:
            log.warning("Lease RPC failed; proceeding without lease for %s: %s", raw_key, e)

    try:
        chunks_meta: List[Dict[str, Any]] = []
        try:
            if mode == "s3" and boto_client:
                chunk_keys = list_chunked_files_s3_for_file_hash(s3_bucket, file_hash, client=boto_client)
                for ck in chunk_keys:
                    try:
                        obj = boto_client.get_object(Bucket=s3_bucket, Key=ck)
                        body = obj["Body"].read().decode("utf-8")
                    except Exception:
                        continue
                    parts = read_json_objects_from_text(body)
                    for p in parts:
                        p["_chunk_source_key"] = ck
                        chunks_meta.append(p)
            else:
                chunk_dir = Path(local_dir) / "chunked"
                if chunk_dir.exists():
                    for fn in chunk_dir.iterdir():
                        if not fn.is_file():
                            continue
                        if not fn.name.startswith(file_hash):
                            continue
                        if fn.suffix.lower() not in (".json", ".jsonl"):
                            continue
                        try:
                            t = fn.read_text(encoding="utf-8")
                        except Exception:
                            continue
                        parts = read_json_objects_from_text(t)
                        for p in parts:
                            p["_chunk_source_key"] = fn.name
                            chunks_meta.append(p)
        except Exception as e:
            log.exception("failed reading chunk files for %s: %s", raw_key, e)
            return 0

        if not chunks_meta:
            return 0

        parsed_chunks = []
        for p in chunks_meta:
            text = p.get("text", "") or ""
            doc_id = p.get("document_id") or p.get("_chunk_source_key") or file_hash
            chunk_id = p.get("chunk_id") or hashlib.sha256((doc_id + text).encode("utf-8")).hexdigest()
            token_count = int(p.get("token_count") or 0)
            parsed_chunks.append({
                "document_id": doc_id,
                "chunk_id": chunk_id,
                "text": text,
                "token_count": token_count,
                "file_name": p.get("file_name", ""),
                "file_type": p.get("file_type", ""),
                "source_url": p.get("source_url", ""),
                "timestamp": p.get("timestamp", ""),
                "page_number": p.get("page_number"),
                "row_range": p.get("row_range"),
                "token_range": p.get("token_range"),
                "audio_range": p.get("audio_range"),
                "headings": p.get("headings"),
                "headings_path": p.get("headings_path"),
                "content_hash": p.get("content_hash") or hashlib.sha256(text.encode("utf-8")).hexdigest()
            })

        if not parsed_chunks:
            return 0

        point_ids = [deterministic_point_id(c["chunk_id"]) for c in parsed_chunks]
        chunk_ids = [c["chunk_id"] for c in parsed_chunks]

        # Query Qdrant
        try:
            exist_map_q = ray.get(qdrant_actor.check_points_exist.remote(point_ids))
        except Exception as e:
            log.warning("qdrant existence check failed: %s", e)
            exist_map_q = {pid: False for pid in point_ids}

        # Query Neo4j
        try:
            neo_info = ray.get(neo4j_actor.get_chunks_info.remote(chunk_ids))
        except Exception as e:
            log.warning("neo4j existence check failed: %s", e)
            neo_info = {cid: {"exists": False, "qdrant_id": None} for cid in chunk_ids}

        to_embed_idxs: List[int] = []
        for i, c in enumerate(parsed_chunks):
            pid = point_ids[i]
            q_present = bool(exist_map_q.get(pid, False))
            ninfo = neo_info.get(c["chunk_id"], {"exists": False, "qdrant_id": None})
            if q_present:
                continue
            if not ninfo["exists"]:
                to_embed_idxs.append(i)
                continue
            if not ninfo.get("qdrant_id"):
                to_embed_idxs.append(i)

        # If nothing to embed, ensure neo4j contains chunks and update manifest then exit.
        if not to_embed_idxs:
            neo_payload = []
            for c, pid in zip(parsed_chunks, point_ids):
                neo_payload.append({
                    "chunk_id": c["chunk_id"],
                    "document_id": c["document_id"],
                    "text": c.get("text", ""),
                    "token_count": c["token_count"],
                    "file_name": c["file_name"],
                    "file_type": c["file_type"],
                    "source_url": c["source_url"],
                    "timestamp": c["timestamp"],
                    "page_number": c.get("page_number"),
                    "row_range": c.get("row_range"),
                    "token_range": c.get("token_range"),
                    "audio_range": c.get("audio_range"),
                    "headings": c.get("headings"),
                    "headings_path": c.get("headings_path"),
                    "content_hash": c.get("content_hash"),
                    "qdrant_point_id": pid
                })
            try:
                r = ray.get(neo4j_actor.bulk_write_chunks.remote(neo_payload))
                if not (isinstance(r, dict) and r.get("ok")):
                    log.warning("neo4j write returned error: %s", r)
            except Exception:
                log.exception("neo4j write failed for %s", raw_key)

            indexed_count = 0
            for i, c in enumerate(parsed_chunks):
                pid = point_ids[i]
                if exist_map_q.get(pid, False) or neo_info.get(c["chunk_id"], {}).get("exists", False):
                    indexed_count += 1
            manifest["indexed_chunks"] = indexed_count

            try:
                if mode == "s3" and boto_client:
                    write_manifest_s3_atomic(s3_bucket, raw_key, manifest, client=boto_client)
                else:
                    write_manifest_local_atomic(raw_key, manifest)
            except Exception:
                log.exception("failed writing manifest for %s", raw_key)
            return 0

        # embed missing
        new_points = []
        embed_texts = [parsed_chunks[i]["text"][:max_tokens] for i in to_embed_idxs]
        if embed_handle is None:
            log.error("embed handle not available; skipping embedding for %s", raw_key)
            return 0

        try:
            for i in range(0, len(embed_texts), embed_batch):
                batch_texts = embed_texts[i:i + embed_batch]
                payload = {"texts": batch_texts, "max_length": max_tokens}
                resp = call_serve(embed_handle, payload, timeout=embed_timeout)
                if isinstance(resp, dict):
                    vecs = resp.get("vectors") or resp.get("embeddings") or resp.get("data")
                else:
                    vecs = resp
                if vecs is None:
                    raise RuntimeError("no vectors returned")
                if isinstance(vecs, dict) and "embeddings" in vecs:
                    vecs = vecs["embeddings"]
                if not isinstance(vecs, list):
                    raise RuntimeError("vectors not list")
                for j, vec in enumerate(vecs):
                    idx = to_embed_idxs[i + j]
                    c = parsed_chunks[idx]
                    pid = point_ids[idx]
                    if MODE == "vector_only":
                        payload = {
                            "chunk_id": c["chunk_id"],
                            "document_id": c["document_id"],
                            "text": c.get("text", ""),
                            "token_count": c["token_count"],
                            "file_name": c["file_name"],
                            "file_type": c["file_type"],
                            "source_url": c["source_url"],
                            "timestamp": c["timestamp"],
                            "content_hash": c.get("content_hash"),
                            "page_number": c.get("page_number"),
                            "row_range": c.get("row_range"),
                            "token_range": c.get("token_range"),
                            "audio_range": c.get("audio_range"),
                            "headings": c.get("headings"),
                            "headings_path": c.get("headings_path"),
                            "source_file_hash": file_hash,
                        }
                    else:
                        payload = {
                            "chunk_id": c["chunk_id"],
                            "document_id": c["document_id"]
                        }
                    new_points.append({"id": pid, "vector": [float(x) for x in vec], "payload": payload})
        except Exception as e:
            log.exception("embedding failed for %s: %s", raw_key, e)
            return 0

        # re-check existence for new_points to avoid duplicate upserts
        try:
            candidate_ids = [p["id"] for p in new_points]
            if candidate_ids:
                exist_after_embed = ray.get(qdrant_actor.check_points_exist.remote(candidate_ids))
            else:
                exist_after_embed = {}
        except Exception as e:
            log.warning("qdrant existence re-check failed: %s", e)
            exist_after_embed = {}

        # filter out already-existing points
        filtered_new_points = []
        for p in new_points:
            pid = p["id"]
            if exist_after_embed.get(pid, False):
                log.debug("Skipping upsert for already-existing point %s", pid)
                continue
            filtered_new_points.append(p)

        # upsert new vectors to Qdrant
        indexed_new = 0
        try:
            for i in range(0, len(filtered_new_points), batch_size):
                batch = filtered_new_points[i:i + batch_size]
                r = ray.get(qdrant_actor.upsert_points.remote(batch))
                if isinstance(r, dict) and r.get("ok"):
                    indexed_new += r.get("inserted", len(batch))
                else:
                    log.warning("qdrant upsert returned error for %s: %s", raw_key, r)
        except Exception as e:
            log.exception("qdrant upsert invocation failed: %s", e)

        # After upsert, re-check Neo4j to decide what to write
        try:
            neo_info_final = ray.get(neo4j_actor.get_chunks_info.remote(chunk_ids))
        except Exception as e:
            log.warning("neo4j final existence check failed: %s", e)
            neo_info_final = {cid: {"exists": False, "qdrant_id": None} for cid in chunk_ids}

        # write neo4j with the canonical qdrant ids (only where needed)
        neo_payload = []
        for c, pid in zip(parsed_chunks, point_ids):
            ninfo = neo_info_final.get(c["chunk_id"], {"exists": False, "qdrant_id": None})
            if not ninfo.get("exists") or not ninfo.get("qdrant_id"):
                neo_payload.append({
                    "chunk_id": c["chunk_id"],
                    "document_id": c["document_id"],
                    "text": c.get("text", ""),
                    "token_count": c["token_count"],
                    "file_name": c["file_name"],
                    "file_type": c["file_type"],
                    "source_url": c["source_url"],
                    "timestamp": c["timestamp"],
                    "page_number": c.get("page_number"),
                    "row_range": c.get("row_range"),
                    "token_range": c.get("token_range"),
                    "audio_range": c.get("audio_range"),
                    "headings": c.get("headings"),
                    "headings_path": c.get("headings_path"),
                    "content_hash": c.get("content_hash"),
                    "qdrant_point_id": pid
                })
        try:
            if neo_payload:
                r = ray.get(neo4j_actor.bulk_write_chunks.remote(neo_payload))
                if not (isinstance(r, dict) and r.get("ok")):
                    log.warning("neo4j write returned error: %s", r)
        except Exception:
            log.exception("neo4j write failed for %s", raw_key)

        # compute indexed_chunks count using final Qdrant and Neo4j state
        try:
            exist_map_final = ray.get(qdrant_actor.check_points_exist.remote(point_ids))
        except Exception as e:
            log.warning("qdrant final existence check failed: %s", e)
            exist_map_final = {pid: False for pid in point_ids}
        try:
            neo_info_final = ray.get(neo4j_actor.get_chunks_info.remote(chunk_ids))
        except Exception as e:
            log.warning("neo4j final existence check failed: %s", e)
            neo_info_final = {cid: {"exists": False, "qdrant_id": None} for cid in chunk_ids}

        existing_count = 0
        for i, c in enumerate(parsed_chunks):
            pid = point_ids[i]
            if exist_map_final.get(pid, False) or neo_info_final.get(c["chunk_id"], {}).get("exists", False):
                existing_count += 1

        manifest["indexed_chunks"] = existing_count

        try:
            if mode == "s3" and boto_client:
                write_manifest_s3_atomic(s3_bucket, raw_key, manifest, client=boto_client)
            else:
                write_manifest_local_atomic(raw_key, manifest)
        except Exception:
            log.exception("failed writing manifest for %s", raw_key)

        return indexed_new

    finally:
        if acquired_lease and neo4j_actor:
            try:
                rel = ray.get(neo4j_actor.release_file_lock.remote(file_hash, owner))
                if isinstance(rel, dict) and not rel.get("ok"):
                    log.warning("lease release returned error for %s: %s", raw_key, rel)
            except Exception as e:
                log.warning("lease release RPC failed for %s: %s", raw_key, e)

# main
def main():
    _ensure_ray_connected()
    log.info("Connected to Ray (namespace=%s) MODE=%s", RAY_NAMESPACE, MODE)

    try:
        _ = get_strict_handle(EMBED_DEPLOYMENT, timeout=10.0)
    except Exception:
        log.warning("embed handle unresolved on driver; workers will resolve")

    qdrant_actor = ensure_actor(QdrantWriter, "qdrant_writer", ["check_points_exist", "upsert_points"], QDRANT_URL, QDRANT_API_KEY, QDRANT_COLLECTION, VECTOR_DIM, QDRANT_ON_DISK_PAYLOAD)
    neo4j_actor = ensure_actor(Neo4jWriter, "neo4j_writer", ["bulk_write_chunks", "get_chunks_info", "try_acquire_file_lock", "release_file_lock"], NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)

    inputs: List[Tuple[str, str]] = []
    if not DATA_IN_LOCAL and S3_BUCKET:
        import boto3
        s3 = boto3.client("s3")
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=S3_RAW_PREFIX):
            for obj in page.get("Contents", []) or []:
                k = obj["Key"]
                if k.endswith("/") or k.lower().endswith(".manifest.json"):
                    continue
                inputs.append(("s3", k))
    else:
        base = Path(LOCAL_DIR_PATH)
        raw_dir = base / "raw"
        if raw_dir.exists():
            for root, _, files in os.walk(raw_dir):
                for f in files:
                    if f.endswith(".manifest.json"):
                        continue
                    inputs.append(("local", os.path.join(root, f)))

    if not inputs:
        log.warning("No raw inputs discovered")
        print("Total indexed chunks: 0")
        return

    cfg = {
        "s3_bucket": S3_BUCKET,
        "s3_raw_prefix": S3_RAW_PREFIX,
        "s3_chunked_prefix": S3_CHUNKED_PREFIX,
        "local_dir": LOCAL_DIR_PATH,
        "embed_batch": EMBED_BATCH,
        "max_tokens": INDEXING_EMBEDDER_MAX_TOKENS,
        "batch_size": BATCH_SIZE,
        "force_rehash": FORCE_REHASH,
        "embed_timeout": EMBED_TIMEOUT,
    }

    total_indexed = 0
    batch_inputs = int(os.getenv("BATCH_INPUTS", "8"))
    for i in range(0, len(inputs), batch_inputs):
        group = inputs[i:i + batch_inputs]
        futures = []
        for mode, key in group:
            futures.append(ingest_file_worker.remote(key, mode, qdrant_actor, neo4j_actor, cfg))
        for f in futures:
            try:
                n = ray.get(f)
                total_indexed += int(n or 0)
            except Exception as e:
                log.exception("ingest task failed: %s", e)

    print(f"Total indexed chunks: {total_indexed}")

if __name__ == "__main__":
    main()
