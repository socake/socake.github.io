---
title: "PostgreSQL 膨胀治理：把 autovacuum 调到你真正需要的样子"
date: 2024-10-29T09:30:00+08:00
draft: false
tags: ["PostgreSQL", "autovacuum", "膨胀", "数据库运维"]
categories: ["数据库"]
description: "深入讲解 PostgreSQL 的 MVCC 膨胀机制、autovacuum 的工作原理、几十个相关参数的含义与调优方法，以及三个真实的生产膨胀故障复盘。基于 PostgreSQL 16/17 版本，覆盖 cost-based throttling、per-table 调参、freeze 风暴规避等进阶话题。"
summary: '大部分 PostgreSQL DBA 对 autovacuum 的理解停留在"它会自己跑"，但一旦膨胀起来才发现：默认参数对现代硬件完全不够用，几十个 autovacuum_* 参数各管一摊，出了问题根本不知道从哪儿看。这篇文章把我在几套 PG 集群上治理膨胀的经验整理出来，从 MVCC 原理讲到参数调优、从监控到应急处置。'
toc: true
math: false
diagram: false
keywords: ["PostgreSQL", "autovacuum", "bloat", "MVCC", "freeze", "vacuum_cost_delay"]
params:
  reading_time: true
---

## 从一次膨胀事故说起

几年前我接手过一套 PG 12 集群，上线两年从来没人管过 autovacuum。某天发现一张 200GB 的订单表磁盘占用突然涨到 650GB，查询慢到无法接受。用 `pgstattuple` 扫了一下，dead tuple 比例 67%。

那次我花了三天时间把这张表 `VACUUM FULL` 了一遍，期间业务只能走从库。事后我才意识到：**autovacuum 不是"自动"，它是一套需要精细调参的机制**，默认值在 2003 年设计时假设磁盘是机械盘、单机只有几 GB 内存，早就过时了。

这篇文章是我之后几年在 PG 膨胀治理上积累的经验。目标读者是：已经在生产跑 PG、但还没系统性理解 autovacuum 的 DBA。本文基于 PostgreSQL 16/17 版本，涉及 17 的新特性会明确标注。

## 一、MVCC 与膨胀：先理解原理

### 1.1 MVCC 简述

PostgreSQL 用 MVCC（Multi-Version Concurrency Control）实现事务隔离。每次 UPDATE 不是原地修改，而是**在同一个 page 里写一个新版本**，老版本标记为"过期"但暂时不删除。DELETE 也只是打个过期标记。

每行数据有两个隐藏列：`xmin`（创建事务 ID）和 `xmax`（删除事务 ID）。一个元组对某个事务可见的条件简化版是：

```
xmin < 我的事务 ID && (xmax = 0 || xmax > 我的事务 ID)
```

所以只要有老事务还在运行，它看得到的老版本就不能被删。这就是"长事务阻塞 vacuum"的原理。

### 1.2 Dead Tuple 和 Bloat

过期但还没清理的元组叫 dead tuple。它们占用磁盘和内存 page，但对查询无用，甚至会拖慢：

- 全表扫描要跳过 dead tuple，变慢
- 索引扫描遇到 dead tuple 要二次确认，变慢
- page 里 dead tuple 多了还会影响 HOT update 的效率

**膨胀（bloat）** 就是 dead tuple 累积到一定程度后的状态。衡量方法：

```sql
-- 需要 pgstattuple 扩展
CREATE EXTENSION pgstattuple;

SELECT * FROM pgstattuple('orders');
-- dead_tuple_percent 这个字段就是膨胀率
```

健康表的 dead_tuple_percent 应该 < 10%，> 20% 就要警惕，> 50% 基本得 VACUUM FULL 或 pg_repack 才能救。

### 1.3 VACUUM 做了什么

VACUUM 的工作非常简单：**把 dead tuple 清理掉，把空间标记为可用**。但不收缩文件大小（那是 VACUUM FULL 的事）。

VACUUM 分三种：

