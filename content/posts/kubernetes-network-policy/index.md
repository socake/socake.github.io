---
title: "Kubernetes NetworkPolicy 网络隔离实战"
date: 2025-06-15T09:00:00+08:00
draft: false
tags: ["Kubernetes", "NetworkPolicy", "安全", "网络", "零信任"]
categories: ["Kubernetes"]
description: "从 NetworkPolicy 工作原理出发，覆盖默认拒绝策略、命名空间隔离、数据库访问控制、Egress 限制等常见场景，结合 Cilium L7 策略、多租户设计和 Istio mTLS 互补关系，给出可直接落地的配置模板。"
summary: "系统讲解 Kubernetes NetworkPolicy 的工作机制与生产实战配置，覆盖 deny-all 基础模板、常见隔离场景、Cilium 扩展、多租户设计、测试验证方法及常见陷阱。"
toc: true
math: false
diagram: false
series: ["K8s 完全指南"]
keywords: ["NetworkPolicy", "Kubernetes", "网络隔离", "Cilium", "零信任", "多租户", "安全", "CNI"]
params:
  reading_time: true
---

## 为什么需要 NetworkPolicy

Kubernetes 默认的网络模型是**完全开放**的：集群内所有 Pod 可以互相通信，不需要任何授权。这在开发环境很便利，但在生产环境是一个严重的安全隐患。

设想一下：如果某个前端 Pod 被攻陷（例如 SSRF、RCE 漏洞），攻击者可以直接从这个 Pod 访问数据库、内部 API、消息队列，甚至其他命名空间的服务。没有任何网络层面的阻拦。

NetworkPolicy 是 Kubernetes 原生的网络访问控制机制，它允许你精确定义：

- **哪些 Pod 可以访问这个 Pod**（Ingress 规则）
- **这个 Pod 可以访问哪些目标**（Egress 规则）

本文从工作原理到生产实战，给出完整的 NetworkPolicy 使用指南。

---

## 工作原理与 CNI 要求

### CNI 支持要求

NetworkPolicy 是 Kubernetes API 对象，但它本身不做任何流量控制。**实际执行网络策略的是 CNI 插件**。只有支持 NetworkPolicy 的 CNI 才能让策略生效：

| CNI 插件 | NetworkPolicy 支持 | L7 策略 | 备注 |
|----------|-------------------|---------|------|
| **Cilium** | ✓ | ✓（原生） | 推荐，基于 eBPF，性能最好 |
| **Calico** | ✓ | 部分（需 Envoy） | 生产广泛使用，支持 GlobalNetworkPolicy |
| **Weave Net** | ✓ | ✗ | 功能基础 |
| **Flannel** | ✗ | ✗ | 不支持 NetworkPolicy |
| **Canal** | ✓ | ✗ | Flannel + Calico 组合 |
| **AWS VPC CNI** | ✓（需额外组件） | ✗ | 需要 Network Policy Controller |

如果你使用 Flannel，NetworkPolicy 对象可以创建，但完全不生效——这是最容易踩的坑之一。

### 策略的本质

NetworkPolicy 通过 **标签选择器** 定义作用范围，通过 **规则列表** 定义允许的流量。重要原则：

1. **策略叠加，不覆盖**：多个 NetworkPolicy 作用于同一 Pod 时，所有策略的规则取并集（OR 关系）
2. **白名单模型**：一旦有 NetworkPolicy 选中了某个 Pod，该 Pod 的未被允许的流量方向就被默认拒绝
3. **双向独立**：Ingress 和 Egress 是独立的，需要分别配置。允许 A 访问 B 的 Ingress 规则，不会自动允许 B 响应 A（TCP 握手的响应流量由 conntrack 自动放行，不需要显式配置）

### 策略评估流程

当 Pod A 尝试连接 Pod B 的 3306 端口时，CNI 的评估流程：

