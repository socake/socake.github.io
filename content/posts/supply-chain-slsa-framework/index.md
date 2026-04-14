---
title: "SLSA 软件供应链等级实施：从 L1 到 L3 的工程化路径"
date: 2025-12-05T10:00:00+08:00
draft: false
tags: ["SLSA", "供应链安全", "Provenance", "DevSecOps", "零信任"]
categories: ["DevSecOps"]
description: "一份 SLSA v1.0 框架的实战落地笔记：讲清楚 Build Track 从 L1 到 L3 的具体要求、用 GitHub Actions 官方 generator 和 Tekton Chains 生成 provenance、用 slsa-verifier 和 Kyverno 做验证、以及和前面 Sigstore/Kyverno/Cosign 的整合。"
summary: "一份 SLSA v1.0 框架的实战落地笔记：讲清楚 Build Track 从 L1 到 L3 的具体要求、用 GitHub Actions 官方 generator 和 Tekton Chains 生成 provenance、用 slsa-verifier 和 Kyverno 做验证、以及和前面 Sigstore/Kyverno/Cosign 的整合。"
toc: true
math: false
diagram: false
keywords: ["SLSA", "provenance", "软件供应链", "Build Track", "Sigstore"]
params:
  reading_time: true
---

## 为什么要谈 SLSA

2020 年 SolarWinds 事件之后，整个软件供应链安全的叙事方式都变了。之前大家聊的是"**如何让我写的代码更安全**"，之后大家意识到更大的威胁是"**如何确保交付物真的是我写的代码**"。中间所有环节——源码仓库、构建系统、测试、发布、分发、部署——任何一步被污染都可能让你的签名代码被替换。SolarWinds 的攻击者就是在构建系统里插入了恶意代码，生成出来的二进制在客户眼里完全合法，签名也是正确的，因为就是 SolarWinds 的合法构建管道签的。

SLSA（Supply-chain Levels for Software Artifacts，读作"salsa"）是 Google 2021 年发起、后来转给 OpenSSF 托管的一套**供应链安全等级框架**。它不是工具，而是一套"你的供应链达到什么等级"的评估标准和实施指南。v1.0 在 2023 年发布，到 2025 年已经被主流云厂商、Linux 发行版、企业安全团队广泛采用。

这篇文章是我在生产环境落地 SLSA 的经验，也是整个零信任系列的收尾篇。我会把 SLSA 的 Build Track 从 L1 到 L3 的**实际工程路径**一条条讲清楚，并把前面九篇文章里的 Sigstore、Cosign、Kyverno、SBOM 等工具串起来。

## 一、SLSA v1.0 框架速览

### 1.1 Tracks 的概念

SLSA v1.0 最大的变化是引入了 "**Tracks**"：把原来一套笼统的 Level 拆成多个独立的纬度。目前定义了：

- **Build Track**：构建过程的完整性（最核心，优先落地）
- **Source Track**：源码管理的完整性（v1.1 草案中）
- **Dependencies Track**：依赖审核（规划中）

目前绝大部分生产实现只关注 Build Track，这篇文章也主要讲 Build Track。

### 1.2 Build Track 的等级

| Level | 简述 | 核心要求 |
|-------|------|---------|
| **L0** | 无保障 | 没有任何供应链信号 |
| **L1** | 有 provenance | 构建过程输出 provenance，说明"我是怎么构建的" |
| **L2** | 托管构建服务 | 构建在受信任的托管服务上，provenance 由构建服务签名 |
| **L3** | 隔离与可验证 | 构建作业之间相互隔离，provenance 不可伪造 |
| **L4** | 规划中 | (v1.0 未定义，v1.1 草案) |

