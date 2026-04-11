---
title: "AWS EKS 生产实践：网络、安全与多集群管理"
date: 2026-04-08T14:00:00+08:00
draft: false
tags: ["AWS", "EKS", "Kubernetes", "云原生", "安全"]
categories: ["AWS"]
description: "总结在多套 EKS 生产集群中积累的实战经验，涵盖网络选型、IRSA、Karpenter、安全加固与成本优化。"
summary: "管理多套 EKS 集群两年下来，踩了不少坑。本文系统整理网络选型、IAM 权限、节点管理、集群升级、安全加固和成本控制这六个核心话题，每个话题都有具体配置示例和实际遇到的问题。"
toc: true
math: false
diagram: false
keywords: ["EKS", "VPC CNI", "Karpenter", "IRSA", "EKS 升级", "Spot 实例", "Network Policy"]
params:
  reading_time: true
---

管理多套 EKS 集群两年下来，从最初踩的 IP 地址耗尽、IRSA 配置错误，到后来系统化做多账号隔离和成本控制，积累了一些不在官方文档里的实战心得。本文尽量绕开基础概念，聚焦在生产环境实际遇到的决策和问题。

## 网络选型：VPC CNI vs Cilium

EKS 默认使用 AWS VPC CNI，每个 Pod 直接分配 VPC IP，优点是网络拓扑简单、与 AWS 原生服务（ALB、Security Group for Pods）无缝集成。但有一个致命问题：**IP 地址消耗极快**。

一个 `m5.xlarge` 节点（4 vCPU）最多能挂 4 个 ENI，每个 ENI 最多 15 个 IP，理论上限 58 个 Pod。但实际上，Daemonset（node-exporter、fluentd、karpenter 等）会占掉 8-10 个，真正可用的 Pod 槽位远少于理论值。

更大的问题是子网规划。如果初期给节点子网划了 /24（254 个 IP），加上节点本身的 IP，撑不了多少 Pod。我们有一套集群初期规划不足，后来迁移子网花了将近一周。

**IP 地址规划建议：**

- 节点子网至少 /22（1022 个 IP），大规模集群用 /20
- 如果 VPC 地址空间紧张，考虑开启 VPC CNI 的 `ENABLE_PREFIX_DELEGATION`，一个 ENI 可分配 /28 前缀（16 个 IP），大幅提升密度

```bash
# 检查节点当前 IP 使用情况
kubectl get node -o json | jq '.items[] | {name: .metadata.name, allocatable: .status.allocatable["vpc.amazonaws.com/pod-eni"]}'

# 开启前缀委派
kubectl set env daemonset aws-node -n kube-system ENABLE_PREFIX_DELEGATION=true
kubectl set env daemonset aws-node -n kube-system WARM_PREFIX_TARGET=1
```

**Cilium 适合什么场景：** 如果需要复杂的 L7 网络策略、eBPF 可观测性，或者想绕开 VPC CNI 的 IP 限制，Cilium 是合理选项。但它需要替换掉 kube-proxy，迁移成本高，而且与 AWS 原生 LB Controller 的集成需要额外配置。我们目前的生产集群没有做这个切换，仍用 VPC CNI + Security Group for Pods 的组合。

## IAM for Service Account（IRSA）

IRSA 是 EKS 上 Pod 访问 AWS 资源的推荐方式。原理是 EKS 集群有一个 OIDC Provider，Pod 的 Service Account 携带一个 OIDC token，AWS STS 验证这个 token 并换取临时凭证。

配置步骤很清晰，但有几个坑：

```bash
# 1. 确认集群已关联 OIDC Provider
aws eks describe-cluster --name my-cluster --query "cluster.identity.oidc.issuer" --output text

# 2. 创建 OIDC Provider（只需一次）
eksctl utils associate-iam-oidc-provider --cluster my-cluster --approve

# 3. 创建 IAM Role，Trust Policy 指向特定 SA
OIDC_PROVIDER=$(aws eks describe-cluster --name my-cluster \
  --query "cluster.identity.oidc.issuer" --output text | sed 's|https://||')

cat > trust-policy.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {
      "Federated": "arn:aws:iam::123456789012:oidc-provider/${OIDC_PROVIDER}"
    },
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": {
        "${OIDC_PROVIDER}:sub": "system:serviceaccount:production:my-app",
        "${OIDC_PROVIDER}:aud": "sts.amazonaws.com"
      }
    }
  }]
}
EOF
```

