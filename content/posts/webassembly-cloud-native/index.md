---
title: "WebAssembly 在云原生中的应用：从浏览器到 K8s 数据面"
date: 2025-11-08T14:00:00+08:00
draft: false
tags: ["WebAssembly", "Wasm", "Kubernetes", "云原生", "Envoy", "安全"]
categories: ["云原生"]
description: "Wasm 早已不是前端的专属玩具。本文从云原生工程师视角系统拆解 WASI、containerd+runwasi、SpinKube、Envoy 插件扩展、OPA 替代以及 AI Agent 沙箱等场景的实际落地路径和成熟度评估。"
summary: "WebAssembly 在云原生领域的热度持续上涨，但很多讨论都停留在概念层面。这篇文章试图给出一个务实的视角：Wasm 在哪些云原生场景已经可以生产落地，在哪些场景还需要等待，以及和容器相比的真实差异。"
toc: true
math: false
diagram: false
keywords: ["WebAssembly", "WASI", "runwasi", "SpinKube", "Envoy Wasm 插件", "云原生沙箱"]
params:
  reading_time: true
---

## 为什么是 Wasm，不是更多容器

先把一个误会解了：这里讨论的 Wasm 和前端那个是同一个字节码，但用法完全不同。浏览器里的 Wasm 是把 C++/Rust 塞进 JS 引擎跑；云原生里的 Wasm 是服务端一个**比容器更轻、比进程更安全**的执行单元。

容器本来是用来解决进程隔离 + 运行时打包的，Wasm 在字节码层就自带这两个能力——所以它在"容器用着嫌重"的场景里天然合适。

云原生 Wasm 的几个核心特性：

**安全边界清晰。** Wasm 模块默认没有任何系统调用能力——它运行在一个沙箱里，访问文件系统、网络、环境变量都必须通过宿主机显式授权的接口。这和容器的安全模型（seccomp + namespace + capabilities）相比，从设计上更难逃逸。

**启动极快。** 一个 Wasm 模块的冷启动时间在毫秒级甚至亚毫秒级，而一个容器（哪怕是最精简的）冷启动通常在百毫秒到秒级。对 serverless 和边缘计算场景意义重大。

**包体积小。** 一个完整的 Rust Wasm 业务模块通常 1-5 MB，而一个最小化的 Alpine 容器镜像也有 5-10 MB，实际业务容器镜像动辄 100MB 以上。

**跨架构。** 同一份 Wasm 字节码在 x86、ARM、RISC-V 上运行，无需重新编译。在多架构 K8s 集群（AWS Graviton 节点混部）里这是真实优势。

## WASI：给 Wasm 装上系统调用

浏览器里的 Wasm 和宿主机的 JS 引擎通信，靠的是 import/export 的函数。放到服务端，Wasm 模块需要和操作系统打交道（读文件、建 socket），不能靠浏览器 API，于是有了 WASI。

WASI（WebAssembly System Interface）是一套**标准化的系统调用接口**，定义了 Wasm 模块可以调用哪些宿主机能力。它不是一个实现，而是一组规范：

- **WASI Preview 1（wasi_snapshot_preview1）**：第一代，已稳定。涵盖文件 I/O、环境变量、随机数、时钟。目前绝大多数 Wasm 工具链（Rust `wasm32-wasi`、TinyGo、AssemblyScript）默认编译目标。
- **WASI Preview 2（wasi 0.2.0）**：2024 年初稳定。基于 Component Model 重新设计，引入了 wasi:io、wasi:http、wasi:sockets 等组件接口。Fermyon Spin、wasmtime 1.0+ 已经支持。
- **WASI 0.3**：在设计中，重点解决异步 I/O。

对运维工程师来说，记住一点就够：**WASI Preview 1 已经可以生产用，Preview 2 是未来方向，编译目标选 `wasm32-wasip1` 或 `wasm32-wasip2` 取决于你用的运行时。**

## containerd + runwasi：K8s 原生运行 Wasm

容器运行时接口（CRI）允许 K8s 对接不同的底层运行时。runwasi 是 containerd 的一个 shim，让 containerd 可以直接运行 OCI 格式打包的 Wasm 模块，**不需要把 Wasm 塞进一个容器镜像里跑**。

架构链路：

```
kubelet → CRI → containerd → containerd-shim-wasmtime-v1 (runwasi) → wasmtime → Wasm 模块
```

