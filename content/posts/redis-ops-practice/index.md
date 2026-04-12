---
title: "Redis 运维实践：持久化配置、集群模式与生产监控"
date: 2024-11-06T10:20:00+08:00
draft: false
tags: ["Redis", "数据库", "运维", "缓存", "高可用"]
categories: ["数据库"]
description: "从持久化选型、部署模式到生产监控，梳理 Redis 运维中的关键决策点，以及大 Key、热 Key、AOF rewrite 内存暴涨等实际问题的处理方式。"
summary: "Redis 运维看起来简单，但真到了生产出了问题才知道水有多深。本文整理了持久化、集群、监控、故障处理等核心运维主题。"
toc: true
math: false
diagram: false
keywords: ["Redis", "RDB", "AOF", "Redis Sentinel", "Redis Cluster", "maxmemory", "redis_exporter", "大Key", "热Key"]
params:
  reading_time: true
---

Redis 在我们的业务里承担了缓存、会话存储、分布式锁、消息队列等多个角色。运维了两三年，对几个关键决策点有了一些自己的判断。本文把这些经验整理出来，主要面向需要在生产环境管理 Redis 的运维和开发。

## 持久化选择：RDB vs AOF vs 混合模式

这个问题没有统一答案，核心是理解你能接受多少数据丢失。

### RDB（快照）

RDB 是将内存数据快照写到磁盘，默认配置：

```redis.conf
# 触发条件：N 秒内有 M 次写操作就触发
save 900 1      # 900 秒内至少 1 次写
save 300 10     # 300 秒内至少 10 次写
save 60 10000   # 60 秒内至少 10000 次写

# RDB 文件名和路径
dbfilename dump.rdb
dir /var/lib/redis

# 子进程写入失败时，停止接受写操作
stop-writes-on-bgsave-error yes

# RDB 文件压缩（CPU 换磁盘空间）
rdbcompression yes
```

**RDB 适合的场景：**
- 可以接受最多几分钟的数据丢失（两次快照之间的数据）
- 对恢复速度要求高（RDB 文件直接加载，比 AOF 快很多）
- 做备份和灾备（RDB 文件结构紧凑，易于传输）

**RDB 的问题：**
- 数据丢失窗口较大。如果在两次 save 之间宕机，最多丢失几百秒的数据
- fork 子进程写 RDB 时会有短暂的内存峰值（COW 机制），大内存实例（32GB+）可能造成明显抖动

### AOF（追加写日志）

AOF 记录每条写命令，重放可以恢复到最新状态。

```redis.conf
appendonly yes
appendfilename "appendonly.aof"

# fsync 策略：这是最关键的配置
# always：每条命令都 fsync，最安全，性能最差
# everysec：每秒 fsync 一次，最多丢 1 秒数据，推荐
# no：由 OS 决定何时 fsync，性能最好，但宕机可能丢更多数据
appendfsync everysec

# AOF 重写触发条件
auto-aof-rewrite-percentage 100   # AOF 文件增长到上次重写后大小的 2 倍
auto-aof-rewrite-min-size 64mb    # 且文件大小超过 64MB
```

`appendfsync everysec` 是最常见的生产选择，在性能和数据安全之间取得平衡，最多丢 1 秒的写操作。

**AOF 适合的场景：**
- 对数据丢失非常敏感（金融、订单类业务）
- 写入量适中（AOF 文件过大会影响重放速度）

### 混合持久化（Redis 4.0+，推荐）

混合模式结合了 RDB 和 AOF 的优点：AOF 文件头部是 RDB 快照，后面追加增量 AOF。加载时先快速恢复 RDB，再重放少量 AOF，比纯 AOF 加载快很多。

```redis.conf
appendonly yes
aof-use-rdb-preamble yes    # 开启混合持久化
```

生产环境我的推荐：**开启混合持久化 + appendfsync everysec**。既有 AOF 的数据安全性，又有接近 RDB 的加载速度。

---

## 部署模式：Sentinel vs Cluster

