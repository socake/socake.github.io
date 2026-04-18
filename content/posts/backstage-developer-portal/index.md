---
title: "Backstage 开发者门户实战：构建内部开发者平台"
date: 2025-09-12T10:00:00+08:00
draft: false
tags: ["Backstage", "平台工程", "开发者体验", "DevOps", "IDP"]
categories: ["DevOps"]
series: "DevOps 工程师成长路径"
description: "从零开始构建基于 Backstage 的内部开发者平台（IDP），覆盖 Software Catalog、脚手架模板、K8s 插件、TechDocs、自定义 Plugin 开发，以及与 ArgoCD/Grafana 的集成方案"
summary: "当团队规模超过 50 人，服务数量超过 100 个，「配置漂移」和「信息孤岛」就成了真实痛点。Backstage 是解决这个问题的平台工程利器。本文从部署到定制，完整拆解如何用 Backstage 构建真正能用起来的内部开发者平台。"
toc: true
math: false
diagram: false
keywords: ["Backstage", "IDP", "Software Catalog", "平台工程", "TechDocs", "Scaffolder", "开发者体验", "Plugin开发", "ArgoCD集成"]
params:
  reading_time: true
---

团队 10 人的时候，口耳相传就够了——新人入职，老人带一天就能上手。团队 100 人的时候，口耳相传是灾难：谁知道 payment-service 用的是哪个 Kafka topic？前端该用哪个 API Gateway 地址？新建一个 Go 微服务需要配哪些 CI/CD 变量？这些知识分散在 Confluence、Slack、脑子里，每个人都在重复解答同样的问题。

Backstage 是 Spotify 开源的内部开发者平台（IDP）框架，干的就是把这些零散的知识和工具塞进一个入口。下面直接上落地过程。

## 为什么需要 IDP

### 配置漂移与知识孤岛

以下场景是否熟悉？

- 生产环境某个服务的 Deployment 配置跟 Git 仓库不一致，没人知道是谁改的
- 新建服务时，每个团队的 CI/CD 流水线配置各不相同，有的忘了加健康检查，有的忘了配告警
- 某个关键服务的文档上次更新是两年前，实际行为早已改变，新人踩坑
- 要找某个 API 的 owner，需要问一圈才能找到负责人

这些问题的本质是**缺乏单一可信信息源**（Single Source of Truth）。Backstage 的 Software Catalog 解决信息孤岛，Scaffolder 模板解决配置漂移，TechDocs 解决文档腐化。

### IDP 带来的可量化价值

根据 DORA 2023 报告，使用内部开发者平台的团队相比未使用的团队：
- 部署频率高 2.1 倍
- 变更失败率低 22%
- 新服务从 0 到上线时间缩短 60%+

Spotify 在推广 Backstage 内部使用后，开发者调研满意度提升了 35%，新人 onboarding 时间从 2 周缩短到 3 天。

---

## Backstage 核心概念

### Software Catalog

Catalog 是 Backstage 的核心，存储所有"软件实体"（Entity）的元数据：

- **Component**：服务、库、网站、数据管道等
- **API**：服务暴露的接口（OpenAPI/AsyncAPI/GraphQL）
- **Resource**：数据库、S3 bucket、消息队列等基础设施
- **Group**：团队或部门
- **User**：开发者个人信息
- **System**：相关组件的集合（如"支付系统"包含多个 Component）
- **Domain**：业务领域（如"订单域""用户域"）

每个实体通过 `catalog-info.yaml` 描述，存在对应的代码仓库里。

### Scaffolder Templates

脚手架模板允许用户通过表单界面，一键创建符合团队规范的新服务——包括代码仓库、CI/CD 流水线、K8s 配置、监控告警规则一次性生成到位。这是从根源解决配置漂移的方案。

### TechDocs

基于 MkDocs 的文档系统，文档以 Markdown 格式存在代码仓库里（文档即代码），Backstage 负责构建和展示，自动关联到对应的 Component。

