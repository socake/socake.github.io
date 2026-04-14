---
title: "Sigstore/Cosign 镜像签名实战：从 keyless 签名到准入策略验证"
date: 2025-10-17T10:00:00+08:00
draft: false
tags: ["Sigstore", "Cosign", "供应链安全", "镜像签名", "DevSecOps"]
categories: ["DevSecOps"]
description: "一份 Sigstore 生产化落地笔记：讲清楚 Fulcio/Rekor/Cosign 三件套的工作原理，演示 GitHub Actions 和 GitLab CI 下的 keyless 签名流水线，对接 Kyverno/Policy Controller 做准入验证，并分享签名验证性能、Rekor 不可用降级、多签策略等真实运维经验。"
summary: "一份 Sigstore 生产化落地笔记：讲清楚 Fulcio/Rekor/Cosign 三件套的工作原理，演示 GitHub Actions 和 GitLab CI 下的 keyless 签名流水线，对接 Kyverno/Policy Controller 做准入验证，并分享签名验证性能、Rekor 不可用降级、多签策略等真实运维经验。"
toc: true
math: false
diagram: false
keywords: ["Cosign", "Sigstore", "Fulcio", "Rekor", "镜像签名"]
params:
  reading_time: true
---

## 为什么镜像签名是供应链安全的基石

2021 年 SolarWinds 事件之后，"软件供应链安全"从学术概念变成了各家公司的合规硬需求。NIST SP 800-218、CISA Secure Software Development Framework、EU Cyber Resilience Act，再到国内的《关键信息基础设施安全保护条例》，都在要求"制品可追溯、可验证"。而在容器生态里，回答"这个镜像是不是我们 CI 流水线构建的"这个问题的答案，就是**镜像签名**。

镜像签名的目标非常简单：**部署时能够验证镜像确实由可信的构建者生成，且未被篡改**。做到这一点，你可以防住几类威胁：

1. **Registry 被入侵**：攻击者替换了 latest tag 指向的 manifest，签名验证失败即阻断。
2. **内部恶意**：某个离职员工偷用凭据 push 了一个后门镜像，没有签名无法通过准入。
3. **中间人**：私有 registry 上传链路被中间人插入，签名校验失败。
4. **供应链上游**：基础镜像被污染，签名链可以追溯到原始构建者。

传统的 Docker Content Trust（Notary v1）早就被弃用，原因是 key 管理复杂、生态不完善、没有透明度日志。**Sigstore** 从 2021 年登场，彻底改变了这个游戏——它解决了 key 管理（keyless 签名）、透明度（Rekor 不可篡改日志）、集成（CI/CD 一键对接），现在已经是事实标准。Kubernetes、CNCF 项目、Chainguard、RedHat UBI、几乎所有主流开源镜像都在用 Sigstore 签名。

这篇文章我会完整走一遍生产落地的过程，基于 **Cosign 2.4+**、**Fulcio v1.6+**、**Rekor v1.4+**，以及 **Policy Controller 0.12+**（从 2024 年开始已经是 Sigstore 官方推荐的 admission 方案）。

## 一、Sigstore 三件套：Cosign、Fulcio、Rekor

### 1.1 整体架构

```
  ┌────────────────┐       ┌───────────────────┐      ┌───────────────┐
  │  CI Pipeline   │       │  Fulcio (CA)      │      │  OIDC Issuer  │
  │                │ (1)──▶│ 用 OIDC token 换证│◀────▶│ GitHub/GitLab │
  │  cosign sign   │       └────────┬──────────┘      │ /Google/etc.  │
  │                │                │                 └───────────────┘
  └───────┬────────┘                │ 签发短期证书
          │                         ▼
          │                  ┌─────────────┐
          │ (2) 签名镜像     │  短期证书    │
          │ + 短期证书       │  (10 分钟)   │
          │ + 上传签名       └─────────────┘
          ▼
  ┌────────────────┐       ┌───────────────────┐
  │  OCI Registry  │       │  Rekor            │
  │                │ (3)──▶│  透明度日志       │
  │  image + .sig  │       │  (不可篡改)       │
  └────────────────┘       └───────────────────┘
          │
          │ (4) 部署时验证
          ▼
  ┌────────────────┐
  │  Policy        │
  │  Controller    │
  │  / Kyverno     │
  └────────────────┘
```