```
Pod A 发起连接
        ↓
检查 Pod A 是否有 Egress NetworkPolicy
  ├── 没有 → 允许（Pod A 的出口流量不受限）
  └── 有 → 检查是否有规则允许访问 Pod B:3306
              ├── 有匹配规则 → 通过出口检查
              └── 无匹配规则 → 拒绝连接

        ↓（出口通过）
检查 Pod B 是否有 Ingress NetworkPolicy
  ├── 没有 → 允许（Pod B 的入口流量不受限）
  └── 有 → 检查是否有规则允许 Pod A 访问
              ├── 有匹配规则 → 连接建立
              └── 无匹配规则 → 拒绝连接
```

注意：**连接需要同时通过两侧的检查**。

---

## 默认拒绝策略（Deny All）

零信任的起点是默认拒绝一切，然后按需开放。以下是生产环境的基础模板。

### 拒绝所有 Ingress

```yaml
# deny-all-ingress.yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny-ingress
  namespace: production
spec:
  podSelector: {}        # 空选择器 = 选中命名空间内所有 Pod
  policyTypes:
    - Ingress
  # 没有 ingress 规则 = 拒绝所有入口流量
```

### 拒绝所有 Egress

```yaml
# deny-all-egress.yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny-egress
  namespace: production
spec:
  podSelector: {}
  policyTypes:
    - Egress
  # 没有 egress 规则 = 拒绝所有出口流量
  # 注意：这会连 DNS 也封掉，通常需要额外放开 DNS
```

### 放开 DNS（关键）

拒绝所有 Egress 后，Pod 的 DNS 解析会失败，导致服务无法工作。必须单独放开 DNS：

```yaml
# allow-dns-egress.yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-dns-egress
  namespace: production
spec:
  podSelector: {}
  policyTypes:
    - Egress
  egress:
    - ports:
        - protocol: UDP
          port: 53
        - protocol: TCP
          port: 53
      # 不指定 to，允许访问集群内任意 DNS（通常是 kube-dns/CoreDNS）
```

### 组合应用

在实际部署中，通常对每个命名空间应用 deny-all + allow-dns：

```bash
# 为 production 命名空间应用基础隔离
kubectl apply -f deny-all-ingress.yaml -n production
kubectl apply -f deny-all-egress.yaml -n production
kubectl apply -f allow-dns-egress.yaml -n production
```

---

## 常见场景实战

### 场景一：命名空间间隔离

**需求**：`team-a` 命名空间的 Pod 只能被同命名空间的 Pod 访问，拒绝 `team-b` 等其他命名空间的访问。

```yaml
# allow-same-namespace.yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-same-namespace
  namespace: team-a
spec:
  podSelector: {}    # 作用于 team-a 下所有 Pod
  policyTypes:
    - Ingress
  ingress:
    - from:
        # 只允许来自同命名空间的 Pod
        - podSelector: {}
```

**验证**：

```bash
# 在 team-b 命名空间启动测试 Pod
kubectl run test-pod --image=busybox -n team-b --restart=Never -- sleep 3600

# 尝试访问 team-a 的服务（应该失败）
kubectl exec -n team-b test-pod -- wget -T 3 -O- http://my-service.team-a/health
# wget: download timed out（连接被拒绝）

# 在 team-a 内部测试（应该成功）
kubectl run test-pod --image=busybox -n team-a --restart=Never -- sleep 3600
kubectl exec -n team-a test-pod -- wget -T 3 -O- http://my-service.team-a/health
# 返回 200 OK
```

### 场景二：允许 Ingress 控制器访问应用

**需求**：只有 `ingress-nginx` 命名空间的 Ingress Controller Pod 可以访问应用的 HTTP 端口，其他 Pod 不能直接访问。

```yaml
# allow-ingress-controller.yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-ingress-controller
  namespace: production
spec:
  podSelector:
    matchLabels:
      app: my-webapp     # 只作用于 webapp Pod
  policyTypes:
    - Ingress
  ingress:
    - from:
        # 允许来自 ingress-nginx 命名空间且带有特定标签的 Pod
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: ingress-nginx
          podSelector:
            matchLabels:
              app.kubernetes.io/name: ingress-nginx
      ports:
        - protocol: TCP
          port: 8080
```

**关键细节**：`namespaceSelector` 和 `podSelector` 写在同一个 `from` 列表项里时，是 AND 关系（必须同时满足）；写在不同列表项时是 OR 关系。

