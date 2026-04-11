---
title: "Kubernetes 存储：PV/PVC/StorageClass 实践"
date: 2025-12-09T10:00:00+08:00
draft: false
tags: ["Kubernetes", "存储", "PVC", "CSI", "运维"]
categories: ["Kubernetes"]
description: "深入解析 Kubernetes 存储体系，覆盖 PV/PVC/StorageClass 概念与关系、访问模式对比、动态供给原理、StatefulSet 存储模板、常用 CSI 驱动配置以及跨集群数据迁移方案。"
summary: "从 PV/PVC 基础概念到生产级 CSI 配置，涵盖动态供给、StatefulSet 存储、AWS EBS/EFS、阿里云云盘/NAS 以及数据迁移实践。"
toc: true
math: false
diagram: false
keywords: ["Kubernetes", "PV", "PVC", "StorageClass", "CSI", "EBS", "EFS", "StatefulSet", "存储迁移"]
params:
  reading_time: true
---

## 存储层级关系

Kubernetes 存储抽象分为四层，从底层到上层依次是：

```
┌─────────────────────────────────────────┐
│              应用 Pod                    │  ← 使用存储
├─────────────────────────────────────────┤
│         PVC (PersistentVolumeClaim)      │  ← 存储需求声明（开发者视角）
├─────────────────────────────────────────┤
│         PV (PersistentVolume)            │  ← 存储资源（运维视角）
├─────────────────────────────────────────┤
│  StorageClass → CSI Driver → 真实存储    │  ← 存储后端（EBS/EFS/云盘等）
└─────────────────────────────────────────┘
```

- **Volume**：Pod 级别，Pod 删除时数据消失（emptyDir/configMap/secret 等）
- **PV**：集群级别的存储资源，独立于 Pod 生命周期
- **PVC**：命名空间级别，Pod 通过 PVC 申请存储
- **StorageClass**：存储模板，定义如何动态创建 PV

---

## 访问模式（AccessModes）

| 模式 | 缩写 | 说明 | 适用场景 |
|------|------|------|----------|
| ReadWriteOnce | RWO | 单节点读写 | 数据库（MySQL/PostgreSQL） |
| ReadOnlyMany | ROX | 多节点只读 | 静态文件共享 |
| ReadWriteMany | RWX | 多节点读写 | 共享存储（NFS/EFS/NAS） |
| ReadWriteOncePod | RWOP | 单 Pod 读写（K8s 1.22+） | 高安全性单实例 |

> **重要**：访问模式由底层存储决定，AWS EBS 只支持 RWO，AWS EFS 支持 RWX。

```bash
# 查看 PV 支持的访问模式
kubectl get pv -o custom-columns='NAME:.metadata.name,CAPACITY:.spec.capacity.storage,ACCESS:.spec.accessModes,STORAGECLASS:.spec.storageClassName,STATUS:.status.phase'
```

---

## 回收策略（Reclaim Policy）

| 策略 | 说明 | 推荐场景 |
|------|------|----------|
| **Retain** | PVC 删除后 PV 保留，需手动清理 | 生产数据库，防止误删 |
| **Delete** | PVC 删除后自动删除 PV 和底层存储 | 临时存储，测试环境 |
| **Recycle**（已弃用） | 清空数据后重新可用 | 不推荐使用 |

```bash
# 修改已有 PV 的回收策略
kubectl patch pv <pv-name> -p '{"spec":{"persistentVolumeReclaimPolicy":"Retain"}}'

# 查看 PV 回收策略
kubectl get pv -o custom-columns='NAME:.metadata.name,RECLAIM:.spec.persistentVolumeReclaimPolicy'
```

---

## StorageClass 定义与动态供给

### 动态供给原理

```
用户创建 PVC
      ↓
kube-controller-manager 检测到未绑定 PVC
      ↓
根据 storageClassName 找到对应 StorageClass
      ↓
调用 CSI Driver Provisioner 创建实际存储（EBS卷/云盘等）
      ↓
自动创建 PV 并绑定到 PVC
      ↓
Pod 挂载成功
```

### AWS EBS StorageClass

```yaml
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: ebs-gp3
  annotations:
    storageclass.kubernetes.io/is-default-class: "true"  # 设为默认
provisioner: ebs.csi.aws.com
parameters:
  type: gp3
  iops: "3000"
  throughput: "125"     # MB/s
  encrypted: "true"
  kmsKeyId: "arn:aws:kms:us-west-2:123456789:key/xxx"  # 自定义 KMS
volumeBindingMode: WaitForFirstConsumer  # 延迟绑定，确保与 Pod 同 AZ
reclaimPolicy: Retain
allowVolumeExpansion: true   # 允许扩容
```

