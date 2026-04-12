---
title: "Vector 日志处理管道：高性能日志采集与转换实践"
date: 2025-10-14T11:01:00+08:00
draft: false
tags: ["Vector", "日志", "ELK", "运维", "Rust"]
categories: ["ELK Stack"]
description: "从架构对比到 K8s DaemonSet 落地，结合 VRL 实战示例和踩坑经验，讲透 Vector 在日志采集管道中的应用。"
summary: "从架构对比到 K8s DaemonSet 落地，结合 VRL 实战示例和踩坑经验，讲透 Vector 在日志采集管道中的应用。"
toc: true
math: false
diagram: false
series: ["ELK Stack 完全手册"]
keywords: ["Vector", "日志采集", "VRL", "Kubernetes", "Logstash替代"]
params:
  reading_time: true
---

在搭建日志平台的时候，日志采集和处理这一层选型往往被忽视，大家都盯着 ES 怎么配置，结果把一个 Logstash 堆上去，跑了一段时间发现它吃掉了跟 ES 差不多的资源。我们从 Logstash 切到 Vector 大概是一年半前的事，现在回头看，这是整个日志平台改造里性价比最高的一次决定——资源占用降了 60%，处理延迟从秒级降到毫秒级，而且配置更简洁。

## Vector 是什么

Vector 是用 Rust 编写的可观测性数据管道，定位是替代 Logstash、Fluentd 等传统日志处理工具，同时也能处理 Metrics 和 Traces。官方号称比 Logstash 快 10 倍，从我们的实测数据来看接近这个数字。

核心架构很简单：**Sources（数据来源） → Transforms（数据转换） → Sinks（数据输出）**，每个组件都是独立的，组合起来构成数据流。

## Vector vs Logstash vs Fluentd

选型时整理了一份对比，省去大家再去查资料：

| 维度 | Vector | Logstash | Fluentd | FluentBit |
|------|--------|----------|---------|-----------|
| 语言 | Rust | Java | Ruby | C |
| 内存占用 | ~50MB | ~500MB | ~150MB | ~10MB |
| CPU 效率 | 高 | 低 | 中 | 高 |
| 处理性能 | ~86 MiB/s | ~4 MiB/s | ~26 MiB/s | ~35 MiB/s |
| 生态成熟度 | 中 | 高 | 高 | 中 |
| 学习曲线 | 中 | 高 | 中 | 低 |
| K8s 集成 | 好 | 一般 | 好 | 很好 |

性能数据来自 Vector 官方 benchmark（TCP to TCP 场景），实际情况因数据类型和处理逻辑而异，但量级差异是真实存在的。

**Logstash 的问题：**
- JVM 冷启动慢，内存占用不可控，GC 停顿影响延迟
- Plugin 质量参差不齐，社区插件有 bug 且维护不积极
- 配置文件语法（Grok 等）难以调试，错了也不报错只是默默丢数据

**FluentBit 的优势：**
资源占用比 Vector 还低，适合资源极其受限的边缘场景。但它的转换能力较弱，复杂的数据处理逻辑很难实现。我们的做法是 FluentBit 做节点级别的轻量采集，Vector 做聚合和复杂处理。

**为什么选 Vector 而不是 FluentBit：**
VRL（Vector Remap Language）是杀手锏功能，下面会详细讲。

## 安装与基础配置

Vector 提供 Helm Chart，在 K8s 上部署很方便：

```bash
helm repo add vector https://helm.vector.dev
helm repo update

helm install vector vector/vector \
  --namespace logging \
  --create-namespace \
  -f vector-values.yaml
```

`vector-values.yaml` 基础配置：

```yaml
role: Agent  # DaemonSet 模式，每个节点一个 Pod

# 资源限制
resources:
  requests:
    memory: 64Mi
    cpu: 100m
  limits:
    memory: 256Mi
    cpu: 500m

# 数据持久化（磁盘缓冲用）
persistence:
  enabled: true
  size: 1Gi

# 挂载节点日志目录
extraVolumes:
  - name: varlog
    hostPath:
      path: /var/log
  - name: varlibdockercontainers
    hostPath:
      path: /var/lib/docker/containers

extraVolumeMounts:
  - name: varlog
    mountPath: /var/log
    readOnly: true
  - name: varlibdockercontainers
    mountPath: /var/lib/docker/containers
    readOnly: true
```

