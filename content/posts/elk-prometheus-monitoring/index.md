---
title: "ELK 集群监控：用 Prometheus + Grafana 监控 Elasticsearch 健康"
date: 2026-04-11T11:30:00+08:00
draft: false
tags: ["Elasticsearch", "Prometheus", "监控", "ELK", "Grafana"]
categories: ["ELK Stack"]
description: "详解 elasticsearch-exporter 在 K8s 环境的部署配置、核心监控指标含义、告警规则设计，以及 Prometheus 外置监控与 Kibana Stack Monitoring 的选型对比。"
summary: "Kibana 内置的 Stack Monitoring 免费功能有限，告警媒介也受商业授权约束。我们最终选择 Prometheus + Grafana 方案监控 ELK 集群，这篇文章记录完整的落地过程和踩坑。"
toc: true
math: false
diagram: false
series: ["ELK Stack 完全手册"]
keywords: ["Elasticsearch", "Prometheus", "elasticsearch-exporter", "Grafana", "监控告警"]
params:
  reading_time: true
---

## 为什么选 Prometheus 而不是 Kibana Stack Monitoring

Kibana 有内置的 Stack Monitoring 功能，打开就能看 ES 集群的各种指标图表，看起来很方便。但我们最终选择了 Prometheus + Grafana 方案，主要原因有三点：

**第一，告警媒介受限。** Stack Monitoring 的告警功能需要 Platinum 或以上授权才能对接钉钉、PagerDuty、Webhook 等告警渠道。免费的 Alertmanager 支持几十种告警接收器，用起来灵活得多。

**第二，监控依赖被监控对象本身有风险。** Stack Monitoring 的监控数据默认写回到同一个 ES 集群，当集群出问题时，监控数据写入也可能受影响。更糟糕的是，如果是因为磁盘满导致集群 readonly，监控数据写不进去，告警自然也不会触发。Prometheus 是完全独立的监控体系，ES 挂了 Prometheus 还能正常采集并告警。

**第三，统一监控体系。** 我们所有服务都用 Prometheus + Grafana，ELK 集群统一进来之后，一个地方看所有告警，oncall 效率高很多。

## 架构概览

```
Elasticsearch 集群
    │
    │ HTTP API (9200)
    ▼
elasticsearch-exporter (9114)
    │
    │ /metrics (Prometheus 格式)
    ▼
Prometheus
    │
    ├──→ Alertmanager → 钉钉/PagerDuty
    │
    └──→ Grafana Dashboard
```

## elasticsearch-exporter 部署（K8s 环境）

我们的 ELK 集群跑在 K8s 里，exporter 也部署在同一个 namespace。

### Deployment

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: elasticsearch-exporter
  namespace: monitoring
spec:
  replicas: 1
  selector:
    matchLabels:
      app: elasticsearch-exporter
  template:
    metadata:
      labels:
        app: elasticsearch-exporter
    spec:
      containers:
        - name: elasticsearch-exporter
          image: quay.io/prometheuscommunity/elasticsearch-exporter:v1.7.0
          args:
            - "--es.uri=https://elastic:$(ES_PASSWORD)@elasticsearch-master:9200"
            - "--es.all"
            - "--es.indices"
            - "--es.indices_settings"
            - "--es.shards"
            - "--es.snapshots"
            - "--es.ssl-skip-verify"
            - "--web.listen-address=:9114"
          env:
            - name: ES_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: elasticsearch-credentials
                  key: password
          ports:
            - containerPort: 9114
              name: metrics
          resources:
            requests:
              cpu: 50m
              memory: 64Mi
            limits:
              cpu: 200m
              memory: 256Mi
```

### Service

```yaml
apiVersion: v1
kind: Service
metadata:
  name: elasticsearch-exporter
  namespace: monitoring
  labels:
    app: elasticsearch-exporter
spec:
  selector:
    app: elasticsearch-exporter
  ports:
    - name: metrics
      port: 9114
      targetPort: 9114
