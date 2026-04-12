---
title: "Kubernetes cgroup v2 迁移实践"
date: 2026-04-12T11:00:00+08:00
draft: false
tags: ["Kubernetes", "cgroup", "Linux", "性能调优", "节点运维"]
categories: ["Kubernetes"]
description: "深入讲解 cgroup v1 与 v2 的核心差异，覆盖 Ubuntu/Amazon Linux/CentOS 节点迁移步骤、containerd 配置、MemoryQoS 启用、PSI 监控集成，以及滚动迁移策略。"
summary: "K8s 1.25+ 默认启用 cgroup v2，MemoryQoS 和 PSI 等新特性只在 v2 支持。本文给出完整的节点迁移操作流程和常见问题解决方案。"
toc: true
math: false
diagram: false
keywords: ["cgroup v2", "Kubernetes", "MemoryQoS", "PSI", "containerd", "节点迁移"]
params:
  reading_time: true
---

## cgroup v1 vs v2 核心差异

### 层级结构变了

cgroup v1 的最大问题是**分裂的层级**：每个子系统（cpu、memory、blkio...）各自维护一棵树，进程可以同时存在于多棵树的不同位置。这导致控制逻辑分散，内核实现复杂，子系统之间无法协调。

cgroup v2 引入**统一层级（Unified Hierarchy）**：所有资源控制器共用一棵 cgroup 树，进程只能属于一个 cgroup。这个改变让资源控制的语义更清晰，也让内核能做跨资源的协调决策。

```bash
# v1：多个挂载点，各自独立
ls /sys/fs/cgroup/
# cpuset  cpu,cpuacct  memory  blkio  devices  pids ...

# v2：单一统一挂载点
ls /sys/fs/cgroup/
# cgroup.controllers  cgroup.procs  cgroup.subtree_control  memory.stat  ...
```

### PSI：压力感知指标

PSI（Pressure Stall Information）是 cgroup v2 引入的关键可观测性特性，能精确衡量 CPU、内存、IO 资源的竞争压力：

- `some`：至少一个任务因等待资源而停滞的时间占比
- `full`：所有可运行任务都在等待资源的时间占比（系统完全停摆）

```bash
# 查看系统级 PSI
cat /proc/pressure/memory
# some avg10=0.23 avg60=0.15 avg300=0.08 total=12345678
# full avg10=0.01 avg60=0.00 avg300=0.00 total=987654

# 查看某个 cgroup 的内存压力
cat /sys/fs/cgroup/kubepods/burstable/pod-xxx/memory.pressure
```

v1 没有 PSI，只有 `memory.stat` 里的静态计数器，无法判断当前系统是否真的在承压。

### 内存控制改进

| 特性 | cgroup v1 | cgroup v2 |
|------|-----------|-----------|
| 内存软限制 | `memory.soft_limit_in_bytes`（内核几乎不执行） | `memory.high`（实际有效，触发回收而非 OOM） |
| 内存保证 | 无 | `memory.min`（保证不被回收） |
| Swap 控制 | `memory.memsw.limit_in_bytes` | `memory.swap.max` |
| OOM 策略 | 粗粒度 | `memory.oom.group`（组内 OOM 策略） |

v2 的 `memory.high` 是个重要改进：当容器内存使用达到 high 时，内核主动触发内存回收（throttling），而不是直接 OOM Kill，给应用更多喘息空间。

### MemoryQoS

MemoryQoS 是 Kubernetes 基于 cgroup v2 构建的特性，按 QoS 类细化内存控制：

- **Guaranteed**：`memory.min = memory.limit`，内存完全保证不被回收
- **Burstable**：`memory.min = requests`，`memory.high = limits * ratio`
- **BestEffort**：`memory.min = 0`，内存压力时优先被回收

---

## 迁移前检查清单

### 内核版本要求

cgroup v2 需要 Linux 5.x 以上才能完整支持所有特性：

