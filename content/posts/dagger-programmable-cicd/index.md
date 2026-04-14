---
title: "Dagger 实战：用代码而不是 YAML 编写 CI/CD"
date: 2026-01-21T10:00:00+08:00
draft: false
tags: ["Dagger", "CI/CD", "BuildKit", "编程化流水线"]
categories: ["CI/CD"]
description: "Dagger 是一个用 Go/Python/TypeScript 代码写 CI 流水线的引擎。底层是定制版 BuildKit，每个操作自动缓存、内容寻址。本文讲 Dagger 的模型、Module 系统、本地与 CI 一致性、以及我们把 GitLab CI YAML 替换成 Dagger module 的实战。"
summary: "每次迁移 CI 平台（Jenkins → GitLab → GitHub Actions → Tekton），业务流水线都要重写一遍。Dagger 的思路是：把流水线写成可移植的代码（Go/Python/TS），底层引擎负责执行和缓存，CI 平台只是调用方。本文讲清楚它怎么工作、什么时候值得引入。"
toc: true
math: false
diagram: true
keywords: ["Dagger", "Dagger Module", "BuildKit", "可编程 CI", "SDK"]
params:
  reading_time: true
---

## 为什么是 Dagger

过去五年里我经历过三次 CI 平台迁移：从 Jenkins Pipeline 迁到 GitLab CI，从 GitLab CI 迁到 GitHub Actions，又从 GitHub Actions 迁到 Tekton。每次迁移都做同一件事：**把业务构建/测试/部署逻辑从一种 YAML DSL 翻译成另一种 YAML DSL**。

迁移成本巨大，而且每次迁移都是有损的：

- Jenkins 的 `shared library` 和 Groovy DSL，搬到 GitLab 后变成一堆 `include:`
- GitLab 的 `extends` 和 `rules`，搬到 GitHub 后变成 composite action 和 `if:`
- GitHub 的 matrix strategy，搬到 Tekton 后变成一堆手写的 DAG

每次迁移都要至少一个季度、一个小团队、一大堆 "构建时间回归" 的踩坑。

Dagger 的核心主张：**把 CI 逻辑从 YAML 解放出来，写成真正的代码**。这段代码在本地、在 GitHub Actions、在 GitLab、在 Jenkins、在 Tekton、在你妈妈家的电脑上跑起来都是一样的。CI 平台退化为"触发器 + 调度 + 环境变量注入"，真正的流水线逻辑是可测试、可复用、可版本化的代码。

更具体地说，Dagger 提供：

- **Go/Python/TypeScript SDK**：用你熟悉的语言写流水线，有 IDE 补全、有单元测试、有 linter
- **定制版 BuildKit 引擎**：每个操作自动内容寻址缓存，不管你是 "拉镜像" 还是 "跑 npm install" 还是 "执行 kubectl apply"
- **Module 系统**：把流水线组件打包成可复用模块，用 `dagger call` 像调 CLI 一样调用
- **本地-CI 一致性**：`dagger call ci` 在本地和在 GitHub Actions 上跑出来的结果/日志/缓存行为完全一致

## Dagger 的心智模型

传统 CI 的抽象：**步骤（Step）+ 环境（Runner）**。

```yaml
# GitHub Actions
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: go build
      - run: go test
      - run: docker build -t app .
```

Dagger 的抽象：**容器（Container）+ 管道（Pipeline）+ 缓存（Cache）**。

```go
// Go SDK
func (m *MyCI) Build(ctx context.Context, source *dagger.Directory) *dagger.Container {
    return dag.Container().
        From("golang:1.23-bookworm").
        WithMountedCache("/go/pkg/mod", dag.CacheVolume("go-mod")).
        WithMountedCache("/root/.cache/go-build", dag.CacheVolume("go-build")).
        WithMountedDirectory("/src", source).
        WithWorkdir("/src").
        WithExec([]string{"go", "build", "-o", "/out/app", "./cmd/server"})
}
```

区别在哪？

