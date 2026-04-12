---
title: "分布式链路追踪实战：Jaeger 与 Tempo 选型对比"
date: 2025-07-10T10:00:00+08:00
draft: false
tags: ["链路追踪", "Jaeger", "Tempo", "OpenTelemetry", "可观测性"]
categories: ["可观测性"]
description: "从链路追踪核心概念出发，深入对比 Jaeger 与 Tempo 的架构差异、存储成本、查询能力和 Grafana 集成度，给出生产环境选型建议，并附 Tempo + OpenTelemetry 完整落地实战。"
summary: "系统梳理 Jaeger 与 Tempo 的架构差异与适用场景，结合 OpenTelemetry SDK 插桩、TraceQL 查询、采样策略和 Traces/Metrics/Logs 关联，给出可落地的生产实战方案。"
toc: true
math: false
diagram: false
series: ["可观测性实战"]
keywords: ["Jaeger", "Tempo", "链路追踪", "OpenTelemetry", "TraceQL", "采样策略", "可观测性", "Kubernetes", "Grafana"]
params:
  reading_time: true
---

## 为什么要做链路追踪

微服务拆分之后，一个用户请求可能穿越十几个服务。当 P99 延迟飙升，你拿着指标只能看到某个服务的 HTTP 响应时间变长，但不知道是这个服务本身慢，还是它调用的下游慢，或者某个中间件出了问题。

链路追踪解决的核心问题是**请求粒度的因果链**。它把一次完整请求拆成若干有因果关系的操作单元，记录每段操作的开始时间、结束时间、状态和关键属性，让你能在几秒内定位到哪个服务、哪个操作、哪个数据库查询造成了慢请求或错误。

这不是"锦上添花"的功能，而是微服务架构下排障的基础设施。没有链路追踪，复杂故障的 MTTR（平均修复时间）往往以小时计；有了它，通常能压缩到分钟级。

---

## 核心概念

在看具体工具之前，先把基础概念统一一下。这些概念在 Jaeger 和 Tempo 里完全通用，因为它们都遵循 OpenTelemetry 规范。

### Trace 与 Span

**Trace** 代表一次完整的请求链路，由一个全局唯一的 `TraceID`（128-bit）标识。整个 Trace 是一棵树，根节点是入口请求，每个节点是一个 **Span**。

**Span** 是链路追踪的最小单元，代表一段有时间跨度的操作。每个 Span 包含：

- `SpanID`：本 Span 的唯一标识（64-bit）
- `ParentSpanID`：父 Span 的 ID，根 Span 没有父节点
- `TraceID`：所属 Trace 的 ID
- `Name`：操作名称，如 `HTTP GET /api/orders`、`SELECT orders`
- `StartTime` / `EndTime`：开始和结束时间（纳秒精度）
- `Status`：OK / ERROR / UNSET
- `Attributes`：键值对形式的附加信息，如 `http.status_code=200`、`db.statement=SELECT ...`
- `Events`：Span 生命周期内的时间点事件，如异常堆栈
- `Links`：指向其他 Span 的引用，用于关联异步消息等场景

### Context Propagation（上下文传播）

链路追踪的本质挑战在于：服务 A 调用服务 B 时，如何把 `TraceID` 和 `SpanID` 传递过去，让 B 创建的 Span 知道自己是 A 的子节点？

这依靠**上下文传播**机制。传播方式分两种：

**进程内传播**：通过语言的上下文机制传递，Go 里是 `context.Context`，Java 里是 ThreadLocal，Python 里是 `contextvars`。SDK 自动处理，业务代码只需正确传递 `ctx`。

**跨进程传播**：通过 HTTP Header 或消息队列的 Header 传递。OpenTelemetry 默认用 **W3C TraceContext** 规范，Header 格式为：

```
traceparent: 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01
              版本 ---- TraceID(32hex) ----------- SpanID(16hex) ----- 采样标志
```

另一个常见格式是 Jaeger 原生的 `uber-trace-id`，但现在推荐统一用 W3C 标准。

### 采样（Sampling）

每个请求都完整记录链路数据在高流量场景下成本极高。**采样**决定哪些请求的 Trace 需要保留。

**头采样（Head-based Sampling）**：在请求刚进入系统时就决定是否采样，决策依据只有 `TraceID`（随机性）。优点是实现简单、开销低；缺点是无法基于请求结果决策，错误率低时可能把大量错误 Trace 丢掉。

**尾采样（Tail-based Sampling）**：等整个 Trace 完成后再决定是否保留。可以基于 Trace 的结果（是否有错误、延迟是否超阈值）做决策，保留所有错误 Trace，对正常请求按比例采样。缺点是需要在 Collector 层缓存所有进行中的 Span，内存压力大。

