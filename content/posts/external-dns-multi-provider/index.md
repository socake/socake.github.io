---
title: "ExternalDNS 多云 DNS 同步实战：从 Route53 到 Cloudflare 再到阿里云 DNS"
date: 2025-02-22T09:45:00+08:00
draft: false
tags: ["ExternalDNS", "Kubernetes", "DNS", "Route53", "Cloudflare"]
categories: ["云原生"]
description: "ExternalDNS 0.15/0.16 在多个 Kubernetes 集群里把 Service / Ingress / Gateway 自动同步到 Route53、Cloudflare、阿里云 DNS、AWS PrivateHostedZone 的完整实战：策略选择、owner/registry、domainFilter、冲突排查、TTL 管理、以及跨集群同 zone 共享的正确做法。"
summary: "手工在 Cloudflare 控制台点 DNS 记录这件事，随着集群和业务增长最终必然崩溃。ExternalDNS 就是把 Kubernetes 资源当 source-of-truth、DNS provider 当执行器的一个 controller。但真要用好，你得理解 txtOwnerId、policy、provider 各自的限制以及跨集群共享 zone 的几个坑。"
toc: true
math: false
diagram: false
keywords: ["ExternalDNS", "Route53", "Cloudflare", "AliDNS", "Kubernetes DNS", "txt registry", "domainFilter"]
params:
  reading_time: true
---

## 为什么一定要用 ExternalDNS

在我们的环境里，有 5 个 Kubernetes 集群（US prod / CN prod / US qa / US pre / CN pre），20+ 个对外域名、上百个子域。如果没有 ExternalDNS，你会遇到：

- 每次发新服务要发一个工单给 DNS 管理员，平均响应时间 4 小时；
- 有个 ingress 改了 host 没通知 DNS 管理员，访问 404 找半天；
- 某条 A 记录指向已被销毁的 EC2 IP，半年没人发现；
- 测试环境域名和生产域名写到一起，某次调试误删了生产 A 记录。

ExternalDNS 是 SIG 维护的 Kubernetes-sigs 项目，它的核心非常简单：watch Service / Ingress / Gateway 资源，把 hostname 同步到你指定的 DNS 提供商。但生产上要用对，得理清几个概念。

## 核心概念：source、provider、registry、policy

这四个词是 ExternalDNS 的"四原色"，理解了再看配置就很直观。

### source

ExternalDNS 可以从哪些资源里读 DNS 信息：

- `service`：LoadBalancer Service 的 `spec.externalIPs`、`status.loadBalancer.ingress`；
- `ingress`：Ingress 资源的 `spec.rules[].host` 和 `status.loadBalancer`；
- `gateway-httproute` / `gateway-grpcroute` / `gateway-tlsroute`：Gateway API 的 Route 资源；
- `istio-virtualservice`：Istio VirtualService 的 hosts；
- `crd`：自定义 CRD（比如 DNSEndpoint）；
- `node`：节点（不常用，自建场景可能用）。

生产用得最多的是 `service` + `ingress`。从 Gateway API 迁移的话，加上 `gateway-httproute`。

### provider

对接的 DNS 服务商。常用的：
- `aws`（Route53）
- `cloudflare`
- `google`（Cloud DNS）
- `azure`（Azure DNS）
- `alibabacloud`（阿里云 DNS）
- `rfc2136`（自建 BIND/PowerDNS）
- `inmemory`（测试用）

一个 ExternalDNS 实例只能跑一个 provider。多 provider 要跑多个实例。

### registry：最容易被忽略的重点

ExternalDNS 怎么知道"这条记录是我刚才创建的"？答案是 registry。支持的 registry：

- `txt`：默认方式，给每条 DNS 记录附带一条 TXT 记录作为所有权标记。TXT 里写的是 `"heritage=external-dns,external-dns/owner=<ownerId>,external-dns/resource=ingress/default/my-ingress"`。
- `aws-sd`：AWS Cloud Map 专用。
- `noop`：不标记，危险（删的时候可能误删手工记录）。

**生产一律用 txt registry**，别懒。一定要设 `--txt-owner-id`，这是 ExternalDNS 最核心的安全开关。

### policy

同步策略：

- `sync`（默认）：完全托管，ExternalDNS 发现不匹配就会改 / 删；
- `upsert-only`：只创建和更新，不删。

生产场景怎么选？

- 如果这个 zone 只有 Kubernetes 在写：用 `sync`；
- 如果这个 zone 还有人手工维护记录：用 `upsert-only`。

