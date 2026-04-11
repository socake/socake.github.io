---
title: "从 Nginx Ingress 迁移到 Traefik：为什么换，怎么换"
date: 2026-04-11T10:00:00+08:00
draft: false
tags: ["Traefik", "Kubernetes", "Ingress", "网络", "DevOps"]
categories: ["Kubernetes"]
description: "记录一次将生产集群从 Nginx Ingress Controller 迁移到 Traefik 的完整过程，包括迁移动机、核心概念对比、Helm 部署配置、IngressRoute 示例以及实际踩坑记录。"
summary: "从实际痛点出发，讲清楚 Traefik 和 Nginx Ingress 的本质区别，给出可直接参考的迁移路径和配置示例。"
toc: true
math: false
diagram: false
keywords: ["Traefik", "Nginx Ingress", "Kubernetes Ingress", "IngressRoute", "迁移", "Middleware"]
params:
  reading_time: true
---

## 为什么要换

在我们的集群稳定运行了大约一年之后，Nginx Ingress Controller 开始成为一个越来越明显的瓶颈点。不是它不好用，而是我们遇到了几个具体问题，让维护成本持续上升。

**第一个问题：配置 reload 导致的抖动**

Nginx 本身是静态配置模型。每当有新的 Ingress 资源被创建或修改，Nginx Ingress Controller 就需要重新生成 `nginx.conf` 并触发 reload。在服务部署频繁的环境里（我们的 CI/CD 每天会产生几十次 Deployment 滚动更新），这个 reload 会造成短暂的连接中断。虽然 Nginx 的 reload 已经做了平滑处理（`-s reload` 会等待老 worker 处理完当前连接再退出），但在高并发下依然偶发 502。

更麻烦的是，upstream 的健康变化（比如某个 Pod 刚启动还没 ready）和配置 reload 是两条独立的路径，有时候会产生竞态。

**第二个问题：复杂路由配置写起来很别扭**

Nginx Ingress 通过 annotation 来扩展路由能力，比如：

```yaml
nginx.ingress.kubernetes.io/rewrite-target: /$2
nginx.ingress.kubernetes.io/configuration-snippet: |
  more_set_headers "X-Request-ID: $request_id";
```

这种做法的问题是：annotation 没有类型约束，字符串里藏着 nginx 配置片段，既不利于 lint，也不利于 GitOps 下的代码审查。当路由规则变复杂（比如按 Header 路由、A/B 测试），annotation 的可读性会迅速崩塌。

**第三个问题：缺少原生的流量控制能力**

限流、熔断、基础认证这些能力，在 Nginx Ingress 里要么靠 annotation 嵌入 nginx.conf 片段，要么额外部署 sidecar，没有一个统一的抽象层。Traefik 的 Middleware 机制很好地解决了这个问题。

---

## Traefik 核心概念：用类比讲清楚

Traefik 的流量流转路径是：**EntryPoint → Router → Middleware → Service**。

可以把它类比到熟悉的概念上：

| Traefik | 类比 Nginx | 说明 |
|---|---|---|
| EntryPoint | `listen 80` / `listen 443` | 监听端口，定义流量入口 |
| Router | `server` + `location` 块 | 根据规则匹配请求，决定交给谁处理 |
| Middleware | `limit_req` / `auth_basic` 等 | 在请求到达 Service 前做处理 |
| Service | `upstream` | 后端真正的服务地址（对应 K8s Service）|

Traefik 最大的不同在于：它是**动态配置**的。当 K8s 里的 Ingress 或 IngressRoute 资源发生变化，Traefik 会实时感知并更新路由规则，不需要 reload 进程。这得益于它对 K8s API 的原生 Watch 机制。

---

## 用 Helm 安装 Traefik

推荐用 Helm 安装，官方 chart 维护很活跃。

```bash
helm repo add traefik https://traefik.github.io/charts
helm repo update
```

核心 `values.yaml` 配置如下，这是我们生产环境实际使用的精简版：

```yaml
# values.yaml
deployment:
  replicas: 2

# 开放端口
ports:
  web:
    port: 8000
    expose:
      default: true
    exposedPort: 80
    redirectTo:
      port: websecure
  websecure:
    port: 8443
    expose:
      default: true
    exposedPort: 443
    tls:
      enabled: true

# Service 类型
service:
  type: LoadBalancer
  annotations:
    service.beta.kubernetes.io/aws-load-balancer-type: "nlb"
    service.beta.kubernetes.io/aws-load-balancer-scheme: "internet-facing"

# Prometheus metrics
metrics:
  prometheus:
    enabled: true
    entryPoint: metrics
    addEntryPointsLabels: true
    addRoutersLabels: true
    addServicesLabels: true

# Dashboard（生产环境不要直接暴露，见后面的安全加固）
ingressRoute:
  dashboard:
    enabled: false

# 日志
logs:
  general:
    level: INFO
  access:
    enabled: true
    format: json

# 资源限制
resources:
  requests:
    cpu: "100m"
    memory: "128Mi"
  limits:
    cpu: "500m"
    memory: "512Mi"

# 让 Traefik 能监听所有 namespace 的 Ingress
providers:
  kubernetesCRD:
    enabled: true
    allowCrossNamespace: true
  kubernetesIngress:
    enabled: true
    allowExternalNameServices: true
```

安装：

```bash
helm upgrade --install traefik traefik/traefik \
  --namespace traefik \
  --create-namespace \
  -f values.yaml
```

---

## IngressRoute CRD：对比标准 Ingress

Traefik 支持两种路由定义方式：标准的 `Ingress` 资源（兼容模式）和它自己的 `IngressRoute` CRD。

