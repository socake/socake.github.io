---
title: "ETCD 运维实战：部署、备份恢复与 K8s 集群数据管理"
date: 2026-04-11T08:00:00+08:00
draft: false
tags: ["ETCD", "Kubernetes", "运维", "分布式", "备份"]
categories: ["Kubernetes"]
description: "从 ETCD 在 Kubernetes 中的核心地位出发，深入讲解三节点集群部署、日常运维命令、自动化备份策略及数据恢复全流程，结合 confd 动态配置实践和真实踩坑记录。"
summary: "ETCD 是 Kubernetes 的命脉，所有集群状态都存储在这里。本文从实际运维角度梳理部署、备份、恢复和配置动态更新的完整操作链路，包含多个踩坑经验。"
toc: true
math: false
diagram: false
keywords: ["ETCD", "etcdctl", "Kubernetes备份", "ETCD恢复", "confd", "Raft协议"]
params:
  reading_time: true
---

## ETCD 在 Kubernetes 中的位置

如果把 Kubernetes 集群比作一家公司，ETCD 就是那个存放所有档案的保险柜。Pod 的状态、Service 的 ClusterIP、ConfigMap 的内容、RBAC 的权限规则——所有这些都以键值对的形式持久化在 ETCD 里。`kube-apiserver` 是唯一能直接读写 ETCD 的组件，其他控制面组件（scheduler、controller-manager）本质上都是在通过 apiserver 间接操作 ETCD 中的数据。

这意味着一件事：**ETCD 挂了，K8s 集群就废了**。现有的 Pod 还能跑，但你无法创建新资源、无法调度、无法做任何控制面操作。ETCD 数据丢失更糟糕，基本等于集群报废重建。

所以对于任何生产级 K8s 集群，ETCD 的运维能力不是加分项，是基本线。

## Raft 协议与奇数节点的原因

ETCD 基于 Raft 协议实现强一致性。Raft 的核心思想是"多数派确认"：一次写操作必须得到超过半数节点的确认，才算提交成功。

**为什么要奇数个节点？**

节点数 N 时，容错数 = `(N-1)/2`，即能容忍的故障节点数。

| 节点数 | 多数派 | 容错数 |
|--------|--------|--------|
| 1      | 1      | 0      |
| 2      | 2      | 0      |
| 3      | 2      | 1      |
| 4      | 3      | 1      |
| 5      | 3      | 2      |

可以看到 4 个节点和 3 个节点的容错数相同，都只能容忍 1 个节点故障，但多了一个节点的成本和 IO 开销。同理 6 节点和 5 节点一样。所以生产环境标准配置是 **3 节点或 5 节点**，奇数是为了避免"浪费"节点。

Raft 的 Leader 选举流程：每个 Follower 都有一个随机的选举超时时间（150-300ms），超时后成为 Candidate 并向其他节点发起投票请求。第一个获得多数派投票的 Candidate 成为新的 Leader，之后以固定心跳间隔（通常 100ms）向 Follower 发送心跳，维持 Leader 身份。

## 三节点集群部署

### 环境规划

```
etcd-1: 192.168.1.101
etcd-2: 192.168.1.102
etcd-3: 192.168.1.103
```

### 生成 TLS 证书

生产环境必须启用 TLS，这里用 cfssl 生成证书：

```bash
# 安装 cfssl
wget https://github.com/cloudflare/cfssl/releases/download/v1.6.4/cfssl_1.6.4_linux_amd64 -O /usr/local/bin/cfssl
wget https://github.com/cloudflare/cfssl/releases/download/v1.6.4/cfssljson_1.6.4_linux_amd64 -O /usr/local/bin/cfssljson
chmod +x /usr/local/bin/cfssl /usr/local/bin/cfssljson

# CA 配置
cat > ca-config.json <<EOF
{
  "signing": {
    "default": { "expiry": "87600h" },
    "profiles": {
      "etcd": {
        "expiry": "87600h",
        "usages": ["signing", "key encipherment", "server auth", "client auth"]
      }
    }
  }
}
EOF

cat > ca-csr.json <<EOF
{
  "CN": "etcd-ca",
  "key": { "algo": "rsa", "size": 2048 },
  "names": [{ "C": "CN", "ST": "Beijing", "O": "etcd-cluster" }]
}
EOF

cfssl gencert -initca ca-csr.json | cfssljson -bare ca

# 生成 etcd server/peer 证书（三个 IP 都写进 hosts）
cat > etcd-csr.json <<EOF
{
  "CN": "etcd",
  "hosts": [
    "192.168.1.101", "192.168.1.102", "192.168.1.103",
    "127.0.0.1", "localhost"
  ],
  "key": { "algo": "rsa", "size": 2048 }
}
EOF

cfssl gencert -ca=ca.pem -ca-key=ca-key.pem -config=ca-config.json \
  -profile=etcd etcd-csr.json | cfssljson -bare etcd
```

