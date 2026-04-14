---
title: "cert-manager 生产级实战：从 Let's Encrypt 到企业内网 PKI 的完整路线"
date: 2025-02-15T14:30:00+08:00
draft: false
tags: ["cert-manager", "Kubernetes", "TLS", "ACME", "Let's Encrypt"]
categories: ["云原生"]
description: "cert-manager 1.20 的完整生产实战：ClusterIssuer 设计、HTTP01 vs DNS01 的选型、通配符证书、多云 DNS provider、证书续期监控、内网私有 CA 对接、Gateway API 证书发放、ACME 限额、CA 注入、以及跨集群证书分发。"
summary: "cert-manager 几乎是每个 Kubernetes 集群的标配，但真正跑到生产的团队都会遇到：Let's Encrypt 限流被打爆、通配符证书续期失败、内部服务想要私有 CA、Istio / Gateway API 的证书怎么发。这篇把一年里我在 5 个集群上做 cert-manager 运维踩过的坑写成一份实操手册。"
toc: true
math: false
diagram: false
keywords: ["cert-manager", "Let's Encrypt", "ACME", "ClusterIssuer", "DNS01", "HTTP01", "Gateway API", "Kubernetes TLS"]
params:
  reading_time: true
---

## 写在前面

cert-manager 到 1.20 这几个版本已经很成熟了，但它的复杂度是"看起来简单用起来扎手"的典型。装起来 10 分钟，配起来一天，查问题一整周。这篇文章只讲生产里会遇到的事情：

- ClusterIssuer vs Issuer 怎么分
- HTTP01 什么时候靠谱，DNS01 什么时候必须用
- 通配符证书不可能用 HTTP01
- Let's Encrypt 限额 / staging 环境的正确姿势
- 多云 / 多 DNS 提供商的 solver 组合
- 证书续期失败怎么排查
- 怎么发给 Istio / Gateway API
- 内网私有 CA 的正确接法
- 跨 namespace / 跨集群证书分发

不写原理章节。有时间的话我会另写一篇讲 ACME 协议本身。

## 版本和兼容性

截至 2026 年 4 月，cert-manager 的稳定版本是 1.20.x 系列。它对 Kubernetes 版本的要求相当宽松，但官方只保证 "最近 N 个" 的版本支持。生产建议：

- Kubernetes ≥ 1.28
- cert-manager ≥ 1.19，强烈推荐 1.20.x
- 如果你还在 1.12/1.13，先升级再往下看，老版本的 ACME 行为和新版 Let's Encrypt 的速率限制有些坑已经修了

**不要用 kubectl apply 方式装**。历史原因，早期文档推荐 `kubectl apply -f cert-manager.yaml` 这种方式，它的坑是：

1. CRD 和 Deployment 在同一个 yaml 里，删的时候一不小心把 CRD 一起删了，集群里所有证书资源瞬间消失；
2. 没法管理 values，参数调整要 patch；
3. 升级路径混乱。

**生产用 Helm**：

```bash
helm repo add jetstack https://charts.jetstack.io
helm repo update
helm upgrade --install cert-manager jetstack/cert-manager \
  --namespace cert-manager --create-namespace \
  --version v1.20.x \
  --set crds.enabled=true \
  --set global.leaderElection.namespace=cert-manager \
  --set prometheus.enabled=true \
  --set webhook.timeoutSeconds=30 \
  --set dns01RecursiveNameservers="1.1.1.1:53,8.8.8.8:53" \
  --set dns01RecursiveNameserversOnly=true
```

几个值得解释的参数：

- **crds.enabled=true**：Helm 自带装 CRD，好处是生命周期跟 Helm release 绑定；坏处是 `helm uninstall` 会删 CRD，所以这参数生产慎重改。我一般设 true 首装，之后用 `helm upgrade --set crds.keep=true` 保平安。
- **webhook.timeoutSeconds=30**：默认 10，生产一定要调大。webhook 超时是 cert-manager 最常见的故障原因，k8s 某些场景下 apiserver 调 webhook 的延迟能到 15 秒。
- **dns01RecursiveNameservers**：覆盖容器内的 DNS 递归查询服务器。这是一个 extremely 重要的参数，我稍后详细讲 DNS01 时会再提。
- **prometheus.enabled=true**：装监控指标。

## ClusterIssuer vs Issuer

这两个 CRD 的区别只有一个词：**作用域**。

