---
title: "Kubernetes 集群升级实践"
date: 2025-12-09T11:00:00+08:00
draft: false
tags: ["Kubernetes", "集群升级", "EKS", "运维", "SOP"]
categories: ["Kubernetes"]
description: "Kubernetes 集群升级完整实践指南，覆盖升级前准备、版本兼容检查、etcd 备份、EKS 升级流程、节点升级策略、API 废弃处理及常见问题解决。"
summary: "K8s 集群升级全流程：从版本兼容性检查、etcd 备份、EKS 托管升级命令，到节点蓝绿替换、PDB 配置、pluto 工具检测废弃 API，再到常见升级问题处理。"
toc: true
math: false
diagram: false
keywords: ["Kubernetes", "集群升级", "EKS", "etcd备份", "API废弃", "pluto", "PodDisruptionBudget", "节点升级"]
params:
  reading_time: true
---

## 升级前准备

集群升级是高风险操作，充分的准备比升级本身更重要。

### 版本兼容性检查

**K8s 版本策略：**
- K8s 每年发布 3 个次要版本（1.28、1.29、1.30...）
- 每个次要版本维护约 14 个月
- **不能跨版本升级**：1.28 → 1.30 要先升到 1.29

```bash
# 查看当前版本
kubectl version --short

# 查看节点版本（control plane 和 worker 可能不同）
kubectl get nodes -o wide

# 查看支持的版本范围（EKS）
aws eks describe-addon-versions --query 'addons[0].addonVersions[0].compatibilities' \
  --output table

# 检查 K8s 官方支持的版本
# https://kubernetes.io/releases/
```

**组件版本兼容矩阵：**

| 组件 | 与 API Server 的版本差 |
|------|----------------------|
| kubelet | ±1 个次要版本 |
| kube-proxy | ±1 个次要版本 |
| kubectl | ±1 个次要版本 |
| etcd | 见 K8s changelog |
| CoreDNS | 见 K8s changelog |

```bash
# 检查 kubelet 和 API Server 版本是否在兼容范围内
kubectl get nodes -o json | \
  jq -r '.items[] | "\(.metadata.name): \(.status.nodeInfo.kubeletVersion)"'
```

### API 废弃检查（关键步骤）

不同 K8s 版本会废弃旧 API，升级后这些资源无法使用。

```bash
# 使用 pluto 检测废弃 API（推荐工具）
# 安装
curl -L https://github.com/FairwindsOps/pluto/releases/download/v5.19.0/pluto_5.19.0_linux_amd64.tar.gz \
  | tar xz && sudo mv pluto /usr/local/bin/

# 检查集群中正在使用的废弃 API（针对目标升级版本）
pluto detect-all-in-cluster --target-versions k8s=v1.29.0

# 检查本地 Helm Chart 中的废弃 API
pluto detect-helm --target-versions k8s=v1.29.0

# 检查本地 YAML 文件
pluto detect -d ./k8s-manifests/ --target-versions k8s=v1.29.0
```

**常见 API 废弃列表：**

| 旧 API | 新 API | 废弃版本 | 移除版本 |
|--------|--------|----------|----------|
| `extensions/v1beta1 Ingress` | `networking.k8s.io/v1` | 1.14 | 1.22 |
| `batch/v1beta1 CronJob` | `batch/v1` | 1.21 | 1.25 |
| `policy/v1beta1 PodDisruptionBudget` | `policy/v1` | 1.21 | 1.25 |
| `autoscaling/v2beta1 HPA` | `autoscaling/v2` | 1.23 | 1.26 |
| `flowcontrol.apiserver.k8s.io/v1beta2` | `v1beta3/v1` | 1.26 | 1.29 |

```bash
# 批量更新 YAML 中的 apiVersion（使用 pluto 配合 sed）
# 先找出所有问题文件
pluto detect -d ./manifests/ --output json | jq -r '.items[].filePath' | sort -u

# 手动更新特定文件中的 apiVersion
sed -i 's|extensions/v1beta1|networking.k8s.io/v1|g' ingress.yaml
```

### etcd 备份

升级前必须备份 etcd，这是唯一的回滚手段（对于自建集群）。

