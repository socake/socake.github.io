---
title: "云原生存储方案选型：EFS/EBS/OSS 实践"
date: 2025-12-09T17:00:00+08:00
draft: false
tags: ["AWS", "存储", "Kubernetes", "EFS", "EBS"]
categories: ["Kubernetes"]
description: "云原生存储方案完整对比与实践：EBS/EFS/S3 特性分析、CSI Driver 配置、性能测试及 Velero 备份方案"
summary: "系统梳理 AWS EBS、EFS、S3 在 Kubernetes 中的使用方式，覆盖 StorageClass 配置、动态供给、性能测试与数据备份策略，附阿里云 NAS/OSS 对比。"
toc: true
math: false
diagram: false
keywords: ["EBS", "EFS", "StorageClass", "PVC", "Velero", "云原生存储"]
params:
  reading_time: true
---

## 云原生存储需求分析

在 K8s 中使用存储，需要先明确几个维度：

- **访问模式**：单节点读写（RWO）还是多节点共享读写（RWX）
- **性能要求**：IOPS、吞吐量、延迟
- **数据生命周期**：跟随 Pod 还是独立持久
- **成本敏感度**：热数据/冷数据分层
- **跨 AZ 需求**：是否要跨可用区共享

---

## AWS 存储方案对比

| 特性 | EBS (gp3) | EFS | S3 |
|------|-----------|-----|----|
| 访问模式 | RWO（单节点）| RWX（多节点多AZ）| 对象存储，非 POSIX |
| 协议 | Block | NFS v4.1 | HTTP/S3 API |
| 延迟 | <1ms | 数ms | 数十ms |
| 吞吐量 | 最高 1000 MB/s | 最高 3 GB/s（burst）| 无上限（受并发限制）|
| 跨 AZ | 不支持（Zone 内）| 原生支持 | 原生支持 |
| 最大容量 | 64 TiB/卷 | 无上限（PB 级）| 无上限 |
| 价格（us-west-2）| $0.08/GB/月 | $0.30/GB/月 | $0.023/GB/月 |
| 适用场景 | 数据库、高性能单实例 | 共享配置、CMS、机器学习数据集 | 日志归档、静态资源、备份 |

**选型决策**：有状态单实例（MySQL/Redis）→ EBS；多实例共享（模型权重/Notebook）→ EFS；归档/对象 → S3。

---

## EBS in Kubernetes

### 安装 EBS CSI Driver

```bash
# 通过 EKS Add-on 安装（推荐）
aws eks create-addon \
  --cluster-name prod-cluster \
  --addon-name aws-ebs-csi-driver \
  --addon-version v1.35.0-eksbuild.1 \
  --service-account-role-arn arn:aws:iam::123456789012:role/ebs-csi-driver-role

# EBS CSI Driver 需要的 IAM 权限
eksctl create iamserviceaccount \
  --cluster prod-cluster \
  --name ebs-csi-controller-sa \
  --namespace kube-system \
  --attach-policy-arn arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy \
  --approve \
  --override-existing-serviceaccounts
```

### StorageClass 配置

```yaml
# gp3 StorageClass（推荐，比 gp2 性价比更高）
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: gp3
  annotations:
    storageclass.kubernetes.io/is-default-class: "true"
provisioner: ebs.csi.aws.com
volumeBindingMode: WaitForFirstConsumer   # 等 Pod 调度后再创建 EBS，保证同 AZ
reclaimPolicy: Retain                      # 生产环境用 Retain，防止误删
parameters:
  type: gp3
  iops: "3000"
  throughput: "125"
  encrypted: "true"
  kmsKeyId: arn:aws:kms:us-west-2:123456789012:key/mrk-xxx
allowVolumeExpansion: true
```

### PVC 使用示例

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: mysql-data
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: gp3
  resources:
    requests:
      storage: 100Gi
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: mysql
spec:
  selector:
    matchLabels:
      app: mysql
  serviceName: mysql
  template:
    spec:
      containers:
        - name: mysql
          image: mysql:8.0
          volumeMounts:
            - name: data
              mountPath: /var/lib/mysql
  volumeClaimTemplates:
    - metadata:
        name: data
      spec:
        accessModes: ["ReadWriteOnce"]
        storageClassName: gp3
        resources:
          requests:
            storage: 100Gi
