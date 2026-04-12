---
title: "可观测性三支柱实战：Metrics/Logs/Traces 联动"
date: 2025-07-14T09:52:00+08:00
draft: false
tags: ["可观测性", "Prometheus", "Loki", "Jaeger", "OpenTelemetry", "Grafana"]
categories: ["可观测性"]
series: ["可观测性实战"]
description: "深入讲解可观测性三支柱的适用场景与局限，以及 Metrics/Logs/Traces 联动排障的实战流程，覆盖 OpenTelemetry、Exemplar 原理与 Grafana 联动查询配置。"
summary: "监控告诉你系统挂了，可观测性告诉你为什么挂。本文从三支柱的核心差异出发，讲透 Prometheus+Loki+Tempo 的联动排障流程，覆盖 OpenTelemetry 采集标准、Exemplar 原理与配置，以及可观测性建设的优先级策略。"
toc: true
math: false
diagram: false
keywords: ["可观测性", "observability", "prometheus", "loki", "jaeger", "tempo", "opentelemetry", "exemplar", "grafana", "分布式追踪"]
params:
  reading_time: true
---

我们的服务曾经有一段时间，用户投诉下单偶发失败，但 Prometheus 的可用性指标显示 99.7%，看起来没什么问题。日志里有 ERROR，但一天产生几百万条日志，根本不知道从哪里找起。

那次事故花了三个小时才定位到根因——是支付服务调用银行 API 时，在特定网络抖动场景下，超时配置没有对齐，导致重试风暴。

这三个小时本来可以缩短到 20 分钟，如果当时有 Traces——能直接看到那条请求路径，看到哪一段调用慢了、哪里发生了重试。

这就是监控（Monitoring）和可观测性（Observability）的差距。

## 为什么监控不等于可观测性

监控是告诉你**已知的问题**——你提前设置了告警阈值，系统越过阈值时通知你。

可观测性是让你能**回答任意问题**——即使是你从来没预料过的问题。它的核心不是收集更多数据，而是让你在面对未知故障时，能通过系统产生的信号去追问"为什么"。

一个只有监控的系统：
> "API 错误率超过 1%，触发告警"
> — 我知道系统挂了，但不知道为什么

一个有可观测性的系统：
> "API 错误率超过 1%，触发告警"
> → 查看错误率折线图，确认只有 /checkout 路径出问题
> → 跳转到该时间段的 Error 日志
> → 找到包含 trace_id 的日志行
> → 跳转到对应的 Trace，看到完整调用链
> → 发现 payment-service → bank-api 这一跳 P99 延迟突然从 200ms 涨到 3000ms
> → 根因：银行 API 区域性限速

整个过程 10 分钟，不需要猜测。

## 三支柱各自的定位

### Metrics：聚合的数字

Metrics 是时间序列数据——在某个时间点，某个指标的数值是多少。

```
http_requests_total{method="POST",path="/checkout",status="500"} 42 1712900400
```

**Prometheus 的数据模型：**

```
指标名{标签1="值1", 标签2="值2"} 数值 时间戳
```

Prometheus 抓取（scrape）服务暴露的 `/metrics` 端点：

```
# HELP http_requests_total Total HTTP requests
# TYPE http_requests_total counter
http_requests_total{method="GET",path="/api/users",status="200"} 12453
http_requests_total{method="POST",path="/api/checkout",status="500"} 23
http_requests_total{method="POST",path="/api/checkout",status="200"} 8921
```

**Metrics 的优势：**
- 存储成本极低（聚合数据，不是原始数据）
- 查询快（时序数据库 TSDB 针对时间范围查询优化）
- 适合趋势分析、SLO 计算、告警规则

**Metrics 的局限：**
- 高基数问题：Label 的组合数量爆炸（比如 user_id 作为 Label 有百万种值，会把 Prometheus 打垮）
- 只有数字，没有上下文：知道有 23 个 500 错误，但不知道是什么错误
- 无法追踪单个请求

