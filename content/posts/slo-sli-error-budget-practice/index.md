---
title: "SLO/SLI/Error Budget 从理论到落地：SRE 可靠性工程实战"
date: 2026-04-12T13:00:00+08:00
draft: false
tags: ["SRE", "SLO", "SLI", "Error Budget", "Prometheus", "Grafana", "可观测性"]
categories: ["Kubernetes"]
description: "SLO/SLI/Error Budget 生产落地：Prometheus recording rules、Grafana Dashboard、burn rate 告警"
summary: "从 SLI 指标选取到 Error Budget 消耗速率告警，系统讲解 SRE 可靠性工程体系的落地实践，包括 Prometheus recording rules 计算 SLI、多窗口 burn rate 告警规则配置、SLO 违规复盘流程，以及与开发团队的协作策略。"
toc: true
math: false
diagram: false
series: ["SRE 实战手册"]
keywords: ["SLO", "SLI", "Error Budget", "SRE", "Prometheus", "burn rate", "可靠性工程"]
params:
  reading_time: true
---

三年前我在推 SLO 体系时，被开发团队问了一个让我一时语塞的问题："我们已经有 uptime 监控了，为什么还要搞这么复杂的东西？" 这篇文章是我对这个问题的完整回答，以及这三年实践下来的经验总结。

## 为什么需要 SLO 而不是 uptime

传统的 uptime 监控只告诉你服务"是否在线"，但用户体验远比这复杂：服务在线但响应需要 30 秒，算不算正常？成功率 95% 但某个核心接口失败率 50%，算不算正常？

SLO（Service Level Objective）体系的核心价值是**把可靠性量化，让工程决策有依据**：

- 没有 Error Budget：每次故障都是"严重事故"，团队陷入焦虑循环
- 有 Error Budget：可以理性讨论"我们还有多少容忍度"，发布决策有数据支撑

## SLI 指标选取

SLI（Service Level Indicator）是用来衡量服务质量的具体指标。选取原则：**站在用户视角，衡量用户实际感受到的服务质量。**

### 三类核心 SLI

**1. 可用性 SLI（Availability）**

最直接的衡量方式：成功请求占总请求的比例。

```
availability = successful_requests / total_requests
```

什么算"成功"需要明确定义：
- HTTP 2xx/3xx 算成功
- 4xx 通常不算服务失败（是客户端错误），但要视业务而定
- 5xx 算服务失败
- 超时算失败

**2. 延迟 SLI（Latency）**

不要用平均延迟，用分位数：

```
latency_p99 = 99th percentile of request duration
```

选 P99 还是 P999 取决于业务场景。支付类接口对尾延迟敏感，可以用 P999；普通查询接口用 P99 足够。

**3. 错误率 SLI（Error Rate）**

```
error_rate = error_requests / total_requests
```

有时候可用性 SLI 和错误率 SLI 是等价的（`availability = 1 - error_rate`），但某些场景需要分开：比如可以接受 5xx 但不接受超时，就需要分别计算。

### SLI 选取的常见误区

- **用基础设施指标代替用户体验指标**：CPU 使用率高不一定导致用户受影响，不适合作为 SLI
- **SLI 太多**：一个服务超过 5 个 SLI 就难以管理，聚焦在最影响用户的 2-3 个
- **忽略长尾用户**：平均延迟良好不代表没有用户在受苦

## Prometheus Recording Rules 计算 SLI

Recording Rules 把复杂的 PromQL 预计算成新的时序，显著提升 Dashboard 查询性能，也让告警规则更简洁。

**首先，需要确保有原始指标。** 以 HTTP 服务为例，Prometheus 通常会有：

```
# HTTP 请求总数（含状态码标签）
http_requests_total{service="my-service", status="200"}
http_requests_total{service="my-service", status="500"}

# HTTP 请求延迟直方图
http_request_duration_seconds_bucket{service="my-service", le="0.1"}
http_request_duration_seconds_bucket{service="my-service", le="0.5"}
http_request_duration_seconds_bucket{service="my-service", le="1.0"}
```

**定义 Recording Rules：**

