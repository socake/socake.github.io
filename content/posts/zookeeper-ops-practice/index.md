---
title: "Zookeeper 运维实战：集群部署、调优与故障排查"
date: 2025-03-05T11:00:00+08:00
draft: false
tags: ["Zookeeper", "分布式协调", "运维", "中间件", "Kafka"]
categories: ["中间件"]
description: "深入 Zookeeper 生产运维：从 ZAB 选举原理到集群调优，从四字命令诊断到连接风暴处理，以及在云原生时代 Zookeeper 的定位与替代方案"
summary: "系统梳理 Zookeeper 生产运维核心技能：ZNode 类型与 Watcher 机制、ZAB 选举算法、3/5 节点集群部署配置、JVM 与 zoo.cfg 调优、四字命令实战诊断、常见故障处理，以及与 Kafka KRaft 模式的关系和云原生场景下的定位。"
toc: true
math: false
diagram: false
keywords: ["Zookeeper", "ZAB", "选举算法", "分布式协调", "Kafka KRaft", "连接风暴", "四字命令", "运维"]
params:
  reading_time: true
---

接手过三套 ZK 集群，两套跟着 Kafka、一套跟着老 HBase。云原生时代新项目基本不会再引入它了，但存量系统的坑你还是得能扛。这篇把踩过的东西记下来。

## Zookeeper 核心概念

### ZNode 类型

Zookeeper 的数据模型是一棵树形结构，每个节点称为 ZNode。ZNode 有四种类型：

```
/
├── /kafka
│   ├── /brokers          (Persistent - 持久节点)
│   ├── /controller       (Ephemeral - 临时节点)
│   └── /config
├── /hadoop
│   └── /leader           (Ephemeral - 临时节点)
└── /locks
    └── /distributed-lock-  (Ephemeral Sequential - 临时顺序节点)
        ├── /distributed-lock-0000000001
        ├── /distributed-lock-0000000002
        └── /distributed-lock-0000000003
```

**Persistent（持久节点）**：
- 创建后永久存在，直到显式删除
- 典型用途：存储配置信息、服务注册表

**Ephemeral（临时节点）**：
- 与创建它的客户端 Session 绑定
- Session 断开后节点自动删除
- 典型用途：服务健康检测、Leader 选举
- 注意：临时节点不能有子节点

**Persistent Sequential（持久顺序节点）**：
- 在父节点下自动追加单调递增的 10 位序号（如 `lock-0000000001`）
- 典型用途：分布式队列、全局唯一 ID 生成

**Ephemeral Sequential（临时顺序节点）**：
- 结合了临时和顺序的特性
- 典型用途：公平分布式锁（Watch 前一个序号节点，实现排队等待）

**Zookeeper 3.6+ 新增 Container 和 TTL 节点**：
- Container：当所有子节点被删除后，Container 节点由服务端自动清理
- TTL：节点超过指定时间未被修改则自动删除

### Watcher 机制

Watcher 是 Zookeeper 实现通知的核心机制，客户端可以在读操作（`getData`、`getChildren`、`exists`）上注册一次性监听器。

```
客户端                              ZooKeeper 服务端
   │                                      │
   │  getData("/config", watch=true)      │
   │─────────────────────────────────────>│
   │  返回数据 + 注册 Watcher             │
   │<─────────────────────────────────────│
   │                                      │
   │       （某时刻 /config 被修改）       │
   │                                      │
   │  NodeDataChanged 事件通知            │
   │<─────────────────────────────────────│
   │                                      │
   │  （客户端重新读取获取最新值）          │
   │  getData("/config", watch=true)      │  ← 必须重新注册！
   │─────────────────────────────────────>│
```

**Watcher 的关键特性**：

1. **一次性**：触发后自动失效，客户端需要重新注册（这是实现代码中最容易忽略的点）
2. **顺序性**：同一 Session 收到的 Watcher 事件是有序的
3. **轻量级通知**：事件本身不携带数据，客户端收到通知后需主动拉取最新值
4. **Session 绑定**：Session 断开时，已注册的 Watcher 会被清除，客户端重连后需重新注册

**Watcher 事件类型**：

| 事件类型 | 触发条件 |
|---------|---------|
| NodeCreated | 节点被创建（对 `exists` 的 watch 生效） |
| NodeDeleted | 节点被删除 |
| NodeDataChanged | 节点数据被修改 |
| NodeChildrenChanged | 子节点列表变化 |
| DataWatchRemoved | watch 被移除（3.6+ 永久 Watcher 专用） |

### ZAB 协议与选举算法

ZAB（Zookeeper Atomic Broadcast）是 Zookeeper 的核心一致性协议，分为两个阶段：

**阶段一：崩溃恢复（Leader 选举）**

当集群启动或 Leader 失联时，触发选举。默认选举算法为 FastLeaderElection（epoch + zxid + myid 三元组投票）：

