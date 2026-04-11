---
title: "Docker 最佳实践：从 Dockerfile 到生产部署"
date: 2026-04-05T10:00:00+08:00
draft: false
tags: ["Docker", "容器化", "DevOps", "运维"]
categories: ["Docker"]
description: "从多阶段构建、缓存策略到信号处理，总结生产环境中真实踩过的坑"
summary: "多阶段构建、.dockerignore 遗漏、非 root 运行、构建缓存优化，以及 entrypoint/cmd 信号处理这些在生产中实际踩过的问题，用具体的 Dockerfile 示例逐一拆解。"
toc: true
math: false
diagram: false
keywords: ["Docker", "Dockerfile", "多阶段构建", "容器安全", "tini", "健康检查"]
params:
  reading_time: true
---

写 Dockerfile 谁都会，但写一个「生产可用」的 Dockerfile 需要踩很多坑。这篇文章不是 Docker 入门教程，而是整理了我在实际运维中遇到的问题和解决方案，从镜像体积优化到信号处理，覆盖从构建到运行的完整链路。

## 多阶段构建：真正减小镜像体积

多阶段构建最大的价值不是「写法优雅」，而是把编译环境和运行环境彻底隔离，避免把构建工具链打包进最终镜像。

### Go 服务示例

Go 的静态编译天然适合多阶段构建，最终镜像可以小到只有几 MB：

```dockerfile
# syntax=docker/dockerfile:1

# ---- 构建阶段 ----
FROM golang:1.22-alpine AS builder

WORKDIR /app

# 先复制依赖文件，利用层缓存
COPY go.mod go.sum ./
RUN go mod download

# 再复制源码
COPY . .

# CGO_ENABLED=0 确保静态链接，GOOS=linux 交叉编译
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
    go build -ldflags="-w -s" -o /app/server ./cmd/server

# ---- 运行阶段 ----
FROM gcr.io/distroless/static-debian12

WORKDIR /app

# 从构建阶段只复制二进制
COPY --from=builder /app/server .

# 非 root 用户（distroless 内置 nonroot uid=65532）
USER nonroot:nonroot

EXPOSE 8080

ENTRYPOINT ["/app/server"]
```

这里用了 `distroless/static`，没有 shell，没有包管理器，攻击面极小。镜像大小通常在 10-20 MB 范围，而用 `golang:1.22` 全量镜像则会到 1 GB 以上。

`-ldflags="-w -s"` 去掉调试符号和符号表，二进制文件大小能再减 30% 左右。

### Python 服务示例

Python 没有静态编译，但多阶段构建仍然有价值——把 pip 安装的缓存和临时文件留在构建层：

```dockerfile
# syntax=docker/dockerfile:1

# ---- 依赖安装阶段 ----
FROM python:3.12-slim AS deps

WORKDIR /install

# 只复制依赖声明
COPY requirements.txt .

# --no-cache-dir 避免 pip 缓存写入镜像层
# --prefix 安装到独立目录，方便后续复制
RUN pip install --no-cache-dir --prefix=/install/packages -r requirements.txt

# ---- 运行阶段 ----
FROM python:3.12-slim

WORKDIR /app

# 创建非 root 用户
RUN groupadd -r appuser && useradd -r -g appuser appuser

# 从构建阶段复制已安装的依赖
COPY --from=deps /install/packages /usr/local

# 复制应用代码
COPY --chown=appuser:appuser . .

USER appuser

# 用 tini 作为 init 进程（下文详细说）
ENTRYPOINT ["tini", "--"]
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

如果用 `uv` 管理依赖，构建速度会快很多，但要确保 `uv.lock` 文件提交到 git，否则每次构建可能拉到不同版本，缓存也会频繁失效。

## .dockerignore 常见遗漏

`.dockerignore` 写得不好，build context 会把大量无用文件发送给 Docker daemon，拖慢构建速度，更严重的是可能把本地配置、密钥文件打包进镜像。

我见过最典型的遗漏：

```gitignore
# 这些很多人会忘记加

