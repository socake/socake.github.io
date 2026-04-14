---
title: "Loki 架构深度解析：从写入路径到 PB 级日志查询优化"
date: 2025-06-05T10:00:00+08:00
draft: false
tags: ["Loki", "日志", "可观测性", "LogQL", "TSDB"]
categories: ["可观测性"]
description: "一篇从写入路径、TSDB 索引、对象存储布局、查询分片到 Bloom 过滤器的 Loki 3.x 生产实战手册，配合我们线上 200TB/月日志平台的真实参数和踩坑记录。"
summary: "围绕 Loki 3.x 架构拆解写入、索引、查询三条链路，给出 schema_config、compactor、bloom、TSDB 的可直接复用配置，并复盘两次线上事故带来的调参经验。"
toc: true
math: false
diagram: false
keywords: ["Loki", "LogQL", "TSDB", "Bloom Filter", "对象存储"]
params:
  reading_time: true
---

## 为什么重新写一篇 Loki 架构

我们团队在 2022 年底把 ELK 换成了 Loki，那时还是 2.6。一路从 2.6 升到 2.8、2.9，再到 3.0、3.1、3.3，踩过的坑远比 Grafana 官方博客描述的多。今天这篇文章不是官方 doc 的翻译，而是带着 200TB/月 日志量、600+ 租户的生产环境回头看 Loki：哪些参数必须调、哪些设计你只有出过事故才会真正理解、哪些新功能可以放心上、哪些还得再等半年。

文章按照「写→存→读」三条链路展开，中间穿插两次真实事故。读完之后，你至少能做到：

1. 给新环境写一份可直接上生产的 `loki.yaml`，不会因为默认值翻车；
2. 在查询慢的时候，能判断瓶颈在 ingester、index gateway、querier、store-gateway 还是对象存储；
3. 知道 bloom 该不该开、TSDB 该怎么配、compactor 该分几个实例。

## 一、整体架构与组件职责

Loki 的组件拓扑相比 Prometheus 更复杂，因为它既要做写入侧的分布式哈希环（类似 Cortex），又要做读侧的对象存储回源。3.x 版本稳定下来的核心组件有下面几组。

### 写入链路

- **Distributor**：无状态，接收来自 Promtail、Alloy、Vector、OTel Collector 的推送。它做两件事：一是对 stream（一组 label 确定的唯一流）做校验（label 数量、长度、速率限制），二是按一致性哈希把同一个 stream 均匀打到 N 个 ingester。
- **Ingester**：有状态，维护内存里的 chunk，每个 stream 一个 chunk builder。Chunk 按大小或时间切分，满了之后 flush 到对象存储并写索引。Ingester 通过 memberlist 或 consul/etcd 组成哈希环。
- **Compactor**：负责把 boltdb-shipper/tsdb-shipper 的索引碎片合并成按天的大索引文件，同时执行 retention 删除和 delete request 处理。3.x 里 compactor 还承担 custom retention 的执行。

### 读链路

- **Query Frontend**：无状态，但承担了非常关键的 split/shard 工作。一个 24h 的 LogQL 查询，frontend 会按 `split_queries_by_interval`（通常 30m 或 1h）拆成若干子查询，再按 TSDB 的统计做 shard，扔进一个内部 queue。
- **Query Scheduler**（可选，推荐开）：3.x 里独立成单独组件，承载 frontend 和 querier 之间的 queue。开了 scheduler 之后，frontend 和 querier 都可以随意水平扩缩容而不会互相耦合。
- **Querier**：从 queue 拉子查询。查询路径分两段：最近数据向 ingester 拿，历史数据向 store（对象存储 + 索引）拿，最后合并。
- **Index Gateway**：3.x 的 TSDB 必选组件。Index 不再跟 querier 耦合，而是由 index gateway 从对象存储拉 TSDB 文件到本地，querier 通过 gRPC 问它「给我这段时间里匹配 `{app="foo"} |= "bar"` 的 chunk 列表」。
- **Bloom Gateway** + **Bloom Compactor**（3.0 起实验性，3.3 增强）：为「针尖麦芒」类查询提供 bloom 过滤。

### Ruler

独立一组实例，跑记录规则和告警规则。LogQL 的 `metric query` 在这里定时执行，结果推给 Prometheus 或 Mimir 里的 remote write 接收端。

