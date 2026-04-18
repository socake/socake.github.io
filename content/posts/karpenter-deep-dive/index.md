---
title: "Karpenter 深度解析：下一代 K8s 节点自动扩缩"
date: 2025-06-11T11:33:00+08:00
draft: false
tags: ["Karpenter", "Kubernetes", "AWS", "弹性伸缩", "成本优化"]
categories: ["Kubernetes"]
description: "深入解析 Karpenter 的设计原理、NodePool 配置、Disruption 机制、多 NodePool 策略与生产踩坑记录。"
summary: "从 Cluster Autoscaler 迁移到 Karpenter 之后，集群扩容速度和节点利用率都有明显提升。本文详细拆解 Karpenter 的核心机制、关键配置项，以及在多套生产集群运行中踩过的坑。"
toc: true
math: false
diagram: false
series: ["K8s 完全指南"]
keywords: ["Karpenter", "NodePool", "EC2NodeClass", "Spot 中断", "Consolidation", "Disruption Budget"]
params:
  reading_time: true
---

我们在 2024 年中将几套 EKS 集群从 Cluster Autoscaler 迁移到 Karpenter，迁移后节点平均扩容时间从 3-5 分钟降到 45-90 秒，节点利用率从约 45% 提升到 65%。本文记录 Karpenter 的核心机制和我们在生产中积累的配置经验。

## Karpenter vs Cluster Autoscaler

理解两者的设计哲学差异，才能用好 Karpenter。

**Cluster Autoscaler（CA）的工作方式：**

CA 依赖 Auto Scaling Group（ASG），ASG 预先定义了固定的实例类型。当有 Pending Pod 时，CA 模拟调度，找到能容纳 Pod 的 ASG，将其 desired count +1，等待 ASG 起新节点。

问题在于：
1. ASG 起节点用的是 EC2 launch template，从 scale-out 到节点 Ready 通常需要 3-5 分钟（EC2 启动 + 系统初始化 + kubelet 注册）
2. 固定实例类型，无法根据 Pod 需求自动选最合适的实例
3. 缩容时 CA 很保守，默认 10 分钟内没有变化才考虑缩容

**Karpenter 的工作方式：**

Karpenter 直接 watch Pending Pod，实时计算这批 Pod 需要什么样的实例（CPU/内存/GPU），从允许的实例类型列表中选最合适的，直接调 EC2 RunInstances API。节点注册到集群后，Karpenter 立即为等待的 Pod 做调度绑定，整个流程比 CA 快一个数量级。

Karpenter 还实现了 **Consolidation**：周期性检查集群中利用率低的节点，如果可以通过驱逐 + 重调度将 Pod 合并到更少的节点，就主动执行，释放多余节点。这是 CA 不具备的能力。

## NodePool + EC2NodeClass 配置详解

Karpenter v1 的核心 API 是 `NodePool` 和 `EC2NodeClass`。两者的分工：

- **NodePool**：定义工作负载的调度约束（实例类型要求、容量类型、Pod 亲和性、资源上限）
- **EC2NodeClass**：定义 AWS 层面的配置（AMI、子网、安全组、IAM Role、用户数据）

```yaml
apiVersion: karpenter.k8s.aws/v1
kind: EC2NodeClass
metadata:
  name: default
spec:
  # AMI 选择：用 alias 跟随 EKS 版本，不要写死 AMI ID
  amiSelectorTerms:
    - alias: eks-node@latest

  # 子网：打了 karpenter.sh/discovery 标签的子网会被自动发现
  subnetSelectorTerms:
    - tags:
        karpenter.sh/discovery: "my-cluster"

  # 安全组：同样通过 tag 发现
  securityGroupSelectorTerms:
    - tags:
        karpenter.sh/discovery: "my-cluster"

  # 节点 IAM Role（不是 instance profile，是 role 名字）
  role: "KarpenterNodeRole-my-cluster"

  # 用户数据：在 kubelet 启动前执行的初始化脚本
  userData: |
    #!/bin/bash
    # 设置 kubelet 参数
    cat >> /etc/kubernetes/kubelet/kubelet-config.json <<EOF
    {
      "maxPods": 110
    }
    EOF

  # EBS 配置
  blockDeviceMappings:
    - deviceName: /dev/xvda
      ebs:
        volumeSize: 50Gi
        volumeType: gp3
        iops: 3000
        throughput: 125
        encrypted: true
        deleteOnTermination: true
```

