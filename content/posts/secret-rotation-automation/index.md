---
title: "密钥自动轮换实战：Vault、AWS Secrets Manager 与 SOPS 的工程化方案"
date: 2025-11-14T10:00:00+08:00
draft: false
tags: ["Vault", "AWS Secrets Manager", "SOPS", "密钥管理", "零信任"]
categories: ["安全"]
description: "一份来自生产环境的密钥轮换实战笔记：对比 Vault dynamic secret、AWS Secrets Manager 原生 rotation、SOPS + GitOps 三种方案的适用场景，给出数据库、Kafka SASL、TLS 证书、API key 的完整轮换工作流，并分享 ESO 同步、rotation 风暴、灰度发布等真实踩坑。"
summary: "一份来自生产环境的密钥轮换实战笔记：对比 Vault dynamic secret、AWS Secrets Manager 原生 rotation、SOPS + GitOps 三种方案的适用场景，给出数据库、Kafka SASL、TLS 证书、API key 的完整轮换工作流，并分享 ESO 同步、rotation 风暴、灰度发布等真实踩坑。"
toc: true
math: false
diagram: false
keywords: ["密钥轮换", "HashiCorp Vault", "AWS Secrets Manager", "SOPS", "External Secrets Operator"]
params:
  reading_time: true
---

## 为什么密钥轮换这么重要

我在运维这行见过的最"离谱"的事故之一：某互联网公司的一个老员工离职 3 年后，老员工记在笔记本上的 MySQL root 密码依然有效——因为那个密码从来没换过。更离谱的是，事后清查发现同一套 root 密码被用在了 7 个数据库、30+ 台应用服务器的配置文件里。

这种事情每一个长期运维的团队都经历过。它的根源不是"某个人忘了换密码"，而是"**密钥轮换是手工工作，手工工作就会被遗忘**"。零信任的一个核心前提是**短生命周期凭据**，而这意味着你必须做自动化的密钥管理，没有任何例外。

这篇文章我会把过去几年踩过的坑、试过的工具、落地过的方案梳理一遍，覆盖三条主流技术路线：

1. **HashiCorp Vault** + dynamic secrets（动态凭据按需生成）
2. **云原生 Secrets Manager**（AWS SM / Google SM / 阿里云 KMS）+ 原生 rotation
3. **SOPS** + GitOps（静态密钥的安全版本控制 + 定期替换）

三者不是互斥的，生产环境往往是混合使用。这篇文章讲清楚什么场景用什么，以及**具体怎么落地**。

## 一、核心概念：静态密钥 vs 动态密钥

讲方案之前先讲认知。

**静态密钥（static secret）**：一次生成、多次使用、长期有效。比如 MySQL 的 root 密码、API key、TLS 证书、RSA 私钥。这些东西在数据库/服务里注册过，不能随便改。

**动态密钥（dynamic secret）**：按需生成、用完即弃、短生命周期。比如"给这个微服务临时生成一个数据库账号，1 小时后自动删除"。Vault 的 dynamic secret engine 是这个范式的代表。

**轮换（rotation）**：周期性地更换密钥。静态密钥通过 rotation 变得"不那么静态"；动态密钥天生就"自动过期"，不需要显式 rotation。

**零信任的理想状态**：用动态密钥替代一切静态密钥。现实是做不到，因为很多遗留系统只认静态密钥。所以真实方案是"**能动态就动态，动态不了就自动轮换**"。

## 二、方案 1：Vault Dynamic Secrets

### 2.1 为什么 Vault 依然是最强方案

尽管 Vault 的运维成本高（HA、unseal、backup），但它在"**动态密钥**"这件事上依然没有对手。核心能力：

- **DB engine**：为 MySQL/PostgreSQL/MongoDB/Redis/Cassandra 等动态生成临时账号
- **AWS engine**：动态生成 IAM user + access key，用完即删
- **PKI engine**：动态签发 X.509 证书
- **SSH engine**：动态生成 SSH 凭据（OTP 或者 CA 签发）
- **Transit engine**：加密即服务，应用不接触密钥本身
- **KV engine**：静态密钥的安全存储（作为备选）

