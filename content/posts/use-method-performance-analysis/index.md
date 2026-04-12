---
title: "USE Method：系统性能分析方法论"
date: 2026-04-12T11:00:00+08:00
draft: false
tags: ["performance", "use-method", "sre", "linux", "prometheus", "kubernetes", "observability"]
categories: ["性能优化"]
description: "深度解析 Brendan Gregg 提出的 USE Method（Utilization + Saturation + Errors），从 CPU、内存、磁盘、网络四个维度建立系统化的性能分析框架，并将其映射到 K8s 环境和 Prometheus PromQL，附带一次真实 CPU 饱和问题的定位全过程。"
summary: "随机尝试是性能排查的大敌。USE Method 用一个三维框架（使用率/饱和度/错误）把所有系统资源纳入统一分析体系，本文从原理到实战全面解析这套方法论，并提供 K8s 环境下的 PromQL 映射和工具链速查表。"
toc: true
math: false
diagram: false
keywords: ["use method", "brendan gregg", "性能分析", "cpu饱和", "prometheus", "kubernetes", "iostat", "vmstat", "性能排查"]
params:
  reading_time: true
---

## 为什么需要方法论

性能问题排查最常见的反模式是**"直觉驱动"**：看到 CPU 高就加机器，看到请求慢就怀疑数据库，翻了半天日志没找到根因，最终靠重启解决问题——直到下次再出现。

这种方式的问题不是工程师不够聪明，而是**缺乏系统性的搜索空间**。没有方法论，排查就是在黑暗中摸索，每次走的路径不同，经验难以积累。

Brendan Gregg 在其著作 *Systems Performance: Enterprise and the Cloud* 中提出了 **USE Method**，它给出了一个穷举所有资源瓶颈的框架：

> For every resource, check utilization, saturation, and errors.

- **Utilization（使用率）**：资源在时间维度上被占用的比例，100% 意味着资源已满载。
- **Saturation（饱和度）**：超过资源处理能力的额外工作量，通常体现为队列长度或等待时间。
- **Errors（错误）**：资源操作的错误事件，即使使用率不高，错误本身也可能造成性能下降。

USE Method 的执行逻辑是：

```
列举系统中所有资源（CPU、内存、磁盘、网络、...）
  ↓
对每个资源，分别检查 U / S / E
  ↓
找到第一个异常指标
  ↓
深入分析该资源
```

这个方法的价值在于**确保不遗漏**，而不是保证最快找到。它和 TSA（The TSA Method，自顶向下逐层钻取）配合使用效果最好，但 USE 更适合于"不知道从哪里开始"的场景。

---

## CPU 分析

### Utilization（使用率）

**工具**：`top`、`htop`、`mpstat`

```bash
# 每隔 1 秒采样，显示每个 CPU 核心
mpstat -P ALL 1 5
```

输出示例：
```
CPU    %usr   %sys   %iowait  %steal  %idle
all    78.5    8.2      0.3      0.1   12.9
0      95.1    4.2      0.0      0.0    0.7   ← 单核瓶颈
1      62.3   12.1      0.0      0.0   25.6
```

**关键指标**：
- `%usr`：用户态 CPU，高值说明应用本身计算密集
- `%sys`：内核态 CPU，高值可能是系统调用频繁（I/O、网络）
- `%steal`：被宿主机偷走的 CPU 时间，虚拟机/容器环境中出现说明资源争用
- `%idle`：空闲，100 - idle ≈ 整体使用率

**注意**：单核使用率 100% 而整体使用率只有 25%（4 核机器），说明应用存在串行瓶颈，加机器没用，需要优化并发度。

### Saturation（饱和度）

CPU 饱和度的核心指标是**运行队列长度**：等待 CPU 的线程数超过 CPU 核数时，产生饱和。

```bash
# vmstat：r 列是运行队列长度
vmstat 1 10
procs -----------memory---------- ---swap-- -----io---- --system-- ------cpu-----
 r  b   swpd   free   buff  cache   si   so    bi    bo   in   cs us sy id wa st
12  0      0 1024000  12000 512000    0    0     0     0 8000 12000 78  8  0  0 14
```

`r = 12`，而机器只有 4 核，说明有 8 个线程在排队，CPU 严重饱和。

**Load Average** 是另一个常用指标，但要注意它**包含了 I/O 等待**（D 状态进程），不能单纯作为 CPU 饱和度指标：

```bash
# uptime 输出的 1/5/15 分钟 load average
load average: 8.42, 7.91, 6.53
```

