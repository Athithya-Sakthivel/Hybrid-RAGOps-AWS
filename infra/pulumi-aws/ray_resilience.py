# infra/head_resilience.py
from __future__ import annotations
import textwrap
import pulumi
import pulumi_aws as aws
stack = pulumi.get_stack()
cfg = pulumi.Config()
VPC_ID = cfg.require("vpcId")
SUBNET_IDS = cfg.require_object("subnetIds")
AMI_ID = cfg.require("headAmi")
INSTANCE_TYPE = cfg.get("headInstanceType") or "m5.large"
HOSTED_ZONE_ID = cfg.require("privateHostedZoneId")
HOSTED_ZONE_NAME = cfg.get("hostedZoneName") or "prod.internal.example.com"
RAY_HEAD_DNS_NAME = f"ray-head.{stack}.{HOSTED_ZONE_NAME}"
SSM_REDIS_PARAM = cfg.get("redisSsmParam") or "/ray/prod/redis_password"
KEY_NAME = cfg.get("keyName")
assume_role = aws.iam.get_policy_document(statements=[{"Effect": "Allow", "Principals": [{"Type": "Service", "Identifiers": ["ec2.amazonaws.com"]}], "Actions": ["sts:AssumeRole"]}])
head_role = aws.iam.Role("rayHeadRole", assume_role_policy=assume_role.json)
aws.iam.RolePolicyAttachment("ssmAttach", role=head_role.name, policy_arn="arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore")
aws.iam.RolePolicyAttachment("cwAttach", role=head_role.name, policy_arn="arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy")
aws.iam.RolePolicyAttachment("s3roAttach", role=head_role.name, policy_arn="arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess")
head_instance_profile = aws.iam.InstanceProfile("rayHeadInstanceProfile", role=head_role.name)
head_sg = aws.ec2.SecurityGroup("rayHeadSg", vpc_id=VPC_ID, description="ray head sg", ingress=[aws.ec2.SecurityGroupIngressArgs(protocol="tcp", from_port=6379, to_port=6379, cidr_blocks=["10.0.0.0/8"]), aws.ec2.SecurityGroupIngressArgs(protocol="tcp", from_port=8000, to_port=8000, cidr_blocks=["10.0.0.0/8"])], egress=[aws.ec2.SecurityGroupEgressArgs(protocol="-1", from_port=0, to_port=0, cidr_blocks=["0.0.0.0/0"])])
ASG_NAME = f"ray-head-asg-{stack}"
LIFECYCLE_HOOK_NAME = f"wait-for-ray-ready-{stack}"
user_data_tpl = """#!/bin/bash
set -euo pipefail
REGION="us-east-1"
ASG_NAME="{asg_name}"
HOOK_NAME="{hook_name}"
SSM_PARAM="{ssm_param}"
AUTOSCALER_S3="{autoscaler_s3}"
exec > >(tee /var/log/ray-head-bootstrap.log|logger -t ray-head-bootstrap -s 2>/dev/console) 2>&1
apt-get update -y
apt-get install -y python3-pip awscli jq curl
python3 -m pip install --upgrade pip
pip3 install "ray[default]==2.5.0"
REDIS_PASSWORD="$(aws ssm get-parameter --name "$SSM_PARAM" --with-decryption --region "$REGION" --query 'Parameter.Value' --output text)"
PRIVATE_IP=$(curl -s http://169.254.169.254/latest/meta-data/local-ipv4 || echo "")
ray stop || true
if aws s3 ls "$AUTOSCALER_S3" >/dev/null 2>&1; then
  aws s3 cp "$AUTOSCALER_S3" /etc/ray/autoscaler.yaml --region "$REGION" || true
  ray start --head --port=6379 --node-ip-address="$PRIVATE_IP" --redis-password="$REDIS_PASSWORD" --autoscaling-config=/etc/ray/autoscaler.yaml --include-dashboard False || true
else
  ray start --head --port=6379 --node-ip-address="$PRIVATE_IP" --redis-password="$REDIS_PASSWORD" --include-dashboard False || true
fi
for i in $(seq 1 60); do
  if ray status --address "auto" 2>/dev/null | grep -q "Cluster status: ALIVE"; then
    break
  fi
  sleep 2
done
INSTANCE_ID=$(curl -s http://169.254.169.254/latest/meta-data/instance-id)
aws cloudwatch put-metric-data --region "$REGION" --namespace "Ray/Head" --metric-data MetricName=HeadReady,Dimensions=[{Name=InstanceId,Value=$INSTANCE_ID}],Value=1,Unit=Count
aws autoscaling complete-lifecycle-action --region "$REGION" --lifecycle-hook-name "$HOOK_NAME" --auto-scaling-group-name "$ASG_NAME" --lifecycle-action-result CONTINUE --instance-id "$INSTANCE_ID" || true
(
  while true; do
    aws cloudwatch put-metric-data --region "$REGION" --namespace "Ray/Head" --metric-data MetricName=HeadReady,Dimensions=[{Name=InstanceId,Value=$INSTANCE_ID}],Value=1,Unit=Count
    sleep 60
  done
) &
"""
lt = aws.ec2.LaunchTemplate("rayHeadLaunchTemplate", name_prefix=f"ray-head-lt-{stack}", image_id=AMI_ID, instance_type=INSTANCE_TYPE, iam_instance_profile=aws.ec2.LaunchTemplateIamInstanceProfileArgs(name=head_instance_profile.name), network_interfaces=[aws.ec2.LaunchTemplateNetworkInterfaceArgs(security_groups=[head_sg.id], associate_public_ip_address=False)], key_name=KEY_NAME if KEY_NAME else None, user_data=pulumi.Output.all().apply(lambda _: user_data_tpl.format(asg_name=ASG_NAME, hook_name=LIFECYCLE_HOOK_NAME, ssm_param=SSM_REDIS_PARAM, autoscaler_s3="s3://ray-configs/prod/autoscaler.yaml")))
tg = aws.lb.TargetGroup("rayHeadTg", port=6379, protocol="TCP", target_type="instance", vpc_id=VPC_ID, health_check=aws.lb.TargetGroupHealthCheckArgs(protocol="TCP", port="6379", interval=10, healthy_threshold=2, unhealthy_threshold=2))
nlb = aws.lb.LoadBalancer("rayHeadNlb", internal=True, load_balancer_type="network", subnets=SUBNET_IDS, tags={"Name": f"ray-head-nlb-{stack}"})
nlb_listener = aws.lb.Listener("nlbListener", load_balancer_arn=nlb.arn, port=6379, protocol="TCP", default_actions=[aws.lb.ListenerDefaultActionArgs(type="forward", target_group_arn=tg.arn)])
asg = aws.autoscaling.Group("rayHeadAsg", name=ASG_NAME, desired_capacity=1, min_size=1, max_size=1, vpc_zone_identifiers=SUBNET_IDS, launch_template=aws.autoscaling.GroupLaunchTemplateArgs(id=lt.id, version="$Latest"), target_group_arns=[tg.arn], health_check_type="ELB", health_check_grace_period=180, tags=[aws.autoscaling.GroupTagArgs(key="Name", value=f"ray-head-{stack}", propagate_at_launch=True)])
lifecycle_hook = aws.autoscaling.LifecycleHook("rayHeadLifecycleHook", autoscaling_group_name=asg.name, default_result="ABANDON", heartbeat_timeout=1800, lifecycle_transition="autoscaling:EC2_INSTANCE_LAUNCHING", name=LIFECYCLE_HOOK_NAME)
record = aws.route53.Record("rayHeadRecord", zone_id=HOSTED_ZONE_ID, name=RAY_HEAD_DNS_NAME, type="A", aliases=[aws.route53.RecordAliasArgs(name=nlb.dns_name, zone_id=nlb.zone_id, evaluate_target_health=False)])
topic = aws.sns.Topic("rayHeadAlertsTopic")
alarm = aws.cloudwatch.MetricAlarm("rayHeadReadyAlarm", alarm_name=f"ray-head-ready-{stack}", comparison_operator="LessThanThreshold", evaluation_periods=3, metric_name="HeadReady", namespace="Ray/Head", period=60, statistic="Maximum", threshold=1, alarm_actions=[topic.arn], dimensions={"InstanceId": asg.name})
sys_alarm = aws.cloudwatch.MetricAlarm("ec2SystemStatusAlarm", alarm_name=f"ec2-system-status-{stack}", comparison_operator="GreaterThanOrEqualToThreshold", evaluation_periods=2, metric_name="StatusCheckFailed_System", namespace="AWS/EC2", period=60, statistic="Maximum", threshold=1, alarm_actions=[topic.arn])
pulumi.export("head_asg_name", asg.name)
pulumi.export("head_dns", record.fqdn)
pulumi.export("nlb_dns", nlb.dns_name)
pulumi.export("alerts_topic", topic.arn)