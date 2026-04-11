---
title: "Dockerfile 编写最佳实践"
date: 2025-12-09T17:00:00+08:00
draft: false
tags: ["Docker", "Dockerfile", "最佳实践"]
categories: ["CI/CD"]
description: "从基础指令用法到信号处理、非 root 运行、HEALTHCHECK，覆盖生产级 Dockerfile 的全部关键点"
summary: "系统讲解 Dockerfile 每条指令的最佳用法、ENTRYPOINT vs CMD 的组合方式、PID 1 信号处理问题，附 Go 服务和 Python 服务完整生产级示例。"
toc: true
math: false
diagram: false
keywords: ["dockerfile", "docker最佳实践", "多阶段构建", "ENTRYPOINT", "dumb-init", "HEALTHCHECK"]
params:
  reading_time: true
---

## 一、基础原则

在写 Dockerfile 之前，确立几条核心原则，后续所有细节都是围绕这些原则展开：

1. **每条指令一个职责**：不要把不相关的操作塞进同一个 `RUN`，除非是为了合并 layer 避免缓存污染
2. **最小权限**：运行容器的进程不应该是 root，非必要不暴露端口，非必要不挂 volume
3. **可重现构建**：相同的源码和 Dockerfile 应该产出相同的镜像，避免依赖网络上的 `latest` tag 或浮动版本
4. **显式优于隐式**：版本号要 pin 住，基础镜像要指定 digest 或精确 tag

---

## 二、指令详解与最佳用法

### FROM

```dockerfile
# 差：latest 不稳定，每次构建可能拿到不同的基础镜像
FROM ubuntu:latest

# 好：pin 到精确版本
FROM ubuntu:24.04

# 更好：用 digest 确保内容不变（适合安全要求极高的场景）
FROM ubuntu:24.04@sha256:723ad8033f109978f8c7e6421ee684efb624eb5b9251b70c6788fdb2405d050b
```

多阶段构建时，给每个 stage 命名：

```dockerfile
FROM golang:1.23-alpine AS builder
FROM gcr.io/distroless/static-debian12 AS runtime
```

### RUN

合并相关命令，尤其是 `apt-get update` 和 `apt-get install` 必须在同一条 `RUN`，否则 layer 缓存会导致使用过期的 apt 索引：

```dockerfile
# 错误：update 和 install 分开会有缓存问题
RUN apt-get update
RUN apt-get install -y curl

# 正确：合并 + 清理缓存
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      curl \
      ca-certificates && \
    rm -rf /var/lib/apt/lists/*
```

利用 BuildKit 的 cache mount（不写入镜像层）：

```dockerfile
# syntax=docker/dockerfile:1
RUN --mount=type=cache,target=/var/cache/apt \
    apt-get update && \
    apt-get install -y --no-install-recommends curl
```

### COPY vs ADD

**永远优先用 COPY，只在必要时用 ADD**：

```dockerfile
# COPY: 明确，只从本地复制文件/目录
COPY src/ /app/src/
COPY config.yaml /app/

# ADD 的额外功能（一般不需要）：
# 1. 自动解压 tar 包
ADD archive.tar.gz /app/   # 会自动解压
# 2. 从 URL 下载（不推荐，应该在 RUN 里用 curl 并做 checksum 校验）
ADD https://example.com/file /tmp/
```

`ADD` 的行为对阅读者不够透明，且 URL 方式没有 checksum 校验，安全性差。

### CMD 与 ENTRYPOINT

这两条是最容易混淆的指令，下面的对比表说明一切：

| | ENTRYPOINT | CMD |
|--|-----------|-----|
| 作用 | 容器的主命令（固定）| 主命令的参数（可覆盖）|
| 覆盖方式 | `docker run --entrypoint` | `docker run ... [CMD]` |
| 推荐格式 | exec 格式（JSON 数组）| exec 格式（JSON 数组）|

**常见组合方式**：

```dockerfile
# 方式 1: 只用 CMD（完全可覆盖）
CMD ["python", "app.py"]
# docker run myimage              → python app.py
# docker run myimage bash         → bash（替换整个命令）

# 方式 2: 只用 ENTRYPOINT（命令固定，参数拼接）
ENTRYPOINT ["nginx"]
# docker run myimage              → nginx
# docker run myimage -g "daemon off;"   → nginx -g "daemon off;"

# 方式 3: ENTRYPOINT + CMD 组合（推荐用于服务）
ENTRYPOINT ["/server"]
CMD ["--port=8080", "--log-level=info"]
# docker run myimage              → /server --port=8080 --log-level=info
# docker run myimage --port=9090  → /server --port=9090（覆盖 CMD 部分）
```

