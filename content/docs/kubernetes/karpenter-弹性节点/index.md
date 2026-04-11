---
title: "Karpenter 弹性节点管理实战"
date: 2025-12-08T13:00:00+08:00
draft: false
tags: ["Karpenter", "Kubernetes", "AWS", "弹性伸缩", "成本优化"]
categories: ["Kubernetes"]
description: "Karpenter 在 AWS EKS 生产环境的落地实践：NodePool/NodeClass 配置、节点整合策略、多集群管理与踩坑记录"
summary: "Karpenter 替代 Cluster Autoscaler 的完整实践：NodePool 约束配置、EC2NodeClass 实例选型、consolidation 节点整合降本、Spot 实例容错，以及多套集群配置的组织方式。"
toc: true
math: false
diagram: false
keywords: ["karpenter", "kubernetes", "aws eks", "弹性伸缩", "节点整合", "成本优化"]
params:
  reading_time: true
---

## Karpenter vs Cluster Autoscaler

在迁移之前我们用了 Cluster Autoscaler 将近两年，它能解决基本问题，但在一些场景下力不从心。下面是对比：

| 能力维度 | Cluster Autoscaler | Karpenter |
|---|---|---|
| **扩容响应速度** | 通常 2-5 分钟（需等待 ASG 启动节点） | 通常 30-90 秒（直接调用 EC2 API） |
| **实例类型灵活度** | 依赖 ASG，每个 ASG 实例类型固定 | 单个 NodePool 可声明数十种实例类型，自动选最优 |
| **节点整合（缩容）** | 根据利用率缩容，但效果较差 | 主动整合：将多个低利用率节点上的 Pod 迁移并终止节点 |
| **Spot 实例支持** | 需要配置多个 ASG 或混合 ASG | 原生支持 `capacityType: spot`，自动处理中断 |
| **成本优化能力** | 被动（只缩不整合） | 主动（consolidation 持续优化节点规格和数量） |
| **配置复杂度** | 中等（需维护多个 ASG） | 中等（YAML 声明式，学习曲线主要在理解 disruption 策略） |
| **节点轮换** | 不支持 | `expireAfter` 自动轮换（配合 AMI 更新） |
| **亲和性/拓扑感知** | 依赖 ASG 可用区分布 | NodePool 中直接声明拓扑约束 |
| **多架构支持** | 需要多个 ASG | `requirements` 中混合 amd64/arm64 |
| **GPU 节点** | 需要独立 ASG | 通过 `nodeClassRef` 区分，同一套流程 |

**迁移后的实测结果：** 扩容时间从平均 3.5 分钟降到 70 秒以内，节点整合每月节省约 15-20% 的计算成本（主要来自消除碎片化的大量低利用率节点）。

---

## 核心概念

Karpenter 的对象模型只有三层，搞清楚这三层就能理解所有配置：

### NodePool

NodePool 是**约束池**，回答"能创建什么样的节点"这个问题。它定义：

- 允许的实例类型（通过 `requirements` 筛选）
- 允许的操作系统和架构
- 节点的最大资源上限（防止失控扩容）
- 节点中断和整合策略

一个集群可以有多个 NodePool，每个 NodePool 对应不同的工作负载类型（通用、GPU、高内存等）。调度器在决定用哪个 NodePool 时，会看 Pod 的 `nodeSelector` 和 `tolerations`。

### EC2NodeClass

EC2NodeClass 是 **AWS 专属配置**，回答"节点怎么初始化"这个问题：

- AMI 选择（通过 tag 或 ID）
- 放入哪个子网（通过 tag 选择）
- 绑定哪些安全组
- IAM 实例 Profile
- 根卷大小和类型
- userData 自定义初始化脚本

NodePool 通过 `nodeClassRef` 引用 EC2NodeClass，多个 NodePool 可以共用同一个 NodeClass。

### NodeClaim

NodeClaim 是 Karpenter 内部对象，每个 NodeClaim 对应一台即将或已经创建的 EC2 实例。通常不需要手动操作，但排查问题时需要看。NodeClaim 创建后，Karpenter 调用 EC2 API 启动实例，实例注册到集群后 NodeClaim 进入 `Launched` 状态。

---

## NodePool 配置实战

### 通用工作负载 NodePool

