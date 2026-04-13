---
title: "ClickHouse 生产运维实战：集群部署、副本分片、性能调优与故障排查"
date: 2026-03-15T10:00:00+08:00
draft: false
tags: ["ClickHouse", "OLAP", "数据库", "性能调优", "分布式"]
categories: ["数据库运维"]
description: "系统讲解 ClickHouse 生产环境部署、ReplicatedMergeTree 副本与分片、物化视图、TTL、慢查询排查与常见坑。"
summary: "ClickHouse 高吞吐 OLAP 能力背后有一套独特的运维范式：ReplicatedMergeTree、ZooKeeper/Keeper、分布式表、物化视图、TTL、MergeTree 家族选型。本文按生产落地路径，从集群规划、副本分片、写入优化、查询调优、物化视图到慢查询排查，配套可直接复用的 SQL 与运维脚本。"
toc: true
math: false
diagram: false
keywords: ["ClickHouse", "ReplicatedMergeTree", "ClickHouse Keeper", "分布式表", "物化视图", "TTL", "慢查询", "OLAP 运维"]
params:
  reading_time: true
---

## 一、为什么选 ClickHouse

### 1.1 OLAP 与 OLTP 的根本差异

OLTP（MySQL、PostgreSQL 这类）关心的是"一行数据的完整生命周期"：增、删、改、主键查，事务和一致性是第一位。行存储让一行数据物理上连续，单行读写只需要一次磁盘寻址。

OLAP 关心的是"一列数据在亿级行上的聚合"：多数查询形如 `SELECT country, sum(amount) FROM events WHERE date >= ? GROUP BY country`。这类查询只涉及 3～5 列，但扫描上亿行。如果用行存，每行都要把整行读进内存再丢掉无关字段，I/O 浪费在 90% 以上。

列存把同一列的数据物理连续存放，读多少列就扫多少列；同一列数据类型相同、取值分布重复，天然适合字典编码、LZ4/ZSTD 压缩，压缩比常见 5～20 倍。再叠加向量化执行（一次处理 1024 或 8192 行的 SIMD 批次），单机每秒可以扫描几十亿行。

### 1.2 ClickHouse 的硬核之处

ClickHouse 的设计几乎把一切都押在"扫描即王道"上：

- 列存 + 稀疏主键索引（每 8192 行一个索引标记，叫 granule），因此主键不是唯一键，是排序键
- MergeTree 存储引擎按 `ORDER BY` 物理有序，范围查询只需二分定位起止 granule
- LZ4 默认压缩，ZSTD 可选，列级压缩编解码器（Delta、DoubleDelta、Gorilla、T64）按数据特性选
- 执行引擎向量化，查询计划按 block（列的矩形切片）流动，CPU cache 命中率高
- 分布式表 Distributed 做 scatter-gather，单表查询自动 fan-out 到所有 shard

### 1.3 与 Doris/StarRocks/Druid 的定位差异

| 维度 | ClickHouse | Apache Doris / StarRocks | Apache Druid |
| --- | --- | --- | --- |
| 写入场景 | 批量 insert、Kafka 消费、物化视图 | 批量 stream load、routine load | 实时流（Kafka indexing service） |
| 更新能力 | ReplacingMergeTree/异步 mutation，重写 part | UNIQUE KEY 合并、主键模型 | 不支持行级更新 |
| JOIN 能力 | 单机强，分布式 JOIN 依赖 GLOBAL/广播 | Colocation Join 更友好 | 弱，靠预聚合 |
| 资源管理 | 相对粗粒度，依赖 settings/quota | 内置资源组、workload group | 内置 | 
| 运维上手 | 学习曲线陡，坑多但可控 | 更像 MySQL | 组件多（Historical/Broker/Coordinator） |

ClickHouse 并不是万能银弹：如果你的业务是"大量高并发小查询 + 频繁更新"，Doris/StarRocks 会更省心；如果是"实时流式聚合 + 低延迟点查"，Druid 的分层架构有优势。但只要是"历史数据量巨大 + 扫描式分析为主 + 批量 ingest"，ClickHouse 的单机吞吐和压缩比目前仍然是第一梯队。

### 1.4 本文的前置假设

为了让后面的内容不至于过度抽象，下面的所有示例都围绕一个虚构的业务场景：某个 SaaS 产品需要把用户行为事件（点击、页面浏览、API 调用）入仓做实时分析。日增 30 亿行，单行 300B 左右（压缩前），保留 180 天，主查询按 `tenant_id`、`event_date`、`event_name` 过滤后做 `count/sum/uniq` 聚合。集群名统一叫 `analytics-cluster`，对外域名 `ch.example.com`。

## 二、生产集群架构规划

### 2.1 副本与分片的基本组合

ClickHouse 的集群拓扑用两个维度描述：

- Shard（分片）：数据水平拆分，每个 shard 存储一部分数据
- Replica（副本）：同一个 shard 的数据冗余，保证可用性

最常见的三种拓扑：

1. 单 shard 多副本：数据量不大但要求高可用，读扩展靠副本
2. 多 shard 单副本：数据量大但对可用性要求低（有外部备份），常见于"反正明天重算"的数仓层
3. 多 shard 多副本：生产标配，通常 N shard × 2 副本

### 2.2 怎么决定 shard 数

一个朴素但实用的公式：

```text
shard 数 ≈ ceil(日增数据量 × 保留天数 × 副本数 / (单机可用容量 × 安全水位))
```

以本文业务为例：

```text
日增 3e9 行 × 300 B ≈ 900 GB/日（未压缩）
ClickHouse 默认 LZ4 压缩比 ~5x，压缩后 ~180 GB/日
保留 180 天 → 32.4 TB/副本
2 副本 → 64.8 TB 总数据
单机按 NVMe 8 TB、安全水位 60% → 可用 4.8 TB
shard 数 = ceil(64.8 / 4.8) = 14
```

留出 20% 余量后，最终按 16 shard × 2 副本 = 32 节点规划。实际容量估算还要考虑：merge 临时空间（预留 30%）、mutation 期间的 part 翻倍、projection 占用。

### 2.3 硬件选型经验值

磁盘是最重要的：

- NVMe SSD 优先，IOPS 和顺序读带宽都能压住 merge 开销
- SATA SSD 勉强可用，HDD 几乎没法用在热数据层（merge 会卡死）
- 冷数据层可以挂 S3 Disk 或 HDD 做 tiered storage
- 文件系统建议 ext4 或 XFS，noatime 挂载，ext4 打开 `data=writeback` 在掉电安全前提下能提点写入

CPU 与内存：

- CPU 核数越多越好，向量化执行可以线性扩展；16C/32C 是常见起点
- 内存建议 `data_size / 50` 起，最小 64 GB；mark cache、uncompressed cache、query memory 都吃它
- 禁用 NUMA 交错或者在 `config.xml` 里绑核，NUMA 不友好会让聚合场景掉 30%

网络：

- 万兆起步，分布式 JOIN 和副本同步都靠它
- 同一个 shard 的两个副本最好放在同机架内或同可用区，跨 AZ 会放大副本同步延迟

### 2.4 目录规划

生产环境永远不要把数据放 `/var/lib/clickhouse` 默认路径，直接跟系统盘绑死。推荐：

```text
/data/clickhouse/            # 主数据盘，挂 NVMe
  ├── data/                  # parts 数据
  ├── metadata/              # 表结构 SQL
  ├── store/                 # UUID 化存储目录
  ├── tmp/                   # 临时文件（merge/insert）
  ├── user_files/            # file() 表函数读写
  └── format_schemas/        # Protobuf/CapnProto schema
/var/log/clickhouse-server/  # 日志
/etc/clickhouse-server/      # 配置
```

和 `config.xml` 对应：

```xml
<path>/data/clickhouse/</path>
<tmp_path>/data/clickhouse/tmp/</tmp_path>
<user_files_path>/data/clickhouse/user_files/</user_files_path>
<format_schema_path>/data/clickhouse/format_schemas/</format_schema_path>
```

## 三、ClickHouse Keeper 部署

### 3.1 从 ZooKeeper 切到 Keeper

ClickHouse 早期依赖 ZooKeeper 存储副本元数据（part 列表、mutation、DDL 队列）。ZooKeeper 有几个痛点：

- JVM，GC 停顿会让整个副本表 READONLY
- watch 数和 znode 数上来以后性能掉得很快
- 写入放大严重，一次 part 提交要走好几个 znode

Keeper 是 ClickHouse 官方用 C++ 重写的 Raft 实现，协议兼容 ZooKeeper，部署上可以：

- 独立进程 `clickhouse-keeper`（推荐生产）
- 和 `clickhouse-server` 同进程内嵌（适合小集群或测试）

