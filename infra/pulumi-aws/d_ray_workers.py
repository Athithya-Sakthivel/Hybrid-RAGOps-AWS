# d_ray_workers.py
from __future__ import annotations
import os
import json
import time
from typing import List, Optional, Dict, Any
import pulumi
import pulumi_aws as aws

STACK = pulumi.get_stack()
cfg = pulumi.Config()

try:
    import a_prereqs_networking as netmod
    pulumi.log.info("Using a_prereqs_networking outputs in workers module")
    PRIVATE_SUBNET_IDS = getattr(netmod, "private_subnet_ids_out", []) or []
    WORKER_SG_ID = getattr(netmod, "worker_security_group_id_out", None)
except Exception:
    PRIVATE_SUBNET_IDS = (os.getenv("PRIVATE_SUBNET_IDS") or cfg.get("privateSubnetIds") or "")
    PRIVATE_SUBNET_IDS = [s.strip() for s in PRIVATE_SUBNET_IDS.split(",") if s.strip()]
    WORKER_SG_ID = os.getenv("WORKER_SECURITY_GROUP_ID") or cfg.get("workerSecurityGroupId") or ""

RAY_HEAD_FQDN = os.getenv("RAY_HEAD_FQDN") or cfg.get("rayHeadFqdn") or f"ray-head.{STACK}.{os.getenv('PRIVATE_HOSTED_ZONE_NAME') or cfg.get('privateHostedZoneName') or 'internal'}"
REDIS_SSM_PARAM = os.getenv("REDIS_SSM_PARAM") or cfg.get("redisSsmParam") or "/ray/prod/redis_password"
CPU_AMI = os.getenv("RAY_CPU_AMI") or cfg.get("rayCpuAmi") or ""
CPU_INSTANCE_TYPE = os.getenv("RAY_CPU_INSTANCE") or cfg.get("rayCpuInstance") or "m5.xlarge"
CPU_INSTANCE_PROFILE = os.getenv("RAY_CPU_INSTANCE_PROFILE") or cfg.get("rayCpuInstanceProfile") or ""
KEY_NAME = os.getenv("KEY_NAME") or cfg.get("keyName") or ""
MIN_WORKERS = int(os.getenv("RAY_CPU_MIN_WORKERS") or cfg.get_int("rayCpuMinWorkers") or 1)
MAX_WORKERS = int(os.getenv("RAY_CPU_MAX_WORKERS") or cfg.get_int("rayCpuMaxWorkers") or 4)
RAY_REDIS_PORT = int(os.getenv("RAY_REDIS_PORT") or cfg.get_int("rayRedisPort") or 6379)

def ensure_worker_instance_profile(profile_name: Optional[str]) -> str:
    if profile_name:
        try:
            ip = aws.iam.get_instance_profile(profile_name)
            return ip.name
        except Exception:
            pulumi.log.warn(f"Worker instance profile {profile_name} not found; creating new")
    role = aws.iam.Role("ray-worker-role", assume_role_policy=json.dumps({"Version":"2012-10-17","Statement":[{"Action":"sts:AssumeRole","Principal":{"Service":"ec2.amazonaws.com"},"Effect":"Allow"}]}))
    aws.iam.RolePolicyAttachment("worker-ssm-attach", role=role.name, policy_arn="arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore")
    aws.iam.RolePolicyAttachment("worker-cw-attach", role=role.name, policy_arn="arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy")
    # minimal SSM access to the specific redis param
    statements = []
    if REDIS_SSM_PARAM:
        ssm_arn = f"arn:aws:ssm:{aws.get_region().name}:{aws.get_caller_identity().account_id}:parameter{REDIS_SSM_PARAM}"
        statements.append({"Effect":"Allow","Action":["ssm:GetParameter","ssm:GetParameters"],"Resource":[ssm_arn]})
    if statements:
        pol = aws.iam.Policy("ray-worker-inline-policy", policy=json.dumps({"Version":"2012-10-17","Statement":statements}))
        aws.iam.PolicyAttachment("ray-worker-inline-attach", policy_arn=pol.arn, roles=[role.name])
    ip = aws.iam.InstanceProfile("ray-worker-ip", role=role.name)
    return ip.name

