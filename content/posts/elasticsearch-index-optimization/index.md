---
title: "Elasticsearch 索引策略：ILM 生命周期管理与写入性能优化"
date: 2025-09-24T11:01:00+08:00
draft: false
tags: ["Elasticsearch", "ELK", "索引", "性能优化", "运维"]
categories: ["ELK Stack"]
description: "ILM 四阶段配置、rollover 策略、bulk 写入调优，以及分片数规划和 mapping 爆炸的避坑指南。"
summary: "ILM 四阶段配置、rollover 策略、bulk 写入调优，以及分片数规划和 mapping 爆炸的避坑指南。"
toc: true
math: false
diagram: false
series: ["ELK Stack 完全手册"]
keywords: ["Elasticsearch", "ILM", "索引生命周期", "写入优化", "分片"]
params:
  reading_time: true
---

ES 集群搭起来之后，接下来最重要的事情是索引策略——分片怎么设、数据怎么流转、写入性能怎么调。这块如果不做好，用不了多久集群就会开始变慢，甚至出现磁盘告警。我们的日志平台在早期就踩过分片数设太多的坑，整个集群响应时间翻了好几倍，排查了好几天才定位到根因。这篇把实际经验都整理出来。

## 索引设计三要素

在配置任何东西之前，先把这三个要素想清楚。

### 分片数规划

ES 的一个分片对应 Lucene 的一个索引实例。分片数直接影响：

- **写入并行度**：分片越多，可以并发写入的节点越多，但单个 bulk 请求的路由开销也越大
- **查询并行度**：查询会下发到所有相关分片并发执行，分片多理论上更快，但超过节点数之后收益递减，协调开销反而更大
- **集群元数据压力**：每个分片在 Master 节点上都有状态记录，几万个分片时 Master 会明显变慢

**实际规划原则：**

单个分片大小建议控制在 10-50GB（日志场景），超过 50GB 的分片查询会变慢，Merge 操作也更耗时。按这个标准反推分片数：

```
分片数 = ceil(索引每日数据量 / 目标分片大小)
```

我们日志平台每天 15GB 数据，每个 rollover 周期保留 1 天的热数据，目标分片大小 20GB：

```
分片数 = ceil(15GB / 20GB) = 1 个主分片
```

加 1 个副本，每个索引 2 个分片。不要一上来就设 5 个主分片，除非你的数据量真的需要。

### 副本数设置

副本的作用：读取高可用 + 查询负载分担。日志场景建议：

- 热数据：1 副本（保证高可用）
- 温数据：1 副本（可以降成 0 节省空间，但失去容错能力）
- 冷数据：0 副本（归档数据，只需要能查到即可）

ILM 可以自动在数据迁移时调整副本数，不需要手动操作。

### Mapping 设计

Mapping 定义了字段的数据类型和索引行为，提前设计好可以避免后期 mapping 爆炸问题。

对于日志类数据，一个比较实用的 mapping 模板：

```json
{
  "mappings": {
    "dynamic": "strict",
    "properties": {
      "@timestamp": { "type": "date" },
      "level": { "type": "keyword" },
      "service": { "type": "keyword" },
      "trace_id": { "type": "keyword", "index": false },
      "message": {
        "type": "text",
        "analyzer": "standard",
        "fields": {
          "keyword": {
            "type": "keyword",
            "ignore_above": 256
          }
        }
      },
      "http": {
        "properties": {
          "method": { "type": "keyword" },
          "status_code": { "type": "short" },
          "path": { "type": "keyword" },
          "duration_ms": { "type": "float" }
        }
      },
      "kubernetes": {
        "properties": {
          "namespace": { "type": "keyword" },
          "pod_name": { "type": "keyword" },
          "container_name": { "type": "keyword" }
        }
      }
    }
  }
}
```

关键点：`"dynamic": "strict"` 禁止动态字段——任何 mapping 里没有定义的字段写入时会报错，而不是自动创建新字段。这是防止 mapping 爆炸最重要的设置，后面踩坑部分会详细讲。

## ILM 四阶段配置

ILM（Index Lifecycle Management）是 ES 管理索引数据流转的机制，从 Hot 到 Warm 到 Cold 最后到 Delete，自动完成分片分配、副本调整、索引冻结和删除。

### 完整 ILM 策略示例

```json
PUT _ilm/policy/logs-lifecycle
{
  "policy": {
    "phases": {
      "hot": {
        "min_age": "0ms",
        "actions": {
          "rollover": {
            "max_primary_shard_size": "20gb",
            "max_age": "1d"
          },
          "set_priority": {
            "priority": 100
          }
        }
      },
      "warm": {
        "min_age": "7d",
        "actions": {
          "set_priority": {
            "priority": 50
          },
          "allocate": {
            "number_of_replicas": 1,
            "require": {
              "data": "warm"
            }
          },
          "forcemerge": {
            "max_num_segments": 1
          },
          "shrink": {
            "number_of_shards": 1
          }
        }
      },
      "cold": {
        "min_age": "30d",
        "actions": {
          "set_priority": {
            "priority": 0
          },
          "allocate": {
            "number_of_replicas": 0,
            "require": {
              "data": "cold"
            }
          },
          "freeze": {}
        }
      },
      "delete": {
        "min_age": "60d",
        "actions": {
          "delete": {
            "delete_searchable_snapshot": true
          }
        }
      }
    }
  }
}
```