### 3.2 三节点 Keeper 配置示例

`/etc/clickhouse-keeper/keeper_config.xml`：

```xml
<clickhouse>
    <logger>
        <level>information</level>
        <log>/var/log/clickhouse-keeper/clickhouse-keeper.log</log>
        <errorlog>/var/log/clickhouse-keeper/clickhouse-keeper.err.log</errorlog>
        <size>1000M</size>
        <count>10</count>
    </logger>

    <keeper_server>
        <tcp_port>9181</tcp_port>
        <server_id>1</server_id> <!-- 三个节点分别为 1/2/3 -->
        <log_storage_path>/data/keeper/coordination/log</log_storage_path>
        <snapshot_storage_path>/data/keeper/coordination/snapshots</snapshot_storage_path>

        <coordination_settings>
            <operation_timeout_ms>10000</operation_timeout_ms>
            <session_timeout_ms>30000</session_timeout_ms>
            <raft_logs_level>information</raft_logs_level>
            <!-- 每 100000 条 log 做一次 snapshot，大集群可调大到 1000000 -->
            <snapshot_distance>100000</snapshot_distance>
            <!-- 自动压缩日志 -->
            <reserved_log_items>10000</reserved_log_items>
        </coordination_settings>

        <raft_configuration>
            <server>
                <id>1</id>
                <hostname>keeper-1.example.com</hostname>
                <port>9234</port>
            </server>
            <server>
                <id>2</id>
                <hostname>keeper-2.example.com</hostname>
                <port>9234</port>
            </server>
            <server>
                <id>3</id>
                <hostname>keeper-3.example.com</hostname>
                <port>9234</port>
            </server>
        </raft_configuration>
    </keeper_server>
</clickhouse>
```

在 `clickhouse-server` 上引用：

```xml
<clickhouse>
    <zookeeper>
        <node index="1"><host>keeper-1.example.com</host><port>9181</port></node>
        <node index="2"><host>keeper-2.example.com</host><port>9181</port></node>
        <node index="3"><host>keeper-3.example.com</host><port>9181</port></node>
        <session_timeout_ms>30000</session_timeout_ms>
        <operation_timeout_ms>10000</operation_timeout_ms>
    </zookeeper>
</clickhouse>
```

### 3.3 Keeper 的核心监控指标

Keeper 暴露 4lw（four-letter words）命令，跟 ZooKeeper 一样：

```bash
# 健康检查
echo mntr | nc keeper-1.example.com 9181

# 看当前 leader
echo stat | nc keeper-1.example.com 9181 | grep Mode

# 看连接数、pending 请求
echo cons | nc keeper-1.example.com 9181
echo wchc | nc keeper-1.example.com 9181 | head
```

关键指标：

- `zk_outstanding_requests`：pending 请求数，持续 > 100 说明 Keeper 成为瓶颈
- `zk_znode_count`：znode 总数，经验值每个副本表约 20～30 个 znode，再乘 shard 数和 part 数
- `zk_watch_count`：watch 数，和副本数量线性相关
- `zk_followers` / `zk_synced_followers`：集群健康

### 3.4 常见 Keeper 故障

**现象**：所有副本表突然变 READONLY，`SELECT * FROM system.replicas WHERE is_readonly = 1` 全都是 1。

**原因**：Keeper 会话过期。常见触发：

- Keeper 节点 GC 或被 OOM kill，CH server 端 session timeout 超过阈值
- 网络抖动超过 `session_timeout_ms`
- Keeper 磁盘写入变慢（snapshot 或 log 同步卡住）

**修复**：

```sql
-- 先确认 Keeper 自身恢复
-- 然后在任意副本上触发重连
SYSTEM RESTART REPLICA db.table;

-- 如果一批表都挂了
SYSTEM RESTART REPLICAS;

-- 观察同步进度
SELECT database, table, is_readonly, absolute_delay, queue_size
FROM system.replicas WHERE is_readonly = 1 OR absolute_delay > 60;
```

**现象**：Keeper 日志刷大量 `ZNONODE`，某个副本追不上。

**原因**：该副本本地 part 和 Keeper 记录对不齐，最常见是磁盘故障或手动删了 part 目录。

**修复**：

```sql
-- 强制从其他副本拉取完整数据
SYSTEM DROP REPLICA 'replica_name' FROM TABLE db.table;
-- 然后在故障副本上 DETACH/ATTACH 触发全量同步
DETACH TABLE db.table;
ATTACH TABLE db.table;
```

## 四、MergeTree 家族选型

MergeTree 是 ClickHouse 的基石引擎，派生出一组变种。选错引擎比调错参数更致命，因为切换引擎意味着全表重建。

### 4.1 MergeTree：最纯粹的列存表

没有任何去重、聚合语义，所有数据按 `ORDER BY` 有序写入，按 `PARTITION BY` 分区。适合"append-only、不去重、不合并"的原始事实表。

```sql
CREATE TABLE events_raw
(
    event_date    Date,
    event_time    DateTime64(3),
    tenant_id     UInt32,
    user_id       UInt64,
    event_name    LowCardinality(String),
    properties    String,  -- JSON 字符串
    _ingest_time  DateTime DEFAULT now()
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(event_date)
ORDER BY (tenant_id, event_date, event_name, user_id)
SETTINGS index_granularity = 8192;
```

注意几个细节：

- `PARTITION BY` 粒度别太细。按天分区在 180 天保留下就是 180 个 part 目录起步，每个表每个 shard 都乘以 shard 数，Keeper znode 会爆。除非日增超过 1TB，否则按月分区足够。
- `ORDER BY` 字段顺序决定主键索引效率，把"等值过滤字段"放前面，范围字段放后面
- `index_granularity` 默认 8192，查询命中稀疏索引后仍要扫这么多行。高选择性场景可以调小到 4096 或 2048，会增加索引 mark 内存占用

### 4.2 ReplacingMergeTree：按主键去重

对同一个 `ORDER BY` 键的多条数据，merge 时只保留一条（按版本列或最后写入）。

```sql
CREATE TABLE users_cdc
(
    user_id    UInt64,
    email      String,
    updated_at DateTime,
    _version   UInt64
)
ENGINE = ReplacingMergeTree(_version)
ORDER BY user_id;
```

**大坑**：去重是"最终一致"的，只有在 merge 发生以后重复才会消失。查询刚写入的数据还会看到多条。生产里要用 `FINAL` 或 `argMax`：

```sql
-- 方式一：FINAL（性能差，单线程合并）
SELECT * FROM users_cdc FINAL WHERE user_id = 123;

-- 方式二：argMax（推荐，并行执行）
SELECT user_id,
       argMax(email, _version) AS email,
       max(_version) AS _version
FROM users_cdc
WHERE user_id = 123
GROUP BY user_id;
```

ClickHouse 23.x 以后 `FINAL` 的性能大幅改善（支持 `do_not_merge_across_partitions_select_final`），但仍不建议在 OLAP 查询主路径上用。

### 4.3 SummingMergeTree：相同 key 求和

merge 时对相同 `ORDER BY` key 的数值列求和，非数值列取第一条。

```sql
CREATE TABLE events_by_hour
(
    tenant_id   UInt32,
    event_hour  DateTime,
    event_name  LowCardinality(String),
    pv          UInt64,
    uv_hll      AggregateFunction(uniq, UInt64)
)
ENGINE = SummingMergeTree((pv))  -- 只对 pv 求和
PARTITION BY toYYYYMM(event_hour)
ORDER BY (tenant_id, event_hour, event_name);
```

SummingMergeTree 只有数值列能 sum，UV 这类需要 HLL 合并的要用 AggregatingMergeTree。

### 4.4 AggregatingMergeTree：通用聚合

把任意聚合状态列（`AggregateFunction(func, T)`）在 merge 时合并。搭配物化视图用是最经典的"预聚合仓"模式。

```sql
CREATE TABLE events_agg
(
    tenant_id   UInt32,
    event_hour  DateTime,
    event_name  LowCardinality(String),
    pv_state    AggregateFunction(sum, UInt64),
    uv_state    AggregateFunction(uniq, UInt64)
)
ENGINE = AggregatingMergeTree
PARTITION BY toYYYYMM(event_hour)
ORDER BY (tenant_id, event_hour, event_name);

-- 查询时用 -Merge 后缀还原最终值
SELECT tenant_id,
       event_hour,
       sumMerge(pv_state)  AS pv,
       uniqMerge(uv_state) AS uv
FROM events_agg
WHERE event_hour >= now() - INTERVAL 1 DAY
GROUP BY tenant_id, event_hour;
```

### 4.5 CollapsingMergeTree 与 VersionedCollapsing

