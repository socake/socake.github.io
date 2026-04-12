---
title: "Linux 内核网络参数深度调优：高并发场景实战"
date: 2026-03-20T10:00:00+08:00
draft: false
tags: ["Linux", "内核调优", "网络性能", "Kubernetes", "高并发", "TCP"]
categories: ["系统运维"]
description: "系统讲解 Linux 内核网络参数在高并发场景下的调优方法，涵盖 TCP 连接管理、内存缓冲区、conntrack 连接跟踪、网卡队列中断亲和性、K8s 节点专属调优，以及压测前后的效果验证方法。"
summary: "在高并发场景下，Linux 默认内核参数往往成为系统瓶颈。本文从原理出发，系统讲解 TCP backlog、TIME_WAIT、keepalive、内存缓冲区、conntrack、网卡队列（RSS/RPS/RFS）的调优方法，并提供 K8s 节点专属的 sysctl DaemonSet 方案和完整的压测验证流程。"
toc: true
math: false
diagram: false
keywords: ["sysctl", "TCP调优", "conntrack", "高并发", "网络性能", "RSS", "RPS", "Kubernetes网络"]
params:
  reading_time: true
---

## 默认参数为什么扛不住高并发

Linux 内核的默认网络参数设计于 1990 年代，面向的是数百并发连接的服务器。当业务规模增长到每秒数万 QPS、维持数十万长连接时，这些参数会在你意想不到的地方引发问题：

- **SYN 丢包**：`tcp_max_syn_backlog` 默认 128，高并发突发时半连接队列溢出，客户端看到连接超时
- **端口耗尽**：`ip_local_port_range` 默认 32768-60999，约 28000 个端口，频繁建立短连接时 SNAT 用完所有端口
- **TIME_WAIT 积压**：大量 TIME_WAIT 状态连接占用内存，每个消耗约 260 字节，100 万个就是 260MB
- **conntrack 表满**：K8s 环境下 `nf_conntrack_max` 默认值偏低，表满后所有新连接被 DROP，引发神秘的间歇性超时
- **接收缓冲区不足**：`tcp_rmem` 默认最大 4MB，大带宽长延迟链路（高 BDP）下吞吐量严重受限

本文以生产环境实战为基础，逐个击破这些瓶颈。

---

## 调优前的基线采集

调优之前必须先建立基线，否则无法量化效果。

```bash
# 保存当前所有网络相关 sysctl
sysctl -a 2>/dev/null | grep -E 'net\.(core|ipv4|ipv6|netfilter)' > /tmp/sysctl_baseline.txt

# TCP 连接状态统计
ss -s

# 查看 SYN 队列溢出（排查 SYN flood 或 backlog 不足）
# 方法1：通过 /proc
watch -n 1 'cat /proc/net/stat/tcp_stats | awk "NR==1{print} NR==2{print}"'

# 方法2：netstat 统计
netstat -s | grep -i "syn\|listen\|overflowed\|time wait\|failed"

# 典型输出示例：
# 12847 times the listen queue of a socket overflowed  <-- backlog 不足
# 12847 SYNs to LISTEN sockets dropped
# 3920567 TCP connections transitions to TIME_WAIT    <-- TIME_WAIT 积压

# conntrack 使用情况
cat /proc/sys/net/netfilter/nf_conntrack_count
cat /proc/sys/net/netfilter/nf_conntrack_max
# 如果 count 接近 max，立即处理

# 网卡队列深度
ethtool -g eth0
```

---

## TCP 连接管理参数

### Backlog 队列：解决 SYN 丢包

TCP 三次握手中有两个队列：
- **SYN queue（半连接队列）**：收到 SYN，发出 SYN-ACK，等待 ACK 的连接
- **Accept queue（全连接队列）**：三次握手完成，等待应用 accept() 的连接

```bash
# 查看当前值
sysctl net.core.somaxconn          # Accept queue 上限
sysctl net.ipv4.tcp_max_syn_backlog # SYN queue 上限

# 生产推荐值
cat >> /etc/sysctl.d/99-network-tuning.conf << 'EOF'
# TCP backlog 队列
# 高并发 Web 服务，Accept queue 设为 65535
net.core.somaxconn = 65535
# SYN queue，应 >= somaxconn
net.ipv4.tcp_max_syn_backlog = 65535
EOF
```

**注意**：`somaxconn` 只是内核上限，应用层的 listen backlog 也需要对应调整：

