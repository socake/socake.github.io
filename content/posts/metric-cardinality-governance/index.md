---
title: "Prometheus 高基数治理实战：从 8 亿 series 到可控增长"
date: 2025-09-28T10:00:00+08:00
draft: false
tags: ["Prometheus", "Cardinality", "可观测性", "Mimir", "治理"]
categories: ["可观测性"]
description: "一份系统的指标基数治理手册：基数为什么是性能杀手、如何测量、常见反模式、relabel 与 drop 技巧、治理流程、告警机制、团队约束，以及两次真实的雪崩复盘。"
summary: "高基数是 Prometheus 生态里最常见的性能杀手。这篇把「为什么发生、怎么发现、怎么治理」讲清楚，并给出一套可推广的组织治理方案。"
toc: true
math: false
diagram: false
keywords: ["Prometheus", "基数", "relabel", "Mimir", "治理"]
params:
  reading_time: true
---

## 故事的开头是一个被打爆的集群

2025 年 3 月一个周三下午，Mimir 集群开始告警。ingester 的 series 数在过去 2 小时内从 3.2 亿涨到 6.1 亿，涨幅几乎是线性的。我们打开 Grafana 的 cardinality dashboard，一眼看到罪魁祸首：某个租户的一个指标，单 metric 名下的 series 数在 2 小时内从 50 万涨到 2 亿。

根因是一个业务团队把一个新 label `container_id` 写进了 Prometheus recording rule。`container_id` 每 pod 重启就变，加上他们的 rate(restart) 恰好触发了 K8s HPA 扩缩容，container_id 每分钟都在产生新值。

当天下午我们花了 4 小时把集群救回来（临时 drop 规则 + 紧急限流 + ingester 扩容）。第二天开始做的事情，是把「基数治理」从一个模糊的期待变成有流程、有工具、有告警的工程实践。

这篇就是那之后的经验总结。涵盖四个点：什么是 cardinality，怎么发现问题，发生了怎么灭火，怎么把治理固化成长期机制。
5. 怎么给业务团队写一份可执行的「指标规范」？

## 一、Cardinality 基础

在 Prometheus / Mimir / VictoriaMetrics 等 TSDB 里，一条 time series 的唯一 key 是 `(metric_name, label_set)`。只要 label_set 的组合多一种，就多一条 series。举个例子：

```
http_requests_total{method="GET", path="/api/users", status="200"}
http_requests_total{method="POST", path="/api/orders", status="200"}
http_requests_total{method="GET", path="/api/users", status="500"}
```

这是 3 条 series。它们共享 metric name `http_requests_total`，但 label_set 不同。

### 基数的乘法效应

如果一个指标带 5 个 label，每个 label 有 N 个可能值，series 总数是 `N1 * N2 * N3 * N4 * N5`。这就是所谓「组合爆炸」。

举例：

- `method`: 5 个 HTTP 方法
- `path`: 100 个 endpoint
- `status`: 10 种状态码
- `pod`: 30 个 pod
- `version`: 5 个版本

5 * 100 * 10 * 30 * 5 = 750,000 series，仅一个 metric。

再加一个高基数 label：

- `trace_id`: 100 万/天

750,000 * 1,000,000 = 7500 亿 series。

基数杀手通常就这么诞生：开发者习惯性把 request id / trace id / uuid / user id / timestamp 之类的东西放进 label，没意识到后果。

### 为什么基数高会出问题

Prometheus / Mimir 的 TSDB 是为中低基数设计的。高基数导致：

1. **内存爆炸**：每条 series 在 ingester 内存里有开销，约 4KB。1 亿 series 就是 400GB。
2. **索引膨胀**：TSDB 的倒排索引是 `label=value -> series_id`，高基数让索引巨大。
3. **查询慢**：PromQL 查询需要按 label 遍历 series，基数高的 label 上查询会很慢。
4. **WAL 增大**：每条 series 的 WAL 记录独立，重启 replay 更慢。
5. **对象存储上的 block 臃肿**：series 数多，index 文件大，store gateway 加载慢。
6. **compactor 追不上**：单 block 太大，compaction 耗时长。

在 Mimir 里，一条 series 的边际成本大约是 0.05 美分/月（对象存储 + compute 综合）。1 亿 series 就是 5 万美元/月。钱烧起来非常快。

## 二、怎么发现高基数

### 方法 1：TSDB status 页面

Prometheus 自带 `/api/v1/status/tsdb` 接口（也在 UI 的 Status → TSDB Stats）：

