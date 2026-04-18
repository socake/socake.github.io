---
title: "Pulumi vs Terraform vs OpenTofu：2026 年 IaC 选型深度对比"
date: 2026-02-09T10:00:00+08:00
draft: false
tags: ["Pulumi", "Terraform", "OpenTofu", "IaC", "基础设施"]
categories: ["基础设施"]
description: "Terraform BSL 化之后 OpenTofu 接管了开源生态，Pulumi 用 Go/Python/TypeScript 写基础设施的理念也日渐成熟。本文从语言、State、Provider、Testing、团队协作五个维度深度对比这三个 IaC 工具，给出不同规模团队的选型建议。"
summary: "2023 年之后 IaC 世界变了：HashiCorp 把 Terraform 改成 BSL，Linux Foundation 接管了 OpenTofu。Pulumi 依然在代码式 IaC 的路上坚持。团队选型时面对的不是 Terraform 一家独大，而是三条技术路线的真实对比。本文试图给出一个不偏不倚的答案。"
toc: true
math: false
diagram: true
keywords: ["Pulumi", "Terraform", "OpenTofu", "IaC", "Infrastructure as Code"]
params:
  reading_time: true
---

## 2023 年之后的 IaC 格局

如果你在 2022 年之前问"IaC 选什么"，答案几乎是反射性的："Terraform。没别的。"

2023 年 8 月，HashiCorp 把 Terraform 从 MPL 2.0 换成 BSL (Business Source License)。这个许可证禁止第三方做"竞争产品"，一石激起千层浪：

- 多个供应商（Spacelift、env0、scalr、Harness）直接受影响
- Linux Foundation 接手社区 fork，成立 **OpenTofu** 项目
- Pulumi（早就走代码式 IaC 路线）获得大量关注流量

到 2026 年，三者的真实状态：

| 维度 | Terraform | OpenTofu | Pulumi |
|------|-----------|----------|--------|
| 开源许可证 | BSL (非 OSI 认证) | MPL 2.0 (OSI 认证) | Apache 2.0 |
| 语言 | HCL | HCL (兼容) | Go/Python/TS/JS/C#/Java/YAML |
| Provider 生态 | 最大 | 兼容 Terraform provider | 包装 TF provider + 原生 |
| 托管服务 | HashiCorp Cloud | 无（社区自建）| Pulumi Cloud |
| State 后端 | S3/Azure/GCS/HashiCorp | 同 Terraform | Pulumi Cloud / S3 / 其它 |
| Registry | registry.terraform.io | registry.opentofu.org | registry.pulumi.com |

这三个不是零和博弈，真实场景下它们服务不同需求。本文试图给出一个相对公允的深度对比。

## 核心哲学差异

### Terraform / OpenTofu：声明式 DSL

HCL 是一个专门为描述基础设施设计的 DSL：

```hcl
# main.tf
terraform {
  required_version = ">= 1.9"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.80"
    }
  }
  backend "s3" {
    bucket         = "my-tfstate"
    key            = "prod/vpc.tfstate"
    region         = "us-west-2"
    dynamodb_table = "tf-locks"
  }
}

provider "aws" {
  region = "us-west-2"
}

variable "environment" {
  type    = string
  default = "prod"
}

locals {
  common_tags = {
    Environment = var.environment
    ManagedBy   = "terraform"
  }
}

resource "aws_vpc" "main" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_hostnames = true

  tags = merge(local.common_tags, {
    Name = "${var.environment}-vpc"
  })
}

resource "aws_subnet" "private" {
  for_each = toset(["a", "b", "c"])

  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(aws_vpc.main.cidr_block, 8, index(["a", "b", "c"], each.value) + 10)
  availability_zone = "us-west-2${each.value}"

  tags = merge(local.common_tags, {
    Name = "${var.environment}-private-${each.value}"
    Tier = "private"
  })
}
```

HCL 的设计哲学是：**配置语言，不是编程语言**。它的 control flow 有限：`count`、`for_each`、`dynamic` 是核心，没有函数定义（只有内置函数），没有 class，没有 loop，没有 exception。

这个限制是**有意的**。HashiCorp 的理念是："基础设施描述应该是声明式的，不应该让你写任意代码。"

优点：

- 配置**易读**：新同事看一眼就知道这段在干啥
- **无副作用**：同一份 HCL 跑出同样的 plan
- **易审计**：code review 时能直接看出 diff 的意图
- **工具化好**：`terraform fmt`、`tflint`、`checkov`、`tfsec` 工具链成熟

缺点：