对于"应用需要连 MySQL"这种经典需求，Vault 的工作流是：

1. 应用启动时向 Vault 认证（通过 k8s SA、AppRole、SPIFFE 等）
2. Vault 验证身份，现场在 MySQL 创建一个带随机名字的临时用户，比如 `v-k8s-app-xxxxxx`
3. Vault 返回 `{username, password, lease_id, ttl: 1h}`
4. 应用用这对凭据连接 MySQL
5. 快到 1 小时时，应用调 Vault `lease renew` 续期；或者让它自然过期
6. Vault 在 TTL 到期后自动从 MySQL 删除这个临时用户

**结果**：没有任何一个长期数据库密码存在于任何地方。即便应用 Pod 被入侵，攻击者拿到的也只是一个 1 小时有效期的账号。

### 2.2 Vault 生产部署要点

Vault 生产部署的坑我这里只点关键的，不展开：

- **HA 用 Raft integrated storage**，别用 Consul backend（已过时）
- **三副本或五副本**，跨 AZ 部署
- **Unseal 用 auto-unseal**，云上用 KMS（AWS KMS / GCP KMS / Aliyun KMS）
- **Audit log 必开**，写到独立的文件或 syslog
- **Snapshot 定时备份**，`vault operator raft snapshot save`
- **Root token 只用于 bootstrap**，bootstrap 后立刻 revoke
- **所有访问走 AppRole / k8s auth / OIDC**，不要长期 token

### 2.3 Database secret engine 配置

以 PostgreSQL 为例，配置步骤：

```bash
# 启用 database engine
vault secrets enable database

# 配置连接
vault write database/config/prod-pg \
    plugin_name=postgresql-database-plugin \
    allowed_roles="readonly,readwrite,migrations" \
    connection_url="postgresql://{{username}}:{{password}}@pg.prod.internal:5432/mydb?sslmode=require" \
    username="vault_admin" \
    password="$VAULT_PG_ADMIN_PASSWORD" \
    password_authentication="scram-sha-256"

# 定义 role (动态账号模板)
vault write database/roles/readonly \
    db_name=prod-pg \
    creation_statements="CREATE ROLE \"{{name}}\" WITH LOGIN PASSWORD '{{password}}' VALID UNTIL '{{expiration}}'; \
                         GRANT SELECT ON ALL TABLES IN SCHEMA public TO \"{{name}}\";" \
    default_ttl="1h" \
    max_ttl="24h"
```

应用侧拉取凭据：

```bash
vault read database/creds/readonly
# Key                Value
# ---                -----
# lease_id           database/creds/readonly/lKxjbVyBdRBqUSGRy9DJJfQh
# lease_duration     1h
# lease_renewable    true
# password           A1a-xxxxxxxxxxxx
# username           v-token-readonly-xxxxxxxxxxx-1697XXXXXX
```

**关键配置点**：

1. **`creation_statements` 里一定要 `VALID UNTIL`**：这是兜底，即便 Vault 自己挂了，临时账号在 `expiration` 后也会被 PostgreSQL 自动禁用。
2. **`default_ttl` 不要太短**：虽然 1 小时听起来不错，但密集启动的 Pod 会对 PostgreSQL master 打出大量 DDL，频繁建删账号。1~4 小时是合理值。
3. **`max_ttl` 控制上限**：避免 lease 续期失控。
4. **管理员凭据本身也要轮换**：`vault_admin` 这个账号的密码可以通过 Vault 的 `root_credentials_rotate_statements` 定期自动换。

### 2.4 K8s 应用集成

Vault 和 K8s 集成的最佳实践是 **Vault Agent Injector**，通过 annotation 自动注入 secret：

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: orders-api
spec:
  template:
    metadata:
      annotations:
        vault.hashicorp.com/agent-inject: "true"
        vault.hashicorp.com/role: "orders-api"
        vault.hashicorp.com/agent-inject-secret-db.conf: "database/creds/readonly"
        vault.hashicorp.com/agent-inject-template-db.conf: |
          {{- with secret "database/creds/readonly" -}}
          DB_HOST=pg.prod.internal
          DB_USER={{ .Data.username }}
          DB_PASS={{ .Data.password }}
          {{- end -}}
        vault.hashicorp.com/agent-inject-file-db.conf: "0400"