worker_instance_profile = ensure_worker_instance_profile(CPU_INSTANCE_PROFILE)

# worker user-data (joins head, with exponential backoff and drain script)
worker_user_data_t = r"""#!/bin/bash
set -euo pipefail
apt-get update -y
apt-get install -y python3-pip awscli jq curl ca-certificates
python3 -m pip install --upgrade pip
python3 -m pip install "ray[default]==2.5.0" httpx

SSM_PARAM="{{REDIS_SSM_PARAM}}"
RAY_HEAD_FQDN="{{RAY_HEAD_FQDN}}"
RAY_REDIS_PORT={{RAY_REDIS_PORT}}
REGION="{{REGION}}"

cat > /usr/local/bin/ray_node_drain.sh <<'DRN'
#!/bin/bash
set -euo pipefail
TIMEOUT=${1:-300}
logger -t ray-node-drain "drain start (timeout=${TIMEOUT}s)"
if command -v curl >/dev/null 2>&1; then
  curl -s -m 5 -X POST "http://{{RAY_HEAD_FQDN}}:8003/internal/drain-node" -H "Content-Type: application/json" -d "{\"instance_id\":\"$(curl -s http://169.254.169.254/latest/meta-data/instance-id || echo '')\"}" || true
fi
END=$((SECONDS + TIMEOUT))
while [ $SECONDS -lt $END ]; do
  if ! ray status --address "auto" 2>/dev/null | grep -q "Cluster status: ALIVE"; then
    break
  fi
  sleep 2
done
ray stop --force || true
logger -t ray-node-drain "drain complete"
DRN
chmod +x /usr/local/bin/ray_node_drain.sh

cat > /usr/local/bin/ray_worker_join.sh <<'JOIN'
#!/bin/bash
set -euo pipefail
LOG_TAG="ray-worker-join"
SSM_PARAM="{{SSM_PARAM}}"
RAY_HEAD_FQDN="{{RAY_HEAD_FQDN}}"
RAY_REDIS_PORT={{RAY_REDIS_PORT}}
REGION="{{REGION}}"
BASE_DELAY=5
BACKOFF_FACTOR=2
MAX_DELAY=300

get_imds_token() {
  curl -s -X PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds:21600" || true
}
IMDS_TOKEN="$(get_imds_token)"
if [ -n "${IMDS_TOKEN}" ]; then
  PRIVATE_IP="$(curl -s -H "X-aws-ec2-metadata-token: ${IMDS_TOKEN}" http://169.254.169.254/latest/meta-data/local-ipv4 || true)"
else
  PRIVATE_IP="$(curl -s http://169.254.169.254/latest/meta-data/local-ipv4 || true)"
fi

log() { logger -t "${LOG_TAG}" -- "$@"; echo "$(date -Is) ${LOG_TAG}: $*"; }

terminate() {
  log "SIGTERM received, running drain"
  /usr/local/bin/ray_node_drain.sh 300 || true
  ray stop --force || true
  log "exiting"
  exit 0
}
trap terminate SIGTERM

fetch_redis_password() {
  if command -v aws >/dev/null 2>&1; then
    aws ssm get-parameter --name "${SSM_PARAM}" --with-decryption --region "${REGION}" --query 'Parameter.Value' --output text 2>/dev/null || echo ""
  else
    echo ""
  fi
}

wait_for_tcp() {
  host="$1"
  port="$2"
  timeout="${3:-5}"
  if command -v nc >/dev/null 2>&1; then
    nc -z -w ${timeout} "${host}" "${port}" >/dev/null 2>&1
    return $?
  fi
  if exec 3>/dev/tcp/${host}/${port} 2>/dev/null; then
    exec 3>&-
    return 0
  fi
  return 1
}

attempt=0
delay=${BASE_DELAY}
while true; do
  attempt=$((attempt+1))
  log "attempt ${attempt} checking ${RAY_HEAD_FQDN}:${RAY_REDIS_PORT}"
  if ! wait_for_tcp "${RAY_HEAD_FQDN}" "${RAY_REDIS_PORT}" 5; then
    log "head not reachable; sleeping ${delay}s"
    sleep ${delay}
    delay=$(( delay * BACKOFF_FACTOR ))
    [ ${delay} -gt ${MAX_DELAY} ] && delay=${MAX_DELAY}
    continue
  fi
  REDIS_PASSWORD="$(fetch_redis_password)"
  set +e
  ray stop || true
  set -e
  if [ -n "${REDIS_PASSWORD}" ]; then
    ray start --address="${RAY_HEAD_FQDN}:${RAY_REDIS_PORT}" --node-ip-address="${PRIVATE_IP}" --redis-password="${REDIS_PASSWORD}" --metrics-export-port=8080
  else
    ray start --address="${RAY_HEAD_FQDN}:${RAY_REDIS_PORT}" --node-ip-address="${PRIVATE_IP}" --metrics-export-port=8080
  fi
  rc=$?
  if [ "${rc}" -eq 0 ]; then
    for i in 1 2 3; do
      sleep 2
      if ray status --address "auto" 2>/dev/null | grep -q "Cluster status: ALIVE"; then
        log "worker joined"
        touch /var/run/ray-ready || true
        wait
      fi
    done
    log "start returned but cluster not ready; retrying"
  else
    log "ray start rc=${rc}; retrying"
  fi
  sleep ${delay}
  delay=$(( delay * BACKOFF_FACTOR ))
  [ ${delay} -gt ${MAX_DELAY} ] && delay=${MAX_DELAY}
done
JOIN
chmod +x /usr/local/bin/ray_worker_join.sh

cat > /etc/systemd/system/ray-worker.service <<'UNIT'
[Unit]
Description=Ray Worker (managed reconnect)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/ray_worker_join.sh
ExecStop=/usr/local/bin/ray_node_drain.sh
Restart=on-failure
RestartSec=30
TimeoutStartSec=300
StartLimitBurst=5
StartLimitIntervalSec=600

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now ray-worker.service

# substitute template variables
worker_user_data = worker_user_data_t.replace("{{REDIS_SSM_PARAM}}", REDIS_SSM_PARAM).replace("{{RAY_HEAD_FQDN}}", RAY_HEAD_FQDN).replace("{{RAY_REDIS_PORT}}", str(RAY_REDIS_PORT)).replace("{{REGION}}", aws.get_region().name).replace("{{SSM_PARAM}}", REDIS_SSM_PARAM)

# Launch template and ASG
network_interfaces = None
if PRIVATE_SUBNET_IDS:
    network_interfaces = [aws.ec2.LaunchTemplateNetworkInterfaceArgs(subnet_id=PRIVATE_SUBNET_IDS[0], security_groups=[WORKER_SG_ID] if WORKER_SG_ID else None)]

lt = aws.ec2.LaunchTemplate("ray-worker-lt", name_prefix=f"ray-worker-lt-{STACK}", image_id=CPU_AMI, instance_type=CPU_INSTANCE_TYPE, iam_instance_profile=aws.ec2.LaunchTemplateIamInstanceProfileArgs(name=worker_instance_profile), key_name=KEY_NAME if KEY_NAME else None, network_interfaces=network_interfaces or None, user_data=worker_user_data)

asg = aws.autoscaling.Group("ray-worker-asg", desired_capacity=MIN_WORKERS, min_size=MIN_WORKERS, max_size=MAX_WORKERS, vpc_zone_identifiers=PRIVATE_SUBNET_IDS, launch_template=aws.autoscaling.GroupLaunchTemplateArgs(id=lt.id, version="$Latest"), tags=[aws.autoscaling.GroupTagArgs(key="Name", value=f"ray-worker-{STACK}", propagate_at_launch=True)])

# SNS topic + lifecycle hook
topic = aws.sns.Topic("ray-worker-life-topic")
lifecycle = aws.autoscaling.LifecycleHook("ray-worker-terminate-hook", autoscaling_group_name=asg.name, lifecycle_transition="autoscaling:EC2_INSTANCE_TERMINATING", default_result="ABANDON", heartbeat_timeout=600, notification_target_arn=topic.arn)

# Lambda to handle lifecycle: package using AssetArchive
lambda_role = aws.iam.Role("ray-drain-lambda-role", assume_role_policy=json.dumps({"Version":"2012-10-17","Statement":[{"Action":"sts:AssumeRole","Principal":{"Service":"lambda.amazonaws.com"},"Effect":"Allow"}]}))
aws.iam.RolePolicyAttachment("lambda-basic", role=lambda_role.name, policy_arn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole")
# minimal permissions for SSM SendCommand and ASG CompleteLifecycleAction (resource scoping can be tightened)
lambda_policy = {
    "Version": "2012-10-17",
    "Statement": [
        {"Effect":"Allow","Action":["ssm:SendCommand","ssm:GetCommandInvocation","ssm:ListCommandInvocations","ssm:ListCommands"], "Resource":"*"},
        {"Effect":"Allow","Action":["autoscaling:CompleteLifecycleAction"], "Resource":"*"},
        {"Effect":"Allow","Action":["ec2:DescribeInstances"], "Resource":"*"},
        {"Effect":"Allow","Action":["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents"], "Resource":"arn:aws:logs:*:*:*"}
    ]
}
lambda_pol = aws.iam.Policy("ray-drain-lambda-inline", policy=json.dumps(lambda_policy))
aws.iam.PolicyAttachment("ray-drain-lambda-attach", policy_arn=lambda_pol.arn, roles=[lambda_role.name])

lambda_code = r"""
import json, boto3, time
ssm = boto3.client('ssm')
asg = boto3.client('autoscaling')

