---
title: "Rook-Ceph on Kubernetes 运维实战：从部署到故障恢复"
date: 2024-12-13T11:00:00+08:00
draft: false
tags: ["Ceph", "Rook", "Kubernetes", "存储", "分布式存储"]
categories: ["存储"]
description: "Rook 把 Ceph 包装成了 Kubernetes 原生存储方案，但 Ceph 的复杂度并没有因为 Rook 就消失。这篇笔记覆盖 Rook-Ceph 的架构理解、CephCluster CR 设计、OSD 布局、pool 和 crush rule 的配置、性能调优、常见故障（OSD down、PG 卡住、慢请求）的处置流程，以及 Rook 运维的几个容易踩坑的地方。"
summary: "当你需要在 Kubernetes 上提供 block、file、object 三种存储时，Rook-Ceph 是几乎没有替代品的方案。但它的复杂度也是所有 K8s 存储方案里最高的。这篇文章是我在一套裸金属 Rook-Ceph 生产集群上两年运维经验的整理，包括几次把集群从悬崖边拉回来的复盘。"
toc: true
math: false
diagram: false
keywords: ["Rook", "Ceph", "Kubernetes", "CephCluster", "OSD", "crush", "RBD", "CephFS"]
params:
  reading_time: true
---

## 写在前面

先说一个结论：**如果你不是真的需要 Ceph，就不要上 Rook-Ceph**。这不是黑它，是一个实话。Rook 让部署 Ceph 变简单，但它让 Ceph 运维变简单了吗？没有。Ceph 的复杂度还在那儿，只是换了一层皮。

那么什么时候"真的需要 Ceph"？

1. **你需要在 K8s 上同时提供 block、file、object 三种存储**
2. **你在裸金属上部署，没有云盘可用**
3. **你的数据量和 IOPS 足以让商业存储吃不消或吃不起**
4. **你的团队有 Ceph 知识储备**

如果上面有一条不满足，先考虑 Longhorn、OpenEBS Mayastor、Piraeus（LINSTOR）或者直接用云盘。

这篇文章基于 Rook v1.15 和 Ceph Squid (v19)，也会提到 Reef 的一些差异。

## 一、先理解 Ceph，再谈 Rook

Rook 不会魔法般地让你不懂 Ceph 也能运维它。出了问题你最终要去看 `ceph -s`、`ceph osd tree`、`ceph pg dump`。所以花 30 分钟把 Ceph 的核心概念理解透比装 10 次 Rook 都重要。

### 1.1 Ceph 的核心组件

```
                    +------------+
                    |   Client   |
                    |  (kRBD/    |
                    |  CephFS)   |
                    +-----+------+
                          |
          +---------------+---------------+
          |                               |
     +----+----+                    +-----+-----+
     |   MON   |                    |  OSD 池   |
     | (3-5个) |                    | 数十到千级|
     | 元数据  |                    |  数据存储 |
     +----+----+                    +-----+-----+
          |                               |
     +----+----+                          |
     |   MGR   | 管理 metrics/balancer    |
     +---------+                          |
                                          |
     +---------+                    +-----+-----+
     |   MDS   |  (CephFS 用)       | BlueStore |
     | Metadata|                    | RocksDB+  |
     |  Server |                    |   裸盘    |
     +---------+                    +-----------+
```

- **MON**：维护 cluster map（OSD、crush map 等）。3 或 5 个，奇数。
- **OSD**：Object Storage Daemon，一个 OSD 通常对应一块物理盘。
- **MGR**：Manager，负责 metrics、balancer、dashboard。
- **MDS**：Metadata Server，只给 CephFS 用。
- **RGW**：Rados Gateway，提供 S3/Swift API，类似 MinIO。

### 1.2 数据怎么分布：CRUSH

Ceph 的数据分布用 CRUSH 算法（Controlled Replication Under Scalable Hashing）。核心思想：

1. 对象通过哈希计算落到某个 **PG (Placement Group)**
2. PG 通过 CRUSH 算法映射到一组 OSD（通常 3 个，对应三副本）
3. CRUSH 算法基于 **CRUSH map**（数据中心 → 机架 → 主机 → OSD 的树形结构）

CRUSH 的好处是**无中心元数据**：任何客户端给定对象 key 都能独立算出它在哪几个 OSD 上，不需要查询。代价是 CRUSH map 的设计很讲究。

