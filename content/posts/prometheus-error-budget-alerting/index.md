---
title: "基于 Error Budget 的 Prometheus 告警设计——燃烧率告警实战"
date: 2026-04-12T12:00:00+08:00
draft: false
tags: ["Prometheus", "SLO", "Error Budget", "告警", "PromQL", "SRE"]
categories: ["监控告警"]
series: ["SRE 可靠性工程师路径"]
description: 基于 Error Budget 燃烧率的 Prometheus 告警设计：多窗口燃烧率规则、Recording Rules 优化、SLO Dashboard 构建，让告警真正反映用户可感知的故障
summary: 错误率告警有一个致命问题：它不告诉你问题有多紧急。1% 的错误率，持续 2 小时和持续 10 分钟，对 SLO 的威胁完全不同。燃烧率告警从 Error Budget 消耗速度出发，让每一次告警都携带"紧急程度"信息。
toc: true
math: false
diagram: false
keywords: ["Error Budget", "燃烧率", "SLO", "Prometheus", "PromQL", "多窗口告警", "Recording Rules"]
params:
  reading_time: true
---

我们有一条 Prometheus 告警规则运行了两年：`http_error_rate > 0.01`（错误率大于 1%）。它每周平均触发 30 次，其中大约 20 次是短暂抖动，5 分钟内自愈，工程师什么都不用做。

这 20 次"无效告警"造成的损失不只是噪音：它训练了工程师的条件反射——看到这个告警先观察 5 分钟，因为"可能自愈"。于是真正严重的那几次，响应也慢了 5 分钟。

燃烧率告警（Burn Rate Alerting）解决的就是这个问题。

## 为什么简单阈值告警不够

先看两个场景：

**场景 A**：错误率突然飙到 10%，持续 15 分钟后恢复正常。

**场景 B**：错误率维持在 0.5%，持续了整整一天。

如果你的告警规则是 `error_rate > 1%`，场景 A 会触发告警（正确），场景 B 不会触发告警（但它会让你的月度 SLO 从 99.9% 跌到 99.4%，损失巨大）。

**问题根源**：简单阈值告警度量的是瞬时状态，不度量影响积累速度。SLO 是一个月维度的约束，但告警是瞬时的，两者语义对不上。

燃烧率告警从另一个角度切入：**你的 Error Budget 正在以多快的速度被消耗？**

## Error Budget 计算基础

以 30 天 SLO 99.9% 为例：

```
Error Budget（月度）= (1 - 0.999) × 30天 × 24小时 × 60分钟
                   = 0.001 × 43,200 分钟
                   = 43.2 分钟
```

也就是说，整个月内，服务最多允许 43.2 分钟的"错误时间"（以 100% 错误率计算）。

换算为每小时的允许消耗：

```
每小时允许消耗 = 43.2 分钟 / (30 × 24 小时)
              = 43.2 / 720 分钟/小时
              = 0.06 分钟/小时 ≈ 每小时 3.6 秒的错误时间
```

**关键概念：燃烧率（Burn Rate）**

```
燃烧率 = 当前错误率 / (1 - SLO)
       = 当前错误率 / error_budget_ratio
```

以 SLO 99.9%（`error_budget_ratio = 0.001`）为例：

| 当前错误率 | 燃烧率 | 含义 |
|-----------|------|------|
| 0.1% | 1x | 正好以"预算速度"消耗，30 天刚好耗尽 |
| 1% | 10x | 10 倍速消耗，3 天耗尽月度预算 |
| 10% | 100x | 100 倍速，7.2 小时耗尽月度预算 |
| 14.4% | 144x | 2 小时耗尽月度预算 → 需要立即响应 |

现在告警的意义变清晰了：**不是"错误率高了"，而是"按这个速度，月度预算将在 X 小时内耗尽"**。

## 多窗口燃烧率告警规则

Google SRE Workbook 推荐的多窗口方案：使用长短窗口配对，短窗口提高召回率（不漏警），长窗口提高精确率（减少误报）。

同时触发短窗口 AND 长窗口告警时，才认为是真实故障。

### 完整 Prometheus 告警规则 YAML

