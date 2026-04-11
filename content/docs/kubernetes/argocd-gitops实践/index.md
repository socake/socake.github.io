---
title: "ArgoCD + Kustomize GitOps 体系实践"
date: 2025-12-08T14:00:00+08:00
draft: false
tags: ["ArgoCD", "GitOps", "Kubernetes", "Kustomize", "CI/CD"]
categories: ["CI/CD"]
description: "基于 ArgoCD + Kustomize 构建多环境 GitOps 发版体系，覆盖目录结构设计、ApplicationSet、同步策略与踩坑记录"
summary: "记录在多套 K8s 集群（AWS EKS + 阿里云 ACK）上落地 GitOps 的完整过程：目录结构设计、Kustomize overlay 环境差异管理、ArgoCD ApplicationSet 自动化、以及真实踩过的坑。"
toc: true
math: false
diagram: false
keywords: ["argocd", "gitops", "kustomize", "kubernetes", "CI/CD"]
params:
  reading_time: true
---

## 为什么要用 GitOps

在真正落地 GitOps 之前，我们的发版流程大概是这样的：CI 构建镜像、推送到镜像仓库，然后 Jenkins Pipeline 执行 `kubectl set image` 更新 Deployment。表面上看没什么问题，但随着环境数量增加（测试、预发、多套生产），问题开始暴露出来。

### 配置漂移

最典型的问题：某人在排查问题时直接 `kubectl edit deployment` 改了副本数或环境变量，没有同步回仓库。几周后另一个同事做发布，把这个"临时修改"覆盖掉了，问题重新出现，排查了半天才发现原因。

`kubectl set image` 这类命令改的是集群里的实际状态，但 Git 里的 YAML 文件并不知道这件事。时间久了，集群实际运行的配置和 Git 里的声明之间就产生了不可见的漂移。

### 环境一致性难以保证

多套环境，每套都有自己微妙的差别。测试环境的 replica 是 1，生产是 3；不同生产环境使用不同云厂商（AWS EKS / 阿里云 ACK），ingress class 不一样，storage class 不一样。以前这些差异散落在各种 Jenkins 脚本和 sed 命令里，没有一个地方能一眼看清楚"这个环境和那个环境到底有什么不同"。

### 回滚难题

传统 `kubectl rollout undo` 只能回滚镜像，如果这次发布同时改了 ConfigMap，回滚不会帮你还原 ConfigMap。想完整回滚必须找到上一个版本的 YAML 文件手动 apply，但你得先找到它在哪里。

### 审计与变更追踪

"这个配置是谁改的、什么时候改的、为什么改"——这些问题在传统模式下基本无解，除非你的团队非常自律地维护 changelog。而 GitOps 把所有变更都记录在 Git 提交历史里，`git log` 和 `git blame` 就是天然的审计日志。

---

## 技术选型

### ArgoCD vs Flux

| 维度 | ArgoCD | Flux v2 |
|------|--------|---------|
| UI | 有完整 Web UI，直观 | 无官方 UI（有第三方） |
| 多集群管理 | 原生支持，一个 ArgoCD 管多个集群 | 需要额外配置 |
| 同步模式 | Pull + Reconcile | Pull + Reconcile |
| Kustomize 支持 | 原生内置 | 原生内置 |
| Helm 支持 | 支持（HelmRelease 方式） | 支持（HelmRelease CRD） |
| 学习曲线 | 相对平缓，UI 降低门槛 | 纯 GitOps 哲学，更"原教旨" |
| 社区活跃度 | 非常活跃，CNCF 毕业项目 | 活跃，CNCF 毕业项目 |
| 通知能力 | 原生 notifications controller | 需要额外配置 |

我们选了 ArgoCD，核心原因是 Web UI。团队里不是所有人都熟悉 CLI，UI 让非 DevOps 成员也能看到各服务的同步状态、健康状态，降低了沟通成本。多集群场景下 ArgoCD 的体验也更顺畅——一个 ArgoCD 实例部署在阿里云 ACK，同时管理 AWS EKS 的多个集群。

### Kustomize vs Helm

这两个不是完全对立的选项，但针对我们的场景做了权衡：

