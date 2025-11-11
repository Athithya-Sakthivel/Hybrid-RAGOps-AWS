# __main__.py
from __future__ import annotations
import os
import pulumi

# toggles to enable each file/module (staging defaults)
ENABLE_FILE_A = os.getenv("ENABLE_FILE_A", "true").lower() in ("1","true","yes")
ENABLE_FILE_B = os.getenv("ENABLE_FILE_B", "true").lower() in ("1","true","yes")
ENABLE_FILE_C = os.getenv("ENABLE_FILE_C", "true").lower() in ("1","true","yes")
ENABLE_FILE_D = os.getenv("ENABLE_FILE_D", "true").lower() in ("1","true","yes")

pulumi.log.info(f"ENABLE_FILE_A={ENABLE_FILE_A} ENABLE_FILE_B={ENABLE_FILE_B} ENABLE_FILE_C={ENABLE_FILE_C} ENABLE_FILE_D={ENABLE_FILE_D}")

if ENABLE_FILE_A:
    import a_prereqs_networking  # creates VPC, KMS, SSM, SGs
if ENABLE_FILE_B:
    import b_identity_alb_iam  # creates ALB, TG, optional Cognito
if ENABLE_FILE_C:
    import c_ray_head  # creates ENI, head instance, internal NLB, autoscaler YAML embedded
if ENABLE_FILE_D:
    import d_ray_workers  # creates worker LT, ASG, lifecycle hook, SNS, Lambda

pulumi.log.info("__main__.py completed. Modules loaded based on ENABLE flags.")