- **不再有 "runner" 概念**。整个流水线是一堆 "在容器里执行的操作"，Dagger Engine 负责在本地 Docker 或远端 K8s 里起这些容器。
- **声明式构建容器**：`.From()` + `.WithX()` 链式调用，每个方法返回新的 Container（immutable），和 Dockerfile 的 RUN/COPY 一一对应。
- **CacheVolume 是 API 一等公民**：不是 Dockerfile 里的 `RUN --mount=type=cache` 副作用，是 Go 代码里显式创建的对象。
- **所有操作自动缓存**：改一行代码，只重跑受影响的方法，其它方法的结果从缓存拿。

## 5 分钟跑一个 Dagger Pipeline

### 安装

```bash
# macOS / Linux
curl -L https://dl.dagger.io/dagger/install.sh | BIN_DIR=$HOME/.local/bin sh

dagger version
# dagger v0.14.0 (registry.dagger.io/engine) linux/amd64
```

Dagger 需要一个 Engine 后端。它会自动在本地 Docker 里起一个 `registry.dagger.io/engine` 容器（类似 BuildKit 但是 Dagger 自己的分发）。

### 初始化 Module

```bash
mkdir my-app && cd my-app
dagger init --sdk=go --name=myci
```

这会生成：

```
./
├── .dagger/
│   ├── dagger.json           # Module metadata
│   └── main.go               # 你的 pipeline 代码
├── go.mod
└── go.sum
```

打开 `.dagger/main.go`：

```go
package main

import (
    "context"
    "dagger/myci/internal/dagger"
)

type Myci struct{}

// Build 构建 Go 二进制
func (m *Myci) Build(
    ctx context.Context,
    // +defaultPath="/"
    source *dagger.Directory,
) *dagger.Container {
    return dag.Container().
        From("golang:1.23-bookworm").
        WithMountedCache("/go/pkg/mod", dag.CacheVolume("go-mod")).
        WithMountedCache("/root/.cache/go-build", dag.CacheVolume("go-build")).
        WithMountedDirectory("/src", source).
        WithWorkdir("/src").
        WithExec([]string{"go", "build", "-o", "/app", "./cmd/server"})
}

// Test 跑单元测试
func (m *Myci) Test(
    ctx context.Context,
    // +defaultPath="/"
    source *dagger.Directory,
) (string, error) {
    return dag.Container().
        From("golang:1.23-bookworm").
        WithMountedCache("/go/pkg/mod", dag.CacheVolume("go-mod")).
        WithMountedDirectory("/src", source).
        WithWorkdir("/src").
        WithExec([]string{"go", "test", "-v", "./..."}).
        Stdout(ctx)
}

// Publish 构建镜像并推送
func (m *Myci) Publish(
    ctx context.Context,
    // +defaultPath="/"
    source *dagger.Directory,
    registry string,
) (string, error) {
    binary := m.Build(ctx, source).File("/app")
    return dag.Container().
        From("gcr.io/distroless/static-debian12:nonroot").
        WithFile("/app", binary).
        WithEntrypoint([]string{"/app"}).
        Publish(ctx, registry)
}
```

### 调用

```bash
# 跑测试
dagger call test --source=.

# 构建并输出二进制
dagger call build --source=. file --path=/app export --path=./app.bin

# 发布镜像
dagger call publish --source=. --registry=ghcr.io/org/app:latest
```

`dagger call` 是通用入口。它：

1. 解析 `--source=.` 这些参数，自动匹配到 `Build` 函数的 `source` 参数
2. 在后台启动 Dagger Engine（如果没在跑）
3. 从 engine 容器内调用 SDK，执行 Go 代码
4. 把每个 `.WithExec()` 调用转成 BuildKit LLB 节点，DAG 化执行
5. 自动缓存每一步的输出，key 是内容 hash

第一次跑 `dagger call build` 可能要 60-90 秒（下载 `golang:1.23-bookworm`、`go mod download`、`go build`）。第二次只改一行业务代码，可能只要 5-10 秒（base image 命中、go.sum 未变 mod download 命中、仅 go build 重新执行）。

## 关键概念细讲

### Container 是 immutable 的 builder

每次 `.WithX()` 调用返回的是**新**的 Container。这不是 "mutate current state"，是 "生成一条新的 LLB 节点"。

```go
base := dag.Container().From("alpine:3.20")

c1 := base.WithExec([]string{"apk", "add", "curl"})
c2 := base.WithExec([]string{"apk", "add", "jq"})
// base 没变。c1 和 c2 是两个独立的构建状态。
```