用来处理"更新"和"删除"语义。每条数据带一个 `sign` 列，`+1` 表示状态、`-1` 表示取消。merge 时 `+1/-1` 成对折叠。

```sql
CREATE TABLE orders_collapsing
(
    order_id  UInt64,
    status    LowCardinality(String),
    amount    Decimal(18, 4),
    sign      Int8
)
ENGINE = CollapsingMergeTree(sign)
ORDER BY order_id;
```

更新时必须先写 `sign=-1`（旧值），再写 `sign=+1`（新值），否则折叠失败。

VersionedCollapsingMergeTree 多一个 version 列，解决乱序写入下的折叠问题，生产如果上游是 Kafka + 多分区，推荐直接用它。

### 4.6 引擎选型决策树

```text
数据是否需要更新？
├─ 否 → MergeTree（原始事实表）
└─ 是
   ├─ 同 key 只保留最新一条 → ReplacingMergeTree
   ├─ 同 key 数值求和         → SummingMergeTree
   ├─ 通用聚合（HLL/分位数）    → AggregatingMergeTree
   └─ 有明确的 insert/delete 对 → VersionedCollapsingMergeTree
```

## 五、ReplicatedMergeTree 副本机制

### 5.1 zookeeper path 规则

ReplicatedMergeTree 的副本协调完全依赖 Keeper/ZooKeeper 上的一个路径，规则强烈建议按宏（macros）模板化：

```sql
CREATE TABLE events_local ON CLUSTER analytics_cluster
(
    event_date Date,
    tenant_id  UInt32,
    event_name LowCardinality(String),
    user_id    UInt64
)
ENGINE = ReplicatedMergeTree(
    '/clickhouse/tables/{shard}/{database}/events_local',
    '{replica}'
)
PARTITION BY toYYYYMM(event_date)
ORDER BY (tenant_id, event_date, event_name);
```

`{shard}` 和 `{replica}` 来自节点上的 `macros` 配置：

```xml
<!-- /etc/clickhouse-server/config.d/macros.xml -->
<clickhouse>
    <macros>
        <cluster>analytics_cluster</cluster>
        <shard>01</shard>
        <replica>shard01-replica-a</replica>
    </macros>
</clickhouse>
```

**坑**：同一个 shard 的两个副本必须使用**相同**的 `{shard}` 值和**不同**的 `{replica}` 值。如果两个节点都写成 `replica-a`，副本间会互相认为对方是自己，part 来回飘。

### 5.2 ON CLUSTER 的执行模型

`ON CLUSTER analytics_cluster` 会把 DDL 语句写入 Keeper 的 DDL 队列 `/clickhouse/task_queue/ddl`，所有节点拉取并执行。常见问题：

- DDL 超时：默认 `distributed_ddl_task_timeout=180s`，大表的 `ALTER TABLE ... ADD COLUMN` 可能超过。可以先增大再执行：

  ```sql
  SET distributed_ddl_task_timeout = 3600;
  ALTER TABLE events_local ON CLUSTER analytics_cluster ADD COLUMN country LowCardinality(String);
  ```

- 某个节点挂了 DDL 卡住：检查 Keeper 上 DDL 队列

  ```sql
  SELECT * FROM system.distributed_ddl_queue
  WHERE cluster = 'analytics_cluster' AND status != 'Finished'
  ORDER BY query_create_time DESC LIMIT 20;
  ```

  找到卡住的任务，手动删除 znode：

  ```bash
  /usr/bin/clickhouse-keeper-client -h keeper-1.example.com -p 9181 \
    --query "rm /clickhouse/task_queue/ddl/query-0000000123"
  ```

### 5.3 副本同步原理

ReplicatedMergeTree 的同步不是 binlog 复制，而是基于 Keeper 的"操作日志 + 数据拉取"：

1. 副本 A 写入新 part 后，在 Keeper 上 `/replicas/A/log/` 写入 `GET_PART` 日志
2. 副本 B 监听该路径，拿到日志后从 A 通过 HTTP `interserver_http_port`（默认 9009）拉取 part
3. 拉取成功后更新本地元数据和 Keeper 上的 parts 列表

查看同步队列：

```sql
SELECT database, table, replica_name, queue_size, inserts_in_queue,
       merges_in_queue, absolute_delay, total_replicas, active_replicas
FROM system.replicas
WHERE absolute_delay > 10 OR queue_size > 100;
```

`absolute_delay` 是副本落后的秒数，`queue_size` 是未完成的任务数。

### 5.4 READONLY 副本恢复

副本进入 READONLY 的典型原因：

1. 本地 metadata 校验失败（表结构和 Keeper 不一致）
2. Keeper 会话丢失并且重连后发现本地 part 比 Keeper 记录多
3. 磁盘写满后部分 part 写了一半

恢复步骤：

```sql
-- 第一步：看原因
SELECT database, table, is_readonly, is_session_expired,
       last_exception, replica_is_active
FROM system.replicas WHERE is_readonly = 1;

-- 第二步：尝试 restart
SYSTEM RESTART REPLICA db.events_local;

-- 第三步：如果还是 readonly，且磁盘数据完整
DETACH TABLE db.events_local;
ATTACH TABLE db.events_local;

-- 第四步：如果本地数据损坏，放弃本地从其他副本重建
-- 在另一个健康副本上先清理当前副本的 Keeper 注册
SYSTEM DROP REPLICA 'shard01-replica-a' FROM TABLE db.events_local;
-- 然后在故障节点上重建表，会从其他副本拉取
```

## 六、分布式表 Distributed

### 6.1 本地表 + 分布式表的双表结构

ClickHouse 的分布式查询需要两张表：

- 本地表（每个节点各一张），保存实际数据
- 分布式表（每个节点一张），只是一个指针，查询时 fan-out 到所有 shard

```sql
-- 先创建本地副本表（每个 shard 的每个副本）
CREATE TABLE db.events_local ON CLUSTER analytics_cluster
(
    event_date Date,
    tenant_id  UInt32,
    event_name LowCardinality(String),
    user_id    UInt64,
    amount     Decimal(18, 4)
)
ENGINE = ReplicatedMergeTree(
    '/clickhouse/tables/{shard}/{database}/events_local',
    '{replica}'
)
PARTITION BY toYYYYMM(event_date)
ORDER BY (tenant_id, event_date, event_name);

-- 再创建分布式表
CREATE TABLE db.events_dist ON CLUSTER analytics_cluster
AS db.events_local
ENGINE = Distributed(
    analytics_cluster,      -- 集群名（remote_servers 里定义）
    db,                     -- 本地库
    events_local,           -- 本地表
    cityHash64(tenant_id)   -- sharding key
);
```

### 6.2 internal_replication 的含义

`remote_servers` 里有个关键参数 `internal_replication`：

```xml
<clickhouse>
    <remote_servers>
        <analytics_cluster>
            <shard>
                <internal_replication>true</internal_replication>
                <replica><host>shard01-a.example.com</host><port>9000</port></replica>
                <replica><host>shard01-b.example.com</host><port>9000</port></replica>
            </shard>
            <shard>
                <internal_replication>true</internal_replication>
                <replica><host>shard02-a.example.com</host><port>9000</port></replica>
                <replica><host>shard02-b.example.com</host><port>9000</port></replica>
            </shard>
        </analytics_cluster>
    </remote_servers>
</clickhouse>
```

- `internal_replication=true`：Distributed 表写入时只往每个 shard 的**一个**副本写，副本间通过 ReplicatedMergeTree 自己同步
- `internal_replication=false`：Distributed 写入时往每个副本都写一遍，适用于非复制表

**生产必须用 `true`**。用 `false` 会导致两个副本各写一份、ReplicatedMergeTree 再同步，数据翻倍。

### 6.3 sharding_key 的选择

sharding_key 决定数据在各 shard 之间的分布。糟糕的 key 会让 90% 数据压到一个 shard：

- 错误示例：`sharding_key = rand()` → 每次随机导致同一用户数据散到所有 shard，JOIN 效率爆炸
- 错误示例：`sharding_key = toYYYYMMDD(event_date)` → 当天写入全堆到一个 shard
- 推荐：`sharding_key = cityHash64(tenant_id)` → 同一租户的数据落在同 shard，JOIN 友好
- 推荐：`sharding_key = cityHash64(user_id)` → 用户维度查询性能好

更高级的做法是用 `jumpConsistentHash` 保证扩容时数据迁移最小化。

### 6.4 写分布式表 vs 直接写本地表

两种写入路径：

1. 写分布式表 → ClickHouse 内部 fan-out 到目标 shard，延迟高但客户端简单
2. 客户端按 sharding_key 自己路由 → 直接写本地表，省去一次转发

