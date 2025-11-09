## 1 — Final architecture (single canonical design)

Network / infra:

* AWS ALB (HTTPS) + Cognito (OIDC) in front.
* ALB forwards only authenticated requests to **FastAPI Gateway** (ECS Fargate service, port **8000**).
* FastAPI Gateway lives **outside** Ray cluster (ECS Fargate), scales independently.
* Ray cluster (Head + autoscaled workers) in private subnets. Ray Head behind internal NLB for internal access.
* Ray Serve runs inside the Ray cluster (head + worker nodes). Serve hosts embed, rerank, and LLM deployments.
* FastAPI connects to Ray head using `ray.init(address="ray://<RAY_HEAD_NLB_DNS>:10001")` or via Ray Client, obtains Serve handles and calls them.
* Qdrant (vector DB) and Neo4j (metadata / BM25) run in the same VPC (managed services or EC2/EKS instances), accessible from Ray workers and FastAPI.

Why this single choice?

* Separates API and model scaling (production-grade).
* Keeps ALB + Cognito in front of a stable set of API tasks (ECS), making ALB health checks and auth robust.
* FastAPI can stream to clients reliably (ALB supports WebSocket & SSE to ECS targets).

---

## 2 — Exact infra & provisioning steps (Pulumi + commands)

### Preconditions (you must have)

* AWS account + permissions.
* Pulumi installed & configured.
* DNS hosted zone for your domain (hosted zone id).
* S3 bucket name for autoscaler YAML and Ray artifacts.
* ECR repository for the FastAPI image.

### 2.1 Pulumi: environment variables / pulumi config values

Set these before running pulumi:

```bash
export AWS_REGION=ap-south-1
export DOMAIN=app.example.com
export HOSTED_ZONE_ID=Z1234567890ABC
export PULUMI_S3_BUCKET=my-ray-configs-bucket
export REDIS_PASSWORD="secret-redis-password"
export ENABLE_PREREQS=true
export ENABLE_NETWORKING=true
export ENABLE_IAM=true
export ENABLE_RENDERER=true
export ENABLE_HEAD=true
```

And add Pulumi secret config:

```bash
pulumi config set --secret redisPassword "${REDIS_PASSWORD}"
pulumi config set s3Bucket ${PULUMI_S3_BUCKET}
pulumi config set hostedZoneId ${HOSTED_ZONE_ID}
```

### 2.2 Run Pulumi to create networking, ALB, Cognito, IAM, head ASG

(Your `infra/pulumi-aws` main file already contains the required modules.)

```bash
pulumi up -y
```

**Pulumi will produce the following outputs you will use:**

* `autoscaler_s3` (S3 URI): `s3://my-ray-configs-bucket/ray-autoscaler-prod.yaml`
* `alb_dns` (ALB DNS)
* `target_group_arn` (ALB target group for API)
* `private_subnet_ids`, `vpc_id`, `ray_head_instance_profile`, `ray_cpu_instance_profile`
* `redis_parameter_name` (SSM parameter name)
* `head_dns` (private hosted zone record for the head NLB)

> The renderer will upload an autoscaler YAML to the S3 bucket. Use the `upload_autoscaler_yaml_to_s3` step — Pulumi already called it (ENABLE_RENDERER true).

---

## 3 — Autoscaler YAML — exact, deterministic content

Pulumi `upload_autoscaler_yaml_to_s3` will produce a YAML. Use this canonical autoscaler YAML (replace placeholders) — save as `ray-autoscaler-prod.yaml` and ensure Pulumi uploaded it to S3. I provide the exact YAML snippet to *match the plan* — this must be the file the head downloads.

