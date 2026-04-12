---
title: "Helm 工程化实践：从 Chart 设计到多环境管理"
date: 2026-04-12T09:00:00+08:00
draft: false
tags: ["Helm", "Kubernetes", "Chart", "DevOps", "GitOps"]
categories: ["Kubernetes"]
description: "Helm Chart 工程化设计：模板函数、多环境 values、私有仓库与回滚实战"
summary: "基于生产踩坑经验，系统梳理 Helm Chart 结构设计、_helpers.tpl 复用技巧、多环境 values 管理策略、私有 Harbor 仓库推送流程，以及 --atomic 升级与回滚的正确姿势。"
toc: true
math: false
diagram: false
series: ["K8s 完全指南"]
keywords: ["Helm", "Chart", "Kubernetes", "values", "Harbor", "helm upgrade", "回滚"]
params:
  reading_time: true
---

在我接手的第一个 Kubernetes 项目里，所有服务的 Helm Chart 都是各自为政：命名规范不一、values 结构随意、没有多环境管理，每次发版都像在拆盲盒。经过两年多的摸爬滚打，我逐渐形成了一套相对稳定的 Helm 工程化实践，这篇文章就来系统梳理一下。

## Chart 目录结构设计

一个合理的 Chart 目录结构是工程化的基础。我目前推荐的结构如下：

```
my-service/
├── Chart.yaml
├── values.yaml                # 默认值，也是文档
├── values-dev.yaml            # 开发环境覆盖
├── values-staging.yaml        # 预发环境覆盖
├── values-prod.yaml           # 生产环境覆盖
├── templates/
│   ├── _helpers.tpl           # 公共模板函数
│   ├── deployment.yaml
│   ├── service.yaml
│   ├── ingress.yaml
│   ├── configmap.yaml
│   ├── serviceaccount.yaml
│   ├── hpa.yaml
│   └── NOTES.txt
└── charts/                    # 子 Chart 依赖
```

`values.yaml` 承担两个职责：一是提供合理的默认值，二是作为配置项的文档。每个字段都应该有注释说明其用途。

```yaml
# values.yaml
replicaCount: 2

image:
  repository: registry.example.com/my-service
  pullPolicy: IfNotPresent
  # tag 留空，部署时通过 --set image.tag=xxx 传入
  tag: ""

# 资源配额，生产环境通过 values-prod.yaml 覆盖
resources:
  limits:
    cpu: 500m
    memory: 512Mi
  requests:
    cpu: 100m
    memory: 128Mi

# 自动扩缩容，默认关闭
autoscaling:
  enabled: false
  minReplicas: 2
  maxReplicas: 10
  targetCPUUtilizationPercentage: 70

# 健康检查
livenessProbe:
  httpGet:
    path: /healthz
    port: 8080
  initialDelaySeconds: 30
  periodSeconds: 10

readinessProbe:
  httpGet:
    path: /ready
    port: 8080
  initialDelaySeconds: 10
  periodSeconds: 5
```

## _helpers.tpl：模板复用的核心

`_helpers.tpl` 是 Helm 模板函数的集中定义文件，下划线前缀让 Helm 知道这个文件不会直接渲染为 K8s 资源。

```
{{/*
生成应用名称，最长 63 字符（DNS label 限制）
*/}}
{{- define "my-service.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
生成完整的 release 名称
如果 release name 包含 chart name，只用 release name
*/}}
{{- define "my-service.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
标准 labels，所有资源都应带上
*/}}
{{- define "my-service.labels" -}}
helm.sh/chart: {{ include "my-service.chart" . }}
{{ include "my-service.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels，用于 Service 选择 Pod
*/}}
{{- define "my-service.selectorLabels" -}}
app.kubernetes.io/name: {{ include "my-service.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
ServiceAccount 名称
*/}}
{{- define "my-service.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "my-service.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}
```

在 Deployment 中引用这些函数：

```yaml
# templates/deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "my-service.fullname" . }}
  labels:
    {{- include "my-service.labels" . | nindent 4 }}
spec:
  {{- if not .Values.autoscaling.enabled }}
  replicas: {{ .Values.replicaCount }}
  {{- end }}
  selector:
    matchLabels:
      {{- include "my-service.selectorLabels" . | nindent 6 }}
  template:
    metadata:
      labels:
        {{- include "my-service.selectorLabels" . | nindent 8 }}
    spec:
      containers:
        - name: {{ .Chart.Name }}
          image: "{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}"
          imagePullPolicy: {{ .Values.image.pullPolicy }}
          ports:
            - name: http
              containerPort: 8080
              protocol: TCP
          {{- with .Values.livenessProbe }}
          livenessProbe:
            {{- toYaml . | nindent 12 }}
          {{- end }}
          {{- with .Values.readinessProbe }}
          readinessProbe:
            {{- toYaml . | nindent 12 }}
          {{- end }}
          resources:
            {{- toYaml .Values.resources | nindent 12 }}
          env:
            - name: APP_ENV
              value: {{ .Values.appEnv | quote }}
          {{- if .Values.extraEnv }}
          {{- range .Values.extraEnv }}
            - name: {{ .name | quote }}
              value: {{ .value | quote }}
          {{- end }}
          {{- end }}
```