### Logs：完整的事件记录

Logs 是离散的事件记录，包含上下文信息。

**好的日志格式（结构化 JSON）：**

```json
{
  "timestamp": "2026-04-12T14:32:15.123Z",
  "level": "error",
  "service": "checkout-api",
  "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
  "span_id": "00f067aa0ba902b7",
  "user_id": "u_12345",
  "order_id": "ord_98765",
  "message": "Payment failed",
  "error": "bank API timeout after 3000ms",
  "http_path": "/api/checkout",
  "http_method": "POST",
  "http_status": 500,
  "duration_ms": 3102
}
```

**Loki 的核心设计理念：** 只索引 Labels，不索引日志内容本身。这使得 Loki 的存储成本远低于 Elasticsearch，代价是全文搜索较慢（需要用 grep-style 的 LogQL）。

```logql
# LogQL 示例
{service="checkout-api", level="error"}                          # 过滤 Labels
{service="checkout-api"} |= "Payment failed"                     # 包含字符串
{service="checkout-api"} | json | duration_ms > 3000             # 解析 JSON 字段并过滤
{service="checkout-api"} | json | line_format "{{.trace_id}}"   # 格式化输出

# 聚合：每分钟错误数
sum(rate({service="checkout-api", level="error"}[1m])) by (http_path)
```

**Logs 的优势：**
- 保留完整的事件上下文
- 可以回答"具体发生了什么"
- 包含 trace_id 时，可以连接到 Traces

**Logs 的局限：**
- 数据量大，存储成本高
- 查询速度慢（相对 Metrics）
- 非结构化日志难以分析
- 大量日志时，找到"那条"日志很困难

### Traces：请求的完整旅程

Traces（分布式追踪）记录一个请求在整个系统中的调用路径和时间。

一个 Trace 由多个 Span 组成，每个 Span 代表一个操作（一次 HTTP 调用、一次数据库查询）：

```
Trace ID: 4bf92f3577b34da6a3ce929d0e0e4736

[frontend]         POST /checkout                          0ms → 3102ms
  [checkout-api]   handleCheckout                          2ms → 3100ms
    [checkout-api] validateOrder (DB query)                3ms → 12ms
    [checkout-api] reserveInventory (gRPC)                15ms → 45ms
    [checkout-api] processPayment (HTTP)                   48ms → 3098ms  ← 这里慢！
      [payment-svc] callBankAPI                            50ms → 3096ms  ← 超时
```

看到这个 Flame Graph，3 秒的延迟立刻定位到 payment-svc 调用银行 API 这一跳。

**Traces 的优势：**
- 端到端可见性，跨服务请求路径清晰
- 直接定位性能瓶颈
- 理解服务依赖关系

**Traces 的局限：**
- 需要在代码中 Instrumentation（插桩）
- 通常需要采样（全量会有性能开销）
- 只能追踪单个请求，不适合聚合分析

## 三支柱联动的实际场景

这才是可观测性的精华——单独看任何一个支柱都是片面的，三者联动才能快速定位根因。

### 典型排障流程

```
1. 告警触发（Metrics）
   Alertmanager: checkout-api 错误率 P0 告警，过去 5 分钟 5xx 率 = 8.3%

2. 定位范围（Metrics）
   在 Grafana 看 http_requests_total 按 path 分组
   → 只有 /api/checkout 出问题，其他接口正常
   → 问题发生时间：14:28 开始

3. 查看日志（Logs）
   LogQL: {service="checkout-api", level="error"} 时间范围 14:25-14:35
   → 找到 500 错误日志，message: "Payment failed: bank API timeout"
   → 日志里有 trace_id: "4bf92f3577b34da6a3ce929d0e0e4736"

4. 追踪调用链（Traces）
   用 trace_id 在 Tempo 查询
   → Flame Graph 显示 processPayment → callBankAPI 耗时 3050ms
   → 银行 API 正常 SLA 是 200ms，这次超时

5. 根因确认
   跨服务查看 payment-svc 的日志（同时间段）
   → "bank API returned 429 Too Many Requests"
   → 原来是促销活动导致下单量激增，触发了银行 API 限速
```

