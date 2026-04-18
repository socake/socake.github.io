---
title: "Terragrunt 规模化 Terraform 工程化：从 DRY 到 Stacks"
date: 2026-02-14T10:00:00+08:00
draft: false
tags: ["Terragrunt", "Terraform", "OpenTofu", "IaC", "多环境"]
categories: ["基础设施"]
description: "Terragrunt 2025 年 1.0 GA，核心特性 Stacks 正式发布。本文系统讲解 Terragrunt 的动机、HCL 配置、dependency blocks、run-all 命令、Stacks 模式，以及我们用 Terragrunt 管理 50+ 账号、200+ state 的工程化实践和踩坑。"
summary: "Terraform 写到 10 个 state 以上就开始痛苦：重复的 provider 配置、散落的变量、无法跨 state 引用、run-all 时的依赖混乱。Terragrunt 是 Terraform 的 wrapper，解决的就是'大规模'这个字——本文讲清楚它怎么用。"
toc: true
math: false
diagram: true
keywords: ["Terragrunt", "Stacks", "DRY Terraform", "multi-environment", "run-all"]
params:
  reading_time: true
---

## Terragrunt 存在的理由

Terraform（或 OpenTofu）用到后面都会碰到同一个问题：**state 拆分以后，管理多个 state 变得非常痛苦**。

一个典型的 "只用 Terraform" 项目长这样：

```
terraform/
├── main.tf      # 500 行 all-in-one
├── variables.tf
├── outputs.tf
└── backend.tf
```

单个 state，所有资源都在一起。刚开始很爽，但很快出现问题：

- `terraform plan` 要 2 分钟（资源越来越多）
- 改一个 VPC 配置要 plan 整个生产环境
- blast radius 巨大：小错误可能误删 RDS
- 一个文件 5000 行没法看

所有人的第一反应都是 **拆 state**。按模块、按环境、按账号拆：

```
terraform/
├── modules/
│   ├── vpc/
│   ├── eks/
│   └── rds/
├── envs/
│   ├── dev/
│   │   ├── vpc/
│   │   │   ├── main.tf
│   │   │   ├── backend.tf
│   │   │   └── ...
│   │   ├── eks/
│   │   └── rds/
│   ├── staging/
│   │   └── ... (一模一样的结构)
│   └── prod/
│       └── ... (又一次复制)
```

现在新问题出来了：

1. **backend.tf 到处复制**：每个目录都要写 `bucket`、`key`、`region`、`dynamodb_table`。30 个目录就是 30 份。
2. **provider 配置到处复制**：region、assume_role、default_tags。又是 30 份。
3. **变量穿透麻烦**：`dev/vpc/outputs` 里的 vpc_id 要给 `dev/eks/main.tf` 用，只能用 `data.terraform_remote_state` 手动写一遍。
4. **跨 state 顺序**：vpc 要先于 eks apply。没有工具保证顺序，靠人记忆。
5. **批量操作**：生产升级 Kubernetes 版本，要 apply 20 个 state，手动 `cd` + `terraform apply` 敲到吐。

**Terragrunt 是这些问题的系统解法**。它是 Terraform/OpenTofu 的 wrapper，用一份 `terragrunt.hcl` 定义 "怎么调 Terraform"，而不是"要部署什么"。Terraform 继续管基础设施描述，Terragrunt 管基础设施编排。

## Terragrunt 的核心抽象

### 一个最小的 terragrunt.hcl

```
live/
├── terragrunt.hcl         # 根配置：backend, provider
└── prod/
    └── us-west-2/
        └── vpc/
            └── terragrunt.hcl   # 单元配置：inputs
```

根配置 `live/terragrunt.hcl`：

```hcl
# 为所有子单元生成 backend
remote_state {
  backend = "s3"
  generate = {
    path      = "backend.tf"
    if_exists = "overwrite"
  }
  config = {
    bucket         = "my-company-tfstate"
    key            = "${path_relative_to_include()}/terraform.tfstate"
    region         = "us-west-2"
    encrypt        = true
    dynamodb_table = "tf-locks"
  }
}

# 为所有子单元生成 provider
generate "provider" {
  path      = "provider.tf"
  if_exists = "overwrite_terragrunt"
  contents  = <<EOF
provider "aws" {
  region = "us-west-2"
  default_tags {
    tags = {
      ManagedBy   = "terragrunt"
      Environment = "prod"
    }
  }
}
EOF
}

# 全局变量
inputs = {
  environment = "prod"
  region      = "us-west-2"
}
```