这个模型让流水线天然可组合：你可以把一个 Container 传给下一个函数继续加工。

### CacheVolume 是真正的持久缓存

```go
goMod := dag.CacheVolume("go-mod")
ctr := dag.Container().
    From("golang:1.23").
    WithMountedCache("/go/pkg/mod", goMod).
    WithExec([]string{"go", "mod", "download"})
```

`CacheVolume("go-mod")` 创建（或复用）一个具名卷。这个卷的数据跨不同 Dagger 调用持久化（只要 Engine 不被销毁）。

和 BuildKit 的 `RUN --mount=type=cache` 最大的区别：**CacheVolume 的生命周期绑在 Dagger Engine 上**，而 Engine 本身可以是长生命周期的（本地 Docker 里常驻的一个容器）。这让本地开发的构建缓存跨天都能保持有效。

CacheVolume 在 CI 环境里稍微复杂：CI 是短生命周期的，Engine 起来又销毁，缓存随之丢。Dagger 提供两种解决方案：

1. **Dagger Cloud**（付费）：远端 cache 服务，每个团队共享。
2. **Self-hosted Engine**：在 K8s 里常驻一个 Dagger Engine pod，CI 通过 `_EXPERIMENTAL_DAGGER_RUNNER_HOST` 连上去，共享 cache volume。

### Function 的参数和返回值

Dagger SDK 用"约定优于配置"的方式把 Go 函数暴露为 CLI。规则：

- 公开方法会被自动暴露为 `dagger call <method>`
- 参数对应 CLI flag（驼峰转 kebab-case：`sourceDir` → `--source-dir`）
- 参数类型只能是 Dagger 原生类型（`Directory`、`File`、`Container`、`Secret`、`CacheVolume`）或 Go 基础类型
- 返回值必须是 Dagger 对象或基础类型，返回 `(X, error)` 表示可失败

特殊装饰器注释：

```go
// +defaultPath="/"         → 默认值是当前目录
// +optional                → 可选参数
// +private                 → 不暴露为 CLI（只能 Go 内部调用）
// +doc="..."               → 帮助文本
```

### Secret 的安全传递

密码、token 不能直接写死在 Go 代码里，Dagger 提供 `Secret` 类型：

```go
func (m *Myci) Publish(
    ctx context.Context,
    source *dagger.Directory,
    registry string,
    token *dagger.Secret,
) (string, error) {
    return dag.Container().
        From("alpine:3.20").
        WithSecretVariable("REGISTRY_TOKEN", token).
        WithExec([]string{"sh", "-c", "docker login -u bot -p $REGISTRY_TOKEN"}).
        // ...
}
```

CLI 注入：

```bash
# 从环境变量
dagger call publish --token=env:GITHUB_TOKEN --source=. --registry=ghcr.io/org/app

# 从文件
dagger call publish --token=file:./token.txt --source=. ...

# 从 stdin
echo $GITHUB_TOKEN | dagger call publish --token=stdin --source=. ...
```

`Secret` 类型在日志里会被自动 mask，并且不会被写进 cache key。这是很重要的安全边界：你不希望 rotating token 导致整个 cache 失效。

## Module 系统：可复用的流水线组件

Dagger 0.11+ 引入了 Module 系统。一个 Module 就是一个独立的 "流水线库"，可以被发布、引用、组合。

### 自己写一个 Go build module

