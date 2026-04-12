---
title: "Kubernetes 存储体系生产实践：PV/PVC/StorageClass 全解"
date: 2026-04-12T12:00:00+08:00
draft: false
tags: ["Kubernetes", "存储", "PV", "PVC", "StorageClass", "AWS EBS", "EFS"]
categories: ["Kubernetes"]
description: "K8s 存储生产实践：StorageClass 动态供给、AWS EBS/EFS CSI、PVC 扩容与数据迁移"
summary: "从存储基础概念到生产实战，覆盖 StorageClass 动态供给配置、AWS EBS 和 EFS CSI 驱动安装、StatefulSet 存储管理、PVC 在线扩容操作、跨 AZ 挂载失败排查，以及有状态服务数据迁移方案。"
toc: true
math: false
diagram: false
keywords: ["Kubernetes", "StorageClass", "PVC", "EBS", "EFS", "CSI", "StatefulSet", "数据迁移"]
params:
  reading_time: true
---

我第一次遇到 K8s 存储问题是在生产环境——一个 StatefulSet 的 Pod 因为节点故障迁移后，新 Pod 始终处于 Pending 状态，原因是 EBS 卷跨 AZ 挂载失败。从那以后我开始认真研究 K8s 存储体系，这篇文章记录了我踩过的坑和总结的最佳实践。

## 存储基础概念梳理

在深入实战前，先理清三个核心概念的关系：

- **PV（PersistentVolume）**：集群级别的存储资源，由管理员或 CSI 驱动创建，描述实际的存储（EBS 卷、NFS 挂载点等）
- **PVC（PersistentVolumeClaim）**：命名空间级别的存储请求，由用户/应用提交，声明需要多大存储、什么访问模式
- **StorageClass**：存储的"模板"，定义如何动态创建 PV，以及使用哪个 provisioner

**accessModes 是最容易踩坑的地方：**

| 模式 | 含义 | 典型存储 |
|------|------|----------|
| ReadWriteOnce (RWO) | 只能被一个节点读写 | AWS EBS, Azure Disk |
| ReadOnlyMany (ROX) | 可以被多个节点只读 | NFS |
| ReadWriteMany (RWX) | 可以被多个节点读写 | AWS EFS, NFS |
| ReadWriteOncePod (RWOP) | 只能被一个 Pod 读写（K8s 1.22+） | EBS |

**关键理解**：`ReadWriteOnce` 是**节点级别**的限制，不是 Pod 级别。同一个节点上的多个 Pod 可以同时挂载一个 RWO 的 PV。如果你需要严格的 Pod 级别独占，用 `ReadWriteOncePod`。

## StorageClass 动态供给

动态供给是生产环境的标准做法：不需要手动预创建 PV，PVC 提交后 CSI 驱动自动创建对应的存储资源。

**查看集群中的 StorageClass：**

```bash
kubectl get storageclass
# NAME            PROVISIONER             RECLAIMPOLICY   VOLUMEBINDINGMODE      ALLOWVOLUMEEXPANSION
# gp2             kubernetes.io/aws-ebs   Delete          Immediate              false
# gp3 (default)   ebs.csi.aws.com         Delete          WaitForFirstConsumer   true
```

**重要参数说明：**

- `RECLAIMPOLICY`：
  - `Delete`：PVC 删除后，PV 和底层存储（如 EBS 卷）一并删除。**生产慎用**
  - `Retain`：PVC 删除后，PV 保留，需要手动清理。**重要数据推荐**
  - `Recycle`：已废弃，不用
- `VOLUMEBINDINGMODE`：
  - `Immediate`：PVC 创建时立即绑定 PV，不考虑 Pod 调度位置
  - `WaitForFirstConsumer`：等到 Pod 被调度到某个节点后，再在该节点所在 AZ 创建 PV。**多 AZ 集群必须用这个**

**创建自定义 StorageClass：**