- `Issuer` 是 namespace 级别，只能签发同 namespace 里的 Certificate；
- `ClusterIssuer` 是集群级别，所有 namespace 都能用。

生产原则：

**一律用 ClusterIssuer，除非你有明确的理由不用**。

理由是：
- 多 namespace 复用，不用每个 namespace 装一份；
- 认证凭据（比如 Cloudflare Token）放在 cert-manager 自己的 namespace 里，不和业务 namespace 混；
- 审计更清晰，谁能改 ClusterIssuer 一般只有 platform 团队，权限好收。

什么时候用 Issuer：
- 多租户集群，不同租户用不同 Vault / 私有 CA；
- 合规要求："业务 A 的证书不能和业务 B 用同一套凭据"。

## ACME ClusterIssuer：Let's Encrypt 示例

一个最常见的生产 ClusterIssuer：

```yaml
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt-prod
spec:
  acme:
    server: https://acme-v02.api.letsencrypt.org/directory
    email: ops@example.com
    privateKeySecretRef:
      name: letsencrypt-prod-key
    solvers:
      - selector:
          dnsZones:
            - example.com
        dns01:
          cloudflare:
            email: ops@example.com
            apiTokenSecretRef:
              name: cloudflare-api-token
              key: api-token
      - http01:
          ingress:
            class: nginx
```

注意：

1. **staging 环境一定要先过一遍**。Let's Encrypt 的 production API 有严格限流（每周每个注册域名最多 50 张新证书，failed validation 每小时 5 次），你一次搞错配置能把域名锁一周。先用 `https://acme-staging-v02.api.letsencrypt.org/directory` 验证通过，再换 prod。
2. **privateKeySecretRef.name** 是 ACME account key 的 secret，不是证书本身。每个 ClusterIssuer 一个 account key，别复用。
3. **solvers 是一个数组**，cert-manager 会按 selector 匹配选用。上面的配置意思是：`example.com` 及其子域走 Cloudflare DNS01；其他域走 nginx HTTP01。
4. **email 必须是可达的邮箱**，Let's Encrypt 在证书快过期时会发信。

## HTTP01 vs DNS01：选型决定一切

这是 cert-manager 最核心的选型决策。

### HTTP01 的工作原理

ACME 服务器（比如 Let's Encrypt）会访问 `http://<domain>/.well-known/acme-challenge/<token>`，读取里面的值验证你对这个域名有控制权。cert-manager 的 HTTP01 solver 会自动创建一个临时 Pod + Service + Ingress 来响应这个请求。

**HTTP01 的硬限制**：

1. 不能签通配符证书。ACME 通配符只接受 DNS01。
2. 必须 80 端口对公网可达。如果你的 ingress 只开 443，HTTP01 永远过不了。
3. 多集群共用一个域名时很难搞，因为同一时刻只能一个集群响应 challenge。
4. 内部服务不能用，ACME 服务器访问不到的都不行。

### DNS01 的工作原理

cert-manager 通过 DNS provider API（Cloudflare / Route53 / AliDNS 等）在域名下创建一条 `_acme-challenge.<domain>` TXT 记录，ACME 服务器去查这条记录验证。

**DNS01 的优势**：

1. 支持通配符（`*.example.com`）；
2. 完全不依赖你的服务是否对公网开放；
3. 多集群共用域名完全没问题，每个集群各自申请各自的证书。

**DNS01 的硬伤**：

1. 需要把 DNS provider 的凭据放进集群，权限管理要小心；
2. 依赖 provider 的 API 稳定性和生效速度（某些国内 DNS 生效延迟大到 cert-manager 都超时）；
3. **递归 DNS 服务器的配置极其重要**。

### 生产原则

**能用 DNS01 就用 DNS01**，不管你的域名是不是通配符。原因：

- 少一个对外暴露路径；
- 跨集群复用方便；
- 不会被 ingress 配置改动牵连；
- 将来换用 Gateway API 不用重新搞。

HTTP01 只在"我实在拿不到 DNS API 权限"的情况下用。

## dns01RecursiveNameservers：这个参数救过我很多次

cert-manager 执行 DNS01 时会先"自检" ——先查一遍 `_acme-challenge` 这条记录是不是真的写上去了，再去告诉 ACME 服务器"你来验吧"。问题来了：cert-manager Pod 用的是集群内 DNS（CoreDNS），CoreDNS 默认上游是 node 的 DNS。

几个典型坑：