我们的做法：**每个环境分离 zone**。生产环境有独立 zone，业务 zone 不共用手工记录。这样所有的 zone 都可以 `sync`。能避免的共享就不要共享。

## 一个完整的 Route53 部署示例

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: external-dns
  namespace: external-dns
spec:
  replicas: 1
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app: external-dns
  template:
    metadata:
      labels:
        app: external-dns
    spec:
      serviceAccountName: external-dns
      containers:
        - name: external-dns
          image: registry.k8s.io/external-dns/external-dns:v0.16.x
          args:
            - --source=service
            - --source=ingress
            - --source=gateway-httproute
            - --provider=aws
            - --aws-zone-type=public
            - --registry=txt
            - --txt-owner-id=us-prod-cluster
            - --txt-prefix=edns-
            - --domain-filter=example.com
            - --policy=sync
            - --interval=1m
            - --log-level=info
            - --aws-batch-change-size=200
            - --events
          resources:
            requests:
              cpu: 50m
              memory: 128Mi
            limits:
              memory: 256Mi
```

ServiceAccount 走 IRSA：

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: external-dns
  namespace: external-dns
  annotations:
    eks.amazonaws.com/role-arn: arn:aws:iam::123456789012:role/external-dns
```

IAM 策略（最小权限）：

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "route53:ChangeResourceRecordSets"
      ],
      "Resource": [
        "arn:aws:route53:::hostedzone/Z1234567890ABC"
      ]
    },
    {
      "Effect": "Allow",
      "Action": [
        "route53:ListHostedZones",
        "route53:ListResourceRecordSets",
        "route53:ListTagsForResource"
      ],
      "Resource": ["*"]
    }
  ]
}
```

关键参数的详细解释：

**`--replicas=1` + `strategy.Recreate`**：ExternalDNS 不支持 leader election（在一些老版本里是能开的实验特性，但不建议上生产）。多副本会产生冲突写，一副本就够了，用 Recreate 策略确保滚动时没两个实例同时存在。

**`--txt-owner-id`**：**生产最关键的参数**。每个 ExternalDNS 实例必须有独一无二的 owner id。推荐命名：`<env>-<cluster>`，比如 `us-prod`、`cn-qa`。这个 id 会被写进 TXT 记录，其他 ExternalDNS 实例看到这个 TXT 就知道"这条记录不是我的，不要碰"。

**`--txt-prefix=edns-`**：默认 TXT 记录和 A 记录同名（比如 A 记录是 `app.example.com`，TXT 也是 `app.example.com`），会和 CNAME 冲突（Route53 规定同一个名字不能同时有 CNAME 和 TXT）。加一个 prefix 会让 TXT 变成 `edns-app.example.com`，和主记录分开。**这个参数生产几乎是必配的**。

**`--domain-filter=example.com`**：ExternalDNS 只会管这个 domain 下的记录。可以多次传来支持多个 domain。没设 domain-filter 的话 ExternalDNS 会尝试管理所有 hosted zone，一出事就是大事。

**`--interval=1m`**：轮询间隔。别改小到 30s 以下，DNS provider API 都有限流。生产 1-3 分钟都可以接受。

**`--aws-batch-change-size=200`**：Route53 允许一次 batch 200 条 change 的改动。默认是 1000，对大集群没问题，但 Route53 一次 batch 不能超过 1000，也不能超过 32000 characters。大集群上我们经常调到 200 避免一次性改动被拒。

**`--events`**：开启 Kubernetes Event，ExternalDNS 每次创建 / 更新 / 删除记录会在对应的 Service/Ingress 上打事件。排障神器。

## Cloudflare 的特殊性

Cloudflare provider 的配置：

```yaml
args:
  - --provider=cloudflare
  - --cloudflare-proxied=false
  - --cloudflare-dns-records-per-page=5000
env:
  - name: CF_API_TOKEN
    valueFrom:
      secretKeyRef:
        name: cloudflare-api-token
        key: api-token
```

**`--cloudflare-proxied`**：控制 Cloudflare 代理（那朵橙色云）是开是关。默认 false。生产上建议显式设，且大多数情况设 false：

- 如果走代理，源站 IP 被隐藏、DDoS 防护生效，但你的 TCP health check 会看到 Cloudflare 的 IP 段；
- 如果不走代理，只是 DNS 解析，没有任何 Cloudflare 特性。

想针对单个 Ingress 控制代理开关，用 annotation：

```yaml
metadata:
  annotations:
    external-dns.alpha.kubernetes.io/cloudflare-proxied: "true"
