---
title: "Kyverno 策略即代码实战：从准入到变异到生成的全场景落地"
date: 2025-11-28T10:00:00+08:00
draft: false
tags: ["Kyverno", "Policy as Code", "admission", "Kubernetes", "治理"]
categories: ["云原生"]
description: "一份基于 Kyverno 1.12+ 的生产落地笔记：覆盖 validate/mutate/generate/verifyImages 四种策略类型的实战用法、CEL 和 JMESPath 表达式语法、策略分层治理、PolicyException、性能调优和常见踩坑，并与 OPA Gatekeeper 做对比。"
summary: "一份基于 Kyverno 1.12+ 的生产落地笔记：覆盖 validate/mutate/generate/verifyImages 四种策略类型的实战用法、CEL 和 JMESPath 表达式语法、策略分层治理、PolicyException、性能调优和常见踩坑，并与 OPA Gatekeeper 做对比。"
toc: true
math: false
diagram: false
keywords: ["Kyverno", "policy as code", "admission controller", "Kubernetes 治理"]
params:
  reading_time: true
---

## 为什么是 Kyverno

三年前我给 K8s 集群做策略治理时还在用 OPA Gatekeeper。那是一段痛苦的经历——每写一条策略要学 Rego，新人入门要一周，debug 要打日志慢慢看。有次我们改了一条 Rego 发现有个 constraint 没生效，查了半天发现是 ConstraintTemplate 的字段类型写错了，但 Gatekeeper 默默失败没有报错。我当时的感觉是："写一个拒绝 latest tag 的策略不应该这么复杂。"

后来切到 Kyverno。第一条策略 5 分钟就跑起来了，而且全用 YAML 表达，团队里不懂 Rego 的人也能看懂。这是 Kyverno 对 Gatekeeper 最本质的优势：**语法是 K8s 原生的**。你不需要学一门新语言，你只需要理解 admission controller 的工作方式。

这篇是我在 Kyverno 1.12+ 上的生产落地经验，涵盖四种策略类型、CEL 表达式、PolicyException、性能调优、和其他工具的组合。

## 一、Kyverno 架构与核心概念

### 1.1 四种策略类型

Kyverno 能做的事情远比 Gatekeeper 多。主要有四种策略类型：

1. **Validate**：验证资源是否符合规则，不符合则拒绝
2. **Mutate**：修改资源，比如自动加 label、补默认值
3. **Generate**：根据某个资源事件生成其他资源，比如创建 namespace 时自动下发默认 NetworkPolicy
4. **VerifyImages**：验证镜像签名（Cosign/Sigstore 集成）

Gatekeeper 只做 validate 和 mutate。Generate 是 Kyverno 独有的杀手锏——用它能把很多"运营规范下发"的事情完全自动化。

### 1.2 策略资源

```
ClusterPolicy       # 全集群生效
Policy              # 单 namespace 生效
PolicyException     # 例外豁免
PolicyReport        # 策略结果报告 (background scan 输出)
ClusterPolicyReport
AdmissionReport     # 准入阶段的即时报告
```

生产里主要用 ClusterPolicy。Policy 只在"某 namespace 要豁免全局规则"时才用。

### 1.3 Match 和 Exclude

所有策略都有 `match` 和 `exclude` 块，控制策略应用到哪些资源。match 支持的维度：

- `resources.kinds`：匹配资源类型
- `resources.names`：匹配资源名（支持 wildcard）
- `resources.namespaces`：匹配 namespace
- `resources.selector`：matchLabels / matchExpressions
- `subjects`：谁在操作（User/Group/ServiceAccount）
- `clusterRoles`/`roles`：通过 RBAC role 匹配

举例：

```yaml
match:
  any:
    - resources:
        kinds: [Pod, Deployment, StatefulSet]
        namespaces: ["prod-*"]
      subjects:
        - kind: Group
          name: "system:serviceaccounts:ci"
exclude:
  any:
    - resources:
        namespaces: ["kube-system", "kyverno"]
```

这段说："在 `prod-*` namespace 里，CI 流水线创建 Pod/Deployment/StatefulSet 时触发检查，但 kube-system 和 kyverno 除外。"

**注意**：`match.any` 和 `match.all` 的区别——any 是或关系，all 是与关系。生产里几乎都用 any，all 用来做复合匹配。

## 二、Validate 策略：最常用的场景

### 2.1 基础写法

一条最简单的 validate 策略：

