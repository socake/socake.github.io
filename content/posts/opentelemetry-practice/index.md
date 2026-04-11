---
title: "OpenTelemetry 落地实践：统一采集 Traces、Metrics、Logs"
date: 2026-04-11T14:00:00+08:00
draft: false
tags: ["OpenTelemetry", "可观测性", "Traces", "Grafana", "Tempo", "Loki"]
categories: ["可观测性"]
description: "记录在 Kubernetes 集群上落地 OpenTelemetry 的完整过程，包括 Collector 部署架构、核心配置、自动埋点注入，以及 Grafana 里 Traces 和 Logs 联动的实现。"
summary: "从为什么选 OpenTelemetry 讲起，给出 DaemonSet + Gateway 的 Collector 部署架构、关键配置和实际踩坑记录。"
toc: true
math: false
diagram: false
keywords: ["OpenTelemetry", "OTel Collector", "Tempo", "Loki", "Grafana", "可观测性", "Traces", "Kubernetes"]
params:
  reading_time: true
---

## 为什么选 OpenTelemetry

在 OpenTelemetry 之前，我们的可观测性栈是"各自为政"的：链路追踪用 Jaeger，服务自己打点 Prometheus 指标，日志靠 Fluent Bit 采集写 Loki。三套体系，三套 Agent，三种数据格式，互相之间完全割裂。

最直接的问题是排障体验差。某个接口偶发超时，我先去 Grafana 看指标，发现 P99 飙升，但指标里看不出是哪个 upstream 慢。然后切到 Jaeger 查 Trace，但 Jaeger 和 Grafana 是两个 URL，时间轴不联动，手动对齐时间段很麻烦。最后想看对应时间段的日志，又要去 Grafana Explore 手动输 Loki 查询，还要自己算时间范围。

整个排障链路大概要 10 分钟才能把三个维度的数据拼在一起。

OpenTelemetry 解决的核心问题是**标准化**：用 OTLP（OpenTelemetry Protocol）统一三种信号的传输格式，用 OTel Collector 统一数据的收集、处理和转发。应用侧只需要输出 OTLP，后端存哪里是 Collector 的事。我们的后端选择是：Traces → Tempo，Metrics → Prometheus，Logs → Loki，全部在 Grafana 统一查看，并且 Trace 和 Log 通过 TraceID 自动关联。

---

## 整体架构

我们采用的是两层 Collector 架构：

```
应用 Pod（OTLP 导出）
    ↓
OTel Collector Agent（DaemonSet，每个节点一个）
    ↓
OTel Collector Gateway（Deployment，2-3 副本）
    ├── Traces → Tempo
    ├── Metrics → Prometheus Remote Write
    └── Logs → Loki
```

**为什么要两层？**

Agent（DaemonSet）负责在节点本地接收数据，做轻量的初步处理（加 K8s 元数据标签），然后批量转发给 Gateway。这样做有几个好处：

1. 应用不需要知道后端地址，只需要发到本节点的 Agent（`localhost:4317`），网络开销最小。
2. Agent 负载分散在各节点上，不会因为某个 Agent 挂掉影响整个集群。
3. Gateway 可以集中做更重的处理（采样决策、批量写入），独立扩容。

---

## OTel Collector 核心配置

### Agent（DaemonSet）配置

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: otel-agent-config
  namespace: monitoring
data:
  config.yaml: |
    receivers:
      otlp:
        protocols:
          grpc:
            endpoint: 0.0.0.0:4317
          http:
            endpoint: 0.0.0.0:4318
      # 采集节点本身的指标（CPU、内存等）
      hostmetrics:
        collection_interval: 30s
        scrapers:
          cpu:
          memory:
          disk:
          network:

    processors:
      # 添加 K8s 元数据：pod name、namespace、node 等
      k8sattributes:
        extract:
          metadata:
            - k8s.namespace.name
            - k8s.pod.name
            - k8s.node.name
            - k8s.deployment.name
            - k8s.container.name
        pod_association:
          - sources:
            - from: resource_attribute
              name: k8s.pod.ip
          - sources:
            - from: connection

      # 内存保护，超过限制时开始丢弃数据
      memory_limiter:
        limit_mib: 256
        spike_limit_mib: 64
        check_interval: 5s

      # 批量发送，减少网络请求次数
      batch:
        send_batch_size: 1000
        timeout: 5s
        send_batch_max_size: 2000

    exporters:
      otlp/gateway:
        endpoint: otel-gateway-collector:4317
        tls:
          insecure: true

    service:
      pipelines:
        traces:
          receivers: [otlp]
          processors: [memory_limiter, k8sattributes, batch]
          exporters: [otlp/gateway]
        metrics:
          receivers: [otlp, hostmetrics]
          processors: [memory_limiter, k8sattributes, batch]
          exporters: [otlp/gateway]
        logs:
          receivers: [otlp]
          processors: [memory_limiter, k8sattributes, batch]
          exporters: [otlp/gateway]
