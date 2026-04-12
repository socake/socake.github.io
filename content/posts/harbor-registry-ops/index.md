---
title: "Harbor 镜像仓库生产运维：高可用、安全扫描与 CI/CD 集成"
date: 2025-02-18T09:30:00+08:00
draft: false
tags: ["Harbor", "容器镜像", "运维", "DevOps", "安全"]
categories: ["DevOps"]
description: "全面覆盖 Harbor 生产运维的核心场景：高可用部署、Trivy 安全扫描、镜像复制、RBAC 权限管理、GC 策略、CI/CD 集成与故障排查"
summary: "从 Harbor 架构原理出发，系统梳理生产环境中高可用部署方案、镜像安全扫描策略、跨区域复制配置、权限体系设计，以及与 Jenkins/GitLab CI 的集成实践，附故障排查手册与 Prometheus 监控配置。"
toc: true
math: false
diagram: false
keywords: ["Harbor", "镜像仓库", "Trivy", "镜像扫描", "镜像复制", "RBAC", "GC", "CI/CD", "高可用"]
params:
  reading_time: true
---

Harbor 是 CNCF 毕业的企业级容器镜像仓库，在我们生产环境中承担着所有微服务镜像的存储、分发和安全扫描职责。这篇文章整理了近两年运维 Harbor 的核心经验，涵盖架构理解、高可用部署、安全体系、权限管理到日常故障处理的完整链路。

## Harbor 架构解析

理解 Harbor 的组件职责是做好运维的前提。Harbor 采用微服务架构，各组件通过内部 HTTP API 和数据库协调工作。

### 核心组件职责

```
┌─────────────────────────────────────────────────────────────┐
│                        Harbor 架构                           │
├─────────────┬──────────────┬──────────────┬─────────────────┤
│   Portal    │     Core     │   Registry   │   JobService    │
│  (前端 UI)  │  (业务逻辑)   │  (镜像存储)   │  (异步任务队列)  │
├─────────────┴──────────────┴──────────────┴─────────────────┤
│              PostgreSQL        Redis                         │
│           (元数据存储)      (缓存/会话/队列)                   │
├─────────────────────────────────────────────────────────────┤
│         Trivy / Clair（可插拔扫描器）                         │
│         Notary（镜像签名，可选）                               │
└─────────────────────────────────────────────────────────────┘
```

**Portal**：基于 Angular 的 Web UI，通过 Nginx 反向代理转发请求到 Core。纯前端静态资源，无状态，可水平扩展。

**Core**：Harbor 的大脑，处理所有业务逻辑：
- 用户认证与授权（内置数据库 / LDAP / OIDC）
- 镜像元数据管理（项目、仓库、Tag 信息存入 PostgreSQL）
- Webhook 触发与通知
- 镜像复制策略的调度
- 与 Registry 的 token 认证对接

**Registry**：底层使用 Docker Distribution（现 distribution/distribution），负责实际的镜像层存储和 OCI manifest 管理。Core 通过 token 机制控制 Registry 的访问权限，Registry 本身不做鉴权判断。

**JobService**：异步任务执行引擎，处理：
- 镜像扫描任务
- 跨仓库复制任务
- 垃圾回收（GC）任务
- Webhook 投递重试

任务状态持久化到 Redis，支持重启恢复。

**PostgreSQL**：存储所有元数据：用户表、项目表、仓库表、Tag 表、扫描结果、复制规则、Webhook 配置等。这是 Harbor 的关键单点，生产必须做高可用。

**Redis**：多重用途：
- JobService 任务队列（基于 Redis List）
- Core 的会话缓存
- Registry 的 blob 上传临时状态
- 速率限制计数器

### 数据流：一次 `docker push` 的完整路径

```
docker push harbor.example.com/myproject/myapp:v1.0

1. Docker CLI → Nginx（TLS 终止）
2. Nginx → Core（/v2/ 路由）
3. Core 验证 Basic Auth，查询 PostgreSQL 确认项目权限
4. Core 生成短期 JWT token，返回给 Docker CLI
5. Docker CLI 携带 token → Registry（/v2/ 路由）
6. Registry 验证 token（公钥验签），接受 blob 上传
7. 镜像层写入后端存储（S3 / NFS / 本地磁盘）
8. Registry 通知 Core：新 manifest 已提交
9. Core 更新 PostgreSQL 元数据（artifact 记录）
10. 如配置了自动扫描，Core 向 JobService 投递扫描任务
11. JobService 调用 Trivy 扫描，结果写入 PostgreSQL
```