## 完整配置示例：K8s 日志采集到 ES

下面是我们实际使用的配置，从 K8s 容器日志采集、解析 JSON、过滤、丰富元数据，到最终写入 ES：

```toml
# /etc/vector/vector.toml

# ============================================================
# Sources：数据来源
# ============================================================

[sources.kubernetes_logs]
type = "kubernetes_logs"
# 只采集特定 namespace
extra_namespace_label_selector = "monitoring=true"
# 排除 kube-system 的日志（通常是系统组件，噪音很多）
exclude_paths_glob_patterns = [
  "/var/log/pods/kube-system_*/**",
  "/var/log/pods/logging_vector*/**"  # 排除 Vector 自身日志，防止循环采集
]

# ============================================================
# Transforms：数据转换（核心处理逻辑）
# ============================================================

# Step 1: 解析 JSON 格式的日志
[transforms.parse_json]
type = "remap"
inputs = ["kubernetes_logs"]
source = '''
# 尝试解析 JSON 格式的日志
parsed, err = parse_json(.message)
if err == null {
  # 解析成功，把 JSON 字段合并到顶层
  . = merge(., parsed)
  del(.message)
} else {
  # 不是 JSON，保留原始 message 字段
  .log_raw = .message
}
'''

# Step 2: 标准化 timestamp 字段
[transforms.normalize_timestamp]
type = "remap"
inputs = ["parse_json"]
source = '''
# 优先使用日志本身的 timestamp，否则用 Vector 采集时间
if exists(.timestamp) {
  ts, err = parse_timestamp(.timestamp, "%+")
  if err == null {
    .@timestamp = ts
  } else {
    # 尝试其他格式
    ts, err = parse_timestamp(.timestamp, "%Y-%m-%d %H:%M:%S%.f")
    if err == null {
      .@timestamp = ts
    } else {
      .@timestamp = .source_timestamp
    }
  }
} else if exists(.time) {
  ts, err = parse_timestamp(.time, "%+")
  if err == null {
    .@timestamp = ts
  } else {
    .@timestamp = .source_timestamp
  }
} else {
  .@timestamp = .source_timestamp
}

del(.timestamp)
del(.time)
del(.source_timestamp)
'''

# Step 3: 丰富 Kubernetes 元数据
[transforms.enrich_k8s_metadata]
type = "remap"
inputs = ["normalize_timestamp"]
source = '''
# 从 kubernetes 元数据中提取关键字段到顶层，方便 ES 索引
.service = .kubernetes.labels."app.kubernetes.io/name" ?? .kubernetes.labels.app ?? .kubernetes.pod_name
.namespace = .kubernetes.pod_namespace
.pod = .kubernetes.pod_name
.container = .kubernetes.container_name
.node = .kubernetes.pod_node_name

# 保留 kubernetes 原始元数据但放到子对象里
.k8s = {
  "namespace": .kubernetes.pod_namespace,
  "pod_name": .kubernetes.pod_name,
  "pod_labels": .kubernetes.pod_labels,
  "container_name": .kubernetes.container_name,
  "node_name": .kubernetes.pod_node_name
}

del(.kubernetes)
del(.file)
del(.host)
'''

# Step 4: 过滤健康检查日志（减少噪音）
[transforms.filter_healthcheck]
type = "filter"
inputs = ["enrich_k8s_metadata"]
condition = '''
# 过滤掉健康检查和就绪检查的日志
!(
  (exists(.http) && .http.path == "/healthz") ||
  (exists(.http) && .http.path == "/readyz") ||
  (exists(.http) && .http.path == "/metrics") ||
  (exists(.message) && contains(string!(.message), "health check"))
)
'''

# Step 5: 解析 HTTP 日志的 status_code，统一为 integer
[transforms.normalize_http_fields]
type = "remap"
inputs = ["filter_healthcheck"]
source = '''
if exists(.http.status_code) {
  code, err = to_int(.http.status_code)
  if err == null {
    .http.status_code = code
  }
}

if exists(.http.duration_ms) {
  dur, err = to_float(.http.duration_ms)
  if err == null {
    .http.duration_ms = dur
  }
}

# 给慢请求打标签，方便告警
if exists(.http.duration_ms) && .http.duration_ms > 1000 {
  .tags = push(.tags ?? [], "slow_request")
}
'''

# Step 6: 路由不同业务的日志到不同索引
[transforms.route_by_namespace]
type = "route"
inputs = ["normalize_http_fields"]

[transforms.route_by_namespace.route]
payment = '.namespace == "payment"'
auth = '.namespace == "auth"'
# 默认路由
_unmatched = 'true'

# ============================================================
# Sinks：数据输出到 Elasticsearch
# ============================================================

[sinks.es_payment]
type = "elasticsearch"
inputs = ["route_by_namespace.payment"]
endpoint = "https://es-logging:9200"
auth.strategy = "basic"
auth.user = "vector-writer"
auth.password = "${ES_PASSWORD}"
tls.ca_file = "/etc/ssl/certs/es-ca.crt"

# 动态索引名称，按天分索引
bulk.index = "logs-payment-%Y.%m.%d"
# 使用 Data Streams（推荐）
data_stream.type = "logs"
data_stream.dataset = "payment"
data_stream.namespace = "prod"

# 重试配置
request.retry_attempts = 3
request.retry_initial_backoff_secs = 1
request.retry_max_duration_secs = 30

# 磁盘缓冲，防止 ES 不可用时丢数据
[sinks.es_payment.buffer]
type = "disk"
max_size = 268435456  # 256MB
when_full = "block"

[sinks.es_auth]
type = "elasticsearch"
inputs = ["route_by_namespace.auth"]
endpoint = "https://es-logging:9200"
auth.strategy = "basic"
auth.user = "vector-writer"
auth.password = "${ES_PASSWORD}"
tls.ca_file = "/etc/ssl/certs/es-ca.crt"
data_stream.type = "logs"
data_stream.dataset = "auth"
data_stream.namespace = "prod"

[sinks.es_default]
type = "elasticsearch"
inputs = ["route_by_namespace._unmatched"]
endpoint = "https://es-logging:9200"
auth.strategy = "basic"
auth.user = "vector-writer"
auth.password = "${ES_PASSWORD}"
tls.ca_file = "/etc/ssl/certs/es-ca.crt"
data_stream.type = "logs"
data_stream.dataset = "generic"
data_stream.namespace = "prod"

# 内置 Prometheus 监控端点
[sources.vector_metrics]
type = "internal_metrics"

[sinks.prometheus]
type = "prometheus_exporter"
inputs = ["vector_metrics"]
address = "0.0.0.0:9598"
```

