---
title: "GitOps 落地实战：ArgoCD + Kustomize 多环境管理"
date: 2026-04-06T10:00:00+08:00
draft: false
tags: ["GitOps", "ArgoCD", "Kustomize", "DevOps", "Kubernetes"]
categories: ["Kubernetes"]
description: "从 GitOps 理念到 ArgoCD + Kustomize 的实际落地，覆盖多集群多环境管理与常见故障排查"
summary: "GitOps 不只是「把配置放 Git 里」，真正落地需要解决 overlay 结构设计、ApplicationSet 管理多集群、image updater 自动化，以及 sync wave、resource hook 这些细节。这篇文章记录我们团队从传统 CI/CD 迁移到 GitOps 的实际过程。"
toc: true
math: false
diagram: false
keywords: ["GitOps", "ArgoCD", "Kustomize", "ApplicationSet", "image updater", "多环境部署"]
params:
  reading_time: true
---

我们团队在过去一年把所有 K8s 服务迁移到了 GitOps 体系，用 ArgoCD + Kustomize 管理横跨 US/CN 两个生产集群、QA 和 PRE 环境共四套环境的几十个服务。这篇文章不打算讲 GitOps 的概念，而是聚焦在落地过程中真正遇到的问题。

## GitOps vs 传统 CI/CD 的本质区别

传统 CI/CD 的模型是「推送」：CI 流水线构建镜像后，通过 `kubectl apply` 或 Helm 命令把变更推送到集群。这意味着流水线需要持有集群凭据，而且「集群实际运行的状态」和「代码仓库里的配置」之间没有强制约束关系——有人直接 `kubectl edit` 改了什么，没人知道。

GitOps 翻转了这个模型：**Git 仓库是唯一的 source of truth，集群从 Git 拉取配置并主动对齐**。ArgoCD 持续 watch Git 仓库，发现 drift 就自动或提示修复。

这带来几个实际好处：

1. **审计可追溯**：所有变更都经过 PR，谁改了什么一目了然
2. **集群凭据不出 CI**：CI 只负责构建镜像、更新 Git 里的 image tag，不直接操作集群
3. **多集群一致性**：同一份 base 配置 + overlay 差异，保证环境间配置结构统一

代价是引入了新的复杂度：需要维护一个专门的 gitops 仓库，配置变更要走 PR 流程，紧急修复时的摩擦比直接 `kubectl apply` 大一些。

## Kustomize Overlay 结构设计

我们的 gitops 仓库目录结构大致如下：

```
gitops/
├── base/
│   ├── namespace.yaml
│   ├── deployment.yaml
│   ├── service.yaml
│   ├── hpa.yaml
│   └── kustomization.yaml
└── overlays/
    ├── qa/
    │   ├── kustomization.yaml
    │   ├── deployment-patch.yaml
    │   └── configmap.yaml
    ├── pre/
    │   ├── kustomization.yaml
    │   └── deployment-patch.yaml
    ├── prod-us/
    │   ├── kustomization.yaml
    │   ├── deployment-patch.yaml
    │   └── hpa-patch.yaml
    └── prod-cn/
        ├── kustomization.yaml
        └── deployment-patch.yaml
```

`base/kustomization.yaml` 声明基础资源：

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization

resources:
  - namespace.yaml
  - deployment.yaml
  - service.yaml
  - hpa.yaml

commonLabels:
  app.kubernetes.io/managed-by: kustomize
```

`base/deployment.yaml` 用占位符，不含环境特定配置：

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: goalfy-api
  namespace: goalfy
spec:
  replicas: 1
  selector:
    matchLabels:
      app: goalfy-api
  template:
    metadata:
      labels:
        app: goalfy-api
    spec:
      containers:
        - name: api
          image: your-registry/goalfy-api:latest
          ports:
            - containerPort: 8080
          resources:
            requests:
              cpu: 100m
              memory: 128Mi
            limits:
              cpu: 500m
              memory: 512Mi
```

