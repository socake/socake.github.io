---
title: "Kubernetes v1.33 新特性深度解读：GA 特性全览与升级指南"
date: 2026-04-12T10:00:00+08:00
draft: false
tags: ["Kubernetes", "v1.33", "云原生", "容器编排", "升级"]
categories: ["Kubernetes"]
description: "深入解读 Kubernetes v1.33 版本的核心 GA 特性，包括 In-Place Pod Vertical Scaling、Sidecar Containers、Pod Scheduling Readiness 等，附完整配置示例和升级注意事项。"
summary: "Kubernetes v1.33 带来了多项重量级 GA 特性，本文深入解读 In-Place Pod Vertical Scaling、原生 Sidecar Containers、Pod Scheduling Readiness、KMS v2 加密等核心变更，并提供实际可用的配置示例和生产升级建议。"
toc: true
math: false
diagram: false
keywords: ["Kubernetes v1.33", "In-Place Pod Vertical Scaling", "Sidecar Containers", "Pod Scheduling Readiness", "KMS v2", "Volume Groups Snapshot"]
params:
  reading_time: true
---

Kubernetes v1.33 在 2025 年 4 月发布，这一版把几个磨了好几代的特性升到了 GA。挑几个我觉得做运维会直接用到的拆开说说。

---

## In-Place Pod Vertical Scaling（GA）

### 背景与痛点

传统 Kubernetes 调整 Pod 的 CPU 或内存 Requests/Limits，唯一办法是删除旧 Pod、创建新 Pod。这对**有状态服务**是个严重问题：数据库实例、缓存服务、长连接 WebSocket 服务，每次扩容都意味着连接中断和短暂不可用。

即使配合 PDB（Pod Disruption Budget）和滚动更新，调整资源规格也至少需要一轮 Pod 替换，这在业务高峰期是不可接受的操作窗口。

### 功能说明

In-Place Pod Vertical Scaling 允许在不重启 Pod 的情况下，动态调整已运行 Pod 的 CPU 和内存资源配额。核心机制是：

- 修改 `spec.containers[].resources` 中的 `requests` 和 `limits`
- kubelet 通过 CRI 接口向容器运行时发送 `UpdateContainerResources` 调用
- 对于 CPU，内核 cgroup 的 `cpu.shares` / `cpu.cfs_quota_us` 即时更新，**无需重启**
- 对于内存，同样更新 cgroup `memory.limit_in_bytes`，但内存缩减存在限制（见注意事项）

每个容器新增 `resizePolicy` 字段，声明各资源类型的调整策略：

```yaml
resizePolicy:
  - resourceName: cpu
    restartPolicy: NotRequired   # CPU 调整无需重启
  - resourceName: memory
    restartPolicy: RestartContainer  # 内存调整需要重启容器
```

### 完整配置示例

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: mysql-standalone
  namespace: production
spec:
  containers:
  - name: mysql
    image: mysql:8.0
    resources:
      requests:
        cpu: "2"
        memory: "4Gi"
      limits:
        cpu: "4"
        memory: "8Gi"
    resizePolicy:
    - resourceName: cpu
      restartPolicy: NotRequired
    - resourceName: memory
      restartPolicy: RestartContainer
    env:
    - name: MYSQL_ROOT_PASSWORD
      valueFrom:
        secretKeyRef:
          name: mysql-secret
          key: password
```

运行时调整资源（直接 patch spec 即可）：

```bash
# 在线扩容 CPU，不重启 Pod
kubectl patch pod mysql-standalone -n production --type=json -p='[
  {
    "op": "replace",
    "path": "/spec/containers/0/resources/requests/cpu",
    "value": "3"
  },
  {
    "op": "replace",
    "path": "/spec/containers/0/resources/limits/cpu",
    "value": "6"
  }
]'

