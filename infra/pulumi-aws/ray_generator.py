from __future__ import annotations
import os
import textwrap
from pathlib import Path
import pulumi
import pulumi_aws as aws
from aws_networking import create_network
from iam import create_ec2_role, attach_elbv2_register_policy
cfg = pulumi.Config()
stack = pulumi.get_stack()
aws_region = os.getenv("AWS_REGION") or aws.get_region().name
VPC_CIDR = os.getenv("VPC_CIDR", "10.0.0.0/16")
PUBLIC_SUBNET_CIDRS = [s.strip() for s in os.getenv("PUBLIC_SUBNET_CIDRS", "10.0.1.0/24,10.0.2.0/24").split(",") if s.strip()]
PRIVATE_SUBNET_CIDRS = [s.strip() for s in os.getenv("PRIVATE_SUBNET_CIDRS", "10.0.11.0/24,10.0.12.0/24").split(",") if s.strip()]
MULTI_AZ = os.getenv("MULTI_AZ_DEPLOYMENT", "false").lower() == "true"
NO_NAT = os.getenv("NO_NAT", "false").lower() == "true"
DOMAIN = cfg.get("domain") or os.getenv("DOMAIN", "example.com")
HOSTED_ZONE_ID = cfg.get("hostedZoneId") or os.getenv("HOSTED_ZONE_ID")
KEY_PAIR_NAME = os.getenv("KEY_PAIR_NAME")
RAY_CLUSTER_NAME = os.getenv("RAY_CLUSTER_NAME", f"ray-{stack}")
RAY_REGION = os.getenv("RSV_AWS_REGION", aws_region)
RAY_SSH_USER = os.getenv("RSV_SSH_USER", "ubuntu")
RAY_SSH_PRIVATE_KEY = os.getenv("RSV_SSH_PRIVATE_KEY", "~/.ssh/id_rsa")
RAY_HEAD_INSTANCE = os.getenv("RAY_HEAD_INSTANCE", "m5.large")
RAY_HEAD_AMI = os.getenv("RAY_HEAD_AMI", "ami-HEAD-AMI-ID")
RAY_HEAD_KEYNAME = os.getenv("RAY_HEAD_KEYNAME", KEY_PAIR_NAME or "your-ec2-keypair")
RAY_CPU_INSTANCE = os.getenv("RAY_CPU_INSTANCE", "m5.xlarge")
RAY_CPU_AMI = os.getenv("RAY_CPU_AMI", "ami-CPU-AMI-ID")
RAY_CPU_MIN = int(os.getenv("RAY_CPU_MIN_WORKERS", "1"))
RAY_CPU_MAX = int(os.getenv("RAY_CPU_MAX_WORKERS", "6"))
RAY_GPU_INSTANCE = os.getenv("RAY_GPU_INSTANCE", "p3.2xlarge")
RAY_GPU_AMI = os.getenv("RAY_GPU_AMI", "ami-GPU-AMI-ID")
RAY_GPU_MIN = int(os.getenv("RAY_GPU_MIN_WORKERS", "0"))
RAY_GPU_MAX = int(os.getenv("RAY_GPU_MAX_WORKERS", "2"))
RAY_IDLE_TIMEOUT_MINUTES = int(os.getenv("RAY_IDLE_TIMEOUT_MINUTES", "10"))
RAY_YAML_PATH = Path(os.getenv("RAY_YAML_PATH", "ray_ec2_autoscaler.yaml"))
RAY_DEFAULT_PIP_PACKAGES = os.getenv("RAY_DEFAULT_PIP_PACKAGES", "ray[default]==2.5.0 httpx transformers")
RAY_GPU_PIP_PACKAGES = os.getenv("RAY_GPU_PIP_PACKAGES", "ray[default]==2.5.0 httpx transformers onnxruntime-gpu")
net = create_network("rag", vpc_cidr=VPC_CIDR, public_subnet_cidrs=PUBLIC_SUBNET_CIDRS, private_subnet_cidrs=PRIVATE_SUBNET_CIDRS, multi_az=MULTI_AZ, no_nat=NO_NAT, create_vpc_endpoints=False, aws_region=aws_region)
vpc = net["vpc"]
public_subnets = net["public_subnets"]
private_subnets = net["private_subnets"]
alb_sg = aws.ec2.SecurityGroup("albSg", vpc_id=vpc.id, description="alb ingress", ingress=[aws.ec2.SecurityGroupIngressArgs(protocol="tcp", from_port=80, to_port=80, cidr_blocks=["0.0.0.0/0"]), aws.ec2.SecurityGroupIngressArgs(protocol="tcp", from_port=443, to_port=443, cidr_blocks=["0.0.0.0/0"])], egress=[aws.ec2.SecurityGroupEgressArgs(protocol="-1", from_port=0, to_port=0, cidr_blocks=["0.0.0.0/0"])], tags={"Name": f"alb-sg-{stack}"})
APP_PORT = int(os.getenv("APP_PORT", "8080"))
backend_sg = aws.ec2.SecurityGroup("backendSg", vpc_id=vpc.id, description="backend from alb", ingress=[aws.ec2.SecurityGroupIngressArgs(protocol="tcp", from_port=APP_PORT, to_port=APP_PORT, security_groups=[alb_sg.id])], egress=[aws.ec2.SecurityGroupEgressArgs(protocol="-1", from_port=0, to_port=0, cidr_blocks=["0.0.0.0/0"])], tags={"Name": f"backend-sg-{stack}"})
management_sg = aws.ec2.SecurityGroup("managementSg", vpc_id=vpc.id, description="management", ingress=[], egress=[aws.ec2.SecurityGroupEgressArgs(protocol="-1", from_port=0, to_port=0, cidr_blocks=["0.0.0.0/0"])], tags={"Name": f"management-sg-{stack}"})
alb = aws.lb.LoadBalancer("ragAlb", internal=False, load_balancer_type="application", security_groups=[alb_sg.id], subnets=[s.id for s in public_subnets], enable_deletion_protection=False, tags={"Name": f"rag-alb-{stack}"})
tg = aws.lb.TargetGroup("ragTg", port=APP_PORT, protocol="HTTP", target_type="ip", vpc_id=vpc.id, health_check=aws.lb.TargetGroupHealthCheckArgs(path="/healthz", healthy_threshold=2, unhealthy_threshold=2, interval=15, timeout=5), tags={"Name": f"rag-tg-{stack}"})
http_listener = aws.lb.Listener("httpListener", load_balancer_arn=alb.arn, port=80, protocol="HTTP", default_actions=[aws.lb.ListenerDefaultActionArgs(type="redirect", redirect=aws.lb.ListenerDefaultActionRedirectArgs(protocol="HTTPS", port="443", status_code="HTTP_301"))])
zone_id = HOSTED_ZONE_ID or aws.route53.Zone("publicZone", name=DOMAIN, tags={"Name": f"public-zone-{stack}"}).id
cert = aws.acm.Certificate("albCert", domain_name=DOMAIN, validation_method="DNS", subject_alternative_names=[f"www.{DOMAIN}"], tags={"Name": f"alb-cert-{stack}"})
def create_validation_records(dvos):
    fqdns = []
    for i, dvo in enumerate(dvos):
        rec = aws.route53.Record(f"certValidation-{i}", zone_id=zone_id, name=dvo["resource_record_name"], type=dvo["resource_record_type"], records=[dvo["resource_record_value"]], ttl=300)
        fqdns.append(rec.fqdn)
    return fqdns
