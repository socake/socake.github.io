---
title: "Kubernetes RBAC 安全加固实战：最小权限到 NetworkPolicy"
date: 2026-04-12T11:00:00+08:00
draft: false
tags: ["Kubernetes", "RBAC", "安全", "NetworkPolicy", "ServiceAccount"]
categories: ["Kubernetes"]
description: "K8s RBAC 生产加固实战：ServiceAccount 最小权限、审计日志分析、NetworkPolicy 命名空间隔离"
summary: "从真实安全事件出发，系统讲解 Kubernetes RBAC 最小权限设计、ClusterRole 与 Role 的适用场景、审计日志分析 RBAC 问题的方法，以及 NetworkPolicy 实现命名空间和 Pod 级别的网络隔离。"
toc: true
math: false
diagram: false
keywords: ["Kubernetes", "RBAC", "NetworkPolicy", "ServiceAccount", "安全加固", "审计日志"]
params:
  reading_time: true
---

K8s 安全问题不是抽象的——我见过因为 default ServiceAccount 被滥用导致的集群沦陷，也见过通配符权限让一个测试 Pod 能操作生产数据库的 Secret。这篇文章从实际踩坑出发，系统梳理 RBAC 和网络策略的正确做法。

## ServiceAccount 最小权限原则

K8s 中每个 Pod 默认使用 `default` ServiceAccount，这个 SA 在很多集群里被赋予了过大的权限。正确做法是：**每个应用创建独立的 ServiceAccount，只授予它实际需要的权限。**

**问题示例：滥用 default ServiceAccount**

```bash
# 在 Pod 内部就能列出所有 Secret（这很危险）
kubectl exec -n my-app pod/my-service-xxx -- \
  curl -s -H "Authorization: Bearer $(cat /var/run/secrets/kubernetes.io/serviceaccount/token)" \
  https://kubernetes.default.svc/api/v1/namespaces/my-app/secrets \
  --cacert /var/run/secrets/kubernetes.io/serviceaccount/ca.crt
```

**正确做法：为应用创建专属 SA**

```yaml
# 1. 创建 ServiceAccount
apiVersion: v1
kind: ServiceAccount
metadata:
  name: my-service-sa
  namespace: my-app
automountServiceAccountToken: false  # 不需要调用 K8s API 的应用，直接禁用

---
# 2. 如果需要调用 K8s API，精确定义所需权限
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: my-service-role
  namespace: my-app
rules:
  # 只允许读取本命名空间的 ConfigMap，不允许 Secret
  - apiGroups: [""]
    resources: ["configmaps"]
    verbs: ["get", "list", "watch"]
  # 只允许读取特定名称的 Secret
  - apiGroups: [""]
    resources: ["secrets"]
    resourceNames: ["my-service-config"]  # 限制到具体资源名
    verbs: ["get"]

---
# 3. 绑定
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: my-service-rolebinding
  namespace: my-app
subjects:
  - kind: ServiceAccount
    name: my-service-sa
    namespace: my-app
roleRef:
  kind: Role
  apiGroupp: rbac.authorization.k8s.io
  name: my-service-role

---
# 4. Deployment 中指定 SA
apiVersion: apps/v1
kind: Deployment
spec:
  template:
    spec:
      serviceAccountName: my-service-sa
      automountServiceAccountToken: true  # Deployment 层面控制
```

**验证权限是否符合预期：**

```bash
# 检查某个 SA 能否执行特定操作
kubectl auth can-i get secrets \
  --as=system:serviceaccount:my-app:my-service-sa \
  -n my-app
# no

kubectl auth can-i get configmaps \
  --as=system:serviceaccount:my-app:my-service-sa \
  -n my-app
# yes

# 列出某个 SA 的所有权限
kubectl auth can-i --list \
  --as=system:serviceaccount:my-app:my-service-sa \
  -n my-app
```

## ClusterRole vs Role：正确区分使用场景

这是我看到最多被混用的地方：

| 类型 | 范围 | 适用场景 |
|------|------|----------|
| Role | 单个命名空间 | 应用级权限，如读取本 namespace 的 ConfigMap |
| ClusterRole | 全集群 | 集群级资源（Node、PV、StorageClass）或跨 namespace 复用 |
| RoleBinding | 绑定到单 namespace | 把 Role 或 ClusterRole 限定在某个 namespace 内生效 |
| ClusterRoleBinding | 全集群范围生效 | 把 ClusterRole 在全集群范围授权 |

**常见错误：用 ClusterRoleBinding 绑定 ClusterRole，却以为只有某个 namespace 生效**

