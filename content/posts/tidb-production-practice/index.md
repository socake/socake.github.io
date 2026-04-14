---
title: "TiDB 生产环境实战：从 Placement Rules 到 TiKV 调优的全链路经验"
date: 2024-10-05T10:00:00+08:00
draft: false
tags: ["TiDB", "分布式数据库", "数据库运维", "TiKV", "HTAP"]
categories: ["数据库"]
description: "一份基于 TiDB 7.5/8.5 LTS 的生产落地笔记，覆盖集群拓扑规划、Placement Rules 多机房部署、TiKV RocksDB 与线程池调优、PD 调度参数、热点治理、DM 同步以及真实踩坑案例。面向已经在跑 TiDB、但还没能把资源压榨到极致的运维团队。"
summary: "把 TiDB 当成"分布式 MySQL"跑起来并不难，真正难的是让 TiKV 在高并发写入下不抖动、让 PD 调度不误伤业务、让跨机房副本在 RPO=0 的前提下活下去。本文把过去两年我在几套 TiDB 集群上踩过的坑、调过的参数和定过的 SOP 都摊开来讲，不是教程，而是一份能直接照抄的作战手册。"
toc: true
math: false
diagram: false
keywords: ["TiDB", "TiKV", "PD", "Placement Rules", "RocksDB", "Region 调度", "TiFlash", "HTAP"]
params:
  reading_time: true
---

## 为什么要写这篇

网上 TiDB 的入门文章已经多到泛滥，但真正把一套 TiDB 集群在生产环境跑稳、跑快、跑到能扛住双十一峰值的资料，却非常稀缺。过去两年我在三个不同业务线上维护过 TiDB 集群，最小的 6 节点、最大的 42 节点，经历过 TiKV OOM 导致 Region 雪崩、PD leader 切换引发业务抖动、Placement Rule 配错跨机房流量暴涨等等故障，也沉淀出一些自己的判断。

这篇笔记的目标读者是：已经在跑 TiDB，对基本概念（TiDB/TiKV/PD/TiFlash）都熟悉，但还没把调优和故障治理做透的团队。如果你还在纠结"要不要上 TiDB"，建议先读官方的 adoption guide，再回来看本文。

本文围绕的版本是 TiDB 7.5 LTS 和 TiDB 8.5 LTS，两个 LTS 版本之间有若干调度器和内存引擎上的变化，我会明确标出。

## 一、集群拓扑：先把机器分对，再谈调优

一个非常常见的误区是"TiDB 反正是分布式数据库，随便撒几台机器就能跑"。实际上，拓扑一旦定错，后面无论怎么调参都是在补窟窿。

### 1.1 角色分离是底线

TiDB 集群有四类核心角色：

| 角色     | 职责                          | CPU/内存偏好                 | 磁盘需求              |
|----------|-------------------------------|------------------------------|-----------------------|
| TiDB     | SQL 层，无状态                | 计算密集，16C/32G 起         | 不需要本地盘          |
| PD       | 元数据、调度                  | 内存中等，8C/16G             | SSD，几十 GB          |
| TiKV     | 行存储引擎（RocksDB+Raft）    | CPU/内存/IO 均敏感           | NVMe SSD，4TB 以内    |
| TiFlash  | 列存储副本，HTAP 分析         | 内存敏感                     | NVMe SSD              |

官方明确要求生产环境每个角色至少配 8 核 CPU，TiKV 硬盘在 PCIe SSD 上控制在 4TB 以内、普通 SSD 上控制在 1.5TB 以内，超过这个值 compaction 放大会把 IO 打爆。我踩过这个坑：某集群为了省机器，把 TiKV 单节点塞到 6TB NVMe，峰值写入时 P99 延迟从 20ms 飙到 500ms，最后还是拆成两个节点才稳住。

**绝对不要把 PD 和 TiKV 混部**。PD 对磁盘 fsync 延迟非常敏感，TiKV 的 WAL 一旦抢 IO，PD leader 选举就会超时触发切换，业务直接报错 `PD server timeout`。

### 1.2 三机房五副本还是同城三机房三副本

这是规划 TiDB 最重要的决策点。我的判断是：

- **同城三 AZ，业务能接受 RTO 分钟级** → 三副本即可，每个 AZ 放一份，成本低、写入延迟低
- **跨城两地三中心，RPO=0 强要求** → 五副本，主城市三份、异地两份，写入延迟会增加一倍左右
- **单机房** → 我强烈建议不要上 TiDB，用 MySQL MGR 或者云数据库更合适，分布式只会带来额外复杂度

