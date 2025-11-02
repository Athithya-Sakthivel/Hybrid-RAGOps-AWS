#!/usr/bin/env bash
set -euo pipefail

# request_quota.sh
# Usage: ./request_quota.sh [INSTANCE_TYPE] [NUM_INSTANCES] [REGION] [--apply]
# Example: ./request_quota.sh g4dn.xlarge 1 ap-south-1 --apply

INSTANCE_TYPE="${1:-g4dn.xlarge}"
NUM_INSTANCES="${2:-1}"
REGION="${3:-ap-south-1}"
APPLY_FLAG="${4:-}"
MARGIN_INSTANCES=1
AWS="aws --region ${REGION}"

err(){ echo "ERROR: $*" >&2; exit 1; }
info(){ echo "$*"; }

command -v aws >/dev/null 2>&1 || err "aws CLI not found"
command -v jq >/dev/null 2>&1 || err "jq not found"

info "REQUEST QUOTA HELPER — region=${REGION} instance_type=${INSTANCE_TYPE} num_instances=${NUM_INSTANCES} margin=${MARGIN_INSTANCES}"
info ""

# 1) credentials
info "1) verifying AWS credentials..."
if ! ${AWS} sts get-caller-identity --output json >/tmp/reqq_sts.json 2>/tmp/reqq_sts_err.json; then
  sed -n '1,200p' /tmp/reqq_sts_err.json || true
  err "aws sts failed - check credentials/region"
fi
cat /tmp/reqq_sts.json
info ""

# 2) check availability
info "2) checking instance offering in ${REGION}..."
OFFERS=$(${AWS} ec2 describe-instance-type-offerings --location-type region --filters Name=instance-type,Values="${INSTANCE_TYPE}" --output json 2>/tmp/reqq_offers_err.json || true)
OFFER_COUNT=$(echo "${OFFERS}" | jq -r '.InstanceTypeOfferings | length' 2>/dev/null || echo 0)
if [[ "${OFFER_COUNT}" == "0" || -z "${OFFER_COUNT}" ]]; then
  info "Instance ${INSTANCE_TYPE} NOT offered in ${REGION}. Listing GPU-capable types in region:"
  ${AWS} ec2 describe-instance-type-offerings --location-type region --output json \
    | jq -r '.InstanceTypeOfferings[].InstanceType' \
    | sort -u \
    | egrep '^(g|p|p3|p4|g4|g5|trn|p5|p6)' || true
  err "Pick valid GPU instance type and re-run"
fi

# 3) get DefaultVCpus
info "3) querying DefaultVCpus for ${INSTANCE_TYPE}..."
set +e
VCPS_RAW=$(${AWS} ec2 describe-instance-types --instance-types "${INSTANCE_TYPE}" --query "InstanceTypes[0].VCpuInfo.DefaultVCpus" --output text 2>/tmp/reqq_vcpu_err.txt)
RC=$?
set -e
if [[ ${RC} -ne 0 || -z "${VCPS_RAW}" || "${VCPS_RAW}" == "None" ]]; then
  sed -n '1,200p' /tmp/reqq_vcpu_err.txt || true
  err "unable to get DefaultVCpus for ${INSTANCE_TYPE}"
fi
VCPS=$(printf "%d" "${VCPS_RAW}")
info "Default vCPUs for ${INSTANCE_TYPE} = ${VCPS}"
REQ_VCPUS=$(( VCPS * NUM_INSTANCES ))
REQ_WITH_MARGIN_VCPUS=$(( REQ_VCPUS + (MARGIN_INSTANCES * VCPS) ))
REQ_INSTANCES_WITH_MARGIN=$(( NUM_INSTANCES + MARGIN_INSTANCES ))
info "vCPUs needed (no margin)=${REQ_VCPUS}; with margin=${REQ_WITH_MARGIN_VCPUS}; instances with margin=${REQ_INSTANCES_WITH_MARGIN}"
info ""

# 4) prepare candidate quota codes (preferred)
info "4) fetching service quotas and selecting candidates..."
SQ_JSON=$(${AWS} service-quotas list-service-quotas --service-code ec2 --output json 2>/tmp/reqq_sq_err.json) || { sed -n '1,200p' /tmp/reqq_sq_err.json || true; err "service-quotas API failed"; }

PREFERRED_CODES=("L-DB2E81BA" "L-CAE24619" "L-4F9BB70B" "L-2C8F52B3" "L-FCF0179E" "L-1216C47A")
candidates=()

for code in "${PREFERRED_CODES[@]}"; do
  entry=$(echo "${SQ_JSON}" | jq -r --arg C "$code" '.Quotas[] | select(.QuotaCode==$C) | "\(.QuotaCode)|||\(.QuotaName)|||\(.Value)"' || true)
  if [[ -n "${entry}" ]]; then
    candidates+=("${entry}")
  fi
done