核心组件：

- **Cosign**：CLI 工具，负责签名和验证。它对接 OIDC 换 Fulcio 证书、向 Rekor 上传签名记录、把签名作为 OCI artifact 推到 registry。
- **Fulcio**：一个特殊的证书颁发机构，接收 OIDC token 并颁发绑定身份的短期 X.509 证书（10 分钟 TTL）。它不是传统 PKI，它是一个**身份到证书的映射器**。
- **Rekor**：透明度日志，基于 Merkle Tree 的 append-only log。每一条签名记录都会入 Rekor，得到一个不可篡改的 `logIndex` 和 `inclusion proof`。

### 1.2 Keyless 签名：核心创新

传统签名的最大痛点是密钥管理。你要生成 key、保存 key（HSM、KMS）、分发公钥、定期轮换、被盗后吊销。Sigstore 的 keyless 签名彻底抛弃了长期密钥：

**签名流程**：
1. CI 作业通过 OIDC provider（GitHub/GitLab/Google 等）获取一个 id_token
2. Cosign 生成**临时 ECDSA 密钥对**（只在内存里存在几秒）
3. Cosign 把临时公钥 + id_token 发给 Fulcio
4. Fulcio 验证 id_token 真实性，把身份信息（email、repo、workflow 路径）写入 X.509 证书扩展字段，用 Fulcio 的 CA 签发一张 10 分钟 TTL 的证书
5. Cosign 用临时私钥对镜像 digest 签名
6. Cosign 把 `证书 + 签名 + 签名时间戳` 上传到 Rekor，得到 `logEntry`
7. Cosign 把签名（含证书）作为一个 OCI artifact 推到 registry，tag 格式为 `sha256-<digest>.sig`
8. **临时私钥销毁**

**验证流程**：
1. 从 registry 拉取签名 artifact
2. 验证证书链能到 Fulcio 根 CA
3. 验证证书的 SAN 扩展里的身份符合策略（比如 `repo:myorg/myrepo:ref:refs/heads/main`）
4. 验证签名能还原出镜像 digest
5. 查 Rekor 确认 logEntry 存在，且签名时间戳在证书有效期内

注意步骤 5：证书虽然只有 10 分钟有效期，但**签名验证可以在几个月后**，这是因为 Rekor 记录了签名发生的时间点，只要那个时间点在证书有效期内就认为有效。这是 Sigstore 设计里最精妙的部分——用透明度日志替代了长期证书。

## 二、CI 流水线签名：GitHub Actions 版本

### 2.1 最小可用版本

```yaml
name: build-and-sign

on:
  push:
    branches: [main]
    tags: ['v*']

permissions:
  id-token: write   # keyless 签名必须
  contents: read
  packages: write

jobs:
  build-sign:
    runs-on: ubuntu-22.04
    steps:
      - uses: actions/checkout@v4

      - name: Install Cosign
        uses: sigstore/cosign-installer@v3.7.0
        with:
          cosign-release: v2.4.1

      - name: Login to GHCR
        uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - name: Build & Push
        id: build
        uses: docker/build-push-action@v6
        with:
          context: .
          push: true
          tags: ghcr.io/${{ github.repository }}:${{ github.sha }}
          provenance: false    # 关掉 BuildKit 自带 provenance, 交给 Cosign
          cache-from: type=gha
          cache-to: type=gha,mode=max

      - name: Sign image with Cosign (keyless)
        env:
          COSIGN_YES: "true"
        run: |
          cosign sign \
            --rekor-url https://rekor.sigstore.dev \
            --fulcio-url https://fulcio.sigstore.dev \
            ghcr.io/${{ github.repository }}@${{ steps.build.outputs.digest }}
```

`permissions.id-token: write` 是 **必须的**，它让 job 能从 GitHub 拿到 OIDC token。没这行会报 `could not fetch token`。

`COSIGN_YES=true` 跳过交互式确认（默认 Cosign 会问"你要把身份写进公开 Rekor 日志吗？"）。CI 里必须设，否则卡住。