```python
# Python gunicorn/uvicorn
uvicorn app:app --backlog 65535

# Nginx
server {
    listen 80 backlog=65535;
}
```

验证 backlog 是否生效：

```bash
# 查看特定端口的 Accept queue 使用情况
# Recv-Q 列显示等待 accept() 的连接数
# Send-Q 列显示 backlog 上限
ss -lntp | grep :8080
# State   Recv-Q  Send-Q  Local Address:Port
# LISTEN  0       65535   0.0.0.0:8080
```

### TIME_WAIT 优化

TIME_WAIT 是 TCP 正常的状态，2MSL（约 60 秒）等待确保对端收到最后的 ACK。但在高并发短连接场景下，大量 TIME_WAIT 可能耗尽端口。

```bash
# 查看 TIME_WAIT 连接数
ss -s | grep time-wait
# 或
ss -ant state time-wait | wc -l

# 扩大本地端口范围（默认 32768-60999，约 28000 个端口）
# 扩展到约 55000 个端口
net.ipv4.ip_local_port_range = 10000 65535

# 允许 TIME_WAIT 状态的 socket 被复用于新的 TCP 连接（只对客户端有效）
# 前提：对端支持 TCP timestamps
net.ipv4.tcp_tw_reuse = 1

# 启用 TCP timestamps（tcp_tw_reuse 的依赖）
net.ipv4.tcp_timestamps = 1

# 调整 fin_timeout（FIN-WAIT-2 超时，默认 60s）
net.ipv4.tcp_fin_timeout = 30
```

**关于 `tcp_tw_recycle`**：在 Linux 4.12 已被彻底移除。在 NAT 环境（几乎所有 K8s 场景）下它会导致丢包，不要使用。

**关于减少 TIME_WAIT 的正确姿势**：
1. 开启长连接（HTTP Keep-Alive、连接池），从根本上减少连接建立/关闭频率
2. `tcp_tw_reuse = 1` 对客户端有效（主动发起连接方）
3. 服务端 `SO_REUSEADDR` 允许复用处于 TIME_WAIT 的本地地址

### Keepalive 保活参数

长连接场景下，keepalive 负责探测死连接，防止资源泄漏。

```bash
# 连接空闲多久后开始发送探测包（默认 7200 秒 = 2 小时）
# 生产建议：300s（5 分钟）
net.ipv4.tcp_keepalive_time = 300

# 探测包发送间隔（默认 75 秒）
net.ipv4.tcp_keepalive_intvl = 30

# 探测包发送次数（达到次数后判定连接断开，默认 9 次）
net.ipv4.tcp_keepalive_probes = 3
```

调优后，死连接最多在 300 + 30 × 3 = 390 秒内被检测到（默认是 7200 + 75 × 9 = 7875 秒）。

**注意**：应用层也需要开启 keepalive（设置 `SO_KEEPALIVE` socket 选项），内核参数才会生效。很多语言/框架的 HTTP 客户端默认不开启 keepalive。

---

## 内存与缓冲区

### TCP 接收/发送缓冲区

TCP 缓冲区大小直接影响吞吐量，尤其在高带宽长延迟链路（高 BDP：Bandwidth-Delay Product）下。

理论最优缓冲区大小 = 带宽 × RTT（BDP）

```
1Gbps 带宽 × 100ms RTT = 125MB/s × 0.1s = 12.5MB
```

```bash
# net.ipv4.tcp_rmem = min default max
# min: 单个连接最小保证内存
# default: 初始缓冲区大小（影响 tcp_adv_win_scale）
# max: 单个连接最大缓冲区（受 net.core.rmem_max 限制）

# 数据中心内网（低延迟，高带宽）
net.ipv4.tcp_rmem = 4096 87380 16777216     # 4KB / 85KB / 16MB
net.ipv4.tcp_wmem = 4096 65536 16777216     # 4KB / 64KB / 16MB

# core 层的全局上限（必须 >= tcp_rmem max）
net.core.rmem_max = 16777216
net.core.wmem_max = 16777216

# 默认 socket 缓冲区大小（影响 UDP 和其他协议）
net.core.rmem_default = 262144
net.core.wmem_default = 262144

# 自动调整缓冲区大小（默认已开启，不要关闭）
net.ipv4.tcp_moderate_rcvbuf = 1
```

### 网卡接收队列