---

## Jaeger 架构解析

Jaeger 是 Uber 开源的分布式追踪系统，2017 年捐给 CNCF，现已是 CNCF 毕业项目。

### 组件拆解

**Jaeger Agent**（已逐步废弃）：以 DaemonSet 或 Sidecar 方式部署在每个节点，接收应用发来的 UDP Span 数据，批量转发给 Collector。在新版本中，推荐直接用 OTel Collector 替代。

**Jaeger Collector**：核心处理组件，接收 Span 数据（支持 Jaeger Thrift、OTLP、Zipkin 格式），做基本校验和处理后写入存储后端。无状态，可水平扩展。

**Storage Backend**：Jaeger 支持多种存储后端：
- **Elasticsearch / OpenSearch**：生产首选，支持全文搜索和复杂过滤，但运维成本高
- **Cassandra**：时序写入性能好，但查询能力弱，适合超大规模场景
- **Badger**：内嵌 KV 存储，仅用于单机开发环境
- **Kafka**（作为缓冲层）：Collector → Kafka → Ingester → 存储，解耦写入压力

**Jaeger Query**：提供查询 API 和 Jaeger UI，支持按 Service、Operation、Tags、时间范围查询 Trace。

**Jaeger UI**：独立的 Web 界面，可视化 Trace 瀑布图，支持 Span 比较、依赖关系图（DAG）。

### Jaeger 的数据模型

Jaeger 使用自己的数据模型，后来逐渐兼容 OTLP。存储在 Elasticsearch 时，每个 Span 是一个文档，包含 `traceID`、`spanID`、`operationName`、`duration` 等字段。

查询时支持：按服务+操作名过滤、按标签（Tag）过滤、按时间范围和 duration 范围过滤。但**不支持聚合查询**，无法直接回答"过去 1 小时哪个操作的 P99 最高"这类问题。

---

## Tempo 架构解析

Grafana Tempo 是 Grafana Labs 在 2020 年开源的分布式追踪后端，设计目标是**极低成本的大规模 Trace 存储**。

### 设计哲学

Tempo 的核心设计理念与 Jaeger 完全不同：**Trace 数据只按 TraceID 检索，不建索引**。

这听起来像是退步，但背后有深刻的权衡：
- Trace 数据量极大，每个请求产生数十个 Span，每天可能有几十亿条记录
- 如果对所有属性建索引（像 Elasticsearch 那样），存储成本乘以 3-5 倍
- 实际使用中，大多数 Trace 查询的起点是从 Metrics 或 Logs 中找到异常的 TraceID，然后直接用 TraceID 拉取 Trace 详情

所以 Tempo 的查询路径是：**先通过 Metrics/Logs 发现问题 → 拿到 TraceID → 直接查 Tempo**。

### 组件架构

```
应用 / OTel Collector
        ↓ OTLP gRPC/HTTP
    Tempo Distributor（接收、路由）
        ↓
    Tempo Ingester（内存缓存，WAL）
        ↓
    对象存储（S3 / GCS / Azure Blob）
        ↑
    Tempo Compactor（合并、压缩数据块）
        ↑
    Tempo Querier（查询，从对象存储读）
        ↑
    Tempo Query Frontend（查询入口，分片）
```

**Distributor**：接收 Span，按 TraceID 做一致性哈希路由到对应 Ingester，确保同一 Trace 的所有 Span 落到同一个 Ingester。

**Ingester**：内存中缓存活跃 Trace，同时写 WAL（Write-Ahead Log）防止数据丢失。达到 block 大小或时间阈值后，将数据块刷写到对象存储。

**Compactor**：后台任务，合并小数据块、删除过期数据，维护对象存储的布隆过滤器索引（用于快速判断 TraceID 是否存在于某个数据块）。

**Querier**：处理具体查询，先查布隆过滤器定位数据块，再从对象存储拉取数据，解析后返回结果。

**存储后端**：S3、GCS、Azure Blob Storage，按实际存储量计费，成本比 Elasticsearch 低 5-10 倍。

### TraceQL

Tempo 2.0 引入了 **TraceQL**，专门为 Trace 数据设计的查询语言，补足了早期"只能按 TraceID 查"的短板。

TraceQL 的基本语法：

```
# 查找包含错误 Span 的所有 Trace
{ status = error }

# 查找特定服务的慢请求（duration > 500ms）
{ resource.service.name = "order-service" && duration > 500ms }

# 查找特定 HTTP 路径的错误
{ span.http.url =~ ".*\/api\/orders.*" && status = error }

# 聚合：按服务统计错误 Trace 数量（TraceQL metrics，需 Tempo 2.3+）
{ status = error } | by(resource.service.name) | rate()

# 查找 span duration 超过 1 秒且包含数据库操作的 Trace
{ span.db.system != "" && duration > 1s }
```

