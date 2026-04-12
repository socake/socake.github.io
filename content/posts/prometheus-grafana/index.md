---
title: "可观测性建设：从 Prometheus 采集到 Grafana 告警联动"
date: 2024-12-06T09:30:00+08:00
draft: false
tags: ["可观测性", "Prometheus", "Grafana", "SRE", "监控"]
categories: ["可观测性"]
description: "从可观测性三要素到 Prometheus 采集架构、PromQL 实用查询、Grafana Dashboard 设计与 Alertmanager 告警路由的完整实践"
summary: "可观测性不是装几个监控工具，而是让系统在出问题时能快速定位根因。这篇文章从采集架构到 PromQL 到告警路由，覆盖我们在生产环境中实际遇到的 cardinality 爆炸、告警噪音等问题。"
toc: true
math: false
diagram: false
series: ["可观测性实战"]
keywords: ["Prometheus", "Grafana", "Alertmanager", "PromQL", "可观测性", "ServiceMonitor", "DingTalk"]
params:
  reading_time: true
---

可观测性这个词现在很热，但在实际落地中，大多数团队做的其实只是「监控」——装了 Prometheus + Grafana，配了一堆 CPU/内存面板，告警规则抄了一份模板，然后发现告警每天轰炸几十条，没人认真看，真出问题了还是靠用户反馈。

这篇文章记录我们在建设可观测性体系过程中踩过的坑和总结的实践。

## 可观测性三要素的关系

Metrics、Logs、Traces 是三个不同维度的数据，回答不同的问题：

- **Metrics（指标）**：「系统现在怎么样？」——QPS、延迟、错误率、资源使用率。适合趋势分析和告警触发。
- **Logs（日志）**：「发生了什么事件？」——具体请求的参数、错误堆栈、业务事件。适合详细排查。
- **Traces（链路追踪）**：「一个请求经过了哪些服务，每一跳耗时多少？」——适合微服务场景定位性能瓶颈。

三者不是替代关系，而是互补的排查链路：告警触发（Metrics）→ 定位问题时间段和范围 → 查对应时间段的日志（Logs）→ 如果是跨服务调用问题再看 Trace。

很多团队的问题是只建了 Metrics，在「告警触发后」这一步就卡住了，因为没有配套的 Logs 和 Traces，每次排查都靠猜或者 ssh 进机器看。

我们目前的技术栈：Metrics 用 Prometheus + VictoriaMetrics（长期存储），Logs 用 Loki（多集群统一查询），Traces 用 Jaeger。这篇主要聚焦 Metrics 链路。

## Prometheus 采集架构

### Exporter 与 ServiceMonitor

Prometheus 生态里 Exporter 负责把各种系统/中间件的指标转换成 Prometheus 格式。常用的：

- `kube-state-metrics`：K8s 资源状态（Pod 数量、Deployment replicas、PVC 状态）
- `node-exporter`：宿主机指标（CPU、内存、磁盘、网络）
- `blackbox-exporter`：外部可用性探测（HTTP、TCP、ICMP）
- `mysql-exporter`、`redis-exporter`：中间件指标

在 K8s 环境里，推荐用 `kube-prometheus-stack` Helm chart 一次性部署 Prometheus Operator + 常用 Exporter + 预置 Grafana Dashboard，省去大量配置工作。

**ServiceMonitor** 是 Prometheus Operator 引入的 CRD，让应用的采集配置和应用本身一起管理，而不是集中写在 Prometheus 的 scrape config 里：

```yaml
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: goalfy-api
  namespace: goalfy
  labels:
    release: kube-prometheus-stack   # 必须匹配 Prometheus 的 serviceMonitorSelector
spec:
  selector:
    matchLabels:
      app: goalfy-api
  endpoints:
    - port: metrics        # Service 里暴露指标的端口名
      path: /metrics
      interval: 15s
      scrapeTimeout: 10s
  namespaceSelector:
    matchNames:
      - goalfy
```

对应的 Service 需要有 `port.name: metrics`：

