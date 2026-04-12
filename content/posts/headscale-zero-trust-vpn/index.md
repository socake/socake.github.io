---
title: "Headscale 自建零信任 VPN：跨云多机房内网打通"
date: 2026-04-12T14:00:00+08:00
draft: false
tags: ["Headscale", "WireGuard", "零信任", "VPN", "网络安全", "跨云"]
categories: ["网络与安全"]
description: "用 Headscale 自建 Tailscale 控制面，基于 WireGuard 打通多云多机房内网，替代传统堡垒机，实现精细 ACL 访问控制和 K8s 服务集成。"
summary: "从 WireGuard 协议原理到 Headscale 完整部署，包括 DERP 自建、Subnet Router 配置、K8s 集成和 ACL 策略设计，用 Mesh VPN 替代传统堡垒机的完整实操指南。"
toc: true
math: false
diagram: false
keywords: ["Headscale", "WireGuard", "Tailscale 自建", "零信任网络", "跨云内网", "Subnet Router"]
params:
  reading_time: true
---

## 为什么需要重新思考内网访问

传统内网访问模型是"进了城墙就安全"：VPN 进去之后，对内网几乎无限制访问。这个模型的问题在 2024 年已经很清晰了——跨云多机房、远程办公、第三方承包商接入，"城墙"越来越难画。

我们之前的架构：

- 堡垒机（Jumpserver）做跳板，研发访问 AWS/阿里云的服务器
- OpenVPN 给合作商开通访问权限
- 数据库只能在内网访问，研发本地调试必须先 SSH 隧道

痛点非常明显：

1. **堡垒机是单点**：挂了所有人断线，高可用方案复杂
2. **OpenVPN 接进来就是全内网**：细粒度控制靠 iptables 手写，维护噩梦
3. **跨云访问靠 VPN 隧道**：AWS 和阿里云之间配 IPSec，延迟高，故障排查困难
4. **审计不完整**：知道谁连进来了，但不知道他访问了什么

Headscale + WireGuard 解决了这些问题，迁移完之后我们关掉了堡垒机。

---

## WireGuard vs 传统 VPN

WireGuard 是 Linux 内核级别的 VPN 协议（5.6 版本合并进主线），相比 OpenVPN 和 IPSec 的核心差异：

**代码量**：WireGuard 约 4000 行代码，OpenVPN 超过 100000 行。代码少意味着攻击面小，审计容易。

**性能**：WireGuard 使用 ChaCha20-Poly1305 和 Curve25519，在现代 CPU 上比 AES-GCM（IPSec 常用）快，延迟通常低 50% 以上。

**握手机制**：WireGuard 没有"连接状态"，只有密钥对。一端发包，另一端用预配置的公钥验证，没有复杂的握手协商过程。这让它对网络切换（WiFi 换 4G）天然友好——不需要重连。

**穿透 NAT**：通过 keep-alive 数据包维持 NAT 映射，大多数 NAT 场景下无需公网 IP。

Tailscale 在 WireGuard 基础上加了：
- 控制面（协调各节点的密钥分发和路由）
- DERP 中继（当 P2P 打洞失败时走中继）
- ACL 策略引擎
- 自动 DNS

Headscale 是 Tailscale 控制面的开源替代实现，你自己托管控制面，客户端还是用官方 Tailscale 客户端。

---

## Headscale 服务端部署

### 环境要求

- 一台公网服务器（作为控制面 + 可选 DERP 中继）
- 域名，用于 HTTPS 访问
- 端口：443（HTTPS）、3478（STUN/DERP UDP）

### Docker Compose 部署

```yaml
# docker-compose.yml
version: '3.8'

services:
  headscale:
    image: headscale/headscale:latest
    container_name: headscale
    restart: unless-stopped
    volumes:
      - ./config:/etc/headscale
      - ./data:/var/lib/headscale
    ports:
      - "8080:8080"   # Headscale API/gRPC
      - "9090:9090"   # Metrics
    command: serve
    networks:
      - headscale_net

  headscale-ui:
    image: ghcr.io/gurucomputing/headscale-ui:latest
    container_name: headscale-ui
    restart: unless-stopped
    ports:
      - "8888:80"
    networks:
      - headscale_net

networks:
  headscale_net:
    driver: bridge
```

### Headscale 核心配置

