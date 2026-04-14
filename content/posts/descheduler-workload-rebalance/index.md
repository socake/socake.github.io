---
title: "Descheduler 深度实战：Kubernetes 自动再平衡的正确打开方式"
date: 2025-03-22T16:00:00+08:00
draft: false
tags: ["Descheduler", "Kubernetes", "调度", "成本优化"]
categories: ["Kubernetes"]
description: "Descheduler 0.34 的完整生产实战：为什么 Kubernetes 需要 descheduler、LowNodeUtilization / RemoveDuplicates / HighNodeUtilization / RemovePodsViolatingTopologySpreadConstraint 的真实用法、DefaultEvictor 策略、生产抖动如何避免、和 Karpenter / cluster-autoscaler 的协作。"
summary: "kube-scheduler 只在 Pod 创建那一刻做决策，之后集群状态变了它就不管了。几个月下来，你的集群会变成 hot node + cold node 混杂、同一个 Deployment 的 Pod 全挤在一个 node、failure-domain 完全失衡。Descheduler 就是把调度决策后置、周期性重新评估的那只手。"
toc: true
math: false
diagram: false
keywords: ["Descheduler", "LowNodeUtilization", "RemoveDuplicates", "Kubernetes", "rebalance", "eviction"]
params:
  reading_time: true
---

## 为什么 Kubernetes 需要 descheduler

先说清楚一件事：kube-scheduler 不是"动态调度器"。它只在 Pod 被创建的瞬间做一次决定，这次决定做完之后，就和它无关了。

但现实是集群状态会变：

- 节点被加入 / 删除（cluster-autoscaler / Karpenter）；
- 有些 Pod 消耗从 50% 涨到 200%；
- 有些 node 因为历史原因成了"热节点"，多个 high-req Pod 都挤在上面；
- 一个 Deployment 几次 rollout 后，副本全部漂到 2 个 node 上；
- 打了新 taint 的节点上仍有老 Pod 没走；
- topology spread constraint 的初始约束被 rollout 破坏。

这些情况下 kube-scheduler 无能为力——它只看新 Pod，不会主动迁移老 Pod。你唯一的办法是手动杀 Pod 让它重新被调度，或者用 descheduler 周期性地做这件事。

Descheduler 的逻辑非常朴素：

1. 按一组策略扫描集群；
2. 找到"应该被迁走"的 Pod；
3. 把它们 evict（用 Eviction API，尊重 PDB）；
4. 让 kube-scheduler 重新调度。

它**不自己决定新位置**，只负责"驱逐"。

## 版本和定位

截至 2026 年 4 月，Descheduler 最新版本是 0.34.0，对应 Kubernetes 1.34 依赖。生产推荐版本：

- Kubernetes 1.28+ 搭配 Descheduler 0.30+
- Kubernetes 1.30+ 搭配 0.32+
- Kubernetes 1.33/1.34 搭配 0.34

Descheduler 不是 Kubernetes 核心的一部分，但它是 sig-scheduling 维护的官方子项目，成熟度很高。几乎所有大规模 Kubernetes 集群都在用。

## 运行模式：CronJob vs Deployment

Descheduler 有两种部署形态：

1. **CronJob（默认）**：定期跑一次，比如每 15 分钟。稳，但粒度粗。
2. **Deployment**：常驻，启动参数加上 `--descheduling-interval=1m`，内部定时循环。粒度可控。

选择建议：

- 开发 / 小集群：CronJob 就够；
- 生产 / 大集群：Deployment，间隔 5-15 分钟。

我们的经验：每 10 分钟跑一次比较合适。跑太频繁（比如每分钟）可能会出现"刚被 evict 又被 evict" 的抖动，跑太慢不平衡的问题解决得慢。

## 策略：DefaultEvictor 和 Profile

从 0.28 版本开始，Descheduler 的配置换成了 Profile 模式，语义上更贴近 Kubernetes scheduler framework：

