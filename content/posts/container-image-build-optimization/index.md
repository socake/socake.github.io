---
title: "容器镜像构建优化：BuildKit、多阶段构建与供应链安全"
date: 2026-03-18T10:00:00+08:00
draft: false
tags: ["Docker", "BuildKit", "容器安全", "CI/CD", "供应链安全", "多阶段构建"]
categories: ["容器化"]
description: "从构建速度、镜像体积到供应链安全，系统讲解生产级容器镜像构建优化实战，涵盖 BuildKit 特性、多阶段构建模板、缓存策略、Distroless 选型、SBOM 生成与 Cosign 签名验证。"
summary: "深入剖析容器镜像构建优化的每个环节：BuildKit 并行构建与 Secrets 注入、Go/Python/Node.js 多阶段 Dockerfile 模板、--mount=type=cache 与远程缓存、Distroless vs Alpine 选型、dive 分析层内容，以及完整的供应链安全闭环（syft SBOM + Cosign 签名 + K8s 准入控制验签）。"
toc: true
math: false
diagram: false
keywords: ["BuildKit", "多阶段构建", "Distroless", "Cosign", "SBOM", "供应链安全", "docker buildx", "镜像优化"]
params:
  reading_time: true
---

## 为什么要认真对待镜像构建

很多团队把镜像构建当作一个"能跑就行"的环节，直到遇到以下问题才开始重视：

- CI 流水线构建耗时 8 分钟，每次代码改一行都要全量重建依赖
- 生产镜像 1.2GB，拉取时间拖慢节点启动速度
- 审计发现镜像里有 47 个高危 CVE，其中一半来自构建工具链
- 供应链攻击：有人推了一个恶意镜像覆盖了 `latest` tag

本文聚焦四个核心指标：**构建时间、镜像大小、缓存命中率、安全性**，用实际可落地的方案系统解决这些问题。

---

## BuildKit：不只是"更快的 docker build"

Docker 18.09 引入 BuildKit，Docker 23.0 起默认启用。BuildKit 不是简单的性能提升，它重构了整个构建执行引擎。

### 核心改进

**并行构建**：传统 docker build 串行执行每一条指令。BuildKit 将 Dockerfile 解析为有向无环图（DAG），独立的构建阶段可以并行执行。对于多阶段构建，构建时间可以从串行之和缩减为最长路径。

**更精细的缓存**：BuildKit 的缓存粒度到达指令级别，并引入了内容寻址缓存（content-addressable cache）。缓存键基于指令内容 + 依赖文件哈希，不再因为 Dockerfile 中某行无关注释的改动而失效整个缓存链。

**`--mount` 指令**：这是 BuildKit 最重要的特性之一，允许在构建时挂载：
- `type=cache`：持久化包管理器缓存，跨构建共享
- `type=secret`：安全注入敏感信息，不会出现在镜像层历史中
- `type=ssh`：转发 SSH agent，安全拉取私有 Git 仓库

**内联 Dockerfile 语法版本**：通过 `# syntax=docker/dockerfile:1` 指定解析器版本，可以使用最新 BuildKit 特性而无需升级 Docker。

### 启用与配置

```bash
# Docker 23.0+ 已默认启用，旧版本手动启用
export DOCKER_BUILDKIT=1

# 或在 /etc/docker/daemon.json 中永久启用
{
  "features": {
    "buildkit": true
  }
}

# 查看 BuildKit 版本
docker buildx version
# github.com/docker/buildx v0.12.0 ...

# 创建支持多平台的 builder
docker buildx create --name mybuilder --driver docker-container --use
docker buildx inspect --bootstrap
```

### Secrets 安全注入

传统方式在构建时注入密钥会永久留在镜像层中：

```dockerfile
# 错误示例 - 密钥会进入镜像历史
ARG NPM_TOKEN
RUN echo "//registry.npmjs.org/:_authToken=${NPM_TOKEN}" > ~/.npmrc
```

BuildKit 的正确做法：

```dockerfile
# syntax=docker/dockerfile:1
FROM node:20-alpine AS builder
RUN --mount=type=secret,id=npm_token \
    NPM_TOKEN=$(cat /run/secrets/npm_token) \
    npm config set //registry.npmjs.org/:_authToken $NPM_TOKEN && \
    npm ci
```

构建时：

```bash
docker buildx build \
  --secret id=npm_token,src=./npm_token.txt \
  -t myapp:latest .
```

密钥只在 RUN 指令执行期间存在于内存中，不写入任何镜像层。

---

## 多阶段构建精讲

多阶段构建的核心思想：**构建时需要的工具，运行时不需要**。编译器、测试框架、调试工具统统留在构建阶段，最终镜像只包含运行时产物。