```bash
# 网卡驱动接收队列长度（每个 CPU 核心一个队列的上限）
# 默认 1000，高包速率时容易溢出
net.core.netdev_max_backlog = 65536

# 网络设备发送队列长度
net.core.dev_weight = 64
```

验证是否有包被丢弃：

```bash
# 查看网卡统计（RX dropped / TX dropped）
ip -s link show eth0
# 或
ethtool -S eth0 | grep -i drop

# 软中断统计（Dropped 列）
cat /proc/net/softnet_stat | awk '{printf "CPU%d: total=%d dropped=%d\n", NR-1, strtonum("0x"$1), strtonum("0x"$2)}'
```

---

## Conntrack 连接跟踪（K8s 环境必看）

Netfilter conntrack 追踪所有经过内核的网络连接，是 iptables NAT、K8s Service 实现的基础。conntrack 表满是 K8s 集群最常见却最难诊断的网络问题之一。

### 问题现象

```
# dmesg 中出现以下告警：
nf_conntrack: nf_conntrack: table full, dropping packet.
nf_conntrack: expectation table full
```

表现为：业务高峰期随机出现连接超时，重试后成功，监控没有明显异常，问题间歇性发生，难以复现。

### 排查 conntrack 表满

```bash
# 当前 conntrack 表使用量
cat /proc/sys/net/netfilter/nf_conntrack_count

# conntrack 表上限
cat /proc/sys/net/netfilter/nf_conntrack_max

# 实时监控（如果接近上限，立即处理）
watch -n 1 'echo "$(cat /proc/sys/net/netfilter/nf_conntrack_count) / $(cat /proc/sys/net/netfilter/nf_conntrack_max)"'

# 查看 conntrack 表中的条目（谨慎在生产执行，可能卡住）
conntrack -L | head -50
conntrack -L | wc -l

# 按协议统计
conntrack -L 2>/dev/null | awk '{print $1}' | sort | uniq -c
```

### 调优参数

```bash
# conntrack 表大小（默认通常是内存大小/16384 的某个函数，约 65536）
# K8s 节点推荐至少 1000000
net.netfilter.nf_conntrack_max = 1000000

# hash 桶数量，影响查找效率
# 推荐设为 nf_conntrack_max / 4（每个桶平均 4 个条目）
net.netfilter.nf_conntrack_buckets = 262144

# conntrack 条目超时（减少无效条目占用表空间）
# TCP established 连接超时（默认 432000 = 5 天，太长）
net.netfilter.nf_conntrack_tcp_timeout_established = 86400  # 1 天

# TCP TIME_WAIT 超时（默认 120s）
net.netfilter.nf_conntrack_tcp_timeout_time_wait = 120

# TCP FIN_WAIT 超时（默认 120s）
net.netfilter.nf_conntrack_tcp_timeout_fin_wait = 30

# UDP 超时
net.netfilter.nf_conntrack_udp_timeout = 30
net.netfilter.nf_conntrack_udp_timeout_stream = 60
```

`nf_conntrack_buckets` 在 `/proc/sys/net/netfilter/nf_conntrack_buckets` 是只读的，必须通过模块参数设置：

```bash
# 方法一：模块参数（需要重新加载模块或重启）
echo "options nf_conntrack hashsize=262144" > /etc/modprobe.d/nf_conntrack.conf

# 方法二：直接写入（部分内核版本支持）
echo 262144 > /sys/module/nf_conntrack/parameters/hashsize
```

### K8s 场景下的 conntrack 问题

在 K8s 中，kube-proxy 使用 iptables NAT 规则，每个 Service 的请求都会经过 DNAT（目标地址转换）。在高流量场景下：

1. 节点上可能有数十万条 conntrack 条目（每个 Pod 连接都会产生记录）
2. NodePort Service 的流量经过两次 NAT，产生两倍的 conntrack 压力
3. DNS 查询（UDP，每次都是短连接）会大量占用 conntrack 表

```bash
# 查看 K8s 节点上 conntrack 的分布
conntrack -L 2>/dev/null | awk '{
  for(i=1;i<=NF;i++) {
    if($i ~ /^dport=/) {
      split($i, a, "=")
      ports[a[2]]++
    }
  }
}
END {
  for(p in ports) printf "%s\t%s\n", ports[p], p
}' | sort -rn | head -20
```

