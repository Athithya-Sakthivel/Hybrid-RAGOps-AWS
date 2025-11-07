# infra/pulumi-aws/__main__.py
from __future__ import annotations
import os
import pulumi
import pulumi_aws as aws
from typing import Any, Dict

log = pulumi.log.info
STACK = pulumi.get_stack()

# Control switches (export "false" to disable)
ENABLE_PREREQS = os.getenv("ENABLE_PREREQS", "true").lower() != "false"
ENABLE_NETWORKING = os.getenv("ENABLE_NETWORKING", "true").lower() != "false"
ENABLE_IAM = os.getenv("ENABLE_IAM", "true").lower() != "false"
ENABLE_RENDERER = os.getenv("ENABLE_RENDERER", "true").lower() != "false"
ENABLE_HEAD = os.getenv("ENABLE_HEAD", "true").lower() != "false"

# Try to import optional modules
_prereqs = None
_auth = None
_iam = None
_renderer = None
_head = None

try:
    import prerequisites as prereqs
    _prereqs = prereqs
    log("prerequisites module loaded")
except ImportError:
    log("prerequisites module not present; skipping import")

try:
    import auth as auth
    _auth = auth
    log("auth module loaded")
except ImportError:
    log("auth module not present; skipping import")

try:
    import iam as iam
    _iam = iam
    log("iam module loaded")
except ImportError:
    log("iam module not present; skipping import")

try:
    import ray_autoscaler_renderer as renderer
    _renderer = renderer
    log("ray_autoscaler_renderer module loaded")
except ImportError:
    log("ray_autoscaler_renderer module not present; skipping import")

try:
    import ray_head as headmod
    _head = headmod
    log("ray_head module loaded")
except ImportError:
    log("ray_head module not present; skipping import")


# Containers for produced resources/outputs
outputs: Dict[str, Any] = {}
resources: Dict[str, Any] = {}

# --- PREREQS ---
if _prereqs and ENABLE_PREREQS:
    log("Running prerequisites step")
    # Prefer a factory function create_prereqs(); otherwise accept top-level 'key' and 'ssm_param'
    if hasattr(_prereqs, "create_prereqs"):
        try:
            pr = _prereqs.create_prereqs()
            resources["prereqs"] = pr
            outputs["redis_parameter_name"] = pr.get("ssm_param").name if pr.get("ssm_param") else None
            outputs["redis_kms_key_arn"] = pr.get("key").arn if pr.get("key") else None
        except Exception as e:
            log(f"prerequisites.create_prereqs() failed: {e}")
    else:
        # look for top-level exports
        key = getattr(_prereqs, "key", None)
        ssm_param = getattr(_prereqs, "ssm_param", None)
        if key or ssm_param:
            resources["prereqs"] = {"key": key, "ssm_param": ssm_param}
            if ssm_param:
                outputs["redis_parameter_name"] = getattr(ssm_param, "name", None)
            if key:
                outputs["redis_kms_key_arn"] = getattr(key, "arn", None)
        else:
            log("prerequisites module present but exposes neither create_prereqs() nor key/ssm_param; skipping prereqs creation")
else:
    log("Skipping prerequisites step (module missing or disabled)")

# --- NETWORKING / AUTH (now via auth.create_auth) ---
if _auth and ENABLE_NETWORKING:
    log("Running auth (networking + auth) step")
    try:
        if hasattr(_auth, "create_auth"):
            # read env-driven defaults; caller can still pass args if desired
            multi_az = os.getenv("MULTI_AZ_DEPLOYMENT", "false").lower() == "true"
            no_nat = os.getenv("NO_NAT", "false").lower() == "true"
            create_vpc_endpoints = os.getenv("CREATE_VPC_ENDPOINTS", "false").lower() == "true"
            auth_res = _auth.create_auth(name=os.getenv("NETWORK_NAME", "ray"),
                                         multi_az=multi_az,
                                         no_nat=no_nat,
                                         create_vpc_endpoints=create_vpc_endpoints)
            resources["network"] = auth_res.get("network") if auth_res.get("network") else auth_res
            # Export network outputs (maintain compatibility)
            net = auth_res.get("network") or {}
            vpc = net.get("vpc") if isinstance(net, dict) else getattr(net, "vpc", None)
            public_subnets = net.get("public_subnets") if isinstance(net, dict) else getattr(net, "public_subnets", None)
            private_subnets = net.get("private_subnets") if isinstance(net, dict) else getattr(net, "private_subnets", None)
            outputs["vpc_id"] = getattr(vpc, "id", None)
            outputs["public_subnet_ids"] = [s.id for s in (public_subnets or [])] if public_subnets else None
            outputs["private_subnet_ids"] = [s.id for s in (private_subnets or [])] if private_subnets else None

            # pass through auth-related outputs if present
            if auth_res.get("alb"):
                outputs["alb_dns"] = auth_res["alb"].dns_name
            if auth_res.get("tg"):
                outputs["target_group_arn"] = auth_res["tg"].arn
            if auth_res.get("user_pool"):
                outputs["cognito_user_pool_id"] = auth_res["user_pool"].id
            if auth_res.get("user_pool_client"):
                outputs["cognito_user_pool_client_id"] = auth_res["user_pool_client"].id
            if auth_res.get("user_pool_domain"):
                outputs["cognito_user_pool_domain"] = auth_res["user_pool_domain"].domain
            if auth_res.get("certificate_arn"):
                outputs["certificate_arn"] = auth_res["certificate_arn"]
        else:
            log("auth module present but exposes no create_auth(); skipping")
    except Exception as e:
        log(f"auth.create_auth() failed: {e}")