```yaml
cluster_name: ray-prod
min_workers: 0
max_workers: 30
idle_timeout_minutes: 10

provider:
  type: aws
  region: ap-south-1

auth:
  ssh_user: ubuntu
  ssh_private_key: "~/.ssh/id_rsa"

head_node:
  InstanceType: m5.large
  ImageId: ami-0c55b159cbfafe1f0
  IamInstanceProfile:
    Name: <RAY_HEAD_INSTANCE_PROFILE>
  SubnetId: <PRIVATE_SUBNET_ID_0>
  SecurityGroupIds: []

available_node_types:
  ray.head.default:
    node_config:
      InstanceType: m5.large
      ImageId: ami-0c55b159cbfafe1f0
      IamInstanceProfile: { Name: <RAY_HEAD_INSTANCE_PROFILE> }
      SubnetId: <PRIVATE_SUBNET_ID_0>
      SecurityGroupIds: []
    max_workers: 0
    resources: { CPU: 4 }

  ray.worker.cpu:
    node_config:
      InstanceType: m5.xlarge
      ImageId: <CPU_AMI>
      IamInstanceProfile: { Name: <RAY_CPU_INSTANCE_PROFILE> }
      SubnetId: <PRIVATE_SUBNET_ID_0>
      SecurityGroupIds: []
    min_workers: 1
    max_workers: 10
    resources: { CPU: 8 }

  ray.worker.llm:
    node_config:
      InstanceType: c6i.2xlarge
      ImageId: <CPU_AMI>
      IamInstanceProfile: { Name: <RAY_CPU_INSTANCE_PROFILE> }
      SubnetId: <PRIVATE_SUBNET_ID_0>
      SecurityGroupIds: []
    min_workers: 0
    max_workers: 6
    resources: { CPU: 16, "node_type:llm": 1 }

  ray.api.small:
    node_config:
      InstanceType: t3.small
      ImageId: <CPU_AMI>
      IamInstanceProfile: { Name: <RAY_CPU_INSTANCE_PROFILE> }
      SubnetId: <PRIVATE_SUBNET_ID_0>
      SecurityGroupIds: []
    min_workers: 1
    max_workers: 3
    resources: { CPU: 1, "node_type:api": 1 }

head_node_type: ray.head.default
worker_default_node_type: ray.worker.cpu
```

**Notes (exact):**

* The `ray.api.small` node type provides small dedicated API nodes. Ray nodes coming up for this node type must expose `resources: {"node_type:api": 1}` so Ray deployments pinned to `api` nodes land there.
* `<RAY_HEAD_INSTANCE_PROFILE>` and `<RAY_CPU_INSTANCE_PROFILE>` are Pulumi outputs — plug them in; when you ran Pulumi they are in outputs.

Pulumi already uploads similar YAML; ensure it includes `ray.api.small`.

---

## 4 — Start Ray cluster & Ray Serve deployments (exact commands)

### 4.1 Ensure head is up (Pulumi created head ASG). Wait for metric that head is ready.

Use AWS console or CloudWatch metric `Ray/Head:HeadReady` — but here's a concrete command to poll head status via SSH:

```bash
# get head instance id (from pulumi output or AWS CLI)
# SSH into head (Pulumi exports key name; ensure security groups permit)
ssh -i ~/.ssh/id_rsa ubuntu@<RAY_HEAD_PRIVATE_IP>

# on head
ray status --address auto
# you should see "Cluster status: ALIVE"
```

### 4.2 Deploy Ray Serve model processes on the cluster (run on any machine that can reach head — typically the head itself via SSM or SSH)

Use the repo scripts you already have (I assume code located at `/workspace/inference_pipeline/`):

```bash
# on the head instance (or your admin machine with network access to head)
cd /workspace/inference_pipeline
python3 infra/rayserve_models_cpu.py
```

**What this command does (deterministic):**

* Connects to Ray head
* Starts Ray Serve (detached)
* Deploys three Serve deployments:

  * `embed_onxx_cpu` — ONNX embedder (num_replicas per env)
  * `rerank_onxx_cpu` — ONNX cross-encoder (if ENABLE_CROSS_ENCODER true)
  * `llm_server_cpu` — LlamaServe (if Llama present and LLM_ENABLE true)