```yaml
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: disallow-latest-tag
spec:
  validationFailureAction: Enforce
  background: true
  rules:
    - name: check-latest
      match:
        any:
          - resources:
              kinds: [Pod]
      validate:
        message: "容器镜像不能使用 'latest' tag，请显式指定版本"
        pattern:
          spec:
            containers:
              - image: "!*:latest"
```

`pattern` 是 Kyverno 最原始的表达式方式，支持 wildcard、`!`（非）、`?*`（存在）等操作符。简单清晰但表达力有限。

### 2.2 CEL 表达式（1.11+）

Kyverno 1.11+ 支持 CEL（Common Expression Language，和 K8s ValidatingAdmissionPolicy 同源）。CEL 的表达力远超 pattern，基本能写任何条件：

```yaml
spec:
  rules:
    - name: check-resources-require-limits
      match:
        any: [{ resources: { kinds: [Pod] }}]
      validate:
        cel:
          expressions:
            - expression: |
                object.spec.containers.all(c, 
                  has(c.resources) && 
                  has(c.resources.limits) && 
                  has(c.resources.limits.memory) && 
                  has(c.resources.limits.cpu))
              message: "所有容器必须设置 cpu/memory limits"
            - expression: |
                object.spec.containers.all(c,
                  quantity(c.resources.limits.memory).compareTo(quantity('16Gi')) <= 0)
              message: "单容器 memory limit 不能超过 16Gi"
```

CEL 的优势：
- 强类型、表达丰富
- 和 K8s 原生 `ValidatingAdmissionPolicy` 语法一致，未来可以无缝迁移
- 性能好，不需要外部执行器

我现在写新策略基本上都用 CEL。老的 pattern 风格保留做基础规则。

### 2.3 Deny 规则：复杂逻辑

`validate.deny` 允许更复杂的 AND/OR 逻辑判断：

```yaml
validate:
  message: "生产 ns 不允许使用 NodePort Service"
  deny:
    conditions:
      all:
        - key: "{{ request.object.spec.type }}"
          operator: Equals
          value: NodePort
        - key: "{{ request.namespace }}"
          operator: AnyIn
          value: ["prod-*"]
```

这里的 `{{ }}` 是 JMESPath 表达式，用来访问请求上下文字段。

### 2.4 使用外部数据（Context）

Kyverno 的 `context` 允许策略查询外部数据源，比如 ConfigMap、API 调用：

```yaml
rules:
  - name: check-allowed-registries
    match:
      any: [{ resources: { kinds: [Pod] }}]
    context:
      - name: allowed_registries
        configMap:
          name: allowed-registries
          namespace: kyverno
    validate:
      message: "镜像必须来自允许的 registry: {{ allowed_registries.data.list }}"
      deny:
        conditions:
          any:
            - key: "{{ request.object.spec.containers[*].image }}"
              operator: AnyNotIn
              value: "{{ allowed_registries.data.list | split('\\n') }}"
```

这样 registry 白名单可以通过 ConfigMap 动态维护，不改策略 YAML。我们生产里用这种模式管理"允许的基础镜像"、"禁用的 capability 列表"、"特殊团队豁免"等。

**进阶**：context 还能调 K8s API 甚至外部 REST API：

```yaml
context:
  - name: pods_in_ns
    apiCall:
      urlPath: "/api/v1/namespaces/{{ request.namespace }}/pods"
      jmesPath: "items | length(@)"
```

这让策略可以做"这个 namespace 里 Pod 数量不能超过 N"这种有状态判断。但要注意性能——每次请求都打 API 会慢。

## 三、Mutate 策略：默认值和自动补全

### 3.1 给 Pod 自动加 label

```yaml
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: auto-label-owner
spec:
  rules:
    - name: add-team-label
      match:
        any: [{ resources: { kinds: [Pod] }}]
      mutate:
        patchStrategicMerge:
          metadata:
            labels:
              team: "{{ request.namespace | split('-') | [0] }}"
              managed-by: kyverno
              created-at: "{{ request.object.metadata.creationTimestamp }}"
```

这条策略会根据 namespace 前缀自动给 Pod 加 `team` label（比如 `payments-prod` → `team=payments`）。对"**需要按 team 归因但开发忘了打 label**"的场景极其有用。

### 3.2 自动注入 securityContext

```yaml
rules:
  - name: inject-default-securitycontext
    match:
      any:
        - resources:
            kinds: [Pod]
            namespaces: ["dev-*", "staging-*"]
    mutate:
      patchesJson6902: |
        - path: "/spec/securityContext/runAsNonRoot"
          op: add
          value: true
        - path: "/spec/securityContext/seccompProfile"
          op: add
          value:
            type: RuntimeDefault
```