### Plugins

Backstage 的扩展机制。官方提供了 100+ 插件，社区还有更多。插件可以在 Catalog 页面增加 Tab，提供额外的上下文信息。

---

## 部署

### 本地快速体验

用 npx 快速启动（需要 Node.js 18+）：

```bash
npx @backstage/create-app@latest
# 输入项目名称，如 my-backstage

cd my-backstage
yarn install
yarn dev
```

浏览器访问 `http://localhost:3000` 即可看到 Backstage 界面。

这个方式适合评估和开发，生产环境需要 Docker 镜像化后部署到 K8s。

### 生产环境：K8s + Helm 部署

**构建镜像：**

```dockerfile
# packages/backend/Dockerfile
FROM node:18-bookworm-slim

# 安装依赖
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && \
    apt-get install -y --no-install-recommends python3 g++ build-essential && \
    rm -rf /var/lib/apt/lists/*

USER node

WORKDIR /app

COPY --chown=node:node yarn.lock package.json packages/backend/dist/bundle.tar.gz ./

RUN tar xzf bundle.tar.gz && \
    yarn install --frozen-lockfile --production --network-timeout 300000

CMD ["node", "packages/backend", "--config", "app-config.yaml", "--config", "app-config.production.yaml"]
```

**Helm 部署：**

```bash
helm repo add backstage https://backstage.github.io/charts
helm repo update

helm install backstage backstage/backstage \
  --namespace backstage \
  --create-namespace \
  --values values.yaml
```

`values.yaml` 核心配置：

```yaml
backstage:
  image:
    registry: my-registry.com
    repository: backstage
    tag: "1.0.0"
  
  appConfig:
    app:
      baseUrl: https://backstage.company.com
    
    backend:
      baseUrl: https://backstage.company.com
      database:
        client: pg
        connection:
          host: ${POSTGRES_HOST}
          port: 5432
          user: ${POSTGRES_USER}
          password: ${POSTGRES_PASSWORD}
          database: backstage
    
    auth:
      providers:
        github:
          development:
            clientId: ${GITHUB_CLIENT_ID}
            clientSecret: ${GITHUB_CLIENT_SECRET}
    
    catalog:
      providers:
        github:
          myOrg:
            organization: "my-github-org"
            catalogPath: "/catalog-info.yaml"
            filters:
              branch: "main"
            schedule:
              frequency: { minutes: 30 }
              timeout: { minutes: 3 }

postgresql:
  enabled: true
  auth:
    password: ${POSTGRES_PASSWORD}

ingress:
  enabled: true
  host: backstage.company.com
  annotations:
    kubernetes.io/ingress.class: nginx
    cert-manager.io/cluster-issuer: letsencrypt-prod
  tls:
    - secretName: backstage-tls
      hosts:
        - backstage.company.com
```

---

## Software Catalog 配置

### catalog-info.yaml 规范

每个代码仓库根目录放一个 `catalog-info.yaml`：

```yaml
apiVersion: backstage.io/v1alpha1
kind: Component
metadata:
  name: payment-service
  title: "支付服务"
  description: "处理订单支付、退款、对账的核心服务"
  annotations:
    # 关联 GitHub 仓库
    github.com/project-slug: "my-org/payment-service"
    # 关联 ArgoCD 应用
    argocd/app-name: "payment-service-prod"
    # 关联 Grafana 仪表盘
    grafana/dashboard-selector: "service=payment-service"
    # 关联 PagerDuty 服务
    pagerduty.com/service-id: "PXXXXXX"
    # 关联 Kubernetes 部署
    backstage.io/kubernetes-id: payment-service
    backstage.io/kubernetes-namespace: production
    # TechDocs
    backstage.io/techdocs-ref: dir:.
  tags:
    - go
    - payment
    - kafka
  links:
    - url: https://grafana.company.com/d/payment
      title: Grafana 监控
      icon: dashboard
    - url: https://runbook.company.com/payment-service
      title: Runbook
      icon: docs
spec:
  type: service
  lifecycle: production      # experimental / deprecated / production
  owner: group:payments-team
  system: payment-system
  dependsOn:
    - resource:default/payment-db
    - component:default/order-service
  providesApis:
    - payment-api
```

