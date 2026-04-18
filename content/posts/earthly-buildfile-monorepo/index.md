---
title: "Earthly 在 Monorepo 的构建统一：Earthfile + Satellites 实战"
date: 2026-02-03T10:00:00+08:00
draft: false
tags: ["Earthly", "Monorepo", "BuildKit", "构建系统"]
categories: ["CI/CD"]
description: "Earthly 用一个类 Dockerfile 的 Earthfile 语法统一 Monorepo 里 Go/Python/Node/Rust 的构建流程，底层还是 BuildKit 但多了 target、import、arg 等 Make 风格抽象。本文讲怎么用 Earthfile 组织大型 Monorepo、Satellites 做远端缓存的取舍、以及什么场景不适合用 Earthly。"
summary: "Bazel 复杂度太高，Makefile 表达力不够，Dockerfile 只能构建一个镜像——Earthly 填的就是这个缝：像 Dockerfile 一样熟悉，像 Makefile 一样组合，像 Bazel 一样可并发、可缓存、可复用。本文讲清楚它在 Monorepo 里的真实位置。"
toc: true
math: false
diagram: true
keywords: ["Earthly", "Earthfile", "Monorepo", "Satellite", "remote cache"]
params:
  reading_time: true
---

## Earthly 填的是哪个坑

Monorepo 构建工具的光谱从"简单"到"复杂"大概是这样：

```mermaid
flowchart LR
    A[Makefile<br/>表达力低] --> B[Dockerfile<br/>只构建镜像]
    B --> C[Earthly<br/>类Dockerfile+target]
    C --> D[Dagger<br/>代码写 pipeline]
    D --> E[Bazel<br/>完全声明式]
    E --> F[Nix<br/>更极端的声明式]
```

- 左边 Makefile：门槛最低，但表达力弱，target 之间依赖靠人脑记
- Dockerfile：能构建镜像，但"构建非镜像产物"（比如跑测试、出 coverage 报告）很笨拙
- Bazel：表达力和性能顶级，但学习曲线极陡峭，全公司上 Bazel 是一年起步的项目
- Nix：更严谨，但比 Bazel 还陡

Earthly 的定位明确："**像 Dockerfile 一样容易上手，但提供 Makefile 风格的 target、import、arg，每个 target 都有缓存和并发**"。

一个最简单的 `Earthfile`：

```earthfile
VERSION 0.8

FROM golang:1.23-bookworm
WORKDIR /src

deps:
    COPY go.mod go.sum .
    RUN go mod download

build:
    FROM +deps
    COPY . .
    RUN go build -o /out/app ./cmd/server
    SAVE ARTIFACT /out/app AS LOCAL ./bin/app

test:
    FROM +deps
    COPY . .
    RUN go test ./...

image:
    FROM gcr.io/distroless/static-debian12:nonroot
    COPY +build/app /app
    ENTRYPOINT ["/app"]
    SAVE IMAGE --push registry.example.com/app:latest
```

用起来：

```bash
earthly +test          # 跑测试
earthly +build         # 构建二进制
earthly +image         # 构建并推镜像
earthly --push +image  # 构建并 push（默认只 build，不 push）
```

你可以把 Earthfile 想成 **Dockerfile + Makefile 的并集**：

- **`FROM` / `COPY` / `RUN`**：和 Dockerfile 一样
- **`target:`**：像 Makefile 的 target，可以被其它 target 引用
- **`+target/artifact`**：跨 target 引用产物，类似 `COPY --from`
- **`SAVE ARTIFACT`**：把文件存到 earthly cache 或导出本地
- **`SAVE IMAGE`**：把结果保存为 OCI 镜像
- **`FROM +other-target`**：继承另一个 target 的状态（重要！是 Earthly 复用的核心机制）

## Earthfile 语法要点

### VERSION 与 feature flags

`VERSION 0.8` 是必需的。它控制 Earthly 的语法解析行为和默认 feature flags 集合。不写的话 Earthly 会报错提醒。

### target 继承：FROM +other-target

这是 Earthly 最重要的抽象。

```earthfile
base:
    FROM golang:1.23-bookworm
    WORKDIR /src
    ENV CGO_ENABLED=0

deps:
    FROM +base
    COPY go.mod go.sum .
    RUN go mod download

build:
    FROM +deps
    COPY . .
    RUN go build -o /out/app ./cmd/server
```