## 高可用部署方案

### 双节点 Harbor + 共享存储

生产推荐的最小 HA 方案：两个 Harbor 实例共享同一套存储和数据库，前置负载均衡。

```
                    ┌─────────────────┐
                    │   Load Balancer │
                    │  (Nginx/HAProxy)│
                    └────────┬────────┘
                             │
              ┌──────────────┴──────────────┐
              │                             │
     ┌────────▼────────┐           ┌────────▼────────┐
     │   Harbor Node1  │           │   Harbor Node2  │
     │  Core+Registry  │           │  Core+Registry  │
     │  Portal+Job     │           │  Portal+Job     │
     └────────┬────────┘           └────────┬────────┘
              │                             │
              └──────────────┬──────────────┘
                             │
               ┌─────────────┴─────────────┐
               │                           │
      ┌────────▼────────┐       ┌──────────▼──────────┐
      │   PostgreSQL HA  │       │     Redis Sentinel   │
      │  (主从 + VIP)    │       │   (3节点高可用)      │
      └─────────────────┘       └─────────────────────┘
                             │
                    ┌────────▼────────┐
                    │  共享存储后端    │
                    │  S3 / NFS / OSS │
                    └─────────────────┘
```

### 使用 S3 作为镜像存储后端

Harbor 的 Registry 组件支持多种存储驱动，生产推荐 S3 兼容存储（AWS S3、阿里云 OSS、MinIO）。

`harbor.yml` 关键配置：

```yaml
# harbor.yml
storage_service:
  s3:
    accesskey: AKIAIOSFODNN7EXAMPLE
    secretkey: wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY
    region: us-west-2
    bucket: harbor-registry-prod
    encrypt: true
    secure: true
    # 开启分块上传加速大镜像
    multipartcopychunksize: 33554432  # 32MB
    multipartcopymaxconcurrency: 100
    multipartcopythresholdsize: 33554432
```

S3 方案的优势：
- 存储无限扩展，无需管理磁盘
- 跨 AZ 数据冗余，无单点故障
- 两个 Harbor 节点直接共享同一 bucket，无需同步
- 结合 S3 生命周期策略可做成本优化

### NFS 共享存储方案

如果不使用云存储，NFS 是备选方案，但需要注意性能瓶颈：

```bash
# NFS 服务端配置（/etc/exports）
/data/harbor-registry  harbor-node1(rw,sync,no_subtree_check,no_root_squash)
/data/harbor-registry  harbor-node2(rw,sync,no_subtree_check,no_root_squash)

# harbor.yml 存储配置
storage_service:
  filesystem:
    rootdirectory: /data/registry  # 挂载 NFS 的路径
```

NFS 注意事项：
- 大并发推拉时 NFS 可能成为瓶颈，建议使用万兆网络
- 必须配置 NFS 高可用（如 DRBD + Keepalived），否则反而引入单点
- 定期检查 NFS inode 使用率，小文件多时容易耗尽

### Harbor Helm Chart 部署（Kubernetes 上）

```yaml
# values.yaml 关键配置
expose:
  type: ingress
  tls:
    enabled: true
    certSource: secret
    secret:
      secretName: harbor-tls
  ingress:
    hosts:
      core: harbor.example.com
    className: nginx
    annotations:
      nginx.ingress.kubernetes.io/proxy-body-size: "0"  # 关键！允许大镜像上传
      nginx.ingress.kubernetes.io/proxy-read-timeout: "900"

externalURL: https://harbor.example.com

# 使用外部数据库
database:
  type: external
  external:
    host: postgres-harbor.db.internal
    port: "5432"
    username: harbor
    password: ${HARBOR_DB_PASSWORD}
    coreDatabase: registry

# 使用外部 Redis
redis:
  type: external
  external:
    addr: redis-harbor.cache.internal:6379
    password: ${HARBOR_REDIS_PASSWORD}

# S3 存储
persistence:
  persistentVolumeClaim:
    registry:
      storageClass: ""  # 不使用 PVC
  imageChartStorage:
    type: s3
    s3:
      region: us-west-2
      bucket: harbor-registry-prod
      accesskey: ${AWS_ACCESS_KEY}
      secretkey: ${AWS_SECRET_KEY}
      encrypt: true

# 副本数
core:
  replicas: 2
jobservice:
  replicas: 2
registry:
  replicas: 2
```

## 镜像安全扫描

### Trivy 集成配置

Harbor 1.10+ 原生集成 Trivy，推荐使用 Trivy 替代旧版 Clair。

