#!/usr/bin/env bash
# infra/aws/pulumi_setup.sh
# Final safe, idempotent Pulumi backend helper
# - Modes:
#     --create         create S3 backend prefix, DynamoDB lock table, IAM policy, optional IAM user,
#                      create venv, install python deps, pulumi login and pulumi up (stack selected)
#     --delete         destroy stack (if exists), delete only S3 prefix (/pulumi/), delete DDB table + IAM policy/user
#     --delete --force same as --delete but also empty & delete entire S3 bucket (irreversible)
# - Idempotent: repeated runs are safe
# - Usage examples:
#     S3_BUCKET=my-bucket STACK=prod PULUMI_IAM_USER=pulumi-ci ./pulumi_setup.sh --create
#     S3_BUCKET=my-bucket STACK=prod ./pulumi_setup.sh --delete
#     S3_BUCKET=my-bucket STACK=prod ./pulumi_setup.sh --delete --force
# install pulumi==3.196.0 pulumi-aws==7.7.0 pulumi-tls==5.2.2


set -euo pipefail

progname="$(basename "$0")"
if [ "${1:-}" == "--help" ] || [ "${1:-}" == "-h" ]; then
  cat <<EOF
Usage: $progname [--create] [--delete] [--force]
Only one of --create or --delete must be provided.
Environment variables:
  AWS_REGION        (default: ap-south-1)
  S3_BUCKET         (REQUIRED)
  S3_PREFIX         (default: pulumi/)          # prefix inside bucket used for backend
  DDB_TABLE         (default: pulumi-state-locks)
  PULUMI_IAM_USER   (optional; create IAM user for CI)
  PULUMI_CREDS_FILE (default: /tmp/pulumi-ci-credentials.json)
  POLICY_NAME       (default: PulumiStateAccessPolicy)
  STACK             (default: prod)
  PROJECT_DIR       (default: infra/pulumi-aws)
  VENV_DIR          (default: PROJECT_DIR/.venv)
  PULUMI_BINARY_PATH (optional: path to pulumi binary)
  FORCE_DELETE      (env var; set "true" to skip confirmation)
EOF
  exit 0
fi

# parse args
MODE=""
FORCE_FLAG=false
for arg in "$@"; do
  case "$arg" in
    --create) MODE="create" ;;
    --delete) MODE="delete" ;;
    --force) FORCE_FLAG=true ;;
    *) echo "Unknown argument: $arg" >&2; exit 2 ;;
  esac
done
if [ -z "$MODE" ]; then echo "ERROR: must provide --create or --delete" >&2; exit 2; fi

# env defaults
AWS_REGION="${AWS_REGION:-ap-south-1}"
S3_BUCKET="${PULUMI_S3_BUCKET:-}"
S3_PREFIX="${S3_PREFIX:-pulumi/}"
DDB_TABLE="${DDB_TABLE:-pulumi-state-locks}"
PULUMI_IAM_USER="${PULUMI_IAM_USER:-}"
PULUMI_CREDS_FILE="${PULUMI_CREDS_FILE:-/tmp/pulumi-ci-credentials.json}"
POLICY_NAME="${POLICY_NAME:-PulumiStateAccessPolicy}"
STACK="${STACK:-prod}"
PROJECT_DIR="${PROJECT_DIR:-infra/pulumi-aws}"
VENV_DIR="${VENV_DIR:-${PROJECT_DIR}/.venv}"
REQ_FILE="${PROJECT_DIR}/requirements.txt"
PULUMI_BINARY_PATH="${PULUMI_BINARY_PATH:-$HOME/.pulumi/bin/pulumi}"
FORCE_DELETE_ENV="${FORCE_DELETE:-false}"

# helpers
require_cmd() { command -v "$1" >/dev/null 2>&1 || { echo "ERROR: required command '$1' not found"; exit 10; }; }
get_account_id() { aws sts get-caller-identity --query Account --output text 2>/dev/null || true; }