### AWS EFS StorageClass（支持 RWX）

```yaml
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: efs-sc
provisioner: efs.csi.aws.com
parameters:
  provisioningMode: efs-ap          # 使用 Access Point
  fileSystemId: fs-0123456789abcdef # EFS 文件系统 ID
  directoryPerms: "700"
  gidRangeStart: "1000"
  gidRangeEnd: "2000"
  basePath: "/dynamic_provisioning"
reclaimPolicy: Delete
volumeBindingMode: Immediate
```

### 阿里云云盘 StorageClass

```yaml
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: alicloud-disk-essd
  annotations:
    storageclass.kubernetes.io/is-default-class: "true"
provisioner: diskplugin.csi.alibabacloud.com
parameters:
  type: cloud_essd
  performanceLevel: PL1       # PL0/PL1/PL2/PL3
  encrypted: "false"
reclaimPolicy: Delete
volumeBindingMode: WaitForFirstConsumer
allowVolumeExpansion: true
```

### 阿里云 NAS StorageClass（支持 RWX）

```yaml
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: alicloud-nas-subpath
provisioner: nasplugin.csi.alibabacloud.com
parameters:
  volumeAs: subpath
  server: "xxxxxxxx.cn-hangzhou.nas.aliyuncs.com"
  path: "/k8s"
  vers: "3"
  mode: "0777"
reclaimPolicy: Retain
volumeBindingMode: Immediate
```

---

## PVC 使用示例

```yaml
# PVC 声明
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: mysql-data
  namespace: production
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: ebs-gp3
  resources:
    requests:
      storage: 100Gi

---
# Pod 挂载 PVC
apiVersion: v1
kind: Pod
metadata:
  name: mysql
  namespace: production
spec:
  containers:
    - name: mysql
      image: mysql:8.0
      env:
        - name: MYSQL_ROOT_PASSWORD
          valueFrom:
            secretKeyRef:
              name: mysql-secret
              key: password
      volumeMounts:
        - name: data
          mountPath: /var/lib/mysql
  volumes:
    - name: data
      persistentVolumeClaim:
        claimName: mysql-data   # 引用 PVC
```

---

## PVC 生命周期与排查 Pending

```
PVC 状态流转：
Pending → Bound → Released → (Available/Failed)

Pending：等待 PV 绑定或动态供给
Bound：成功绑定
Released：PVC 删除但 PV 保留（Retain 策略）
```

### 排查 PVC Pending

```bash
# 1. 查看 PVC 状态
kubectl get pvc -n production
kubectl describe pvc mysql-data -n production

# 常见原因 1：StorageClass 不存在
kubectl get storageclass

# 常见原因 2：没有满足条件的 PV（静态供给场景）
kubectl get pv | grep Available

# 常见原因 3：CSI Driver 未安装或 Pod 异常
kubectl -n kube-system get pods | grep csi
kubectl -n kube-system logs daemonset/ebs-csi-node -c ebs-plugin | tail -50

# 常见原因 4：WaitForFirstConsumer 模式下未创建 Pod
# PVC 会一直 Pending 直到 Pod 调度

# 常见原因 5：存储配额不足（AWS 账户 EBS 限制）
kubectl get events -n production --sort-by='.lastTimestamp' | grep pvc
```

---

## StatefulSet + PVC 模板

StatefulSet 使用 `volumeClaimTemplates` 为每个副本自动创建独立 PVC：

```yaml
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: mysql
  namespace: production
spec:
  serviceName: mysql-headless
  replicas: 3
  selector:
    matchLabels:
      app: mysql
  template:
    metadata:
      labels:
        app: mysql
    spec:
      containers:
        - name: mysql
          image: mysql:8.0
          ports:
            - containerPort: 3306
          volumeMounts:
            - name: data
              mountPath: /var/lib/mysql
            - name: config
              mountPath: /etc/mysql/conf.d
          resources:
            requests:
              cpu: "500m"
              memory: "1Gi"
            limits:
              cpu: "2"
              memory: "4Gi"
      volumes:
        - name: config
          configMap:
            name: mysql-config
  volumeClaimTemplates:                   # 每个 Pod 独立 PVC
    - metadata:
        name: data
      spec:
        accessModes: ["ReadWriteOnce"]
        storageClassName: ebs-gp3
        resources:
          requests:
            storage: 50Gi
```

