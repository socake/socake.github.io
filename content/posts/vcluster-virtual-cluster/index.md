---
title: "vcluster 虚拟集群实战：比 namespace 强一百倍的多租户方案"
date: 2025-03-08T15:10:00+08:00
draft: false
tags: ["vcluster", "多租户", "Kubernetes", "平台工程"]
categories: ["平台工程"]
description: "vcluster 0.33 在生产里作为 AI 沙箱隔离、开发者自助命名空间、租户隔离的真实用法：架构、sync 机制、存储与网络隔离、用哪种发行版（k3s / k8s）、持久化、etcd vs sqlite、和 Loft 平台的区别。"
summary: "namespace 不是隔离边界，它只是一层命名约定。ClusterRole、CRD、webhook、LimitRange 全都穿透 namespace。真正的多租户需要每个租户有自己的 kube-apiserver。vcluster 让这件事便宜到几乎免费——一个 namespace 里起一个完整的 Kubernetes 控制平面。"
toc: true
math: false
diagram: false
keywords: ["vcluster", "virtual cluster", "多租户", "Kubernetes", "platform engineering", "sandbox"]
params:
  reading_time: true
---

## namespace 不是隔离边界

Kubernetes 里的 namespace 一直被宣传成多租户的基础，但凡是真正尝试过"在一个集群里给不同租户发 namespace 就不管了"的团队，都会遇到这些事：

- 租户 A 安装了一个 Operator，CRD 是集群级的，影响了所有 namespace；
- 租户 B 的 webhook 挂了，拦截了所有 Pod 创建；
- 租户 C 装了一个 DaemonSet，在所有节点上跑 sidecar；
- 租户 D 的 service account 通过某个 ClusterRole 看到了其他 namespace 的资源；
- 租户 E 的 admission webhook 给所有 namespace 的 Pod 注入了一个错误的环境变量。

namespace 只是一个 "命名空间"，它不隔离 CRD、不隔离 ClusterRole、不隔离 API 层面的 watch，更不隔离 Kubernetes 的 API server 本身。要做真正的隔离，历史上有几条路：

1. **多集群**：最干净，但每个集群都要买 master、付 LoadBalancer、管 networking、做升级。成本极高。
2. **KCP**：一个实验性项目，把 Kubernetes API 抽成多 workspace。早期工程、不成熟。
3. **vcluster**：把一个完整的 Kubernetes 控制平面塞进一个 namespace 里。

第三条是过去两年里真正成熟落地的方案。这篇是我在生产上跑 vcluster 0.33 做 AI 沙箱 + 开发者自助 namespace + QA 环境隔离之后的笔记。

## vcluster 的核心思想

想象你在一个 host 集群（下面叫 "host cluster"）的某个 namespace 里启一个 Pod，这个 Pod 跑的是一个完整的 Kubernetes 控制平面（apiserver + controller-manager + scheduler + storage）。这个控制平面的 API 独立可访问，你用它做 `kubectl` 的 target。

这就是 vcluster。

关键事实：

1. **vcluster 的 workload 真正跑在 host cluster 的 node 上**。vcluster 内部并没有新的 node，vcluster 自己的 scheduler 只是 "假装调度"，底层调度最终回到 host cluster 的 kubelet。
2. **每个 vcluster 在 host 的某个 namespace 里**。host namespace 对 vcluster 完全透明，vcluster 里看不到 host 的东西。
3. **vcluster 有自己的 kube-apiserver、自己的 etcd/sqlite**。CRD、RBAC、namespace、admission 完全隔离。
4. **vcluster 里创建的 Pod，会被 syncer 同步到 host 的 namespace 中**。Pod name 会加前缀，syncer 负责把两边的状态保持一致。
5. **host 的 CNI、CSI、Ingress、Node 这些基础设施 vcluster 直接复用**，不用额外跑一份。

一张图：

