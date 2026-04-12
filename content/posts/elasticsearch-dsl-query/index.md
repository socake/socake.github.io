---
title: "Elasticsearch 查询实战：从 URI Search 到 DSL 复杂聚合"
date: 2026-04-11T10:00:00+08:00
draft: false
tags: ["Elasticsearch", "ELK", "查询", "DSL", "运维"]
categories: ["ELK Stack"]
description: "系统梳理 ES 查询体系：URI Search 快速查、Query DSL 精确控制、Aggregations 聚合分析，以及 _cat API 运维常用命令，配合真实运维场景案例。"
summary: "ES 查询是每个运维必须掌握的技能。这篇文章从 URI Search 快速上手，到 DSL bool 查询、聚合分析，再到运维常用的 _cat API，配合真实排障场景整理成一篇实战手册。"
toc: true
math: false
diagram: false
series: ["ELK Stack 完全手册"]
keywords: ["Elasticsearch", "DSL", "Query", "Aggregation", "_cat API", "搜索"]
params:
  reading_time: true
---

## 两种查询方式的定位

ES 提供两种查询方式：**URI Search** 和 **Query DSL**。

URI Search 就是把查询参数拼在 URL 里，用 curl 一行命令搞定：

```bash
GET /logs-nginx-*/_search?q=status_code:500&sort=@timestamp:desc&size=10
```

优点是快，缺点是复杂查询写起来很丑，而且 URL 长度有限制，不支持所有 DSL 功能。

**适合 URI Search 的场景**：

- Shell 脚本里的临时查询
- 快速验证数据是否存在
- Grafana ES 数据源的简单 query 配置

**适合 Query DSL 的场景**：

- 复杂的多条件组合查询
- 聚合统计
- 运维脚本中需要精确控制查询行为
- 所有生产级别的查询

日常工作中 URI Search 用来快速探索，Query DSL 用来写生产脚本。

## URI Search 常用参数

基本格式：`GET /index/_search?参数1=值1&参数2=值2`

| 参数 | 说明 | 示例 |
|------|------|------|
| `q` | 查询字符串，Lucene 语法 | `q=status_code:500` |
| `sort` | 排序，格式 `field:asc/desc` | `sort=@timestamp:desc` |
| `size` | 返回条数，默认 10 | `size=50` |
| `from` | 偏移量，配合 size 分页 | `from=0` |
| `_source` | 返回哪些字段 | `_source=status_code,request` |
| `timeout` | 查询超时时间 | `timeout=5s` |

查询语法示例：

```bash
# 查 status_code 为 500 的日志
GET /logs-nginx-*/_search?q=status_code:500

# AND 查询
GET /logs-nginx-*/_search?q=status_code:500 AND service:payment

# 范围查询
GET /logs-nginx-*/_search?q=response_time:[1000 TO *]

# 通配符
GET /logs-nginx-*/_search?q=request_path:\/api\/user\/*
```

## Query DSL 核心

### match：全文搜索

```json
GET /logs-app-*/_search
{
  "query": {
    "match": {
      "error.message": "connection refused"
    }
  }
}
```

`match` 会对搜索词分词，然后做全文匹配。`"connection refused"` 会分成 `connection` 和 `refused` 两个词，只要文档包含其中一个就能匹配。

如果要求两个词都出现：

```json
{
  "query": {
    "match": {
      "error.message": {
        "query": "connection refused",
        "operator": "and"
      }
    }
  }
}
```

完整短语匹配用 `match_phrase`：

```json
{
  "query": {
    "match_phrase": {
      "error.message": "connection refused"
    }
  }
}
```

### term：精确匹配

```json
GET /logs-nginx-*/_search
{
  "query": {
    "term": {
      "status_code": 500
    }
  }
}
```

`term` 不分词，做精确匹配。**对于字符串字段，必须用 `.keyword` 子字段**：

```json
{
  "query": {
    "term": {
      "service.name.keyword": "payment-service"
    }
  }
}
```

如果用 `service.name`（text 字段），`term` 查询会失效，因为 text 字段存的是分词后的词条，而不是原始字符串。

多值 term 用 `terms`：

```json
{
  "query": {
    "terms": {
      "status_code": [502, 503, 504]
    }
  }
}
```

### range：范围查询

```json
GET /logs-nginx-*/_search
{
  "query": {
    "range": {
      "@timestamp": {
        "gte": "now-1h",
        "lt": "now"
      }
    }
  }
}
```

数值范围：

```json
{
  "query": {
    "range": {
      "response_time": {
        "gte": 1000,
        "lt": 5000
      }
    }
  }
}
```

操作符：`gt`（大于）、`gte`（大于等于）、`lt`（小于）、`lte`（小于等于）。