```
选举规则（按优先级排序）：
1. 优先选 epoch（逻辑时钟/纪元）最大的节点
2. epoch 相同时，优先选 zxid（事务 ID）最大的节点
3. zxid 相同时，优先选 myid 最大的节点

目标：选出数据最新（zxid 最大）的节点作为 Leader，保证不丢数据
```

选举流程示例（3 节点集群，myid 分别为 1、2、3）：

```
1. 初始状态：所有节点都投票给自己
   节点1: vote(epoch=0, zxid=100, myid=1)
   节点2: vote(epoch=0, zxid=102, myid=2)  ← zxid 最大
   节点3: vote(epoch=0, zxid=101, myid=3)

2. 节点1、3 收到节点2 的投票，发现 zxid=102 > 自己，改投节点2
   节点1: vote(epoch=0, zxid=102, myid=2)
   节点2: vote(epoch=0, zxid=102, myid=2)
   节点3: vote(epoch=0, zxid=102, myid=2)

3. 节点2 收到超过半数（3/3）的票，成为 Leader
   选举完成，耗时通常 < 200ms（单机房）
```

**阶段二：消息广播（正常写入）**

```
客户端 → Leader：写请求
Leader：生成新的 zxid，向所有 Follower 发送 Proposal（提案）
Follower：写入本地事务日志，回复 ACK
Leader：收到 Quorum（半数以上）ACK → 发送 Commit
Leader → 客户端：写入成功

关键点：Leader 不需要等所有 Follower ACK，只需 Quorum（n/2+1）即可提交
3 节点集群：需要 2 个 ACK
5 节点集群：需要 3 个 ACK
```

## 集群部署

### 节点数量选择

```
3 节点集群：可容忍 1 台故障
5 节点集群：可容忍 2 台故障
7 节点集群：可容忍 3 台故障

公式：N 节点集群，可容忍 (N-1)/2 台故障

为什么是奇数？
偶数节点集群并不增加容错能力：
- 4 节点 = 容忍 1 台故障（需要 3 个 ACK）
- 4 节点和 3 节点容错能力相同，但 4 节点资源消耗更多
```

生产推荐：**3 节点集群**满足大多数场景，ZooKeeper 本身是轻量级服务，不需要太多节点。

### myid 配置

每个节点必须有唯一的数字标识，写入 `dataDir` 下的 `myid` 文件：

```bash
# 节点1
echo 1 > /data/zookeeper/myid

# 节点2
echo 2 > /data/zookeeper/myid

# 节点3
echo 3 > /data/zookeeper/myid
```

### zoo.cfg 完整配置

```properties
# /etc/zookeeper/conf/zoo.cfg

# ==================== 基础配置 ====================
# 心跳基本单位（毫秒）
tickTime=2000

# dataDir 必须挂载独立磁盘（与 OS 分开），避免 I/O 竞争
dataDir=/data/zookeeper/data

# 事务日志目录，强烈建议与 dataDir 挂不同的磁盘
# 事务日志是顺序写，对磁盘 IOPS 要求高
dataLogDir=/data/zookeeper/txlog

# 客户端连接端口
clientPort=2181

# ==================== 集群配置 ====================
# 集群成员：server.myid=host:集群通信端口:选举端口
server.1=zk1.internal:2888:3888
server.2=zk2.internal:2888:3888
server.3=zk3.internal:2888:3888

# Follower 与 Leader 建立连接的最大 tick 数
# 实际超时 = initLimit * tickTime = 10 * 2000 = 20 秒
initLimit=10

# Follower 与 Leader 同步数据的最大 tick 数
# 超过此时间未同步完成，Follower 与 Leader 断开
# 实际超时 = syncLimit * tickTime = 5 * 2000 = 10 秒
syncLimit=5

# ==================== 性能配置 ====================
# 单次批量提交的最大事务数（默认 1000）
maxBatchSize=1000

# 客户端连接超时（毫秒），客户端 Session 超时的最小/最大值
minSessionTimeout=4000     # 默认 2 * tickTime
maxSessionTimeout=40000    # 默认 20 * tickTime

# 单客户端最大并发连接数（防止单客户端耗尽连接池）
maxClientCnxns=200

# Snapcount：事务日志超过此数量后触发快照
snapCount=100000

# ==================== 安全配置 ====================
# 4 字命令白名单（生产建议只开必要的）
4lw.commands.whitelist=mntr,ruok,stat,dump,conf,isro

# 开启 JMX 监控（配合 Prometheus exporter）
# 通过 JVM 参数配置，见后文

# ==================== 3.5+ 新特性 ====================
# 开启管理端 UI（访问 http://host:8080/commands）
admin.enableServer=true
admin.serverPort=8080

# 自动清理快照和事务日志（防止磁盘写满）
autopurge.purgeInterval=24     # 每 24 小时清理一次
autopurge.snapRetainCount=5    # 保留最近 5 个快照
```

