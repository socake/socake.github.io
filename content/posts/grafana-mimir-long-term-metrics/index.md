---
title: "Grafana Mimir 长期指标存储实战：从单集群 Prometheus 到 10 亿级 series"
date: 2025-06-18T10:00:00+08:00
draft: false
tags: ["Mimir", "Prometheus", "可观测性", "多租户"]
categories: ["可观测性"]
description: "Mimir 3.x 生产落地笔记：classic vs ingest storage 架构选型、blocks storage 布局、compactor/store-gateway 调参、多租户隔离和容量规划的完整踩坑记录。"
summary: "从一套 Prometheus HA pair 起步，一路扩到跨三地多活 Mimir，把 series 数从千万推到十亿级。本文把架构、配置、监控、事故按顺序讲清楚。"
toc: true
math: false
diagram: false
keywords: ["Mimir", "Prometheus", "TSDB", "多租户", "remote write"]
params:
  reading_time: true
---

## 为什么要换成 Mimir

2023 年之前我们的指标平台是两套 Prometheus HA pair + Thanos sidecar，存储 Thanos Store + S3。日常 5 亿 active series，查询高峰 3 亿 samples/s。Thanos 的问题不在它的设计，而在它的运维心智负担：compactor 经常卡住、store gateway 的 cache 命中率不稳定、多租户隔离只能靠 namespace 级外挂。2024 年初我们下决心迁到 Mimir。

迁完之后的状态：

- 单集群 9 亿 active series，高峰 13 亿；
- remote write 吞吐 8.5M samples/s；
- 查询 p95 800ms、p99 4.8s；
- 3 个物理集群互为多活，对 Grafana 呈现为单一入口；
- 运维人力从 2 FTE 降到 0.5 FTE。

这篇文章把迁移和调优过程里学到的东西整理出来，顺便把 Mimir 3.x 引入的 ingest storage 架构说清楚——它是我认为这两年 Mimir 最重要的变化。

## 一、Mimir 的两套架构：classic vs ingest storage

2024 年之前 Mimir 只有一套架构，官方现在叫 **classic architecture**；2024 年底的 Mimir 2.14 把 **ingest storage architecture** 标记为 beta，Mimir 3.0 正式 GA 并推荐新部署使用。它们的核心区别是：

### Classic 架构

```
Prometheus/Alloy
   │  remote_write
   ▼
Distributor ──hash ring──▶ Ingester (x N, RF=3)
                               │
                               ▼ 2h blocks
                        Object Storage (S3/GCS/OSS)
                               ▲
Querier ──▶ Store Gateway ─────┘
```

- Distributor 拿到样本，按 series 的 label hash 打到 N 个 ingester；
- Ingester 内存中维护 TSDB head，每 2 小时切一个 block 上传对象存储；
- Querier 近 13h 的数据走 ingester，历史数据走 store gateway。

**痛点**：读和写共享 ingester，写入高峰时查询会被拖慢；ingester 扩缩容需要 shuffle ring，数据迁移麻烦；跨 AZ 部署时 distributor → ingester 有大量跨 zone 流量。

### Ingest storage 架构

```
Prometheus/Alloy
   │  remote_write
   ▼
Distributor ──▶ Kafka topic (1 partition per ingester)
                    │
                    ▼
                Ingester (consume, RF logical)
                    │
                    ▼ 2h blocks
              Object Storage
                    ▲
Querier ──▶ Store Gateway ─────┘
```

- Distributor 不再直接和 ingester 通信，而是把每个样本写进 Kafka；
- Ingester 作为 consumer 拉数据、建 TSDB；
- 副本冗余从 ingester ring 移到了 Kafka 复制；
- 读写解耦：ingester 只消费 Kafka，不再直接接收 distributor 请求；
- ingester 重启只要重新 consume 一小段 offset，不需要像以前那样重放 ring。

我们在 2025 年 6 月迁到 ingest storage，核心收益有三点：

1. **ingester 扩容不再 shuffle**：以前加一个 ingester 要折腾 24h 等 hand-off，现在开 partition 即可；
2. **写读物理隔离**：查询高峰不影响写入；
3. **跨 AZ 成本显著下降**：Kafka 内部做副本，distributor 到 Kafka 只走一次跨 AZ。

代价是：你要额外维护一个 Kafka 集群。我们复用了已有的 Kafka 平台，运维成本边际很低。新入场的团队建议评估一下自己是否有 Kafka 的运维能力，如果没有，classic 架构依然能撑到 5 亿 series 这个量级。

