---
title: "Doris 与 StarRocks：一次严肃的生产选型笔记"
date: 2025-01-22T15:30:00+08:00
draft: false
tags: ["Doris", "StarRocks", "OLAP", "数据仓库", "MPP"]
categories: ["数据库"]
description: "Apache Doris 和 StarRocks 是当前最火的两个开源实时 OLAP 引擎，也经常被拿来比较。本文从架构、性能、成熟度、运维、生态五个维度做严肃对比，结合我在两套生产集群上的运维经验，给出一份能直接用于选型决策的笔记。覆盖 Doris 3.0 和 StarRocks 3.3 两个较新的稳定版本。"
summary: 'Doris 和 StarRocks 同源、相似、又各有偏好。选哪个不是"谁更好"的问题，而是"谁更适合我们的场景"的问题。这篇文章是我在两套 OLAP 集群（一套 Doris、一套 StarRocks）上运维一年多后写的深度对比，希望能帮你跳过几个月的调研和踩坑。'
toc: true
math: false
diagram: false
keywords: ["Apache Doris", "StarRocks", "OLAP", "MPP", "向量化", "实时数仓"]
params:
  reading_time: true
---

## 写在前面

实时 OLAP 这几年成了大数据生态里竞争最激烈的赛道。2020 年之前大家还在纠结 ClickHouse vs Druid vs Presto，2021 年之后 Doris 和 StarRocks 几乎瓜分了国内这块市场。

这两个项目的关系很有意思：StarRocks 是前 Apache Doris PMC 成员在 2020 年 fork 出来的，原因是对 Doris 的发展路线有分歧。四年多过去了，两个项目各自发展，在 2025 年形成了既相似又差异化的格局。很多团队选型时反复纠结，我也一样，最后在两个不同业务上分别用了 Doris 和 StarRocks。这篇文章就是基于这个经历的对比笔记。

本文覆盖 Doris 3.0（2024 年发布）和 StarRocks 3.3（2024 年中发布）。

## 一、共同祖先：一眼看穿的相似

两个系统有大量共同设计：

- **MPP 架构**：查询并行执行
- **列式存储**：PAX-like 页格式、字典编码、位图索引
- **FE/BE 分离**：FE 是 Java 元数据 + 查询规划，BE 是 C++ 存储 + 执行
- **MySQL 协议**：客户端用 MySQL 驱动连
- **SQL 兼容**：大部分 MySQL SQL 能跑
- **Colocate Join**：同分布键的表 JOIN 在本地进行
- **Materialized View**：预计算加速
- **Routine Load**：从 Kafka 实时导入

所以你会发现很多基础操作（建表、查询、导入）两者几乎一样。这不是巧合，是共同的起源。

### 1.1 基础架构图

```
            +---------------+
            |  MySQL Client |
            +-------+-------+
                    |
             FE Leader/Follower
         (元数据、SQL 解析、计划)
                    |
     +--------------+---------------+
     |              |               |
  +--+--+        +--+--+         +--+--+
  |  BE |        |  BE |         |  BE |
  |存储+|        |存储+|         |存储+|
  |执行 |        |执行 |         |执行 |
  +-----+        +-----+         +-----+
  (多副本数据分片存储)
```

FE 通常 3 或 5 节点，用 bdbje（Doris）或 自研 Paxos（StarRocks）做元数据一致性。BE 节点数从 3 到几百，水平扩展。

## 二、存储模型：这才是差异的根源

Doris 和 StarRocks 的存储看似都是列式，但实现细节差异巨大，直接决定了它们的适用场景。

### 2.1 数据模型对比

Doris 和 StarRocks 都支持三种数据模型，但实现不同：

| 模型               | Doris                     | StarRocks                   |
|--------------------|---------------------------|-----------------------------|
| 明细 Duplicate     | 插入的每一行都保留       | 同 Doris                    |
| 聚合 Aggregate     | 按 key 聚合，预聚合存储   | 同 Doris                    |
| 主键 Unique/Primary| Unique Key Model          | Primary Key Model（更强）  |

StarRocks 的 Primary Key Model 是它的杀手锏。Doris Unique Key 用 merge-on-read（查询时合并多版本），StarRocks Primary Key 用 delete+insert（写入时合并），查询时**不需要 merge**，所以 StarRocks 的 upsert 场景查询性能明显优于 Doris。

实测：同样的 1 亿行上按主键更新 1000 万行，查询性能：

- StarRocks PK Model：100ms 级
- Doris Unique Key：500ms-1s

