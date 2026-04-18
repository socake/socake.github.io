---
title: "RabbitMQ 运维实战：集群部署、消费者可靠性与监控体系"
date: 2025-04-22T14:30:00+08:00
draft: false
tags: ["RabbitMQ", "消息队列", "运维", "中间件"]
categories: ["中间件"]
description: "覆盖 RabbitMQ 生产环境全链路运维：3节点 Quorum Queue 集群搭建、Exchange/Queue 核心概念、消费者 ACK 可靠性、死信队列、Prometheus 监控告警与常见故障处理"
summary: "系统梳理 RabbitMQ 运维核心技能：Quorum Queue 集群部署与镜像队列对比、生产配置调优、消费者 prefetch 与死信队列配置、基于 Management API 和 rabbitmq_exporter 的监控体系，以及消息堆积、脑裂等常见故障的处理方案。"
toc: true
math: false
diagram: false
keywords: ["RabbitMQ", "Quorum Queue", "死信队列", "DLX", "prefetch", "rabbitmq_exporter", "消息堆积", "脑裂"]
params:
  reading_time: true
---

我们这边 RabbitMQ 扛着 AI 任务调度、异步通知、工单流转几条主链路。从部署到踩坑，Exchange、持久化、ACK、脑裂，每一块都能单独讲半天，这里把几年运维的经验整理下来。

## 集群部署：3 节点 Quorum Queue

### 节点规划

生产环境推荐 3 节点奇数集群，满足 Quorum Queue 的多数派写入要求（3 节点可容忍 1 节点故障）。

```
节点规划示例：
rabbit@mq-node1  10.0.1.11   磁盘节点（disc）
rabbit@mq-node2  10.0.1.12   磁盘节点（disc）
rabbit@mq-node3  10.0.1.13   磁盘节点（disc）
```

RabbitMQ 支持磁盘节点（disc）和内存节点（ram）两种类型。生产环境全部使用磁盘节点，内存节点重启后元数据丢失，风险高。

### 配置 /etc/hosts 和 Erlang Cookie

RabbitMQ 集群基于 Erlang 分布式，节点间通过 Erlang Cookie 认证。所有节点的 Cookie 必须一致。

```bash
# 在所有节点执行，确保主机名互相解析
cat >> /etc/hosts << EOF
10.0.1.11 mq-node1
10.0.1.12 mq-node2
10.0.1.13 mq-node3
EOF

# 统一 Erlang Cookie（所有节点相同）
echo "RABBITMQ_ERLANG_COOKIE_STRING" > /var/lib/rabbitmq/.erlang.cookie
chown rabbitmq:rabbitmq /var/lib/rabbitmq/.erlang.cookie
chmod 400 /var/lib/rabbitmq/.erlang.cookie
```

### rabbitmq.conf 基础配置

```ini
# /etc/rabbitmq/rabbitmq.conf

# 节点名称（每个节点不同）
# node 名称通过环境变量 RABBITMQ_NODENAME 或 systemd 设置

# 监听配置
listeners.tcp.default = 5672
management.tcp.port = 15672

# 集群分区策略（pause_minority 是 Quorum Queue 的推荐策略）
cluster_partition_handling = pause_minority

# 日志级别
log.console = true
log.console.level = info
log.file = /var/log/rabbitmq/rabbit.log
log.file.level = info
```

### 加入集群

在 node2、node3 上执行：

```bash
# 停止 RabbitMQ 应用（Erlang 节点保持运行）
rabbitmqctl stop_app

# 重置本节点状态
rabbitmqctl reset

# 加入集群
rabbitmqctl join_cluster rabbit@mq-node1

# 启动 RabbitMQ 应用
rabbitmqctl start_app

# 验证集群状态
rabbitmqctl cluster_status
```

输出示例：
```
Cluster status of node rabbit@mq-node2 ...
Basics
Cluster name: rabbit@mq-node1
Total CPU cores available: 8

Disk Nodes
rabbit@mq-node1
rabbit@mq-node2
rabbit@mq-node3

Running Nodes
rabbit@mq-node1
rabbit@mq-node2
rabbit@mq-node3
```