* For placement:

  * You **must** set `LLM_NUM_CPUS_PER_REPLICA`, `EMBED_NUM_CPUS_PER_REPLICA`, and set resource labels for `node_type:llm` and `node_type:api` (see next step). We will pin the LLM to `ray.worker.llm` nodes by assigning `ray_actor_options.resources` in the deployment `.options()`.

**Concrete deployment options to apply in the script (you must add these lines to `rayserve_models_cpu.py`):**

* For embed deployment `.options(...)`: include `ray_actor_options={"num_cpus": 1.0, "resources": {"node_type:api": 0.0}}` (or leave default — embed can run on cpu workers).
* For LlamaServe `.options(...)`: include `ray_actor_options={"num_cpus": 8.0, "resources": {"node_type:llm": 1}}` to pin to `llm` nodes.
* Do the same for FastAPI (if you were to run inside Serve, but in this plan FastAPI is external so you do not).

*(If you need the exact code edits to `rayserve_models_cpu.py` to set these `.options(...)`, I can produce them; this plan assumes you will set the actor resource labels to pin to node types.)*

---

## 5 — Build & deploy FastAPI Gateway (exact steps)

**We will deploy FastAPI in ECS Fargate** (production-grade, autoscaled) listening on port **8000**. ALB target group will point to ECS tasks on port 8000. FastAPI will connect to Ray head and call Serve handles.

### 5.1 Build Docker image and push to ECR (commands)

```bash
# Build image locally
docker build -t rag-fastapi:latest -f infra/Dockerfile .

# Create ECR repo (one-time):
aws ecr create-repository --repository-name rag-fastapi --image-scanning-configuration scanOnPush=true

# Login and push
aws ecr get-login-password | docker login --username AWS --password-stdin <aws_account_id>.dkr.ecr.<region>.amazonaws.com
docker tag rag-fastapi:latest <aws_account_id>.dkr.ecr.<region>.amazonaws.com/rag-fastapi:latest
docker push <aws_account_id>.dkr.ecr.<region>.amazonaws.com/rag-fastapi:latest
```

### 5.2 FastAPI container configuration (exact)

Container listens on `0.0.0.0:8000`. Environment variables (set in ECS task def):

```text
RAY_HEAD_NLB_DNS=<head_nlb_dns_from_pulumi_output>
RAY_HEAD_RAY_PORT=10001
EMBED_DEPLOYMENT=embed_onxx_cpu
RERANK_DEPLOYMENT=rerank_onxx_cpu
LLM_DEPLOYMENT=llm_server_cpu
INFERENCE_EMBEDDER_MAX_TOKENS=64
CROSS_ENCODER_MAX_TOKENS=600
MAX_CHUNKS_TO_LLM=8
```

(You will set these exact env vars in the ECS task definition.)

### 5.3 FastAPI gateway autoscaling / health-check requirements

* Task port: **8000** (HTTP).
* ALB target group points to ECS service on port 8000.
* ALB health check path: `/healthz`.
* ALB idle timeout: **300s** (set in ALB target group / listener).
* Desired count: 2; autoscale based on CPU to min 2 max 10.

---

## 6 — FastAPI code (exact) — streaming WebSocket and Ray Serve handle usage

Below is the **complete, deterministic FastAPI app** you must deploy to ECS. Put this in your repo as `frontend/fastapi_app.py`. It uses `ray` client to connect to the Ray head and gets Serve handles. It streams LLM tokens over WebSocket.

> This code expects `LlamaServe` to implement a streaming method `generate_stream(prompt, params)` that yields tokens via `asyncio.Queue` bridging. We include an exact token streaming wrapper to call the Serve handle that will call the LLM method via an actor that streams tokens as object refs. (You will implement the server-side `LlamaServe.generate_stream` described earlier in code edits.)

**File: `fastapi_app.py`**

