---
title: "Alertmanager 完全指南：路由、抑制、静默与多渠道通知"
date: 2026-04-11T13:00:00+08:00
draft: false
tags: ["Alertmanager", "Prometheus", "告警", "SRE", "运维"]
categories: ["可观测性"]
description: "系统讲解 Alertmanager 的路由树设计、多渠道接收配置（钉钉/PagerDuty/Email）、抑制规则降噪，以及三节点高可用部署，附实际生产环境的踩坑记录。"
summary: "告警太多和告警太少一样有害。Alertmanager 的路由、抑制、分组机制是控制告警噪声的核心手段，本文从一个真实的多环境告警体系出发，讲清楚每个配置的意图和陷阱。"
toc: true
math: false
diagram: false
keywords: ["Alertmanager", "告警路由", "抑制规则", "钉钉告警", "PagerDuty", "高可用"]
params:
  reading_time: true
---

告警体系搭起来容易，让它真正好用很难。我见过两种极端：一种是告警太少，出了问题没人知道；另一种是告警太多，钉钉群里每天几百条消息，大家习惯性忽略，反而埋下了更大的隐患。

Alertmanager 的价值不只是把 Prometheus 的 alert 转发出去，而是通过路由、抑制、分组，把告警信息变成有效的、有优先级的、不重复的通知。这篇文章把我们团队在生产环境用了两年的告警配置梳理出来。

## 核心概念

在深入配置之前，先把几个核心概念说清楚：

**Route（路由树）**：Alertmanager 收到告警后，按照树形路由规则决定把告警发给谁。每条告警从根节点开始匹配，找到最深的匹配节点，发给对应的 receiver。

**Receiver（接收器）**：告警的通知目标，可以是邮件、Webhook（钉钉、Slack 等）、PagerDuty 等。每个 receiver 有唯一名称。

**Inhibition（抑制）**：当某个严重告警触发时，自动静默相关的低级别告警。例如节点宕机时，该节点上所有 Pod 的告警都可以被抑制。

**Silence（静默）**：临时屏蔽特定告警，常用于维护窗口期。可以通过 UI 或 API 创建。

**Group（分组）**：把相似的告警合并成一条通知发出，避免同一问题触发大量重复告警。

## 完整配置文件结构

先看一个生产可用的完整配置：