这条策略把非生产环境的 Pod 强制补默认 securityContext，不需要开发手动写。结合 PSS 的 Restricted 模式，形成"不改 YAML 就能自动符合严格标准"的效果。

### 3.3 targets: 修改已存在资源

Kyverno 1.10+ 的 mutate existing 特性允许修改**已经存在**的资源（不只是新建时）。典型用法是"某 ConfigMap 被更新时同步修改依赖它的 Deployment"：

```yaml
rules:
  - name: rotate-deployment-on-config-change
    match:
      any:
        - resources:
            kinds: [ConfigMap]
            namespaces: ["apps"]
            name: "app-config"
    mutate:
      targets:
        - apiVersion: apps/v1
          kind: Deployment
          namespace: "{{ request.namespace }}"
          name: my-app
      patchStrategicMerge:
        spec:
          template:
            metadata:
              annotations:
                config-hash: "{{ random('[0-9a-f]{8}') }}"
```

ConfigMap 一变，对应 Deployment 的 annotation 就被修改，触发 rollout。比 stakater/reloader 更原生。

## 四、Generate 策略：运营自动化

Generate 是 Kyverno 的独门绝技。典型用法：

### 4.1 新建 namespace 自动下发基础资源

```yaml
rules:
  - name: sync-default-network-policy
    match:
      any:
        - resources:
            kinds: [Namespace]
    exclude:
      any:
        - resources:
            namespaces: ["kube-system", "kyverno", "kube-public"]
    generate:
      apiVersion: networking.k8s.io/v1
      kind: NetworkPolicy
      name: default-deny
      namespace: "{{ request.object.metadata.name }}"
      synchronize: true
      data:
        spec:
          podSelector: {}
          policyTypes: [Ingress, Egress]
          ingress:
            - from:
                - namespaceSelector:
                    matchLabels:
                      kubernetes.io/metadata.name: "{{ request.object.metadata.name }}"
          egress:
            - to:
                - namespaceSelector: {}
              ports:
                - protocol: UDP
                  port: 53
```

创建 namespace → Kyverno 自动下发一条 default-deny NetworkPolicy，只允许 namespace 内互通 + DNS egress。新 namespace 天生就是"零信任基线"状态。

`synchronize: true` 表示：如果有人删了这个 NetworkPolicy，Kyverno 会自动重新创建。这是 "**policy 守护**" 的典型用法。

### 4.2 自动生成 ImagePullSecret

```yaml
rules:
  - name: sync-image-pull-secret
    match:
      any: [{ resources: { kinds: [Namespace] }}]
    generate:
      apiVersion: v1
      kind: Secret
      name: regcred
      namespace: "{{ request.object.metadata.name }}"
      synchronize: true
      clone:
        namespace: kyverno
        name: regcred-template
```

`clone` 模式是从另一个 namespace 复制 Secret/ConfigMap，复制方向是单向的：源修改时 Kyverno 会同步到所有目标。

### 4.3 团队 namespace 标准资源包

一个更复杂的例子——新 namespace 自动创建 RoleBinding + ResourceQuota + LimitRange + 默认 ServiceAccount：

```yaml
spec:
  rules:
    - name: namespace-bootstrap
      match:
        any: [{ resources: { kinds: [Namespace] }}]
      preconditions:
        all:
          - key: "{{ request.object.metadata.labels.\"app.kubernetes.io/managed-by\" || '' }}"
            operator: NotEquals
            value: "system"
      generate:
        apiVersion: v1
        kind: ResourceQuota
        name: default
        namespace: "{{ request.object.metadata.name }}"
        synchronize: true
        data:
          spec:
            hard:
              requests.cpu: "100"
              requests.memory: "200Gi"
              limits.cpu: "200"
              limits.memory: "400Gi"
              count/pods: "500"
              persistentvolumeclaims: "50"
```

我们内部的"开团队"流程彻底被这种 Generate 策略替代——PR 创建一个 Namespace 对象，其他资源全由 Kyverno 自动生成。从周级流程变成秒级流程。

## 五、VerifyImages 策略：和 Sigstore 整合

前面 Sigstore 那一篇讲过 Cosign 签名，这里讲 Kyverno 怎么验签。

