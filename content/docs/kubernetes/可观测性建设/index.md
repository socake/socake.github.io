---
title: "Prometheus + Grafana + Loki 可观测性体系建设"
date: 2025-12-08T15:00:00+08:00
draft: false
tags: ["Prometheus", "Grafana", "Loki", "可观测性", "监控"]
categories: ["Kubernetes"]
description: "从零搭建云原生可观测性体系：Prometheus 指标采集、Grafana 可视化、Loki 日志聚合，覆盖多集群场景"
summary: "记录在多套 K8s 集群上建立统一可观测性平台的实践经验，包含 Prometheus 采集配置、告警规则设计、Grafana Dashboard 组织方式，以及跨集群日志聚合的 Loki 部署方案。"
toc: true
math: false
diagram: false
keywords: ["prometheus", "grafana", "loki", "监控", "可观测性", "kubernetes"]
params:
  reading_time: true
---

## 可观测性三支柱

可观测性（Observability）不等于监控。监控是预先知道你要关注什么，可观测性是系统出了问题你能通过外部输出推断内部状态。云原生体系下，可观测性通常分三个维度：

**Metrics（指标）**

时序数据，适合回答"是什么"和"有多严重"。Prometheus 是事实标准，记录的是聚合后的数值，比如 QPS、延迟百分位、错误率、CPU/内存使用率。指标的优势是存储小、查询快，适合告警和 Dashboard 展示。局限是丢失了单次请求的上下文。

**Logs（日志）**

结构化或非结构化的事件记录，适合回答"发生了什么"。日志保留了请求级别的详细上下文，是排查具体问题的首选。代价是存储量大，需要有效的采集、传输、索引方案。Loki 的设计思路是只对 label 建索引，日志内容不做全文索引，以此换来极低的存储成本。

**Traces（链路追踪）**

分布式调用链，适合回答"慢在哪里"。一次请求经过多个微服务，Trace 把每一跳的耗时、状态串联成一条完整的调用链。Tempo 是 Grafana Labs 推出的 Trace 后端，与 Loki/Prometheus 共享相同的标签体系，三者在 Grafana 里可以互相跳转。

三者互补：告警触发 → 看 Dashboard（Metrics）定位服务 → 看日志（Logs）找具体错误 → 看链路（Traces）定位慢点。

---

## 整体架构

```
                          ┌─────────────────────────────────────┐
                          │           Grafana (统一入口)          │
                          │   Dashboard / Explore / Alerting      │
                          └──────┬────────────┬──────────────────┘
                                 │            │
              ┌──────────────────▼──┐    ┌────▼─────────────────┐
              │    Prometheus        │    │        Loki           │
              │  (时序指标存储)       │    │  (日志聚合存储)        │
              └──────┬──────────────┘    └────────┬─────────────┘
                     │                            │
         ┌───────────▼──────────┐     ┌───────────▼──────────────┐
         │  ServiceMonitor /     │     │   Promtail / Alloy        │
         │  PodMonitor (拉取)    │     │   (日志采集 Agent)         │
         └───────────┬──────────┘     └───────────┬──────────────┘
                     │                            │
         ┌───────────▼────────────────────────────▼──────────────┐
         │                   K8s 集群                              │
         │   Pods / Nodes / Services / Ingress                     │
         └───────────────────────────────────────────────────────┘

         告警链路：Prometheus → AlertManager → Webhook → 钉钉/PagerDuty

         链路追踪（可选）：
         应用 SDK → OpenTelemetry Collector → Tempo → Grafana Explore
```

多集群场景下，各集群部署独立的 Prometheus + Promtail，Grafana 通过 Data Source 聚合多个 Prometheus 和 Loki 实例，或者使用 Thanos/Cortex 做跨集群指标联邦。

---

## Prometheus 部署与配置

### kube-prometheus-stack 安装

推荐用 Helm Chart `kube-prometheus-stack`，一键安装 Prometheus、AlertManager、Grafana、kube-state-metrics、node-exporter 全家桶。

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update