**不要用 shell 格式**（会导致 PID 1 问题，下面详细说）：

```dockerfile
# 差：shell 格式，进程是 /bin/sh -c 的子进程
ENTRYPOINT python app.py

# 好：exec 格式，进程直接是 PID 1
ENTRYPOINT ["python", "app.py"]
```

### ENV 与 ARG

```dockerfile
# ARG: 仅在构建时有效，不写入最终镜像
ARG BUILD_VERSION=dev
ARG TARGETARCH

# ENV: 写入镜像，容器运行时可见
ENV APP_ENV=production
ENV LOG_LEVEL=info

# 两者结合：构建时传参，运行时可见
ARG APP_VERSION
ENV APP_VERSION=${APP_VERSION:-unknown}
```

构建时传入 ARG：

```bash
docker build --build-arg APP_VERSION=1.2.0 -t myapp:1.2.0 .
```

**注意**：不要通过 ARG 传递 secret，构建历史中可见。应使用 `--mount=type=secret`：

```dockerfile
# syntax=docker/dockerfile:1
RUN --mount=type=secret,id=github_token \
    GITHUB_TOKEN=$(cat /run/secrets/github_token) \
    git clone https://oauth2:${GITHUB_TOKEN}@github.com/private/repo.git
```

### EXPOSE

```dockerfile
# EXPOSE 只是文档声明，不实际开放端口
# 实际映射需要 docker run -p 8080:8080
EXPOSE 8080
EXPOSE 9090  # metrics
```

即使不写 EXPOSE，容器内的进程监听端口照样可以被访问（只要端口映射正确）。EXPOSE 的价值在于文档化和 `docker run -P` 随机映射时使用。

### VOLUME

```dockerfile
# 声明匿名 volume，容器删除后数据丢失（除非显式挂载）
VOLUME ["/data", "/logs"]
```

生产环境中，建议在 Kubernetes 的 manifest 中显式声明 PVC，不依赖 Dockerfile 的 VOLUME 指令。

### WORKDIR

```dockerfile
# 用绝对路径，不要用 cd
WORKDIR /app

# 可以多次使用，路径会叠加
WORKDIR /app/src   # 等于 cd /app && mkdir src && cd src
```

### HEALTHCHECK

```dockerfile
# 基础 HTTP 健康检查
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

# 使用 wget（alpine 通常有 wget 但没有 curl）
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD wget -qO- http://localhost:8080/health || exit 1

# 对于没有 shell 的 distroless 镜像，需要内置健康检查二进制
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD ["/healthcheck"]
```

参数说明：
- `--interval`：检查间隔（默认 30s）
- `--timeout`：单次检查超时（默认 30s）
- `--start-period`：容器启动后的等待时间，期间失败不计入 retries（默认 0s，启动慢的服务要调高）
- `--retries`：连续失败多少次后标记为 unhealthy（默认 3）

### USER

```dockerfile
# 创建非 root 用户
RUN groupadd -g 1001 appgroup && \
    useradd -u 1001 -g appgroup -s /bin/false -r appuser

# 切换到非 root 用户（之后的所有指令都以此用户执行）
USER appuser

# distroless 镜像内置了 nonroot 用户
FROM gcr.io/distroless/static-debian12:nonroot
# 已经是 nonroot 用户，无需额外 USER 指令
```

---

## 三、PID 1 问题与信号处理

容器内的第一个进程（PID 1）有特殊职责：它负责接收和转发信号，回收僵尸进程。

**问题**：普通应用程序（如 Python/Node.js 进程）通常不处理这些职责，导致：
- `docker stop` 发送 SIGTERM 后，应用不响应，等 10 秒后被 SIGKILL 强杀
- 子进程变成僵尸进程无法回收

**解决方案 1：使用 tini**

```dockerfile
# 安装 tini（轻量级 init）
RUN apt-get install -y tini

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "app.py"]
```

或者使用 Docker 内置的 `--init` 标志（不修改 Dockerfile）：

```bash
docker run --init my-app
```

**解决方案 2：使用 dumb-init**

```dockerfile
RUN apt-get install -y dumb-init

ENTRYPOINT ["/usr/bin/dumb-init", "--"]
CMD ["/server"]
```