整个过程 Metrics → Logs → Traces 三次跳转，每次跳转都是在缩小范围、深入细节。

## OpenTelemetry：统一采集标准

在 OpenTelemetry 之前，每家厂商都有自己的 SDK：Jaeger 有 jaeger-client，Zipkin 有 zipkin-client，Datadog 有 dd-trace，迁移成本极高。

OpenTelemetry（OTel）是 CNCF 的开源项目，目标是成为 Metrics/Logs/Traces 的统一采集标准：

```
应用代码
  → OTel SDK（统一 API）
    → OTel Collector（处理、过滤、路由）
      → Prometheus（Metrics）
      → Loki（Logs）
      → Tempo / Jaeger（Traces）
```

**Go 服务接入示例：**

```go
package main

import (
    "go.opentelemetry.io/otel"
    "go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc"
    "go.opentelemetry.io/otel/sdk/trace"
    "go.opentelemetry.io/otel/propagation"
)

func initTracer(ctx context.Context) (*trace.TracerProvider, error) {
    exporter, err := otlptracegrpc.New(ctx,
        otlptracegrpc.WithEndpoint("otel-collector:4317"),
        otlptracegrpc.WithInsecure(),
    )
    if err != nil {
        return nil, err
    }

    tp := trace.NewTracerProvider(
        trace.WithBatcher(exporter),
        trace.WithSampler(trace.TraceIDRatioBased(0.1)), // 采样 10%
        trace.WithResource(resource.NewWithAttributes(
            semconv.SchemaURL,
            semconv.ServiceNameKey.String("checkout-api"),
            semconv.ServiceVersionKey.String("v2.3.0"),
        )),
    )

    otel.SetTracerProvider(tp)
    otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(
        propagation.TraceContext{},
        propagation.Baggage{},
    ))

    return tp, nil
}

// 在业务函数中使用
func processPayment(ctx context.Context, orderID string) error {
    tracer := otel.Tracer("checkout-api")
    ctx, span := tracer.Start(ctx, "processPayment")
    defer span.End()

    span.SetAttributes(
        attribute.String("order.id", orderID),
        attribute.String("payment.method", "credit_card"),
    )

    // 调用外部 API 时，trace context 会通过 HTTP header 传播
    result, err := callBankAPI(ctx, orderID)
    if err != nil {
        span.RecordError(err)
        span.SetStatus(codes.Error, err.Error())
        return err
    }

    span.SetAttributes(attribute.String("payment.transaction_id", result.TxID))
    return nil
}
```

**OTel Collector 配置：**

```yaml
# otel-collector-config.yaml
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
      http:
        endpoint: 0.0.0.0:4318

processors:
  batch:
    timeout: 5s
    send_batch_size: 1000
  resource:
    attributes:
      - key: environment
        value: production
        action: insert

exporters:
  otlp/tempo:
    endpoint: tempo:4317
    tls:
      insecure: true
  prometheusremotewrite:
    endpoint: http://prometheus:9090/api/v1/write
  loki:
    endpoint: http://loki:3100/loki/api/v1/push

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [batch, resource]
      exporters: [otlp/tempo]
    metrics:
      receivers: [otlp]
      processors: [batch]
      exporters: [prometheusremotewrite]
    logs:
      receivers: [otlp]
      processors: [batch]
      exporters: [loki]
```

## Exemplar：Metric 中嵌入 Trace ID

Exemplar 是 Prometheus 的一个功能，允许在 Metrics 数据点上附加额外的元数据（比如 trace_id）。这是 Metrics → Traces 直接跳转的关键。

**原理：** 在记录某个 Histogram 观测值时，同时记录当时的 trace_id。当你在 Grafana 看到延迟突增时，可以直接点击那个数据点，跳转到对应的 Trace。

