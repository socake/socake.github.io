---
title: "DevSecOps 安全左移实践：从代码到生产的全链路安全"
date: 2025-08-20T10:30:00+08:00
draft: false
tags: ["DevSecOps", "安全", "CI/CD", "容器安全", "供应链安全"]
categories: ["DevOps"]
description: "系统梳理 DevSecOps 安全左移的核心理念与落地路径，覆盖 SAST/SCA/镜像扫描/K8s 加固/供应链安全/密钥管理，给出一条可直接复用的流水线设计方案"
summary: "安全不是最后一道关卡，而是嵌入每个研发环节的连续过程。本文从代码静态分析、依赖漏洞扫描、镜像安全、K8s 运行时防护到供应链签名，逐层拆解 DevSecOps 的完整实施路径，并给出一个可落地的流水线设计。"
toc: true
math: false
diagram: false
keywords: ["DevSecOps", "安全左移", "SAST", "SCA", "Trivy", "Cosign", "Kyverno", "Vault", "kube-bench", "容器安全"]
params:
  reading_time: true
---

传统研发流程里，安全测试往往排在最后——等所有功能开发完毕，才交给安全团队做渗透测试。结果要么是上线前发现一堆高危漏洞，要么是"先上线再说"，安全变成摆设。DevSecOps 的核心思想很简单：**安全左移**，把安全检查前置到每一个研发阶段，而不是堆在末尾。

这篇文章不讲概念，只讲落地。我们会从代码阶段一路走到生产，覆盖每个环节的工具选型和配置细节，最后给出一条完整的安全流水线设计。

## DevSecOps 核心理念

### 安全左移意味着什么

"左移"来自研发生命周期的时间轴：代码编写在左，生产部署在右。安全左移意味着在时间轴上尽量靠左发现问题——在开发者的 IDE 里、在 commit 钩子里、在 CI 流水线里，而不是等到生产环境才暴露漏洞。

发现漏洞的成本与阶段密切相关。根据 IBM 的研究数据，生产环境修复一个漏洞的成本是开发阶段的 30 倍以上。原因很直观：越晚发现，需要回滚的代码越多，影响的系统越广，修复的协调成本越高。

### 安全即代码

安全规则本身也应该版本化管理。OPA 策略、Kyverno Policy、Semgrep 规则文件、Vault 的 Secret 路径定义——都应该存在 Git 仓库里，跟代码一起 Review，一起测试，一起部署。这样才能避免"安全配置漂移"：K8s 集群里某个命名空间悄悄去掉了 Pod Security Policy，没有人知道，也没有告警。

### 每个阶段的安全门禁

一个典型的 DevSecOps 流水线包含以下阶段：

```
代码提交 → SAST 扫描 → 依赖漏洞扫描(SCA) → 构建镜像 → 镜像漏洞扫描
       → 镜像签名 → 部署到 Staging → 动态扫描(DAST) → 合规检查 → 生产部署
```

每个阶段都有对应的"门禁"：发现高危问题则阻断流水线，强制修复后才能继续。门禁的严格程度可以分级，比如 HIGH 级别漏洞阻断，MEDIUM 级别警告，LOW 级别仅记录。

---

## 代码阶段：SAST 静态扫描

### SAST 能检查什么

静态应用安全测试（SAST）分析源代码，不需要运行程序。能发现：
- SQL 注入、XSS、路径遍历等经典注入漏洞
- 硬编码密钥（API Key、密码写死在代码里）
- 不安全的加密算法（MD5、SHA1 用于密码散列）
- 权限提升、不安全的反序列化
- 错误处理不当导致的信息泄露

### SonarQube 集成 CI

SonarQube 是企业级 SAST 工具，支持 30+ 种语言，有丰富的规则集和可视化界面。

**在 GitLab CI 中集成：**

```yaml
sonar-scan:
  image: sonarsource/sonar-scanner-cli:latest
  stage: security
  variables:
    SONAR_USER_HOME: "${CI_PROJECT_DIR}/.sonar"
    GIT_DEPTH: "0"
  cache:
    key: "${CI_JOB_NAME}"
    paths:
      - .sonar/cache
  script:
    - sonar-scanner
        -Dsonar.projectKey=${CI_PROJECT_NAME}
        -Dsonar.sources=.
        -Dsonar.host.url=${SONAR_HOST_URL}
        -Dsonar.login=${SONAR_TOKEN}
        -Dsonar.qualitygate.wait=true
  rules:
    - if: '$CI_PIPELINE_SOURCE == "merge_request_event"'
    - if: '$CI_COMMIT_BRANCH == "main"'
```

