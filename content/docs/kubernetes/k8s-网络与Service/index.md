---
title: "Kubernetes 网络模型与 Service 详解"
date: 2025-12-09T10:00:00+08:00
draft: false
tags: ["Kubernetes", "网络", "Service", "CNI", "运维"]
categories: ["Kubernetes"]
description: "深入解析 Kubernetes 网络模型四大要求、CNI 插件选型、Service 四种类型工作原理、kube-proxy 模式对比、CoreDNS 服务发现及网络故障排查实践。"
summary: "从 K8s 网络基础模型到生产级 Service 配置，覆盖 CNI 插件对比、kube-proxy 模式选择、DNS 解析规则和排查思路。"
toc: true
math: false
diagram: false
keywords: ["Kubernetes", "Service", "CNI", "kube-proxy", "CoreDNS", "网络模型", "Flannel", "Calico", "Cilium"]
params:
  reading_time: true
---

## Kubernetes 网络模型四大要求

Kubernetes 对网络有四条核心约束，所有 CNI 插件必须满足：

1. **Pod 间通信不需要 NAT**：任意 Pod 可以用对方的 Pod IP 直接通信
2. **Node 与 Pod 通信不需要 NAT**：节点可以直接访问任何 Pod IP
3. **Pod 看到的自身 IP 与外界看到的一致**：不存在 IP 伪装问题
4. **跨节点 Pod 通信**：不同节点上的 Pod 也能直接互访

这意味着每个 Pod 有独立 IP，且整个集群共享一个扁平网络（flat network），这与 Docker 的 NAT 模型完全不同。

```
节点A                          节点B
┌─────────────────┐            ┌─────────────────┐
│  Pod-A          │            │  Pod-B          │
│  10.0.1.5:8080  │◄──────────►│  10.0.2.7:8080  │
│                 │  直接通信   │                 │
└─────────────────┘            └─────────────────┘
    eth0: 192.168.1.10              eth0: 192.168.1.11
```

---

## CNI 插件对比

| 插件 | 工作模式 | 网络策略 | 性能 | 适用场景 |
|------|----------|----------|------|----------|
| **Flannel** | Overlay (VXLAN/host-gw) | 不支持（需配合 Canal） | 中等 | 简单集群，快速搭建 |
| **Calico** | BGP 路由 / Overlay | 支持（NetworkPolicy） | 高 | 生产级，大规模集群 |
| **Cilium** | eBPF | 支持（L3-L7） | 最高 | 高性能，安全要求高 |
| **Terway** | VPC 弹性网卡 | 支持 | 极高 | 阿里云 ACK 专用 |
| **AWS VPC CNI** | ENI 直通 | 支持（SG for Pods） | 极高 | AWS EKS 专用 |

### Calico 安装示例

```bash
# 使用 operator 安装 Calico
kubectl create -f https://raw.githubusercontent.com/projectcalico/calico/v3.27.0/manifests/tigera-operator.yaml

cat <<EOF | kubectl apply -f -
apiVersion: operator.tigera.io/v1
kind: Installation
metadata:
  name: default
spec:
  calicoNetwork:
    ipPools:
    - blockSize: 26
      cidr: 10.244.0.0/16
      encapsulation: VXLANCrossSubnet  # 同子网用 BGP，跨子网用 VXLAN
      natOutgoing: Enabled
      nodeSelector: all()
EOF
```

### Cilium 安装示例（Helm）

```bash
helm repo add cilium https://helm.cilium.io/
helm install cilium cilium/cilium \
  --namespace kube-system \
  --set kubeProxyReplacement=true \   # 替换 kube-proxy
  --set k8sServiceHost=<API_SERVER_IP> \
  --set k8sServicePort=6443
```

---

## Service 四种类型

### ClusterIP（默认）

集群内部虚拟 IP，只能在集群内访问。kube-proxy 通过 iptables/IPVS 将流量转发到后端 Pod。

