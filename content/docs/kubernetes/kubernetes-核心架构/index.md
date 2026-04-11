---
title: "Kubernetes 核心架构全景"
date: 2025-12-08T10:00:00+08:00
draft: false
tags: ["Kubernetes", "云原生", "容器编排"]
categories: ["Kubernetes"]
description: "从控制面到工作节点，Kubernetes 核心组件原理与生产环境实践要点"
summary: "深入理解 Kubernetes 控制面与工作节点各组件的职责与交互关系，结合生产环境实际经验，梳理核心资源对象与调度原理。"
toc: true
math: false
diagram: false
keywords: ["kubernetes", "k8s", "控制面", "etcd", "调度器"]
params:
  reading_time: true
---

## 1. Kubernetes 是什么

**一句话定义**：Kubernetes（简称 K8s）是一个开源的容器编排平台，用于自动化部署、扩缩容和管理容器化应用。

**解决什么问题**：在容器技术（Docker）普及之后，单机跑容器很简单，但当应用需要跨多台机器部署、自动故障恢复、滚动升级、流量负载均衡时，手工管理几乎不可能。Kubernetes 的核心价值在于：

- **声明式管理**：描述"期望状态"，系统自动驱动实际状态向期望收敛
- **自愈能力**：Pod 挂了自动重启，节点挂了自动迁移
- **弹性扩缩容**：基于 CPU/内存指标自动水平扩缩 Pod 数量
- **服务发现与负载均衡**：内置 DNS + Service 抽象，屏蔽 Pod IP 变化
- **配置与密钥管理**：ConfigMap/Secret 解耦配置与镜像

---

## 2. 整体架构

Kubernetes 集群由**控制面（Control Plane）**和**工作节点（Worker Node）**两部分组成，etcd 作为整个集群的状态存储独立存在（生产建议与控制面分离部署）。

```
┌─────────────────────────────────────────────────────────────────┐
│                        Control Plane                            │
│                                                                 │
│  ┌─────────────────┐   ┌──────────────┐   ┌─────────────────┐  │
│  │  kube-apiserver  │   │kube-scheduler│   │  kube-controller│  │
│  │  (唯一入口)       │   │  (调度决策)   │   │    -manager     │  │
│  │  :6443           │   │              │   │  (控制循环)      │  │
│  └────────┬─────────┘   └──────┬───────┘   └────────┬────────┘  │
│           │                   │                     │           │
│           └───────────────────┼─────────────────────┘           │
│                               │  (全部经由 apiserver 通信)       │
│           ┌───────────────────┘                                 │
│           ▼                                                     │
│  ┌─────────────────┐   ┌──────────────────────────────────┐    │
│  │      etcd        │   │     cloud-controller-manager     │    │
│  │  (集群状态存储)   │   │   (云厂商集成: LB/节点/路由)      │    │
│  │  :2379           │   │                                  │    │
│  └─────────────────┘   └──────────────────────────────────┘    │
└────────────────────────────┬────────────────────────────────────┘
                             │  HTTPS (apiserver → kubelet)
              ┌──────────────┼──────────────────┐
              ▼              ▼                  ▼
┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐
│   Worker Node 1  │  │   Worker Node 2  │  │   Worker Node N  │
│                 │  │                 │  │                 │
│ ┌─────────────┐ │  │ ┌─────────────┐ │  │ ┌─────────────┐ │
│ │   kubelet   │ │  │ │   kubelet   │ │  │ │   kubelet   │ │
│ └─────────────┘ │  │ └─────────────┘ │  │ └─────────────┘ │
│ ┌─────────────┐ │  │ ┌─────────────┐ │  │ ┌─────────────┐ │
│ │ kube-proxy  │ │  │ │ kube-proxy  │ │  │ │ kube-proxy  │ │
│ └─────────────┘ │  │ └─────────────┘ │  │ └─────────────┘ │
│ ┌─────────────┐ │  │ ┌─────────────┐ │  │ ┌─────────────┐ │
│ │  containerd │ │  │ │  containerd │ │  │ │  containerd │ │
│ └─────────────┘ │  │ └─────────────┘ │  │ └─────────────┘ │
│ ┌────┐ ┌────┐  │  │ ┌────┐ ┌────┐  │  │ ┌────┐ ┌────┐  │
│ │Pod │ │Pod │  │  │ │Pod │ │Pod │  │  │ │Pod │ │Pod │  │
│ └────┘ └────┘  │  │ └────┘ └────┘  │  │ └────┘ └────┘  │
└─────────────────┘  └─────────────────┘  └─────────────────┘
```