在 Harbor UI → 配置 → 系统设置 → 扫描器中确认 Trivy 已启用。通过 API 验证扫描器状态：

```bash
curl -u admin:Harbor12345 \
  https://harbor.example.com/api/v2.0/scanners | jq '.[].name'
# 输出: "Trivy"
```

**Trivy 离线漏洞库更新**（生产环境通常无法直连外网）：

```bash
# 在有网络的机器下载漏洞库
trivy image --download-db-only
# 打包上传到内网
tar -czf trivy-db.tar.gz ~/.cache/trivy/db/

# Harbor Trivy 组件使用独立的数据库目录
# 通过 harbor.yml 配置离线 DB 路径
trivy:
  ignore_unfixed: false
  skip_update: false  # 改为 true 后使用离线 DB
  offline_scan: false
  # 企业内网场景下配置代理
  github_token: ""
```

### CVE 告警策略

**项目级别扫描策略**（推荐）：

在 Harbor UI → 项目 → 配置 → 防止有漏洞的镜像运行：

```
阻止镜像拉取的严重级别: Critical / High
```

通过 API 批量配置所有项目：

```bash
#!/bin/bash
# 为所有项目开启漏洞阻断策略
HARBOR_URL="https://harbor.example.com"
HARBOR_USER="admin"
HARBOR_PASS="Harbor12345"

# 获取所有项目 ID
projects=$(curl -s -u "${HARBOR_USER}:${HARBOR_PASS}" \
  "${HARBOR_URL}/api/v2.0/projects?page_size=100" | jq '.[].project_id')

for project_id in $projects; do
  curl -s -u "${HARBOR_USER}:${HARBOR_PASS}" \
    -X PUT \
    -H "Content-Type: application/json" \
    "${HARBOR_URL}/api/v2.0/projects/${project_id}" \
    -d '{
      "metadata": {
        "prevent_vul": "true",
        "severity": "high",
        "auto_scan": "true"
      }
    }'
  echo "Updated project ${project_id}"
done
```

**推送阶段阻断**（更严格）：

结合 CI/CD 在推送后立即触发扫描并等待结果：

```bash
#!/bin/bash
# ci-scan-check.sh - 在 CI 流水线中扫描并阻断高危镜像
IMAGE="harbor.example.com/myproject/myapp:${CI_COMMIT_SHA}"

# 推送镜像
docker push "${IMAGE}"

# 触发扫描（通过 API）
REPO=$(echo "${IMAGE}" | cut -d'/' -f2-)
REPO_ENCODED=$(python3 -c "import urllib.parse; print(urllib.parse.quote('${REPO}', safe=''))")

curl -s -u "${HARBOR_USER}:${HARBOR_PASS}" \
  -X POST \
  "https://harbor.example.com/api/v2.0/projects/myproject/repositories/${REPO_ENCODED}/artifacts/${CI_COMMIT_SHA}/scan"

# 轮询扫描状态（最多等待 5 分钟）
for i in $(seq 1 60); do
  STATUS=$(curl -s -u "${HARBOR_USER}:${HARBOR_PASS}" \
    "https://harbor.example.com/api/v2.0/projects/myproject/repositories/${REPO_ENCODED}/artifacts/${CI_COMMIT_SHA}" \
    | jq -r '.scan_overview."application/vnd.security.vulnerability.report; version=1.1".scan_status')

  if [ "${STATUS}" = "Success" ]; then
    # 检查高危漏洞数量
    CRITICAL=$(curl -s -u "${HARBOR_USER}:${HARBOR_PASS}" \
      "https://harbor.example.com/api/v2.0/projects/myproject/repositories/${REPO_ENCODED}/artifacts/${CI_COMMIT_SHA}" \
      | jq '.scan_overview."application/vnd.security.vulnerability.report; version=1.1".summary.summary.Critical // 0')

    HIGH=$(curl -s -u "${HARBOR_USER}:${HARBOR_PASS}" \
      "https://harbor.example.com/api/v2.0/projects/myproject/repositories/${REPO_ENCODED}/artifacts/${CI_COMMIT_SHA}" \
      | jq '.scan_overview."application/vnd.security.vulnerability.report; version=1.1".summary.summary.High // 0')

    if [ "${CRITICAL}" -gt 0 ] || [ "${HIGH}" -gt 5 ]; then
      echo "❌ 扫描失败: Critical=${CRITICAL}, High=${HIGH}，阻断部署"
      exit 1
    fi
    echo "✅ 扫描通过: Critical=${CRITICAL}, High=${HIGH}"
    exit 0
  fi

  echo "扫描进行中... (${i}/60)"
  sleep 5
done

echo "扫描超时，按策略阻断部署"
exit 1
```

