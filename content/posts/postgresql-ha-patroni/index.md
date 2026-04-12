---
title: "PostgreSQL 高可用实战：Patroni + HAProxy + etcd 完整部署指南"
date: 2026-04-12T10:00:00+08:00
draft: false
tags: ["PostgreSQL", "Patroni", "HAProxy", "etcd", "高可用", "数据库"]
categories: ["数据库"]
description: "从零搭建 Patroni + HAProxy + etcd 三节点 PostgreSQL 高可用集群，涵盖故障切换演练、patronictl 运维命令、Prometheus 监控集成，以及 Kubernetes 上 CloudNativePG Operator 方案。"
summary: "详解 Patroni 自动故障转移机制，手把手完成 etcd 三节点集群搭建、Patroni 完整配置（含 pg_hba.conf 托管）、HAProxy 读写分离配置，以及 kill primary 故障切换演练全过程。"
toc: true
math: false
diagram: false
keywords: ["Patroni", "HAProxy", "etcd", "PostgreSQL HA", "故障切换", "patronictl", "CloudNativePG", "pg_basebackup"]
params:
  reading_time: true
---

生产环境的 PostgreSQL 单点是最大的风险敞口。我们在把核心业务从 RDS 迁移到自建集群的过程中，选择了 Patroni 作为 HA 框架。Patroni 是目前社区最成熟的 PostgreSQL 高可用方案，Zalando、GitLab、Crunchy Data 都在生产大规模使用。这篇文章记录从零搭建的完整过程，踩过的坑都会标出来。

## 整体架构

```
                        ┌─────────────────┐
                        │    应用层         │
                        │  App / ORM       │
                        └────────┬────────┘
                                 │
                    ┌────────────┴────────────┐
                    │         HAProxy          │
                    │  :5000 (读写/主节点)      │
                    │  :5001 (只读/从节点)      │
                    └──────┬──────────┬───────┘
                           │          │
              ┌────────────┘          └────────────┐
              │                                     │
   ┌──────────▼──────────┐             ┌───────────▼──────────┐
   │  pg-node1 (Leader)  │             │  pg-node2 (Replica)  │
   │  Patroni + PG 16    │◄────WAL────►│  Patroni + PG 16     │
   │  192.168.1.101      │             │  192.168.1.102        │
   └─────────────────────┘             └──────────────────────┘
                                                    │
                                        ┌───────────▼──────────┐
                                        │  pg-node3 (Replica)  │
                                        │  Patroni + PG 16     │
                                        │  192.168.1.103        │
                                        └──────────────────────┘

   ┌──────────────────────────────────────────────────────────┐
   │              etcd 集群（3节点）                            │
   │  etcd1: 192.168.1.101   etcd2: 192.168.1.102            │
   │  etcd3: 192.168.1.103                                    │
   └──────────────────────────────────────────────────────────┘
```

**节点规划**：

| 主机名 | IP | 角色 |
|--------|-----|------|
| pg-node1 | 192.168.1.101 | Patroni + etcd + PostgreSQL |
| pg-node2 | 192.168.1.102 | Patroni + etcd + PostgreSQL |
| pg-node3 | 192.168.1.103 | Patroni + etcd + PostgreSQL |
| haproxy | 192.168.1.100 | HAProxy（可与 PG 节点合并）|

etcd 与 Patroni 节点复用，节省机器资源。生产环境建议 etcd 独立部署，避免 PostgreSQL IO 压力影响 etcd 的 fsync 延迟导致误判主节点宕机。

---

## 第一步：etcd 集群搭建

**三节点都要执行：**

```bash
# Ubuntu 22.04
apt-get update && apt-get install -y etcd-server etcd-client

# 或者手动下载指定版本
ETCD_VER=v3.5.12
curl -L https://github.com/etcd-io/etcd/releases/download/${ETCD_VER}/etcd-${ETCD_VER}-linux-amd64.tar.gz \
  -o /tmp/etcd.tar.gz
tar xzf /tmp/etcd.tar.gz -C /usr/local/bin --strip-components=1 \
  etcd-${ETCD_VER}-linux-amd64/etcd \
  etcd-${ETCD_VER}-linux-amd64/etcdctl
```

