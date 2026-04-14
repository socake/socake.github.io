---
title: "Redis Cluster 扩缩容与数据迁移实战：从 SETSLOT 到 Atomic Slot Migration"
date: 2024-11-08T10:30:00+08:00
draft: false
tags: ["Redis", "Redis Cluster", "数据库运维", "数据迁移"]
categories: ["数据库"]
description: "Redis Cluster 上手容易运维难。这篇笔记梳理 Cluster 的 slot 模型、扩缩容流程、SETSLOT 协议细节、客户端重定向踩坑、Redis 7 Sharded Pub/Sub 和 Redis 8.4 的 Atomic Slot Migration 新特性，以及跨机房迁移和 Codis → Cluster 平滑迁移的实战案例。"
summary: "很多团队把 Redis Cluster 当成"开箱即用"的分布式 Redis，直到要做扩缩容或数据迁移时才发现：SETSLOT 协议里有十几种状态，迁移过程中客户端重定向要么不生效要么风暴，migrate 卡住没法断，big key 直接把迁移拖垮。这篇文章把我在几套千亿级 Cluster 上做过的扩缩容、迁移、救火全过一遍。"
toc: true
math: false
diagram: false
keywords: ["Redis Cluster", "slot migration", "resharding", "MOVED", "ASK", "ASM"]
params:
  reading_time: true
---

## 写在前面：Redis Cluster 不等于分布式 Redis

Redis Cluster 是官方推荐的分布式方案，但它的设计哲学非常"克制"：没有中心元数据服务、gossip 传播拓扑、客户端直连节点。这套设计的好处是简单、无单点，缺点是一旦拓扑变化，客户端和服务器之间的协调就变得复杂。

我过去维护过两套比较大的 Redis Cluster，分别是 48 分片和 120 分片。扩缩容做过十几次、迁移做过三次、救过一次被 big key 搞挂的生产事故。这篇文章就把这些经验整理出来，希望能帮正在用 Redis Cluster 的团队少踩一些坑。

文章基于 Redis 7.2 和 7.4，提到 Redis 8.4 的 Atomic Slot Migration 时会明确标注。

## 一、Slot 模型：数据怎么分布

### 1.1 16384 个 slot

Redis Cluster 把所有 key 哈希到 16384 个 slot，每个 slot 由一个主节点负责。哈希函数是：

```
slot = CRC16(key) mod 16384
```

如果 key 包含 `{...}` 结构（比如 `user:{1000}:profile`），只对花括号内的部分做哈希。这是 hash tag 机制，能让多个 key 落到同一个 slot，MULTI/事务/Lua 脚本的前提。

一个 6 节点的 Cluster 示意：

```
                    16384 slots
   +-----------+-----------+-----------+-----------+
   |  0-4095   | 4096-8191 | 8192-12287| 12288-16383|
   +-----------+-----------+-----------+-----------+
         |           |           |           |
     master-1    master-2    master-3    master-4
         |           |           |           |
     replica-1   replica-2   replica-3   replica-4
```

### 1.2 Master 数量不是越多越好

很多人默认"分片越多性能越好"，实际上 Cluster 推荐的分片数：

| 数据量规模  | 建议分片数  | 原因                              |
|-------------|-------------|-----------------------------------|
| < 30GB      | 3-6         | 单机 Redis 更简单                 |
| 30-200GB    | 6-12        | 适度分片                          |
| 200GB-1TB   | 12-30       | 官方建议分片不超过 1000 但别贪多  |
| > 1TB       | 30-100      | 考虑 Keyspace 分库或多 Cluster    |

分片多了带来的问题：

1. **Gossip 消息量 O(N²)**：100 分片每秒 gossip 流量能到几十 MB
2. **fail detection 变慢**：节点数多了选举更久
3. **客户端连接池膨胀**：客户端要维护到所有 master 的连接
4. **运维复杂度指数上升**

我的建议是单 Cluster 不超过 60 分片，再大就考虑按业务拆多个 Cluster。

## 二、扩容的完整流程

假设我们有个 6 分片 Cluster，想加到 8 分片。

### 2.1 加新节点

```bash
# 启动两个新实例
redis-server /etc/redis/new-master-1.conf
redis-server /etc/redis/new-master-2.conf

# 加入 Cluster
redis-cli --cluster add-node new-master-1:6379 existing-master-1:6379
redis-cli --cluster add-node new-master-2:6379 existing-master-1:6379

# 给新节点加 replica
redis-cli --cluster add-node new-replica-1:6379 existing-master-1:6379 \
  --cluster-slave --cluster-master-id <new-master-1-id>
```