```yaml
# prometheus-rules.yaml
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: slo-recording-rules
  namespace: monitoring
  labels:
    prometheus: kube-prometheus
    role: alert-rules
spec:
  groups:
    # ===== 原始 SLI 计算（5m 窗口）=====
    - name: slo.sli.raw
      interval: 30s
      rules:
        # 可用性：过去5分钟的成功请求比例
        - record: job:http_requests:success_rate5m
          expr: |
            sum(rate(http_requests_total{status=~"2..|3.."}[5m])) by (job)
            /
            sum(rate(http_requests_total[5m])) by (job)

        # 错误率：过去5分钟的5xx比例
        - record: job:http_requests:error_rate5m
          expr: |
            sum(rate(http_requests_total{status=~"5.."}[5m])) by (job)
            /
            sum(rate(http_requests_total[5m])) by (job)

        # P99 延迟
        - record: job:http_request_duration_seconds:p99_5m
          expr: |
            histogram_quantile(
              0.99,
              sum(rate(http_request_duration_seconds_bucket[5m])) by (job, le)
            )

    # ===== SLO 窗口计算（用于 Error Budget）=====
    - name: slo.windows
      interval: 30s
      rules:
        # 过去1小时的错误预算消耗
        - record: job:http_requests:error_budget_burn_rate1h
          expr: |
            (
              1 - sum(rate(http_requests_total{status=~"2..|3.."}[1h])) by (job)
              / sum(rate(http_requests_total[1h])) by (job)
            )
            / (1 - 0.999)  # 除以 (1 - SLO目标)，SLO=99.9%

        # 过去6小时的消耗速率
        - record: job:http_requests:error_budget_burn_rate6h
          expr: |
            (
              1 - sum(rate(http_requests_total{status=~"2..|3.."}[6h])) by (job)
              / sum(rate(http_requests_total[6h])) by (job)
            )
            / (1 - 0.999)

        # 过去3天的消耗速率
        - record: job:http_requests:error_budget_burn_rate3d
          expr: |
            (
              1 - sum(rate(http_requests_total{status=~"2..|3.."}[3d])) by (job)
              / sum(rate(http_requests_total[3d])) by (job)
            )
            / (1 - 0.999)

        # 30天窗口剩余 Error Budget 百分比
        - record: job:http_requests:error_budget_remaining30d
          expr: |
            1 - (
              (
                1 - sum(rate(http_requests_total{status=~"2..|3.."}[30d])) by (job)
                / sum(rate(http_requests_total[30d])) by (job)
              )
              / (1 - 0.999)
            )
```

**burn rate 的含义：**

- burn rate = 1：Error Budget 消耗速率恰好等于 SLO 允许的速率（30天刚好耗尽）
- burn rate = 2：消耗速度是正常的2倍（15天就耗尽）
- burn rate = 14.4：在1小时内消耗了约5%的月度 Error Budget（需要立即响应）

## Grafana SLO Dashboard

一个好的 SLO Dashboard 需要展示三层信息：当前 SLI 状态、Error Budget 剩余量、历史趋势。

**关键 Panel 配置：**

```json
// Panel 1: 当前可用性（Stat Panel）
{
  "title": "服务可用性（过去5分钟）",
  "type": "stat",
  "targets": [
    {
      "expr": "job:http_requests:success_rate5m{job='my-service'} * 100",
      "legendFormat": "可用性 %"
    }
  ],
  "fieldConfig": {
    "defaults": {
      "unit": "percent",
      "thresholds": {
        "steps": [
          {"color": "red", "value": 0},
          {"color": "yellow", "value": 99},
          {"color": "green", "value": 99.9}
        ]
      }
    }
  }
}
```

```json
// Panel 2: Error Budget 剩余（Gauge Panel）
{
  "title": "Error Budget 剩余（本月）",
  "type": "gauge",
  "targets": [
    {
      "expr": "job:http_requests:error_budget_remaining30d{job='my-service'} * 100",
      "legendFormat": "剩余 %"
    }
  ],
  "fieldConfig": {
    "defaults": {
      "unit": "percent",
      "min": 0,
      "max": 100,
      "thresholds": {
        "steps": [
          {"color": "red", "value": 0},
          {"color": "yellow", "value": 25},
          {"color": "green", "value": 50}
        ]
      }
    }
  }
}
```

**Grafana Dashboard JSON 关键配置（Time Series Panel）：**

```json
// Panel 3: Burn Rate 趋势
{
  "title": "Error Budget 消耗速率",
  "type": "timeseries",
  "targets": [
    {
      "expr": "job:http_requests:error_budget_burn_rate1h{job='my-service'}",
      "legendFormat": "1h burn rate"
    },
    {
      "expr": "job:http_requests:error_budget_burn_rate6h{job='my-service'}",
      "legendFormat": "6h burn rate"
    }
  ],
  "options": {
    "tooltip": {"mode": "multi"}
  },
  "fieldConfig": {
    "overrides": [
      {
        "matcher": {"id": "byName", "options": "1h burn rate"},
        "properties": [
          {"id": "color", "value": {"mode": "fixed", "fixedColor": "orange"}}
        ]
      }
    ]
  }
}
```

