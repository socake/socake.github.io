---
title: "MongoDB 运维入门：部署、备份与生产性能调优"
date: 2026-04-11T13:00:00+08:00
draft: false
tags: ["MongoDB", "数据库", "运维", "NoSQL", "高可用"]
categories: ["数据库"]
description: "介绍 MongoDB 的适用场景选型、Replica Set 高可用部署、常用运维命令、索引管理、备份恢复策略以及生产性能调优经验。"
summary: "MongoDB 运维从选型到调优：何时选 MongoDB、Replica Set 三节点部署、索引设计、mongodump 备份，以及 wiredTiger、连接池、大文档等生产踩坑。"
toc: true
math: false
diagram: false
keywords: ["MongoDB", "Replica Set", "运维", "NoSQL", "索引", "mongodump", "wiredTiger", "K8s StatefulSet"]
params:
  reading_time: true
---

在关系型数据库大行其道的背景下，MongoDB 依然在特定场景里有不可替代的优势。本文从选型出发，介绍生产环境中 MongoDB 的部署、日常运维、性能调优，以及真实踩过的坑。

## 什么时候选 MongoDB

这是运维经常被问到的问题。MongoDB vs MySQL 不是优劣之争，是场景之分：

**选 MongoDB 的场景：**

- **文档结构多变**：用户画像、商品属性、配置项——不同记录的字段集合差异很大，频繁 ALTER TABLE 代价太高
- **嵌套/层次数据**：订单包含多个商品行，评论包含回复树——用嵌套文档比多表 JOIN 更自然
- **写多读少，且不强依赖事务**：埋点日志、行为轨迹、IoT 数据流——高吞吐写入
- **快速迭代的原型阶段**：schema-less 让早期不确定数据结构时开发更快
- **全文检索与地理位置查询**：MongoDB 内置文本索引和 2dsphere 索引

**坚守 MySQL 的场景：**

- 强事务、多表关联的金融账务
- 报表类复杂 SQL 聚合查询
- 数据关系高度规范化，外键约束强依赖

MongoDB 4.0+ 已经支持多文档 ACID 事务，但性能代价不小，真正依赖跨集合事务的场景还是用关系型数据库更合适。

---

## Replica Set 高可用部署

生产环境最低配置是三节点 Replica Set：一个 Primary、两个 Secondary。Primary 负责写入，Secondary 异步复制，Primary 宕机时 Secondary 自动选举新 Primary。

### K8s StatefulSet 部署

三节点 Replica Set 的核心 StatefulSet 配置：

```yaml
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: mongodb
  namespace: database
spec:
  serviceName: mongodb-headless
  replicas: 3
  selector:
    matchLabels:
      app: mongodb
  template:
    metadata:
      labels:
        app: mongodb
    spec:
      containers:
        - name: mongodb
          image: mongo:7.0
          ports:
            - containerPort: 27017
          command:
            - mongod
            - --replSet
            - rs0
            - --bind_ip_all
            - --wiredTigerCacheSizeGB
            - "1"           # 显式限制 cache，防止吃掉所有内存
          env:
            - name: MONGO_INITDB_ROOT_USERNAME
              valueFrom:
                secretKeyRef:
                  name: mongodb-secret
                  key: username
            - name: MONGO_INITDB_ROOT_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: mongodb-secret
                  key: password
          volumeMounts:
            - name: data
              mountPath: /data/db
          resources:
            requests:
              cpu: 500m
              memory: 1Gi
            limits:
              cpu: 2
              memory: 4Gi
          readinessProbe:
            exec:
              command:
                - mongosh
                - --eval
                - "db.adminCommand('ping')"
            initialDelaySeconds: 30
            periodSeconds: 10
  volumeClaimTemplates:
    - metadata:
        name: data
      spec:
        accessModes: ["ReadWriteOnce"]
        storageClassName: gp3
        resources:
          requests:
            storage: 100Gi
---
apiVersion: v1
kind: Service
metadata:
  name: mongodb-headless
  namespace: database
spec:
  clusterIP: None
  selector:
    app: mongodb
  ports:
    - port: 27017
```