**API 实体：**

```yaml
apiVersion: backstage.io/v1alpha1
kind: API
metadata:
  name: payment-api
  description: "支付服务 REST API"
  annotations:
    backstage.io/techdocs-ref: dir:.
spec:
  type: openapi
  lifecycle: production
  owner: group:payments-team
  definition:
    $text: ./openapi.yaml   # 引用本仓库的 OpenAPI spec 文件
```

**Resource 实体（数据库）：**

```yaml
apiVersion: backstage.io/v1alpha1
kind: Resource
metadata:
  name: payment-db
  description: "支付服务 PostgreSQL 数据库"
spec:
  type: database
  owner: group:payments-team
  system: payment-system
```

### 批量导入 GitHub 组织仓库

手动在每个仓库添加 `catalog-info.yaml` 是起步阶段的做法。规模大了需要自动化发现：

```yaml
# app-config.yaml
catalog:
  providers:
    github:
      myOrg:
        organization: "my-github-org"
        # 自动扫描所有仓库
        catalogPath: "/catalog-info.yaml"
        filters:
          # 只扫描非归档仓库
          visibility: ["public", "private"]
        schedule:
          frequency: { hours: 1 }
          timeout: { minutes: 5 }
```

对于已有几百个仓库但没有 `catalog-info.yaml` 的情况，可以用脚本批量创建 PR：

```python
#!/usr/bin/env python3
"""批量为 GitHub 组织仓库生成 catalog-info.yaml"""

import os
import base64
from github import Github

g = Github(os.environ["GITHUB_TOKEN"])
org = g.get_organization("my-github-org")

TEMPLATE = """apiVersion: backstage.io/v1alpha1
kind: Component
metadata:
  name: {repo_name}
  description: "{description}"
  annotations:
    github.com/project-slug: "my-github-org/{repo_name}"
  tags: []
spec:
  type: service
  lifecycle: production
  owner: group:default/unknown
"""

for repo in org.get_repos():
    if repo.archived:
        continue
    
    # 检查是否已有 catalog-info.yaml
    try:
        repo.get_contents("catalog-info.yaml")
        print(f"{repo.name}: 已存在，跳过")
        continue
    except Exception:
        pass
    
    content = TEMPLATE.format(
        repo_name=repo.name,
        description=repo.description or f"{repo.name} service"
    )
    
    # 创建 PR
    default_branch = repo.default_branch
    main_ref = repo.get_git_ref(f"heads/{default_branch}")
    
    branch_name = "add-backstage-catalog"
    try:
        repo.create_git_ref(
            f"refs/heads/{branch_name}",
            main_ref.object.sha
        )
    except Exception:
        pass
    
    repo.create_file(
        "catalog-info.yaml",
        "chore: add Backstage catalog config",
        content,
        branch=branch_name
    )
    
    repo.create_pull(
        title="Add Backstage catalog-info.yaml",
        body="自动生成 Backstage catalog 配置，请 review 后合并",
        head=branch_name,
        base=default_branch
    )
    print(f"{repo.name}: 已创建 PR")
```

---

## 脚手架模板：一键创建服务

### 模板结构

Scaffolder Template 由三部分组成：
1. **Parameters**：用户填写的表单字段
2. **Steps**：执行的操作（获取代码、生成文件、发布到 GitHub、注册到 Catalog）
3. **Output**：完成后展示给用户的链接