### 忽略规则管理

某些 CVE 属于误报或暂无修复版本，可通过 Harbor 的 CVE 忽略列表处理：

```bash
# 系统级 CVE 忽略（对所有项目生效）
curl -u admin:Harbor12345 \
  -X POST \
  -H "Content-Type: application/json" \
  "https://harbor.example.com/api/v2.0/system/CVEAllowlist" \
  -d '{
    "items": [
      {"cve_id": "CVE-2023-44487"},
      {"cve_id": "CVE-2024-1234"}
    ],
    "expires_at": 1735689600
  }'
```

## 镜像复制与同步

### 跨区域复制策略

Harbor 的复制功能支持推（Push）和拉（Pull）两种模式，以及基于过滤规则的精细化复制。

**场景一：主仓库 → 多区域分发**

```yaml
# 复制规则配置（通过 API）
POST /api/v2.0/replication/policies

{
  "name": "us-to-cn-sync",
  "src_registry": {
    "id": 1  # 源 Harbor（US 区）
  },
  "dest_registry": {
    "id": 2  # 目标 Harbor（CN 区）
  },
  "dest_namespace": "production",
  "dest_namespace_replace_count": 1,
  "filters": [
    {
      "type": "name",
      "value": "production/**"  # 只复制 production 项目
    },
    {
      "type": "tag",
      "value": "v*"  # 只复制 v 开头的版本标签（排除 latest/dev）
    }
  ],
  "trigger": {
    "type": "scheduled",
    "trigger_settings": {
      "cron": "0 2 * * *"  # 每天凌晨 2 点同步
    }
  },
  "enabled": true,
  "deletion": false,  # 源仓库删除时不同步删除（安全起见）
  "override": true,
  "speed": 10  # 限速 10 MB/s，避免占用带宽
}
```

**场景二：事件驱动实时复制**

```json
{
  "trigger": {
    "type": "event_based",
    "trigger_settings": {}
  }
}
```

事件驱动复制在 push 完成后立即触发，适合需要快速分发的场景，但会增加跨区域带宽消耗。

### 复制任务监控

```bash
# 查看复制任务执行状态
curl -u admin:Harbor12345 \
  "https://harbor.example.com/api/v2.0/replication/executions?policy_id=1&page_size=20" \
  | jq '.[] | {id, status, start_time, end_time, total, succeed, failed}'

# 查看失败的任务详情
curl -u admin:Harbor12345 \
  "https://harbor.example.com/api/v2.0/replication/executions/123/tasks?status=Failed" \
  | jq '.[] | {resource_url, error_msg}'
```

**常见复制失败原因**：
- 目标仓库项目不存在（需提前创建或开启自动创建）
- 网络超时（调大 `timeout` 参数或检查防火墙）
- 目标仓库磁盘空间不足
- 认证信息过期（检查 Endpoint 的账号密码）

## RBAC 权限管理

### Harbor 权限模型

Harbor 有两层权限：

1. **系统级角色**：Harbor Admin（超级管理员）、普通用户
2. **项目级角色**：Project Admin、Maintainer、Developer、Guest、Limited Guest

| 角色 | 推送镜像 | 拉取镜像 | 删除镜像 | 管理成员 | 扫描镜像 |
|------|---------|---------|---------|---------|---------|
| Project Admin | ✅ | ✅ | ✅ | ✅ | ✅ |
| Maintainer | ✅ | ✅ | ✅ | ❌ | ✅ |
| Developer | ✅ | ✅ | ❌ | ❌ | ❌ |
| Guest | ❌ | ✅ | ❌ | ❌ | ❌ |
| Limited Guest | ❌ | 部分 | ❌ | ❌ | ❌ |

### LDAP 集成配置

```yaml
# harbor.yml LDAP 配置
auth_mode: ldap_auth

ldap:
  url: ldaps://ldap.example.com:636
  base_dn: dc=example,dc=com
  search_dn: cn=harbor-bind,ou=service-accounts,dc=example,dc=com
  search_password: ${LDAP_BIND_PASSWORD}
  uid: sAMAccountName  # AD 使用 sAMAccountName，OpenLDAP 使用 uid
  scope: 2  # subtree
  timeout: 5
  verify_certificate: true
  group_base_dn: ou=groups,dc=example,dc=com
  group_search_filter: (objectClass=groupOfNames)
  group_attribute_name: cn
  group_admin_dn: cn=harbor-admins,ou=groups,dc=example,dc=com
```

