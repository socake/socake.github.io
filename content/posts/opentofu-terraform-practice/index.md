---
title: "OpenTofu 实战：开源 Terraform 管理 AWS 和阿里云基础设施"
date: 2026-04-11T13:00:00+08:00
draft: false
tags: ["OpenTofu", "Terraform", "IaC", "AWS", "阿里云", "DevOps"]
categories: ["基础设施"]
description: "从 HashiCorp BSL 协议事件说起，系统介绍 OpenTofu 核心概念，并通过 AWS EKS 和阿里云 ACK 的完整实例演示如何用 IaC 管理云基础设施，涵盖 State 管理、Module 封装与 Atlantis 自动化。"
summary: "Terraform 改协议了，OpenTofu 是开源的替代。本文介绍 OpenTofu 核心概念，并给出创建 AWS EKS 和阿里云 ACK 的完整配置示例，以及 State 管理、Module 复用和 Atlantis GitOps 集成方案。"
toc: true
math: false
diagram: false
keywords: ["OpenTofu", "Terraform 替代", "IaC", "AWS EKS", "阿里云 ACK", "Atlantis"]
params:
  reading_time: true
---

## OpenTofu 是什么

2023 年 8 月，HashiCorp 宣布将 Terraform 的许可证从 MPL 2.0（开源）改为 BSL 1.1（Business Source License）。BSL 的核心限制是：**不能用 Terraform 去构建与 HashiCorp 竞争的产品或服务**，这直接影响了大量基于 Terraform 构建的 SaaS 工具商。

同年 9 月，OpenTofu 项目在 Linux Foundation 下诞生，是 Terraform 1.5.x 的直接 Fork，保持 MPL 2.0 开源许可证。截至 2026 年初，OpenTofu 已经发布到 1.9 版本，在 Provider 兼容性上完全继承了 Terraform 生态。

**该用 OpenTofu 还是 Terraform？**

- 如果你的团队没有使用 Terraform Cloud/Enterprise，且不构建 Terraform SaaS，两者区别不大
- 新项目推荐直接用 OpenTofu，避免未来的许可证风险
- 已有 Terraform 项目可以用 `tofu` 命令替换 `terraform` 命令，大多数情况无需改动 .tf 文件

```bash
# 安装 OpenTofu
brew install opentofu  # macOS

# Linux
curl --proto '=https' --tlsv1.2 -fsSL https://get.opentofu.org/install-opentofu.sh | sh

# 验证
tofu version
```

## 核心概念速查

### Provider

Provider 是连接云平台的插件。使用前需要在 `required_providers` 里声明版本约束：

```hcl
terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"  # 锁定大版本，允许小版本升级
    }
    alicloud = {
      source  = "aliyun/alicloud"
      version = "~> 1.220"
    }
  }
}

provider "aws" {
  region = var.aws_region
  # 凭证从环境变量 AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY 读取
  # 或者 ~/.aws/credentials / IAM Role
}

provider "alicloud" {
  region     = var.alicloud_region
  access_key = var.alicloud_access_key  # 建议用 sensitive 变量或环境变量
  secret_key = var.alicloud_secret_key
}
```

### Resource、Data Source、Output

```hcl
# Resource：创建/管理云资源
resource "aws_s3_bucket" "logs" {
  bucket = "my-app-logs-${var.environment}"

  tags = {
    Environment = var.environment
    ManagedBy   = "opentofu"
  }
}

# Data Source：查询已存在的资源（只读）
data "aws_ami" "amazon_linux" {
  most_recent = true
  owners      = ["amazon"]
  filter {
    name   = "name"
    values = ["al2023-ami-*-x86_64"]
  }
}

# Output：暴露给其他模块或 CLI 输出
output "bucket_arn" {
  value = aws_s3_bucket.logs.arn
}
```

## AWS Provider：创建 EKS 集群完整示例

下面是一个生产可用的 EKS 集群配置，包含 VPC、Subnet、EKS Control Plane 和 NodeGroup：

### 目录结构

```
eks-cluster/
├── main.tf
├── variables.tf
├── outputs.tf
├── vpc.tf
├── eks.tf
└── backend.tf
```

### vpc.tf