```yaml
# config/config.yaml
server_url: https://headscale.example.com

listen_addr: 0.0.0.0:8080
metrics_listen_addr: 0.0.0.0:9090

# 私有网络地址段（分配给各节点的 Tailscale IP）
ip_prefixes:
  - 100.64.0.0/10   # Tailscale 标准地址段

# 数据库（生产用 PostgreSQL，测试用 sqlite）
database:
  type: postgres
  postgres:
    host: 127.0.0.1
    port: 5432
    name: headscale
    user: headscale
    password: ${DB_PASSWORD}
    max_open_conns: 10
    max_idle_conns: 10

# DNS 配置
dns:
  override_local_dns: true
  nameservers:
    global:
      - 1.1.1.1
      - 8.8.8.8
  magic_dns: true              # 节点可以用 hostname.tailnet.ts.net 互访
  base_domain: ts.example.com  # 自定义域名

# DERP 配置（后面详细讲）
derp:
  server:
    enabled: true
    region_id: 999
    region_code: "custom"
    region_name: "Custom DERP"
    stun_listen_addr: "0.0.0.0:3478"
  urls:
    - https://controlplane.tailscale.com/derpmap/default  # 保留官方 DERP 作为备份
  auto_update_enabled: true
  update_frequency: 24h

# 节点过期时间（0 表示永不过期，生产建议设置）
ephemeral_node_inactivity_timeout: 30m

log:
  level: info
```

### Nginx 反向代理

```nginx
# /etc/nginx/sites-available/headscale
server {
    listen 443 ssl http2;
    server_name headscale.example.com;

    ssl_certificate /etc/letsencrypt/live/headscale.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/headscale.example.com/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;

    # Headscale 需要支持长连接和流式响应
    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # 长连接超时，Headscale 使用长轮询
        proxy_read_timeout 86400s;
        proxy_send_timeout 86400s;
    }
}
```

启动服务：

```bash
docker compose up -d

# 验证服务状态
docker exec headscale headscale version
docker exec headscale headscale nodes list
```

---

## 自建 DERP 中继服务器

DERP（Detoured Encrypted Routing Protocol）是 WireGuard P2P 打洞失败时的备用中继路径。Tailscale 官方提供全球 DERP 节点，但自建 DERP 有两个好处：

1. **降低延迟**：中国大陆到 Tailscale 官方 DERP 延迟高，自建亚太节点可以从 200ms 降到 30ms
2. **隐私**：流量不经过第三方服务器

### 部署独立 DERP 服务器

```bash
# 安装 derper（Tailscale 官方工具）
go install tailscale.com/cmd/derper@latest

# 或者用 Docker
docker run -d \
  --name derper \
  --restart unless-stopped \
  -p 443:443 \
  -p 3478:3478/udp \
  -v /etc/letsencrypt:/certs:ro \
  fredliang/derper:latest \
  --hostname=derp.example.com \
  --certdir=/certs \
  --certmode=manual \
  --verify-clients=true   # 只允许注册到你的 Headscale 的客户端使用
```

`--verify-clients=true` 非常重要，否则你的 DERP 服务器会成为任何 Tailscale 用户的免费中继。

### 在 Headscale 配置自建 DERP

```yaml
# 方式一：直接在 config.yaml 里配置（重启生效）
derp:
  paths:
    - /etc/headscale/derp.yaml

# derp.yaml
regions:
  900:
    regionid: 900
    regioncode: cn-hangzhou
    regionname: CN Hangzhou
    nodes:
      - name: 900a
        regionid: 900
        hostname: derp.example.com
        stunport: 3478
        derpport: 443
```

### 测试 DERP 延迟

```bash
# 在客户端查看当前使用的中继和延迟
tailscale netcheck

# 输出示例
Report:
  * UDP: true
  * IPv4: yes, 1.2.3.4:xxxxx
  * IPv6: no
  * MappingVariesByDestIP: false
  * CaptivePortal: false
  * Nearest DERP: CN Hangzhou
  * DERP latency:
    - cn-hangzhou: 28ms  (选用了自建节点)
    - tok: 85ms
    - sfo: 180ms
```

---

## 客户端注册

### 创建 User（原来叫 Namespace）

```bash
# 创建用户/团队
docker exec headscale headscale users create engineering
docker exec headscale headscale users create ops
docker exec headscale headscale users create contractors
```

### Linux 客户端

```bash
# 安装 Tailscale 客户端
curl -fsSL https://tailscale.com/install.sh | sh

# 连接到自建 Headscale（而非 Tailscale 官方控制面）
tailscale up \
  --login-server=https://headscale.example.com \
  --accept-routes=true \
  --accept-dns=true

# 命令会输出一个注册 URL，在服务端用 headscale 命令批准
# 服务端执行：
docker exec headscale headscale nodes register \
  --user engineering \
  --key <上面输出的 nodekey>
```

### 生成预授权密钥（用于无人值守注册）