```yaml
groups:
  - name: slo_burn_rate_alerts
    rules:
      # P1：极速燃烧 - 预计 2 小时内耗尽月度预算
      - alert: HighErrorBudgetBurnRate
        expr: |
          (
            job:slo_errors_per_request:ratio_rate1h{job="payment-service"} > (14.4 * 0.001)
          )
          and
          (
            job:slo_errors_per_request:ratio_rate5m{job="payment-service"} > (14.4 * 0.001)
          )
        for: 2m
        labels:
          severity: critical
          team: payment
        annotations:
          summary: '{{ $labels.job }} 极高错误燃烧率：预计 2 小时内耗尽月度 Error Budget'
          description: |
            服务 {{ $labels.job }} 当前 1h 燃烧率为 {{ $value | humanizePercentage }}（阈值 14.4x）。
            按此速度，月度 Error Budget 将在约 2 小时内耗尽。
            Runbook: https://wiki.example.com/runbook/high-burn-rate
            Grafana: https://grafana.example.com/d/slo-dashboard?var-job={{ $labels.job }}

      # P2：快速燃烧 - 预计 6 小时内耗尽月度预算
      - alert: MediumErrorBudgetBurnRate
        expr: |
          (
            job:slo_errors_per_request:ratio_rate6h{job="payment-service"} > (6 * 0.001)
          )
          and
          (
            job:slo_errors_per_request:ratio_rate30m{job="payment-service"} > (6 * 0.001)
          )
        for: 15m
        labels:
          severity: warning
          team: payment
        annotations:
          summary: '{{ $labels.job }} 较高错误燃烧率：预计 6 小时内消耗 5% 月度 Error Budget'
          description: |
            服务 {{ $labels.job }} 当前 6h 燃烧率为 {{ $value | humanizePercentage }}（阈值 6x）。
            Runbook: https://wiki.example.com/runbook/medium-burn-rate

      # P3：趋势告警 - 3 天窗口燃烧率超标
      - alert: SlowErrorBudgetBurnRate
        expr: |
          job:slo_errors_per_request:ratio_rate3d{job="payment-service"} > (1 * 0.001)
        for: 1h
        labels:
          severity: info
          team: payment
        annotations:
          summary: '{{ $labels.job }} Error Budget 消耗趋势告警：按当前速度月底将超出预算'
          description: |
            服务 {{ $labels.job }} 3 天燃烧率超过 1x（SLO 基准线），月底有超出 Error Budget 风险。
            当前剩余 Error Budget: {{ $value }}%
```

### 各窗口的计算逻辑

| 告警级别 | 短窗口 | 长窗口 | 燃烧率阈值 | 预计耗尽时间 |
|---------|-------|-------|-----------|------------|
| P1 Critical | 5m | 1h | > 14.4x | ~2 小时 |
| P2 Warning | 30m | 6h | > 6x | ~5 小时内消耗 5% |
| P3 Info | — | 3d | > 1x | 月底超出 |

**为什么 14.4 这个数字？**

```
月度允许消耗比例 = 1 - SLO = 0.001
2 小时耗尽 = 2h / (30d × 24h) = 2 / 720 ≈ 0.00278
燃烧率 = 0.00278 / 0.001 × 100% ≈ 2.78%
但这里说的是"2小时内耗尽月度预算的 5%"
实际阈值：如果要在 1 小时窗口内消耗 2% 的月预算
2% × 0.001 / (1/720) ≈ 14.4
```

直接记住结论即可：**P1 = 14.4x，P2 = 6x**，这是 Google SRE Workbook 的推荐值。

## Recording Rules：性能优化的关键

燃烧率计算涉及多个时间窗口的比率运算，实时计算会让 Prometheus 查询超时，而且同样的表达式会被多条规则重复计算。

Recording Rules 把高频计算的结果预先存储为新指标：