- **复用难**：`module` 是唯一抽象手段，嵌套深了难以维护
- **逻辑表达弱**：条件判断、循环、字符串处理都是绕着弯
- **没有类型**：HCL 是弱类型的，错误只在 `terraform apply` 时才暴露
- **不能测试**：官方 `terraform test` 是 2023 年才加的，生态薄弱

### Pulumi：代码式 IaC

Pulumi 的哲学完全相反：**用通用编程语言描述基础设施，获得所有编程语言的好处（类型、测试、复用、IDE 支持）**。

同样的 VPC 用 TypeScript：

```typescript
// index.ts
import * as pulumi from "@pulumi/pulumi";
import * as aws from "@pulumi/aws";

const config = new pulumi.Config();
const environment = config.require("environment");

const commonTags = {
  Environment: environment,
  ManagedBy: "pulumi",
};

const vpc = new aws.ec2.Vpc("main", {
  cidrBlock: "10.0.0.0/16",
  enableDnsHostnames: true,
  tags: { ...commonTags, Name: `${environment}-vpc` },
});

const azs = ["a", "b", "c"];
const privateSubnets = azs.map((az, idx) => {
  return new aws.ec2.Subnet(`private-${az}`, {
    vpcId: vpc.id,
    cidrBlock: pulumi.interpolate`10.0.${10 + idx}.0/24`,
    availabilityZone: `us-west-2${az}`,
    tags: {
      ...commonTags,
      Name: `${environment}-private-${az}`,
      Tier: "private",
    },
  });
});

export const vpcId = vpc.id;
export const privateSubnetIds = privateSubnets.map(s => s.id);
```

或 Go：

```go
package main

import (
    "fmt"

    "github.com/pulumi/pulumi-aws/sdk/v6/go/aws/ec2"
    "github.com/pulumi/pulumi/sdk/v3/go/pulumi"
    "github.com/pulumi/pulumi/sdk/v3/go/pulumi/config"
)

func main() {
    pulumi.Run(func(ctx *pulumi.Context) error {
        cfg := config.New(ctx, "")
        env := cfg.Require("environment")

        commonTags := pulumi.StringMap{
            "Environment": pulumi.String(env),
            "ManagedBy":   pulumi.String("pulumi"),
        }

        vpc, err := ec2.NewVpc(ctx, "main", &ec2.VpcArgs{
            CidrBlock:          pulumi.String("10.0.0.0/16"),
            EnableDnsHostnames: pulumi.Bool(true),
            Tags:               addTag(commonTags, "Name", env+"-vpc"),
        })
        if err != nil {
            return err
        }

        azs := []string{"a", "b", "c"}
        for i, az := range azs {
            _, err := ec2.NewSubnet(ctx, fmt.Sprintf("private-%s", az), &ec2.SubnetArgs{
                VpcId:            vpc.ID(),
                CidrBlock:        pulumi.String(fmt.Sprintf("10.0.%d.0/24", i+10)),
                AvailabilityZone: pulumi.String("us-west-2" + az),
                Tags: addTag(commonTags, "Name",
                    fmt.Sprintf("%s-private-%s", env, az)),
            })
            if err != nil {
                return err
            }
        }

        ctx.Export("vpcId", vpc.ID())
        return nil
    })
}
```

优点：

- **IDE 补全**：写 `vpc.` 有所有属性提示
- **类型检查**：编译期发现大部分错误
- **复用强**：函数、class、npm/pypi/go module
- **单元测试**：可以 mock provider，纯单测业务逻辑
- **一致性**：和应用代码同一种语言，没有上下文切换

缺点：

- **代码 vs 配置**：复杂逻辑写嗨了容易失控，"过度抽象"风险
- **阅读成本高**：不熟悉 Pulumi 的人看代码要先理解 SDK
- **diff 难看**：`pulumi up` 的 plan 不如 `terraform plan` 直观
- **学习 Pulumi SDK 概念**：Output/Input/Apply 是 Pulumi 独有的异步模型

### 关键差异：Input/Output 异步模型

Pulumi 最难理解的一点：**资源的属性是异步的**。`vpc.id` 不是一个字符串，是 `pulumi.Output<string>`。你不能直接 `console.log(vpc.id)`，要 `vpc.id.apply(id => console.log(id))`。

```typescript
// 错误：id 不是字符串，是 Output<string>
const name = `subnet-${vpc.id}`;  // TypeScript 不报错，但运行时这里是对象拼接，出的 name 是垃圾

// 正确：用 pulumi.interpolate 处理 Output
const name = pulumi.interpolate`subnet-${vpc.id}`;

// 或用 apply
const name = vpc.id.apply(id => `subnet-${id}`);
```

