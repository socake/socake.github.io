---
title: "CoreDNS 深度排障：K8s DNS 问题完全指南"
date: 2026-04-12T18:00:00+08:00
draft: false
tags: ["coredns", "kubernetes", "dns", "troubleshooting", "networking"]
categories: ["Kubernetes"]
description: "K8s DNS 解析链路、ndots=5 的坑、CoreDNS 调优配置，以及常见 DNS 超时和 5 秒延迟问题的完整排障指南。"
summary: "DNS 问题是 K8s 中最难定位的问题之一，因为它的失败往往是间歇性的、有延迟的，看起来像网络问题，实际上是 DNS 超时。本文记录了我在生产环境排查过的多类 DNS 故障，附详细的抓包分析和调优配置。"
toc: true
math: false
diagram: false
series: ["K8s 完全指南"]
keywords: ["coredns", "kubernetes dns", "ndots", "dns 超时", "5秒延迟", "dns 排障"]
params:
  reading_time: true
---

## K8s DNS 解析链路

在排障之前，必须先搞清楚一个 Pod 里的 DNS 请求是怎么走的。

```
Pod 内应用
    │ DNS 查询 (UDP 53)
    ▼
/etc/resolv.conf 中的 nameserver（通常是 CoreDNS Service ClusterIP）
    │
    ▼
CoreDNS Pod（通常2-3个副本，运行在 kube-system）
    │
    ├─ cluster.local 域 → 查 K8s 内部 Service/Pod DNS
    ├─ 反向查找 → 查 K8s 内部
    └─ 其他域名 → Forward 到上游 DNS（通常是节点的 /etc/resolv.conf）
```

查看 Pod 的 DNS 配置：

```bash
kubectl exec -n production my-pod -- cat /etc/resolv.conf
# nameserver 10.96.0.10       ← CoreDNS Service IP
# search production.svc.cluster.local svc.cluster.local cluster.local
# options ndots:5
```

这三行是理解所有 DNS 问题的起点。

---

## ndots:5 的坑

`ndots:5` 是 K8s 给每个 Pod 设置的默认值，意思是：如果查询的域名中点的个数少于5个，就先在 search 列表中逐一尝试追加后缀，失败后才查原始域名。

### 一次请求变多次

假设 Pod 查询 `api.example.com`：

```
api.example.com → 点数=2，< 5，先走 search 列表：

1. api.example.com.production.svc.cluster.local → NXDOMAIN
2. api.example.com.svc.cluster.local            → NXDOMAIN
3. api.example.com.cluster.local                → NXDOMAIN
4. api.example.com.                             → 外部 DNS 解析成功 ✓
```

一次看似简单的 DNS 查询，在 Pod 里实际发出了 **4 次 UDP 请求**。在高并发下，这 3 次无效查询会显著增加 CoreDNS 负载，也会增加应用的 DNS 解析延迟。

### 解决方案

**方案1：域名末尾加点（FQDN）**

```python
# Python
import requests
# 改成 FQDN（末尾加点），跳过 search 列表
requests.get("http://api.example.com./v1/data")
```

**方案2：在 Pod 的 dnsConfig 中调整**

```yaml
apiVersion: v1
kind: Pod
spec:
  dnsConfig:
    options:
      - name: ndots
        value: "2"   # 减小阈值，只有1个点才走 search
      - name: timeout
        value: "5"
      - name: attempts
        value: "2"
```

**方案3：在 Deployment 中统一设置**

```yaml
spec:
  template:
    spec:
      dnsConfig:
        options:
          - name: ndots
            value: "2"
          - name: single-request-reopen   # 解决 5 秒延迟，后文详解
```

---

## CoreDNS 配置详解

CoreDNS 通过 ConfigMap `coredns` 在 `kube-system` 命名空间中配置：

```bash
kubectl get configmap coredns -n kube-system -o yaml
```

默认 Corefile：

```
.:53 {
    errors
    health {
        lameduck 5s
    }
    ready
    kubernetes cluster.local in-addr.arpa ip6.arpa {
        pods insecure
        fallthrough in-addr.arpa ip6.arpa
        ttl 30
    }
    prometheus :9153
    forward . /etc/resolv.conf {
        max_concurrent 1000
    }
    cache 30
    loop
    reload
    loadbalance
}
```

### 配置调优

```
.:53 {
    errors

    # 健康检查宽限期（滚动重启时给 CoreDNS 优雅退出时间）
    health {
        lameduck 10s
    }
    ready

    kubernetes cluster.local in-addr.arpa ip6.arpa {
        pods insecure
        fallthrough in-addr.arpa ip6.arpa
        ttl 30
    }

    prometheus :9153

    # 上游 DNS Forward 调优
    forward . 8.8.8.8 8.8.4.4 {
        max_concurrent 1000
        prefer_udp         # 优先 UDP（避免 TCP 连接开销）
        health_check 5s    # 上游健康检查间隔
    }

    # 缓存调优
    cache {
        success 9984 30    # 成功响应缓存：最多9984条，TTL 30s
        denial 9984 5      # 失败响应（NXDOMAIN）缓存：TTL 5s
        prefetch 10 1m 10% # 缓存命中率高的域名提前刷新
    }

    loop    # 防止 DNS 转发循环
    reload  # 自动 reload ConfigMap（无需重启 Pod）
    loadbalance round_robin  # 多 A 记录时轮询
}
```