**Go 代码中启用 Exemplar：**

```go
import (
    "github.com/prometheus/client_golang/prometheus"
    "go.opentelemetry.io/otel/trace"
)

var (
    requestDuration = prometheus.NewHistogramVec(
        prometheus.HistogramOpts{
            Name:    "http_request_duration_seconds",
            Buckets: prometheus.DefBuckets,
        },
        []string{"method", "path", "status"},
    )
)

// HTTP middleware
func metricsMiddleware(next http.Handler) http.Handler {
    return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        start := time.Now()
        rw := newResponseWriter(w)
        next.ServeHTTP(rw, r)

        duration := time.Since(start).Seconds()
        labels := prometheus.Labels{
            "method": r.Method,
            "path":   r.URL.Path,
            "status": strconv.Itoa(rw.statusCode),
        }

        // 从当前 context 获取 trace_id，附加到 Exemplar
        spanCtx := trace.SpanFromContext(r.Context()).SpanContext()
        if spanCtx.IsValid() {
            requestDuration.With(labels).(prometheus.ExemplarObserver).ObserveWithExemplar(
                duration,
                prometheus.Labels{"traceID": spanCtx.TraceID().String()},
            )
        } else {
            requestDuration.With(labels).Observe(duration)
        }
    })
}
```

**Prometheus 配置启用 Exemplar 存储：**

```yaml
# prometheus.yml
global:
  scrape_interval: 15s

# 启用 exemplar 存储（需要 Prometheus 2.26+）
storage:
  exemplars:
    max_exemplars: 100000
```

**Grafana 中查看 Exemplar：** 在 Panel 设置中，开启 "Exemplars" 选项，数据点上会出现小菱形标记，点击可跳转到 Tempo 查询对应 Trace。

## Grafana Explore 联动查询实战

Grafana Explore 是三支柱联动的最佳入口。

**配置数据源关联（Data Source Linking）：**

```yaml
# grafana.ini 或 provisioning/datasources/
# 在 Loki 数据源中配置关联到 Tempo
apiVersion: 1
datasources:
  - name: Loki
    type: loki
    url: http://loki:3100
    jsonData:
      derivedFields:
        - datasourceUid: tempo-uid      # Tempo 数据源的 UID
          matcherRegex: '"trace_id":"(\w+)"'  # 从日志中提取 trace_id
          name: TraceID
          url: '$${__value.raw}'         # 跳转 URL

  - name: Tempo
    type: tempo
    url: http://tempo:3200
    uid: tempo-uid
    jsonData:
      tracesToLogs:
        datasourceUid: loki-uid         # 从 Trace 跳转到 Loki
        filterByTraceID: true
        tags: ['service.name', 'pod']
      tracesToMetrics:
        datasourceUid: prometheus-uid   # 从 Trace 跳转到 Prometheus
        tags: [{key: 'service.name', value: 'service'}]
        queries:
          - name: 'Request Rate'
            query: 'rate(http_requests_total{service="$${__tags.service}"}[5m])'

  - name: Prometheus
    type: prometheus
    url: http://prometheus:9090
    uid: prometheus-uid
    jsonData:
      exemplarTraceIdDestinations:
        - datasourceUid: tempo-uid      # Exemplar 中的 traceID 跳转到 Tempo
          name: traceID
```

配置完成后，在 Grafana Explore 中：
- 看 Metrics 时，Exemplar 菱形点击直接跳 Traces
- 看 Logs 时，trace_id 字段点击直接跳 Traces
- 看 Traces 时，Service name 点击直接跳对应时间段的 Logs 或 Metrics

## 可观测性建设的优先级

从零开始建设，应该按什么顺序来？

### 第一步：Metrics（最高 ROI）

Metrics 的采集成本低、查询快、告警系统成熟。先把所有服务的基础指标接入：

