---
title: "Kubernetes 多租户方案深度对比：vCluster vs Capsule vs HNC"
date: 2025-12-03T10:00:00+08:00
draft: false
tags: ["kubernetes", "multitenancy", "vcluster", "capsule", "hnc", "platform-engineering"]
categories: ["Kubernetes"]
description: "深度对比 vCluster、Capsule、HNC 三种 K8s 多租户方案，涵盖架构原理、部署配置、隔离能力、选型建议和成本计量，帮助你在不同场景下做出正确决策。"
summary: "Namespace 级隔离远不够用。本文深入剖析 vCluster、Capsule、HNC 三种主流多租户方案的架构差异，给出完整的部署配置示例、隔离能力横向对比，以及 SaaS 平台、内部平台、开发环境三种场景下的选型建议。"
toc: true
math: false
diagram: false
keywords: ["kubernetes多租户", "vcluster", "capsule", "hnc", "namespace隔离", "tenant", "platform engineering"]
params:
  reading_time: true
---

## 多租户的本质问题

很多团队以为给每个团队创建一个 Namespace 就实现了多租户，这是对 K8s 隔离模型最大的误解。

Namespace 本质上只是一个**命名空间**，不是安全边界。来看几个具体问题：

**1. 集群级资源无隔离**

`ClusterRole`、`StorageClass`、`PriorityClass`、`IngressClass`、`CRD` 全是集群范围的资源。一个租户的管理员如果拿到了 `ClusterRole` 的创建权限，整个集群就暴露了。即便你用 `RoleBinding` 把权限锁在 Namespace 内，共享的 `ClusterRole` 仍然可能被利用。

**2. 网络默认互通**

不加 `NetworkPolicy` 的情况下，任意 Pod 都能访问其他 Namespace 的 Service。`kube-dns` 全局解析，Pod 直接 `curl http://payment-service.finance.svc.cluster.local` 就能跨租户访问。

**3. 资源抢占**

没有 `ResourceQuota` 和 `LimitRange` 的 Namespace，里面的 Pod 可以把节点内存吃满，影响所有邻居。但配置这些还需要有人维护，一旦漏掉就是生产故障。

**4. 审计和计费困难**

多个团队共用集群，谁消耗了多少 CPU/Memory？按 Namespace 汇总很粗粒度，跨 Namespace 的项目更难统计。

**5. 自助申请困难**

开发团队想新建一个 Namespace，要找平台团队手动操作，还要配齐 NetworkPolicy、ResourceQuota、LimitRange、ServiceAccount、RoleBinding……每次都是重复劳动。

这五个问题是真实的生产痛点。接下来的三个方案从不同维度解决它们。

---

## 方案一：vCluster

### 架构原理

vCluster 的思路最激进：**在宿主集群的 Namespace 里运行一个完整的虚拟 K8s 集群**。

```
Host Cluster
└── Namespace: tenant-a
    ├── Pod: vcluster-0 (StatefulSet)
    │   ├── k3s / k8s API Server
    │   ├── etcd (可选独立)
    │   └── syncer (核心组件)
    └── Service: vcluster (LoadBalancer/NodePort)
```

**Syncer** 是 vCluster 的关键：它把虚拟集群里的 Pod、Service、PVC 等资源"同步"到宿主集群的 Namespace 里真正调度。虚拟集群的 API Server 完全独立，租户拿到的 kubeconfig 指向这个虚拟 API Server，对宿主集群一无所知。

同步策略分两层：
- **向下同步**：Pod、ConfigMap、Secret（部分）、PVC 从虚拟集群同步到宿主
- **向上同步**：Pod 状态、Node 信息从宿主同步回虚拟集群

Node 默认以伪节点形式出现在虚拟集群里，租户看到的是"完整的集群"，但底层调度还是宿主 Scheduler。

### 安装

```bash
# 安装 vCluster CLI
curl -L -o /usr/local/bin/vcluster \
  "https://github.com/loft-sh/vcluster/releases/latest/download/vcluster-linux-amd64"
chmod +x /usr/local/bin/vcluster

# 创建租户 A 的虚拟集群
vcluster create tenant-a \
  --namespace vcluster-tenant-a \
  --values values-tenant-a.yaml
```

`values-tenant-a.yaml` 的关键配置：

