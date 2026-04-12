---
title: "Istio Ambient Mode 无 Sidecar 服务网格实践"
date: 2025-11-08T10:00:00+08:00
draft: false
tags: ["Istio", "Service Mesh", "Ambient Mode", "Kubernetes", "云原生"]
categories: ["云原生"]
description: "深入拆解 Istio Ambient Mode 的两层架构设计，覆盖安装、接入、Waypoint 配置到生产迁移策略，彻底摆脱 Sidecar 模式的历史包袱。"
summary: "Sidecar 模式已经陪我们走了六七年，但它的问题也越来越难以忽视。Ambient Mode 不是缝缝补补，而是从架构层面重新设计了服务网格的数据面。本文从实际运维视角深入拆解 ztunnel + Waypoint 两层架构，并给出从 Sidecar 迁移到 Ambient 的完整路径。"
toc: true
math: false
diagram: false
keywords: ["Istio Ambient", "ztunnel", "Waypoint Proxy", "服务网格", "无 Sidecar"]
params:
  reading_time: true
---

## Sidecar 模式：六年之痒

Istio 的 Sidecar 模式在 2017 年发布时是一个相当优雅的设计——把所有网络逻辑下沉到 envoy proxy，业务代码完全不感知。但在大规模生产环境跑了几年之后，问题越来越难绕过：

**资源开销是第一道坎。** 每个 Pod 强制注入一个 Envoy，默认配置下 Envoy 请求 100m CPU 和 128Mi 内存。一个中等规模集群跑着 500 个 Pod，光 Envoy sidecar 就吃掉了 50 核 CPU 和 64 GB 内存的资源请求。Kubernetes 调度器按请求分配节点，这些资源实际上是废的——Envoy 平时根本用不到这么多。

**启动顺序是第二道坎。** Sidecar 和业务容器的启动顺序没有严格保证（Init Container 方案也只是缓解而不是根治）。常见的故障场景：业务容器先起来，发出的第一批请求因为 Envoy 还没就绪而被拒绝，或者 Envoy 还没完成 xDS 配置下发就开始转发流量导致路由错误。我们生产上出现过两次启动期间的流量抖动，排查了好久才定位到是 sidecar 就绪时序问题。

**CNI 竞争是第三道坎。** Istio CNI plugin 需要在 Pod 的网络命名空间里设置 iptables 规则，劫持进出流量。但各家 CNI（Cilium、Calico、Terway）自己也有 iptables/eBPF 规则，两者经常打架。阿里云 ACK 上用 Terway 搭配 Istio 的时候，我们踩过一个坑：Terway 在特定版本下对 iptables REDIRECT 链的处理和 Istio CNI 的预期不一致，导致部分 Pod 间流量走了两层 NAT，RTT 翻倍。

**调试复杂度是第四道坎。** 出现网络问题时，你面对的是：业务代码 → Envoy → iptables → 内核网络栈 → 对端内核网络栈 → 对端 iptables → 对端 Envoy → 对端业务代码。每一层都可能是问题所在，`istioctl proxy-config` 和 `istioctl analyze` 的输出动辄几百行，定位一个路由配置错误经常要花半天。

Ambient Mode 的出发点很直接：能不能把 mesh 的能力从 Pod 里挪出来，放到节点层面？

## Ambient Mode 架构：两层分离

Ambient Mode 在 2022 年 9 月合并进 Istio 主线，1.21 版本后进入 GA。它的核心设计是**把 L4 和 L7 处理拆成两个独立组件**，而不是像 Sidecar 那样全部塞进一个 Envoy。

### ztunnel：节点级 L4 代理

ztunnel（zero-trust tunnel）以 DaemonSet 形式运行，每个节点一个实例，负责处理所有 L4 流量：

- mTLS 加密（HBONE 协议，基于 HTTP/2 + CONNECT 隧道）
- 流量授权（L4 级别的 AuthorizationPolicy）
- 遥测数据采集（TCP 连接级别的 metrics 和 access log）

ztunnel 使用 Rust 编写，资源消耗极低。一个节点上跑着 50 个 Pod，ztunnel 只需要一个实例，静态内存占用约 20-30 MB。相比 50 个 Envoy sidecar 的开销，差距显而易见。