# --- create helpers ---
create_bucket_if_missing() {
  local bucket="$1"
  echo "[s3] ensure bucket exists: ${bucket} (region=${AWS_REGION})"
  if aws s3api head-bucket --bucket "$bucket" 2>/dev/null; then
    echo "  bucket exists"
  else
    set +e
    if [ "$AWS_REGION" = "us-east-1" ]; then
      aws s3api create-bucket --bucket "$bucket" 2>/dev/null
    else
      aws s3api create-bucket --bucket "$bucket" --create-bucket-configuration LocationConstraint="$AWS_REGION" 2>/dev/null
    fi
    rc=$?
    set -e
    if [ $rc -ne 0 ]; then
      echo "  create-bucket returned non-zero (continuing - may already be owned by you or race condition)"
    else
      echo "  created bucket $bucket"
    fi
  fi
  aws s3api put-bucket-versioning --bucket "$bucket" --versioning-configuration Status=Enabled >/dev/null 2>&1 || true
  aws s3api put-bucket-encryption --bucket "$bucket" --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}' >/dev/null 2>&1 || true
  aws s3api put-bucket-lifecycle-configuration --bucket "$bucket" --lifecycle-configuration "{\"Rules\":[{\"ID\":\"pulumi-noncurrent-expire\",\"Prefix\":\"${S3_PREFIX}\",\"Status\":\"Enabled\",\"NoncurrentVersionExpiration\":{\"NoncurrentDays\":365}}]}" >/dev/null 2>&1 || true
  echo "[s3] bucket prepared (prefix=${S3_PREFIX})"
}

create_dynamodb_if_missing() {
  local table="$1"
  echo "[ddb] ensure DynamoDB table exists: ${table}"
  if aws dynamodb describe-table --table-name "$table" >/dev/null 2>&1; then
    echo "  table exists"
  else
    set +e
    aws dynamodb create-table --table-name "$table" --attribute-definitions AttributeName=LockID,AttributeType=S --key-schema AttributeName=LockID,KeyType=HASH --provisioned-throughput ReadCapacityUnits=5,WriteCapacityUnits=5 --region "$AWS_REGION" >/dev/null 2>&1
    rc=$?
    set -e
    if [ $rc -ne 0 ]; then
      echo "  create-table returned non-zero (continuing)"
    else
      aws dynamodb wait table-exists --table-name "$table" --region "$AWS_REGION"
      echo "  table ACTIVE"
    fi
  fi
}

create_policy_if_missing() {
  local bucket="$1" table="$2" name="$3"
  local acct; acct="$(get_account_id)"
  echo "[iam] ensure policy ${name}"
  local existing; existing="$(aws iam list-policies --scope Local --query "Policies[?PolicyName=='${name}'].Arn" --output text || true)"
  if [ -n "$existing" ]; then
    echo "  policy exists: $existing"
    POLICY_ARN="$existing"
    return 0
  fi
  local doc
  doc=$(cat <<JSON
{
  "Version":"2012-10-17",
  "Statement":[
    {
      "Effect":"Allow",
      "Action":[
        "s3:GetObject","s3:PutObject","s3:DeleteObject","s3:ListBucket","s3:GetBucketVersioning","s3:PutBucketVersioning"
      ],
      "Resource":[ "arn:aws:s3:::${bucket}", "arn:aws:s3:::${bucket}/*" ]
    },
    {
      "Effect":"Allow",
      "Action":[
        "dynamodb:GetItem","dynamodb:PutItem","dynamodb:DeleteItem","dynamodb:UpdateItem","dynamodb:Query","dynamodb:Scan","dynamodb:ConditionCheckItem"
      ],
      "Resource":[ "arn:aws:dynamodb:${AWS_REGION}:${acct}:table/${table}" ]
    }
  ]
}
JSON
)
  aws iam create-policy --policy-name "$name" --policy-document "$doc" >/dev/null 2>&1 || true
  POLICY_ARN="$(aws iam list-policies --scope Local --query "Policies[?PolicyName=='${name}'].Arn" --output text || true)"
  echo "  policy ARN: ${POLICY_ARN:-(not found)}"
}

