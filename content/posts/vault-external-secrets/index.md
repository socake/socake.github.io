---
title: "Secret 管理实战：HashiCorp Vault + External Secrets Operator"
date: 2025-02-20T10:20:00+08:00
draft: false
tags: ["Vault", "Secret", "Kubernetes", "安全", "运维"]
categories: ["安全"]
description: "从 Git 泄密事故出发，系统讲解 HashiCorp Vault 架构设计与 External Secrets Operator 在 K8s 中的落地实践，涵盖动态凭证、自动轮换与常见踩坑。"
summary: "base64 不是加密。本文从 Secret 泄露风险说起，完整介绍 Vault 核心概念、K8s 部署方式、ESO 集成配置，以及动态数据库凭证的自动轮换实践。"
toc: true
math: false
diagram: false
keywords: ["HashiCorp Vault", "External Secrets Operator", "Kubernetes Secret", "Secret 管理", "动态凭证"]
params:
  reading_time: true
---

## 为什么不能把 Secret 存进 Git

很多团队刚上 Kubernetes 时，把数据库密码、API Key 直接写进 `secret.yaml`，然后推进了 Git。

```yaml
# 别这样做
apiVersion: v1
kind: Secret
metadata:
  name: db-secret
data:
  password: bXlwYXNzd29yZDEyMw==  # "mypassword123" 的 base64
```

`bXlwYXNzd29yZDEyMw==` 看起来像加密，实际上 base64 是**编码**而不是加密，任何人执行 `echo bXlwYXNzd29yZDEyMw== | base64 -d` 就能还原明文。

更大的风险来自 Git 历史记录。即使你在下一个 commit 删掉了这个文件，`git log` 依然可以翻出历史版本。GitHub 的 secret scanning 每天都在扫描公开仓库里的 AWS Access Key、数据库密码——这些泄露事件发生的概率比你想象的高得多。

我们真正需要的是：
- Secret 不出现在代码仓库（无论是明文还是 base64）
- 不同环境（dev/staging/prod）使用不同凭证
- 凭证有生命周期，定期自动轮换
- 访问凭证有审计日志

这就是 HashiCorp Vault 要解决的问题。

## Vault 核心概念

### Secret Engine

Vault 把不同类型的 Secret 管理能力抽象成"引擎（Secret Engine）"，按需挂载。

**KV（Key-Value）引擎** 是最常用的。KV v2 支持版本历史，方便回滚：

```bash
# 挂载 KV v2 引擎
vault secrets enable -path=secret kv-v2

# 写入一个 Secret
vault kv put secret/myapp/prod db_password="s3cur3p@ss" api_key="abc123"

# 读取
vault kv get secret/myapp/prod
```

**Database 引擎** 是更高级的能力——Vault 动态生成临时数据库账号，用完即销毁：

```bash
vault secrets enable database

vault write database/config/my-postgres \
    plugin_name=postgresql-database-plugin \
    allowed_roles="app-role" \
    connection_url="postgresql://{{username}}:{{password}}@postgres:5432/mydb" \
    username="vault-admin" \
    password="admin-pass"

vault write database/roles/app-role \
    db_name=my-postgres \
    creation_statements="CREATE ROLE '{{name}}' WITH LOGIN PASSWORD '{{password}}' VALID UNTIL '{{expiration}}'; GRANT SELECT ON ALL TABLES IN SCHEMA public TO '{{name}}';" \
    default_ttl="1h" \
    max_ttl="24h"
```

**PKI 引擎** 让 Vault 变成内部 CA，自动签发和吊销 TLS 证书，解决内部服务间 mTLS 证书管理问题。

### Auth Method

Vault 需要先验证调用者的身份，才会颁发 Token 去读取 Secret。

**Kubernetes Auth Method** 是 K8s 场景里最常用的。Pod 自带 ServiceAccount Token，Vault 可以用这个 Token 去验证 K8s API Server，确认"这个 Pod 确实存在于某个 namespace，使用某个 ServiceAccount"。

```bash
vault auth enable kubernetes

vault write auth/kubernetes/config \
    kubernetes_host="https://kubernetes.default.svc" \
    kubernetes_ca_cert=@/var/run/secrets/kubernetes.io/serviceaccount/ca.crt \
    token_reviewer_jwt=@/var/run/secrets/kubernetes.io/serviceaccount/token

# 绑定规则：哪个 namespace + ServiceAccount 可以读什么 Secret
vault write auth/kubernetes/role/myapp \
    bound_service_account_names=myapp-sa \
    bound_service_account_namespaces=production \
    policies=myapp-policy \
    ttl=1h
```

