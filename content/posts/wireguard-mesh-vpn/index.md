---
title: "用 WireGuard 构建多云 mesh VPN：从点对点到全网互联"
date: 2025-11-07T10:00:00+08:00
draft: false
tags: ["WireGuard", "VPN", "多云", "网络", "零信任"]
categories: ["零信任"]
description: "一份从实战出发的 WireGuard mesh VPN 笔记：讲清楚为什么不用 IPSec/OpenVPN、手写配置 vs Netmaker vs Tailscale 的选型对比、AWS 与阿里云跨云 mesh 的真实部署方案、MTU 与 NAT 穿透的踩坑，以及自动化密钥分发与监控方案。"
summary: "一份从实战出发的 WireGuard mesh VPN 笔记：讲清楚为什么不用 IPSec/OpenVPN、手写配置 vs Netmaker vs Tailscale 的选型对比、AWS 与阿里云跨云 mesh 的真实部署方案、MTU 与 NAT 穿透的踩坑，以及自动化密钥分发与监控方案。"
toc: true
math: false
diagram: false
keywords: ["WireGuard", "mesh VPN", "多云互联", "Netmaker", "Tailscale"]
params:
  reading_time: true
---

## 为什么是 WireGuard

在 WireGuard 出现之前，我们这些运维的"VPN 选择困难症"是真实存在的：

- **IPSec**：企业级标准，配置地狱、debug 地狱、内核模块一大堆。两个厂家的 IPSec 实现能因为一个 phase2 的 DH group 协商不上就死活连不上。我见过花三天调通一个 IPSec 隧道的事。
- **OpenVPN**：好用，但性能拉胯，TLS over UDP 的开销大，单核瓶颈明显。大流量场景经常 CPU 被打爆。
- **SSH tunnel**：只适合临时用，无法做 mesh，无法做路由。

2018 年 WireGuard 主线合入内核，2020 年成为 Linux 5.6+ 默认，这一切就变了。WireGuard 的设计哲学非常"工程师"：

- **代码极简**：核心代码不到 4000 行（OpenVPN 是 10 万+）
- **加密套件固定**：ChaCha20 + Poly1305 + Curve25519，不支持协商，意味着没有"协议降级"攻击面
- **性能极高**：内核态实现，单核能跑满 10Gbps+
- **配置简洁**：一个 peer 就几行，没有任何 magic
- **天然支持 roaming**：client IP 变了直接继续用

这几年我在生产里用 WireGuard 替换了几乎所有旧 VPN，从"运维跳板"到"多云互联"到"k8s 集群边缘"。这篇文章讲三个实战场景：**多云 mesh VPN**、**办公网到数据中心**、**k8s 集群跨 region 互通**，基于 **WireGuard kernel module + wg-quick**，以及 **Netmaker 0.24+**。

## 一、WireGuard 基础快速过

这里只讲最少必要的概念，假设你完全没用过。

### 1.1 几个核心概念

- **Peer**：通信的对端。WireGuard 里没有 "client/server" 之分，所有节点都是平等的 peer。
- **Public/Private key**：每个 peer 有一对密钥。私钥本地保留，公钥发给对端。
- **AllowedIPs**：**最重要的概念**。它既是路由表，又是访问控制列表。对端发来的包，源 IP 必须在 AllowedIPs 列表里才被接受；本地发往某个 IP 的包，根据 AllowedIPs 决定走哪个 peer。
- **Endpoint**：对端的公网 IP:port。只有一端需要知道对端的 endpoint，另一端可以是 NAT 后自动发现（roaming）。
- **PersistentKeepalive**：NAT 后的 peer 需要定期发包保持 NAT 映射，一般 25 秒。

### 1.2 最小化配置

```ini
# /etc/wireguard/wg0.conf (Node A)
[Interface]
PrivateKey = <nodeA_private>
Address = 10.100.0.1/24
ListenPort = 51820

[Peer]
PublicKey = <nodeB_public>
AllowedIPs = 10.100.0.2/32
Endpoint = nodeB.example.com:51820
PersistentKeepalive = 25
```

```ini
# Node B
[Interface]
PrivateKey = <nodeB_private>
Address = 10.100.0.2/24
ListenPort = 51820

[Peer]
PublicKey = <nodeA_public>
AllowedIPs = 10.100.0.1/32
Endpoint = nodeA.example.com:51820
PersistentKeepalive = 25
```

启动：

```bash
wg-quick up wg0
systemctl enable --now wg-quick@wg0
```