**本文剩下部分默认 classic 架构**，ingest storage 的差异会单独说明。

## 二、组件清单和职责

Classic 架构下你一定会看到的组件：

- **Distributor**：无状态。接收 remote write，做样本校验、label enforcement、HA tracker（两套 Prometheus 去重）、shard 到 ingester。
- **Ingester**：有状态（memberlist ring）。维护每个租户的 TSDB head，2h 切一个 block 上传。默认 RF=3。
- **Querier**：无状态。近期数据查 ingester，历史数据查 store gateway。
- **Store Gateway**：有状态（ring）。从对象存储下载 block index header 并本地缓存，响应 series/chunks 查询。
- **Query Frontend**：无状态。拆分查询、缓存、限流、排队。
- **Query Scheduler**：可选但推荐。把 frontend 和 querier 之间的 queue 独立出来。
- **Compactor**：有状态。后台 compact block：垂直合并（同一 2h 窗口的 3 个副本合成 1 个）和水平合并（把多个 2h 合成 12h、2d、8d）。
- **Ruler**：定时执行记录规则和告警规则，结果写回 Mimir。
- **Alertmanager**：可复用外部 AM，也可用 Mimir 内置多租户 AM。
- **Overrides Exporter**：把 limits/overrides 暴露成指标，方便做配额治理。

一个中等规模集群（2 亿 series）的组件分布参考：

| 组件 | 副本 | 规格 | 说明 |
|---|---|---|---|
| distributor | 8 | 8c/16G | 按 remote write QPS 扩 |
| ingester | 30 | 16c/96G | 最耗内存的组件 |
| querier | 30 | 16c/32G | 查询并发按 QPS 扩 |
| store-gateway | 12 | 8c/32G | 内存用于 index header |
| query-frontend | 4 | 4c/8G | 无状态，副本数小 |
| query-scheduler | 3 | 4c/8G | 为 HA 而非性能 |
| compactor | 4 | 16c/64G | 看 block 大小 |
| ruler | 6 | 8c/16G | 规则多时扩 |

## 三、blocks storage：2 小时一块的秘密

Mimir 的存储格式几乎就是 Prometheus TSDB：

```
<bucket>/
└── <tenant_id>/
    ├── 01HXYZ... (block ULID)
    │   ├── index                # 倒排 + 符号表
    │   ├── meta.json            # 元信息 (from/to/stats)
    │   ├── chunks/
    │   │   └── 000001
    │   └── tombstones
    ├── 01HXZZ.../...
    └── markers/
        ├── 01HXYZ-deletion-mark.json
        └── 01HXYZ-no-compact-mark.json
```

关键点：

1. **每个 ingester 独立产生 block**，所以同一 2h 窗口会有 RF=3 份块。Compactor 做垂直合并消重。
2. **block 是不可变的**。删除通过 tombstone 或 deletion mark 实现；retention 过期的 block 由 compactor 打 deletion mark，延迟一段时间后真正删除。
3. **block 上传之后还会在 ingester 内存里待一段时间**，由 `-blocks-storage.tsdb.retention-period` 控制（默认 13h）。这段时间给 store gateway 发现新 block 的窗口，避免查询空洞。
4. **meta.json 里的 stats** 非常重要，包含 sample 数、series 数、chunk 数，compactor 和 query frontend 都靠它做 planning。

### 对象存储配置示例

```yaml
common:
  storage:
    backend: s3
    s3:
      endpoint: s3.ap-southeast-1.amazonaws.com
      bucket_name: mimir-prod-blocks
      region: ap-southeast-1
      access_key_id: ${S3_ACCESS_KEY}
      secret_access_key: ${S3_SECRET_KEY}
      sse:
        type: SSE-KMS
        kms_key_id: arn:aws:kms:...
blocks_storage:
  backend: s3
  tsdb:
    dir: /data/tsdb
    retention_period: 13h
    wal_compression_enabled: true
    block_ranges_period: [2h]
    head_compaction_interval: 1m
```

### 对象存储操作要点

- **不要把 index 和 chunk 分桶**，Mimir 没有分开配置的能力，会出错。
- **开 bucket key 模式**（AWS KMS），不然 KMS throttle 会变成瓶颈。
- **lifecycle rule 不要误删 markers/ 目录**，否则 compactor 会再次看到已删除的 block，造成「幽灵数据」。

## 四、ingester：最需要调教的组件

Ingester 是 Mimir 里最吃资源、也最容易出事故的组件。核心参数：