# Python 项目
__pycache__/
*.pyc
*.pyo
.pytest_cache/
.mypy_cache/
.venv/
venv/
dist/
*.egg-info/
.coverage
htmlcov/

# Go 项目
vendor/        # 如果用 go mod，vendor 目录不需要打包
*.test
*.out

# 通用
.git/          # 最容易被忘记！整个 git 历史都会进 context
.env           # 本地环境变量文件
.env.local
*.env.*
.DS_Store

# IDE
.idea/
.vscode/
*.swp

# 测试和文档
tests/
docs/
*.md           # 视情况，README 通常不需要
Makefile

# CI/CD
.github/
.gitlab-ci.yml
Jenkinsfile
```

`.git/` 被遗漏的后果尤其严重。一个有几年历史的项目，`.git` 目录可能有几百 MB，全部被发送到 daemon 只为了构建一个几十 MB 的镜像。

## 非 root 用户运行

默认情况下 Docker 容器以 root 运行，这在容器逃逸场景下会放大风险。改为非 root 是低成本高收益的安全加固。

```dockerfile
# 方式一：创建专用用户
RUN groupadd --gid 1001 appgroup && \
    useradd --uid 1001 --gid appgroup --no-create-home appuser

# 方式二：直接用数字 UID（适合 distroless 等没有 useradd 的镜像）
USER 1001:1001
```

切换非 root 后需要注意几个地方：

**文件权限**：应用写入的目录（日志、临时文件、上传）需要提前设置好权限：

```dockerfile
RUN mkdir -p /app/logs /app/tmp && \
    chown -R appuser:appgroup /app/logs /app/tmp

USER appuser
```

**端口绑定**：非 root 用户无法绑定 1024 以下端口。应用应该监听高位端口（如 8080），由 K8s Service 或 Load Balancer 处理端口映射，不需要在容器内绑定 80/443。

**挂载卷**：如果用 hostPath 或 PVC 挂载，挂载路径的宿主机目录权限需要和容器内 UID 对齐，否则会出现 permission denied。这个问题在 K8s 中用 `securityContext.fsGroup` 解决：

```yaml
securityContext:
  runAsUser: 1001
  runAsGroup: 1001
  fsGroup: 1001
```

## 构建缓存优化策略

Docker 层缓存的核心规则：**变化越频繁的指令放越靠后**。

典型的错误写法：

```dockerfile
# 错误：源码一变动，后面的 pip install 都要重跑
COPY . .
RUN pip install -r requirements.txt
```

正确写法：

```dockerfile
# 正确：依赖文件不变则 pip install 命中缓存
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
```

对于 Go 项目，`go mod download` 和 `go build` 分开：

```dockerfile
COPY go.mod go.sum ./
RUN go mod download  # 只要 go.mod/go.sum 没变，这层就命中缓存

COPY . .
RUN go build ...
```

**BuildKit 缓存挂载**是更进一步的优化，适合 CI 环境：

```dockerfile
# syntax=docker/dockerfile:1

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

RUN --mount=type=cache,target=/root/.cache/go/pkg/mod \
    go mod download
```

这种方式把包管理器的缓存持久化在 BuildKit 缓存中，即使镜像层不命中，包也不需要重新从网络拉取。在 CI 上需要配置 cache 持久化，GitHub Actions 用 `cache-from`/`cache-to`，自建 CI 用 registry cache：

```bash
docker buildx build \
  --cache-from type=registry,ref=your-registry/app:cache \
  --cache-to type=registry,ref=your-registry/app:cache,mode=max \
  -t your-registry/app:latest .
```

## 生产环境踩过的坑

### ENTRYPOINT vs CMD 的语义差异

这两个指令的区别经常搞混：

- `ENTRYPOINT`：容器的主进程，`docker run` 后面追加的参数会作为参数传给它
- `CMD`：ENTRYPOINT 的默认参数，可以被 `docker run` 覆盖

生产中最常见的错误是用 shell 形式：

```dockerfile
# Shell 形式（错误）：实际上是 /bin/sh -c "python app.py"
# PID 1 是 shell，不是 python
ENTRYPOINT python app.py

