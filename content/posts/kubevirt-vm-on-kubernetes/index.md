---
title: "KubeVirt 生产实战：在 Kubernetes 上跑虚拟机的完整路线"
date: 2025-03-29T10:30:00+08:00
draft: false
tags: ["KubeVirt", "Kubernetes", "虚拟化", "VMware替代"]
categories: ["基础设施"]
description: "KubeVirt 1.8 生产落地指南：VirtualMachine / VirtualMachineInstance 的区别、DataVolume 存储、网络模型（masquerade / bridge / multus）、CDI 镜像导入、热迁移、热插拔、备份、监控，以及从 VMware 迁过来的路径与踩坑。"
summary: "Broadcom 吃掉 VMware 之后，VMware 替代方案成了所有基础设施团队的议题。KubeVirt 1.8 已经是个相当成熟的选择，能在 Kubernetes 里跑真正的 VM——不是轻量容器、不是 microVM，是完整的 Windows/Linux VM。这是一年多的实战笔记。"
toc: true
math: false
diagram: false
keywords: ["KubeVirt", "Kubernetes virtualization", "VirtualMachine", "CDI", "live migration", "VMware alternative"]
params:
  reading_time: true
---

## 谁真的需要 KubeVirt

回答这个问题之前先要澄清一件事：容器不是万能的。

有些 workload 必须跑在 VM 里：

- Windows 应用（.NET Framework 老版本、MS SQL Server、AD Controller）；
- 老系统里的 Linux 应用，没法容器化（比如编译时依赖非常复杂、需要特定内核模块）；
- 数据库的"虚拟机优先"部署方式（Oracle、某些需要 huge page 调优的 MySQL）；
- 合规要求："每个客户/租户必须独立 OS 隔离"，不接受容器；
- 开发测试环境需要完整的 OS（开发机、QA lab）。

传统上这些都跑在 vSphere 或者 OpenStack 上。问题是 VMware 在 Broadcom 收购后涨价凶，OpenStack 维护成本又太高。如果你的团队已经在维护一个成熟的 Kubernetes 平台，把 VM 塞进 Kubernetes（让容器和 VM 用同一套调度、存储、网络、监控、CI/CD）反而是最省事的方案。

KubeVirt 就是干这个的。

## KubeVirt 的核心事实

KubeVirt 是 CNCF 孵化项目，现在是 CNCF 毕业状态。最新版本 1.8 在 2026 年 3 月发布，对齐 Kubernetes 1.35。它做的事情概括起来：

1. 用 Kubernetes 的 CRD 定义 VM 资源（VirtualMachine / VirtualMachineInstance）；
2. 用 libvirt + QEMU 在 Pod 里跑 VM；
3. VM 的生命周期由 KubeVirt controller 管理，不是由 kubelet 直接管；
4. VM 的网络、存储、CPU/内存请求走 Kubernetes 一套；
5. VM 和容器能在同一个 node 上共存。

一个 VM 的实际形态：一个叫 `virt-launcher` 的 Pod，里面跑 libvirt + qemu-kvm，qemu 里是 VM guest OS。从 Kubernetes 视角看就是一个 Pod，从 KubeVirt 视角看是一个 VMI，从用户视角看是一台完整的 VM。

## 架构组件

KubeVirt 装好之后你会看到这些 Pod：

- **virt-operator**：负责安装、升级 KubeVirt 本身；
- **virt-api**：KubeVirt 的 admission/conversion webhook；
- **virt-controller**：reconcile VirtualMachine/VirtualMachineInstance；
- **virt-handler**：DaemonSet，每个 node 一个。负责把 VMI 下发到 node、管理本地 libvirt；
- **virt-launcher**：每个 VM 对应一个 Pod，跑 libvirt + qemu。

另外几乎一定会一起装的：

