---
title: "Filebeat + Logstash 日志采集管道：大规模日志处理实战"
date: 2025-10-10T10:20:00+08:00
draft: false
tags: ["Filebeat", "Logstash", "ELK", "日志", "运维"]
categories: ["ELK Stack"]
description: "从选型对比到生产落地，详解 Filebeat + Kafka + Logstash 大规模日志采集管道的完整搭建过程，包含 grok 调试、性能调优和常见踩坑。"
summary: "大流量日志场景下，Fleet 直写 ES 会出现严重写入堆积。本文记录了我们从 Fleet 切换到 Filebeat + Kafka + Logstash 管道的全过程，重点讲 Logstash pipeline 配置和性能调优。"
toc: true
math: false
diagram: false
series: ["ELK Stack 完全手册"]
keywords: ["Filebeat", "Logstash", "Kafka", "日志采集", "ELK", "grok"]
params:
  reading_time: true
---

## 为什么要从 Fleet 切到 Filebeat

我们有一个业务高峰特征很明显的服务，平时日志量约 2000 条/秒，高峰期能飙到 20000 条/秒，持续时间 30 到 60 分钟。最初用 Fleet + Elastic Agent 直接走 ingest pipeline 写 ES，平时没问题，一到高峰就出事：ingest node CPU 跑满，写入延迟从毫秒级升到十几秒，最终出现大量写入拒绝，Kibana 里日志出现几分钟的空洞。

排查下来根本原因是缺少缓冲层。ES 的写入能力是有上限的，ingest pipeline 处理也消耗资源，高峰流量直接打过来，没有任何削峰手段。

切到 Filebeat → Kafka → Logstash → ES 之后，Kafka 作为消息缓冲层，高峰期积压的消息会在流量回落后被 Logstash 慢慢消化，ES 写入压力变得平稳，高峰期的日志空洞问题彻底消失。

## Filebeat vs Fluent Bit：选型分析

切换前我们评估了 Filebeat 和 Fluent Bit 两个采集端方案。

**Fluent Bit 的优势**在于极低的资源占用，C 语言实现，内存占用通常在 10MB 以内，CPU 开销接近可忽略。容器云场景下作为 DaemonSet 部署非常适合，尤其是节点数多、每个节点日志量不大的情况。

**Filebeat 的优势**在于与 Elastic 生态集成更紧密，Modules 配置简单，registry 文件机制（记录每个文件的采集偏移量）在大单体日志场景下更可靠。我们的业务日志文件单个可以达到数 GB，文件轮转逻辑复杂，用 Filebeat 的 filestream input 类型处理起来更省心。

另外 Filebeat 输出到 Kafka 的配置比 Fluent Bit 更完善，支持分区策略、压缩、ack 等级等细粒度配置。综合来看，**大单体日志文件场景选 Filebeat，云原生多容器场景选 Fluent Bit**。

## 整体架构

```
业务容器 (emptyDir 挂载)
    │
    ▼
Filebeat Sidecar
    │   output.kafka（gzip 压缩）
    ▼
Kafka 集群（3 broker，KRaft 模式）
    │   topic: app-logs（12 partitions）
    ▼
Logstash（2 实例，consumer_threads=6）
    │   grok → json → geoip → mutate
    ▼
Elasticsearch 数据流（logs-myapp-default）
    │
    ▼
Kibana 可视化
```

Kafka 在这里承担三个职责：削峰缓冲、数据持久化（日志保留 48 小时）、解耦采集和处理。即使 Logstash 临时宕机，日志也不会丢失。

## Filebeat Sidecar 模式配置

容器化环境下，Filebeat 以 Sidecar 模式与业务容器共享 `emptyDir` 卷，业务容器写日志到挂载目录，Filebeat 从同一目录读取。

Kubernetes 部署片段：

```yaml
volumes:
  - name: app-logs
    emptyDir: {}

containers:
  - name: app
    image: myapp:latest
    volumeMounts:
      - name: app-logs
        mountPath: /var/log/app

  - name: filebeat
    image: docker.elastic.co/beats/filebeat:8.12.0
    volumeMounts:
      - name: app-logs
        mountPath: /var/log/app
        readOnly: true
      - name: filebeat-config
        mountPath: /usr/share/filebeat/filebeat.yml
        subPath: filebeat.yml
```