create_iam_user_if_requested_idempotent() {
  if [ -z "$PULUMI_IAM_USER" ]; then echo "  no IAM user requested; skipping"; return 0; fi
  echo "[iam] ensure user: ${PULUMI_IAM_USER}"
  if aws iam get-user --user-name "$PULUMI_IAM_USER" >/dev/null 2>&1; then
    echo "  user exists"
  else
    aws iam create-user --user-name "$PULUMI_IAM_USER" >/dev/null 2>&1 || true
    echo "  created user"
  fi
  if ! aws iam list-attached-user-policies --user-name "$PULUMI_IAM_USER" --query "AttachedPolicies[?PolicyArn=='$POLICY_ARN']" --output text | grep -q "$POLICY_ARN" 2>/dev/null; then
    aws iam attach-user-policy --user-name "$PULUMI_IAM_USER" --policy-arn "$POLICY_ARN" >/dev/null 2>&1 || true
    echo "  attached policy"
  else
    echo "  policy already attached"
  fi
  local keys; keys="$(aws iam list-access-keys --user-name "$PULUMI_IAM_USER" --query 'AccessKeyMetadata[].AccessKeyId' --output text || true)"
  if [ -z "$keys" ]; then
    aws iam create-access-key --user-name "$PULUMI_IAM_USER" >"$PULUMI_CREDS_FILE"
    chmod 600 "$PULUMI_CREDS_FILE" || true
    echo "  created access key at $PULUMI_CREDS_FILE"
  else
    echo "  user already has access keys (not creating new one)"
  fi
}

# --- delete helpers ---
delete_prefix_objects_and_versions() {
  local bucket="$1" prefix="$2"
  echo "[s3-delete-prefix] deleting objects under s3://${bucket}/${prefix}"
  while :; do
    rv="$(aws s3api list-object-versions --bucket "$bucket" --prefix "$prefix" --output json 2>/dev/null || echo '{}')"
    count=$(jq -r '[.Versions[], .DeleteMarkers[]] | length' <<<"$rv" 2>/dev/null || echo 0)
    if [ -z "$count" ] || [ "$count" = "0" ]; then break; fi
    objs=$(jq -c '[.Versions[]?, .DeleteMarkers[]?] | map({Key:.Key,VersionId:.VersionId})' <<<"$rv")
    tmp="$(mktemp)"; jq -n --argjson arr "$objs" '{Objects:$arr}' >"$tmp"
    aws s3api delete-objects --bucket "$bucket" --delete "file://$tmp" >/dev/null 2>&1 || true
    rm -f "$tmp"; sleep 1
  done
  echo "[s3-delete-prefix] done"
}

empty_and_delete_bucket_force() {
  local bucket="$1"
  echo "[s3-delete-all] force-empty & delete s3://${bucket} (irrevocable)"
  while :; do
    rv="$(aws s3api list-object-versions --bucket "$bucket" --output json 2>/dev/null || echo '{}')"
    count=$(jq -r '[.Versions[], .DeleteMarkers[]] | length' <<<"$rv" 2>/dev/null || echo 0)
    if [ -z "$count" ] || [ "$count" = "0" ]; then break; fi
    objs=$(jq -c '[.Versions[]?, .DeleteMarkers[]?] | map({Key:.Key,VersionId:.VersionId})' <<<"$rv")
    tmp="$(mktemp)"; jq -n --argjson arr "$objs" '{Objects:$arr}' >"$tmp"
    aws s3api delete-objects --bucket "$bucket" --delete "file://$tmp" >/dev/null 2>&1 || true
    rm -f "$tmp"; sleep 1
  done
  aws s3api delete-bucket --bucket "$bucket" --region "$AWS_REGION" >/dev/null 2>&1 || true
  echo "[s3-delete-all] bucket delete requested"
}

delete_dynamodb_table_if_exists() {
  local table="$1"
  if aws dynamodb describe-table --table-name "$table" >/dev/null 2>&1; then
    aws dynamodb delete-table --table-name "$table" --region "$AWS_REGION" >/dev/null 2>&1 || true
    aws dynamodb wait table-not-exists --table-name "$table" --region "$AWS_REGION" || true
    echo "[ddb-delete] table deleted or delete attempted"
  else
    echo "[ddb-delete] table not found; skipping"
  fi
}