### Redis Sentinel（哨兵）

适合单机数据量不大（内存 < 64GB）、需要高可用的场景。

```
┌──────────┐  ┌──────────┐  ┌──────────┐
│Sentinel 1│  │Sentinel 2│  │Sentinel 3│
└──────────┘  └──────────┘  └──────────┘
      │              │              │
      └──────────────┼──────────────┘
                     │
          ┌──────────┼──────────┐
          │          │          │
      ┌───────┐  ┌───────┐  ┌───────┐
      │Master │  │Slave 1│  │Slave 2│
      └───────┘  └───────┘  └───────┘
```

Sentinel 本身至少 3 个节点（保证选主时的多数投票），生产建议 Redis 节点 1 主 2 从，Sentinel 3 节点。

客户端连接 Sentinel，由 Sentinel 告知当前 Master 地址：

```python
import redis

# 通过 Sentinel 获取连接
sentinel = redis.Sentinel([
    ('sentinel-1', 26379),
    ('sentinel-2', 26379),
    ('sentinel-3', 26379),
], socket_timeout=0.5)

# 获取 Master 连接（自动感知主从切换）
master = sentinel.master_for('mymaster', socket_timeout=0.5)
slave = sentinel.slave_for('mymaster', socket_timeout=0.5)

master.set('key', 'value')
value = slave.get('key')
```

### Redis Cluster（集群）

适合数据量大、需要水平扩展的场景。数据按 16384 个 slot 分片，每个节点负责一部分 slot。

```
Node 1 (Master)  Node 2 (Master)  Node 3 (Master)
slots: 0-5460    slots: 5461-10922  slots: 10923-16383
    │                  │                    │
Node 4 (Slave)  Node 5 (Slave)   Node 6 (Slave)
```

选择边界：
- 数据量 < 50GB、QPS < 10W：Sentinel 够用
- 数据量 > 50GB 或需要横向扩展：考虑 Cluster
- 业务代码用了 MGET/Pipeline 且 key 分散在不同 slot：需要用 HashTag 保证同 slot，或改写代码

---

## K8s 上部署 Redis（Bitnami Helm Chart）

```bash
helm repo add bitnami https://charts.bitnami.com/bitnami
helm repo update

# Sentinel 模式部署
helm install redis bitnami/redis \
  --namespace redis \
  --create-namespace \
  -f redis-values.yaml
```

关键配置文件 `redis-values.yaml`：

```yaml
architecture: replication    # replication（主从+Sentinel）或 standalone

auth:
  enabled: true
  password: "your-strong-password"

sentinel:
  enabled: true
  masterSet: mymaster
  quorum: 2

master:
  persistence:
    enabled: true
    storageClass: gp3
    size: 20Gi
  resources:
    requests:
      memory: 2Gi
      cpu: 500m
    limits:
      memory: 4Gi
      cpu: 2000m

replica:
  replicaCount: 2
  persistence:
    enabled: true
    storageClass: gp3
    size: 20Gi
  resources:
    requests:
      memory: 2Gi
      cpu: 500m
    limits:
      memory: 4Gi
      cpu: 2000m

# 关键配置参数
commonConfiguration: |-
  maxmemory 3gb
  maxmemory-policy allkeys-lru
  appendonly yes
  aof-use-rdb-preamble yes
  appendfsync everysec
  hz 15
  tcp-keepalive 300

metrics:
  enabled: true
  serviceMonitor:
    enabled: true    # 配合 Prometheus Operator 自动发现
```

---

## 内存管理：maxmemory-policy 策略选择

这个配置直接影响缓存满了之后的行为，选错了要出事故。

```redis.conf
maxmemory 4gb
maxmemory-policy allkeys-lru   # 选择淘汰策略
```

各策略说明：