# Exec 形式（正确）：python 直接作为 PID 1
ENTRYPOINT ["python", "app.py"]
```

用 shell 形式时，信号（SIGTERM、SIGINT）发给 shell 进程，shell 不会默认转发给子进程，导致容器关闭时应用无法优雅退出，K8s 会等待 `terminationGracePeriodSeconds` 超时后强制 kill。

### 信号处理与 tini

即使用了 exec 形式，如果应用没有正确处理 SIGTERM，或者产生了僵尸进程（父进程退出但子进程未被回收），都会有问题。

`tini` 是一个极小的 init 进程，专门解决这两个问题：

```dockerfile
# Alpine
RUN apk add --no-cache tini

# Debian/Ubuntu
RUN apt-get install -y tini

ENTRYPOINT ["/sbin/tini", "--"]
CMD ["python", "app.py"]
```

tini 会：
1. 作为 PID 1 正确转发信号给子进程
2. 回收僵尸进程（zombie reaping）

如果用 distroless 镜像没法安装 tini，可以从其他镜像复制：

```dockerfile
COPY --from=krallin/ubuntu-tini /usr/bin/tini /tini
ENTRYPOINT ["/tini", "--"]
```

K8s 1.20+ 也可以在 Pod spec 里开启 `shareProcessNamespace: true` 配合 pause 容器来处理，但直接在镜像里加 tini 更简单可控。

### 另一个常见坑：环境变量泄漏

`ARG` 和 `ENV` 的区别：

```dockerfile
# ARG 只在构建期有效，不会出现在最终镜像的环境变量里
ARG BUILD_VERSION

# ENV 会持久化到镜像，docker inspect 可以看到
ENV APP_VERSION=${BUILD_VERSION}

# 危险！密钥不要用 ENV 传入
# ENV DB_PASSWORD=secret  # 这会永久存在镜像层里
```

密钥应该通过运行时注入（K8s Secret、环境变量挂载），绝对不能烘焙进镜像。

## 健康检查配置

Dockerfile 里的 `HEALTHCHECK` 和 K8s 的 `livenessProbe`/`readinessProbe` 是两个层面的健康检查，各有用途。

Dockerfile HEALTHCHECK 主要用于 `docker run` 裸跑场景：

```dockerfile
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget --no-verbose --tries=1 --spider http://localhost:8080/health || exit 1
```

用 `wget` 而不是 `curl` 是因为有些精简镜像（alpine）默认有 wget 没有 curl。也可以用应用自带的健康检查命令：

```dockerfile
HEALTHCHECK --interval=10s --timeout=3s \
    CMD ["/app/server", "--health-check"] || exit 1
```

K8s 中更推荐在 Deployment 里配置 probe，而不是依赖镜像内的 HEALTHCHECK，因为 K8s 的 probe 更灵活，支持 httpGet、tcpSocket、exec 三种方式，还有 `startupProbe` 专门处理慢启动场景：

```yaml
livenessProbe:
  httpGet:
    path: /health/live
    port: 8080
  initialDelaySeconds: 10
  periodSeconds: 10
  failureThreshold: 3

readinessProbe:
  httpGet:
    path: /health/ready
    port: 8080
  initialDelaySeconds: 5
  periodSeconds: 5

startupProbe:
  httpGet:
    path: /health/live
    port: 8080
  failureThreshold: 30   # 最多等 30*10=300 秒
  periodSeconds: 10
```

`livenessProbe` 失败会重启容器，`readinessProbe` 失败只是把 Pod 从 Service endpoints 摘掉，不重启。这个区别非常重要——不要把依赖检查（DB 连接、下游服务）放进 liveness，否则下游抖动会导致自己被重启，形成雪崩。

---

这些实践大部分都是被坑过之后总结出来的，单独看每一条可能觉得是小细节，但在生产环境高频变更、多团队协作的背景下，每一个细节都可能是事故的根因。
