#!/usr/bin/env bash
# pulumi_setup.sh - idempotent create/delete helper for infra/pulumi-aws
set -euo pipefail

# Prevent sourcing
if [ "${BASH_SOURCE[0]}" != "$0" ]; then
  echo "ERROR: do not source this file. Run it: bash $0" >&2
  return 1 2>/dev/null || exit 1
fi

# ---------------------------
# Project Configuration (defaults)
# ---------------------------
export PROJECT_DIR="${PROJECT_DIR:-infra/pulumi-aws}"
export VENV_DIR="${VENV_DIR:-${PROJECT_DIR}/venv}"
export REQ_FILE="${REQ_FILE:-${PROJECT_DIR}/requirements.txt}"

export AWS_REGION="${AWS_REGION:-ap-south-1}"
export PULUMI_S3_BUCKET="${PULUMI_S3_BUCKET:-e2e-rag-42}"
export S3_BUCKET="${S3_BUCKET:-${PULUMI_S3_BUCKET}}"
export S3_PREFIX="${S3_PREFIX:-pulumi/}"
export DDB_TABLE="${DDB_TABLE:-pulumi-state-locks}"
export PULUMI_STACK="${PULUMI_STACK:-prod}"
export STACK="${STACK:-${PULUMI_STACK}}"
export PULUMI_CONFIG_PASSPHRASE="${PULUMI_CONFIG_PASSPHRASE:-"password"}"
export FORCE_DELETE="${FORCE_DELETE:-true}"

export PULUMI_ORG="${PULUMI_ORG:-}"
export PULUMI_IAM_USER="${PULUMI_IAM_USER:-}"
export PULUMI_CREDS_FILE="${PULUMI_CREDS_FILE:-/tmp/pulumi-ci-credentials.json}"
export POLICY_NAME="${POLICY_NAME:-PulumiStateAccessPolicy}"

# Optional infra-specific env defaults (override in environment or export.sh)
export MULTI_AZ_DEPLOYMENT="${MULTI_AZ_DEPLOYMENT:-false}"
export CREATE_VPC_ENDPOINTS="${CREATE_VPC_ENDPOINTS:-false}"
export NO_NAT="${NO_NAT:-false}"
export PUBLIC_SUBNET_CIDRS="${PUBLIC_SUBNET_CIDRS:-10.0.1.0/24,10.0.2.0/24}"
export PRIVATE_SUBNET_CIDRS="${PRIVATE_SUBNET_CIDRS:-10.0.11.0/24,10.0.12.0/24}"

# domain/DNS defaults
export DOMAIN="${DOMAIN:-}"
export HOSTED_ZONE_ID="${HOSTED_ZONE_ID:-}"
export PRIVATE_HOSTED_ZONE_NAME="${PRIVATE_HOSTED_ZONE_NAME:-internal}"
export PRIVATE_HOSTED_ZONE_ID="${PRIVATE_HOSTED_ZONE_ID:-}"

# AMI / instance defaults (must be set to real values in prod)
export HEAD_AMI="${HEAD_AMI:-}"
export HEAD_INSTANCE_TYPE="${HEAD_INSTANCE_TYPE:-m5.large}"
export RAY_HEAD_INSTANCE_PROFILE="${RAY_HEAD_INSTANCE_PROFILE:-}"
export RAY_CPU_AMI="${RAY_CPU_AMI:-}"
export RAY_CPU_INSTANCE="${RAY_CPU_INSTANCE:-m5.xlarge}"
export RAY_CPU_INSTANCE_PROFILE="${RAY_CPU_INSTANCE_PROFILE:-}"
export KEY_NAME="${KEY_NAME:-}"

# Secrets / ssm
export REDIS_SSM_PARAM="${REDIS_SSM_PARAM:-/ray/prod/redis_password}"

# Additional optional toggles
export ENABLE_COGNITO="${ENABLE_COGNITO:-false}"
export ENABLE_WAF="${ENABLE_WAF:-false}"
export ENABLE_ELASTICACHE="${ENABLE_ELASTICACHE:-false}"
export ENABLE_PROMETHEUS="${ENABLE_PROMETHEUS:-false}"
export ENABLE_VPC_ENDPOINTS="${ENABLE_VPC_ENDPOINTS:-false}"

# Additional buckets (defaults may be overridden)
export PULUMI_STATE_BUCKET="${PULUMI_STATE_BUCKET:-pulumi-state-${STACK}-${AWS_REGION}}"
export AUTOSCALER_BUCKET_NAME="${AUTOSCALER_BUCKET_NAME:-ray-autoscaler-${STACK}-${AWS_REGION}}"
export MODELS_S3_BUCKET="${MODELS_S3_BUCKET:-ray-models-${STACK}-${AWS_REGION}}"

# ---------------------------
# Helpers
# ---------------------------
abs_path() {
  local p="$1"
  if command -v realpath >/dev/null 2>&1; then
    realpath -m "$p"
  elif command -v readlink >/dev/null 2>&1; then
    readlink -f "$p" || python3 -c "import os,sys; print(os.path.abspath(sys.argv[1]))" "$p"
  else
    python3 -c "import os,sys; print(os.path.abspath(sys.argv[1]))" "$p"
  fi
}
PROJECT_DIR="$(abs_path "$PROJECT_DIR")"
VENV_DIR="$(abs_path "$VENV_DIR")"
REQ_FILE="$(abs_path "$REQ_FILE")"