```yaml
apiVersion: karpenter.sh/v1
kind: NodePool
metadata:
  name: general
spec:
  template:
    metadata:
      labels:
        workload-type: general
      annotations:
        # 可以加任意 annotation，会传到节点
        team: platform
    spec:
      nodeClassRef:
        group: karpenter.k8s.aws
        kind: EC2NodeClass
        name: default

      requirements:
        # 容量类型：优先 Spot，不够时用 On-Demand
        - key: karpenter.sh/capacity-type
          operator: In
          values: ["spot", "on-demand"]

        # 实例类别：c=计算优化，m=通用，r=内存优化
        - key: karpenter.k8s.aws/instance-category
          operator: In
          values: ["c", "m", "r"]

        # 只用第5代及以上（第4代性价比更好，但某些区域可用性差）
        - key: karpenter.k8s.aws/instance-generation
          operator: Gt
          values: ["4"]

        # 架构：只用 amd64，不混 arm（除非应用已验证）
        - key: kubernetes.io/arch
          operator: In
          values: ["amd64"]

        # 可用区：覆盖所有 AZ 保证高可用
        - key: topology.kubernetes.io/zone
          operator: In
          values: ["us-west-2a", "us-west-2b", "us-west-2c"]

      # 节点自动过期，强制轮换（更新 AMI、应用安全补丁）
      expireAfter: 720h  # 30 天

  disruption:
    # 主动合并策略
    consolidationPolicy: WhenEmptyOrUnderutilized
    # 节点利用率低于多久后考虑合并（不是 empty 的情况）
    consolidateAfter: 5m
    # 预算：同时最多驱逐多少节点（百分比或绝对数）
    budgets:
      - nodes: "10%"

  # 整个 NodePool 的资源上限（防止异常扩容）
  limits:
    cpu: "400"
    memory: 800Gi
```

## Disruption 机制详解

Disruption 是 Karpenter 最复杂也最重要的功能之一，包含三种场景：

1. **Expiration**：节点超过 `expireAfter` 时间，主动驱逐并替换（相当于滚动更新节点）
2. **Consolidation**：合并低利用率节点（`WhenEmpty` 只合并空节点，`WhenEmptyOrUnderutilized` 更激进）
3. **Drift**：节点配置与 NodePool/EC2NodeClass 规格不符时自动替换（如 AMI 更新后）

### Disruption Budget

如果不配置 budget，Karpenter 可能同时驱逐大量节点，导致业务中断。

```yaml
disruption:
  budgets:
    # 全天默认：最多同时替换 10% 的节点
    - nodes: "10%"

    # 业务高峰期（北京时间 09:00-23:00）：最多替换 5%
    - nodes: "5%"
      schedule: "0 1 * * *"   # UTC 01:00 = 北京 09:00
      duration: 14h

    # 深夜维护窗口（北京时间 02:00-04:00）：最多替换 20%，加快节点轮换
    - nodes: "20%"
      schedule: "0 18 * * *"  # UTC 18:00 = 北京 02:00
      duration: 2h
```

schedule 用的是 cron 格式，UTC 时区。duration 是窗口持续时长。多个 budget 同时匹配时，取最保守的（最小值）。

### Pod Disruption Budget 的配合

Karpenter 在驱逐 Pod 前会检查 PDB。如果 PDB 说不允许驱逐，Karpenter 会等待或跳过。所以 **关键服务必须配置 PDB**：

```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: my-service-pdb
spec:
  selector:
    matchLabels:
      app: my-service
  # 至少保持 2 个 Pod 可用（绝对数比百分比更可预测）
  minAvailable: 2
  # 或者用 maxUnavailable
  # maxUnavailable: 1
```

注意：PDB 的 `minAvailable` 要根据服务的实际副本数设置。如果服务只有 2 个副本，`minAvailable: 2` 会导致 Karpenter 永远无法驱逐（没有 Pod 可以停），节点轮换卡住。

## 多 NodePool 策略

不同工作负载对节点有不同需求，用多个 NodePool 隔离：

```yaml
# NodePool 1: GPU 工作负载
---
apiVersion: karpenter.sh/v1
kind: NodePool
metadata:
  name: gpu
spec:
  template:
    metadata:
      labels:
        workload-type: gpu
    spec:
      nodeClassRef:
        group: karpenter.k8s.aws
        kind: EC2NodeClass
        name: gpu-class
      requirements:
        - key: karpenter.k8s.aws/instance-family
          operator: In
          values: ["g5", "g4dn"]
        - key: karpenter.sh/capacity-type
          operator: In
          values: ["on-demand"]  # GPU Spot 可用性差，不混用
      taints:
        - key: nvidia.com/gpu
          effect: NoSchedule
  limits:
    cpu: "100"

# NodePool 2: 批处理作业，允许 Spot，可被中断
---
apiVersion: karpenter.sh/v1
kind: NodePool
metadata:
  name: batch
spec:
  template:
    metadata:
      labels:
        workload-type: batch
    spec:
      nodeClassRef:
        group: karpenter.k8s.aws
        kind: EC2NodeClass
        name: default
      requirements:
        - key: karpenter.sh/capacity-type
          operator: In
          values: ["spot"]  # 只用 Spot
        - key: karpenter.k8s.aws/instance-category
          operator: In
          values: ["c", "m"]
      taints:
        - key: workload-type
          value: batch
          effect: NoSchedule
  disruption:
    consolidationPolicy: WhenEmpty  # 批处理节点只在空时合并
    consolidateAfter: 30s
```