val_fqdns = cert.domain_validation_options.apply(lambda dvos: create_validation_records(dvos))
cert_validation = aws.acm.CertificateValidation("albCertValidation", certificate_arn=cert.arn, validation_record_fqdns=val_fqdns)
https_listener = aws.lb.Listener("httpsListener", load_balancer_arn=alb.arn, port=443, protocol="HTTPS", ssl_policy="ELBSecurityPolicy-TLS13-1-2-2021-06", certificate_arn=cert_validation.certificate_arn, default_actions=[aws.lb.ListenerDefaultActionArgs(type="forward", target_group_arn=tg.arn)])
for idx, path in enumerate(["/query*", "/embed*", "/rerank*"], start=10):
    aws.lb.ListenerRule(f"rule-{idx}", listener_arn=https_listener.arn, actions=[aws.lb.ListenerRuleActionArgs(type="forward", target_group_arn=tg.arn)], conditions=[aws.lb.ListenerRuleConditionArgs(path_pattern=aws.lb.ListenerRuleConditionPathPatternArgs(values=[path]))], priority=idx)
iam_res = create_ec2_role("rag")
ec2_role = iam_res["role"]
instance_profile = iam_res["instance_profile"]
attach_elbv2_register_policy(ec2_role, tg.arn)
def pick_subnet(pub_ids, priv_ids):
    if os.getenv("NO_NAT", "false").lower() == "true":
        return pub_ids[0] if pub_ids else (priv_ids[0] if priv_ids else "")
    else:
        return priv_ids[0] if priv_ids else (pub_ids[0] if pub_ids else "")
