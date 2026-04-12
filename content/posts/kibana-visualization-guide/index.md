---
title: "Kibana 实战：从日志查询到 Dashboard 可视化的完整指南"
date: 2026-04-11T08:30:00+08:00
draft: false
tags: ["Kibana", "ELK", "可视化", "日志", "KQL"]
categories: ["ELK Stack"]
description: "系统梳理 Kibana 日常使用技巧：KQL 语法、Lens 可视化、Dashboard 设计、告警规则配置，以及时区、字段类型等高频踩坑。"
summary: "Kibana 是我们 ELK 体系里使用频率最高的工具。这篇文章把我在实际运维中积累的 Kibana 使用技巧整理成体系，从 Discover 查询到 Dashboard 制作，再到 ILM 管理。"
toc: true
math: false
diagram: false
series: ["ELK Stack 完全手册"]
keywords: ["Kibana", "KQL", "Dashboard", "Lens", "ELK", "日志可视化"]
params:
  reading_time: true
---

## 前言

用 Kibana 用了几年，说实话学习曲线不低。界面每个大版本都有不小的变化，文档又是英文的，很多功能靠摸索才知道怎么用。这篇文章把我日常用得最多的功能整理出来，希望能帮到同样在用 ELK 做日志分析的同学。

环境：Kibana 8.12，使用 Elasticsearch 数据流存储日志。

## Discover：日志查询的主战场

### 创建数据视图

Discover 的前提是要有数据视图（Data View，旧版叫 Index Pattern）。进入 Stack Management → Data Views → Create data view，填写索引匹配规则。

对于数据流，模式写 `logs-nginx-*` 可以匹配所有 nginx 相关的数据流。时间戳字段选 `@timestamp`。

一个实用技巧：如果你的索引命名规则比较混乱，可以用通配符 `*` 匹配所有索引，但注意这会让 Kibana 加载所有索引的 field mappings，首次打开 Discover 会很慢。生产环境最好按业务线创建多个精细的数据视图。

### KQL 语法精要

KQL（Kibana Query Language）是 Discover 的核心查询语言，比 Lucene 语法更直观。

**字段精确匹配**

```
status_code: 500
service.name: "payment-service"
```

注意：字符串字段用 `service.name: payment-service` 会做模糊匹配（包含即可），加引号 `"payment-service"` 才是精确匹配。

**范围查询**

```
# 响应时间大于 1000ms
response_time > 1000

# 状态码 400 到 599
status_code >= 400 and status_code < 600

# 日期范围（不如直接用右上角的时间选择器）
@timestamp > "2026-04-11T00:00:00"
```

**通配符匹配**

```
# 匹配所有 /api/ 开头的路径
request.path: /api/*

# 匹配 error 或 Error（KQL 默认大小写不敏感）
log.level: error
```

**布尔逻辑**

```
# AND 条件
service.name: "order-service" and status_code: 500

# OR 条件
status_code: 502 or status_code: 503 or status_code: 504

# NOT 条件
not status_code: 200

# 括号分组
(status_code: 400 or status_code: 404) and service.name: "api-gateway"
```

**exists 查询**

```
# 字段存在（用于排查字段缺失问题）
error.message: *

# 字段不存在
not error.message: *
```

### 常用查询模式

**查 5xx 错误**

```
status_code >= 500 and status_code < 600
```

选择最近 1 小时的时间范围，右边 Documents 数量就是错误总数。

**按服务名过滤**

```
kubernetes.labels.app: "payment-service"
```

如果你用的是 ECS（Elastic Common Schema）格式，服务字段是 `service.name`。

**查慢请求**

```
response_time > 2000 and status_code: 200
```

**排查特定用户的请求链路**

```
user.id: "u-123456" and @timestamp > "2026-04-11T09:00:00"
```

配合 Kibana 左侧字段面板，选中 `trace.id` 字段，可以看到完整的请求追踪链。

### Discover 的几个隐藏功能