- **CDI (Containerized Data Importer)**：把 VM 镜像（qcow2/raw）导入到 PVC。没有它你只能手动准备 PVC；
- **hostpath-provisioner**（可选）：本地 hostPath 存储的动态供应；
- **KubeVirt Manager / CAaS / Cockpit**（可选）：web UI，不是必需。

## 安装 KubeVirt

KubeVirt 的标准安装：

```bash
# 核心
kubectl apply -f https://github.com/kubevirt/kubevirt/releases/download/v1.8.0/kubevirt-operator.yaml
kubectl apply -f https://github.com/kubevirt/kubevirt/releases/download/v1.8.0/kubevirt-cr.yaml

# CDI
kubectl apply -f https://github.com/kubevirt/containerized-data-importer/releases/download/v1.60.x/cdi-operator.yaml
kubectl apply -f https://github.com/kubevirt/containerized-data-importer/releases/download/v1.60.x/cdi-cr.yaml
```

CR 里有一些生产要调的参数：

```yaml
apiVersion: kubevirt.io/v1
kind: KubeVirt
metadata:
  name: kubevirt
  namespace: kubevirt
spec:
  certificateRotateStrategy: {}
  configuration:
    developerConfiguration:
      featureGates:
        - LiveMigration
        - HotplugVolumes
        - HotplugNICs
        - CPUManager
        - NUMA
        - Snapshot
        - GPU
        - HostDevices
    evictionStrategy: LiveMigrate
    migrations:
      parallelMigrationsPerCluster: 5
      parallelOutboundMigrationsPerNode: 2
      bandwidthPerMigration: 0
      completionTimeoutPerGiB: 800
      progressTimeout: 150
    vmStateStorageClass: rook-ceph-block
  workloadUpdateStrategy:
    workloadUpdateMethods:
      - LiveMigrate
```

几个关键选项：

- **featureGates**：KubeVirt 很多能力是 feature gate 开关的。生产环境一般需要 LiveMigration（热迁移）、Snapshot（快照）、HotplugVolumes（热插拔盘）、CPUManager（CPU 亲和性）。
- **evictionStrategy: LiveMigrate**：当 node drain 时，KubeVirt 会尝试对 VM 做热迁移而不是直接关机。生产必开。
- **migrations.***：热迁移的并发限制。初始配置不要太激进，5 个并发的 cluster-wide 限制够用。
- **workloadUpdateStrategy: LiveMigrate**：KubeVirt 自己升级时对运行中的 VM 如何处理。LiveMigrate 是最安全的——升级 virt-launcher 时先迁走 VM 再升级。

### Node 要求

Node 必须满足：

- Linux kernel 支持 KVM（几乎所有 x86_64 node 都行）；
- `/dev/kvm` 存在；
- CPU 支持虚拟化扩展（Intel VT-x / AMD-V）且在 BIOS 启用；
- 在云上：AWS bare metal / nested virtualization 支持的机型（不是所有机型都行）。

检查：

```bash
# 在 node 上执行
ls -l /dev/kvm
egrep -c 'vmx|svm' /proc/cpuinfo
```

**AWS 用户注意**：不是所有 EC2 机型都支持 nested virtualization。`.metal` 系列直接支持 KVM，其他机型需要用 `i3.metal` / `c5n.metal` / `m5.metal` 之类。如果你用的是普通 `m5.large`，KubeVirt 是跑不了的（或者跑得极其吃亏）。

## 第一个 VM：cirros 冒烟测试

最小 VM 示例：

```yaml
apiVersion: kubevirt.io/v1
kind: VirtualMachine
metadata:
  name: cirros-vm
  namespace: default
spec:
  running: true
  template:
    metadata:
      labels:
        kubevirt.io/vm: cirros-vm
    spec:
      domain:
        cpu:
          cores: 1
        resources:
          requests:
            memory: 128Mi
        devices:
          disks:
            - name: containerdisk
              disk:
                bus: virtio
          interfaces:
            - name: default
              masquerade: {}
      networks:
        - name: default
          pod: {}
      volumes:
        - name: containerdisk
          containerDisk:
            image: quay.io/kubevirt/cirros-container-disk-demo:latest
```

