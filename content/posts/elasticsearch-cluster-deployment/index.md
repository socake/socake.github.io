---
title: "Elasticsearch 集群部署实战：ECK 在 K8s 上的生产级配置"
date: 2026-04-11T08:00:00+08:00
draft: false
tags: ["Elasticsearch", "ELK", "Kubernetes", "ECK", "运维"]
categories: ["ELK Stack"]
description: "从集群角色规划到 ECK Operator 落地，结合生产环境踩坑经验，完整讲解 Elasticsearch 在 Kubernetes 上的生产级部署方案。"
summary: "从集群角色规划到 ECK Operator 落地，结合生产环境踩坑经验，完整讲解 Elasticsearch 在 Kubernetes 上的生产级部署方案。"
toc: true
math: false
diagram: false
keywords: ["Elasticsearch", "ECK", "Kubernetes", "集群部署", "ELK Stack"]
params:
  reading_time: true
---

最近在把公司日志平台从裸机 ES 集群迁移到 Kubernetes，趁这个机会把整个过程整理一下。裸机部署 ES 我已经做了两三年，节点配置、JVM 调优、集群扩容这些都有套路了，但迁到 K8s 之后遇到了不少新问题——主要是 ECK（Elastic Cloud on Kubernetes）这个 Operator 有自己的一套逻辑，和手动管 StatefulSet 差异很大。

## 为什么选 ECK 而不是手动 StatefulSet

这个问题在团队内部讨论了挺长时间。手动写 StatefulSet 的好处是完全可控，但 ES 集群的运维复杂度很高：

- 证书轮换：ES 8.x 默认强制开启 TLS，transport 层和 HTTP 层都要证书，手动管理几十个节点的证书极其麻烦
- 滚动升级：ES 的滚动升级顺序有要求（先升 master 节点，再升 data 节点），StatefulSet 原生的滚动更新策略不理解这个约束
- 配置变更：修改 JVM 参数或者 ES 配置需要重启 Pod，ECK 会自动处理这个过程并确保集群健康
- 快照生命周期：ECK 可以直接管理 SLM（Snapshot Lifecycle Management）策略

花了一周评估之后，决定用 ECK。主要原因是 Elastic 官方维护，和 ES 版本绑定，兼容性有保证，而且 CRD 设计得比较清晰。

## 集群角色规划

ES 的节点角色这块很多人踩过坑——早期版本里 `node.master: true` 和 `node.data: true` 直接写在配置里，ES 8.x 改成了 `node.roles` 数组，更灵活，但也容易搞混。

我们的日志平台需求：每天入库约 15GB 原始日志，热数据保留 7 天，温数据保留 23 天，冷存档 30 天。基于这个需求规划节点：

**Master 节点（3 台）**

只负责集群元数据管理，不存数据不处理查询。必须奇数台（3 或 5），防止脑裂。配置很低——2 核 4G 足够，但内存不能省，因为 Master 节点要维护整个集群的状态（所有索引的 mapping、分片路由表），集群越大这个开销越高。

**Coordinating 节点（2 台）**

这是很多小集群忽略的角色。Coordinating 节点不存数据，专门负责接收客户端请求、把查询分发到数据节点、聚合结果返回。好处是把数据节点从繁重的聚合计算里解放出来，特别是做大范围日志检索的时候效果明显。我们加了两台 4 核 8G 的 Coordinating 节点之后，p99 查询延迟从 3s 降到了 800ms。

**Ingest 节点（2 台）**

处理写入前的 Pipeline 转换，比如 geoip 解析、日志字段提取。可以和 Master 节点合并，但如果 ingest pipeline 逻辑复杂、写入量大，建议独立出来。

**Hot 数据节点（3 台）**

SSD 存储，高 CPU，处理最新的写入和频繁查询。按照 30:1 的磁盘内存比估算，7 天数据约 308GB（含副本 616GB），3 台节点各需要约 210GB SSD。

**Warm 数据节点（2 台）**