**常见踩坑：**

1. **Trust Policy 的 namespace 写错**：`system:serviceaccount:<namespace>:<sa-name>` 中的 namespace 必须与 Pod 实际运行的 namespace 一致，大小写敏感。
2. **Pod 启动后 SA 的 annotation 没生效**：annotation `eks.amazonaws.com/role-arn` 必须在 SA 上，不是 Pod 上。修改 SA 后已有的 Pod 不会自动更新 token，需要重启。
3. **跨账号 assume role**：如果需要访问另一个账号的资源，要在目标账号的 Role 上额外加信任，允许源账号的 IRSA Role 来 assume。

```yaml
# Service Account 配置示例
apiVersion: v1
kind: ServiceAccount
metadata:
  name: my-app
  namespace: production
  annotations:
    eks.amazonaws.com/role-arn: arn:aws:iam::123456789012:role/my-app-role
    eks.amazonaws.com/token-expiration: "86400"  # token 有效期，默认 86400s
```

## 节点组 vs Karpenter

这是 EKS 集群架构的核心决策之一。

**Managed Node Group** 的优势是稳定、AWS 负责底层生命周期管理、节点升级时 AWS 会自动做 drain。但它是静态的，你需要手动或通过 Cluster Autoscaler 来扩缩，而 Cluster Autoscaler 的扩容逻辑是"有 Pending Pod 才扩"，且每次只扩一个节点，速度慢。

**Karpenter** 的核心优势：
- 看 Pod 的实际资源需求选最合适的实例类型，而不是固定实例类型
- 支持 Consolidation，主动合并利用率低的节点
- 不依赖 ASG，直接调 EC2 API，扩容速度快很多

我们的做法是混用：核心基础设施组件（ArgoCD、监控、日志）放在 Managed Node Group 上保证稳定性，业务工作负载全部交给 Karpenter 管理。

```yaml
# Karpenter NodePool 示例
apiVersion: karpenter.sh/v1
kind: NodePool
metadata:
  name: general
spec:
  template:
    metadata:
      labels:
        workload-type: general
    spec:
      nodeClassRef:
        group: karpenter.k8s.aws
        kind: EC2NodeClass
        name: default
      requirements:
        - key: karpenter.sh/capacity-type
          operator: In
          values: ["spot", "on-demand"]
        - key: karpenter.k8s.aws/instance-category
          operator: In
          values: ["c", "m", "r"]
        - key: karpenter.k8s.aws/instance-generation
          operator: Gt
          values: ["4"]
      expireAfter: 720h  # 30天强制轮换节点
  disruption:
    consolidationPolicy: WhenEmptyOrUnderutilized
    consolidateAfter: 1m
  limits:
    cpu: "200"
    memory: 400Gi
```

## 多账号多集群访问管理

生产环境通常有 dev/staging/prod 多个账号，每个账号下可能还有多个集群（us-west-2、ap-southeast-1）。kubeconfig 管理如果不规范，很容易误操作到错误集群。

```bash
# 标准化添加集群到 kubeconfig，profile 对应 AWS 账号
aws eks update-kubeconfig \
  --region us-west-2 \
  --name prod-us \
  --alias prod-us \
  --profile prod-account

aws eks update-kubeconfig \
  --region ap-southeast-1 \
  --name prod-cn \
  --alias prod-cn \
  --profile prod-account

# 列出所有 context
kubectl config get-contexts

# 强制指定 context，避免依赖当前默认 context（在脚本里尤其重要）
kubectl --context=prod-us get nodes
```

**防止误操作的实践：**

在 `.zshrc` 里加一个 prompt 显示当前 context：

```bash
# 在 PS1 里加入 k8s context
parse_k8s() {
  kubectl config current-context 2>/dev/null | sed 's/.*\///'
}
export PS1='$(parse_k8s) $ '
```

另外，对于 prod 集群的写操作，我们用 `kubectl` 的 `--dry-run=server` 先验证，再执行。

## EKS 集群升级策略

EKS 每年发布约 3 个 K8s 小版本，每个版本支持 14 个月，到期前必须升级，否则会被强制升级。

**升级前检查清单：**

