---
title: "On-Call 轮值管理实战：从告警疲劳到可持续值班"
date: 2025-09-24T10:00:00+08:00
draft: false
tags: ["On-Call", "SRE", "告警", "轮值"]
categories: ["SRE"]
description: "一份可以直接抄的 on-call 轮值体系设计笔记：轮值规则、补偿、告警治理、alert fatigue 衡量、交接 SOP、心理安全、和升级链设计。附两次真实的轮值改革经验。"
summary: "On-call 不是福利也不是惩罚，是一份职责。把它做成可持续的工程实践，比任何高级监控工具都重要。"
toc: true
math: false
diagram: false
keywords: ["On-Call", "轮值", "告警疲劳", "升级链"]
params:
  reading_time: true
---

## 一个真实的对话

18 个月前，一位我很敬重的工程师在离职面谈里对我说：「我离职不是因为工资，也不是因为项目。是因为连续三个月每周被深夜 4 点叫醒，我老婆说再这样下去她要带孩子回娘家。我没法继续了。」

这段话让我下定决心彻底改革 on-call 流程。当时我们有 3 个团队做 on-call：一组 7 人轮值，每周一换，平均每周 28 次告警，其中 8 次在 23:00~6:00。换算下来每人每年 on-call 7.4 周、被夜间叫醒 59 次。

18 个月后的数据：一组 8 人轮值（扩了一人），每周 6 次告警，其中 0.5 次在夜间。每人每年 on-call 6.5 周，被夜间叫醒不到 4 次。

这不是我一个人的功劳，是团队和管理层共同推动的。但有一件事确定：**on-call 本身的设计和治理比任何监控工具选型都重要**。系统可观测性再完美，on-call 值班人员崩溃了一切归零。这篇文章是这 18 个月里我们做的全部事情。

## 一、为什么 on-call 值得被当成工程问题

很多团队把 on-call 当成「天然负担」，认为它就应该痛苦。这是错的。Google 的 SRE book 里有一条硬规矩：**SRE 的 on-call 工作量不能超过总时间的 25%**，超了就要把工作往 dev 团队推。这个规矩存在的前提是：on-call 是可以量化、可以治理、可以工程化的。

几个可以被量化的维度：

1. **告警数量**（每班次）；
2. **夜间告警比例**（22:00~8:00）；
3. **告警响应时间**（ack 到开始处理）；
4. **告警解决时间**（MTTR）；
5. **告警可执行性**（告警有没有对应 runbook）；
6. **告警真阳率**（告警是真问题 vs 误报）；
7. **值班人的主观疲劳度**（问卷，1~10 分）。

这些数据能从 PagerDuty / Alertmanager 里导出。只要你愿意量化，on-call 就从「大家默默忍受」变成「可以改进的工程问题」。

## 二、轮值规则：几个基本选择

先谈时长：

### 主流选择 1：每周轮值

最常见。周一到周日连续 7 天。优点是交接少，节奏统一；缺点是周末对 on-call 值班人剥夺太大。

### 主流选择 2：每日轮值

24 小时一轮。每人一周值 1~2 天。优点是恢复时间短；缺点是交接频繁，每次交接都是风险点。

### 主流选择 3：工作日 + 周末分开

工作日一人，周末另一人。每周交接 2 次。这是我目前觉得最人性化的方案。

### 主流选择 4：白天 + 夜晚分开

Follow-the-sun 模式，需要多地办公室。适合跨国公司。

**我们的选择**：工作日 + 周末分开。周末值班给 1.5 倍补偿。实测团队满意度最高。

### 关于补偿

On-call 必须有显性补偿，无论是钱还是调休。我们的模式：

- 工作日 on-call：每班次 1 天调休；
- 周末 on-call：每班次 2 天调休；
- 被叫醒（夜间 22:00~8:00 被真实告警打扰）：额外 0.5 天调休；
- 处理 SEV-1/2 事故超过 2 小时：计入加班时数，发加班费。