### 安装 runwasi

在 K8s 节点上安装 wasmtime shim（以 Ubuntu 22.04 为例）：

```bash
# 下载 runwasi 发行版（包含各 runtime 的 shim）
RUNWASI_VERSION=0.5.0
curl -LO https://github.com/containerd/runwasi/releases/download/containerd-shim-wasmtime/v${RUNWASI_VERSION}/containerd-shim-wasmtime-v${RUNWASI_VERSION}-x86_64-linux.tar.gz
tar xzf containerd-shim-wasmtime-*.tar.gz
mv containerd-shim-wasmtime-v1 /usr/local/bin/
chmod +x /usr/local/bin/containerd-shim-wasmtime-v1

# 重启 containerd
systemctl restart containerd
```

配置 containerd 使用 wasm shim（`/etc/containerd/config.toml`）：

```toml
[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.wasmtime]
  runtime_type = "io.containerd.wasmtime.v1"
```

### 创建 RuntimeClass

```yaml
apiVersion: node.k8s.io/v1
kind: RuntimeClass
metadata:
  name: wasmtime
handler: wasmtime
```

### 部署 Wasm 工作负载

Wasm 模块需要打包成 OCI 镜像（只是用 OCI 格式存储，不是真的容器）：

```bash
# 用 Rust 编译一个简单的 Wasm HTTP 服务
# Cargo.toml 里 target = "wasm32-wasip1"
cargo build --target wasm32-wasip1 --release

# 打包成 OCI 镜像
# wasm-to-oci 或直接用 oci-spec-rs
docker buildx build --platform wasi/wasm \
  -t registry.example.com/my-wasm-app:latest \
  --push .
```

```yaml
# Pod 使用 wasmtime RuntimeClass
apiVersion: v1
kind: Pod
metadata:
  name: wasm-hello
spec:
  runtimeClassName: wasmtime
  containers:
  - name: app
    image: registry.example.com/my-wasm-app:latest
    # Wasm 模块不需要 CMD，wasmtime 直接执行 _start 导出函数
```

**当前成熟度**：runwasi + containerd 的方案在 2024 年已经相对稳定，但仍有几个生产限制：不支持 GPU、不支持特权模式、调试工具链比容器稀缺。适合无状态、计算密集、安全要求高的场景。

## SpinKube：Wasm 微服务的更完整方案

runwasi 解决的是"K8s 能不能运行 Wasm"，SpinKube 解决的是"Wasm 微服务如何在 K8s 上管理生命周期"。

SpinKube = Fermyon Spin（Wasm 微服务框架）+ containerd-shim-spin（运行时）+ SpinApp CRD（K8s 自定义资源）+ Spin Operator（控制器）。

### 安装 SpinKube

```bash
# 安装 cert-manager（依赖）
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/latest/download/cert-manager.yaml

# 安装 SpinKube operator
helm install spin-operator \
  --namespace spin-operator \
  --create-namespace \
  --version 0.3.0 \
  --wait \
  oci://ghcr.io/spinkube/charts/spin-operator

# 安装 RuntimeClass 和 SpinAppExecutor
kubectl apply -f https://github.com/spinkube/spin-operator/releases/download/v0.3.0/spin-operator.runtime-class.yaml
kubectl apply -f https://github.com/spinkube/spin-operator/releases/download/v0.3.0/spin-operator.spin-app-executor.yaml
```

### 编写 Spin 应用

```rust
// src/lib.rs - 一个 Spin HTTP handler
use spin_sdk::http::{IntoResponse, Request, Response};
use spin_sdk::http_component;

#[http_component]
fn handle_request(req: Request) -> anyhow::Result<impl IntoResponse> {
    println!("收到请求: {:?}", req.headers());
    Ok(Response::builder()
        .status(200)
        .header("Content-Type", "application/json")
        .body(r#"{"status":"ok","runtime":"spin-wasm"}"#)
        .build())
}
```

```toml
# spin.toml
spin_manifest_version = 2

[application]
name = "my-service"
version = "0.1.0"

[[trigger.http]]
route = "/..."
component = "my-service"

[component.my-service]
source = "target/wasm32-wasip1/release/my_service.wasm"
[component.my-service.build]
command = "cargo build --target wasm32-wasip1 --release"
watch = ["src/**/*.rs", "Cargo.toml"]
```

