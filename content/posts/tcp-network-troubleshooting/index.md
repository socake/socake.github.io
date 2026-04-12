---
title: "TCP/IP 网络排障：抓包与连接问题诊断"
date: 2026-04-12T19:00:00+08:00
draft: false
tags: ["tcp", "tcpdump", "network", "troubleshooting", "linux", "kubernetes"]
categories: ["Linux"]
description: "从 tcpdump 抓包、三次握手分析、TIME_WAIT 处理到 K8s 跨节点网络排障的完整 TCP/IP 诊断指南。"
summary: "网络问题排查的核心是「眼见为实」，没有抓包的排障都是猜测。本文系统梳理了 tcpdump 的实战用法、TCP 连接状态机分析、conntrack 追踪，以及 Kubernetes 中 NodePort/LoadBalancer 的典型网络故障定位方法。"
toc: true
math: false
diagram: false
series: ["SRE 实战手册"]
keywords: ["tcpdump", "tcp", "网络排障", "TIME_WAIT", "conntrack", "kubernetes networking"]
params:
  reading_time: true
---

## 从一个真实故障说起

某天凌晨收到告警：一批 API 请求返回 `connection refused`，但 Pod 全部 Running。进一步看发现，报错只发生在流量突增的前几秒，之后恢复正常。

排查过程：
1. 看日志 → 应用层没有错误，说明请求没有到达应用
2. 看 Pod 网络 → `ss -s` 发现 TIME_WAIT 连接数高达 3 万
3. 抓包 → 确认是 SYN 包被 RST，原因是本地端口耗尽

根因：服务作为客户端向上游发出大量短连接，TIME_WAIT 状态连接没有及时回收，可用本地端口耗尽。

这个案例涉及了本文的大部分知识点——抓包验证、连接状态分析、内核参数调优。下面系统展开。

---

## tcpdump 实战

### 基础语法

```bash
tcpdump [选项] [表达式]

# 常用选项
-i eth0        # 指定网卡（-i any 监听所有）
-n             # 不解析 IP/端口到域名/服务名（推荐，避免 DNS 查询干扰）
-nn            # 同时不解析协议名
-v / -vv       # 增加详细程度
-w dump.pcap   # 写入文件（用 Wireshark 分析）
-r dump.pcap   # 读取文件
-c 100         # 只抓100个包后退出
-s 0           # 抓取完整包（默认截断为96字节）
-A             # 以 ASCII 显示包内容（看 HTTP 头有用）
-X             # 以 hex + ASCII 显示
```

### 按主机/端口过滤

```bash
# 抓所有与 192.168.1.100 通信的包
tcpdump -i eth0 -nn host 192.168.1.100

# 抓目标端口 8080 的 TCP 包
tcpdump -i eth0 -nn tcp port 8080

# 抓来自特定源端口的包
tcpdump -i eth0 -nn src port 443

# 组合条件（and/or/not）
tcpdump -i eth0 -nn 'host 192.168.1.100 and tcp port 8080'
tcpdump -i eth0 -nn 'tcp port 80 or tcp port 443'
tcpdump -i eth0 -nn 'not port 22'  # 排除 SSH

# 抓 SYN 包（连接建立请求）
tcpdump -i eth0 -nn 'tcp[tcpflags] & tcp-syn != 0'

# 抓 RST 包（连接被强制重置）
tcpdump -i eth0 -nn 'tcp[tcpflags] & tcp-rst != 0'

# 抓 ICMP
tcpdump -i eth0 -nn icmp
```

### 按协议过滤

```bash
# UDP DNS 查询
tcpdump -i eth0 -nn 'udp port 53'

# HTTP 请求（明文）
tcpdump -i eth0 -nn -A 'tcp port 80 and (tcp[((tcp[12:1] & 0xf0) >> 2):4] = 0x47455420)'
# 0x47455420 = "GET "

# 抓 ARP（排查二层问题）
tcpdump -i eth0 arp
```

### 实用场景命令

