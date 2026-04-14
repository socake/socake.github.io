---
title: "Cilium NetworkPolicy 与 L7 过滤生产落地实战"
date: 2025-10-31T10:00:00+08:00
draft: false
tags: ["Cilium", "NetworkPolicy", "eBPF", "零信任", "Kubernetes"]
categories: ["零信任"]
description: "一份基于 Cilium 1.16+ 的生产落地笔记：讲清楚 Kubernetes NetworkPolicy 的局限、CiliumNetworkPolicy 的扩展能力、L7 HTTP/Kafka/DNS 过滤的真实用法、Hubble 可观测性、策略开发方法论，以及多集群 ClusterMesh 场景下的策略治理。"
summary: "一份基于 Cilium 1.16+ 的生产落地笔记：讲清楚 Kubernetes NetworkPolicy 的局限、CiliumNetworkPolicy 的扩展能力、L7 HTTP/Kafka/DNS 过滤的真实用法、Hubble 可观测性、策略开发方法论，以及多集群 ClusterMesh 场景下的策略治理。"
toc: true
math: false
diagram: false
keywords: ["Cilium", "NetworkPolicy", "eBPF", "Hubble", "零信任网络"]
params:
  reading_time: true
---

## 开门见山：为什么 NetworkPolicy 这么难落地

我在 K8s 圈子里混了这些年，见过太多团队"装了 Calico/Cilium 但一条 NetworkPolicy 都没写"。不是他们懒，是网络策略这东西**天然不好写**——一个工程师想要限制某个服务的出站只能到 MySQL，他会发现：

1. 他不知道这个服务实际在访问什么（没可观测性）
2. 写了 policy 后一测试就 500（因为忘了放行 DNS）
3. 改完 DNS 后又挂（因为忘了放行 Istio sidecar）
4. 最后妥协写成 `allow all`，等于没写

这整个体验导致大部分团队的 NetworkPolicy 要么不存在，要么是"默认全通 + 几条硬塞的业务规则"。但在一个真正的零信任环境里，**L3/L4 默认拒绝 + 按需放行 + L7 过滤**才是最低标准。

Cilium 是目前唯一能把这套东西在生产规模做好的开源方案。这篇文章基于 **Cilium 1.16+**（2025 年下半年版本），讲如何真正把网络策略落下去。我不讲 Cilium 基础安装，假设你已经有集群，直接进入策略这一段。

## 一、Kubernetes NetworkPolicy 的局限

先说说原生 `NetworkPolicy`（`networking.k8s.io/v1`）有哪些做不了，理解这个是理解 CiliumNetworkPolicy 存在价值的前提。

**原生 NetworkPolicy 能做**：
- 基于 Pod label / Namespace label 的 L3/L4 规则
- Ingress / Egress 方向分开
- TCP/UDP/SCTP 端口
- IPBlock（CIDR）

**做不了**：
1. **L7 过滤**：你不能说"这个 Pod 只能 GET /api/v1/users，不能 POST /admin"
2. **DNS 策略**：你不能说"只允许访问 `*.googleapis.com`"，只能写 IP CIDR
3. **FQDN 拒绝名单**：你不能阻断对 `pastebin.com` 这种 exfiltration 目标的访问
4. **ICMP 过滤**：很多时候 ping 就是穿透防御的第一步
5. **策略审计**：出问题了你不知道哪条规则 drop 了包
6. **节点级策略**：对节点本身流量控制很弱
7. **跨集群策略**：原生 NetworkPolicy 只能在集群内生效

Cilium 通过 `CiliumNetworkPolicy` (CNP) 和 `CiliumClusterwideNetworkPolicy` (CCNP) 扩展了所有这些能力。

## 二、Cilium 的策略模型

### 2.1 Identity 而不是 IP

Cilium 最根本的设计是"**把流量按身份（identity）分类，而不是 IP**"。每个 Pod 根据它的 labels 被分配一个 "security identity" 数字 ID。所有策略都是基于身份 ID 做的，不是 IP。