```bash
uname -r
# 要求：>= 5.4（基础支持）
# 推荐：>= 5.15（PSI、MemoryQoS 完整支持）

# 检查内核是否编译了 cgroup v2 支持
grep CONFIG_CGROUP /boot/config-$(uname -r) | grep -E "CGROUP_V2|MEMCG"
# CONFIG_CGROUP_V2=y
# CONFIG_MEMCG=y
```

各发行版内核情况：
- Ubuntu 22.04 LTS：5.15，开箱即用
- Amazon Linux 2023：6.1，开箱即用
- CentOS Stream 9：5.14，满足要求
- Amazon Linux 2：5.10（需升级内核或换 AL2023）
- Ubuntu 20.04：5.4，基础可用但 PSI 功能有限

### 确认当前 cgroup 版本

```bash
# 方法1：检查 systemd
stat -fc %T /sys/fs/cgroup/
# tmpfs → cgroup v1（或混合模式）
# cgroup2fs → 纯 cgroup v2

# 方法2：检查挂载
mount | grep cgroup
# cgroup2 on /sys/fs/cgroup type cgroup2 → v2
# cgroup on /sys/fs/cgroup/memory type cgroup → v1

# 方法3：检查 /proc/1/cgroup
cat /proc/1/cgroup
# 0::/init.scope → 纯 cgroup v2
# 12:memory:/init.scope → v1 memory controller
```

### 容器运行时版本

```bash
# containerd
containerd --version
# 要求 >= 1.4（v2 基础支持）
# 推荐 >= 1.6（完整 MemoryQoS 支持）

# runc
runc --version
# 要求 >= 1.0.0-rc93

# 检查 containerd 当前配置
grep -E "cgroup_driver|SystemdCgroup" /etc/containerd/config.toml
```

### systemd 版本

```bash
systemctl --version
# 要求 >= 244（完整 cgroup v2 支持）
# Ubuntu 22.04 是 249，Amazon Linux 2023 是 252，均满足
```

### kubelet 版本

K8s 各版本的 cgroup v2 支持状态：
- K8s 1.22：Alpha
- K8s 1.25：Beta，**默认启用 cgroup v2**
- K8s 1.31+：GA

```bash
kubelet --version
# 推荐 >= 1.25
```

---

## 节点级迁移步骤

### Ubuntu 22.04

Ubuntu 22.04 默认已经是 cgroup v2，但需要确认 systemd 的 `unified_cgroup_hierarchy` 参数：

```bash
# 检查当前状态
cat /proc/cmdline | grep -o 'systemd.unified_cgroup_hierarchy=[^ ]*'
# 如果没有这个参数，Ubuntu 22.04 默认就是 cgroup v2

# 如果发现是 v1，修改 grub 参数
sudo sed -i 's/GRUB_CMDLINE_LINUX=""/GRUB_CMDLINE_LINUX="systemd.unified_cgroup_hierarchy=1"/' \
  /etc/default/grub

sudo update-grub
sudo reboot
```

### Amazon Linux 2023

AL2023 默认启用 cgroup v2，通常无需修改：

```bash
# 确认
stat -fc %T /sys/fs/cgroup/
# cgroup2fs → 已经是 v2

# 如果是旧 AL2023 镜像仍在 v1，修改内核参数
sudo grubby --update-kernel=ALL \
  --args="systemd.unified_cgroup_hierarchy=1"
sudo reboot
```

### CentOS Stream 9 / RHEL 9

```bash
# RHEL 9 / CentOS Stream 9 默认 v2，但确认一下
stat -fc %T /sys/fs/cgroup/

# 如需强制启用
sudo grubby --update-kernel=ALL \
  --args="systemd.unified_cgroup_hierarchy=1 cgroup_no_v1=all"

# 禁用 v1 legacy controllers（可选，更彻底）
echo "cgroup_no_v1=all" | sudo tee /etc/modprobe.d/cgroup-v1.conf

sudo reboot
```

### 验证迁移结果