```bash
# 方法一：etcdctl snapshot（kubeadm 集群）
ETCDCTL_API=3 etcdctl \
  --endpoints=https://127.0.0.1:2379 \
  --cacert=/etc/kubernetes/pki/etcd/ca.crt \
  --cert=/etc/kubernetes/pki/etcd/server.crt \
  --key=/etc/kubernetes/pki/etcd/server.key \
  snapshot save /backup/etcd-snapshot-$(date +%Y%m%d-%H%M%S).db

# 验证备份文件
ETCDCTL_API=3 etcdctl snapshot status /backup/etcd-snapshot-xxx.db --write-out=table

# 上传到 S3
aws s3 cp /backup/etcd-snapshot-xxx.db \
  s3://my-backup-bucket/etcd/etcd-snapshot-$(date +%Y%m%d-%H%M%S).db
```

```bash
# 方法二：EKS 集群（AWS 托管 etcd，需要备份工作负载资源）
# EKS 的 etcd 由 AWS 管理，无法直接备份
# 备份方案：Velero 备份所有 K8s 资源

# 安装 Velero
velero install \
  --provider aws \
  --plugins velero/velero-plugin-for-aws:v1.8.0 \
  --bucket my-velero-bucket \
  --backup-location-config region=us-east-1 \
  --snapshot-location-config region=us-east-1 \
  --use-node-agent

# 创建完整备份
velero backup create pre-upgrade-backup \
  --include-namespaces='*' \
  --wait

# 查看备份状态
velero backup describe pre-upgrade-backup
velero backup logs pre-upgrade-backup
```

---

## 升级顺序

```
etcd 升级（如有）
   ↓
kube-apiserver 升级
   ↓
kube-controller-manager 升级
   ↓
kube-scheduler 升级
   ↓
kube-proxy、CoreDNS、CNI 等 add-on 升级
   ↓
Worker 节点升级（kubelet + kube-proxy）
```

**为什么不能跨版本：** K8s 保证 N-1 向后兼容，1.28 的 kubelet 可以连 1.29 的 API Server，但不保证跨 2 个版本兼容。

---

## EKS 升级流程

### 1. 升级 Control Plane

```bash
# 查看当前 EKS 集群版本
aws eks describe-cluster --name my-cluster --query 'cluster.version'

# 查看可以升级到的版本
aws eks describe-cluster --name my-cluster \
  --query 'cluster.version' --output text

# 发起 Control Plane 升级（通常需要 15-25 分钟）
aws eks update-cluster-version \
  --name my-cluster \
  --kubernetes-version 1.29

# 等待升级完成
aws eks wait cluster-active --name my-cluster

# 或者实时查看状态
watch -n 10 aws eks describe-cluster --name my-cluster \
  --query 'cluster.status' --output text
```

### 2. 升级 EKS Add-on

EKS Add-on 要在 Control Plane 升级完成后，Worker 节点升级前进行。

```bash
# 查看当前 add-on 及版本
aws eks list-addons --cluster-name my-cluster
aws eks describe-addon --cluster-name my-cluster --addon-name vpc-cni

# 查看 add-on 支持的版本
aws eks describe-addon-versions \
  --kubernetes-version 1.29 \
  --addon-name vpc-cni \
  --query 'addons[0].addonVersions[*].addonVersion'

# 升级 add-on
aws eks update-addon \
  --cluster-name my-cluster \
  --addon-name vpc-cni \
  --addon-version v1.16.0-eksbuild.1 \
  --resolve-conflicts OVERWRITE

# 等待 add-on 升级完成
aws eks wait addon-active \
  --cluster-name my-cluster \
  --addon-name vpc-cni
```

**EKS Add-on 升级顺序：**

