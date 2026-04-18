---
title: "Kueue 批处理调度实战：让 Kubernetes 真正承担 AI/HPC 工作负载"
date: 2025-03-15T09:40:00+08:00
draft: false
tags: ["Kueue", "Kubernetes", "批处理", "AI训练", "调度"]
categories: ["Kubernetes"]
description: "Kueue v1beta2 在生产中用来调度 AI 训练任务、批处理 Job、RayJob、JobSet 的完整实战：ResourceFlavor / ClusterQueue / LocalQueue / Cohort 四层模型、all-or-nothing 语义、抢占、公平共享、MultiKueue 跨集群、GPU 资源隔离、以及和原生 Job / Kubeflow 的组合方式。"
summary: "把 AI 训练任务塞进 Kubernetes，第一天你会发现原生调度器完全不够用：没有队列、没有 quota、没有 gang scheduling、没有公平共享、preemption 语义一塌糊涂。Kueue 是 sig-scheduling 官方给出的答案，它比 Volcano 更贴近 Kubernetes 原生、比自研 controller 更成熟。这是一份真实的生产笔记。"
toc: true
math: false
diagram: false
keywords: ["Kueue", "Kubernetes batch", "ResourceFlavor", "ClusterQueue", "gang scheduling", "AI training", "GPU scheduling"]
params:
  reading_time: true
---

## 为什么 HPA + kube-scheduler 不够

Kubernetes 的原生调度器是"一个 Pod 一个 Pod 做决策"的。这个模型对在线服务完美，对批处理就是灾难。

批处理 / AI 训练的典型需求：

1. **All-or-nothing**：一个 8 卡训练任务，要么 8 张卡都到位一起开跑，要么一张都不起。只起 6 张在那干等剩下 2 张，是典型的资源死锁。
2. **队列**：同一时间想跑的 job 可能有几十个，但 GPU 只够跑 5 个。剩下的要排队，按优先级/提交顺序等。
3. **Quota / 配额**：每个团队有自己的 GPU 预算，不能互相抢。
4. **公平共享**：虽然有 quota，但资源闲置时谁先来谁先用，避免浪费。
5. **抢占**：高优先级 job 来了，把低优先级 job 的 Pod 踢掉。
6. **资源类别（flavor）**：你有 A100 也有 H100，也有 spot 和 on-demand，任务要能指定倾向。

原生 kube-scheduler 一样不支持。Kueue 就是在这个需求下长出来的。

历史上解决这个问题的有：

- **Volcano**：和 Kubernetes 比较独立的一套 API，历史久但社区比较分散；
- **YuniKorn**：Apache 项目，主打大数据；
- **Kueue**：Kubernetes SIG 官方项目，API 设计和 Kubernetes 原生 Job / RBAC / Namespace 完全贴合。

如果你是从零开始做选型，我推荐 Kueue。它是 v1beta2 了，生产跑几千 job 的案例已经不少。

## Kueue 的四层对象模型

理解 Kueue 就是理解它的四层对象：

```
                   ┌─────────────────┐
                   │  ResourceFlavor │   <- "资源类别" 比如 a100-on-demand
                   └────────┬────────┘
                            │
                            ▼
                   ┌─────────────────┐
                   │  ClusterQueue   │   <- "配额池", 定义谁能用多少
                   └────────┬────────┘
                            │
                            ▼
                   ┌─────────────────┐
                   │   LocalQueue    │   <- "团队入口", namespace-scoped
                   └────────┬────────┘
                            │
                            ▼
                   ┌─────────────────┐
                   │   Workload      │   <- Job / RayJob / JobSet / Pod 组
                   └─────────────────┘
```

这四层要分开理解：

### ResourceFlavor

对"资源"的分类。一个 flavor 可以是：

- GPU 型号（a100 / h100 / v100）
- 节点生命周期（on-demand / spot）
- 区域（us-west / us-east）
- 架构（amd64 / arm64）

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: ResourceFlavor
metadata:
  name: a100-on-demand
spec:
  nodeLabels:
    nvidia.com/gpu.product: "NVIDIA-A100-SXM4-40GB"
    karpenter.sh/capacity-type: "on-demand"
  nodeTaints:
    - key: nvidia.com/gpu
      value: "true"
      effect: NoSchedule
  tolerations:
    - key: nvidia.com/gpu
      operator: Exists
      effect: NoSchedule
