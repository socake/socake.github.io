---
title: "Flagger 渐进式交付实战：金丝雀、蓝绿、A/B 与 Istio/NGINX/Gateway API 集成"
date: 2026-04-11T10:00:00+08:00
draft: false
tags: ["Flagger", "渐进式交付", "金丝雀发布", "Istio", "Kubernetes", "CI/CD", "Gateway API"]
categories: ["云原生"]
description: "用 Flagger 在 Kubernetes 上实现金丝雀、蓝绿、A/B 三种渐进式交付策略，涵盖 Istio、NGINX Ingress、Gateway API 集成，Prometheus 指标驱动的自动回滚，与 Argo Rollouts 的选型对比。"
summary: "传统的 kubectl apply 发布方式让风险集中在发布那一刻。Flagger 通过指标驱动的渐进式切流（Canary Analysis），把风险摊到整个发布过程，异常自动回滚。本文基于官方文档，系统讲解 Canary CR 的完整字段、三种策略的配置模板、与 Istio/NGINX Ingress/Gateway API 的集成、自定义指标分析、自动化回滚机制，以及与 Argo Rollouts 的选型对比。"
toc: true
math: false
diagram: false
keywords: ["Flagger", "Canary", "Blue/Green", "A/B Testing", "Istio", "Gateway API", "Argo Rollouts", "渐进式交付"]
params:
  reading_time: true
---

## 1. 发布风险与渐进式交付

### 1.1 滚动更新到底解决了什么，又没解决什么

Kubernetes 原生的 `Deployment` 只有一种发布策略是真正意义上的"安全"的：`RollingUpdate`。它通过 `maxSurge` 和 `maxUnavailable` 两个旋钮控制滚动速度，在新旧 Pod 之间平滑切换，保证服务不中断。这套机制从 2015 年 Kubernetes 1.0 之后几乎没动过，原因是它已经足够解决"部署过程中不掉线"这一个问题。

但滚动更新没解决的问题更多：

- **风险集中在发布那一刻**：滚动 1 分钟完成，如果新版本有 bug，1 分钟内所有用户都被打中。
- **没有指标门禁**：kubectl 不知道什么叫"错误率升高"，它只看 `readinessProbe` 是否为 ok。而 readiness 只能告诉你 Pod 能否接流量，不能告诉你业务逻辑是否正确。
- **回滚靠人**：发现问题之后，运维敲 `kubectl rollout undo`，从发现到执行中间是分钟级的人工窗口。
- **无法做 A/B 测试**：没办法让 1% 流量去尝试一个实验性版本，其余 99% 走稳定版本。
- **无法渐进切流**：滚动 3/10 个 Pod，流量比例并不是精确的 30%，因为 `kube-proxy` 的负载均衡粒度是 endpoint 而不是权重。

这些问题，渐进式交付（Progressive Delivery）都可以解决。渐进式交付不是一个具体的工具，而是一类方法论：把"部署"和"放量"解耦，让新版本先上线但不接流量，然后按指标分阶段放量，每一阶段都要过指标门禁，过不了就自动回滚。

### 1.2 金丝雀、蓝绿、A/B 三种策略的本质区别

三种策略经常被混着提，但它们解决的问题不一样：

**金丝雀（Canary）**：两个版本同时在线，按权重切流。从 10% → 20% → 50% → 100%。核心假设：新版本如果有问题，小流量下就能通过错误率、延迟等指标观察到。适用于大多数增量变更。

**蓝绿（Blue/Green）**：两套完整环境同时存在，流量一次性切换。切换前可以对绿环境做充分的冒烟测试，通过之后把流量从蓝切到绿。核心假设：变更的风险无法通过小流量观察，必须在"影子环境"里做完整回归。适用于 schema 变更、协议变更、大版本升级。

**A/B 测试（A/B Testing）**：基于请求特征（header、cookie、地理位置、用户 ID）切流，而不是基于权重。核心假设：需要让特定用户群体走特定版本，观察业务指标而不是系统指标。适用于产品实验、功能灰度、按租户开关。

| 维度 | 金丝雀 | 蓝绿 | A/B |
|------|--------|------|-----|
| 切流依据 | 权重 | 全量切换 | 请求特征 |
| 观察指标 | 系统指标（错误率、延迟） | 手动冒烟 + 系统指标 | 业务指标（转化率、留存） |
| 回滚成本 | 降权即可 | 切回蓝环境 | 降权/下线 canary 规则 |
| 资源成本 | 略高（多一份 Pod） | 翻倍 | 略高 |
| 典型场景 | 增量变更 | 高风险变更 | 产品实验 |

Flagger 把这三种策略统一到一个 CRD（`Canary`）里面，只是 `analysis` 字段的配置不同。这是 Flagger 区别于 Argo Rollouts 的核心设计之一。

### 1.3 为什么需要"控制器化"地做这件事

原理搞清楚之后，很多人第一反应是"我写个脚本也能做"。比如自己写一个 shell 脚本，部署 canary Deployment、更新 VirtualService 的权重、调用 PromQL 查错误率、判断之后再推进。这种脚本化方案的问题在于：

1. **状态不持久**：脚本跑一半挂了怎么办，重启之后无法感知当前阶段。
2. **没有一致性保证**：多个服务同时发版，可能互相影响，脚本难以编排。
3. **不是声明式**：和 Kubernetes 的声明式风格格格不入，GitOps 工具（Argo CD、Flux）无法直接管理。
4. **扩展困难**：想加一个新的指标来源、新的 mesh 支持，都要改脚本。

控制器化（operator pattern）是 Kubernetes 生态解决这类问题的标准答案。Flagger 把整套逻辑装进一个 controller，通过 CRD 声明意图，controller 轮询当前状态并推进。这样：

- 状态写在 etcd 里，controller 重启无损。
- 用户声明"我要金丝雀，每 1 分钟增 10%"，controller 负责推进。
- 可以被 Argo CD / Flux 作为标准 Kubernetes 资源管理。
- 新增 provider 只要实现一个 interface，不动主干。

这也是 Flagger 能长期活在 CNCF 毕业项目之下的原因。

## 2. Flagger 是什么

### 2.1 项目背景

Flagger 最初由 Weaveworks 团队开发，和 Flux 是同一家。2020 年随 Flux 一起捐给 CNCF，目前是 CNCF 毕业项目。它在设计上刻意做到 mesh/ingress 无关，这意味着不论你用 Istio、Linkerd、App Mesh、NGINX Ingress、Contour、Gloo、Skipper、Traefik，还是新兴的 Gateway API，都能用同一个 CRD 描述渐进式发布流程。

### 2.2 和 Flux 的关系

Flagger 是 Flux 生态的一部分，但不强依赖 Flux。你可以只装 Flagger 不装 Flux，在 Argo CD 的体系下使用也完全没问题。Flagger 负责"发布过程"，Flux/Argo CD 负责"期望状态同步"，二者正交。

### 2.3 和 Service Mesh 的关系

Flagger 不是 service mesh，它是 service mesh 的"指挥家"。它利用 mesh 提供的流量路由能力（VirtualService / HTTPRoute / Ingress annotation）执行切流，利用 mesh 提供的遥测（Prometheus 指标）做决策。没有 mesh 也能跑，Flagger 会退化到用 NGINX Ingress 或 Gateway API 的 backendRefs 切流。

### 2.4 核心能力清单

- **Canary**：权重切流，可配置步长、阈值、最大权重。
- **Blue/Green**：`iterations` 模式，一次性切流前多次指标检查。
- **A/B Testing**：基于 header/cookie 的流量匹配。
- **Traffic Mirroring**：影子流量，复制一份生产流量到 canary，不影响用户。
- **Metric Analysis**：支持 Prometheus、Datadog、New Relic、CloudWatch、Dynatrace、Graphite。
- **Webhook**：pre/during/post rollout 的钩子，用于集成负载测试、冒烟测试、外部审批。
- **通知**：Slack、Discord、Microsoft Teams、Rocket、Google Chat、通用 Webhook。
- **Alerting**：MetricTemplate 一键生成 PrometheusRule。