这个概念刚开始非常反直觉。理解了它，Pulumi 就用得舒服；理解不了，就会一直写 bug。Terraform 没这个问题（HCL 的 interpolation 是自动处理的）。

## OpenTofu：Terraform 的开源续命

OpenTofu 是 Terraform 1.6 的 fork，起始点完全兼容。2024 年之后两者开始分叉，OpenTofu 加了一些 Terraform 没有或滞后的特性：

### OpenTofu 独有特性

**1. Early Variable Evaluation (1.8+)**

Terraform 里你不能在 `module` 块里用变量：

```hcl
module "network" {
  source = "./modules/${var.environment}-network"  # Terraform 报错
}
```

OpenTofu 允许：

```hcl
module "network" {
  source = "./modules/${var.environment}-network"  # OK
}
```

这个看着小，实际项目里经常救命。

**2. State Encryption (1.7+)**

OpenTofu 原生支持对 state 文件加密（AES / PGP / KMS）：

```hcl
terraform {
  encryption {
    key_provider "aws_kms" "key" {
      kms_key_id = "arn:aws:kms:us-west-2:1234:key/..."
      region     = "us-west-2"
    }
    method "aes_gcm" "standard" {
      keys = key_provider.aws_kms.key
    }
    state {
      method   = method.aes_gcm.standard
      enforced = true
    }
  }
}
```

Terraform 的 state 加密只能靠 backend（S3 SSE）实现，颗粒度更粗。

**3. Provider iteration (`for_each` on providers)**

多 region 部署时，Terraform 要写 N 个 provider alias，OpenTofu 可以：

```hcl
provider "aws" {
  for_each = toset(["us-west-2", "us-east-1", "eu-west-1"])
  alias    = "by_region"
  region   = each.value
}
```

Terraform 至今不支持。

### OpenTofu 的风险

**1. Registry 分裂**：大部分 provider 还是发在 `registry.terraform.io`，OpenTofu 用 mirror 访问。未来如果 HashiCorp 限制 OpenTofu 访问，可能要完全重建 registry。

**2. 生态迁移速度**：新 provider 默认在 Terraform 先发，OpenTofu 后跟进。

**3. 商业支持**：Terraform 有 HashiCorp 做企业支持和咨询，OpenTofu 是社区驱动，企业支持靠第三方（Spacelift、env0 等）。

**4. 和 Terraform 的兼容性会慢慢裂开**：OpenTofu 1.8+ 的新特性 Terraform 没有，混用两者的项目会有 diff。

## State 管理对比

State 是 IaC 最重要也最容易出问题的部分。

### Terraform / OpenTofu State

State 是一个 JSON 文件，记录所有 resource 的当前属性和 dependency graph。

```json
{
  "version": 4,
  "terraform_version": "1.9.8",
  "serial": 123,
  "resources": [
    {
      "type": "aws_vpc",
      "name": "main",
      "provider": "provider[\"registry.terraform.io/hashicorp/aws\"]",
      "instances": [{
        "attributes": {
          "id": "vpc-abc123",
          "cidr_block": "10.0.0.0/16",
          ...
        }
      }]
    }
  ]
}
```

后端选择：

- **S3 + DynamoDB lock**：最主流，完全受控，便宜
- **Terraform Cloud / HCP Terraform**：托管，带 UI/RBAC/审计
- **Azure Storage / GCS**：云原生
- **HTTP backend**：自建 (Atlantis、scalr)
- **本地文件**：只用于实验

生产上我们几乎都用 **S3 + DynamoDB**，因为：
- 完全掌握数据
- 成本低（一个 bucket 一张表）
- 支持 versioning 做 rollback
- 和现有 AWS 权限体系打通

### Pulumi State

Pulumi 的 state 叫 **stack state**，功能上等价于 Terraform state，但设计有差：

1. 默认后端是 **Pulumi Cloud**（商业 SaaS），有免费额度
2. 自建后端支持 S3、Azure、GCS、本地
3. State 文件格式稍简单（也是 JSON）

```bash
# 用 S3 作为后端
pulumi login s3://my-pulumi-state

# 用本地文件
pulumi login file://~/.pulumi
```

**Pulumi Cloud 是 Pulumi 商业模式的核心**。它提供：
- Web UI 看 stack
- RBAC 和审计
- Policy as code (CrossGuard)
- Secret 加密
- 多人协作锁

免费档有 5 个 stack，够个人和小团队。生产用建议评估 self-host vs 付费。

### State 大小和性能

我的实际体验：

