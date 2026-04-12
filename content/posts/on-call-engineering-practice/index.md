---
title: "On-Call 工程实践：从告警响应到 Runbook 设计"
date: 2026-04-12T10:00:00+08:00
draft: false
tags: ["SRE", "On-Call", "告警", "Runbook", "故障响应"]
categories: ["SRE"]
series: ["SRE 可靠性工程师路径"]
description: On-Call 工程实践：如何设计告警路由、编写 Runbook、建立升级策略，以及用数据驱动告警质量持续优化
summary: 好的 On-Call 体系不是让人 24 小时盯着屏幕，而是让每一次叫醒都有价值。从告警质量到 Runbook 设计，从轮班制度到数据驱动改进，这篇文章是我们团队在生产环境打磨 3 年的实践总结。
toc: true
math: false
diagram: false
keywords: ["On-Call", "SRE", "Runbook", "告警质量", "MTTA", "PagerDuty", "故障响应"]
params:
  reading_time: true
---

凌晨 2:47，手机响了。

我盯着 PagerDuty 的推送，告警标题是 `high_cpu_usage on node-03`。CPU 95%，持续 10 分钟。我花了 20 分钟翻日志、登服务器，最终发现是一个定时任务跑完了，CPU 早就回落了。那次告警什么都没做，只是让我少睡了一小时。

这就是烂告警的样子。它叫醒了你，但没告诉你该做什么，也没有真正的问题需要你处理。

On-Call 的核心矛盾不是技术问题，是信噪比问题。

## 什么样的告警值得叫醒人

我用三个标准判断一个告警是否值得进 on-call rotation：

**可操作性（Actionable）**：工程师收到告警后，有明确的处理步骤。如果第一反应是"先看看是不是误报"，这个告警就没达标。

**紧急性（Urgent）**：需要立即人工干预，不处理会造成或加剧服务影响。能等到工作时间处理的，就不应该在凌晨叫人。

**真实性（Real）**：告警代表真实的用户可感知的问题，不是中间状态、不是自愈中的瞬间抖动。

### 症状告警 vs 原因告警

这是提升告警质量最关键的一步区分。

**原因告警**示例：`node CPU > 90%`、`disk iops > 5000`、`JVM GC pause > 200ms`

**症状告警**示例：`HTTP 5xx rate > 1%`、`P99 latency > 2s`、`payment success rate < 99%`

原因告警的问题在于：高 CPU 可能导致延迟上升，也可能什么问题都没有（批处理任务）。症状告警直接反映用户感受，是更稳定的告警信号。

我们团队的规则：**优先配症状告警，原因告警只在对应症状告警响应中作为排查辅助**。CPU 高不进 on-call，但 API 延迟超 SLO 触发的 on-call 响应过程中，CPU 高作为面板数据供参考。

## 标准 Runbook 模板

好告警的配套是好 Runbook。每条进入 on-call 的告警，必须有对应的 Runbook 链接。

以下是我们的 Runbook 标准模板：

```markdown
# [服务名] [告警名] Runbook

## 基本信息
- **告警名称**：payment_service_error_rate_high
- **触发条件**：5 分钟内 HTTP 5xx 比例 > 1%
- **Runbook 版本**：v2.3（2026-03-15 更新）
- **值班负责人**：支付团队 on-call

## 影响范围评估
| 级别 | 条件 | 影响 |
|------|------|------|
| P1 | 错误率 > 5% 持续 5min | 支付全面不可用，直接影响营收 |
| P2 | 错误率 1%-5% | 部分用户支付失败，影响转化率 |
| P3 | 错误率 < 1% 但持续 | 长尾用户受影响，需关注趋势 |

## 第一步（前 2 分钟）

1. 打开 [Grafana 支付服务面板](https://grafana.example.com/d/payment)
2. 确认告警是真实的，不是单点抖动
3. 查看错误分布：是全部接口还是特定接口？
4. 通知渠道：在 #incident 频道发送：`[P?] 支付服务错误率告警触发，正在排查`

## 排查决策树

```
错误率高
├── 是否有最近部署？
│   ├── 是 → 检查部署时间点和错误开始时间是否吻合 → 考虑回滚
│   └── 否 → 继续下一步
├── 查看依赖服务健康状态（数据库/Redis/上游 API）
│   ├── 数据库连接失败 → 见 [DB 连接排查 Runbook]
│   ├── Redis 超时 → 见 [Redis 排查 Runbook]
│   └── 依赖正常 → 继续下一步
├── 查看应用日志：kubectl logs -n payment deploy/payment-service --tail=100
│   ├── OOM/panic → 检查内存用量，考虑重启
│   └── 业务异常 → 上报研发团队
```

## 处理方案

**方案 A：回滚**（适用于最近 2 小时内有部署）
```bash
# 查看当前版本
kubectl get deploy payment-service -n payment -o jsonpath='{.spec.template.spec.containers[0].image}'
# 回滚到上一版本
kubectl rollout undo deploy/payment-service -n payment
# 验证回滚状态
kubectl rollout status deploy/payment-service -n payment
```

**方案 B：重启 Pod**（适用于单 Pod 异常）
```bash
kubectl delete pod -n payment -l app=payment-service --field-selector=status.phase=Running
```

**方案 C：紧急扩容**（适用于流量突增）
```bash
kubectl scale deploy/payment-service -n payment --replicas=10
```

## 升级条件
- 排查超过 15 分钟无法定位原因 → 呼叫 Tech Lead
- P1 级别故障，无论能否定位 → 立即通知 EM 和 CTO
- 涉及数据异常（重复扣款/漏单）→ 立即通知业务团队停止相关功能
```