减轻 conntrack 压力的方案：
- 对集群内流量使用 eBPF/cilium 绕过 iptables（不产生 conntrack）
- 对已知服务的 UDP DNS 设置 `--conntrack-udp-timeout` 更短
- 关闭不需要 conntrack 的规则（`-j NOTRACK`）

---

## 网卡队列与中断亲和性

### 问题背景

单核 CPU 处理网络中断在高包速率下会成为瓶颈（100Gbps 网卡可以产生每秒数千万个中断）。多队列网卡（Multiqueue NIC）配合 RSS/RPS/RFS 可以将网络处理负载均摊到多个 CPU 核心。

### RSS（Receive Side Scaling）

RSS 是硬件级的负载均衡，网卡将收到的包按（src/dst IP + port）的哈希分配到多个硬件队列，每个队列绑定到不同的 CPU。

```bash
# 查看网卡队列数
ethtool -l eth0
# Combined: 当前队列数（RX+TX）
# Maximum: 最大支持队列数

# 设置队列数（建议 = CPU 核心数）
ethtool -L eth0 combined $(nproc)

# 查看中断亲和性（每个队列绑定的 CPU）
cat /proc/interrupts | grep eth0
# 输出示例：
# 32: 145678  0  0  0  0  0  0  0  PCI-MSI eth0-rx-0  <- 绑定 CPU0
# 33: 0  156789  0  0  0  0  0  0  PCI-MSI eth0-rx-1  <- 绑定 CPU1

# 手动设置中断亲和性（CPU mask，十六进制）
# 将 eth0-rx-0 的中断绑定到 CPU0（mask = 0x01）
echo 1 > /proc/irq/32/smp_affinity
# 绑定到 CPU1（mask = 0x02）
echo 2 > /proc/irq/33/smp_affinity
```

自动化脚本（绑定网卡队列中断到各 CPU）：

```bash
#!/bin/bash
# set_irq_affinity.sh - 自动设置网卡中断亲和性
NIC=${1:-eth0}
CPU_COUNT=$(nproc)
IRQ_LIST=$(grep "$NIC" /proc/interrupts | awk -F: '{print $1}' | tr -d ' ')

i=0
for irq in $IRQ_LIST; do
    cpu_mask=$(printf "%x" $((1 << (i % CPU_COUNT))))
    echo "Setting IRQ $irq -> CPU $((i % CPU_COUNT)) (mask 0x$cpu_mask)"
    echo "$cpu_mask" > /proc/irq/$irq/smp_affinity
    ((i++))
done
```

### RPS（Receive Packet Steering）

对于单队列网卡（不支持 RSS 的虚拟网卡，如 virtio），RPS 在软件层面模拟 RSS 的效果，将包分发到多个 CPU 的 backlog 队列。

```bash
# 开启 RPS（将所有 CPU 都用于处理，mask = 全 1）
# CPU_COUNT=8 时，mask = ff；16 时 = ffff；32 时 = ffffffff
CPU_MASK=$(printf '%x' $(( (1 << $(nproc)) - 1 )))

for rx_queue in /sys/class/net/eth0/queues/rx-*/rps_cpus; do
    echo "$CPU_MASK" > $rx_queue
    echo "Set $rx_queue = $CPU_MASK"
done

# 验证
cat /sys/class/net/eth0/queues/rx-0/rps_cpus
```

### RFS（Receive Flow Steering）

RFS 是 RPS 的增强，它将连接的后续包路由到与应用程序运行在同一 CPU 上，减少缓存 miss。

```bash
# 开启 RFS
# rps_flow_cnt: 每个队列跟踪的流数量
echo 32768 > /sys/class/net/eth0/queues/rx-0/rps_flow_cnt

# 全局流表大小（= 所有队列 rps_flow_cnt 之和）
echo 32768 > /proc/sys/net/core/rps_sock_flow_entries
```

### 验证调优效果

```bash
# 查看软中断分布（ideally 均匀分布在各 CPU）
watch -n 1 'cat /proc/softirqs | grep -E "CPU|NET_RX|NET_TX"'

# 查看每个 CPU 的包处理统计
cat /proc/net/softnet_stat
# 格式：total processed  dropped  time_squeezed  0 0 0 0 0  cpu_collision  received_rps  flow_limit_count
# 如果 time_squeezed 持续增长，需要增加 net.core.dev_weight 或优化 NAPI poll
```

---

## K8s 节点专属调优

