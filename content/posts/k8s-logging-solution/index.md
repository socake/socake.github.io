---
title: "Kubernetes 日志采集方案选型：从技术对比到生产落地"
date: 2025-02-25T11:01:00+08:00
draft: false
tags: ["Kubernetes", "日志", "EFK", "Loki", "可观测性", "运维"]
categories: ["Kubernetes"]
description: "从零搭建 K8s 日志系统的决策全过程，涵盖采集器选型、存储对比、DaemonSet vs Sidecar 部署模式，以及 EFK 生产落地的真实踩坑经验。"
summary: "记录我们团队从无到有建立 Kubernetes 日志采集系统的完整历程，最终选择 Fluent Bit + Fluentd + Elasticsearch 方案的技术依据，以及生产环境踩过的那些坑。"
toc: true
math: false
diagram: false
series: ["K8s 完全指南"]
keywords: ["Kubernetes日志", "EFK", "Fluent Bit", "Fluentd", "Elasticsearch", "Loki", "日志采集"]
params:
  reading_time: true
---

## 背景：一次被迫提速的日志系统建设

去年我们的微服务数量从十几个增长到将近五十个，分布在三套 EKS 集群上。那段时间有一次线上故障，某个服务在凌晨报 500，oncall 的同事需要翻查日志，结果发现每个服务只有 `kubectl logs` 可用，容器重启之后日志就丢了。那次故障定位花了将近三小时，其中两个小时是在找日志。

那之后日志系统建设被提上了高优先级。这篇文章记录我们整个选型和落地的过程，包括中间踩过的坑。

---

## 整体架构设计思路

在做技术选型之前，先把需求梳理清楚：

1. **日志不丢失**：容器重启或节点替换后，历史日志要能查到
2. **延迟可接受**：允许分钟级延迟，不需要实时
3. **多集群统一入口**：三套集群的日志要能在同一个地方查
4. **资源占用可控**：采集 Agent 不能抢占业务资源
5. **运维成本低**：团队只有 3 个 DevOps，不想维护太复杂的系统

基于这些约束，日志采集链路可以抽象为三层：

```
Pod/容器日志
    ↓
采集层（Agent）
    ↓
处理/聚合层（可选）
    ↓
存储层
    ↓
查询/展示层
```

每一层都有多个候选方案，下面逐层分析。

---

## 采集器选型

### 主要候选

目前 K8s 生态里比较成熟的采集器有四个：

**Fluent Bit**：C 语言编写，内存占用极低，官方给的数据是约 450KB 内存、不到 1% CPU。功能相对单一，主要做采集和基础过滤，复杂的数据处理能力不如 Fluentd。

**Fluentd**：Ruby 编写，生态丰富，插件系统完善。内存占用比 Fluent Bit 高一个数量级，通常在 40-100MB 左右，但数据处理和路由能力很强。

**Filebeat**：Elastic 家的产品，和 Elasticsearch 天然集成，配置直观。但它不支持太复杂的数据转换，灵活性不如 Fluentd。

**Vector**：Rust 编写，性能很好，近几年发展很快。但生产验证案例还不算多，我们当时评估后认为风险偏高。

我们最终决定用 **Fluent Bit + Fluentd 的双层架构**：Fluent Bit 以 DaemonSet 形式部署在每个节点做轻量采集，Fluentd 作为聚合层做数据处理和缓冲。

这个选择的核心理由是：把轻量和强处理能力分开，Fluent Bit 不占用业务资源，Fluentd 集中处理可以复用缓冲，减少对 ES 的直接压力。

---

## 部署模式：DaemonSet vs Sidecar

这是架构层面最重要的决策之一，两种模式有本质区别。

### DaemonSet 模式

DaemonSet 部署一个 Agent 在每个 Node 上，读取宿主机的 `/var/log/containers/` 目录，采集该节点所有 Pod 的日志。

优点很明显：资源复用效率高，一个 Agent 服务整个节点；运维管理简单，只需维护一套 DaemonSet。

缺点是所有 Pod 的日志都是混在一起的容器标准输出，如果应用把日志写到了容器内某个文件里而不是 stdout，DaemonSet 模式就采集不到。

### Sidecar 模式

在每个 Pod 里注入一个 Sidecar 容器，专门采集主容器的日志，通过 emptyDir 共享卷读取日志文件。

优点是可以处理写文件的场景，也可以给不同 Pod 做完全独立的日志配置。

缺点是资源消耗翻倍，每个 Pod 多一个容器；而且如果 Pod 数量很多，维护成本会线性增长。

### 我们的选择

我们绝大多数服务都是云原生应用，日志输出遵循 12-Factor 规范，直接写 stdout。少数几个遗留服务写文件，这部分我们通过改造应用把日志导到 stdout 来解决，而不是引入 Sidecar 的复杂性。