```yaml
rules:
  - name: verify-signatures
    match:
      any: [{ resources: { kinds: [Pod], namespaces: ["prod-*"] }}]
    verifyImages:
      - imageReferences:
          - "ghcr.io/myorg/**"
        attestors:
          - count: 1
            entries:
              - keyless:
                  subject: "https://github.com/myorg/myrepo/.github/workflows/build.yml@refs/heads/main"
                  issuer: https://token.actions.githubusercontent.com
                  rekor:
                    url: https://rekor.sigstore.dev
        mutateDigest: true
        required: true
        failureAction: Enforce
```

关键字段：

- **`mutateDigest: true`**：准入时把 `image: xxx:tag` 改成 `image: xxx@sha256:digest`，避免 tag 漂移
- **`required: true`**：没有签名直接拒绝（默认 false 会跳过）
- **`failureAction: Enforce`**：和 `validationFailureAction` 分开，可以做到"签名验证强制，其他规则审计"

### 5.1 多 attestor 与组合条件

有时候你想说"必须有 build workflow 签名 **且** 有 vuln scan attestation"：

```yaml
verifyImages:
  - imageReferences: ["ghcr.io/myorg/**"]
    attestors:
      - count: 1
        entries:
          - keyless:
              subject: "https://github.com/myorg/myrepo/.github/workflows/build.yml@refs/heads/main"
              issuer: https://token.actions.githubusercontent.com
    attestations:
      - type: https://cosign.sigstore.dev/attestation/vuln/v1
        conditions:
          - all:
              - key: "{{ element.scanner.result.summary.Critical }}"
                operator: LessThanOrEquals
                value: 0
              - key: "{{ element.scanner.result.summary.High }}"
                operator: LessThanOrEquals
                value: 5
```

这样镜像必须有签名 + 有漏扫 attestation + 漏扫 Critical=0 && High<=5，三个条件都满足才放行。

## 六、策略治理：分层与豁免

### 6.1 策略分层

大规模集群里策略多到几十上百条。合理分层：

```
ClusterPolicy:
├── baseline/           # 基础安全基线 (enforce)
│   ├── disallow-privileged-containers
│   ├── disallow-hostpath
│   ├── require-drop-all-caps
├── best-practice/      # 最佳实践 (audit/warn)
│   ├── require-resource-limits
│   ├── require-liveness-probe
│   ├── disallow-latest-tag
├── company/            # 公司规范 (enforce)
│   ├── require-owner-label
│   ├── allowed-registries
│   ├── namespace-naming-convention
├── verify-images/      # 签名验证 (enforce)
│   └── verify-prod-images
└── generate/           # 自动化生成
    ├── default-network-policy
    ├── default-resource-quota
```

每层有明确的 action：基线必须 enforce，最佳实践可以 audit/warn，公司规范 enforce，签名 enforce，生成类无 action。

### 6.2 PolicyException

现实里总会有合法例外——某个业务真的需要 hostPath、某个 SA 真的需要 root。Kyverno 1.9+ 的 `PolicyException` 专门处理这种情况：

```yaml
apiVersion: kyverno.io/v2
kind: PolicyException
metadata:
  name: logging-fluentd-hostpath
  namespace: logging
spec:
  exceptions:
    - policyName: disallow-hostpath
      ruleNames:
        - check-hostpath
  match:
    any:
      - resources:
          kinds: [Pod]
          namespaces: [logging]
          selector:
            matchLabels:
              app: fluentd
  conditions:
    all:
      - key: "{{ request.object.spec.volumes[*].hostPath.path }}"
        operator: AnyIn
        value: ["/var/log", "/var/log/containers"]
```

**关键设计**：PolicyException 是声明式的，可以 Git 管理、PR review、审计。不像 Gatekeeper 的 exempt 字段那样塞在 ConstraintTemplate 里。

我们的使用规范：
1. 每个 exception 必须关联 Jira ticket
2. 必须有 `expire-at` annotation（手动维护）
3. 每个月 review 一次，过期的删除

### 6.3 GitOps 管理

Kyverno 策略天然适合 GitOps。我们的结构：

```
kyverno-policies/
├── base/
│   ├── baseline/
│   ├── best-practice/
│   ├── company/
│   └── kustomization.yaml
├── overlays/
│   ├── prod/
│   │   ├── patches.yaml       # 生产严格
│   │   └── kustomization.yaml
│   └── staging/
│       ├── patches.yaml       # staging 宽松
│       └── kustomization.yaml
└── exceptions/
    └── namespace-specific/
```

Argo CD 同步整个目录到集群，`overlays/prod/` 应用到生产集群、`overlays/staging/` 应用到 staging。所有变更走 PR。