### 初始化 Replica Set

StatefulSet 部署后需要手动初始化副本集（或用 init container 自动化）：

```javascript
// 连接到 mongodb-0 Pod
mongosh -u admin -p password

// 初始化
rs.initiate({
  _id: "rs0",
  members: [
    { _id: 0, host: "mongodb-0.mongodb-headless.database.svc:27017", priority: 2 },
    { _id: 1, host: "mongodb-1.mongodb-headless.database.svc:27017", priority: 1 },
    { _id: 2, host: "mongodb-2.mongodb-headless.database.svc:27017", priority: 1 },
  ]
})
```

`priority` 值越高越优先成为 Primary，把 Pod 0 设为首选 Primary 便于维护。

---

## 常用运维命令

### 查看副本集状态

```javascript
rs.status()
```

重点关注 `members` 数组里每个节点的 `stateStr`（PRIMARY/SECONDARY/ARBITER）和 `optimeDate`（复制进度）。Secondary 落后太多（`optimeLag` 很大）说明有复制延迟，可能是网络或 Primary 写入压力过大。

### 查看数据库统计

```javascript
use mydb
db.stats()
// 输出：dataSize（数据大小）、indexSize（索引大小）、storageSize（实际占用磁盘）

// 查看单个集合
db.orders.stats()
```

### 查看当前慢操作

```javascript
db.currentOp({ "secs_running": { "$gt": 5 } })
```

找到正在执行且超过 5 秒的操作，`opid` 字段可以用来强制终止：

```javascript
db.killOp(opid)
```

### 开启慢查询日志

```javascript
// 记录超过 100ms 的操作
db.setProfilingLevel(1, { slowms: 100 })

// 查看慢查询日志
db.system.profile.find().sort({ ts: -1 }).limit(10).pretty()
```

---

## 索引管理

### 创建索引

```javascript
// 单字段索引
db.orders.createIndex({ user_id: 1 })

// 复合索引（顺序很重要，遵循 ESR 原则：Equality > Sort > Range）
db.orders.createIndex({ user_id: 1, status: 1, created_at: -1 })

// 后台创建（不阻塞读写，MongoDB 4.2+ 默认在后台）
db.orders.createIndex({ product_id: 1 }, { background: true })

// 唯一索引
db.users.createIndex({ email: 1 }, { unique: true })

// TTL 索引（自动删除过期文档）
db.sessions.createIndex({ created_at: 1 }, { expireAfterSeconds: 86400 })
```

### 用 explain() 验证索引使用

```javascript
db.orders.find({
  user_id: "u123",
  status: "PAID",
}).sort({ created_at: -1 }).explain("executionStats")
```

重点看：
- `winningPlan.stage`：`IXSCAN` 表示用了索引，`COLLSCAN` 表示全集合扫描（需要优化）
- `executionStats.totalDocsExamined`：扫描文档数，越接近 `nReturned` 越好
- `executionStats.executionTimeMillis`：执行时间

### 查看和删除索引

```javascript
// 查看所有索引
db.orders.getIndexes()

// 删除指定索引
db.orders.dropIndex("user_id_1_status_1_created_at_-1")

// 找出未被使用的索引（MongoDB 4.4+）
db.orders.aggregate([
  { $indexStats: {} },
  { $match: { "accesses.ops": 0 } }
])
```

---

## 备份与恢复

### mongodump / mongorestore

```bash
# 备份整个实例（Replica Set 从 Secondary 备份，不影响 Primary）
mongodump \
  --uri="mongodb://admin:password@mongodb-0:27017/?authSource=admin&replicaSet=rs0" \
  --readPreference=secondary \
  --gzip \
  --archive=/backup/mongodb-$(date +%Y%m%d).gz

# 备份单个数据库
mongodump \
  --uri="mongodb://admin:password@mongodb-0:27017/mydb?authSource=admin" \
  --gzip \
  --archive=/backup/mydb-$(date +%Y%m%d).gz

# 恢复
mongorestore \
  --uri="mongodb://admin:password@mongodb-0:27017/?authSource=admin" \
  --gzip \
  --archive=/backup/mydb-20260411.gz \
  --nsInclude="mydb.*"
```