大流量场景（> 100K rows/s）强烈推荐客户端路由。分布式表写入的坏处：

- 分布式表节点会把数据先落到 `data/default/.bin` 临时文件，再异步转发，磁盘压力大
- 下游节点挂了，分布式表节点会堆积转发队列，慢慢磨掉磁盘
- 出故障排查链路长

如果一定要用分布式表写入，开启异步 insert：

```sql
SET insert_distributed_sync = 0;           -- 异步转发
SET distributed_background_insert_sleep_time_ms = 100;
SET distributed_background_insert_max_sleep_time_ms = 30000;
```

### 6.5 读路径和 GLOBAL 子查询

分布式 SELECT 的默认行为：发起节点把查询发给每个 shard 的一个副本，各自执行后在发起节点聚合。

JOIN 场景要小心：

```sql
-- 错误：右表在每个 shard 上都是本 shard 的局部数据
SELECT a.tenant_id, b.company_name
FROM events_dist a
JOIN tenants_dist b ON a.tenant_id = b.tenant_id;

-- 正确：GLOBAL JOIN 会把右表结果广播到所有 shard
SELECT a.tenant_id, b.company_name
FROM events_dist a
GLOBAL JOIN tenants_dist b ON a.tenant_id = b.tenant_id;
```

`GLOBAL` 的代价是广播，右表大小决定内存占用。如果右表特别大，考虑 colocation（同 sharding_key）或者做成字典表。

## 七、写入优化

### 7.1 为什么写入那么讲究

ClickHouse 每次 INSERT 会生成一个新 part（目录），然后后台 merge 线程把小 part 合并成大 part。如果 insert 频率太高：

- part 数量爆炸，Keeper znode 撑爆
- merge 跟不上，触发 `Too many parts` 报错
- 查询变慢（要扫描更多 part）

官方的经验值：

- 单表 insert 频率 ≤ 1 次/秒
- 单批次 ≥ 10000 行，最好 100000 ～ 1000000 行

### 7.2 批次大小怎么定

```python
# 伪代码：积攒到阈值或超时就 flush
buffer = []
MAX_ROWS = 500_000
MAX_WAIT_MS = 5000

def on_message(row):
    buffer.append(row)
    if len(buffer) >= MAX_ROWS or elapsed_ms() >= MAX_WAIT_MS:
        flush()

def flush():
    ch_client.insert("events_local", buffer)
    buffer.clear()
```

数值参考：每批 50 万行在 20 字段宽度下约 150 MB 左右，落盘时间 < 2s。

### 7.3 async_insert：让服务端帮你攒批

从 22.x 开始可用。客户端不用管批次，服务端内部缓冲：

```sql
SET async_insert = 1;
SET wait_for_async_insert = 1;       -- 同步等待确认
SET async_insert_max_data_size = 10_000_000;   -- 10 MB 或
SET async_insert_busy_timeout_ms = 1000;       -- 1 秒 flush
```

注意：

- `wait_for_async_insert=0` 时客户端收到的是"已进入缓冲区"而不是"已落盘"，掉电会丢
- 每个 "query hash + 用户 + settings" 组合对应一个 buffer，如果客户端写入语句不完全一致，服务端会建多个 buffer 达不到合并效果
- async_insert 也有对应的监控：`system.asynchronous_inserts`、`system.asynchronous_insert_log`

### 7.4 Buffer 表（已不推荐，但要知道）

Buffer 引擎把数据先放内存，达到阈值后 flush 到底层表：

```sql
CREATE TABLE events_buffer AS events_local
ENGINE = Buffer(db, events_local,
    16,           -- 并发 layer 数
    10, 60,       -- min/max 秒数
    10000, 1000000, -- min/max 行数
    10000000, 100000000); -- min/max 字节
```

问题：

- 掉电丢数据
- 查询 Buffer 表会同时扫内存和底层，JOIN 和谓词下推有问题
- async_insert 出现后基本被替代，新项目不要用

### 7.5 从 Kafka 消费

ClickHouse 自带 Kafka 引擎表，配合物化视图可以做到"Kafka → CH"零代码：

```sql
-- 1. Kafka 引擎表，只是个消费者代理
CREATE TABLE kafka_events_source
(
    event_date Date,
    tenant_id  UInt32,
    event_name String,
    user_id    UInt64,
    amount     Decimal(18, 4)
)
ENGINE = Kafka
SETTINGS
    kafka_broker_list = 'kafka-1.example.com:9092,kafka-2.example.com:9092',
    kafka_topic_list = 'events',
    kafka_group_name = 'ch_events_consumer',
    kafka_format = 'JSONEachRow',
    kafka_num_consumers = 4,
    kafka_thread_per_consumer = 1,
    kafka_max_block_size = 1048576,
    kafka_poll_max_batch_size = 65536;

-- 2. 目标 ReplicatedMergeTree 表
CREATE TABLE events_local ON CLUSTER analytics_cluster
(
    event_date Date,
    tenant_id  UInt32,
    event_name LowCardinality(String),
    user_id    UInt64,
    amount     Decimal(18, 4)
)
ENGINE = ReplicatedMergeTree(
    '/clickhouse/tables/{shard}/{database}/events_local',
    '{replica}'
)
PARTITION BY toYYYYMM(event_date)
ORDER BY (tenant_id, event_date, event_name);

-- 3. 物化视图作为"搬运工"
CREATE MATERIALIZED VIEW mv_kafka_to_events TO events_local AS
SELECT event_date, tenant_id, event_name, user_id, amount
FROM kafka_events_source;
```

**坑合集**：

- Kafka 引擎表单表不要接太多消费者，`kafka_num_consumers` 不能超过 topic 的 partition 数，否则多余消费者空转
- Kafka 消息格式异常会让整个 block 失败，可以打开 `kafka_skip_broken_messages = N` 跳过最多 N 条坏消息
- CH 重启后消费者 offset 保留在 Kafka 侧（靠 group name），如果新建表时复用老 group name 会接着上次消费
- 查问题看 `system.kafka_consumers` 和 `system.errors`

### 7.6 避免 Too many parts

当单分区 part 数超过 `parts_to_throw_insert`（默认 300）时，INSERT 直接报错。触发原因和修复：

| 原因 | 现象 | 修复 |
| --- | --- | --- |
| insert 频率太高 | `system.parts` 小 part 巨多 | 增大批次、开 async_insert |
| merge 线程不够 | `system.merges` 一直满 | `SET background_pool_size=32`（需重启） |
| 分区粒度太细 | 单表 part 总数 > 50 万 | 改 PARTITION BY 粗粒度 |
| 磁盘慢 | merge 速度 MB/s 个位数 | 换 NVMe，或增大 `max_bytes_to_merge_at_max_space_in_pool` |

紧急止血：临时调大阈值并发起强制 merge。

```sql
-- 临时放宽（不推荐长期）
ALTER TABLE events_local MODIFY SETTING parts_to_throw_insert = 1000;

-- 触发所有分区 merge
OPTIMIZE TABLE events_local PARTITION '202603' FINAL;
```

`OPTIMIZE FINAL` 会把该分区所有 part 合并成一个，代价巨大，只能在低峰期操作。

## 八、查询优化

### 8.1 主键与 ORDER BY

主键就是 ORDER BY 的前缀，稀疏索引建立在主键上。查询是否能走索引的判据：

- WHERE 条件必须包含主键**前缀**的等值或范围过滤
- `toYYYYMM(event_date)` 不是主键上的函数，可能无法下推

看查询是否用了主键：

```sql
EXPLAIN indexes = 1
SELECT count() FROM events_local
WHERE tenant_id = 123 AND event_date >= '2026-03-01';
```

输出会显示 `Keys: tenant_id, event_date`、`Granules: 1234/56789`，比值越低说明命中越好。

### 8.2 Data Skipping Indexes（二级跳数索引）

不是 B+ 树那种二级索引，而是"在主键之外，对某些 granule 记录统计信息（min/max/bloom filter），查询时跳过明显不匹配的 granule"。

```sql
ALTER TABLE events_local
ADD INDEX idx_user_id user_id TYPE minmax GRANULARITY 4;

ALTER TABLE events_local
ADD INDEX idx_event_name event_name TYPE set(100) GRANULARITY 4;

ALTER TABLE events_local
ADD INDEX idx_url url TYPE tokenbf_v1(32768, 3, 0) GRANULARITY 4;

-- 新添加的索引只对新数据生效，对存量数据要物化
ALTER TABLE events_local MATERIALIZE INDEX idx_user_id;
```

索引类型选型：