```

### Gateway（Deployment）配置

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: otel-gateway-config
  namespace: monitoring
data:
  config.yaml: |
    receivers:
      otlp:
        protocols:
          grpc:
            endpoint: 0.0.0.0:4317

    processors:
      memory_limiter:
        limit_mib: 1024
        spike_limit_mib: 256
        check_interval: 5s

      batch:
        send_batch_size: 2000
        timeout: 10s

      # 尾部采样：在 Gateway 层做采样决策
      # 确保一条 Trace 的所有 Span 被同一个 Gateway 实例处理后再决定要不要保留
      tail_sampling:
        decision_wait: 10s
        num_traces: 10000
        policies:
          # 有错误的 Trace 全部保留
          - name: errors-policy
            type: status_code
            status_code: {status_codes: [ERROR]}
          # 慢请求全部保留（超过 1 秒）
          - name: slow-traces-policy
            type: latency
            latency: {threshold_ms: 1000}
          # 其余按 10% 采样
          - name: sample-policy
            type: probabilistic
            probabilistic: {sampling_percentage: 10}

    exporters:
      otlp/tempo:
        endpoint: tempo:4317
        tls:
          insecure: true

      prometheusremotewrite:
        endpoint: http://prometheus:9090/api/v1/write
        tls:
          insecure: true

      loki:
        endpoint: http://loki:3100/loki/api/v1/push
        default_labels_enabled:
          exporter: false
          job: true
          level: true
        # 把 resource attributes 映射为 Loki label
        labels:
          resource:
            k8s.namespace.name: "namespace"
            k8s.pod.name: "pod"
            k8s.container.name: "container"
            service.name: "service"

    service:
      pipelines:
        traces:
          receivers: [otlp]
          processors: [memory_limiter, tail_sampling, batch]
          exporters: [otlp/tempo]
        metrics:
          receivers: [otlp]
          processors: [memory_limiter, batch]
          exporters: [prometheusremotewrite]
        logs:
          receivers: [otlp]
          processors: [memory_limiter, batch]
          exporters: [loki]
```

---

## K8s 自动注入 Instrumentation

OTel Operator 提供了 `Instrumentation` CRD，可以通过 annotation 自动向 Pod 注入 SDK，无需修改应用代码。

首先安装 OTel Operator：

```bash
kubectl apply -f https://github.com/open-telemetry/opentelemetry-operator/releases/latest/download/opentelemetry-operator.yaml
```

定义 Instrumentation 资源：

```yaml
apiVersion: opentelemetry.io/v1alpha1
kind: Instrumentation
metadata:
  name: otel-instrumentation
  namespace: default
spec:
  exporter:
    endpoint: http://$(OTEL_AGENT_HOST):4318  # 发到本节点 Agent
  propagators:
    - tracecontext
    - baggage
    - b3

  # Python 自动埋点配置
  python:
    env:
      - name: OTEL_LOGS_EXPORTER
        value: otlp
      - name: OTEL_PYTHON_LOG_CORRELATION
        value: "true"  # 自动在日志里注入 TraceID
      - name: OTEL_PYTHON_LOG_LEVEL
        value: info

  # Java 自动埋点配置
  java:
    env:
      - name: OTEL_INSTRUMENTATION_JDBC_ENABLED
        value: "true"

  # Go 不支持真正的 eBPF 级别自动注入（编译型语言限制）
  # 但可以注入环境变量，配合应用使用 auto-instrumentation 库
  go:
    env:
      - name: OTEL_GO_AUTO_TARGET_EXE
        value: /app/server
```

在 Deployment 上添加 annotation 触发注入：

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: my-python-service
spec:
  template:
    metadata:
      annotations:
        # 自动注入 Python SDK
        instrumentation.opentelemetry.io/inject-python: "true"
        # 或者指定具体的 Instrumentation 资源
        instrumentation.opentelemetry.io/inject-python: "otel-instrumentation"
    spec:
      containers:
        - name: app
          image: my-python-service:latest
          env:
            - name: OTEL_SERVICE_NAME
              value: "my-python-service"
            - name: OTEL_RESOURCE_ATTRIBUTES
              value: "deployment.environment=production"
