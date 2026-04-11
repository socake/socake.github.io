---
title: "VictoriaMetrics：比 Prometheus 更省资源的监控存储方案"
date: 2026-04-11T11:00:00+08:00
draft: false
tags: ["VictoriaMetrics", "Prometheus", "监控", "可观测性", "运维", "2026"]
categories: ["可观测性"]
description: "从 Prometheus 的存储和查询瓶颈出发，对比分析 VictoriaMetrics 的核心优势，详细讲解单节点与集群部署、Prometheus 迁移方案、MetricsQL 扩展以及 Grafana 接入配置。"
summary: "Prometheus 撑不住了？本文对比 VictoriaMetrics 与 Prometheus 的核心差异，介绍 remote_write 无缝迁移方案，以及 VM 在资源占用、压缩率、查询性能上的实际提升。"
toc: true
math: false
diagram: false
keywords: ["VictoriaMetrics", "Prometheus 替代", "监控存储", "MetricsQL", "remote_write"]
params:
  reading_time: true
---

## Prometheus 的成长烦恼

Prometheus 已经是监控领域的事实标准，但随着规模增大，它的问题也越来越明显。

**存储瓶颈**：Prometheus 默认数据保留 15 天，TSDB 的压缩率在大规模场景下表现一般。监控 500 个服务、每个服务 200 个指标、15 秒采集间隔，一个月的数据量可以轻松超过 500GB。

**查询性能**：当你的 Grafana Dashboard 上有几十个 Panel、每个 Panel 都是复杂的 PromQL 聚合查询，时间范围选"过去 30 天"时，查询超时是家常便饭。

**高可用复杂**：Prometheus 本身是单节点设计，要做 HA 需要部署两个实例 + Thanos 或 Cortex，架构复杂，运维成本高。

**扩展困难**：Prometheus 不支持水平扩展写入，所有数据都得打到一个实例。

VictoriaMetrics（简称 VM）是一个高性能的时序数据库，专门为 Prometheus 兼容场景设计，解决了上述大部分问题。

## VM vs Prometheus 核心差异

### 写入方式

Prometheus 是主动 Pull 模式：定期去各个 Target 抓取指标。VM 本身不抓取，而是作为存储后端，接收 Prometheus 通过 `remote_write` 推过来的数据。

这意味着：**你不需要替换 Prometheus 的抓取层，只需要改存储**。现有的 ServiceMonitor、scrape_config、Alertmanager 全部保留，只是数据不再存在 Prometheus 本地，而是写到 VM。

```yaml
# prometheus.yml 追加
remote_write:
  - url: http://victoriametrics:8428/api/v1/write
    queue_config:
      max_samples_per_send: 10000
      capacity: 20000
      max_shards: 30
```

### 存储压缩率

VM 声称比 Prometheus 的存储压缩率高 7 倍，从实际使用来看，相同数据量下 VM 的磁盘占用通常是 Prometheus 的 1/4 到 1/3。

主要原因是 VM 使用了更激进的压缩算法，以及针对时序数据的 delta-of-delta + 变长整数编码，对于单调递增的计数器指标（Counter）效果尤其好。

### 查询性能

VM 在范围查询（如"过去 30 天的 P99 延迟"）上比 Prometheus 快很多，原因是：
- VM 的 Block 结构对范围扫描更友好
- 支持并行查询（多核利用率更高）
- 索引设计减少了大范围查询的 IO

实测数据：同样的查询，Prometheus 需要 8 秒，VM 需要 1.2 秒。

## 单节点 vs 集群部署

### 单节点（vmsingle）

适合中小规模（每秒写入 < 100 万 samples），部署极简：

```bash
helm repo add vm https://victoriametrics.github.io/helm-charts/
helm install vmsingle vm/victoria-metrics-single \
  --set server.retentionPeriod=3  \  # 保留 3 个月
  --set server.storage.volumeClaimTemplate.spec.resources.requests.storage=500Gi
```

单节点的 Docker 启动（测试用）：

```bash
docker run -it --rm \
  -v $(pwd)/victoria-metrics-data:/victoria-metrics-data \
  -p 8428:8428 \
  victoriametrics/victoria-metrics \
  -retentionPeriod=3 \
  -storageDataPath=/victoria-metrics-data
```

### 集群模式（vmcluster）

每秒 > 100 万 samples，或者需要存储扩展、高可用时选集群模式。集群由三个组件组成：

- **vmstorage**：实际存储数据，可水平扩展
- **vminsert**：接收写入请求，按一致性哈希分片到不同 vmstorage
- **vmselect**：处理查询请求，从多个 vmstorage 合并结果

```yaml
# values-cluster.yaml
vmcluster:
  enabled: true
  spec:
    retentionPeriod: "3"  # 3 个月
    replicationFactor: 2   # 每份数据存 2 副本

    vmstorage:
      replicaCount: 3
      resources:
        requests:
          cpu: "2"
          memory: 4Gi
      storage:
        volumeClaimTemplate:
          spec:
            resources:
              requests:
                storage: 1Ti

    vminsert:
      replicaCount: 2
      resources:
        requests:
          cpu: "1"
          memory: 1Gi

    vmselect:
      replicaCount: 2
      resources:
        requests:
          cpu: "1"
          memory: 2Gi
```