```yaml
# AND 关系（同一个 - 下）：来自 ingress-nginx 命名空间 且 带指定标签的 Pod
ingress:
  - from:
      - namespaceSelector:
          matchLabels:
            kubernetes.io/metadata.name: ingress-nginx
        podSelector:        # 注意：没有 -，与上面的 namespaceSelector 同级
          matchLabels:
            app.kubernetes.io/name: ingress-nginx

# OR 关系（不同 - 下）：来自 ingress-nginx 命名空间 的任意 Pod，OR 带指定标签的任意命名空间 Pod
ingress:
  - from:
      - namespaceSelector:
          matchLabels:
            kubernetes.io/metadata.name: ingress-nginx
      - podSelector:        # 注意：有 -，是新的列表项
          matchLabels:
            app.kubernetes.io/name: ingress-nginx
```

这是 NetworkPolicy 最常见的理解错误，务必注意 YAML 的缩进和 `-` 位置。

### 场景三：数据库只允许特定服务访问

**需求**：MySQL Pod 只允许 `backend` 服务访问 3306 端口，其他所有 Pod（包括运维工具）都无法直接连接数据库。

```yaml
# mysql-network-policy.yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: mysql-access-control
  namespace: production
spec:
  podSelector:
    matchLabels:
      app: mysql
  policyTypes:
    - Ingress
    - Egress
  ingress:
    # 只允许 backend 服务 Pod 访问 3306
    - from:
        - podSelector:
            matchLabels:
              app: backend
              role: api-server
      ports:
        - protocol: TCP
          port: 3306

    # 允许 MySQL 主从复制（如果有从库）
    - from:
        - podSelector:
            matchLabels:
              app: mysql
              role: replica
      ports:
        - protocol: TCP
          port: 3306

  egress:
    # MySQL 通常只需要 DNS，不需要主动出口
    - ports:
        - protocol: UDP
          port: 53
        - protocol: TCP
          port: 53
    # 如果有主从复制，允许访问其他 MySQL 实例
    - to:
        - podSelector:
            matchLabels:
              app: mysql
      ports:
        - protocol: TCP
          port: 3306
```

**配套：backend 服务的 Egress 规则**

只有 Ingress 规则还不够，还需要确保 backend Pod 的 Egress 策略允许访问 MySQL：

```yaml
# backend-egress-policy.yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: backend-egress
  namespace: production
spec:
  podSelector:
    matchLabels:
      app: backend
  policyTypes:
    - Egress
  egress:
    # 允许访问 MySQL
    - to:
        - podSelector:
            matchLabels:
              app: mysql
      ports:
        - protocol: TCP
          port: 3306

    # 允许访问 Redis
    - to:
        - podSelector:
            matchLabels:
              app: redis
      ports:
        - protocol: TCP
          port: 6379

    # 允许调用其他内部服务（HTTP/HTTPS）
    - to:
        - podSelector:
            matchLabels:
              tier: internal-service
      ports:
        - protocol: TCP
          port: 8080
        - protocol: TCP
          port: 8443

    # 允许 DNS
    - ports:
        - protocol: UDP
          port: 53
        - protocol: TCP
          port: 53
```

### 场景四：限制 Pod 的 Egress 出口（只允许访问特定外部 IP）

**需求**：支付服务只能访问支付网关的 IP 段（`203.0.113.0/24`），禁止访问其他外部 IP，防止数据泄露或 SSRF 横向攻击。

```yaml
# payment-egress-restriction.yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: payment-service-egress
  namespace: production
spec:
  podSelector:
    matchLabels:
      app: payment-service
  policyTypes:
    - Egress
  egress:
    # 允许访问支付网关 IP 段
    - to:
        - ipBlock:
            cidr: 203.0.113.0/24
            except:
              - 203.0.113.100/32  # 排除某个特定 IP
      ports:
        - protocol: TCP
          port: 443

    # 允许访问内部数据库（在集群 CIDR 内）
    - to:
        - podSelector:
            matchLabels:
              app: mysql
      ports:
        - protocol: TCP
          port: 3306

    # 允许访问 Kafka（内部）
    - to:
        - podSelector:
            matchLabels:
              app: kafka
      ports:
        - protocol: TCP
          port: 9092

    # 允许 DNS
    - ports:
        - protocol: UDP
          port: 53
```