**etcd1（192.168.1.101）的配置文件 `/etc/etcd/etcd.conf`：**

```yaml
name: etcd1
data-dir: /var/lib/etcd
listen-client-urls: http://192.168.1.101:2379,http://127.0.0.1:2379
advertise-client-urls: http://192.168.1.101:2379
listen-peer-urls: http://192.168.1.101:2380
initial-advertise-peer-urls: http://192.168.1.101:2380
initial-cluster: etcd1=http://192.168.1.101:2380,etcd2=http://192.168.1.102:2380,etcd3=http://192.168.1.103:2380
initial-cluster-token: pg-etcd-cluster-prod
initial-cluster-state: new
# 心跳与选举超时，默认值对大多数场景够用
heartbeat-interval: 100
election-timeout: 1000
# 快照
snapshot-count: 10000
max-snapshots: 5
# 日志
logger: zap
log-level: warn
```

etcd2/etcd3 只改 `name` 和三处 IP 地址即可。

```bash
# systemd service
cat > /etc/systemd/system/etcd.service << 'EOF'
[Unit]
Description=etcd key-value store
After=network.target

[Service]
Type=notify
ExecStart=/usr/local/bin/etcd --config-file /etc/etcd/etcd.conf
Restart=always
RestartSec=5
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable etcd
systemctl start etcd
```

**验证 etcd 集群健康：**

```bash
etcdctl --endpoints=http://192.168.1.101:2379,http://192.168.1.102:2379,http://192.168.1.103:2379 \
  endpoint health

# 输出类似：
# http://192.168.1.101:2379 is healthy: successfully committed proposal: took = 2.1ms
# http://192.168.1.102:2379 is healthy: successfully committed proposal: took = 1.8ms
# http://192.168.1.103:2379 is healthy: successfully committed proposal: took = 2.4ms

etcdctl --endpoints=http://192.168.1.101:2379 \
  endpoint status --write-out=table
```

---

## 第二步：安装 PostgreSQL 16

**三节点都要执行：**

```bash
# 添加 PGDG 源
apt-get install -y curl ca-certificates
install -d /usr/share/postgresql-common/pgdg
curl -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
  --fail https://www.postgresql.org/media/keys/ACCC4CF8.asc
sh -c 'echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] \
  https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" \
  > /etc/apt/sources.list.d/pgdg.list'
apt-get update
apt-get install -y postgresql-16 postgresql-client-16

# 停止并禁用默认的 postgresql 服务（由 Patroni 接管启停）
systemctl stop postgresql
systemctl disable postgresql

# 清空默认数据目录（Patroni 会自己初始化）
rm -rf /var/lib/postgresql/16/main
```

---

## 第三步：安装 Patroni

```bash
apt-get install -y python3-pip python3-dev libpq-dev gcc

# 安装 Patroni + etcd 支持
pip3 install patroni[etcd] psycopg2-binary

# 或者用 pipx 隔离环境（推荐）
pipx install 'patroni[etcd]'
```

---

## 第四步：Patroni 配置文件

这是整个方案的核心，每个节点配置文件有差异，以下是 **pg-node1** 的 `/etc/patroni/patroni.yml`：