### Go 应用 Dockerfile

Go 的静态编译特性使其成为多阶段构建的理想场景，最终可以用 scratch 或 distroless。

```dockerfile
# syntax=docker/dockerfile:1
FROM golang:1.22-alpine AS deps
WORKDIR /app
# 先复制依赖声明文件，利用缓存层
COPY go.mod go.sum ./
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    go mod download

FROM deps AS builder
COPY . .
# CGO_ENABLED=0 生成纯静态二进制
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
    go build -ldflags="-w -s -X main.version=${VERSION}" \
    -trimpath \
    -o /app/server ./cmd/server

# 安全扫描阶段（可选，但推荐在 CI 中启用）
FROM aquasec/trivy:latest AS scanner
COPY --from=builder /app/server /app/server
RUN trivy rootfs --exit-code 1 --severity HIGH,CRITICAL /app/server

# 最终运行镜像使用 distroless
FROM gcr.io/distroless/static-debian12:nonroot AS runtime
COPY --from=builder /app/server /server
# distroless nonroot 使用 uid 65532
USER nonroot:nonroot
EXPOSE 8080
ENTRYPOINT ["/server"]
```

关键优化点：
- `-ldflags="-w -s"` 去除调试符号，减小二进制体积约 30%
- `-trimpath` 移除构建路径信息，提高可重现性
- `--mount=type=cache` 复用 Go 模块缓存和编译缓存

### Python 应用 Dockerfile

Python 的挑战在于依赖安装慢，且运行时需要 Python 解释器。

```dockerfile
# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=0 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

FROM base AS deps
WORKDIR /app
COPY requirements.txt .
# 使用 BuildKit 缓存挂载，pip 缓存跨构建持久化
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --prefix=/install -r requirements.txt

FROM base AS runtime
WORKDIR /app
# 只复制安装好的包，不包含 pip 本身
COPY --from=deps /install /usr/local
COPY src/ ./src/
# 创建非 root 用户
RUN groupadd --gid 1000 appuser && \
    useradd --uid 1000 --gid appuser --no-create-home appuser
USER appuser
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

对于使用 `uv` 的现代 Python 项目：

```dockerfile
# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project --no-dev

COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

FROM python:3.12-slim AS runtime
COPY --from=builder --chown=app:app /app /app
ENV PATH="/app/.venv/bin:$PATH"
WORKDIR /app
USER 1000
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0"]
```

### Node.js 应用 Dockerfile

Node.js 的 `node_modules` 通常是体积和安全问题的重灾区。

```dockerfile
# syntax=docker/dockerfile:1
FROM node:20-alpine AS base
RUN apk add --no-cache libc6-compat
WORKDIR /app

FROM base AS deps
COPY package.json package-lock.json ./
RUN --mount=type=cache,target=/root/.npm \
    npm ci --prefer-offline

FROM base AS builder
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

# 生产依赖（去除 devDependencies）
FROM base AS prod-deps
COPY package.json package-lock.json ./
RUN --mount=type=cache,target=/root/.npm \
    npm ci --omit=dev --prefer-offline

FROM base AS runtime
RUN addgroup --system --gid 1001 nodejs && \
    adduser --system --uid 1001 nextjs
COPY --from=builder /app/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static
USER nextjs
EXPOSE 3000
ENV NODE_ENV=production PORT=3000
CMD ["node", "server.js"]
```

---

## 缓存策略深度优化

### 依赖层与代码层分离

这是最基础也最重要的缓存优化原则。Docker 层缓存是基于"前面所有层都命中缓存"的前提，任何一层失效都会导致后续所有层重建。

```dockerfile
# 错误示例 - 代码变更会导致依赖重新安装
COPY . .
RUN npm ci

# 正确示例 - 依赖声明文件不变则复用缓存
COPY package.json package-lock.json ./
RUN npm ci
COPY src/ ./src/
COPY public/ ./public/
```

变更频率从低到高排列层的顺序：
1. 基础镜像（FROM）
2. 系统依赖安装（apt/apk）
3. 应用依赖声明文件（go.mod、package.json、requirements.txt）
4. 应用依赖安装（go mod download、npm ci）
5. 源代码（COPY . .）
6. 构建步骤（RUN go build）

### --mount=type=cache 实战

```dockerfile
# apt 包缓存
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl git

# Go 模块和编译缓存
RUN --mount=type=cache,target=/go/pkg/mod,sharing=shared \
    --mount=type=cache,target=/root/.cache/go-build,sharing=shared \
    go build ./...

# pip 缓存
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

