---
title: "Cilium Hubble 实战：用 eBPF 看透 Kubernetes 网络"
date: 2025-07-30T10:00:00+08:00
draft: false
tags: ["Cilium", "Hubble", "eBPF", "可观测性", "Kubernetes"]
categories: ["可观测性"]
description: "把 Cilium Hubble 从 CLI 玩具做成生产级网络可观测性平台的完整记录：架构、部署、Hubble Relay/UI、flow log、metrics、L7 可见性、和 Loki/Grafana 的联动、以及两次线上网络问题排查复盘。"
summary: "Cilium Hubble 是 Kubernetes 下最接近交换机镜像端口的东西。本文讲清楚它的架构、关键配置和生产上如何读 flow 定位网络问题。"
toc: true
math: false
diagram: false
keywords: ["Cilium", "Hubble", "eBPF", "网络可观测性", "flow log"]
params:
  reading_time: true
---

## 为什么我们需要 Hubble

传统网络可观测性在 K8s 里基本是瞎的。tcpdump 只能看到节点层，拿不到 pod label；VPC flow log 到不了容器级；Istio/envoy access log 只覆盖 mesh 内的 HTTP，对 TCP/UDP/gRPC 以外的协议就失灵。我们在一次排查中花了整整 6 小时，只为了回答一个问题：「是哪个命名空间的哪个 pod 在往 10.0.x.y:3306 发请求」。那次之后，我们决定把 Cilium 从 CNI 上移到 Hubble + eBPF 的网络观测平台方向。

Hubble 是 Cilium 的网络可观测性子项目。它利用 Cilium 已经嵌入到 socket、tc、cgroup 层的 eBPF 程序，把每一条 L3/L4 flow、L7 请求都抓成结构化事件，然后通过 Hubble Relay 聚合、Hubble UI 展示、Hubble Exporter 落地。

这篇文章记录的是我们把 Hubble 从「Cilium 自带的 CLI 工具」做成「生产级网络可观测性平台」的全过程，包括架构、部署、排障和踩坑。

## 一、Cilium + Hubble 的角色分工

先把几个组件搞清楚，否则后面配置会乱。

- **Cilium Agent**：DaemonSet，每个节点一个，负责 eBPF 程序的加载和 pod 网络配置。它是 Hubble 数据的源头。
- **Hubble Server**：不是独立组件，是 Cilium Agent 里内置的一个 gRPC server，监听 4244 端口（默认 unix socket）。它从 eBPF map 拉 flow 事件，对外 gRPC 暴露。
- **Hubble Relay**：独立 Deployment，聚合所有节点的 Hubble Server 流。Grafana、Hubble UI、`hubble` CLI 都是直接连 Relay。
- **Hubble UI**：独立 Deployment，Web 界面，用来可视化 service map 和 flow。
- **Hubble Metrics**：Cilium Agent 里内置，开 metrics 之后暴露 `hubble_flows_processed_total` 等 Prometheus 指标。
- **Hubble Exporter**（1.14+）：把 flow 以 JSONL 格式写到文件或转发到 Loki/OpenSearch。

数据流大概是：

```
eBPF program (kernel)
    │  ring buffer
    ▼
Cilium Agent (Hubble Server)
    │  gRPC
    ▼
Hubble Relay (所有节点)
    │
    ├──▶ Hubble CLI
    ├──▶ Hubble UI
    └──▶ Hubble Exporter ──▶ Loki / OpenSearch / Kafka
```

指标路径是独立的：Cilium Agent 把 Hubble metrics 暴露在 9965，Prometheus 直接 scrape。

## 二、一条 flow 是什么

eBPF 里每次 socket send/recv、TC 入站出站、kube-proxy 替代的 NAT 都会生成一个 flow 事件，字段大概这样：