```hcl
# 创建 VPC
resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = { Name = "${var.cluster_name}-vpc" }
}

# 互联网网关
resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${var.cluster_name}-igw" }
}

# 公有子网（NAT Gateway 和 Load Balancer 用）
resource "aws_subnet" "public" {
  count             = length(var.availability_zones)
  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 4, count.index)
  availability_zone = var.availability_zones[count.index]
  map_public_ip_on_launch = true

  tags = {
    Name                                        = "${var.cluster_name}-public-${count.index + 1}"
    "kubernetes.io/role/elb"                    = "1"  # ALB 需要
    "kubernetes.io/cluster/${var.cluster_name}" = "shared"
  }
}

# 私有子网（Worker Node 用）
resource "aws_subnet" "private" {
  count             = length(var.availability_zones)
  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 4, count.index + length(var.availability_zones))
  availability_zone = var.availability_zones[count.index]

  tags = {
    Name                                        = "${var.cluster_name}-private-${count.index + 1}"
    "kubernetes.io/role/internal-elb"           = "1"
    "kubernetes.io/cluster/${var.cluster_name}" = "shared"
  }
}

# NAT Gateway（私有子网出网用）
resource "aws_eip" "nat" {
  count  = length(var.availability_zones)
  domain = "vpc"
}

resource "aws_nat_gateway" "main" {
  count         = length(var.availability_zones)
  allocation_id = aws_eip.nat[count.index].id
  subnet_id     = aws_subnet.public[count.index].id
  depends_on    = [aws_internet_gateway.main]
}
```

### eks.tf

```hcl
# EKS IAM Role
resource "aws_iam_role" "eks_cluster" {
  name = "${var.cluster_name}-cluster-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "eks.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "eks_cluster_policy" {
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
  role       = aws_iam_role.eks_cluster.name
}

# EKS 集群
resource "aws_eks_cluster" "main" {
  name     = var.cluster_name
  version  = var.kubernetes_version
  role_arn = aws_iam_role.eks_cluster.arn

  vpc_config {
    subnet_ids              = concat(aws_subnet.public[*].id, aws_subnet.private[*].id)
    endpoint_private_access = true
    endpoint_public_access  = true
    public_access_cidrs     = var.allowed_cidrs
  }

  enabled_cluster_log_types = ["api", "audit", "authenticator"]

  depends_on = [aws_iam_role_policy_attachment.eks_cluster_policy]
}

# Node Group IAM Role
resource "aws_iam_role" "node_group" {
  name = "${var.cluster_name}-node-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "node_group_policies" {
  for_each = toset([
    "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
    "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
    "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
  ])
  policy_arn = each.value
  role       = aws_iam_role.node_group.name
}

# EKS Managed Node Group
resource "aws_eks_node_group" "main" {
  cluster_name    = aws_eks_cluster.main.name
  node_group_name = "${var.cluster_name}-ng-01"
  node_role_arn   = aws_iam_role.node_group.arn
  subnet_ids      = aws_subnet.private[*].id

  instance_types = var.node_instance_types
  capacity_type  = "ON_DEMAND"

  scaling_config {
    desired_size = var.node_desired_count
    max_size     = var.node_max_count
    min_size     = var.node_min_count
  }

  update_config {
    max_unavailable = 1
  }

  labels = {
    role = "application"
  }

  depends_on = [aws_iam_role_policy_attachment.node_group_policies]
}
```

## 阿里云 Provider：创建 ACK 集群

```hcl
# 创建 VPC
resource "alicloud_vpc" "main" {
  vpc_name   = "${var.cluster_name}-vpc"
  cidr_block = "172.16.0.0/16"
}

# 交换机（类似 AWS Subnet）
resource "alicloud_vswitch" "worker" {
  count        = 3
  vswitch_name = "${var.cluster_name}-vsw-${count.index + 1}"
  cidr_block   = cidrsubnet("172.16.0.0/16", 4, count.index)
  vpc_id       = alicloud_vpc.main.id
  zone_id      = data.alicloud_zones.available.zones[count.index].id
}

# ACK 托管版集群
resource "alicloud_cs_managed_kubernetes" "main" {
  name               = var.cluster_name
  cluster_spec       = "ack.pro.small"  # 专业版
  kubernetes_version = "1.30.1-aliyun.1"

  vswitch_ids = alicloud_vswitch.worker[*].id
  pod_cidr    = "10.244.0.0/16"
  service_cidr = "10.96.0.0/16"

  # 网络插件
  proxy_mode   = "ipvs"
  # Terway 网络插件（阿里云推荐，支持 NetworkPolicy 和 ENI）

  # 日志
  enable_log = true
  log_config {
    type     = "SLS"
    project  = alicloud_log_project.k8s.name
  }

  # 控制面私有化（可选，提升安全性）
  endpoint_public_access_enabled = true
  resource_group_id = var.resource_group_id

  addons {
    name   = "terway-eniip"
    config = jsonencode({ "IPVlan" = "false", "NetworkPolicy" = "true" })
  }

  addons {
    name = "csi-plugin"
  }
}

# Worker 节点池
resource "alicloud_cs_kubernetes_node_pool" "main" {
  cluster_id     = alicloud_cs_managed_kubernetes.main.id
  node_pool_name = "default-pool"

  vswitch_ids    = alicloud_vswitch.worker[*].id
  instance_types = ["ecs.c7.xlarge"]

  system_disk_category = "cloud_essd"
  system_disk_size     = 100

  scaling_config {
    enable       = true
    min_size     = 2
    max_size     = 10
    desired_size = 3
    type         = "cpu"
  }
}
```

