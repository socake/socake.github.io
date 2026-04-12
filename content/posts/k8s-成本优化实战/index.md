---
title: "Kubernetes 成本优化实战：系统性降本的四条路径"
date: 2025-12-08T16:00:00+08:00
draft: false
tags: ["Kubernetes", "Karpenter", "成本优化", "FinOps", "运维"]
categories: ["Kubernetes"]
description: "记录在 AWS EKS 上通过 Karpenter 弹性节点、资源规格治理、节点规格收敛等手段，系统性降低云资源成本的完整过程"
summary: "真实的降本案例：从发现成本异常到分析根因，通过 Karpenter 节点弹性伸缩、资源请求规格治理、大机型收敛等手段，系统性降低 AWS EC2 成本。包含具体配置和执行思路。"
toc: true
math: false
diagram: false
series: ["K8s 完全指南"]
keywords: ["kubernetes", "成本优化", "karpenter", "finops", "aws eks"]
params:
  reading_time: true
---

运营多套 AWS EKS 集群，从某个时间点开始，AWS 账单每个月都在涨，直到某天收到告警通知，说本月 EC2 费用超出预算阈值，才开始认真盯这件事。

这篇文章记录了整个降本过程：从发现问题、分析根因，到逐步实施四项优化手段的完整路径。不是教程，是一个有点痛苦的真实案例。

---

## 一、成本告警触发，开始排查

### 怎么发现问题的

配置了 AWS Budgets 告警，每月费用超出预算的 80% 会自动推送通知。有一天早上收到消息：本月 EC2 费用已超出月度预算阈值。

第一反应是：最近没有大的业务增长，为什么费用涨了这么多？

打开 AWS Cost Explorer，按资源维度分析：

```
EC2 实例费用       $4,312 / 月    占比 63%
EBS 存储费用       $892 / 月     占比 13%
数据传输费用       $687 / 月     占比 10%
其他（ELB/ECR等）  $956 / 月     占比 14%
```

EC2 是大头。再往下钻，按 Tag 分组查看各环境费用分布：

```
prod               占总费用 ~47%
staging/qa         占总费用 ~35%    ← 不合理，非生产环境费用接近生产
sandbox            占总费用 ~18%
```

Staging 和 QA 环境费用快接近生产了，这明显有问题——这些环境白天用，晚上和周末基本无流量，根本不需要一直跑这么多节点。

### 按时间段分析

Cost Explorer 的每日费用曲线更说明问题：工作日和周末的 EC2 费用几乎没有差别。这意味着周末没人用的环境，节点仍然在满载运行。

光这一项，就意味着每周有 2/7 的时间在"空转"烧钱。

---

## 二、根因分析

我把问题归纳成四类，每类单独分析：

### 根因一：资源请求设置不合理

用 `kubectl top nodes` 和 Grafana 对比节点的 allocated resources 和 actual usage，发现差距触目惊心：

```
节点类型       CPU 请求/实际使用    内存请求/实际使用
c5.2xlarge    78% / 12%           72% / 31%
m5.xlarge     65% / 18%           81% / 45%
```

CPU 请求是实际使用的 6 倍多。节点明明只用了 12% 的 CPU，却因为 requests 占满，Kubernetes 调度器认为这个节点已经"满了"，继续拉新节点。

历史原因：早期开发同学图省事，给服务设置了非常高的 CPU requests（比如 `requests.cpu: 2000m`），从没有人去真正测量过实际消耗。

### 根因二：夜间/周末无弹性

我们当时用的是 Cluster Autoscaler（CA），CA 的缩容逻辑比较保守：
- 缩容触发条件：节点资源利用率低于 50% 且持续 10 分钟以上
- 但只要节点上有 Pod 且 Pod 没有设置 PodDisruptionBudget，CA 默认不驱逐

结果就是：晚上流量降为零，服务副本数不变（HPA 缩到 min replicas），但每个 Pod 的 requests 还是那么高，节点的 allocated 利用率仍然超过 50%，CA 不触发缩容。

节点就这样一整晚白跑。

### 根因三：大内存实例跑小负载应用

