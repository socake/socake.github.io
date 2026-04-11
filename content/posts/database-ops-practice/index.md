---
title: "数据库运维实践：MySQL 高可用与 PostgreSQL 调优经验"
date: 2026-04-11T10:00:00+08:00
draft: false
tags: ["MySQL", "PostgreSQL", "数据库", "运维", "高可用"]
categories: ["数据库"]
description: "总结 MySQL 主从延迟处理、慢查询分析、PostgreSQL 连接池规划、索引管理与 RDS 运维注意事项。"
summary: "数据库运维不复杂，但细节多、出问题代价大。本文整理了 MySQL 主从复制、慢查询分析、PostgreSQL 连接池这几个高频话题的实战经验，以及一些日常运维 SQL 备忘。"
toc: true
math: false
diagram: false
keywords: ["MySQL 复制延迟", "慢查询分析", "PgBouncer", "PostgreSQL 连接池", "RDS 运维", "索引优化"]
params:
  reading_time: true
---

数据库是最不允许出问题的那层基础设施。运维经验积累的过程基本就是「踩坑 → 复盘 → 建立 SOP」的循环。本文整理了这几年在 MySQL 和 PostgreSQL 运维上积累的实战经验，重点在实用性，不讲基础原理。

## MySQL 主从复制延迟

复制延迟（`Seconds_Behind_Master`）是 MySQL 主从架构最常见的问题，严重时会导致读从库的业务读到旧数据，更严重时如果主库宕机、从库延迟过大，切换主库会有数据丢失风险。

### 监控延迟

```sql
-- 在从库执行，查看详细复制状态
SHOW REPLICA STATUS\G

-- 关键字段：
-- Seconds_Behind_Master: 从库落后主库的秒数
-- Relay_Log_Space: relay log 大小，快速增长说明 SQL thread 处理慢
-- Exec_Master_Log_Pos vs Read_Master_Log_Pos: 两者差距大说明 SQL thread 跟不上 IO thread
```

`Seconds_Behind_Master` 有一个陷阱：它是用「当前正在执行的 binlog 事件的时间戳」和「当前时间」计算的。如果从库有一个大事务执行了很久，这个值会持续增大，但事务提交后会瞬间跳回 0。不能只看快照值，要看趋势。

### 延迟原因分类

**1. 大事务**

主库的 DDL（`ALTER TABLE`）或大批量 DML 会产生一个巨大的 binlog event，从库必须串行执行完这个事务才能继续。

```sql
-- 找出正在执行的大事务（从库执行）
SELECT * FROM information_schema.INNODB_TRX 
ORDER BY trx_started ASC LIMIT 10;

-- 查看 relay log 当前执行到哪
SHOW PROCESSLIST;
```

处理：大表 DDL 一定用 `pt-online-schema-change` 或 `gh-ost`，分批次执行，避免长时间锁表和大 binlog。

**2. 从库单线程回放**

MySQL 5.7 之前从库 SQL thread 是单线程的，主库并发写入高时，从库跟不上。MySQL 5.7+ 支持并行复制：

```ini
# my.cnf 从库配置
[mysqld]
slave_parallel_workers = 8        # 并行线程数，建议 CPU 核数的 2-4 倍
slave_parallel_type = LOGICAL_CLOCK  # 比 DATABASE 模式并发度更高
slave_preserve_commit_order = ON  # 保证从库事务提交顺序与主库一致（数据一致性）
```

**3. 从库 IO 瓶颈**

从库写 relay log 和读 relay log 都在磁盘，IO 慢会是瓶颈。检查：

```bash
# 查看磁盘 IO 使用率
iostat -x 1 5

# 查看 MySQL 的 IO wait
SELECT * FROM performance_schema.file_summary_by_event_name 
WHERE event_name LIKE 'wait/io/file/innodb%'
ORDER BY total_latency DESC LIMIT 10;
```

### 复制监控告警

```yaml
# Prometheus 告警规则
- alert: MySQLReplicationLag
  expr: mysql_slave_status_seconds_behind_master > 30
  for: 2m
  labels:
    severity: warning
  annotations:
    summary: "MySQL 从库复制延迟超过 30 秒"
    description: "实例 {{ $labels.instance }} 延迟 {{ $value }} 秒"

- alert: MySQLReplicationStopped
  expr: mysql_slave_status_slave_sql_running == 0
    or mysql_slave_status_slave_io_running == 0
  for: 1m
  labels:
    severity: critical
```

## 慢查询分析

### 开启慢查询日志