### Docker Compose 部署（测试/开发环境）

```yaml
# docker-compose.yml
version: '3.8'
services:
  zoo1:
    image: zookeeper:3.8
    hostname: zoo1
    ports:
      - "2181:2181"
      - "8080:8080"
    environment:
      ZOO_MY_ID: 1
      ZOO_SERVERS: server.1=zoo1:2888:3888;2181 server.2=zoo2:2888:3888;2181 server.3=zoo3:2888:3888;2181
      ZOO_DATA_DIR: /data
      ZOO_DATA_LOG_DIR: /datalog
      ZOO_TICK_TIME: 2000
      ZOO_INIT_LIMIT: 10
      ZOO_SYNC_LIMIT: 5
      ZOO_MAX_CLIENT_CNXNS: 200
      ZOO_AUTOPURGE_PURGEINTERVAL: 24
      ZOO_AUTOPURGE_SNAPRETAINCOUNT: 5
      ZOO_4LW_COMMANDS_WHITELIST: "mntr,ruok,stat,dump,conf"
    volumes:
      - zoo1-data:/data
      - zoo1-log:/datalog

  zoo2:
    image: zookeeper:3.8
    hostname: zoo2
    ports:
      - "2182:2181"
    environment:
      ZOO_MY_ID: 2
      ZOO_SERVERS: server.1=zoo1:2888:3888;2181 server.2=zoo2:2888:3888;2181 server.3=zoo3:2888:3888;2181
    volumes:
      - zoo2-data:/data
      - zoo2-log:/datalog

  zoo3:
    image: zookeeper:3.8
    hostname: zoo3
    ports:
      - "2183:2181"
    environment:
      ZOO_MY_ID: 3
      ZOO_SERVERS: server.1=zoo1:2888:3888;2181 server.2=zoo2:2888:3888;2181 server.3=zoo3:2888:3888;2181
    volumes:
      - zoo3-data:/data
      - zoo3-log:/datalog

volumes:
  zoo1-data:
  zoo1-log:
  zoo2-data:
  zoo2-log:
  zoo3-data:
  zoo3-log:
```

### Kubernetes StatefulSet 部署

```yaml
# zookeeper-statefulset.yaml
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: zookeeper
  namespace: middleware
spec:
  serviceName: zookeeper-headless
  replicas: 3
  selector:
    matchLabels:
      app: zookeeper
  template:
    metadata:
      labels:
        app: zookeeper
    spec:
      # ZooKeeper 对延迟敏感，建议反亲和性确保跨节点部署
      affinity:
        podAntiAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            - labelSelector:
                matchLabels:
                  app: zookeeper
              topologyKey: kubernetes.io/hostname
      containers:
        - name: zookeeper
          image: zookeeper:3.8
          ports:
            - containerPort: 2181
              name: client
            - containerPort: 2888
              name: follower
            - containerPort: 3888
              name: election
            - containerPort: 8080
              name: admin
          env:
            - name: ZOO_MY_ID
              valueFrom:
                fieldRef:
                  fieldPath: metadata.name  # 配合 init container 解析序号
            - name: ZOO_SERVERS
              value: "server.1=zookeeper-0.zookeeper-headless:2888:3888;2181 server.2=zookeeper-1.zookeeper-headless:2888:3888;2181 server.3=zookeeper-2.zookeeper-headless:2888:3888;2181"
          resources:
            requests:
              memory: "1Gi"
              cpu: "500m"
            limits:
              memory: "2Gi"
              cpu: "2"
          volumeMounts:
            - name: data
              mountPath: /data
            - name: datalog
              mountPath: /datalog
          livenessProbe:
            exec:
              command: ["/bin/bash", "-c", "echo ruok | nc localhost 2181 | grep imok"]
            initialDelaySeconds: 30
            periodSeconds: 10
          readinessProbe:
            exec:
              command: ["/bin/bash", "-c", "echo ruok | nc localhost 2181 | grep imok"]
            initialDelaySeconds: 10
            periodSeconds: 5
  volumeClaimTemplates:
    - metadata:
        name: data
      spec:
        accessModes: ["ReadWriteOnce"]
        storageClassName: fast-ssd  # 使用 SSD 存储类
        resources:
          requests:
            storage: 20Gi
    - metadata:
        name: datalog
      spec:
        accessModes: ["ReadWriteOnce"]
        storageClassName: fast-ssd
        resources:
          requests:
            storage: 20Gi
---
apiVersion: v1
kind: Service
metadata:
  name: zookeeper-headless
  namespace: middleware
spec:
  clusterIP: None
  selector:
    app: zookeeper
  ports:
    - name: client
      port: 2181
    - name: follower
      port: 2888
    - name: election
      port: 3888
```

## 生产配置调优

### JVM 参数调优

