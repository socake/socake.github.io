---
title: "AWS EKS 实战指南"
date: 2025-12-09T15:00:00+08:00
draft: false
tags: ["AWS", "EKS", "Kubernetes", "云原生"]
categories: ["Kubernetes"]
description: "AWS EKS 完整实战指南：从集群创建、节点组管理、IRSA 权限集成到升级流程和常见问题排查"
summary: "覆盖 EKS 核心架构、eksctl/aws cli 常用操作、IRSA 原理与配置、VPC CNI 网络限制、升级流程及常见故障排查。"
toc: true
math: false
diagram: false
keywords: ["EKS", "eksctl", "IRSA", "VPC CNI", "Kubernetes", "AWS"]
params:
  reading_time: true
---

## EKS 架构概述

EKS（Elastic Kubernetes Service）托管控制面，AWS 负责 etcd、API Server、Controller Manager、Scheduler 的高可用和版本维护，用户只需管理数据面（Worker Node）。

### 三种计算模式对比

| 模式 | 适用场景 | 成本模型 | 限制 |
|------|----------|----------|------|
| 托管节点组（Managed Node Group）| 通用工作负载 | EC2 按需/Spot | 需维护 AMI 版本 |
| 自管节点组（Self-managed）| 需要定制内核/AMI | EC2 | 完全自管升级 |
| Fargate | 无状态、短生命周期 | vCPU+内存按用计费 | 不支持 DaemonSet、HostPath |

Fargate 最大的坑：每个 Pod 独占一个 micro VM，调度延迟较高，且无法运行需要宿主机挂载的 DaemonSet（如 Fluent Bit node-level 采集）。

---

## eksctl 常用操作

### 创建集群

```bash
eksctl create cluster \
  --name prod-cluster \
  --region us-west-2 \
  --version 1.30 \
  --nodegroup-name standard-workers \
  --node-type m5.xlarge \
  --nodes 3 \
  --nodes-min 2 \
  --nodes-max 10 \
  --managed \
  --with-oidc \
  --ssh-access \
  --ssh-public-key ~/.ssh/id_rsa.pub
```

`--with-oidc` 是关键参数，不加的话后续 IRSA 无法用。

### 用配置文件创建（推荐生产环境）

```yaml
# cluster.yaml
apiVersion: eksctl.io/v1alpha5
kind: ClusterConfig

metadata:
  name: prod-cluster
  region: us-west-2
  version: "1.30"

iam:
  withOIDC: true

managedNodeGroups:
  - name: ng-general
    instanceType: m5.xlarge
    minSize: 2
    maxSize: 20
    desiredCapacity: 3
    volumeSize: 100
    volumeType: gp3
    privateNetworking: true
    labels:
      role: general
    tags:
      k8s.io/cluster-autoscaler/enabled: "true"
      k8s.io/cluster-autoscaler/prod-cluster: "owned"
    iam:
      attachPolicyARNs:
        - arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy
        - arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy
        - arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly
```

```bash
eksctl create cluster -f cluster.yaml
```

### 节点组扩缩

```bash
# 手动调整节点数
eksctl scale nodegroup \
  --cluster prod-cluster \
  --name ng-general \
  --nodes 5 \
  --nodes-min 2 \
  --nodes-max 20

# 查看节点组状态
eksctl get nodegroup --cluster prod-cluster

# 滚动更新节点组（AMI 更新后）
eksctl upgrade nodegroup \
  --cluster prod-cluster \
  --name ng-general \
  --force-upgrade
```

### 删除节点组

```bash
# 先 drain 节点，再删除
eksctl delete nodegroup \
  --cluster prod-cluster \
  --name ng-old \
  --drain
```

---

## aws cli 操作 EKS

### 更新 kubeconfig

```bash
# 更新本地 kubeconfig
aws eks update-kubeconfig \
  --name prod-cluster \
  --region us-west-2

# 指定 profile
aws eks update-kubeconfig \
  --name prod-cluster \
  --region us-west-2 \
  --profile prod-account

# 重命名 context
aws eks update-kubeconfig \
  --name prod-cluster \
  --region us-west-2 \
  --alias eks-prod
```