规则：`load average / CPU 核数 > 1` 时开始关注，`> 2` 时需要立即排查。

### Errors（错误）

CPU 错误主要来自硬件层面：

```bash
# 检查机器检查异常（Machine Check Exception）
dmesg | grep -i "mce\|machine check"
# 或者查看 MCE 记录
mcelog --client  # 需要安装 mcelog
```

在容器环境中，CPU throttling 也是一种"软错误"：

```bash
# 检查容器 CPU throttle 统计
cat /sys/fs/cgroup/cpu/cpuacct.stat
cat /sys/fs/cgroup/cpu/cpu.stat
# throttled_time 单位是纳秒
```

`throttled_time` 持续增长说明容器 CPU limit 设置过低，应用被强制限速。

---

## 内存分析

### Utilization（使用率）

```bash
free -h
              total        used        free      shared  buff/cache   available
Mem:           31G         22G        1.2G        512M        7.8G        8.2G
Swap:          4.0G        2.1G        1.9G
```

**关键**：`available` 而非 `free` 才是真实可用内存——Linux 会用空闲内存做 buffer/cache，`free` 接近 0 是正常的，`available` 接近 0 才需要警惕。

```bash
# 实时内存使用（每秒）
vmstat 1 | awk '{print $3, $4, $5, $6}'  # swpd free buff cache
```

### Saturation（饱和度）

内存饱和的直接表现是**swap 活动**和**页面错误**：

```bash
# vmstat 中的 si/so：swap in / swap out（KB/s）
vmstat 1
 r  b   swpd   free   buff  cache   si   so
 2  4   2097152 204800  0  512000  512 1024  ← so=1024 KB/s，正在换出内存

# 主缺页（需要磁盘读取，代价高）vs 次缺页（匿名内存分配，代价低）
/usr/bin/time -v your_program 2>&1 | grep "Major page faults"
```

`Major page faults`（主缺页）频繁说明物理内存不足，进程的页面被换出到磁盘后再次访问，每次约 10ms 延迟。

```bash
# 系统级别的页面换入换出
sar -B 1 5
pgpgin/s  pgpgout/s   fault/s  majflt/s
   0.00    1024.00   5000.00     12.00   ← majflt/s=12，每秒 12 次主缺页
```

### Errors（错误）

```bash
# EDAC（Error Detection and Correction）内存硬件错误
dmesg | grep -i "edac\|ecc\|memory error"
# 或
edac-util -s 10  # 需要安装 edac-utils
```

在 K8s 环境中，OOMKill 是内存错误的主要表现：

```bash
# 查看被 OOM Kill 的容器
kubectl get events --all-namespaces | grep OOMKilling
# 或从 Pod 事件查看
kubectl describe pod <pod-name> | grep -A5 "OOMKilled"
```

---

## 磁盘 I/O 分析

### Utilization（使用率）

`iostat` 是磁盘 I/O 分析的主力工具：

```bash
iostat -xz 1 5
Device            r/s     w/s    rkB/s    wkB/s   rrqm/s   wrqm/s  %rrqm  %wrqm r_await w_await aqu-sz  rareq-sz  wareq-sz  svctm  %util
nvme0n1          50.0   150.0   400.0   4800.0     0.0      8.0   0.0   5.1    0.5     2.1    0.33    8.0     32.0    4.9   98.0
```

- `%util`：磁盘使用率，**接近 100% 说明磁盘已饱和**（但 SSD/NVMe 可以并行处理多个请求，100% util 不一定是瓶颈）
- `r_await` / `w_await`：读/写请求的平均等待时间（ms）

### Saturation（饱和度）

```bash
# aqu-sz（average queue size）：平均队列长度 > 1 说明有等待
iostat -xz 1 | awk '/nvme|sd/{print $1, $NF, $(NF-1)}'  # device, %util, aqu-sz
```

更直观的方式：

```bash
# await 时间对比 svctm（服务时间）
# await >> svctm 说明有大量排队等待
# await = 2.1ms, svctm = 0.5ms → 队列等待 1.6ms，饱和迹象
```

对于 Linux 内核 4.18+ 的 blk-mq 架构，`svctm` 已不再准确，应更多关注 `r_await`/`w_await`。

### Errors（错误）

```bash
# 内核 I/O 错误
dmesg | grep -E "I/O error|hard error|reset|timeout" | tail -20
# 或通过 smartctl 查看磁盘 SMART 数据
smartctl -a /dev/nvme0n1 | grep -E "Reallocated|Pending|Uncorrectable"
```