几个新概念：

### VirtualMachine vs VirtualMachineInstance

- **VirtualMachine (VM)**：声明式的 VM 定义。类似 Deployment。它决定 VM 是否应该运行（`spec.running`）和 VM 的 spec。
- **VirtualMachineInstance (VMI)**：实际在跑的 VM 实例。类似 Pod，由 VM controller 创建和管理。手动创建 VMI 也行但不推荐，因为不重启 / 不恢复，类似裸 Pod。

生产**只用 VirtualMachine**，让 controller 帮你管。

### 磁盘和卷

上面的例子用 `containerDisk`，镜像打包在容器镜像里。这种方式适合临时 / 测试场景，因为 containerDisk 是 ephemeral（临时）——VM 关机重启后数据丢失。

生产必须用 PVC 或 DataVolume。

### 网络

上面用了 `masquerade: {}`，这是 KubeVirt 最简单的网络模式：Pod 网络直接映射给 VM，VM 用 NAT 出去。VM 看到的 IP 是内部的，但出网用 Pod 的 IP。适合绝大多数"只需要能访问 Pod 网络"的 VM。

其他模式稍后讲。

## DataVolume：生产存储的标准方式

DataVolume 是 CDI 的 CRD，它把 "导入镜像到 PVC" 这件事自动化了。例子：

```yaml
apiVersion: kubevirt.io/v1
kind: VirtualMachine
metadata:
  name: ubuntu-vm
spec:
  running: true
  dataVolumeTemplates:
    - metadata:
        name: ubuntu-disk
      spec:
        sourceRef:
          kind: DataSource
          name: ubuntu-2404
          namespace: golden-images
        storage:
          resources:
            requests:
              storage: 30Gi
          storageClassName: rook-ceph-block
  template:
    spec:
      domain:
        cpu:
          cores: 2
        resources:
          requests:
            memory: 4Gi
        devices:
          disks:
            - name: rootdisk
              disk:
                bus: virtio
          interfaces:
            - name: default
              masquerade: {}
      networks:
        - name: default
          pod: {}
      volumes:
        - name: rootdisk
          dataVolume:
            name: ubuntu-disk
```

几个重点：

- **dataVolumeTemplates**：VM spec 里定义 DataVolume 模板，VM 创建时自动创建对应的 DataVolume + PVC；
- **sourceRef + DataSource**：不要每个 VM 都重新从 URL 下载镜像。用 DataSource 定义"金像"，VM 创建时 CDI 自动 clone。
- **storageClassName**：必须用 RWX（如果要热迁移）或 RWO（不热迁移），见下一节。

### 准备 golden image

```yaml
apiVersion: cdi.kubevirt.io/v1beta1
kind: DataVolume
metadata:
  name: ubuntu-2404-golden
  namespace: golden-images
spec:
  source:
    http:
      url: "https://cloud-images.ubuntu.com/releases/24.04/release/ubuntu-24.04-server-cloudimg-amd64.img"
  storage:
    resources:
      requests:
        storage: 10Gi
    storageClassName: rook-ceph-block
```

CDI 会下载镜像、转换成 PVC 里的数据。下载一次，后续所有 VM 都 clone 它。

```yaml
apiVersion: cdi.kubevirt.io/v1beta1
kind: DataSource
metadata:
  name: ubuntu-2404
  namespace: golden-images
spec:
  source:
    pvc:
      name: ubuntu-2404-golden
      namespace: golden-images
```

然后 VM 的 `sourceRef` 引用这个 DataSource 就行。

**重要**：golden image 所在的 namespace 通常叫 `golden-images` 或 `os-images`，并且要配置 cross-namespace cloning 权限。CDI 默认不允许跨 namespace clone，需要在目标 namespace 创建 RoleBinding 给 CDI 跨命名空间访问权限。

## 网络模型

