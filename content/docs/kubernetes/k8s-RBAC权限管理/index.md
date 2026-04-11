---
title: "Kubernetes RBAC 权限管理实践"
date: 2025-12-09T10:00:00+08:00
draft: false
tags: ["Kubernetes", "RBAC", "安全", "权限", "运维"]
categories: ["Kubernetes"]
description: "系统讲解 Kubernetes RBAC 四大核心资源（Role/ClusterRole/RoleBinding/ClusterRoleBinding）、Subject 类型、ServiceAccount 最小权限实践、多租户命名空间隔离及常见 403 权限错误排查。"
summary: "从 RBAC 核心概念到生产级多租户权限设计，涵盖 ServiceAccount 最小权限、kubectl auth can-i 排查和命名空间隔离实践。"
toc: true
math: false
diagram: false
keywords: ["Kubernetes", "RBAC", "Role", "ClusterRole", "ServiceAccount", "权限管理", "多租户", "安全"]
params:
  reading_time: true
---

## RBAC 核心概念

Kubernetes RBAC（基于角色的访问控制）由四种资源组成：

```
Subject（主体）          Role/ClusterRole（角色）      API Resources（资源）
┌───────────────┐        ┌──────────────────────┐      ┌──────────────────┐
│ User          │        │ Role（命名空间级）     │      │ pods             │
│ Group         │──绑定──►│   rules:             │──允许►│ deployments      │
│ ServiceAccount│        │   - verbs: [get,list] │      │ services         │
└───────────────┘        │   - resources: [pods] │      │ configmaps       │
                         ├──────────────────────┤      │ secrets          │
        RoleBinding       │ ClusterRole（集群级） │      └──────────────────┘
        ClusterRoleBinding│   rules: ...         │
                         └──────────────────────┘
```

| 资源 | 作用域 | 说明 |
|------|--------|------|
| **Role** | Namespace | 定义命名空间内的权限规则 |
| **ClusterRole** | Cluster | 定义集群范围权限，或可复用的规则 |
| **RoleBinding** | Namespace | 将 Role 或 ClusterRole 绑定到 Namespace 内的 Subject |
| **ClusterRoleBinding** | Cluster | 将 ClusterRole 绑定到集群范围的 Subject |

---

## Subject 类型

```yaml
# 三种 Subject 写法示例
subjects:
  # 1. ServiceAccount（推荐，机器身份）
  - kind: ServiceAccount
    name: my-service-account
    namespace: production

  # 2. User（人类用户，需外部 IdP 或证书颁发）
  - kind: User
    name: "jane@company.com"
    apiGroup: rbac.authorization.k8s.io

  # 3. Group（用户组）
  - kind: Group
    name: "developers"
    apiGroup: rbac.authorization.k8s.io
```

---

## 内置 ClusterRole

Kubernetes 预置了几个常用的 ClusterRole：

| ClusterRole | 权限范围 | 适用人员 |
|-------------|----------|----------|
| **cluster-admin** | 完全控制所有资源，包括 RBAC 自身 | 集群管理员 |
| **admin** | 命名空间内几乎所有资源（含 RBAC） | 项目负责人 |
| **edit** | 命名空间内大多数资源读写，不含 RBAC | 开发者 |
| **view** | 命名空间内大多数资源只读，不含 Secret | 只读用户 |

```bash
# 查看内置 ClusterRole 的具体权限
kubectl describe clusterrole admin
kubectl describe clusterrole edit
kubectl describe clusterrole view

# 给用户 jane 在 production 命名空间赋 edit 权限
kubectl create rolebinding jane-edit \
  --clusterrole=edit \
  --user=jane@company.com \
  --namespace=production
```

---

## 自定义 Role 示例

### 只读 Pod 权限

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: pod-reader
  namespace: production
rules:
  - apiGroups: [""]          # "" 表示 core API group
    resources: ["pods", "pods/log", "pods/status"]
    verbs: ["get", "list", "watch"]
```

### 管理 Deployment（不含删除）

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: deployment-manager
  namespace: production
rules:
  - apiGroups: ["apps"]
    resources: ["deployments", "replicasets"]
    verbs: ["get", "list", "watch", "create", "update", "patch"]
    # 注意：不含 "delete"
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["get", "list", "watch"]
  - apiGroups: [""]
    resources: ["pods/exec"]       # 允许 kubectl exec
    verbs: ["create"]
  - apiGroups: [""]
    resources: ["pods/log"]        # 允许 kubectl logs
    verbs: ["get", "list"]
```

