# c_ray_head.py
from __future__ import annotations
import os
import json
from typing import Optional, List, Dict, Any
import pulumi
import pulumi_aws as aws

STACK = pulumi.get_stack()
cfg = pulumi.Config()

# Try to import a_prereqs_networking outputs if run in same process
try:
    import a_prereqs_networking as netmod
    pulumi.log.info("Using a_prereqs_networking outputs")
    PRIVATE_SUBNET_IDS = getattr(netmod, "private_subnet_ids_out", []) or []
    HEAD_SG_ID = getattr(netmod, "head_security_group_id_out", None)
    VPC_ID = getattr(netmod, "vpc_id_out", None)
except Exception:
    PRIVATE_SUBNET_IDS = (os.getenv("PRIVATE_SUBNET_IDS") or cfg.get("privateSubnetIds") or "")
    PRIVATE_SUBNET_IDS = [s.strip() for s in PRIVATE_SUBNET_IDS.split(",") if s.strip()]
    HEAD_SG_ID = os.getenv("HEAD_SECURITY_GROUP_ID") or cfg.get("headSecurityGroupId")
    VPC_ID = os.getenv("VPC_ID") or cfg.get("vpcId")

HOSTED_ZONE_ID = os.getenv("PRIVATE_HOSTED_ZONE_ID") or cfg.get("privateHostedZoneId") or ""
HOSTED_ZONE_NAME = os.getenv("PRIVATE_HOSTED_ZONE_NAME") or cfg.get("privateHostedZoneName") or "internal"
HEAD_AMI = os.getenv("HEAD_AMI") or cfg.get("headAmi") or ""
HEAD_INSTANCE_TYPE = os.getenv("HEAD_INSTANCE_TYPE") or cfg.get("headInstanceType") or "m5.large"
KEY_NAME = os.getenv("KEY_NAME") or cfg.get("keyName") or ""
INSTANCE_PROFILE_NAME = os.getenv("RAY_HEAD_INSTANCE_PROFILE") or cfg.get("rayHeadInstanceProfile") or ""
REDIS_SSM_PARAM = os.getenv("REDIS_SSM_PARAM") or cfg.get("redisSsmParam") or "/ray/prod/redis_password"
RAY_REDIS_PORT = int(os.getenv("RAY_REDIS_PORT") or cfg.get_int("rayRedisPort") or 6379)

# ensure instance profile exists or create one with scoped permissions
def ensure_head_instance_profile(profile_name: Optional[str]) -> str:
    if profile_name:
        try:
            ip = aws.iam.get_instance_profile(profile_name)
            return ip.name
        except Exception:
            pulumi.log.warn(f"Instance profile {profile_name} not found; creating new")
    role = aws.iam.Role("ray-head-role", assume_role_policy=json.dumps({"Version":"2012-10-17","Statement":[{"Action":"sts:AssumeRole","Principal":{"Service":"ec2.amazonaws.com"},"Effect":"Allow"}]}))
    aws.iam.RolePolicyAttachment("head-ssm-attach", role=role.name, policy_arn="arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore")
    aws.iam.RolePolicyAttachment("head-cw-attach", role=role.name, policy_arn="arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy")

    # scoped SSM + KMS policy for the redis param and decrypt (restricting to param ARN)
    statements = []
    if REDIS_SSM_PARAM:
        ssm_param_arn = f"arn:aws:ssm:{aws.get_region().name}:{aws.get_caller_identity().account_id}:parameter{REDIS_SSM_PARAM}"
        statements.append({"Effect":"Allow","Action":["ssm:GetParameter","ssm:GetParameters"],"Resource":[ssm_param_arn]})
    # allow kms:Decrypt on the KMS key used in a_prereqs; we use cfg to retrieve arn if provided
    kms_arn = getattr(netmod, "kms_key_arn_out", None) if 'netmod' in globals() else cfg.get("kmsKeyArn")
    if kms_arn:
        statements.append({"Effect":"Allow","Action":["kms:Decrypt","kms:DescribeKey"],"Resource":[kms_arn]})
    if statements:
        pol = aws.iam.Policy("ray-head-inline-policy", policy=json.dumps({"Version":"2012-10-17","Statement":statements}))
        aws.iam.PolicyAttachment("ray-head-inline-attach", policy_arn=pol.arn, roles=[role.name])

    ip = aws.iam.InstanceProfile("ray-head-ip", role=role.name)
    return ip.name

