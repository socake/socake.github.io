---
title: "Karmada 多集群联邦实战：PropagationPolicy、OverridePolicy 与 FailOver 的真实用法"
date: 2025-03-02T11:20:00+08:00
draft: false
tags: ["Karmada", "多集群", "Kubernetes", "联邦"]
categories: ["云原生"]
description: "把 Karmada 当作多集群的控制平面跑了一年之后，我把 PropagationPolicy / ClusterPropagationPolicy / OverridePolicy / ClusterResourceBinding 的生产配置、踩坑、failover 策略、和 ArgoCD / GitOps 的协作方式，全部写进这一篇。"
summary: "如果你有 2 个以上 Kubernetes 集群，跨集群发同一个应用这件事迟早成为你的日常。Karmada 是 CNCF 孵化项目里做多集群联邦最完整的一个，但它的 CRD 设计比较克制，生产要用得好，得理清资源分发、差异覆盖、调度和 failover 四层语义。"
toc: true
math: false
diagram: false
keywords: ["Karmada", "PropagationPolicy", "OverridePolicy", "ClusterResourceBinding", "Kubernetes federation", "multi-cluster"]
params:
  reading_time: true
---

## 谁需要 Karmada

先澄清一件事：不是所有多集群场景都需要 Karmada。

真正需要的大致是这三类：

1. **同一个应用要跨集群部署**，但你不想写 N 份 yaml、让 CI/CD 对 N 个集群 kubectl apply。
2. **跨集群 failover**：A 集群挂了，把应用自动切到 B 集群。
3. **统一的策略管理**：某个应用在 us 集群跑 2 副本，cn 集群跑 5 副本，资源限制不同，想在一个地方统一管理差异。

如果你只是"有两个集群但它们各自跑自己的东西"，没必要上 Karmada。两个 ArgoCD Project 也能搞定。

Karmada 的价值是把"多集群"抽象成一个"逻辑集群"——你对这个逻辑集群下发资源，它会把资源按策略同步到成员集群。

## Karmada 的架构

Karmada 控制平面的组件：

```
                    ┌────────────────────────────┐
                    │    Karmada API Server      │
                    │   (etcd + kube-apiserver)  │
                    └────────┬───────────────────┘
                             │
   ┌─────────────┬──────────┴─────────┬────────────┐
   │             │                    │            │
   ▼             ▼                    ▼            ▼
 karmada-    karmada-            karmada-     karmada-
 controller- scheduler           webhook       aggregated-
 manager                                        apiserver
   │             │                    │
   │             │                    │
   ▼             ▼                    ▼
 (reconcile    (决定资源          (校验/变更
  Resource      下发到哪些           CRD 请求)
  Binding)     成员集群)
   │
   │ push/pull
   ▼
 ┌──────────┐   ┌──────────┐   ┌──────────┐
 │ Cluster1 │   │ Cluster2 │   │ Cluster3 │
 │(member)  │   │(member)  │   │(member)  │
 └──────────┘   └──────────┘   └──────────┘
```

关键组件：

- **karmada-apiserver**：Karmada 自己的 API Server，和 Kubernetes API Server 代码是一样的，但跑在 Karmada 控制平面里，它**不跑任何业务 workload**，只存 CRD、Deployment 等资源的"模板"。
- **karmada-controller-manager**：watches PropagationPolicy、Deployment 等资源，生成 ResourceBinding。
- **karmada-scheduler**：根据 PropagationPolicy 的 placement，决定每个 ResourceBinding 分发到哪些成员集群。
- **karmada-aggregated-apiserver**：提供对成员集群的 "access" 能力，比如 `karmadactl exec` 进一个成员集群的 Pod。
- **karmada-webhook**：CRD 校验和默认值注入。
- **karmada-agent**（pull 模式）：装在成员集群里，主动把 control plane 的资源同步到本地。
- **karmada-execution-controller**（push 模式）：在 control plane 里，把资源推到成员集群。

Karmada 支持两种成员集群注册模式：

- **Push 模式**：control plane 通过 kubeconfig 直接访问成员集群。适合内网互通的场景。
- **Pull 模式**：成员集群跑一个 agent，主动连 control plane。适合 control plane 和成员集群不能直连（防火墙、公网隔离）的场景。

生产用哪种？看你的网络情况。我们的做法：同 VPC 的集群用 push，跨公网的用 pull。

## 核心 CRD 全景

Karmada 的 CRD 分成几大类。上手前至少要知道这几个：

**资源分发类**：

