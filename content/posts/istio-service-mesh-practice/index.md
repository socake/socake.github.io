---
title: "Istio Service Mesh 落地实战：从 Sidecar 注入到灰度发布"
date: 2025-06-06T12:06:00+08:00
draft: false
tags: ["Istio", "Service Mesh", "Kubernetes", "灰度发布", "mTLS"]
categories: ["Kubernetes"]
description: "Istio 生产落地实战：流量切分灰度发布、DestinationRule 熔断、mTLS 配置与排障"
summary: "记录 Istio Service Mesh 从零落地的完整过程，包括 sidecar 注入原理、VirtualService 灰度发布流量切分、DestinationRule 熔断与负载均衡配置、PeerAuthentication mTLS 加固，以及用 istioctl analyze 排查常见问题。"
toc: true
math: false
diagram: false
series: ["K8s 完全指南"]
keywords: ["Istio", "Service Mesh", "VirtualService", "DestinationRule", "mTLS", "灰度发布", "熔断"]
params:
  reading_time: true
---

Service Mesh 讲了好几年，但能把 Istio 在生产里跑稳的团队其实不多。我自己也是从"装上就算完事"的状态走到"知道每个 CRD 在干什么"，这篇是这段过程的笔记。

## Sidecar 注入原理

Istio 的核心是给每个 Pod 注入一个 Envoy sidecar 代理，所有进出 Pod 的流量都经过这个代理。注入方式有两种：

**自动注入（推荐）：** 给 namespace 打上标签，Istio 的 MutatingWebhookConfiguration 会在 Pod 创建时自动注入 sidecar。

```bash
# 开启命名空间自动注入
kubectl label namespace my-app istio-injection=enabled

# 验证
kubectl get namespace my-app --show-labels
```

**手动注入：**

```bash
# 用 istioctl 手动注入，适合测试或特殊场景
istioctl kube-inject -f deployment.yaml | kubectl apply -f -
```

**排除特定 Pod 不注入：**

```yaml
# 在 Pod spec 的 annotations 中设置
metadata:
  annotations:
    sidecar.istio.io/inject: "false"
```

验证注入成功：

```bash
# 正常注入的 Pod 应该有 2 个容器（应用 + istio-proxy）
kubectl get pods -n my-app
# NAME                          READY   STATUS    RESTARTS   AGE
# my-service-7d9f8b6c5d-xk2p9   2/2     Running   0          5m

# 查看 sidecar 日志
kubectl logs my-service-7d9f8b6c5d-xk2p9 -c istio-proxy -n my-app
```

**资源开销评估：** 每个 Envoy sidecar 在空载时大约消耗 50-100m CPU、50-100Mi 内存。100 个 Pod 的集群，额外引入的资源成本约为 10 CPU core 和 10Gi 内存，选择是否引入 Istio 要把这个成本算进去。

## VirtualService 流量切分：灰度发布实战

灰度发布是 Istio 最典型的使用场景。假设我们要把 `my-service` 从 v1 升级到 v2，使用 10% → 50% → 100% 的分阶段方式。

**第一步：部署两个版本的 Deployment，打上不同的版本标签：**

```yaml
# deployment-v1.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: my-service-v1
  namespace: my-app
spec:
  replicas: 3
  selector:
    matchLabels:
      app: my-service
      version: v1
  template:
    metadata:
      labels:
        app: my-service
        version: v1
    spec:
      containers:
        - name: my-service
          image: registry.example.com/my-service:v1.0.0
```

```yaml
# deployment-v2.yaml（结构相同，替换 version 和 image）
apiVersion: apps/v1
kind: Deployment
metadata:
  name: my-service-v2
  namespace: my-app
spec:
  replicas: 1
  selector:
    matchLabels:
      app: my-service
      version: v2
  template:
    metadata:
      labels:
        app: my-service
        version: v2
    spec:
      containers:
        - name: my-service
          image: registry.example.com/my-service:v2.0.0
```

Service 用 `app: my-service` 选择两个版本的 Pod（不带 version 标签）：

```yaml
apiVersion: v1
kind: Service
metadata:
  name: my-service
  namespace: my-app
spec:
  selector:
    app: my-service
  ports:
    - port: 80
      targetPort: 8080
```

**第二步：定义 DestinationRule，声明 subset：**

```yaml
apiVersion: networking.istio.io/v1beta1
kind: DestinationRule
metadata:
  name: my-service
  namespace: my-app
spec:
  host: my-service
  subsets:
    - name: v1
      labels:
        version: v1
    - name: v2
      labels:
        version: v2
```

**第三步：VirtualService 控制流量比例（灰度 10%）：**