就这么简单。`10.100.0.0/24` 是 overlay 网段，两台机器通过这个网段互通。

### 1.3 mesh vs hub-spoke

**Hub-spoke**：所有客户端连到一个中心节点，节点间通过中心转发。优点是配置简单（一个中心，N 个 spoke），缺点是中心故障全挂、流量翻倍、延迟高。

**Full mesh**：每两个节点直接有隧道。优点是任意两点最短路径、无单点、延迟低，缺点是配置复杂（N 个节点 = N² 条隧道）、密钥分发麻烦。

**Partial mesh**：关键节点 full mesh，边缘节点通过最近的关键节点访问其他。这是大多数生产环境的选择。

## 二、场景 1：多云 mesh VPN（AWS + 阿里云）

这是我花了最多时间优化的场景：把 AWS 的 us-west-2 和阿里云的 cn-hangzhou 两个地域打通，让两边的 EKS/ACK 集群能互相访问。

### 2.1 方案选型

首先**不要用云厂商的 VPN Gateway**。原因：
- AWS Site-to-Site VPN 贵（$0.05/小时/隧道 + 流量费）
- 阿里云 SSL VPN 体验不好
- 两家的 VPN Gateway 互相协商 IPSec 经常出兼容问题
- 无法自定义路由策略

我的方案是在两端各起 2~3 台 EC2/ECS 作为 WireGuard 边界节点，自己 mesh。

```
   AWS us-west-2                   Alibaba cn-hangzhou
   ┌─────────────────┐             ┌──────────────────┐
   │                 │             │                  │
   │  VPC 10.0.0/16  │             │ VPC 172.16.0/16  │
   │                 │             │                  │
   │  ┌───────────┐  │             │  ┌────────────┐  │
   │  │ wg-gw-a1  │◀─┼──wg tunnel──┼──│ wg-gw-b1   │  │
   │  │ wg-gw-a2  │◀─┼──wg tunnel──┼──│ wg-gw-b2   │  │
   │  └─────┬─────┘  │             │  └──────┬─────┘  │
   │        │        │             │         │        │
   │  ┌─────▼─────┐  │             │  ┌──────▼─────┐  │
   │  │  EKS Pods │  │             │  │  ACK Pods  │  │
   │  └───────────┘  │             │  └────────────┘  │
   └─────────────────┘             └──────────────────┘
```

### 2.2 节点部署

每个 region 起 2 个节点做 HA（不同 AZ）。EC2 规格 c6i.large 足够跑 1Gbps。

安装：

```bash
apt update && apt install -y wireguard wireguard-tools iptables resolvconf
sysctl -w net.ipv4.ip_forward=1
sysctl -w net.ipv4.conf.all.src_valid_mark=1
cat >> /etc/sysctl.d/99-wg.conf <<EOF
net.ipv4.ip_forward = 1
net.ipv4.conf.all.src_valid_mark = 1
net.core.rmem_max = 26214400
net.core.wmem_max = 26214400
EOF
sysctl -p /etc/sysctl.d/99-wg.conf
```

生成密钥：

```bash
wg genkey | tee privatekey | wg pubkey > publickey
```

AWS 侧节点 `wg-gw-a1` 的配置：

```ini
[Interface]
PrivateKey = <a1_priv>
Address = 10.200.0.1/24
ListenPort = 51820
MTU = 1420
# 让 overlay 流量能出 VPC
PostUp = iptables -A FORWARD -i %i -j ACCEPT; iptables -A FORWARD -o %i -j ACCEPT; iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE
PostDown = iptables -D FORWARD -i %i -j ACCEPT; iptables -D FORWARD -o %i -j ACCEPT; iptables -t nat -D POSTROUTING -o eth0 -j MASQUERADE

# 到阿里云 gw b1
[Peer]
PublicKey = <b1_pub>
AllowedIPs = 10.200.0.11/32, 172.16.0.0/16
Endpoint = 47.xx.xx.xx:51820
PersistentKeepalive = 25

# 到阿里云 gw b2
[Peer]
PublicKey = <b2_pub>
AllowedIPs = 10.200.0.12/32
Endpoint = 47.xx.xx.yy:51820
PersistentKeepalive = 25

# 同 region 另一台 gw a2
[Peer]
PublicKey = <a2_pub>
AllowedIPs = 10.200.0.2/32
Endpoint = 10.0.1.20:51820   # 内网 IP，同 VPC 走内网
```