1. **CoreDNS 有缓存**。你刚写的 TXT 记录，CoreDNS 里还是 NXDOMAIN，cert-manager 自检失败，一直重试。
2. **公司内网 DNS 不递归查询外部域**。比如你的公司 DNS 只解析 `*.internal`，访问 `example.com` 要跳出去，结果 CoreDNS 查到了 NXDOMAIN。
3. **split horizon DNS**。内部 DNS 给 `example.com` 返回内网 IP，外部查返回公网，TXT 记录写的是公网那边，cert-manager 看到的是内部返回结果。

解决办法就是把 dns01 的查询绕开集群内 DNS，直接走公共 DNS：

```bash
--set dns01RecursiveNameservers="1.1.1.1:53,8.8.8.8:53"
--set dns01RecursiveNameserversOnly=true
```

`dns01RecursiveNameserversOnly=true` 意味着"只用这些 nameserver，不要 fallback"。这是必须的，fallback 上去你就又回到了坑里。

## DNS01 的 provider：生产常见配置

### Cloudflare

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: cloudflare-api-token
  namespace: cert-manager
type: Opaque
stringData:
  api-token: "your-token-with-Zone:DNS:Edit-permission"
---
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt-cf
spec:
  acme:
    server: https://acme-v02.api.letsencrypt.org/directory
    email: ops@example.com
    privateKeySecretRef:
      name: letsencrypt-cf-key
    solvers:
      - dns01:
          cloudflare:
            apiTokenSecretRef:
              name: cloudflare-api-token
              key: api-token
```

权限：用 API Token 不要用 Global API Key。Token 至少需要对目标 zone 的 `Zone:DNS:Edit`，zone list 至少 `Zone:Zone:Read`。

### Route53 (AWS)

Route53 的推荐方式是 IRSA（IAM Roles for Service Accounts），不要在集群里存 AK/SK。

```yaml
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt-r53
spec:
  acme:
    server: https://acme-v02.api.letsencrypt.org/directory
    email: ops@example.com
    privateKeySecretRef:
      name: letsencrypt-r53-key
    solvers:
      - dns01:
          route53:
            region: us-west-2
            hostedZoneID: Z1234567890ABC
```

ServiceAccount 的 annotation：

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: cert-manager
  namespace: cert-manager
  annotations:
    eks.amazonaws.com/role-arn: arn:aws:iam::123456789012:role/cert-manager
```

IAM 策略最小权限：

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "route53:GetChange",
      "Resource": "arn:aws:route53:::change/*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "route53:ChangeResourceRecordSets",
        "route53:ListResourceRecordSets"
      ],
      "Resource": "arn:aws:route53:::hostedzone/Z1234567890ABC"
    },
    {
      "Effect": "Allow",
      "Action": "route53:ListHostedZonesByName",
      "Resource": "*"
    }
  ]
}
```

**重要**：IAM 信任策略里一定要限制 `StringEquals` 里的 ServiceAccount，千万别用 `*`，否则谁都能假冒你的 cert-manager。

```json
"Condition": {
  "StringEquals": {
    "oidc.eks.us-west-2.amazonaws.com/id/XXXX:sub": "system:serviceaccount:cert-manager:cert-manager",
    "oidc.eks.us-west-2.amazonaws.com/id/XXXX:aud": "sts.amazonaws.com"
  }
}
```

### AliDNS（阿里云）

官方没有原生 provider，社区用 webhook 的方式：

```bash
helm install cert-manager-webhook-alidns \
  cert-manager-webhook-alidns/cert-manager-webhook-alidns \
  --namespace cert-manager
```

然后 ClusterIssuer：

```yaml
spec:
  acme:
    solvers:
      - dns01:
          webhook:
            groupName: acme.example.com
            solverName: alidns-solver
            config:
              region: cn-hangzhou
              accessKeyIDRef:
                name: alidns-secret
                key: access-key-id
              accessKeySecretRef:
                name: alidns-secret
                key: access-key-secret
```

这块的坑：阿里云 DNS 生效延迟有时候大到几分钟，cert-manager 默认的 `propagationTimeoutSeconds` 顶不住。改 webhook 的配置把它调到 300 以上。

## Certificate 资源：字段每个都重要

Certificate 是你直接告诉 cert-manager "我要这张证书" 的 CRD。

```yaml
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: example-com-tls
  namespace: default