```ini
# my.cnf
[mysqld]
slow_query_log = ON
slow_query_log_file = /var/log/mysql/slow.log
long_query_time = 1        # 超过 1 秒记录（生产建议 0.5-1）
log_queries_not_using_indexes = ON
log_throttle_queries_not_using_indexes = 10  # 每分钟最多记录 10 条未用索引的查询，避免日志爆炸
min_examined_row_limit = 100  # 扫描行数小于 100 的不记录
```

### pt-query-digest 分析

`pt-query-digest` 是分析慢查询日志的最佳工具，按查询模式聚合，找出最耗时的 SQL：

```bash
# 分析慢查询日志，按总耗时倒序
pt-query-digest /var/log/mysql/slow.log \
  --order-by Query_time:sum \
  --limit 20 \
  > slow_report.txt

# 只看最近 1 小时的
pt-query-digest /var/log/mysql/slow.log \
  --since "1h" \
  --limit 10

# 输出关键字段含义：
# Response time: 总耗时（占比）
# Calls: 执行次数
# R/Call: 平均每次耗时
# Rows sent/examined: 发送行数/扫描行数（比值低说明全表扫描）
```

### EXPLAIN 解读关键点

```sql
EXPLAIN SELECT o.id, u.name 
FROM orders o 
JOIN users u ON o.user_id = u.id 
WHERE o.status = 'pending' AND o.created_at > '2026-01-01'
ORDER BY o.created_at DESC 
LIMIT 100\G
```

看 EXPLAIN 结果时，重点关注：

| 字段 | 好的值 | 需要优化 |
|------|--------|---------|
| type | ref, range, const | ALL（全表扫）, index（全索引扫）|
| key | 有值（用了索引） | NULL（没用索引）|
| rows | 尽量小 | 超过表总行数的 10% 要注意 |
| Extra | Using index（覆盖索引）| Using filesort（内存/磁盘排序）, Using temporary |

`Using filesort` 不一定慢（数据量小时可以），但配合 `rows` 很大时就是问题。

**覆盖索引**是常见优化手段：

```sql
-- 原始查询需要回表（先查索引，再读数据行）
SELECT id, name, email FROM users WHERE status = 'active';

-- 创建覆盖索引，查询只需读索引页
ALTER TABLE users ADD INDEX idx_status_covering (status, id, name, email);
-- EXPLAIN 的 Extra 列会显示 Using index
```

## PostgreSQL 连接池：PgBouncer

PostgreSQL 的连接模型与 MySQL 不同：每个连接对应一个后端进程（fork），连接数高时内存消耗大，且连接建立本身（TCP + auth + catalog lookup）开销不小。Web 应用动辄几百个并发连接，不用连接池会把 Postgres 压垮。

### PgBouncer 配置

```ini
# pgbouncer.ini

[databases]
# 格式: 逻辑库名 = host=... dbname=...
mydb = host=postgres-host port=5432 dbname=mydb
mydb_readonly = host=postgres-replica port=5432 dbname=mydb

[pgbouncer]
listen_addr = 0.0.0.0
listen_port = 5432
auth_type = scram-sha-256
auth_file = /etc/pgbouncer/userlist.txt

# 连接池模式（核心配置）
# session: 客户端连接期间独占一个服务端连接（兼容性最好，省连接效果差）
# transaction: 事务结束后释放服务端连接（推荐，对应用透明）
# statement: 每条 SQL 后释放（不支持事务，少用）
pool_mode = transaction

# 每个 database/user 组合的最大服务端连接数
default_pool_size = 20

# 总服务端连接上限
max_client_conn = 1000

# 队列中等待连接的最大时间（超时返回错误，避免雪崩）
query_wait_timeout = 30

# 空闲连接保留时间
server_idle_timeout = 600

# 统计信息更新间隔
stats_period = 60

# 日志
log_connections = 0  # 生产关掉，否则日志量巨大
log_disconnections = 0
```

### max_connections 规划

PostgreSQL 的 `max_connections` 建议值是 `(CPU核数 × 4)` 到 `(CPU核数 × 10)`。太高会导致上下文切换开销大，反而降低吞吐。

```sql
-- 查看当前连接使用情况
SELECT 
  state,
  count(*) as count,
  max(now() - state_change) as max_duration
FROM pg_stat_activity 
WHERE datname = 'mydb'
GROUP BY state;

-- 找出长时间 idle 的连接（可能是连接池泄漏）
SELECT pid, usename, application_name, state, 
       now() - state_change as idle_duration
FROM pg_stat_activity 
WHERE state = 'idle' 
  AND now() - state_change > interval '10 minutes'
ORDER BY idle_duration DESC;

-- 强制关闭问题连接
SELECT pg_terminate_backend(pid) 
FROM pg_stat_activity 
WHERE pid <> pg_backend_pid()
  AND state = 'idle'
  AND now() - state_change > interval '30 minutes';
```

