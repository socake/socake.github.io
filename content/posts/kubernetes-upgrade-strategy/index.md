---
title: "Kubernetes 集群升级策略：零停机升级的完整实践指南"
date: 2026-04-12T10:00:00+08:00
draft: false
tags: ["Kubernetes", "集群升级", "EKS", "运维", "零停机", "SRE"]
categories: ["Kubernetes"]
series: ["K8s 完全指南"]
description: 'Kubernetes 集群零停机升级完整指南：升级前检查、API 弃用处理、EKS 托管节点组滚动升级、PDB 配置，以及升级过程中常见故障的处理方法'
summary: "K8s 集群升级听起来简单，实际操作中坑很多：API 弃用导致的 Helm 失败、Admission Webhook 拦截升级流量、PDB 配置不当导致服务中断。这篇文章从真实的升级经验出发，给出一套可复用的零停机升级方案。"
toc: true
math: false
diagram: false
keywords: ["Kubernetes升级", "EKS升级", "PodDisruptionBudget", "节点升级", "API弃用", "零停机"]
params:
  reading_time: true
---

我们的 EKS 集群从 1.24 一路升级到 1.30，踩过的坑远比文档说的多。K8s 升级不只是点击几个按钮的事情——控制平面升级完，发现 Admission Webhook 不兼容；节点 Drain 时，有个 Pod 带着 PVC 就是驱逐不掉；好不容易升完，某个团队的 Helm chart 里用了已废弃的 API，部署流水线直接报错。这篇文章把这些场景都覆盖到，给出可操作的处理方法。

## 版本支持策略：为什么每次只能升一个 minor 版本

K8s 的版本号是 `major.minor.patch`，比如 `1.29.3`。社区对每个 minor 版本的支持周期大约是 14 个月（从发布到 EOL）。目前通常同时支持最近 3 个 minor 版本，意味着如果你在用 1.27，等 1.31 发布时 1.27 就进入 EOL 了。

**不能跨版本升级**是硬约束，不是建议。比如从 1.27 升到 1.29，必须经过 1.28，不能直接跳。原因是：
1. etcd 数据格式在相邻版本之间保持兼容，跨版本不保证
2. API 弃用是渐进的，跳版本会遗漏中间的迁移窗口
3. 控制平面组件（apiserver、kubelet）只保证相邻版本的 skew 兼容

所以如果你的集群落后了 3 个版本，需要做 3 次独立升级，每次都要走完整流程。

## 升级前检查清单

### 1. API 弃用扫描（Pluto）

K8s 每个版本都会弃用或移除一批 API。比如 `networking.k8s.io/v1beta1` 的 Ingress 在 1.22 被移除，换成 `networking.k8s.io/v1`。如果你的 Helm chart 还在用旧 API，升级后部署就会报错。

用 Pluto 扫描集群里现有的资源和本地 Helm chart：

```bash
# 安装 Pluto
brew install fairwindsops/tap/pluto

# 扫描集群里已部署的资源（检查是否有将在目标版本废弃的 API）
pluto detect-all-in-cluster --target-versions k8s=v1.30.0

# 扫描本地 Helm chart
pluto detect-helm --target-versions k8s=v1.30.0
```

输出示例：
```
NAME                          NAMESPACE   KIND        VERSION                 REPLACED IN   REMOVED IN
nginx-ingress/ingress-nginx   default     Ingress     networking.k8s.io/v1beta1   1.19        1.22
```

把所有 `REMOVED IN <= 目标版本` 的条目都处理掉，再升级。

### 2. Admission Webhook 兼容性检查

Admission Webhook 是升级中最容易被忽视的炸弹。Webhook 配置了 `failurePolicy: Fail` 时，如果 webhook server 不响应，API Server 会拒绝所有相关资源的创建和更新请求——包括节点驱逐过程中的 Pod 重建。

检查集群里所有 Webhook：

```bash
# 列出所有 MutatingWebhookConfiguration
kubectl get mutatingwebhookconfigurations -o json | \
  jq '.items[] | {name: .metadata.name, failurePolicy: .webhooks[].failurePolicy}'

# 列出所有 ValidatingWebhookConfiguration
kubectl get validatingwebhookconfigurations -o json | \
  jq '.items[] | {name: .metadata.name, failurePolicy: .webhooks[].failurePolicy}'
```

对于 `failurePolicy: Fail` 的 Webhook，确认对应的 webhook server 在升级期间是高可用的，或者临时改为 `Ignore`。

### 3. 检查 DaemonSet 和系统组件版本

节点升级时，DaemonSet 会随节点一起被驱逐然后在新节点上重建。确认 DaemonSet 使用的镜像兼容新版 kubelet：