ztunnel 的流量劫持方式和 Sidecar 也完全不同。它不依赖 iptables REDIRECT，而是通过 **network namespace + 内核路由**，把进入 Pod 的流量路由到 ztunnel 的网络命名空间处理，再转发回目标 Pod。这个机制绕开了 CNI 的 iptables 链，和 Cilium/Terway 的兼容性问题从根本上消失了。

### Waypoint Proxy：按需部署的 L7 代理

Waypoint Proxy 是一个独立的 Envoy 实例，以普通 Deployment 形式运行在 namespace 或 service account 级别。它只处理需要 L7 能力的流量：

- HTTP 路由（HTTPRoute、VirtualService）
- 重试、超时、熔断
- 请求级别的授权（JWT 验证、header 匹配）
- 更细粒度的 metrics（请求级别而不是连接级别）

Waypoint 是**可选的**。如果你只需要 mTLS 和 L4 授权，根本不需要部署 Waypoint——ztunnel 足够。只有当 namespace 里某个服务需要 L7 能力的时候，才为它单独部署 Waypoint。

这个设计的好处是：一个 namespace 里 10 个服务，可能只有 2 个需要 A/B 测试或 JWT 鉴权，只给这 2 个服务部署 Waypoint，其他 8 个服务纯粹走 ztunnel，零 L7 开销。

### 流量路径对比

**Sidecar 模式下 Pod A → Pod B：**
```
Pod A 业务进程
  → iptables REDIRECT (出向)
  → Envoy (Pod A sidecar)
  → 网络
  → iptables REDIRECT (入向)
  → Envoy (Pod B sidecar)
  → Pod B 业务进程
```

**Ambient Mode 下 Pod A → Pod B（仅 L4）：**
```
Pod A 业务进程
  → ztunnel (节点 A)  # HBONE mTLS 隧道
  → 网络
  → ztunnel (节点 B)
  → Pod B 业务进程
```

**Ambient Mode 下 Pod A → Pod B（需要 L7）：**
```
Pod A 业务进程
  → ztunnel (节点 A)  # HBONE 隧道
  → Waypoint Proxy    # L7 处理（可在任意节点）
  → ztunnel (节点 B)
  → Pod B 业务进程
```

## 安装 Ambient Mode

环境要求：Kubernetes 1.27+，Helm 3.x，CNI 要求见下文。

### CNI 兼容性

Ambient Mode 对 CNI 的要求和 Sidecar 不同：

| CNI | 支持状态 | 备注 |
|-----|---------|------|
| Cilium | 支持（需 1.14.7+ 且禁用 Cilium 的 kube-proxy 替换） | 生产验证 |
| Calico | 支持 | 需 3.26+ |
| Flannel | 支持 | 功能最简单，兼容性最好 |
| Terway | 支持（阿里云 ACK）| 需 ACK 集群版本 1.26+ |
| AWS VPC CNI | 支持 | EKS 1.27+ |

### Helm 安装

```bash
# 添加 Istio Helm repo
helm repo add istio https://istio-release.storage.googleapis.com/charts
helm repo update

# 安装 base（CRD）
helm install istio-base istio/base \
  -n istio-system \
  --create-namespace \
  --version 1.23.0

# 安装 istiod（控制面）
helm install istiod istio/istiod \
  -n istio-system \
  --set profile=ambient \
  --version 1.23.0 \
  --wait

# 安装 CNI 插件（ambient 模式需要，但作用不同于 sidecar 模式）
helm install istio-cni istio/cni \
  -n istio-system \
  --set profile=ambient \
  --version 1.23.0 \
  --wait

# 安装 ztunnel
helm install ztunnel istio/ztunnel \
  -n istio-system \
  --version 1.23.0 \
  --wait
```

验证安装：

```bash
kubectl get pods -n istio-system
# 应该看到：istiod、istio-cni-node（DaemonSet）、ztunnel（DaemonSet）

kubectl get daemonset -n istio-system
# NAME             DESIRED   CURRENT   READY
# istio-cni-node   3         3         3
# ztunnel          3         3         3
```

## 接入 Ambient Mode

### 命名空间接入