**Helm 的问题**：
- Chart 模板语法复杂，`{{ if .Values.xxx }}{{ end }}` 嵌套深了可读性很差
- 自定义资源需要用 `_helpers.tpl`，调试困难
- values.yaml 覆盖层次多了容易搞不清楚最终渲染结果是什么

**Kustomize 的优势**：
- 纯 YAML，没有模板语法，看到什么就是什么
- `kustomize build` 可以随时预览最终输出
- `patches` 机制让环境差异表达得很清晰——base 是通用的，overlay 只写差异
- kubectl 内置支持（`kubectl apply -k`），不需要额外安装

选择 Kustomize 还有一个现实原因：我们的服务大多是内部开发的，没有"发布 Chart 给别人用"的需求，Helm 的打包分发能力对我们是多余的。

---

## 仓库目录结构设计

这是整个 GitOps 体系里最重要的决策，结构设计得不好后面改起来很痛。

### Monorepo 方案

我们把所有服务的 K8s 配置放在一个仓库里（gitops-repo），而不是每个服务一个仓库。原因：

1. ArgoCD 轮询仓库有频率限制，多仓库意味着多个 webhook 和轮询连接
2. 跨服务的关联变更可以在一个 PR 里完成（比如同时更新 A 服务和它依赖的 ConfigMap）
3. 权限管理集中，只需要管好这一个仓库的分支保护规则

### 目录结构

```
gitops-repo/
├── base/
│   ├── service-a/
│   │   ├── deployment.yaml
│   │   ├── service.yaml
│   │   ├── hpa.yaml
│   │   └── kustomization.yaml
│   ├── service-b/
│   │   ├── deployment.yaml
│   │   ├── service.yaml
│   │   └── kustomization.yaml
│   └── infra/
│       ├── cert-manager/
│       └── ingress-nginx/
├── overlays/
│   ├── qa/
│   │   ├── service-a/
│   │   │   ├── kustomization.yaml
│   │   │   └── patches/
│   │   │       ├── deployment-replicas.yaml
│   │   │       └── hpa-minmax.yaml
│   │   └── service-b/
│   │       └── kustomization.yaml
│   ├── pre/
│   │   ├── service-a/
│   │   │   └── kustomization.yaml
│   │   └── service-b/
│   │       └── kustomization.yaml
│   ├── prod-aws/
│   │   ├── service-a/
│   │   │   ├── kustomization.yaml
│   │   │   └── patches/
│   │   │       ├── deployment-resources.yaml
│   │   │       └── ingress-class.yaml
│   │   └── service-b/
│   │       └── kustomization.yaml
│   └── prod-aliyun/
│       ├── service-a/
│       │   ├── kustomization.yaml
│       │   └── patches/
│       │       ├── deployment-resources.yaml
│       │       └── ingress-alb.yaml
│       └── service-b/
│           └── kustomization.yaml
└── argocd/
    ├── projects/
    │   ├── qa-project.yaml
    │   ├── pre-project.yaml
    │   └── prod-project.yaml
    └── applicationsets/
        ├── qa-appset.yaml
        ├── pre-appset.yaml
        ├── prod-aws-appset.yaml
        └── prod-aliyun-appset.yaml
```

**关键原则**：
- `base/` 只放通用配置，不能有任何环境特定的值（不能有 `namespace: production`）
- `overlays/` 只写差异，能在 base 里写的不要在 overlay 重复
- `argocd/` 目录存放 ArgoCD 自身的配置，这些资源也由 ArgoCD 管理（App of Apps 模式）

### base/ 的 kustomization.yaml

```yaml
# base/service-a/kustomization.yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization

resources:
  - deployment.yaml
  - service.yaml
  - hpa.yaml
```

base 里的 Deployment 不写 namespace，不写具体的副本数（或者写一个安全的默认值），镜像 tag 用 `latest` 占位，后续由 CI 更新：