```bash
# 推荐顺序：
# 1. vpc-cni（网络插件，最先升级）
# 2. kube-proxy
# 3. coredns
# 4. aws-ebs-csi-driver / aws-efs-csi-driver（如果使用）
# 5. aws-load-balancer-controller（如果使用）

for addon in vpc-cni kube-proxy coredns; do
  echo "Upgrading $addon..."
  LATEST_VERSION=$(aws eks describe-addon-versions \
    --kubernetes-version 1.29 \
    --addon-name $addon \
    --query 'addons[0].addonVersions[0].addonVersion' \
    --output text)
  
  aws eks update-addon \
    --cluster-name my-cluster \
    --addon-name $addon \
    --addon-version $LATEST_VERSION \
    --resolve-conflicts OVERWRITE
  
  aws eks wait addon-active --cluster-name my-cluster --addon-name $addon
  echo "$addon upgraded to $LATEST_VERSION"
done
```

### 3. 升级节点组

**方案 A：托管节点组就地滚动升级**

```bash
# 查看节点组信息
aws eks describe-nodegroup \
  --cluster-name my-cluster \
  --nodegroup-name my-nodegroup

# 更新节点组 AMI（触发滚动更新）
aws eks update-nodegroup-version \
  --cluster-name my-cluster \
  --nodegroup-name my-nodegroup \
  --kubernetes-version 1.29

# 等待节点组更新完成（可能需要 30-60 分钟）
aws eks wait nodegroup-active \
  --cluster-name my-cluster \
  --nodegroup-name my-nodegroup
```

**方案 B：蓝绿节点组（推荐，零停机）**

```bash
# 1. 创建新版本节点组（使用新 AMI）
aws eks create-nodegroup \
  --cluster-name my-cluster \
  --nodegroup-name my-nodegroup-v2 \
  --kubernetes-version 1.29 \
  --node-role arn:aws:iam::123456789012:role/EKSNodeRole \
  --subnets subnet-xxx subnet-yyy \
  --scaling-config minSize=2,maxSize=10,desiredSize=3 \
  --disk-size 100 \
  --instance-types m5.xlarge

# 2. 等待新节点组就绪
aws eks wait nodegroup-active \
  --cluster-name my-cluster \
  --nodegroup-name my-nodegroup-v2

# 3. cordon 旧节点（不再调度新 Pod）
OLD_NODES=$(kubectl get nodes -l eks.amazonaws.com/nodegroup=my-nodegroup \
  -o jsonpath='{.items[*].metadata.name}')
for node in $OLD_NODES; do
  kubectl cordon $node
done

# 4. drain 旧节点（驱逐 Pod 到新节点）
for node in $OLD_NODES; do
  kubectl drain $node \
    --ignore-daemonsets \
    --delete-emptydir-data \
    --grace-period=60 \
    --timeout=300s
done

# 5. 验证所有 Pod 在新节点上正常运行
kubectl get pods -A -o wide | grep my-nodegroup-v2

# 6. 删除旧节点组
aws eks delete-nodegroup \
  --cluster-name my-cluster \
  --nodegroup-name my-nodegroup
```

---

## PodDisruptionBudget — 节点升级的关键

drain 节点时会驱逐 Pod，PDB 确保驱逐过程中始终有足够的 Pod 在运行。

```yaml
# 确保 api-service 在 drain 期间至少有 2 个 Pod 可用
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: api-service-pdb
  namespace: production
spec:
  minAvailable: 2           # 方式1：最少可用数量
  # maxUnavailable: 1       # 方式2：最多不可用数量（二选一）
  selector:
    matchLabels:
      app: api-service
```

```bash
# 查看 PDB 状态
kubectl get pdb -n production
kubectl describe pdb api-service-pdb -n production

# 如果 drain 时 PDB 阻止了驱逐，会看到：
# Cannot evict pod as it would violate the pod's disruption budget

# 强制忽略 PDB（不推荐，可能导致服务不可用）
kubectl drain <node> --disable-eviction

# 正确做法：先扩容 Deployment 再 drain
kubectl scale deployment api-service --replicas=4 -n production
kubectl drain <node> --ignore-daemonsets
```

---

## 节点升级策略

### 滚动 Drain 策略