**注意要签 `@digest` 而不是 `:tag`**。Tag 是可变的，digest 是不可变的哈希。签 tag 会导致验证时取不到正确的签名。

### 2.2 把签名和 SBOM 一起上传

一个成熟的供应链流水线应当同时生成签名、SBOM 和 provenance，并把它们挂到 OCI artifact 上：

```yaml
      - name: Generate SBOM
        uses: anchore/sbom-action@v0
        with:
          image: ghcr.io/${{ github.repository }}@${{ steps.build.outputs.digest }}
          format: cyclonedx-json
          output-file: sbom.cdx.json

      - name: Attach SBOM as attestation
        env:
          COSIGN_YES: "true"
        run: |
          cosign attest \
            --predicate sbom.cdx.json \
            --type cyclonedx \
            ghcr.io/${{ github.repository }}@${{ steps.build.outputs.digest }}

      - name: Generate SLSA provenance
        uses: slsa-framework/slsa-github-generator/.github/workflows/generator_container_slsa3.yml@v2.0.0
        with:
          image: ghcr.io/${{ github.repository }}
          digest: ${{ steps.build.outputs.digest }}
```

`cosign attest` 和 `cosign sign` 的区别：sign 只是对 digest 签名，attest 是把一段"声明"（JSON 结构的 predicate）签名并挂到镜像上，常见的声明类型有 SBOM、SLSA provenance、漏洞扫描结果。

签名、SBOM attestation、provenance 都是独立的 OCI artifact，tag 命名不同：

```
ghcr.io/org/repo@sha256:abc123...             # 镜像本体
ghcr.io/org/repo:sha256-abc123....sig         # Cosign 签名
ghcr.io/org/repo:sha256-abc123....att         # attestation (SBOM/provenance)
```

Cosign 验证的时候会按命名约定找到对应的 artifact。

### 2.3 GitLab CI 版本

GitLab 从 16.0 开始原生支持 OIDC，流程和 GitHub 几乎一样：

```yaml
sign-image:
  stage: sign
  image: cgr.dev/chainguard/cosign:latest
  id_tokens:
    SIGSTORE_ID_TOKEN:
      aud: sigstore
  variables:
    COSIGN_YES: "true"
  script:
    - |
      cosign sign \
        --identity-token $SIGSTORE_ID_TOKEN \
        $CI_REGISTRY_IMAGE@$IMAGE_DIGEST
```

关键是 `id_tokens` 配置块，`aud: sigstore` 是 Fulcio 要求的 audience 值。`$SIGSTORE_ID_TOKEN` 是 GitLab 自动注入的环境变量。

### 2.4 Jenkins / 私有 CI

Jenkins 没有原生 OIDC，可以用两种方案：

1. **Spiffe/SPIRE 模式**：Jenkins Agent 接入 SPIRE，用 SPIFFE JWT 作为 Fulcio 的身份源。需要 Fulcio 配置支持 SPIFFE 的 issuer。
2. **Key-based 模式**：退回到传统 key 签名，私钥存 Vault/KMS。简单粗暴，但失去 keyless 的好处。

我们内部有一个老 Jenkins 集群用的是方案 2，配合 AWS KMS：

```bash
cosign sign \
  --key awskms:///arn:aws:kms:us-west-2:123456789012:key/xxxx \
  ghcr.io/org/repo@sha256:abc...
```

验证时：

```bash
cosign verify \
  --key awskms:///arn:aws:kms:us-west-2:123456789012:key/xxxx \
  ghcr.io/org/repo@sha256:abc...
```

## 三、在 Kubernetes 做准入验证

签了名只是起点，真正有意义的是**部署时强制验证**。不验证的签名等于没签。

### 3.1 Policy Controller 还是 Kyverno？

两个方案都能做镜像签名验证，我的对比：

| 维度 | Sigstore Policy Controller | Kyverno |
|------|----------------------------|---------|
| 专注领域 | 只做签名/attestation 验证 | 通用 policy engine |
| 策略语法 | ClusterImagePolicy CRD | ClusterPolicy CRD（CEL/JSON patch） |
| 性能 | 极优（专门优化） | 好 |
| 灵活性 | 只做签名 | 镜像签名 + YAML validation + mutation |
| 运维成本 | 低 | 中 |