delete_policy_and_user_idempotent() {
  local policy_name="$1" user="$2"
  existing="$(aws iam list-policies --scope Local --query "Policies[?PolicyName=='${policy_name}'].Arn" --output text || true)"
  if [ -n "$existing" ]; then
    for u in $(aws iam list-entities-for-policy --policy-arn "$existing" --query 'PolicyUsers[].UserName' --output text || true); do aws iam detach-user-policy --user-name "$u" --policy-arn "$existing" || true; done
    for r in $(aws iam list-entities-for-policy --policy-arn "$existing" --query 'PolicyRoles[].RoleName' --output text || true); do aws iam detach-role-policy --role-name "$r" --policy-arn "$existing" || true; done
    for v in $(aws iam list-policy-versions --policy-arn "$existing" --query 'Versions[?IsDefaultVersion==`false`].VersionId' --output text || true); do aws iam delete-policy-version --policy-arn "$existing" --version-id "$v" || true; done
    aws iam delete-policy --policy-arn "$existing" || true
    echo "[iam-delete] policy delete attempted"
  else
    echo "[iam-delete] policy not found; skipping"
  fi
  if [ -n "$user" ]; then
    if aws iam get-user --user-name "$user" >/dev/null 2>&1; then
      for k in $(aws iam list-access-keys --user-name "$user" --query 'AccessKeyMetadata[].AccessKeyId' --output text || true); do aws iam delete-access-key --user-name "$user" --access-key-id "$k" || true; done
      for a in $(aws iam list-attached-user-policies --user-name "$user" --query 'AttachedPolicies[].PolicyArn' --output text || true); do aws iam detach-user-policy --user-name "$user" --policy-arn "$a" || true; done
      for ip in $(aws iam list-user-policies --user-name "$user" --query 'PolicyNames[]' --output text || true); do aws iam delete-user-policy --user-name "$user" --policy-name "$ip" || true; done
      aws iam delete-user --user-name "$user" || true
      echo "[iam-delete] user delete attempted"
    else
      echo "[iam-delete] user not found; skipping"
    fi
  fi
}

# --- pulumi & venv helpers ---
ensure_pulumi_cli() {
  if command -v pulumi >/dev/null 2>&1; then return 0; fi
  if [ -x "$PULUMI_BINARY_PATH" ]; then export PATH="$(dirname "$PULUMI_BINARY_PATH"):$PATH"; return 0; fi
  if command -v curl >/dev/null 2>&1; then curl -fsSL https://get.pulumi.com | sh; export PATH="$HOME/.pulumi/bin:$PATH"; return 0; fi
  echo "ERROR: pulumi CLI not found and cannot auto-install (curl missing)" >&2; exit 11
}

create_venv_and_install() {
  if [ ! -d "$VENV_DIR" ]; then python3 -m venv "$VENV_DIR"; fi
  # shellcheck disable=SC1090
  source "${VENV_DIR}/bin/activate"
  python -m pip install --upgrade pip setuptools wheel >/dev/null
  if [ -f "$REQ_FILE" ]; then python -m pip install -r "$REQ_FILE"; else python -m pip install pulumi pulumi-aws pulumi-tls >/dev/null; fi
  echo "[venv] ready"
}

activate_venv_if_exists() {
  if [ -d "$VENV_DIR" ]; then # shellcheck disable=SC1090
    source "${VENV_DIR}/bin/activate"
    echo "[venv] activated"
  fi
}