### 部署 SpinApp

```yaml
apiVersion: core.spinoperator.dev/v1alpha1
kind: SpinApp
metadata:
  name: my-service
  namespace: production
spec:
  image: "registry.example.com/my-service:v1.0.0"
  replicas: 3
  executor: containerd-shim-spin
  resources:
    limits:
      cpu: 100m
      memory: 64Mi  # Wasm 应用内存占用极低
    requests:
      cpu: 10m
      memory: 16Mi
```

SpinKube 的 HPA 和普通 Deployment 一样配置，不需要特殊处理。冷启动时间 < 5ms，非常适合用 KEDA 做基于消息队列的弹性伸缩。

## Envoy/Istio Wasm 插件扩展

这是**目前云原生 Wasm 落地最成熟的场景**之一，生产可用。

Envoy 支持 Wasm 插件作为 HTTP filter，可以在请求/响应链路上插入自定义逻辑，替代 Lua filter 或 ext_proc 的部分场景。

### 为什么要用 Wasm 替代 Lua

Lua filter 的问题：
- Lua 运行在 Envoy 的 Lua JIT 里，没有严格的资源隔离
- Lua 代码一旦崩溃可能影响整个 Envoy 进程
- 没有类型系统，大型 Lua 脚本维护成本高
- 调试困难，没有好用的本地测试框架

Wasm 插件的优势：
- 每个插件运行在独立沙箱，崩溃不影响 Envoy 主进程
- 可以用 Rust/Go/AssemblyScript 编写，类型安全
- 本地用 `envoy` + Wasm 插件直接测试，和生产行为一致

### 用 Rust 写一个 Envoy Wasm 插件

```toml
# Cargo.toml
[package]
name = "rate-limit-header"
version = "0.1.0"
edition = "2021"

[lib]
crate-type = ["cdylib"]

[dependencies]
proxy-wasm = "0.2"
serde = { version = "1", features = ["derive"] }
serde_json = "1"
```

```rust
// src/lib.rs - 给所有响应注入限流头
use proxy_wasm::traits::*;
use proxy_wasm::types::*;

proxy_wasm::main! {{
    proxy_wasm::set_log_level(LogLevel::Trace);
    proxy_wasm::set_root_context(|_| -> Box<dyn RootContext> {
        Box::new(RateLimitRoot)
    });
}}

struct RateLimitRoot;
impl Context for RateLimitRoot {}
impl RootContext for RateLimitRoot {
    fn create_http_context(&self, _: u32) -> Option<Box<dyn HttpContext>> {
        Some(Box::new(RateLimitFilter))
    }
    fn get_type(&self) -> Option<ContextType> {
        Some(ContextType::HttpContext)
    }
}

struct RateLimitFilter;
impl Context for RateLimitFilter {}
impl HttpContext for RateLimitFilter {
    fn on_http_response_headers(&mut self, _: usize, _: bool) -> Action {
        self.set_http_response_header("X-Ratelimit-Limit", Some("1000"));
        self.set_http_response_header("X-Powered-By", Some("envoy-wasm"));
        Action::Continue
    }
}
```

```bash
# 编译
cargo build --target wasm32-unknown-unknown --release
# 产物：target/wasm32-unknown-unknown/release/rate_limit_header.wasm
```

### 在 Istio 中部署 Wasm 插件

Istio 1.12+ 支持通过 `WasmPlugin` CRD 分发 Wasm 插件，不需要手动把 wasm 文件放到每个节点：

```yaml
apiVersion: extensions.istio.io/v1alpha1
kind: WasmPlugin
metadata:
  name: rate-limit-header
  namespace: my-app
spec:
  selector:
    matchLabels:
      app: backend  # 只注入到 backend Pod 的 sidecar
  url: oci://registry.example.com/wasm/rate-limit-header:v1.0.0
  phase: STATS  # 在 stats filter 之后执行
  pluginConfig:
    max_requests: 1000
```

Istio 会自动把 Wasm 模块分发到匹配的 Envoy sidecar（或 Ambient Mode 的 Waypoint Proxy）。插件配置通过 `pluginConfig` 以 JSON 传入，插件代码用 `get_configuration()` 读取。

### 性能对比

根据 Envoy 官方测试数据（2024）：