## 3. 架构剖析

### 3.1 核心对象关系

用户创建一个 `Canary` 资源，指向一个现有的 `Deployment`（称为 target）。Flagger controller 监听这个 CR，然后自顶向下创建一堆派生资源：

```
Canary (CR, user creates)
  └── targetRef → Deployment (user creates, Flagger mutates)
  │
  ├── creates: <name>-primary Deployment
  ├── creates: <name>-primary Service (ClusterIP)
  ├── creates: <name>-canary Service  (ClusterIP)
  ├── creates: <name>  Service (虚拟入口，指向 primary)
  ├── creates: VirtualService (Istio) / HTTPRoute (Gateway API) / Ingress rules (NGINX)
  └── creates: MetricTemplate / PrometheusRule
```

注意几个关键点：

1. **targetRef 指向的 Deployment 最终不会接生产流量**。Flagger 会把它的副本数降为 0，只把它当作"canary 的源"，真正跑生产流量的是 `<name>-primary`。
2. **用户不要手动改 `<name>-primary`**，它由 Flagger 管理。
3. **服务访问入口是 `<name>` 这个 Service**，不是原来的 `<name>`，Flagger 会把原 Service 的 selector 也调整到 primary。
4. **每次发布，Flagger 检测到 targetRef 变化，把变化同步到 canary Deployment（即用户创建的那个），然后启动 analysis**。analysis 推进过程中，流量逐步从 primary 迁到 canary，最后 analysis 通过，primary 被更新成 canary 的内容，canary 副本数再次降为 0，完成一次发布。

### 3.2 发布生命周期状态机

一个 Canary 对象的 `status.phase` 会在下列状态之间流转：

- **Initializing**：Flagger 正在创建 primary/canary 资源。
- **Initialized**：primary 已就绪，等待 targetRef 的变化。
- **Progressing**：检测到变化，正在做 analysis（切流 + 指标检查）。
- **Promoting**：analysis 通过，正在把 canary 的配置同步到 primary。
- **Finalising**：primary 更新完成，等待老版本 Pod 销毁。
- **Succeeded**：整个发布成功，canary 副本数归零，等待下一次变化。
- **Failed**：analysis 未通过，流量切回 primary，canary 副本数归零。

这个状态机很重要，排障时第一步就是 `kubectl get canary` 看当前卡在哪。

### 3.3 与 Prometheus 的关系

Flagger 自带两条默认指标：`request-success-rate` 和 `request-duration`。它们用的 PromQL 会根据 mesh provider 不同而不同。比如 Istio 的 `request-success-rate` 是：

```promql
sum(
  rate(
    istio_requests_total{
      reporter="destination",
      destination_workload_namespace="{{ namespace }}",
      destination_workload=~"{{ target }}",
      response_code!~"5.*"
    }[{{ interval }}]
  )
) / sum(
  rate(
    istio_requests_total{
      reporter="destination",
      destination_workload_namespace="{{ namespace }}",
      destination_workload=~"{{ target }}"
    }[{{ interval }}]
  )
) * 100
```

NGINX 的版本是基于 `nginx_ingress_controller_requests`，Gateway API 的版本依赖 Prometheus 抓取 Gateway 实现的指标。三者结构一致，只是指标名换了。

Flagger 把这些 PromQL 抽象成 `MetricTemplate` CRD，用户可以通过它定义任意自定义指标。这是后面自定义指标章节要展开的。

## 4. 安装部署

### 4.1 前置条件

- Kubernetes 1.23+
- 一个 mesh 或 ingress provider（Istio / Linkerd / NGINX / Gateway API 实现 / …）
- Prometheus 可访问的 endpoint（不必装在同一个集群，但网络要通）

### 4.2 Helm 安装 Flagger（Istio 模式）

```bash
helm repo add flagger https://flagger.app
helm repo update

kubectl create ns flagger-system || true

helm upgrade -i flagger flagger/flagger \
  --namespace flagger-system \
  --set meshProvider=istio \
  --set metricsServer=http://prometheus.monitoring:9090 \
  --set slack.url=https://hooks.slack.com/services/xxx \
  --set slack.channel=release \
  --set slack.user=flagger
```

几个参数的意思：

- `meshProvider`：mesh/ingress 类型，可选 `istio | linkerd | appmesh:v1beta2 | contour | gloo | nginx | skipper | traefik | osm | kuma | gatewayapi`。
- `metricsServer`：Prometheus 的 URL。这里填一个例子 `http://prometheus.monitoring:9090`，请替换成你自己集群的地址。
- `slack.*`：告警通知渠道。不用 Slack 可以用 `msteams.url` / `discord.url` / `webhook.url`。

### 4.3 安装 Flagger Loadtester（可选）

Flagger 自带一个负载测试工具 `flagger-loadtester`，webhook 里调用它可以在 canary 阶段主动产生流量，让指标有数据可算。生产环境强烈建议装。

```bash
helm upgrade -i flagger-loadtester flagger/loadtester \
  --namespace flagger-system \
  --set cmd.timeout=1h \
  --set cmd.namespaceRegexp=''
```

### 4.4 验证安装

```bash
kubectl -n flagger-system get pods
kubectl -n flagger-system logs deploy/flagger -f
```

看到 log 里有 `Connected to metrics server http://prometheus.monitoring:9090` 就表示 Flagger 和 Prometheus 通了。如果看到 `failed to query Prometheus`，先去查网络可达性和 Prometheus URL 是否正确。

### 4.5 Gateway API 模式下的区别

Gateway API 模式要多装一步，指定 Gateway 的 class：

```bash
helm upgrade -i flagger flagger/flagger \
  --namespace flagger-system \
  --set meshProvider=gatewayapi \
  --set metricsServer=http://prometheus.monitoring:9090
```

然后 Canary CR 里要填 `gatewayRefs`，这个后面会展开。

## 5. Canary CR 完整字段拆解

Canary 这个 CRD 字段非常多，下面把常用的都过一遍，每个字段都配简短解释。

### 5.1 顶层结构

```yaml
apiVersion: flagger.app/v1beta1
kind: Canary
metadata:
  name: frontend-api
  namespace: apps
spec:
  # 1. 目标资源
  provider: istio
  targetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: frontend-api
  autoscalerRef:
    apiVersion: autoscaling/v2
    kind: HorizontalPodAutoscaler
    name: frontend-api
  progressDeadlineSeconds: 600

  # 2. 服务与路由
  service:
    port: 80
    targetPort: 8080
    portName: http
    portDiscovery: true
    gateways:
      - public-gateway.istio-system.svc.cluster.local
      - mesh
    hosts:
      - api.example.com
    trafficPolicy:
      tls:
        mode: DISABLE
    retries:
      attempts: 3
      perTryTimeout: 1s
      retryOn: gateway-error,connect-failure,refused-stream
    headers:
      request:
        add:
          x-envoy-upstream-rq-timeout-ms: "15000"

  # 3. 分析配置
  analysis:
    interval: 1m
    threshold: 5
    maxWeight: 50
    stepWeight: 10
    metrics:
      - name: request-success-rate
        thresholdRange:
          min: 99
        interval: 1m
      - name: request-duration
        thresholdRange:
          max: 500
        interval: 1m
    webhooks:
      - name: acceptance-test
        type: pre-rollout
        url: http://flagger-loadtester.flagger-system/
        timeout: 30s
        metadata:
          type: bash
          cmd: "curl -sS http://frontend-api-canary.apps/healthz"
      - name: load-test
        url: http://flagger-loadtester.flagger-system/
        timeout: 5s
        metadata:
          cmd: "hey -z 1m -q 10 -c 2 http://frontend-api-canary.apps/"
```