# npm 缓存
RUN --mount=type=cache,target=/root/.npm \
    npm ci --prefer-offline

# Rust/cargo 缓存
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/app/target \
    cargo build --release
```

`sharing` 参数控制并发访问策略：
- `shared`：多个并发构建可以同时读写（适合只读的下载缓存）
- `locked`：同一时间只有一个构建可以访问（适合 apt 等有锁的场景）
- `private`：每个构建有独立副本

### 远程缓存：registry cache

在 CI 环境中，本地缓存无法跨 Runner 共享。Registry cache 是目前最通用的解决方案：

```bash
# 构建并推送缓存到 registry
docker buildx build \
  --cache-from type=registry,ref=registry.example.com/myapp:cache \
  --cache-to type=registry,ref=registry.example.com/myapp:cache,mode=max \
  --tag registry.example.com/myapp:latest \
  --push .
```

`mode=max` 会将所有中间层的缓存都推送到 registry（而不仅是最终阶段），对多阶段构建的缓存命中率提升显著。

GitHub Actions 中的完整配置：

```yaml
- name: Set up Docker Buildx
  uses: docker/setup-buildx-action@v3

- name: Build and push
  uses: docker/build-push-action@v5
  with:
    context: .
    platforms: linux/amd64,linux/arm64
    push: true
    tags: |
      registry.example.com/myapp:${{ github.sha }}
      registry.example.com/myapp:latest
    cache-from: type=registry,ref=registry.example.com/myapp:buildcache
    cache-to: type=registry,ref=registry.example.com/myapp:buildcache,mode=max
```

---

## 镜像最小化

### 基础镜像选型

| 基础镜像 | 压缩大小 | Shell | 包管理器 | 适用场景 |
|---------|---------|-------|---------|---------|
| ubuntu:24.04 | ~30MB | ✓ | apt | 需要完整工具链 |
| debian:bookworm-slim | ~30MB | ✓ | apt | 需要 glibc 但不要完整 debian |
| alpine:3.19 | ~3.5MB | ash | apk | 节点代理、工具类应用 |
| gcr.io/distroless/static | ~2MB | ✗ | ✗ | Go 静态二进制 |
| gcr.io/distroless/base | ~20MB | ✗ | ✗ | 需要 glibc 的应用 |
| gcr.io/distroless/python3 | ~52MB | ✗ | ✗ | Python 应用 |
| scratch | 0MB | ✗ | ✗ | 完全静态二进制 |

**Distroless vs Alpine 的选择**：

Alpine 使用 musl libc，与 glibc 存在兼容性问题，尤其是一些 C 扩展的 Python 包（如 numpy）在 Alpine 上需要重新编译。Distroless 基于 Debian，使用 glibc，兼容性更好。

对于 Go 应用，优先选 `distroless/static:nonroot`；需要调用系统库（如 CGO、DNS 解析）时用 `distroless/base:nonroot`；Python/Node.js 用对应语言的 distroless 变体。

Distroless 的 `nonroot` 变体内置了非 root 用户（uid 65532），无需在 Dockerfile 中手动创建用户。

### 用 dive 分析层内容

```bash
# 安装 dive
curl -OL https://github.com/wagoodman/dive/releases/download/v0.12.0/dive_0.12.0_linux_amd64.deb
dpkg -i dive_0.12.0_linux_amd64.deb

# 分析镜像
dive myapp:latest

# CI 模式：检查镜像效率（低于阈值则失败）
CI=true dive myapp:latest

# 关键指标：
# Image efficiency score: 95%  (越高越好)
# Potential wasted space: 12 MB  (越少越好)
```

常见的"浪费"来源：
- 同一层先 `apt-get install` 后又在不同层 `apt-get clean`
- 构建中间产物（`.o` 文件、测试文件）没有被清理
- 敏感文件（密钥、配置）虽然后来被删除但仍存在于历史层

修复方案：将清理操作合并到同一 RUN 指令：

```dockerfile
# 错误 - 包缓存在不同层
RUN apt-get update
RUN apt-get install -y build-essential
RUN apt-get clean  # 这层的清理不影响上面层的缓存