```yaml
global:
  # 告警恢复后，Alertmanager 等多久才认为它真正恢复了
  resolve_timeout: 5m
  
  # 全局 SMTP 配置（email receiver 使用）
  smtp_smarthost: 'smtp.example.com:587'
  smtp_from: 'alertmanager@example.com'
  smtp_auth_username: 'alertmanager@example.com'
  smtp_auth_password: 'your-smtp-password'
  smtp_require_tls: true
  
  # 全局 HTTP 配置（影响所有 webhook）
  http_config:
    follow_redirects: true

# 自定义通知模板
templates:
  - '/etc/alertmanager/templates/*.tmpl'

# 路由树根节点
route:
  # 默认接收器（兜底）
  receiver: 'default-ops'
  
  # 按集群和告警名分组，同一集群的同类告警合并
  group_by: ['cluster', 'alertname', 'namespace']
  
  # 同一分组第一个告警等待 30s，收集同批次的其他告警一起发
  group_wait: 30s
  
  # 同一分组有新告警时，等 5 分钟再发
  group_interval: 5m
  
  # 告警持续未恢复，每 4h 重复通知一次
  repeat_interval: 4h
  
  routes:
    # 数据库告警 → DBA 团队
    - receiver: 'dba-team'
      matchers:
        - service =~ "mysql|postgresql|redis"
      group_wait: 10s
      repeat_interval: 1h
      continue: false
    
    # Critical 级别告警 → PagerDuty，同时抄送钉钉
    - receiver: 'pagerduty-critical'
      matchers:
        - severity = "critical"
      group_wait: 10s
      repeat_interval: 30m
      continue: true  # continue=true，继续匹配后续路由
    
    # 所有 critical 告警同时发到钉钉（和上面的 continue 配合）
    - receiver: 'dingtalk-critical'
      matchers:
        - severity = "critical"
      group_wait: 10s
    
    # Warning 告警 → 钉钉普通群
    - receiver: 'dingtalk-warning'
      matchers:
        - severity = "warning"
      group_wait: 60s
      repeat_interval: 8h
    
    # 维护相关告警 → 运维团队邮件
    - receiver: 'ops-email'
      matchers:
        - team = "ops"
    
    # 业务告警 → 对应业务组钉钉
    - receiver: 'dingtalk-business'
      matchers:
        - team = "business"

receivers:
  - name: 'default-ops'
    email_configs:
      - to: 'ops-team@example.com'
        send_resolved: true
  
  - name: 'dba-team'
    email_configs:
      - to: 'dba@example.com'
        send_resolved: true
    webhook_configs:
      - url: 'http://dingtalk-webhook:8060/dingtalk/dba/send'
        send_resolved: true
  
  - name: 'pagerduty-critical'
    pagerduty_configs:
      - routing_key: 'your-pagerduty-routing-key'
        send_resolved: true
        description: '{{ range .Alerts }}{{ .Annotations.summary }}{{ end }}'
        severity: '{{ if eq .CommonLabels.severity "critical" }}critical{{ else }}warning{{ end }}'
  
  - name: 'dingtalk-critical'
    webhook_configs:
      - url: 'http://dingtalk-webhook:8060/dingtalk/critical/send'
        send_resolved: true
        max_alerts: 10
  
  - name: 'dingtalk-warning'
    webhook_configs:
      - url: 'http://dingtalk-webhook:8060/dingtalk/warning/send'
        send_resolved: true
        max_alerts: 20
  
  - name: 'ops-email'
    email_configs:
      - to: 'ops@example.com'
        send_resolved: true
  
  - name: 'dingtalk-business'
    webhook_configs:
      - url: 'http://dingtalk-webhook:8060/dingtalk/business/send'
        send_resolved: false  # 业务告警不发恢复通知，减少噪声

inhibit_rules:
  # Critical 告警触发时，抑制同 cluster+namespace 下的 warning 告警
  - source_matchers:
      - severity = "critical"
    target_matchers:
      - severity = "warning"
    equal: ['cluster', 'namespace', 'alertname']
  
  # 节点 NotReady 时，抑制该节点上的所有 Pod 告警
  - source_matchers:
      - alertname = "KubeNodeNotReady"
    target_matchers:
      - alertname =~ "KubePodCrashLooping|KubePodNotReady"
    equal: ['node', 'cluster']
  
  # 整个集群不可达时，抑制所有该集群的告警
  - source_matchers:
      - alertname = "ClusterUnreachable"
    target_matchers:
      - cluster != ""
    equal: ['cluster']
```

## 路由树设计原则

### 根节点的特殊性

根 route 不能有 `match` 或 `match_re`，它必须匹配所有告警。它的 receiver 是兜底接收器，当所有子路由都不匹配时，告警会发到这里。

不要把根节点的 receiver 设成一个会被忽略的地方（比如一个没人看的邮件组），否则未分类的告警就会静默消失。

### continue 的用法

默认情况下，告警匹配到第一个子路由就停止，不会继续向下匹配。设置 `continue: true` 可以让告警继续往下匹配。

这在"一条告警需要同时通知多个渠道"时很有用：

```yaml
routes:
  - receiver: 'pagerduty'
    matchers:
      - severity = "critical"
    continue: true  # 不停在这里，继续往下
  
  - receiver: 'dingtalk'
    matchers:
      - severity = "critical"
    # 这里没有 continue，停止匹配
```

注意：`continue: true` 的路由即使匹配了，也会继续向下，所以下面的路由如果也能匹配，两个都会执行。

### match 和 matchers 的区别

老版本配置用 `match` 和 `match_re`，新版本推荐用 `matchers`（Alertmanager 0.22+）：

```yaml
# 老写法
match:
  severity: critical

# 新写法（推荐）
matchers:
  - severity = "critical"      # 等于
  - severity != "warning"      # 不等于
  - service =~ "mysql|pgsql"   # 正则匹配
  - service !~ "redis.*"       # 正则不匹配
```

`matchers` 更直观，支持更复杂的表达式，新配置建议统一用新写法。

## 多渠道接收配置

### 钉钉 Webhook

钉钉官方不提供 Alertmanager 集成，需要用第三方工具（如 `prometheus-webhook-dingtalk`）作为中间层：