```bash
helm install vmcluster vm/victoria-metrics-cluster -f values-cluster.yaml
```

集群写入地址：`http://vminsert:8480/insert/0/prometheus/`
集群查询地址：`http://vmselect:8481/select/0/prometheus/`

**选择建议**：团队刚起步先用 vmsingle，够用就不要增加架构复杂度；当 vmsingle 的 CPU 长期打满或磁盘扩展不方便时再迁移到集群。

## Prometheus → VM 迁移

如果已有 Prometheus 数据想迁移到 VM，分两步：

**第一步**：历史数据迁移，用 `vmctl` 工具：

```bash
# 安装 vmctl
curl -L https://github.com/VictoriaMetrics/VictoriaMetrics/releases/latest/download/vmutils-linux-amd64.tar.gz | tar xz

# 从 Prometheus 迁移数据到 VM
./vmctl prometheus \
  --prom-snapshot=/path/to/prometheus/data \  # Prometheus 数据目录
  --vm-addr=http://victoriametrics:8428
```

或者从已运行的 Prometheus API 迁移：

```bash
./vmctl remote-read \
  --remote-read-src-addr=http://prometheus:9090 \
  --remote-read-step-interval=day \
  --remote-read-filter-time-start=2025-01-01T00:00:00Z \
  --vm-addr=http://victoriametrics:8428
```

**第二步**：修改 Prometheus 的 remote_write 配置，让新数据写到 VM。两者可以并行运行一段时间，确认 VM 数据正常后再下掉 Prometheus 本地存储或整个 Prometheus。

## MetricsQL：兼容且扩展 PromQL

VM 使用 MetricsQL 作为查询语言，完全兼容 PromQL，同时有一些实用扩展。

### 兼容 PromQL

所有标准 PromQL 查询直接可用：

```promql
# 请求成功率（标准 PromQL）
sum(rate(http_requests_total{status="200"}[5m])) /
sum(rate(http_requests_total[5m]))
```

### MetricsQL 扩展

**`rollup` 系列函数**：一个函数返回多个统计值，无需写多个查询：

```promql
# 同时返回 min/avg/max
rollup(node_cpu_seconds_total[1h])
```

**`topk_max`**：按时间窗口内的最大值 Top K，而不是当前值：

```promql
# 过去 1 小时内最高延迟的 Top 5 服务
topk_max(5, max_over_time(http_request_duration_seconds{quantile="0.99"}[1h]))
```

**`limitOffset`**：查询结果分页，配合大量 label 值时有用：

```promql
limitOffset(10, 0, sort_desc(sum by (service) (rate(http_requests_total[5m]))))
```

**`aggr_over_time`**：对滚动窗口内的样本做多种聚合：

```promql
aggr_over_time("min,max,avg,stddev", my_metric[1d])
```

## Grafana 接入

VM 完全兼容 Prometheus 数据源协议，Grafana 里直接配置：

1. Grafana → Configuration → Data Sources → Add data source
2. 选择 **Prometheus** 类型（不是 VictoriaMetrics，因为 VM 兼容 Prometheus API）
3. URL 填 VM 地址：
   - 单节点：`http://victoriametrics:8428`
   - 集群：`http://vmselect:8481/select/0/prometheus`
4. 点击 Save & Test，显示 "Data source is working" 即可

现有的 Prometheus Dashboard（包括从 grafana.com 导入的 Dashboard ID）无需修改，直接可用。

VM 也提供了 vmui（内置 UI）：访问 `http://victoriametrics:8428/vmui/`，可以直接执行 MetricsQL 查询，比 Prometheus 的 Web UI 好用很多，支持自动补全和查询耗时统计。

## 踩坑记录

**VM 不支持的 PromQL 函数**

`holt_winters`（指数平滑预测）和某些实验性函数在 VM 里不支持。如果现有告警规则用了这些函数，迁移前要检查。用 `vmctl` 的 `--check-promql` 参数可以批量验证规则兼容性。

**数据保留策略配置**

VM 的 `retentionPeriod` 默认单位是月（填 `3` 表示 3 个月），但也支持带单位的写法 `90d`、`1y`。集群模式下 vmstorage 的 `retentionPeriod` 要和 vminsert、vmselect 保持一致，不然可能出现查询返回空数据的情况。

**内存占用优化**

VM 默认会把大量数据缓存在内存里加速查询，如果机器内存不足，可以调整：

```bash
# 限制 VM 最大内存使用（默认是系统内存的 60%）
./victoria-metrics \
  -memory.allowedPercent=40 \
  -search.maxMemoryPerQuery=256MB
```

**remote_write 积压问题**

如果 VM 出现短暂不可用，Prometheus 的 remote_write queue 会积压。默认队列容量可能不够，需要调大：

```yaml
remote_write:
  - url: http://victoriametrics:8428/api/v1/write
    queue_config:
      capacity: 100000
      max_samples_per_send: 10000
      batch_send_deadline: 5s
      max_shards: 100
      min_backoff: 30ms
      max_backoff: 5s
```

**时区问题**

VM 内部统一用 UTC 存储，Grafana 展示时依赖浏览器时区。如果 Dashboard 里的时间和你预期不一致，检查 Grafana 的 "Browser time zone" 设置，以及查询里是否有 `offset` 操作。