```

ResourceFlavor 不直接分配资源，它只是一个"标签"。Kueue 根据这个标签把 Workload 往对应的 node 上调。

### ClusterQueue

配额池。它声明"这个队列最多可以用多少 CPU / 内存 / GPU"，并且允许哪些 flavor：

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: ClusterQueue
metadata:
  name: ai-team-queue
spec:
  namespaceSelector: {}
  queueingStrategy: BestEffortFIFO
  cohort: ai
  preemption:
    reclaimWithinCohort: LowerPriority
    borrowWithinCohort:
      policy: LowerPriority
    withinClusterQueue: LowerPriority
  resourceGroups:
    - coveredResources:
        - cpu
        - memory
        - nvidia.com/gpu
      flavors:
        - name: a100-on-demand
          resources:
            - name: cpu
              nominalQuota: "96"
              borrowingLimit: "192"
            - name: memory
              nominalQuota: "768Gi"
              borrowingLimit: "1536Gi"
            - name: nvidia.com/gpu
              nominalQuota: "8"
              borrowingLimit: "16"
        - name: h100-on-demand
          resources:
            - name: cpu
              nominalQuota: "0"
              borrowingLimit: "128"
            - name: memory
              nominalQuota: "0"
              borrowingLimit: "1024Gi"
            - name: nvidia.com/gpu
              nominalQuota: "0"
              borrowingLimit: "8"
```

重要字段：

- **cohort**：队列组。同一个 cohort 里的队列可以互借资源。
- **nominalQuota**：保证配额。这个队列一定能用这么多。
- **borrowingLimit**：借用上限。当 cohort 里其他队列有空闲时，这个队列能借用多少。
- **queueingStrategy**：
  - `StrictFIFO`：严格按提交顺序。前面的 job 卡住，后面的都等；
  - `BestEffortFIFO`：尽量按顺序，但如果前面的 job 因为资源不够不能启动，Kueue 会尝试后面的。
- **preemption**：抢占策略。
  - `reclaimWithinCohort`：从 cohort 内其他队列抢回借出的资源；
  - `borrowWithinCohort`：是否允许借资源时抢占；
  - `withinClusterQueue`：队列内部是否允许高优先级抢占低优先级。

生产上 preemption 配得好不好决定了高峰期跑不跑得动。

### LocalQueue

LocalQueue 是 namespace 级别的 "入口"。业务不直接提交 Job 到 ClusterQueue，而是通过它的 namespace 里的 LocalQueue：

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: LocalQueue
metadata:
  name: default
  namespace: ai-team-a
spec:
  clusterQueue: ai-team-queue
```

然后 Job 上打一个 label：

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: train-mnist
  namespace: ai-team-a
  labels:
    kueue.x-k8s.io/queue-name: default
spec:
  parallelism: 4
  completions: 4
  suspend: true                        # !! 一定要 suspend
  template:
    spec:
      containers:
        - name: train
          image: registry.example.com/train:1.0
          resources:
            requests:
              cpu: "4"
              memory: 16Gi
              nvidia.com/gpu: 1
            limits:
              nvidia.com/gpu: 1
      restartPolicy: Never
```

**两个必须的点**：

1. Label `kueue.x-k8s.io/queue-name` 指定 LocalQueue 名字；
2. **`spec.suspend: true`**，这是让 Job 以"挂起"状态提交。Kueue 看到 suspended 的 Job 才会接管——把它塞进队列、等资源够了再 unsuspend。

如果你提交一个没 suspend 的 Job，Kueue 默认不管，kube-scheduler 会立刻调度它，就完全绕过了 Kueue。

## 最小实战：一个 AI 训练任务的完整链路

假设我们要跑一个 4 GPU 的训练任务。完整的 YAML 链路：

