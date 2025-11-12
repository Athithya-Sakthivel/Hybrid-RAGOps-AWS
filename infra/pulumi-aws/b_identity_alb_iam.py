# b_identity_alb_iam.py
from __future__ import annotations
import os
import json
import pulumi
import pulumi_aws as aws
from pulumi import ResourceOptions, CustomTimeouts

STACK = pulumi.get_stack()
cfg = pulumi.Config()

# Try to reuse outputs from a_prereqs_networking when running in same program
try:
    import a_prereqs_networking as netmod  # type: ignore
    pulumi.log.info("Imported a_prereqs_networking outputs for wiring")
    VPC_ID = getattr(netmod, "vpc_id_out", None)
    PUBLIC_SUBNET_IDS = getattr(netmod, "public_subnet_ids_out", None) or []
    ALB_SG_ID = getattr(netmod, "alb_security_group_id_out", None)
except Exception:
    VPC_ID = os.getenv("VPC_ID") or cfg.get("vpcId")
    PUBLIC_SUBNET_IDS = (os.getenv("PUBLIC_SUBNET_IDS") or cfg.get("publicSubnetIds") or "")
    PUBLIC_SUBNET_IDS = [s.strip() for s in PUBLIC_SUBNET_IDS.split(",") if s.strip()]
    ALB_SG_ID = os.getenv("ALB_SECURITY_GROUP_ID") or cfg.get("albSecurityGroupId")

APP_PORT = int(os.getenv("APP_PORT") or cfg.get_int("appPort") or 8003)
APP_HEALTH_PATH = os.getenv("APP_HEALTH_PATH") or cfg.get("appHealthPath") or "/healthz"
DOMAIN = os.getenv("DOMAIN") or cfg.get("domain") or ""
HOSTED_ZONE_ID = os.getenv("HOSTED_ZONE_ID") or cfg.get("hostedZoneId") or ""
ALB_IDLE_TIMEOUT = int(os.getenv("ALB_IDLE_TIMEOUT") or cfg.get_int("albIdleTimeout") or 300)
ENABLE_COGNITO = (os.getenv("ENABLE_COGNITO") or cfg.get("enableCognito") or "false").lower() in ("1","true","yes")
ENABLE_WAF = (os.getenv("ENABLE_WAF") or cfg.get("enableWaf") or "false").lower() in ("1","true","yes")

alb = None
tg = None
https_listener = None
http_listener = None
certificate_arn = None
cert_validation = None

# create ALB + target group if enough inputs available
if PUBLIC_SUBNET_IDS and ALB_SG_ID:
    alb = aws.lb.LoadBalancer(
        "alb",
        internal=False,
        load_balancer_type="application",
        subnets=PUBLIC_SUBNET_IDS,
        security_groups=[ALB_SG_ID],
        idle_timeout=ALB_IDLE_TIMEOUT,
        tags={"Name": f"ray-alb-{STACK}"},
    )

    # ensure VPC_ID exists for target group
    if not VPC_ID:
        pulumi.log.warn("VPC_ID not available. Target group will be created without vpc_id; this may fail. Provide VPC_ID or run a_prereqs_networking in same run.")
    tg = aws.lb.TargetGroup(
        "alb-tg",
        port=APP_PORT,
        protocol="HTTP",
        target_type="instance",
        vpc_id=VPC_ID,
        health_check=aws.lb.TargetGroupHealthCheckArgs(
            path=APP_HEALTH_PATH,
            protocol="HTTP",
            port=str(APP_PORT),
            interval=15,
            timeout=5,
            healthy_threshold=2,
            unhealthy_threshold=2,
        ),
        deregistration_delay=300,
        tags={"Name": f"ray-tg-{STACK}"},
    )

    # HTTP listener redirects to HTTPS
    http_listener = aws.lb.Listener(
        "alb-http-listener",
        load_balancer_arn=alb.arn,
        port=80,
        protocol="HTTP",
        default_actions=[aws.lb.ListenerDefaultActionArgs(
            type="redirect",
            redirect=aws.lb.ListenerDefaultActionRedirectArgs(
                port="443",
                protocol="HTTPS",
                status_code="HTTP_301",
            ),
        )],
    )
else:
    pulumi.log.info("Skipping ALB creation: PUBLIC_SUBNET_IDS or ALB_SG_ID not provided")

# ACM certificate with DNS validation (auto create Route53 records when HOSTED_ZONE_ID provided)
if DOMAIN:
    # increase create timeout because DNS propagation may be slow
    cert_opts = ResourceOptions(custom_timeouts=CustomTimeouts(create="30m"))
    cert = aws.acm.Certificate(
        "albCert",
        domain_name=DOMAIN,
        validation_method="DNS",
        tags={"Name": f"alb-cert-{STACK}"},
        opts=cert_opts,
    )

    # If hosted zone provided, create Route53 records based on domain_validation_options
    if HOSTED_ZONE_ID:
        def make_validation_records(dvos):
            recs = []
            for i, dvo in enumerate(dvos):
                r = aws.route53.Record(
                    f"albCertValidation-{i}",
                    zone_id=HOSTED_ZONE_ID,
                    name=dvo["resource_record_name"],
                    type=dvo["resource_record_type"],
                    records=[dvo["resource_record_value"]],
                    ttl=300,
                )
                recs.append(r.fqdn)
            return recs

        cert_validation_fqdns = cert.domain_validation_options.apply(lambda d: make_validation_records(d))
        cert_validation = aws.acm.CertificateValidation(
            "albCertValidation",
            certificate_arn=cert.arn,
            validation_record_fqdns=cert_validation_fqdns,
            opts=ResourceOptions(depends_on=[cert]),
        )
        certificate_arn = cert_validation.certificate_arn
    else:
        # export domain_validation_options so operator can create records manually
        pulumi.export("cert_domain_validation_options", cert.domain_validation_options)
        certificate_arn = cert.arn
