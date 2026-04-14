---
title: "故障响应与 Blameless 复盘：让每一次事故都变成组织资产"
date: 2025-09-10T10:00:00+08:00
draft: false
tags: ["故障响应", "Postmortem", "SRE", "On-Call"]
categories: ["SRE"]
description: "写给正在从 PagerDuty 随性响应升级到规范化事故管理的团队。包含 incident severity 分级、IMOC/IC 角色、沟通模板、blameless postmortem 模板、action item 跟踪、组织层面的心理安全建设。"
summary: "事故响应不是英雄主义，是一套可重复的流程。把流程、模板、文化讲清楚，让每次事故都能沉淀成组织资产。"
toc: true
math: false
diagram: false
keywords: ["事故响应", "Postmortem", "Blameless", "SRE", "Incident Commander"]
params:
  reading_time: true
---

## 先定义问题

几乎所有规模的工程团队都做故障响应，但 90% 的团队做得不系统。你见过的常见现象：

- 告警响了，值班的人进群骂两句，拉一个微信群拉十几个人；
- 群里 30 个人在聊「有没有人能看一下」；
- 业务问「现在恢复了吗」没人答；
- 事故结束后谁都不想写复盘，最后领导催着写了一份流水账；
- 上次的 action items 没人跟进，同样的故障三个月后又来一次。

这些不是人的问题，是流程缺失的问题。SRE 书里讲的 Incident Response（IR）和 Blameless Postmortem 已经是十年前的最佳实践了，但真正落地到流程、文档、工具、文化层面的团队依然不多。这篇文章记录我们从「群聊救火」演进到「规范化事故响应」的过程，以及里面每个环节踩的坑。

目标读者：正在从 on-call 混战升级到结构化响应的团队。

## 一、先谈文化：Blameless 是前提

如果组织文化里还有「谁的锅」这个问题，那后面的流程都是花架子。工程师不敢说真话，复盘就是表面文章。所以 blameless 是前置条件。

Blameless 不是「不追责」，而是把「人为什么会犯错」当成一个系统问题来分析。同样是「改错了生产配置」，blameless 问的是：

- 为什么这个配置是人手改的而不是 GitOps 管的？
- 为什么没有 peer review？
- 为什么没有 dry-run 机制？
- 为什么 prod 和 dev 的操作入口是同一个？

结论会指向系统改进，而不是「以后小心点」。

我们在组织里推 blameless 时用过两个有效做法：

1. **事故响应群禁止追究「谁做的」**。响应阶段聚焦止损，复盘阶段才分析原因。
2. **复盘文档里删掉所有 who 字段**。只写 what 和 why，when 用时间戳代替。

第二点在我们团队效果意外的好。不是绝对不写人名，而是默认不写。如果必须写，比如「张三执行了 rollback」，用的是角色而不是人名：「IC（张三）执行了 rollback」。

## 二、事故分级：SEV-1 到 SEV-4

没有分级就没有响应节奏。我们借鉴 Google SRE 和 PagerDuty 的分级，改成了 4 级：

### SEV-1（Critical）

- 全站宕机或核心业务完全不可用；
- 数据丢失或数据不一致；
- 合规 / 安全事件；
- 影响范围 > 50% 用户。

响应：立即拉群，IC 上线，IMOC 通知高管，业务侧同步客服。目标 15 分钟内 mitigation。

### SEV-2（Major）

- 单个核心功能不可用；
- 部分用户受影响（10%~50%）；
- 关键业务指标劣化 50%+；
- 有明确升级风险，可能变 SEV-1。

响应：拉群，IC 上线，通知相关业务。目标 30 分钟内 mitigation。

### SEV-3（Minor）

- 非核心功能降级；
- 小部分用户受影响（<10%）；
- 性能劣化但未达致命。

响应：值班同学处理，可以不拉群。目标 2 小时内 mitigation。

### SEV-4（Low）

- 内部工具异常；
- 无用户影响的告警；
- 可延迟处理的异常。

响应：记录 ticket，排期处理。

分级有两个关键实操原则：

