---
title: "PostgreSQL 运维实战：配置调优、连接池、慢查询与高可用"
date: 2025-03-18T10:15:00+08:00
draft: false
tags: ["PostgreSQL", "数据库", "运维", "性能优化"]
categories: ["数据库"]
description: "覆盖 PostgreSQL 生产环境全链路运维：参数调优、PgBouncer 连接池、慢查询分析、备份恢复、主从复制、VACUUM 膨胀处理与 Prometheus 监控体系"
summary: "系统梳理 PostgreSQL 运维核心技能：从 shared_buffers、WAL 参数调优，到 PgBouncer 事务模式配置；从 pg_stat_statements 慢查询分析到 PITR 时间点恢复；以及主从流复制、膨胀表清理和 Prometheus 监控指标的完整实践。"
toc: true
math: false
diagram: false
keywords: ["PostgreSQL", "PgBouncer", "pg_stat_statements", "PITR", "流复制", "VACUUM", "postgres_exporter", "连接池"]
params:
  reading_time: true
---

我们核心业务的账户、工作流状态、AI 任务记录都压在 PG 上。从 MySQL 背景过来做 PG 运维，好多操作习惯得重新建，踩的坑也不一样。这篇是这几年积累的运维笔记。

## 生产配置调优

PostgreSQL 默认配置面向通用场景，对生产环境几乎都需要定制。配置文件通常位于 `/etc/postgresql/{version}/main/postgresql.conf`，或通过 `SHOW config_file;` 查询实际路径。

### 内存参数

**shared_buffers**

PostgreSQL 自己管理的共享内存缓冲区，类似 InnoDB buffer pool。

```
# 推荐值：物理内存的 25%
shared_buffers = 8GB   # 32GB 机器
```

不同于 MySQL，PostgreSQL 同时依赖操作系统 Page Cache，所以不建议把 shared_buffers 设太高（超过 40% 往往适得其反）。修改后需要重启数据库。

**effective_cache_size**

这个参数不影响实际内存分配，只是告诉查询优化器操作系统缓存大约有多大，帮助优化器选择更合理的执行计划。

```
# 推荐值：物理内存的 50%~75%
effective_cache_size = 24GB
```

**work_mem**

每个排序或哈希操作可用的内存，注意这是**每个操作**的上限，而非每个连接。一个复杂查询可能同时有多个排序节点，每个都能用到 work_mem。

```
# 起始值保守一些，避免内存爆炸
work_mem = 64MB
```

计算公式：`work_mem * max_connections * 并发查询中排序节点数` 是潜在峰值内存用量。在连接数较多的场景下，建议通过 `SET work_mem` 在 session 级别按需调高，而不是全局设大。

**maintenance_work_mem**

VACUUM、CREATE INDEX、ALTER TABLE 等维护操作使用的内存，可以设大一些：

```
maintenance_work_mem = 1GB
```

**wal_buffers**

WAL 日志写入内存缓冲区大小，默认 `-1` 会自动设为 shared_buffers 的 1/32（最大 64MB）。写密集场景可手动设为：

```
wal_buffers = 64MB
```

### 连接参数

```
# 根据实际业务连接数规划，不要设太大
# 配合 PgBouncer 后，数据库侧可以控制在 100~300
max_connections = 200
```

PostgreSQL 每个连接都是独立进程，连接数过多会显著增加内存消耗和上下文切换开销。生产环境**必须配合连接池**，不要直接把应用连接数堆上来。

### WAL 与检查点参数

WAL（Write-Ahead Logging）是 PostgreSQL 数据持久化和复制的核心机制，调优直接影响写入性能和恢复时间。

```
# WAL 级别：replica 支持流复制，logical 支持逻辑复制
wal_level = replica

# 检查点触发间隔（默认 5min，写密集场景可延长）
checkpoint_timeout = 15min

# 检查点时脏页写入速率限制，避免 IO 突刺
checkpoint_completion_target = 0.9

# WAL 保留量上限（防止磁盘爆满）
max_wal_size = 4GB
min_wal_size = 1GB

# 同步提交：off 可提高写入吞吐，但崩溃可能丢最近几个事务
# 对于非关键数据可以开启
synchronous_commit = on
```

**fsync 与 full_page_writes**

```
# 生产环境必须开启，关闭会有数据损坏风险
fsync = on
full_page_writes = on
```

### 查询优化器参数