这个设计在大规模集群里带来的好处非常明显：
- Pod 重启 IP 变了，身份不变，策略不需要重算
- 同一个 Deployment 的 10 个 Pod 共享一个身份 ID，规则数量不随 Pod 数量线性增长
- 跨集群的 Pod 可以共享身份，跨集群策略直接复用

### 2.2 策略决策流程

```
入站包 ──▶ 查 src IP → identity 映射 ──▶ 查 dst pod 的入站策略
                                                │
                                                ▼
                           L3/L4 允许? ──No──▶ drop (记录到 Hubble)
                                │ Yes
                                ▼
                           有 L7 规则? ──No──▶ accept
                                │ Yes
                                ▼
                           走 envoy sidecar (per-node)
                                │
                                ▼
                           L7 解析 + 匹配 ──▶ accept / deny
```

**关键点**：L3/L4 由 eBPF 直接在内核决策，性能极高；L7 规则会把包 redirect 到 node 上的 cilium-envoy（每节点一个），解析 HTTP/Kafka/DNS 后做决策。

### 2.3 CiliumNetworkPolicy vs CiliumClusterwideNetworkPolicy

| 类型 | 作用域 | 典型用途 |
|------|--------|----------|
| NetworkPolicy (原生) | Namespace | 兼容性策略 |
| CiliumNetworkPolicy | Namespace | 业务 L3/L4/L7 策略 |
| CiliumClusterwideNetworkPolicy | 全集群 | 基础设施策略（比如"禁止所有 Pod 访问 metadata 169.254.169.254"） |

生产里两者结合用，CCNP 做"全局基线"，CNP 做"业务定制"。

## 三、从零开始的生产策略体系

我的生产方案是**"默认拒绝 + 分层放行"**。分四层：

```
  ┌───────────────────────────────────────────┐
  │  Layer 1: 全局基线 (CCNP)                 │
  │    - 禁止访问 metadata (169.254.169.254)  │
  │    - 禁止访问 kubelet port 10250          │
  │    - 禁止访问内部管理网段                 │
  │    - 允许所有 Pod 访问 CoreDNS            │
  └───────────────────────────────────────────┘
  ┌───────────────────────────────────────────┐
  │  Layer 2: Namespace 默认拒绝 (CNP)        │
  │    - 每个 namespace 都有一条 default-deny │
  └───────────────────────────────────────────┘
  ┌───────────────────────────────────────────┐
  │  Layer 3: 业务 Pod 放行 (CNP)             │
  │    - 服务 A → 服务 B 的 L4 允许          │
  │    - 服务 C → MySQL 3306                 │
  └───────────────────────────────────────────┘
  ┌───────────────────────────────────────────┐
  │  Layer 4: L7 精细化 (CNP)                 │
  │    - HTTP 路径/方法限制                  │
  │    - 只允许特定 FQDN                     │
  └───────────────────────────────────────────┘
```

### 3.1 Layer 1: 全局基线

第一条策略是任何生产集群都必须有的——禁止 Pod 访问云 metadata 服务：

```yaml
apiVersion: cilium.io/v2
kind: CiliumClusterwideNetworkPolicy
metadata:
  name: deny-cloud-metadata
spec:
  endpointSelector:
    matchExpressions:
      - key: io.kubernetes.pod.namespace
        operator: NotIn
        values: ["kube-system", "cilium-system"]
  egressDeny:
    - toCIDR:
        - 169.254.169.254/32     # AWS / GCP / Alibaba metadata
        - 100.100.100.200/32     # Alibaba userdata
      toPorts:
        - ports:
            - port: "80"
            - port: "443"
```

**为什么重要**：云 metadata 服务暴露 IAM 凭据。一个 Pod 如果能访问 metadata，就可能偷到宿主机绑定的 IAM role。这是 2018 年特斯拉 K8s 被挖矿事件的根因之一。所有云上集群必须有这条。