```yaml
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: ResourceFlavor
metadata:
  name: a100
spec:
  nodeLabels:
    nvidia.com/gpu.product: "NVIDIA-A100-SXM4-40GB"
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: ClusterQueue
metadata:
  name: ai-team
spec:
  namespaceSelector: {}
  cohort: ai
  resourceGroups:
    - coveredResources: ["cpu", "memory", "nvidia.com/gpu"]
      flavors:
        - name: a100
          resources:
            - name: cpu
              nominalQuota: "32"
            - name: memory
              nominalQuota: "256Gi"
            - name: nvidia.com/gpu
              nominalQuota: "4"
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: LocalQueue
metadata:
  name: default
  namespace: ai-dev
spec:
  clusterQueue: ai-team
---
apiVersion: batch/v1
kind: Job
metadata:
  name: llm-finetune
  namespace: ai-dev
  labels:
    kueue.x-k8s.io/queue-name: default
    kueue.x-k8s.io/priority-class: high-priority
spec:
  parallelism: 4
  completions: 4
  suspend: true
  template:
    metadata:
      labels:
        app: llm-finetune
    spec:
      restartPolicy: Never
      containers:
        - name: trainer
          image: registry.example.com/trainer:1.5
          command: ["/bin/bash", "-c", "torchrun --nproc_per_node=1 train.py"]
          resources:
            requests:
              cpu: "8"
              memory: "64Gi"
              nvidia.com/gpu: 1
            limits:
              nvidia.com/gpu: 1
```

提交后发生的事：

1. Kueue 看到一个 suspended 的 Job；
2. Job 属于 `ai-dev` namespace，匹配 LocalQueue `default`；
3. LocalQueue 指向 ClusterQueue `ai-team`；
4. Kueue 检查 ai-team 的 nominalQuota：4 个 GPU，训练任务要 4 个，刚好；
5. Kueue 创建一个 Workload 对象；
6. Kueue 等到资源足够时，把 Job 的 `spec.suspend` 改成 `false`；
7. Job 的 Pod 开始被 kube-scheduler 调度，跑到 a100 节点上。

如果同时来了两个 4-GPU 的 Job，ClusterQueue 只有 4 GPU，第二个会被挂起直到第一个完成。

## All-or-nothing：gang scheduling 的真意

AI 训练里最致命的不是"资源不够"，而是"资源不够但一部分 Pod 先被调度了"。8 卡的任务只有 5 张卡能被调度，剩下 3 张在 pending，那 5 张已经占着不干活，整个集群的其他 job 也跑不起来。

Kueue 的解法：**它只在"一次满足"的前提下 admit 一个 Workload**。

- 8-GPU 的 Job 提交了，Kueue 先评估 "ClusterQueue 里能不能一次给出 8 张 A100 + 对应的 CPU 和 memory"；
- 如果不够，Job 继续 suspended；
- 如果够，Kueue 一次性把 8 个 Pod 的资源都"占住"，然后 unsuspend Job。

这是最朴素但非常有效的 gang scheduling。配合 Kubernetes 的 Pod scheduling gate，Kueue 可以更精细地控制 Pod 何时被调度器看到，进一步降低资源抢占死锁的概率。

## Cohort 和借用：让资源不浪费

假设你有两个 ClusterQueue，都属于 cohort `ai`：

- `team-a`：nominalQuota = 4 GPU
- `team-b`：nominalQuota = 4 GPU

如果 team-a 有 8 GPU 需求、team-b 当前没任务，**能不能让 team-a 借用 team-b 的 4 张卡跑到 8？**

答案是：**可以，但前提是配了 `borrowingLimit`**。

```yaml
resourceGroups:
  - coveredResources: ["nvidia.com/gpu"]
    flavors:
      - name: a100
        resources:
          - name: nvidia.com/gpu
            nominalQuota: "4"
            borrowingLimit: "8"       # 最多借到 8（本队列总上限 12）
```

当 team-a 借了 team-b 的 4 张卡之后，如果 team-b 忽然来了一个任务怎么办？看 `preemption.reclaimWithinCohort`：

- `Never`：team-b 的任务只能等 team-a 跑完；
- `Any`：team-b 可以抢占 team-a 借去的那部分；
- `LowerPriority`：只有当 team-b 的任务优先级高于 team-a 借用的任务时才能抢。

生产建议：**`LowerPriority`**。给 job 打 priorityClass，高优先级可以抢。不要用 `Any`，会让低优先级任务被反复抢占，从不跑完。

## 抢占语义的细节

