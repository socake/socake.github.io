---
title: "Helm 使用指南：从入门到生产实践"
date: 2025-12-09T11:00:00+08:00
draft: false
tags: ["Kubernetes", "Helm", "运维", "DevOps"]
categories: ["Kubernetes"]
description: "Helm 包管理器完整使用指南，涵盖核心概念、常用命令、模板语法、生产最佳实践及与 Kustomize 的取舍分析。"
summary: "Helm 从入门到生产实践：Chart 结构、values 覆盖、模板语法、--atomic/--wait 等生产参数，以及常用 Chart 安装示例。"
toc: true
math: false
diagram: false
keywords: ["Helm", "Kubernetes", "Chart", "Helm Chart", "values.yaml", "Kustomize"]
params:
  reading_time: true
---

## 核心概念

在动手之前，先理清四个核心概念的关系：

| 概念 | 说明 | 类比 |
|------|------|------|
| **Chart** | Helm 的打包格式，包含一组 K8s 资源模板 | apt 的 .deb 包 |
| **Release** | Chart 在集群中的一次部署实例，有独立名称和版本 | 安装好的软件实例 |
| **Repository** | 存放 Chart 的仓库（HTTP 服务） | apt 的软件源 |
| **Values** | 渲染模板时注入的变量，可层叠覆盖 | 配置文件 |

一个 Chart 可以在同一集群中安装多次，每次产生一个独立的 Release，互不影响。例如：同一个 `redis` Chart 可以安装为 `redis-cache` 和 `redis-session` 两个 Release。

---

## 安装与配置

```bash
# 安装 Helm（Linux）
curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash

# 验证
helm version

# 添加常用 Chart 仓库
helm repo add stable https://charts.helm.sh/stable
helm repo add bitnami https://charts.bitnami.com/bitnami
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx
helm repo add jetstack https://charts.jetstack.io
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts

# 更新仓库索引
helm repo update

# 查看已添加的仓库
helm repo list
```

---

## 常用命令

### 搜索与查看

```bash
# 在仓库中搜索 Chart
helm search repo nginx

# 查看 Chart 所有可用版本
helm search repo bitnami/redis --versions

# 查看 Chart 的默认 values
helm show values bitnami/redis

# 查看 Chart 详情（README）
helm show readme bitnami/redis

# 查看 Chart 将生成的所有 K8s 资源（不实际部署）
helm template my-redis bitnami/redis -f my-values.yaml
```

### 安装

```bash
# 基础安装
helm install <release-name> <chart> -n <namespace>

# 安装并等待就绪，失败自动回滚（生产推荐）
helm install my-app ./my-chart \
  -n production \
  --create-namespace \
  --wait \
  --timeout 5m \
  --atomic

# 指定 values 文件安装
helm install my-redis bitnami/redis \
  -n database \
  -f values-prod.yaml \
  --set auth.password=mysecretpassword

# 安装指定版本
helm install my-redis bitnami/redis \
  --version 18.6.1 \
  -n database
```

### 升级与回滚

```bash
# 升级 Release
helm upgrade my-app ./my-chart -n production -f values.yaml

# 安装不存在时安装，已存在时升级（CI/CD 常用）
helm upgrade --install my-app ./my-chart \
  -n production \
  --create-namespace \
  -f values.yaml \
  --wait \
  --atomic \
  --timeout 5m

# 查看 Release 历史版本
helm history my-app -n production

# 回滚到指定版本
helm rollback my-app 2 -n production

# 回滚到上一个版本
helm rollback my-app -n production
```

### 查看与管理

```bash
# 列出所有 Release
helm list -A
helm list -n production

# 查看 Release 状态
helm status my-app -n production

# 查看 Release 实际使用的 values（包含默认值）
helm get values my-app -n production
helm get values my-app -n production --all  # 包含所有默认值

# 查看 Release 生成的 manifest
helm get manifest my-app -n production

# 卸载（默认保留历史记录）
helm uninstall my-app -n production

# 卸载并删除历史记录
helm uninstall my-app -n production --keep-history=false
```

---

## Values 覆盖方式与优先级

Helm 支持多层 values 叠加，优先级从低到高：

```
Chart 内置 values.yaml
  ↓ 被覆盖
-f values-base.yaml
  ↓ 被覆盖
-f values-prod.yaml      ← 多个 -f 后者覆盖前者
  ↓ 被覆盖
--set key=value          ← 最高优先级
```

```bash
# 多文件覆盖（常用于环境差异配置）
helm upgrade --install my-app ./chart \
  -f values/base.yaml \
  -f values/production.yaml \
  --set image.tag=v1.2.3 \
  --set replicaCount=3

# --set 设置嵌套值
--set ingress.hosts[0].host=example.com
--set persistence.storageClass=gp3

# --set-string 强制字符串类型（避免数字被解析为 int）
--set-string annotations."app\.kubernetes\.io/version"=1.0

# --set-file 从文件读取值
--set-file config.nginx=nginx.conf
```