```yaml
# base/service-a/deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: service-a
spec:
  replicas: 1
  selector:
    matchLabels:
      app: service-a
  template:
    metadata:
      labels:
        app: service-a
    spec:
      containers:
        - name: service-a
          image: 123456789.dkr.ecr.us-west-2.amazonaws.com/service-a:latest
          ports:
            - containerPort: 8080
          resources:
            requests:
              cpu: 100m
              memory: 128Mi
            limits:
              cpu: 500m
              memory: 512Mi
          env:
            - name: APP_ENV
              value: "default"
```

### overlays/ 各环境的 kustomization.yaml

**QA 环境**（最简化，副本数少，资源限制低）：

```yaml
# overlays/qa/service-a/kustomization.yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization

namespace: qa

resources:
  - ../../../base/service-a

images:
  - name: 123456789.dkr.ecr.us-west-2.amazonaws.com/service-a
    newTag: "a1b2c3d"  # 由 CI 更新

patches:
  - path: patches/deployment-replicas.yaml
  - path: patches/hpa-minmax.yaml
```

```yaml
# overlays/qa/service-a/patches/deployment-replicas.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: service-a
spec:
  replicas: 1
```

**生产环境（AWS）**（高可用，AWS 特定 ingress）：

```yaml
# overlays/prod-aws/service-a/kustomization.yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization

namespace: production

resources:
  - ../../../base/service-a

images:
  - name: 123456789.dkr.ecr.us-west-2.amazonaws.com/service-a
    newTag: "a1b2c3d"

patches:
  - path: patches/deployment-resources.yaml
  - path: patches/ingress-class.yaml
```

```yaml
# overlays/prod-aws/service-a/patches/deployment-resources.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: service-a
spec:
  replicas: 3
  template:
    spec:
      containers:
        - name: service-a
          resources:
            requests:
              cpu: 500m
              memory: 512Mi
            limits:
              cpu: 2000m
              memory: 2Gi
```

**生产环境（阿里云）**（阿里云 ACK，使用阿里云 ALB ingress）：

```yaml
# overlays/prod-aliyun/service-a/kustomization.yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization

namespace: production

resources:
  - ../../../base/service-a
  - ingress.yaml  # 阿里云环境独有的 ALB ingress，base 里没有

images:
  - name: registry.cn-hangzhou.aliyuncs.com/myorg/service-a
    newTag: "a1b2c3d"

patches:
  - path: patches/deployment-resources.yaml
  - path: patches/deployment-registry.yaml  # 替换镜像仓库地址
```

---

## Kustomize 关键用法

### Strategic Merge Patch vs JSON Patch

Kustomize 支持两种 patch 方式，选哪个取决于要改什么：

**Strategic Merge Patch**（推荐，大多数情况够用）：

```yaml
# 只写你要改的字段，其余字段会被保留
apiVersion: apps/v1
kind: Deployment
metadata:
  name: service-a
spec:
  replicas: 3
  template:
    spec:
      containers:
        - name: service-a
          env:
            - name: LOG_LEVEL
              value: "warn"
```

注意：对于 List 类型字段（比如 `containers`、`env`），Strategic Merge Patch 会按 key 字段合并，不是简单替换。`containers` 用 `name` 作为 merge key，`env` 用 `name` 作为 merge key。

**JSON Patch**（适合精确操作，比如删除某个字段）：

```yaml
# overlays/prod/patches/remove-debug.yaml
- op: remove
  path: /spec/template/spec/containers/0/env/2  # 删除第三个环境变量
```

```yaml
# kustomization.yaml 里引用 JSON Patch
patches:
  - path: patches/remove-debug.yaml
    target:
      kind: Deployment
      name: service-a
```

### configMapGenerator

直接在 kustomization.yaml 里生成 ConfigMap，还会自动添加内容 hash 后缀，让 Deployment 感知到 ConfigMap 变化：

```yaml
configMapGenerator:
  - name: service-a-config
    literals:
      - APP_ENV=production
      - LOG_LEVEL=info
    files:
      - config/app.properties

generatorOptions:
  disableNameSuffixHash: false  # 默认 false，会追加 hash，推荐保留
```

生成的 ConfigMap 名称会变成 `service-a-config-k8bcm9mh5b` 这样，Deployment 引用 ConfigMap 时，Kustomize 会自动替换成带 hash 的名称。好处是：ConfigMap 内容变化 → hash 变化 → Deployment 的 `volumes.configMap.name` 变化 → Deployment 触发滚动更新。

