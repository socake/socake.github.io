---
title: "Kubernetes 安全加固实践"
date: 2025-12-09T11:00:00+08:00
draft: false
tags: ["Kubernetes", "安全", "RBAC", "NetworkPolicy", "运维"]
categories: ["Kubernetes"]
description: "Kubernetes 安全加固完整指南，覆盖 Pod 安全上下文、PodSecurity Admission、NetworkPolicy、Secret 管理、镜像安全、RBAC 最小权限及 API Server 审计日志配置。"
summary: "K8s 安全加固从 Pod 到集群：SecurityContext 配置、网络策略隔离、Secret 安全管理、镜像漏洞扫描、RBAC 最小权限原则的落地实践。"
toc: true
math: false
diagram: false
keywords: ["Kubernetes", "安全", "SecurityContext", "NetworkPolicy", "RBAC", "Sealed Secrets", "Pod Security"]
params:
  reading_time: true
---

## K8s 安全威胁模型

在开始加固之前，先明确 K8s 的攻击面：

```
外部攻击面：
  - API Server 暴露（未启用认证/授权）
  - Ingress/LoadBalancer 暴露的服务
  - 节点 SSH 暴露

集群内攻击面：
  - 容器逃逸（特权容器/危险能力）
  - Pod 横向移动（无 NetworkPolicy）
  - Secret 泄露（明文存储/宽松权限）
  - 镜像供应链攻击（使用不可信镜像）
  - RBAC 权限过大（Service Account 滥用）

数据面攻击面：
  - etcd 未加密（静态数据）
  - etcd 未启用 TLS（传输数据）
```

**安全加固优先级：**

| 优先级 | 措施 | 影响范围 |
|--------|------|----------|
| P0 | 禁止特权容器、限制 hostPID/hostNetwork | 阻止容器逃逸 |
| P0 | RBAC 最小权限 | 降低横向移动风险 |
| P1 | NetworkPolicy 隔离 | 限制 Pod 间通信 |
| P1 | Secret 加密管理 | 防止凭证泄露 |
| P2 | 镜像扫描 | 降低供应链风险 |
| P2 | 审计日志 | 威胁发现和溯源 |
| P3 | etcd 加密 | 防止数据泄露 |

---

## Pod 安全：SecurityContext

SecurityContext 是 K8s 最直接的容器安全控制手段，分为 Pod 级别和 Container 级别。

### 完整安全配置示例

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: secure-app
  namespace: production
spec:
  replicas: 2
  selector:
    matchLabels:
      app: secure-app
  template:
    metadata:
      labels:
        app: secure-app
    spec:
      # Pod 级别安全上下文
      securityContext:
        runAsNonRoot: true          # 禁止以 root 运行
        runAsUser: 1000             # 指定 UID
        runAsGroup: 1000            # 指定 GID
        fsGroup: 1000               # 挂载卷的 GID
        seccompProfile:
          type: RuntimeDefault      # 使用 runtime 默认 seccomp 配置（限制危险系统调用）

      containers:
        - name: app
          image: my-app:v1.2.3
          securityContext:
            allowPrivilegeEscalation: false   # 禁止提权（最重要的单项配置）
            readOnlyRootFilesystem: true       # 根文件系统只读
            privileged: false                  # 非特权模式
            capabilities:
              drop:
                - ALL                          # 丢弃所有 Linux Capabilities
              add:
                - NET_BIND_SERVICE             # 只保留必要的（按需添加）
          resources:
            requests:
              cpu: "100m"
              memory: "128Mi"
            limits:
              cpu: "500m"
              memory: "256Mi"
          volumeMounts:
            - name: tmp-dir
              mountPath: /tmp              # 如果应用需要写 /tmp，用临时卷
            - name: cache-dir
              mountPath: /app/cache        # 需要写入的目录单独挂载 emptyDir

      volumes:
        - name: tmp-dir
          emptyDir: {}
        - name: cache-dir
          emptyDir: {}

      # 不挂载 Service Account Token（如果不需要访问 K8s API）
      automountServiceAccountToken: false
