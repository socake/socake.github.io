---
title: "Grafana Tempo 大规模分布式追踪实战：从 OTel 接入到 TraceQL 调优"
date: 2025-07-16T10:00:00+08:00
draft: false
tags: ["Tempo", "Tracing", "可观测性", "OpenTelemetry", "TraceQL"]
categories: ["可观测性"]
description: "Tempo 2.x 从零到每日 30 亿 span 的生产部署笔记。架构剖析、OTel Collector 接入、对象存储与索引、TraceQL 查询优化、tail sampling、metrics generator、和 Loki/Pyroscope 的联动。"
summary: "Tempo 是目前最便宜的分布式追踪后端。本文把架构、接入、TraceQL、tail sampling、成本优化、事故案例都串起来，供团队直接抄作业。"
toc: true
math: false
diagram: false
keywords: ["Tempo", "OpenTelemetry", "TraceQL", "分布式追踪", "Tail sampling"]
params:
  reading_time: true
---

## 为什么最后选了 Tempo

过去两年我在两个团队分别推过追踪后端，第一次选了 Jaeger + Elasticsearch，第二次选了 Tempo。两套对比下来，Tempo 在大规模场景下的运维成本几乎只有 Jaeger 的五分之一，核心原因是它做了一件反直觉的事：**不建倒排索引**。

传统追踪后端（Jaeger、Zipkin、SkyWalking）都要对每个 span 的 tag 建倒排索引，这样才能支持「查所有 duration > 500ms 且 http.status_code=500 的 trace」。Tempo 最早（1.x）走极端路线：只按 trace_id 建索引，查询全靠 trace_id 精确定位；trace_id 之外的过滤靠 Grafana 从 Loki 查日志拿到 trace_id 再回 Tempo 查 trace。这样存储成本极低，但功能也弱。

2.x 开始，Tempo 引入了 **Parquet block 格式** 和 **TraceQL** 语言：block 里带 span 的列式存储，查询时对 Parquet 做全表扫描但是列裁剪，过滤能力补上来了。加上 tail sampling 和 metrics generator，基本覆盖了 Jaeger 的主要场景。现在（2.6 / 2.7）我们生产日均 30 亿 span，对象存储 18TB/月，成本大约是 Elasticsearch 方案的 1/10。

这篇文章按生产落地的顺序讲清楚 Tempo 的方方面面。

## 一、架构总览

组件和 Mimir、Loki 家族类似：

```
Apps / SDK / OTel Agent
        │  OTLP / Jaeger / Zipkin
        ▼
Distributor (无状态)
        │ hash ring (trace_id)
        ▼
Ingester (有状态, RF=3)
        │  2h block -> upload
        ▼
Object Storage (S3/GCS/OSS)
        ▲
Querier ─▶ Store Gateway (可选)
        ▲
Query Frontend
        ▲
  Grafana UI
```

区别于 Loki：

1. **Compactor** 相对简单，只做 block 合并，不做 retention（retention 由 compactor + bucket lifecycle 协作）；
2. **Metrics Generator** 是 Tempo 独有的组件，基于 span 数据生成 RED metrics 和 service graph，推给 Prometheus/Mimir；
3. **Store Gateway** 从 2.1 开始可选，默认查询直接访问对象存储。小规模不用，大规模强烈推荐。

## 二、一条 span 的旅程

应用用 OpenTelemetry SDK 埋点：

```go
import (
    "go.opentelemetry.io/otel"
    "go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc"
    "go.opentelemetry.io/otel/sdk/trace"
    "go.opentelemetry.io/otel/sdk/resource"
    semconv "go.opentelemetry.io/otel/semconv/v1.21.0"
)

func initTracer(ctx context.Context) func() {
    exp, _ := otlptracegrpc.New(ctx,
        otlptracegrpc.WithEndpoint("otel-collector.obs.svc:4317"),
        otlptracegrpc.WithInsecure())

    tp := trace.NewTracerProvider(
        trace.WithBatcher(exp),
        trace.WithSampler(trace.ParentBased(trace.TraceIDRatioBased(0.05))),
        trace.WithResource(resource.NewWithAttributes(
            semconv.SchemaURL,
            semconv.ServiceName("order-service"),
            semconv.ServiceVersion(os.Getenv("APP_VERSION")),
            attribute.String("deployment.environment", "prod"),
        )),
    )
    otel.SetTracerProvider(tp)
    return func() { _ = tp.Shutdown(ctx) }
}
```