**关键点**：

1. **MTU = 1420**：WireGuard 封装额外占用约 80 字节（UDP 头 + WG 头），overlay MTU 要比物理 MTU 小。1500 - 80 = 1420。如果不设 MTU，大包会被分片，性能大降。
2. **AllowedIPs 同时写对端 overlay IP 和对端 VPC 网段**：这样到 172.16.0.0/16 的流量会被路由到 b1，实现"经隧道访问对端 VPC"。
3. **只有到 AWS 远端 VPC 的路由放在第一个 peer**：因为 AllowedIPs 是"最精确匹配 + 第一匹配"，写在哪个 peer 上就走哪个 peer。
4. **同 region 用内网 IP**：VPC 内部 peer 的 Endpoint 用 internal IP，不要走公网，省钱且快。

### 2.3 路由配置

WireGuard 本身不处理"VPC 里的其他机器怎么把流量送到 gw"。你需要在 VPC 路由表里添加：

```
AWS VPC 路由表:
  Destination: 172.16.0.0/16
  Target: eni-xxxx (wg-gw-a1 的网卡)

阿里云 VPC 路由表:
  目标: 10.0.0.0/16
  下一跳类型: 弹性网卡
  下一跳: eni-yyyy (wg-gw-b1)
```

还要在 EC2/ECS 上**禁用 source/destination check**，否则云厂商会丢弃非本机 IP 的包：

```bash
# AWS
aws ec2 modify-instance-attribute --instance-id i-xxx \
    --no-source-dest-check
```

阿里云在 ECS 的安全组或网络接口里有类似选项。

### 2.4 HA 切换

两个 gw 怎么做 HA？我用的是 **keepalived + VIP**，把 VPC 内部的 next-hop IP 做成 VIP，主节点宕机时 VIP 漂到备节点，流量无感切换：

```
wg-gw-a1 (10.0.1.10) + wg-gw-a2 (10.0.1.11)
           共享 VIP: 10.0.1.100
VPC 路由表下一跳: 10.0.1.100 (通过 ENI 绑定)
```

但云环境的 VIP 漂移要通过 API 调用 re-associate ENI，不能只靠 ARP gratuitous。有个工具叫 `aws-ha-vip` 可以做这件事；阿里云类似的可以写个 shell 脚本调 `aliyun ecs ModifyNetworkInterfaceAttribute`。

更简单的方案：**ECMP**。Linux 路由表可以配等价多路径，VPC 路由表支持多个下一跳。两个 gw 都 active，流量按 hash 分发。但 VPC 路由的 ECMP 支持因云而异，不稳，我们最终用的是 keepalived+VIP 方案。

### 2.5 性能调优

默认配置跑 1Gbps 没问题，要跑 5Gbps+ 需要调优：

1. **开启多队列**：WireGuard 默认单队列，CPU 密集型情况下单核瓶颈。Linux 5.17+ 支持多队列，开启：
   ```bash
   ethtool -L wg0 combined 4
   ```
2. **CPU affinity**：把 WireGuard 的 softirq 绑定到 NUMA 节点近 NIC 的核上：
   ```bash
   # 看 NIC 在哪个 numa node
   cat /sys/class/net/eth0/device/numa_node
   ```
3. **关闭 offload 冲突**：某些 NIC 的 TSO/GRO 和 WireGuard 配合有问题：
   ```bash
   ethtool -K eth0 gro on tso on
   ethtool -K wg0 gro on
   ```
4. **ring buffer 调大**：
   ```bash
   ethtool -G eth0 rx 4096 tx 4096
   ```

我们生产节点（c6i.xlarge）实测跑 3.2Gbps 稳定，CPU 使用率约 60%。

## 三、场景 2：办公网到数据中心

这个场景比多云简单：N 个办公电脑 + 一个公司数据中心。

### 3.1 为什么不继续用 OpenVPN

我们最早用 OpenVPN，问题：
- TLS 握手慢，冷启动 3~5 秒
- 带宽只能跑到 50~80Mbps（协议开销）
- 断网恢复经常重连失败
- Windows 客户端 Tap 驱动偶发崩溃

切到 WireGuard 后：
- 连接 <1 秒
- 带宽能跑到 400Mbps+（千兆光纤上限）
- 网络切换（Wi-Fi ↔ 4G）无感
- 所有平台原生支持

### 3.2 选择 Tailscale 还是自建