```
host cluster
┌──────────────────────────────────────────────┐
│                                              │
│  namespace: tenant-a                         │
│  ┌────────────────────────────┐              │
│  │  vcluster Pod (statefulset)│              │
│  │  ┌───────┐ ┌───────────┐   │              │
│  │  │ api-  │ │ syncer    │   │              │
│  │  │ server│ │           │   │              │
│  │  └───┬───┘ └─────┬─────┘   │              │
│  │      │           │         │              │
│  │   sqlite/      (watch vcluster            │
│  │    etcd         creates in                │
│  │                  vcluster's etcd          │
│  │                  and sync to host)        │
│  └────────────────────────────┘              │
│                                              │
│   vcluster 里创建的 Pod 在 host 体现为       │
│   real Pod, 由 host kubelet 调度              │
│  ┌──────────┐ ┌──────────┐                   │
│  │ Pod A-x  │ │ Pod A-y  │ (namespace 前缀)  │
│  └──────────┘ └──────────┘                   │
└──────────────────────────────────────────────┘
```

## vcluster 的发行版：k3s / k8s / eks

vcluster 可以选不同的底层 Kubernetes 发行版：

- **k3s**（默认）：最轻，内存占用 128Mi 起，sqlite 存储，适合开发 / 沙箱。
- **k8s**：完整的 kube-apiserver + etcd。和上游 Kubernetes 完全一致，生产首选。
- **eks**：用 AWS 的 EKS-D 发行版。需要 Loft 的商业版本。

选择建议：

- **开发者自助 namespace / 测试环境** → k3s，便宜、快；
- **QA / 预生产 / 生产** → k8s；
- **你不明白为什么要用 k8s 版本** → 默认 k3s 就行。

一个"k3s 够不够生产"的经验判断：如果你的 workload 不依赖 kube-apiserver 的某些边缘特性（比如 aggregated apiserver、某些 admission webhook 顺序），k3s 版本的 vcluster 就能上生产。我们生产跑过 k3s 版 vcluster，几十个 namespace，稳定。

## 一个最小可运行的 vcluster

安装 CLI：

```bash
curl -L -o vcluster https://github.com/loft-sh/vcluster/releases/latest/download/vcluster-linux-amd64
chmod +x vcluster && sudo mv vcluster /usr/local/bin
```

创建一个 vcluster（最简单）：

```bash
vcluster create my-vcluster -n tenant-a
```

这条命令会：

1. 在 host cluster 的 `tenant-a` namespace 里起一个 StatefulSet；
2. 创建一个 Service；
3. 等 StatefulSet Ready；
4. 自动帮你连上 vcluster（`kubectl config` 会切到 vcluster）。

之后你就能对这个 vcluster 正常 `kubectl get nodes`、`kubectl create deployment` 等等。

断开 vcluster：

```bash
vcluster disconnect
```

删除：

```bash
vcluster delete my-vcluster -n tenant-a
```

## Helm 安装：生产用的方式

CLI 方便，但生产一律用 Helm values 文件管理：

```yaml
# values.yaml
sync:
  toHost:
    serviceAccounts:
      enabled: true
    ingresses:
      enabled: true
  fromHost:
    csiDrivers:
      enabled: true
    csiNodes:
      enabled: true
    csiStorageCapacities:
      enabled: true

controlPlane:
  backingStore:
    etcd:
      embedded:
        enabled: true
  distro:
    k8s:
      enabled: true
  statefulSet:
    resources:
      requests:
        cpu: 200m
        memory: 512Mi
      limits:
        cpu: 2
        memory: 2Gi
    persistence:
      volumeClaim:
        enabled: true
        size: 10Gi
        storageClass: gp3

exportKubeConfig:
  server: https://my-vcluster.tenant-a.svc.cluster.local:443

networking:
  advanced:
    clusterDomain: cluster.local
  replicateServices:
    fromHost: []
    toHost: []
```

```bash
helm upgrade --install my-vcluster vcluster/vcluster \
  --namespace tenant-a --create-namespace \
  --version 0.33.x \
  -f values.yaml
```

解释几个关键配置：

### controlPlane.backingStore

vcluster 的控制平面存储选项：

- `sqlite`（默认 k3s）：最轻，但无法做 HA；
- `embeddedEtcd`：vcluster 的 StatefulSet 内部跑 etcd，HA 的话 3 副本；
- `externalEtcd`：外部 etcd 集群，最稳，但运维成本高。

我们线上的选择：

- 开发 / QA：sqlite，够用；
- 预发 / 生产：embedded etcd 3 replicas（vcluster 支持 HA 模式 replicas=3 时自动集群化）。