```bash
# zkEnv.sh 或通过环境变量 SERVER_JVMFLAGS 设置
export SERVER_JVMFLAGS="
  -Xmx2g
  -Xms2g
  -XX:+UseG1GC
  -XX:MaxGCPauseMillis=100
  -XX:G1HeapRegionSize=16m
  -XX:+ParallelRefProcEnabled
  -XX:+UnlockExperimentalVMOptions
  -XX:+PrintGCDetails
  -XX:+PrintGCDateStamps
  -Xloggc:/var/log/zookeeper/zk-gc.log
  -XX:+UseGCLogFileRotation
  -XX:NumberOfGCLogFiles=5
  -XX:GCLogFileSize=100m
  -Dcom.sun.management.jmxremote
  -Dcom.sun.management.jmxremote.port=9999
  -Dcom.sun.management.jmxremote.authenticate=false
  -Dcom.sun.management.jmxremote.ssl=false
"
```

**堆内存指导原则**：
- ZooKeeper 将所有数据常驻内存（内存即数据库）
- 堆大小 ≥ 数据集大小 × 2（为 GC 预留空间）
- 生产环境建议 `2g ~ 4g`，数据量大的场景可到 `8g`
- 避免配置过大（>8g）导致 GC 停顿影响选举超时

### zoo.cfg 关键参数调优

```properties
# tickTime：基础时间单位，是 Watcher 超时和 Session 超时的基准
# 调小（如 1000ms）：更快检测故障，但网络抖动时选举更频繁
# 调大（如 3000ms）：更稳定，但故障检测延迟增加
# 推荐：单机房 2000ms，跨机房 4000ms
tickTime=2000

# syncLimit 的选择关键：
# 如果 Leader 和 Follower 之间网络延迟较高（如跨 AZ），需要调大
# syncLimit * tickTime 必须大于数据同步所需时间
# 如果有大量写入导致 Follower 频繁 lag，适当调大此值
syncLimit=5

# 客户端 Session 超时范围
# 如果客户端频繁出现 SessionExpired，考虑适当调大 maxSessionTimeout
minSessionTimeout=4000
maxSessionTimeout=40000
```

### 磁盘规划建议

```bash
# 推荐的磁盘布局
/data/zookeeper/
├── data/          → SSD 独立挂载点，存放快照（Snapshot）
│   └── myid
└── txlog/         → SSD 独立挂载点（最好与 data 不同盘），存放事务日志

# 为什么要分开？
# 事务日志是高频顺序写（每次写入都 fsync），与快照 I/O 在同一磁盘会相互干扰
# 生产环境中因磁盘 I/O 竞争导致 Zookeeper 超时的案例非常常见

# 磁盘容量估算
# 事务日志：每秒 1000 TPS × 1KB/事务 = 1MB/s
# 快照：随数据集大小，通常 100MB ~ 1GB
# 日志保留：建议保留 5 个快照 + 对应的事务日志
# 建议数据盘和日志盘各 50GB（足够大多数场景）
```

## 四字命令诊断

四字命令是 Zookeeper 内置的诊断接口，通过 `nc` 或 `telnet` 直接发送：

```bash
# 通用查询方式
echo <cmd> | nc <host> 2181

# 或使用 zookeeper-shell（如果安装了 ZK 客户端工具）
# 注意：生产环境需要在 zoo.cfg 中配置 4lw.commands.whitelist
```

### ruok：健康检查

```bash
$ echo ruok | nc zk1.internal 2181
imok
```

返回 `imok` 表示进程正常运行。注意：`ruok` 只检查进程是否响应，不检查是否处于正常服务状态（如选举中的节点也会返回 `imok`）。

### stat：服务状态概览

```bash
$ echo stat | nc zk1.internal 2181
Zookeeper version: 3.8.3-6ad6d364c7c0bcf0de452d54ebefa3c3fc0a7548, built on 09/07/2023 05:39 GMT
Clients:
 /10.0.1.5:52341[1](queued=0,recved=1254,sent=1254)
 /10.0.1.6:48892[1](queued=0,recved=8765,sent=8766)
 /10.0.1.7:61023[0](queued=0,recved=1,sent=0)

Latency min/avg/max: 0/1/45
Received: 287643
Sent: 287644
Connections: 23
Outstanding: 0
Zxid: 0x10000043f
Mode: leader                   ← 当前角色（leader/follower/observer）
Node count: 15234
```

关键字段解读：
- `Mode: leader/follower`：确认节点角色，用于判断集群是否正常
- `Outstanding: 0`：待处理请求数，若持续 > 0 说明处理能力不足
- `Latency avg`：平均处理延迟，正常应 < 10ms，若持续 > 100ms 需排查
- `Connections`：当前客户端连接数，超过 `maxClientCnxns` 会拒绝新连接

### mntr：详细指标（Prometheus 拉取主要来源）

