---
title: "Volcano 批调度实战：AI 训练集群的 Gang Scheduling、队列与抢占"
date: 2026-03-25T15:30:00+08:00
draft: false
tags: ["Volcano", "Kubernetes", "调度器", "GPU", "批处理"]
categories: ["云原生"]
description: "K8s 默认调度器对 AI 训练极不友好。Volcano 把 HPC 调度理念搬进 K8s：Gang Scheduling、Queue、Fairshare、Preemption、拓扑亲和。这篇讲清楚它在 AI 训练集群的落地。"
summary: "K8s 默认调度器对 AI 训练极不友好。Volcano 把 HPC 调度理念搬进 K8s：Gang Scheduling、Queue、Fairshare、Preemption、拓扑亲和。这篇讲清楚它在 AI 训练集群的落地。"
toc: true
math: false
diagram: false
keywords: ["Volcano", "Gang Scheduling", "Kubernetes Scheduler", "GPU Scheduling", "Batch"]
params:
  reading_time: true
---

## K8s 默认调度器为什么不够

K8s 默认调度器（kube-scheduler）的设计目标是"在线服务调度"：每个 Pod 独立决策、尽快启动、倾向于均衡分布。这套模型在 Web 后端、微服务、CI Job 上都很合适。

但**AI 训练作业**有三个默认调度器完全不擅长的特点：

1. **All-or-Nothing**：一个分布式训练作业需要 N 个 Pod 同时就位，少一个都不行。默认调度器一个一个 Pod 调度，极易出现"4 个 Worker 调上去了，第 5 个资源不够卡住，前 4 个占着资源干等"的局面
2. **资源拓扑敏感**：多卡训练对 GPU 在同一节点、跨节点有 NVLink / RDMA 等拓扑要求，默认调度器不感知
3. **队列和配额**：多团队共享 GPU 集群需要队列、份额、抢占这些 HPC 调度器的标配能力，默认调度器没有

这三件事任何一个都能让你的 GPU 集群利用率从 80% 掉到 40%。Volcano 是为了解决这些问题诞生的 K8s 原生批调度器。

## 一、Volcano 做了什么

Volcano 本质是一个**替代/补充**kube-scheduler 的组件。它可以：

- 和默认调度器共存，只接管有特定注解的 Pod（批作业）
- 提供 **Gang Scheduling**（所有 Pod 一起调度或都不调度）
- 提供 **Queue** 抽象（队列 + 配额 + 优先级）
- 提供 **Fair Share / Proportion / DRF** 等调度策略
- 提供 **Preemption**（高优作业抢占低优作业）
- 提供 **Task Topology**（任务间亲和/反亲和）
- 集成 **Volcano Job**（一种新 CRD）统一描述批作业

Volcano 不是一个训练框架。它只做调度，训练框架（PyTorch DDP、DeepSpeed、MPI、Horovod、TensorFlow PS-Worker 等）原封不动继续用。

## 二、架构

```
 ┌──────────────────────────────────────────────────┐
 │                  Volcano                         │
 │                                                  │
 │  ┌──────────────┐   ┌──────────────────────┐     │
 │  │ Volcano      │   │  Volcano Webhook     │     │
 │  │ Controller   │   │  (准入校验)           │     │
 │  │ Manager      │   └──────────────────────┘     │
 │  └──────┬───────┘                                │
 │         │ 生成 PodGroup + Pod                    │
 │         ▼                                        │
 │  ┌──────────────────────────────────────────┐    │
 │  │          Volcano Scheduler               │    │
 │  │  ┌──────────────────────────────────┐    │    │
 │  │  │  Session (周期性)                │    │    │
 │  │  │  ┌────────────────────────────┐  │    │    │
 │  │  │  │  Actions                   │  │    │    │
 │  │  │  │  - enqueue                 │  │    │    │
 │  │  │  │  - allocate                │  │    │    │
 │  │  │  │  - preempt                 │  │    │    │
 │  │  │  │  - backfill                │  │    │    │
 │  │  │  │  - reclaim                 │  │    │    │
 │  │  │  └────────────────────────────┘  │    │    │
 │  │  │  ┌────────────────────────────┐  │    │    │
 │  │  │  │  Plugins                   │  │    │    │
 │  │  │  │  - gang                    │  │    │    │
 │  │  │  │  - priority                │  │    │    │
 │  │  │  │  - drf                     │  │    │    │
 │  │  │  │  - proportion              │  │    │    │
 │  │  │  │  - predicates              │  │    │    │
 │  │  │  │  - nodeorder               │  │    │    │
 │  │  │  │  - binpack                 │  │    │    │
 │  │  │  └────────────────────────────┘  │    │    │
 │  │  └──────────────────────────────────┘    │    │
 │  └──────────────────────────────────────────┘    │
 └──────────────────────────────────────────────────┘
```