```

### EBS 快照备份

```yaml
# VolumeSnapshotClass
apiVersion: snapshot.storage.k8s.io/v1
kind: VolumeSnapshotClass
metadata:
  name: ebs-vsc
driver: ebs.csi.aws.com
deletionPolicy: Retain
---
# 创建快照
apiVersion: snapshot.storage.k8s.io/v1
kind: VolumeSnapshot
metadata:
  name: mysql-data-snapshot-20251209
spec:
  volumeSnapshotClassName: ebs-vsc
  source:
    persistentVolumeClaimName: mysql-data
```

**注意**：EBS 卷是 Zone 级别的，`volumeBindingMode: WaitForFirstConsumer` 保证 PV 在 Pod 所在 AZ 创建，否则 Pod 和 PV 可能跨 AZ，导致挂载失败。

---

## EFS in Kubernetes

### 安装 EFS CSI Driver

```bash
# 创建 EFS 文件系统（先在 AWS 侧）
EFS_ID=$(aws efs create-file-system \
  --performance-mode generalPurpose \
  --throughput-mode elastic \
  --encrypted \
  --tags Key=Name,Value=k8s-shared-storage \
  --query 'FileSystemId' --output text)

echo "EFS ID: $EFS_ID"

# 创建挂载点（每个 AZ 各一个）
for SUBNET_ID in subnet-aaa subnet-bbb subnet-ccc; do
  aws efs create-mount-target \
    --file-system-id $EFS_ID \
    --subnet-id $SUBNET_ID \
    --security-groups sg-xxxxxxxx
done

# 安装 EFS CSI Driver
aws eks create-addon \
  --cluster-name prod-cluster \
  --addon-name aws-efs-csi-driver \
  --addon-version v2.0.7-eksbuild.1
```

### 动态供给 StorageClass

```yaml
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: efs-sc
provisioner: efs.csi.aws.com
parameters:
  provisioningMode: efs-ap          # 用 Access Point 做动态供给
  fileSystemId: fs-0123456789abcdef0
  directoryPerms: "700"
  gidRangeStart: "1000"
  gidRangeEnd: "2000"
  basePath: "/dynamic"
```

### 静态供给（共享同一 EFS 根目录）

```yaml
apiVersion: v1
kind: PersistentVolume
metadata:
  name: efs-pv-shared
spec:
  capacity:
    storage: 5Ti             # EFS 实际不限大小，这里是声明值
  volumeMode: Filesystem
  accessModes:
    - ReadWriteMany
  persistentVolumeReclaimPolicy: Retain
  storageClassName: ""
  csi:
    driver: efs.csi.aws.com
    volumeHandle: fs-0123456789abcdef0   # EFS ID
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: efs-pvc-shared
spec:
  accessModes:
    - ReadWriteMany
  storageClassName: ""
  resources:
    requests:
      storage: 5Ti
  volumeName: efs-pv-shared
```

### 多 Pod 共享挂载示例

```yaml
# Deployment 多副本共享同一个 EFS
apiVersion: apps/v1
kind: Deployment
metadata:
  name: model-server
spec:
  replicas: 5
  template:
    spec:
      containers:
        - name: server
          image: my-model-server:latest
          volumeMounts:
            - name: model-weights
              mountPath: /models
              readOnly: true
      volumes:
        - name: model-weights
          persistentVolumeClaim:
            claimName: efs-pvc-shared
```

---

## 阿里云存储对比

在阿里云 ACK 环境中，对应关系如下：

| AWS | 阿里云 | 特性差异 |
|-----|--------|----------|
| EBS gp3 | 云盘 ESSD PL1 | 阿里云 ESSD 单盘 IOPS 上限更高（PL3: 1M IOPS）|
| EFS | NAS 通用型/极速型 | 极速型延迟更低，但价格贵 3 倍 |
| S3 | OSS | API 兼容 S3，可用 s3cmd/mc 操作 |

```bash
# 阿里云 NAS StorageClass
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: alicloud-nas
provisioner: nasplugin.csi.alibabacloud.com
parameters:
  volumeAs: subpath
  server: "xxx.cn-hangzhou.nas.aliyuncs.com"
  path: "/"
  vers: "3"
  mode: "755"
reclaimPolicy: Retain
```

---

## 存储性能测试

在 Pod 内用 fio 测试实际性能：

```yaml
# 测试 Job
apiVersion: batch/v1
kind: Job
metadata:
  name: storage-perf-test