```bash
# 1. 检查 deprecated API（重要！K8s 1.25 移除了 PodSecurityPolicy）
kubectl get --raw /apis | jq '.groups[].preferredVersion.version' | sort -u

# 2. 用 pluto 扫描 deprecated API
pluto detect-all-in-cluster --target-versions k8s=v1.31.0

# 3. 检查 add-on 版本兼容性
aws eks describe-addon-versions --kubernetes-version 1.31 \
  --query 'addons[].{Name:addonName,Versions:addonVersions[0].addonVersion}'
```

**升级顺序：**

1. 升级 Control Plane（AWS 管理，约 10 分钟）
2. 升级 CoreDNS、kube-proxy、VPC CNI 等 managed add-ons
3. 升级节点（Managed Node Group 做滚动更新，或 Karpenter 通过 `expireAfter` 自然轮换）

对于生产集群，我们优先用 **blue-green 升级**：在同一个 VPC 里建新版本集群，迁移工作负载，而不是 in-place 升级。成本高一些，但风险可控，出问题可以立刻切回去。

## 安全加固

### Pod Security Admission

K8s 1.25 移除 PSP 后，PSA（Pod Security Admission）是内置替代方案。我们对不同 namespace 设置不同的安全级别：

```yaml
# namespace 标签控制 PSA 策略
apiVersion: v1
kind: Namespace
metadata:
  name: production
  labels:
    pod-security.kubernetes.io/enforce: restricted
    pod-security.kubernetes.io/enforce-version: latest
    pod-security.kubernetes.io/warn: restricted
    pod-security.kubernetes.io/audit: restricted
```

`restricted` 策略要求容器不能以 root 运行、不能挂载 hostPath、必须设置 securityContext。大部分业务应用改起来不难，麻烦的是一些老的基础设施组件（如某些日志收集器）需要 root，要单独给它们的 namespace 设 `privileged` 或 `baseline`。

### Network Policy

默认 EKS 集群 Pod 之间全通。Network Policy 是 namespace 级别的 L4 防火墙：

```yaml
# 只允许来自同 namespace 的流量，以及 monitoring namespace 的 Prometheus 抓取
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny-ingress
  namespace: production
spec:
  podSelector: {}
  policyTypes:
    - Ingress
  ingress:
    - from:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: production
    - from:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: monitoring
      ports:
        - protocol: TCP
          port: 8080  # metrics port
```

注意：VPC CNI 默认支持 Network Policy，但需要确认 `aws-node` daemonset 开启了 `NETWORK_POLICY_ENFORCING_MODE`。

## 成本优化

**Spot 实例混用策略：**

Karpenter 的 `capacity-type` 同时包含 `spot` 和 `on-demand`，Karpenter 会优先尝试 Spot。但有几点要注意：

- 有状态服务（如带本地 PV 的组件）不要跑 Spot
- 用 `topologySpreadConstraints` 分散 Pod，避免同一个节点被中断时影响过大
- Spot 中断前 2 分钟会有通知，Karpenter 会自动处理（drain + 起新节点）

```yaml
# Pod 配置 Spot 容忍
spec:
  tolerations:
    - key: karpenter.sh/capacity-type
      operator: Equal
      value: spot
      effect: NoSchedule
  topologySpreadConstraints:
    - maxSkew: 1
      topologyKey: topology.kubernetes.io/zone
      whenUnsatisfiable: DoNotSchedule
      labelSelector:
        matchLabels:
          app: my-app
```

**Savings Plans：** 对于 on-demand 节点，Compute Savings Plans 可以省 40-60%。按过去 3 个月的实际用量承诺基线，Peak 流量超出的部分走按需计费。我们买的是 1 年期 Compute Savings Plans，不绑定实例类型，灵活性最高。

实际下来，EKS 集群的 EC2 成本通过 Spot + Karpenter consolidation + Savings Plans 三管齐下，相比初期纯 on-demand 节点组的方案，降低了约 55%。

## 小结

EKS 生产化没有捷径，很多问题只有在规模上来之后才会暴露。IP 地址规划要在一开始做对，因为后期改很痛苦。IRSA 权限要最小化，每个服务一个专用 Role。升级要有 SOP，不要等到最后期限才动手。安全策略要分层，不要指望一个工具解决所有问题。
