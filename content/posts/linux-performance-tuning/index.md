---
title: "Linux 性能调优实战：CPU、内存、IO 瓶颈的系统排查方法"
date: 2024-09-08T13:50:00+08:00
draft: false
tags: ["Linux", "性能调优", "运维", "系统"]
categories: ["Linux"]
description: "系统性的 Linux 性能排查方法论，覆盖 CPU、内存、IO、网络四大维度，含容器环境特殊性说明"
summary: "从工具链选择到实战排查，梳理 Linux 性能调优的完整方法论：CPU 上下文切换与软中断分析、OOM 日志解读、IO 调度器选择、TCP TIME_WAIT 处理，以及容器环境下 cgroup 限制的特殊影响。"
toc: true
math: false
diagram: false
keywords: ["Linux", "性能调优", "OOM", "iostat", "cgroup", "sysctl", "perf"]
params:
  reading_time: true
---

性能问题是运维工作中最考验系统化思维的场景。不同于故障排查的"恢复服务"目标，性能调优需要你在数十个指标里找到真正的瓶颈，而不是凭感觉乱调参数。这篇文章整理了我处理生产性能问题的排查框架和常用工具，重点放在实际能用上的内容。

## 排查工具链概览

先建立工具认知，避免"拿着锤子找钉子"：

| 工具 | 适用场景 | 特点 |
|------|---------|------|
| `top` / `htop` | 快速全局概览 | 实时，htop 交互更友好 |
| `atop` | 历史回溯 | 可保存历史数据，事后分析 |
| `vmstat` | CPU + 内存 + IO 综合 | 时序数据，适合趋势观察 |
| `iostat` | 磁盘 IO | 精确到设备级别的吞吐和延迟 |
| `sar` | 历史数据查询 | sysstat 套件，适合夜间问题回溯 |
| `perf` | CPU 热点函数 | 采样分析，找代码级瓶颈 |
| `ss` | 网络连接状态 | 替代 netstat，速度更快 |
| `pidstat` | 进程级 CPU/IO | 精确到单进程 |

**排查优先级建议：** 先用 `vmstat 1 5` 和 `iostat -x 1 5` 快速定位瓶颈类型（CPU bound / IO bound / 内存压力），再针对性深入。

```bash
# 5 秒快速概览：CPU、内存、IO 全局状态
vmstat 1 5

# 输出关键列说明
# r: 运行队列（持续 > CPU 核数说明 CPU 饱和）
# b: 阻塞在 IO 的进程数
# si/so: swap 换入/换出（非零说明内存不足）
# wa: iowait 百分比
# cs: 上下文切换次数/秒
```

---

## CPU 性能分析

### 上下文切换（Context Switch）

上下文切换本身不是问题，高频切换才是。每次切换需要保存/恢复 CPU 寄存器，频繁切换会消耗大量 CPU 时间。

```bash
# 系统级上下文切换
vmstat 1 10 | awk '{print $12, $13}'  # cs 列（上下文切换）和 in 列（中断）

# 进程级上下文切换（找到具体的"肇事者"）
pidstat -w 1 5

# 输出示例
# PID  cswch/s  nvcswch/s  Command
# 1234  1200.3   850.1      java
# cswch: 自愿切换（等待 IO/锁），nvcswch: 非自愿切换（时间片用完）
```

**判断标准：** 自愿切换高通常是 IO 或锁竞争，非自愿切换高说明 CPU 资源不足（进程太多抢占）。

### 软中断（softirq）

软中断处理占用 CPU 但不在进程维度体现，`top` 里看到 `si%` 高需要关注：

```bash
# 查看各类软中断的处理次数
watch -n 1 cat /proc/softirqs

# 找到处理软中断的 CPU 分布（网络软中断是否集中在单核）
cat /proc/interrupts | grep -E "CPU|eth|ens"
```

网络收包软中断（NET_RX）集中在单核是常见问题，解决方案是开启 RPS（Receive Packet Steering）：

```bash
# 将网卡中断分散到所有 CPU 核
echo f > /sys/class/net/eth0/queues/rx-0/rps_cpus  # f = 使用所有核
```

### iowait 分析

`wa%`（iowait）高不一定是磁盘慢，也可能是正常的 IO 密集型负载。区分方法：

```bash
# 看 iowait 的同时看 await（IO 请求平均等待时间）
iostat -x 1 5

# 关键指标
# await: 平均 IO 延迟（SSD 正常 < 1ms，HDD 正常 < 20ms）
# %util: 设备使用率（接近 100% 说明磁盘饱和）
# r/s, w/s: 读写 IOPS
# rMB/s, wMB/s: 读写吞吐量
```

### perf 火焰图（CPU 热点）

当 CPU 使用率高但找不到具体原因时，perf 采样能定位到具体函数：

