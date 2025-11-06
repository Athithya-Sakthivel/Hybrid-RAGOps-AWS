# infra/pulumi-aws/prerequisites.py
from __future__ import annotations
import os
import pulumi
import pulumi_aws as aws

STACK = pulumi.get_stack()
# env-driven values
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD")            # optional if you used pulumi config secret
REDIS_SSM_PARAM = os.getenv("REDIS_SSM_PARAM", "/ray/prod/redis_password")
KMS_ALIAS = os.getenv("KMS_ALIAS", "alias/ray-ssm-key")

# If secret wasn't passed via env, try Pulumi config secret 'redisPassword'
if not REDIS_PASSWORD:
    cfg = pulumi.Config()
    try:
        REDIS_PASSWORD = cfg.require_secret("redisPassword")  # Pulumi secret (keeps encrypted)
    except Exception:
        REDIS_PASSWORD = None

if REDIS_PASSWORD is None:
    raise Exception("REDIS_PASSWORD not provided. Export env REDIS_PASSWORD or set pulumi config secret redisPassword.")

# Try to find an existing alias; if found reuse the key, otherwise create key+alias.
key_resource = None
alias_resource = None

try:
    # lookup existing alias (data-source). If exists, get target_key_id
    existing = aws.kms.get_alias(name=KMS_ALIAS)
    # reuse existing key by importing it as a Pulumi resource handle
    key_resource = aws.kms.Key.get("raySsmKeyExisting", existing.target_key_id)
    pulumi.log.info(f"Found existing KMS alias {KMS_ALIAS}, reusing key {existing.target_key_id}")
except Exception:
    # alias not found -> create new key + alias
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

# Create or overwrite SSM parameter. Use overwrite=True to allow updating existing param.
# If REDIS_PASSWORD is a Pulumi secret (Output/Secret), wrap appropriately.
param_args = dict(
    name=REDIS_SSM_PARAM,
    type="SecureString",
    value=REDIS_PASSWORD,
    key_id=key_resource.arn,
    tags={"Name": f"ray-redis-password-{STACK}"},
    overwrite=True,    # <- crucial: allow update if parameter already exists
)

ssm_param = aws.ssm.Parameter("rayRedisParameter", **param_args)

pulumi.export("redis_parameter_name", ssm_param.name)
pulumi.export("redis_kms_key_arn", key_resource.arn)
pulumi.export("redis_kms_key_id", key_resource.key_id)