### 1.3 PG 数：最重要的参数

每个 pool 都有 PG 数，它决定了：

- 数据的分布粒度：PG 多 → 分布均匀；PG 少 → 容易倾斜
- 元数据开销：PG 多 → MON 和 OSD 内存占用大
- Recovery 粒度：PG 多 → recovery 更灵活

推荐公式：

```
PG 数 = (OSD 数 × 100) / 副本数

举例：30 OSD + 三副本 → 30 × 100 / 3 = 1000 → 取 2 的幂 = 1024
```

**每个 OSD 承担的 PG 不要超过 200**，超过会 OOM。

Reef 之后的版本有 **pg_autoscaler** 能自动调整 PG 数，但我还是建议手动算好初始值，让 autoscaler 微调，而不是完全托管。

## 二、Rook 部署实战

### 2.1 基础装 Operator

```bash
kubectl create namespace rook-ceph
kubectl apply -f https://raw.githubusercontent.com/rook/rook/v1.15.0/deploy/examples/crds.yaml
kubectl apply -f https://raw.githubusercontent.com/rook/rook/v1.15.0/deploy/examples/common.yaml
kubectl apply -f https://raw.githubusercontent.com/rook/rook/v1.15.0/deploy/examples/operator.yaml
```

等 operator 起来：

```bash
kubectl -n rook-ceph get pod -l app=rook-ceph-operator
```

### 2.2 CephCluster CR

这是 Rook 的核心资源，定义了整个 Ceph 集群：

```yaml
apiVersion: ceph.rook.io/v1
kind: CephCluster
metadata:
  name: rook-ceph
  namespace: rook-ceph
spec:
  cephVersion:
    image: quay.io/ceph/ceph:v19.2.0
    allowUnsupported: false
  dataDirHostPath: /var/lib/rook
  skipUpgradeChecks: false
  continueUpgradeAfterChecksEvenIfNotHealthy: false
  waitTimeoutForHealthyOSDInMinutes: 10

  mon:
    count: 3
    allowMultiplePerNode: false
    volumeClaimTemplate:
      spec:
        storageClassName: local-storage
        resources:
          requests:
            storage: 10Gi

  mgr:
    count: 2
    allowMultiplePerNode: false
    modules:
      - name: pg_autoscaler
        enabled: true
      - name: balancer
        enabled: true
      - name: prometheus
        enabled: true

  dashboard:
    enabled: true
    ssl: true

  monitoring:
    enabled: true
    metricsDisabled: false

  network:
    provider: host
    connections:
      encryption:
        enabled: false        # msgr2 加密，有 CPU 代价
      compression:
        enabled: false

  storage:
    useAllNodes: false
    useAllDevices: false
    nodes:
      - name: "storage-01"
        devices:
          - name: "nvme0n1"
          - name: "nvme1n1"
          - name: "nvme2n1"
          - name: "nvme3n1"
      - name: "storage-02"
        devices:
          - name: "nvme0n1"
          - name: "nvme1n1"
          - name: "nvme2n1"
          - name: "nvme3n1"
      - name: "storage-03"
        devices:
          - name: "nvme0n1"
          - name: "nvme1n1"
          - name: "nvme2n1"
          - name: "nvme3n1"

  resources:
    mon:
      requests:
        cpu: "1000m"
        memory: "4Gi"
      # 注意：不要设 limits，Ceph 认为 daemon 资源应该 guaranteed
    mgr:
      requests:
        cpu: "500m"
        memory: "1Gi"
    osd:
      requests:
        cpu: "2000m"
        memory: "4Gi"
```

几个要点：

1. **`useAllDevices: false`**：生产绝不开 `useAllDevices: true`，会把系统盘也抓进去
2. **显式列 devices**：每台节点哪几块盘给 Ceph，明明白白
3. **不设 limits**：Ceph daemon 是 critical，OOM Killer 不能动它
4. **`network.provider: host`**：host network 比 K8s CNI 性能高 20-50%，生产推荐
5. **mon count: 3**：最少 3 个，允许一个 mon 故障

### 2.3 Pool 和 StorageClass