### 获取 token（调试用）

```bash
# 获取临时 token（有效期 15 分钟）
aws eks get-token --cluster-name prod-cluster

# 查看集群信息
aws eks describe-cluster --name prod-cluster --region us-west-2

# 列出所有集群
aws eks list-clusters --region us-west-2
```

---

## EKS Add-on 管理

EKS Add-on 是 AWS 托管的核心组件，支持独立版本管理，不跟集群版本强绑定。

### 核心 Add-on

```bash
# 查看已安装的 add-on
aws eks list-addons --cluster-name prod-cluster

# 查看某个 add-on 的支持版本
aws eks describe-addon-versions \
  --addon-name vpc-cni \
  --kubernetes-version 1.30

# 创建/更新 add-on
aws eks create-addon \
  --cluster-name prod-cluster \
  --addon-name vpc-cni \
  --addon-version v1.18.1-eksbuild.3 \
  --resolve-conflicts OVERWRITE

# 更新到最新版本
aws eks update-addon \
  --cluster-name prod-cluster \
  --addon-name coredns \
  --addon-version v1.11.1-eksbuild.9 \
  --resolve-conflicts PRESERVE
```

### 常用 Add-on 版本对应关系（K8s 1.30）

| Add-on | 推荐版本 | 说明 |
|--------|----------|------|
| vpc-cni | v1.18.x | ENI/IP 分配 |
| coredns | v1.11.x | 集群 DNS |
| kube-proxy | v1.30.x | iptables/ipvs 规则 |
| aws-ebs-csi-driver | v1.35.x | EBS 存储 |
| aws-efs-csi-driver | v2.0.x | EFS 共享存储 |

---

## IRSA：IAM Roles for Service Accounts

### 原理

IRSA 通过 OIDC 联合身份实现 Pod 级别的 AWS 权限，无需在 EC2 上绑定大权限 Instance Profile。

流程：
1. EKS 集群暴露 OIDC Issuer URL
2. 创建 IAM Role，信任策略引用该 OIDC Provider
3. K8s ServiceAccount 通过 annotation 绑定 IAM Role ARN
4. Pod 启动时 EKS Pod Identity webhook 注入临时凭据（`AWS_WEB_IDENTITY_TOKEN_FILE`）
5. AWS SDK 自动读取 token 并调用 STS 换取临时 AK/SK

### 配置步骤

```bash
# 1. 确认 OIDC provider 已创建
aws iam list-open-id-connect-providers

# 2. 获取集群 OIDC issuer
OIDC_URL=$(aws eks describe-cluster \
  --name prod-cluster \
  --query "cluster.identity.oidc.issuer" \
  --output text)
echo $OIDC_URL
# 输出类似: https://oidc.eks.us-west-2.amazonaws.com/id/EXAMPLED539D4633E53DE1B71EXAMPLE

# 3. 用 eksctl 快速创建 IRSA
eksctl create iamserviceaccount \
  --cluster prod-cluster \
  --namespace default \
  --name my-service-sa \
  --attach-policy-arn arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess \
  --approve
```

### 手动创建 IAM Role 信任策略

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::123456789012:oidc-provider/oidc.eks.us-west-2.amazonaws.com/id/EXAMPLED539D4633E53DE1B71EXAMPLE"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "oidc.eks.us-west-2.amazonaws.com/id/EXAMPLED539D4633E53DE1B71EXAMPLE:sub": "system:serviceaccount:default:my-service-sa",
          "oidc.eks.us-west-2.amazonaws.com/id/EXAMPLED539D4633E53DE1B71EXAMPLE:aud": "sts.amazonaws.com"
        }
      }
    }
  ]
}
```

### K8s 侧配置

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: my-service-sa
  namespace: default
  annotations:
    eks.amazonaws.com/role-arn: arn:aws:iam::123456789012:role/my-irsa-role
```

Pod 引用该 ServiceAccount 后，会自动注入：

