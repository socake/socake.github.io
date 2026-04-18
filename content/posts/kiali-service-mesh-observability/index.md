---
title: "Kiali 服务网格可观测性实战：从拓扑图到告警联动"
date: 2025-08-12T10:00:00+08:00
draft: false
tags: ["Kiali", "Istio", "Service Mesh", "可观测性"]
categories: ["可观测性"]
description: "Kiali 2.x 在 Istio 生产环境的落地笔记：架构组成、与 Prometheus/Tempo/Grafana 的集成、流量图使用技巧、Validations 的价值、告警策略、以及两次线上流量异常的定位复盘。"
summary: "Kiali 不只是画拓扑图的工具，它是服务网格的诊断中心。本文把 Kiali 2.x 在生产中的配置、用法、踩坑都写清楚。"
toc: true
math: false
diagram: false
keywords: ["Kiali", "Istio", "Service Mesh", "流量拓扑", "Validations"]
params:
  reading_time: true
---

## 为什么要郑重其事地写一篇 Kiali

装过 Istio 的团队很多都把 Kiali 当成「Istio 套件里那个画拓扑图的 Web UI」。实际上，Kiali 2.x 是我在 Istio 生产环境里花时间最多的一个工具。它承担四件事：

1. **流量拓扑可视化**：按 namespace、workload、service 实时画调用关系；
2. **配置校验 Validations**：检测 VirtualService / DestinationRule / PeerAuthentication 的错配；
3. **流量指标面板**：把 Prometheus 的 istio_requests_total 等指标做成业务可读的图；
4. **trace / log 联动**：点一个 service 跳到 Grafana Tempo 的 trace 或 Loki 的 log。

这四件事 Grafana Dashboard 也能做，但 Kiali 的优势在于「针对 Istio 语义做了深度集成」。它知道什么是 VirtualService、什么是 Sidecar 资源、什么是 mTLS migration，所以出的图更直接，Validations 更专业。

这篇文章讲清楚：Kiali 在你生产 Istio 集群里该怎么部署、用、调优，以及碰到问题怎么借它定位。

## 一、Kiali 的组件和数据来源

Kiali 本身是无状态的 Web/API 服务器，不存储任何数据。它的图和告警都从外部数据源拉：

```
                      ┌─────────────────┐
                      │  Grafana Tempo  │◀── traces
                      └────────┬────────┘
                               │
┌──────────┐           ┌───────┴────────┐        ┌─────────┐
│ Istio CR │◀──────────│     Kiali       │───────▶│ Grafana │
│ (k8s API)│           │  (Deployment)   │        └─────────┘
└──────────┘           └───────┬────────┘
                               │
                      ┌────────┴────────┐
                      │   Prometheus    │◀── istio_requests_total
                      │      /Mimir     │
                      └─────────────────┘
```

数据源职责：

- **Prometheus / Mimir**：画拓扑图所需的所有 edge、error rate、latency 都从 `istio_requests_total`、`istio_request_duration_seconds_bucket` 这些指标里计算；
- **Kubernetes API**：拉 Istio CR（VirtualService、DestinationRule、Sidecar、Gateway 等），做 Validations；
- **Jaeger / Tempo**：trace 跳转；
- **Grafana**：dashboard 跳转；
- **Istiod**：通过 xDS debug API 查 Envoy 配置状态。

**Kiali 本身不存数据**，所以重启 Kiali 不会丢任何东西。你要做的是保证上面这些数据源正常。

## 二、一次部署，把所有集成配对

Helm 安装示例：

```bash
helm repo add kiali https://kiali.org/helm-charts
helm install \
  --namespace istio-system \
  --set auth.strategy=openshift \
  --set deployment.instance_name=kiali-prod \
  --set external_services.prometheus.url=http://mimir-gateway.mimir.svc:9009/prometheus \
  --set external_services.grafana.in_cluster_url=http://grafana.monitoring.svc:3000 \
  --set external_services.grafana.url=https://grafana.example.com \
  --set external_services.tracing.provider=tempo \
  --set external_services.tracing.in_cluster_url=http://tempo-query-frontend.tempo.svc:3200 \
  --set external_services.tracing.url=https://grafana.example.com \
  --set external_services.tracing.use_grpc=true \
  kiali-server kiali/kiali-server
```

核心配置节：