KubeVirt 的网络是个复杂话题。主要模式：

### Pod network + masquerade

最简单。VM 用 QEMU 的 NAT 网络，看到一个内部 IP，出网走 Pod IP。Pod 的 Service / Ingress 能像普通 Pod 一样访问 VM 的 port。

适合：大多数"VM 跑应用，只需要出网 + 开一些端口"的场景。

```yaml
interfaces:
  - name: default
    masquerade: {}
    ports:
      - port: 22
      - port: 80
```

限制：VM 看到的 IP 不是 Pod IP，一些应用依赖"自己看到的 IP 等于对外的 IP"时会出问题。

### Pod network + bridge

VM 直接拿到 Pod 的 IP，没有 NAT。更"原生"的网络体验。

```yaml
interfaces:
  - name: default
    bridge: {}
```

限制：有些 CNI 不兼容 bridge 模式。Calico 支持，Cilium 需要特殊配置。

### Multus + 物理网络

复杂场景：VM 需要 VLAN、特定 MAC、直连物理网络。用 Multus + NetworkAttachmentDefinition：

```yaml
apiVersion: k8s.cni.cncf.io/v1
kind: NetworkAttachmentDefinition
metadata:
  name: vlan-100
spec:
  config: |
    {
      "cniVersion": "0.4.0",
      "type": "bridge",
      "bridge": "br0",
      "vlan": 100,
      "ipam": {"type": "whereabouts", "range": "10.100.0.0/24"}
    }
```

VM 引用：

```yaml
networks:
  - name: vlan-100
    multus:
      networkName: vlan-100
interfaces:
  - name: vlan-100
    bridge: {}
```

这个组合用在"VM 要替换物理机，网络拓扑必须保持"的场景。复杂但可行。

### SR-IOV

对带宽 / 延迟敏感的 workload（比如高性能数据库、NFV 应用），KubeVirt 支持 SR-IOV 直通物理网卡给 VM。需要：

- SR-IOV capable NIC；
- Node 开启 SR-IOV；
- 装 sriov-network-operator；
- VM 的 network interface 用 sriov 类型。

不展开，这是一个单独的深坑。

## 热迁移（Live Migration）

这是 KubeVirt 的招牌特性。前提：

1. VM 用的 PVC 必须是 **ReadWriteMany (RWX)** 或者 **Block (RWO)**。RWX 最稳，很多 CSI 都支持；RWO Block 需要 KubeVirt 的 "hotpluggable storage" 支持。
2. Node 之间网络互通；
3. KubeVirt feature gate LiveMigration 开启；
4. VM 的 evictionStrategy 推荐设 `LiveMigrate`，这样 node drain 会自动触发热迁移。

手动触发：

```bash
virtctl migrate my-vm
```

或者 CRD：

```yaml
apiVersion: kubevirt.io/v1
kind: VirtualMachineInstanceMigration
metadata:
  name: migrate-my-vm
spec:
  vmiName: my-vm
```

热迁移在后台做 memory copy + cpu state transfer，VM 里的业务只会有亚秒级的暂停。

**坑**：

1. **存储必须支持跨 node 访问**。Ceph RBD / NFS / Longhorn / GlusterFS / AWS EBS io2 multi-attach 都行；AWS EBS gp3 / gp2 默认 RWO 不行。
2. **CPU model 要一致**。如果 node A 是 Intel Skylake、node B 是 Intel Cascade Lake，迁移过去的 VM 不能用 Cascade Lake 新指令。解决：`spec.domain.cpu.model: Skylake-Client` 或 `Nehalem` 这种 baseline。
3. **大内存 VM 迁移慢**。32GB 内存的 VM 迁移可能要几十秒。调优 `completionTimeoutPerGiB` 和 `bandwidthPerMigration`。
4. **PCI 直通设备**（GPU / SR-IOV NIC）**不能热迁移**。这是 KVM 的限制，KubeVirt 也绕不过。