```bash
# 重启后验证
stat -fc %T /sys/fs/cgroup/
# 输出应为：cgroup2fs

mount | grep cgroup
# 应只有一条 cgroup2 挂载，没有 v1 的 memory/cpu 等子挂载

# 验证 PSI 可用
cat /proc/pressure/cpu
# some avg10=... → PSI 工作正常
```

---

## containerd 配置更新

cgroup v2 必须使用 **systemd cgroup driver**，不能再用 cgroupfs driver。

```bash
# 生成默认配置（如果还没有的话）
containerd config default | sudo tee /etc/containerd/config.toml

# 关键配置：确认这两处
grep -n "SystemdCgroup\|cgroup_driver" /etc/containerd/config.toml
```

修改配置文件：

```toml
# /etc/containerd/config.toml
version = 2

[plugins."io.containerd.grpc.v1.cri"]
  # ... 其他配置 ...
  
  [plugins."io.containerd.grpc.v1.cri".containerd]
    [plugins."io.containerd.grpc.v1.cri".containerd.runtimes]
      [plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runc]
        runtime_type = "io.containerd.runc.v2"
        
        [plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runc.options]
          SystemdCgroup = true    # 关键：必须为 true
```

用 sed 快速修改：

```bash
sudo sed -i 's/SystemdCgroup = false/SystemdCgroup = true/' \
  /etc/containerd/config.toml

# 或者用 toml 工具更可靠
sudo python3 -c "
import toml, sys
with open('/etc/containerd/config.toml') as f:
    cfg = toml.load(f)
runc_opts = cfg['plugins']['io.containerd.grpc.v1.cri']['containerd']['runtimes']['runc']['options']
runc_opts['SystemdCgroup'] = True
with open('/etc/containerd/config.toml', 'w') as f:
    toml.dump(cfg, f)
print('Done')
"

# 重启 containerd
sudo systemctl restart containerd
sudo systemctl status containerd
```

### kubelet 配置

```yaml
# /var/lib/kubelet/config.yaml
apiVersion: kubelet.config.k8s.io/v1beta1
kind: KubeletConfiguration
cgroupDriver: systemd          # 必须与 containerd 一致
cgroupsPerQOS: true            # 默认 true，按 QoS class 创建 cgroup
enforceNodeAllocatable:
- pods
- system-reserved
- kube-reserved
```

```bash
# 重启 kubelet
sudo systemctl restart kubelet

# 验证 kubelet 使用了正确的 cgroup driver
journalctl -u kubelet | grep -i "cgroup driver"
# kubelet: "Using cgroupDriver" driver="systemd"
```

---

## 启用 MemoryQoS

MemoryQoS 是 Kubernetes Feature Gate，1.22 进入 Alpha，1.27 进入 Beta（默认关闭），需要手动启用：

```yaml
# /var/lib/kubelet/config.yaml
apiVersion: kubelet.config.k8s.io/v1beta1
kind: KubeletConfiguration
featureGates:
  MemoryQoS: true         # 启用 MemoryQoS
  KubeletCgroupDriverFromCRI: true  # 让 kubelet 从 CRI 获取 cgroup driver（推荐）
```

启用后，kubelet 会根据容器的 QoS 类自动设置 cgroup v2 的内存参数：

```bash
# 找到一个 Guaranteed QoS 的 Pod
kubectl get pod nginx -n production -o jsonpath='{.status.qosClass}'
# Guaranteed

# 找到对应的 cgroup 路径
CONTAINER_ID=$(kubectl get pod nginx -n production \
  -o jsonpath='{.status.containerStatuses[0].containerID}' | cut -d/ -f3)

CGROUP_PATH="/sys/fs/cgroup/kubepods/guaranteed/pod$(kubectl get pod nginx -n production -o jsonpath='{.metadata.uid}')/${CONTAINER_ID:0:12}"

# 查看内存控制参数
cat $CGROUP_PATH/memory.min    # = memory limit（保证不被回收）
cat $CGROUP_PATH/memory.high   # = memory limit（触发回收阈值）
cat $CGROUP_PATH/memory.max    # = memory limit（硬上限，超出 OOM）
```

