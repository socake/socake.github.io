---
title: "从 Ingress 迁移到 Gateway API：完整实操指南"
date: 2026-04-12T10:00:00+08:00
draft: false
tags: ["Kubernetes", "Gateway API", "Ingress", "网络", "迁移"]
categories: ["Kubernetes"]
description: "系统讲解 Kubernetes Gateway API 的设计理念、核心资源模型，以及从 Ingress-NGINX 完整迁移到 Gateway API 的实操步骤，包含 YAML 对比、工具使用和常见问题排查。"
summary: "Gateway API 是 Kubernetes 官方下一代流量入口标准，解决了 Ingress 注解泛滥、跨实现不可移植等历史遗留问题。本文带你从零完成生产迁移。"
toc: true
math: false
diagram: false
keywords: ["Gateway API", "Ingress 迁移", "HTTPRoute", "GatewayClass", "Kubernetes 网络"]
params:
  reading_time: true
---

## 为什么要抛弃 Ingress

Ingress 在 2015 年作为 Kubernetes 的七层路由抽象引入，核心设计极其简单：一个 `rules` 列表加一个 `backend`。这种简单性在早期是优势，到了生产规模下却变成了负债。

### 注解泛滥，不可移植

任何稍微复杂一点的需求都要靠 annotation 实现，而不同实现的 annotation 完全不兼容：

```yaml
# NGINX Ingress 的金丝雀配置
nginx.ingress.kubernetes.io/canary: "true"
nginx.ingress.kubernetes.io/canary-weight: "20"

# Traefik 的同等功能
traefik.ingress.kubernetes.io/router.middlewares: default-my-canary@kubernetescrd
```

从 NGINX Ingress 换到 Traefik，每一条 annotation 都要重写，没有任何标准可言。一个大规模集群动辄有几十个不同的 annotation，运维的心智负担极高。

### 角色边界模糊

Ingress 把基础设施层（使用哪个 LoadBalancer）、平台层（TLS 证书管理）和应用层（路由规则）全部混在同一个资源里。开发者提了一个 Ingress PR，运维才发现他顺手改了全局的 TLS 配置。

### 无法跨 Namespace 共享

一个 Ingress 只能引用同一 Namespace 的 Service。要做跨命名空间路由，要么用 ExternalName Service 绕，要么每个 Namespace 都部署一套 Ingress Controller，资源浪费且难以统一管理。

### 功能天花板低

Ingress spec 只支持 HTTP/HTTPS，没有 TCP/UDP 路由、没有 gRPC 支持、没有流量镜像、没有请求改写的标准字段。所有这些都只能靠注解，带来的是无法预测的跨实现行为。

---

## Gateway API 的设计哲学

Gateway API 不是 Ingress 的升级版，而是从零开始重新设计的流量管理 API。核心理念是**角色导向设计**（Role-Oriented Design）。

### 三层资源模型

```
GatewayClass  ──→  由基础设施管理员管理（运维团队/平台团队）
     │
     ↓
  Gateway     ──→  由平台管理员管理（各BU的平台工程师）
     │
     ↓
 HTTPRoute    ──→  由应用开发者管理（业务团队自助）
```

**GatewayClass**：声明"我们集群里有哪种 Gateway 实现可用"，类似 StorageClass。由集群管理员创建，开发者只读。

**Gateway**：声明"我要起一个监听特定端口/协议的入口"，绑定到某个 GatewayClass。可以跨 Namespace 被 HTTPRoute 引用（通过 allowedRoutes 控制权限）。

**HTTPRoute/TCPRoute/GRPCRoute**：声明"我的服务怎么接流量"，由开发者在自己的 Namespace 里创建，绑定到对应的 Gateway。

这种分层让权限控制变得自然：开发者只能改自己的 Route，改不了 Gateway 和 GatewayClass。

---

## 安装 Gateway API CRDs

Gateway API 的 CRD 独立于 Kubernetes 版本，分两个通道：

- **Standard Channel**：稳定功能，HTTPRoute/GatewayClass/Gateway 等核心资源
- **Experimental Channel**：实验功能，TCPRoute/UDPRoute/TLSRoute/BackendLBPolicy 等

```bash
# 安装标准通道（生产推荐）
kubectl apply -f https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.2.1/standard-install.yaml

# 安装实验通道（需要 TCPRoute/UDPRoute 时）
kubectl apply -f https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.2.1/experimental-install.yaml

# 验证 CRD 安装
kubectl get crd | grep gateway.networking.k8s.io
```

输出应包含：
```
gatewayclasses.gateway.networking.k8s.io
gateways.gateway.networking.k8s.io
httproutes.gateway.networking.k8s.io
grpcroutes.gateway.networking.k8s.io
referencegrants.gateway.networking.k8s.io
```

---

## 选择 Gateway API 实现

几个主流实现的对比：