SDK 把 span 通过 OTLP/gRPC 发给 OTel Collector：

```yaml
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
      http:
        endpoint: 0.0.0.0:4318

processors:
  batch:
    timeout: 1s
    send_batch_size: 8192
  memory_limiter:
    check_interval: 1s
    limit_percentage: 80
    spike_limit_percentage: 25
  attributes/add_cluster:
    actions:
      - key: cluster
        value: prod-ap-southeast-1
        action: insert
  tail_sampling:
    decision_wait: 10s
    policies:
      - name: error-traces
        type: status_code
        status_code: {status_codes: [ERROR]}
      - name: slow-traces
        type: latency
        latency: {threshold_ms: 1000}
      - name: probabilistic
        type: probabilistic
        probabilistic: {sampling_percentage: 5}

exporters:
  otlp/tempo:
    endpoint: tempo-distributor.tempo.svc:4317
    tls:
      insecure: true
    sending_queue:
      enabled: true
      num_consumers: 20
      queue_size: 50000

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [memory_limiter, tail_sampling, attributes/add_cluster, batch]
      exporters: [otlp/tempo]
```

Tempo distributor 拿到 span 之后，按 `trace_id` 的前几位做哈希，打到 N 个 ingester（RF=3）。每个 ingester 在内存里按 trace_id 聚合同一条 trace 的所有 span，等待一个 "trace complete" 窗口（默认 10s 无新 span），然后把整个 trace 追加到当前 block。Block 默认每 10 分钟或达到大小阈值上传对象存储。

## 三、存储格式：Parquet 救了 Tempo

1.x 时代 Tempo 用自研的 "v2" block format：简单的 append-only 日志 + trace_id → offset 索引。查询时只能按 trace_id 精确命中，其他过滤靠 client 端脑补。

2.x 的 Parquet block 革命性改变了这件事。Block 结构：

```
<bucket>/<tenant>/<block_id>/
├── data.parquet             # 列式存储 span
├── bloom-0
├── bloom-1
├── index                    # trace_id -> row_group
├── meta.json                # 元数据
```

`data.parquet` 的 schema 大概长这样（简化）：

```
ResourceSpans {
  resource.attributes: map<string, string>
  scope.name: string
  scope.version: string
  spans: list<Span>
}
Span {
  trace_id: bytes
  span_id: bytes
  parent_span_id: bytes
  name: string
  kind: int
  start_time: int64
  end_time: int64
  status_code: int
  attributes: map<string, string>
  events: list<Event>
}
```

Parquet 的列式布局让 TraceQL 查询不需要加载所有列。例如 `{ .status = error }` 只读 `status_code` 这一列，跨 block 扫描速度非常快。加上 row_group 级的 bloom filter，trace_id 精确查询依然是毫秒级。

## 四、OTel Collector：最值得投入时间的组件

Tempo 的前端（也就是应用接入层）几乎一定会过 OTel Collector。Collector 是一个插件化的 pipeline，你要决定：

### 1. 部署模式

- **Agent 模式**：每个节点跑一个 DaemonSet，应用通过 `host.ip:4317` 上报。对应用友好，网络跳转少。缺点：tail sampling 只能看到一个节点的 trace，做不了全局决策。
- **Gateway 模式**：集中部署几个 Collector，应用跨网段推送。tail sampling 可以做全局决策，但需要有状态（同一 trace 要打到同一 gateway）。
- **Agent + Gateway 双层**：Agent 负责 batch、resource attribute enrichment，Gateway 负责 tail sampling、路由分发。大规模场景推荐。

