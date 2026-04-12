---
title: "OPA/Kyverno：K8s 准入控制策略实战"
date: 2025-09-11T13:36:00+08:00
draft: false
tags: ["kyverno", "opa", "Kubernetes", "policy", "security", "admission-control"]
categories: ["Kubernetes"]
description: "用 Kyverno 和 OPA Gatekeeper 在 K8s 中实施准入控制策略，从安装到常用策略的完整实战。"
summary: "没有准入控制的 K8s 集群就像一个没有门卫的机房——任何人都能随意进出。本文记录了我在多个生产集群部署 Kyverno 策略的实战经验，涵盖资源限制强制、镜像来源白名单、标签规范、以及与 OPA Gatekeeper 的对比选型思路。"
toc: true
math: false
diagram: false
series: ["K8s 完全指南"]
keywords: ["kyverno", "opa gatekeeper", "准入控制", "kubernetes policy", "安全策略"]
params:
  reading_time: true
---

## 为什么需要准入控制

K8s 默认情况下，只要有 `kubectl apply` 权限，几乎可以创建任意资源：没有 `resources.limits` 的 Pod、使用 `latest` 镜像的 Deployment、从任意仓库拉取的镜像……这些问题不会在运行时立刻暴露，但会在某个凌晨两点变成你的噩梦。

我经历过几个典型事故：

- 某个测试 Pod 没有设置内存限制，OOM 之后 K8s 开始驱逐同节点上的生产 Pod
- 开发推送了一个 `image: myapp:latest`，部署时实际拉取了一个三个月前的旧镜像（Registry 的缓存问题）
- 一批 Pod 没有 `team` 标签，出了问题根本不知道是哪个团队的服务

这些问题的根源不是开发者的能力，而是**缺乏在资源创建时的约束机制**。K8s 的 Admission Webhook 体系就是为此而生的。

### Admission 控制流程

```
kubectl apply
    │
    ▼
API Server 认证/鉴权
    │
    ▼
Mutating Admission Webhooks（可修改请求）
    │
    ▼
Schema Validation（CRD/OpenAPI）
    │
    ▼
Validating Admission Webhooks（只读验证）
    │
    ▼
资源写入 etcd
```

OPA Gatekeeper 和 Kyverno 都是通过实现 Validating/Mutating Webhook 来工作的。

---

## Kyverno vs OPA Gatekeeper

简单来说，如果你没有 Rego 经验，选 Kyverno；如果你的团队已经在用 OPA，选 Gatekeeper。

| 维度 | Kyverno | OPA Gatekeeper |
|------|---------|----------------|
| 策略语言 | Kubernetes 原生 YAML + JMESPath | Rego（专用语言） |
| 学习曲线 | 低 | 高 |
| Mutation 支持 | 原生支持 | 需要额外配置 |
| 生态策略库 | Kyverno Policies 官方库 | OPA Library |
| 审计模式 | 支持 | 支持 |
| 报告能力 | PolicyReport（CRD）| Audit Controller |
| 社区活跃度 | CNCF 孵化项目，活跃 | CNCF 毕业项目，稳定 |

我在大多数新集群选 Kyverno，主要原因是策略即 YAML，团队成员不需要额外学习 Rego，降低了策略维护的门槛。

---

## Kyverno 安装

```bash
# Helm 安装（推荐）
helm repo add kyverno https://kyverno.github.io/kyverno/
helm repo update

kubectl create ns kyverno

helm install kyverno kyverno/kyverno \
  --namespace kyverno \
  --set replicaCount=3 \    # 生产环境至少3副本
  --version 3.1.4
```

验证：

```bash
kubectl get pods -n kyverno
# kyverno-admission-controller-xxx   1/1   Running
# kyverno-background-controller-xxx  1/1   Running
# kyverno-cleanup-controller-xxx     1/1   Running
# kyverno-reports-controller-xxx     1/1   Running
```

**重要提醒**：生产集群安装 Kyverno 之前，先以 `audit` 模式运行1-2周，观察哪些现有资源会违规，再切换到 `enforce` 模式。直接 enforce 会导致已有 CD 流程失败。

---

## Policy vs ClusterPolicy

- `Policy`：命名空间级别，只影响当前 namespace
- `ClusterPolicy`：集群级别，影响所有 namespace（可以用 `exclude` 排除）

```yaml
# 命名空间级别策略（只影响 team-a namespace）
apiVersion: kyverno.io/v1
kind: Policy
metadata:
  name: require-labels
  namespace: team-a

---
# 集群级别策略（影响全集群，但排除 kube-system）
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: require-labels-global
spec:
  rules:
    - name: check-labels
      exclude:
        any:
          - resources:
              namespaces:
                - kube-system
                - kyverno
                - cert-manager
```

---

## 常用策略实战