```yaml
apiVersion: v1
kind: Service
metadata:
  name: goalfy-api
  labels:
    app: goalfy-api
spec:
  ports:
    - name: http
      port: 8080
    - name: metrics
      port: 9090    # 应用的 metrics 端口
```

应用侧（Go 示例）暴露 Prometheus metrics：

```go
import (
    "github.com/prometheus/client_golang/prometheus/promhttp"
    "net/http"
)

// 在独立端口暴露 metrics，避免和业务流量混用
go func() {
    http.Handle("/metrics", promhttp.Handler())
    http.ListenAndServe(":9090", nil)
}()
```

### Scrape Config 处理特殊场景

ServiceMonitor 覆盖不了的场景（比如采集集群外的服务），用 `additionalScrapeConfigs`：

```yaml
# values.yaml for kube-prometheus-stack
prometheus:
  prometheusSpec:
    additionalScrapeConfigs:
      - job_name: 'external-mysql'
        static_configs:
          - targets:
              - 'mysql-exporter.internal:9104'
        relabel_configs:
          - source_labels: [__address__]
            target_label: instance
            regex: '([^:]+).*'
            replacement: '$1'
```

`relabel_configs` 是 Prometheus 采集配置中最强大也最容易出问题的部分，用于在采集时动态修改 label。常用操作：

```yaml
relabel_configs:
  # 从 Pod annotation 读取采集路径
  - source_labels: [__meta_kubernetes_pod_annotation_prometheus_io_path]
    action: replace
    target_label: __metrics_path__
    regex: (.+)

  # 丢弃特定 namespace 的指标
  - source_labels: [__meta_kubernetes_namespace]
    action: drop
    regex: kube-system
```

## PromQL 实用查询

### rate 与 irate

`rate()` 计算时间窗口内的平均增长率，`irate()` 计算最后两个数据点的瞬时增长率。

```promql
# QPS：过去 5 分钟 HTTP 请求平均速率
rate(http_requests_total[5m])

# 按状态码分组的错误率
sum(rate(http_requests_total{status=~"5.."}[5m])) by (service)
/
sum(rate(http_requests_total[5m])) by (service)

# 用 irate 更灵敏地反映突刺（但噪音更大）
irate(http_requests_total[5m])
```

经验：告警规则用 `rate`，它对噪音更平滑；Debug 时用 `irate` 能更清晰地看到瞬间的流量突刺。

### histogram_quantile 计算延迟分位数

P99 延迟是比平均延迟更有意义的指标，因为平均值会掩盖尾部延迟：

```promql
# P99 请求延迟（需要应用暴露 histogram 类型的指标）
histogram_quantile(0.99,
  sum(rate(http_request_duration_seconds_bucket[5m])) by (le, service)
)

# P50、P95、P99 对比
histogram_quantile(0.50, sum(rate(http_request_duration_seconds_bucket[5m])) by (le))
histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket[5m])) by (le))
histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket[5m])) by (le))
```

注意：`histogram_quantile` 只能在有 `_bucket` 后缀的 histogram 指标上使用。Summary 类型的指标（有 `_quantile` label）不能用这个函数重新计算分位数。

### absent 检测指标消失

这是一个容易被忽视但很有用的函数——当某个指标消失时（比如服务挂掉不再上报），`absent()` 返回 1：

```promql
# 如果 goalfy-api 的指标超过 2 分钟没有数据，触发告警
absent(up{job="goalfy-api"}) == 1

# 或者直接检测 up 指标
up{job="goalfy-api"} == 0
```

### topk 找出资源占用最高的对象

```promql
# 内存占用最高的 5 个 Pod
topk(5,
  sum(container_memory_working_set_bytes{container!=""}) by (pod, namespace)
)

# CPU 使用率最高的 5 个 namespace
topk(5,
  sum(rate(container_cpu_usage_seconds_total{container!=""}[5m])) by (namespace)
)
```

## Grafana Dashboard 设计原则

一个好用的 Dashboard 应该让人在 30 秒内判断「系统是否健康」，而不是展示大量数字让人自己解读。