我们生产用双层，Agent 以 DaemonSet 形式跑，Gateway 是独立 Deployment，每个 Gateway 副本对应一个 trace_id 分片。

### 2. Tail Sampling 的设计

Head sampling 的问题：应用上来就决定采不采，错误 trace 可能因为采样率低而错过。Tail sampling 的思路：先全量收集，等 trace 完整后再决定要不要采。

一个工业级 tail_sampling 策略示例：

```yaml
tail_sampling:
  decision_wait: 15s
  num_traces: 200000
  expected_new_traces_per_sec: 50000
  policies:
    # 一律保留 error
    - name: keep-errors
      type: status_code
      status_code:
        status_codes: [ERROR]
    # 一律保留慢请求
    - name: keep-slow
      type: latency
      latency:
        threshold_ms: 800
    # 保留包含特定关键字符串的 span
    - name: keep-important-endpoints
      type: string_attribute
      string_attribute:
        key: http.target
        values: ["/api/payment", "/api/order/submit"]
    # 其他按 5% 采样
    - name: sample-rest
      type: probabilistic
      probabilistic:
        sampling_percentage: 5
```

**核心参数解释**：

- `decision_wait: 15s`：一条 trace 第一次出现后等待 15s 再决策。要比应用最长请求时间稍长。
- `num_traces`：Collector 内存中同时保留的 trace 数量，乘以每 trace 平均 span 数决定内存需求。
- `expected_new_traces_per_sec`：用于预估内存，不会强制限流。

### 3. Tail sampling 的坑

- **同一 trace 必须打到同一 Collector**：OTel Collector 的 `tail_sampling` 是单机状态。Gateway 前面要用一致性哈希 load balancing，按 trace_id hash。我们用 Envoy 的 `ring_hash` LB 策略。
- **decision_wait 太长会吃内存，太短会漏 span**：需要和实际请求 latency 匹配。我们 p99 800ms，设 15s 有充裕 buffer。
- **policy 顺序无关紧要**：tail_sampling 会 OR 所有 policy，只要命中一条就保留。
- **tail_sampling 和 batch processor 顺序**：tail_sampling 必须在 batch 之前，否则 batch 会把不同 trace 混在一起。

## 五、Tempo 服务端配置骨架

```yaml
target: all   # 小规模；生产用单独 target 每个组件分开

multitenancy_enabled: true

server:
  http_listen_port: 3200
  grpc_listen_port: 9095

distributor:
  receivers:
    otlp:
      protocols:
        grpc:
          endpoint: 0.0.0.0:4317
        http:
          endpoint: 0.0.0.0:4318
  log_received_spans:
    enabled: false

ingester:
  lifecycler:
    ring:
      replication_factor: 3
      kvstore:
        store: memberlist
  max_block_duration: 10m
  max_block_bytes: 524288000   # 500MB
  trace_idle_period: 10s

memberlist:
  join_members:
    - tempo-memberlist.tempo.svc.cluster.local

compactor:
  ring:
    kvstore:
      store: memberlist
  compaction:
    block_retention: 720h      # 30 天
    compacted_block_retention: 1h
    compaction_window: 1h

storage:
  trace:
    backend: s3
    s3:
      bucket: tempo-prod-blocks
      endpoint: s3.ap-southeast-1.amazonaws.com
      region: ap-southeast-1
    block:
      version: vParquet4       # 最新推荐
      bloom_filter_false_positive: 0.01
    wal:
      path: /var/tempo/wal

overrides:
  defaults:
    ingestion:
      rate_limit_bytes: 20000000
      burst_size_bytes: 40000000
    metrics_generator:
      processors: [service-graphs, span-metrics]

metrics_generator:
  registry:
    external_labels:
      source: tempo
      cluster: prod
  storage:
    path: /var/tempo/generator/wal
    remote_write:
      - url: http://mimir.mimir.svc:9009/api/v1/push
        send_exemplars: true
```

**几个参数的选择逻辑**：

