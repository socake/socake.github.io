---
title: "MongoDB 分片集群实战：从 shard key 设计到 chunk 均衡的全链路"
date: 2024-11-20T15:00:00+08:00
draft: false
tags: ["MongoDB", "分片", "数据库运维", "分布式"]
categories: ["数据库"]
description: "MongoDB 分片集群的真正难点不是搭建，而是 shard key 设计、chunk 均衡和热点治理。这篇笔记覆盖 MongoDB 7.0 LTS 的 shard key 选型策略、hashed vs ranged、refineCollectionShardKey、zone sharding、balancer 调优以及几个真实热点和 jumbo chunk 故障的救火案例。"
summary: "很多团队把 MongoDB 分片当成"设个 shard key 就完事"，结果上线半年后发现 80% 数据在一个 shard 上、balancer 每天搬几十 GB 却怎么都追不上、某个 collection 出现 jumbo chunk 无法分裂。这篇文章把我在几套 MongoDB 分片集群上的经验整理出来，希望能让你在分片之前少走一些弯路。"
toc: true
math: false
diagram: false
keywords: ["MongoDB", "sharding", "shard key", "chunk", "balancer", "zone sharding"]
params:
  reading_time: true
---

## 为什么要写这篇

MongoDB 的分片是出了名的"上手简单、搞懂难"。官方文档写得非常详细，但真正决定集群命运的那些细节——比如 shard key 到底怎么选、chunk 大了为什么没法分裂、zone sharding 和 compound shard key 的取舍——往往只有踩过坑才懂。

这篇文章是我维护几套 MongoDB 分片集群（最大 24 shard、单 collection 20 亿文档）积累下来的经验，基于 MongoDB 7.0 LTS（2023 年 9 月发布，当前最新 LTS），兼顾 8.0 的一些新变化。目标读者是：已经在跑 MongoDB 分片，或者正在规划分片的团队。

## 一、分片架构回顾

MongoDB 分片集群由三部分组成：

```
      +-----------+       +-----------+       +-----------+
      |  mongos   |       |  mongos   |       |  mongos   |
      |  router   |       |  router   |       |  router   |
      +-----+-----+       +-----+-----+       +-----+-----+
            |                   |                   |
            +---------+---------+---------+---------+
                      |                   |
              +-------+-------+   +-------+-------+
              |  Config Server|   |  Shard-1 (RS) |
              |  Replica Set  |   |  Primary      |
              |  (CSRS)       |   |  Secondary*2  |
              +---------------+   +---------------+
                                           ...
                                  +---------------+
                                  |  Shard-N (RS) |
                                  +---------------+
```

- **mongos**：无状态路由，负责解析 shard key 选 shard
- **Config Server**：存集群元数据（chunk 分布、shard 列表），本身是一个 replica set
- **Shard**：每个 shard 是一个独立的 replica set

几个容易被忽视的架构决策：

1. **mongos 放哪儿**：推荐每个应用节点本地一个 mongos，或者每个 AZ 几个 mongos + LB。不要搞"中心化 mongos 集群"。
2. **Config Server 独立部署**：不要和 shard 混部，CSRS 对延迟敏感，fsync 不能被 shard 的 compaction 拖累。
3. **Shard 的 replica set 至少 3 节点**：primary + 2 secondary，`writeConcern: majority` 才有意义。
4. **奇数节点是必须的**：选举需要多数派。

## 二、Shard Key：整个分片集群的灵魂

90% 的分片问题都能追溯到 shard key 没选好。一旦集群跑起来，老版本的 MongoDB 是不能修改 shard key 的（4.4 之前）。4.4 引入 `refineCollectionShardKey`、5.0 引入 `reshardCollection`，但这些操作都有代价。

### 2.1 四个黄金原则

好的 shard key 必须满足：

1. **高基数（cardinality）**：可选值多，才能切出足够多的 chunk
2. **低频率（frequency）**：单个值不会占太多文档
3. **非单调递增**：避免所有新写入都打到最后一个 chunk
4. **匹配查询模式**：常见查询能带上 shard key，避免 scatter-gather

这四点可以互相冲突。比如时间戳字段基数高但单调递增；user_id 匹配查询好但可能频率不均。这就是为什么 shard key 设计没有银弹。