| 实现 | 适用场景 | 特点 |
|------|---------|------|
| **Cilium Gateway API** | 已用 Cilium CNI 的集群 | eBPF 加速，无额外组件 |
| **Envoy Gateway** | 需要强大流量治理 | CNCF 项目，xDS 协议，可观测性强 |
| **NGINX Gateway Fabric** | 从 NGINX Ingress 迁移 | 官方出品，配置习惯接近 |
| **Kong Gateway** | 需要 API 管理功能 | 插件生态丰富 |
| **Istio** | 已有 Service Mesh | 与 Istio 深度集成 |

以 **Envoy Gateway** 为例安装：

```bash
helm install eg oci://docker.io/envoyproxy/gateway-helm \
  --version v1.2.1 \
  -n envoy-gateway-system \
  --create-namespace

# 等待就绪
kubectl wait --timeout=5m -n envoy-gateway-system \
  deployment/envoy-gateway --for=condition=Available
```

安装后会自动创建 GatewayClass：

```bash
kubectl get gatewayclass
# NAME            CONTROLLER                        ACCEPTED
# eg              gateway.envoyproxy.io/gatewayclass True
```

---

## Ingress vs Gateway API YAML 对比

### 场景：基础 HTTP 路由

**Ingress 写法：**

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: my-app
  namespace: production
  annotations:
    nginx.ingress.kubernetes.io/rewrite-target: /
spec:
  ingressClassName: nginx
  rules:
  - host: app.example.com
    http:
      paths:
      - path: /api
        pathType: Prefix
        backend:
          service:
            name: api-service
            port:
              number: 8080
      - path: /
        pathType: Prefix
        backend:
          service:
            name: frontend-service
            port:
              number: 3000
  tls:
  - hosts:
    - app.example.com
    secretName: app-tls
```

**Gateway API 写法：**

```yaml
# 平台管理员创建 Gateway（通常在 infra namespace）
apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
metadata:
  name: prod-gateway
  namespace: infra
spec:
  gatewayClassName: eg
  listeners:
  - name: https
    port: 443
    protocol: HTTPS
    hostname: "*.example.com"
    tls:
      mode: Terminate
      certificateRefs:
      - kind: Secret
        name: wildcard-tls
        namespace: infra
    allowedRoutes:
      namespaces:
        from: All   # 允许所有 namespace 的 HTTPRoute 绑定
---
# 开发者在自己的 namespace 创建 HTTPRoute
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: my-app
  namespace: production
spec:
  parentRefs:
  - name: prod-gateway
    namespace: infra
    sectionName: https
  hostnames:
  - "app.example.com"
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
  - matches:
    - path:
        type: PathPrefix
        value: /
    backendRefs:
    - name: frontend-service
      port: 3000
```

---

## 使用 ingress2gateway 工具自动转换

`ingress2gateway` 是官方提供的迁移辅助工具，支持将现有 Ingress 资源转换为 Gateway API 资源。

```bash
# 安装
go install sigs.k8s.io/ingress2gateway@latest

# 或直接下载二进制
curl -L https://github.com/kubernetes-sigs/ingress2gateway/releases/download/v0.3.0/ingress2gateway_linux_amd64.tar.gz | tar xz
sudo mv ingress2gateway /usr/local/bin/

# 转换当前集群所有 Ingress（dry-run 输出到 stdout）
ingress2gateway print

# 只转换指定 namespace
ingress2gateway print -n production

# 指定 ingress class（针对 NGINX）
ingress2gateway print --providers=ingress-nginx

# 输出到文件
ingress2gateway print -n production > gateway-resources.yaml
```

转换后需要人工检查几个点：
1. 注解里的自定义功能是否有对应的 Gateway API 标准字段
2. TLS 证书 Secret 是否在正确的 Namespace 或需要 ReferenceGrant
3. `pathType: Exact` 和 `pathType: ImplementationSpecific` 的语义转换

---

## HTTPRoute 高级功能详解

### 1. Header 匹配与路由

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: header-routing
  namespace: production
spec:
  parentRefs:
  - name: prod-gateway
    namespace: infra
  hostnames:
  - "api.example.com"
  rules:
  # 按 Header 路由到 v2 版本
  - matches:
    - headers:
      - name: "X-API-Version"
        value: "v2"
    backendRefs:
    - name: api-v2-service
      port: 8080
  # 按 Header 前缀匹配
  - matches:
    - headers:
      - name: "User-Agent"
        type: RegularExpression
        value: ".*Mobile.*"
    backendRefs:
    - name: mobile-api-service
      port: 8080
  # 默认路由
  - backendRefs:
    - name: api-service
      port: 8080
```

### 2. 金丝雀发布（流量权重）

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: canary-deployment
  namespace: production