**我的建议**：如果你只做镜像签名验证，用 Policy Controller；如果你已经在用 Kyverno 做其他 policy（比如 Pod Security、命名规范、资源约束），那就用 Kyverno 统一管理。我们同时用了两者——Policy Controller 专门负责签名验证，Kyverno 负责其他策略，互不干扰。

### 3.2 Policy Controller 配置

```yaml
apiVersion: policy.sigstore.dev/v1beta1
kind: ClusterImagePolicy
metadata:
  name: must-sign-by-main-branch
spec:
  images:
    - glob: "ghcr.io/myorg/**"
    - glob: "registry.example.com/prod/**"
  authorities:
    - name: main-branch-signer
      keyless:
        url: https://fulcio.sigstore.dev
        identities:
          - issuer: https://token.actions.githubusercontent.com
            subject: "https://github.com/myorg/myrepo/.github/workflows/build.yml@refs/heads/main"
        trustRootRef: public-good
      ctlog:
        url: https://rekor.sigstore.dev
        trustRootRef: public-good
  mode: enforce
```

这段策略说："所有 `ghcr.io/myorg/**` 的镜像必须有 keyless 签名，身份必须是 GitHub Actions 的 main 分支 workflow。" 任何手工推送的镜像、feature 分支构建的镜像、或者被替换 tag 的镜像都会被拒绝。

关键字段：

- **`images.glob`**：匹配哪些镜像受这条策略约束。不匹配的镜像不受影响。
- **`keyless.identities.subject`**：匹配 Fulcio 证书里 SAN 扩展的 subject 字段。主要用于区分 "main 分支构建" vs "PR 构建"，防止把 PR 镜像部署到 prod。
- **`mode`**：`enforce` 拒绝不符合的，`warn` 只警告不拒绝。**上线时先用 warn 观察一周再切 enforce**。
- **`trustRootRef`**：信任根。公用实例用 `public-good`，私有实例需要先创建 `TrustRoot` CR 指向你自己的 Fulcio/Rekor CA。

### 3.3 Kyverno 版本

Kyverno 从 1.11 开始原生支持 Cosign 验证：

```yaml
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: verify-image-signatures
spec:
  validationFailureAction: Enforce
  background: false
  webhookTimeoutSeconds: 30
  rules:
    - name: check-image-signatures
      match:
        any:
          - resources:
              kinds: [Pod]
              namespaces: ["payments", "orders", "inventory"]
      verifyImages:
        - imageReferences:
            - "ghcr.io/myorg/**"
          attestors:
            - count: 1
              entries:
                - keyless:
                    subject: "https://github.com/myorg/myrepo/.github/workflows/build.yml@refs/heads/main"
                    issuer: https://token.actions.githubusercontent.com
                    rekor:
                      url: https://rekor.sigstore.dev
          mutateDigest: true     # 把 tag 替换为 digest，避免后续变化
          required: true
          failureAction: Enforce
```

`mutateDigest: true` 这个选项**非常重要**，它会在准入时把 `image: xxx:latest` 重写成 `image: xxx@sha256:...`，这样即便 latest tag 后来被改了，Pod 里跑的依然是签名时的那个镜像。这是防篡改的最后一道防线。

## 四、私有 Sigstore 实例：Fulcio、Rekor、TUF root

很多企业出于合规或者网络隔离原因不能用 Sigstore 公共实例（`fulcio.sigstore.dev`、`rekor.sigstore.dev`），需要自建。主要组件：

- **Fulcio**：容器化部署，需要挂自己的 OIDC issuer 列表。存储后端是 AWS/GCP KMS 或者软件 key。
- **Rekor**：基于 Trillian 的透明度日志，后端是 MySQL 或 CockroachDB。
- **TUF root**：给客户端分发 Fulcio 和 Rekor 的公钥。需要一个 HTTPS 地址托管 metadata。

我们自建 Sigstore 的规模：一个 region 一套，三副本 Fulcio + 三副本 Rekor + Trillian log server + MySQL Aurora。资源占用不算大，主要是存储长期累积。Rekor 日志从不删除（append-only），一年数据大约 50GB。

**关键运维点**：