```

**Cloudflare 的 Batch API**：新版 ExternalDNS 用 Cloudflare 的 Batch DNS Records API 批量提交变更，而不是一条一条打 API。这个在记录数多时对 API 限流友好很多。如果你用的是 0.15 之前的版本，升级到 0.16 能显著缓解 API 限流问题。

**API Token 权限**：只给 `Zone:DNS:Edit` 和 `Zone:Zone:Read`，并且限定到具体 zone，不要给 account 级权限。Cloudflare 的 Token 粒度很细，没有理由给大权限。

## 阿里云 DNS：国内场景的常用组合

阿里云 DNS 的 provider 叫 `alibabacloud`。配置：

```yaml
args:
  - --provider=alibabacloud
  - --alibaba-cloud-zone-type=public
  - --alibaba-cloud-config-file=/etc/kubernetes/alibaba-cloud.json
volumeMounts:
  - name: alibaba-cloud-config
    mountPath: /etc/kubernetes
volumes:
  - name: alibaba-cloud-config
    secret:
      secretName: alibaba-cloud-credentials
```

credential 格式：

```json
{
  "regionId": "cn-hangzhou",
  "accessKeyId": "LTAI...",
  "accessKeySecret": "..."
}
```

注意点：

1. 阿里云 DNS 对 RAM 权限有单独的 action，只需 `AliyunDNSFullAccess` 的子集，生产环境最好写个自定义策略限死到具体 domain，不过阿里云 DNS 的 action 粒度没 AWS 那么细。
2. 阿里云 DNS 的生效延迟比 Route53 / Cloudflare 大不少，几分钟是常态。别小看这一点，会影响 cert-manager 的 DNS01 流程。
3. 一些阿里云 DNS 的云解析企业版才支持按照 ACL 限制的私有解析，普通版只有公共解析。

## AWS Private Hosted Zone：内部 DNS

`--aws-zone-type=private` 让 ExternalDNS 管理 Route53 Private Hosted Zone。这是我们 VPC 内部域名自动化的主力。

```yaml
args:
  - --provider=aws
  - --aws-zone-type=private
  - --domain-filter=internal.example.com
  - --registry=txt
  - --txt-owner-id=us-prod-internal
  - --txt-prefix=edns-
  - --policy=sync