Sidecar 模式通过给命名空间加 `istio-injection: enabled` 标签，触发 webhook 注入。Ambient Mode 用不同的标签：

```bash
# Sidecar 模式（旧）
kubectl label namespace my-app istio-injection=enabled

# Ambient Mode（新）
kubectl label namespace my-app istio.io/dataplane-mode=ambient
```

加了这个标签之后，命名空间里**不需要重启 Pod**——ztunnel 会自动感知节点上新的 Pod 并接管其流量。这是 Ambient Mode 的一大优势：存量 Pod 不需要滚动重启就能进入 mesh。

验证接入状态：

```bash
kubectl get pod -n my-app -o wide
# 查看 ztunnel 是否已经在管理这个 Pod
kubectl exec -n istio-system daemonset/ztunnel -- \
  curl -s localhost:15020/debug/workloads | jq '.[] | select(.namespace == "my-app")'
```

### 验证 mTLS

```bash
# 部署测试 Pod
kubectl apply -f - <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: sleep
  namespace: my-app
spec:
  containers:
  - name: sleep
    image: curlimages/curl
    command: ["sleep", "infinity"]
EOF

# 从 sleep Pod 请求另一个服务，查看 ztunnel 日志确认 mTLS
kubectl logs -n istio-system daemonset/ztunnel -f | grep "my-app"
# 应该看到类似：
# connection complete src=... dst=... direction=outbound bytes_sent=... tls=true
```

## 配置 Waypoint Proxy

Waypoint Proxy 需要显式创建。一般按 namespace 粒度部署，也可以按 service account 粒度（更细但更复杂）。

### 创建 Waypoint

```bash
# 为整个 namespace 创建 Waypoint
istioctl waypoint apply -n my-app --enroll-namespace

# 或者通过 yaml（推荐生产环境，纳入 GitOps）
kubectl apply -f - <<EOF
apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
metadata:
  name: waypoint
  namespace: my-app
  annotations:
    istio.io/waypoint-for: service
spec:
  gatewayClassName: istio-waypoint
  listeners:
  - name: mesh
    port: 15008
    protocol: HBONE
EOF

# 让 namespace 使用这个 waypoint
kubectl label namespace my-app \
  istio.io/use-waypoint=waypoint
```

验证 Waypoint 运行：

```bash
kubectl get gateway -n my-app
# NAME       CLASS            ADDRESS       PROGRAMMED   AGE
# waypoint   istio-waypoint   10.96.x.x     True         30s

kubectl get pods -n my-app -l gateway.istio.io/managed=istio-waypoint
# 应该看到 waypoint-xxx Pod 在运行
```

### HTTPRoute 配置示例

Ambient Mode 优先使用 Kubernetes Gateway API，而不是 Istio 自己的 VirtualService（后者也支持，但长期会被 Gateway API 取代）：

```yaml
# 流量分割：将 my-service 的流量 80/20 分到两个版本
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: my-service-route
  namespace: my-app
spec:
  parentRefs:
  - group: ""
    kind: Service
    name: my-service
    port: 8080
  rules:
  - backendRefs:
    - name: my-service-v1
      port: 8080
      weight: 80
    - name: my-service-v2
      port: 8080
      weight: 20
```

```yaml
# 重试配置
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: my-service-retry
  namespace: my-app
spec:
  parentRefs:
  - group: ""
    kind: Service
    name: my-service
    port: 8080
  rules:
  - backendRefs:
    - name: my-service
      port: 8080
    filters:
    - type: RequestMirror  # 流量镜像
      requestMirror:
        backendRef:
          name: my-service-canary
          port: 8080
```

如果你原来用的是 VirtualService，Ambient Mode 也支持，不需要立即迁移：

```yaml
# VirtualService 在 Ambient 下依然有效
apiVersion: networking.istio.io/v1
kind: VirtualService
metadata:
  name: my-service
  namespace: my-app
spec:
  hosts:
  - my-service
  http:
  - route:
    - destination:
        host: my-service
        subset: v1
      weight: 80
    - destination:
        host: my-service
        subset: v2
      weight: 20
    retries:
      attempts: 3
      perTryTimeout: 2s
```

### L4 授权策略

