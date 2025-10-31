Embed, rerank and LLM are three independent Serve deployments. Each runs as a Ray actor. Each actor loads its model inside its __init__. Ray schedules those actors to nodes based on resource requests. Use Serve handles (not IPs) for robust intra-cluster RPC. Below is concrete, production-ready documentation that explains what the file does, how replicas get scheduled, how RPC works and how to tune everything.

Overview — what ray_serve_models.py provides

Three deployments:

EmbedDeployment — ONNX embedding model via onnxruntime. Lightweight CPU/GPU actor that returns vectors.

RerankDeployment — ONNX cross-encoder reranker via onnxruntime. Optional. Returns scores.

LLMServer + OpenAI ingress — vLLM-based generation service managed via Ray Serve LLM integration. Exposes OpenAI-style HTTP endpoints and can be called by handle.


A small FastAPI health app at /healthz and /ready.

Deterministic bind-style deployment using serve.run(*binds).

Per-deployment resource hints using ray_actor_options and LLMServer.get_deployment_options(...).

Safety: fail-fast checks for missing files and guarded runtime_env for vLLM.


What a Serve deployment actually is

@serve.deployment defines a Ray actor class.

num_replicas = number of actor instances created for that class.

Each replica runs __init__ on the node Ray selects, loads the model files and holds model state in-process.

The actor exposes __call__ which Serve uses to handle requests, or methods you call directly via handle.


Where code runs (physical mapping)

Ray head node runs control plane.

Worker nodes (EC2) run actor processes. Ray places them based on available resources.

Each ONNX replica runs an onnxruntime.InferenceSession inside the actor process on the node it was scheduled to.

Each LLM replica runs a vLLM engine inside the actor process (when using LLMServer).


How replicas are placed (scheduler basics)

Ray scheduler chooses nodes that satisfy requested resources: CPU, memory, num_gpus, custom resources.

Default packing vs spreading is Ray’s internal heuristic.

To avoid multiple replicas on same node use max_replicas_per_node=1 on the deployment. That prevents more than one replica of that deployment per node.

If num_replicas > number of compatible nodes, replicas stay pending until nodes appear (autoscaler) or you reduce replicas.

For complex placement (a single model spanning multiple GPUs) use placement groups (strategy SPREAD or STRICT_PACK) and vLLM tensor/pipeline parallelism.


Concrete placement controls and examples

Simple one-replica-per-node:


EmbedDeployment.options(num_replicas=4, max_replicas_per_node=1, ray_actor_options={"num_gpus":1.0})

Use placement group (SPREAD) to force Ray to reserve bundles on different nodes:


pg = ray.util.placement_group([{"GPU":1}]*4, strategy="SPREAD")
# then schedule actor with scheduling_strategy=PlacementGroupSchedulingStrategy(pg)

Node affinity (pin to node):


from ray.util.scheduling_strategies import NodeAffinitySchedulingStrategy
MyActor.options(scheduling_strategy=NodeAffinitySchedulingStrategy(node_id, soft=False)).deploy()

Use affinity only when necessary.

Resource hints you must set and why

ray_actor_options={"num_gpus": X} — required for GPU scheduling. Ray will only place the actor on nodes with at least X free GPUs.

num_cpus via .options(num_cpus=...) — gives Ray a CPU allocation for the actor and avoids CPU contention.

Memory: Ray does not have a strict per-actor memory flag like num_gpus; monitor memory but place nodes sized appropriately.

LLMServer.get_deployment_options(llm_config) — call this. It returns engine-aware options (MIG, fractional gpu hints, runtime_env). Use them rather than hand-guessing.

ONNX threads: tune ORT_INTRA_THREADS and ORT_INTER_THREADS for CPU performance.


Why replicas = independent processes matters for LLMs

Each replica loads its own model. Memory and GPU are used per replica.

For models that do not fit on a single GPU, do not spin multiple replicas. Instead:

Use vLLM parallelism options (tensor/pipeline).

Use LLMServer.get_deployment_options and a placement group to start a single distributed actor that spans GPUs.


For models that fit on one GPU, one replica per GPU is the usual pattern.


Internal RPC (robust patterns, avoid IP fragility)

Use Serve handles not IP addresses.

Get handle: h = serve.get_deployment_handle("deployment_name")

Call: ref = h.remote(payload) then ray.get(ref, timeout=...)

This is intra-cluster RPC. It remains valid if the actor moves nodes or restarts.


Do not hardcode actor IP/port. Those change during autoscale or restart.

Use ray.wait([ref], timeout=secs) to avoid indefinite blocking.

Cancel long requests with ray.cancel(ref) on timeout.

Example robust call:


ref = router_handle.completions.remote({"prompt":"hi","max_tokens":16})
ready, _ = ray.wait([ref], timeout=10)
if not ready:
    ray.cancel(ref); raise TimeoutError("LLM call timed out")
result = ray.get(ref)

For streaming results prefer HTTP OpenAI-style ingress with server-sent events. That uses HTTP and is simpler for client streaming semantics.


Making internal RPC reliable: retries and backoff

Retry transient failures with exponential backoff.

Use idempotent request design if re-tries are possible.

Example pattern:

Try up to N times.

On ray.exceptions.RayTaskError or ConnectionError, sleep backoff = base * 2**attempt then retry.


Limit total latency budget and surface meaningful errors to call sites.


Health checks, readiness and warm startup

/healthz should return server alive (Serve process started).