```json
{
  "headStats": {
    "numSeries": 324856,
    "numLabelPairs": 1823,
    "chunkCount": 1945212
  },
  "seriesCountByMetricName": [
    {"name": "http_request_duration_seconds_bucket", "value": 84325},
    {"name": "container_memory_working_set_bytes", "value": 32165},
    ...
  ],
  "labelValueCountByLabelName": [
    {"name": "__name__", "value": 3211},
    {"name": "le", "value": 43},
    {"name": "trace_id", "value": 18422},
    ...
  ],
  "memoryInBytesByLabelName": [...],
  "seriesCountByLabelValuePair": [...]
}
```

**重点看三个字段**：

1. **seriesCountByMetricName**：哪些 metric 产生最多 series；
2. **labelValueCountByLabelName**：哪些 label 的 value 基数最高；
3. **seriesCountByLabelValuePair**：特定的 (label, value) 组合的 series 数。

Mimir 有 per-tenant 的版本：

```
curl -s "http://mimir-gateway/api/v1/cardinality/label_names?selector={__name__=~\".+\"}" \
  -H "X-Scope-OrgID: team-a"
```

### 方法 2：cardinality dashboard

官方 Prometheus / Mimir 的 mixin 里有 cardinality dashboard，能显示：

- per-metric series count 排行；
- per-label cardinality 排行；
- 新增 series 速度；
- 各租户的 series 占比。

没有的话自己写：

```promql
# Top metrics by series
topk(20,
  count by(__name__) ({__name__=~".+"})
)

# Top labels by cardinality (仅 Mimir)
topk(20,
  sum by(label) (cortex_ingester_memory_series_labels_count)
)

# Series 增长速度
deriv(cortex_ingester_memory_series[30m]) * 60
```

### 方法 3：promtool tsdb analyze

对于本地 Prometheus：

```bash
promtool tsdb analyze /var/prometheus/data
```

输出例子：

```
Highest cardinality labels:
  3421 __name__
  18422 trace_id
  12084 pod
  ...

Highest cardinality metric names:
  84325 http_request_duration_seconds_bucket
  32165 container_memory_working_set_bytes
  ...

Label pairs most involved in churning:
  app=foo, instance=... 2145
  container=bar, pod=baz 1982
```

`churning` 是 series 增删速度，它和绝对 series 数一样重要。一个租户 series 总数 500 万但 churn 率 70%/小时，内存压力比 1000 万 series 但 churn 率 1%/小时 的租户大得多。

### 方法 4：告警

基数变化告警是必需的。几个核心告警：

```yaml
- alert: PrometheusTotalSeriesHigh
  expr: |
    prometheus_tsdb_head_series > 5000000
  for: 10m

- alert: PrometheusSeriesGrowthFast
  expr: |
    deriv(prometheus_tsdb_head_series[30m]) * 3600 > 1000000
  for: 10m
  annotations:
    summary: series 数过去 30m 增速超过 100 万/小时

- alert: MetricHighCardinality
  expr: |
    topk(5, count by(__name__) ({__name__=~".+"})) > 500000
  for: 15m

- alert: TenantCardinalityNearLimit
  expr: |
    cortex_ingester_memory_series{user!=""} 
    / on(user) group_left() 
    (cortex_overrides{limit_name="max_global_series_per_user"}) > 0.8
  for: 15m
```

## 三、反模式：开发者最常犯的 8 个错误

### 反模式 1：把 ID 放 label

```
http_requests_total{user_id="12345"}
http_requests_total{user_id="12346"}
...
```

用户 ID 往往几十万上百万，直接变成几十万上百万 series。**ID 永远不能当 label**。

### 反模式 2：把 trace_id / request_id 放 label

同上，trace_id 更狠，每个请求一个。

### 反模式 3：把 URL path 当 label（带参数）

```
http_requests_total{path="/users/12345/orders/67890"}
http_requests_total{path="/users/12346/orders/67891"}
```

正确做法是把 path 模板化：

```
http_requests_total{path="/users/:id/orders/:orderId"}
```

这件事必须在埋点框架里做。我们推荐用 OTel 语义约定 `http.route`（非 `http.target`）作为 label。

### 反模式 4：把时间戳放 label

```
events_total{event_time="2025-09-28T15:23:45Z"}
```

时间戳单调递增，每秒一个新 series。最离谱的一类。

### 反模式 5：把错误消息当 label

```
errors_total{message="connection refused: 10.1.2.3:8080"}
```

错误消息里带 IP、端口、文件名 等可变部分，基数爆炸。正确做法是用 error_code 或 error_type 分类。

### 反模式 6：把 hostname 或 pod name 当 label