**核心设计原则**：所有组件都只与 apiserver 通信，不互相直连。etcd 只有 apiserver 可以访问，其他组件通过 apiserver 读写集群状态。

---

## 3. 控制面组件详解

### 3.1 kube-apiserver

**职责**：集群的唯一入口，所有对 Kubernetes 资源的读写操作都必须经过 apiserver。

**请求处理流程**：

```
客户端请求
    │
    ▼
┌──────────────┐
│  认证 (AuthN) │  ← 证书 / ServiceAccount Token / OIDC / Webhook
└──────┬───────┘
       │
       ▼
┌──────────────┐
│  授权 (AuthZ) │  ← RBAC / ABAC / Node / Webhook
└──────┬───────┘
       │
       ▼
┌──────────────────┐
│  准入控制 (Admission)│  ← MutatingWebhook → ValidatingWebhook
└──────┬───────────┘
       │
       ▼
┌──────────────┐
│  写入 etcd    │
└──────────────┘
```

**生产注意事项**：
- apiserver 是无状态服务，可以水平扩展多副本，前面挂 LB（内网 NLB 或 HAProxy）
- 通过 `--audit-log-path` 开启审计日志，生产合规必须
- `--etcd-servers` 指定 etcd 集群地址，多个 etcd 节点逗号分隔
- 请求量大时关注 `apiserver_request_total` 指标，按动词/资源分类监控

```bash
# 查看 apiserver 状态
kubectl get componentstatuses

# 查看 apiserver 暴露的 API 版本
kubectl api-versions

# 查看某个资源的 API 详情
kubectl explain pod.spec.containers
```

### 3.2 etcd

**职责**：分布式键值存储，存储 Kubernetes 集群的所有状态数据（Pod、Service、Deployment 等所有对象）。

**关键特性**：
- 使用 Raft 共识算法保证强一致性
- 只有 kube-apiserver 直接访问 etcd（其他组件不直连）
- etcd 集群节点数建议为奇数：3 节点容忍 1 节点故障，5 节点容忍 2 节点故障

**生产部署建议**：

| 场景 | 建议 |
|------|------|
| 开发/测试 | etcd 与控制面同机部署，单节点即可 |
| 生产小规模 | 独立 3 节点 etcd 集群，SSD 存储 |
| 生产大规模 | 独立 5 节点 etcd 集群，专用 SSD，与控制面网络隔离 |

**备份与恢复**（极其重要，etcd 数据丢失 = 集群数据全丢）：

```bash
# 备份 etcd 快照
ETCDCTL_API=3 etcdctl snapshot save /backup/etcd-$(date +%Y%m%d%H%M%S).db \
  --endpoints=https://127.0.0.1:2379 \
  --cacert=/etc/kubernetes/pki/etcd/ca.crt \
  --cert=/etc/kubernetes/pki/etcd/server.crt \
  --key=/etc/kubernetes/pki/etcd/server.key

# 验证备份完整性
ETCDCTL_API=3 etcdctl snapshot status /backup/etcd-20251208.db --write-out=table

# 从快照恢复（需停止 apiserver）
ETCDCTL_API=3 etcdctl snapshot restore /backup/etcd-20251208.db \
  --data-dir=/var/lib/etcd-restore \
  --name=etcd-node1 \
  --initial-cluster=etcd-node1=https://10.0.0.1:2380 \
  --initial-advertise-peer-urls=https://10.0.0.1:2380
```

**监控关键指标**：
- `etcd_server_leader_changes_seen_total`：Leader 切换次数，频繁切换说明网络或磁盘问题
- `etcd_disk_wal_fsync_duration_seconds`：WAL 刷盘延迟，SSD 建议 < 10ms
- `etcd_mvcc_db_total_size_in_bytes`：数据库大小，建议不超过 8GB

### 3.3 kube-scheduler

**职责**：为新创建的（未绑定节点的）Pod 选择合适的 Worker Node。

**调度决策流程**：

```
待调度 Pod 进入队列
        │
        ▼
┌───────────────────┐
│  Filter（过滤）    │  筛选出所有"可行"节点
│  - NodeSelector   │
│  - Taints/Tolerations│
│  - 资源是否充足    │
│  - PodAffinity    │
│  - NodeAffinity   │
│  - HostPort 冲突  │
└────────┬──────────┘
         │  可行节点列表
         ▼
┌───────────────────┐
│  Score（打分）     │  对可行节点评分 0-100
│  - LeastRequested │  资源剩余越多分越高
│  - NodeAffinity   │  软亲和加分
│  - ImageLocality  │  节点已有镜像加分
│  - InterPod*      │  Pod 间亲和/反亲和
└────────┬──────────┘
         │  最高分节点
         ▼
┌───────────────────┐
│  Bind（绑定）      │  写入 Pod.spec.nodeName
└───────────────────┘
```