### bool：组合查询的核心

bool 查询是最重要的查询类型，把多个查询条件组合起来：

| 子句 | 含义 | 影响评分 |
|------|------|---------|
| `must` | 必须满足，相当于 AND | 是 |
| `filter` | 必须满足，但不计算相关性评分 | 否 |
| `should` | 满足一个或多个 | 是 |
| `must_not` | 必须不满足 | 否 |

**filter vs must 的关键区别**：filter 不计算相关性评分，结果会被缓存，性能更好。对于日志查询这种不需要排序相关性的场景，**条件过滤全部放 filter 里**。

```json
GET /logs-nginx-*/_search
{
  "query": {
    "bool": {
      "filter": [
        {
          "range": {
            "@timestamp": {
              "gte": "now-1h"
            }
          }
        },
        {
          "terms": {
            "status_code": [500, 502, 503, 504]
          }
        },
        {
          "term": {
            "service.name.keyword": "api-gateway"
          }
        }
      ],
      "must_not": [
        {
          "term": {
            "request.path.keyword": "/health"
          }
        }
      ]
    }
  },
  "size": 50,
  "sort": [
    {"@timestamp": "desc"}
  ]
}
```

这个查询的含义：最近 1 小时内，api-gateway 服务的 5xx 错误，排除健康检查接口，按时间倒序返回 50 条。

## 聚合查询：从原始数据到统计洞察

聚合查询（Aggregations）是 ES 最强大的功能之一，对应 SQL 里的 GROUP BY + 聚合函数。

### terms：按字段分组统计

统计每个服务的请求量：

```json
GET /logs-nginx-*/_search
{
  "size": 0,
  "query": {
    "range": {
      "@timestamp": {"gte": "now-1h"}
    }
  },
  "aggs": {
    "by_service": {
      "terms": {
        "field": "service.name.keyword",
        "size": 10,
        "order": {"_count": "desc"}
      }
    }
  }
}
```

`"size": 0` 表示不返回原始文档，只返回聚合结果，节省带宽。

结果：

```json
{
  "aggregations": {
    "by_service": {
      "buckets": [
        {"key": "api-gateway", "doc_count": 45820},
        {"key": "payment-service", "doc_count": 12340},
        {"key": "order-service", "doc_count": 8900}
      ]
    }
  }
}
```

### date_histogram：时序聚合

统计每 5 分钟的请求量（用于画折线图）：

```json
GET /logs-nginx-*/_search
{
  "size": 0,
  "query": {
    "range": {"@timestamp": {"gte": "now-6h"}}
  },
  "aggs": {
    "requests_over_time": {
      "date_histogram": {
        "field": "@timestamp",
        "fixed_interval": "5m",
        "min_doc_count": 0
      }
    }
  }
}
```

`"min_doc_count": 0` 确保没有数据的时间点也返回，这样折线图不会有空缺。

### avg / sum / percentiles：数值统计

统计接口响应时间的 P50、P95、P99：

```json
GET /logs-nginx-*/_search
{
  "size": 0,
  "query": {
    "bool": {
      "filter": [
        {"range": {"@timestamp": {"gte": "now-1h"}}},
        {"term": {"service.name.keyword": "payment-service"}}
      ]
    }
  },
  "aggs": {
    "latency_percentiles": {
      "percentiles": {
        "field": "response_time",
        "percents": [50, 95, 99]
      }
    },
    "latency_avg": {
      "avg": {
        "field": "response_time"
      }
    }
  }
}
```

### 嵌套聚合：组合使用

先按服务分组，再统计每个服务的错误率：

```json
GET /logs-nginx-*/_search
{
  "size": 0,
  "query": {
    "range": {"@timestamp": {"gte": "now-1h"}}
  },
  "aggs": {
    "by_service": {
      "terms": {
        "field": "service.name.keyword",
        "size": 20
      },
      "aggs": {
        "error_count": {
          "filter": {
            "range": {"status_code": {"gte": 500}}
          }
        },
        "error_rate": {
          "bucket_script": {
            "buckets_path": {
              "errors": "error_count._count",
              "total": "_count"
            },
            "script": "params.errors / params.total * 100"
          }
        }
      }
    }
  }
}
```

## 实用运维查询场景

### 最近 1 小时 5xx 请求数

```json
GET /logs-nginx-*/_count
{
  "query": {
    "bool": {
      "filter": [
        {"range": {"@timestamp": {"gte": "now-1h"}}},
        {"range": {"status_code": {"gte": 500, "lt": 600}}}
      ]
    }
  }
}
```

用 `_count` 接口比 `_search` 更高效，只返回计数不返回文档。