TraceQL 支持的过滤维度：
- `resource.*`：资源属性，如 `resource.service.name`、`resource.k8s.pod.name`
- `span.*`：Span 属性，如 `span.http.status_code`、`span.db.statement`
- `duration`：Span 持续时间
- `status`：ok / error / unset
- `name`：Span 名称
- `traceDuration`：整个 Trace 的总耗时（用于根 Span 过滤）

---

## Jaeger vs Tempo 选型对比

### 存储成本

这是两者最大的差异。

Jaeger 使用 Elasticsearch，需要维护 ES 集群：
- 3 节点 ES 集群（16C32G × 3）+ 热温冷分层，大约每天处理 1000 万 Span 需要 500GB+ 存储
- ES 存储成本（AWS EBS gp3）：$0.08/GB/月，加上计算成本，每月约 $800-1500
- 运维成本：索引管理、分片调整、Mapping 变更都需要人工干预

Tempo 使用 S3：
- 同等数据量存储到 S3（开启压缩后约 100-200GB），成本约 $3-6/月
- 无需维护存储集群，Compactor 自动管理数据生命周期
- 查询时从 S3 拉取数据，延迟略高（首次查询 2-5s），但可接受

**结论**：存储成本 Tempo 比 Jaeger 低 95%+，这是压倒性的优势。

### 查询能力

**Jaeger**：
- 支持多维度过滤（Service、Operation、Tags、Duration、时间范围）
- 不支持聚合，无法做 Trace 层面的统计分析
- 查询基于 Elasticsearch 索引，响应快（< 1s）
- 支持服务依赖图（从 Span 关系自动生成）

**Tempo**：
- TraceQL 支持复杂过滤和聚合（2.0+ 版本）
- 早期版本只能按 TraceID 查询，2.0 后补齐了过滤能力
- TraceQL metrics 可以直接从 Trace 数据生成 Rate/Error/Duration 指标
- 首次查询略慢（需从 S3 读数据）

**结论**：Tempo 2.3+ 的 TraceQL 已经基本追平 Jaeger 的查询能力，在聚合分析方面甚至更强。

### Grafana 集成度

**Jaeger**：有独立 UI（jaeger-query），Grafana 通过 Data Source 插件接入，功能是 Jaeger UI 的子集。Trace 和 Metrics/Logs 的关联需要额外配置 Exemplar 或手动跳转。

**Tempo**：Grafana 原生数据源，与 Loki、Prometheus 的联动是一等公民：
- Loki 日志中的 TraceID 可以直接点击跳转到 Tempo Trace 详情
- Prometheus Exemplar 携带 TraceID，在 Grafana 指标图中可以直接跳转 Trace
- Grafana Explore 的 Trace 视图与 Tempo 深度集成

**结论**：如果已经在用 Grafana 栈，Tempo 的集成体验远优于 Jaeger。

### 社区与维护

**Jaeger**：CNCF 毕业项目，社区活跃，维护稳定。但近期迭代速度放缓，新功能以兼容 OTLP 为主，核心架构变化不大。

**Tempo**：Grafana Labs 主导，迭代速度快，每个季度有重要功能发布（TraceQL metrics、流式查询等）。与 Grafana/Loki/Mimir 生态深度绑定。

### 选型建议

| 场景 | 推荐 |
|------|------|
| 已有 Grafana + Loki + Prometheus 栈 | Tempo |
| 需要极致查询响应速度（< 500ms） | Jaeger（ES 后端） |
| 成本敏感，Trace 量大 | Tempo |
| 团队已熟悉 Elasticsearch 运维 | Jaeger 也可，但成本高 |
| 需要强大的服务依赖拓扑图 | Jaeger 原生更成熟 |
| 新项目从零搭建 | Tempo + Grafana |

---

## Tempo + Grafana 部署实战

### 前置条件

- Kubernetes 集群（1.24+）
- Helm 3.x
- S3 Bucket（以 AWS 为例）
- 已部署 Grafana（kube-prometheus-stack 或单独部署）

### 部署 Tempo（Helm）

添加 Grafana Helm 仓库：

```bash
helm repo add grafana https://grafana.github.io/helm-charts
helm repo update
```

创建 `values-tempo.yaml`：