关键参数 `sonar.qualitygate.wait=true` 让 SonarQube 等待质量门禁结果，不通过则 CI 失败。Quality Gate 建议配置：

- 新代码覆盖率 ≥ 80%
- 新代码重复率 ≤ 3%
- 安全热点审查率 = 100%
- 无新增 BLOCKER 或 CRITICAL 级别问题

### Semgrep：轻量级可定制规则引擎

SonarQube 重，适合有专职安全团队维护的场景。Semgrep 更轻量，规则用 YAML 写，适合在 CI 里快速运行，也适合团队自定义规则。

**安装并运行：**

```bash
pip install semgrep

# 使用官方规则集扫描
semgrep --config=p/security-audit --config=p/owasp-top-ten .

# 只扫描特定语言
semgrep --config=p/python --config=p/django .
```

**自定义规则示例**——检测硬编码的 AWS 密钥：

```yaml
rules:
  - id: hardcoded-aws-key
    patterns:
      - pattern: |
          $KEY = "AKIA..."
    message: "检测到硬编码的 AWS Access Key，请使用环境变量或 Vault"
    languages: [python, go, javascript]
    severity: ERROR
    metadata:
      category: security
      cwe: "CWE-798"
```

**GitHub Actions 集成：**

```yaml
name: Semgrep
on:
  pull_request: {}
  push:
    branches: [main, develop]

jobs:
  semgrep:
    name: Semgrep Scan
    runs-on: ubuntu-latest
    container:
      image: semgrep/semgrep
    steps:
      - uses: actions/checkout@v4
      - name: Run Semgrep
        run: |
          semgrep ci \
            --config=p/security-audit \
            --config=p/secrets \
            --error \
            --json-output=semgrep-results.json
        env:
          SEMGREP_APP_TOKEN: ${{ secrets.SEMGREP_APP_TOKEN }}
      - name: Upload Results
        uses: actions/upload-artifact@v3
        if: always()
        with:
          name: semgrep-results
          path: semgrep-results.json
```

### Pre-commit 钩子：在本地就拦截

最早的安全左移是在开发者本地。用 `pre-commit` 框架，提交代码前自动运行扫描：

```yaml
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/gitleaks/gitleaks
    rev: v8.18.0
    hooks:
      - id: gitleaks
        name: 检测敏感信息泄露

  - repo: https://github.com/returntocorp/semgrep
    rev: v1.45.0
    hooks:
      - id: semgrep
        args: ['--config=p/secrets', '--error']

  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: v4.5.0
    hooks:
      - id: detect-private-key
      - id: detect-aws-credentials
```

---

## 依赖阶段：SCA 开源组件扫描

### 开源组件的风险

现代应用 70-90% 的代码来自开源依赖。Log4Shell（CVE-2021-44228）就是最典型的例子——Java 应用广泛使用的日志库 log4j 存在 RCE 漏洞，几乎影响了所有 Java 生态的软件。SCA（软件成分分析）就是解决这类问题的。

### OWASP Dependency-Check

开源免费，支持 Java、.NET、Python、Node.js 等多种语言，数据来源是 NVD（National Vulnerability Database）。

**Maven 项目集成：**

```xml
<plugin>
  <groupId>org.owasp</groupId>
  <artifactId>dependency-check-maven</artifactId>
  <version>9.0.7</version>
  <configuration>
    <failBuildOnCVSS>7</failBuildOnCVSS>
    <format>HTML</format>
    <format>JSON</format>
    <outputDirectory>${project.build.directory}/dependency-check-report</outputDirectory>
  </configuration>
</plugin>
```

`failBuildOnCVSS=7` 表示 CVSS 评分 ≥ 7.0（HIGH）的漏洞会导致构建失败。

**CLI 扫描 Python 项目：**

```bash
dependency-check.sh \
  --project "my-app" \
  --scan requirements.txt \
  --format HTML \
  --out ./reports \
  --failOnCVSS 7 \
  --enableRetired
```

### Snyk：更智能的 SCA 工具

Snyk 的优势在于漏洞数据库比 NVD 更新更快，还能给出修复建议（升级到哪个版本可以修复）。

**CI 集成：**