```bash
# 监控某个 Pod 与数据库之间的连接（在 Pod 所在节点执行）
tcpdump -i any -nn -w /tmp/db.pcap \
  'host 10.0.0.50 and port 3306' &

# 10秒后停止
sleep 10 && kill %1

# 统计连接建立速率（SYN 包频率）
tcpdump -i eth0 -nn 'tcp[tcpflags] == tcp-syn' -c 1000 2>&1 | \
  grep "packets captured"

# 查看 HTTP 请求 URI（明文场景）
tcpdump -i eth0 -nn -A port 8080 | grep -E "^(GET|POST|PUT|DELETE|HEAD)"
```

---

## 三次握手与四次挥手分析

### 正常三次握手

```
Client                    Server
  │                         │
  │──── SYN (seq=100) ─────>│    [SYN_SENT]
  │                         │    [SYN_RCVD]
  │<─── SYN+ACK (seq=200, ack=101) ─│
  │                         │
  │──── ACK (ack=201) ──────>│    [ESTABLISHED]
  │                         │    [ESTABLISHED]
```

tcpdump 输出：

```
10:00:00.001  192.168.1.2.45678 > 192.168.1.3.8080: Flags [S],  seq 100, win 65535
10:00:00.002  192.168.1.3.8080 > 192.168.1.2.45678: Flags [S.], seq 200, ack 101, win 65535
10:00:00.003  192.168.1.2.45678 > 192.168.1.3.8080: Flags [.],  ack 201
```

标志位含义：
- `[S]` = SYN
- `[S.]` = SYN+ACK（`.` 表示 ACK）
- `[.]` = 纯 ACK
- `[P.]` = PSH+ACK（携带数据）
- `[F.]` = FIN+ACK
- `[R]` = RST

### 异常情况：连接被拒绝（RST）

```
Client                    Server
  │──── SYN ──────────────>│
  │<─── RST ───────────────│    # 端口未监听 或 防火墙拒绝
```

tcpdump 看到的特征：

```
10:00:00.001  src.12345 > dst.8080: Flags [S]
10:00:00.001  dst.8080 > src.12345: Flags [R.]   # 立即 RST
```

`RST` 立即返回 = 端口根本没有监听。如果是 SYN 包超时（重传），则看不到 RST，只会看到 SYN 包每隔 1s/2s/4s 重传（指数退避）。

### 正常四次挥手

```
Client                    Server
  │──── FIN ───────────────>│    [FIN_WAIT_1]
  │<─── ACK ────────────────│    [CLOSE_WAIT]
  │                         │
  │<─── FIN ────────────────│    [LAST_ACK]
  │──── ACK ───────────────>│
  │                         │    [TIME_WAIT]
```

客户端发完 ACK 后进入 `TIME_WAIT` 状态，等待 2 × MSL（最大报文段生存时间，Linux 默认 60 秒）后才真正关闭连接。

---

## TIME_WAIT 积压处理

### 观察 TIME_WAIT

```bash
# ss 查看连接状态统计
ss -s
# Total: 8234
# TCP:   6123 (estab 1200, closed 4800, orphaned 3, timewait 4750)

# 详细查看 TIME_WAIT 连接
ss -nn state time-wait | head -20

# 按状态统计
ss -nn | awk '{print $1}' | sort | uniq -c | sort -rn
```

### 内核参数调优

```bash
# 查看当前参数
sysctl net.ipv4.tcp_tw_reuse
sysctl net.ipv4.tcp_fin_timeout
sysctl net.ipv4.ip_local_port_range

# 调整（临时生效）
sysctl -w net.ipv4.tcp_tw_reuse=1          # 允许 TIME_WAIT socket 复用（仅客户端有效）
sysctl -w net.ipv4.tcp_fin_timeout=30      # 缩短 FIN_WAIT_2 超时（默认60s）
sysctl -w net.ipv4.ip_local_port_range="1024 65535"  # 扩大可用本地端口范围

# 持久化写入
cat >> /etc/sysctl.conf << 'EOF'
net.ipv4.tcp_tw_reuse = 1
net.ipv4.tcp_fin_timeout = 30
net.ipv4.ip_local_port_range = 1024 65535
net.core.somaxconn = 65535
net.ipv4.tcp_max_syn_backlog = 65535
EOF
sysctl -p
```