**从 RED 方法（或 USE 方法）组织 Panel**

RED（Rate、Errors、Duration）适合服务层面：
- Rate：当前 QPS 是多少
- Errors：错误率是否异常
- Duration：P95/P99 延迟是否在阈值内

USE（Utilization、Saturation、Errors）适合资源层面：
- Utilization：资源使用率（CPU 60%）
- Saturation：是否在排队（等待中的请求数）
- Errors：是否有错误

**用颜色传达状态而不只是展示数字**

在 Grafana 的 Stat Panel 和 Gauge 里配置阈值颜色：

```
绿色：正常范围
黄色：需要关注（比如 P99 > 500ms）
红色：需要立即处理（比如错误率 > 1%）
```

这样值班的人看 Dashboard 时，绿色就是「没事」，出现红色就是「有问题」，不需要逐个 Panel 读数字。

**时间对齐与变量**

Dashboard 里加入变量让它可复用：

```
变量 $namespace：让同一个 Dashboard 适用于不同 namespace
变量 $service：聚焦到某个具体服务
变量 $interval：调整图表时间粒度（1m、5m、1h）
```

PromQL 里使用变量：

```promql
rate(http_requests_total{namespace="$namespace", service="$service"}[$interval])
```

## Alertmanager 告警路由与通知

### 告警规则设计

告警规则写在 `PrometheusRule` CRD 里：

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: goalfy-api-alerts
  namespace: goalfy
  labels:
    release: kube-prometheus-stack
spec:
  groups:
    - name: goalfy-api
      interval: 1m
      rules:
        # 错误率告警
        - alert: HighErrorRate
          expr: |
            sum(rate(http_requests_total{service="goalfy-api", status=~"5.."}[5m]))
            /
            sum(rate(http_requests_total{service="goalfy-api"}[5m]))
            > 0.01
          for: 5m    # 持续 5 分钟才触发，避免瞬间抖动
          labels:
            severity: critical
            team: backend
          annotations:
            summary: "goalfy-api 错误率过高"
            description: "当前错误率 {{ $value | humanizePercentage }}，持续超过 5 分钟"
            runbook_url: "https://wiki.internal/runbooks/high-error-rate"

        # P99 延迟告警
        - alert: HighLatencyP99
          expr: |
            histogram_quantile(0.99,
              sum(rate(http_request_duration_seconds_bucket{service="goalfy-api"}[5m])) by (le)
            ) > 2
          for: 10m
          labels:
            severity: warning
            team: backend
          annotations:
            summary: "goalfy-api P99 延迟超过 2 秒"
            description: "P99 延迟当前为 {{ $value | humanizeDuration }}"

        # Pod 不可用告警
        - alert: PodNotReady
          expr: |
            kube_pod_status_ready{condition="true", namespace="goalfy"}
            / kube_deployment_spec_replicas{namespace="goalfy"}
            < 0.5
          for: 3m
          labels:
            severity: critical
          annotations:
            summary: "{{ $labels.deployment }} 超过一半 Pod 不可用"
```

`for` 字段非常重要。不加 `for` 时，指标一超阈值就立刻触发，网络抖动、单次慢请求都会产生告警。加了 `for: 5m` 后，只有持续 5 分钟超阈值才触发，大幅减少误报。

### Alertmanager 路由配置

```yaml
# alertmanager.yaml
global:
  resolve_timeout: 5m

route:
  receiver: default
  group_by: ["alertname", "team"]
  group_wait: 30s          # 等待同组告警聚合
  group_interval: 5m       # 同组告警再次发送间隔
  repeat_interval: 4h      # 持续告警重复发送间隔

  routes:
    # critical 告警走 PagerDuty（或电话）
    - match:
        severity: critical
      receiver: pagerduty
      continue: true     # 继续匹配下面的路由，同时发钉钉

    # 所有告警都发钉钉
    - match_re:
        severity: critical|warning
      receiver: dingtalk

    # 特定 team 的告警发对应群
    - match:
        team: backend
      receiver: dingtalk-backend