**结论：统一用 DaemonSet，要求所有服务日志必须输出到 stdout/stderr。**

这个决策省了大量运维成本，事实证明是对的。

---

## 存储层选型对比

采集层确定之后，存储层是另一个重要决策。我们重点评估了三个方案：

| 维度 | Elasticsearch | Loki | ClickHouse |
|------|---------------|------|------------|
| 查询能力 | 全文检索，非常强 | 标签过滤 + LogQL，中等 | SQL 查询，需要定义 schema |
| 写入性能 | 较高，需要倒排索引 | 低，只索引标签 | 极高，列存压缩率好 |
| 存储成本 | 高（倒排索引本身很大） | 低 | 低 |
| 运维难度 | 高（JVM 调优、分片管理） | 低 | 中 |
| 生态成熟度 | 非常成熟 | 中等，Grafana 强依赖 | 较成熟但日志场景偏少 |
| 非结构化日志 | 支持良好 | 一般 | 需要结构化 |
| 团队熟悉度 | 高 | 低 | 低 |

Loki 的架构很轻量，资源占用确实低，但它的查询模型是基于标签的，对于我们需要做大量关键字检索（比如搜 traceId、error message）的场景，表现不够好。Loki 更适合"日志量巨大但查询需求简单"的场景。

ClickHouse 在分析场景下性能很好，但需要日志是结构化的，而我们有大量 Java 服务输出的是半结构化日志，改造成本高。

最终选择 Elasticsearch。虽然运维成本高，但我们团队有 ES 经验，查询能力是我们最核心的需求，这个取舍值得。

---

## 生产配置详解

