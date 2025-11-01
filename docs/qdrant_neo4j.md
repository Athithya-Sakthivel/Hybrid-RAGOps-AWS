# Connections, security and backups with Weaviate

Weaviate runs as a Docker container on a single EC2. For a deterministic external endpoint this deployment uses a **pre-allocated Elastic IP (EIP)** that is associated to whichever instance the ASG has active. Ray clusters and other clients use that fixed public IP (or a DNS name pointing to it) to reach Weaviate. Vertical scaling with local NVMe-based EC2 (c8gd family) is common for RAG workloads. The storage will be lost on EC2 instance stop/termination but the ASG can immediately create a new instance and restore from the latest backup.

---

## 1) Networking & IPs — deterministic connectivity

* **Deterministic endpoint (EIP):** allocate one Elastic IP in your account and ensure instances launched by the ASG associate that EIP (for example via user-data or via an ASG lifecycle hook + Lambda). That EIP remains the public endpoint even when ASG replaces the instance.
* **Security groups:** Weaviate SG must allow inbound **TCP 8080** (HTTP/REST & GraphQL) — and **TCP 50051** if you use gRPC — only from Ray SG(s) or other trusted sources. Avoid wide public inbound rules unless intentionally required.
* **VPC/subnet:** If Ray and Weaviate are in the same VPC, prefer private routing for lower latency. Using the EIP for intra-VPC traffic works but is less efficient than private routing or an internal load balancer; consider an internal DNS or internal LB if low-latency internal traffic is critical.

---

## 2) Docker run & port binding — bind to host interfaces (use 0.0.0.0)

Because the instance private IP can change when ASG replaces instances (even if the EIP is reattached), do **not** hard-code a host private IP in the container binding. Bind the Weaviate container to all interfaces (`0.0.0.0`) and control access with security groups and network ACLs. Persist storage on the host (NVMe or EBS) and map it into the container.

Example (single-node Docker run; adjust env vars/modules as needed):

```bash
sudo mkdir -p /workspace/weaviate/data
sudo chown 1000:1000 /workspace/weaviate/data

docker run -d --name weaviate \
  -p 0.0.0.0:8080:8080 \
  -p 0.0.0.0:50051:50051 \
  -v /workspace/weaviate/data:/var/lib/weaviate \
  -e PERSISTENCE_DATA_PATH="/var/lib/weaviate" \
  -e AUTHENTICATION_ANONYMOUS_ACCESS_ENABLED="false" \
  -e DEFAULT_VECTORIZER_MODULE="none" \
  cr.weaviate.io/weaviate:latest
```

Notes:

* `-p 0.0.0.0:8080:8080` ensures the service listens on all host interfaces and is reachable regardless of the instance private IP; security groups still enforce who can reach the service.
* Storage must persist on the host at `/workspace/weaviate/data` (maps to container `/var/lib/weaviate` / `PERSISTENCE_DATA_PATH`).

---

## 3) Weaviate endpoints & credentials

* API endpoint: `http://<WEAVIATE_EIP>:8080` (or use a DNS name pointed at that EIP)

  * Meta/health/version: `GET /v1/meta` — basic health/version check.
  * Search / data operations: GraphQL or REST under `/v1/` (e.g., `POST /v1/graphql` or `/v1/objects`).

* Use authentication (API keys / OpenID Connect / other auth modules) in production and store secrets in Secrets Manager / SSM; disable anonymous access unless intentionally used.

Example health check:

```bash
curl -s http://<WEAVIATE_EIP>:8080/v1/meta
```

---

## 4) How Ray clusters connect

* **Indexing cluster (writes):** call `http://<WEAVIATE_EIP>:8080` with the proper API key/auth to create objects and vectors. Use deterministic chunk IDs for idempotency.
* **Inference cluster (writes if real time):** same endpoint for searches; use GraphQL or REST search endpoints with vector search, filters, and hybrid queries.

If Ray clusters are in the same VPC and you need lower-latency internal-only traffic, provide a private DNS or an internal load balancer for internal traffic rather than routing via the EIP.

---

## 5) Example upsert payload (batched, deterministic IDs)

Use a deterministic ID approach such as `SHA256(doc_id + chunk_index)` for object IDs. Create objects with those IDs and include `vector` if you need to override vectorizer output; otherwise let Weaviate vectorizers populate vectors. Use the batch API for bulk ingestion.

---

## 6) Backups — S3-backed Weaviate backups (preferred)