### 镜像队列 vs Quorum Queue

从 RabbitMQ 3.8 起，官方推荐用 **Quorum Queue** 替代经典镜像队列（Classic Mirrored Queue）。

| 特性 | 经典镜像队列 | Quorum Queue |
|------|------------|-------------|
| 复制机制 | 异步镜像，可能丢消息 | Raft 共识，强一致 |
| 故障恢复 | 手动同步，可能数据不一致 | 自动，多数派即可服务 |
| 性能 | 较高（异步写） | 略低（同步写多数派）|
| 支持版本 | 3.x（已 deprecated） | 3.8+（推荐）|
| 消息持久化 | 可选 | 强制持久化 |
| 死信队列 | 支持 | 支持 |
| 优先级队列 | 支持 | 不支持 |

**创建 Quorum Queue 示例**

```bash
# 通过 CLI 创建
rabbitmqadmin declare queue \
  name=task.queue \
  durable=true \
  arguments='{"x-queue-type":"quorum"}'
```

通过 AMQP 客户端创建（Python 示例）：

```python
channel.queue_declare(
    queue='task.queue',
    durable=True,
    arguments={
        'x-queue-type': 'quorum',
        # 初始副本数（默认等于集群节点数，最多 5）
        'x-quorum-initial-group-size': 3,
    }
)
```

---

## 核心概念

### Exchange 类型

RabbitMQ 消息路由通过 Exchange 完成，生产者发消息到 Exchange，Exchange 根据绑定规则路由到队列。

**Direct Exchange**

精确匹配 Routing Key，一对一路由：

```
Producer → Exchange(direct) → [routing_key=order.created] → Queue(order-processor)
```

适合：任务分发、特定业务事件通知。

**Fanout Exchange**

忽略 Routing Key，广播到所有绑定队列：

```
Producer → Exchange(fanout) → Queue(service-a)
                             → Queue(service-b)
                             → Queue(service-c)
```

适合：事件广播、缓存失效通知、审计日志。

**Topic Exchange**

支持通配符匹配：`*` 匹配一个词，`#` 匹配零或多个词：

```
Routing Key: user.order.created

绑定模式 user.#       → 匹配
绑定模式 *.order.*    → 匹配
绑定模式 user.order   → 不匹配（缺少第三段）
```

适合：按业务域路由，不同服务订阅不同模式。

**Headers Exchange**

根据消息 Header 属性匹配，不依赖 Routing Key。实际生产中较少使用，性能也比其他类型差。

### Queue 类型

| 类型 | 说明 | 适用场景 |
|------|------|--------|
| Classic | 默认类型，单节点或镜像 | 开发测试、非关键业务 |
| Quorum | Raft 共识，强一致 | **生产环境推荐** |
| Stream | 持久化流，类似 Kafka | 需要重放消息、多消费者读同一流 |

### Vhost 隔离

Vhost（Virtual Host）类似数据库的 Schema，提供命名空间隔离。不同 Vhost 之间的 Exchange、Queue、Binding 完全隔离，用户权限也可以按 Vhost 控制。

```bash
# 创建 Vhost
rabbitmqctl add_vhost /production
rabbitmqctl add_vhost /staging

# 创建用户并授权
rabbitmqctl add_user app_user strong_password
rabbitmqctl set_permissions -p /production app_user ".*" ".*" ".*"
# 格式：set_permissions -p {vhost} {user} {configure正则} {write正则} {read正则}

# 查看 Vhost 权限
rabbitmqctl list_permissions -p /production
```

生产建议：每个业务系统或环境使用独立 Vhost，避免相互影响。

---

## 生产配置调优

### 内存与磁盘水位

RabbitMQ 通过水位（watermark）机制保护自身，当内存或磁盘使用超过阈值时，阻塞所有生产者连接。