---

## 网络分析

### Utilization（使用率）

```bash
# iftop 实时带宽（交互式）
iftop -i eth0 -B  # 显示字节而非位

# 非交互式：nethogs 按进程
nethogs eth0

# 计算使用率需要知道链路带宽
ethtool eth0 | grep Speed  # Speed: 10000Mb/s（10Gbps）
# 当前吞吐量 / 链路带宽 = 使用率
```

```bash
# 用 sar 采样网络吞吐
sar -n DEV 1 5
IFACE   rxpck/s   txpck/s    rxkB/s    txkB/s   rxcmp/s   txcmp/s  rxmcst/s
eth0    15000.0   14000.0   18000.0   22000.0      0.0       0.0      0.0
# rxkB/s + txkB/s ≈ 40 MB/s ≈ 320 Mbps，在 10Gbps 链路上使用率 3.2%
```

### Saturation（饱和度）

网络饱和的信号是**丢包**和**缓冲区溢出**：

```bash
# 查看网卡统计（包含 drops/overruns）
ethtool -S eth0 | grep -E "drop|miss|overflow|error"
# 或
ip -s link show eth0
RX: bytes  packets  errors  dropped missed  mcast
    12345678 100000    0       42      0       0    ← dropped=42，有丢包

# TCP 重传率
ss -s
netstat -s | grep -E "retransmit|failed"
```

```bash
# 查看 socket 接收/发送缓冲区满（backlog 溢出）
ss -lnt  # LISTEN 状态，Send-Q 是 backlog 大小，Recv-Q 是积压连接数
State    Recv-Q   Send-Q   Local Address:Port
LISTEN    128      128      0.0.0.0:8080       ← Recv-Q=128=backlog，说明 accept 跟不上
```

### Errors（错误）

```bash
# 全量网卡错误统计
ethtool -S eth0 | grep -E "error|fail|bad"

# 检查 conntrack 表满（会导致新连接被丢弃）
sysctl net.netfilter.nf_conntrack_count
sysctl net.netfilter.nf_conntrack_max
# count 接近 max 时，新连接会被拒绝，症状是随机连接超时

# TCP 连接错误
netstat -s | grep -E "connection.*fail|reset"
```

---

## K8s 环境下的 USE 映射

在 Kubernetes 中，USE Method 需要在两个层面分别分析：**节点（Node）层**和 **Pod/容器层**。

### 节点层 vs Pod 层对比

| 资源 | 节点层指标 | Pod/容器层指标 |
|------|-----------|--------------|
| CPU 使用率 | `node_cpu_seconds_total` | `container_cpu_usage_seconds_total` |
| CPU 饱和度 | `node_load1` / CPU 数 | `container_cpu_cfs_throttled_seconds_total` |
| CPU 错误 | `node_hwmon_*`（MCE） | OOMKill 事件 |
| 内存使用率 | `node_memory_MemTotal` | `container_memory_working_set_bytes` |
| 内存饱和度 | `node_vmstat_pgmajfault` | 容器 OOMKill |
| 内存错误 | `node_edac_*` | — |
| 磁盘使用率 | `node_disk_io_time_seconds_total` | `container_fs_reads_bytes_total` |
| 磁盘饱和度 | `node_disk_io_time_weighted_seconds_total` | — |
| 网络使用率 | `node_network_receive_bytes_total` | `container_network_receive_bytes_total` |
| 网络饱和度 | `node_network_receive_drop_total` | — |

### 容器 CPU Throttling 是最常被忽视的问题

```bash
# 找到 CPU throttle 率超过 20% 的容器
kubectl get pods -A -o json | \
  jq '.items[] | select(.status.containerStatuses != null) | 
  .metadata.namespace + "/" + .metadata.name'

# 从 cgroup 直接读取（在节点上）
find /sys/fs/cgroup/cpu -name "cpu.stat" -exec \
  awk '/throttled_time/{if($2>0) print FILENAME, $2}' {} \;
```

---

## Prometheus PromQL：USE 三要素映射

### CPU

```promql
# Utilization：节点 CPU 使用率（5分钟均值）
1 - avg(rate(node_cpu_seconds_total{mode="idle"}[5m])) by (instance)

# Saturation：运行队列 / CPU 核数（> 1 告警）
node_load1 / count(node_cpu_seconds_total{mode="idle"}) by (instance)

# Errors：容器 CPU Throttle 率
rate(container_cpu_cfs_throttled_seconds_total[5m]) /
rate(container_cpu_cfs_periods_total[5m])
```