```yaml
groups:
  - name: slo_recording_rules
    interval: 30s
    rules:
      # 基础错误率：HTTP 5xx / 总请求数
      - record: job:http_requests_total:rate5m
        expr: |
          sum(rate(http_requests_total[5m])) by (job, status_code)

      - record: job:http_errors_total:rate5m
        expr: |
          sum(rate(http_requests_total{status_code=~"5.."}[5m])) by (job)

      # SLI：各时间窗口的错误率
      - record: job:slo_errors_per_request:ratio_rate5m
        expr: |
          sum(rate(http_requests_total{status_code=~"5.."}[5m])) by (job)
          /
          sum(rate(http_requests_total[5m])) by (job)

      - record: job:slo_errors_per_request:ratio_rate30m
        expr: |
          sum(rate(http_requests_total{status_code=~"5.."}[30m])) by (job)
          /
          sum(rate(http_requests_total[30m])) by (job)

      - record: job:slo_errors_per_request:ratio_rate1h
        expr: |
          sum(rate(http_requests_total{status_code=~"5.."}[1h])) by (job)
          /
          sum(rate(http_requests_total[1h])) by (job)

      - record: job:slo_errors_per_request:ratio_rate6h
        expr: |
          sum(rate(http_requests_total{status_code=~"5.."}[6h])) by (job)
          /
          sum(rate(http_requests_total[6h])) by (job)

      - record: job:slo_errors_per_request:ratio_rate3d
        expr: |
          sum(rate(http_requests_total{status_code=~"5.."}[3d])) by (job)
          /
          sum(rate(http_requests_total[3d])) by (job)

      # Error Budget 剩余量（百分比）
      - record: job:slo_error_budget_remaining:ratio
        expr: |
          1 - (
            sum_over_time(job:slo_errors_per_request:ratio_rate5m[30d])
            /
            count_over_time(job:slo_errors_per_request:ratio_rate5m[30d])
          ) / 0.001
```

Recording Rules 的命名约定遵循 `level:metric:operations` 格式：
- `job` = 聚合维度
- `slo_errors_per_request` = 指标含义
- `ratio_rate5m` = 计算方式（比率 + 窗口）

## 延迟 SLI 的 PromQL 示例

除了错误率，P99 延迟是另一个常见 SLI。

```promql
# P99 延迟（使用 histogram_quantile，需要应用上报 histogram 类型指标）
histogram_quantile(0.99,
  sum(rate(http_request_duration_seconds_bucket{job="payment-service"}[5m])) by (le, job)
)

# 延迟 SLI：超过 1 秒的请求比例（另一种方式，更精确）
(
  sum(rate(http_request_duration_seconds_bucket{job="payment-service", le="1.0"}[5m]))
  /
  sum(rate(http_request_duration_seconds_count{job="payment-service"}[5m]))
)

# 综合 SLI：同时满足错误率和延迟的请求占比（复合 SLO）
(
  sum(rate(http_requests_total{job="payment-service", status_code!~"5..", duration_le="1.0"}[5m]))
  /
  sum(rate(http_requests_total{job="payment-service"}[5m]))
)
```

延迟的燃烧率配置方式和错误率完全相同，只是把 `ratio_rate*` 指标换成延迟 SLI 的 Recording Rule。

## Grafana Dashboard 设计

Error Budget Dashboard 需要回答三个问题：

1. **当前状态**：现在的错误率是多少，燃烧率是多少？
2. **历史趋势**：本月 Error Budget 消耗曲线
3. **剩余预算**：还剩多少 Error Budget？

### 推荐面板布局

**Row 1：当前状态（Stat 面板）**
- 当前错误率（`job:slo_errors_per_request:ratio_rate5m`）
- 当前燃烧率（错误率 / 0.001）
- Error Budget 剩余百分比

**Row 2：燃烧曲线（Time Series）**

```promql
# 各时间窗口燃烧率对比
job:slo_errors_per_request:ratio_rate1h{job="payment-service"} / 0.001
job:slo_errors_per_request:ratio_rate6h{job="payment-service"} / 0.001

# 告警阈值参考线
vector(14.4)  # P1 阈值
vector(6)     # P2 阈值
```

**Row 3：Error Budget 剩余量（Gauge + Time Series）**

```promql
# 剩余 Error Budget 百分比（Gauge，0-100%）
(1 - (
  sum_over_time(job:slo_errors_per_request:ratio_rate5m{job="payment-service"}[30d:5m])
  / count_over_time(job:slo_errors_per_request:ratio_rate5m{job="payment-service"}[30d:5m])
) / 0.001) * 100
```

**Row 4：请求量和错误分布（Bar Chart）**

```promql
# 按错误码分组的请求量
sum(rate(http_requests_total{job="payment-service"}[5m])) by (status_code)
```

### 颜色编码建议

Error Budget 剩余量 Gauge 使用阈值着色：
- 绿色：> 50%（健康）
- 黄色：20%-50%（关注）
- 橙色：5%-20%（告警）
- 红色：< 5%（危险）

