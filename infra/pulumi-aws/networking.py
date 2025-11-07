# infra/pulumi-aws/networking.py
from __future__ import annotations
import os
import ipaddress
from typing import Any, Dict, List, Optional
from collections import defaultdict
import pulumi
import pulumi_aws as aws

def _bool_env(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return str(v).lower() not in ("0", "false", "no", "")

def _validate_cidrs(cidrs: List[str]) -> None:
    for c in cidrs:
        try:
            ipaddress.ip_network(c)
        except Exception as e:
            raise ValueError(f"Invalid CIDR '{c}': {e}")

def create_network(
    name: str = "ray",
    vpc_cidr: str = "10.0.0.0/16",
    public_subnet_cidrs: Optional[List[str]] = None,
    private_subnet_cidrs: Optional[List[str]] = None,
    multi_az: Optional[bool] = None,
    no_nat: Optional[bool] = None,
    create_vpc_endpoints: Optional[bool] = None,
    aws_region: Optional[str] = None,
    use_existing_vpc_id: Optional[str] = None,
    create_subnets: bool = True,
) -> Dict[str, Any]:
    """
    Create a flexible VPC and subnets.
    Returns dict keys: vpc, public_subnets, private_subnets, internet_gateway,
    public_route_table, private_route_tables, nat_gateways, elastic_ips, vpc_endpoints
    """

    if multi_az is None:
        multi_az = _bool_env("MULTI_AZ_DEPLOYMENT", False)
    if no_nat is None:
        no_nat = _bool_env("AVOID_NAT", False) or _bool_env("NO_NAT", False)
    if create_vpc_endpoints is None:
        create_vpc_endpoints = _bool_env("CREATE_VPC_ENDPOINTS", False)

    aws_region = aws_region or os.getenv("AWS_REGION") or aws.get_region().name

    public_subnet_cidrs = public_subnet_cidrs or [c.strip() for c in (os.getenv("PUBLIC_SUBNET_CIDRS") or "10.0.1.0/24").split(",") if c.strip()]
    private_subnet_cidrs = private_subnet_cidrs or [c.strip() for c in (os.getenv("PRIVATE_SUBNET_CIDRS") or "10.0.11.0/24").split(",") if c.strip()]

    if create_subnets:
        _validate_cidrs(public_subnet_cidrs)
        _validate_cidrs(private_subnet_cidrs)

    pulumi.log.info(f"create_network: name={name} region={aws_region} multi_az={multi_az} no_nat={no_nat} create_vpc_endpoints={create_vpc_endpoints} use_existing_vpc_id={bool(use_existing_vpc_id)}")

    # VPC (create or reference)
    if use_existing_vpc_id:
        vpc = aws.ec2.get_vpc(id=use_existing_vpc_id)
    else:
        vpc = aws.ec2.Vpc(f"{name}-vpc",
                          cidr_block=vpc_cidr,
                          enable_dns_hostnames=True,
                          enable_dns_support=True,
                          tags={"Name": f"{name}-vpc"})

    # Internet gateway + public route table (only if we manage IGW)
    igw = aws.ec2.InternetGateway(f"{name}-igw", vpc_id=vpc.id, tags={"Name": f"{name}-igw"}) if not use_existing_vpc_id else None
    public_rt = None
    if igw:
        public_rt = aws.ec2.RouteTable(f"{name}-public-rt",
                                       vpc_id=vpc.id,
                                       routes=[aws.ec2.RouteTableRouteArgs(cidr_block="0.0.0.0/0", gateway_id=igw.id)],
                                       tags={"Name": f"{name}-public-rt"})

    # AZ choices
    azs_all = aws.get_availability_zones(state="available")
    az_names = azs_all.names
    if multi_az:
        max_needed = max(len(public_subnet_cidrs), len(private_subnet_cidrs), 1)
        use_azs = az_names[:min(len(az_names), max_needed)]
    else:
        use_azs = az_names[:1]

    public_subnets: List[Any] = []
    private_subnets: List[Any] = []
    public_by_az: Dict[str, List[Any]] = defaultdict(list)
    private_by_az: Dict[str, List[Any]] = defaultdict(list)

    if create_subnets:
        for i, cidr in enumerate(public_subnet_cidrs):
            az = use_azs[i % max(1, len(use_azs))] if use_azs else None
            sn = aws.ec2.Subnet(f"{name}-public-{i}", vpc_id=vpc.id, cidr_block=cidr, availability_zone=az, map_public_ip_on_launch=True, tags={"Name": f"{name}-public-{i}"})
            if public_rt:
                aws.ec2.RouteTableAssociation(f"{name}-pubrta-{i}", route_table_id=public_rt.id, subnet_id=sn.id)
            public_subnets.append(sn)
            public_by_az[az].append(sn)

        for i, cidr in enumerate(private_subnet_cidrs):
            az = use_azs[i % max(1, len(use_azs))] if use_azs else None
            sn = aws.ec2.Subnet(f"{name}-private-{i}", vpc_id=vpc.id, cidr_block=cidr, availability_zone=az, map_public_ip_on_launch=False, tags={"Name": f"{name}-private-{i}"})
            private_subnets.append(sn)
            private_by_az[az].append(sn)

    nat_gateways: List[Any] = []
    elastic_ips: List[Any] = []
    private_route_tables: List[Any] = []

    if not no_nat and create_subnets:
        azs_with_privates = [az for az in use_azs if private_by_az.get(az)]
        if not azs_with_privates and private_subnets:
            azs_with_privates = [use_azs[0] if use_azs else None]
        for idx, az in enumerate([a for a in azs_with_privates if a is not None]):
            eip = aws.ec2.Eip(f"{name}-nat-eip-{idx}", vpc=True, tags={"Name": f"{name}-nat-eip-{idx}"})
            elastic_ips.append(eip)
            pub_candidates = public_by_az.get(az) or public_subnets
            if not pub_candidates:
                raise Exception("No public subnets available to host NAT gateway; ensure PUBLIC_SUBNET_CIDRS provided")
            host_pub = pub_candidates[0]
            nat = aws.ec2.NatGateway(f"{name}-nat-{idx}", allocation_id=eip.id, subnet_id=host_pub.id, tags={"Name": f"{name}-nat-{idx}"})
            nat_gateways.append(nat)
            prt = aws.ec2.RouteTable(f"{name}-private-rt-{idx}", vpc_id=vpc.id, routes=[aws.ec2.RouteTableRouteArgs(cidr_block="0.0.0.0/0", nat_gateway_id=nat.id)], tags={"Name": f"{name}-private-rt-{idx}"})
            private_route_tables.append(prt)
            for j, priv in enumerate(private_by_az.get(az, [])):
                aws.ec2.RouteTableAssociation(f"{name}-privrta-{idx}-{j}", route_table_id=prt.id, subnet_id=priv.id)
    elif create_subnets:
        for idx, priv in enumerate(private_subnets):
            prt = aws.ec2.RouteTable(f"{name}-private-rt-{idx}", vpc_id=vpc.id, tags={"Name": f"{name}-private-rt-{idx}"})
            private_route_tables.append(prt)
            aws.ec2.RouteTableAssociation(f"{name}-privrta-{idx}", route_table_id=prt.id, subnet_id=priv.id)

    vpc_endpoints: Dict[str, Any] = {}
    endpoints_needed = create_vpc_endpoints or no_nat
    if endpoints_needed:
        pulumi.log.info("Creating VPC endpoints")
        route_table_ids = []
        if public_rt:
            route_table_ids.append(public_rt.id)
        route_table_ids += [rt.id for rt in private_route_tables]
        if route_table_ids:
            s3_ep = aws.ec2.VpcEndpoint(f"{name}-vpce-s3",
                                       vpc_id=vpc.id,
                                       service_name=f"com.amazonaws.{aws_region}.s3",
                                       vpc_endpoint_type="Gateway",
                                       route_table_ids=route_table_ids,
                                       tags={"Name": f"{name}-vpce-s3"})
            vpc_endpoints["s3"] = s3_ep

        interface_services = [s.strip() for s in (os.getenv("VPC_ENDPOINT_SERVICES") or "ssm,ssmmessages,ec2messages,sts,secretsmanager,ecr.api,ecr.dkr").split(",") if s.strip()]
        try:
            vpc_cidr_block = vpc.cidr_block
            ingress_cidrs = [vpc_cidr_block]
        except Exception:
            ingress_cidrs = ["0.0.0.0/0"]
        endpoint_sg = aws.ec2.SecurityGroup(f"{name}-vpce-sg",
                                           vpc_id=vpc.id,
                                           description="VPC endpoint security group",
                                           ingress=[aws.ec2.SecurityGroupIngressArgs(protocol="tcp", from_port=443, to_port=443, cidr_blocks=ingress_cidrs)],
                                           egress=[aws.ec2.SecurityGroupEgressArgs(protocol="-1", from_port=0, to_port=0, cidr_blocks=["0.0.0.0/0"])],
                                           tags={"Name": f"{name}-vpce-sg"})
        subnet_ids_for_endpoints: List[pulumi.Input[str]] = []
        for az in use_azs:
            chosen = None
            if private_by_az.get(az):
                chosen = private_by_az[az][0]
            elif public_by_az.get(az):
                chosen = public_by_az[az][0]
            elif private_subnets:
                chosen = private_subnets[0]
            elif public_subnets:
                chosen = public_subnets[0]
            if chosen:
                subnet_ids_for_endpoints.append(chosen.id)
        for svc in interface_services:
            svc_name = f"com.amazonaws.{aws_region}.{svc}"
            ep = aws.ec2.VpcEndpoint(f"{name}-vpce-{svc.replace('.', '-')}",
                                     vpc_id=vpc.id,
                                     service_name=svc_name,
                                     vpc_endpoint_type="Interface",
                                     subnet_ids=subnet_ids_for_endpoints,
                                     security_group_ids=[endpoint_sg.id],
                                     private_dns_enabled=True,
                                     tags={"Name": f"{name}-vpce-{svc}"})
            vpc_endpoints[svc] = ep

    return {
        "vpc": vpc,
        "internet_gateway": igw,
        "public_subnets": public_subnets,
        "private_subnets": private_subnets,
        "public_route_table": public_rt,
        "private_route_tables": private_route_tables,
        "nat_gateways": nat_gateways,
        "elastic_ips": elastic_ips,
        "vpc_endpoints": vpc_endpoints,
    }