如果你的业务是大量实时 update/upsert + 复杂查询，StarRocks 有明显优势。

### 2.2 分区与分桶

两者都支持两级：分区（Partition）+ 分桶（Bucket）。

- **Partition**：粗粒度，通常按时间。裁剪查询范围。
- **Bucket**：细粒度，按哈希。并行度和 Colocate Join 的基础。

建表语法几乎一样：

```sql
CREATE TABLE orders (
  order_id BIGINT,
  user_id BIGINT,
  amount DECIMAL(10, 2),
  create_time DATETIME
)
DUPLICATE KEY(order_id, user_id)
PARTITION BY RANGE(create_time) (
  PARTITION p202401 VALUES [('2024-01-01'), ('2024-02-01')),
  PARTITION p202402 VALUES [('2024-02-01'), ('2024-03-01'))
)
DISTRIBUTED BY HASH(user_id) BUCKETS 32
PROPERTIES (
  "replication_num" = "3"
);
```

Bucket 数选择原则：

- 单个 bucket 数据量 1-10GB
- 总 bucket 数 = BE 数 × 2-4（让每个 BE 分到多个 bucket）
- 不要太多，每个 bucket 有元数据开销

### 2.3 Compaction

两者都有 compaction 合并小文件。差异是：

- **Doris**：Cumulative + Base + Full 三级 compaction，参数多
- **StarRocks**：Size-Tiered compaction，更智能

实测 Doris 在高导入场景下 compaction 压力更大，需要手动调参。StarRocks 的默认参数通常够用。

## 三、查询引擎：向量化和 Cost-based

### 3.1 向量化

StarRocks 的向量化引擎是从头写的，彻底。Doris 2.0 之后才开始全面向量化，3.0 基本追平。

在 TPC-H 1TB 这种标准 benchmark 上，StarRocks 目前仍略快（大概 10-30%），但差距比 2022 年小很多。对于绝大部分业务，两者性能差异不足以成为选型决定因素。

### 3.2 优化器

两者都有 CBO（Cost-based Optimizer），但成熟度有差异：

- **StarRocks CBO**：2022 年就完善，支持复杂 JOIN reorder
- **Doris CBO**：在 Nereids 优化器推出后追赶，3.0 达到生产可用

复杂多表 JOIN 场景 StarRocks 更稳，简单查询两者接近。

### 3.3 JOIN 能力

JOIN 是 OLAP 的硬核能力。StarRocks 支持：

- Broadcast Join
- Shuffle Join
- Colocate Join
- Bucket Shuffle Join
- Runtime Filter

Doris 3.0 也支持以上全部。性能上 StarRocks 在大多数场景略快，但没有代差。

### 3.4 Materialized View

物化视图是预计算加速的关键，两者都支持，但**StarRocks 的 MV 更强**：

- 支持异步刷新
- 支持多表 JOIN MV
- 自动查询改写（query rewrite）
- 基于 Iceberg/Hudi 的 MV

Doris 的 MV 在 3.0 才支持完整的多表 JOIN，查询改写能力还不如 StarRocks。

## 四、实时导入：Routine Load

两者都支持从 Kafka 实时导入，命令几乎一样：

```sql
CREATE ROUTINE LOAD mydb.orders_load ON orders
COLUMNS TERMINATED BY ",",
COLUMNS(order_id, user_id, amount, create_time)
PROPERTIES (
  "desired_concurrent_number" = "3",
  "max_batch_interval" = "10",
  "max_batch_rows" = "200000",
  "max_batch_size" = "104857600"
)
FROM KAFKA (
  "kafka_broker_list" = "broker:9092",
  "kafka_topic" = "orders",
  "property.group.id" = "orders-group"
);
```

差异：

- **StarRocks**：实时性略好，端到端延迟秒级
- **Doris**：延迟 1-5 秒

两者都支持 Stream Load（HTTP 推数据）、Broker Load（从 HDFS/S3）、Insert Into Select（从其他表）。

## 五、数据湖集成

这是 2024-2025 年两个项目的主战场：不仅做自己的存储，还能查外部数据湖（Iceberg / Hudi / Paimon / Delta Lake）。

StarRocks 在这块起步更早，3.0 就支持了完整的 Iceberg 读写；Doris 3.0 追上了读，写入还在完善。

典型用法：

