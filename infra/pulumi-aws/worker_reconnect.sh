set -euo pipefail
HEAD_DNS="${HEAD_DNS:-ray-head.prod.internal.example.com:6379}"
SSM_REDIS_PARAM="${SSM_REDIS_PARAM:-/ray/prod/redis_password}"
REGION="${REGION:-us-east-1}"
fetch_redis_password(){
  for i in $(seq 1 6); do
    val=$(aws ssm get-parameter --name "$SSM_REDIS_PARAM" --with-decryption --region "$REGION" --query "Parameter.Value" --output text 2>/dev/null || true)
    if [ -n "$val" ]; then
      echo "$val"
      return 0
    fi
    sleep 2
  done
  return 1
}
REDIS_PASSWORD=$(fetch_redis_password || echo "")
while true; do
  if ! pgrep -f "ray.*start" >/dev/null 2>&1; then
    ray stop || true
    PRIVATE_IP=$(curl -s http://169.254.169.254/latest/meta-data/local-ipv4 || true)
    ray start --address="$HEAD_DNS" --redis-password="$REDIS_PASSWORD" --node-ip-address="$PRIVATE_IP" || true
    sleep 2
  fi
  if ! ray status --address "auto" 2>/dev/null | grep -q "Cluster status: ALIVE"; then
    ray stop || true
    sleep 2
    continue
  fi
  sleep 5
done