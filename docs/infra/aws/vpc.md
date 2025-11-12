```sh
VPC (ray-vpc-<stack>) [10.0.0.0/16]
│
├── InternetGateway (ray-igw-<stack>)
│
├── Public Subnets
│   ├── ray-public-0-<stack> [10.0.1.0/24]
│   │   ├── ALB (internet-facing, HTTPS :443 → Gateway :8003)
│   │   ├── NAT Gateway (if NO_NAT=false)
│   │   └── Route Table: 0.0.0.0/0 → IGW
│   └── ray-public-1-<stack> [10.0.2.0/24] (if MULTI_AZ=true)
│       └── same structure as above
│
├── Private Subnets
│   ├── ray-private-0-<stack> [10.0.11.0/24]
│   │   ├── Ray Head EC2 (Gateway + Ray Head service)
│   │   ├── Ray Workers (autoscaled)
│   │   └── Route Table:
│   │       ├── if NO_NAT=false → 0.0.0.0/0 → NAT Gateway
│   │       └── if NO_NAT=true → private only (optionally VPC Endpoints)
│   └── ray-private-1-<stack> [10.0.12.0/24] (if MULTI_AZ=true)
│       └── Ray Workers (autoscaled)
│
├── Security Groups
│   ├── ALB SG: allow 443/80 from 0.0.0.0/0 → Gateway port 8003
│   ├── Head SG: allow 8003 + 6379 from VPC CIDR and ALB SG
│   └── Worker SG: allow internal Ray and model traffic
│
├── Optional VPC Endpoints (if CREATE_VPC_ENDPOINTS=true)
│   ├── S3 Gateway Endpoint
│   ├── Interface Endpoints: SSM, EC2Messages, SecretsManager, ECR, STS
│   └── Security Group: allows 443 inside VPC
│
└── S3 Buckets + KMS + SSM
    ├── pulumi-state-bucket
    ├── autoscaler-bucket
    ├── models-bucket
    ├── KMS CMK for SSM encryption
    └── SSM Parameter (/ray/prod/redis_password)

```

Breakdown of that tree:

---

### 1. **VPC (ray-vpc-<stack>)**

Top-level private network for everything.
CIDR `10.0.0.0/16` means it owns IPs `10.0.x.x`.
All subnets, gateways, and security groups live here.

---

### 2. **InternetGateway**

A VPC attachment that provides outbound internet access for **public subnets**.
Without it, nothing can reach the public internet.

---

### 3. **Public Subnets**

Each in a different Availability Zone (if `MULTI_AZ=true`).

* Host **internet-facing components**:

  * The **ALB** (Application Load Balancer).
  * The **NAT Gateway** (if enabled).
* Each subnet has a route table that sends all `0.0.0.0/0` traffic through the **InternetGateway**.

**Why:** users need a way to enter from the internet, but backend compute stays private.

---

### 4. **Private Subnets**

Internal network where all compute runs.

* **Ray Head EC2** → runs Ray control plane and the Gateway API.
* **Ray Workers** → autoscaled nodes running embedding, rerank, and LLM tasks.
* Their route table depends on setup:

  * If `NO_NAT=false`: send outbound traffic through **NAT Gateway** (can access internet safely).
  * If `NO_NAT=true`: no outbound route, so they need **VPC Endpoints** for AWS APIs.

**Why:** isolates compute and data from public exposure.

---

### 5. **Security Groups**

Virtual firewalls controlling inbound/outbound rules.

| SG        | Allows                        | Purpose            |
| --------- | ----------------------------- | ------------------ |
| ALB SG    | 443/80 from anywhere          | Public entry       |
| Head SG   | 8003 + 6379 from VPC + ALB SG | Internal Ray + API |
| Worker SG | Internal Ray communication    | Autoscaled workers |

**Why:** enforce least-privilege network paths.

---

### 6. **VPC Endpoints** (optional)

Private connections to AWS services.
If you remove NAT (`NO_NAT=true`), you need these to let EC2s reach:

* S3 (for model/artifact fetch)
* SSM / SecretsManager (for config + secrets)
* ECR (for pulling containers)
* STS (for IAM tokens)

They replace internet egress.

---

### 7. **S3 Buckets, KMS, SSM**

Support services for the system:

* **pulumi-state-bucket** → stores Pulumi deployment state.
* **autoscaler-bucket** → holds Ray autoscaler YAMLs.
* **models-bucket** → stores model artifacts.
* **KMS key** → encrypts secrets.
* **SSM parameter** → securely stores Redis password (used by gateway rate limiter).

**Why:** decouple storage, secrets, and infrastructure management cleanly.

---

### 8. **Flow summary**

1. User → ALB (public HTTPS).
2. ALB → Gateway on Ray head (private HTTP:8003).
3. Gateway → Ray internal services (private-only).
4. Ray → managed stores (via endpoints).
5. Response streams back → ALB → user.

---

This tree shows the **layered isolation** model:

* Public (entry only)
* Private (compute)
* Managed (data + secrets)
  All traffic controlled, logged, and encrypted.