mkdir -p "$PROJECT_DIR"

out_json="${PROJECT_DIR}/pulumi-outputs.json"
if [ ! -f "$out_json" ]; then
  printf '{}' >"$out_json" || true
fi

out_exports="${PROJECT_DIR}/pulumi-exports.sh"
if [ ! -f "$out_exports" ]; then
  printf '#!/usr/bin/env bash\n# pulumi exports placeholder\n' >"$out_exports" || true
  chmod +x "$out_exports" || true
fi

out_setup="${PROJECT_DIR}/pulumi_setup.sh"
if [ ! -f "$out_setup" ]; then
  cat >"$out_setup" <<'SH'
#!/usr/bin/env bash
# project-level helper placeholder created by infra/pulumi-aws/pulumi_setup.sh
echo "This is a placeholder helper. It does not modify project source."
SH
  chmod +x "$out_setup" || true
fi

prog="$(basename "$0")"
usage() {
  cat <<EOF
Usage: $prog [--create|--delete] [--force] [--preview] [--preview-and-up] [--a] [--b] [--c] [--d] [--e] [-h|--help]
  --create            create backend + venv + pulumi up (or preview)
  --delete            destroy stack and remove backend artifacts
  --a                 include module A (a_prereqs_networking)
  --b                 include module B (b_identity_alb_iam)
  --c                 include module C (c_ray_head)
  --d                 include module D (d_ray_workers)
  --e                 include extras (ElastiCache/Prometheus/WAF/etc) - optional/placeholder
  --force             with --delete also delete entire S3 bucket
  --preview           run pulumi preview only (no up)
  --preview-and-up    run preview and, if successful, pulumi up
Notes:
  - If no --a..--e flags are passed with --create, the script enables A..D by default.
EOF
}

MODE="" FORCE_FLAG=false PREVIEW=false PREVIEW_AND_UP=false
FLAG_A=false; FLAG_B=false; FLAG_C=false; FLAG_D=false; FLAG_E=false

while [ $# -gt 0 ]; do
  case "$1" in
    --create) MODE="create"; shift;;
    --delete) MODE="delete"; shift;;
    --force) FORCE_FLAG=true; shift;;
    --preview) PREVIEW=true; shift;;
    --preview-and-up) PREVIEW_AND_UP=true; shift;;
    -h|--help) usage; exit 0;;
    --a) FLAG_A=true; shift;;
    --b) FLAG_B=true; shift;;
    --c) FLAG_C=true; shift;;
    --d) FLAG_D=true; shift;;
    --e) FLAG_E=true; shift;;
    *) echo "Unknown arg: $1" >&2; usage; exit 2;;
  esac
done
[ -n "$MODE" ] || { echo "ERROR: must pass --create or --delete" >&2; usage; exit 2; }

log() { printf '[%s] %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*"; }
die() { echo "ERROR: $*" >&2; exit "${2:-1}"; }
require_cmd() { command -v "$1" >/dev/null 2>&1 || die "required command '$1' not found" 10; }

TMPS=()
cleanup() { for f in "${TMPS[@]:-}"; do [ -f "$f" ] && rm -f "$f"; done; }
trap cleanup EXIT

retry() {
  local tries=${1:-5}; shift || true
  local delay=${1:-1}; shift || true
  local i=0 rc=0
  while [ $i -lt $tries ]; do
    set +e
    "$@"
    rc=$?
    set -e
    [ $rc -eq 0 ] && return 0
    i=$((i+1))
    sleep $delay
    delay=$((delay * 2))
  done
  return $rc
}

require_cmd aws
require_cmd python3
# jq optional; used for JSON handling when present
if ! command -v jq >/dev/null 2>&1; then
  log "note: jq not found; script will fall back to python for JSON parsing"
fi

if ! aws sts get-caller-identity >/dev/null 2>&1; then
  die "AWS credentials not configured or not working (aws sts get-caller-identity failed)" 20
fi

# ---------------------------
# S3 / DDB / IAM helpers
# ---------------------------
create_bucket_if_missing() {
  local bucket="$1"
  log "s3: ensure bucket exists: ${bucket} (region=${AWS_REGION})"
  if retry 6 1 aws s3api head-bucket --bucket "$bucket" >/dev/null 2>&1; then
    log "s3: bucket exists"
  else
    if [ "$AWS_REGION" = "us-east-1" ]; then
      aws s3api create-bucket --bucket "$bucket" >/dev/null 2>&1 || log "s3: create returned non-zero"
    else
      aws s3api create-bucket --bucket "$bucket" --create-bucket-configuration LocationConstraint="$AWS_REGION" >/dev/null 2>&1 || log "s3: create returned non-zero"
    fi
    retry 8 2 aws s3api head-bucket --bucket "$bucket" >/dev/null 2>&1 || log "s3: head-bucket still failing (continuing)"
  fi
  aws s3api put-bucket-versioning --bucket "$bucket" --versioning-configuration Status=Enabled >/dev/null 2>&1 || true
  aws s3api put-bucket-encryption --bucket "$bucket" --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}' >/dev/null 2>&1 || true
  aws s3api put-bucket-lifecycle-configuration --bucket "$bucket" --lifecycle-configuration "{\"Rules\":[{\"ID\":\"pulumi-noncurrent-expire\",\"Prefix\":\"${S3_PREFIX}\",\"Status\":\"Enabled\",\"NoncurrentVersionExpiration\":{\"NoncurrentDays\":365}}]}" >/dev/null 2>&1 || true
  log "s3: bucket prepared (prefix=${S3_PREFIX})"
}