```bash
# 部署 dingtalk-webhook
kubectl apply -f - << 'EOF'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: dingtalk-webhook
  namespace: monitoring
spec:
  replicas: 2
  selector:
    matchLabels:
      app: dingtalk-webhook
  template:
    metadata:
      labels:
        app: dingtalk-webhook
    spec:
      containers:
        - name: webhook
          image: timonwong/prometheus-webhook-dingtalk:latest
          args:
            - --config.file=/config/config.yaml
          ports:
            - containerPort: 8060
          volumeMounts:
            - name: config
              mountPath: /config
      volumes:
        - name: config
          configMap:
            name: dingtalk-webhook-config
EOF
```

`dingtalk-webhook` 的配置：

```yaml
# config.yaml
targets:
  critical:
    url: https://oapi.dingtalk.com/robot/send?access_token=YOUR_TOKEN_CRITICAL
    secret: YOUR_SECRET_CRITICAL  # 加签验证
    mention:
      all: true  # Critical 告警 @所有人
  
  warning:
    url: https://oapi.dingtalk.com/robot/send?access_token=YOUR_TOKEN_WARNING
    # warning 不需要 @所有人
  
  business:
    url: https://oapi.dingtalk.com/robot/send?access_token=YOUR_TOKEN_BUSINESS
    mention:
      mobiles:
        - "13800138000"  # @特定人
```

钉钉通知模板定制，创建 `/etc/alertmanager/templates/dingtalk.tmpl`：

```
{{ define "dingtalk.title" }}
[{{ .Status | toUpper }}] {{ .GroupLabels.alertname }} ({{ .GroupLabels.cluster }})
{{ end }}

{{ define "dingtalk.body" }}
**告警状态**: {{ if eq .Status "firing" }}🔴 触发中{{ else }}✅ 已恢复{{ end }}

**告警数量**: {{ len .Alerts }}

{{ range .Alerts }}
---
**告警名**: {{ .Labels.alertname }}
**严重程度**: {{ .Labels.severity }}
**命名空间**: {{ .Labels.namespace }}
**详情**: {{ .Annotations.description }}
**开始时间**: {{ .StartsAt.Format "2006-01-02 15:04:05" }}
{{ if .EndsAt }}**恢复时间**: {{ .EndsAt.Format "2006-01-02 15:04:05" }}{{ end }}
{{ end }}
{{ end }}
```

### PagerDuty 配置

PagerDuty 是值班告警的标准方案，支持电话、短信、App 推送，以及 on-call 排班：

```yaml
receivers:
  - name: 'pagerduty-p1'
    pagerduty_configs:
      - routing_key: 'YOUR_PAGERDUTY_INTEGRATION_KEY'
        send_resolved: true
        # 告警标题
        description: '{{ .CommonAnnotations.summary }}'
        # 严重程度映射
        severity: '{{ if eq .CommonLabels.severity "critical" }}critical{{ else if eq .CommonLabels.severity "warning" }}warning{{ else }}info{{ end }}'
        # 附加信息
        details:
          cluster: '{{ .CommonLabels.cluster }}'
          namespace: '{{ .CommonLabels.namespace }}'
          runbook: '{{ .CommonAnnotations.runbook_url }}'
        # 用于去重的 key，相同 dedup_key 的告警不会重复创建 incident
        client: 'Alertmanager'
        client_url: 'https://alertmanager.example.com/#/alerts'
```

### Email 配置

```yaml
receivers:
  - name: 'ops-email'
    email_configs:
      - to: 'ops@example.com, manager@example.com'
        send_resolved: true
        # 使用自定义模板
        html: '{{ template "email.html" . }}'
        headers:
          Subject: '[{{ .Status | toUpper }}] {{ .GroupLabels.alertname }} - {{ .GroupLabels.cluster }}'
        # TLS 配置
        tls_config:
          insecure_skip_verify: false
```

Email HTML 模板：

```html
{{ define "email.html" }}
<!DOCTYPE html>
<html>
<head>
  <style>
    .firing { color: #d32f2f; }
    .resolved { color: #388e3c; }
    table { border-collapse: collapse; width: 100%; }
    td, th { border: 1px solid #ddd; padding: 8px; }
    th { background-color: #f5f5f5; }
  </style>
</head>
<body>
<h2 class="{{ .Status }}">
  {{ if eq .Status "firing" }}🔴 告警触发{{ else }}✅ 告警恢复{{ end }}
</h2>
<table>
  <tr><th>字段</th><th>值</th></tr>
  {{ range .CommonLabels.SortedPairs }}
  <tr><td>{{ .Name }}</td><td>{{ .Value }}</td></tr>
  {{ end }}
</table>
<h3>告警列表</h3>
{{ range .Alerts }}
<div style="border: 1px solid #ccc; margin: 10px 0; padding: 10px;">
  <p><strong>{{ .Labels.alertname }}</strong></p>
  <p>{{ .Annotations.description }}</p>
  <p>开始: {{ .StartsAt.Format "2006-01-02 15:04:05 MST" }}</p>
</div>
{{ end }}
</body>
</html>
{{ end }}
```