### 2.2 Hashed vs Ranged

**Hashed sharding**：对 shard key 做哈希，均匀分布。

优点：
- 写入永远均衡，不会热点
- 不需要预分片

缺点：
- 范围查询会广播到所有 shard
- 无法利用 locality

**Ranged sharding**：按 shard key 的值范围切分。

优点：
- 范围查询高效
- 同值查询定位单个 shard

缺点：
- 如果 shard key 单调递增，写入全打到一个 shard
- 需要预分片防止初期热点

### 2.3 选择决策树

```
业务主要是按 ID 点查 && 写入量大?
  是 → hashed sharding on {_id: "hashed"} 或者 {userId: "hashed"}
  否 ↓

业务有明显的时间序列查询?
  是 → compound shard key { tenantId: 1, timestamp: 1 }
        用 tenantId 分散热点，timestamp 保留 locality
  否 ↓

是多租户 SaaS?
  是 → { tenantId: 1, _id: 1 } 或者直接 zone sharding
  否 ↓

默认 → { _id: "hashed" }
```

我见过的错误示例：

- **错**：用 `{ createdAt: 1 }` 做 shard key。所有新写入都打到同一个 chunk。
- **错**：用 `{ type: 1 }` 做 shard key，type 只有 5 种值。基数太低。
- **错**：用 `{ _id: 1 }` 做 ranged。ObjectId 有 4 字节时间戳前缀，基本等于单调递增。
- **对**：`{ tenantId: 1, createdAt: 1 }` 给多租户用。
- **对**：`{ _id: "hashed" }` 均匀分布写入。

### 2.4 Compound shard key 的妙用

Compound shard key（复合分片键）是个被低估的工具。它的核心思路是：**用第一个字段分散负载，用后续字段保留查询 locality**。

例子：一张 `orders` 表，业务按 `userId` 查订单，但有些大客户订单特别多。

- 用 `{ userId: "hashed" }`：解决分散问题，但无法范围查询
- 用 `{ userId: 1 }`：大客户的订单全在一个 chunk
- 用 `{ userId: 1, orderId: 1 }`：userId 相同的订单有序分布，大客户的数据也会被切分到多个 chunk

Compound shard key 还能通过 `refineCollectionShardKey` 在运行时加字段（只能加不能删）：

```js
db.adminCommand({
  refineCollectionShardKey: "mydb.orders",
  key: { userId: 1, orderId: 1 }
});
```

这个命令要求新 key 包含原 key 作为前缀。我在生产用过一次，从 `{ userId: 1 }` 扩展到 `{ userId: 1, orderId: 1 }`，立刻解决了大客户数据倾斜。

## 三、Chunk 与 Balancer

### 3.1 Chunk 基础

MongoDB 把数据按 shard key 切分成 chunk（块），默认大小 128MB（早期 64MB）。每个 chunk 是一段连续的 shard key 范围，比如 `{userId: MinKey}` 到 `{userId: 1000}`。

Chunk 达到 `chunkSize` 时会自动分裂（split），split 触发条件是写入时 mongos 发现 chunk 超大。

Balancer 是 MongoDB 的后台任务（跑在 config server primary 上），负责把 chunk 从"忙" shard 移到"闲" shard。移动的触发条件：

- chunk 数差异超过 `migrationThreshold`（2、4 或 8，取决于集群大小）
- balancer 窗口内

### 3.2 Balancer 调优

默认 balancer 是全天 24 小时跑。生产环境强烈建议配置活动窗口：

```js
use config
db.settings.update(
  { _id: "balancer" },
  { $set: { activeWindow: { start: "01:00", stop: "06:00" } } },
  { upsert: true }
);
```

让 balancer 只在业务低峰跑，避免和业务 IO 抢。

其他 balancer 参数：

```js
// 限制并发 migration 数（7.0 默认允许每个 shard 参与 1 次 migration）
db.settings.update(
  { _id: "balancer" },
  { $set: { _secondaryThrottle: { w: "majority", wtimeout: 10000 } } }
);

// 允许平行迁移（多 shard 同时迁）
// 7.0 之前只能串行，7.0 之后默认支持 parallel
```