```yaml
apiVersion: networking.istio.io/v1beta1
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
          weight: 90
        - destination:
            host: my-service
            subset: v2
          weight: 10
```

```bash
kubectl apply -f virtualservice.yaml

# 验证流量分配
kubectl exec -n my-app deploy/test-client -- \
  bash -c 'for i in $(seq 1 20); do curl -s http://my-service/version; echo; done' | sort | uniq -c
```

**提升到 50%：**

```bash
# 直接 patch，不需要重新 apply 完整文件
kubectl patch virtualservice my-service -n my-app --type=json \
  -p='[
    {"op": "replace", "path": "/spec/http/0/route/0/weight", "value": 50},
    {"op": "replace", "path": "/spec/http/0/route/1/weight", "value": 50}
  ]'
```

**全量切到 v2（100%）：**

```yaml
spec:
  hosts:
    - my-service
  http:
    - route:
        - destination:
            host: my-service
            subset: v2
          weight: 100
```

全量验证无误后，删除 v1 Deployment 和旧的 subset 配置。

**基于 Header 的金丝雀路由（测试账号先体验新版本）：**

```yaml
spec:
  hosts:
    - my-service
  http:
    - match:
        - headers:
            x-canary:
              exact: "true"
      route:
        - destination:
            host: my-service
            subset: v2
    - route:
        - destination:
            host: my-service
            subset: v1
```

## DestinationRule：负载均衡与熔断

DestinationRule 不仅用于定义 subset，还控制连接池、负载均衡策略和熔断配置。

**负载均衡策略：**

```yaml
apiVersion: networking.istio.io/v1beta1
kind: DestinationRule
metadata:
  name: my-service
  namespace: my-app
spec:
  host: my-service
  trafficPolicy:
    loadBalancer:
      simple: LEAST_CONN  # 最少连接数，适合处理时间差异大的服务
      # 其他选项：ROUND_ROBIN（默认）、RANDOM、PASSTHROUGH
  subsets:
    - name: v1
      labels:
        version: v1
    - name: v2
      labels:
        version: v2
      trafficPolicy:
        loadBalancer:
          simple: ROUND_ROBIN  # subset 级别可以覆盖全局策略
```

**熔断配置（生产必备）：**

```yaml
spec:
  host: my-service
  trafficPolicy:
    connectionPool:
      tcp:
        maxConnections: 100        # 最大 TCP 连接数
      http:
        http1MaxPendingRequests: 50   # HTTP/1.1 最大排队请求数
        http2MaxRequests: 100         # HTTP/2 最大并发请求数
        maxRequestsPerConnection: 10  # 每个连接最多处理多少请求后关闭
    outlierDetection:
      consecutive5xxErrors: 5        # 连续 5 次 5xx 触发驱逐
      interval: 30s                  # 检测间隔
      baseEjectionTime: 30s          # 最短驱逐时间
      maxEjectionPercent: 50         # 最多驱逐 50% 的实例
      minHealthPercent: 50           # 健康实例低于 50% 时停止驱逐
```

这套熔断配置的效果：如果某个 Pod 连续返回 5 次 5xx，Istio 会把它从负载均衡池中暂时移除 30 秒，期间请求不会发往这个 Pod。

## PeerAuthentication：mTLS 加固

Istio 默认使用 `PERMISSIVE` 模式（既接受明文也接受 mTLS）。生产环境应该切换到 `STRICT` 模式，强制要求服务间通信必须使用 mTLS。

```yaml
# 全局开启 mTLS STRICT 模式（mesh 级别）
apiVersion: security.istio.io/v1beta1
kind: PeerAuthentication
metadata:
  name: default
  namespace: istio-system  # 放在 istio-system 生效范围是全 mesh
spec:
  mtls:
    mode: STRICT
```

**分步骤迁移（避免直接切换破坏现有流量）：**

```bash
# 第一步：先用 PERMISSIVE，确认 sidecar 全部注入
kubectl apply -f - <<EOF
apiVersion: security.istio.io/v1beta1
kind: PeerAuthentication
metadata:
  name: default
  namespace: my-app
spec:
  mtls:
    mode: PERMISSIVE
EOF

# 第二步：检查 mTLS 状态
istioctl x describe service my-service.my-app

# 第三步：确认后切换到 STRICT
kubectl patch peerauthentication default -n my-app --type=merge \
  -p='{"spec":{"mtls":{"mode":"STRICT"}}}'
```

**查看 mTLS 连接情况：**

```bash
# 查看 Envoy 的 mTLS 统计
kubectl exec deploy/my-service -n my-app -c istio-proxy -- \
  pilot-agent request GET stats | grep ssl
```