def handler(event, context):
    try:
        records = event.get('Records', [])
        for r in records:
            msg = r.get('Sns', {}).get('Message')
            if not msg:
                continue
            payload = json.loads(msg)
            # attempt parse fields
            instance_id = payload.get('EC2InstanceId') or payload.get('detail',{}).get('EC2InstanceId') or payload.get('InstanceId')
            hook_name = payload.get('LifecycleHookName') or payload.get('detail',{}).get('LifecycleHookName')
            asg_name = payload.get('AutoScalingGroupName') or payload.get('detail',{}).get('AutoScalingGroupName')
            if not instance_id or not hook_name or not asg_name:
                print("Missing lifecycle notification fields; ignoring", payload)
                continue
            print(f"Draining instance {instance_id} in ASG {asg_name} hook {hook_name}")
            resp = ssm.send_command(InstanceIds=[instance_id], DocumentName="AWS-RunShellScript", Parameters={"commands":["/usr/local/bin/ray_node_drain.sh 300"]}, TimeoutSeconds=600)
            cmd_id = resp['Command']['CommandId']
            ok = False
            for _ in range(60):
                try:
                    inv = ssm.get_command_invocation(CommandId=cmd_id, InstanceId=instance_id)
                    status = inv.get('Status')
                    print("Invocation status", status)
                    if status in ('Success','Failed','TimedOut','Cancelled'):
                        ok = status == 'Success'
                        break
                except Exception as e:
                    print("get_command_invocation error", e)
                time.sleep(5)
            result = 'CONTINUE' if ok else 'ABANDON'
            print("Completing lifecycle with", result)
            asg.complete_lifecycle_action(AutoScalingGroupName=asg_name, LifecycleHookName=hook_name, InstanceId=instance_id, LifecycleActionResult=result)
    except Exception as e:
        print("Handler error", e)
        raise
"""

lambda_asset = pulumi.AssetArchive({
    "index.py": pulumi.StringAsset(lambda_code)
})

z = aws.lambda_.Function("ray-drain-lambda", runtime="python3.10", role=lambda_role.arn, handler="index.handler", code=lambda_asset, timeout=900)

perm = aws.lambda_.Permission("allow-sns-invoke", action="lambda:InvokeFunction", function=z.arn, principal="sns.amazonaws.com", source_arn=topic.arn)
sub = aws.sns.TopicSubscription("topic-sub", topic=topic.arn, protocol="lambda", endpoint=z.arn)

pulumi.export("ray_worker_launch_template_id", lt.id)
pulumi.export("ray_worker_asg_name", asg.name)
pulumi.export("ray_worker_lifecycle_topic", topic.arn)
pulumi.export("ray_worker_lifecycle_hook", lifecycle.name)
pulumi.log.info("d_ray_workers.py completed")