spec:
  parentRefs:
  - name: prod-gateway
    namespace: infra
  hostnames:
  - "app.example.com"
  rules:
  - backendRefs:
    - name: app-stable
      port: 8080
      weight: 90    # 90% 流量到稳定版
    - name: app-canary
      port: 8080
      weight: 10    # 10% 流量到金丝雀版
```

金丝雀发布全流程：

```bash
# 部署新版本
kubectl set image deployment/app-canary app=myapp:v2 -n production

# 观察错误率（用 Prometheus 查）
kubectl exec -n monitoring prometheus-0 -- \
  promtool query instant 'rate(http_requests_total{status=~"5.."}[5m])'

# 逐步调整权重（编辑 HTTPRoute）
kubectl edit httproute canary-deployment -n production
# 改为 weight: 50 / 50，再观察，再改为 0 / 100

# 最终切流完成后删除旧版本
kubectl delete deployment app-stable -n production
```

### 3. URL Rewrite 与 Redirect

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: url-rewrite-demo
  namespace: production
spec:
  parentRefs:
  - name: prod-gateway
    namespace: infra
  rules:
  # HTTP 强制跳转 HTTPS
  - matches:
    - path:
        type: PathPrefix
        value: /
    filters:
    - type: RequestRedirect
      requestRedirect:
        scheme: https
        statusCode: 301
  # 路径重写：/v1/users → /users
  - matches:
    - path:
        type: PathPrefix
        value: /v1
    filters:
    - type: URLRewrite
      urlRewrite:
        path:
          type: ReplacePrefixMatch
          replacePrefixMatch: /
    backendRefs:
    - name: users-service
      port: 8080
  # 添加请求 Header
  - matches:
    - path:
        type: PathPrefix
        value: /api
    filters:
    - type: RequestHeaderModifier
      requestHeaderModifier:
        add:
        - name: X-Forwarded-Prefix
          value: /api
        set:
        - name: X-Real-IP
          value: "{{ .RemoteAddr }}"
    backendRefs:
    - name: api-service
      port: 8080
```

### 4. 请求镜像（流量镜像）

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: traffic-mirror
  namespace: production
spec:
  parentRefs:
  - name: prod-gateway
    namespace: infra
  rules:
  - backendRefs:
    - name: prod-service
      port: 8080
    filters:
    - type: RequestMirror
      requestMirror:
        backendRef:
          name: shadow-service   # 镜像流量发到这里，不影响响应
          port: 8080
```

流量镜像常用于：新版本上线前的影子测试、日志/审计副本收集、压测基准对比。

---

## 跨 Namespace 路由（ReferenceGrant）

Gateway API 默认禁止跨 Namespace 引用资源（出于安全考虑）。如果 HTTPRoute 在 `production` Namespace，需要引用 `infra` Namespace 的 Gateway，或者引用其他 Namespace 的 Service，必须创建 `ReferenceGrant`。

```yaml
# 场景1：允许 production namespace 的 HTTPRoute 绑定 infra namespace 的 Gateway
# 这个资源要创建在被引用的 namespace，即 infra
apiVersion: gateway.networking.k8s.io/v1beta1
kind: ReferenceGrant
metadata:
  name: allow-production-routes
  namespace: infra          # 被引用资源所在 namespace
spec:
  from:
  - group: gateway.networking.k8s.io
    kind: HTTPRoute
    namespace: production   # 允许这个 namespace 来引用
  to:
  - group: gateway.networking.k8s.io
    kind: Gateway
    name: prod-gateway      # 具体到某个 Gateway，也可以不指定 name 允许所有
```

```yaml
# 场景2：HTTPRoute 跨 namespace 引用 backend Service
# 在 database namespace 创建，允许 production 的 HTTPRoute 引用该 ns 的 Service
apiVersion: gateway.networking.k8s.io/v1beta1
kind: ReferenceGrant
metadata:
  name: allow-cross-ns-backend
  namespace: database
spec:
  from:
  - group: gateway.networking.k8s.io
    kind: HTTPRoute
    namespace: production
  to:
  - group: ""
    kind: Service
```

```yaml
# HTTPRoute 侧的引用方式
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: cross-ns-route
  namespace: production
spec:
  parentRefs:
  - name: prod-gateway
    namespace: infra        # 跨 namespace 引用 Gateway
  rules:
  - backendRefs:
    - name: db-proxy-service
      namespace: database   # 跨 namespace 引用 Service
      port: 5432
```

---

## 迁移步骤：逐个 Ingress 资源迁移

### 第一步：盘点现有 Ingress

```bash
# 列出所有 Ingress 及其 annotations
kubectl get ingress -A -o json | jq -r '
  .items[] | 
  "\(.metadata.namespace)/\(.metadata.name): " + 
  (.metadata.annotations | keys | join(", "))
'

# 统计使用了哪些 annotation
kubectl get ingress -A -o json | jq -r '
  .items[].metadata.annotations | keys[]