**解决方案 3：应用层面优雅退出（Go 示例）**

```go
func main() {
    ctx, stop := signal.NotifyContext(context.Background(),
        syscall.SIGTERM, syscall.SIGINT)
    defer stop()

    server := &http.Server{Addr: ":8080"}

    go func() {
        <-ctx.Done()
        shutdownCtx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
        defer cancel()
        server.Shutdown(shutdownCtx)
    }()

    server.ListenAndServe()
}
```

---

## 四、完整生产级示例

### Go 服务

```dockerfile
# syntax=docker/dockerfile:1

# ── 构建阶段 ──────────────────────────────────────────────────
FROM golang:1.23-alpine AS builder

# 安装构建依赖
RUN apk add --no-cache git ca-certificates tzdata

WORKDIR /build

# 先复制依赖文件，利用缓存
COPY go.mod go.sum ./
RUN --mount=type=cache,target=/go/pkg/mod \
    go mod download

# 复制源码
COPY . .

# 静态编译
ARG APP_VERSION=dev
ARG COMMIT_SHA=unknown
RUN --mount=type=cache,target=/root/.cache/go-build \
    CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build \
    -ldflags="-s -w \
              -X main.Version=${APP_VERSION} \
              -X main.CommitSHA=${COMMIT_SHA} \
              -extldflags '-static'" \
    -trimpath \
    -o /app/server \
    ./cmd/server

# 构建健康检查工具（如果需要在 distroless 中用）
RUN CGO_ENABLED=0 go build -o /app/healthcheck ./cmd/healthcheck

# ── 运行阶段 ──────────────────────────────────────────────────
FROM gcr.io/distroless/static-debian12:nonroot

# 时区数据
COPY --from=builder /usr/share/zoneinfo /usr/share/zoneinfo
# CA 证书（HTTPS 请求需要）
COPY --from=builder /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/

# 复制二进制
COPY --from=builder /app/server /server
COPY --from=builder /app/healthcheck /healthcheck

# 暴露端口（文档用途）
EXPOSE 8080 9090

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["/healthcheck"]

# distroless:nonroot 已经是非 root 用户（UID 65532）
ENTRYPOINT ["/server"]
```

### Python 服务

```dockerfile
# syntax=docker/dockerfile:1

# ── 依赖安装阶段 ──────────────────────────────────────────────
FROM python:3.12-slim AS dependencies

WORKDIR /install

# 安装编译依赖
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      gcc \
      libpq-dev && \
    rm -rf /var/lib/apt/lists/*

# 复制依赖声明
COPY requirements.txt ./

# 安装到独立目录，方便复制到运行镜像
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-cache-dir --prefix=/python-deps -r requirements.txt

# ── 运行阶段 ──────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# 安装运行时依赖（非编译时）
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      libpq5 \
      dumb-init \
      curl && \
    rm -rf /var/lib/apt/lists/*

# 创建非 root 用户
RUN groupadd -g 1001 appgroup && \
    useradd -u 1001 -g appgroup -s /bin/false -r -d /app appuser

WORKDIR /app

# 复制已安装的 Python 依赖
COPY --from=dependencies /python-deps /usr/local

# 复制应用代码
COPY --chown=appuser:appgroup src/ ./src/

# 切换用户
USER appuser

EXPOSE 8000

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# dumb-init 解决 PID 1 问题
ENTRYPOINT ["/usr/bin/dumb-init", "--"]
CMD ["python", "-m", "uvicorn", "src.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "4", \
     "--no-access-log"]
```

---

## 五、常见误区总结

| 误区 | 正确做法 |
|------|---------|
| 用 `root` 用户运行应用 | 创建专用用户，`USER appuser` |
| ENTRYPOINT 用 shell 格式 | 用 exec 格式（JSON 数组） |
| FROM 用 `latest` | Pin 到精确版本 |
| 构建和运行用同一镜像 | 多阶段构建 |
| COPY 顺序不优化 | 先复制依赖文件，后复制源码 |
| 不写 `.dockerignore` | 维护完善的 `.dockerignore` |
| 不处理 SIGTERM | 使用 dumb-init/tini，或应用层优雅退出 |
| 不设置 HEALTHCHECK | 配置合理的健康检查（含 start-period）|
| ARG 传 secret | 用 `--mount=type=secret` |
| ADD 替代 COPY | 优先 COPY，ADD 只用于解压 tar |