业务 Pod 通过 `nodeSelector` + `tolerations` 选择对应 NodePool：

```yaml
# 批处理 Job 配置
spec:
  template:
    spec:
      nodeSelector:
        workload-type: batch
      tolerations:
        - key: workload-type
          value: batch
          effect: NoSchedule
```

## 生产踩坑记录

### 坑1：instanceCategory 导致 Spot 可用性差

初期把 `instance-category` 设成了 `["c", "m", "r", "t"]`，`t` 系列 Spot 可用性极差，而且 t 系列有 CPU 积分限制，突发流量时性能不稳定。后来把 `t` 从列表里移除，Spot 中断频率明显下降。

另外，限制了 `instance-generation > 4`（即只用第5代+），过老的实例类型网络性能差，而且 Spot 竞价通常更贵（因为存量减少）。

### 坑2：nodeSelector 与 NodePool requirements 不匹配

有一次上线了一批 Pod，带着 `nodeSelector: {"node-type": "high-memory"}`，但 NodePool 的 requirements 里没有对应的 label。结果 Karpenter 起了新节点，但节点没有 `node-type: high-memory` 这个 label，Pod 还是 Pending。

排查：

```bash
# 查看 Karpenter 的决策日志
kubectl logs -n karpenter -l app.kubernetes.io/name=karpenter -f | grep -E "(launched|failed|Pending)"

# 查看 Pod 无法调度的原因
kubectl describe pod <pod-name> | grep -A 10 "Events:"

# 查看 Karpenter 认为这个 Pod 应该去哪
kubectl get nodeclaim -o wide
```

修复方法：在 `EC2NodeClass` 的 `userData` 或 NodePool 的 `template.metadata.labels` 里加上对应 label，或者修改 Pod 的 `nodeSelector` 用 Karpenter 会自动打的 label（如 `karpenter.k8s.aws/instance-category`）。

### 坑3：Consolidation 在业务高峰时驱逐 Pod

没有配 Disruption Budget 时，Karpenter 在业务高峰期把利用率低的节点合并，驱逐了正在处理请求的 Pod，导致短暂的 5xx。

教训：
1. 关键服务必须配 PDB
2. NodePool 的 `disruption.budgets` 要配高峰期限速
3. Pod 的 `terminationGracePeriodSeconds` 要足够长（比请求超时时间长），让进行中的请求正常完成

### 坑4：expireAfter 导致节点频繁轮换

把 `expireAfter` 设成了 `168h`（7天），结果节点轮换太频繁，Consolidation 刚合并好节点，没多久又因为 expiration 触发轮换，浪费资源。改成 `720h`（30天）后节点利用率更稳定。

## 监控 Karpenter 行为

Karpenter 暴露了详细的 Prometheus metrics：

```yaml
# 关键监控指标
# 当前集群中 Karpenter 管理的节点数
karpenter_nodes_total

# NodeClaim 的状态分布（launched/registered/initialized）
karpenter_nodeclaims_total

# Disruption 操作次数和原因
karpenter_disruption_actions_performed_total

# Pod 等待节点的时间（扩容延迟）
karpenter_pods_startup_duration_seconds

# 节点利用率
karpenter_nodes_allocatable
karpenter_nodes_total_pod_requests
```

推荐的 Grafana 告警：

```yaml
# Pending Pod 超过 5 分钟（可能是 Karpenter 无法满足请求）
- alert: KarpenterPendingPodsTooLong
  expr: |
    count(kube_pod_status_phase{phase="Pending"} == 1) > 0
  for: 5m

# Karpenter 错误率过高
- alert: KarpenterLaunchErrors
  expr: |
    rate(karpenter_nodeclaims_termination_total{reason="disrupted"}[5m]) > 0.1
  for: 2m
```

## 小结

Karpenter 比 Cluster Autoscaler 强的地方其实就两点：按需选实例、主动 Consolidation——这两点 CA 做不到。配置上，NodePool 的 requirements 别锁太死，实例类型选择空间要够宽；Disruption Budget 和 PDB 务必配好，不然 Consolidation 一跑业务就抖。多 NodePool 隔离是管理不同工作负载特性的关键抓手。