## Error Budget Burn Rate 告警

Google SRE 书中推荐的多窗口 burn rate 告警是目前最实用的告警策略，核心思路是：**用多个时间窗口交叉确认，避免短暂毛刺触发告警，也避免低速消耗长期不告警。**

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: slo-alerts
  namespace: monitoring
spec:
  groups:
    - name: slo.alerts
      rules:
        # 告警级别 P1：快速消耗（2% Error Budget 在1小时内）
        # burn rate 14.4 = 消耗速率是正常的14.4倍
        # 在1小时内消耗了5%的月度 Error Budget 就需要立即处理
        - alert: SLOBurnRateCritical
          expr: |
            (
              job:http_requests:error_budget_burn_rate1h{job="my-service"} > 14.4
              AND
              job:http_requests:error_budget_burn_rate5m{job="my-service"} > 14.4
            )
          for: 2m  # 持续2分钟才告警，过滤毛刺
          labels:
            severity: critical
            team: backend
          annotations:
            summary: "{{ $labels.job }} SLO Error Budget 快速消耗"
            description: |
              服务 {{ $labels.job }} 的 Error Budget 消耗速率为 {{ $value | humanize }}x（正常值1x）。
              当前速率下，月度 Error Budget 将在 {{ div 730 $value | humanizeDuration }} 内耗尽。
              当前消耗速率：{{ $value }}
            runbook_url: "https://wiki.example.com/runbooks/slo-burnrate"

        # 告警级别 P2：中速消耗（5% Error Budget 在6小时内）
        - alert: SLOBurnRateHigh
          expr: |
            (
              job:http_requests:error_budget_burn_rate6h{job="my-service"} > 6
              AND
              job:http_requests:error_budget_burn_rate30m{job="my-service"} > 6
            )
          for: 15m
          labels:
            severity: warning
            team: backend
          annotations:
            summary: "{{ $labels.job }} SLO Error Budget 消耗偏高"
            description: |
              服务 {{ $labels.job }} 的 Error Budget 消耗速率偏高（{{ $value | humanize }}x）。
              请检查服务状态，评估是否需要暂停发布。

        # 告警级别 P3：慢速消耗（10% Error Budget 在3天内）
        - alert: SLOBurnRateMedium
          expr: |
            (
              job:http_requests:error_budget_burn_rate3d{job="my-service"} > 1
              AND
              job:http_requests:error_budget_burn_rate6h{job="my-service"} > 1
            )
          for: 1h
          labels:
            severity: info
            team: backend
          annotations:
            summary: "{{ $labels.job }} SLO Error Budget 消耗持续"
            description: "Error Budget 消耗速率大于1x，本月 Error Budget 有超支风险。"

        # Error Budget 耗尽告警
        - alert: SLOErrorBudgetExhausted
          expr: |
            job:http_requests:error_budget_remaining30d{job="my-service"} < 0.1
          for: 5m
          labels:
            severity: critical
          annotations:
            summary: "{{ $labels.job }} Error Budget 剩余不足10%"
            description: "本月 Error Budget 剩余 {{ $value | humanizePercentage }}，建议冻结非紧急发布。"
```

**多窗口告警的逻辑说明：**

使用两个窗口 AND 的原因：
- **长窗口**（1h、6h、3d）检测持续性问题，过滤短暂抖动
- **短窗口**（5m、30m）确认问题正在发生，过滤历史遗留的"尾巴"

## SLO 违规复盘流程

SLO 违规（Error Budget 耗尽或接近耗尽）后，需要有结构化的复盘流程。我用的模板：

**复盘文档结构：**

```markdown
# SLO 违规复盘：my-service 2026-04-10

## 事件概述
- 违规时间窗口：2026-04-10 14:30 ~ 2026-04-10 16:45
- 受影响 SLI：HTTP 可用性（目标 99.9%，实际 97.2%）
- Error Budget 消耗：本月预算的 68%（本次事件消耗）