**影响调度的主要因素**：

```yaml
# 示例：带完整调度约束的 Pod spec
apiVersion: v1
kind: Pod
metadata:
  name: example-pod
spec:
  # 节点选择器（硬性要求）
  nodeSelector:
    kubernetes.io/arch: amd64

  # 节点亲和性（支持软/硬两种）
  affinity:
    nodeAffinity:
      requiredDuringSchedulingIgnoredDuringExecution:
        nodeSelectorTerms:
          - matchExpressions:
              - key: node-role
                operator: In
                values: ["worker"]
      preferredDuringSchedulingIgnoredDuringExecution:
        - weight: 100
          preference:
            matchExpressions:
              - key: topology.kubernetes.io/zone
                operator: In
                values: ["us-west-2a"]

    # Pod 反亲和：同一 Deployment 的 Pod 不调度到同一节点
    podAntiAffinity:
      requiredDuringSchedulingIgnoredDuringExecution:
        - labelSelector:
            matchLabels:
              app: my-service
          topologyKey: kubernetes.io/hostname

  # 容忍污点
  tolerations:
    - key: "dedicated"
      operator: "Equal"
      value: "gpu"
      effect: "NoSchedule"

  containers:
    - name: app
      image: my-app:latest
      resources:
        requests:
          cpu: "500m"
          memory: "512Mi"
        limits:
          cpu: "1"
          memory: "1Gi"
```

### 3.4 kube-controller-manager

**职责**：运行所有内置控制器的进程，每个控制器负责将某类资源的实际状态驱动到期望状态。

**控制循环原理（Reconcile Loop）**：

```
         ┌─────────────────────────────────┐
         │         Controller              │
         │                                 │
         │  Watch(apiserver) → 事件触发     │
         │          │                      │
         │          ▼                      │
         │  读取期望状态 (Spec)              │
         │          │                      │
         │          ▼                      │
         │  读取实际状态 (Status)            │
         │          │                      │
         │          ▼                      │
         │  Diff → 执行操作 → 更新 Status   │
         │          │                      │
         │          └──→ 循环              │
         └─────────────────────────────────┘
```

**内置控制器列表**（常用）：

| 控制器 | 职责 |
|--------|------|
| Deployment Controller | 管理 ReplicaSet，实现滚动更新/回滚 |
| ReplicaSet Controller | 保证指定数量的 Pod 副本运行 |
| StatefulSet Controller | 有序部署/扩缩/删除有状态应用 |
| DaemonSet Controller | 确保每个节点运行一个 Pod 副本 |
| Job Controller | 管理一次性任务，保证完成数 |
| CronJob Controller | 按计划触发 Job |
| Node Controller | 监控节点状态，处理节点不可达 |
| Namespace Controller | 处理 Namespace 删除时的级联清理 |
| Endpoints Controller | 维护 Service 对应的 Endpoints 列表 |
| ServiceAccount Controller | 为新 Namespace 创建默认 ServiceAccount |
| PV Controller | 绑定 PV 与 PVC，处理存储回收 |

**查看控制器日志**：

```bash
# 控制面用 kubeadm 部署时，controller-manager 作为 static pod 运行
kubectl logs -n kube-system kube-controller-manager-<node-name>

# 查看 Deployment 控制器的事件
kubectl describe deployment my-deployment
kubectl get events --field-selector involvedObject.name=my-deployment
```

### 3.5 cloud-controller-manager

**职责**：将 Kubernetes 与具体云厂商的 API 集成，实现云资源的自动管理。

**主要集成能力**：

| 控制器 | 功能 | 示例 |
|--------|------|------|
| Node Controller | 节点注册/删除时同步云资源 | AWS EC2 实例标签同步 |
| Route Controller | 配置云网络路由 | VPC 路由表，Pod CIDR 路由 |
| Service Controller | LoadBalancer 类型 Service 自动创建云 LB | AWS NLB/ALB，阿里云 SLB |

**实际效果**：创建一个 `type: LoadBalancer` 的 Service，cloud-controller-manager 自动在云上创建对应的负载均衡器并配置好转发规则，无需手动操作：