```yaml
# values-tempo.yaml
tempo:
  storage:
    trace:
      backend: s3
      s3:
        bucket: my-tempo-traces
        endpoint: s3.us-west-2.amazonaws.com
        region: us-west-2
        # 使用 IRSA（推荐）或 accessKey/secretKey
        # access_key: ""
        # secret_key: ""

  # 保留 7 天数据
  retention: 168h

  # 接收端口配置
  receivers:
    otlp:
      protocols:
        grpc:
          endpoint: "0.0.0.0:4317"
        http:
          endpoint: "0.0.0.0:4318"
    jaeger:
      protocols:
        grpc:
          endpoint: "0.0.0.0:14250"

  # 启用 TraceQL metrics（实验性）
  metricsGenerator:
    enabled: true
    remoteWriteUrl: "http://prometheus-operated:9090/api/v1/write"
    ingestion:
      tenantId: anonymous
    storage:
      path: /var/tempo/wal
      remote_write_flush_deadline: 1m
    traces_storage:
      path: /var/tempo/traces

  # Ingester 配置
  ingester:
    max_block_duration: 5m
    trace_idle_period: 10s
    flush_check_period: 10s

  # 查询配置
  querier:
    max_concurrent_queries: 20
    search:
      prefer_self: 10

  # 全局采样配置（概率采样）
  distributor:
    receivers:
      otlp:
        protocols:
          grpc: {}
          http: {}

# 持久化 WAL
persistence:
  enabled: true
  size: 10Gi
  storageClassName: gp3

# 资源限制
resources:
  requests:
    cpu: 500m
    memory: 1Gi
  limits:
    cpu: 2
    memory: 4Gi

serviceMonitor:
  enabled: true
```

部署：

```bash
kubectl create namespace observability

# 如果用 IRSA，需要给 ServiceAccount 打 annotation
helm install tempo grafana/tempo-distributed \
  -n observability \
  -f values-tempo.yaml
```

验证部署：

```bash
kubectl get pods -n observability | grep tempo
# tempo-compactor-xxx         1/1  Running
# tempo-distributor-xxx       1/1  Running
# tempo-ingester-0            1/1  Running
# tempo-querier-xxx           1/1  Running
# tempo-query-frontend-xxx    1/1  Running
```

### 在 Grafana 中配置 Tempo Data Source

在 Grafana UI 或 ConfigMap 中配置：

```yaml
# grafana-datasources.yaml（通过 ConfigMap 注入）
apiVersion: v1
kind: ConfigMap
metadata:
  name: grafana-datasources
  namespace: observability
data:
  datasources.yaml: |
    apiVersion: 1
    datasources:
      - name: Tempo
        type: tempo
        url: http://tempo-query-frontend.observability:3100
        jsonData:
          tracesToLogsV2:
            datasourceUid: loki
            spanStartTimeShift: "-1m"
            spanEndTimeShift: "1m"
            filterByTraceID: true
            filterBySpanID: false
            customQuery: true
            query: '{namespace="${__span.tags.k8s.namespace.name}"} |= "${__trace.traceId}"'
          tracesToMetrics:
            datasourceUid: prometheus
            queries:
              - name: "Request Rate"
                query: 'rate(http_requests_total{service="${__span.tags.service.name}"}[5m])'
          serviceMap:
            datasourceUid: prometheus
          nodeGraph:
            enabled: true
          search:
            hide: false
          lokiSearch:
            datasourceUid: loki
```

### TraceQL 实战查询示例

在 Grafana Explore → Tempo 中使用 TraceQL：

```
# 1. 查找 order-service 最近 1 小时的错误 Trace
{ resource.service.name = "order-service" && status = error }

# 2. 查找 HTTP 5xx 错误
{ span.http.status_code >= 500 }

# 3. 查找 P99 > 2s 的数据库操作
{ span.db.system != "" } | select(duration) | duration > 2s

# 4. 查找特定 TraceID（直接输入）
{ traceId = "4bf92f3577b34da6a3ce929d0e0e4736" }

# 5. 跨服务慢链路：整个 Trace 耗时超过 5 秒
{ traceDuration > 5s }

# 6. 查找包含重试行为的 Trace（同一 operation 出现多次）
{ span.http.url =~ ".*/retry.*" }
```

---

## OpenTelemetry SDK 插桩实战

### Go 应用插桩

**自动插桩**（通过 HTTP/gRPC 中间件）：