1. **宁可高估不要低估**。升级容易降级难。发现问题的第一时间按最严重预估，确认没问题再降。
2. **分级决定流程，不决定责任**。SEV-1 不等于谁写错了，只是说我们需要加倍资源响应。

## 三、角色定义：IC / IMOC / CL / Ops

结构化响应的核心是每次事故都有**明确的指挥结构**。我们用的是简化版 ICS（Incident Command System）：

### Incident Commander（IC）

事故响应的唯一指挥。职责：

- 组织响应节奏；
- 做重大决策（rollback / 切流 / 封锁发布）；
- 分配任务；
- 主持定期通报；
- **不写代码**，不直接操作，只指挥。

IC 最重要的技能是「冷静」和「信息整合」。技术深度其次。一个合格的 IC 能在 10 分钟内把群里 30 条消息凝练成 5 行状态摘要。

### Ops Lead（或 Tech Lead）

真正动手的人。负责：

- 执行 IC 分配的技术任务；
- 汇报技术状态；
- 复现和定位问题；
- 回滚 / 重启 / 切流 等操作。

### Communications Lead（CL）

对外沟通。负责：

- 每 15~30 分钟向业务、客服、管理层发状态更新；
- 统一对外口径，避免一百个人问一百遍；
- 撰写事故沟通邮件 / 内部公告。

### Incident Manager On-Call（IMOC）

跨事故的管理者。职责：

- 监督 IC 是否履职；
- 在 IC 疲惫时接手；
- 跨事故协调资源；
- 决定事故升级和降级。

我们的做法：所有 SEV-1 / SEV-2 上面四个角色都要显式任命，SEV-3 可以合并。任命方式是在事故频道发一条消息：

```
【角色任命】
IC: @张三
Ops Lead: @李四
CL: @王五
IMOC: @赵六
```

写在频道置顶，后来加入的人一看就知道谁在指挥。

## 四、响应时序：从告警到 resolve

一次规范化的事故响应大致是这样：

### T+0：告警触发

- PagerDuty / 钉钉告警响起；
- 值班人员 acknowledge，意味着「我看到了」。ack 不等于问题已处理。

### T+2min：初步判断

- 打开 dashboard 看影响面；
- 决定 severity；
- 如果 SEV-3 以下自己处理，SEV-2 以上拉事故群。

### T+5min：拉事故群，任命角色

- 群命名规范：`#incident-YYYYMMDD-HHmm-<短描述>`；
- 任命 IC / Ops Lead / CL；
- IC 贴第一条状态摘要：

```
【事故摘要 v1】
症状：order-api p99 从 150ms 涨到 8s
影响：下单流程 30% 失败
时间：15:02 开始
初步判断：疑似 MySQL 慢 SQL
当前动作：Ops Lead 正在查 MySQL 进程
下次更新：15:17
```

### T+10min：根因调查

- Ops Lead 调查；
- 其他参与者按 IC 分配辅助查监控、日志、trace；
- 不要在群里发「什么情况」这种无效信息。

### T+15min：第一次 mitigation 尝试

- 如果有明确的回滚路径，优先回滚而非修复；
- 目标是**止损**，不是找到根因；
- 任何操作前 IC 确认：「执行 X 操作，预期 Y，风险 Z，2 分钟后我们看结果」。

### T+30min：状态复盘

- 是否有效？
- 需要升级吗？
- 需要更换 IC 吗？
- 通知范围要扩大吗？

### T+mitigation：确认恢复

- 业务指标回落到基线；
- IC 宣布 mitigation 成功；
- 但事故**不关闭**，进入 monitoring 阶段。

### T+30min 稳定后：事故关闭

- 业务指标稳定 30 分钟以上；
- IC 宣布事故 resolve；
- 排定复盘会议时间（通常 48 小时内）。

### T+48h：复盘

- 写 postmortem 文档；
- 所有相关人员参会；
- 产出 action items 和 owner。

## 五、事故沟通模板

人在事故中最容易做不好的是沟通。把模板固化下来，压力下也能输出。

### 事故群置顶

```
状态：ongoing / mitigated / resolved
Severity: SEV-2
IC: @张三
CL: @王五
开始时间: 15:02
影响: 下单成功率降低
最新摘要: [链接到下面的摘要]
```

