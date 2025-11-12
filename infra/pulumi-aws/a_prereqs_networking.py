# a_prereqs_networking.py
from __future__ import annotations
import os
import ipaddress
from typing import List, Dict, Any, Optional
import pulumi
import pulumi_aws as aws

STACK = pulumi.get_stack()
cfg = pulumi.Config()

# === staging defaults (override with env or pulumi config) ===
MULTI_AZ = os.getenv("MULTI_AZ_DEPLOYMENT", str(cfg.get_bool("multiAz") or False)).lower() in ("1", "true", "yes")
NO_NAT = os.getenv("NO_NAT", cfg.get("noNat") or "false").lower() in ("1", "true", "yes")
CREATE_VPC_ENDPOINTS = os.getenv("CREATE_VPC_ENDPOINTS", cfg.get("createVpcEndpoints") or "false").lower() in ("1", "true", "yes")
VPC_CIDR = os.getenv("VPC_CIDR", cfg.get("vpcCidr") or "10.0.0.0/16")
PUBLIC_SUBNET_CIDRS = (os.getenv("PUBLIC_SUBNET_CIDRS") or cfg.get("publicSubnetCidrs") or "10.0.1.0/24,10.0.2.0/24").split(",")
PRIVATE_SUBNET_CIDRS = (os.getenv("PRIVATE_SUBNET_CIDRS") or cfg.get("privateSubnetCidrs") or "10.0.11.0/24,10.0.12.0/24").split(",")
REDIS_SSM_PARAM = os.getenv("REDIS_SSM_PARAM", cfg.get("redisSsmParam") or "/ray/prod/redis_password")

def _validate_cidrs(cidrs: List[str]) -> None:
    for c in cidrs:
        try:
            ipaddress.ip_network(c)
        except Exception as e:
            raise pulumi.ResourceError(f"Invalid CIDR '{c}': {e}", resource=None)

_validate_cidrs(PUBLIC_SUBNET_CIDRS)
_validate_cidrs(PRIVATE_SUBNET_CIDRS)

# --- KMS (create or reuse alias) ---
KMS_ALIAS = os.getenv("KMS_ALIAS", cfg.get("kmsAlias") or f"alias/ray-ssm-key-{STACK}")
kms_key: aws.kms.Key
try:
    existing = aws.kms.get_alias(name=KMS_ALIAS)
    kms_key = aws.kms.Key.get("raySsmKeyExisting", existing.target_key_id)
    pulumi.log.info(f"Reusing existing KMS alias {KMS_ALIAS}")
except Exception:
    kms_key = aws.kms.Key("raySsmKey",
                          description=f"CMK for Ray SSM ({STACK})",
                          deletion_window_in_days=30,
                          tags={"Name": f"ray-ssm-key-{STACK}"})
    aws.kms.Alias("raySsmKeyAlias", name=KMS_ALIAS, target_key_id=kms_key.key_id)
    pulumi.log.info("Created new KMS key for SSM")

# --- VPC, subnets, IGW, route tables ---
vpc = aws.ec2.Vpc(f"ray-vpc-{STACK}", cidr_block=VPC_CIDR, enable_dns_hostnames=True, enable_dns_support=True, tags={"Name": f"ray-vpc-{STACK}"})
igw = aws.ec2.InternetGateway(f"ray-igw-{STACK}", vpc_id=vpc.id, tags={"Name": f"ray-igw-{STACK}"})
public_rt = aws.ec2.RouteTable(f"ray-public-rt-{STACK}", vpc_id=vpc.id, routes=[aws.ec2.RouteTableRouteArgs(cidr_block="0.0.0.0/0", gateway_id=igw.id)], tags={"Name": f"ray-public-rt-{STACK}"})

# choose AZs
azs = aws.get_availability_zones(state="available").names
use_azs = azs if MULTI_AZ else azs[:1]

public_subnets: List[aws.ec2.Subnet] = []
private_subnets: List[aws.ec2.Subnet] = []
for i, c in enumerate(PUBLIC_SUBNET_CIDRS):
    az = use_azs[i % len(use_azs)]
    sn = aws.ec2.Subnet(f"ray-public-{i}-{STACK}", vpc_id=vpc.id, cidr_block=c.strip(), availability_zone=az, map_public_ip_on_launch=True, tags={"Name": f"ray-public-{i}-{STACK}"})
    aws.ec2.RouteTableAssociation(f"ray-public-rt-assoc-{i}-{STACK}", route_table_id=public_rt.id, subnet_id=sn.id)
    public_subnets.append(sn)
for i, c in enumerate(PRIVATE_SUBNET_CIDRS):
    az = use_azs[i % len(use_azs)]
    sn = aws.ec2.Subnet(f"ray-private-{i}-{STACK}", vpc_id=vpc.id, cidr_block=c.strip(), availability_zone=az, map_public_ip_on_launch=False, tags={"Name": f"ray-private-{i}-{STACK}"})
    private_subnets.append(sn)