```

### 关键配置说明

| 配置项 | 推荐值 | 说明 |
|--------|--------|------|
| `runAsNonRoot` | `true` | 强制非 root 运行，镜像必须配合 |
| `allowPrivilegeEscalation` | `false` | 禁止通过 setuid/sudo 提权，最重要 |
| `readOnlyRootFilesystem` | `true` | 根文件系统只读，攻击者无法写入恶意文件 |
| `privileged` | `false` | 特权容器等同于 root 在宿主机，必须禁止 |
| `capabilities.drop: [ALL]` | 必须 | 丢弃所有能力，按需 add |
| `seccompProfile: RuntimeDefault` | 推荐 | 限制约 300 个危险系统调用 |

### 危险配置警告

```yaml
# 以下配置在生产中应被禁止：
securityContext:
  privileged: true              # 危险！等同于宿主机 root
  hostPID: true                 # 危险！可看到宿主机所有进程
  hostNetwork: true             # 危险！共享宿主机网络命名空间
  hostIPC: true                 # 危险！共享宿主机 IPC
  allowPrivilegeEscalation: true  # 危险！允许提权

# 以下 capabilities 极度危险，严禁在生产使用：
capabilities:
  add:
    - SYS_ADMIN     # 几乎等同于 root
    - NET_ADMIN     # 可修改网络配置
    - SYS_PTRACE    # 可 trace 其他进程（容器逃逸利用点）
```

---

## PodSecurity Admission

K8s 1.25 正式 GA 的内置 Pod 安全准入控制器，替代已废弃的 PodSecurityPolicy。

### 三个安全级别

| 级别 | 说明 | 适用场景 |
|------|------|----------|
| `privileged` | 无限制 | 系统级工作负载（监控 agent、CNI 等） |
| `baseline` | 防止已知提权，允许默认配置 | 一般业务应用 |
| `restricted` | 最严格，强制最佳安全实践 | 对安全要求高的应用 |

### 配置方式（Namespace 标签）

```yaml
# 为 Namespace 添加 Pod Security 标签
apiVersion: v1
kind: Namespace
metadata:
  name: production
  labels:
    # enforce：违反直接拒绝
    pod-security.kubernetes.io/enforce: restricted
    pod-security.kubernetes.io/enforce-version: v1.28

    # audit：违反记录审计日志但不拒绝（用于评估影响）
    pod-security.kubernetes.io/audit: restricted
    pod-security.kubernetes.io/audit-version: v1.28

    # warn：违反在 API 响应中返回警告
    pod-security.kubernetes.io/warn: restricted
    pod-security.kubernetes.io/warn-version: v1.28
```

```bash
# 快速为 namespace 添加标签
kubectl label namespace production \
  pod-security.kubernetes.io/enforce=baseline \
  pod-security.kubernetes.io/warn=restricted

# 检查 namespace 的 Pod Security 配置
kubectl get namespace production -o yaml | grep pod-security

# 测试现有工作负载是否符合某个级别（dry-run）
kubectl label namespace production \
  pod-security.kubernetes.io/enforce=restricted \
  --dry-run=server
```

### restricted 级别要求的配置

使用 `restricted` 策略时，Pod 必须满足：

```yaml
spec:
  securityContext:
    runAsNonRoot: true
    seccompProfile:
      type: RuntimeDefault      # 或 Localhost
  containers:
    - securityContext:
        allowPrivilegeEscalation: false
        capabilities:
          drop:
            - ALL
```

---

## NetworkPolicy — 网络隔离

默认情况下，K8s 中所有 Pod 可以互相通信。NetworkPolicy 用来限制流量。

**前提：** CNI 插件必须支持 NetworkPolicy（Calico、Cilium、Weave 支持；Flannel 默认不支持）。

### 默认拒绝所有策略（推荐先设置）

```yaml
# 拒绝 namespace 内所有入站流量
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny-ingress
  namespace: production
spec:
  podSelector: {}    # 匹配所有 Pod
  policyTypes:
    - Ingress        # 应用入站规则
---
# 拒绝 namespace 内所有出站流量
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny-egress
  namespace: production
spec:
  podSelector: {}
  policyTypes:
    - Egress
```

### 允许特定入站流量

```yaml
# 只允许来自 ingress-nginx 的流量访问 web 应用
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-ingress-to-web
  namespace: production