## 多环境 values 管理

用多个 values 文件覆盖默认值，是我见过最清晰的多环境管理方式。每个环境文件只写与默认值**不同**的部分：

```yaml
# values-dev.yaml
replicaCount: 1

resources:
  limits:
    cpu: 200m
    memory: 256Mi
  requests:
    cpu: 50m
    memory: 64Mi

appEnv: "development"

# 开发环境关闭 HPA
autoscaling:
  enabled: false
```

```yaml
# values-staging.yaml
replicaCount: 2

appEnv: "staging"

autoscaling:
  enabled: true
  minReplicas: 2
  maxReplicas: 5
```

```yaml
# values-prod.yaml
replicaCount: 3

resources:
  limits:
    cpu: 2000m
    memory: 2Gi
  requests:
    cpu: 500m
    memory: 512Mi

appEnv: "production"

autoscaling:
  enabled: true
  minReplicas: 3
  maxReplicas: 20
  targetCPUUtilizationPercentage: 60
```

部署命令：

```bash
# 开发环境
helm upgrade --install my-service ./my-service \
  -f values-dev.yaml \
  --set image.tag=v1.2.3 \
  -n dev

# 生产环境
helm upgrade --install my-service ./my-service \
  -f values-prod.yaml \
  --set image.tag=v1.2.3 \
  -n prod \
  --atomic \
  --timeout 5m
```

`-f` 支持多次使用，后面的文件会覆盖前面的值，这在需要叠加环境配置时很有用：

```bash
# 基础配置 + 区域特定配置
helm upgrade --install my-service ./my-service \
  -f values-prod.yaml \
  -f values-prod-us.yaml \
  --set image.tag=v1.2.3
```

## 私有 Harbor 仓库推送

团队内部一般都会有私有镜像仓库，Helm Chart 同样可以托管在 Harbor 的 OCI 仓库中。

**推送 Chart 到 Harbor：**

```bash
# Harbor 2.x 支持 OCI 格式
helm registry login registry.example.com \
  --username admin \
  --password-stdin <<< "$HARBOR_PASSWORD"

# 打包
helm package ./my-service --version 1.2.3

# 推送（OCI 格式）
helm push my-service-1.2.3.tgz oci://registry.example.com/helm-charts
```

**使用传统 Chart Repository（chartmuseum）：**

```bash
# 添加私有 repo
helm repo add my-repo https://registry.example.com/chartrepo/my-project \
  --username admin \
  --password "$HARBOR_PASSWORD"

helm repo update

# 安装
helm install my-service my-repo/my-service --version 1.2.3
```

**CI/CD 中自动推送：**

```bash
#!/bin/bash
set -e

CHART_NAME="my-service"
CHART_VERSION="${CI_COMMIT_TAG:-0.0.0-dev}"
REGISTRY="registry.example.com"

# 更新 Chart.yaml 版本
sed -i "s/^version:.*/version: ${CHART_VERSION}/" Chart.yaml
sed -i "s/^appVersion:.*/appVersion: \"${CHART_VERSION}\"/" Chart.yaml

helm package .
helm push "${CHART_NAME}-${CHART_VERSION}.tgz" "oci://${REGISTRY}/helm-charts"
```

## helm upgrade --atomic 与回滚

`--atomic` 是我在生产环境必用的参数。它的行为是：升级失败时自动回滚到上一个版本，不会让集群处于半升级状态。

```bash
helm upgrade --install my-service ./my-service \
  -f values-prod.yaml \
  --set image.tag=v1.2.4 \
  -n prod \
  --atomic \          # 失败自动回滚
  --timeout 10m \     # 等待超时时间
  --cleanup-on-fail \ # 失败时删除新建的资源
  --wait              # 等待所有资源就绪
```

**手动回滚：**

```bash
# 查看历史版本
helm history my-service -n prod

# 回滚到上一版本
helm rollback my-service -n prod

# 回滚到指定版本
helm rollback my-service 3 -n prod --wait

# 查看当前值
helm get values my-service -n prod
```

**diff 插件（强烈推荐）：**