delete_s3_objects() {
  local bucket="$1" prefix="${2:-}"
  log "s3-delete: deleting objects in s3://${bucket}/${prefix}"
  while :; do
    local rv count objs tmp
    if [ -n "$prefix" ]; then
      rv="$(aws s3api list-object-versions --bucket "$bucket" --prefix "$prefix" --output json 2>/dev/null || echo '{}')"
    else
      rv="$(aws s3api list-object-versions --bucket "$bucket" --output json 2>/dev/null || echo '{}')"
    fi
    if command -v jq >/dev/null 2>&1; then
      count=$(jq -r '[.Versions[], .DeleteMarkers[]] | length' <<<"$rv" 2>/dev/null || echo 0)
    else
      count=$(python3 - <<PY
import sys,json
try:
  r=json.load(sys.stdin)
  c=sum(len(r.get(k,[])) for k in ("Versions","DeleteMarkers"))
  print(c)
except Exception:
  print(0)
PY
<<<"$rv")
    fi
    [ -z "$count" ] || [ "$count" = "0" ] && break
    if command -v jq >/dev/null 2>&1; then
      objs=$(jq -c '[.Versions[]?, .DeleteMarkers[]?] | map({Key:.Key,VersionId:.VersionId})' <<<"$rv")
    else
      objs=$(python3 - <<PY
import sys,json
r=json.load(sys.stdin)
arr=[]
for k in ("Versions","DeleteMarkers"):
  for it in r.get(k,[]):
    arr.append({"Key":it.get("Key"), "VersionId": it.get("VersionId")})
print(json.dumps(arr))
PY
<<<"$rv")
    fi
    tmp="$(mktemp)"; TMPS+=("$tmp")
    printf '{"Objects":%s}' "$objs" >"$tmp"
    aws s3api delete-objects --bucket "$bucket" --delete "file://$tmp" >/dev/null 2>&1 || true
    rm -f "$tmp" || true
    sleep 1
  done
  log "s3-delete: done for s3://${bucket}/${prefix}"
}

empty_and_delete_bucket_force() {
  local bucket="$1"
  log "s3-delete-all: force-empty & delete s3://${bucket}"
  delete_s3_objects "$bucket" ""
  aws s3api delete-bucket --bucket "$bucket" --region "$AWS_REGION" >/dev/null 2>&1 || true
  log "s3-delete-all: bucket delete attempted"
}

create_dynamodb_if_missing() {
  local table="$1"
  log "ddb: ensure table ${table}"
  if aws dynamodb describe-table --table-name "$table" >/dev/null 2>&1; then
    log "ddb: exists"
  else
    set +e
    aws dynamodb create-table --table-name "$table" \
      --attribute-definitions AttributeName=LockID,AttributeType=S \
      --key-schema AttributeName=LockID,KeyType=HASH \
      --billing-mode PAY_PER_REQUEST --region "$AWS_REGION" >/dev/null 2>&1
    rc=$?
    set -e
    if [ "$rc" -eq 0 ]; then
      aws dynamodb wait table-exists --table-name "$table" --region "$AWS_REGION" >/dev/null 2>&1 || true
      log "ddb: created and ACTIVE"
    else
      log "ddb: create returned non-zero (continuing)"
    fi
  fi
  aws dynamodb update-time-to-live --table-name "$table" --time-to-live-specification "Enabled=true,AttributeName=Expires" >/dev/null 2>&1 || true
}

delete_dynamodb_table_if_exists() {
  local table="$1"
  if aws dynamodb describe-table --table-name "$table" >/dev/null 2>&1; then
    aws dynamodb delete-table --table-name "$table" --region "$AWS_REGION" >/dev/null 2>&1 || true
    aws dynamodb wait table-not-exists --table-name "$table" --region "$AWS_REGION" || true
    log "ddb-delete: table deleted or attempted"
  else
    log "ddb-delete: table not found; skipping"
  fi
}

get_account_id() { aws sts get-caller-identity --query Account --output text 2>/dev/null || true; }

wait_for_policy_arn() {
  local name="$1" tries=8 delay=1 arn=""
  for i in $(seq 1 $tries); do
    arn="$(aws iam list-policies --scope Local --query "Policies[?PolicyName=='${name}'].Arn" --output text 2>/dev/null || true)"
    [ -n "$arn" ] && { echo "$arn"; return 0; }
    sleep "$delay"
    delay=$((delay * 2))
  done
  return 1
}

