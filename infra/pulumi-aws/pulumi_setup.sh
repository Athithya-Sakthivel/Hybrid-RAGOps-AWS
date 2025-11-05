#!/usr/bin/env bash
# infra/pulumi-aws/pulumi_setup.sh
# Robust non-interactive Pulumi backend helper (create / delete)
set -euo pipefail

prog="$(basename "$0")"
usage() {
  cat <<EOF
Usage: $prog [--create|--delete] [--force] [-h|--help]
Non-interactive. --force with --delete will attempt to delete the entire S3 bucket.
Env defaults (override with env):
  AWS_REGION=ap-south-1
  S3_BUCKET=e2e-rag-42
  S3_PREFIX=pulumi/
  DDB_TABLE=pulumi-state-locks
  PULUMI_IAM_USER (optional)
  PULUMI_CREDS_FILE=/tmp/pulumi-ci-credentials.json
  POLICY_NAME=PulumiStateAccessPolicy
  PROJECT_DIR=infra/pulumi-aws
  VENV_DIR=PROJECT_DIR/.venv
  STACK=ragops
EOF
}

# parse args
MODE="" FORCE_FLAG=false
while [ $# -gt 0 ]; do
  case "$1" in
    --create) MODE="create"; shift;;
    --delete) MODE="delete"; shift;;
    --force) FORCE_FLAG=true; shift;;
    -h|--help) usage; exit 0;;
    *) echo "Unknown arg: $1" >&2; usage; exit 2;;
  esac
done
[ -n "$MODE" ] || { echo "ERROR: must pass --create or --delete" >&2; usage; exit 2; }

# sane defaults
AWS_REGION="${AWS_REGION:-ap-south-1}"
S3_BUCKET="${S3_BUCKET:-${PULUMI_S3_BUCKET:-e2e-rag-42}}"
S3_PREFIX="${S3_PREFIX:-pulumi/}"
PULUMI_CONFIG_PASSPHRASE="${PULUMI_CONFIG_PASSPHRASE:-}"
DDB_TABLE="${DDB_TABLE:-pulumi-state-locks}"
PULUMI_IAM_USER="${PULUMI_IAM_USER:-}"
PULUMI_CREDS_FILE="${PULUMI_CREDS_FILE:-/tmp/pulumi-ci-credentials.json}"
POLICY_NAME="${POLICY_NAME:-PulumiStateAccessPolicy}"
PROJECT_DIR="${PROJECT_DIR:-infra/pulumi-aws}"
VENV_DIR="${VENV_DIR:-${PROJECT_DIR}/.venv}"
REQ_FILE="${REQ_FILE:-${PROJECT_DIR}/requirements.txt}"
PULUMI_BINARY_PATH="${PULUMI_BINARY_PATH:-$HOME/.pulumi/bin/pulumi}"
STACK="${STACK:-ragops}"
FORCE_DELETE_ENV="${FORCE_DELETE:-false}"

log() { printf '[%s] %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*"; }
die() { echo "ERROR: $*" >&2; exit "${2:-1}"; }
require_cmd() { command -v "$1" >/dev/null 2>&1 || die "required command '$1' not found" 10; }

# temp cleanup
TMPS=()
cleanup() { for f in "${TMPS[@]:-}"; do [ -f "$f" ] && rm -f "$f"; done; }
trap cleanup EXIT

# --- preflight AWS creds ---
if ! aws sts get-caller-identity >/dev/null 2>&1; then
  die "AWS credentials not configured / can't call sts" 20
fi

# --- S3 helpers ---
create_bucket_if_missing() {
  local bucket="$1"
  log "s3: ensure bucket exists: ${bucket} (region=${AWS_REGION})"
  if aws s3api head-bucket --bucket "$bucket" >/dev/null 2>&1; then
    log "s3: bucket exists"
  else
    if [ "$AWS_REGION" = "us-east-1" ]; then
      aws s3api create-bucket --bucket "$bucket" >/dev/null 2>&1 || log "s3: create returned non-zero"
    else
      aws s3api create-bucket --bucket "$bucket" --create-bucket-configuration LocationConstraint="$AWS_REGION" >/dev/null 2>&1 || log "s3: create returned non-zero"
    fi
    log "s3: create attempted"
  fi
  aws s3api put-bucket-versioning --bucket "$bucket" --versioning-configuration Status=Enabled >/dev/null 2>&1 || true
  aws s3api put-bucket-encryption --bucket "$bucket" --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}' >/dev/null 2>&1 || true
  aws s3api put-bucket-lifecycle-configuration --bucket "$bucket" --lifecycle-configuration "{\"Rules\":[{\"ID\":\"pulumi-noncurrent-expire\",\"Prefix\":\"${S3_PREFIX}\",\"Status\":\"Enabled\",\"NoncurrentVersionExpiration\":{\"NoncurrentDays\":365}}]}" >/dev/null 2>&1 || true
  log "s3: bucket prepared (prefix=${S3_PREFIX})"
}