```yaml
apiVersion: ceph.rook.io/v1
kind: CephBlockPool
metadata:
  name: replicapool
  namespace: rook-ceph
spec:
  failureDomain: host          # 副本分布在不同 host
  replicated:
    size: 3
    requireSafeReplicaSize: true
  parameters:
    compression_mode: none
  deviceClass: ssd             # 只用 ssd 类型 OSD

---
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: rook-ceph-block
provisioner: rook-ceph.rbd.csi.ceph.com
parameters:
  clusterID: rook-ceph
  pool: replicapool
  imageFormat: "2"
  imageFeatures: layering
  csi.storage.k8s.io/provisioner-secret-name: rook-csi-rbd-provisioner
  csi.storage.k8s.io/provisioner-secret-namespace: rook-ceph
  csi.storage.k8s.io/controller-expand-secret-name: rook-csi-rbd-provisioner
  csi.storage.k8s.io/controller-expand-secret-namespace: rook-ceph
  csi.storage.k8s.io/node-stage-secret-name: rook-csi-rbd-node
  csi.storage.k8s.io/node-stage-secret-namespace: rook-ceph
  csi.storage.k8s.io/fstype: xfs
reclaimPolicy: Delete
allowVolumeExpansion: true
```

`failureDomain: host` 保证副本不会落在同一台机器。如果你有多机架，可以设 `rack` 做更强隔离。

### 2.4 Toolbox 是你的朋友

Rook 提供一个 toolbox pod 方便用原生 ceph 命令：

```bash
kubectl apply -f https://raw.githubusercontent.com/rook/rook/v1.15.0/deploy/examples/toolbox.yaml
kubectl -n rook-ceph exec -it deploy/rook-ceph-tools -- ceph -s
```

所有复杂问题最后都在这里解决：

```bash
ceph -s                 # 集群状态概览
ceph osd tree           # OSD 拓扑
ceph df                 # 容量
ceph pg dump            # PG 详情
ceph osd df             # 每个 OSD 的容量
ceph health detail      # 详细健康信息
```

## 三、几个重要的调优点

### 3.1 BlueStore 配置

现代 Ceph 用 BlueStore 作为 OSD 的存储引擎，直接管理裸盘不走文件系统。关键参数（在 CephCluster 里通过 `cephConfig` 注入）：

```yaml
spec:
  cephConfig:
    global:
      bluestore_cache_size_ssd: "4294967296"    # 4GB per OSD
      bluestore_cache_size_hdd: "1073741824"    # 1GB per OSD
      osd_memory_target: "8589934592"           # 8GB per OSD
      osd_max_backfills: "4"
      osd_recovery_max_active: "4"
      osd_recovery_op_priority: "3"
```

`osd_memory_target` 是每个 OSD 的目标内存使用，BlueStore cache + 其他开销加起来不超过这个值。设得太小 cache 命中率低、太大 OOM。

### 3.2 Recovery 参数

Recovery（数据恢复）和 Backfill（数据迁移）对业务 IO 有影响。默认参数偏保守：

```yaml
osd_max_backfills: "1"
osd_recovery_max_active: "1"
osd_recovery_sleep_ssd: "0"
```

SSD 集群可以放开：

```yaml
osd_max_backfills: "4"
osd_recovery_max_active: "4"
osd_recovery_sleep_ssd: "0"
```

但要在业务低峰做，高峰期 recovery 会影响 P99。临时恢复速率调整：

```bash
ceph config set osd osd_max_backfills 8
# 恢复结束后
ceph config set osd osd_max_backfills 1
```

### 3.3 PG 数调整

用 autoscaler 或手动：

```bash
# 查看当前自动调整的建议
ceph osd pool autoscale-status

# 手动调整（会触发大规模 recovery）
ceph osd pool set replicapool pg_num 1024
ceph osd pool set replicapool pgp_num 1024
```

**永远在业务低峰调整 PG 数**，大集群可能 recovery 几小时。

### 3.4 Balancer

Ceph 自带 balancer 模块，根据 CRUSH 动态均衡 OSD 负载：

```bash
ceph balancer status
ceph balancer mode upmap
ceph balancer on
```

`upmap` 模式比 `crush-compat` 更精细，推荐生产用 upmap。要求客户端版本 Luminous+（基本都满足）。

## 四、容量管理的生死线

Ceph 有几个关键容量水位：