| 指标 | Terraform/OpenTofu | Pulumi |
|------|---------------------|--------|
| 1000 资源 state 大小 | ~5-10 MB JSON | ~3-8 MB JSON |
| plan 时间 | 10-30 秒 | 15-40 秒 |
| refresh 时间 | 2-5 分钟 | 2-5 分钟 |

两者在大型 state 上性能相近，都会随着资源数量线性变慢。我们见过 3000+ 资源的 Terraform state，`plan` 需要 2 分钟。超过这个规模就该考虑把 state 拆分（`terragrunt` 的核心用途，我们另一篇讲）。

## Provider 生态

### Terraform Provider

Terraform 有 **3000+ 官方/社区 provider**，几乎涵盖所有公有云和 SaaS：AWS、GCP、Azure、阿里云、Cloudflare、Datadog、GitHub、Kubernetes、Grafana、Vault……

这是 Terraform 最大的护城河。任何新服务上线，第一件事就是写 Terraform provider。

### OpenTofu Provider

因为 OpenTofu 是 Terraform fork，**所有 Terraform provider 都可以在 OpenTofu 里用**。你 `tofu init` 时它从 `registry.opentofu.org` 拉 provider，registry 本身是一个 proxy，backing store 还是 Terraform 的 provider binary。

实际使用感觉 99% 一致。少数新 provider 发布时有 1-2 天延迟。

### Pulumi Provider

Pulumi 有三类 provider：

1. **Native provider**：Pulumi 团队自己写（AWS、Azure、GCP、Kubernetes），质量高，跟进云厂商新功能快
2. **Bridge provider**：自动从 Terraform provider 生成（`pulumi-terraform-bridge`），大部分生态 provider 走这条
3. **Dynamic provider**：用户自己写的自定义资源

日常使用体感：AWS、K8s 用 native provider 体验优秀，冷门服务用 bridge provider 质量略差（error message、文档都欠一点）。

## 测试能力

### Terraform test

Terraform 1.6 引入了 `terraform test`，用 HCL 写测试：

```hcl
# tests/vpc.tftest.hcl
run "create_vpc" {
  command = plan

  variables {
    environment = "test"
  }

  assert {
    condition     = aws_vpc.main.cidr_block == "10.0.0.0/16"
    error_message = "VPC CIDR block incorrect"
  }
}
```

缺点：表达力有限，只能对 `plan`/`apply` 后的状态断言。复杂逻辑测不了。

### Pulumi test

Pulumi 用通用语言，天然支持单测。Jest / pytest / go test 都能用：

```typescript
// vpc.test.ts
import * as pulumi from "@pulumi/pulumi";
import "jest";

pulumi.runtime.setMocks({
  newResource: (args) => ({
    id: `${args.name}-id`,
    state: args.inputs,
  }),
  call: () => ({}),
});

describe("VPC", () => {
  let infra: typeof import("./index");

  beforeAll(async () => {
    infra = await import("./index");
  });

  it("creates VPC with correct CIDR", async () => {
    const vpc = await infra.vpc;
    expect(vpc.cidrBlock).resolves.toBe("10.0.0.0/16");
  });

  it("creates 3 private subnets", async () => {
    expect(await infra.privateSubnetIds).toHaveLength(3);
  });
});
```

这是 Pulumi 相比 Terraform 最大的技术优势。你可以 mock provider，纯单测业务逻辑，跑一次几秒钟，不需要任何云账号。

## Policy as Code

### Terraform: Sentinel / OPA

Terraform Cloud 用 Sentinel (HashiCorp 专有语言) 做 Policy。开源社区用 **Conftest / OPA** 对 `terraform show -json` 做检查：

```rego
# policy/required_tags.rego
package terraform.tags

deny[msg] {
    resource := input.resource_changes[_]
    resource.type == "aws_vpc"
    not resource.change.after.tags.Environment
    msg := sprintf("VPC %s missing Environment tag", [resource.address])
}
```

### OpenTofu: 同 Terraform

Sentinel 不开源，OpenTofu 用户走 OPA/Conftest 是唯一选择。

### Pulumi: CrossGuard

Pulumi 有自带的 policy 框架 **CrossGuard**，用同样的语言（TypeScript/Python）写 policy：

```typescript
import * as aws from "@pulumi/aws";
import { PolicyPack, validateResourceOfType } from "@pulumi/policy";

new PolicyPack("company-policy", {
  policies: [{
    name: "require-environment-tag",
    description: "All VPCs must have Environment tag",
    enforcementLevel: "mandatory",
    validateResource: validateResourceOfType(aws.ec2.Vpc, (vpc, args, reportViolation) => {
      if (!vpc.tags?.Environment) {
        reportViolation("VPC missing Environment tag");
      }
    }),
  }],
});
```