```yaml
apiVersion: v1
kind: Service
metadata:
  name: my-service
  annotations:
    # AWS 特定注解
    service.beta.kubernetes.io/aws-load-balancer-type: "nlb"
    service.beta.kubernetes.io/aws-load-balancer-internal: "true"
spec:
  type: LoadBalancer
  selector:
    app: my-app
  ports:
    - port: 80
      targetPort: 8080
```

---

## 4. 工作节点组件详解

### 4.1 kubelet

**职责**：运行在每个 Worker Node 上，是节点的"代理"，负责管理该节点上所有 Pod 的生命周期。

**核心职责**：
1. 向 apiserver 注册节点，定期汇报节点状态（心跳）
2. Watch apiserver，获取调度到本节点的 Pod 定义
3. 调用 CRI（容器运行时接口）创建/删除容器
4. 调用 CNI（容器网络接口）配置 Pod 网络
5. 调用 CSI（容器存储接口）挂载持久卷
6. 执行 liveness/readiness/startup probe，处理探针失败

**Pod 生命周期管理**：

```
apiserver 下发 Pod 定义
        │
        ▼
kubelet 接收到 Pod (PodAdmission)
        │
        ▼
拉取镜像 (ImagePull via CRI)
        │
        ▼
创建 Pause 容器 (infra container, 持有 network namespace)
        │
        ▼
CNI 为 Pause 容器配置网络 (分配 Pod IP)
        │
        ▼
创建 Init 容器 (按顺序，逐个完成)
        │
        ▼
创建业务容器 (并发启动)
        │
        ▼
执行 PostStart Hook (若配置)
        │
        ▼
Startup Probe → Readiness Probe → Liveness Probe (持续运行)
        │
  Pod 正常运行中
        │
  收到删除信号
        │
        ▼
执行 PreStop Hook (若配置，等待完成)
        │
        ▼
发送 SIGTERM 信号给容器进程
        │
        ▼
等待 terminationGracePeriodSeconds (默认 30s)
        │
        ▼
发送 SIGKILL（若进程仍存在）
        │
        ▼
CNI 清理网络，CSI 卸载存储
```

**生产关键配置**：

```yaml
# Pod 中关于优雅终止的配置
spec:
  terminationGracePeriodSeconds: 60  # 根据应用实际停止时间调整
  containers:
    - name: app
      lifecycle:
        preStop:
          exec:
            command: ["/bin/sh", "-c", "sleep 5"]  # 给 LB 摘流时间
      livenessProbe:
        httpGet:
          path: /health
          port: 8080
        initialDelaySeconds: 30
        periodSeconds: 10
        failureThreshold: 3
      readinessProbe:
        httpGet:
          path: /ready
          port: 8080
        initialDelaySeconds: 5
        periodSeconds: 5
        failureThreshold: 2
```

### 4.2 kube-proxy

**职责**：运行在每个节点上，维护节点的网络规则，实现 Service 的流量转发（ClusterIP/NodePort/LoadBalancer）。

**工作模式对比**：

| 特性 | iptables 模式 | ipvs 模式 |
|------|--------------|-----------|
| 实现方式 | iptables 规则链 | Linux IPVS（内核级 LVS） |
| 规则复杂度 | O(n) 线性，规则量随 Service 增长 | O(1) hash 表，性能稳定 |
| 负载均衡算法 | 随机（概率分配） | rr/lc/dh/sh/sed/nq 多种 |
| 适用场景 | Service 数量 < 1000 | Service 数量大，高性能要求 |
| 依赖 | 无额外依赖 | 需要内核模块 ip_vs |
| 连接跟踪 | 依赖 conntrack | 依赖 conntrack |
| 默认模式 | 是（老版本） | 推荐切换 |

**切换到 ipvs 模式**：

```bash
# 确认内核模块
lsmod | grep ip_vs
# 若没有，加载模块
modprobe ip_vs
modprobe ip_vs_rr
modprobe ip_vs_wrr
modprobe ip_vs_sh

# 修改 kube-proxy ConfigMap
kubectl edit configmap kube-proxy -n kube-system
# 将 mode: "" 改为 mode: "ipvs"

# 重启 kube-proxy DaemonSet
kubectl rollout restart daemonset kube-proxy -n kube-system

# 验证 ipvs 规则
ipvsadm -ln
```

**Service 流量转发原理（iptables 模式简示）**：

```
Pod 发出请求 → ClusterIP:Port
        │
        ▼ iptables PREROUTING
KUBE-SERVICES chain
        │
        ▼ 匹配 ClusterIP
KUBE-SVC-XXXX chain (按概率 DNAT 到某个 Pod)
        │
        ▼
KUBE-SEP-YYYY chain (DNAT: ClusterIP → PodIP:Port)
        │
        ▼
实际 Pod 收到请求
```