pub_ids_out = pulumi.Output.all(*[s.id for s in public_subnets]) if public_subnets else pulumi.Output.from_input([])
priv_ids_out = pulumi.Output.all(*[s.id for s in private_subnets]) if private_subnets else pulumi.Output.from_input([])
chosen_subnet_out = pulumi.Output.all(pub_ids_out, priv_ids_out).apply(lambda args: pick_subnet(list(args[0]), list(args[1])))
def _compose_and_write(args):
    (region, ssh_user, ssh_key_path, cluster_name, head_instance, head_ami, head_keyname, cpu_instance, cpu_ami, cpu_min, cpu_max, gpu_instance, gpu_ami, gpu_min, gpu_max, idle_timeout, pub_subs, priv_subs, chosen_subnet, backend_sg_id, management_sg_id, instance_profile_name, keyname, tg_arn) = args
    def q(x): return f'"{x}"' if x else '""'
    pub_yaml = "[" + ", ".join(q(s) for s in pub_subs) + "]"
    priv_yaml = "[" + ", ".join(q(s) for s in priv_subs) + "]"
    chosen_yaml = q(chosen_subnet)
    register_script = f"MY_IP=$(curl -s http://169.254.169.254/latest/meta-data/local-ipv4) && aws elbv2 register-targets --region {region} --target-group-arn {tg_arn} --targets Id=$MY_IP"
    yaml_text = f"""# ray_ec2_autoscaler.yaml - generated
cluster_name: {cluster_name}
min_workers: 0
max_workers: 10
idle_timeout_minutes: {idle_timeout}
provider:
  type: aws
  region: {region}
auth:
  ssh_user: {ssh_user}
  ssh_private_key: {ssh_key_path}
head_node:
  InstanceType: {head_instance}
  ImageId: {head_ami}
  KeyName: {head_keyname}
  IamInstanceProfile:
    Name: {instance_profile_name}
  SubnetId: {chosen_yaml}
  SecurityGroupIds: [{backend_sg_id}]
available_node_types:
  ray.head.default:
    node_config:
      InstanceType: {head_instance}
      ImageId: {head_ami}
      KeyName: {head_keyname}
      IamInstanceProfile:
        Name: {instance_profile_name}
      SubnetId: {chosen_yaml}
      SecurityGroupIds: [{backend_sg_id}]
    max_workers: 0
    resources: {{"CPU": 4}}
  ray.worker.cpu:
    node_config:
      InstanceType: {cpu_instance}
      ImageId: {cpu_ami}
      KeyName: {keyname}
      IamInstanceProfile:
        Name: {instance_profile_name}
      SubnetId: {chosen_yaml}
      SecurityGroupIds: [{backend_sg_id}]
    min_workers: {cpu_min}
    max_workers: {cpu_max}
    resources: {{"CPU": 8}}
    setup_commands:
      - "sudo apt-get update -y"
      - "sudo apt-get install -y python3 python3-pip git build-essential awscli"
      - "python3 -m pip install -U pip"
      - "pip3 install {RAY_DEFAULT_PIP_PACKAGES}"
      - "{register_script}"
  ray.worker.gpu:
    node_config:
      InstanceType: {gpu_instance}
      ImageId: {gpu_ami}
      KeyName: {keyname}
      IamInstanceProfile:
        Name: {instance_profile_name}
      SubnetId: {chosen_yaml}
      SecurityGroupIds: [{backend_sg_id}]
    min_workers: {gpu_min}
    max_workers: {gpu_max}
    resources: {{"GPU": 1, "CPU": 8}}
    setup_commands:
      - "sudo apt-get update -y"
      - "sudo apt-get install -y python3 python3-pip git build-essential awscli"
      - "python3 -m pip install -U pip"
      - "pip3 install {RAY_GPU_PIP_PACKAGES}"
      - "{register_script}"
head_node_type: ray.head.default
worker_default_node_type: ray.worker.cpu
"""
    path = Path(os.getenv("RAY_YAML_PATH", "ray_ec2_autoscaler.yaml"))
    path.write_text(textwrap.dedent(yaml_text))
    pulumi.log.info(f"Wrote ray autoscaler YAML to {path.resolve()}")
    return str(path.resolve())
inputs = pulumi.Output.all(RAY_REGION, RAY_SSH_USER, RAY_SSH_PRIVATE_KEY, RAY_CLUSTER_NAME, RAY_HEAD_INSTANCE, RAY_HEAD_AMI, RAY_HEAD_KEYNAME, RAY_CPU_INSTANCE, RAY_CPU_AMI, RAY_CPU_MIN, RAY_CPU_MAX, RAY_GPU_INSTANCE, RAY_GPU_AMI, RAY_GPU_MIN, RAY_GPU_MAX, RAY_IDLE_TIMEOUT_MINUTES, pub_ids_out, priv_ids_out, chosen_subnet_out, backend_sg.id, management_sg.id, instance_profile.name, (KEY_PAIR_NAME or RAY_HEAD_KEYNAME), tg.arn)
ray_yaml_path = inputs.apply(_compose_and_write)
pulumi.export("vpc_id", vpc.id)
pulumi.export("public_subnet_ids", pub_ids_out)
pulumi.export("private_subnet_ids", priv_ids_out)
pulumi.export("alb_dns", alb.dns_name)
pulumi.export("target_group_arn", tg.arn)
pulumi.export("instance_profile_name", instance_profile.name)
pulumi.export("ray_autoscaler_yaml", ray_yaml_path)