### 核心概念

- **PodGroup**：一组 Pod 的集合，Gang Scheduling 的基本单元
- **Queue**：队列，定义配额、权重、优先级
- **Job (vcjob)**：Volcano 自己的作业 CRD，描述一个批作业（多个 Task）
- **Session**：调度器的一个调度周期（默认 1 秒），在 Session 里执行若干 Action
- **Action**：一个调度动作（enqueue/allocate/preempt 等）
- **Plugin**：为 Action 提供决策的插件（gang/drf/binpack 等）

## 三、安装

Volcano 的安装方式：

```bash
kubectl apply -f https://raw.githubusercontent.com/volcano-sh/volcano/master/installer/volcano-development.yaml
```

生产环境推荐 Helm：

```bash
helm repo add volcano-sh https://volcano-sh.github.io/helm-charts
helm install volcano volcano-sh/volcano -n volcano-system --create-namespace
```

安装后会出现这几个 Pod：

- `volcano-admission`：webhook，做 PodGroup 准入校验
- `volcano-controllers`：Job/PodGroup 控制器
- `volcano-scheduler`：调度器本体

以及几个 CRD：`Job (vcjob)`、`Queue`、`PodGroup`、`CommandJob`。

## 四、PodGroup 和 Gang Scheduling

### 4.1 什么是 Gang Scheduling

训练作业需要 N 个 Pod **同时**就位才能开始工作。默认调度器的 "一个一个调度" 策略在资源紧张时会陷入死锁：

```
作业 A 需要 4 个 Worker，集群剩 3 个 GPU 空位
作业 B 需要 3 个 Worker，集群剩 3 个 GPU 空位

默认调度器：
- A 调 3 个 Worker 上去（占了 3 个 GPU）
- B 调 0 个（没资源了）
- A 的第 4 个 Worker 永远等不到
- B 被 A 的 3 个 Worker 卡住永远起不来
- 集群死锁，手动 kill 才能恢复
```

Gang Scheduling 的做法是**all-or-nothing**：

```
A 想要 4 个 → 但集群只凑出 3 个 → A 一个都不调
B 想要 3 个 → 集群正好 3 个 → B 全部调度成功
A 等 B 结束后空出资源再调
```

### 4.2 PodGroup 定义

```yaml
apiVersion: scheduling.volcano.sh/v1beta1
kind: PodGroup
metadata:
  name: training-job-a
  namespace: ai-train
spec:
  minMember: 4
  minResources:
    cpu: "32"
    memory: "128Gi"
    nvidia.com/gpu: "4"
  queue: ai-team-1
  priorityClassName: normal
```

字段解释：

- `minMember`：至少需要多少个 Pod 就位才能开跑（Gang 的核心）
- `minResources`：PodGroup 需要的最小资源总量
- `queue`：归属哪个队列
- `priorityClassName`：优先级

### 4.3 让 Pod 关联到 PodGroup

Pod 通过 annotation 关联 PodGroup：

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: worker-0
  namespace: ai-train
  annotations:
    scheduling.k8s.io/group-name: training-job-a
spec:
  schedulerName: volcano
  containers:
    - name: pytorch
      image: pytorch:2.3.0-cuda12.1
      resources:
        limits:
          nvidia.com/gpu: 1
```

`schedulerName: volcano` 让这个 Pod 被 Volcano 调度器处理，而不是默认 kube-scheduler。

## 五、Volcano Job：推荐的作业描述方式

手写 PodGroup + Pod 很啰嗦。Volcano 提供了 `Job` CRD 统一描述：

```yaml
apiVersion: batch.volcano.sh/v1alpha1
kind: Job
metadata:
  name: pytorch-ddp-training
  namespace: ai-train