- `minmax`：数值或日期，低选择性列（主键前缀之外的范围过滤）
- `set(N)`：枚举值少（< N）的 String/LowCardinality
- `bloom_filter`：等值匹配为主，误判率低
- `tokenbf_v1`：全文关键字，`hasToken(url, 'foo')` 会用它
- `ngrambf_v1`：子串匹配，`LIKE '%foo%'` 会用它

跳数索引不是越多越好，每个索引都会增加写入开销和存储。优先优化主键，再考虑跳数索引。

### 8.3 PREWHERE

PREWHERE 是 ClickHouse 的独门绝活：先用最小的列过滤掉大部分行，再读其他列。例：

```sql
-- 写法一：WHERE
SELECT tenant_id, user_id, properties
FROM events_local
WHERE event_date = '2026-03-15' AND amount > 1000;

-- 写法二：PREWHERE
SELECT tenant_id, user_id, properties
FROM events_local
PREWHERE event_date = '2026-03-15' AND amount > 1000;
```

优化器会自动把部分条件下推到 PREWHERE，但如果你的谓词涉及"大宽列"（比如 properties），手动写 PREWHERE 能避免把宽列读进来。

### 8.4 Projection：写时物化，查时自动选

Projection 类似 Oracle 的物化视图，但对查询透明。ClickHouse 会在查询时自动选择最合适的 projection。

```sql
ALTER TABLE events_local
ADD PROJECTION proj_user
(
    SELECT
        tenant_id,
        user_id,
        count() AS events_cnt,
        sum(amount) AS total_amount
    GROUP BY tenant_id, user_id
);

-- 物化存量数据
ALTER TABLE events_local MATERIALIZE PROJECTION proj_user;
```

查询 `SELECT tenant_id, user_id, count(), sum(amount) FROM events_local GROUP BY tenant_id, user_id` 时会自动走 projection，扫描量下降几十倍。

Projection 的代价：

- 写入时每个 projection 都要同步写，写入吞吐会掉
- 存储翻倍
- mutation（ALTER UPDATE/DELETE）会对 projection 也执行一次

### 8.5 物化视图 vs Projection

| 维度 | Materialized View | Projection |
| --- | --- | --- |
| 存储位置 | 独立表 | 同表的子目录 |
| 透明度 | 需显式查询 MV 表 | 查询原表自动选 |
| 维护 | insert trigger 式 | MergeTree 内部 |
| 典型用途 | 聚合预计算，跨表 JOIN | 同表不同排序/聚合 |
| 调试 | 直观，易查 | 不易看清生效情况 |

### 8.6 ARRAY JOIN

ClickHouse 的数组类型非常强，Array 列 + ARRAY JOIN 可以实现"数组展开"效果：

```sql
CREATE TABLE events_with_tags
(
    event_id UInt64,
    tags     Array(String)
)
ENGINE = MergeTree ORDER BY event_id;

-- 查询每个 tag 的出现次数
SELECT tag, count()
FROM events_with_tags
ARRAY JOIN tags AS tag
GROUP BY tag;
```

ARRAY JOIN 不会跨行，只是把一行的数组列"炸开"成多行，语义比 SQL 标准的 LATERAL 清晰，性能也更好。

### 8.7 实用调优 setting 清单

```sql
-- 单查询最大内存，默认 10 GB
SET max_memory_usage = 20_000_000_000;

-- 单用户最大内存，多查询共享
SET max_memory_usage_for_user = 40_000_000_000;

-- 启用分布式聚合的中间状态合并优化
SET distributed_aggregation_memory_efficient = 1;

-- 查询优先级（数字越大优先级越低）
SET priority = 1;

-- 超时
SET max_execution_time = 300;

-- 对低基数字符串使用字典编码
SET low_cardinality_allow_in_native_format = 1;

-- 自动选择 PREWHERE 列
SET optimize_move_to_prewhere = 1;
```

可以把常用 settings 写到 `users.xml` 的 profile 里，避免客户端每次都 SET。

## 九、物化视图实战

### 9.1 物化视图的本质

ClickHouse 的 MV 不是"定期 refresh 的快照"，而是"insert trigger"：

- 每次源表写入，MV 的 SELECT 被当作 trigger 执行一次
- 结果写入 TO 表（或 .inner 表）
- 源表历史数据**不会**自动进 MV，要手动 POPULATE 或回填

### 9.2 聚合物化视图

```sql
-- 目标聚合表
CREATE TABLE events_hourly_agg ON CLUSTER analytics_cluster
(
    tenant_id  UInt32,
    event_hour DateTime,
    event_name LowCardinality(String),
    pv_state   AggregateFunction(sum,  UInt64),
    uv_state   AggregateFunction(uniq, UInt64),
    amt_state  AggregateFunction(sum,  Decimal(18, 4))
)
ENGINE = ReplicatedAggregatingMergeTree(
    '/clickhouse/tables/{shard}/{database}/events_hourly_agg',
    '{replica}'
)
PARTITION BY toYYYYMM(event_hour)
ORDER BY (tenant_id, event_hour, event_name);

-- MV：源表每次 insert 都会把这段 SELECT 跑一遍
CREATE MATERIALIZED VIEW mv_events_hourly ON CLUSTER analytics_cluster
TO events_hourly_agg AS
SELECT
    tenant_id,
    toStartOfHour(event_time) AS event_hour,
    event_name,
    sumState(toUInt64(1))     AS pv_state,
    uniqState(user_id)        AS uv_state,
    sumState(amount)          AS amt_state
FROM events_local
GROUP BY tenant_id, event_hour, event_name;
```

查询：

```sql
SELECT tenant_id, event_hour,
       sumMerge(pv_state)  AS pv,
       uniqMerge(uv_state) AS uv,
       sumMerge(amt_state) AS total_amount
FROM events_hourly_agg
WHERE event_hour >= now() - INTERVAL 7 DAY
GROUP BY tenant_id, event_hour
ORDER BY event_hour DESC;
```

### 9.3 回填历史数据

MV 创建时**不会**自动处理已有数据。回填两种方式：

方式一：POPULATE（只适合小表，期间源表写入会丢）

```sql
CREATE MATERIALIZED VIEW mv_x TO target_table
POPULATE AS SELECT ... FROM source_table;
```

方式二：手动分区回填（推荐）

```sql
INSERT INTO events_hourly_agg
SELECT tenant_id,
       toStartOfHour(event_time) AS event_hour,
       event_name,
       sumState(toUInt64(1)),
       uniqState(user_id),
       sumState(amount)
FROM events_local
WHERE event_time >= '2026-01-01' AND event_time < '2026-02-01'
GROUP BY tenant_id, event_hour, event_name;
```

### 9.4 可刷新物化视图（Refreshable MV）

23.12 引入的 `REFRESH` 语法，把 MV 从"insert trigger"变成"定时全量 refresh"，适合低频批处理：

```sql
CREATE MATERIALIZED VIEW mv_tenant_daily
REFRESH EVERY 1 HOUR
TO tenant_daily AS
SELECT tenant_id,
       toDate(event_time) AS event_date,
       count()            AS events_cnt,
       uniq(user_id)      AS unique_users
FROM events_local
WHERE event_time >= now() - INTERVAL 7 DAY
GROUP BY tenant_id, event_date;
```

每小时整点重算最近 7 天数据。优点是逻辑简单，缺点是资源消耗集中。

### 9.5 物化视图常见坑

**坑 1：链式 MV 写放大**

如果 `mv_a` 从 `events_local` 触发，`mv_b` 又从 `mv_a` 触发，每次 insert 都会级联执行。级联超过 3 层后排查链路极痛苦。

修复：用 `TO table` 形式让 MV 直接写另一个表，避免链式依赖。可以用 `allow_experimental_refreshable_materialized_view` 改成可刷新。

**坑 2：MV 失败阻塞源表 insert**

MV 的 SELECT 抛异常默认会让整个 insert 失败。开启 `SET materialized_views_ignore_errors = 1` 可以跳过错误，但会丢数据。更稳妥的做法是让 MV 的 SELECT 永远不会出错（用 `assumeNotNull`、`toUInt64OrZero` 等容错函数）。

**坑 3：源表 ALTER 后 MV 未同步**

`ALTER TABLE events_local ADD COLUMN` 不会自动改 MV。要手动：

```sql
ALTER TABLE mv_events_hourly MODIFY QUERY ...;
```

**坑 4：背压**

MV 里写大量数据到目标表，而目标表 merge 慢，会反过来让源表 insert 变慢。监控 `system.part_log` 看 MV 目标表的 merge 延迟。

## 十、TTL 与数据生命周期

### 10.1 列 TTL

字段级别的"软清理"。到期后列被清成默认值，节省存储但保留行：

```sql
ALTER TABLE events_local
MODIFY COLUMN properties String TTL event_date + INTERVAL 30 DAY;
```