```bash
# 采样 30 秒，对所有进程
perf record -ag -F 99 sleep 30

# 生成报告
perf report --stdio | head -50

# 生成火焰图（需要 FlameGraph 工具）
perf script | stackcollapse-perf.pl | flamegraph.pl > flamegraph.svg
```

---

## 内存问题排查

### OOM 日志分析

OOM（Out of Memory）Killer 是内核在内存耗尽时的最后手段。发生 OOM 时，内核日志会留下详细信息：

```bash
# 查看 OOM 日志
dmesg | grep -E "OOM|out of memory|Killed process" | tail -20

# 或者从 journald 查
journalctl -k | grep -i "oom\|killed process" | tail -20

# 典型 OOM 日志
# Out of memory: Kill process 12345 (java) score 876 or sacrifice child
# Killed process 12345 (java) total-vm:8388608kB, anon-rss:6291456kB
```

**OOM Score** 决定哪个进程被杀。Score 越高越容易被杀，由内存使用量和 `oom_score_adj` 共同决定：

```bash
# 查看进程的 OOM score
cat /proc/$(pgrep java)/oom_score

# 降低重要进程被 OOM 杀死的概率（-1000 = 永不被杀）
echo -500 > /proc/$(pgrep mysqld)/oom_score_adj

# 在 systemd service 中配置
# OOMScoreAdjust=-500
```

### 内存泄漏排查

```bash
# 观察进程内存随时间的变化
pidstat -r -p 12345 60  # 每分钟采样一次

# 查看进程内存详细分解
cat /proc/12345/status | grep -E "VmRSS|VmSwap|VmPeak"

# VmRSS: 实际物理内存占用（关键指标）
# VmSwap: 被 swap 到磁盘的内存
# VmPeak: 历史最高内存使用

# 查看内存映射（找到哪个 so 库占用内存大）
pmap -x 12345 | sort -k3 -n | tail -20
```

对于 Go/Java 服务，内存泄漏通常需要配合语言层面的工具（pprof、jmap）才能定位具体对象。

### Swap 踩坑

Swap 在生产环境的使用存在争议：

**不能完全禁用 Swap 的情况：** 某些内核版本在 swappiness=0 时，即使物理内存充足，也可能触发 OOM。建议设置 swappiness=1（几乎不 swap，但保留 swap 作为最后兜底）。

```bash
# 临时设置
sysctl vm.swappiness=1

# 永久生效
echo "vm.swappiness=1" >> /etc/sysctl.conf
sysctl -p

# 查看当前 swap 使用
free -h
swapon --show
```

**踩坑：** K8s 节点默认要求禁用 swap（kubelet 启动会报错）。但如果宿主机 swappiness 未设为 0，即使 `swapoff -a` 关闭了 swap 分区，内核仍可能尝试使用。节点扩容时记得检查：

```bash
# K8s 节点上确认 swap 状态
free -h | grep Swap
cat /proc/swaps
```

---

## IO 性能分析

### 磁盘读写延迟排查

```bash
# 实时 IO 监控（-x 显示扩展指标）
iostat -x 1 10

# 找到 IO 最高的进程
iotop -o -b -n 5  # -o 只显示有 IO 的进程，-b 非交互模式

# 查看单个进程的 IO 统计
cat /proc/12345/io
# rchar: 读字节数（含缓存）
# read_bytes: 实际磁盘读
# write_bytes: 实际磁盘写
```

### IO 调度器选择

不同场景适合不同 IO 调度器：

```bash
# 查看当前调度器
cat /sys/block/sda/queue/scheduler
# 输出示例：[mq-deadline] kyber bfq none

# 修改调度器
echo mq-deadline > /sys/block/sda/queue/scheduler
```

| 调度器 | 适用场景 |
|--------|---------|
| `mq-deadline` | 通用场景，兼顾延迟和吞吐（推荐默认） |
| `none` (noop) | NVMe SSD、虚拟机磁盘（硬件自带队列） |
| `bfq` | 桌面场景，保证交互响应性 |
| `kyber` | 低延迟 SSD |

**生产经验：** 对于 AWS EBS（SSD）和阿里云 ESSD，使用 `none` 或 `mq-deadline` 效果最好。不要在 SSD 上用 `cfq`（旧版），会增加不必要的合并延迟。

---

## 网络性能分析

### ss 替代 netstat

`ss` 比 `netstat` 快得多（直接读 `/proc/net`），是现代 Linux 的标配：

```bash
# 查看所有 TCP 连接状态汇总
ss -s

# 查看 ESTABLISHED 连接（按进程）
ss -tnp state established

# 查看特定端口的连接
ss -tnp 'sport = :8080'

# 查看连接数最多的远端 IP
ss -tn state established | awk '{print $5}' | cut -d: -f1 | sort | uniq -c | sort -rn | head
```

### TIME_WAIT 问题