### 状态摘要（每 15 分钟）

```
【事故摘要 v<N>】<时间>
现状: <一句话>
新发现: <bullet>
已执行: <bullet>
下一步: <bullet>
ETA: <估计恢复时间，未知写 "unknown">
```

### 对外状态页（status page）

```
[查清中] 我们正在调查订单提交失败的报告
- 15:05  问题被发现
- 15:15  我们已定位到一个下游服务异常
- 15:30  初步修复已部署，正在验证
- 15:45  服务已恢复，正在监控
```

用户看不到技术细节，只看到「知道了 → 在查 → 修了 → 恢复了」这四个状态。不要写「MySQL 慢 SQL 导致 etcd 写放大」这种话。

### 内部通报邮件（SEV-1/2 需要）

```
主题: [Incident Report] SEV-2 Order API Degradation (2025-09-10 15:02 - 15:58)

摘要: order-api 在 15:02 到 15:58 期间 p99 显著升高，下单成功率降至 67%。初步原因为 MySQL 主库慢 SQL 堆积。已通过 kill 慢查询 + rollback 上一次发布恢复。

受影响: 所有 web / mobile 下单用户约 30%
持续: 56 分钟
根因: 初步判断
恢复方式: rollback + kill query
下一步: 48h 内产出 postmortem 和 action items
```

SEV-2 以上的邮件一定要有。即使收件人不看，发的这个动作本身就强制你把事情想清楚。

## 六、工具栈：不要把流程绑死在工具上

我们用过的组合：

- **告警**：Alertmanager / PagerDuty；
- **事故频道**：Slack / 钉钉专属频道；
- **事故管理**：FireHydrant / incident.io / Rootly / 自研 bot；
- **状态页**：Statuspage / Cachet / 自研；
- **复盘文档**：Confluence / Notion / Markdown 仓库。

关键经验：不要一开始就上专业工具。先用 Confluence + 钉钉频道跑通流程，跑半年再决定是否买专业工具。专业工具主要省 20% 的操作，但 80% 的价值在你的流程和模板。

## 七、Blameless Postmortem 模板

我们用的模板，可以直接抄：

```
# Postmortem: <标题>

## 摘要
<三句话讲清楚发生了什么，影响多大，怎么恢复的>

## 事故元数据
- 发现时间: 2025-09-10 15:02
- 恢复时间: 2025-09-10 15:58
- 持续时间: 56 分钟
- Severity: SEV-2
- IC: <角色而非姓名>
- 影响: 下单成功率降低至 67%，影响约 12 万笔订单
- 检测方式: Prometheus 告警 OrderAPI_p99_high

## 时间轴（UTC+8）
- 14:50 订单 API v2.4.1 发布到 prod（灰度 50%）
- 14:58 监控显示 order-api pod 的 DB 查询耗时开始上升
- 15:02 Alertmanager 告警 order-api p99 >1s，值班 ack
- 15:05 值班判断 SEV-2 拉事故群，任命 IC
- 15:08 Ops Lead 查 DB slow query log，发现新 SQL 未走索引
- 15:15 IC 决策: rollback deployment
- 15:22 rollback 完成
- 15:28 p99 回落到 200ms
- 15:45 业务指标全面恢复
- 15:58 IC 宣布 resolve

## 影响
- 用户影响: 约 5 万用户下单失败或重试
- 业务影响: 订单量 1h 内减少 30%
- 财务影响: 初估损失 X 万
- SLA 影响: Order API 月度 SLA 消耗 0.04 个错误预算

## 根因分析（RCA）
1. 直接原因: v2.4.1 引入一条新 SQL 使用 order_status 字段过滤，但 order 表未在 order_status 上建索引
2. 触发条件: 灰度 50% 之后写入压力让 SQL 每秒执行 1000+ 次，缓存失效后成为热查询
3. 传播原因: 数据库慢查询导致连接池耗尽，无慢查询的请求也被阻塞
4. 检测滞后: 告警规则基于 p99，从异常到告警滞后约 4 分钟

## 贡献因素（Contributing Factors）
1. 缺少 SQL 审查流程: PR 里新增 SQL 没有 DBA 审查
2. 缺少 staging 真实流量压测: staging 写入 QPS 只有 prod 的 1%
3. 告警滞后: p99 采样窗口 2m，告警 for 2m，理论下限延迟 4m
4. 回滚流程文档不够清晰: IC 花了 3 分钟确认 rollback 命令

## 做得好的地方（What went well）
- IC / Ops Lead 角色任命清晰
- 15 分钟内决策 rollback
- 没有盲目查代码，先止损

## 做得不好的地方（What went wrong）
- PR 未拦截这个索引问题
- 灰度策略没有在小流量时观察足够长
- 业务侧通知滞后 10 分钟

## 运气成分（Where we got lucky）
- 事故发生在非高峰（周三下午），高峰时可能严重 10 倍
- 上一次发布的 image 还在 registry，rollback 快

## Action Items
| # | 描述 | 类型 | 优先级 | Owner | Deadline |
|---|---|---|---|---|---|
| 1 | 为 order.order_status 建索引 | Fix | P0 | DB Team | 2025-09-11 |
| 2 | PR 流程加 SQL lint 检查 | Prevent | P1 | Platform | 2025-09-20 |
| 3 | staging 复制 prod 1/10 真实流量 | Detect | P2 | SRE | 2025-10-15 |
| 4 | p99 告警窗口调到 1m | Mitigate | P1 | SRE | 2025-09-15 |
| 5 | 更新 rollback runbook，加一键脚本 | Process | P1 | SRE | 2025-09-18 |
| 6 | 编写业务侧通知 playbook | Process | P2 | CL | 2025-09-25 |

## 学到的东西（Lessons Learned）
1. SQL 索引问题在小流量灰度时很难暴露，需要真实写压力
2. 事故响应最耗时的部分不是诊断，是「确认下一步动作是否安全」
3. 回滚路径上的任何摩擦点都会放大事故时长
```