```bash
# 生成一次性密钥（用于自动化脚本、CI/CD 节点注册）
docker exec headscale headscale preauthkeys create \
  --user engineering \
  --reusable \         # 可复用
  --expiration 24h \   # 24 小时有效
  --tags tag:k8s-node  # 打标签，用于 ACL

# 客户端用预授权密钥注册（不需要手动批准）
tailscale up \
  --login-server=https://headscale.example.com \
  --authkey=<preauthkey> \
  --accept-routes=true
```

### macOS / Windows

安装 Tailscale 客户端，然后在菜单栏或系统托盘里找到 "Use custom coordination server"，填入 `https://headscale.example.com`，其余步骤相同。

---

## Subnet Router：整个 VPC 接入 Tailnet

Subnet Router 是 FinOps 价值最高的功能之一：**只需要在 VPC 里的一台机器上装 Tailscale，就能让整个 VPC 的 IP 段对 Tailnet 可见**，不需要在每台服务器上安装客户端。

### 场景

- RDS 数据库（不能装软件）需要从办公室直接访问
- 整个 K8s Node 网段需要对 Ops 团队可见
- 阿里云 VPC 和 AWS VPC 打通，不需要 VPN 隧道

### 配置 Subnet Router

```bash
# 在 VPC 内的一台 Linux 机器上（建议用专用的小实例）
# 1. 开启 IP 转发
echo 'net.ipv4.ip_forward = 1' | sudo tee -a /etc/sysctl.conf
echo 'net.ipv6.conf.all.forwarding = 1' | sudo tee -a /etc/sysctl.conf
sudo sysctl -p

# 2. 启动 Tailscale 并声明需要路由的子网
tailscale up \
  --login-server=https://headscale.example.com \
  --authkey=<preauthkey> \
  --advertise-routes=172.16.0.0/16,10.0.0.0/8 \  # 你的 VPC CIDR
  --accept-routes=true \
  --snat-subnet-routes=false   # 保留原始源 IP，方便日志审计

# 3. 在 Headscale 服务端批准这个路由声明
docker exec headscale headscale routes list
docker exec headscale headscale routes enable --route <route-id>
```

### 高可用 Subnet Router

生产环境建议部署两台 Subnet Router（不同 AZ），Tailscale 客户端会自动选择延迟低的那台：

```bash
# 两台机器都配置相同的 advertise-routes
# Headscale 会将两条路由都启用
# 客户端自动感知，其中一台挂了会切换到另一台
docker exec headscale headscale routes list
# ID  Machine           Prefix          Advertised  Enabled  Primary
# 1   subnet-router-1a  172.16.0.0/16   true        true     true
# 2   subnet-router-1b  172.16.0.0/16   true        true     false  (备用)
```

---

## ACL 访问控制策略

Headscale 的 ACL 使用 HuJSON 格式（JSON 的超集，支持注释），定义谁能访问哪些节点的哪些端口。

```jsonc
// /etc/headscale/acls.hujson
{
  // 定义分组
  "groups": {
    "group:engineering": ["user:alice@", "user:bob@"],
    "group:ops":         ["user:charlie@", "user:david@"],
    "group:contractors": ["user:vendor1@"]
  },

  // 定义标签（用于机器，而不是用户）
  "tagOwners": {
    "tag:prod-server":    ["group:ops"],
    "tag:staging-server": ["group:engineering", "group:ops"],
    "tag:k8s-node":       ["group:ops"],
    "tag:db-proxy":       ["group:ops"]
  },

  // 主机别名（方便引用）
  "hosts": {
    "prod-rds":      "172.16.10.5/32",
    "staging-rds":   "172.16.20.5/32",
    "aws-vpc":       "10.0.0.0/8",
    "aliyun-vpc":    "172.16.0.0/16"
  },

  // ACL 规则（默认拒绝所有，仅允许明确声明的）
  "acls": [
    // Ops 团队可以 SSH 到所有服务器
    {
      "action": "accept",
      "src": ["group:ops"],
      "dst": ["tag:prod-server:22", "tag:staging-server:22"]
    },

    // 工程师可以访问 staging 数据库（仅 MySQL 端口）
    {
      "action": "accept",
      "src": ["group:engineering"],
      "dst": ["staging-rds:3306"]
    },

    // Ops 可以访问 prod 数据库
    {
      "action": "accept",
      "src": ["group:ops"],
      "dst": ["prod-rds:3306", "prod-rds:5432"]
    },

    // 承包商只能访问特定的 staging 服务
    {
      "action": "accept",
      "src": ["group:contractors"],
      "dst": ["tag:staging-server:8080", "tag:staging-server:443"]
    },

    // K8s 节点之间互通（Pod 网络需要）
    {
      "action": "accept",
      "src": ["tag:k8s-node"],
      "dst": ["tag:k8s-node:*"]
    },

    // 所有人可以 ping（用于调试连通性）
    {
      "action": "accept",
      "src": ["*"],
      "dst": ["*:icmp"]
    }
  ],

  // SSH 规则（Tailscale SSH，不同于普通 ACL）
  "ssh": [
    {
      "action": "accept",
      "src": ["group:ops"],
      "dst": ["tag:prod-server"],
      "users": ["root", "ubuntu"]
    }
  ]
}
```