# 正确 - 同一层完成安装和清理
RUN apt-get update && \
    apt-get install -y --no-install-recommends build-essential && \
    rm -rf /var/lib/apt/lists/*
```

### 多平台构建

```bash
# 创建支持多平台的 builder（使用 QEMU 模拟）
docker buildx create --name multiplatform \
  --driver docker-container \
  --platform linux/amd64,linux/arm64 \
  --use

# 构建并推送多平台镜像
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  --tag myapp:latest \
  --push .
```

在 Dockerfile 中获取目标平台信息：

```dockerfile
FROM --platform=$BUILDPLATFORM golang:1.22 AS builder
ARG TARGETOS TARGETARCH
RUN GOOS=$TARGETOS GOARCH=$TARGETARCH go build -o /app/server .
```

`$BUILDPLATFORM` 是构建机器平台（用于构建工具），`$TARGETPLATFORM` 是目标平台（用于最终产物）。交叉编译比 QEMU 模拟快 10-50 倍，对于支持交叉编译的语言（Go、Rust）应优先使用。

---

## 供应链安全

### 固定 Base Image Digest

`FROM python:3.12-slim` 在不同时间构建可能拉到不同的镜像内容（tag 可以被覆盖）。固定 digest 保证构建可重现：

```bash
# 获取镜像 digest
docker buildx imagetools inspect python:3.12-slim
# 输出：Digest: sha256:abcd1234...

# 在 Dockerfile 中使用 digest 固定版本
FROM python:3.12-slim@sha256:4efa85de8db5704dc85b7b3d2d0ab8bd35e05f2c7cd9ebe05bb4a31df26bdd52
```

建议在 CI 中定期更新 digest（例如每周通过自动化 PR 更新 base image），既保证安全更新又不失可重现性。

### SBOM 生成（syft）

软件物料清单（SBOM）记录了镜像中所有软件组件的来源、版本和许可证，是供应链安全审计的基础。

```bash
# 安装 syft
curl -sSfL https://raw.githubusercontent.com/anchore/syft/main/install.sh | sh -s -- -b /usr/local/bin

# 生成 SBOM（SPDX 格式）
syft myapp:latest -o spdx-json > myapp-sbom.spdx.json

# 生成 CycloneDX 格式（更广泛支持）
syft myapp:latest -o cyclonedx-json > myapp-sbom.cdx.json

# 扫描 SBOM 中的漏洞（结合 grype）
grype sbom:./myapp-sbom.spdx.json

# 将 SBOM 作为 OCI artifact 附加到镜像（attestation）
syft attest --output spdx-json \
  --key cosign.key \
  registry.example.com/myapp:latest > myapp.att.json

cosign attest \
  --key cosign.key \
  --predicate myapp.att.json \
  --type https://spdx.dev/Document \
  registry.example.com/myapp:latest
```

### Cosign 镜像签名

Cosign 是 Sigstore 项目的核心工具，实现了无密钥（keyless）或基于密钥的镜像签名。

```bash
# 安装 cosign
brew install sigstore/tap/cosign
# 或
curl -O -L https://github.com/sigstore/cosign/releases/latest/download/cosign-linux-amd64
install -m 755 cosign-linux-amd64 /usr/local/bin/cosign

# 生成密钥对
cosign generate-key-pair
# 生成 cosign.key (私钥) 和 cosign.pub (公钥)

# 签名镜像（镜像必须已推送到 registry）
cosign sign --key cosign.key registry.example.com/myapp:latest

# 验证签名
cosign verify --key cosign.pub registry.example.com/myapp:latest

# Keyless 签名（利用 OIDC，适合 CI 环境）
# 在 GitHub Actions 中自动通过 OIDC 获取身份
COSIGN_EXPERIMENTAL=1 cosign sign registry.example.com/myapp:latest
```

CI/CD 完整签名流程（GitHub Actions）：

```yaml
- name: Sign the Docker image
  env:
    COSIGN_PRIVATE_KEY: ${{ secrets.COSIGN_PRIVATE_KEY }}
    COSIGN_PASSWORD: ${{ secrets.COSIGN_PASSWORD }}
  run: |
    cosign sign --key env://COSIGN_PRIVATE_KEY \
      registry.example.com/myapp:${{ github.sha }}

- name: Generate and attest SBOM
  run: |
    syft registry.example.com/myapp:${{ github.sha }} \
      -o cyclonedx-json > sbom.cdx.json
    cosign attest --key env://COSIGN_PRIVATE_KEY \
      --predicate sbom.cdx.json \
      --type cyclonedx \
      registry.example.com/myapp:${{ github.sha }}
```

### K8s 准入控制验签

在 Kubernetes 集群中，通过 Policy Controller（Sigstore 项目）或 Kyverno 实现准入时验签，阻止未签名或签名无效的镜像部署。

**方案一：Sigstore Policy Controller**

```bash
helm repo add sigstore https://sigstore.github.io/helm-charts
helm install policy-controller sigstore/policy-controller \
  --namespace cosign-system \
  --create-namespace
```

```yaml
# ClusterImagePolicy - 要求所有镜像必须有有效签名
apiVersion: policy.sigstore.dev/v1beta1
kind: ClusterImagePolicy
metadata:
  name: require-signed-images
spec:
  images:
    - glob: "registry.example.com/**"
  authorities:
    - key:
        data: |
          -----BEGIN PUBLIC KEY-----
          MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE...
          -----END PUBLIC KEY-----
```

**方案二：Kyverno**

```yaml
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: verify-image-signature
spec:
  validationFailureAction: Enforce
  rules:
    - name: check-signature
      match:
        any:
          - resources:
              kinds: [Pod]
              namespaces: [production]
      verifyImages:
        - imageReferences:
            - "registry.example.com/*"
          attestors:
            - count: 1
              entries:
                - keys:
                    publicKeys: |-
                      -----BEGIN PUBLIC KEY-----
                      MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE...
                      -----END PUBLIC KEY-----
```

---

## CI/CD 完整构建流水线

### GitHub Actions 完整示例

```yaml
name: Build and Push

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

env:
  REGISTRY: ghcr.io
  IMAGE_NAME: ${{ github.repository }}

jobs:
  build:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: write
      id-token: write  # 用于 keyless 签名

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Set up QEMU
        uses: docker/setup-qemu-action@v3

      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@v3
        with:
          driver-opts: |
            image=moby/buildkit:latest
            network=host

      - name: Login to GHCR
        uses: docker/login-action@v3
        with:
          registry: ${{ env.REGISTRY }}
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - name: Extract metadata
        id: meta
        uses: docker/metadata-action@v5
        with:
          images: ${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}
          tags: |
            type=sha,prefix={{branch}}-
            type=raw,value=latest,enable={{is_default_branch}}

      - name: Build and push
        id: build
        uses: docker/build-push-action@v5
        with:
          context: .
          platforms: linux/amd64,linux/arm64
          push: ${{ github.event_name != 'pull_request' }}
          tags: ${{ steps.meta.outputs.tags }}
          labels: ${{ steps.meta.outputs.labels }}
          cache-from: type=registry,ref=${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}:buildcache
          cache-to: type=registry,ref=${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}:buildcache,mode=max
          build-args: |
            VERSION=${{ github.sha }}
            BUILD_DATE=${{ github.event.head_commit.timestamp }}

      - name: Install cosign
        if: github.event_name != 'pull_request'
        uses: sigstore/cosign-installer@v3

      - name: Sign image with keyless
        if: github.event_name != 'pull_request'
        env:
          COSIGN_EXPERIMENTAL: "true"
        run: |
          cosign sign --yes \
            ${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}@${{ steps.build.outputs.digest }}

      - name: Run Trivy vulnerability scan
        uses: aquasecurity/trivy-action@master
        with:
          image-ref: ${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}@${{ steps.build.outputs.digest }}
          format: sarif
          output: trivy-results.sarif
          severity: CRITICAL,HIGH
          exit-code: "1"

      - name: Upload Trivy scan results
        uses: github/codeql-action/upload-sarif@v3
        if: always()
        with:
          sarif_file: trivy-results.sarif
```

---

## 优化效果对比

以一个典型的 Go Web 服务为例：

| 指标 | 优化前 | 优化后 | 改善 |
|------|--------|--------|------|
| 镜像大小 | 892MB | 18MB | -98% |
| 冷构建时间 | 4m32s | 3m15s | -28% |
| 热构建（只改代码） | 4m32s | 0m48s | -82% |
| CVE 高危数量 | 23 | 0 | -100% |
| 拉取时间（1Gbps） | 8.2s | 0.3s | -96% |

镜像从 ubuntu base + 完整 Go 工具链 → distroless/static，减少了 98% 的大小，同时彻底消除了来自 OS 和工具链的 CVE。

缓存命中率的提升是构建提速的关键：依赖层分离后，日常代码提交（go.mod 不变）的构建时间从 4.5 分钟降到不到 1 分钟。

---

## 总结

镜像构建优化是一个全链路工程：

1. **BuildKit + --mount=type=cache**：解决构建速度和缓存命中率
2. **多阶段构建 + 依赖层分离**：同时解决构建速度和镜像大小
3. **Distroless/scratch**：最小化运行时攻击面
4. **固定 digest + Trivy 扫描**：解决漏洞管理
5. **syft SBOM + cosign 签名 + K8s 验签**：构建完整供应链安全闭环

这些优化不是相互独立的，而是一个递进的体系。建议按照"先解决速度问题（缓存）→ 再解决大小问题（多阶段+基础镜像）→ 最后解决安全问题（供应链）"的顺序推进，每一步都有明确可量化的收益。