```ini
# 内存水位：当 RabbitMQ 使用内存超过系统总内存的 40% 时触发流控
vm_memory_high_watermark.relative = 0.4

# 也可以使用绝对值
# vm_memory_high_watermark.absolute = 4GB

# 磁盘空闲空间低于此值时触发流控（防止磁盘写满）
disk_free_limit.absolute = 5GB

# 或相对于内存的倍数
# disk_free_limit.relative = 2.0
```

**查看当前水位状态**

```bash
rabbitmqctl status | grep -A5 "memory\|disk_free"
```

### 连接与信道限制

每个 TCP 连接可以创建多个信道（Channel），信道是 AMQP 协议的轻量级并发机制。

```ini
# 单个连接最大信道数（默认 2047）
channel_max = 200

# 最大连接数（默认无限制）
connection_max = 500
```

```bash
# 查看当前连接数
rabbitmqctl list_connections | wc -l

# 查看信道数
rabbitmqctl list_channels | wc -l
```

信道数过多（> 1000）通常说明应用层连接管理有问题，每个线程创建了独立连接或信道而没有复用。

### 消息持久化

消息和队列都需要设置持久化，才能在 RabbitMQ 重启后保留：

```python
# 队列持久化（durable=True）
channel.queue_declare(queue='important.tasks', durable=True)

# 消息持久化（delivery_mode=2）
channel.basic_publish(
    exchange='',
    routing_key='important.tasks',
    body=json.dumps(message),
    properties=pika.BasicProperties(
        delivery_mode=2,          # 持久化
        content_type='application/json',
        message_id=str(uuid.uuid4()),
        timestamp=int(time.time()),
    )
)
```

注意：Quorum Queue 强制持久化，不需要显式设置 `delivery_mode`，但设置了也没有副作用。

### 消息 TTL 与队列 TTL

```python
# 队列级别：所有消息 TTL 为 1 小时
channel.queue_declare(
    queue='temp.tasks',
    durable=True,
    arguments={
        'x-message-ttl': 3600000,        # 毫秒
        'x-expires': 7200000,            # 队列空闲 2h 后自动删除
        'x-max-length': 100000,          # 最大消息数
        'x-max-length-bytes': 104857600, # 最大字节数（100MB）
        'x-overflow': 'reject-publish',  # 超出限制后拒绝新消息（而非丢弃旧消息）
    }
)
```

---

## 消费者可靠性

### ACK / NACK / Reject 机制

RabbitMQ 的消息确认机制是保证可靠消费的核心：

| 操作 | 含义 | 消息去向 |
|------|------|--------|
| `basic_ack` | 处理成功 | 从队列删除 |
| `basic_nack(requeue=True)` | 处理失败，稍后重试 | 重新入队 |
| `basic_nack(requeue=False)` | 处理失败，不重试 | 进入死信队列（如已配置），否则丢弃 |
| `basic_reject(requeue=False)` | 拒绝处理 | 同 nack requeue=False |

```python
def process_message(ch, method, properties, body):
    try:
        data = json.loads(body)
        handle_task(data)
        # 处理成功，确认消息
        ch.basic_ack(delivery_tag=method.delivery_tag)
    except RetryableError as e:
        logger.warning(f"可重试错误，消息重新入队: {e}")
        # 重新入队，但要注意无限循环问题
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
    except PermanentError as e:
        logger.error(f"永久性错误，消息进入死信队列: {e}")
        # 不重新入队，发往死信队列
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
    except Exception as e:
        logger.error(f"未知错误: {e}", exc_info=True)
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
```

**注意**：`requeue=True` 的消息会被重新放回队列头部，在单消费者场景下可能导致无限循环处理同一条失败消息。建议：
1. 配合死信队列限制重试次数
2. 在应用层记录已重试次数（消息 Header 中）
3. 超过阈值后改为 `requeue=False`

### prefetch_count 调优

prefetch（QoS）控制消费者在未 ACK 的情况下最多预取多少条消息：

```python
# 设置 prefetch count
# 值为 1：严格逐条处理，吞吐量最低但最公平
# 值为 10~100：批量预取，吞吐量更高，但单个消费者可能积压更多未处理消息
channel.basic_qos(prefetch_count=10)
```