```

几个重点：

1. Private zone 的 owner id 要和 public 的区分开，否则 ExternalDNS 两个实例会打架。
2. Private zone 的 annotation 可以跟 public 分开：

   ```yaml
   metadata:
     annotations:
       external-dns.alpha.kubernetes.io/hostname: "api.internal.example.com"
       external-dns.alpha.kubernetes.io/aws-weight: "50"
   ```

3. Private zone 会关联多个 VPC，跨 VPC 访问要单独建 resolver rule。这是 AWS 的事，ExternalDNS 不管。

## 跨集群共享同一个 zone

这是一个你早晚会撞到的问题：两个集群的 ingress 都想在同一个 zone 里写记录。比如 us-prod 和 cn-prod 都在 `example.com` 下写 `api.us.example.com` 和 `api.cn.example.com`。

**正确做法**：

1. 两个集群的 ExternalDNS **必须有不同的 txt-owner-id**；
2. 最好用 `--domain-filter` 或者 `--annotation-filter` 把两边各自管的范围再收窄一层；
3. 两边都用 `sync` policy，因为 owner id 隔离已经保证了安全；
4. 仔细想好 "如果两个集群不小心声明了同一个 hostname 会怎么样"——先到先得，后面那个会不断重试但不会覆盖前面的。

**错误做法**（都踩过）：

- 两个集群用同一个 owner id。等于两边在抢同一条记录，每一分钟都在打架，API 限流分分钟触发。
- 不设 owner id，用默认。等于两边都能改，且没 TXT 标记，删除策略会误删对方的记录。
- 不设 domain filter，两边管了整个 hosted zone。一出事就是全 zone 的事。

## annotation 大全：业务层最该知道的

ExternalDNS 从 Service / Ingress 上读 annotation 控制行为。常用的：

**指定 hostname**：

```yaml
external-dns.alpha.kubernetes.io/hostname: "api.example.com,www.example.com"
```

- 多个 hostname 用逗号分隔；
- 对于 Service 必须设这个（Service 没有 host 字段）；
- 对于 Ingress，如果 annotation 和 `spec.rules[].host` 都有，annotation 优先。

**TTL**：

```yaml
external-dns.alpha.kubernetes.io/ttl: "60"
```

单位秒。默认 300。对灾备切换频繁的场景（比如蓝绿部署前后），我们会临时降到 60。不建议长期低 TTL，DNS provider 的解析次数会上升影响费用（Route53 的计费和 query 次数相关）。

**Target 覆盖**：

```yaml
external-dns.alpha.kubernetes.io/target: "192.168.1.1,10.0.0.1"
```

强制指定 DNS 记录的 target，而不是从 Service 的 externalIP 读。典型场景：LoadBalancer 的 IP 由某个 NLB 固定、我们想手动指向一个 VIP。

**排除某个 Service**：

```yaml
external-dns.alpha.kubernetes.io/exclude: "true"
```

让 ExternalDNS 不管这个资源。

**access ACL / 按 annotation 过滤**：

在 ExternalDNS 启动参数加：

```
--annotation-filter=external-dns.alpha.kubernetes.io/enable=true
```

然后只有显式加了 `external-dns.alpha.kubernetes.io/enable: "true"` 的 Service/Ingress 才会被管理。**这是我在所有生产环境里的默认做法**——默认"不管"，业务显式 opt-in 才管。避免某个 dev 同事随手写了个 host 就上了 DNS。

## domain-filter 的几个细节

domain-filter 的行为是"**允许列表**"：只同步匹配的域名。

- `--domain-filter=example.com`：会匹配 `example.com`、`app.example.com`、`x.y.example.com`；
- 可以多次传入：`--domain-filter=example.com --domain-filter=example.net`；
- 如果有一个 hostname 不匹配任何 domain-filter，ExternalDNS 直接跳过。

配合 `--exclude-domains`：

- `--exclude-domains=internal.example.com`：在 domain-filter 匹配之后再排除一批。

生产建议：

- 如果一个集群只管一个 zone，只设 `--domain-filter`；
- 如果一个集群管多个 zone，设多次 `--domain-filter`，不要直接不设；
- 不设 domain-filter 是作死行为。

## TTL 规划

我们的默认策略：

- 内部服务：300s；
- 对外服务：60s。

对外服务的 TTL 设低一点是因为 DR/failover 场景下需要快速切换。低 TTL 的代价是 DNS query 次数上升，Route53 按 query 计费，我们实际测下来 60s TTL 对一个中等规模服务的月度费用影响不超过 10 美元。

特殊场景：
- 给某个域名配 Latency/Weighted/Failover routing policy 时，TTL 尽量短（30s 左右），否则切换不生效；
- `_service._proto.domain` 这种 SRV 记录 TTL 可以长到 3600，变动少。

## ExternalDNS 和 cert-manager 的协作

ExternalDNS 和 cert-manager 是 Kubernetes 里的"DNS 双子星"，但它们之间其实**没有耦合**，也没有 race。

- cert-manager 的 DNS01 是直接调 DNS provider API 写 TXT；
- ExternalDNS 只同步 Service/Ingress 的 A/CNAME；
- 两者写的 TXT 记录是不同的，也不会互相覆盖。

但有个小坑：如果你用 ExternalDNS 的 txt registry（会写 TXT 记录），和 cert-manager 的 `_acme-challenge` TXT 记录都在同一个 zone，一定要确保 TXT 记录名不冲突。默认情况下两者写的 TXT 名称不同，不会互相影响，但是如果你改过 txt-prefix 或者用了奇怪的 hostname 格式，需要多检查一眼。

## 监控和告警

ExternalDNS 有 Prometheus 指标：

- `external_dns_controller_last_sync_timestamp_seconds`：最后一次同步成功的时间戳；
- `external_dns_source_endpoints_total`：source 收集到多少个 endpoint；
- `external_dns_registry_endpoints_total`：registry 里有多少条记录；
- `external_dns_source_errors_total`、`external_dns_registry_errors_total`：错误计数。

最核心的告警：

```yaml
- alert: ExternalDNSNotSyncing
  expr: |
    time() - external_dns_controller_last_sync_timestamp_seconds > 600
  for: 10m
  labels:
    severity: critical
  annotations:
    summary: "ExternalDNS 超过 10 分钟没有成功同步"

- alert: ExternalDNSErrors
  expr: |
    increase(external_dns_source_errors_total[10m]) > 0
    or increase(external_dns_registry_errors_total[10m]) > 0
  for: 5m
  labels:
    severity: warning
  annotations:
    summary: "ExternalDNS 出现错误"