不部署 Waypoint 也可以用 AuthorizationPolicy，但只能做 L4 级别（按 IP、端口、服务身份）：

```yaml
apiVersion: security.istio.io/v1
kind: AuthorizationPolicy
metadata:
  name: allow-frontend
  namespace: my-app
spec:
  selector:
    matchLabels:
      app: backend
  action: ALLOW
  rules:
  - from:
    - source:
        principals:
        - "cluster.local/ns/my-app/sa/frontend"
```

部署了 Waypoint 后，AuthorizationPolicy 可以做 L7 匹配（HTTP method、path、header）：

```yaml
apiVersion: security.istio.io/v1
kind: AuthorizationPolicy
metadata:
  name: allow-get-only
  namespace: my-app
spec:
  targetRef:
    group: gateway.networking.k8s.io
    kind: Gateway
    name: waypoint
  action: ALLOW
  rules:
  - from:
    - source:
        principals:
        - "cluster.local/ns/my-app/sa/readonly-client"
    to:
    - operation:
        methods: ["GET"]
```

## 可观测性

### Metrics

Ambient Mode 的 metrics 分两层：

- **ztunnel metrics**：TCP 连接级别，`connection_security_policy`、`tcp_sent_bytes_total`、`tcp_received_bytes_total`
- **Waypoint metrics**：请求级别，和 Sidecar 模式的 Envoy metrics 格式基本一致

```bash
# 查看 ztunnel metrics
kubectl exec -n istio-system daemonset/ztunnel -- \
  curl -s localhost:15020/metrics | grep ztunnel_

# Prometheus 抓取配置（ztunnel）
# ztunnel 会在 Pod annotation 上暴露 prometheus.io/scrape=true
kubectl get pod -n istio-system -l app=ztunnel \
  -o jsonpath='{.items[0].metadata.annotations}'
```

Prometheus 推荐的 scrape config：

```yaml
# prometheus.yml 片段
scrape_configs:
  - job_name: 'ztunnel'
    kubernetes_sd_configs:
    - role: pod
      namespaces:
        names: ['istio-system']
    relabel_configs:
    - source_labels: [__meta_kubernetes_pod_label_app]
      regex: ztunnel
      action: keep
    - source_labels: [__meta_kubernetes_pod_ip]
      target_label: __address__
      replacement: '${1}:15020'
```

### 访问日志

ztunnel 的访问日志格式和 Envoy 不同，是结构化 JSON：

```json
{
  "timestamp": "2025-11-08T02:00:00.000Z",
  "level": "info",
  "src": {"workload": "frontend-xxx", "namespace": "my-app"},
  "dst": {"workload": "backend-xxx", "namespace": "my-app"},
  "direction": "outbound",
  "bytes_sent": 1234,
  "bytes_received": 5678,
  "duration": "2ms",
  "tls": true,
  "response_flags": "-"
}
```

开启 ztunnel 访问日志：

```bash
helm upgrade ztunnel istio/ztunnel \
  -n istio-system \
  --set accessLog=true
```

### 与 Kiali 集成

Kiali 从 1.73 版本开始支持 Ambient Mode，但需要部署 Prometheus 和 Jaeger（或 Tempo）。注意：Kiali 的流量图在 Ambient Mode 下依赖 Waypoint Proxy 产生的 L7 metrics，纯 ztunnel 流量只有 TCP 级别的图。

## 生产迁移策略

从 Sidecar 迁移到 Ambient 不是一键切换，需要逐步推进。

### 阶段一：并行验证（1-2 周）

选一个低风险的 namespace（比如内部工具、监控组件），切换到 Ambient，同时保持其他 namespace 还在 Sidecar 模式。验证：

```bash
# 切换测试 namespace
kubectl label namespace monitoring \
  istio-injection-  # 移除 sidecar 注入标签
kubectl label namespace monitoring \
  istio.io/dataplane-mode=ambient

# 重启 namespace 里的 Pod（移除 sidecar 容器）
kubectl rollout restart deployment -n monitoring
```

验证期间检查：
- 服务间 mTLS 是否正常（查 ztunnel 日志中的 `tls=true`）
- AuthorizationPolicy 是否按预期生效
- metrics 数据是否正常上报