修改配置后：

```bash
kubectl edit configmap coredns -n kube-system
# reload 插件会在 30 秒内自动生效，无需重启 CoreDNS Pod
```

---

## 常见故障排障

### 故障1：DNS 解析失败（NXDOMAIN）

**现象**：服务启动时 `dial tcp: lookup svc-name: no such host`

**排查步骤**：

```bash
# 1. 确认 CoreDNS Pod 是否正常
kubectl get pods -n kube-system -l k8s-app=kube-dns
kubectl logs -n kube-system -l k8s-app=kube-dns --since=5m

# 2. 在故障 Pod 中手动测试
kubectl exec -n production my-pod -- nslookup my-service
kubectl exec -n production my-pod -- nslookup my-service.production
kubectl exec -n production my-pod -- nslookup my-service.production.svc.cluster.local

# 3. 确认 Service 存在且 Endpoints 正常
kubectl get svc my-service -n production
kubectl get endpoints my-service -n production

# 4. 检查 Service 的 DNS 记录（格式：<service>.<namespace>.svc.<cluster-domain>）
kubectl exec -n production my-pod -- nslookup my-service.production.svc.cluster.local 10.96.0.10
```

**常见原因**：

- 跨命名空间访问没有带命名空间（`my-service` vs `my-service.other-namespace`）
- Service 名字和实际访问的域名不匹配（大小写、拼写错误）
- CoreDNS 重启后缓存还没有热身，偶发 NXDOMAIN

### 故障2：DNS 查询 5 秒延迟

这是 K8s 最臭名昭著的 DNS 问题，根因是 **Linux conntrack 竞态条件**。

**背景**：

CoreDNS Service 通常绑定一个 ClusterIP，同一个 Pod 同时发出多个 DNS 查询时，Linux 内核的 DNAT 规则存在竞态：两个 UDP 包同时到达，conntrack 表插入产生冲突，导致其中一个包被丢弃。UDP 没有重传机制，只能等超时（默认 5 秒）。

**复现验证**：

```bash
# 抓取 DNS 请求，观察是否有超时重传
kubectl exec -n production my-pod -- \
  tcpdump -i any -nn -w /tmp/dns.pcap port 53 &

# 触发 DNS 查询
kubectl exec -n production my-pod -- \
  for i in $(seq 1 100); do nslookup api.example.com &; done; wait

# 分析抓包（在本地）
tcpdump -r /tmp/dns.pcap -nn | grep "id:" | awk '{print $1, $NF}' | sort
```

**解决方案**：

```yaml
# 方案1：dnsConfig 中加 single-request-reopen
spec:
  dnsConfig:
    options:
      - name: single-request-reopen  # A/AAAA 记录查询用不同 socket，避免竞态
      - name: ndots
        value: "5"
```

```
# 方案2：CoreDNS 使用 TCP（完全避免 UDP 竞态）
# 在 Corefile 的 forward 中加 force_tcp
forward . 8.8.8.8 {
    force_tcp
}
```

```
# 方案3：调整 conntrack 参数（节点级别）
# /etc/sysctl.conf
net.netfilter.nf_conntrack_udp_timeout = 10
net.netfilter.nf_conntrack_udp_timeout_stream = 180
```

实际效果最好的是方案1（`single-request-reopen`），对应用无侵入，只需在 Pod Spec 中加一行。

### 故障3：CoreDNS OOMKill

**现象**：CoreDNS Pod 频繁重启，`kubectl describe pod` 看到 `OOMKilled`

```bash
kubectl top pods -n kube-system -l k8s-app=kube-dns
# NAME                    CPU(cores)   MEMORY(bytes)
# coredns-xxx             200m         450Mi   ← 接近 limit
```

**原因**：大集群中 CoreDNS 默认的内存 limit（170Mi）远不够用，缓存会持续增长。

**解决**：

```yaml
# 通过 HPA 或直接调整 resources
kubectl patch deployment coredns -n kube-system --patch='
{
  "spec": {
    "template": {
      "spec": {
        "containers": [{
          "name": "coredns",
          "resources": {
            "requests": {"cpu": "100m", "memory": "128Mi"},
            "limits": {"cpu": "500m", "memory": "512Mi"}
          }
        }]
      }
    }
  }
}'
```

同时配置 CoreDNS 的 HPA：