### controlPlane.distro

选 Kubernetes 发行版。`k8s.enabled=true` 是完整 kube-apiserver 版本，我们生产用这个。

### controlPlane.statefulSet.persistence

持久化 PVC，用来存 etcd 数据。**非常重要**：vcluster 的 etcd 挂了等于整个 vcluster 没了，PVC 必须用可靠的 storageClass。

### sync：双向同步机制

这是 vcluster 最精妙也最容易出坑的部分。

**sync.toHost**：vcluster 里创建的资源同步到 host。默认是 Pod / Service / Endpoints / ConfigMap / Secret / PVC / PV（virtual 层的）/ Events。你可以额外开启：

- `serviceAccounts`：把 vcluster 里的 SA 也同步出去，这样 Pod 才能以 SA 身份访问 host 的 API（生产一般要开）；
- `ingresses`：把 vcluster 里创建的 Ingress 同步到 host，让 host 的 Ingress Controller 处理；
- `networkPolicies`：同步 NetworkPolicy。

**sync.fromHost**：从 host 读取数据到 vcluster 里。默认是 Node / PersistentVolume / StorageClass。可以额外开启：

- `csiDrivers / csiNodes / csiStorageCapacities`：用来做 CSI 相关的动态 provisioning；
- `priorityClasses`：把 host 的 PriorityClass 复制到 vcluster，让 vcluster 里的 Pod 能用 host 的调度优先级。

**原则**：
- 能不 sync 的不 sync；
- sync 的越多，host 和 vcluster 的耦合越强；
- sync ingresses 要小心——所有 vcluster 的 Ingress 在 host 上都是真实 Ingress，要确保名字唯一（syncer 会加前缀）。

## 多租户隔离的关键：network 和 storage

### Pod 网络隔离

vcluster 默认**不做网络隔离**。vcluster A 的 Pod 和 vcluster B 的 Pod 在 host 的 CNI 层是能互通的。

要做隔离，两条路：

1. **NetworkPolicy + sync.toHost.networkPolicies**：在 vcluster 里定义 NetworkPolicy，syncer 把它推到 host，host 的 CNI（Calico / Cilium）负责执行。前提是你的 host CNI 支持 NetworkPolicy。

2. **host-level 隔离**：在 host 层面，给每个 vcluster 的 namespace 写一个 default-deny 的 NetworkPolicy，只允许 vcluster 内部通信、不允许跨 namespace 访问。这个最干净，但需要你提前规划 namespace 布局。

我们的做法：host 层面默认 deny + 白名单。对平台核心服务（DNS、monitoring、ingress）开 egress 白名单，对 vcluster 之间的 namespace 默认 deny。

### 存储隔离

vcluster 里创建的 PVC 会被 syncer 转成 host 的 PVC，实际由 host 的 CSI 处理。这里要关注：

1. **storageClass 权限**：vcluster 能用 host 的哪些 StorageClass？默认全部。如果你希望限制租户只能用某几个 SC（比如只能用 gp3、不能用 io2），在 vcluster 里显式创建 StorageClass 并禁用 fromHost 同步。
2. **PV 配额**：vcluster 本身不做 storage quota，要走 host namespace 的 ResourceQuota。
3. **PV name 冲突**：syncer 会给 PV 加前缀，不会冲突。但如果你手动创建 static PV 就要小心。

## RBAC：谁能访问这个 vcluster

vcluster 的 kubeconfig 默认访问方式：

- **ClusterIP + port-forward**：开发用；
- **LoadBalancer**：生产用；
- **NodePort**：不建议；
- **Ingress**：需要 TCP passthrough 的 ingress controller。

生产推荐：给 vcluster 的 Service 开 LoadBalancer，对应一个内网 NLB。租户拿着自己的 kubeconfig 访问。

kubeconfig 怎么生成？

```bash
vcluster connect my-vcluster -n tenant-a --update-current=false --kube-config=./tenant-a.kubeconfig
```

或者 vcluster StatefulSet 启动时会生成一个 Secret，名字类似 `vc-my-vcluster`，里面有 admin kubeconfig。**这个 kubeconfig 是 vcluster 的 cluster-admin，不能直接给业务用**。