```go
// .dagger/main.go
package main

import (
    "context"
    "dagger/golang/internal/dagger"
)

type Golang struct {
    // 默认 Go 版本
    Version string
}

// New 构造函数，允许外部注入版本
func New(
    // +optional
    // +default="1.23"
    version string,
) *Golang {
    return &Golang{Version: version}
}

// Base 返回带缓存的 Go 构建容器
func (g *Golang) Base() *dagger.Container {
    return dag.Container().
        From("golang:"+g.Version+"-bookworm").
        WithMountedCache("/go/pkg/mod", dag.CacheVolume("go-mod-"+g.Version)).
        WithMountedCache("/root/.cache/go-build", dag.CacheVolume("go-build-"+g.Version)).
        WithEnvVariable("CGO_ENABLED", "0")
}

// Test 跑测试
func (g *Golang) Test(
    ctx context.Context,
    source *dagger.Directory,
    // +optional
    pkg string,
) (string, error) {
    if pkg == "" {
        pkg = "./..."
    }
    return g.Base().
        WithMountedDirectory("/src", source).
        WithWorkdir("/src").
        WithExec([]string{"go", "test", "-v", "-race", pkg}).
        Stdout(ctx)
}

// Build 构建二进制
func (g *Golang) Build(
    source *dagger.Directory,
    pkg string,
    // +optional
    ldflags string,
) *dagger.File {
    args := []string{"go", "build", "-trimpath", "-o", "/out/bin"}
    if ldflags != "" {
        args = append(args, "-ldflags="+ldflags)
    }
    args = append(args, pkg)
    return g.Base().
        WithMountedDirectory("/src", source).
        WithWorkdir("/src").
        WithExec(args).
        File("/out/bin")
}

// Lint 跑 golangci-lint
func (g *Golang) Lint(
    ctx context.Context,
    source *dagger.Directory,
) (string, error) {
    return dag.Container().
        From("golangci/golangci-lint:v1.61.0").
        WithMountedCache("/root/.cache/golangci-lint", dag.CacheVolume("golangci-lint")).
        WithMountedDirectory("/src", source).
        WithWorkdir("/src").
        WithExec([]string{"golangci-lint", "run", "--timeout=10m", "./..."}).
        Stdout(ctx)
}
```

发布到 GitHub：

```bash
git add .dagger/
git commit -m "feat: add golang dagger module"
git push
```

### 在其它项目引用这个 module

```bash
# 安装这个 module
dagger install github.com/org/dagger-modules/golang

# 调用
dagger call -m github.com/org/dagger-modules/golang test --source=. --pkg=./...
dagger call -m github.com/org/dagger-modules/golang build --source=. --pkg=./cmd/server

# 或者在自己的 .dagger/main.go 里用
```

```go
import "dagger/myci/internal/dagger"

func (m *Myci) Ci(ctx context.Context, source *dagger.Directory) error {
    golang := dag.Golang(dagger.GolangOpts{Version: "1.23"})

    // 并发跑 lint 和 test
    errs := make(chan error, 2)
    go func() {
        _, err := golang.Lint(ctx, source)
        errs <- err
    }()
    go func() {
        _, err := golang.Test(ctx, source, "./...")
        errs <- err
    }()
    for i := 0; i < 2; i++ {
        if err := <-errs; err != nil {
            return err
        }
    }
    return nil
}
```