```

另外强烈建议给生产 zone 再开一个"外部探针告警"——用 Blackbox Exporter 定期 dig 你关心的那几个核心域名，对比预期 IP，一旦解析异常立刻告警。ExternalDNS 的内部监控只能告诉你"它活着"，不能告诉你"结果对"。

## 排障的经典问题

### 问题 1：记录没同步，但 ExternalDNS 没报错

大概率是 domain-filter 不匹配。加 `--log-level=debug` 重启一下，日志里会看到 "skipping endpoint ... not matching domain filter"。

### 问题 2：记录被 ExternalDNS 反复删除再创建

大概率是 source 里有多个东西声明同一个 hostname，ExternalDNS 周期性地 reconcile 到不同的 target。检查是不是 Service 和 Ingress 都加了相同的 hostname annotation。

### 问题 3：CNAME 和 TXT 冲突

症状：日志里出现 "InvalidChangeBatch: ... RRSet of type CNAME with DNS name ... is not permitted because a conflicting RRSet of type TXT"。

原因：Route53 不允许同名下同时有 CNAME 和其他类型记录，TXT registry 默认就是同名 TXT。

解决：加 `--txt-prefix=edns-`。

### 问题 4：记录删掉了，但 TXT 还在

ExternalDNS 删除是两步：先删 A/CNAME 再删 TXT。如果中间崩溃，TXT 会残留。下一次 reconcile 看到"有 TXT 但没对应的主记录"会当成 orphan TXT 处理。但如果你改了 owner-id，ExternalDNS 会认为这条 TXT 不是自己的，不会删。这种情况只能手动清理或者先临时改回旧 owner-id 让它自清。

### 问题 5：删除 Ingress 但 DNS 记录留下

可能是 policy 设成了 `upsert-only`。upsert-only 不删除。改 `sync` 即可。

## Gateway API：新范式下的 ExternalDNS

Kubernetes Gateway API 稳定之后，ExternalDNS 支持直接从 HTTPRoute / GRPCRoute / TLSRoute 里读 hostname。启用方式：

```yaml
args:
  - --source=gateway-httproute
  - --source=gateway-grpcroute
  - --source=gateway-tlsroute
```

注意：

1. `gateway-httproute` 只会读 HTTPRoute 上声明的 hostnames，**不会自动读 Gateway 的 hostnames**。这和 Ingress 很不一样，Ingress source 是直接读 Ingress 上的 rules。如果你想让 Gateway 的 hostnames 也同步，要显式加 `--gateway-name=<name>` 或者在 HTTPRoute 上写上相同的 hostname。
2. 一个 Gateway 可能被多个 HTTPRoute 引用，它们加起来的 hostname 并集就是最终的 DNS 记录集合。
3. HTTPRoute 的 `status.parents[].conditions` 里如果没有 `Accepted=True`，ExternalDNS 会跳过这个 route。确保你的 Gateway Controller（Istio / Contour / Envoy Gateway）正确设置了 status。

## 安全：别让 ExternalDNS 成为攻击面

几个必须做的：

1. **RBAC 收紧**：ExternalDNS 只需要 `get/list/watch` Service / Ingress / HTTPRoute，不需要 write。别用 cluster-admin。
2. **用 annotation-filter 做 opt-in**：默认不管，业务主动打 annotation 才管。
3. **IAM/API Token 限权**：只给具体 zone 的写权限，别给 account 级。
4. **不要暴露 metrics 端口**：ExternalDNS 的 metrics 里能看到所有 hostname，等于把你的内部拓扑公开。metrics 只暴露给 Prometheus 集群内部访问。
5. **不跑在 public subnet 的 node 上**：没意义，只会增加攻击面。

## 最后的一些原则

- 一个 ExternalDNS 实例管一个 provider、一个或一组 zone；
- 多环境一定不同 owner id；
- domain-filter 必设；
- annotation-filter 做 opt-in；
- txt-prefix 避免 CNAME 冲突；
- 监控 last_sync_timestamp，外加外部探针；
- TTL 分对内对外；
- 不要让 ExternalDNS 和手工改记录共存。

ExternalDNS 是那种"装好之后再也没人想起它，一旦出问题就地动山摇"的组件。前期把 owner id、filter、RBAC 捋清楚，后面几乎不会出事。反之，任何一条懒省事都会在下一次事故里连本带利还给你。