## VRL（Vector Remap Language）深入

VRL 是 Vector 的核心优势，专门为日志处理设计的表达式语言，兼具类型安全和灵活性。

### 基础语法

```vrl
# 变量赋值
.field = "value"

# 条件判断
if .level == "ERROR" {
  .alert = true
}

# 可选链（处理字段不存在的情况）
.service = .kubernetes.labels.app ?? "unknown"

# 字符串操作
.message = upcase(.level) + ": " + .message

# 正则匹配
if match(.message, r'(?i)panic|fatal|oom') {
  .severity = "critical"
}

# 解析特定格式
parsed, err = parse_regex(.message, r'(?P<ip>\d+\.\d+\.\d+\.\d+) - (?P<user>\S+) \[(?P<time>[^\]]+)\]')
if err == null {
  .client_ip = parsed.ip
  .user = parsed.user
}
```

### 类型系统

VRL 是强类型的，这是很多人一开始不习惯的地方。字段读取默认返回 `Value` 类型，需要显式转换才能做类型相关操作：

```vrl
# 错误写法：to_string 期望 String 类型，.status_code 是 Value 类型
code_str = to_string(.status_code)  # 编译错误

# 正确写法：用 ! 表示"断言非空"，转换类型
code_str = to_string!(.status_code)

# 或者用 ?? 提供默认值
code_str = to_string(.status_code ?? 0)
```