```
# 随机 IO 代价，SSD 场景可调低（默认 4.0）
random_page_cost = 1.5

# 并行查询工作线程数
max_parallel_workers_per_gather = 4
max_parallel_workers = 8

# 统计信息采样精度（默认 100，复杂表可调高）
default_statistics_target = 200
```

### 日志配置

```
# 记录执行时间超过 1 秒的查询
log_min_duration_statement = 1000

# 记录等待锁超过 500ms 的语句
log_lock_waits = on
deadlock_timeout = 500ms

# 记录 autovacuum 行为（排障必备）
log_autovacuum_min_duration = 0

# 慢查询日志目录
logging_collector = on
log_directory = 'pg_log'
log_filename = 'postgresql-%Y-%m-%d_%H%M%S.log'
```

### 配置热加载

部分参数修改后无需重启，执行 `SELECT pg_reload_conf();` 即可生效。需要重启的参数可通过以下方式查询：

```sql
SELECT name, setting, unit, context
FROM pg_settings
WHERE context IN ('postmaster', 'sighup')
ORDER BY context, name;
-- context = postmaster 需要重启
-- context = sighup 热加载即可
```

---

## 连接池方案：PgBouncer 实战

### 为什么需要连接池

PostgreSQL 的连接模型是每个连接对应一个后台进程（postmaster fork），而非线程模型。连接数达到几百时，进程切换开销显著，内存消耗也线性增长（每个连接约 5~10MB）。

PgBouncer 作为连接池代理，将应用侧的大量短连接复用到少量数据库长连接，是 PostgreSQL 生产部署的标准配置。

### 工作模式选择

PgBouncer 支持三种池化模式：

| 模式 | 连接释放时机 | 适用场景 |
|------|------------|--------|
| session | 客户端断开时 | 使用了 session 级特性（临时表、预备语句等）|
| transaction | 事务提交/回滚后 | **推荐**，大多数 Web 应用 |
| statement | 每条语句后 | 极少用，不支持多语句事务 |

**事务模式（transaction pooling）** 是生产环境首选，连接复用率最高。但使用事务模式时，以下特性不可用：
- `SET` 设置的 session 参数不持久
- 预备语句（prepared statements）需要在应用侧禁用或使用 PgBouncer 的 `server_reset_query`
- `LISTEN/NOTIFY`

### pgbouncer.ini 配置示例

```ini
[databases]
; 格式：逻辑库名 = host=... port=... dbname=...
myapp = host=127.0.0.1 port=5432 dbname=myapp

; 也可以使用通配符，让应用连同名数据库
* = host=127.0.0.1 port=5432

[pgbouncer]
listen_addr = 0.0.0.0
listen_port = 6432

; 认证方式（推荐 scram-sha-256，老版本用 md5）
auth_type = scram-sha-256
auth_file = /etc/pgbouncer/userlist.txt

; 池化模式
pool_mode = transaction

; 数据库侧最大连接数（所有 pool 共享）
max_client_conn = 1000
default_pool_size = 20

; 等待队列上限
max_db_connections = 100

; 空闲连接保活/超时
server_idle_timeout = 600
client_idle_timeout = 0

; 连接释放后清理 session 状态
server_reset_query = DISCARD ALL

; 健康检查
server_check_query = SELECT 1
server_check_delay = 30

; 日志
log_connections = 0
log_disconnections = 0
log_pooler_errors = 1

; 管理接口
admin_users = pgbouncer
stats_users = monitoring
```

### userlist.txt 生成

```bash
# 从 PostgreSQL 导出用户密码哈希
psql -c "SELECT concat('\"', usename, '\" \"', passwd, '\"') FROM pg_shadow WHERE usename NOT LIKE 'pg_%';" -t
```

格式为：
```
"myapp_user" "SCRAM-SHA-256$..."
"pgbouncer" "SCRAM-SHA-256$..."
```

### 监控 PgBouncer 状态

连接到 PgBouncer 管理库（`pgbouncer` 数据库）：

```bash
psql -h 127.0.0.1 -p 6432 -U pgbouncer pgbouncer
```

常用管理命令：

```sql
-- 查看连接池状态
SHOW POOLS;

-- 查看所有数据库连接统计
SHOW DATABASES;

-- 查看当前活跃客户端连接
SHOW CLIENTS;

-- 查看当前服务端连接
SHOW SERVERS;

-- 查看统计汇总
SHOW STATS;

-- 在线重载配置
RELOAD;

-- 优雅暂停（维护时使用）
PAUSE myapp;
RESUME myapp;
```