spec:
  template:
    spec:
      containers:
        - name: fio
          image: nixery.dev/shell/fio
          command:
            - /bin/sh
            - -c
            - |
              # 顺序写测试
              fio --name=seq-write \
                --directory=/data \
                --rw=write \
                --bs=1M \
                --size=4G \
                --numjobs=1 \
                --iodepth=32 \
                --runtime=60 \
                --time_based \
                --output-format=json \
                --output=/data/seq-write.json

              # 随机读写测试（IOPS 敏感场景）
              fio --name=rand-rw \
                --directory=/data \
                --rw=randrw \
                --rwmixread=70 \
                --bs=4K \
                --size=4G \
                --numjobs=4 \
                --iodepth=64 \
                --runtime=60 \
                --time_based \
                --output-format=json \
                --output=/data/rand-rw.json

              cat /data/seq-write.json | python3 -c "
              import json,sys
              d=json.load(sys.stdin)
              j=d['jobs'][0]
              print(f'Write BW: {j[\"write\"][\"bw_bytes\"]/1024/1024:.1f} MB/s')
              print(f'Write IOPS: {j[\"write\"][\"iops\"]:.0f}')
              "
          volumeMounts:
            - name: test-vol
              mountPath: /data
      restartPolicy: Never
      volumes:
        - name: test-vol
          persistentVolumeClaim:
            claimName: your-pvc-name
```

典型测试结果参考（us-west-2，实际以测试为准）：

| 存储类型 | 顺序写吞吐 | 随机读 IOPS (4K) |
|----------|-----------|-----------------|
| EBS gp3 (默认) | ~125 MB/s | ~3000 |
| EBS gp3 (调优) | ~1000 MB/s | ~16000 |
| EFS 通用 | ~100 MB/s | ~500 |
| EFS Elastic 吞吐 | ~300 MB/s | ~1500 |

---

## Velero 备份 PVC 数据

```bash
# 安装 Velero
helm repo add vmware-tanzu https://vmware-tanzu.github.io/helm-charts
helm install velero vmware-tanzu/velero \
  --namespace velero \
  --create-namespace \
  --set configuration.backupStorageLocation[0].name=default \
  --set configuration.backupStorageLocation[0].provider=aws \
  --set configuration.backupStorageLocation[0].bucket=my-velero-backup \
  --set configuration.backupStorageLocation[0].config.region=us-west-2 \
  --set configuration.volumeSnapshotLocation[0].name=default \
  --set configuration.volumeSnapshotLocation[0].provider=aws \
  --set configuration.volumeSnapshotLocation[0].config.region=us-west-2 \
  --set serviceAccount.server.annotations."eks\.amazonaws\.com/role-arn"=arn:aws:iam::123456789012:role/velero-role

# 创建按需备份
velero backup create my-backup \
  --include-namespaces production \
  --storage-location default \
  --volume-snapshot-locations default

# 创建定时备份（每天凌晨 2 点）
velero schedule create daily-backup \
  --schedule="0 2 * * *" \
  --include-namespaces production \
  --ttl 720h0m0s    # 保留 30 天

# 查看备份状态
velero backup describe my-backup --details

# 恢复
velero restore create --from-backup my-backup \
  --include-namespaces production
```

---

## 选型决策矩阵

| 需求 | 推荐方案 | 理由 |
|------|----------|------|
| MySQL/PostgreSQL 单实例 | EBS gp3 | 低延迟，RWO 符合数据库独占需求 |
| Redis 持久化 | EBS gp3 | 同上 |
| Jupyter Notebook 共享 | EFS | 多用户同时挂载，跨 AZ 可用 |
| ML 模型权重只读共享 | EFS 静态供给 | 多 Pod 并发只读，EFS 完美匹配 |
| 日志归档 | S3（Loki/直接写）| 成本最低，不需要 POSIX 语义 |
| CI/CD 构建缓存 | EFS 或 EBS（取决于并发）| 单 Builder: EBS；并发 Builder: EFS |
| 配置文件/证书共享 | ConfigMap/Secret 或 EFS | 小文件用 CM/Secret，大文件用 EFS |
| 跨区域备份 | S3 + 跨区域复制 | S3 原生支持 CRR |