这里我推荐 **Tailscale 或 Headscale**（开源自托管 Tailscale 控制面）。**除非你有非常特殊的合规需求**，否则不要自己写 wg-quick 管办公 VPN。原因：

1. 密钥管理：每个员工一个 peer，加入离职都要改配置，人肉难管
2. ACL：要限制某些员工只能访问某些服务器，原生 WireGuard 要写 iptables
3. 故障排查：用户说"连不上"，你怎么知道是他的问题还是你的问题
4. DNS 和魔法子域：Tailscale 的 MagicDNS 能让 `devbox-01` 直接 resolve 成 tailnet IP，非常丝滑

**Headscale** 是社区开源的 Tailscale 控制面实现，Go 写的，部署极简单。数据面依然是标准 WireGuard，但密钥、ACL、节点发现都交给 Headscale。我们办公网用 Headscale + Tailscale 客户端，已经跑了两年稳定。

Headscale 部署核心配置（`config.yaml`）：

```yaml
server_url: https://headscale.corp.example.com
listen_addr: 0.0.0.0:8080
metrics_listen_addr: 0.0.0.0:9090
grpc_listen_addr: 0.0.0.0:50443

private_key_path: /var/lib/headscale/private.key
noise:
  private_key_path: /var/lib/headscale/noise_private.key

ip_prefixes:
  - 100.64.0.0/10

derp:
  server:
    enabled: true
    region_id: 900
    region_code: "corp"
    stun_listen_addr: "0.0.0.0:3478"

database:
  type: postgres
  postgres:
    host: headscale-db.internal
    port: 5432
    name: headscale
    user: headscale
    password_file: /etc/headscale/db.password

acl_policy_path: /etc/headscale/acl.hujson

oidc:
  only_start_if_oidc_is_available: true
  issuer: https://keycloak.corp.example.com/realms/corp
  client_id: headscale
  client_secret_path: /etc/headscale/oidc.secret
  scope: ["openid", "profile", "email", "groups"]
  allowed_groups:
    - engineering
    - ops
```

**OIDC 集成**是关键。员工登录 Headscale 时走公司 SSO，离职时从 SSO 删除就自动失去接入权限。不要搞"发钥匙"那一套。

ACL 示例：

```hujson
{
  "groups": {
    "group:admins": ["alice@corp", "bob@corp"],
    "group:engineers": ["*@corp"],
  },
  "tagOwners": {
    "tag:server": ["group:admins"],
    "tag:devbox": ["group:engineers"],
  },
  "acls": [
    // admin 能访问所有
    { "action": "accept", "src": ["group:admins"], "dst": ["*:*"] },
    // engineer 只能访问 devbox
    { "action": "accept", "src": ["group:engineers"], "dst": ["tag:devbox:*"] },
    // 任何人能访问内部 Gitlab/Jira
    { "action": "accept", "src": ["group:engineers"],
      "dst": ["gitlab.corp.example.com:443", "jira.corp.example.com:443"] },
  ]
}
```

### 3.3 拆分隧道

全流量走 VPN 有两个大问题：1) VPN 出口流量费用高；2) 员工访问 YouTube 这类娱乐流量经过公司网络不合适。所以要**拆分隧道**（split tunnel）：只有访问公司内网才走 VPN，其他流量直接本地上网。

Tailscale/Headscale 通过 `--accept-routes=false` 实现客户端不接收路由广播，然后通过 subnet router 做精确路由：

```bash
# 在公司数据中心的 subnet router 上
sudo tailscale up --advertise-routes=10.0.0.0/8,172.16.0.0/12 --accept-dns=true
```

客户端：
```bash
tailscale up --accept-routes
```

这样客户端只有"目标在 10.0.0.0/8 或 172.16.0.0/12 的流量"走 VPN，其他不经过 VPN。

## 四、场景 3：K8s 集群 overlay 网络

这个场景比较特殊——Cilium 自己就支持 WireGuard 作为 pod-to-pod 加密通道：

```yaml
encryption:
  enabled: true
  type: wireguard
  nodeEncryption: true
```

启用后 Cilium 自动给每个节点生成 WireGuard 密钥，节点间所有 Pod 流量自动加密。这比传统的 IPSec transport mode 性能好得多，也比 Istio mTLS 省资源（因为在内核加密，不走 sidecar）。

**注意事项**：
- 启用后所有节点要能互相发 UDP 51820
- Pod 数量大的集群性能会下降 5~10%
- Cilium 1.16+ 才支持真正的 node-to-node（之前只是 pod-to-pod）