```bash
# 检查所有 DaemonSet
kubectl get daemonsets -A -o json | \
  jq '.items[] | {namespace: .metadata.namespace, name: .metadata.name, image: .spec.template.spec.containers[].image}'

# 重点检查 CNI 插件（如 Calico、Cilium）、日志收集器、监控 agent 的版本
```

CNI 插件版本不兼容新版 K8s 会导致新节点上的 Pod 无法获得 IP，这是最严重的故障之一。

### 4. PodDisruptionBudget 现状检查

升级前了解集群里有哪些 PDB，以及它们的配置是否合理：

```bash
kubectl get pdb -A -o wide
```

没有 PDB 的关键服务在节点 Drain 时可能被一次性全部驱逐，导致服务中断。升级前补上 PDB（见下节）。

## EKS 升级流程

EKS 把升级分为两步：控制平面和数据平面。控制平面由 AWS 管理，你只需要触发升级；数据平面（工作节点）需要手动或半自动处理。

### 控制平面升级

```bash
# 触发控制平面升级（实际会有 10-20 分钟的滚动更新）
aws eks update-cluster-version \
  --name my-cluster \
  --kubernetes-version 1.30 \
  --region us-west-2

# 等待升级完成
aws eks wait cluster-active --name my-cluster --region us-west-2

# 确认版本
aws eks describe-cluster --name my-cluster --query 'cluster.version'

# 更新 kubeconfig
aws eks update-kubeconfig --name my-cluster --region us-west-2
```

控制平面升级期间，API Server 会有短暂的滚动重启，已建立的长连接（如 `kubectl exec`）会断开，但不影响已运行的 Pod。

控制平面升级后，记得更新 kube-proxy、CoreDNS 和 VPC CNI 这三个附加组件到推荐版本：

```bash
# 更新 kube-proxy
aws eks update-addon --cluster-name my-cluster --addon-name kube-proxy \
  --addon-version v1.30.0-eksbuild.3 --resolve-conflicts OVERWRITE

# 更新 CoreDNS
aws eks update-addon --cluster-name my-cluster --addon-name coredns \
  --addon-version v1.11.1-eksbuild.9 --resolve-conflicts OVERWRITE

# 更新 VPC CNI
aws eks update-addon --cluster-name my-cluster --addon-name vpc-cni \
  --addon-version v1.18.1-eksbuild.3 --resolve-conflicts OVERWRITE
```

### 托管节点组升级（推荐）

托管节点组升级是 AWS 帮你做滚动替换：新节点用新 AMI 启动，等新节点 Ready 后再 Drain 老节点，一批一批来：

```bash
# 触发节点组升级
aws eks update-nodegroup-version \
  --cluster-name my-cluster \
  --nodegroup-name workers \
  --kubernetes-version 1.30

# 或者指定具体的 AMI Release Version
aws eks update-nodegroup-version \
  --cluster-name my-cluster \
  --nodegroup-name workers \
  --release-version 1.30.0-20241201

# 监控进度
aws eks describe-nodegroup \
  --cluster-name my-cluster \
  --nodegroup-name workers \
  --query 'nodegroup.status'
```

默认情况下，托管节点组升级一次最多让 1 个节点不可用（`maxUnavailable: 1`）。可以修改这个配置加速升级（但要确保集群有足够容量）：

```bash
aws eks update-nodegroup-config \
  --cluster-name my-cluster \
  --nodegroup-name workers \
  --update-config maxUnavailable=2
```

### 自管理节点组升级（手动流程）

Karpenter 管理的节点或手动创建的节点组需要手动 Drain：

```bash
# 1. 标记节点不可调度
kubectl cordon node-1.us-west-2.compute.internal

# 2. 驱逐节点上的所有 Pod
kubectl drain node-1.us-west-2.compute.internal \
  --ignore-daemonsets \   # DaemonSet Pod 不驱逐
  --delete-emptydir-data \ # 允许删除 emptyDir 数据
  --timeout=300s \         # 最多等 5 分钟
  --grace-period=30        # 给 Pod 30 秒优雅退出

# 3. 等节点完全驱逐后，终止并替换节点（新节点会用新 AMI 自动加入集群）

# 4. 确认新节点 Ready 后，如果是手动 uncordon 的场景
kubectl uncordon new-node-1.us-west-2.compute.internal
```

## PodDisruptionBudget：保护关键服务

PDB 告诉 K8s 在自愿中断（如节点 Drain）时，最少保留多少个 Pod 副本：

```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: order-service-pdb
  namespace: commerce
spec:
  # 方式一：最少保留 2 个 Pod（绝对值）
  minAvailable: 2

  # 方式二：最多允许 1 个 Pod 不可用（绝对值）
  # maxUnavailable: 1

  # 方式三：最多允许 20% 不可用（百分比）
  # maxUnavailable: 20%

  selector:
    matchLabels:
      app: order-service
```