spec:
  minAvailable: 4
  schedulerName: volcano
  queue: ai-team-1
  priorityClassName: high
  plugins:
    env: []
    ssh: []
    svc: []
  tasks:
    - replicas: 4
      name: worker
      template:
        metadata:
          labels:
            role: worker
        spec:
          restartPolicy: OnFailure
          containers:
            - name: pytorch
              image: your-registry/pytorch:2.3-cu121
              command:
                - torchrun
                - --nnodes=4
                - --nproc_per_node=8
                - --rdzv_backend=c10d
                - --rdzv_endpoint=pytorch-ddp-training-worker-0.pytorch-ddp-training:29400
                - /workspace/train.py
              resources:
                limits:
                  nvidia.com/gpu: 8
                  cpu: "64"
                  memory: "512Gi"
              volumeMounts:
                - { name: data, mountPath: /data }
                - { name: shm, mountPath: /dev/shm }
          volumes:
            - name: data
              persistentVolumeClaim:
                claimName: training-data
            - name: shm
              emptyDir:
                medium: Memory
                sizeLimit: 64Gi
```

几个字段说明：

- `minAvailable: 4`：至少 4 个 Task Pod 就位才启动
- `plugins`：启用几个内置插件
  - `env`：自动注入 `VC_TASK_INDEX`、`VC_WORKER_NUM` 等环境变量
  - `ssh`：为所有 Pod 配置免密 SSH（MPI 作业必需）
  - `svc`：自动创建 Service 让 Pod 之间能解析
- `tasks`：一个作业可以有多种角色（master / worker / ps / chief），每种一段 spec

生产里 MPI 作业几乎都启 `ssh` 插件，PS-Worker 架构启 `svc` 插件。

## 六、Queue：多团队共享集群

### 6.1 队列定义

```yaml
apiVersion: scheduling.volcano.sh/v1beta1
kind: Queue
metadata:
  name: ai-team-1
spec:
  weight: 2
  reclaimable: true
  capability:
    cpu: "256"
    memory: "1024Gi"
    nvidia.com/gpu: "64"
  guarantee:
    resource:
      cpu: "64"
      memory: "256Gi"
      nvidia.com/gpu: "16"
```

字段：

- `weight`：用于 Proportion 插件计算队列份额，越大占比越多
- `reclaimable`：队列里的资源是否可被抢占回收
- `capability`：队列上限，即使集群有更多资源也不能超过
- `guarantee`：队列保证资源，至少能用这么多（哪怕被抢占也会先保证这个量）

### 6.2 队列层次

Volcano 1.8+ 支持层级队列：

```yaml
apiVersion: scheduling.volcano.sh/v1beta1
kind: Queue
metadata:
  name: ai-org
spec:
  weight: 10
  parent: root
---
apiVersion: scheduling.volcano.sh/v1beta1
kind: Queue
metadata:
  name: ai-team-1
spec:
  weight: 2
  parent: ai-org
---
apiVersion: scheduling.volcano.sh/v1beta1
kind: Queue
metadata:
  name: ai-team-2
spec:
  weight: 3
  parent: ai-org
```

典型场景：公司 AI 部门先拿一份总额度，下面再按组细分。

### 6.3 三种份额算法

Volcano 支持的调度策略插件：

| 插件 | 算法 | 适用场景 |
|---|---|---|
| `proportion` | 按 weight 比例分配队列份额 | 多团队公平分配 |
| `drf` | 主导资源公平（Dominant Resource Fairness） | 资源类型异构，GPU+CPU 混合 |
| `fairshare` | 经典 HPC fair share | 长时间公平性 |

在 scheduler config 里启用：

```yaml
actions: "enqueue, allocate, preempt, backfill"
tiers:
  - plugins:
      - name: priority
      - name: gang
      - name: conformance
  - plugins:
      - name: drf
      - name: predicates
      - name: proportion
      - name: nodeorder
      - name: binpack