**注意**：`ipBlock` 的 `cidr` 字段用于匹配目标 IP 范围。集群内 Pod 的 IP 通常在 Pod CIDR 内（如 `10.0.0.0/8`），如果你的规则里有 `ipBlock: 0.0.0.0/0`，实际上也包含了集群内 Pod，需要用 `except` 排除 Pod CIDR 和 Service CIDR：

```yaml
egress:
  - to:
      - ipBlock:
          cidr: 0.0.0.0/0
          except:
            - 10.0.0.0/8       # Pod CIDR
            - 172.16.0.0/12    # Service CIDR
            - 192.168.0.0/16   # 其他内部网段
```

---

## Cilium NetworkPolicy 扩展

标准 Kubernetes NetworkPolicy 只能做到 L3/L4（IP 地址和端口）级别的控制。Cilium 通过 `CiliumNetworkPolicy` 扩展到 L7（应用层协议），可以精确控制 HTTP 方法、URL 路径、gRPC 方法等。

### L7 HTTP 策略示例

**需求**：`frontend` Pod 只能对 `backend` 的 `/api/public/*` 路径发 GET 请求，不能访问 `/api/admin/*`，不能发 POST/DELETE 请求。

```yaml
# cilium-l7-http-policy.yaml
apiVersion: cilium.io/v2
kind: CiliumNetworkPolicy
metadata:
  name: frontend-to-backend-l7
  namespace: production
spec:
  endpointSelector:
    matchLabels:
      app: backend          # 作用于 backend Pod（控制谁能访问它）
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
              # 只允许 GET /api/public/ 下的路径
              - method: GET
                path: "^/api/public/.*"
              # 允许 GET /health（健康检查）
              - method: GET
                path: "^/health$"
```

### gRPC 方法级别控制

```yaml
apiVersion: cilium.io/v2
kind: CiliumNetworkPolicy
metadata:
  name: grpc-method-control
  namespace: production
spec:
  endpointSelector:
    matchLabels:
      app: user-service
  ingress:
    - fromEndpoints:
        - matchLabels:
            app: api-gateway
      toPorts:
        - ports:
            - port: "50051"
              protocol: TCP
          rules:
            # 只允许调用 UserService 的 GetUser 和 ListUsers 方法
            # 禁止调用 DeleteUser、UpdateUser
            http:
              - method: POST
                path: "/user.UserService/GetUser"
              - method: POST
                path: "/user.UserService/ListUsers"
```

### DNS 策略（限制外部域名访问）

Cilium 可以基于 DNS 域名而非 IP 配置策略，解决外部服务 IP 动态变化的问题：

```yaml
apiVersion: cilium.io/v2
kind: CiliumNetworkPolicy
metadata:
  name: allow-external-apis
  namespace: production
spec:
  endpointSelector:
    matchLabels:
      app: payment-service
  egress:
    # 允许访问特定域名（Cilium 自动解析并更新 IP）
    - toFQDNs:
        - matchName: "api.stripe.com"
        - matchName: "api.paypal.com"
        - matchPattern: "*.amazonaws.com"    # 支持通配符
      toPorts:
        - ports:
            - port: "443"
              protocol: TCP
    # 必须放开 DNS，Cilium 需要拦截 DNS 响应来学习 IP 映射
    - toEndpoints:
        - matchLabels:
            k8s:io.kubernetes.pod.namespace: kube-system
            k8s:k8s-app: kube-dns
      toPorts:
        - ports:
            - port: "53"
              protocol: UDP
            - port: "53"
              protocol: TCP
          rules:
            dns:
              - matchPattern: "*"
```

---

## 多租户场景下的 NetworkPolicy 设计

在多租户 Kubernetes 集群中（多个团队共用一个集群，每个团队一个命名空间），网络隔离是租户安全的基础。

### 命名空间级别的隔离框架