重点关注 `SHOW POOLS` 的以下字段：
- `cl_active`：正在执行查询的客户端连接
- `cl_waiting`：等待空闲服务端连接的客户端
- `sv_active`：正在使用的服务端连接
- `sv_idle`：空闲服务端连接

`cl_waiting` 持续大于 0 说明连接池饱和，需要增大 `default_pool_size` 或优化慢查询。

---

## 慢查询分析

### 启用 pg_stat_statements

`pg_stat_statements` 是 PostgreSQL 内置的慢查询统计扩展，记录每类 SQL 的执行次数、总耗时、IO 消耗等信息。

```sql
-- 在 postgresql.conf 中加载
shared_preload_libraries = 'pg_stat_statements'

-- 然后在目标数据库创建扩展
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
```

**查询 Top 慢 SQL（按总耗时排序）**

```sql
SELECT
    round(total_exec_time::numeric, 2) AS total_ms,
    calls,
    round(mean_exec_time::numeric, 2) AS mean_ms,
    round((100 * total_exec_time / sum(total_exec_time) OVER ())::numeric, 2) AS pct,
    left(query, 120) AS query
FROM pg_stat_statements
ORDER BY total_exec_time DESC
LIMIT 20;
```

**查询平均耗时最高的 SQL**

```sql
SELECT
    calls,
    round(mean_exec_time::numeric, 2) AS mean_ms,
    round(stddev_exec_time::numeric, 2) AS stddev_ms,
    left(query, 120) AS query
FROM pg_stat_statements
WHERE calls > 100
ORDER BY mean_exec_time DESC
LIMIT 20;
```

**查询 IO 消耗最大的 SQL**

```sql
SELECT
    calls,
    shared_blks_hit,
    shared_blks_read,
    round(100.0 * shared_blks_hit / nullif(shared_blks_hit + shared_blks_read, 0), 2) AS hit_rate,
    left(query, 120) AS query
FROM pg_stat_statements
WHERE shared_blks_hit + shared_blks_read > 0
ORDER BY shared_blks_read DESC
LIMIT 20;
```

定期清空统计：

```sql
SELECT pg_stat_statements_reset();
```

### EXPLAIN ANALYZE 解读

拿到慢 SQL 后，用 `EXPLAIN ANALYZE` 获取实际执行计划：

```sql
EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)
SELECT u.id, u.name, COUNT(o.id) AS order_count
FROM users u
LEFT JOIN orders o ON o.user_id = u.id
WHERE u.created_at > '2024-01-01'
GROUP BY u.id, u.name
ORDER BY order_count DESC
LIMIT 100;
```

**关键节点解读**

```
Gather Merge  (cost=... rows=... width=...) (actual time=120.3..145.6 rows=100 loops=1)
  ->  Sort  (cost=... rows=... width=...) (actual time=115.2..118.9 rows=... loops=4)
        Sort Key: (count(o.id)) DESC
        Sort Method: quicksort  Memory: 256kB     ← 内存排序，OK
  ->  Partial HashAggregate  (cost=...) (actual time=...) (never executed)
        Batches: 1  Memory Usage: 4096kB          ← 注意 Batches > 1 表示溢写磁盘
  ->  Hash Left Join  (cost=...) (actual time=...) (never executed)
        Hash Cond: (o.user_id = u.id)
        Buffers: shared hit=23450 read=8920       ← read 高说明缓存命中差
  ->  Seq Scan on orders o  (cost=...)            ← 全表扫描，可能需要索引
        Filter: (user_id IS NOT NULL)
        Rows Removed by Filter: 5000
  ->  Bitmap Heap Scan on users u  (cost=...)     ← 使用了索引
        Recheck Cond: (created_at > '2024-01-01')
        ->  Bitmap Index Scan on idx_users_created_at
```

重点关注：
- **Seq Scan**：全表扫描，大表上出现需要检查是否缺索引，或统计信息过期
- **actual rows 与 rows 差距大**：统计信息不准，执行 `ANALYZE tablename`
- **Buffers: read 高**：数据不在缓存，考虑增大 shared_buffers 或优化查询
- **Sort Method: external merge**：排序溢写磁盘，增大 work_mem

### 索引优化实践

**查找缺失索引**

