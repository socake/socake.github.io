---
title: "基础设施即代码：Terraform 入门与实践"
date: 2025-12-09T20:00:00+08:00
draft: false
tags: ["Terraform", "IaC", "AWS", "DevOps"]
categories: ["CI/CD"]
description: "Terraform 完整实践指南：核心概念、HCL 语法、State 管理、模块化设计，以及 EKS 节点组、S3、IAM Role 的实战配置"
summary: "从 IaC 解决的本质问题出发，系统介绍 Terraform 的核心概念和工作流，重点覆盖 State 管理、模块化最佳实践，以及常见陷阱。"
toc: true
math: false
diagram: false
keywords: ["Terraform", "IaC", "HCL", "State管理", "模块化", "EKS"]
params:
  reading_time: true
---

## IaC 解决什么问题

在没有 IaC 之前，基础设施的状态散落在：
- 每个工程师脑子里（"这个安全组是我三年前加的，具体为什么我忘了"）
- 各种 Wiki 文档里（通常已经过时）
- 点点点操作的控制台历史记录（根本没有历史记录）

这带来几个致命问题：

**无法复现**：生产环境出了问题，无法在测试环境精确复现，因为两个环境的配置已经悄悄漂移。

**变更追溯困难**：安全合规要求"三个月前这个端口是怎么开放的"，没人知道。

**团队协作摩擦**：新人不知道为什么某个资源是这样配置的，不敢改，不敢删，积累越来越多的技术债。

IaC 用代码描述基础设施的期望状态，用 Git 管理版本，用 CI/CD 执行变更。这样基础设施的每一次变更都有历史记录、代码审查、自动化测试。

---

## Terraform 核心概念

### Provider

Provider 是 Terraform 与各种 API 通信的桥梁。AWS、GCP、阿里云、GitHub、Kubernetes 都有对应的 Provider。

```hcl
# 声明使用 AWS Provider，锁定版本
terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"   # 允许 5.x，不跨大版本
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.0"
    }
  }
  required_version = ">= 1.6.0"
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      ManagedBy   = "terraform"
      Environment = var.environment
    }
  }
}
```

### Resource

Resource 是 Terraform 管理的最小单元，对应一个真实的基础设施资源。

```hcl
# 语法：resource "<类型>" "<本地名称>" { ... }
resource "aws_s3_bucket" "logs" {
  bucket = "my-company-logs-${var.environment}"

  tags = {
    Name = "Application Logs"
  }
}

# 引用其他资源的属性
resource "aws_s3_bucket_versioning" "logs" {
  bucket = aws_s3_bucket.logs.id   # 引用上面 bucket 的 id

  versioning_configuration {
    status = "Enabled"
  }
}
```

### State

State 是 Terraform 的核心，记录"Terraform 认为真实世界现在是什么状态"。

**State 的作用**：
- 记录 Terraform 管理的资源列表及其 ID
- 计算 `plan` 时的 diff（期望状态 vs 当前状态）
- 追踪资源依赖关系

**State 是敏感数据**：可能包含数据库密码、私钥等，不能放到 Git 里。

### Module

Module 是可复用的 Terraform 代码单元，类似函数。

### Workspace

Workspace 允许同一套代码管理多个环境的 State（dev/staging/prod），但实际生产中更推荐用独立目录/仓库隔离环境，workspace 容易误操作。

---

## 基本工作流

```bash
# 初始化：下载 Provider 插件
terraform init

# 格式化代码
terraform fmt -recursive

# 语法检查
terraform validate

# 预览变更（最重要的命令，必须仔细看）
terraform plan -out=tfplan

# 应用变更
terraform apply tfplan

# 销毁资源（危险！生产环境谨慎使用）
terraform destroy
```

`plan` 的输出要仔细看：

```
# aws_instance.web will be updated in-place    ← 原地更新，低风险
~ resource "aws_instance" "web" {
    id = "i-1234567890abcdef0"
  ~ instance_type = "t3.small" -> "t3.medium"  ← 这个变更
  }

# aws_db_instance.main must be replaced        ← 销毁重建！高风险
-/+ resource "aws_db_instance" "main" {
  ~ identifier = "prod-db" -> "prod-db-v2"     ← 改了 identifier，触发重建
  }
```

看到 `must be replaced` 要非常小心，某些资源（RDS、ElastiCache）重建会有停机时间。

---

## HCL 语法速查

### Variables 和 Outputs

