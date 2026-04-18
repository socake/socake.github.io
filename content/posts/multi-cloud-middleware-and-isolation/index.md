---
title: "多云中间件横向速查与跨环境隔离实战"
date: 2026-04-18T13:00:00+08:00
draft: false
tags: ["AWS", "阿里云", "中间件", "RabbitMQ", "Kafka", "Aurora", "Redis", "Valkey", "运维实战"]
categories: ["云原生"]
series: ["云中间件实战"]
description: "AWS 与阿里云中间件全栈横向对照（数据库/缓存/MQ/搜索/配置中心/容器），跨环境隔离 7 条强制 checklist，高频运维操作命令速查，覆盖资深运维在多云环境下最容易踩的坑。"
summary: "做多云运维最容易的事就是把 AWS 那套思维原样搬到阿里云，然后在某次故障里发现选型完全错位。本文整理了一份 AWS↔阿里云中间件横向对照表，附上跨环境隔离强制 checklist 和高频运维命令速查，是我自己工作中反复回查的一份速记。"
toc: true
math: false
diagram: false
keywords: ["AWS 阿里云对照", "中间件选型", "Aurora", "PolarDB", "ElastiCache", "Redis Tair", "MSK", "RocketMQ", "Amazon MQ", "RabbitMQ vhost", "MSE Nacos", "EKS ACK", "跨环境隔离", "数据混用", "consumer group", "auto increment", "运维命令"]
params:
  reading_time: true
---

做多云运维最容易的事就是把 AWS 那套思维原样搬到阿里云，然后在某次故障里发现选型完全错位——`mq.t3.micro` 不支持 RabbitMQ、ElastiCache Replication Group 默认 user 不是 `admin`、PolarDB 没有 Backtrack 但有"按时间点克隆"……

这些细节没写在任何官方对照文档里，但每次撞上都会浪费几小时。本文是我自己反复回查的一份速记，分四部分：

1. **AWS ↔ 阿里云中间件横向对照**（数据库 / 缓存 / MQ / 搜索 / 对象存储 / 配置中心 / 容器 / 监控）
2. **跨环境隔离 7 条强制 checklist**（新环境上线必走，否则一定会踩"撞 ID 数据混用"这种事故）
3. **高频运维操作命令速查**（AWS CLI / aliyun CLI / kubectl / mysql / RabbitMQ Management API）
4. **多云常见坑速记**

---

## 一、AWS ↔ 阿里云中间件横向对照

### 1. 关系数据库

| 维度 | AWS | 阿里云 |
|---|---|---|
| MySQL 兼容云原生数据库 | **Aurora MySQL** | **PolarDB MySQL** |
| 计费 | 按 instance-hour（Provisioned）或 ACU-second（Serverless v2）| 按节点规格 + 存储；PolarDB 也有 Serverless |
| 写副本 | 1 writer + 0~15 reader（同一份共享存储） | 1 主节点 + 0~15 只读节点（同一份共享存储 PolarFS） |
| 时光倒流 | **Backtrack**（72h 内整集群级 in-place 回滚） | **PolarDB 没有 Backtrack**，但有"按时间点克隆"（克隆出新集群） |
| Point-In-Time Recovery | 是（恢复到新集群） | 是（恢复到新集群） |
| Binlog 订阅 | DMS / Debezium 直接订阅 binlog | DTS / 数据传输服务，或开放 binlog 自己订阅 |
| 标准 PG | RDS PostgreSQL | RDS PostgreSQL / PolarDB PostgreSQL |

**关键差异**：
- Aurora 的 Backtrack 是 in-place（不重建集群），但**整 cluster 级别**——恢复点之后所有库的所有写入都会丢，慎用
- PolarDB 没 Backtrack，但克隆速度极快（共享存储 metadata 只复制 indirect 指针），几分钟出一个新集群
- **两者都不要把"备份"和"恢复点"混淆**：每天的 snapshot 是定时点，业务回滚还是要靠 binlog

### 2. 缓存（Redis / Valkey）

| 维度 | AWS | 阿里云 |
|---|---|---|
| 标准产品 | **ElastiCache for Valkey / Redis** | **Tair**（Redis 增强版）/ **Redis 标准版** |
| Serverless | 是（按 ECPU 计费，最低 1GB-hour storage ≈ $90/月）| 是（按容量计费，弹性扩容） |
| 集群模式 | Cluster mode enabled（分片）/ disabled（单 master + replica） | 集群版（分片）/ 标准版（主从） |
| TLS / AUTH | Replication Group 才支持，单节点 Cache Cluster 不支持 | 全产品默认支持 TLS，AUTH token 可选 |
| 多 user RBAC | ElastiCache User Group（Replication Group 模式） | Tair 支持 ACL 多账号 |
| 默认 user | **`default`**（不是 `admin`！）| `default` |