**prefetch 选择指南**

| 场景 | 推荐值 | 原因 |
|------|--------|------|
| 任务处理时间长且不均匀 | 1~5 | 避免慢消费者积压太多 |
| 高吞吐简单任务 | 50~200 | 减少网络往返 |
| 多消费者负载均衡 | 避免过大 | 防止某个消费者独占消息 |
| 内存敏感型消费者 | 根据消息大小计算 | 防止 OOM |

```python
# 更合理的方式：按字节设置（需 RabbitMQ 3.x 的 global QoS 支持）
# 目前 basic_qos 的 prefetch_size 参数大多数客户端库不完整支持
# 建议通过 prefetch_count + 消息大小控制间接实现
channel.basic_qos(prefetch_count=20, global_qos=False)
# global_qos=True：限制整个信道
# global_qos=False（默认）：限制单个消费者
```

### 死信队列（DLX）

死信队列（Dead Letter Exchange）是处理失败消息的标准模式，避免坏消息阻塞正常消费。

**配置流程**

```python
# 1. 先声明死信 Exchange 和队列
channel.exchange_declare(exchange='dlx', exchange_type='direct', durable=True)
channel.queue_declare(queue='task.queue.dead', durable=True)
channel.queue_bind(queue='task.queue.dead', exchange='dlx', routing_key='task.queue')

# 2. 创建业务队列，绑定 DLX
channel.queue_declare(
    queue='task.queue',
    durable=True,
    arguments={
        'x-queue-type': 'quorum',
        'x-dead-letter-exchange': 'dlx',         # 死信 Exchange
        'x-dead-letter-routing-key': 'task.queue', # 死信 Routing Key
        'x-delivery-limit': 3,                    # Quorum Queue: 最大投递次数
    }
)
```

`x-delivery-limit` 是 Quorum Queue 特有属性，消息被投递超过此次数后自动进入死信队列，无需应用层计数。

**死信队列处理建议**

```python
# 死信消费者：记录、报警、人工处理或降级处理
def process_dead_letter(ch, method, properties, body):
    headers = properties.headers or {}
    death_info = headers.get('x-death', [{}])[0]

    logger.error(
        "消息进入死信队列",
        extra={
            'original_queue': death_info.get('queue'),
            'reason': death_info.get('reason'),
            'count': death_info.get('count'),
            'message_id': properties.message_id,
            'body_preview': body[:200].decode('utf-8', errors='replace'),
        }
    )

    # 根据情况：持久化到 DB、发告警、人工干预
    save_to_failed_messages_db(properties, body)
    ch.basic_ack(delivery_tag=method.delivery_tag)
```

### 延迟消息

RabbitMQ 原生不支持延迟消息，需要使用 `rabbitmq_delayed_message_exchange` 插件：

```bash
# 启用插件
rabbitmq-plugins enable rabbitmq_delayed_message_exchange
```

```python
# 创建 delayed exchange
channel.exchange_declare(
    exchange='delayed.exchange',
    exchange_type='x-delayed-message',
    durable=True,
    arguments={'x-delayed-type': 'direct'}
)

# 发送延迟消息（延迟 30 秒）
channel.basic_publish(
    exchange='delayed.exchange',
    routing_key='task.delayed',
    body=json.dumps(payload),
    properties=pika.BasicProperties(
        headers={'x-delay': 30000},   # 毫秒
        delivery_mode=2,
    )
)
```

---

## 监控体系

### Management Plugin API

Management Plugin 提供 HTTP API，可以获取队列、连接、节点等详细统计信息。

```bash
# 获取所有队列状态
curl -s -u guest:guest http://localhost:15672/api/queues/%2F | \
  jq '.[] | {name, messages, messages_ready, messages_unacknowledged, consumers}'

# 获取单个队列详情
curl -s -u admin:password \
  "http://localhost:15672/api/queues/%2Fproduction/task.queue" | jq .

# 获取节点状态
curl -s -u admin:password http://localhost:15672/api/nodes | \
  jq '.[] | {name, running, mem_used, disk_free, proc_used}'

# 获取连接列表
curl -s -u admin:password http://localhost:15672/api/connections | \
  jq '.[] | {name, state, channels, send_pend}'
```