注意 `egressDeny`（而不是 `egress`），这是 Cilium 1.15+ 的显式拒绝语义，优先级高于 allow。没有 deny 语义的话，一旦其他策略意外放行了 0.0.0.0/0，metadata 就也被放了。

第二条：禁止 Pod 访问 kubelet：

```yaml
apiVersion: cilium.io/v2
kind: CiliumClusterwideNetworkPolicy
metadata:
  name: deny-kubelet-api
spec:
  endpointSelector:
    matchExpressions:
      - key: io.kubernetes.pod.namespace
        operator: NotIn
        values: ["kube-system"]
  egressDeny:
    - toEntities: ["host"]
      toPorts:
        - ports:
            - port: "10250"
            - port: "10255"
```

`toEntities: ["host"]` 是 Cilium 的特殊实体，表示 "所有节点本身的 IP"。原生 NetworkPolicy 里要写节点 IP CIDR，节点弹性伸缩后会失效，Cilium 的 entity 则动态更新。

第三条：允许所有 Pod 访问 CoreDNS：

```yaml
apiVersion: cilium.io/v2
kind: CiliumClusterwideNetworkPolicy
metadata:
  name: allow-dns-egress
spec:
  endpointSelector: {}
  egress:
    - toEndpoints:
        - matchLabels:
            k8s-app: kube-dns
      toPorts:
        - ports:
            - port: "53"
              protocol: UDP
            - port: "53"
              protocol: TCP
          rules:
            dns:
              - matchPattern: "*"   # 先全放，后续按业务收紧
```

**注意 `rules.dns`**：这里启用了 Cilium 的 DNS 代理。启用后 Cilium 会接管 CoreDNS 的响应解析，把 DNS 查询结果的 IP 临时记录到 endpoint 的 "允许 IP 池"。这是 FQDN 策略的基础。

### 3.2 Layer 2: Namespace 默认拒绝

每个业务 namespace 加一条 default-deny：

```yaml
apiVersion: cilium.io/v2
kind: CiliumNetworkPolicy
metadata:
  name: default-deny
  namespace: payments
spec:
  endpointSelector: {}
  ingress:
    - {}        # 不是真的"deny"，是"没有任何 allow 规则"，等于全拒
  egress:
    - {}
```

Cilium 的模型里，**只要一个 Pod 被任何 CNP 选中，它就进入"白名单模式"——没被明确放行的流量全部 drop**。所以上面这个空的 ingress/egress 等于"只要被选中就默认拒绝"。

但这样太严格，至少要放行 DNS 和 kube-api:

```yaml
apiVersion: cilium.io/v2
kind: CiliumNetworkPolicy
metadata:
  name: allow-baseline-egress
  namespace: payments
spec:
  endpointSelector: {}
  egress:
    # DNS
    - toEndpoints:
        - matchLabels:
            k8s-app: kube-dns
            io.kubernetes.pod.namespace: kube-system
      toPorts:
        - ports: [{port: "53", protocol: UDP}]
    # kube-apiserver
    - toEntities: ["kube-apiserver"]
      toPorts:
        - ports: [{port: "443", protocol: TCP}]
```

`toEntities: ["kube-apiserver"]` 是 1.14+ 引入的快捷写法，自动匹配 apiserver endpoint，不用自己维护 CIDR。

### 3.3 Layer 3: 业务 L4 放行

```yaml
apiVersion: cilium.io/v2
kind: CiliumNetworkPolicy
metadata:
  name: checkout-to-db
  namespace: payments
spec:
  endpointSelector:
    matchLabels:
      app: checkout-service
  egress:
    - toEndpoints:
        - matchLabels:
            app: postgres
            tier: primary
      toPorts:
        - ports: [{port: "5432", protocol: TCP}]
```

这条策略非常明确："payments 命名空间里 `app=checkout-service` 的 Pod 只能访问 `app=postgres,tier=primary` 的 Pod 的 5432 端口。" 超出范围的出站都会被 drop。

### 3.4 Layer 4: L7 过滤