```json
{
  "time": "2025-07-21T03:14:15.123Z",
  "verdict": "FORWARDED",
  "source": {
    "identity": 12345,
    "namespace": "payments",
    "pod_name": "payment-api-5f4-abc",
    "labels": ["app=payment-api", "env=prod"]
  },
  "destination": {
    "identity": 23456,
    "namespace": "data",
    "pod_name": "postgres-0",
    "labels": ["app=postgres"]
  },
  "IP": { "source": "10.1.2.3", "destination": "10.1.7.8" },
  "l4": { "TCP": { "source_port": 41234, "destination_port": 5432 } },
  "Type": "L3_L4",
  "traffic_direction": "EGRESS",
  "node_name": "ip-10-1-5-12.ec2.internal"
}
```

如果开了 L7 可见性，还会有 HTTP/gRPC/DNS 字段，比如：

```json
"l7": {
  "type": "Request",
  "http": {
    "method": "POST",
    "url": "http://order.svc/api/v1/pay",
    "protocol": "HTTP/1.1"
  }
}
```

L7 不是默认开的，需要配 `CiliumNetworkPolicy` 或 annotation。后面会讲。

## 三、部署：Cilium 配置要点

安装 Cilium 时要显式打开 Hubble 相关开关：

```bash
helm install cilium cilium/cilium \
  --namespace kube-system \
  --set kubeProxyReplacement=strict \
  --set k8sServiceHost=<api-server> \
  --set k8sServicePort=443 \
  --set hubble.enabled=true \
  --set hubble.relay.enabled=true \
  --set hubble.ui.enabled=true \
  --set hubble.metrics.enabled='{dns,drop,tcp,flow,icmp,http}' \
  --set hubble.metrics.enableOpenMetrics=true \
  --set hubble.tls.enabled=true \
  --set hubble.tls.auto.enabled=true \
  --set hubble.tls.auto.method=certmanager \
  --set hubble.tls.auto.certManagerIssuerRef.group=cert-manager.io \
  --set hubble.tls.auto.certManagerIssuerRef.kind=ClusterIssuer \
  --set hubble.tls.auto.certManagerIssuerRef.name=internal-ca \
  --set operator.replicas=2 \
  --set bpf.masquerade=true \
  --set ipam.mode=kubernetes
```

几个关键开关说明：

- **`kubeProxyReplacement=strict`**：让 Cilium 替代 kube-proxy 做 Service / NAT，这是 Hubble 能看到完整 Service-level flow 的前提。如果保留 iptables kube-proxy，你拿到的 flow 里 destination 可能是 ClusterIP 而不是后端 pod。
- **`hubble.metrics.enabled`**：选你要的 metric 类型。`flow` 是通用 L4 flow metric，`drop` 是丢包事件，`http` / `dns` 是 L7 metric。
- **`hubble.tls.auto.method=certmanager`**：Relay → Server 的 gRPC 通信默认强制 mTLS。生产上强烈建议走 cert-manager 管理 CA。
- **`bpf.masquerade=true`**：让 Cilium 在 eBPF 层做 SNAT，避免 iptables。

## 四、Hubble Relay 的几个坑

Relay 是数据聚合器，所有 `hubble observe` 请求都打到它。

### 坑 1：单 Relay 副本是瓶颈

默认 `hubble.relay.replicas=1`。在 200+ 节点的集群里，Relay 成为单点：它要同时维护 200 条到 Cilium Agent 的 gRPC stream，任何一次 GC 或重启都会断流 30~60 秒。

生产配置：

```yaml
hubble:
  relay:
    replicas: 3
    resources:
      requests:
        cpu: 500m
        memory: 512Mi
      limits:
        cpu: 2
        memory: 2Gi
    rollOutPods: true
  ui:
    replicas: 2
    backend:
      resources:
        requests:
          cpu: 200m
          memory: 256Mi
    frontend:
      resources:
        requests:
          cpu: 100m
          memory: 128Mi
```

### 坑 2：Relay 和 Server 之间 buffer 太小导致 flow drop

当 Relay 消费速度跟不上 Server 生产速度时，Cilium Agent 会丢事件。指标 `hubble_lost_events_total` 会涨。解决办法：