```

### ServiceMonitor（Prometheus Operator）

如果你用的是 kube-prometheus-stack，通过 ServiceMonitor 配置采集：

```yaml
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: elasticsearch-exporter
  namespace: monitoring
  labels:
    release: kube-prometheus-stack  # 要和 Prometheus Operator 的 serviceMonitorSelector 匹配
spec:
  selector:
    matchLabels:
      app: elasticsearch-exporter
  endpoints:
    - port: metrics
      interval: 30s
      scrapeTimeout: 10s
      path: /metrics
```

**版本兼容性注意**：elasticsearch-exporter 的版本必须与 ES 版本匹配。v1.7.x 支持 ES 7.x 和 8.x，但 ES 8.x 的某些新指标在旧版本 exporter 里拿不到。我们踩过一个坑：用 v1.5 的 exporter 连 ES 8.8，某些 `_cluster/stats` 的字段 exporter 解析报错，导致整个 `/metrics` 接口返回 500，Prometheus 采集失败。升级到 v1.7 解决。

## 核心监控指标详解

### 集群健康状态

```promql
# 集群健康状态（0=green, 1=yellow, 2=red）
elasticsearch_cluster_health_status{color="green"}
elasticsearch_cluster_health_status{color="yellow"}
elasticsearch_cluster_health_status{color="red"}

# 实际值（1 表示当前处于该状态）
elasticsearch_cluster_health_status{cluster="my-cluster"}
```

日常巡检用：

```promql
# 获取当前状态（0=green, 1=yellow, 2=red）
elasticsearch_cluster_health_status{color!="green"} == 1
```

### 分片相关指标

```promql
# 未分配分片数
elasticsearch_cluster_health_unassigned_shards

# 初始化中的分片（节点重启时会短暂出现）
elasticsearch_cluster_health_initializing_shards

# 重定位中的分片（集群扩缩容时出现）
elasticsearch_cluster_health_relocating_shards

# 活跃分片总数
elasticsearch_cluster_health_active_shards
```

### 索引写入速率

```promql
# 每秒索引写入速率（rate 计算最近 5 分钟的增量）
rate(elasticsearch_indices_indexing_index_total[5m])

# 所有节点写入速率之和
sum(rate(elasticsearch_indices_indexing_index_total[5m]))
```

写入速率突然下降是采集管道出问题的信号，比 Kafka consumer lag 更早发现问题。

### 查询延迟

```promql
# 平均查询延迟（毫秒）
rate(elasticsearch_indices_search_fetch_time_milliseconds_total[5m])
/
rate(elasticsearch_indices_search_fetch_total[5m])

# 索引级别的查询延迟（找出慢索引）
rate(elasticsearch_indices_search_query_time_milliseconds_total[5m])
/ on(index) 
rate(elasticsearch_indices_search_query_total[5m])
```

### JVM 堆内存

```promql
# 堆内存使用率（百分比）
elasticsearch_jvm_memory_used_bytes{area="heap"}
/
elasticsearch_jvm_memory_max_bytes{area="heap"}
* 100

# 按节点查看
elasticsearch_jvm_memory_used_bytes{area="heap", node="es-hot-1"}
/
elasticsearch_jvm_memory_max_bytes{area="heap", node="es-hot-1"}
* 100
```

JVM 堆内存持续在 75% 以上需要关注，超过 80% 要告警，GC 频率会明显升高影响查询性能。

### GC 频率

```promql
# 老年代 GC（Full GC）次数增长率
rate(elasticsearch_jvm_gc_collection_seconds_count{gc="old"}[5m])