验证 LDAP 配置：

```bash
curl -u admin:Harbor12345 \
  -X POST \
  -H "Content-Type: application/json" \
  "https://harbor.example.com/api/v2.0/ldap/ping" \
  -d '{"ldap_url": "ldaps://ldap.example.com:636", ...}'
```

### OIDC 集成（Keycloak / Dex）

```yaml
auth_mode: oidc_auth

oidc:
  name: Keycloak
  endpoint: https://keycloak.example.com/realms/harbor
  client_id: harbor
  client_secret: ${OIDC_CLIENT_SECRET}
  scope: "openid,profile,email,groups"
  groups_claim: groups
  admin_group: harbor-admins
  verify_certificate: true
  auto_onboard: true  # 首次登录自动创建 Harbor 账号
  user_claim: preferred_username  # 用作 Harbor 用户名的字段
```

### 机器人账号管理（CI/CD 专用）

生产实践：每个 CI/CD 项目使用独立的 Robot Account，而非共享管理员账号。

```bash
# 创建项目级 Robot Account
curl -u admin:Harbor12345 \
  -X POST \
  -H "Content-Type: application/json" \
  "https://harbor.example.com/api/v2.0/projects/myproject/robots" \
  -d '{
    "name": "ci-pipeline",
    "description": "GitLab CI 流水线专用",
    "duration": 365,
    "access": [
      {"resource": "/project/myproject/repository", "action": "push"},
      {"resource": "/project/myproject/repository", "action": "pull"},
      {"resource": "/project/myproject/artifact", "action": "read"}
    ]
  }'

# 返回值中包含 token，只显示一次，需立即保存到 CI 变量
```

**系统级 Robot Account**（需要跨项目操作的场景，如全局复制任务）：

```bash
curl -u admin:Harbor12345 \
  -X POST \
  -H "Content-Type: application/json" \
  "https://harbor.example.com/api/v2.0/robots" \
  -d '{
    "name": "replication-bot",
    "level": "system",
    "duration": -1,
    "permissions": [
      {
        "kind": "system",
        "namespace": "/",
        "access": [
          {"resource": "replication", "action": "read"},
          {"resource": "replication", "action": "execute"}
        ]
      }
    ]
  }'
```

## 生产配置优化

### GC（垃圾回收）策略

Harbor GC 分两步：
1. **标记阶段**：遍历所有 manifest，收集被引用的 blob digest
2. **清除阶段**：删除未被引用的 blob 文件

**GC 策略配置**（建议在业务低峰期执行）：

```
Harbor UI → 系统管理 → 垃圾清理

调度: 0 2 * * 0  （每周日凌晨 2 点）
删除未打 Tag 的 artifact: ✅
```

通过 API 触发手动 GC：

```bash
curl -u admin:Harbor12345 \
  -X POST \
  -H "Content-Type: application/json" \
  "https://harbor.example.com/api/v2.0/system/gc/schedule" \
  -d '{"schedule": {"type": "Manual"}}'

# 查看 GC 执行记录
curl -u admin:Harbor12345 \
  "https://harbor.example.com/api/v2.0/system/gc?page_size=10" \
  | jq '.[] | {id, job_status, creation_time, update_time}'
```

### 镜像保留规则

镜像保留（Retention）规则防止历史版本无限积累，是控制存储成本的关键配置。

```bash
# 为项目配置保留规则
curl -u admin:Harbor12345 \
  -X POST \
  -H "Content-Type: application/json" \
  "https://harbor.example.com/api/v2.0/retentions" \
  -d '{
    "algorithm": "or",
    "rules": [
      {
        "disabled": false,
        "action": "retain",
        "template": "latestPushedK",
        "params": {
          "latestK": 10
        },
        "tag_selectors": [
          {
            "kind": "doublestar",
            "decoration": "matches",
            "pattern": "v*"
          }
        ],
        "scope_selectors": {
          "repository": [
            {
              "kind": "doublestar",
              "decoration": "repoMatches",
              "pattern": "**"
            }
          ]
        }
      },
      {
        "disabled": false,
        "action": "retain",
        "template": "always",
        "tag_selectors": [
          {
            "kind": "doublestar",
            "decoration": "matches",
            "pattern": "latest"
          }
        ]
      }
    ],
    "scope": {
      "level": "project",
      "ref": 1
    },
    "trigger": {
      "kind": "Schedule",
      "settings": {
        "cron": "0 4 * * 1"
      },
      "references": {}
    }
  }'
```