```yaml
apiVersion: karpenter.sh/v1
kind: NodePool
metadata:
  name: general
spec:
  template:
    metadata:
      labels:
        nodepool: general
      annotations:
        # 用于 kubectl get node 时识别来源
        karpenter.sh/nodepool: general
    spec:
      nodeClassRef:
        group: karpenter.k8s.aws
        kind: EC2NodeClass
        name: default

      # 实例筛选约束（AND 关系）
      requirements:
        # 实例大类：通用计算（排除存储优化、内存优化）
        - key: karpenter.k8s.aws/instance-category
          operator: In
          values: ["c", "m", "r"]

        # 实例大小：排除太小（nano/micro/small）和太大（32xlarge+）
        - key: karpenter.k8s.aws/instance-size
          operator: NotIn
          values: ["nano", "micro", "small", "metal"]

        # 架构：同时支持 amd64 和 arm64（Graviton 更便宜）
        - key: kubernetes.io/arch
          operator: In
          values: ["amd64", "arm64"]

        # 容量类型：优先 Spot，允许 on-demand 兜底
        - key: karpenter.sh/capacity-type
          operator: In
          values: ["spot", "on-demand"]

        # 可用区：只在特定 AZ 启动（避免跨 AZ 数据传输费用）
        - key: topology.kubernetes.io/zone
          operator: In
          values: ["us-west-2a", "us-west-2b", "us-west-2c"]

        # 代数限制：只用第 4 代及以上（性价比更好）
        - key: karpenter.k8s.aws/instance-generation
          operator: Gt
          values: ["3"]

      # 节点上的 Kubelet 配置
      kubelet:
        maxPods: 110
        systemReserved:
          cpu: "100m"
          memory: "200Mi"
        kubeReserved:
          cpu: "100m"
          memory: "200Mi"
          ephemeral-storage: "1Gi"

  # 最大资源上限（防止 bug 导致无限扩容）
  limits:
    cpu: "1000"
    memory: 4000Gi

  # 节点中断与整合策略
  disruption:
    # WhenEmptyOrUnderutilized: 节点空闲或利用率低时整合
    # WhenEmpty: 只整合空节点（更保守）
    consolidationPolicy: WhenEmptyOrUnderutilized

    # 节点利用率低于阈值多久后触发整合（避免频繁抖动）
    consolidateAfter: 5m

    # 节点最长寿命（到期后 Karpenter 会优雅驱逐并替换）
    # 配合 AMI 自动更新使用，确保节点不会运行太老的镜像
    expireAfter: 720h   # 30 天

  # NodePool 权重（多个 NodePool 时，权重高的优先调度）
  weight: 50
```

### GPU 专用 NodePool

```yaml
apiVersion: karpenter.sh/v1
kind: NodePool
metadata:
  name: gpu-workload
spec:
  template:
    metadata:
      labels:
        nodepool: gpu
        accelerator: nvidia
    spec:
      nodeClassRef:
        group: karpenter.k8s.aws
        kind: EC2NodeClass
        name: gpu  # 引用专门的 GPU NodeClass

      requirements:
        # 只用 GPU 实例系列
        - key: karpenter.k8s.aws/instance-category
          operator: In
          values: ["g", "p"]

        - key: karpenter.k8s.aws/instance-size
          operator: In
          values: ["4xlarge", "8xlarge", "12xlarge", "16xlarge"]

        # GPU 节点通常只用 on-demand（Spot 中断对 GPU 训练任务影响太大）
        - key: karpenter.sh/capacity-type
          operator: In
          values: ["on-demand"]

        - key: kubernetes.io/arch
          operator: In
          values: ["amd64"]

      # GPU 节点打 taint，防止普通 Pod 漂移过来
      taints:
        - key: nvidia.com/gpu
          value: "true"
          effect: NoSchedule

      kubelet:
        maxPods: 30

  limits:
    cpu: "200"
    memory: 2000Gi

  disruption:
    # GPU 节点只在空闲时整合（不打断正在跑的任务）
    consolidationPolicy: WhenEmpty
    expireAfter: 2160h  # 90 天

  weight: 100  # GPU NodePool 权重更高，让 GPU Pod 优先匹配
```

---

## EC2NodeClass 配置

### 默认通用 NodeClass