1. **CA 根密钥必须放 HSM 或云 KMS**。Fulcio CA 是整个信任体系的根，一旦泄漏所有签名作废。
2. **Rekor 的 Trillian log 必须定期做 consistency proof 备份**。这保证了即便 Rekor 后端被篡改，也能通过外部备份发现。
3. **TUF root 轮换有标准流程**。不要手动改 TUF metadata，用 `tuf-on-ci` 或者 Chainguard 的 tuf 工具走规范流程。
4. **OIDC issuer 接入内网 IdP**（比如 Okta、Keycloak）。确保 issuer 颁发的 token 里有 `sub` 字段标识服务/人员身份。

### 4.1 私有实例的 verify 配置

客户端使用私有 Sigstore 实例时，不能依赖 `sigstore-js` 自带的 public-good TUF root，需要显式指定：

```yaml
apiVersion: policy.sigstore.dev/v1alpha1
kind: TrustRoot
metadata:
  name: corp-sigstore
spec:
  remote:
    mirror: https://tuf.sigstore.corp.example.com
    root: |
      {"signed":{"_type":"root",...}}
---
apiVersion: policy.sigstore.dev/v1beta1
kind: ClusterImagePolicy
metadata:
  name: corp-images
spec:
  images:
    - glob: "registry.corp.example.com/**"
  authorities:
    - name: corp-ci
      keyless:
        url: https://fulcio.sigstore.corp.example.com
        trustRootRef: corp-sigstore
        identities:
          - issuer: https://gitlab.corp.example.com
            subjectRegExp: "^https://gitlab\\.corp\\.example\\.com/.*/.gitlab-ci\\.yml@refs/heads/(main|release/.*)$"
      ctlog:
        url: https://rekor.sigstore.corp.example.com
        trustRootRef: corp-sigstore
```

注意 `subjectRegExp`，允许正则匹配多个合法身份。这在多个仓库共用一条策略时很方便。

## 五、踩坑记录

### 5.1 Rekor 公共实例不可用导致全站拉镜像失败

这是我们遇到过的最大一次事故。2024 年底某次 Rekor 公共实例出现了一段时间的 503，我们 Kyverno 配置了强制 Rekor 验证，结果所有新部署的 Pod 卡在 `ImagePullBackOff`——准入 webhook 返回 error 被 `failureAction: Enforce` 拒绝。

根因：我们没有设置 **webhook failure policy** 和 **Rekor 验证超时**。修复方案：

1. Kyverno policy 的 `webhookTimeoutSeconds` 设短（10~15 秒），超时走 fallback
2. 对关键 namespace 配置 `failurePolicy: Ignore`（但这会降低安全性）
3. **切到私有 Rekor 实例**，可用性自己掌控
4. 使用 `--offline` 模式验证：把 Rekor 证明预先拉下来打包进镜像（Cosign 2.2+ 支持 bundle）

最终方案是 3 + 4 结合。私有 Rekor + offline bundle 让我们完全不依赖外部服务，同时保留完整签名链。

### 5.2 OCI registry 不支持 referrers API

Cosign 签名默认用 "tag 命名约定"（`sha256-xxxxx.sig`）上传，兼容性最好。2023 年 OCI 1.1 引入了 referrers API，Cosign 2.0+ 支持用 referrers 上传签名，好处是签名和镜像绑在同一个索引下、registry GC 不会误删。

但 **很多私有 registry 不支持 referrers API**（比如老版 Harbor < 2.8、Artifactory 某些版本）。建议：

- 生产环境显式强制用 tag 方式：`cosign sign --registry-referrers-mode=legacy`
- 迁移到支持 referrers 的 registry 时做兼容测试
- Cosign 2.5+ 有自动检测，但别依赖，该写死就写死

### 5.3 GitHub Actions subject 路径陷阱

Fulcio 颁发的证书里，GitHub Actions 的 subject 格式是：

```
https://github.com/<owner>/<repo>/.github/workflows/<workflow-file>@refs/heads/<branch>
```

**注意**是 workflow 文件路径，不是 job 名或者 workflow 名。如果你的 workflow 文件叫 `build.yml`，subject 就是 `build.yml@refs/heads/main`；改名字后策略就失效了。