常用类型转换：
- `to_string(value)` / `to_string!(value)`
- `to_int(value)` / `to_int!(value)` 
- `to_float(value)` / `to_float!(value)`
- `to_bool(value)` / `to_bool!(value)`
- `to_timestamp(value)` / `parse_timestamp(value, format)`

### 错误处理模式

VRL 函数通常返回 `(value, error)` 元组，需要处理 error：

```vrl
# 模式一：忽略错误（用 !），出错时会 abort 整个事件
.data = parse_json!(.raw_json)

# 模式二：显式处理错误
data, err = parse_json(.raw_json)
if err != null {
  log("Failed to parse JSON: " + err, level: "warn")
  .parse_error = err
} else {
  . = merge(., data)
}

# 模式三：提供默认值
.data = parse_json(.raw_json) ?? {}
```

### 实用 VRL 片段

**提取 trace_id 并关联 APM：**

```vrl
# 从 HTTP header 或日志字段提取 trace_id
if exists(.http.headers."x-trace-id") {
  .trace.id = .http.headers."x-trace-id"
} else if match(.message, r'trace_id=([a-f0-9]+)') {
  groups = parse_regex!(.message, r'trace_id=(?P<trace_id>[a-f0-9]+)')
  .trace.id = groups.trace_id
}
```

**解析 Nginx access log：**

```vrl
parsed, err = parse_nginx_log(.message, "combined")
if err == null {
  .http.method = parsed.method
  .http.path = parsed.path
  .http.status_code = to_int!(parsed.status)
  .http.response_size = to_int!(parsed.size)
  .client.ip = parsed.client
  .http.user_agent = parsed.agent
  del(.message)
}
```

**按 log level 打 severity 标签：**

```vrl
.severity = if includes(["ERROR", "FATAL", "CRITICAL"], upcase(string!(.level ?? ""))) {
  "high"
} else if .level == "WARN" || .level == "WARNING" {
  "medium"
} else {
  "low"
}
```

## 缓冲策略选择

Vector 支持两种缓冲：内存缓冲和磁盘缓冲。

**内存缓冲（默认）：**

```toml
[sinks.es.buffer]
type = "memory"
max_events = 500
when_full = "block"  # 或 "drop_newest"
```

优点：速度快，延迟低。缺点：Vector 重启或 crash 时缓冲数据丢失。

**磁盘缓冲：**

```toml
[sinks.es.buffer]
type = "disk"
max_size = 268435456  # 256MB
when_full = "block"
```

优点：持久化，重启后继续发送。缺点：速度略慢，需要额外的 PVC 挂载。

**如何选择：**

对于日志场景，我的建议：
- K8s DaemonSet 模式（Agent 模式）：使用磁盘缓冲，因为 ES 短暂不可用时不能丢数据，而且 DaemonSet 节点异常重启很常见
- 高吞吐、低延迟要求（>100MB/s）：内存缓冲，磁盘 IO 会成为瓶颈

我们踩过一次坑：用内存缓冲，ES 做滚动升级时（大约 5 分钟不可用），Vector 的缓冲队列满了，触发了 `drop_newest` 策略，丢失了约 200 万条日志。换成磁盘缓冲后，ES 升级期间的日志在恢复连接后补发，零丢失。

## 性能调优

Vector 默认配置在大多数场景下够用，但有几个参数需要根据实际情况调整：

**并发度控制：**

```toml
[sinks.elasticsearch]
request.concurrency = "adaptive"  # 自适应并发（推荐）
# 或者固定值
# request.concurrency = 5
```

`adaptive` 模式会根据后端响应时间自动调整并发请求数，ES 负载高时自动降速，避免雪崩。

**批量大小：**

```toml
[sinks.elasticsearch]
batch.max_bytes = 10485760   # 10MB per bulk request
batch.max_events = 10000
batch.timeout_secs = 5       # 最多等 5 秒，即使没满也发送
```

**内部并行度：**

Vector 默认使用所有 CPU 核心，可以通过环境变量限制：

```bash
VECTOR_THREADS=2  # 限制 2 个 worker 线程
```