```

## 七、Action 详解

### 7.1 enqueue

决定 PodGroup 是否允许进入"可调度"状态。入口前会检查队列配额、集群总资源等。核心作用是**防止集群被过多 PodGroup 淹没**。

### 7.2 allocate

真正把 Pod 绑定到 Node。按 priority + 份额算法排序，然后一个个 Pod 试图找 Node。

### 7.3 preempt

当高优作业调不上但集群已满时，尝试把低优作业的 Pod 驱逐腾出空间。可配置抢占策略（按优先级、按时间、按资源）。

### 7.4 backfill

空闲时隙填充：当一个大作业还在等资源时，可以让小作业先跑（前提是不影响大作业的排队等待）。经典 HPC 思路。

### 7.5 reclaim

跨队列资源回收：当高 weight 队列实际用量低于 guarantee 时，把借给其他队列的资源收回来。

## 八、拓扑感知调度

AI 训练对 GPU 拓扑很敏感。Volcano 通过几个机制支持：

### 8.1 NodeSelector / Affinity

最基础的手段，指定作业只能跑在特定节点池：

```yaml
template:
  spec:
    nodeSelector:
      node-role.kubernetes.io/ai-training: "true"
      gpu-type: h100
    affinity:
      nodeAffinity:
        requiredDuringSchedulingIgnoredDuringExecution:
          nodeSelectorTerms:
            - matchExpressions:
                - key: rdma-capable
                  operator: In
                  values: ["true"]
```

### 8.2 Task Topology 插件

Volcano 有 `task-topology` 插件让多任务间亲和/反亲和：

```yaml
spec:
  plugins:
    task-topology: ["--task-affinity=ps,worker"]
  tasks:
    - name: ps
      replicas: 2
    - name: worker
      replicas: 4
```

把 ps 和 worker 尽量调到同一 zone / rack，减少跨机房通信。

### 8.3 GPU 拓扑感知（device plugin 层）

Volcano 本身不直接处理 NVLink 拓扑，这是 NVIDIA device plugin + Volcano 协作的事情。NVIDIA device plugin 可以把节点内 GPU 的 NVLink 拓扑作为资源 label 暴露出来，Volcano 的 `binpack` 插件可以利用它把多个 Pod 的 GPU 尽量装到同一个 NVLink 域。

## 九、和 Kubeflow / TrainingOperator 的集成

业界的 AI 训练作业大多数通过 Kubeflow Training Operator（PyTorchJob、TFJob、MPIJob）提交。Volcano 能和它们无缝集成：

**方式一**：让 Training Operator 用 Volcano 调度

Training Operator 0.4+ 支持配置调度器：

```yaml
apiVersion: kubeflow.org/v1
kind: PyTorchJob
metadata:
  name: pytorch-demo
spec:
  runPolicy:
    schedulingPolicy:
      minAvailable: 4
      queue: ai-team-1
  pytorchReplicaSpecs:
    Master:
      replicas: 1
      template:
        spec:
          schedulerName: volcano
          containers:
            - ...
    Worker:
      replicas: 3
      template:
        spec:
          schedulerName: volcano
          containers:
            - ...
```

Training Operator 会自动为这个作业生成 PodGroup。

**方式二**：业务直接用 Volcano Job

如果你不需要 Training Operator 的角色管理（PS/Worker/Chief），直接用 Volcano Job 更轻量。

我的经验是：

- PyTorch DDP：用 Volcano Job + torchrun rdzv，最简单
- PS-Worker（少见了）：用 TFJob
- MPI（Horovod / DeepSpeed launcher 模式）：用 MPIJob
- 自研 launcher：Volcano Job

## 十、监控和运维

Volcano 暴露 Prometheus 指标在 `:8080/metrics`。关键指标：

| 指标 | 含义 |
|---|---|
| `volcano_job_retry_counts` | 作业重试次数，高说明有调度失败 |
| `volcano_pending_jobs` | 排队中作业数 |
| `volcano_queue_allocated_cpu/memory/gpu` | 队列已分配资源 |
| `volcano_queue_capacity_*` | 队列上限 |
| `volcano_queue_weight` | 权重 |
| `volcano_task_count_*` | 不同阶段 Task 数量 |
| `volcano_session_duration_seconds` | 调度周期耗时 |

告警规则示例：

- `volcano_session_duration > 5s`：调度器本身慢，集群规模太大或插件性能问题
- `volcano_pending_jobs > 0 持续 10min`：作业排队积压
- `queue_allocated / queue_capacity > 0.95 持续 30min`：队列即将满，考虑扩容
- `gang scheduling 失败率高`：`minMember` 和实际资源不匹配

## 十一、调度器配置文件

Volcano 调度器的行为由一个 ConfigMap 控制，默认名 `volcano-scheduler-configmap`：

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: volcano-scheduler-configmap
  namespace: volcano-system
data:
  volcano-scheduler.conf: |
    actions: "enqueue, allocate, preempt, backfill"
    tiers:
      - plugins:
          - name: priority
          - name: gang
            enablePreemptable: true
          - name: conformance
      - plugins:
          - name: overcommit
          - name: drf
            enablePreemptable: true
            enableHierarchy: true
          - name: predicates
          - name: proportion
          - name: nodeorder
          - name: binpack
            arguments:
              binpack.weight: 10
              binpack.cpu: 1
              binpack.memory: 1
              binpack.resources: "nvidia.com/gpu"
              binpack.resources.nvidia.com/gpu: 5
```