对于 Burstable Pod：

```bash
# requests.memory = 256Mi, limits.memory = 512Mi
cat $CGROUP_PATH/memory.min   # = 256Mi（保证量）
cat $CGROUP_PATH/memory.high  # = 512Mi * 0.9 = 460Mi（触发回收）
cat $CGROUP_PATH/memory.max   # = 512Mi（OOM 上限）
```

---

## PSI 监控集成

### 在 Prometheus 中采集 PSI 指标

node_exporter >= 1.3.0 默认采集 PSI 指标：

```bash
# 确认 node_exporter 版本
node_exporter --version

# 确认 PSI 指标已被采集
curl -s http://localhost:9100/metrics | grep node_pressure
# node_pressure_cpu_waiting_seconds_total
# node_pressure_memory_waiting_seconds_total
# node_pressure_memory_stalled_seconds_total
# node_pressure_io_waiting_seconds_total
# node_pressure_io_stalled_seconds_total
```

### Prometheus 告警规则

```yaml
# prometheus-rules-psi.yaml
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: kubernetes-psi-alerts
  namespace: monitoring
spec:
  groups:
  - name: node-psi
    interval: 30s
    rules:
    # 内存压力告警：some > 10% 持续 5 分钟
    - alert: NodeMemoryPressureHigh
      expr: |
        rate(node_pressure_memory_waiting_seconds_total[5m]) * 100 > 10
      for: 5m
      labels:
        severity: warning
      annotations:
        summary: "节点内存压力过高"
        description: "节点 {{ $labels.instance }} 内存 PSI some 指标为 {{ $value | humanize }}%，系统可能存在内存竞争"

    # IO 压力告警：full > 5% 持续 3 分钟（说明系统完全被 IO 卡住）
    - alert: NodeIOPressureCritical
      expr: |
        rate(node_pressure_io_stalled_seconds_total[5m]) * 100 > 5
      for: 3m
      labels:
        severity: critical
      annotations:
        summary: "节点 IO 完全停滞"
        description: "节点 {{ $labels.instance }} IO PSI full 指标为 {{ $value | humanize }}%，所有任务都在等待 IO"

    # CPU 压力告警
    - alert: NodeCPUPressureHigh
      expr: |
        rate(node_pressure_cpu_waiting_seconds_total[5m]) * 100 > 20
      for: 5m
      labels:
        severity: warning
      annotations:
        summary: "节点 CPU 压力过高"
        description: "节点 {{ $labels.instance }} CPU PSI some 为 {{ $value | humanize }}%"
```

### Grafana Dashboard 关键面板

```yaml
# PSI 面板查询示例（PromQL）
# 内存压力趋势（some，1分钟平均）
rate(node_pressure_memory_waiting_seconds_total{instance="$node"}[1m]) * 100

# 内存压力趋势（full，1分钟平均）
rate(node_pressure_memory_stalled_seconds_total{instance="$node"}[1m]) * 100

# IO 压力（some）
rate(node_pressure_io_waiting_seconds_total{instance="$node"}[1m]) * 100
```

PSI 相比传统的 `node_memory_MemAvailable_bytes` 有本质优势：内存充足时 PSI 可以是 0，但内存触发了大量 swap 时 PSI 会飙升，而剩余内存指标可能仍然显示"正常"。

---

## 常见问题处理

### Java 应用无法正确识别容器内存

Java 8u191 之前的版本不识别 cgroup v2，会读取宿主机总内存来设置堆大小，导致 OOM Kill。