```yaml
snyk-test:
  stage: security
  image: snyk/snyk:node
  script:
    - snyk auth ${SNYK_TOKEN}
    - snyk test --severity-threshold=high --json > snyk-results.json || true
    - snyk monitor --project-name=${CI_PROJECT_NAME}
  artifacts:
    reports:
      sast: snyk-results.json
    when: always
```

**Go 模块扫描：**

```bash
snyk test --file=go.mod --severity-threshold=high
```

Snyk 还支持 IaC 扫描，可以检查 Terraform、Kubernetes YAML 中的安全配置问题：

```bash
snyk iac test ./k8s/ --severity-threshold=medium
```

### 依赖更新自动化：Dependabot

发现漏洞还不够，还要能自动提 PR 修复。GitHub Dependabot 可以自动检测依赖更新并提 PR：

```yaml
# .github/dependabot.yml
version: 2
updates:
  - package-ecosystem: "gomod"
    directory: "/"
    schedule:
      interval: "weekly"
    open-pull-requests-limit: 10
    labels:
      - "dependencies"
      - "security"

  - package-ecosystem: "docker"
    directory: "/"
    schedule:
      interval: "weekly"
```

---

## 镜像阶段：容器镜像安全扫描

### 为什么镜像扫描不可省

即使代码本身没有漏洞，容器基础镜像也可能带来风险。一个基于 `ubuntu:20.04` 的镜像里可能包含几十个已知 CVE，其中不乏高危漏洞。镜像扫描的意义在于：在镜像推送到 Registry 之前，或在部署之前，拦截高危漏洞。

### Trivy：最流行的容器扫描工具

Trivy 由 Aqua Security 开源，扫描速度快，支持容器镜像、文件系统、Git 仓库、Kubernetes 资源，是目前最主流的选择。

**本地快速扫描：**

```bash
# 扫描镜像
trivy image nginx:1.25

# 只报告 HIGH 和 CRITICAL
trivy image --severity HIGH,CRITICAL nginx:1.25

# 输出 JSON 格式
trivy image --format json --output trivy-report.json myapp:latest

# 扫描本地文件系统（扫描依赖文件）
trivy fs --security-checks vuln,secret .
```

**在 CI 中集成并设置阻断条件：**

```yaml
trivy-scan:
  stage: security
  image:
    name: aquasec/trivy:latest
    entrypoint: [""]
  variables:
    IMAGE: "${CI_REGISTRY_IMAGE}:${CI_COMMIT_SHORT_SHA}"
    TRIVY_NO_PROGRESS: "true"
    TRIVY_CACHE_DIR: ".trivycache/"
  cache:
    paths:
      - .trivycache/
  script:
    # 先构建镜像
    - docker build -t ${IMAGE} .
    # 扫描并在有 CRITICAL 漏洞时退出码非零
    - trivy image
        --exit-code 0
        --severity LOW,MEDIUM
        --format table
        ${IMAGE}
    - trivy image
        --exit-code 1
        --severity HIGH,CRITICAL
        --ignore-unfixed
        --format json
        --output trivy-critical.json
        ${IMAGE}
  artifacts:
    reports:
      container_scanning: trivy-critical.json
    when: always
```

两次扫描的设计：LOW/MEDIUM 只打印不阻断（`--exit-code 0`），HIGH/CRITICAL 则直接失败（`--exit-code 1`）。`--ignore-unfixed` 过滤掉暂无修复版本的漏洞，减少误报噪音。

**最小化基础镜像策略：**

选择合适的基础镜像可以从源头减少攻击面：

```dockerfile
# 避免：使用完整的 Ubuntu/Debian
FROM ubuntu:22.04

# 推荐：使用 distroless 镜像（Google 维护，只包含运行时）
FROM gcr.io/distroless/static-debian12

# 或使用 Alpine（极小体积，但注意 musl libc 的兼容性）
FROM alpine:3.19

# Go 应用的最佳实践：多阶段构建 + distroless
FROM golang:1.22 AS builder
WORKDIR /app
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -o server .

FROM gcr.io/distroless/static-debian12
COPY --from=builder /app/server /server
USER nonroot:nonroot
ENTRYPOINT ["/server"]
```

使用 distroless 镜像后，Trivy 扫描结果通常从几十个 CVE 降到个位数甚至零。

### Harbor 集成 Trivy 自动扫描

如果使用 Harbor 作为私有 Registry，可以配置在镜像推送后自动触发 Trivy 扫描，并通过 Webhook 阻止带有高危漏洞的镜像被拉取。