helm upgrade --install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  --namespace monitoring \
  --create-namespace \
  --values values.yaml \
  --version 55.5.0
```

关键的 `values.yaml` 配置项：

```yaml
prometheus:
  prometheusSpec:
    # 数据保留时长，建议配合远程存储使用
    retention: 15d
    retentionSize: 50GB
    # 资源限制，生产环境按实际负载调整
    resources:
      requests:
        memory: 2Gi
        cpu: 500m
      limits:
        memory: 8Gi
        cpu: 2000m
    # 存储
    storageSpec:
      volumeClaimTemplate:
        spec:
          storageClassName: gp3
          accessModes: ["ReadWriteOnce"]
          resources:
            requests:
              storage: 100Gi
    # 跨 namespace 发现 ServiceMonitor
    serviceMonitorSelectorNilUsesHelmValues: false
    podMonitorSelectorNilUsesHelmValues: false
    ruleSelectorNilUsesHelmValues: false

alertmanager:
  alertmanagerSpec:
    storage:
      volumeClaimTemplate:
        spec:
          storageClassName: gp3
          resources:
            requests:
              storage: 10Gi

grafana:
  adminPassword: "your-password"
  persistence:
    enabled: true
    size: 10Gi
  # 默认 Dashboard 导入
  defaultDashboardsEnabled: true
```

### ServiceMonitor 自定义采集

`ServiceMonitor` 是 kube-prometheus-stack 引入的 CRD，用于声明式配置 Prometheus 的抓取目标，不需要直接修改 Prometheus 配置文件。

```yaml
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: my-app-metrics
  namespace: production
  labels:
    # 这个 label 需要与 Prometheus 的 serviceMonitorSelector 匹配
    release: kube-prometheus-stack
spec:
  # 选择 Service 的 namespace
  namespaceSelector:
    matchNames:
      - production
      - staging
  # 选择哪些 Service
  selector:
    matchLabels:
      app.kubernetes.io/name: my-app
  endpoints:
    - port: metrics          # Service 中的 port name
      path: /metrics
      interval: 30s
      scrapeTimeout: 10s
      # 如果 metrics 路径需要认证
      # basicAuth:
      #   username:
      #     name: my-secret
      #     key: username
```

`PodMonitor` 直接选 Pod，适合没有对应 Service 的场景：

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PodMonitor
metadata:
  name: my-batch-job
  namespace: production
  labels:
    release: kube-prometheus-stack
spec:
  namespaceSelector:
    matchNames:
      - production
  selector:
    matchLabels:
      app: batch-processor
  podMetricsEndpoints:
    - port: metrics
      path: /metrics
      interval: 60s
```

### Recording Rules 预聚合

高基数指标直接查询很慢，Recording Rules 提前聚合计算结果存为新的时序，大幅降低 Dashboard 查询延迟。

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: recording-rules
  namespace: monitoring
  labels:
    release: kube-prometheus-stack
spec:
  groups:
    - name: http_request_rates
      interval: 30s
      rules:
        # 预聚合每个服务的 5 分钟请求成功率
        - record: job:http_requests_success_rate:5m
          expr: |
            sum by (job, namespace) (
              rate(http_requests_total{status=~"2.."}[5m])
            ) / sum by (job, namespace) (
              rate(http_requests_total[5m])
            )
        # 预聚合 P99 延迟（按 namespace 汇总）
        - record: namespace:http_request_duration_p99:5m
          expr: |
            histogram_quantile(0.99,
              sum by (namespace, le) (
                rate(http_request_duration_seconds_bucket[5m])
              )
            )
    - name: node_resources
      interval: 60s
      rules:
        # 节点 CPU 使用率
        - record: node:cpu_utilization:avg5m
          expr: |
            1 - avg by (node) (
              rate(node_cpu_seconds_total{mode="idle"}[5m])
            )
```

### AlertManager 配置

AlertManager 负责告警的分组、去重、静默、路由和通知。配置结构：`route`（路由树）→ `receiver`（通知渠道）。

```yaml
# alertmanager-config.yaml
global:
  resolve_timeout: 5m
  # 钉钉 Webhook URL（通过 Secret 注入更安全）
  # http_config 可以设置全局代理

