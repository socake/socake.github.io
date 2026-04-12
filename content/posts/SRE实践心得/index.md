---
title: "SRE 实践心得：从运维到 SRE 的思维转变"
date: 2025-12-09T19:00:00+08:00
draft: false
tags: ["SRE", "运维", "云原生", "职业发展"]
categories: ["博客"]
description: "从传统运维视角理解 SRE 的本质转变：SLO 定义与错误预算、Toil 识别与消除、Blameless 文化，以及如何在团队中推动 SRE 实践"
summary: "SRE 不是换了个头衔的运维，而是一套用软件工程思维解决可靠性问题的方法论。这篇文章记录了我在实践过程中最有感触的几个转变。"
toc: true
math: false
diagram: false
series: ["SRE 实战手册"]
keywords: ["SRE", "SLO", "SLA", "错误预算", "Toil", "Blameless"]
params:
  reading_time: true
---

## 运维 vs SRE：不是换个名字

我见过很多团队把运维部门改名叫 SRE，然后继续干一样的事。这不是 SRE。

传统运维的核心关注点是"系统现在还活着吗"，被动响应告警，追求零停机，害怕变更。SRE 的核心关注点是"我们能在多大程度上接受不可靠，换来更快的交付速度"，主动设计可靠性，把停机当成正常的事情来管理。

最根本的差异在于两件事：

**1. SRE 用数据说话**：不是"感觉稳定性还不错"，而是"过去 30 天 P99 延迟 < 200ms 的时间占比是 99.8%"。

**2. SRE 把可靠性当成功能来交付**：就像开发要交付业务功能，SRE 要交付可靠性功能——监控、告警、自动恢复、灾难演练。

---

## SLI/SLO/SLA：从模糊到量化

### 三个概念的关系

- **SLI**（Service Level Indicator）：衡量服务质量的具体指标。比如"成功请求比例"、"P99 延迟"。
- **SLO**（Service Level Objective）：对 SLI 设定的目标。比如"成功率 ≥ 99.9%"、"P99 延迟 ≤ 500ms"。
- **SLA**（Service Level Agreement）：对外承诺的协议，通常比 SLO 宽松，违反了有赔偿。

**关系**：SLI 是测量，SLO 是内部目标，SLA 是外部承诺。先有 SLO，才能谈 SLA。

### 如何定义你的服务 SLO

步骤一：找到用户最在意的体验，转化为 SLI。

```
用户在意：页面打开快不快
→ SLI：HTTP 请求成功率 & P95/P99 延迟

用户在意：数据准不准
→ SLI：数据处理任务的成功率

用户在意：功能能不能用
→ SLI：核心功能的可用率（用探针定期检测）
```

步骤二：基于历史数据设定合理目标，不要拍脑袋。

```bash
# 用 Prometheus 查过去 30 天的实际 P99 延迟
histogram_quantile(0.99,
  sum(rate(http_request_duration_seconds_bucket{job="api-server"}[30d])) by (le)
)

# 查历史成功率
sum(rate(http_requests_total{status!~"5.."}[30d]))
/
sum(rate(http_requests_total[30d]))
```

步骤三：SLO 要比当前实际情况稍微严一点，但不能太严。

如果历史成功率是 99.95%，SLO 设 99.99% 是在给自己挖坑。SLO 应该是"用户开始不满意的临界点"，不是"我们技术上能做到的极限"。

### Prometheus 告警规则示例

```yaml
# 基于 SLO 的告警（而不是简单阈值）
groups:
  - name: slo-alerts
    rules:
      # 错误率告警（1小时窗口，快速告警）
      - alert: HighErrorRateFast
        expr: |
          (
            sum(rate(http_requests_total{status=~"5..",job="api-server"}[1h]))
            /
            sum(rate(http_requests_total{job="api-server"}[1h]))
          ) > 0.01
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: "API 错误率超过 1%（1小时窗口）"
          description: "当前错误率 {{ $value | humanizePercentage }}，SLO 目标 0.1%"

      # 延迟告警
      - alert: HighLatencyP99
        expr: |
          histogram_quantile(0.99,
            sum(rate(http_request_duration_seconds_bucket{job="api-server"}[5m])) by (le)
          ) > 0.5
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "P99 延迟超过 500ms"
```

---

## 错误预算：用数据说服业务方

### 什么是错误预算

SLO 是 99.9%，那错误预算就是 0.1%。一个月（43800 分钟）里，允许不可用 43.8 分钟。

错误预算的妙处在于：**它把可靠性从"工程问题"变成了"资源分配问题"**。

发布新功能有风险，可能消耗错误预算。如果预算还很充裕，发！如果预算快耗完了，先稳定再迭代。这个决策不再是"运维说不能发"，而是"数据说我们还有多少余量"。

### 错误预算燃尽率告警（Burn Rate）

单纯看错误率不够，还要看消耗速度。如果按当前速度继续，30 天的预算在 3 天内就会耗尽，那必须立刻处理，即使当前错误率看起来不高。

```yaml
# 错误预算消耗速率告警
- alert: ErrorBudgetBurnRate
  expr: |
    (
      # 1小时窗口的消耗速率
      (1 - sum(rate(http_requests_total{status!~"5.."}[1h])) / sum(rate(http_requests_total[1h])))
      /
      0.001  # SLO 是 99.9%，错误预算是 0.1%
    ) > 14.4  # 14.4x 意味着 1小时内消耗了72小时的预算
  for: 2m
  labels:
    severity: critical
  annotations:
    summary: "错误预算消耗速率过高"
    description: "按当前速率，30天错误预算将在 {{ 30 / $value | humanizeDuration }} 内耗尽"
```

这个 14.4 怎么来的：如果 1 小时内消耗速率是正常的 14.4 倍，意味着 30 天的预算在 30/14.4 ≈ 2 天内耗尽，属于紧急情况。