这是 Cilium 真正超越原生 NetworkPolicy 的地方。

**HTTP 方法限制**：

```yaml
apiVersion: cilium.io/v2
kind: CiliumNetworkPolicy
metadata:
  name: order-api-readonly
  namespace: orders
spec:
  endpointSelector:
    matchLabels:
      app: order-api
  ingress:
    - fromEndpoints:
        - matchLabels:
            app: order-reader
      toPorts:
        - ports: [{port: "8080", protocol: TCP}]
          rules:
            http:
              - method: "GET"
                path: "/api/v1/orders/.*"
              - method: "GET"
                path: "/api/v1/orders/[^/]+"
```

`order-reader` Pod 只能以 GET 方式访问 `order-api` 的 `/api/v1/orders/*`，其他路径、其他方法都被拒绝。这对"只读副本"、"审计导出"这种服务非常有用。

**FQDN 限制**（非常常用）：

```yaml
apiVersion: cilium.io/v2
kind: CiliumNetworkPolicy
metadata:
  name: allow-s3-only
  namespace: analytics
spec:
  endpointSelector:
    matchLabels:
      app: data-exporter
  egress:
    - toFQDNs:
        - matchName: "s3.us-west-2.amazonaws.com"
        - matchPattern: "*.s3.us-west-2.amazonaws.com"
      toPorts:
        - ports: [{port: "443", protocol: TCP}]
    - toEndpoints:
        - matchLabels:
            k8s-app: kube-dns
            io.kubernetes.pod.namespace: kube-system
      toPorts:
        - ports: [{port: "53", protocol: UDP}]
          rules:
            dns:
              - matchName: "s3.us-west-2.amazonaws.com"
              - matchPattern: "*.s3.us-west-2.amazonaws.com"
```

**注意 dns rules 和 toFQDNs 要成对出现**。Cilium 的 FQDN 策略工作原理是：DNS 代理看到 Pod 查 "xx.s3.amazonaws.com"，解析后临时把结果 IP 加到放行列表，当 Pod 发起 connect 时 eBPF 查这个动态列表决定放不放。如果 DNS rules 没写，代理看不到查询，FQDN 就失效。

**Kafka 过滤**：

```yaml
apiVersion: cilium.io/v2
kind: CiliumNetworkPolicy
metadata:
  name: kafka-topic-acl
spec:
  endpointSelector:
    matchLabels:
      app: event-producer
  egress:
    - toEndpoints:
        - matchLabels:
            app: kafka-broker
      toPorts:
        - ports: [{port: "9092", protocol: TCP}]
          rules:
            kafka:
              - role: "produce"
                topic: "orders.events"
              - role: "produce"
                topic: "orders.audit"
```

这条策略限制 `event-producer` 只能向 `orders.events` 和 `orders.audit` 两个 topic 生产消息。其他 topic、消费操作全部被拒绝。Kafka 本身的 ACL 可以做类似事情但配置复杂，Cilium 这种声明式方式对运维友好得多。

## 四、策略开发方法论：从观察到下发

前面说过"工程师写不出好策略的根因是没可观测性"。Cilium 的答案是 **Hubble**——一个基于 eBPF 的流量可视化工具。

### 4.1 先开 Hubble 观察

部署 Cilium 时启用 Hubble：

```yaml
hubble:
  enabled: true
  relay: { enabled: true }
  ui: { enabled: true }
  metrics:
    enabled:
      - dns
      - drop
      - tcp
      - flow
      - port-distribution
      - icmp
      - httpV2
```

然后用 `hubble observe` 看流量：

```bash
# 看 payments 命名空间的所有流量
hubble observe --namespace payments

# 看 drop 的包
hubble observe --namespace payments --verdict DROPPED

# 看某个 Pod 的所有出站 HTTP
hubble observe --from-pod payments/checkout-xxx --protocol http
```

**方法论是**：

