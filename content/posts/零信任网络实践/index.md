---
title: "零信任网络改造：从公网暴露到 Headscale VPN"
date: 2025-11-22T13:37:00+08:00
draft: false
tags: ["安全", "网络", "Headscale", "零信任", "运维"]
categories: ["Kubernetes"]
description: "记录将运维系统从公网暴露迁移到 Headscale 零信任网络的完整过程，包括资产梳理、方案对比和落地挑战"
summary: "从发现公网暴露的安全隐患开始，到用 Headscale 自建零信任网络，替代跳板机体系，实现 kubectl 和运维系统的 VPN 接入。"
toc: true
math: false
diagram: false
series: ["SRE 实战手册"]
keywords: ["零信任", "headscale", "tailscale", "wireguard", "VPN", "网络安全", "kubectl"]
params:
  reading_time: true
---

## 为什么要做这件事

某天做常规安全审查，用 shodan 和 nmap 扫了一遍我们的公网暴露资产，结果让我有点坐不住：

- ArgoCD UI 暴露在公网（用 NodePort，临时的，结果忘了）
- Grafana 有公网入口，只有弱口令保护
- 几个服务的 metrics 端口（9090）直接对公网
- 一台用于应急的跳板机 SSH 开放在公网，端口 22

当时的安全策略是"加 IP 白名单"，但白名单维护越来越混乱，有些条目的来源已经无从追溯。

更大的问题是：这套系统对"内部"和"外部"的边界判断基于 IP 地址，而 IP 地址在云环境下很难成为可靠的信任依据——研发在家办公怎么办？出差的工程师怎么办？开发机被入侵的风险呢？

零信任的核心思路是：**不信任任何网络位置，每个连接都要验证身份**。这次改造的目标就是把所有运维系统从公网撤回来，统一走 VPN，以身份而非 IP 地址作为信任依据。

---

## 现状梳理：公网暴露资产扫描

改造之前，先摸清楚有哪些东西暴露在外面。

```bash
# 用 nmap 扫自己的公网 IP 段
nmap -sV -p 22,80,443,2376,2379,6443,8080,8443,9090,9093 \
  --open \
  x.x.x.0/24

# 检查 AWS 安全组，找出 0.0.0.0/0 的入站规则
aws ec2 describe-security-groups \
  --filters Name=ip-permission.cidr,Values=0.0.0.0/0 \
  --query 'SecurityGroups[*].{ID:GroupId,Name:GroupName,Rules:IpPermissions}' \
  --output table

# 检查 K8s 中 type=LoadBalancer 或 NodePort 的 Service
kubectl get svc --all-namespaces | grep -E 'LoadBalancer|NodePort'
```

扫描后整理成清单：

| 服务 | 暴露方式 | 端口 | 风险级别 | 处理方案 |
|------|---------|------|---------|---------|
| ArgoCD | NodePort | 30080 | 高 | 撤回内网，走 VPN |
| Grafana | LoadBalancer | 443 | 中 | 撤回内网，走 VPN |
| Metrics 端口 | 安全组 0.0.0.0/0 | 9090 | 中 | 收紧安全组 |
| 跳板机 SSH | 安全组 0.0.0.0/0 | 22 | 高 | 改为 VPN 接入，关闭公网 |

---

## 方案选型：Headscale vs Tailscale vs WireGuard

市面上有几种方案可选：

### 纯 WireGuard

WireGuard 是底层 VPN 协议，性能极好，配置相对简单，但：
- 没有 peer 自动发现，每台机器都要手动配置对端 public key 和 endpoint
- 没有 NAT 穿透支持，家庭网络（CG-NAT）下不稳定
- 没有用户管理界面，设备多了维护成本高

适合：节点数量少（< 10 台），不需要频繁动态加入新设备。

### Tailscale（SaaS）

Tailscale 在 WireGuard 基础上构建了完整的 mesh VPN：
- 自动 NAT 穿透（基于 DERP relay）
- 用户/设备管理
- ACL 访问控制
- 免费版支持 3 个用户

问题是：控制面在 Tailscale 的服务器上，企业数据流量的 key 管理对第三方有依赖。对安全要求高或者有数据合规要求的团队，这一点是接受不了的。