```yaml
# Harbor 扫描策略（通过 API 配置）
scan_all_policy:
  type: scheduled
  parameter:
    schedule:
      cron: "0 0 * * *"
      type: Custom

# 阻止高危镜像部署的 CVE 白名单策略
cve_allowlist:
  items:
    - cve_id: "CVE-2023-XXXXX"  # 已评估的可接受风险
```

---

## 运行阶段：Kubernetes 安全加固

### Pod Security Admission

Kubernetes 1.25 正式移除了 PodSecurityPolicy，替代方案是 Pod Security Admission（PSA）。PSA 在命名空间级别强制执行安全标准，有三个策略级别：
- `privileged`：无限制（通常只给基础设施命名空间）
- `baseline`：禁止已知的高危配置（禁止 hostPID、hostIPC、privileged 容器等）
- `restricted`：最严格，要求只读文件系统、非 root 运行、禁止特权提升

通过命名空间标签启用：

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: production
  labels:
    # enforce: 违规则拒绝 Pod 创建
    # audit: 违规则记录审计日志但不拒绝
    # warn: 违规则向用户发出警告
    pod-security.kubernetes.io/enforce: restricted
    pod-security.kubernetes.io/enforce-version: v1.28
    pod-security.kubernetes.io/audit: restricted
    pod-security.kubernetes.io/warn: restricted
```

### SecurityContext 最佳实践

每个 Deployment 都应该配置完整的 SecurityContext：

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: my-app
spec:
  template:
    spec:
      # Pod 级别：禁止以 root 运行
      securityContext:
        runAsNonRoot: true
        runAsUser: 10001
        runAsGroup: 10001
        fsGroup: 10001
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: app
          image: myapp:latest
          # 容器级别安全配置
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop:
                - ALL
              add:
                - NET_BIND_SERVICE  # 仅在需要绑定 1024 以下端口时添加
          # 挂载可写目录（如果应用需要写临时文件）
          volumeMounts:
            - name: tmp
              mountPath: /tmp
      volumes:
        - name: tmp
          emptyDir: {}
```

### Seccomp 配置文件

Seccomp（Secure Computing Mode）可以限制容器内进程允许使用的系统调用，大幅减少内核攻击面。

`RuntimeDefault` 是 containerd/Docker 默认提供的安全配置，已经屏蔽了大量不常用的危险系统调用。对于安全要求更高的场景，可以自定义：

```json
{
  "defaultAction": "SCMP_ACT_ERRNO",
  "architectures": ["SCMP_ARCH_X86_64"],
  "syscalls": [
    {
      "names": [
        "accept4", "access", "arch_prctl", "bind", "brk",
        "clone", "close", "connect", "epoll_create1", "epoll_ctl",
        "epoll_wait", "exit", "exit_group", "fchown", "fcntl",
        "fstat", "futex", "getdents64", "getpid", "getuid",
        "listen", "mmap", "mprotect", "munmap", "nanosleep",
        "openat", "read", "recvfrom", "sendto", "setuid",
        "socket", "stat", "write"
      ],
      "action": "SCMP_ACT_ALLOW"
    }
  ]
}
```

将此文件放在 `/var/lib/kubelet/seccomp/profiles/my-app.json`，然后在 Pod 中引用：

```yaml
securityContext:
  seccompProfile:
    type: Localhost
    localhostProfile: profiles/my-app.json
```

### AppArmor

AppArmor 是 Linux 的强制访问控制系统，可以限制进程的文件系统访问、网络访问和能力：

```
# /etc/apparmor.d/my-container
#include <tunables/global>

profile my-container flags=(attach_disconnected,mediate_deleted) {
  #include <abstractions/base>

  network inet tcp,
  network inet udp,

  # 允许读取应用目录
  /app/** r,
  /app/server ix,

  # 允许写入临时目录
  /tmp/** rw,

  # 拒绝写入其他位置
  deny / rw,
  deny /etc/** w,
  deny /usr/** w,
}
```

在 Pod 中通过注解启用：

```yaml
metadata:
  annotations:
    container.apparmor.security.beta.kubernetes.io/app: localhost/my-container
```

### Network Policy：微分段

默认情况下，K8s 集群内所有 Pod 可以互相通信。Network Policy 实现微分段，最小化爆炸半径：

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: backend-policy
  namespace: production