1. 不下发任何策略，只开 Hubble 观察一周
2. 统计每个服务的入站源、出站目标
3. 按观察到的流量生成"审计策略"（mode: audit）
4. 再观察一周，确认策略没漏掉合法流量
5. 切换到 enforce 模式
6. 迭代收紧

### 4.2 Audit 模式

Cilium 1.14+ 支持在 CNP 里设置 audit 模式：

```yaml
spec:
  enableDefaultDeny:
    ingress: false
    egress: false
  # 这里不写 deny 只写 allow
  ingress: [...]
```

但真正的 audit 模式需要在 Cilium config 里开：

```yaml
policyAuditMode: true
```

audit 模式下所有"本应被拒绝"的包依然放行，但会在日志里标记。这让你在不影响业务的前提下验证策略正确性。

### 4.3 用 hubble-exporter 写回策略

有一个社区工具 `cilium-policy-generator`（以及 Isovalent Tetragon 的类似工具）可以从 Hubble 流量推导策略。基本流程：

```bash
hubble observe --namespace payments --last 24h -o json > flows.json
cilium-policy-generator -f flows.json > generated.yaml
```

生成出来的策略通常是过于宽松的（它是从"看到的"流量推，看不到的场景不会写），但作为起点很好用。我们内部用类似工具把"生成草稿"变成一个 PR，然后工程师在 PR 里人工收紧。

### 4.4 CI 里校验策略

我们给每个 CNP 加 CI 检查，确保：

1. 不包含 `toEntities: [world]` 和 `0.0.0.0/0`（除非明确批注）
2. 不包含 `endpointSelector: {}` 且 allow 全通的组合
3. 必须关联至少一个 Jira ticket（通过 annotation）

检查脚本用 OPA + `conftest`：

```rego
package cilium

deny[msg] {
  input.kind == "CiliumNetworkPolicy"
  input.spec.egress[_].toEntities[_] == "world"
  not startswith(input.metadata.annotations["security.example.com/exception"], "JIRA-")
  msg := sprintf("CNP %s allows egress to 'world' without Jira exception", [input.metadata.name])
}
```

## 五、Hubble UI + Prometheus 监控

Hubble UI 是一个流量拓扑可视化工具，对平时运维非常有帮助。部署后可以直接看到每个 namespace 的 service map：

```
   [checkout-service] ──200──▶ [payment-gateway]
         │
         └──403──▶ [order-service]    ← 被策略拒绝的调用
```

UI 里红色边就是被策略 drop 的流量。上线新策略后直接看红边是最快的 debug 方式。

### 5.1 关键 Prometheus 指标

```
# 总 drop 量
cilium_drop_count_total

# 按 reason 分
cilium_drop_count_total{reason="Policy denied"}

# 按策略
hubble_flows_processed_total{verdict="DROPPED"}

# FQDN 代理活跃度
cilium_fqdn_active_names
cilium_fqdn_active_ips

# policy map 容量（重要）
cilium_bpf_map_pressure{map_name="cilium_policy"}
```

**`cilium_bpf_map_pressure` 是高频事故指标**。Cilium 的 policy map 默认容量 16384 entry/endpoint，每条规则会占用多个 entry。规则太多、selector 太宽都会撑爆这个 map，表现是某些流量无法被策略覆盖。

解决办法是调整 BPF map 大小：

```yaml
bpf:
  policyMapMax: 65536
```

我们一个集群因为 FQDN 策略太多（200 多条 pattern），policy map pressure 到 85%，差一点就事故。后来把 FQDN 合并为少数 wildcard（比如 `*.amazonaws.com` 一条顶十条）才解决。

### 5.2 Grafana 仪表盘

Cilium 官方有一个 Grafana dashboard（ID 16613），包含 policy drop 率、L7 响应码分布、DNS 查询速率等。一定要部署上，并配告警：

```yaml
- alert: CiliumPolicyDropSpike
  expr: |
    sum(rate(cilium_drop_count_total{reason="Policy denied"}[5m])) by (namespace)
      > 100
  for: 5m
  annotations:
    summary: "{{ $labels.namespace }} 策略拒绝率突增"
```