# 查看调整状态
kubectl get pod mysql-standalone -n production -o jsonpath='{.status.resize}'
# 输出: InProgress -> Deferred -> Infeasible -> "" (成功)
```

Pod status 新增 `resize` 字段反映调整进度：

```yaml
status:
  resize: ""          # 空字符串表示调整完成或无进行中的调整
  containerStatuses:
  - name: mysql
    allocatedResources:
      cpu: "3"        # 实际分配的资源（可能与 spec 不同步）
      memory: "4Gi"
    resources:
      requests:
        cpu: "3"
        memory: "4Gi"
```

### 与 VPA 的关系

In-Place 特性本身是底层机制，VPA（Vertical Pod Autoscaler）在 Updater 组件中已开始利用此特性实现"原地更新"模式，减少 Pod 驱逐次数。生产中推荐组合使用：VPA 负责分析和推荐，In-Place 负责无损执行。

### 注意事项

1. **内存缩减风险**：缩减内存 Limit 时，如果容器实际使用量超过新 Limit，内核的 OOM Killer 会立即介入。建议只在确认实际内存用量后再缩减。
2. **cgroup v1 限制**：内存的 In-Place 调整在 cgroup v1 上行为存在差异，强烈建议升级到 cgroup v2（v1.25+ 默认启用）。
3. **QoS 类不变**：调整不能改变 Pod 的 QoS 类别（Guaranteed/Burstable/BestEffort），若调整后 requests == limits 但原来不是，QoS 类仍维持原状。
4. **Deployment/StatefulSet 支持**：通过更新 Pod Template 中的资源配置，Deployment 控制器默认仍会触发滚动更新。若要利用 In-Place，需通过直接 patch Pod（适用于有状态场景）或等待 workload controller 对该特性的原生支持。

---

## Sidecar Containers（GA）

### 背景与痛点

在 v1.33 之前，"sidecar 模式"只是社区约定，在实现层面上 sidecar 和普通 initContainer 没有区别。这导致了两个经典问题：

1. **启动顺序**：sidecar（如日志采集器、服务网格代理）需要先于主容器启动，但 initContainer 必须执行完成才能启动下一个，无法实现"先启动但保持运行"。
2. **优雅退出**：Job 场景中，sidecar 不知道主容器何时完成，导致 Job 永远无法 Complete（Istio envoy sidecar 注入 Job 的经典问题）。

### 功能说明

v1.28 引入、v1.33 GA 的原生 Sidecar 通过在 `initContainers` 中新增 `restartPolicy: Always` 字段实现：

- **原生启动顺序**：`restartPolicy: Always` 的 initContainer 会在普通 initContainer 之后、主容器之前启动，且不等待其"完成"（因为它一直运行）
- **优雅退出**：所有普通容器（主容器）退出后，sidecar 才会收到 SIGTERM
- **探针支持**：原生 sidecar 支持 `startupProbe`，下一个 initContainer 或主容器要等 sidecar 的 startupProbe 通过才启动

### 完整配置示例

**场景一：日志采集 sidecar（确保采集器先于业务启动）**

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: app-with-logging
  namespace: production
spec:
  initContainers:
  # 原生 sidecar：在主容器之前启动，与主容器同生命周期
  - name: log-collector
    image: fluent/fluent-bit:3.0
    restartPolicy: Always          # 这是关键字段
    volumeMounts:
    - name: log-volume
      mountPath: /logs
    - name: fluent-bit-config
      mountPath: /fluent-bit/etc
    startupProbe:
      httpGet:
        path: /api/v1/health
        port: 2020
      initialDelaySeconds: 3
      periodSeconds: 5
      failureThreshold: 10
  containers:
  - name: app
    image: myapp:v2.0
    volumeMounts:
    - name: log-volume
      mountPath: /var/log/app
  volumes:
  - name: log-volume
    emptyDir: {}
  - name: fluent-bit-config
    configMap:
      name: fluent-bit-config
```

**场景二：Job 中的 Istio sidecar 问题修复**

之前 Istio 注入 Job 后，envoy sidecar 不会退出导致 Job 卡住。使用原生 sidecar 后：

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: data-migration
spec:
  template:
    spec:
      initContainers:
      - name: istio-proxy
        image: istio/proxyv2:1.21.0
        restartPolicy: Always
        args: ["proxy", "sidecar"]
        env:
        - name: ISTIO_META_WORKLOAD_NAME
          value: data-migration
      containers:
      - name: migrator
        image: myapp/migrator:v1.0
        command: ["./migrate.sh"]
      restartPolicy: Never
