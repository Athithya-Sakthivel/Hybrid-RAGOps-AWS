sudo rm -rf /tmp/ray/ && ray stop --force && ray start --head && python3 infra/rayserve_onnx.py



# 1) Start a dev Postgres container (persist data to ~/ragops_pgdata)
docker run -d --name ragops-postgres \
  -p 5432:5432 \
  -e POSTGRES_USER=devuser \
  -e POSTGRES_PASSWORD=devpass \
  -e POSTGRES_DB=ragops \
  -v ~/ragops_pgdata:/var/lib/postgresql/data \
  postgres:15

# 4) Export DATABASE_URL (used by the script) and run ingest
export DATABASE_URL="postgresql://devuser:devpass@127.0.0.1:5432/ragops"
python3 indexing_pipeline/ingest.py