```yaml
# 命名空间模板：每个租户命名空间都应用这套策略

# 1. 拒绝所有跨命名空间流量（Ingress）
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny-all
  namespace: tenant-a     # 每个租户命名空间都部署一份
spec:
  podSelector: {}
  policyTypes:
    - Ingress
    - Egress
---
# 2. 允许命名空间内部通信
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-intra-namespace
  namespace: tenant-a
spec:
  podSelector: {}
  policyTypes:
    - Ingress
    - Egress
  ingress:
    - from:
        - podSelector: {}    # 同命名空间内所有 Pod
  egress:
    - to:
        - podSelector: {}    # 同命名空间内所有 Pod
---
# 3. 允许 DNS（系统级，每个命名空间都需要）
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-dns
  namespace: tenant-a
spec:
  podSelector: {}
  policyTypes:
    - Egress
  egress:
    - ports:
        - protocol: UDP
          port: 53
        - protocol: TCP
          port: 53
---
# 4. 允许被 Ingress Controller 访问（如果租户有对外暴露的服务）
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-ingress-nginx
  namespace: tenant-a
spec:
  podSelector:
    matchLabels:
      expose: "true"     # 只有打了这个标签的 Pod 才被 Ingress 访问
  policyTypes:
    - Ingress
  ingress:
    - from:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: ingress-nginx
          podSelector:
            matchLabels:
              app.kubernetes.io/name: ingress-nginx
      ports:
        - protocol: TCP
          port: 8080
```

### 使用 Kyverno 自动注入策略

手动为每个命名空间部署策略容易遗漏，使用 Kyverno 的 ClusterPolicy 自动为新命名空间注入：

```yaml
# kyverno-inject-network-policy.yaml
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: inject-default-network-policies
spec:
  rules:
    - name: inject-deny-all
      match:
        any:
          - resources:
              kinds:
                - Namespace
              selector:
                matchLabels:
                  tenant: "true"    # 只对打了 tenant=true 标签的命名空间生效
      generate:
        apiVersion: networking.k8s.io/v1
        kind: NetworkPolicy
        name: default-deny-all
        namespace: "{{request.object.metadata.name}}"
        synchronize: true          # 如果策略被删除，自动重建
        data:
          spec:
            podSelector: {}
            policyTypes:
              - Ingress
              - Egress

    - name: inject-allow-dns
      match:
        any:
          - resources:
              kinds:
                - Namespace
              selector:
                matchLabels:
                  tenant: "true"
      generate:
        apiVersion: networking.k8s.io/v1
        kind: NetworkPolicy
        name: allow-dns-egress
        namespace: "{{request.object.metadata.name}}"
        synchronize: true
        data:
          spec:
            podSelector: {}
            policyTypes:
              - Egress
            egress:
              - ports:
                  - protocol: UDP
                    port: 53
                  - protocol: TCP
                    port: 53
```

### 共享服务访问控制

多租户场景下，集群中通常有共享的基础设施服务（如日志采集 Agent、Metrics Exporter），这些服务需要访问所有租户命名空间的 Pod。

```yaml
# allow-monitoring-access.yaml
# 部署到每个租户命名空间，允许 monitoring 命名空间的 Prometheus 抓取指标
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-prometheus-scrape
  namespace: tenant-a
spec:
  podSelector:
    matchLabels:
      monitoring: "true"    # 只有暴露了 metrics 的 Pod
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
        - protocol: TCP
          port: 8080    # 通用 metrics 端口
```

---

## 测试与验证

### 使用 netcat 测试连通性

```bash
# 在目标 Pod 的命名空间内启动测试 Pod
kubectl run netshoot \
  --image=nicolaka/netshoot \
  -n production \
  --restart=Never \
  -- sleep 3600

# 测试 TCP 连接（nc -zv 目标IP 端口）
kubectl exec -n production netshoot -- nc -zv mysql-service 3306
# 允许：Connection to mysql-service (10.96.1.100) 3306 port [tcp/mysql] succeeded!
# 拒绝：nc: connect to mysql-service port 3306 (tcp) failed: Connection timed out

# 测试 HTTP 连接
kubectl exec -n production netshoot -- curl -I --max-time 3 http://backend-service:8080/health

# 测试跨命名空间（从 team-b 访问 team-a）
kubectl exec -n team-b netshoot -- nc -zv my-service.team-a.svc.cluster.local 8080
```

### 使用 kubectl debug 临时测试