### 5.2 `provider`

指定 mesh/ingress 类型。如果 Flagger 全局只有一个 provider，可以省略；多 provider 共存时必填。常见取值：`istio | linkerd | nginx | contour | gatewayapi | appmesh:v1beta2 | gloo | traefik | osm | kuma`。

### 5.3 `targetRef`

指向被管理的 Deployment（也可以是 DaemonSet）。注意 Flagger 会接管这个 Deployment 的副本数，你不应该手动 `kubectl scale`。

### 5.4 `autoscalerRef`

可选。如果服务有 HPA，这里声明一下，Flagger 会在 primary 上复制一份 HPA，保证 primary 有自己的弹性。不声明会导致 primary 无 HPA，canary 阶段一旦突发流量，primary 扛不住。

### 5.5 `progressDeadlineSeconds`

一次发布的总超时。超过这个时间 analysis 还没完，整体判定失败回滚。默认 600 秒。按你的 analysis 长度估算后设置，建议 = `interval * (maxWeight / stepWeight) * 1.5`。

### 5.6 `service.port / targetPort / portName`

和 Service 的字段一致。`portName` 在 Istio 场景下必须以 `http-` 或 `grpc-` 开头（Istio 约定），否则不会走 mesh。

### 5.7 `service.portDiscovery`

如果设为 `true`，Flagger 会自动发现 Deployment 其他端口并加到 Service 上。用于一个 Pod 暴露多个端口的情况。

### 5.8 `service.gateways / hosts`

Istio 专属。`gateways` 填要关联的 Istio Gateway 名，`hosts` 填 host 列表。Flagger 会把它们写到自动生成的 VirtualService 里。

### 5.9 `service.trafficPolicy / retries / headers`

这些字段会透传到 Istio DestinationRule / VirtualService。需要 CORS、超时、重试等高级配置，在这里填即可。

### 5.10 `analysis.interval`

每次指标检查的间隔。建议 1m 起步，指标太稀疏的场景可以到 2m。低于 30 秒基本没意义，PromQL 窗口太小误差大。

### 5.11 `analysis.threshold`

指标连续失败多少次判定整体失败。默认 10。实战建议降到 3-5，避免发布拖得太久。

### 5.12 `analysis.maxWeight / stepWeight`

`maxWeight` 是 canary 的最大权重。到了这个权重且指标全部通过，就进入 promotion 阶段（primary 被同步成 canary 内容）。`stepWeight` 是每次推进的增量。经典配置：`maxWeight=50, stepWeight=10`，意味着 10% → 20% → 30% → 40% → 50%，每一步停留 `interval` 秒。

注意 `maxWeight` 不必到 100，50 就够了。因为到了 50% 如果没问题，继续推到 100 也不会发现新问题，不如直接 promote。

### 5.13 `analysis.iterations`（Blue/Green）

当 `iterations` 被设置时，canary 会以 0% 或 100% 两种状态跑，每次 interval 做一次指标检查，跑满 iterations 次就 promote。这是蓝绿模式。不能和 `stepWeight` 同时出现。

### 5.14 `analysis.match`（A/B）

当 `match` 被设置时，Flagger 会基于请求匹配规则把特定流量转到 canary，其余走 primary。match 的语法是 Istio VirtualService 的 match，支持 header、uri、scheme 等。也是和 `stepWeight` 互斥。

### 5.15 `analysis.metrics[]`

指标列表，每个元素指向一个内置指标或 MetricTemplate。每个指标有一个 `thresholdRange`（min 或 max）和一个 `interval`。任意一个指标在 `threshold` 次连续检查里失败，整体失败。

### 5.16 `analysis.webhooks[]`

钩子列表，每个钩子有个 `type`，决定在什么时候调用：

- `confirm-rollout`：开始 analysis 前等待人工确认。HTTP 200 才继续。
- `pre-rollout`：analysis 开始前调用一次，失败则不开始。
- `rollout`：每次 interval 都会调一次，适合跑 smoke test。
- `confirm-promotion`：promotion 前的人工审批。
- `post-rollout`：promotion 之后，无论成功失败都调一次。
- `rollback`：失败回滚时调用。
- `event`：Canary 状态变化事件（通知用）。

### 5.17 `analysis.alerts[]`

指定告警渠道（通过 `AlertProvider` CRD 引用），可以为单个 Canary 覆盖全局默认渠道。

### 5.18 `skipAnalysis`

设为 `true` 时跳过所有分析，直接 promote。应急使用，不建议生产长期打开。

## 6. 金丝雀策略完整模板（Istio）

下面给一套可以直接 apply 的 YAML，服务名统一为 `frontend-api`，命名空间 `apps`。

### 6.1 Deployment

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: frontend-api
  namespace: apps
  labels:
    app: frontend-api
spec:
  replicas: 3
  selector:
    matchLabels:
      app: frontend-api
  template:
    metadata:
      labels:
        app: frontend-api
      annotations:
        prometheus.io/scrape: "true"
        prometheus.io/port: "9090"
    spec:
      containers:
        - name: app
          image: registry.example.com/frontend-api:1.0.0
          imagePullPolicy: IfNotPresent
          ports:
            - name: http
              containerPort: 8080
            - name: metrics
              containerPort: 9090
          readinessProbe:
            httpGet:
              path: /healthz
              port: 8080
            initialDelaySeconds: 5
            periodSeconds: 5
          livenessProbe:
            httpGet:
              path: /healthz
              port: 8080
            initialDelaySeconds: 15
            periodSeconds: 10
          resources:
            requests:
              cpu: 100m
              memory: 128Mi
            limits:
              cpu: 500m
              memory: 512Mi
```

注意 `containerPort: 8080` 的 `name: http`，后面 Canary 里 `targetPort: 8080` 要对得上。

### 6.2 HPA

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: frontend-api
  namespace: apps
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: frontend-api
  minReplicas: 3
  maxReplicas: 20
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 70
```

### 6.3 Canary

```yaml
apiVersion: flagger.app/v1beta1
kind: Canary
metadata:
  name: frontend-api
  namespace: apps
spec:
  provider: istio
  targetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: frontend-api
  autoscalerRef:
    apiVersion: autoscaling/v2
    kind: HorizontalPodAutoscaler
    name: frontend-api
  progressDeadlineSeconds: 900
  service:
    port: 80
    targetPort: 8080
    portName: http
    gateways:
      - public-gateway.istio-system.svc.cluster.local
      - mesh
    hosts:
      - api.example.com
    retries:
      attempts: 3
      perTryTimeout: 2s
      retryOn: gateway-error,connect-failure,refused-stream
  analysis:
    interval: 1m
    threshold: 5
    maxWeight: 50
    stepWeight: 10
    metrics:
      - name: request-success-rate
        thresholdRange:
          min: 99
        interval: 1m
      - name: request-duration
        thresholdRange:
          max: 500
        interval: 1m
    webhooks:
      - name: acceptance-test
        type: pre-rollout
        url: http://flagger-loadtester.flagger-system/
        timeout: 30s
        metadata:
          type: bash
          cmd: "curl -sS http://frontend-api-canary.apps/healthz"
      - name: load-test
        url: http://flagger-loadtester.flagger-system/
        timeout: 5s
        metadata:
          cmd: "hey -z 1m -q 20 -c 4 http://frontend-api-canary.apps/"
```

apply 之后 Flagger 会：