调休的关键：**允许在下一个 sprint 灵活消化**，不能说「当天补休」，否则 on-call 后的白天你不休也得干活。

## 三、团队规模和轮值频率

一个团队做 on-call 的最小有效规模是 5 人。太少每个人轮得太勤；太多每个人又会「脱手」。我的经验：

| 团队规模 | 每人年 on-call 周数 | 体感 |
|---|---|---|
| 4 人 | 13 | 疲惫，交接不到位 |
| 5~6 人 | 8~10 | 有压力但可承受 |
| 7~8 人 | 6~7 | 较健康 |
| 9+ 人 | < 6 | 脱手严重，不熟悉系统 |

**最佳区间是 6~8 人**。4 人以下的团队如果实在要 on-call，建议和隔壁团队合并。9 人以上的团队建议拆成两个 rotation。

## 四、告警治理：真正决定 on-call 质量的那件事

轮值规则只能让「负担分摊」变得公平，但决定 on-call 是否可持续的是告警本身。我们内部有一条核心口号：**每一个 on-call 告警都必须是「有人需要立刻做一个动作」**。不满足这个标准的就不配叫告警，只能叫 metric 或 ticket。

这条原则推导出一系列实操规则。

### 规则 1：三层分流

所有告警分三层：

- **Page（真告警）**：打电话/钉钉/PagerDuty，on-call 必须立刻响应。
- **Ticket（工单）**：进 JIRA 或 Issues，下个工作日处理。
- **Log（日志）**：发到团队频道，仅作记录，不强制响应。

默认所有告警是 ticket，只有满足「立即响应有意义」才升级为 page。

### 规则 2：告警必须有 runbook

Page 级告警必须有一份 runbook 指向以下几件事：

1. 这个告警意味着什么？
2. 如何判断是真问题？
3. 第一响应动作是什么？
4. 如何回滚 / mitigation？
5. 如果处理不了升级给谁？

没 runbook 的告警不能是 page。我们强制：新增 page 级告警必须在 PR 里附 runbook 链接，否则 review 拒绝。

### 规则 3：告警必须是症状不是原因

症状（good）：「Payment API 错误率 > 5%」
原因（bad）：「Redis master pod 不可用」

原因级告警的问题是：系统总会有组件故障，但未必影响用户。告警应该只在用户真的受影响时触发。Redis master pod 不可用但 replica 接管了、业务没受影响，就不该 page。

这条规则的本质是 **alert on SLO，not on capacity**。Google SRE book 里讲得很清楚。

### 规则 4：Alert on burn rate

SLO 告警用 multi-window multi-burn-rate：

```yaml
- alert: PaymentAPIBurnRateFast
  expr: |
    (
      (1 - (sum(rate(http_requests{status!~"5.."}[5m])) / sum(rate(http_requests[5m]))))
      > (14.4 * 0.001)
    )
    and
    (
      (1 - (sum(rate(http_requests{status!~"5.."}[1h])) / sum(rate(http_requests[1h]))))
      > (14.4 * 0.001)
    )
  for: 2m
  labels:
    severity: page
  annotations:
    summary: 支付 API 在 1h 内将消耗 2% 错误预算（SLO 99.9%）
    runbook_url: https://wiki.example.com/runbook/payment-burn-rate
```

这种告警有两个好处：

1. **避免了 for: 30m 的麻烦**：fast burn 会在 2 分钟内触发，快慢两个窗口同时确认才 page，减少误报；
2. **告警强度和真实影响挂钩**：burn rate 高意味着真的在消耗错误预算，不是某个 pod 抖动一下。

### 规则 5：定期做告警审计

每月的 sprint planning 里留 2 小时做告警 review：

- 按告警名 group，看每类告警出现次数；
- Top 5 最频繁的告警，问：这是有效告警吗？如果不是，怎么改？
- 上个月新增的告警，review 是否合理；
- 上个月 MTTA > 15 分钟的告警，问为什么响应慢。