`overlays/prod-us/kustomization.yaml` 引用 base 并打补丁：

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization

resources:
  - ../../base

namespace: goalfy-prod

images:
  - name: your-registry/goalfy-api
    newTag: "v1.2.3"   # 由 image updater 自动更新这一行

patches:
  - path: deployment-patch.yaml
  - path: hpa-patch.yaml
```

`overlays/prod-us/deployment-patch.yaml` 只覆盖需要差异化的字段：

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: goalfy-api
spec:
  replicas: 3
  template:
    spec:
      containers:
        - name: api
          resources:
            requests:
              cpu: 500m
              memory: 512Mi
            limits:
              cpu: 2000m
              memory: 2Gi
          env:
            - name: APP_ENV
              value: production
            - name: DB_HOST
              valueFrom:
                secretKeyRef:
                  name: db-credentials
                  key: host
```

这种结构的优势在于：QA 环境的 replica 是 1，prod 是 3，资源配额不同，但 Deployment 的核心结构（labels、probe 配置、容器名）保持一致。如果 base 里加了新的环境变量，所有 overlay 自动继承，不需要每个环境单独加。

## ArgoCD ApplicationSet 管理多集群多环境

单个 Application 只能管一个集群的一套配置。我们有 4 个环境 × N 个服务，如果每个都手动创建 Application，管理成本很高。ApplicationSet 解决了这个问题。

我们用 `matrix generator` 把服务列表和环境列表做笛卡尔积：

```yaml
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: goalfy-services
  namespace: argocd
spec:
  generators:
    - matrix:
        generators:
          - list:
              elements:
                - service: goalfy-api
                - service: goalfy-worker
                - service: goalfy-scheduler
          - list:
              elements:
                - env: qa
                  cluster: https://qa-cluster-endpoint
                  namespace: goalfy-qa
                - env: pre
                  cluster: https://pre-cluster-endpoint
                  namespace: goalfy-pre
                - env: prod-us
                  cluster: https://prod-us-endpoint
                  namespace: goalfy-prod
  template:
    metadata:
      name: "{{service}}-{{env}}"
    spec:
      project: goalfy
      source:
        repoURL: https://github.com/your-org/gitops
        targetRevision: main
        path: "services/{{service}}/overlays/{{env}}"
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

这样 3 个服务 × 3 个环境自动生成 9 个 Application，新增服务只需要在 `elements` 里加一行，并在 gitops 仓库里创建对应的 overlay 目录。

`selfHeal: true` 很重要——它保证即使有人直接 `kubectl edit` 修改了集群状态，ArgoCD 会在下个 sync 周期把它恢复回 Git 里的状态。这是 GitOps 「防漂移」的核心机制。

## Image Updater 自动更新镜像

手动更新 `kustomization.yaml` 里的 image tag 是低价值重复工作。ArgoCD Image Updater 监听镜像仓库，自动提交 PR 或直接更新。

配置方式是在 Application 里加 annotation：

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: goalfy-api-qa
  annotations:
    argocd-image-updater.argoproj.io/image-list: api=your-registry/goalfy-api
    argocd-image-updater.argoproj.io/api.update-strategy: semver
    argocd-image-updater.argoproj.io/api.allow-tags: regexp:^v[0-9]+\.[0-9]+\.[0-9]+$
    argocd-image-updater.argoproj.io/write-back-method: git
    argocd-image-updater.argoproj.io/git-branch: main
```

`update-strategy: semver` 表示自动升级到最新的语义版本。对于 QA 环境，我们用 `latest` 策略，每次有新镜像推送就自动更新；对于 prod，用 `semver` 并限制只跟随 patch 版本，minor/major 升级需要手动触发。

write-back 模式建议用 `git` 而不是 `argocd`，前者会提交 commit 到仓库，变更有记录；后者直接在 ArgoCD 内部修改，不留 git 历史。

## 常见问题：Sync Wave、Resource Hook、Sync Policy

### Sync Wave 顺序依赖