```yaml
ingester:
  ring:
    replication_factor: 3
    kvstore:
      store: memberlist
  instance_limits:
    max_ingestion_rate: 300000
    max_series: 3500000
    max_tenants: 3000
    max_inflight_push_requests: 30000
  concurrent_flushes: 16
  flush_op_timeout: 2m

limits:
  max_global_series_per_user: 5000000
  max_global_series_per_metric: 500000
  max_label_names_per_series: 40
  max_label_value_length: 2048
  ingestion_rate: 500000
  ingestion_burst_size: 5000000
  compactor_blocks_retention_period: 90d
```

坑：

1. **`max_global_series_per_user` 是全局 series 配额**，distributor 会基于 ring size 计算每个 ingester 该分到的配额。配少了一个坏租户就可以把好租户挤掉。我们生产默认 2000 万/租户，特大租户单独 override 到 1 亿。
2. **`max_series` instance limit 必须设**，否则一个 ingester 被打爆会挂掉整个写路径。
3. **`concurrent_flushes` 默认 1，太小**。block 上传是 I/O 密集的，并发开到 CPU 核数的一半比较合适。
4. **`wal_compression_enabled` 一定开**。WAL 不压缩的情况下，一个 2h block 周期 WAL 能到 20GB，重启 replay 特别慢。

### ingester 内存计算

实测公式（classic 架构）：

```
mem ≈ 4KB * active_series
    + 0.5GB * blocks_in_memory
    + 2GB * wal_replay_buffer
```

举例：2000 万 active series，13h 内 7 个 block 在内存中：

```
mem ≈ 4KB * 2e7 + 0.5G * 7 + 2G
    ≈ 80GB + 3.5G + 2G
    ≈ 86GB
```

所以 96GB 的 ingester 只能承载 2000 万 active series（单实例），RF=3 之下三个 ingester 总容量就是 2000 万全局 series。想扩到 1 亿 series，就需要 15 个 ingester，依此类推。

## 五、store-gateway：sharding 与 index-header

Store gateway 是历史数据查询的前线。它把对象存储里的 block 下载一部分元数据（index-header）到本地，响应 querier 的 `series/chunks` 请求。

```yaml
store_gateway:
  sharding_enabled: true
  sharding_ring:
    kvstore:
      store: memberlist
    replication_factor: 3
  sharding_strategy: shuffle-sharding
```

为什么推荐 shuffle sharding：

- 默认 sharding 把所有 tenant 均匀切到所有 store gateway；
- shuffle sharding 给每个租户分配一个 subring，大小由 `store_gateway_tenant_shard_size` 决定；
- 一个坏租户（比如查了一年 range）只会打爆它 subring 内的几个 pod，不影响其他租户；
- 同时提升 block index header 本地缓存命中率。

配合：

```yaml
limits:
  store_gateway_tenant_shard_size: 6
```

### index-header 常驻

index-header 是对象存储里 block `index` 文件的符号表和倒排索引的子集，大概是完整 index 的 1%。store gateway 启动时按 ring 分配要负责的 block，逐个下载 index-header 到本地。一个 pod 至少要预留：

```
local_ssd_size ≈ 1% * total_blocks_size
```

我们集群 total blocks ≈ 180TB，1% ≈ 1.8TB，按 12 个 store-gateway、RF=3 算，每 pod 至少 450GB。实际我们用 700GB gp3。

**踩坑**：第一次部署时 local disk 只给了 200GB，store gateway 一边下载 index-header 一边删，命中率不到 20%，查询 p99 超过 30s。扩到 700GB 之后命中率稳定在 95% 以上。

## 六、query frontend：拆分、缓存、限流

Frontend 最关键的三件事：

```yaml
frontend:
  align_queries_with_step: true
  log_queries_longer_than: 10s
  results_cache:
    backend: memcached
    memcached:
      addresses: dns+memcached.mimir.svc:11211
      timeout: 500ms
  query_sharding_enabled: true
  query_sharding_total_shards: 16

limits:
  split_instant_queries_by_interval: 1h
  split_queries_by_interval: 24h
  max_query_parallelism: 240
  max_cache_freshness: 10m
  max_query_lookback: 90d
  max_query_length: 720h
```

**query_sharding_enabled** 是 Mimir 相对于 Thanos 最大的查询性能优势。它把一个 PromQL 按 series hash shard 成 N 个子查询，每个子查询只处理一部分 series，然后在 frontend 合并。对于 `sum by(foo)(rate(...))` 这种聚合查询效果最明显，p99 从 20s+ 降到 2s 以下。