三副本同城方案下的机房拓扑示意：

```
                    +------------------+
                    |   应用层/SLB     |
                    +---------+--------+
                              |
         +--------------------+--------------------+
         |                    |                    |
    +----v----+          +----v----+          +----v----+
    |  AZ-A   |          |  AZ-B   |          |  AZ-C   |
    |         |          |         |          |         |
    | TiDB*2  |          | TiDB*2  |          | TiDB*2  |
    | PD      |          | PD      |          | PD      |
    | TiKV*4  |          | TiKV*4  |          | TiKV*4  |
    | TiFlash |          | TiFlash |          | TiFlash |
    +---------+          +---------+          +---------+
         |                    |                    |
         +--------+-----------+----------+---------+
                  |                      |
              专线 < 2ms RTT         专线 < 2ms RTT
```

TiKV 通过 label 机制感知拓扑，在 `tikv.toml` 中：

```toml
[server]
labels = { zone = "az-a", host = "tikv-01" }
```

PD 侧配置 location-labels 让调度器知道优先在不同 zone 打散副本：

```toml
[replication]
location-labels = ["zone", "host"]
max-replicas = 3
isolation-level = "zone"
```

`isolation-level = "zone"` 是关键，它强制 PD 在无法满足 zone 级隔离时拒绝调度，而不是退化到同 zone 多副本。我在一次扩容后忘记给新节点打 label，导致某个 Region 的三副本都落在了 AZ-A，如果当时 AZ-A 断电就是数据不可用事故。

## 二、Placement Rules in SQL：精细化数据放置

Placement Rules 是 TiDB 4.0 引入、5.3 GA 的能力，允许你在 SQL 层面把某个库/表/分区的副本固定到特定的机房或节点。听起来很酷，但真正用好它需要想清楚几个问题。

### 2.1 什么场景下才需要

不是所有业务都需要 Placement Rules。过度使用会带来运维复杂度上升、PD 调度压力变大。官方建议单个集群的 placement policy 不要超过 10 个、绑定策略的表+分区总数不要超过 10000 个。我的经验是超过 5 个 policy 就该停下来想想是不是设计过度。

真正值得用的场景：

1. **合规要求**：比如欧盟 GDPR 要求用户数据必须存在欧洲机房
2. **冷热分离**：历史分区放到廉价机型，热分区放高配机型
3. **多租户隔离**：不同租户的数据物理隔离，避免噪声邻居
4. **跨 region 就近读**：通过 Follower Read 让异地业务读本地副本

### 2.2 冷热分区的完整示例

假设我们有一张订单表按月分区，想把 2024 年之前的分区迁到冷存储节点。先定义两个 policy：

```sql
-- 热数据策略：三副本跨 AZ，走 SSD 节点
CREATE PLACEMENT POLICY hot_policy
  PRIMARY_REGION="az-a"
  REGIONS="az-a,az-b,az-c"
  CONSTRAINTS="[+disk=ssd]"
  FOLLOWERS=2;

-- 冷数据策略：两副本，放在 HDD 节点
CREATE PLACEMENT POLICY cold_policy
  CONSTRAINTS="[+disk=hdd]"
  FOLLOWERS=1;
```

然后给分区绑定：

```sql
ALTER TABLE orders PARTITION p202410 PLACEMENT POLICY=hot_policy;
ALTER TABLE orders PARTITION p202301 PLACEMENT POLICY=cold_policy;
```

TiKV 节点上需要同步打好 label：

```toml
[server]
labels = { zone = "az-a", disk = "ssd", host = "tikv-hot-01" }
```

绑定后 PD 会按照 policy 重新调度，你可以通过下面这条 SQL 观察进度：

```sql
SELECT * FROM information_schema.placement_policies;
SELECT TABLE_NAME, PARTITION_NAME, TIDB_PLACEMENT_POLICY_NAME
FROM information_schema.partitions
WHERE TABLE_SCHEMA = 'orders_db';
```

### 2.3 一个真实踩坑

我们有个业务场景：华东集群要给华南的只读业务提供就近访问，用了 Follower Read + Placement Rule 把一份 follower 副本固定在华南机房。看起来很美，上线两周后发现华南业务的读延迟不降反升。