## 抑制规则（inhibit_rules）

抑制是减少告警噪声最有效的手段之一。核心思路：当更高优先级（source）的告警触发时，自动静默相关的低优先级（target）告警。

### 经典场景：严重度抑制

```yaml
inhibit_rules:
  # Critical 触发时，抑制同组的 warning
  # equal 指定哪些标签必须相同才生效
  - source_matchers:
      - severity = "critical"
    target_matchers:
      - severity = "warning"
    equal: ['alertname', 'cluster', 'service']
```

注意：`equal` 列表里的标签，在 source 和 target 里必须同时存在且值相同，规则才会生效。如果某个标签在 source 里有，在 target 里没有，这条抑制规则对该 target 无效。

### 场景：节点宕机抑制 Pod 告警

```yaml
inhibit_rules:
  - source_matchers:
      - alertname = "NodeDown"
    target_matchers:
      - alertname =~ "PodCrashLooping|PodNotReady|DeploymentReplicasMismatch"
    # node 标签必须相同
    equal: ['cluster', 'node']
```

### 场景：整个可用区告警抑制单个服务告警

```yaml
inhibit_rules:
  - source_matchers:
      - alertname = "AZNetworkIssue"
    target_matchers:
      - az != ""  # 有 az 标签的所有告警
    equal: ['cluster', 'az']
```

## 高可用部署：三节点 Mesh 模式

单节点 Alertmanager 有单点故障风险。Alertmanager 支持 gossip 协议的集群模式，多个节点之间共享状态（静默、抑制、分组状态），避免告警重复发送。

```yaml
# alertmanager-cluster.yaml
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: alertmanager
  namespace: monitoring
spec:
  replicas: 3
  serviceName: alertmanager-headless
  selector:
    matchLabels:
      app: alertmanager
  template:
    metadata:
      labels:
        app: alertmanager
    spec:
      affinity:
        podAntiAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            - labelSelector:
                matchLabels:
                  app: alertmanager
              topologyKey: kubernetes.io/hostname
      
      containers:
        - name: alertmanager
          image: prom/alertmanager:v0.27.0
          args:
            - '--config.file=/etc/alertmanager/alertmanager.yaml'
            - '--storage.path=/alertmanager'
            - '--web.external-url=https://alertmanager.example.com'
            # 集群通信端口
            - '--cluster.listen-address=0.0.0.0:9094'
            # 通过 DNS 发现其他节点（headless service）
            - '--cluster.peer=alertmanager-0.alertmanager-headless.monitoring.svc:9094'
            - '--cluster.peer=alertmanager-1.alertmanager-headless.monitoring.svc:9094'
            - '--cluster.peer=alertmanager-2.alertmanager-headless.monitoring.svc:9094'
            # 等待对等节点连接的超时时间
            - '--cluster.peer-timeout=15s'
          ports:
            - containerPort: 9093
              name: http
            - containerPort: 9094
              name: cluster
          
          volumeMounts:
            - name: config
              mountPath: /etc/alertmanager
            - name: data
              mountPath: /alertmanager
          
          resources:
            requests:
              cpu: 100m
              memory: 256Mi
            limits:
              cpu: 500m
              memory: 512Mi
      
      volumes:
        - name: config
          configMap:
            name: alertmanager-config
  
  volumeClaimTemplates:
    - metadata:
        name: data
      spec:
        accessModes: ["ReadWriteOnce"]
        storageClassName: gp3
        resources:
          requests:
            storage: 10Gi
---
# Headless Service for StatefulSet DNS
apiVersion: v1
kind: Service
metadata:
  name: alertmanager-headless
  namespace: monitoring
spec:
  clusterIP: None
  selector:
    app: alertmanager
  ports:
    - port: 9094
      name: cluster
---
# 对外暴露 UI 和 API
apiVersion: v1
kind: Service
metadata:
  name: alertmanager
  namespace: monitoring
spec:
  selector:
    app: alertmanager
  ports:
    - port: 9093
      name: http
```

Prometheus 配置多个 Alertmanager 地址：

