from __future__ import annotations
import os,sys,json,time,uuid,logging
from typing import List,Dict,TypedDict,Optional,Any
from functools import wraps
from pathlib import Path
import ray
from ray import serve
from ray.serve.handle import DeploymentResponse
from neo4j import GraphDatabase
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct,VectorParams,Distance
class Doc(TypedDict):
    external_id: str
    title: str
    text: str
RAY_ADDRESS=os.getenv("RAY_ADDRESS","auto")
APP_NAME=os.getenv("SERVE_APP_NAME","default")
EMBED_DEPLOYMENT=os.getenv("EMBED_DEPLOYMENT","embed_onxx")
QDRANT_URL=os.getenv("QDRANT_URL","http://localhost:6333")
QDRANT_API_KEY=os.getenv("QDRANT_API_KEY") or None
PREFER_GRPC=os.getenv("PREFER_GRPC","true").lower() in ("1","true","yes")
COLLECTION=os.getenv("COLLECTION","my_collection")
VECTOR_DIM=int(os.getenv("VECTOR_DIM","768"))
BATCH_SIZE=int(os.getenv("BATCH_SIZE","256"))
CHUNK_SIZE_WORDS=int(os.getenv("CHUNK_SIZE_WORDS","60"))
EMBED_TIMEOUT=int(os.getenv("EMBED_TIMEOUT","60"))
INGEST_SOURCE=os.getenv("INGEST_SOURCE","")
NEO4J_URI=os.getenv("NEO4J_URI","bolt://localhost:7687")
NEO4J_USER=os.getenv("NEO4J_USER","neo4j")
NEO4J_PASSWORD=os.getenv("NEO4J_PASSWORD","neo4j")
SERVE_WAIT=float(os.getenv("SERVE_WAIT","60"))
RETRY_ATTEMPTS=int(os.getenv("RETRY_ATTEMPTS","3"))
RETRY_BASE=float(os.getenv("RETRY_BASE_SECONDS","0.5"))
logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO"))
log=logging.getLogger("ingest")
def retry(attempts: int = RETRY_ATTEMPTS, base: float = RETRY_BASE):
    def deco(fn):
        def wrapper(*a,**k):
            last_exc=None
            for i in range(attempts):
                try:
                    return fn(*a,**k)
                except Exception as e:
                    last_exc=e
                    wait=base*(2**i)
                    log.warning("retry %d/%d %s: %s (sleep %.2fs)",i+1,attempts,fn.__name__,e,wait)
                    time.sleep(wait)
            log.error("all retries failed for %s",fn.__name__)
            raise last_exc
        return wrapper
    return deco
def chunk_text(text: str, chunk_size_words: int = CHUNK_SIZE_WORDS) -> List[str]:
    words=text.split()
    if not words:
        return []
    return [" ".join(words[i:i+chunk_size_words]) for i in range(0,len(words),chunk_size_words)]
@retry()
def ensure_qdrant_collection(client: QdrantClient, collection: str, dim: int) -> None:
    existing=client.get_collections().collections
    names=[c.name for c in existing]
    if collection not in names:
        client.create_collection(collection_name=collection,vectors_config=VectorParams(size=dim,distance=Distance.COSINE))
        log.info("Created qdrant collection %s dim=%d",collection,dim)
    else:
        log.info("Qdrant collection %s already exists",collection)
@retry()
def upsert_points(client: QdrantClient, collection: str, points: List[PointStruct], batch_size: int = BATCH_SIZE) -> None:
    if not points:
        return
    for i in range(0,len(points),batch_size):
        batch=points[i:i+batch_size]
        client.upsert(collection_name=collection,points=batch)
        log.info("Upserted %d points to %s",len(batch),collection)
@retry()
def merge_document_node(driver: GraphDatabase.driver, external_id: str, title: str, text: str) -> None:
    with driver.session() as session:
        session.run("MERGE (d:Document {neo4j_id:$id}) SET d.title=$title, d.text=$text, d.updated_at = datetime()",id=external_id,title=title,text=text)
def validate_embed_response(resp: dict, expected_batch: int) -> List[List[float]]:
    if not isinstance(resp,dict):
        raise RuntimeError(f"embed response must be dict; got {type(resp)}")
    vectors=resp.get("vectors")
    if not isinstance(vectors,list):
        raise RuntimeError(f"embed vectors must be list; got {type(vectors)}")
    if len(vectors)!=expected_batch:
        raise RuntimeError(f"embed returned {len(vectors)} vectors for {expected_batch} inputs")
    for i,v in enumerate(vectors):
        if not isinstance(v,(list,tuple)):
            raise RuntimeError(f"embed vector {i} is not list/tuple: {type(v)}")
        if len(v)!=VECTOR_DIM:
            raise RuntimeError(f"embed vector dim mismatch for idx {i}: got {len(v)} expected {VECTOR_DIM}")
    return vectors