```yaml
# values-tenant-a.yaml
controlPlane:
  distro:
    k3s:
      enabled: true
      version: "v1.29.3-k3s1"
  statefulSet:
    resources:
      requests:
        cpu: 200m
        memory: 256Mi
      limits:
        cpu: 2
        memory: 2Gi

# 同步策略
sync:
  toHost:
    pods:
      enabled: true
      rewriteHosts:
        enabled: true
    services:
      enabled: true
    persistentVolumeClaims:
      enabled: true
    ingresses:
      enabled: true
    networkPolicies:
      enabled: true  # 允许租户管理自己的 NetworkPolicy
  fromHost:
    nodes:
      enabled: true
      selector:
        all: false
        labels:
          tenant: "a"  # 可以绑定特定节点池

# 资源隔离：映射宿主 StorageClass
mapServices:
  fromHost:
    - from: fast-ssd
      to: default

# 把宿主的某个 Secret 注入虚拟集群（如镜像仓库凭据）
referencedCoreV1Resources: "secrets,configmaps"

# 隔离模式：禁止访问宿主 API
experimental:
  isolatedControlPlane:
    enabled: false

# 给宿主 Namespace 加 ResourceQuota
isolation:
  enabled: true
  resourceQuota:
    enabled: true
    quota:
      requests.cpu: "10"
      requests.memory: 20Gi
      limits.cpu: "20"
      limits.memory: 40Gi
      count/pods: "200"
  limitRange:
    enabled: true
    default:
      cpu: 500m
      memory: 512Mi
    defaultRequest:
      cpu: 100m
      memory: 128Mi
```

### 获取租户 kubeconfig

```bash
# 获取虚拟集群的 kubeconfig
vcluster connect tenant-a --namespace vcluster-tenant-a \
  --server https://tenant-a.k8s.example.com \
  --update-current=false \
  -n vcluster-tenant-a \
  > tenant-a-kubeconfig.yaml

# 租户管理员拿到这个 kubeconfig 后，有完整的集群管理权
KUBECONFIG=tenant-a-kubeconfig.yaml kubectl get nodes
# NAME          STATUS   ROLES    AGE
# fake-node-0   Ready    <none>   5m
```

### 网络隔离补充

虚拟集群的 Pod 在宿主层共享节点网络，需要在宿主层加 NetworkPolicy 隔离不同虚拟集群的流量：

```yaml
# 宿主集群：禁止不同 vcluster namespace 之间的流量
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: isolate-vcluster
  namespace: vcluster-tenant-a
spec:
  podSelector: {}
  policyTypes:
    - Ingress
    - Egress
  ingress:
    - from:
        - podSelector: {}          # 同 namespace 内允许
  egress:
    - to:
        - podSelector: {}          # 同 namespace 内允许
    - ports:
        - port: 53                 # DNS
          protocol: UDP
    - to:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: kube-system
```

---

## 方案二：Capsule

### 架构原理

Capsule 的思路是**不引入新的控制平面，而是在现有集群上叠加多租户语义**。

核心概念：`Tenant` CRD 聚合一组 Namespace，通过 Webhook 和控制器在这些 Namespace 上统一执行策略。

```
Capsule Controller
├── TenantController → 管理 Namespace 创建/策略下推
├── Admission Webhook → 拦截请求，执行跨 Namespace 策略
└── Capsule Proxy (可选) → 代理 kubectl，实现跨 Namespace 资源聚合视图

Tenant: team-frontend
├── Namespace: frontend-dev
├── Namespace: frontend-staging
└── Namespace: frontend-prod
    (统一 ResourceQuota, NetworkPolicy, RBAC, ImagePolicy)
```

`TenantUser` 通过普通 kubeconfig 访问集群，Webhook 识别他的身份，限制他只能操作自己 Tenant 下的 Namespace。

### 安装

```bash
helm repo add projectcapsule https://projectcapsule.github.io/charts
helm repo update

helm install capsule projectcapsule/capsule \
  --namespace capsule-system \
  --create-namespace \
  --set manager.options.forceTenantPrefix=true \
  --set manager.options.userGroups[0]=capsule.clastix.io \
  --set manager.options.capsuleUserGroups[0]=capsule.clastix.io
```

### 创建 Tenant