```bash
$ echo mntr | nc zk1.internal 2181
zk_version	3.8.3-6ad6d364c7c0bcf0de452d54ebefa3c3fc0a7548, built on 09/07/2023 05:39 GMT
zk_avg_latency	1
zk_max_latency	45
zk_min_latency	0
zk_packets_received	287643
zk_packets_sent	287644
zk_num_alive_connections	23
zk_outstanding_requests	0
zk_server_state	leader
zk_znode_count	15234
zk_watch_count	4521           ← 活跃 Watcher 数量
zk_ephemerals_count	234       ← 临时节点数量
zk_approximate_data_size	2048576   ← 内存中数据集大小（字节）
zk_open_file_descriptor_count	128
zk_max_file_descriptor_count	65536
zk_followers	2               ← Leader 视角：当前 Follower 数（只有 Leader 输出）
zk_synced_followers	2        ← 已同步的 Follower 数（应等于 followers）
zk_pending_syncs	0           ← 等待同步的 Follower 数
zk_last_proposal_size	32
zk_max_proposal_size	1024
zk_min_proposal_size	32
```

**重点告警指标**：
- `zk_outstanding_requests > 10`：请求积压，检查 Leader 处理能力
- `zk_synced_followers < 2`（3 节点集群）：Follower 掉线，集群可能失去 Quorum
- `zk_watch_count > 100000`：Watcher 数量异常，可能有内存泄漏
- `zk_approximate_data_size` 增速异常：数据集意外膨胀

### dump：会话与临时节点信息

```bash
$ echo dump | nc zk1.internal 2181
SessionTracker dump:
Session Sets (3):
0x10000000000001	VALID	  # Session ID
0x10000000000002	VALID
0x10000000000003	CLOSING

ephemeral nodes dump:
Sessions with Ephemerals (2):
0x10000000000001:           # 该 Session 持有的临时节点
	/kafka/controller
	/kafka/brokers/ids/1
0x10000000000002:
	/kafka/brokers/ids/2
```

`dump` 用于排查"临时节点为什么没有消失"——找到对应 Session ID，结合 Session 状态判断。

### conf：查看运行时配置

```bash
$ echo conf | nc zk1.internal 2181
clientPort=2181
secureClientPort=-1
dataDir=/data/zookeeper/data/version-2
dataLogDir=/data/zookeeper/txlog/version-2
tickTime=2000
maxClientCnxns=200
minSessionTimeout=4000
maxSessionTimeout=40000
serverId=1
initLimit=10
syncLimit=5
electionAlg=3
electionPort=3888
quorumPort=2888
peerType=0
membership:
server.1=zk1.internal:2888:3888:participant
server.2=zk2.internal:2888:3888:participant
server.3=zk3.internal:2888:3888:participant
```

## 常见问题排查

### 选举超时导致集群不可用

**症状**：客户端报 `ConnectionLoss`，Zookeeper 日志持续出现 `LOOKING` 状态

```bash
# 查看选举日志
grep -E "LOOKING|LEADING|FOLLOWING|election" /var/log/zookeeper/zookeeper.log | tail -50

# 检查节点间网络连通性（2888 和 3888 端口）
nc -zv zk2.internal 3888
nc -zv zk2.internal 2888

# 查看是否存在 GC 停顿导致的超时
grep "GC pause\|Stop-the-world" /var/log/zookeeper/zk-gc.log | tail -20
```

**常见原因与处理**：

1. **网络分区**：检查防火墙规则，确认 2888/3888 端口双向可达
2. **GC 停顿过长**：调整 JVM 参数，使用 G1GC，降低 MaxGCPauseMillis
3. **磁盘 I/O 过高**：检查 `iostat -x 1` 磁盘利用率，事务日志写入慢会导致心跳超时
4. **时钟偏差**：Zookeeper 依赖本机时钟，检查 `ntpstat` 或 `chronyc tracking`

```bash
# 强制触发新一轮选举（已确认某节点数据落后时使用）
# 方法：停止数据最旧的节点，让其他节点先选出 Leader
systemctl stop zookeeper

# 清除数据（仅当节点数据已无法恢复时）
# 危险操作！确保集群中至少有 n/2+1 个节点数据完整
rm -rf /data/zookeeper/data/version-2
rm -rf /data/zookeeper/txlog/version-2
systemctl start zookeeper
# 重启后该节点会作为 Learner 从 Leader 全量同步数据
```

### 连接风暴（Connection Storm）

**症状**：短时间内 `zk_num_alive_connections` 急剧增长，Zookeeper CPU 飙高，大量请求超时

**触发场景**：Kafka Broker 全部重启时，所有 Broker 同时重连 Zookeeper，形成连接风暴。

```bash
# 查看连接数变化趋势
while true; do
  echo -n "$(date): "
  echo stat | nc zk1.internal 2181 | grep "Connections"
  sleep 1
done

# 查看哪些 IP 连接数最多
echo stat | nc zk1.internal 2181 | grep "^/" | awk -F'[/:]' '{print $2}' | sort | uniq -c | sort -rn | head -20
```