- `ingester.max_block_duration: 10m`：默认 30m。线上我们选 10m 为了让查询更快看到新数据（ingester 不 flush block 的话查询只能看 ingester 内存）。代价是 block 更多、compactor 压力更大。
- `ingester.max_block_bytes: 500MB`：太小频繁上传，太大 compactor 单次合并吃力。500MB 是甜点区。
- `compactor.compaction_window: 1h`：compactor 一次合并一个小时窗口内的 block。对象存储成本和查询速度的 trade-off。
- `storage.trace.block.version: vParquet4`：2.4+ 默认，相比 vParquet3 体积更小、列裁剪更好。

## 六、TraceQL：Tempo 的查询语言

TraceQL 是 Tempo 2.x 的查询语言，语法类似 LogQL 但面向 span：

### 基础查询

```traceql
# 按 service name 过滤
{ resource.service.name = "order-service" }

# 按 status 过滤
{ .status = error }

# 按 duration
{ duration > 500ms }

# 按 HTTP 属性
{ span.http.status_code = 500 }

# 组合
{ resource.service.name = "payment-api" && duration > 1s }
```

### 结构化查询：trace-level 过滤

```traceql
# 找出所有包含 payment error 的 trace
{ resource.service.name = "payment-api" && .status = error }

# 找出调用了 db 且耗时 > 1s 的 trace
{ resource.service.name = "db-proxy" } && { duration > 1s }

# 条件组合：trace 必须包含 A 且包含 B
{ span.http.target = "/checkout" } >> { .status = error }
```

`>>` 是 descendant 运算符，表示父子关系。`>` 是直接子。

### 聚合查询（2.4+）

```traceql
{ resource.service.name = "api-gateway" } 
  | select(span.http.target, duration) 
  | by(span.http.target) 
  | count() > 100
```

聚合能让 Tempo 替代一部分 metrics 查询，尤其是「某 endpoint 调用次数」之类。

### TraceQL 的性能陷阱

- **没带 service.name 的过滤非常慢**。Tempo 没有倒排索引，service.name 是一个比较好的剪枝维度。
- **时间范围尽量短**。一个 trace 查询通常选 1h 或更短，30 天的跨度即使列式扫描也慢。
- **`||` 昂贵**：or 运算会让 Tempo 扫描两边的所有候选。能用 `=~` 尽量用。
- **别用 `not`**：非操作会强制全量扫描。

## 七、Metrics Generator：从 span 生出指标

Tempo 的一个大杀器。`metrics_generator` 订阅 ingester 的实时 span 流，算两类指标：

### 1. Span Metrics（类似 RED）

对每个 service 生成：

```
traces_spanmetrics_calls_total{service, operation, status_code, span_name}
traces_spanmetrics_latency_bucket{service, operation, ...}
traces_spanmetrics_latency_sum
traces_spanmetrics_latency_count
```

这些指标直接 remote_write 到 Mimir。相当于你不需要业务代码在 HTTP handler 里手动埋 metric，Tempo 会从 span 自动生成。

### 2. Service Graph

分析 span 的父子关系和 peer.service，生成服务依赖图：

```
traces_service_graph_request_total{client, server, connection_type}
traces_service_graph_request_failed_total
traces_service_graph_request_client_seconds_bucket
```

Grafana 的 Service Map 面板直接基于这些指标绘制。相当于免费拿到一张全局依赖拓扑。

### 配置注意

```yaml
metrics_generator:
  processor:
    service_graphs:
      max_items: 10000
      workers: 10
      dimensions: [cluster, http.method]
      histogram_buckets: [0.1, 0.2, 0.4, 0.8, 1.6, 3.2, 6.4]
    span_metrics:
      dimensions: [http.method, http.status_code]
      histogram_buckets: [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0]
```

**坑**：

- `dimensions` 加太多会导致指标基数爆炸，打爆 Mimir。我们最多加 3 个 dimension。
- `max_items` 是 service_graph 缓存的 edge 数量，大集群要往上调。
- metrics generator 默认被禁，要在 overrides 里显式开：`metrics_generator.processors: [service-graphs, span-metrics]`。