```go
package main

import (
    "context"
    "log"
    "net/http"

    "go.opentelemetry.io/otel"
    "go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc"
    "go.opentelemetry.io/otel/propagation"
    "go.opentelemetry.io/otel/sdk/resource"
    sdktrace "go.opentelemetry.io/otel/sdk/trace"
    semconv "go.opentelemetry.io/otel/semconv/v1.21.0"
    "go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"
)

func initTracer(ctx context.Context) (*sdktrace.TracerProvider, error) {
    // 创建 OTLP gRPC exporter，发送到 OTel Collector 或直接发 Tempo
    exporter, err := otlptracegrpc.New(ctx,
        otlptracegrpc.WithEndpoint("otel-collector:4317"),
        otlptracegrpc.WithInsecure(),
    )
    if err != nil {
        return nil, err
    }

    // 资源属性：标识这个服务
    res, err := resource.New(ctx,
        resource.WithAttributes(
            semconv.ServiceName("order-service"),
            semconv.ServiceVersion("v1.2.3"),
            semconv.DeploymentEnvironment("production"),
        ),
        resource.WithFromEnv(),   // 支持 OTEL_RESOURCE_ATTRIBUTES 环境变量
        resource.WithProcess(),   // 自动添加进程信息
        resource.WithOS(),
        resource.WithContainer(), // 自动添加容器/Pod 信息
        resource.WithHost(),
    )
    if err != nil {
        return nil, err
    }

    // TracerProvider：采样策略用概率采样 10%，错误全量保留
    tp := sdktrace.NewTracerProvider(
        sdktrace.WithBatcher(exporter),
        sdktrace.WithResource(res),
        sdktrace.WithSampler(
            sdktrace.ParentBased(
                sdktrace.TraceIDRatioBased(0.1), // 10% 采样
            ),
        ),
    )

    // 设置全局 TracerProvider 和 Propagator
    otel.SetTracerProvider(tp)
    otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(
        propagation.TraceContext{}, // W3C TraceContext
        propagation.Baggage{},
    ))

    return tp, nil
}

func main() {
    ctx := context.Background()

    tp, err := initTracer(ctx)
    if err != nil {
        log.Fatal(err)
    }
    defer tp.Shutdown(ctx)

    // otelhttp 自动为每个请求创建 Span，传播上下文
    mux := http.NewServeMux()
    mux.HandleFunc("/api/orders", handleOrders)

    handler := otelhttp.NewHandler(mux, "order-service",
        otelhttp.WithMessageEvents(otelhttp.ReadEvents, otelhttp.WriteEvents),
    )

    log.Fatal(http.ListenAndServe(":8080", handler))
}
```

**手动插桩**（在关键逻辑处创建子 Span）：

```go
package service

import (
    "context"
    "fmt"

    "go.opentelemetry.io/otel"
    "go.opentelemetry.io/otel/attribute"
    "go.opentelemetry.io/otel/codes"
    semconv "go.opentelemetry.io/otel/semconv/v1.21.0"
)

var tracer = otel.Tracer("order-service/service")

func (s *OrderService) CreateOrder(ctx context.Context, userID int64, items []Item) (*Order, error) {
    // 创建子 Span
    ctx, span := tracer.Start(ctx, "OrderService.CreateOrder")
    defer span.End()

    // 添加业务属性
    span.SetAttributes(
        attribute.Int64("user.id", userID),
        attribute.Int("order.items_count", len(items)),
    )

    // 校验库存
    if err := s.checkInventory(ctx, items); err != nil {
        // 记录错误并设置 Span 状态
        span.RecordError(err)
        span.SetStatus(codes.Error, "inventory check failed")
        return nil, fmt.Errorf("inventory check: %w", err)
    }

    // 写数据库（数据库 SDK 通常自动插桩，这里演示手动）
    ctx, dbSpan := tracer.Start(ctx, "db.insert_order")
    dbSpan.SetAttributes(
        semconv.DBSystem("mysql"),
        semconv.DBName("orders"),
        semconv.DBOperation("INSERT"),
        semconv.DBStatement("INSERT INTO orders (user_id, status) VALUES (?, ?)"),
    )

    order, err := s.repo.Insert(ctx, userID, items)
    if err != nil {
        dbSpan.RecordError(err)
        dbSpan.SetStatus(codes.Error, err.Error())
        dbSpan.End()
        span.SetStatus(codes.Error, "db insert failed")
        return nil, err
    }
    dbSpan.SetAttributes(attribute.Int64("order.id", order.ID))
    dbSpan.End()

    // 发送 MQ 消息（添加关键事件）
    span.AddEvent("order_created", trace.WithAttributes(
        attribute.Int64("order.id", order.ID),
    ))

    return order, nil
}
```

### Python 应用插桩

**自动插桩**（Flask + SQLAlchemy）：