### 4.3 Container Runtime（容器运行时）

**职责**：实际负责容器的创建、启动、停止、删除，kubelet 通过 CRI 接口与其通信。

**CRI（Container Runtime Interface）**：kubelet 与容器运行时之间的标准接口（gRPC），使 kubelet 不依赖具体实现。

**主流运行时对比**：

| 特性 | containerd | CRI-O | Docker (via cri-dockerd) |
|------|-----------|-------|--------------------------|
| CNCF 项目 | 是 | 是 | 否 |
| 轻量程度 | 轻量 | 最轻量 | 较重 |
| 镜像兼容性 | OCI 标准 | OCI 标准 | OCI 标准 |
| 生产使用 | 最广泛 | OpenShift 默认 | 逐渐淘汰 |
| K8s 1.24+ | 直接支持 | 直接支持 | 需额外 shim |
| 调试工具 | crictl, nerdctl | crictl | docker CLI |

> K8s 1.24 起正式移除了内置的 dockershim，Docker 作为运行时需要通过 cri-dockerd 桥接。新集群推荐直接使用 containerd。

**常用 containerd 调试命令**：

```bash
# 安装 crictl（CRI 调试工具）
# 配置指向 containerd socket
cat > /etc/crictl.yaml <<EOF
runtime-endpoint: unix:///run/containerd/containerd.sock
image-endpoint: unix:///run/containerd/containerd.sock
timeout: 30
EOF

# 列出运行中的容器
crictl ps

# 列出所有镜像
crictl images

# 查看容器日志
crictl logs <container-id>

# 查看 Pod 详情
crictl inspect <container-id>

# 拉取镜像
crictl pull nginx:1.25
```

---

## 5. 核心资源对象速览

### 5.1 工作负载资源

| 资源对象 | 作用 | 常用场景 |
|---------|------|---------|
| **Pod** | 最小部署单元，一组共享网络/存储的容器 | 直接使用较少，通常由上层控制器管理 |
| **ReplicaSet** | 保证指定数量的 Pod 副本运行 | 通常不直接创建，由 Deployment 管理 |
| **Deployment** | 管理无状态应用，支持滚动更新/回滚 | Web 服务、API 服务等无状态应用 |
| **StatefulSet** | 有序部署，稳定网络标识，稳定存储 | 数据库、Kafka、ZooKeeper 等有状态应用 |
| **DaemonSet** | 每个节点运行一个 Pod | 日志采集（Fluentd）、监控（node-exporter）、CNI 插件 |
| **Job** | 一次性任务，保证成功完成 N 次 | 数据迁移、批量处理、初始化脚本 |
| **CronJob** | 按 cron 表达式定期创建 Job | 定时报表、定时清理、定时备份 |

### 5.2 服务与网络资源

| 资源对象 | 作用 | 常用场景 |
|---------|------|---------|
| **Service** | 为一组 Pod 提供稳定的访问入口 | 内部服务发现、LoadBalancer 暴露外部 |
| **Ingress** | HTTP/HTTPS 七层路由规则 | 多个服务共享一个 LB，基于域名/路径路由 |
| **Endpoints** | Service 对应的后端 Pod IP 列表 | 手动管理时用于接入集群外部服务 |
| **EndpointSlice** | Endpoints 的分片实现，大规模下性能更好 | K8s 1.17+ 自动使用 |
| **NetworkPolicy** | 定义 Pod 间的网络访问规则 | 微服务安全隔离，限制东西向流量 |

### 5.3 配置与存储资源

| 资源对象 | 作用 | 常用场景 |
|---------|------|---------|
| **ConfigMap** | 存储非敏感配置数据（键值/文件） | 应用配置文件、环境变量 |
| **Secret** | 存储敏感数据（base64 编码，可对接 KMS） | 密码、证书、镜像拉取凭证 |
| **PersistentVolume (PV)** | 集群层面的存储资源 | 管理员预先配置或动态供应的存储 |
| **PersistentVolumeClaim (PVC)** | Pod 对存储的请求声明 | 应用申请存储，与具体存储解耦 |
| **StorageClass** | 定义存储的"类型"和供应方式 | 动态 PV 供应，区分 SSD/HDD/NFS |

### 5.4 访问控制资源