```sql
-- 查找高 seq scan 的表（可能需要索引）
SELECT schemaname, tablename, seq_scan, seq_tup_read,
       idx_scan, idx_tup_fetch,
       n_live_tup
FROM pg_stat_user_tables
WHERE seq_scan > 1000
ORDER BY seq_tup_read DESC
LIMIT 20;
```

**查找未使用的索引**

```sql
SELECT schemaname, tablename, indexname, idx_scan
FROM pg_stat_user_indexes
WHERE idx_scan = 0
  AND indexname NOT LIKE 'pg_%'
  AND indexname NOT LIKE '%_pkey'
ORDER BY pg_relation_size(indexrelid) DESC
LIMIT 20;
```

**并发创建索引（不锁表）**

```sql
-- 加 CONCURRENTLY 不阻塞写入，但耗时更长
CREATE INDEX CONCURRENTLY idx_orders_user_created
ON orders (user_id, created_at DESC)
WHERE status != 'cancelled';  -- 部分索引，减少索引大小
```

**常用索引类型选择**

| 索引类型 | 适用场景 |
|---------|--------|
| B-tree（默认）| 等值查询、范围查询、排序 |
| Hash | 仅等值查询，不支持范围 |
| GIN | 全文搜索、数组、JSONB 包含查询 |
| GiST | 地理空间、范围类型、全文搜索 |
| BRIN | 超大表的时序数据（物理顺序与值顺序相关） |

---

## 备份与恢复

### pg_dump 逻辑备份

适合中小规模数据库，支持跨版本迁移，但恢复时间与数据量成正比。

```bash
# 备份单个数据库（自定义格式，支持并行恢复）
pg_dump \
  -h localhost \
  -U postgres \
  -Fc \                    # 自定义压缩格式
  -j 4 \                   # 4 并发 worker
  -f /backup/myapp_$(date +%Y%m%d_%H%M%S).dump \
  myapp

# 备份所有数据库（含角色和全局对象）
pg_dumpall -h localhost -U postgres > /backup/globals.sql

# 恢复
pg_restore \
  -h localhost \
  -U postgres \
  -d myapp \
  -j 4 \                   # 并行恢复
  --clean \                # 恢复前先删除已存在对象
  --if-exists \
  /backup/myapp_20240318_101500.dump
```

### pg_basebackup 物理备份

物理备份速度快、恢复快，是大规模生产环境的首选。也是搭建流复制备库的标准方式。

```bash
# 创建物理基础备份
pg_basebackup \
  -h localhost \
  -U replicator \
  -D /backup/basebackup_$(date +%Y%m%d) \
  -Ft \                    # tar 格式
  -z \                     # gzip 压缩
  -Xs \                    # 包含 WAL（streaming 模式）
  -P \                     # 显示进度
  --wal-method=stream

# 恢复时解压到 PostgreSQL data 目录
tar -xzf /backup/basebackup_20240318/base.tar.gz -C /var/lib/postgresql/data/
tar -xzf /backup/basebackup_20240318/pg_wal.tar.gz -C /var/lib/postgresql/data/pg_wal/
```

### PITR 时间点恢复

PITR（Point-in-Time Recovery）利用基础备份 + WAL 归档，将数据库恢复到任意历史时间点，是应对误操作的终极手段。

**1. 配置 WAL 归档**

```
# postgresql.conf
archive_mode = on
archive_command = 'cp %p /wal-archive/%f'
# 或使用 AWS S3：
# archive_command = 'aws s3 cp %p s3://my-bucket/wal/%f'
```

**2. 执行恢复**

在 `$PGDATA` 目录下创建 `recovery.signal` 文件，并配置 `postgresql.conf`：

```
# postgresql.conf（PG 12+，之前版本用 recovery.conf）
restore_command = 'cp /wal-archive/%f %p'

# 恢复到指定时间点
recovery_target_time = '2024-03-18 09:30:00+08'

# 恢复后的行为：promote（默认）升为主库
recovery_target_action = promote
```

然后启动 PostgreSQL，它会自动进入恢复模式，应用 WAL 直到目标时间点后停止。

**3. 验证恢复结果**

```sql
-- 确认已退出恢复模式
SELECT pg_is_in_recovery();  -- 应返回 false

-- 确认数据状态
SELECT now(), count(*) FROM orders WHERE created_at > '2024-03-18 09:00:00';
```

---

## 主从流复制

### 配置主库

