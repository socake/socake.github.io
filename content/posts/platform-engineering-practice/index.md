---
title: "平台工程实践：构建 Internal Developer Platform"
date: 2025-08-10T09:44:00+08:00
draft: false
tags: ["Platform Engineering", "IDP", "Backstage", "Kubernetes", "DevOps", "Golden Path"]
categories: ["Kubernetes"]
series: ["SRE 实战手册"]
description: "从零搭建 Internal Developer Platform：Backstage 服务目录、黄金路径设计、自服务脚手架，以及平台团队如何系统性降低开发团队的认知负担"
summary: "平台工程不是给 DevOps 换个名字，而是把基础设施能力产品化——让开发者像用 SaaS 一样消费平台能力。这篇文章记录我们团队从 0 到 MVP 的六个月实践，包括 Backstage 落地、黄金路径设计、以及用 DORA 指标验证平台价值。"
toc: true
math: false
diagram: false
keywords: ["平台工程", "IDP", "Backstage", "Golden Path", "Kustomize", "DORA 指标", "开发者体验"]
params:
  reading_time: true
---

加入现在这家公司时，我接手了一个让人头皮发麻的局面：12 个后端服务，每个服务的 CI/CD 流水线写法各不相同，有人用 Makefile、有人手写 Dockerfile、监控配置全靠口耳相传。每次来了新工程师，光是把本地环境跑起来就要折腾一天。这不是技术问题，是**认知负担（cognitive load）**问题。

平台工程（Platform Engineering）就是解这道题的。花了大半年时间，我们从一片混乱到有了一个基本可用的 IDP（Internal Developer Platform），这篇文章把这段经历完整梳理一遍。

## 平台工程 vs DevOps：别混淆这两个概念

我见过太多团队把 Platform Engineering 当成 DevOps 的升级版，其实它们解决的是不同层次的问题。

**DevOps** 是一种文化和实践，强调开发与运维的协作——Dev 要理解运维，Ops 要融入开发流程，核心是「你构建，你运行（You build it, you run it）」。

**Platform Engineering** 是把这套实践**产品化**：平台团队构建一套自助服务平台，业务开发团队作为用户消费平台能力，不需要深入理解底层基础设施细节。

用一个类比：DevOps 是教大家做饭，Platform Engineering 是开一家餐厅——菜单固定、流程标准化，厨师（业务团队）只管炒好自己那道菜。

| 维度 | DevOps | Platform Engineering |
|------|--------|---------------------|
| 关注点 | 文化与协作 | 产品化基础设施能力 |
| 主要受益者 | 开发+运维双方 | 业务开发团队 |
| 核心产出 | 流程改善 | 可自助的平台产品（IDP） |
| 成功指标 | 团队协作效率 | 开发者体验（DX）、DORA 指标 |
| 典型工具 | Jenkins、GitLab CI | Backstage、Port、Kratix |

## IDP 的核心组件

一个完整的 Internal Developer Platform 至少包含以下几个部分，我按照依赖关系列出来：

### 1. 开发者门户（Developer Portal）

这是 IDP 的入口，开发者在这里看到所有服务、文档、工具。目前业界主流是 Spotify 开源的 **Backstage**，我们也选了这个。

### 2. 服务目录（Service Catalog）

回答「我们有哪些服务，谁负责，文档在哪，依赖什么」。这是平台的基础数据层。

### 3. 黄金路径（Golden Path）

预设的最佳实践路径——标准化的项目模板、Dockerfile、CI 流水线、K8s 配置。开发者不需要从零设计，走黄金路径就是走最佳实践。

### 4. 脚手架（Scaffolding）

自动生成项目骨架的工具。输入服务名、语言、数据库类型，自动创建 Git 仓库、生成标准代码结构、配置好 CI 流水线。

### 5. 环境管理（Environment Management）

让开发者能自助申请、创建、销毁测试环境，不需要找运维开票。

### 6. CI/CD 模板库

预置的 CI 流水线模板，开发者只需引用，不需要重复造轮子。

## Backstage 落地实战

Backstage 是 Node.js 应用，我们把它部署在 K8s 上，使用 PostgreSQL 作为后端存储。

### 部署基础架构

```yaml
# backstage-deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: backstage
  namespace: platform
spec:
  replicas: 2
  selector:
    matchLabels:
      app: backstage
  template:
    metadata:
      labels:
        app: backstage
    spec:
      containers:
        - name: backstage
          image: registry.example.com/backstage:1.24.0
          ports:
            - containerPort: 7007
          env:
            - name: POSTGRES_HOST
              valueFrom:
                secretKeyRef:
                  name: backstage-secrets
                  key: postgres-host
            - name: POSTGRES_USER
              valueFrom:
                secretKeyRef:
                  name: backstage-secrets
                  key: postgres-user
            - name: POSTGRES_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: backstage-secrets
                  key: postgres-password
          resources:
            requests:
              memory: "512Mi"
              cpu: "250m"
            limits:
              memory: "1Gi"
              cpu: "500m"
          readinessProbe:
            httpGet:
              path: /healthcheck
              port: 7007
            initialDelaySeconds: 30
            periodSeconds: 10
```