**注意**：Management Plugin 的 `/api/queues` 接口开销较大，不建议高频调用（> 1次/秒），监控系统应使用 prometheus exporter。

### Prometheus + rabbitmq_exporter

**方案一：官方内置 Prometheus 插件（推荐，RabbitMQ 3.8+）**

```bash
rabbitmq-plugins enable rabbitmq_prometheus
```

启用后在 `http://localhost:15692/metrics` 暴露 Prometheus 格式指标，无需额外部署 exporter。

```yaml
# prometheus.yml 抓取配置
scrape_configs:
  - job_name: 'rabbitmq'
    static_configs:
      - targets:
        - 'mq-node1:15692'
        - 'mq-node2:15692'
        - 'mq-node3:15692'
    metric_relabel_configs:
      - source_labels: [queue]
        target_label: queue_name
```

**方案二：kbudde/rabbitmq-exporter（兼容老版本）**

```yaml
# docker-compose.yml
services:
  rabbitmq_exporter:
    image: kbudde/rabbitmq-exporter:latest
    environment:
      RABBIT_URL: "http://mq-node1:15672"
      RABBIT_USER: monitoring
      RABBIT_PASSWORD: password
      RABBIT_CAPABILITIES: "bert,no_sort"
      PUBLISH_PORT: "9419"
    ports:
      - "9419:9419"
```

### 关键监控指标

**队列健康**

| 指标 | 说明 | 告警建议 |
|------|------|--------|
| `rabbitmq_queue_messages` | 队列消息总数 | 超过业务阈值告警 |
| `rabbitmq_queue_messages_ready` | 待消费消息数 | 持续增长超 5min |
| `rabbitmq_queue_messages_unacknowledged` | 未 ACK 消息数 | 超过 prefetch * consumer_count |
| `rabbitmq_queue_consumers` | 消费者数量 | 降为 0 立即告警 |
| `rabbitmq_queue_messages_published_total` | 消息发布速率 | 突然降为 0（生产者故障）|
| `rabbitmq_queue_messages_delivered_total` | 消息消费速率 | 远低于发布速率（消费者瓶颈）|

**节点健康**

| 指标 | 说明 | 告警建议 |
|------|------|--------|
| `rabbitmq_process_resident_memory_bytes` | 进程内存使用 | > 内存水位 80% |
| `rabbitmq_disk_space_available_bytes` | 磁盘可用空间 | < 磁盘水位 + 10GB |
| `rabbitmq_erlang_processes_used` | Erlang 进程数 | > `process_limit` 的 70% |
| `rabbitmq_connections` | TCP 连接数 | > connection_max 的 80% |
| `rabbitmq_channels` | 信道总数 | > 预期值的 2 倍 |

### Grafana 看板配置

推荐直接使用官方维护的 Grafana Dashboard：
- ID `10991`：RabbitMQ Overview（基于 prometheus 内置插件）
- ID `4279`：RabbitMQ Monitoring（基于 kbudde exporter）

### 关键告警规则