**推荐的多环境 values 目录结构：**

```
chart/
├── values.yaml           # 默认值（所有环境通用）
├── values/
│   ├── base.yaml         # 公共覆盖
│   ├── staging.yaml      # Staging 环境
│   └── production.yaml   # 生产环境
```

---

## Chart 目录结构

```
my-chart/
├── Chart.yaml            # Chart 元数据（必须）
├── values.yaml           # 默认配置值
├── values.schema.json    # values 校验 Schema（可选，推荐）
├── charts/               # 依赖的子 Chart
├── templates/            # K8s 资源模板
│   ├── _helpers.tpl      # 模板辅助函数（不生成资源）
│   ├── deployment.yaml
│   ├── service.yaml
│   ├── ingress.yaml
│   ├── configmap.yaml
│   ├── hpa.yaml
│   └── NOTES.txt         # 安装完成后显示的说明
└── .helmignore           # 打包时忽略的文件
```

### Chart.yaml

```yaml
apiVersion: v2
name: my-app
description: A Helm chart for my application
type: application      # application 或 library
version: 0.1.0         # Chart 版本（语义化版本）
appVersion: "1.2.3"    # 应用版本（字符串）

dependencies:
  - name: redis
    version: "18.x.x"
    repository: https://charts.bitnami.com/bitnami
    condition: redis.enabled   # 可通过 values 控制是否启用
```

### _helpers.tpl — 模板辅助函数

```gotpl
{{/*
生成应用完整名称，最多 63 字符
*/}}
{{- define "my-app.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{/*
公共标签
*/}}
{{- define "my-app.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector 标签（不能变，否则会导致 Deployment 无法更新）
*/}}
{{- define "my-app.selectorLabels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
```

---

## 模板语法基础

### 访问 Values

```yaml
# templates/deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "my-app.fullname" . }}
  labels:
    {{- include "my-app.labels" . | nindent 4 }}
spec:
  replicas: {{ .Values.replicaCount }}
  selector:
    matchLabels:
      {{- include "my-app.selectorLabels" . | nindent 6 }}
  template:
    metadata:
      labels:
        {{- include "my-app.selectorLabels" . | nindent 8 }}
    spec:
      containers:
        - name: {{ .Chart.Name }}
          image: "{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}"
          imagePullPolicy: {{ .Values.image.pullPolicy }}
          ports:
            - containerPort: {{ .Values.service.port }}
          env:
            - name: ENV
              value: {{ .Values.env | quote }}        # quote 防止布尔值/数字被误解析
          resources:
            {{- toYaml .Values.resources | nindent 12 }}  # 将 values 中的对象直接渲染为 YAML
```

### if / else 条件

```yaml
{{- if .Values.ingress.enabled }}
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: {{ include "my-app.fullname" . }}
  {{- if .Values.ingress.annotations }}
  annotations:
    {{- toYaml .Values.ingress.annotations | nindent 4 }}
  {{- end }}
spec:
  {{- if .Values.ingress.tls }}
  tls:
    {{- range .Values.ingress.tls }}
    - hosts:
        {{- range .hosts }}
        - {{ . | quote }}
        {{- end }}
      secretName: {{ .secretName }}
    {{- end }}
  {{- end }}
{{- end }}
```

### range 循环

```yaml
# 遍历列表
env:
  {{- range .Values.extraEnvVars }}
  - name: {{ .name }}
    value: {{ .value | quote }}
  {{- end }}

# 遍历 map
podAnnotations:
  {{- range $key, $value := .Values.podAnnotations }}
  {{ $key }}: {{ $value | quote }}
  {{- end }}
```

### with 上下文切换

```yaml
{{- with .Values.nodeSelector }}
nodeSelector:
  {{- toYaml . | nindent 8 }}
{{- end }}

{{- with .Values.tolerations }}
tolerations:
  {{- toYaml . | nindent 8 }}
{{- end }}
```

---

## 生产实践

### 必备参数

```bash
# 生产环境部署标准命令
helm upgrade --install <release> <chart> \
  -n <namespace> \
  --create-namespace \
  -f values.yaml \
  --atomic \          # 失败时自动回滚
  --wait \            # 等待所有资源就绪
  --timeout 10m \     # 超时时间（根据应用启动时间调整）
  --history-max 5 \   # 保留最近 5 个版本
  --cleanup-on-fail   # 失败时清理新创建的资源
```

### values.schema.json — 值校验

