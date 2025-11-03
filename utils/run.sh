ray stop --force || true && sudo rm -rf /tmp/ray/ && ray start --head && python3 infra/rayserve_onnx.py

pip install ray==2.51.0 vllm onnxruntime-gpu==1.23.2 transformers==4.57.1 numpy==2.2.6 httpx==0.25.1 typing==3.7.4.3 ray[serve,llm]==2.51.0