**标准 Ingress 写法（兼容，但能力受限）：**

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: my-app
  annotations:
    traefik.ingress.kubernetes.io/router.middlewares: default-rate-limit@kubernetescrd
spec:
  rules:
  - host: app.example.com
    http:
      paths:
      - path: /api
        pathType: Prefix
        backend:
          service:
            name: my-app-svc
            port:
              number: 8080
```

**IngressRoute CRD 写法（推荐，表达能力更强）：**

```yaml
apiVersion: traefik.io/v1alpha1
kind: IngressRoute
metadata:
  name: my-app
  namespace: default
spec:
  entryPoints:
    - websecure
  routes:
    - match: Host(`app.example.com`) && PathPrefix(`/api`)
      kind: Rule
      services:
        - name: my-app-svc
          port: 8080
      middlewares:
        - name: rate-limit
        - name: basic-auth
    # 按 Header 路由：金丝雀发布
    - match: Host(`app.example.com`) && HeaderRegexp(`X-Canary`, `^true$`)
      kind: Rule
      priority: 10  # 优先级更高，先匹配
      services:
        - name: my-app-canary-svc
          port: 8080
  tls:
    certResolver: letsencrypt
```

IngressRoute 的路由规则是用 Traefik 自定义的 DSL 写的，支持的匹配条件包括：`Host`、`PathPrefix`、`Path`、`Headers`、`HeaderRegexp`、`Query`、`Method` 等，可以用 `&&` 和 `||` 组合。

---

## Middleware：限流和基础认证示例

Middleware 是 Traefik 的核心扩展点，作为独立的 CRD 资源定义，可以在多个路由间复用。

**限流 Middleware：**

```yaml
apiVersion: traefik.io/v1alpha1
kind: Middleware
metadata:
  name: rate-limit
  namespace: default
spec:
  rateLimit:
    average: 100    # 每秒平均请求数
    burst: 50       # 允许的突发量
    period: 1s
    sourceCriterion:
      ipStrategy:
        depth: 1    # 从 X-Forwarded-For 取第一个 IP
```

**基础认证 Middleware：**

密码需要用 `htpasswd` 格式生成，然后存入 Secret：

```bash
# 生成密码
htpasswd -nb admin yourpassword
# 输出：admin:$apr1$xxxxx$yyyyyyy
```

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: basic-auth-secret
  namespace: default
type: Opaque
stringData:
  users: "admin:$apr1$xxxxx$yyyyyyy"
---
apiVersion: traefik.io/v1alpha1
kind: Middleware
metadata:
  name: basic-auth
  namespace: default
spec:
  basicAuth:
    secret: basic-auth-secret
    removeHeader: true  # 认证通过后从请求中移除 Authorization header
```

**路径重写 Middleware（等价于 Nginx 的 rewrite）：**

```yaml
apiVersion: traefik.io/v1alpha1
kind: Middleware
metadata:
  name: strip-prefix
  namespace: default
spec:
  stripPrefix:
    prefixes:
      - /api/v1
    forceSlash: false
```

---

## 踩坑记录

### IngressRoute 和 Ingress 混用时的问题

我们迁移期间同时存在两种资源，遇到了一个诡异的问题：明明已经创建了 IngressRoute，但流量还是走的 Ingress。

原因：Traefik 处理路由匹配时，两种资源生成的路由在同一个优先级下，`Ingress` 资源由于没有显式 `priority` 字段，默认优先级由路由规则的字符串长度决定。

解决方法：在 IngressRoute 里显式设置较高的 `priority`，或者彻底清理掉对应的 Ingress 资源，不要让两者同时存在于同一个 host。

### Traefik Dashboard 生产环境安全加固

Traefik 的 Dashboard 默认在 8080 端口以 HTTP 方式暴露，不能直接通过 LoadBalancer 对外。正确做法是用 IngressRoute + Middleware 来保护它：

```yaml
apiVersion: traefik.io/v1alpha1
kind: IngressRoute
metadata:
  name: traefik-dashboard
  namespace: traefik
spec:
  entryPoints:
    - websecure
  routes:
    - match: Host(`traefik.internal.example.com`) && (PathPrefix(`/dashboard`) || PathPrefix(`/api`))
      kind: Rule
      services:
        - name: api@internal   # Traefik 内置 service，指向 dashboard
          kind: TraefikService
      middlewares:
        - name: basic-auth
          namespace: traefik
  tls:
    secretName: internal-tls-cert
```

同时在 DNS 层把 `traefik.internal.example.com` 解析限制在内网，加双重保险。

### CRD 版本不兼容

升级 Traefik 大版本时，CRD 的 API 版本可能会变（比如从 `traefik.containo.us/v1alpha1` 到 `traefik.io/v1alpha1`）。Helm upgrade 不会自动更新已安装的 CRD，需要手动执行：

```bash
kubectl apply -f https://raw.githubusercontent.com/traefik/traefik-helm-chart/master/traefik/crds/ingressroute.yaml
```

---

## 迁移建议：逐服务切流

不要一次性把所有 Ingress 迁到 IngressRoute。正确的节奏是：

1. **先并行运行**：Traefik 和 Nginx Ingress 同时运行，各自管理不同的 Service，通过不同的 LoadBalancer IP 对外服务。
2. **低风险服务先迁**：选内部管理后台、监控页面这类流量小、容错高的服务率先切到 Traefik。
3. **验证完整再迁核心服务**：跑一周没问题，再把核心 API 切过来。
4. **清理旧资源**：确认 Traefik 接管后，才删掉对应的 Ingress 资源和 Nginx Ingress Controller。

迁移完成后，我们集群的 Ingress reload 抖动彻底消失了，复杂路由（按 Header 的灰度发布）的配置也从一堆 annotation 变成了可读的 YAML，维护体验好了不少。