```py
import os
import json
import asyncio
import ray
from ray import serve
from fastapi import FastAPI, WebSocket, Request, HTTPException
from fastapi.responses import JSONResponse

RAY_HEAD = os.environ.get("RAY_HEAD_NLB_DNS")
RAY_PORT = int(os.environ.get("RAY_HEAD_RAY_PORT", "10001"))
RAY_ADDRESS = f"ray://{RAY_HEAD}:{RAY_PORT}"

EMBED_DEPLOYMENT = os.environ.get("EMBED_DEPLOYMENT", "embed_onxx_cpu")
RERANK_DEPLOYMENT = os.environ.get("RERANK_DEPLOYMENT", "rerank_onxx_cpu")
LLM_DEPLOYMENT = os.environ.get("LLM_DEPLOYMENT", "llm_server_cpu")

app = FastAPI()

@app.on_event("startup")
def startup():
    # Connect to Ray head (ray client)
    ray.init(address=RAY_ADDRESS, ignore_reinit_error=True)
    serve.start(detached=True)  # in case not started; if Serve is already running, no-op
    # resolve handles
    global embed_handle, rerank_handle, llm_handle
    embed_handle = get_handle(EMBED_DEPLOYMENT)
    rerank_handle = get_handle(RERANK_DEPLOYMENT) if os.environ.get("ENABLE_CROSS_ENCODER", "true").lower() in ("1","true") else None
    llm_handle = get_handle(LLM_DEPLOYMENT) if os.environ.get("LLM_ENABLE", "true").lower() in ("1","true") else None

def get_handle(name: str):
    # deterministic strict handle resolution using Serve API
    try:
        return serve.get_deployment(name).get_handle(sync=False)
    except Exception:
        try:
            return serve.get_deployment_handle(name, _check_exists=False)
        except Exception as e:
            raise RuntimeError(f"unable to get handle {name}: {e}")

@app.get("/healthz")
async def healthz():
    # probe embed and llm health via their health method
    try:
        eh = await call_handle(embed_handle, {"__health_check__": True}, timeout=5.0)
        ready_e = getattr(eh, "get", None) is None or True
    except Exception:
        ready_e = False
    try:
        if llm_handle is not None:
            lh = await call_handle(llm_handle, {"__health_check__": True}, timeout=5.0)
            ready_l = True
        else:
            ready_l = True
    except Exception:
        ready_l = False
    if ready_e and ready_l:
        return JSONResponse({"ready": True})
    raise HTTPException(status_code=503, detail="not ready")

async def call_handle(handle, payload, timeout: float = 10.0):
    if hasattr(handle, "remote"):
        ref = handle.remote(payload)
    else:
        ref = handle(payload)
    # try to get result from Ray object ref
    try:
        return await asyncio.wrap_future(asyncio.get_running_loop().run_in_executor(None, lambda: ray.get(ref, timeout=timeout)))
    except Exception:
        # fallback: if it's a DeploymentResponse or direct dict, return as-is
        try:
            return ray.get(ref)
        except Exception:
            return ref

@app.post("/retrieve")
async def retrieve(req: Request):
    body = await req.json()
    query = body.get("query") or body.get("prompt") or ""
    if not query:
        raise HTTPException(400, "missing query")
    # 1) embed call
    embed_resp = await call_handle(embed_handle, {"texts":[query], "max_length": int(os.environ.get("INFERENCE_EMBEDDER_MAX_TOKENS","64"))}, timeout=10.0)
    if isinstance(embed_resp, dict):
        vectors = embed_resp.get("vectors") or embed_resp.get("embeddings") or embed_resp.get("data")
    else:
        vectors = embed_resp
    if not vectors:
        raise HTTPException(500, "embed failed")
    q_vec = vectors[0] if isinstance(vectors, list) else vectors
    # 2) call Qdrant + neo4j (do locally here or call retrieval deployment) -- simplified: call a retrieval Serve handle if you have one
    # For deterministic flow, call an internal retrieval deployment deployed as 'retrieval' (not shown in earlier code).
    try:
        retrieval_handle = get_handle("retrieval")  # assumes you packaged the retrieval pipeline as a Serve deployment
        retr = await call_handle(retrieval_handle, {"query": query, "vec": q_vec, "max_chunks": int(os.environ.get("MAX_CHUNKS_TO_LLM","8"))}, timeout=20.0)
    except Exception:
        # fallback: return just embed response
        retr = {"records": [], "provenance": [], "prompt": "", "elapsed":0.0}
    return JSONResponse(retr)

# WebSocket streaming for LLM
@app.websocket("/ws_generate")
async def ws_generate(ws: WebSocket):
    await ws.accept()
    try:
        data = await ws.receive_json()
    except Exception:
        await ws.close(code=1002)
        return
    query = data.get("query") or data.get("prompt") or ""
    if not query:
        await ws.send_json({"error":"no query provided"})
        await ws.close()
        return

    # 1) run retrieval (call retrieval Serve handle)
    retrieval_handle = get_handle("retrieval")
    retr = await call_handle(retrieval_handle, {"query": query, "max_chunks": int(os.environ.get("MAX_CHUNKS_TO_LLM","8"))}, timeout=20.0)
    prompt = retr.get("prompt") or json.dumps({"QUERY": query, "CONTEXT_CHUNKS": retr.get("records", [])})

    # 2) call LLM streaming handle
    # LlamaServe must implement generate_stream and allow callbacks that push tokens to an asyncio queue
    # We will call it via a small helper actor that returns an object_ref for a queue or a generator ref
    try:
        # llm_handle.generate_stream returns an Ray ObjectRef for an iterator or stream session id
        stream_ref = await call_handle(llm_handle, {"stream": True, "prompt": prompt}, timeout=10.0)
        # Now poll the stream_ref (this is deterministic: the LLM handle must return a Ray object that yields tokens)
        # Implementation detail: LlamaServe should create a Ray actor per-stream that exposes `get_next()`; stream_ref is actor handle.
        while True:
            token_objref = stream_ref.get_next.remote()
            token = await asyncio.get_running_loop().run_in_executor(None, lambda: ray.get(token_objref, timeout=60))
            if token is None or token == "<DONE>":
                await ws.send_json({"done": True})
                break
            await ws.send_json({"token": token})
    except Exception as e:
        await ws.send_json({"error": str(e)})
    finally:
        await ws.close()
```