```bash
# StatefulSet 自动创建的 PVC 命名规则
# {volumeClaimTemplate.name}-{statefulset.name}-{序号}
kubectl get pvc -n production
# NAME           STATUS   VOLUME         CAPACITY   ACCESS MODES
# data-mysql-0   Bound    pvc-abc123     50Gi       RWO
# data-mysql-1   Bound    pvc-def456     50Gi       RWO
# data-mysql-2   Bound    pvc-ghi789     50Gi       RWO

# 注意：删除 StatefulSet 不会删除 PVC（保护数据）
kubectl delete statefulset mysql  # PVC 仍然存在
```

---

## PV 扩容

```bash
# 1. 确认 StorageClass 开启了 allowVolumeExpansion
kubectl get storageclass ebs-gp3 -o jsonpath='{.allowVolumeExpansion}'

# 2. 编辑 PVC 扩容（只能增大，不能缩小）
kubectl patch pvc mysql-data -n production -p '{"spec":{"resources":{"requests":{"storage":"200Gi"}}}}'

# 3. 查看扩容状态
kubectl describe pvc mysql-data -n production | grep -A5 "Conditions"
# Conditions 会显示 FileSystemResizePending → 完成后消失

# 4. 对于需要 Pod 重启的文件系统扩容，重启 Pod
kubectl rollout restart statefulset mysql -n production
```

---

## 数据迁移方案

### 方案一：同集群 PVC 数据复制

```bash
# 使用临时 Pod 在两个 PVC 间复制数据
kubectl apply -f - <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: pvc-migrator
  namespace: production
spec:
  restartPolicy: Never
  containers:
    - name: migrator
      image: alpine
      command: ["sh", "-c", "cp -av /source/. /dest/ && echo 'Done'"]
      volumeMounts:
        - name: source
          mountPath: /source
        - name: dest
          mountPath: /dest
  volumes:
    - name: source
      persistentVolumeClaim:
        claimName: old-pvc
    - name: dest
      persistentVolumeClaim:
        claimName: new-pvc
EOF

kubectl logs -f pvc-migrator -n production
```

### 方案二：使用 Velero 跨集群迁移

```bash
# 安装 Velero（以 AWS S3 为后端）
velero install \
  --provider aws \
  --plugins velero/velero-plugin-for-aws:v1.8.0 \
  --bucket my-velero-backup \
  --backup-location-config region=us-west-2 \
  --snapshot-location-config region=us-west-2 \
  --secret-file ./credentials-velero

# 备份指定命名空间（含 PVC 快照）
velero backup create production-backup \
  --include-namespaces production \
  --snapshot-volumes=true \
  --wait

# 查看备份状态
velero backup describe production-backup
velero backup logs production-backup

# 在目标集群恢复
velero restore create --from-backup production-backup \
  --namespace-mappings production:production-new \
  --wait
```

### 方案三：PVC 快照（CSI Snapshot）

```yaml
# 1. 创建 VolumeSnapshotClass
apiVersion: snapshot.storage.k8s.io/v1
kind: VolumeSnapshotClass
metadata:
  name: ebs-csi-aws
driver: ebs.csi.aws.com
deletionPolicy: Delete

---
# 2. 创建快照
apiVersion: snapshot.storage.k8s.io/v1
kind: VolumeSnapshot
metadata:
  name: mysql-data-snapshot
  namespace: production
spec:
  volumeSnapshotClassName: ebs-csi-aws
  source:
    persistentVolumeClaimName: mysql-data

---
# 3. 从快照创建新 PVC
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: mysql-data-restored
  namespace: production
spec:
  dataSource:
    name: mysql-data-snapshot
    kind: VolumeSnapshot
    apiGroup: snapshot.storage.k8s.io
  accessModes:
    - ReadWriteOnce
  storageClassName: ebs-gp3
  resources:
    requests:
      storage: 100Gi
```

```bash
# 查看快照状态
kubectl get volumesnapshot -n production
kubectl describe volumesnapshot mysql-data-snapshot -n production
```

---

## 常用排查命令

```bash
# 全面查看存储状态
kubectl get pv,pvc,storageclass -A

# 查看 PV/PVC 绑定关系
kubectl get pv -o custom-columns='NAME:.metadata.name,CLAIM:.spec.claimRef.namespace,PVC:.spec.claimRef.name,STATUS:.status.phase,CAPACITY:.spec.capacity.storage'

# 检查 CSI Node 插件
kubectl -n kube-system get daemonset | grep csi
kubectl -n kube-system describe daemonset ebs-csi-node

# 检查节点是否挂载了 PV
kubectl describe node <node-name> | grep -A20 "Volumes"

# 查看存储相关事件
kubectl get events -A --field-selector reason=ProvisioningSucceeded
kubectl get events -A --field-selector reason=ProvisioningFailed
```