```yaml
apiVersion: descheduler/v1alpha2
kind: DeschedulerPolicy
profiles:
  - name: default
    pluginConfig:
      - name: DefaultEvictor
        args:
          evictLocalStoragePods: false
          evictSystemCriticalPods: false
          ignorePvcPods: false
          nodeFit: true
          priorityThreshold:
            value: 10000
      - name: RemoveDuplicates
      - name: LowNodeUtilization
        args:
          thresholds:
            cpu: 20
            memory: 20
            pods: 20
          targetThresholds:
            cpu: 50
            memory: 50
            pods: 50
    plugins:
      balance:
        enabled:
          - RemoveDuplicates
          - LowNodeUtilization
```

### DefaultEvictor：最关键的策略

DefaultEvictor 是所有其他策略共用的"驱逐过滤器"。它决定哪些 Pod "可以被 evict"：

- **evictLocalStoragePods**：带本地存储（emptyDir / hostPath）的 Pod 是否能 evict。**生产建议 false**。带本地数据的 Pod 一被驱逐就丢数据。
- **evictSystemCriticalPods**：system-node-critical / system-cluster-critical 的 Pod 能否 evict。**必须 false**。
- **ignorePvcPods**：是否跳过带 PVC 的 Pod。默认 false（即不跳过）。但你可能想跳过——带 PVC 的 Pod 有 statefulset 依赖，evict 可能触发复杂的重调度。生产偏向 true。
- **nodeFit**：**这是最重要的参数**。设为 true 后，descheduler evict 一个 Pod 之前，会先检查"是否存在一个别的 node 能容纳它"。如果没有可去的地方，就不 evict。**生产必开**，不然你会看到 Pod 被 evict 之后又 pending 在原地的悲剧。
- **priorityThreshold.value**：只 evict 优先级低于此值的 Pod。生产推荐设一个中等值（比如 10000），关键业务 priorityClass 都给 > 10000，低优先级的离线任务给 < 10000，只动离线任务。
- **labelSelector / namespaceSelector**：限制 descheduler 只管某些 label / namespace 的 Pod。我建议默认做 namespace 白名单，只让 descheduler 管应用 namespace，不碰 kube-system / monitoring 等基础设施。

## 核心策略详解

### LowNodeUtilization：冷热节点再平衡

**适用场景**：集群里有一些 node 很忙（CPU / memory 80%+），另一些 node 很闲（20% 以下），希望把 Pod 从忙 node 迁到闲 node。

```yaml
- name: LowNodeUtilization
  args:
    thresholds:
      cpu: 20
      memory: 20
      pods: 20
    targetThresholds:
      cpu: 50
      memory: 50
      pods: 50
    numberOfNodes: 3
```

关键概念：

- **thresholds**：定义"冷 node"的上限。CPU 使用率 < 20% 且 memory < 20% 且 pods < 20% 的 node 是"冷 node"。
- **targetThresholds**：定义"热 node"的下限。CPU > 50% 或 memory > 50% 或 pods > 50% 的 node 是"热 node"。
- **numberOfNodes**：至少有几个"冷 node"才触发。防止"只有一个 node 闲" 这种情况下频繁扰动。

运行逻辑：

1. 扫描所有 node，把它们分成 冷 / 正常 / 热 三类；
2. 如果冷 node 数量 ≥ `numberOfNodes`，找热 node 上的 Pod；
3. 按照 PriorityClass 从低到高挑一些 Pod 驱逐；
4. 每次运行最多驱逐一定数量（可配 `maxNoOfPodsToEvictPerNode`）；
5. Pod 被驱逐后 kube-scheduler 会看到冷 node 有资源，就调度过去。

**重要注意**：

1. **"使用率"是 requests 还是 actual？** 0.28 之前只支持 requests（基于 Pod requests 算占用）。之后支持了基于 actual metrics 的方式（`metricsUtilization: true` + metrics-server）。
2. **生产建议用 requests 模式**，actual 模式更激进也更容易抖动。
3. thresholds 不要设得太接近 targetThresholds，中间留一段"不管"区域，避免震荡。
4. 使用这个策略的前提：集群 **本来就有冷热不均**。如果你的集群所有 node 利用率都差不多（比如 40%-50%），这个策略不会驱逐任何东西。

### RemoveDuplicates：别让 Deployment 全挤一个 node