30 天以后 `properties` 列被置空。适合只短期需要的明细字段。

### 10.2 分区 TTL（行级过期）

最常见的用法，整行到期删除：

```sql
ALTER TABLE events_local
MODIFY TTL event_date + INTERVAL 180 DAY;
```

执行 TTL 的时机由 `merge_with_ttl_timeout` 控制，默认每 4 小时触发一次。

### 10.3 移动到冷存

TTL + TO DISK 可以把老数据搬到廉价存储：

```xml
<clickhouse>
    <storage_configuration>
        <disks>
            <hot>
                <path>/data/clickhouse/</path>
            </hot>
            <cold_s3>
                <type>s3</type>
                <endpoint>https://s3.example.com/ch-cold/</endpoint>
                <access_key_id>AKIAxxxxxxxx</access_key_id>
                <secret_access_key>xxxxxxxxxxxxxxxx</secret_access_key>
                <metadata_path>/data/clickhouse/disks/cold_s3/</metadata_path>
                <cache_enabled>true</cache_enabled>
                <data_cache_size>107374182400</data_cache_size>
            </cold_s3>
        </disks>

        <policies>
            <tiered>
                <volumes>
                    <hot>
                        <disk>hot</disk>
                    </hot>
                    <cold>
                        <disk>cold_s3</disk>
                    </cold>
                </volumes>
                <move_factor>0.2</move_factor>
            </tiered>
        </policies>
    </storage_configuration>
</clickhouse>
```

表层面应用策略：

```sql
ALTER TABLE events_local
MODIFY TTL
    event_date + INTERVAL 30  DAY TO VOLUME 'cold',
    event_date + INTERVAL 180 DAY DELETE
SETTINGS storage_policy = 'tiered';
```

30 天以内数据在本地 NVMe，30～180 天数据在 S3，180 天以后删除。注意 S3 的查询延迟比本地盘高一个数量级，查询冷数据时要显式开启 `SET optimize_move_to_prewhere = 1` 并控制并发。

### 10.4 TTL 执行监控

```sql
SELECT database, table, partition, rows, bytes_on_disk, move_ttl_info
FROM system.parts
WHERE active AND has(column_names, 'properties')
  AND bytes_on_disk > 0
ORDER BY bytes_on_disk DESC
LIMIT 20;

-- 正在跑的 TTL merge
SELECT * FROM system.merges WHERE merge_type = 'TTL_DELETE';
```

## 十一、慢查询排查

### 11.1 system.query_log 是第一入口

开启 query_log（默认开）：

```xml
<query_log>
    <database>system</database>
    <table>query_log</table>
    <partition_by>toYYYYMM(event_date)</partition_by>
    <flush_interval_milliseconds>7500</flush_interval_milliseconds>
    <max_size_rows>1048576</max_size_rows>
</query_log>
```

查最近一小时 Top 慢查询：

```sql
SELECT
    query_duration_ms,
    read_rows,
    formatReadableSize(read_bytes)          AS read_bytes,
    formatReadableSize(memory_usage)        AS memory,
    result_rows,
    user,
    client_hostname,
    substring(query, 1, 200)                AS query
FROM system.query_log
WHERE type = 'QueryFinish'
  AND event_time >= now() - INTERVAL 1 HOUR
ORDER BY query_duration_ms DESC
LIMIT 20;
```

按归一化查询聚合（看哪类 SQL 累计最慢）：

```sql
SELECT
    normalized_query_hash,
    any(substring(query, 1, 200)) AS sample,
    count()                        AS cnt,
    avg(query_duration_ms)         AS avg_ms,
    quantile(0.95)(query_duration_ms) AS p95_ms,
    sum(read_rows)                 AS total_rows
FROM system.query_log
WHERE type = 'QueryFinish'
  AND event_time >= now() - INTERVAL 1 DAY
GROUP BY normalized_query_hash
ORDER BY p95_ms DESC
LIMIT 20;
```

### 11.2 query_thread_log 和 metric_log

`query_thread_log` 拆分到每个 worker thread 级别，可以看查询的并行度和不均衡：

```sql
SELECT
    thread_id,
    query_duration_ms,
    memory_usage,
    ProfileEvents['RealTimeMicroseconds'] AS real_us,
    ProfileEvents['UserTimeMicroseconds'] AS user_us
FROM system.query_thread_log
WHERE event_time >= now() - INTERVAL 1 HOUR
  AND initial_query_id = 'xxxx-xxxx-xxxx-xxxx'
ORDER BY thread_id;
```

`metric_log` 是每秒采样一次的全局指标，排查"某个时刻全盘慢"类问题必用：

```sql
SELECT event_time,
       CurrentMetric_MemoryTracking / 1e9  AS mem_gb,
       CurrentMetric_Query                 AS running_queries,
       CurrentMetric_Merge                 AS running_merges,
       ProfileEvent_SelectedRows           AS selected_rows
FROM system.metric_log
WHERE event_time >= now() - INTERVAL 1 HOUR
ORDER BY event_time DESC;
```

### 11.3 EXPLAIN 的正确打开方式

```sql
-- 语法树
EXPLAIN AST SELECT ...;

-- 逻辑计划
EXPLAIN SYNTAX SELECT ...;

-- 执行计划
EXPLAIN PLAN SELECT ...;

-- 估计的索引命中情况（最有用）
EXPLAIN indexes = 1 SELECT ...;

-- 估计的数据读取
EXPLAIN estimate SELECT ...;

-- Pipeline 物理执行
EXPLAIN PIPELINE SELECT ...;
```

重点看 `Granules:` 行的 `matched/total` 比例。如果比例接近 1，说明谓词没命中主键索引。

### 11.4 clickhouse-benchmark 压测

复现慢查询用于调参：

```bash
echo "SELECT count() FROM events_local WHERE tenant_id = 123 AND event_date >= '2026-03-01'" \
  | clickhouse-benchmark \
    --host=ch.example.com \
    --port=9000 \
    --user=default \
    --iterations=100 \
    --concurrency=8 \
    --continue_on_errors
```

输出 QPS、p50/p95/p99 延迟，方便对比不同 settings 下的效果。

### 11.5 火焰图

ClickHouse 内置基于 eBPF 的 query profile，从 `system.trace_log` 取：

```sql
SELECT event_time,
       trace_type,
       arrayStringConcat(
           arrayMap(x -> concat(addressToLine(x), '#', demangle(addressToSymbol(x))), trace),
           ';') AS stack
FROM system.trace_log
WHERE query_id = 'xxxx' AND trace_type = 'CPU'
ORDER BY event_time;
```

导出后用 `flamegraph.pl` 生成 SVG：

```bash
clickhouse-client --query="
SELECT arrayStringConcat(
    arrayMap(x -> concat(demangle(addressToSymbol(x)), '_[k]'), trace),
    ';') AS stack,
    count() AS cnt
FROM system.trace_log
WHERE query_id = 'xxxx' AND trace_type = 'CPU'
GROUP BY stack
" --format=TabSeparated \
  | flamegraph.pl > query.svg
```

看 CPU 热点是"读取列"还是"聚合函数"还是"JOIN"，能直接决定下一步优化方向。

### 11.6 一些高频慢查询模式

**模式 A：主键没命中**

现象：`EXPLAIN indexes=1` 显示 `Granules: 56789/56789`。

原因：WHERE 条件没有用主键前缀字段，或者用了函数包裹（`toYYYYMM(event_date)` 在 `event_date` 是主键列时仍然能下推，但 `substring(tenant_name, 1, 3)` 就不行）。

修复：改查询条件或调整主键顺序。必要时加 projection 提供另一种排序。

**模式 B：读取列过多**

现象：扫描 row 数少，但 `read_bytes` 巨大。

原因：宽列 `properties String` 被无意义地拉进来。

修复：避免 `SELECT *`；对宽列建 PREWHERE 过滤；把 `properties` 换成 `Map(String, String)` 按需展开。

**模式 C：分布式聚合不均衡**

现象：查询慢，但只有一个 shard 的 CPU 跑满。

原因：sharding_key 分布不均（如大租户数据集中在一个 shard）。

修复：改 sharding_key（需要迁移）；或对大租户查询走 shard-local。

**模式 D：Merge 抢资源**

现象：白天正常，晚上慢，`system.merges` 里几十个并发 merge。

原因：白天攒的小 part 集中在晚上 merge。

修复：调整 `background_pool_size` 和 `max_bytes_to_merge_at_max_space_in_pool`，或者把 merge 窗口移到查询低峰。

## 十二、监控

### 12.1 核心指标清单

下面这些指标每个生产集群都应该监控：