### Headscale（自托管 Tailscale 控制面）

Headscale 是 Tailscale 控制面的开源实现，数据面仍然走 WireGuard，但控制面完全自托管：
- 自己掌握所有节点 key
- 兼容 Tailscale 客户端（不需要额外客户端）
- 支持 MagicDNS（节点之间用主机名互访）
- 开源，社区活跃

缺点：需要自己维护服务，功能比 SaaS Tailscale 少（如无 SSO 集成，需要额外配置）。

**我们的选择：Headscale**，控制面自托管，符合数据安全要求，客户端兼容性好。

---

## Headscale 部署

### 服务端部署

选一台有公网 IP 的小机器（跳板机或专用 VPN 节点）部署 Headscale：

```bash
# 下载最新版本（以 0.23.0 为例）
wget https://github.com/juanfont/headscale/releases/download/v0.23.0/headscale_0.23.0_linux_amd64
chmod +x headscale_0.23.0_linux_amd64
mv headscale_0.23.0_linux_amd64 /usr/local/bin/headscale
```

配置文件 `/etc/headscale/config.yaml`：

```yaml
server_url: https://headscale.example.com
listen_addr: 0.0.0.0:8080
metrics_listen_addr: 0.0.0.0:9090

# IP 地址段分配给 VPN 内部设备
ip_prefixes:
  - fd7a:115c:a1e0::/48
  - 100.64.0.0/10

# DNS 配置（MagicDNS）
dns_config:
  nameservers:
    - 1.1.1.1
  domains: []
  magic_dns: true
  base_domain: vpn.internal

# DERP（relay 服务器，用于 NAT 穿透）
derp:
  server:
    enabled: false   # 自己的 DERP 服务可以后续开启
  urls:
    - https://controlplane.tailscale.com/derpmap/default   # 先用 Tailscale 的公共 DERP

# 数据库（使用 SQLite，节点少时足够）
database:
  type: sqlite
  sqlite:
    path: /var/lib/headscale/db.sqlite

# TLS 配置（使用 nginx 反代 + Let's Encrypt）
tls_cert_path: ""
tls_key_path: ""
```

systemd 服务：

```ini
[Unit]
Description=Headscale VPN Control Server
After=network.target

[Service]
User=headscale
Group=headscale
ExecStart=/usr/local/bin/headscale serve
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
systemctl enable headscale
systemctl start headscale
```

Nginx 反代（443 → Headscale 8080）：

```nginx
server {
    listen 443 ssl http2;
    server_name headscale.example.com;

    ssl_certificate /etc/letsencrypt/live/headscale.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/headscale.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

### 用户和设备管理

```bash
# 创建用户（相当于 Tailscale 的 namespace）
headscale users create devteam
headscale users create opsengineers

# 生成设备注册 key（工程师用来接入 VPN）
headscale preauthkeys create --user devteam --expiration 24h --reusable

# 查看已接入设备
headscale nodes list

# 输出示例
ID  Hostname        User        IP Addresses                 Last Seen
1   dev-mbp         devteam     100.64.0.1, fd7a:...         2025-12-09 14:30
2   ops-server-1    opsengineers 100.64.0.2, fd7a:...        2025-12-09 14:28
```

### 客户端接入（工程师侧）

```bash
# macOS / Linux 安装 Tailscale 客户端
brew install tailscale   # macOS

# 连接到自托管的 Headscale
tailscale up --login-server https://headscale.example.com

# 查看连接状态
tailscale status
```

---

## 和 Kubernetes 集成

### kubectl over VPN

将 API Server 的公网入口收回，只保留 VPN 内网访问：

```bash
# 修改 EKS 集群安全组：只允许 VPN 内网段访问 6443
aws ec2 authorize-security-group-ingress \
  --group-id sg-xxxxxxxxxx \
  --protocol tcp \
  --port 6443 \
  --cidr 100.64.0.0/10   # Headscale 分配的 VPN IP 段

# 撤销公网访问
aws ec2 revoke-security-group-ingress \
  --group-id sg-xxxxxxxxxx \
  --protocol tcp \
  --port 6443 \
  --cidr 0.0.0.0/0