**适用场景**：Deployment 有 5 个副本，全部跑在同一个 node 上（因为 rollout 时 node 比较空，scheduler 把它们都放一个地方了）。这种情况下 node 一挂全军覆没。

```yaml
- name: RemoveDuplicates
```

它不需要额外参数（或者 `excludeOwnerKinds` 来排除某些类型）。

运行逻辑：

1. 扫描所有 Pod，按 ownerRef（Deployment / ReplicaSet / StatefulSet 等）分组；
2. 对每一组，如果同一个 node 上有超过 1 个副本，就 evict 多余的；
3. evict 之后 scheduler 会把它们分散到其他 node。

**注意**：

- 如果你的 Deployment 本身只有 1 副本，这个策略不会做任何事；
- 如果你有些 Deployment 故意要副本共置（很少见，但有），用 `excludeOwnerKinds` 排除；
- 这个策略配合 `topologySpreadConstraints` 更好，TSC 负责"新 Pod 分散"，descheduler 负责"历史 Pod 分散"。

### RemovePodsViolatingTopologySpreadConstraint

**适用场景**：Deployment 声明了 topologySpreadConstraints，但因为历史原因有违反约束的 Pod。

```yaml
- name: RemovePodsViolatingTopologySpreadConstraint
  args:
    constraints:
      - DoNotSchedule
    labelSelector:
      matchLabels:
        tier: frontend
```

它会根据 Pod 上声明的 topologySpreadConstraints 找违反的，驱逐。

和 RemoveDuplicates 的区别：
- RemoveDuplicates 看 ownerRef，粗粒度，按 Deployment 分散；
- RemovePodsViolatingTopologySpreadConstraint 看 Pod spec 的 TSC，精细，按任意 topology（zone / host / rack）分散。

生产上这两个都开。

### HighNodeUtilization：反向策略

**适用场景**：你希望把 Pod 集中到少数 node 上，为 cluster-autoscaler 缩容创造机会。

```yaml
- name: HighNodeUtilization
  args:
    thresholds:
      cpu: 20
      memory: 20
```

它和 LowNodeUtilization 完全相反——把低利用率 node 上的 Pod 驱逐，让它们集中到其他 node，空出来的 node 就能被 autoscaler 缩掉。

**使用条件**：
- 只在 kube-scheduler 配置了 MostAllocated 策略时才有意义（默认是 LeastAllocated）；
- 或者你用 Karpenter 的 Consolidation 特性（下文讲）。

生产使用 HighNodeUtilization 的团队比较少，大部分在用 Karpenter 的话直接靠 Karpenter 做 consolidation 就够。

### RemovePodsViolatingNodeAffinity

**适用场景**：某个 Pod 以前满足 node affinity（比如跑在有特定 label 的 node 上），但后来 node 的 label 变了，Pod 不再符合 affinity 但还在上面跑着。

```yaml
- name: RemovePodsViolatingNodeAffinity
  args:
    nodeAffinityType:
      - requiredDuringSchedulingIgnoredDuringExecution
```

注意：`IgnoredDuringExecution` 意味着 Kubernetes 自己不会赶走它，但 descheduler 可以。这是少数 descheduler 帮你"补 Kubernetes 设计缺口"的场景。

### RemovePodsViolatingNodeTaints

**适用场景**：node 上后来打了新 taint，已有 Pod 没 toleration 但也不被自动驱逐（因为 taint 是 NoSchedule 而不是 NoExecute）。

```yaml
- name: RemovePodsViolatingNodeTaints
```

生产常用场景：你 cordon + 打 taint 一个 node 要做维护，descheduler 会帮你把不该在上面的 Pod 驱逐（当然 `kubectl drain` 也能做，但 drain 是一次性的）。

### RemovePodsHavingTooManyRestarts

**适用场景**：一个 Pod 被重启了几十次还是起不来。可能是这个 node 有问题。Descheduler 可以把它驱逐，让它换个 node 试试。

```yaml
- name: RemovePodsHavingTooManyRestarts
  args:
    podRestartThreshold: 10
    includingInitContainers: true
```

这个策略要谨慎用。一个 Pod 频繁重启大概率是应用问题，换个 node 没用。我只建议针对特定的 app 开（用 labelSelector 过滤）。

### PodLifeTime