- `ClickHouseAsyncMetrics_ReplicasMaxAbsoluteDelay`：副本最大延迟（秒）
- `ClickHouseMetrics_ReadonlyReplica`：只读副本数
- `ClickHouseMetrics_DelayedInserts`：被限流的 insert 数
- `ClickHouseMetrics_MemoryTracking`：内存占用
- `ClickHouseMetrics_BackgroundPoolTask`：后台 merge 任务数
- `ClickHouseMetrics_DistributedFilesToInsert`：分布式表待转发的文件数
- `ClickHouseMetrics_Query`：正在运行的查询数
- `ClickHouseAsyncMetrics_MaxPartCountForPartition`：单分区 part 数最大值
- `ClickHouseProfileEvents_RejectedInserts`：被拒绝的 insert（太多 part）
- `ClickHouseProfileEvents_ZooKeeperHardwareExceptions`：Keeper 硬件异常数

### 12.2 Prometheus Exporter

ClickHouse 自带 `/metrics` 端点，在 `config.xml` 开启：

```xml
<prometheus>
    <endpoint>/metrics</endpoint>
    <port>9363</port>
    <metrics>true</metrics>
    <events>true</events>
    <asynchronous_metrics>true</asynchronous_metrics>
    <status_info>true</status_info>
</prometheus>
```

Prometheus scrape 配置：

```yaml
- job_name: clickhouse
  static_configs:
    - targets:
        - shard01-a.example.com:9363
        - shard01-b.example.com:9363
        - shard02-a.example.com:9363
        - shard02-b.example.com:9363
  metrics_path: /metrics
```

### 12.3 Grafana Dashboard

官方和社区有几个推荐：

- 官方 ClickHouse Dashboard（ID `14192`）：综合面板
- Altinity Dashboard（ID `13500`）：细粒度指标，含 Keeper
- 自建：把上面的关键指标搭一个"集群一屏"，用 `by (instance)` 分组

### 12.4 告警规则参考

```yaml
groups:
- name: clickhouse
  rules:
  - alert: ClickHouseReplicaReadOnly
    expr: ClickHouseMetrics_ReadonlyReplica > 0
    for: 5m
    annotations:
      summary: "ClickHouse 副本进入只读 ({{ $labels.instance }})"

  - alert: ClickHouseReplicaLag
    expr: ClickHouseAsyncMetrics_ReplicasMaxAbsoluteDelay > 120
    for: 10m
    annotations:
      summary: "ClickHouse 副本延迟 > 2 分钟"

  - alert: ClickHousePartCountHigh
    expr: ClickHouseAsyncMetrics_MaxPartCountForPartition > 200
    for: 15m
    annotations:
      summary: "ClickHouse 单分区 part 数过多，即将触发 Too many parts"

  - alert: ClickHouseRejectedInserts
    expr: rate(ClickHouseProfileEvents_RejectedInserts[5m]) > 0
    for: 5m
    annotations:
      summary: "ClickHouse 有 insert 被拒绝"

  - alert: ClickHouseDistributedFilesToInsertHigh
    expr: ClickHouseMetrics_DistributedFilesToInsert > 10000
    for: 10m
    annotations:
      summary: "分布式表待转发文件数过高，下游可能挂了"

  - alert: ClickHouseKeeperConnectionLost
    expr: rate(ClickHouseProfileEvents_ZooKeeperHardwareExceptions[5m]) > 0
    for: 5m
    annotations:
      summary: "ClickHouse Keeper 连接异常"
```

## 十三、备份恢复

### 13.1 内置 BACKUP / RESTORE

23.x 以后的 ClickHouse 自带 `BACKUP TABLE ... TO` 语法，支持本地、S3、磁盘。

```sql
-- 备份到本地路径（需要在 <backups> 配置中允许）
BACKUP TABLE db.events_local TO Disk('backups', 'events_local_20260315.zip');

-- 备份到 S3
BACKUP TABLE db.events_local TO S3(
    'https://s3.example.com/ch-backups/events_local/20260315/',
    'AKIAxxxxxxxx',
    'xxxxxxxxxxxxxxxx'
);

-- 全库备份
BACKUP DATABASE db TO S3(...);

-- 集群备份
BACKUP DATABASE db ON CLUSTER analytics_cluster TO S3(...);
```

恢复：

```sql
RESTORE TABLE db.events_local FROM Disk('backups', 'events_local_20260315.zip');

RESTORE TABLE db.events_local AS db.events_local_restore
FROM S3('https://s3.example.com/ch-backups/events_local/20260315/', ...);
```

`config.xml` 里声明 backup disk：

```xml
<backups>
    <allowed_path>/data/clickhouse/backups/</allowed_path>
    <allowed_disk>backups</allowed_disk>
</backups>

<storage_configuration>
    <disks>
        <backups>
            <type>local</type>
            <path>/data/clickhouse/backups/</path>
        </backups>
    </disks>
</storage_configuration>
```

### 13.2 clickhouse-backup 工具

社区维护的工具，功能比内置 BACKUP 更全：

- 支持 remote（S3、GCS、SFTP）
- 增量备份
- 按表/库/pattern 过滤
- 定时任务友好

安装略。典型配置 `/etc/clickhouse-backup/config.yml`：

```yaml
general:
  remote_storage: s3
  max_file_size: 0
  backups_to_keep_local: 3
  backups_to_keep_remote: 30

clickhouse:
  username: backup_user
  password: xxxxxxxx
  host: localhost
  port: 9000

s3:
  access_key: AKIAxxxxxxxx
  secret_key: xxxxxxxxxxxxxxxx
  bucket: ch-backups
  endpoint: https://s3.example.com
  region: us-east-1
  path: /analytics-cluster/{shard}
  compression_level: 3
  compression_format: lz4
```

操作：

```bash
# 创建本地快照
clickhouse-backup create daily_$(date +%F)

# 上传到 S3
clickhouse-backup upload daily_$(date +%F)

# 列出备份
clickhouse-backup list

# 下载并恢复
clickhouse-backup download daily_20260315
clickhouse-backup restore daily_20260315 --rm
```

### 13.3 备份策略建议

- **元数据快照**：每天一次全库 schema 备份（`SHOW CREATE TABLE` 导出），独立于数据备份
- **数据全量**：每周一次全量，每天增量
- **跨区域冗余**：备份写到跨 AZ 的对象存储
- **定期恢复演练**：每季度至少做一次"从备份恢复 1 张表到测试集群"的端到端验证

### 13.4 不要依赖副本当备份

副本是"高可用"不是"备份"：

- `DROP TABLE` 会在所有副本上执行
- `ALTER DELETE` 会在所有副本上执行
- 逻辑故障（程序误删）所有副本同时中枪

备份必须是"离线 + 时间点可回溯"的才算数。

## 十四、版本升级

### 14.1 兼容性原则

ClickHouse 的版本号看起来像 `24.3.x.y`，前两位是 YY.M（年份和月份）。一般来说：

- 主版本向前兼容，老版本写的数据新版本能读
- 新特性可能只在更高的 `use_*` settings 下启用，默认保持旧行为
- 文件格式版本化，rollback 到低版本通常也能启动（但可能丢失新特性）
- 官方推荐订阅 `stable` 或 `lts` 分支，不要跟 `testing`

### 14.2 灰度升级流程

假设 32 节点（16 shard × 2 副本）升级：

1. 在测试集群先验证目标版本能读写生产的 table schema
2. 锁定 DDL：通过告警约定期间不做 `ON CLUSTER` 变更
3. 先升一个 shard 的一个副本（shard01-b）
   - 停 clickhouse-server
   - 备份 `/var/lib/clickhouse/metadata`
   - 升级包并启动
   - 观察 `system.replicas` 是否同步追上
   - 业务观察 30 分钟无异常
4. 再升同 shard 的另一个副本（shard01-a）
5. 按 shard 顺序逐步推进，每升一个 shard 等 15 分钟
6. 升级完成后逐步释放 DDL 限制

### 14.3 回滚方法

如果新版本启动后发现功能异常：

1. 先确认是否可以只改 setting（多数"新行为"都能通过 setting 关掉）
2. 必要时降级：停服务，`apt install clickhouse-server=<old_version>`，启动
3. 降级后 `system.replicas` 可能报告 metadata 版本不匹配，一般通过 `DETACH/ATTACH` 解决
4. 大版本跨越（比如从 24.x 降到 22.x）有风险，不推荐

提前准备 rollback 脚本是升级的必要条件。

## 十五、坑位合集

### 15.1 Too many parts

**现象**：`DB::Exception: Too many parts (300). Parts cleaning are processing significantly slower than inserts`

**原因**：写入频率太高或批次太小，merge 跟不上。

**修复**：

