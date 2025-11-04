import pulumi
import pulumi_aws as aws
import os

# --- Load config/env ---
cfg = pulumi.Config()

region = os.getenv("AWS_REGION", aws.get_region().name)
vpc_cidr = os.getenv("VPC_CIDR", "10.0.0.0/16")

# Subnet CIDRs (comma-separated string → list)
public_subnet_cidrs = os.getenv("PUBLIC_SUBNET_CIDRS", "10.0.1.0/24,10.0.2.0/24").split(",")
public_subnet_count = int(os.getenv("PULUMI_PUBLIC_SUBNET_COUNT", str(len(public_subnet_cidrs))))

# Optional private subnet CIDRs (default: offset from public)
private_subnet_cidrs = os.getenv("PRIVATE_SUBNET_CIDRS", "10.0.11.0/24,10.0.12.0/24").split(",")

multi_az = os.getenv("MULTI_AZ_DEPLOYMENT", "false").lower() == "true"
domain = cfg.get("domain") or os.getenv("DOMAIN", "example.com")
existing_zone_id = cfg.get("hostedZoneId") or os.getenv("HOSTED_ZONE_ID")

# --- AZ selection ---
azs = aws.get_availability_zones(state="available").names
if multi_az:
    azs = azs[:max(public_subnet_count, len(public_subnet_cidrs))]
else:
    azs = azs[:1]

# --- VPC ---
vpc = aws.ec2.Vpc(
    "ragVpc",
    cidr_block=vpc_cidr,
    enable_dns_hostnames=True,
    enable_dns_support=True,
    tags={"Name": f"rag-vpc"}
)

igw = aws.ec2.InternetGateway("ragIgw", vpc_id=vpc.id, tags={"Name": "rag-igw"})

# --- Public subnets ---
public_subnets = []
for i, cidr in enumerate(public_subnet_cidrs[:len(azs)]):
    subnet = aws.ec2.Subnet(
        f"publicSubnet{i+1}",
        vpc_id=vpc.id,
        cidr_block=cidr,
        availability_zone=azs[i % len(azs)],
        map_public_ip_on_launch=True,
        tags={"Name": f"public-{i+1}", "Public": "true"}
    )
    public_subnets.append(subnet)

# --- Private subnets ---
private_subnets = []
for i, cidr in enumerate(private_subnet_cidrs[:len(azs)]):
    subnet = aws.ec2.Subnet(
        f"privateSubnet{i+1}",
        vpc_id=vpc.id,
        cidr_block=cidr,
        availability_zone=azs[i % len(azs)],
        tags={"Name": f"private-{i+1}", "Private": "true"}
    )
    private_subnets.append(subnet)

# --- Route tables ---
public_rt = aws.ec2.RouteTable("publicRt", vpc_id=vpc.id, tags={"Name": "public-rt"})
aws.ec2.Route("publicRoute0", route_table_id=public_rt.id,
              destination_cidr_block="0.0.0.0/0", gateway_id=igw.id)

for i, subnet in enumerate(public_subnets):
    aws.ec2.RouteTableAssociation(f"publicAssoc{i+1}", subnet_id=subnet.id, route_table_id=public_rt.id)

# NAT gateways (one per public subnet)
nat_gateways = []
for i, subnet in enumerate(public_subnets):
    eip = aws.ec2.Eip(f"natEip{i+1}", domain="vpc")
    nat = aws.ec2.NatGateway(f"natGw{i+1}", allocation_id=eip.id, subnet_id=subnet.id,
                             tags={"Name": f"nat-{i+1}"})
    nat_gateways.append(nat)

# Private route tables
for i, subnet in enumerate(private_subnets):
    rt = aws.ec2.RouteTable(f"privateRt{i+1}", vpc_id=vpc.id, tags={"Name": f"private-rt-{i+1}"})
    aws.ec2.Route(f"privateRoute{i+1}", route_table_id=rt.id,
                  destination_cidr_block="0.0.0.0/0", nat_gateway_id=nat_gateways[i % len(nat_gateways)].id)
    aws.ec2.RouteTableAssociation(f"privateAssoc{i+1}", subnet_id=subnet.id, route_table_id=rt.id)