子单元 `live/prod/us-west-2/vpc/terragrunt.hcl`：

```hcl
include "root" {
  path = find_in_parent_folders()
}

terraform {
  source = "git::git@github.com:org/terraform-modules.git//vpc?ref=v1.5.0"
}

inputs = {
  cidr_block = "10.0.0.0/16"
  azs        = ["us-west-2a", "us-west-2b", "us-west-2c"]
}
```

跑起来：

```bash
cd live/prod/us-west-2/vpc
terragrunt init
terragrunt plan
terragrunt apply
```

Terragrunt 背后做的事：

1. **解析 `include`**：加载父目录的 `terragrunt.hcl`，合并 remote_state、generate、inputs
2. **生成 backend.tf、provider.tf**：写到临时目录（`.terragrunt-cache/`）
3. **下载 `source`**：如果 source 是 git，克隆到临时目录
4. **调用 Terraform**：`terraform init` 用生成的 backend，`terraform plan` 传入 inputs

这样每个子单元的 `terragrunt.hcl` 只写"自己独有的配置"，backend/provider 全部继承自根。100 个 state 的项目，backend.tf 只写一次。

### find_in_parent_folders 和 include

`find_in_parent_folders()` 会从当前目录往上找最近的 `terragrunt.hcl`。它是 Terragrunt DRY 模式的核心。

多级 include：

```
live/
├── terragrunt.hcl              # 全局（backend）
├── _env/
│   └── prod.hcl                # prod 环境特有
└── prod/
    └── us-west-2/
        └── _region/
            └── us-west-2.hcl   # region 特有
        └── vpc/
            └── terragrunt.hcl
```

`live/prod/us-west-2/vpc/terragrunt.hcl`：

```hcl
include "root" {
  path = find_in_parent_folders()
}

include "env" {
  path = find_in_parent_folders("_env/prod.hcl")
}

include "region" {
  path = find_in_parent_folders("_region/us-west-2.hcl")
}

terraform {
  source = "${get_path_to_repo_root()}/modules/vpc"
}

inputs = {
  cidr_block = "10.0.0.0/16"
}
```

三层继承：全局 → 环境 → region → 单元。这个模式适合大公司的多环境管理。

## dependencies 和 dependency：跨 state 引用

这是 Terragrunt 最重要的特性。

Terraform 里跨 state 引用要手动：

```hcl
data "terraform_remote_state" "vpc" {
  backend = "s3"
  config = {
    bucket = "my-tfstate"
    key    = "prod/vpc/terraform.tfstate"
    region = "us-west-2"
  }
}

resource "aws_eks_cluster" "main" {
  vpc_config {
    subnet_ids = data.terraform_remote_state.vpc.outputs.private_subnet_ids
  }
}
```

每次引用都要写一遍 data block，出错率高。

Terragrunt 的 `dependency` block 是优雅的替代：

```hcl
# live/prod/us-west-2/eks/terragrunt.hcl
include "root" {
  path = find_in_parent_folders()
}

dependency "vpc" {
  config_path = "../vpc"

  mock_outputs = {
    vpc_id             = "vpc-mock"
    private_subnet_ids = ["subnet-mock-1", "subnet-mock-2"]
  }
  mock_outputs_allowed_terraform_commands = ["validate", "plan"]
}

terraform {
  source = "git::...//modules/eks?ref=v1.5.0"
}

inputs = {
  vpc_id     = dependency.vpc.outputs.vpc_id
  subnet_ids = dependency.vpc.outputs.private_subnet_ids
  cluster_name = "prod-main"
}
```

Terragrunt 在 apply `eks` 之前会：

1. 检查 `../vpc` 是否已经 apply 过
2. 从 `../vpc` 的 state 里读 outputs
3. 把 outputs 注入到当前 inputs

`mock_outputs` 的作用：`../vpc` 还没 apply 时，plan/validate 阶段用 mock 值代替，让你能在 dev 环境看到 plan 而不是报错。

### dependencies block：只定义执行顺序

`dependencies` block（复数）只声明顺序，不读 output：