**results_cache 不能省**。Memcached 缓存 query frontend 的结果，对 Grafana dashboard 的周期性查询命中率一般能到 70% 以上。我们用的是 3 个 memcached pod，每个 16GB 内存。

**关于 split_queries_by_interval**：太大会单子查询太重，太小会 subquery 数量爆炸。我们选 24h，对 7d 查询切成 7 个子查询，再配合 query_sharding 的 16 shard，总共 112 个并发子查询，对 querier 的规模正好。

## 七、compactor：长周期查询的生命线

Compactor 做两件事：

1. **Vertical compaction**：合并同一时间窗口来自不同 ingester 的块，去重；
2. **Horizontal compaction**：把多个时间窗口合并成更大的块，默认策略 2h → 12h → 2d → 8d。

```yaml
compactor:
  data_dir: /data/compactor
  block_ranges: [2h, 12h, 24h, 48h, 168h]
  cleanup_interval: 15m
  tenant_cleanup_delay: 6h
  sharding_ring:
    kvstore:
      store: memberlist
  compaction_concurrency: 3
  deletion_delay: 12h
  max_compaction_parallelism: 1
```

### 为什么 compactor 经常追不上

Compactor 跟不上的症状：store gateway 里小 block 越来越多，查询 p99 变长，对象存储 API 成本上升。常见原因：

1. **单 compactor 实例**。compaction_concurrency 只是单实例内部并发，跨租户并行要靠 sharding ring。我们生产 4 个 compactor，每个 32c/128G。
2. **大租户把一个 compactor 卡死**。即使 sharding，一个超大租户的 compaction 可能跑 12h 以上。解决办法：对大租户单独开 split-and-merge，把 8d block 切小：

```yaml
limits:
  compactor_split_and_merge_shards: 8
  compactor_split_groups: 2
```

3. **对象存储 list 慢**。compactor 启动会 list 整个 bucket，大 bucket 上要几分钟。每次 compaction_cycle 也要 list，我们后来把 cleanup_interval 从 5m 调到 15m。

### 长保留期的影响

`compactor_blocks_retention_period` 设成 90d，意味着最大 block 是 8d 一个，90d 大约 12 个 block。如果要保留 13 个月，最好开更大的 block_range（比如 30d），否则 block 数太多拖慢查询。

## 八、HA pair 去重：HA Tracker

典型场景：两套 Prometheus HA 对同一批 target 采集，然后都 remote write 给 Mimir。Mimir 的 distributor 通过 HA tracker 去重：

```yaml
distributor:
  ha_tracker:
    enable_ha_tracker: true
    kvstore:
      store: consul
    ha_tracker_update_timeout: 15s
    ha_tracker_failover_timeout: 30s

limits:
  accept_ha_samples: true
  ha_cluster_label: __replica__
  ha_replica_label: cluster
```

Prometheus 需要在 external labels 里带上 `cluster` 和 `__replica__`：

```yaml
global:
  external_labels:
    cluster: prod-prom-ha
    __replica__: replica-a   # 另一台是 replica-b
```

Mimir 以 `(cluster, __replica__)` 为 key 做 leader election，同一时刻只接受 leader 的样本，failover 发生时在 30s 内切换。这样 Grafana 看到的指标没有重复。

**坑**：如果你忘了配 external labels，两套 Prometheus 都会被当成独立源，series 直接翻倍。

## 九、多租户：隔离到底到哪一层

Mimir 的多租户是通过 HTTP header `X-Scope-OrgID` 实现的。所有路径都是 tenant-aware 的：

- 对象存储按 tenant 前缀存；
- ingester 的 TSDB 按 tenant 分；
- store gateway / compactor 的 sharding ring 按 tenant 分配；
- limits 可以 per-tenant override。

典型 overrides 文件（Helm values 里）：

```yaml
overrides:
  team-a:
    ingestion_rate: 200000
    ingestion_burst_size: 2000000
    max_global_series_per_user: 5000000
    compactor_blocks_retention_period: 30d
    max_query_length: 180d
    max_query_parallelism: 120
  team-b:
    ingestion_rate: 1000000
    max_global_series_per_user: 100000000
    compactor_blocks_retention_period: 365d
```

租户级别不够的时候，可以通过 **nginx/auth proxy** 把一个大租户再切分成多个子租户。我们早期把所有业务放一个 tenant，后来出过一次雪崩，改成 **一个业务线一个 tenant**，隔离效果立竿见影。