ensure_policy() {
  local bucket="$1" table="$2" name="$3"
  log "iam: ensure policy ${name}"
  local existing
  existing="$(aws iam list-policies --scope Local --query "Policies[?PolicyName=='${name}'].Arn" --output text || true)"
  if [ -n "$existing" ]; then
    log "iam: policy exists $existing"
    echo "$existing"; return 0
  fi
  local acct; acct="$(get_account_id || true)"
  local tmp
  tmp="$(mktemp)"; TMPS+=("$tmp")
  cat >"$tmp" <<JSON
{
  "Version":"2012-10-17",
  "Statement":[
    {
      "Effect":"Allow",
      "Action":["s3:GetObject","s3:PutObject","s3:DeleteObject","s3:ListBucket","s3:GetBucketVersioning","s3:PutBucketVersioning"],
      "Resource":["arn:aws:s3:::${bucket}","arn:aws:s3:::${bucket}/*"]
    },
    {
      "Effect":"Allow",
      "Action":["dynamodb:GetItem","dynamodb:PutItem","dynamodb:DeleteItem","dynamodb:UpdateItem","dynamodb:Query","dynamodb:Scan","dynamodb:ConditionCheckItem"],
      "Resource":["arn:aws:dynamodb:${AWS_REGION}:${acct}:table/${table}"]
    }
  ]
}
JSON
  aws iam create-policy --policy-name "$name" --policy-document "file://$tmp" >/dev/null 2>&1 || true
  local arn
  arn="$(wait_for_policy_arn "$name" || true)"
  log "iam: policy ARN: ${arn:-not-found}"
  echo "$arn"
}

create_iam_user_if_requested() {
  local user="$1" policy_arn="$2" creds_file="$3"
  if [ -z "$user" ]; then log "iam: no IAM user requested; skipping"; return 0; fi
  log "iam: ensure user $user"
  aws iam create-user --user-name "$user" >/dev/null 2>&1 || true
  if [ -n "$policy_arn" ]; then aws iam attach-user-policy --user-name "$user" --policy-arn "$policy_arn" >/dev/null 2>&1 || true; fi
  if [ -z "$(aws iam list-access-keys --user-name "$user" --query 'AccessKeyMetadata[].AccessKeyId' --output text || true)" ]; then
    aws iam create-access-key --user-name "$user" >"$creds_file"
    chmod 600 "$creds_file" || true
    log "iam: created access key at $creds_file"
  else
    log "iam: user has access keys; not creating new one"
  fi
}

delete_policy_and_user_idempotent() {
  local policy_name="$1" user="$2"
  local existing
  existing="$(aws iam list-policies --scope Local --query "Policies[?PolicyName=='${policy_name}'].Arn" --output text || true)"
  if [ -n "$existing" ]; then
    for u in $(aws iam list-entities-for-policy --policy-arn "$existing" --query 'PolicyUsers[].UserName' --output text || true); do aws iam detach-user-policy --user-name "$u" --policy-arn "$existing" || true; done
    for r in $(aws iam list-entities-for-policy --policy-arn "$existing" --query 'PolicyRoles[].RoleName' --output text || true); do aws iam.detach-role-policy --role-name "$r" --policy-arn "$existing" || true; done
    for v in $(aws iam list-policy-versions --policy-arn "$existing" --query 'Versions[?IsDefaultVersion==`false`].VersionId' --output text || true); do aws iam delete-policy-version --policy-arn "$existing" --version-id "$v" || true; done
    aws iam delete-policy --policy-arn "$existing" || true
    log "iam-delete: policy delete attempted"
  else
    log "iam-delete: policy not found; skipping"
  fi
  if [ -n "$user" ]; then
    if aws iam get-user --user-name "$user" >/dev/null 2>&1; then
      for k in $(aws iam list-access-keys --user-name "$user" --query 'AccessKeyMetadata[].AccessKeyId' --output text || true); do aws iam delete-access-key --user-name "$user" --access-key-id "$k" || true; done
      for a in $(aws iam list-attached-user-policies --user-name "$user" --query 'AttachedPolicies[].PolicyArn' --output text || true); do aws iam detach-user-policy --user-name "$user" --policy-arn "$a" || true; done
      for ip in $(aws iam list-user-policies --user-name "$user" --query 'PolicyNames[]' --output text || true); do aws iam delete-user-policy --user-name "$user" --policy-name "$ip" || true; done
      aws iam delete-user --user-name "$user" || true
      log "iam-delete: user delete attempted"
    else
      log "iam-delete: user not found; skipping"
    fi
  fi
}

# ---------------------------
# Pulumi helpers + venv bootstrap
# ---------------------------
ensure_pulumi_cli() {
  if command -v pulumi >/dev/null 2>&1; then return 0; fi
  if [ -n "${PULUMI_BINARY_PATH:-}" ] && [ -x "${PULUMI_BINARY_PATH}" ]; then
    export PATH="$(dirname "$PULUMI_BINARY_PATH"):$PATH"
  fi
  if ! command -v pulumi >/dev/null 2>&1; then
    if command -v curl >/dev/null 2>&1; then
      curl -fsSL https://get.pulumi.com | sh >/dev/null 2>&1 || true
      export PATH="$HOME/.pulumi/bin:$PATH"
    else
      die "pulumi CLI not found and cannot auto-install (curl missing)" 11
    fi
  fi
  for i in 1 3; do
    if pulumi version >/dev/null 2>&1; then
      log "pulumi: $(pulumi version)"
      return 0
    fi
    sleep 1
  done
  die "pulumi not responding after install" 11
}

# Ensure pip exists in given python binary by running ensurepip if needed
ensure_pip_for_python() {
  local py="$1"
  # test pip module
  set +e
  "$py" -m pip --version >/dev/null 2>&1
  rc=$?
  set -e
  if [ $rc -ne 0 ]; then
    log "pip not present for $py; attempting ensurepip bootstrap"
    set +e
    "$py" -m ensurepip --upgrade >/dev/null 2>&1 || true
    "$py" -m pip install --upgrade pip setuptools wheel >/dev/null 2>&1 || true
    set -e
  fi
}