```yaml
# gp3 StorageClass（AWS EBS CSI）
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: gp3-retain
  annotations:
    storageclass.kubernetes.io/is-default-class: "false"
provisioner: ebs.csi.aws.com
parameters:
  type: gp3
  iops: "3000"
  throughput: "125"
  encrypted: "true"
  # KMS 加密（可选）
  # kmsKeyId: "arn:aws:kms:us-west-2:123456789:key/xxx"
volumeBindingMode: WaitForFirstConsumer  # 多 AZ 必须
reclaimPolicy: Retain                   # 重要数据保留
allowVolumeExpansion: true              # 允许 PVC 扩容
```

## AWS EBS CSI 驱动配置

旧版的 `kubernetes.io/aws-ebs` in-tree 驱动已经废弃，生产环境必须迁移到 EBS CSI 驱动。

**安装 EBS CSI 驱动（EKS 推荐用插件方式）：**

```bash
# EKS 托管插件安装（推荐）
aws eks create-addon \
  --cluster-name my-cluster \
  --addon-name aws-ebs-csi-driver \
  --service-account-role-arn arn:aws:iam::123456789012:role/AmazonEKS_EBS_CSI_DriverRole

# 验证安装
kubectl get pods -n kube-system -l app=ebs-csi-controller
kubectl get pods -n kube-system -l app=ebs-csi-node
```

**EBS CSI 驱动需要 IAM 权限（IRSA 方式）：**

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ec2:CreateSnapshot",
        "ec2:AttachVolume",
        "ec2:DetachVolume",
        "ec2:ModifyVolume",
        "ec2:DescribeAvailabilityZones",
        "ec2:DescribeInstances",
        "ec2:DescribeSnapshots",
        "ec2:DescribeTags",
        "ec2:DescribeVolumes",
        "ec2:DescribeVolumesModifications",
        "ec2:CreateVolume",
        "ec2:DeleteVolume",
        "ec2:DeleteSnapshot",
        "ec2:CreateTags"
      ],
      "Resource": "*"
    }
  ]
}
```

## AWS EFS CSI 驱动配置

EFS 支持 ReadWriteMany，适合多 Pod 共享文件的场景（如配置文件、上传文件存储）。

```bash
# 安装 EFS CSI 驱动
helm repo add aws-efs-csi-driver https://kubernetes-sigs.github.io/aws-efs-csi-driver/
helm upgrade --install aws-efs-csi-driver aws-efs-csi-driver/aws-efs-csi-driver \
  --namespace kube-system \
  --set controller.serviceAccount.annotations."eks\.amazonaws\.com/role-arn"=arn:aws:iam::123456789012:role/AmazonEKS_EFS_CSI_DriverRole
```

**EFS StorageClass 和 PVC：**

```yaml
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: efs-sc
provisioner: efs.csi.aws.com
parameters:
  provisioningMode: efs-ap         # 使用 EFS Access Point
  fileSystemId: fs-0123456789abcdef  # EFS 文件系统 ID
  directoryPerms: "700"
  basePath: "/apps"
reclaimPolicy: Retain
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: shared-storage
  namespace: my-app
spec:
  accessModes:
    - ReadWriteMany    # EFS 支持多节点读写
  storageClassName: efs-sc
  resources:
    requests:
      storage: 10Gi    # EFS 动态供给时这个值只是声明，实际不限制大小
```

## StatefulSet 存储管理

StatefulSet 的每个 Pod 会有独立的 PVC，通过 `volumeClaimTemplates` 定义：

```yaml
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: postgresql
  namespace: data
spec:
  serviceName: postgresql-headless
  replicas: 3
  selector:
    matchLabels:
      app: postgresql
  template:
    metadata:
      labels:
        app: postgresql
    spec:
      containers:
        - name: postgresql
          image: postgres:15
          volumeMounts:
            - name: data
              mountPath: /var/lib/postgresql/data
            - name: config
              mountPath: /etc/postgresql
  volumeClaimTemplates:
    - metadata:
        name: data
      spec:
        accessModes: ["ReadWriteOnce"]
        storageClassName: gp3-retain
        resources:
          requests:
            storage: 50Gi