**关键坑**：
- ElastiCache 用 `create-replication-group` 才能开 TLS+AUTH，单纯 `create-cache-cluster` 是裸 TCP，没有加密
- 客户端连 Replication Group 的 default user 时，**username 字段必须是 `default`，写 `admin` 会 `WRONGPASS`**——很多人(包括我)第一次都栽
- 阿里云 Tair 比 Redis 标准版多了向量、Stream、Bloom 等扩展，但贵，不是必须不用
- **Serverless storage 1GB-hour 起步**：AWS ElastiCache Serverless 哪怕完全闲置，月费也 ~$90 起，比 cache.t4g.micro（$11/月）贵 8 倍。低流量场景一定别上 Serverless

### 3. 消息队列（中间件大坑区）

#### 3.1 RabbitMQ

| 维度 | AWS | 阿里云 |
|---|---|---|
| 产品 | **Amazon MQ for RabbitMQ** | **消息队列 RabbitMQ 版**（也有 Serverless 模式）|
| 最小实例 | **mq.m7g.medium**（$71/月起，**不支持 t3 系列！**）| 按容量 / TPS 计费，更灵活 |
| User 管理 | 必须在 RabbitMQ web console 内部管理，**AWS API 不暴露**！| 控制台 + API 都能管 |
| Vhost 管理 | 必须用 management user 调 management API | 控制台直接建 |

**关键坑**：
- AWS Amazon MQ for RabbitMQ 的 user 管理**没有 AWS API**——broker 创建时填的初始 admin 凭据丢了的话，**只能 reboot broker 重置**或者删 broker 重建。这是 ActiveMQ broker 没有的限制
- RabbitMQ 业务 user 通常**不是 management user**，调 `/api/vhosts` 返回 `Not management user` 401 是常态。需要专门给 user 打 `administrator` tag
- Vhost 切换时，旧 vhost 上的队列**不会自动迁移**，未消费的消息会变孤儿——切流时双消费者并行消费完旧队列再下线
- Amazon MQ for RabbitMQ 的 broker-level policy 不会自动应用到新建 vhost，新 vhost 要手动补 policy

#### 3.2 Kafka

| 维度 | AWS | 阿里云 |
|---|---|---|
| 托管 Kafka | **Amazon MSK**（Provisioned / Serverless）| **消息队列 Kafka 版** |
| 阿里云独家 | — | **RocketMQ**（自家协议，5.0 支持 OpenMessaging） |
| 认证 | AWS_MSK_IAM（无需密码）/ SASL/SCRAM / mTLS | SASL_PLAIN / SASL_SCRAM |
| 计费起点 | Serverless 固定 $540/月 起；Provisioned 2 × kafka.t3.small ~$75/月 | 按存储 + 流量计费，更细 |

**关键坑**：
- MSK Serverless 最低消费 $540/月，**测试环境绝对不要用 Serverless**——非要用 Kafka 就用 Provisioned 2 × kafka.t3.small（约 $75/月）
- AWS_MSK_IAM 认证时，sarama 客户端要用 IAM token provider，**bootstrap servers 用逗号分隔的列表，不要用单字符串**——Sarama 这块文档不清晰，第一次配很容易格式错
- **Consumer Group 改名 = 新 group 没 offset**，按 `auto.offset.reset` 决定从哪开始消费：
  - `earliest`：重复消费历史（适合幂等业务）
  - `latest`：丢窗口期内的在途消息（适合实时业务）
  - 想要"零丢零重"：用 `kafka-consumer-groups.sh --reset-offsets --to-group <old> --to-group-new <new>` 把旧 group 的 committed offset 复制到新 group
- 用 RocketMQ 的话，注意它的"消息组"（MessageGroup）跟 Kafka consumer group 不是一回事——RocketMQ 是顺序消费维度，不是消费分组

### 4. 搜索引擎

| 维度 | AWS | 阿里云 |
|---|---|---|
| 产品 | **OpenSearch Serverless (AOSS)** / OpenSearch Service | **阿里云 Elasticsearch** / **OpenSearch** |
| 计费 | AOSS 按 OCU（最少 2 OCU，~$700/月起）| ES 按节点计费，可弹性 |
| 全托管日志 | 一般用 OpenSearch + Loki 配合 | **SLS（日志服务）**——阿里云独家神器，免运维，按量计费 |

