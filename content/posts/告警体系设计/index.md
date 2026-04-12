---
title: "如何设计一个好的告警体系"
date: 2025-11-18T13:37:00+08:00
draft: false
tags: ["可观测性", "告警", "SRE", "运维"]
categories: ["Kubernetes"]
description: "从告警疲劳到 SLO 驱动的告警体系，分享构建可操作、低噪音告警系统的实践经验"
summary: "从真实的告警噪音泛滥经历出发，分享如何用 SLI/SLO 重新设计告警体系，包括告警分级、规则设计原则、路由策略和复盘机制。"
toc: true
math: false
diagram: false
series: ["SRE 实战手册"]
keywords: ["告警", "SLO", "SLI", "alertmanager", "prometheus", "SRE", "告警疲劳"]
params:
  reading_time: true
---

运维了一年多的 EKS 集群，有段时间钉钉群的告警消息一天能超过 200 条。到最后，所有人都对告警视而不见——因为大多数都是噪音。直到某天真正的故障来临，值班工程师因为习惯性地忽略告警，足足延误了 20 分钟才响应。

那次之后我们开始认真重构告警体系。这篇文章是我们在这个过程中的一些思考和实践。

---

## 告警噪音是怎么来的

回顾那段时间的告警，归结起来有几类：

**1. 阈值设置太敏感**

```yaml
# 错误的设置：CPU 超过 70% 就告警，几乎每天都触发
- alert: HighCPU
  expr: cpu_usage > 70
  for: 1m
```

服务正常运行时 CPU 在 60–80% 之间波动，这个阈值没有任何实际意义。

**2. 告警没有 for 缓冲**

```yaml
# 瞬时抖动就触发告警，30 秒后自己好了
- alert: PodRestartTooFrequent
  expr: kube_pod_container_status_restarts_total > 3
```

Pod 偶尔重启一两次是正常的，没有时间窗口的告警会在每次 Pod 启动时都触发。

**3. 原因告警而非症状告警**

一台节点的磁盘写入延迟高，触发了：
- 节点磁盘告警
- 该节点上所有 Pod 的请求延迟告警
- 依赖这些服务的上游告警

三层级联，同一个根因产生了十几条告警。

**4. 告警没有优先级，全部发同一个群**

P0 和 P3 的告警混在一起，P0 告警出现时被淹没了。

---

## 好告警的标准

Google SRE 书里提到的告警原则，我认为用四个词概括最准确：**可操作、及时、准确、上下文充足**。

**可操作**：每一条告警触发后，值班工程师应该知道下一步要做什么。如果一条告警触发后，工程师需要先去查另外三个系统才能判断是不是真正的问题，这条告警的设计就有问题。

**及时**：告警应该在用户感知到问题之前触发（或者至少同时）。一条告警在故障发生 30 分钟后才触发，已经没有意义了。

**准确**：告警应该精确反映真实问题，误报和漏报都是失败。误报会导致告警疲劳，漏报会导致故障扩大。

**上下文充足**：告警消息要包含足够的信息，让值班工程师不需要额外查询就能判断严重程度和初步方向。

---

## SLI/SLO 与告警的关系

这是我们重构告警最重要的思维转变：**基于症状告警，而不是基于原因告警**。

### 什么是 SLI 和 SLO

**SLI（Service Level Indicator）**：衡量服务健康的指标，通常是：
- 可用性：成功请求 / 总请求
- 延迟：P95/P99 响应时间
- 错误率：5xx / 总请求

**SLO（Service Level Objective）**：SLI 的目标值，例如：
- 可用性 ≥ 99.9%（30 天内最多允许 43.2 分钟不可用）
- P99 延迟 ≤ 500ms
- 错误率 ≤ 0.1%

### 基于 SLO 的告警设计

传统做法：监控各种基础设施指标（CPU、内存、磁盘），指标异常就告警。

SLO 做法：直接监控用户体验指标（错误率、延迟），用户感受到的问题才是告警的依据。

```yaml
# 传统告警：原因导向
- alert: HighCPU
  expr: container_cpu_usage_seconds_total > 0.8

# SLO 告警：症状导向（用户真正感受到的）
- alert: ErrorRateTooHigh
  expr: |
    sum(rate(http_requests_total{status=~"5.."}[5m])) /
    sum(rate(http_requests_total[5m])) > 0.01
  for: 5m
  labels:
    severity: critical
  annotations:
    summary: "{{ $labels.service }} 错误率超过 1%"
    description: "当前错误率: {{ $value | humanizePercentage }}，超过 SLO 阈值 0.1%"
    runbook: "https://wiki.internal/runbooks/high-error-rate"
```

### 错误预算告警

更进一步，可以基于错误预算消耗速率告警：

```yaml
# 30 天错误预算，如果当前速率继续，1 小时内会消耗 2% 的月度预算
- alert: ErrorBudgetBurnRateCritical
  expr: |
    (
      sum(rate(http_requests_total{status=~"5.."}[1h])) /
      sum(rate(http_requests_total[1h]))
    ) > 14.4 * 0.001   # 14.4 倍于 SLO 阈值
  for: 2m
  labels:
    severity: page   # 需要立即叫醒人
```

---

## 告警分级设计

我们目前使用四级告警：

| 级别 | 定义 | 响应时间 | 通知方式 |
|------|------|---------|---------|
| **P0** | 核心功能完全不可用，用户大规模受影响 | 立即（5 分钟内）| 电话 + IM |
| **P1** | 核心功能降级，部分用户受影响 | 15 分钟内 | IM（@人）|
| **P2** | 非核心功能异常，有应急方案 | 1 小时内 | IM 群通知 |
| **P3** | 低影响问题，需要关注但不紧急 | 工作时间内 | 告警看板 |

