---
title: "Kubernetes 网络深度解析——CNI、kube-proxy、NetworkPolicy 完全指南"
date: 2025-01-10T13:50:00+08:00
draft: false
tags: ["Kubernetes", "网络", "CNI", "NetworkPolicy", "Cilium", "kube-proxy"]
categories: ["Kubernetes"]
series: ["K8s 完全指南"]
description: Kubernetes 网络深度解析：从 Pod 网络模型、CNI 选型（Calico vs Cilium）、kube-proxy 模式，到 NetworkPolicy 实战隔离策略，彻底搞懂 K8s 网络
summary: K8s 网络是很多工程师的知识盲区，平时不出问题就忽略，一出问题就完全不知道从哪下手。我在多次生产网络故障的排查中，深刻理解了 K8s 网络的每一层。这篇文章从 Pod 网络模型讲到 NetworkPolicy 实战，帮你建立完整的 K8s 网络知识体系。
toc: true
math: false
diagram: false
keywords: ["Kubernetes", "CNI", "kube-proxy", "NetworkPolicy", "Calico", "Cilium", "eBPF", "Service", "网络排查"]
params:
  reading_time: true
---

K8s 网络是不少工程师的知识盲区——平时不出问题就忽略，一出问题就不知道从哪下手。这些年排过几次生产网络故障，每次都要重新把每一层捋一遍。这篇按我自己的理解顺序写：Pod 网络模型、CNI、kube-proxy、NetworkPolicy。

## K8s 网络的四大基本要求

K8s 对网络有明确的规范，任何 CNI 插件都必须满足：

1. **Pod-to-Pod**：同节点或跨节点的 Pod 之间可以直接通信，不需要 NAT
2. **Pod-to-Node**：Pod 可以访问所在节点，节点可以访问所有 Pod
3. **Pod-to-Service**：Pod 可以通过 Service ClusterIP 访问服务
4. **外部流量入口**：外部流量可以通过 NodePort/LoadBalancer 进入集群

这里最关键的是第一条——**Pod 间通信无 NAT**。每个 Pod 都有独立的 IP，Pod 看到的源 IP 就是对端真实的 Pod IP，不经过任何地址转换。这和 Docker bridge 网络的 NAT 模式完全不同。

---

## CNI 工作原理：veth pair 和 IPAM

当 kubelet 创建 Pod 时，CNI 插件负责：

1. 创建 **veth pair**（虚拟以太网对）：一端放入 Pod 的 network namespace，另一端留在宿主机
2. 给 Pod 端分配 IP（IPAM：IP Address Management）
3. 配置路由，让 Pod 能访问集群内其他地址

```bash
# 在宿主机上查看 veth pair
ip link show type veth

# 进入 Pod 查看网络接口
kubectl exec -it <pod-name> -- ip addr
# eth0@if15  <- if15 是宿主机侧的接口编号

# 在宿主机找对应接口
ip link show | grep "^15:"
```

**跨节点通信的两种模式**：

- **Overlay（隧道模式）**：VXLAN/IPIP 封装，在 IP 包外再包一层 UDP，兼容性好但有封包开销
- **Underlay（路由模式）**：修改底层路由表，直接路由，性能更好但需要网络设备支持（BGP 或同一 L2 域）

---

## Calico vs Cilium：选型对比

### Calico

Calico 是最成熟的 CNI 之一，有两种工作模式：

**BGP 路由模式**（推荐）：节点之间通过 BGP 协议交换路由，Pod IP 直接可路由，没有封包开销。

```bash
# Calico BGP 状态
calicoctl node status

# 查看 BGP peer
calicoctl get bgpPeer

# 查看路由表（能看到其他节点的 Pod CIDR 路由）
ip route show | grep "via"
# 192.168.1.0/24 via 10.0.1.5 dev eth0  <- 节点 10.0.1.5 上的 Pod 网段
```

**IPIP 模式**：跨子网时的 fallback，有额外封包开销。

Calico 的 NetworkPolicy 基于 iptables（或 eBPF），成熟稳定，文档完善。

### Cilium

Cilium 是新一代 CNI，核心差异是**基于 eBPF 替代 iptables**。

```
传统 iptables 路径：
用户态 -> 内核网络栈 -> iptables 链（NAT/filter）-> 转发

Cilium eBPF 路径：
用户态 -> XDP/TC hook（eBPF 程序直接处理）-> 转发
```

**eBPF 的核心优势**：

| 对比项 | iptables | eBPF |
|--------|---------|------|
| 规则查找复杂度 | O(n) 链式遍历 | O(1) 哈希表 |
| 5000 Service 规则数 | ~25万条 iptables 规则 | 哈希表几乎无影响 |
| 可观测性 | 有限 | Hubble 提供完整 L7 可见性 |
| NetworkPolicy | L3/L4 | L3/L4/L7（HTTP path/method）|

