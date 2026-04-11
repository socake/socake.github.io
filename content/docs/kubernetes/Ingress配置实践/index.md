---
title: "Kubernetes Ingress 配置实践"
date: 2025-12-09T11:00:00+08:00
draft: false
tags: ["Kubernetes", "Ingress", "Nginx", "TLS", "运维"]
categories: ["Kubernetes"]
description: "Kubernetes Ingress 完整配置指南，覆盖控制器选型、路由配置、TLS 证书管理、常用 annotations、灰度发布及故障排查。"
summary: "从 Ingress 概念到生产实践：nginx/traefik/ALB 选型对比、TLS 自动签发、canary 灰度发布、限速超时等常用 annotations 详解。"
toc: true
math: false
diagram: false
keywords: ["Kubernetes", "Ingress", "ingress-nginx", "cert-manager", "canary", "TLS", "annotations"]
params:
  reading_time: true
---

## 为什么需要 Ingress

**没有 Ingress 时的困境：**

- 每个服务要对外暴露就得用 `Service type: LoadBalancer`，会消耗大量云负载均衡器资源，成本高
- 无法基于 Host / Path 进行路由，所有流量都是 L4 级别
- TLS 终止需要在每个服务单独处理
- 无法统一做限速、认证、监控

**Ingress 做了什么：**

```
外部请求
   ↓
LoadBalancer（只需要一个）
   ↓
Ingress Controller（nginx/traefik 等）
   ↓  基于 Host、Path 路由
Service A / Service B / Service C
```

Ingress 工作在 L7（HTTP/HTTPS）层，可以做：基于域名路由、基于路径路由、TLS 终止、重写 URL、限速、认证、灰度发布等。

**Ingress 与 Service 的区别：**

| 特性 | Service（LoadBalancer） | Ingress |
|------|------------------------|---------|
| 工作层 | L4（TCP/UDP） | L7（HTTP/HTTPS） |
| 路由能力 | 无 | Host / Path |
| TLS 终止 | 需自行处理 | 统一处理 |
| 云LB 数量 | 每个 Service 一个 | 共享一个 |
| 成本 | 高 | 低 |

---

## Ingress Controller 选型

| Controller | 维护方 | 适用场景 | 优势 | 劣势 |
|------------|--------|----------|------|------|
| **ingress-nginx** | K8s 社区 | 通用场景 | 功能最全、社区大、文档多 | 配置相对复杂 |
| **Traefik** | Traefik Labs | 微服务、动态配置 | 自动发现、Dashboard 好看 | 学习曲线 |
| **AWS ALB** | AWS | EKS + AWS 原生 | 与 AWS 深度集成 | 只能在 AWS 用 |
| **Contour** | VMware/CNCF | 需要 HTTP/2、gRPC | Envoy 作为数据面，性能好 | 社区较小 |
| **HAProxy** | HAProxy Tech | 高性能四/七层 | 极致性能 | 配置麻烦 |

**生产选型建议：**
- 自建 K8s / 非云厂商托管 → **ingress-nginx**（最成熟，问题多容易搜到答案）
- EKS 且深度使用 AWS 服务 → **AWS Load Balancer Controller (ALB)**
- 需要动态证书管理和漂亮 Dashboard → **Traefik**

---

## 安装 ingress-nginx