有几个服务在我们采购实例时是按"最大负载"规格买的，用的是 `r5.2xlarge`（64GB 内存）。这些服务后来做了优化，实际内存峰值不超过 4GB，但实例规格没有跟着调整。

一台 `r5.2xlarge` 在 us-west-2 的按需价格约 $0.504/小时，月费约 $363。换成 `c5.xlarge`（4 vCPU / 8GB）的话月费只需 $124，足够这几个服务用了。

### 根因四：RabbitMQ EC2 实例冗余

我们的 RabbitMQ 是部署在独立 EC2 上的，用的是 `m5.large`（2 vCPU / 8GB），三节点集群。当时的考量是"中间件上 K8s 不稳定"，但其实这个 RabbitMQ 的消息量很低（日均 10 万条消息），远没达到需要独立 EC2 的级别。

三台 `m5.large` 月费约 $210，加上 EBS 存储，实际约 $280/月。

---

## 三、优化手段一：Karpenter 弹性节点

这是整个降本项目里改动最大、效果最显著的一步。

### Karpenter vs Cluster Autoscaler

在切换之前，我专门对比了两者的核心差异，帮助团队说服迁移：

| 对比维度 | Cluster Autoscaler | Karpenter |
|---------|-------------------|-----------|
| 扩容速度 | 需要先确定 NodeGroup，再拉起 EC2（2-3 分钟） | 直接调用 EC2 API，通常 < 60 秒 |
| 实例选择 | 固定 NodeGroup 的实例类型 | 动态选择最合适/最便宜的实例类型 |
| Spot 支持 | 需要预配置多个 Spot NodeGroup | 原生支持 Spot，自动 fallback 到 On-Demand |
| 缩容策略 | 保守，容易缩不下来 | `WhenUnderutilized` 策略更激进，可合并节点 |
| 节点整合 | 不支持 | 支持（把多个半空节点合并到少数节点） |
| 配置复杂度 | 简单（NodeGroup 配置） | 稍复杂（NodePool + EC2NodeClass） |

关键优势是**节点整合（Consolidation）**：假设现在有 3 个节点，每个用了 40% 的资源，Karpenter 可以把这些 Pod 重新调度，合并到 2 个甚至 1 个节点上，然后删除多余节点。CA 不会做这件事。

### NodePool 配置

```yaml
apiVersion: karpenter.sh/v1
kind: NodePool
metadata:
  name: general-purpose
spec:
  template:
    metadata:
      labels:
        node-type: general
    spec:
      nodeClassRef:
        group: karpenter.k8s.aws
        kind: EC2NodeClass
        name: general-purpose
      requirements:
        # 允许 On-Demand 和 Spot，优先用 Spot
        - key: karpenter.sh/capacity-type
          operator: In
          values: ["spot", "on-demand"]
        # 限制实例族，排除老旧机型和超大机型
        - key: node.kubernetes.io/instance-type
          operator: In
          values:
            - c5.large
            - c5.xlarge
            - c5.2xlarge
            - c5a.large
            - c5a.xlarge
            - c5a.2xlarge
            - m5.large
            - m5.xlarge
            - m5.2xlarge
        # 只用特定 AZ，避免数据跨 AZ 流量费
        - key: topology.kubernetes.io/zone
          operator: In
          values: ["us-west-2a", "us-west-2b", "us-west-2c"]
      # 节点启动后最长存活时间（强制轮转，避免 Spot 积累风险）
      expireAfter: 720h
  # 整合策略
  disruption:
    consolidationPolicy: WhenUnderutilized
    consolidateAfter: 30s    # 发现空闲后 30s 开始整合
    budgets:
      # 业务高峰期限制同时中断的节点数
      - schedule: "0 9-18 * * 1-5"   # 工作日 9-18 点
        nodes: "10%"                  # 最多同时中断 10% 的节点
      - nodes: "50%"                  # 其他时段允许更激进的整合
  # NodePool 资源上限，防止意外扩容过多
  limits:
    cpu: 100
    memory: 200Gi
```

### EC2NodeClass 配置