当一个应用有多个资源，且有依赖顺序时（比如要先创建 ConfigMap 再创建 Deployment），用 `sync-wave` 注解控制：

```yaml
# ConfigMap 先同步（wave 0）
metadata:
  annotations:
    argocd.argoproj.io/sync-wave: "0"

---
# Deployment 后同步（wave 1）
metadata:
  annotations:
    argocd.argoproj.io/sync-wave: "1"
```

wave 值越小越先同步。同一 wave 内的资源并行同步。

注意：wave 只控制同步顺序，不等待前一个 wave 的资源「就绪」。如果 ConfigMap 创建成功但 Deployment 依赖的 Secret 还没就绪，Deployment 的 Pod 还是会因为 Secret 不存在而启动失败。需要真正的就绪等待，要用 Resource Hook。

### Resource Hook

Resource Hook 允许在 sync 的特定阶段执行 Job：

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: db-migration
  annotations:
    argocd.argoproj.io/hook: PreSync          # 在 sync 开始前执行
    argocd.argoproj.io/hook-delete-policy: BeforeHookCreation  # 下次 sync 前清理上次的 Job
spec:
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: migrate
          image: your-registry/goalfy-api:latest
          command: ["python", "manage.py", "migrate"]
          env:
            - name: DB_URL
              valueFrom:
                secretKeyRef:
                  name: db-credentials
                  key: url
```

常用的 hook 类型：
- `PreSync`：sync 开始前，适合数据库迁移
- `PostSync`：所有资源 sync 成功后，适合冒烟测试、通知
- `SyncFail`：sync 失败时，适合告警或回滚逻辑

### Sync Policy 配置细节

```yaml
syncPolicy:
  automated:
    prune: true        # 删除 Git 中不存在的资源
    selfHeal: true     # 自动修复 drift
  retry:
    limit: 5
    backoff:
      duration: 5s
      factor: 2
      maxDuration: 3m
  syncOptions:
    - CreateNamespace=true
    - PrunePropagationPolicy=foreground   # 级联删除
    - RespectIgnoreDifferences=true
```

`prune: true` 要谨慎。如果有人在集群里手动创建了 ArgoCD Application 没有管到的资源，一旦开启 prune 且 ArgoCD 认为那个资源属于这个 app，就会被删掉。建议先开着 `selfHeal` 但关闭 `prune`，观察一段时间，确认没有意外资源后再打开。

## ArgoCD 同步失败排查思路

遇到 sync 失败，按以下顺序排查：

**1. 看 ArgoCD UI 的 sync 日志**

最直接，通常会明确告诉你哪个资源报错，报什么错。常见的是 webhook 超时、资源 schema 不匹配、namespace 不存在。

**2. 检查 diff**

```bash
argocd app diff <app-name>
```

这会显示 ArgoCD 计算出的「期望状态」和「实际状态」的差异，有时候能发现意外的 annotation 或 label 被其他控制器加上去导致 drift。

**3. 手动 dry-run**

```bash
kubectl apply --dry-run=server -f <manifest>
```

有些错误只有在真正提交给 APIServer 时才会出现（比如 CRD 版本不对、ValidatingWebhookConfiguration 拦截）。

**4. 检查 RBAC**

ArgoCD 的 service account 没有某个资源的操作权限时，会静默失败或报 forbidden。检查 ArgoCD 使用的 ClusterRole 是否覆盖了新引入的 CRD。

**5. Kustomize 渲染错误**

本地重现：

```bash
kustomize build overlays/prod-us/
```

如果本地渲染失败，ArgoCD 也会失败。常见原因是 patch 的字段路径写错、引用了不存在的 base 资源。

---

GitOps 落地初期会有一段适应期，团队成员习惯了直接 `kubectl apply` 的工作方式，切换到「所有变更必须走 Git」有摩擦。但跑了几个月后，最大的感受是**生产事故的排查效率显著提升**——出问题了，直接看 git log，哪个 commit 之后出问题的，一目了然，回滚也就是 `git revert` 加上一个 ArgoCD sync。