```bash
# 验证问题
kubectl exec -it java-app-pod -- java -XX:+PrintFlagsFinal -version 2>&1 | grep MaxHeapSize
# 如果 MaxHeapSize 远大于容器 limits，说明 JVM 没有识别 cgroup

# 解决方案1：升级 JDK >= 11（原生支持 cgroup v2）
# 解决方案2：JDK 8/11 手动指定堆大小
JAVA_OPTS="-Xms512m -Xmx512m"

# 解决方案3：使用 JVM 容器感知参数（JDK 10+）
JAVA_OPTS="-XX:+UseContainerSupport -XX:MaxRAMPercentage=75.0"
```

验证 JVM 正确识别了容器内存：

```bash
kubectl exec -it java-app-pod -- java \
  -XX:+PrintContainerInfo \
  -XX:+PrintFlagsFinal \
  -version 2>&1 | grep -E "MaxHeapSize|container"
# container_memory_limit_in_bytes: 536870912 (512m)
# MaxHeapSize = 402653184 (75% of 512m) → 正确
```

### metrics-server 兼容问题

旧版 metrics-server（< 0.6.0）在 cgroup v2 节点上可能无法采集到正确的内存用量：

```bash
# 检查 metrics-server 版本
kubectl get deployment metrics-server -n kube-system \
  -o jsonpath='{.spec.template.spec.containers[0].image}'

# 如果 < 0.6.0，升级
kubectl set image deployment/metrics-server \
  metrics-server=registry.k8s.io/metrics-server/metrics-server:v0.7.2 \
  -n kube-system

# 验证 metrics 正常
kubectl top nodes
kubectl top pods -A
```

### 监控指标变化：cadvisor

cAdvisor 在 cgroup v2 下，部分 v1 的指标路径变了：

```bash
# v1 中 container_memory_cache 对应 v2 中：
container_memory_cache → 从 memory.stat 的 file 字段读取

# v1 中 container_blkio_device_usage_total 在 v2 中更名
# 检查 cadvisor 是否支持 v2
kubectl exec -n kube-system $(kubectl get pod -n kube-system -l app=cadvisor -o name | head -1) -- \
  /usr/bin/cadvisor --version
# 要求 >= 0.46.0
```

如果使用 kube-prometheus-stack，建议升级到 chart >= 45.x（内置 cadvisor >= 0.46）。

### 节点上的容器无法启动（OCI runtime error）

```bash
# 报错示例
# failed to create containerd task: failed to create shim: OCI runtime create failed:
# container_linux.go:380: starting container process caused: ...
# cgroups: cgroup mountpoint does not exist: unknown

# 原因：containerd 还在用旧的 cgroupfs driver
grep SystemdCgroup /etc/containerd/config.toml
# 如果是 false 或者没有这个配置，修改为 true 并重启 containerd
```

---

## 滚动迁移策略

生产集群不能一次性全部迁移，需要混跑过渡期。

### 方案：节点池分离

```bash
# 1. 新建 cgroup v2 节点池（NodeGroup 或 Karpenter NodePool）
# 用 label 区分

# Karpenter NodePool 示例
cat <<EOF | kubectl apply -f -
apiVersion: karpenter.sh/v1
kind: NodePool
metadata:
  name: cgroup-v2-pool
spec:
  template:
    metadata:
      labels:
        cgroup-version: "v2"
    spec:
      nodeClassRef:
        apiVersion: karpenter.k8s.aws/v1
        kind: EC2NodeClass
        name: cgroup-v2-class
      requirements:
      - key: kubernetes.io/os
        operator: In
        values: ["linux"]
      - key: karpenter.sh/capacity-type
        operator: In
        values: ["on-demand"]
  limits:
    cpu: 1000
    memory: 4000Gi
EOF

# EC2NodeClass 使用 AL2023 AMI（原生 cgroup v2）
cat <<EOF | kubectl apply -f -
apiVersion: karpenter.k8s.aws/v1
kind: EC2NodeClass
metadata:
  name: cgroup-v2-class
spec:
  amiFamily: AL2023    # 原生 cgroup v2
  amiSelectorTerms:
  - alias: al2023@latest
  subnetSelectorTerms:
  - tags:
      karpenter.sh/discovery: my-cluster
  securityGroupSelectorTerms:
  - tags:
      karpenter.sh/discovery: my-cluster
EOF
```