### 配额监控

装 `overrides-exporter`，暴露所有 per-tenant limits 为指标：

```
cortex_overrides{limit_name="max_global_series_per_user",user="team-a"} 5000000
```

配合 `cortex_ingester_memory_series_created_total` 做使用率告警：

```promql
(
  sum by(user) (cortex_ingester_memory_series{user=~".+"})
  /
  max by(user) (cortex_overrides{limit_name="max_global_series_per_user"})
) > 0.8
```

## 十、事故复盘：compactor 雪崩导致查询全挂

时间：2025 年 2 月一个周三凌晨。现象：所有 Grafana 查询超过 1h 的都超时，24h 内的查询正常。

**根因链**：

1. 周二晚一个新业务上线，label `container_id` 高基数，每 2h 产生 1.2GB block。
2. 该租户的 2h 块每天 12 个，周末积了 60 个，compactor 从 12h → 2d 合并时，单个 merge 要读 14GB block。
3. Compactor 本地磁盘 300GB，被这个租户的 merge 占满，其他租户的 compaction 全部排队。
4. Store gateway 发现 12h+ 范围没有 compacted block，只能从 2h 块里查，内存不够 OOM。
5. 查询超过 24h 的全部 5xx。

**应急**：

1. 先把 compactor 本地磁盘扩到 1TB；
2. 给这个租户临时调小 `max_global_series_per_user` 刹车；
3. 手动触发 deletion mark 清理已完成的 block；
4. 重启 store gateway 让它重新 shuffle。

**后续改进**：

- 加 `cortex_compactor_block_cleanup_failures_total` 和 `cortex_compactor_runs_failed_total` 告警；
- 给 compactor 的本地磁盘做配额隔离，按租户限；
- 建立新业务接入前的 series cardinality 评估流程，所有新指标要先做 cardinality 预估；
- 把 compactor 的 split-and-merge 打开。

## 十一、事故复盘：HA tracker 脑裂导致样本翻倍

时间：2025 年 8 月。现象：某业务指标突然翻倍，图表上直接变成 2 倍台阶。

**根因**：consul 集群因为磁盘满短暂不可用 40s，Mimir HA tracker 无法更新 leader，两个 replica 的样本都被接收，导致同一 (labels, timestamp) 的样本被 append 两次。

**应急**：

1. 先把 consul 磁盘救活；
2. 用 `PromQL: sum without(__replica__)` 临时规避；
3. Mimir compactor 的垂直 compaction 会在下一个 2h block 合并时自动去重，不用手动干预历史数据。

**改进**：

- consul 换成 etcd，且 etcd 独立部署监控；
- HA tracker 的 `ha_tracker_failover_timeout` 改短到 15s，避免长时间无 leader；
- 研究替换成 memberlist 的方案（2.12+ 支持）。

## 十二、迁移路线图：从 Prometheus / Thanos 到 Mimir

如果你现在要做迁移，我的建议路线：

1. **先双写**。用 Prometheus 的 remote_write 同时写 Thanos/Cortex/Mimir 和原存储，跑两周。
2. **配 Grafana datasource 做 A/B**，两边数据源切换对比 dashboard 是否一致。
3. **Ruler 和 Alertmanager 不要一起迁**。先迁存储和查询，告警等稳定后再搬。
4. **数据回填**。Mimir 的 `-blocks-storage.tsdb.block-upload-enabled=true` 支持历史 block 上传，但有限制：block 必须满足 Mimir 的 compaction 边界、不能和已有 block 重叠。实践中我们只回填了 30d，更久的数据放 Thanos 兼容查询。
5. **兼容查询**：Mimir 提供 `-querier.query-store-after` 来控制查询何时下沉到 store gateway，配置 0s 可以全部走 store。
6. **下线 Prometheus 本地盘**。最后一步把 Prometheus 缩成 1h retention，只做 scrape + remote write。

## 十三、成本优化杂谈

Mimir 的成本大头有三块：对象存储（主要是 API 和存储量）、compute（ingester 内存）、网络（跨 AZ）。

1. **对象存储存储量**：用 Zstandard chunk encoding（2.14+）可以再省 20%~30%。
2. **对象存储 API**：compactor 的 list/get 是大头，每次 cleanup 都要扫全 bucket。调大 `cleanup_interval` 和用 `-compactor.skip-blocks-with-out-of-order-chunks-enabled` 减少重复处理。
3. **跨 AZ 流量**：classic 架构里 distributor → ingester 跨 AZ 是 hot path，开 `zone_aware_replication` 让 RF=3 强制跨 3 AZ，然后 distributor 选同 AZ 的 ingester 作为 leader：