| 资源对象 | 作用 | 常用场景 |
|---------|------|---------|
| **Namespace** | 集群内的逻辑隔离单元 | 多团队/多环境隔离 |
| **ServiceAccount** | Pod 的身份标识，用于访问 apiserver | 为应用赋予最小权限 |
| **Role** | Namespace 级别的权限定义 | 限定某命名空间内的操作权限 |
| **ClusterRole** | 集群级别的权限定义 | 跨命名空间或集群级别资源权限 |
| **RoleBinding** | 将 Role 绑定到用户/组/ServiceAccount | Namespace 内授权 |
| **ClusterRoleBinding** | 将 ClusterRole 绑定到主体 | 集群管理员权限授予 |

### 5.5 自动伸缩与稳定性资源

| 资源对象 | 作用 | 常用场景 |
|---------|------|---------|
| **HPA** (HorizontalPodAutoscaler) | 基于指标自动水平扩缩 Pod 数 | CPU/内存/自定义指标驱动弹性扩缩 |
| **VPA** (VerticalPodAutoscaler) | 自动调整 Pod 的 resource requests | 优化资源利用率（谨慎用于生产） |
| **PodDisruptionBudget (PDB)** | 限制同时中断的 Pod 数量 | 滚动更新/节点维护时保证可用性 |
| **PriorityClass** | 定义 Pod 调度优先级 | 关键服务高优先级，确保资源抢占 |

**PDB 示例**（生产中关键服务必须配置）：

```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: my-service-pdb
  namespace: production
spec:
  # 至少保证 2 个 Pod 可用
  minAvailable: 2
  # 或者用百分比：maxUnavailable: 25%
  selector:
    matchLabels:
      app: my-service
```

**HPA 示例**：

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: my-service-hpa
  namespace: production
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: my-service
  minReplicas: 3
  maxReplicas: 20
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 70
    - type: Resource
      resource:
        name: memory
        target:
          type: Utilization
          averageUtilization: 80
  behavior:
    scaleDown:
      stabilizationWindowSeconds: 300  # 缩容冷却 5 分钟，避免抖动
      policies:
        - type: Percent
          value: 20
          periodSeconds: 60
    scaleUp:
      stabilizationWindowSeconds: 30
      policies:
        - type: Percent
          value: 100
          periodSeconds: 30
```

---

## 6. 请求链路追踪：kubectl apply 背后发生了什么

以 `kubectl apply -f deployment.yaml` 为例，追踪完整链路：

```
Step 1: kubectl 读取本地 kubeconfig（~/.kube/config）
        确定 apiserver 地址、证书、当前 context

Step 2: kubectl 发送 HTTP PATCH 请求到 apiserver
        POST /apis/apps/v1/namespaces/default/deployments
        Body: Deployment 的 JSON 定义
        Header: 证书或 Token 认证

Step 3: apiserver 认证（AuthN）
        验证客户端证书或 Bearer Token
        确认请求者身份（如 admin 用户）

Step 4: apiserver 授权（AuthZ）
        RBAC 检查：admin 是否有 create deployments 权限
        通过则继续，否则返回 403

Step 5: apiserver 准入控制（Admission）
        MutatingWebhook：注入 sidecar、设置默认值（如默认 requests/limits）
        ValidatingWebhook：校验资源定义合法性
        内置准入：ResourceQuota 检查命名空间配额

Step 6: apiserver 将 Deployment 对象写入 etcd
        对象获得 resourceVersion、uid 等元数据

Step 7: Deployment Controller 感知到新 Deployment（Watch 事件）
        计算需要创建的 ReplicaSet
        创建 ReplicaSet 对象（写入 etcd via apiserver）

Step 8: ReplicaSet Controller 感知到新 ReplicaSet
        计算需要创建 N 个 Pod
        创建 Pod 对象（nodeName 为空，写入 etcd via apiserver）

Step 9: kube-scheduler 感知到未调度的 Pod（Watch 事件）
        执行 Filter → Score → Bind
        选定节点，更新 Pod.spec.nodeName（写入 etcd via apiserver）

Step 10: 目标节点的 kubelet 感知到分配给自己的 Pod（Watch 事件）
         调用 CRI（containerd）拉取镜像
         创建 Pause 容器，调用 CNI 分配 Pod IP
         创建 Init 容器（按顺序）
         创建业务容器

Step 11: kubelet 更新 Pod Status（写入 etcd via apiserver）
         phase: Running, conditions: Ready: True

Step 12: Endpoints Controller 感知到 Pod Ready
         将 Pod IP 加入对应 Service 的 Endpoints

Step 13: kube-proxy 感知到 Endpoints 变更
         更新节点上的 iptables/ipvs 规则
         新 Pod 开始接收流量