Module 是 Dagger 的核心复用机制。社区的 [Daggerverse](https://daggerverse.dev) 收录了数百个公开 module，常见的 `golang`、`python`、`node`、`docker`、`helm`、`kubectl`、`terraform` 都有。你可以直接 install 用，或 fork 定制。

## Dagger 和 BuildKit 的关系

Dagger Engine 是 **custom BuildKit**。两者的差异：

| 维度 | 纯 BuildKit | Dagger |
|------|-------------|--------|
| 输入 | Dockerfile 或 LLB | SDK 代码（Go/Py/TS）|
| 输出 | 镜像 | 镜像 + 任意 artifact + return value |
| API 暴露 | `buildctl`/`buildx` | `dagger` CLI + SDK |
| 缓存 | Layer cache + mount cache | Layer cache + CacheVolume + Function-level cache |
| 语义 | "构建一个镜像" | "执行任意管道" |

所以 Dagger 不是 BuildKit 的替代品，是 **BuildKit 的上层抽象**。BuildKit 擅长 "构建镜像"，Dagger 擅长 "编排一切可容器化的操作"：构建镜像是其中一个场景，还可以跑测试、做部署、跑数据迁移、调 API。

## 落地案例：替换 GitLab CI YAML

我们公司有一个核心服务的 `.gitlab-ci.yml`，原本 650 行，包含：

- Go lint、test、coverage
- 多阶段 Docker 构建
- Trivy 扫描
- Helm chart lint + package
- 部署到 staging/prod 的 ArgoCD sync 触发

迁移到 Dagger 之后：

```
.dagger/
├── dagger.json
├── main.go             # 150 行
├── build.go            # 80 行
├── test.go             # 60 行
├── deploy.go           # 70 行
└── helpers.go          # 40 行
```

总共 400 行 Go 代码。而且：

- **可以 `go test` 测流水线本身**：我们对 `helpers.go` 里的版本号生成逻辑写了单元测试。
- **IDE 补全**：写 `.WithExec(["kubectl", "apply"])` 有补全，不会拼错字段。
- **重构友好**：改一个函数签名，编译器会告诉你所有调用点。
- **本地可跑**：开发者在笔记本上 `dagger call ci --source=.` 一次把整个流水线跑完，不需要 push 到 GitLab 等结果。

`.gitlab-ci.yml` 本身变得极短：

```yaml
stages: [ci]

ci:
  stage: ci
  image: registry.dagger.io/engine:v0.14.0
  services:
    - docker:dind
  variables:
    DOCKER_HOST: tcp://docker:2375
    _EXPERIMENTAL_DAGGER_CACHE_CONFIG: "type=s3,region=us-west-2,bucket=dagger-cache"
  script:
    - curl -fsSL https://dl.dagger.io/dagger/install.sh | sh
    - ./bin/dagger call ci --source=. --git-sha=$CI_COMMIT_SHA
```

GitLab CI 只负责触发，真正的流水线逻辑在 Go 代码里。未来如果要迁 GitHub Actions、Tekton、Jenkins，只需要写一份 30 行的 trigger config，不用重写流水线。

## 性能和缓存的实战

### 本地开发的缓存命中率

Dagger 的缓存基于内容寻址。每次 `dagger call` 运行时，它会：

1. 计算每个操作的输入 hash（容器镜像 digest、挂载目录内容、环境变量、命令参数）
2. 查 Engine 的 cache 里有没有这个 hash 对应的输出
3. 命中就直接返回，不命中就执行

这意味着：**只要输入不变，结果就从 cache 拿**。改一行 README 不会让 `go build` 重跑，因为 `source` 目录的内容 hash 变化但 `go build` 的输入（`*.go` 文件）没变——前提是你在参数里用 `filter` 过滤了无关文件。

```go
func (m *Myci) Build(
    ctx context.Context,
    // +defaultPath="/"
    // +ignore=["*.md", "docs/**", ".github/**"]
    source *dagger.Directory,
) *dagger.Container {
    // ...
}
```

`+ignore` 是 Dagger 的 pre-cache filtering：在 hash 计算之前就过滤掉不需要的文件，从源头避免无意义的 cache miss。这是 2025 年加的特性，对大 monorepo 影响巨大。

### CI 环境下的缓存策略

CI 的 runner 是短生命周期的，Dagger Engine 每次都是空的 cache，这时候怎么加速？

**方案 A：Dagger Cloud（付费）**

```bash
export DAGGER_CLOUD_TOKEN=xxx
dagger call ci ...
```

Dagger Cloud 是托管的 cache 服务。CI 跑的时候 engine 自动把 cache 上传/下载到 cloud。团队内所有 runner 共享同一个 cache，第二次跑同一个 commit 基本全命中。

**方案 B：自建 S3 cache**

```bash
export _EXPERIMENTAL_DAGGER_CACHE_CONFIG="type=s3,region=us-west-2,bucket=dagger-cache,mode=max"
dagger call ci ...
```

类似 BuildKit 的 S3 backend，把 cache 存 S3。需要 Engine 支持（0.13+）。

**方案 C：常驻 Engine Pod**

在 K8s 里部署一个长期运行的 Dagger Engine：

```yaml
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: dagger-engine
  namespace: ci
spec:
  serviceName: dagger-engine
  replicas: 1
  template:
    spec:
      containers:
        - name: engine
          image: registry.dagger.io/engine:v0.14.0
          securityContext:
            privileged: true
          volumeMounts:
            - name: data
              mountPath: /var/lib/dagger
  volumeClaimTemplates:
    - metadata:
        name: data
      spec:
        accessModes: [ReadWriteOnce]
        resources:
          requests:
            storage: 500Gi
        storageClassName: gp3
```

CI runner 通过 `_EXPERIMENTAL_DAGGER_RUNNER_HOST=tcp://dagger-engine.ci:7777` 连过来用，共享同一个 engine 的 cache。

## 坑和取舍

### 坑 1：Engine 要 privileged 权限

Dagger Engine 跑容器需要 privileged（至少要 CAP_SYS_ADMIN）。在 K8s 里部署要开 PodSecurityPolicy / PSS 例外。严格的多租户集群会挑刺，需要和安全团队沟通。

替代方案：Dagger 支持 rootless 模式但功能受限，生产一般不用。

### 坑 2：Dagger 本身是额外的依赖

YAML CI 的好处是 "零额外依赖"，GitLab/GitHub Runner 直接解析 YAML 执行。Dagger 多了一层抽象：你要装 dagger CLI、要起 Engine、要学 SDK 语法、要维护 `.dagger/` 代码。

对小团队（5 人以下、一个主仓库）来说这个成本不值。Dagger 最适合：

- 多仓库、跨语言，需要统一构建逻辑
- 流水线复杂度高（100+ 行 YAML）
- 对本地-CI 一致性有强需求
- 频繁迁移 CI 平台或多云部署

### 坑 3：模块版本管理

Dagger Module 引用方式是 `github.com/org/repo@branch-or-tag`。这是 Git 级别的引用，没有类似 Go module 的 semver 解析。如果你引用 `@main`，未来这个 module 有破坏性变更会直接打到你的流水线。

实践建议：**永远 pin 到 tag 或 commit SHA**：

```bash
dagger install github.com/org/dagger-modules/golang@v1.2.0
dagger install github.com/org/dagger-modules/golang@abc1234
```

并且配 Renovate bot 自动 PR 升级。

### 坑 4：调试 Dagger Function 比调试 Bash 脚本麻烦

Bash 脚本错了你直接 `set -x` 看每一步。Dagger 是 Go 代码编译后在 engine 里执行，栈信息要通过 TUI 日志看。

Dagger 0.13+ 的 TUI 做了很多改进：

```bash
dagger call ci ...
# 打开一个全屏 TUI，展示每个 step 的 DAG + 实时日志 + 缓存命中状态
```

按 Tab 键在 steps 之间切换，按 Enter 看详细日志。但比起 "一屏 shell 输出" 还是更重。

另外 `dagger` 默认不跑 DAG 的非必需分支，如果你只想看其中一个 function 的效果，精确 call 它：

```bash
dagger call test --source=.
```

只会跑 Test，不会跑 Build/Publish。

## 什么时候选 Dagger

我的判断标准：

**选 Dagger 的场景**：
- 流水线复杂、跨多仓库、跨多语言，希望统一抽象
- 频繁在本地复现 CI 问题，需要 local-CI 一致
- 团队里有 Go/Python/TS 能力，不排斥写代码
- 打算长期做 CI 平台解耦，不想再迁一次 YAML

**不选 Dagger 的场景**：
- 单个小项目，YAML 就能搞定
- 团队完全只会 Bash，不想学 SDK
- 不允许在 CI 里跑 privileged 容器
- 只依赖 CI 平台的原生功能（Actions marketplace、GitLab include）

## 结语

Dagger 不是取代 Tekton/GitHub Actions 的 CI 平台，它是**运行在任何 CI 平台之上的流水线引擎**。你依然要选一个 CI 平台做触发和调度，但流水线逻辑本身被抽成可移植的代码。

这个理念在 2023 年刚出来时有点超前，2026 年它已经有足够多的生产实践证明是可行的：GitLab、HuggingFace、Replicate、Roblox 等都在用。对中大型公司而言，Dagger 是解决 "CI 平台绑定" 这个长期痛点的最优解之一。

如果你正在做新 CI 平台选型，强烈建议在 Tekton/GitHub Actions 之外，把 Dagger 也纳入 POC 名单。用一两个非关键服务跑一个月，感受一下用 Go 写 CI 是什么体验。很多时候选型不是 "选 Dagger 还是选 Tekton"，而是 "Dagger 写流水线代码 + Tekton 做触发调度" 的组合。

Sources:
- [Dagger overview docs](https://docs.dagger.io/)
- [Dagger GitHub](https://github.com/dagger/dagger)
- [Dagger 0.13 release](https://dagger.io/blog/dagger-0-13)
- [Dagger Python SDK](https://dagger.io/blog/python-sdk)
- [Dagger TypeScript SDK performance](https://dagger.io/blog/typescript-sdk-performance)
- [Building a Dagger module for Go](https://www.felipecruz.es/building-a-dagger-module/)