在 K8s 里通过 resources.limits.cpu 间接控制，不需要手动设置 VECTOR_THREADS。

## 监控与告警

Vector 内置 Prometheus metrics 端点，暴露丰富的运行时指标：

```bash
# 查看 Vector 的处理统计
curl http://vector-pod:9598/metrics | grep -E "vector_component_(sent|received|errors)"
```

关键指标：
- `vector_component_sent_events_total`：各 sink 发送的事件总数
- `vector_component_received_events_total`：各 source 接收的事件总数
- `vector_component_errors_total`：错误计数（持续增长说明有问题）
- `vector_buffer_events`：缓冲队列中的事件数（持续增长说明 sink 写入跟不上）

Grafana Dashboard 推荐使用 Vector 官方的 Dashboard ID `18604`，导入后直接可用。

## 踩坑记录

**坑1：VRL 类型错误导致事件被静默丢弃**

现象：某些日志在 Vector 处理后消失了，ES 里查不到。

排查：打开 Vector 的 debug 日志：

```bash
VECTOR_LOG=debug vector --config /etc/vector/vector.toml
```

看到大量：

```
ERROR vector::topology::builder: ... VRL error: expected string, found integer at path .http.status_code
```

原因：写 VRL 时用了 `!` 断言（`to_string!(.status_code)`），当类型不匹配时整个事件被 abort（丢弃）。

修复：改为带错误处理的版本：

```vrl
code, err = to_string(.http.status_code)
if err != null {
  .http.status_code = to_string(.http.status_code ?? 0)
}
```

或者直接用更宽容的转换函数，比如 `to_string` 接受任意类型。

**坑2：K8s 日志文件轮转导致重复采集**

现象：某些日志出现重复，ES 里能查到同一条日志两次。

原因：Vector 用文件 offset 记录采集位置（存在 `/var/lib/vector/` 下），当 K8s 做日志 rotate（重命名旧文件，创建新文件）时，Vector 有时候会同时处理新旧文件的交叉部分。

解决：

```toml
[sources.kubernetes_logs]
type = "kubernetes_logs"
# 延迟处理新文件，等 rotate 完成
glob_minimum_cooldown_ms = 5000
```

或者在 ES 写入时配置文档 ID，利用 ES 的幂等写入去重：

```toml
[sinks.elasticsearch]
id_field = "kubernetes.pod_uid"  # 用 pod_uid + offset 组合做唯一 ID
```

**坑3：transform 链中某一步报错导致整个管道停止**

现象：Vector 运行一段时间后停止处理数据，`vector_component_received_events_total` 不再增长。

排查：检查 Vector 的 source 统计：

```bash
curl http://vector-pod:9598/metrics | grep "vector_component_received"
```

source 在接收数据，但 transform 之后的 sink 没有发送，说明 transform 阶段卡住了。

查 transform 组件的 metrics：

```
vector_component_errors_total{component_id="parse_json"} 15234
```

parse_json transform 积累了大量错误。原因：某个应用突然开始输出非 JSON 格式的日志，VRL 里用了 `parse_json!(.message)` 这种会 abort 的写法，导致整个事件丢弃，但是 abort 本身不影响管道继续工作，实际问题是 VRL 里有一个没有处理 null 的路径导致 panic。

教训：VRL 里凡是用 `!` 的地方都要仔细考虑是否真的能保证类型安全，生产环境建议全部改成带错误处理的版本，哪怕代码稍微长一点。

---

Vector 在我们的日志平台运行了一年多，整体非常稳定。唯一的遗憾是 VRL 调试比较痛苦，没有交互式 REPL，只能通过 `vector test` 命令离线测试，或者在测试集群上跑实际数据验证。官方最近在 Web 上提供了一个 VRL Playground（vrl.dev），可以直接在浏览器里测试 VRL 表达式，大大降低了调试成本。

整个 ELK 系列到这里告一段落——从 ECK 部署、索引策略，到备份恢复，再到 Vector 采集管道，这四篇覆盖了日志平台运维的主要环节。每个环节都还有很多可以深入的地方，欢迎在评论里讨论具体问题。