这个会每月 2 小时，能砍掉大量无效告警。我们第一个月砍了 60% 的 page 级告警。

## 五、如何衡量 alert fatigue

光感觉「最近告警好像变多了」不够，要有数据。几个指标：

```promql
# 每班次告警数（按值班人聚合）
sum by(oncall_user) (increase(alertmanager_notifications_total{receiver="pager"}[7d]))

# 夜间告警占比
sum(increase(alertmanager_notifications_total{hour=~"22|23|00|01|02|03|04|05"}[30d]))
/
sum(increase(alertmanager_notifications_total[30d]))

# MTTA
avg(pd_incident_ack_time_seconds)

# 告警真阳率（需要手动标记或从 postmortem 推导）
count_over_time(incident_true_positive[30d])
/
count_over_time(incident_total[30d])
```

### Toil Survey

每季度做一次 on-call 问卷，10 分钟填完：

1. 过去一个月 on-call 中，你被夜间叫醒多少次？
2. 1~10 分，你的 on-call 疲劳度是几分？（1 = 轻松，10 = 濒临崩溃）
3. 最让你疲劳的三个告警是什么？
4. 有没有告警你根本不知道怎么处理？
5. runbook 有没有漏洞？
6. 你觉得当前轮值规则公平吗？
7. 有没有什么事你一直想做但 on-call 吃掉了所有精力？

把这些数据加总起来报给领导层。它比任何 PPT 都能说明问题。

## 六、升级链：No hero，No orphan

升级链（escalation policy）是防止「告警没人接」和「on-call 一个人扛不住」的关键机制。

### 典型升级链

```
Level 1 (0~5 min): primary on-call
Level 2 (5~10 min): secondary on-call
Level 3 (10~15 min): team lead
Level 4 (15+ min): IMOC / manager on duty
```

PagerDuty / Alertmanager / FireHydrant 都支持这种分级。关键规则：

- **primary 接到告警 5 分钟不 ack，自动升级**；
- **secondary 存在的意义是「primary 失联时顶上」**，不是「primary 一起接」；
- **升到 team lead 时不是惩罚 primary**，是流程本身的一部分；
- **升级不等于替代**，primary 依然负责事故，lead 只是支援。

### Secondary 的设计

Secondary 也要被调度，但负担比 primary 小：

- secondary 不会被每个告警打扰，只在 primary 未 ack 时；
- secondary 可以比 primary 更低资历（比如 L3 工程师配 L5 secondary）；
- secondary 周期和 primary 错开一周，避免两人同时疲劳。

### 不要让一个人扛

最差的做法是「只有一个人被 page」。任何 SEV-2 以上，primary 应该主动拉 secondary 上线，不要死撑。我们明确告诉每个 on-call：**你觉得扛不住就叫人，这不是软弱，是流程**。

## 七、交接 SOP

on-call 交接是经常被忽视的环节。一个糟糕的交接会让新 on-call 的第一小时变成猜谜。我们的交接 SOP：

### 交接清单

```
## On-Call Handoff: <from> -> <to>
### 日期
2025-09-22

### 待处理事项
- [ ] incident-2025-09-19-payment-api 还未写 postmortem
- [ ] 上周 OrderAPI_p99 告警调整的 Grafana dashboard

### 未解的根因
- Redis client 偶尔报 ETIMEDOUT（已压制，未定位）
- node-exporter 指标抖动，暂时放弃告警

### 环境变更
- 2025-09-20 Istio 从 1.21 升到 1.22
- 周四下午有一个新业务上线，sentinel 的配额已经预留

### 风险提醒
- MySQL 主库 SSL 证书 2025-09-25 过期，有 ticket #1234 跟进
- cert-manager 最近有异常重启

### Runbook 最近更新
- payment-api 的 rollback 步骤更新，新增 cache flush
```

交接会议 15 分钟，在 on-call 轮换日的上午做，值班人员一对一过这个文档。所有内容归档到团队 wiki。

## 八、心理安全：最常被忽略的一环