```yaml
scope: pg-cluster          # 集群名称，所有节点必须一致
namespace: /db/            # etcd 中的 key 前缀
name: pg-node1             # 本节点名称，每个节点唯一

restapi:
  listen: 192.168.1.101:8008       # Patroni REST API 监听地址
  connect_address: 192.168.1.101:8008
  # 生产建议加认证
  # authentication:
  #   username: patroni
  #   password: strongpassword

etcd3:
  hosts: 192.168.1.101:2379,192.168.1.102:2379,192.168.1.103:2379
  # 可选：开启 TLS
  # protocol: https
  # cacert: /etc/ssl/etcd/ca.crt

bootstrap:
  # DCS 中不存在集群时的初始化配置
  dcs:
    ttl: 30                        # leader key 的 TTL（秒），超时触发重新选举
    loop_wait: 10                  # Patroni 主循环间隔（秒）
    retry_timeout: 10              # 操作 DCS 的超时时间
    maximum_lag_on_failover: 1048576  # 允许故障转移的最大 WAL 滞后（1MB）
    # 同步复制：至少 1 个同步副本
    synchronous_mode: false
    # synchronous_mode_strict: false
    postgresql:
      use_pg_rewind: true          # 开启 pg_rewind，允许老主降级后追上新主
      use_slots: true              # 使用复制槽，防止 WAL 被清除
      parameters:
        wal_level: replica
        hot_standby: "on"
        wal_keep_size: 1024        # MB，保留 WAL 段
        max_wal_senders: 10
        max_replication_slots: 10
        wal_log_hints: "on"        # pg_rewind 需要
        archive_mode: "on"
        archive_command: 'test ! -f /var/lib/postgresql/wal_archive/%f && cp %p /var/lib/postgresql/wal_archive/%f'
        shared_buffers: 4GB
        effective_cache_size: 12GB
        maintenance_work_mem: 512MB
        checkpoint_completion_target: 0.9
        wal_buffers: 64MB
        default_statistics_target: 100
        random_page_cost: 1.1
        effective_io_concurrency: 200
        work_mem: 16MB
        min_wal_size: 1GB
        max_wal_size: 4GB
        max_worker_processes: 8
        max_parallel_workers_per_gather: 4
        max_parallel_workers: 8
        max_parallel_maintenance_workers: 4
        log_destination: stderr
        logging_collector: "on"
        log_directory: /var/log/postgresql
        log_filename: postgresql-%Y-%m-%d_%H%M%S.log
        log_min_duration_statement: 1000    # 慢查询阈值 1s
        log_checkpoints: "on"
        log_connections: "off"
        log_disconnections: "off"
        log_lock_waits: "on"
        log_temp_files: 0
        log_autovacuum_min_duration: 0
        track_activity_query_size: 4096
        shared_preload_libraries: pg_stat_statements
        pg_stat_statements.max: 10000
        pg_stat_statements.track: all

  # 初始化时执行的 SQL
  initdb:
    - encoding: UTF8
    - data-checksums       # 开启数据校验，生产必须
    - locale: en_US.UTF-8

  # Patroni 托管 pg_hba.conf，不要手动修改该文件
  pg_hba:
    - host replication replicator 192.168.1.0/24 md5
    - host all all 0.0.0.0/0 md5
    - local all all peer

  # 初始化后执行的 SQL（创建复制用户）
  post_init: /etc/patroni/post_init.sh

postgresql:
  listen: 192.168.1.101:5432       # 每个节点改为本机 IP
  connect_address: 192.168.1.101:5432
  data_dir: /var/lib/postgresql/16/main
  bin_dir: /usr/lib/postgresql/16/bin
  config_dir: /var/lib/postgresql/16/main
  pgpass: /tmp/pgpass0

  authentication:
    replication:
      username: replicator
      password: "ReplStr0ngPass!"
    superuser:
      username: postgres
      password: "PGSuperStr0ng!"
    rewind:
      username: rewind_user
      password: "RewindStr0ng!"

  # 额外的 recovery 参数（PG 12+ 写入 postgresql.conf）
  recovery_conf:
    restore_command: 'cp /var/lib/postgresql/wal_archive/%f %p'

tags:
  nofailover: false        # 设为 true 则此节点不参与选主
  noloadbalance: false     # 设为 true 则 HAProxy 不向此节点路由读请求
  clonefrom: false         # 设为 true 则优先从此节点克隆新副本
  nosync: false
```

