---
title: "Python 操作 Elasticsearch：从索引管理到复杂聚合查询"
date: 2026-04-11T07:00:00+08:00
draft: false
tags: ["Python", "Elasticsearch", "ELK", "运维", "自动化"]
categories: ["编程"]
description: "系统介绍 elasticsearch-py 客户端的核心用法，涵盖索引管理、文档 CRUD、复杂查询、聚合统计以及运维常用脚本，并整理了生产环境中常见的踩坑经验。"
summary: "从客户端初始化到批量操作、scroll 查询、聚合统计，一篇文章搞定 Python 操作 Elasticsearch 的高频场景。"
toc: true
math: false
diagram: false
keywords: ["Python", "Elasticsearch", "elasticsearch-py", "bulk", "scroll", "聚合查询", "索引管理"]
params:
  reading_time: true
---

Elasticsearch 是 ELK 体系的核心存储与检索引擎，日志分析、全文搜索、监控数据聚合都少不了它。在运维自动化场景中，经常需要用 Python 直接操作 ES：清理过期索引、统计各索引大小、批量导出数据。这篇文章把我日常用到的模式都整理出来，踩过的坑也一并记录。

## 客户端初始化

安装官方客户端：

```bash
pip install elasticsearch==8.x.x   # 版本要与 ES 服务端大版本对齐
```

最基础的初始化：

```python
from elasticsearch import Elasticsearch

es = Elasticsearch(
    hosts=["https://es-host:9200"],
    basic_auth=("elastic", "your_password"),
    ca_certs="/path/to/http_ca.crt",   # 开启 TLS 时需要
    request_timeout=30,
    retry_on_timeout=True,
    max_retries=3,
)

# 验证连接
info = es.info()
print(info["version"]["number"])
```

生产环境几个关键参数要留意：

- `request_timeout`：单次请求超时，默认 10 秒，批量写入时要调大
- `retry_on_timeout=True`：超时自动重试，搭配 `max_retries` 使用
- `sniff_on_start`：ES 7.x 支持，8.x 已移除，不要用
- 连接池：客户端内部维护连接池，不需要手动管理，但程序退出时应调用 `es.close()`

如果 ES 部署在 K8s 内部且不开 TLS，用 HTTP 更简单：

```python
es = Elasticsearch(
    hosts=["http://elasticsearch-svc:9200"],
    request_timeout=30,
)
```

---

## 索引操作

### 创建索引并指定 Mapping

```python
index_name = "app-logs-2026.04"

mapping = {
    "mappings": {
        "properties": {
            "timestamp": {"type": "date"},
            "level":     {"type": "keyword"},
            "service":   {"type": "keyword"},
            "message":   {"type": "text", "analyzer": "standard"},
            "duration_ms": {"type": "long"},
        }
    },
    "settings": {
        "number_of_shards": 3,
        "number_of_replicas": 1,
        "index.refresh_interval": "5s",
    }
}

if not es.indices.exists(index=index_name):
    es.indices.create(index=index_name, body=mapping)
    print(f"索引 {index_name} 创建成功")
```

### 查看 Mapping

```python
resp = es.indices.get_mapping(index="app-logs-*")
for idx, meta in resp.items():
    print(f"--- {idx} ---")
    for field, props in meta["mappings"]["properties"].items():
        print(f"  {field}: {props.get('type', 'object')}")
```

### 删除索引

```python
def delete_index(es, index_pattern: str, dry_run: bool = True):
    """删除匹配通配符的索引，dry_run=True 时只打印不执行"""
    indices = list(es.indices.get(index=index_pattern).keys())
    indices.sort()
    print(f"待删除索引（共 {len(indices)} 个）：")
    for idx in indices:
        print(f"  {idx}")
    if not dry_run:
        es.indices.delete(index=index_pattern)
        print("删除完成")
```

---

## 文档 CRUD

### 写入单条文档