def build_point(eid: str, idx: int, vec: List[float], chunk_text_str: str, title: Optional[str]) -> PointStruct:
    pid=f"{eid}-{idx}"
    payload: Dict[str,object]={"neo4j_id":eid,"chunk_id":pid,"text":chunk_text_str}
    if title:
        payload["title"]=title
    return PointStruct(id=pid,vector=vec,payload=payload)
def _get_embed_handle(name: str, timeout: float = SERVE_WAIT, poll: float = 1.0):
    address=RAY_ADDRESS or "auto"
    if not ray.is_initialized():
        ray.init(address=address,ignore_reinit_error=True)
    start=time.time()
    last_exc=None
    while time.time()-start<timeout:
        try:
            handle=serve.get_deployment_handle(name,app_name=APP_NAME)
            test_resp=handle.remote({"texts":["health-check"]})
            if isinstance(test_resp,DeploymentResponse):
                out=test_resp.result(timeout=EMBED_TIMEOUT)
            else:
                out=test_resp
            validate_embed_response(out,expected_batch=1)
            return handle
        except Exception as e:
            last_exc=e
            time.sleep(poll)
    raise RuntimeError(f"Timed out waiting for Serve deployment '{name}': last error: {last_exc}")
@retry()
def ingest(docs: List[Doc]) -> int:
    handle=_get_embed_handle(EMBED_DEPLOYMENT)
    qdrant=QdrantClient(url=QDRANT_URL,api_key=QDRANT_API_KEY,prefer_grpc=PREFER_GRPC)
    ensure_qdrant_collection(qdrant,COLLECTION,VECTOR_DIM)
    driver=GraphDatabase.driver(NEO4J_URI,auth=(NEO4J_USER,NEO4J_PASSWORD))
    points: List[PointStruct]=[]
    for doc in docs:
        eid=doc.get("external_id") or str(uuid.uuid4())
        title=doc.get("title","")
        text=doc.get("text","")
        merge_document_node(driver,eid,title,text)
        chunks=chunk_text(text,CHUNK_SIZE_WORDS)
        if not chunks:
            log.info("No chunks for %s; skipping",eid)
            continue
        resp=handle.remote({"texts":chunks})
        if isinstance(resp,DeploymentResponse):
            resp_out=resp.result(timeout=EMBED_TIMEOUT)
        else:
            resp_out=resp
        vectors=validate_embed_response(resp_out,expected_batch=len(chunks))
        for i,(chunk,vec) in enumerate(zip(chunks,vectors)):
            pt=build_point(eid,i,vec,chunk,title)
            required={"neo4j_id","chunk_id","text"}
            if not required.issubset(set(pt.payload.keys())):
                raise RuntimeError(f"point payload missing keys: {pt.payload.keys()}")
            points.append(pt)
        if len(points)>=BATCH_SIZE:
            upsert_points(qdrant,COLLECTION,points[:BATCH_SIZE],BATCH_SIZE)
            points=points[BATCH_SIZE:]
    if points:
        upsert_points(qdrant,COLLECTION,points,BATCH_SIZE)
    try:
        qdrant.close()
    except Exception:
        log.debug("qdrant.close failed",exc_info=True)
    try:
        driver.close()
    except Exception:
        log.debug("neo4j close failed",exc_info=True)
    return 0
if __name__=="__main__":
    docs_list: List[Doc]=[]
    if INGEST_SOURCE and Path(INGEST_SOURCE).exists():
        with open(INGEST_SOURCE,"r",encoding="utf8") as fh:
            for line in fh:
                line=line.strip()
                if not line:
                    continue
                obj=json.loads(line)
                if "external_id" not in obj or "text" not in obj:
                    raise RuntimeError("each line must be json with keys external_id and text")
                docs_list.append({"external_id":obj["external_id"],"title":obj.get("title",""),"text":obj["text"]})
    else:
        docs_list=[{"external_id":"product-A","title":"Product A spec","text":"Product A has features X Y Z used for analytics."},{"external_id":"manual-B","title":"User manual B","text":"To install B unpack the archive then run the installer and follow prompts."}]
    rc=ingest(docs_list)
    log.info("ingest complete rc=%s",rc)
    sys.exit(0)