```yaml
# tenant-frontend.yaml
apiVersion: capsule.clastix.io/v1beta2
kind: Tenant
metadata:
  name: team-frontend
spec:
  owners:
    - name: alice
      kind: User
    - name: frontend-leads
      kind: Group

  # Namespace 命名限制（强制前缀）
  namespaceOptions:
    quota: 10                    # 最多创建 10 个 Namespace
    forbiddenLabels:
      denied:
        - environment: production  # 禁止自行打 production 标签
    additionalMetadata:
      labels:
        team: frontend
        cost-center: cc-001
      annotations:
        monitoring.example.com/team: frontend

  # 统一 ResourceQuota
  resourceQuotas:
    scope: Tenant                # Tenant 级别总量
    items:
      - hard:
          requests.cpu: "20"
          requests.memory: 40Gi
          limits.cpu: "40"
          limits.memory: 80Gi
          count/pods: "500"
          count/services: "50"
          count/persistentvolumeclaims: "20"

  # 每个 Namespace 的 LimitRange
  limitRanges:
    items:
      - limits:
          - type: Container
            default:
              cpu: 500m
              memory: 512Mi
            defaultRequest:
              cpu: 100m
              memory: 128Mi
            max:
              cpu: "8"
              memory: 16Gi

  # NetworkPolicy：自动注入到每个 Namespace
  networkPolicies:
    items:
      - podSelector: {}
        policyTypes:
          - Ingress
          - Egress
        ingress:
          - from:
              - namespaceSelector:
                  matchLabels:
                    capsule.clastix.io/tenant: team-frontend
        egress:
          - to:
              - namespaceSelector:
                  matchLabels:
                    capsule.clastix.io/tenant: team-frontend
          - ports:
              - port: 53
                protocol: UDP
              - port: 443  # 允许出公网 HTTPS

  # 允许使用哪些 StorageClass
  storageClasses:
    matchLabels:
      capsule.clastix.io/storage-class: allowed
    allowed:
      - fast-ssd
      - standard

  # 允许使用哪些 IngressClass
  ingressOptions:
    allowedClasses:
      allowed:
        - nginx
    allowedHostnames:
      allowed:
        - "*.frontend.example.com"
    hostnameCollisionScope: Tenant  # 防止同租户内域名冲突

  # 节点选择器（可选）
  nodeSelector:
    node-pool: frontend

  # 镜像仓库限制
  containerRegistries:
    allowed:
      - registry.example.com
      - "*.dkr.ecr.*.amazonaws.com"
```

```bash
kubectl apply -f tenant-frontend.yaml

# 创建绑定关系：alice 加入 capsule.clastix.io 组
# （通常通过 OIDC 的 group claim 实现）
kubectl create clusterrolebinding alice-capsule \
  --clusterrole=capsule:tenant:team-frontend \
  --user=alice
```

### 租户自助创建 Namespace

租户管理员（alice）创建 Namespace 时，Capsule Webhook 自动验证前缀和配额：

```bash
# alice 的 kubeconfig 指向同一个 API Server，但 Webhook 限制了她的操作范围
kubectl create namespace frontend-dev
# namespace/frontend-dev created  （Capsule 自动打上 tenant label，注入策略）

kubectl create namespace production-db
# Error: namespace name must have prefix "team-frontend-"
# （forceTenantPrefix=true 时自动验证）
```

### Capsule Proxy

Capsule Proxy 让租户用 `kubectl get namespaces` 只看到自己的 Namespace，解决 ClusterScoped 资源的"幻觉隔离"：

```bash
helm install capsule-proxy projectcapsule/capsule-proxy \
  --namespace capsule-system \
  --set options.generateCertificates=true \
  --set options.oidcUsernameClaim=email

# 租户 kubeconfig 的 server 改为 capsule-proxy 地址
# kubectl get namespaces 只返回 team-frontend 下的 namespace
```

---

## 方案三：HNC（Hierarchical Namespace Controller）

### 架构原理

HNC 来自 Google，解决的是**Namespace 策略继承**问题，而非完整的多租户隔离。

```
org-root (anchor)
├── team-platform
│   ├── platform-dev
│   └── platform-staging
└── team-frontend
    ├── frontend-dev
    └── frontend-prod
        └── frontend-prod-canary  (子 Namespace)
```

核心机制：
- **SubnamespaceAnchor**：在父 Namespace 创建一个特殊对象，触发子 Namespace 的创建
- **传播规则**：父 Namespace 的 RBAC、NetworkPolicy、LimitRange 自动传播到所有子 Namespace
- **异常覆盖**：子 Namespace 可以声明 `propagate.hnc.x-k8s.io/nonCascading` 阻止传播