### 内存

```promql
# Utilization：节点内存使用率
1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)

# Saturation：主缺页率（pages/s），> 100 需关注
rate(node_vmstat_pgmajfault[5m])

# Errors：OOMKill 事件（过去 1 小时）
increase(kube_pod_container_status_last_terminated_reason{reason="OOMKilled"}[1h])
```

### 磁盘

```promql
# Utilization：磁盘 I/O 使用率
rate(node_disk_io_time_seconds_total[5m])

# Saturation：加权 I/O 时间（队列深度代理指标）
rate(node_disk_io_time_weighted_seconds_total[5m])

# Errors：磁盘读写错误
rate(node_disk_read_errors_total[5m]) + rate(node_disk_write_errors_total[5m])
```

### 网络

```promql
# Utilization：网络带宽使用率（需要已知链路速度，此处用 10Gbps 举例）
rate(node_network_receive_bytes_total{device!="lo"}[5m]) * 8 / 10e9

# Saturation：网络接收丢包率
rate(node_network_receive_drop_total{device!="lo"}[5m]) /
rate(node_network_receive_packets_total{device!="lo"}[5m])

# Errors：网络接收错误率
rate(node_network_receive_errs_total{device!="lo"}[5m])
```

---

## 工具链速查表

| 资源 | 使用率 | 饱和度 | 错误 |
|------|-------|-------|------|
| **CPU** | `mpstat -P ALL 1` | `vmstat 1`（r列）| `dmesg \| grep mce` |
| **内存** | `free -h` | `vmstat 1`（si/so）| `dmesg \| grep edac` |
| **磁盘** | `iostat -xz 1`（%util）| `iostat -xz 1`（aqu-sz, await）| `dmesg \| grep "I/O error"` |
| **网络** | `sar -n DEV 1` | `ethtool -S`（drops）| `ethtool -S`（errors）|
| **文件描述符** | `lsof \| wc -l` | `/proc/sys/fs/file-nr` | — |
| **连接跟踪** | `conntrack -C` | 对比 `nf_conntrack_max` | `dmesg \| grep conntrack` |

**K8s 专用工具**：

```bash
# 节点资源分配概览
kubectl describe node <node> | grep -A10 "Allocated resources"

# Top 资源消耗 Pod
kubectl top pods -A --sort-by=cpu | head -20
kubectl top pods -A --sort-by=memory | head -20

# 容器资源请求 vs 实际使用
kubectl get pods -A -o custom-columns='NS:.metadata.namespace,NAME:.metadata.name,CPU_REQ:.spec.containers[*].resources.requests.cpu,CPU_LIM:.spec.containers[*].resources.limits.cpu'
```

---

## 实战：用 USE Method 15 分钟定位 CPU 饱和问题

以下是一次真实生产事故的排查过程（已脱敏），某 Go 服务的 P99 延迟从 50ms 飙升到 2s，同时有少量 5xx 错误。

### 0:00 — 收到告警，建立排查框架

告警触发：`http_request_duration_p99 > 1s`，`http_5xx_rate > 0.1%`。

不要急着看代码或数据库，先用 USE Method 扫描所有资源：

```bash
# 登录对应节点
kubectl get pod <pod-name> -o wide  # 找到节点 IP
ssh node-ip
```

### 2:00 — CPU 检查

```bash
mpstat -P ALL 1 3
CPU    %usr   %sys   %iowait  %idle
all    94.2    3.1      0.1    2.6   ← 整体 94%，CPU 使用率极高
0      99.8    0.1      0.0    0.1   ← CPU 0 已满载
1      88.6    6.2      0.2    5.0
2      98.1    1.4      0.0    0.5
3      91.2    4.3      0.1    4.4
```

CPU 使用率高，继续检查饱和度：

```bash
vmstat 1 5
 r  b   swpd   free
 9  0      0  2048000  ← r=9，4 核机器，运行队列 9，饱和度 9/4 = 2.25
```

**结论：CPU 严重饱和，是 P99 延迟飙升的直接原因。**

### 5:00 — 定位是哪个进程

```bash
top -b -n 1 -H  # 线程级别（-H）
PID     USER   PR  NI    VIRT    RES    SHR S  %CPU  %MEM     TIME+  COMMAND
12345   app     20   0  1024m   256m    12m R  390.0   0.8   5:23.12 go-service
```