# 老年代 GC 耗时增长率（秒）
rate(elasticsearch_jvm_gc_collection_seconds_sum{gc="old"}[5m])
```

老年代 GC 频率 > 1次/分钟 是严重的性能问题信号，通常意味着内存配置不足或有内存泄漏。

### 磁盘使用率

exporter 本身不采集磁盘指标，需要配合 node-exporter：

```promql
# ES 数据目录所在磁盘使用率
(
  node_filesystem_size_bytes{mountpoint="/data"} 
  - node_filesystem_avail_bytes{mountpoint="/data"}
)
/ node_filesystem_size_bytes{mountpoint="/data"}
* 100
```

## 告警规则配置

在 Prometheus 的 rules 文件里（或者 Prometheus Operator 的 PrometheusRule CRD）配置：

```yaml
groups:
  - name: elasticsearch
    rules:
      # 集群状态 red（主分片未分配，数据不可用）
      - alert: ElasticsearchClusterRed
        expr: elasticsearch_cluster_health_status{color="red"} == 1
        for: 2m
        labels:
          severity: critical
        annotations:
          summary: "ES 集群状态 RED"
          description: "集群 {{ $labels.cluster }} 状态变为 RED，主分片未分配，部分数据不可用。立即检查节点状态。"

      # 集群状态 yellow 超过 15 分钟（副本分片未分配）
      - alert: ElasticsearchClusterYellow
        expr: elasticsearch_cluster_health_status{color="yellow"} == 1
        for: 15m
        labels:
          severity: warning
        annotations:
          summary: "ES 集群状态 YELLOW 持续超过 15 分钟"
          description: "集群 {{ $labels.cluster }} 副本分片未分配，可能是节点宕机或磁盘不足。"

      # 节点数量减少（节点离线）
      - alert: ElasticsearchNodeDown
        expr: elasticsearch_cluster_health_number_of_nodes < 3
        for: 2m
        labels:
          severity: critical
        annotations:
          summary: "ES 节点离线"
          description: "当前节点数 {{ $value }}，期望 3 个节点。"

      # JVM 堆内存使用率过高
      - alert: ElasticsearchJvmHeapHigh
        expr: |
          elasticsearch_jvm_memory_used_bytes{area="heap"}
          /
          elasticsearch_jvm_memory_max_bytes{area="heap"}
          * 100 > 80
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "ES 节点 JVM 堆内存使用率高"
          description: "节点 {{ $labels.node }} 堆内存使用率 {{ $value | humanize }}%，超过 80%。"

      # 未分配分片告警
      - alert: ElasticsearchUnassignedShards
        expr: elasticsearch_cluster_health_unassigned_shards > 0
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "ES 存在未分配分片"
          description: "{{ $value }} 个分片未分配，可能影响数据可靠性。"

      # 磁盘使用率过高（需要 node-exporter）
      - alert: ElasticsearchDiskSpaceHigh
        expr: |
          (
            node_filesystem_size_bytes{mountpoint="/data"}
            - node_filesystem_avail_bytes{mountpoint="/data"}
          )
          / node_filesystem_size_bytes{mountpoint="/data"}
          * 100 > 80
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "ES 数据盘使用率超 80%"
          description: "节点 {{ $labels.instance }} 数据盘使用率 {{ $value | humanize }}%。"

      # 写入速率骤降（可能是采集管道故障）
      - alert: ElasticsearchIndexingRateDrop
        expr: |
          sum(rate(elasticsearch_indices_indexing_index_total[5m])) 
          < 
          sum(rate(elasticsearch_indices_indexing_index_total[5m] offset 30m)) * 0.3
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "ES 写入速率骤降超过 70%"
          description: "当前写入速率 {{ $value | humanize }} docs/s，较 30 分钟前下降超过 70%，请检查 Filebeat/Logstash。"