```yaml
# template.yaml
apiVersion: scaffolder.backstage.io/v1beta3
kind: Template
metadata:
  name: create-go-service
  title: "创建 Go 微服务"
  description: "一键创建符合公司规范的 Go 微服务，包含 CI/CD 流水线、K8s 配置和监控告警"
  tags:
    - recommended
    - go
    - microservice
spec:
  owner: group:platform-team
  type: service

  parameters:
    - title: "服务基本信息"
      required: [name, description, owner]
      properties:
        name:
          title: 服务名称
          type: string
          description: "小写字母和连字符，如 payment-service"
          pattern: "^[a-z][a-z0-9-]*[a-z0-9]$"
          maxLength: 50
          ui:autofocus: true
        description:
          title: 服务描述
          type: string
          maxLength: 200
        owner:
          title: 负责团队
          type: string
          ui:field: OwnerPicker
          ui:options:
            allowArbitraryValues: false

    - title: "技术选型"
      properties:
        httpPort:
          title: HTTP 端口
          type: integer
          default: 8080
        enableKafka:
          title: 是否使用 Kafka
          type: boolean
          default: false
        enablePostgres:
          title: 是否使用 PostgreSQL
          type: boolean
          default: false
        deployEnvs:
          title: 部署环境
          type: array
          items:
            type: string
            enum: [dev, staging, production]
          uniqueItems: true
          ui:widget: checkboxes

    - title: "代码仓库"
      required: [repoOrg]
      properties:
        repoOrg:
          title: GitHub 组织
          type: string
          default: my-github-org
        repoVisibility:
          title: 仓库可见性
          type: string
          default: private
          enum: [public, private, internal]

  steps:
    # 从模板目录拉取骨架代码
    - id: fetch-base
      name: 初始化代码模板
      action: fetch:template
      input:
        url: ./skeleton
        values:
          name: ${{ parameters.name }}
          description: ${{ parameters.description }}
          owner: ${{ parameters.owner }}
          httpPort: ${{ parameters.httpPort }}
          enableKafka: ${{ parameters.enableKafka }}
          enablePostgres: ${{ parameters.enablePostgres }}

    # 创建 GitHub 仓库并推送代码
    - id: publish
      name: 创建 GitHub 仓库
      action: publish:github
      input:
        allowedHosts: ["github.com"]
        description: ${{ parameters.description }}
        repoUrl: "github.com?owner=${{ parameters.repoOrg }}&repo=${{ parameters.name }}"
        defaultBranch: main
        repoVisibility: ${{ parameters.repoVisibility }}
        gitCommitMessage: "feat: initial service scaffold"
        topics:
          - go
          - microservice

    # 注册到 Backstage Catalog
    - id: register
      name: 注册到 Catalog
      action: catalog:register
      input:
        repoContentsUrl: ${{ steps.publish.output.repoContentsUrl }}
        catalogInfoPath: "/catalog-info.yaml"

    # 触发 CI 初始化
    - id: github-actions
      name: 配置 GitHub Actions
      action: github:actions:dispatch
      input:
        repoUrl: ${{ steps.publish.output.remoteUrl }}
        workflowId: init-service.yml
        branchOrTagName: main
        workflowInputs:
          service_name: ${{ parameters.name }}
          deploy_envs: ${{ parameters.deployEnvs | join(',') }}

  output:
    links:
      - title: 打开代码仓库
        url: ${{ steps.publish.output.remoteUrl }}
        icon: github
      - title: 查看 Catalog
        icon: catalog
        entityRef: ${{ steps.register.output.entityRef }}
      - title: 查看 CI 流水线
        url: ${{ steps.publish.output.remoteUrl }}/actions
        icon: launch
```

### 骨架代码模板（skeleton）

模板的 `skeleton/` 目录包含实际的文件模板，使用 Nunjucks 语法插值：

```
skeleton/
├── catalog-info.yaml
├── go.mod
├── main.go
├── .github/
│   └── workflows/
│       └── ci.yml
├── k8s/
│   ├── deployment.yaml
│   └── service.yaml
└── docs/
    └── index.md
```

`skeleton/catalog-info.yaml`：