```bash
#!/bin/bash
# rolling-drain.sh — 逐个 drain 节点并验证服务健康
NAMESPACE=${1:-production}
WAIT_TIME=${2:-60}

nodes=$(kubectl get nodes --no-headers | awk '{print $1}')

for node in $nodes; do
  echo "=== Processing node: $node ==="

  # cordon
  kubectl cordon $node
  echo "Node cordoned, waiting ${WAIT_TIME}s before drain..."
  sleep $WAIT_TIME

  # 检查节点上的 Pod 数量
  pod_count=$(kubectl get pods -A --field-selector=spec.nodeName=$node \
    --no-headers 2>/dev/null | grep -v "DaemonSet" | wc -l)
  echo "Pods to evict: $pod_count"

  # drain
  kubectl drain $node \
    --ignore-daemonsets \
    --delete-emptydir-data \
    --grace-period=90 \
    --timeout=300s

  if [ $? -ne 0 ]; then
    echo "ERROR: Drain failed for $node, stopping!"
    kubectl uncordon $node
    exit 1
  fi

  # 等待新节点上的 Pod 就绪
  echo "Waiting for pods to be ready on other nodes..."
  sleep 30
  
  kubectl wait pods -n $NAMESPACE -l app=api-service \
    --for=condition=Ready \
    --timeout=120s

  echo "Node $node drained successfully"
done
```

---

## 升级后验证 Checklist

```bash
#!/bin/bash
# post-upgrade-verify.sh

echo "=== 1. 集群版本验证 ==="
kubectl version --short
kubectl get nodes

echo ""
echo "=== 2. 系统 Pod 健康状态 ==="
kubectl get pods -n kube-system
kubectl get pods -n cert-manager
kubectl get pods -n ingress-nginx

echo ""
echo "=== 3. 所有 Pod 状态（异常 Pod）==="
kubectl get pods -A | grep -v "Running\|Completed\|NAME"

echo ""
echo "=== 4. 核心工作负载验证 ==="
kubectl get deployments -A | grep -v "READY\|1/1\|2/2\|3/3"

echo ""
echo "=== 5. PVC 状态 ==="
kubectl get pvc -A | grep -v Bound

echo ""
echo "=== 6. Service 和 Endpoints ==="
kubectl get endpoints -A | grep "<none>"

echo ""
echo "=== 7. 最近 Warning 事件 ==="
kubectl get events -A --field-selector=type=Warning \
  --sort-by='.lastTimestamp' | tail -20

echo ""
echo "=== 8. HPA 状态 ==="
kubectl get hpa -A
```

**验证 Checklist：**

- [ ] `kubectl get nodes` 所有节点 Ready，版本正确
- [ ] `kubectl get pods -n kube-system` 所有系统 Pod Running
- [ ] 业务 Pod 全部 Running，无 CrashLoop
- [ ] Ingress 访问正常
- [ ] 数据库连接正常（应用日志无报错）
- [ ] HPA 正常工作
- [ ] 监控告警无异常
- [ ] 测试关键业务流程

---

## 常见升级问题

### Admission Webhook 阻止升级

```bash
# 现象：升级过程中 Pod 创建被 webhook 拒绝
# 错误：Error from server: admission webhook "xxx" denied the request

# 查看所有 webhook
kubectl get validatingwebhookconfigurations
kubectl get mutatingwebhookconfigurations

# 临时禁用有问题的 webhook（排查期间）
kubectl patch validatingwebhookconfiguration <name> \
  -p '{"webhooks":[{"name":"xxx","failurePolicy":"Ignore"}]}'

# 查看 webhook 是否可达
kubectl describe validatingwebhookconfiguration <name> | grep "Service\|URL"
```

### CRD 兼容性问题

```bash
# 现象：升级后 CRD 相关的 operator 报错
# 检查 CRD 的存储版本
kubectl get crd <crd-name> -o jsonpath='{.status.storedVersions}'

# 如果 storedVersions 包含旧版本，需要迁移
# 先确认 operator 支持新版本
kubectl get crd <crd-name> -o jsonpath='{.spec.versions[*].name}'

# 迁移旧版本资源（以 Certificate 为例）
kubectl get certificate -A -o json | kubectl apply -f -
```

### etcd compaction 问题