```bash
# 使用 Helm 安装（推荐）
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx
helm repo update

# 生产配置
cat > ingress-nginx-values.yaml << 'EOF'
controller:
  replicaCount: 2        # 至少 2 副本，高可用

  # 资源限制
  resources:
    requests:
      cpu: 100m
      memory: 90Mi
    limits:
      cpu: 500m
      memory: 512Mi

  # Pod 反亲和，避免两个副本调度到同一节点
  affinity:
    podAntiAffinity:
      preferredDuringSchedulingIgnoredDuringExecution:
        - weight: 100
          podAffinityTerm:
            labelSelector:
              matchExpressions:
                - key: app.kubernetes.io/component
                  operator: In
                  values:
                    - controller
            topologyKey: kubernetes.io/hostname

  # 全局默认配置
  config:
    use-gzip: "true"
    gzip-level: "5"
    proxy-body-size: "100m"
    proxy-read-timeout: "60"
    proxy-connect-timeout: "10"
    keep-alive: "75"
    worker-processes: "auto"

  # 开启 metrics（用于 Prometheus 采集）
  metrics:
    enabled: true
    serviceMonitor:
      enabled: true   # 如果使用 kube-prometheus-stack

  # HPA 配置
  autoscaling:
    enabled: true
    minReplicas: 2
    maxReplicas: 10
    targetCPUUtilizationPercentage: 70

  service:
    annotations:
      # AWS EKS：使用 NLB
      service.beta.kubernetes.io/aws-load-balancer-type: "nlb"
      service.beta.kubernetes.io/aws-load-balancer-cross-zone-load-balancing-enabled: "true"
EOF

helm upgrade --install ingress-nginx ingress-nginx/ingress-nginx \
  -n ingress-nginx \
  --create-namespace \
  -f ingress-nginx-values.yaml \
  --wait \
  --timeout 5m
```

---

## Ingress 资源配置

### 基础路由

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: my-app-ingress
  namespace: production
  annotations:
    kubernetes.io/ingress.class: "nginx"   # 指定使用哪个 Controller
spec:
  ingressClassName: nginx    # K8s 1.18+ 推荐方式
  rules:
    - host: app.example.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: my-app-service
                port:
                  number: 80
```

### Path 类型：Prefix vs Exact vs ImplementationSpecific

| 类型 | 说明 | 匹配示例 |
|------|------|----------|
| `Prefix` | 前缀匹配（基于 `/` 分割的路径段） | `/api` 匹配 `/api`、`/api/users`、`/api/v1/` |
| `Exact` | 精确匹配 | `/api` 只匹配 `/api`，不匹配 `/api/` |
| `ImplementationSpecific` | 行为取决于 IngressClass | nginx 中等同于 Prefix |

```yaml
# 常见场景：前端走根路径，API 走 /api 前缀
spec:
  rules:
    - host: app.example.com
      http:
        paths:
          - path: /api
            pathType: Prefix      # /api、/api/users 都匹配
            backend:
              service:
                name: api-service
                port:
                  number: 8080
          - path: /
            pathType: Prefix      # 兜底路由，放在最后
            backend:
              service:
                name: frontend-service
                port:
                  number: 80
```

### 多域名路由

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: multi-domain-ingress
  namespace: production
spec:
  ingressClassName: nginx
  tls:
    - hosts:
        - app.example.com
        - api.example.com
      secretName: example-tls
  rules:
    - host: app.example.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: frontend-service
                port:
                  number: 80
    - host: api.example.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: api-service
                port:
                  number: 8080
```

### URL Rewrite

```yaml
# 将 /app/api/users 重写为 /api/users（去掉 /app 前缀）
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: rewrite-ingress
  annotations:
    nginx.ingress.kubernetes.io/rewrite-target: /$2   # $2 对应 (.*) 捕获组
spec:
  ingressClassName: nginx
  rules:
    - host: example.com
      http:
        paths:
          - path: /app(/|$)(.*)      # 正则：/app 后面的内容赋值给 $2
            pathType: ImplementationSpecific
            backend:
              service:
                name: app-service
                port:
                  number: 80
```

---

## TLS 配置

### 方法一：cert-manager 自动签发（推荐）

```bash
# 安装 cert-manager
helm upgrade --install cert-manager jetstack/cert-manager \
  -n cert-manager \
  --create-namespace \
  --set installCRDs=true \
  --version v1.13.0 \
  --wait
```

```yaml
# 创建 ClusterIssuer（Let's Encrypt 生产环境）
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt-prod
spec:
  acme:
    server: https://acme-v02.api.letsencrypt.org/directory
    email: admin@example.com
    privateKeySecretRef:
      name: letsencrypt-prod-key
    solvers:
      - http01:
          ingress:
            class: nginx
```