1. 创建 `frontend-api-primary` Deployment，副本数 3。
2. 创建 `frontend-api-primary` / `frontend-api-canary` Service。
3. 把用户定义的 `frontend-api` Deployment 副本数降为 0。
4. 创建 VirtualService + DestinationRule。
5. 把 Canary status 置为 `Initialized`。

之后你改 `frontend-api` Deployment 的镜像 tag 到 `1.1.0`，Flagger 会：

1. 把 `frontend-api` Deployment 副本数恢复为 3，启动新版本 Pod。
2. 每 1 分钟推进 10% 权重，检查指标。
3. 任何指标连续 5 次失败则整体失败，权重清零，canary 副本数归零。
4. 权重到 50% 且指标全绿，进入 promotion：把 `frontend-api-primary` 的镜像同步到 `1.1.0`，primary 滚动更新。
5. primary 完成后，权重切回 0，canary 副本归零，发布成功。

## 7. 蓝绿策略完整模板

蓝绿的本质是"不切权重，只切指标"。用 `iterations` 代替 `stepWeight`。

### 7.1 Canary（Blue/Green 模式）

```yaml
apiVersion: flagger.app/v1beta1
kind: Canary
metadata:
  name: frontend-api
  namespace: apps
spec:
  provider: istio
  targetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: frontend-api
  autoscalerRef:
    apiVersion: autoscaling/v2
    kind: HorizontalPodAutoscaler
    name: frontend-api
  progressDeadlineSeconds: 1800
  service:
    port: 80
    targetPort: 8080
    portName: http
    gateways:
      - public-gateway.istio-system.svc.cluster.local
      - mesh
    hosts:
      - api.example.com
  analysis:
    interval: 1m
    threshold: 2
    iterations: 10
    metrics:
      - name: request-success-rate
        thresholdRange:
          min: 99
        interval: 1m
      - name: request-duration
        thresholdRange:
          max: 500
        interval: 1m
    webhooks:
      - name: smoke-test
        type: pre-rollout
        url: http://flagger-loadtester.flagger-system/
        timeout: 2m
        metadata:
          type: bash
          cmd: "curl -sS -f http://frontend-api-canary.apps/api/v1/ready"
      - name: confirm-promotion
        type: confirm-promotion
        url: http://ops-webhook.example.com/approve
        timeout: 1h
        metadata:
          message: "frontend-api ready for promotion, please approve"
```

注意：

- `iterations: 10` 表示做 10 轮指标检查，每轮 1 分钟，总共 10 分钟。
- `threshold: 2` 表示单个指标连续 2 次失败就整体失败。
- `confirm-promotion` webhook 加了人工审批，在 10 轮检查通过后、真正 promote 之前停住等人点头。

蓝绿模式下 canary 不接生产流量（权重始终是 0），所以 `load-test` webhook 在这里仍然有意义：它往 `frontend-api-canary` Service 上打流量让指标有值，否则 10 轮检查都在处理空数据。

## 8. A/B 测试策略

A/B 模式基于请求匹配，不是权重。典型场景是：给带 `x-experiment: canary` header 的请求走新版本。

```yaml
apiVersion: flagger.app/v1beta1
kind: Canary
metadata:
  name: frontend-api
  namespace: apps
spec:
  provider: istio
  targetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: frontend-api
  autoscalerRef:
    apiVersion: autoscaling/v2
    kind: HorizontalPodAutoscaler
    name: frontend-api
  progressDeadlineSeconds: 1800
  service:
    port: 80
    targetPort: 8080
    portName: http
    gateways:
      - public-gateway.istio-system.svc.cluster.local
      - mesh
    hosts:
      - api.example.com
  analysis:
    interval: 1m
    threshold: 5
    iterations: 20
    match:
      - headers:
          x-experiment:
            exact: canary
      - headers:
          cookie:
            regex: ".*experiment=canary.*"
    metrics:
      - name: request-success-rate
        thresholdRange:
          min: 99
        interval: 1m
      - name: request-duration
        thresholdRange:
          max: 500
        interval: 1m
    webhooks:
      - name: generate-traffic
        type: rollout
        url: http://flagger-loadtester.flagger-system/
        timeout: 1m
        metadata:
          cmd: |
            hey -z 1m -q 5 -c 2 -H 'x-experiment: canary' http://frontend-api.apps/
```

match 里列出的所有规则是 OR 关系，满足任意一条就走 canary。`iterations` 和 A/B 一起用，表示做 20 轮检查，每轮 1 分钟。

A/B 模式下 canary 始终只接满足条件的请求，其余流量还是走 primary。promotion 时 primary 被同步成 canary 内容，match 规则解除，canary 副本归零。

## 9. Metrics Provider 接入

### 9.1 Prometheus（默认）

Helm 安装时 `metricsServer` 参数指向 Prometheus URL。所有 MetricTemplate 默认用这个连接。如果有多个 Prometheus，可以在 MetricTemplate 里按 provider 覆盖。

### 9.2 Datadog

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: datadog
  namespace: apps
data:
  datadog_api_key: <base64>
  datadog_application_key: <base64>
---
apiVersion: flagger.app/v1beta1
kind: MetricTemplate
metadata:
  name: frontend-api-datadog-success
  namespace: apps
spec:
  provider:
    type: datadog
    address: https://api.datadoghq.com
    secretRef:
      name: datadog
  query: |
    100 - (
      sum:trace.http.request.errors{service:{{ target }}}.as_count() /
      sum:trace.http.request.hits{service:{{ target }}}.as_count()
    ) * 100
```

### 9.3 New Relic

```yaml
apiVersion: flagger.app/v1beta1
kind: MetricTemplate
metadata:
  name: frontend-api-newrelic
  namespace: apps
spec:
  provider:
    type: newrelic
    secretRef:
      name: newrelic
  query: |
    SELECT percentage(count(*), WHERE httpResponseCode NOT LIKE '5%')
    FROM Transaction WHERE appName = '{{ target }}'
```

### 9.4 CloudWatch

```yaml
apiVersion: flagger.app/v1beta1
kind: MetricTemplate
metadata:
  name: frontend-api-cloudwatch
  namespace: apps
spec:
  provider:
    type: cloudwatch
    region: us-west-2
  query: |
    [
      {
        "Id": "e1",
        "Expression": "m1 / m2 * 100",
        "Label": "success-rate"
      },
      {
        "Id": "m1",
        "MetricStat": {
          "Metric": {
            "Namespace": "AWS/ApplicationELB",
            "MetricName": "HTTPCode_Target_2XX_Count",
            "Dimensions": [
              {"Name": "LoadBalancer", "Value": "app/alb/xxx"}
            ]
          },
          "Period": 60,
          "Stat": "Sum"
        },
        "ReturnData": false
      },
      {
        "Id": "m2",
        "MetricStat": {
          "Metric": {
            "Namespace": "AWS/ApplicationELB",
            "MetricName": "RequestCount",
            "Dimensions": [
              {"Name": "LoadBalancer", "Value": "app/alb/xxx"}
            ]
          },
          "Period": 60,
          "Stat": "Sum"
        },
        "ReturnData": false
      }
    ]
```

### 9.5 Graphite

```yaml
apiVersion: flagger.app/v1beta1
kind: MetricTemplate
metadata:
  name: frontend-api-graphite
  namespace: apps
spec:
  provider:
    type: graphite
    address: http://graphite.monitoring:8080
  query: |
    target=alias(asPercent(
      sumSeries(stats.counters.{{ target }}.ok.count),
      sumSeries(stats.counters.{{ target }}.all.count)
    ), 'success-rate')