instance_profile_name = ensure_head_instance_profile(INSTANCE_PROFILE_NAME)

# choose first private subnet for ENI
head_subnet = PRIVATE_SUBNET_IDS[0] if PRIVATE_SUBNET_IDS else None

eni = None
if head_subnet:
    eni = aws.ec2.NetworkInterface("ray-head-eni", subnet_id=head_subnet, description=f"ray-head-eni-{STACK}", security_groups=[HEAD_SG_ID] if HEAD_SG_ID else None, tags={"Name": f"ray-head-eni-{STACK}"})
    pulumi.export("ray_head_eni_id", eni.id)
else:
    pulumi.log.warn("No private subnet provided for head ENI")

# autoscaler YAML embedded (simplified but editable)
AUTOSCALER_TEMPLATE = """cluster_name: "ray-{stack}"
min_workers: 0
max_workers: 4
idle_timeout_minutes: 10
provider:
  type: aws
  region: "{region}"
auth:
  ssh_user: "ubuntu"
head_node:
  InstanceType: "{head_instance_type}"
  ImageId: "{head_ami}"
  IamInstanceProfile:
    Name: "{instance_profile}"
  KeyName: "{key_name}"
  SubnetId: "{subnet}"
available_node_types:
  ray.head.default:
    node_config:
      InstanceType: "{head_instance_type}"
      ImageId: "{head_ami}"
      IamInstanceProfile:
        Name: "{instance_profile}"
      KeyName: "{key_name}"
      SubnetId: "{subnet}"
    max_workers: 0
    resources: {"CPU": 4}
  ray.worker.cpu:
    node_config:
      InstanceType: "m5.xlarge"
      ImageId: "{head_ami}"
      IamInstanceProfile:
        Name: "{instance_profile}"
      KeyName: "{key_name}"
      SubnetId: "{subnet}"
    min_workers: 0
    max_workers: 4
    resources: {"CPU": 8}
head_node_type: ray.head.default
worker_default_node_type: ray.worker.cpu
"""

autoscaler_yaml = AUTOSCALER_TEMPLATE.format(stack=STACK, region=aws.get_region().name, head_instance_type=HEAD_INSTANCE_TYPE, head_ami=HEAD_AMI, instance_profile=instance_profile_name, key_name=KEY_NAME or "", subnet=head_subnet or "")

# user data script for head; reads SSM for redis, writes autoscaler YAML to /etc/ray/autoscaler.yaml
user_data_template = r"""#!/bin/bash
set -euo pipefail
apt-get update -y
apt-get install -y python3-pip awscli jq curl ca-certificates
python3 -m pip install --upgrade pip
python3 -m pip install "ray[default]==2.5.0"

mkdir -p /etc/ray
cat > /etc/ray/autoscaler.yaml <<'AUTOSCALER'
{AUTOSCALER}
AUTOSCALER

REGION="{REGION}"
SSM_PARAM="{REDIS_SSM_PARAM}"
REDIS_PASSWORD=""
if command -v aws >/dev/null 2>&1; then
  REDIS_PASSWORD=$(aws ssm get-parameter --name "$SSM_PARAM" --with-decryption --region "$REGION" --query 'Parameter.Value' --output text 2>/dev/null || echo "")
fi
PRIVATE_IP=$(curl -s http://169.254.169.254/latest/meta-data/local-ipv4 || echo "")
if [ -n "$REDIS_PASSWORD" ]; then
  ray start --head --port={RAY_REDIS_PORT} --node-ip-address="$PRIVATE_IP" --redis-password="$REDIS_PASSWORD" --autoscaling-config=/etc/ray/autoscaler.yaml --include-dashboard False --metrics-export-port=8080 || true
else
  ray start --head --port={RAY_REDIS_PORT} --node-ip-address="$PRIVATE_IP" --autoscaling-config=/etc/ray/autoscaler.yaml --include-dashboard False --metrics-export-port=8080 || true
fi
"""

