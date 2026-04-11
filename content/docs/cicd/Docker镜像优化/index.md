---
title: "Docker 镜像优化实践"
date: 2025-12-09T14:00:00+08:00
draft: false
tags: ["Docker", "CI/CD", "镜像优化"]
categories: ["CI/CD"]
description: "从多阶段构建、基础镜像选型到 layer 缓存策略，系统性压缩 Docker 镜像体积，降低构建时间和安全风险"
summary: "覆盖多阶段构建、基础镜像选型（alpine/distroless/scratch）、layer 缓存优化、BuildKit cache mount、漏洞扫描等实战技巧，附优化前后对比数据。"
toc: true
math: false
diagram: false
keywords: ["docker", "镜像优化", "多阶段构建", "distroless", "trivy", "buildkit"]
params:
  reading_time: true
---

## 一、镜像大小为什么重要

很多团队把镜像大小当成无关紧要的小事，直到几个问题同时出现：

- **拉取速度**：冷启动场景（节点扩容、Pod 迁移）依赖镜像拉取速度。一个 1.5 GB 的镜像比 80 MB 的镜像慢 10–20 倍
- **存储成本**：ECR、Docker Hub 按存储量计费，多环境多版本叠加下，几百个镜像的存储费用不可忽视
- **安全面**：镜像越大，包含的软件包越多，CVE 漏洞面越宽。distroless 镜像扫描出的漏洞数量通常是 ubuntu base 的 1/10

一个真实的对比：某 Go 服务未优化镜像 1.2 GB，优化后 18 MB，在 EKS 弹性扩容场景下，Pod 就绪时间从 45 秒降至 8 秒。

---

## 二、多阶段构建

多阶段构建是镜像优化最核心的手段，核心思路是：构建环境和运行环境分离，只把最终产物复制到运行镜像。

### Go 服务完整示例

```dockerfile
# syntax=docker/dockerfile:1

# ── Stage 1: 构建 ──────────────────────────────────────────────
FROM golang:1.23-alpine AS builder

WORKDIR /build

# 先复制依赖文件，利用 layer 缓存（代码改变时不重新下载依赖）
COPY go.mod go.sum ./
RUN go mod download

# 再复制源码
COPY . .

# 静态编译，不依赖 libc
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build \
    -ldflags="-s -w -extldflags '-static'" \
    -trimpath \
    -o /app/server \
    ./cmd/server

# ── Stage 2: 运行 ──────────────────────────────────────────────
FROM gcr.io/distroless/static-debian12:nonroot

# 从构建阶段复制二进制
COPY --from=builder /app/server /server

# 如果需要时区数据
COPY --from=builder /usr/share/zoneinfo /usr/share/zoneinfo

EXPOSE 8080

ENTRYPOINT ["/server"]
```

最终镜像大小：约 15–20 MB（视业务代码量），而 `golang:1.23` 基础镜像本身就有 800 MB+。

### Python 服务示例

```dockerfile
# syntax=docker/dockerfile:1

# ── Stage 1: 安装依赖 ──────────────────────────────────────────
FROM python:3.12-slim AS dependencies

WORKDIR /app

# 只复制依赖声明文件
COPY requirements.txt ./

RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── Stage 2: 运行 ──────────────────────────────────────────────
FROM python:3.12-slim AS runtime

WORKDIR /app

# 复制已安装的依赖
COPY --from=dependencies /install /usr/local

# 复制应用代码
COPY src/ ./src/

# 非 root 用户
RUN useradd -u 1001 -r appuser
USER appuser

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "src.main:app", "--host", "0.0.0.00", "--port", "8000"]
```

### Node.js 服务示例

```dockerfile
# syntax=docker/dockerfile:1

# ── Stage 1: 安装依赖 ──────────────────────────────────────────
FROM node:20-alpine AS deps

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --only=production

# ── Stage 2: 构建 ──────────────────────────────────────────────
FROM node:20-alpine AS builder

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

COPY . .
RUN npm run build

# ── Stage 3: 运行 ──────────────────────────────────────────────
FROM node:20-alpine AS runtime

WORKDIR /app

# 只复制生产依赖和构建产物
COPY --from=deps /app/node_modules ./node_modules
COPY --from=builder /app/dist ./dist
COPY --from=builder /app/package.json ./

RUN addgroup -g 1001 appgroup && adduser -u 1001 -G appgroup -s /bin/sh -D appuser
USER appuser

EXPOSE 3000
CMD ["node", "dist/index.js"]
```