```

Vault Agent sidecar 会：
1. 通过 K8s SA token 向 Vault 认证
2. 获取 `database/creds/readonly` 的动态凭据
3. 渲染模板写到 `/vault/secrets/db.conf`
4. TTL 到期前自动续期
5. 续期失败时写新的文件，应用通过 inotify 或者 reload 重新读取

**踩坑**：默认 Agent 续期失败不会删旧文件，应用可能继续用过期凭据。我们通过设置 `exit_on_retry_failure: true` 让 Agent 在续期失败时直接退出，K8s 重建 Pod 强制重新认证。

### 2.5 Go 应用直连 Vault

对于能改代码的应用，直接用 Vault API 比 Agent 更灵活：

```go
import (
    vault "github.com/hashicorp/vault/api"
    "github.com/hashicorp/vault/api/auth/kubernetes"
)

func main() {
    config := vault.DefaultConfig()
    client, _ := vault.NewClient(config)

    // K8s 认证
    k8sAuth, _ := kubernetes.NewKubernetesAuth("orders-api")
    authInfo, _ := client.Auth().Login(ctx, k8sAuth)
    if authInfo == nil {
        log.Fatal("no auth info returned")
    }

    // 取凭据
    secret, _ := client.Logical().Read("database/creds/readonly")
    username := secret.Data["username"].(string)
    password := secret.Data["password"].(string)

    // 建连接
    db, _ := sql.Open("postgres", fmt.Sprintf("postgres://%s:%s@pg.prod.internal/mydb", username, password))

    // 后台 goroutine 续期
    go renewLoop(client, secret)
    ...
}

func renewLoop(client *vault.Client, secret *vault.Secret) {
    watcher, _ := client.NewLifetimeWatcher(&vault.LifetimeWatcherInput{
        Secret: secret,
    })
    go watcher.Start()
    defer watcher.Stop()
    for {
        select {
        case err := <-watcher.DoneCh():
            if err != nil { log.Error(err) }
            // TODO: 取新凭据重建连接池
        case <-watcher.RenewCh():
            log.Info("lease renewed")
        }
    }
}
```

**关键点**：连接池要能在凭据轮换时"无缝切换"，旧连接继续用到结束，新连接用新凭据建立。pgx 连接池支持 `BeforeAcquire` 回调做这事。

## 三、方案 2：AWS Secrets Manager 原生 Rotation

### 3.1 什么情况下用 AWS SM

Vault 强但重，很多团队不想维护一个独立的高可用服务。AWS SM 的优势：

- **零运维**：AWS 托管，HA 内置
- **原生集成 RDS**：打勾就能开启轮换
- **和 IAM 深度集成**：权限控制通过 IAM policy
- **跨 region 复制**：灾备方便
- **成本低**：$0.4/secret/month + API 调用费

劣势：
- 没有动态凭据（不能按需生成临时账号）
- 只能轮换"预先注册的 secret"
- 对非 AWS 资源支持有限

**我的建议**：如果你在 AWS 上、不需要动态凭据、主要是 RDS 这种场景，AWS SM 完全够用。不用强行上 Vault。

### 3.2 RDS 凭据自动轮换

AWS SM 内置了几种 rotation 策略，RDS 场景最常用的是 **"双用户"模式**：

1. 你在 RDS 里预先创建两个用户 `app_user_a` 和 `app_user_b`
2. SM 初始状态指向 `app_user_a`
3. 轮换时 SM 改 `app_user_b` 的密码，secret 指向 `app_user_b`
4. 下次轮换反过来

这样做的好处是：应用永远有一个"刚刚被改过密码的账号"和一个"当前用的账号"。哪怕应用缓存了一段时间的老密码，老账号依然有效（只是过期后会再次被改），不会出现"改密的一瞬间连接全断"的情况。

配置：

```bash
# 创建 rotation function (AWS 提供模板 lambda)
aws secretsmanager rotate-secret \
    --secret-id prod/rds/orders-db \
    --rotation-lambda-arn arn:aws:lambda:us-west-2:xxx:function:SecretsManagerRDSPostgreSQLRotationMultiUser \
    --rotation-rules '{"ScheduleExpression":"rate(7 days)"}'