几个关键配置解释：

**rollover 触发条件**

rollover 支持三个触发条件（任意一个满足就触发）：
- `max_primary_shard_size`：主分片大小，推荐 20-50GB
- `max_age`：索引年龄，日志场景通常设 1 天
- `max_docs`：文档数量，一般不用这个

注意：rollover 只有在同时满足以下条件时才会执行：
1. ILM 策略里配置了 rollover
2. 索引通过 alias 关联了数据流或索引别名
3. 当前索引是别名的 write index

**warm 阶段的 forcemerge**

温数据不再写入，可以把多个 Lucene segment 合并成 1 个（`max_num_segments: 1`），减少查询时的 segment 扫描开销，节省磁盘空间（删除标记被真正清除）。

forcemerge 是 IO 密集型操作，会触发大量磁盘读写，建议在低峰期执行。ECK 环境下可以通过 ILM 的 `min_age` 控制执行时间窗口。

**cold 阶段的 freeze**

冻结（freeze）会把索引的内存状态释放掉，减少内存占用，但每次查询时需要重新加载，有延迟。如果冷数据完全不查，可以直接删副本；如果偶尔需要查，freeze 是好选择。

### 索引模板配置

ILM 策略需要和索引模板关联，这样新创建的索引才会自动应用策略：

```json
PUT _index_template/logs-template
{
  "index_patterns": ["logs-*"],
  "data_stream": {},
  "template": {
    "settings": {
      "number_of_shards": 1,
      "number_of_replicas": 1,
      "index.lifecycle.name": "logs-lifecycle",
      "index.routing.allocation.require.data": "hot",
      "index.refresh_interval": "10s",
      "index.translog.durability": "async",
      "index.translog.sync_interval": "30s"
    },
    "mappings": {
      "dynamic": "strict",
      "properties": {
        "@timestamp": { "type": "date" },
        "level": { "type": "keyword" },
        "service": { "type": "keyword" },
        "message": { "type": "text" }
      }
    }
  },
  "priority": 200
}
```

注意 `"data_stream": {}` 这一行——使用 Data Streams 而不是传统的 alias + index 组合，是 ES 8.x 推荐的日志数据管理方式。Data Streams 自动管理 rollover，不需要手动维护 alias。

### 创建数据流

```bash
# 使用索引模板创建数据流
PUT _data_stream/logs-myapp

# 验证
GET _data_stream/logs-myapp
```

写入数据时，直接写入数据流名称即可：

```bash
POST logs-myapp/_doc
{
  "@timestamp": "2026-04-11T10:00:00Z",
  "level": "INFO",
  "service": "payment-service",
  "message": "Payment processed successfully"
}
```

## 写入性能优化

写入性能对日志平台很关键，几个核心优化点：

### Bulk API

单条写入和批量写入性能差异巨大。ES 的 HTTP 请求每次都有 TCP 握手、序列化、路由计算等开销，单条写入在高并发下很快就会成为瓶颈。

建议的 bulk 大小：
- 每批 5-15MB 数据（压缩前）
- 每批 500-5000 条文档
- 具体数字需要根据文档大小测试调整

```bash
POST /_bulk
{ "index": { "_index": "logs-myapp" } }
{ "@timestamp": "2026-04-11T10:00:00Z", "level": "INFO", "message": "event 1" }
{ "index": { "_index": "logs-myapp" } }
{ "@timestamp": "2026-04-11T10:00:01Z", "level": "ERROR", "message": "event 2" }
```

**客户端并发度：** 并发 bulk 请求数建议等于数据节点数，超过之后收益很小，反而增加协调节点的聚合压力。

### refresh_interval 调整

ES 默认每 1 秒刷新一次（`refresh_interval: 1s`），刷新会把 in-memory buffer 的数据写入新的 Lucene segment，刷新后数据才可被搜索到（近实时搜索）。

每次刷新都会创建新 segment，segment 越多查询越慢，Merge 开销越大。对于日志场景，1 分钟内的延迟通常可以接受，可以放大 refresh_interval：

```json
PUT logs-myapp/_settings
{
  "index.refresh_interval": "30s"
}
```

批量导入历史数据时，可以临时关闭刷新：

```json
PUT logs-myapp/_settings
{
  "index.refresh_interval": "-1"
}
```

导入完成后记得恢复。

### translog 异步刷盘

translog（事务日志）是 ES 的 WAL，每次写操作都会写入 translog，默认每次写入都同步刷盘（`request` 模式），这对写入性能影响很大。

对于日志场景，数据丢失几十秒的写入通常可以接受，可以改为异步模式：

```json
PUT logs-myapp/_settings
{
  "index.translog.durability": "async",
  "index.translog.sync_interval": "30s"
}
```