---

## 三、基础镜像选型

| 镜像 | 典型大小 | Shell | 包管理器 | 适用场景 |
|------|---------|-------|---------|---------|
| `ubuntu:24.04` | ~80 MB | ✅ bash | ✅ apt | 调试/开发 |
| `debian:12-slim` | ~75 MB | ✅ sh | ✅ apt | 生产，需 apt 安装运行时依赖 |
| `alpine:3.20` | ~8 MB | ✅ sh | ✅ apk | 生产，轻量，musl libc（注意兼容性） |
| `gcr.io/distroless/static` | ~2 MB | ❌ | ❌ | 静态编译二进制（Go） |
| `gcr.io/distroless/base` | ~20 MB | ❌ | ❌ | 需要 glibc 的动态链接程序 |
| `gcr.io/distroless/python3` | ~50 MB | ❌ | ❌ | Python 应用 |
| `scratch` | 0 MB | ❌ | ❌ | 纯静态二进制，极限瘦身 |

**实际选型建议**：
- Go 静态编译 → `distroless/static:nonroot`，安全性最好
- Python/Node → `slim` 变体 + 非 root 用户
- 需要调试时 → 单独维护一个 debug 镜像，生产不用

Alpine 的 musl libc 与 glibc 存在微小差异，某些 C 扩展（如部分 Python 包）在 Alpine 上会编译失败或行为异常，踩坑后谨慎使用。

---

## 四、Layer 缓存优化

Docker 从上到下执行 Dockerfile，某一层变化后，后续所有层都会重新构建。原则：**变化频率低的指令放前面**。

### 错误示范

```dockerfile
# 每次代码改动都会导致 npm install 重新执行
COPY . .
RUN npm install
```

### 正确做法

```dockerfile
# 先复制 package.json（不常变）→ install → 再复制源码（频繁变）
COPY package.json package-lock.json ./
RUN npm ci
COPY src/ ./src/
```

### 依赖文件先于代码的原则

各语言对应规则：

```dockerfile
# Go
COPY go.mod go.sum ./
RUN go mod download
COPY . .

# Python
COPY requirements.txt ./
RUN pip install -r requirements.txt
COPY . .

# Java (Maven)
COPY pom.xml ./
RUN mvn dependency:go-offline
COPY src/ ./src/
```

---

## 五、.dockerignore 规范

`.dockerignore` 决定哪些文件不会被发送到 Docker build context，影响构建速度和镜像内容。

```dockerignore
# 版本控制
.git
.gitignore

# 依赖目录（通过容器内安装，不从宿主机复制）
node_modules
vendor
__pycache__
*.pyc
*.pyo
.venv
venv

# 构建产物
dist
build
target
*.o
*.a

# 测试文件
**/*_test.go
**/*.test.js
coverage/
.pytest_cache

# 文档
docs
*.md
README*

# 本地配置
.env
.env.local
*.local

# IDE 文件
.idea
.vscode
*.swp

# CI 配置（不需要进镜像）
.github
.gitlab-ci.yml
Jenkinsfile

# Docker 自身文件
Dockerfile*
docker-compose*.yml
```

一个没有 `.dockerignore` 的 Node 项目，`node_modules` 可能有几百 MB，全部发送给 daemon 会让构建上下文膨胀，即使最终镜像不包含这些文件。

---

## 六、减小镜像大小的其他技巧

### 合并 RUN 指令

```dockerfile
# 错误：每个 RUN 产生一个 layer，缓存无法清理
RUN apt-get update
RUN apt-get install -y curl wget
RUN rm -rf /var/lib/apt/lists/*

# 正确：合并为一条，确保缓存清理在同一层
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      curl \
      wget \
      ca-certificates && \
    rm -rf /var/lib/apt/lists/*
```

### `--no-install-recommends`

apt 默认会安装推荐包，加上这个参数只安装必需依赖：