```

Job 的主容器 `migrator` 完成后，`istio-proxy` sidecar 会自动收到终止信号，Job 正常进入 `Complete` 状态。

### 注意事项

1. **与 Istio/Linkerd 自动注入的兼容性**：服务网格的 Mutating Webhook 可能还在注入老式 sidecar（普通 container），需确认服务网格版本是否已切换到原生 sidecar 注入（Istio 1.21+ 支持）。
2. **资源计算**：原生 sidecar 的资源会计入 Pod 总资源，影响调度决策和 LimitRange 检查。
3. **优先级**：多个原生 sidecar 按定义顺序依次启动，每个 sidecar 的 `startupProbe` 通过后才启动下一个。

---

## Job 改进：Backoff Limit Per Index 与 Pod Failure Policy

### Backoff Limit Per Index（GA）

Indexed Job 中，每个 index（任务分片）现在可以有独立的失败重试次数，而不是整个 Job 共用一个 `backoffLimit`。

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: batch-processor
spec:
  completions: 100
  parallelism: 10
  completionMode: Indexed
  backoffLimitPerIndex: 3    # 每个 index 最多重试 3 次
  maxFailedIndexes: 10       # 超过 10 个 index 失败后，整个 Job 失败
  template:
    spec:
      containers:
      - name: processor
        image: myapp/processor:v1
        env:
        - name: JOB_COMPLETION_INDEX
          valueFrom:
            fieldRef:
              fieldPath: metadata.annotations['batch.kubernetes.io/job-completion-index']
      restartPolicy: Never
```

**使用场景**：大规模批处理（ML 训练数据预处理、报表生成等），部分任务因数据问题必然失败，不应该因为少数分片失败导致整个 Job 重试风暴。

### Pod Failure Policy（GA）

精细控制 Pod 失败后的处理行为，支持基于退出码和容器状态进行规则匹配：

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: ml-training
spec:
  backoffLimit: 6
  podFailurePolicy:
    rules:
    # OOM 导致的失败：立即终止整个 Job，不重试（资源不足）
    - action: FailJob
      onPodConditions:
      - type: DisruptionTarget
    # 退出码 42 表示数据错误：忽略该次失败，不计入 backoffLimit
    - action: Ignore
      onExitCodes:
        containerName: trainer
        operator: In
        values: [42]
    # 退出码 1：正常计入重试
    - action: Count
      onExitCodes:
        containerName: trainer
        operator: In
        values: [1]
  template:
    spec:
      containers:
      - name: trainer
        image: myapp/trainer:v1
      restartPolicy: Never
```

---

## Pod Scheduling Readiness（GA）

### 功能说明

通过 `schedulingGates` 字段，可以阻止 Pod 进入调度队列，直到外部控制器移除所有 gate。这对以下场景非常有价值：

- **依赖预热**：等待 ConfigMap/Secret 准备好再调度（避免调度后启动失败）
- **配额预留**：先占位，待外部资源（GPU、特殊硬件）确认可用后再正式调度
- **批量调度协调**：多个 Pod 协调后一起进入调度队列，避免碎片化占用节点

### 配置示例

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: gpu-workload
  namespace: ml-team
spec:
  schedulingGates:
  - name: "example.com/gpu-quota-reserved"   # 自定义 gate 名称
  - name: "example.com/dataset-ready"
  containers:
  - name: trainer
    image: pytorch/pytorch:2.2
    resources:
      limits:
        nvidia.com/gpu: "4"
```

外部控制器确认资源就绪后，移除 gate：

```bash
# 移除单个 gate
kubectl patch pod gpu-workload -n ml-team --type=json -p='[
  {
    "op": "remove",
    "path": "/spec/schedulingGates/0"
  }
]'

# 当所有 gate 都被移除后，Pod 才进入调度队列
```