### systemd 启动配置

以 etcd-1 为例，创建 `/etc/systemd/system/etcd.service`：

```ini
[Unit]
Description=etcd
After=network.target

[Service]
Type=notify
ExecStart=/usr/local/bin/etcd \
  --name=etcd-1 \
  --data-dir=/var/lib/etcd \
  --listen-peer-urls=https://192.168.1.101:2380 \
  --listen-client-urls=https://192.168.1.101:2379,https://127.0.0.1:2379 \
  --advertise-client-urls=https://192.168.1.101:2379 \
  --initial-advertise-peer-urls=https://192.168.1.101:2380 \
  --initial-cluster=etcd-1=https://192.168.1.101:2380,etcd-2=https://192.168.1.102:2380,etcd-3=https://192.168.1.103:2380 \
  --initial-cluster-token=etcd-cluster-prod \
  --initial-cluster-state=new \
  --cert-file=/etc/etcd/tls/etcd.pem \
  --key-file=/etc/etcd/tls/etcd-key.pem \
  --peer-cert-file=/etc/etcd/tls/etcd.pem \
  --peer-key-file=/etc/etcd/tls/etcd-key.pem \
  --trusted-ca-file=/etc/etcd/tls/ca.pem \
  --peer-trusted-ca-file=/etc/etcd/tls/ca.pem \
  --peer-client-cert-auth=true \
  --client-cert-auth=true \
  --auto-compaction-retention=1 \
  --quota-backend-bytes=8589934592
Restart=on-failure
RestartSec=5s
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
```

`--quota-backend-bytes=8589934592` 把数据库大小上限设为 8GB，默认是 2GB，生产环境必须调大，否则 ETCD 会进入只读模式。`--auto-compaction-retention=1` 表示保留 1 小时内的历史版本，防止 boltdb 无限增长。

其他两个节点修改 `--name`、`--listen-peer-urls`、`--listen-client-urls`、`--advertise-client-urls`、`--initial-advertise-peer-urls` 中的 IP，`--initial-cluster-state` 仍为 `new`。

```bash
# 三台节点都执行
systemctl daemon-reload
systemctl enable etcd
systemctl start etcd
```

## 日常运维命令

操作 ETCD 统一用 `etcdctl`，注意 v3 API 需要设置环境变量：

```bash
# 设置环境变量（写入 ~/.bashrc 或 /etc/profile.d/etcd.sh）
export ETCDCTL_API=3
export ETCDCTL_ENDPOINTS="https://192.168.1.101:2379,https://192.168.1.102:2379,https://192.168.1.103:2379"
export ETCDCTL_CACERT=/etc/etcd/tls/ca.pem
export ETCDCTL_CERT=/etc/etcd/tls/etcd.pem
export ETCDCTL_KEY=/etc/etcd/tls/etcd-key.pem
```

### 集群状态检查