```

rotation 每 7 天触发一次。Lambda 会：
1. 创建新密码（第一次是生成，之后是 random）
2. 在 RDS 里 `ALTER ROLE ... WITH PASSWORD ...` 
3. 验证新密码能登录
4. 更新 secret 版本（AWSCURRENT 和 AWSPREVIOUS 标签）
5. 如果任何一步失败，回滚

### 3.3 应用读取 secret

应用侧两种方式：

**方式 A：SDK 直接读**（每次启动/定时）：

```python
import boto3, json
sm = boto3.client('secretsmanager')
secret = json.loads(sm.get_secret_value(SecretId='prod/rds/orders-db')['SecretString'])
conn = psycopg2.connect(host=secret['host'], user=secret['username'], password=secret['password'])
```

缺点是每次启动都要调 SM API，大量 Pod 同时启动会打爆速率限制。

**方式 B：External Secrets Operator（ESO）**：把 SM secret 同步到 K8s Secret。

```yaml
apiVersion: external-secrets.io/v1beta1
kind: SecretStore
metadata:
  name: aws-sm
spec:
  provider:
    aws:
      service: SecretsManager
      region: us-west-2
      auth:
        jwt:
          serviceAccountRef:
            name: external-secrets-sa  # 通过 IRSA 认证
---
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: orders-db
spec:
  refreshInterval: 5m
  secretStoreRef:
    name: aws-sm
    kind: SecretStore
  target:
    name: orders-db-secret
    template:
      data:
        DB_HOST: "{{ .host }}"
        DB_USER: "{{ .username }}"
        DB_PASS: "{{ .password }}"
  dataFrom:
    - extract:
        key: prod/rds/orders-db
```

ESO 会每 5 分钟从 SM 拉最新 secret，同步到 K8s Secret 对象。应用通过普通 `env.valueFrom.secretKeyRef` 或 `volumeMounts` 使用。

**坑 1：应用本身不会因为 Secret 变化而重启**。ESO 支持一个 annotation `reloader.stakater.com/auto: "true"`（配合 stakater/reloader controller），或者用 `kubectl rollout restart` 手动触发。

**坑 2：refreshInterval 设太短会打爆 SM API 费用**。5 分钟是个平衡点。更好的做法是设 1 小时 + 订阅 SM 的 EventBridge 事件，rotation 发生时主动推送 ESO 强制刷新。

### 3.4 Kafka SASL/SCRAM 密码轮换

Kafka 的 SASL/SCRAM 密码也能通过类似方式轮换，但 Kafka 本身没有"双用户"机制。我们的方案：

1. Kafka 开启 SCRAM + ACL
2. 每个应用一个 Kafka user
3. SM 存每个 user 的 secret
4. 轮换时 Lambda 通过 Kafka Admin API 改密码（ALTER USER），再更新 secret
5. 应用通过 ESO 同步 secret，配合 reloader 触发 rollout

关键点是应用端要有重试机制——轮换的一瞬间 brokers 可能有几秒不接受旧密码，客户端要能重连。

## 四、方案 3：SOPS + GitOps

有些 secret 既不能动态生成，也不适合走 SM（比如第三方 API key、license 文件）。这类"静态但不能放明文"的 secret，我们用 **SOPS**（Mozilla 出品）管理。

### 4.1 SOPS 基本用法

SOPS 用 KMS/PGP/age 密钥加密 YAML/JSON 文件的 value 部分，key 保留明文，便于 diff。

```yaml
# secrets.yaml (加密后)
apiVersion: v1
kind: Secret
metadata:
  name: third-party-api