spec:
  podSelector:
    matchLabels:
      app: web-app      # 这条策略作用于带此 label 的 Pod
  policyTypes:
    - Ingress
  ingress:
    - from:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: ingress-nginx   # 来自 ingress-nginx namespace
          podSelector:
            matchLabels:
              app.kubernetes.io/component: controller      # 且是 controller Pod
      ports:
        - protocol: TCP
          port: 8080
```

### 允许同 namespace 内服务互访

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-same-namespace
  namespace: production
spec:
  podSelector: {}    # 所有 Pod
  policyTypes:
    - Ingress
  ingress:
    - from:
        - podSelector: {}    # 来自同 namespace 的任意 Pod
```

### 允许特定出站（如访问数据库）

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: app-egress-policy
  namespace: production
spec:
  podSelector:
    matchLabels:
      app: api-service
  policyTypes:
    - Egress
  egress:
    # 允许访问数据库 namespace 中的 MySQL
    - to:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: database
          podSelector:
            matchLabels:
              app: mysql
      ports:
        - protocol: TCP
          port: 3306

    # 允许 DNS 解析（必须！否则服务发现全部失败）
    - to:
        - namespaceSelector: {}
      ports:
        - protocol: UDP
          port: 53
        - protocol: TCP
          port: 53

    # 允许访问 K8s API Server（如果需要）
    - ports:
        - protocol: TCP
          port: 443
        - protocol: TCP
          port: 6443
```

---

## Secret 安全管理

### 为什么不能直接用 K8s Secret

K8s Secret 默认只是 base64 编码（不是加密），存储在 etcd 中。存在以下风险：

1. 有 etcd 访问权限就能读取所有 Secret
2. Secret YAML 提交到 git → 凭证泄露
3. 任何有 `get secret` RBAC 权限的人都能读

### 方案一：Sealed Secrets（离线加密）

```bash
# 安装 Sealed Secrets Controller
helm repo add sealed-secrets https://bitnami-labs.github.io/sealed-secrets
helm upgrade --install sealed-secrets sealed-secrets/sealed-secrets \
  -n kube-system \
  --set fullnameOverride=sealed-secrets-controller

# 安装客户端工具 kubeseal
curl -L https://github.com/bitnami-labs/sealed-secrets/releases/download/v0.24.0/kubeseal-0.24.0-linux-amd64.tar.gz | tar xz
sudo install -m 755 kubeseal /usr/local/bin/kubeseal

# 创建普通 Secret 并加密为 SealedSecret
kubectl create secret generic db-password \
  --from-literal=password='mysecretpassword' \
  --dry-run=client \
  -o yaml | \
  kubeseal \
  --controller-namespace kube-system \
  --controller-name sealed-secrets-controller \
  --format yaml > sealed-db-password.yaml

# sealed-db-password.yaml 可以安全地提交到 git
git add sealed-db-password.yaml
git commit -m "add encrypted db password"
```

```yaml
# sealed-db-password.yaml 内容示例（加密后的密文）
apiVersion: bitnami.com/v1alpha1
kind: SealedSecret
metadata:
  name: db-password
  namespace: production
spec:
  encryptedData:
    password: AgBy3i4OJSWK+PiTySYZZA9rO43cGDEq...   # 加密后的密文
  template:
    metadata:
      name: db-password
      namespace: production
```

### 方案二：External Secrets Operator（云原生推荐）

从 AWS Secrets Manager / HashiCorp Vault / GCP Secret Manager 等同步 Secret：

```bash
# 安装 External Secrets Operator
helm repo add external-secrets https://charts.external-secrets.io
helm upgrade --install external-secrets external-secrets/external-secrets \
  -n external-secrets \
  --create-namespace \
  --wait
```

```yaml
# SecretStore：定义凭证来源（AWS Secrets Manager）
apiVersion: external-secrets.io/v1beta1
kind: SecretStore
metadata:
  name: aws-secretsmanager
  namespace: production
spec:
  provider:
    aws:
      service: SecretsManager
      region: us-east-1
      auth:
        # 使用 IRSA（EKS 推荐方式，不需要 AK/SK）
        jwt:
          serviceAccountRef:
            name: external-secrets-sa
---
# ExternalSecret：声明要同步哪个 Secret
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: db-credentials
  namespace: production