我们线上的组件分布大致是：distributor 12 个，ingester 30 个（每个 10c/48G），querier 40 个（10c/16G），query-frontend 6 个，query-scheduler 3 个，index-gateway 8 个，compactor 3 个（主从），bloom-compactor 和 bloom-gateway 各 4 个，ruler 6 个。

## 二、一张贯穿全文的写入时序图

先用一段伪时序把写入链路讲清楚：

```
Promtail/Alloy
   │  POST /loki/api/v1/push  (snappy + protobuf)
   ▼
Distributor
   │ 1. validate (labels/rate/size)
   │ 2. stream = hash(labels)  -> ring
   │ 3. replicate to N ingesters
   ▼
Ingester (N=3)
   │ 1. append to in-memory chunk per stream
   │ 2. chunk full? flush:
   │     - upload chunk to object store (S3/GCS/OSS)
   │     - append index entry to TSDB WAL
   │     - sync TSDB head
   ▼
Shipper (embedded in ingester)
   │ 1. rotate TSDB head -> .tsdb file
   │ 2. upload tsdb file to object store: index/tsdb/<tenant>/<date>/
   ▼
Compactor
   │ 1. merge small tsdb files -> daily compacted file
   │ 2. apply retention + delete requests
```

这里有几点是刚上手 Loki 的人容易忽略的：

1. **Chunk 和 index 是两种完全不同的文件**。chunk 是压缩后的原始日志 + 结构化 metadata，大小以 MB 计；index 是标签倒排 + chunk 引用，大小以百 KB 到几 MB 计，数量比 chunk 少得多。
2. **Ingester 既是写入组件也是「最近数据的读组件」**。Loki 的查询路径会优先问 ingester 要还没 flush 的热数据，不要以为查询只走对象存储。
3. **Shipper 是内嵌在 ingester/querier/compactor 里的一段代码**，不是独立进程。升级时这三类 pod 都要滚动。

## 三、schema_config：一次配错，半年恶心

`schema_config` 是整个 Loki 最需要谨慎对待的配置，因为它决定了历史数据的存储格式，一旦写入就不能随便改。3.x 推荐的 schema 是 `v13` + TSDB：

```yaml
schema_config:
  configs:
    - from: 2023-07-01
      store: tsdb
      object_store: s3
      schema: v13
      index:
        prefix: loki_index_
        period: 24h
```

几个关键点逐一说明：

- `from`：不是「这个配置从什么时候生效」，而是「用这套 schema 写入的第一天日期」。改 schema 意味着**追加**一条新 config，老 config 继续负责历史数据，不要删！
- `store: tsdb`：2.8 之后默认且推荐，相比 boltdb-shipper 在查询规划、shard、bloom 集成上都更好。
- `schema: v13`：3.x 里新增 `structured metadata` 必须依赖 v13，v12 及以下不支持；OTel 接入、trace_id 索引都会用到 structured metadata。
- `index.period: 24h`：每天一个索引桶，和 compactor 对齐。不要改成 12h 或 1h，除非你清楚 compactor、retention、delete request 的行为。

我们在 2024 年犯过一个错：把老集群从 v11 迁到 v13 时，直接把老 config 改成新 schema，结果所有 2023 年的数据都查不出来。后来只能从对象存储备份中回滚，重新以追加方式写：

```yaml
schema_config:
  configs:
    - from: 2022-10-01   # 老数据
      store: boltdb-shipper
      object_store: s3
      schema: v11
      index:
        prefix: loki_idx_
        period: 24h
    - from: 2023-07-01   # 新数据
      store: tsdb
      object_store: s3
      schema: v13
      index:
        prefix: loki_tsdb_idx_
        period: 24h
```

**教训**：`schema_config.configs` 是追加日志，不是覆盖表。任何已经写过数据的 schema config 都不能改动 `from` 或 `prefix`，否则历史数据的读路径直接断掉。

## 四、对象存储布局：以 S3 为例

Loki 把对象存储当成唯一真相来源，索引 shipper 只是本地加速。以 S3 的 bucket 结构为例：

```
s3://my-loki-prod/
├── fake/                  # 单租户时默认 tenant id
│   └── index/tsdb/
│       └── 19845/         # 天级编号 = days since epoch
│           └── tsdb-...tsdb
├── tenants/
│   └── team-a/
│       ├── index/tsdb/19845/...
│       └── chunks/
│           └── <stream-hash>/
│               └── <chunk-id>
```