**pg-node2 / pg-node3** 只需改三处：`name`、`restapi.listen`、`restapi.connect_address`、`postgresql.listen`、`postgresql.connect_address`。

**创建 post_init.sh：**

```bash
cat > /etc/patroni/post_init.sh << 'EOF'
#!/bin/bash
psql -U postgres << SQL
CREATE USER replicator REPLICATION LOGIN ENCRYPTED PASSWORD 'ReplStr0ngPass!';
CREATE USER rewind_user LOGIN ENCRYPTED PASSWORD 'RewindStr0ng!';
GRANT EXECUTE ON function pg_catalog.pg_ls_dir(text, boolean, boolean) TO rewind_user;
GRANT EXECUTE ON function pg_catalog.pg_stat_file(text, boolean) TO rewind_user;
GRANT EXECUTE ON function pg_catalog.pg_read_binary_file(text) TO rewind_user;
GRANT EXECUTE ON function pg_catalog.pg_read_binary_file(text, bigint, bigint, boolean) TO rewind_user;
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
SQL
EOF
chmod +x /etc/patroni/post_init.sh
```

**创建 systemd service：**

```bash
cat > /etc/systemd/system/patroni.service << 'EOF'
[Unit]
Description=Patroni - High Availability PostgreSQL Cluster
After=syslog.target network.target etcd.service
Requires=etcd.service

[Service]
Type=simple
User=postgres
Group=postgres
ExecStart=/usr/local/bin/patroni /etc/patroni/patroni.yml
ExecReload=/bin/kill -s HUP $MAINPID
KillMode=process
TimeoutSec=30
Restart=on-failure
RestartSec=5
# ulimits
LimitNOFILE=1048576

[Install]
WantedBy=multi-user.target
EOF

# 创建日志目录
mkdir -p /var/log/postgresql
chown postgres:postgres /var/log/postgresql

# 创建 WAL 归档目录
mkdir -p /var/lib/postgresql/wal_archive
chown postgres:postgres /var/lib/postgresql/wal_archive

# 启动（先启动 pg-node1）
systemctl daemon-reload
systemctl enable patroni
systemctl start patroni
systemctl status patroni
```

**在 pg-node1 成功初始化后，再依次启动 pg-node2、pg-node3**，Patroni 会自动通过 `pg_basebackup` 克隆主节点数据。

---

## 第五步：HAProxy 配置

HAProxy 实现两个虚拟端口：
- **5000**：写端口，只转发到当前 Leader（Patroni REST API 返回 HTTP 200 表示主节点，HTTP 503 表示从节点）
- **5001**：读端口，转发到所有 Replica

```bash
apt-get install -y haproxy
```

**`/etc/haproxy/haproxy.cfg`：**

```
global
    maxconn 100000
    log /dev/log local0
    log /dev/log local1 notice
    chroot /var/lib/haproxy
    stats socket /run/haproxy/admin.sock mode 660 level admin expose-fd listeners
    stats timeout 30s
    user haproxy
    group haproxy
    daemon

defaults
    log global
    mode tcp
    option tcplog
    option dontlognull
    timeout connect 5s
    timeout client 30s
    timeout server 30s
    timeout check 5s

#
# 统计页面
#
listen stats
    bind *:7000
    mode http
    stats enable
    stats uri /haproxy
    stats refresh 10s
    stats show-legends
    stats auth admin:haproxy_admin_pass

#
# 主节点（读写）端口 5000
# Patroni 主节点 REST API 返回 HTTP 200
#
listen pg_primary
    bind *:5000
    option httpchk GET /primary
    http-check expect status 200
    default-server inter 3s fall 3 rise 2 on-marked-down shutdown-sessions
    server pg-node1 192.168.1.101:5432 check port 8008
    server pg-node2 192.168.1.102:5432 check port 8008
    server pg-node3 192.168.1.103:5432 check port 8008

#
# 从节点（只读）端口 5001
# Patroni 从节点 REST API 返回 HTTP 200（/replica 接口）
#
listen pg_replicas
    bind *:5001
    balance roundrobin
    option httpchk GET /replica
    http-check expect status 200
    default-server inter 3s fall 3 rise 2 on-marked-down shutdown-sessions
    server pg-node1 192.168.1.101:5432 check port 8008
    server pg-node2 192.168.1.102:5432 check port 8008
    server pg-node3 192.168.1.103:5432 check port 8008
```