```python
# requirements.txt
# opentelemetry-sdk
# opentelemetry-exporter-otlp-proto-grpc
# opentelemetry-instrumentation-flask
# opentelemetry-instrumentation-sqlalchemy
# opentelemetry-instrumentation-requests

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource, SERVICE_NAME, SERVICE_VERSION
from opentelemetry.instrumentation.flask import FlaskInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.propagate import set_global_textmap
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.propagators.b3 import B3Format
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

def init_tracing(app, db_engine):
    resource = Resource.create({
        SERVICE_NAME: "payment-service",
        SERVICE_VERSION: "2.1.0",
        "deployment.environment": "production",
    })

    exporter = OTLPSpanExporter(
        endpoint="http://otel-collector:4317",
        insecure=True,
    )

    provider = TracerProvider(
        resource=resource,
        # 尾采样由 OTel Collector 处理，这里 100% 发送到 Collector
        # sampler=TraceIdRatioBased(0.1),  # 如果在应用层头采样
    )
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    set_global_textmap(TraceContextTextMapPropagator())

    # 自动插桩 Flask
    FlaskInstrumentor().instrument_app(app)

    # 自动插桩 SQLAlchemy（自动记录所有 SQL 语句）
    SQLAlchemyInstrumentor().instrument(engine=db_engine)

    # 自动插桩 requests 库（HTTP 出口请求）
    RequestsInstrumentor().instrument()
```

**手动插桩**（业务逻辑层）：

```python
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

tracer = trace.get_tracer("payment-service.payment")

def process_payment(user_id: int, amount: float, currency: str) -> dict:
    with tracer.start_as_current_span("PaymentService.process") as span:
        span.set_attribute("user.id", user_id)
        span.set_attribute("payment.amount", amount)
        span.set_attribute("payment.currency", currency)

        try:
            # 调用风控服务
            with tracer.start_as_current_span("risk.check") as risk_span:
                risk_result = risk_client.check(user_id, amount)
                risk_span.set_attribute("risk.score", risk_result.score)
                risk_span.set_attribute("risk.decision", risk_result.decision)

            if risk_result.decision == "REJECT":
                span.set_status(Status(StatusCode.ERROR, "payment rejected by risk"))
                span.set_attribute("payment.status", "rejected")
                return {"status": "rejected", "reason": "risk_control"}

            # 调用支付网关
            with tracer.start_as_current_span("gateway.charge") as gw_span:
                gw_span.set_attribute("gateway.provider", "stripe")
                result = stripe_client.charge(amount, currency)
                gw_span.set_attribute("gateway.transaction_id", result.transaction_id)

            span.add_event("payment_completed", {
                "transaction_id": result.transaction_id,
            })
            return {"status": "success", "transaction_id": result.transaction_id}

        except Exception as e:
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
            raise
```

---

## 采样策略深度配置

### OTel Collector 尾采样

在 OTel Collector 配置尾采样处理器（`tail_sampling`）：

```yaml
# otelcol-config.yaml
processors:
  tail_sampling:
    decision_wait: 30s          # 等待 Trace 完成的最长时间
    num_traces: 100000          # 内存中缓存的最大 Trace 数量
    expected_new_traces_per_sec: 10000
    policies:
      # 策略1：所有错误 Trace 全量保留
      - name: errors-policy
        type: status_code
        status_code:
          status_codes: [ERROR]

      # 策略2：慢请求保留（> 1 秒）
      - name: slow-traces-policy
        type: latency
        latency:
          threshold_ms: 1000

      # 策略3：正常请求 5% 概率采样
      - name: probabilistic-policy
        type: probabilistic
        probabilistic:
          sampling_percentage: 5

      # 策略4：特定服务全量保留（如支付服务）
      - name: payment-service-policy
        type: string_attribute
        string_attribute:
          key: service.name
          values: ["payment-service"]
          enabled_regex_matching: false

    # 组合策略：满足任一条件即保留
    composite:
      max_total_spans_per_second: 50000
      policy_order:
        - errors-policy
        - slow-traces-policy
        - payment-service-policy
        - probabilistic-policy
```

### Tempo 概率采样配置

Tempo 本身不做采样决策，采样在应用层或 Collector 层处理。但可以通过 `metrics_generator` 控制哪些 Trace 生成 RED 指标：

```yaml
# tempo values.yaml
tempo:
  metricsGenerator:
    enabled: true
    processor:
      service_graphs:
        enabled: true
        # 只为采样率内的 Trace 生成 service graph
        max_items: 10000
        wait: 10s
      span_metrics:
        enabled: true
        # 生成 traces_spanmetrics_* 指标
        histogram_buckets:
          - 0.002
          - 0.004
          - 0.008
          - 0.016
          - 0.032
          - 0.064
          - 0.128
          - 0.256
          - 0.512
          - 1.024
          - 2.048
```

---

## Traces 与 Metrics/Logs 的关联