```yaml
ingester:
  ring:
    zone_awareness_enabled: true
    instance_availability_zone: ${ZONE}
distributor:
  ring:
    zone_awareness_enabled: true
```

4. **冷热分层**：store gateway 的 index header 用 SSD，chunks 可以用对象存储的 IA 层，数月不读的 block 可以自动降级。

迁到 ingest storage 之后，跨 AZ 成本再降一档，因为 Kafka 内部做副本，distributor 不再直接跨 AZ 复制。

## 十四、生产配置骨架

```yaml
multitenancy_enabled: true

common:
  storage:
    backend: s3
    s3:
      bucket_name: mimir-prod-blocks
      region: ap-southeast-1

blocks_storage:
  backend: s3
  bucket_store:
    sync_dir: /data/tsdb-sync
    index_cache:
      backend: memcached
      memcached:
        addresses: dns+idx-cache.mimir.svc:11211
    chunks_cache:
      backend: memcached
      memcached:
        addresses: dns+chunks-cache.mimir.svc:11211
  tsdb:
    dir: /data/tsdb
    retention_period: 13h
    wal_compression_enabled: true

distributor:
  ha_tracker:
    enable_ha_tracker: true
    kvstore:
      store: etcd
      etcd:
        endpoints:
          - etcd.mimir.svc:2379

ingester:
  ring:
    replication_factor: 3
    zone_awareness_enabled: true
    kvstore:
      store: memberlist

store_gateway:
  sharding_ring:
    replication_factor: 3
    zone_awareness_enabled: true
    kvstore:
      store: memberlist

compactor:
  sharding_ring:
    kvstore:
      store: memberlist
  cleanup_interval: 15m
  compaction_concurrency: 3

frontend:
  query_sharding_enabled: true
  query_sharding_total_shards: 16
  results_cache:
    backend: memcached
    memcached:
      addresses: dns+results-cache.mimir.svc:11211

limits:
  ingestion_rate: 500000
  ingestion_burst_size: 5000000
  max_global_series_per_user: 20000000
  max_global_series_per_metric: 2000000
  max_label_names_per_series: 40
  compactor_blocks_retention_period: 90d
  split_queries_by_interval: 24h
  max_query_parallelism: 240
  max_query_length: 2160h
```

## 十五、自监控要点

Mimir 的 `runbook` 仓库里有一份 mixin，可以直接用。我在生产上一定盯的几个指标：

- **写入**：`cortex_distributor_received_samples_total`、`cortex_ingester_ingested_samples_failures_total`；
- **ingester 内存 series**：`cortex_ingester_memory_series`；
- **ingester WAL 落后**：`cortex_ingester_wal_replay_duration_seconds`；
- **block 上传**：`cortex_ingester_shipper_uploads_total`、`cortex_ingester_shipper_upload_failures_total`；
- **compactor**：`cortex_compactor_runs_failed_total`、`cortex_compactor_block_cleanup_failures_total`；
- **store gateway**：`cortex_bucket_store_block_loads_total`、`cortex_bucket_store_sync_failures_total`；
- **query frontend**：`cortex_query_frontend_queries_in_progress`、`cortex_query_frontend_retries`；
- **对象存储 API**：`thanos_objstore_bucket_operations_total`（按 bucket 和 operation 聚合）。

## 十六、写在最后

Mimir 不算简单，但和 Cortex / Thanos 比，它把运维心智负担降下来了一档，3.x 的 ingest storage 更是把 classic 架构最痛的扩缩容问题直接解掉。

纠结选型的话我给几个快速判断：

- **series < 1 亿，团队精力有限** → Grafana Cloud 或者 VictoriaMetrics；
- **series 1~10 亿，有 K8s 运维能力** → Mimir classic；
- **series > 10 亿 或 对扩缩容弹性要求高** → Mimir ingest storage（准备 Kafka）；
- **需要 PromQL 完全兼容 + 多租户** → Mimir 不用犹豫。

## 参考资料

- Grafana Mimir 官方文档（classic / ingest storage architecture、compactor、store-gateway 章节）
- Grafana 博客 Mimir 3.0 release notes
- Grafana Mimir GitHub runbooks 仓库
- Grafana Mimir mixin dashboards