```yaml
apiVersion: karpenter.k8s.aws/v1
kind: EC2NodeClass
metadata:
  name: general-purpose
spec:
  amiFamily: AL2
  # AMI 选择策略（使用最新的 EKS 优化 AMI）
  amiSelectorTerms:
    - alias: al2@latest
  # 节点所在子网（通过 Tag 选择）
  subnetSelectorTerms:
    - tags:
        karpenter.sh/discovery: my-cluster
  # 安全组
  securityGroupSelectorTerms:
    - tags:
        karpenter.sh/discovery: my-cluster
  # 实例 Profile（需要有 SSM/ECR 权限）
  instanceProfile: KarpenterNodeInstanceProfile
  # 根卷配置
  blockDeviceMappings:
    - deviceName: /dev/xvda
      ebs:
        volumeSize: 50Gi
        volumeType: gp3
        iops: 3000
        throughput: 125
        deleteOnTermination: true
  # 节点启动脚本（可注入自定义配置）
  userData: |
    #!/bin/bash
    /etc/eks/bootstrap.sh my-cluster \
      --container-runtime containerd \
      --kubelet-extra-args '--max-pods=110'
  tags:
    Environment: staging
    ManagedBy: karpenter
```

### Spot 实例容忍配置

使用 Spot 实例的 Pod 需要能容忍节点被回收（Spot 中断），关键配置：

```yaml
# 应用 Deployment 添加 toleration
spec:
  template:
    spec:
      tolerations:
        - key: karpenter.sh/capacity-type
          operator: Equal
          value: spot
          effect: NoSchedule
      # 优先调度到 Spot 节点
      affinity:
        nodeAffinity:
          preferredDuringSchedulingIgnoredDuringExecution:
            - weight: 100
              preference:
                matchExpressions:
                  - key: karpenter.sh/capacity-type
                    operator: In
                    values: ["spot"]
      # 确保 Pod 能优雅处理中断
      terminationGracePeriodSeconds: 60
```

对于无状态应用，Spot 中断影响极小——Karpenter 会在节点中断前 2 分钟收到警告，并开始驱逐 Pod 到其他节点。只要 HPA 有多副本，用户基本感知不到。

**不适合上 Spot 的服务**：有状态中间件（数据库、消息队列）、对延迟极度敏感的服务、启动时间超过 5 分钟的服务。

---

## 四、优化手段二：资源规格治理

Karpenter 装好了，但如果服务的 requests 还是虚高，节点整合效果会大打折扣——因为 Karpenter 的整合判断也是基于 requests。

### 用 VPA 推荐模式扫描存量服务

VPA（Vertical Pod Autoscaler）有三种模式：`Auto`（自动更新 requests）、`Initial`（只在 Pod 创建时更新）、`Off`（只给建议，不修改）。

我们先用 `Off` 模式扫描所有服务，看推荐值和当前设置的差距：

```yaml
apiVersion: autoscaling.k8s.io/v1
kind: VerticalPodAutoscaler
metadata:
  name: my-service-vpa
  namespace: production
spec:
  targetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: my-service
  updatePolicy:
    updateMode: "Off"   # 只推荐，不修改
  resourcePolicy:
    containerPolicies:
      - containerName: my-service
        minAllowed:
          cpu: 50m
          memory: 64Mi
        maxAllowed:
          cpu: 4000m
          memory: 4Gi
```

装好后等几天，VPA 会根据实际用量计算推荐值：

```bash
kubectl describe vpa my-service-vpa -n production
# 输出示例：
# Recommendation:
#   Container Recommendations:
#     Container Name: my-service
#     Lower Bound:
#       Cpu: 50m
#       Memory: 128Mi
#     Target:
#       Cpu: 120m        ← VPA 推荐，当前设置 1000m
#       Memory: 256Mi    ← VPA 推荐，当前设置 2Gi
#     Upper Bound:
#       Cpu: 800m
#       Memory: 1Gi
```

CPU requests 从 1000m 降到 120m，差了将近 8 倍。这样的服务在我们系统里有十几个。

### 资源规格分级标准

光降低个别服务还不够，问题的本质是没有规范。我们制定了内部资源规格分级标准，要求新服务上线时必须选择对应等级，不允许随意填写：