```yaml
# prometheus-rules.yaml
groups:
  - name: rabbitmq.rules
    rules:
      # 队列消息积压
      - alert: RabbitmqQueueMessagesHigh
        expr: |
          rabbitmq_queue_messages{queue!~".*\\.dead"} > 50000
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "队列 {{ $labels.queue }} 消息积压"
          description: "队列 {{ $labels.queue }} 积压 {{ $value }} 条消息"

      # 消费者数量为 0
      - alert: RabbitmqQueueNoConsumers
        expr: |
          rabbitmq_queue_consumers{queue!~".*\\.dead"} == 0
            and
          rabbitmq_queue_messages > 0
        for: 2m
        labels:
          severity: critical
        annotations:
          summary: "队列 {{ $labels.queue }} 无消费者"

      # 节点内存超水位
      - alert: RabbitmqMemoryHighWatermark
        expr: |
          rabbitmq_process_resident_memory_bytes
          /
          rabbitmq_vm_memory_high_watermark_bytes > 0.9
        for: 3m
        labels:
          severity: warning
        annotations:
          summary: "RabbitMQ 节点 {{ $labels.instance }} 内存接近水位"

      # 节点离线
      - alert: RabbitmqNodeDown
        expr: up{job="rabbitmq"} == 0
        for: 1m
        labels:
          severity: critical
        annotations:
          summary: "RabbitMQ 节点 {{ $labels.instance }} 不可达"

      # 死信队列有消息积压
      - alert: RabbitmqDeadLetterQueueNotEmpty
        expr: rabbitmq_queue_messages{queue=~".*\\.dead"} > 0
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "死信队列 {{ $labels.queue }} 有 {{ $value }} 条消息需要处理"
```

---

## 常见故障处理

### 消息堆积

**定位根因**

```bash
# 查看各队列积压情况
rabbitmqctl list_queues name messages consumers message_bytes \
  --vhost /production \
  --formatter table

# 找出积压最严重的队列
rabbitmqctl list_queues name messages consumers --formatter table | \
  sort -k2 -rn | head -20
```

消息堆积的常见原因：
1. **消费者处理过慢**：检查消费者日志、CPU/内存资源、外部依赖耗时
2. **消费者数量不足**：扩容消费者实例
3. **消费者全部下线**：检查服务状态，查看是否因异常退出
4. **消息处理异常**：检查死信队列，看是否有大量失败消息在重试循环

**临时应急：快速消费积压消息**

```bash
# 方法1：临时增加 prefetch（在消费者配置中调大）
# 方法2：手动 purge 非关键队列（慎用！消息会丢失）
rabbitmqctl purge_queue non_critical_queue --vhost /production

# 方法3：将消息转移到另一个队列（使用 shovel 插件）
rabbitmq-plugins enable rabbitmq_shovel rabbitmq_shovel_management
```

**根本方案**

- 增加消费者副本数（Kubernetes 扩容）
- 优化消费者处理逻辑，减少单条消息处理时间
- 评估是否需要增加队列分区（使用多个队列分担流量）

### 队列变为 unmirrored / 副本不足

Quorum Queue 在节点下线时，可能出现副本数低于期望值的情况：

```bash
# 查看 Quorum Queue 副本状态
rabbitmq-diagnostics check_if_any_deprecated_features_are_used

# 查看详细副本分布
rabbitmqctl list_queues name type members online slave_pids \
  --vhost /production \
  --formatter table
```

当节点重新上线后，副本会自动同步。如果需要手动触发：

```bash
# 手动触发 Quorum Queue 成员重选（一般不需要）
rabbitmqctl force_boot  # 仅在所有节点都下线时使用，否则可能丢数据！
```

经典镜像队列的 `unmirrored` 问题：

```bash
# 查看未完全同步的镜像队列
rabbitmqctl list_queues name slave_pids synchronised_slave_pids \
  --vhost /production | \
  awk '{if($2!=$3) print $0}'

# 手动触发同步
rabbitmqctl sync_queue -p /production queue_name
```

### 脑裂恢复

网络分区（脑裂）是 RabbitMQ 集群最严重的故障，两侧节点各自独立运行，消息和队列可能出现不一致。

**确认是否发生脑裂**

```bash
rabbitmqctl cluster_status | grep -A5 "Network Partitions"
```

输出中出现 `Network Partitions:` 后有内容则说明存在分区。

**恢复步骤（`pause_minority` 策略下）**

在 `pause_minority` 策略下，少数派节点会自动暂停服务，大多数派继续运行。网络恢复后：

```bash
# 1. 确认网络已恢复，所有节点互通
ping mq-node1
ping mq-node2
ping mq-node3

# 2. 检查分区状态
rabbitmqctl cluster_status

# 3. 如果还有分区记录，需要滚动重启节点来清除分区状态
# 先重启少数派节点（它们的数据更旧）
systemctl restart rabbitmq-server  # 在少数派节点执行

# 4. 验证集群状态
rabbitmqctl cluster_status
```

