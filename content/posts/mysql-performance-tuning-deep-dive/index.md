---
title: "MySQL 深度调优：从 Buffer Pool 到锁等待的生产手册"
date: 2024-10-18T14:30:00+08:00
draft: false
tags: ["MySQL", "InnoDB", "数据库调优", "性能优化"]
categories: ["数据库"]
description: "一份面向 MySQL 8.0/8.4 生产环境的深度调优笔记。覆盖 InnoDB 缓冲池分区、redo log capacity、double write、自适应 hash、锁等待诊断、慢 SQL 治理以及几个真实线上故障的复盘。不是参数速查表，而是一份基于实战总结出来的决策框架。"
summary: "你有没有过这种体验：按网上教程把 innodb_buffer_pool_size 调到 75%、关了 query cache、打开了 innodb_file_per_table，然后告诉自己"MySQL 调优就这样了"？真正的调优是一个持续观察、假设、验证、回滚的过程。这篇文章把我在过去几年维护的十几套 MySQL 实例上积累的调参经验整理出来，每一条都能追到具体指标和业务效果。"
toc: true
math: false
diagram: false
keywords: ["MySQL", "InnoDB", "innodb_buffer_pool_size", "redo log", "double write", "锁等待", "慢查询"]
params:
  reading_time: true
---

## 写在前面

MySQL 调优的文章满世界都是，但大部分都在复制粘贴那几个经典参数：buffer pool 75%、log file 1GB、flush method O_DIRECT。这些没错，但也没什么用——它们是 2010 年的建议，在 MySQL 8.0/8.4 时代很多已经过时或默认值就已经对了。

这篇笔记的写作出发点是：**调参之前先想清楚要解决什么问题**。我把过去几年在生产环境遇到的 MySQL 性能问题分类，每一类给出诊断方法、调优参数、效果验证方式，并且穿插真实故障案例。目标读者是：管着几套 MySQL、有一定基础、但还没建立起系统调优框架的 DBA 或 SRE。

本文基于 MySQL 8.0.36 和 8.4 LTS，两个版本在参数默认值和行为上有一些差别，涉及时会明确标注。

## 一、调优之前：先把监控打通

没有监控的调优是在瞎猜。我的最低要求是：

1. **Prometheus + mysqld_exporter**：抓 performance_schema 和 InnoDB metrics
2. **慢查询日志**：long_query_time = 0.5 或更低，配合 pt-query-digest 聚合
3. **Percona PMM 或自建 Grafana**：至少要有 buffer pool 命中率、redo log 写入速率、锁等待、QPS/TPS 大盘

核心指标的报警阈值（参考，按业务调整）：

| 指标                                      | 正常范围       | 告警阈值       |
|-------------------------------------------|----------------|----------------|
| InnoDB Buffer Pool 命中率                 | > 99%          | < 98%          |
| `Innodb_log_waits` 每秒增量                | 0              | > 1            |
| `Threads_running`                         | < 20           | > 50 持续 1min |
| 慢查询数每分钟                            | < 5            | > 20           |
| 复制延迟 `Seconds_Behind_Master`          | < 1s           | > 10s          |
| InnoDB Row Lock Wait Avg                  | < 5ms          | > 50ms         |

下面所有的调优讨论都假设你已经有这些监控数据，否则调什么都白搭。

## 二、InnoDB Buffer Pool：最重要的那一个参数

### 2.1 大小怎么定

老掉牙的建议是"物理内存的 70-80%"。这个值有前提条件：

- 专用数据库服务器
- 没有其他重型进程（比如 Java 应用）
- 操作系统和其他服务能在剩下 20-30% 内存里活下来

实际决策流程：

```
1. 算数据集实际大小（所有 .ibd 文件总和）
   du -sh /var/lib/mysql/*/*.ibd | awk '{s+=$1} END {print s}'

2. 如果数据集 < 可用内存 * 0.7
   → buffer pool = 数据集大小 * 1.2（留 20% 空间给索引和 undo）

3. 如果数据集 >= 可用内存 * 0.7
   → buffer pool = (总内存 - 操作系统预留 - MySQL 其他开销) * 0.9
```

MySQL 8.0 自己的其他内存开销（per_thread_buffers、join_buffer、tmp_table、innodb_additional_mem_pool 等）通常在 2-4GB，算的时候不要忘了减掉。

