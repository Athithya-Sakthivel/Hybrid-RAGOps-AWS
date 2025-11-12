# __main__.py
from __future__ import annotations
import os
import pulumi

# toggles (staging defaults)
ENABLE_FILE_A = os.getenv("ENABLE_A", "true").lower() in ("1","true","yes")
ENABLE_FILE_B = os.getenv("ENABLE_B", "true").lower() in ("1","true","yes")
ENABLE_FILE_C = os.getenv("ENABLE_C", "true").lower() in ("1","true","yes")
ENABLE_FILE_D = os.getenv("ENABLE_D", "true").lower() in ("1","true","yes")
ENABLE_FILE_E = os.getenv("ENABLE_E", "true").lower() in ("1","true","yes")

pulumi.log.info(f"ENABLE_FILE_A={ENABLE_FILE_A} ENABLE_FILE_B={ENABLE_FILE_B} ENABLE_FILE_C={ENABLE_FILE_C} ENABLE_FILE_D={ENABLE_FILE_D} ENABLE_FILE_E={ENABLE_FILE_E}")

if ENABLE_FILE_A:
    import a_prereqs_networking
if ENABLE_FILE_B:
    import b_identity_alb_iam
if ENABLE_FILE_C:
    import c_ray_head
if ENABLE_FILE_D:
    import d_ray_workers
if ENABLE_FILE_E:
    import e_observability_misc

pulumi.log.info("__main__.py completed. Modules loaded based on ENABLE flags.")