## 八、和 Loki / Pyroscope 的联动

Tempo 的真正价值要串起来才能看到：

### Loki → Tempo（日志查 trace）

Loki datasource 里配 `derivedFields`，用正则从日志提取 trace_id：

```yaml
jsonData:
  derivedFields:
    - matcherRegex: "trace_id=(\\w+)"
      name: TraceID
      url: "$${__value.raw}"
      datasourceUid: tempo_ds
```

点击日志里的 trace_id 直接跳到 Tempo trace 详情。

### Tempo → Loki（trace 查日志）

Tempo datasource 里配 `tracesToLogs`：

```yaml
tracesToLogsV2:
  datasourceUid: loki_ds
  tags: [{ key: "service.name", value: "app" }]
  filterByTraceID: true
  spanStartTimeShift: "-1m"
  spanEndTimeShift: "1m"
```

点击一个 span 自动跳转到 Loki 的日志，查询语句是 `{app="..."} |= "trace_id=<id>"`。

### Tempo → Pyroscope（trace 查 profile）

Tempo datasource 里开 Span Profiles：

```yaml
tracesToProfilesV2:
  datasourceUid: pyroscope_ds
  tags: [{ key: "service.name" }]
  profileTypeId: "process_cpu:cpu:nanoseconds"
```

点击 span 的「Profile」按钮跳到 Pyroscope 并自动过滤 trace_id。前提是 SDK 在采 profile 时把 trace_id 写成 pprof label。

### Tempo → Mimir（service graph 跳指标）

Grafana 的 Service Map 本身就是基于 Mimir 查指标，不需要额外配。

## 九、事故复盘：ingester OOM 导致 trace 丢失

时间：2025 年 5 月。现象：Grafana 上某个时间段的 trace 完全查不到。

**根因**：上线一个新业务，span rate 突然涨 3 倍，达到 90K/s。ingester 的 `trace_idle_period: 10s` + `max_block_duration: 10m` 让内存中同时存在的 trace 数爆炸，OOM。默认 replication_factor=3 下，三个副本依次 OOM，trace 断档 8 分钟。

**应急**：

1. 扩 ingester 副本 + 内存；
2. 紧急降 tail_sampling 到 3%（减压）；
3. 调大 `overrides.defaults.ingestion.max_traces_per_user` 分租户限流。

**改进**：

- 建立 `tempo_ingester_live_traces` 指标告警（超过 80% ingester 内存估算阈值）；
- 上线 circuit breaker：distributor 侧开 `max_spans_per_trace`（默认 10000）降到 5000，避免一条 trace 无限长吃内存；
- 对新业务接入走灰度：先在 staging 跑 48h，然后按 10% → 50% → 100% 放量。

## 十、事故复盘：service graph 把 Mimir 打爆

时间：2025 年 9 月。现象：Mimir series 数突然涨 3000 万。

**根因**：一个团队在 metrics_generator 的 `service_graphs.dimensions` 里加了 `http.target`，这个 label 里包含了 REST URL path 参数（例如 `/users/12345/orders/67890`），基数爆炸。

**应急**：

1. 先在 Mimir 用 `drop` relabel rule 把这些指标丢掉；
2. 修改 Tempo overrides 移除 dimensions；
3. 让业务 fix URL 模板，把参数放 attributes 里，path 用 `/users/:id/orders/:orderId`。

**改进**：

- metrics_generator dimensions 变更要走审核；
- service_graph 的 target label 永远不能是 http.target，应该是 `peer.service` 或 `db.system`。

## 十一、成本分析

线上数据（每日 30 亿 span）：

- 对象存储：18TB/月（vParquet4 压缩 + 30d retention）
- S3 API 成本：大约 $200/月（compactor list/get 是大头）
- Compute：
  - distributor 6 * 4c/8G
  - ingester 12 * 16c/64G
  - compactor 3 * 16c/32G
  - querier 8 * 8c/32G
  - query-frontend 3 * 4c/8G
  - metrics-generator 4 * 8c/16G