**PgBouncer 使用 `transaction` 模式的限制：**

不支持以下特性，应用要注意：
- SET 命令（会话级参数设置）
- LISTEN/NOTIFY
- `pg_advisory_lock`
- 预处理语句（`PREPARE`/`EXECUTE`）— 在 pgbouncer.ini 里可以开启 `server_reset_query` 解决部分场景

## 索引管理

### 找出可以删除的冗余索引

```sql
-- PostgreSQL：找出从未被使用的索引（重启后才能准确，RDS 实例重启较少，数据比较可信）
SELECT 
  schemaname,
  tablename,
  indexname,
  pg_size_pretty(pg_relation_size(indexrelid)) as index_size,
  idx_scan as times_used
FROM pg_stat_user_indexes
WHERE idx_scan = 0
  AND indexname NOT LIKE '%pkey%'  -- 主键不算
ORDER BY pg_relation_size(indexrelid) DESC;

-- MySQL：找出未使用的索引
SELECT 
  object_schema,
  object_name,
  index_name,
  count_read,
  count_write
FROM performance_schema.table_io_waits_summary_by_index_usage
WHERE object_schema NOT IN ('mysql', 'performance_schema', 'information_schema')
  AND index_name IS NOT NULL
  AND count_read = 0
ORDER BY count_write DESC;
```

**删索引前的注意事项：**

1. `idx_scan = 0` 的索引不一定能删，要确认监控窗口足够长（至少覆盖一个完整业务周期，包括月末跑批等低频场景）
2. 外键约束会自动创建索引，要先检查是否被外键使用
3. 删除前在 staging 环境验证，观察慢查询日志

### 统计信息维护

```sql
-- PostgreSQL：查看统计信息是否过期
SELECT 
  relname,
  n_live_tup,
  n_dead_tup,
  last_vacuum,
  last_autovacuum,
  last_analyze,
  last_autoanalyze
FROM pg_stat_user_tables
WHERE n_dead_tup > 10000
ORDER BY n_dead_tup DESC;

-- 手动触发 vacuum + analyze（不锁表）
VACUUM ANALYZE orders;

-- 查看 autovacuum 是否在正常工作
SELECT pid, query, state, now() - state_change as duration
FROM pg_stat_activity
WHERE query LIKE '%autovacuum%';
```

表膨胀（dead tuple 积累）会导致查询变慢、索引效率下降。如果 autovacuum 跟不上（大表高频写入场景），要调整 autovacuum 参数或手动 vacuum：

```sql
-- 针对特定高频写入表调整 autovacuum 频率
ALTER TABLE orders SET (
  autovacuum_vacuum_scale_factor = 0.01,  -- 默认 0.2，即 1% 变更就触发
  autovacuum_analyze_scale_factor = 0.005
);
```

## 备份策略与恢复演练

备份的核心价值在于「能恢复」，而不是「有备份」。很多团队有备份，但从来没做过恢复演练，真出事才发现备份文件损坏或恢复流程有问题。

**标准备份策略：**

- 全量备份：每天一次（AWS RDS 自动执行），保留 7-30 天
- binlog/WAL 备份：持续上传到 S3，支持 point-in-time recovery（PITR）

**恢复演练 SOP：**

```bash
# MySQL（基于 RDS 快照的 PITR 演练）
# 1. 在 AWS 控制台或 CLI 将快照恢复到测试实例
aws rds restore-db-instance-to-point-in-time \
  --source-db-instance-identifier prod-mysql \
  --target-db-instance-identifier recovery-test \
  --restore-time "2026-04-01T10:00:00Z"

# 2. 连接测试实例，验证数据完整性
mysql -h recovery-test.xxx.rds.amazonaws.com -u admin -p << 'EOF'
SELECT COUNT(*) FROM orders WHERE created_at < '2026-04-01 10:00:00';
SELECT MAX(created_at) FROM orders;
EOF

# 3. 记录恢复时间（RTO）
# 一般 RDS PITR 恢复需要 10-30 分钟，取决于数据库大小
```

建议每季度做一次完整的恢复演练，并记录 RTO（恢复时间目标）和 RPO（恢复点目标）的实际值。

## RDS 运维注意事项

### AWS RDS vs 自建 MySQL/PostgreSQL