```hcl
# variables.tf
variable "environment" {
  type        = string
  description = "部署环境"
  default     = "dev"

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment 必须是 dev/staging/prod 之一"
  }
}

variable "instance_count" {
  type    = number
  default = 2
}

variable "allowed_cidrs" {
  type    = list(string)
  default = ["10.0.0.0/8"]
}

variable "tags" {
  type    = map(string)
  default = {}
}

# outputs.tf
output "cluster_endpoint" {
  value       = aws_eks_cluster.main.endpoint
  description = "EKS cluster API server endpoint"
  sensitive   = false   # 设为 true 则 plan/apply 时不显示值
}
```

### Locals

```hcl
locals {
  # 常量定义
  app_name = "my-app"

  # 组合表达式
  name_prefix = "${local.app_name}-${var.environment}"

  # 条件表达式
  instance_type = var.environment == "prod" ? "m5.xlarge" : "t3.medium"

  # 合并 tags
  common_tags = merge(var.tags, {
    Application = local.app_name
    Environment = var.environment
  })
}
```

### Data Source

Data Source 读取已有资源的信息，不管理其生命周期。

```hcl
# 读取已有 VPC
data "aws_vpc" "main" {
  filter {
    name   = "tag:Name"
    values = ["prod-vpc"]
  }
}

# 读取最新的 EKS 优化 AMI
data "aws_ssm_parameter" "eks_ami" {
  name = "/aws/service/eks/optimized-ami/1.30/amazon-linux-2/recommended/image_id"
}

# 读取当前账号信息
data "aws_caller_identity" "current" {}

output "account_id" {
  value = data.aws_caller_identity.current.account_id
}
```

### count 和 for_each

```hcl
# count：简单的数量控制
resource "aws_subnet" "private" {
  count             = length(var.availability_zones)
  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 4, count.index)
  availability_zone = var.availability_zones[count.index]

  tags = {
    Name = "${local.name_prefix}-private-${count.index + 1}"
  }
}

# for_each：基于 map 或 set 创建资源（推荐，删除中间元素不会影响其他）
resource "aws_iam_user" "team" {
  for_each = toset(["alice", "bob", "charlie"])
  name     = each.key
}

# 条件创建
resource "aws_cloudwatch_log_group" "app" {
  count = var.enable_cloudwatch_logs ? 1 : 0
  name  = "/app/${local.name_prefix}"
}
```

---

## State 管理

### Remote State（S3 + DynamoDB）

```hcl
# backend.tf
terraform {
  backend "s3" {
    bucket         = "my-terraform-state"
    key            = "prod/eks/terraform.tfstate"
    region         = "us-west-2"
    encrypt        = true
    kms_key_id     = "arn:aws:kms:us-west-2:123456789012:key/mrk-xxx"

    # DynamoDB 表用于状态锁，防止并发执行
    dynamodb_table = "terraform-state-lock"
  }
}
```

```bash
# 创建 S3 bucket 和 DynamoDB 表（先用 aws cli，这部分不能用 Terraform 管理自己的 backend）
aws s3api create-bucket \
  --bucket my-terraform-state \
  --region us-west-2 \
  --create-bucket-configuration LocationConstraint=us-west-2

aws s3api put-bucket-versioning \
  --bucket my-terraform-state \
  --versioning-configuration Status=Enabled

aws dynamodb create-table \
  --table-name terraform-state-lock \
  --attribute-definitions AttributeName=LockID,AttributeType=S \
  --key-schema AttributeName=LockID,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST
```

### State 操作

```bash
# 查看 state 中的资源列表
terraform state list

# 查看单个资源的 state 详情
terraform state show aws_eks_cluster.main

# 将已有资源导入 state（资源已存在，但不在 state 中）
terraform import aws_s3_bucket.legacy my-existing-bucket-name

# 移动资源（重构代码时）
terraform state mv \
  aws_security_group.old_name \
  aws_security_group.new_name

# 从 state 中移除资源（不删除真实资源，只停止 Terraform 管理）
terraform state rm aws_instance.temporary
```

---

## 模块化

### 目录结构

```
infrastructure/
├── modules/
│   ├── eks-cluster/
│   │   ├── main.tf
│   │   ├── variables.tf
│   │   ├── outputs.tf
│   │   └── README.md
│   ├── rds-instance/
│   └── networking/
├── environments/
│   ├── dev/
│   │   ├── main.tf
│   │   ├── variables.tf
│   │   └── terraform.tfvars
│   ├── staging/
│   └── prod/
└── global/           # IAM、Route53 等全局资源
```

### 模块定义（modules/eks-cluster/）