HDD 存储，高内存，存放 7-30 天的历史数据。磁盘内存比可以放大到 100:1，2 台节点各约 600GB HDD，内存 16G。

**Cold 数据节点（1 台）**

最便宜的存储，冷存档数据通常 0 副本，只要能查就行，不要求性能。

## ECK Operator 安装

ECK 分两部分：Operator 本身和 CRD。官方推荐的安装方式：

```bash
# 安装 CRD
kubectl create -f https://download.elastic.co/downloads/eck/2.13.0/crds.yaml

# 安装 Operator
kubectl apply -f https://download.elastic.co/downloads/eck/2.13.0/operator.yaml
```

Operator 会部署在 `elastic-system` namespace，它会 watch 所有 namespace 下的 Elasticsearch、Kibana、Agent 等 CRD。

验证安装：

```bash
kubectl -n elastic-system logs -f statefulset.apps/elastic-operator
```

看到 `starting up operator` 并且没有 error 就 OK。

**坑：Operator 权限不足**

在我们的 K8s 集群（开了 PodSecurityAdmission），ECK Operator 需要 `privileged` PSA 标签才能正常工作。如果 `elastic-system` namespace 没有打标签，Operator Pod 会一直 Pending：

```bash
kubectl label namespace elastic-system pod-security.kubernetes.io/enforce=privileged
```

## Elasticsearch CRD 配置详解

下面是我们生产环境的完整配置，拆开来讲：

```yaml
apiVersion: elasticsearch.k8s.elastic.co/v1
kind: Elasticsearch
metadata:
  name: es-logging
  namespace: logging
spec:
  version: 8.13.0
  
  # HTTP 配置：生产环境建议配置 LoadBalancer 或者 Ingress
  http:
    service:
      spec:
        type: ClusterIP
    tls:
      selfSignedCertificate:
        disabled: false  # 保持 TLS 开启，但用 ECK 自动生成的证书

  nodeSets:
    # Master 节点组
    - name: master
      count: 3
      config:
        node.roles: ["master"]
        cluster.name: es-logging
        # 重要：Master 节点不存数据
        xpack.security.enabled: true
      podTemplate:
        spec:
          initContainers:
            - name: sysctl
              securityContext:
                privileged: true
              command: ["sh", "-c", "sysctl -w vm.max_map_count=262144"]
          containers:
            - name: elasticsearch
              resources:
                requests:
                  memory: 4Gi
                  cpu: 1
                limits:
                  memory: 4Gi
                  cpu: 2
              env:
                - name: ES_JAVA_OPTS
                  value: "-Xms2g -Xmx2g"
      volumeClaimTemplates:
        - metadata:
            name: elasticsearch-data
          spec:
            accessModes: ["ReadWriteOnce"]
            resources:
              requests:
                storage: 10Gi  # Master 节点只需要存元数据
            storageClassName: gp3

    # Hot 数据节点组
    - name: data-hot
      count: 3
      config:
        node.roles: ["data", "data_content", "data_hot"]
        cluster.name: es-logging
        # 关闭自动索引创建，防止意外写入
        action.auto_create_index: ".monitoring-*,.watches,.triggered_watches,.watcher-history-*,.ml-*,logs-*,metrics-*,traces-*"
      podTemplate:
        spec:
          nodeSelector:
            node-type: es-hot  # 调度到 SSD 节点
          tolerations:
            - key: "es-hot"
              operator: "Exists"
              effect: "NoSchedule"
          initContainers:
            - name: sysctl
              securityContext:
                privileged: true
              command: ["sh", "-c", "sysctl -w vm.max_map_count=262144"]
          containers:
            - name: elasticsearch
              resources:
                requests:
                  memory: 16Gi
                  cpu: 4
                limits:
                  memory: 16Gi
                  cpu: 8
              env:
                - name: ES_JAVA_OPTS
                  value: "-Xms8g -Xmx8g"
      volumeClaimTemplates:
        - metadata:
            name: elasticsearch-data
          spec:
            accessModes: ["ReadWriteOnce"]
            resources:
              requests:
                storage: 500Gi
            storageClassName: gp3  # AWS EBS gp3，SSD

    # Warm 数据节点组
    - name: data-warm
      count: 2
      config:
        node.roles: ["data", "data_content", "data_warm"]
        cluster.name: es-logging
      podTemplate:
        spec:
          nodeSelector:
            node-type: es-warm
          containers:
            - name: elasticsearch
              resources:
                requests:
                  memory: 32Gi
                  cpu: 2
                limits:
                  memory: 32Gi
                  cpu: 4
              env:
                - name: ES_JAVA_OPTS
                  value: "-Xms16g -Xmx16g"
      volumeClaimTemplates:
        - metadata:
            name: elasticsearch-data
          spec:
            accessModes: ["ReadWriteOnce"]
            resources:
              requests:
                storage: 2Ti
            storageClassName: sc1  # AWS EBS sc1，HDD，便宜

    # Coordinating 节点组
    - name: coordinating
      count: 2
      config:
        node.roles: []  # 空数组 = coordinating only
        cluster.name: es-logging
      podTemplate:
        spec:
          containers:
            - name: elasticsearch
              resources:
                requests:
                  memory: 8Gi
                  cpu: 2
                limits:
                  memory: 8Gi
                  cpu: 4
              env:
                - name: ES_JAVA_OPTS
                  value: "-Xms4g -Xmx4g"
      volumeClaimTemplates:
        - metadata:
            name: elasticsearch-data
          spec:
            accessModes: ["ReadWriteOnce"]
            resources:
              requests:
                storage: 10Gi
            storageClassName: gp3
```

