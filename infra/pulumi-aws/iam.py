# infra/pulumi-aws/iam.py
from __future__ import annotations
import json
import pulumi
import pulumi_aws as aws
from typing import Optional

def _ec2_assume_policy():
    return aws.iam.get_policy_document(statements=[{
        "Effect": "Allow",
        "Principals": [{"Type": "Service", "Identifiers": ["ec2.amazonaws.com"]}],
        "Actions": ["sts:AssumeRole"],
    }])

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
    bucket = bucket or pulumi.Config().get("s3Bucket") or None
    if not bucket:
        raise Exception("create_ray_cpu_worker_s3_role: S3 bucket required (pulumi config s3Bucket or env S3_BUCKET)")
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
    policy = aws.iam.Policy(f"{name_prefix}-elbv2-register-policy", policy=pulumi.Output.from_input(target_group_arn).apply(make_doc))
    aws.iam.PolicyAttachment(f"{name_prefix}-elbv2-register-attach", policy_arn=policy.arn, roles=[role.name])
    return policy

def create_and_attach_ssm_kms_read(role: aws.iam.Role, ssm_arn: pulumi.Input[str], kms_arn: pulumi.Input[str], name: str = "ray-ssm-kms-policy"):
    def make_doc(args):
        ssm_a, kms_a = args
        doc = {
            "Version": "2012-10-17",
            "Statement": [
                {"Effect": "Allow", "Action": ["ssm:GetParameter", "ssm:GetParametersByPath", "ssm:GetParameters"], "Resource": ssm_a},
                {"Effect": "Allow", "Action": ["kms:Decrypt", "kms:GenerateDataKey"], "Resource": kms_a}
            ]
        }
        return json.dumps(doc)
    policy = pulumi.Output.all(ssm_arn, kms_arn).apply(lambda args: aws.iam.Policy(name, policy=make_doc(args)))
    # attach when created
    def attach(p):
        aws.iam.PolicyAttachment(f"{name}-attach-{role.name}", policy_arn=p.arn, roles=[role.name])
        return p
    return policy.apply(attach)