spec:
  podSelector:
    matchLabels:
      app: backend
  policyTypes:
    - Ingress
    - Egress
  ingress:
    # 只允许来自 frontend 的流量
    - from:
        - podSelector:
            matchLabels:
              app: frontend
      ports:
        - protocol: TCP
          port: 8080
  egress:
    # 只允许访问数据库
    - to:
        - podSelector:
            matchLabels:
              app: postgres
      ports:
        - protocol: TCP
          port: 5432
    # 允许 DNS 查询
    - to:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: kube-system
      ports:
        - protocol: UDP
          port: 53
```

---

## 供应链安全：Cosign 镜像签名

### 供应链攻击的威胁

SolarWinds 事件让整个行业开始重视软件供应链安全。在容器化场景下，供应链攻击可能通过以下方式发生：
- 篡改基础镜像（在 Registry 上替换合法镜像）
- 污染 CI/CD 流水线（在构建过程中注入恶意代码）
- 依赖包投毒（发布同名恶意包）

Cosign 是 Sigstore 项目的核心工具，用于对容器镜像进行签名和验证，确保镜像的完整性和来源可信。

### Cosign 签名流程

**生成密钥对：**

```bash
cosign generate-key-pair
# 生成 cosign.key（私钥）和 cosign.pub（公钥）
# 私钥存入 CI 密钥管理系统，公钥公开
```

**在 CI 中签名镜像：**

```yaml
sign-image:
  stage: sign
  image: gcr.io/projectsigstore/cosign:v2.2.0
  needs: ["trivy-scan"]  # 必须扫描通过才能签名
  script:
    - IMAGE="${CI_REGISTRY_IMAGE}:${CI_COMMIT_SHORT_SHA}"
    - cosign sign
        --key env://COSIGN_PRIVATE_KEY
        --annotations "git-commit=${CI_COMMIT_SHA}"
        --annotations "pipeline-id=${CI_PIPELINE_ID}"
        --annotations "build-time=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        ${IMAGE}
  environment:
    name: production
```

**验证镜像签名：**

```bash
cosign verify \
  --key cosign.pub \
  --annotations "git-commit=abc123" \
  myregistry.com/myapp:v1.0.0
```

### Kyverno 强制验证签名

签名有了，但如何确保只有经过签名的镜像才能部署？Kyverno 是 K8s 原生的策略引擎，可以在 Admission 阶段拦截未签名镜像：

```yaml
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: verify-image-signature
spec:
  validationFailureAction: Enforce
  background: false
  rules:
    - name: check-image-signature
      match:
        any:
          - resources:
              kinds: [Pod]
              namespaces: [production, staging]
      verifyImages:
        - imageReferences:
            - "myregistry.com/myapp/*"
          attestors:
            - count: 1
              entries:
                - keys:
                    publicKeys: |-
                      -----BEGIN PUBLIC KEY-----
                      MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE...
                      -----END PUBLIC KEY-----
```

这个策略强制要求所有部署到 `production` 和 `staging` 命名空间的 Pod，其镜像必须有对应的 Cosign 签名，否则拒绝创建。

### SBOM：软件物料清单

Cosign 还支持附加 SBOM（Software Bill of Materials），记录镜像中包含的所有软件组件：

```bash
# 使用 Syft 生成 SBOM
syft myapp:latest -o spdx-json > sbom.json

# 将 SBOM 附加到镜像（存储在 OCI Registry）
cosign attach sbom --sbom sbom.json myapp:latest

# 验证并下载 SBOM
cosign download sbom myapp:latest
```

---

## 密钥管理：Vault + External Secrets

### 密钥管理的核心原则

任何环境变量、配置文件、CI 流水线变量中都不应该存明文密钥。正确做法：
1. 密钥集中存储在 Vault
2. CI/CD 运行时动态获取
3. K8s Pod 通过 External Secrets Operator 注入

### Vault 在 CI/CD 中的集成

**GitLab CI 使用 JWT 认证（推荐）：**

```yaml
deploy:
  stage: deploy
  id_tokens:
    VAULT_ID_TOKEN:
      aud: https://vault.company.com
  secrets:
    DATABASE_PASSWORD:
      vault: production/data/database#password
      file: false
    AWS_ACCESS_KEY:
      vault: production/data/aws#access_key
      file: false
  script:
    - echo "DATABASE_PASSWORD is available as env var"
    - ./deploy.sh
```

Vault 端配置 JWT 认证：

```bash
# 启用 JWT 认证
vault auth enable jwt