```yaml
apiVersion: v1
kind: Service
metadata:
  name: my-app
  namespace: production
spec:
  type: ClusterIP
  selector:
    app: my-app
  ports:
    - name: http
      port: 80          # Service 端口
      targetPort: 8080  # Pod 端口
      protocol: TCP
```

访问方式：`curl http://my-app.production.svc.cluster.local`

### NodePort

在每个节点上开放一个端口（30000-32767），通过 `NodeIP:NodePort` 从外部访问。

```yaml
apiVersion: v1
kind: Service
metadata:
  name: my-app-nodeport
spec:
  type: NodePort
  selector:
    app: my-app
  ports:
    - port: 80
      targetPort: 8080
      nodePort: 31080   # 不指定则随机分配
```

```bash
# 从集群外访问
curl http://192.168.1.10:31080

# 查看 NodePort 分配
kubectl get svc my-app-nodeport -o jsonpath='{.spec.ports[0].nodePort}'
```

### LoadBalancer

在 NodePort 基础上，由云厂商自动创建外部负载均衡器（CLB/ALB/NLB）。

```yaml
apiVersion: v1
kind: Service
metadata:
  name: my-app-lb
  annotations:
    # AWS NLB
    service.beta.kubernetes.io/aws-load-balancer-type: "nlb"
    service.beta.kubernetes.io/aws-load-balancer-scheme: "internet-facing"
    # 阿里云 SLB
    # service.beta.kubernetes.io/alibaba-cloud-loadbalancer-spec: "slb.s2.small"
spec:
  type: LoadBalancer
  selector:
    app: my-app
  ports:
    - port: 80
      targetPort: 8080
```

```bash
# 等待 EXTERNAL-IP 分配
kubectl get svc my-app-lb -w

# 输出示例
NAME        TYPE           CLUSTER-IP     EXTERNAL-IP        PORT(S)        AGE
my-app-lb   LoadBalancer   10.96.45.123   a1b2c3.elb.amazonaws.com   80:31234/TCP   2m
```

### ExternalName

将 Service 映射到外部 DNS 名称，不创建任何代理，纯粹是 CNAME 记录。

```yaml
apiVersion: v1
kind: Service
metadata:
  name: external-db
  namespace: production
spec:
  type: ExternalName
  externalName: my-rds.abc123.us-west-2.rds.amazonaws.com
```

应用通过 `external-db.production.svc.cluster.local` 访问，实际解析为 RDS 地址，便于后续迁移。

---

## kube-proxy 工作模式

### iptables 模式（默认）

每个 Service 创建一组 iptables 规则，随机选择后端 Pod。规则数量随 Service 数线性增长，1000+ Service 时性能下降明显。

```bash
# 查看 iptables 规则（以 my-app Service 为例）
iptables -t nat -L KUBE-SERVICES | grep my-app
iptables -t nat -L KUBE-SVC-XXXXXXXX   # 查看对应 chain

# 当前 iptables 规则数
iptables -t nat -L | wc -l
```

### IPVS 模式（推荐生产使用）

基于 Linux IPVS（内核级负载均衡），哈希表查找复杂度 O(1)，支持多种调度算法。

```bash
# 检查节点 IPVS 支持
lsmod | grep ip_vs

# 切换到 IPVS 模式（修改 kube-proxy ConfigMap）
kubectl -n kube-system edit configmap kube-proxy
```

```yaml
# kube-proxy ConfigMap 关键配置
apiVersion: v1
kind: ConfigMap
metadata:
  name: kube-proxy
  namespace: kube-system
data:
  config.conf: |
    mode: "ipvs"
    ipvs:
      scheduler: "rr"          # round-robin，也支持 lc/dh/sh/sed/nq
      strictARP: true          # LoadBalancer 模式必须开启
    iptables:
      masqueradeAll: false
```

```bash
# 重启 kube-proxy DaemonSet 生效
kubectl -n kube-system rollout restart daemonset kube-proxy

# 验证 IPVS 规则
ipvsadm -Ln | grep -A3 "10.96.45.123:80"
```