Weaviate supports built-in backup/restore to cloud storage (S3, GCS, Azure) or local filesystem (local only for testing). Use the backup API or official client to create backups written directly to S3; this is the recommended production pattern rather than relying solely on EBS snapshots.

Recommended sequence (atomic & idempotent):

1. Quiesce writers (finish indexing batch and no in-flight writes).
2. Create a backup using Weaviate’s backup API / Python client to your S3 backend.
3. Compute checksum / manifest (optional but recommended): produce a manifest that references the backup id/path and a checksum, write `latest_weaviate_backup.manifest.json.tmp` and atomically rename to `latest_weaviate_backup.manifest.json`. ASG/user-data restore logic should consult the manifest.
4. If you prefer, produce an additional tar.zst externally, but the built-in backup API writes directly to S3 and handles chunking and resumability.

If no manifest is used, the restore logic can list the backup prefix and pick the most recent backup by `LastModified` as a fallback.

---

## 7) Restore workflow (first provisioning / ASG replacement)

1. Instance boots; user-data reads `s3://$S3_BUCKET/latest_weaviate_backup.manifest.json` (if present) or lists the backup prefix to find the latest backup id/path.
2. Trigger Weaviate restore (Python client or REST) for the chosen backup id/path.
3. Start the Weaviate container (bind to `0.0.0.0`), wait for `/v1/meta` and for restore `SUCCESS`.
4. Run warm-up queries if desired; then mark instance healthy for traffic.

---

## 8) Minimal idempotent user-data script

Paste this script into your Launch Template user-data (edit variables at top). It allocates a tagged EIP if missing, associates it to the instance, installs prerequisites, runs Weaviate in Docker, and attempts a restore from S3 (manifest or latest backup).