```yaml
auth:
  strategy: openid    # 生产强推 OIDC，不要用 token 方式暴露给业务
  openid:
    client_id: kiali-prod
    issuer_uri: https://sso.example.com/realms/prod
    username_claim: preferred_username
    disable_rbac: false
    authorization_endpoint: https://sso.example.com/realms/prod/protocol/openid-connect/auth

external_services:
  prometheus:
    url: http://mimir-gateway.mimir.svc:9009/prometheus
    auth:
      type: basic
      username: kiali
      password_secret: mimir-kiali-basic
    query_scope:
      cluster: prod-ap-southeast-1
    cache_enabled: true
    cache_duration: 10
    cache_expiration: 300
  grafana:
    enabled: true
    in_cluster_url: http://grafana.monitoring.svc:3000
    url: https://grafana.example.com
    dashboards:
      - name: "Istio Service Dashboard"
      - name: "Istio Workload Dashboard"
  tracing:
    enabled: true
    provider: tempo
    use_grpc: true
    in_cluster_url: grpc://tempo-query-frontend.tempo.svc:9095
    url: https://grafana.example.com
    tempo_config:
      org_id: prod
      datasource_uid: tempo_ds
      url_format: grafana
  istio:
    component_status:
      enabled: true
    config_map_name: istio
    istiod_deployment_name: istiod
```

**几个坑**：

1. **`query_scope.cluster` 不设置时多集群会混图**。我们有 3 个 Istio 集群共享一个 Mimir，不设 query_scope 的话 Kiali 会把三个集群的指标合到一张图里，看起来 service 数是真实的 3 倍。
2. **`auth.strategy` 默认 `token`**：这个 token 是挂载 Service Account 的，给任何人 UI 访问权限就等于给了集群 admin。一定改成 `openid` 或 `header`。
3. **`tracing.use_grpc` 推荐开**：Tempo 的 HTTP API 在大规模 trace 查询时慢。
4. **`cache_enabled` 一定开**：Kiali 默认每次刷新拓扑图都会重新查 Prometheus 几十次，开 cache 后相同查询走本地 10s cache，对 Prometheus 压力大幅下降。

## 三、流量拓扑图：真正的生产主菜

### 三种 graph type

1. **Workload graph**：以 Deployment/StatefulSet 为节点（比如 `order-api-v1`、`order-api-v2` 分开画）；
2. **Service graph**：以 Kubernetes Service 为节点（`order-api` 一个节点）；
3. **App graph**：按 `app` label 聚合（`order` 一个节点）；
4. **Versioned app graph**：`app` + `version` 聚合，适合看 canary。

生产主要看：Service graph 用于概览，Versioned app graph 用于灰度发布。

### 图的刷新频率和时间范围

Kiali 流量图基于 Prometheus `rate()` 查询计算，默认窗口 1 分钟。你选的时间范围越大，边的权重越平滑但越失真。经验值：

- 排查实时问题：窗口 1m，刷新 10s；
- 看稳态拓扑：窗口 5m，刷新 30s；
- 看历史流量：窗口 30m+，不需要刷新。

### 边的语义

每条边有三种指标选择：

- **Request rate (req/s)**：最直观，默认；
- **Response time (p95/p99)**：用颜色编码 latency；
- **TCP bytes**：非 HTTP 流量。

你可以叠加显示，边的颜色反映错误率（绿色 0%，黄色 1%~10%，红色 >10%）。

### 节点上的 icon

边上和节点上会有多个图标，常见的含义：

- 🔒 **锁**：mTLS 加密流量；
- 🔄 **circuit breaker**：DestinationRule 里配了 outlierDetection；
- ⚠️ **警告三角**：Validations 发现问题；
- 🚪 **vs**：挂载了 VirtualService；
- ⚡ **fault injection**：正在做故障注入。

## 四、Validations：Istio 配置的 linter

Kiali 内置的 Validations 是我认为最被低估的功能。它会定期检查所有 Istio CR，发现错误和警告。常见问题类型：

1. **KIA1101**：VirtualService 里引用了不存在的 subset；
2. **KIA0202**：DestinationRule 没有匹配的 host；
3. **KIA0301**：PeerAuthentication 冲突；
4. **KIA0501**：Gateway 没被任何 VirtualService 使用；
5. **KIA1104**：VirtualService 的 http match 冲突；
6. **KIA0601**：ServiceEntry 的 host 和 DNS 冲突；
7. **KIA1203**：Sidecar 资源的 egress host 找不到；
8. **KIA0104**：workload 没有 sidecar injection。

每个 Validation 都有详细说明和修复建议。在 Kiali UI 的 **Istio Config** 页面能看到全集群 Validation 概览。

