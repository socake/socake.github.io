---
title: "AWS IAM 权限管理实践"
date: 2025-12-09T16:00:00+08:00
draft: false
tags: ["AWS", "IAM", "安全", "云原生"]
categories: ["Kubernetes"]
description: "AWS IAM 权限管理完整实践：核心概念、最小权限原则、OIDC 联合身份、AssumeRole 跨账号授权及权限排查方法"
summary: "从 IAM 核心概念到 IRSA/GitHub Actions OIDC 联合身份，再到权限边界与 SCP，系统梳理 AWS IAM 在生产环境的最佳实践。"
toc: true
math: false
diagram: false
keywords: ["IAM", "IRSA", "OIDC", "AssumeRole", "权限边界", "AWS安全"]
params:
  reading_time: true
---

## IAM 核心概念

IAM 的权限模型基于三个问题：**谁**（Principal）能做**什么**（Action）对**什么资源**（Resource）在**什么条件**（Condition）下。

### 核心实体关系

```
Account
  ├── User（长期凭据，尽量少用）
  │     └── 可以属于多个 Group
  ├── Group（策略集合，只能包含 User）
  ├── Role（可被 Assume 的临时凭据载体）
  │     ├── 信任策略（谁能 Assume 这个 Role）
  │     └── 权限策略（这个 Role 能做什么）
  └── Policy（JSON 格式的权限声明）
        ├── AWS 托管策略
        ├── 客户托管策略
        └── 内联策略（直接嵌入 Entity）
```

**原则**：生产环境不应该有长期 AK/SK。人用 SSO/Role，机器用 IRSA/Instance Profile/Role Chaining。

---

## Policy 结构详解

### 基本结构

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AllowS3ReadOnSpecificBucket",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:ListBucket"
      ],
      "Resource": [
        "arn:aws:s3:::my-bucket",
        "arn:aws:s3:::my-bucket/*"
      ],
      "Condition": {
        "StringEquals": {
          "s3:prefix": ["logs/", "data/"]
        },
        "IpAddress": {
          "aws:SourceIp": "203.0.113.0/24"
        }
      }
    }
  ]
}
```

### Effect/Action/Resource 细节

**Effect**：`Allow` 或 `Deny`。显式 Deny 优先级最高，覆盖任何 Allow。

**Action 通配符**：

```json
// 所有 S3 操作
"Action": "s3:*"

// 所有 List 类操作
"Action": "s3:List*"

// 精确指定（推荐）
"Action": ["s3:GetObject", "s3:PutObject"]
```

**Resource ARN 格式**：

```
arn:partition:service:region:account-id:resource-type/resource-id