### 找出最慢的 10 个接口

```json
GET /logs-nginx-*/_search
{
  "size": 0,
  "query": {
    "bool": {
      "filter": [
        {"range": {"@timestamp": {"gte": "now-24h"}}},
        {"term": {"status_code": 200}}
      ]
    }
  },
  "aggs": {
    "slowest_apis": {
      "terms": {
        "field": "request.path.keyword",
        "size": 10,
        "order": {"p99_latency": "desc"}
      },
      "aggs": {
        "p99_latency": {
          "percentiles": {
            "field": "response_time",
            "percents": [99]
          }
        }
      }
    }
  }
}
```

注意这里用 P99 而不是平均值排序，更能找出真正有问题的接口。

### 按服务统计错误率（最近 5 分钟）

```json
GET /logs-nginx-*/_search
{
  "size": 0,
  "query": {
    "range": {"@timestamp": {"gte": "now-5m"}}
  },
  "aggs": {
    "by_service": {
      "terms": {"field": "service.name.keyword", "size": 50},
      "aggs": {
        "total": {"value_count": {"field": "status_code"}},
        "errors": {
          "filter": {"range": {"status_code": {"gte": 500}}}
        }
      }
    }
  }
}
```

## _cat API：运维日常必备

`_cat` API 返回人类可读的表格格式，主要用于运维巡检。

### 查看集群健康

```bash
GET /_cat/health?v

# 输出示例：
# epoch      timestamp cluster       status node.total node.data shards pri relo init unassign
# 1712803200 08:00:00  my-es-cluster green          3         3     45  15    0    0        0
```

`status` 字段：
- `green`：所有分片正常
- `yellow`：主分片正常，部分副本分片未分配（通常是单节点集群）
- `red`：有主分片未分配，数据不可用

### 查看节点状态

```bash
GET /_cat/nodes?v&h=name,ip,heap.percent,ram.percent,cpu,load_1m,node.role

# 关注 heap.percent > 80% 的节点，可能需要调整内存
```

### 查看索引状态

```bash
GET /_cat/indices?v&s=store.size:desc&h=index,status,health,pri,rep,docs.count,store.size

# 按大小降序排列，找出占空间最大的索引
```

### 查看分片分布

```bash
GET /_cat/shards?v&h=index,shard,prirep,state,node

# 找出 UNASSIGNED 状态的分片
GET /_cat/shards?v&h=index,shard,prirep,state,node&s=state:asc
```

分片未分配（UNASSIGNED）是集群变 yellow 的常见原因，需要用以下命令查看具体原因：

```json
GET /_cluster/allocation/explain
```

## 踩坑记录

### text 字段不能做精确匹配

前面说过很多次了，这里总结一下规律：

- **搜索（match/match_phrase）**：用 text 字段
- **精确匹配（term/terms）**：用 keyword 字段（`.keyword`）
- **聚合（terms agg/排序）**：用 keyword 字段
- **范围查询（range）**：用数值字段或 keyword 字段（日期字段用 date 类型）

判断用哪个的简单方法：在 Kibana Dev Tools 里 `GET /your-index/_mapping`，看字段类型。如果是 `"type": "text"`，聚合和精确匹配用 `.keyword`；如果直接是 `"type": "keyword"` 或 `"type": "integer"`，直接用原字段。

### 深分页性能问题

`from + size` 分页在深度分页时（比如 from=10000）性能很差。ES 需要在每个分片上取 `from + size` 条记录，然后在协调节点上合并排序，from 越大开销越大。

**search_after 替代方案**：

第一页：

```json
GET /logs-nginx-*/_search
{
  "size": 100,
  "sort": [
    {"@timestamp": "desc"},
    {"_id": "asc"}
  ]
}
```

记录最后一条的 `sort` 值，作为下一页的游标：

```json
GET /logs-nginx-*/_search
{
  "size": 100,
  "sort": [
    {"@timestamp": "desc"},
    {"_id": "asc"}
  ],
  "search_after": ["2026-04-11T07:59:55.000Z", "abc123"]
}
```

这种方式每次只拉取 100 条，不管翻到第几页性能都稳定。缺点是只能顺序翻页，不能跳到任意页。

### 聚合结果不准确

`terms` 聚合默认只从每个分片取 size × 1.5 条数据，在分片数多的情况下，聚合结果可能不准确（特别是尾部排名）。如果需要精确的 Top N，可以设置 `shard_size`：

```json
{
  "aggs": {
    "by_service": {
      "terms": {
        "field": "service.name.keyword",
        "size": 10,
        "shard_size": 100
      }
    }
  }
}
```

`shard_size` 越大准确性越高，但性能开销也越大。