| 规格等级 | CPU Requests | CPU Limits | Memory Requests | Memory Limits | 适用场景 |
|---------|-------------|-----------|----------------|--------------|---------|
| XS      | 50m         | 200m      | 64Mi           | 256Mi        | 轻量工具、定时任务 |
| S       | 100m        | 500m      | 128Mi          | 512Mi        | 低流量服务、内部工具 |
| M       | 200m        | 1000m     | 256Mi          | 1Gi          | 普通业务服务（默认） |
| L       | 500m        | 2000m     | 512Mi          | 2Gi          | 中等流量、计算型服务 |
| XL      | 1000m       | 4000m     | 1Gi            | 4Gi          | 高流量核心服务 |
| 自定义  | 申请审批      | —         | —              | —            | 超出 XL 的服务 |

这个标准落地阻力不小——开发同学的第一反应是"我的服务很特殊，M 不够用"。我们的做法是：先用 VPA 推荐值作为数据依据，再和开发确认，不接受拍脑袋的规格申请。

推动了大约 3 周，把存量服务的 requests 整体下调了约 60%。

---

## 五、优化手段三：节点规格收敛

资源请求合理了，下一步是让节点规格也合理。

### 移除大机型

排查 NodeGroup 配置，发现历史上为了"保险"买了几台 `c5.4xlarge`（16 vCPU / 32GB）来跑 staging 环境。这些节点跑着的服务，加在一起实际用量也就 3-4 vCPU / 8GB，大量资源空转。

迁移到 Karpenter 后，我们明确限制了 NodePool 里不包含 `c5.4xlarge` 以上的机型。Karpenter 在整合节点时，会自动选择更小、更合适的机型来装载这些 Pod。

实测一周后，staging 集群从平均 4 台 `c5.4xlarge` 降到 2 台 `c5.xlarge`，节省了约 $340/月。

### Spot 比例调整

切换 Karpenter 之前，我们的 Spot 使用比例接近零（历史遗留，当时 CA 配置里只有 On-Demand NodeGroup）。切换后：

- Staging/QA 环境：90% Spot + 10% On-Demand
- Production 环境：30% Spot + 70% On-Demand（核心服务强制 On-Demand）

Spot 实例相比 On-Demand 通常便宜 60-70%。以 `c5.xlarge` 为例：
- On-Demand：$0.17/小时 = $124/月
- Spot：约 $0.05-0.07/小时 = $37-51/月

Staging 环境全面切 Spot 后，EC2 费用直接砍掉一半多。

---

## 六、优化手段四：中间件降配

### RabbitMQ 迁移上 K8s

这是最简单也最直接的一步。把 RabbitMQ 从独立 EC2 迁移到 K8s 集群内部运行，省掉了三台 EC2 的费用。

迁移顾虑主要是稳定性。我做了以下评估：

- 消息量：日均 10 万条，峰值 500 条/秒，完全在 K8s RabbitMQ 的承载范围内
- 持久化：用 PVC（EBS gp3）做消息持久化，数据安全性有保障
- 高可用：K8s 集群本身多节点，配合 PodAntiAffinity 确保 RabbitMQ 副本不在同一节点
- 监控：用 ServiceMonitor + Prometheus 采集 RabbitMQ 指标，钉钉告警覆盖队列积压、连接数异常等情况

使用 Bitnami RabbitMQ Helm Chart 部署：

```yaml
# rabbitmq-values.yaml
replicaCount: 3
auth:
  username: admin
  password: "your-password"
  erlangCookie: "your-erlang-cookie"

persistence:
  enabled: true
  size: 20Gi
  storageClass: gp3

resources:
  requests:
    cpu: 200m
    memory: 512Mi
  limits:
    cpu: 1000m
    memory: 2Gi

affinity:
  podAntiAffinity:
    requiredDuringSchedulingIgnoredDuringExecution:
      - labelSelector:
          matchLabels:
            app.kubernetes.io/name: rabbitmq
        topologyKey: kubernetes.io/hostname

metrics:
  enabled: true
  serviceMonitor:
    enabled: true
    namespace: monitoring
    labels:
      release: kube-prometheus-stack
```