```

整个过程从 `kubectl apply` 到 Pod 开始接收流量，正常情况下 10-60 秒（取决于镜像大小和资源充裕程度）。

---

## 7. 生产实践要点

### 7.1 etcd 备份策略

etcd 是整个集群的"大脑"，数据丢失无法恢复（集群中所有 Deployment、Service、Secret 等全部消失），必须认真对待备份。

```bash
#!/bin/bash
# etcd 定时备份脚本，建议每小时执行一次
# 配合 cron: 0 * * * * /usr/local/bin/etcd-backup.sh

BACKUP_DIR="/data/etcd-backups"
RETAIN_DAYS=7
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="${BACKUP_DIR}/etcd-snapshot-${TIMESTAMP}.db"

mkdir -p "${BACKUP_DIR}"

ETCDCTL_API=3 etcdctl snapshot save "${BACKUP_FILE}" \
  --endpoints=https://127.0.0.1:2379 \
  --cacert=/etc/kubernetes/pki/etcd/ca.crt \
  --cert=/etc/kubernetes/pki/etcd/server.crt \
  --key=/etc/kubernetes/pki/etcd/server.key

# 验证备份
if ETCDCTL_API=3 etcdctl snapshot status "${BACKUP_FILE}" > /dev/null 2>&1; then
  echo "[OK] etcd backup succeeded: ${BACKUP_FILE}"
  # 同步到远端存储（S3/OSS）
  aws s3 cp "${BACKUP_FILE}" "s3://my-k8s-backup/etcd/${TIMESTAMP}.db"
else
  echo "[ERROR] etcd backup failed!"
  exit 1
fi

# 清理超过 7 天的本地备份
find "${BACKUP_DIR}" -name "etcd-snapshot-*.db" -mtime "+${RETAIN_DAYS}" -delete
```

### 7.2 apiserver 高可用

生产环境 apiserver 至少 2 副本，kubeadm 部署的控制面节点建议 3 个：

```
                    ┌─────────────────┐
                    │   Internal LB   │
                    │  (NLB / HAProxy)│
                    │  :6443          │
                    └────────┬────────┘
                             │
           ┌─────────────────┼─────────────────┐
           ▼                 ▼                 ▼
  ┌─────────────┐   ┌─────────────┐   ┌─────────────┐
  │ apiserver-1 │   │ apiserver-2 │   │ apiserver-3 │
  │ :6443       │   │ :6443       │   │ :6443       │
  └─────────────┘   └─────────────┘   └─────────────┘
           │                 │                 │
           └─────────────────┼─────────────────┘
                             │
                    ┌────────▼────────┐
                    │   etcd 集群      │
                    │  (3 or 5 节点)   │
                    └─────────────────┘
```

kubeconfig 中的 server 指向 LB 地址，任一 apiserver 实例故障，LB 自动摘除，不影响集群操作。

### 7.3 资源限制必须设置

不设置 `resources.requests` 和 `resources.limits` 是生产事故的重要来源之一：

```yaml
# 错误示例：不设置资源限制
containers:
  - name: app
    image: my-app:latest
    # 没有 resources 字段 → QoS 为 BestEffort → 节点资源紧张时第一个被驱逐

# 正确示例：明确设置资源
containers:
  - name: app
    image: my-app:latest
    resources:
      requests:        # 调度依据，保证这么多资源
        cpu: "500m"
        memory: "512Mi"
      limits:          # 硬性上限，超过 CPU 被限速，超过内存被 OOM Kill
        cpu: "1"
        memory: "1Gi"
```

**QoS 等级与驱逐优先级**：

| QoS 等级 | 条件 | 驱逐优先级 |
|---------|------|----------|
| Guaranteed | requests == limits（所有容器） | 最后被驱逐 |
| Burstable | requests < limits，或部分设置 | 中等 |
| BestEffort | 未设置任何 requests/limits | 最先被驱逐 |

**使用 LimitRange 设置命名空间默认值**：

```yaml
apiVersion: v1
kind: LimitRange
metadata:
  name: default-limits
  namespace: production
spec:
  limits:
    - type: Container
      default:           # 默认 limits
        cpu: "500m"
        memory: "512Mi"
      defaultRequest:    # 默认 requests
        cpu: "100m"
        memory: "128Mi"
      max:               # 最大值
        cpu: "4"
        memory: "8Gi"
```

**使用 ResourceQuota 限制命名空间总用量**：

```yaml
apiVersion: v1
kind: ResourceQuota
metadata:
  name: production-quota
  namespace: production
spec:
  hard:
    requests.cpu: "20"
    requests.memory: "40Gi"
    limits.cpu: "40"
    limits.memory: "80Gi"
    count/pods: "100"
    count/services: "20"
    count/persistentvolumeclaims: "30"