```hcl
dependencies {
  paths = ["../vpc", "../iam"]
}
```

常用于 "A 必须在 B 之前 apply" 但 A 不需要读 B 的 output。和 `dependency` (单数) 的区别：

| 特性 | dependency | dependencies |
|------|-----------|--------------|
| 读取 outputs | 是 | 否 |
| 影响执行顺序 | 是 | 是 |
| 支持 mock | 是 | 否 |
| 配置方式 | 一个 block 一个依赖 | 一个 block 多个路径 |

## run-all：批量操作的命令

真正的规模化要靠 `run-all`。

```bash
# 在整个 live/prod 目录下按依赖顺序 plan 所有 state
cd live/prod
terragrunt run-all plan

# apply 所有
terragrunt run-all apply

# 只看某一组的 graph
terragrunt graph-dependencies
```

`run-all` 做的事：

1. 递归扫描当前目录下所有 `terragrunt.hcl`
2. 解析每个 unit 的 `dependency` / `dependencies` block 构建 DAG
3. 按 DAG 拓扑顺序调用 Terraform（无依赖的并发）
4. 每个 unit 的 stdout 聚合到一起输出

### run-all 的生产坑

**坑 1：并发数过高打爆 API rate limit**

默认 `run-all` 并发度很高。对 AWS API 的 describe 请求密集时可能打到 rate limit，表现为间歇性 `ThrottlingException`。

限并发：

```bash
terragrunt run-all apply --terragrunt-parallelism 4
```

**坑 2：run-all apply 无人守护**

在 CI 里 `run-all apply` 默认会每个 unit 都问你 "yes/no"。要加 `--auto-approve`：

```bash
terragrunt run-all apply --terragrunt-non-interactive
```

谨慎：这意味着没有人工审核。生产 apply 建议先 `run-all plan` 存 plan 文件，审核后再对每个 plan 文件 apply。

**坑 3：部分失败时的回滚**

`run-all apply` 可能中途某个 unit 失败，之前成功的 unit 已经改了状态。没有"事务回滚"机制。

工程实践：**先 `plan` 确认所有 unit 都能过 plan，再 `apply`**。如果中途失败，手动修好问题继续 apply 未完成的 unit。避免 "apply 一半撤销" 的复杂场景。

## Stacks：2025 的新核心特性

Terragrunt 1.0（2025 年 5 月发布）的核心特性是 **Stacks**。

### Stacks 解决什么

即使有 Terragrunt，多环境多 region 部署依然有 "目录爆炸" 问题：

```
live/
├── prod/
│   ├── us-west-2/
│   │   ├── vpc/     ← 一份 terragrunt.hcl
│   │   ├── eks/
│   │   └── rds/
│   ├── us-east-1/
│   │   ├── vpc/     ← 又一份，几乎一模一样
│   │   ├── eks/
│   │   └── rds/
│   └── eu-west-1/
│       └── ... ← 再一份
└── staging/
    └── ... ← 复制 prod
```

每个环境 * region 都要复制一套目录结构，即使 Terragrunt 的 `include` 已经抽取了共性，每个单元至少还是要一个目录 + 一个 `terragrunt.hcl` 占坑。

### Stacks 的 on-demand 生成

Stacks 的想法是 **用一份 `terragrunt.stack.hcl` 描述"要生成哪些 unit"**：

```hcl
# live/prod/us-west-2/terragrunt.stack.hcl
unit "vpc" {
  source = "${get_repo_root()}/catalog/units/vpc"
  path   = "vpc"

  values = {
    cidr_block = "10.0.0.0/16"
    azs        = ["us-west-2a", "us-west-2b", "us-west-2c"]
  }
}

unit "eks" {
  source = "${get_repo_root()}/catalog/units/eks"
  path   = "eks"

  values = {
    cluster_name = "prod-us-west-2-main"
    k8s_version  = "1.31"
  }
}

unit "rds" {
  source = "${get_repo_root()}/catalog/units/rds"
  path   = "rds"

  values = {
    instance_class = "db.r6g.xlarge"
    multi_az       = true
  }
}
```

然后 `catalog/units/vpc` 是一个可复用的 unit 模板（一个独立目录，里面有 `terragrunt.hcl`）。

执行：