**保存搜索**：常用的查询可以保存下来，下次直接从列表加载。保存时可以勾选"保存为 dashboard 组件"，之后可以把这个搜索直接嵌到 Dashboard 里。

**字段统计**：点击左侧任意字段，会展示该字段的 Top 5 值和分布。对于排查"哪个接口报错最多"非常有用，不需要专门去做聚合查询。

**CSV 导出**：右上角 Share → CSV Reports，可以导出当前过滤条件下的数据。注意数据量超过 1 万条时导出速度很慢，超过 10 万条建议用 Logstash 的 CSV output 插件。

## Lens：可视化编辑器

Lens 是 Kibana 7.x 之后推荐的可视化方式，比老的 Visualize 更直观。在 Dashboard 里点"Add panel → Create visualization"就进入 Lens 编辑器。

### 时序折线图

**场景**：展示 5xx 错误随时间的变化趋势。

1. 图表类型选 Line
2. 横轴（X axis）：`@timestamp`，选 Date histogram，间隔 Auto
3. 纵轴（Y axis）：Count of records
4. 添加过滤器：`status_code >= 500`
5. 可以再加一条线表示总请求量，做对比

**关键设置**：在 Y 轴点击"Advanced"，勾选"Show as percentage"可以转成错误率视图。

### Top N 柱状图

**场景**：展示响应时间最慢的 Top 10 接口。

1. 图表类型选 Bar vertical
2. 横轴：`request.path.keyword`，选 Top values，显示数量 10
3. 纵轴：`response_time` 字段的 Median（中位数比平均值更能反映真实情况，不会被极端值拉偏）
4. 降序排列，确保最慢的在最前面

注意这里用的是 `request.path.keyword` 而不是 `request.path`。**text 字段不能做聚合，必须用 .keyword 子字段**，这是 ES 里最容易踩的坑之一，后面专门说。

### 饼图

**场景**：展示 HTTP 状态码分布。

1. 图表类型选 Pie
2. Slice by：`status_code`，选 Top values，显示 8 个
3. Size by：Count of records

饼图适合展示构成比例，不适合展示变化趋势。状态码分布用饼图很合适；如果要看不同服务的请求量对比，柱状图会更清晰。

## Dashboard 设计原则

我们建了一个服务健康总览 Dashboard，日常 oncall 的时候第一眼就看这个。分享一下设计思路。

### 布局结构

```
┌──────────────────────────────────────────────────┐
│  [单值] 总请求数  [单值] 错误率  [单值] P99延迟    │
├──────────────────────────────────────────────────┤
│  [折线图] 请求量趋势（按服务分色）                  │
├──────────────────────────────────────────────────┤
│  [折线图] 错误率趋势  │  [柱状图] 慢接口 Top10    │
├──────────────────────────────────────────────────┤
│  [表格] 最近 50 条错误日志                         │
└──────────────────────────────────────────────────┘
```

顶部三个单值指标让人一眼看出整体状态，往下是趋势图看变化，最下面是原始日志方便深入排查。

### Dashboard 的几个实用技巧

**时间联动**：Dashboard 右上角的时间选择器会同步作用到所有面板，不需要每个面板单独设置时间范围。

**面板过滤**：点击图表上的某个数据点（比如点击某个服务名），Dashboard 会自动添加该值的过滤条件，所有面板联动过滤。这个功能叫 Drilldown，是 Dashboard 分析的杀手级特性。

**变量（Controls）**：在 Dashboard 顶部添加 Controls 组件，可以做下拉选择器，让用户动态切换服务名、环境等维度，不需要修改每个面板的查询条件。

**跨数据视图**：同一个 Dashboard 里的不同面板可以使用不同的数据视图，比如把 nginx 日志和 app 日志放在同一个 Dashboard 里对照分析。

## Alerting：基于 ES 查询的告警

Kibana 的 Alerting 功能（Observability → Alerts）可以基于 ES 查询设置告警规则，免费版支持基本的 ES query 告警。