`minAvailable` vs `maxUnavailable` 的选择：
- **关键单体服务（2 副本）**：用 `minAvailable: 1`，保证至少 1 个在线
- **无状态水平扩展服务**：用 `maxUnavailable: 25%`，允许批量驱逐加速升级
- **有状态服务（数据库）**：用 `minAvailable: N-1`（N 是总副本数），最保守

**注意**：PDB 必须和副本数匹配。如果 Deployment 只有 1 个副本，设置 `minAvailable: 1` 会导致这个 Pod 永远无法被驱逐，节点 Drain 会卡住。

## 节点 Drain 常见卡点处理

### 1. DaemonSet Pod 无法驱逐

Drain 命令默认拒绝驱逐 DaemonSet 管理的 Pod（因为驱逐后 DaemonSet controller 会立即在同一节点重建，没有意义）。加 `--ignore-daemonsets` 跳过它们，这是正确的——DaemonSet Pod 会在新节点上自动创建。

### 2. Pod 有 PVC 挂载

有 PVC 的 Pod 驱逐后，PVC 需要重新绑定到新节点。如果存储类不支持跨 AZ 迁移（比如 `gp2` 只能在同 AZ 内迁移），驱逐后 Pod 可能调度到其他 AZ 导致 PVC 挂载失败。

处理方法：
1. 使用支持跨 AZ 的存储类（如 EFS、或者带 topology 约束的 EBS）
2. 如果必须用 AZ 绑定的 PVC，先确保目标 AZ 有可用节点
3. 对于数据库类服务，在节点 Drain 前手动迁移 PVC

```bash
# 检查 Pod 使用的 PV 和 AZ
kubectl get pv -o json | jq '.items[] | {name: .metadata.name, zone: .metadata.labels["topology.kubernetes.io/zone"], storageClass: .spec.storageClassName}'
```

### 3. Pod 无 PDB 或 PDB 配置导致卡住

如果集群里有 Pod 没有 PDB，Drain 会直接删除它们（只要没有其他约束）。但如果 PDB 的 `minAvailable` 设置得太高，Drain 会一直等待，超时后报错：

```
error when evicting pods/"my-pod": Cannot evict pod as it would violate the pod's disruption budget.
```

解决方案之一是临时放宽 PDB：

```bash
kubectl patch pdb my-pdb -p '{"spec":{"minAvailable":0}}'
# 完成 Drain 后恢复
kubectl patch pdb my-pdb -p '{"spec":{"minAvailable":1}}'
```

## 升级后验证

节点全部替换完毕后，执行一轮健康检查：

```bash
# 检查所有节点状态
kubectl get nodes -o wide

# 检查所有 Pod 是否正常运行
kubectl get pods -A | grep -v Running | grep -v Completed

# 检查所有 Deployment 副本数是否达到期望值
kubectl get deployments -A | awk '$4 != $5 {print}'

# 检查 HPA 状态
kubectl get hpa -A

# 检查关键 CRD 是否正常
kubectl get crds | grep -v "CREATED AT"

# 跑一次 Helm 模板验证（确认 chart API 版本兼容）
helm template my-release ./my-chart --kube-version 1.30
```

## 回滚策略：控制平面无法降级

这是很多人不知道的关键点：**控制平面一旦升级，无法降级**。AWS 和所有云厂商都不支持 K8s 版本降级。所以：

1. **升级前快照重要数据**（Velero 备份、etcd 快照）
2. **控制平面用蓝绿集群而不是原地升级**（成本更高但更安全）
3. **节点可以回滚**：如果工作节点升级后有问题，可以用旧版 AMI 替换回来（控制平面仍然是新版本，但 kubelet 版本向前兼容一个 minor 版本）

实际操作中最安全的升级方式是：

1. 先在 staging 环境完整演练
2. 生产环境先升级一小部分节点（canary 节点组）
3. 观察 24 小时无异常后，全量升级剩余节点

## 真实案例：一次 1.27 → 1.29 的跨版本误操作

曾经有个同事在 QA 环境手滑，直接触发了 1.27 → 1.29 的控制平面升级（跳过了 1.28）。EKS 控制台实际上会阻止这种操作，报错 `InvalidParameterException: Kubernetes version 1.29 is not valid for upgrade from version 1.27`。

但更实际的教训是：我们当时有个服务的 Helm chart 里混用了两个版本的 API（`apps/v1beta1` 和 `apps/v1`），在 1.28 升级时 Pluto 扫出来了但没处理，结果拖到 1.29 升级时那个 API 已经被移除，升级完成后整个服务无法部署，紧急回滚 chart 处理了 2 小时。

教训：**Pluto 的报告必须清零才能开始升级，不要带着已知问题上路。**
