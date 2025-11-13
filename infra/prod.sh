export SERVE_HTTP_HOST=0.0.0.0
export SERVE_HTTP_PORT=8003
export RAY_ADDRESS=
export RAY_NAMESPACE=default
export EMBED_REPLICAS=2
export RERANK_REPLICAS=1
export LLM_REPLICAS=2
export GATEWAY_REPLICAS=1
export EMBED_NUM_CPUS_PER_REPLICA=1.0
export RERANK_NUM_CPUS_PER_REPLICA=2.0
export LLM_NUM_CPUS_PER_REPLICA=4.0
export GATEWAY_CPUS=1.0
export EMBED_NUM_GPUS_PER_REPLICA=0.0
export RERANK_NUM_GPUS_PER_REPLICA=0.0
export LLM_NUM_GPUS_PER_REPLICA=0.0
export GATEWAY_HEAD_RESOURCE=0.01
export REQUIRE_LLM_ON_DEPLOY=true
export REQUIRE_EMBED_ON_DEPLOY=true
export REQUIRE_RERANK_ON_DEPLOY=false
export REQUIRE_INDEX_BACKENDS=true
export ENABLE_CROSS_ENCODER=true
export MODE=hybrid
export MAX_STREAM_SECONDS=240
export CALL_TIMEOUT_SECONDS=30
export HANDLE_RESOLVE_TIMEOUT=60
export LOG_LEVEL=INFO
export LLM_MODEL_PATH=/opt/models/llm.gguf
export ONNX_EMBED_PATH=/opt/models/emb.onnx
export ONNX_EMBED_TOKENIZER=/opt/models/emb.tokenizer.json
export ONNX_RERANK_PATH=/opt/models/rerank.onnx
export ONNX_RERANK_TOKENIZER=/opt/models/rerank.tokenizer.json

ray start --head --port=6379 --node-ip-address="$(curl -s http://169.254.169.254/latest/meta-data/local-ipv4)" --autoscaling-config=/etc/ray/autoscaler.yaml --metrics-export-port=8080 --resources='{"head":1}'


python3 - <<'PY'
import importlib, inspect, json
m = importlib.import_module("llama_cpp")
L = getattr(m, "Llama", None)
print("llama_module:", getattr(m, "__version__", "unknown"))
if L is None:
    print("Llama class: NOT FOUND")
else:
    attrs = sorted([a for a in dir(L) if not a.startswith("_")])
    methods = [a for a in attrs if inspect.isfunction(getattr(L, a, None)) or inspect.ismethod(getattr(L, a, None))]
    print("Llama methods:", json.dumps(methods, indent=None))
    for cand in ("create_completion","stream_complete","__call__","create_chat_completion","stream"):
        print(cand, "present" if cand in methods else "absent")
PY