spec:
  secretName: example-com-tls
  issuerRef:
    name: letsencrypt-prod
    kind: ClusterIssuer
  commonName: example.com
  dnsNames:
    - example.com
    - www.example.com
    - "*.example.com"
  duration: 2160h      # 90d
  renewBefore: 360h    # 提前 15d 续期
  privateKey:
    algorithm: ECDSA
    size: 256
    rotationPolicy: Always
  usages:
    - server auth
    - client auth
  revisionHistoryLimit: 3
```

几个重点字段：

**duration / renewBefore**：默认的证书有效期由 ACME 服务器决定，Let's Encrypt 是 90 天。`duration` 是你"期望"的有效期，cert-manager 会告诉 ACME，但最终是否尊重看 CA。`renewBefore` 决定提前多久续期，Let's Encrypt 的 90 天证书强烈建议 `renewBefore: 720h`（30 天）以上，给失败留足重试窗口。

**rotationPolicy**：`Always` 表示每次续期都换新私钥，`Never` 表示保留老私钥。生产场景：

- 对外服务一律 `Always`，私钥不复用是基本安全要求；
- 有些特殊场景（私钥要 pin 住，比如 HPKP 之类）用 `Never`，但这些场景本身已经很罕见。

**privateKey.algorithm**：`RSA` 或 `ECDSA`。ECDSA 256 足够强、体积小、握手快。Let's Encrypt 目前两种都支持。用 ECDSA 还有一个附加好处，某些老设备不支持 ECDSA，可以当"筛选器"用。

**revisionHistoryLimit**：cert-manager 会把历次 CertificateRequest 留下来方便排查。生产 3 够了，100 会让 etcd 很难看。

## Ingress 上的 annotation：最省心的方式

如果你不想显式写 Certificate，对 Ingress 加 annotation 就行，cert-manager 自动创建：

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: example-com
  annotations:
    cert-manager.io/cluster-issuer: "letsencrypt-prod"
    cert-manager.io/common-name: "example.com"
    cert-manager.io/duration: "2160h"
    cert-manager.io/renew-before: "720h"
    cert-manager.io/revision-history-limit: "3"
spec:
  tls:
    - hosts:
        - example.com
        - www.example.com
      secretName: example-com-tls
  rules:
    - host: example.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: example
                port:
                  number: 80
```

cert-manager 的 ingress-shim controller 会读 `tls.hosts`，自动创建 Certificate。这条路径最省心，但缺点是"显式性"差，有时候你排障时找不到 Certificate 在哪里。

**我的建议**：平台内部服务用 annotation 省心；对外关键业务用显式 Certificate，配置文件版本化，谁改过一清二楚。

## Gateway API：新的正规路线

Kubernetes 1.29 之后 Gateway API 是 stable 的，cert-manager 从 1.15 开始对它有一等支持。和 Ingress annotation 完全一致：

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
metadata:
  name: example-gw
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt-prod
spec:
  gatewayClassName: istio
  listeners:
    - name: https
      port: 443
      protocol: HTTPS
      hostname: "*.example.com"
      tls:
        mode: Terminate
        certificateRefs:
          - name: example-com-wildcard-tls
            kind: Secret
```

cert-manager 看到这个 Gateway 会自动创建对应的 Certificate。如果你还在 Ingress，趁现在切 Gateway API 正合适。

## 监控 cert-manager

cert-manager 的 Prometheus 指标是运维的命脉。几个必须看的：

- `certmanager_certificate_ready_status`：每张 Certificate 的 ready 状态（True/False/Unknown）；
- `certmanager_certificate_expiration_timestamp_seconds`：每张 Certificate 的到期时间戳；
- `certmanager_http_acme_client_request_count`：ACME API 调用计数，看有没有打到 Let's Encrypt 限流；
- `certmanager_clock_time_seconds`：cert-manager 自己看到的时间，确认 Pod 时钟没飘。

核心告警规则：

```yaml
groups:
  - name: cert-manager
    rules:
      - alert: CertificateExpiringSoon
        expr: |
          (certmanager_certificate_expiration_timestamp_seconds - time()) < 14*86400
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "证书 {{ $labels.namespace }}/{{ $labels.name }} 将在 14 天内过期"

      - alert: CertificateNotReady
        expr: |
          certmanager_certificate_ready_status{condition="True"} == 0
        for: 1h
        labels:
          severity: critical
        annotations:
          summary: "证书 {{ $labels.namespace }}/{{ $labels.name }} 不处于 Ready 状态"

      - alert: CertManagerAcmeAccountError
        expr: |
          rate(certmanager_http_acme_client_request_count{status=~"4.."}[15m]) > 0
        for: 15m
        labels:
          severity: warning
        annotations:
          summary: "cert-manager 调用 ACME API 出现 4xx，可能是限流或配置错误"