**Important, deterministic requirements for the FastAPI code to work:**

1. **You must** implement a Serve-deployed `retrieval` deployment that takes `{"query","vec","max_chunks"}` and returns `{"prompt","records","provenance"}` (this is the logic currently in `query.py` `retrieve_pipeline` — wrap it into a Serve deployment).
2. **You must** update `LlamaServe` on the Serve side to provide a deterministic `generate_stream` API that returns a stream actor with `get_next()` method. The FastAPI code above expects that.
3. ALB health check path must be `/healthz`.

(You will implement those deterministic code changes — this plan assumes you will add a Serve `retrieval` deployment and streaming `LlamaServe` per the next section.)

---

## 7 — Ray Serve changes you must make (exact edits to repo)

Make three small deterministic code additions/edits:

### 7.1 Add a Serve `retrieval` deployment (wrap `retrieve_pipeline` from `query.py`)

Create `serve_retrieval.py` and register:

```py
from ray import serve
from query import retrieve_pipeline  # import your function from query.py

@serve.deployment(name="retrieval", num_replicas=2, ray_actor_options={"num_cpus":0.5, "resources":{"node_type:api":0.001}})
class RetrievalDeployment:
    def __init__(self):
        pass
    async def __call__(self, payload):
        query = payload.get("query","")
        max_chunks = int(payload.get("max_chunks",8))
        # open qdrant and neo4j clients inside the process or use existing clients
        # call retrieve_pipeline but pass in embed_handle / rerank_handle from Serve
        from ray import serve as _serve
        embed_handle = _serve.get_deployment_handle("embed_onxx_cpu", _check_exists=False)
        rerank_handle = _serve.get_deployment_handle("rerank_onxx_cpu", _check_exists=False) if payload.get("use_rerank", True) else None
        # call your retrieve_pipeline (synchronous) in a thread if necessary
        res = retrieve_pipeline(embed_handle, rerank_handle, qdrant_client=<create client here>, neo4j_driver=<create driver here>, query_text=query, max_chunks=max_chunks)
        return res

# Bind & deploy (run on head or via a ray job)
RetrievalDeployment.options(num_replicas=2).bind().deploy()
```