注意 `add-node` 只是加入拓扑，新节点还没分配任何 slot，需要下一步 reshard。

### 2.2 Reshard：分配 slot

```bash
redis-cli --cluster reshard existing-master-1:6379 \
  --cluster-from all \
  --cluster-to <new-master-1-id> \
  --cluster-slots 2048 \
  --cluster-yes
```

意思是：把 2048 个 slot 从所有现有节点平均迁到 new-master-1。执行过程中 `redis-cli` 会调用 `CLUSTER SETSLOT` 系列命令逐个 slot 迁移。

**不要一次性大量迁移**。建议分批，每批 500-1000 个 slot，中间观察业务 P99 是否抖动。

### 2.3 最后 Rebalance

迁移完成后用 rebalance 命令把 slot 分布微调到均衡：

```bash
redis-cli --cluster rebalance existing-master-1:6379 --cluster-use-empty-masters
```

整个扩容从 6 分片到 8 分片，我通常留 2-4 小时的窗口，业务侧配合做好连接池重试。

## 三、SETSLOT 协议：理解迁移的底层

redis-cli 的 reshard 只是封装，底层是 `CLUSTER SETSLOT` 命令。理解它能帮你在迁移卡住时手动救场。

### 3.1 迁移的四个步骤

以把 slot 1000 从节点 A 迁到节点 B 为例：

```
# 1. B 上把 slot 1000 标记为 importing
B> CLUSTER SETSLOT 1000 IMPORTING <A-node-id>

# 2. A 上把 slot 1000 标记为 migrating
A> CLUSTER SETSLOT 1000 MIGRATING <B-node-id>

# 3. 循环：从 A 拿一批 key，MIGRATE 到 B
A> CLUSTER GETKEYSINSLOT 1000 100
A> MIGRATE B-host B-port "" 0 5000 KEYS key1 key2 ...

# 4. 所有 key 迁完后，通知拓扑
A> CLUSTER SETSLOT 1000 NODE <B-node-id>
B> CLUSTER SETSLOT 1000 NODE <B-node-id>
# 然后 gossip 会把这个变更传播到其他所有节点
```

### 3.2 MIGRATING/IMPORTING 状态下的读写行为

这是最容易踩坑的地方。假设 slot 1000 正在从 A 迁到 B，此时客户端去读 key X：

- **X 已经迁到 B**：A 返回 `ASK` 重定向，告诉客户端"去 B 试试"
- **X 还在 A**：A 正常返回数据
- **X 不存在**：A 返回 `ASK`（因为可能在 B）

客户端收到 `ASK` 后要做两件事：

1. 先对目标节点发送 `ASKING` 命令
2. 再发送原命令

**关键**：`ASKING` 是"一次性"的，只对下一条命令有效。很多自研客户端实现这里出错。

而 `MOVED` 重定向表示 slot 已经完全属于另一个节点，客户端要更新路由表。

### 3.3 常见"卡住"原因

迁移卡住的根因通常是其中之一：

1. **Big Key**：单个 key 太大，MIGRATE 超时
2. **客户端超时**：MIGRATE 命令超时阈值不够
3. **BGSAVE 运行中**：MIGRATE 会 fork，和 BGSAVE 冲突
4. **ASKING 没实现**：客户端拿不到迁移中 key
5. **Lua 脚本持有 key**：long-running script 阻塞 slot

手动恢复方法：

```bash
# 看迁移状态
redis-cli -p <port> CLUSTER NODES | grep -E 'migrating|importing'

# 强制把 slot 归还
redis-cli -p <source> CLUSTER SETSLOT 1000 STABLE

# 或者强制设给目标节点
redis-cli -p <source> CLUSTER SETSLOT 1000 NODE <dest-id>
redis-cli -p <dest> CLUSTER SETSLOT 1000 NODE <dest-id>
```

`STABLE` 是取消迁移状态，让 slot 回到正常归属。但要注意如果有数据已经迁走，STABLE 之后那部分数据就"消失"了（其实是在 B 上，但逻辑上不属于这个 slot）。所以 STABLE 只能在迁移还没开始或者已经完成前用。

## 四、Big Key 问题：迁移的头号杀手

### 4.1 什么是 big key

我个人的定义：