合计 compute 约 400 vCPU、1.3TB 内存，换算 EKS 成本约 $8000/月。

对比 Elasticsearch 存同样量 span 数据的成本：保存 7 天就需要 200+ vCPU、3TB 内存、50TB 磁盘。Tempo 单存储就省了 70%，查询虽然慢一些（p95 1.5s vs ES 600ms），但可接受。

## 十二、成本优化 checklist

1. **vParquet4 格式**：2.4+ 默认，比 vParquet3 体积小 20%；
2. **对象存储用 Intelligent-Tiering**：30 天后自动降级到 IA；
3. **compactor 并发调高**：减少小 block 滞留；
4. **关掉 metrics_generator 的 span_metrics dimensions**：或者只留 1 个维度；
5. **tail sampling 全局不超过 10%**：错误和慢请求全采，其他 5%；
6. **distributor 侧限流**：`ingestion.rate_limit_bytes` 按租户限，避免单租户压爆 ingester；
7. **retention 拆两档**：热数据 7 天存 S3 Standard，温数据 30 天存 IA；
8. **禁用 discovery 查询**：`limits.max_search_bytes_per_trace` 设小，避免查全 span。

## 十三、自监控要点

最该盯的指标：

- `tempo_distributor_spans_received_total`：写入 QPS
- `tempo_distributor_ingester_push_failures_total`：ingester 拒收
- `tempo_ingester_live_traces`：内存中 trace 数
- `tempo_ingester_blocks_flushed_total`：block flush 速度
- `tempo_compactor_block_backlog`：compactor 积压
- `tempo_query_frontend_queries_total`：查询 QPS
- `tempo_query_frontend_retries_total`：frontend 重试次数
- `tempo_metrics_generator_registry_active_series`：metrics generator 活跃 series

## 十四、Jaeger / Zipkin 迁移经验

从 Jaeger 迁过来的一般流程：

1. **双写阶段**：用 OTel Collector 同时把 span 发给 Jaeger 和 Tempo，跑两周验证；
2. **Grafana UI 对接**：Tempo datasource 上线，业务逐步切查询入口；
3. **Tempo 兼容 Jaeger 协议**：可以让老 SDK 直接推 Tempo，平滑过渡；
4. **关 Jaeger**：下线 Elasticsearch 和 Jaeger collector，节省成本。

**要注意**：Jaeger 的 tag 索引查询在 Tempo 里需要 TraceQL 重写，部分 dashboard 需要手动改。service graph 的 peer.service 语义在 Jaeger / OTel 之间略有差异，迁移前要对齐 instrumentation 约定。

## 十五、生产建议清单

最后给出我个人落地 Tempo 的建议：

1. **直接上 2.6+**：不要从 1.x 起步；
2. **默认 vParquet4 + multitenancy**：即使一开始一个租户，也要开启多租户架构；
3. **OTel Collector 双层**：Agent + Gateway，tail sampling 在 Gateway 层；
4. **tail sampling 永远开**：即便你没那么多 span，也要准备好将来放量；
5. **metrics generator 谨慎加 dimension**：上限 2~3 个，且必须是低基数字段；
6. **和 Loki、Pyroscope 一起部署**：三件套联动才是最大价值；
7. **从第一天就配好 overrides**：每租户限流，避免坏邻居；
8. **retention 30 天起步，根据业务调**：90 天以上建议做冷热分层；
9. **建立 trace quality 审计**：每月扫一遍接入情况，补齐没埋点的服务；
10. **不要依赖 trace 做告警**：告警走 span metrics 或业务 metrics，trace 是用来定位的。

Tempo 不会让你惊艳，但它稳、便宜、可扩展。有 Loki 和 Mimir 的环境里，它是最自然的第三块拼图。

## 参考资料

- Grafana Tempo 官方文档 2.x 架构、TraceQL、metrics_generator
- Grafana Labs Blog：Tempo 2.x release notes
- OpenTelemetry Collector 官方文档 tail_sampling processor
- Grafana Tempo GitHub runbooks