| Filter 类型 | 额外延迟 P50 | 额外延迟 P99 | 内存（per worker） |
|------------|-------------|-------------|-------------------|
| Native C++ | ~0.01ms | ~0.05ms | 基准 |
| Lua filter | ~0.1ms | ~0.5ms | +2MB |
| Wasm filter | ~0.05ms | ~0.2ms | +5MB（wasm 运行时） |
| ext_proc | ~0.5ms+ | ~2ms+ | 取决于外部进程 |

Wasm 比 Lua 快，比 ext_proc 快得多，只比 native C++ 慢一点但安全隔离更好。

## Wasm 做 OPA 策略扩展

Open Policy Agent（OPA）是云原生策略引擎，但 Rego 语言对很多团队有学习成本，而且某些复杂策略（调用外部 API、复杂数据转换）用 Rego 写很痛苦。

OPA 从 0.35 版本开始支持 **Wasm 编译后的策略**：把 Rego 策略编译成 Wasm，嵌入到应用进程里执行，或者用 Wasm 直接写策略逻辑。

### OPA + Wasm 的两种用法

**方式一：把 Rego 编译成 Wasm（减少网络调用）**

```bash
# 把 Rego policy 编译成 wasm bundle
opa build -t wasm -e 'authz/allow' policy.rego

# 产物是一个 bundle.tar.gz，里面包含 policy.wasm
# 在应用里用 @open-policy-agent/opa-wasm（JS）或 wasmtime（Rust/Go）执行
```

这种方式的好处：策略在进程内执行，不需要 sidecar OPA 进程，延迟从 1-5ms 降到 0.1ms 以内。

**方式二：用 Rust/Go 编写自定义策略函数（Rego built-in）**

OPA 支持通过 Wasm 扩展内置函数：

```rust
// 注册一个 custom_jwt_verify built-in
// 让 Rego 可以调用：custom_jwt_verify(token, pubkey)
use opa_wasm_sdk::*;

#[no_mangle]
pub extern "C" fn custom_jwt_verify(token_ptr: i32, key_ptr: i32) -> i32 {
    // 自定义 JWT 验证逻辑（比如支持特殊算法）
    // 返回 1 表示有效，0 表示无效
    todo!()
}
```

实际上这种方式目前工具链还不成熟，大多数团队用的是方式一（Rego → Wasm 编译）。

### 在 K8s 准入控制中的应用

```yaml
# 用 OPA Gatekeeper + Wasm bundle 替代纯 Rego
# 把复杂的 Go 逻辑编译成 Wasm，在 Gatekeeper 中调用
apiVersion: templates.gatekeeper.sh/v1
kind: ConstraintTemplate
metadata:
  name: customjwtpolicy
spec:
  crd:
    spec:
      names:
        kind: CustomJwtPolicy
  targets:
  - target: admission.k8s.gatekeeper.sh
    rego: |
      package customjwtpolicy
      # 调用 Wasm 编译的内置函数
      violation[{"msg": msg}] {
        token := input.review.object.metadata.annotations["auth-token"]
        not custom_jwt_verify(token, data.pubkey)
        msg := "invalid JWT token"
      }
```

## Wasm vs 容器：数据对比

| 维度 | 容器（Alpine base） | Wasm（wasmtime） | 备注 |
|------|-------------------|-----------------|------|
| 冷启动时间 | 200ms - 2s | 1 - 10ms | Wasm 优势显著 |
| 最小镜像大小 | 5MB（Alpine）| 0.5 - 5MB | 依赖语言和框架 |
| 内存占用（hello world）| 10-30MB | 1-5MB | Wasm 更轻 |
| CPU overhead | 几乎为零 | 5-15%（JIT 解释）| 成熟 AOT 编译后接近零 |
| 安全隔离 | namespace + seccomp | 沙箱 + WASI 白名单 | Wasm 更严格 |
| 生态系统 | 极其成熟 | 快速成长但仍有差距 | 容器胜 |
| 调试工具 | 完善 | 基础可用 | 容器胜 |
| 状态管理 | 完整（volumes、PVC）| 有限（WASI fs）| 容器胜 |
| GPU 支持 | 支持 | 不支持 | 容器胜 |

冷启动时间数据来源：CNCF Wasm 工作组 2024 年基准测试，测试环境 AWS c6g.xlarge（Graviton 3）。

## AI Agent 沙箱：Wasm 的新战场