核心概念是 **provenance**——一份描述"这个制品是怎么来的"的结构化声明，格式是 in-toto 的 [SLSA Provenance Predicate](https://slsa.dev/provenance/v1)。典型字段：

- `buildType`: 用什么构建工具和流程
- `builder.id`: 谁在构建（比如 GitHub Actions 的 workflow ref）
- `invocation.configSource`: 源码 commit hash 和仓库 URL
- `invocation.parameters`: 构建参数
- `materials`: 所有输入（依赖包、base image 等）
- `buildStartedOn` / `buildFinishedOn`

有了 provenance，一个可信的消费者可以验证："这个镜像确实是从我们的 main 分支 commit abc123 通过 build.yml workflow 构建的，构建时间是 X，输入依赖是 Y。" 任何一步被篡改都会被发现。

### 1.3 Provenance 不等于签名

这是初学者最容易混淆的点。签名（Cosign）证明"这个制品被某人签名过"，provenance 证明"这个制品是怎么来的"。两者是互补关系：

- 签名只说明"**来源可信**"
- Provenance 说明"**来源可信 + 过程透明**"

SLSA 要求 provenance 本身**被签名**（通常用 Sigstore/DSSE 格式），形成"可验证的构建声明"。所以 SLSA 实施通常是 Provenance + Sigstore 组合，不是二选一。

## 二、SLSA L1：最基础的 provenance 生成

L1 的要求最简单：**构建过程输出 provenance，provenance 至少描述基本信息**。允许 provenance 由构建脚本自己生成、不强制签名、允许 provenance 伪造。

这个级别的价值主要是"**让团队习惯 provenance 的存在**"，为 L2/L3 打基础。

### 2.1 手写 L1 provenance

一个最简单的 L1 provenance 示例（JSON）：

```json
{
  "_type": "https://in-toto.io/Statement/v1",
  "subject": [
    {
      "name": "ghcr.io/myorg/myapp",
      "digest": {
        "sha256": "abc123...."
      }
    }
  ],
  "predicateType": "https://slsa.dev/provenance/v1",
  "predicate": {
    "buildDefinition": {
      "buildType": "https://github.com/actions/workflow/v1",
      "externalParameters": {
        "workflow": {
          "ref": "refs/heads/main",
          "repository": "https://github.com/myorg/myrepo",
          "path": ".github/workflows/build.yml"
        }
      },
      "resolvedDependencies": [
        {
          "uri": "git+https://github.com/myorg/myrepo",
          "digest": { "gitCommit": "abcdef1234" }
        }
      ]
    },
    "runDetails": {
      "builder": {
        "id": "https://github.com/actions/runner"
      },
      "metadata": {
        "invocationId": "1234567",
        "startedOn": "2025-10-15T08:00:00Z",
        "finishedOn": "2025-10-15T08:05:00Z"
      }
    }
  }
}
```

这样的 JSON 可以用 `cosign attest` 挂到镜像上：

```bash
cosign attest --predicate provenance.json \
              --type slsaprovenance1 \
              ghcr.io/myorg/myapp@sha256:abc123...
```

**L1 的局限**：因为构建脚本自己写 provenance，攻击者能伪造任何内容。例如攻击者构建一个恶意镜像，然后写一份假 provenance 声称自己来自 main 分支。

要防这种伪造必须升级到 L2。

## 三、SLSA L2：可信的构建服务

L2 要求：
1. 使用**托管构建服务**（GitHub Actions、GitLab CI、Cloud Build、Tekton 等）
2. Provenance 由**构建服务自身**生成（不是用户的构建脚本）
3. Provenance 被构建服务**签名**
4. Source 和 build 服务提供"来自何处"的验证

GitHub 在 Actions 里原生支持生成 SLSA L2 provenance（通过 [slsa-github-generator](https://github.com/slsa-framework/slsa-github-generator)）。关键是生成器**运行在 GitHub 的 reusable workflow 里**，构建脚本本身不能污染它。

### 3.1 GitHub Actions L2 实现

```yaml
name: ci

on:
  push:
    tags: [ 'v*' ]

permissions: {}

jobs:
  build:
    permissions:
      id-token: write
      contents: read
      packages: write
    runs-on: ubuntu-22.04
    outputs:
      image: ${{ steps.meta.outputs.image }}
      digest: ${{ steps.push.outputs.digest }}
    steps:
      - uses: actions/checkout@v4

      - id: meta
        run: |
          echo "image=ghcr.io/${{ github.repository }}" >> "$GITHUB_OUTPUT"

      - uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - id: push
        uses: docker/build-push-action@v6
        with:
          context: .
          push: true
          tags: ${{ steps.meta.outputs.image }}:${{ github.sha }}
          provenance: false   # 用 SLSA generator 生成，而不是 buildx 自带

  provenance:
    needs: build
    permissions:
      id-token: write
      packages: write
      actions: read
    uses: slsa-framework/slsa-github-generator/.github/workflows/generator_container_slsa3.yml@v2.0.0
    with:
      image: ${{ needs.build.outputs.image }}
      digest: ${{ needs.build.outputs.digest }}
      registry-username: ${{ github.actor }}
    secrets:
      registry-password: ${{ secrets.GITHUB_TOKEN }}
```

**注意这里用的是 `generator_container_slsa3.yml`**——GitHub Actions 官方的生成器其实能直接产出**L3 级别**的 provenance。它被实现成 reusable workflow，生成过程运行在一个独立的 ephemeral runner 上，和主 build job 隔离，这个隔离就是 L3 的关键。

流程：
1. `build` job 负责构建和推镜像
2. `provenance` job 调用官方 generator workflow，**不执行用户的任何脚本**
3. generator 读取 build job 的 outputs（不可篡改）
4. generator 生成 SLSA v1.0 provenance，用 Sigstore keyless 签名
5. generator 把 provenance attestation 推到 registry

用户构建脚本无法影响 provenance 内容，这是 L3 级别的关键特性。

### 3.2 Tekton Chains 实现

如果你不用 GitHub Actions，Tekton Chains 是同等级别的 Tekton Pipeline 选项。

```yaml
apiVersion: tekton.dev/v1beta1
kind: Pipeline
metadata:
  name: build-and-attest
spec:
  tasks:
    - name: build
      taskRef:
        name: kaniko
      params:
        - name: IMAGE
          value: ghcr.io/myorg/myapp
```

然后启用 Chains controller：

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: chains-config
  namespace: tekton-chains
data:
  artifacts.oci.format: "slsa/v2"
  artifacts.oci.storage: "oci"
  artifacts.oci.signer: "x509"
  transparency.enabled: "true"
  transparency.url: "https://rekor.sigstore.dev"
```

Tekton Chains 会 watch 所有 PipelineRun，PipelineRun 结束后自动生成 provenance，签名后推到 OCI registry。和 GHA 相比，Tekton Chains 的优势是**可以在私有集群跑**，不依赖 GitHub 的托管 runner。

### 3.3 GitLab CI 实现

GitLab 17.0+ 原生支持 SLSA provenance 生成：

```yaml
build:
  stage: build
  image: docker:27
  services:
    - docker:27-dind
  id_tokens:
    SIGSTORE_ID_TOKEN:
      aud: sigstore
  script:
    - docker build -t $CI_REGISTRY_IMAGE:$CI_COMMIT_SHA .
    - docker push $CI_REGISTRY_IMAGE:$CI_COMMIT_SHA
    - cosign attest --predicate provenance.json --type slsaprovenance1 \
                    $CI_REGISTRY_IMAGE:$CI_COMMIT_SHA
  artifacts:
    reports:
      cyclonedx: sbom.json
```

GitLab 目前的 provenance 只是 L2 级别，没做到完整 L3 隔离。但对大多数场景够用。

## 四、SLSA L3：隔离和不可伪造

L3 在 L2 基础上增加两个硬要求：

1. **Build 过程隔离**：不同构建任务之间不能互相影响（内存、磁盘、网络）
2. **Provenance 不可伪造**：用户的构建脚本无法改变 provenance 内容

GitHub Actions 的官方 generator (`slsa-github-generator`) 达到 L3 的方式：

- Provenance 生成在一个**独立的 reusable workflow** 里
- 这个 workflow 使用 `id-token: write` + Sigstore keyless，拿到的 OIDC token 的 `audience` 和 `subject` 字段由 GitHub 平台控制，用户脚本无法篡改
- Provenance 的关键字段（commit hash、repo、workflow ref）从 GitHub 平台元数据读取，而不是 build step 的 output
- 整个生成过程在一个独立 runner 上，构建 job 的磁盘/环境变量不会泄露进来

这些机制加起来使得"**攻击者即便能污染 build step**（比如安装一个恶意 npm 包），也无法伪造 provenance**"。

### 4.1 L3 验证流程

消费者拿到镜像后的验证流程：

```bash
slsa-verifier verify-image \
  ghcr.io/myorg/myapp@sha256:abc... \
  --source-uri github.com/myorg/myrepo \
  --source-tag v1.2.3
```

这条命令会：
1. 从 registry 拉 provenance attestation
2. 验证 Sigstore 签名（证书来自 Fulcio + 在 Rekor 里有记录）
3. 验证 provenance 里的 `builder.id` 是官方 SLSA generator
4. 验证 `source-uri` 符合传入的 repo
5. 验证 `source-tag` 符合传入的 tag/commit
6. 全部通过才退出 0

任何一步失败，`slsa-verifier` 返回非零退出码。可以直接嵌进 CI/CD 里作为"部署前门禁"。

### 4.2 Kyverno 集成 SLSA 验证

Kyverno 1.11+ 的 VerifyImages 直接支持 SLSA provenance 验证：

```yaml
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: verify-slsa-provenance
spec:
  validationFailureAction: Enforce
  rules:
    - name: check-slsa
      match:
        any: [{ resources: { kinds: [Pod], namespaces: ["prod-*"] }}]
      verifyImages:
        - imageReferences: ["ghcr.io/myorg/**"]
          attestors:
            - count: 1
              entries:
                - keyless:
                    subject: "https://github.com/slsa-framework/slsa-github-generator/.github/workflows/generator_container_slsa3.yml@refs/tags/v2.0.0"
                    issuer: https://token.actions.githubusercontent.com
          attestations:
            - type: https://slsa.dev/provenance/v1
              conditions:
                - all:
                    - key: "{{ buildDefinition.externalParameters.source.uri }}"
                      operator: Equals
                      value: "git+https://github.com/myorg/myrepo"
                    - key: "{{ buildDefinition.externalParameters.source.ref }}"
                      operator: AnyIn
                      value: ["refs/heads/main", "refs/heads/release/*"]
```

这条策略强制 `prod-*` namespace 里的所有镜像必须有**来自 SLSA generator workflow 签名的 provenance**，且 source 必须是我们自己的 repo 的 main 或 release 分支。任何 PR 分支、fork repo 构建的镜像都过不了。

**注意 subject 里的 `refs/tags/v2.0.0`**：这锁定了用的是官方 generator 的哪个版本。升级 generator 版本时要同步更新策略。

## 五、生产落地路线

SLSA 落地不是一次性工程，是循序渐进的过程。

### 5.1 评估当前等级

很可能你现在处于 L0 或 L1。评估 checklist：

- [ ] 有没有受信任的托管构建系统？（GH Actions / GitLab / Tekton）
- [ ] 有没有在构建时生成某种 provenance？
- [ ] Provenance 是不是被签名过？
- [ ] Provenance 能不能被 build step 伪造？
- [ ] 消费者是不是真的在部署前验证？

能全部打勾是 L3。其他情况请对照前面章节补上缺失的部分。

### 5.2 按项目优先级推进

不是所有项目都需要 L3。我们的实践：

| 类别 | 目标等级 |
|------|----------|
| 第三方依赖 / base image | L3 (选有 SLSA 的 upstream) |
| 生产核心服务（支付/登录/数据） | L3 |
| 生产一般服务 | L2 |
| 内部工具 / staging | L1 |
| 临时实验 | L0 (不要求) |

核心原则：**优先保护"攻破代价最大"的制品**。

### 5.3 与前九篇的整合

这是整个零信任系列的总结图：

```
                 ┌─────────────────────┐
                 │      开发者          │
                 └──────────┬──────────┘
                            │ commit
                            ▼
                 ┌─────────────────────┐
                 │   源码仓库           │
                 │   (SLSA Source v1.1) │
                 └──────────┬──────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────┐
│              CI/CD 流水线 (SLSA Build L3)             │
│                                                     │
│  构建 ─▶ 扫描 (Trivy) ─▶ SBOM (Syft)                 │
│    │                         │                     │
│    ▼                         ▼                     │
│  签名 (Cosign keyless)     Dependency-Track        │
│    │                                                │
│    ▼                                                │
│  Provenance (SLSA generator, Sigstore 签名)        │
│    │                                                │
└────┼────────────────────────────────────────────────┘
     │
     ▼ push
┌─────────────────────┐
│    OCI Registry     │
│  image + .sig + .att│
└──────────┬──────────┘
           │ pull
           ▼
┌─────────────────────────────────────────────────────┐
│            Kubernetes 集群                          │
│                                                     │
│  Admission:                                         │
│    ├── PSA (Pod Security)                           │
│    ├── Kyverno (policy + verify signatures/SLSA)    │
│    └── Kyverno (verify SBOM/vuln attestation)       │
│                                                     │
│  Runtime:                                           │
│    ├── SPIRE (workload identity)                    │
│    ├── Cilium (network policy + L7)                 │
│    ├── Falco (runtime detection)                    │
│    └── Secret Rotation (Vault / SM / SOPS)          │
│                                                     │
│  Edge:                                              │
│    └── WireGuard Mesh (跨云互联)                     │
└─────────────────────────────────────────────────────┘
```

每一层有专门的工具：

1. **构建层**：SLSA provenance + Cosign 签名 + Syft SBOM + Trivy 漏洞
2. **准入层**：PSA + Kyverno + VerifyImages（签名 + provenance + SBOM 条件）
3. **运行时层**：Falco 检测 + Cilium 网络 + SPIRE 身份
4. **密钥层**：Vault/SM/SOPS 动态和轮换管理
5. **网络层**：WireGuard mesh VPN 跨域连接

**缺一不可**：这些工具没有单一能覆盖整个供应链，只有组合起来才能形成完整防御。SLSA 提供的是整体框架，告诉你"**怎么衡量自己到哪一层**"。

## 六、踩坑记录

### 6.1 GHA generator 版本升级破坏策略

我们把 `slsa-github-generator` 从 v1.10 升级到 v2.0，Kyverno policy 里的 `subject` 字段是 `refs/tags/v1.10.0`，升级后的 provenance subject 变成了 `v2.0.0`，policy 直接拒绝所有新镜像。

修复：
1. 升级 generator 时**先更新 Kyverno policy**，再触发新构建
2. 或者用 `subjectRegExp` 匹配多个版本：
   ```yaml
   subjectRegExp: "^https://github.com/slsa-framework/slsa-github-generator/.*@refs/tags/v[12]\\..*$"
   ```

### 6.2 Provenance 里 source commit 不是预期的

有次我们发现 provenance 里的 `resolvedDependencies` 里 commit hash 和 UI 上看到的不一致。根因是 `actions/checkout` 的 fetch-depth 默认是 1，checkout 后的 HEAD 是一个合并的临时 commit，不是真实 main 分支的 commit。

修复：
```yaml
- uses: actions/checkout@v4
  with:
    fetch-depth: 0
    ref: ${{ github.sha }}
```

显式 checkout 真实 commit。

### 6.3 Rekor 不可用导致验证失败

前面 Sigstore 那篇也提过这个坑。SLSA 验证依赖 Rekor 的 inclusion proof，Rekor 公共实例偶发抽风，会让所有部署卡住。

修复：
- 生产用私有 Sigstore 实例（Fulcio + Rekor）
- 或者用 Sigstore 2.2+ 的 offline bundle 机制：
  ```bash
  cosign sign --new-bundle-format ...
  cosign verify --offline ...
  ```

### 6.4 L3 generator 性能问题

`slsa-github-generator` 本身是一个独立 job，会多花 1~2 分钟。对于高频构建的单仓库，每天多加半小时的 CI 时间。

优化：
- 只在 tag push 时生成 L3 provenance，push main 只生成 L1/L2
- 用 reusable workflow 的 concurrency group 避免重复触发
- 接受这个开销——L3 的安全收益远超 1~2 分钟的成本

### 6.5 多 arch 镜像 provenance 复杂性

docker buildx 构建 multi-arch 镜像时会有一个 manifest list + N 个 arch-specific manifest。provenance 应该挂在哪个上？

- **方案 A**：挂在每个 arch manifest 上，消费者按 arch 验证
- **方案 B**：挂在 manifest list 上，但有些工具不支持

`slsa-github-generator` v2.0 的做法是挂在 manifest list（OCI Index）上，然后在 provenance 的 `subject` 里列出所有 arch digest。Kyverno 和 slsa-verifier 都支持这种模式，其他工具要确认一下。

## 七、衡量进度：SLSA 指标

SLSA 落地后怎么衡量？几个关键指标：

```
# provenance 覆盖率
total_images / images_with_valid_provenance

# L3 覆盖率
total_images / images_with_l3_provenance

# 验证失败率
kyverno_verifyimages_failures_total

# 部署前验证成功率
slsa_verifier_success_total / slsa_verifier_total
```

做成 Grafana 仪表盘，每周团队 review。指标的意义是"**让这件事可见**"——不能被度量的东西，就不会被改进。

## 八、未来方向：SLSA v1.1 和 Source Track

SLSA v1.1 的草案里有两个重要方向：

1. **Source Track**：衡量源码管理的完整性，包括"commit 是不是经过 review"、"强制 2FA"、"commit 历史不可篡改"等
2. **Verification Summary Attestation (VSA)**：让消费者信任上游的验证结果。比如你信任 Google distroless 的团队，就可以信任他们发布的 VSA 而不自己验证每个 provenance

2025 年这些都还是草案阶段，但值得关注。一旦成熟，供应链安全的标准会变得更完整。

## 九、实战建议

最后几条总结性建议：

**1. 不要追求完美**。先从 L1 开始，让团队习惯 provenance 的存在。L3 是远期目标，先有东西比完美重要。

**2. 选择官方 generator**。不要自己实现 SLSA generator，你写的肯定做不到 L3 的隔离保证。GitHub Actions 用 `slsa-github-generator`，Tekton 用 Chains，GitLab 用原生。

**3. 把验证做在多个位置**。部署前验证、运行时验证、审计时验证。单点验证容易被绕过。

**4. 建立"白名单" generator 策略**。你的 Kyverno 策略里只接受官方 generator 签名的 provenance。私有的构建工具，写明显的豁免机制并严格 review。

**5. 关注上游供应链**。Base image (distroless/chainguard)、语言包管理器 (npm/pypi/maven)、基础依赖 (openssl/libcurl) 都有各自的 SLSA 进展。选有 SLSA 的上游比自己搞更有效。

**6. 不要只盯技术**。SLSA 涉及工程、安全、开发、运维多角色协作。技术实施只是 30%，剩下 70% 是流程和文化。

## 十、整个零信任系列的收尾

这是本系列的第十篇也是最后一篇。回顾整个旅程：

1. **Falco**：运行时行为检测的最后一道防线
2. **SPIRE**：工作负载身份的统一根
3. **Sigstore/Cosign**：制品来源的可信证明
4. **Dependency-Track**：依赖漏洞的持续监测
5. **Cilium**：网络层的身份级策略
6. **WireGuard**：跨云跨域的加密通道
7. **Secret Rotation**：动态凭据与自动轮换
8. **Pod Security Standards**：工作负载特权的底线
9. **Kyverno**：策略即代码的治理层
10. **SLSA**：整个供应链的完整性框架

十篇文章讲的都是"**零信任在云原生环境的具体工程实施**"。零信任不是一个产品，不是一个架构图，更不是一条 marketing slogan。**它是一整套把"默认不信任、持续验证、最小权限"这三个原则固化到代码和流程里的工程实践**。

没有哪一个工具能单独覆盖零信任的所有需求——你需要运行时检测（Falco）、身份体系（SPIRE）、制品可信（Sigstore）、依赖管理（Dependency-Track）、网络边界（Cilium）、加密通道（WireGuard）、密钥管理（Vault/SM/SOPS）、准入控制（PSA + Kyverno）、整体框架（SLSA）。每一层都有自己的职责，每一层都需要独立投入。

我希望这十篇文章能帮到一些在零信任落地路上的同行。这是一条漫长但值得走的路。等你把这套东西真正跑起来后，你会发现一个最有趣的变化：**一旦有了这些工具，你再也不相信任何"靠信任"的做法了**。当你习惯了每次服务调用都有身份验证、每个镜像都有 provenance、每条网络流量都有策略、每次 Pod 启动都被准入检查——再回头看那些"内网信任"、"管理员手改配置"、"明文密码 YAML"的老环境，你会觉得那是远古时代的实践。

这就是零信任真正的价值：**它不仅给你一套工具，它改变了你看待基础设施安全的方式**。

感谢阅读到这里。祝你在自己的零信任之旅上一切顺利。