```

StatefulSet 会自动创建以 `{pvcName}-{statefulsetName}-{ordinal}` 命名的 PVC：
- `data-postgresql-0`
- `data-postgresql-1`
- `data-postgresql-2`

**重要：** 删除 StatefulSet 时，PVC 不会自动删除（这是保护机制）。需要手动清理：

```bash
# 删除 StatefulSet 但保留 PVC（默认行为）
kubectl delete statefulset postgresql -n data

# 查看残留 PVC
kubectl get pvc -n data -l app=postgresql

# 确认数据已备份后再删除
kubectl delete pvc data-postgresql-0 data-postgresql-1 data-postgresql-2 -n data
```

## PVC 扩容操作

PVC 扩容需要两个前提：StorageClass 开启了 `allowVolumeExpansion: true`，且底层存储支持在线扩容（EBS gp3 支持）。

```bash
# 查看当前 PVC 大小
kubectl get pvc my-data-pvc -n my-app

# 扩容：直接 edit 或者 patch
kubectl patch pvc my-data-pvc -n my-app \
  -p '{"spec":{"resources":{"requests":{"storage":"100Gi"}}}}'

# 监控扩容状态
kubectl get pvc my-data-pvc -n my-app -w
# NAME          STATUS   VOLUME    CAPACITY   ACCESS MODES   STORAGECLASS   AGE
# my-data-pvc   Bound    pvc-xxx   50Gi       RWO            gp3-retain     10d
# my-data-pvc   Bound    pvc-xxx   100Gi      RWO            gp3-retain     10d
```

**文件系统扩容（部分情况需要）：**

某些情况下 EBS 卷扩容后，Pod 内的文件系统还没有扩展，需要重启 Pod 触发 `resize2fs`：

```bash
# 检查 PVC 是否在等待文件系统扩容
kubectl describe pvc my-data-pvc -n my-app | grep -A5 "Conditions"
# Conditions:
#   Type                      Status
#   FileSystemResizePending   True   # 需要重启 Pod

# 重启 Pod 触发文件系统扩容
kubectl rollout restart deployment my-service -n my-app
```

**不能缩容**：K8s 不支持 PVC 缩容，只能扩大不能缩小。

## 数据迁移方案

当需要把数据从一个 PVC 迁移到另一个 PVC（例如换 StorageClass、跨 AZ），常用方法：

**方案1：rsync 同步（适合在线迁移）**

```bash
# 临时启动一个带两个 PVC 的迁移 Pod
kubectl apply -f - <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: data-migration
  namespace: my-app
spec:
  containers:
    - name: migrator
      image: alpine
      command: ["/bin/sh", "-c", "sleep 3600"]
      volumeMounts:
        - name: source
          mountPath: /source
        - name: target
          mountPath: /target
  volumes:
    - name: source
      persistentVolumeClaim:
        claimName: old-data-pvc
    - name: target
      persistentVolumeClaim:
        claimName: new-data-pvc
  restartPolicy: Never
EOF

# 执行迁移
kubectl exec -n my-app data-migration -- \
  sh -c "apk add rsync && rsync -avz /source/ /target/"

# 验证数据完整性
kubectl exec -n my-app data-migration -- \
  sh -c "du -sh /source /target; ls -la /source | md5sum; ls -la /target | md5sum"

# 清理迁移 Pod
kubectl delete pod data-migration -n my-app
```

**方案2：VolumeSnapshot 克隆（AWS EBS 支持）**

```yaml
# 1. 创建快照
apiVersion: snapshot.storage.k8s.io/v1
kind: VolumeSnapshot
metadata:
  name: my-data-snapshot
  namespace: my-app
spec:
  volumeSnapshotClassName: csi-aws-vsc
  source:
    persistentVolumeClaimName: old-data-pvc