## Snapshot 和 Backup

### VirtualMachineSnapshot

在 VM 一致性状态做快照：

```yaml
apiVersion: snapshot.kubevirt.io/v1beta1
kind: VirtualMachineSnapshot
metadata:
  name: my-vm-snap-1
spec:
  source:
    apiGroup: kubevirt.io
    kind: VirtualMachine
    name: my-vm
```

KubeVirt 会协调 guest agent（如果装了）做 freeze，然后对底层 PVC 做 snapshot（依赖 CSI 的 VolumeSnapshot 能力）。恢复用 VirtualMachineRestore。

要求：

- 底层 CSI 必须支持 VolumeSnapshot；
- 最好在 guest 里装 qemu-guest-agent，否则只能做 crash-consistent 快照（可能 FS 不一致）。

### Incremental Backup with CBT（1.8 新特性）

KubeVirt 1.8 引入了基于 CBT (Changed Block Tracking) 的增量备份。使用 qemu / libvirt 自身的 CBT 能力，只备份变更的块。这是 VMware vSphere 的 CBT 在 KubeVirt 上的对应物。

```yaml
apiVersion: backup.kubevirt.io/v1alpha1
kind: BackupConfiguration
spec:
  # ... 具体 API 还在 beta 阶段，生产可以先用 Velero + VolumeSnapshot 组合
```

实际生产我们用的是 Velero + CSI snapshot。等 KubeVirt 1.8 的增量备份 API 稳定再迁。

## 从 VMware 迁 VM 过来

这是最多团队关心的路径。KubeVirt 有一个叫 Forklift（或者 MTV, Migration Toolkit for Virtualization）的项目，专门做 vSphere → KubeVirt 的迁移。

大致步骤：

1. 装 Forklift operator；
2. 创建 Provider 定义源 vSphere 和目标 KubeVirt；
3. 创建 Plan，列出要迁的 VM 列表；
4. 运行 Plan，Forklift 会：
   - 用 virt-v2v 把 vSphere 的 VM 磁盘转换成 qcow2；
   - 把数据传输到目标 CDI PVC；
   - 生成 KubeVirt VirtualMachine CR；
   - 可选 cutover：关掉源 VM、启动目标 VM。

**实战踩过的坑**：

- **网络延迟**：源 vSphere 和目标 KubeVirt 之间的带宽决定迁移速度。跨区域迁移大 VM 可能要几小时。
- **Windows 驱动**：迁 Windows VM 要先在源 VM 里装 virtio 驱动，否则启动蓝屏。virt-v2v 一般会处理但不是 100%。
- **UEFI / Legacy Boot**：有些老 VM 是 BIOS 模式，KubeVirt 默认是 UEFI，需要在目标 VM spec 里显式声明 `firmware.bootloader.bios: {}`。
- **应用 IP / License**：VM 里的应用如果 hardcode 了 IP 或者依赖 MAC 做 licensing，迁移后会失效。这些必须提前盘点。
- **非 thin 磁盘**：vSphere 的 thick provisioned 磁盘迁过来会占满 PVC 声明的空间，即使 guest 里只用了一部分。用 `sparsify` 先瘦身。

**迁移策略**：

- 不要一次性迁完。分批，先迁 dev / test，再迁非关键 prod，最后核心 prod；
- 迁 cold VM（能停机的）比 warm / hot VM 容易；
- 窗口期预留 2-3 倍预估时间。

## GPU 和 PCI 直通

GPU VM 是个大话题。简单版本：

```yaml
apiVersion: kubevirt.io/v1
kind: VirtualMachine
spec:
  template:
    spec:
      domain:
        devices:
          gpus:
            - deviceName: nvidia.com/TU104GL_Tesla_T4
              name: gpu1
```

前提：

- Node 上装了 NVIDIA driver + vfio-pci；
- PermittedHostDevices 在 KubeVirt CR 里声明：