```sql
-- 创建专用复制用户
CREATE ROLE replicator WITH REPLICATION LOGIN PASSWORD 'strong_password';
```

```
# postgresql.conf
wal_level = replica
max_wal_senders = 10          # 最多允许 N 个 standby 连接
wal_keep_size = 1GB           # 主库保留 WAL 量，防止 standby 落后太多
hot_standby = on              # 备库允许只读查询
```

```
# pg_hba.conf 允许复制连接
host  replication  replicator  10.0.0.0/8  scram-sha-256
```

### 创建备库

```bash
# 在备库机器上执行基础备份
pg_basebackup \
  -h 10.0.1.10 \
  -U replicator \
  -D /var/lib/postgresql/data \
  -Xs \
  -R \                         # 自动生成 standby.signal 和复制配置
  -P
```

`-R` 参数会自动在 data 目录写入 `standby.signal` 文件，并将连接信息写入 `postgresql.auto.conf`：

```
primary_conninfo = 'host=10.0.1.10 port=5432 user=replicator password=...'
```

启动备库后，它会自动以流复制模式连接主库。

### 监控复制状态

**在主库查看复制状态**

```sql
SELECT
    client_addr,
    state,
    sent_lsn,
    write_lsn,
    flush_lsn,
    replay_lsn,
    (sent_lsn - replay_lsn) AS replication_lag_bytes,
    write_lag,
    flush_lag,
    replay_lag,
    sync_state
FROM pg_stat_replication;
```

关键字段：
- `replay_lag`：备库 replay 落后主库的时间
- `(sent_lsn - replay_lsn)`：落后的字节数
- `sync_state`：`async`（异步）或 `sync`（同步）

**在备库查看状态**

```sql
-- 确认备库状态
SELECT pg_is_in_recovery();         -- true 表示仍在 standby 模式
SELECT pg_last_wal_receive_lsn();   -- 已接收的 WAL 位置
SELECT pg_last_wal_replay_lsn();    -- 已应用的 WAL 位置
SELECT now() - pg_last_xact_replay_timestamp() AS replication_delay;
```

### 延迟告警配置

```yaml
# Prometheus AlertRule
- alert: PostgresReplicationLagHigh
  expr: |
    pg_replication_lag > 30
  for: 5m
  labels:
    severity: warning
  annotations:
    summary: "PostgreSQL 复制延迟超过 30 秒"
    description: "实例 {{ $labels.instance }} 复制延迟 {{ $value }}s"
```

### 手动 Failover

```bash
# 在备库执行提升操作（PG 12+）
pg_ctl promote -D /var/lib/postgresql/data

# 或通过触发文件（老版本）
touch /var/lib/postgresql/data/failover.signal
```

提升后，原备库成为新主库，需要将其他 standby 和应用连接切换到新主库。

---

## 常见故障排查

### 连接耗尽

**症状**：`FATAL: remaining connection slots are reserved for non-replication superuser connections`

**排查步骤**

```sql
-- 查看当前连接数按状态分布
SELECT state, count(*) FROM pg_stat_activity GROUP BY state ORDER BY count DESC;

-- 查看连接数按应用/用户分布
SELECT usename, application_name, client_addr, state, count(*)
FROM pg_stat_activity
WHERE pid <> pg_backend_pid()
GROUP BY 1,2,3,4
ORDER BY count DESC
LIMIT 30;

-- 查看长时间 idle 的连接（可能是连接泄漏）
SELECT pid, usename, application_name, client_addr, state,
       now() - state_change AS idle_duration,
       query
FROM pg_stat_activity
WHERE state = 'idle'
  AND now() - state_change > interval '10 minutes'
ORDER BY idle_duration DESC;
```

**处理方式**

```sql
-- 终止特定连接（非 superuser 可用）
SELECT pg_terminate_backend(pid)
FROM pg_stat_activity
WHERE state = 'idle'
  AND now() - state_change > interval '30 minutes'
  AND usename = 'myapp_user';
```

根本解决方案：调小应用连接池大小，或引入 PgBouncer。

### 死锁

PostgreSQL 会自动检测死锁（默认 500ms 后），并终止代价较小的事务，同时在日志中记录详情。

```bash
# 从日志中查找死锁事件
grep -i "deadlock detected" /var/log/postgresql/postgresql-*.log | tail -20
```