# 配置 GitLab 的 JWT
vault write auth/jwt/config \
  jwks_url="https://gitlab.company.com/-/jwks" \
  bound_issuer="https://gitlab.company.com"

# 创建角色
vault write auth/jwt/role/gitlab-deploy \
  role_type="jwt" \
  bound_audiences="https://vault.company.com" \
  bound_claims='{"project_path": "mygroup/myapp"}' \
  user_claim="project_path" \
  policies="production-deploy" \
  ttl="1h"
```

### External Secrets Operator：K8s 密钥注入

ESO 在 K8s 中监听 ExternalSecret 资源，自动从 Vault 拉取密钥并创建 K8s Secret：

```yaml
apiVersion: external-secrets.io/v1beta1
kind: SecretStore
metadata:
  name: vault-backend
  namespace: production
spec:
  provider:
    vault:
      server: "https://vault.company.com"
      path: "secret"
      version: "v2"
      auth:
        kubernetes:
          mountPath: "Kubernetes"
          role: "production-app"
---
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: app-secrets
  namespace: production
spec:
  refreshInterval: 1h
  secretStoreRef:
    name: vault-backend
    kind: SecretStore
  target:
    name: app-secrets
    creationPolicy: Owner
  data:
    - secretKey: database-password
      remoteRef:
        key: production/database
        property: password
    - secretKey: jwt-secret
      remoteRef:
        key: production/app
        property: jwt_secret
```

密钥会自动同步到 K8s Secret `app-secrets`，Pod 通过 `envFrom` 或 `volumeMounts` 使用，不需要接触 Vault API。

### 密钥轮换

ESO 的 `refreshInterval` 确保密钥自动续期。Vault 的 Dynamic Secrets 可以更进一步——每次请求生成一次性密钥：

```bash
# 为数据库配置动态密钥
vault write database/config/mydb \
  plugin_name=mysql-database-plugin \
  connection_url="{{username}}:{{password}}@tcp(mysql:3306)/" \
  allowed_roles="app-role" \
  username="vault-admin" \
  password="vault-admin-password"

vault write database/roles/app-role \
  db_name=mydb \
  creation_statements="CREATE USER '{{name}}'@'%' IDENTIFIED BY '{{password}}'; GRANT SELECT, INSERT, UPDATE ON mydb.* TO '{{name}}'@'%';" \
  default_ttl="1h" \
  max_ttl="24h"
```

每次 Pod 启动，ESO 向 Vault 请求一个新的数据库账号，TTL 到期后自动吊销。

---

## 合规扫描：CIS Benchmark

### kube-bench

kube-bench 是 Aqua Security 开源的工具，按照 CIS Kubernetes Benchmark 自动检查集群配置。

**在 K8s 集群内运行扫描：**

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: kube-bench
  namespace: security
spec:
  template:
    spec:
      hostPID: true
      nodeSelector:
        node-role.kubernetes.io/control-plane: ""
      tolerations:
        - key: node-role.kubernetes.io/control-plane
          effect: NoSchedule
      containers:
        - name: kube-bench
          image: aquasec/kube-bench:v0.7.3
          command: ["kube-bench"]
          args: ["run", "--targets", "master", "--json"]
          volumeMounts:
            - name: var-lib-etcd
              mountPath: /var/lib/etcd
              readOnly: true
            - name: etc-kubernetes
              mountPath: /etc/kubernetes
              readOnly: true
      restartPolicy: Never
      volumes:
        - name: var-lib-etcd
          hostPath:
            path: /var/lib/etcd
        - name: etc-kubernetes
          hostPath:
            path: /etc/kubernetes
```

**关键检查项：**
- etcd 是否启用 TLS 双向认证
- API Server 是否禁用了匿名认证
- RBAC 是否启用
- audit log 是否配置
- kubelet 是否禁用了匿名访问

**集成到监控告警：**

```bash
# 将 kube-bench 结果推送到 Prometheus
kube-bench run --json | \
  jq -r '.Controls[] | .tests[] | .results[] | select(.status=="FAIL") | 
  "kube_bench_fail{id=\"\(.test_number)\",desc=\"\(.test_desc)\"} 1"' | \
  curl --data-binary @- http://pushgateway:9091/metrics/job/kube-bench
```

---

## 完整 DevSecOps 流水线设计

把以上所有环节串联起来，一条完整的安全流水线如下：