```bash
# Cilium 状态检查
cilium status

# 查看 Hubble 网络流量观测
hubble observe --namespace production --last 100

# 查看 NetworkPolicy 命中情况
hubble observe --verdict DROPPED -n production
```

**选型建议**：

- 新集群、追求性能和可观测性 → **Cilium**
- 需要稳定性、团队熟悉度、AWS/GKE 托管 → **Calico**
- 阿里云 ACK 等托管 K8s → 通常强制使用厂商 CNI（Terway），不要换

---

## kube-proxy：iptables 模式 vs IPVS 模式

kube-proxy 负责实现 Service 的负载均衡，在每个节点上维护规则，把 ClusterIP 流量转发到后端 Pod。

### iptables 模式

每个 Service 和 Endpoint 都对应一批 iptables 规则，流量命中后随机选一个后端。

```bash
# 查看 ClusterIP 对应的 iptables 规则
iptables -t nat -L KUBE-SERVICES -n | grep <ClusterIP>

# 查看具体的转发规则
iptables -t nat -L KUBE-SVC-XXXX -n
# 每条规则带 statistic probability，实现随机负载均衡
```

**iptables 模式的问题**：规则数量随 Service 数量线性增长。1000 个 Service 就有几万条规则，每个新连接都需要遍历所有规则，内核锁竞争严重。大集群（> 500 Service）下延迟可以达到几百毫秒。

### IPVS 模式

IPVS（IP Virtual Server）使用内核级别的哈希表，查找复杂度 O(1)：

```bash
# 启用 IPVS 模式（kube-proxy 配置）
# kube-proxy ConfigMap 中设置 mode: "ipvs"

# 查看 IPVS 规则
ipvsadm -Ln

# 输出示例：
# TCP 10.96.0.1:443 rr
#   -> 10.0.1.5:6443  Masq 1 0 0
#   -> 10.0.1.6:6443  Masq 1 0 0
```

IPVS 还支持多种负载均衡算法：`rr`（轮询）、`lc`（最小连接）、`sh`（源地址哈希，会话保持）。

**切换建议**：集群 Service 数量超过 200 个，就应该考虑切换到 IPVS 模式，或者直接用 Cilium 完全绕过 kube-proxy。

---

## Service 类型深度解析

### ClusterIP（默认）

仅集群内部可访问的虚拟 IP，kube-proxy 负责转发。

```yaml
apiVersion: v1
kind: Service
metadata:
  name: myapp
spec:
  selector:
    app: myapp
  ports:
  - port: 80
    targetPort: 8080
  type: ClusterIP
```

DNS 解析：`myapp.production.svc.cluster.local` → ClusterIP

### Headless Service

不分配 ClusterIP，DNS 直接解析到 Pod IP 列表：

```yaml
spec:
  clusterIP: None   # Headless
  selector:
    app: myapp
```

DNS 解析：`myapp.production.svc.cluster.local` → [Pod1 IP, Pod2 IP, ...]

**使用场景**：StatefulSet（数据库集群）、需要客户端自己做负载均衡的场景。

### NodePort

在每个节点上开放一个端口（30000-32767），外部流量 → NodeIP:NodePort → Service → Pod：

```yaml
spec:
  type: NodePort
  ports:
  - port: 80
    targetPort: 8080
    nodePort: 30080   # 指定端口，不指定则随机分配
```

### LoadBalancer

在云厂商上自动创建外部负载均衡器，是生产环境暴露服务的标准方式：

```yaml
spec:
  type: LoadBalancer
  # 云厂商通过 annotations 控制 LB 行为
  annotations:
    service.beta.kubernetes.io/aws-load-balancer-type: "nlb"
    service.beta.kubernetes.io/aws-load-balancer-internal: "true"  # 内网 LB
```

---

## NetworkPolicy 实战：零信任网络策略

### 默认拒绝策略（必须先建）

```yaml
# 拒绝 production 命名空间所有入流量
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny-ingress
  namespace: production
spec:
  podSelector: {}   # 匹配所有 Pod
  policyTypes:
  - Ingress

---
# 拒绝所有出流量
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny-egress
  namespace: production
spec:
  podSelector: {}
  policyTypes:
  - Egress
```

### 白名单方式开放访问