| 对比项 | iptables | IPVS |
|--------|----------|------|
| 查找复杂度 | O(n) | O(1) |
| 调度算法 | 随机 | RR/LC/DH/SH 等 |
| 规则更新 | 全量刷新 | 增量更新 |
| 适用规模 | < 1000 Service | 无限制 |
| 健康检查 | 不支持 | 支持 |

---

## Endpoints 与 EndpointSlice

Service 选中的 Pod 列表存储在 Endpoints/EndpointSlice 对象中。

```bash
# 查看 Service 对应的 Endpoints
kubectl get endpoints my-app -o yaml

# 输出示例
subsets:
  - addresses:
    - ip: 10.244.1.5
      targetRef:
        kind: Pod
        name: my-app-7d6b9f-abc12
    - ip: 10.244.2.8
      targetRef:
        kind: Pod
        name: my-app-7d6b9f-xyz89
    ports:
    - port: 8080
      protocol: TCP
```

EndpointSlice（K8s 1.17+ 默认启用）将 Endpoints 分片存储，每片最多 100 个端点，解决大规模集群的性能问题：

```bash
kubectl get endpointslices -l kubernetes.io/service-name=my-app
```

---

## DNS 服务发现（CoreDNS）

### 服务名解析规则

| 访问方式 | 解析结果 | 适用场景 |
|----------|----------|----------|
| `my-app` | 同 Namespace（需同 NS） | 同命名空间内 |
| `my-app.production` | production 命名空间 | 跨命名空间 |
| `my-app.production.svc` | 同上 | 明确指定 |
| `my-app.production.svc.cluster.local` | 完整 FQDN | 最明确 |

```bash
# 查看 CoreDNS 配置
kubectl -n kube-system get configmap coredns -o yaml

# 在 Pod 内调试 DNS
kubectl run dnsutils --image=gcr.io/kubernetes-e2e-test-images/dnsutils:1.3 -it --rm -- bash
nslookup my-app.production.svc.cluster.local
dig my-app.production.svc.cluster.local
```

### CoreDNS 自定义配置

```yaml
# 添加自定义 hosts 或转发规则
apiVersion: v1
kind: ConfigMap
metadata:
  name: coredns
  namespace: kube-system
data:
  Corefile: |
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
        # 自定义转发：内部域名走私有 DNS
        forward internal.company.com 10.0.0.53
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

---

## Headless Service

不分配 ClusterIP（`clusterIP: None`），DNS 直接返回所有 Pod IP，适用于 StatefulSet 和需要客户端负载均衡的场景。

```yaml
apiVersion: v1
kind: Service
metadata:
  name: mysql-headless
  namespace: production
spec:
  clusterIP: None        # 关键：不分配 VIP
  selector:
    app: mysql
  ports:
    - port: 3306
      targetPort: 3306
```

```bash
# Headless Service DNS 返回多个 A 记录
nslookup mysql-headless.production.svc.cluster.local
# Server:  10.96.0.10
# Address: 10.96.0.10#53
# Name: mysql-headless.production.svc.cluster.local
# Address: 10.244.1.5
# Address: 10.244.2.8
# Address: 10.244.3.2

# StatefulSet Pod 通过固定 DNS 访问
# mysql-0.mysql-headless.production.svc.cluster.local
# mysql-1.mysql-headless.production.svc.cluster.local
```

---

## NetworkPolicy 网络策略

默认所有 Pod 互通，NetworkPolicy 用于实现网络隔离：

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: api-allow-only-frontend
  namespace: production
spec:
  podSelector:
    matchLabels:
      app: api-server
  policyTypes:
    - Ingress
    - Egress
  ingress:
    - from:
        - podSelector:
            matchLabels:
              app: frontend      # 只允许 frontend Pod 访问
        - namespaceSelector:
            matchLabels:
              name: monitoring   # 允许 monitoring 命名空间访问（Prometheus 抓取）
      ports:
        - protocol: TCP
          port: 8080
  egress:
    - to:
        - podSelector:
            matchLabels:
              app: mysql
      ports:
        - protocol: TCP
          port: 3306
    - to: []                     # 允许 DNS 查询
      ports:
        - protocol: UDP
          port: 53
```