```yaml
hubble:
  eventBufferCapacity: 65535   # 默认 4095
  eventQueueSize: 0            # 0 表示按节点 CPU 动态
```

调到 65535 之后我们的 drop 率从 0.3% 降到 0.001%。

### 坑 3：TLS 证书 rotate 导致 Relay 断流

cert-manager 默认每 24h rotate 一次证书，Relay 的 gRPC 连接不会主动 reload TLS，需要等连接自然断开。我们加了一个 CronJob 每天 rotate 证书后强制重启 Relay。

## 五、Hubble Metrics：集成到 Prometheus

Cilium Agent 暴露的 Hubble metrics 示例：

```
hubble_flows_processed_total{protocol="TCP",verdict="FORWARDED",source_namespace="payments",destination_namespace="data"}
hubble_http_requests_total{method="POST",status="500",source_workload="order-api"}
hubble_dns_responses_total{rcode="NOERROR",qtypes="A"}
hubble_drop_total{reason="Policy denied",protocol="TCP"}
hubble_tcp_flags_total{flag="RST"}
```

Prometheus 加 scrape config：

```yaml
- job_name: 'cilium-agent-hubble'
  kubernetes_sd_configs:
    - role: pod
  relabel_configs:
    - source_labels: [__meta_kubernetes_pod_label_k8s_app]
      regex: cilium
      action: keep
    - source_labels: [__meta_kubernetes_pod_container_port_number]
      regex: "9965"
      action: keep
```

### 基于 metric 的关键告警

```yaml
- alert: HubbleHighDropRate
  expr: |
    sum by(source_namespace, reason) (rate(hubble_drop_total[5m]))
    / sum by(source_namespace) (rate(hubble_flows_processed_total[5m])) > 0.01
  for: 5m
  annotations:
    summary: "{{ $labels.source_namespace }} 丢包率超过 1%，原因 {{ $labels.reason }}"

- alert: HubbleHTTP5xxSpike
  expr: |
    sum by(source_workload, destination_workload) (rate(hubble_http_requests_total{status=~"5.."}[5m])) > 10
  for: 3m

- alert: CiliumEventsLost
  expr: rate(hubble_lost_events_total[5m]) > 100
  for: 10m
```

## 六、L7 可见性：HTTP/gRPC 要单独启用

Cilium 默认只抓 L3/L4 flow。要让 Hubble 看到 HTTP method、URL、status，需要让这条流经过 Cilium 的 L7 proxy（Envoy）。启用方式是给 pod 配 CiliumNetworkPolicy 显式匹配 L7：

```yaml
apiVersion: cilium.io/v2
kind: CiliumNetworkPolicy
metadata:
  name: payments-l7-visibility
  namespace: payments
spec:
  endpointSelector:
    matchLabels:
      app: payment-api
  ingress:
    - toPorts:
        - ports:
            - port: "8080"
              protocol: TCP
          rules:
            http:
              - {}
```

空的 http 规则意味着「允许所有 HTTP，但我想观测它们」。Cilium 检测到这条规则就会把流量路由到 Envoy 做 L7 解析，解析后的字段进入 Hubble flow。

**注意代价**：过 Envoy 的 L7 路径性能损耗大约 10%~20% latency、30%~50% CPU。不要对高吞吐的 sidecar-free 服务全量开，只对业务 API 的入口开。

另一种方式是用 annotation：

```yaml
io.cilium.proxy-visibility: "<Ingress/8080/TCP/HTTP>,<Egress/53/UDP/DNS>"
```

annotation 方式更轻量，不需要完整的 CNP。

## 七、Hubble Exporter：把 flow 落到 Loki

CLI 的 `hubble observe` 只能看最近几分钟。要长期存储，用 Hubble Exporter 把 flow 序列化到文件：

```yaml
hubble:
  export:
    static:
      enabled: true
      filePath: /var/run/cilium/hubble/events.log
      fileMaxSizeMb: 50
      fileMaxBackups: 5
      fieldMask: []
      allowList: []
      denyList:
        - '{"source_pod":"kube-system/coredns*"}'
        - '{"destination_port":"10250"}'
```