**缓解措施**：

```properties
# zoo.cfg 配置连接限流
# 单客户端 IP 最大连接数
maxClientCnxns=200

# 3.6.1+ 新增：全局连接限制
globalOutstandingLimit=1000
```

```java
// 客户端侧：使用指数退避重连策略
CuratorFramework client = CuratorFrameworkFactory.builder()
    .connectString("zk1:2181,zk2:2181,zk3:2181")
    .retryPolicy(new ExponentialBackoffRetry(1000, 10))  // 1s 起步，最多 10 次重试
    .sessionTimeoutMs(30000)
    .connectionTimeoutMs(15000)
    .build();
```

### 磁盘满导致服务中断

Zookeeper 的事务日志采用 fsync 确保持久化，磁盘满后事务日志无法写入，直接导致所有写操作超时。

```bash
# 紧急处理：清理旧的快照和事务日志
# 方法一：使用内置清理工具
java -cp /opt/zookeeper/lib/*:/opt/zookeeper/zookeeper-*.jar \
  org.apache.zookeeper.server.PurgeTxnLog \
  /data/zookeeper/data \
  /data/zookeeper/txlog \
  -n 3  # 保留最近 3 个快照及对应的事务日志

# 方法二：手动删除旧文件
# 快照文件命名：snapshot.<zxid>
# 事务日志命名：log.<zxid>
ls -lt /data/zookeeper/data/version-2/snapshot.* | tail -n +6 | xargs rm -f
ls -lt /data/zookeeper/txlog/version-2/log.* | tail -n +6 | xargs rm -f

# 配置 autopurge 防止再次发生
# zoo.cfg
autopurge.purgeInterval=24
autopurge.snapRetainCount=5
```

### 脑裂（Split Brain）处理

脑裂是指网络分区导致集群出现两个 Leader 的情况。ZAB 协议通过 Quorum 机制防止脑裂：只要集群中没有一半以上节点可达，就不会选出新 Leader。

```
3 节点集群中，如果节点 A 网络隔离：
- 节点 A 自己认为自己是 Leader（但实际上 B+C 已经选出新 Leader）
- 节点 A 处于老 epoch，它的写入客户端已连接不上（因为 Quorum 在 B+C 侧）
- 节点 A 重新加入后，会发现自己 epoch 落后，转变为 Follower
```

但如果同时存在多个旧 epoch 的 "幽灵 Leader"，可能导致客户端状态混乱：

```bash
# 确认当前集群 Leader 是哪个节点
for host in zk1 zk2 zk3; do
  echo -n "${host}: "
  echo stat | nc ${host}.internal 2181 | grep "Mode"
done
# 正常输出应该只有一个 leader，其余为 follower
```

## 与 Kafka 的关系

### Kafka 对 Zookeeper 的依赖（旧版本）

在 Kafka 2.8 之前，Zookeeper 是 Kafka 的核心依赖：

```
Kafka 使用 Zookeeper 存储的数据：
/kafka/brokers/ids/           - Broker 注册与发现
/kafka/controller             - Controller 选举（临时节点）
/kafka/brokers/topics/        - Topic 元数据（分区数、副本分配）
/kafka/config/topics/         - Topic 配置
/kafka/consumers/             - Consumer Group 位移（老版本）
/kafka/admin/                 - 管理操作状态
/kafka/isr_change_notification - ISR 变更通知
```

Zookeeper 的可用性直接影响 Kafka：
- Zookeeper 不可用时，Kafka Controller 无法工作，分区 Leader 选举停止
- 新的 Consumer Group 无法创建
- Topic 配置无法修改

### Kafka KRaft 模式：告别 Zookeeper

Kafka 2.8 引入 KRaft（Kafka Raft）模式，3.3 版本进入 Production Ready，**Kafka 4.0 已彻底移除 Zookeeper 支持**。

```
KRaft 架构变化：
旧架构：Kafka Broker + 独立 Zookeeper 集群（3 节点）
新架构：Kafka 自身实现 Raft 共识（Controller 节点负责）

KRaft 的 Controller 节点职责：
- 存储集群元数据（取代 Zookeeper）
- 基于 Raft 协议选举，无需外部依赖
- 元数据存储在 __cluster_metadata 主题中
```

**迁移策略**：

```bash
# 检查当前 Kafka 版本
kafka-broker-api-versions.sh --bootstrap-server localhost:9092 | head -5

# KRaft 迁移路径（生产谨慎操作）
# Kafka 3.x 支持 ZK 模式 → KRaft 迁移工具
kafka-storage.sh random-uuid  # 生成新的 Cluster ID

# 如果是新集群，直接使用 KRaft 模式
kafka-storage.sh format \
  --config /etc/kafka/kraft/server.properties \
  --cluster-id $(kafka-storage.sh random-uuid)
```

