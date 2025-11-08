ray stop --force || true && sudo rm -rf /tmp/ray/ && ray start --head && python3 infra/rayserve_models_cpu.py

"""
CPU-first Ray Serve deployment for:
 - ONNX embedder (ONNXEmbed)
 - ONNX cross-encoder reranker (ONNXRerank)
 - optional LLM powered by llama-cpp-python (LlamaServe)

This file contains a self-contained, production-oriented single-file deployment:
 - Early BLAS/OMP env tuning to avoid thread oversubscription.
 - One ONNXSession / Llama instance per actor process.
 - Offload blocking work (onnxruntime sess.run, tokenization, llama calls) to threadpool via run_in_executor.
 - Bounded concurrency inside actors via asyncio.Semaphore.
 - Defensive guards for outputs and env parsing.
 - Fully configurable via environment variables.
"""