pulumi_login_and_up() {
  ensure_pulumi_cli
  export AWS_DYNAMODB_LOCK_TABLE="$DDB_TABLE"
  echo "[pulumi] login s3://${S3_BUCKET}/${S3_PREFIX}"
  pulumi login "s3://${S3_BUCKET}/${S3_PREFIX}" >/dev/null 2>&1 || true
  if [ ! -d "$PROJECT_DIR" ]; then echo "ERROR: project dir $PROJECT_DIR not found" >&2; exit 12; fi
  pushd "$PROJECT_DIR" >/dev/null || exit 1
  activate_venv_if_exists
  if ! pulumi stack select "$STACK" >/dev/null 2>&1; then
    pulumi stack init "$STACK" >/dev/null 2>&1 || true
  fi
  pulumi stack select "$STACK"
  pulumi config set aws:region "$AWS_REGION" --stack "$STACK" >/dev/null 2>&1 || true
  for e in $(env | awk -F= '/^PULUMI_CONFIG_/{print $1}'); do
    val="$(printenv "$e")"; key="${e#PULUMI_CONFIG_}"; key_lc="$(echo "$key" | tr '[:upper:]' '[:lower:]')"; pulumi config set "$key_lc" "$val" --stack "$STACK" >/dev/null 2>&1 || true
  done
  pulumi up --yes
  # capture outputs
  out_json="$(pwd)/pulumi-outputs.json"
  pulumi stack output --json >"$out_json" 2>/dev/null || true
  out_sh="$(pwd)/pulumi-exports.sh"
  {
    printf '%s\n' '#!/usr/bin/env bash'
    printf '%s\n' '# Pulumi exports generated'
    if command -v jq >/dev/null 2>&1 && [ -s "$out_json" ]; then
      jq -r 'to_entries | map({k:("PULUMI_"+(.key|ascii_upcase|gsub("-";"_")), v:(.value|if type=="string" then . else tostring end)})| .[] | "export \(.k)=\"" + ( .v | gsub("\""; "\\\"") ) + "\""' "$out_json"
    fi
  } >"$out_sh"
  chmod +x "$out_sh" || true
  echo "[pulumi] outputs written to $out_json and $out_sh"
  popd >/dev/null || true
}

pulumi_destroy_stack_if_exists() {
  ensure_pulumi_cli
  if [ ! -d "$PROJECT_DIR" ]; then echo "[pulumi] project dir ${PROJECT_DIR} not found; skipping destroy"; return; fi
  pushd "$PROJECT_DIR" >/dev/null || return
  activate_venv_if_exists
  if pulumi stack select "$STACK" >/dev/null 2>&1; then
    pulumi destroy --yes || true
    pulumi stack rm --yes || true
    echo "[pulumi] stack destroyed/removed"
  else
    echo "[pulumi] stack ${STACK} not present; skipping"
  fi
  popd >/dev/null || true
}

# --- runtime checks ---
require_cmd aws
require_cmd python3
command -v jq >/dev/null 2>&1 || echo "[info] jq not found; JSON output conversion will be skipped"

# --- main flows ---
if [ -z "$S3_BUCKET" ]; then echo "ERROR: S3_BUCKET must be set"; exit 2; fi

if [ "$MODE" = "create" ]; then
  echo "=== CREATE MODE ==="
  create_bucket_if_missing "$S3_BUCKET"
  create_dynamodb_if_missing "$DDB_TABLE"
  create_policy_if_missing "$S3_BUCKET" "$DDB_TABLE" "$POLICY_NAME"
  create_iam_user_if_requested_idempotent
  echo "Sleeping briefly for IAM propagation..."
  sleep 2
  create_venv_and_install
  pulumi_login_and_up
  echo "CREATE complete"
  exit 0
fi

if [ "$MODE" = "delete" ]; then
  echo "=== DELETE MODE ==="
  if [ "$FORCE_FLAG" = true ] || [ "$FORCE_DELETE_ENV" = "true" ]; then
    echo "[delete] FORCE mode enabled; entire bucket will be removed after prefix and infra cleanup"
    CONFIRM=false
  else
    CONFIRM=true
  fi
  if [ "$CONFIRM" = true ]; then
    echo "This will delete Pulumi state under s3://${S3_BUCKET}/${S3_PREFIX}, remove DDB table ${DDB_TABLE}, and IAM policy ${POLICY_NAME}."
    if [ -n "$PULUMI_IAM_USER" ]; then echo "IAM user to remove (if present): ${PULUMI_IAM_USER}"; fi
    read -r -p "Type 'DELETE' to continue: " ans
    if [ "$ans" != "DELETE" ]; then echo "Aborting delete"; exit 1; fi
  fi

  pulumi_destroy_stack_if_exists

  # delete only prefix
  delete_prefix_objects_and_versions "$S3_BUCKET" "$S3_PREFIX"

  delete_dynamodb_table_if_exists "$DDB_TABLE"
  delete_policy_and_user_idempotent "$POLICY_NAME" "$PULUMI_IAM_USER"

  if [ "$FORCE_FLAG" = true ]; then
    empty_and_delete_bucket_force "$S3_BUCKET"
  else
    echo "[info] S3 bucket preserved; only prefix removed"
  fi

  echo "DELETE complete"
  exit 0
fi

exit 0