Kueue 的抢占是"批量"的，不是"一个 Pod 一个 Pod" 的。它会评估："为了让这个新 Workload 跑起来，需要驱逐哪些现存 Workload？" 然后一次性下决定。

驱逐一个 Workload 意味着：

- 对 Job，Kueue 把 `spec.suspend` 改回 `true`，Job controller 会删掉所有 Pod；
- 对 RayJob / JobSet，行为类似，整个 Workload 被挂起；
- Pod 被删，对 stateful 的训练任务来说，需要 checkpoint 恢复。

所以：**能被 Kueue 抢占的任务必须有 checkpoint 机制**。否则你是在杀生产任务。

对于"不能抢占"的任务，打一个 `kueue.x-k8s.io/priority-class` 是高优先级（或者用 `priorityClassName` 配合 Kubernetes PriorityClass），并且设置它为 non-preemptible（通过 `kueue.x-k8s.io/pod-group-fast-admission` 或者相关 annotation）。具体语法看文档，但原则是"不能被杀的任务要显式标记出来"。

## 和 RayJob / JobSet / Kubeflow 集成

Kueue 对这些高级工作负载类型有原生支持，在 ConfigMap 里开启即可：

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: Configuration
spec:
  integrations:
    frameworks:
      - "batch/job"
      - "kubeflow.org/mpijob"
      - "ray.io/rayjob"
      - "ray.io/raycluster"
      - "jobset.x-k8s.io/jobset"
      - "kubeflow.org/pytorchjob"
      - "kubeflow.org/tfjob"
      - "kubeflow.org/mxjob"
      - "kubeflow.org/xgboostjob"
```

几个注意：

1. 要确保对应的 CRD 已经被安装（Kueue 只是 integration，不装 CRD）；
2. RayJob 提交时仍然要加 label `kueue.x-k8s.io/queue-name`；
3. PyTorchJob 的 replica spec 里所有角色（Master/Worker）都会被算进 Workload 总资源；
4. JobSet 的 gang scheduling 和 Kueue 的 admission 配合效果最好，比原生 Job 更细粒度。

## Plain Pods 和 Pod Groups

如果你的任务不是 Job 而是一组裸 Pod（比如自己写的 controller），Kueue 也能管，需要开启 plain pod integration 并给 Pod 打上 label 声明它们是同一组：

```yaml
metadata:
  labels:
    kueue.x-k8s.io/queue-name: default
    kueue.x-k8s.io/pod-group-name: my-group
  annotations:
    kueue.x-k8s.io/pod-group-total-count: "4"
```

Kueue 会把 4 个 pod 当成一个 Workload 处理。但要注意：裸 Pod 的生命周期管理不如 Job 好，建议能用 Job / JobSet 就不用裸 Pod。

## 公平共享（Fair Sharing）

v0.7 之后 Kueue 引入了 Fair Sharing，目前在 v1beta2 里已经是稳定功能。它的作用是：当多个 ClusterQueue 在同一个 cohort 里竞争资源时，Kueue 不仅按 quota 分，还会按"当前使用量"动态分配，让长期使用资源最少的队列优先。

配置：

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: Configuration
spec:
  fairSharing:
    enable: true
    preemptionStrategies:
      - LessThanOrEqualToFinalShare
      - LessThanInitialShare
```

它改变的主要是抢占决策。生产里开了 Fair Sharing 之后，长期占用资源的队列会被"减持"，新的任务进来会分到资源。

## MultiKueue：跨集群调度

这是 Kueue 2026 的重头戏。MultiKueue 让你可以把一个 Workload 提交到 "managing cluster"，Kueue 会根据可用资源把它分发到某个 "worker cluster" 实际执行。

典型用法：

- Managing cluster：一个很小的集群，只跑 Kueue 控制平面；
- Worker clusters：若干个 GPU 集群，每个集群的 Kueue 自己管 local 资源；
- 业务把 Job 提交到 managing cluster 的 LocalQueue；
- Kueue 挑一个有空闲资源的 worker cluster，把 Job "影射" 过去实际运行；
- 结果通过 status sync 同步回 managing cluster。

配置简化示例：

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: MultiKueueCluster
metadata:
  name: gpu-west
spec:
  kubeConfig:
    locationType: Secret
    location: gpu-west-kubeconfig
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: MultiKueueConfig
metadata:
  name: multi-gpu