这个配置让每个 Cilium Agent 在本地写一个 events.log，然后我们用 Promtail 或 Vector 收到 Loki：

```yaml
# promtail
scrape_configs:
  - job_name: cilium-hubble
    static_configs:
      - targets: [localhost]
        labels:
          job: cilium-hubble
          __path__: /var/run/cilium/hubble/events.log
    pipeline_stages:
      - json:
          expressions:
            source_ns: source.namespace
            dest_ns: destination.namespace
            verdict: verdict
            l4_protocol: l4.TCP.destination_port
      - labels:
          source_ns:
          dest_ns:
          verdict:
```

这样在 Loki 里就能直接查「某命名空间的 dropped flow」：

```logql
{job="cilium-hubble", source_ns="payments", verdict="DROPPED"}
```

**denyList 非常重要**。不过滤的话，一个中等集群每天产生几十亿 flow，写到 Loki 的成本会比业务日志还高。我们实际只保留：

- 所有 `verdict=DROPPED` 的 flow；
- 所有 L7 HTTP error（`http.status >= 500`）；
- 关键命名空间（payments、data）的 ingress/egress；
- 业务以外的都丢。

## 八、Service Map：一张全集群的 L7 拓扑

Hubble UI 根据 flow 动态生成 service map，相当于 Kiali 对 Istio 做的事，但对象是整个集群而非 mesh。

```
hubble observe --namespace payments --follow --output compact
```

但 UI 更好用。关键点：

- **service map 是实时的**，不是存储的拓扑。后端存 2 分钟窗口数据。
- **node 筛选**：按 namespace、pod、workload、label 过滤；
- **点一条 edge 看流量明细**，包括 L4/L7 字段。

我们的做法：不直接给业务团队开放 Hubble UI（太重），而是每天凌晨跑一个 `hubble observe --since 24h --output json` 脚本，生成集群级流量图表上传到内网。

## 九、案例一：用 Hubble 定位「偶发连接重置」

时间：2025 年 6 月。现象：一个支付服务每小时会有 5~10 次 `connection reset by peer`，业务 retry 能兜住，但有告警。

**排查路径**：

1. 开 `hubble observe --namespace payments --pod payment-api --follow --type drop` 看是否有 drop；
2. 没有 drop，但有 `RST` flag 的 TCP flow。改查 `tcp_flags_total{flag="RST"}`；
3. 指标上 RST 确实和业务事件对应。进一步 `hubble observe --since 10m --protocol tcp --tcp-flags RST --output json | jq` 看源；
4. 发现 RST 来自 `kube-system/node-local-dns` pod 上的某个 port，一看端口是 DNS；
5. 反推到业务：connection reset 不是 payment-api 本身问题，而是业务代码里有个 DNS 查询用 TCP socket（少见），node-local-dns 重启时 RST 了 TCP 连接；
6. 让业务改用 UDP DNS，问题消失。

没有 Hubble 的话，这种 RST 的源头几乎无法定位。tcpdump 看到 RST 但不知道哪个 pod。

## 十、案例二：DNS 解析失败连锁反应

时间：2025 年 10 月。现象：多个业务 service 5xx 告警同时爆发，但只持续 90 秒，恢复后找不到原因。

**排查**：

1. Hubble metric `hubble_dns_responses_total{rcode="SERVFAIL"}` 在事故时间窗口涨到 3000/s；
2. `hubble observe --protocol udp --destination-port 53 --since 1h` 看 DNS query 的源；
3. 发现 DNS 服务端 pod（kube-dns）有一个在事故时段被 Karpenter 缩掉了，但 Service endpoint 没及时更新；
4. 部分业务 DNS cache miss 时命中了已被删除的 kube-dns pod，SERVFAIL；
5. 根因：Karpenter drain 的 terminationGracePeriodSeconds 配太短，kube-dns pod 还没被 Service endpoint 摘掉就被杀了。

改进措施：

