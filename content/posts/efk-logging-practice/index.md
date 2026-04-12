---
title: "EFK 日志系统实战：Fluent Bit + Fluentd + Elasticsearch 完整部署"
date: 2026-04-11T10:00:00+08:00
draft: false
tags: ["EFK", "Elasticsearch", "Fluent Bit", "Fluentd", "日志", "Kubernetes"]
categories: ["可观测性"]
description: "从零开始搭建一套生产可用的 EFK 日志系统，详解 Fluent Bit 采集层、Fluentd 聚合层、Elasticsearch 存储层的完整配置，以及实际遇到的踩坑和调优经验。"
summary: "讲清楚为什么要 Fluent Bit + Fluentd 两层架构，给出可直接参考的完整 ConfigMap 配置和 ES 索引模板设计。"
toc: true
math: false
diagram: false
series: ["ELK Stack 完全手册"]
keywords: ["EFK", "Fluent Bit", "Fluentd", "Elasticsearch", "Kubernetes 日志", "日志采集", "Kibana"]
params:
  reading_time: true
---

## 为什么是 Fluent Bit + Fluentd 两层架构

最直接的问题：为什么不直接用 Fluent Bit 写 Elasticsearch？

Fluent Bit 是 Fluentd 的"轻量版"，内存占用极低（典型运行时 ~5MB），适合部署成 DaemonSet 跑在每个节点上。但它在数据处理能力上有限制：复杂的正则解析、多路由逻辑、灵活的 buffer 配置，Fluent Bit 做起来要么性能有损耗，要么配置很麻烦。

Fluentd 是 Ruby 实现的，内存占用大得多（几十到几百 MB），但插件生态极其丰富，对 Elasticsearch 的写入支持（bulk API、自动创建 index、retry 逻辑）非常成熟。

**两层架构的职责分离：**

- **Fluent Bit（DaemonSet）**：负责采集，轻量，低开销，处理节点本地的日志文件 tail，做基础的 K8s 元数据 enrichment，然后通过 Forward 协议把数据转发给 Fluentd。
- **Fluentd（Deployment）**：负责聚合和处理，集中做 JSON 解析、字段映射、添加环境标签，然后批量写入 Elasticsearch。

这个架构还有一个好处：Fluentd 可以独立扩缩容，而不需要动 DaemonSet。当 ES 写入压力大时，直接给 Fluentd 加副本就行。

整体数据流：

```
节点上的 /var/log/containers/*.log
    ↓（tail）
Fluent Bit DaemonSet（K8s enrichment → Forward）
    ↓（Forward 协议，TCP）
Fluentd Deployment（JSON 解析 → 打标签 → Buffer）
    ↓（bulk API）
Elasticsearch 集群
    ↓
Kibana / Grafana（查询展示）
```

---

## Fluent Bit 配置

Fluent Bit 通过 ConfigMap 挂载配置，DaemonSet 需要挂载节点的 `/var/log` 目录。

### ConfigMap

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: fluent-bit-config
  namespace: logging