注意几点：

1. **chunks 下面是 stream hash 的一级目录**，会产生海量小文件，不要在 S3 上开「列表全桶」类的任务，否则 cost 爆炸。
2. **索引和 chunk 要同一个 bucket 吗？** 不强制。生产上我们做过分桶：索引放 gp3/standard，chunk 放 standard-IA，成本降到 1/3。
3. **structured metadata 不是走单独桶**，它内嵌在 chunk 里，通过 v13 schema 支持。

### 对象存储的一致性坑

S3 在 2020 年之后就是强一致的，但 aliyun OSS 和一些 MinIO 集群不是强一致。我们在私有化版本上碰到过 flush 成功之后 `HeadObject` 返回 404，ingester 把 chunk 当成未上传重试，导致同一条日志在查询时重复。解决办法是把 `ingester.chunk_retain_period` 从默认 30s 调到 5m，并确保对象存储开 `consistency: strong`。

## 五、TSDB 索引：你真的理解它在做什么吗？

TSDB 是 Prometheus 的索引格式，Loki 2.8 搬了过来。它解决了 boltdb-shipper 时代的两个老问题：

1. 查询规划阶段不知道某个 shard 到底包含多少数据，只能盲目 shard；
2. 对象存储上海量的小 boltdb 文件难以 compact，compactor 经常追不上。

TSDB 在 Loki 里长这样（简化）：

```
postings:  label -> [series_id, ...]
series:    series_id -> { labels, chunk_refs[] }
chunk_ref: { from, through, checksum, KB, entries }
```

注意 `chunk_ref` 里有 KB 和 entries 两个字段，它们是 TSDB 相对 boltdb 最核心的改进：Query Frontend 可以在规划阶段就知道这个子查询要过多少数据，从而决定分多少 shard。这叫 **Dynamic Query Sharding**，是 3.x 查询性能的基石。

在 `limits_config` 里控制它：

```yaml
limits_config:
  tsdb_max_query_parallelism: 512
  split_queries_by_interval: 30m
  query_ready_index_num_days: 7
  max_query_series: 5000
  max_query_parallelism: 32
  max_entries_limit_per_query: 100000
```

我们踩过的坑：

- `tsdb_max_query_parallelism` 默认 128，对 PB 级查询太小。我们按 querier 数量 * 16 来调，保证每个子查询都能吃满一个 querier 的 CPU。
- `split_queries_by_interval` 不要调得太短。一个 24h 查询切成 48 个 30min 子查询是合理的；如果切成 1440 个 1min 子查询，frontend 自己的开销就压死它了。
- `query_ready_index_num_days` 决定 index gateway 启动时预加载多少天的索引到本地 SSD，太大会 OOM，太小会在第一次查询时卡半分钟。

## 六、Chunk 生命周期与 ingester 调参

一个 chunk 从诞生到上传分四个状态：

1. `active`：正在接收新日志；
2. `closed`：达到 `chunk_target_size` 或 `max_chunk_age` 时关闭；
3. `flushed`：上传完对象存储，TSDB 里写了引用；
4. `retained`：仍然留在 ingester 内存中，为了应对 ingester 挂掉时副本还没同步完。

对应的配置：

```yaml
ingester:
  chunk_idle_period: 30m          # stream 空闲多久后 flush
  chunk_target_size: 1572864      # 1.5MB, 压缩前
  chunk_encoding: snappy          # 快速+压缩比均衡
  max_chunk_age: 2h               # chunk 最大存活
  chunk_block_size: 262144        # 256KB, 每次 seek 单位
  chunk_retain_period: 5m         # 上传后内存保留时间
  wal:
    enabled: true
    dir: /var/loki/wal
    checkpoint_duration: 5m
    flush_on_shutdown: true
    replay_memory_ceiling: 4GB
```

几个容易出事的点：

- **`chunk_target_size` 不要超过 4MB**。Loki 的 chunk 读取时是整块加载到内存的，太大会让 querier OOM。我们最终选 1.5MB 作为甜点区。
- **`max_chunk_age` 必须大于 `chunk_idle_period`**，否则会出现高频 stream 被反复切分为小 chunk，查询时要扫更多文件。
- **WAL 不能关**。关了之后 ingester 重启等于丢数据，Loki 的 replica_factor=3 只是为了 flush 前的冗余，不是持久化。
- **`replay_memory_ceiling` 一定要设**。我们出过一次事故：ingester OOM 重启后开始 replay WAL，但没有内存上限，replay 过程中把自己又 OOM 一次，陷入循环。设置之后 replay 会按 ceiling 节奏执行，超了就丢最老的数据。

