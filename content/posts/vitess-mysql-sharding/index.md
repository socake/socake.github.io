---
title: "Vitess 实战：把 MySQL 水平扩展到 PB 级的路"
date: 2024-12-24T14:00:00+08:00
draft: false
tags: ["Vitess", "MySQL", "分布式数据库", "分库分表"]
categories: ["数据库"]
description: "Vitess 是 YouTube/Slack/GitHub 都在用的 MySQL 水平扩展方案。这篇笔记从 Vitess 的架构原理讲到 keyspace/shard/vindex 的建模、VReplication 的工作机制、MoveTables 和 Resharding 的实战步骤、Online DDL、以及生产运维的几个核心问题。目标是帮你判断"我们的业务到底需不需要 Vitess"。"
summary: "当 MySQL 单库扛不住、又不想切 TiDB 或 PG 的时候，Vitess 就成了最后一个选项。它保留了 MySQL 兼容性，用 vtgate 做分片代理，用 VReplication 做在线 resharding。听起来很美，但 Vitess 的学习曲线陡得惊人。这篇文章是我调研 Vitess 几个月、在 staging 跑通一个 4 shard 集群后的全面笔记。"
toc: true
math: false
diagram: false
keywords: ["Vitess", "vtgate", "vttablet", "VReplication", "resharding", "vindex", "MySQL sharding"]
params:
  reading_time: true
---

## Vitess 是什么，又不是什么

Vitess 最早是 YouTube 内部为了解决 MySQL 水平扩展问题做出来的，后来捐给 CNCF 成了毕业项目。它目前被 Slack、GitHub、HubSpot、PlanetScale 用在生产，最大的案例单集群几千个 shard、PB 级数据。

但 Vitess 不是**分布式数据库**（像 TiDB、CockroachDB 那样）。它本质是一套**分库分表代理 + 元数据管理 + 在线迁移工具**的组合。底下跑的仍然是标准 MySQL。理解这个定位非常关键：

- Vitess 有：MySQL 兼容、水平扩展、在线 resharding
- Vitess 没有：全局强一致、跨 shard 事务（有限支持）、自动分布式 SQL 优化

如果你的业务需要跨 shard 强一致事务，Vitess 不是好选择，考虑 TiDB 或 CockroachDB。如果你的业务能按租户/用户分片、99% 查询都带分片键，Vitess 可能是最省事的方案。

这篇文章基于 Vitess v20 和 v22 两个版本。本文预设读者熟悉 MySQL 主从复制和基础分库分表概念。

## 一、架构：每一层都有讲究

```
                  +---------------+
                  |  Application  |
                  +-------+-------+
                          | MySQL protocol
                          |
                  +-------+-------+
                  |    vtgate     |   无状态代理层
                  |  (路由/查询)  |
                  +---+---+---+---+
                      |   |   |
          +-----------+   |   +-----------+
          |               |               |
    +-----+-----+   +-----+-----+   +-----+-----+
    | vttablet  |   | vttablet  |   | vttablet  |
    |  + mysqld |   |  + mysqld |   |  + mysqld |
    +-----------+   +-----------+   +-----------+
     shard -80       shard 80-       shard xxx
          ^
          |
    +-----+-----+
    |  topology |   etcd / consul / zk
    |   server  |   存元数据
    +-----------+
```

### 1.1 vtgate：MySQL 协议的代理

应用连 vtgate 就像连普通 MySQL。vtgate 负责：

1. 解析 SQL
2. 根据 vschema 路由到正确的 shard
3. 跨 shard 时做 scatter-gather
4. 事务管理（单 shard 或跨 shard）

vtgate 无状态，可以水平扩展。生产部署建议每个可用区放几个 vtgate 实例，用 LB 打散。

### 1.2 vttablet：管 mysqld 的 sidecar

每个 mysqld 实例都有一个 vttablet 作为 sidecar，两者 1:1。vttablet 做几件事：

1. 提供 gRPC API 给 vtgate 调用
2. 管理 mysqld 生命周期（启停、健康检查）
3. 执行 backup/restore
4. 运行 VReplication 流
5. 处理故障切换