route:
  group_by: ['alertname', 'cluster', 'namespace']
  group_wait: 30s        # 同组告警等待时间（允许更多告警聚合）
  group_interval: 5m     # 同组已发送后，新告警等待时间
  repeat_interval: 4h    # 持续告警重复通知间隔
  receiver: 'default'
  routes:
    # P0 告警立即通知，不等待分组
    - matchers:
        - severity="critical"
      receiver: 'oncall-pagerduty'
      group_wait: 0s
      repeat_interval: 30m
    # 节点相关告警路由到基础设施组
    - matchers:
        - alertname=~"Node.*"
      receiver: 'infra-dingtalk'
    # 业务告警路由到业务组
    - matchers:
        - team="backend"
      receiver: 'backend-dingtalk'

receivers:
  - name: 'default'
    webhook_configs:
      - url: 'http://dingtalk-webhook:8060/dingtalk/default/send'
        send_resolved: true

  - name: 'oncall-pagerduty'
    pagerduty_configs:
      - routing_key: '<integration-key>'
        description: '{{ range .Alerts }}{{ .Annotations.summary }}{{ end }}'

  - name: 'infra-dingtalk'
    webhook_configs:
      - url: 'http://dingtalk-webhook:8060/dingtalk/infra/send'
        send_resolved: true
        http_config:
          # 可配置 Bearer Token 鉴权

  - name: 'backend-dingtalk'
    webhook_configs:
      - url: 'http://dingtalk-webhook:8060/dingtalk/backend/send'
        send_resolved: true

inhibit_rules:
  # 节点 Down 时，抑制该节点上所有 Pod 级别的告警
  - source_matchers:
      - alertname="NodeDown"
    target_matchers:
      - alertname=~"Pod.*"
    equal: ['node']
```

钉钉 Webhook 推荐使用 `timonwong/prometheus-webhook-dingtalk`，支持自定义消息模板：

```yaml
# prometheus-webhook-dingtalk 配置示例
targets:
  default:
    url: "https://oapi.dingtalk.com/robot/send?access_token=xxx"
    secret: "your-sign-secret"
    # 消息模板（Markdown）
    message:
      title: '{{ template "ding.link.title" . }}'
      text: '{{ template "ding.link.content" . }}'
```

---

## 告警规则设计原则

### SLI / SLO 与告警的关系

告警不应该监控系统内部实现，而应该监控用户可感知的体验。SLI（Service Level Indicator）是衡量服务质量的具体指标，SLO（Service Level Objective）是对应的目标值。

典型 SLI：
- **可用性**：过去 5 分钟成功请求比例
- **延迟**：P99 请求延迟 < 500ms
- **吞吐量**：每秒处理请求数
- **错误率**：5xx 响应占比 < 0.1%

基于 SLO 的错误预算告警比直接告警更有意义：

```yaml
# 错误预算消耗速率告警（Burn Rate Alert）
# 以 30 天 99.9% 可用性为例，错误预算 = 43.2 分钟
- alert: HighErrorBudgetBurnRate
  expr: |
    (
      job:http_requests_success_rate:5m < 0.99
    ) and (
      job:http_requests_success_rate:1h < 0.999
    )
  for: 2m
  labels:
    severity: critical
  annotations:
    summary: "服务 {{ $labels.job }} 错误预算消耗过快"
    description: "5min 成功率 {{ $value | humanizePercentage }}，持续消耗错误预算"