```sql
-- 临时放宽
ALTER TABLE events_local
MODIFY SETTING parts_to_throw_insert = 600,
               parts_to_delay_insert = 300;

-- 增大后台 merge 池（需要重启）
-- <background_pool_size>32</background_pool_size>

-- 强制合并
OPTIMIZE TABLE events_local PARTITION '202603' FINAL;
```

从根子上修：加大单次写入批次；用 async_insert；检查 PARTITION BY 是否太细。

### 15.2 Merge 跟不上

**现象**：`system.merges` 持续 > 10 个并发，`system.parts` 小 part 堆积。

**原因**：

- 后台线程池小
- 磁盘慢
- mutation 抢了 merge 线程
- TTL merge 挤占资源

**修复**：

```sql
SELECT database, table, elapsed,
       progress, num_parts, total_size_bytes_compressed / 1024 / 1024 AS mb,
       merge_type
FROM system.merges
ORDER BY elapsed DESC;

-- 看哪个表最拖后腿
SELECT database, table, count() AS running
FROM system.merges
GROUP BY database, table
ORDER BY running DESC;
```

根据结果：

- 如果是 TTL_DELETE 占满，降低 TTL 触发频率或手动错峰触发
- 如果是 mutation 占满，尝试 `KILL MUTATION` 掉不必要的（见下）
- 如果只是 RegularMerge 跟不上，扩容磁盘吞吐或 `background_pool_size`

### 15.3 Mutation 无法取消

**现象**：`ALTER UPDATE` 或 `ALTER DELETE` 执行中发现错了，想取消。

**坑**：ClickHouse 的 mutation 是"写入一条 mutation 日志 + 异步重写 part"。`KILL MUTATION` 只能阻止还没开始的 part，对正在重写的 part 无能为力。

**修复**：

```sql
-- 查看 mutation 列表
SELECT database, table, mutation_id, command,
       create_time, is_done, parts_to_do, latest_failed_part, latest_fail_reason
FROM system.mutations
WHERE is_done = 0;

-- 尝试 kill
KILL MUTATION WHERE mutation_id = 'mutation_12345.txt';

-- 如果真的卡死，最后手段：手动删 Keeper 上的 mutation znode
-- 先 stop 掉副本表 replication
SYSTEM STOP REPLICATED SENDS db.events_local;
-- 删 znode（危险操作，先在测试集群练手）
-- /usr/bin/clickhouse-keeper-client ...
-- SYSTEM START REPLICATED SENDS db.events_local;
```

避免这个坑的办法：mutation 前先在 `WHERE` 条件下用 `SELECT` 确认影响范围，带 `LIMIT`；大批量删除优先考虑 `ALTER TABLE DROP PARTITION` 而不是 `DELETE`。

### 15.4 分布式 DDL 超时

**现象**：`DB::Exception: Watching task /clickhouse/task_queue/ddl/query-xxxx is executing longer than distributed_ddl_task_timeout`

**原因**：某个节点执行 DDL 太慢，或该节点离线。

**修复**：

```sql
-- 查看 DDL 队列
SELECT * FROM system.distributed_ddl_queue
WHERE cluster = 'analytics_cluster'
ORDER BY query_create_time DESC
LIMIT 10;

-- 如果确认 DDL 已经在大多数节点完成，只是个别节点超时
-- 不需要做什么，超时的节点会在恢复后继续执行队列

-- 如果要强制跳过某个节点（危险）
-- 删除 Keeper 上对应节点的 finished 记录让它重新执行，或者
-- 把那个节点从 macros/remote_servers 临时移除
```

避免这个坑：变更窗口前确认所有节点都在线；调大 `distributed_ddl_task_timeout`；大表 ALTER 分开执行，不要在一个事务里改多个 shard。

### 15.5 PartitionsToThrowInsert

**现象**：`Too many partitions for single INSERT block (more than 100)`

**原因**：单次 INSERT 的数据跨了太多分区，通常是批次里 `event_date` 跨度过大。

**修复**：

```sql
-- 临时放宽
SET max_partitions_per_insert_block = 1000;
```

根本修复：客户端按分区预聚合后再 insert；或调整 PARTITION BY 粗粒度。

### 15.6 ZooKeeper 会话失联

**现象**：`Code: 999. DB::Exception: Session expired`

**原因**：

- Keeper/ZK 自身重启
- 网络抖动 > `session_timeout_ms`
- CH server JVM GC 类问题（CH 没 JVM，这里指长时间 stop-the-world 型的系统卡顿，如 swap）

**修复**：

```sql
-- 看会话状态
SELECT * FROM system.zookeeper_connection;

-- 重启副本
SYSTEM RESTART REPLICA db.events_local;
```

预防：禁用 swap；`session_timeout_ms` 不要设得太小（默认 30s 起步）；Keeper 机器单独部署，别和 CH 混部。

### 15.7 DROP TABLE 卡死

**现象**：`DROP TABLE` 久久不返回，其他会话查该表也卡。

**原因**：表上有正在执行的查询，DROP 等它们结束。

**修复**：

```sql
-- 看有没有正在跑的查询
SELECT query_id, user, query FROM system.processes WHERE has(tables, 'db.events_local');

-- kill 掉
KILL QUERY WHERE query_id = 'xxxx';

-- 也可以用 DROP TABLE ... SYNC 强制同步
DROP TABLE db.events_local SYNC;
```

### 15.8 空 IN 子查询导致全扫

**现象**：`SELECT ... WHERE tenant_id IN (SELECT id FROM tenants WHERE ...)` 在子查询结果为空时，优化器没把它变成 `false`，触发全表扫。

**修复**：业务代码先判断子查询结果是否为空；或升级到 23.x+，优化器已经处理这种 case。

### 15.9 LowCardinality(Nullable) 的坑

`LowCardinality(Nullable(String))` 写法看起来合理，但有性能陷阱：字典编码会多一层 null 标记。对高基数列不要用 LowCardinality，对可空列考虑用 `String DEFAULT ''` 替代。

### 15.10 ORDER BY 改不了

一旦表创建完成，`ORDER BY` 无法通过 ALTER 修改。只能：

1. 新建一张结构相同但 ORDER BY 不同的表
2. `INSERT INTO new_table SELECT * FROM old_table`
3. RENAME 切换
4. DROP old_table

好消息是可以通过 ADD PROJECTION 绕开，projection 可以提供另一个 ORDER BY 的物化视图。

## 十六、生产落地 checklist

上线前对照下面这份清单：

- [ ] Keeper 独立部署（3 或 5 节点），和 CH server 不混部，独立磁盘
- [ ] 所有生产表使用 Replicated* 引擎，zookeeper path 中含 `{shard}` 宏
- [ ] macros.xml 中 `shard` 和 `replica` 的值每台机器唯一，且经过交叉检查
- [ ] 分布式表 `remote_servers` 全部 `internal_replication=true`
- [ ] PARTITION BY 按月或更粗粒度；单表 part 总数预估 < 10000
- [ ] ORDER BY 前 2～3 个字段是主要过滤字段，经过 `EXPLAIN indexes` 验证
- [ ] 写入路径固定批次 ≥ 50K 行、频率 ≤ 1 次/秒，或用 async_insert
- [ ] 监控接入 Prometheus，关键告警（Readonly / Lag / Part Count / Rejected Insert）全开
- [ ] 备份：至少有一份离线备份（内置 BACKUP 或 clickhouse-backup），每月恢复演练一次
- [ ] 升级前有回滚脚本和备份，按 shard 灰度
- [ ] TTL 覆盖所有大表，冷热分层策略已验证
- [ ] `max_memory_usage` / `max_memory_usage_for_user` 按业务配置，避免单查询吃爆内存
- [ ] 定期审视 `system.query_log`，维护一张"Top N 慢查询"看板
- [ ] 变更窗口：所有 `ON CLUSTER` DDL 在低峰期执行，提前通知
- [ ] 禁用 swap，disable THP，`ulimit -n` 调到 500000 以上

## 十七、写在最后

ClickHouse 的学习曲线比大多数 OLAP 产品都陡：同样是"副本表"，它的语义和 MySQL/PostgreSQL 主从完全不同；同样是"物化视图"，它是 insert trigger 不是定时刷新；同样是"UPDATE"，它是异步 mutation 不是事务。但一旦理解了"列存 + MergeTree + Keeper 协调"这三件套，后续的性能调优和故障排查会顺畅很多。

真正把 ClickHouse 用明白的团队，通常都经过一次"某张表 part 数爆掉" / "某个副本进入 READONLY" / "某个大 mutation 卡住" 这样的事故洗礼。与其等事故发生再翻文档，不如上线前就按本文 checklist 逐条走一遍，能省下大量深夜加班的时间。

祝你和你的集群都健康。