根因是：Follower Read 默认策略是 `leader`，需要显式设置成 `closest-replicas` 或 `closest-adaptive` 才会走就近副本。而且 TiDB 会话级别的变量 `tidb_replica_read` 必须在连接池初始化的时候就设好，很多 JDBC 连接池会缓存 session，导致部分连接拿不到这个配置。

修复方式：

```sql
-- 全局默认
SET GLOBAL tidb_replica_read = 'closest-adaptive';
```

并且在连接 URL 里带上 `sessionVariables=tidb_replica_read='closest-adaptive'` 确保新建连接生效。

## 三、TiKV 调优：内存与线程池

TiKV 是整个集群的瓶颈点，90% 的性能问题都在 TiKV 层。调优的核心是三个池子：block cache、raftstore 线程池、写入线程池。

### 3.1 Block Cache：别迷信默认值

官方默认 `storage.block-cache.capacity` 占系统内存的 45%，在混合读写场景下够用。但如果你的业务是：

- **重读 OLTP**：调到 55%，让更多热数据驻留内存
- **重写 + 点查少**：保持 40% 甚至降到 35%，给 memtable 和 compaction 留空间
- **TiKV 和其他服务混部**：必须显式降到 30%，否则 OOM Killer 会直接送你上天

```toml
[storage.block-cache]
capacity = "64GB"   # 128GB 机器，显式配置而非百分比
```

显式配置绝对容量比百分比更可控，尤其是当 TiKV 节点的可用内存受 cgroup 限制时。我遇到过在 K8s 里跑 TiKV，limit 是 64G，但容器内 `/proc/meminfo` 看到的是宿主机 256G，TiKV 按默认 45% 算成 115G，直接被 OOM Killer 爆掉。

Block Cache 各 CF 的默认分配：

| CF              | 默认占比 | 说明                      |
|-----------------|----------|---------------------------|
| default CF      | 25%      | 实际数据                  |
| write CF        | 15%      | MVCC 版本信息             |
| lock CF         | 2%       | 事务锁                    |
| raft default CF | 2%       | Raft 日志                 |

8.0 之后引入了 shared block cache，所有 CF 共用一个 pool，调度更灵活。如果你还在 6.x 用独立 cache，升级后记得把独立配置去掉，让 TiKV 自己分配。

### 3.2 Raftstore 线程池

`raftstore.store-pool-size` 默认值是 2，看起来很小。官方的建议是：**保持 Raftstore CPU 使用率低于 60%，不要盲目加大**。加大会导致 fsync 竞争变严重，反而增加写入延迟。

调优 checklist：

1. 观察 Grafana 的 `TiKV-Details → Thread CPU → Raft store CPU`
2. 持续高于 60% 再考虑加
3. 每次加 1，观察写入 P99 和 compaction 水位
4. 同时开 StoreWriter 池分担：`raftstore.store-io-pool-size = 2`

```toml
[raftstore]
store-pool-size = 2
apply-pool-size = 2
store-io-pool-size = 2  # 8.0+ 推荐开启
```

StoreWriter 是 6.5 引入的异步写入池，把 Raft log 的 IO 从 store 线程里剥离出来。开了之后观察 Raftstore CPU 通常能降 10-15 个百分点。

### 3.3 UnifyReadPool：写多读少场景的福音

TiKV 7.1 默认启用了 UnifyReadPool，把 coprocessor 读请求和普通 kv get 请求的线程池合并。老版本上我们经常看到：coprocessor 池忙死，kv get 池空转。合并后利用率显著提升：

```toml
[readpool.unified]
min-thread-count = 1
max-thread-count = 16   # 一般设为 CPU 核数的 80%
```

注意 `max-thread-count` 一旦加到超过 CPU 核数，会触发线程切换开销，反而变慢。

## 四、PD 调度参数：让调度别掺和业务

PD 是 TiDB 的大脑，调度器参数直接决定集群稳定性。几个最关键的参数：

```toml
[schedule]
leader-schedule-limit = 4      # 同时调度的 leader 上限
region-schedule-limit = 2048   # 同时调度的 region 上限
replica-schedule-limit = 64    # 副本级调度
merge-schedule-limit = 8       # region 合并
hot-region-schedule-limit = 4  # 热点调度

[schedule.store-limit]
add-peer = 15   # 新加副本速率
remove-peer = 15
```

我的经验法则：

- **扩容时**：`region-schedule-limit` 和 `store-limit` 可以临时调大到默认的 2 倍，加快数据均衡
- **业务高峰**：调小 `leader-schedule-limit` 到 1 或 2，避免频繁切 leader 影响 P99
- **节点下线**：把下线节点的 `store-limit-remove-peer` 调到 20-30，加快数据迁出，否则 `pd-ctl store remove` 要跑几天