```dockerfile
RUN apt-get install -y --no-install-recommends nginx
```

### pip 无缓存安装

```dockerfile
RUN pip install --no-cache-dir -r requirements.txt
```

### 清理构建工具

```dockerfile
RUN apk add --no-cache --virtual .build-deps \
      gcc musl-dev python3-dev && \
    pip install --no-cache-dir -r requirements.txt && \
    apk del .build-deps
```

---

## 七、漏洞扫描：Trivy

镜像构建完成后，用 Trivy 扫描 CVE：

```bash
# 安装 trivy
brew install aquasecurity/trivy/trivy  # macOS
# 或直接拉 docker 镜像
docker run --rm aquasec/trivy image my-app:latest

# 扫描本地镜像，只显示 HIGH 和 CRITICAL
trivy image --severity HIGH,CRITICAL my-app:latest

# 扫描并输出 JSON，供 CI 解析
trivy image --format json --output trivy-report.json my-app:latest

# 在 CI 中设置失败阈值
trivy image --exit-code 1 --severity CRITICAL my-app:latest
```

在 GitHub Actions 中集成：

```yaml
- name: 漏洞扫描
  uses: aquasecurity/trivy-action@master
  with:
    image-ref: ${{ env.IMAGE_URI }}
    format: sarif
    output: trivy-results.sarif
    severity: HIGH,CRITICAL
    exit-code: 1

- name: 上传扫描结果到 GitHub Security
  uses: github/codeql-action/upload-sarif@v3
  with:
    sarif_file: trivy-results.sarif
```

---

## 八、构建缓存加速

### BuildKit cache mount（最有效）

BuildKit 的 `--mount=type=cache` 允许在构建间持久化缓存目录，不会写入镜像层：

```dockerfile
# syntax=docker/dockerfile:1

# Go 模块缓存
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    go build -o /app/server ./cmd/server

# pip 缓存
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

# npm 缓存
RUN --mount=type=cache,target=/root/.npm \
    npm ci
```

启用 BuildKit：

```bash
DOCKER_BUILDKIT=1 docker build .
# 或
docker buildx build .
```

### GitHub Actions 缓存

```yaml
- name: 设置 Docker Buildx
  uses: docker/setup-buildx-action@v3

- name: 构建并推送
  uses: docker/build-push-action@v5
  with:
    context: .
    push: true
    tags: ${{ env.IMAGE_URI }}
    cache-from: type=gha
    cache-to: type=gha,mode=max
```

`type=gha` 使用 GitHub Actions Cache，`mode=max` 缓存所有中间层，首次构建后后续构建速度提升明显。

---

## 九、实测对比

以一个实际 Go 微服务为例：

| 优化阶段 | 镜像大小 | 构建时间（有缓存）| 漏洞数（HIGH+） |
|---------|---------|----------------|--------------|
| 原始（golang:1.21 + 应用代码）| 1.24 GB | 3m 20s | 47 |
| 多阶段构建（alpine runtime）| 38 MB | 1m 05s | 12 |
| 多阶段构建（distroless/static）| 18 MB | 58s | 0 |
| distroless + BuildKit cache | 18 MB | 12s | 0 |

关键结论：
- 多阶段构建是必做项，大小从 GB 级降到 MB 级
- distroless 相比 alpine 大小差不多，但漏洞清零，安全优先选 distroless
- BuildKit cache mount 对 CI 环境价值最大，有依赖变化时也只需重新安装变化部分

---

## 十、完整的生产级 Dockerfile 检查清单

- [ ] 使用多阶段构建，运行镜像不包含编译工具
- [ ] 选择最小化基础镜像（distroless / slim / alpine）
- [ ] 依赖文件先于源码 COPY，充分利用缓存
- [ ] `.dockerignore` 排除不必要文件
- [ ] RUN 指令合并，清理包管理器缓存
- [ ] 以非 root 用户运行（`USER nonroot` 或自建用户）
- [ ] 设置 HEALTHCHECK
- [ ] 使用 BuildKit cache mount 加速依赖安装
- [ ] CI 中集成 Trivy 漏洞扫描，CRITICAL 级别阻断构建
- [ ] 镜像 tag 包含 commit SHA，可追溯