```
process_cpu_usage{pod="order-api-5f4-abc12"}
```

K8s 下 pod name 带随机后缀，每次重启变化。用 `workload` 或 `app` 代替。如果实在需要 pod 级粒度（比如 kube-state-metrics），ingester 侧要有 churn 率告警。

### 反模式 7：没必要的高基数 join label

```
db_queries_total{
  db_name="orders",
  schema="public",
  table="order_items",
  sql_hash="a1b2c3d4e5f6",
  caller_service="payment-api",
  caller_version="2.3.1",
  ...
}
```

太多 dimension 组合起来就是灾难。每个 label 自身基数不算高，但乘起来爆炸。

### 反模式 8：把高基数 label 加到 recording rule

Recording rule 里的 `by (high_cardinality_label)` 比原始指标更狠，因为它是持续聚合的输出。

## 四、灭火：发现问题后怎么办

生产上真的爆了，按这个顺序处理：

### Step 1：确认影响面

```
topk(20, count by(__name__) ({__name__=~".+", tenant="问题租户"}))
```

找出是哪个 metric 爆了。

### Step 2：紧急 drop

在 Prometheus 或 Mimir 的 remote_write 层加 relabel 丢弃：

```yaml
remote_write:
  - url: http://mimir/api/v1/push
    write_relabel_configs:
      - source_labels: [__name__]
        regex: "problematic_metric_name"
        action: drop
```

Mimir distributor 层也可以配 `distributor.write_requests_buffer_pooling_enabled` 和 `limits.drop_labels` 做紧急 drop：

```yaml
limits:
  drop_labels: ["trace_id", "request_id"]
```

### Step 3：限流

紧急把这个租户的 `max_global_series_per_user` 调小：

```yaml
overrides:
  team-problem:
    max_global_series_per_user: 1000000
    ingestion_rate: 50000
```

限流之后新 series 被拒绝，老 series 依然在。效果是「止血」而不是「清理」。

### Step 4：清理 head

Mimir 的 ingester 会在 `-blocks-storage.tsdb.retention-period`（默认 13h）后清理 head 里的 series。所以**真正完全清理**需要等 13h。你可以加速这个过程：

1. 缩短该租户的 retention；
2. 重启 ingester 强制 head flush（有风险，务必逐个重启）；
3. 在 distributor 侧 drop 规则，防止 series 继续增长。

### Step 5：通知团队

告诉出问题的业务团队：

- 你们产生的 series 数；
- 当前被 drop 的 label；
- 限流配置；
- 需要他们做什么（改代码 / 改 config / 重新上线）。

沟通时客气但明确：「你们的 metric 触发了全租户保护，我们临时丢了 X 标签，请在 T+24h 之前改好。」

## 五、写好一份「指标规范」

灭火不能解决根本问题。需要给业务团队一份白纸黑字的指标规范，指导他们怎么写指标。我们内部的指标规范节选：

### 1. 命名规范

- 使用 snake_case，全小写；
- 单位后缀：`_total`, `_seconds`, `_bytes`, `_ratio`；
- Counter 必须 `_total` 结尾；
- 不用 metric name 编码业务维度（比如 `order_count_vs_payment_count`）。

### 2. Label 规范

- **禁用的 label**：`*_id`、`trace_id`、`request_id`、`user_id`、`email`、`ip`、`url` 完整路径、`timestamp`、`uuid`、`pod`（除 kube-state-metrics）、`hostname`；
- **允许的 label**：服务名、业务分类、HTTP method、HTTP route（模板化）、HTTP status class（2xx/3xx/...）、env、region；
- **单个 metric 的 label 数量上限 10 个**；
- **单个 label value 长度上限 200 字符**；
- **所有新 metric 上线前必须做基数预估**（下面有模板）。

### 3. 基数预估模板

```
## 指标基数预估 - order_created_total

### 指标说明
业务：订单创建事件计数
用途：监控订单创建速率、成功率

### Label 列表
| Label | 可能值数 | 举例 |
|---|---|---|
| region | 4 | ap-se1, ap-ne1, us-east-1, us-west-2 |
| env | 3 | prod, staging, dev |
| status | 5 | success, failed_stock, failed_payment, ... |
| channel | 6 | web, android, ios, minipro, ... |

### 基数计算
4 * 3 * 5 * 6 = 360 series

### 评审结论
✅ 通过。预估 360 series，远低于单 metric 100k 的阈值。
```

这份模板强制开发者在写 metric 前做数学计算，直接过滤掉 90% 的基数问题。

### 4. 指标接入流程