### Fluent Bit DaemonSet 核心配置

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
        HTTP_Server   On
        HTTP_Listen   0.0.0.0
        HTTP_Port     2020

    [INPUT]
        Name              tail
        Tag               kube.*
        Path              /var/log/containers/*.log
        Parser            docker
        DB                /var/log/flb_kube.db
        Mem_Buf_Limit     50MB
        Skip_Long_Lines   On
        Refresh_Interval  10

    [FILTER]
        Name                kubernetes
        Match               kube.*
        Kube_URL            https://kubernetes.default.svc:443
        Kube_CA_File        /var/run/secrets/kubernetes.io/serviceaccount/ca.crt
        Kube_Token_File     /var/run/secrets/kubernetes.io/serviceaccount/token
        Kube_Tag_Prefix     kube.var.log.containers.
        Merge_Log           On
        Merge_Log_Key       log_processed
        Keep_Log            Off
        K8S-Logging.Parser  On
        K8S-Logging.Exclude On
        Labels              On
        Annotations         Off

    [FILTER]
        Name    grep
        Match   kube.*
        Exclude $kubernetes['namespace_name'] logging

    [OUTPUT]
        Name          forward
        Match         kube.*
        Host          fluentd-aggregator.logging.svc.cluster.local
        Port          24224
        Retry_Limit   False

  parsers.conf: |
    [PARSER]
        Name        docker
        Format      json
        Time_Key    time
        Time_Format %Y-%m-%dT%H:%M:%S.%L
        Time_Keep   On
```

几个关键配置说明：

- `Mem_Buf_Limit 50MB`：限制每个 tail 插件的内存用量，防止日志突增把节点内存吃满
- `DB /var/log/flb_kube.db`：使用 SQLite 记录读取位点，容器重启后从上次位置续读
- `Exclude $kubernetes['namespace_name'] logging`：过滤掉 logging 命名空间自身的日志，避免循环采集
- `K8S-Logging.Exclude On`：支持 Pod 通过 annotation 主动排除自身日志采集

### Fluentd 聚合层配置

```ruby
# fluentd.conf 核心片段
<source>
  @type forward
  port 24224
  bind 0.0.0.0
</source>

<filter kube.**>
  @type record_transformer
  enable_ruby true
  <record>
    cluster_name "#{ENV['CLUSTER_NAME']}"
    @timestamp ${time.strftime('%Y-%m-%dT%H:%M:%S.%3NZ')}
  </record>
</filter>

<match kube.**>
  @type elasticsearch
  host "#{ENV['ES_HOST']}"
  port 9200
  scheme https
  ssl_verify true
  user "#{ENV['ES_USER']}"
  password "#{ENV['ES_PASSWORD']}"
  
  index_name fluentd-${record['kubernetes']['namespace_name']}-%Y.%m.%d
  
  <buffer tag,time>
    @type file
    path /var/log/fluentd-buffers/kubernetes.system.buffer
    flush_mode interval
    retry_type exponential_backoff
    flush_thread_count 2
    flush_interval 5s
    retry_forever true
    retry_max_interval 30
    chunk_limit_size 8M
    total_limit_size 512M
    overflow_action block
  </buffer>
</match>
```

`overflow_action block` 这个配置很关键，后面踩坑部分会详细说。

---

## 生产踩坑记录

### 坑一：日志量突增导致 ES 写入背压

有一次我们做了一个大促，流量翻了五倍，日志量随之暴增。ES 集群的写入队列满了，开始拒绝请求，报 `429 Too Many Requests`。

当时 Fluentd 的 buffer 配置是默认的 `overflow_action drop_oldest`，结果丢失了大量日志，事后排查时完全看不到那个时间段的数据。

**解决方案：**

首先把 `overflow_action` 改成 `block`，这样 buffer 满的时候 Fluentd 会停止接收新数据而不是丢弃，背压会传递到 Fluent Bit，Fluent Bit 的 `Mem_Buf_Limit` 会触发，最终的代价是采集延迟增加，但日志不丢失。

其次把 `total_limit_size` 从 256M 调大到 512M，给更多的缓冲空间应对突发。

最后针对 ES 集群做了写入限流的自动扩容策略，在写入队列 utilization 超过 80% 时触发 data node 扩容。

```yaml
# ES 集群告警规则
- alert: ElasticsearchHighIndexingLatency
  expr: |
    elasticsearch_indices_indexing_index_time_seconds_total / 
    elasticsearch_indices_indexing_index_total > 0.1
  for: 5m
  annotations:
    summary: "ES 写入延迟过高，检查 bulk queue"
```

### 坑二：Fluentd buffer 磁盘写满

某个节点的 `/var/log/fluentd-buffers/` 目录把磁盘写满了，Fluentd Pod 直接 OOMKilled（其实是 buffer 写磁盘失败，但表现像是 OOM）。

原因是那个节点上恰好有一个异常的服务在死循环输出日志，Fluent Bit 采集速度远超 Fluentd 转发速度，buffer 文件持续增长。

**解决方案：**

把 buffer 目录挂载到独立的 PVC 上，和节点系统盘隔离：

```yaml
volumeMounts:
  - name: buffer
    mountPath: /var/log/fluentd-buffers
volumes:
  - name: buffer
    persistentVolumeClaim:
      claimName: fluentd-buffer-pvc
```

同时对 buffer 大小加了硬性上限，超过后直接丢弃最老的 chunk，接受少量数据丢失换取系统稳定性。

另外加了 Pod 日志速率限制，通过 Fluent Bit 的 throttle filter 对单个 Pod 的日志输出做限流：

```conf
[FILTER]
    Name          throttle
    Match         kube.*
    Rate          1000
    Window        5
    Print_Status  true
    Interval      30s
```

### 坑三：Kubernetes filter 权限问题

刚部署完发现 Fluent Bit 的 Kubernetes filter 不工作，日志里没有 Pod 元数据。查日志发现是 API Server 返回 403。

原因是忘记给 Fluent Bit 的 ServiceAccount 绑定相应的 RBAC 权限：

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: fluent-bit
rules:
  - apiGroups: [""]
    resources:
      - namespaces
      - pods
      - pods/logs
    verbs: ["get", "list", "watch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: fluent-bit
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: fluent-bit
subjects:
  - kind: ServiceAccount
    name: fluent-bit
    namespace: logging
```

---

## 不同规模的选型建议

经过这段时间的实践，总结一下针对不同规模的建议：

**小规模（< 10 个服务，单集群）**

直接用 Loki + Promtail + Grafana。资源占用极低，部署简单，Grafana 通吃监控和日志。如果已经有 Prometheus + Grafana，接入成本几乎为零。不需要复杂的全文检索，LogQL 足够用了。

**中规模（10-100 个服务，1-3 个集群）**

Fluent Bit（DaemonSet）+ Elasticsearch + Kibana。Fluentd 聚合层可以根据日志量决定是否需要，日志量不大的话 Fluent Bit 直接输出到 ES 也可以。ES 用托管服务（AWS OpenSearch 或 Elastic Cloud），避免自己管 JVM 调优。

**大规模（> 100 个服务，多集群）**

Fluent Bit + Fluentd 双层架构 + Elasticsearch。这时候 Fluentd 的缓冲和路由能力就非常重要了。ES 要做好分片规划，按 namespace 或服务名分 index，避免单一超大 index。可以考虑引入 Kafka 在 Fluentd 和 ES 之间做流量削峰。

日志采集系统看起来简单，实际上生产环境里细节很多。最重要的两点：日志不丢失（buffer 策略）和资源隔离（避免日志系统影响业务）。其他功能都可以迭代，这两点要在设计阶段就定好。