```yaml
spec:
  configuration:
    permittedHostDevices:
      pciHostDevices:
        - pciVendorSelector: "10de:1eb8"
          resourceName: nvidia.com/TU104GL_Tesla_T4
```

- virt-handler 会管理 GPU 的分配。

**限制**：GPU 直通的 VM **不能热迁移**。这是 KVM 的硬限制。对 AI 训练场景要注意：一旦 VM 跑起来，如果 node 出问题你只能冷迁移（关机 + 启动），中间有 downtime。

## 监控

KubeVirt 暴露了大量 Prometheus metrics：

- `kubevirt_vm_created_total`、`kubevirt_vm_deleted_total`：VM 创建/删除计数；
- `kubevirt_vmi_memory_available_bytes` / `kubevirt_vmi_memory_used_bytes`：VM 内存情况；
- `kubevirt_vmi_cpu_usage_seconds_total`：CPU 使用；
- `kubevirt_vmi_network_receive_bytes_total` / `transmit_bytes_total`：网络流量；
- `kubevirt_vmi_migration_*`：热迁移成功率、耗时；
- `kubevirt_vmi_storage_iops_total` / `traffic_bytes_total`：VM 磁盘 IO。

对 VMware 管理员来说这些指标可能不够"传统"（比如没有 CPU ready time 这种 ESX 指标），但基本够用。

几个核心告警：

```yaml
- alert: KubeVirtVMIDown
  expr: |
    kubevirt_vmi_phase_count{phase="Failed"} > 0
  for: 5m
  labels:
    severity: critical
  annotations:
    summary: "VM {{ $labels.name }} 处于 Failed 状态"

- alert: KubeVirtLiveMigrationFailed
  expr: |
    increase(kubevirt_vmi_migration_failed_total[30m]) > 0
  labels:
    severity: warning
  annotations:
    summary: "KubeVirt 热迁移失败"
```

## 和容器 workload 共存

一个物理 node 可以同时跑容器 Pod 和 virt-launcher Pod。KubeVirt 在这一点上和传统 hypervisor 不同——你不需要"专门的 VM 节点"。

但生产上我还是建议**分池管理**：

- node taint：`workload=vm:NoSchedule`；
- VM 的 Pod spec 加对应 toleration；
- 容器 workload 不加 toleration，调度不到 VM node。

好处：

- VM 的 "hot" 资源（CPU pinning / huge pages / SR-IOV）不会被容器干扰；
- VM 的调度行为可预测；
- 运维上易于区分。

坏处：
- 资源利用率略低。

对"生产 VM"我强烈建议分池。对"开发 / 测试 VM" 可以混跑，成本更低。

## 踩过的坑总结

### 坑 1：cloud-init 没配

VM 起来之后 SSH 登不进去。99% 是 cloud-init 没设：

```yaml
volumes:
  - name: cloudinit
    cloudInitNoCloud:
      userData: |
        #cloud-config
        users:
          - name: ubuntu
            sudo: ALL=(ALL) NOPASSWD:ALL
            ssh_authorized_keys:
              - ssh-ed25519 AAAA... user@example
```

对应 disk：

```yaml
disks:
  - name: cloudinit
    disk:
      bus: virtio
```

### 坑 2：virt-launcher 被 OOM

VM 内存声明 4Gi，virt-launcher Pod 的 memory request 默认只加了少量开销。当 guest 里内存用满时，virt-launcher 本身可能 OOM。解决：给 VM spec 加 memoryOvercommit 或者 overhead guarantees。KubeVirt 1.5+ 的 `spec.domain.memory.guest` 更精确。

### 坑 3：CPU 利用率不对

KubeVirt 的 VM 实际是 qemu 进程，CPU 请求 `cores: 4`。但 Kubernetes 看到的 Pod CPU request 可能只有 100m，因为 KubeVirt 默认不把 guest CPU 请求映射到 Pod request（避免过度调度）。结果是 VM 和容器同 node，容器把 CPU 吃满，VM 明显卡。