1. **autovacuum**：后台进程自动触发，是生产主力
2. **手动 VACUUM**：`VACUUM table;`，用于补救和维护窗口
3. **VACUUM FULL**：重写整张表，彻底收缩空间，但要 ACCESS EXCLUSIVE 锁，业务不可用

日常靠 autovacuum，大清洗靠 VACUUM FULL，但 VACUUM FULL 实际生产用得少，大家更常用 `pg_repack`（无锁重建表）。

## 二、Autovacuum 的触发条件

一张表什么时候会被 autovacuum 挑中？核心公式：

```
autovacuum 触发阈值 = autovacuum_vacuum_threshold
                  + autovacuum_vacuum_scale_factor * 表总行数

autoanalyze 触发阈值 = autovacuum_analyze_threshold
                   + autovacuum_analyze_scale_factor * 表总行数
```

默认值：

| 参数                                  | 默认值 | 含义                          |
|---------------------------------------|--------|-------------------------------|
| autovacuum_vacuum_threshold           | 50     | 至少 50 行变更才考虑          |
| autovacuum_vacuum_scale_factor        | 0.2    | 20% 行变更                    |
| autovacuum_analyze_threshold          | 50     | 至少 50 行变更                |
| autovacuum_analyze_scale_factor       | 0.1    | 10% 行变更                    |
| autovacuum_naptime                    | 1min   | worker 检查间隔               |
| autovacuum_max_workers                | 3      | 同时运行的 worker 数          |

对 1 亿行的大表，默认要等 2000 万行变更才触发 vacuum，这在现代高并发业务下太宽松了。

### 2.1 scale_factor 的陷阱

20% 对小表合适，对大表是灾难。假设一张 10 亿行的表：

- 20% = 2 亿行 dead tuple 才触发
- 单次 vacuum 要处理几百 GB 数据
- 跑一次 vacuum 几小时起步
- 期间 vacuum worker 一直占着，其他表排队

正确做法是**按表粒度单独配置**：

```sql
ALTER TABLE orders SET (
  autovacuum_vacuum_scale_factor = 0.01,       -- 1%
  autovacuum_vacuum_threshold = 10000,          -- 或至少 1 万行
  autovacuum_analyze_scale_factor = 0.005,
  autovacuum_analyze_threshold = 5000
);
```

小表保留默认，大表用这种激进参数，让 vacuum 跑得频繁、每次处理的数据量小。

## 三、Cost-Based Throttling：最关键也最难理解

autovacuum 为了不影响业务，用了一套 cost-based 限速机制：vacuum 做事累积 cost，到 cost limit 就 sleep 一下。

### 3.1 核心参数

| 参数                          | 默认值（16/17）     | 说明                           |
|-------------------------------|---------------------|--------------------------------|
| vacuum_cost_page_hit          | 1                   | 命中 shared buffer 的 page     |
| vacuum_cost_page_miss         | 2 (PG14+)           | 需要从 OS cache 读的 page      |
| vacuum_cost_page_dirty        | 20                  | 修改了 page                    |
| vacuum_cost_limit             | 200                 | 累积到多少开始 sleep           |
| autovacuum_vacuum_cost_limit  | -1（用上面的值）    | autovacuum 专用 limit          |
| autovacuum_vacuum_cost_delay  | **2ms** (PG12+)     | 每次 sleep 的时长              |

PG 12 之前 `autovacuum_vacuum_cost_delay` 默认是 20ms，12 改成了 2ms。这个改动很关键——老版本的 autovacuum 默认是"龟速"的，升上来之后如果 SSD 机器记得把 delay 显式配成 2ms 或更低。

### 3.2 算一下吞吐

默认参数下 autovacuum 的理论吞吐：

```
每秒最多 cost = 200 limit / 2ms delay * 1000 = 100000 cost / s
每个 dirty page = 20 cost
=> 每秒最多处理 5000 dirty page = 40MB/s（page size 8KB）
```

40MB/s 在 NVMe 上是**严重浪费磁盘能力**。现代 SSD 应该把它放开：