```yaml
# 错误：这给了 SA 全集群的 Pod 读取权限
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: pod-reader-binding
subjects:
  - kind: ServiceAccount
    name: my-sa
    namespace: my-app
roleRef:
  kind: ClusterRole
  name: pod-reader
  apiGroup: rbac.authorization.k8s.io

# 正确：用 RoleBinding 绑定 ClusterRole，范围限定在 my-app namespace
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: pod-reader-binding
  namespace: my-app  # 关键：这里限定了范围
subjects:
  - kind: ServiceAccount
    name: my-sa
    namespace: my-app
roleRef:
  kind: ClusterRole  # 可以引用 ClusterRole
  name: pod-reader
  apiGroup: rbac.authorization.k8s.io
```

**什么时候真的需要 ClusterRole + ClusterRoleBinding：**

```yaml
# 监控组件需要读取所有命名空间的 Pod 信息
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: prometheus-scraper
rules:
  - apiGroups: [""]
    resources: ["nodes", "pods", "services", "endpoints"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["extensions", "networking.k8s.io"]
    resources: ["ingresses"]
    verbs: ["get", "list", "watch"]
  - nonResourceURLs: ["/metrics"]
    verbs: ["get"]
```

## 审计日志：分析 RBAC 问题

K8s 审计日志是排查权限问题的利器。先确认集群开启了审计日志：

```yaml
# kube-apiserver 启动参数
--audit-log-path=/var/log/kubernetes/audit.log
--audit-log-maxage=30
--audit-log-maxbackup=10
--audit-log-maxsize=100
--audit-policy-file=/etc/kubernetes/audit-policy.yaml
```

**审计策略配置（记录关键操作）：**

```yaml
# /etc/kubernetes/audit-policy.yaml
apiVersion: audit.k8s.io/v1
kind: Policy
rules:
  # 记录所有 secrets 的访问（只记录 metadata，不记录内容）
  - level: Metadata
    resources:
      - group: ""
        resources: ["secrets"]

  # 记录所有写操作（create/update/delete/patch）
  - level: RequestResponse
    verbs: ["create", "update", "delete", "patch"]
    resources:
      - group: ""
        resources: ["pods", "services", "configmaps"]

  # 记录所有 RBAC 变更
  - level: RequestResponse
    resources:
      - group: "rbac.authorization.k8s.io"
        resources: ["roles", "clusterroles", "rolebindings", "clusterrolebindings"]

  # 忽略健康检查噪音
  - level: None
    users: ["system:kube-proxy"]
    verbs: ["watch"]
    resources:
      - group: ""
        resources: ["endpoints", "services"]

  # 默认记录 Metadata 级别
  - level: Metadata
```

**分析审计日志，找出 RBAC 拒绝事件：**

```bash
# 查找所有 RBAC 拒绝（Forbidden）
grep '"code":403' /var/log/kubernetes/audit.log | \
  jq '{time: .requestReceivedTimestamp, user: .user.username, verb: .verb, resource: .objectRef.resource, namespace: .objectRef.namespace}' | \
  head -50

# 找出某个 SA 的被拒绝操作
grep '"system:serviceaccount:my-app:my-service-sa"' /var/log/kubernetes/audit.log | \
  grep '"code":403' | \
  jq '{verb: .verb, resource: .objectRef.resource}'

# 统计拒绝最多的资源访问
grep '"code":403' /var/log/kubernetes/audit.log | \
  jq -r '[.user.username, .verb, .objectRef.resource] | join(" ")' | \
  sort | uniq -c | sort -rn | head -20
```

**使用 kubectl-who-can 插件快速排查：**

```bash
# 安装
kubectl krew install who-can

# 查看谁能 delete pods
kubectl who-can delete pods -n my-app

# 查看谁能读 secrets
kubectl who-can get secrets -n my-app
```

## NetworkPolicy：网络层隔离

RBAC 控制的是 K8s API 访问权限，NetworkPolicy 控制的是 Pod 之间的网络连通性。两者都要配。

**默认拒绝所有入站流量（推荐在敏感命名空间使用）：**

```yaml
# 先封锁所有入站，再按需开放
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny-ingress
  namespace: production
spec:
  podSelector: {}  # 选择所有 Pod
  policyTypes:
    - Ingress
```

**命名空间级别隔离：只允许同 namespace 内的 Pod 互相访问**

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-same-namespace
  namespace: my-app
spec:
  podSelector: {}
  policyTypes:
    - Ingress
  ingress:
    - from:
        - podSelector: {}  # 同 namespace 内任意 Pod
```

**只允许特定来源访问数据库 Pod：**

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: postgres-access
  namespace: data
spec:
  podSelector:
    matchLabels:
      app: postgresql
  policyTypes:
    - Ingress
  ingress:
    # 只允许 my-app namespace 中带 app=my-service 标签的 Pod 访问
    - from:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: my-app
          podSelector:
            matchLabels:
              app: my-service
      ports:
        - protocol: TCP
          port: 5432
```