规则含义：对所有仓库保留最近 10 个 `v*` 格式的 Tag，以及永久保留 `latest`。

### JVM 与性能调优

```yaml
# docker-compose.yml（bare metal 部署时）
core:
  environment:
    - JAVA_OPTS=-Xmx1g -Xms512m -XX:+UseG1GC
    
jobservice:
  environment:
    # 并发扫描任务数（根据 CPU 核数调整）
    - MAX_JOB_WORKERS=10
    # 日志保留天数
    - JOB_LOGGER_SWEEPER_DURATION=1

registry:
  environment:
    # Registry 存储删除开关，GC 时必须开启
    - REGISTRY_STORAGE_DELETE_ENABLED=true
```

## 与 CI/CD 集成

### Jenkins 集成

```groovy
// Jenkinsfile
pipeline {
    agent any
    
    environment {
        HARBOR_URL = 'harbor.example.com'
        HARBOR_PROJECT = 'production'
        IMAGE_NAME = 'myapp'
        // 使用 Jenkins Credentials 存储 Robot Account token
        HARBOR_CREDS = credentials('harbor-robot-ci')
    }
    
    stages {
        stage('Build') {
            steps {
                script {
                    def imageTag = "${HARBOR_URL}/${HARBOR_PROJECT}/${IMAGE_NAME}:${BUILD_NUMBER}"
                    sh "docker build -t ${imageTag} ."
                }
            }
        }
        
        stage('Push to Harbor') {
            steps {
                script {
                    def imageTag = "${HARBOR_URL}/${HARBOR_PROJECT}/${IMAGE_NAME}:${BUILD_NUMBER}"
                    sh """
                        echo ${HARBOR_CREDS_PSW} | docker login ${HARBOR_URL} \
                          -u ${HARBOR_CREDS_USR} --password-stdin
                        docker push ${imageTag}
                    """
                }
            }
        }
        
        stage('Security Scan') {
            steps {
                script {
                    // 等待扫描完成并检查结果
                    sh "./scripts/harbor-scan-check.sh ${imageTag}"
                }
            }
        }
    }
    
    post {
        always {
            sh "docker logout ${HARBOR_URL}"
        }
    }
}
```

### GitLab CI 集成

```yaml
# .gitlab-ci.yml
variables:
  HARBOR_URL: harbor.example.com
  HARBOR_PROJECT: production
  IMAGE_TAG: ${HARBOR_URL}/${HARBOR_PROJECT}/${CI_PROJECT_NAME}:${CI_COMMIT_SHORT_SHA}

stages:
  - build
  - push
  - scan

build-image:
  stage: build
  image: docker:24
  services:
    - docker:24-dind
  script:
    - docker build -t ${IMAGE_TAG} .
    - docker save ${IMAGE_TAG} | gzip > image.tar.gz
  artifacts:
    paths:
      - image.tar.gz
    expire_in: 1 hour

push-to-harbor:
  stage: push
  image: docker:24
  services:
    - docker:24-dind
  before_script:
    - echo ${HARBOR_ROBOT_TOKEN} | docker login ${HARBOR_URL} -u ${HARBOR_ROBOT_USER} --password-stdin
  script:
    - docker load < image.tar.gz
    - docker push ${IMAGE_TAG}
    # 同时打 latest 标签（仅 main 分支）
    - |
      if [ "${CI_COMMIT_BRANCH}" = "main" ]; then
        LATEST_TAG="${HARBOR_URL}/${HARBOR_PROJECT}/${CI_PROJECT_NAME}:latest"
        docker tag ${IMAGE_TAG} ${LATEST_TAG}
        docker push ${LATEST_TAG}
      fi
  after_script:
    - docker logout ${HARBOR_URL}

harbor-scan-check:
  stage: scan
  image: alpine/curl:latest
  script:
    - ./scripts/wait-for-scan.sh ${IMAGE_TAG}
  allow_failure: false
```

### Kubernetes 集群拉取 Harbor 镜像

```bash
# 创建 imagePullSecret
kubectl create secret docker-registry harbor-pull-secret \
  --docker-server=harbor.example.com \
  --docker-username=robot\$myproject+ci-pull \
  --docker-password=<robot_account_token> \
  --namespace=production

# 或者配置 ServiceAccount 默认使用
kubectl patch serviceaccount default \
  -n production \
  -p '{"imagePullSecrets": [{"name": "harbor-pull-secret"}]}'
```