## JVM Heap 设置原则

这是 ES 运维里最常问的问题。核心规则两条：

**规则一：不超过物理内存的 50%**

ES 严重依赖 OS 文件系统缓存（Page Cache），Lucene 直接操作文件，如果 JVM 把内存全占了，Page Cache 没空间，磁盘 IO 会大幅增加。一般建议 JVM heap 占内存的 50%，剩下的留给 OS。

**规则二：不超过 32GB**

这是因为 JVM 的压缩指针（Compressed OOPs）优化。当堆小于 32GB 时，JVM 用 4 字节表示对象指针（实际上是 35 位地址空间），超过 32GB 之后退化成 64 位指针，每个对象额外多 4 字节，内存效率下降 ~10%，而且 GC 压力也会变大。

具体到 ECK，通过环境变量设置：

```yaml
env:
  - name: ES_JAVA_OPTS
    value: "-Xms8g -Xmx8g"
```

注意 `-Xms` 和 `-Xmx` 必须相等，避免运行时 heap 扩张带来的 GC 停顿。

**实际案例：** 我们曾经有台 64G 内存的节点，JVM 设了 `-Xms32g -Xmx32g`。结果集群经常出现 GC 告警，查了半天发现设置到 32G 刚好在临界点——有时候触发压缩指针，有时候不触发，行为不稳定。改成 30G 之后彻底稳定了。

## 集群健康度监控

ES 集群有三个健康状态：Green（全部正常）、Yellow（有未分配的副本分片）、Red（有未分配的主分片，部分数据不可用）。

关键监控指标：

```bash
# 查看集群健康
GET /_cluster/health?pretty

# 查看未分配分片原因
GET /_cluster/allocation/explain

# 查看节点资源使用
GET /_cat/nodes?v&h=name,heap.percent,ram.percent,cpu,load_1m,node.role
```

Prometheus 监控建议部署 `elasticsearch-exporter`，关注这几个指标：

- `elasticsearch_cluster_health_status`：集群状态（0=green, 1=yellow, 2=red）
- `elasticsearch_jvm_memory_used_bytes`：JVM 内存使用量，超过 75% 开始告警
- `elasticsearch_filesystem_data_free_bytes`：磁盘空间，低于 15% 触发告警（ES 默认在 85% 时停止写入）
- `elasticsearch_indices_indexing_index_time_seconds_total`：写入延迟

## 踩坑记录

**坑1：集群状态变 Yellow 后一直不恢复**