```bash
# 查看成员列表
etcdctl member list -w table

# 输出示例
+------------------+---------+--------+-----------------------------+-----------------------------+------------+
|        ID        | STATUS  |  NAME  |         PEER ADDRS          |        CLIENT ADDRS         | IS LEARNER |
+------------------+---------+--------+-----------------------------+-----------------------------+------------+
| 1234567890abcdef | started | etcd-1 | https://192.168.1.101:2380  | https://192.168.1.101:2379  |      false |
| abcdef1234567890 | started | etcd-2 | https://192.168.1.102:2380  | https://192.168.1.102:2379  |      false |
| fedcba0987654321 | started | etcd-3 | https://192.168.1.103:2380  | https://192.168.1.103:2379  |      false |
+------------------+---------+--------+-----------------------------+-----------------------------+------------+

# 检查各节点健康状态
etcdctl endpoint health -w table

# 查看各节点延迟和 Leader
etcdctl endpoint status -w table

# 输出里的 IS LEADER 列可以看出谁是 Leader
```

### 数据操作

```bash
# 写入键值
etcdctl put /config/app/env "production"

# 读取
etcdctl get /config/app/env

# 按前缀列出所有键（类似 ls）
etcdctl get /config/ --prefix --keys-only

# 监听键变化（实时）
etcdctl watch /config/app/env

# 查看 K8s 中某个 namespace 下的 Pod（K8s 数据都在 /registry/ 下）
etcdctl get /registry/pods/default --prefix --keys-only
```

### 压缩和碎片整理

```bash
# 获取当前 revision
REV=$(etcdctl endpoint status --write-out="json" | python3 -c "import sys,json; data=json.load(sys.stdin); print(data[0]['Status']['header']['revision'])")

# 压缩旧版本（保留当前 revision）
etcdctl compact $REV

# 碎片整理（每个节点都要执行，会短暂阻塞）
etcdctl defrag --endpoints=https://192.168.1.101:2379
etcdctl defrag --endpoints=https://192.168.1.102:2379
etcdctl defrag --endpoints=https://192.168.1.103:2379
```

## 备份策略：定时快照

ETCD 的 `snapshot save` 命令会把整个数据库状态导出为一个文件，是最可靠的备份方式。

### 备份脚本

```bash
#!/bin/bash
# /opt/scripts/etcd-backup.sh

set -euo pipefail

BACKUP_DIR="/data/etcd-backups"
RETENTION_DAYS=7
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="${BACKUP_DIR}/etcd-snapshot-${TIMESTAMP}.db"

# TLS 参数
ENDPOINTS="https://127.0.0.1:2379"
CACERT="/etc/etcd/tls/ca.pem"
CERT="/etc/etcd/tls/etcd.pem"
KEY="/etc/etcd/tls/etcd-key.pem"

# 创建备份目录
mkdir -p "${BACKUP_DIR}"

# 执行快照
ETCDCTL_API=3 etcdctl snapshot save "${BACKUP_FILE}" \
  --endpoints="${ENDPOINTS}" \
  --cacert="${CACERT}" \
  --cert="${CERT}" \
  --key="${KEY}"

# 验证快照完整性
ETCDCTL_API=3 etcdctl snapshot status "${BACKUP_FILE}" -w table

# 压缩
gzip "${BACKUP_FILE}"

echo "[$(date)] Backup completed: ${BACKUP_FILE}.gz"

# 删除超过保留期的备份
find "${BACKUP_DIR}" -name "etcd-snapshot-*.db.gz" -mtime +${RETENTION_DAYS} -delete

echo "[$(date)] Cleanup done, keeping last ${RETENTION_DAYS} days"
```

### Cron 配置

```bash
# /etc/cron.d/etcd-backup
# 每天凌晨 2 点执行备份，仅在 etcd-1 节点运行
0 2 * * * root /opt/scripts/etcd-backup.sh >> /var/log/etcd-backup.log 2>&1
```

**注意**：只需要在一个节点做备份，因为 ETCD 是强一致的，任意节点的快照都包含完整数据。我习惯选非 Leader 节点备份，避免对 Leader 的 IO 造成额外压力。

### 备份到 S3（可选）

```bash
# 在备份脚本末尾追加
aws s3 cp "${BACKUP_FILE}.gz" "s3://your-backup-bucket/etcd/${TIMESTAMP}/" \
  --storage-class STANDARD_IA

# 验证上传
aws s3 ls "s3://your-backup-bucket/etcd/${TIMESTAMP}/"
```

## 数据恢复流程

**恢复是最需要冷静的操作**。错误的恢复步骤可能让集群状态更混乱。