```sql
-- 查看当前等待锁的查询
SELECT
    blocked.pid AS blocked_pid,
    blocked.query AS blocked_query,
    blocking.pid AS blocking_pid,
    blocking.query AS blocking_query,
    now() - blocked.query_start AS blocked_duration
FROM pg_stat_activity blocked
JOIN pg_stat_activity blocking
    ON blocking.pid = ANY(pg_blocking_pids(blocked.pid))
ORDER BY blocked_duration DESC;
```

死锁的根本原因通常是多个事务以不同顺序锁定同一批资源。解决方法：在应用层固定加锁顺序，或使用 `SELECT ... FOR UPDATE SKIP LOCKED` 避免竞争。

### 表膨胀与 VACUUM

PostgreSQL 使用 MVCC，UPDATE/DELETE 不会立即物理删除旧版本，而是标记为死元组（dead tuples）。VACUUM 负责回收这些死元组。

**查看膨胀情况**

```sql
-- 查看死元组比例较高的表
SELECT
    schemaname,
    tablename,
    n_live_tup,
    n_dead_tup,
    round(100.0 * n_dead_tup / nullif(n_live_tup + n_dead_tup, 0), 2) AS dead_pct,
    last_autovacuum,
    last_autoanalyze
FROM pg_stat_user_tables
WHERE n_dead_tup > 10000
ORDER BY dead_pct DESC
LIMIT 20;
```

**手动触发 VACUUM**

```sql
-- 普通 VACUUM（回收死元组，不缩小文件）
VACUUM myapp.orders;

-- VACUUM ANALYZE（同时更新统计信息）
VACUUM ANALYZE myapp.orders;

-- VACUUM FULL（压缩文件，需要锁表，谨慎使用）
VACUUM FULL myapp.orders;  -- 生产环境用 pg_repack 替代
```

**调优 autovacuum**

```
# postgresql.conf
autovacuum_vacuum_scale_factor = 0.01    # 默认 0.2，1% 死元组就触发（更积极）
autovacuum_analyze_scale_factor = 0.005  # 0.5% 变更就更新统计信息
autovacuum_vacuum_cost_delay = 2ms       # 默认 20ms，降低延迟提高效率
autovacuum_max_workers = 6               # 默认 3，增加并发
```

对于超大表，可以在表级别单独配置：

```sql
ALTER TABLE large_table SET (
    autovacuum_vacuum_scale_factor = 0.005,
    autovacuum_vacuum_cost_delay = 2
);
```

**使用 pg_repack 在线重建**

`VACUUM FULL` 需要锁表，不适合生产环境。`pg_repack` 可以在不锁表的情况下重建表并回收空间：

```bash
# 安装
apt install postgresql-14-repack

# 重建单表（不锁表）
pg_repack -h localhost -U postgres -d myapp -t large_table

# 重建所有表和索引
pg_repack -h localhost -U postgres -d myapp
```

---

## Prometheus 监控

### 部署 postgres_exporter

```yaml
# docker-compose.yml 示例
services:
  postgres_exporter:
    image: prometheuscommunity/postgres-exporter:v0.15.0
    environment:
      DATA_SOURCE_NAME: "postgresql://monitoring:password@postgres:5432/postgres?sslmode=disable"
    ports:
      - "9187:9187"
    command:
      - "--extend.query-path=/etc/postgres_exporter/queries.yaml"
```

### 关键指标说明

| 指标 | 说明 | 告警阈值建议 |
|------|------|------------|
| `pg_up` | 实例是否可达 | == 0 立即告警 |
| `pg_stat_activity_count{state="active"}` | 活跃查询数 | > max_connections * 0.8 |
| `pg_stat_activity_count{state="idle in transaction"}` | 事务中空闲连接 | > 10 持续 5min |
| `pg_replication_lag` | 主从复制延迟（秒）| > 30s |
| `pg_stat_bgwriter_checkpoints_timed_total` | 定时触发检查点次数 | -- |
| `pg_stat_bgwriter_checkpoints_req_total` | 强制触发检查点次数 | 频繁说明 WAL 量大 |
| `pg_stat_database_blks_hit` | 缓存命中数 | 缓存命中率 < 95% 告警 |
| `pg_stat_database_blks_read` | 磁盘读取数 | 结合命中率监控 |
| `pg_stat_database_deadlocks` | 死锁计数 | 任何增长都值得关注 |
| `pg_stat_user_tables_n_dead_tup` | 死元组数 | 结合 pg_class.reltuples 计算比例 |
| `pg_database_size_bytes` | 数据库大小 | 磁盘使用率 > 70% 告警 |