## 七、查询路径详解：一次 LogQL 的旅程

以这条常见查询为例：

```logql
sum by (status) (
  rate({cluster="prod", app="api-gateway"} 
    |= "trace_id=abc123" 
    | json 
    | status >= 500 [5m])
)
```

它会经过下面这些步骤：

1. **Query Frontend 接收**。frontend 拿到 start/end 后按 `split_queries_by_interval=30m` 切分。假设查 6h，切成 12 份。
2. **TSDB 规划 shard**。frontend 请求 index gateway：这 12 个 30min 窗口里，`{cluster="prod",app="api-gateway"}` 匹配的 series 有多少、chunk 有多少 KB？index gateway 基于 TSDB 里的 `KB` 字段返回统计。frontend 按「每个子查询 processor 大约处理 300MB 为目标」计算 shard 数，假设每个 30min 有 1.2GB，就 shard=4，总共 48 个子查询。
3. **Scheduler 排队**。frontend 把 48 个子查询塞进 scheduler 的队列，按租户 round-robin 出队。
4. **Querier 拉取**。每个 querier 从 scheduler 拿子查询，先问 ingester 要 `max_chunk_age + chunk_idle_period` 范围内的数据（最近 2~3 小时），再问 index gateway 要 chunk 列表，最后从对象存储下载 chunk。
5. **Bloom 过滤（可选）**。如果启用了 bloom gateway，且 LogQL 里有 `|= "trace_id=abc123"` 这种字面量过滤，frontend 会先问 bloom gateway：这些 chunk 里哪些**可能**包含 "abc123"？typical 可以过滤掉 80%+ 的 chunk。
6. **Chunk decode**。querier 把 chunk 从 snappy 解出来，按 block 遍历，对每行执行 `|= "trace_id=abc123"` 和 `| json | status >= 500` 两段 pipeline。
7. **聚合**。querier 把 `rate()` 按 5m 步长计算的结果返回给 frontend，frontend 合并 48 个子结果做 `sum by (status)` 最终返回给 Grafana。

这就是为什么我在 Loki 排障时一定先看几个指标：

- `loki_query_frontend_queries_total{status="503"}`：frontend 拒绝了多少请求；
- `loki_querier_store_chunks_downloaded_total`：真实下载量，和 bloom 命中率反向；
- `loki_ingester_chunks_flushed_total`：flush 速度，落后会造成 ingester OOM；
- `cortex_tsdb_loaded_blocks`：index gateway 本地加载的 TSDB 数量。

## 八、Bloom Filter：值不值得开

3.0 推出，3.1 增强，3.3 稳定化。官方建议日志量 > 75TB/月 才值得开。它的工作方式是：

- `bloom-compactor` 定时把 chunk 里的 tokens（通常是 n-gram 切分后的词）编码成 bloom 位图，存到对象存储。
- `bloom-gateway` 接收 frontend 的过滤请求，返回不可能匹配的 chunk 集合，frontend 据此 prune。

开启方式（3.3）：

```yaml
bloom_build:
  enabled: true
  planner:
    planning_interval: 6h
  builder:
    planner_address: bloom-planner.loki.svc:9095

bloom_gateway:
  enabled: true
  client:
    addresses: dns+bloom-gateway-headless.loki.svc:9095

limits_config:
  bloom_creation_enabled: true
  bloom_gateway_enable_filtering: true
  bloom_ngram_length: 4
  bloom_ngram_skip: 1
```

我们线上的实际收益：trace_id 类针尖麦芒查询命中率 92%，平均耗时从 38s 降到 6s；grep 类查询命中率只有 20%~30%，因为 bloom 对短字符串效果差。cost 方面：bloom 文件占 chunk 总量的 1%~2%，compactor 额外开销约 20% CPU。

**不要开 bloom 的情况**：

- 日志量 < 20TB/月，bloom 的维护开销大于收益；
- 查询以结构化过滤为主（`| json | level="error"`），bloom 不参与；
- 对象存储是私有化 MinIO 且 IOPS 紧张，bloom 会显著抬高请求率。

## 九、Compactor：决定 retention 能不能按时执行