spec:
  refreshInterval: 1h              # 每小时自动同步
  secretStoreRef:
    name: aws-secretsmanager
    kind: SecretStore
  target:
    name: db-credentials           # 在 K8s 中创建的 Secret 名称
    creationPolicy: Owner
  data:
    - secretKey: password          # K8s Secret 中的 key
      remoteRef:
        key: production/db         # AWS Secrets Manager 中的 key
        property: password         # JSON 字段
```

---

## 镜像安全

### 使用最小基础镜像

```dockerfile
# 不推荐：使用 ubuntu/debian 等完整系统镜像
FROM ubuntu:22.04

# 推荐：使用 distroless（无 shell、无包管理器）
FROM gcr.io/distroless/java17-debian11

# 推荐：使用 alpine（极小，有 busybox）
FROM alpine:3.19

# 推荐：多阶段构建，最终镜像只包含二进制
FROM golang:1.21 AS builder
WORKDIR /app
COPY . .
RUN CGO_ENABLED=0 go build -o /app/server .

FROM gcr.io/distroless/static-debian11   # 只有 CA 证书和时区数据
COPY --from=builder /app/server /server
USER nonroot:nonroot
ENTRYPOINT ["/server"]
```

### 漏洞扫描

```bash
# 使用 Trivy 扫描镜像漏洞（推荐，最全面）
# 安装
curl -sfL https://raw.githubusercontent.com/aquasecurity/trivy/main/contrib/install.sh | sh -s -- -b /usr/local/bin

# 扫描镜像
trivy image my-app:v1.2.3

# 只报告 HIGH 和 CRITICAL 级别漏洞
trivy image --severity HIGH,CRITICAL my-app:v1.2.3

# 扫描并输出 SARIF 格式（可集成到 GitHub Actions）
trivy image --format sarif --output results.sarif my-app:v1.2.3

# 在 CI/CD 中扫描并设置失败阈值
trivy image --exit-code 1 --severity CRITICAL my-app:v1.2.3
# 发现 CRITICAL 漏洞则退出码为 1，阻断构建
```

### imagePullPolicy 和 tag

```yaml
# 生产环境：禁止使用 latest tag
image: my-app:latest        # 危险！不可追溯
image: my-app:v1.2.3        # 推荐：语义化版本
image: my-app:sha256:abc123 # 最严格：digest 固定

# imagePullPolicy 配置
imagePullPolicy: Always         # 每次都拉取（适合 latest，但生产不推荐用 latest）
imagePullPolicy: IfNotPresent   # 本地有则不拉取（生产推荐，配合固定 tag）
imagePullPolicy: Never          # 只用本地（离线环境）
```

---

## RBAC 最小权限

### 原则

- 每个应用使用独立的 Service Account，不共用 `default`
- 只授予实际需要的资源和操作
- 优先用 Role（namespace 级）而不是 ClusterRole

### 标准配置示例

```yaml
# 1. 创建专用 Service Account
apiVersion: v1
kind: ServiceAccount
metadata:
  name: api-service-sa
  namespace: production
automountServiceAccountToken: false   # 默认不挂载，需要时再开启
---
# 2. 定义 Role（最小权限）
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: api-service-role
  namespace: production
rules:
  # 只允许读取 ConfigMap（不允许写入）
  - apiGroups: [""]
    resources: ["configmaps"]
    verbs: ["get", "list", "watch"]
  # 只允许读取特定名称的 Secret
  - apiGroups: [""]
    resources: ["secrets"]
    resourceNames: ["app-config-secret"]   # 限定只能访问这一个 Secret
    verbs: ["get"]
---
# 3. 绑定
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: api-service-binding
  namespace: production
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: api-service-role
subjects:
  - kind: ServiceAccount
    name: api-service-sa
    namespace: production
```

```bash
# 检查某个 Service Account 的权限
kubectl auth can-i get secrets \
  --as=system:serviceaccount:production:api-service-sa \
  -n production

# 检查当前用户所有权限
kubectl auth can-i --list -n production

# 查找有高危权限的 ClusterRoleBinding（排查权限过大）
kubectl get clusterrolebindings -o json | \
  jq '.items[] | select(.roleRef.name == "cluster-admin") | .metadata.name'