### 什么时候还需要 Zookeeper

在 2025 年，仍然需要 Zookeeper 的场景：

1. **Kafka 版本 < 2.8** 的存量集群（未完成升级时）
2. **HBase**：HBase 仍深度依赖 Zookeeper（RegionServer 注册、Master 选举、分布式锁）
3. **Apache Hadoop YARN**：ResourceManager HA 使用 Zookeeper 做 Leader 选举
4. **Apache Curator 框架**：基于 Zookeeper 实现的分布式原语（锁、选举、队列）
5. **老旧的 SOA 服务发现**（如 Dubbo 2.x 默认注册中心）

**已有替代方案的场景**：

| 用途 | Zookeeper | 替代方案 |
|------|-----------|---------|
| 服务注册发现 | Dubbo + ZK | Nacos / Consul / etcd |
| 分布式锁 | Curator InterProcessMutex | Redis Redlock / etcd |
| 配置中心 | ZK 节点存配置 | Nacos / Apollo / etcd |
| Leader 选举 | 临时节点 + Watcher | etcd / etcd-based K8s leaderelection |
| Kafka 元数据 | ZK 存储 | Kafka KRaft |

## 监控体系

### Prometheus zookeeper_exporter

```yaml
# docker-compose.yml 添加 exporter
zookeeper-exporter:
  image: dabealu/zookeeper-exporter:v0.1.9
  command:
    - --zk-hosts=zk1.internal:2181,zk2.internal:2181,zk3.internal:2181
    - --web.listen-address=:9141
    - --timeout=5
  ports:
    - "9141:9141"
```

Prometheus scrape 配置：

```yaml
scrape_configs:
  - job_name: 'zookeeper'
    static_configs:
      - targets:
          - zookeeper-exporter:9141
    relabel_configs:
      - source_labels: [__address__]
        target_label: instance
```

### 关键 Prometheus 指标

```promql
# 节点是否存活（1=正常，0=不可达）
zk_up

# 当前服务器角色（leader=2, follower=1, standalone=3）
zk_server_state

# 活跃连接数
zk_num_alive_connections

# 请求积压（持续 > 0 需告警）
zk_outstanding_requests

# 平均处理延迟（ms）
zk_avg_latency
zk_max_latency

# ZNode 数量（监控数据集增长）
zk_znode_count

# 活跃 Watcher 数量
zk_watch_count

# 临时节点数量
zk_ephemerals_count

# 内存数据集大小（字节）
zk_approximate_data_size

# Leader 视角：Follower 同步状态
zk_followers
zk_synced_followers  # 应等于 zk_followers
zk_pending_syncs     # 应为 0
```

### 告警规则

```yaml
# zookeeper-alerts.yaml
groups:
  - name: zookeeper.alerts
    rules:
      # 节点不可达
      - alert: ZookeeperNodeDown
        expr: zk_up == 0
        for: 1m
        labels:
          severity: critical
        annotations:
          summary: "Zookeeper 节点 {{ $labels.instance }} 不可达"

      # 集群失去 Quorum（3 节点集群中超过 1 个节点故障）
      - alert: ZookeeperQuorumLost
        expr: count(zk_up == 1) < 2
        for: 30s
        labels:
          severity: critical
        annotations:
          summary: "Zookeeper 集群可能失去 Quorum，当前存活节点: {{ $value }}"

      # Follower 与 Leader 不同步
      - alert: ZookeeperFollowerNotSynced
        expr: zk_synced_followers{} < zk_followers{}
        for: 2m
        labels:
          severity: warning
        annotations:
          summary: "Zookeeper 存在未同步的 Follower: synced={{ $value }}"

      # 请求积压
      - alert: ZookeeperOutstandingRequestsHigh
        expr: zk_outstanding_requests > 20
        for: 2m
        labels:
          severity: warning
        annotations:
          summary: "Zookeeper 请求积压: {{ $value }} 个未处理请求"

      # 连接数过高（接近 maxClientCnxns）
      - alert: ZookeeperConnectionsHigh
        expr: zk_num_alive_connections > 180
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "Zookeeper 连接数过高: {{ $value }}（上限 200）"

      # 平均延迟过高
      - alert: ZookeeperLatencyHigh
        expr: zk_avg_latency > 100
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "Zookeeper 处理延迟过高: {{ $value }}ms"

      # Watcher 数量异常（可能内存泄漏）
      - alert: ZookeeperWatcherCountHigh
        expr: zk_watch_count > 100000
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "Zookeeper Watcher 数量异常: {{ $value }}"
```

### Grafana Dashboard 配置

推荐导入 Grafana Dashboard ID `10465`（Zookeeper 3.x），重点面板：

1. **集群健康状态**：各节点 `zk_up` 和角色分布
2. **请求处理性能**：`zk_avg_latency` + `zk_max_latency` 趋势
3. **连接数监控**：`zk_num_alive_connections` 时序图
4. **数据集大小**：`zk_approximate_data_size` 增长趋势
5. **Follower 同步状态**：`zk_synced_followers` vs `zk_followers`