### 安装

```bash
# 使用官方 manifest
kubectl apply -f https://github.com/kubernetes-sigs/hierarchical-namespaces/releases/latest/download/default.yaml

# 安装 kubectl 插件
curl -L https://github.com/kubernetes-sigs/hierarchical-namespaces/releases/latest/download/kubectl-hns_linux_amd64 \
  -o /usr/local/bin/kubectl-hns
chmod +x /usr/local/bin/kubectl-hns
```

### 创建层级 Namespace

```bash
# 创建根 Namespace
kubectl create namespace team-frontend

# 平台团队在 team-frontend 下创建子 Namespace
kubectl hns create frontend-dev -n team-frontend
kubectl hns create frontend-staging -n team-frontend
kubectl hns create frontend-prod -n team-frontend

# 开发团队可以在 frontend-dev 下自助创建子 Namespace
kubectl hns create frontend-dev-feature-x -n frontend-dev
```

对应的 SubnamespaceAnchor 对象（自动创建）：

```yaml
apiVersion: hnc.x-k8s.io/v1alpha2
kind: SubnamespaceAnchor
metadata:
  name: frontend-dev
  namespace: team-frontend
```

### 传播 RBAC

```yaml
# 在父 Namespace 创建 RoleBinding，自动传播到所有子 Namespace
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: frontend-dev-access
  namespace: team-frontend
  annotations:
    # 不加这个 annotation 默认全部传播
    # hnc.x-k8s.io/propagated: "true"
subjects:
  - kind: Group
    name: frontend-engineers
    apiGroup: rbac.authorization.k8s.io
roleRef:
  kind: ClusterRole
  name: edit
  apiGroup: rbac.authorization.k8s.io
```

### HNC 配置策略

```yaml
apiVersion: hnc.x-k8s.io/v1alpha2
kind: HNCConfiguration
metadata:
  name: config
spec:
  resources:
    - resource: roles
      group: rbac.authorization.k8s.io
      mode: Propagate          # 传播
    - resource: rolebindings
      group: rbac.authorization.k8s.io
      mode: Propagate
    - resource: networkpolicies
      group: networking.k8s.io
      mode: Propagate
    - resource: limitranges
      group: ""
      mode: Propagate
    - resource: resourcequotas
      group: ""
      mode: Ignore             # ResourceQuota 不传播，各子 Namespace 独立配置
    - resource: configmaps
      group: ""
      mode: Propagate
    - resource: secrets
      group: ""
      mode: AllowPropagate     # 仅传播带有特定 annotation 的 Secret
```

---

## 隔离能力横向对比

| 维度 | vCluster | Capsule | HNC |
|------|----------|---------|-----|
| **API Server 隔离** | 完全独立 | 共享 | 共享 |
| **etcd 隔离** | 独立（虚拟集群内） | 共享 | 共享 |
| **CRD 隔离** | 完全隔离，租户可自定义 CRD | 共享 CRD，不能冲突 | 共享 CRD |
| **RBAC 隔离** | 虚拟集群内完全独立 | Webhook 强制，ClusterRole 共享 | 传播继承，ClusterRole 共享 |
| **网络隔离** | 宿主层 NetworkPolicy 手动配置 | 自动注入 NetworkPolicy | 传播 NetworkPolicy |
| **节点隔离** | 可绑定节点池（node selector） | 可指定 nodeSelector | 不涉及 |
| **资源配额** | 宿主 Namespace 层 ResourceQuota | Tenant 级聚合 + Namespace 级 | 各自独立配置 |
| **自助 Namespace** | 租户内完全自助 | 租户内受控自助 | 子 Namespace 自助 |
| **K8s 版本差异** | 可以和宿主不同版本 | 必须一致 | 必须一致 |
| **运营开销** | 每租户一个虚拟集群（资源开销约 200m CPU/256Mi） | 轻量，Webhook + Controller | 极轻量 |
| **成熟度** | CNCF Sandbox，生产可用 | CNCF Sandbox，生产可用 | k8s-sigs，Google 内部大量使用 |

---

## 租户自助 Namespace 申请工作流

以 Capsule 为例，设计一个 GitOps 驱动的自助申请流程：