- String：单值 > 10KB
- List/Set/Hash/Zset：元素数 > 5000 或总大小 > 1MB
- Stream：entry 数 > 10000

big key 在迁移时会被 MIGRATE 当成一个原子操作发送，Redis 是单线程，这段时间不能处理其他请求。10MB 的 big key 迁移起来可能要几百毫秒，业务直接超时。

### 4.2 扫出所有 big key

```bash
redis-cli --bigkeys -i 0.1
```

这个命令用 SCAN 遍历所有 key，每 100 个 key sleep 0.1 秒，不会影响业务。输出里会列出每种类型最大的 key。

更精细的扫描用 `redis-rdb-tools`：

```bash
pip install rdbtools python-lzf

rdb -c memory dump.rdb > keys.csv
# keys.csv 包含每个 key 的精确内存占用，可以筛出 > 1MB 的
```

### 4.3 治理 big key

1. **拆分**：big hash 按 field 哈希拆成多个 hash
2. **删除**：用 `UNLINK` 异步删除，别用 `DEL`（会阻塞）
3. **过期**：给 big key 加短 TTL 逐步淘汰
4. **迁移前拆分**：确认哪些 big key 即将被迁移，提前拆分

迁移前我会跑这个脚本先探测：

```bash
for slot in $(redis-cli -p 6379 --cluster check localhost:6379 | grep "going to migrate" | awk '{print $4}'); do
  redis-cli -p 6379 CLUSTER COUNTKEYSINSLOT $slot
done
```

找出 key 数量特别多的 slot，提前做 big key 扫描。

## 五、客户端侧的配合

服务端迁移再完美，客户端不配合也是白搭。几个主流 Redis 客户端的配置建议：

### 5.1 Jedis (Java)

```java
JedisCluster jedis = new JedisCluster(
    Set.of(new HostAndPort("master-1", 6379), /* ... */),
    5000,              // connection timeout
    5000,              // socket timeout
    5,                 // max redirections，迁移期间设大一些
    "password",
    new GenericObjectPoolConfig<>() {{
        setMaxTotal(200);
        setMinIdle(10);
        setMaxWaitMillis(3000);
    }}
);
```

`maxRedirections` 默认 5，扩容期间建议调到 10-16。过小会导致请求在拓扑变化时直接失败。

### 5.2 Lettuce (Java)

```java
ClusterClientOptions options = ClusterClientOptions.builder()
    .topologyRefreshOptions(
        ClusterTopologyRefreshOptions.builder()
            .enablePeriodicRefresh(Duration.ofSeconds(30))
            .enableAllAdaptiveRefreshTriggers()
            .build())
    .maxRedirects(10)
    .build();
```

Lettuce 比 Jedis 更智能，支持 adaptive refresh，一旦收到 MOVED 就触发拓扑刷新。生产推荐 Lettuce。

### 5.3 go-redis (Go)

```go
rdb := redis.NewClusterClient(&redis.ClusterOptions{
    Addrs: []string{"master-1:6379", "master-2:6379"},
    MaxRedirects: 10,
    RouteRandomly: false,
    ReadOnly: false,
    PoolSize: 50,
    MinIdleConns: 10,
    DialTimeout: 5 * time.Second,
    ReadTimeout: 3 * time.Second,
    WriteTimeout: 3 * time.Second,
})
```

### 5.4 Python redis-py

```python
from redis.cluster import RedisCluster

rdb = RedisCluster(
    host='master-1', port=6379,
    max_connections_per_node=50,
    retry_on_timeout=True,
    cluster_error_retry_attempts=5,
    socket_timeout=3,
    socket_connect_timeout=5,
)
```

### 5.5 通用原则

1. **所有客户端都要支持 MOVED 和 ASK**：别用老 driver
2. **拓扑定时刷新**：不要等到 MOVED 再刷
3. **连接池大小要足**：扩容期间连接复用率下降
4. **重试次数调大**：5 次不够，设 10-16 次

## 六、Redis 8.4 的 Atomic Slot Migration

Redis 8.4 引入了 ASM（Atomic Slot Migration），是近几年 Cluster 侧最大的改进。

### 6.1 老协议的痛点

传统 SETSLOT 迁移的问题：

1. **per-key 迁移**：每个 key 单独 MIGRATE，上下文切换开销大
2. **big key 卡主线程**：迁移期间阻塞
3. **慢**：1000 万 key 的 slot 迁移大约 192-219 秒
4. **客户端重定向复杂**：ASK 状态管理

### 6.2 ASM 的改进