else:
    log("Skipping auth/networking step (module missing or disabled)")

# --- IAM ---
if _iam and ENABLE_IAM:
    log("Running iam step")
    try:
        # create head role if function exists
        iam_results = {}
        if hasattr(_iam, "create_ray_head_role"):
            iam_results["head"] = _iam.create_ray_head_role(prefix=os.getenv("IAM_PREFIX", "rag"))
        if hasattr(_iam, "create_ec2_role"):
            iam_results["ec2"] = _iam.create_ec2_role(prefix=os.getenv("IAM_PREFIX", "rag"))
        # cpu worker S3 role requires bucket name
        try:
            bucket_name = os.getenv("AUTOSCALER_BUCKET_NAME") or os.getenv("PULUMI_S3_BUCKET")
            if hasattr(_iam, "create_ray_cpu_worker_s3_role"):
                iam_results["cpu_s3"] = _iam.create_ray_cpu_worker_s3_role(prefix=os.getenv("IAM_PREFIX", "rag"), bucket=bucket_name)
        except Exception as e:
            log(f"create_ray_cpu_worker_s3_role skipped/failed: {e}")
        resources["iam"] = iam_results
        # export instance profile names if available
        if iam_results.get("head"):
            outputs["ray_head_instance_profile"] = iam_results["head"]["instance_profile"].name
        if iam_results.get("cpu_s3"):
            outputs["ray_cpu_instance_profile"] = iam_results["cpu_s3"]["instance_profile"].name
    except Exception as e:
        log(f"IAM creation failed: {e}")
else:
    log("Skipping iam step (module missing or disabled)")

# --- AUTOSCALER RENDER + S3 UPLOAD ---
if _renderer and ENABLE_RENDERER:
    log("Running autoscaler renderer step")
    try:
        # Need cpu/profile names from IAM; pass what we have (may be Pulumi Outputs)
        head_profile = outputs.get("ray_head_instance_profile") or (resources.get("iam") or {}).get("head", {}).get("instance_profile", {}).get("name")
        cpu_profile = outputs.get("ray_cpu_instance_profile") or (resources.get("iam") or {}).get("cpu_s3", {}).get("instance_profile", {}).get("name")
        # If none available, renderer may still run using env-only inputs
        render_res = _renderer.upload_autoscaler_yaml_to_s3(
            bucket_name=os.getenv("AUTOSCALER_BUCKET_NAME") or None,
            stack=STACK,
            head_profile_name=head_profile or "",
            cpu_profile_name=cpu_profile or "",
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
        log(f"renderer step failed: {e}")
else:
    log("Skipping renderer step (module missing or disabled)")

# --- RAY HEAD / ASG ---
if _head and ENABLE_HEAD:
    log("Running ray head step")
    try:
        # gather inputs; many are optional; if missing the create function should fail gracefully
        vpc_id = outputs.get("vpc_id") or (resources.get("network") or {}).get("vpc", {}).get("id")
        private_subnet_ids = outputs.get("private_subnet_ids") or (resources.get("network") or {}).get("private_subnets")
        # normalize private_subnet_ids to list of ids if resource objects provided
        if private_subnet_ids and isinstance(private_subnet_ids, list) and hasattr(private_subnet_ids[0], "id"):
            private_subnet_ids = [s.id for s in private_subnet_ids]
        autoscaler_s3 = outputs.get("autoscaler_s3") or os.getenv("AUTOSCALER_S3_PATH")
        ssm_param_name = os.getenv("REDIS_SSM_PARAM", "/ray/prod/redis_password")
        hosted_zone_id = os.getenv("PRIVATE_HOSTED_ZONE_ID")
        hosted_zone_name = os.getenv("HOSTED_ZONE_NAME", "prod.internal.example.com")
        head_res = _head.create_ray_head(
            stack=STACK,
            vpc_id=vpc_id,
            private_subnet_ids=private_subnet_ids or [],
            head_ami=os.getenv("HEAD_AMI", ""),
            head_instance_type=os.getenv("HEAD_INSTANCE_TYPE", "m5.large"),
            instance_profile_name=outputs.get("ray_head_instance_profile") or "",
            autoscaler_s3=autoscaler_s3 or "",
            ssm_param_name=ssm_param_name,
            hosted_zone_id=hosted_zone_id,
            hosted_zone_name=hosted_zone_name,
            key_name=os.getenv("KEY_NAME", None)
        )
        resources["head"] = head_res
        # export some head outputs if present
        if head_res.get("asg"):
            outputs["head_asg_name"] = head_res["asg"].name
        if head_res.get("record"):
            outputs["head_dns"] = head_res["record"].fqdn
        if head_res.get("nlb"):
            outputs["nlb_dns"] = head_res["nlb"].dns_name
    except Exception as e:
        log(f"ray head step failed: {e}")
else:
    log("Skipping ray head step (module missing or disabled)")

# --- Export available outputs (only those that exist) ---
for k, v in outputs.items():
    if v is None:
        continue
    try:
        pulumi.export(k, v)
    except Exception:
        # some values may be complex dicts of resources; attempt to export common attributes
        try:
            if isinstance(v, list):
                pulumi.export(k, v)
        except Exception:
            log(f"Could not export {k}")

log("__main__.py completed. Modules executed: " +
    f"prereqs={bool(_prereqs and ENABLE_PREREQS)}, " +
    f"auth={bool(_auth and ENABLE_NETWORKING)}, " +
    f"iam={bool(_iam and ENABLE_IAM)}, " +
    f"renderer={bool(_renderer and ENABLE_RENDERER)}, " +
    f"head={bool(_head and ENABLE_HEAD)}")