## 告警文本模板：让 On-Call 一眼看懂

好的告警通知应该包含：**发生了什么、有多严重、该去哪里处理**。

```yaml
# Alertmanager 消息模板（Go template）
annotations:
  summary: |
    [{{ .Labels.severity | toUpper }}] {{ .Labels.job }} Error Budget 燃烧告警
  description: |
    🚨 服务：{{ .Labels.job }}
    📊 燃烧率：{{ $value | printf "%.1f" }}x（正常基准 1x）
    ⏱ 按此速度月度 Error Budget 将在 {{ if gt $value 14.4 }}2 小时{{ else if gt $value 6.0 }}6 小时{{ else }}本月底{{ end }}耗尽
    📉 当前 1h 错误率：{{ with query "job:slo_errors_per_request:ratio_rate1h" }}{{ . | first | value | humanizePercentage }}{{ end }}

    ➡️  Runbook：https://wiki.example.com/runbook/slo-burn-rate
    📈 Grafana：https://grafana.example.com/d/slo?var-job={{ .Labels.job }}
    🔍 日志：https://loki.example.com/?query={job="{{ .Labels.job }}"}
```

钉钉效果示意：

```
[CRITICAL] payment-service Error Budget 燃烧告警

服务：payment-service
燃烧率：18.3x（正常基准 1x）
⏱ 按此速度月度 Error Budget 将在 2 小时耗尽
当前 1h 错误率：1.83%

➡️ Runbook：...
📈 Grafana：...
```

收到这条告警，工程师不需要去查面板就知道：问题很严重（18x），很紧急（2 小时），要去哪里处理。

## 常见陷阱

**陷阱 1：忘记设置 `for` 参数**

```yaml
- alert: HighBurnRate
  expr: ...
  # 没有 for，瞬间触发
```

没有 `for` 的告警会在条件刚满足时立即触发，非常容易产生抖动误报。建议 P1 设 `for: 2m`，P2 设 `for: 15m`。

**陷阱 2：Recording Rules 的 interval 设置过长**

如果 Recording Rule 的 `interval: 5m`，而你的告警 `for: 2m`，告警可能因为数据刷新不及时而产生奇怪的行为。Recording Rule interval 应该 ≤ 告警的 `for` 时间的一半。

**陷阱 3：SLO 基准值写死在告警表达式里**

```yaml
# 糟糕的做法
expr: job:slo_errors_per_request:ratio_rate1h > 0.0144  # 14.4 × 0.001
```

当 SLO 从 99.9% 调整为 99.95% 时，你需要找到所有告警规则并更新。更好的做法是用 Recording Rule 存储 SLO 配置，或者在 Helm values 中管理。

```yaml
# 更好的做法（Helm values 注入）
expr: |
  job:slo_errors_per_request:ratio_rate1h{job="{{ .Values.service.name }}"}
  > (14.4 * {{ .Values.slo.errorBudget }})
```

**陷阱 4：多窗口条件写 OR 而不是 AND**

```yaml
# 错误：OR 条件太宽松，误报率高
expr: |
  job:slo_errors_per_request:ratio_rate1h > 0.0144
  or
  job:slo_errors_per_request:ratio_rate5m > 0.0144

# 正确：AND 条件，两个窗口都超标才告警
expr: |
  job:slo_errors_per_request:ratio_rate1h > 0.0144
  and
  job:slo_errors_per_request:ratio_rate5m > 0.0144
```

OR 条件会因为短窗口抖动频繁触发。多窗口方案的精髓就是 AND：短窗口提高灵敏度，长窗口过滤噪音。

**陷阱 5：只做错误率 SLO，忽略延迟 SLO**

用户感受到的"慢"和"错"同样影响体验。P99 延迟超 2 秒的比例，和错误率一样需要 Error Budget 管理。两个维度的 SLO 可以用不同的 recording rule 系列分别管理。

---

从简单阈值告警迁移到燃烧率告警需要一些前期工作（Recording Rules 配置、Dashboard 建立），但带来的回报是：每次 on-call 告警都有清晰的紧急程度，误报率大幅下降，工程师可以真正信任告警。

我们团队切换后，P1 告警的 MTTA 从平均 12 分钟降到 6 分钟——因为工程师知道这条告警不会是误报，第一时间响应而不是"观察一下"。