```yaml
apiVersion: karpenter.k8s.aws/v1
kind: EC2NodeClass
metadata:
  name: default
spec:
  # AMI 选择：通过 tag 匹配（推荐方式，比写死 ID 更灵活）
  amiSelectorTerms:
    - alias: al2023@latest   # Amazon Linux 2023 最新版（EKS 优化 AMI）
  # 如果用自定义 AMI（例如预装了监控 agent）：
  # amiSelectorTerms:
  #   - tags:
  #       custom-ami: "eks-1.30-node-v2"
  #       Environment: production

  # AMI 族（和 alias 配合使用）
  amiFamily: AL2023

  # 子网选择：通过 tag 选（不要写死子网 ID，方便多 AZ 扩展）
  subnetSelectorTerms:
    - tags:
        karpenter.sh/discovery: my-cluster-name
        SubnetType: private

  # 安全组选择
  securityGroupSelectorTerms:
    - tags:
        karpenter.sh/discovery: my-cluster-name
    - tags:
        Name: eks-node-sg

  # IAM 实例 Profile（节点需要的权限：ECR 拉镜像、SSM 等）
  role: "KarpenterNodeRole-my-cluster"

  # 根卷配置
  blockDeviceMappings:
    - deviceName: /dev/xvda
      ebs:
        volumeSize: 50Gi
        volumeType: gp3
        iops: 3000
        throughput: 125
        encrypted: true
        deleteOnTermination: true

  # 本地 NVMe SSD 配置（针对存储密集型工作负载）
  # instanceStorePolicy: RAID0

  # 标签（会自动打到 EC2 实例上，方便计费分析）
  tags:
    Environment: production
    ManagedBy: karpenter
    Cluster: my-cluster

  # 自定义 userData（节点启动时执行）
  # 注意：AL2023 用 MIME multi-part，AL2 用 NodeGroup bootstrap
  userData: |
    MIME-Version: 1.0
    Content-Type: multipart/mixed; boundary="==boundary=="

    --==boundary==
    Content-Type: text/x-shellscript; charset="us-ascii"

    #!/bin/bash
    # 安装自定义监控 agent
    /opt/aws/bin/cfn-init || true

    # 调整内核参数
    sysctl -w net.core.somaxconn=65535
    sysctl -w net.ipv4.tcp_max_syn_backlog=65535
    echo 'net.core.somaxconn=65535' >> /etc/sysctl.d/99-custom.conf

    --==boundary==--
```

### GPU NodeClass

```yaml
apiVersion: karpenter.k8s.aws/v1
kind: EC2NodeClass
metadata:
  name: gpu
spec:
  amiSelectorTerms:
    - alias: al2023@latest

  amiFamily: AL2023

  subnetSelectorTerms:
    - tags:
        karpenter.sh/discovery: my-cluster-name
        SubnetType: private

  securityGroupSelectorTerms:
    - tags:
        karpenter.sh/discovery: my-cluster-name

  role: "KarpenterNodeRole-my-cluster"

  blockDeviceMappings:
    - deviceName: /dev/xvda
      ebs:
        volumeSize: 200Gi    # GPU 节点镜像和数据更大
        volumeType: gp3
        iops: 6000
        throughput: 250
        encrypted: true
        deleteOnTermination: true

  tags:
    Environment: production
    WorkloadType: gpu
    ManagedBy: karpenter

  userData: |
    MIME-Version: 1.0
    Content-Type: multipart/mixed; boundary="==boundary=="

    --==boundary==
    Content-Type: text/x-shellscript; charset="us-ascii"

    #!/bin/bash
    # 安装 NVIDIA 驱动和容器运行时（如果 AMI 未预装）
    # nvidia-ctk runtime configure --runtime=containerd
    # systemctl restart containerd

    --==boundary==--
```

---

## Spot 实例实战

### 中断队列配置

Karpenter 通过 SQS 队列接收 Spot 中断通知，提前 2 分钟开始驱逐 Pod。需要在部署 Karpenter 时配置：

```bash
# 创建 SQS 队列（Karpenter 安装时通常由 CloudFormation/Terraform 自动创建）
aws sqs create-queue \
    --queue-name karpenter-my-cluster \
    --attributes '{
        "MessageRetentionPeriod": "300"
    }'

# 在 Karpenter Controller 的 configmap 中配置中断队列
kubectl edit configmap karpenter -n kube-system
# 或者通过 Helm values:
# settings:
#   interruptionQueueName: karpenter-my-cluster
```

### Pod 容错配置