# create venv if missing; ALWAYS use venv python for pip
create_venv_and_install() {
  mkdir -p "$(dirname "$VENV_DIR")"
  if [ ! -d "$VENV_DIR" ]; then
    log "venv: creating virtualenv at $VENV_DIR"
    python3 -m venv "$VENV_DIR"
  else
    log "venv: using existing virtualenv at $VENV_DIR"
  fi

  # choose python executable inside venv
  if [ -x "${VENV_DIR}/bin/python3" ]; then
    VENV_PYTHON="${VENV_DIR}/bin/python3"
  elif [ -x "${VENV_DIR}/bin/python" ]; then
    VENV_PYTHON="${VENV_DIR}/bin/python"
  else
    die "venv python not found at ${VENV_DIR}/bin/python{,3}"
  fi
  export PULUMI_PYTHON_CMD="${VENV_PYTHON}"

  log "venv: ensuring pip exists for ${VENV_PYTHON}"
  ensure_pip_for_python "${VENV_PYTHON}"

  log "venv: installing packages using ${VENV_PYTHON} (requirements: ${REQ_FILE})"
  if [ -f "$REQ_FILE" ]; then
    "${VENV_PYTHON}" -m pip install -r "$REQ_FILE"
  else
    "${VENV_PYTHON}" -m pip install --upgrade pip setuptools wheel >/dev/null 2>&1 || true
    # install essential packages
    "${VENV_PYTHON}" -m pip install pulumi pulumi-aws boto3 awscli >/dev/null 2>&1 || true
  fi
  log "venv: ready (python: ${VENV_PYTHON})"
}

activate_venv_if_exists() {
  if [ -f "${VENV_DIR}/bin/activate" ]; then
    # shellcheck disable=SC1090
    source "${VENV_DIR}/bin/activate"
    if [ -x "${VENV_DIR}/bin/python3" ]; then
      export PULUMI_PYTHON_CMD="${VENV_DIR}/bin/python3"
    elif [ -x "${VENV_DIR}/bin/python" ]; then
      export PULUMI_PYTHON_CMD="${VENV_DIR}/bin/python"
    fi
    log "venv: activated and PULUMI_PYTHON_CMD=${PULUMI_PYTHON_CMD}"
  else
    log "venv: not present; continuing without activation"
  fi
}

find_pulumi_entrypoint() {
  local pd="$PROJECT_DIR"
  local pd_name
  pd_name="$(awk -F: '/^name[[:space:]]*:/ {gsub(/^[ \t]+|[ \t]+$/,"",$2); print $2; exit}' "${PROJECT_DIR}/Pulumi.yaml" 2>/dev/null || true)"
  local candidates=(
    "${pd}/__main__.py"
    "${pd}/${pd_name}/__main__.py"
    "${pd}/${pd_name}.py"
    "${pd}/main.py"
    "${pd}/__init__.py"
  )
  for f in "${candidates[@]}"; do
    [ -f "$f" ] && { printf '%s' "$f"; return 0; }
  done
  return 1
}

ensure_valid_entrypoint_exists() {
  if ep="$(find_pulumi_entrypoint)"; then
    # compile check using venv Python if available
    local pycmd="${PULUMI_PYTHON_CMD:-python3}"
    if ! "$pycmd" -m py_compile "$ep" >/dev/null 2>&1; then
      die "Pulumi entrypoint '$ep' exists but contains syntax errors. Fix it and re-run."
    fi
    log "pulumi: entrypoint found and valid: $ep"
    return 0
  fi
  die "__main__.py or other Python Pulumi entrypoint missing in ${PROJECT_DIR}; this script will not create or modify __main__.py. Add a valid Pulumi program and re-run."
}

get_pulumi_project_name() {
  local pd="${PROJECT_DIR}/Pulumi.yaml"
  if [ -f "$pd" ]; then awk -F: '/^name[[:space:]]*:/ {gsub(/^[ \t]+|[ \t]+$/,"",$2); print $2; exit}' "$pd" || true; fi
}

verify_stack_selected() {
  if pulumi stack >/dev/null 2>&1; then return 0; fi
  return 1
}