### 策略1：强制资源限制

这是最基础也最重要的策略。没有 `limits` 的 Pod 是潜在的资源炸弹。

```yaml
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: require-resource-limits
  annotations:
    policies.kyverno.io/title: "强制资源限制"
    policies.kyverno.io/description: "所有容器必须设置 CPU 和内存的 requests 与 limits"
spec:
  validationFailureAction: Enforce   # Audit（只记录）或 Enforce（拒绝）
  background: true                   # 对已有资源做审计
  rules:
    - name: check-resource-limits
      match:
        any:
          - resources:
              kinds:
                - Pod
      exclude:
        any:
          - resources:
              namespaces:
                - kube-system
                - monitoring
      validate:
        message: "容器 '{{ request.object.spec.containers[].name }}' 必须设置 resources.limits.cpu 和 resources.limits.memory"
        pattern:
          spec:
            containers:
              - resources:
                  limits:
                    memory: "?*"
                    cpu: "?*"
                  requests:
                    memory: "?*"
                    cpu: "?*"
```

### 策略2：禁止 latest 镜像标签

```yaml
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: disallow-latest-tag
spec:
  validationFailureAction: Enforce
  background: true
  rules:
    - name: require-image-tag
      match:
        any:
          - resources:
              kinds: [Pod]
      validate:
        message: "镜像必须使用明确的版本标签，禁止使用 :latest 或不带标签"
        foreach:
          - list: "request.object.spec.containers"
            deny:
              conditions:
                any:
                  # 镜像名以 :latest 结尾
                  - key: "{{ element.image }}"
                    operator: Equals
                    value: "*:latest"
                  # 镜像名不包含冒号（没有标签）
                  - key: "{{ element.image }}"
                    operator: NotContains
                    value: ":"
```

### 策略3：强制标签规范

```yaml
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: require-standard-labels
spec:
  validationFailureAction: Enforce
  background: true
  rules:
    - name: check-deployment-labels
      match:
        any:
          - resources:
              kinds: [Deployment, StatefulSet, DaemonSet]
      validate:
        message: "工作负载必须包含 app、team、version 标签"
        pattern:
          metadata:
            labels:
              app: "?*"
              team: "?*"
              version: "?*"
    - name: check-pod-labels
      match:
        any:
          - resources:
              kinds: [Pod]
      validate:
        message: "Pod 必须包含 app 和 team 标签"
        pattern:
          metadata:
            labels:
              app: "?*"
              team: "?*"
```

### 策略4：镜像来源白名单

只允许从指定私有仓库拉取镜像，防止供应链攻击。

```yaml
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: restrict-image-registries
spec:
  validationFailureAction: Enforce
  background: true
  rules:
    - name: validate-registries
      match:
        any:
          - resources:
              kinds: [Pod]
      exclude:
        any:
          - resources:
              namespaces: [kube-system, kyverno]
      validate:
        message: "镜像只能来自授权仓库：registry.example.com 或 mirror.example.com"
        foreach:
          - list: "request.object.spec.containers"
            deny:
              conditions:
                all:
                  - key: "{{ element.image }}"
                    operator: NotStartsWith
                    value: "registry.example.com/"
                  - key: "{{ element.image }}"
                    operator: NotStartsWith
                    value: "mirror.example.com/"
```

### 策略5：Mutation——自动注入标签

Mutation 策略可以在资源创建时自动修改，减少开发者的认知负担。

```yaml
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: add-default-labels
spec:
  rules:
    - name: add-environment-label
      match:
        any:
          - resources:
              kinds: [Pod]
      mutate:
        patchStrategicMerge:
          metadata:
            labels:
              # 如果没有 environment 标签，自动注入
              +(environment): "production"
```

`+()` 语法表示"仅在不存在时添加"，不会覆盖已有标签。

### 策略6：强制 PodDisruptionBudget

```yaml
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: require-pdb
spec:
  validationFailureAction: Audit   # 先 Audit，观察一段时间
  background: true
  rules:
    - name: check-pdb-exists
      match:
        any:
          - resources:
              kinds: [Deployment]
              operations: [CREATE, UPDATE]
      preconditions:
        all:
          - key: "{{ request.object.spec.replicas }}"
            operator: GreaterThan
            value: 1
      validate:
        message: "副本数 > 1 的 Deployment 必须配置 PodDisruptionBudget"
        deny:
          conditions:
            all:
              - key: "{{ request.object.metadata.name }}"
                operator: Equals
                value: "{{ request.object.metadata.name }}"
```

> 这个策略实际上需要跨资源验证（检查同名 PDB 是否存在），完整实现建议用 Kyverno 的 `foreach` + `context.apiCall` 特性查询集群状态。

---

## 审计模式 vs 强制模式

这两种模式决定了违规时的行为：

