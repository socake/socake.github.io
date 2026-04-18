---
title: "供应链安全：Trivy 镜像扫描 + Cosign 签名验证实践"
date: 2025-09-06T13:50:00+08:00
draft: false
tags: ["Trivy", "Cosign", "安全", "镜像", "Kubernetes", "SLSA"]
categories: ["安全"]
description: "从容器供应链攻击面分析入手，系统讲解 Trivy 漏洞扫描、Cosign 镜像签名与 K8s Admission Webhook 准入控制的完整实践，以及 CI/CD 流水线集成方案。"
summary: "你的镜像安全吗？本文梳理容器供应链的主要攻击面，手把手演示 Trivy 扫描、Cosign 签名、K8s 准入控制三层防护的搭建过程，并给出 GitLab CI 集成示例。"
toc: true
math: false
diagram: false
keywords: ["Trivy 镜像扫描", "Cosign 签名", "供应链安全", "SLSA", "K8s 准入控制"]
params:
  reading_time: true
---

## 容器供应链的攻击面

容器化应用的供应链上有好几个可能被塞东西的点，我们自己运维的时候最操心的就是这四个：

**基础镜像漏洞**：你的 `FROM python:3.11-slim` 里可能已经包含了 CVE 高危漏洞。OpenSSL、glibc、curl 这些基础库的漏洞在 NVD 数据库里每天都在增加，如果不定期重新构建镜像，生产环境跑的可能是几个月前的旧镜像，漏洞早已公开。

**第三方依赖**：`pip install requests` 或 `npm install lodash` 都可能引入带漏洞的传递依赖。左移（Shift Left）安全要求在 CI 阶段就扫出来，不要等到上了生产。

**构建过程注入**：CI/CD Runner 被攻陷后，攻击者可以在构建过程中替换二进制文件，而最终镜像的 SHA256 是合法构建出来的，难以察觉。

**镜像仓库篡改**：镜像推到 Registry 后，如果缺乏完整性验证，理论上 Registry 管理员或攻击者可以替换镜像内容而不改变 tag。

应对这些威胁，需要三层防护：**扫描**（发现已知漏洞）+ **签名**（保证镜像未被篡改）+ **准入控制**（只允许合规镜像进 K8s）。

## Trivy：全能扫描器

Trivy 是 Aqua Security 开源的扫描工具，除了镜像漏洞，还能扫配置错误、IaC 文件、SBOM。

### 安装

```bash
# macOS
brew install trivy

# Linux
curl -sfL https://raw.githubusercontent.com/aquasecurity/trivy/main/contrib/install.sh | sh -s -- -b /usr/local/bin

# Docker（不需要安装，直接用）
docker run --rm aquasec/trivy image nginx:latest
```

### 镜像漏洞扫描

```bash
# 扫描镜像
trivy image nginx:latest

# 只报 HIGH 和 CRITICAL
trivy image --severity HIGH,CRITICAL nginx:latest

# 输出 JSON（CI 解析用）
trivy image --format json --output result.json nginx:latest

# 如果发现 HIGH/CRITICAL 漏洞就退出非 0（让 CI 失败）
trivy image --exit-code 1 --severity HIGH,CRITICAL myapp:latest
```

输出示例：
```
nginx:latest (debian 11.6)
Total: 142 (UNKNOWN: 0, LOW: 89, MEDIUM: 40, HIGH: 11, CRITICAL: 2)

┌──────────────┬────────────────┬──────────┬────────────────┬────────────────┬──────────────────────────────┐
│   Library    │ Vulnerability  │ Severity │ Installed Ver  │  Fixed Version │          Title               │
├──────────────┼────────────────┼──────────┼────────────────┼────────────────┼──────────────────────────────┤
│ libssl1.1    │ CVE-2023-0286  │ CRITICAL │ 1.1.1n-0+deb11u3 │ 1.1.1n-0+deb11u4 │ X.400 address type         │
└──────────────┴────────────────┴──────────┴────────────────┴────────────────┴──────────────────────────────┘
```

### K8s YAML 和 Terraform 扫描