```yaml
env:
- name: AWS_WEB_IDENTITY_TOKEN_FILE
  value: /var/run/secrets/eks.amazonaws.com/serviceaccount/token
- name: AWS_ROLE_ARN
  value: arn:aws:iam::123456789012:role/my-irsa-role
```

---

## 网络：VPC CNI 与 IP 限制

### ENI IP 数量限制

EKS 默认使用 VPC CNI，每个 Pod 占用一个 VPC IP。每种 EC2 实例类型的 ENI 数量和每个 ENI 的 IP 数量有上限：

```
最大 Pod 数 = (ENI 数量 × 每 ENI IP 数) - 1
```

常见实例型的限制：

| 实例类型 | 最大 ENI | 每 ENI IP | 最大 Pod 数 |
|----------|----------|-----------|-------------|
| t3.medium | 3 | 6 | 17 |
| m5.large | 3 | 10 | 29 |
| m5.xlarge | 4 | 15 | 58 |
| m5.4xlarge | 8 | 30 | 234 |

### 突破 IP 限制：prefix delegation

启用 prefix delegation 后每个 ENI 可以分配 /28 前缀（16 个 IP），大幅提升 Pod 密度：

```bash
# 启用 prefix delegation
kubectl set env daemonset aws-node \
  -n kube-system \
  ENABLE_PREFIX_DELEGATION=true \
  WARM_PREFIX_TARGET=1
```

---

## EKS 升级流程

升级顺序：控制面 → Add-on → 节点组，不能跨版本跳升。

```bash
# 1. 升级控制面
aws eks update-cluster-version \
  --name prod-cluster \
  --kubernetes-version 1.31

# 等待升级完成
aws eks wait cluster-active --name prod-cluster

# 2. 升级 add-on
aws eks update-addon \
  --cluster-name prod-cluster \
  --addon-name vpc-cni \
  --addon-version v1.19.0-eksbuild.1

# 3. 升级节点组（触发滚动替换）
eksctl upgrade nodegroup \
  --cluster prod-cluster \
  --name ng-general \
  --kubernetes-version 1.31
```

---

## 常见问题排查

### 节点加入失败

```bash
# 查看节点状态
kubectl get nodes
kubectl describe node <node-name>

# 检查 bootstrap 日志（在节点上）
sudo cat /var/log/cloud-init-output.log
sudo journalctl -u kubelet -f

# 常见原因：
# 1. 安全组没有开放 443 到控制面
# 2. aws-auth ConfigMap 没有添加节点组 Role
kubectl edit configmap aws-auth -n kube-system
```

aws-auth 正确格式：

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: aws-auth
  namespace: kube-system
data:
  mapRoles: |
    - rolearn: arn:aws:iam::123456789012:role/eksctl-prod-cluster-NodeInstanceRole
      username: system:node:{{EC2PrivateDNSName}}
      groups:
        - system:bootstrappers
        - system:nodes
```

### IRSA 权限问题排查

```bash
# 在 Pod 内验证身份
kubectl exec -it <pod> -- aws sts get-caller-identity

# 预期输出
{
    "UserId": "AROA...:botocore-session-...",
    "Account": "123456789012",
    "Arn": "arn:aws:sts::123456789012:assumed-role/my-irsa-role/botocore-session-..."
}

# 检查 token 文件是否注入
kubectl exec -it <pod> -- env | grep AWS
# 应该看到 AWS_ROLE_ARN 和 AWS_WEB_IDENTITY_TOKEN_FILE

# 常见原因：
# 1. ServiceAccount annotation 拼错 role ARN
# 2. IAM 信任策略中 sub 条件与实际 namespace/sa 名称不匹配
# 3. Pod 没有引用正确的 ServiceAccount
```

### Pod 调度到错误节点组

```bash
# 给节点组打 taint 隔离工作负载
kubectl taint nodes -l role=gpu dedicated=gpu:NoSchedule

# Pod 侧加 toleration
tolerations:
- key: "dedicated"
  operator: "Equal"
  value: "gpu"
  effect: "NoSchedule"
nodeSelector:
  role: gpu
```