### Exemplar（指标关联 Trace）

Exemplar 是 Prometheus 的扩展，允许在指标数据点上携带 TraceID，实现"从指标异常点直接跳转到对应 Trace"。

在 Go 中配置 Exemplar：

```go
import (
    "github.com/prometheus/client_golang/prometheus"
    "go.opentelemetry.io/otel/trace"
)

var httpDuration = prometheus.NewHistogramVec(
    prometheus.HistogramOpts{
        Name:    "http_request_duration_seconds",
        Help:    "HTTP request duration",
        Buckets: prometheus.DefBuckets,
    },
    []string{"method", "path", "status"},
)

func recordWithExemplar(ctx context.Context, method, path, status string, duration float64) {
    spanCtx := trace.SpanFromContext(ctx).SpanContext()
    if spanCtx.IsValid() {
        httpDuration.WithLabelValues(method, path, status).(prometheus.ExemplarObserver).ObserveWithExemplar(
            duration,
            prometheus.Labels{
                "traceID": spanCtx.TraceID().String(),
                "spanID":  spanCtx.SpanID().String(),
            },
        )
    } else {
        httpDuration.WithLabelValues(method, path, status).Observe(duration)
    }
}
```

Prometheus 配置启用 Exemplar 存储：

```yaml
# prometheus.yaml
global:
  scrape_interval: 15s

# 启用 Exemplar 存储（需要 Prometheus 2.43+）
storage:
  exemplars:
    max_exemplars: 100000
```

### Loki 日志关联 Trace

在日志中注入 TraceID，Grafana 自动识别并提供跳转链接。

Go 中通过 zap 日志注入 TraceID：

```go
import (
    "go.opentelemetry.io/otel/trace"
    "go.uber.org/zap"
)

func logWithTrace(ctx context.Context, logger *zap.Logger, msg string, fields ...zap.Field) {
    spanCtx := trace.SpanFromContext(ctx).SpanContext()
    if spanCtx.IsValid() {
        fields = append(fields,
            zap.String("traceID", spanCtx.TraceID().String()),
            zap.String("spanID", spanCtx.SpanID().String()),
        )
    }
    logger.Info(msg, fields...)
}
```

在 Grafana Loki Data Source 配置中启用 TraceID 识别：

```yaml
datasources:
  - name: Loki
    type: loki
    url: http://loki:3100
    jsonData:
      derivedFields:
        - datasourceName: Tempo
          datasourceUid: tempo-uid
          matcherRegex: '"traceID":"(\w+)"'
          name: TraceID
          url: "$${__value.raw}"
```

---

## 生产运维

### 存储调优

Tempo 在 S3 上的数据组织方式是按时间分桶的数据块（block）。调优要点：

```yaml
# tempo 存储配置
tempo:
  storage:
    trace:
      backend: s3
      block:
        bloom_filter_false_positive: 0.01    # 布隆过滤器假阳率，越低占用空间越大
        bloom_filter_shard_size_bytes: 100000 # 每个 shard 大小
        v2_encoding: zstd                    # 压缩算法，zstd 压缩比高
        v2_index_downsample_bytes: 1000000   # 索引采样间隔
      wal:
        encoding: snappy                     # WAL 压缩
      pool:
        max_workers: 100                     # 并发读取 S3 的 worker 数
        queue_depth: 10000

  compactor:
    compaction:
      block_retention: 720h                  # 数据保留 30 天
      compacted_block_retention: 1h          # 已合并的旧块保留 1 小时
      compaction_window: 1h                  # 合并窗口
      max_block_bytes: 107374182400          # 单块最大 100GB
      max_compaction_objects: 6000000        # 单次合并最大对象数
      retention_concurrency: 10
```

### 高基数问题

Span Attributes 中使用用户 ID、请求 ID 等高基数值会导致存储膨胀和查询变慢。

常见问题模式：

```go
// 错误：把高基数值放到 Span Name
span.SetName(fmt.Sprintf("GET /api/orders/%d", orderID)) // ❌

// 正确：Span Name 用固定模板，高基数值用 Attribute
span.SetName("GET /api/orders/:id")  // ✓
span.SetAttributes(attribute.Int64("order.id", orderID))  // ✓

// 错误：SQL 语句直接插值（高基数 + 安全风险）
span.SetAttributes(attribute.String("db.statement",
    fmt.Sprintf("SELECT * FROM orders WHERE id = %d", orderID))) // ❌

// 正确：使用参数化 SQL
span.SetAttributes(attribute.String("db.statement",
    "SELECT * FROM orders WHERE id = ?"))  // ✓
```

### 采样率动态调整

