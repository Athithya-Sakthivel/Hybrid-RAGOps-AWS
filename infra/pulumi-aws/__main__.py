from __future__ import annotations
import os
import json
import ipaddress
import textwrap
from pathlib import Path
from typing import Any, Dict, List, Optional
from collections import defaultdict
import pulumi
import pulumi_aws as aws
import pulumi_tls as tls
def require_provider_versions(expected_pulumi="3.196.0", expected_pulumi_aws="7.7.0"):
    import pulumi as _pulumi
    import pulumi_aws as _pulumi_aws
    pv = getattr(_pulumi, "__version__", None)
    av = getattr(_pulumi_aws, "__version__", None)
    if pv is None or av is None:
        raise RuntimeError(f"Pulumi or pulumi_aws version info missing. Expected pulumi=={expected_pulumi}, pulumi-aws=={expected_pulumi_aws}")
    if pv != expected_pulumi or av != expected_pulumi_aws:
        raise RuntimeError(f"Version mismatch: pulumi=={pv} pulumi-aws=={av}; expected pulumi=={expected_pulumi} pulumi-aws=={expected_pulumi_aws}")
require_provider_versions()
def _bool_env(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return str(v).lower() not in ("0", "false", "no", "")
def _get(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name)
    if v is None:
        v = pulumi.Config().get(name)
    return v if v is not None else default
def _bool(name: str, default: bool) -> bool:
    v = _get(name, None)
    if v is None:
        return default
    return str(v).lower() not in ("0", "false", "no", "")
def _list(name: str, default: str = "") -> List[str]:
    v = _get(name, default) or ""
    return [s.strip() for s in v.split(",") if s.strip()]
def _int(name: str, default: int) -> int:
    v = _get(name, None)
    if v is None:
        return default
    return int(v)
def _validate_cidrs(cidrs: List[str]) -> None:
    for c in cidrs:
        try:
            ipaddress.ip_network(c)
        except Exception as e:
            raise ValueError(f"Invalid CIDR '{c}': {e}")
def _ec2_assume_policy():
    return aws.iam.get_policy_document(statements=[{
        "Effect": "Allow",
        "Principals": [{"Type": "Service", "Identifiers": ["ec2.amazonaws.com"]}],
        "Actions": ["sts:AssumeRole"],
    }])
def create_prereqs():
    STACK = pulumi.get_stack()
    cfg = pulumi.Config()
    try:
        REDIS_PASSWORD = cfg.require_secret("redisPassword")
    except Exception:
        REDIS_PASSWORD = os.getenv("REDIS_PASSWORD")
    if REDIS_PASSWORD is None:
        raise Exception("REDIS_PASSWORD not provided. Set via 'pulumi config set --secret redisPassword <val>' or env REDIS_PASSWORD")
    REDIS_SSM_PARAM = os.getenv("REDIS_SSM_PARAM", "/ray/prod/redis_password")
    KMS_ALIAS = os.getenv("KMS_ALIAS", "alias/ray-ssm-key")
    key_resource = None
    alias_resource = None
    try:
        existing = aws.kms.get_alias(name=KMS_ALIAS)
        key_resource = aws.kms.Key.get("raySsmKeyExisting", existing.target_key_id)
        pulumi.log.info(f"Found existing KMS alias {KMS_ALIAS}, reusing key {existing.target_key_id}")
    except Exception:
        key_resource = aws.kms.Key("raySsmKey",
            description=f"CMK for Ray SSM SecureString ({STACK})",
            deletion_window_in_days=30,
            tags={"Name": f"ray-ssm-key-{STACK}"}
        )
        alias_resource = aws.kms.Alias("raySsmKeyAlias",
            name=KMS_ALIAS,
            target_key_id=key_resource.key_id
        )
        pulumi.log.info(f"Created new KMS key + alias {KMS_ALIAS}")
    param_args = dict(
        name=REDIS_SSM_PARAM,
        type="SecureString",
        value=REDIS_PASSWORD,
        key_id=key_resource.arn,
        tags={"Name": f"ray-redis-password-{STACK}"},
        overwrite=True,
    )
    ssm_param = aws.ssm.Parameter("rayRedisParameter", **param_args)
    return {"key": key_resource, "ssm_param": ssm_param}
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
    if use_existing_vpc_id:
        vpc = aws.ec2.get_vpc(id=use_existing_vpc_id)
    else:
        vpc = aws.ec2.Vpc(f"{name}-vpc",
                          cidr_block=vpc_cidr,
                          enable_dns_hostnames=True,
                          enable_dns_support=True,
                          tags={"Name": f"{name}-vpc"})
    igw = aws.ec2.InternetGateway(f"{name}-igw", vpc_id=vpc.id, tags={"Name": f"{name}-igw"}) if not use_existing_vpc_id else None
    public_rt = None
    if igw:
        public_rt = aws.ec2.RouteTable(f"{name}-public-rt",
                                       vpc_id=vpc.id,
                                       routes=[aws.ec2.RouteTableRouteArgs(cidr_block="0.0.0.0/0", gateway_id=igw.id)],
                                       tags={"Name": f"{name}-public-rt"})
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
            try:
                eip = aws.ec2.Eip(f"{name}-nat-eip-{idx}", domain="vpc", tags={"Name": f"{name}-nat-eip-{idx}"})
            except TypeError as te:
                raise RuntimeError(f"Provider compatibility error creating EIP: {te}")
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
def create_auth(
    name: str = "ray",
    multi_az: Optional[bool] = None,
    no_nat: Optional[bool] = None,
    create_vpc_endpoints: Optional[bool] = None,
    aws_region: Optional[str] = None
) -> Dict[str, Any]:
    aws_region = aws_region or _get("AWS_REGION", "ap-south-1")
    NAME_PREFIX = _get("NETWORK_NAME", name)
    APP_PORT = _int("APP_PORT", 8003)
    APP_HEALTH_PATH = _get("APP_HEALTH_PATH", "/healthz")
    ALB_PROTECTED_PATHS = _list("ALB_PROTECTED_PATHS", "/query*,/embed*,/rerank*")
    DOMAIN = _get("DOMAIN", None)
    HOSTED_ZONE_ID = _get("HOSTED_ZONE_ID", None)
    COGNITO_DOMAIN_PREFIX = _get("COGNITO_DOMAIN_PREFIX", f"{NAME_PREFIX}-{pulumi.get_stack()}")
    COGNITO_AUTO_VERIFY_EMAIL = _bool("COGNITO_AUTO_VERIFY_EMAIL", True)
    COGNITO_MFA = _get("COGNITO_MFA", "OFF").upper()
    if COGNITO_MFA not in ("OFF", "OPTIONAL", "ON"):
        raise pulumi.ResourceError("COGNITO_MFA must be OFF, OPTIONAL, or ON", resource=None)
    COGNITO_PASSWORD_MIN_LENGTH = _int("COGNITO_PASSWORD_MIN_LENGTH", 8)
    COGNITO_PASSWORD_REQUIRE_LOWER = _bool("COGNITO_PASSWORD_REQUIRE_LOWER", True)
    COGNITO_PASSWORD_REQUIRE_UPPER = _bool("COGNITO_PASSWORD_REQUIRE_UPPER", False)
    COGNITO_PASSWORD_REQUIRE_SYMBOL = _bool("COGNITO_PASSWORD_REQUIRE_SYMBOL", False)
    COGNITO_PASSWORD_REQUIRE_NUMBER = _bool("COGNITO_PASSWORD_REQUIRE_NUMBER", True)
    COGNITO_APP_GENERATE_SECRET = _bool("COGNITO_APP_GENERATE_SECRET", False)
    COGNITO_OAUTH_FLOWS = _list("COGNITO_OAUTH_FLOWS", "code")
    COGNITO_OAUTH_SCOPES = _list("COGNITO_OAUTH_SCOPES", "openid,email,profile")
    COGNITO_CALLBACK_URLS = _list("COGNITO_CALLBACK_URLS", "")
    COGNITO_LOGOUT_URLS = _list("COGNITO_LOGOUT_URLS", "")
    COGNITO_ID_TOKEN_VALIDITY = _int("COGNITO_ID_TOKEN_VALIDITY", 3600)
    COGNITO_ACCESS_TOKEN_VALIDITY = _int("COGNITO_ACCESS_TOKEN_VALIDITY", 3600)
    COGNITO_REFRESH_TOKEN_VALIDITY = _int("COGNITO_REFRESH_TOKEN_VALIDITY", 30)
    PREAUTH_LAMBDA_ENABLE = _bool("PREAUTH_LAMBDA_ENABLE", False)
    ALLOWED_EMAIL_DOMAINS = _list("ALLOWED_EMAIL_DOMAINS", "")
    if PREAUTH_LAMBDA_ENABLE and not ALLOWED_EMAIL_DOMAINS:
        raise pulumi.ResourceError("PREAUTH_LAMBDA_ENABLE=true requires ALLOWED_EMAIL_DOMAINS to be set", resource=None)
    net = create_network(name=NAME_PREFIX, multi_az=multi_az, no_nat=no_nat, create_vpc_endpoints=create_vpc_endpoints, aws_region=aws_region)
    vpc = net["vpc"]
    public_subnets = net["public_subnets"]
    private_subnets = net["private_subnets"]
    tags_common = {"Name": f"{NAME_PREFIX}-{pulumi.get_stack()}"}
    alb_sg = aws.ec2.SecurityGroup(f"{NAME_PREFIX}-alb-sg",
        vpc_id=vpc.id,
        description="ALB security group",
        ingress=[
            aws.ec2.SecurityGroupIngressArgs(protocol="tcp", from_port=443, to_port=443, cidr_blocks=["0.0.0.0/0"]),
            aws.ec2.SecurityGroupIngressArgs(protocol="tcp", from_port=80, to_port=80, cidr_blocks=["0.0.0.0/0"])
        ],
        egress=[aws.ec2.SecurityGroupEgressArgs(protocol="-1", from_port=0, to_port=0, cidr_blocks=["0.0.0.0/0"])],
        tags={**tags_common, "Component": "alb"})
    worker_sg = aws.ec2.SecurityGroup(f"{NAME_PREFIX}-worker-sg",
        vpc_id=vpc.id,
        description="Worker SG: allow ALB to APP_PORT only",
        ingress=[aws.ec2.SecurityGroupIngressArgs(protocol="tcp", from_port=APP_PORT, to_port=APP_PORT, security_groups=[alb_sg.id])],
        egress=[aws.ec2.SecurityGroupEgressArgs(protocol="-1", from_port=0, to_port=0, cidr_blocks=["0.0.0.0/0"])],
        tags={**tags_common, "Component": "worker"})
    pub_ids = [s.id for s in public_subnets] if public_subnets else []
    if pub_ids and len(pub_ids) < 2 and not _bool_env("MULTI_AZ_DEPLOYMENT", False):
        raise RuntimeError("Insufficient public subnets to create an internet-facing ALB. Set MULTI_AZ_DEPLOYMENT=true and provide at least 2 PUBLIC_SUBNET_CIDRS or unset DOMAIN to skip ACM/ALB automation.")
    alb = None
    tg = None
    if pub_ids:
        alb = aws.lb.LoadBalancer(f"{NAME_PREFIX}-alb",
            internal=False,
            load_balancer_type="application",
            security_groups=[alb_sg.id],
            subnets=pub_ids,
            enable_deletion_protection=False,
            tags={**tags_common, "Resource": "alb"})
        tg = aws.lb.TargetGroup(f"{NAME_PREFIX}-tg",
            port=APP_PORT,
            protocol="HTTP",
            target_type="instance",
            vpc_id=vpc.id,
            health_check=aws.lb.TargetGroupHealthCheckArgs(path=APP_HEALTH_PATH, protocol="HTTP", port=str(APP_PORT), interval=15, timeout=5, healthy_threshold=2, unhealthy_threshold=2),
            deregistration_delay=300,
            tags={**tags_common, "Resource": "target-group"})
        http_listener = aws.lb.Listener(f"{NAME_PREFIX}-http-listener",
            load_balancer_arn=alb.arn,
            port=80,
            protocol="HTTP",
            default_actions=[aws.lb.ListenerDefaultActionArgs(type="redirect", redirect=aws.lb.ListenerDefaultActionRedirectArgs(protocol="HTTPS", port="443", status_code="HTTP_301"))])
    certificate_arn = None
    if DOMAIN:
        cert = aws.acm.Certificate(f"{NAME_PREFIX}-cert",
            domain_name=DOMAIN,
            validation_method="DNS",
            subject_alternative_names=[f"www.{DOMAIN}"] if DOMAIN else None,
            tags={**tags_common, "Resource": "acm-cert"})
        if HOSTED_ZONE_ID:
            def _mk_records(dvos):
                recs = []
                for i, dvo in enumerate(dvos):
                    rec = aws.route53.Record(f"{NAME_PREFIX}-certval-{i}",
                        zone_id=HOSTED_ZONE_ID,
                        name=dvo["resource_record_name"],
                        type=dvo["resource_record_type"],
                        records=[dvo["resource_record_value"]],
                        ttl=300)
                    recs.append(rec.fqdn)
                return recs
            cert_validation_record_fqdns = cert.domain_validation_options.apply(_mk_records)
            cert_validation = aws.acm.CertificateValidation(f"{NAME_PREFIX}-cert-validation",
                certificate_arn=cert.arn,
                validation_record_fqdns=cert_validation_record_fqdns)
            certificate_arn = cert_validation.certificate_arn
        else:
            pulumi.export("cert_domain_validation_options", cert.domain_validation_options)
            certificate_arn = cert.arn
    https_listener = None
    if pub_ids:
        https_listener = aws.lb.Listener(f"{NAME_PREFIX}-https-listener",
            load_balancer_arn=alb.arn,
            port=443,
            protocol="HTTPS",
            ssl_policy="ELBSecurityPolicy-TLS13-1-2-2021-06",
            certificate_arn=certificate_arn if certificate_arn else "",
            default_actions=[aws.lb.ListenerDefaultActionArgs(type="fixed-response", fixed_response=aws.lb.ListenerDefaultActionFixedResponseArgs(content_type="text/plain", status_code="404", message_body="Not found"))])
    preauth_lambda = None
    if PREAUTH_LAMBDA_ENABLE:
        lambda_role = aws.iam.Role(f"{NAME_PREFIX}-preauth-lambda-role",
            assume_role_policy=json.dumps({"Version":"2012-10-17","Statement":[{"Action":"sts:AssumeRole","Principal":{"Service":"lambda.amazonaws.com"},"Effect":"Allow"}]}))
        aws.iam.RolePolicyAttachment(f"{NAME_PREFIX}-preauth-lambda-basic",
            role=lambda_role.name,
            policy_arn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole")
        handler_code = """import os
def handler(event, context):
    allowed = [d.strip().lower() for d in os.getenv('ALLOWED_EMAIL_DOMAINS','').split(',') if d.strip()]
    if not allowed:
        return event
    attrs = event.get('request',{}).get('userAttributes') or {}
    email = attrs.get('email','') or ''
    if not email:
        raise Exception('PreAuth: missing email')
    domain = email.split('@')[-1].lower()
    if domain not in allowed:
        raise Exception('PreAuth: unauthorized domain')
    return event
"""
        code_asset = pulumi.AssetArchive({"index.py": pulumi.StringAsset(handler_code)})
        preauth_lambda = aws.lambda_.Function(f"{NAME_PREFIX}-preauth-lambda",
            role=lambda_role.arn,
            runtime="python3.10",
            handler="index.handler",
            code=code_asset,
            environment=aws.lambda_.FunctionEnvironmentArgs(variables={"ALLOWED_EMAIL_DOMAINS": ",".join(ALLOWED_EMAIL_DOMAINS)}))
    user_pool = aws.cognito.UserPool(f"{NAME_PREFIX}-userpool",
        auto_verified_attributes=(["email"] if COGNITO_AUTO_VERIFY_EMAIL else []),
        alias_attributes=(["email"] if COGNITO_AUTO_VERIFY_EMAIL else []),
        mfa_configuration=COGNITO_MFA,
        password_policy=aws.cognito.UserPoolPasswordPolicyArgs(
            minimum_length=COGNITO_PASSWORD_MIN_LENGTH,
            require_lowercase=COGNITO_PASSWORD_REQUIRE_LOWER,
            require_uppercase=COGNITO_PASSWORD_REQUIRE_UPPER,
            require_symbols=COGNITO_PASSWORD_REQUIRE_SYMBOL,
            require_numbers=COGNITO_PASSWORD_REQUIRE_NUMBER
        ),
        lambda_config=aws.cognito.UserPoolLambdaConfigArgs(pre_authentication=preauth_lambda.arn) if preauth_lambda else None,
        tags={**tags_common, "Resource": "cognito-userpool"})
    def _build_callback_urls(alb_dns: str) -> List[str]:
        if COGNITO_CALLBACK_URLS:
            return COGNITO_CALLBACK_URLS
        return [f"https://{alb_dns}/oauth2/idpresponse"]
    def _build_logout_urls(alb_dns: str) -> List[str]:
        if COGNITO_LOGOUT_URLS:
            return COGNITO_LOGOUT_URLS
        return [f"https://{alb_dns}/logout"]
    callback_urls_output = pulumi.Output.all(alb.dns_name if alb else "").apply(lambda args: _build_callback_urls(args[0] or ""))
    logout_urls_output = pulumi.Output.all(alb.dns_name if alb else "").apply(lambda args: _build_logout_urls(args[0] or ""))
    user_pool_client = aws.cognito.UserPoolClient(f"{NAME_PREFIX}-client",
        user_pool_id=user_pool.id,
        generate_secret=COGNITO_APP_GENERATE_SECRET,
        allowed_oauth_flows=COGNITO_OAUTH_FLOWS,
        allowed_oauth_scopes=COGNITO_OAUTH_SCOPES,
        callback_urls=callback_urls_output,
        logout_urls=logout_urls_output,
        supported_identity_providers=["COGNITO"],
        access_token_validity=COGNITO_ACCESS_TOKEN_VALIDITY,
        id_token_validity=COGNITO_ID_TOKEN_VALIDITY,
        refresh_token_validity=COGNITO_REFRESH_TOKEN_VALIDITY)
    user_pool_domain = aws.cognito.UserPoolDomain(f"{NAME_PREFIX}-domain",
        domain=COGNITO_DOMAIN_PREFIX,
        user_pool_id=user_pool.id,
        tags={**tags_common, "Resource": "cognito-domain"})
    auth_args = aws.lb.ListenerRuleActionAuthenticateCognitoArgs(
        user_pool_arn=user_pool.arn,
        user_pool_client_id=user_pool_client.id,
        user_pool_domain=user_pool_domain.domain,
        on_unauthenticated_request="authenticate"
    )
    for i, path in enumerate(ALB_PROTECTED_PATHS):
        if https_listener:
            aws.lb.ListenerRule(f"{NAME_PREFIX}-rule-{i}",
                listener_arn=https_listener.arn,
                priority=1000 + i,
                conditions=[aws.lb.ListenerRuleConditionArgs(path_pattern=aws.lb.ListenerRuleConditionPathPatternArgs(values=[path]))],
                actions=[
                    aws.lb.ListenerRuleActionArgs(type="authenticate-cognito", authenticate_cognito=auth_args),
                    aws.lb.ListenerRuleActionArgs(type="forward", target_group_arn=tg.arn)
                ])
    return {
        "network": net,
        "alb": alb,
        "tg": tg,
        "user_pool": user_pool,
        "user_pool_client": user_pool_client,
        "user_pool_domain": user_pool_domain,
        "certificate_arn": certificate_arn,
        "preauth_lambda": preauth_lambda,
        "worker_sg": worker_sg,
        "alb_sg": alb_sg,
    }
def create_ray_head_role(prefix: str = "rag"):
    assume = _ec2_assume_policy()
    role = aws.iam.Role(f"{prefix}-ray-head-role", assume_role_policy=assume.json)
    aws.iam.RolePolicyAttachment(f"{prefix}-head-ssm-attach", role=role.name,
                                 policy_arn="arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore")
    aws.iam.RolePolicyAttachment(f"{prefix}-head-cw-attach", role=role.name,
                                 policy_arn="arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy")
    aws.iam.RolePolicyAttachment(f"{prefix}-head-s3ro-attach", role=role.name,
                                 policy_arn="arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess")
    instance_profile = aws.iam.InstanceProfile(f"{prefix}-ray-head-instance-profile", role=role.name)
    return {"role": role, "instance_profile": instance_profile}
def create_ec2_role(prefix: str = "rag"):
    assume = _ec2_assume_policy()
    role = aws.iam.Role(f"{prefix}-ec2-role", assume_role_policy=assume.json)
    aws.iam.RolePolicyAttachment(f"{prefix}-ssm-attach", role=role.name,
                                 policy_arn="arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore")
    aws.iam.RolePolicyAttachment(f"{prefix}-cw-attach", role=role.name,
                                 policy_arn="arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy")
    aws.iam.RolePolicyAttachment(f"{prefix}-s3ro-attach", role=role.name,
                                 policy_arn="arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess")
    instance_profile = aws.iam.InstanceProfile(f"{prefix}-instance-profile", role=role.name)
    return {"role": role, "instance_profile": instance_profile}
def create_ray_cpu_worker_s3_role(prefix: str = "rag", bucket: Optional[str] = None):
    bucket = bucket or os.getenv("PULUMI_S3_BUCKET") or pulumi.Config().get("s3Bucket") or None
    if not bucket:
        raise Exception("create_ray_cpu_worker_s3_role: S3 bucket required")
    assume = _ec2_assume_policy()
    role = aws.iam.Role(f"{prefix}-ray-cpu-s3-role", assume_role_policy=assume.json)
    aws.iam.RolePolicyAttachment(f"{prefix}-ray-cpu-ssm-attach", role=role.name,
                                 policy_arn="arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore")
    aws.iam.RolePolicyAttachment(f"{prefix}-ray-cpu-cw-attach", role=role.name,
                                 policy_arn="arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy")
    s3_doc = {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": f"arn:aws:s3:::{bucket}"},
            {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:PutObjectAcl"], "Resource": f"arn:aws:s3:::{bucket}/*"}
        ]
    }
    policy = aws.iam.Policy(f"{prefix}-ray-cpu-s3-policy", policy=json.dumps(s3_doc))
    aws.iam.PolicyAttachment(f"{prefix}-ray-cpu-s3-attach", policy_arn=policy.arn, roles=[role.name])
    instance_profile = aws.iam.InstanceProfile(f"{prefix}-ray-cpu-s3-instance-profile", role=role.name)
    return {"role": role, "instance_profile": instance_profile, "policy": policy}
def attach_elbv2_register_policy(role: aws.iam.Role, target_group_arn: pulumi.Input[str], name_prefix: str = "rag"):
    def make_doc(tg_arn: str):
        doc = {
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Action": [
                    "elasticloadbalancing:RegisterTargets",
                    "elasticloadbalancing:DeregisterTargets",
                    "elasticloadbalancing:DescribeTargetHealth"
                ],
                "Resource": tg_arn
            }]
        }
        return json.dumps(doc)
    policy = aws.iam.Policy(f"{name_prefix}-elbv2-register-policy", policy=pulumi.Output.from_input(target_group_arn).apply(lambda tg: make_doc(tg)))
    aws.iam.PolicyAttachment(f"{name_prefix}-elbv2-register-attach", policy_arn=policy.arn, roles=[role.name])
    return policy
def create_and_attach_ssm_kms_read(role: aws.iam.Role, ssm_param_name: pulumi.Input[str], kms_arn: pulumi.Input[str], name: str = "ray-ssm-kms-policy"):
    region = aws.get_region().name
    account = aws.get_caller_identity().account_id
    def make_policy(args):
        ssm_name, kms_a = args
        ssm_arn = f"arn:aws:ssm:{region}:{account}:parameter{ssm_name}"
        doc = {"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["ssm:GetParameter","ssm:GetParametersByPath","ssm:GetParameters"],"Resource":ssm_arn},{"Effect":"Allow","Action":["kms:Decrypt","kms:GenerateDataKey"],"Resource":kms_a}]}
        return json.dumps(doc)
    policy_str = pulumi.Output.all(ssm_param_name, kms_arn).apply(lambda args: make_policy(args))
    policy = aws.iam.Policy(name, policy=policy_str)
    def attach(p):
        aws.iam.PolicyAttachment(f"{name}-attach-{role.name}", policy_arn=p.arn, roles=[role.name])
        return p
    return pulumi.Output.from_input(policy).apply(attach)
def upload_autoscaler_yaml_to_s3(
    bucket_name: Optional[str] = None,
    stack: str = "prod",
    head_profile_name: str = "",
    cpu_profile_name: str = "",
    head_ami: str = "",
    cpu_ami: str = "",
    cpu_instance: str = "m5.xlarge",
    cpu_min: int = 1,
    cpu_max: int = 6,
    idle_timeout_minutes: int = 10,
    ssh_user: str = "ubuntu",
    ssh_private_key: str = "~/.ssh/id_rsa",
    cpu_pip_packages: str = "ray[default]==2.5.0 httpx transformers"
):
    import yaml
    cfg = pulumi.Config()
    stack = pulumi.get_stack()
    aws_region = os.getenv("AWS_REGION") or aws.get_region().name
    if not cfg.get("vpcId"):
        raise RuntimeError("upload_autoscaler_yaml_to_s3 requires pulumi config 'vpcId'")
    if not cfg.get_object("subnetIds"):
        raise RuntimeError("upload_autoscaler_yaml_to_s3 requires pulumi config 'subnetIds'")
    VPC_ID = cfg.require("vpcId")
    SUBNET_IDS = cfg.require_object("subnetIds")
    KEY_NAME = cfg.get("keyName")
    bucket_name = bucket_name or os.getenv("PULUMI_S3_BUCKET") or cfg.get("s3Bucket")
    if not bucket_name:
        raise Exception("upload_autoscaler_yaml_to_s3: S3 bucket name required")
    autoscaler_config = {
        "cluster_name": f"ray-{stack}",
        "min_workers": 0,
        "max_workers": 10,
        "idle_timeout_minutes": idle_timeout_minutes,
        "provider": {
            "type": "aws",
            "region": aws_region
        },
        "auth": {
            "ssh_user": ssh_user,
            "ssh_private_key": ssh_private_key
        },
        "head_node": {
            "InstanceType": "m5.large",
            "ImageId": head_ami or "ami-0c55b159cbfafe1f0",
            "KeyName": KEY_NAME,
            "IamInstanceProfile": {"Name": head_profile_name} if head_profile_name else None,
            "SubnetId": SUBNET_IDS[0] if SUBNET_IDS else None,
            "SecurityGroupIds": [],
        },
        "available_node_types": {
            "ray.head.default": {
                "node_config": {
                    "InstanceType": "m5.large",
                    "ImageId": head_ami or "ami-0c55b159cbfafe1f0",
                    "KeyName": KEY_NAME,
                    "IamInstanceProfile": {"Name": head_profile_name} if head_profile_name else None,
                    "SubnetId": SUBNET_IDS[0] if SUBNET_IDS else None,
                    "SecurityGroupIds": [],
                },
                "max_workers": 0,
                "resources": {"CPU": 4}
            },
            "ray.worker.cpu": {
                "node_config": {
                    "InstanceType": cpu_instance,
                    "ImageId": cpu_ami or "ami-0c55b159cbfafe1f0",
                    "KeyName": KEY_NAME,
                    "IamInstanceProfile": {"Name": cpu_profile_name} if cpu_profile_name else None,
                    "SubnetId": SUBNET_IDS[0] if SUBNET_IDS else None,
                    "SecurityGroupIds": [],
                },
                "min_workers": cpu_min,
                "max_workers": cpu_max,
                "resources": {"CPU": 8},
              "setup_commands": [
    "sudo mkdir -p /usr/local/bin",
    "echo '#!/bin/bash\nwhile true; do\n  ray start --address=$RAY_HEAD_IP:6379 --redis-password=$REDIS_PASSWORD --block\n  sleep 5\ndone' | sudo tee /usr/local/bin/worker_reconnect.sh > /dev/null",
    "sudo chmod +x /usr/local/bin/worker_reconnect.sh",
    "echo '[Unit]\nDescription=Ray Worker Reconnect\nAfter=network-online.target\nWants=network-online.target\n[Service]\nType=simple\nExecStart=/usr/local/bin/worker_reconnect.sh\nRestart=always\nRestartSec=5\n[Install]\nWantedBy=multi-user.target' | sudo tee /etc/systemd/system/ray-reconnect.service > /dev/null",
    "sudo systemctl daemon-reload",
    "sudo systemctl enable ray-reconnect.service",
    "sudo systemctl start ray-reconnect.service",
]
        },
        "head_node_type": "ray.head.default",
        "worker_default_node_type": "ray.worker.cpu"
    }}
    yaml_content = yaml.dump(autoscaler_config, default_flow_style=False)
    s3_key = f"ray-autoscaler-{stack}.yaml"
    s3_object = aws.s3.BucketObject(f"autoscaler-yaml-{stack}",
                                    bucket=bucket_name,
                                    key=s3_key,
                                    source=pulumi.StringAsset(yaml_content),
                                    content_type="application/x-yaml")
    return {"s3_uri": f"s3://{bucket_name}/{s3_key}", "s3_object": s3_object}
def create_ray_head(
    stack: str,
    vpc_id: Optional[str],
    private_subnet_ids: List[str],
    head_ami: str,
    head_instance_type: str,
    instance_profile_name: str,
    autoscaler_s3: str,
    ssm_param_name: str,
    hosted_zone_id: Optional[str],
    hosted_zone_name: str,
    key_name: Optional[str] = None
):
    if not vpc_id:
        raise Exception("create_ray_head: vpc_id is required")
    if not private_subnet_ids:
        raise Exception("create_ray_head: private_subnet_ids are required")
    if not head_ami:
        raise Exception("create_ray_head: head_ami is required")
    if not instance_profile_name:
        raise Exception("create_ray_head: instance_profile_name is required")
    if not ssm_param_name:
        raise Exception("create_ray_head: ssm_param_name is required")
    if not hosted_zone_id:
        raise Exception("create_ray_head: hosted_zone_id is required for head DNS record")
    RAY_HEAD_DNS_NAME = f"ray-head.{stack}.{hosted_zone_name}"
    ASG_NAME = f"ray-head-asg-{stack}"
    LIFECYCLE_HOOK_NAME = f"wait-for-ray-ready-{stack}"
    assume_role = aws.iam.get_policy_document(statements=[{"Effect": "Allow", "Principals": [{"Type": "Service", "Identifiers": ["ec2.amazonaws.com"]}], "Actions": ["sts:AssumeRole"]}])
    head_role = aws.iam.Role(f"rayHeadRole-{stack}", assume_role_policy=assume_role.json)
    aws.iam.RolePolicyAttachment(f"ssmAttach-{stack}", role=head_role.name, policy_arn="arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore")
    aws.iam.RolePolicyAttachment(f"cwAttach-{stack}", role=head_role.name, policy_arn="arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy")
    aws.iam.RolePolicyAttachment(f"s3roAttach-{stack}", role=head_role.name, policy_arn="arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess")
    head_instance_profile = aws.iam.InstanceProfile(f"rayHeadInstanceProfile-{stack}", role=head_role.name)
    head_sg = aws.ec2.SecurityGroup(f"rayHeadSg-{stack}", vpc_id=vpc_id, description="ray head sg", ingress=[aws.ec2.SecurityGroupIngressArgs(protocol="tcp", from_port=6379, to_port=6379, cidr_blocks=["10.0.0.0/8"]), aws.ec2.SecurityGroupIngressArgs(protocol="tcp", from_port=8000, to_port=8000, cidr_blocks=["10.0.0.0/8"])], egress=[aws.ec2.SecurityGroupEgressArgs(protocol="-1", from_port=0, to_port=0, cidr_blocks=["0.0.0.0/0"])])
    create_and_attach_ssm_kms_read(head_role, ssm_param_name, os.getenv("REDIS_KMS_KEY_ARN") or pulumi.Config().get("redisKmsKeyArn") or "", f"ray-ssm-kms-{stack}")
    user_data_script = pulumi.Output.all(ASG_NAME, LIFECYCLE_HOOK_NAME, ssm_param_name, autoscaler_s3, os.getenv("AWS_REGION") or aws.get_region().name).apply(lambda args: f"""#!/bin/bash
set -euo pipefail
ASG_NAME="{args[0]}"
HOOK_NAME="{args[1]}"
SSM_PARAM="{args[2]}"
AUTOSCALER_S3="{args[3]}"
REGION="{args[4]}"
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
aws cloudwatch put-metric-data --region "$REGION" --namespace "Ray/Head" --metric-data MetricName=HeadReady,Dimensions=[{{Name=InstanceId,Value=$INSTANCE_ID}}],Value=1,Unit=Count
aws autoscaling complete-lifecycle-action --region "$REGION" --lifecycle-hook-name "$HOOK_NAME" --auto-scaling-group-name "$ASG_NAME" --lifecycle-action-result CONTINUE --instance-id "$INSTANCE_ID" || true
(
  while true; do
    aws cloudwatch put-metric-data --region "$REGION" --namespace "Ray/Head" --metric-data MetricName=HeadReady,Dimensions=[{{Name=InstanceId,Value=$INSTANCE_ID}}],Value=1,Unit=Count
    sleep 60
  done
) &
""")
    lt = aws.ec2.LaunchTemplate(f"rayHeadLaunchTemplate-{stack}", name_prefix=f"ray-head-lt-{stack}", image_id=head_ami, instance_type=head_instance_type, iam_instance_profile=aws.ec2.LaunchTemplateIamInstanceProfileArgs(name=instance_profile_name), network_interfaces=[aws.ec2.LaunchTemplateNetworkInterfaceArgs(subnet_id=private_subnet_ids[0], associate_public_ip_address=False, security_groups=[head_sg.id])], key_name=key_name, user_data=user_data_script.base64())
    tg = aws.lb.TargetGroup(f"rayHeadTg-{stack}", port=6379, protocol="TCP", target_type="instance", vpc_id=vpc_id, health_check=aws.lb.TargetGroupHealthCheckArgs(protocol="TCP", port="6379", interval=10, healthy_threshold=2, unhealthy_threshold=2))
    nlb = aws.lb.LoadBalancer(f"rayHeadNlb-{stack}", internal=True, load_balancer_type="network", subnets=private_subnet_ids, tags={"Name": f"ray-head-nlb-{stack}"})
    nlb_listener = aws.lb.Listener(f"nlbListener-{stack}", load_balancer_arn=nlb.arn, port=6379, protocol="TCP", default_actions=[aws.lb.ListenerDefaultActionArgs(type="forward", target_group_arn=tg.arn)])
    asg = aws.autoscaling.Group(f"rayHeadAsg-{stack}", name=ASG_NAME, desired_capacity=1, min_size=1, max_size=1, vpc_zone_identifiers=private_subnet_ids, launch_template=aws.autoscaling.GroupLaunchTemplateArgs(id=lt.id, version="$Latest"), target_group_arns=[tg.arn], health_check_type="ELB", health_check_grace_period=180, tags=[aws.autoscaling.GroupTagArgs(key="Name", value=f"ray-head-{stack}", propagate_at_launch=True)])
    lifecycle_hook = aws.autoscaling.LifecycleHook(f"rayHeadLifecycleHook-{stack}", autoscaling_group_name=asg.name, default_result="ABANDON", heartbeat_timeout=1800, lifecycle_transition="autoscaling:EC2_INSTANCE_LAUNCHING", name=LIFECYCLE_HOOK_NAME)
    record = aws.route53.Record(f"rayHeadRecord-{stack}", zone_id=hosted_zone_id, name=RAY_HEAD_DNS_NAME, type="A", aliases=[aws.route53.RecordAliasArgs(name=nlb.dns_name, zone_id=nlb.zone_id, evaluate_target_health=False)])
    topic = aws.sns.Topic(f"rayHeadAlertsTopic-{stack}")
    alarm = aws.cloudwatch.MetricAlarm(f"rayHeadReadyAlarm-{stack}", alarm_name=f"ray-head-ready-{stack}", comparison_operator="LessThanThreshold", evaluation_periods=3, metric_name="HeadReady", namespace="Ray/Head", period=60, statistic="Maximum", threshold=1, alarm_actions=[topic.arn], dimensions={"InstanceId": asg.name})
    sys_alarm = aws.cloudwatch.MetricAlarm(f"ec2SystemStatusAlarm-{stack}", alarm_name=f"ec2-system-status-{stack}", comparison_operator="GreaterThanOrEqualToThreshold", evaluation_periods=2, metric_name="StatusCheckFailed_System", namespace="AWS/EC2", period=60, statistic="Maximum", threshold=1, alarm_actions=[topic.arn])
    return {
        "asg": asg,
        "record": record,
        "nlb": nlb,
        "topic": topic,
        "alarm": alarm,
        "sys_alarm": sys_alarm
    }
log = pulumi.log.info
STACK = pulumi.get_stack()
ENABLE_PREREQS = os.getenv("ENABLE_PREREQS", "true").lower() != "false"
ENABLE_NETWORKING = os.getenv("ENABLE_NETWORKING", "true").lower() != "false"
ENABLE_IAM = os.getenv("ENABLE_IAM", "true").lower() != "false"
ENABLE_RENDERER = os.getenv("ENABLE_RENDERER", "false").lower() != "false"
ENABLE_HEAD = os.getenv("ENABLE_HEAD", "false").lower() != "false"
outputs: Dict[str, Any] = {}
resources: Dict[str, Any] = {}
if ENABLE_PREREQS:
    log("Running prerequisites step")
    try:
        pr = create_prereqs()
        resources["prereqs"] = pr
        outputs["redis_parameter_name"] = pr.get("ssm_param").name if pr.get("ssm_param") else None
        outputs["redis_kms_key_arn"] = pr.get("key").arn if pr.get("key") else None
        outputs["redis_kms_key_id"] = pr.get("key").key_id if pr.get("key") else None
    except Exception as e:
        pulumi.log.error(f"prerequisites.create_prereqs() failed: {e}")
        pulumi.export("prereqs_step_failed", str(e))
        raise
else:
    log("Skipping prerequisites step (disabled)")
if ENABLE_NETWORKING:
    log("Running auth (networking + auth) step")
    multi_az = os.getenv("MULTI_AZ_DEPLOYMENT", "false").lower() == "true"
    no_nat = os.getenv("NO_NAT", "false").lower() == "true"
    create_vpc_endpoints = os.getenv("CREATE_VPC_ENDPOINTS", "false").lower() == "true"
    try:
        auth_res = create_auth(name=os.getenv("NETWORK_NAME", "ray"), multi_az=multi_az, no_nat=no_nat, create_vpc_endpoints=create_vpc_endpoints)
        if not auth_res:
            raise RuntimeError("create_auth returned no result")
        net = auth_res.get("network") if auth_res and isinstance(auth_res, dict) else getattr(auth_res, "network", None) or auth_res
        if not net or (not getattr(net, "vpc", None) and not (isinstance(net, dict) and net.get("vpc"))):
            raise RuntimeError("create_auth returned incomplete network info")
        vpc = net["vpc"] if isinstance(net, dict) else getattr(net, "vpc", None)
        public_subnets = net.get("public_subnets") if isinstance(net, dict) else getattr(net, "public_subnets", None)
        private_subnets = net.get("private_subnets") if isinstance(net, dict) else getattr(net, "private_subnets", None)
        internet_gateway = net.get("internet_gateway") if isinstance(net, dict) else getattr(net, "internet_gateway", None)
        nat_gateways = net.get("nat_gateways") if isinstance(net, dict) else getattr(net, "nat_gateways", None)
        outputs["vpc_id"] = getattr(vpc, "id", None)
        outputs["public_subnet_ids"] = [s.id for s in (public_subnets or [])] if public_subnets else None
        outputs["private_subnet_ids"] = [s.id for s in (private_subnets or [])] if private_subnets else None
        outputs["internet_gateway_id"] = getattr(internet_gateway, "id", None)
        outputs["nat_gateway_ids"] = [ng.id for ng in (nat_gateways or [])] if nat_gateways else None
        alb = auth_res.get("alb") if isinstance(auth_res, dict) else getattr(auth_res, "alb", None)
        tg = auth_res.get("tg") if isinstance(auth_res, dict) else getattr(auth_res, "tg", None)
        user_pool = auth_res.get("user_pool") if isinstance(auth_res, dict) else getattr(auth_res, "user_pool", None)
        user_pool_client = auth_res.get("user_pool_client") if isinstance(auth_res, dict) else getattr(auth_res, "user_pool_client", None)
        user_pool_domain = auth_res.get("user_pool_domain") if isinstance(auth_res, dict) else getattr(auth_res, "user_pool_domain", None)
        certificate_arn = auth_res.get("certificate_arn") if isinstance(auth_res, dict) else getattr(auth_res, "certificate_arn", None)
        preauth_lambda = auth_res.get("preauth_lambda") if isinstance(auth_res, dict) else getattr(auth_res, "preauth_lambda", None)
        alb_sg = auth_res.get("alb_sg") if isinstance(auth_res, dict) else getattr(auth_res, "alb_sg", None)
        worker_sg = auth_res.get("worker_sg") if isinstance(auth_res, dict) else getattr(auth_res, "worker_sg", None)
        if alb:
            outputs["alb_dns"] = getattr(alb, "dns_name", None)
            outputs["alb_arn"] = getattr(alb, "arn", None)
            outputs["alb_zone_id"] = getattr(alb, "zone_id", None)
        if tg:
            outputs["target_group_arn"] = getattr(tg, "arn", None)
            outputs["target_group_name"] = getattr(tg, "name", None)
        if user_pool:
            outputs["cognito_user_pool_id"] = getattr(user_pool, "id", None)
            outputs["cognito_user_pool_arn"] = getattr(user_pool, "arn", None)
        if user_pool_client:
            outputs["cognito_user_pool_client_id"] = getattr(user_pool_client, "id", None)
            outputs["cognito_user_pool_client_arn"] = getattr(user_pool_client, "arn", None)
        if user_pool_domain:
            outputs["cognito_user_pool_domain"] = getattr(user_pool_domain, "domain", None)
            outputs["cognito_user_pool_domain_arn"] = getattr(user_pool_domain, "arn", None)
        outputs["certificate_arn"] = certificate_arn
        if preauth_lambda:
            outputs["preauth_lambda_arn"] = getattr(preauth_lambda, "arn", None)
        if alb_sg:
            outputs["alb_security_group_id"] = getattr(alb_sg, "id", None)
        if worker_sg:
            outputs["worker_security_group_id"] = getattr(worker_sg, "id", None)
    except Exception as e:
        pulumi.log.error(f"auth.create_auth() failed: {e}")
        pulumi.export("auth_step_failed", str(e))
        raise
else:
    log("Skipping auth/networking step (disabled)")
if ENABLE_IAM:
    log("Running iam step")
    try:
        iam_results = {}
        iam_results["head"] = create_ray_head_role(prefix=os.getenv("IAM_PREFIX", "rag"))
        iam_results["ec2"] = create_ec2_role(prefix=os.getenv("IAM_PREFIX", "rag"))
        try:
            bucket_name = os.getenv("AUTOSCALER_BUCKET_NAME") or os.getenv("PULUMI_S3_BUCKET")
            if bucket_name:
                iam_results["cpu_s3"] = create_ray_cpu_worker_s3_role(prefix=os.getenv("IAM_PREFIX", "rag"), bucket=bucket_name)
            else:
                log("IAM: create_ray_cpu_worker_s3_role requires AUTOSCALER_BUCKET_NAME or PULUMI_S3_BUCKET to be set.")
        except Exception as e:
            log(f"create_ray_cpu_worker_s3_role skipped/failed: {e}")
        resources["iam"] = iam_results
        head_data = iam_results.get("head")
        cpu_s3_data = iam_results.get("cpu_s3")
        ec2_data = iam_results.get("ec2")
        if head_data:
            outputs["ray_head_instance_profile"] = getattr(head_data.get("instance_profile"), "name", None)
            outputs["ray_head_role_arn"] = getattr(head_data.get("role"), "arn", None)
        if cpu_s3_data:
            outputs["ray_cpu_instance_profile"] = getattr(cpu_s3_data.get("instance_profile"), "name", None)
            outputs["ray_cpu_role_arn"] = getattr(cpu_s3_data.get("role"), "arn", None)
            outputs["ray_cpu_s3_policy_arn"] = getattr(cpu_s3_data.get("policy"), "arn", None)
        if ec2_data:
            outputs["ray_ec2_instance_profile"] = getattr(ec2_data.get("instance_profile"), "name", None)
            outputs["ray_ec2_role_arn"] = getattr(ec2_data.get("role"), "arn", None)
    except Exception as e:
        pulumi.log.error(f"IAM creation failed: {e}")
        pulumi.export("iam_step_failed", str(e))
        raise
else:
    log("Skipping iam step (disabled)")
if ENABLE_RENDERER:
    log("Running autoscaler renderer step")
    try:
        head_profile = outputs.get("ray_head_instance_profile") or ""
        cpu_profile = outputs.get("ray_cpu_instance_profile") or ""
        render_res = upload_autoscaler_yaml_to_s3(
            bucket_name=os.getenv("AUTOSCALER_BUCKET_NAME") or None,
            stack=STACK,
            head_profile_name=head_profile,
            cpu_profile_name=cpu_profile,
            head_ami=os.getenv("HEAD_AMI", ""),
            cpu_ami=os.getenv("RAY_CPU_AMI", ""),
            cpu_instance=os.getenv("RAY_CPU_INSTANCE", "m5.xlarge"),
            cpu_min=int(os.getenv("RAY_CPU_MIN_WORKERS", "1")),
            cpu_max=int(os.getenv("RAY_CPU_MAX_WORKERS", "6")),
            idle_timeout_minutes=int(os.getenv("RAY_IDLE_TIMEOUT_MINUTES", "10")),
            ssh_user=os.getenv("RSV_SSH_USER", "ubuntu"),
            ssh_private_key=os.getenv("RSV_SSH_PRIVATE_KEY", "~/.ssh/id_rsa"),
            cpu_pip_packages=os.getenv("RAY_DEFAULT_PIP_PACKAGES", "ray[default]==2.5.0 httpx transformers")
        )
        resources["renderer"] = render_res
        outputs["autoscaler_s3"] = render_res.get("s3_uri") if isinstance(render_res, dict) else render_res
    except Exception as e:
        pulumi.log.error(f"renderer step failed: {e}")
        pulumi.export("renderer_step_failed", str(e))
        raise
else:
    log("Skipping renderer step (disabled)")
if ENABLE_HEAD:
    log("Running ray head step")
    try:
        vpc_id = outputs.get("vpc_id")
        private_subnet_ids = outputs.get("private_subnet_ids")
        if private_subnet_ids and isinstance(private_subnet_ids, list) and hasattr(private_subnet_ids[0], "id"):
            private_subnet_ids = [s.id for s in private_subnet_ids]
        autoscaler_s3 = outputs.get("autoscaler_s3") or ""
        ssm_param_name = os.getenv("REDIS_SSM_PARAM", "/ray/prod/redis_password")
        hosted_zone_id = os.getenv("PRIVATE_HOSTED_ZONE_ID")
        hosted_zone_name = os.getenv("HOSTED_ZONE_NAME", "prod.internal.example.com")
        head_res = create_ray_head(
            stack=STACK,
            vpc_id=vpc_id,
            private_subnet_ids=private_subnet_ids or [],
            head_ami=os.getenv("HEAD_AMI", ""),
            head_instance_type=os.getenv("HEAD_INSTANCE_TYPE", "m5.large"),
            instance_profile_name=outputs.get("ray_head_instance_profile") or "",
            autoscaler_s3=autoscaler_s3,
            ssm_param_name=ssm_param_name,
            hosted_zone_id=hosted_zone_id,
            hosted_zone_name=hosted_zone_name,
            key_name=os.getenv("KEY_NAME", None)
        )
        resources["head"] = head_res
        if head_res.get("asg"):
            outputs["head_asg_name"] = head_res["asg"].name
        if head_res.get("record"):
            outputs["head_dns"] = head_res["record"].fqdn
        if head_res.get("nlb"):
            outputs["nlb_dns"] = head_res["nlb"].dns_name
        if head_res.get("topic"):
            outputs["head_alerts_topic"] = head_res["topic"].arn
        if head_res.get("alarm"):
            outputs["head_ready_alarm"] = head_res["alarm"].alarm_name
        if head_res.get("sys_alarm"):
            outputs["head_system_alarm"] = head_res["sys_alarm"].alarm_name
    except Exception as e:
        pulumi.log.error(f"ray head step failed: {e}")
        pulumi.export("head_step_failed", str(e))
        raise
else:
    log("Skipping ray head step (disabled)")
for k, v in outputs.items():
    if v is None:
        continue
    try:
        pulumi.export(k, v)
    except Exception as e:
        try:
            if isinstance(v, list):
                pulumi.export(k, v)
            else:
                log(f"Could not export {k} (value: {v}, error: {e})")
        except Exception:
            log(f"Could not export {k} (value: {v}, error: {e})")
log("__main__.py completed. Modules executed: " +
    f"prereqs={ENABLE_PREREQS}, " +
    f"auth={ENABLE_NETWORKING}, " +
    f"iam={ENABLE_IAM}, " +
    f"renderer={ENABLE_RENDERER}, " +
    f"head={ENABLE_HEAD}")