一个 shard 对应一个 replica set（1 primary + N replica），每个 mysqld 都有自己的 vttablet。

### 1.3 Topology Server

Vitess 的元数据（shard 拓扑、schema、vschema）存在 topology server，支持 etcd、consul、zookeeper。

**推荐 etcd**：部署简单、性能好、和 K8s 生态一致。etcd 3 节点就够，不需要像 CephMON 那样 5 节点。

### 1.4 Cell：地理概念

Vitess 的 **cell** 大致对应一个 AZ 或机房。一个 keyspace 可以跨多个 cell 部署。Cell 是故障隔离的单位：vtgate 优先路由到本 cell 的 vttablet，跨 cell 只在本 cell 无可用副本时才做。

## 二、核心概念：Keyspace / Shard / VSchema

### 2.1 Keyspace

对应一个"逻辑数据库"。类似 MySQL 的 database，但被切分到多个 shard。

### 2.2 Shard

Keyspace 的水平分片单位。每个 shard 是一个独立的 MySQL 复制组（1 primary + replicas）。Vitess 的 shard 名字用 key range 表示：

- `-80`：key 的二进制小于 `0x80` 的落这里
- `80-`：key 的二进制大于等于 `0x80` 的落这里
- `-40`、`40-80`、`80-c0`、`c0-`：四分片

这种表示法的好处是 resharding 时能很自然地"切一半"：`-80` 可以切成 `-40` 和 `40-80`。

### 2.3 VSchema：分片规则

VSchema 定义每张表怎么分片。核心是 **vindex**（Vitess index）。常见 vindex 类型：

- **hash**：对 key 做哈希，均匀分布
- **unicode_loose_md5**：字符串的哈希
- **lookup**：二级索引，通过 lookup 表映射到 primary vindex
- **consistent_lookup**：强一致的 lookup

例子：

```json
{
  "sharded": true,
  "vindexes": {
    "hash_idx": { "type": "hash" },
    "user_lookup": {
      "type": "lookup_hash_unique",
      "params": {
        "table": "user_lookup",
        "from": "email",
        "to": "user_id"
      },
      "owner": "users"
    }
  },
  "tables": {
    "users": {
      "column_vindexes": [
        {
          "column": "user_id",
          "name": "hash_idx"
        },
        {
          "column": "email",
          "name": "user_lookup"
        }
      ]
    },
    "orders": {
      "column_vindexes": [
        { "column": "user_id", "name": "hash_idx" }
      ]
    }
  }
}
```

这段 vschema 的含义：

1. `users` 表按 `user_id` 做 hash 分片（primary vindex）
2. `users` 表还有一个 `email` 的 lookup vindex：通过 `user_lookup` 这张表把 email 映射到 user_id
3. `orders` 表按 `user_id` 分片，和 `users` 同分片规则

这样设计的好处：

- 按 `user_id` 查 users 和 orders 都是单 shard，高效
- 按 `email` 查 users 时，vtgate 先查 lookup 表拿到 user_id、再路由到正确 shard
- `users` 和 `orders` JOIN 只要带 `user_id`，都是本地 JOIN

### 2.4 Sequences：分布式自增

Vitess 用 sequence 表实现分布式自增 ID：

```sql
CREATE TABLE user_seq (
  id INT,
  next_id BIGINT,
  cache BIGINT,
  PRIMARY KEY (id)
) COMMENT 'vitess_sequence';

INSERT INTO user_seq (id, next_id, cache) VALUES (0, 1, 1000);
```

然后在 vschema 里绑定：

```json
"users": {
  "auto_increment": {
    "column": "user_id",
    "sequence": "user_seq"
  }
}
```

Vitess 从 sequence 表批量申请 ID（每次拉 1000 个），应用代码不用改，插入 users 时 vtgate 自动填充 user_id。

## 三、VReplication：Vitess 的核心魔法

VReplication 是 Vitess 的在线数据迁移引擎，几乎所有涉及数据移动的操作都基于它：

1. **MoveTables**：把表从一个 keyspace 移到另一个
2. **Resharding**：改变 shard 数量
3. **Materialize**：创建物化视图
4. **Online DDL**：运行时 schema 变更

### 3.1 工作原理