通过 OTel Collector 的 OTTL（OpenTelemetry Transformation Language）动态过滤：

```yaml
processors:
  filter/drop_health_checks:
    error_mode: ignore
    traces:
      span:
        # 丢弃健康检查请求
        - 'attributes["http.url"] == "/health"'
        - 'attributes["http.url"] == "/metrics"'
        - 'attributes["http.url"] == "/readyz"'
```

---

## 基于 Trace 错误率的告警规则

Tempo metricsGenerator 会自动生成 `traces_spanmetrics_*` 系列指标，可以直接用于告警。

```yaml
# prometheus-rules.yaml
groups:
  - name: tracing.rules
    rules:
      # 告警：服务错误率超过 1%
      - alert: ServiceHighErrorRate
        expr: |
          (
            sum by (service_name) (
              rate(traces_spanmetrics_calls_total{status_code="STATUS_CODE_ERROR"}[5m])
            )
            /
            sum by (service_name) (
              rate(traces_spanmetrics_calls_total[5m])
            )
          ) > 0.01
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "服务 {{ $labels.service_name }} 错误率过高"
          description: "过去 5 分钟错误率为 {{ $value | humanizePercentage }}，超过 1% 阈值"

      # 告警：P99 延迟超过 2 秒
      - alert: ServiceHighLatency
        expr: |
          histogram_quantile(0.99,
            sum by (service_name, le) (
              rate(traces_spanmetrics_latency_bucket[5m])
            )
          ) > 2
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "服务 {{ $labels.service_name }} P99 延迟过高"
          description: "P99 延迟为 {{ $value | humanizeDuration }}，超过 2s 阈值"

      # 告警：Tempo Ingester 写入失败
      - alert: TempoIngesterWriteFailures
        expr: |
          rate(tempo_ingester_traces_created_total{err!=""}[5m]) > 0
        for: 2m
        labels:
          severity: critical
        annotations:
          summary: "Tempo Ingester 写入错误"
          description: "Ingester 出现写入失败，可能丢失 Trace 数据"

      # 告警：OTel Collector 丢弃 Span
      - alert: OtelCollectorSpanDropped
        expr: |
          rate(otelcol_processor_dropped_spans_total[5m]) > 100
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "OTel Collector 正在丢弃 Span"
          description: "每秒丢弃 {{ $value }} 个 Span，检查 Collector 容量或下游连接"
```

---

## 踩坑记录

**问题1：Tempo Ingester OOM**

症状：Ingester Pod 反复 OOM 重启。原因是 `trace_idle_period` 设置太长（默认 30s），大量活跃 Trace 积压在内存。

解决：缩短 `trace_idle_period` 到 10s，调大 Ingester 内存 limit，同时检查是否有异常大的 Trace（循环调用导致 Span 数量爆炸）。

**问题2：Trace 数据不完整（丢 Span）**

症状：部分 Trace 只有几个 Span，缺少下游服务的记录。原因是尾采样时，不同 Span 的 exporter 发到了不同的 Collector 实例，而尾采样需要同一 Trace 的所有 Span 在同一实例。

解决：在 Collector 前加一层 LoadBalancer Exporter，按 TraceID 做一致性哈希路由：

```yaml
exporters:
  loadbalancing:
    protocol:
      otlp:
        tls:
          insecure: true
    resolver:
      dns:
        hostname: otelcol-tail-sampler-headless.observability
        port: 4317
```

**问题3：W3C TraceContext 与 Jaeger Header 不兼容**

症状：混合部署时（部分服务还在用 Jaeger SDK），Trace 在服务边界断裂。

解决：在 OTel SDK 和 Jaeger SDK 的服务边界配置双 Propagator，同时支持 `traceparent` 和 `uber-trace-id`：

```go
otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(
    propagation.TraceContext{},
    propagation.Baggage{},
    jaegerprop.Jaeger{}, // 兼容 Jaeger header
))
```

---

## 总结

Jaeger 与 Tempo 的选型不是非此即彼，而是看你的技术栈和优先级：

- **如果你的可观测性栈已经是 Grafana + Loki + Prometheus**，Tempo 是自然的选择，存储成本低、集成无缝、TraceQL 够用。
- **如果你需要独立的追踪 UI、复杂的标签搜索、或已有 Elasticsearch 集群**，Jaeger 也是成熟可靠的选择。

无论选哪个，都应该通过 **OpenTelemetry SDK** 插桩，这样未来切换后端只需改 Exporter 配置，不需要改应用代码。采样策略推荐在 OTel Collector 层做尾采样，保留所有错误 Trace 和慢 Trace，对正常请求按 5-10% 采样，这是成本和可观测性的最佳平衡点。