**一个常见错误**：在 64GB 机器上设 `innodb_buffer_pool_size = 56G`，然后 `max_connections = 2000`，每个连接 `sort_buffer_size + read_buffer_size + join_buffer_size = 4MB`，峰值并发就是 8GB 额外占用，OOM 就等着你。我遇到过一次，机器直接 OOM Killer 干掉 mysqld，业务中断 40 分钟。

### 2.2 Buffer Pool Instances 分区

`innodb_buffer_pool_instances` 在 MySQL 8.0 默认是 8（当 buffer pool > 1GB 时）。这个参数很多人不动，其实值得调：

- Buffer pool < 8GB：1-4 个 instance
- Buffer pool 8-64GB：8 个（默认）
- Buffer pool 64-256GB：16-32 个
- Buffer pool > 256GB：32 个

每个 instance 有独立的 mutex，分得越多锁竞争越少，但管理开销也越大。分区大小建议不小于 1GB，否则 chunk 调度会很零碎。

### 2.3 命中率监控

```sql
SELECT
  (1 - (SELECT VARIABLE_VALUE FROM performance_schema.global_status
        WHERE VARIABLE_NAME = 'Innodb_buffer_pool_reads') /
       (SELECT VARIABLE_VALUE FROM performance_schema.global_status
        WHERE VARIABLE_NAME = 'Innodb_buffer_pool_read_requests')) * 100
  AS hit_rate_percent;
```

健康的 OLTP 系统应该 > 99.5%，低于 99% 说明 buffer pool 偏小。注意这个命中率是累积值，重启后才清零，诊断时要看 Prometheus 的短期 delta。

## 三、Redo Log：8.0.30 之后的新玩法

MySQL 8.0.30 把老的 `innodb_log_file_size + innodb_log_files_in_group` 换成了 `innodb_redo_log_capacity`。行为变了但原理没变：redo log 是 InnoDB 的 WAL，决定写入吞吐和崩溃恢复时间。

### 3.1 Redo Log 太小的症状

```sql
SHOW GLOBAL STATUS LIKE 'Innodb_log_waits';
```

`Innodb_log_waits` 每秒增长哪怕只有 1，都说明 redo log 已经不够了。现象包括：

- 写入 TPS 波动大、有周期性毛刺
- checkpoint age 接近 max checkpoint age，触发同步 flush 导致全库 hang 住几秒
- `SHOW ENGINE INNODB STATUS` 里 `LOG` 段看到 "Checkpoint age too old"

### 3.2 Redo Log Capacity 推荐值

MySQL 8.4 默认 100MB，绝对不够用。生产环境建议：

| 写入 TPS 量级    | innodb_redo_log_capacity |
|------------------|--------------------------|
| < 1k write/s     | 2GB                      |
| 1k-5k write/s    | 8GB                      |
| 5k-20k write/s   | 16GB                     |
| > 20k write/s    | 32GB 或更高              |

调大的副作用：崩溃恢复时间线性增加，32GB redo log 恢复大概 2-5 分钟。如果业务对 RTO 非常敏感，要在恢复时间和写入吞吐之间权衡。

在线修改（8.0.30+）：

```sql
SET GLOBAL innodb_redo_log_capacity = 16 * 1024 * 1024 * 1024;  -- 16GB
```

不需要重启，InnoDB 会自动调整 redo log 文件数量（它维持 32 个文件，每个 = capacity/32）。

### 3.3 Log Buffer

`innodb_log_buffer_size` 默认 16MB，写入密集型调到 64MB-128MB。观察 `Innodb_log_waits` 和 `Innodb_log_write_requests`，如果 wait / write_requests > 0 就加。

## 四、Flush 策略：持久化和性能的博弈

### 4.1 innodb_flush_log_at_trx_commit

这个参数直接决定 RPO：

| 值 | 行为                          | 崩溃风险         | 适用场景       |
|----|-------------------------------|------------------|----------------|
| 1  | 每次 commit 都 fsync          | 0                | 默认/金融      |
| 2  | 每次 commit 写 OS cache，每秒 fsync | 至多丢 1 秒 | 日志、分析型   |
| 0  | 每秒写 OS cache + fsync       | 可能丢 1 秒      | 不推荐         |