`build` 从 `+deps` 继承，`deps` 从 `+base` 继承。整个链路是一个 DAG，Earthly 会自动算出哪些 target 可以共享层、哪些需要重新执行。

**等价的 Dockerfile**：

```dockerfile
FROM golang:1.23-bookworm AS base
WORKDIR /src
ENV CGO_ENABLED=0

FROM base AS deps
COPY go.mod go.sum .
RUN go mod download

FROM deps AS build
COPY . .
RUN go build -o /out/app ./cmd/server
```

差别在哪？Earthfile 的 target 是**可独立调用**的：`earthly +deps` 会只跑到 `deps` 为止。Dockerfile 的 stage 只能作为构建镜像的中间状态，你不能说 "我只想产出 `deps` 的结果"。这个差别在 Monorepo 里很关键。

### ARG：参数化 target

```earthfile
build:
    ARG GO_VERSION=1.23
    ARG PKG=./cmd/server
    FROM golang:$GO_VERSION-bookworm
    WORKDIR /src
    COPY . .
    RUN go build -o /out/bin $PKG
    SAVE ARTIFACT /out/bin AS LOCAL bin/
```

调用：

```bash
earthly +build --GO_VERSION=1.22 --PKG=./cmd/worker
```

ARG 是构建时参数，不会进最终镜像。`--arg` 和 Docker 的 `--build-arg` 类似但语法更灵活。

### BUILD：显式并发

```earthfile
all:
    BUILD +build-go
    BUILD +build-node
    BUILD +test-go
    BUILD +test-node
```

`BUILD` 声明依赖但**不**继承文件系统。上面的 `all` target 会并发跑四个子 target。

注意 `FROM +x` 和 `BUILD +x` 的区别：

- `FROM +x`：继承 x 的 filesystem 状态，x 一定会先跑完
- `BUILD +x`：只是触发 x 跑，不继承任何东西

前者像 C 语言的 `include`，后者像 Makefile 的 dependency 声明。

### SAVE ARTIFACT 和 COPY 的跨 target 交互

```earthfile
build-binary:
    FROM +deps
    COPY . .
    RUN go build -o /out/app ./cmd/server
    SAVE ARTIFACT /out/app app

image:
    FROM gcr.io/distroless/static-debian12:nonroot
    COPY +build-binary/app /app
    ENTRYPOINT ["/app"]
    SAVE IMAGE --push registry.example.com/app:latest
```

`SAVE ARTIFACT /out/app app` 把容器里的 `/out/app` 存为 "本 target 的产物，名字叫 app"。

`COPY +build-binary/app /app` 在另一个 target 里拉这个产物。Earthly 知道：要跑 `image`，必须先跑 `build-binary`；且 `build-binary` 的结果可以缓存。

## Monorepo 的目录组织

真正的价值在 Monorepo。一个典型布局：

```
monorepo/
├── Earthfile                 # 根 Earthfile：定义全局 target
├── services/
│   ├── api/
│   │   ├── Earthfile         # api 服务的 Earthfile
│   │   ├── cmd/
│   │   └── internal/
│   ├── worker/
│   │   ├── Earthfile
│   │   └── ...
│   └── frontend/
│       ├── Earthfile         # Node 项目
│       └── ...
├── libs/
│   ├── common-go/
│   │   └── Earthfile         # 共享 Go lib 的 Earthfile
│   └── common-ts/
│       └── Earthfile
└── tools/
    └── Earthfile             # 构建工具集
```

根 Earthfile：

```earthfile
VERSION 0.8

# 全局入口
all:
    BUILD ./services/api+image
    BUILD ./services/worker+image
    BUILD ./services/frontend+image

# 只构建改动的服务（由 CI 传参）
changed:
    ARG --required SERVICES
    FOR svc IN $SERVICES
        BUILD ./services/$svc+image
    END

# 全量测试
test-all:
    BUILD ./services/api+test
    BUILD ./services/worker+test
    BUILD ./services/frontend+test
    BUILD ./libs/common-go+test
    BUILD ./libs/common-ts+test
```

子 Earthfile 引用上级：

