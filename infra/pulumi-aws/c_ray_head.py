# c_ray_head.py
from __future__ import annotations
import os
import json
from typing import Optional, List, Dict, Any
import pulumi
import pulumi_aws as aws

STACK = pulumi.get_stack()
cfg = pulumi.Config()

PRIVATE_SUBNET_IDS = (os.getenv("PRIVATE_SUBNET_IDS") or cfg.get("privateSubnetIds") or "")
PRIVATE_SUBNET_IDS = [s.strip() for s in PRIVATE_SUBNET_IDS.split(",") if s.strip()]
HEAD_SG_ID = os.getenv("HEAD_SECURITY_GROUP_ID") or cfg.get("headSecurityGroupId")
HOSTED_ZONE_ID = os.getenv("PRIVATE_HOSTED_ZONE_ID") or cfg.get("privateHostedZoneId") or ""
HOSTED_ZONE_NAME = os.getenv("PRIVATE_HOSTED_ZONE_NAME") or cfg.get("privateHostedZoneName") or "internal"
HEAD_AMI = os.getenv("HEAD_AMI") or cfg.get("headAmi") or ""
HEAD_INSTANCE_TYPE = os.getenv("HEAD_INSTANCE_TYPE") or cfg.get("headInstanceType") or "m5.large"
KEY_NAME = os.getenv("KEY_NAME") or cfg.get("keyName") or ""
INSTANCE_PROFILE_NAME = os.getenv("RAY_HEAD_INSTANCE_PROFILE") or cfg.get("rayHeadInstanceProfile") or ""
REDIS_SSM_PARAM = os.getenv("REDIS_SSM_PARAM") or cfg.get("redisSsmParam") or "/ray/prod/redis_password"
RAY_REDIS_PORT = int(os.getenv("RAY_REDIS_PORT") or cfg.get_int("rayRedisPort") or 6379)

def ensure_head_instance_profile(profile_name: Optional[str]) -> str:
    if profile_name:
        try:
            ip = aws.iam.get_instance_profile(profile_name)
            return ip.name
        except Exception:
            pulumi.log.warn(f"Instance profile {profile_name} not found; creating new")
    role = aws.iam.Role(f"ray-head-role-{STACK}", assume_role_policy=json.dumps({"Version":"2012-10-17","Statement":[{"Action":"sts:AssumeRole","Principal":{"Service":"ec2.amazonaws.com"},"Effect":"Allow"}]}))
    aws.iam.RolePolicyAttachment(f"head-ssm-attach-{STACK}", role=role.name, policy_arn="arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore")
    aws.iam.RolePolicyAttachment(f"head-cw-attach-{STACK}", role=role.name, policy_arn="arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy")
    # scoped ssm policy
    statements = []
    if REDIS_SSM_PARAM:
        ssm_arn = f"arn:aws:ssm:{aws.get_region().name}:{aws.get_caller_identity().account_id}:parameter{REDIS_SSM_PARAM}"
        statements.append({"Effect":"Allow","Action":["ssm:GetParameter","ssm:GetParameters"],"Resource":ssm_arn})
    if statements:
        pol = aws.iam.Policy(f"ray-head-inline-policy-{STACK}", policy=json.dumps({"Version":"2012-10-17","Statement":statements}))
        aws.iam.PolicyAttachment(f"ray-head-inline-attach-{STACK}", policy_arn=pol.arn, roles=[role.name])
    ip = aws.iam.InstanceProfile(f"ray-head-ip-{STACK}", role=role.name)
    return ip.name

instance_profile_name = ensure_head_instance_profile(INSTANCE_PROFILE_NAME)

head_subnet = PRIVATE_SUBNET_IDS[0] if PRIVATE_SUBNET_IDS else None

eni = None
if head_subnet:
    eni = aws.ec2.NetworkInterface(f"ray-head-eni-{STACK}", subnet_id=head_subnet, description=f"ray-head-eni-{STACK}", security_groups=[HEAD_SG_ID] if HEAD_SG_ID else None, tags={"Name": f"ray-head-eni-{STACK}"})
    pulumi.export("ray_head_eni_id", eni.id)
else:
    pulumi.log.warn("No private subnet provided")

# embed autoscaler YAML as string, safe substitution via replace
AUTOSCALER_TEMPLATE = """cluster_name: "ray-{{STACK}}"
min_workers: 0
max_workers: 4
idle_timeout_minutes: 10
provider:
  type: aws
  region: "{{REGION}}"
auth:
  ssh_user: "ubuntu"
head_node:
  InstanceType: "{{HEAD_INSTANCE_TYPE}}"
  ImageId: "{{HEAD_AMI}}"
  IamInstanceProfile:
    Name: "{{INSTANCE_PROFILE}}"
  KeyName: "{{KEY_NAME}}"
  SubnetId: "{{SUBNET}}"
available_node_types:
  ray.head.default:
    node_config:
      InstanceType: "{{HEAD_INSTANCE_TYPE}}"
      ImageId: "{{HEAD_AMI}}"
      IamInstanceProfile:
        Name: "{{INSTANCE_PROFILE}}"
      KeyName: "{{KEY_NAME}}"
      SubnetId: "{{SUBNET}}"
    max_workers: 0
    resources: {"CPU": 4}
  ray.worker.cpu:
    node_config:
      InstanceType: "m5.xlarge"
      ImageId: "{{HEAD_AMI}}"
      IamInstanceProfile:
        Name: "{{INSTANCE_PROFILE}}"
      KeyName: "{{KEY_NAME}}"
      SubnetId: "{{SUBNET}}"
    min_workers: 0
    max_workers: 4
    resources: {"CPU": 8}
head_node_type: ray.head.default
worker_default_node_type: ray.worker.cpu
"""