1. 开发者提 PR，包含新 metric 的基数预估文档；
2. CI 自动检查：metric name 是否符合规范、label 名是否在黑名单、label 数量是否超限；
3. 人工 review：SRE 或平台团队检查基数预估合理性；
4. 合并上线；
5. 上线 24h 后平台自动检查实际 series 数，和预估值对比，超过 2 倍发告警给团队。

CI 检查脚本大概 50 行 Python，用 `prometheus_client` 库解析 metric 定义。

## 六、治理工具：把规范固化到代码

### 静态检查

用 `promtool check rules` 和 `promtool check metrics`：

```bash
promtool check rules rules.yaml
promtool check metrics < metrics.prom
```

可以捕获：rule 名称不符合规范、空 label、metric 名非法等。

### Lint：自己写的规则

光靠 promtool 不够，写一个 linter 检查公司内部规则：

```python
import re
from prometheus_client.parser import text_string_to_metric_families

def lint(metrics_text, forbidden_labels=None):
    forbidden = forbidden_labels or [
        "trace_id", "request_id", "user_id", "uuid", "pod", "ip"
    ]
    issues = []
    for family in text_string_to_metric_families(metrics_text):
        if not re.match(r"^[a-z_][a-z0-9_]*$", family.name):
            issues.append(f"bad name: {family.name}")
        if family.type == "counter" and not family.name.endswith("_total"):
            issues.append(f"counter missing _total: {family.name}")
        for sample in family.samples:
            for label in sample.labels:
                if label in forbidden:
                    issues.append(f"forbidden label: {family.name}{{{label}}}")
                if len(sample.labels[label]) > 200:
                    issues.append(f"label too long: {family.name}{{{label}}}")
    return issues
```

这个 lint 在 CI 里对每次 PR 跑，抓到违规直接拒 merge。

### 实时监控：series churn rate

按租户和 metric 维度做 churn rate 监控：

```promql
# Mimir 有 cortex_ingester_memory_series_created_total
rate(cortex_ingester_memory_series_created_total[5m])
```

Churn rate 高的 metric 通常是高基数问题的先兆。

### Self-service dashboard

给每个业务团队一个 self-service 的 cardinality dashboard：

- 我们团队的 top metrics by series？
- 过去 7 天 series 增长曲线？
- 哪些 label 是基数驱动？
- 接近配额了吗？

业务团队能看到数据就能自己管理，否则永远是「平台团队追着业务团队改」的无限循环。

## 七、案例：K8s 标签意外导致基数爆炸

时间：2025 年 6 月。现象：`kube_pod_labels` 的 series 数从 2 万涨到 40 万。

**根因**：某业务团队在 deployment template 里加了一个 label：

```yaml
metadata:
  labels:
    build_sha: ${CI_COMMIT_SHA}
```

每次部署 SHA 变，kube-state-metrics 把 label 转成 `label_build_sha="..."` 作为 Prometheus label。CI 每天部署 50 次，30 天就是 1500 个值，再乘以 pod 数量...

**排查**：`topk(5, count by(label_build_sha)(kube_pod_labels))` 直接看到。

**修复**：

1. kube-state-metrics 的 `--metric-labels-allowlist` 白名单化，只导出业务需要的 label；
2. 业务侧把 build_sha 从 label 移到 annotation；
3. CI 流程加 lint 检查 deployment metadata。

## 八、案例：histogram bucket 数量失控

时间：2025 年 10 月。现象：`http_request_duration_seconds_bucket` 一个 metric 就占了集群 series 的 15%。

**根因**：某个团队的 HTTP SDK 默认 histogram bucket 定义是 `[0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 30, 60, 120, 300]` — 17 个 bucket。加上 `_bucket`, `_count`, `_sum` 三个后缀和每个 bucket 一条 series，一个 endpoint 一次 histogram 就是 19 条 series。

他们有 80 个 endpoint * 30 个 pod * 3 个 env = 7200 个维度组合。7200 * 19 = 136,800 series。再乘以每个 bucket 的额外维度（status, method），总数 80 万。

**修复**：

1. 把 bucket 从 17 个压到 10 个（业务常用的 0.01~5s）；
2. 禁止在 histogram 上加 `pod` label（改成 `workload`）；
3. 用 native histogram（Prometheus 2.40+）：native histogram 一个 series 表达整个分布，series 从 19 条降到 1 条。

Native histogram 是 2024 年以后真正解决 histogram 基数问题的方案。目前 Grafana 10.4+ 支持查询，建议新项目直接用。

## 九、Native Histogram：一个重要的未来方向