pulumi_select_or_init_stack() {
  local stack="$1"
  # ensure pulumi CLI installed
  ensure_pulumi_cli
  export PULUMI_PYTHON_CMD="${PULUMI_PYTHON_CMD:-${VENV_DIR}/bin/python3}"
  for attempt in 1 6; do
    if pulumi stack select "$stack" >/dev/null 2>&1; then
      log "pulumi: selected existing stack '$stack'"
      return 0
    fi
    sleep $((attempt))
  done

  PROJECT_NAME="$(get_pulumi_project_name || true)"
  candidates=("$stack")
  [ -n "${PROJECT_NAME:-}" ] && candidates+=("${PROJECT_NAME}/${stack}")
  [ -n "${PULUMI_ORG:-}" ] && [ -n "${PROJECT_NAME:-}" ] && candidates+=("${PULUMI_ORG}/${PROJECT_NAME}/${stack}")

  for c in "${candidates[@]}"; do
    [ -z "$c" ] && continue
    for attempt in 1 4; do
      log "pulumi: trying stack init '$c' (attempt $attempt)"
      set +e
      pulumi stack init "$c" >/dev/null 2>&1 || true
      rc=$?
      set -e
      if [ $rc -eq 0 ]; then
        pulumi stack select "$c" >/dev/null 2>&1 || true
        if verify_stack_selected; then
          log "pulumi: created and selected '$c'"
          return 0
        fi
      fi
      sleep $((attempt))
    done
  done

  # fallback to pulumi new
  log "pulumi: fallback -> attempting non-interactive 'pulumi new python --yes --force'"
  ensure_pulumi_cli
  set +e
  pulumi new python --yes --force >/dev/null 2>&1 || true
  rc=$?
  set -e
  if [ $rc -ne 0 ]; then
    die "unable to select or init pulumi stack '${stack}' and pulumi new failed"
  fi
  if pulumi stack init "$stack" >/dev/null 2>&1; then
    pulumi stack select "$stack" >/dev/null 2>&1 || true
    verify_stack_selected || die "fallback created stack but verification failed"
    log "pulumi: fallback created and selected stack '$stack'"
    return 0
  fi
  die "unable to select or init pulumi stack '${stack}'"
}

pulumi_preview_and_capture() {
  local logdir="${PROJECT_DIR}/.pulumi-logs"; mkdir -p "$logdir"
  local logf="${logdir}/pulumi-preview-$(date -u +%s).log"
  : >"$logf"
  export PULUMI_PYTHON_CMD="${PULUMI_PYTHON_CMD:-${VENV_DIR}/bin/python3}"
  if pulumi preview --diff --non-interactive >"$logf" 2>&1; then
    log "pulumi: preview succeeded (log: $logf)"; return 0
  else
    log "pulumi: preview failed; last 200 lines of $logf" >&2
    tail -n 200 "$logf" >&2 || true
    return 2
  fi
}

pulumi_up_and_capture() {
  local logdir="${PROJECT_DIR}/.pulumi-logs"; mkdir -p "$logdir"
  local logf="${logdir}/pulumi-up-$(date -u +%s).log"
  : >"$logf"
  export PULUMI_PYTHON_CMD="${PULUMI_PYTHON_CMD:-${VENV_DIR}/bin/python3}"
  if pulumi up --yes >"$logf" 2>&1; then
    log "pulumi: up succeeded (log: $logf)"; return 0
  else
    log "pulumi: up failed; last 200 lines of $logf" >&2
    tail -n 200 "$logf" >&2 || true
    return 3
  fi
}

write_stack_outputs() {
  local out_json="${PROJECT_DIR}/pulumi-outputs.json"
  local out_sh="${PROJECT_DIR}/pulumi-exports.sh"
  mkdir -p "${PROJECT_DIR}/.pulumi-logs" || true
  export PULUMI_PYTHON_CMD="${PULUMI_PYTHON_CMD:-${VENV_DIR}/bin/python3}"
  set +e
  pulumi stack output --json >"${out_json}.tmp" 2>/dev/null
  rc=$?
  set -e
  if [ $rc -ne 0 ]; then
    log "pulumi: could not get stack outputs (rc=${rc}); writing empty outputs file"
    printf '{}' >"${out_json}.tmp" || true
  fi
  mv "${out_json}.tmp" "$out_json" || true

  if [ -s "$out_json" ] && command -v python3 >/dev/null 2>&1; then
    python3 - "$out_json" "$out_sh" <<'PY'
import json,sys,os
json_fn = sys.argv[1]
out_fn = sys.argv[2]
try:
    with open(json_fn) as f:
        data = json.load(f)
except Exception:
    data = {}
tmp = out_fn + ".tmp"
with open(tmp, "w") as o:
    o.write("#!/usr/bin/env bash\n# pulumi exports generated\n")
    for k, v in data.items():
        key = "PULUMI_" + k.upper().replace("-", "_")
        if isinstance(v, str):
            val = v
        else:
            import json as _j
            val = _j.dumps(v)
        val = val.replace('"', '\\"')
        o.write(f'export {key}="{val}"\n')
os.replace(tmp, out_fn)
PY
  else
    printf '#!/usr/bin/env bash\n# pulumi exports placeholder\n' >"$out_sh" || true
  fi
  chmod +x "$out_sh" >/dev/null 2>&1 || true
  log "pulumi: outputs written to $out_json and $out_sh"
}