```

对于 Go 服务，由于 Go 是编译型语言，自动注入只能注入环境变量，实际 SDK 集成还是需要在代码里引入：

```go
import (
    "go.opentelemetry.io/otel"
    "go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc"
    sdktrace "go.opentelemetry.io/otel/sdk/trace"
)

func initTracer() func() {
    ctx := context.Background()
    
    // 从环境变量读取 endpoint，方便 K8s 注入配置
    endpoint := os.Getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if endpoint == "" {
        endpoint = "localhost:4317"
    }
    
    exp, _ := otlptracegrpc.New(ctx,
        otlptracegrpc.WithEndpoint(endpoint),
        otlptracegrpc.WithInsecure(),
    )
    
    tp := sdktrace.NewTracerProvider(
        sdktrace.WithBatcher(exp),
        sdktrace.WithResource(resource.NewWithAttributes(
            semconv.SchemaURL,
            semconv.ServiceName(os.Getenv("OTEL_SERVICE_NAME")),
        )),
    )
    otel.SetTracerProvider(tp)
    
    return func() { tp.Shutdown(ctx) }
}
```

---

## Grafana 联动：从 Trace 跳转到 Logs

这是 OpenTelemetry 方案最大的体验优势之一。在 Grafana 里配置 Tempo 和 Loki 的关联：

**Tempo Data Source 配置（在 Grafana UI 里）：**

```yaml
# Tempo datasource 的 "Derived fields" 或在 provisioning 里配置
# 在 Tempo 的 datasource 设置里，找 "Trace to logs" 配置：
datasources:
  - name: Tempo
    type: tempo
    url: http://tempo:3100
    jsonData:
      tracesToLogsV2:
        datasourceUid: loki-uid   # Loki datasource 的 UID
        spanStartTimeShift: "-5m"
        spanEndTimeShift: "5m"
        filterByTraceID: true
        filterBySpanID: false
        customQuery: true
        query: |
          {namespace="${__span.tags["k8s.namespace.name"]}",
           pod="${__span.tags["k8s.pod.name"]}"}
           | json
           | trace_id="${__trace.traceId}"
```

配置完成后，在 Tempo 的 Trace 视图里点击任意一个 Span，右侧会出现 "Logs for this span" 的跳转链接，自动带着 TraceID 和时间范围跳转到 Loki，过滤出这条请求对应的所有日志行。

---

## 踩坑记录

### Collector 内存暴涨

上线初期遇到 OTel Gateway 的内存一直在涨，最终 OOM。排查后发现有两个原因叠加：

**原因一：batch processor 配置不当。** 我们最初的配置是 `timeout: 30s`，同时 `send_batch_max_size` 没有设置上限。在流量突增时，30 秒内积攒的数据量非常大，一次性刷出去前内存占用极高。

修复：把 `timeout` 降到 `5s`，同时设置 `send_batch_max_size: 2000` 作为硬上限。

**原因二：tail_sampling 的 `num_traces` 设置过大。** tail_sampling 需要在内存里缓存完整 Trace 直到 `decision_wait` 时间到期。如果 `num_traces: 100000`，每条 Trace 平均 10 个 Span，每个 Span 2KB，就是 2GB 内存。

修复：根据实际流量估算，把 `num_traces` 降到合理值（我们设的是 10000），并且给 Gateway 设置足够的内存 limit，同时确保 `memory_limiter` 在内存达到 80% 时开始丢弃数据，而不是 OOM。

### 采样率设置的坑

尾部采样（tail sampling）的 `decision_wait` 必须大于最长可能的 Trace 持续时间。我们有一个批处理任务的 Trace 可能持续 2 分钟，如果 `decision_wait: 10s`，这条 Trace 会在还没结束时就被做出"保留/丢弃"的决策，导致 Trace 数据不完整。

另外，使用 tail_sampling 时，同一条 Trace 的所有 Span 必须发到同一个 Gateway 实例，否则每个实例只看到部分 Span，无法做正确的采样决策。解决方法是在 Agent 到 Gateway 之间用基于 TraceID 的负载均衡（`loadbalancing` exporter）：

```yaml
exporters:
  loadbalancing:
    protocol:
      otlp:
        tls:
          insecure: true
    resolver:
      k8s:
        service: otel-gateway-collector
        ports:
          - 4317
```

这个 exporter 会把同一 TraceID 的所有 Span 路由到同一个 Gateway 实例。