```yaml
# Ingress 中引用，cert-manager 自动创建证书 Secret
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: tls-ingress
  namespace: production
  annotations:
    cert-manager.io/cluster-issuer: "letsencrypt-prod"   # 关键 annotation
spec:
  ingressClassName: nginx
  tls:
    - hosts:
        - app.example.com
      secretName: app-example-tls    # cert-manager 会自动创建这个 Secret
  rules:
    - host: app.example.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: app-service
                port:
                  number: 80
```

```bash
# 查看证书状态
kubectl get certificate -n production
kubectl describe certificate app-example-tls -n production

# 证书签发过程中查看 Challenge
kubectl get challenges -n production
```

### 方法二：手动管理证书 Secret

```bash
# 从证书文件创建 Secret
kubectl create secret tls my-tls-secret \
  --cert=path/to/tls.crt \
  --key=path/to/tls.key \
  -n production

# 查看证书到期时间
kubectl get secret my-tls-secret -n production -o jsonpath='{.data.tls\.crt}' \
  | base64 -d | openssl x509 -noout -dates
```

---

## 常用 Annotations

### 限速与超时

```yaml
metadata:
  annotations:
    # 限制每秒请求数（基于客户端 IP）
    nginx.ingress.kubernetes.io/limit-rps: "10"
    # 限制每分钟连接数
    nginx.ingress.kubernetes.io/limit-connections: "5"

    # 超时设置（秒）
    nginx.ingress.kubernetes.io/proxy-connect-timeout: "10"
    nginx.ingress.kubernetes.io/proxy-read-timeout: "60"
    nginx.ingress.kubernetes.io/proxy-send-timeout: "60"

    # 请求体大小限制
    nginx.ingress.kubernetes.io/proxy-body-size: "50m"
```

### Proxy Buffer 配置

```yaml
metadata:
  annotations:
    # 启用 proxy buffer（大响应时避免 upstream 等待 client 读取）
    nginx.ingress.kubernetes.io/proxy-buffering: "on"
    nginx.ingress.kubernetes.io/proxy-buffers-number: "4"
    nginx.ingress.kubernetes.io/proxy-buffer-size: "8k"
```

### CORS 配置

```yaml
metadata:
  annotations:
    nginx.ingress.kubernetes.io/enable-cors: "true"
    nginx.ingress.kubernetes.io/cors-allow-origin: "https://app.example.com"
    nginx.ingress.kubernetes.io/cors-allow-methods: "GET, POST, PUT, DELETE, OPTIONS"
    nginx.ingress.kubernetes.io/cors-allow-headers: "Authorization, Content-Type, X-Requested-With"
    nginx.ingress.kubernetes.io/cors-max-age: "600"
```

### Basic Auth 认证

```bash
# 创建 htpasswd 文件
htpasswd -c auth admin
kubectl create secret generic basic-auth \
  --from-file=auth \
  -n production
```

```yaml
metadata:
  annotations:
    nginx.ingress.kubernetes.io/auth-type: basic
    nginx.ingress.kubernetes.io/auth-secret: basic-auth
    nginx.ingress.kubernetes.io/auth-realm: "Authentication Required"
```

### HTTP 强制跳转 HTTPS

```yaml
metadata:
  annotations:
    nginx.ingress.kubernetes.io/ssl-redirect: "true"
    nginx.ingress.kubernetes.io/force-ssl-redirect: "true"
```

### WebSocket 支持

```yaml
metadata:
  annotations:
    nginx.ingress.kubernetes.io/proxy-read-timeout: "3600"   # 长连接不超时
    nginx.ingress.kubernetes.io/proxy-send-timeout: "3600"
    nginx.ingress.kubernetes.io/configuration-snippet: |
      proxy_set_header Upgrade $http_upgrade;
      proxy_set_header Connection "upgrade";
```

---

## 灰度发布（Canary）

ingress-nginx 内置 Canary 支持，通过 annotations 实现按比例/按 Header/按 Cookie 分流。

### 按权重分流（最常用）