## 六、多集群 ClusterMesh 下的策略

Cilium ClusterMesh 能把多个集群连成一个逻辑网络，Pod 可以直接按 `<service>.<ns>.svc.clusterset.local` 访问其他集群的服务。策略怎么写？

ClusterMesh 环境里的 CNP 可以指定对端集群：

```yaml
apiVersion: cilium.io/v2
kind: CiliumNetworkPolicy
metadata:
  name: cross-cluster-db-access
spec:
  endpointSelector:
    matchLabels:
      app: reporting
  egress:
    - toEndpoints:
        - matchLabels:
            app: datawarehouse
            io.cilium.k8s.policy.cluster: analytics-cluster
      toPorts:
        - ports: [{port: "5439", protocol: TCP}]
```

`io.cilium.k8s.policy.cluster: analytics-cluster` 这个特殊 label 是 ClusterMesh 为每个集群自动加的身份。通过它可以精确限定"只能访问 analytics 集群的 datawarehouse"。

**踩坑**：ClusterMesh 的 identity 同步有延迟，新建 Pod 后可能 1~2 秒才全局可见。新部署完的策略如果跨集群访问失败，先等一会再重试。

## 七、真实踩坑记录

### 7.1 DNS 策略把所有业务搞挂

2024 年我第一次给一个中型集群（200 Pod）上 DNS 策略，写得太严格：

```yaml
egress:
  - toEndpoints:
      - matchLabels: { k8s-app: kube-dns }
    toPorts:
      - ports: [{port: "53", protocol: UDP}]
        rules:
          dns:
            - matchName: "*.svc.cluster.local"   # 只允许集群内 DNS
```

上线后 10 分钟整个 payments 挂了。根因是 `*.svc.cluster.local` 这个 pattern 写错了——它只匹配**严格三段式**的域名（比如 `checkout.payments.svc.cluster.local`），但 DNS resolver 实际查询时会依次尝试：

```
checkout.payments.svc.cluster.local.
checkout.svc.cluster.local.
checkout.cluster.local.
checkout.example.internal.
checkout.
```

后面几次查询都被策略 drop 了，resolver 没等到任何一次成功就报错。修复办法：

```yaml
rules:
  dns:
    - matchPattern: "*"   # 先宽松
```

或者更精细：

```yaml
rules:
  dns:
    - matchPattern: "*.cluster.local"
    - matchPattern: "*.svc.cluster.local"
    - matchPattern: "*.example.internal"
```

**教训**：DNS 策略第一次上必须先 `matchPattern: "*"` 跑通，验证基础流量后再逐步收紧。

### 7.2 Istio sidecar 和 Cilium 策略冲突

Istio sidecar 劫持 Pod 的进出流量，Cilium 看到的"发起连接的源"是 sidecar 而不是应用本身。一些 L7 策略会因此失效。

解决方案 1：**关掉 sidecar 对 DNS 的劫持**（Istio 有配置选项）。
解决方案 2：**Cilium + Istio 的 mTLS 透传模式**（需要 Cilium 1.15+），Cilium 作为 CNI 层识别 sidecar 流量并做相应处理。
解决方案 3：**不要在 L7 层同时用 Cilium 和 Istio**。L4 用 Cilium，L7 用 Istio AuthorizationPolicy。

我们线上用的是方案 3，因为 Istio 已经做了很多 L7 治理。Cilium 在这种情况下主要负责"谁能连谁"的 L4 边界。

### 7.3 Network Policy 数量爆炸

一个大集群（1000+ Pod、几十个 namespace），如果每个 namespace 都写一套 CNP，再加每个服务的 L4 放行规则，数量可能上千。Cilium 的策略引擎理论上能支持几万条，但运维压力巨大：

- `kubectl get cnp -A` 输出一屏都不够
- 改动一条策略要 review 一大堆
- 新人完全没法上手