我们的做法：把 Validations 也接入告警。Kiali 提供 API：

```bash
curl -s https://kiali.example.com/api/istio/validations?cluster=prod \
  | jq '.objectTypeValidation[] | .objectValidations | .[] | .[] | select(.valid==false)'
```

写个 CronJob 每 10 分钟查一次，任何 severity>=warning 的发钉钉。上线半年抓到过 40+ 次配置错误，最严重的一次是 VirtualService match 冲突导致部分请求路由到已下线 subset。

## 五、trace / log 联动

### Trace 跳转

点 service 节点 → Traces Tab → 看到 Tempo 里过去 10 分钟的 trace 列表。Kiali 自动按 service 过滤。点任一 trace 跳到 Grafana Tempo 面板。

注意：Tempo 集成需要在 datasource 里配 `datasourceUid`，这样 Kiali 生成的链接能直接打开 Grafana 的 trace 详情页而不是 Tempo 原始 UI。

### Log 跳转

Kiali 2.x 默认没有 log 按钮，但可以配置 `external_services.grafana.dashboards` 加上自定义 dashboard URL 模板：

```yaml
external_services:
  grafana:
    dashboards:
      - name: "Loki Logs"
        variables:
          namespace: "var-namespace"
          app: "var-app"
```

这样在 service 详情页能一键跳到「按当前 service 过滤的日志 dashboard」。

### Metric Tab

每个 service 详情页有 Inbound Metrics 和 Outbound Metrics，展示 RED 指标。这些指标来自 Prometheus，面板定义写死在 Kiali 代码里，所以不需要额外配置。

## 六、案例一：灰度发布时流量分布异常

时间：2025 年 6 月。现象：业务给 `order-api-v2` 灰度 10%，但 Kiali 上显示 v2 实际只收到 2% 流量。

**排查**：

1. 打开 Versioned app graph，过滤 order-api；
2. 边上标签清晰显示：v1 收 98%，v2 收 2%；
3. 进 Istio Config 看 VirtualService，weight 配的是 v1:90 / v2:10；
4. 换 Workload graph 看，发现进入 order-api 的上游有两个 ingress：istio-gateway（走 VirtualService）和一个 legacy NodePort（绕过 mesh）；
5. NodePort 直连 v1 的 ClusterIP，不经过 VirtualService，所以 9:1 的比例只在 gateway 流量里生效；
6. 总流量里 gateway 占 20%，NodePort 占 80%，最终 v2 的整体占比就是 20% * 10% = 2%。

如果只看 Prometheus `istio_requests_total` 指标，很难发现 NodePort 的旁路流量，因为它根本没进 mesh。Kiali 的拓扑图把「未知来源」标记为 `unknown` 节点，这才让我们看到问题。

修复方案：把 NodePort 废弃，全量走 Gateway。

## 七、案例二：mTLS 迁移导致部分请求失败

时间：2025 年 9 月。现象：某业务在开启 STRICT mTLS 后 5% 请求 503。

**排查**：

1. Kiali 拓扑图里有个节点的锁图标是虚线（表示部分 mTLS）；
2. Validations 面板出现 KIA0301：PeerAuthentication 和 DestinationRule 的 TLS 模式冲突；
3. 详细信息：namespace 级 PeerAuthentication STRICT，但某个 DestinationRule 显式写了 `tls.mode: DISABLE`，导致客户端以明文发、服务端以 STRICT 拒收；
4. 删掉 DR 的 DISABLE 之后恢复。

这个问题没有 Kiali 的话要靠 `istioctl authn tls-check` 一台台 pod 查，极慢。

## 八、Kiali 的监控和告警

Kiali 自己应该被监控：

```
kiali_info{version}                                  # 版本
kiali_graph_generation_duration_seconds_bucket       # 图生成耗时
kiali_api_failures_total                             # API 失败
kiali_kubernetes_client_failures_total               # k8s API 失败
kiali_prometheus_client_failures_total               # Prometheus 失败
```

几个告警例子：

```yaml
- alert: KialiDown
  expr: absent(up{job="kiali"}) or up{job="kiali"} == 0
  for: 5m

- alert: KialiGraphGenerationSlow
  expr: histogram_quantile(0.95, rate(kiali_graph_generation_duration_seconds_bucket[5m])) > 5
  for: 10m
  annotations:
    summary: Kiali 流量图生成 p95 >5s，Prometheus 可能慢了

- alert: KialiPrometheusFailing
  expr: rate(kiali_prometheus_client_failures_total[5m]) > 0.1
  for: 5m
```