## State 管理：远程 Backend

默认 State 存在本地 `terraform.tfstate`，多人协作时会冲突。AWS S3 + DynamoDB 是最常用的远程 Backend：

```hcl
# backend.tf
terraform {
  backend "s3" {
    bucket         = "my-company-tofu-state"
    key            = "eks-prod/terraform.tfstate"
    region         = "us-west-2"
    encrypt        = true  # S3 服务端加密

    # DynamoDB 实现分布式锁，防止并发 apply
    dynamodb_table = "tofu-state-lock"
  }
}
```

DynamoDB 表只需要一个 `LockID` 主键（String 类型），用 AWS Console 或 tofu 创建都行：

```bash
aws dynamodb create-table \
  --table-name tofu-state-lock \
  --attribute-definitions AttributeName=LockID,AttributeType=S \
  --key-schema AttributeName=LockID,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST
```

## Module 封装

把常用的资源组合封装成 Module，像函数一样复用：

```
modules/
└── eks-cluster/
    ├── main.tf
    ├── variables.tf
    └── outputs.tf

envs/
├── prod/
│   └── main.tf  # 调用 module，传不同参数
└── staging/
    └── main.tf
```

```hcl
# envs/prod/main.tf
module "eks_prod" {
  source = "../../modules/eks-cluster"

  cluster_name       = "prod-us-west-2"
  kubernetes_version = "1.30"
  vpc_cidr           = "10.0.0.0/16"
  availability_zones = ["us-west-2a", "us-west-2b", "us-west-2c"]
  node_instance_types = ["c6i.xlarge"]
  node_desired_count = 5
  node_min_count     = 3
  node_max_count     = 20
}
```

## GitOps 集成：Atlantis 自动化

Atlantis 是专为 Terraform/OpenTofu 设计的 GitOps 工具，PR 自动触发 `plan`，审批后自动 `apply`。

```yaml
# atlantis.yaml
version: 3
projects:
  - name: eks-prod
    dir: envs/prod
    workspace: default
    terraform_version: tofu1.8
    autoplan:
      when_modified:
        - "**/*.tf"
        - "../../modules/**/*.tf"
      enabled: true
    apply_requirements:
      - approved      # 必须有人 approve PR
      - mergeable     # PR 必须可合并（CI 通过）
```

工作流：
1. 开发者修改 `.tf` 文件，提交 PR
2. Atlantis 自动运行 `tofu plan`，把 Plan 结果评论在 PR 里
3. 同事 Review Plan，确认无问题后 Approve PR
4. 在 PR 里评论 `atlantis apply`
5. Atlantis 运行 `tofu apply`，基础设施变更生效

## 踩坑记录

**State 文件锁超时**

`apply` 被强制中断后，DynamoDB 锁没有释放，下次操作报 "Error locking state"。手动解锁：

```bash
tofu force-unlock <lock-id>
# lock-id 在错误信息里会显示
```

**Provider 版本锁定**

`tofu init` 后会生成 `.terraform.lock.hcl`，这个文件要提交到 Git，确保团队所有人用同一版本的 Provider。Provider 大版本升级时可能有 Breaking Change，必须先看 Changelog。

**Import 已有资源**

如果云上有不是用 OpenTofu 创建的资源，想纳入管理：

```bash
# 先在 .tf 里写好 resource 块，再 import
tofu import aws_s3_bucket.logs my-existing-bucket-name

# OpenTofu 1.5+ 支持 import 块，更优雅
import {
  to = aws_s3_bucket.logs
  id = "my-existing-bucket-name"
}
```

Import 后运行 `tofu plan`，如果配置和实际资源有差异，Plan 会显示 diff，手动补齐配置直到 Plan 显示 "No changes"。

**count vs for_each**

用 `count` 创建多个资源时，如果删除中间某个（比如删掉 index 1 的子网），OpenTofu 会重新索引，导致后续所有资源被销毁重建。生产环境一定要用 `for_each`，Key 基于稳定的标识符（zone name 而不是 index）。

```hcl
# 不推荐
resource "aws_subnet" "private" {
  count = 3
  # ...
}

# 推荐
resource "aws_subnet" "private" {
  for_each          = toset(var.availability_zones)
  availability_zone = each.key
  # ...
}
```