# if none found, heuristic search (family token / gpu / on-demand / vcpu)
if [[ ${#candidates[@]} -eq 0 ]]; then
  FAM_TOKEN=$(echo "${INSTANCE_TYPE}" | sed -E 's/^([a-zA-Z0-9-]+).*/\1/' | tr '[:upper:]' '[:lower:]')
  mapfile -t tmpmatches < <(echo "${SQ_JSON}" \
    | jq -r --arg TOK "$FAM_TOKEN" '.Quotas[] | select((.QuotaName|ascii_downcase) | contains($TOK) or contains("g and vt") or contains("running on-demand g") or contains("running on-demand") or contains("vcpu") or contains("g4dn")) | "\(.QuotaCode)|||\(.QuotaName)|||\(.Value)"')
  for m in "${tmpmatches[@]}"; do candidates+=("$m"); done
fi

if [[ ${#candidates[@]} -eq 0 ]]; then
  err "no candidate quotas found; open AWS Service Quotas console and search for GPU / G / P family quotas"
fi

info "5) candidates (priority):"
i=0
for c in "${candidates[@]}"; do
  i=$((i+1))
  code=$(printf "%s" "$c" | cut -d '|' -f1)
  name=$(printf "%s" "$c" | cut -d '|' -f2)
  val=$(printf "%s" "$c" | cut -d '|' -f3)
  printf " %2d) %s  -- %s  (current=%s)\n" "${i}" "${code}" "${name}" "${val:-null}"
done
info ""

# 6) compute desired and optionally submit
info "6) computing desired values and preparing commands..."
declare -a to_request_cmds
declare -a to_request_codes

for c in "${candidates[@]}"; do
  code=$(printf "%s" "$c" | cut -d '|' -f1)
  # fetch up-to-date quota name/value using get-service-quota (some list outputs may omit Value)
  set +e
  qinfo=$(${AWS} service-quotas get-service-quota --service-code ec2 --quota-code "${code}" --output json 2>/tmp/reqq_getsq_err.json)
  rc=$?
  set -e
  if [[ ${rc} -ne 0 || -z "${qinfo}" ]]; then
    # fallback to candidate entry name
    qname=$(printf "%s" "$c" | cut -d '|' -f2)
    qval=$(printf "%s" "$c" | cut -d '|' -f3)
  else
    qname=$(echo "${qinfo}" | jq -r '.Quota.QuotaName // empty')
    qval=$(echo "${qinfo}" | jq -r '.Quota.Value // empty')
  fi
  qname_l=$(echo "${qname}" | tr '[:upper:]' '[:lower:]')
  desired=""
  if echo "${qname_l}" | grep -q "vcpu" || echo "${qname_l}" | grep -q "running on-demand standard"; then
    desired="${REQ_WITH_MARGIN_VCPUS}"
  elif echo "${qname_l}" | grep -q "g and vt" || echo "${qname_l}" | grep -q "running on-demand g" || echo "${qname_l}" | grep -q "g4dn" || echo "${qname_l}" | grep -q "running on-demand g and vt" || echo "${qname_l}" | grep -q "running on-demand g and vt instances" ; then
    desired="${REQ_INSTANCES_WITH_MARGIN}"
  elif echo "${qname_l}" | grep -q "running on-demand" && ( echo "${qname_l}" | grep -q "g" || echo "${qname_l}" | grep -q "p" ); then
    desired="${REQ_INSTANCES_WITH_MARGIN}"
  elif echo "${qname_l}" | grep -q "running dedicated" && ( echo "${qname_l}" | grep -q "g" || echo "${qname_l}" | grep -q "${INSTANCE_TYPE%%.*}" ); then
    desired="${REQ_INSTANCES_WITH_MARGIN}"
  else
    # conservative default -> request instance blocks
    desired="${REQ_INSTANCES_WITH_MARGIN}"
  fi

  # final sanity: desired must be positive integer
  if ! [[ "${desired}" =~ ^[0-9]+$ ]] || [ "${desired}" -le 0 ]; then
    echo "Skipping ${code} (${qname}): computed invalid desired='${desired}'"
    continue
  fi

  to_request_cmds+=("${AWS} service-quotas request-service-quota-increase --service-code ec2 --quota-code ${code} --desired-value ${desired}")
  to_request_codes+=("${code}|||${desired}|||${qname}")
done

if [[ ${#to_request_cmds[@]} -eq 0 ]]; then
  err "no valid quota requests computed"
fi

info "Suggested commands:"
for idx in "${!to_request_cmds[@]}"; do
  code=$(echo "${to_request_codes[idx]}" | cut -d '|' -f1)
  desired=$(echo "${to_request_codes[idx]}" | cut -d '|' -f2)
  qname=$(echo "${to_request_codes[idx]}" | cut -d '|' -f3)
  printf " %s    # %s -> desired=%s\n" "${to_request_cmds[idx]}" "${qname}" "${desired}"
done
info ""

if [[ "${APPLY_FLAG}" == "--apply" || "${APPLY_FLAG}" == "apply" || "${APPLY_FLAG}" == "-y" ]]; then
  info "Applying requests now..."
  for idx in "${!to_request_cmds[@]}"; do
    cmd="${to_request_cmds[idx]}"
    code=$(echo "${to_request_codes[idx]}" | cut -d '|' -f1)
    desired=$(echo "${to_request_codes[idx]}" | cut -d '|' -f2)
    qname=$(echo "${to_request_codes[idx]}" | cut -d '|' -f3)
    info "Requesting ${code} (${qname}) -> desired=${desired} ..."
    set +e
    out=$(${cmd} 2>&1) || true
    rc=$?
    set -e
    if [ ${rc} -ne 0 ]; then
      echo "Request failed for ${code}:"
      echo "${out}"
      echo "You can re-run the printed command manually."
    else
      # successful - parse RequestId
      rid=$(echo "${out}" | jq -r '.RequestedQuotaChange.RequestedQuotaChangeId // empty' 2>/dev/null || true)
      if [[ -n "${rid}" ]]; then
        echo "Submitted request ${rid} for ${code} -> desired=${desired}"
      else
        echo "Submitted request for ${code}. Response:"
        echo "${out}"
      fi
    fi
  done
  info ""
  info "List recent quota change requests (may be empty while pending):"
  ${AWS} service-quotas list-requested-service-quota-change-history --service-code ec2 --max-items 50 || true
else
  info "Dry-run: not submitted. Re-run with --apply to submit requests."
fi

exit 0