# NAT gateways if not NO_NAT
nat_gateways: List[aws.ec2.NatGateway] = []
private_route_tables: List[aws.ec2.RouteTable] = []
if not NO_NAT:
    for idx, az in enumerate(use_azs):
        pub_candidate = next((p for p in public_subnets if getattr(p, "availability_zone", "") == az), public_subnets[0])
        eip = aws.ec2.Eip(f"ray-nat-eip-{idx}-{STACK}", domain="vpc", tags={"Name": f"ray-nat-eip-{idx}-{STACK}"})
        nat = aws.ec2.NatGateway(f"ray-nat-{idx}-{STACK}", allocation_id=eip.id, subnet_id=pub_candidate.id, tags={"Name": f"ray-nat-{idx}-{STACK}"})
        nat_gateways.append(nat)
        prt = aws.ec2.RouteTable(f"ray-private-rt-{idx}-{STACK}", vpc_id=vpc.id, routes=[aws.ec2.RouteTableRouteArgs(cidr_block="0.0.0.0/0", nat_gateway_id=nat.id)], tags={"Name": f"ray-private-rt-{idx}-{STACK}"})
        private_route_tables.append(prt)
        # associate private subnets in same AZ where possible
        matched = [p for p in private_subnets if getattr(p, "availability_zone", "") == az] or private_subnets[:1]
        for j, ps in enumerate(matched):
            aws.ec2.RouteTableAssociation(f"ray-priv-rt-assoc-{idx}-{j}-{STACK}", route_table_id=prt.id, subnet_id=ps.id)
else:
    for idx, ps in enumerate(private_subnets):
        prt = aws.ec2.RouteTable(f"ray-private-rt-{idx}-{STACK}", vpc_id=vpc.id, tags={"Name": f"ray-private-rt-{idx}-{STACK}"})
        aws.ec2.RouteTableAssociation(f"ray-priv-rt-assoc-{idx}-{STACK}", route_table_id=prt.id, subnet_id=ps.id)
        private_route_tables.append(prt)

# S3 buckets used by the project (pulumi state bucket, autoscaler, models)
# Keep bucket definitions simple to avoid provider-version signature mismatches.
pulumi_state_bucket = aws.s3.Bucket(f"pulumi-state-bucket",
                                    bucket=f"pulumi-state-{STACK}-{aws.get_caller_identity().account_id}",
                                    server_side_encryption_configuration=aws.s3.BucketServerSideEncryptionConfigurationArgs(
                                        rule=aws.s3.BucketServerSideEncryptionConfigurationRuleArgs(
                                            apply_server_side_encryption_by_default=aws.s3.BucketServerSideEncryptionConfigurationRuleApplyServerSideEncryptionByDefaultArgs(
                                                sse_algorithm="AES256"
                                            )
                                        )
                                    ),
                                    opts=pulumi.ResourceOptions(parent=vpc))

autoscaler_bucket = aws.s3.Bucket("autoscaler-bucket",
                                 bucket=(os.getenv("AUTOSCALER_BUCKET_NAME") or cfg.get("autoscalerBucketName") or f"ray-autoscaler-{STACK}-{aws.get_region().name}"),
                                 opts=pulumi.ResourceOptions(parent=vpc))

models_bucket = aws.s3.Bucket("models-bucket",
                             bucket=(os.getenv("MODELS_S3_BUCKET") or cfg.get("modelsS3Bucket") or f"ray-models-{STACK}-{aws.get_region().name}"),
                             opts=pulumi.ResourceOptions(parent=vpc))

# Enable versioning on pulumi-state bucket using correct arg name for Pulumi provider
# use the boolean 'enabled' field to avoid provider API mismatches across versions
aws.s3.BucketVersioning("pulumi-state-bucket-versioning",
    bucket=pulumi_state_bucket.id,
    versioning_configuration=aws.s3.BucketVersioningVersioningConfigurationArgs(
        status="Enabled"
    )
)

# VPC endpoints (optional)
vpc_endpoints: Dict[str, Any] = {}
if CREATE_VPC_ENDPOINTS:
    route_table_ids = [public_rt.id] + [r.id for r in private_route_tables]
    s3_ep = aws.ec2.VpcEndpoint(f"ray-vpce-s3-{STACK}", vpc_id=vpc.id, service_name=f"com.amazonaws.{aws.get_region().name}.s3", vpc_endpoint_type="Gateway", route_table_ids=route_table_ids, tags={"Name": f"ray-vpce-s3-{STACK}"})
    vpc_endpoints["s3"] = s3_ep
    svc_list = ["ssm", "ssmmessages", "ec2messages", "secretsmanager", "sts", "ecr.api", "ecr.dkr"]
    ep_sg = aws.ec2.SecurityGroup(f"ray-vpce-sg-{STACK}", vpc_id=vpc.id, description="VPC Endpoint SG", ingress=[aws.ec2.SecurityGroupIngressArgs(protocol="tcp", from_port=443, to_port=443, cidr_blocks=[VPC_CIDR])], egress=[aws.ec2.SecurityGroupEgressArgs(protocol="-1", from_port=0, to_port=0, cidr_blocks=["0.0.0.0/0"])])
    subnet_ids_for_endpoints = [s.id for s in (private_subnets or public_subnets)][:len(use_azs)]
    for svc in svc_list:
        svc_name = f"com.amazonaws.{aws.get_region().name}.{svc}"
        ep = aws.ec2.VpcEndpoint(f"ray-vpce-{svc.replace('.','-')}-{STACK}", vpc_id=vpc.id, service_name=svc_name, vpc_endpoint_type="Interface", subnet_ids=subnet_ids_for_endpoints, security_group_ids=[ep_sg.id], private_dns_enabled=True, tags={"Name": f"ray-vpce-{svc}-{STACK}"})
        vpc_endpoints[svc] = ep