```

最后一条告警是我们自己加的，用于监控采集管道的健康状态。ES 写入速率突然大幅下降，通常意味着 Logstash 宕机或 Kafka 积压严重，比等用户反馈日志延迟要早发现。

## Grafana Dashboard 关键面板

我们的 ES 集群健康总览 Dashboard 包含以下面板，按重要性排列：

**第一行：状态指标（单值面板）**
- 集群状态（绿/黄/红，带颜色变化）
- 节点数量
- 未分配分片数
- JVM 堆内存最高使用率（取所有节点最大值）

**第二行：写入与查询趋势**
- 索引写入速率折线图（按索引分色）
- 查询 QPS 折线图
- 平均查询延迟折线图

**第三行：资源使用**
- 各节点 JVM 堆内存使用率（多线折线图）
- 各节点 GC 频率
- 各节点磁盘使用率（进度条面板）

**第四行：分片详情**
- 各索引分片分布热力图
- 最近的分片分配事件（基于 ES 集群日志）

推荐直接从 Grafana Dashboard 中心导入，搜索 "elasticsearch" 可以找到 ID 2322（ES 综合监控）和 ID 14191（ES Overview），根据自己的 exporter 版本选对应的 dashboard，导入后微调字段名即可，不需要从头画。

## Kibana Stack Monitoring vs Prometheus 的选择

| 对比维度 | Kibana Stack Monitoring | Prometheus + Grafana |
|---------|------------------------|---------------------|
| 部署成本 | 低，内置功能直接开启 | 中等，需额外部署 |
| 告警媒介 | 受商业授权限制 | 完全开源，渠道丰富 |
| 监控独立性 | 依赖 ES 自身 | 完全独立 |
| 指标丰富度 | ES 专属指标完整 | 需 exporter，部分指标缺失 |
| 统一监控 | 只有 ELK 指标 | 与其他服务统一 |
| 历史数据 | 存在 ES 里 | 存在 Prometheus/Thanos |

我的建议：**如果团队已经用 Prometheus 监控其他服务，选 Prometheus 方案**，统一体系运维成本更低；**如果只有 ELK 没有其他监控，用 Kibana Stack Monitoring 就够了**，省去额外组件。

两者也可以并存：Stack Monitoring 做集群内部的深度指标展示（比如各索引的段信息、fielddata 使用量这些细粒度指标），Prometheus 做告警和与其他系统的整合。

## 踩坑记录

### exporter 版本与 ES 版本兼容性

前面提到过，一定要用匹配的版本。还有一个坑是 ES 8.x 默认开启了安全认证，exporter 连接 ES 需要配置认证信息。如果 exporter 的 `--es.uri` 参数里的密码包含特殊字符（比如 `+`、`/`、`@` 等），需要 URL encode，否则 exporter 启动时解析 URI 会出错，日志里是很难看懂的连接错误。

### 监控数据写回自身的风险

如果你还是用 Stack Monitoring，强烈建议开启 `metricbeat` 模式把监控数据写到一个**独立的 ES 集群**里，而不是写回被监控集群自身。

配置方式：在 kibana.yml 里设置：

```yaml
monitoring.cluster_uuid: "被监控集群的UUID"
xpack.monitoring.elasticsearch.collection.enabled: false
```

然后部署独立的 Metricbeat 收集数据写到监控集群。这样即使被监控集群完全宕机，监控数据还在，告警还能触发。我们有一次磁盘满导致集群变 readonly，自身写不进去，监控数据也丢了一段，事后复盘时发现那段时间的历史曲线是空的，非常影响根因分析。

### exporter 抓取超时

当 ES 集群压力大时，`/_cluster/stats`、`/_all/_stats` 这些接口响应会很慢，exporter 可能超时。Prometheus 默认的 scrapeTimeout 是 10 秒，生产环境建议调到 30 秒：

```yaml
# ServiceMonitor 配置
endpoints:
  - port: metrics
    interval: 60s     # 采集间隔可以稍长
    scrapeTimeout: 30s  # 超时时间要够
```

同时在 exporter 启动参数里加 `--es.timeout=30s`，确保 exporter 对 ES 的请求超时时间大于 Prometheus 的 scrapeTimeout，避免 exporter 还没拿到 ES 响应就被 Prometheus 判超时。
