ray stop --force || true && sudo rm -rf /tmp/ray/ && ray start --head && python3 infra/rayserve_onnx.py
export RAY_SERVE_QUEUE_LENGTH_RESPONSE_DEADLINE_S=5.0