### 查看日志专用角色

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: log-viewer
  namespace: production
rules:
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["get", "list", "watch"]
  - apiGroups: [""]
    resources: ["pods/log"]
    verbs: ["get"]
  - apiGroups: ["apps"]
    resources: ["deployments", "statefulsets", "daemonsets"]
    verbs: ["get", "list", "watch"]

---
# 绑定给运维团队 Group
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: log-viewer-binding
  namespace: production
subjects:
  - kind: Group
    name: "ops-team"
    apiGroup: rbac.authorization.k8s.io
roleRef:
  kind: Role
  name: log-viewer
  apiGroup: rbac.authorization.k8s.io
```

### 针对特定资源实例的权限

```yaml
# 只允许访问名为 "app-config" 的 ConfigMap
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: specific-configmap-reader
  namespace: production
rules:
  - apiGroups: [""]
    resources: ["configmaps"]
    resourceNames: ["app-config", "feature-flags"]  # 限定资源名
    verbs: ["get"]
```

---

## ServiceAccount 最佳实践

### 最小权限原则

```yaml
# 1. 为每个应用创建独立 ServiceAccount
apiVersion: v1
kind: ServiceAccount
metadata:
  name: payment-service
  namespace: production
  annotations:
    # AWS IRSA：绑定 IAM Role（推荐替代 Node 实例角色）
    eks.amazonaws.com/role-arn: "arn:aws:iam::123456789:role/payment-service-role"

---
# 2. 创建最小权限 Role（仅业务需要的权限）
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: payment-service-role
  namespace: production
rules:
  - apiGroups: [""]
    resources: ["configmaps"]
    resourceNames: ["payment-config"]
    verbs: ["get"]
  - apiGroups: [""]
    resources: ["secrets"]
    resourceNames: ["payment-credentials"]
    verbs: ["get"]

---
# 3. 绑定
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: payment-service-binding
  namespace: production
subjects:
  - kind: ServiceAccount
    name: payment-service
    namespace: production
roleRef:
  kind: Role
  name: payment-service-role
  apiGroup: rbac.authorization.k8s.io

---
# 4. Pod 引用 ServiceAccount
apiVersion: apps/v1
kind: Deployment
metadata:
  name: payment-service
  namespace: production
spec:
  template:
    spec:
      serviceAccountName: payment-service   # 指定 SA
      automountServiceAccountToken: true    # 默认 true，不需要时设为 false
      containers:
        - name: app
          image: payment-service:v1.2.0
```

### 禁用自动挂载 Token（对不需要访问 API 的 Pod）

```yaml
# SA 级别禁用
apiVersion: v1
kind: ServiceAccount
metadata:
  name: stateless-app
  namespace: production
automountServiceAccountToken: false  # SA 下所有 Pod 不自动挂载

---
# Pod 级别覆盖
spec:
  automountServiceAccountToken: false  # 单个 Pod 设置
```

---

## 多租户场景：Namespace 隔离

### 完整多租户配置

```bash
# 1. 创建团队命名空间
kubectl create namespace team-alpha
kubectl create namespace team-beta

# 2. 打标签（用于 NetworkPolicy 选择器）
kubectl label namespace team-alpha team=alpha
kubectl label namespace team-beta team=beta
```

```yaml
# 3. ResourceQuota 限制资源总量
apiVersion: v1
kind: ResourceQuota
metadata:
  name: team-alpha-quota
  namespace: team-alpha
spec:
  hard:
    requests.cpu: "20"
    requests.memory: "40Gi"
    limits.cpu: "40"
    limits.memory: "80Gi"
    pods: "50"
    services: "20"
    persistentvolumeclaims: "10"
    secrets: "20"
    configmaps: "20"

---
# 4. LimitRange 设置默认资源限制
apiVersion: v1
kind: LimitRange
metadata:
  name: team-alpha-limits
  namespace: team-alpha
spec:
  limits:
    - type: Container
      default:
        cpu: "500m"
        memory: "512Mi"
      defaultRequest:
        cpu: "100m"
        memory: "128Mi"
      max:
        cpu: "4"
        memory: "8Gi"
    - type: PersistentVolumeClaim
      max:
        storage: "50Gi"

---
# 5. 给团队 admin 赋命名空间内 admin 权限
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: team-alpha-admin
  namespace: team-alpha
subjects:
  - kind: Group
    name: "team-alpha-admins"
    apiGroup: rbac.authorization.k8s.io