```ini
# postgresql.conf
autovacuum_vacuum_cost_limit = 2000        # 10x
autovacuum_vacuum_cost_delay = 2ms          # 默认
# => 理论吞吐 ~400MB/s
```

或者换算成 IO：NVMe 能做 50k IOPS，vacuum 用其中 10% = 5000 IOPS 就够猛了，对应 cost limit 2000-4000 是合理值。

### 3.3 业务压力期间怎么办

vacuum 跑太猛会影响业务延迟。折中方案：

```ini
# 默认温和
autovacuum_vacuum_cost_limit = 1000
autovacuum_vacuum_cost_delay = 2ms

# 晚上跑 cron 手动加速
# SELECT set_config('vacuum_cost_limit', '10000', false);
# VACUUM (VERBOSE) big_table;
```

或者用 PG 13+ 的 parallel vacuum：

```sql
VACUUM (PARALLEL 4) big_table;
```

注意 parallel 只对索引阶段有效，堆阶段仍然单线程。

## 四、Freeze：另一个容易踩坑的概念

### 4.1 Transaction ID Wraparound

PostgreSQL 的事务 ID 是 32bit，大约 40 亿。为了防止回绕（wraparound）造成数据"消失"，PG 会定期把老元组的 xmin 改成一个特殊的 `FrozenXid`，表示"这行对所有事务都可见，不用再比较"。这个过程叫 freeze。

autovacuum 被强制触发 freeze 的条件：

```
最老未 freeze 事务 ID 距离当前超过 autovacuum_freeze_max_age (默认 2 亿)
```

一旦触发，这个 vacuum 是"抗不得的"，无论 `autovacuum = off` 都照跑，叫 **aggressive vacuum for wraparound**。大表的 aggressive vacuum 可能跑几小时甚至几天，期间 CPU/IO 飙高，业务抖动。

### 4.2 Freeze 风暴

如果多张大表同时达到 freeze 阈值，就是 freeze 风暴。典型症状：

- 多个 autovacuum worker 同时跑全表 freeze
- `pg_stat_activity` 里一堆 `autovacuum: VACUUM public.xxx (to prevent wraparound)`
- IO 打满，业务查询延迟飙升

规避方法：

1. **提前分散触发**：给大表设不同的 `autovacuum_freeze_max_age`，错开时间
2. **日常做 vacuum freeze**：在业务低峰期主动跑 `VACUUM FREEZE`，把老元组处理掉
3. **PG 17 的 VISIBILITY_MAP 优化**：17 版本引入了 `vacuum_freeze_min_age` 的动态调整，让 vacuum 做普通工作时顺便 freeze 一部分，降低集中 freeze 的压力

监控 freeze 进度：

```sql
SELECT
  c.oid::regclass AS table_name,
  age(c.relfrozenxid) AS xid_age,
  pg_size_pretty(pg_table_size(c.oid)) AS size
FROM pg_class c
WHERE c.relkind = 'r'
  AND age(c.relfrozenxid) > 100000000
ORDER BY xid_age DESC
LIMIT 20;
```

`xid_age > 200000000` 就要准备好承担 freeze 风暴，> 15 亿是紧急告警。

### 4.3 PG 17 的改进

PG 17 在 freeze 方面有几个实打实的优化：

- **Streaming I/O**：vacuum 的顺序读用上了 async I/O，大表 vacuum 快 20-30%
- **WAL 减少**：freeze 产生的 WAL 量显著降低
- **Progress reporting 增强**：`pg_stat_progress_vacuum` 视图更详细

如果你在跑 PG 13-16，升级到 17 是个明确的性能收益。

## 五、监控膨胀的几种姿势

### 5.1 快速版：pg_stat_user_tables

```sql
SELECT
  schemaname, relname,
  n_live_tup, n_dead_tup,
  round(100.0 * n_dead_tup / NULLIF(n_live_tup + n_dead_tup, 0), 2) AS dead_pct,
  last_autovacuum, last_autoanalyze
FROM pg_stat_user_tables
WHERE n_live_tup + n_dead_tup > 10000
ORDER BY dead_pct DESC NULLS LAST
LIMIT 20;
```