```json
{
  "$schema": "https://json-schema.org/draft-07/schema#",
  "type": "object",
  "required": ["image"],
  "properties": {
    "replicaCount": {
      "type": "integer",
      "minimum": 1
    },
    "image": {
      "type": "object",
      "required": ["repository", "tag"],
      "properties": {
        "repository": { "type": "string" },
        "tag": { "type": "string" },
        "pullPolicy": {
          "type": "string",
          "enum": ["Always", "IfNotPresent", "Never"]
        }
      }
    }
  }
}
```

### Chart 依赖管理

```bash
# 下载依赖（在 Chart 目录下执行）
helm dependency update ./my-chart

# 构建依赖（使用 charts/ 目录中已有的）
helm dependency build ./my-chart

# 查看依赖状态
helm dependency list ./my-chart
```

---

## 常用公共 Chart 安装示例

### ingress-nginx

```bash
helm upgrade --install ingress-nginx ingress-nginx/ingress-nginx \
  -n ingress-nginx \
  --create-namespace \
  --set controller.replicaCount=2 \
  --set controller.resources.requests.cpu=100m \
  --set controller.resources.requests.memory=90Mi \
  --wait
```

### cert-manager

```bash
# 安装 cert-manager（需要先安装 CRD）
helm upgrade --install cert-manager jetstack/cert-manager \
  -n cert-manager \
  --create-namespace \
  --version v1.13.0 \
  --set installCRDs=true \
  --wait
```

```yaml
# 安装完后创建 ClusterIssuer（Let's Encrypt）
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt-prod
spec:
  acme:
    server: https://acme-v02.api.letsencrypt.org/directory
    email: admin@example.com
    privateKeySecretRef:
      name: letsencrypt-prod
    solvers:
      - http01:
          ingress:
            class: nginx
```

### kube-prometheus-stack

```bash
# values-monitoring.yaml
cat > values-monitoring.yaml << 'EOF'
grafana:
  adminPassword: "changeme"
  ingress:
    enabled: true
    hosts:
      - grafana.example.com

prometheus:
  prometheusSpec:
    retention: 15d
    storageSpec:
      volumeClaimTemplate:
        spec:
          storageClassName: gp3
          resources:
            requests:
              storage: 50Gi

alertmanager:
  alertmanagerSpec:
    storage:
      volumeClaimTemplate:
        spec:
          storageClassName: gp3
          resources:
            requests:
              storage: 10Gi
EOF

helm upgrade --install kube-prometheus-stack \
  prometheus-community/kube-prometheus-stack \
  -n monitoring \
  --create-namespace \
  -f values-monitoring.yaml \
  --version 55.5.0 \
  --wait \
  --timeout 10m
```

---

## Helm vs Kustomize 取舍

| 维度 | Helm | Kustomize |
|------|------|-----------|
| 学习曲线 | 较高（模板语法） | 较低（纯 YAML） |
| 打包分发 | 强（Chart 仓库） | 弱（git 引用） |
| 多环境差异 | values 文件覆盖 | overlay 目录 |
| 参数化能力 | 强（完整模板语言） | 弱（仅 patch） |
| 官方工具集成 | 完整（Helm Hub） | kubectl 内置 |
| 版本管理 | 内置（helm history） | 依赖 git |
| 调试体验 | `helm template` 预渲染 | `kustomize build` |
| ArgoCD 支持 | 原生支持 | 原生支持 |

**选型建议：**

- 使用第三方软件（nginx、prometheus、cert-manager）→ **优先选 Helm**，这些软件官方维护的 Helm Chart 质量高，直接用
- 管理自己的业务应用 → **Kustomize 更适合**，YAML 原生，结构清晰，适合 GitOps
- 已有 Helm Chart 且需要多环境差异 → **Helm + values 多文件**
- 复杂多环境、需要精细 patch → **Kustomize 的 overlay 机制**更灵活

**实际生产中常见方案：第三方依赖用 Helm，自研服务用 Kustomize，ArgoCD 统一管理。**

---

## 常见问题排查

```bash
# 查看 Helm 操作历史
helm history my-app -n production

# Release 升级卡住/失败后强制回滚
helm rollback my-app -n production

# 处理 "cannot re-use a name that is still in use" 错误
# 先检查是否真的存在
helm list -A | grep my-app
# 删除后重装
helm uninstall my-app -n production

# 处理 "rendered manifests contain a resource that already exists" 错误
# 通常是 --install 时已有同名资源，用 --replace 或先手动删除冲突资源

# 渲染模板检查（本地验证）
helm template my-app ./chart -f values.yaml | kubectl apply --dry-run=client -f -

# 查看实际部署的资源版本
helm get manifest my-app -n production | grep "image:"
```