```yaml
apiVersion: backstage.io/v1alpha1
kind: Component
metadata:
  name: ${{ values.name }}
  description: "${{ values.description }}"
  annotations:
    github.com/project-slug: "my-github-org/${{ values.name }}"
    backstage.io/techdocs-ref: dir:.
spec:
  type: service
  lifecycle: experimental
  owner: ${{ values.owner }}
```

`skeleton/k8s/deployment.yaml`：

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ${{ values.name }}
  labels:
    app: ${{ values.name }}
spec:
  replicas: 2
  selector:
    matchLabels:
      app: ${{ values.name }}
  template:
    metadata:
      labels:
        app: ${{ values.name }}
    spec:
      containers:
        - name: ${{ values.name }}
          image: my-registry.com/${{ values.name }}:latest
          ports:
            - containerPort: ${{ values.httpPort }}
          resources:
            requests:
              cpu: "100m"
              memory: "128Mi"
            limits:
              cpu: "500m"
              memory: "512Mi"
          livenessProbe:
            httpGet:
              path: /healthz
              port: ${{ values.httpPort }}
          readinessProbe:
            httpGet:
              path: /readyz
              port: ${{ values.httpPort }}
```

---

## Kubernetes 插件

K8s 插件让开发者不需要直接使用 kubectl，就能在 Backstage 界面查看服务的部署状态、Pod 日志、HPA 状态等。

### 安装配置

```bash
# 前端插件
yarn --cwd packages/app add @backstage/plugin-kubernetes

# 后端插件
yarn --cwd packages/backend add @backstage/plugin-kubernetes-backend
```

在 `app-config.yaml` 中配置集群信息：

```yaml
kubernetes:
  serviceLocatorMethod:
    type: "multiTenant"
  clusterLocatorMethods:
    - type: "config"
      clusters:
        - url: https://k8s-prod.company.com
          name: production
          authProvider: "serviceAccount"
          skipTLSVerify: false
          skipMetricsLookup: false
          serviceAccountToken: ${K8S_PROD_SA_TOKEN}
          caData: ${K8S_PROD_CA_DATA}
        - url: https://k8s-staging.company.com
          name: staging
          authProvider: "serviceAccount"
          serviceAccountToken: ${K8S_STAGING_SA_TOKEN}
          caData: ${K8S_STAGING_CA_DATA}
  customResources:
    - group: "argoproj.io"
      apiVersion: "v1alpha1"
      plural: "rollouts"
```

为 Backstage 创建专用的 ServiceAccount 和 RBAC：

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: backstage
  namespace: backstage
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: backstage-read-only
rules:
  - apiGroups: [""]
    resources: ["pods", "pods/log", "services", "configmaps", "limitranges",
                "resourcequotas", "endpoints", "events", "namespaces"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["apps"]
    resources: ["deployments", "replicasets", "statefulsets", "daemonsets"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["autoscaling"]
    resources: ["horizontalpodautoscalers"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["networking.k8s.io"]
    resources: ["ingresses"]
    verbs: ["get", "list", "watch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: backstage-read-only
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: backstage-read-only
subjects:
  - kind: ServiceAccount
    name: backstage
    namespace: backstage
```

服务关联 K8s 资源，在 `catalog-info.yaml` 中添加注解：

```yaml
annotations:
  backstage.io/kubernetes-id: payment-service
  backstage.io/kubernetes-namespace: production
  backstage.io/kubernetes-label-selector: "app=payment-service"
```

配置后，payment-service 的 Catalog 页面会出现"Kubernetes"Tab，展示所有集群中该服务的 Pod 状态、Deployment 滚动更新进度、最新日志。

---

## TechDocs：文档即代码

### 配置 TechDocs

在 `catalog-info.yaml` 中添加注解：

```yaml
annotations:
  backstage.io/techdocs-ref: dir:.
```

在代码仓库根目录添加 `mkdocs.yml`：