```

无论用哪个 provider，最后都在 Canary 的 `analysis.metrics[].templateRef` 里引用 MetricTemplate。

## 10. 自定义 MetricTemplate 深入

### 10.1 为什么需要自定义

内置的 `request-success-rate` 只看 HTTP 5xx，很多业务需要看：

- 业务层错误码（HTTP 200 但 body 里有 `code != 0`）
- 下游依赖错误率（数据库连接失败、外部 API 失败）
- P99 延迟，不是平均延迟
- 消息队列消费延迟
- 缓存命中率

这些都需要自己写 PromQL。Flagger 用 Go template 语法提供变量：

- `{{ namespace }}`：Canary 所在 ns
- `{{ target }}`：targetRef 名
- `{{ interval }}`：analysis interval
- `{{ variables.xxx }}`：用户自定义变量

### 10.2 业务错误码 MetricTemplate

```yaml
apiVersion: flagger.app/v1beta1
kind: MetricTemplate
metadata:
  name: business-success-rate
  namespace: apps
spec:
  provider:
    type: prometheus
    address: http://prometheus.monitoring:9090
  query: |
    100 - (
      sum(rate(
        http_requests_total{
          namespace="{{ namespace }}",
          workload="{{ target }}",
          business_code!="0"
        }[{{ interval }}]
      )) /
      sum(rate(
        http_requests_total{
          namespace="{{ namespace }}",
          workload="{{ target }}"
        }[{{ interval }}]
      ))
    ) * 100
```

在 Canary 里这样用：

```yaml
analysis:
  metrics:
    - name: "business success rate"
      templateRef:
        name: business-success-rate
        namespace: apps
      thresholdRange:
        min: 99.5
      interval: 1m
```

### 10.3 P99 延迟 MetricTemplate

```yaml
apiVersion: flagger.app/v1beta1
kind: MetricTemplate
metadata:
  name: http-p99-latency
  namespace: apps
spec:
  provider:
    type: prometheus
    address: http://prometheus.monitoring:9090
  query: |
    histogram_quantile(0.99,
      sum(rate(
        istio_request_duration_milliseconds_bucket{
          reporter="destination",
          destination_workload_namespace="{{ namespace }}",
          destination_workload="{{ target }}"
        }[{{ interval }}]
      )) by (le)
    )
```

### 10.4 下游依赖错误率

```yaml
apiVersion: flagger.app/v1beta1
kind: MetricTemplate
metadata:
  name: db-error-rate
  namespace: apps
spec:
  provider:
    type: prometheus
    address: http://prometheus.monitoring:9090
  query: |
    sum(rate(
      db_client_errors_total{
        namespace="{{ namespace }}",
        workload="{{ target }}"
      }[{{ interval }}]
    )) /
    sum(rate(
      db_client_requests_total{
        namespace="{{ namespace }}",
        workload="{{ target }}"
      }[{{ interval }}]
    )) * 100
```

threshold 就写 `max: 1`（错误率不能超过 1%）。

### 10.5 变量化的 MetricTemplate

Flagger 0.30+ 支持 `variables`，让一个 template 被多个 Canary 复用：

```yaml
apiVersion: flagger.app/v1beta1
kind: MetricTemplate
metadata:
  name: http-error-rate-by-route
  namespace: apps
spec:
  provider:
    type: prometheus
    address: http://prometheus.monitoring:9090
  query: |
    sum(rate(
      http_requests_total{
        namespace="{{ namespace }}",
        workload="{{ target }}",
        route="{{ variables.route }}",
        status=~"5.."
      }[{{ interval }}]
    )) /
    sum(rate(
      http_requests_total{
        namespace="{{ namespace }}",
        workload="{{ target }}",
        route="{{ variables.route }}"
      }[{{ interval }}]
    )) * 100
```

Canary 里传参：

```yaml
analysis:
  metrics:
    - name: "error rate (/api/v1/users)"
      templateRef:
        name: http-error-rate-by-route
      thresholdRange:
        max: 1
      interval: 1m
      templateVariables:
        route: /api/v1/users
```

## 11. Webhook 钩子实战

### 11.1 钩子类型速查

| 类型 | 时机 | 用途 |
|------|------|------|
| `confirm-rollout` | analysis 开始前 | 人工审批 / 发布窗口判断 |
| `pre-rollout` | analysis 第一次 interval 前 | 冒烟测试 / 数据库 migrate |
| `rollout` | 每次 interval | 负载测试 / smoke test |
| `confirm-traffic-increase` | 每次 stepWeight 前 | 人工控制切流节奏 |
| `confirm-promotion` | analysis 结束、promote 前 | 人工确认 promote |
| `post-rollout` | promote 完成后 | 通知 / 清理 |
| `rollback` | 失败回滚时 | 通知 / 审计 |
| `event` | 状态变化 | 外部监控 |

### 11.2 调 flagger-loadtester 跑压测

`flagger-loadtester` 暴露一个 HTTP API，接收 JSON 请求，在容器内跑命令。内置 `hey`、`wrk`、`ghz`、`bombardier` 等工具。

```yaml
webhooks:
  - name: load-test-http
    type: rollout
    url: http://flagger-loadtester.flagger-system/
    timeout: 5s
    metadata:
      cmd: "hey -z 1m -q 50 -c 10 http://frontend-api-canary.apps/"
  - name: load-test-grpc
    type: rollout
    url: http://flagger-loadtester.flagger-system/
    timeout: 5s
    metadata:
      cmd: "ghz --insecure --proto /tmp/app.proto --call app.Service/Get -d '{}' -c 5 -n 1000 frontend-api-canary.apps:8080"
```

### 11.3 调外部 webhook 做业务 smoke test

假设你有个内部的回归测试服务 `qa-smoke.example.com`，接收 `{service, version}` 参数然后跑一套用例。接法：

```yaml
webhooks:
  - name: smoke-test
    type: pre-rollout
    url: https://qa-smoke.example.com/run
    timeout: 5m
    metadata:
      service: frontend-api
      suite: critical-path
```

回归服务返回非 2xx 表示失败，Flagger 会判定 pre-rollout 失败，直接取消本次发布。

### 11.4 人工审批门禁

```yaml
webhooks:
  - name: manual-gate
    type: confirm-rollout
    url: http://flagger-loadtester.flagger-system/gate/check
    timeout: 1h
```

这个 URL 对应 loadtester 的 `gate` API。默认状态是 open，如果要求发布前人工确认，运维先调：

```bash
curl -X POST http://flagger-loadtester.flagger-system/gate/close
```

Flagger 在 confirm-rollout 阶段会卡住。确认可以发了再：

```bash
curl -X POST http://flagger-loadtester.flagger-system/gate/open
```

## 12. 与 Argo Rollouts 深度对比

这是选型最常问的问题。两者都能做渐进式交付，但设计哲学不一样。

### 12.1 架构差异

**Argo Rollouts**：引入新的 `Rollout` CRD 替代 `Deployment`。你原来的 `Deployment` 要改成 `Rollout`，因为 Rollout 内嵌了发布策略字段（`strategy.canary.steps`）。策略是资源本身的一部分。

**Flagger**：不改 `Deployment`，外挂一个 `Canary` CR 指向 Deployment。Deployment 仍然是 Deployment，可以脱离 Flagger 独立工作。

这个差异的影响非常大：

- Argo Rollouts 的方式对现有系统侵入性强。Helm chart / Kustomize / Operator 都得改，把 Deployment 换成 Rollout。
- Flagger 的方式是叠加的，关掉 Flagger 服务还是服务，不受影响。

### 12.2 Mesh 支持

| Provider | Flagger | Argo Rollouts |
|----------|---------|---------------|
| Istio | 原生 | 原生 |
| Linkerd | 原生 | 原生 |
| NGINX Ingress | 原生 | 原生 |
| AWS App Mesh | 原生 | 原生 |
| Contour | 原生 | 需插件 |
| Gloo | 原生 | 原生 |
| Traefik | 原生 | 原生 |
| SMI | 原生 | 原生 |
| Gateway API | 原生 | 原生（1.6+） |
| Kuma | 原生 | 社区插件 |

两者目前支持都不错，但 Flagger 多年来始终以 mesh-agnostic 为卖点，覆盖略广一点。

### 12.3 分析机制

Flagger：`MetricTemplate` 是集群范围的资源，`Canary` 引用 template 填参。逻辑：**template 是模板，canary 填参**。

Argo Rollouts：`AnalysisTemplate` / `ClusterAnalysisTemplate` 定义分析模板，`Rollout` 在特定 step 启动一个 `AnalysisRun`。分析是一个独立的生命周期对象，结果可以查询、追溯。逻辑：**analysis 是一次运行，有独立的对象**。

Rollouts 的 AnalysisRun 独立对象设计，好处是每次分析可审计、可重跑、可和 Rollout 解耦。Flagger 的 MetricTemplate 更轻量，但分析结果不是独立资源，只能从 Canary status 看。

### 12.4 发布策略表达能力

Argo Rollouts 的 canary steps 可以用 DSL 自由编排，比如：

```yaml
steps:
  - setWeight: 5
  - pause: {duration: 2m}
  - setWeight: 20
  - pause: {}            # 无限停留，等人推进
  - analysis:
      templates:
        - templateName: success-rate
  - setWeight: 50
  - pause: {duration: 5m}