ASM 的核心思想：**把整个 slot 范围作为原子单位迁移**，用类似主从同步的流机制一次性搬过去。改进：

1. **6-8 秒完成**：官方测试比老协议快约 30 倍
2. **无 ASK 中间态**：客户端看到的要么是"在源"要么是"在目标"
3. **原子切换**：拓扑变更一次完成
4. **Big Key 友好**：流式传输，不阻塞

启用方式（Redis 8.4+）：

```bash
CLUSTER SLOTMIGRATE <target-node-id> <slot-start> <slot-end>
```

或者通过 redis-cli 的新参数：

```bash
redis-cli --cluster reshard ... --cluster-use-atomic
```

不过要注意：

1. 需要全 Cluster 升级到 8.4
2. 客户端版本要支持新的命令响应
3. 迁移期间目标节点内存占用会临时翻倍（因为数据先复制再切换）

如果你正在规划一次大的 Cluster 迁移，是否值得等 8.4？我的建议是：

- 如果当前版本 < 7.2，先升级到 7.2 LTS 稳定运行
- 8.4 GA 后先在 staging 跑 1-2 个月
- 核心业务 2025 年底 2026 年初再上生产

## 七、跨机房迁移的实战

跨机房迁移是个大活。我做过一次从华东机房整体迁到华南机房，数据量 800GB、分片数 48。

### 7.1 方案选择

几种方案对比：

| 方案                       | 优点                | 缺点                        |
|----------------------------|---------------------|-----------------------------|
| Cluster replication 扩副本 | 简单，自带一致性    | 需要互通网络，延迟敏感      |
| RDB 全量 + AOF 增量同步    | 网络要求低          | 自己写脚本复杂              |
| redis-shake                | 工具成熟            | 对 Cluster 支持参差         |
| 双写迁移                   | 最稳                | 业务改造量大                |
| DBdoctor / Canal 类工具    | 透明                | 依赖外部组件                |

我们选的是 **redis-shake v4**（阿里开源，现在 tair 团队维护），它对 Cluster 支持比较完善。

### 7.2 redis-shake 实战

```toml
# shake.toml
type = "sync"

[source]
version = "7.2"
address = "source-cluster:6379"
password = "xxx"

[target]
type = "redis"
version = "7.2"
address = "target-cluster:6379"
password = "yyy"

[advanced]
dir = "data"
ncpu = 4
pipeline_count_limit = 1024
target_redis_proto_max_bulk_len = 536870912
```

跑起来：

```bash
./redis-shake shake.toml
```

几个注意事项：

1. **big key 可能卡住**，配置 `big_key_threshold` 提前拆分
2. **全量同步时目标 Cluster 不能有写入**，否则会被覆盖
3. **增量同步有延迟**，切换前要等 delay 降到 0
4. **切换时要做数据校验**，用 `redis-full-check` 对比源和目标

### 7.3 切换步骤

我们实际切换用的步骤：

```
T0   启动 redis-shake 全量+增量同步
T+6h 全量完成，进入增量同步阶段
T+12h 延迟稳定在 < 100ms
T+14h 业务低峰期，公告开始切换
T+14h+1min 业务停写（通过降级开关）
T+14h+3min redis-shake 延迟降到 0
T+14h+5min 数据校验通过
T+14h+6min 业务切换 DNS 到新 Cluster
T+14h+10min 灰度回滚观察
T+14h+30min 全量切换完成
```

整个切换业务可写中断 5-10 分钟，可读中断 0。

## 八、监控告警

Redis Cluster 要监控的关键指标：

```yaml
# 节点存活
- alert: RedisClusterNodeDown
  expr: up{job="redis"} == 0
  for: 30s

# Slot 分布
- alert: RedisClusterSlotsUnassigned
  expr: redis_cluster_slots_assigned < 16384
  for: 1m
  annotations:
    summary: "Cluster slots 未完全分配，当前 {{ $value }}"

# 主从失联
- alert: RedisMasterWithoutReplica
  expr: redis_connected_slaves == 0
  for: 5m
  labels:
    severity: critical

# 内存
- alert: RedisMemoryHigh
  expr: redis_memory_used_bytes / redis_memory_max_bytes > 0.85
  for: 5m

# Big Key 兆兆警告
- alert: RedisBigKey
  expr: redis_db_keys{db="db0"} > 0 and on(instance) redis_key_size_bytes > 10485760
  for: 1m
```

