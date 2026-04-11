---
title: "K8s Gateway API：告别 Ingress，拥抱下一代流量路由"
date: 2026-04-11T13:00:00+08:00
draft: false
tags: ["Kubernetes", "Gateway API", "Ingress", "网络", "云原生", "2026"]
categories: ["Kubernetes"]
description: "Gateway API 已经 GA，是时候认真考虑从 Ingress 迁移了。本文梳理 Gateway API 的设计理念、实际配置示例和迁移注意事项。"
summary: "Gateway API 已经 GA，是时候认真考虑从 Ingress 迁移了。本文梳理 Gateway API 的设计理念、实际配置示例和迁移注意事项。"
toc: true
math: false
diagram: false
keywords: ["Kubernetes Gateway API", "Ingress", "HTTPRoute", "Envoy Gateway", "金丝雀发布", "流量路由"]
params:
  reading_time: true
---

## Ingress 的问题

Ingress 是 K8s 最早的流量入口抽象，用了这么多年，大家对它的局限性应该都有体会。

**功能受限，靠注解打补丁。** Ingress 规范只定义了最基础的路径匹配和 TLS 终止。稍微复杂一点的需求，比如超时设置、限流、Header 改写、跨域，统统要靠 `nginx.ingress.kubernetes.io/proxy-connect-timeout` 这类私有注解实现。不同实现（nginx-ingress、Traefik、Kong）的注解完全不一样，写的配置跟实现深度绑定，换个 Ingress Controller 就要重写。

**权限模型不合理。** Ingress 资源和 Service 在同一层，业务研发可以随意创建 Ingress，直接影响到集群入口的路由规则。在多租户场景下，这种设计让基础设施团队很难做权限管控——你总不能让所有人都只能操作同一个 Ingress 对象。

**协议支持不够。** 原生 Ingress 只支持 HTTP/HTTPS，TCP/UDP 路由、gRPC 都没有。各家实现用 CRD 扩展，但又是私有的。

Gateway API 就是在这个背景下设计出来的，目标是用一套标准 API 覆盖 Ingress 的所有场景，同时解决权限模型的问题。

## Gateway API 的设计分层

Gateway API 把流量路由拆成三层，对应三种角色：

```
基础设施管理员
    ↓ 管理
GatewayClass（定义使用什么实现，类似 StorageClass）
    ↓
Gateway（具体的负载均衡器实例，绑定端口/TLS/证书）
    ↑ 业务团队
HTTPRoute / TCPRoute / GRPCRoute（定义路由规则，指向 Service）
```

**GatewayClass** 是集群级别资源，由基础设施团队创建，定义使用哪种实现（Envoy Gateway、Cilium、Traefik 等）。

**Gateway** 定义一个具体的入口，包括监听的协议/端口/证书。通常也由基础设施团队管理，或者授权给特定 namespace 的管理员。

**HTTPRoute/TCPRoute/GRPCRoute** 定义实际的路由规则，指向具体的 Service。业务团队自己管理，不需要依赖基础设施团队。

这个分层解决了权限问题：基础设施团队控制 Gateway（入口能力），业务团队自主管理路由规则，互不干扰。

## 实际配置示例

### 基础 HTTPRoute

```yaml
# 基础设施团队创建
apiVersion: gateway.networking.k8s.io/v1
kind: GatewayClass
metadata:
  name: envoy
spec:
  controllerName: gateway.envoyproxy.io/gatewayclass-controller
---
apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
metadata:
  name: prod-gateway
  namespace: infra
spec:
  gatewayClassName: envoy
  listeners:
    - name: https
      protocol: HTTPS
      port: 443
      tls:
        mode: Terminate
        certificateRefs:
          - name: prod-tls-cert
            namespace: infra
      allowedRoutes:
        namespaces:
          from: Selector
          selector:
            matchLabels:
              gateway-access: "true"
```

`allowedRoutes.namespaces` 控制哪些 namespace 的 Route 可以附着到这个 Gateway——这是权限隔离的关键配置。

```yaml
# 业务团队创建，在自己的 namespace 下
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: api-route
  namespace: production
spec:
  parentRefs:
    - name: prod-gateway
      namespace: infra
  hostnames:
    - "api.example.com"
  rules:
    - matches:
        - path:
            type: PathPrefix
            value: /api/v1
          headers:
            - name: X-API-Version
              value: "v1"
      backendRefs:
        - name: api-service-v1
          port: 8080
          weight: 100
```

### 路径匹配和 Header 匹配

```yaml
rules:
  - matches:
      - path:
          type: Exact
          value: /healthz
    backendRefs:
      - name: health-service
        port: 8080

  - matches:
      - path:
          type: RegularExpression
          value: /api/v[0-9]+/.*
    backendRefs:
      - name: api-service
        port: 8080
```

### 金丝雀发布（流量权重）

这是 Gateway API 最实用的功能之一，原生支持流量拆分：

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: canary-route
  namespace: production