```bash
#!/bin/bash
set -euo pipefail
REGION="us-east-1"
EIP_TAG_KEY="Name"
EIP_TAG_VAL="weaviate-eip"
S3_BUCKET="my-weaviate-backups-bucket"
S3_MANIFEST_KEY="latest_weaviate_backup.manifest.json"
BACKUP_S3_PREFIX="weaviate-backups/"
WEAVIATE_IMAGE="cr.weaviate.io/weaviate:latest"
WEAVIATE_PORT_HTTP=8080
WEAVIATE_PORT_GRPC=50051
HOST_DATA_DIR="/workspace/weaviate/data"
CONTAINER_PERSISTENCE_PATH="/var/lib/weaviate"
EXTRA_ENVS=()

if command -v apt-get >/dev/null 2>&1; then apt-get update -y; apt-get install -y unzip curl jq docker.io || true; elif command -v yum >/dev/null 2>&1; then yum makecache -y; yum install -y unzip curl jq docker || true; else echo "no pkg mgr"; fi
systemctl enable docker || true
systemctl start docker || true

if ! command -v aws >/dev/null 2>&1; then TMPDIR=$(mktemp -d); curl -s "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "${TMPDIR}/awscliv2.zip"; unzip -q "${TMPDIR}/awscliv2.zip" -d "${TMPDIR}"; "${TMPDIR}/aws/install" --update || "${TMPDIR}/aws/install"; rm -rf "${TMPDIR}"; fi

export AWS_DEFAULT_REGION="$REGION"
INSTANCE_ID=$(curl -s http://169.254.169.254/latest/meta-data/instance-id)

ALLOCATION_ID=$(aws ec2 describe-addresses --filters "Name=tag:${EIP_TAG_KEY},Values=${EIP_TAG_VAL}" --query 'Addresses[0].AllocationId' --output text || echo "None")
if [ -z "$ALLOCATION_ID" ] || [ "$ALLOCATION_ID" = "None" ] || [ "$ALLOCATION_ID" = "null" ]; then
  ALLOCATION_ID=$(aws ec2 allocate-address --domain vpc --query 'AllocationId' --output text)
  [ -n "$ALLOCATION_ID" ] && aws ec2 create-tags --resources "$ALLOCATION_ID" --tags Key="${EIP_TAG_KEY}",Value="${EIP_TAG_VAL}" || true
fi
for i in 1 2 3 4 5; do if aws ec2 associate-address --instance-id "$INSTANCE_ID" --allocation-id "$ALLOCATION_ID" --allow-reassociation --region "$REGION"; then break; else sleep 3; fi; done
EIP_PUBLIC_IP=$(aws ec2 describe-addresses --allocation-ids "$ALLOCATION_ID" --query 'Addresses[0].PublicIp' --output text --region "$REGION" || echo "")
echo "${EIP_PUBLIC_IP:-}" >/etc/weaviate_eip || true

mkdir -p "$HOST_DATA_DIR"
chown 1000:1000 "$HOST_DATA_DIR" || true
if docker ps -a --format '{{.Names}}' | grep -qw weaviate; then docker rm -f weaviate || true; fi

ENV_OPTS=(-e PERSISTENCE_DATA_PATH="$CONTAINER_PERSISTENCE_PATH" -e AUTHENTICATION_ANONYMOUS_ACCESS_ENABLED="false" -e DEFAULT_VECTORIZER_MODULE="none")
for e in "${EXTRA_ENVS[@]}"; do ENV_OPTS+=("$e"); done

docker run -d --name weaviate -p 0.0.0.0:${WEAVIATE_PORT_HTTP}:8080 -p 0.0.0.0:${WEAVIATE_PORT_GRPC}:50051 -v "${HOST_DATA_DIR}:${CONTAINER_PERSISTENCE_PATH}" "${ENV_OPTS[@]}" "$WEAVIATE_IMAGE"

for i in {1..40}; do if curl -fsS "http://127.0.0.1:${WEAVIATE_PORT_HTTP}/v1/meta" >/dev/null 2>&1; then break; else sleep 5; fi; done

BACKUP_ID=""
if [ -n "$S3_BUCKET" ]; then
  if aws s3api head-object --bucket "$S3_BUCKET" --key "$S3_MANIFEST_KEY" >/dev/null 2>&1; then
    aws s3 cp "s3://$S3_BUCKET/$S3_MANIFEST_KEY" /tmp/weaviate_manifest.json --region "$REGION" || true
    BACKUP_ID=$(jq -r '.backup_id // .id // .path // empty' /tmp/weaviate_manifest.json || true)
    if [ -z "$BACKUP_ID" ]; then PATHVAL=$(jq -r '.path // empty' /tmp/weaviate_manifest.json || true); [ -n "$PATHVAL" ] && BACKUP_ID=$(basename "$PATHVAL"); fi
  fi
  if [ -z "$BACKUP_ID" ]; then
    LATEST_KEY=$(aws s3api list-objects-v2 --bucket "$S3_BUCKET" --prefix "$BACKUP_S3_PREFIX" --query 'sort_by(Contents,&LastModified)[-1].Key' --output text --region "$REGION" || echo "None")
    if [ -n "$LATEST_KEY" ] && [ "$LATEST_KEY" != "None" ]; then BACKUP_ID=$(basename "$LATEST_KEY"); fi
  fi
fi

if [ -n "${BACKUP_ID:-}" ]; then
  for i in {1..20}; do if curl -fsS "http://127.0.0.1:${WEAVIATE_PORT_HTTP}/v1/meta" >/dev/null 2>&1; then break; else sleep 5; fi; done
  curl -fsS -X POST "http://127.0.0.1:${WEAVIATE_PORT_HTTP}/v1/backups/s3/${BACKUP_ID}/restore" -H "Content-Type: application/json" -d '{"wait_for_completion":true}' || true
fi

exit 0
```

---

## 11) IAM permissions for the instance role

Attach an instance role (instance profile) to your Launch Template/ASG with at least:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Effect": "Allow", "Action": ["ec2:DescribeAddresses","ec2:AssociateAddress"], "Resource": "*" },
    { "Effect": "Allow", "Action": ["ec2:AllocateAddress","ec2:CreateTags"], "Resource": "*" },
    { "Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": ["arn:aws:s3:::YOUR_BUCKET"] },
    { "Effect": "Allow", "Action": ["s3:GetObject"], "Resource": ["arn:aws:s3:::YOUR_BUCKET/*"] }
  ]
}
```

`AllocateAddress`/`CreateTags` are needed only if you want the script to allocate the EIP on first run. If you pre-allocate the EIP manually, you may remove those actions and only allow `DescribeAddresses`/`AssociateAddress`.

---

## 12) Quick checklist

* [ ] EIP pre-allocated or allow allocation in the instance role; script will tag and reuse it.
* [ ] Launch Template has the instance profile (IAM role) attached.
* [ ] Docker Weaviate launched: bound to `0.0.0.0` and mounted host storage `/workspace/weaviate/data`.
* [ ] Security Group inbound allows only trusted sources to 8080 (and 50051 if needed).
* [ ] Indexing job uses deterministic IDs and emits success marker for backup trigger.
* [ ] Backup automation: Weaviate backup → S3 → optional manifest + checksum in S3.
* [ ] Restore drill scheduled to verify end-to-end S3 restore works.

---