| 策略 | 行为 | 适用场景 |
|------|------|---------|
| `noeviction` | 满了直接报错，拒绝写入 | 不能丢数据的持久化场景（慎用） |
| `allkeys-lru` | 从所有 key 中淘汰最近最少使用 | 纯缓存场景，推荐 |
| `volatile-lru` | 只从设了 TTL 的 key 中淘汰 LRU | 混合存储（部分 key 无 TTL）的场景 |
| `allkeys-lfu` | 从所有 key 中淘汰使用频率最低 | 热点数据明显，LFU 比 LRU 更精准 |
| `allkeys-random` | 随机淘汰 | 几乎不用 |
| `volatile-ttl` | 优先淘汰 TTL 最短的 key | 特定场景下有用 |

纯缓存场景推荐 `allkeys-lru` 或 `allkeys-lfu`。`noeviction` 是最危险的：内存满了，任何写操作都会返回 OOM 错误，包括更新现有 key，直接导致业务故障。

---

## 监控指标与 redis_exporter

用 redis_exporter 暴露 Prometheus 指标，配合 Grafana 监控。

```yaml
# 部署 redis_exporter
apiVersion: apps/v1
kind: Deployment
metadata:
  name: redis-exporter
  namespace: redis
spec:
  replicas: 1
  selector:
    matchLabels:
      app: redis-exporter
  template:
    metadata:
      labels:
        app: redis-exporter
    spec:
      containers:
        - name: redis-exporter
          image: oliver006/redis_exporter:v1.58.0
          env:
            - name: REDIS_ADDR
              value: "redis://redis-master.redis.svc.cluster.local:6379"
            - name: REDIS_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: redis
                  key: redis-password
          ports:
            - containerPort: 9121
```

关键监控指标：

```promql
# 缓存命中率（核心指标，低于 90% 要告警）
rate(redis_keyspace_hits_total[5m]) / 
(rate(redis_keyspace_hits_total[5m]) + rate(redis_keyspace_misses_total[5m]))

# 内存使用率
redis_memory_used_bytes / redis_memory_max_bytes

# 连接数
redis_connected_clients

# 每秒操作数
rate(redis_commands_processed_total[1m])

# 命令平均延迟（毫秒）
redis_commands_duration_seconds_total / redis_commands_processed_total * 1000

# 被驱逐的 key 数量（持续驱逐说明内存不够）
rate(redis_evicted_keys_total[5m])

# 复制延迟（主从同步是否正常）
redis_replication_lag
```

Grafana 告警规则建议：
- 命中率 < 80%：告警
- 内存使用率 > 85%：告警
- 复制延迟 > 30 秒：告警
- 连接数 > maxclients 的 80%：告警

---

## 生产常见问题处理

### 大 Key 扫描与处理

大 Key（Value 很大或集合类型元素很多）会阻塞 Redis 主线程，影响所有请求。

```bash
# 扫描大 Key（不会阻塞，推荐）
redis-cli --bigkeys -h redis-master -a yourpassword

# 输出示例
# Biggest string found so far 'user:profile:10001' with 524288 bytes
# Biggest list   found so far 'task:queue' with 50000 items
# Biggest hash   found so far 'product:details' with 10000 fields

# 查看具体 key 的大小
redis-cli -h redis-master -a yourpassword debug object <key>
# encoding:embstr serializedlength:524288 lru:...

# 对于大 Hash/List/Set，用 HSCAN/LRANGE/SSCAN 分批处理，不要用 HGETALL
redis-cli -h redis-master -a yourpassword
> HSCAN product:details 0 COUNT 100
```

处理大 Key 的原则：
- 大 String：考虑压缩存储，或者拆分成多个小 Key
- 大 List/Set：分页存储，用多个 Key 分片
- 删除大 Key 用 `UNLINK`（异步删除），不用 `DEL`（同步，会阻塞）

```bash
# 安全删除大 Key
redis-cli -h redis-master -a yourpassword UNLINK bigkey:name
```

### 热 Key 处理

热 Key 是指被频繁访问的少数 Key，所有流量打到同一个 Redis 节点，可能造成单点过热。