### images 字段：CI/CD 集成的关键

这是 CI 更新镜像 tag 的标准方式：

```yaml
# kustomization.yaml
images:
  - name: 123456789.dkr.ecr.us-west-2.amazonaws.com/service-a
    newTag: "abc1234"
```

CI 里用 `kustomize edit set image` 更新，不用手动 sed 替换 YAML：

```bash
cd overlays/qa/service-a
kustomize edit set image \
  123456789.dkr.ecr.us-west-2.amazonaws.com/service-a=123456789.dkr.ecr.us-west-2.amazonaws.com/service-a:${GIT_SHA}
```

也可以用 `newName` 同时替换仓库地址：

```yaml
images:
  - name: service-a  # base 里用短名
    newName: 123456789.dkr.ecr.us-west-2.amazonaws.com/service-a
    newTag: "abc1234"
```

### namePrefix / nameSuffix

如果想让同一套配置部署到同一个 namespace 的不同实例（比如蓝绿部署），可以用 namePrefix：

```yaml
namePrefix: blue-
# 所有资源名称都会变成 blue-service-a, blue-service-a-config 等
```

生产环境我们用得不多，主要是 QA 环境有时候需要同时跑多个版本做对比测试。

### 验证构建结果

在提交前养成习惯，先 `kustomize build` 看看最终输出：

```bash
# 预览 QA 环境的 service-a 最终 YAML
kustomize build overlays/qa/service-a

# 和上一个版本做 diff
kustomize build overlays/qa/service-a | kubectl diff -f - --context=qa-cluster
```

---

## ArgoCD 配置

### Application 资源

最基本的 Application 定义：

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: service-a-qa
  namespace: argocd
  finalizers:
    - resources-finalizer.argocd.argoproj.io  # 删除 App 时级联删除 K8s 资源
spec:
  project: qa-project

  source:
    repoURL: https://github.com/myorg/gitops-repo
    targetRevision: main
    path: overlays/qa/service-a

  destination:
    server: https://kubernetes.default.svc  # 本集群
    namespace: qa

  syncPolicy:
    automated:
      prune: true      # 删除 Git 里已移除的资源
      selfHeal: true   # 发现漂移自动修复
    syncOptions:
      - CreateNamespace=true  # namespace 不存在时自动创建
      - PrunePropagationPolicy=foreground
      - RespectIgnoreDifferences=true
    retry:
      limit: 3
      backoff:
        duration: 5s
        factor: 2
        maxDuration: 3m
```

**`prune: true` 要谨慎**：开启后，如果你从 kustomization.yaml 里移除了某个资源（比如一个 Service），下次同步时 ArgoCD 会把集群里对应的 Service 删掉。这是期望行为，但如果手滑把资源从 Git 里删了，可能造成意外中断。建议生产环境把 `automated` 去掉，改为手动触发同步，或者至少把 `prune` 设为 false，删除资源单独操作。

**`selfHeal: true`**：有人直接 `kubectl edit` 改了集群资源，ArgoCD 会在下次 reconcile 时（默认 3 分钟）把改动回滚回 Git 里的状态。这是 GitOps 的核心保障，但刚开始用的时候团队需要适应"所有改动必须走 Git"的习惯。

### ignoreDifferences

有些字段是 K8s 控制器自动填充的，或者你故意不想被 ArgoCD 管理，可以忽略：

```yaml
spec:
  ignoreDifferences:
    - group: apps
      kind: Deployment
      jsonPointers:
        - /spec/replicas  # 如果你用了 HPA，replicas 由 HPA 管理，不要让 ArgoCD 覆盖
    - group: ""
      kind: ConfigMap
      name: service-a-generated
      jsonPointers:
        - /data  # 某些 CM 内容由运行时生成
```

### ApplicationSet：自动化管理多环境

手动为每个服务每个环境创建 Application 资源太繁琐，ApplicationSet 可以按规则自动生成。

**List Generator**（适合环境数量固定、配置差异明显的场景）：

```yaml
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: service-a-appset
  namespace: argocd