这样 translog 每 30 秒刷盘一次，而不是每次写入都刷。如果节点在 30 秒内崩溃，最多丢失 30 秒的数据。

**注意：** 这个设置在索引模板里配置，不要用 API 临时修改生产索引的 translog 设置，容易忘记恢复。

### 写入线程池监控

如果 bulk 请求出现 429 错误（Too Many Requests），说明写入线程池满了：

```bash
GET /_cat/thread_pool/write?v&h=node_name,name,active,rejected,completed
```

如果 `rejected` 持续增长，说明写入量超过集群处理能力，需要：
1. 增加数据节点
2. 降低写入速率（客户端限速）
3. 增大写入队列大小（会增加内存压力，治标不治本）

## 踩坑记录

**坑1：分片数设置过多导致集群变慢**

这是我们踩得最深的坑。早期规划的时候，参考了网上的"经验"：每天数据按 5 个主分片来，加上 7 天热数据，总分片数 = 5 * 7 * 2（含副本）= 70 个，看起来不多。

但是！我们有十几个业务服务，每个服务单独一条数据流，加上系统内置的 `.monitoring-*`、`.kibana_*` 等索引，总分片数很快超过了 5000。

现象：集群查询 p99 从 200ms 涨到了 5s，Master 节点 CPU 经常 100%。

诊断：

```bash
GET /_cluster/health?pretty
# 看 active_shards 总数

GET /_cat/indices?v&s=pri.store.size:desc
# 看每个索引的分片大小，识别过小的分片
```

发现大量分片只有几百 MB，远没有到 20GB 的目标大小。

根因：rollover 的 `max_age: 1d` 条件触发了，不管分片有多小都每天 rollover，导致小分片堆积。

解决方案：

```json
"rollover": {
  "max_primary_shard_size": "20gb",
  "max_age": "1d",
  "min_primary_shard_size": "5gb"  // ES 8.2+ 支持最小分片大小
}
```

同时把流量小的业务服务合并到同一个数据流，通过 `service` 字段区分，而不是每个服务单独一条数据流。

**坑2：Mapping 爆炸（Mapping Explosion）**

现象：某次上线了一个新版本，应用把 HTTP 请求的全部 headers 都打到了日志里，包括动态生成的 `X-Request-*` 自定义 header。默认 `dynamic: true` 的情况下，ES 为每个 header key 创建了一个 keyword 字段，几天内字段数从几十个暴涨到几万个。

影响：
- Master 节点内存暴涨（维护所有字段的 cluster state）
- 新文档写入越来越慢（每次写入都要更新 cluster state）
- Kibana 字段列表加载超时

解决过程很痛苦——ES 不支持删除字段，只能 reindex：

```bash
# 1. 创建新索引，设置正确的 mapping
PUT logs-app-v2
{
  "mappings": {
    "dynamic": "strict",
    "properties": { ... }
  }
}

# 2. reindex 数据（会很慢）
POST _reindex
{
  "source": { "index": "logs-app-*" },
  "dest": { "index": "logs-app-v2" }
}
```

更好的预防方案：

```json
{
  "mappings": {
    "dynamic": "strict",
    "properties": {
      "http": {
        "properties": {
          "headers": {
            "type": "object",
            "enabled": false  // 不索引，只存储原始 JSON
          }
        }
      }
    }
  }
}
```

对于结构不固定的嵌套对象，设置 `"enabled": false` 可以存储但不索引，避免动态字段爆炸。

**坑3：ILM 策略不生效**

现象：配置了 ILM 策略，但数据没有按时从 hot 迁移到 warm。

排查：

```bash
GET logs-myapp/_ilm/explain
```

看到 `"step": "ERROR"`，错误信息是：`"The index 'logs-myapp-000001' is not the write index for alias 'logs-myapp'"`。

原因：手动创建了索引但忘记设置 write index，导致 rollover 失败，ILM 状态机卡死了。

修复：

```bash
POST _aliases
{
  "actions": [
    {
      "add": {
        "index": "logs-myapp-000001",
        "alias": "logs-myapp",
        "is_write_index": true
      }
    }
  ]
}

# 然后重试 ILM
POST logs-myapp-000001/_ilm/retry
```

教训：使用 Data Streams 而不是手动管理 alias，可以避免这类问题。

## ILM 运维常用命令

```bash
# 查看数据流的 ILM 状态
GET logs-myapp/_ilm/explain

# 查看某个索引当前处于哪个阶段
GET .ds-logs-myapp-2026.04.11-000001/_ilm/explain

# 强制推进到下一个阶段（调试用）
POST .ds-logs-myapp-2026.04.11-000001/_ilm/move/phase
{
  "current_step": {
    "phase": "hot",
    "action": "rollover",
    "name": "check-rollover-ready"
  },
  "next_step": {
    "phase": "warm",
    "action": "allocate",
    "name": "allocate"
  }
}

# 查看所有 ILM 策略
GET /_ilm/policy

# 查看 ILM 执行统计
GET /_ilm/stats
```

索引策略是 ES 运维的基础，做好了集群可以长期稳定运行。下一篇讲备份和恢复，这是保障数据安全的最后一道防线。