```

---

## API Server 审计日志

### 配置审计策略

```yaml
# audit-policy.yaml
apiVersion: audit.k8s.io/v1
kind: Policy
rules:
  # 不记录 kube-system 的只读请求（减少噪音）
  - level: None
    namespaces: ["kube-system"]
    verbs: ["get", "watch", "list"]

  # 不记录 metrics 和健康检查
  - level: None
    nonResourceURLs:
      - /healthz*
      - /readyz*
      - /livez*
      - /metrics

  # 记录 Secret 的所有操作（包含请求元数据，不记录 body，防止密码泄露）
  - level: Metadata
    resources:
      - group: ""
        resources: ["secrets"]

  # 记录所有写操作（create/update/patch/delete）的请求体
  - level: Request
    verbs: ["create", "update", "patch", "delete"]
    omitStages:
      - RequestReceived

  # 其他请求只记录元数据
  - level: Metadata
```

```bash
# kubeadm 集群配置审计（修改 API Server 启动参数）
# /etc/kubernetes/manifests/kube-apiserver.yaml 中添加：
# --audit-log-path=/var/log/kubernetes/audit.log
# --audit-policy-file=/etc/kubernetes/audit-policy.yaml
# --audit-log-maxage=30
# --audit-log-maxbackup=10
# --audit-log-maxsize=100
```

---

## etcd 数据加密

```yaml
# encryption-config.yaml（配置静态加密）
apiVersion: apiserver.config.k8s.io/v1
kind: EncryptionConfiguration
resources:
  - resources:
      - secrets          # 对 Secret 静态加密
    providers:
      - aescbc:
          keys:
            - name: key1
              secret: <base64-encoded-32-byte-key>   # openssl rand -base64 32
      - identity: {}     # 兜底：未加密（用于迁移期间解密旧数据）
```

```bash
# 生成加密 key
openssl rand -base64 32

# 启用后，对现有 Secret 重新加密（使其用新密钥加密存储）
kubectl get secrets -A -o json | kubectl replace -f -

# 验证 etcd 中的 Secret 已加密（数据不再是 base64 明文）
ETCDCTL_API=3 etcdctl \
  --endpoints=https://127.0.0.1:2379 \
  --cacert=/etc/kubernetes/pki/etcd/ca.crt \
  --cert=/etc/kubernetes/pki/etcd/server.crt \
  --key=/etc/kubernetes/pki/etcd/server.key \
  get /registry/secrets/default/my-secret | hexdump -C | head
# 如果看到 k8s:enc:aescbc 开头说明已加密
```

---

## 安全加固 Checklist

```bash
# 检查是否有特权容器
kubectl get pods -A -o json | \
  jq '.items[] | select(.spec.containers[].securityContext.privileged == true) | .metadata.name'

# 检查是否有挂载宿主机路径的 Pod（hostPath）
kubectl get pods -A -o json | \
  jq '.items[] | select(.spec.volumes[]?.hostPath != null) | .metadata.name'

# 检查是否有使用 default Service Account 且自动挂载 token 的 Pod
kubectl get pods -A -o json | \
  jq '.items[] | select(.spec.serviceAccountName == "default" and .spec.automountServiceAccountToken != false) | "\(.metadata.namespace)/\(.metadata.name)"'

# 检查 RBAC 中有 * 权限的 Role
kubectl get roles,clusterroles -A -o json | \
  jq '.items[] | select(.rules[]?.verbs[] == "*") | .metadata.name'

# 检查 cluster-admin 绑定
kubectl get clusterrolebindings -o json | \
  jq '.items[] | select(.roleRef.name == "cluster-admin") | "\(.metadata.name): \(.subjects)"'
```

**上线前安全审查项：**

- [ ] 所有 Pod 配置了 `runAsNonRoot: true`
- [ ] 所有容器配置了 `allowPrivilegeEscalation: false`
- [ ] 所有容器配置了 `capabilities.drop: [ALL]`
- [ ] 无 `privileged: true` 的容器
- [ ] Namespace 配置了 PodSecurity Admission
- [ ] 敏感 Namespace 配置了 NetworkPolicy
- [ ] Secret 未明文提交 git
- [ ] 镜像使用固定 digest 或语义化 tag
- [ ] 通过 Trivy 扫描无 CRITICAL 漏洞
- [ ] Service Account 最小权限，不共用 default