一条非常有用的 pd-ctl 命令：

```bash
# 临时调整某节点的 store limit，不动全局配置
pd-ctl store limit 4 30 remove-peer
```

### 4.1 Hot Region 热点治理

TiDB 最常见的性能问题之一是热点写入，通常出现在：

1. 自增 ID 主键，所有写入都打到最后一个 region
2. 时间戳前缀索引，按时间写入单调递增
3. 分区表的新分区刚创建时是一个 region

TiDB 6.1 引入的 `SHARD_ROW_ID_BITS` 和 auto-random 是主要解法：

```sql
-- 整数主键用 auto random
CREATE TABLE t1 (
  id BIGINT PRIMARY KEY AUTO_RANDOM(5),
  ...
);

-- 无主键表用 shard row id
CREATE TABLE t2 (
  ...
) SHARD_ROW_ID_BITS=4 PRE_SPLIT_REGIONS=4;
```

`AUTO_RANDOM(5)` 会把主键的高 5 位做成随机值，把顺序写打散成 32 个 region。`PRE_SPLIT_REGIONS=4` 则在建表时预分裂成 16 个 region，避免刚上线时的冷启动热点。

配合观察 Dashboard 的 Key Visualizer，能看到写入是否均匀。发现热点后如果是历史表，用 `SPLIT TABLE t BETWEEN (a) AND (b) REGIONS 16` 手动分裂也可以。

## 五、TiFlash：别一上来就全量副本

TiFlash 是 TiDB 的列存引擎，给 HTAP 场景用。很多团队上 TiFlash 的姿势不对：把所有大表都创建 TiFlash 副本，以为这样分析查询就快了。

真实情况是：

1. TiFlash 副本会消耗大量内存和磁盘，一张 1TB 表的 TiFlash 副本可能占 200GB
2. TiFlash 的写入是从 TiKV 同步的，高 TPS 下会给 TiKV 带来额外 CPU 压力
3. 优化器并不总是选 TiFlash，配置不当反而走 TiKV 扫描更慢

我的建议流程：

1. 先用 `EXPLAIN` 看哪些慢查询受益于列存
2. 单独给这些表加 TiFlash 副本：`ALTER TABLE t SET TIFLASH REPLICA 1`
3. 观察 TiFlash 副本同步完成：`SELECT * FROM information_schema.tiflash_replica`
4. 强制走 TiFlash 验证效果：`SELECT /*+ READ_FROM_STORAGE(TIFLASH[t]) */ ...`
5. 收集统计信息：`ANALYZE TABLE t`
6. 观察一周，如果稳定再去掉 hint

TiFlash 的 MPP 模式需要至少两个 TiFlash 节点才能发挥，单节点等于白买。

## 六、备份恢复：BR + PITR 的组合拳

TiDB 的备份方案在过去两年变化很大，现在的推荐是 BR（Backup & Restore）+ PITR（Point-in-Time Recovery）组合。

### 6.1 全量备份 + 日志备份

```bash
# 1. 开启日志备份（需要 6.2+）
tiup br log start --task-name=daily-pitr \
  --pd="pd-0:2379" \
  --storage="s3://mybucket/tidb-log?access-key=xxx&secret-access-key=yyy"

# 2. 每日全量快照
tiup br backup full \
  --pd="pd-0:2379" \
  --storage="s3://mybucket/tidb-snapshot/$(date +%Y%m%d)" \
  --ratelimit 128  # 限速 128MB/s 每节点

# 3. 恢复到任意时间点
tiup br restore point \
  --pd="pd-0:2379" \
  --full-backup-storage="s3://mybucket/tidb-snapshot/20241005" \
  --storage="s3://mybucket/tidb-log" \
  --restored-ts='2024-10-05 14:32:00'
```

几个注意事项：

1. 日志备份会在所有 TiKV 节点启动 `br log` 任务，网络出口要足够，否则 log 堆积
2. `ratelimit` 一定要加，否则全量备份能把业务 IO 全占了
3. 恢复到新集群时，TiDB 的 GC safepoint 必须早于备份时间，否则数据不全
4. 定期做恢复演练，我们每季度一次，真实恢复过两次全量

### 6.2 备份 SLA 实操

我们团队的 SLA 定义：