### app-config.yaml 核心配置

```yaml
app:
  title: 内部开发者门户
  baseUrl: https://backstage.internal.example.com

backend:
  baseUrl: https://backstage.internal.example.com
  listen:
    port: 7007
  database:
    client: pg
    connection:
      host: ${POSTGRES_HOST}
      port: 5432
      user: ${POSTGRES_USER}
      password: ${POSTGRES_PASSWORD}
      database: backstage_plugin_catalog

# 集成 GitHub/GitLab
integrations:
  github:
    - host: github.com
      token: ${GITHUB_TOKEN}

# 服务目录发现规则
catalog:
  rules:
    - allow: [Component, API, Group, User, Resource, System, Domain]
  locations:
    # 自动扫描所有含 catalog-info.yaml 的仓库
    - type: github-discovery
      target: https://github.com/your-org/*/blob/main/catalog-info.yaml
    # 组织架构
    - type: url
      target: https://github.com/your-org/backstage-catalog/blob/main/org.yaml

# 技术文档
techdocs:
  builder: external
  generator:
    runIn: docker
  publisher:
    type: awsS3
    awsS3:
      bucketName: example-techdocs
      region: us-west-2
```

### 服务注册：catalog-info.yaml

每个服务仓库根目录放一个 `catalog-info.yaml`，这是服务进入目录的「注册表」：

```yaml
apiVersion: backstage.io/v1alpha1
kind: Component
metadata:
  name: order-service
  title: 订单服务
  description: 处理用户订单的核心服务，包含下单、支付、状态查询
  annotations:
    github.com/project-slug: your-org/order-service
    backstage.io/techdocs-ref: dir:.
    prometheus.io/alert-dashboard: "https://grafana.internal/d/order-service"
    argocd/app-name: order-service-prod
  tags:
    - golang
    - postgresql
    - kafka
  links:
    - url: https://grafana.internal/d/order-service
      title: 监控大盘
      icon: dashboard
    - url: https://runbook.internal/order-service
      title: 故障处理手册
      icon: docs
spec:
  type: service
  lifecycle: production
  owner: team-commerce
  system: e-commerce-platform
  dependsOn:
    - component:user-service
    - component:payment-service
    - resource:orders-db
  providesApis:
    - order-api
```

### 插件体系

Backstage 的核心价值在于插件生态。我们安装了以下插件：

```typescript
// packages/app/src/App.tsx 关键插件引入
import { ArgoCDPage } from '@backstage-community/plugin-argocd';
import { GrafanaPage } from '@backstage-community/plugin-grafana';
import { KubernetesPage } from '@backstage/plugin-kubernetes';
import { CostInsightsPage } from '@backstage/plugin-cost-insights';
import { TechRadarPage } from '@backstage-community/plugin-tech-radar';
```

对我们来说最有价值的三个插件：

1. **Kubernetes 插件**：直接在 Backstage 看服务的 Pod 状态、最近部署历史，不需要跑 kubectl
2. **ArgoCD 插件**：显示同步状态、最后一次部署的 commit
3. **Cost Insights 插件**：按团队看云成本，平台团队用这个数据推动各团队做资源优化

## 黄金路径设计

黄金路径（Golden Path）是平台工程最核心的产出。我们的黄金路径覆盖三个技术栈：Go、Python（FastAPI）、Node.js。

### 标准化 Helm Chart 结构

我们没有让每个服务自己写 Helm Chart，而是维护一个「公司级基础 Chart」，服务只需提供 `values.yaml`：

```
charts/
├── base-service/           # 公司基础 Chart
│   ├── Chart.yaml
│   ├── templates/
│   │   ├── deployment.yaml    # 标准 Deployment，含 preStop/readiness/liveness
│   │   ├── service.yaml
│   │   ├── hpa.yaml           # 自动扩缩容
│   │   ├── pdb.yaml           # Pod Disruption Budget
│   │   ├── servicemonitor.yaml # Prometheus 抓取
│   │   └── _helpers.tpl
│   └── values.yaml         # 默认值（生产级配置）
└── services/
    ├── order-service/
    │   └── values.yaml     # 只写差异
    └── user-service/
        └── values.yaml
```

基础 Chart 的 `values.yaml` 预设了生产级默认值：