/ready should return true only when:

All required deployments have finished __init__ and are ready.

LLM engine finished loading.


Implement readiness by toggling flags in the deployment initialization code or by validating a cheap call to the actor (e.g., a small inference or status method).

Warm models on startup by making a small cheap request after deploy. This ensures model kernels are JITed/cached and avoids first-request latency spikes.


Autoscaling and max_replicas_per_node tradeoffs

num_replicas + max_replicas_per_node=1 => guarantee at most 1 replica per node.

Use autoscaler (Ray cluster autoscaler or K8s autoscaler) to add nodes if replicas remain pending.

If you allow multiple replicas per node you improve packing and cost efficiency, but risk contention and OOM.

For LLMs:

Prefer one vLLM replica per GPU for large models.

For small models, fractional GPUs can help but test carefully.



ONNX runtime-specific tuning

ONNX session options:

intra_op_num_threads and inter_op_num_threads must be tuned to your CPU topology.

If using CUDAExecutionProvider, ensure the CUDA-enabled onnxruntime wheel is installed and the node has compatible driver.


Batch size:

For embeddings, batch multiple texts in the tokenization stage to reduce per-sample overhead.

Use a queue or batching layer if traffic is high.



vLLM specifics (LLMServer)

Use LLMConfig(model_loading_config=..., runtime_env=...) to ensure worker nodes have required packages if not baked into AMI.

Call LLMServer.get_deployment_options(llm_config) and pass result into .options(**...). This captures engine GPU and scheduling needs.

To run a single model across GPUs use vLLM tensor/pipeline parallelism engine kwargs. That requires placement groups and careful sizing.

For streaming, OpenAI ingress supports stream endpoints; prefer ingress for external clients.


Observability and debugging advice

Use Ray Dashboard to inspect actors, placement, and resource utilization.

Export logging to stdout; collect with a log aggregator (CloudWatch / ELK).

Expose Prometheus metrics from actors if needed (via a metrics library).

Check ray status and ray nodes() for resource availability.

In actors include an endpoint or method that returns:

Node id where actor is running.

Free memory and GPU usage.

Model version and load time. Example inside actor:



import ray
def where_am_i(self):
    return {"node": ray.get_runtime_context().node_id}

For silent crashes, look at ray logs and journalctl on node.


Failure modes and mitigation

Replica pending due to lack of nodes -> autoscaler or reduce replicas.

Replica OOM -> decrease num_replicas, reduce batch sizes or use larger instances.

Package mismatch (CUDA/PyTorch/vLLM) -> bake AMI with pinned versions.

Model file missing -> fail-fast during startup and surface clear error logs.

Node disruptions -> Ray will reschedule actors. Use idempotent startup and persistent model sync paths.


Concrete tuning recommendations (starting points)

Small embed model:

EMBED_REPLICAS=2, EMBED_GPU_PER_REPLICA=0 (CPU), ORT_INTRA_THREADS=4.


Reranker:

RERANK_REPLICAS=1, RERANK_GPU_PER_REPLICA=0 or GPU if faster.


LLM (vLLM small ~6-8B that fits on 1 GPU):

LLM_REPLICAS=1 per GPU, LLM_GPU_PER_REPLICA=1.0, max_replicas_per_node=1.


If you need concurrency > throughput on one GPU:

Start multiple smaller replicas per node with fractional num_gpus only if validated for memory and vLLM support.


Timeouts:

LLM generation: ray.wait(ref, timeout=30) for short generation tasks; increase for long responses.


Health check frequency: every 10s for kube/lb probes; readiness should be conservative.


Deployment checklist (pre-launch)

1. Pin Ray and vLLM versions and test locally.


2. Bake AMI or container with CUDA driver, torch cu128, vllm 0.11.0, onnxruntime-gpu.


3. Pre-sync models to local disk (aws s3 sync) in user-data or bake into AMI.


4. Set env vars for model paths and resource hints.


5. Start Ray head and join workers; verify ray nodes.


6. Start serve_prod.py under systemd or supervisor.


7. Call /healthz and /ready. Warm LLM with a short request.


8. Run smoke tests: embed, rerank and generation via handle calls.



Example internal call patterns (concrete)

From another actor or the driver, robust calls:


# get handle
router = serve.get_deployment_handle("openai_ingress")
# remote call and timeout/ cancel
ref = router.completions.remote({"prompt":"hi","max_tokens":8})
ready, _ = ray.wait([ref], timeout=10)
if not ready:
    ray.cancel(ref)
    raise TimeoutError("LLM timeout")
res = ray.get(ref)

Embed handle:


h = serve.get_deployment_handle("embed_onxx")
vecs = ray.get(h.remote({"texts":["a","b"]}), timeout=5)

Security, networking and best practices

Do not expose node ports. Expose only the Serve HTTP endpoint. Use TLS and authentication at the ingress layer.

Use IAM roles for S3 access. Avoid embedding credentials in images.

Use private subnets and load balancers for production.


Short FAQs

Q: Do replicas use the same IP? A: No. Actors can move. Use Serve handles.

Q: How many vLLM engines run? A: One per replica by default. To avoid duplicates, run fewer replicas or use distributed vLLM mode.

Q: How to guarantee one replica per EC2? A: max_replicas_per_node=1 and ensure enough nodes exist.

Q: What if the LLM crashes at init? A: The process will log error and Ray will try to restart the actor. Design __init__ to fail fast with clear messages.