## AuthorizationPolicy：服务间访问控制

mTLS 确保了传输安全，AuthorizationPolicy 进一步控制哪些服务可以访问哪些服务：

```yaml
# 只允许 frontend 命名空间的服务访问 my-service
apiVersion: security.istio.io/v1beta1
kind: AuthorizationPolicy
metadata:
  name: my-service-policy
  namespace: my-app
spec:
  selector:
    matchLabels:
      app: my-service
  action: ALLOW
  rules:
    - from:
        - source:
            namespaces: ["frontend"]
            principals: ["cluster.local/ns/frontend/sa/frontend-service"]
      to:
        - operation:
            methods: ["GET", "POST"]
            paths: ["/api/*"]
```

## istioctl analyze 排障

`istioctl analyze` 是排查 Istio 配置问题的首选工具：

```bash
# 分析整个集群的配置问题
istioctl analyze --all-namespaces

# 分析特定命名空间
istioctl analyze -n my-app

# 分析本地配置文件（apply 前预检）
istioctl analyze ./my-virtualservice.yaml

# 输出示例
Warn [IST0108] (VirtualService my-service.my-app) 
  Referenced host not found: "my-service-v2"
Error [IST0101] (DestinationRule my-service.my-app) 
  Referenced selector not found for subset "v2"
```

**常见问题排查：**

```bash
# 查看某个服务的 Envoy 配置（路由、集群、监听器）
istioctl proxy-config routes deploy/my-service -n my-app
istioctl proxy-config clusters deploy/my-service -n my-app
istioctl proxy-config listeners deploy/my-service -n my-app

# 查看 Pilot 推送到 Envoy 的配置是否一致
istioctl proxy-status

# 检查某个 Pod 的连通性
istioctl x describe pod my-service-xxx -n my-app

# 开启 Envoy 访问日志（临时调试用）
kubectl exec deploy/my-service -n my-app -c istio-proxy -- \
  curl -s -X POST "http://localhost:15000/logging?level=debug"
```

**流量无法路由的典型排查步骤：**

```bash
# 1. 确认 VirtualService 的 hosts 和 Service 名称一致
kubectl get vs my-service -n my-app -o yaml | grep "hosts:"

# 2. 确认 DestinationRule 的 host 和 subset labels 存在
kubectl get dr my-service -n my-app -o yaml

# 3. 确认 Pod 有对应的 version label
kubectl get pods -n my-app --show-labels | grep version

# 4. 用 kiali 可视化流量拓扑（如果有安装）
istioctl dashboard kiali
```

## 踩坑记录

### 坑1：VirtualService 的 hosts 大小写敏感

`hosts` 字段必须与 Service 名称完全一致，包括大小写。K8s Service 名称全小写，但有时候配置 VirtualService 时会不小心写错。

### 坑2：跨命名空间引用需要带 FQDN

VirtualService 要路由到其他命名空间的服务时，必须用完整域名：

```yaml
# 错误（只在同一命名空间有效）
hosts:
  - my-service

# 正确（跨命名空间）
hosts:
  - my-service.other-namespace.svc.cluster.local
```

### 坑3：PeerAuthentication STRICT 导致健康检查失败

kubelet 的 liveness/readiness probe 是从节点发起的，没有 sidecar，因此在 STRICT mTLS 模式下会失败。需要给健康检查端口设置例外：

```yaml
spec:
  mtls:
    mode: STRICT
  portLevelMtls:
    8081:  # 健康检查端口
      mode: DISABLE
```

或者在 Deployment 中配置 Istio 排除健康检查端口：

```yaml
metadata:
  annotations:
    traffic.sidecar.istio.io/excludeInboundPorts: "8081"
```

### 坑4：istio-proxy 版本与控制平面不匹配

升级 Istio 控制平面后，存量 Pod 的 sidecar 版本没有更新，新旧版本混用可能导致问题：

```bash
# 查看各 Pod 的 sidecar 版本
istioctl proxy-status | awk '{print $7}' | sort | uniq -c

# 触发滚动重启，让 sidecar 重新注入新版本
kubectl rollout restart deployment -n my-app
```

## 总结

Istio 落地最大的难点不在技术本身，在渐进引入和团队认知对齐。我的节奏是：

1. 先只用它做可观测性（Kiali + Jaeger），不动任何流量规则
2. 熟悉了再引入 VirtualService 做灰度，一次只动一个服务
3. mTLS 最后开，分 namespace 逐步切
4. `istioctl analyze` 塞进 CI/CD，配置错误在合入前暴露

资源开销是真金白银的成本。团队小、服务间调用简单的情况，Istio 带来的运维负担可能大于收益——引入之前先算一遍这本账。