这是 2024-2025 年 Wasm 在云原生领域最值得关注的新场景：**用 Wasm 给 AI Agent 的代码执行能力提供安全隔离**。

AI Agent 执行用户或 LLM 生成的代码，安全风险极高：
- 恶意代码尝试读取 `/etc/passwd`、访问云 metadata 接口
- 代码死循环或内存溢出
- 横向移动，访问集群内其他服务

传统方案是给每个代码执行任务起一个容器（gVisor 加固），但冷启动 200ms+ 在交互式 Agent 场景下用户体验很差。

Wasm 的方案：

```rust
// 用 wasmtime 嵌入式运行时执行用户代码
use wasmtime::*;

fn execute_user_code(wasm_bytes: &[u8], input: &str) -> Result<String> {
    let engine = Engine::default();
    let mut store = Store::new(&engine, ());

    // 限制资源：最多执行 1 亿条指令，最多 64MB 内存
    let mut limits = StoreLimitsBuilder::new()
        .fuel(100_000_000)
        .memory_size(64 * 1024 * 1024)
        .build();
    store.limiter(|_| &mut limits);
    store.add_fuel(100_000_000)?;

    // 不暴露任何 WASI 接口，完全沙箱
    let module = Module::new(&engine, wasm_bytes)?;
    let instance = Instance::new(&mut store, &module, &[])?;

    let run = instance.get_typed_func::<(), i32>(&mut store, "run")?;
    let result = run.call(&mut store, ())?;

    Ok(format!("exit code: {}", result))
}
```

这个方案的好处：
- 冷启动 < 5ms（wasmtime 加载预编译的 Wasm 模块）
- 内存限制精确可控
- CPU 时间通过 fuel 机制精确计量和限制
- 无法访问文件系统、网络，除非宿主机显式允许

实际上 Cloudflare Workers、Fastly Compute@Edge 已经把这套方案运行在生产上，处理数十亿次请求。在私有 K8s 集群里复刻这套方案的工具链已经基本成熟。

**在 K8s 里部署 AI 代码执行沙箱：**

```yaml
# 用 SpinKube 部署代码执行沙箱服务
apiVersion: core.spinoperator.dev/v1alpha1
kind: SpinApp
metadata:
  name: code-executor
  namespace: ai-agents
spec:
  image: "registry.example.com/code-executor:v1.0.0"
  replicas: 5
  executor: containerd-shim-spin
  resources:
    limits:
      cpu: 500m
      memory: 512Mi  # Wasm 沙箱内的内存限制由代码控制，这里是节点级别限制
```

## 当前成熟度评估

给出一个直接的落地建议：

**现在就可以上生产的场景：**

1. **Envoy/Istio Wasm 插件**：替代 Lua filter，适合自定义认证、请求改写、遥测注入。工具链成熟，Istio WasmPlugin CRD 简化了分发。
2. **OPA Rego → Wasm 编译**：减少策略评估延迟，适合高频准入控制。只需要 `opa build -t wasm` 一条命令。
3. **边缘/CDN 计算**（如果用 Cloudflare Workers 或 Fastly）：完全成熟。

**2025 年可以试验，2026 年考虑生产的场景：**

4. **SpinKube 微服务**：适合无状态、短请求、安全要求高的服务。框架在快速成熟，但运维工具链（日志、调试、traceing 集成）还在追赶。
5. **AI Agent 代码执行沙箱**：技术可行，但需要投入工程时间搭建语言编译流水线（把用户提交的 Python/JS 转成 Wasm）。

**还太早，观望为主：**

6. **runwasi 替代通用容器**：大多数业务服务仍然需要依赖 glibc、数据库驱动等复杂库，迁移成本高，WASI 的 posix 兼容性还有缺口。
7. **有状态服务**：Wasm 的持久化存储方案还没有标准化，不适合数据库、消息队列这类工作负载。

**一个判断标准：** 如果你的工作负载是**无状态 + 短生命周期 + 安全敏感 + 启动时间关键**，Wasm 值得认真考虑。如果有其中一条不满足，容器仍然是更稳妥的选择。

Wasm 在云原生的定位不是取代容器，而是在容器不够轻、不够快、不够安全的地方填补空白。这个空白比五年前大了很多，工具链也成熟了很多，但距离大规模替代容器还有相当长的路要走。
