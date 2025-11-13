source .venv/bin/activate
ray stop --force || true
sudo rm -rf /tmp/ray/ || true
ray start --head --port=6379 --resources='{"head":1}' --disable-usage-stats
python3 -m infra.rayserve_deployments


curl -sS http://127.0.0.1:8003/healthz | jq .
curl -sS -X POST http://127.0.0.1:8003/retrieve -H "Content-Type: application/json" -d '{"query":"What is MLOps?","stream":false}' | jq .

source infra/pulumi-aws/venv/bin/activate && pulumi up --yes