```python
doc = {
    "timestamp": "2026-04-11T08:00:00+08:00",
    "level": "ERROR",
    "service": "payment",
    "message": "database connection timeout",
    "duration_ms": 5000,
}

resp = es.index(index="app-logs-2026.04", id="doc-001", document=doc)
print(resp["result"])  # created / updated
```

### 获取和更新文档

```python
# 获取
doc = es.get(index="app-logs-2026.04", id="doc-001")
print(doc["_source"])

# 局部更新（不覆盖整个文档）
es.update(
    index="app-logs-2026.04",
    id="doc-001",
    doc={"duration_ms": 6000},
)

# 删除
es.delete(index="app-logs-2026.04", id="doc-001")
```

### bulk 批量写入

批量写入是性能关键，生产环境单条 index 调用会被放大成严重瓶颈：

```python
from elasticsearch.helpers import bulk, BulkIndexError

def bulk_index(es, index: str, docs: list[dict]):
    actions = [
        {
            "_index": index,
            "_id": doc.get("id"),   # 没有就让 ES 自动生成
            "_source": doc,
        }
        for doc in docs
    ]
    try:
        success, errors = bulk(es, actions, raise_on_error=False, stats_only=False)
        print(f"成功: {success} 条")
        if errors:
            print(f"失败: {len(errors)} 条")
            for err in errors[:5]:   # 只打印前 5 条避免日志爆炸
                print(err)
    except BulkIndexError as e:
        print(f"bulk 整体失败: {e}")
```

`bulk()` 的 `chunk_size` 默认 500，数据量很大时可以适当调小，避免单个请求体超过 ES 的 `http.max_content_length`（默认 100MB）。

---

## 查询

### match 全文检索

```python
resp = es.search(
    index="app-logs-*",
    query={
        "match": {
            "message": "connection timeout"
        }
    },
    size=20,
    sort=[{"timestamp": {"order": "desc"}}],
)

for hit in resp["hits"]["hits"]:
    print(hit["_score"], hit["_source"]["message"])
```

### bool 组合查询

实际场景里几乎都是多条件组合：

```python
query = {
    "bool": {
        "must": [
            {"term": {"level": "ERROR"}},
            {"term": {"service": "payment"}},
        ],
        "filter": [
            {
                "range": {
                    "timestamp": {
                        "gte": "2026-04-10T00:00:00+08:00",
                        "lte": "2026-04-11T00:00:00+08:00",
                    }
                }
            }
        ],
        "must_not": [
            {"match": {"message": "timeout retry success"}}
        ],
    }
}

resp = es.search(index="app-logs-*", query=query, size=100)
print(f"命中总数: {resp['hits']['total']['value']}")
```

`must` 影响相关性得分，`filter` 不影响得分但会走缓存，纯过滤条件放 `filter` 性能更好。

### 聚合统计

统计各服务的错误数量，并计算平均响应时间：

```python
resp = es.search(
    index="app-logs-*",
    query={"term": {"level": "ERROR"}},
    size=0,   # 只要聚合结果，不要原始文档
    aggs={
        "by_service": {
            "terms": {
                "field": "service",
                "size": 20,
            },
            "aggs": {
                "avg_duration": {
                    "avg": {"field": "duration_ms"}
                }
            }
        }
    },
)

for bucket in resp["aggregations"]["by_service"]["buckets"]:
    svc = bucket["key"]
    count = bucket["doc_count"]
    avg_ms = bucket["avg_duration"]["value"] or 0
    print(f"{svc}: {count} 次错误，平均耗时 {avg_ms:.0f}ms")
```

---

## 运维实用脚本

### 批量删除 N 天前的日志索引