- `PropagationPolicy`：namespace-scoped，定义"什么资源要同步到什么成员集群"。
- `ClusterPropagationPolicy`：cluster-scoped 版本，可以同步 cluster-scoped 资源（比如 ClusterRole）。

**差异覆盖类**：

- `OverridePolicy`：namespace-scoped，定义"资源同步到不同集群时需要怎么改"。
- `ClusterOverridePolicy`：cluster-scoped。

**成员集群管理**：

- `Cluster`：注册的成员集群，带 label 和 taint，供 PropagationPolicy 的 placement 匹配。

**内部资源**（一般不直接改）：

- `ResourceBinding` / `ClusterResourceBinding`：PropagationPolicy 匹配之后生成的绑定对象，scheduler 看它来调度。
- `Work`：每个成员集群对应一个 Work，Work 里封装了要下发到那个集群的实际对象。这是"最接地"的一层。

**多集群调度类**：

- `MultiClusterIngress`：多集群 Ingress。
- `MultiClusterService`：跨集群服务发现。

生产最常用的只有 PropagationPolicy、OverridePolicy、Cluster 这三个。

## 第一个 PropagationPolicy

一个最简单的例子：

```yaml
apiVersion: policy.karmada.io/v1alpha1
kind: PropagationPolicy
metadata:
  name: nginx-propagation
  namespace: default
spec:
  resourceSelectors:
    - apiVersion: apps/v1
      kind: Deployment
      name: nginx
  placement:
    clusterAffinity:
      clusterNames:
        - us-prod
        - cn-prod
```

这条策略说："在 default namespace 里找到名叫 nginx 的 Deployment，把它同步到 us-prod 和 cn-prod 这两个 member 集群"。

你要在 Karmada control plane 上 `kubectl apply` 这个 Deployment：

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: nginx
  namespace: default
spec:
  replicas: 3
  selector:
    matchLabels:
      app: nginx
  template:
    metadata:
      labels:
        app: nginx
    spec:
      containers:
        - name: nginx
          image: nginx:1.27
```

`kubectl --kubeconfig=karmada.config apply -f deployment.yaml`，然后 `kubectl --kubeconfig=karmada.config get deploy -n default`，你会看到 nginx。但注意：**Karmada 自己不跑 Pod**，Deployment 在 karmada-apiserver 里只是一个"模板"。真正的 Pod 在 member 集群。

要看 Pod 你得切到 member 集群的 kubeconfig。

## placement 的几种方式

`placement` 是 PropagationPolicy 的灵魂，它决定"同步到哪些集群"。

### clusterAffinity

最直接的方式。三种子字段：

1. **clusterNames**：直接列集群名。
2. **labelSelector**：按 label 选。
3. **fieldSelector**：按字段选（比如 provider、region、zone）。

```yaml
placement:
  clusterAffinity:
    labelSelector:
      matchLabels:
        environment: production
      matchExpressions:
        - key: region
          operator: In
          values: ["us-west-2", "cn-hangzhou"]
```

### clusterAffinities（注意这是复数）

和单数 `clusterAffinity` 不同，`clusterAffinities` 可以声明多个候选组，按 `affinityName` 命名，配合调度器做 failover 时非常好用：

```yaml
placement:
  clusterAffinities:
    - affinityName: primary
      clusterNames: ["us-prod"]
    - affinityName: backup
      clusterNames: ["cn-prod", "eu-prod"]
```

Karmada 会先尝试 primary，如果 primary 不满足条件（集群不存在/不健康/不够资源），按 spreadConstraint 往下切换。

### clusterTolerations

Member 集群可以有 taint，比如 `maintenance=true:NoSchedule`。默认 PropagationPolicy 不会调度到带 taint 的集群。如果你希望在维护期也能调度，加 toleration：

```yaml
placement:
  clusterTolerations:
    - key: maintenance
      operator: Equal
      value: "true"
      effect: NoSchedule
```

### spreadConstraints

跨集群的"分散约束"，类似 Kubernetes 内部的 topologySpreadConstraints，但粒度到集群：

```yaml
placement:
  spreadConstraints:
    - spreadByField: region
      maxGroups: 2
      minGroups: 1
    - spreadByField: provider
      maxGroups: 3
      minGroups: 2
```

`spreadByField` 可以是 `cluster` / `region` / `zone` / `provider`，决定按哪个维度分散。`minGroups` / `maxGroups` 决定最少/最多分散到几个组。

## ReplicaSchedulingStrategy：副本怎么分

这是最细节也最强大的一块。`ReplicaSchedulingStrategy` 决定："总共 10 个副本，分到 3 个集群，每个集群几个？"

```yaml
placement:
  replicaScheduling:
    replicaSchedulingType: Divided
    replicaDivisionPreference: Weighted
    weightPreference:
      staticWeightList:
        - targetCluster:
            clusterNames: ["us-prod"]
          weight: 2
        - targetCluster:
            clusterNames: ["cn-prod"]
          weight: 1