```

可以交叉混用 pause / setWeight / analysis / experiment / setHeaderRoute，非常灵活。

Flagger 的 canary 策略表达能力偏"规则化"：`stepWeight + maxWeight + interval + threshold` 四个旋钮，均匀推进。要做非均匀步长、中间加暂停，需要组合 webhook 和 gate。

如果你的发布流程复杂（比如 5% → 人工确认 → 20% → 30 分钟观察 → 50% → A/B 实验），Rollouts 更自然。如果你的流程是统一的、标准化的，Flagger 更简洁。

### 12.5 A/B / Experiment

Rollouts 有独立的 `Experiment` CRD，可以启动"实验"——临时拉起一套带特定 label 的 Pod 接流量跑一段时间然后销毁。非常适合 shadow / dark launch。

Flagger 通过 A/B 模式的 `match` 做类似的事，但没有独立 Experiment 对象的生命周期。

### 12.6 选型建议

- **如果已经用了 Flux 或 Weave 生态** → 选 Flagger，一家人无缝衔接。
- **如果已经用了 Argo CD** → Rollouts 集成更紧密（比如 Argo CD UI 可以直接展示 Rollout 状态条），但 Flagger 也完全可用。
- **不想改 Deployment** → Flagger。
- **要做复杂的发布编排（手动 gate + 多阶段）** → Rollouts 的 steps DSL 表达力更强。
- **mesh/ingress 种类多** → Flagger provider 覆盖稍广。
- **需要独立的 Experiment / AnalysisRun 审计对象** → Rollouts。
- **团队规模小、追求标准化** → Flagger（配置量少）。
- **业务差异大、需要每个服务独立定制发布流程** → Rollouts。

我的经验结论：**小规模 / 多服务 / 流程标准化的团队用 Flagger；大规模 / 少量核心服务 / 每个服务发布流程定制化的团队用 Rollouts**。

## 13. Istio 集成细节

### 13.1 自动生成的 VirtualService

apply 上面 6.3 的 Canary 之后，Flagger 会生成类似这样的 VirtualService：

```yaml
apiVersion: networking.istio.io/v1alpha3
kind: VirtualService
metadata:
  name: frontend-api
  namespace: apps
  ownerReferences:
    - apiVersion: flagger.app/v1beta1
      kind: Canary
      name: frontend-api
spec:
  hosts:
    - api.example.com
    - frontend-api
  gateways:
    - public-gateway.istio-system.svc.cluster.local
    - mesh
  http:
    - retries:
        attempts: 3
        perTryTimeout: 2s
        retryOn: gateway-error,connect-failure,refused-stream
      route:
        - destination:
            host: frontend-api-primary
          weight: 100
        - destination:
            host: frontend-api-canary
          weight: 0
```

analysis 推进时 Flagger 只改这两个 weight，不动其余字段。

### 13.2 DestinationRule

```yaml
apiVersion: networking.istio.io/v1alpha3
kind: DestinationRule
metadata:
  name: frontend-api-primary
  namespace: apps
spec:
  host: frontend-api-primary
---
apiVersion: networking.istio.io/v1alpha3
kind: DestinationRule
metadata:
  name: frontend-api-canary
  namespace: apps
spec:
  host: frontend-api-canary
```

两个 DR 分别管 primary / canary 的连接池、熔断、mTLS。如果你在 Canary 的 `service.trafficPolicy` 里配置了连接池，会写到这两个 DR 里。

### 13.3 流量流向图

```
client → Ingress Gateway
         → VirtualService(frontend-api)
             ├─ weight=90 → DR(primary) → Service(primary) → Pod(primary)
             └─ weight=10 → DR(canary)  → Service(canary)  → Pod(canary)
```

Flagger 每分钟把 10 递增到 20 → 30 → 40 → 50，同时查 Prometheus 的 `istio_requests_total` 确认错误率。

### 13.4 Istio Sidecar 注入

命名空间要打 `istio-injection=enabled`，否则 Pod 没有 sidecar，istio_requests_total 不会有数据，Flagger 的指标查询始终为空，analysis 会卡死。

```bash
kubectl label ns apps istio-injection=enabled
```

## 14. NGINX Ingress 集成

没有 service mesh 的集群也可以做 canary。NGINX Ingress controller 原生支持 canary annotation（`nginx.ingress.kubernetes.io/canary`），Flagger 利用这个能力实现切流。

### 14.1 Canary CR

```yaml
apiVersion: flagger.app/v1beta1
kind: Canary
metadata:
  name: frontend-api
  namespace: apps
spec:
  provider: nginx
  targetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: frontend-api
  ingressRef:
    apiVersion: networking.k8s.io/v1
    kind: Ingress
    name: frontend-api
  autoscalerRef:
    apiVersion: autoscaling/v2
    kind: HorizontalPodAutoscaler
    name: frontend-api
  progressDeadlineSeconds: 900
  service:
    port: 80
    targetPort: 8080
  analysis:
    interval: 30s
    threshold: 5
    maxWeight: 50
    stepWeight: 10
    metrics:
      - name: request-success-rate
        thresholdRange:
          min: 99
        interval: 1m
      - name: request-duration
        thresholdRange:
          max: 500
        interval: 1m
    webhooks:
      - name: load-test
        type: rollout
        url: http://flagger-loadtester.flagger-system/
        metadata:
          cmd: "hey -z 30s -q 20 -c 2 -host api.example.com http://ingress-nginx-controller.ingress-nginx/"
```

### 14.2 对应的 Ingress

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: frontend-api
  namespace: apps
  labels:
    app: frontend-api
  annotations:
    kubernetes.io/ingress.class: nginx
    nginx.ingress.kubernetes.io/ssl-redirect: "true"
spec:
  rules:
    - host: api.example.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: frontend-api
                port:
                  number: 80
```

Flagger 会基于这个 Ingress 自动复制一个 `frontend-api-canary` Ingress，带：

```yaml
metadata:
  annotations:
    nginx.ingress.kubernetes.io/canary: "true"
    nginx.ingress.kubernetes.io/canary-weight: "10"
```

analysis 推进时只改 `canary-weight` 这个 annotation 的值，NGINX Ingress controller 自动重载配置。

### 14.3 NGINX Ingress 的限制