```bash
# 在已有 Pod 旁边启动临时调试容器（不需要创建新 Pod）
kubectl debug -n production \
  deployment/backend \
  -it \
  --image=nicolaka/netshoot \
  --target=backend \
  -- bash

# 在容器里测试
nc -zv mysql 3306
nslookup mysql
curl -v http://redis:6379
```

### network-policy-viewer 可视化

使用 `kubectl-network-policy-viewer` 插件可视化策略：

```bash
# 安装（通过 krew）
kubectl krew install np-viewer

# 查看某个 Pod 适用的所有策略
kubectl np-viewer -n production -p app=backend

# 输出示例：
# Pod: backend-xxx
# Ingress:
#   ✓ from ingress-nginx (port 8080)
#   ✓ from prometheus (port 9090)
#   ✗ all others DENIED
# Egress:
#   ✓ to mysql (port 3306)
#   ✓ to redis (port 6379)
#   ✓ DNS (UDP/TCP 53)
#   ✗ all others DENIED
```

### 使用 Cilium CLI 验证 L7 策略

```bash
# 安装 Cilium CLI
curl -L --remote-name-all https://github.com/cilium/cilium-cli/releases/latest/download/cilium-linux-amd64.tar.gz
tar xzvf cilium-linux-amd64.tar.gz
mv cilium /usr/local/bin

# 运行连通性测试
cilium connectivity test

# 查看某个 Endpoint 的策略
cilium endpoint list
cilium endpoint get <endpoint-id>

# 查看策略执行日志（需要开启 policy audit mode）
cilium monitor --type policy-verdict
```

### 编写策略测试用例

在 CI/CD 中加入网络策略测试，防止策略被意外修改：

```bash
#!/bin/bash
# test-network-policies.sh

NAMESPACE="production"
PASS=0
FAIL=0

# 测试函数
test_connectivity() {
    local from_pod=$1
    local to_service=$2
    local port=$3
    local expected=$4  # "allowed" or "denied"
    local description=$5

    result=$(kubectl exec -n $NAMESPACE $from_pod -- \
        nc -zv -w 3 $to_service $port 2>&1)

    if echo "$result" | grep -q "succeeded"; then
        actual="allowed"
    else
        actual="denied"
    fi

    if [ "$actual" = "$expected" ]; then
        echo "✓ PASS: $description"
        ((PASS++))
    else
        echo "✗ FAIL: $description (expected: $expected, actual: $actual)"
        ((FAIL++))
    fi
}

# 确保测试 Pod 存在
kubectl run test-frontend -n $NAMESPACE --image=busybox --restart=Never -- sleep 3600 2>/dev/null || true
kubectl run test-attacker -n team-b --image=busybox --restart=Never -- sleep 3600 2>/dev/null || true
kubectl wait --for=condition=Ready pod/test-frontend -n $NAMESPACE --timeout=30s

# 运行测试
test_connectivity "test-frontend" "mysql" "3306" "denied" "Frontend 不能访问 MySQL"
test_connectivity "test-frontend" "backend" "8080" "allowed" "Frontend 可以访问 Backend"
test_connectivity "test-attacker" "backend.$NAMESPACE" "8080" "denied" "其他命名空间不能访问 Backend"

# 清理
kubectl delete pod test-frontend -n $NAMESPACE --ignore-not-found
kubectl delete pod test-attacker -n team-b --ignore-not-found

echo "\n结果：$PASS 通过，$FAIL 失败"
[ $FAIL -eq 0 ] && exit 0 || exit 1
```

---

## 常见陷阱

### 陷阱一：空 podSelector 的含义

```yaml
podSelector: {}        # 选中命名空间内 所有 Pod
podSelector:           # 空对象，等同于 {}，同样选中所有 Pod
matchLabels: {}        # 也是选中所有 Pod（空标签选择器 = 匹配任意）

# 但注意：
ingress:
  - from:
      - podSelector: {}   # 允许来自同命名空间内所有 Pod
      # 这不包括其他命名空间的 Pod！
```

### 陷阱二：policyTypes 显式声明的重要性