对于运行在 Spot 上的工作负载，需要做好容错：

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: web-api
spec:
  replicas: 6
  selector:
    matchLabels:
      app: web-api
  template:
    metadata:
      labels:
        app: web-api
    spec:
      # 拓扑分散约束：避免所有副本落到同一个 Spot 实例池
      # Spot 中断可能影响同一 capacity pool 内的多个节点
      topologySpreadConstraints:
        - maxSkew: 1
          topologyKey: topology.kubernetes.io/zone
          whenUnsatisfiable: DoNotSchedule
          labelSelector:
            matchLabels:
              app: web-api
        - maxSkew: 2
          topologyKey: kubernetes.io/hostname
          whenUnsatisfiable: ScheduleAnyway
          labelSelector:
            matchLabels:
              app: web-api

      # 容忍 Spot 节点的 taint（如果有打 taint 的话）
      tolerations:
        - key: karpenter.sh/capacity-type
          operator: Equal
          value: spot
          effect: NoSchedule

      # PodDisruptionBudget 要配合使用，防止 consolidation 同时驱逐太多
      # 见下面 PDB 配置

      containers:
        - name: api
          image: myapp:v1.2.0
          # 务必设置合理的 requests，Karpenter 依赖此来选择实例大小
          resources:
            requests:
              cpu: "500m"
              memory: "512Mi"
            limits:
              cpu: "1000m"
              memory: "1Gi"

          # 优雅终止：Spot 中断前有 2 分钟，要在这时间内完成
          lifecycle:
            preStop:
              exec:
                command: ["/bin/sh", "-c", "sleep 5"]
          terminationGracePeriodSeconds: 60
---
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: web-api-pdb
spec:
  # 保证至少 60% 的副本始终可用（6 个副本中至少 3 个）
  minAvailable: "60%"
  selector:
    matchLabels:
      app: web-api
```

### 优先使用 Spot 的 NodeAffinity

```yaml
# 通过 nodeAffinity 软约束表达优先级，而非强制要求
affinity:
  nodeAffinity:
    preferredDuringSchedulingIgnoredDuringExecution:
      # 优先调度到 Spot 节点
      - weight: 80
        preference:
          matchExpressions:
            - key: karpenter.sh/capacity-type
              operator: In
              values: ["spot"]
      # 次选 on-demand（当 Spot 容量不足时）
      - weight: 20
        preference:
          matchExpressions:
            - key: karpenter.sh/capacity-type
              operator: In
              values: ["on-demand"]
```

---

## 多集群配置管理

管理多套集群（生产/预发/测试/沙箱等）时，关键是保持配置可追溯、差异可见。

### 目录结构

```
karpenter-configs/
├── base/                         # 通用基础配置（各集群共享）
│   ├── nodeclass-default.yaml
│   └── nodepool-general.yaml
│
├── clusters/
│   ├── prod/
│   │   ├── kustomization.yaml
│   │   ├── nodepool-general-patch.yaml    # 覆盖 limits 和实例类型
│   │   ├── nodepool-gpu.yaml              # 生产独有的 GPU 节点池
│   │   └── nodeclass-default-patch.yaml  # 覆盖 AMI tag 和子网 tag
│   │
│   ├── pre/
│   │   ├── kustomization.yaml
│   │   └── nodepool-general-patch.yaml   # 预发环境配置
│   │
│   ├── qa/
│   │   ├── kustomization.yaml
│   │   └── nodepool-general-patch.yaml   # 限制 limits 更小，节省成本
│   │
│   └── sandbox/
│       ├── kustomization.yaml
│       └── nodepool-gvisor.yaml           # gVisor 沙箱节点池
│
└── scripts/
    ├── sync.sh                   # 同步本地配置到集群
    └── diff.sh                   # 查看本地配置与集群当前状态的差异
```

### kustomization.yaml 示例

```yaml
# clusters/prod/kustomization.yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization

resources:
  - ../../base/nodeclass-default.yaml
  - ../../base/nodepool-general.yaml
  - nodepool-gpu.yaml

patches:
  - path: nodepool-general-patch.yaml
  - path: nodeclass-default-patch.yaml
```

```yaml
# clusters/prod/nodepool-general-patch.yaml
apiVersion: karpenter.sh/v1
kind: NodePool
metadata:
  name: general
spec:
  limits:
    cpu: "2000"       # prod 上限更高
    memory: 8000Gi
  disruption:
    consolidateAfter: 10m   # prod 整合更保守
    expireAfter: 720h