# Security groups
alb_sg = aws.ec2.SecurityGroup(f"ray-alb-sg-{STACK}", vpc_id=vpc.id, description="ALB SG", ingress=[
    aws.ec2.SecurityGroupIngressArgs(protocol="tcp", from_port=443, to_port=443, cidr_blocks=["0.0.0.0/0"]),
    aws.ec2.SecurityGroupIngressArgs(protocol="tcp", from_port=80, to_port=80, cidr_blocks=["0.0.0.0/0"])
], egress=[aws.ec2.SecurityGroupEgressArgs(protocol="-1", from_port=0, to_port=0, cidr_blocks=["0.0.0.0/0"])], tags={"Name": f"ray-alb-sg-{STACK}"})

head_sg = aws.ec2.SecurityGroup(f"ray-head-sg-{STACK}", vpc_id=vpc.id, description="Head SG", ingress=[
    aws.ec2.SecurityGroupIngressArgs(protocol="tcp", from_port=6379, to_port=6379, cidr_blocks=[VPC_CIDR]),
    aws.ec2.SecurityGroupIngressArgs(protocol="tcp", from_port=8003, to_port=8003, cidr_blocks=[VPC_CIDR])
], egress=[aws.ec2.SecurityGroupEgressArgs(protocol="-1", from_port=0, to_port=0, cidr_blocks=["0.0.0.0/0"])], tags={"Name": f"ray-head-sg-{STACK}"})

worker_sg = aws.ec2.SecurityGroup(f"ray-worker-sg-{STACK}", vpc_id=vpc.id, description="Worker SG", ingress=[
    aws.ec2.SecurityGroupIngressArgs(protocol="tcp", from_port=8003, to_port=8003, security_groups=[alb_sg.id])
], egress=[aws.ec2.SecurityGroupEgressArgs(protocol="-1", from_port=0, to_port=0, cidr_blocks=["0.0.0.0/0"])], tags={"Name": f"ray-worker-sg-{STACK}"})

# SSM parameter for redis password — create only when a non-empty secret exists
def _get_redis_secret() -> Optional[Any]:
    # Prefer pulumi config secret if present
    try:
        # cfg.get_secret returns a value or raises; it may return an Output
        val = cfg.get_secret("redisPassword")
    except Exception:
        val = None
    # fallback to non-secret config or env var (plaintext fallback only)
    if not val:
        val = os.getenv("REDIS_PASSWORD") or cfg.get("redisPassword")
    return val

REDIS_SECRET = _get_redis_secret()

# Only create SSM parameter if REDIS_SECRET is provided and non-empty string.
# If REDIS_SECRET is a Pulumi secret (Output), we cannot evaluate it here; assume it's valid and create.
should_create_ssm = False
if REDIS_SECRET is None:
    should_create_ssm = False
else:
    # if it's a plain python string check non-empty
    if isinstance(REDIS_SECRET, str):
        should_create_ssm = len(REDIS_SECRET) > 0
    else:
        # likely a Pulumi Output or SecretValue. Create and let Pulumi handle the secret.
        should_create_ssm = True

if not should_create_ssm:
    pulumi.log.warn("redisPassword not found in pulumi config or env REDIS_PASSWORD. Create SSM param manually or set config.")
else:
    ssm_param = aws.ssm.Parameter("rayRedisParameter", name=REDIS_SSM_PARAM, type="SecureString", value=REDIS_SECRET, key_id=kms_key.arn, tags={"Name": f"ray-redis-password-{STACK}"})
    pulumi.export("redis_ssm_param_name", ssm_param.name)

pulumi.export("vpc_id", vpc.id)
pulumi.export("public_subnet_ids", [s.id for s in public_subnets])
pulumi.export("private_subnet_ids", [s.id for s in private_subnets])
pulumi.export("alb_security_group_id", alb_sg.id)
pulumi.export("head_security_group_id", head_sg.id)
pulumi.export("worker_security_group_id", worker_sg.id)
pulumi.export("kms_key_arn", kms_key.arn)
if vpc_endpoints:
    pulumi.export("vpc_endpoints", {k: getattr(v, "id", None) for k, v in vpc_endpoints.items()})
pulumi.log.info("a_prereqs_networking.py completed")