---
# 2. 从快照恢复到新 PVC（可以指定不同的 StorageClass）
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: new-data-pvc
  namespace: my-app
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: gp3-retain
  resources:
    requests:
      storage: 50Gi
  dataSource:
    name: my-data-snapshot
    kind: VolumeSnapshot
    apiGroup: snapshot.storage.k8s.io
```

## 常见坑记录

### 坑1：PV 回收策略 Delete 导致数据丢失

删除 PVC 后 EBS 卷被自动删除，这个操作无法恢复。生产环境重要数据的 StorageClass 必须设置 `reclaimPolicy: Retain`。

如果使用了错误的 StorageClass（Delete 策略），补救方法是修改现有 PV 的回收策略：

```bash
# 临时修改 PV 的回收策略（不影响 StorageClass）
kubectl patch pv pvc-abc123 -p '{"spec":{"persistentVolumeReclaimPolicy":"Retain"}}'
```

### 坑2：跨 AZ 挂载失败

EBS 是 AZ 级别的资源，一个 EBS 卷只能挂载到同一个 AZ 内的节点。如果 Pod 被调度到了不同 AZ 的节点，挂载会失败：

```bash
# 排查：查看 Pod 事件
kubectl describe pod my-pod -n my-app | grep -A10 "Events:"
# Warning  FailedAttachVolume  Multi-Attach error: volume "pvc-xxx" is already exclusively attached to node

# 查看 PV 所在 AZ
kubectl get pv pvc-xxx -o jsonpath='{.spec.nodeAffinity}'
# {"required":{"nodeSelectorTerms":[{"matchExpressions":[{"key":"topology.kubernetes.io/zone","operator":"In","values":["us-west-2a"]}]}]}}

# 查看当前节点 AZ
kubectl get node my-node -o jsonpath='{.metadata.labels.topology\.kubernetes\.io/zone}'
```

**解决方案：** 使用 `WaitForFirstConsumer` 的 StorageClass，K8s 会在 Pod 被调度到某个节点后，再在该 AZ 创建 EBS 卷，确保同 AZ。

### 坑3：PVC 处于 Pending 状态

```bash
kubectl describe pvc my-pvc -n my-app
# 常见原因：
# 1. StorageClass 不存在
#    Error: storageclass "gp3-retain" not found
# 2. CSI 驱动没有安装或权限不足
#    Warning  ProvisioningFailed  Failed to provision volume: UnauthorizedAccess
# 3. 没有可用节点满足 nodeAffinity（WaitForFirstConsumer 场景下）
#    Normal   WaitForFirstConsumer  waiting for first consumer to be created before binding
```

### 坑4：StatefulSet 缩容后 PVC 残留

StatefulSet 缩容（如从 3 副本缩到 1 副本）后，`data-postgresql-1` 和 `data-postgresql-2` 的 PVC 不会自动删除，会一直计费。需要定期检查并清理：

```bash
# 找出不再被任何 Pod 使用的 PVC
kubectl get pvc -A | grep -v Bound
# 或者
kubectl get pvc -A -o json | \
  jq '.items[] | select(.status.phase != "Bound") | .metadata.name'
```

### 坑5：EFS 挂载延迟高

EFS 挂载在高 I/O 场景下延迟显著高于 EBS（毫秒 vs 微秒级别）。EFS 适合：配置文件、日志归档、用户上传文件。**不适合**：数据库文件、需要低延迟的场景。

## 总结

K8s 存储的核心原则：

1. **动态供给是标准**：用 StorageClass + PVC，不要手动管理 PV
2. **多 AZ 集群必须用 WaitForFirstConsumer**：避免 EBS 跨 AZ 挂载失败
3. **生产数据用 Retain 策略**：宁可手动清理，不要让数据因为误删 PVC 丢失
4. **按场景选存储类型**：数据库用 EBS（低延迟），共享文件用 EFS（多节点访问）
5. **定期检查孤立 PVC**：StatefulSet 缩容后要手动清理，避免存储浪费

数据是最宝贵的，存储配置错误的代价往往是不可逆的，务必在测试环境先验证所有存储相关的操作。