```

### 集群差异对照

| 配置项 | prod | sandbox-qa | sandbox-pre |
|---|---|---|---|
| CPU 上限 | 2000 核 | 200 核 | 100 核 |
| 实例类型偏好 | c5/m5/r5 + Graviton | m5/m6g 为主 | 任意小机型 |
| Spot 比例 | 70% Spot | 90% Spot | 90% Spot |
| consolidateAfter | 10m | 2m | 2m |
| expireAfter | 720h (30d) | 168h (7d) | 168h (7d) |
| GPU 节点池 | 有 | 无 | 无 |

### 配置同步脚本

```bash
#!/bin/bash
# scripts/sync.sh - 将本地配置应用到指定集群

set -euo pipefail

CLUSTER="${1:?请指定集群名称，例如: prod}"
DRY_RUN="${DRY_RUN:-true}"  # 默认 dry-run，安全起见

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="$SCRIPT_DIR/../clusters/$CLUSTER"

[[ ! -d "$CONFIG_DIR" ]] && { echo "找不到集群配置: $CONFIG_DIR"; exit 1; }

echo "=== 同步集群: $CLUSTER ==="
echo "配置目录: $CONFIG_DIR"

if [[ "$DRY_RUN" == "true" ]]; then
    echo "[DRY-RUN] 以下内容将被应用："
    kubectl kustomize "$CONFIG_DIR"
else
    echo "正在应用配置..."
    kubectl kustomize "$CONFIG_DIR" | kubectl apply -f -
    echo "完成"
fi
```

---

## 踩坑记录

### 坑 1：NodePool limits 设置过低导致 Pod Pending

**现象：** 集群节点数已达上限，但新 Pod 一直 Pending，事件显示 `NodePool capacity limit reached`。

**原因：** `spec.limits.cpu` 是所有由该 NodePool 管理的节点的 CPU 总和上限，不是单节点的限制。我们最初从 Cluster Autoscaler 迁移时，照着 ASG max size 估算，设得太保守了。

**排查：**
```bash
# 查看当前 NodePool 的资源使用情况
kubectl get nodepool general -o yaml | grep -A 10 "status:"
# 或
kubectl get nodepool general -o jsonpath='{.status.resources}' | jq .
```

**修复：** 根据实际峰值用量的 1.5-2 倍来设置 limits，并配置告警在达到 80% 时通知。

---

### 坑 2：Spot 中断 + consolidation 同时触发导致服务抖动

**现象：** 某天下午业务高峰期，5 分钟内出现了多次 Pod 重启，监控显示服务错误率飙升。

**原因：** Spot 中断通知触发了部分节点上的 Pod 驱逐，恰好此时 consolidation 也在将几个低利用率节点上的 Pod 迁移，两波驱逐叠加导致某些服务副本数量同时降到 PDB 限制以下。

**修复措施：**
1. 收紧 PDB：`minAvailable` 从 50% 改到 70%
2. 把关键服务的副本分布从同一个 NodePool 拆到两个（一个 Spot，一个 on-demand），确保 Spot 全挂时 on-demand 副本还在
3. 给关键服务设置 `podAntiAffinity`，强制跨节点分布：

```yaml
affinity:
  podAntiAffinity:
    requiredDuringSchedulingIgnoredDuringExecution:
      - labelSelector:
          matchLabels:
            app: critical-service
        topologyKey: kubernetes.io/hostname
```

---

### 坑 3：AMI 自动更新触发节点全量替换

**现象：** 某次 EKS 优化 AMI 发布新版本后，Karpenter 在 `expireAfter` 设置的时间窗内将几乎所有节点都替换了一遍，导致业务持续抖动超过 2 小时。

**原因：** `expireAfter` 是从节点创建时间算的，恰好大量节点在同一时间窗内创建（初始部署时），所以同时到期。

**修复：**
1. 不同 NodePool 设置不同的 `expireAfter`，错开轮换窗口：

```yaml
# 生产通用节点：30 天轮换
expireAfter: 720h

# GPU 节点：90 天轮换（更稳定）
expireAfter: 2160h
```

2. 如果不想自动轮换，可以设置 `expireAfter: Never`，手动在低峰期触发：

```bash
# 手动驱逐特定节点（让 Karpenter 重新拉新节点替换）
kubectl annotate node ip-10-0-1-50.us-west-2.compute.internal \
    karpenter.sh/do-not-disrupt=false
kubectl drain ip-10-0-1-50.us-west-2.compute.internal \
    --ignore-daemonsets --delete-emptydir-data