### 场景：三节点全部宕机，从快照恢复

```bash
# Step 1: 停止所有节点的 etcd（三台都执行）
systemctl stop etcd

# Step 2: 备份当前损坏的数据目录（以防万一）
mv /var/lib/etcd /var/lib/etcd.broken.$(date +%Y%m%d)

# Step 3: 从快照恢复（三台都要执行，但用各自的配置）
# 在 etcd-1 上
ETCDCTL_API=3 etcdctl snapshot restore /tmp/etcd-snapshot-20260411.db \
  --name=etcd-1 \
  --data-dir=/var/lib/etcd \
  --initial-cluster=etcd-1=https://192.168.1.101:2380,etcd-2=https://192.168.1.102:2380,etcd-3=https://192.168.1.103:2380 \
  --initial-cluster-token=etcd-cluster-prod \
  --initial-advertise-peer-urls=https://192.168.1.101:2380

# 在 etcd-2 上（修改 name 和 initial-advertise-peer-urls）
ETCDCTL_API=3 etcdctl snapshot restore /tmp/etcd-snapshot-20260411.db \
  --name=etcd-2 \
  --data-dir=/var/lib/etcd \
  --initial-cluster=etcd-1=https://192.168.1.101:2380,etcd-2=https://192.168.1.102:2380,etcd-3=https://192.168.1.103:2380 \
  --initial-cluster-token=etcd-cluster-prod \
  --initial-advertise-peer-urls=https://192.168.1.102:2380

# etcd-3 同理

# Step 4: 三台都恢复后，同时启动（或者依次启动，但要快）
systemctl start etcd

# Step 5: 验证集群恢复
etcdctl endpoint health -w table
etcdctl member list -w table
```

### 在 kubeadm 搭建的 K8s 集群中恢复

kubeadm 环境的 ETCD 通常是以 static pod 形式运行在 `/etc/kubernetes/manifests/etcd.yaml`：

```bash
# Step 1: 停止 apiserver 和 etcd（移出 manifests 目录让 kubelet 停止管理）
cd /etc/kubernetes/manifests/
mv etcd.yaml /tmp/
mv kube-apiserver.yaml /tmp/

# 等待容器停止
sleep 10

# Step 2: 恢复数据（data dir 通常在 /var/lib/etcd）
ETCDCTL_API=3 etcdctl snapshot restore /tmp/etcd-snapshot.db \
  --data-dir=/var/lib/etcd-restore \
  --name=master \
  --initial-cluster=master=https://127.0.0.1:2380 \
  --initial-advertise-peer-urls=https://127.0.0.1:2380

# Step 3: 替换数据目录
mv /var/lib/etcd /var/lib/etcd.old
mv /var/lib/etcd-restore /var/lib/etcd

# Step 4: 恢复 manifests，kubelet 会自动重启这些 static pod
mv /tmp/etcd.yaml /etc/kubernetes/manifests/
mv /tmp/kube-apiserver.yaml /etc/kubernetes/manifests/

# Step 5: 观察 pod 启动
watch crictl ps | grep -E "etcd|apiserver"
```

## confd：监听 ETCD 动态更新配置

confd 是一个轻量工具，监听 ETCD（或 Consul）中的键值变化，自动渲染模板并重启相关服务，实现配置的动态下发。

### 安装

```bash
wget https://github.com/kelseyhightower/confd/releases/download/v0.19.0/confd-0.19.0-linux-amd64
mv confd-0.19.0-linux-amd64 /usr/local/bin/confd
chmod +x /usr/local/bin/confd
```

### 目录结构

```
/etc/confd/
├── conf.d/
│   └── nginx.toml          # resource 配置：监听哪些键、触发什么命令
└── templates/
    └── nginx.conf.tmpl     # Go template 格式的配置模板
```

### 示例：动态更新 nginx upstream

```toml
# /etc/confd/conf.d/nginx.toml
[template]
src = "nginx.conf.tmpl"
dest = "/etc/nginx/conf.d/upstream.conf"
keys = [
  "/services/web/servers"
]
check_cmd = "nginx -t"
reload_cmd = "systemctl reload nginx"
```