# --- DNS / ACM ---
zone_id = existing_zone_id or aws.route53.Zone("publicZone", name=domain).id

cert = aws.acm.Certificate("albCert",
    domain_name=domain,
    validation_method="DNS",
    subject_alternative_names=[f"www.{domain}"])

val_records = []
for i, dvo in enumerate(cert.domain_validation_options):
    val_records.append(aws.route53.Record(f"certValidation-{i}",
        zone_id=zone_id,
        name=dvo.resource_record_name,
        type=dvo.resource_record_type,
        records=[dvo.resource_record_value],
        ttl=300))

cert_validation = aws.acm.CertificateValidation("albCertValidation",
    certificate_arn=cert.arn,
    validation_record_fqdns=[r.fqdn for r in val_records])

# --- Security groups ---
alb_sg = aws.ec2.SecurityGroup("albSg",
    vpc_id=vpc.id,
    description="ALB ingress",
    ingress=[
        aws.ec2.SecurityGroupIngressArgs(protocol="tcp", from_port=80, to_port=80, cidr_blocks=["0.0.0.0/0"]),
        aws.ec2.SecurityGroupIngressArgs(protocol="tcp", from_port=443, to_port=443, cidr_blocks=["0.0.0.0/0"])
    ],
    egress=[aws.ec2.SecurityGroupEgressArgs(protocol="-1", from_port=0, to_port=0, cidr_blocks=["0.0.0.0/0"])],
    tags={"Name": "alb-sg"})

backend_sg = aws.ec2.SecurityGroup("backendSg",
    vpc_id=vpc.id,
    description="Backend from ALB",
    ingress=[aws.ec2.SecurityGroupIngressArgs(protocol="tcp", from_port=8080, to_port=8080, security_groups=[alb_sg.id])],
    egress=[aws.ec2.SecurityGroupEgressArgs(protocol="-1", from_port=0, to_port=0, cidr_blocks=["0.0.0.0/0"])],
    tags={"Name": "backend-sg"})

# --- ALB + Target Group ---
alb = aws.lb.LoadBalancer("ragAlb",
    internal=False,
    load_balancer_type="application",
    security_groups=[alb_sg.id],
    subnets=[s.id for s in public_subnets],
    enable_deletion_protection=True,
    tags={"Name": "rag-alb"})

tg = aws.lb.TargetGroup("ragTg",
    port=8080,
    protocol="HTTP",
    target_type="instance",
    vpc_id=vpc.id,
    health_check=aws.lb.TargetGroupHealthCheckArgs(
        path="/healthz", healthy_threshold=2, unhealthy_threshold=2, interval=15, timeout=5),
    tags={"Name": "rag-tg"})

http_listener = aws.lb.Listener("httpListener",
    load_balancer_arn=alb.arn,
    port=80,
    protocol="HTTP",
    default_actions=[aws.lb.ListenerDefaultActionArgs(
        type="redirect",
        redirect=aws.lb.ListenerDefaultActionRedirectArgs(protocol="HTTPS", port="443", status_code="HTTP_301"))])

https_listener = aws.lb.Listener("httpsListener",
    load_balancer_arn=alb.arn,
    port=443,
    protocol="HTTPS",
    ssl_policy="ELBSecurityPolicy-TLS13-1-2-2021-06",
    certificate_arn=cert_validation.certificate_arn,
    default_actions=[aws.lb.ListenerDefaultActionArgs(type="forward", target_group_arn=tg.arn)])

# Example listener rules
for idx, path in enumerate(["/query*", "/embed*", "/rerank*"], start=10):
    aws.lb.ListenerRule(f"rule{idx}",
        listener_arn=https_listener.arn,
        actions=[aws.lb.ListenerRuleActionArgs(type="forward", target_group_arn=tg.arn)],
        conditions=[aws.lb.ListenerRuleConditionArgs(
            path_pattern=aws.lb.ListenerRuleConditionPathPatternArgs(values=[path]))],
        priority=idx)