VReplication 的本质是一个高级的 MySQL binlog 消费者。给定：

- Source：一组源 tablet
- Target：目标 tablet
- Filter：一段 SQL filter，描述要复制什么数据

```
Source binlog → VReplication stream → Filter → Target mysqld
```

比如 resharding 时 filter 可能是：

```sql
SELECT * FROM users WHERE user_id IN (hash(...) < 0x80)
```

VReplication 先做全量 copy，然后追增量 binlog，最终 "trafic switch" 把读写切到新 shard。

### 3.2 MoveTables 实战

场景：从 `monolith` keyspace 拆出 `users` 和 `user_profile` 到新的 `users` keyspace。

```bash
# 1. 创建目标 keyspace（未分片）
vtctldclient CreateKeyspace users --sharding-column-type=VARBINARY

# 2. 启动 MoveTables
vtctldclient MoveTables --workflow=move_users --target-keyspace=users create \
  --source-keyspace=monolith --tables="users,user_profile"

# 3. 等全量复制完成
vtctldclient Workflow --keyspace=users move_users show

# 4. 预检查数据一致性
vtctldclient VDiff --workflow=move_users --target-keyspace=users create my_diff
vtctldclient VDiff --workflow=move_users --target-keyspace=users show my_diff

# 5. 切换读流量
vtctldclient MoveTables --workflow=move_users --target-keyspace=users SwitchTraffic --tablet-types=replica,rdonly

# 6. 观察一段时间，确认无问题后切换写流量
vtctldclient MoveTables --workflow=move_users --target-keyspace=users SwitchTraffic --tablet-types=primary

# 7. 完成后清理
vtctldclient MoveTables --workflow=move_users --target-keyspace=users Complete
```

整个过程对应用是透明的，应用看到的 SQL 语法、连接信息都不变，只是背后数据物理位置变了。

### 3.3 Resharding 实战

场景：把 `users` keyspace 从 2 shard（-80、80-）扩到 4 shard（-40、40-80、80-c0、c0-）。

```bash
# 1. 创建新 shard
vtctldclient CreateShard users/-40
vtctldclient CreateShard users/40-80
vtctldclient CreateShard users/80-c0
vtctldclient CreateShard users/c0-

# 2. 给新 shard 部署 tablet（略）

# 3. 初始化新 shard
vtctldclient InitShardPrimary --force users/-40 zone1-100
# ...

# 4. 启动 Reshard 工作流
vtctldclient Reshard --workflow=reshard_users --target-keyspace=users create \
  --source-shards='-80,80-' --target-shards='-40,40-80,80-c0,c0-'

# 5. 等复制追上
vtctldclient Workflow --keyspace=users reshard_users show

# 6. VDiff 校验
vtctldclient VDiff --workflow=reshard_users --target-keyspace=users create verify1

# 7. 切流量
vtctldclient Reshard --workflow=reshard_users --target-keyspace=users SwitchTraffic --tablet-types=replica,rdonly
# 观察
vtctldclient Reshard --workflow=reshard_users --target-keyspace=users SwitchTraffic --tablet-types=primary

# 8. 完成
vtctldclient Reshard --workflow=reshard_users --target-keyspace=users Complete
```

注意事项：

1. **全量阶段耗时长**：10TB 数据可能跑几天
2. **增量阶段会有延迟**：切流量前必须确认 lag < 几秒
3. **VDiff 必做**：切之前用 VDiff 逐行校验，发现不一致中止
4. **切流量有短暂阻塞**：几秒钟的写入 pause，应用要能容忍
5. **回滚很贵**：Complete 之前都能回滚，Complete 后就定型了

## 四、查询路由与跨 shard 查询

Vitess 的查询路由按三个级别：

1. **Single Shard**：SQL 里 WHERE 带了 vindex 列，路由到单 shard。最快。
2. **Scatter**：没带 vindex，查所有 shard 合并结果。慢。
3. **Unsharded**：访问 unsharded keyspace，走唯一的那个 shard。

### 4.1 避免 scatter

scatter 查询有两个大问题：

1. 查询被发到所有 shard，shard 数多了 latency 叠加
2. 单个 shard 慢就整个查询慢
3. 占用所有 shard 的 CPU/IO