- RPO：5 分钟（日志备份频率）
- RTO：2 小时（全量 1TB 数据恢复时间）
- 备份成功率：> 99%
- 恢复演练：每季度一次

告警规则示例（Prometheus）：

```yaml
- alert: TiDBBackupLogLag
  expr: tikv_log_backup_last_flush_ts - time() < -300
  for: 5m
  annotations:
    summary: "TiDB 日志备份延迟超过 5 分钟"

- alert: TiDBBackupFailed
  expr: increase(backup_failed_total[1d]) > 0
  for: 1m
```

## 七、监控与告警：别只看 Grafana 首页

TiDB 自带的 Grafana 面板非常全，但首页只能告诉你"有没有事"，真正诊断问题要深入到二级面板。我平时最常看的几个：

| 面板                           | 看什么                                      |
|--------------------------------|---------------------------------------------|
| TiDB-Summary                   | QPS/Duration/Connection 大盘                |
| TiKV-Details → RocksDB         | Write Stall、Compaction 水位、SST 文件数    |
| TiKV-Details → Thread CPU      | Raftstore/Apply/Sched CPU 是否打满          |
| PD → Cluster                   | Region 数、调度中数量、store 状态           |
| PD → Operator                  | 调度算子成功率、耗时                        |
| TiDB-Runtime                   | Go GC、goroutine 数                         |

核心告警规则（精简版）：

```yaml
# TiKV Write Stall
- alert: TiKVWriteStall
  expr: delta(tikv_engine_write_stall{type=~"level0|memtable"}[1m]) > 10
  for: 2m

# Raftstore CPU 过高
- alert: TiKVRaftstoreCPUHigh
  expr: sum(rate(tikv_thread_cpu_seconds_total{name=~"raftstore.*"}[1m])) by (instance) > 0.8 * count(tikv_thread_cpu_seconds_total{name=~"raftstore.*"}) by (instance)
  for: 5m

# PD leader 频繁切换
- alert: PDLeaderChange
  expr: changes(pd_server_tso_handle_tsos_duration_seconds_count[10m]) > 3
  for: 1m

# Region 严重不均衡
- alert: TiKVRegionUnbalanced
  expr: (max(tikv_pd_heartbeat_tick_total) - min(tikv_pd_heartbeat_tick_total)) / avg(tikv_pd_heartbeat_tick_total) > 0.3
  for: 30m
```

## 八、真实故障复盘

### 8.1 Raftstore CPU 打爆导致集群雪崩

**现象**：某个周五晚上 22 点，监控报 TiKV P99 延迟从 30ms 飙到 2s，应用端大量 `context deadline exceeded`，持续 15 分钟后自动恢复。

**排查过程**：

1. 看 Grafana → TiKV Thread CPU，Raftstore 线程 CPU 到 100% 持续 15 分钟
2. 同时段 Compaction L0 文件数从 4 涨到 40，出现 Write Stall
3. 查业务侧，发现有个离线 ETL 任务用 `INSERT INTO ... SELECT` 往 TiDB 灌了 2 亿行数据
4. 这个任务默认批次 5000 行、无限速，把 Raftstore 和 RocksDB 都打穿了

**根因**：批量写入场景下，单个事务涉及的 region 过多，Raftstore 来不及 apply。

**修复**：

1. 紧急：降低 ETL 并发，每批 500 行，加 10ms sleep
2. 中期：给 ETL 用的 TiDB 节点单独拉出来，限制 `txn-total-size-limit`
3. 长期：改用 TiDB Lightning 的 Physical Import 模式做批量导入，绕过 Raftstore

教训：**TiDB 不是 MySQL，不要把所有负载都塞给同一套 TiDB 节点**。OLTP 用一组、批量任务用另一组，SQL 层面隔离。

### 8.2 Placement Rule 配错引发跨机房流量暴涨

**现象**：上线某个新的 Placement Policy 后，机房间带宽从 200Mbps 飙到 2Gbps，触发网络告警。

**根因**：策略写错了 PRIMARY_REGION，把 leader 全调到了异地机房，所有读请求都走跨机房。

**修复**：回滚策略，等 PD 把 leader 调回来（大概 20 分钟）。

**教训**：Placement Policy 变更必须在 staging 集群先灰度，生产变更前用 `pd-ctl config placement-rules show` 确认规则没打架。

### 8.3 PD 磁盘 fsync 慢导致 leader 选举抖动

**现象**：PD leader 每隔几小时切换一次，业务偶发 5 秒卡顿。