```bash
cd live/prod/us-west-2
terragrunt stack generate
# 这会在 .terragrunt-stack/ 下生成 vpc/、eks/、rds/ 的完整 terragrunt.hcl
terragrunt run-all apply
```

**关键变化**：以前你需要物理复制 30 个目录，现在只要一个 `terragrunt.stack.hcl` 声明"要哪些 unit"，Terragrunt 动态生成。

### 用 for_each 生成批量 unit

Stacks 支持循环生成：

```hcl
locals {
  regions = ["us-west-2", "us-east-1", "eu-west-1"]
}

unit "vpc" {
  for_each = toset(local.regions)
  source   = "${get_repo_root()}/catalog/units/vpc"
  path     = each.key

  values = {
    region     = each.key
    cidr_block = local.cidrs[each.key]
  }
}
```

一份声明生成三份 vpc unit。这是 "Terragrunt as a platform" 的核心能力。

### Stacks 的适用场景

**适合**：
- 大量相似 unit 的批量管理（多 region/多账号/多租户）
- 想要"unit 模板"概念，集中维护，批量实例化
- 基础设施 catalog 化的平台团队

**不适合**：
- 小规模项目（5 个以下 state），直接写 terragrunt.hcl 更简单
- 每个 unit 都非常独特、无复用价值

## 完整项目结构示例

我们公司生产的 Terragrunt 项目大致长这样：

```
infra/
├── terragrunt.hcl                  # 根：remote_state + generate provider
├── _env/
│   ├── global.hcl                  # 跨环境共享
│   ├── dev.hcl                     # dev 特有
│   ├── staging.hcl
│   └── prod.hcl
├── _region/
│   ├── us-west-2.hcl
│   ├── us-east-1.hcl
│   └── eu-west-1.hcl
├── catalog/
│   └── units/
│       ├── vpc/terragrunt.hcl
│       ├── eks/terragrunt.hcl
│       ├── rds/terragrunt.hcl
│       ├── alb/terragrunt.hcl
│       ├── s3/terragrunt.hcl
│       └── iam-role/terragrunt.hcl
├── live/
│   ├── dev/
│   │   └── us-west-2/
│   │       └── terragrunt.stack.hcl    # 声明 dev 要哪些 unit
│   ├── staging/
│   │   ├── us-west-2/terragrunt.stack.hcl
│   │   └── us-east-1/terragrunt.stack.hcl
│   └── prod/
│       ├── us-west-2/terragrunt.stack.hcl
│       ├── us-east-1/terragrunt.stack.hcl
│       └── eu-west-1/terragrunt.stack.hcl
└── modules/                        # Terraform 模块
    ├── vpc/
    ├── eks/
    └── rds/
```

关键布局：

- **`catalog/units/`**：unit 模板，定义"这个 unit 怎么调 modules"。一次写，N 次用。
- **`modules/`**：真正的 Terraform 模块。Terragrunt 不接管，由 `catalog/units/` 里的 `source` 引用。
- **`live/`**：环境声明。每个环境 * region 一个 `terragrunt.stack.hcl`。
- **`_env/` 和 `_region/`**：共享配置，通过 `include` 引入。

分层清晰：
- **modules** = "怎么建一个 VPC"
- **catalog/units** = "VPC unit 需要哪些参数、依赖什么"
- **live stack** = "prod 环境的 us-west-2 要建一个 VPC"

## 团队协作模式

### 本地：terragrunt run-all plan

开发在本地写改动，`run-all plan` 看影响：

```bash
cd live/prod/us-west-2
terragrunt run-all plan -out=/tmp/plans
```

把 plan 文件存起来，review 过后才 apply。

### CI：Atlantis 或 Spacelift

Terragrunt 的 CI 最常见是配合 **Atlantis**。Atlantis 是一个 PR 机器人，监听 PR 事件，自动跑 `terragrunt plan`，把 output 贴到 PR 评论里：

```yaml
# atlantis.yaml
version: 3
projects:
  - name: prod-vpc
    dir: live/prod/us-west-2/vpc
    terraform_version: v1.9.8
    autoplan:
      when_modified:
        - "*.hcl"
        - "../../../../modules/vpc/**"
    apply_requirements:
      - approved
      - mergeable
```

审核通过后在 PR 里评论 `atlantis apply`，Atlantis 自动跑 apply。