---

## Toil：识别并消除重复劳动

### 什么是 Toil

Google SRE 对 Toil 的定义很精确：
- **手动的**：需要人工触发或干预
- **重复的**：同样的事情反复做
- **可自动化的**：理论上可以用代码替代
- **没有持久价值的**：做完不留下改进，下次还要做同样的事

**不是所有繁琐工作都是 Toil**。设计新的监控告警规则是有价值的工程工作，不是 Toil。每次发布手动去检查 10 个 Dashboard 确认健康，是 Toil。

### 量化 Toil

```bash
# 简单但有效：让团队每周记录花在 Toil 上的时间
# Toil 时间 / 总工作时间，SRE 建议不超过 50%

# 常见 Toil 类型统计（示例）
# 手动扩容：每次 10-15 分钟，每周 3-5 次
# 日志手动查询：每次 20 分钟，每天 2-3 次
# 证书手动续期：每次 30 分钟，每季度 10+ 次
# 数据库慢查询手动分析：每次 1 小时，每周 1-2 次
```

### 消除 Toil 的优先级

高频 + 高时间成本 = 立刻自动化。

```bash
# 示例：手动扩容 → HPA 自动扩容
kubectl autoscale deployment api-server \
  --cpu-percent=70 \
  --min=3 \
  --max=50

# 示例：证书手动续期 → cert-manager 自动续期
helm install cert-manager jetstack/cert-manager \
  --namespace cert-manager \
  --create-namespace \
  --set installCRDs=true
```

---

## Blameless Postmortem 文化

这是 SRE 实践中落地最难的部分，因为它要改变组织文化，而不只是技术流程。

### 为什么 Blameless 这么难

人类天然倾向于找替罪羊。故障后问"谁干的"，比问"系统为什么允许这件事发生"容易得多，也更有情绪宣泄感。

但追责文化的后果是灾难性的：
- 人们开始隐瞒问题，避免被追责
- 本可以早发现的问题被压着，直到爆发成更大故障
- 团队失去心理安全感，没人愿意承担有风险的改进工作

### 实践 Blameless 的关键

**1. 把"人为错误"当成症状，不是根因**

"工程师执行了错误的命令" 不是根因。根因是"为什么系统允许这个命令被执行而没有任何保护"。

**2. 区分个人能力问题和系统设计问题**

极少数情况下是纯粹的个人能力问题。大多数故障是系统设计给人挖的坑（文档不清、没有二次确认、缺少防护机制）。

**3. 复盘会议的话术**

```
❌ "你为什么没有检查这个配置？"
✓  "这个检查步骤是否应该加入自动化流程或 Checklist？"

❌ "这个人不适合干这个工作"
✓  "我们的 Runbook 是否清晰到让任何人都能正确执行这个操作？"
```

---

## 可靠性与速度的平衡

这是 SRE 存在的根本张力：开发想快速发布，SRE 想保持稳定，怎么协调？

**错误预算是最好的协调工具**（前文已述）。但还有几个实践值得提：

### 渐进式发布（Progressive Delivery）

```yaml
# 用 Argo Rollouts 做金丝雀发布
apiVersion: argoproj.io/v1alpha1
kind: Rollout
spec:
  strategy:
    canary:
      steps:
        - setWeight: 5      # 先放 5% 流量
        - pause: {duration: 10m}
        - setWeight: 20
        - pause: {duration: 10m}
        - setWeight: 50
        - pause: {duration: 10m}
        - setWeight: 100
      analysis:
        templates:
          - templateName: success-rate
        startingStep: 2
        args:
          - name: service-name
            value: api-server
```

### 功能开关（Feature Flag）

```python
# 用 LaunchDarkly 或自己实现简单的功能开关
def process_payment(user_id: str, amount: float):
    if feature_flag.is_enabled("new_payment_flow", user_id):
        return new_payment_processor.process(user_id, amount)
    else:
        return legacy_payment_processor.process(user_id, amount)
```

功能开关让发布和功能上线解耦——代码发出去了，功能还没打开，有问题可以立刻关掉，不需要回滚。

---

## 实践建议：从哪里开始

很多团队说"我们要做 SRE"，然后不知道从哪下手。我的建议是从最痛的地方开始，而不是从最"SRE"的地方开始：

### 第一步：定义一个 SLO，哪怕只有一个

找到你们最核心的服务，定义一个 SLI，设一个 SLO。用 Prometheus + Grafana 把它可视化出来。

这一步看起来简单，但它逼着团队回答"用户最在意什么"这个问题，往往会引发很有价值的讨论。

### 第二步：记录 Toil，量化它

让团队在下周开始记录自己花在 Toil 上的时间。不需要 100% 精确，大概就行。

拿到数据后，选最耗时的一项 Toil，两周内把它自动化掉。让团队感受到"原来减少 Toil 是真的可以做到的"。

### 第三步：做一次认真的故障复盘

找一个最近的、有代表性的故障（不需要是 P0），严格按照 Blameless 原则做一次复盘，输出清晰的行动项，并跟踪完成。

关键是跟踪完成。很多团队开复盘会、写报告，然后行动项在任务系统里尘封。跟踪落实才是建立可靠性改进文化的核心。

### 不要一步到位

SRE 转型是一个 2-3 年的过程，不是一个季度就能完成的事。先把 Toil 降下来，让团队有时间做有价值的工作；先把 SLO 建起来，让可靠性有了度量；先有一次好的复盘，让团队感受到 Blameless 文化的价值。

工具和流程是次要的，文化和思维方式才是核心。一个有 SRE 文化的团队，用烂工具也能做好可靠性；一个没有 SRE 文化的团队，用最好的工具也是徒劳。