注意：`namespaceSelector` 和 `podSelector` 写在同一个 `from` 列表项中时是 **AND** 关系（同时满足）；写在不同列表项时是 **OR** 关系。

```yaml
# AND：来自 my-app namespace 且带有 app=my-service 标签的 Pod
ingress:
  - from:
      - namespaceSelector:
          matchLabels:
            kubernetes.io/metadata.name: my-app
        podSelector:         # 注意：同一个 from item，是 AND
          matchLabels:
            app: my-service

# OR：来自 my-app namespace 的任意 Pod，或者带有 app=my-service 的任意 Pod
ingress:
  - from:
      - namespaceSelector:   # 独立的 from item，是 OR
          matchLabels:
            kubernetes.io/metadata.name: my-app
      - podSelector:         # 独立的 from item，是 OR
          matchLabels:
            app: my-service
```

**允许 Prometheus 从任意 namespace 抓取 metrics：**

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-prometheus-scrape
  namespace: my-app
spec:
  podSelector:
    matchLabels:
      app: my-service
  policyTypes:
    - Ingress
  ingress:
    - from:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: monitoring
          podSelector:
            matchLabels:
              app: prometheus
      ports:
        - protocol: TCP
          port: 9090
```

**出站限制（Egress）：禁止 Pod 访问外部，只允许访问集群内服务**

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: restrict-egress
  namespace: my-app
spec:
  podSelector:
    matchLabels:
      app: my-service
  policyTypes:
    - Egress
  egress:
    # 允许 DNS 解析
    - ports:
        - protocol: UDP
          port: 53
        - protocol: TCP
          port: 53
    # 允许访问同 namespace 内的服务
    - to:
        - podSelector: {}
    # 允许访问 data namespace 的 postgresql
    - to:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: data
      ports:
        - protocol: TCP
          port: 5432
```

## 常见误区

### 误区1：default ServiceAccount 权限"应该没问题"

Helm chart 默认创建的 RBAC 规则有时候很宽松。`helm install` 某些 chart 后，会自动创建有较大权限的 ClusterRole，务必检查：

```bash
# 查看所有 ClusterRoleBinding，找出绑定了高权限角色的
kubectl get clusterrolebindings -o json | \
  jq '.items[] | select(.roleRef.name == "cluster-admin") | .subjects'
```

### 误区2：通配符权限"方便测试"

```yaml
# 绝对不要在生产出现这种规则
rules:
  - apiGroups: ["*"]
    resources: ["*"]
    verbs: ["*"]
```

即使是临时的调试账号，也应该限定时间和范围。

### 误区3：NetworkPolicy 创建了但不生效

NetworkPolicy 需要网络插件（CNI）支持才能生效。Flannel 不支持 NetworkPolicy，需要使用 Calico、Cilium、Weave 等。

```bash
# 检查 CNI 是否支持 NetworkPolicy
kubectl get pods -n kube-system | grep -E "calico|cilium|weave"

# 测试 NetworkPolicy 是否真的生效
kubectl run test-client --image=busybox -n other-namespace --rm -it -- \
  wget -qO- --timeout=3 http://my-service.my-app.svc.cluster.local
```

### 误区4：RBAC 权限审计只看当前状态

权限配置是会随时间累积的，新功能需要新权限，但旧权限很少被及时清理。建议定期跑一次权限审计：

```bash
# 找出所有有 secrets 读取权限的 SA
kubectl get rolebindings,clusterrolebindings -A -o json | \
  jq '.items[] | select(.roleRef.kind == "ClusterRole") | 
      {name: .metadata.name, namespace: .metadata.namespace, subjects: .subjects}'
```

## 总结

K8s 安全加固是一个持续迭代的过程，不是一次性的配置工作：

1. **ServiceAccount 最小权限**：新服务上线时就定好，不要等出了问题再收紧
2. **ClusterRole 慎用**：能用 Role 解决的不用 ClusterRole，能用 RoleBinding 限定范围的不用 ClusterRoleBinding
3. **审计日志要开**：403 错误是最好的 RBAC 调试工具，也是安全事件的重要证据
4. **NetworkPolicy 与 RBAC 互补**：RBAC 控制 API 访问，NetworkPolicy 控制网络访问，两者不能互相替代
5. **定期权限审计**：权限只增不减的趋势很危险，每季度清理一次"僵尸权限"

最难推进的通常不是技术实现，而是说服开发团队接受权限收紧。我的经验是先从新服务开始推行，做成标准模板，逐步迁移存量服务。