**注意**：`tcp_tw_recycle` 在 Linux 4.12+ 已被移除，不要设置。

---

## ss/netstat 排障

`ss` 是 `netstat` 的现代替代，速度更快。

```bash
# 查看所有 TCP 连接
ss -tn

# 监听端口
ss -tlnp

# 查看连接到特定端口的所有连接
ss -tn dst :8080

# 查看特定连接的详细信息（含 socket 统计）
ss -ti dst 10.0.0.1

# 输出示例中的关键字段：
# retrans 重传次数（高说明网络有丢包）
# rto     重传超时（毫秒）
# rtt     往返时延
# rcv_space 接收缓冲区大小

# 查找 CLOSE_WAIT 过多（应用没有正确关闭连接）
ss -tn state close-wait
```

`CLOSE_WAIT` 过多通常是**应用 Bug**：服务端收到了 FIN，但没有调用 `close()` 关闭连接。在 Go 里常见于 `resp.Body` 没有正确 `defer resp.Body.Close()`。

---

## conntrack 连接追踪

conntrack 是 Linux NAT 和防火墙的基础，K8s 的 iptables/IPVS 规则依赖它。

```bash
# 查看 conntrack 表
conntrack -L 2>/dev/null | head -20

# 查看特定协议/状态
conntrack -L -p tcp --state ESTABLISHED | wc -l

# 查看 conntrack 表使用量
cat /proc/sys/net/netfilter/nf_conntrack_count    # 当前条目数
cat /proc/sys/net/netfilter/nf_conntrack_max      # 最大容量

# 手动删除特定连接（谨慎！）
conntrack -D -s 192.168.1.100 -d 10.0.0.50 -p tcp --dport 8080

# 监控 conntrack 事件
conntrack -E -p tcp
```

**conntrack 表溢出**是 K8s 环境的常见故障：

```bash
# 内核日志中会看到
dmesg | grep "nf_conntrack: table full"

# 调整最大值
sysctl -w net.netfilter.nf_conntrack_max=524288
# 同时调整 hash 表大小（约为 max 的 1/4）
sysctl -w net.netfilter.nf_conntrack_buckets=131072
```

---

## K8s 网络排障

### 跨节点 Pod 通信

```
Pod A (Node1)                    Pod B (Node2)
   │                                  │
   │ 192.168.1.10 → 192.168.2.20     │
   ▼                                  ▼
Node1 eth0 (10.0.0.1)            Node2 eth0 (10.0.0.2)
      │                                  ▲
      └──── 封装（VXLAN/IPIP/BGP） ──────┘
```

排障步骤：

```bash
# 1. 确认 Pod IP 和节点 IP
kubectl get pod pod-a -o wide
kubectl get pod pod-b -o wide

# 2. 在 Pod A 内 ping Pod B
kubectl exec pod-a -- ping -c3 192.168.2.20

# 3. 如果 ping 不通，在 Pod A 所在节点抓包
# 看包是否出了 Node1
tcpdump -i eth0 -nn host 192.168.2.20

# 4. 在 Pod B 所在节点抓包
# 看包是否到达了 Node2
tcpdump -i eth0 -nn host 192.168.1.10

# 5. 如果节点间不通，检查 CNI 状态
kubectl get pods -n kube-system -l k8s-app=flannel  # 或 calico/cilium
```

### NodePort 排障

```
外部客户端 → NodePort(30080) → iptables DNAT → Pod(8080)
```

```bash
# 确认 NodePort Service 配置
kubectl get svc my-service -o yaml

# 在节点上查看 iptables 规则
iptables -t nat -L KUBE-SERVICES -n | grep my-service
iptables -t nat -L KUBE-NODEPORTS -n

# 抓包：观察 NodePort 流量
tcpdump -i eth0 -nn port 30080

# 确认流量是否到达 Pod
tcpdump -i any -nn port 8080
```