现象：某次节点重启后，集群状态从 Green 变 Yellow，等了很久没有自动恢复。

排查过程：

```bash
GET /_cluster/allocation/explain
{
  "index": "logs-app-2026.04.01",
  "shard": 2,
  "primary": false
}
```

返回结果显示 `"decider": "same_shard"`，原因是副本分片和主分片被分配到同一个节点了。这发生在节点数量不足的情况下——我们临时缩容了一台热节点，导致 3 个副本要分配到 2 个节点，某些分片只能"主副同节点"被拒绝。

解决：临时调整副本数或者把节点加回来。

**坑2：分片分配失败，磁盘水位告警**

现象：新索引写入失败，报错 `blocked by: [FORBIDDEN/12/index read-only / allow delete (api)]`。

原因：节点磁盘使用率超过 85%（ES 默认 high watermark），ES 自动将索引设为只读。

紧急处理：

```bash
# 临时解除只读限制（先腾出磁盘空间再执行，否则治标不治本）
PUT /logs-app-2026.04.01/_settings
{
  "index.blocks.read_only_allow_delete": null
}

# 调整水位（临时）
PUT /_cluster/settings
{
  "transient": {
    "cluster.routing.allocation.disk.watermark.low": "88%",
    "cluster.routing.allocation.disk.watermark.high": "90%",
    "cluster.routing.allocation.disk.watermark.flood_stage": "95%"
  }
}
```

根本解决：ILM 策略要设置好，确保数据按时 rollover 和迁移到 warm/cold 节点。这个问题在下一篇文章里会详细讲。

**坑3：ECK 滚动重启卡住**

现象：更新 ES 配置后，ECK 触发了滚动重启，但其中一个 Pod 一直卡在 `Terminating` 状态，整个滚动更新停在那里不动了。

排查：

```bash
kubectl describe pod es-logging-data-hot-1 -n logging
```

发现是 PreStop Hook 超时——默认 `terminationGracePeriodSeconds` 是 30s，但 ES 节点在关闭时需要等待分片迁移完成，30s 远远不够。

解决：在 podTemplate 里设置更长的优雅终止时间：

```yaml
podTemplate:
  spec:
    terminationGracePeriodSeconds: 300  # 5 分钟
```

ECK 官方建议对数据节点设置 5-10 分钟，取决于分片大小。

**坑4：OOM Killed**

现象：数据节点频繁被 OOM Killed，K8s 日志里看到 `OOMKilled`。

原因一：JVM 参数设置了 `-Xms16g -Xmx16g`，但 K8s resources.limits.memory 也是 16Gi，没有给 JVM 堆之外的内存留空间。JVM 除了 heap 还有 direct memory、metaspace、stack 等，加上 OS 开销，实际需要比 heap 多 2-3G。

解决：`resources.limits.memory` 至少要比 JVM heap 大 2G：

```yaml
resources:
  limits:
    memory: 18Gi  # heap 16G，额外留 2G
env:
  - name: ES_JAVA_OPTS
    value: "-Xms16g -Xmx16g"
```

原因二：ES 8.x 默认开启了 `xpack.ml.enabled: true`，机器学习功能会占用额外内存。如果不用 ML 功能，直接关掉：

```yaml
config:
  xpack.ml.enabled: false
```

## 生产就绪 Checklist

部署完成后，对照这个清单验证：

```bash
# 1. 集群状态 Green
GET /_cluster/health

# 2. 所有节点角色正确
GET /_cat/nodes?v&h=name,node.role,heap.percent

# 3. 没有未分配分片
GET /_cat/shards?h=index,shard,prirep,state,node | grep -v STARTED

# 4. 磁盘使用率健康
GET /_cat/allocation?v

# 5. 确认 ILM 策略已配置（见下篇）
GET /_ilm/policy
```

ECK 在 K8s 日志平台的实践已经跑了半年，整体稳定性比裸机部署好很多，主要是证书管理和滚动升级这两块省了大量运维工作。下一篇会讲索引策略和 ILM 配置，这是 ES 长期稳定运行的关键。