# New helper: set Pulumi config keys from environment variables
set_pulumi_config_from_envs() {
  declare -A cfgmap=(
    ["AWS_REGION"]="aws:region"
    ["VPC_ID"]="vpcId"
    ["PUBLIC_SUBNET_IDS"]="publicSubnetIds"
    ["PRIVATE_SUBNET_IDS"]="privateSubnetIds"
    ["ALB_SECURITY_GROUP_ID"]="albSecurityGroupId"
    ["HEAD_SECURITY_GROUP_ID"]="headSecurityGroupId"
    ["WORKER_SECURITY_GROUP_ID"]="workerSecurityGroupId"
    ["HEAD_AMI"]="headAmi"
    ["HEAD_INSTANCE_TYPE"]="headInstanceType"
    ["RAY_HEAD_INSTANCE_PROFILE"]="rayHeadInstanceProfile"
    ["RAY_CPU_AMI"]="rayCpuAmi"
    ["RAY_CPU_INSTANCE"]="rayCpuInstance"
    ["RAY_CPU_INSTANCE_PROFILE"]="rayCpuInstanceProfile"
    ["KEY_NAME"]="keyName"
    ["REDIS_SSM_PARAM"]="redisSsmParam"
    ["AUTOSCALER_BUCKET_NAME"]="autoscalerBucket"
    ["MODELS_S3_BUCKET"]="modelsBucket"
    ["DOMAIN"]="domain"
    ["HOSTED_ZONE_ID"]="hostedZoneId"
    ["PRIVATE_HOSTED_ZONE_ID"]="privateHostedZoneId"
    ["PRIVATE_HOSTED_ZONE_NAME"]="privateHostedZoneName"
    ["ENABLE_COGNITO"]="enableCognito"
    ["ENABLE_WAF"]="enableWaf"
    ["ENABLE_ELASTICACHE"]="enableElastiCache"
    ["ENABLE_PROMETHEUS"]="enablePrometheus"
    ["ENABLE_VPC_ENDPOINTS"]="createVpcEndpoints"
    ["MULTI_AZ_DEPLOYMENT"]="multiAz"
    ["NO_NAT"]="noNat"
    ["ENABLE_RATE_LIMITER"]="enableRateLimiter"
    ["ENABLE_GPU"]="enableGpu"
    ["MODE"]="mode"
    ["ENABLE_CROSS_ENCODER"]="enableCrossEncoder"
    ["PULUMI_STATE_BUCKET"]="pulumiStateBucket"
  )

  for envvar in "${!cfgmap[@]}"; do
    val="${!envvar-}"
    pulumi_key="${cfgmap[$envvar]}"
    if [ -n "$val" ]; then
      log "pulumi config: setting ${pulumi_key} = (from env ${envvar})"
      pulumi config set "${pulumi_key}" "$val" >/dev/null 2>&1 || log "pulumi config set ${pulumi_key} returned non-zero (continuing)"
    fi
  done
}

pulumi_login_and_run() {
  ensure_pulumi_cli
  export AWS_DYNAMODB_LOCK_TABLE="$DDB_TABLE"
  [ -n "$PULUMI_CONFIG_PASSPHRASE" ] && export PULUMI_CONFIG_PASSPHRASE
  export PULUMI_PYTHON_CMD="${PULUMI_PYTHON_CMD:-${VENV_DIR}/bin/python3}"
  log "pulumi: login s3://${S3_BUCKET}/${S3_PREFIX} (PULUMI_PYTHON_CMD=${PULUMI_PYTHON_CMD})"
  # login may fail if not configured; allow script to continue and let pulumi commands report errors later
  set +e
  pulumi login "s3://${S3_BUCKET}/${S3_PREFIX}" >/dev/null 2>&1
  rc=$?
  set -e
  if [ $rc -ne 0 ]; then
    log "pulumi: login returned non-zero (continuing); ensure S3 backend is reachable and bucket exists"
  fi

  if [ ! -d "$PROJECT_DIR" ]; then die "project dir $PROJECT_DIR not found" 13; fi
  pushd "$PROJECT_DIR" >/dev/null || exit 1
  ensure_valid_entrypoint_exists
  activate_venv_if_exists
  pulumi_select_or_init_stack "$STACK"

  pulumi config set aws:region "$AWS_REGION" >/dev/null 2>&1 || true
  set_pulumi_config_from_envs

  export ENABLE_FILE_A="${ENABLE_FILE_A:-false}"
  export ENABLE_FILE_B="${ENABLE_FILE_B:-false}"
  export ENABLE_FILE_C="${ENABLE_FILE_C:-false}"
  export ENABLE_FILE_D="${ENABLE_FILE_D:-false}"
  export ENABLE_FILE_E="${ENABLE_FILE_E:-false}"

  local up_rc=0

  if [ "$PREVIEW" = true ]; then
    pulumi_preview_and_capture || up_rc=$?
    write_stack_outputs
    popd >/dev/null || true
    if [ $up_rc -ne 0 ]; then die "pulumi preview failed (see logs)"; else return 0; fi
  fi

  if [ "$PREVIEW_AND_UP" = true ]; then
    pulumi_preview_and_capture || { log "pulumi: preview failed; aborting up"; up_rc=$?; }
    if [ $up_rc -ne 0 ]; then
      write_stack_outputs
      popd >/dev/null || true
      die "pulumi preview failed; aborting up"
    fi
  fi

  if [ "$PREVIEW" != true ]; then
    pulumi_up_and_capture || up_rc=$?
    write_stack_outputs
    popd >/dev/null || true
    if [ $up_rc -ne 0 ]; then
      die "pulumi up failed; inspect logs in ${PROJECT_DIR}/.pulumi-logs" 1
    fi
    return 0
  fi
}