receivers:
  - name: default
    webhook_configs:
      - url: "http://alertmanager-webhook/default"

  - name: dingtalk
    webhook_configs:
      - url: "http://dingtalk-webhook:8060/dingtalk/ops/send"
        send_resolved: true

  - name: dingtalk-backend
    webhook_configs:
      - url: "http://dingtalk-webhook:8060/dingtalk/backend/send"
        send_resolved: true

inhibit_rules:
  # critical 告警触发时，抑制同 namespace 的 warning 告警
  - source_match:
      severity: critical
    target_match:
      severity: warning
    equal: ["namespace"]
```

### DingTalk Webhook 配置

使用 `timonwong/prometheus-webhook-dingtalk` 这个开源工具作为 Alertmanager 和钉钉之间的适配器：

```yaml
# prometheus-webhook-dingtalk config.yaml
targets:
  ops:
    url: https://oapi.dingtalk.com/robot/send?access_token=<YOUR_TOKEN>
    secret: <YOUR_SECRET>    # 安全设置里的加签密钥
    message:
      title: '{{ template "ding.link.title" . }}'
      text: '{{ template "ding.link.content" . }}'

  backend:
    url: https://oapi.dingtalk.com/robot/send?access_token=<BACKEND_TOKEN>
    secret: <BACKEND_SECRET>
```

K8s 部署：

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: dingtalk-webhook
  namespace: monitoring
spec:
  replicas: 1
  template:
    spec:
      containers:
        - name: webhook
          image: timonwong/prometheus-webhook-dingtalk:latest
          args:
            - --config.file=/config/config.yaml
          ports:
            - containerPort: 8060
          volumeMounts:
            - name: config
              mountPath: /config
      volumes:
        - name: config
          configMap:
            name: dingtalk-webhook-config
```

## 常见陷阱

### Cardinality 爆炸

这是 Prometheus 生产环境最常见的性能杀手。Prometheus 的内存使用量和时间序列（time series）数量成正比，而时间序列数量 = 所有 label 值的组合数。

一个典型的错误：把用户 ID 或请求 ID 作为 label：

```go
// 错误：userId 有多少用户就有多少时间序列
httpRequestsTotal.WithLabelValues(userId, endpoint, method).Inc()

// 正确：label 只包含低基数的维度
httpRequestsTotal.WithLabelValues(endpoint, method, statusCode).Inc()
```

检查当前 cardinality：

```promql
# 找出时间序列最多的指标
topk(10, count({__name__=~".+"}) by (__name__))

# 某个指标的 label 基数
count(http_requests_total) by (user_id)
```

一旦发现 cardinality 问题，可以用 `metric_relabel_configs` 在采集时丢弃高基数 label：

```yaml
metric_relabel_configs:
  - source_labels: [user_id]
    action: labeldrop
    regex: user_id
```

### 告警噪音抑制

告警太多等于没有告警。几个减少噪音的策略：

**1. 合理使用 `for` 字段**

不同严重程度配置不同的持续时间：critical 告警 `for: 5m`，warning 告警 `for: 15m`。

**2. inhibit_rules 抑制重复告警**

上游故障会导致下游一堆告警同时触发，用 inhibit 只保留根因：

```yaml
inhibit_rules:
  # 如果节点挂了，抑制该节点上所有 Pod 的告警
  - source_match:
      alertname: NodeNotReady
    target_match_re:
      alertname: PodNotReady|HighErrorRate
    equal: ["node"]
```

**3. 告警分级**

- P0（电话/即时消息唤醒）：影响用户的生产故障
- P1（即时消息）：有潜在影响，需要几小时内处理
- P2（工单/邮件）：需要关注但不紧急
- P3（Dashboard 展示）：趋势性问题，不告警

大多数团队的问题是把太多 P2/P3 的内容用 P0 级别通知出来，导致真正的 P0 被淹没。

---

可观测性建设是一个持续迭代的过程。不要试图一开始就建立完美的监控体系，从最核心的 RED 指标开始，每次故障复盘后补充缺失的监控，逐步完善。