```
必须有：
- HTTP 请求数（按状态码分组）
- HTTP 请求延迟（Histogram，有 P50/P95/P99）
- 错误率
- 服务实例数/可用性

K8s 基础设施：
- CPU/内存使用率（kube-state-metrics + node-exporter）
- Pod 重启次数
- OOM Kill 次数

数据库：
- 慢查询数量
- 连接池使用率
- 错误率
```

**先有 Metrics，才能设 SLO，才有告警，才有 On-call 触发点。**

### 第二步：结构化日志（中等 ROI）

把服务的日志全部改成 JSON 格式，并统一包含 trace_id、service、level、timestamp。

这一步看起来简单，实际上推动起来最难——因为要改所有服务的代码。可以用 Middleware/Interceptor 统一注入字段，减少各服务的改造成本。

### 第三步：Traces（前提：服务间调用链复杂）

Traces 的价值在于**跨服务调用**。如果你的架构是单体应用，Traces 的价值不大；如果是微服务（5个以上服务互相调用），Traces 能节省大量排障时间。

接入顺序：先从核心链路（用户下单/支付等）开始，不需要一次接入所有服务。

### 优先级总结

```
单体应用：
  Metrics > Logs > Traces（Traces 可能不需要）

微服务（<5个）：
  Metrics > Logs > Traces（Traces 有用但不紧迫）

微服务（>5个）：
  Metrics > Traces > Logs（Traces 比完整日志更有性价比）
```

## 踩坑记录

### 踩坑一：日志没有 trace_id，三支柱变两支柱

Metrics → Traces 的路径靠 Exemplar，Traces → Logs 的路径靠 trace_id。如果日志里没有 trace_id，这条链就断了。

解决：在日志 Middleware 里，从 span context 提取 trace_id 写入日志：

```go
func loggingMiddleware(next http.Handler) http.Handler {
    return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        ctx := r.Context()
        spanCtx := trace.SpanFromContext(ctx).SpanContext()

        logger := log.With().
            Str("trace_id", spanCtx.TraceID().String()).
            Str("span_id", spanCtx.SpanID().String()).
            Logger()

        r = r.WithContext(logger.WithContext(ctx))
        next.ServeHTTP(w, r)
    })
}
```

### 踩坑二：Prometheus 高基数

把 user_id、order_id 这类高基数字段当 Label，会导致 Prometheus 时间序列数量爆炸（每个用户一条序列），内存暴涨。

规则：**Label 的不同值数量 > 1000 就要谨慎，> 10000 绝对不行**。

高基数数据应该放进 Logs 或 Traces，不要放 Metrics。

### 踩坑三：采样率配置不当

Traces 全量采集会有性能开销。但采样率设太低（比如 1%），在低流量时根本看不到 Trace。

更好的策略是**尾采样（Tail Sampling）**：先收集所有 Trace，在 OTel Collector 端根据规则决定保留哪些：

```yaml
# 尾采样规则：错误的 Trace 全保留，慢请求全保留，其他随机 10%
processors:
  tail_sampling:
    decision_wait: 10s
    policies:
      - name: keep-errors
        type: status_code
        status_code: {status_codes: [ERROR]}
      - name: keep-slow
        type: latency
        latency: {threshold_ms: 2000}
      - name: sample-rest
        type: probabilistic
        probabilistic: {sampling_percentage: 10}
```

## 总结

可观测性的核心是**让工程师有能力理解任何系统状态**，而不只是"系统是否健康"。

三支柱的价值不在于各自，而在于联动：

- **Metrics**：发现问题（告警触发），缩小范围（哪个服务、哪个接口）
- **Logs**：理解细节（具体发生了什么，包含 trace_id）
- **Traces**：定位根因（跨服务调用链，哪一跳出了问题）

建设顺序：先 Metrics（低成本、高 ROI）→ 结构化日志（确保包含 trace_id）→ Traces（从核心链路开始）。

OpenTelemetry 的出现大幅降低了接入成本，也解决了厂商锁定问题。如果你现在从零开始建设，直接上 OTel SDK + OTel Collector，不要再用各家厂商的私有 SDK。