| 水位                  | 默认值 | 行为                           |
|-----------------------|--------|--------------------------------|
| mon_osd_nearfull_ratio | 85%    | HEALTH_WARN                    |
| mon_osd_backfillfull_ratio | 90% | 拒绝 backfill                  |
| mon_osd_full_ratio    | 95%    | **拒绝所有写入，集群只读**     |

一旦到 95% 全集群只读，处置极其痛苦（只能删数据或加盘）。所以**任何 OSD 的使用率超过 80% 就要开始扩容**。

监控：

```bash
ceph osd df | awk '$8+0 > 80 {print}'
```

扩容方法：加新 OSD，Ceph 会自动 rebalance。一个 4TB 的新 OSD 完整 rebalance 通常要几小时。

**绝对不要让 OSD 跑到 85%+**。我遇到过一次，处理过程是：

1. 紧急加盘 → balancer 分流
2. 删除不必要的快照
3. 降低冷 pool 的副本数从 3 到 2（临时）
4. 拼命 recovery

整个过程花了 8 小时，期间业务读写受影响。

## 五、真实故障复盘

### 5.1 PG 卡在 peering 状态

**现象**：`ceph -s` 显示 `pgs: 42 peering`，`ceph pg dump` 里这些 PG 状态一直是 `peering` 或 `creating+peering`。

**排查**：

```bash
ceph pg dump_stuck
ceph pg <pgid> query
```

`query` 的输出里能看到 PG 在等哪个 OSD。发现是某个 OSD 节点的时间同步出了问题，时钟漂移超过 30 秒，MON 拒绝它加入。

**修复**：

1. `chronyd` 重启，强制时间同步
2. OSD 重启
3. PG peering 完成

**教训**：Ceph 对时钟非常敏感，所有节点必须装 chrony/ntp，时钟漂移 > 50ms 就告警。

### 5.2 OSD 频繁 down/up 导致集群不稳

**现象**：某台 storage 节点上 4 个 OSD 每隔几分钟 down 一次又自动 up，`ceph -s` 一直报 slow ops。

**排查**：看 OSD log，发现大量 `wrongly marked down` 日志，然后 heartbeat check 失败。

**根因**：这台节点的网卡 bonding 有个不稳定的 slave，偶尔丢包。OSD heartbeat 失败就被 MON 标记 down，然后 OSD 自己发现还活着又报 up。反复发生。

**修复**：换网卡 + 加 heartbeat 超时容忍度：

```yaml
mon_osd_down_out_interval: "600"    # 默认 600 秒保持不变
osd_heartbeat_interval: "10"         # 默认 6
osd_heartbeat_grace: "60"            # 默认 20
```

**教训**：网络稳定性对 Ceph 至关重要，不要省网卡钱。生产集群推荐万兆 + bonding。

### 5.3 Rook Operator 升级导致的连锁故障

**现象**：升级 Rook Operator 从 1.12 到 1.14，升级完成后 OSD 开始大量重启，集群 HEALTH_ERR。

**根因**：Rook 跨大版本升级时 OSD 的 StatefulSet spec 变了，Operator 按新 spec 重建 OSD pod，但启动参数和旧数据不兼容。

**修复**：回滚 Operator，然后按官方 upgrade guide 严格走：

1. 先小版本升级（1.12 → 1.13）
2. 等集群稳定再 1.13 → 1.14
3. 每个版本之间留 24 小时观察

**教训**：Rook 大版本升级必须读 upgrade guide，**绝对不要跨版本升级**。同时升级前把 `continueUpgradeAfterChecksEvenIfNotHealthy: false`，确保健康检查不过就停下。

### 5.4 RBD image 快照太多导致删除超时

**现象**：删除一个旧的 PV，RBD image 对应的 CephBlockPoolRadosNamespace 里有几千个 snapshot，csi-rbd 删除调用超时，PV 一直 Terminating。

**修复**：

```bash
# 进 toolbox
rbd snap purge <pool>/<image>
rbd rm <pool>/<image>
```

然后给 PV patch 掉 finalizer：

```bash
kubectl patch pv <pv-name> --type json -p='[{"op": "remove", "path": "/metadata/finalizers"}]'
```

**教训**：RBD snapshot 不是免费的，Velero backup 或 CSI snapshot 要控制数量，定期清理老 snapshot。