看一个 SQL 是不是 scatter：

```sql
EXPLAIN FORMAT=VITESS
SELECT * FROM orders WHERE user_id = 123;
```

输出会告诉你 routing 方式。应用侧能做的：

1. **所有 WHERE 都带分片键**：这是最有效的
2. **大查询走离线**：走 analytics 副本或者导出到数仓
3. **限制 vtgate scatter 超时**：`--query_timeout=30s`，防止一个慢查询拖垮集群

### 4.2 JOIN 的处理

Vitess 的 JOIN 分三种：

1. **Same-shard JOIN**：两张表同分片键，vtgate 直接下推到 mysqld 本地 JOIN。最快。
2. **Cross-shard JOIN**：需要 vtgate 做 Nested Loop，先查一张再查另一张。慢。
3. **Unsharded JOIN**：两张都在 unsharded keyspace。按正常 MySQL 处理。

Vitess 22 之后有实验性的 hash join 支持，但生产还不建议依赖它。**分片设计时尽量让常 JOIN 的表共享分片键**。

### 4.3 跨 shard 事务

Vitess 支持跨 shard 事务，但语义比较微妙：

- **SINGLE**：只能单 shard 事务（默认）
- **MULTI**：允许跨 shard，但是 best-effort，不保证原子
- **TWOPC**：基于两阶段提交，强一致但慢

```sql
SET GLOBAL transaction_mode = 'MULTI';
```

**建议**：生产默认 SINGLE，跨 shard 业务逻辑用 outbox pattern 或 saga 解决，不要依赖 TWOPC。

## 五、高可用与故障切换

Vitess 的 HA 通过 **orchestrator** 或内置的 `vtorc` 组件实现：

```yaml
# vtorc 配置
orchestrator:
  enabled: true
  topology_refresh_seconds: 30
  recovery_period_block_seconds: 60
  recovery_ignore_hostname_filters: []
```

vtorc 监控所有 tablet，primary 故障时自动提升 replica。切换时间通常 30-60 秒。

对应用的影响：

1. 主切换期间写入会失败（几十秒）
2. vtgate 会自动重路由到新 primary
3. 应用层要处理连接错误和重试

## 六、备份与恢复

Vitess 自带备份机制，支持存到 S3/GCS 等：

```yaml
backup:
  backup_storage_implementation: s3
  s3_backup_aws_region: us-west-2
  s3_backup_storage_bucket: vitess-backups
  s3_backup_storage_root: prod/
```

触发备份：

```bash
vtctldclient Backup zone1-100
```

备份原理是 `xtrabackup` 或 `mysqldump`，选 xtrabackup，增量备份、恢复快。

恢复一个新 replica：

```bash
vtctldclient RestoreFromBackup zone1-101
```

tablet 启动时会自动从 S3 拉最新备份恢复。

## 七、监控与告警

Vitess 暴露 Prometheus metrics：

```yaml
- alert: VitessTabletUnhealthy
  expr: vtgate_tablet_health{status!="SERVING"} == 1
  for: 2m

- alert: VitessReplicationLagHigh
  expr: vttablet_mysql_replication_lag_seconds > 30
  for: 5m

- alert: VitessVReplicationLag
  expr: vttablet_vreplication_lag_seconds > 60
  for: 5m

- alert: VitessQueryFailed
  expr: rate(vtgate_queries_processed_errors[5m]) > 10
  for: 5m

- alert: VitessScatterQueryHigh
  expr: rate(vtgate_queries_processed{plan="Scatter"}[5m]) > 100
  for: 10m
  annotations:
    summary: "Scatter 查询频率异常，检查是否有 SQL 没带分片键"
```

Vitess 还有个官方 Grafana 面板叫 "vitess-dashboard"，包含 keyspace、shard、tablet、workflow 多层视图。

## 八、踩坑与经验

### 8.1 Schema 变更慢得让人绝望

Vitess 的 schema 变更必须通过 vtctldclient 执行，底层用 gh-ost 做在线 DDL：

```bash
vtctldclient ApplySchema --sql-file=alter.sql --ddl-strategy="online" users
```