```sql
-- 创建外部 catalog
CREATE EXTERNAL CATALOG iceberg_catalog
PROPERTIES (
  "type" = "iceberg",
  "iceberg.catalog.type" = "hive",
  "hive.metastore.uris" = "thrift://metastore:9083"
);

-- 直接查
SELECT * FROM iceberg_catalog.db.orders WHERE create_time > '2024-01-01';

-- 跟本地表 JOIN
SELECT o.*, u.name
FROM iceberg_catalog.db.orders o
JOIN local_catalog.users u ON o.user_id = u.id;
```

两者都支持这种"湖仓一体"能力，但在细节上 StarRocks 更成熟：

- StarRocks 的 Iceberg 连接器支持更多 Predicate Pushdown
- StarRocks 的元数据缓存更积极，重复查询快
- StarRocks 的 Data Cache 功能把远程湖数据本地化，重复查询接近本地表性能
- Doris 在 Paimon 上支持更早（两家都是 Paimon 早期贡献者）

## 六、Compute-Storage 分离架构

StarRocks 3.0 和 Doris 3.0 都发布了存算分离版本，把数据放 S3、计算节点无状态弹性伸缩。

```
+-----------+  +-----------+  +-----------+
|  FE(无状态)|  |  FE(无状态)|  |  FE(无状态)|
+-----+-----+  +-----+-----+  +-----+-----+
       \              |              /
        +-------------+-------------+
                      |
        +-------------+-------------+
        |             |             |
    +---+---+     +---+---+     +---+---+
    | CN    |     | CN    |     | CN    |
    | (无状态)|     | (无状态)|     | (无状态)|
    | + cache|     | + cache|     | + cache|
    +---+---+     +---+---+     +---+---+
        |             |             |
        +-------------+-------------+
                      |
              +-------+-------+
              |  S3 / OSS     |
              |  (持久化)     |
              +---------------+
```

优点：

1. 计算节点可以随时扩缩，分钟级
2. 存储按量付费，冷数据便宜
3. 多集群共享数据

缺点：

1. 首次查询延迟高（要从 S3 拉数据）
2. Cache 失效带来的性能抖动
3. 运维复杂度增加

**2025 年的状态**：两者的存算分离都还在快速迭代，**生产上线要谨慎**。核心场景建议还是用 shared-nothing 架构，把存算分离当作探索型项目先在非核心业务跑。

我自己的建议：2025 年上生产选 shared-nothing 版本，2026 年再评估存算分离。

## 七、运维体验

### 7.1 部署

两者都有二进制包和 Docker 镜像，部署复杂度相当。Kubernetes 支持：

- Doris 有官方 Operator（2023 年）
- StarRocks 有官方 Operator（2022 年），更成熟

如果在 K8s 上跑，StarRocks Operator 的体验更好。

### 7.2 备份与恢复

两者都支持备份到 S3/HDFS：

```sql
BACKUP SNAPSHOT mydb.snap1
TO broker_s3
ON (table1, table2)
PROPERTIES ("type" = "full");

RESTORE SNAPSHOT mydb.snap1
FROM broker_s3
ON (table1, table2);
```

流程几乎一致。恢复速度两者接近。

### 7.3 监控

两者都暴露 Prometheus metrics，都有官方 Grafana 模板。StarRocks 的 metrics 更细、更多。

核心告警：

```yaml
- alert: FELeaderMissing
  expr: up{job="fe_leader"} == 0
  for: 1m

- alert: BEDown
  expr: be_up == 0
  for: 2m

- alert: IngestionLag
  expr: routine_load_lag_seconds > 60
  for: 5m

- alert: CompactionBacklog
  expr: compaction_score > 300
  for: 10m
  annotations:
    summary: "Compaction backlog，导入太快或参数不够激进"

- alert: QueryFailed
  expr: rate(query_err_total[5m]) > 5
  for: 5m
```

`compaction_score` 是一个重要指标：它是待 compact 的 rowset 数，持续 > 100 就说明 compaction 跟不上导入，> 300 已经严重影响查询性能。

### 7.4 Schema 变更

两者都支持 Online Schema Change，但各有限制：

- **Doris**：Light Schema Change 支持加列/改类型的秒级完成
- **StarRocks**：类似能力，但对 Primary Key Model 限制较多

大表（亿行以上）加列两者都能秒级搞定，因为 light schema change 只改元数据。

## 八、几个真实的选型决策

### 8.1 业务 A：实时订单分析

**需求**：每秒 5000 订单写入，支持按用户、商户、时间多维分析，大量 upsert（订单状态变更）。

**选型**：StarRocks

**理由**：

1. Primary Key Model 处理 upsert 明显快
2. JOIN 性能更稳
3. 团队之前用过

**效果**：上线一年多，20 个节点扛住每天 5 亿订单更新，P99 查询 < 500ms。