```

更新 kubeconfig 使用内网地址：

```yaml
# ~/.kube/config
clusters:
- cluster:
    server: https://api.prod-cluster.vpn.internal:6443   # VPN 内网域名
  name: prod-cluster
```

### ArgoCD 撤回内网

把 ArgoCD 的 Service 类型从 LoadBalancer 改为 ClusterIP，用 VPN 内网 + kubectl port-forward 或内网 Ingress 访问：

```yaml
# argocd-server service
apiVersion: v1
kind: Service
metadata:
  name: argocd-server
  namespace: argocd
spec:
  type: ClusterIP   # 从 LoadBalancer 改为 ClusterIP
  ports:
  - port: 443
    targetPort: 8080
```

```bash
# 工程师在 VPN 内通过 port-forward 访问
kubectl port-forward svc/argocd-server -n argocd 8080:443
# 然后访问 https://localhost:8080
```

---

## 接入流程设计

### 开发工程师接入流程

1. 运维创建预授权 key（设置 24h 有效期）
2. 工程师安装 Tailscale 客户端，使用 key 加入 VPN
3. 运维在 Headscale 确认设备注册，分配到对应 user group
4. 工程师可以访问 QA/PRE 环境，PROD 需要额外申请

### ACL 访问控制

Headscale 支持 Tailscale 的 ACL 格式，按 user group 控制访问权限：

```json
{
  "groups": {
    "group:devs": ["devteam"],
    "group:ops": ["opsengineers"]
  },
  "acls": [
    // 开发组：只能访问 QA 和 PRE 的 K8s API
    {
      "action": "accept",
      "src": ["group:devs"],
      "dst": ["100.64.0.10:6443", "100.64.0.11:6443"]
    },
    // 运维组：全部访问权限
    {
      "action": "accept",
      "src": ["group:ops"],
      "dst": ["*:*"]
    }
  ]
}
```

---

## 收敛过程中的挑战

**挑战 1：老系统的硬编码公网地址**

有些监控 agent 和日志收集器硬编码了公网 IP。迁移时需要逐一修改配置，比预想的工作量大。

**解决**：建一个映射表，把公网地址和 VPN 内网地址对应起来，用 DNS CNAME 过渡，给老系统一个缓冲期。

**挑战 2：CI/CD 系统的访问权限**

GitHub Actions runner 在公网，撤销 API Server 公网入口后，CI 流水线无法部署到 K8s。

**解决方案 1**：在 K8s 集群内部署 self-hosted runner，从集群内部访问 API Server。

**解决方案 2**：让 runner 通过 Headscale API 动态注册为节点，完成部署后注销。

我们选了方案 1，self-hosted runner 顺便解决了 CI 机器规格不够的问题。

**挑战 3：DERP relay 稳定性**

早期用 Tailscale 的公共 DERP 服务器，国内访问延迟高。后来在阿里云部署了自己的 DERP 节点，延迟降到了 30ms 以内。

```yaml
# headscale config.yaml：配置自建 DERP
derp:
  paths:
    - /etc/headscale/derp.yaml

# /etc/headscale/derp.yaml
regions:
  900:
    regionid: 900
    regioncode: cn-hangzhou
    regionname: Aliyun Hangzhou
    nodes:
      - name: 900a
        regionid: 900
        hostname: derp-cn.example.com
        ipv4: x.x.x.x
        derpport: 443
        stunport: 3478
```

---

## 改造后的变化

改造完成两个月，几个明显的变化：

1. **安全告警减少了**：Cloudtrail 和安全组里来自陌生 IP 的扫描行为基本消失
2. **管理复杂度降低**：不再需要维护 IP 白名单，新同事接入只需要分发一个 pre-auth key
3. **跳板机退役**：那台专门用来跳板的 EC2 终于关掉了，每月省了一点机器费用
4. **审计更清晰**：Headscale 的日志记录了每个设备的连接记录，谁在什么时候访问了什么，有迹可查

零信任不是一次性改造，而是一个持续收紧的过程。后续还计划做设备合规检查（只有装了 EDR 的设备才能加入 VPN）和操作审计（所有 kubectl 操作记录到日志系统）。

---

回头看，最大的感受是：安全改造的时机永远是"现在"，等到出了事再做往往代价更大。这件事拖了半年才开始做，幸好没有在这半年里出什么问题。