Filebeat 配置（filebeat.yml）：

```yaml
filebeat.inputs:
  - type: filestream
    id: app-logs
    enabled: true
    paths:
      - /var/log/app/*.log
    parsers:
      - multiline:
          type: pattern
          pattern: '^\d{4}-\d{2}-\d{2}'
          negate: true
          match: after
    prospector.scanner.symlinks: true

output.kafka:
  enabled: true
  hosts:
    - "kafka-0.kafka:9092"
    - "kafka-1.kafka:9092"
    - "kafka-2.kafka:9092"
  topic: "app-logs"
  partition.round_robin:
    reachable_only: true
  required_acks: -1
  compression: gzip
  compression_level: 4
  max_message_bytes: 1048576
  bulk_max_size: 512

logging.level: warning
logging.to_files: true
logging.files:
  path: /var/log/filebeat
  name: filebeat
  keepfiles: 3
```

这里用的是 `filestream` 类型而不是老的 `log` 类型，filestream 有几个重要改进：ID 机制避免文件重命名导致的重复采集，以及更细粒度的 ACK 机制减少重启后的重复发送。

`required_acks: -1` 表示需要所有 ISR 副本确认后才算发送成功，避免在 Kafka leader 切换时丢消息。

## Kafka 集群配置（KRaft 模式）

我们用的是 Kafka 3.5，KRaft 模式不依赖 ZooKeeper，3 个节点每个都同时担任 broker 和 controller。

关键参数：

```properties
# 每个节点的 server.properties
process.roles=broker,controller
node.id=1  # 每台不同，1/2/3

# 日志相关
log.retention.hours=48
log.segment.bytes=536870912  # 512MB per segment
log.retention.check.interval.ms=300000

# 性能相关
num.network.threads=6
num.io.threads=16
socket.send.buffer.bytes=102400
socket.receive.buffer.bytes=102400
```

topic 创建时 partition 数量要提前规划好，这决定了 Logstash 消费的最大并发度。我们按照 Logstash 实例数 × consumer_threads 来设置：2 实例 × 6 线程 = 12，所以创建 12 个 partition：

```bash
kafka-topics.sh --bootstrap-server localhost:9092 \
  --create --topic app-logs \
  --partitions 12 \
  --replication-factor 3 \
  --config retention.ms=172800000
```

## Logstash Pipeline 配置

这是整个管道最核心的部分，我们需要解析 Nginx access log 格式并进行字段提取。

`/etc/logstash/conf.d/app-logs.conf`：

```ruby
input {
  kafka {
    bootstrap_servers => "kafka-0:9092,kafka-1:9092,kafka-2:9092"
    topics => ["app-logs"]
    group_id => "logstash-consumer"
    auto_offset_reset => "latest"
    consumer_threads => 6
    decorate_events => true
    codec => "plain"
  }
}

filter {
  # 第一步：grok 解析 Nginx access log
  # 格式: 192.168.1.1 - - [11/Apr/2026:08:00:00 +0800] "GET /api/v1/users HTTP/1.1" 200 1234 "-" "Mozilla/5.0"
  grok {
    match => {
      "message" => '%{IPORHOST:client_ip} - %{DATA:ident} \[%{HTTPDATE:timestamp}\] "(?:%{WORD:method} %{NOTSPACE:request}(?: HTTP/%{NUMBER:http_version})?|-)" %{NUMBER:status_code:int} (?:%{NUMBER:bytes:int}|-) "%{DATA:referrer}" "%{DATA:user_agent}"'
    }
    tag_on_failure => ["_grokparsefailure"]
  }

  # grok 失败的日志单独处理，不丢弃
  if "_grokparsefailure" in [tags] {
    mutate {
      add_field => { "parse_error" => "grok_failure" }
    }
  }

  if "_grokparsefailure" not in [tags] {
    # 第二步：时间戳解析
    date {
      match => ["timestamp", "dd/MMM/yyyy:HH:mm:ss Z"]
      target => "@timestamp"
      timezone => "Asia/Shanghai"
    }

    # 第三步：geoip 地理位置
    geoip {
      source => "client_ip"
      target => "geoip"
      database => "/etc/logstash/GeoLite2-City.mmdb"
      ecs_compatibility => disabled
      fields => ["city_name", "country_name", "country_code2", "location"]
    }

    # 第四步：字段类型转换和清理
    mutate {
      convert => {
        "status_code" => "integer"
        "bytes" => "integer"
      }
      remove_field => ["timestamp", "ident", "message"]
    }

    # 第五步：标记 5xx 错误
    if [status_code] >= 500 {
      mutate {
        add_field => { "is_error" => true }
      }
    }
  }
}

output {
  elasticsearch {
    hosts => ["https://es-master:9200"]
    data_stream => "true"
    data_stream_type => "logs"
    data_stream_dataset => "nginx"
    data_stream_namespace => "prod"
    user => "logstash_writer"
    password => "${ES_PASSWORD}"
    ssl => true
    cacert => "/etc/logstash/http_ca.crt"
    timeout => 120
    pool_max => 1000
    bulk_max_size => 500
  }
}
```