stringData:
  STRIPE_KEY: ENC[AES256_GCM,data:xxxxx,iv:yyyy,tag:zzzz]
  SENDGRID_KEY: ENC[AES256_GCM,data:aaaa,iv:bbbb,tag:cccc]
sops:
  kms:
    - arn: arn:aws:kms:us-west-2:xxx:key/yyy
      created_at: "2025-10-01T00:00:00Z"
  age:
    - recipient: age1xxxxxxxxxxxxxxxxxxxx
```

编辑：

```bash
sops secrets.yaml
```

SOPS 自动解密 → 启动 editor → 保存时自动加密回去。多个 KMS/age key 可以同时加密，任一方都能解。

### 4.2 GitOps 集成

SOPS 加密后的文件可以**安全地放进 Git 仓库**。Flux 和 Argo CD 都支持 SOPS 解密：

**Flux 方案**：

```yaml
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: apps
spec:
  interval: 5m
  path: ./apps
  decryption:
    provider: sops
    secretRef:
      name: sops-age-key
```

Flux 在 reconcile 时会自动用 `sops-age-key` 中的 age 私钥解密所有 `*.enc.yaml` 文件，再应用到集群。

**Argo CD 方案**：Argo CD 本身不原生支持 SOPS，但有 plugin `argocd-vault-plugin` 和 `helm-secrets` 可以做类似的事。我个人更喜欢 Flux 的方案因为更简洁。

### 4.3 SOPS 的轮换工作流

SOPS 本身不做自动轮换，但它让"手动轮换"变得可追溯：

1. 需要换 `STRIPE_KEY`：在 Stripe dashboard 生成新 key
2. `sops secrets.yaml`，替换 value，保存
3. git commit + push
4. Flux 同步到集群，应用自动 reload
5. 在 Stripe dashboard 禁用旧 key

整个过程有 Git 历史，任何人都能看到"什么时候轮换过、谁操作的"。比"运维手动登录服务器改配置"可审计得多。

**自动化增强**：写一个定时 job，每 90 天检查每个 secret 的 `sops.lastmodified` 字段，超期发告警推动人工轮换。

```bash
#!/bin/bash
THRESHOLD=$((86400 * 90))  # 90 天
for f in $(find . -name "*.enc.yaml"); do
  LAST=$(sops -d $f | yq '.sops.lastmodified')
  AGE=$(($(date +%s) - $(date -d $LAST +%s)))
  if [ $AGE -gt $THRESHOLD ]; then
    echo "$f 超过 90 天未轮换"
  fi
done
```

## 五、端到端的轮换工作流

把三个方案组合一下，一个成熟的生产环境的密钥管理长这样：

```
┌─────────────────────────────────────────────────────────────┐
│                      密钥类型                                │
├─────────────────────────────────────────────────────────────┤
│  ┌──────────────┐   ┌──────────────┐   ┌──────────────┐    │
│  │ DB 凭据       │   │ 第三方 API Key│   │ TLS 证书     │    │
│  │ SSH 证书      │   │ license 文件  │   │ SPIFFE SVID  │    │
│  │ AWS IAM      │   │               │   │              │    │
│  └──────┬───────┘   └──────┬───────┘   └──────┬───────┘    │
└─────────┼──────────────────┼─────────────────┼─────────────┘
          ▼                  ▼                 ▼
   ┌──────────────┐   ┌──────────────┐  ┌──────────────┐
   │ Vault        │   │ SOPS + Git   │  │ SPIRE/cert-  │
   │ Dynamic      │   │ (手动轮换)    │  │ manager      │
   │ Secrets      │   │               │  │              │
   └──────┬───────┘   └──────┬───────┘  └──────┬───────┘
          │                  │                 │
          └────────┬─────────┴─────────────────┘
                   ▼
          ┌──────────────┐
          │ K8s Pod      │
          │ (Vault Agent │
          │ / ESO / CSI) │
          └──────────────┘