> **注意**：`/primary` 接口只在 Leader 节点返回 200，`/replica` 接口只在 Replica 节点返回 200。HAProxy 的健康检查会自动将当前主节点从只读池中排除。

```bash
haproxy -c -f /etc/haproxy/haproxy.cfg   # 验证配置语法
systemctl enable haproxy
systemctl start haproxy
```

**连接验证：**

```bash
psql -h 192.168.1.100 -p 5000 -U postgres -c "SELECT pg_is_in_recovery();"
# 应返回 f（false，即主节点）

psql -h 192.168.1.100 -p 5001 -U postgres -c "SELECT pg_is_in_recovery();"
# 应返回 t（true，即从节点）
```

---

## 第六步：patronictl 常用运维命令

```bash
# 设置 PATRONICTL_CONFIG_FILE 环境变量，简化命令
export PATRONICTL_CONFIG_FILE=/etc/patroni/patroni.yml

# 查看集群状态
patronictl -c /etc/patroni/patroni.yml list

# 输出示例：
# + Cluster: pg-cluster (7234567890123456789) +---------+----+-----------+
# | Member   | Host              | Role    | State   | TL | Lag in MB |
# +----------+-------------------+---------+---------+----+-----------+
# | pg-node1 | 192.168.1.101:5432 | Leader  | running |  1 |           |
# | pg-node2 | 192.168.1.102:5432 | Replica | running |  1 |         0 |
# | pg-node3 | 192.168.1.103:5432 | Replica | running |  1 |         0 |
# +----------+-------------------+---------+---------+----+-----------+

# 手动 Switchover（计划内切换，有确认提示）
patronictl -c /etc/patroni/patroni.yml switchover pg-cluster \
  --master pg-node1 --candidate pg-node2 --scheduled now

# 强制 Failover（紧急切换，不等当前主节点响应）
patronictl -c /etc/patroni/patroni.yml failover pg-cluster \
  --master pg-node1 --candidate pg-node2 --force

# 重启某个节点（等待，不强制）
patronictl -c /etc/patroni/patroni.yml restart pg-cluster pg-node2

# 重新加载配置（patroni.yml 改动后）
patronictl -c /etc/patroni/patroni.yml reload pg-cluster

# 暂停自动故障转移（维护窗口必用）
patronictl -c /etc/patroni/patroni.yml pause pg-cluster
# 恢复
patronictl -c /etc/patroni/patroni.yml resume pg-cluster

# 编辑 DCS 中的集群配置（等效于修改 bootstrap.dcs 段）
patronictl -c /etc/patroni/patroni.yml edit-config pg-cluster

# 查看历史时间线
patronictl -c /etc/patroni/patroni.yml history pg-cluster

# 删除某个成员的 DCS 注册（成员彻底下线时）
patronictl -c /etc/patroni/patroni.yml remove pg-cluster
```

---

## 第七步：故障切换演练

**演练场景：Kill 主节点，观察自动选主**

```bash
# 确认当前主节点
patronictl -c /etc/patroni/patroni.yml list
# pg-node1 是 Leader

# 终端1：持续监控
watch -n 1 'patronictl -c /etc/patroni/patroni.yml list'

# 终端2：模拟主节点宕机（在 pg-node1 上执行）
systemctl stop patroni

# 观察切换过程（约 30s，即 TTL 时间）：
# 1. pg-node1 状态变为 stopped
# 2. etcd 中 leader key TTL 超时（30s）
# 3. pg-node2 或 pg-node3 竞争 leader key
# 4. 获胜节点执行 promote，成为新 Leader
# 5. HAProxy 健康检查感知变化，流量切换（约 3-9s 后）
```