## 五、Netmaker：自动化的 mesh 管理

如果 Headscale 是"面向人的控制面"，**Netmaker** 就是"面向服务器的控制面"。它专门为数据中心/云节点互联设计，不像 Tailscale 面向终端设备。

核心差异：

| 特性 | Netmaker | Tailscale/Headscale |
|------|----------|---------------------|
| 定位 | 服务器、容器、IoT mesh | 办公/终端 VPN |
| 控制面 | 自托管 | 自托管(Headscale) / SaaS |
| 数据面协议 | 标准 WireGuard | 标准 WireGuard |
| 节点发现 | gRPC + REST API | DERP relay |
| NAT 穿透 | 内置 holepunch | DERP 中继 |
| ACL 粒度 | 粗（network 级别） | 细（per-service） |

Netmaker 适合：
- 管理多云服务器 mesh
- k8s 集群之间互联
- 物联网设备组网

部署 Netmaker Server（一个 docker-compose 就够）：

```yaml
version: "3.4"
services:
  netmaker:
    image: gravitl/netmaker:v0.24.3
    environment:
      MASTER_KEY: "yourrandommaster"
      SERVER_NAME: "nm.example.com"
      SERVER_HTTP_HOST: "api.nm.example.com"
      SERVER_BROKER_ENDPOINT: "mq.nm.example.com"
      DATABASE: "sqlite"
      CORS_ALLOWED_ORIGIN: "*"
    ports:
      - "8081:8081"
    volumes:
      - ./data:/root/data
      - /var/run/docker.sock:/var/run/docker.sock
```

Node 加入 mesh：

```bash
curl -sfL https://install.netmaker.org | VERSION=v0.24.3 sh -
netclient join -t <enrollment-token>
```

我们内部用 Netmaker 维护了一个"跨 3 云 12 region 的 mesh"，每 region 3 个节点作为边界，节点间自动 mesh，出故障时 API 推送新 peer list，节点自动更新配置。

## 六、真实踩坑记录

### 6.1 MTU 黑洞

最经典的 WireGuard 问题：TCP 连接能建立、小包能通、大包（比如文件下载）卡住。根因几乎都是 MTU。

症状：`ping -s 1400` 能通，`ping -s 1500` 不通。

根因：WireGuard 的 overlay MTU 比物理 MTU 小，但 TCP MSS 没被正确 clamp 到小值，导致 1500 字节的包经过 WireGuard 被分片，下游丢弃。

修复：

```bash
# 在 wg 接口上开启 MSS clamping
iptables -t mangle -A FORWARD -o wg0 -p tcp --tcp-flags SYN,RST SYN \
         -j TCPMSS --clamp-mss-to-pmtu
```

或者显式设 MTU：

```ini
[Interface]
MTU = 1380   # 再减 40 保险
```

我生产上是 **1380** 这个保守值，兼容所有场景。

### 6.2 双向 NAT 下的连不通

两端都在 NAT 后面（比如办公电脑之间想直连），没有公网 endpoint 怎么办？

WireGuard 原生不支持 NAT 穿透，需要上层协调。解决方案：

1. **STUN + 打洞**：Tailscale / Netmaker / wgsd 这类工具实现了 STUN 打洞，能让双向 NAT 的 peer 直连
2. **中继（DERP）**：打洞失败时走中继服务器转发
3. **放弃直连**：办公网到数据中心通常只需单向，没必要打洞

我生产场景基本只用 1 和 3，用 Tailscale/Headscale 的 MagicDNS + DERP 自动处理。

### 6.3 密钥轮换

WireGuard 私钥一旦生成就没法轮换（除非重建整个 peer），怎么办？

**方案 1：不轮换**。WireGuard 的加密强度设计上就是"私钥泄露前不轮换也安全"。实际生产大多数人就是这么做的。

**方案 2：定期全量换**。每季度用脚本自动重新生成所有节点密钥、更新 peer 配置、热加载。

```bash
wg set wg0 private-key <(echo "<new_privkey>")
```

WireGuard 支持不停服热更新私钥，只是要确保所有对端都更新了对你的 public key 认知。

**方案 3：预共享密钥（PSK）**。WireGuard 支持每个 peer 额外加一个 PSK，用于抵御"量子计算破解 Curve25519"这种未来威胁。PSK 可以定期轮换，不影响主密钥：

```ini
[Peer]
PublicKey = ...
PresharedKey = <base64 psk>
```