---

## Service 故障排查

### ClusterIP 不通

```bash
# 1. 确认 Service 存在且 Selector 正确
kubectl get svc my-app -o yaml
kubectl describe svc my-app

# 2. 确认 Endpoints 不为空
kubectl get endpoints my-app
# 如果 ENDPOINTS 列为 <none>，说明没有匹配的 Pod

# 3. 确认 Pod 正在运行且标签匹配
kubectl get pods -l app=my-app
kubectl get pods --show-labels

# 4. 直接访问 Pod IP 测试
kubectl get pods -l app=my-app -o jsonpath='{.items[0].status.podIP}'
kubectl exec -it test-pod -- curl http://10.244.1.5:8080

# 5. 通过 ClusterIP 访问测试
kubectl exec -it test-pod -- curl http://10.96.45.123:80

# 6. 检查 kube-proxy 状态
kubectl -n kube-system get pods -l k8s-app=kube-proxy
kubectl -n kube-system logs daemonset/kube-proxy | tail -50
```

### 外部无法访问 LoadBalancer

```bash
# 1. 检查 EXTERNAL-IP 是否分配
kubectl get svc my-app-lb

# 2. 如果 EXTERNAL-IP 一直是 <pending>，检查云厂商 LB 控制器
kubectl -n kube-system get pods | grep aws-load-balancer
kubectl -n kube-system logs deployment/aws-load-balancer-controller | tail -50

# 3. 检查安全组是否放通了 NodePort 范围 (30000-32767)
# AWS: 检查节点 SG 的入站规则

# 4. 检查节点是否可达
curl http://<NodeIP>:<NodePort>

# 5. 检查 Pod readiness probe
kubectl describe pod <pod-name> | grep -A10 "Readiness"
```

### DNS 解析失败

```bash
# 1. 检查 CoreDNS Pod 状态
kubectl -n kube-system get pods -l k8s-app=kube-dns
kubectl -n kube-system logs deployment/coredns

# 2. 在问题 Pod 内测试 DNS
kubectl exec -it <pod-name> -- nslookup kubernetes.default.svc.cluster.local

# 3. 检查 Pod 的 /etc/resolv.conf
kubectl exec -it <pod-name> -- cat /etc/resolv.conf
# 应该包含：
# nameserver 10.96.0.10
# search production.svc.cluster.local svc.cluster.local cluster.local

# 4. 检查 CoreDNS ConfigMap
kubectl -n kube-system get configmap coredns -o yaml

# 5. 测试外部 DNS 解析
kubectl exec -it <pod-name> -- nslookup google.com
```

---

## 生产配置建议

```yaml
# Service 生产配置模板
apiVersion: v1
kind: Service
metadata:
  name: my-app
  namespace: production
  annotations:
    # AWS NLB 跨可用区
    service.beta.kubernetes.io/aws-load-balancer-cross-zone-load-balancing-enabled: "true"
spec:
  type: LoadBalancer
  selector:
    app: my-app
  # 流量策略：Local 保留客户端源 IP，但可能不均衡
  # Cluster 均衡分发但会做 SNAT
  externalTrafficPolicy: Cluster
  sessionAffinity: None
  ports:
    - name: http
      port: 80
      targetPort: 8080
    - name: https
      port: 443
      targetPort: 8443
  # 健康检查端口（给 LB 探活用）
  healthCheckNodePort: 31234
```

```bash
# 生产常用排查命令速查
kubectl get svc,endpoints,endpointslices -n production
kubectl describe svc <name> -n production
kubectl get events -n production --sort-by='.lastTimestamp' | tail -20
# 查看 iptables/IPVS 规则
iptables -t nat -L KUBE-SERVICES -n | grep <ClusterIP>
ipvsadm -Ln | grep -A5 <ClusterIP>
```