spec:
  generators:
    - list:
        elements:
          - env: qa
            cluster: https://qa-eks.example.com
            namespace: qa
            revision: main
          - env: pre
            cluster: https://pre-eks.example.com
            namespace: pre
            revision: main
          - env: prod-aws
            cluster: https://prod-aws-eks.example.com
            namespace: production
            revision: main
          - env: prod-aliyun
            cluster: https://prod-aliyun-ack.example.com
            namespace: production
            revision: main

  template:
    metadata:
      name: "service-a-{{env}}"
      namespace: argocd
    spec:
      project: "{{env}}-project"
      source:
        repoURL: https://github.com/myorg/gitops-repo
        targetRevision: "{{revision}}"
        path: "overlays/{{env}}/service-a"
      destination:
        server: "{{cluster}}"
        namespace: "{{namespace}}"
      syncPolicy:
        automated:
          prune: true
          selfHeal: true
        syncOptions:
          - CreateNamespace=true
```

**Git Generator**（适合服务数量多、目录结构规律的场景）：

```yaml
spec:
  generators:
    - git:
        repoURL: https://github.com/myorg/gitops-repo
        revision: main
        directories:
          - path: overlays/qa/*  # 自动发现 overlays/qa/ 下的所有子目录
```

Git Generator 会把每个发现的目录路径作为一个元素，生成对应的 Application。新增服务只需要在 Git 里创建目录，ApplicationSet 会自动发现并创建 Application，不需要手动操作 ArgoCD。

### ArgoCD Project

Project 用来做隔离和权限控制：

```yaml
apiVersion: argoproj.io/v1alpha1
kind: AppProject
metadata:
  name: qa-project
  namespace: argocd
spec:
  description: QA 环境项目

  sourceRepos:
    - "https://github.com/myorg/gitops-repo"

  destinations:
    - namespace: qa
      server: https://qa-eks.example.com

  clusterResourceWhitelist:
    - group: ""
      kind: Namespace

  namespaceResourceBlacklist:
    - group: ""
      kind: ResourceQuota  # QA 不允许修改 ResourceQuota

  roles:
    - name: developer
      description: 开发人员可以同步但不能删除
      policies:
        - p, proj:qa-project:developer, applications, sync, qa-project/*, allow
        - p, proj:qa-project:developer, applications, get, qa-project/*, allow
      groups:
        - myorg:developers
```

---

## CI/CD 集成

整个流程分两个阶段：CI 负责构建和推送镜像，然后更新 GitOps 仓库的镜像 tag；ArgoCD 检测到 Git 变化后自动同步到集群。

### CI 阶段（以 GitHub Actions 为例）

```yaml
# .github/workflows/build-and-deploy.yaml
name: Build and Deploy

on:
  push:
    branches: [main]

jobs:
  build:
    runs-on: ubuntu-latest
    outputs:
      image-tag: ${{ steps.tag.outputs.tag }}
    steps:
      - uses: actions/checkout@v4

      - name: Generate image tag
        id: tag
        run: echo "tag=$(git rev-parse --short HEAD)" >> $GITHUB_OUTPUT

      - name: Configure AWS credentials
        uses: aws-actions/configure-aws-credentials@v4
        with:
          aws-access-key-id: ${{ secrets.AWS_ACCESS_KEY_ID }}
          aws-secret-access-key: ${{ secrets.AWS_SECRET_ACCESS_KEY }}
          aws-region: us-west-2

      - name: Login to ECR
        uses: aws-actions/amazon-ecr-login@v2

      - name: Build and push image
        env:
          ECR_REGISTRY: 123456789.dkr.ecr.us-west-2.amazonaws.com
          IMAGE_NAME: service-a
          IMAGE_TAG: ${{ steps.tag.outputs.tag }}
        run: |
          docker build -t $ECR_REGISTRY/$IMAGE_NAME:$IMAGE_TAG .
          docker push $ECR_REGISTRY/$IMAGE_NAME:$IMAGE_TAG

  update-gitops:
    needs: build
    runs-on: ubuntu-latest
    steps:
      - name: Checkout GitOps repo
        uses: actions/checkout@v4
        with:
          repository: myorg/gitops-repo
          token: ${{ secrets.GITOPS_TOKEN }}

      - name: Install kustomize
        run: |
          curl -s "https://raw.githubusercontent.com/kubernetes-sigs/kustomize/master/hack/install_kustomize.sh" | bash
          sudo mv kustomize /usr/local/bin/

      - name: Update image tag in QA overlay
        env:
          IMAGE_TAG: ${{ needs.build.outputs.image-tag }}
          ECR_REGISTRY: 123456789.dkr.ecr.us-west-2.amazonaws.com
        run: |
          cd overlays/qa/service-a
          kustomize edit set image \
            $ECR_REGISTRY/service-a=$ECR_REGISTRY/service-a:$IMAGE_TAG

      - name: Commit and push
        run: |
          git config user.name "ci-bot"
          git config user.email "ci-bot@myorg.com"
          git add .
          git commit -m "chore: update service-a to ${{ needs.build.outputs.image-tag }}"
          git push
```

生产环境的镜像 tag 更新通常不直接从 CI 推，而是通过 PR 的方式——CI 创建一个 PR 更新生产环境的镜像 tag，人工 review 后合并，ArgoCD 才会自动同步。这多了一层人工确认的保障。

### CD 阶段

ArgoCD 配置了 webhook，GitHub 推送后几秒内 ArgoCD 就能检测到变化并开始同步。如果没有配置 webhook，默认是 3 分钟轮询一次。

可以用 argocd CLI 手动触发同步（适合紧急发布）：

```bash
argocd app sync service-a-qa --prune
```

---

## 踩坑记录

这部分是真实踩过的坑，文档里一般不会告诉你这些。

### 1. Kustomize patches 路径写错导致静默失败

**现象**：修改了 overlay 里的 patch 文件，推送后 ArgoCD 显示同步成功，但集群里的资源没有变化。

**原因**：`kustomization.yaml` 里的 `patches` 路径写错了，指向了一个不存在的文件，但 Kustomize 某些版本不会报错，直接忽略了这个 patch。

```yaml
patches:
  - path: patches/deployment-replicas.yaml  # 实际文件是 patch/deployment-replicas.yaml
```

**解法**：养成在提交前 `kustomize build` 验证的习惯。如果 patch 文件不存在，新版本的 Kustomize 会报错，但不要依赖这个行为，显式验证更安全。

还有一个更隐蔽的变体：patch 文件存在，但 patch 的目标资源 `metadata.name` 写错了，导致 patch 没有匹配到任何资源。比如 base 里资源名是 `service-a`，但 patch 文件里写成了 `service_a`（下划线），strategic merge patch 找不到目标，静默跳过。

### 2. argocd sync 卡住不动

**现象**：`argocd app sync` 命令执行后，应用状态变成 `Syncing`，但一直没有完成，等了很久才超时报错。

**常见原因**：

**PreSync / Sync Hook 挂了**：如果你用了 `argocd.argoproj.io/hook: PreSync` 的 Job，Job 失败了会导致整个同步卡住（取决于 `argocd.argoproj.io/hook-delete-policy`）。检查：

```bash
# 查看 hook job 状态
kubectl get jobs -n qa -l app.kubernetes.io/managed-by=Helm
kubectl describe job <job-name> -n qa
```

**资源 Finalizer 死锁**：某个资源有 Finalizer，但控制器已经不存在了，资源删不掉，同步卡住。解法：

```bash
# 手动清除 Finalizer（谨慎操作）
kubectl patch <resource> <name> -n <ns> \
  --type=json \
  -p='[{"op": "remove", "path": "/metadata/finalizers"}]'
```

**Webhook 证书问题**：如果有 validating/mutating webhook，证书过期或 webhook service 不可用，`kubectl apply` 会被拒绝，ArgoCD 也会卡住。

```bash
# 检查 webhook
kubectl get validatingwebhookconfigurations
kubectl get mutatingwebhookconfigurations
```

### 3. 多集群 ArgoCD：主集群在阿里云，管理 AWS 集群

我们的 ArgoCD 部署在阿里云 ACK 上，需要管理 AWS EKS 集群。注册外部集群的步骤：

```bash
# 在本机（或 CI），kubeconfig 里需要同时有两个集群的 context
# 确保 argocd CLI 登录的是阿里云上的 ArgoCD
argocd login argocd.internal.myorg.com

# 注册 AWS EKS 集群
argocd cluster add aws-eks-us-west-2 \
  --kubeconfig ~/.kube/config \
  --name prod-aws-eks

# 验证
argocd cluster list
```

**踩到的坑**：EKS 的 kubeconfig 使用 `aws eks get-token` 命令生成临时 token，这个 token 有效期只有 15 分钟。argocd 注册集群时会把这个 ServiceAccount token 存在 argocd namespace 下的 Secret 里，但如果注册时使用的是你的个人 IAM 身份，argocd 的 controller 后续无法续期 token。

正确做法：在 EKS 集群里创建专用 ServiceAccount，绑定足够权限，用 SA token 注册，而不是用 `aws eks get-token`：

```bash
# 在 EKS 集群里创建 SA
kubectl create serviceaccount argocd-manager -n kube-system
kubectl create clusterrolebinding argocd-manager \
  --clusterrole=cluster-admin \
  --serviceaccount=kube-system:argocd-manager

# 获取 SA token（K8s 1.24+ 需要手动创建）
kubectl create token argocd-manager -n kube-system --duration=87600h

# 用 bearer token 注册
argocd cluster add <cluster-context> \
  --name prod-aws-eks \
  --bearer-token <token> \
  --server https://<eks-endpoint>
```

### 4. ApplicationSet 更新不触发同步

**现象**：修改了 ApplicationSet 里的某个字段（比如 `syncPolicy`），但已存在的 Application 没有更新。

**原因**：ApplicationSet controller 负责创建和删除 Application，但**不会修改**已经存在的 Application 的所有字段（具体哪些字段受控取决于版本和配置）。

**解法**：
- 在 ApplicationSet 的 `syncPolicy` 加上 `preservedFields`，明确哪些字段由用户管理
- 或者删除对应的 Application 让 ApplicationSet 重新创建
- 检查 applicationset-controller 的日志确认是否有相关警告

```bash
kubectl logs -n argocd \
  -l app.kubernetes.io/component=applicationset-controller \
  --tail=100
```

### 5. Secret 管理：不能明文存 GitOps 仓库

这是很多团队最开始犯的错误：把 Secret 的明文 YAML 放进 GitOps 仓库，然后发现 GitHub 告警说仓库里有敏感信息。

**我们用的方案：Sealed Secrets**

Sealed Secrets 由 Bitnami 开源，分两个组件：
- `sealed-secrets-controller`：部署在集群里，持有解密私钥
- `kubeseal`：CLI 工具，用集群的公钥加密 Secret，生成 SealedSecret 资源

加密后的 SealedSecret 可以安全地提交到 Git，只有对应集群的 controller 能解密：

```bash
# 安装 kubeseal
brew install kubeseal

# 获取集群公钥
kubeseal --fetch-cert \
  --controller-namespace=sealed-secrets \
  --controller-name=sealed-secrets-controller \
  > cluster-cert.pem

# 加密 Secret
kubectl create secret generic db-password \
  --from-literal=password=supersecret123 \
  --dry-run=client \
  -o yaml | \
  kubeseal \
    --cert cluster-cert.pem \
    --format yaml \
    > overlays/qa/service-a/sealed-db-password.yaml
```

生成的 `sealed-db-password.yaml` 长这样：

```yaml
apiVersion: bitnami.com/v1alpha1
kind: SealedSecret
metadata:
  name: db-password
  namespace: qa
spec:
  encryptedData:
    password: AgBy3i4OJSWK+PiTySYZZA9rO43cGDEq...（加密后的内容）
  template:
    metadata:
      name: db-password
      namespace: qa
    type: Opaque
```

提交到 Git，ArgoCD 同步后，controller 自动解密并创建对应的 Secret。

**注意**：Sealed Secrets 是按集群（或按 namespace）加密的，一个环境的 SealedSecret 在另一个环境的集群里无法解密。每个环境需要单独加密。

另一个方案是 **External Secrets Operator**，从 AWS Secrets Manager / Vault / 阿里云 KMS 等外部存储读取 Secret，更适合已经有集中 Secret 管理系统的团队。

### 6. HPA 与 ArgoCD 的副本数冲突

如果服务开启了 HPA，HPA 会动态调整 replica 数。ArgoCD 同步时会把 Deployment 的 `spec.replicas` 改回 Git 里的值，然后 HPA 再改回去，产生频繁的 reconcile 循环。

解法是在 Application 的 `ignoreDifferences` 里忽略 `spec.replicas`：

```yaml
spec:
  ignoreDifferences:
    - group: apps
      kind: Deployment
      jsonPointers:
        - /spec/replicas
```

或者在 `kustomization.yaml` 里直接不设置 replicas，让 HPA 完全掌控。但注意：如果 HPA 被删了，Deployment 会保持上次 HPA 设置的副本数，可能不是你期望的默认值。

---

## 常用 argocd CLI 命令速查

```bash
# 登录
argocd login argocd.internal.myorg.com --sso

# 查看所有应用状态
argocd app list

# 查看单个应用详情（包括同步状态、健康状态、资源列表）
argocd app get service-a-qa

# 手动触发同步
argocd app sync service-a-qa

# 同步并强制删除不在 Git 里的资源
argocd app sync service-a-qa --prune

# 预览变更（不实际同步，很有用）
argocd app diff service-a-qa

# 回滚到上一个版本
argocd app rollback service-a-qa

# 回滚到指定历史版本（先查 history）
argocd app history service-a-qa
argocd app rollback service-a-qa <history-id>

# 暂停自动同步（紧急情况下临时关闭自动同步）
argocd app set service-a-qa --sync-policy none

# 恢复自动同步
argocd app set service-a-qa --sync-policy automated

# 手动刷新（强制 ArgoCD 重新从 Git 拉取，不等轮询）
argocd app get service-a-qa --refresh

# 强制刷新（清除缓存，适合 Helm chart 有变化但没检测到的情况）
argocd app get service-a-qa --hard-refresh

# 删除应用（加 --cascade 会同时删除 K8s 资源）
argocd app delete service-a-qa --cascade

# 查看所有集群
argocd cluster list

# 查看同步失败的原因
argocd app get service-a-qa -o json | jq '.status.conditions'

# 管理 Project
argocd proj list
argocd proj get qa-project
argocd proj role list qa-project
```

---

## 一些运维习惯

落地 GitOps 之后，有几个习惯能让日常运维更顺：

**所有变更走 PR**：即使是紧急修复，也要 PR + Squash Merge，保持 Git 历史干净。紧急程度不是绕过 PR 的理由，而是减少 Review 等待时间的理由（比如只要一个人 approve 就合）。

**保持 base 精简**：base 只放所有环境共用的内容，遇到"这个字段大多数环境都一样，只有一个环境不同"的情况，还是把这个字段放 overlay 里，base 里删掉。不然 base 里的值会变成一个隐藏的"默认值"，新来的同学很容易误解。

**定期 `kustomize build` 验证**：在 CI 里加一步 `kustomize build` 检查，确保所有 overlay 都能正常构建，防止有人改了 base 资源名称但没有更新 patch 里的 target。

**ArgoCD 的 notification 配置起来**：同步失败、健康状态变化及时推送到 IM（我们用钉钉），不然靠人工看 UI 发现问题太慢。

---

## 参考链接

- [ArgoCD 官方文档](https://argo-cd.readthedocs.io/en/stable/)
- [Kustomize 官方文档](https://kubectl.docs.kubernetes.io/references/kustomize/)
- [ApplicationSet Controller 文档](https://argocd-applicationset.readthedocs.io/en/stable/)
- [Sealed Secrets GitHub](https://github.com/bitnami-labs/sealed-secrets)
- [External Secrets Operator](https://external-secrets.io/)
- [ArgoCD 最佳实践](https://argo-cd.readthedocs.io/en/stable/user-guide/best_practices/)
- [Kustomize Strategic Merge Patch 说明](https://kubectl.docs.kubernetes.io/references/kustomize/kustomization/patches/)
