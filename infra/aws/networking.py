# infra/aws_networking.py
from __future__ import annotations
import pulumi
import pulumi_aws as aws
def create_network(name, vpc_cidr="10.0.0.0/16", public_subnet_cidrs=None, private_subnet_cidrs=None, multi_az=False, no_nat=False, create_vpc_endpoints=False, aws_region=None):
    public_subnet_cidrs = public_subnet_cidrs or ["10.0.1.0/24", "10.0.2.0/24"]
    private_subnet_cidrs = private_subnet_cidrs or ["10.0.11.0/24", "10.0.12.0/24"]
    azs = aws.get_availability_zones(state="available").names
    if multi_az:
        azs = azs[:max(1, len(public_subnet_cidrs))]
    else:
        azs = azs[:1]
    vpc = aws.ec2.Vpc(f"{name}-vpc", cidr_block=vpc_cidr, enable_dns_hostnames=True, enable_dns_support=True, tags={"Name": f"{name}-vpc"})
    igw = aws.ec2.InternetGateway(f"{name}-igw", vpc_id=vpc.id, tags={"Name": f"{name}-igw"})
    public_subnets = []
    for i, cidr in enumerate(public_subnet_cidrs[:len(azs)]):
        subnet = aws.ec2.Subnet(f"{name}-public-{i+1}", vpc_id=vpc.id, cidr_block=cidr, availability_zone=azs[i % len(azs)], map_public_ip_on_launch=True, tags={"Name": f"{name}-public-{i+1}"})
        public_subnets.append(subnet)
    private_subnets = []
    for i, cidr in enumerate(private_subnet_cidrs[:len(azs)]):
        subnet = aws.ec2.Subnet(f"{name}-private-{i+1}", vpc_id=vpc.id, cidr_block=cidr, availability_zone=azs[i % len(azs)], tags={"Name": f"{name}-private-{i+1}"})
        private_subnets.append(subnet)
    public_rt = aws.ec2.RouteTable(f"{name}-public-rt", vpc_id=vpc.id, tags={"Name": f"{name}-public-rt"})
    aws.ec2.Route(f"{name}-public-route", route_table_id=public_rt.id, destination_cidr_block="0.0.0.0/0", gateway_id=igw.id)
    for i, subnet in enumerate(public_subnets):
        aws.ec2.RouteTableAssociation(f"{name}-public-assoc-{i+1}", subnet_id=subnet.id, route_table_id=public_rt.id)
    nat_gateways = []
    if not no_nat:
        for i, subnet in enumerate(public_subnets):
            eip = aws.ec2.Eip(f"{name}-nat-eip-{i+1}", domain="vpc")
            nat = aws.ec2.NatGateway(f"{name}-nat-{i+1}", allocation_id=eip.id, subnet_id=subnet.id, tags={"Name": f"{name}-nat-{i+1}"})
            nat_gateways.append(nat)
    for i, subnet in enumerate(private_subnets):
        rt = aws.ec2.RouteTable(f"{name}-private-rt-{i+1}", vpc_id=vpc.id, tags={"Name": f"{name}-private-rt-{i+1}"})
        if nat_gateways:
            nat_id = nat_gateways[i % len(nat_gateways)].id
            aws.ec2.Route(f"{name}-private-route-{i+1}", route_table_id=rt.id, destination_cidr_block="0.0.0.0/0", nat_gateway_id=nat_id)
        else:
            aws.ec2.Route(f"{name}-private-route-{i+1}", route_table_id=rt.id, destination_cidr_block="0.0.0.0/0", gateway_id=igw.id)
        aws.ec2.RouteTableAssociation(f"{name}-private-assoc-{i+1}", subnet_id=subnet.id, route_table_id=rt.id)
    if create_vpc_endpoints and aws_region:
        aws.ec2.VpcEndpoint(f"{name}-s3-endpoint", vpc_id=vpc.id, service_name=f"com.amazonaws.{aws_region}.s3", route_table_ids=[public_rt.id])
    return {"vpc": vpc, "public_subnets": public_subnets, "private_subnets": private_subnets}