```bash
# 安装 helm-diff 插件
helm plugin install https://github.com/databus23/helm-diff

# 升级前预览变更
helm diff upgrade my-service ./my-service \
  -f values-prod.yaml \
  --set image.tag=v1.2.4 \
  -n prod
```

## 常见坑记录

### 坑1：字符串值忘记加 quote

YAML 中某些值看起来像数字或布尔，Helm 渲染时可能类型错误：

```yaml
# 错误：port 会被渲染为整数 8080，某些情况下导致解析失败
port: {{ .Values.service.port }}

# 正确：始终用 quote 包裹不确定的值
port: {{ .Values.service.port | quote }}

# 或者在 values.yaml 中直接用字符串
nodePort: "30080"
```

### 坑2：toYaml 缩进问题

`toYaml` 必须配合 `nindent` 或 `indent` 使用，否则会破坏 YAML 结构：

```yaml
# 错误：没有正确缩进
resources:
  {{ toYaml .Values.resources }}

# 正确：使用 nindent（会自动加换行）
resources:
  {{- toYaml .Values.resources | nindent 2 }}

# 或者用 with 块
{{- with .Values.resources }}
resources:
  {{- toYaml . | nindent 2 }}
{{- end }}
```

### 坑3：range 循环中的变量作用域

在 `range` 循环内访问外层变量（如 `.Release.Name`）会失效，因为 `.` 被重新绑定了：

```yaml
# 错误：循环内 .Release.Name 为空
{{- range .Values.hosts }}
  - host: {{ . }}
    # 这里访问不到外层的 .Release.Name
    serviceName: {{ .Release.Name }}-service
{{- end }}

# 正确：循环前保存外层 context
{{- $releaseName := .Release.Name }}
{{- range .Values.hosts }}
  - host: {{ . }}
    serviceName: {{ $releaseName }}-service
{{- end }}
```

### 坑4：条件渲染中的空行问题

Helm 模板中 `{{- }}` 和 `{{ -}}` 的空白控制很容易出错：

```yaml
# 可能产生多余空行
{{ if .Values.ingress.enabled }}
apiVersion: networking.k8s.io/v1
{{ end }}

# 正确：使用 {{- 消除前导空白
{{- if .Values.ingress.enabled }}
apiVersion: networking.k8s.io/v1
{{- end }}
```

### 坑5：helm upgrade 时 secret 丢失

如果 values 中有敏感字段（如数据库密码），每次 `helm upgrade` 都需要重新传入，否则会被重置为 values.yaml 的默认值：

```bash
# 使用 --reuse-values 复用上次的值
helm upgrade my-service ./my-service \
  --set image.tag=v1.2.4 \
  --reuse-values \
  -n prod
```

但 `--reuse-values` 也有坑：新增的 values 字段不会取默认值，而是直接忽略。更安全的做法是把敏感配置放进 K8s Secret，通过 `envFrom` 注入。

## Helmfile：多 Chart 编排

当项目有多个相互依赖的 Chart 时，可以用 Helmfile 做编排：

```yaml
# helmfile.yaml
repositories:
  - name: bitnami
    url: https://charts.bitnami.com/bitnami

releases:
  - name: postgresql
    namespace: db
    chart: bitnami/postgresql
    version: 12.x.x
    values:
      - values/postgresql.yaml

  - name: my-service
    namespace: app
    chart: ./charts/my-service
    values:
      - values/my-service-{{ .Environment.Name }}.yaml
    set:
      - name: image.tag
        value: {{ env "IMAGE_TAG" | default "latest" }}
    needs:
      - db/postgresql  # 先部署 postgresql

environments:
  dev:
    values:
      - env: dev
  prod:
    values:
      - env: prod
```

```bash
# 部署到 prod 环境
helmfile -e prod sync

# 只 diff 不实际操作
helmfile -e prod diff
```

## 总结

Helm 工程化的核心是**一致性**和**可预期性**：

1. 统一 Chart 目录结构和命名规范，降低新人上手成本
2. `_helpers.tpl` 集中管理公共模板函数，避免重复定义
3. 多环境 values 文件只写差异，主 values.yaml 保持完整默认值并充当文档
4. 生产部署必用 `--atomic`，配合 `helm diff` 做变更预览
5. 私有 Harbor 仓库配合 CI/CD 自动推送，版本号与 Git tag 对齐

踩过的最深的坑是 `toYaml` 缩进和 `range` 作用域问题，这两个问题在调试时不会报错，只会产生结构错误的 YAML，排查起来很费时间。建议在本地用 `helm template` 先渲染输出检查一遍，再执行 `helm upgrade`。