```yaml
compactor:
  working_directory: /var/loki/compactor
  compaction_interval: 10m
  retention_enabled: true
  retention_delete_delay: 2h
  retention_delete_worker_count: 150
  delete_request_store: s3
  delete_max_interval: 24h
```

踩坑记录：

- **`retention_delete_worker_count` 默认 150 太小**。我们一天有 6000 万个过期 chunk，单 compactor 跑一天跑不完，最终调到 800，并把 compactor 换成 32c 机型。
- **`compactor` 不是无状态的**。它在同一时刻只允许一个实例真正执行 compaction 和 retention，其他实例 standby。通过 `ring` 做 leader 选举。所以副本数建议 2~3，不要 10 个。
- **custom retention 在 overrides 里配置**：

```yaml
overrides:
  team-a:
    retention_period: 720h
  team-b:
    retention_period: 2160h
    retention_stream:
      - selector: '{app="audit"}'
        period: 8760h
```

## 十、事故复盘：一次 TSDB OOM 雪崩

时间：2025 年 3 月的一个周五下午。现象：index gateway 集群 8 个 pod 依次 OOM，整个查询面瘫痪 27 分钟。

**背景**：一个算法团队接入了新业务，给日志加了 `experiment_id` 这个 label，每天 80 万个 unique 值。我们的 label 数量报警阈值是 100 万/天，没触发。

**第一阶段：TSDB 膨胀**。experiment_id 作为 label 进 TSDB 之后，单日 TSDB 文件从 1.2GB 涨到 11GB。每个 index gateway 默认加载过去 7 天 TSDB，即 77GB 本地常驻，pod 内存限制 32GB。

**第二阶段：连锁 OOM**。第一个 pod OOM 后，流量被 K8s Service 打到剩下 7 个 pod，加载速度加快，第二个 pod 跟着 OOM，雪崩开始。

**应急**：
1. 把 `query_ready_index_num_days` 从 7 降到 2，减少常驻量；
2. 扩 index gateway 内存到 64GB；
3. 在 distributor 加 label drop：`experiment_id` 进 structured metadata 而不是 label；
4. 给 algo 团队做 label 方案评审。

**后续改进**：
- 建立 label cardinality 告警：`loki_ingester_memory_streams{tenant="..."}` 超过 200 万告警；
- 加了 `max_label_names_per_series` 和 `max_global_streams_per_user` 的 per-tenant 配置；
- 写了一个脚本每天扫描 TSDB 文件，发现 cardinality top 10 的 label，发给 owner。

**根本教训**：Loki 的 TSDB 和 Prometheus 一样，label 基数是第一杀手。所有从 ELK 迁移来的团队都需要重新培训：structured metadata 才是放高基数字段的地方。

## 十一、事故复盘：对象存储限流导致 flush 堆积

时间：2025 年 7 月。现象：ingester 内存使用从 40% 开始线性上涨，2 小时后全部 OOM。

**根因**：S3 bucket 开了 KMS SSE，KMS 账户级并发限制 500。当时刚上线一个新业务，chunk flush 速率从 800/s 涨到 1600/s，S3 返回 `KMS ThrottlingException`，ingester 按指数退避重试，flush 堵住，chunk 在内存里积压。

**应急**：
1. 立即扩 ingester 内存到 96GB 缓冲 OOM；
2. 向 AWS 申请临时提高 KMS 并发到 2000；
3. 把 KMS 换成 bucket key 模式，降低 KMS 调用频次。

**后续改进**：
- 监控加 `loki_ingester_chunks_flushed_total` vs `loki_ingester_chunks_created_total` 的差值告警；
- 压测时 mock 了 S3 429，验证 ingester 退避行为；
- 给对象存储加 per-bucket 的 throttling 告警。

## 十二、生产最小配置清单（可直接抄）