## 故障排查

### 推送失败排查

**症状**：`docker push` 返回 `unauthorized: unauthorized to access repository`

```bash
# 1. 确认认证是否成功
curl -v -u username:password \
  https://harbor.example.com/service/token?service=harbor-registry&scope=repository:myproject/myapp:push

# 2. 检查项目是否存在，用户是否有 Developer 以上权限
curl -u admin:Harbor12345 \
  "https://harbor.example.com/api/v2.0/projects/myproject/members" \
  | jq '.[] | select(.entity_name == "myuser")'

# 3. 检查 Core 组件日志
docker logs harbor-core 2>&1 | grep "ERROR\|WARN" | tail -50
# K8s 部署
kubectl logs -n harbor deployment/harbor-core --tail=100 | grep -E "ERROR|WARN"
```

**症状**：`docker push` 卡在上传某一层

```bash
# 检查 Registry 日志
kubectl logs -n harbor deployment/harbor-registry --tail=100

# 检查存储后端连通性（S3）
kubectl exec -n harbor deployment/harbor-registry -- \
  curl -I https://harbor-registry.s3.us-west-2.amazonaws.com/

# 检查 Nginx 超时配置
# nginx.conf 中确认 proxy_read_timeout 足够大（建议 900s）
kubectl get configmap -n harbor harbor-nginx -o yaml | grep timeout
```

### GC 卡住排查

GC 任务执行时会将 Registry 切换到只读模式，如果 GC 异常终止，Registry 将无法写入。

```bash
# 检查 Registry 是否处于只读模式
kubectl exec -n harbor deployment/harbor-registry -- \
  cat /etc/registry/config.yml | grep -A5 storage

# 如果 readonly.enabled: true，手动关闭只读模式
kubectl exec -n harbor deployment/harbor-registry -- \
  sed -i 's/enabled: true/enabled: false/' /etc/registry/config.yml
kubectl rollout restart -n harbor deployment/harbor-registry

# 清理僵尸 GC 任务
kubectl exec -n harbor deployment/harbor-core -- \
  psql -U postgres -d registry -c \
  "UPDATE admin_job SET status='Error', update_time=now() WHERE job_type='gc' AND status='Running';"
```

### 数据库连接问题

```bash
# 检查 PostgreSQL 连接数
kubectl exec -n harbor deployment/harbor-core -- \
  psql -h harbor-database -U postgres -d registry -c \
  "SELECT count(*), state FROM pg_stat_activity GROUP BY state;"

# Harbor Core 连接池配置（docker-compose 方式）
# 环境变量
- POSTGRESQL_MAX_IDLE_CONNS=50
- POSTGRESQL_MAX_OPEN_CONNS=1000

# 检查数据库大小
kubectl exec -n harbor deployment/harbor-core -- \
  psql -h harbor-database -U postgres -c \
  "SELECT pg_database.datname, pg_size_pretty(pg_database_size(pg_database.datname)) FROM pg_database ORDER BY pg_database_size(pg_database.datname) DESC;"
```

### 磁盘空间告警处理

```bash
# 快速查看各存储桶/目录占用（S3 场景）
aws s3 ls s3://harbor-registry-prod --recursive --human-readable --summarize \
  | tail -5

# 临时紧急清理：删除所有 untagged artifact
curl -u admin:Harbor12345 \
  "https://harbor.example.com/api/v2.0/projects?page_size=100" | \
  jq -r '.[].name' | while read project; do
    curl -u admin:Harbor12345 \
      "https://harbor.example.com/api/v2.0/projects/${project}/repositories?page_size=100" | \
      jq -r '.[].name' | while read repo; do
        repo_encoded=$(python3 -c "import urllib.parse; print(urllib.parse.quote('${repo}', safe=''))")
        # 删除无 tag 的 artifact
        curl -u admin:Harbor12345 \
          "https://harbor.example.com/api/v2.0/projects/${project}/repositories/${repo_encoded}/artifacts?with_tag=false&page_size=100" | \
          jq -r '.[].digest' | while read digest; do
            curl -u admin:Harbor12345 -X DELETE \
              "https://harbor.example.com/api/v2.0/projects/${project}/repositories/${repo_encoded}/artifacts/${digest}"
          done
      done
  done

# 清理完成后立即执行 GC
curl -u admin:Harbor12345 \
  -X POST \
  -H "Content-Type: application/json" \
  "https://harbor.example.com/api/v2.0/system/gc/schedule" \
  -d '{"schedule": {"type": "Manual"}}'
```