用 `pulumi up --policy-pack ./policy-pack` 强制应用。可读性和 Sentinel 相比好很多。

## 团队协作模式

### Terraform / OpenTofu

主流协作模式：

1. **Atlantis / env0 / Spacelift**：在 PR 里自动跑 `terraform plan`，人工批准后 apply
2. **Terragrunt**：管理多 state 的 wrapper，大规模项目必备
3. **Module registry**：内部 Git repo 或 Terraform Cloud Registry

成熟度最高，工具链最丰富。

### Pulumi

主流模式：

1. **Pulumi Cloud**：PR 集成、Preview、Deploy webhook
2. **`pulumi up --yes` in CI**：自动化部署
3. **Component Resource**：Pulumi 的"模块"概念，用 class 封装

Pulumi 的团队协作工具不如 Terraform 丰富，但 Pulumi Cloud 本身足够好用。

## 我的选型建议

### 场景 1：50 人以下团队、早期创业

**推荐 OpenTofu**。理由：
- 生态最成熟，解决 80% 问题
- 开源许可证清晰，不担心未来被收费
- 招人容易（大部分运维懂 Terraform/OpenTofu）
- HCL 可读性高，code review 效率高

### 场景 2：50-500 人中型公司，已有 Terraform

**继续 Terraform 或迁到 OpenTofu**。理由：
- 迁移 Pulumi 成本巨大（要重写所有代码）
- 团队已掌握 HCL，生态顺手
- 如果在意许可证 + 想要 state encryption 等新特性，迁 OpenTofu 很容易

迁移方式：改 CLI 名字（`tofu init` 替代 `terraform init`），大部分项目开箱即用。

### 场景 3：新项目、团队有强编程能力

**推荐 Pulumi**。理由：
- 有类型、有测试、有 IDE，开发体验最好
- 一种语言（TypeScript/Go）统一应用和基础设施
- 复杂逻辑表达力强

条件：团队真的能投入时间学 Pulumi SDK 概念（Input/Output）。否则强行上 Pulumi 会变成"会写代码但不懂云"的人在写屎山。

### 场景 4：K8s 为主的平台

**Pulumi 或 Crossplane 都考虑**。Pulumi 的 K8s provider 体验优秀（TS 补全 K8s manifest），Crossplane 则是"K8s 里管 K8s 外资源"的另一种哲学。

### 场景 5：纯多云、跨厂商一致性优先

**OpenTofu**。Pulumi 和 Terraform 都支持多云，但 OpenTofu 的 provider 生态最全、社区最稳定，适合那种"同时在 AWS、阿里云、GCP、本地 OpenStack 上跑"的企业。

## 最后怎么选

Terraform 一家独大的时代过去了。OpenTofu 接住了开源那部分，Terraform 继续做 HashiCorp 的商业产品，Pulumi 始终是代码式 IaC 的小众但坚挺的路线。三个都不会消失，选型没有"哪个最好"，只有"哪个最适合你团队"。我一般按这个顺序判断：

1. **团队已有技能栈** → 有 Terraform 经验继续用 Terraform/OpenTofu，有强编程能力选 Pulumi
2. **许可证要求** → 严格开源必须 OpenTofu
3. **生态依赖** → 用 HashiCorp 一整套（Vault、Consul、Nomad）选 Terraform
4. **可测试性需求** → 强需求选 Pulumi
5. **团队规模** → 大团队推荐 OpenTofu（生态稳定）

不管选哪个，核心实践都一样：**state 远端存储 + lock + PR 驱动的 plan + 强制 policy 检查**。工具只是载体，流程才是关键。

Sources:
- [Pulumi vs OpenTofu comparison](https://www.pulumi.com/docs/iac/comparisons/opentofu/)
- [Pulumi vs Terraform - Spacelift](https://spacelift.io/blog/pulumi-vs-terraform)
- [Terraform vs Pulumi vs OpenTofu 2026 - EITT](https://eitt.academy/knowledge-base/terraform-vs-pulumi-vs-opentofu-iac-comparison-2026/)
- [IaC 2026 comparison - dasroot](https://dasroot.net/posts/2026/01/infrastructure-as-code-terraform-opentofu-pulumi-comparison-2026/)
- [How To Choose IaC Tool - OpenSourceForU](https://www.opensourceforu.com/2025/10/how-to-choose-between-terraform-pulumi-and-opentofu/)