```

### 7.4 命名空间隔离策略

多团队共享集群时，命名空间隔离是关键：

```yaml
# NetworkPolicy：禁止跨命名空间访问（默认拒绝所有入站）
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: deny-cross-namespace
  namespace: team-a
spec:
  podSelector: {}  # 匹配所有 Pod
  policyTypes:
    - Ingress
    - Egress
  ingress:
    # 只允许来自同命名空间的流量
    - from:
        - podSelector: {}
    # 允许来自 monitoring 命名空间的 Prometheus 抓取
    - from:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: monitoring
      ports:
        - port: 9090
          protocol: TCP
  egress:
    # 允许访问 kube-dns
    - to:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: kube-system
      ports:
        - port: 53
          protocol: UDP
        - port: 53
          protocol: TCP
    # 允许访问同命名空间
    - to:
        - podSelector: {}
    # 允许访问外网（视需要开放）
    - to:
        - ipBlock:
            cidr: 0.0.0.0/0
            except:
              - 10.0.0.0/8
              - 172.16.0.0/12
              - 192.168.0.0/16
```

**RBAC 最小权限原则**：

```yaml
# 为应用 ServiceAccount 授予最小权限
apiVersion: v1
kind: ServiceAccount
metadata:
  name: my-service-account
  namespace: production

---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: my-service-role
  namespace: production
spec:
  rules:
    # 只允许读取自己命名空间的 ConfigMap 和 Secret
    - apiGroups: [""]
      resources: ["configmaps", "secrets"]
      verbs: ["get", "list", "watch"]
    # 允许读取 Pod 信息（用于服务发现）
    - apiGroups: [""]
      resources: ["pods"]
      verbs: ["get", "list"]

---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: my-service-rolebinding
  namespace: production
subjects:
  - kind: ServiceAccount
    name: my-service-account
    namespace: production
roleRef:
  kind: Role
  name: my-service-role
  apiGroup: rbac.authorization.k8s.io
```

### 7.5 常用生产排障命令

```bash
# 查看集群整体状态
kubectl get nodes -o wide
kubectl get pods -A | grep -v Running

# 查看 Pod 异常原因
kubectl describe pod <pod-name> -n <namespace>
kubectl logs <pod-name> -n <namespace> --previous  # 查看上一次崩溃的日志
kubectl logs <pod-name> -n <namespace> -c <container>  # 多容器 Pod 指定容器

# 进入 Pod 内部调试
kubectl exec -it <pod-name> -n <namespace> -- /bin/sh

# 查看节点资源使用情况
kubectl top nodes
kubectl top pods -A --sort-by=memory

# 查看事件（按时间排序）
kubectl get events -n <namespace> --sort-by='.lastTimestamp'

# 强制删除卡住的 Pod（谨慎！有状态应用慎用）
kubectl delete pod <pod-name> -n <namespace> --grace-period=0 --force

# 查看 Pod 调度失败原因
kubectl get events -n <namespace> | grep Warning | grep FailedScheduling

# 临时扩缩 Deployment 副本数
kubectl scale deployment <name> -n <namespace> --replicas=5

# 查看 Deployment 滚动更新状态
kubectl rollout status deployment/<name> -n <namespace>

# 回滚 Deployment 到上一版本
kubectl rollout undo deployment/<name> -n <namespace>

# 查看 Deployment 历史版本
kubectl rollout history deployment/<name> -n <namespace>
```

---

## 8. 参考链接

- [Kubernetes 官方文档](https://kubernetes.io/docs/home/)
- [Kubernetes 架构概览](https://kubernetes.io/docs/concepts/overview/components/)
- [kube-apiserver 参考](https://kubernetes.io/docs/reference/command-line-tools-reference/kube-apiserver/)
- [etcd 官方文档](https://etcd.io/docs/)
- [etcd 操作指南](https://etcd.io/docs/v3.5/op-guide/)
- [Kubernetes 调度框架](https://kubernetes.io/docs/concepts/scheduling-eviction/scheduling-framework/)
- [Kubernetes RBAC 授权](https://kubernetes.io/docs/reference/access-authn-authz/rbac/)
- [NetworkPolicy 文档](https://kubernetes.io/docs/concepts/services-networking/network-policies/)
- [HPA 文档](https://kubernetes.io/docs/tasks/run-application/horizontal-pod-autoscale/)
- [容器运行时接口（CRI）](https://kubernetes.io/docs/concepts/architecture/cri/)
- [containerd 官方文档](https://containerd.io/docs/)