```yaml
site_name: "支付服务文档"
site_description: "支付服务开发、运维文档"

nav:
  - 首页: index.md
  - 架构设计:
    - 整体架构: architecture/overview.md
    - 数据库设计: architecture/database.md
    - Kafka 消息格式: architecture/kafka-events.md
  - 运维手册:
    - 部署流程: ops/deployment.md
    - 告警处理: ops/alerting.md
    - 故障排查: ops/troubleshooting.md
  - API 文档:
    - REST API: api/rest.md
    - 错误码: api/error-codes.md

plugins:
  - techdocs-core
```

文档目录结构：

```
docs/
├── index.md                  # 服务概览
├── architecture/
│   ├── overview.md
│   ├── database.md
│   └── kafka-events.md
├── ops/
│   ├── deployment.md
│   ├── alerting.md
│   └── troubleshooting.md
└── api/
    ├── rest.md
    └── error-codes.md
```

### 生产环境 TechDocs 存储

开发环境 TechDocs 可以本地构建，生产环境推荐使用 S3 存储预构建的文档：

```yaml
# app-config.yaml
techdocs:
  builder: "external"
  generator:
    runIn: "local"
  publisher:
    type: "awsS3"
    awsS3:
      bucketName: my-company-techdocs
      region: us-east-1
      accountId: "123456789012"
```

在 CI 中预构建文档（以 GitHub Actions 为例）：

```yaml
name: Publish TechDocs

on:
  push:
    branches: [main]
    paths:
      - docs/**
      - mkdocs.yml
      - catalog-info.yaml

jobs:
  publish-techdocs:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Setup Python
        uses: actions/setup-python@v4
        with:
          python-version: "3.11"
      - name: Install TechDocs CLI
        run: pip install mkdocs-techdocs-core==1.3.3
      - name: Install @techdocs/cli
        run: npm install -g @techdocs/cli
      - name: Publish TechDocs to S3
        run: |
          techdocs-cli publish \
            --publisher-type awsS3 \
            --storage-name my-company-techdocs \
            --entity default/component/payment-service
        env:
          AWS_ACCESS_KEY_ID: ${{ secrets.AWS_ACCESS_KEY_ID }}
          AWS_SECRET_ACCESS_KEY: ${{ secrets.AWS_SECRET_ACCESS_KEY }}
          AWS_REGION: us-east-1
```

---

## 自定义 Plugin 开发

### 前端插件：展示自定义信息

假设需要在每个服务的 Catalog 页面展示该服务的当前 SLO 状态。

**创建插件：**

```bash
yarn backstage-cli new --select plugin
# 输入插件名：slo-status
```

生成的插件结构：

```
plugins/slo-status/
├── src/
│   ├── components/
│   │   └── SloStatusCard/
│   │       ├── SloStatusCard.tsx
│   │       └── index.ts
│   ├── plugin.ts
│   └── index.ts
├── package.json
└── README.md
```

`SloStatusCard.tsx`：

```tsx
import React from 'react';
import {
  InfoCard,
  Progress,
  StatusOK,
  StatusError,
  StatusWarning,
} from '@backstage/core-components';
import { useEntity } from '@backstage/plugin-catalog-react';
import { useApi } from '@backstage/core-plugin-api';
import { sloApiRef } from '../../api';

export const SloStatusCard = () => {
  const { entity } = useEntity();
  const sloApi = useApi(sloApiRef);
  const serviceName = entity.metadata.name;

  const { value: sloData, loading, error } = useAsync(
    () => sloApi.getSloStatus(serviceName),
    [serviceName]
  );

  if (loading) return <Progress />;
  if (error) return <div>无法加载 SLO 数据</div>;

  const { availability, errorBudget, status } = sloData!;

  const StatusIcon = status === 'ok' ? StatusOK :
                     status === 'warning' ? StatusWarning : StatusError;

  return (
    <InfoCard title="SLO 状态" subheader={`服务: ${serviceName}`}>
      <Grid container spacing={2}>
        <Grid item xs={6}>
          <Typography variant="h6">可用性</Typography>
          <Typography variant="h4" color={availability >= 99.9 ? 'primary' : 'error'}>
            {availability.toFixed(3)}%
          </Typography>
          <Typography variant="body2" color="textSecondary">
            目标: 99.9%
          </Typography>
        </Grid>
        <Grid item xs={6}>
          <Typography variant="h6">错误预算剩余</Typography>
          <Typography variant="h4">
            {errorBudget.toFixed(1)}%
          </Typography>
          <LinearProgress
            variant="determinate"
            value={errorBudget}
            color={errorBudget > 50 ? 'primary' : errorBudget > 20 ? 'secondary' : 'error'}
          />
        </Grid>
        <Grid item xs={12}>
          <Chip
            icon={<StatusIcon />}
            label={status === 'ok' ? '正常' : status === 'warning' ? '警告' : '违规'}
            color={status === 'ok' ? 'primary' : 'default'}
          />
        </Grid>
      </Grid>
    </InfoCard>
  );
};
```