```yaml
auth_enabled: true

server:
  http_listen_port: 3100
  grpc_listen_port: 9095
  grpc_server_max_recv_msg_size: 67108864
  grpc_server_max_send_msg_size: 67108864
  log_level: info

common:
  replication_factor: 3
  path_prefix: /var/loki
  storage:
    s3:
      bucketnames: my-loki-prod
      region: ap-southeast-1
      s3forcepathstyle: false
  ring:
    kvstore:
      store: memberlist

memberlist:
  join_members:
    - loki-memberlist.loki.svc.cluster.local:7946

schema_config:
  configs:
    - from: 2023-07-01
      store: tsdb
      object_store: s3
      schema: v13
      index:
        prefix: loki_tsdb_idx_
        period: 24h

ingester:
  lifecycler:
    ring:
      kvstore:
        store: memberlist
  chunk_idle_period: 30m
  chunk_target_size: 1572864
  chunk_encoding: snappy
  max_chunk_age: 2h
  chunk_retain_period: 5m
  wal:
    enabled: true
    dir: /var/loki/wal
    flush_on_shutdown: true
    replay_memory_ceiling: 4GB

compactor:
  working_directory: /var/loki/compactor
  compaction_interval: 10m
  retention_enabled: true
  retention_delete_delay: 2h
  retention_delete_worker_count: 800
  delete_request_store: s3

query_scheduler:
  max_outstanding_requests_per_tenant: 2048

frontend:
  max_outstanding_per_tenant: 2048
  compress_responses: true
  log_queries_longer_than: 10s
  scheduler_address: query-scheduler.loki.svc:9095

querier:
  max_concurrent: 8
  query_ingesters_within: 3h

limits_config:
  ingestion_rate_mb: 20
  ingestion_burst_size_mb: 40
  max_global_streams_per_user: 500000
  max_query_parallelism: 32
  tsdb_max_query_parallelism: 512
  split_queries_by_interval: 30m
  max_entries_limit_per_query: 100000
  max_query_series: 5000
  max_query_length: 721h
  query_ready_index_num_days: 2
  retention_period: 720h
  volume_enabled: true
```

## 十三、观测 Loki 自己

一套最小但够用的 Loki 自监控面板应该包含：

1. **写入侧**：distributor 接收速率、ingester flush 堆积、WAL replay 时间；
2. **读侧**：query_frontend QPS、p95/p99、scheduler queue length、querier 并发；
3. **存储侧**：对象存储 4xx/5xx、S3 latency、KMS throttling；
4. **索引侧**：index gateway 本地 cache 命中率、TSDB 文件总大小；
5. **compactor**：compaction 耗时、retention 积压、delete request 执行时间；
6. **租户级**：每租户 ingestion rate vs 配额、stream 数、查询 QPS、平均耗时。

自监控 dashboard 官方有现成的 `loki-mixin`，拉下来之后再针对本地口径微调即可。

## 十四、和 Mimir / Tempo 的联动

可观测性三件套真正发挥价值是在三者 join 起来之后。我们的做法：

- Loki 的 LogQL 加 `| json | trace_id != ""` 提取 trace_id，Grafana Dashboard 配 `dataLinks` 直接跳 Tempo。
- Ruler 把 LogQL metric query 推到 Mimir，例如 `sum by(app) (rate({env="prod"} |= "ERROR" [5m]))`，它就变成了一个 Prometheus 可查的指标。
- Tempo 的 trace 详情里通过 Loki datasource 反向查日志，用 `{cluster="prod"} | json | trace_id = "$trace_id"`。

Grafana 11 之后 Explore 的三列联动已经非常顺滑，是整合三件套性价比最高的 UI 投入。

## 十五、未来方向与建议

2025 年底看 Loki，我认为它已经度过了青春期。不会再像 2.x 时代每两个月出一次 breaking change。如果你刚开始上 Loki，我的建议是：

1. **直接上 3.3+**，不要再从 2.x 起步，schema 直接 v13；
2. **replication_factor=3，ingester 32c/48G 起步**，后期按 stream 数扩；
3. **TSDB + index gateway + query scheduler 三件套一起上**，不要图省事合并组件；
4. **租户 label 规范先立起来**：禁止把高基数字段写成 label，提供 structured metadata 的接入模板；
5. **bloom 先观望半年**，等你日志量真的到 50TB/月以上再开；
6. **retention 和 compactor 从第一天就配好**，否则对象存储成本会慢慢爆掉。

可观测性这件事的复杂度，一半在工具，一半在规范。Loki 本身的架构已经够优秀，剩下的坑多数是人给挖的。希望这篇文章能帮你提前绕过其中大部分。

## 参考资料

- Grafana Loki 官方文档，3.x architecture / TSDB / Bloom 章节
- Grafana 博客：Loki 3.0 release: Bloom filters, native OpenTelemetry support
- Grafana 博客：Loki query acceleration: How we sped up queries without adding resources
- Grafana Loki GitHub release notes v3.0 ~ v3.3