迁移过程：先在 K8s 内起新的 RabbitMQ 集群，修改应用连接配置（配置中心更新），流量迁移过去验证稳定后，停掉旧 EC2。整个过程业务无感知。

### 配套告警

迁移上 K8s 后，告警规则也要跟上：

```yaml
# RabbitMQ 告警规则
- alert: RabbitMQQueueDepthHigh
  expr: |
    rabbitmq_queue_messages_ready > 10000
  for: 5m
  labels:
    severity: warning
  annotations:
    summary: "RabbitMQ 队列 {{ $labels.queue }} 积压超过 10000 条"

- alert: RabbitMQConnectionsDrop
  expr: |
    delta(rabbitmq_connections[5m]) < -10
  for: 2m
  labels:
    severity: critical
  annotations:
    summary: "RabbitMQ 连接数在 5 分钟内急剧下降，可能有服务大规模断连"
```

---

## 七、结果与经验总结

### 降本效果汇总

整理每项优化的实际收益（以相对占比呈现）：

| 优化项 | 贡献占比 | 说明 |
|-------|---------|------|
| Karpenter 节点整合（staging/QA） | ~33% | 周末/夜间空节点自动回收 |
| Spot 实例替代 On-Demand（staging） | ~25% | 90% Spot，均价降低 ~65% |
| 资源请求规格治理（全环境） | ~18% | requests 下调，节点利用率提升，少拉节点 |
| 大机型 c5.4xlarge 下线 | ~11% | 替换为 c5.xlarge + 弹性 |
| 消息队列迁移上 K8s | ~13% | 省去独立 EC2 费用 |
| **合计** | **~100%** | 月均总费用下降幅度显著 |

生产环境因为要保障稳定性，Spot 比例和整合策略相对保守，暂时没有大幅优化。后续计划进一步细化生产环境的 NodePool 分层（核心服务 On-Demand、普通服务 Spot）。

### 经验：先分析再动手

这次降本项目前后花了大约 6 周，其中两周在分析和规划，四周在执行。

回顾下来最重要的原则是：**先分析再动手，不要盲目缩容**。

我见过一种常见的错误操作：发现节点利用率低，直接缩减最小节点数。这样做有很大风险——如果分析不到位，在流量高峰期节点数不够，服务扩容比预期慢，可能直接影响用户。

正确做法是：
1. 先通过 Cost Explorer + Grafana 充分理解成本分布和资源使用现状
2. 找到"低效"的根因（是 requests 虚高？还是没有弹性？还是规格选错了？）
3. 在压力最小的环境（QA/staging）先试，观察一两周，确认没有问题再推到生产
4. 每一步变更都要有对应的监控和告警，异常能第一时间发现

另外，**Karpenter 的整合策略要谨慎配置**。`consolidateAfter: 30s` 意味着节点利用率下降 30 秒后就开始整合，这在流量快速波动的场景可能导致频繁的 Pod 驱逐。建议生产环境设置 `consolidateAfter: 300s` 以上，给 HPA 足够的时间扩副本再整合节点。

### 持续治理机制

成本优化不是一次性的工作，需要持续跟踪：

**每周**：在 Cost Explorer 查看各环境费用趋势，对比环比变化。

**每月**：检查 VPA 推荐值，推动偏差较大的服务更新资源规格；检查 Karpenter 整合日志，确认整合效果符合预期。

**每季度**：重新评估实例类型选择，AWS 会定期推出新的实例族（比如 Graviton 系列），通常性价比更高。

**持续**：所有新服务上线时，强制按资源规格分级标准填写 requests/limits，Code Review 阶段把关。

降本不是终点，是一个持续的工程习惯。

---

最后说一句：这些优化做完后，我们的 AWS 账单确实好看多了，但最大的收获不是省的那两千块钱，而是逼着我把整个集群的资源管理逻辑从头到尾梳理了一遍。很多历史配置为什么这样设、有没有还在用、合不合理，以前都是糊涂账。现在每个 NodePool、每个服务的资源规格都有据可查，运维起来心里踏实很多。