K8s 节点的内核参数调优面临一个特殊挑战：Pod 默认继承节点的 network namespace，但安全策略不允许 Pod 直接修改节点内核参数。正确的方式是通过 DaemonSet 在节点上运行特权容器。

### 方案一：Init Container 方式（简单场景）

适用于自管理 K8s，节点 OS 可自行配置。

直接修改节点 `/etc/sysctl.d/` 文件，重启或执行 `sysctl -p`。

### 方案二：DaemonSet 特权容器

适用于托管 K8s（EKS/GKE/AKS）或需要通过 GitOps 管理节点配置的场景。

```yaml
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: node-sysctl-tuning
  namespace: kube-system
  labels:
    app: node-sysctl-tuning
spec:
  selector:
    matchLabels:
      app: node-sysctl-tuning
  updateStrategy:
    type: RollingUpdate
    rollingUpdate:
      maxUnavailable: 1
  template:
    metadata:
      labels:
        app: node-sysctl-tuning
    spec:
      hostPID: true
      hostNetwork: true
      tolerations:
        - effect: NoSchedule
          operator: Exists
        - effect: NoExecute
          operator: Exists
      initContainers:
        - name: sysctl-tuner
          image: busybox:1.36
          securityContext:
            privileged: true
          command:
            - /bin/sh
            - -c
            - |
              # TCP 连接管理
              sysctl -w net.core.somaxconn=65535
              sysctl -w net.ipv4.tcp_max_syn_backlog=65535
              sysctl -w net.ipv4.tcp_tw_reuse=1
              sysctl -w net.ipv4.tcp_timestamps=1
              sysctl -w net.ipv4.tcp_fin_timeout=30
              sysctl -w net.ipv4.ip_local_port_range="10000 65535"

              # keepalive
              sysctl -w net.ipv4.tcp_keepalive_time=300
              sysctl -w net.ipv4.tcp_keepalive_intvl=30
              sysctl -w net.ipv4.tcp_keepalive_probes=3

              # 内存缓冲区
              sysctl -w net.core.rmem_max=16777216
              sysctl -w net.core.wmem_max=16777216
              sysctl -w net.ipv4.tcp_rmem="4096 87380 16777216"
              sysctl -w net.ipv4.tcp_wmem="4096 65536 16777216"
              sysctl -w net.core.netdev_max_backlog=65536

              # conntrack
              sysctl -w net.netfilter.nf_conntrack_max=1000000
              sysctl -w net.netfilter.nf_conntrack_tcp_timeout_established=86400
              sysctl -w net.netfilter.nf_conntrack_tcp_timeout_time_wait=120
              sysctl -w net.netfilter.nf_conntrack_tcp_timeout_fin_wait=30
              sysctl -w net.netfilter.nf_conntrack_udp_timeout=30
              sysctl -w net.netfilter.nf_conntrack_udp_timeout_stream=60

              echo "sysctl tuning completed"
      containers:
        - name: pause
          image: gcr.io/google_containers/pause:3.9
          resources:
            limits:
              cpu: "10m"
              memory: "10Mi"
```

### 方案三：通过 Kubelet 配置安全 sysctl

K8s 1.21+ 支持在 Pod spec 中设置部分"安全的" sysctl（namespaced sysctl），无需特权容器：

```yaml
apiVersion: v1
kind: Pod
spec:
  securityContext:
    sysctls:
      # 这些是 namespaced sysctl，只影响当前 Pod 的网络 namespace
      - name: net.ipv4.tcp_keepalive_time
        value: "300"
      - name: net.ipv4.tcp_keepalive_intvl
        value: "30"
      - name: net.ipv4.tcp_keepalive_probes
        value: "3"
```

但需要注意：大多数性能相关的 sysctl（如 `net.core.somaxconn`）是节点级别的，不支持 namespaced 方式。

**通过 Kubelet allowedUnsafeSysctls 解锁**：

```yaml
# /etc/kubernetes/kubelet-config.yaml
apiVersion: kubelet.config.k8s.io/v1beta1
kind: KubeletConfiguration
allowedUnsafeSysctls:
  - "net.core.somaxconn"
  - "net.ipv4.tcp_tw_reuse"
```

---

## 完整 sysctl 配置文件

以下是经过生产验证的完整配置，适用于高并发 Web 服务节点：