**结论**：不要因为"性能"把它改成 2。2 意味着你的 MySQL 掉电会丢数据，除非业务能接受。我遇到过开发图快改成 2，结果机房断电丢了 5 万订单的惨案。

如果要性能又要持久化，正确方向是用 group commit（`binlog_group_commit_sync_delay`）让多个事务攒一起 fsync：

```ini
binlog_group_commit_sync_delay = 1000         # 微秒，等待 1ms
binlog_group_commit_sync_no_delay_count = 20  # 攒够 20 个立即提交
```

### 4.2 sync_binlog

与上面对应，`sync_binlog = 1` 是金融级、=0 是不安全、=N 是每 N 次 commit 做一次 fsync。

**生产唯一正确答案：sync_binlog = 1 + innodb_flush_log_at_trx_commit = 1**（双 1）。性能差？那说明磁盘不行，该换 NVMe 而不是降低一致性。

### 4.3 innodb_doublewrite

默认开启，防止"撕裂写"。有人说关了快一点，在 NVMe 上确实能提升写入 5-10%，但代价是丢失崩溃一致性保护。**除非你用的是支持 atomic write 的存储设备（比如 FusionIO 或 EXT4 with dioread_nolock），否则不要关**。

MySQL 8.0.20 之后 doublewrite 性能已经大幅提升（拆成独立文件，默认 2 个 batch），关闭收益越来越小。

## 五、IO 能力与并发

### 5.1 innodb_io_capacity

告诉 InnoDB 磁盘的 IOPS 能力，决定后台 flush 速率：

| 存储类型          | io_capacity | io_capacity_max |
|-------------------|-------------|-----------------|
| 机械硬盘 RAID10   | 200         | 400             |
| SATA SSD          | 2000        | 4000            |
| NVMe SSD          | 10000       | 20000           |
| 企业级 NVMe       | 20000-50000 | 50000-100000    |

官方默认 200 是给机械盘的，SSD 时代必须调大。调太小的症状：checkpoint age 持续高位、dirty page 比例压不下来、偶发写入卡顿。调太大的症状：后台 IO 抢占前台 IO，反而降低 QPS。

验证方法：跑一段时间 `sysbench oltp_write_only`，观察 `Innodb_buffer_pool_pages_dirty` 是否能稳定在 `innodb_max_dirty_pages_pct`（默认 90）附近，稳不住就加 capacity。

### 5.2 innodb_io_capacity_max 和 pct_lwm

`innodb_max_dirty_pages_pct_lwm = 10`（8.0 默认）是一个"提前刷"的水位，到 10% 就开始加速 flush。业务突发写入大的场景可以保留默认，匀速写入可以调到 0 禁用提前刷。

### 5.3 Purge 线程

`innodb_purge_threads`（默认 4）负责清理 undo。长事务多的业务容易 undo 堆积，导致：

- ibtmp1 或 undo tablespace 膨胀
- 历史版本链变长，二级索引查询变慢
- Purge lag 增大，`SHOW ENGINE INNODB STATUS` 里能看到 `History list length` 飙升

诊断：

```sql
SELECT NAME, COUNT FROM information_schema.innodb_metrics
WHERE NAME = 'trx_rseg_history_len';
```

> 100 万 就要警觉。解决方法：先杀长事务、再考虑 `innodb_purge_threads` 加到 8-16、`innodb_purge_batch_size` 调大到 600-1000。

## 六、锁与事务：最容易被忽视的性能杀手

大部分 MySQL 慢不是因为 CPU 或 IO 不够，而是**锁等待**。

### 6.1 怎么诊断

```sql
-- 当前锁等待
SELECT * FROM performance_schema.data_lock_waits;

-- 锁详情
SELECT
  r.trx_id waiting_trx_id,
  r.trx_mysql_thread_id waiting_thread,
  r.trx_query waiting_query,
  b.trx_id blocking_trx_id,
  b.trx_mysql_thread_id blocking_thread,
  b.trx_query blocking_query
FROM performance_schema.data_lock_waits w
JOIN information_schema.innodb_trx b ON b.trx_id = w.blocking_engine_transaction_id
JOIN information_schema.innodb_trx r ON r.trx_id = w.requesting_engine_transaction_id;
```

长事务检测：