```bash
# 升级前/后建议执行 etcd compaction（减小 etcd 体积）

# 获取当前 revision
REV=$(ETCDCTL_API=3 etcdctl \
  --endpoints=https://127.0.0.1:2379 \
  --cacert=/etc/kubernetes/pki/etcd/ca.crt \
  --cert=/etc/kubernetes/pki/etcd/server.crt \
  --key=/etc/kubernetes/pki/etcd/server.key \
  endpoint status --write-out="json" | jq '.[] | .Status.header.revision')

# Compact（保留最新 revision，清除历史）
ETCDCTL_API=3 etcdctl \
  --endpoints=https://127.0.0.1:2379 \
  --cacert=/etc/kubernetes/pki/etcd/ca.crt \
  --cert=/etc/kubernetes/pki/etcd/server.crt \
  --key=/etc/kubernetes/pki/etcd/server.key \
  compact $REV

# Defrag（整理存储空间）
ETCDCTL_API=3 etcdctl \
  --endpoints=https://127.0.0.1:2379 \
  --cacert=/etc/kubernetes/pki/etcd/ca.crt \
  --cert=/etc/kubernetes/pki/etcd/server.crt \
  --key=/etc/kubernetes/pki/etcd/server.key \
  defrag
```

### 节点 drain 卡住

```bash
# 查看哪个 Pod 阻止了 drain
kubectl get events -n <namespace> | grep "Cannot evict"

# 查看 PDB 状态
kubectl get pdb -A

# 常见原因：
# 1. PDB 太严格（minAvailable 等于副本数）
#    → 先调整 PDB 或扩容 Deployment
# 2. StatefulSet 的 Pod（有序性保证，drain 会等待）
#    → 检查 StatefulSet 的 terminationGracePeriodSeconds
# 3. Job Pod（不受 PDB 控制，但 drain 默认等待 Job 完成）
#    → 使用 --delete-emptydir-data

# 查看具体是哪个 Pod 卡住
kubectl get pods -n <namespace> --field-selector=spec.nodeName=<node-name>
```

---

## 回滚方案

### EKS 回滚

```bash
# EKS Control Plane 不支持降级！
# 唯一回滚方式：恢复 Velero 备份到新集群

# 查看备份列表
velero backup get

# 从备份恢复
velero restore create --from-backup pre-upgrade-backup \
  --include-namespaces production

# 等待恢复完成
velero restore describe <restore-name>
```

### 节点回滚（蓝绿方案的优势）

```bash
# 如果使用蓝绿节点组方案，回滚只需要：
# 1. uncordon 旧节点组
# 2. cordon 新节点组
# 3. 将 Pod 驱逐回旧节点

# 这也是为什么推荐蓝绿而不是就地升级
```

### kubeadm 集群回滚（极端情况）

```bash
# 从 etcd snapshot 恢复（只在 Control Plane 升级失败时使用）
systemctl stop kube-apiserver kube-controller-manager kube-scheduler

ETCDCTL_API=3 etcdctl snapshot restore /backup/etcd-snapshot-xxx.db \
  --data-dir=/var/lib/etcd-restore

# 将 /var/lib/etcd 替换为恢复的数据
mv /var/lib/etcd /var/lib/etcd.bak
mv /var/lib/etcd-restore /var/lib/etcd

systemctl start etcd kube-apiserver kube-controller-manager kube-scheduler
```

---

## 升级计划模板

```markdown
## K8s 集群升级计划 — v1.28 → v1.29

### 时间窗口
- 计划时间：周六 02:00 - 06:00（低峰期）
- 预计耗时：4 小时
- 超时回滚时间点：04:00

### 升级前准备（D-3）
- [ ] pluto 扫描 API 废弃，更新所有 manifest
- [ ] etcd/Velero 备份验证可恢复
- [ ] 告知相关团队，在升级窗口内暂停非紧急发布
- [ ] 确认 PDB 配置正确
- [ ] 准备回滚 runbook

### 升级步骤
1. 升级 Control Plane（~20min）
2. 升级 EKS Add-on（~15min）
3. 创建新节点组 v1.29（~10min）
4. 滚动 drain 旧节点组（~60min）
5. 升级验证（~30min）
6. 清理旧节点组（~5min）

### 验证标准
- 所有节点 v1.29 且 Ready
- 所有业务 Pod Running
- 关键接口 P99 延迟正常
- 无新增 Error 日志
```