```bash
kubectl autoscale deployment coredns \
  --namespace=kube-system \
  --min=2 --max=10 \
  --cpu-percent=70
```

### 故障4：外部域名解析慢

**现象**：集群内访问外部服务域名（如第三方 API）延迟异常高。

**排查**：

```bash
# 测试外部域名解析时间
kubectl exec -n production my-pod -- time nslookup external-api.example.com

# 对比直接查上游
kubectl exec -n production my-pod -- time nslookup external-api.example.com 8.8.8.8
```

如果直接查上游快，说明问题在 CoreDNS 的 forward 环节。

**常见原因**：
- 节点上的 `/etc/resolv.conf` 指向的 DNS 有问题（CoreDNS 默认把节点的 resolv.conf 作为上游）
- 上游 DNS `max_concurrent` 不够，请求排队

**解决**：

```
# 在 Corefile 中直接指定可靠的上游
forward . 8.8.8.8 8.8.4.4 1.1.1.1 {
    max_concurrent 2000
}
```

---

## dnsutils 调试工具箱

```bash
# 部署调试 Pod
kubectl run dnsutils --image=registry.k8s.io/e2e-test-images/jessie-dnsutils:1.3 \
  --restart=Never --rm -it -- sh

# 在 dnsutils Pod 中常用命令
# 基础查询
nslookup kubernetes.default
dig kubernetes.default.svc.cluster.local

# 详细解析过程
dig +trace external-api.example.com

# 测试反向查询
dig -x 10.96.0.1

# 查询 SRV 记录（服务发现）
dig _http._tcp.my-service.production.svc.cluster.local SRV

# 测试解析速度
time for i in $(seq 1 10); do nslookup my-service.production.svc.cluster.local; done
```

---

## 抓包验证

```bash
# 在 CoreDNS Pod 上抓 DNS 请求
kubectl exec -n kube-system coredns-xxx -- \
  tcpdump -i any -nn port 53 -c 100

# 在业务 Pod 上抓（需要 Pod 有 tcpdump 或用 nsenter）
# 通过 nsenter 进入 Pod 网络命名空间（需要节点权限）
# 1. 找到 Pod 在哪个节点
kubectl get pod my-pod -o wide

# 2. SSH 到节点，找到 PID
crictl inspect $(crictl ps --name my-pod -q) | jq .info.pid

# 3. nsenter 进入网络命名空间
nsenter -t <PID> -n -- tcpdump -nn -i eth0 port 53
```

典型的 5 秒延迟抓包特征：

```
# 正常（两次查询，时间戳紧密）
14:00:00.001 A query: api.example.com
14:00:00.003 A response: api.example.com -> 1.2.3.4

# 异常（5秒超时重传）
14:00:00.001 A query: api.example.com
14:00:05.001 A query: api.example.com  ← 5秒后重试
14:00:05.003 A response: api.example.com -> 1.2.3.4
```

---

## CoreDNS 监控指标

```yaml
# Prometheus 告警规则
groups:
  - name: coredns
    rules:
      # DNS 请求错误率 > 1%
      - alert: CoreDNSHighErrorRate
        expr: |
          sum(rate(coredns_dns_responses_total{rcode!="NOERROR",rcode!="NXDOMAIN"}[5m]))
          / sum(rate(coredns_dns_responses_total[5m])) > 0.01
        for: 5m
        annotations:
          summary: "CoreDNS 错误率过高: {{ $value | humanizePercentage }}"

      # DNS P99 延迟 > 500ms
      - alert: CoreDNSHighLatency
        expr: |
          histogram_quantile(0.99,
            sum(rate(coredns_dns_request_duration_seconds_bucket[5m])) by (le, server, zone)
          ) > 0.5
        for: 5m
        annotations:
          summary: "CoreDNS P99 延迟过高"

      # CoreDNS Pod 不足
      - alert: CoreDNSDown
        expr: kube_deployment_status_replicas_available{deployment="coredns", namespace="kube-system"} < 2
        for: 2m
        annotations:
          summary: "CoreDNS 可用副本数不足"
```

---

## 总结

处理 K8s DNS 问题的优先级：

1. **先确认 CoreDNS Pod 是否健康**：Running、Ready、无 OOMKill
2. **用 dnsutils 手工测试**：区分"Pod 内 resolv.conf 问题"和"CoreDNS 本身问题"
3. **区分内部域名 vs 外部域名**：内部问题看 CoreDNS ↔ K8s 服务注册；外部问题看 forward 上游
4. **5 秒延迟**：十有八九是 conntrack 竞态，加 `single-request-reopen`
5. **高并发下 NXDOMAIN 偶发**：检查 CoreDNS 缓存容量和副本数

DNS 排障最大的陷阱是"间歇性"——问题复现率低，容易被误判为网络抖动。养成习惯：凡是应用层面的连接超时，先用 `dig`/`nslookup` 验证 DNS 解析是否正常，再往下排查。