这个 Runbook 的关键点：**决策树替代文字描述**，工程师凌晨 3 点大脑不清醒，不要让他们读段落，给他们一条明确的操作路径。

## On-Call 轮班制度设计

### 跟随时区的排班

我们团队分布在北京和新加坡，跟随时区设计：

- **主班（Primary）**：每人连续 7 天，工作时间内优先响应
- **备班（Secondary）**：主班无响应时自动升级，约定 5 分钟窗口
- **经理升级（Manager Escalation）**：P1 故障 15 分钟无响应时触发

```yaml
# PagerDuty Escalation Policy 配置示意
escalation_policy:
  name: "Payment Service On-Call"
  rules:
    - escalation_delay_in_minutes: 5
      targets:
        - type: schedule
          id: PRIMARY_SCHEDULE_ID
    - escalation_delay_in_minutes: 10
      targets:
        - type: schedule
          id: SECONDARY_SCHEDULE_ID
    - escalation_delay_in_minutes: 15
      targets:
        - type: user
          id: TECH_LEAD_USER_ID
```

### 换班健康规则

这些规则是从痛苦中总结出来的：

1. **最小 on-call 人员**：一个服务至少 4 人参与轮换，否则每人每月 on-call 周超过 1 周，会有严重的倦怠感
2. **凌晨告警补偿**：00:00-06:00 被叫醒，次日工作时间可减少 2 小时
3. **新人保护期**：新加入 on-call rotation 的工程师，前 2 周必须有 Shadow（跟着老人一起处理）

## 告警质量度量

度量是改进的基础。我们用以下指标追踪告警质量，每月回顾一次。

### Alert Fatigue Rate（告警噪音率）

```
噪音率 = 未采取任何处理动作的告警数 / 总告警数 × 100%
```

目标：< 10%。我见过噪音率超过 60% 的团队，on-call 工程师已经条件反射地忽略大部分告警。

### MTTA（Mean Time to Acknowledge）

```
MTTA = sum(告警触发到第一次 acknowledge 的时间) / 告警总数
```

目标：工作时间 < 5 分钟，非工作时间 < 15 分钟。MTTA 长意味着工程师压力大、告警太多，或 escalation policy 不合理。

### Actionable Alert Rate（有效告警率）

```
有效率 = 触发后采取了至少一个处理动作的告警数 / 总告警数 × 100%
```

这个指标和噪音率互补。我们的目标：> 80%。

### 数据收集方式

我们用 PagerDuty 的 API 导出数据，写了一个简单的 Python 脚本每周汇总：

```python
import requests
from datetime import datetime, timedelta

PD_API_KEY = "your_api_key"

def get_alert_stats(days=30):
    since = (datetime.now() - timedelta(days=days)).isoformat() + "Z"
    
    headers = {
        "Authorization": f"Token token={PD_API_KEY}",
        "Accept": "application/vnd.pagerduty+json;version=2"
    }
    
    resp = requests.get(
        "https://api.pagerduty.com/incidents",
        headers=headers,
        params={"since": since, "limit": 100, "statuses[]": ["resolved"]}
    )
    
    incidents = resp.json()["incidents"]
    
    total = len(incidents)
    # 通过 notes 或 custom fields 标记是否有实际处理动作
    actionable = sum(1 for i in incidents if i.get("last_status_change_by"))
    
    mtta_list = []
    for inc in incidents:
        created = datetime.fromisoformat(inc["created_at"].replace("Z", "+00:00"))
        acknowledged = inc.get("first_trigger_log_entry", {}).get("created_at")
        if acknowledged:
            ack_time = datetime.fromisoformat(acknowledged.replace("Z", "+00:00"))
            mtta_list.append((ack_time - created).total_seconds() / 60)
    
    return {
        "total": total,
        "actionable_rate": actionable / total if total > 0 else 0,
        "mtta_minutes": sum(mtta_list) / len(mtta_list) if mtta_list else 0,
        "fatigue_rate": 1 - (actionable / total) if total > 0 else 0
    }
```

## 钉钉 Webhook 集成：告警携带上下文

裸告警没有上下文，工程师还要自己去找面板和 Runbook 链接，很低效。我们的告警模板：