spec:
  parentRefs:
    - name: prod-gateway
      namespace: infra
  hostnames:
    - "api.example.com"
  rules:
    - matches:
        - path:
            type: PathPrefix
            value: /api
      backendRefs:
        - name: api-service-stable
          port: 8080
          weight: 90
        - name: api-service-canary
          port: 8080
          weight: 10
```

90% 流量到 stable，10% 到 canary，这在 Ingress 里需要靠各家私有注解实现，Gateway API 原生支持。

结合 Header 可以做更精细的金丝雀：

```yaml
rules:
  # 带有特定 Header 的请求全量走 canary
  - matches:
      - headers:
          - name: X-Canary
            value: "true"
    backendRefs:
      - name: api-service-canary
        port: 8080
        weight: 100
  # 其余流量走 stable
  - matches:
      - path:
          type: PathPrefix
          value: /api
    backendRefs:
      - name: api-service-stable
        port: 8080
        weight: 100
```

### gRPC 路由

Gateway API 有专门的 GRPCRoute，无需用 Ingress 的各种 grpc 注解：

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: GRPCRoute
metadata:
  name: grpc-route
  namespace: production
spec:
  parentRefs:
    - name: prod-gateway
      namespace: infra
  rules:
    - matches:
        - method:
            service: order.OrderService
            method: CreateOrder
      backendRefs:
        - name: order-grpc-service
          port: 9090
```

## 支持 Gateway API 的主流实现

| 实现 | 特点 | 适用场景 |
|-----|------|---------|
| Envoy Gateway | 官方参考实现，功能完整 | 通用，推荐新项目 |
| Cilium | 与 Cilium CNI 深度集成 | 已用 Cilium CNI 的集群 |
| Traefik v3 | 轻量，易操作 | 中小规模 |
| Kong | 企业级功能（限流/认证） | 需要 API 网关功能 |
| Istio | 与服务网格集成 | 已用 Istio 的场景 |

我们目前在生产用 Envoy Gateway，API 兼容性最好，社区活跃。

## 从 Ingress 迁移

### 迁移步骤

1. **安装 Gateway API CRD**（注意选版本）

```bash
kubectl apply -f https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.2.0/standard-install.yaml
```

2. **安装 Gateway API 实现**（以 Envoy Gateway 为例）

```bash
helm install eg oci://docker.io/envoyproxy/gateway-helm \
  --version v1.3.0 \
  -n envoy-gateway-system \
  --create-namespace
```

3. **逐条迁移 Ingress 规则，先不删老的**

原来的 Ingress：
```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: api-ingress
  annotations:
    nginx.ingress.kubernetes.io/rewrite-target: /
    nginx.ingress.kubernetes.io/proxy-body-size: "10m"
spec:
  rules:
    - host: api.example.com
      http:
        paths:
          - path: /api
            pathType: Prefix
            backend:
              service:
                name: api-service
                port:
                  number: 8080
```

对应的 HTTPRoute（注意注解对应的功能要通过 Gateway 的 filter 或者实现特定的扩展来配置）：

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: api-route
  namespace: production
spec:
  parentRefs:
    - name: prod-gateway
      namespace: infra
  hostnames:
    - "api.example.com"
  rules:
    - matches:
        - path:
            type: PathPrefix
            value: /api
      filters:
        - type: URLRewrite
          urlRewrite:
            path:
              type: ReplacePrefixMatch
              replacePrefixMatch: /
      backendRefs:
        - name: api-service
          port: 8080
```

4. **用 DNS 做切流**：把域名先解析到新的 Gateway LB，观察一段时间，确认没问题后删老的 Ingress。

## 踩坑

**CRD 版本问题。** Gateway API 有 `standard` 和 `experimental` 两个 channel，stable 功能在 standard 里，TCPRoute、GRPCRoute 等部分功能还在 experimental。安装 CRD 时要确认版本。

**实现差异。** Gateway API 定义了核心规范，但各家实现对扩展功能的支持不一样。比如超时设置，不同实现用不同方式配置（有的通过 filter，有的通过实现特定的 Policy CRD）。迁移前先查目标实现的文档。

**与老 Ingress 共存。** 两套系统可以同时运行，只是会有两个 LB。如果集群规模大，要注意 LB 的成本。通常的做法是迁移一个服务就删一个 Ingress，分批次推进。

**allowedRoutes 配置容易遗漏。** 如果 HTTPRoute 创建后没有生效，大概率是 Gateway 的 `allowedRoutes` 没有包含 Route 所在的 namespace。检查 Gateway 状态和 Route 状态，有明确的 status condition 可以看。

Gateway API 在 2025 年已经 GA，核心资源（Gateway、HTTPRoute）都升到了 v1。社区的态度很明确：Ingress 不会废弃，但 Gateway API 才是未来。对于新项目，完全值得直接上 Gateway API，省去将来迁移的麻烦。