spec:
  clusters:
    - gpu-west
    - gpu-east
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: AdmissionCheck
metadata:
  name: multi-gpu
spec:
  controllerName: kueue.x-k8s.io/multikueue
  parameters:
    apiGroup: kueue.x-k8s.io
    kind: MultiKueueConfig
    name: multi-gpu
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: ClusterQueue
metadata:
  name: ai-multi
spec:
  admissionChecks:
    - multi-gpu
  # ...
```

MultiKueue 的主要坑：

- worker cluster 的 Kueue 版本要对齐；
- 网络互通必须保证，kubeconfig 访问得通；
- status sync 有延迟，别依赖"立刻看到 Pod 起来"；
- 失败重试语义：如果 worker cluster 挂了，managing cluster 会不会自动切到另一个？目前不是完全自动，某些场景下需要人工干预。

MultiKueue 我线上还在 QA 环境跑，生产上还在观望。社区在 2026 的路线图里把它列为重点，预期下半年生产就绪。

## GPU 场景的特殊配置

几个 GPU 场景下的常见配置：

### 1. 一个 Pod 多 GPU vs 多 Pod 一 GPU

- 数据并行（DDP / FSDP）：多 Pod 一 GPU，每个 Pod 1 张卡，通过 NCCL 通信；
- 模型并行：一个 Pod 多张卡，比如一个 Pod 2 张 A100 放模型不同层。

前者 Kueue 的 Workload 资源请求是 `requests: nvidia.com/gpu: 1` × N 个 pod，总 N 张卡。

后者是 `requests: nvidia.com/gpu: 2` × M 个 pod，每个 Pod 2 张，总 2M 张。

两种都能用 Kueue 管，但 all-or-nothing 语义意味着：M=4 个 Pod，每个 2 张卡，Kueue 必须一次给到 8 张卡才 admit。

### 2. 和 NVIDIA GPU Operator 的协作

GPU Operator 会往 node 上打一堆 label 和 taint：

- label: `nvidia.com/gpu.product=NVIDIA-A100-SXM4-40GB`
- taint: `nvidia.com/gpu=true:NoSchedule`

ResourceFlavor 的 `nodeLabels` 要对齐这些 label，tolerations 要对齐 taint。如果不对齐，Kueue 以为资源够、实际调度时 Pod 起不来。

### 3. MIG（Multi-Instance GPU）

A100/H100 的 MIG 能把一张卡切成几份。切分后的 "子 GPU" 在 K8s 里是不同的资源类型（比如 `nvidia.com/mig-2g.10gb`）。每种 MIG profile 可以在 ResourceFlavor 里单独定义一个 flavor，在 ClusterQueue 里分别配额。

## 监控和排障

Kueue 暴露的 Prometheus 指标：

- `kueue_pending_workloads`：每个队列里 pending 的 Workload 数；
- `kueue_admitted_workloads_total`：累计 admitted 数；
- `kueue_admission_wait_time_seconds`：从提交到 admit 的等待时间分布；
- `kueue_cluster_queue_resource_usage`：每个 ClusterQueue 每个 flavor 每种资源的当前使用量；
- `kueue_cluster_queue_nominal_quota`：配额。

最有用的告警：

```yaml
- alert: KueueAdmissionWaitTimeHigh
  expr: |
    histogram_quantile(0.9,
      sum by (le, cluster_queue) (
        rate(kueue_admission_wait_time_seconds_bucket[15m])
      )
    ) > 600
  for: 30m
  labels:
    severity: warning
  annotations:
    summary: "ClusterQueue {{ $labels.cluster_queue }} 的 P90 等待时间超过 10 分钟"

- alert: KueueQueueFullyUtilized
  expr: |
    kueue_cluster_queue_resource_usage
    / ignoring(resource_name) kueue_cluster_queue_nominal_quota > 0.9
  for: 1h
  labels:
    severity: info
  annotations:
    summary: "ClusterQueue {{ $labels.cluster_queue }} 资源使用率 > 90% 持续 1h"