**手动恢复（所有节点都在线但分区未自愈）**

```bash
# 在其中一个节点执行，强制重新合并（会触发数据选择）
rabbitmqctl force_reset  # 极端情况才用，会清空该节点数据！

# 更安全的方式：移除再重新加入集群
rabbitmqctl stop_app
rabbitmqctl reset
rabbitmqctl join_cluster rabbit@mq-node1
rabbitmqctl start_app
```

**重要**：脑裂期间两侧可能都有写入，恢复时必然有一侧的数据会丢失。需要根据业务情况决定保留哪侧，并通过应用层的幂等设计和监控来补偿。

### 内存/磁盘水位触发，生产者被阻塞

**症状**：生产者发送消息卡住，日志出现 `blocked` 相关报错。

```bash
# 查看被阻塞的连接
rabbitmqctl list_connections name state blocked_by --formatter table

# 查看当前内存使用
rabbitmqctl status | grep memory

# 查看磁盘使用
rabbitmqctl status | grep disk_free
```

**临时缓解**

```bash
# 临时提高内存水位（治标不治本）
rabbitmqctl set_vm_memory_high_watermark 0.5

# 快速消费或清理队列，释放内存
rabbitmqctl purge_queue large_queue --vhost /production
```

**根本解决**

- 消费者追上进度，减少内存中的消息数量
- 增加服务器内存
- 检查是否有消费者异常导致消息无法被确认
- 调整 `x-max-length` 限制队列大小

---

## 与 Kafka 的适用场景对比

### 选 RabbitMQ 的场景

1. **需要灵活路由**：多个服务基于不同规则订阅同一类事件，用 Topic Exchange 比 Kafka 多个 Topic 更简洁
2. **任务队列（Work Queue）**：多个 Worker 竞争消费，RabbitMQ 的轮询分发天然支持；Kafka 需要保证 partition 数 >= consumer 数
3. **请求-响应模式（RPC over MQ）**：RabbitMQ 有 correlation_id + reply_to 原生支持
4. **需要消息优先级**：RabbitMQ Classic Queue 支持 `x-max-priority`
5. **延迟消息**：通过 delayed message 插件支持，Kafka 需要额外方案
6. **消息量较小（< 100K/s）**：RabbitMQ 运维复杂度更低

### 选 Kafka 的场景

1. **高吞吐持久化流**：单 broker 轻松百万 TPS，RabbitMQ 通常在几万~十万量级
2. **消息回放**：Kafka 消息可以保留数天/周，消费者可以重新消费历史消息；RabbitMQ 消息消费后即删除
3. **多消费组独立消费**：每个消费组独立维护 offset，互不影响；RabbitMQ 需要为每个消费者创建独立队列
4. **日志/事件溯源**：Kafka 的 partition 是有序日志，天然适合 Event Sourcing
5. **流处理**：Kafka Streams / Flink + Kafka 生态成熟

### 核心差异总结

| 维度 | RabbitMQ | Kafka |
|------|----------|-------|
| 消息模型 | Push（Broker 推给消费者）| Pull（消费者主动拉）|
| 消息保留 | 消费后删除 | 按时间/大小保留 |
| 路由能力 | 灵活（Exchange 多类型）| 简单（按 Topic/Partition）|
| 吞吐量 | 万~十万/s | 百万/s |
| 延迟 | 低（毫秒级）| 较低（毫秒~百毫秒）|
| 消息顺序 | 单队列 FIFO | Partition 内有序 |
| 消息回放 | 不支持 | 支持 |
| 学习曲线 | 中等（概念多）| 较陡（分布式原理复杂）|

---

## 运维命令速查

### 集群管理

```bash
# 查看集群状态
rabbitmqctl cluster_status

# 查看节点信息
rabbitmqctl node_health_check
rabbitmq-diagnostics check_port_connectivity
rabbitmq-diagnostics check_protocol_listener

# 优雅关闭节点（等待消费者完成当前消息）
rabbitmqctl shutdown

# 强制关闭（紧急情况）
rabbitmqctl stop_app
```