```bash
# 扫描 K8s YAML 配置（检查 privileged、hostPath、root 用户等）
trivy config ./k8s/

# 扫描 Terraform（检查 S3 public access、安全组 0.0.0.0/0 等）
trivy config ./terraform/

# 扫描整个 Git 仓库（镜像 + 配置 + 依赖一起）
trivy repo .
```

### 生成 SBOM

SBOM（Software Bill of Materials，软件物料清单）是记录镜像里所有组件的清单，方便日后出现新漏洞时快速判断是否受影响：

```bash
# 生成 CycloneDX 格式 SBOM
trivy image --format cyclonedx --output sbom.json myapp:latest

# 基于 SBOM 做漏洞扫描（离线场景）
trivy sbom sbom.json
```

## Cosign：镜像签名与验证

Cosign 是 Sigstore 项目的一部分，提供容器镜像的签名、验证和证明（Attestation）能力。

### 安装

```bash
# macOS
brew install cosign

# Linux
curl -O -L "https://github.com/sigstore/cosign/releases/latest/download/cosign-linux-amd64"
mv cosign-linux-amd64 /usr/local/bin/cosign && chmod +x /usr/local/bin/cosign
```

### 密钥对签名（传统模式）

适合私有仓库、自建 CI 环境：

```bash
# 生成密钥对（会要求设置密码）
cosign generate-key-pair

# 会生成 cosign.key（私钥）和 cosign.pub（公钥）
# 私钥存 Vault 或 CI Secret，公钥可以公开

# 推送镜像后签名（用私钥）
cosign sign --key cosign.key myregistry.com/myapp:v1.0.0

# 验证镜像签名（用公钥）
cosign verify --key cosign.pub myregistry.com/myapp:v1.0.0
```

签名信息作为 OCI Artifact 存储在 Registry 里，不影响镜像本身的 Digest。

### Keyless 模式（推荐）

Keyless 模式通过 OIDC（GitHub Actions、GitLab CI 等提供的身份）完成签名，不需要管理密钥对。签名绑定到构建者的 OIDC 身份，通过 Fulcio CA 和 Rekor 透明日志记录：

```bash
# GitHub Actions 里的 keyless 签名（无需配置密钥）
cosign sign --yes myregistry.com/myapp:${{ github.sha }}

# 验证时指定期望的 OIDC 发行方和主题
cosign verify \
  --certificate-identity-regexp="https://github.com/myorg/myrepo" \
  --certificate-oidc-issuer="https://token.actions.githubusercontent.com" \
  myregistry.com/myapp:latest
```

### 附加 Attestation（证明）

可以把 Trivy 的扫描结果、SBOM 等附加到镜像上，作为可验证的证明：

```bash
# 把 SBOM 附加到镜像
cosign attest --key cosign.key \
  --type cyclonedx \
  --predicate sbom.json \
  myregistry.com/myapp:v1.0.0

# 把 Trivy 扫描结果附加
trivy image --format cosign-vuln --output vuln.json myapp:v1.0.0
cosign attest --key cosign.key \
  --type vuln \
  --predicate vuln.json \
  myregistry.com/myapp:v1.0.0
```

## K8s 准入控制：只允许签名镜像部署

扫描和签名都做了，但如果 K8s 集群还能跑未签名的镜像，安全链路就不完整。Policy Controller（原 Connaisseur 或 Kyverno 都可以做，官方推荐用 Sigstore Policy Controller）通过 Admission Webhook 拦截每个 Pod 创建请求。

```bash
helm repo add sigstore https://sigstore.github.io/helm-charts
helm install policy-controller sigstore/policy-controller \
  -n cosign-system --create-namespace
```

创建 ClusterImagePolicy：

```yaml
apiVersion: policy.sigstore.dev/v1beta1
kind: ClusterImagePolicy
metadata:
  name: require-signed-images
spec:
  images:
    # 只对生产镜像仓库的镜像要求签名
    - glob: "myregistry.com/myapp/**"
  authorities:
    - keyless:
        url: https://fulcio.sigstore.dev
        identities:
          - issuer: https://token.actions.githubusercontent.com
            subject: https://github.com/myorg/myrepo/.github/workflows/build.yml@refs/heads/main
    # 或者用静态公钥
    - key:
        data: |
          -----BEGIN PUBLIC KEY-----
          MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE...
          -----END PUBLIC KEY-----
```