### 3.3 手动 moveChunk

Balancer 有时候慢得让人着急，可以手动 moveChunk 加速：

```js
sh.moveChunk("mydb.orders",
  { userId: 500000 },              // chunk 内的任意 shard key
  "shard03");                      // 目标 shard
```

手动迁移前检查 chunk 分布：

```js
db.orders.getShardDistribution();
```

输出类似：

```
Shard shard01 at shard01/host1:27018,host2:27018,host3:27018
 data : 245.3GiB docs : 180000000 chunks : 1840
 estimated data per chunk : 136MiB
 estimated docs per chunk : 97826

Shard shard02 at shard02/...
 data : 189.5GiB docs : 140000000 chunks : 1420
 ...
```

理想状态是 chunks 数和 data 在各 shard 上均衡。

### 3.4 Jumbo Chunk：最头疼的问题

当一个 chunk 内所有文档都有相同的 shard key 值时，这个 chunk 无法再分裂。比如你用 `{ type: 1 }` 做 shard key，`type="normal"` 有 1 亿条文档，它们会塞在同一个 chunk 里，大小可能达到几 GB，这就是 **jumbo chunk**。

jumbo chunk 的麻烦：

1. balancer 无法移动它（超过默认 moveChunk 大小限制 256MB）
2. 单 shard 数据倾斜
3. 查询聚集在这一个 shard 上，变成性能瓶颈

识别 jumbo chunk：

```js
use config
db.chunks.find({ jumbo: true });
```

或者看 chunk 大小：

```js
db.adminCommand({
  dataSize: "mydb.orders",
  keyPattern: { type: 1 },
  min: { type: "normal" },
  max: { type: "promo" }
});
```

### 3.5 解决 jumbo chunk

方法 1：**修改 shard key**（用 refineCollectionShardKey 加维度）

```js
db.adminCommand({
  refineCollectionShardKey: "mydb.orders",
  key: { type: 1, _id: 1 }
});
```

加了 `_id` 后，同 type 的文档可以按 _id 继续切分。

方法 2：**手动移动 jumbo chunk**（临时方案）

```js
// 提高 moveChunk 大小限制
db.adminCommand({ setParameter: 1, maxJumboChunkMovement: true });
sh.moveChunk("mydb.orders", { type: "normal" }, "shard02");
```

方法 3：**reshardCollection**（5.0+，最彻底但最贵）

```js
db.adminCommand({
  reshardCollection: "mydb.orders",
  key: { userId: "hashed" },
  unique: false,
  numInitialChunks: 100
});
```

reshardCollection 会在后台重建整张表到新 shard key，对业务几乎透明但代价是：

- 需要临时磁盘空间（约 120% 原表大小）
- 持续时间长（几小时到几天）
- CPU/IO 占用高
- 期间集群能读写但 chunk 分布会变

我只在一次严重数据倾斜事故中用过 reshardCollection，跑了 36 小时，集群 P99 明显升高但没挂。

## 四、Zone Sharding：地理/业务隔离

Zone sharding 让你把某个 shard key 范围绑定到指定 shard。典型场景：

1. **多地域部署**：欧洲用户数据放欧洲机房的 shard
2. **冷热分层**：热数据放 SSD shard、冷数据放 HDD shard
3. **租户隔离**：大客户独占 shard

例子（多地域）：

```js
// 给 shard 打 tag
sh.addShardTag("shard-eu-1", "EU");
sh.addShardTag("shard-eu-2", "EU");
sh.addShardTag("shard-us-1", "US");
sh.addShardTag("shard-us-2", "US");

// 给 shard key 范围绑定 tag
sh.addTagRange("mydb.users",
  { region: "EU", _id: MinKey },
  { region: "EU", _id: MaxKey },
  "EU");

sh.addTagRange("mydb.users",
  { region: "US", _id: MinKey },
  { region: "US", _id: MaxKey },
  "US");
```

这之后 balancer 会把 `region: "EU"` 的文档都迁到带 EU tag 的 shard，实现地理就近。

注意：

1. shard key 必须包含 zone 字段
2. 迁移不是实时的，等 balancer 慢慢搬
3. zone 边界不能重叠

