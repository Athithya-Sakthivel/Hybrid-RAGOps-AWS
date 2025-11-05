# infra/pulumi-aws/prerequisites.py
from __future__ import annotations
import os
import pulumi
import pulumi_aws as aws

# Read from environment (operator must export these)
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD")  # REQUIRED. Prefer CI secret injection.
REDIS_SSM_PARAM = os.getenv("REDIS_SSM_PARAM", "/ray/prod/redis_password")
KMS_ALIAS = os.getenv("KMS_ALIAS", "alias/ray-ssm-key")
STACK = pulumi.get_stack()

if not REDIS_PASSWORD:
    raise Exception("Environment variable REDIS_PASSWORD is required. Use secure CI/vault to inject it.")

# Create a customer-managed KMS key and alias
key = aws.kms.Key("raySsmKey",
    description=f"CMK for Ray SSM SecureString ({STACK})",
    deletion_window_in_days=30,
    tags={"Name": f"ray-ssm-key-{STACK}"}
)

aws.kms.Alias("raySsmAlias",
    name=KMS_ALIAS,
    target_key_id=key.key_id
)

# Create SSM SecureString parameter encrypted with the CMK
ssm_param = aws.ssm.Parameter("rayRedisParameter",
    name=REDIS_SSM_PARAM,
    type="SecureString",
    value=REDIS_PASSWORD,
    key_id=key.arn,
    tags={"Name": f"ray-redis-password-{STACK}"}
)

pulumi.export("redis_parameter_name", ssm_param.name)
pulumi.export("redis_kms_key_arn", key.arn)
pulumi.export("redis_kms_key_id", key.key_id)