**关键差异**：
- AWS AOSS 闲置 collection 也要 ~$700/月起步（2 OCU 最低消费），**绝对别留闲置 collection**
- 阿里云 SLS 是日志领域的"无敌存在"——你不用维护 ES 集群，按存储 + 查询计费，监控告警 + 仪表盘 + 投递 + 加工 一站式。如果是阿里云为主的环境，强烈推荐 SLS over 自建 Loki

### 5. 对象存储

| 维度 | AWS | 阿里云 |
|---|---|---|
| 产品 | **S3** | **OSS** |
| 协议 | S3 API 是事实标准 | OSS 兼容 S3 API（少数 header 差异）|
| 最便宜 tier | S3 Glacier Instant Retrieval | OSS 归档 / 冷归档 |
| 跨云访问 | rclone / s3-compatible 客户端两边都通 | 同上 |

阿里云 OSS 几乎所有 S3 SDK 都能用，但有几个小坑：
- OSS 对 `?uploadId` 等 query string 的 multipart 上传 URL 编码处理跟 S3 略有差异
- bucket 命名规则更严（不能有大写字母）

### 6. 配置中心 / 服务注册

| 维度 | AWS | 阿里云 |
|---|---|---|
| 主流方案 | AppConfig / Parameter Store / 自建 etcd | **MSE Nacos**（托管 Nacos，Java/Spring Cloud 生态主选） |
| 服务注册 | Cloud Map | MSE Nacos / MSE Zookeeper |

**用 Spring Cloud / 自家 Go 服务的，绝大部分用 Nacos**——它配置中心 + 服务注册一体化，比单独 etcd 灵活。AWS 上一般也是自建 Nacos pod 部署在 EKS 集群里。

MSE Nacos 几个坑：
- Namespace 隔离严格，跨 namespace 配置完全独立
- 客户端缓存路径：Go 在 `/tmp/nacos/`，Python 在 `/tmp/nacos-cache/`，排障时进 pod cat 这些缓存确认应用拿到了什么
- 配置 type 选错（YAML/TOML 写成 Properties）会导致客户端解析失败——发布时一定要选对 type

### 7. 容器服务

| 维度 | AWS | 阿里云 |
|---|---|---|
| 托管 K8s | **EKS** | **ACK**（容器服务 Kubernetes） |
| Serverless 容器 | EKS Fargate | ACK Serverless 集群 / ECI（弹性容器实例） |
| 节点自动扩缩 | **Karpenter** / Cluster Autoscaler | ACK 弹性节点池 + Cluster Autoscaler |
| 控制面 | EKS 控制面 $0.10/h（$73/月）每集群 | ACK Pro 版控制面 ~¥640/月 |

**关键差异**：
- Karpenter 是 AWS 自研的下一代节点扩缩，比 cluster-autoscaler 快得多（直接调 EC2 API 而不是改 ASG），强烈推荐替换
- 阿里云 ACK 控制面比 EKS 便宜，且免费版可用（无 SLA），适合非关键集群
- ECI 是阿里云"按 pod 分钟付费"的极致 Serverless，适合突发批处理

### 8. 监控可观测

| 维度 | AWS | 阿里云 |
|---|---|---|
| 指标 | CloudWatch | ARMS / 云监控 |
| 日志 | CloudWatch Logs / OpenSearch | **SLS（无敌存在）** |
| Tracing | X-Ray | ARMS Trace |
| 推荐自建栈 | Prometheus + Loki + Tempo + Grafana | 同左（OSS 通用） |

CloudWatch 又贵又慢，多数公司在 EKS 上自建 Prometheus + Loki。但**桥接 CloudWatch → Prometheus 必备一个工具**：

- **YACE**（yet-another-cloudwatch-exporter）：从 CloudWatch 拉指标转成 Prometheus 格式，支持 ALB、RDS、ElastiCache、SQS 等几十种 AWS 服务

阿里云这边**强烈推荐用 SLS**——它的功能远超 CloudWatch Logs：内置 SQL 查询、机器学习异常检测、定时投递、告警一站式，比自建 Loki 省心 5 倍。

---

## 二、跨环境隔离 7 条强制 checklist

新建任何子环境（staging / pilot / 临时压测 / 实验环境）都必须走这 7 条。**少一条都可能在某天爆出"测试环境串数据"事故**：