10 亿行的大表 ALTER 要跑几个小时。可以用：

```bash
--ddl-strategy="online --allow-concurrent --fast-over-revertible"
```

`--fast-over-revertible` 会用更快的 in-place 方式但放弃回滚能力。

### 8.2 VDiff 很慢且吃资源

VDiff 逐行比对源和目标，10TB 数据可能跑 24 小时。建议：

1. 在 replica 上跑 VDiff，不碰 primary
2. 用采样模式：`--limit 1000000`
3. 错开业务高峰

### 8.3 Lookup Vindex 的一致性问题

Lookup vindex 需要维护 lookup 表，写入时是两阶段：先写 lookup 再写主表。如果中间故障，会出现 lookup 表有记录但主表没有。Vitess 的 `consistent_lookup` 类型能强一致但性能差一半。

**建议**：能用 hash 就用 hash，lookup 是最后手段。

### 8.4 Keyspace 合并比拆分难

Vitess 的 workflow 支持拆分，但合并 keyspace（比如发现拆得太细想合回去）要手动做 MoveTables + 删 keyspace，比拆麻烦得多。

**教训**：**永远按 under-shard 设计**，宁可初期少分几个 shard，后面再加。

## 九、真实经验：到底要不要上 Vitess

我调研和在 staging 跑过几个月 Vitess 后的判断：

**适合上 Vitess**：

1. 你已经有一套 MySQL 单库扛不住的业务，数据量 > 1TB、QPS > 1 万
2. 业务天然能按用户/租户分片
3. 团队 MySQL 运维经验丰富
4. 短期内不打算切到 NewSQL
5. 能投入至少 1 个专职 DBA

**不适合上 Vitess**：

1. 数据量 < 500GB：MySQL 主从 + 从库分担就够
2. 团队没有 MySQL 运维经验：直接上 TiDB 心智负担小
3. 需要跨 shard 强一致事务：用 TiDB 或 CockroachDB
4. 需要复杂分析 SQL：Vitess 的 SQL 支持不如 TiDB 全
5. 对运维复杂度敏感：Vitess 的学习曲线陡

**替代方案对比**：

| 方案           | 优点                            | 缺点                        |
|----------------|---------------------------------|-----------------------------|
| Vitess         | MySQL 兼容，在线 resharding     | 学习曲线陡，跨 shard 弱    |
| TiDB           | NewSQL 语义，运维简单           | MySQL 兼容 95%，生态较新    |
| ShardingSphere | 代码即分片，对应用改造小        | 在线扩缩容难                |
| ProxySQL+分库  | 简单粗暴                        | 运维手工                    |
| PlanetScale    | Vitess 托管，开箱即用           | SaaS、费用、合规            |

我最后的选择是**不上 Vitess 而是 TiDB**，因为：

1. 团队运维经验倾向 TiDB 生态
2. 跨 shard 场景比较多
3. 数据量还没到必须分片的程度

这不是说 Vitess 不好，只是说没有银弹，选型要结合团队和业务。

## 十、经验法则

- **Vitess 不是分布式数据库**，是 MySQL 分片代理
- **分片设计是核心**，一旦错了代价极高
- **hash vindex 优先**，lookup 是最后手段
- **让常 JOIN 的表共享分片键**
- **单 shard 查询优先，scatter 要监控**
- **VReplication 是强大工具但慢**
- **Schema 变更必须走 online DDL**
- **HA 依赖 vtorc，切换有几十秒窗口**
- **团队要有 MySQL 老 DBA**

Vitess 是一个技术上非常漂亮的项目，YouTube 在十几年前做的设计选择今天看仍然领先。但它的复杂度也实在过高，大部分团队其实够不着它的甜蜜点。希望这篇笔记能帮你理性评估自己是不是真的需要 Vitess。

参考资料：

- Vitess 官方文档 vitess.io/docs，v20 和 v22 有差异注意
- Vitess GitHub 的 examples 目录，有完整可跑的 demo
- PlanetScale 的博客，他们是 Vitess 最大的商业用户
- Slack engineering 的 Vitess 迁移系列文章
- vitessio/vitess 的 design doc 目录
