# RAG Platform — README

## What this repository does

Provision a production-ready, deterministic Ray-based RAG platform on AWS that is resilient to head-node failures.
Key features:

* Ray **head** runs inside an EC2 **Auto Scaling Group (ASG, desired=1)** for automatic replacement.
* Internal **Network Load Balancer (NLB)** + **Route53** private record provides a stable DNS `ray-head.*` endpoint.
* Head boots from a Launch Template `user_data` which fetches an autoscaler YAML from S3 and starts the Ray head with `--autoscaling-config`.
* Workers are created by the autoscaler and run a **reconnect watchdog** (systemd service) so they persist across head replacement and automatically rejoin the new head.
* CloudWatch `HeadReady` heartbeat metric and Alarms + SNS for monitoring and remediation.
* Pulumi is used to declare and create AWS resources.

## Repo layout

```
project/
├── run.sh
├── infra/
│   ├── aws_networking.py
│   ├── iam.py
│   ├── dynamic_ray_generator.yaml.py
│   └── head_resilience.py
└── scripts/
    ├── worker_reconnect.sh
    └── ray-reconnect.service
```

## High-level deterministic workflow

1. Pulumi provisions IAM, Launch Template, NLB, Target Group, ASG (min=1, desired=1), lifecycle hook, Route53 record and CloudWatch alarms.
2. Head instance boots, `user_data`:

   * fetches secret from SSM,
   * fetches autoscaler YAML from S3,
   * starts Ray head with `--autoscaling-config`,
   * when healthy writes `Ray/Head HeadReady=1` and calls `CompleteLifecycleAction` so ASG marks the instance InService,
   * heartbeats `HeadReady` every 60s.
3. Ray autoscaler (running on the head) launches worker instances. `setup_commands` install the reconnect watchdog and systemd unit.
4. If head is replaced, DNS (Route53 → NLB) remains stable. Workers keep running and their watchdog reconnects to the new head automatically. CloudWatch alarms notify ops on failures.

## Key files and intent

* `infra/aws_networking.py` — VPC, subnets, route tables, optional NAT & VPC endpoints.
* `infra/iam.py` — EC2 roles, instance profile, small ELB policy helper.
* `infra/dynamic_ray_generator.yaml.py` — generates `ray_ec2_autoscaler.yaml` dynamically (worker `setup_commands` included). Edit AMIs/keynames before production.
* `infra/head_resilience.py` — Pulumi module that creates head LaunchTemplate, ASG, NLB, Route53, lifecycle hook, CloudWatch alarms and SNS.
* `scripts/worker_reconnect.sh` & `scripts/ray-reconnect.service` — worker watchdog to auto-reconnect workers to the head.

## Minimal prerequisites

* AWS account and credentials (`aws configure` or environment).
* Pulumi installed and logged in.
* `aws`, `jq`, `ray` (for local testing/development).
* Create SSM SecureString: `/ray/prod/redis_password`.
* Upload autoscaler YAML to S3: `s3://ray-configs/prod/autoscaler.yaml` (or update Pulumi config to your path).
* Chosen AMIs for head & workers (Ubuntu recommended).

## Quick start (concise)

1. Set Pulumi config keys:

```bash
pulumi config set vpcId <vpc-id>
pulumi config set --path subnetIds '["subnet-priv-1","subnet-priv-2"]'
pulumi config set headAmi <ami-id>
pulumi config set privateHostedZoneId <private-zone-id>
pulumi config set keyName <ec2-keypair>  # optional
pulumi config set redisSsmParam /ray/prod/redis_password
```

2. Upload autoscaler YAML:

```bash
aws s3 cp ./ray_ec2_autoscaler.yaml s3://ray-configs/prod/autoscaler.yaml
```

3. Create SSM param:

```bash
aws ssm put-parameter --name /ray/prod/redis_password --type SecureString --value "CHANGE_ME" --region us-east-1
```

4. Deploy:

```bash
./run.sh
```

## Testing checklist (must pass)

* `ray-head.<stack>.<zone>` resolves and points to NLB.
* `ray status` on head shows `ALIVE`.
* Deploy a worker via autoscaler; worker appears in `ray nodes`.
* Terminate head instance; ASG launches replacement, head becomes `ALIVE`, workers rejoin within ~3 minutes.
* CloudWatch `HeadReady` heartbeat exists and alarms fire when expected.

## Operational runbook (cheat commands)

* Get head ASG instance id:

```bash
aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names ray-head-asg-prod --query 'AutoScalingGroups[0].Instances[0].InstanceId'
```

* Force head replacement:

```bash
aws ec2 terminate-instances --instance-ids <id>
```

* Restart Ray on head via SSM:

```bash
aws ssm send-command --document-name "AWS-RunShellScript" --parameters commands=["sudo pkill -f ray || true","sudo ray start --head ..."] --instance-ids <id>
```

* Fetch bootstrap logs via SSM shell:

```bash
aws ssm start-session --target <id>
sudo cat /var/log/ray-head-bootstrap.log
```

## Security & best-practices (concise)

* Secrets: store in SSM/Secrets Manager; do not bake in AMI or user_data.
* IAM: tighten policies to specific ARNs (SSM param and S3 path). Use least privilege.
* Use private subnets for head/workers; prefer SSM Session Manager instead of public SSH.
* Bake worker/head AMIs for faster scale-up if needed.

## Next steps (recommended)

* Harden IAM policies (replace wildcard ARNs).
* Add CloudWatch Logs agent to ship bootstrap logs.
* Add Lambda/SSM automation for alarm-driven remediation.
* Add CI test to simulate head termination and verify worker rejoin.