## 五、写入与读取的一致性

### 5.1 Write Concern

几个层级：

| Level               | 含义                          |
|---------------------|-------------------------------|
| `{w: 0}`            | 不等 ack，最快最不安全        |
| `{w: 1}`            | primary 确认                  |
| `{w: "majority"}`   | 多数派确认，默认推荐          |
| `{w: <N>}`          | 指定 N 个节点确认             |

生产**必须用 `w: majority`**，这是 MongoDB 语义下的持久化保证。配合 `j: true` 确保 journal 落盘。

### 5.2 Read Preference

| Mode                    | 含义                          |
|-------------------------|-------------------------------|
| `primary`               | 只读 primary，默认            |
| `primaryPreferred`      | 优先 primary，失败读 secondary|
| `secondary`             | 只读 secondary                |
| `secondaryPreferred`    | 优先 secondary                |
| `nearest`               | 就近读                        |

分片集群里还有一个 `readConcern: "majority"`，读到的数据必须是多数派已确认的。写入 `majority` + 读取 `majority` 是 linearizable 的前提。

### 5.3 Causal Consistency

MongoDB 4.0 引入的 causal consistency（因果一致性）让同一个 session 内的操作保持顺序：

```js
const session = client.startSession({ causalConsistency: true });
const coll = session.client.db("mydb").collection("orders");
coll.insertOne({ userId: 1, amount: 100 }, { session });
// 后续读一定能读到刚插入的数据
coll.findOne({ userId: 1 }, { session });
```

对于"写完立即读"场景非常有用。代价是要用 session，开发同学经常忘。

## 六、监控与告警

几个必看指标：

| 指标                               | 怎么看                              | 告警阈值       |
|------------------------------------|-------------------------------------|----------------|
| Chunk 分布不均衡                   | `sh.status()` 或 `getShardDistribution` | 差异 > 20%   |
| Balancer 延迟                      | `db.locks.findOne({_id:"balancer"})` | 持续 locked > 1h |
| Jumbo chunks 数量                  | `db.chunks.count({jumbo: true})`    | > 5            |
| Secondary 复制延迟                 | `rs.printSecondaryReplicationInfo()` | > 10s         |
| Config Server 连接数               | serverStatus connections            | > 80% max      |
| mongos → shard 连接池              | mongostat                            | 持续满        |

Prometheus 告警：

```yaml
- alert: MongoChunkImbalance
  expr: |
    (max(mongo_shard_chunks) - min(mongo_shard_chunks))
    / avg(mongo_shard_chunks) > 0.3
  for: 1h

- alert: MongoJumboChunks
  expr: mongo_config_jumbo_chunks > 0
  for: 5m

- alert: MongoReplicationLag
  expr: mongodb_mongod_replset_member_optime_date - on(set) max(mongodb_mongod_replset_member_optime_date) > 10
  for: 5m
```

## 七、真实故障复盘

### 7.1 ShardKey 选错导致 80% 数据在一个 shard

**背景**：新业务上线，6 shard 集群，用 `{ createdAt: 1 }` 做 shard key。

**现象**：上线两周后发现 shard01 的磁盘使用率 90%，其他 shard 30% 不到。

**根因**：`createdAt` 单调递增，所有新写入都打到最后一个 chunk，最后一个 chunk 总在同一个 shard。经典的 monotonic shard key 灾难。

**修复**：

1. 紧急：手动 moveChunk 把部分 chunk 搬到其他 shard
2. 中期：加一个字段补救，`refineCollectionShardKey` 到 `{ createdAt: 1, _id: 1 }`，让 _id 切分热点
3. 长期：`reshardCollection` 到 `{ _id: "hashed" }`，彻底解决

reshardCollection 跑了 48 小时，期间 chunk 分布逐渐均衡。教训是：**shard key 设计的时候就把单调递增问题想清楚**，不要等上线才发现。

### 7.2 Balancer 追不上写入速度

**现象**：一个业务活动期间，shard01 写入 QPS 突增 5 倍，balancer 每天迁移 50GB 数据，但 shard01 和其他 shard 的差距越来越大。