## Prometheus 监控

### harbor_exporter 指标配置

Harbor 2.x 内置了 Prometheus metrics 端点，无需额外 exporter：

```bash
# 确认 metrics 端点
curl -u admin:Harbor12345 \
  https://harbor.example.com/api/v2.0/metrics

# harbor.yml 开启 metrics
metric:
  enabled: true
  port: 9090
  path: /metrics
```

Prometheus scrape 配置：

```yaml
# prometheus.yml
scrape_configs:
  - job_name: 'harbor'
    scheme: https
    tls_config:
      insecure_skip_verify: false
      ca_file: /etc/prometheus/certs/ca.crt
    basic_auth:
      username: admin
      password: Harbor12345
    static_configs:
      - targets: ['harbor.example.com:443']
    metrics_path: /api/v2.0/metrics
    scrape_interval: 30s
```

### 关键监控指标

```yaml
# 关键指标说明
harbor_project_total              # 项目总数
harbor_project_repo_total         # 仓库总数（按项目）
harbor_project_artifact_total     # Artifact 总数（按项目）
harbor_project_quota_usage_byte   # 存储配额使用量（字节）
harbor_project_quota_byte         # 存储配额上限
harbor_registry_request_total     # Registry 请求总数（按操作类型）
harbor_registry_request_duration_seconds  # 请求延迟（histogram）
harbor_task_queue_size            # 异步任务队列深度
harbor_task_queue_latency_seconds # 任务队列等待时间
harbor_job_service_task_total     # JobService 任务执行总数（按状态）
```

### 告警规则

```yaml
# harbor-alerts.yaml
groups:
  - name: harbor.alerts
    rules:
      # 存储配额超过 80%
      - alert: HarborProjectQuotaHigh
        expr: |
          harbor_project_quota_usage_byte / harbor_project_quota_byte > 0.8
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "Harbor 项目 {{ $labels.project }} 存储配额超过 80%"
          description: "当前使用率: {{ $value | humanizePercentage }}"

      # Registry 请求错误率过高
      - alert: HarborRegistryErrorRateHigh
        expr: |
          rate(harbor_registry_request_total{status=~"5.."}[5m]) /
          rate(harbor_registry_request_total[5m]) > 0.05
        for: 2m
        labels:
          severity: critical
        annotations:
          summary: "Harbor Registry 5xx 错误率超过 5%"

      # 任务队列积压
      - alert: HarborJobQueueBacklog
        expr: harbor_task_queue_size > 100
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "Harbor 任务队列积压: {{ $value }} 个待处理任务"

      # 扫描任务失败率
      - alert: HarborScanTaskFailureHigh
        expr: |
          rate(harbor_job_service_task_total{status="Error",job_type="scan"}[1h]) > 0.1
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "Harbor 镜像扫描失败率过高"
```

### Grafana Dashboard 关键面板

推荐导入官方 Dashboard ID `14075`（Harbor 2.x），重点关注以下面板：

1. **请求吞吐量**：`rate(harbor_registry_request_total[5m])` 按操作类型分类
2. **P99 延迟**：`histogram_quantile(0.99, rate(harbor_registry_request_duration_seconds_bucket[5m]))`
3. **存储使用趋势**：`harbor_project_quota_usage_byte` 折线图
4. **任务执行成功率**：Success vs Error 对比
5. **活跃用户数**：通过 PostgreSQL 查询辅助

## 运维最佳实践总结

**日常检查清单（每周执行）**：
- 检查各项目存储配额使用率（>70% 需关注）
- 检查 GC 最近执行结果
- 检查复制任务执行状态，确认跨区同步正常
- 检查 Robot Account 证书有效期，提前 30 天轮换
- 检查漏洞库更新时间，超过 7 天需手动更新

**版本升级建议**：
- 先在测试环境验证，Harbor 升级通常需要数据库 schema 迁移
- 升级前备份 PostgreSQL 数据库
- 查阅 Release Notes 中的 Breaking Changes
- 升级过程中 Registry 会短暂不可用，选择业务低峰期执行

**安全加固要点**：
- 修改默认管理员密码（`Harbor12345` 是众所周知的默认密码）
- 启用 HTTPS，禁止 HTTP 访问
- 为每个 CI/CD 流水线创建独立 Robot Account，定期轮换
- 开启审计日志，记录镜像推拉操作
- 配置 IP 白名单（在 Nginx 层面限制管理 API 访问来源）