**AppRole** 适合 CI/CD 场景，用 Role ID + Secret ID 两个凭证换取 Token，可以限制 Secret ID 的使用次数（`secret_id_num_uses=1`），用完即失效。

### Policy

Policy 是 Vault 的权限控制层，采用最小权限原则：

```hcl
# myapp-policy.hcl
path "secret/data/myapp/prod/*" {
  capabilities = ["read"]
}

path "database/creds/app-role" {
  capabilities = ["read"]
}

# 禁止删除
path "secret/data/myapp/prod/*" {
  capabilities = ["deny"]
  denied_parameters = {
    "version" = []
  }
}
```

```bash
vault policy write myapp-policy myapp-policy.hcl
```

## K8s 部署 Vault

### Dev 模式（本地测试）

Dev 模式启动快，但 Secret 存内存、重启丢失，仅用于开发测试：

```bash
helm repo add hashicorp https://helm.releases.hashicorp.com
helm install vault hashicorp/vault \
  --set "server.dev.enabled=true" \
  --set "server.dev.devRootToken=root"
```

### 生产 HA 模式

生产环境需要 HA 部署，后端存储用 Raft（Vault 内置分布式存储，不需要额外的 Consul）：

```yaml
# values-prod.yaml
server:
  ha:
    enabled: true
    replicas: 3
    raft:
      enabled: true
      setNodeId: true
      config: |
        ui = true
        listener "tcp" {
          tls_disable = 1
          address = "[::]:8200"
          cluster_address = "[::]:8201"
        }
        storage "raft" {
          path = "/vault/data"
          retry_join {
            leader_api_addr = "http://vault-0.vault-internal:8200"
          }
          retry_join {
            leader_api_addr = "http://vault-1.vault-internal:8200"
          }
          retry_join {
            leader_api_addr = "http://vault-2.vault-internal:8200"
          }
        }
        service_registration "Kubernetes" {}

  dataStorage:
    enabled: true
    size: 20Gi
    storageClass: gp3

injector:
  enabled: true  # Sidecar 注入模式（可选）
```

```bash
helm install vault hashicorp/vault -f values-prod.yaml -n vault --create-namespace
```

初始化：

```bash
# 首次初始化，生成 Unseal Key 和 Root Token
kubectl exec vault-0 -n vault -- vault operator init \
  -key-shares=5 \
  -key-threshold=3 \
  -format=json > vault-init.json

# 保存 vault-init.json 到 KMS 或硬件保险箱，绝对不要存 Git！

# Unseal（需要 threshold 数量的 key）
kubectl exec vault-0 -n vault -- vault operator unseal <unseal-key-1>
kubectl exec vault-0 -n vault -- vault operator unseal <unseal-key-2>
kubectl exec vault-0 -n vault -- vault operator unseal <unseal-key-3>
```

**Auto Unseal** 是生产必备——重启后不需要手动输入 Unseal Key，用 AWS KMS 或阿里云 KMS 自动解封：

```hcl
seal "awskms" {
  region     = "us-west-2"
  kms_key_id = "arn:aws:kms:us-west-2:123456789:key/xxx"
}
```

## External Secrets Operator

手动在 Pod 里调用 Vault API 太繁琐。External Secrets Operator（ESO）是 CNCF 项目，它以 K8s 原生方式把 Vault（以及 AWS SSM、GCP Secret Manager 等）的 Secret 同步成 K8s Secret 对象，应用无感知。

```bash
helm repo add external-secrets https://charts.external-secrets.io
helm install external-secrets external-secrets/external-secrets -n external-secrets --create-namespace
```

### SecretStore

SecretStore 定义"去哪里取 Secret"——连接配置和认证方式：

```yaml
apiVersion: external-secrets.io/v1beta1
kind: SecretStore
metadata:
  name: vault-backend
  namespace: production
spec:
  provider:
    vault:
      server: "http://vault.vault.svc.cluster.local:8200"
      path: "secret"
      version: "v2"
      auth:
        kubernetes:
          mountPath: "Kubernetes"
          role: "myapp"
          serviceAccountRef:
            name: "myapp-sa"
```

如果需要跨 namespace 共享，用 `ClusterSecretStore`（去掉 `namespace` 字段，改用 ClusterSecretStore 类型）。

### ExternalSecret

ExternalSecret 定义"取哪些 key，同步成什么 K8s Secret"：