```earthfile
# services/api/Earthfile
VERSION 0.8

FROM golang:1.23-bookworm
WORKDIR /src

deps:
    COPY ../../libs/common-go+src/* /src/libs/common-go/
    COPY go.mod go.sum .
    RUN go mod download

build:
    FROM +deps
    COPY . .
    RUN go build -o /out/api ./cmd/api
    SAVE ARTIFACT /out/api api

test:
    FROM +deps
    COPY . .
    RUN go test ./...

image:
    FROM gcr.io/distroless/static-debian12:nonroot
    COPY +build/api /api
    ENTRYPOINT ["/api"]
    SAVE IMAGE --push registry.example.com/api:latest
```

关键是 `COPY ../../libs/common-go+src/* /src/libs/common-go/`：跨目录引用另一个 Earthfile 的 target 产物。这个机制让 libs 和 services 解耦，libs 变更时只有依赖它的 services 重构建。

### 只构建变更服务

Monorepo 的核心诉求是 **增量构建**：一个 PR 只改了 `services/api/`，就不应该重构 `services/worker/` 和 `services/frontend/`。

Earthly 本身不做 git diff 分析，需要 CI 脚本计算：

```bash
#!/bin/bash
# scripts/changed-services.sh
BASE=${1:-origin/main}
CHANGED_FILES=$(git diff --name-only $BASE...HEAD)

CHANGED_SERVICES=()
for file in $CHANGED_FILES; do
    if [[ $file == services/* ]]; then
        svc=$(echo $file | cut -d/ -f2)
        CHANGED_SERVICES+=($svc)
    elif [[ $file == libs/common-go/* ]]; then
        # common-go 变了，所有 Go 服务都要重构
        CHANGED_SERVICES+=(api worker)
    elif [[ $file == libs/common-ts/* ]]; then
        CHANGED_SERVICES+=(frontend)
    fi
done

# 去重
echo "${CHANGED_SERVICES[@]}" | tr ' ' '\n' | sort -u | tr '\n' ' '
```

CI 调用：

```bash
SERVICES=$(./scripts/changed-services.sh)
if [ -n "$SERVICES" ]; then
    earthly --ci +changed --SERVICES="$SERVICES"
fi
```

这种手动计算有点麻烦，但换来的是精确控制。社区有一些 "Earthly + Nx" 或 "Earthly + Turborepo" 的尝试，把变更检测交给 Nx/Turbo 做，Earthly 只负责实际构建。

## Satellites：Earthly 的远端缓存方案

Monorepo 构建最大的敌人是**冷 cache**。本地 `earthly +build` 每次都是秒级（因为 BuildKit layer cache 命中），但 CI runner 是短生命周期的，每次开机缓存都是空的，回到全量构建。

Earthly 的官方解法是 **Satellites**：一个托管的远端 BuildKit worker + 持久 cache。你在 Earthly Cloud 里起一个 Satellite，CI 不再在本地跑构建，而是把 Earthfile "外包" 给 Satellite 执行，Satellite 持有长期 cache。

```bash
# 选择一个 Satellite
earthly sat select my-satellite

# 之后所有 earthly 命令都在 satellite 上执行
earthly +build
```

Satellites 的优势：

- **远端持久 cache**：跨 CI 运行、跨开发者共享
- **机器性能高**：Earthly 提供 4c/8c/16c 的 satellite，比 GHA free runner 强一截
- **网络就近**：拉 base image、push 镜像都在 Earthly 的骨干上，不受 CI runner 网络限制
- **无需管理**：不用自建 BuildKit 集群

缺点也很明显：

- **付费**：Earthly Cloud 按 satellite 小时数收费，小团队按月费大约 $100-500
- **数据出境**：你的源代码会上传到 Earthly 的 satellite 执行。对数据敏感的公司要评估合规
- **供应商锁定**：一旦依赖 Satellite 特性，迁出成本高

不想用 Earthly Cloud 也有替代方案：**自建 BuildKit worker pool + earthly remote runner**。

```bash
# 在 K8s 里起一个 BuildKit StatefulSet
kubectl apply -f buildkit-pool.yaml

# 在 CI 里让 earthly 连过去
earthly --buildkit-host tcp://buildkit.ci.svc:1234 +build
```

这套和 Satellites 功能相似，但需要你自己维护 BuildKit pool、cache volume、网络。大团队值得做，中小团队直接买 Satellites 更经济。

## Earthly vs 其它工具的对比

### vs Docker + Makefile