```

原则：

1. **动态优先**：能用 Vault dynamic 的就别用 static
2. **云原生优先**：AWS 上的 RDS 用 AWS SM rotation，Vault 做补充
3. **GitOps 兜底**：不适合上两种方案的，SOPS + Git 明文可审计
4. **SPIFFE 处理身份类凭据**：证书、token 走 SPIRE，别塞进 Vault

## 六、真实踩坑记录

### 6.1 Rotation 风暴

**背景**：我们最早给所有应用配了"每小时轮换一次 DB 凭据"。线上正常，但有一天触发了一个雪崩——几百个 Pod 的 Vault lease 同一分钟到期，同一秒向 Vault 请求新凭据，Vault 又同一秒向 PostgreSQL 打了几百个 CREATE ROLE 语句，PG 的 DDL 锁被打满，应用侧全部超时。

**修复**：
1. lease TTL 加随机抖动（Vault 1.13+ 支持）：`default_lease_ttl: "1h+30m"`
2. Vault Agent 设置 `auth.method.retry.num_retries: 5` + `random_delay: true`
3. PostgreSQL 端加连接池（pgbouncer），DDL 和业务流量隔离
4. lease TTL 拉长到 4~8 小时

### 6.2 ESO 同步延迟

有一次 AWS SM 里改了一个 secret，ESO 本来 5 分钟应该同步，结果 20 分钟后 K8s Secret 还是旧值。根因是 ESO controller OOM 了，chart 默认内存限制 128Mi 对大规模场景不够。提到 512Mi 后恢复。

教训：ESO 的 `refreshInterval` 只是"最多等多久"，实际同步还要看 controller 健康状况，一定要监控：

```
external_secrets_sync_calls_total{status="error"}
external_secrets_sync_calls_duration_seconds_bucket
```

### 6.3 Vault 和 PostgreSQL 的连接池冲突

Vault 的 database plugin 默认维护一个到 PostgreSQL 的连接池。如果 plugin 的连接池大小 > PostgreSQL 的 `max_connections`，会出现"Vault 连不上 PG" 的诡异现象，但其他客户端都能连。

修复：

```
vault write database/config/prod-pg ... \
    max_open_connections=5 \
    max_idle_connections=2 \
    max_connection_lifetime=5m
```

5 个连接足够 Vault 做 DDL 操作。别贪心。

### 6.4 SOPS key 备份丢失

**最惨痛的一次**：某同学删除了公司 KMS 里一个老 key，没意识到那个 key 还在加密着若干个 repo 的 SOPS 文件。那些文件瞬间变成不可解密的砖块。后来我们花了两天从 git 历史里翻出旧版本 + 查 CloudTrail 恢复 key（好在 KMS 有 7 天恢复窗口）。

**教训**：
1. SOPS 必须用**多个 recipient 同时加密**（一个 KMS key + 一个 age key + 一个 PGP key），任一方都能解
2. KMS key 打标签 "sops-encryption-key"，禁止删除
3. age 私钥分发给至少 3 个 admin，独立保存

### 6.5 应用 reload 漏洞

应用侧如果只在启动时读取 secret，不支持热 reload，那轮换就变成"每次都要重启"。改动老应用支持 reload 是大工程。我们的 workaround：

1. 应用前面加 pgbouncer，pgbouncer 支持连接字符串热 reload
2. 应用依然连 pgbouncer，pgbouncer 的 auth 凭据被 Vault Agent 轮换
3. 应用无感

另一种方案是用 K8s 的 projected volume + inotify，应用 watch 配置文件变化自动 reload（Java 的 Spring Cloud Config 就是这么做的）。

### 6.6 lease 泄漏

Vault 的 lease 要被正确释放，否则 PG 里会积累大量未回收的临时账号。我见过一个 PG 实例里有 3000 多个 `v-xxxxx` 账号没清理，是因为应用 crash 时没有 `lease revoke`。

修复：
1. 应用 shutdown hook 里调 `vault lease revoke`
2. 设置 `max_ttl`，即便应用不 revoke，到 max_ttl 也会强制失效
3. 定时任务清理 PG 里"不在 Vault lease 列表"的孤儿账号

## 七、监控与审计

### 7.1 Vault 必开的 metric

```
vault_core_unsealed                      # 是否 unseal (必须 = 1)
vault_runtime_alloc_bytes                # 内存占用
vault_audit_log_request_count            # 审计日志写入
vault_barrier_*                          # barrier 调用
vault_secret_lease_creation_count        # lease 创建速率
vault_expire_num_leases                  # 活跃 lease 数量
vault_token_count_by_auth                # 各 auth 方法的 token 数
```

告警：

```yaml
- alert: VaultSealed
  expr: vault_core_unsealed == 0
  for: 1m
  labels: { severity: critical }
  annotations:
    summary: "Vault 节点 {{ $labels.instance }} 被密封"