```

---

### 坑 4：大机型被 consolidation 后新节点规格不匹配

**现象：** 一台 m5.4xlarge 节点在低利用率时被 consolidation 终止，Pod 被重新调度到一台 m5.xlarge，但由于 Pod 请求的 CPU 加起来超过 xlarge 的容量，又触发了扩容，最终浪费了更多时间。

**原因：** consolidation 在评估目标节点时，只看当前运行的 Pod 资源 requests，没有考虑到高峰期会有更多 Pod 调度进来（HPA 还没来得及扩容）。

**修复：**
1. 给关键的 Deployment 配置合理的 `requests`（不要设太低，否则 Karpenter 会低估需求）
2. 设置 `consolidateAfter: 10m` 给 HPA 更多反应时间，避免节点刚缩就要再扩
3. 对某些不希望被整合的节点，可以在 Pod 上加注解：

```yaml
# 在 Pod template 的 annotations 中添加（阻止 Karpenter 驱逐此 Pod）
annotations:
  karpenter.sh/do-not-disrupt: "true"
```

---

## 常用排查命令

### NodeClaim 状态查看

```bash
# 查看所有 NodeClaim
kubectl get nodeclaim
# NAME                       TYPE         ZONE         NODE                         READY   AGE
# general-8kfzq              m5.2xlarge   us-west-2a   ip-10-0-1-50.ec2.internal   True    2d
# general-9xbtn              c5.xlarge    us-west-2b   ip-10-0-2-30.ec2.internal   True    1d

# 查看某个 NodeClaim 的详情（包括启动耗时、状态转换）
kubectl describe nodeclaim general-8kfzq

# 查看 NodeClaim 的状态条件
kubectl get nodeclaim general-8kfzq -o jsonpath='{.status.conditions}' | jq .

# 找出处于非 Ready 状态的 NodeClaim
kubectl get nodeclaim -o json | jq '.items[] | select(.status.conditions[] | select(.type=="Ready" and .status!="True")) | {name: .metadata.name, conditions: .status.conditions}'
```

### NodePool 状态

```bash
# 查看 NodePool 当前资源使用 vs 上限
kubectl get nodepool
# NAME      NODECLASS   NODES   READY   AGE
# general   default     8       8       15d

# 详细状态（包含资源用量）
kubectl describe nodepool general

# 所有 NodePool 的资源汇总
kubectl get nodepool -o custom-columns=\
'NAME:.metadata.name,NODES:.status.resources.nodes,CPU:.status.resources.cpu,MEMORY:.status.resources.memory'
```

### Karpenter 日志

```bash
# 查看 Karpenter Controller 日志（最近 100 行）
kubectl logs -n kube-system -l app.kubernetes.io/name=karpenter \
    -c controller --tail=100

# 实时跟踪日志
kubectl logs -n kube-system -l app.kubernetes.io/name=karpenter \
    -c controller -f

# 只看扩容事件
kubectl logs -n kube-system -l app.kubernetes.io/name=karpenter \
    -c controller --tail=500 | grep -E "launched|created|registered"

# 只看整合事件
kubectl logs -n kube-system -l app.kubernetes.io/name=karpenter \
    -c controller --tail=500 | grep -E "consolidat|disruption|terminating"

# 查看 Karpenter 的 Kubernetes 事件
kubectl get events -n kube-system --sort-by='.lastTimestamp' | \
    grep -i karpenter | tail -30
```

### 节点与 Pod 关联排查

```bash
# 查看哪些 Pod 在 Karpenter 管理的节点上
kubectl get pods -A -o wide | grep "karpenter"

# 查看某个节点上的所有 Pod（按 CPU 请求排序）
NODE="ip-10-0-1-50.us-west-2.compute.internal"
kubectl get pods -A --field-selector spec.nodeName=$NODE \
    -o custom-columns='NAMESPACE:.metadata.namespace,NAME:.metadata.name,CPU_REQ:.spec.containers[0].resources.requests.cpu'

# 查看哪些 Pod 没有设置 resource requests（会影响 Karpenter 节点选型）
kubectl get pods -A -o json | jq '.items[] |
  select(.spec.containers[].resources.requests == null) |
  {namespace: .metadata.namespace, name: .metadata.name}'

# 模拟 Karpenter 决策（查看某个 pending Pod 会触发哪种节点）
kubectl describe pod <pending-pod-name> | grep -A 5 "Events:"
```