user_data = user_data_template.format(AUTOSCALER=autoscaler_yaml.replace("'", "'\\''"), REGION=aws.get_region().name, REDIS_SSM_PARAM=REDIS_SSM_PARAM, RAY_REDIS_PORT=RAY_REDIS_PORT)

# create instance (attach ENI if present)
if eni:
    head_instance = aws.ec2.Instance("ray-head-instance", ami=HEAD_AMI, instance_type=HEAD_INSTANCE_TYPE, iam_instance_profile=instance_profile_name, key_name=KEY_NAME if KEY_NAME else None, network_interfaces=[aws.ec2.InstanceNetworkInterfaceArgs(network_interface_id=eni.id, device_index=0)], user_data=user_data, tags={"Name": f"ray-head-{STACK}"})
else:
    head_instance = aws.ec2.Instance("ray-head-instance", ami=HEAD_AMI, instance_type=HEAD_INSTANCE_TYPE, subnet_id=head_subnet, iam_instance_profile=instance_profile_name, key_name=KEY_NAME if KEY_NAME else None, user_data=user_data, tags={"Name": f"ray-head-{STACK}"})

pulumi.export("ray_head_instance_id", head_instance.id)
pulumi.export("ray_head_private_ip", head_instance.private_ip)

# internal NLB + TG + attach to head
if PRIVATE_SUBNET_IDS:
    if not VPC_ID:
        raise pulumi.ResourceError("vpc_id is required to create NLB target group; set via a_prereqs_networking or VPC_ID config", resource=None)

    nlb = aws.lb.LoadBalancer("ray-head-nlb", internal=True, load_balancer_type="network", subnets=PRIVATE_SUBNET_IDS, tags={"Name": f"ray-head-nlb-{STACK}"})
    tg = aws.lb.TargetGroup("ray-head-tg", port=RAY_REDIS_PORT, protocol="TCP", target_type="instance", vpc_id=VPC_ID, health_check=aws.lb.TargetGroupHealthCheckArgs(protocol="TCP", port=str(RAY_REDIS_PORT), interval=10, healthy_threshold=2, unhealthy_threshold=2))
    listener = aws.lb.Listener("ray-head-nlb-listener", load_balancer_arn=nlb.arn, port=RAY_REDIS_PORT, protocol="TCP", default_actions=[aws.lb.ListenerDefaultActionArgs(type="forward", target_group_arn=tg.arn)])
    attach = aws.lb.TargetGroupAttachment("ray-head-tg-attach", target_group_arn=tg.arn, target_id=head_instance.id, port=RAY_REDIS_PORT)
    pulumi.export("ray_head_nlb_dns", nlb.dns_name)
    pulumi.export("ray_head_tg_arn", tg.arn)
    if HOSTED_ZONE_ID and HOSTED_ZONE_NAME:
        fqdn = f"ray-head.{STACK}.{HOSTED_ZONE_NAME}"
        record = aws.route53.Record("ray-head-record", zone_id=HOSTED_ZONE_ID, name=fqdn, type="A", aliases=[aws.route53.RecordAliasArgs(name=nlb.dns_name, zone_id=nlb.zone_id, evaluate_target_health=False)])
        pulumi.export("ray_head_fqdn", fqdn)
else:
    pulumi.log.info("Skipping NLB: no private subnets provided")

pulumi.log.info("c_ray_head.py completed")