```yaml
# base-service/values.yaml
replicaCount: 2

image:
  pullPolicy: IfNotPresent

resources:
  requests:
    cpu: 100m
    memory: 128Mi
  limits:
    cpu: 500m
    memory: 512Mi

# 默认启用 PDB，最少保留 1 个副本
podDisruptionBudget:
  enabled: true
  minAvailable: 1

# 默认 HPA 配置
autoscaling:
  enabled: true
  minReplicas: 2
  maxReplicas: 10
  targetCPUUtilizationPercentage: 70

# 标准健康检查
probes:
  readiness:
    httpGet:
      path: /healthz
      port: http
    initialDelaySeconds: 10
    periodSeconds: 5
    failureThreshold: 3
  liveness:
    httpGet:
      path: /healthz
      port: http
    initialDelaySeconds: 30
    periodSeconds: 10
    failureThreshold: 3

# 优雅关闭
lifecycle:
  preStop:
    exec:
      command: ["/bin/sh", "-c", "sleep 5"]

terminationGracePeriodSeconds: 60

# 标准安全上下文
securityContext:
  runAsNonRoot: true
  runAsUser: 1000
  readOnlyRootFilesystem: true

# Prometheus 监控
serviceMonitor:
  enabled: true
  interval: 30s
  path: /metrics
```

服务的 `values.yaml` 只写真正有差异的部分：

```yaml
# services/order-service/values.yaml
image:
  repository: registry.example.com/order-service
  tag: "v1.2.3"

service:
  port: 8080

env:
  - name: DB_DSN
    valueFrom:
      secretKeyRef:
        name: order-service-secrets
        key: db-dsn
  - name: KAFKA_BROKERS
    value: "kafka-0.kafka:9092,kafka-1.kafka:9092"

resources:
  requests:
    memory: 256Mi
  limits:
    memory: 1Gi

autoscaling:
  maxReplicas: 20  # 订单服务流量大，放宽上限
```

### Kustomize 多环境模板

对于不用 Helm 的团队，我们提供 Kustomize 模板：

```
kustomize-templates/
├── base/
│   ├── kustomization.yaml
│   ├── deployment.yaml
│   └── service.yaml
└── overlays/
    ├── qa/
    │   ├── kustomization.yaml
    │   └── patch-replicas.yaml   # replicas: 1
    ├── staging/
    │   ├── kustomization.yaml
    │   └── patch-resources.yaml  # 缩减资源规格
    └── prod/
        ├── kustomization.yaml
        └── patch-hpa.yaml        # 生产 HPA 配置
```

## 脚手架：自动生成新服务

这是让开发者体验提升最明显的功能。通过 Backstage 的 Software Templates，开发者填写表单，5 分钟内拿到一个完整可运行的新服务。

```yaml
# templates/go-service-template.yaml
apiVersion: scaffolder.backstage.io/v1beta3
kind: Template
metadata:
  name: go-microservice
  title: Go 微服务模板
  description: 创建一个包含完整工程配置的 Go 微服务
  tags:
    - golang
    - recommended
spec:
  owner: platform-team
  type: service

  parameters:
    - title: 基本信息
      required:
        - name
        - description
        - owner
      properties:
        name:
          title: 服务名称
          type: string
          description: 小写字母+连字符，例如 order-service
          pattern: '^[a-z][a-z0-9-]*$'
        description:
          title: 服务描述
          type: string
        owner:
          title: 负责团队
          type: string
          ui:field: OwnerPicker
          ui:options:
            allowedKinds: [Group]
    - title: 技术配置
      properties:
        database:
          title: 是否需要数据库
          type: string
          enum: [none, postgresql, mysql]
          default: none
        enableKafka:
          title: 是否接入 Kafka
          type: boolean
          default: false

  steps:
    - id: fetch-base
      name: 生成项目骨架
      action: fetch:template
      input:
        url: ./skeleton
        values:
          name: ${{ parameters.name }}
          description: ${{ parameters.description }}
          owner: ${{ parameters.owner }}
          database: ${{ parameters.database }}
          enableKafka: ${{ parameters.enableKafka }}

    - id: create-repo
      name: 创建 Git 仓库
      action: github:repo:create
      input:
        repoUrl: github.com?owner=your-org&repo=${{ parameters.name }}

    - id: publish
      name: 推送代码
      action: publish:github
      input:
        repoUrl: github.com?owner=your-org&repo=${{ parameters.name }}
        defaultBranch: main

    - id: create-argocd-app
      name: 注册 ArgoCD 应用
      action: argocd:create-resources
      input:
        appName: ${{ parameters.name }}-qa
        namespace: ${{ parameters.name }}
        project: default
        repoUrl: https://github.com/your-org/${{ parameters.name }}
        path: k8s/overlays/qa

  output:
    links:
      - title: 代码仓库
        url: ${{ steps['create-repo'].output.remoteUrl }}
      - title: Backstage 服务页面
        url: ${{ steps['register'].output.catalogInfoUrl }}
```

## 平台团队如何减少认知负担