```yaml
# .gitlab-ci.yml - 完整 DevSecOps 流水线

stages:
  - lint
  - security-sast
  - security-sca
  - build
  - security-image
  - sign
  - deploy-staging
  - security-dast
  - deploy-production

variables:
  IMAGE: "${CI_REGISTRY_IMAGE}:${CI_COMMIT_SHORT_SHA}"

# ===== 阶段1：代码静态分析 =====
semgrep:
  stage: security-sast
  image: semgrep/semgrep
  script:
    - semgrep ci --config=p/security-audit --config=p/secrets --error
  rules:
    - if: '$CI_PIPELINE_SOURCE == "merge_request_event"'

sonarqube:
  stage: security-sast
  script:
    - sonar-scanner -Dsonar.qualitygate.wait=true
  allow_failure: false

# ===== 阶段2：依赖漏洞扫描 =====
snyk-sca:
  stage: security-sca
  image: snyk/snyk:node
  script:
    - snyk auth ${SNYK_TOKEN}
    - snyk test --severity-threshold=high
    - snyk monitor
  allow_failure: false

# ===== 阶段3：构建镜像 =====
build:
  stage: build
  script:
    - docker build -t ${IMAGE} .
    - docker push ${IMAGE}

# ===== 阶段4：镜像漏洞扫描 =====
trivy:
  stage: security-image
  image: aquasec/trivy:latest
  script:
    - trivy image --exit-code 1 --severity HIGH,CRITICAL --ignore-unfixed ${IMAGE}
  allow_failure: false

# ===== 阶段5：镜像签名 =====
cosign-sign:
  stage: sign
  needs: ["trivy"]
  image: gcr.io/projectsigstore/cosign:v2.2.0
  script:
    - cosign sign --key env://COSIGN_PRIVATE_KEY ${IMAGE}

# ===== 阶段6：部署 Staging =====
deploy-staging:
  stage: deploy-staging
  script:
    - kubectl set image deployment/myapp app=${IMAGE} -n staging
    - kubectl rollout status deployment/myapp -n staging --timeout=5m

# ===== 阶段7：动态安全测试 =====
zap-dast:
  stage: security-dast
  image: owasp/zap2docker-stable
  script:
    - zap-baseline.py -t https://staging.myapp.com -J zap-report.json
    - python check-zap-results.py zap-report.json  # 解析结果，高危则失败
  artifacts:
    reports:
      dast: zap-report.json

# ===== 阶段8：生产部署（需人工审批）=====
deploy-production:
  stage: deploy-production
  when: manual
  script:
    - kubectl set image deployment/myapp app=${IMAGE} -n production
    - kubectl rollout status deployment/myapp -n production --timeout=10m
  environment:
    name: production
```

### 门禁策略总结

| 阶段 | 工具 | 阻断条件 | 告警 |
|------|------|----------|------|
| 代码扫描 | Semgrep | 任何 ERROR 级别规则 | Slack |
| 代码质量 | SonarQube | Quality Gate 不通过 | Slack |
| 依赖扫描 | Snyk | CVSS ≥ 7.0 | Jira 工单 |
| 镜像扫描 | Trivy | HIGH/CRITICAL 且有修复版本 | Slack + 邮件 |
| 运行时 | Kyverno | 未签名镜像 | K8s Event |
| 合规 | kube-bench | CIS 检查项 FAIL 超过阈值 | PagerDuty |

### 落地注意事项

**渐进式引入，不要一刀切。** 第一周先跑扫描但不阻断，收集现有代码库的漏洞基线。第二周对新增代码启用阻断。第三周再逐步要求修复存量漏洞。直接把一堆严格规则扔给团队，只会制造对立情绪。

**维护误报白名单。** 每个工具都有误报。Semgrep 规则可能误触业务逻辑，Trivy 可能报告实际不可利用的漏洞。建立白名单流程：安全团队 Review 后，可以将特定问题加入白名单并记录理由和到期时间。

**安全指标可视化。** 把扫描结果推到 Grafana 仪表盘：每周新增漏洞数、修复平均时间（MTTR）、各严重级别漏洞趋势。数据可见，才能驱动改进。

DevSecOps 不是一次性项目，而是持续运营的能力建设。工具只是载体，关键是让安全意识渗透到每个工程师的日常工作中——当开发者在 IDE 里就能看到安全提示，当 PR Review 自动附上扫描报告，安全才真正成为研发文化的一部分。