```nginx
# /etc/confd/templates/nginx.conf.tmpl
upstream web_backend {
  {{range getvs "/services/web/servers/*"}}
  server {{.}};
  {{end}}
}
```

向 ETCD 写入服务节点：

```bash
etcdctl put /services/web/servers/1 "192.168.1.201:8080"
etcdctl put /services/web/servers/2 "192.168.1.202:8080"
```

启动 confd（watch 模式持续监听）：

```bash
confd -watch -backend etcdv3 \
  -node https://192.168.1.101:2379 \
  -client-ca-keys /etc/etcd/tls/ca.pem \
  -client-cert /etc/etcd/tls/etcd.pem \
  -client-key /etc/etcd/tls/etcd-key.pem
```

写入新节点后 confd 会自动渲染模板、校验配置、reload nginx，整个过程秒级完成。

## 踩坑记录

### 坑 1：磁盘 IO 高导致选举超时

症状：监控告警 ETCD Leader 频繁切换，`etcdctl endpoint health` 时不时报某个节点 unhealthy，但节点本身并没有宕机。

排查过程：
```bash
# 查看 etcd 日志
journalctl -u etcd -f | grep -E "slow|timeout|leader"

# 典型错误
# "apply entries took too long [1.2s for 10 entries]"
# "leader failed to send out heartbeat on time"
# "elected leader ... at term X"
```

根因：ETCD 数据目录和系统日志在同一块磁盘，日志高峰期 IO 打满，ETCD 的 WAL 写入延迟超过了心跳超时阈值（默认 1s），触发重新选举。

解决方案：
1. ETCD 数据目录单独挂载 SSD，建议用低延迟的 NVMe（云上用 io2/GP3 高 IOPS 类型）
2. 适当增大心跳超时：`--heartbeat-interval=250` 和 `--election-timeout=1250`（单位 ms，选举超时应为心跳的 10 倍）
3. 开启 IO 调度器优化：`echo deadline > /sys/block/sda/queue/scheduler`

### 坑 2：snapshot restore 后集群无法组建

症状：执行了 restore 命令，三台节点都起来了，但是 `etcdctl member list` 一直报错，节点互相看不到对方。

原因：`snapshot restore` 时 `--initial-cluster-token` 写错了，三台节点用了不同的 token，导致它们认为自己属于不同的集群。

教训：restore 脚本要用变量统一管理 `CLUSTER_TOKEN`，不要手敲，三台节点必须使用完全相同的 token。

### 坑 3：ETCD 数据库满了进入 only read 模式

症状：K8s 无法创建任何新资源，apiserver 日志报 `etcdserver: mvcc: database space exceeded`。

```bash
# 检查数据库大小
etcdctl endpoint status -w table
# DB SIZE 列如果接近或超过 quota-backend-bytes 就会触发
```

应急处理：
```bash
# 1. 压缩历史版本
REV=$(etcdctl endpoint status --write-out="json" | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(data[0]['Status']['header']['revision'])
")
etcdctl compact $REV

# 2. 碎片整理
for endpoint in https://192.168.1.101:2379 https://192.168.1.102:2379 https://192.168.1.103:2379; do
  etcdctl defrag --endpoints=$endpoint
done

# 3. 解除告警（数据库满时 ETCD 会设置 NOSPACE alarm）
etcdctl alarm disarm
```

预防：定期执行压缩和碎片整理，监控 `etcd_mvcc_db_total_size_in_bytes` 指标，超过 quota 的 70% 时告警。

### 坑 4：备份文件恢复时提示 "hash mismatch"

原因：snapshot 文件在传输过程中损坏，或者用了旧版 etcdctl（v3.3 以下）的快照与新版 etcdctl 不兼容。

解决：传输后先用 `etcdctl snapshot status <file>` 验证完整性，备份脚本里加 MD5 校验，etcdctl 版本要和 ETCD server 版本匹配。

```bash
# 备份时生成 checksum
sha256sum "${BACKUP_FILE}.gz" > "${BACKUP_FILE}.gz.sha256"

# 恢复前验证
sha256sum -c "${BACKUP_FILE}.gz.sha256"
```