关键配置：

- `tiers`：插件分层，前面的 tier 先决策，后面的 tier 只能在前者允许的集合里继续筛
- `binpack.weight`：打分权重，越大越倾向紧凑调度（把 Pod 塞到已有 Pod 的节点）
- `binpack.resources` 指定重点打包的资源类型，AI 场景设 `nvidia.com/gpu` 让 GPU 尽量集中

调整 ConfigMap 后 scheduler Pod 会自动 reload。

## 十二、实战配置：一个完整训练集群

一个我实际部署过的场景：

**背景**：4 个 AI 团队共享 32 节点 × 8×H100 集群，需要做到：

- 训练作业独占 GPU，不和推理混
- 团队间资源有保证也有弹性（空闲时能借）
- 紧急作业（线上故障的紧急微调）可以抢占

**实现**：

```yaml
# 三个层级队列
---
apiVersion: scheduling.volcano.sh/v1beta1
kind: Queue
metadata: { name: root }
spec:
  weight: 1

---
apiVersion: scheduling.volcano.sh/v1beta1
kind: Queue
metadata: { name: ai-org }
spec:
  parent: root
  weight: 100
  capability:
    nvidia.com/gpu: "256"

---
apiVersion: scheduling.volcano.sh/v1beta1
kind: Queue
metadata: { name: team-nlp }
spec:
  parent: ai-org
  weight: 30
  reclaimable: true
  guarantee:
    resource: { nvidia.com/gpu: "64" }

---
apiVersion: scheduling.volcano.sh/v1beta1
kind: Queue
metadata: { name: team-cv }
spec:
  parent: ai-org
  weight: 30
  reclaimable: true
  guarantee:
    resource: { nvidia.com/gpu: "64" }

---
apiVersion: scheduling.volcano.sh/v1beta1
kind: Queue
metadata: { name: team-research }
spec:
  parent: ai-org
  weight: 20
  reclaimable: true
  guarantee:
    resource: { nvidia.com/gpu: "32" }

---
apiVersion: scheduling.volcano.sh/v1beta1
kind: Queue
metadata: { name: emergency }
spec:
  parent: ai-org
  weight: 100
  reclaimable: false   # 紧急队列不被抢占
  guarantee:
    resource: { nvidia.com/gpu: "16" }

---
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: emergency
value: 100000
preemptionPolicy: PreemptLowerPriority
```

- 4 个业务队列，紧急队列有最高权重和不可回收保证
- `reclaimable: true` 让队列闲时借出去，忙时收回来
- 紧急作业用 `emergency` PriorityClass 走最高优先级，通过 preempt 抢占其他作业

提交紧急作业：

```yaml
apiVersion: batch.volcano.sh/v1alpha1
kind: Job
metadata:
  name: emergency-hotfix-training
spec:
  queue: emergency
  priorityClassName: emergency
  minAvailable: 2
  schedulerName: volcano
  tasks:
    - replicas: 2
      name: worker
      template:
        spec:
          containers:
            - name: torch
              image: ...
              resources:
                limits: { nvidia.com/gpu: 8 }
```

Volcano 会尝试腾出 2 个 8 卡节点，如果必要会驱逐其他可抢占作业。

## 十三、踩坑合集

### 坑 1：默认调度器和 Volcano 共存混乱

安装 Volcano 不会自动接管所有 Pod。如果你的 AI Pod 没写 `schedulerName: volcano`，还是会走默认调度器。要么全局默认改成 Volcano（不推荐），要么每个 AI 作业显式指定。

### 坑 2：PodGroup 和 Pod 的 namespace 必须一致

PodGroup 是 namespace 级别的。Pod 的 annotation 引用跨 namespace 的 PodGroup 会被忽略。

### 坑 3：minAvailable 设小会退化成普通调度

`minAvailable: 1` 等于没有 Gang Scheduling。必须和 `replicas` 匹配或者按业务真实的最小可用数设置。