```yaml
alerting:
  alertmanagers:
    - static_configs:
        - targets:
            - alertmanager-0.alertmanager-headless.monitoring.svc:9093
            - alertmanager-1.alertmanager-headless.monitoring.svc:9093
            - alertmanager-2.alertmanager-headless.monitoring.svc:9093
```

Prometheus 会把同一条告警发给所有 Alertmanager 节点，节点之间通过 gossip 协议协调，确保每条告警只发送一次。

## 踩坑记录

**坑1：route 匹配顺序和 continue 的误解**

症状：同一条告警应该发给两个 receiver，但只发到了第一个。

原因：没有设置 `continue: true`，告警匹配到第一个路由就停止了。

修复：在需要继续匹配的路由上加 `continue: true`。

但有个容易忽略的细节：如果父路由（非根节点）匹配了，子路由才会被检查。如果是根节点的子路由，是从上到下顺序检查的，一旦某个子路由匹配且没有 `continue`，就停止。

**坑2：group_wait 导致告警延迟**

症状：凌晨 3 点数据库宕机，告警发出来已经是 3 点 31 分，原因是 `group_wait: 30s`，但 `repeat_interval: 4h` 之前还有一次 `group_interval: 5m`，第一批告警等了 30s 发出，但随后又因为 `group_interval` 等了 5 分钟才发第二批，而第二批里才有更关键的告警。

理解这三个时间参数的关系：
- `group_wait`：分组内**第一次**发送前的等待时间（聚合同批次告警）
- `group_interval`：分组内**有新告警**时等待时间（让新告警加入）
- `repeat_interval`：**没有新告警**时重复发送的间隔

对于 critical 级别，把 `group_wait` 和 `group_interval` 都调小（10s / 1m），宁可稍微重复，也要快。

**坑3：webhook 超时导致告警丢失**

症状：钉钉 webhook 偶尔收不到告警，Alertmanager 日志里有 `context deadline exceeded`。

原因：
1. 钉钉 robot 的频率限制：每分钟最多 20 条，超过会被限流
2. webhook 服务（dingtalk-webhook）遇到限流后没有重试，直接返回错误

解法：
- 在 dingtalk-webhook 加指数退避重试
- Alertmanager 的 `webhook_configs` 里设置合理的 `http_config.timeout`（默认 10s）
- 使用 `max_alerts` 限制单次发送的告警数量，避免一次发太多触发限流

```yaml
webhook_configs:
  - url: 'http://dingtalk-webhook:8060/dingtalk/critical/send'
    send_resolved: true
    max_alerts: 5  # 每次最多发 5 条，分批发送
    http_config:
      # 增加超时时间
      timeout: 30s
```

**坑4：静默不生效**

症状：创建了 Silence，但告警还在继续发送。

可能原因：
1. Silence 的标签匹配条件和告警标签不完全匹配，要求精确匹配
2. Silence 已经过期
3. 高可用模式下，Silence 只同步到了部分节点（gossip 延迟）

排查方法：在 Alertmanager UI 的 Silences 页面，点击对应 Silence 查看"Affected Alerts"，如果显示 0 条，说明标签没匹配上。

**坑5：inhibit_rules 中 equal 字段的陷阱**

症状：明明两条告警的 cluster 值相同，但抑制规则没有生效。

原因：`equal` 列表里的标签名是大小写敏感的，并且如果某个标签在告警里不存在，这条抑制规则就不会对该告警生效。

```yaml
inhibit_rules:
  - source_matchers:
      - severity = "critical"
    target_matchers:
      - severity = "warning"
    # 如果某条 warning 告警没有 'cluster' 标签，这条规则不会抑制它
    equal: ['cluster', 'alertname']
```

解决方法：确保 Prometheus 告警规则里给所有告警都加上 `cluster` 标签：

```yaml
# prometheus-rules.yaml
groups:
  - name: example
    rules:
      - alert: SomethingWrong
        expr: some_metric > threshold
        labels:
          severity: warning
          cluster: '{{ $externalLabels.cluster }}'  # 从外部标签继承
```

---

告警体系是一个需要持续调优的系统。刚上线时 `repeat_interval` 可以短一点，告警多了之后逐渐调长；业务高速发展期可以把阈值调宽松，稳定期再收紧。Alertmanager 的 UI 提供了很好的调试界面，`/api/v2/alerts` 可以看到当前所有活跃告警，`/api/v2/silences` 可以管理静默，善用这些工具可以大幅提升排查效率。