pulumi_destroy_stack_if_exists_noninteractive() {
  ensure_pulumi_cli
  if [ ! -d "$PROJECT_DIR" ]; then log "pulumi: project dir ${PROJECT_DIR} not found; skipping destroy"; return; fi
  pushd "$PROJECT_DIR" >/dev/null || return
  activate_venv_if_exists
  export PULUMI_PYTHON_CMD="${PULUMI_PYTHON_CMD:-${VENV_DIR}/bin/python3}"
  if pulumi stack select "$STACK" >/dev/null 2>&1; then
    pulumi destroy --yes >/dev/null 2>&1 || true
    pulumi stack rm --yes >/dev/null 2>&1 || true
    log "pulumi: stack destroyed/removed"
  else
    PROJECT_NAME="$(get_pulumi_project_name || true)"
    if [ -n "${PROJECT_NAME:-}" ]; then
      for candidate in "${PROJECT_NAME}/${STACK}" "${PULUMI_ORG:-}/${PROJECT_NAME}/${STACK}"; do
        if pulumi stack select "$candidate" >/dev/null 2>&1; then
          pulumi destroy --yes >/dev/null 2>&1 || true
          pulumi stack rm --yes >/dev/null 2>&1 || true
          log "pulumi: stack ${candidate} destroyed/removed"
        fi
      done
    fi
    log "pulumi: stack ${STACK} not present; skipping"
  fi
  popd >/dev/null || true
}

cleanup_local_outputs() {
  local out_json="${PROJECT_DIR}/pulumi-outputs.json"
  local out_sh="${PROJECT_DIR}/pulumi-exports.sh"
  local pulumi_dir="${PROJECT_DIR}/.pulumi"
  log "cleanup-local: removing $out_json , $out_sh , and $pulumi_dir (if present)"
  rm -f "$out_json" "$out_sh" || true
  rm -rf "$pulumi_dir" || true
  rm -rf "${PROJECT_DIR}/.venv" || true
  rm -rf "${VENV_DIR}" || true
}

log "Using project dir: ${PROJECT_DIR}"
log "Using S3 bucket: ${S3_BUCKET}"

if [ "$MODE" = "create" ]; then
  log "=== CREATE MODE ==="

  flags_count=0
  [ "$FLAG_A" = true ] && flags_count=$((flags_count+1))
  [ "$FLAG_B" = true ] && flags_count=$((flags_count+1))
  [ "$FLAG_C" = true ] && flags_count=$((flags_count+1))
  [ "$FLAG_D" = true ] && flags_count=$((flags_count+1))
  [ "$FLAG_E" = true ] && flags_count=$((flags_count+1))

  if [ "$flags_count" -eq 0 ]; then
    export ENABLE_FILE_A=true
    export ENABLE_FILE_B=true
    export ENABLE_FILE_C=true
    export ENABLE_FILE_D=true
    export ENABLE_FILE_E=false
    log "No module flags passed; defaulting to enable A,B,C,D"
  else
    export ENABLE_FILE_A=${FLAG_A}
    export ENABLE_FILE_B=${FLAG_B}
    export ENABLE_FILE_C=${FLAG_C}
    export ENABLE_FILE_D=${FLAG_D}
    export ENABLE_FILE_E=${FLAG_E}
    log "Module flags set: A=${ENABLE_FILE_A} B=${ENABLE_FILE_B} C=${ENABLE_FILE_C} D=${ENABLE_FILE_D} E=${ENABLE_FILE_E}"
  fi

  create_bucket_if_missing "$S3_BUCKET"
  create_dynamodb_if_missing "$DDB_TABLE"
  POLICY_ARN="$(ensure_policy "$S3_BUCKET" "$DDB_TABLE" "$POLICY_NAME" || true)"
  create_iam_user_if_requested "$PULUMI_IAM_USER" "$POLICY_ARN" "$PULUMI_CREDS_FILE"
  log "waiting briefly for IAM propagation..."
  sleep 3

  create_venv_and_install

  if [ ! -f "${PROJECT_DIR}/Pulumi.yaml" ]; then
    cat >"${PROJECT_DIR}/Pulumi.yaml" <<YAML
name: ${STACK}-project
runtime: python
description: Minimal project created by pulumi_setup.sh
YAML
    log "pulumi-project: wrote ${PROJECT_DIR}/Pulumi.yaml"
  else
    log "pulumi-project: Pulumi.yaml exists; leaving"
  fi

  if [ ! -f "$REQ_FILE" ]; then
    cat >"$REQ_FILE" <<'REQ'
pulumi
pulumi-aws
boto3
REQ
    log "pulumi-project: wrote $REQ_FILE"
  else
    log "pulumi-project: requirements.txt exists; leaving"
  fi

  pulumi_login_and_run

  log "CREATE complete"
  exit 0
fi

if [ "$MODE" = "delete" ]; then
  log "=== DELETE MODE ==="
  if [ "$FORCE_FLAG" = true ] || [ "$FORCE_DELETE" = "true" ]; then
    log "[delete] FORCE mode enabled; entire bucket will be removed after prefix and infra cleanup"
  fi
  pulumi_destroy_stack_if_exists_noninteractive
  delete_s3_objects "$S3_BUCKET" "${S3_PREFIX}${STACK}"
  delete_s3_objects "$S3_BUCKET" "$S3_PREFIX"
  delete_dynamodb_table_if_exists "$DDB_TABLE"
  delete_policy_and_user_idempotent "$POLICY_NAME" "$PULUMI_IAM_USER"
  if [ "$FORCE_FLAG" = true ] || [ "$FORCE_DELETE" = "true" ]; then
    sleep 2
    empty_and_delete_bucket_force "$S3_BUCKET"
  else
    log "info: S3 bucket preserved; only prefix removed"
  fi
  cleanup_local_outputs
  log "DELETE complete"
  exit 0
fi

exit 0