除此之外强烈推荐集成 `redis-cli --cluster check` 到每日巡检：

```bash
#!/bin/bash
RESULT=$(redis-cli --cluster check master-1:6379)
if echo "$RESULT" | grep -qi "error\|warning"; then
  send_alert "$RESULT"
fi
```

## 九、真实故障复盘

### 9.1 Big Key 导致迁移卡住，整个 Cluster hang 住

**现象**：某次扩容迁移到 slot 5000 时卡住，应用报 `CLUSTERDOWN The cluster is down` 错误。

**排查**：源节点 `SLOWLOG` 看到一条 MIGRATE 命令耗时 8 秒。查这个 slot 的 key，发现一个 120MB 的 hash。

**根因**：MIGRATE 8 秒内源节点无法响应其他请求，cluster gossip 错误判定源节点失联，其他节点发起了选举。选举冲突导致 slot 归属混乱，`CLUSTERDOWN` 触发。

**紧急恢复**：

```bash
# 所有节点都执行，临时降低 cluster-node-timeout
redis-cli -p <port> CONFIG SET cluster-node-timeout 30000

# 手动 STABLE 卡住的 slot
redis-cli -p <source> CLUSTER SETSLOT 5000 STABLE
redis-cli -p <dest> CLUSTER SETSLOT 5000 STABLE

# 拆分 big key
redis-cli HGETALL big_hash | awk '...' | redis-cli --pipe
redis-cli UNLINK big_hash
```

**长期改进**：

1. 迁移前必须跑 big key 扫描
2. 发现 > 10MB 的 key 必须先拆分
3. 迁移期间 `cluster-node-timeout` 调大到 30s
4. 升级到 Redis 8.4 用 ASM（长远方案）

### 9.2 客户端 MOVED 风暴

**现象**：某次扩容完成后，应用侧突然 QPS 降低 40%，业务报错率飙升。

**排查**：Java 应用侧大量 `MOVED` 日志，Lettuce 的拓扑刷新一直在重试。

**根因**：扩容期间 gossip 还没完全同步，Lettuce 的 periodic refresh 间隔 60 秒，在此期间客户端路由表错乱，每个请求都要经历一次 MOVED 重定向。

**修复**：把 `refreshPeriod` 调到 10 秒，`enableAllAdaptiveRefreshTriggers` 打开。迁移期间再临时降到 5 秒。

### 9.3 跨机房网络抖动触发脑裂

**现象**：跨机房 Cluster（A 机房 4 master、B 机房 4 master）B 机房到 A 机房的专线抖动 30 秒，之后出现 slot 冲突。

**根因**：`cluster-require-full-coverage yes` 情况下，B 机房以为 A 机房挂了，触发了副本晋升。专线恢复后两个机房都有 master 认为自己拥有相同 slot。

**修复**：

1. 关闭 `cluster-require-full-coverage`（但业务要能接受部分不可用）
2. `cluster-node-timeout` 调大到 30s，给网络抖动缓冲
3. 长期方案：单机房 Cluster + 跨机房用其他方案（比如 shake 同步）

**教训**：**Redis Cluster 不是为跨机房设计的**，强一致性要求跨机房场景应该用多 Cluster 方案，不要把一个 Cluster 的节点分散到不同机房。

## 十、总结与经验法则

写到这里，把 Redis Cluster 运维心得浓缩成几条：

- **分片数不要贪多**，60 以内足够绝大多数业务
- **big key 是一切问题的根源**，上线前就要有 big key 扫描和告警
- **不要跨机房部署单个 Cluster**，跨机房用同步工具
- **客户端必须支持 MOVED/ASK**，拓扑定时刷新是刚需
- **迁移分批进行**，一次几百 slot，观察 P99
- **`cluster-require-full-coverage` 按业务决定**，不是默认开就对
- **Redis 8.4 的 ASM 值得等**，尤其是大 Cluster
- **监控要同时覆盖集群拓扑和 per-node 指标**

Redis Cluster 是个很典型的"简单到复杂"的系统：入门文档几页就能跑起来，但真正用到生产级别需要理解每个协议细节。希望这篇笔记能让你在下次扩容前多一份底气。

参考资料：

- Redis 官方的 Cluster Specification 文档
- Redis 7.2/8.4 的 release notes
- redis-shake v4 文档
- Lettuce/Jedis 的 Cluster Topology Refresh 相关文档
- Severalnines 和 OneUptime 两个社区上的几篇 Cluster 实战文章