应用 ACL：

```bash
docker exec headscale headscale policy set --file /etc/headscale/acls.hujson

# 验证某个节点的连通性
docker exec headscale headscale debug acl check \
  --src-node engineering-laptop \
  --dst-node prod-rds \
  --dst-port 3306
```

---

## Exit Node：全局流量代理

Exit Node 让所有节点的出站流量都经过指定节点，相当于全局代理。使用场景：

- 开发环境访问只允许特定 IP 的生产资源
- 合规要求所有流量走固定出口 IP

```bash
# 把某台机器设置为 Exit Node
tailscale up \
  --login-server=https://headscale.example.com \
  --advertise-exit-node

# 服务端批准
docker exec headscale headscale routes enable --route <exit-node-route-id>

# 客户端使用 Exit Node
tailscale up --exit-node=<exit-node-ip>

# 或者只让某些流量走 Exit Node（排除局域网）
tailscale up \
  --exit-node=<exit-node-ip> \
  --exit-node-allow-lan-access=true
```

---

## 与 Kubernetes 集成

### 方案一：Subnet Router 暴露 K8s Service CIDR

最简单的方案，在每个 K8s 集群里部署一个 Subnet Router Pod，把 Pod 网段和 Service 网段暴露到 Tailnet：

```yaml
# headscale-subnet-router.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: headscale-subnet-router
  namespace: kube-system
spec:
  replicas: 2
  selector:
    matchLabels:
      app: headscale-subnet-router
  template:
    metadata:
      labels:
        app: headscale-subnet-router
    spec:
      # 需要 hostNetwork 来做路由
      hostNetwork: false
      containers:
        - name: tailscale
          image: ghcr.io/tailscale/tailscale:latest
          env:
            - name: TS_AUTHKEY
              valueFrom:
                secretKeyRef:
                  name: tailscale-auth
                  key: TS_AUTHKEY
            - name: TS_USERSPACE
              value: "true"
            - name: TS_ROUTES
              value: "10.96.0.0/12,10.244.0.0/16"  # Service CIDR + Pod CIDR
            - name: TS_EXTRA_ARGS
              value: "--login-server=https://headscale.example.com"
          securityContext:
            capabilities:
              add:
                - NET_ADMIN
          volumeMounts:
            - name: tailscale-state
              mountPath: /var/lib/tailscale
      volumes:
        - name: tailscale-state
          emptyDir: {}
---
apiVersion: v1
kind: Secret
metadata:
  name: tailscale-auth
  namespace: kube-system
stringData:
  TS_AUTHKEY: "<preauthkey>"
```

部署后，任何连接 Tailnet 的机器都能直接访问 K8s 的 ClusterIP Service，不需要 `kubectl port-forward`。

### 方案二：Tailscale Operator（更完整的集成）

Tailscale 官方提供了 K8s Operator，能把 K8s Service 和 Ingress 直接暴露到 Tailnet：

```bash
# 安装 Tailscale Operator（支持 Headscale）
helm install tailscale-operator tailscale/tailscale-operator \
  --namespace tailscale \
  --create-namespace \
  --set oauth.clientId=<client-id> \
  --set oauth.clientSecret=<client-secret> \
  --set apiServerProxyConfig.mode=off
```

给 Service 加注解，自动在 Tailnet 里创建可访问的端点：

```yaml
apiVersion: v1
kind: Service
metadata:
  name: internal-api
  annotations:
    tailscale.com/expose: "true"
    tailscale.com/hostname: "internal-api-prod"
spec:
  selector:
    app: internal-api
  ports:
    - port: 8080
```

加了注解之后，Tailnet 里的机器可以直接用 `http://internal-api-prod:8080` 访问这个 Service，完全不经过 Ingress 和公网。

---

## 运维场景实战

### 场景一：替代堡垒机

传统堡垒机方案：研发登录堡垒机 → 堡垒机 SSH 到目标服务器。