**注意**：`schedulingGates` 只能移除，不能新增（Pod 创建后）。gate 名称需符合域名格式，推荐使用公司域名前缀。

---

## Volume Groups Snapshot（Beta）

### 功能说明

跨多个 PVC 的原子快照。典型场景：数据库的数据盘和日志盘是两个 PVC，单独快照会有时间差导致数据不一致；`VolumeGroupSnapshot` 保证多个 PVC 在同一时刻被快照。

```yaml
apiVersion: groupsnapshot.storage.k8s.io/v1beta1
kind: VolumeGroupSnapshot
metadata:
  name: mysql-consistent-snapshot
  namespace: production
spec:
  volumeGroupSnapshotClassName: csi-aws-vgs-class
  source:
    selector:
      matchLabels:
        app: mysql
        snapshot-group: data-and-log   # 选择同一组的多个 PVC
---
# 对应的 PVC 需要打上相同标签
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: mysql-data
  labels:
    app: mysql
    snapshot-group: data-and-log
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 100Gi
  storageClassName: gp3-encrypted
```

**当前限制**：需要 CSI 驱动支持 `CREATE_VOLUME_GROUP_SNAPSHOT` 能力，AWS EBS CSI Driver v1.27+、GCE PD CSI Driver v1.12+ 已支持。

---

## KMS v2 GA：更安全的 etcd 数据加密

### 背景

KMS v1 使用同步 gRPC 调用，每个加密操作都是阻塞的，在高写入负载下会成为 apiserver 的性能瓶颈。KMS v2 引入了：

- **异步加密**：通过 `WatchKeys` 流式 RPC 接收密钥更新通知，无需每次请求都调用 KMS
- **密钥缓存**：本地缓存 DEK（Data Encryption Key），性能大幅提升
- **密钥轮换**：支持自动密钥轮换，无需重启 apiserver

### 配置示例（使用 AWS KMS）

`/etc/kubernetes/encryption-config.yaml`：

```yaml
apiVersion: apiserver.config.k8s.io/v1
kind: EncryptionConfiguration
resources:
  - resources:
    - secrets
    - configmaps
    providers:
    - kms:
        apiVersion: v2          # 使用 KMS v2
        name: aws-kms-provider
        endpoint: unix:///var/run/kmsplugin/socket.sock
        timeout: 3s
        cachesize: 1000         # 本地缓存 DEK 数量
    - identity: {}              # 兜底：未加密读取（用于迁移）
```

kube-apiserver 启动参数：

```yaml
- --encryption-provider-config=/etc/kubernetes/encryption-config.yaml
- --encryption-provider-config-automatic-reload=true  # 支持热重载配置
```

**迁移步骤（v1 → v2）**：

```bash
# 1. 先部署支持 v2 的 KMS plugin（兼容 v2 协议）
# 2. 更新 encryption-config，apiVersion 改为 v2
# 3. 执行 Secret 重加密（用新密钥重写所有 Secret）
kubectl get secrets --all-namespaces -o json | \
  kubectl replace -f -

# 4. 验证加密状态
kubectl get --raw='/healthz/etcd-encryption'
```

---

## Node Memory Swap 改进：LimitedSwap 策略

v1.33 对 swap 支持进一步完善，`LimitedSwap` 策略正式稳定：

- **BestEffort Pod**：禁止使用 swap
- **Burstable Pod**：允许按比例使用 swap（`swap_limit = memory_limit * swapRatio`）
- **Guaranteed Pod**：默认禁止（可通过注解开启）

节点配置（kubelet）：

```yaml
# /etc/kubernetes/kubelet-config.yaml
apiVersion: kubelet.config.k8s.io/v1beta1
kind: KubeletConfiguration
memorySwap:
  swapBehavior: LimitedSwap
featureGates:
  NodeSwap: true
```

**使用场景**：内存敏感但允许偶发 swap 的批处理任务，可以减少 OOM Kill 频率，但会带来性能波动。**生产数据库节点不建议开启**。

---

## Structured Authorization Configuration（GA）