```sql
SELECT trx_id, trx_started, trx_mysql_thread_id, trx_query,
       TIMESTAMPDIFF(SECOND, trx_started, NOW()) AS duration_sec
FROM information_schema.innodb_trx
WHERE TIMESTAMPDIFF(SECOND, trx_started, NOW()) > 60
ORDER BY duration_sec DESC;
```

建议给长事务（>60s）配置自动告警，并且给 DBA 一个一键 kill 脚本。

### 6.2 innodb_lock_wait_timeout

默认 50 秒，生产强烈建议降到 5-10 秒。理由：

1. 50 秒意味着一个死锁能让业务线程挂 50 秒
2. 大部分业务请求超时都比 50 秒短，拿着锁等也没意义
3. 快速失败让应用重试，比慢慢等死好

```ini
innodb_lock_wait_timeout = 10
```

### 6.3 Next-Key Lock 与 RR 隔离级别

MySQL 默认 REPEATABLE READ 会加 gap lock，范围删除/更新容易产生大范围锁等待。几个常见坑：

1. `DELETE FROM t WHERE create_time < '2024-01-01'` 在 `create_time` 是普通索引时，会对前后的 gap 都加锁
2. `INSERT ON DUPLICATE KEY UPDATE` 在高并发下会触发 S-lock 和 X-lock 冲突导致死锁
3. `SELECT ... FOR UPDATE` 没命中索引会退化成全表锁

解决方向：

- 删除/更新大范围数据用小批量，每批 500-1000 行，事务尽量小
- `INSERT ON DUPLICATE KEY UPDATE` 可以替换为 `INSERT IGNORE` + 单独 UPDATE
- 考虑是否能用 READ COMMITTED（降低隔离级别、减少 gap lock）

注意：RC 没有 gap lock，但对 binlog 模式有要求，必须是 `binlog_format = ROW`（8.0 默认）。

## 七、自适应 Hash 与 Change Buffer

### 7.1 Adaptive Hash Index

`innodb_adaptive_hash_index` 默认开启，对等值查询有加速。但在两种场景下要关：

1. **高并发写入**：AHI 的 index 构建/失效会抢 latch，写入 TPS 损失明显
2. **数据变化频繁**：构建的 hash 很快就无效，白浪费 CPU

观察指标：

```sql
SHOW ENGINE INNODB STATUS\G
-- 找 "Hash table size X, node heap has Y buffer(s)"
-- 找 "x.xx hash searches/s, y.yy non-hash searches/s"
```

如果 non-hash searches 反而占大头，考虑关掉：

```ini
innodb_adaptive_hash_index = OFF
```

Percona 的观点是：现代 NVMe + 大 buffer pool 场景下 AHI 收益有限，默认关比默认开更合理。我在几套写入密集型集群上关了 AHI，TPS 提升 10-15%。

### 7.2 Change Buffer

用于缓存对二级索引的修改，减少随机 IO。`innodb_change_buffer_max_size` 默认 25%（buffer pool 的），写入密集型业务可以调到 50%。

但注意：change buffer merge 是触发式的，如果 merge 不及时，二级索引查询会被拖慢。OLTP 为主的业务建议保持默认；批量导入场景可以临时调大到 50% 加速。

## 八、慢查询治理：从日志到优化的闭环

### 8.1 慢查询聚合

`long_query_time = 0.5`，配合 pt-query-digest：

```bash
pt-query-digest /var/log/mysql/slow.log \
  --since '1 day ago' \
  --limit 20 > slow-report.txt
```

重点看：

- Response time 占比最高的 Top 10
- 执行次数 Top 10
- 平均响应时间 Top 10

不要只看"最慢的 SQL"，一个每次 10 秒但一天只跑 10 次的，不如一个每次 100ms 但一天跑 100 万次的更值得优化。

### 8.2 EXPLAIN FORMAT=TREE

MySQL 8.0 的 TREE 格式比传统表格更直观：

```sql
EXPLAIN FORMAT=TREE
SELECT u.name, o.amount
FROM users u JOIN orders o ON u.id = o.user_id
WHERE u.created_at > '2024-01-01' AND o.status = 'paid';
```

输出里的 `(cost=...)` 是优化器估算的代价，`actual time=` 是真实耗时（用 `EXPLAIN ANALYZE` 才有）。对比两者能发现优化器的估算误差，很多性能问题根因就是优化器估错了行数。

### 8.3 强制索引与 optimizer hints