## 时间线
| 时间 | 事件 |
|------|------|
| 14:25 | 发布 v2.3.4 |
| 14:30 | P1 告警触发，burn rate 超过14.4 |
| 14:35 | 值班工程师响应 |
| 14:42 | 确认是新版本问题，开始回滚 |
| 14:58 | 回滚完成，服务恢复 |
| 16:45 | 延迟影响彻底消除 |

## 根因分析
新版本引入了一个 N+1 查询问题，在高流量下数据库连接池耗尽，导致大量请求超时。

## 改进措施
| 措施 | 负责人 | 截止日期 |
|------|--------|----------|
| 添加数据库连接数监控告警 | 张三 | 2026-04-17 |
| 代码审查加入查询性能检查 | 李四 | 2026-04-20 |
| 引入 SQL 慢查询自动检测 | 王五 | 2026-04-24 |
```

**Error Budget 冻结策略：** 当 Error Budget 消耗超过 50% 时，我们的策略是：
1. 所有非紧急功能发布暂停
2. 只允许修复性发布
3. 下次发布前必须经过 SRE review

## 与开发团队的沟通策略

这是 SLO 体系落地最难的部分，技术实现反而简单。

**常见阻力和应对：**

**阻力1："99.9% 太严格了，我们根本达不到"**

解法：把 SLO 目标和 Error Budget 一起讲。"我们允许每月有 43 分钟不可用，目前我们还有 30 分钟的余量，这次发布需要评估风险。" 数字比百分比更有说服力。

**阻力2："告警太多了，都是误报"**

解法：让开发参与调整告警阈值和 for 时间。告警质量是需要持续迭代的，第一版一定不完美。记录每次告警是否有效，3个月后回顾一次。

**阻力3："我的功能很重要，必须按时发布"**

解法：把 Error Budget 展示在公共 Dashboard 上，让发布决策透明化。"当前还有 X% 的 Error Budget，这次发布风险评估是 Y，是否继续发布由团队共同决定。"

**推进 SLO 文化的实用建议：**

```
第一阶段（1-2个月）：
  - 只观察，不告警
  - 建立 Dashboard，让团队熟悉指标含义
  - 找出数据异常点，修正 SLI 计算逻辑

第二阶段（3-4个月）：
  - 只有 P1 告警（快速消耗）
  - 每月做一次 Error Budget 回顾会议
  - 建立 SLO 违规复盘流程

第三阶段（5个月+）：
  - 引入 Error Budget 冻结策略
  - SLO 指标影响发布决策
  - 定期调整 SLO 目标（每季度回顾）
```

## Sloth：SLO 配置自动化

手写 Recording Rules 和告警规则容易出错，[Sloth](https://github.com/slok/sloth) 可以从简单的 SLO 定义自动生成 Prometheus 规则：

```yaml
# sloth-slo.yaml
apiVersion: sloth.slok.dev/v1
kind: PrometheusServiceLevel
metadata:
  name: my-service-slo
  namespace: monitoring
spec:
  service: "my-service"
  labels:
    team: "backend"
  slos:
    - name: "requests-availability"
      objective: 99.9
      description: "HTTP 请求可用性 SLO"
      sli:
        events:
          error_query: sum(rate(http_requests_total{job="my-service", status=~"5.."}[{{.window}}]))
          total_query: sum(rate(http_requests_total{job="my-service"}[{{.window}}]))
      alerting:
        name: MyServiceHighErrorRate
        labels:
          team: backend
        annotations:
          summary: "my-service 错误率过高"
        page_alert:
          labels:
            severity: critical
        ticket_alert:
          labels:
            severity: warning
```

```bash
# 生成 Prometheus 规则
sloth generate -i sloth-slo.yaml -o generated-rules.yaml

# 应用
kubectl apply -f generated-rules.yaml
```

## 总结

SLO 体系的价值不仅仅是技术监控，更重要的是建立了**可靠性的共同语言**：

1. **从用户视角定义 SLI**：不要用系统指标，要用用户感受到的指标
2. **Recording Rules 先行**：预计算好 SLI，才能高效查询和告警
3. **多窗口 burn rate 告警**：是目前最实用的 SLO 告警策略，减少噪音
4. **Error Budget 是决策工具**：让发布风险可量化，而不是靠直觉判断
5. **渐进式推进**：先观察后告警，让团队接受需要时间

最后一点：SLO 目标不是越高越好。一味追求 99.99% 会让团队疲于应付，正确的 SLO 应该是"足够好"——比用户期望稍高一点，但留下足够的 Error Budget 让工程师可以安心迭代创新。
