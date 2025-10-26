

grep -qxF 'source .venv/bin/activate' ~/.bashrc || echo 'source .venv/bin/activate' >> ~/.bashrc



if [ "${ENABLE_CROSS_ENCODER:-true}" = "true" ]; then
  echo "[info] ollama container (optional)"
  docker rm -f ollama 2>/dev/null || true
  docker run -d --name ollama -v ollama_models:/root/.ollama -p 11434:11434 -e OLLAMA_HOST=0.0.0.0 ollama/ollama:latest || true
fi
echo "[info] waiting a few seconds for qdrant/neo4j to become ready (adjust if needed)"
sleep 6
echo "[info] running ingestion (this will wait for embed handle if Serve not ready)"
python3 ./indexing_pipeline/ingest.py || (echo "[error] ingest.py failed" && exit 1)
echo "[info] ingestion finished"
echo "[info] you can now run inference pipeline or tests"