```

### 常用告警规则

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: kubernetes-alerts
  namespace: monitoring
  labels:
    release: kube-prometheus-stack
spec:
  groups:
    - name: pod.rules
      rules:
        # Pod 持续重启（CrashLoopBackOff）
        - alert: PodCrashLooping
          expr: |
            increase(kube_pod_container_status_restarts_total[1h]) > 5
          for: 5m
          labels:
            severity: warning
          annotations:
            summary: "Pod {{ $labels.namespace }}/{{ $labels.pod }} 频繁重启"
            description: "过去 1h 重启 {{ $value }} 次，请检查容器日志"

        # Pod 长时间 Pending
        - alert: PodStuckPending
          expr: |
            kube_pod_status_phase{phase="Pending"} == 1
          for: 15m
          labels:
            severity: warning
          annotations:
            summary: "Pod {{ $labels.namespace }}/{{ $labels.pod }} 长时间 Pending"

        # OOMKilled
        - alert: ContainerOOMKilled
          expr: |
            kube_pod_container_status_last_terminated_reason{reason="OOMKilled"} == 1
          for: 0m
          labels:
            severity: warning
          annotations:
            summary: "容器 {{ $labels.container }} 发生 OOMKilled"

    - name: node.rules
      rules:
        # 节点内存使用率过高
        - alert: NodeMemoryHigh
          expr: |
            (
              node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes
            ) / node_memory_MemTotal_bytes > 0.90
          for: 10m
          labels:
            severity: warning
          annotations:
            summary: "节点 {{ $labels.instance }} 内存使用率超过 90%"
            description: "当前使用率 {{ $value | humanizePercentage }}"

        # 节点磁盘使用率过高
        - alert: NodeDiskHigh
          expr: |
            (
              node_filesystem_size_bytes{fstype!~"tmpfs|overlay"}
              - node_filesystem_avail_bytes{fstype!~"tmpfs|overlay"}
            ) / node_filesystem_size_bytes{fstype!~"tmpfs|overlay"} > 0.85
          for: 5m
          labels:
            severity: warning
          annotations:
            summary: "节点 {{ $labels.instance }} 磁盘 {{ $labels.mountpoint }} 使用率超过 85%"

        # 节点不可达
        - alert: NodeDown
          expr: up{job="node-exporter"} == 0
          for: 5m
          labels:
            severity: critical
          annotations:
            summary: "节点 {{ $labels.instance }} 不可达"

    - name: http.rules
      rules:
        # 接口错误率过高
        - alert: HTTPErrorRateHigh
          expr: |
            sum by (job, namespace) (
              rate(http_requests_total{status=~"5.."}[5m])
            ) / sum by (job, namespace) (
              rate(http_requests_total[5m])
            ) > 0.05
          for: 5m
          labels:
            severity: critical
          annotations:
            summary: "服务 {{ $labels.job }} 5xx 错误率超过 5%"

        # P99 延迟过高
        - alert: HTTPLatencyHigh
          expr: |
            histogram_quantile(0.99,
              sum by (job, le) (
                rate(http_request_duration_seconds_bucket[5m])
              )
            ) > 2
          for: 5m
          labels:
            severity: warning
          annotations:
            summary: "服务 {{ $labels.job }} P99 延迟超过 2s"
```

### 告警噪音治理

告警太多等于没告警，oncall 工程师会开始忽略所有通知。减少噪音的几个原则：

**1. `for` 持续时间要合理**：瞬时抖动不应该触发告警，`for: 5m` 意味着指标持续异常 5 分钟才通知。

**2. 善用 Inhibit Rules（抑制规则）**：父级问题（节点 Down）触发时，自动抑制子级告警（Pod 异常），避免几十条重复通知。

**3. 分级处理**：`critical` 立即电话/钉钉，`warning` 发工作群，`info` 只写日志不推送。

**4. 定期审查告警触发历史**：频繁触发但没人处理的告警，要么提高阈值，要么排查根因修掉。

**5. Silence（临时静默）**：维护窗口期在 AlertManager UI 创建 Silence，避免计划内变更触发告警风暴。

---

## Grafana 实践

### Dashboard 分层管理

按层次组织 Dashboard，从宏观到微观：

```
Cluster Overview        → 集群层：节点数、整体资源水位、告警汇总
  └── Namespace View    → 命名空间层：各 namespace 资源用量、Pod 状态
        └── Service View → 服务层：QPS/延迟/错误率（RED 方法）
              └── Pod View → Pod 层：单 Pod CPU/内存/重启/日志入口
```