TIME_WAIT 是 TCP 正常关闭流程的一部分，不是 Bug。但如果 TIME_WAIT 连接数过多（几万甚至几十万），会耗尽端口资源：

```bash
# 查看 TIME_WAIT 连接数
ss -s | grep TIME-WAIT

# 或者
cat /proc/net/sockstat | grep TCP
```

**解决方案（优先级排序）：**

1. **优先：启用连接复用（HTTP Keep-Alive）**，减少频繁建立/关闭连接
2. **次选：调整内核参数**

```bash
# /etc/sysctl.conf

# 开启 TCP TIME_WAIT 快速回收（只在 NAT 环境下关闭）
net.ipv4.tcp_tw_reuse = 1

# TIME_WAIT 超时时间（默认 60s，不建议改小，会影响网络可靠性）
# 不要设置 tcp_tw_recycle，已在 Linux 4.12 移除

# 增大本地端口范围
net.ipv4.ip_local_port_range = 1024 65535

# 连接跟踪表大小（如果使用 iptables）
net.netfilter.nf_conntrack_max = 1048576
```

---

## 容器环境下的性能特殊性

### cgroup 资源限制的影响

容器内的进程看到的是宿主机的 CPU 和内存信息（通过 `/proc`），但实际可用资源受 cgroup 限制：

```bash
# 容器内查看 cgroup CPU 限制
cat /sys/fs/cgroup/cpu/cpu.cfs_quota_us   # CPU 配额（微秒）
cat /sys/fs/cgroup/cpu/cpu.cfs_period_us  # 统计周期（通常 100000μs = 100ms）

# 实际 CPU 核数 = quota / period
# quota=200000, period=100000 → 2 核

# 容器内查看内存限制
cat /sys/fs/cgroup/memory/memory.limit_in_bytes
cat /sys/fs/cgroup/memory/memory.usage_in_bytes
```

**常见踩坑：** Java 应用在容器内默认根据 `/proc/cpuinfo` 设置线程池大小，在 96 核宿主机上运行 2 核限制的容器，Java 会创建 96 个线程，导致大量上下文切换。

解决方案：
- Java 8u191+ 和 Java 10+ 已支持容器感知（`-XX:+UseContainerSupport`，默认开启）
- 旧版 Java 需要显式指定：`-XX:ActiveProcessorCount=2`

### namespace 对性能工具的影响

在容器内使用 `top`、`ps` 等工具，只能看到同一 PID namespace 的进程，看不到宿主机其他进程。这是正常的隔离机制，但在排查宿主机级别的竞争时会有盲点。

```bash
# 在宿主机上用 nsenter 进入容器 namespace 排查
# 先找到容器 PID
docker inspect --format '{{.State.Pid}}' container_name

# 进入容器的网络 namespace 执行命令
nsenter -t <PID> -n -- ss -s

# 在宿主机看所有进程（包含容器内进程）的资源使用
top -H  # 显示线程级别
```

---

## 常用 sysctl 优化参数

以下是经过验证的生产环境参数，根据实际情况调整：

```bash
# /etc/sysctl.d/99-performance.conf

# 网络
net.core.somaxconn = 32768          # listen 队列长度
net.ipv4.tcp_max_syn_backlog = 8192
net.core.netdev_max_backlog = 16384
net.ipv4.tcp_tw_reuse = 1
net.ipv4.ip_local_port_range = 1024 65535
net.ipv4.tcp_keepalive_time = 600   # TCP keepalive 间隔
net.ipv4.tcp_fin_timeout = 30       # FIN_WAIT2 超时

# 内存
vm.swappiness = 1
vm.dirty_ratio = 10                 # 脏页占比超过此值强制刷盘
vm.dirty_background_ratio = 5       # 后台刷盘阈值
vm.overcommit_memory = 1            # 允许内存超售（Redis 要求）

# 文件描述符
fs.file-max = 1048576
fs.inotify.max_user_watches = 524288  # 防止 inotify watch 耗尽

# 应用生效
sysctl -p /etc/sysctl.d/99-performance.conf
```

**注意：** `vm.overcommit_memory = 1` 是 Redis 官方要求的配置，允许内核在内存超售时不拒绝 malloc，但会增加 OOM 风险。在内存本已紧张的机器上要谨慎。

---

## 性能分析方法论总结

1. **先量化，再判断**：收集足够数据再下结论，避免"感觉慢"的主观判断
2. **自顶向下**：从 CPU → 内存 → IO → 网络，逐层排查
3. **区分均值和百分位**：平均延迟正常但 P99 高，说明有异常请求；只看均值会漏掉长尾问题
4. **对比基线**：保存正常状态下的性能数据（atop 历史、Prometheus 指标），才能判断"异常"
5. **一次改一个参数**：调优时单变量原则，避免多参数同时修改导致效果难以评估