Spotify 工程师 Matthew Skelton 在《Team Topologies》里提出了一个概念：**认知负担（Cognitive Load）是限制团队效能的核心因素**。平台工程就是系统性地把认知负担从业务团队转移到平台团队。

我们做了几件具体的事：

**1. 「可以工作」是最低标准，「不需要思考」才是目标**

以前让开发者配置监控，他们要学 Prometheus 的 scrape 配置、ServiceMonitor CRD、Grafana 面板。现在他们只需要在 `values.yaml` 里加一行：

```yaml
serviceMonitor:
  enabled: true
```

剩下的——创建 ServiceMonitor、导入预设的 Grafana 面板、配置关键告警——全部由平台自动完成。

**2. 错误路径比正确路径更重要**

我们不只提供黄金路径，还要确保「错误路径走不通」。比如：
- CI 流水线强制通过安全扫描（Trivy）才能发布
- `values.yaml` 中 `resources.limits` 不填则流水线报错
- 没有 `readinessProbe` 的 Deployment 会被 Admission Webhook 拦截

**3. 文档和代码放在一起**

我们强制要求每个服务仓库包含 `docs/` 目录，Backstage TechDocs 自动渲染成网页。文档不在 Confluence 里孤立存在，而是和代码一起经历 review、版本控制。

## DORA 指标与平台工程的关系

DORA（DevOps Research and Assessment）四项指标是验证平台投入是否有效的标尺：

| 指标 | 含义 | 平台工程的影响 |
|------|------|--------------|
| 部署频率（Deployment Frequency） | 多久部署一次 | 标准化流水线降低发布阻力 |
| 变更前置时间（Lead Time for Changes） | 代码从提交到生产需要多久 | 自动化减少等待时间 |
| 变更失败率（Change Failure Rate） | 发布导致故障的比例 | 黄金路径内置最佳实践，减少配置错误 |
| 恢复时间（Time to Restore Service） | 故障后多久恢复 | 标准化可观测性，缩短排查时间 |

我们在平台 MVP 上线 3 个月后做了一次测量：

- 部署频率：从 2次/周 提升到 5次/周（新服务脚手架消除了发布前的手工配置）
- 变更前置时间：从平均 3 天降到 6 小时（流水线全自动，不需要等运维介入）
- 新服务从创建到第一次部署：从 2 天降到 30 分钟

## 典型落地路径：6 个月从零到 MVP

**Month 1-2：清点现状，建服务目录**

不要急着上工具，先做「服务地图」——把所有服务、负责人、技术栈、依赖关系整理清楚。这个过程本身就有价值，很多团队对自己系统的全貌都是模糊的。

部署 Backstage，先只开服务目录功能，让各团队自己填写 `catalog-info.yaml`。

**Month 3：黄金路径 v1**

选一个最典型的技术栈（比如 Go + PostgreSQL），设计标准化模板，在 1-2 个新项目上试跑，收集反馈。这时候不要追求完美，够用就行。

**Month 4：CI/CD 模板化**

把共用的 CI 流水线逻辑抽成模板，现有服务逐步迁移。重点是**不要一次性大迁移**，按团队分批，给每个团队两周时间消化。

**Month 5：脚手架上线**

在 Backstage 中添加 Software Templates，让新服务创建走标准化流程。

**Month 6：可观测性标准化 + 开始度量**

把监控、日志、告警的配置标准化，同时开始收集 DORA 指标，让数据说话。

## 常见陷阱

**陷阱一：平台太复杂，开发者不愿意用**

我见过有团队的 IDP 设计了二十几个参数表单，最后没人用，大家还是自己写 YAML。黄金路径要足够「黄金」——对 80% 的场景开箱即用，不需要额外配置。

**陷阱二：平台团队和业务团队脱节**

平台团队很容易陷入「我觉得这个功能很重要」的自嗨，而不是解决业务团队真正的痛点。我们的做法是每两周和 2-3 个业务工程师做用户访谈，把反馈优先级排在新功能之上。

**陷阱三：文档缺失**

再好的工具，没有文档就等于零。我们的规则是：任何新平台功能，必须同时提供：一个 5 分钟的快速上手示例、一个常见问题（FAQ）页面、一个对应的 TechDocs 页面。

**陷阱四：强制迁移**

平台工具如果是强制的，会产生抵触。我们选择「激励迁移」：走黄金路径的服务可以享受更快的部署审批、自动的安全合规证明等特权，而不是强制要求。

## 写在最后

Backstage 只是一个骨架，真正有用的是你往里填什么——服务目录更不更新得动、黄金路径黄不黄金、脚手架是真方便还是又一个祖传 YAML 生成器。工具选型只占 20%，剩下 80% 是平台团队跟业务团队磨合出来的东西。六个月能把框架跑起来已经算快，但真正做成团队信任的 IDP，一两年都不算多。