- **session affinity 不兼容**：如果你的 Ingress 开了 `nginx.ingress.kubernetes.io/affinity: cookie`，NGINX 会把用户固定到某个后端，canary 权重就失效了。要么关掉 affinity，要么用 mesh provider。
- **指标来源是 NGINX**：Flagger 查 `nginx_ingress_controller_requests`，要确保 NGINX controller 开了 Prometheus metrics。
- **canary-weight 粒度 1%**：步长最小 1%，实际 NGINX 的分流是基于随机数，不是精确计数，样本小的时候会有偏差。

## 15. Gateway API 集成

Gateway API 是 Kubernetes 官方在推的下一代 Ingress 规范。Flagger 从 1.23 开始原生支持。

### 15.1 前置

先装一个 Gateway API 实现（Istio / Contour / Envoy Gateway / Cilium / Traefik / Kong 等都有），并创建一个 `Gateway` 资源。

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
metadata:
  name: public
  namespace: gateway-system
spec:
  gatewayClassName: envoy
  listeners:
    - name: http
      port: 80
      protocol: HTTP
      allowedRoutes:
        namespaces:
          from: All
```

### 15.2 Canary CR

```yaml
apiVersion: flagger.app/v1beta1
kind: Canary
metadata:
  name: frontend-api
  namespace: apps
spec:
  provider: gatewayapi
  targetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: frontend-api
  autoscalerRef:
    apiVersion: autoscaling/v2
    kind: HorizontalPodAutoscaler
    name: frontend-api
  progressDeadlineSeconds: 900
  service:
    port: 80
    targetPort: 8080
    hosts:
      - api.example.com
    gatewayRefs:
      - name: public
        namespace: gateway-system
  analysis:
    interval: 1m
    threshold: 5
    maxWeight: 50
    stepWeight: 10
    metrics:
      - name: request-success-rate
        thresholdRange:
          min: 99
        interval: 1m
      - name: request-duration
        thresholdRange:
          max: 500
        interval: 1m
    webhooks:
      - name: load-test
        type: rollout
        url: http://flagger-loadtester.flagger-system/
        metadata:
          cmd: "hey -z 1m -q 20 -c 2 -host api.example.com http://envoy-gateway.gateway-system/"
```

### 15.3 自动生成的 HTTPRoute

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: frontend-api
  namespace: apps
  ownerReferences:
    - apiVersion: flagger.app/v1beta1
      kind: Canary
      name: frontend-api
spec:
  parentRefs:
    - name: public
      namespace: gateway-system
  hostnames:
    - api.example.com
  rules:
    - matches:
        - path:
            type: PathPrefix
            value: /
      backendRefs:
        - name: frontend-api-primary
          port: 80
          weight: 100
        - name: frontend-api-canary
          port: 80
          weight: 0
```

Flagger 改 `weight` 字段实现切流。

### 15.4 Gateway API 指标来源

Gateway API 的 Prometheus 指标取决于底层实现：

- Istio 的 Gateway API 实现 → 还是 `istio_requests_total`
- Envoy Gateway → `envoy_http_downstream_rq_total`
- Contour → `contour_httpproxy_total`

Flagger 默认的 MetricTemplate 在 Gateway API 模式下会用一个统一的模板，基于 `istio_requests_total`（假设底层是 Istio 的 ingress impl）。如果你用 Envoy Gateway 或其他实现，要自己写 MetricTemplate。

## 16. 从滚动更新迁移到 Flagger：三步走

大多数团队是从"kubectl rolling update + 手动观察"迁到 Flagger。我的推荐路径：

### 16.1 Step 1：灰度启用（影子模式）

先在一个非核心服务上启用 Flagger，设置 `skipAnalysis: true` 或把 threshold 调得很宽松，让 Flagger 走完整套流程但不阻塞发布。目的：

- 验证 primary/canary 资源创建正常
- 验证 Prometheus 连通性
- 验证 webhook 可达
- 让运维熟悉 Canary status 的观察方式

建议持续 1-2 周，期间发布 5 次以上。

### 16.2 Step 2：全量接入

扩展到全部服务。这一阶段要做的事：

- 统一 Canary template，通过 Helm / Kustomize 生成
- 指标门禁先松后紧：第一周 `min: 90`，观察没有误杀就收紧到 `min: 99`
- 接入通知（Slack/钉钉），让发布状态可视化
- 准备手动 `kubectl edit` 强制 promote / abort 的运维手册

建议持续 2-4 周。

### 16.3 Step 3：建立发布门禁

把 Flagger 纳入 CI/CD pipeline，而不是只作为运维工具：

- Pipeline push 镜像后，kubectl apply 新的 Deployment
- 外部流程监听 Canary 的 `.status.phase`，直到 `Succeeded` 或 `Failed`
- 失败自动通知对应服务 owner
- 建立发布窗口限制（例如晚高峰不能发）通过 `confirm-rollout` webhook 实现

这一步的关键是把"发布"从运维职责转变为开发职责，运维只负责基础设施。

### 16.4 常见阻力

- **开发抵触**：说"我本来 kubectl apply 30 秒搞定，现在要 10 分钟才能看到效果"。回应：10 分钟是指标检查时间，节省的是事后回滚的 2 小时。
- **测试环境不愿意上**：测试环境指标稀疏，容易指标查询为空导致卡死。解决办法：测试环境用 `skipAnalysis: true`，只用 Flagger 做流量切分不做门禁。
- **HPA 和 Flagger 冲突**：没有在 Canary 里声明 `autoscalerRef` 导致 primary 没有 HPA，放量时 primary 扛不住。必须声明。

## 17. 监控

### 17.1 Flagger 自身指标

Flagger controller 在 8080 端口暴露 Prometheus 指标。关键指标：

- `flagger_canary_total{namespace, name}`：每个 Canary 的存在计数（用作 service discovery）
- `flagger_canary_status{namespace, name, phase}`：当前 phase（Initialized/Progressing/Succeeded/Failed 等）
- `flagger_canary_weight{namespace, name}`：当前 canary 权重
- `flagger_canary_duration_seconds`：发布耗时
- `flagger_canary_metric_analysis{namespace, name, metric}`：单个指标的最近值

### 17.2 Grafana Dashboard

Flagger 官方提供了一个 Grafana dashboard，ID 是 `10466`（可在 grafana.com/dashboards 搜 "Flagger"）。可以看到每个 Canary 当前权重、状态、成功率、延迟趋势。

### 17.3 告警项

建议的告警规则：

```yaml
groups:
  - name: flagger
    rules:
      - alert: FlaggerCanaryFailed
        expr: flagger_canary_status{phase="Failed"} == 1
        for: 1m
        annotations:
          summary: "Canary {{ $labels.namespace }}/{{ $labels.name }} failed"
      - alert: FlaggerCanaryStuck
        expr: flagger_canary_status{phase="Progressing"} == 1
        for: 30m
        annotations:
          summary: "Canary {{ $labels.namespace }}/{{ $labels.name }} has been progressing for 30m"
      - alert: FlaggerControllerDown
        expr: up{job="flagger"} == 0
        for: 5m
```

### 17.4 日志关键字

Flagger 的日志是结构化的 JSON。关键字：

- `"Starting canary analysis"`：开始一次发布
- `"Advance canary weight"`：推进了一步
- `"Halt advancement"`：某次指标检查失败（不代表整体失败）
- `"Rolling back"`：整体失败，回滚中
- `"Promotion completed"`：发布成功

排障先看 controller 日志：

```bash
kubectl -n flagger-system logs deploy/flagger --tail 200 | grep frontend-api
```

## 18. 坑位合集

### 18.1 primary 初始化卡住

症状：`kubectl get canary` 一直显示 `Initializing`。

原因和解法：