Headscale 方案：研发机器加入 Tailnet，直接 SSH 到目标服务器（走 WireGuard 加密隧道），ACL 控制权限。

```bash
# 研发机器（加入 Tailnet 后）直接 SSH
ssh ubuntu@100.64.0.15   # Tailscale IP，等价于走堡垒机

# 或者配置 ~/.ssh/config 用主机名
Host prod-web-01
    HostName prod-web-01.ts.example.com
    User ubuntu
    IdentityFile ~/.ssh/id_ed25519
```

审计：Headscale 记录所有节点连接日志，Tailscale SSH 模式还能记录 session 内容。

### 场景二：跨云数据库访问

AWS RDS 在 AWS VPC，阿里云 RDS 在阿里云 VPC。以前需要打两个 IPSec 隧道，现在：

1. AWS VPC 部署 Subnet Router，声明 `10.0.0.0/8`
2. 阿里云 VPC 部署 Subnet Router，声明 `172.16.0.0/16`
3. 两个 Subnet Router 都加入同一个 Tailnet
4. DBA 机器加入 Tailnet，可以直接连接两个 VPC 的 RDS

连接路径：DBA 机器 → WireGuard 隧道 → Subnet Router → RDS，延迟比 IPSec 低，配置比 VPN 隧道简单。

### 场景三：开发环境访问生产配置

只读权限，不需要完整的生产网络访问：

```jsonc
// ACL：允许 engineering 组只读访问 Nacos 配置中心
{
  "action": "accept",
  "src": ["group:engineering"],
  "dst": ["nacos-prod:8848"]
}
```

### 场景四：CI/CD 访问私有资源

GitLab Runner 或 GitHub Actions Self-hosted Runner 注册到 Tailnet，就能在流水线里直接访问私有 Registry、私有 Maven/PyPI 仓库：

```yaml
# .gitlab-ci.yml 中使用 Tailscale IP 访问私有服务
build:
  script:
    - docker login registry.internal:5000  # Tailnet 内的私有 Registry
    - mvn deploy -s settings.xml           # settings.xml 里配置 Tailnet 内的 Nexus 地址
```

---

## 运维注意事项

### 密钥轮换

预授权密钥有过期时间，需要自动化轮换：

```bash
#!/bin/bash
# rotate-preauthkeys.sh
# 生成新密钥，更新 K8s Secret，重启 Subnet Router

NEW_KEY=$(docker exec headscale headscale preauthkeys create \
  --user ops \
  --reusable \
  --expiration 720h \
  --tags tag:k8s-node \
  --output json | jq -r '.key')

kubectl create secret generic tailscale-auth \
  --namespace kube-system \
  --from-literal=TS_AUTHKEY="$NEW_KEY" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl rollout restart deployment/headscale-subnet-router -n kube-system
```

### 监控 Tailnet 健康状态

```bash
# 检查所有节点的最后在线时间
docker exec headscale headscale nodes list --output json | \
  jq -r '.[] | [.name, .last_seen, .online] | @tsv' | \
  column -t

# Prometheus 指标（Headscale 暴露在 9090 端口）
# headscale_nodes_total - 总节点数
# headscale_auth_keys_total - 预授权密钥数量
```

### 故障排查

```bash
# 节点 P2P 打洞失败，流量走 DERP
tailscale status  # 查看每个节点的连接方式（direct/relay）

# 如果显示 relay，尝试强制重新打洞
tailscale ping <目标节点>  # 多 ping 几次，有时候可以触发打洞

# 查看详细路径信息
tailscale debug peer-status <目标节点IP>

# Headscale 服务端日志
docker logs headscale --tail 100 --follow
```

---

## 从堡垒机迁移的平滑路径

不要一刀切，分阶段迁移：

**第一阶段（2 周）**：Headscale 和堡垒机并行运行。内部用户注册到 Tailnet，测试连通性和 ACL。

**第二阶段（1 个月）**：所有新接入需求走 Tailnet，不再给堡垒机开新账号。监控两套系统的使用情况。

**第三阶段**：确认 Tailnet 稳定后，通知剩余堡垒机用户迁移，设定下线日期，关闭堡垒机。

整个迁移过程中，Tailnet 作为"更方便的选项"自然会吸引用户，不需要强制。当堡垒机用户发现直接 SSH 比跳板机快、不需要二次认证之后，自然会主动迁移。

---

Headscale 配合 WireGuard 的零信任模型解决了传统 VPN 的根本问题：从"进了城墙就安全"变成"每次连接都验证身份和权限"。更重要的是，它的运维复杂度比 IPSec VPN 低一个数量级，任何一个熟悉 Linux 的运维都能在一天内搭起来。