```

`replicaSchedulingType`：

- `Duplicated`（默认）：每个集群都跑完整的 replicas 数。比如 Deployment.replicas=3 会变成"每个集群都 3 个副本"。
- `Divided`：把 replicas 总数划分到各个集群。

`replicaDivisionPreference`：
- `Aggregated`：尽量把副本集中在少数集群，"够用就不分散"；
- `Weighted`：按静态权重或动态权重分配。

**Aggregated** 适合"跨集群 failover"的场景，正常情况下只在 primary 跑，primary 挂了才切 backup。

**Weighted** 适合"按容量分流"的场景，哪个集群容量大分多点。

### 动态权重：按可用资源

```yaml
replicaScheduling:
  replicaSchedulingType: Divided
  replicaDivisionPreference: Weighted
  weightPreference:
    dynamicWeight: AvailableReplicas
```

`dynamicWeight: AvailableReplicas` 让 Karmada 根据每个集群当前可用的 replica 数（剩余调度能力）动态分配。这个比静态权重更好用，但前提是你的成员集群 metrics 能正常上报。

## OverridePolicy：集群差异

假设 us 集群要跑 10 副本，cn 集群要跑 5 副本，资源请求也不一样。副本数的差异可以通过 ReplicaSchedulingStrategy 解决，但"镜像 tag 不同" / "环境变量不同" 这种差异需要 OverridePolicy：

```yaml
apiVersion: policy.karmada.io/v1alpha1
kind: OverridePolicy
metadata:
  name: nginx-override
  namespace: default
spec:
  resourceSelectors:
    - apiVersion: apps/v1
      kind: Deployment
      name: nginx
  overrideRules:
    - targetCluster:
        clusterNames: ["us-prod"]
      overriders:
        plaintext:
          - path: "/spec/template/spec/containers/0/image"
            operator: replace
            value: "registry.us.example.com/nginx:1.27"
          - path: "/spec/template/spec/containers/0/env/0/value"
            operator: replace
            value: "us-prod"
    - targetCluster:
        clusterNames: ["cn-prod"]
      overriders:
        plaintext:
          - path: "/spec/template/spec/containers/0/image"
            operator: replace
            value: "registry.cn.example.com/nginx:1.27"
```

`overriders` 有几种：

- **plaintext**：JSON Patch 语法，直接打补丁。最通用。
- **imageOverrider**：专门处理镜像替换，能按 component 替换 registry / repository / tag。
- **commandOverrider** / **argsOverrider**：改 container 的 command / args。
- **labelsOverrider** / **annotationsOverrider**：改 metadata 里的 labels / annotations。

`imageOverrider` 的例子：

```yaml
overriders:
  imageOverrider:
    - predicate:
        path: "/spec/template/spec/containers/0/image"
      component: Registry
      operator: replace
      value: "registry.us.example.com"
```

这会把 us-prod 集群里的 nginx image 的 registry 部分替换成内部 registry，tag 和 repository 不动。多集群跨 registry 迁移时特别好用。

## ClusterPropagationPolicy 和 namespace 分发

ClusterPropagationPolicy 比 PropagationPolicy 多了一个能力：可以分发 **cluster-scoped 资源**，比如 Namespace、ClusterRole、CRD。

```yaml
apiVersion: policy.karmada.io/v1alpha1
kind: ClusterPropagationPolicy
metadata:
  name: app-namespace
spec:
  resourceSelectors:
    - apiVersion: v1
      kind: Namespace
      name: app-prod
  placement:
    clusterAffinity:
      clusterNames: ["us-prod", "cn-prod"]
```

Namespace 这个资源比较特殊：它既可以是单纯的 namespace 对象本身，也可能"连着"一堆 namespace-scoped 的资源。Karmada **不会自动连带分发里面的资源**，你要对每个资源单独写 PropagationPolicy。

小技巧：Karmada 有一个默认行为，成员集群注册之后，control plane 里创建的 namespace 会被自动同步到所有 member 集群（可通过 `--skipped-propagating-namespaces` 配置）。所以大多数情况下你不用手动写 namespace 的 ClusterPropagationPolicy。

## MultiClusterService：跨集群服务发现

MultiClusterService 是 Karmada 的跨集群服务暴露机制。典型用法：

```yaml
apiVersion: networking.karmada.io/v1alpha1
kind: MultiClusterService
metadata:
  name: nginx
  namespace: default