## 七、PolicyReport：policy as code 的可观测

Kyverno 把策略结果写成 `PolicyReport` CRD，每个 namespace 一个（集群级的是 `ClusterPolicyReport`）。

```bash
kubectl get policyreport -A
# NAMESPACE   NAME                PASS   FAIL   WARN   ERROR   SKIP   AGE
# payments    polr-ns-payments    45     3      2      0       0      7d
# orders      polr-ns-orders      52     0      0      0       0      7d
```

```bash
kubectl get polr -n payments polr-ns-payments -o yaml
```

每个策略的通过/失败数量都能看到。配合 **policy-reporter** 这个工具可以把结果可视化：

```bash
helm install policy-reporter policy-reporter/policy-reporter \
  --namespace policy-reporter \
  --set ui.enabled=true \
  --set kyverno.enabled=true
```

policy-reporter 提供一个 UI dashboard，按 namespace/policy/severity 分组查看。配合 Grafana 的 policy-reporter datasource，可以做历史趋势图、违规 top 榜等。

### 7.1 Prometheus 指标

Kyverno 自带 Prometheus metric：

```
kyverno_admission_requests_total{}
kyverno_admission_review_duration_seconds{}
kyverno_policy_results_total{}
kyverno_policy_execution_duration_seconds{}
```

告警：

```yaml
- alert: KyvernoAdmissionLatencyHigh
  expr: |
    histogram_quantile(0.95, 
      sum(rate(kyverno_admission_review_duration_seconds_bucket[5m])) by (le)
    ) > 1
  for: 5m
  annotations:
    summary: "Kyverno 准入 P95 延迟 > 1s"
```

## 八、性能调优

### 8.1 background scan 频率

Kyverno 默认每小时做一次全集群 background scan（计算已有资源的策略违规）。大集群会占用大量 CPU/内存。调整：

```yaml
# kyverno values
backgroundController:
  resources:
    limits: { memory: 4Gi, cpu: 2 }
    requests: { memory: 1Gi, cpu: 500m }
```

调整 scan 频率：

```yaml
config:
  backgroundScanInterval: 1h   # 默认 1h
```

或者对高频策略标记 `background: false` 只在准入时触发。

### 8.2 准入延迟

Kyverno 的准入延迟一般 5~50ms。如果某条策略特别慢：

1. 用 CEL 代替 JMESPath 表达式（CEL 快 3~5 倍）
2. 减少 `context.apiCall`，尤其避免外部 HTTP
3. 把 `preconditions` 写在前面提前短路
4. `failurePolicy: Ignore` 让 Kyverno 挂掉时不阻塞业务

```yaml
webhookConfiguration:
  failurePolicy: Ignore   # 生产慎重选择
  timeoutSeconds: 10
```

### 8.3 内存占用

Kyverno 的 admission controller 和 background controller 是两个不同的 deploy，分别配置。大集群下（5000+ Pod）建议：

- admission：2 副本，4Gi 内存
- background：2 副本，4Gi 内存
- reports controller：2 副本，2Gi 内存
- cleanup controller：1 副本，1Gi 内存

## 九、踩坑记录

### 9.1 kyverno 自己挂了，准入全挂

**事故**：某次 kyverno 的 admission pod OOM，所有新 Pod 创建被卡住（默认 failurePolicy=Fail）。我们有一个 CronJob 正好在那时跑，被拒绝后没了，业务中断了 15 分钟。

修复：
1. admission webhook 设置 `failurePolicy: Ignore`（接受"Kyverno 挂了就不管"）
2. kyverno 自己设置 PodDisruptionBudget 防止滚动更新时全挂
3. 资源 request/limit 给够，不要让 OOM 发生

**但 failurePolicy=Ignore 有安全代价**：Kyverno 挂的时候坏 Pod 能创建。我们的折中是：
- **安全关键策略（verifyImages、baseline）**：一条独立的 ValidatingWebhookConfiguration，failurePolicy=Fail
- **其他策略**：failurePolicy=Ignore

分开两个 webhook 配置。

### 9.2 mutate 策略导致 rollout 无限循环

**事故**：写了个 mutate 策略自动给 Pod 加 annotation `last-updated: {{ time_now }}`。结果每次 reconcile 触发 update，annotation 变了触发下一次 mutate，Pod 进入无限 mutation 循环。

修复：
1. mutate 不要用时间戳等不稳定值
2. Kyverno 1.10+ 的 `mutateExistingOnPolicyUpdate: false` 可以关闭策略更新时的重放