```yaml
# 允许 frontend 访问 backend 的 8080 端口
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-frontend-to-backend
  namespace: production
spec:
  podSelector:
    matchLabels:
      app: backend
  policyTypes:
  - Ingress
  ingress:
  - from:
    - podSelector:
        matchLabels:
          app: frontend
    ports:
    - protocol: TCP
      port: 8080

---
# 允许跨命名空间访问：monitoring 命名空间的 Prometheus 抓取 production 的 metrics
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-prometheus-scrape
  namespace: production
spec:
  podSelector:
    matchLabels:
      app: myapp
  policyTypes:
  - Ingress
  ingress:
  - from:
    - namespaceSelector:
        matchLabels:
          kubernetes.io/metadata.name: monitoring
      podSelector:
        matchLabels:
          app: prometheus
    ports:
    - protocol: TCP
      port: 9090

---
# 允许 DNS 查询（必须放行，否则服务解析域名会失败）
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-dns
  namespace: production
spec:
  podSelector: {}
  policyTypes:
  - Egress
  egress:
  - to:
    - namespaceSelector:
        matchLabels:
          kubernetes.io/metadata.name: kube-system
      podSelector:
        matchLabels:
          k8s-app: kube-dns
    ports:
    - protocol: UDP
      port: 53
    - protocol: TCP
      port: 53
```

**常见陷阱**：启用 `default-deny-egress` 后忘记放行 DNS（UDP/TCP 53），导致所有服务 DNS 解析失败，症状和网络不通一样，但 `nslookup` 会超时。

---

## 常见网络排查步骤

### DNS 解析失败

```bash
# 1. 进入问题 Pod 测试 DNS
kubectl exec -it <pod> -- nslookup myapp.production.svc.cluster.local

# 2. 直接测试 CoreDNS IP（绕过 FQDN）
kubectl exec -it <pod> -- nslookup myapp 10.96.0.10  # CoreDNS ClusterIP

# 3. 查看 CoreDNS 日志
kubectl logs -n kube-system -l k8s-app=kube-dns --tail=100

# 4. 检查 /etc/resolv.conf 配置
kubectl exec -it <pod> -- cat /etc/resolv.conf
# 应该包含 nameserver <CoreDNS ClusterIP> 和正确的 search 域
```

### Service 不通

```bash
# 1. 确认 Endpoints 不为空
kubectl get endpoints myapp -n production
# 为空说明 selector 不匹配或 Pod 没有 Ready

# 2. 确认 Pod 本身正常
kubectl exec -it <pod> -- curl localhost:8080/health

# 3. 在集群内直接访问 Pod IP（绕过 Service）
kubectl exec -it <another-pod> -- curl <pod-ip>:8080

# 4. 通过 ClusterIP 访问
kubectl exec -it <another-pod> -- curl <cluster-ip>:80

# 5. 查看 kube-proxy 日志
kubectl logs -n kube-system -l k8s-app=kube-proxy --tail=100
```

### 跨节点延迟高

```bash
# 1. 确认是跨节点还是同节点问题
# 创建两个 Pod，分别 pin 到不同节点
kubectl run test1 --image=busybox --overrides='{"spec":{"nodeName":"node1"}}' -- sleep 3600
kubectl run test2 --image=busybox --overrides='{"spec":{"nodeName":"node2"}}' -- sleep 3600

# 测试延迟
kubectl exec -it test2 -- ping <test1-pod-ip>

# 2. 如果跨节点延迟明显高于同节点，检查 MTU 设置
# Overlay 网络需要减小 MTU（VXLAN 封包需要额外 50 字节）
ip link show | grep mtu

# 3. 查看节点网络接口的丢包和错误
ip -s link show eth0
```

有一次我们的 VXLAN overlay 网络在某个可用区出现间歇性丢包，`ping` 测试显示偶发 5-10% 丢包率，但 Pod 之间直连正常。最终排查到是底层交换机对 UDP 4789 端口（VXLAN）做了流量限速，换成 BGP 直接路由模式后彻底解决。

---

## 排查工具速查

```bash
# 网络连通性测试
kubectl run netshoot --image=nicolaka/netshoot --rm -it -- bash

# 在 netshoot 里可以用：
# ping / traceroute / mtr - 连通性和路由追踪
# nslookup / dig - DNS 排查
# curl / wget - HTTP 测试
# tcpdump - 抓包分析
# ss / netstat - 连接状态

# 抓包（在节点上）
tcpdump -i any host <pod-ip> -w /tmp/capture.pcap

# 查看 iptables 规则命中计数
iptables -t nat -L -n -v | grep <ClusterIP>
# 如果 pkts 一直是 0，说明流量根本没到这条规则
```

K8s 网络看起来复杂，但每一层都有清晰的职责：CNI 负责 Pod IP 和跨节点路由，kube-proxy 负责 Service 的负载均衡转发，NetworkPolicy 负责访问控制。理清这三层，90% 的网络问题都能快速定位。