else:
    pulumi.log.info("DOMAIN not provided; skipping ACM certificate")

# Create HTTPS listener after TG and certificate are available
if alb:
    # choose default action list
    default_actions = []
    # If Cognito enabled, create User Pool + Client, and add authenticate-cognito action before forward
    user_pool = None
    user_pool_client = None
    user_pool_domain = None
    if ENABLE_COGNITO:
        user_pool = aws.cognito.UserPool(
            "ray-userpool",
            auto_verified_attributes=["email"],
            mfa_configuration="OFF",
            password_policy=aws.cognito.UserPoolPasswordPolicyArgs(minimum_length=8, require_lowercase=True, require_numbers=True),
            tags={"Name": f"ray-userpool-{STACK}"},
        )
        cb_urls = [alb.dns_name.apply(lambda d: f"https://{d}/oauth2/idpresponse")]
        lo_urls = [alb.dns_name.apply(lambda d: f"https://{d}/logout")]
        user_pool_client = aws.cognito.UserPoolClient(
            "ray-userpool-client",
            user_pool_id=user_pool.id,
            generate_secret=False,
            allowed_oauth_flows=["code"],
            allowed_oauth_scopes=["openid", "email", "profile"],
            callback_urls=cb_urls,
            logout_urls=lo_urls,
            supported_identity_providers=["COGNITO"],
            access_token_validity=3600,
            id_token_validity=3600,
            refresh_token_validity=30 * 24 * 60,
        )
        user_pool_domain = aws.cognito.UserPoolDomain("ray-userpool-domain", domain=f"ray-{STACK}", user_pool_id=user_pool.id)

    # Build authenticate-cognito action if configured and user pool/client/domain exist
    if ENABLE_COGNITO and user_pool and user_pool_client and user_pool_domain:
        auth_action = aws.lb.ListenerDefaultActionArgs(
            type="authenticate-cognito",
            authenticate_cognito=aws.lb.ListenerDefaultActionAuthenticateCognitoArgs(
                user_pool_arn=user_pool.arn,
                user_pool_client_id=user_pool_client.id,
                user_pool_domain=user_pool_domain.domain,
                on_unauthenticated_request="authenticate"
            ),
        )
        default_actions.append(auth_action)

    # forward action to TG if exists, else fixed-response
    if tg:
        forward_action = aws.lb.ListenerDefaultActionArgs(type="forward", target_group_arn=tg.arn)
        default_actions.append(forward_action)
    else:
        fixed = aws.lb.ListenerDefaultActionArgs(type="fixed-response", fixed_response=aws.lb.ListenerDefaultActionFixedResponseArgs(content_type="text/plain", status_code="404", message_body="Not found"))
        default_actions.append(fixed)

    # if certificate present, use HTTPS listener; otherwise warn
    if certificate_arn:
        https_listener = aws.lb.Listener(
            "alb-https-listener",
            load_balancer_arn=alb.arn,
            port=443,
            protocol="HTTPS",
            ssl_policy="ELBSecurityPolicy-TLS13-1-2-2021-06",
            certificate_arn=certificate_arn,
            default_actions=default_actions,
        )
    else:
        pulumi.log.warn("Certificate not available; creating HTTPS listener skipped (or will fail). Exported cert_domain_validation_options if applicable.")

# Optional: WAF scaffold (associate if requested)
if ENABLE_WAF and alb:
    try:
        web_acl = aws.wafv2.WebAcl(
            "ray-waf",
            scope="REGIONAL",
            default_action=aws.wafv2.WebAclDefaultActionArgs(allow={}),
            visibility_config=aws.wafv2.WebAclVisibilityConfigArgs(cloudwatch_metrics_enabled=True, metric_name=f"ray-waf-{STACK}", sampled_requests_enabled=True),
            rules=[],
            tags={"Name": f"ray-waf-{STACK}"},
        )
        aws.wafv2.WebAclAssociation("ray-waf-assoc", resource_arn=alb.arn, web_acl_arn=web_acl.arn)
    except Exception as ex:
        pulumi.log.warn(f"WAF association failed or not supported: {ex}")

# Exports
if alb:
    pulumi.export("alb_dns", alb.dns_name)
    pulumi.export("alb_arn", alb.arn)
if tg:
    pulumi.export("target_group_arn", tg.arn)
if certificate_arn:
    pulumi.export("certificate_arn", certificate_arn)
if ENABLE_COGNITO and user_pool:
    pulumi.export("cognito_user_pool_id", user_pool.id)
    pulumi.export("cognito_user_pool_client_id", user_pool_client.id)
    pulumi.export("cognito_domain", user_pool_domain.domain)

pulumi.log.info("b_identity_alb_iam.py completed")