390% CPU（4 核机器，接近 100% × 4）。确认是目标服务。

### 7:00 — 分析是计算密集还是系统调用

```bash
mpstat -P ALL 1 3
CPU    %usr   %sys
all    92.1    2.1   ← usr 远高于 sys，说明是用户态计算密集，非 I/O

# 使用 perf 采样调用栈
perf top -p 12345 -g --call-graph dwarf
```

`perf top` 输出（节选）：

```
Overhead  Symbol
  45.2%   runtime.mallocgc        ← Go 内存分配
  18.3%   runtime.gcBgMarkWorker  ← GC 标记
  12.1%   encoding/json.Marshal   ← JSON 序列化
   8.4%   compress/gzip.Write     ← gzip 压缩
```

**根因浮现**：GC 压力 + JSON 序列化占用了大量 CPU。

### 10:00 — 验证 GC 假设

```bash
# 查看 Go runtime 指标（如果暴露了 /debug/vars 或 pprof）
curl http://localhost:8080/debug/pprof/heap > heap.prof
go tool pprof heap.prof
(pprof) top10
```

或直接通过 Prometheus（如果集成了 `prometheus/client_golang`）：

```promql
# Go GC 暂停时间
rate(go_gc_duration_seconds_sum[5m]) / rate(go_gc_duration_seconds_count[5m])
# GC 运行频率
rate(go_gc_pause_total_ns[5m]) / 1e9
```

发现 GC 暂停时间从正常的 0.5ms 上升到 15ms，GC 频率从 2/min 上升到 40/min。

### 12:00 — 找到触发点

查看监控，CPU 飙升发生在某次部署之后 10 分钟。对比代码变更：

```diff
- resp, _ := json.Marshal(items)
+ items = append(items, newLargeObject)  // 新增了一个 100KB 的大对象
+ resp, _ := json.Marshal(items)
+ gzipWriter.Write(resp)  // 新增了 gzip 压缩
```

新版本在热路径上增加了 100KB 对象的 JSON 序列化 + gzip 压缩，触发大量内存分配，导致 GC 频率急剧上升，CPU 被 GC 占用，产生 CPU 饱和。

### 15:00 — 确认并制定修复方案

**立即缓解**：回滚此次部署（30 秒内恢复）。

**根本修复**：
1. 使用对象池（`sync.Pool`）复用大对象，减少 GC 压力
2. gzip 压缩移到 response 中间件，按 Content-Type 条件触发
3. 将该接口的 JSON 响应改为 protobuf，减少序列化开销

整个排查过程 15 分钟，遵循了 USE Method 的逻辑：
1. **USE 扫描**（CPU 使用率 94%，饱和度 2.25）→ 确认 CPU 是瓶颈
2. **区分 usr/sys**（usr 主导）→ 确认是用户态计算，非 I/O
3. **perf 采样**（GC + JSON + gzip）→ 定位具体热路径
4. **对比变更**（部署时间点吻合）→ 找到根因

---

## USE Method 的局限与补充

USE Method 的设计目标是**资源瓶颈**，有两类问题它不擅长处理：

1. **软件错误**：死锁、内存泄漏的早期阶段（资源使用率还不高）、配置错误。这些需要用 **RED Method**（Rate、Errors、Duration）从服务层视角分析。

2. **容量规划**：USE 是当前状态的快照，不能直接回答"什么时候会打满"。需要结合趋势分析（`predict_linear` in PromQL）。

最佳实践是 USE + RED 联合使用：
- **RED（服务视角）**：先判断用户侧影响（请求率、错误率、延迟）
- **USE（资源视角）**：定位底层资源瓶颈

两者配合，从症状到根因，形成完整的排查闭环。

---

## 总结

USE Method 的核心价值是**提供了一个不会遗漏的搜索空间**。把每个系统资源的三个维度（使用率/饱和度/错误）作为强制检查项，避免了因为先入为主的假设而跳过某个资源的检查。

在 K8s 环境下，USE Method 需要在节点层和 Pod 层双重视角分析，特别要关注容器 CPU throttling（使用率看着正常但实际被限流）和 OOMKill（内存错误的主要形式）这两个 K8s 特有的"错误模式"。

Prometheus + node_exporter + cadvisor 的指标体系已经覆盖了 USE Method 所需的绝大多数指标，构建一套对齐 USE 三维度的告警规则，是 SRE 建立性能感知能力的最短路径。
