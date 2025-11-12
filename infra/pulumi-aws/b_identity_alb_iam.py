# b_identity_alb_iam.py
from __future__ import annotations
import os
import json
import pulumi
import pulumi_aws as aws

STACK = pulumi.get_stack()
cfg = pulumi.Config()

# Try to reuse outputs from a_prereqs_networking when running in same program
try:
    import a_prereqs_networking as netmod
    pulumi.log.info("Imported a_prereqs_networking outputs")
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

if PUBLIC_SUBNET_IDS and ALB_SG_ID:
    alb = aws.lb.LoadBalancer("alb", internal=False, load_balancer_type="application", subnets=PUBLIC_SUBNET_IDS, security_groups=[ALB_SG_ID], idle_timeout=ALB_IDLE_TIMEOUT, tags={"Name": f"ray-alb-{STACK}"})
    tg = aws.lb.TargetGroup("alb-tg", port=APP_PORT, protocol="HTTP", target_type="instance", vpc_id=VPC_ID, health_check=aws.lb.TargetGroupHealthCheckArgs(path=APP_HEALTH_PATH, protocol="HTTP", port=str(APP_PORT), interval=15, timeout=5, healthy_threshold=2, unhealthy_threshold=2), deregistration_delay=300, tags={"Name": f"ray-tg-{STACK}"})
    http_listener = aws.lb.Listener("alb-http-listener", load_balancer_arn=alb.arn, port=80, protocol="HTTP", default_actions=[aws.lb.ListenerDefaultActionArgs(type="redirect", redirect=aws.lb.ListenerDefaultActionRedirectArgs(port="443", protocol="HTTPS", status_code="HTTP_301"))])
else:
    pulumi.log.info("Skipping ALB creation: PUBLIC_SUBNET_IDS or ALB_SG_ID not provided")

# ACM certificate DNS validation if domain+hosted zone provided
certificate_arn = None
if DOMAIN:
    cert = aws.acm.Certificate("albCert", domain_name=DOMAIN, validation_method="DNS", tags={"Name": f"alb-cert-{STACK}"})
    if HOSTED_ZONE_ID:
        def mk_records(dvos):
            recs = []
            for i, dvo in enumerate(dvos):
                rec = aws.route53.Record(f"albCertValidation-{i}", zone_id=HOSTED_ZONE_ID, name=dvo["resource_record_name"], type=dvo["resource_record_type"], records=[dvo["resource_record_value"]], ttl=300)
                recs.append(rec.fqdn)
            return recs
        cert_validation_fqdns = cert.domain_validation_options.apply(lambda dvos: mk_records(dvos))
        cert_validation = aws.acm.CertificateValidation("albCertValidation", certificate_arn=cert.arn, validation_record_fqdns=cert_validation_fqdns)
        certificate_arn = cert_validation.certificate_arn
    else:
        pulumi.export("cert_domain_validation_options", cert.domain_validation_options)
        certificate_arn = cert.arn
else:
    pulumi.log.info("DOMAIN not provided; skipping ACM certificate")

# HTTPS listener (if cert available) — default action will be configured to forward later when TG exists
if alb and certificate_arn:
    https_listener = aws.lb.Listener("alb-https-listener", load_balancer_arn=alb.arn, port=443, protocol="HTTPS", ssl_policy="ELBSecurityPolicy-TLS13-1-2-2021-06", certificate_arn=certificate_arn, default_actions=[aws.lb.ListenerDefaultActionArgs(type="fixed-response", fixed_response=aws.lb.ListenerDefaultActionFixedResponseArgs(content_type="text/plain", status_code="404", message_body="Not found"))])
elif alb:
    pulumi.log.warn("Certificate not available; HTTPS listener not created")

# optionally create Cognito user pool/client and associate with ALB if ENABLE_COGNITO
user_pool = None
user_pool_client = None
user_pool_domain = None
if ENABLE_COGNITO and alb:
    user_pool = aws.cognito.UserPool("ray-userpool", auto_verified_attributes=["email"], mfa_configuration="OFF", password_policy=aws.cognito.UserPoolPasswordPolicyArgs(minimum_length=8, require_lowercase=True, require_numbers=True), tags={"Name": f"ray-userpool-{STACK}"})
    cb_urls = [f"https://{alb.dns_name}/oauth2/idpresponse"]
    lo_urls = [f"https://{alb.dns_name}/logout"]
    user_pool_client = aws.cognito.UserPoolClient("ray-userpool-client", user_pool_id=user_pool.id, generate_secret=False, allowed_oauth_flows=["code"], allowed_oauth_scopes=["openid","email","profile"], callback_urls=cb_urls, logout_urls=lo_urls, supported_identity_providers=["COGNITO"], access_token_validity=3600, id_token_validity=3600, refresh_token_validity=30*24*60)
    user_pool_domain = aws.cognito.UserPoolDomain("ray-userpool-domain", domain=f"ray-{STACK}", user_pool_id=user_pool.id)

# Attach a minimal policy helper for instances to register with ALB (if needed)
def attach_elbv2_register_policy(role_name: str, target_group_arn: str, name_prefix: str = "ray"):
    doc = {"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["elasticloadbalancing:RegisterTargets","elasticloadbalancing:DeregisterTargets","elasticloadbalancing:DescribeTargetHealth"],"Resource":target_group_arn}]}
    policy = aws.iam.Policy(f"{name_prefix}-elbv2-register-policy", policy=json.dumps(doc))
    aws.iam.PolicyAttachment(f"{name_prefix}-elbv2-register-attach", policy_arn=policy.arn, roles=[role_name])
    return policy.arn

# WAF WebACL scaffold (optional)
if ENABLE_WAF and alb:
    web_acl = aws.wafv2.WebAcl("ray-waf", scope="REGIONAL", default_action=aws.wafv2.WebAclDefaultActionArgs(allow={}), visibility_config=aws.wafv2.WebAclVisibilityConfigArgs(cloudwatch_metrics_enabled=True, metric_name=f"ray-waf-{STACK}", sampled_requests_enabled=True), rules=[], tags={"Name": f"ray-waf-{STACK}"})
    try:
        aws.wafv2.WebAclAssociation("ray-waf-assoc", resource_arn=alb.arn, web_acl_arn=web_acl.arn)
    except Exception:
        pulumi.log.warn("WAF association failed; ensure ALB ARN is regional and WAF has correct scope.")

# Exports
if alb:
    pulumi.export("alb_dns", alb.dns_name)
    pulumi.export("alb_arn", alb.arn)
if tg:
    pulumi.export("target_group_arn", tg.arn)
if certificate_arn:
    pulumi.export("certificate_arn", certificate_arn)
if user_pool:
    pulumi.export("cognito_user_pool_id", user_pool.id)

pulumi.log.info("b_identity_alb_iam.py completed")