8.0+ 推荐用 optimizer hint 而不是 `FORCE INDEX`：

```sql
SELECT /*+ INDEX(users idx_created_at) */ ...
SELECT /*+ JOIN_ORDER(u, o) */ ...
SELECT /*+ SET_VAR(optimizer_switch='index_merge=off') */ ...
```

hint 的作用域更精细，不影响其他 SQL。

### 8.4 统计信息

`innodb_stats_persistent = ON`（默认），`innodb_stats_auto_recalc = ON`（默认）。但自动重算的触发条件是表变化 > 10%，大表很久不重算。手动：

```sql
ANALYZE TABLE orders UPDATE HISTOGRAM ON status WITH 16 BUCKETS;
```

MySQL 8.0 支持 Histogram，对低基数列（status、type 等）的 WHERE 条件选择率估算更准。上了 histogram 之后一些之前走全表的 SQL 会自动走索引。

## 九、复制与高可用

### 9.1 主从复制参数

MySQL 8.0 的复制推荐配置：

```ini
# binlog
log_bin = mysql-bin
binlog_format = ROW
binlog_row_image = MINIMAL      # 只记录必要列，binlog 体积小 60%
binlog_expire_logs_seconds = 604800  # 7 天

# GTID
gtid_mode = ON
enforce_gtid_consistency = ON

# 并行复制
slave_parallel_type = LOGICAL_CLOCK
slave_parallel_workers = 16
slave_preserve_commit_order = ON   # 保证从库 commit 顺序

# 半同步（或者直接上组复制）
rpl_semi_sync_master_enabled = 1
rpl_semi_sync_master_timeout = 1000  # 1s
```

`slave_parallel_workers` 调大能显著降低从库延迟，但超过 CPU 核数后没用。常见值 8-32。

### 9.2 半同步的坑

半同步开了之后，主库等待至少一个从库 ACK 才返回 commit。两个常见问题：

1. **网络抖动导致主库 commit 变慢**：把 `rpl_semi_sync_master_timeout` 设成 1000-3000ms，超时自动降级成异步
2. **降级后没告警，完全不知道**：监控 `Rpl_semi_sync_master_status`，一旦 = 0 立即告警

更好的方案是上 **Group Replication** 或 **InnoDB Cluster**，这是 MySQL 官方推荐的高可用方案，自带多数派提交和自动故障切换。缺点是对网络延迟敏感，跨机房部署需要谨慎。

## 十、真实故障复盘

### 10.1 Buffer Pool 太小导致的慢查询雪崩

**背景**：某个业务数据量从 50GB 增长到 300GB，buffer pool 仍然是 32GB。

**现象**：晚上 20 点大促开始后 10 分钟，数据库 CPU 飙到 100%，大量查询超时。

**排查**：

1. 命中率从 99.8% 掉到 87%
2. `iostat -x` 看磁盘 read 从 50MB/s 飙到 1.5GB/s，queue 拉到 100+
3. 慢查询日志显示所有走索引的 SELECT 都变慢

**根因**：热点数据超过了 buffer pool，每次查询都要从磁盘读，大促流量让 IO 直接打爆。

**修复**：临时加机器 + 调 buffer pool 到 128GB，彻底解决后把表按时间分片迁移到归档库。

**教训**：buffer pool 命中率要长期监控趋势，低于 99% 就要考虑扩容或分片，不要等到报警才动。

### 10.2 长事务导致从库复制延迟 3 小时

**背景**：夜里有人手动跑 `UPDATE big_table SET status=1 WHERE create_time < xxx` 一条 SQL 影响 2000 万行。

**现象**：主库 30 分钟跑完，从库复制延迟从 0 涨到 3 小时，下游分析任务全挂。

**根因**：并行复制按事务粒度并行，单个超大事务无法并行，只能一个 worker 跑。

**修复**：停掉 SQL、从备份恢复。之后制定规范：

1. 单事务影响行数 > 10000 必须拆批
2. DDL 和大批量 DML 走 pt-online-schema-change 或 gh-ost
3. 上线 `max_statement_time` 限制（8.0 支持）防止误操作

```sql
SET GLOBAL max_execution_time = 300000;  -- 5 分钟
```

### 10.3 统计信息过时导致优化器选错索引

**现象**：某个查询突然从 10ms 变成 5 秒，执行计划从走 `idx_user_id` 变成全表扫。