配置后，部署未签名镜像会被拒绝：

```
Error from server: admission webhook "policy.sigstore.dev" denied the request:
validation failed: no matching signatures: myregistry.com/myapp:latest
```

## CI/CD 集成：GitLab CI 完整示例

```yaml
# .gitlab-ci.yml
stages:
  - build
  - scan
  - sign
  - deploy

variables:
  IMAGE: $CI_REGISTRY_IMAGE:$CI_COMMIT_SHA

build:
  stage: build
  image: docker:24
  services:
    - docker:24-dind
  script:
    - docker login -u $CI_REGISTRY_USER -p $CI_REGISTRY_PASSWORD $CI_REGISTRY
    - docker build -t $IMAGE .
    - docker push $IMAGE

trivy-scan:
  stage: scan
  image: aquasec/trivy:latest
  script:
    # 扫描镜像漏洞，发现 CRITICAL 就失败
    - trivy image --exit-code 1 --severity CRITICAL
        --format sarif --output trivy-results.sarif
        $IMAGE
    # 扫描 K8s YAML 配置
    - trivy config k8s/
  artifacts:
    reports:
      sast: trivy-results.sarif  # GitLab Security Dashboard 可以展示
    expire_in: 1 week
  allow_failure: false

cosign-sign:
  stage: sign
  image: bitnami/cosign:latest
  script:
    # 从 CI Variable 读取私钥（存为 File 类型的 CI Variable）
    - cosign sign --key $COSIGN_PRIVATE_KEY
        --tlog-upload=false  # 私有部署不上传到 Rekor
        $IMAGE
  only:
    - main  # 只对主分支构建的镜像签名

deploy:
  stage: deploy
  script:
    # 部署前验证签名
    - cosign verify --key $COSIGN_PUBLIC_KEY $IMAGE
    - kubectl set image deployment/myapp app=$IMAGE
  environment:
    name: production
  only:
    - main
```

## SLSA 框架简介

SLSA（Supply-chain Levels for Software Artifacts，发音 "salsa"）是 Google 提出的供应链安全框架，定义了 4 个等级：

- **SLSA 1**：有构建过程的文档和 Provenance（证明是哪个系统构建的）
- **SLSA 2**：用版本控制的脚本构建，有可验证的 Provenance
- **SLSA 3**：构建平台本身有安全保证，Provenance 防篡改
- **SLSA 4**：两人审查，密封构建环境，所有依赖可复现

大多数团队做到 SLSA 2-3 就已经显著降低风险。GitHub Actions 提供了官方的 SLSA Provenance 生成 Action（`slsa-framework/slsa-github-generator`），生成的 Provenance 可以用 Cosign 附加到镜像上。

## 踩坑记录

**Trivy DB 更新频率**

Trivy 每次扫描会下载最新漏洞数据库（约 200MB），在 CI 里每次都下载很慢。建议把 Trivy DB 缓存到 CI Cache 或者使用 `--skip-db-update` 配合定期更新的 Registry Mirror：

```bash
# 缓存 Trivy DB
trivy image --cache-dir .trivy-cache --download-db-only
# 后续扫描用本地 DB
trivy image --cache-dir .trivy-cache --skip-db-update $IMAGE
```

**私有镜像仓库认证**

Cosign 签名私有仓库镜像时需要先 `docker login`，或者通过环境变量传递凭证：

```bash
export REGISTRY_USERNAME=user
export REGISTRY_PASSWORD=pass
cosign sign --key cosign.key registry.example.com/myapp:latest
```

**Cosign Keyless 在内网的问题**

Keyless 模式依赖 `fulcio.sigstore.dev` 和 `rekor.sigstore.dev` 两个公网服务。内网或离线环境需要自建 Sigstore 服务栈（sigstore/scaffolding 项目提供了 Helm chart），或者改用传统密钥对模式。

**ClusterImagePolicy glob 匹配**

Policy Controller 的 glob 匹配规则：`myregistry.com/myapp/**` 匹配所有子路径，但 `myregistry.com/myapp/*` 只匹配一层。很容易因为镜像路径层级问题导致策略不生效，建议部署后用一个未签名镜像测试是否确实被拒绝。