Makefile 的问题是**无缓存、无并发、无沙盒**。你写 `make test`，每次都跑全量测试；两个 make target 之间无隔离，一个 target 写的 `/tmp/cache` 影响另一个。

Earthly 继承了 BuildKit 的沙盒和缓存，每个 target 独立执行，文件系统完全隔离。

```makefile
# Makefile
test:
	go test ./...

build:
	go build -o bin/app ./cmd/server

image:
	docker build -t app .
```

```earthfile
# Earthfile
test:
    FROM +deps
    COPY . .
    RUN go test ./...

build:
    FROM +deps
    COPY . .
    RUN go build -o /out/app ./cmd/server
    SAVE ARTIFACT /out/app AS LOCAL bin/

image:
    FROM +base
    COPY +build/app /app
    SAVE IMAGE --push app:latest
```

行数差不多，但 Earthly 的三个 target 互不影响，都有缓存，都能并发。

### vs Bazel

Bazel 是另一个声明式构建系统，更严格：

| 维度 | Earthly | Bazel |
|------|---------|-------|
| 学习曲线 | 低（Dockerfile 用户 1 天上手）| 高（几周到几个月）|
| 生态成熟度 | 中 | 非常高（Google/Shopify/Stripe 生产级） |
| 增量构建精度 | target 级别 | 文件级别 |
| 远程执行 | Satellite / 自建 BuildKit | RBE / Buildbarn |
| 多语言支持 | 通过 Dockerfile 风格 | 每种语言都有 rules_X |
| 封装度 | 相对松（可以 `RUN` 任意命令）| 极严格（必须用 rules）|

Earthly 更适合"**从 Dockerfile/Makefile 过渡过来、想要更现代的构建抽象但不想上 Bazel**"的团队。

Bazel 更适合"**万人规模 Monorepo、愿意投入半年基础设施改造**"的团队。

大部分 50-500 人的公司，Earthly 的性价比明显高于 Bazel。

### vs Dagger

Dagger（我们另一篇讲过）用**代码**写 pipeline，Earthly 用**Earthfile** DSL。

| 维度 | Earthly | Dagger |
|------|---------|--------|
| 语法 | Earthfile（类 Dockerfile）| SDK 代码（Go/Python/TS）|
| 学习曲线 | 低 | 中 |
| 可测试性 | Earthfile 本身不能跑 go test | 代码可写单测 |
| IDE 支持 | 有 syntax highlight | 完整 IDE（编译期检查）|
| 适合场景 | 构建/测试/出镜像 | 构建 + 部署 + 任意管道 |
| 复用机制 | target + import | module + SDK |

Earthly 更轻、更快上手。Dagger 更灵活、更接近真正的"pipeline as code"。两者不是竞争关系，有些团队 Earthfile 做构建、Dagger 做部署编排。

## 落地实战：一个 20 服务 Monorepo 的迁移

我们公司有个大型 Monorepo：15 个 Go 微服务、3 个 Python 服务、2 个 Node 前端，加一堆 libs。迁移前用 Make + Dockerfile 组合，问题：

- 每个服务一个 Dockerfile，重复代码多（都是 `FROM golang → mod download → build → COPY 到 distroless`）
- 全量 CI 构建 28 分钟（因为没有跨 job cache）
- "只构建改动服务" 的脚本一堆 bash if/else，维护头痛

迁移到 Earthly 大约花了两周：

**第一周**：

1. 写根 `Earthfile` 定义全局 target
2. 写 `libs/common-go/Earthfile` 和 `libs/common-ts/Earthfile`
3. 迁移前 3 个 Go 服务的 Dockerfile 到 Earthfile

**第二周**：

1. 批量迁移剩余服务（大部分是 copy-paste 改名）
2. 写 CI 集成，用 Satellites
3. 用 `changed-services.sh` 做增量构建
4. 下线所有 Dockerfile

迁移后数据：

| 指标 | 迁移前 | 迁移后 |
|------|--------|--------|
| 全量 CI 构建时间 | 28 分钟 | 8 分钟（冷 cache）/ 90 秒（热 cache）|
| 增量 CI 构建时间（改一个服务）| 14 分钟 | 45 秒 |
| 重复代码行数 | ~1200 行 Dockerfile | ~400 行 Earthfile |
| "构建系统"相关故障/月 | 4-5 次 | ~0 |
| CI 月费 | $1200 | $700（GHA）+ $300（Earthly Satellite）= $1000 |