```yaml
# 不声明 policyTypes，只有 ingress 规则
spec:
  podSelector:
    matchLabels:
      app: backend
  ingress:
    - from:
        - podSelector:
            matchLabels:
              app: frontend
# 结果：Egress 不受限（因为没有声明 Egress policyType）
# Backend 可以访问任意目标

# 正确做法：显式声明两个方向
spec:
  podSelector:
    matchLabels:
      app: backend
  policyTypes:
    - Ingress    # 声明了就会限制，即使没有 ingress 规则（= deny all ingress）
    - Egress     # 同上
  ingress:
    - from: ...
  # 不写 egress 规则 = 拒绝所有出口（前提是 policyTypes 里声明了 Egress）
```

### 陷阱三：策略叠加导致意外放开

```yaml
# 策略 A：只允许 frontend 访问 backend
ingress:
  - from:
      - podSelector:
          matchLabels:
            app: frontend

# 策略 B：只允许 monitoring 访问 backend（Prometheus 抓取）
ingress:
  - from:
      - podSelector:
          matchLabels:
            app: prometheus

# 结果：两个策略都作用于 backend，取并集
# = frontend 可以访问 AND prometheus 可以访问
# 这是正确的预期行为，但如果你期望"只有策略 B 生效"，就会困惑
```

### 陷阱四：Service IP vs Pod IP

NetworkPolicy 匹配的是**实际的 Pod IP**，不是 Service ClusterIP。当流量通过 Service 访问时，kube-proxy 在 DNAT 后，NetworkPolicy 看到的是目标 Pod IP，来源仍是发起方 Pod IP。

这意味着：podSelector 过滤的是 Pod 标签，而不是 Service 标签。不能用 NetworkPolicy 来"允许访问某个 Service，但不允许直接访问 Pod IP"——两者在 NetworkPolicy 层面是等价的。

### 陷阱五：CNI 未支持就以为策略生效

```bash
# 检查 CNI 是否支持 NetworkPolicy
kubectl get pods -n kube-system | grep -E "cilium|calico|weave"

# 如果是 flannel：
kubectl get pods -n kube-system | grep flannel
# Flannel 不支持 NetworkPolicy，策略对象存在但完全不执行！

# 验证方式：创建一个 deny-all 策略后测试连通性
# 如果连通性没有变化，说明 CNI 不支持 NetworkPolicy
```

---

## 与 Istio mTLS 的关系

Istio Service Mesh 也提供网络访问控制（AuthorizationPolicy），与 NetworkPolicy 在功能上有重叠，但两者是**不同层面的互补**机制。

### 分层对比

| 维度 | NetworkPolicy | Istio AuthorizationPolicy |
|------|--------------|--------------------------|
| 工作层 | L3/L4（IP/端口）| L7（HTTP/gRPC 应用层）|
| 执行位置 | 内核网络栈（eBPF/iptables）| Envoy Sidecar |
| 身份认证 | Pod IP / 标签 | SPIFFE X.509 证书（mTLS）|
| 加密 | 无 | mTLS 加密 |
| 绕过风险 | 难（内核执行）| 可能被绕过（如果禁用 sidecar）|
| 性能开销 | 极低 | 较高（sidecar proxy 额外延迟） |

### 推荐配合使用

```yaml
# 第一层：NetworkPolicy（L4，快速拒绝非法连接）
# 确保只有合法的命名空间/Pod 才能建立 TCP 连接
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-order-service
  namespace: production
spec:
  podSelector:
    matchLabels:
      app: payment-service
  ingress:
    - from:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: production
          podSelector:
            matchLabels:
              app: order-service
      ports:
        - port: 8080
---
# 第二层：Istio AuthorizationPolicy（L7，基于身份的细粒度控制）
apiVersion: security.istio.io/v1beta1
kind: AuthorizationPolicy
metadata:
  name: payment-service-authz
  namespace: production
spec:
  selector:
    matchLabels:
      app: payment-service
  action: ALLOW
  rules:
    - from:
        - source:
            # 使用 SPIFFE 身份，比 Pod IP 更可靠
            principals:
              - "cluster.local/ns/production/sa/order-service"
      to:
        - operation:
            methods: ["POST"]
            paths: ["/api/payment/charge"]
```

### mTLS 补足 NetworkPolicy 的盲区