```bash
# /etc/sysctl.d/99-production-network-tuning.conf
# 高并发网络调优 - 生产环境

# =====================
# TCP 连接队列
# =====================
net.core.somaxconn = 65535
net.ipv4.tcp_max_syn_backlog = 65535

# =====================
# TIME_WAIT 优化
# =====================
net.ipv4.tcp_tw_reuse = 1
net.ipv4.tcp_timestamps = 1
net.ipv4.tcp_fin_timeout = 30
net.ipv4.ip_local_port_range = 10000 65535
# 允许的最大 TIME_WAIT 数量（超出后老的 socket 被强制关闭）
net.ipv4.tcp_max_tw_buckets = 262144

# =====================
# TCP Keepalive
# =====================
net.ipv4.tcp_keepalive_time = 300
net.ipv4.tcp_keepalive_intvl = 30
net.ipv4.tcp_keepalive_probes = 3

# =====================
# 内存缓冲区
# =====================
net.core.rmem_max = 16777216
net.core.wmem_max = 16777216
net.core.rmem_default = 262144
net.core.wmem_default = 262144
net.ipv4.tcp_rmem = 4096 87380 16777216
net.ipv4.tcp_wmem = 4096 65536 16777216

# 网络设备队列
net.core.netdev_max_backlog = 65536

# =====================
# Conntrack（K8s 节点必须调）
# =====================
net.netfilter.nf_conntrack_max = 1000000
net.netfilter.nf_conntrack_tcp_timeout_established = 86400
net.netfilter.nf_conntrack_tcp_timeout_time_wait = 120
net.netfilter.nf_conntrack_tcp_timeout_fin_wait = 30
net.netfilter.nf_conntrack_tcp_timeout_close_wait = 30
net.netfilter.nf_conntrack_udp_timeout = 30
net.netfilter.nf_conntrack_udp_timeout_stream = 60
net.netfilter.nf_conntrack_generic_timeout = 120

# =====================
# 其他 TCP 优化
# =====================
# 启用 TCP 快速开放（减少 RTT）
net.ipv4.tcp_fastopen = 3

# TCP 慢启动重启（长连接空闲后重置拥塞窗口 - 对长连接应用建议关闭）
net.ipv4.tcp_slow_start_after_idle = 0

# 初始拥塞窗口提升（Google 推荐 initcwnd=10 已是内核默认）
# 通过 ip route 设置：ip route change default ... initcwnd 10

# SYN Cookie（防 SYN flood，正常业务也应开启）
net.ipv4.tcp_syncookies = 1

# ARP 表大小（集群内大量 Pod 时可能需要）
net.ipv4.neigh.default.gc_thresh1 = 4096
net.ipv4.neigh.default.gc_thresh2 = 8192
net.ipv4.neigh.default.gc_thresh3 = 16384

# 文件描述符上限（配合 ulimit）
fs.file-max = 2097152
```

应用配置：

```bash
sysctl -p /etc/sysctl.d/99-production-network-tuning.conf
# 验证
sysctl net.core.somaxconn
```

---

## 调优效果验证

### 验证工具汇总

```bash
# 1. 连接状态概览
ss -s
# Tcp:   estab 45123, closed 234, orphaned 12, timewait 8934
# Transport Total  IP  IPv6
# RAW        0      0    0
# UDP        8      7    1
# TCP      46234  46230    4

# 2. 详细连接统计（替代 netstat，速度更快）
ss -ant | awk 'NR>1 {counts[$1]++} END {for(state in counts) print state, counts[state]}' | sort -k2 -rn

# 3. conntrack 实时监控
watch -n 2 '
echo "=== Conntrack Usage ==="
echo "$(cat /proc/sys/net/netfilter/nf_conntrack_count) / $(cat /proc/sys/net/netfilter/nf_conntrack_max)"
echo ""
echo "=== TCP Stats ==="
netstat -s | grep -E "failed|overflow|resets|retransmit|SYN"
'

# 4. 网卡队列丢包统计
ethtool -S eth0 | grep -iE "drop|error|miss|overflow"

# 5. 软中断负载分布
mpstat -I SCPU 2 5 | head -40
# 理想状态：各 CPU 的 NET_RX/NET_TX 中断均匀分布

# 6. TCP 重传率（重传率 > 1% 说明有问题）
ss -ti | grep retrans
# 或通过 /proc/net/snmp
awk '/^Tcp:/{if(NR==8){print "RetransRate:", $13/$14*100"%"}}' /proc/net/snmp
```

### 压测验证方案

使用 `wrk` 进行 HTTP 压测前后对比：