### 1. 数据库
- [ ] **独立 RDS / Aurora cluster**（不要共用 schema 后缀做"软隔离"——一旦代码 hardcode 库名前缀就跨写）
- [ ] **独立账号 + REVOKE 跨库权限**（admin 账号能 SELECT 所有库，必须收回）
- [ ] **所有自增表 `AUTO_INCREMENT >= 千万级别`** 或起点 ≥ 现有任一环境 max_id × 2

> 自增 ID 起点是个隐藏陷阱：新环境 `m_xxx.id` 从 1 自增，跟老环境老数据撞 ID。如果业务消息通过共享 MQ 广播，对端环境写自己库时按 ID 直接写到老项目下面，瞬间变成"老项目里冒出陌生消息"。

### 2. 消息中间件
- [ ] **独立 RabbitMQ broker**（最低限度独立 vhost，但 broker 共享时 management policy 不会自动隔离，建议直接独立 broker）
- [ ] **独立 Kafka cluster** 或至少独立 topic 命名空间 + 独立 consumer group
- [ ] **独立 Valkey/Redis 实例**（cache.t4g.micro $11/月起，比共用一个实例后续清洗成本低多了）

### 3. 应用层（代码侧）
- [ ] **dispatch / consumer 必须按 env 字段严格过滤**跨环境消息，丢弃不属于本环境的（这是最后一道防线）
- [ ] **Cache key 必须带 env 前缀**（如 `prod:user:123` / `staging:user:123`），不依赖中间件物理隔离

### 4. 配置中心
- [ ] **独立 namespace**（如 Nacos 的 `staging` namespace，跟 prod 完全独立）
- [ ] **配置必须包含完整 section**（不能缺关键 section，缺了代码 fallback 行为不可控，可能调到错的环境）

### 5. 部署侧
- [ ] kustomization / Helm values 必须显式 patch `env=<新环境名>`
- [ ] K8s 标签 `app.kubernetes.io/part-of=<env>`

### 6. 上线前验证
- [ ] 跑自动化 checklist 脚本，逐条不通过禁止上线（强烈建议沉淀成 CI gate）

### 7. 上线后 24 小时观察
- [ ] SQL 巡检：邻接环境的消息表是否有 project_id / 主键 落到对端环境老数据区间
- [ ] 检查 RabbitMQ Management UI：vhost 内队列名是否带 env 后缀
- [ ] Kafka consumer-group 列表确认 group 名带 env

---

## 三、高频运维操作命令速查

### 1. AWS CLI

```bash
# 列所有 RDS Aurora cluster
aws rds describe-db-clusters --region <region> \
  --query 'DBClusters[].[DBClusterIdentifier,Engine,Status,ClusterCreateTime]' \
  --output table

# 列所有 ElastiCache（Serverless + Replication Group + Cache Cluster）
aws elasticache describe-serverless-caches --region <region>
aws elasticache describe-replication-groups --region <region>
aws elasticache describe-cache-clusters --region <region>

# 列所有 MSK 集群（含 Provisioned + Serverless）
aws kafka list-clusters-v2 --region <region>

# 列所有 Amazon MQ broker
aws mq list-brokers --region <region>
aws mq describe-broker --broker-id <id> --region <region>

# CloudTrail 查 broker 创建事件（找初始 admin user 名）
aws cloudtrail lookup-events --region <region> \
  --lookup-attributes AttributeKey=EventName,AttributeValue=CreateBroker \
  --max-results 10

# 创建独立 ElastiCache Replication Group（带 TLS+AUTH）
aws elasticache create-replication-group --region <region> \
  --replication-group-id <name> \
  --engine valkey --engine-version 8.0 \
  --cache-node-type cache.t4g.micro --num-cache-clusters 1 \
  --cache-subnet-group-name <subnet-group> \
  --security-group-ids <sg-id> \
  --transit-encryption-enabled --auth-token "<password>" \
  --port 6379

# 创建 Amazon MQ for RabbitMQ broker
aws mq create-broker --region <region> \
  --broker-name <name> --engine-type RabbitMQ --engine-version 3.13 \
  --host-instance-type mq.m7g.medium \
  --deployment-mode SINGLE_INSTANCE --no-publicly-accessible \
  --subnet-ids <subnet-id> --security-groups <sg-id> \
  --users '[{"Username":"admin","Password":"<pwd>","ConsoleAccess":true}]'
```

### 2. aliyun CLI（阿里云）