分级不是贴标签，而是真正指导响应行为。P0 必须有人立刻看，P3 允许明天上班再处理。

---

## 告警规则设计原则

### 1. 使用 `for` 时长过滤抖动

```yaml
# 没有 for：瞬时抖动就触发
- alert: HighMemory
  expr: memory_usage > 0.9

# 有 for：持续 5 分钟才触发
- alert: HighMemory
  expr: memory_usage > 0.9
  for: 5m   # 持续 5 分钟才触发告警
```

`for` 时长的选择：
- 需要立即感知的（错误率、服务不可用）：`for: 2m`
- 资源类（CPU、内存）：`for: 10m`
- 容量预警（磁盘将满）：`for: 30m`

### 2. 避免高基数 label

```yaml
# 差：user_id 是高基数 label，会产生大量时间序列
- alert: UserHighLatency
  expr: request_latency_seconds{user_id=~".+"} > 1

# 好：按服务聚合
- alert: ServiceHighLatency
  expr: histogram_quantile(0.99, rate(request_duration_seconds_bucket[5m])) > 1
```

### 3. 告警消息要包含操作指引

```yaml
annotations:
  summary: "{{ $labels.namespace }}/{{ $labels.deployment }} 副本数不足"
  description: |
    期望副本数: {{ $value }}
    当前可用副本数: {{ query "kube_deployment_status_replicas_available" | first | value }}
    
    可能原因:
    1. Pod 调度失败（检查节点资源）
    2. 容器镜像拉取失败
    3. 健康检查失败
    
    排查命令:
    kubectl describe deployment {{ $labels.deployment }} -n {{ $labels.namespace }}
    kubectl get events -n {{ $labels.namespace }} --sort-by=.lastTimestamp
  runbook: "https://wiki.internal/runbooks/deployment-unavailable"
```

### 4. 避免告警风暴：使用 inhibit 规则

当高级别告警触发时，抑制相关的低级别告警：

```yaml
# alertmanager.yml
inhibit_rules:
  # 节点宕机时，抑制该节点上所有 Pod 的告警
  - source_match:
      severity: critical
      alertname: NodeDown
    target_match:
      severity: warning
    equal: [node]
  
  # 服务完全不可用时，抑制相关的延迟告警
  - source_match:
      alertname: ServiceDown
    target_match:
      alertname: HighLatency
    equal: [service]
```

---

## 告警路由设计

不同级别、不同服务的告警应该路由到不同的人和渠道：

```yaml
# alertmanager.yml
route:
  receiver: default
  group_by: [alertname, cluster, service]
  group_wait: 30s       # 同组告警等待 30s 再发送（合并）
  group_interval: 5m    # 同组告警最短 5 分钟发一次
  repeat_interval: 4h   # 持续告警每 4 小时重复一次

  routes:
    # P0 告警：立即通知，电话告警
    - match:
        severity: critical
      receiver: pagerduty
      group_wait: 0s
      repeat_interval: 1h

    # P1 告警：钉钉 @相关人
    - match:
        severity: high
      receiver: dingtalk-oncall
      group_wait: 1m

    # 数据库相关告警路由给 DBA
    - match:
        component: database
      receiver: dba-team

    # 夜间静默非关键告警（22:00 - 08:00）
    - match_re:
        severity: "warning|info"
      mute_time_intervals:
        - night-hours
      receiver: dingtalk-noncritical

time_intervals:
  - name: night-hours
    time_intervals:
      - times:
          - start_time: "22:00"
            end_time: "08:00"
        weekdays: [monday:friday]
      - weekdays: [saturday, sunday]
```

---

## 告警复盘

告警风暴后，我们会做一次告警质量复盘，分析：

**1. 误报率**：过去 7 天内，有多少告警触发后被手动 resolve，没有实际操作？

```bash
# 在 Alertmanager API 中查询已 resolve 的告警
curl 'http://alertmanager:9093/api/v2/alerts?silenced=false&active=false' | \
  jq '[.[] | select(.status.state == "unprocessed")] | length'
```

**2. 无效告警**：有多少告警在 `for` 时间内就自动消失（说明只是抖动）？

**3. 响应时延**：P0 告警从触发到有人 ack，平均需要多少分钟？

**4. 遮盖问题**：有多少告警同时触发，互相遮盖？

基于复盘结果，我们会：
- 提高误报告警的 `for` 时长
- 删除 3 个月内从未触发的告警规则（可能是误配置或场景不存在）
- 优化告警分组规则，减少风暴

---

## 一些实际教训

**教训 1：先建 SLO，再建告警**

我们之前的告警大多是"能想到什么就告警什么"，没有体系。重新梳理后，先定义核心服务的 SLO，再基于 SLO 设计告警，告警数量从 80+ 条缩减到 23 条，但覆盖的真实问题反而更全了。

**教训 2：告警要有 runbook**

一条没有 runbook 的告警，工程师收到后第一反应是"这是什么？我该怎么办？"。每条告警都应该附上处理链接，哪怕只是一个简单的内部 wiki 页面。

**教训 3：不要把 metrics 展示图当告警**

"这个指标看起来重要，告警一下吧"——这是告警噪音的来源。告警的触发条件必须是"用户受影响"或"将要受影响"，纯粹的信息展示放在 Grafana dashboard，不发告警。

**教训 4：夜间静默要真的设置好**

值班工程师的精力是有限的，夜间频繁的非关键告警会导致告警疲劳，反而让真正的 P0 告警被忽略。我们现在夜间只有 P0/P1 告警会触发，P2/P3 等工作时间统一处理。

---

好的告警体系不是一次性建成的，是在一次次告警风暴后逐渐打磨出来的。核心是：**告警应该驱动行动，而不是制造焦虑**。每条告警触发时，都应该有人知道下一步要做什么，这才是告警存在的意义。