**排查**：Balancer 只在凌晨 1-6 点的活动窗口跑，高峰期 8 小时就能拉出 100GB 差距，凌晨 5 小时只能搬 50GB。

**修复**：

1. 临时：取消 balancer 窗口，24 小时跑
2. 短期：把活动窗口改到 22:00 - 08:00
3. 长期：对这张表加 `{ shard_id: 1 }` 前缀字段，业务层面均衡写入

教训：**balancer 不是万能的**，业务层的数据分布要先做对，balancer 只是兜底。

### 7.3 Jumbo Chunk 阻塞整个 balancer

**现象**：balancer 日志一直报 "failed to move chunk: chunk is jumbo"。

**排查**：某个 collection 用 `{ category: 1, createdAt: 1 }` 做 shard key，其中 `category: "default"` 占 60% 数据，单个 chunk 超过 5GB。

**修复**：

1. 短期：开 `maxJumboChunkMovement`，强制迁移 jumbo chunk 释放压力
2. 长期：`refineCollectionShardKey` 加 `_id` 维度，让 category=default 的数据能切分

**注意**：强制迁移 jumbo chunk 是个"最后手段"，会占用大量网络和 CPU，建议业务低峰做。

## 八、备份与恢复

MongoDB 分片集群的备份比单机复杂得多，核心挑战是**跨 shard 一致性**。

### 8.1 方案选型

| 方案                | 一致性    | 速度    | 适用场景             |
|---------------------|-----------|---------|----------------------|
| mongodump           | 无一致性  | 慢      | 小集群、非生产       |
| fsync + snapshot    | 有一致性  | 快      | 云盘快照，生产首选   |
| Percona Backup      | 有一致性  | 中      | 开源生产             |
| Ops Manager         | 有一致性  | 中      | 商业版               |

推荐的方案是：**停 balancer → fsync lock → 快照所有 shard → 解锁**。

```bash
# 1. 停 balancer
mongosh --eval "sh.stopBalancer()"

# 2. 所有 shard 和 config server 做 fsync lock
for shard in shard01 shard02 shard03 csrs; do
  mongosh --host $shard --eval "db.fsyncLock()"
done

# 3. 对所有节点的数据盘做快照（AWS EBS、阿里云云盘）
aws ec2 create-snapshot --volume-id vol-xxx --description "mongo-backup-$(date +%Y%m%d)"

# 4. 解锁
for shard in shard01 shard02 shard03 csrs; do
  mongosh --host $shard --eval "db.fsyncUnlock()"
done

# 5. 启动 balancer
mongosh --eval "sh.startBalancer()"
```

整个流程通常 5-10 分钟（不算快照创建时间），业务可读可写。

### 8.2 Point-in-Time Recovery

PBM（Percona Backup for MongoDB）支持 PITR，配合定时全量 + 连续 oplog 备份：

```bash
# 启动 oplog slicer
pbm config --set pitr.enabled=true
pbm config --set pitr.oplogSpanMin=10

# 恢复到指定时间
pbm restore --time="2024-11-20T14:30:00"
```

## 九、经验法则

最后是几条铁律：

- **shard key 设计是头等大事**，别等上线才发现
- **hashed 是安全默认**，不确定就选它
- **compound shard key 能救场**，refineCollectionShardKey 是你的朋友
- **balancer 是兜底**，业务层要先做对
- **jumbo chunk 要提前预防**，用 compound key 避免同值聚集
- **writeConcern: majority 是红线**，不要为了性能降级
- **mongos 本地化**，避免成为集群瓶颈
- **备份要有 PITR**，快照 + oplog 双保险
- **监控要细**，chunk 分布比 CPU/内存更重要

MongoDB 分片是个非常强大的能力，用好了能支撑几十亿文档的集群。关键是理解每一个决策的后果，尤其是 shard key 这种一旦定型就很难改的东西。希望这篇笔记能让你在下次规划分片时少走一些弯路。

参考资料：

- MongoDB 7.0 官方 Sharding 章节，所有命令和参数以官方为准
- Percona Blog 的 MongoDB Sharding Pitfalls 系列
- MongoDB Atlas 的 best practices 白皮书
- `sh.status()` 和 `getShardDistribution()` 的实际输出格式参考官方