data:
  fluent-bit.conf: |
    [SERVICE]
        Flush         5
        Daemon        Off
        Log_Level     info
        Parsers_File  parsers.conf
        # 使用文件记录每个日志文件读到的位置，Pod 重启后不会重复采集
        storage.path  /var/log/flb-storage/
        storage.sync  normal
        storage.checksum off
        storage.max_chunks_up 128

    [INPUT]
        Name              tail
        Tag               kube.*
        Path              /var/log/containers/*.log
        # 排除系统组件和日志采集本身的日志，避免日志风暴
        Exclude_Path      /var/log/containers/fluent-bit*,/var/log/containers/fluentd*
        Parser            docker
        DB                /var/log/flb_kube.db
        Mem_Buf_Limit     50MB
        Skip_Long_Lines   On
        Refresh_Interval  10
        Rotate_Wait       30

    [FILTER]
        Name                kubernetes
        Match               kube.*
        Kube_URL            https://kubernetes.default.svc:443
        Kube_CA_File        /var/run/secrets/kubernetes.io/serviceaccount/ca.crt
        Kube_Token_File     /var/run/secrets/kubernetes.io/serviceaccount/token
        Merge_Log           On          # 把 log 字段里的 JSON 解析合并到顶层
        Merge_Log_Key       log_processed
        Keep_Log            Off         # 合并后删除原始 log 字段
        K8S-Logging.Parser  On          # 支持 Pod annotation 指定 parser
        K8S-Logging.Exclude On          # 支持 Pod annotation 排除某些日志
        # 自动添加以下字段：
        # kubernetes.namespace_name, kubernetes.pod_name
        # kubernetes.container_name, kubernetes.labels.*

    [FILTER]
        Name    modify
        Match   kube.*
        # 添加节点名，方便排查节点级别的问题
        Add     node_name ${NODE_NAME}
        # 添加集群标识（通过环境变量注入）
        Add     cluster ${CLUSTER_NAME}

    [OUTPUT]
        Name          forward
        Match         kube.*
        Host          fluentd.logging.svc.cluster.local
        Port          24224
        # 连接失败时的重试配置
        Retry_Limit   10
        # 开启 TLS（如果 Fluentd 侧也配了 TLS）
        # tls         on
        # tls.verify  off

  parsers.conf: |
    # Docker 格式日志（containerd 输出）
    [PARSER]
        Name        docker
        Format      json
        Time_Key    time
        Time_Format %Y-%m-%dT%H:%M:%S.%L
        Time_Keep   Off
        Decode_Field_As escaped_utf8 log do_next
        Decode_Field_As json log
    
    # containerd/CRI 格式
    [PARSER]
        Name        cri
        Format      regex
        Regex       ^(?<time>[^ ]+) (?<stream>stdout|stderr) (?<logtag>[^ ]*) (?<log>.*)$
        Time_Key    time
        Time_Format %Y-%m-%dT%H:%M:%S.%L%z
```

### DaemonSet

```yaml
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: fluent-bit
  namespace: logging
spec:
  selector:
    matchLabels:
      app: fluent-bit
  template:
    metadata:
      labels:
        app: fluent-bit
    spec:
      serviceAccountName: fluent-bit
      tolerations:
        - key: node-role.kubernetes.io/master
          effect: NoSchedule
      containers:
        - name: fluent-bit
          image: fluent/fluent-bit:3.2
          resources:
            requests:
              cpu: "50m"
              memory: "64Mi"
            limits:
              cpu: "200m"
              memory: "256Mi"
          env:
            - name: NODE_NAME
              valueFrom:
                fieldRef:
                  fieldPath: spec.nodeName
            - name: CLUSTER_NAME
              value: "production-us"
          volumeMounts:
            - name: varlog
              mountPath: /var/log
            - name: config
              mountPath: /fluent-bit/etc/
            - name: flb-storage
              mountPath: /var/log/flb-storage
      volumes:
        - name: varlog
          hostPath:
            path: /var/log
        - name: config
          configMap:
            name: fluent-bit-config
        - name: flb-storage
          hostPath:
            path: /var/log/flb-storage
            type: DirectoryOrCreate
```

---

## Fluentd 配置

Fluentd 负责接收来自各节点 Fluent Bit 的数据，做处理后写入 ES。

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: fluentd-config
  namespace: logging
data:
  fluent.conf: |
    # 接收来自 Fluent Bit 的 Forward 数据
    <source>
      @type forward
      port 24224
      bind 0.0.0.0
    </source>

    # 过滤处理：解析 JSON 日志，处理嵌套字段
    <filter kube.**>
      @type record_transformer
      enable_ruby true
      <record>
        # 将 kubernetes.labels 里的 app 标签提取出来作为一级字段
        app_name ${record.dig("kubernetes", "labels", "app") || record.dig("kubernetes", "labels", "app.kubernetes.io/name") || "unknown"}
        # 统一时间戳格式
        @timestamp ${time.strftime('%Y-%m-%dT%H:%M:%S.%3NZ')}
      </record>
      # 删除重复的嵌套字段，减小文档体积
      remove_keys $.kubernetes.annotations
    </filter>

    # 解析应用层的 JSON 结构日志
    <filter kube.**>
      @type parser
      key_name log_processed
      reserve_data true
      remove_key_name_field true
      emit_invalid_record_to_error false  # 非 JSON 日志不报错，直接原样保留
      <parse>
        @type json
        time_key time
        time_format %Y-%m-%dT%H:%M:%S.%NZ
      </parse>
    </filter>

    # 按 namespace 路由到不同的 ES 索引
    # 系统 namespace 的日志单独存放，保留时间更短
    <match kube.var.log.containers.**kube-system**.log>
      @type elasticsearch
      host "#{ENV['ELASTICSEARCH_HOST']}"
      port "#{ENV['ELASTICSEARCH_PORT']}"
      scheme https
      user "#{ENV['ELASTICSEARCH_USER']}"
      password "#{ENV['ELASTICSEARCH_PASSWORD']}"
      ssl_verify false
      
      logstash_format true
      logstash_prefix k8s-system
      logstash_dateformat %Y.%m.%d
      
      <buffer>
        @type file
        path /var/log/fluentd-buffers/system
        flush_mode interval
        flush_interval 10s
        flush_thread_count 2
        chunk_limit_size 8MB
        total_limit_size 512MB
        retry_max_interval 30s
        retry_forever false
        retry_max_times 5
        overflow_action drop_oldest_chunk
      </buffer>
    </match>

    # 业务应用日志
    <match kube.**>
      @type elasticsearch
      host "#{ENV['ELASTICSEARCH_HOST']}"
      port "#{ENV['ELASTICSEARCH_PORT']}"
      scheme https
      user "#{ENV['ELASTICSEARCH_USER']}"
      password "#{ENV['ELASTICSEARCH_PASSWORD']}"
      ssl_verify false
      
      logstash_format true
      logstash_prefix k8s-app
      logstash_dateformat %Y.%m.%d
      
      # ILM（Index Lifecycle Management）索引策略名称
      # 需要在 ES 里提前创建
      ilm_policy_id k8s-app-ilm-policy
      ilm_policy_overwrite false
      
      # 每个文档写入前检查 index template 是否已创建
      template_name k8s-app-template
      template_file /fluentd/etc/index-template.json
      template_overwrite false
      
      <buffer tag, time>
        @type file
        path /var/log/fluentd-buffers/app
        timekey 1h              # 按小时分 chunk
        timekey_wait 10m        # 等待 10 分钟再 flush，等迟到数据
        flush_mode interval
        flush_interval 30s
        flush_thread_count 4
        chunk_limit_size 16MB
        total_limit_size 2GB
        retry_max_interval 60s
        retry_forever true      # 业务日志不丢，一直重试
        overflow_action block   # buffer 满了就阻塞，不丢数据（注意背压）
      </buffer>
    </match>
```

---

## Elasticsearch Index Template 设计

按日期滚动的 index template，配合 ILM 策略控制数据生命周期：

```json
{
  "index_patterns": ["k8s-app-*"],
  "template": {
    "settings": {
      "number_of_shards": 3,
      "number_of_replicas": 1,
      "index.lifecycle.name": "k8s-app-ilm-policy",
      "index.lifecycle.rollover_alias": "k8s-app",
      "index.codec": "best_compression",
      "index.refresh_interval": "30s"
    },
    "mappings": {
      "dynamic_templates": [
        {
          "labels_as_keywords": {
            "path_match": "kubernetes.labels.*",
            "mapping": {
              "type": "keyword",
              "ignore_above": 256
            }
          }
        }
      ],
      "properties": {
        "@timestamp":       { "type": "date" },
        "cluster":          { "type": "keyword" },
        "app_name":         { "type": "keyword" },
        "level":            { "type": "keyword" },
        "message":          { "type": "text", "analyzer": "standard" },
        "trace_id":         { "type": "keyword" },
        "span_id":          { "type": "keyword" },
        "kubernetes": {
          "properties": {
            "namespace_name": { "type": "keyword" },
            "pod_name":        { "type": "keyword" },
            "container_name":  { "type": "keyword" },
            "node_name":       { "type": "keyword" }
          }
        }
      }
    }
  }
}
```

ILM 策略示例（业务日志保留 30 天）：

```json
{
  "policy": {
    "phases": {
      "hot": {
        "min_age": "0ms",
        "actions": {
          "rollover": {
            "max_primary_shard_size": "50gb",
            "max_age": "1d"
          }
        }
      },
      "warm": {
        "min_age": "3d",
        "actions": {
          "shrink": { "number_of_shards": 1 },
          "forcemerge": { "max_num_segments": 1 }
        }
      },
      "cold": {
        "min_age": "14d",
        "actions": {
          "freeze": {}
        }
      },
      "delete": {
        "min_age": "30d",
        "actions": {
          "delete": {}
        }
      }
    }
  }
}
```

---

## Grafana Loki vs Kibana：各自的适合场景

我们的集群同时跑了 EFK 和 Loki 两套日志系统（历史遗留原因），两者使用下来各有侧重：

**Kibana（配合 Elasticsearch）适合的场景：**
- 需要全文搜索、模糊匹配（Elasticsearch 的 text 类型分析器很强）
- 审计日志、安全日志，需要长期保存和精确查询
- 复杂的聚合分析（按字段 group by、histogram、top N）
- 日志量大但查询模式固定，可以提前设计好 mapping

Kibana 的 KQL 查询语法比较直观，但 Dashboard 配置繁琐。

**Grafana Loki 适合的场景：**
- 实时监控和告警，配合 Prometheus 一起看
- 日志和指标的关联分析（在同一个 Grafana 面板里）
- 存储成本敏感（Loki 不做全文索引，只索引 label，存储成本低很多）
- 开发阶段快速排查，LogQL 的流处理管道很方便

Loki 的 LogQL 示例：

```logql
# 查某个服务的错误日志
{namespace="production", app="my-service"} |= "ERROR"

# 解析 JSON 日志并过滤
{namespace="production"} | json | level="error" | duration > 1000

# 统计每分钟错误数
rate({namespace="production"} |= "ERROR" [1m])
```

---

## 踩坑记录

### Fluentd Buffer 满了怎么办

我们遇到过 Elasticsearch 集群滚动重启导致写入暂时不可用，Fluentd 的 buffer 在几分钟内就写满了，触发了 `overflow_action block`，导致 Fluentd 开始背压 Fluent Bit，最终 Fluent Bit 的内存 buffer 也满了，开始丢日志。

**处理方案：**

首先要监控 buffer 使用率，在 Grafana 里配置告警（Fluentd 暴露了 Prometheus 指标）：

```
fluentd_output_status_buffer_total_bytes / fluentd_output_status_buffer_total_size > 0.8
```

其次，`total_limit_size` 要根据节点磁盘容量合理设置。我们把 Fluentd 的 buffer 目录挂在了一个独立的 PVC 上（50GB），避免和系统盘竞争。

最后，`retry_forever: true` 的配置要配合监控，不能就这样放着不管。当 retry 持续超过 30 分钟，说明下游有问题，需要人工介入。

### ES 索引分片过多导致集群变慢

上线初期每天生成的 index 数量 = namespace 数量 × 日期 × 3 个 shard，一个月下来光是 k8s-app-* 就有 2000+ 个分片。ES 集群的 master 节点 CPU 一直居高不下，查询也变慢了。

根因：每个 ES 分片在 JVM 堆内存里都有开销（约 500KB），2000 个分片就是 1GB 的堆内存只用来管理元数据。

解决方案分两步：

**短期**：删掉过期的历史 index，释放分片：
```bash
# 先查哪些 index 最老、分片最多
GET /_cat/indices/k8s-app-*?v&s=creation.date&h=index,pri,rep,docs.count,store.size,creation.date
# 删除 30 天前的
DELETE /k8s-app-2025.02.*
```

**长期**：用 ILM + Rollover 代替按日期固定分片的方案（即上面给出的 ILM 配置）。Rollover 根据 shard 大小（50GB）滚动，而不是按天，避免了"低流量日也要新建 3 个分片"的问题。同时 warm 阶段 shrink 到 1 个分片，大幅减少分片总数。

### Fluent Bit 采集延迟

刚上线时发现日志在 Grafana 里有 1-2 分钟的延迟，排查后发现是 Fluent Bit 的 `Refresh_Interval` 设置太长（默认 60s，我们改成了 10s），以及 `Flush` 间隔设置为 30s。

调整后：`Flush 5`（每 5 秒 flush 一次），`Refresh_Interval 10`，延迟降到了 10 秒以内，对于日志查询场景完全够用。

注意 `Mem_Buf_Limit` 要设合理，太小会在日志量突增时丢数据，太大会影响节点上其他 Pod 的内存。我们设的是 50MB，对应的磁盘 storage 限制是 512MB。