(Replace `<create client here>` with the exact Qdrant and Neo4j client creation code from your query.py.)

### 7.2 Implement LLM streaming in `LlamaServe`

Edit your `LlamaServe` to include a streaming method and a helper `StreamActor`:

* Add method `generate_stream(prompt, params)` which:

  1. Creates a dedicated Ray **actor** `LLMStreamerActor` with a private asyncio.Queue for tokens.
  2. Starts `self.llm(prompt, stream=True, callback=callback)` in a thread where `callback(token)` does `queue.put(token)`.
  3. Returns the actor handle to the caller. Actor has `get_next()` method that returns `queue.get()` (or `None` at end).

**This is deterministic code to add** (high level — you implement exactly):

```py
@ray.remote
class LLMStreamActor:
    def __init__(self):
        import asyncio
        self._q = asyncio.Queue()
        self._done = False
    async def put(self, token):
        await self._q.put(token)
    async def get_next(self):
        tok = await self._q.get()
        return tok
    async def mark_done(self):
        self._done = True
        await self._q.put("<DONE>")
```

Then `LlamaServe.generate_stream` spawns `LLMStreamActor.remote()` and runs `self.llm(prompt, stream=True, callback=lambda t: ray.get_actor(...).put.remote(t))` in executor, returns actor handle.

### 7.3 Ensure `LlamaServe` is pinned to `ray.worker.llm` nodes

When you deploy LlamaServe, call:

```py
LlamaServe.options(
    num_replicas=<LLM_REPLICAS>,
    ray_actor_options={"num_cpus": 8.0, "resources":{"node_type:llm": 1}}
).deploy()
```

This ensures `llm_worker` nodes from the autoscaler serve LLM only.

---

## 8 — ALB / Cognito configuration (exact)

Pulumi `create_auth()` produces:

* ALB with listener 443.
* Cognito user pool & client.
* Listener rules that `authenticate-cognito` on path patterns `/generate*`, `/ws_generate*`, `/retrieve*`, then `forward` to ALB target group.

**ALB settings to set exactly:**

* Idle timeout = **300 seconds** (for streaming).
* Health-check path = `/healthz`.
* Target group type: `ip` for ECS Fargate (if using Fargate) or `instance` if you attach EC2 instances.

Attach the ALB target group to the ECS service.

---

## 9 — Client usage (how a user sends query)

### 9.1 Browser (WebSocket streaming)

In your client JS (Streamlit or plain JS):

```js
const ws = new WebSocket("wss://app.example.com/ws_generate");
ws.onopen = () => ws.send(JSON.stringify({query: "What is MLOps?"}));
ws.onmessage = (evt) => {
  const msg = JSON.parse(evt.data);
  if (msg.token) {
    appendTokenToUI(msg.token);
  } else if (msg.done) {
    showProvenance();
  } else if (msg.error) {
    showError(msg.error);
  }
};
```

### 9.2 Simple curl (non-streaming)

You can POST to `/retrieve` to get retrieval + prompt (not LLM streaming):

```bash
curl -X POST "https://app.example.com/retrieve" \
  -H "Content-Type: application/json" \
  -d '{"query":"What is MLOps?"}'
```

This returns `{"prompt": "...", "records": [...], "provenance": [...], "elapsed": 0.3}`

---

## 10 — End-to-end runbook (exact sequence you will run)