```yaml
# Alertmanager receivers 配置
receivers:
  - name: 'dingtalk-critical'
    webhook_configs:
      - url: 'http://dingtalk-webhook-proxy:8060/dingtalk/webhook1/send'
        send_resolved: true
        http_config:
          tls_config:
            insecure_skip_verify: true

# 钉钉 webhook proxy 的消息模板（Go template）
templates:
  - '/etc/alertmanager/templates/dingtalk.tmpl'
```

```
{{ define "dingtalk.message" }}
## {{ .Status | toUpper }} {{ .CommonLabels.alertname }}

**服务**：{{ .CommonLabels.service }}
**环境**：{{ .CommonLabels.env }}
**严重程度**：{{ .CommonLabels.severity }}

**告警详情**：
{{ range .Alerts }}
- {{ .Annotations.summary }}
  开始时间：{{ .StartsAt.Format "2006-01-02 15:04:05" }}
{{ end }}

**快速操作**：
📊 [Grafana 面板]({{ .CommonAnnotations.grafana_url }})
📖 [Runbook]({{ .CommonAnnotations.runbook_url }})
🔍 [日志]({{ .CommonAnnotations.loki_url }})
{{ end }}
```

效果是工程师收到钉钉消息后，直接点链接就能进入排查，不用再四处找面板。

## 从数据驱动改进：识别 Toil 告警

每月的告警复盘，我们会把告警按频率排序，找出 Top 10 高频告警。对每条高频告警问三个问题：

1. 这个告警最近 30 天触发了多少次，有多少次是真实问题？
2. 每次处理平均花了多少时间？
3. 能否自动修复（Auto-remediation）？

**修复 vs 静默的决策框架**：

```
高频告警
├── 告警代表真实问题？
│   ├── 是，但每次自动恢复 → 考虑加 for 窗口（等稳定再告警）
│   ├── 是，处理步骤固定 → 开发 Auto-remediation
│   └── 是，需要人工判断 → 优化 Runbook，减少处理时间
└── 告警是误报/噪音？
    ├── 阈值不合理 → 调整阈值或改用 SLO-based 告警
    ├── 监控指标本身问题 → 修复指标采集
    └── 临时现象已解决 → 直接删除
```

## 一次真实的凌晨 On-Call 记录

这是 2025 年 11 月某天凌晨 3:12 的处理记录，原文如实记录：

**03:12** PagerDuty 告警：`payment_error_rate_high P1`，错误率 8%，持续 3 分钟。

**03:13** Acknowledge。打开 Grafana 面板，确认是真实告警，所有支付接口均有 5xx 返回。在 #incident 频道发：`[P1] 支付错误率 8%，正在排查，预计 5 分钟内初步定位`。

**03:14** 按 Runbook 检查：最近部署？查 ArgoCD，上次部署是下午 17:00，6 小时前，排除。

**03:15** 检查数据库连接：`kubectl exec -n payment deploy/payment-service -- nc -z mysql-master 3306`，无响应。数据库连接问题。

**03:16** 查看数据库 Pod 状态：`kubectl get pod -n data -l app=mysql`，发现 mysql-master Pod 处于 `Pending` 状态，events 显示 `insufficient memory`。

**03:17** 临时处理：将 payment-service 切换到只读模式（降级），减少对数据库的写压力，同时呼叫 DBA on-call。

**03:19** 在频道更新：`[P1] 定位原因：MySQL master Pod 内存不足导致重启，支付服务已切换降级模式（只读），正在协同 DBA 处理`。

**03:28** DBA 介入，扩大 MySQL Pod 内存限制，Pod 重启恢复，支付服务取消降级。错误率回落到 0.1% 以下。

**03:30** Resolve 告警，关闭 incident，记录处理时间：18 分钟，根因：MySQL Pod 内存配置不足。

**03:31** 写下改进项：MySQL 内存配置需要审查，增加 MySQL Pod 内存告警，下个 Sprint 处理。

---

这次处理快速的关键是：告警有明确的 Runbook，每一步都知道该干什么，不需要边想边查。

## 常见陷阱

**陷阱 1：把所有告警都加进 on-call**

刚建告警体系的团队最容易犯这个错。先问：这个告警如果不在凌晨叫醒人，会有什么后果？如果答案是"也没什么大不了"，它就不应该进 on-call rotation。

**陷阱 2：Runbook 只写一次，从不更新**

服务架构变了，部署方式变了，Runbook 还是两年前的，执行起来全是坑。我们的规定：每次处理告警后，如果发现 Runbook 有出入，当场更新，不过夜。

**陷阱 3：噪音率高但没人推动改进**

"告警太多"这个问题每个人都知道，但没人去解决，因为这不是优先级最高的事。我们的方案：每季度 on-call 质量复盘会，噪音率是一个硬指标，超过 20% 必须制定改进计划。

**陷阱 4：轮班人数不够**

4 人以下的轮班非常容易造成 on-call 倦怠。如果服务确实重要但人手不够，和 EM 讨论：要么增加人手，要么降低 SLO，要么引入外部值守服务，不能靠少数几个人硬撑。

On-Call 是一个系统工程，好告警 + 好 Runbook + 合理轮班 + 数据改进，缺一不可。