roleRef:
  kind: ClusterRole
  name: admin       # 使用内置 ClusterRole
  apiGroup: rbac.authorization.k8s.io

---
# 6. 网络隔离：只允许命名空间内通信 + 监控系统访问
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny-ingress
  namespace: team-alpha
spec:
  podSelector: {}       # 选中所有 Pod
  policyTypes:
    - Ingress
  ingress:
    - from:
        - namespaceSelector:
            matchLabels:
              team: alpha   # 允许同命名空间
    - from:
        - namespaceSelector:
            matchLabels:
              name: monitoring  # 允许 Prometheus 抓取
```

---

## kubectl auth can-i 排查权限

```bash
# 检查当前用户权限
kubectl auth can-i create deployments --namespace production
kubectl auth can-i delete pods --namespace production
kubectl auth can-i "*" "*"  # 是否有全部权限

# 检查指定 ServiceAccount 权限
kubectl auth can-i list pods \
  --namespace production \
  --as system:serviceaccount:production:payment-service

# 检查指定用户权限
kubectl auth can-i get secrets \
  --namespace production \
  --as jane@company.com

# 列出当前用户在命名空间内的所有权限
kubectl auth can-i --list --namespace production

# 检查 Group 权限
kubectl auth can-i list pods \
  --as-group=team-alpha-admins \
  --as=fake-user \
  --namespace team-alpha
```

---

## 常见权限错误排查

### 403 Forbidden 分析

```bash
# 错误示例：
# Error from server (Forbidden): pods is forbidden:
# User "system:serviceaccount:production:payment-service"
# cannot list resource "pods" in API group "" in the namespace "production"

# 排查步骤：

# 1. 确认 SA 是否存在
kubectl get serviceaccount payment-service -n production

# 2. 检查 SA 绑定的 RoleBinding/ClusterRoleBinding
kubectl get rolebindings,clusterrolebindings -A -o wide | grep payment-service

# 3. 查看具体 Role 的权限规则
kubectl describe role payment-service-role -n production

# 4. 用 auth can-i 直接验证
kubectl auth can-i list pods \
  --namespace production \
  --as system:serviceaccount:production:payment-service

# 5. 检查是否有 Admission Webhook 拦截（如 OPA/Kyverno）
kubectl get validatingwebhookconfigurations
kubectl get mutatingwebhookconfigurations
```

### RBAC 审计日志分析

```bash
# 在 API Server 审计日志中找权限拒绝事件
# 审计日志路径（通常在 /var/log/kubernetes/audit.log）
grep '"verb":".*".*"user":.*payment-service.*"code":403' /var/log/kubernetes/audit.log | jq .

# 或通过 kubectl 查看 RBAC 相关事件
kubectl get events --field-selector reason=Forbidden -A

# 查看 kube-apiserver 日志中的权限拒绝
kubectl -n kube-system logs kube-apiserver-<node> | grep "RBAC DENY"
```

### 常见误区

```bash
# 误区1：ClusterRoleBinding 绑定了 ClusterRole，但 Role 是命名空间级别
# ClusterRoleBinding → ClusterRole = 集群范围生效
# RoleBinding → ClusterRole = 只在绑定的命名空间生效（常用于复用规则）

# 误区2：aggregationRule 聚合 ClusterRole
kubectl describe clusterrole admin | grep -A5 "AggregationRule"
# admin 是聚合角色，通过标签自动聚合子 Role

# 误区3：默认 SA 权限
# default ServiceAccount 默认无权限（K8s 1.24+ 不再自动挂载 Token）
kubectl get clusterrolebinding | grep default  # 确认 default SA 没有被误授权
```

---

## 生产 RBAC 配置速查

```bash
# 快速创建常用绑定
# 给 SA 赋予只读权限
kubectl create rolebinding <name> \
  --clusterrole=view \
  --serviceaccount=<namespace>:<sa-name> \
  --namespace=<namespace>

# 给用户赋 edit 权限
kubectl create rolebinding <name> \
  --clusterrole=edit \
  --user=<user> \
  --namespace=<namespace>

# 导出命名空间所有 RBAC 配置
kubectl get roles,rolebindings,clusterroles,clusterrolebindings \
  -n production -o yaml > production-rbac-backup.yaml

# 查找所有有 cluster-admin 权限的绑定（安全审计）
kubectl get clusterrolebindings -o json | \
  jq '.items[] | select(.roleRef.name=="cluster-admin") | {name:.metadata.name, subjects:.subjects}'
```