- **原 Deployment 没有 readinessProbe**：Flagger 要求必须有，否则等不到 ready。加上。
- **原 Deployment 副本数是 0**：Flagger 拒绝初始化。先 scale 到 ≥1。
- **Pod 没有 istio sidecar**（Istio 模式下）：ns 没打 injection label，primary Pod 起来了但 Service 走不通。
- **网络策略（NetworkPolicy）阻断**：primary 和 canary Service 之间互通被阻断。检查 NP。

### 18.2 指标查询为空导致发布僵死

症状：canary 权重卡在 10%，controller 日志看到 `Halt advancement: no values found for metric request-success-rate`。

原因：canary 副本刚起来，Prometheus 还没抓到数据；或者压根没流量到 canary。

解法：

- 加 `load-test` webhook 主动打流量
- 调大 `analysis.interval` 到 2m，让 Prometheus 有更多采样
- 确认 Prometheus 已经抓到 `<name>-canary` 的 target
- 在 MetricTemplate 的 PromQL 里用 `or vector(100)` 给默认值：

```promql
(sum(rate(...)) / sum(rate(...)) * 100) or vector(100)
```

最后一招要慎用，本质是"没有数据就当成 100% 成功"，会掩盖问题。

### 18.3 流量权重与 HPA 冲突

症状：canary 到 50%，突发流量，primary 的 Pod 数没有增加，延迟飙升。

原因：没有声明 `autoscalerRef`，Flagger 没给 primary 复制 HPA，primary 只有初始副本数。

解法：Canary CR 里加 `autoscalerRef`，重新 apply。Flagger 会创建 `<name>-primary` HPA。

### 18.4 session affinity 与 canary 权重冲突

症状：NGINX Ingress 模式下，canary-weight 设了 20%，但 canary Pod 收到的流量远小于 20%。

原因：Ingress 开了 cookie-based session affinity，老用户全部被粘到 primary，只有新连接才按权重分。

解法：

- 关闭 affinity（影响功能）
- 换成 mesh provider（Istio 的权重切流对 affinity 免疫）
- 或者把 canary 的 affinity 也关掉，只保留读路径

### 18.5 Gateway API backendRefs 顺序

症状：Gateway API 模式下，Flagger 推进权重但流量完全没变化。

原因：某些 Gateway API 实现（早期 Contour）对 backendRefs 顺序敏感，权重改了但实现不 reload。

解法：升级实现版本，或在 bug 修复前回退到 Istio 模式。

### 18.6 Deployment 的 revisionHistoryLimit 设太小

症状：发布失败回滚时，primary 的老版本 ReplicaSet 已经被清理，找不到可回滚的 image。

解法：Deployment 的 `revisionHistoryLimit` 不要低于 10。

### 18.7 webhook 超时设置过短

症状：pre-rollout webhook 设了 30s，但 smoke test 要 2 分钟跑完，每次都失败。

解法：timeout 设成实际耗时的 1.5 倍。注意 webhook timeout 最长 1 小时。

### 18.8 MetricTemplate 写错导致所有发布失败

症状：新加了一个 MetricTemplate，从那以后所有发布都卡在 Progressing。

原因：PromQL 语法错误或指标不存在，每次查询返回错误，Flagger 把"查询出错"当作"指标失败"。

解法：先单独 `curl` Prometheus API 验证 PromQL，再放进 MetricTemplate。可以用：

```bash
curl -G http://prometheus.monitoring:9090/api/v1/query \
  --data-urlencode 'query=sum(rate(istio_requests_total{destination_workload="frontend-api"}[1m]))'
```

### 18.9 多 Canary 共享同一个 Deployment

症状：两个 Canary 都 targetRef 到同一个 Deployment，行为异常。

原因：不允许。一个 Deployment 只能被一个 Canary 管理。

解法：每个 Deployment 独立一个 Canary。

### 18.10 Canary 删除后资源没清理

症状：`kubectl delete canary frontend-api` 后，primary Deployment、Service、VirtualService 都还在。

原因：这是设计如此。删除 Canary 不会删 primary，避免误操作导致服务中断。

解法：确实要清理，手动删：

```bash
kubectl -n apps delete deploy frontend-api-primary
kubectl -n apps delete svc frontend-api-primary frontend-api-canary
kubectl -n apps delete vs frontend-api
kubectl -n apps delete dr frontend-api-primary frontend-api-canary
```

## 19. 生产落地 Checklist

上生产前对照下面这个 checklist 过一遍：

### 19.1 基础设施

- [ ] Prometheus 有稳定的 endpoint，Flagger 可达
- [ ] Prometheus 抓取 mesh/ingress 的指标，确认 `istio_requests_total` 或等价物有数据
- [ ] Flagger 和 flagger-loadtester 都装好
- [ ] Slack/钉钉/Teams 通知渠道接通
- [ ] Grafana dashboard 导入完成

### 19.2 服务就绪

- [ ] 每个待接入服务都有健康的 readinessProbe 和 livenessProbe
- [ ] 每个服务都有 HPA，且 Canary CR 里声明了 `autoscalerRef`
- [ ] Deployment 的 `revisionHistoryLimit ≥ 10`
- [ ] 命名空间 istio-injection 已开启（Istio 模式）
- [ ] Service 的 port 命名符合 mesh 约定（`http-*` / `grpc-*`）

### 19.3 Canary 配置

- [ ] `progressDeadlineSeconds` 合理（= interval × stepCount × 1.5）
- [ ] `interval ≥ 1m`
- [ ] `threshold` 在 3-5 之间
- [ ] `maxWeight ≤ 50`（不是必须，但经验上够用）
- [ ] 至少两个 metric：成功率 + 延迟
- [ ] 业务关键服务增加业务 metric（错误码、下游依赖）
- [ ] `pre-rollout` webhook 跑 smoke test
- [ ] `rollout` webhook 跑 load test（对于低流量服务）
- [ ] 重要服务加 `confirm-rollout` / `confirm-promotion` 人工门禁

### 19.4 监控告警

- [ ] `FlaggerCanaryFailed` 告警
- [ ] `FlaggerCanaryStuck` 告警
- [ ] `FlaggerControllerDown` 告警
- [ ] 发布成功/失败通知到 release 频道
- [ ] 每个 Canary 都在 Grafana dashboard 可见

### 19.5 运维能力

- [ ] 团队知道如何看 `kubectl describe canary` 排障
- [ ] 团队知道如何强制 promote（`kubectl annotate canary xxx skipAnalysis=true` 或改 analysis 阈值）
- [ ] 团队知道如何手动 abort（降 stepWeight 到 0 或删 Canary）
- [ ] 有回滚演练记录
- [ ] 有灰度失败时的应急流程文档

### 19.6 迁移计划

- [ ] 先非核心后核心，分批接入
- [ ] 前 1-2 周指标阈值放宽，避免误杀
- [ ] 每批发布数量 ≥ 5 次再进入下一批
- [ ] 每周复盘：卡单原因、误杀原因、指标调整
- [ ] 最终目标：所有生产发布经过 Canary，人工发布作为 fallback

## 20. 写在最后

Flagger 只摊薄"发布动作本身"的风险。它管不了：

- **架构设计错误**：新功能从一开始设计就错，指标再好看也白搭。
- **需求错误**：产品要的东西本身有问题。
- **数据层变更**：DB schema、数据迁移它看不见。
- **跨服务事务**：多服务原子变更靠手工协调或 feature flag。

把边界认清之后，装上、用熟、纳入 CI/CD 就够了。我自己记它只用一句：**别信发布之前的测试，信发布之中的指标。** Canary、MetricTemplate、Webhook 都是为这句话服务的。

参考资料：

- 官方文档 https://docs.flagger.app
- CNCF Flagger 项目主页
- Istio traffic management 文档
- Kubernetes Gateway API 规范
- Argo Rollouts 官方文档（对比阅读）