### 阶段二：按业务域推进（2-4 周）

从依赖最少、影响面最小的 namespace 开始，逐个切换。每切换一个 namespace，观察 1-2 天再继续。

关键检查项：

```bash
# 确认没有 Pod 还带着旧的 sidecar
kubectl get pod -n my-app \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{range .spec.containers[*]}{.name}{" "}{end}{"\n"}{end}' \
  | grep istio-proxy

# 确认 ztunnel 已经接管流量
istioctl ztunnel-config workload -n my-app
```

### 阶段三：清理 Sidecar 基础设施（迁移完成后）

所有 namespace 迁移完成后：

```bash
# 移除 istiod 的 sidecar 注入 webhook（可选，如果完全不再用 sidecar）
kubectl delete mutatingwebhookconfiguration istio-sidecar-injector

# 删除各 namespace 的旧注解
kubectl get namespace -o json | \
  jq -r '.items[] | select(.metadata.labels["istio-injection"]=="enabled") | .metadata.name' | \
  while read ns; do
    kubectl label namespace $ns istio-injection-
    echo "Cleaned: $ns"
  done
```

### 迁移中的常见问题

**问题 1：AuthorizationPolicy 行为变化**

Sidecar 模式下，AuthorizationPolicy 的 `selector` 匹配目标 Pod。Ambient Mode 下，如果部署了 Waypoint，policy 要指向 Waypoint（用 `targetRef`），否则只能做 L4 过滤。如果你的 policy 依赖 L7 属性（HTTP method、path），必须先部署 Waypoint 再迁移。

**问题 2：某些 CNI 版本不兼容**

Cilium 在启用 kube-proxy replacement 的情况下和 ztunnel 有冲突。检查：

```bash
# 确认 Cilium 没有接管 kube-proxy
kubectl -n kube-system exec daemonset/cilium -- \
  cilium status | grep "KubeProxyReplacement"
# 必须是 "KubeProxyReplacement: Disabled" 或 "Partial"
```

**问题 3：headless Service 流量**

Ambient Mode 对 headless Service（ClusterIP: None）的处理和 Sidecar 有差异。如果你的应用（比如 StatefulSet）依赖 headless Service 做服务发现，迁移前要单独测试。

## 适用场景和局限性

**Ambient Mode 的优势场景：**

1. **大规模部署**：Pod 数量 > 200，sidecar 资源开销已经无法忽视
2. **频繁扩缩容**：Sidecar 注入会增加 Pod 启动时间（webhook 调用 + Envoy 初始化），Ambient 模式下 Pod 启动不涉及 sidecar 注入
3. **CNI 兼容性问题**：已经踩过 iptables 冲突的集群
4. **批处理工作负载**：Job/CronJob 的短生命周期 Pod，sidecar 的生命周期管理本来就很麻烦

**还需要 Sidecar 的场景：**

1. **Per-pod 细粒度 L7 策略**：Ambient 的 Waypoint 是 per-namespace/per-service-account，如果需要每个 Pod 独立的 L7 策略，Sidecar 更灵活
2. **特殊协议**：gRPC-Web、某些私有协议，ztunnel 的 L4 处理可能不够，而 Waypoint 的配置比 Sidecar 更复杂
3. **本地流量调试**：`istioctl proxy-config` 这套工具链在 Ambient 下不适用，用 `istioctl ztunnel-config` 替代，但功能还没 Sidecar 完善
4. **多集群 east-west gateway**：Ambient 的多集群支持在 1.23 还是 beta，Sidecar 模式的多集群方案更成熟

**当前限制（截至 Istio 1.23）：**

- Waypoint 不支持 TCP 流量的 L7 策略（只有 HTTP/gRPC）
- `istio-proxy` sidecar 和 Ambient 的混用（同一 namespace）目前不支持
- Envoy 的部分 EnvoyFilter 扩展需要通过 Waypoint 配置，姿势和 Sidecar 不一样

Ambient Mode 现在已经够稳定用于生产，但它不是银弹。新集群直接上 Ambient；老集群有 Sidecar 模式运行稳定的，除非资源压力或 CNI 兼容问题比较突出，不需要急着迁移。