```

排障的经典路径：

1. Job 提交了但不跑？
   - `kubectl get jobs -n ai-dev` 看 `spec.suspend` 还是不是 true；
   - 是的话，`kubectl get workloads -n ai-dev` 找对应 Workload；
   - `kubectl describe workload <name>` 看 conditions，里面有 Kueue 的 admission 决策理由；
2. Workload pending？
   - 看 ClusterQueue 的当前使用量是不是接近配额；
   - 看 LocalQueue 有没有绑对 ClusterQueue；
   - 看有没有其他 workload 在 admitted 但还没结束；
3. 资源够但还是不 admit？
   - 看 label 和 flavor 是不是对齐（最常见）；
   - 看 queueingStrategy 是不是 StrictFIFO 被前面的阻塞了。

## 和 Volcano 的对比

对比维度：

| 维度 | Kueue | Volcano |
|---|---|---|
| 成熟度 | v1beta2，SIG 项目 | 已稳定多年 |
| API 贴近 K8s | 非常贴近（用原生 Job/PriorityClass） | 自有 CRD |
| gang scheduling | 通过 all-or-nothing admission 实现 | 原生 PodGroup |
| 公平共享 | 有（beta→stable 阶段） | 有 |
| 和 kube-scheduler 的关系 | 不替换，只做 admission gate | 替换 |
| 生态集成（RayJob/Kubeflow） | 原生 integration | 需要 Volcano 模式 |
| MultiKueue 跨集群 | 有 | 无 |

选型建议：

- 如果你是新项目，强烈建议 Kueue，和 Kubernetes 的后向兼容性最好；
- 如果你已经在用 Volcano 且稳定，没必要迁；
- 大数据（Spark/Flink）重度用户 Volcano 的 Spark 集成成熟度稍高；
- AI 训练（PyTorch/Ray/Jax）Kueue 的支持度更完整。

## 几个踩过的坑

### 坑 1：忘了 suspend: true

最常见的坑。提交 Job 没设 suspend，Kueue 根本不接管，kube-scheduler 直接跑了。后果：完全绕过 Kueue，quota 形同虚设。

解决：写一个 webhook / Kyverno Policy，禁止 `ai-*` namespace 下没有 `kueue.x-k8s.io/queue-name` label 的 Job 被创建，或者强制设置 `suspend: true`。

### 坑 2：ResourceFlavor 和 node 不匹配

Flavor 配了 `nodeLabels: nvidia.com/gpu.product: A100`，但你的节点其实是 `A100-SXM4`。Kueue admit 成功、Pod pending 不动。

解决：`kubectl get nodes --show-labels | grep gpu`，跟 Flavor 的 label 完全对齐。

### 坑 3：borrowingLimit 不设

没设 borrowingLimit 的 ClusterQueue 不能借 cohort 里别的队列资源。很多人以为 cohort 自动打通，实际上得显式开。

### 坑 4：Kueue 本身的资源

Kueue 的 controller 在处理几千个 Workload 时 CPU / 内存需求会显著上升。生产上我们给 kueue-controller-manager 配 2 CPU / 4 GiB，少于这个规模稳定性会下降。

### 坑 5：Workload 堆积无人清理

默认 Kueue 不会自动清理 completed Workload，几千个 job 跑完后 etcd 里堆满 Workload 对象。开启 `spec.objectRetentionPolicies`（v1beta2 新字段），设置 completed workload 的 TTL。

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: Configuration
spec:
  objectRetentionPolicies:
    workloads:
      afterFinished: 24h
```

## 总结式的几条原则

- ResourceFlavor 先设计好，和 node label 严格对齐；
- ClusterQueue 按团队划分，用 cohort 做借用；
- LocalQueue 是业务的入口，每个 namespace 一个；
- Job 提交必须 suspend: true + queue-name label；
- 抢占优先用 LowerPriority，避免互相踢；
- Fair Sharing 推荐开启；
- 监控 admission wait time 和 cluster queue usage；
- 对 AI 训练，强制 checkpoint 机制，才能安全被抢占；
- 几千 Workload 以上规模时给 Kueue 足够资源 + retention policy；
- 多集群场景谨慎上 MultiKueue，成熟度还在爬坡。

Kueue 的 API 比 Volcano 那种"在 K8s 上再造一套 CRD"要干净很多，用下来踩的坑基本都是配置层面的，很少碰到它本身的 bug。把 AI 训练塞进 K8s 这件事，现在算是有了个还算顺手的答案。