生产做法：

1. 用 vcluster admin kubeconfig 创建业务专属的 ServiceAccount；
2. 用 RoleBinding / ClusterRoleBinding 给这个 SA 指定权限；
3. 给业务生成一个只绑这个 SA 的 kubeconfig；
4. admin kubeconfig 只留在平台团队手上。

## 资源配额

vcluster 本身的资源占用：

- k3s 版本：128-256Mi 内存，50m CPU；
- k8s 版本：1-2Gi 内存，500m CPU（因为 kube-apiserver + etcd + scheduler + controller-manager）；
- HA 模式的 k8s：3 倍。

给业务的配额要加在 host 的 namespace 上（用 ResourceQuota），而不是 vcluster 内部。vcluster 内部 ResourceQuota 只对 vcluster 里的 namespace 有效，不能限制 host 资源总用量。

示例（加在 host 的 tenant-a namespace）：

```yaml
apiVersion: v1
kind: ResourceQuota
metadata:
  name: tenant-a-quota
  namespace: tenant-a
spec:
  hard:
    requests.cpu: "40"
    requests.memory: "80Gi"
    limits.cpu: "80"
    limits.memory: "160Gi"
    requests.storage: "500Gi"
    count/pods: "500"
```

这个 quota 包含了 vcluster 控制平面 + 所有同步到 host 的 Pod + PVC，是真实的租户总预算。

## vcluster 里跑 Operator 会怎么样

这是最常被问的。答案：**可以，且 Operator 的 CRD 只影响这个 vcluster**。

但有两个注意点：

1. **Operator 依赖的 CRD** 需要在 vcluster 内安装。有些 Operator 的安装脚本默认是 cluster-admin 操作，在 vcluster 里也行，因为 vcluster 的 cluster-admin 不出 vcluster 范围。
2. **Operator 创建的 Pod** 最终会被 syncer 同步到 host。Pod spec 有些字段会被 syncer 改写（比如 node selector、tolerations 可能会被追加 host level 的默认值）。绝大部分 Operator 不会受影响，但像 node-exporter 这种 DaemonSet 就不行——vcluster 里"所有 node" 看起来就那么几个，但你并不想每个 host node 跑一个 vcluster 的 node-exporter。

DaemonSet 是 vcluster 里**一个特殊的情况**：vcluster 里定义一个 DaemonSet，syncer 不会在 host 每个 node 上都起一个，而是按 vcluster 视图里的"node 数"来。这是安全的默认行为。

## 监控 vcluster

vcluster 暴露了自己的 Prometheus metrics，你可以从 vcluster 的 Pod 抓。几个重要指标：

- `vcluster_syncer_sync_errors_total`：syncer 同步错误；
- `vcluster_syncer_sync_duration_seconds`：同步延迟；
- `apiserver_*`：vcluster 里的 kube-apiserver 指标，和普通 Kubernetes 一样。

监控的另一个维度是 host 视角：vcluster 所在的 namespace 里，Pod 的资源使用情况能通过 host 的 metrics-server 看到。这是你判断 vcluster 是否消耗过多资源的唯一可靠渠道。

## 踩过的坑

### 坑 1：vcluster 的 kubeconfig 里的 server 地址

默认情况下 vcluster 生成的 kubeconfig 指向 `https://localhost:8443`（因为你通过 port-forward 访问）。在生产中你要改成 `https://<service>.<namespace>.svc.cluster.local:443` 或 LoadBalancer 的外网地址。

Helm values 里用 `exportKubeConfig.server` 设置正确的 server 地址，这样 vcluster 启动时生成的 kubeconfig Secret 就直接可用。

### 坑 2：sync.toHost.ingresses 开启后，host ingress controller 突然拦不住

vcluster 里创建的 Ingress 会同步到 host，但 IngressClass 没同步。这会导致 vcluster 里的 Ingress 没有 ingressClassName，host 的 ingress controller 不管。解决：在 vcluster 里创建一个 IngressClass 的 "影子"，或者 fromHost 同步 IngressClasses。

### 坑 3：CoreDNS

vcluster 里默认有自己的 CoreDNS Pod，它只解析 vcluster 视图里的 Service。一般够用，但如果你要从 vcluster 里访问 host namespace 里的 Service，需要显式配置 serviceCIDR mapping 或者使用 `replicateServices`。