**缓存命中率计算**

```yaml
# Prometheus recording rule
- record: pg:database:cache_hit_ratio
  expr: |
    rate(pg_stat_database_blks_hit[5m])
    /
    (rate(pg_stat_database_blks_hit[5m]) + rate(pg_stat_database_blks_read[5m]))
```

### 自定义查询扩展

`postgres_exporter` 支持通过 `queries.yaml` 扩展自定义指标：

```yaml
# /etc/postgres_exporter/queries.yaml
pg_long_running_queries:
  query: |
    SELECT
      count(*) AS count,
      max(extract(epoch FROM (now() - query_start))) AS max_duration_seconds
    FROM pg_stat_activity
    WHERE state = 'active'
      AND query_start < now() - interval '1 minute'
      AND query NOT LIKE '%pg_stat_activity%'
  metrics:
    - count:
        usage: "GAUGE"
        description: "长时间运行的查询数量"
    - max_duration_seconds:
        usage: "GAUGE"
        description: "最长运行查询的持续时间（秒）"
```

---

## 与 MySQL 的关键运维差异

对于有 MySQL 经验的运维工程师，以下几点差异需要特别注意：

### 1. 连接模型

- MySQL：线程池，每个连接一个线程，连接开销小
- PostgreSQL：进程模型，每个连接一个进程，**必须使用连接池（PgBouncer）**

### 2. 数据文件组织

- MySQL（InnoDB）：数据按表存储在 `.ibd` 文件，或共享表空间
- PostgreSQL：每个表对应多个物理文件（8KB page），存放在 `$PGDATA/base/{oid}/` 目录下，**不能直接移动文件**

### 3. 事务与 MVCC

- MySQL：回滚数据存在 undo log，purge 线程清理
- PostgreSQL：旧版本数据与新数据存在同一文件中（dead tuples），依赖 **VACUUM 机制**清理。长事务会阻止 VACUUM 回收，导致表膨胀

### 4. 自增 ID

- MySQL：`AUTO_INCREMENT`，宕机重启后不会重置（InnoDB）
- PostgreSQL：`SERIAL` 或 `GENERATED ALWAYS AS IDENTITY`，底层是序列（Sequence），序列值不参与事务回滚——**事务回滚后序列值不会退回**，会出现 ID 空洞，这是正常现象

### 5. 备份工具

- MySQL：`mysqldump`（逻辑）、`xtrabackup`（物理）
- PostgreSQL：`pg_dump`（逻辑）、`pg_basebackup`（物理）。`pg_dump` 备份的是一致性快照，**不需要停库**

### 6. 字符串大小写

- MySQL：默认大小写不敏感（utf8mb4_general_ci）
- PostgreSQL：默认大小写敏感，`'Apple' != 'apple'`。需要不敏感查询时用 `ILIKE` 或 `citext` 扩展

### 7. 分区与分库

- MySQL：原生支持分区表，分库分表依赖中间件（ShardingSphere 等）
- PostgreSQL：原生支持声明式分区（PG 10+），分布式扩展推荐 **Citus**（已被微软收购）

### 8. Explain 格式

MySQL 的 `EXPLAIN` 是一行一行的简表；PostgreSQL 的 `EXPLAIN ANALYZE` 输出树状执行计划，信息更丰富，但需要时间学习解读。推荐使用 [explain.dalibo.com](https://explain.dalibo.com) 可视化分析。

---

## 运维速查

```bash
# 查看 PostgreSQL 版本和运行状态
psql -c "SELECT version();"
pg_lsclusters  # Debian/Ubuntu 系统

# 查看所有数据库大小
psql -c "\l+"

# 查看表大小（含索引）
psql -d myapp -c "\dt+"

# 查看当前锁等待链
psql -d myapp -c "SELECT pid, wait_event_type, wait_event, state, left(query,80) FROM pg_stat_activity WHERE wait_event IS NOT NULL;"

# 立即终止某个 pid
psql -c "SELECT pg_terminate_backend(12345);"

# 查看慢查询日志（实时）
tail -f /var/log/postgresql/postgresql-*.log | grep -E "duration:|ERROR:|FATAL:"

# 重载配置
psql -c "SELECT pg_reload_conf();"

# 查看参数值
psql -c "SHOW shared_buffers;"
psql -c "SHOW ALL;" | grep work_mem
```