```

第二条告警 for 时间给 1h，因为 cert-manager 重试是有 backoff 的，短时间 NotReady 属于正常波动。

## 证书续期失败：排障 checklist

我整理的顺序，通常前 3 条就能解决 90% 的问题：

1. **看 Certificate 的 status.conditions**：
   ```bash
   kubectl -n default describe certificate example-com-tls
   ```
   重点看 `Events` 和 `Status.Conditions`，里面有 cert-manager 最后一次 reconcile 的错误。

2. **看 CertificateRequest**：
   ```bash
   kubectl -n default get cr
   kubectl -n default describe cr example-com-tls-xyz
   ```
   CertificateRequest 是单次签发的记录，每次续期会产生新的 CR。90% 的错误信息在这里。

3. **看 Order 和 Challenge**：
   ```bash
   kubectl -n default get orders.acme.cert-manager.io
   kubectl -n default describe challenge example-com-tls-xyz-123
   ```
   DNS01 失败时 Challenge 里会有非常清晰的信息："expected txt record ... but got ..."。

4. **看 cert-manager 自己的日志**：
   ```bash
   kubectl -n cert-manager logs -l app.kubernetes.io/name=cert-manager --tail=200 | grep -i error
   ```
   有些 webhook / solver 的错误只在 Pod 日志里。

5. **手动验证 DNS 生效**：
   ```bash
   dig +short TXT _acme-challenge.example.com @1.1.1.1
   ```
   要用公共 DNS 查，不要用你本机的 DNS。记住 cert-manager 也是这么查的（如果你设了 dns01RecursiveNameservers）。

## 私有 CA：内部服务的正确姿势

内部服务（比如 `app.internal`）不能用 Let's Encrypt，因为域名不公开。方案有三种：

### 方案 1：self-signed + CA Certificate

最简单粗暴，适合 lab：

```yaml
apiVersion: cert-manager.io/v1
kind: Issuer
metadata:
  name: selfsigned-bootstrap
spec:
  selfSigned: {}
---
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: my-ca
spec:
  isCA: true
  commonName: my-ca
  secretName: my-ca-secret
  privateKey:
    algorithm: ECDSA
    size: 256
  issuerRef:
    name: selfsigned-bootstrap
    kind: Issuer
---
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: my-ca-issuer
spec:
  ca:
    secretName: my-ca-secret
```

之后内部证书都走 `my-ca-issuer`。问题：所有客户端都要信任 my-ca，分发难。

### 方案 2：Vault PKI

生产推荐。HashiCorp Vault 的 PKI secret engine 做根 CA，cert-manager 用 Vault issuer 签发。优点：CA 私钥在 Vault 里，不会被带出集群；访问审计完备。

```yaml
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: vault-issuer
spec:
  vault:
    server: https://vault.example.com
    path: pki/sign/kubernetes
    auth:
      kubernetes:
        role: cert-manager
        mountPath: /v1/auth/kubernetes
        serviceAccountRef:
          name: cert-manager
```

Vault 端需要配 Kubernetes auth method 和 PKI role。具体配置别在这里展开，Vault 那边的文档有比较标准的 cert-manager 集成指南。

### 方案 3：AWS Private CA / 阿里云私有 CA

云厂商的私有 CA 服务，cert-manager 有官方 external issuer。成本高（AWS Private CA 每月几百美金），但适合对 CA 生命周期管理有硬性合规需求的团队。一般中型团队用 Vault 就够了。

## 跨 namespace 和跨集群证书分发

### 跨 namespace：reflector / Secret replicator

cert-manager 签发的 Secret 只在一个 namespace。如果多个 namespace 的 Ingress 要用同一张证书（比如通配符证书），有几种方案：

1. **每个 namespace 各自签一张**：最干净但最费 ACME 配额。通配符证书没必要这么干。
2. **reflector**（`emberstack/kubernetes-reflector`）：给源 Secret 加 annotation，reflector 自动同步到目标 namespace。生产够用。
3. **手动 kubectl get | apply**：别这么干。

我们线上用 reflector，示例：

```yaml
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: wildcard-example-com
  namespace: cert-manager