优点：快、不扫表。缺点：`n_dead_tup` 是估算值，不够准。适合日常巡检。

### 5.2 准确版：pgstattuple

```sql
CREATE EXTENSION IF NOT EXISTS pgstattuple;

SELECT
  table_name,
  pg_size_pretty(table_len) AS total,
  tuple_count, tuple_percent,
  dead_tuple_count, dead_tuple_percent,
  free_percent
FROM pgstattuple('orders') t, (VALUES ('orders')) v(table_name);
```

准确但会扫全表，GB 级以上的表要避开业务高峰跑。

PG 提供了一个采样版：`pgstattuple_approx('table')`，扫 1% 估算，速度快很多。

### 5.3 索引膨胀

索引膨胀和表膨胀独立。查询：

```sql
CREATE EXTENSION IF NOT EXISTS pgstattuple;

SELECT
  i.indexrelname AS index_name,
  pg_size_pretty(pg_relation_size(i.indexrelid)) AS size,
  (pgstatindex(i.indexrelid)).*
FROM pg_stat_user_indexes i
WHERE pg_relation_size(i.indexrelid) > 100000000  -- > 100MB
ORDER BY pg_relation_size(i.indexrelid) DESC
LIMIT 10;
```

`avg_leaf_density` 低于 50% 就说明索引膨胀严重，需要 `REINDEX CONCURRENTLY`。

## 六、应急处置：膨胀已经发生了怎么办

### 6.1 方案对比

| 方法               | 锁              | 空间收缩 | 速度     | 适用场景                |
|--------------------|-----------------|----------|----------|-------------------------|
| VACUUM             | SHARE UPDATE    | 否       | 快       | 日常维护                |
| VACUUM FULL        | ACCESS EXCL     | 是       | 慢       | 维护窗口、小表          |
| CLUSTER            | ACCESS EXCL     | 是       | 中       | 需要物理排序            |
| pg_repack          | 轻微，几乎无锁  | 是       | 慢       | **生产首选**            |
| pg_squeeze         | 轻微            | 是       | 中       | 定时重建                |
| 建新表+切换        | 切换瞬间锁      | 是       | 极慢     | 超大表、其他方案不行    |

### 6.2 pg_repack 实战

pg_repack 的原理：创建一张临时表，复制数据到临时表（用触发器捕获期间的变化），然后做原子切换。过程中只在最后切换时短暂锁表。

```bash
# 单表重建
pg_repack -h localhost -d mydb -t orders --no-superuser-check

# 只重建索引
pg_repack -h localhost -d mydb -t orders --only-indexes

# 整库
pg_repack -h localhost -d mydb
```

几个注意事项：

1. **需要两倍磁盘空间**，不然中间会写满
2. **主键/唯一索引必须存在**
3. **长事务会阻塞 pg_repack 切换阶段**，跑之前 kill 掉
4. **大表 repack 可能跑几小时**，期间 WAL 会飙升，从库延迟要监控
5. **PG 17 配合新 WAL 优化后 repack 效率提升明显**

一个生产脚本示范：

```bash
#!/bin/bash
set -e

TABLE=$1
DB=mydb

# 前置检查
DEAD_PCT=$(psql -tAc "SELECT round(100.0 * n_dead_tup / NULLIF(n_live_tup + n_dead_tup, 0), 2)
                     FROM pg_stat_user_tables WHERE relname = '$TABLE'")
echo "Table $TABLE dead tuple: ${DEAD_PCT}%"

if (( $(echo "$DEAD_PCT < 20" | bc -l) )); then
  echo "Below threshold, skip"
  exit 0
fi

# 检查长事务
LONG_TX=$(psql -tAc "SELECT count(*) FROM pg_stat_activity
                    WHERE state = 'active' AND xact_start < now() - interval '5 min'")
if [ "$LONG_TX" -gt 0 ]; then
  echo "Long transactions detected, aborting"
  exit 1
fi

# 跑 repack
pg_repack -d $DB -t $TABLE --jobs 4

# 跑完 analyze
psql -c "ANALYZE $TABLE"
```