NetworkPolicy 无法防止以下场景：
1. **容器逃逸后的主机网络攻击**：如果攻击者通过容器逃逸获得了节点网络权限，可以绕过 NetworkPolicy
2. **合法 Pod 的恶意行为**：某个合法服务被攻陷后，仍然可以以其 Pod 标签身份访问被 NetworkPolicy 允许的目标

Istio mTLS 用加密证书（SPIFFE/X.509）来证明身份，即使 Pod IP 被欺骗，也无法伪造正确的证书完成 mTLS 握手。两者配合构成更完整的防御体系：

- **NetworkPolicy**：粗粒度过滤，阻止非预期的 TCP 连接，性能开销低
- **Istio mTLS + AuthorizationPolicy**：细粒度控制，基于密码学身份，抵御横向移动攻击

---

## 生产部署建议

### 渐进式落地策略

直接在生产集群打开 deny-all 很危险，建议分阶段：

1. **审计阶段**：先用 Cilium 的 `policy-audit` 模式，观察实际流量不拦截，记录哪些流量路径需要放开
2. **命名空间级别逐步收紧**：先从非核心命名空间开始，验证不影响业务后再推广
3. **监控告警**：策略生效后监控 `cilium_drop_count_total` 或 `network_policy_denied` 指标，及时发现误拦截

```yaml
# Cilium 审计模式（不拦截，只记录）
apiVersion: cilium.io/v2
kind: CiliumClusterwideNetworkPolicy
metadata:
  name: audit-all-traffic
spec:
  endpointSelector: {}
  ingress:
    - fromEntities:
        - all
  egress:
    - toEntities:
        - all
# 在 Cilium ConfigMap 中设置
# policyAuditMode: "true"
```

### 策略即代码（Policy as Code）

将所有 NetworkPolicy 存入 Git，通过 GitOps 管理：

```
gitops-repo/
├── base/
│   └── network-policies/
│       ├── deny-all.yaml
│       ├── allow-dns.yaml
│       └── allow-ingress-controller.yaml
├── overlays/
│   ├── production/
│   │   └── network-policies/
│   │       ├── mysql-access.yaml
│   │       └── payment-egress.yaml
│   └── staging/
│       └── network-policies/
│           └── relaxed-mysql-access.yaml  # 测试环境适当放松
```

### 定期策略审查

```bash
# 列出所有命名空间的 NetworkPolicy
kubectl get networkpolicy --all-namespaces

# 找出没有 NetworkPolicy 保护的命名空间（重点检查）
kubectl get namespaces -o name | while read ns; do
    count=$(kubectl get networkpolicy -n ${ns#*/} --no-headers 2>/dev/null | wc -l)
    if [ "$count" -eq 0 ]; then
        echo "⚠️  命名空间 ${ns#*/} 没有 NetworkPolicy"
    fi
done

# 检查没有被任何 NetworkPolicy 选中的 Pod（"孤立" Pod，完全无保护）
# 通过对比 Pod 标签和 NetworkPolicy 的 podSelector 实现（需要自定义脚本）
```

---

## 总结

NetworkPolicy 是 Kubernetes 安全体系的基础组件，核心要点：

1. **CNI 支持是前提**：Cilium / Calico 才有效，Flannel 不支持
2. **从 deny-all 开始**：按需开放比事后补锁安全得多
3. **注意 AND/OR 关系**：同一 `from` 列表项下的 `podSelector` + `namespaceSelector` 是 AND，不同列表项是 OR
4. **显式声明 policyTypes**：避免因隐式规则产生意外的开放
5. **别忘 DNS**：deny-all egress 必须配合 allow-dns
6. **Cilium 扩展 L7**：HTTP/gRPC 方法级别的控制需要 CiliumNetworkPolicy
7. **与 Istio 互补**：NetworkPolicy 做 L4 粗过滤，Istio mTLS 做身份认证和 L7 细粒度控制

NetworkPolicy 的复杂性主要在于"**哪些规则在哪些 Pod 上生效**"的追踪。建议引入 `np-viewer`、Cilium UI 或 Kiali 等可视化工具，让策略的实际效果可观测，而不是只靠 YAML 文件推理。