解决：打开 **dedicatedCpuPlacement** 或者显式设 `spec.domain.resources.requests.cpu`，让 Pod 的 request 反映真实 CPU 需求。生产一般要开 dedicatedCpuPlacement 做 CPU pinning。

### 坑 4：VM 重启 IP 变

如果你用的是 pod 网络 masquerade / bridge，Pod 重启 Pod IP 会变。对"VM 期望 IP 稳定" 的场景（Windows AD 之类），这是不可接受的。解决：

- 用 Multus + 静态 IPAM（比如 whereabouts）；
- 或者在 VM 外部做 DNS 映射，应用不依赖 IP。

### 坑 5：Windows VM 的许可问题

License 模型不同。Windows VM 在 KubeVirt 上，Microsoft 对许可的要求你要跟法务核对。这不是 KubeVirt 的锅但是你的责任。

### 坑 6：virt-handler 升级

升级 KubeVirt 时 virt-handler DaemonSet 会被重建。`workloadUpdateStrategy: LiveMigrate` 会让 controller 主动热迁移 VM 到新版本的 virt-handler。但如果 VM 不可热迁（GPU 直通），这些 VM 会被保留在旧节点上，升级要手动处理。

## 什么场景不要用 KubeVirt

说点反面的：

- **你没有任何 VM workload**：别硬塞 VM 进来。
- **应用可以容器化**：能容器化就容器化，容器的资源效率比 VM 高得多。
- **你对底层 QEMU / libvirt 零经验**：KubeVirt 的排障需要 debug 到 libvirt 日志，完全不懂 KVM 的人维护会很痛。
- **GPU 密集 workload 要求热迁移**：KubeVirt + GPU 直通没法热迁移。
- **网络需要极端性能（100Gbps+）**：考虑专门的 VM 平台（OpenStack）或裸金属。

## 和 OpenStack / VMware 的对比

| 维度 | KubeVirt | OpenStack | VMware vSphere |
|---|---|---|---|
| 学习曲线 | 中（会 K8s 就能用） | 高 | 中 |
| 安装复杂度 | 低（Helm / operator） | 极高 | 中 |
| 多租户 | 依赖 K8s RBAC + namespace | 原生 Project 隔离 | 原生 |
| 存储选项 | K8s CSI 生态 | Cinder | VMFS/vSAN |
| 网络高级特性 | 依赖 Multus + 生态 | 原生 Neutron 丰富 | NSX |
| 社区成熟度 | 中到高 | 高 | 商业 |
| 价格 | 开源 | 开源 | 贵 |
| 和容器混部 | 原生 | 有方案 | 有方案 |
| 适合的团队 | 已有 K8s 团队 | VMs-first | 传统 IT |

我的结论：**如果你已经运维一个成熟的 Kubernetes 平台**，KubeVirt 是目前 VMware 替代方案里性价比最高的；**如果你只有 VM workload，没 K8s 经验**，OpenStack 或者 Proxmox 可能更对路。

## 最后

KubeVirt 1.8 这个版本让我觉得"它真的成熟了"。早年 KubeVirt 很多是"能跑，但不敢生产"，现在已经到了"能跑 Windows SQL Server，而且跑得挺稳"的程度。

如果你是 SRE 或者平台工程师，在你的老板下一次问 "我们要不要换 VMware 替代方案" 的时候，KubeVirt 值得认真评估。前提是你已经把 Kubernetes 运维好了。把 VM 塞进一个稳定的 K8s 平台是可行的，把 VM 塞进一个不稳定的 K8s 平台是灾难。

这一年跑下来最大的感受是：KubeVirt 让 VM 变成了"另一种 Pod"。你用同一套 CI/CD、同一套 GitOps、同一套 monitoring、同一套 IAM/RBAC 管 VM 和容器，运维体验的一致性非常舒服。对于那些"最后几个不能容器化的老服务"，它把最后一片拼图补上了。