```bash
# 列所有 RDS 实例
aliyun rds DescribeDBInstances --RegionId <region>

# 列 PolarDB 集群
aliyun polardb DescribeDBClusters --RegionId <region>

# 列 MSE Nacos 实例
aliyun mse ListClusters --PageNum 1 --PageSize 50

# 列 MSE Nacos namespaces（同一个 Nacos 实例下的所有 namespace）
aliyun mse ListEngineNamespaces --InstanceId <mse_instance_id>

# 拉取 Nacos 配置内容
aliyun mse GetNacosConfig --InstanceId <mse_id> \
  --NamespaceId <ns> --Group <group> --DataId <dataid>

# 更新 Nacos 配置
aliyun mse UpdateNacosConfig --InstanceId <mse_id> \
  --NamespaceId <ns> --Group <group> --DataId <dataid> \
  --Type yaml --Content "$(cat config.yaml)"
```

### 3. kubectl（多集群常用）

```bash
# 看所有 context（多集群环境必备）
kubectl config get-contexts

# 切 context（不要忘了带 -n namespace）
kubectl --context <ctx> -n <ns> ...

# 滚动重启所有匹配 label 的 deployment
kubectl --context <ctx> -n <ns> rollout restart deploy --selector=app.kubernetes.io/part-of=<your-app>

# 起一个临时跳板 pod（用 alpine + curl 调内网 API）
kubectl --context <ctx> -n <ns> run probe \
  --image=curlimages/curl:latest --restart=Never \
  --command -- sleep 600

# 起 mysql 客户端 pod 跳板查 RDS
kubectl --context <ctx> -n <ns> run mysql-probe \
  --image=mysql:8 --restart=Never \
  --command -- sleep infinity

# 看 pod 拉取的 Nacos 配置缓存
kubectl --context <ctx> -n <ns> exec <pod> -- cat /tmp/nacos/cache/config/<dataid>@@<group>@@<namespace>
```

### 4. mysql / Aurora 长事务排查

```sql
-- 查所有运行中的事务（找长事务 / 死锁源头）
SELECT trx_id, trx_mysql_thread_id, trx_started, trx_state,
       trx_rows_locked, LEFT(trx_query, 100) AS query
FROM information_schema.innodb_trx
WHERE TIMESTAMPDIFF(SECOND, trx_started, NOW()) > 5;

-- KILL 僵尸事务（用上面查到的 trx_mysql_thread_id）
KILL <thread_id>;

-- 查锁等待
SELECT * FROM performance_schema.data_lock_waits;

-- 大表分批 DELETE（CTAS 备份 → 临时表中转 → 1000-5000/批）
CREATE TABLE main_bak_20260418 AS SELECT * FROM main WHERE <condition>;
ALTER TABLE main_bak_20260418 ADD PRIMARY KEY (id);  -- ★ 必加，否则 JOIN 全表扫死锁

DELIMITER //
CREATE PROCEDURE batch_del()
BEGIN
  DECLARE deleted INT DEFAULT 1;
  WHILE deleted > 0 DO
    DROP TEMPORARY TABLE IF EXISTS tmp_ids;
    CREATE TEMPORARY TABLE tmp_ids (id BIGINT PRIMARY KEY) AS
      SELECT id FROM main_bak_20260418 ORDER BY id LIMIT 1000;
    DELETE m FROM main m INNER JOIN tmp_ids t ON m.id = t.id;
    DELETE bak FROM main_bak_20260418 bak INNER JOIN tmp_ids t ON bak.id = t.id;
    SET deleted = ROW_COUNT();
  END WHILE;
END//
DELIMITER ;
CALL batch_del();
DROP PROCEDURE batch_del;
```

### 5. RabbitMQ Management API（admin user 必备）

```bash
# 看 broker 上所有 user 的 tags
curl -s -u 'admin:<pwd>' https://<broker>.mq.<region>.on.aws/api/users | jq '.[] | {name, tags}'

# 列 vhosts
curl -s -u 'admin:<pwd>' https://<broker>/api/vhosts | jq '.[].name'

# 创建 vhost
curl -X PUT -u 'admin:<pwd>' -H 'Content-Type: application/json' \
  https://<broker>/api/vhosts/<new_vhost> -d '{}'

# 给 user 授权 vhost（read/write/configure all）
curl -X PUT -u 'admin:<pwd>' -H 'Content-Type: application/json' \
  https://<broker>/api/permissions/<vhost>/<user> \
  -d '{"configure":".*","write":".*","read":".*"}'

# 看 vhost 内所有 queue + 消费者数
curl -s -u 'admin:<pwd>' \
  'https://<broker>/api/queues/<vhost>?columns=name,consumers,messages,message_stats.publish' \
  | jq '.[] | {name, consumers, messages}'
```