spec:
  types:
    - CrossCluster
  ports:
    - port: 80
      protocol: TCP
  serviceConsumptionClusters:
    - us-prod
  serviceProvisionClusters:
    - us-prod
    - cn-prod
```

意思是：us-prod 集群里的 pod 可以访问 `nginx.default.svc`，流量可能会被转发到 us-prod 或 cn-prod 的 nginx Pod。

这个能力依赖成员集群之间的 Pod / Service 网络互通。如果你的集群网络是独立的 VPC 且没打通，MultiClusterService 做不到，只能用外部负载均衡或者自己搭 service mesh。

所以生产上 MultiClusterService 的应用场景其实比较受限，更多团队还是用 Istio 多集群或者 Submariner 搭跨集群网络。

## FailOver：集群出问题自动切

Karmada 原生支持 cluster failover。配置：

```yaml
apiVersion: policy.karmada.io/v1alpha1
kind: PropagationPolicy
metadata:
  name: nginx-ha
  namespace: default
spec:
  resourceSelectors:
    - apiVersion: apps/v1
      kind: Deployment
      name: nginx
  placement:
    clusterAffinities:
      - affinityName: primary
        clusterNames: ["us-prod"]
      - affinityName: backup
        clusterNames: ["cn-prod"]
    replicaScheduling:
      replicaSchedulingType: Duplicated
  failover:
    application:
      decisionConditions:
        tolerationSeconds: 120
      purgeMode: Graciously
      gracePeriodSeconds: 60
```

`failover.application.decisionConditions.tolerationSeconds`：集群 unreachable 多久后触发 failover。生产建议 2-5 分钟，太短容易误判。

`purgeMode`：
- `Immediately`：立刻在原集群清理；
- `Never`：从不清理（永远留在原集群）；
- `Graciously`：等原集群恢复后再清理。

生产推荐 `Graciously`，避免"集群短暂 unreachable + 正在处理中的 Pod 被杀"这种风险。

**需要理解的一点**：Karmada failover 是"应用级别"的，不是"流量级别"的。它不会自动改 DNS 或负载均衡器。真正的用户流量切换，得靠你的前端 LB / DNS / 服务网格。Karmada 只是确保 Pod 在 backup 集群起来。

## Karmada + ArgoCD：GitOps 怎么搭

Karmada 和 ArgoCD 不冲突，两者可以结合。常见的三种架构：

### 架构 A：ArgoCD 直接管 Karmada

把 Karmada 当作一个"集群"加到 ArgoCD 里，ArgoCD apply yaml 到 Karmada，Karmada 再分发。

优点：GitOps 只有一条链路；
缺点：ArgoCD 的 sync status 只反映 Karmada control plane 的状态，不反映真正的 member 集群。Karmada 下发失败 ArgoCD 看不到。

### 架构 B：ArgoCD 分别管每个 member 集群 + Karmada 只做 placement

ArgoCD 直接连各 member 集群，apply 应用；Karmada 只负责"策略类"资源，比如 PodDisruptionBudget、NetworkPolicy 这种。

优点：ArgoCD sync status 真实；
缺点：多集群差异逻辑要在 ArgoCD 的 ApplicationSet 里写，等于重新实现 OverridePolicy。

### 架构 C：ArgoCD + Karmada 联合，Karmada 作为应用分发器

ArgoCD 管 Karmada 的 PropagationPolicy / OverridePolicy，让 Karmada 负责分发。ArgoCD 不直接连 member 集群。

这是官方推荐，我们线上用的也是这套。关键点：

- ArgoCD Application 的 destination 是 Karmada apiserver；
- ArgoCD Application sync 成功只表示"已写入 Karmada"，不代表"已分发到 member 集群"；
- 多维护一个 watcher 盯 member 集群的 Work 资源状态，和 ArgoCD 解耦。

## 监控 Karmada 本身

Karmada 的 Prometheus metrics 走 `karmada-controller-manager` 和 `karmada-scheduler`。关键指标：

- `karmada_schedule_attempts_total{result="..."}`：调度尝试次数，按结果分类；
- `karmada_resource_match_policy`：资源匹配到 PropagationPolicy 的次数；
- `karmada_cluster_ready_condition`：每个成员集群的 Ready 状态；
- `karmada_work_status`：Work 资源的 apply 状态。

核心告警：

```yaml
- alert: KarmadaClusterNotReady
  expr: |
    karmada_cluster_ready_condition{condition="Ready"} == 0
  for: 5m
  labels:
    severity: critical
  annotations:
    summary: "Karmada 成员集群 {{ $labels.cluster_name }} 不 Ready"