诊断方式：
```bash
# Redis 4.0+ 的 hotkeys 功能
redis-cli --hotkeys -h redis-master -a yourpassword

# 或者用 monitor 抓取（生产慎用，会影响性能）
redis-cli -h redis-master -a yourpassword monitor | head -1000 | \
  awk '{print $4}' | sort | uniq -c | sort -rn | head -20
```

处理热 Key 的方法：
1. **本地缓存**：应用层对热 Key 结果做本地内存缓存（Caffeine/Guava Cache），TTL 设短一些（几秒到几十秒）
2. **Key 分散**：将热 Key 复制成多份（`hot_key:1`、`hot_key:2`...），读取时随机选一个
3. **Redis Cluster + 读从节点**：把热 Key 的读操作分散到多个从节点

---

## 踩坑记录

### 坑1：AOF rewrite 期间内存暴涨

触发 AOF 重写时，Redis 会 fork 子进程来写新的 AOF 文件。期间父进程的写操作会走 COW（写时复制），如果写入量很大，内存可能翻倍。

我们有一次在业务高峰期触发了 AOF rewrite（文件增长到触发阈值），实例从正常使用 8GB 内存瞬间涨到 14GB，触发了 OOM Killer，Redis 进程被杀死。

应对措施：
```redis.conf
# 方案1：在 rewrite 期间禁止 fsync（牺牲一点数据安全）
no-appendfsync-on-rewrite yes

# 方案2：调高 rewrite 触发阈值，避免频繁 rewrite
auto-aof-rewrite-percentage 200   # 增长到 200% 才触发
auto-aof-rewrite-min-size 512mb

# 方案3：调大内存 limits，给 rewrite 留足空间
# limits 建议是 maxmemory 的 1.5-2 倍
```

### 坑2：Redis Cluster 的 MOVED 错误

使用 Redis Cluster 时，客户端如果不支持 Cluster 协议，或者连接到了错误的节点，会收到 MOVED 错误：

```
(error) MOVED 3999 127.0.0.1:6380
```

这个错误的意思是：key 的 slot 在另一个节点上，请去那个节点操作。

解决方法：
1. **使用支持 Cluster 的客户端库**（Python 用 redis-py-cluster 或 redis-py 4.x，Java 用 Lettuce，Go 用 go-redis）
2. 代码里不要直接 hardcode Redis 节点地址，用 Cluster 客户端连接，它会自动处理 MOVED 重定向

```python
from redis.cluster import RedisCluster

# 正确的 Cluster 连接方式
rc = RedisCluster(
    host="redis-cluster.redis.svc.cluster.local",
    port=6379,
    password="yourpassword",
    decode_responses=True
)

rc.set("foo", "bar")    # 自动路由到正确节点
```

### 坑3：持久化目录满了导致 Redis 拒绝写入

有次 RDB 写入失败（磁盘满了），`stop-writes-on-bgsave-error yes` 配置让 Redis 直接停止接受所有写操作，业务全量报错。

```bash
# 检查 Redis 持久化错误
redis-cli info persistence | grep rdb_last_bgsave_status
# rdb_last_bgsave_status:err

# 临时解决（清理磁盘后重置错误状态）
redis-cli config set stop-writes-on-bgsave-error no
# 或者触发一次成功的 BGSAVE
redis-cli bgsave
```

长期解决：监控持久化目录磁盘使用率，提前告警。在 K8s 上用 PVC，配合 StorageClass 设置合理的容量，并监控 PVC 使用量。

---

## 小结

Redis 运维最重要的三件事：

1. **持久化配置要匹配业务需求**：纯缓存可以只开 RDB 或不持久化；对数据完整性有要求的，开混合持久化
2. **内存策略要明确**：永远不要在缓存场景用 noeviction，allkeys-lru/lfu 是更安全的默认选择
3. **监控要到位**：命中率、内存使用率、复制延迟这三个指标至少要有告警覆盖

大多数 Redis 生产事故都源于配置不合理加上监控不足，在问题扩大到影响业务之前没有发现。把监控和告警做好，你的 Redis 运维会轻松很多。