我们踩过一次这个坑：把 `build.yml` 重命名为 `build-and-sign.yml`，结果所有 prod 部署全挂，policy controller 拒绝新镜像。教训：workflow 文件名要当成"公开 API"来对待，不要随便改。变更走灰度。

### 5.4 跨账号/跨组织的信任传递

如果你的上游（base image）是另一个组织构建的，比如你用 `gcr.io/distroless/base`，怎么验证它也是合法签名的？

**方案**：在你的策略里直接信任对方的签名身份。distroless 的签名 subject 是 `keyless@distroless.iam.gserviceaccount.com`：

```yaml
- name: distroless-base
  keyless:
    url: https://fulcio.sigstore.dev
    identities:
      - issuer: https://accounts.google.com
        subject: keyless@distroless.iam.gserviceaccount.com
```

这样在 CI 拉 base image 时先验证一次，再用 builder 构建自己的镜像。Cosign 的 `--experimental-oci-layout` 模式可以把验证结果作为 attestation 挂到自己的镜像上，形成可追溯的信任链。

### 5.5 签名体积导致 registry 膨胀

一个镜像只占几 MB，但加上签名、SBOM attestation、provenance attestation 可能再加 1~2MB。大规模 CI 环境每天上千次构建，一个月能累积几十 GB 的签名 artifact。

**定期清理策略**：

- **keep 策略**：保留最近 N 次构建的签名（比如 30 次）
- **immutable 策略**：prod 环境用过的镜像对应签名永久保留
- 其他过期清理

Harbor 的 retention policy 支持按 tag pattern 过滤，配置 `sha256-*.sig` 和 `*.att` 的保留规则。

## 六、与其他安全工具集成

### 6.1 和 Trivy 的关系

很多人混淆：签名和漏洞扫描是两件事。**签名只证明"来源可信"，不证明"内容安全"**。一个合法签名的镜像里照样可能有 CVE。

正确的工作流是：

```
构建 ─▶ Trivy 扫描（质量门禁）─▶ Cosign 签名 ─▶ Cosign attest 挂漏扫结果 ─▶ 部署
                                                                 │
                                                                 ▼
                                                        准入时再次校验 attestation
```

准入时既验证签名合法，又验证 attestation 里的漏洞数量低于阈值。Kyverno 的 `verifyImages` 支持 `attestations.conditions`：

```yaml
attestations:
  - predicateType: cosign.sigstore.dev/attestation/vuln/v1
    conditions:
      - all:
          - key: "{{ regex_match('^[0-4]$', '{{summary.Critical}}') }}"
            operator: Equals
            value: true
          - key: "{{ summary.High }}"
            operator: LessThanOrEquals
            value: 10
```

这条策略要求镜像必须带 trivy 的漏洞声明，且 Critical <= 4、High <= 10。

### 6.2 和 SPIRE 的关系

前一篇讲 SPIRE 时说过，SPIFFE ID 可以作为 Fulcio 的 OIDC 身份来源。这意味着 **SPIRE 颁发的 JWT-SVID 可以用来向 Fulcio 换签名证书**，CI Runner 也可以走这条路：

1. Runner 上部署 SPIRE Agent，通过 `k8s_psat` 证明自己是某个 Job Pod
2. Runner 的构建脚本从 Workload API 取 JWT-SVID
3. 把 JWT 作为 OIDC token 传给 `cosign sign --identity-token=$svid`
4. Fulcio 颁发证书，subject 是 SPIFFE ID

这比直接用 CI 平台的 OIDC 更灵活——你可以精细控制哪个 Runner、哪个 Job 被允许签名。

## 七、完整的端到端流水线示例

把上面各部分整合起来，一个生产级流水线应该是这样的：