- alert: KarmadaScheduleFailures
  expr: |
    rate(karmada_schedule_attempts_total{result="failure"}[10m]) > 0.1
  for: 10m
  labels:
    severity: warning
  annotations:
    summary: "Karmada 调度失败率上升"
```

## 生产踩过的坑

### 坑 1：namespace 同步的默认跳过列表

Karmada 默认会把 control plane 里的 namespace 自动同步到所有成员集群，但有一些是跳过的：`karmada-system`、`karmada-cluster`、`karmada-es-*`、`kube-*` 等。如果你给应用建了个 namespace 叫 `kube-app-prod`，它永远不会被同步。教训：不要给业务用 `kube-` 开头的 namespace。

### 坑 2：ResourceTemplate 和 member 集群里的实际资源有漂移

用户可能直接连 member 集群 kubectl edit 某个 Deployment。Karmada 会看到漂移，默认会覆盖回去（reconcile）。但某些资源字段比如 `status`、或者 admission controller 自动注入的字段，Karmada 不管。排障时记住一个原则：**control plane 上的是期望值，member 集群上是实际值，两者不一致先查 OverridePolicy 和 Work**。

### 坑 3：CRD 的分发比较复杂

Karmada 对 CRD 的支持有两层：一是 CRD 资源本身可以通过 ClusterPropagationPolicy 分发（复制 CRD 定义到 member 集群），二是基于这个 CRD 的 CR 需要你**告诉 Karmada 这个 CRD 怎么 interpret**。后者通过 `ResourceInterpreterCustomization` 实现，比较复杂，我一般建议：能用原生 Deployment/Service 搞定的就别上自定义 CRD 走 Karmada。

### 坑 4：control plane 的 etcd 容量

Karmada control plane 跑着一整个 Kubernetes，但它不跑 Pod。它的 etcd 存的是所有模板资源 + ResourceBinding + Work。对于大集群来说这些加起来也能上 GB 级。我们 15 个 namespace、200+ deployment 的规模下，etcd 大概 800MB。比普通 Kubernetes 的 etcd 小，但不能忽略。

### 坑 5：pull 模式的 agent 升级

pull 模式下，每个 member 集群里都跑一个 karmada-agent。control plane 升级 Karmada 版本后，agent 也要升。agent 版本和 control plane 版本不匹配时 reconcile 会怪异地半失败。升级时一定要按"control plane → 所有 agent"的顺序。

### 坑 6：ArgoCD 和 Karmada 的 sync 语义

ArgoCD 的 "Synced" 不等于 "部署成功"。ArgoCD 只知道资源已经被 apply 到 Karmada control plane，后面 Karmada 是否分发到了 member 集群，它不知道。监控里一定要盯 Work 的状态。

## 什么时候不用 Karmada

- 两个集群跑的东西完全不同——用 ArgoCD 分别管就行；
- 你只要跨集群 failover，不要跨集群配置分发——用 Istio 多集群 + 多个独立 ArgoCD Project；
- 你的团队没人愿意学 PropagationPolicy 的语义——真诚建议别上，否则下次故障没人能排；
- 你只有 1 个生产集群——不要过度设计，等到你有 3 个再说。

## 一些替代品

- **Cluster API** + 自研 controller：底层是独立的，不是联邦；
- **KubeFed**（旧）：SIG 已经 deprecated；
- **Clusternet**：国内团队做的，思路和 Karmada 接近，规模较小；
- **OCM (Open Cluster Management)**：Red Hat 的方案，更重，更适合企业级多租户。

Karmada 是这几个里"CRD 设计最克制、社区活跃度最好、国内外都有生产案例"的一个。如果你决定搞多集群联邦，它是当前的首选。

## 最后的几句

Karmada 的学习曲线集中在"资源分发 + 差异覆盖 + 调度"这三个层面。一旦你理解了 PropagationPolicy 和 OverridePolicy 如何组合，剩下的都是细节。生产上最重要的原则：

- **先做 placement，再做 override**：少就是多，别过度覆盖；
- **一个 Deployment 只被一个 PropagationPolicy 管**：多 policy 匹配到同一个资源时会走优先级，容易出难追查的问题；
- **failover 不等于用户流量切换**：域名和 LB 还是你自己的事；
- **monitor Work 而非 Policy**：Work 是最接近 member 集群实际状态的一层。

Karmada 把"管理 N 个集群"这件事从 N 倍工作量降到 1 倍加 30%。那 30% 就是学 Karmada 本身的成本。一旦翻过这个坎，它是真的好用。