spec:
  secretName: wildcard-example-com-tls
  secretTemplate:
    annotations:
      reflector.v1.k8s.emberstack.com/reflection-allowed: "true"
      reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces: "prod-.*,staging-.*"
      reflector.v1.k8s.emberstack.com/reflection-auto-enabled: "true"
      reflector.v1.k8s.emberstack.com/reflection-auto-namespaces: "prod-.*,staging-.*"
  dnsNames:
    - "*.example.com"
    - "example.com"
  issuerRef:
    name: letsencrypt-prod
    kind: ClusterIssuer
```

Certificate 写在 `cert-manager` namespace，reflector 自动把 Secret 同步到所有 prod-/staging- 开头的 namespace。

### 跨集群：一个集群签、其他集群用

五个集群都去申请同一个通配符证书是浪费。我们的做法：

1. 一个"证书主集群"上跑 cert-manager + Certificate，签出通配符证书；
2. 通过 External Secrets Operator 从证书主集群读 Secret，推到其他集群的对应 namespace；
3. 或者用 GitOps，把 Secret 脱敏后写进 Vault，各集群从 Vault 拉。

Method 2 的步骤大致是：

- 证书主集群：cert-manager 签证书，写到 K8s Secret；
- cluster-secret-sync（或 ESO 的 PushSecret 功能）把这个 Secret 推到一个中心 Vault 路径；
- 其他集群的 ESO 从 Vault 拉这个路径，生成本地 Secret。

这么做的好处是 ACME 配额只用 1 份，证书和私钥的传输过程全程加密，审计清晰。

## ACME 限额：不要自己打自己

Let's Encrypt 的限额里，生产最常撞的是：

- **每个注册域名每周 50 张证书**：不是每次续期，是"新的证书"。你一直续期同一张是不算的，但每次改 dnsNames 就算新证书。
- **每小时 5 次 failed validation**：调试阶段一不小心就撞到。
- **每个 IP 每 3 小时 10 个 account**：一般撞不到，除非你在 CI 里狂 helm install。

防撞办法：

1. **debug 永远在 staging**；
2. **dnsNames 稳定**，不要动不动加减子域名。需要新增就走新的 Certificate 资源；
3. **Helm 测试用 self-signed Issuer**，别拿 Let's Encrypt 做烟雾测试；
4. 监控里看 `certmanager_http_acme_client_request_count`。

## 几个不算 FAQ 的 FAQ

**Q: cert-manager 能不能续期手动上传的证书？**
A: 不能。cert-manager 只管它自己签发的。手动证书建议要么全都交给 cert-manager，要么用 Vault 管。

**Q: ECDSA 证书有兼容性问题吗？**
A: 目前主流浏览器和客户端都支持。一些内部的 Java 老应用（JDK 7 以下）可能有问题，内部系统确认一下。

**Q: cert-manager 停机会不会影响已签发的证书？**
A: 不会。已经签好的 Secret 静静地躺在 etcd 里，Ingress 用着不会有任何影响。cert-manager 停机只影响新签发和续期。所以 cert-manager 挂掉不紧急，但到期前一定要恢复。

**Q: 证书 Ready 但是访问还是用的老证书？**
A: 看 Ingress Controller 是不是 reload 了。nginx-ingress 默认是热加载 Secret 的，但某些老版本有 bug。`kubectl -n ingress-nginx rollout restart deployment/ingress-nginx-controller`。

## 最后一张 checklist

生产 cert-manager 安装完之后，我会对照下面这张表一项项确认：

- [ ] Helm 装的，版本 ≥ 1.19
- [ ] CRD 装了且 `crds.keep=true`
- [ ] webhook.timeoutSeconds ≥ 30
- [ ] dns01RecursiveNameservers 设了公共 DNS
- [ ] 至少有一个 staging ClusterIssuer，一个 prod ClusterIssuer
- [ ] 每个 ClusterIssuer 有独立的 account key secret
- [ ] ServiceAccount 用 IRSA / Workload Identity，不塞 AK/SK
- [ ] 启用 Prometheus 指标 + 配到期告警 + Ready 告警
- [ ] revisionHistoryLimit 设合理
- [ ] 有一个通配符证书的生产跑通用例
- [ ] 测试过 cert-manager Pod 重启后的行为
- [ ] 测试过续期流程（手动 `cmctl renew`）

把这张 checklist 打印出来贴墙上。cert-manager 不是你每天都会碰的组件，但每次碰的时候一般都是证书快过期、老板在群里催。有备无患。