### 方案：用 Taint 控制调度

```bash
# 给旧节点（v1）加 taint，不允许新 Pod 调度
kubectl taint nodes <v1-node-1> cgroup-version=v1:NoSchedule
kubectl taint nodes <v1-node-2> cgroup-version=v1:NoSchedule

# 新节点（v2）不加 taint，新 Pod 默认调度到 v2 节点
# 检查节点 cgroup 版本的 DaemonSet
```

用 DaemonSet 自动打 Label：

```yaml
# detect-cgroup-version-ds.yaml
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: detect-cgroup-version
  namespace: kube-system
spec:
  selector:
    matchLabels:
      name: detect-cgroup-version
  template:
    metadata:
      labels:
        name: detect-cgroup-version
    spec:
      hostPID: true
      containers:
      - name: detect
        image: alpine:3.19
        command:
        - /bin/sh
        - -c
        - |
          CGROUPFS=$(stat -fc %T /sys/fs/cgroup/)
          if [ "$CGROUPFS" = "cgroup2fs" ]; then
            VERSION="v2"
          else
            VERSION="v1"
          fi
          # 给节点打 label
          NODENAME=$(cat /etc/hostname)
          kubectl label node $NODENAME cgroup-version=$VERSION --overwrite
          sleep infinity
        volumeMounts:
        - name: cgroup
          mountPath: /sys/fs/cgroup
          readOnly: true
      volumes:
      - name: cgroup
        hostPath:
          path: /sys/fs/cgroup
      serviceAccountName: node-labeler
      tolerations:
      - operator: Exists  # 所有节点都运行
```

### 迁移进度追踪

```bash
# 查看各 cgroup 版本节点数量
kubectl get nodes -L cgroup-version

# 查看还在 v1 节点上运行的 Pod
kubectl get pods -A -o wide | \
  awk 'NR>1 {print $7}' | \
  sort -u | \
  xargs -I{} kubectl get node {} -L cgroup-version --no-headers | \
  grep "v1$"

# 统计
kubectl get nodes -l cgroup-version=v2 --no-headers | wc -l
kubectl get nodes -l cgroup-version=v1 --no-headers | wc -l
```

### 回滚方案

如果新节点有问题，修改内核参数回退：

```bash
# Ubuntu/Debian：恢复 v1
sudo sed -i 's/systemd.unified_cgroup_hierarchy=1/systemd.unified_cgroup_hierarchy=0/' \
  /etc/default/grub
sudo update-grub
sudo reboot

# 验证回退成功
stat -fc %T /sys/fs/cgroup/
# tmpfs → 回退到 v1
```

---

## 迁移后验证清单

```bash
# 1. 节点 cgroup 版本
stat -fc %T /sys/fs/cgroup/
# cgroup2fs ✓

# 2. containerd cgroup driver
grep SystemdCgroup /etc/containerd/config.toml
# SystemdCgroup = true ✓

# 3. kubelet cgroup driver
journalctl -u kubelet | grep "cgroup driver"
# Using cgroupDriver driver="systemd" ✓

# 4. Pod 正常启动
kubectl get pods -A | grep -v Running | grep -v Completed

# 5. HPA 和 VPA 正常工作
kubectl top nodes
kubectl top pods -A

# 6. PSI 指标可采集
curl -s http://NODE_IP:9100/metrics | grep node_pressure | head -5

# 7. MemoryQoS 生效（如果启用了）
kubectl exec -it test-pod -- cat /sys/fs/cgroup/memory.min
```

迁移完成后，最直接的收益是 **PSI 驱动的 HPA**（需要 KEDA 或自定义 HPA metrics）和 **MemoryQoS** 带来的更精确内存保证。这两个特性在 v1 上完全无法实现，是升级的核心动力。