### 配置 5xx 错误率告警

进入 Observability → Alerts → Manage Rules → Create rule：

1. Rule type：选 Elasticsearch query
2. Query：

```json
{
  "query": {
    "bool": {
      "filter": [
        {
          "range": {
            "@timestamp": {
              "gte": "now-5m"
            }
          }
        },
        {
          "range": {
            "status_code": {
              "gte": 500
            }
          }
        }
      ]
    }
  }
}
```

3. Threshold：当匹配文档数 > 50 时触发
4. Check every：1 minute
5. Actions：配置发送到 Slack 或 Email

**注意**：免费版的告警动作（Actions）只支持 Server log 和 Index，Slack/PagerDuty/Email 等需要 Basic 订阅及以上。如果不想花钱，建议用 Prometheus + Alertmanager 做告警，Kibana 做纯查询和可视化，参考我们组的另一篇文章。

## Index Lifecycle Management（ILM）

ILM 是 ES 索引生命周期管理，在 Kibana 界面操作比直接写 API 方便很多。

进入 Stack Management → Index Lifecycle Policies → Create policy。

我们的日志 ILM 策略：

| 阶段 | 触发条件 | 操作 |
|------|---------|------|
| Hot | 创建即进入 | 正常写入，1 副本 |
| Warm | 7 天后 | 禁止写入，force merge 到 1 segment，缩减到 0 副本 |
| Cold | 30 天后 | 迁移到冷节点（如果有的话） |
| Delete | 90 天后 | 删除索引 |

创建 policy 后，把它绑定到数据流的 index template 上。新索引创建时会自动应用这个 policy，不需要手动操作。

一个坑：**修改已存在的 ILM policy 不会立即对已进入某阶段的索引生效**，已经在 warm phase 的索引会继续按老的 policy 执行。新的 policy 只对之后新进入该阶段的索引生效。

## 踩坑集合

### 时区配置

这是我们团队新人最常踩的坑。Kibana 里显示的时间默认跟随浏览器时区，但日志里的 `@timestamp` 存的是 UTC 时间。如果你在上海（UTC+8），看到的时间是本地时间没问题，但**在告警规则和 DSL 查询里写时间范围一定要写 UTC 时间或带时区信息**。

统一的最佳实践：日志时间戳在采集时统一转为 UTC 存入 ES，Kibana 个人设置里的时区选自己所在时区，这样 Discover 里显示的是本地时间，但底层存储和查询都是 UTC，不会出现混乱。

设置路径：右上角头像 → Profile → Date Format → Time Zone。

### text vs keyword：影响聚合和精确匹配

ES 的字符串字段有两种映射类型：

- `text`：全文分词索引，适合模糊搜索，**不支持精确匹配和聚合**
- `keyword`：不分词索引，适合精确匹配、排序和聚合

默认情况下，字符串字段会同时创建 `text` 和 `keyword` 两种映射，比如 `service.name`（text）和 `service.name.keyword`（keyword）。

**在 Lens 里做 Top N 聚合，必须用 `.keyword` 字段**，用 text 字段会报错或返回错误结果。在 KQL 查询里两者都能用，但语义不同：

```
# text 字段：全文匹配，"payment" 能匹配 "payment-service"
service.name: payment

# keyword 字段：精确匹配
service.name.keyword: "payment-service"
```

很多人在 Lens 里找不到字段用于聚合，99% 的情况是因为用了 text 字段而不是 .keyword。

### Dashboard 跨 Index Pattern 数据时间不对齐

一个 Dashboard 里放了两个不同数据视图的面板，发现时间范围对不上。原因通常是两个数据视图的时间字段名不同，一个是 `@timestamp`，另一个是 `event_time` 或者 `created_at`。

解决方案：在创建数据视图时，确保时间字段都选 `@timestamp`，并在采集端统一把时间字段映射为 `@timestamp`。标准化字段命名是 ELK 使用的基础，越早统一越省事。