**HAProxy 侧验证：**

```bash
# 持续测试写端口是否恢复
while true; do
  psql -h 192.168.1.100 -p 5000 -U postgres -c "SELECT now(), pg_is_in_recovery();" 2>&1
  sleep 2
done
```

**pg_rewind 恢复老主节点：**

当 pg-node1 重新上线时，因为它的时间线已经落后于新主，不能直接加入集群。Patroni 配置了 `use_pg_rewind: true` 后会自动处理，但需要确认 `wal_log_hints=on` 已生效：

```bash
# 重新启动 pg-node1 的 Patroni
systemctl start patroni

# Patroni 会自动：
# 1. 检测到时间线不匹配
# 2. 执行 pg_rewind 从新主节点同步差异 WAL
# 3. 以 Replica 身份重新加入集群

# 如果 pg_rewind 失败，手动克隆：
systemctl stop patroni
rm -rf /var/lib/postgresql/16/main/*
pg_basebackup -h 192.168.1.102 -U replicator -D /var/lib/postgresql/16/main \
  -P -Xs -R -C -S pg-node1-slot
chown -R postgres:postgres /var/lib/postgresql/16/main
systemctl start patroni
```

---

## 第八步：监控集成

### Prometheus patroni_exporter

```bash
# 每个 Patroni 节点安装 patroni_exporter
pip3 install patroni[zookeeper,etcd,consul,kubernetes]

# 也可以用独立的 patroni exporter
# https://github.com/woblerr/patroni_exporter
wget https://github.com/woblerr/patroni_exporter/releases/download/v0.8.0/patroni_exporter_linux_amd64
chmod +x patroni_exporter_linux_amd64
mv patroni_exporter_linux_amd64 /usr/local/bin/patroni_exporter
```

实际上 **Patroni 内置了 `/metrics` 接口**，直接在 Prometheus 中刮取即可：

```yaml
# prometheus.yml
scrape_configs:
  - job_name: 'patroni'
    static_configs:
      - targets:
          - '192.168.1.101:8008'
          - '192.168.1.102:8008'
          - '192.168.1.103:8008'
    metrics_path: /metrics
```

**关键 Prometheus 告警规则：**

```yaml
# patroni-alerts.yml
groups:
  - name: patroni
    rules:
      - alert: PatroniClusterUnhealthy
        expr: patroni_cluster_unlocked == 1
        for: 1m
        labels:
          severity: critical
        annotations:
          summary: "Patroni 集群无 Leader（{{ $labels.scope }}）"

      - alert: PatroniReplicaLagging
        expr: patroni_replica_lag_in_megabytes > 100
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "副本 {{ $labels.patroni_member }} 延迟 {{ $value }}MB"

      - alert: PatroniMemberDown
        expr: patroni_patroni_info == 0
        for: 2m
        labels:
          severity: critical
        annotations:
          summary: "Patroni 节点 {{ $labels.instance }} 离线"

      - alert: PatroniFailoverDetected
        expr: changes(patroni_master[5m]) > 0
        for: 0m
        labels:
          severity: warning
        annotations:
          summary: "集群 {{ $labels.scope }} 发生了主节点切换"
```

### postgres_exporter 补充指标

```bash
# 安装 postgres_exporter
wget https://github.com/prometheus-community/postgres_exporter/releases/download/v0.15.0/postgres_exporter-0.15.0.linux-amd64.tar.gz
tar xzf postgres_exporter-0.15.0.linux-amd64.tar.gz
mv postgres_exporter-0.15.0.linux-amd64/postgres_exporter /usr/local/bin/

# 配置数据源
export DATA_SOURCE_NAME="postgresql://postgres:PGSuperStr0ng!@localhost:5432/postgres?sslmode=disable"
postgres_exporter --web.listen-address=":9187" &
```