### 功能说明

替代单一的 `--authorization-mode` 参数，支持通过配置文件定义多阶段授权链，每个阶段可以是：

- `Node`、`RBAC`：内置授权器
- `Webhook`：外部 webhook（支持配置超时、缓存、失败策略）

并且支持 **CEL 表达式**进行请求预过滤，减少不必要的 webhook 调用。

### 配置示例

`/etc/kubernetes/authz-config.yaml`：

```yaml
apiVersion: apiserver.config.k8s.io/v1alpha1
kind: AuthorizationConfiguration
authorizers:
  # 第一步：节点授权（kubelet 访问自己节点的资源）
  - type: Node
    name: node

  # 第二步：RBAC（标准权限检查）
  - type: RBAC
    name: rbac

  # 第三步：外部 OPA webhook（仅对特定资源调用）
  - type: Webhook
    name: opa-authz
    webhook:
      timeout: 3s
      failurePolicy: Deny          # webhook 不可用时拒绝请求
      matchConditions:
        # CEL 过滤：只有操作 secrets 或 rolebindings 才调用 OPA
        - expression: >
            request.resourceAttributes.resource in ['secrets', 'rolebindings']
      connectionInfo:
        type: KubeConfigFile
        kubeConfigFile: /etc/kubernetes/opa-authz-kubeconfig.yaml
      authorizedTTL: 5m            # 授权结果缓存 5 分钟
      unauthorizedTTL: 30s
```

kube-apiserver 启动参数：

```yaml
- --authorization-config=/etc/kubernetes/authz-config.yaml
# 移除旧参数
# --authorization-mode=Node,RBAC  (由配置文件替代)
```

**迁移注意**：`--authorization-config` 与 `--authorization-mode` 互斥，切换时需同步修改启动参数。

---

## 升级到 v1.33 的注意事项

### API 废弃与移除

| API | 废弃版本 | 移除版本 | 替代 |
|-----|---------|---------|-----|
| `flowcontrol.apiserver.k8s.io/v1beta2` | v1.29 | v1.33 | `v1` |
| `autoscaling/v2beta2` HPA | v1.26 | v1.33 | `autoscaling/v2` |

**升级前必做检查**：

```bash
# 检查集群中是否还在使用废弃 API
kubectl get --raw /metrics | grep apiserver_requested_deprecated_apis

# 使用 pluto 扫描 YAML 文件
pluto detect-files -d ./k8s-manifests --target-versions k8s=v1.33.0

# 使用 kubent（kube-no-trouble）
kubent --target-version 1.33
```

### 升级路径建议

```bash
# 1. 备份 etcd
ETCDCTL_API=3 etcdctl snapshot save /backup/etcd-$(date +%Y%m%d).db

# 2. 升级 control plane（以 kubeadm 为例）
kubeadm upgrade plan v1.33.0
kubeadm upgrade apply v1.33.0

# 3. 逐节点 drain + 升级 kubelet/kubectl
kubectl drain node-01 --ignore-daemonsets --delete-emptydir-data
apt-get install -y kubelet=1.33.0-* kubectl=1.33.0-*
systemctl restart kubelet
kubectl uncordon node-01

# 4. 验证集群健康
kubectl get nodes
kubectl get pods -A | grep -v Running | grep -v Completed
```

### Feature Gate 变更

v1.33 中以下 Feature Gate 已锁定为 `true`（无法关闭）：

- `InPlacePodVerticalScaling`
- `SidecarContainers`
- `PodSchedulingReadiness`
- `KMSv2`
- `StructuredAuthorizationConfiguration`

如果之前通过 Feature Gate 禁用过这些特性，升级后需要相应更新应用逻辑。

---

## 总结

v1.33 里我觉得生产最值得先用的两个：Sidecar Containers（专治 Job + sidecar 那种永远退不掉的问题）和 In-Place Pod Vertical Scaling（配合 VPA 不用再滚 Pod）。KMS v2 和 Structured AuthZ 可以在安全集群配套跟进，Scheduling Gates 目前主要是平台团队会用到。