最大的收益是**心智模型统一**。以前每个服务一个 Dockerfile、一个 Makefile，新同事进来要学 3 种 "怎么构建这个服务" 的方式。现在全公司 `earthly +build` 一条命令，任何人看 Earthfile 都能看懂。

## 坑和限制

### 坑 1：跨 Earthfile 引用路径必须是相对的

```earthfile
# 这样可以
COPY ../../libs/common-go+src/* ./libs/common-go/

# 这样不行（绝对路径）
COPY /monorepo/libs/common-go+src/* ./libs/common-go/
```

所有路径必须相对 Earthfile 所在目录。用绝对路径会报错。

### 坑 2：SAVE ARTIFACT 的语法细节

```earthfile
# 把容器内的 /out/app 保存为当前 target 的产物 app
SAVE ARTIFACT /out/app app

# 把 /out/app 导出到本地（host）./bin/app
SAVE ARTIFACT /out/app AS LOCAL ./bin/app

# 同时做两件事
SAVE ARTIFACT /out/app app AS LOCAL ./bin/app
```

`AS LOCAL` 表示导出到 host filesystem。CI 里用 `AS LOCAL` 可能和 Satellite 冲突（Satellite 是远端的，LOCAL 是哪？），这时候 Earthly 会自动下载到 CI runner 的本地。但要注意数据量大时下载会拖慢。

### 坑 3：Earthfile 里用 git clone 私有 repo

Earthfile 的 `RUN` 执行在沙盒容器里，默认没有 git credential。要用私有 repo：

```earthfile
deps:
    ARG GIT_TOKEN
    RUN --secret GITHUB_TOKEN=$GIT_TOKEN \
        git config --global url."https://${GITHUB_TOKEN}@github.com/".insteadOf "https://github.com/" && \
        go mod download
```

调用：

```bash
earthly --secret GIT_TOKEN=$GITHUB_TOKEN +deps
```

`--secret` 的内容不会进 cache key、不会出现在日志里。

### 坑 4：Earthfile 调试不如 Dockerfile

Dockerfile 出错可以 `docker run -it <中间层>` 进去看看。Earthfile 要复现中间状态要用：

```bash
earthly --interactive +build
```

这会在 `+build` 失败时自动 drop 进一个 shell，你能看到容器里文件状态。但只能在**失败**时触发，不支持"进到某个 target 的中间状态去看看"。

### 坑 5：Earthly 不是 Kubernetes Native

Earthly 本质是 "本地 / Satellite 上的 BuildKit 封装"。你没法像 Tekton 那样在 K8s 里部署一堆 Earthly Pod 承接并发任务。CI runner 上装 earthly binary 然后连 satellite，是目前的主流用法。

对习惯了 K8s Native CI（Tekton / Argo Workflows）的团队，Earthly 模型略显"本地化"。

## 什么时候选 Earthly

**选 Earthly 的场景**：
- Monorepo，多语言（Go + Node + Python 等混合）
- 已经在用 Dockerfile + Makefile，感觉难维护
- 团队对 Bazel 望而生畏
- 希望构建系统足够简单（一天上手）

**不选 Earthly 的场景**：
- 单体服务，一个 Dockerfile 就够了
- 已经上了 Bazel，迁移成本不值
- 所有构建逻辑都是 Go，ko 可能更极致
- 强依赖 CI 平台原生 cache（GHA cache），不想引入新工具

## 结语

Earthly 服务的是"从 Dockerfile 毕业、但还没准备好上 Bazel"的那类团队，50-500 人的 Monorepo 公司最受用。真正让我愿意推它的点不是性能，是它把 Dockerfile 扩成了一个能 target、能 import、能并发、能跨 target 引用的 DSL，配上 Satellites 远端缓存，一个下午就能看出效果。

Monorepo 构建痛点开始冒头的时候，它值得一试。

Sources:
- [Earthly official site](https://earthly.dev/)
- [Earthfiles reference](https://earthly.dev/earthfile/)
- [Earthly GitHub](https://github.com/earthly/earthly)
- [Earthly for Monorepos](https://earthly.dev/monorepos)
- [Earthly Satellites best practices](https://docs.earthly.dev/earthly-cloud/satellites/best-practices)