```yaml
spec:
  validationFailureAction: Audit    # 只记录，不拒绝
  # 或
  validationFailureAction: Enforce  # 拒绝创建/更新
```

**推荐的上线流程**：

```
1. Audit 模式上线
      ↓
2. 观察 PolicyReport（1-2周）
      ↓
3. 修复存量违规资源
      ↓
4. 切换为 Enforce 模式
      ↓
5. 通知所有开发团队
```

查看审计报告：

```bash
# 查看集群级别违规报告
kubectl get clusterpolicyreport

# 查看详情
kubectl get clusterpolicyreport -o jsonpath='{.items[*].results[?(@.result=="fail")]}'  | jq .

# 命名空间级别
kubectl get policyreport -n production -o yaml
```

---

## OPA Gatekeeper 对比实践

如果你需要更复杂的策略逻辑（例如跨资源联动、复杂条件计算），OPA Gatekeeper 更有优势。

安装：

```bash
kubectl apply -f https://raw.githubusercontent.com/open-policy-agent/gatekeeper/v3.14.0/deploy/gatekeeper.yaml
```

Gatekeeper 的策略分两层：

1. `ConstraintTemplate`：定义约束的 Rego 逻辑
2. `Constraint`（具体类型）：实例化约束、配置参数

```yaml
# 1. 定义模板
apiVersion: templates.gatekeeper.sh/v1beta1
kind: ConstraintTemplate
metadata:
  name: k8srequiredlabels
spec:
  crd:
    spec:
      names:
        kind: K8sRequiredLabels
      validation:
        openAPIV3Schema:
          type: object
          properties:
            labels:
              type: array
              items:
                type: string
  targets:
    - target: admission.k8s.gatekeeper.sh
      rego: |
        package k8srequiredlabels

        violation[{"msg": msg}] {
          provided := {label | input.review.object.metadata.labels[label]}
          required := {label | label := input.parameters.labels[_]}
          missing := required - provided
          count(missing) > 0
          msg := sprintf("缺少必需标签: %v", [missing])
        }

---
# 2. 实例化约束
apiVersion: constraints.gatekeeper.sh/v1beta1
kind: K8sRequiredLabels
metadata:
  name: require-team-label
spec:
  enforcementAction: deny   # 或 warn、dryrun
  match:
    kinds:
      - apiGroups: ["apps"]
        kinds: ["Deployment"]
  parameters:
    labels: ["app", "team", "version"]
```

Rego 的优势在于可以写复杂逻辑，缺点是语法陌生，调试也比较麻烦。建议用 [OPA Playground](https://play.openpolicyagent.org/) 在线测试策略逻辑。

---

## 踩坑记录

**坑1：Kyverno 高可用配置**

单副本 Kyverno 在重启时，Webhook 超时会导致所有 Pod 创建请求被拒绝（FailurePolicy: Fail）。生产环境必须 3 副本 + 反亲和。

```yaml
# Kyverno Helm values
replicaCount: 3
podAntiAffinity:
  requiredDuringSchedulingIgnoredDuringExecution:
    - labelSelector:
        matchLabels:
          app.kubernetes.io/name: kyverno
      topologyKey: kubernetes.io/hostname
```

**坑2：Webhook 超时导致 CD 失败**

Kyverno 默认 Webhook 超时是 10s。如果集群负载高或 Kyverno Pod 资源紧张，校验请求可能超时，导致 CD 流水线莫名其妙地失败。监控 Kyverno 的 `kyverno_admission_requests_total` 指标，以及 `kyverno_policy_execution_duration_seconds` 。

**坑3：背景扫描（Background Scan）的影响**

`background: true` 会让 Kyverno 定期扫描集群中已有的资源并生成 PolicyReport。在大集群（几千个 Pod）中，这会消耗不少 CPU。建议在 values 中调整：

```yaml
backgroundController:
  resources:
    requests:
      cpu: 200m
      memory: 256Mi
    limits:
      cpu: 1000m
      memory: 1Gi
```

**坑4：排除系统命名空间**

一定要在 ClusterPolicy 中排除 `kube-system`、`kyverno`、`cert-manager` 等基础设施命名空间，否则 DaemonSet/系统组件更新时会被自己的策略卡住。

---

## 总结

准入控制是 K8s 平台工程的重要组成部分。我的建议是：

1. **从最少侵入的策略开始**：先 Audit 模式，先做标签和资源限制
2. **策略代码化**：所有策略存入 Git，走 CI 验证，不要在集群里手动改策略
3. **定期审查 PolicyReport**：设置告警，有违规立刻修复，不要让技术债累积
4. **与 CD 集成**：可以用 `kyverno apply` 命令在 CI 阶段预验证 YAML，提前发现问题

策略不是银弹，它解决的是"不知不觉违规"的问题，开发文化和规范文档同样重要。