**适用场景**：周期性强制重建长时间运行的 Pod。典型场景：有些应用有内存泄漏，跑 7 天就要重启一次。

```yaml
- name: PodLifeTime
  args:
    maxPodLifeTimeSeconds: 604800     # 7d
    states:
      - Running
```

**这个非常危险**，几乎从不生产开。应用内存泄漏应该修应用，不要靠 descheduler 周期性杀。开了等于给 SRE 制造定时炸弹。

## PDB：descheduler 的守护神

Descheduler evict Pod 是走 Kubernetes 的 Eviction API，会尊重 PodDisruptionBudget。这意味着：

1. 你的 Deployment 有 PDB，descheduler 一次不能 evict 太多；
2. 有 PDB 保护的话 descheduler 是安全的；
3. 没 PDB 的服务，descheduler 可能一次 evict 几个副本，短时服务不可用。

**生产强制原则**：**任何生产 Deployment 都要有 PDB**，不只是为了 descheduler，还为了 node drain / cluster-autoscaler / upgrade。

示例 PDB：

```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: app-pdb
spec:
  maxUnavailable: 1
  selector:
    matchLabels:
      app: my-app
```

或者按百分比：

```yaml
spec:
  maxUnavailable: 25%
```

## Descheduler + Karpenter：天作之合

这俩是互补的：

- Karpenter 负责"有 pending pod 时新增 node"和"node 利用率低时删 node"；
- Descheduler 负责"重新平衡已有 pod 的分布"。

典型配合：

1. Karpenter 的 Consolidation 开启后，会主动 drain 利用率低的 node；
2. drain 过程中 Pod 被 evict 到其他 node；
3. 但重新落下的位置可能造成新的不均；
4. Descheduler 每 10 分钟跑一次，修复新产生的不均。

**一个经典的坑**：Karpenter consolidation 频繁缩扩时，descheduler 可能 evict 刚被 Karpenter 放置的 Pod，两个组件互相扰动。解决方案：

1. Descheduler 的 `DefaultEvictor.priorityThreshold.value` 设成只管低优先级 Pod；
2. Karpenter consolidation 的 policy 设成 `WhenUnderutilized`，不要太激进。

## 和 cluster-autoscaler 的协作

对 cluster-autoscaler 用户来说：

- `cluster-autoscaler.kubernetes.io/safe-to-evict: "true"` 注解的 Pod 可以被 CA 驱逐；
- `cluster-autoscaler.kubernetes.io/safe-to-evict: "false"` 或未设置的 Pod CA 不会碰；
- Descheduler 不读这个 annotation，但你可以通过 `DefaultEvictor.labelSelector` 复用类似逻辑。

想让 descheduler 遵循 safe-to-evict 语义，可以加一个 label selector：

```yaml
- name: DefaultEvictor
  args:
    labelSelector:
      matchExpressions:
        - key: cluster-autoscaler.kubernetes.io/safe-to-evict
          operator: NotIn
          values: ["false"]
```

这样标记了 `safe-to-evict=false` 的 Pod 就不会被 descheduler 碰。

## 排除 kube-system 和基础设施

生产上绝对不能 evict 的：

- `kube-system` namespace 的所有东西；
- DaemonSet（descheduler 默认会跳过 mirror pod 和 DaemonSet pod，但为了保险再加一层 filter）；
- Istio / Linkerd 的 sidecar 依赖；
- CNI / CSI 相关。

最干净的做法是 namespace 白名单：

```yaml
- name: DefaultEvictor
  args:
    namespaceSelector:
      matchExpressions:
        - key: kubernetes.io/metadata.name
          operator: NotIn
          values:
            - kube-system
            - kube-public
            - monitoring
            - istio-system
            - cert-manager
            - external-dns
            - karpenter
            - descheduler
```

或者反过来，明确只管某些 namespace：

```yaml
    namespaceSelector:
      matchLabels:
        descheduler.kubernetes.io/enabled: "true"
```

然后给允许 descheduler 管的 namespace 打这个 label。这是我最推荐的做法：**显式 opt-in**。