| 方面 | RDS | 自建 |
|------|-----|------|
| 主从切换 | 自动（Multi-AZ），约 60-120 秒 | 需要手动或脚本，更快但需要维护 |
| 参数修改 | 通过 Parameter Group，部分需重启 | 直接改 my.cnf/postgresql.conf |
| 超级权限 | 受限，无法 `SUPER`，改用 RDS 特有存储过程 | 完全控制 |
| 操作系统访问 | 无 | 有 |
| 维护窗口 | 要规划，避免业务高峰 | 自己控制 |

**RDS 常踩的坑：**

1. **参数组修改生效时机**：`dynamic` 参数立即生效，`static` 参数需要重启实例。在变更参数组前确认参数类型，避免计划外重启。

2. **存储自动扩展**：RDS 支持存储自动扩展，但扩展后无法缩小。建议开启自动扩展 + 设置上限，同时监控存储使用率告警（80%时告警，90%时紧急处理）。

3. **Enhanced Monitoring vs CloudWatch**：Enhanced Monitoring 是 OS 级别的监控（1 秒粒度），CloudWatch 是数据库层面（1 分钟粒度）。排查 IO/CPU 问题时要用 Enhanced Monitoring。

## 常用运维 SQL 备忘

```sql
-- ====== MySQL ======

-- 查看表大小（按数据+索引排序）
SELECT 
  table_schema,
  table_name,
  ROUND((data_length + index_length) / 1024 / 1024, 2) AS size_mb,
  table_rows
FROM information_schema.TABLES
WHERE table_schema NOT IN ('mysql', 'information_schema', 'performance_schema')
ORDER BY (data_length + index_length) DESC
LIMIT 20;

-- 查看当前正在执行的查询（排除 Sleep）
SELECT id, user, host, db, command, time, state, LEFT(info, 100) as query
FROM information_schema.PROCESSLIST
WHERE command != 'Sleep'
ORDER BY time DESC;

-- 查看 InnoDB 状态（锁等待分析）
SHOW ENGINE INNODB STATUS\G

-- 查看锁等待关系
SELECT 
  r.trx_id waiting_trx_id,
  r.trx_mysql_thread_id waiting_thread,
  r.trx_query waiting_query,
  b.trx_id blocking_trx_id,
  b.trx_mysql_thread_id blocking_thread,
  b.trx_query blocking_query
FROM information_schema.innodb_lock_waits w
JOIN information_schema.innodb_trx b ON b.trx_id = w.blocking_trx_id
JOIN information_schema.innodb_trx r ON r.trx_id = w.requesting_trx_id;

-- ====== PostgreSQL ======

-- 查看表大小（含 TOAST）
SELECT 
  relname as table_name,
  pg_size_pretty(pg_total_relation_size(relid)) as total_size,
  pg_size_pretty(pg_relation_size(relid)) as table_size,
  pg_size_pretty(pg_total_relation_size(relid) - pg_relation_size(relid)) as index_size
FROM pg_stat_user_tables
ORDER BY pg_total_relation_size(relid) DESC
LIMIT 20;

-- 查看当前活跃查询及等待事件
SELECT 
  pid,
  usename,
  application_name,
  state,
  wait_event_type,
  wait_event,
  now() - query_start as duration,
  LEFT(query, 100) as query
FROM pg_stat_activity
WHERE state != 'idle'
ORDER BY duration DESC NULLS LAST;

-- 查看锁冲突
SELECT 
  blocked.pid AS blocked_pid,
  blocked.query AS blocked_query,
  blocking.pid AS blocking_pid,
  blocking.query AS blocking_query
FROM pg_stat_activity blocked
JOIN pg_stat_activity blocking 
  ON blocking.pid = ANY(pg_blocking_pids(blocked.pid))
WHERE blocked.cardinality(pg_blocking_pids(blocked.pid)) > 0;

-- 查看索引使用率（命中率低于 99% 考虑优化）
SELECT 
  sum(idx_blks_hit) / nullif(sum(idx_blks_hit + idx_blks_read), 0) as index_hit_rate,
  sum(heap_blks_hit) / nullif(sum(heap_blks_hit + heap_blks_read), 0) as table_hit_rate
FROM pg_statio_user_tables;
```

## 小结

MySQL 和 PostgreSQL 的日常运维有很多共通之处：监控延迟/慢查询、定期清理无用索引、保证备份可恢复。区别主要在连接模型（PostgreSQL 必须用连接池）和统计信息维护（PostgreSQL 的 autovacuum 需要更多关注）。RDS 降低了运维门槛，但不意味着可以不关注数据库内部状态，定期查 slow log 和监控连接数是最基本的习惯。