on-call 的问题往往不是技术，是心理。值班人焦虑、怕出错、怕被追责，越紧张越容易误操作。建立心理安全需要：

1. **显性 blameless 文化**（前文讲过）；
2. **公开承认 on-call 辛苦**。管理层在会议上说「我知道最近告警多，辛苦你们」比任何补偿都有效；
3. **允许说「我不会」**。on-call 不是全能工程师，遇到不会的事情应该升级；
4. **事故后主动关怀**。经历大事故后值班人要有一天 recovery time，可以什么都不做；
5. **on-call 的表现不和绩效挂钩**。处理得好不加分，处理不好不扣分。只看「按流程响应」。

第 5 条争议最大。有人说「处理得好为什么不奖励」。我的观点：on-call 是 team sport，个人英雄主义反而会让团队依赖个别人，长期看是负债。

## 九、工具栈选择

### Alertmanager + PagerDuty

- Alertmanager 做 routing、dedup、silence；
- PagerDuty 做 rotation、escalation、incident；
- 两边数据通过 receiver 打通。

### 自研轮值表

如果预算有限：

- 用 Google Calendar / 钉钉值班机器人维护；
- Alertmanager routing 里用 webhook 推到对应值班人；
- 交接在文档里手动管。

### Grafana OnCall / OpsGenie / incident.io

几个流行的替代品。我们评估过 Grafana OnCall（开源 + 商业），功能足够，和 Grafana 生态集成好。如果你已经用 Grafana 全家桶，它是 PagerDuty 的廉价替代。

### 自研 on-call bot

很多公司最后都写一个 bot 处理告警 routing + 值班查询 + 交接 reminder。我们的 bot 大概有 500 行 Python，承担：

- 从值班表推出当前 on-call；
- Alertmanager webhook 接收告警，推到对应值班人；
- 告警未 ack 5 分钟自动 @secondary；
- 每周一早上发交接 reminder；
- 每月输出告警统计报告。

## 十、案例：一次彻底的告警清理

时间：2024 年 Q3。当时现状：SRE 团队 on-call 每周平均 42 次告警，27% 夜间，真阳率 35%。值班人员持续抱怨。

### 第一周：数据采集

从 Alertmanager 导出 30 天告警，按 alertname group，统计：

- 总次数
- 夜间次数
- 平均 MTTA
- 有没有 runbook
- 是否触发过真实 action

第一周只做这一件事。结论：前 10 个告警占了总量的 72%。

### 第二周：top 10 逐一处理

对前 10 个告警，逐一做 review：

1. **DiskUsageHigh**（126 次/月）：改成 ticket 级，因为磁盘扩容不需要立刻做。
2. **PodRestartFrequent**（98 次）：条件改严，只有 1 分钟内 > 5 次才 page。
3. **HTTP_5xx_high**（74 次）：改成 SLO burn rate 告警，砍掉 60% 误报。
4. **CertExpirySoon**（56 次）：改成 90 天提醒一次，不再每天 page。
5. **NodeCPUHigh**（49 次）：砍掉，改成 SLO-driven 的业务层告警。
6. **KubernetesPodCrashLooping**（42 次）：只在 crashloop 超过 30 分钟才 page。
7. **MySQLReplicationLag**（38 次）：lag > 30s 才 page，而非 5s。
8. **RedisMemoryHigh**（31 次）：改为 ticket，自动扩容脚本兜底。
9. **IstioProxyNotReady**（28 次）：改成 log 级。
10. **NginxIngressHigh5xx**（26 次）：用 burn rate 告警替代。

### 第三周：部署与观察

把上面改动发布到生产，观察一周。

### 第四周：成果

告警总量从 42 次/周降到 8 次/周，夜间告警从 11 次降到 2 次。真阳率从 35% 涨到 75%（因为假的全被过滤了）。

**最核心的收获**：工作量下降之后，真正重要的告警反而被好好处理。以前淹没在噪音里的几个关键告警被重视起来。

## 十一、案例：轮值改革前后对比