### 用户与权限

```bash
# 列出用户
rabbitmqctl list_users

# 添加用户
rabbitmqctl add_user new_user strong_password

# 设置角色（administrator/monitoring/policymaker/management）
rabbitmqctl set_user_tags new_user monitoring

# 设置 Vhost 权限
rabbitmqctl set_permissions -p /production new_user ".*" ".*" ".*"

# 删除用户
rabbitmqctl delete_user old_user

# 修改密码
rabbitmqctl change_password myuser new_password
```

### 队列操作

```bash
# 列出队列（含消息数和消费者数）
rabbitmqctl list_queues name messages consumers memory --vhost /production

# 删除队列
rabbitmqctl delete_queue my_queue --vhost /production

# 清空队列（消息丢失，慎用）
rabbitmqctl purge_queue my_queue --vhost /production

# 查看队列详情
rabbitmqadmin get queue=my_queue count=1 --vhost=/production
```

### Exchange 与 Binding

```bash
# 列出 Exchange
rabbitmqctl list_exchanges name type durable --vhost /production

# 列出 Binding
rabbitmqctl list_bindings \
  source_name destination_name routing_key \
  --vhost /production

# 通过 rabbitmqadmin 声明 Exchange
rabbitmqadmin declare exchange \
  name=my.exchange \
  type=topic \
  durable=true \
  --vhost=/production
```

### 策略（Policy）管理

Policy 可以动态给队列/Exchange 添加属性，无需重建：

```bash
# 给所有队列添加死信队列策略
rabbitmqctl set_policy DLX ".*" \
  '{"dead-letter-exchange":"dlx"}' \
  --apply-to queues \
  --vhost /production \
  --priority 10

# 给特定队列设置 TTL
rabbitmqctl set_policy TTL "temp\\..*" \
  '{"message-ttl":3600000}' \
  --apply-to queues \
  --vhost /production

# 查看所有策略
rabbitmqctl list_policies --vhost /production

# 删除策略
rabbitmqctl clear_policy DLX --vhost /production
```

### 日志与诊断

```bash
# 实时查看 RabbitMQ 日志
tail -f /var/log/rabbitmq/rabbit@mq-node1.log

# 查看连接详情
rabbitmqctl list_connections \
  name peer_host peer_port state channels \
  send_pend recv_cnt send_cnt

# 查看信道信息
rabbitmqctl list_channels \
  connection name consumer_count messages_unacknowledged

# 查看消费者信息
rabbitmqctl list_consumers \
  --vhost /production \
  queue_name channel_pid consumer_tag prefetch_count

# 查看插件状态
rabbitmq-plugins list
rabbitmq-plugins list --enabled

# 启用/禁用插件
rabbitmq-plugins enable rabbitmq_shovel
rabbitmq-plugins disable rabbitmq_mqtt
```

### 使用 rabbitmqadmin

`rabbitmqadmin` 是基于 Management HTTP API 的命令行工具，比 `rabbitmqctl` 更适合操作 Exchange、Queue、Binding 和消息：

```bash
# 安装
curl -s http://localhost:15672/cli/rabbitmqadmin > /usr/local/bin/rabbitmqadmin
chmod +x /usr/local/bin/rabbitmqadmin

# 配置别名（含认证）
alias rmqadm='rabbitmqadmin -u admin -p password -V /production'

# 发布测试消息
rabbitmqadmin publish \
  exchange=amq.default \
  routing_key=test.queue \
  payload='{"test": true}' \
  -u admin -p password

# 获取（消费）一条消息
rabbitmqadmin get queue=test.queue count=1 -u admin -p password

# 导出所有配置（备份）
rabbitmqadmin export /backup/rabbitmq-config-$(date +%Y%m%d).json \
  -u admin -p password

# 导入配置（恢复）
rabbitmqadmin import /backup/rabbitmq-config-20240422.json \
  -u admin -p password
```