# 示例
arn:aws:s3:::my-bucket/*
arn:aws:iam::123456789012:role/my-role
arn:aws:eks:us-west-2:123456789012:cluster/prod-cluster
arn:aws:ec2:us-west-2:123456789012:instance/i-1234567890abcdef0
```

### 常用 Condition 操作符

```json
{
  "Condition": {
    // 字符串精确匹配
    "StringEquals": {"aws:RequestedRegion": "us-west-2"},

    // 字符串前缀
    "StringLike": {"s3:prefix": ["home/${aws:username}/*"]},

    // 标签条件（常用于资源隔离）
    "StringEquals": {"ec2:ResourceTag/Environment": "prod"},

    // 时间窗口
    "DateGreaterThan": {"aws:CurrentTime": "2025-01-01T00:00:00Z"},

    // MFA 要求
    "BoolIfExists": {"aws:MultiFactorAuthPresent": "true"},

    // 源 IP
    "IpAddress": {"aws:SourceIp": ["10.0.0.0/8", "172.16.0.0/12"]}
  }
}
```

---

## 最小权限原则实践

### 从宽到窄的迭代方法

1. 先用 `CloudTrail + IAM Access Analyzer` 记录实际调用
2. 用 `aws iam generate-policy` 基于 CloudTrail 生成最小策略
3. 替换原有宽泛策略，观察告警

```bash
# 生成基于 CloudTrail 的最小化策略（需先安装 policy_sentry 或 iamlive）
# 方法1: iamlive（本地代理抓取 API 调用）
pip install iamlive
iamlive --mode proxy --sort-alphabetically

# 方法2: 基于 CloudTrail 事件
aws iam generate-service-last-accessed-details \
  --arn arn:aws:iam::123456789012:role/my-role

aws iam get-service-last-accessed-details \
  --job-id <job-id>
```

### 托管策略 vs 自定义策略

| 场景 | 推荐 |
|------|------|
| EKS 节点基础权限 | 用 AWS 托管（AmazonEKSWorkerNodePolicy 等）|
| 应用访问特定 S3 | 自定义策略，精确到 Bucket 和前缀 |
| 开发者访问控制台 | 自定义，结合 IP/MFA Condition |
| 跨账号读取 | 自定义信任策略 + 权限策略 |

---

## OIDC 联合身份

### GitHub Actions OIDC（无需长期 AK/SK）

```bash
# 1. 创建 GitHub OIDC Provider（每个账号只需一次）
aws iam create-open-id-connect-provider \
  --url https://token.actions.githubusercontent.com \
  --client-id-list sts.amazonaws.com \
  --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1
```

IAM Role 信任策略：

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
        },
        "StringLike": {
          "token.actions.githubusercontent.com:sub": "repo:my-org/my-repo:*"
        }
      }
    }
  ]
}
```

GitHub Actions workflow：

```yaml
# .github/workflows/deploy.yaml
permissions:
  id-token: write
  contents: read

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - name: Configure AWS credentials
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: arn:aws:iam::123456789012:role/github-actions-role
          aws-region: us-west-2
          role-session-name: github-actions-${{ github.run_id }}

      - name: Deploy
        run: |
          aws sts get-caller-identity
          aws ecr get-login-password | docker login --username AWS --password-stdin ...
```

### K8s IRSA（详见 EKS 实战指南）

核心差异：GitHub Actions 用 `token.actions.githubusercontent.com`，IRSA 用 `oidc.eks.<region>.amazonaws.com/id/<hash>`，原理相同，都是 OIDC Web Identity Token 换 STS 临时凭据。

---

## AssumeRole 跨账号授权

### 场景：从 Dev 账号访问 Prod 账号资源

```bash
# Prod 账号：创建 Role，信任 Dev 账号
# 信任策略（Prod 账号操作）
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "AWS": "arn:aws:iam::DEV_ACCOUNT_ID:root"
      },
      "Action": "sts:AssumeRole",
      "Condition": {
        "BoolIfExists": {
          "aws:MultiFactorAuthPresent": "true"
        }
      }
    }
  ]
}

# Dev 账号：给用户/角色添加 AssumeRole 权限
{
  "Effect": "Allow",
  "Action": "sts:AssumeRole",
  "Resource": "arn:aws:iam::PROD_ACCOUNT_ID:role/cross-account-read-role"
}
```

```bash
# 手动 assume role
CREDS=$(aws sts assume-role \
  --role-arn arn:aws:iam::PROD_ACCOUNT_ID:role/cross-account-read-role \
  --role-session-name my-session \
  --duration-seconds 3600)

export AWS_ACCESS_KEY_ID=$(echo $CREDS | jq -r '.Credentials.AccessKeyId')
export AWS_SECRET_ACCESS_KEY=$(echo $CREDS | jq -r '.Credentials.SecretAccessKey')
export AWS_SESSION_TOKEN=$(echo $CREDS | jq -r '.Credentials.SessionToken')

# 验证
aws sts get-caller-identity
```

### 在 aws config 中配置 Role Chaining

```ini
# ~/.aws/config
[profile prod-read]
role_arn = arn:aws:iam::PROD_ACCOUNT_ID:role/cross-account-read-role
source_profile = default
mfa_serial = arn:aws:iam::DEV_ACCOUNT_ID:mfa/my-user
duration_seconds = 3600
```

```bash
# 直接使用，自动 assume role
aws s3 ls --profile prod-read
```

---

## 权限排查

### 快速定位是否有权限

```bash
# 检查当前身份
aws sts get-caller-identity

# 模拟权限检查（不实际执行操作）
aws iam simulate-principal-policy \
  --policy-source-arn arn:aws:iam::123456789012:role/my-role \
  --action-names s3:PutObject \
  --resource-arns arn:aws:s3:::my-bucket/test.txt

# 输出
{
    "EvaluationResults": [
        {
            "EvalActionName": "s3:PutObject",
            "EvalResourceName": "arn:aws:s3:::my-bucket/test.txt",
            "EvalDecision": "allowed"   # 或 "explicitDeny" / "implicitDeny"
        }
    ]
}
```

### Access Analyzer 找最小权限

```bash
# 创建分析器
aws accessanalyzer create-analyzer \
  --analyzer-name my-analyzer \
  --type ACCOUNT

# 查看外部访问发现
aws accessanalyzer list-findings \
  --analyzer-name my-analyzer

# 生成最小权限策略（基于 CloudTrail）
aws accessanalyzer generate-policy \
  --policy-generation-details \
    principalArn=arn:aws:iam::123456789012:role/my-role
```

### CloudTrail 排查历史权限错误

```bash
# 查找 AccessDenied 事件
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=EventName,AttributeValue=AccessDenied \
  --start-time "2025-12-01T00:00:00Z" \
  --max-results 10

# 更精确：用 CloudWatch Logs Insights（需先将 CloudTrail 导入 CWL）
fields @timestamp, userIdentity.arn, errorCode, errorMessage, requestParameters
| filter errorCode = "AccessDenied"
| sort @timestamp desc
| limit 50
```

---

## 常见陷阱

### 1. 权限边界（Permission Boundary）

权限边界限制 Role/User 的最大权限天花板，即使 Policy 允许，边界不包含也无效。

```bash
# 创建带权限边界的 Role（常用于授权他人创建 Role，防止越权）
aws iam create-role \
  --role-name limited-role \
  --assume-role-policy-document file://trust.json \
  --permissions-boundary arn:aws:iam::123456789012:policy/dev-boundary
```

### 2. SCP（Service Control Policy）

SCP 在 AWS Organizations 层面限制整个账号或 OU，优先级高于所有账号内策略。

```bash
# 查看账号上有哪些 SCP
aws organizations list-policies-for-target \
  --target-id $(aws organizations describe-account \
    --account-id $(aws sts get-caller-identity --query Account --output text) \
    --query 'Account.Id' --output text) \
  --filter SERVICE_CONTROL_POLICY
```

### 3. 路径问题

IAM 资源有 Path 属性（默认 `/`），某些 Policy 的 Resource 指定了路径，会导致不匹配：

```json
// 错误：只匹配 path=/service/ 下的 role
"Resource": "arn:aws:iam::*:role/service/*"

// 正确：匹配所有路径下的特定 role
"Resource": "arn:aws:iam::*:role*my-role-name"
```

### 4. Not Action / Not Resource

```json
// 拒绝除指定操作之外的所有操作（慎用，语义容易混淆）
{
  "Effect": "Deny",
  "NotAction": ["s3:GetObject", "s3:ListBucket"],
  "Resource": "*"
}

// 常用于 SCP：只允许特定区域
{
  "Effect": "Deny",
  "NotAction": [
    "iam:*",
    "sts:*",
    "cloudfront:*"
  ],
  "Resource": "*",
  "Condition": {
    "StringNotEquals": {
      "aws:RequestedRegion": ["us-west-2", "us-east-1"]
    }
  }
}
```

### 5. 资源策略与身份策略并存

S3、KMS、SQS 等服务支持资源策略（Resource-based Policy），权限评估是身份策略 **AND** 资源策略（跨账号时两者都要有 Allow，同账号只需一个 Allow 即可）。

```bash
# 查看 S3 Bucket Policy
aws s3api get-bucket-policy --bucket my-bucket

# 查看 KMS Key Policy
aws kms get-key-policy --key-id <key-id> --policy-name default
```