### 6.3 索引 REINDEX CONCURRENTLY

PG 12 引入的 `REINDEX CONCURRENTLY` 不锁表，生产可以随时跑：

```sql
REINDEX INDEX CONCURRENTLY orders_user_id_idx;
REINDEX TABLE CONCURRENTLY orders;
REINDEX (VERBOSE) TABLE CONCURRENTLY orders;
```

速度比 pg_repack 慢一些但更简单，如果只是索引膨胀推荐这个。

## 七、配置模板

下面是我常用的 postgresql.conf 关于 vacuum 的部分，假设 64GB 内存、NVMe SSD、OLTP 业务：

```ini
# === Autovacuum ===
autovacuum = on
autovacuum_max_workers = 6                     # 默认 3，大库加到 6-10
autovacuum_naptime = 15s                        # 默认 1min，调紧
autovacuum_vacuum_threshold = 50
autovacuum_analyze_threshold = 50
autovacuum_vacuum_scale_factor = 0.05           # 默认 0.2，大表单独配
autovacuum_analyze_scale_factor = 0.02
autovacuum_freeze_max_age = 200000000           # 默认
autovacuum_multixact_freeze_max_age = 400000000

# === Cost-based throttling ===
autovacuum_vacuum_cost_delay = 2ms               # PG12+ 默认，显式写
autovacuum_vacuum_cost_limit = 2000              # 默认 200 太保守

# === Vacuum 工作内存 ===
maintenance_work_mem = 2GB                       # autovacuum worker 每个能用的内存
autovacuum_work_mem = -1                          # 继承上面

# === Freeze ===
vacuum_freeze_min_age = 50000000                 # 默认 5000 万
vacuum_freeze_table_age = 150000000              # 默认 1.5 亿

# === WAL 相关（间接影响 vacuum 效果）===
wal_compression = on                              # 减少 freeze WAL
```

给核心大表的 per-table 配置：

```sql
ALTER TABLE orders SET (
  autovacuum_vacuum_scale_factor = 0.01,
  autovacuum_vacuum_threshold = 10000,
  autovacuum_analyze_scale_factor = 0.005,
  autovacuum_vacuum_cost_limit = 5000,
  fillfactor = 90                             -- 留空间给 HOT update
);
```

`fillfactor = 90` 的作用：每个 page 留 10% 空间，让 UPDATE 更倾向于 HOT（Heap-Only Tuple），不用更新索引，大幅减少索引膨胀。写入密集型的表值得设 80-85。

## 八、真实故障复盘

### 8.1 长事务把整个数据库的 vacuum 卡住

**现象**：全库所有表的 `n_dead_tup` 都在涨，autovacuum 明明在跑，但一个都清不掉。

**排查**：

```sql
SELECT pid, now() - xact_start AS duration, state, query
FROM pg_stat_activity
WHERE state != 'idle'
ORDER BY xact_start;
```

看到一个连接 state 是 `idle in transaction`，已经保持 18 小时。

**根因**：BI 工具连接池有个脚本开了事务之后没 commit 就断线了，连接池没清理干净。PG 的 vacuum 不能清理比这个事务更新的 dead tuple（因为它可能还要看到）。

**修复**：立即 `SELECT pg_terminate_backend(pid)`，autovacuum 立刻开始生效。

**教训**：

1. 监控 `idle in transaction` 时长，> 30 分钟告警
2. 设 `idle_in_transaction_session_timeout = 600000`（10 分钟）自动 kill
3. 设 `statement_timeout` 防止长查询

### 8.2 XID wraparound 告警

**现象**：`age(relfrozenxid)` 超过 19 亿，再涨就要强制只读模式了。

**排查**：发现 autovacuum 一直在跑那张大表但进度卡在 30%，cost limit 太保守。