**排查**：`EXPLAIN` 显示优化器估计走索引要扫 50 万行，实际只有 500 行。

**根因**：表最近批量插入了大量数据但没触发自动 analyze，优化器看到的统计信息是一周前的。

**修复**：`ANALYZE TABLE` 立即恢复。之后加了定时 job，对核心表每晚 analyze 一次。

## 十一、MySQL 8.4 LTS 升级要点

MySQL 8.4（2024 年 4 月 LTS）相比 8.0 的主要变化：

1. **默认启用 caching_sha2_password**：老客户端不支持 sha2 的要升级驱动
2. **Group Replication 参数改名**：`group_replication_*` 前缀调整
3. **移除 query_cache**：早就废弃了，8.4 彻底删了（其实 8.0 就删了）
4. **Redo log 管理变化**：只能用 `innodb_redo_log_capacity`
5. **移除 mysql_native_password 的默认支持**：要手动开

升级建议从 8.0.latest 直接到 8.4，中间版本跳过。滚动升级顺序：从库 → 主从切换 → 老主库升级。

## 十二、我的调参清单

最后给一个我常用的"开箱即用"配置模板，64GB 机器、NVMe SSD、OLTP 业务：

```ini
[mysqld]
# 基础
server_id = 1
datadir = /data/mysql
socket = /tmp/mysql.sock

# 连接
max_connections = 1000
max_connect_errors = 100000
thread_cache_size = 100

# InnoDB 核心
innodb_buffer_pool_size = 48G
innodb_buffer_pool_instances = 16
innodb_redo_log_capacity = 16G
innodb_log_buffer_size = 64M
innodb_flush_log_at_trx_commit = 1
innodb_flush_method = O_DIRECT

# IO
innodb_io_capacity = 10000
innodb_io_capacity_max = 20000
innodb_read_io_threads = 8
innodb_write_io_threads = 8

# 锁
innodb_lock_wait_timeout = 10
innodb_rollback_on_timeout = ON

# 并发
innodb_thread_concurrency = 0
innodb_purge_threads = 8

# Doublewrite
innodb_doublewrite = ON
innodb_doublewrite_files = 2

# 临时表
tmp_table_size = 128M
max_heap_table_size = 128M

# binlog
log_bin = mysql-bin
binlog_format = ROW
binlog_row_image = MINIMAL
sync_binlog = 1
binlog_expire_logs_seconds = 604800
binlog_group_commit_sync_delay = 1000
binlog_group_commit_sync_no_delay_count = 20

# GTID
gtid_mode = ON
enforce_gtid_consistency = ON

# 慢查询
slow_query_log = 1
long_query_time = 0.5
log_slow_admin_statements = 1
log_queries_not_using_indexes = 1

# 复制
slave_parallel_type = LOGICAL_CLOCK
slave_parallel_workers = 16
slave_preserve_commit_order = ON

# 性能 schema
performance_schema = ON
performance_schema_instrument = 'wait/lock/metadata/sql/mdl=ON'
```

这个模板不是银弹，上线前务必 benchmark 验证。推荐用 sysbench 跑三个场景：`oltp_read_only`、`oltp_write_only`、`oltp_read_write`，记录基线数据，调参前后对比。

## 经验法则总结

- **调优是假设-验证-回滚的闭环，不是开盒即调**
- **buffer pool 命中率是最重要的单一指标，其他都是辅助**
- **双 1 配置不可动摇，想快就换硬件**
- **锁等待比 CPU 和 IO 更常见，性能问题先查锁**
- **长事务是万恶之源，严格限制**
- **统计信息要新鲜，否则优化器会坑你**
- **upgrade 前做完整回归，8.0 到 8.4 有几个破坏性变更**

调优的尽头是对业务的理解。同样一套参数，在读多写少的电商和写多读少的日志系统上效果完全不同。多看监控、多复盘故障，参数值自然就心里有数了。

参考资料：

- MySQL 8.0/8.4 官方手册的 Optimization 章节，所有参数行为以官方为准
- Percona Blog 的 InnoDB 系列文章
- Mark Callaghan 的博客，尤其是 LSM vs B-tree 对比
- Aurora for MySQL 的参数指南（对比 Aurora 和原生 MySQL 的默认值差异很有意思）