- alert: VaultLeaseExplosion
  expr: vault_expire_num_leases > 50000
  for: 5m
  annotations:
    summary: "Vault 活跃 lease 数量异常高"
```

### 7.2 审计日志

Vault audit log 是**必开**的。它记录每一次请求的身份、路径、参数（敏感字段 hash）、响应时间。

```bash
vault audit enable file file_path=/var/log/vault/audit.log
```

审计日志用 Filebeat/Vector 送到 Loki 或者 SIEM。一条典型记录：

```json
{
  "time": "2025-10-15T08:23:11.234Z",
  "type": "response",
  "auth": {
    "client_token_accessor": "xxx",
    "display_name": "kubernetes-orders-api",
    "policies": ["orders-api-read"]
  },
  "request": {
    "operation": "read",
    "path": "database/creds/readonly"
  },
  "response": {
    "data": {
      "lease_id": "hmac-xxxxx",
      "username": "hmac-xxxxx",
      "password": "hmac-xxxxx"
    }
  }
}
```

密码本身是 HMAC 过的，不会泄露，但你能看到"谁什么时候拿了什么凭据"。这是合规审计的核心证据。

### 7.3 定期 review

我们每个季度做一次 secret 审计：

1. 列出所有 SM secret，检查 rotation 策略配置
2. 列出所有 SOPS 文件，检查 lastmodified 超过 90 天的
3. 列出所有 Vault role，检查有哪些长期未被使用（可能已废弃）
4. 列出 K8s Secret 对象，看有没有手写的硬编码明文

这个 review 花不了太多时间（有脚本辅助），但能避免"长期漂移"。

## 八、落地建议

几条实战经验：

**1. 不要一上来就搞 Vault**。Vault 的运维成本很高，大部分场景 AWS SM/GCP SM + ESO + SOPS 就够了。只有当你真的需要动态凭据、需要跨云统一、需要 PKI/SSH 引擎时，Vault 才值得投入。

**2. 从"凭据清点"开始**。落地第一步不是选工具，是搞清楚"**我们现在有多少密钥，都在哪里，谁在用**"。这通常是一个痛苦但有价值的过程。用 trufflehog 扫代码库，用 DataDog Secret Scanner 扫日志，一个一个登记。

**3. 优先处理"血泪级"密钥**。生产数据库密码、云平台 root key、支付系统 API key——这三类优先轮换自动化。其他次之。

**4. 灰度推广**。新接入一个系统先用手动/明文，跑通后再迁移到 Vault/SM。不要一次切换所有东西，出事会找不到根因。

**5. 建立"密钥责任人"机制**。每个 secret 必须有 owner（个人 + team），owner 负责 review 和轮换策略。用 tag 或 label 记录，定期发报告。

**6. 不要忘记员工离职流程**。密钥轮换自动化以后，常常忘掉"离职员工手里的 API key"这种事。必须把 off-boarding 和密钥清单打通。

## 九、结语

零信任做到后面我才意识到，再花哨的 eBPF、SPIRE、Cilium、Falco，只要有一个长期密码躺在某个 YAML 文件里，都等于零——那就是免费的绕过路径。密钥这件事枯燥，但是地基。

下一篇写 Pod Security Standards 的落地，讲"工作负载本身能做什么"的底线防御。