delete_s3_objects() {
  local bucket="$1" prefix="${2:-}"
  mkdir -p "${PROJECT_DIR}/.pulumi-logs" || true
  log "s3-delete: deleting objects in s3://${bucket}/${prefix}"
  while :; do
    local rv count objs tmp
    if [ -n "$prefix" ]; then
      rv="$(aws s3api list-object-versions --bucket "$bucket" --prefix "$prefix" --output json 2>/dev/null || echo '{}')"
    else
      rv="$(aws s3api list-object-versions --bucket "$bucket" --output json 2>/dev/null || echo '{}')"
    fi
    count=$(jq -r '[.Versions[], .DeleteMarkers[]] | length' <<<"$rv" 2>/dev/null || echo 0)
    if [ -z "$count" ] || [ "$count" = "0" ]; then break; fi
    objs=$(jq -c '[.Versions[]?, .DeleteMarkers[]?] | map({Key:.Key,VersionId:.VersionId})' <<<"$rv")
    tmp="$(mktemp)"; TMPS+=("$tmp")
    jq -n --argjson arr "$objs" '{Objects:$arr}' >"$tmp"
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

# --- DynamoDB helpers ---
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

# --- IAM helpers ---
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
    for r in $(aws iam list-entities-for-policy --policy-arn "$existing" --query 'PolicyRoles[].RoleName' --output text || true); do aws iam detach-role-policy --role-name "$r" --policy-arn "$existing" || true; done
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

# --- Pulumi & venv helpers ---
ensure_pulumi_cli() {
  if command -v pulumi >/dev/null 2>&1; then return 0; fi
  if [ -x "$PULUMI_BINARY_PATH" ]; then export PATH="$(dirname "$PULUMI_BINARY_PATH"):$PATH"; fi
  if ! command -v pulumi >/dev/null 2>&1; then
    if command -v curl >/dev/null 2>&1; then
      curl -fsSL https://get.pulumi.com | sh
      export PATH="$HOME/.pulumi/bin:$PATH"
    else
      die "pulumi CLI not found and cannot auto-install (curl missing)" 11
    fi
  fi
  for i in 1 2 3; do
    if pulumi version >/dev/null 2>&1; then
      log "pulumi: $(pulumi version)"
      return 0
    fi
    sleep 1
  done
  die "pulumi not responding after install" 11
}

create_venv_and_install() {
  [ -d "$VENV_DIR" ] || python3 -m venv "$VENV_DIR"
  source "${VENV_DIR}/bin/activate"
  echo "INSTALLING THE NECCESSARY PACKAGES IF NOT ALREADY..."
  python -m pip install --upgrade pip setuptools wheel >/dev/null 2>&1 || true
  python -m pip install -r $REQ_FILE >/dev/null
}

activate_venv_if_exists() {
  if [ -d "$VENV_DIR" ]; then
    # shellcheck disable=SC1090
    source "${VENV_DIR}/bin/activate"
    log "venv: activated"
  fi
}

# create project atomically if missing
create_project_atomic_if_missing() {
  mkdir -p "$PROJECT_DIR"
  local pd="${PROJECT_DIR}/Pulumi.yaml"
  local main_py="${PROJECT_DIR}/__main__.py"
  local req="${REQ_FILE}"
  if [ ! -f "$pd" ]; then
    tmp="$(mktemp)"; TMPS+=("$tmp")
    cat >"$tmp" <<YAML
name: ${STACK}-project
runtime: python
description: Minimal project auto-created by pulumi_setup.sh
YAML
    mv "$tmp" "$pd"
    log "pulumi-project: wrote $pd"
  else
    log "pulumi-project: Pulumi.yaml exists; leaving"
  fi

  if [ ! -f "$main_py" ]; then
    tmp="$(mktemp)"; TMPS+=("$tmp")
    cat >"$tmp" <<'PY'
import pulumi
pulumi.export('message', 'minimal-pulumi-project: no-op')
PY
    mv "$tmp" "$main_py"
    log "pulumi-project: wrote $main_py"
  else
    log "pulumi-project: __main__.py exists; leaving"
  fi

  if [ ! -f "$req" ]; then
    tmp="$(mktemp)"; TMPS+=("$tmp")
    cat >"$tmp" <<'REQ'
pulumi==3.196.0
pulumi-aws==7.7.0
pulumi-tls==5.2.2
boto3
REQ
    mv "$tmp" "$req"
    log "pulumi-project: wrote $req"
  else
    log "pulumi-project: requirements.txt exists; leaving"
  fi
}

get_pulumi_project_name() {
  local pd="${PROJECT_DIR}/Pulumi.yaml"
  if [ -f "$pd" ]; then awk -F: '/^name[[:space:]]*:/ {gsub(/^[ \t]+|[ \t]+$/,"",$2); print $2; exit}' "$pd" || true; fi
}

# Verify current stack selected (returns 0 if current stack present)
verify_stack_selected() {
  if pulumi stack >/dev/null 2>&1; then return 0; fi
  return 1
}

# robust select/init
pulumi_select_or_init_stack() {
  local stack="$1"
  if pulumi stack select "$stack" >/dev/null 2>&1; then
    log "pulumi: selected existing stack '$stack'"
    verify_stack_selected || die "stack select reported success but verification failed" 12
    return 0
  fi

  PROJECT_NAME="$(get_pulumi_project_name || true)"
  candidates=("$stack")
  [ -n "${PROJECT_NAME:-}" ] && candidates+=("${PROJECT_NAME}/${stack}")
  [ -n "${PULUMI_ORG:-}" ] && [ -n "${PROJECT_NAME:-}" ] && candidates+=("${PULUMI_ORG}/${PROJECT_NAME}/${stack}")

  for c in "${candidates[@]}"; do
    [ -z "$c" ] && continue
    attempts=1
    while [ $attempts -le 4 ]; do
      log "pulumi: trying stack init '$c' (attempt $attempts)"
      set +e
      out="$(pulumi stack init "$c" 2>&1 || true)"
      rc=$?
      set -e
      if [ $rc -eq 0 ]; then
        pulumi stack select "$c" >/dev/null 2>&1 || true
        if verify_stack_selected; then
          log "pulumi: created and selected '$c'"
          return 0
        else
          log "pulumi: init succeeded but selection verification failed for '$c'; output: ${out:-<no output>}"
        fi
      else
        log "pulumi: init failed for '$c': ${out:-<no output>}"
      fi
      attempts=$((attempts+1))
      sleep $((attempts))
    done
  done

  log "pulumi: fallback -> pulumi new python --yes --force (non-interactive)"
  if pulumi new python --yes --force >/dev/null 2>&1; then
    if pulumi stack init "$stack" >/dev/null 2>&1; then
      pulumi stack select "$stack" >/dev/null 2>&1 || true
      verify_stack_selected || die "fallback created stack but verification failed" 12
      log "pulumi: fallback created and selected stack '$stack'"
      return 0
    fi
  fi

  die "unable to select or init pulumi stack '${stack}' (candidates: ${candidates[*]})" 12
}

# pulumi up with log capture
pulumi_up_and_capture() {
  local logdir="${PROJECT_DIR}/.pulumi-logs"; mkdir -p "$logdir"
  local logf="${logdir}/pulumi-up-$(date -u +%s).log"
  : >"$logf"
  if pulumi up --yes >"$logf" 2>&1; then
    log "pulumi: up succeeded (log: $logf)"
    return 0
  else
    log "pulumi: up failed; last 300 lines of $logf" >&2
    tail -n 300 "$logf" >&2 || true
    return 1
  fi
}

pulumi_login_and_up_noninteractive() {
  ensure_pulumi_cli
  export AWS_DYNAMODB_LOCK_TABLE="$DDB_TABLE"
  log "pulumi: login s3://${S3_BUCKET}/${S3_PREFIX}"
  pulumi login "s3://${S3_BUCKET}/${S3_PREFIX}" >/dev/null 2>&1 || log "pulumi: login returned non-zero (continuing)"

  if [ ! -d "$PROJECT_DIR" ]; then die "project dir $PROJECT_DIR not found" 13; fi
  pushd "$PROJECT_DIR" >/dev/null || exit 1
  activate_venv_if_exists

  pulumi_select_or_init_stack "$STACK"

  pulumi config set aws:region "$AWS_REGION" >/dev/null 2>&1 || true
  for e in $(env | awk -F= '/^PULUMI_CONFIG_/{print $1}'); do
    val="$(printenv "$e")"; key="${e#PULUMI_CONFIG_}"; key_lc="$(echo "$key" | tr '[:upper:]' '[:lower:]')"
    pulumi config set "$key_lc" "$val" >/dev/null 2>&1 || true
  done

  if ! pulumi_up_and_capture; then
    die "pulumi up failed; inspect logs in ${PROJECT_DIR}/.pulumi-logs" 1
  fi

  out_json="$(pwd)/pulumi-outputs.json"
  pulumi stack output --json >"$out_json" 2>/dev/null || true
  out_sh="$(pwd)/pulumi-exports.sh"

  # Use Python to safely generate export lines to avoid jq quoting issues.
  if command -v python3 >/dev/null 2>&1 && [ -s "$out_json" ]; then
    python3 - "$out_json" >"$out_sh" <<'PY'
import json,sys
fn=sys.argv[1]
with open(fn) as f:
    data=json.load(f)
for k,v in data.items():
    key="PULUMI_"+k.upper().replace("-","_")
    if isinstance(v,str):
        val=v
    else:
        val=json.dumps(v)
    # escape double quotes
    val=val.replace('"','\\"')
    print(f'export {key}="{val}"')
PY
  else
    : >"$out_sh"
  fi

  chmod +x "$out_sh" >/dev/null 2>&1 || true
  log "pulumi: outputs written to $out_json and $out_sh"
  popd >/dev/null || true
}

pulumi_destroy_stack_if_exists_noninteractive() {
  ensure_pulumi_cli
  if [ ! -d "$PROJECT_DIR" ]; then log "pulumi: project dir ${PROJECT_DIR} not found; skipping destroy"; return; fi
  pushd "$PROJECT_DIR" >/dev/null || return
  activate_venv_if_exists
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

# --- runtime checks ---
require_cmd aws
require_cmd python3
require_cmd jq
command -v jq >/dev/null 2>&1 || log "info: jq not found; outputs JSON conversion limited"

log "Using S3 bucket: ${S3_BUCKET}"

if [ "$MODE" = "create" ]; then
  log "=== CREATE MODE ==="
  create_bucket_if_missing "$S3_BUCKET"
  create_dynamodb_if_missing "$DDB_TABLE"
  POLICY_ARN="$(ensure_policy "$S3_BUCKET" "$DDB_TABLE" "$POLICY_NAME" || true)"
  create_iam_user_if_requested "$PULUMI_IAM_USER" "$POLICY_ARN" "$PULUMI_CREDS_FILE"
  log "waiting briefly for IAM propagation..."
  sleep 3
  create_venv_and_install
  create_project_atomic_if_missing
  pulumi_login_and_up_noninteractive
  log "CREATE complete"
  exit 0
fi

if [ "$MODE" = "delete" ]; then
  log "=== DELETE MODE (non-interactive) ==="
  pulumi_destroy_stack_if_exists_noninteractive
  delete_s3_objects "$S3_BUCKET" "$S3_PREFIX"
  delete_dynamodb_table_if_exists "$DDB_TABLE"
  delete_policy_and_user_idempotent "$POLICY_NAME" "$PULUMI_IAM_USER"
  if [ "$FORCE_FLAG" = true ] || [ "$FORCE_DELETE_ENV" = "true" ]; then
    empty_and_delete_bucket_force "$S3_BUCKET"
  else
    log "info: S3 bucket preserved; only prefix removed"
  fi
  log "DELETE complete"
  exit 0
fi

exit 0