- Karpenter 的 node termination handler 配 endpoint propagation delay；
- kube-dns pod 加 preStop sleep 10s；
- 上线 node-local-dns cache 降低对 upstream kube-dns 的直接依赖；
- Hubble 告警加 `dns_responses_total{rcode!="NOERROR"}` > 1% 的阈值。

## 十一、性能开销：心里有个数

在一个 100 节点、3000 pod 的集群里：

- Cilium Agent CPU：每节点平均 0.2~0.5 vCPU
- Cilium Agent 内存：每节点 500MB~1.5GB
- Hubble Relay：3 副本 * 0.5~1.5 vCPU, 500MB~1GB
- Hubble UI：2 副本 * 0.2 vCPU, 256MB

开 L7 visibility 之后对应 pod 的 latency 会涨 5%~15%，视流量特征。

对象存储侧（flow 落到 Loki）：按前面的 denyList 策略，每天大约 30~80GB 原始日志，压缩后 5~15GB。

## 十二、上线 checklist

1. Cilium 版本 1.14+（新 Hubble 特性大多在 1.14/1.15/1.16）；
2. kube-proxy replacement 开启；
3. Hubble Relay 副本 3；
4. Hubble metrics 接 Prometheus，告警规则就位；
5. Hubble Exporter 接 Loki 或 OpenSearch，denyList 配好；
6. 关键业务命名空间开 L7 visibility；
7. Hubble UI 走 SSO，不直接暴露；
8. Cilium TLS 证书用 cert-manager 自动 rotate；
9. 监控 `hubble_lost_events_total` 和 `cilium_bpf_map_ops_total`；
10. 定期跑 `cilium-cli status` 做健康检查。

## 十三、和其他工具的对比

- **vs Istio + Kiali**：Kiali 只看 mesh 内流量，Hubble 看全集群包括非 mesh；Kiali 对 L7 HTTP 更丰富（有 trace 集成），Hubble 覆盖更广；两者不冲突，可以并存。
- **vs Calico Flow logs**：Calico 也有 flow log，但没有 Hubble Relay 这种聚合层，查询体验差；Cilium 的 eBPF 数据源更丰富。
- **vs 传统 VPC Flow Log**：VPC flow log 只有 5-tuple，没有 pod / namespace 元数据，要用 IP 反查 pod。Hubble 直接带 label。

## 十四、常见问题

1. **Hubble 看不到 pod 间流量**：检查 kube-proxy replacement 是否开启，以及 Service 是否用 ClusterIP。
2. **HTTP metric 都是空的**：L7 visibility 没开，或业务没走 Envoy 路径。
3. **hubble observe 报 connection refused**：Relay 没装、或 port-forward 没做；`hubble` CLI 默认连 Relay，也可以直连 Agent unix socket。
4. **flow 时间戳偏移**：节点 NTP 不同步；Cilium 会用 CLOCK_BOOTTIME，相对时间不影响。
5. **Hubble UI 特别卡**：一般是 flow 量太大，Relay 压力高，建议加过滤条件浏览。

## 十五、写在最后

eBPF 让 Kubernetes 网络第一次变得「可见」。Hubble 是目前最成熟的路径：你不需要手动写 eBPF 程序，只要把 Cilium 作为 CNI，就能免费拿到节点到 pod、L4 到 L7 的所有可见性。代价是部分 feature 需要 CPU 和内存，但在可观测性的投资回报里，它是最划算的几项之一。

我们在经历过几次「tcpdump 摸瞎 6 小时」的事故之后，再也不想没有它。如果你现在还在用 iptables kube-proxy + VPC flow log，我建议你严肃地考虑一次 Cilium 升级。哪怕只用 Hubble 这一个子项目，回报已经超过投入。

## 参考资料

- Cilium 官方文档 Hubble 与 Metrics 章节
- Isovalent Blog - Hubble Exporter 和 L7 Visibility
- Cilium GitHub release notes 1.14 ~ 1.16
- eBPF Summit 2024/2025 Talks: Hubble at scale