**我的方案**：
1. 把"基础设施基线"（CCNP）集中在一个 GitOps 仓库，专人维护
2. 业务 CNP 放在每个应用自己的 Helm chart 里，跟应用一起发布
3. 用 kustomize 的 `namePrefix` 和 `commonLabels` 自动加 team owner 标签
4. 定期运行脚本 audit：找出长期未触发 drop 的策略（可能已过期）

### 7.4 FQDN 策略的 TTL 不同步

Cilium 的 DNS 代理会缓存 DNS 响应的 TTL。如果你依赖的外部域名（比如 AWS S3）IP 变化频繁，可能出现**策略允许旧 IP、新 IP 被 drop** 的情况。

解决办法：
```yaml
bpf:
  policyMapMax: 65536
dnsProxy:
  minTTL: 3600   # 强制最低缓存 1 小时
  maxDeferredConnectionDeletes: 10000
```

`minTTL` 和 `maxDeferredConnectionDeletes` 配合能让 Cilium 更宽容地处理 IP 漂移，避免闪断。

### 7.5 跨 ns 选择器踩坑

很多人这么写：

```yaml
ingress:
  - fromEndpoints:
      - matchLabels:
          app: checkout
```

意图是"允许 checkout pod 访问"，但这个选择器**只匹配同 ns 的 checkout**。跨 ns 需要显式加 ns 标签：

```yaml
fromEndpoints:
  - matchLabels:
      app: checkout
      io.kubernetes.pod.namespace: payments
```

这是最常见的"策略没生效"问题，十有八九是这个。

## 八、性能与资源开销

我在生产环境观察的数据：

- **eBPF L3/L4 策略**：单节点 Cilium agent CPU 约 80~150m，内存 300~500Mi，策略规则数量对 CPU 影响很小
- **L7 代理（envoy）**：每节点额外 200~400m CPU + 200Mi 内存，启用 L7 的 Pod 流量会走代理，延迟增加约 200~500µs
- **FQDN 代理**：DNS 代理本身 CPU 开销很小（50~100m），但 FQDN pattern 数量多会影响 BPF map 压力

**优化建议**：

- L7 只对真正需要的流量启用（比如对外 API 边界）
- FQDN 策略优先合并 wildcard
- 定期清理"永远不匹配任何流量"的策略
- 高流量路径避免 L7（比如服务间内部调用）

## 九、落地路线

按我的经验，企业落地 Cilium 策略的路线应该是：

**阶段 1（1~2 个月）**：部署 Cilium + Hubble，观察模式运行，不写任何策略。教育团队看 Hubble UI 和 `hubble observe`。

**阶段 2（1 个月）**：上线 Layer 1 全局基线（deny metadata、deny kubelet、allow DNS），其他什么都不做。观察有没有误伤。

**阶段 3（2~3 个月）**：选一个非关键 namespace 做试点，上 default-deny + 业务放行，观察 drop 告警，迭代完善。

**阶段 4（3~6 个月）**：推广到所有 namespace，强制新 namespace 必须有 CNP 才能创建（用 Kyverno 或 OPA）。

**阶段 5（持续）**：对核心服务上 L7 和 FQDN 策略，收紧边界。定期 audit 和清理。

## 十、结语

网络策略不是一个"一次性完成"的项目，它是持续的工程实践。Cilium 提供的工具链（eBPF + Hubble + L7 代理）让这件事从"不可能"变成"可以做"，但真正做下来还是需要团队长期投入。

我的核心观点是：**别再假装你写了策略就是零信任**。零信任不是一套 YAML，它是一种"**默认不信任 + 持续验证 + 最小权限**"的工程文化。Cilium 是实现这种文化的网络层工具，Falco 是运行时层，SPIRE 是身份层，Sigstore 是制品层。四件事凑齐了，你才有资格说自己在搞零信任。

下一篇我会写 WireGuard 多云 mesh VPN，那是把零信任从数据中心内部延伸到办公网络和多云互联的必备方案。