## 九、性能调优

大集群里（1000+ workload）Kiali 默认配置会慢。优化点：

1. **开 cache**，cache_duration 和 cache_expiration 拉到 10s / 300s；
2. **namespace 过滤**：默认 Kiali 扫描所有 namespace，可以在 `deployment.accessible_namespaces` 里只保留业务相关的；
3. **Prometheus 压力**：让 Kiali 连的 Prometheus/Mimir 不是主力查询源，专门开一个副本或子集群；
4. **graph 限制**：`graph.time_range` 默认 10m，集群大的话改 1m/3m；
5. **replica count**：Kiali 默认 1 副本，生产至少 2；
6. **istioAPIEnabled**：false 可以关掉 xDS 检查功能，减少对 istiod 的压力。

## 十、多集群 / 多 mesh 的 Kiali

Kiali 2.x 支持 multi-cluster：一个 Kiali 实例连多个 Istio 集群。前提是这些集群共享一个 mesh ID 或至少共享 Prometheus。

配置：

```yaml
deployment:
  cluster_name: prod-east
external_services:
  prometheus:
    url: http://mimir-gateway.mimir.svc:9009/prometheus
    query_scope:
      cluster: prod-east
kiali_feature_flags:
  clustering:
    clusters:
      - name: prod-east
        network: prod-east
      - name: prod-west
        network: prod-west
```

生产经验：如果两个集群的 mesh 真的有互通（通过 east-west gateway），一个 Kiali 足够；如果只是同一个 SRE 团队维护独立 mesh，建议每个集群一个 Kiali 实例，各自查本地 Prometheus，减少故障面。

## 十一、Kiali 没解决的问题（用别的工具补）

Kiali 擅长 mesh 内观测，下面这些问题它做不了：

1. **非 mesh 流量**：pod 没 sidecar 的情况，用 Cilium Hubble 补；
2. **L4 协议**：TCP / Redis / MySQL 的内容级观测，用 eBPF tools；
3. **长期容量规划**：Kiali 的图只是实时流量，做长期分析要去 Grafana；
4. **根因排障**：Kiali 告诉你「哪里有问题」，不告诉你「为什么」，需要配合 trace 和 log；
5. **告警**：Kiali 不是告警工具，告警走 Prometheus Rule。

## 十二、权限和 RBAC

生产一定要开 OIDC + RBAC。Kiali 支持通过 OIDC 里的 group claim 映射到 Kubernetes RBAC：

```yaml
auth:
  openid:
    authorization_endpoint: https://sso.example.com/.../auth
    username_claim: preferred_username
    scopes: [openid, groups]
    api_proxy:
      api_proxy_ca_data: ""
```

然后在 K8s 里建 Role + RoleBinding：

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: kiali-viewer-team-a
subjects:
  - kind: Group
    name: "team-a"
    apiGroup: rbac.authorization.k8s.io
roleRef:
  kind: ClusterRole
  name: kiali-viewer
  apiGroup: rbac.authorization.k8s.io
```

Kiali 通过 `SelfSubjectAccessReview` 检查当前登录用户的 K8s 权限，决定 UI 上能看见哪些 namespace。

## 十三、一份上线 checklist

1. Istio 版本 >= 1.22；
2. Prometheus / Mimir 已接入，`istio_requests_total` 能查；
3. Tempo 已接入 Grafana，trace 能跳；
4. Grafana Dashboard UID 稳定；
5. Kiali 安装，replica=2，cache 开启；
6. OIDC + RBAC 配置；
7. query_scope 和 cluster_name 正确；
8. Validations 告警 CronJob 上线；
9. Kiali 自身监控和告警；
10. 业务团队培训：怎么看拓扑图、怎么查流量异常。

## 十四、小结

在 Istio 生产环境里，Kiali 不是可选项。没有它你要么自己用 PromQL 手写拓扑图，要么 SSH 进 pod 里翻 Envoy config，效率低得离谱。Kiali 的价值在于「专为 Istio 语义做过深度集成」，它懂 VirtualService 的 weight、懂 Sidecar 的 egress、懂 PeerAuthentication 的冲突——这是任何通用监控工具替代不了的。

## 参考资料

- Kiali 官方文档 2.x 部署和配置指南
- Istio 文档 Observability 章节
- Kiali GitHub Validations 代码库
- Kiali Blog：Validations 详解系列