## Logstash 性能调优

默认配置下 Logstash 性能往往达不到预期，核心参数在 `/etc/logstash/logstash.yml`：

```yaml
# Pipeline 工作线程数，建议等于 CPU 核心数
pipeline.workers: 8

# 每个 worker 每次从 input 取的事件数
# 值越大，吞吐越高，但延迟也越高
pipeline.batch.size: 500

# worker 等待事件的最长时间（毫秒）
# 低延迟场景降低此值，高吞吐场景可适当提高
pipeline.batch.delay: 50

# 是否允许同一 pipeline 多个 input 并发
pipeline.unsafe_shutdown: false
```

JVM 堆内存配置（`/etc/logstash/jvm.options`）：

```
# 生产环境建议设为宿主机内存的 50%，最大不超过 32GB
-Xms4g
-Xmx4g

# G1GC 在大堆内存下表现更好
-XX:+UseG1GC
-XX:G1ReservePercent=25
-XX:InitiatingHeapOccupancyPercent=30
```

调优后我们单台 Logstash（8 核 16G）能稳定处理 15000 条/秒，两台足以覆盖高峰场景。

## 踩坑记录

### grok 调试：先用 Grok Debugger

grok 表达式写错了很难定位问题，直接上生产测试效率极低。正确做法是打开 Kibana 的 Dev Tools → Grok Debugger，粘贴一行真实日志和你的 pattern，实时预览匹配结果和捕获到的字段。

另外强烈推荐用 `tag_on_failure` 而不是让 Logstash 静默丢弃解析失败的日志。我配置里所有 grok 都加了 `tag_on_failure => ["_grokparsefailure"]`，然后在 output 里把失败的日志写到单独的 index，方便后续排查。

### filestream 的 harvest 锁问题

升级 Filebeat 后发现有时日志文件更新了但 Filebeat 没有读取新数据。排查发现是 filestream input 的文件状态 registry 数据库（位于 `/var/lib/filebeat/registry/`）损坏了。直接删除 registry 目录然后重启 Filebeat，会重新从文件头开始采集，可能产生重复数据，但总比日志空洞强。

更好的做法是给 filestream input 配置合理的 `close.on_state_change.inactive` 时间，避免大量不活跃的文件 handler 占用资源导致 registry 过大。

### Kafka consumer lag 监控

Kafka consumer lag 是监控这条管道健康状态最重要的指标。lag 持续增长说明 Logstash 处理速度跟不上 Filebeat 写入速度。

用 Kafka 自带工具查看 lag：

```bash
kafka-consumer-groups.sh --bootstrap-server localhost:9092 \
  --describe --group logstash-consumer
```

生产环境建议用 kafka-exporter + Prometheus + Grafana 做持续监控，配置 lag 超过 100000 触发告警。我们有一次 Logstash 内存溢出 OOM 停掉了，靠 lag 告警第一时间发现，比等用户反馈日志延迟要快得多。

### 多行日志的消费顺序

Java 异常堆栈是多行的，Filebeat 的 multiline 配置能在采集端把多行合并成一条。但要注意：合并后单条消息体积可能很大（几十 KB），需要相应调大 Kafka 的 `max.message.bytes` 和 Filebeat 的 `max_message_bytes`，否则超大消息会被 Kafka 拒绝，Filebeat 日志里会出现 `message too large` 错误但不会立即报错退出，只是那条日志静默丢失了。