```hcl
# modules/eks-cluster/variables.tf
variable "cluster_name" {
  type = string
}
variable "cluster_version" {
  type    = string
  default = "1.30"
}
variable "node_groups" {
  type = map(object({
    instance_types = list(string)
    min_size       = number
    max_size       = number
    desired_size   = number
  }))
}

# modules/eks-cluster/outputs.tf
output "cluster_endpoint" {
  value = aws_eks_cluster.this.endpoint
}
output "cluster_certificate_authority_data" {
  value = aws_eks_cluster.this.certificate_authority[0].data
}
output "oidc_provider_arn" {
  value = aws_iam_openid_connect_provider.this.arn
}
```

### 调用模块

```hcl
# environments/prod/main.tf
module "eks" {
  source  = "../../modules/eks-cluster"
  # 或者使用 Terraform Registry 的公共模块
  # source  = "terraform-aws-modules/eks/aws"
  # version = "~> 20.0"

  cluster_name    = "prod-cluster"
  cluster_version = "1.30"

  node_groups = {
    general = {
      instance_types = ["m5.xlarge"]
      min_size       = 2
      max_size       = 20
      desired_size   = 3
    }
    gpu = {
      instance_types = ["g4dn.xlarge"]
      min_size       = 0
      max_size       = 5
      desired_size   = 0
    }
  }
}

output "eks_endpoint" {
  value = module.eks.cluster_endpoint
}
```

---

## 实战片段

### 创建 IAM Role（IRSA 场景）

```hcl
# 数据：获取 EKS OIDC Provider ARN
data "aws_eks_cluster" "main" {
  name = var.cluster_name
}

locals {
  oidc_issuer = trimprefix(data.aws_eks_cluster.main.identity[0].oidc[0].issuer, "https://")
}

data "aws_iam_openid_connect_provider" "eks" {
  url = data.aws_eks_cluster.main.identity[0].oidc[0].issuer
}

# IRSA Role
resource "aws_iam_role" "app_irsa" {
  name = "${var.cluster_name}-${var.app_name}-irsa"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Federated = data.aws_iam_openid_connect_provider.eks.arn
      }
      Action = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.oidc_issuer}:sub" = "system:serviceaccount:${var.namespace}:${var.service_account_name}"
          "${local.oidc_issuer}:aud" = "sts.amazonaws.com"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "app_s3" {
  role       = aws_iam_role.app_irsa.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess"
}
```

### 创建加密 S3 Bucket

```hcl
resource "aws_s3_bucket" "data" {
  bucket = "${var.company}-${var.environment}-data"
}

resource "aws_s3_bucket_server_side_encryption_configuration" "data" {
  bucket = aws_s3_bucket.data.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.s3.arn
    }
    bucket_key_enabled = true  # 降低 KMS API 调用成本
  }
}

resource "aws_s3_bucket_public_access_block" "data" {
  bucket = aws_s3_bucket.data.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "data" {
  bucket = aws_s3_bucket.data.id
  rule {
    id     = "transition-to-ia"
    status = "Enabled"
    transition {
      days          = 30
      storage_class = "STANDARD_IA"
    }
    transition {
      days          = 90
      storage_class = "GLACIER"
    }
  }
}
```

---

## 常见陷阱

### 1. State 漂移

有人直接在控制台改了资源，导致真实状态和 State 不一致。

```bash
# 检测漂移（不做任何变更）
terraform plan -refresh-only

# 将真实状态同步到 State（不修改真实资源）
terraform apply -refresh-only
```

### 2. Destroy 顺序问题

Terraform 通常能自动处理资源依赖顺序，但某些情况下需要手动指定 `depends_on`：

```hcl
resource "aws_eks_fargate_profile" "coredns" {
  # 必须等 EKS 集群完全就绪
  depends_on = [aws_eks_addon.coredns]
}
```

### 3. Provider 版本漂移

不锁版本的 `terraform init` 每次可能下载不同版本的 Provider，导致计划出现意外 diff。

```bash
# 生成 .terraform.lock.hcl 文件后提交到 Git
terraform providers lock \
  -platform=linux_amd64 \
  -platform=darwin_amd64 \
  -platform=darwin_arm64
```

### 4. 敏感值泄露到 State

数据库密码、私钥等敏感信息如果放在 Terraform 的 `resource` 里，会明文存在 State 中。

```hcl
# 不要这样做
resource "aws_db_instance" "main" {
  password = "my-hardcoded-password"   # 会出现在 state 里！
}

# 用 AWS Secrets Manager 或随机生成
resource "random_password" "db" {
  length  = 32
  special = false
}

resource "aws_secretsmanager_secret_version" "db_password" {
  secret_id     = aws_secretsmanager_secret.db.id
  secret_string = random_password.db.result
}

resource "aws_db_instance" "main" {
  password = random_password.db.result
  # state 里会有密码，但 state 本身是加密存储的（backend 配置了 encrypt=true）
}
```