**注册插件到 Catalog 实体页面：**

```tsx
// packages/app/src/components/catalog/EntityPage.tsx
import { SloStatusCard } from '@internal/plugin-slo-status';

const serviceEntityPage = (
  <EntityLayout>
    <EntityLayout.Route path="/" title="概览">
      <Grid container spacing={3}>
        <Grid item xs={12} md={6}>
          <EntityAboutCard variant="gridItem" />
        </Grid>
        <Grid item xs={12} md={6}>
          <SloStatusCard />  {/* 添加自定义卡片 */}
        </Grid>
        <Grid item xs={12}>
          <EntityHasSystemsCard variant="gridItem" />
        </Grid>
      </Grid>
    </EntityLayout.Route>
    {/* 其他 Tab... */}
  </EntityLayout>
);
```

### 后端插件：自定义 API

前端插件调用的 SLO 数据需要后端插件提供 API：

```bash
yarn backstage-cli new --select backend-plugin
# 输入插件名：slo-status-backend
```

`router.ts`：

```typescript
import { Router } from 'express';
import { CatalogClient } from '@backstage/catalog-client';
import { PrometheusClient } from './prometheus';

export async function createRouter(options: RouterOptions): Promise<Router> {
  const router = Router();
  const prometheus = new PrometheusClient(options.config);

  router.get('/slo/:serviceName', async (req, res) => {
    const { serviceName } = req.params;

    try {
      // 从 Prometheus 查询 SLO 数据
      const availability = await prometheus.query(
        `avg_over_time(
          (1 - rate(http_requests_total{service="${serviceName}",status=~"5.."}[5m])
          / rate(http_requests_total{service="${serviceName}"}[5m]))[30d:5m]
        ) * 100`
      );

      const errorBudgetUsed = await prometheus.query(
        `(1 - avg_over_time(
          (1 - rate(http_requests_total{service="${serviceName}",status=~"5.."}[5m])
          / rate(http_requests_total{service="${serviceName}"}[5m]))[30d:5m]
        )) / 0.001 * 100`
      );

      const avail = parseFloat(availability);
      const budgetRemaining = 100 - parseFloat(errorBudgetUsed);

      res.json({
        availability: avail,
        errorBudget: budgetRemaining,
        status: avail >= 99.9 ? 'ok' : avail >= 99.0 ? 'warning' : 'error',
      });
    } catch (error) {
      res.status(500).json({ error: 'Failed to fetch SLO data' });
    }
  });

  return router;
}
```

---

## 与现有工具集成

### ArgoCD 集成

安装 ArgoCD 插件后，Catalog 页面会显示 ArgoCD 应用的同步状态、健康状态、最近部署历史。

```bash
yarn --cwd packages/app add @roadiehq/backstage-plugin-argo-cd
yarn --cwd packages/backend add @roadiehq/backstage-plugin-argo-cd-backend
```

`app-config.yaml` 配置：