## 数据备份与迁移

### 快照备份

```bash
#!/bin/bash
# zk-backup.sh - 定期备份 Zookeeper 快照

BACKUP_DIR="/backup/zookeeper/$(date +%Y%m%d)"
DATA_DIR="/data/zookeeper/data/version-2"
TXLOG_DIR="/data/zookeeper/txlog/version-2"
S3_BUCKET="s3://my-backups/zookeeper"

mkdir -p "${BACKUP_DIR}"

# 备份最近的快照和对应的事务日志
ls -t "${DATA_DIR}"/snapshot.* | head -3 | xargs -I{} cp {} "${BACKUP_DIR}/"
ls -t "${TXLOG_DIR}"/log.* | head -10 | xargs -I{} cp {} "${BACKUP_DIR}/"

# 压缩并上传 S3
tar -czf "/tmp/zk-backup-$(date +%Y%m%d).tar.gz" "${BACKUP_DIR}"
aws s3 cp "/tmp/zk-backup-$(date +%Y%m%d).tar.gz" "${S3_BUCKET}/"

# 清理本地临时文件
rm -rf "${BACKUP_DIR}" "/tmp/zk-backup-$(date +%Y%m%d).tar.gz"

echo "备份完成: $(date)"
```

```bash
# crontab 每天凌晨 3 点执行备份
0 3 * * * /opt/scripts/zk-backup.sh >> /var/log/zk-backup.log 2>&1
```

### 数据迁移

```bash
# 场景：将 Zookeeper 数据从旧集群迁移到新集群
# 方法：使用 zkCopy 工具（比手动重放快照更安全）

# 安装 zkcopy
pip install kazoo

# Python 迁移脚本
python3 << 'EOF'
from kazoo.client import KazooClient
import sys

def copy_zk_tree(src_client, dst_client, path="/"):
    """递归复制 ZNode 树"""
    try:
        data, stat = src_client.get(path)
        # 在目标创建节点（跳过根节点）
        if path != "/":
            if not dst_client.exists(path):
                dst_client.create(path, data, makepath=True)
            else:
                dst_client.set(path, data)
        
        # 递归处理子节点
        children = src_client.get_children(path)
        for child in children:
            child_path = f"{path}/{child}" if path != "/" else f"/{child}"
            copy_zk_tree(src_client, dst_client, child_path)
    except Exception as e:
        print(f"Error copying {path}: {e}", file=sys.stderr)

src = KazooClient(hosts="old-zk1:2181,old-zk2:2181,old-zk3:2181")
dst = KazooClient(hosts="new-zk1:2181,new-zk2:2181,new-zk3:2181")

src.start()
dst.start()

# 只迁移需要的路径
for root_path in ["/kafka", "/dubbo", "/config"]:
    print(f"迁移: {root_path}")
    copy_zk_tree(src, dst, root_path)

src.stop()
dst.stop()
print("迁移完成")
EOF
```

## 云原生场景下的定位

### Zookeeper 的历史地位与现状

Zookeeper 在 2010 年代是分布式协调的首选方案，但随着生态演进，它在新系统中的使用逐渐减少：

**不推荐在新项目中引入 Zookeeper 的原因**：
1. **运维复杂**：需要维护独立的 JVM 集群，故障影响面广
2. **已有更好的替代**：etcd（更轻量，K8s 生态原生）、Nacos（服务发现+配置）
3. **Kafka 已去 ZK 化**：Kafka 4.0 彻底移除 ZK 依赖，新部署直接用 KRaft
4. **云厂商托管成本**：云上 Zookeeper 使用场景极少，独立维护性价比低

**仍值得投入的场景**：
1. **HBase 存量系统**：无法短期替换，需要保障 ZK 稳定性
2. **Dubbo 2.x 老服务**：升级到 Nacos 需要较长迁移周期
3. **大数据平台**（Hadoop/YARN）：生命周期与集群绑定

**运维建议**：
- 使用托管 ZooKeeper（如 AWS MSK 内置、阿里云 Kafka 版内置），减少自建运维成本
- 制定明确的迁移计划，新业务不引入 ZK 依赖
- 存量系统每季度检查一次：能否用 etcd/Nacos 替换

```bash
# 评估现有 ZK 依赖的快速方法
# 查看 ZK 中注册的服务列表
echo ls / | zkCli.sh -server zk1:2181 2>/dev/null | grep "^\["

# 典型输出
[zookeeper, kafka, dubbo, hadoop-ha, hbase]
# 逐一确认哪些可以替换，哪些强依赖
```

ZK 的设计思想今天看依然值得学，但在生产里该让位就让位。会运维、也敢推着存量系统"毕业"——这两件事一起做好，才算真把它玩明白。