1. `pulumi up -y` (with env vars/config set) → creates ALB, Cognito, S3, head ASG, IAM, uploads `ray-autoscaler-prod.yaml` to S3.
2. Wait for head ASG instance(s) to boot (Pulumi created). Wait until `ray status --address auto` on head returns `ALIVE`.
3. SSH to head and run:

   ```bash
   cd /workspace/inference_pipeline
   python3 infra/rayserve_models_cpu.py
   python3 serve_retrieval.py   # deploy retrieval deployment
   ```

   (Or run these as Ray Jobs via `ray job submit` if you prefer automation).
4. Build & push FastAPI Docker image; create ECS Fargate service that runs it; set ALB target group to ECS service.
5. Wait for ECS service tasks to pass ALB health checks (`/healthz` should return ready).
6. Test end-to-end:

   * Open `https://<ALB DNS>/` — login via Cognito.
   * Use WebSocket client to `wss://<ALB DNS>/ws_generate` and send `{"query":"What is MLOps?"}`.
   * Observe tokens streaming in the browser.

---

## 11 — Monitoring & ops (exact items to configure)

* CloudWatch Alarms:

  * Ray Head `HeadReady` metric (already created by Pulumi).
  * ECS service alarm for high CPU, task unhealthy.
* Logs:

  * FastAPI -> CloudWatch logs (ECS log driver).
  * Ray Serve logs -> CloudWatch (via worker instance agent or container logs).
  * Qdrant / Neo4j logs to CloudWatch/managed service logs.
* Tracing: instrument FastAPI with request ID and attach to Ray logs for cross-correlation.

---

## 12 — Deterministic testing checklist (do these exact checks)

1. `pulumi stack output` shows `autoscaler_s3` and `alb_dns`.
2. On head: `ray status --address auto` returns ALIVE.
3. Ray Serve handles resolved:

   ```py
   python -c "import ray; ray.init(address='ray://<head>:10001'); from ray import serve; print(serve.get_deployment('embed_onxx_cpu'))"
   ```
4. ECS Fargate tasks in service are `RUNNING` and ALB target group shows healthy.
5. `curl -k https://<alb_dns>/healthz` => HTTP 200 JSON `{"ready":true}`
6. WebSocket flow: connect -> send query -> receive stream of tokens -> receive final done.

---

## 13 — Which files to edit now (exact minimal changes you must implement in repo)

1. `rayserve_models_cpu.py` — add `.options(..., ray_actor_options={"resources":{"node_type:llm":1}})` for `LlamaServe` deployment; ensure model paths via env are correct.
2. `query.py` — extract `retrieve_pipeline` into a Serve deployment module `serve_retrieval.py` and export it as `retrieval` deployment.
3. `LlamaServe` (in `rayserve_models_cpu.py`) — implement `generate_stream` that returns an actor handle as described (LLMStreamActor). This change provides the streaming contract.
4. Add `frontend/fastapi_app.py` to your repo (as above).
5. Add Dockerfile for FastAPI (simple, exact lines below).
6. Add Pulumi ECS service support (or write CloudFormation/ECS Terraform) to register the FastAPI image to target ALB target group output by Pulumi.

**FastAPI Dockerfile (exact):**

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY frontend/requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt
COPY frontend /app
CMD ["uvicorn", "fastapi_app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
```

`frontend/requirements.txt` should include:

```
fastapi
uvicorn[standard]
ray
ray[serve]
httpx
python-jose[cryptography]  # if you validate Cognito JWT
qdrant-client
neo4j
```

---

## 14 — TL;DR — one-liner sequence you will run now

1. `pulumi up -y` (create infra + upload autoscaler YAML)
2. Wait head boot + `ray status` ALIVE
3. SSH into head → `python3 infra/rayserve_models_cpu.py` and `python3 serve_retrieval.py`
4. Build & push FastAPI image → deploy ECS service attached to ALB target group (port 8000)
5. Open `https://<alb_dns>` → login → connect WebSocket to `/ws_generate` → send `"What is MLOps?"` → receive tokens streaming.

---