### 9.3 Generate 策略 race condition

新建 namespace 时 Kyverno 立刻尝试 generate 资源，但 namespace 的 RBAC 可能还没初始化好（admission controller 串行问题），generate 失败。

修复：策略里加 `preconditions` 检查关键 SA 存在：

```yaml
preconditions:
  all:
    - key: "{{ request.object.metadata.name }}"
      operator: NotEquals
      value: ""
```

以及 Kyverno 1.11+ 对 generate 做了 retry，一般能自愈。

### 9.4 PolicyReport 爆 etcd

大集群 + 大量策略违规会产生海量 PolicyReport 条目，每条都是 CRD。我们一个集群一度有 30000+ PolicyReport，etcd 空间占用翻倍。

修复：

```yaml
reportsController:
  emitEvents: false
config:
  reportsChunkSize: 1000
  maxReportChangeRequests: 10000
```

以及定期清理老 report。Kyverno 1.12+ 有自动清理。

### 9.5 策略冲突

两条 mutate 同时想改同一个字段，行为不确定。Kyverno 按名字字母序执行，后执行的覆盖前面的。**不要依赖这个顺序**，应当避免冲突。

## 十、和其他工具的组合

这里讲几个经典组合：

### 10.1 Kyverno + PSA

PSA 做"底线"，Kyverno 做"细化"。PSA 的 profile 不能自定义，Kyverno 补上所有 PSA 没覆盖的维度（registry 白名单、resource limits、label 规范等）。

### 10.2 Kyverno + Cosign

VerifyImages 就是集成。Kyverno 现在是官方推荐的"用 Kyverno 做签名验证"的方案之一（另一个是 Sigstore Policy Controller）。

### 10.3 Kyverno + Argo CD

Argo CD 同步 Kyverno 策略，所有策略变更走 PR。Argo CD 还可以给出 policy 冲突 preview——应用新策略前看看会拒绝哪些现有资源。

### 10.4 Kyverno + Falco

Falco 做运行时检测，Kyverno 做准入时拒绝。两者覆盖不同阶段：准入阻止"**不应该被创建**"的资源，运行时监控"**创建后发生了什么**"。

## 十一、Kyverno vs OPA Gatekeeper：最后的对比

| 维度 | Kyverno | OPA Gatekeeper |
|------|---------|----------------|
| 语言 | YAML + CEL/JMESPath | Rego |
| 学习曲线 | 低 | 高 |
| K8s 原生程度 | 极高 | 中 |
| 功能范围 | Validate/Mutate/Generate/VerifyImages | Validate/Mutate |
| 社区策略库 | 丰富 (Kyverno Policies) | 丰富 (OPA Library) |
| 外部数据 | context (ConfigMap/API call) | ExternalData provider |
| 适用规模 | 中大型集群 | 大型集群 |
| 性能 | 好 | 略胜一筹（Rego 编译执行） |

**我的推荐**：
- **新项目、K8s 原生团队**：Kyverno。学习曲线低，上手快。
- **已有 Rego 积累、非常复杂策略**：Gatekeeper。Rego 表达力更强。
- **极致性能**：Gatekeeper 略好。但大多数场景差异不明显。

从 2024 年开始，Kyverno 的生态进步明显快于 Gatekeeper。CNCF 毕业状态、社区策略库、和 Sigstore 的深度整合都领先。如果没有特别理由，选 Kyverno。

## 十二、落地路线

最后给一个循序渐进的落地建议：

**阶段 1（1 周）**：部署 Kyverno，引入 baseline 策略（社区 policy library）。全部 `audit` 模式，收集违规数据。

**阶段 2（2~4 周）**：跟业务沟通整改，将 baseline 策略切到 `Enforce`。同时引入 best-practice 策略（保持 audit）。

**阶段 3（1~2 月）**：引入公司规范策略（registry 白名单、label 规范、owner 归属）。引入 PolicyException 机制处理合法豁免。

**阶段 4（持续）**：Generate 策略做 namespace bootstrap；VerifyImages 策略做镜像签名强制；定期 review 违规和 exception。

## 十三、结语

Policy as Code 落到 Kyverno 这个层面就是一句话——**用声明式 YAML 把集群该有的样子写出来**。当你的集群跑着几百条策略、每条都经过 PR review、每条都有 metric 和 exception 管理流程，所谓的"安全合规最佳实践"就不用再靠口头提醒了。

下一篇是这个零信任系列最后一篇：SLSA 软件供应链等级实施。