常见问题：`externalTrafficPolicy: Local` 导致不在 Pod 所在节点的 NodePort 请求被丢弃：

```yaml
# 如果设置了 Local，只有运行了对应 Pod 的节点才会接受该 NodePort 流量
spec:
  externalTrafficPolicy: Local  # 改为 Cluster（默认）可以解决，但会失去客户端真实 IP
```

### LoadBalancer Service 排障

```bash
# 1. 检查 LoadBalancer 是否分配了外部 IP
kubectl get svc my-lb-service
# NAME            TYPE           CLUSTER-IP    EXTERNAL-IP    PORT(S)
# my-lb-service   LoadBalancer   10.96.1.100   <pending>      80:30080/TCP
# EXTERNAL-IP 是 <pending> 说明云厂商 LB controller 有问题

# 2. 查看 Service Events
kubectl describe svc my-lb-service

# 3. 检查 cloud-controller-manager
kubectl logs -n kube-system -l component=cloud-controller-manager

# 4. 确认 Health Check Target Group（AWS ELB）
# Target Group 的健康检查对应的是 NodePort，确认节点安全组允许 NodePort 范围
```

### 网络策略（NetworkPolicy）排障

```bash
# 查看 Pod 上生效的 NetworkPolicy
kubectl get networkpolicy -n production

# 测试连通性
kubectl exec -n production pod-a -- curl -v http://pod-b:8080/health

# 如果 NetworkPolicy 阻断，curl 会超时（不是 connection refused）
# 区分：
# - connection refused = 端口没有监听（应用问题）
# - 超时 = 防火墙/NetworkPolicy 丢包

# 临时允许（调试用，记得删除）
kubectl annotate networkpolicy my-policy temp-bypass=true
```

---

## 综合排障流程

遇到网络连接问题时，按这个流程走：

```
1. 确认是网络层问题还是应用层问题
   curl -v URL
   → connection refused = 端口/进程问题
   → timeout = 防火墙/路由问题
   → 200 但内容异常 = 应用层问题

2. 定位在哪个环节丢包
   客户端 → 中间网络 → 目标服务器
   tcpdump 分别在两端抓包，看包是否到达

3. 检查连接状态
   ss -tn | grep <IP>
   有大量 SYN_SENT = 服务端没有响应
   有大量 CLOSE_WAIT = 应用没有关闭连接

4. 检查系统资源
   ss -s（连接总数）
   cat /proc/sys/net/netfilter/nf_conntrack_count（conntrack 用量）
   ulimit -n（文件描述符限制）
   
5. 如果是 K8s 环境
   先确认 Service/Endpoint 正常
   再看 iptables/ipvs 规则
   最后看 CNI 状态
```

---

## 常用一行命令

```bash
# 实时统计各状态 TCP 连接数
watch -n1 "ss -nn | awk '{print \$1}' | sort | uniq -c | sort -rn"

# 找出连接数最多的远端 IP
ss -nn state established | awk '{print $5}' | cut -d: -f1 | sort | uniq -c | sort -rn | head

# 查看重传率
ss -ti | grep -oP 'retrans:\K[^,]+' | awk '{sum+=$1} END{print "total retrans:", sum}'

# 统计 SYN_RECV 数（高于几千可能在被 SYN Flood）
ss -nn state syn-recv | wc -l

# 找出占用最多连接的进程
ss -tnp | awk '{print $6}' | sort | uniq -c | sort -rn | head

# 抓包并实时显示 HTTP 请求路径（明文）
tcpdump -i any -nn -A port 8080 2>/dev/null | grep -E "^(GET|POST|PUT|DELETE) "
```

网络排障最大的原则是：**不要假设，用数据说话**。一次抓包胜过十次猜测。掌握了 tcpdump + ss + conntrack 这套工具链，大部分网络问题都能在30分钟内定位到根因。