### 8.2 业务 B：日志分析

**需求**：每天接入 5TB 日志，查询主要是 top-k、where 过滤、少量聚合，不涉及 update。

**选型**：Doris

**理由**：

1. 明细模型够用，不需要 primary key
2. Doris 在点查和简单过滤上性能已经够
3. 社区活跃度国内稍高、文档更多中文
4. 成本：Doris 这个规模运维成本稍低

**效果**：12 个节点支撑每天 5TB 入库，查询 P95 < 2 秒。

### 8.3 业务 C：BI 看板

**需求**：中台 BI 看板，几百张表、复杂 JOIN、物化视图加速。

**建议**：StarRocks

**理由**：

1. CBO 更成熟，复杂 JOIN 稳定
2. MV 的自动查询改写能力强
3. 湖仓集成更好，能直接查 Iceberg 底层

### 8.4 选型总结

| 场景                       | 推荐         |
|----------------------------|--------------|
| 大量 upsert + 查询         | StarRocks    |
| 日志/监控分析              | Doris        |
| 复杂 BI / 多表 JOIN        | StarRocks    |
| 点查 / 高频小查询          | Doris        |
| 湖仓一体                   | StarRocks    |
| 团队熟 Apache 生态         | Doris        |
| 团队更看重稳定性           | StarRocks    |

## 九、踩坑与经验

### 9.1 Bucket 数选错导致性能灾难

**现象**：某张大表查询极慢，明明只查 1 分钟数据。

**根因**：bucket 数设的是 128，但实际只有 16 个 BE。每次查询都要在 128 个 bucket 上并行，而每个 BE 要跑 8 个 bucket，线程调度开销巨大。

**修复**：重建表，bucket 数改为 64（BE 数 × 4）。

**教训**：bucket 数不是越多越好，和 BE 数要匹配。

### 9.2 Compaction Backlog

**现象**：`compaction score` 飙到 500+，查询变慢。

**根因**：Routine Load 导入并发太高，BE compaction 线程数不够。

**修复**：

```sql
ADMIN SET FRONTEND CONFIG ("max_cumulative_compaction_num_singleton_deltas" = "2000");
-- BE 侧调整
UPDATE be_config SET value = '8' WHERE name = 'max_compaction_concurrency';
```

同时降低 Routine Load 并发。

### 9.3 FE Leader 切换导致业务中断

**现象**：FE leader 所在节点 OOM 重启，新 leader 选举花了 30 秒，期间业务报错。

**根因**：FE 的 JVM heap 只设了 8GB，元数据规模上来后频繁 full GC 导致 OOM。

**修复**：FE heap 调到 32GB，集群元数据定期清理（删掉老的 transaction 记录等）。

**教训**：FE 不是"无状态组件"，heap 要按元数据规模预留。

## 十、项目健康度

最后说一下两个项目的"项目层面"观察：

**Apache Doris**：

- Apache 基金会项目，治理规范
- 社区活跃，中国贡献者为主
- VeloDB 是主要商业推动方
- 文档偏中文，国际化还在努力
- Release 节奏快，3-4 个月一个版本

**StarRocks**：

- Linux 基金会项目
- 背后是 StarRocks Inc（商业公司）
- 社区和商业版并行，商业版功能更全
- 英文文档好于中文
- Release 节奏类似

两者的商业化策略相似，都是开源社区版 + 商业增强版。核心功能开源，运维、权限、管理类功能商业版。

## 十一、经验法则

- **两者差异比以前小了**，选错了也不是世界末日
- **Upsert 场景优先 StarRocks**
- **日志分析两者都行**
- **先跑 POC 再决定**，别完全靠文档对比
- **Bucket 数和 BE 数匹配**
- **FE 内存要给足**
- **Compaction 要监控**
- **存算分离先观望**
- **生态兼容性都够用**

OLAP 这个领域已经进入"细节之战"，两个项目都足够好。真正决定项目成败的往往不是选型，而是你的建模和查询模式是否合理。对比选型花一个月的时间，不如好好设计分区、分桶、物化视图。

希望这篇笔记能帮你的选型决策少纠结一点。

参考资料：

- Apache Doris 官方文档 doris.apache.org，3.0 版本
- StarRocks 官方文档 docs.starrocks.io，3.3 版本
- 两者的 release notes 和 changelog
- pracdata.io 的 "State of Open Source Real-Time OLAP Systems 2025" 综述
- StarRocks Engineering 和 VeloDB 两个 Medium 账号的对比文章