```yaml
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: myapp-secrets
  namespace: production
spec:
  refreshInterval: "15m"  # 每 15 分钟同步一次
  secretStoreRef:
    name: vault-backend
    kind: SecretStore
  target:
    name: myapp-secret  # 生成的 K8s Secret 名字
    creationPolicy: Owner
    template:
      engineVersion: v2
      data:
        # 可以用 Go template 重新格式化
        DATABASE_URL: "postgresql://{{ .db_user }}:{{ .db_password }}@postgres:5432/mydb"
  data:
    - secretKey: db_user
      remoteRef:
        key: myapp/prod
        property: db_user
    - secretKey: db_password
      remoteRef:
        key: myapp/prod
        property: db_password
  # 也可以批量同步整个路径下所有 key
  dataFrom:
    - extract:
        key: myapp/prod
```

同步后会生成标准的 K8s Secret，Pod 照常使用：

```yaml
env:
  - name: DB_PASSWORD
    valueFrom:
      secretKeyRef:
        name: myapp-secret
        key: db_password
```

## 动态凭证：真正的自动轮换

ESO 同步的是静态 Secret——Vault 里的值改了，K8s Secret 才会更新。Database Engine 提供更高级的能力：**每次请求都生成全新的临时凭证**，有 TTL，过期自动失效。

配置完 Database Engine 之后，ExternalSecret 直接引用动态凭证路径：

```yaml
spec:
  refreshInterval: "45m"  # 在 TTL 到期前刷新
  data:
    - secretKey: db_credentials
      remoteRef:
        key: database/creds/app-role  # 动态凭证路径
```

每次 ESO 刷新，Vault 都会为这个应用生成新的数据库用户名和密码，旧的自动过期。数据库里不会存在长期有效的应用账号。

这种模式的好处：即使凭证泄露，攻击者也只有不到一小时的窗口；泄露发生时，直接吊销 Vault Lease，当前凭证立即失效。

## 踩坑合集

**Vault Seal/Unseal 问题**

生产最常见的坑：Pod 重启后 Vault 进入 sealed 状态，所有请求返回 503。务必配置 Auto Unseal（AWS KMS 或阿里云 KMS），否则半夜 Pod 被 K8s 驱逐，你得爬起来手动 unseal 三次。

**K8s Auth 配置失败**

Vault 用 Token Review API 验证 ServiceAccount，需要给 Vault 的 ServiceAccount 授权：

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: vault-auth
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: system:auth-delegator
subjects:
  - kind: ServiceAccount
    name: vault
    namespace: vault
```

如果 K8s 1.24+ 版本，ServiceAccount Token 不再自动创建 Secret，需要手动创建或者在 Vault 配置时指定 `disable_local_ca_jwt=true`。

**ESO 同步失败排查**

```bash
# 查看 ExternalSecret 状态
kubectl describe externalsecret myapp-secrets -n production

# 看 ESO 控制器日志
kubectl logs -n external-secrets -l app.kubernetes.io/name=external-secrets --tail=100

# 常见原因：
# 1. SecretStore 连不上 Vault（网络策略阻断）
# 2. Kubernetes Auth Role 绑定的 ServiceAccount 不对
# 3. Vault Policy 没有 read 权限
# 4. KV v2 路径要加 /data/，API 路径和 CLI 路径不一样
```

**KV v2 路径混淆**

KV v2 的 API 路径是 `secret/data/myapp/prod`，但 CLI 和 Policy 里写 `secret/myapp/prod`，不少人在 Policy 里把路径写错导致权限拒绝。ESO 配置里 `remoteRef.key` 填的是 CLI 风格路径（不带 `/data/`），ESO 内部会自动处理。

**Secret 轮换时的滚动重启**

ESO 同步更新了 K8s Secret 后，如果 Pod 是通过 env 引用 Secret，更新不会自动触发重启。可以用 Reloader（https://github.com/stakater/Reloader）监听 Secret 变化自动重启 Pod：

```yaml
metadata:
  annotations:
    reloader.stakater.com/auto: "true"
```

## 整体架构总结

一个完整的生产落地链路：

```
开发者 → 写代码（不涉及 Secret）
               ↓
GitOps 仓库 → 只存 ExternalSecret/SecretStore CRD（无敏感值）
               ↓
ArgoCD 同步 → 在 K8s 创建 ExternalSecret 对象
               ↓
ESO Controller → 读取 SecretStore 配置 → 调用 Vault API
               ↓
Vault → 验证 K8s ServiceAccount → 检查 Policy → 返回 Secret
               ↓
ESO → 创建/更新 K8s Secret
               ↓
Pod → 通过 envFrom/volumeMount 使用 Secret
```

这套方案下，Git 仓库里永远不会出现敏感值，审计日志记录每次 Secret 访问，凭证有 TTL 自动过期。初始搭建成本约半天，但长期省去了大量手动轮换密码和处理泄露事件的时间。