```python
import re
from datetime import datetime, timedelta

def cleanup_old_indices(es, prefix: str = "app-logs-", keep_days: int = 30):
    """删除超过 keep_days 天的日志索引（格式：prefix-YYYY.MM.DD）"""
    cutoff = datetime.utcnow() - timedelta(days=keep_days)
    pattern = re.compile(rf"^{re.escape(prefix)}(\d{{4}}\.\d{{2}}\.\d{{2}})$")

    all_indices = list(es.indices.get(index=f"{prefix}*").keys())
    to_delete = []

    for idx in all_indices:
        m = pattern.match(idx)
        if not m:
            continue
        idx_date = datetime.strptime(m.group(1), "%Y.%m.%d")
        if idx_date < cutoff:
            to_delete.append(idx)

    if not to_delete:
        print("没有需要清理的索引")
        return

    print(f"即将删除 {len(to_delete)} 个索引：")
    for idx in sorted(to_delete):
        print(f"  {idx}")

    confirm = input("确认删除？(yes/no): ")
    if confirm.strip().lower() == "yes":
        es.indices.delete(index=",".join(to_delete))
        print("清理完成")
```

### 统计各索引大小

```python
def show_index_stats(es, pattern: str = "*"):
    stats = es.indices.stats(index=pattern, metric="store")
    results = []
    for idx, data in stats["indices"].items():
        size_bytes = data["total"]["store"]["size_in_bytes"]
        doc_count  = data["total"]["docs"]["count"]
        results.append((idx, size_bytes, doc_count))

    results.sort(key=lambda x: x[1], reverse=True)
    print(f"{'索引名':<40} {'大小':>12} {'文档数':>12}")
    print("-" * 66)
    for idx, size, docs in results[:20]:
        size_mb = size / 1024 / 1024
        print(f"{idx:<40} {size_mb:>10.1f}M {docs:>12,}")
```

### 导出查询结果到 CSV

大量数据导出需要用 scroll 或 point-in-time（ES 8.x 推荐 PIT）：

```python
import csv
from elasticsearch.helpers import scan

def export_to_csv(es, index: str, query: dict, output_file: str):
    fieldnames = ["timestamp", "level", "service", "message", "duration_ms"]

    with open(output_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        count = 0
        for hit in scan(es, index=index, query={"query": query}, scroll="2m", size=1000):
            src = hit["_source"]
            writer.writerow({k: src.get(k, "") for k in fieldnames})
            count += 1
            if count % 10000 == 0:
                print(f"已导出 {count} 条...")

    print(f"导出完成，共 {count} 条 -> {output_file}")
```

`scan()` 是对 scroll API 的封装，会持续翻页直到耗尽结果集，不用手动维护 `scroll_id`。

---

## 踩坑记录

**bulk 操作的错误处理**

`bulk()` 默认 `raise_on_error=True`，一旦有文档写入失败就抛异常，整批都会中断。生产环境建议设为 `False`，自己遍历 errors 列表处理失败文档，否则单条数据格式问题会导致整批丢失。

**scroll 查询大数据量**

`scroll` 参数是 scroll context 的存活时间（如 `"2m"`），不是整个查询的超时。数据量非常大时（千万级），每次 `_search/scroll` 之间不能超过这个时间。另外，scroll context 会占用 ES heap，完成后记得调用 `es.clear_scroll(scroll_id=sid)` 释放。ES 8.x 的 PIT + search_after 方案性能更好，推荐新项目用 PIT 替代 scroll。

**连接泄漏**

`Elasticsearch` 对象内部用 `urllib3` 连接池，正常 long-lived 进程里复用单个 `es` 实例即可。常见错误是在每次函数调用里 `new Elasticsearch()`，连接用完不释放，最终导致 `connection pool is full, discarding connection` 警告甚至请求失败。建议把 `es` 实例做成全局单例或依赖注入。

**Mapping 字段动态推断**

ES 默认开启 dynamic mapping，第一条写入的文档会决定字段类型。如果同一字段后续出现了类型冲突（比如先写入 string，后写入 number），会导致文档写入失败。生产环境建议显式定义 mapping，或者把 `dynamic` 设为 `strict` 来强制校验。

**版本对应**

elasticsearch-py 的主版本必须和 ES 服务端对齐（7.x 客户端连 8.x 服务端会报兼容性错误）。升级服务端前先确认客户端库版本。