```bash
# 安装 wrk
apt-get install -y wrk

# 压测命令：100 并发，持续 60 秒，模拟真实请求
wrk -t4 -c1000 -d60s --latency http://your-service:8080/api/health

# 调优前典型输出：
# Running 60s test @ http://your-service:8080/api/health
# Thread Stats   Avg      Stdev     Max   +/- Stdev
#   Latency    45.23ms   89.45ms   2.01s    92.34%
#   Req/Sec     2.34k     456.78    3.12k    68.00%
# Latency Distribution
#    50%   12.34ms
#    75%   23.45ms
#    90%   89.12ms
#    99%  512.34ms
# Requests/sec:   9234.56
# Transfer/sec:      3.45MB
# Socket errors: connect 0, read 23, write 0, timeout 45

# 调优后典型输出：
# Thread Stats   Avg      Stdev     Max   +/- Stdev
#   Latency    12.34ms   18.23ms  234.56ms    94.12%
#   Req/Sec     4.89k     234.56    5.67k    72.00%
# Latency Distribution
#    50%    8.23ms
#    75%   14.56ms
#    90%   28.90ms
#    99%   89.23ms
# Requests/sec:  19456.78
# Transfer/sec:      7.28MB
# Socket errors: connect 0, read 0, write 0, timeout 0
```

### 生产验证数据

以某电商平台大促压测为例，节点配置：32 核 64GB，单节点峰值 QPS 约 8000：

| 指标 | 调优前 | 调优后 | 改善 |
|------|--------|--------|------|
| P99 延迟 | 1234ms | 89ms | -93% |
| 最大 QPS（不报错） | 5200 | 19500 | +275% |
| SYN 队列溢出次数/分钟 | 234 | 0 | 完全消除 |
| conntrack 表使用率（峰值） | 98% | 35% | 安全边际 |
| TIME_WAIT 连接数（稳定） | 180000 | 42000 | -77% |
| socket 读超时错误 | 45/分钟 | 0 | 完全消除 |

主要改善来源：
1. `somaxconn` 从 128 → 65535，消除了所有 SYN 溢出（贡献最大，P99 从 1.2s 降到 200ms）
2. `nf_conntrack_max` 从 65536 → 1000000，conntrack 不再成为瓶颈
3. `ip_local_port_range` 扩展 + `tcp_tw_reuse`，消除了端口耗尽导致的 connect 失败

---

## 持久化与自动化

### systemd 服务确保参数持久

```bash
# 验证 sysctl.d 文件在重启后生效
systemctl cat systemd-sysctl.service

# 手动触发加载（不重启）
systemctl restart systemd-sysctl
```

### 监控告警配置

在 Prometheus + Alertmanager 中监控关键指标：

```yaml
# prometheus rules for network tuning monitoring
groups:
  - name: network_tuning
    rules:
      - alert: ConntrackTableNearFull
        expr: |
          node_nf_conntrack_entries / node_nf_conntrack_entries_limit > 0.8
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "conntrack 表使用率超过 80%"
          description: "节点 {{ $labels.instance }} conntrack 表使用率 {{ $value | humanizePercentage }}"

      - alert: ConntrackTableFull
        expr: |
          node_nf_conntrack_entries / node_nf_conntrack_entries_limit > 0.95
        for: 1m
        labels:
          severity: critical
        annotations:
          summary: "conntrack 表即将耗尽，可能开始丢包"

      - alert: HighTimeWaitConnections
        expr: |
          node_netstat_Tcp_TimeWait > 200000
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "TIME_WAIT 连接数超过 20 万"
```

---

## 总结

Linux 网络参数调优遵循以下原则：

1. **先观测再调整**：用 `ss -s`、`netstat -s`、`conntrack -L` 确认瓶颈在哪，不要盲目调参
2. **每次只改一组参数**：便于归因，避免调参结果互相干扰
3. **有些参数是万能钥匙**：`net.core.somaxconn`、`nf_conntrack_max` 解决了 90% 的高并发网络问题
4. **K8s 节点额外关注 conntrack**：这是最常见的隐性瓶颈，也是最容易被忽视的
5. **配合应用层调优**：内核参数是地基，应用层的连接池、keepalive、超时设置同样重要

参数调优没有银弹，需要结合具体业务的流量模式、连接特征和硬件配置来取舍。本文的数值是经过多个生产环境验证的参考值，实际落地时请结合压测数据进行微调。