把本文开头的数据整理成一张表：

| 维度 | 改革前 | 改革后 |
|---|---|---|
| 团队规模 | 7 人 | 8 人 |
| 每周告警 | 28 次 | 6 次 |
| 夜间告警比例 | 29% | 8% |
| 每人年 on-call 周数 | 7.4 | 6.5 |
| 每人年被叫醒次数 | 59 | 4 |
| 告警真阳率 | 38% | 78% |
| on-call 疲劳度（问卷） | 7.2/10 | 3.8/10 |
| 季度主动离职 | 2 | 0 |

改革具体做了什么：

1. 加 1 人到 rotation；
2. 告警清理（上面那个 case）；
3. 工作日 / 周末拆开；
4. 交接 SOP 固化；
5. 夜间告警 escalation 补偿；
6. 每季度 toil survey；
7. team lead 每月 1:1 关注 on-call 状态。

没有一项是「神奇技术」，都是工程化的细节。

## 十二、反模式清单

我见过的 on-call 反模式：

1. **一个人长期独揽**：表面上「他最懂系统」，实际是组织脆弱性。
2. **告警越多越好**：以为告警多代表覆盖全，其实是噪音压死了信号。
3. **primary + secondary 都必须响应**：浪费第二个人的时间。
4. **on-call 期间不能上班其他事**：太奢侈。小告警可以穿插处理。
5. **on-call 完全不补偿**：涸泽而渔。
6. **告警 group 设得过大**：合并 5 个独立告警成一条，丢失信号。
7. **runbook 过时没人更新**：改代码不改 runbook，下次值班人惨。
8. **升级链越长越好**：3 层足够，再多让 primary 有依赖心理。
9. **值班交接走流程不走心**：15 分钟会议改成 5 分钟流程念清单。
10. **把 on-call 当新人培训机会**：新人扛不住会崩溃。应该让新人先 shadow，再独立。

## 十三、On-Call 新人培养

新同事加入 on-call rotation 的路径：

1. **Shadow 2 周**：作为 observer 参与所有告警响应，不做操作，只观察；
2. **Shadowed primary 2 周**：作为 primary 响应告警，但有资深工程师 shadow，随时接管；
3. **独立 primary + 在线 backup 2 周**：独立处理，但 backup 随叫随到；
4. **完全独立**：加入轮值表。

总共 6 周。时间长看起来慢，实际能极大降低新人焦虑和误操作风险。

## 十四、给管理层的话

如果你是 engineering manager 或 director，记住一件事：**on-call 不是团队的副产品，是核心交付能力**。你对 on-call 的投入应该和对线上质量的投入成正比。具体：

1. 把 on-call 质量指标（告警数、夜间比例、疲劳度）放进你的 monthly report；
2. 给团队一个明确的「on-call 优化」时间预算，每季度至少 5% 的工时；
3. 当 on-call 疲劳度高于 6 分时，把新 feature 优先级往后推；
4. 一个工程师离职时主动问「是不是 on-call 的原因」；
5. 给 on-call 值班人显性致谢，无论是所有人会议还是团队频道。

这些不是「软技能」，是让工程团队可持续的硬工程。

## 十五、结语

on-call 是 SRE 文化里最体现「工程 vs 英雄主义」的分野。好的 on-call 体系让普通工程师都能从容应对生产事故；坏的 on-call 体系让最强的工程师都撑不过一年。你的团队属于哪一类，很大程度上取决于你有没有把 on-call 当成工程问题来治理。

我开头讲的那位离职同事后来去了另一家公司，两年了还在做工程。有一次聊天他说：「我其实很喜欢那份工作，只是那时候我真的撑不住了」。我听完心里有点难过。希望读这篇文章的你，能把下一个「他」留下来。

## 参考资料

- Google SRE Book 第 11 章 Being On-Call
- Google SRE Workbook 第 8 章 On-Call
- PagerDuty Incident Response 文档
- Limoncelli《The Practice of System and Network Administration》