Dashboard 用 JSON 文件管理，存放在 Git 仓库，通过 ConfigMap 挂载到 Grafana（Grafana 支持 sidecar 自动加载 ConfigMap）：

```yaml
grafana:
  sidecar:
    dashboards:
      enabled: true
      searchNamespace: ALL
      label: grafana_dashboard
      labelValue: "1"
```

对应的 ConfigMap：

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: my-app-dashboard
  namespace: monitoring
  labels:
    grafana_dashboard: "1"
data:
  my-app.json: |
    { ... dashboard JSON ... }
```

### 变量模板

Dashboard 变量让同一个面板可以切换查询维度，避免为每个集群/命名空间单独创建 Dashboard。

常用变量配置（在 Dashboard Settings → Variables 中配置）：

| 变量名 | 类型 | Query |
|--------|------|-------|
| `cluster` | Query | `label_values(kube_node_info, cluster)` |
| `namespace` | Query | `label_values(kube_pod_info{cluster="$cluster"}, namespace)` |
| `pod` | Query | `label_values(kube_pod_info{cluster="$cluster",namespace="$namespace"}, pod)` |
| `interval` | Interval | `1m,5m,15m,1h` |

面板中使用变量：`rate(http_requests_total{cluster="$cluster",namespace="$namespace"}[$interval])`

### 常用 PromQL 速查

```promql
# Pod CPU 使用率（按 pod 分组）
sum by (pod, namespace) (
  rate(container_cpu_usage_seconds_total{container!=""}[5m])
)

# Pod 内存使用（RSS，不含 cache）
sum by (pod, namespace) (
  container_memory_rss{container!=""}
)

# 节点 CPU 使用率
1 - avg by (instance) (
  rate(node_cpu_seconds_total{mode="idle"}[5m])
)

# 节点内存使用率
(node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes)
/ node_memory_MemTotal_bytes

# HTTP 请求成功率（过去 5 分钟）
sum(rate(http_requests_total{status=~"2.."}[5m]))
/ sum(rate(http_requests_total[5m]))

# P50 / P95 / P99 延迟
histogram_quantile(0.99,
  sum by (le, job) (
    rate(http_request_duration_seconds_bucket[5m])
  )
)

# 过去 1 小时 Pod 重启次数
increase(kube_pod_container_status_restarts_total[1h])

# 集群各 namespace 资源请求量
sum by (namespace) (
  kube_pod_container_resource_requests{resource="cpu"}
)

# 节点磁盘剩余空间
node_filesystem_avail_bytes{fstype!~"tmpfs|overlay"}
/ node_filesystem_size_bytes{fstype!~"tmpfs|overlay"}
```

---

## Loki 日志聚合

### 架构选择：单体 vs 微服务

**单体模式（Monolithic）**：所有组件在同一个进程内，适合日志量 < 100GB/天的场景。部署简单，运维成本低，用一个 Helm release 搞定。

**微服务模式（Microservices）**：各组件（Distributor、Ingester、Querier、Query Frontend、Compactor 等）独立部署，水平扩展。适合日志量大、对查询性能要求高的生产环境。

**简单可扩展模式（Simple Scalable）**：介于两者之间，将组件分为 `read` 和 `write` 两组，兼顾扩展性和运维简单性。这是官方推荐的生产起步方案。

```bash
helm repo add grafana https://grafana.github.io/helm-charts
helm repo update

helm upgrade --install loki grafana/loki \
  --namespace monitoring \
  --values loki-values.yaml