```
开发团队 → PR 到 tenant-config 仓库
         ↓
         提交 SubnamespaceRequest（自定义 CRD 或 YAML）
         ↓
Reviewer 审批 → ArgoCD 同步 → Capsule 创建 Namespace
         ↓
         自动触发：注入 NetworkPolicy、ResourceQuota、LimitRange、ServiceAccount
         ↓
         Slack/钉钉通知申请人
```

`SubnamespaceRequest` 示例（简化版 CRD）：

```yaml
apiVersion: platform.example.com/v1alpha1
kind: NamespaceRequest
metadata:
  name: feature-payment-refactor
  namespace: team-backend    # 提交到所在 Tenant 的父 Namespace
spec:
  requestedBy: bob@example.com
  purpose: "重构支付模块，需要独立测试环境"
  ttl: "30d"                 # 30 天后自动回收
  resourceProfile: small     # small/medium/large 对应预设的 ResourceQuota
  environments:
    - dev
    - staging
```

---

## 选型指南

### SaaS 平台（强隔离）→ vCluster

- 客户之间完全隔离，不能互相感知
- 客户需要 CRD 自定义能力（安装自己的 Operator）
- 不同客户可能需要不同 K8s 版本（版本销售）
- 代价：每客户至少 200m CPU + 256Mi，1000 个租户就是 200 核 + 256Gi 的控制平面开销

```bash
# 自动化创建：每个新客户注册时触发
vcluster create customer-${CUSTOMER_ID} \
  --namespace vcluster-${CUSTOMER_ID} \
  --values /etc/vcluster/customer-template.yaml
```

### 企业内部平台（受控共享）→ Capsule

- 多个业务团队共用集群，平台团队统一治理
- 需要集中管控镜像仓库、IngressClass、StorageClass 的使用权限
- 团队需要跨 Namespace 的聚合视图（多个 env Namespace 属于同一团队）
- 不需要 CRD 隔离，共享 Operator 生态

### 开发环境/项目隔离（轻量策略继承）→ HNC

- 项目树状管理：org → team → project → feature-branch
- 主要诉求是 RBAC 和 NetworkPolicy 的层级继承，减少手工配置
- 不需要强隔离，信任内部用户
- 已有大量 Namespace，想在不迁移的情况下增加层级管理

---

## 费用计量与 Chargeback（OpenCost）

部署 OpenCost 后，按 Namespace 汇总费用，再结合 Capsule 的 Tenant 标签做 Chargeback：

```bash
helm install opencost opencost/opencost \
  --namespace opencost \
  --create-namespace \
  --set opencost.exporter.cloudProviderApiKey="" \
  --set opencost.prometheus.internal.enabled=true
```

查询 team-frontend 的月度费用：

```bash
# OpenCost API
curl "http://opencost.opencost.svc:9003/allocation/compute?\
  window=month&\
  aggregate=namespace&\
  filter=namespace:frontend-dev+frontend-staging+frontend-prod" \
  | jq '.data[0] | to_entries[] | {ns: .key, cost: .value.totalCost}'
```

结合 Capsule 的 `cost-center` annotation，自动生成 Chargeback 报表：

```python
import requests

def get_tenant_cost(tenant_namespaces: list[str], window: str = "month") -> float:
    ns_filter = "+".join(tenant_namespaces)
    resp = requests.get(
        f"http://opencost.opencost.svc:9003/allocation/compute",
        params={"window": window, "aggregate": "namespace", "filter": f"namespace:{ns_filter}"}
    )
    data = resp.json()["data"][0]
    return sum(v["totalCost"] for v in data.values())

# Capsule Tenant 的 cost-center label → 汇总到对应部门
tenants = {
    "cc-001": ["frontend-dev", "frontend-staging", "frontend-prod"],
    "cc-002": ["backend-dev", "backend-staging"],
}
for cc, namespaces in tenants.items():
    cost = get_tenant_cost(namespaces)
    print(f"Cost Center {cc}: ${cost:.2f}/month")
```

---

## 总结

三种方案不是竞争关系，甚至可以组合使用——用 HNC 管理 Namespace 树，在 HNC 管理的 Namespace 里运行 Capsule Tenant，或者用 vCluster 给强隔离需求的外部客户，用 Capsule 管理内部团队。

关键决策因素只有三个：**隔离强度**（外部客户 vs 内部团队）、**CRD 自主性**（租户是否需要安装自己的 Operator）、**规模**（租户数量决定控制平面开销是否可接受）。把这三个问题回答清楚，选型就不会错。