```yaml
name: secure-build

on: { push: { branches: [main] } }

permissions:
  id-token: write
  contents: read
  packages: write

jobs:
  build:
    runs-on: ubuntu-22.04
    outputs:
      digest: ${{ steps.push.outputs.digest }}
    steps:
      - uses: actions/checkout@v4
      - uses: docker/setup-buildx-action@v3

      - name: Build
        id: push
        uses: docker/build-push-action@v6
        with:
          push: true
          tags: ghcr.io/${{ github.repository }}:${{ github.sha }}
          provenance: false
          sbom: false

  scan:
    needs: build
    runs-on: ubuntu-22.04
    steps:
      - uses: aquasecurity/trivy-action@0.28.0
        with:
          image-ref: ghcr.io/${{ github.repository }}@${{ needs.build.outputs.digest }}
          format: cosign-vuln
          output: vuln.json
          exit-code: '1'
          severity: 'CRITICAL'
      - uses: actions/upload-artifact@v4
        with: { name: vuln, path: vuln.json }

  sign:
    needs: [build, scan]
    runs-on: ubuntu-22.04
    steps:
      - uses: sigstore/cosign-installer@v3.7.0
      - uses: actions/download-artifact@v4
        with: { name: vuln }
      - name: Sign image
        env: { COSIGN_YES: "true" }
        run: |
          IMAGE=ghcr.io/${{ github.repository }}@${{ needs.build.outputs.digest }}
          cosign sign $IMAGE
          cosign attest --predicate vuln.json --type vuln $IMAGE

      - name: Generate & attach SBOM
        run: |
          syft ghcr.io/${{ github.repository }}@${{ needs.build.outputs.digest }} \
               -o cyclonedx-json > sbom.json
          cosign attest --predicate sbom.json --type cyclonedx \
               ghcr.io/${{ github.repository }}@${{ needs.build.outputs.digest }}

  deploy:
    needs: sign
    runs-on: ubuntu-22.04
    environment: production
    steps:
      - name: Verify before deploy
        run: |
          cosign verify \
            --certificate-identity "https://github.com/${{ github.repository }}/.github/workflows/secure-build.yml@refs/heads/main" \
            --certificate-oidc-issuer https://token.actions.githubusercontent.com \
            ghcr.io/${{ github.repository }}@${{ needs.build.outputs.digest }}
          cosign verify-attestation \
            --certificate-identity "..." \
            --type vuln \
            ghcr.io/${{ github.repository }}@${{ needs.build.outputs.digest }}
      - name: Update manifest
        run: |
          # GitOps：更新 kustomize 镜像 digest，推 infra repo
          kustomize edit set image app=ghcr.io/${{ github.repository }}@${{ needs.build.outputs.digest }}
```

这个流水线的关键设计：

- **每个阶段独立 job**：build/scan/sign/deploy 解耦，便于重跑失败步骤
- **强制 scan 通过才 sign**：失败扫描阻断签名
- **deploy 前再次 verify**：即便 registry 被篡改也能发现
- **GitOps 更新 digest**：避免 tag 漂移

## 八、落地路线图

和上一篇 SPIRE 类似，Sigstore 落地也要循序渐进：

**阶段 1（2 周）**：在一个非关键业务的 CI 里开启 `cosign sign`，先看签名、不做验证。熟悉命令和产物。

**阶段 2（1 个月）**：部署 Policy Controller 或 Kyverno，配置 `warn` 模式。观察日志找出哪些镜像没签名、哪些身份不符合策略。

**阶段 3（1 个月）**：切到 `enforce` 模式，但先从低优先级 namespace 开始。同时把所有 CI 流水线补齐签名步骤。

**阶段 4（1~3 个月）**：部署私有 Sigstore 实例（如果合规要求），迁移 policy 指向私有 TrustRoot。添加 SBOM/vuln attestation。

**阶段 5（持续）**：和 SPIRE/OPA/Falco 等其他安全工具联动，形成完整的"构建时+部署时+运行时"三层防护。

## 九、结语

两三年前签名还是一个可选项，今天已经变成合规硬性要求。Sigstore 的 keyless 签名让这件事的技术门槛降到了"CI 里加几行"，但真正把它落到生产上依然需要处理私有部署、策略治理、失败降级这些工程细节。

我的经验是：**先跑起来，再调细节**。不要一开始就追求完美的私有 Sigstore + SPIRE + 多签名策略，那样半年都上不了线。先用公共实例+简单策略把流水线打通，看到真实的签名数据后再考虑升级。每一步都有可验证的价值，团队才会持续投入。

下一篇我会写 SBOM 生成和 Dependency-Track 管理，那是"签名之后"最重要的一块——知道你的镜像里到底有什么。