## 部署 descheduler 的完整示例

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: descheduler
  namespace: descheduler
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: descheduler
rules:
  - apiGroups: [""]
    resources: ["events"]
    verbs: ["create", "update", "patch"]
  - apiGroups: [""]
    resources: ["nodes"]
    verbs: ["get", "watch", "list"]
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["get", "watch", "list", "delete"]
  - apiGroups: [""]
    resources: ["pods/eviction"]
    verbs: ["create"]
  - apiGroups: ["scheduling.k8s.io"]
    resources: ["priorityclasses"]
    verbs: ["get", "watch", "list"]
  - apiGroups: ["apps"]
    resources: ["deployments", "replicasets"]
    verbs: ["get", "watch", "list"]
  - apiGroups: ["metrics.k8s.io"]
    resources: ["nodes", "pods"]
    verbs: ["get", "list"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: descheduler
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: descheduler
subjects:
  - kind: ServiceAccount
    name: descheduler
    namespace: descheduler
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: descheduler-policy
  namespace: descheduler
data:
  policy.yaml: |
    apiVersion: descheduler/v1alpha2
    kind: DeschedulerPolicy
    profiles:
      - name: default
        pluginConfig:
          - name: DefaultEvictor
            args:
              evictLocalStoragePods: false
              evictSystemCriticalPods: false
              ignorePvcPods: true
              nodeFit: true
              priorityThreshold:
                value: 10000
              namespaceSelector:
                matchExpressions:
                  - key: kubernetes.io/metadata.name
                    operator: NotIn
                    values: [kube-system, kube-public, monitoring, istio-system]
          - name: RemoveDuplicates
          - name: LowNodeUtilization
            args:
              thresholds:
                cpu: 20
                memory: 20
              targetThresholds:
                cpu: 50
                memory: 50
          - name: RemovePodsViolatingNodeTaints
          - name: RemovePodsViolatingTopologySpreadConstraint
            args:
              constraints:
                - DoNotSchedule
        plugins:
          balance:
            enabled:
              - RemoveDuplicates
              - LowNodeUtilization
              - RemovePodsViolatingTopologySpreadConstraint
          deschedule:
            enabled:
              - RemovePodsViolatingNodeTaints
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: descheduler
  namespace: descheduler
spec:
  replicas: 1
  selector:
    matchLabels:
      app: descheduler
  template:
    metadata:
      labels:
        app: descheduler
    spec:
      serviceAccountName: descheduler
      containers:
        - name: descheduler
          image: registry.k8s.io/descheduler/descheduler:v0.34.0
          args:
            - --policy-config-file=/policy-dir/policy.yaml
            - --descheduling-interval=10m
            - --v=3
          volumeMounts:
            - name: policy-volume
              mountPath: /policy-dir
          resources:
            requests:
              cpu: 100m
              memory: 128Mi
            limits:
              memory: 256Mi
      volumes:
        - name: policy-volume
          configMap:
            name: descheduler-policy
```

## 安全：第一次上生产的正确顺序

千万不要一上来就在生产开。步骤：

1. **Dry run**：Descheduler 支持 `--dry-run=true`，只报告会 evict 什么，不真动。先跑几天看看 report；
2. **白名单 opt-in**：挑一两个低风险 namespace 打上 `descheduler.kubernetes.io/enabled=true`，让 descheduler 只管这些；
3. **观察一周**：看业务有没有异常、Pod evict 频率是不是合理、PDB 有没有被频繁卡住；
4. **逐步扩 namespace**；
5. **全量开**后要继续监控一个月。

## 监控

Descheduler 的 Prometheus metrics：

- `descheduler_pods_evicted_total`：按 strategy / namespace / reason 的 evict 计数；
- `descheduler_loop_duration_seconds`：每次主循环耗时；
- `descheduler_strategy_total`：每个策略被触发的次数。

告警：

```yaml
- alert: DeschedulerHighEviction
  expr: |
    sum by (namespace) (rate(descheduler_pods_evicted_total[1h])) > 1
  for: 30m
  labels:
    severity: warning
  annotations:
    summary: "Descheduler 正在 {{ $labels.namespace }} namespace 频繁 evict pod"
```

这个告警的目的是抓抖动——正常情况下 descheduler 不应该每小时 evict 超过 1 个 Pod。如果频率上去了，说明集群有 "持续不均衡" 的问题，或者 descheduler 配置激进。

## 几个实际踩过的坑

### 坑 1：LowNodeUtilization 激烈抖动

我们某次把 thresholds 和 targetThresholds 设得太近（20% / 30%），结果 descheduler 每 10 分钟 evict 几十个 pod。业务投诉。

原因：kube-scheduler 的 LeastAllocated 策略会把新 Pod 放到最闲的 node，但如果"最闲"之后又变"最忙"，descheduler 又会 evict。形成震荡。

解决：
- thresholds 和 targetThresholds 之间留至少 30% 的 gap；
- 给 `maxNoOfPodsToEvictPerNode` 设限（比如 5）；
- 给 descheduler 加 `--v=4` 观察几轮决策过程。

### 坑 2：PDB 配置不足导致长期 stuck

PDB 设成 `minAvailable: 100%`，descheduler 永远 evict 不了。日志里一堆 "cannot evict due to PDB"。

解决：PDB 用 `maxUnavailable: 1` 代替 `minAvailable: 100%`，表达更准确。

### 坑 3：RemoveDuplicates 对 StatefulSet 的意外效果

某个 StatefulSet 3 副本全在一个 node。descheduler 开了 RemoveDuplicates。结果 evict 了 2 个 StatefulSet Pod。StatefulSet Pod 重建时要挂 PVC，AZ 对不上，pending 了 20 分钟。

教训：StatefulSet 的 topology 要提前规划好（用 zone 级 topologySpreadConstraints），不要让 descheduler 去修。或者 `RemoveDuplicates` 加 `excludeOwnerKinds: [StatefulSet]`。

### 坑 4：nodeFit 没开导致 Pod 在原地 pending

没开 nodeFit，descheduler evict 一个 Pod，但别的 node 根本没地方放，Pod 在原 node 重启，循环一圈又被 evict。日志非常混乱。

解决：**永远开 nodeFit**。这个默认值 false 是历史原因，社区建议生产必开。

### 坑 5：RemovePodsViolatingNodeTaints 和 drain 冲突

有一次我们同时跑了一个批量 drain 脚本和 descheduler。drain 脚本会加 taint + evict，descheduler 也会 evict，两边一起 evict 同一个 Pod，PDB 被踩爆。

教训：节点维护期间临时关掉 descheduler。可以加一条 "在打某种 label 的 node 上不执行"。

## 一个让我很喜欢的组合

生产我最推崇的配置是：

1. **DefaultEvictor**：nodeFit=true, priorityThreshold=10000, namespace opt-in；
2. **RemoveDuplicates**：防止副本共置；
3. **LowNodeUtilization**：thresholds 20/20，targetThresholds 55/55；
4. **RemovePodsViolatingNodeTaints**：配合 drain / 维护；
5. **RemovePodsViolatingTopologySpreadConstraint**：配合 TSC 一起用。

这套配置在多个中大型集群稳定跑了很久。关键是 opt-in + PDB 覆盖率 + priorityThreshold 三件套一个都不能少。

## 什么时候不用 descheduler

- 集群很小（< 10 node），手动 rollout restart 就能搞定；
- 使用 Karpenter 激进 consolidation 的集群，Karpenter 已经在频繁改动，descheduler 再插一脚会互相打架；
- 业务对 Pod 重启极端敏感（比如长连接 WebSocket、TCP 游戏服务器），这类服务应该通过更强的 PDB 和手动流程管理。

## 收尾

Descheduler 的使用准则其实很少：

- nodeFit 必开；
- PDB 必须完整；
- priorityThreshold 隔离关键和非关键业务；
- opt-in 而非 opt-out；
- 监控抖动频率；
- 和 Karpenter / autoscaler 协调好优先级。

它不是一个"装好就忘"的组件，而是一个你要和集群一起演进的"日常清洁工"。正常情况下它做的事情默默无闻；当集群不均衡时它帮你修；当你运维失误时它会给你 feedback。

我个人的经验：生产 Kubernetes 跑超过 100 node，不装 descheduler 的结果几乎肯定是冷热不均和 pod 堆积。早装早省心。