---

## 在 Kubernetes 上：CloudNativePG Operator

如果数据库运行在 K8s 上，**CloudNativePG（CNPG）** 是 Patroni 的云原生替代方案，由原 Zalando postgres-operator 核心团队开发。

```bash
# 安装 CNPG Operator
kubectl apply -f \
  https://raw.githubusercontent.com/cloudnative-pg/cloudnative-pg/release-1.22/releases/cnpg-1.22.0.yaml
```

**Cluster 资源定义（三节点，内置 HAProxy 等效功能）：**

```yaml
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: pg-cluster
  namespace: database
spec:
  instances: 3
  imageName: ghcr.io/cloudnative-pg/postgresql:16.2

  postgresql:
    parameters:
      shared_buffers: "4GB"
      work_mem: "16MB"
      max_connections: "200"
      wal_level: "replica"
      max_wal_senders: "10"
      shared_preload_libraries: "pg_stat_statements"
      pg_stat_statements.max: "10000"
      log_min_duration_statement: "1000"
    pg_hba:
      - host all all 10.0.0.0/8 md5
      - host replication replicator 10.0.0.0/8 md5

  bootstrap:
    initdb:
      database: appdb
      owner: appuser
      secret:
        name: pg-user-secret
      encoding: UTF8
      dataChecksums: true

  storage:
    size: 100Gi
    storageClass: gp3

  walStorage:
    size: 20Gi
    storageClass: gp3

  backup:
    retentionPolicy: "30d"
    barmanObjectStore:
      destinationPath: s3://your-bucket/pg-cluster
      s3Credentials:
        accessKeyId:
          name: aws-creds
          key: ACCESS_KEY_ID
        secretAccessKey:
          name: aws-creds
          key: ACCESS_SECRET_KEY
      wal:
        compression: gzip
        maxParallel: 8

  resources:
    requests:
      memory: "8Gi"
      cpu: "2"
    limits:
      memory: "16Gi"
      cpu: "4"

  # 自动故障转移配置
  failoverDelay: 0
  switchoverDelay: 3600

  # 监控
  monitoring:
    enablePodMonitor: true
```

CNPG 会自动创建三个 Service：
- `pg-cluster-rw`：指向 Leader（应用写连接）
- `pg-cluster-ro`：指向 Replica（负载均衡只读）
- `pg-cluster-r`：指向所有节点

---

## 常见问题

**1. etcd key TTL 超时时间应该设多少？**

`ttl: 30` 意味着主节点宕机后最长 30 秒内完成切换。对于多数业务可接受，如需更快切换可设 15，但太小会导致网络抖动引发误切。

**2. `maximum_lag_on_failover` 的作用**

如果所有 Replica 的 WAL 滞后都超过这个值（默认 1MB），Patroni 会拒绝自动故障转移，避免数据丢失，需要 DBA 手动介入。

**3. 两个节点都认为自己是 Leader（脑裂）**

Patroni 通过 etcd 的 CAS（Compare-And-Swap）操作确保同一时刻只有一个 Leader 持有 key，从协议层面杜绝脑裂。但 etcd 本身崩溃时，Patroni 会进入 `pause` 模式，保持现状不切换。

**4. pg_rewind 权限问题**

`rewind_user` 需要特定函数的 EXECUTE 权限（已在 `post_init.sh` 中授权）。PostgreSQL 15+ 可以直接 `GRANT pg_rewind TO rewind_user;`。

**5. 生产建议**

- etcd 使用 SSD，`fsync` 延迟直接影响 TTL 判断准确性
- `synchronous_mode: true` 可开启同步复制，RPO=0 但写延迟升高
- 维护窗口操作前务必先 `patronictl pause`，避免意外故障转移
- 定期测试 `switchover`，确保切换流程熟练