### 定时备份到 S3

```bash
#!/bin/bash
set -euo pipefail

DATE=$(date +%Y%m%d-%H%M%S)
BACKUP_FILE="/tmp/mongodb-${DATE}.gz"
S3_BUCKET="s3://my-backups/mongodb/"

mongodump \
  --uri="${MONGODB_URI}" \
  --readPreference=secondary \
  --gzip \
  --archive="${BACKUP_FILE}"

aws s3 cp "${BACKUP_FILE}" "${S3_BUCKET}"
rm -f "${BACKUP_FILE}"

# 删除 7 天前的备份
aws s3 ls "${S3_BUCKET}" | awk '{print $4}' | while read f; do
  file_date=$(echo "$f" | grep -oE '[0-9]{8}')
  if [[ $(date -d "$file_date" +%s) -lt $(date -d "7 days ago" +%s) ]]; then
    aws s3 rm "${S3_BUCKET}${f}"
  fi
done
```

### MongoDB Atlas 托管备份

使用 Atlas 时，连续备份（Continuous Backup）可以恢复到任意时间点（PIT Recovery），成本比自建备份管理低很多。对于不需要自托管的场景，Atlas 是更好的选择。

---

## 监控与告警

### mongodb-exporter + Prometheus

```bash
# 部署 percona mongodb_exporter
docker run -d \
  -p 9216:9216 \
  percona/mongodb_exporter:0.40 \
  --mongodb.uri="mongodb://monitor:password@mongodb:27017/?authSource=admin"
```

核心监控指标：

| 指标 | 含义 | 告警阈值参考 |
|------|------|-------------|
| `mongodb_rs_members_health` | 副本集成员健康状态 | == 0 立即告警 |
| `mongodb_ss_opcounters` | 各操作类型 QPS | 突增 >2x 基线 |
| `mongodb_ss_connections{state="current"}` | 当前连接数 | >80% max |
| `mongodb_ss_wiredTiger_cache_bytes_currently_in_cache` | WiredTiger cache 用量 | >90% 限制值 |
| `mongodb_ss_repl_lag` | 复制延迟（秒） | >30s 告警 |

---

## 踩坑记录

**wiredTiger cache 设置**

WiredTiger 默认使用系统内存的 50%（减去 1GB）作为 cache。在 K8s 里，如果不设置 `--wiredTigerCacheSizeGB`，MongoDB 读取的是宿主机内存（不是容器 limit），会分配远超容器限制的 cache，导致 OOM 被强制 kill。部署时一定要显式设置，通常设为容器内存 limit 的 50-60%。

**连接池耗尽**

Python 应用用 `pymongo` 时，`MongoClient` 默认连接池大小是 100。高并发下如果业务代码每次请求都 `new MongoClient()`（常见错误），会瞬间耗尽连接数，导致 MongoDB 侧 `too many open connections`。`MongoClient` 要作为全局单例复用，并根据应用并发量调整 `maxPoolSize`：

```python
from pymongo import MongoClient

client = MongoClient(
    "mongodb://admin:password@mongodb:27017/",
    maxPoolSize=50,
    minPoolSize=5,
    serverSelectionTimeoutMS=5000,
)
db = client["mydb"]
```

**大文档影响性能**

MongoDB 单文档最大 16MB。实践中遇到过把二进制文件（图片、PDF）直接存入文档的情况，导致：
- 查询返回大文档时网络传输慢
- WiredTiger cache 被大文档占满，有效 cache 利用率下降
- 复制延迟变大（大文档 oplog 体积大）

正确做法：二进制数据存 S3/OSS，MongoDB 只存 URL 和元数据。单文档超过 1MB 就要考虑是否设计合理。

**Replica Set 脑裂**

三节点中有节点短暂网络隔离时，Replica Set 会重新选举。如果网络恢复后出现两个节点都认为自己是 Primary（实际不会，因为需要多数票），或者 Primary 因为无法写入 majority 而降级。应用层的 MongoClient 要配置 `readPreference=primaryPreferred` 并做好重连逻辑，不要假设连接永远稳定。