### 坑 4：抢占策略对 StatefulSet 友好度差

StatefulSet Pod 被抢占后会从 `-0` 开始重建，对 AI 训练来说通常等于 checkpoint 恢复。要设计好 checkpoint 频率，否则一次抢占损失几小时训练进度。

### 坑 5：binpack 把小 Pod 挤到同一个节点导致训练作业起不来

典型场景：小 Pod（Notebook、CI）被 binpack 到一个节点的角落，剩下的 GPU 资源碎片化，大训练作业需要整节点时凑不出来。解法：给 Notebook 走单独队列，或者加 `anti-affinity`。

### 坑 6：Queue 删除但里面有作业

Queue 有 Pod 关联时不能直接删。先停掉里面的作业，再删队列。

### 坑 7：PriorityClass 一定要提前创建

Volcano Job 里引用的 PriorityClass 是 K8s 原生资源，不是 Volcano 管的。引用不存在的 PriorityClass 作业会被 webhook 拒绝。

### 坑 8：gang 调度失败后没明显提示

PodGroup 的 `minMember` 凑不齐时 Pod 会一直 Pending，kube 事件里不一定有明确原因。`kubectl describe podgroup <name>` 能看到 `NotEnoughResources` 之类的状态。做成 dashboard 面板监控这个状态避免无头案。

### 坑 9：Volcano 升级不平滑

Volcano 的 CRD 在 0.x → 1.x 之间有过 breaking change。生产升级前一定做完整演练，建议：

- 备份所有 Queue/PodGroup/Job 的 YAML
- 先升测试集群
- 滚动升级 scheduler/controller 组件
- 观察 1-2 周再升其他集群

### 坑 10：大规模集群 scheduler 慢

节点数 > 500 时单 session 耗时可能超过 1 秒，作业调度变慢。可以调整 `scheduler-period` 或者启用 NodeGroup 分片调度。

## 十四、Volcano vs 其他方案

| 方案 | 调度能力 | 学习曲线 | 生态 | AI 场景适配 |
|---|---|---|---|---|
| kube-scheduler | 基础 | 低 | 最广 | 差 |
| Volcano | 强（HPC 完整） | 中 | Kubeflow/CNCF | 优 |
| Yunikorn | 强（Spark 场景） | 中 | 大数据为主 | 良 |
| Kueue | 适中 | 中 | K8s SIG 官方 | 良 |
| Slurm | 最强（HPC 经典） | 高 | HPC | 优（非 K8s） |

**决策建议**：

- 纯 AI 训练、K8s 原生：Volcano
- 离线大数据 + AI 混合：Yunikorn 或 Volcano
- 想要 upstream 方案：Kueue（K8s SIG 在推）
- 传统 HPC 背景团队：Slurm + K8s 共存

Kueue 近两年发展很快，和 Volcano 的定位有一定重叠。我的观感是：Volcano 功能更全，Kueue 更简单。选哪个看团队背景。

## 十五、上线 checklist

```
[ ] Volcano 各组件 Pod Running 正常
[ ] Prometheus 接入，session_duration、pending_jobs 有监控
[ ] Queue 层级和业务团队对齐
[ ] guarantee 和 capability 计算过，避免超发
[ ] PriorityClass 预先创建
[ ] 默认 scheduler 不接管 AI 作业，通过 schedulerName 显式区分
[ ] binpack 配置优化 GPU 资源集中度
[ ] 抢占策略演练过，确认 checkpoint 机制完善
[ ] Volcano Job 的 YAML 模板进代码仓库作为业务标准
[ ] 文档给团队：怎么提交作业、怎么看队列状态、作业 Pending 怎么排查
```

## 十六、收尾

Volcano 的价值不是"一个功能酷炫的调度器"，而是**把 HPC 几十年沉淀的调度理念变成 K8s 原生的扩展点**。你不需要把集群退回到 Slurm，也不需要忍受默认调度器的尴尬。

落地要点：

1. 队列设计要和组织架构匹配，技术服务业务
2. Gang Scheduling 是核心价值，没有它上 Volcano 没意义
3. 抢占策略配合 checkpoint 才能真正发挥作用
4. 监控指标要进 Grafana，出问题才能快速定位

把这几件事做到位，GPU 集群利用率从 40% 提到 75% 完全可行。这是实实在在的成本节约。