**排查**：

1. `etcdctl endpoint status` 看到 PD 背后的 etcd fsync P99 超过 1s
2. `iostat -x` 发现 PD 机器的 SSD await 达到 50ms
3. 进一步发现是 PD 和 TiKV 混部，TiKV compaction 把磁盘 IO 打满了

**修复**：拆分 PD 到独立机器，问题彻底解决。

## 九、升级策略：LTS 之间怎么跳

TiDB 的 LTS 版本大概每年一个，7.5 → 8.5 是典型路径。升级要点：

1. **读 release notes 里的 Compatibility Changes**，8.5 有几个默认参数变了（比如 `tidb_enable_non_prepared_plan_cache` 默认开）
2. **备份 + PITR 就位**，升级前做全量备份
3. **滚动升级顺序**：PD → TiKV → TiFlash → TiDB → 工具（TiCDC/DM）
4. **先升级 staging 验证一周**，重点看慢 SQL 是否有回退
5. **生产升级选业务低峰期**，TiKV 滚动升级每节点大约 10 分钟，60 节点集群整体约 1.5-2 小时

TiUP 命令：

```bash
tiup cluster upgrade prod-cluster v8.5.0 --transfer-timeout 600
```

`--transfer-timeout` 是 leader 驱逐超时，默认 5 分钟。大集群建议加到 10-15 分钟，否则可能因为 leader 没驱逐干净而失败。

## 十、什么时候不要用 TiDB

写了这么多 TiDB 的好话，最后也讲讲它不适合的场景。我见过几个团队上 TiDB 后又下掉的，总结下来：

1. **数据量 < 500GB**：MySQL + 从库够用，TiDB 的运维成本不划算
2. **QPS < 1000 且没有水平扩展需求**：上 TiDB 等于杀鸡用牛刀
3. **对事务隔离级别有特殊要求**：TiDB 只支持 RC 和 RR，没有 Serializable
4. **大量外键和触发器**：TiDB 支持但性能不如 MySQL
5. **存储过程重度依赖**：TiDB 不支持存储过程

技术选型没有银弹。我现在的判断标准是：**只有当数据量、QPS、扩展性三者至少两项卡住 MySQL 时，才考虑 TiDB**。否则老老实实上主从 + 分库分表，运维心智负担小得多。

## 工具生态速查

几个必装的外围工具：

| 工具          | 用途                            | 版本要求     |
|---------------|---------------------------------|--------------|
| TiUP          | 集群管理                        | 跟随 TiDB    |
| BR            | 备份恢复                        | 内置         |
| DM            | MySQL → TiDB 同步               | 7.x+         |
| TiCDC         | TiDB 增量同步到 Kafka/MySQL     | 内置         |
| Lightning     | 批量导入                        | 内置         |
| pd-ctl        | PD 调度控制                     | 内置         |
| tikv-ctl      | TiKV 故障修复                   | 慎用，救命用 |
| Dashboard     | Slow Log、Key Visualizer、ContinuousProfiling | 内置 |

Dashboard 的 Continuous Profiling（6.5+）是个好东西，它会定时给每个组件做 profiling，故障回溯时翻翻火焰图经常能找到根因。默认关闭，生产强烈建议打开。

## 最后几条经验法则

- **任何参数调整都在非高峰先试**，用 `pd-ctl` 修改在线参数比改 toml 重启快得多
- **监控比调优重要**，没有完善监控的集群不要动调优参数
- **PD 比 TiKV 更脆弱**，优先保证 PD 的资源隔离
- **热点问题早发现**，Dashboard 的 Key Visualizer 每周至少看一次
- **版本不要跨太多**，老老实实从 LTS 到 LTS，别贪新版本的新特性

TiDB 是一套非常强大的分布式数据库，但它的复杂度也比 MySQL 高一个量级。把它运维好需要团队在存储、网络、Raft、RocksDB 都有一些基础认知。这篇文章只能覆盖最常见的 30% 场景，剩下的 70% 要靠你自己在生产里慢慢积累。

参考资料（用于写作时核对版本与参数名）：

- PingCAP 官方文档 docs.pingcap.com/tidb/stable，本文参数以 7.5/8.5 LTS 为准
- TiKV RocksDB Overview 与 Thread Pool Tuning 两篇文档
- TiDB 8.5 LTS Release Notes（2024-12 发布）
- 社区 Asktug 上的若干生产案例帖，用于交叉验证参数建议
