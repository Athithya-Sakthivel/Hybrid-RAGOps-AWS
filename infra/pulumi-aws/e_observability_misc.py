# e_observability_misc.py
from __future__ import annotations
import os
import pulumi
import pulumi_aws as aws

STACK = pulumi.get_stack()
cfg = pulumi.Config()

# try to reuse earlier outputs
try:
    import a_prereqs_networking as netmod
    pulumi.log.info("Using a_prereqs_networking outputs in observability module")
    vpc_id = getattr(netmod, "vpc_id_out", None)
    private_subnet_ids = getattr(netmod, "private_subnet_ids_out", []) or []
except Exception:
    vpc_id = os.getenv("VPC_ID") or cfg.get("vpcId")
    private_subnet_ids = (os.getenv("PRIVATE_SUBNET_IDS") or cfg.get("privateSubnetIds") or "").split(",")

# ===== ElastiCache Redis (Valkey) =====
ENABLE_ELASTICACHE = (os.getenv("ENABLE_ELASTICACHE") or cfg.get("enableElastiCache") or "true").lower() in ("1","true","yes")
valkey = None
valkey_subnet_group = None
if ENABLE_ELASTICACHE:
    if not private_subnet_ids:
        pulumi.log.warn("No private_subnet_ids available; skipping ElastiCache creation")
    else:
        valkey_subnet_group = aws.elasticache.SubnetGroup("valkey-subnet-group", subnet_ids=private_subnet_ids, description=f"valkey-subnet-group-{STACK}")
        # create a single-node Redis replication group with AUTH token pulled from SSM if available
        redis_auth_param = os.getenv("VALKEY_AUTH_SSM_PARAM") or cfg.get("valkeyAuthSsmParam") or ""
        auth_token = None
        if redis_auth_param:
            try:
                s = aws.ssm.get_parameter(name=redis_auth_param, with_decryption=True)
                auth_token = s.value
            except Exception:
                pulumi.log.warn("VALKEY_AUTH_SSM_PARAM configured but unreadable; create SSM param or leave blank")
        valkey = aws.elasticache.ReplicationGroup("valkey-redis",
                                                 replication_group_description=f"valkey-{STACK}",
                                                 node_type="cache.t3.micro",
                                                 number_cache_clusters=1,
                                                 automatic_failover_enabled=False,
                                                 subnet_group_name=valkey_subnet_group.name,
                                                 auth_token=auth_token if auth_token else None,
                                                 security_group_ids=[],
                                                 tags={"Name": f"valkey-{STACK}"})
        pulumi.export("valkey_endpoint", valkey.primary_endpoint_address)

# ===== CloudWatch log groups and basic alarms =====
log_prefix = f"/ray/{STACK}"
log_groups = {}
for name in ("head", "workers", "gateway", "llm_server", "lambda"):
    lg = aws.cloudwatch.LogGroup(f"lg-{name}", name=f"{log_prefix}/{name}", retention_in_days=30)
    log_groups[name] = lg.name
pulumi.export("log_groups", log_groups)

# SNS topic for alerts
alerts_topic = aws.sns.Topic("ray-alerts-topic")
pulumi.export("alerts_topic_arn", alerts_topic.arn)

# Example alarm: Ray head not reporting metrics (placeholder metric name)
head_cpu_alarm = aws.cloudwatch.MetricAlarm("head-heartbeat-alarm",
                                            alarm_name=f"ray-head-not-ready-{STACK}",
                                            comparison_operator="LessThanThreshold",
                                            evaluation_periods=1,
                                            metric_name="CPUUtilization",
                                            namespace="AWS/EC2",
                                            period=300,
                                            statistic="Average",
                                            threshold=1.0,
                                            alarm_actions=[alerts_topic.arn])

# ECR repositories for container images
ecr_gate = aws.ecr.Repository("ecr-gateway", name=f"gateway-{STACK}")
ecr_llm = aws.ecr.Repository("ecr-llm", name=f"llm-server-{STACK}")
pulumi.export("ecr_gateway_repo", ecr_gate.repository_url)
pulumi.export("ecr_llm_repo", ecr_llm.repository_url)

# Basic WAF WebACL already supported in b_identity; optional here to export placeholder
pulumi.log.info("e_observability_misc.py completed")