autoscaler_yaml = (AUTOSCALER_TEMPLATE.replace("{{STACK}}", STACK)
                                   .replace("{{REGION}}", aws.get_region().name)
                                   .replace("{{HEAD_INSTANCE_TYPE}}", HEAD_INSTANCE_TYPE)
                                   .replace("{{HEAD_AMI}}", HEAD_AMI)
                                   .replace("{{INSTANCE_PROFILE}}", instance_profile_name)
                                   .replace("{{KEY_NAME}}", KEY_NAME)
                                   .replace("{{SUBNET}}", head_subnet or ""))

user_data_template = r"""#!/bin/bash
set -euo pipefail
apt-get update -y
apt-get install -y python3-pip awscli jq curl ca-certificates
python3 -m pip install --upgrade pip
python3 -m pip install "ray[default]==2.5.0"

mkdir -p /etc/ray
cat > /etc/ray/autoscaler.yaml <<'AUTOSCALER'
{{AUTOSCALER_YAML}}
AUTOSCALER

REGION="{{REGION}}"
SSM_PARAM="{{REDIS_SSM_PARAM}}"
REDIS_PASSWORD=""
if command -v aws >/dev/null 2>&1; then
  REDIS_PASSWORD=$(aws ssm get-parameter --name "$SSM_PARAM" --with-decryption --region "$REGION" --query 'Parameter.Value' --output text 2>/dev/null || echo "")
fi
PRIVATE_IP=$(curl -s http://169.254.169.254/latest/meta-data/local-ipv4 || echo "")
if [ -n "$REDIS_PASSWORD" ]; then
  ray start --head --port={{RAY_REDIS_PORT}} --node-ip-address="$PRIVATE_IP" --redis-password="$REDIS_PASSWORD" --autoscaling-config=/etc/ray/autoscaler.yaml --include-dashboard False --metrics-export-port=8080 || true
else
  ray start --head --port={{RAY_REDIS_PORT}} --node-ip-address="$PRIVATE_IP" --autoscaling-config=/etc/ray/autoscaler.yaml --include-dashboard False --metrics-export-port=8080 || true
fi
"""

user_data = user_data_template.replace("{{AUTOSCALER_YAML}}", autoscaler_yaml.replace("'", "'\\''")).replace("{{REGION}}", aws.get_region().name).replace("{{REDIS_SSM_PARAM}}", REDIS_SSM_PARAM).replace("{{RAY_REDIS_PORT}}", str(RAY_REDIS_PORT))

if eni:
    head_instance = aws.ec2.Instance(f"ray-head-instance-{STACK}", ami=HEAD_AMI, instance_type=HEAD_INSTANCE_TYPE, iam_instance_profile=instance_profile_name, key_name=KEY_NAME if KEY_NAME else None, network_interfaces=[aws.ec2.InstanceNetworkInterfaceArgs(network_interface_id=eni.id, device_index=0)], user_data=user_data, tags={"Name": f"ray-head-{STACK}"})
else:
    head_instance = aws.ec2.Instance(f"ray-head-instance-{STACK}", ami=HEAD_AMI, instance_type=HEAD_INSTANCE_TYPE, subnet_id=head_subnet, iam_instance_profile=instance_profile_name, key_name=KEY_NAME if KEY_NAME else None, user_data=user_data, tags={"Name": f"ray-head-{STACK}"})

pulumi.export("ray_head_instance_id", head_instance.id)
pulumi.export("ray_head_private_ip", head_instance.private_ip)

# internal NLB + TG + listener + register instance target
if PRIVATE_SUBNET_IDS:
    nlb = aws.lb.LoadBalancer(f"ray-head-nlb-{STACK}", internal=True, load_balancer_type="network", subnets=PRIVATE_SUBNET_IDS, tags={"Name": f"ray-head-nlb-{STACK}"})
    tg = aws.lb.TargetGroup(f"ray-head-tg-{STACK}", port=RAY_REDIS_PORT, protocol="TCP", target_type="instance", vpc_id=vpc_id := cfg.get("vpcId") or "", health_check=aws.lb.TargetGroupHealthCheckArgs(protocol="TCP", port=str(RAY_REDIS_PORT), interval=10, healthy_threshold=2, unhealthy_threshold=2))
    listener = aws.lb.Listener(f"ray-head-nlb-listener-{STACK}", load_balancer_arn=nlb.arn, port=RAY_REDIS_PORT, protocol="TCP", default_actions=[aws.lb.ListenerDefaultActionArgs(type="forward", target_group_arn=tg.arn)])
    attach = aws.lb.TargetGroupAttachment(f"ray-head-tg-attach-{STACK}", target_group_arn=tg.arn, target_id=head_instance.id, port=RAY_REDIS_PORT)
    pulumi.export("ray_head_nlb_dns", nlb.dns_name)
    pulumi.export("ray_head_tg_arn", tg.arn)
    if HOSTED_ZONE_ID and HOSTED_ZONE_NAME:
        fqdn = f"ray-head.{STACK}.{HOSTED_ZONE_NAME}"
        record = aws.route53.Record(f"ray-head-record-{STACK}", zone_id=HOSTED_ZONE_ID, name=fqdn, type="A", aliases=[aws.route53.RecordAliasArgs(name=nlb.dns_name, zone_id=nlb.zone_id, evaluate_target_health=False)])
        pulumi.export("ray_head_fqdn", fqdn)
else:
    pulumi.log.info("Skipping NLB: no private subnets provided")

pulumi.log.info("c_ray_head.py completed")