几点说明：

1. **不写人名**，用角色（IC / Ops Lead）。
2. **Action items 分类**：Fix（修根因）、Prevent（防再发）、Detect（早发现）、Mitigate（减影响）、Process（流程）。每类至少有一条才完整。
3. **Action items 必须有 deadline 和 owner**，没有这两个字段的条目不算。
4. **「运气成分」一栏**非常重要，它提醒你「这次没死纯属运气」，推动更严谨的改进。
5. **「学到的东西」** 是给组织读的，比 action items 更长远。

## 八、Action Items 的跟踪机制

90% 的 postmortem 死在 action items 上：写得漂亮，但没人跟进。几个月后同样的故障又来一次。

我们的跟踪机制：

1. **所有 action items 进 Jira 的 incident backlog**；
2. **IMOC 每周 review 一次未关闭的 items**；
3. **超期 items 报给 team lead**；
4. **月度事故回顾会**回看所有 open items；
5. **action items 未关闭的团队**不允许做新的演进项目（这条是软约束，但有效）。

最有效的一点：把 P0/P1 action items 的 deadline 进 OKR。上线这条之后，action item 完成率从 40% 涨到 85%。

## 九、组织层面：事故知识沉淀

单个 postmortem 的价值一次性，多个 postmortem 合起来才有组织价值。做法：

### 1. 事故数据库

所有 postmortem 放同一个仓库，打标签：

```
- scope: database / network / deployment / application / external
- root_cause: config-error / code-bug / capacity / dependency / human-error
- severity: SEV-1 / SEV-2 / SEV-3
```

季度回顾时可以做数据分析：这季度多少次 SEV-2？根因分布？哪个服务最频繁？

### 2. 事故模式识别

连续三次 postmortem 涉及 DNS 解析失败？说明你有 DNS 架构问题，不是偶发。把这类模式抽出来做专题整改，比单次修复有价值 10 倍。

### 3. 季度事故回顾会

全团队级别的 review，不是单次复盘，是「这 3 个月我们在事故里学到了什么」。一般 1 小时，内容：

- 数据总结：事故次数、平均 MTTR、按类别分布；
- Top 3 教训；
- 关键 action items 完成情况；
- 下季度优先事项。

我们做了 4 次季度回顾后发现，持续跟踪改善的 MTTR 从 52 分钟降到 28 分钟。