## 六、监控与告警

Rook 自带的 Prometheus ServiceMonitor 能抓到核心指标：

```yaml
- alert: CephClusterNotHealthy
  expr: ceph_health_status != 0
  for: 5m
  annotations:
    summary: "Ceph 集群状态异常"

- alert: CephOSDDown
  expr: ceph_osd_up == 0
  for: 5m

- alert: CephPoolNearFull
  expr: ceph_pool_percent_used > 0.8
  for: 10m

- alert: CephPgInactive
  expr: sum(ceph_pg_active) / sum(ceph_pg_total) < 1
  for: 5m
  labels:
    severity: critical

- alert: CephSlowOps
  expr: ceph_healthcheck_slow_ops > 0
  for: 5m

- alert: CephMDSDown
  expr: ceph_mds_up == 0
  for: 5m
```

另外强烈建议部署 Ceph Dashboard 并且把它接到 SSO，日常排查非常方便。

## 七、备份与灾备

Rook-Ceph 的备份方案有几层：

1. **RBD 快照**：pool 级，增量，恢复快，但依赖集群本身
2. **RBD Mirror**：跨集群异步复制
3. **Velero + CSI Snapshot**：K8s 原生备份 PV
4. **应用层备份**：数据库自己 dump 到 S3

生产建议至少做到 1 + 3：集群内快照防误删、Velero 到外部存储防集群整体故障。

Velero 示例：

```bash
velero install \
  --provider aws \
  --bucket velero-backups \
  --secret-file credentials-velero \
  --use-volume-snapshots=true \
  --snapshot-location-config region=us-west-2
```

配合 Ceph CSI snapshot：

```yaml
apiVersion: snapshot.storage.k8s.io/v1
kind: VolumeSnapshotClass
metadata:
  name: csi-rbdplugin-snapclass
driver: rook-ceph.rbd.csi.ceph.com
deletionPolicy: Delete
parameters:
  clusterID: rook-ceph
  csi.storage.k8s.io/snapshotter-secret-name: rook-csi-rbd-provisioner
  csi.storage.k8s.io/snapshotter-secret-namespace: rook-ceph
```

## 八、Rook vs 其他 K8s 存储方案

最后做一个选型对比：

| 方案                  | 适用场景                 | 复杂度 | 成熟度    |
|-----------------------|--------------------------|--------|-----------|
| Rook-Ceph             | 大规模多模态、裸金属     | 极高   | 成熟      |
| Longhorn              | 中小规模 block           | 低     | 成熟      |
| OpenEBS Mayastor      | 高性能 NVMe block        | 中     | 较新      |
| Piraeus (LINSTOR)     | 企业 block               | 中     | 成熟      |
| Portworx              | 商业，功能全             | 中     | 成熟      |
| 云厂商 CSI            | 云上                     | 低     | 成熟      |

我的决策原则：

- **云上**：直接用云厂商 CSI，ebs-csi / pd-csi 都很好
- **裸金属 + 小规模**：Longhorn 开始
- **裸金属 + 大规模 + 多模态**：Rook-Ceph
- **裸金属 + 高性能 block 至上**：Mayastor 或 Piraeus

## 九、经验法则

- **不懂 Ceph 不要碰 Rook**
- **网络稳定性 > 磁盘性能**
- **时钟必须同步**
- **不要 useAllDevices**
- **OSD 不设 limits**
- **容量 80% 是警戒线**
- **不要跨版本升级 Rook**
- **Recovery 在低峰做**
- **监控要细到 per-OSD**
- **toolbox 常备，ceph 命令熟练**

Rook-Ceph 是一套强大但不宽容的系统。它能让你用一套集群同时提供 block/file/object、能扛住数百 PB、能灵活做多机房容灾。但前提是你对它有足够敬畏，监控要狠、容量要留足、升级要稳。

希望这篇笔记能让你的 Rook-Ceph 集群少挂几次。

参考资料：

- Rook 官方文档 rook.io/docs
- Ceph 官方文档 docs.ceph.com，Squid 和 Reef 两个版本
- SUSE 的 Rook Best Practices 白皮书
- CloudOps 的 Rook Survival Guide
- `ceph -s` 和 `ceph pg query` 的实际输出格式参考 Ceph 官方