**修复**：

1. 紧急手动跑 `VACUUM FREEZE`，临时调大 maintenance_work_mem 到 8GB
2. 把 `vacuum_cost_limit` 临时调到 10000
3. 加并发 `VACUUM (PARALLEL 4) freeze big_table`

跑了 4 小时终于搞定。事后把 `autovacuum_vacuum_cost_limit` 永久调高、大表按表分配不同的 `autovacuum_freeze_max_age` 错开触发。

### 8.3 索引膨胀导致查询变慢

**现象**：一个按 user_id 的索引从几百 MB 涨到 8GB，查询从 5ms 变成 200ms。

**排查**：`pgstatindex` 显示 `avg_leaf_density` 只有 12%，严重膨胀。

**根因**：这个索引用在一个频繁 UPDATE 的字段上，每次 UPDATE 都产生新的 index entry，但对应的老 entry 要等 vacuum 才清理，而且 B-tree 的空 slot 只有在 page 被合并时才回收。

**修复**：`REINDEX INDEX CONCURRENTLY`，跑了 20 分钟，索引从 8GB 降到 600MB。

**长期改进**：

1. 定期 reindex 核心大索引（cron 每月一次）
2. 考虑 HOT update：这张表 `fillfactor` 从 100 改成 85，让 UPDATE 在同一个 page 内写新版本，不用更新索引（如果索引字段没变）

## 九、监控告警清单

```yaml
# prometheus rules
- alert: PGDeadTupleHigh
  expr: |
    pg_stat_user_tables_n_dead_tup / (pg_stat_user_tables_n_live_tup + pg_stat_user_tables_n_dead_tup) > 0.2
  for: 1h
  annotations:
    summary: "表 {{ $labels.relname }} dead tuple 超过 20%"

- alert: PGAutovacuumNotRunning
  expr: time() - pg_stat_user_tables_last_autovacuum > 86400
  for: 10m
  annotations:
    summary: "表 {{ $labels.relname }} 24 小时没跑 autovacuum"

- alert: PGXidWraparound
  expr: pg_database_xid_age > 1500000000
  for: 5m
  annotations:
    summary: "数据库 {{ $labels.datname }} xid age > 15 亿"

- alert: PGLongTransaction
  expr: pg_stat_activity_max_tx_duration > 1800
  for: 5m
  annotations:
    summary: "存在超过 30 分钟的长事务"

- alert: PGIdleInTransaction
  expr: pg_stat_activity_count{state="idle in transaction"} > 10
  for: 10m
```

这些规则配合 postgres_exporter 的 `pg_stat_user_tables` 采集就能跑。注意 postgres_exporter 默认不采集 per-table 指标，需要加 custom queries。

## 十、经验法则

写到这里，我把这些年积累的"膨胀治理心法"浓缩成几条：

- **autovacuum 不是黑盒，它的每个参数都有明确含义，先理解再调**
- **scale_factor 对大表没用，必须 per-table 重配**
- **cost_delay 默认值是给机械盘的，SSD 上把 cost_limit 调大到 2000+**
- **长事务是 vacuum 的头号敌人，比任何参数都重要**
- **freeze 风暴能提前分散触发**
- **pg_repack 是生产救命神器**
- **HOT update 和 fillfactor 能从源头减少索引膨胀**
- **监控要同时看 pg_stat_user_tables 和 pgstattuple，两个数据不一致时以后者为准**
- **PG 17 是真的值得升级**

真正让 PG 稳定运行的，从来不是一套"最佳配置"，而是对业务特征的持续观察和调整。同样是 OLTP，订单库和用户库的 vacuum 策略可能完全不同。希望这篇笔记能帮你少走一些弯路。

参考资料：

- PostgreSQL 16/17 官方文档，Runtime Config - Autovacuum 章节
- Robert Haas、Álvaro Herrera 等 core 成员的 blog（尤其 2ndquadrant 老文章）
- Percona 的 PG 膨胀系列
- `pgstattuple` 和 `pg_repack` 的官方 README