```

核心配置 `loki-values.yaml`：

```yaml
loki:
  auth_enabled: false   # 单租户场景关闭认证
  commonConfig:
    replication_factor: 1  # 测试环境，生产建议 3
  storage:
    type: s3
    s3:
      endpoint: s3.us-west-2.amazonaws.com
      region: us-west-2
      bucketnames: my-loki-chunks
      access_key_id: ${AWS_ACCESS_KEY_ID}
      secret_access_key: ${AWS_SECRET_ACCESS_KEY}
  schemaConfig:
    configs:
      - from: 2024-01-01
        store: tsdb
        object_store: s3
        schema: v13
        index:
          prefix: loki_index_
          period: 24h
  limits_config:
    # 限制单个租户的写入速率
    ingestion_rate_mb: 16
    ingestion_burst_size_mb: 32
    # 限制单次查询返回的日志量
    max_entries_limit_per_query: 5000
    # 日志保留时间
    retention_period: 30d

# 简单可扩展模式
deploymentMode: SimpleScalable

backend:
  replicas: 3
read:
  replicas: 3
write:
  replicas: 3
```

### Promtail / Grafana Alloy 配置

Promtail 是 Loki 的官方日志采集 Agent，以 DaemonSet 形式部署在每个节点上，读取 `/var/log/pods/` 下的容器日志。

```yaml
# promtail-values.yaml
config:
  clients:
    - url: http://loki-gateway/loki/api/v1/push

  scrape_configs:
    - job_name: kubernetes-pods
      kubernetes_sd_configs:
        - role: pod
      pipeline_stages:
        # 解析容器运行时日志格式（CRI-O / containerd）
        - cri: {}
        # 提取 JSON 字段作为 label（慎用，高基数 label 影响性能）
        - json:
            expressions:
              level: level
        # 根据 level 设置 label
        - labels:
            level:
        # 过滤掉 DEBUG 日志（减少写入量）
        - match:
            selector: '{level="debug"}'
            action: drop
      relabel_configs:
        # 保留 namespace / pod / container label
        - source_labels: [__meta_kubernetes_namespace]
          target_label: namespace
        - source_labels: [__meta_kubernetes_pod_name]
          target_label: pod
        - source_labels: [__meta_kubernetes_container_name]
          target_label: container
        - source_labels: [__meta_kubernetes_pod_label_app_kubernetes_io_name]
          target_label: app
        # 过滤掉不需要采集的 namespace
        - source_labels: [namespace]
          regex: kube-system|cert-manager
          action: drop
```

Grafana Alloy 是新一代的采集 Agent，兼容 Promtail 同时支持 Metrics/Logs/Traces 统一采集，配置语言为 River（HCL 风格），是未来的推荐方向。

### LogQL 常用语法

LogQL 是 Loki 的查询语言，语法上参考了 PromQL。

**日志流选择器（Stream Selector）**：

```logql
# 选择特定 namespace 和 app 的日志
{namespace="production", app="my-service"}

# 支持正则
{namespace=~"prod.*", container!="sidecar"}
```

**过滤器（Filter）**：

```logql
# 包含关键词
{namespace="production"} |= "ERROR"

# 正则匹配
{namespace="production"} |~ "timeout|connection refused"

# 排除
{namespace="production"} != "healthcheck"

# 解析 JSON 日志，然后过滤字段
{namespace="production"} | json | level="error" | status_code >= 500
```

**聚合统计**：

```logql
# 每分钟错误日志数量（类比 PromQL 的 rate）
sum by (pod) (
  rate({namespace="production"} |= "ERROR" [1m])
)

# 统计各服务的日志量（排查日志爆炸来源）
sum by (app) (
  bytes_rate({namespace="production"}[5m])
)

# 解析结构化日志，统计各接口 P99 延迟
quantile_over_time(0.99,
  {namespace="production", app="api-gateway"}
  | json
  | unwrap duration_ms [5m]
) by (path)
```

**常用排查场景**：

```logql
# 查询最近 1 小时某 Pod 的所有错误
{namespace="production", pod="my-app-xxx"} |= "ERROR" | line_format "{{.message}}"

# 统计 HTTP 500 错误的路径分布
{namespace="production", app="api"}
| json
| status_code >= 500
| line_format "{{.path}}"