### 4. 入职培训必读材料

把经典事故 postmortem 列成新人必读。新同事第一天先读 10 个真实故事，比任何 onboarding 文档都有效。读完之后对系统的脆弱性、对 blameless 文化的理解、对「保持冷静」的重要性都会有直观认识。

## 十、案例：一个好的事故响应长什么样

时间：2025 年 11 月某个周二下午。

14:31 告警：`PaymentAPI_error_rate > 5%`
14:31 值班 A ack，打开 dashboard 看到错误率 12%
14:32 A 判断 SEV-2，拉事故频道，@IC
14:33 IC B 上线，任命 Ops Lead C，CL D
14:33 B 发第一条摘要：「payment-api 错误率 12%，疑似下游 signal-api 异常」
14:34 C 查 trace，发现 signal-api 返回 500
14:35 C 查 signal-api 日志，发现 redis connection refused
14:36 C 查 redis 状态，发现一个 redis master pod 被 evict
14:37 B 决策：先手动切换 redis 到 replica
14:38 C 执行 sentinel failover
14:39 指标恢复
14:40 B 宣布 mitigation 成功，进入 monitoring
15:10 B 宣布 resolve，安排 48h 内复盘

9 分钟从告警到恢复。事后 postmortem 发现根因：node 的 kubelet `eviction hard` 设得过激，redis pod 占内存稍高就被 evict。action items：调 eviction 阈值、redis 加 priorityClass、加 PDB。

这种「教科书式」响应不是一天练成的，是团队跑过 20+ 次事故、打磨过多次流程之后形成的肌肉记忆。

## 十一、常见反模式

最后列出几个常见的反模式，避开：

1. **英雄主义**：一个人闷头修，不汇报，不同步，修好了才说。后果：团队没法学习，这个人下次休假必出事。
2. **群聊大乱炖**：事故群里 50 个人发各种猜测。IC 失控。解决：严格角色分工，无关的人踢出。
3. **先查根因后止损**：花 1 小时查根因，业务已经挂了 1 小时。永远优先止损。
4. **复盘延期**：事故过后三周才写复盘，细节都忘了。硬规矩：48 小时内写，72 小时内开会。
5. **追责大会**：复盘开成批斗。下次没人敢说真话。
6. **Action items 永远不完成**：既伤士气又留隐患。
7. **沟通空白**：业务问「什么时候好」没人答。CL 必须存在。
8. **不分级**：所有告警都当 SEV-1 响应，团队疲劳；或都当 SEV-3，遗漏大事故。

## 十二、给不同规模团队的建议

- **< 10 人团队**：别搞 IC/CL 这套，一个值班人就够了。模板可以用，流程简化。但 postmortem 一定要写，即使很短。
- **10~50 人**：引入 IC 角色，SEV-1/2 规范化。CL 角色可选。
- **50~200 人**：四角色齐全，做事故工具。
- **> 200 人**：必须有专职 IMOC 轮值，事故平台工具化。

## 十三、最后的话

事故响应是 SRE 最核心的日常能力。它的价值不是「恢复服务」——任何稍微有经验的工程师都能做到；真正的价值是**让每次事故都成为组织改进的杠杆**。做得好，一次事故能推动 10 个改进，把系统推到下一个可靠性等级；做得差，同样的故障循环发生。

这篇文章里的模板、流程、文化都是我们一年多踩坑换来的。读完之后，我希望你能做两件事：

1. **下周拉一个会**，和团队对齐事故分级、角色定义、响应流程，写成文档发到所有工程师；
2. **找一次最近的事故**，按这份 postmortem 模板补写一遍，看看哪里还有缺口。

做完这两件事之后，你的团队就从 ad-hoc 响应进入了结构化响应。剩下的就是时间和肌肉记忆。祝你一切顺利，以及——少出点事故。

## 参考资料

- Google SRE Book 第 14 章 Managing Incidents
- Google SRE Workbook 第 9 章 Incident Response
- PagerDuty Incident Response Documentation
- Blameless Postmortem (Etsy Blog 原始文章)
- incident.io / FireHydrant 产品文档