### 6. Kafka 命令（kafka-cli 跳板 pod）

```bash
# 列 topic
kafka-topics --list --bootstrap-server $BROKER --command-config /tmp/client.properties

# 看 consumer group + offset
kafka-consumer-groups --bootstrap-server $BROKER --command-config /tmp/client.properties \
  --describe --group <group_name>

# 复制旧 group offset 到新 group（改 group 名时零丢零重的关键）
kafka-consumer-groups --bootstrap-server $BROKER --command-config /tmp/client.properties \
  --reset-offsets --to-group <old_group> --to-group-new <new_group> --execute --all-topics
```

### 7. Loki / 日志查询

```bash
# 跨集群查日志（自己写个 wrapper 调 logcli 即可）
logcli --addr=<loki-url> query \
  '{namespace="myapp", app="backend"} |= "ERROR"' --since=1h

# Loki LogQL 常用
{namespace="x"} |= "keyword"               # 含关键词
{namespace="x"} |~ "regex.*pattern"        # 正则
{namespace="x"} | json | level="error"     # 解析 JSON 字段
sum by (level) (rate({namespace="x"}[5m])) # 按 level 聚合
```

---

## 四、多云常见坑速记

| 坑 | 现象 | 规避 |
|---|---|---|
| ElastiCache Replication Group user 是 `default` | 应用配 `username=admin` 报 `WRONGPASS` | 配置改 `default`；或者用 RBAC user group 显式建 `admin` user |
| ElastiCache Serverless 闲置也要 $90/月起 | 月底账单看到莫名的 storage 费用 | 低流量场景一定用 node-based cache.t4g.micro |
| RabbitMQ broker user 不是 management user | 调 `/api/vhosts` 401 | broker 创建时填的初始 admin 才是 management user，记得保存密码（AWS API 不可重置） |
| Amazon MQ for RabbitMQ 不支持 t3 实例 | `mq.t3.micro` create 报错 | 最便宜 mq.m7g.medium ($71/月) |
| MSK Serverless 最低消费 $540/月/集群 | 测试环境账单爆炸 | 测试环境一定用 Provisioned 2 × kafka.t3.small |
| AOSS 闲置 collection $700/月起 | 同上 | 不用就删 collection，别留闲置 |
| Aurora Backtrack 是整集群级 | "我只想恢复一张表" 但所有库都 in-place 回滚 | 单表恢复用 PITR 到新集群 |
| MySQL 大表 DELETE 分批必须用临时表中转 | DELETE INNER JOIN 子查询自锁，Lock wait timeout | 临时表加 PK，`LIMIT 1000` 一批 |
| MySQL 僵尸事务长期持锁 | OOM Killed 客户端后事务没回滚，新事务全等锁 | `SHOW innodb_trx` 找出 + `KILL <thread_id>` |
| Nacos 配置 type 选错 | Pod 启动后无法解析配置 | 发布时显式选 yaml/toml/properties |
| Kafka consumer_group 改名丢消息 | 切换瞬间在途消息消失 | 用 `kafka-consumer-groups --reset-offsets --to-group` 复制 offset |
| ID 自增起点撞车 | 新建库 `id` 从 1 起，跟老库老数据撞 | 新库 `ALTER TABLE ... AUTO_INCREMENT=10000000` |
| EKS 跨集群 svc DNS 解析不到 | 应用拿到对端集群的 pod 名后调用失败 | 服务发现 cache 必须按集群 / env 隔离 |

---

## 五、结语

多云不是把 AWS 那套抄到阿里云，更不是反过来。两边各有自己的"最佳实践"和"陷阱"，但**有一条是通用的**：

> **新环境上线时，宁可多花一份独立中间件的钱，也不要省钱让两个环境共享。** 数据混用事故的清洗成本远高于多起一个 broker 的 $80/月。

PRE 环境就是反例的反例：因为它从一开始就独立 RDS、独立 broker、独立 Kafka、独立 Valkey、id 起点高位，所以从来没出过跨环境数据问题。其他环境都是因为"复用现有资源省钱"埋下的雷，最后某天爆炸。

工具上保持一份这种速查清单，故障来的时候能快速翻到对应章节，比从头查文档快 10 倍。