Prometheus 2.40+ 引入的 native histogram（也叫 sparse histogram）把整个分布存成一条 series，由服务端动态管理 bucket。相比 classic histogram：

- **series 数减少 10~50 倍**：一个 metric 从 19 条 bucket series 变成 1 条 histogram；
- **精度更高**：支持 0.01% 分位数精度；
- **存储成本低**：压缩率比 classic 好；
- **查询快**：histogram_quantile 直接用 native 数据。

需要注意：

- 客户端 SDK 要支持：Go 的 prometheus/client_golang 1.17+，Java 的 client_java 1.0+；
- Grafana 要升级；
- Mimir 2.12+ / VictoriaMetrics 最近版本都支持；
- Native 和 classic 可以同时上报，过渡阶段用 dual-mode。

我们在 2025 年 Q3 把所有新服务默认用 native histogram，Q4 开始推老服务迁移。

## 十、组织层面：谁负责指标治理

治理光有工具不够，要有 ownership。我们的分工：

| 角色 | 职责 |
|---|---|
| 业务团队 | 决定要采什么指标、做基数预估、响应治理告警 |
| 平台团队（SRE） | 提供 Prometheus/Mimir 平台、maintain 规范、审批例外 |
| 业务 TL | 定期 review 本团队指标，对基数负责 |
| Architect | 跨业务的 metric 标准、命名约定 |

关键原则：**平台团队是裁判，不是保姆**。平台不替业务删 metric，只拦截和告警。否则业务永远不会意识到基数问题。

## 十一、Per-tenant 配额设计

Mimir / Cortex 都支持 per-tenant 配额。我们的设计：

```yaml
overrides:
  team-a:
    max_global_series_per_user: 5000000
    max_global_series_per_metric: 500000
    max_label_names_per_series: 20
    max_label_value_length: 2048
    ingestion_rate: 200000
    ingestion_burst_size: 2000000
```

**分层配额**：

- 默认 tenant 限 500 万 series；
- 按申请给到 1000 万 / 2000 万 / 5000 万；
- 超过 5000 万需要平台团队审批；
- 单 metric 限 50 万 series，防止单 metric 爆炸整 tenant。

配额超限时 distributor 返回 429，Prometheus remote_write 会重试，最终 drop。业务侧会看到 `prometheus_remote_storage_failed_samples_total` 指标异常，主动找平台。

## 十二、季度 cardinality review

每季度做一次全集群 review：

1. Top 10 metric by series；
2. Top 10 label by cardinality；
3. 每 tenant 的 series 配额使用率；
4. 本季度新增 metric 的数量和影响；
5. churn rate 异常的 metric；
6. 没使用的 metric（查询数 = 0，可以考虑删）。

第 6 条特别重要。用 `prometheus_http_requests_total{handler="/api/v1/query"}` 配合日志分析，能找出「采集了但从没查过」的 metric。这类 metric 通常占比 20%~30%，都可以砍掉。

## 十三、总结清单

把这篇文章的核心要点压成一份 checklist：

### 预防

1. 写一份明确的 metric 规范；
2. label 黑名单（trace_id / request_id / user_id / uuid / pod / ip / url）；
3. CI lint 拦截不规范 metric；
4. 新 metric 必须做基数预估；
5. 给 histogram 做 bucket 控制；
6. 新项目直接用 native histogram。

### 发现

1. TSDB status 页面定期检查；
2. cardinality dashboard 上线；
3. series 增长告警；
4. tenant 配额告警；
5. churn rate 监控。

### 灭火

1. 紧急 drop 规则；
2. distributor 限流；
3. ingester head 清理；
4. 和业务团队沟通模板。

### 治理

1. Per-tenant 配额；
2. 业务 TL 负责指标 ownership；
3. 季度 review；
4. 未使用 metric 清理。

## 十四、给管理层的一句话

高基数不是技术问题，是组织问题。它的根源是「业务团队没意识到 metric 有成本」。治理的核心是让成本可见：每个团队知道自己每月烧多少钱在指标上，自然就会自我约束。否则任何工具都是打补丁。

我们花了 6 个月把基数从 8 亿降到 5 亿（业务同时增长 30%），靠的不是什么神奇算法，就是把上面那些流程一条条落地。结果：对象存储成本降 30%、查询 p99 降 50%、ingester 内存降 40%。预防永远比半夜灭火便宜。

## 参考资料

- Prometheus 官方文档：TSDB status / cardinality analysis
- Grafana Mimir 文档：per-tenant limits、cardinality API
- Prometheus Blog：Native Histograms
- Robust Perception Blog：cardinality articles
- Google SRE Workbook SLO 章节