```yaml
# 稳定版 Ingress（原有）
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: app-ingress-stable
  namespace: production
spec:
  ingressClassName: nginx
  rules:
    - host: app.example.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: app-service-v1
                port:
                  number: 80
---
# 金丝雀 Ingress（新版本，承载 10% 流量）
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: app-ingress-canary
  namespace: production
  annotations:
    nginx.ingress.kubernetes.io/canary: "true"
    nginx.ingress.kubernetes.io/canary-weight: "10"   # 10% 流量到新版本
spec:
  ingressClassName: nginx
  rules:
    - host: app.example.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: app-service-v2    # 新版本 Service
                port:
                  number: 80
```

### 按 Header 分流（测试/内部用户）

```yaml
metadata:
  annotations:
    nginx.ingress.kubernetes.io/canary: "true"
    nginx.ingress.kubernetes.io/canary-by-header: "X-Canary"
    nginx.ingress.kubernetes.io/canary-by-header-value: "true"
    # 请求头带 X-Canary: true 的流量路由到新版本
```

### 按 Cookie 分流

```yaml
metadata:
  annotations:
    nginx.ingress.kubernetes.io/canary: "true"
    nginx.ingress.kubernetes.io/canary-by-cookie: "canary_user"
    # Cookie 中 canary_user=always 则路由到新版本
    # canary_user=never 则永远走稳定版
```

**灰度发布流程：**

```bash
# 1. 创建 canary Ingress，先 5% 流量
kubectl apply -f ingress-canary.yaml

# 2. 观察监控，逐步提高比例
kubectl annotate ingress app-ingress-canary \
  nginx.ingress.kubernetes.io/canary-weight=30 \
  --overwrite -n production

# 3. 稳定后切全量
kubectl annotate ingress app-ingress-canary \
  nginx.ingress.kubernetes.io/canary-weight=100 \
  --overwrite -n production

# 4. 删除旧 Service，删除 canary Ingress，更新 stable Ingress 指向新版本
kubectl delete ingress app-ingress-canary -n production
```

---

## 排查常见问题

### Ingress 不生效

```bash
# 1. 确认 Ingress 资源存在且 ADDRESS 已分配
kubectl get ingress -n production
# ADDRESS 列为空说明 Controller 没有处理到，检查 ingressClassName

# 2. 检查 ingressClassName 是否匹配
kubectl get ingressclass
kubectl get ingress <name> -n production -o jsonpath='{.spec.ingressClassName}'

# 3. 查看 ingress-nginx controller 日志
kubectl logs -n ingress-nginx -l app.kubernetes.io/component=controller --tail=50

# 4. 确认 Service 和 Endpoints 正常
kubectl get endpoints <svc-name> -n production
```

### 502 Bad Gateway

```bash
# 通常是后端 Pod 无法访问
# 1. 检查 Endpoints
kubectl get endpoints <backend-service> -n production

# 2. 测试从 Controller 到 Pod 的连通性
kubectl exec -n ingress-nginx <nginx-pod> -- curl http://<pod-ip>:<port>

# 3. 查看 nginx 错误日志
kubectl logs -n ingress-nginx <nginx-pod> | grep "upstream"

# 4. 检查 Service port 配置是否匹配 Pod 的 containerPort
kubectl describe svc <svc-name> -n production
kubectl describe pod <pod-name> -n production | grep -A 5 "Ports:"
```

### SSL 证书问题

```bash
# 查看 cert-manager 日志
kubectl logs -n cert-manager -l app=cert-manager --tail=50

# 查看 Certificate 资源状态
kubectl describe certificate <cert-name> -n production

# 查看 CertificateRequest
kubectl get certificaterequest -n production
kubectl describe certificaterequest <name> -n production

# 查看 ACME Challenge（http01 验证）
kubectl get challenges -n production
# Challenge 需要通过 http://domain/.well-known/acme-challenge/xxx 可访问

# 手动测试 ACME 验证路径是否可达
curl http://app.example.com/.well-known/acme-challenge/test
```

### 查看 nginx 实际配置

```bash
# 进入 Controller Pod 查看生成的 nginx.conf
kubectl exec -n ingress-nginx <controller-pod> -- cat /etc/nginx/nginx.conf | grep -A 20 "server_name app.example.com"

# 或使用 nginx -T 查看完整配置（包含所有 include）
kubectl exec -n ingress-nginx <controller-pod> -- nginx -T 2>/dev/null | head -200
```