# 关联追踪 ID，找某次请求的完整链路日志
{namespace="production"} |= "trace_id=abc123"
```

### 多集群统一查询方案

多集群场景下，有几种方案：

**方案一：各集群独立 Loki + Grafana 多 Data Source**

最简单，Grafana 添加多个 Loki Data Source，Explore 页面手动切换。缺点是无法跨集群聚合查询。

**方案二：中心化 Loki，各集群 Promtail 推送**

各集群的 Promtail 直接推送日志到中心 Loki（需要网络互通）。打上 `cluster` label 区分来源，LogQL 可以跨集群查询：

```logql
# 查所有集群的错误
{app="my-service"} |= "ERROR"

# 只查 prod 集群
{cluster="prod", app="my-service"} |= "ERROR"
```

Promtail 配置推送到远端：
```yaml
clients:
  - url: http://central-loki.ops.svc/loki/api/v1/push
    external_labels:
      cluster: us-qa          # 打上集群标识
      environment: qa
```

**方案三：Grafana Enterprise / Loki Federation**

企业级方案，支持多个 Loki 实例联邦查询，成本较高。中小规模团队方案二已经够用。

---

## 踩坑记录

### 高基数问题（Cardinality Explosion）

**现象**：Prometheus 内存持续上涨，最终 OOM。

**根因**：某个 label 的取值数量过多（比如把 `user_id`、`request_id` 作为 label），导致时序数量爆炸。Prometheus 是内存型数据库，每个时序都要在内存维护状态。

**排查方法**：

```promql
# 找出基数最高的 metric
topk(10, count by (__name__)({__name__=~".+"}))

# 查看某个 metric 的时序数
count(http_requests_total)
```

**解决办法**：
- 把高基数值移到日志里，不放进 metric label
- 用 `metric_relabel_configs` 在采集时删除高基数 label
- 配置 `per_series_memory` 限制，超出时拒绝写入

```yaml
# 在 ServiceMonitor 中删除不必要的 label
metricRelabelings:
  - sourceLabels: [request_id]
    action: labeldrop
    regex: request_id
```

### Loki 写入量过大 OOM

**现象**：Loki Ingester Pod 频繁 OOMKilled，日志写入延迟飙升。

**根因**：某个应用日志突然爆炸（循环打印大量 DEBUG 日志），导致写入速率超过 Ingester 处理能力，内存积压。

**解决办法**：
1. 在 Promtail pipeline 中 drop DEBUG 级别日志
2. 配置 Loki `limits_config.ingestion_rate_mb` 限流，超出时返回 429 让 Promtail 重试而非内存积压
3. 排查应用，修复日志爆炸的根因
4. Ingester 内存 limit 调大，给足缓冲时间让告警触发再处理

### 告警 Resolved 消息不发送

**现象**：告警触发有通知，但恢复后没有 "Resolved" 通知，导致告警状态不清晰。

**根因**：`send_resolved: false`（Webhook receiver 默认值），或者 AlertManager 配置了 `repeat_interval` 但没有正确处理 Resolved 状态。

**解决办法**：

```yaml
receivers:
  - name: 'dingtalk'
    webhook_configs:
      - url: '...'
        send_resolved: true   # 必须显式设置为 true
```

另一个坑：AlertManager 的 `resolve_timeout` 默认 5 分钟，意味着告警消失后要等 5 分钟才发 Resolved。如果 Prometheus 的 `scrape_interval` 较长，可以适当调短 `resolve_timeout`。

---

## 参考链接

- [Prometheus 官方文档](https://prometheus.io/docs/)
- [Grafana Loki 文档](https://grafana.com/docs/loki/latest/)
- [kube-prometheus-stack Helm Chart](https://github.com/prometheus-community/helm-charts/tree/main/charts/kube-prometheus-stack)
- [Google SRE Book - SLO 章节](https://sre.google/sre-book/service-level-objectives/)
- [Loki LogQL 语法参考](https://grafana.com/docs/loki/latest/query/)
- [AlertManager 配置文档](https://prometheus.io/docs/alerting/latest/configuration/)
- [Grafana Alloy 文档](https://grafana.com/docs/alloy/latest/)
