# infra/pulumi-aws/auth.py
from __future__ import annotations
import os
import json
import pulumi
import pulumi_aws as aws
from typing import Any, Dict, List, Optional
from networking import create_network

cfg = pulumi.Config()
stack = pulumi.get_stack()

def _get(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name)
    if v is None:
        v = cfg.get(name)
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

def create_auth(
    name: str = "ray",
    multi_az: Optional[bool] = None,
    no_nat: Optional[bool] = None,
    create_vpc_endpoints: Optional[bool] = None,
    aws_region: Optional[str] = None
) -> Dict[str, Any]:
    """
    Create networking + ALB + Cognito user pool + optional preauth lambda.
    Returns a dict with keys including 'network', 'alb', 'tg', 'user_pool', 'user_pool_client', 'user_pool_domain', 'certificate_arn', 'preauth_lambda'.
    """

    # read envs (defaults)
    aws_region = aws_region or _get("AWS_REGION", None) or aws.get_region().name
    NAME_PREFIX = _get("NETWORK_NAME", name)
    APP_PORT = _int("APP_PORT", 8003)
    APP_HEALTH_PATH = _get("APP_HEALTH_PATH", "/healthz")
    ALB_PROTECTED_PATHS = _list("ALB_PROTECTED_PATHS", "/query*,/embed*,/rerank*")

    DOMAIN = _get("DOMAIN", None)
    HOSTED_ZONE_ID = _get("HOSTED_ZONE_ID", None)

    # Cognito knobs
    COGNITO_DOMAIN_PREFIX = _get("COGNITO_DOMAIN_PREFIX", f"{NAME_PREFIX}-{stack}")
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

    # Create or reuse network
    net = create_network(name=NAME_PREFIX, multi_az=multi_az, no_nat=no_nat, create_vpc_endpoints=create_vpc_endpoints, aws_region=aws_region)
    vpc = net["vpc"]
    public_subnets = net["public_subnets"]
    private_subnets = net["private_subnets"]

    tags_common = {"Name": f"{NAME_PREFIX}-{stack}"}

    # Security groups
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

    # ALB + Target Group
    alb = aws.lb.LoadBalancer(f"{NAME_PREFIX}-alb",
        internal=False,
        load_balancer_type="application",
        security_groups=[alb_sg.id],
        subnets=[s.id for s in public_subnets],
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

    # ACM certificate
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
    else:
        pulumi.log.warn("DOMAIN not set. Skipping ACM automation.")

    https_listener = aws.lb.Listener(f"{NAME_PREFIX}-https-listener",
        load_balancer_arn=alb.arn,
        port=443,
        protocol="HTTPS",
        ssl_policy="ELBSecurityPolicy-TLS13-1-2-2021-06",
        certificate_arn=certificate_arn if certificate_arn else "",
        default_actions=[aws.lb.ListenerDefaultActionArgs(type="fixed-response", fixed_response=aws.lb.ListenerDefaultActionFixedResponseArgs(content_type="text/plain", status_code="404", message_body="Not found"))])

    # Optional PreAuth Lambda
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

    # Cognito user pool and client
    lambda_cfg = None
    if preauth_lambda:
        lambda_cfg = aws.cognito.UserPoolLambdaConfigArgs(pre_authentication=preauth_lambda.arn)

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
        lambda_config=lambda_cfg,
        tags={**tags_common, "Resource": "cognito-userpool"})

    def _build_callback_urls(alb_dns: str) -> List[str]:
        if COGNITO_CALLBACK_URLS:
            return COGNITO_CALLBACK_URLS
        return [f"https://{alb_dns}/oauth2/idpresponse"]

    def _build_logout_urls(alb_dns: str) -> List[str]:
        if COGNITO_LOGOUT_URLS:
            return COGNITO_LOGOUT_URLS
        return [f"https://{alb_dns}/logout"]

    callback_urls_output = pulumi.Output.all(alb.dns_name).apply(lambda args: _build_callback_urls(args[0]))
    logout_urls_output = pulumi.Output.all(alb.dns_name).apply(lambda args: _build_logout_urls(args[0]))

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
        refresh_token_validity=COGNITO_REFRESH_TOKEN_VALIDITY,
        tags={**tags_common, "Resource": "cognito-client"})

    user_pool_domain = aws.cognito.UserPoolDomain(f"{NAME_PREFIX}-domain",
        domain=COGNITO_DOMAIN_PREFIX,
        user_pool_id=user_pool.id,
        tags={**tags_common, "Resource": "cognito-domain"})

    # authenticate-cognito listener rules: protect paths then forward to TG
    auth_args = aws.lb.ListenerRuleActionAuthenticateCognitoArgs(
        user_pool_arn=user_pool.arn,
        user_pool_client_id=user_pool_client.id,
        user_pool_domain=user_pool_domain.domain,
        on_unauthenticated_request="authenticate"
    )

    for i, path in enumerate(ALB_PROTECTED_PATHS):
        aws.lb.ListenerRule(f"{NAME_PREFIX}-rule-{i}",
            listener_arn=https_listener.arn,
            priority=1000 + i,
            conditions=[aws.lb.ListenerRuleConditionArgs(path_pattern=aws.lb.ListenerRuleConditionPathPatternArgs(values=[path]))],
            actions=[
                aws.lb.ListenerRuleActionArgs(type="authenticate-cognito", authenticate_cognito=auth_args),
                aws.lb.ListenerRuleActionArgs(type="forward", target_group_arn=tg.arn)
            ])

    # Return structured dict for __main__.py consumption
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