合规要求严格的生产环境（金融、政府）开启 PSK，普通场景可选。

### 6.4 Endpoint IP 变化

Endpoint 域名解析的 IP 变了怎么办？WireGuard 默认在启动时解析一次，之后用 IP 通信，域名变了不会自动重新解析。

解决：

```bash
# 每 5 分钟重新解析
wg-quick save wg0
# 或者用 systemd timer + 脚本
```

实际上用 `wg-quick down wg0 && wg-quick up wg0` 重启就行，损失一两秒连接。

### 6.5 策略路由冲突

wg-quick 会自动给 `AllowedIPs` 添加路由。如果 AllowedIPs 包含 `0.0.0.0/0`（全流量进 VPN），它会覆盖默认路由，导致本机流量全走 VPN。

修复：
```ini
[Interface]
Table = off    # 不自动加路由
# 自己写 PostUp 精确加
PostUp = ip route add 10.0.0.0/8 dev wg0
```

或者用"policy routing"：

```bash
ip rule add from 10.100.0.1 table 100
ip route add default dev wg0 table 100
```

这个坑在手机 VPN 场景非常常见（想让办公流量走 VPN，其他流量直连）。

## 七、监控与可观测性

WireGuard 本身 metric 很少，主要看这几个：

```bash
wg show wg0
# interface: wg0
#   public key: xxxxx
#   private key: (hidden)
#   listening port: 51820
#
# peer: <pubkey>
#   endpoint: 47.xx.xx.xx:51820
#   allowed ips: 10.200.0.11/32, 172.16.0.0/16
#   latest handshake: 25 seconds ago
#   transfer: 1.23 GiB received, 456.78 MiB sent
#   persistent keepalive: every 25 seconds
```

关键指标：

- **latest handshake**：超过 3 分钟未握手说明隧道断了
- **transfer**：流量计数，可以做流量告警
- **endpoint**：变化说明对端 IP 变了

Prometheus exporter 用 [`prometheus_wireguard_exporter`](https://github.com/MindFlavor/prometheus_wireguard_exporter)：

```
wireguard_peer_last_handshake_seconds{...}
wireguard_peer_receive_bytes_total{...}
wireguard_peer_transmit_bytes_total{...}
```

告警规则：

```yaml
- alert: WireguardPeerDown
  expr: time() - wireguard_peer_last_handshake_seconds > 300
  for: 2m
  labels: { severity: critical }
  annotations:
    summary: "WG peer {{ $labels.public_key }} 超过 5 分钟未握手"
```

## 八、实战建议

几条经验总结：

**1. 不要自己写 mesh 自动化**。密钥分发、ACL 同步这些事情用 Tailscale/Headscale/Netmaker，别自己造轮子。你写的肯定没人家成熟。

**2. MTU 固定写死**。所有节点用同一个 MTU 值（推荐 1380），不要指望路径 MTU discovery 帮你处理。

**3. 用云 CLB/NLB 做 endpoint**。不要把 WireGuard 直接暴露在 EC2 弹性 IP 上，用 NLB 的 UDP 监听器做前置，对端通过 NLB DNS 连接。好处：节点重启不影响对端、可以做灰度、能加健康检查。

**4. 带外管理面不要依赖 VPN 本身**。SSH 到 VPN 节点的通道不要走 VPN，否则 VPN 挂了就进不去机器了。保留独立的跳板机。

**5. 多云场景做流量成本监控**。WireGuard 跨云流量要走云厂商的外网出口，单向 0.05~0.1 美元/GB 不等，容易失控。定期统计 `wireguard_peer_transmit_bytes_total` 并核对账单。

**6. 备份密钥**。丢了私钥的后果是重建整个 peer 关系。所有 `privatekey` 文件加密存 Git / Vault / KMS，本地 `/etc/wireguard/*` 是 root 600 权限。

## 九、和前面几篇的关系

这篇是零信任系列的"数据平面"补充。前面几篇：
- Falco 负责**运行时监控**
- SPIRE 负责**工作负载身份**
- Sigstore 负责**制品可信**
- Dependency-Track 负责**组件漏洞**
- Cilium 负责**集群内 L3~L7 策略**

WireGuard 管的是**集群间/云间/办公到数据中心**的加密通道。这几层加起来才是我们现在跑的零信任栈，少一层都有缝。

下一篇写密钥自动轮换（Vault、AWS SM、SOPS），把"长期凭据"这个最后的软肋解掉。