' | sort | uniq -c | sort -rn
```

### 第二步：分类处理

将 Ingress 按复杂度分三类：

- **简单**：只有基础路由，无特殊 annotation → 直接用 ingress2gateway 转换
- **中等**：有 rewrite/redirect/CORS 等标准功能 → ingress2gateway 转换后手动补全
- **复杂**：有自定义认证、限流、WAF 等 → 需要用 Gateway API 扩展资源（各实现不同）

### 第三步：并行运行验证

```bash
# 1. 保留原 Ingress 不动
# 2. 创建新的 Gateway + HTTPRoute
kubectl apply -f gateway-resources.yaml

# 3. 修改 /etc/hosts 或内部 DNS 做局部测试
echo "1.2.3.4 app.example.com" >> /etc/hosts

# 4. 使用 curl 验证路由规则
curl -v https://app.example.com/api/health
curl -H "X-API-Version: v2" https://app.example.com/api/users

# 5. 检查 HTTPRoute 状态
kubectl get httproute -n production -o yaml | grep -A 10 "status:"
```

### 第四步：切流并观察

```bash
# 更新 DNS，将流量切到新的 Gateway LB IP
GATEWAY_IP=$(kubectl get gateway prod-gateway -n infra \
  -o jsonpath='{.status.addresses[0].value}')
echo "New Gateway IP: $GATEWAY_IP"

# 更新 DNS A 记录（操作你的 DNS 提供商）
# 观察 5-15 分钟错误率

# 如有问题，DNS 切回旧 Ingress IP
```

### 第五步：清理 Ingress

```bash
# 确认迁移完毕后删除
kubectl delete ingress my-app -n production

# 如果所有 Ingress 都迁移完毕，卸载 NGINX Ingress Controller
helm uninstall ingress-nginx -n ingress-nginx
```

---

## GRPCRoute：gRPC 服务路由

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
  hostnames:
  - "grpc.example.com"
  rules:
  - matches:
    - method:
        service: com.example.UserService
        method: GetUser
    backendRefs:
    - name: user-grpc-service
      port: 50051
  - matches:
    - method:
        service: com.example.OrderService
    backendRefs:
    - name: order-grpc-service
      port: 50051
```

---

## 常见迁移问题排查

### HTTPRoute 无法绑定 Gateway

```bash
kubectl describe httproute my-app -n production
```

看 `Status.Parents` 字段：

```yaml
status:
  parents:
  - conditions:
    - message: 'Not accepted: Gateway infra/prod-gateway does not allow Routes from namespace production'
      reason: NotAllowedByListeners
      status: "False"
      type: Accepted
```

**解决**：检查 Gateway 的 `spec.listeners[].allowedRoutes.namespaces`，或创建对应的 ReferenceGrant。

### TLS 证书 Secret 跨 Namespace 引用失败

```bash
kubectl describe gateway prod-gateway -n infra
# 看到: secret "wildcard-tls" not found in namespace "infra"
```

**解决**：把 Secret 复制到 Gateway 所在 Namespace，或用 external-secrets 同步：

```bash
kubectl get secret wildcard-tls -n production -o yaml | \
  sed 's/namespace: production/namespace: infra/' | \
  kubectl apply -f -
```

### 路由规则不生效，流量走了默认后端

检查 HTTPRoute 的 `hostnames` 是否与 Gateway listener 的 `hostname` 匹配：

```bash
# Gateway listener 配置的是 *.example.com
# HTTPRoute 配置的是 app.example.com → 匹配
# HTTPRoute 配置的是 app.other.com → 不匹配，流量不会走这个 Route
```

### 检查 Gateway 实现的日志

```bash
# Envoy Gateway
kubectl logs -n envoy-gateway-system \
  deployment/envoy-gateway --tail=100 -f

# 查看生成的 Envoy 配置（xDS）
kubectl get configmap -n envoy-gateway-system -l gateway.envoyproxy.io/owning-gateway-name=prod-gateway
```

---

## 迁移后的运维收益

完成迁移后，你会发现：

1. **YAML 可读性大幅提升**：路由逻辑全在 spec 里，不再靠 annotation 猜功能
2. **权限模型清晰**：用 Kubernetes RBAC 控制谁能改 Gateway，谁能改 HTTPRoute，不需要额外的准入控制
3. **多实现可迁移**：今天用 Envoy Gateway，明天换 Cilium，HTTPRoute 不用改
4. **功能边界明确**：标准 spec 里没有的功能，用各实现提供的 Policy Attachment 扩展，两者清晰分离

Gateway API 已在 K8s 1.28 中将 HTTPRoute/Gateway/GatewayClass 升级为 GA，这是官方对其稳定性的背书。Ingress 虽然不会立刻废弃，但新功能不会再往里加——现在开始迁移是正确时机。