```yaml
argocd:
  baseUrl: https://argocd.company.com
  token: ${ARGOCD_TOKEN}
  waitCycles: 25
  appLocatorMethods:
    - type: 'config'
      instances:
        - name: argocd
          url: https://argocd.company.com
          token: ${ARGOCD_TOKEN}
```

`catalog-info.yaml` 关联 ArgoCD 应用：

```yaml
annotations:
  argocd/app-name: "payment-service-prod"
  # 多应用（多集群部署）
  argocd/app-name: "payment-service-prod,payment-service-staging"
```

### Grafana 集成

```bash
yarn --cwd packages/app add @k-phoen/backstage-plugin-grafana
```

```yaml
# app-config.yaml
grafana:
  domain: https://grafana.company.com
  unifiedAlerting: true
```

```yaml
# catalog-info.yaml
annotations:
  grafana/dashboard-selector: "service=payment-service"
  grafana/alert-label-selector: "service=payment-service"
```

### Slack 集成

让 Backstage 知道每个服务对应哪个 Slack 频道，开发者可以直接跳转：

```yaml
# catalog-info.yaml
metadata:
  links:
    - url: https://my-company.slack.com/channels/payment-team
      title: Slack 频道
      icon: chat
```

或通过 PagerDuty 插件，直接在 Backstage 触发告警或查看 on-call 排班：

```yaml
annotations:
  pagerduty.com/service-id: "PXXXXXX"
  pagerduty.com/integration-key: ${PAGERDUTY_INTEGRATION_KEY}
```

---

## 推广与落地

### 如何说服团队使用

直接说"用 Backstage 吧"很难推动。更有效的方式是从痛点入手：

**找准第一个高价值场景。** 对于工程基础建设薄弱的团队，通常最痛的是新服务创建——一个后端服务从立项到第一次生产部署，可能需要在 10 个地方配置，花 1-2 天。做一个好用的 Scaffolder 模板，让这个过程缩短到 10 分钟，这就是立竿见影的价值。

**让 Catalog 先成为"黄页"。** 不要一开始就追求大而全，先把所有服务的基本信息（owner、关联仓库、Grafana 链接）录入进去，让大家养成"找服务信息就上 Backstage 查"的习惯。

**运维团队先用起来。** 给 on-call 工程师配置 K8s 插件和 PagerDuty 集成，让他们处理告警的时候能直接在 Backstage 看 Pod 状态，减少切换工具的摩擦。

### 如何衡量 IDP 价值

量化价值是持续获得资源投入的前提：

**开发者体验指标（通过季度问卷）：**
- "找到我不熟悉的服务信息需要多长时间？"
- "创建一个新服务需要多长时间？"
- "你对 Backstage 的满意度（1-10分）"

**客观指标：**

```
新服务创建时间 = 从 Scaffolder 提交到第一次生产部署的时长
服务信息完整率 = 有完整 catalog-info.yaml 的服务数 / 总服务数
文档新鲜度 = 过去 3 个月内有更新的文档 / 总文档数
Catalog 月活 = 每月使用 Backstage 的独立用户数
```

**典型成功案例：**
- 某团队引入 Scaffolder 后，新服务 onboarding 时间从 2 天 → 20 分钟
- Catalog 上线后，"这个服务谁负责？"类型的 Slack 问题减少了 80%
- TechDocs 统一后，服务文档覆盖率从 30% 提升到 85%

### 持续运营

Backstage 不是部署完就一劳永逸的工具。需要有专人（平台工程师或基础设施工程师）持续维护：

- 定期检查 Catalog 数据质量（过期、orphan 实体清理）
- 跟进 Backstage 版本升级（官方每 2 周发一个小版本）
- 收集开发者反馈，持续迭代插件和模板
- 建立 Backstage 使用文档和培训材料

一个健康的 IDP 应该是团队工程文化的一部分，而不是强制使用的工具。当开发者发现"在 Backstage 上做事比绕过它更方便"的时候，推广就成功了。