### 坑 4：time drift

vcluster 控制平面 Pod 如果被迁移或者重启后时间和 host 有差，可能导致 etcd / kube-apiserver 的内部时间戳混乱。解决：Pod 里用 chronyd sidecar 或者让 host node NTP 永远同步。

### 坑 5：vcluster 升级

vcluster 升级就是 Helm upgrade，但要注意：

- etcd 的数据会留在 PVC 里，不会丢；
- 控制平面 image 会更新；
- Kubernetes 版本跨度大时（比如 1.27 → 1.30）要按照上游 Kubernetes 升级节奏，不能跳版本。

升级前一定：
1. 备份 vcluster 的 etcd（或 sqlite 文件）；
2. 在低峰期做；
3. 先升级一个非生产 vcluster 试试。

### 坑 6：删除 vcluster 不等于删除业务数据

`vcluster delete` 会删 StatefulSet 和相关 Service，但**不会删 PVC**（避免误删）。完整清理要加 `--delete-pvc` 或者手动删 PVC。运维自动化脚本里记得包含这步。

## vcluster vs 其他方案

### vcluster vs HNC（Hierarchical Namespace Controller）

HNC 是"namespace 之间做继承关系"，给 RBAC / Quota 做继承。它没解决 CRD、admission、API 隔离的问题。vcluster 解决了。

**HNC 适合**：大型组织内部的 namespace 组织结构，比如 "team-a/project-1"；
**vcluster 适合**：真正的租户隔离。

### vcluster vs Multi-Cluster

跨 AWS Account、跨 VPC、合规要求（PCI DSS 不能和其他租户共 node）这些场景必须走独立集群。vcluster 节省不了。

但对于 "把 80% 的隔离需求压缩到 5% 的成本" 这种场景，vcluster 是最划算的。

### vcluster vs KCP

KCP 的思路更激进：把 Kubernetes 的 "workspace" 抽象成一个多租户的 API 平面。它不跑 Pod 同步，也没有 syncer 那一层复杂性，但 KCP 目前成熟度还比不上 vcluster，而且它的社区用户基础小得多。

## Loft 平台：vcluster 的商业版本

Loft 是 vcluster 的母公司（loft-sh），Loft Platform 是基于 vcluster 做的企业版管理平台，提供：

- Web UI 创建 vcluster；
- 自助式 namespace；
- SSO 集成；
- Sleep Mode（闲时自动暂停 vcluster 节省资源）；
- Audit log；
- Cost chargeback。

开源 vcluster 本身完全可用。你什么时候需要 Loft Platform？

- 租户超过 20 个，平台团队手动管理太累；
- 需要 web UI / SSO；
- 需要 sleep mode（开发环境闲时省钱）。

15 个 vcluster 以下，开源版本 + 自研脚本够用。

## 一些最佳实践总结

我在生产上跑 vcluster 的几条铁律：

1. **每个 vcluster 独占一个 host namespace**，不要共享；
2. **host namespace 有严格的 ResourceQuota**，vcluster 自身的开销要算进去；
3. **k8s 发行版 + embedded etcd + persistent storage**，生产不要用 sqlite；
4. **host 层默认 deny network policy + 白名单**；
5. **vcluster admin kubeconfig 不给业务**，业务用专属 SA 的 kubeconfig；
6. **监控 vcluster syncer 错误和 etcd 使用量**；
7. **vcluster 本身的 image 要定期升级**，跟 host Kubernetes 的版本保持兼容；
8. **备份 vcluster 的 PVC**（里面是 etcd 数据），用 Velero 或 snapshotter；
9. **删除 vcluster 的脚本里带上 PVC 清理**；
10. **不要在 vcluster 里跑 DaemonSet 做 node 级监控**。

vcluster 这东西最大的价值是"用便宜得多的方式做接近独立集群的隔离"。一旦你接受了它的模型，namespace 这个抽象在很多场景下就变得可有可无了。我们现在的新业务 default 做法是：开一个 vcluster，而不是开一个 namespace。

这是过去两年里 Kubernetes 生态给我的几个最有意思的工程惊喜之一。