更成熟的选择是 **Spacelift**，它原生支持 Terragrunt，有可视化 UI、drift detection、policy 集成。

### Drift Detection

Terragrunt 本身不做 drift detection，需要额外工具。我们的做法：

- **每天凌晨定时跑 `run-all plan`**，plan 结果和上次比较
- **差异发钉钉**：如果有资源漂移，alert 给运维
- 漂移常见原因：手动在 console 改、非 Terragrunt 管理的工具（比如 ASG 自动调容量）

## 落地踩坑

### 坑 1：.terragrunt-cache 占盘

Terragrunt 每次 run 会在每个 unit 下创建 `.terragrunt-cache/` 目录，下载 source、保存 plan。一个大型项目这个目录可能占 10+ GB。

清理：

```bash
find . -type d -name .terragrunt-cache -exec rm -rf {} +
```

CI 里每次运行后主动清理。

### 坑 2：dependency mock 写错导致生产事故

mock_outputs 的目的是让 plan 能跑，但如果你不小心在 apply 时也用了 mock（误配 `mock_outputs_allowed_terraform_commands`），生产会应用 mock 值——比如 `subnet_ids = ["subnet-mock"]` 就真去创建一个不存在的 subnet 的 EKS，直接失败或更糟。

规则：**`mock_outputs_allowed_terraform_commands` 只列 `["validate", "plan"]`，永远不要加 `apply`**。

### 坑 3：run-all 的 dependency 跨目录问题

`dependency "../vpc"` 引用相对路径。如果你重构目录结构，所有依赖都得改。这是 Terragrunt 1.0 之前的痛点。

Stacks 模式下依赖是通过 `values` 传入 unit 的，改目录不影响依赖表达。这是 Stacks 带来的隐性好处。

### 坑 4：Terragrunt 版本升级破坏兼容

Terragrunt 历史上做过几次小的 breaking change（比如 `dependency` block 语义调整）。生产上**锁定 Terragrunt 版本**，升级前在 staging 跑全量 plan：

```yaml
# .terragrunt-version
v0.78.2
```

配合 `tgenv` 或 `asdf` 自动切换版本。

### 坑 5：run-all 并发下的 state lock 冲突

两个 unit 同时对同一个 DynamoDB lock table 竞争，可能 deadlock。大量 unit 并发时偶尔看到 `Error locking state`。

缓解：降低 `--terragrunt-parallelism`，或给每个环境独立的 lock table。

## 什么时候不用 Terragrunt

- **State 少于 10 个**：直接写 Terraform 更简单
- **团队不熟 HCL**：学曲线叠加，引入新工具增加心智负担
- **完全 Pulumi 体系**：Pulumi 有自己的 Stack + Component，Terragrunt 管不了

Terragrunt 的甜蜜区是：**"已经深度用 Terraform/OpenTofu + state 超过 20 个 + 有多环境/多 region 管理需求"**。小于这个规模纯 Terraform 就够。

## 结语

Terragrunt 不是替代 Terraform，是规模化场景下的 wrapper。我们接手的时候 state 已经 50+，没 Terragrunt 没法过。它真正的价值是把几件事自动化了：

- DRY 的 backend 和 provider
- 跨 state 依赖声明
- 批量 plan/apply
- unit 模板化和 on-demand 生成

Stacks 2025 年 GA 把 multi-region 资源模板化这个长期痛点解掉了。如果你已经在用 TF/OpenTofu 且 state 超过 20 个，就上 Terragrunt 1.0+。迁移是增量的，先套一个目录试水，风险很小。

Sources:
- [Terragrunt Stacks docs](https://terragrunt.gruntwork.io/docs/features/stacks)
- [Terragrunt 1.0 Stacks GA blog - Gruntwork](https://www.gruntwork.io/blog/the-road-to-terragrunt-1-0-stacks)
- [Terragrunt Tutorial - Scalr](https://scalr.com/learning-center/beginners-guide-to-terragrunt/)
- [Why Terragrunt over Terraform 2025 - MLOps Community](https://home.mlops.community/public/blogs/why-i-use-terragrunt-over-terraformopentofu-in-2025)
- [Terragrunt multi-region multi-account](https://ervinszilagyi.dev/articles/terragrunt-for-multi-region-multi-account-deployments.html)
