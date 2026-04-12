---
title: "MySQL 高可用实战：MGR + ProxySQL + Orchestrator 完整部署"
date: 2026-04-12T14:00:00+08:00
draft: false
tags: ["MySQL", "MGR", "ProxySQL", "Orchestrator", "高可用", "数据库"]
categories: ["数据库"]
description: "从零部署 MySQL Group Replication 三节点集群，配合 ProxySQL 实现读写分离和连接池，Orchestrator 管理拓扑可视化和自动故障转移，附 XtraBackup 备份策略。"
summary: "详细讲解 MySQL 8.0 MGR 单主模式完整搭建过程、脑裂与 GTID 不一致处理方法、ProxySQL 读写分离配置和健康检查脚本、Orchestrator 自动故障转移与 ProxySQL 联动，以及 mysqld_exporter 监控集成。"
toc: true
math: false
diagram: false
keywords: ["MySQL Group Replication", "MGR", "ProxySQL", "Orchestrator", "读写分离", "XtraBackup", "mysqld_exporter", "高可用"]
params:
  reading_time: true
---

MySQL 高可用方案的演进走了不少弯路。从早年的主从 + Keepalived，到 MHA（Master High Availability Manager），再到 MGR，每一代都是在填上一代的坑。这篇文章集中在目前最主流的自建 HA 方案：**MGR 单主模式 + ProxySQL + Orchestrator**，这个组合在国内中大型互联网公司落地最广。

## 方案演进简述

| 方案 | 优点 | 主要缺陷 |
|------|------|----------|
| 主从 + Keepalived/VIP | 简单 | 切换依赖脚本，数据可能丢失，无法保证一致性 |
| MHA | 较成熟，社区久 | 需要 SSH 互信，binlog 补偿可能失败，作者已不维护 |
| MGR 单主 | 基于 Paxos 协议，数据强一致，官方原生支持 | 配置复杂，对网络延迟敏感，大事务性能下降 |
| MGR 多主 | 多点写入 | 冲突检测开销大，DDL 限制多，生产少用 |
| AWS RDS Multi-AZ | 全托管，简单 | 贵，黑盒，定制空间小 |

**本文选择 MGR 单主模式**，搭配 ProxySQL 做代理层，Orchestrator 做拓扑管理。

---

## 整体架构

```
                    ┌─────────────────┐
                    │    应用层         │
                    │  App / ORM       │
                    └────────┬────────┘
                             │
                    ┌────────▼────────┐
                    │    ProxySQL      │
                    │  :6033 (读写分离)│
                    │  :6032 (管理端)  │
                    └──┬──────────┬───┘
                       │          │
            ┌──────────┘          └──────────┐
            │  写流量（hostgroup 10）          │  读流量（hostgroup 20）
            │                                 │
  ┌─────────▼───────┐          ┌─────────────▼──────────┐
  │ mysql-node1     │          │  mysql-node2 / node3    │
  │ MGR Primary     │◄──MGR───►│  MGR Secondary          │
  │ 192.168.1.201   │          │  .202 / .203            │
  └─────────────────┘          └────────────────────────┘

  ┌──────────────────────────────────────────────────────┐
  │              Orchestrator (单节点或集群)               │
  │  192.168.1.200:3000  Web UI + API                    │
  │  监控拓扑 + 触发故障转移 + 更新 ProxySQL 后端            │
  └──────────────────────────────────────────────────────┘
```

---

## 第一步：MySQL 8.0 三节点基础配置

**环境准备（三节点都执行）：**

```bash
# 安装 MySQL 8.0
apt-get install -y mysql-server-8.0

# 关闭 AppArmor 对 MySQL 的限制（可选，调试期间）
# aa-complain /usr/sbin/mysqld
```

**关键：my.cnf 配置。以下是 mysql-node1 的 `/etc/mysql/mysql.conf.d/mysqld.cnf`：**

```ini
[mysqld]
# 基础配置
server-id = 1                          # 每个节点唯一：1/2/3
bind-address = 0.0.0.0
port = 3306
datadir = /var/lib/mysql
socket = /var/run/mysqld/mysqld.sock
log_error = /var/log/mysql/error.log
pid-file = /var/run/mysqld/mysqld.pid

# GTID（MGR 强依赖）
gtid_mode = ON
enforce_gtid_consistency = ON

# Binlog
log_bin = /var/log/mysql/mysql-bin
binlog_format = ROW
binlog_row_image = FULL             # MGR 需要 FULL
log_replica_updates = ON            # 从库也写 binlog，MGR 必须
expire_logs_days = 7

# InnoDB
innodb_buffer_pool_size = 8G
innodb_buffer_pool_instances = 8
innodb_log_file_size = 2G
innodb_log_buffer_size = 64M
innodb_flush_log_at_trx_commit = 1  # 强一致，不要改 0/2
innodb_flush_method = O_DIRECT
innodb_file_per_table = ON
innodb_io_capacity = 2000
innodb_io_capacity_max = 4000
innodb_read_io_threads = 8
innodb_write_io_threads = 8
innodb_lru_scan_depth = 512

# 连接
max_connections = 500
wait_timeout = 300
interactive_timeout = 300
net_read_timeout = 60
net_write_timeout = 60

# 慢查询
slow_query_log = ON
slow_query_log_file = /var/log/mysql/slow.log
long_query_time = 1
log_queries_not_using_indexes = ON
min_examined_row_limit = 100

# MGR 核心配置
plugin_load_add = group_replication.so
group_replication_group_name = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"  # 用 UUID 生成
group_replication_start_on_boot = OFF             # 先关掉，手动启动
group_replication_local_address = "192.168.1.201:33061"   # 本节点 IP
group_replication_group_seeds = "192.168.1.201:33061,192.168.1.202:33061,192.168.1.203:33061"
group_replication_bootstrap_group = OFF           # 仅 node1 首次启动时设为 ON
group_replication_single_primary_mode = ON        # 单主模式
group_replication_enforce_update_everywhere_checks = OFF  # 单主关闭

# 白名单：允许 MGR 成员互相连接
# MySQL 8.0.22+ 改为 group_replication_ip_allowlist
group_replication_ip_allowlist = "192.168.1.0/24,127.0.0.1/8"

# 事务超时（大事务在 MGR 中会阻塞所有节点认证）
group_replication_transaction_size_limit = 150000000   # 150MB，超过报错

# 流量控制（避免从节点大幅落后）
group_replication_flow_control_mode = QUOTA
group_replication_flow_control_applier_threshold = 25000
group_replication_flow_control_certifier_threshold = 25000

# 性能 schema（监控需要）
performance_schema = ON
```

node2 改 `server-id=2`，`group_replication_local_address="192.168.1.202:33061"`；node3 类似。

---

## 第二步：MGR 集群初始化

**在 node1 上操作：**

```sql
-- 创建 MGR 复制用户
CREATE USER 'repl'@'%' IDENTIFIED WITH mysql_native_password BY 'ReplStr0ng!';
GRANT REPLICATION SLAVE ON *.* TO 'repl'@'%';
GRANT BACKUP_ADMIN ON *.* TO 'repl'@'%';   -- MySQL 8.0 备份权限
FLUSH PRIVILEGES;

-- 配置复制通道（MGR 内部使用）
CHANGE REPLICATION SOURCE TO
  SOURCE_USER='repl',
  SOURCE_PASSWORD='ReplStr0ng!'
  FOR CHANNEL 'group_replication_recovery';

-- 首次启动：临时开启 bootstrap
SET GLOBAL group_replication_bootstrap_group = ON;
START GROUP_REPLICATION;
SET GLOBAL group_replication_bootstrap_group = OFF;

-- 验证 node1 是 Primary
SELECT * FROM performance_schema.replication_group_members;
```

**在 node2、node3 上依次操作：**

```sql
-- 配置复制通道
CHANGE REPLICATION SOURCE TO
  SOURCE_USER='repl',
  SOURCE_PASSWORD='ReplStr0ng!'
  FOR CHANNEL 'group_replication_recovery';

-- 加入集群（不需要 bootstrap）
START GROUP_REPLICATION;

-- 验证
SELECT MEMBER_HOST, MEMBER_ROLE, MEMBER_STATE
FROM performance_schema.replication_group_members;

-- 预期输出：
-- +------------------+-------------+--------------+
-- | MEMBER_HOST      | MEMBER_ROLE | MEMBER_STATE |
-- +------------------+-------------+--------------+
-- | 192.168.1.201    | PRIMARY     | ONLINE       |
-- | 192.168.1.202    | SECONDARY   | ONLINE       |
-- | 192.168.1.203    | SECONDARY   | ONLINE       |
-- +------------------+-------------+--------------+
```

**设置开机自动加入集群：**

初始化完成后，将 `my.cnf` 中 `group_replication_start_on_boot = ON`，并创建一个 systemd 的 post-start 脚本确保加入成功。注意 node1 不要设 bootstrap=ON，否则重启后会分裂出新集群。

---

## 第三步：MGR 常见问题处理

### 3.1 成员驱逐与脑裂检测

```sql
-- 查看当前视图 ID，判断是否发生了脑裂（view_id 不一致则有问题）
SELECT VARIABLE_NAME, VARIABLE_VALUE
FROM performance_schema.global_status
WHERE VARIABLE_NAME IN (
  'group_replication_primary_member',
  'Gr_majority_transactions_already_certified'
);

-- 查看每个成员的状态（UNREACHABLE 表示被怀疑宕机）
SELECT * FROM performance_schema.replication_group_members;
SELECT * FROM performance_schema.replication_group_member_stats\G
```

**驱逐超时配置**（避免成员长时间处于 UNREACHABLE 状态影响写入）：

```sql
-- 5 秒内无响应则驱逐
SET GLOBAL group_replication_member_expel_timeout = 5;

-- 超过多少秒无法和多数成员通信则主动退出
SET GLOBAL group_replication_unreachable_majority_timeout = 30;
```

### 3.2 GTID 不一致处理

这是 MGR 中最常见的故障，通常在强制重启节点后出现 `ERROR 3134`：

```sql
-- 在出问题的节点上查看 GTID 状态
SHOW GLOBAL VARIABLES LIKE 'gtid_executed';
SHOW GLOBAL VARIABLES LIKE 'gtid_purged';

-- 方案一：重置 GTID 并重新克隆（推荐）
-- 先停 MGR
STOP GROUP_REPLICATION;

-- 重置 GTID（危险操作，确认节点是从节点）
RESET MASTER;

-- 重新加入
START GROUP_REPLICATION;
```

**推荐方案：开启 MySQL Clone Plugin（MySQL 8.0.17+）**，让新节点/故障节点自动从 Primary 完整克隆：

```sql
-- 在所有节点安装 Clone 插件
INSTALL PLUGIN clone SONAME 'mysql_clone.so';

-- 在 my.cnf 中加入
plugin_load_add = clone.so
group_replication_clone_threshold = 1    -- relay log 超过 1 个事务差距就触发 Clone

-- Clone 完成后节点会自动重启并加入集群
```

### 3.3 大事务导致集群性能下降

```sql
-- 监控认证延迟
SELECT MEMBER_ID, COUNT_TRANSACTIONS_IN_QUEUE,
       COUNT_TRANSACTIONS_CHECKED, COUNT_CONFLICTS_DETECTED
FROM performance_schema.replication_group_member_stats;

-- 慢事务排查
SELECT * FROM information_schema.innodb_trx
ORDER BY trx_started
LIMIT 10;
```

MGR 每个事务提交前需要在所有节点做 **冲突认证**（Certify），大事务的认证数据（writeset）会占用内存并阻塞其他事务。务必拆分批量写入操作，单事务行数控制在 1000 以内。

---

## 第四步：ProxySQL 配置

### 安装

```bash
# 添加 ProxySQL 源
wget -O- 'https://repo.proxysql.com/ProxySQL/proxysql-2.6.x/repo_pub_key' | apt-key add -
echo "deb https://repo.proxysql.com/ProxySQL/proxysql-2.6.x/$(lsb_release -cs)/ ./" \
  | tee /etc/apt/sources.list.d/proxysql.list
apt-get update && apt-get install -y proxysql2

systemctl enable proxysql
systemctl start proxysql
```

### 核心配置

ProxySQL 通过 MySQL 协议的管理端口（6032）配置，所有配置写入 SQLite：

```bash
# 连接管理端
mysql -u admin -padmin -h 127.0.0.1 -P 6032
```

```sql
-- 配置 MySQL 后端服务器
-- hostgroup 10：写组（Primary）
-- hostgroup 20：读组（Secondary）
INSERT INTO mysql_servers(hostgroup_id, hostname, port, weight, comment) VALUES
  (10, '192.168.1.201', 3306, 1000, 'primary'),
  (20, '192.168.1.202', 3306, 1000, 'secondary-1'),
  (20, '192.168.1.203', 3306, 1000, 'secondary-2');

-- 配置监控用户（在后端 MySQL 上要先创建）
SET mysql-monitor_username='proxysql_monitor';
SET mysql-monitor_password='MonitorPass!';
SET mysql-monitor_replication_lag_interval=2000;     -- 2s 检查一次复制延迟
SET mysql-monitor_replication_lag_timeout=1000;
SET mysql-monitor_connect_interval=2000;
SET mysql-monitor_ping_interval=2000;

-- 配置 MGR 专用监控（ProxySQL 2.x 内置 MGR 感知）
-- 需要在 mysql_group_replication_hostgroups 表配置
DELETE FROM mysql_group_replication_hostgroups;
INSERT INTO mysql_group_replication_hostgroups(
  writer_hostgroup,
  backup_writer_hostgroup,
  reader_hostgroup,
  offline_hostgroup,
  active,
  max_writers,
  writer_is_also_reader,
  max_transactions_behind
) VALUES (10, 30, 20, 40, 1, 1, 0, 100);
-- writer_is_also_reader=0：Primary 不接受读流量（纯写分离）
-- max_transactions_behind=100：从节点事务落后超 100 则移出读组

-- 配置应用用户
INSERT INTO mysql_users(username, password, default_hostgroup, transaction_persistent) VALUES
  ('appuser', 'AppStr0ng!', 10, 1);
-- transaction_persistent=1：同一事务内所有查询都去同一后端

-- 配置读写分离路由规则
-- SELECT 开头的查询路由到读组
INSERT INTO mysql_query_rules(rule_id, active, match_pattern, destination_hostgroup, apply) VALUES
  (1, 1, '^SELECT.*FOR UPDATE$', 10, 1),   -- SELECT FOR UPDATE 走主库
  (2, 1, '^SELECT', 20, 1);                 -- 其他 SELECT 走从库

-- 保存并应用配置
LOAD MYSQL SERVERS TO RUNTIME;
SAVE MYSQL SERVERS TO DISK;
LOAD MYSQL USERS TO RUNTIME;
SAVE MYSQL USERS TO DISK;
LOAD MYSQL QUERY RULES TO RUNTIME;
SAVE MYSQL QUERY RULES TO DISK;
LOAD MYSQL VARIABLES TO RUNTIME;
SAVE MYSQL VARIABLES TO DISK;
```

**在 MySQL 后端创建监控用户：**

```sql
-- 在三个 MySQL 节点上执行（或在 Primary 执行，会自动复制）
CREATE USER 'proxysql_monitor'@'%' IDENTIFIED WITH mysql_native_password BY 'MonitorPass!';
GRANT USAGE, REPLICATION CLIENT ON *.* TO 'proxysql_monitor'@'%';
-- MGR 监控需要额外权限
GRANT SELECT ON performance_schema.* TO 'proxysql_monitor'@'%';
FLUSH PRIVILEGES;
```

### 验证读写分离

```bash
# 通过 ProxySQL 连接（端口 6033）
mysql -u appuser -pAppStr0ng! -h 127.0.0.1 -P 6033

# 检查写连接是否去了 Primary
SELECT @@hostname, @@server_id;

# 查看路由统计
mysql -u admin -padmin -h 127.0.0.1 -P 6032 \
  -e "SELECT hostgroup, srv_host, ConnUsed, ConnFree, Queries FROM stats.stats_mysql_connection_pool;"
```

### ProxySQL 连接池调优

```sql
-- 关键连接池参数
SET mysql-max_connections=10000;                  -- ProxySQL 接收的最大前端连接
SET mysql-free_connections_pct=10;                -- 每个后端保留 10% 空闲连接
SET mysql-connection_max_age_ms=1800000;          -- 后端连接最长复用 30min
SET mysql-max_transaction_time=14400000;          -- 事务超时 4 小时
SET mysql-threshold_query_length=524288;          -- 超过 512KB 的查询记录日志
SET mysql-eventslog_filename='/var/lib/proxysql/events.log';
SET mysql-eventslog_filesize=104857600;

-- 后端每个 hostgroup 最大连接数（在 mysql_servers 表设置）
UPDATE mysql_servers SET max_connections=200 WHERE hostgroup_id=10;
UPDATE mysql_servers SET max_connections=200 WHERE hostgroup_id=20;

LOAD MYSQL VARIABLES TO RUNTIME;
SAVE MYSQL VARIABLES TO DISK;
```

---

## 第五步：Orchestrator 拓扑管理

Orchestrator 是目前最完善的 MySQL 拓扑发现和故障转移工具，Web UI 直观，API 丰富，可以和 ProxySQL 深度集成。

### 安装

```bash
# 下载 Orchestrator
wget https://github.com/openark/orchestrator/releases/download/v3.2.6/orchestrator-3.2.6-linux-amd64.tar.gz
tar xzf orchestrator-3.2.6-linux-amd64.tar.gz -C /usr/local/
ln -s /usr/local/orchestrator/orchestrator /usr/local/bin/orchestrator

# Orchestrator 使用 SQLite 或 MySQL 存储元数据（生产用 MySQL）
mysql -u root -e "CREATE DATABASE orchestrator;"
mysql -u root -e "CREATE USER 'orc_server'@'127.0.0.1' IDENTIFIED BY 'OrcStr0ng!';"
mysql -u root -e "GRANT ALL ON orchestrator.* TO 'orc_server'@'127.0.0.1';"
```

### 配置文件 `/etc/orchestrator/orchestrator.conf.json`

```json
{
  "Debug": false,
  "ListenAddress": ":3000",

  "MySQLTopologyUser": "orchestrator",
  "MySQLTopologyPassword": "OrcTopologyPass!",
  "MySQLTopologyCredentialsConfigFile": "",

  "MySQLOrchestratorHost": "127.0.0.1",
  "MySQLOrchestratorPort": 3306,
  "MySQLOrchestratorDatabase": "orchestrator",
  "MySQLOrchestratorUser": "orc_server",
  "MySQLOrchestratorPassword": "OrcStr0ng!",

  "SlaveLagQuery": "SELECT TIMESTAMPDIFF(SECOND, ts, NOW()) AS lag FROM meta.heartbeat ORDER BY ts DESC LIMIT 1",
  "SlaveStartPostWaitMilliseconds": 1000,

  "DiscoverByShowSlaveHosts": false,
  "InstancePollSeconds": 5,
  "UnseenInstanceForgetHours": 240,

  "ReasonableReplicationLagSeconds": 10,
  "AuditLogFile": "/var/log/orchestrator/audit.log",

  "RecoverMasterClusterFilters": ["*"],
  "RecoverIntermediateMasterClusterFilters": ["*"],
  "RecoveryPeriodBlockSeconds": 300,

  "OnFailureDetectionProcesses": [
    "echo 'Master failure detected: {failureType} on {failedHost}:{failedPort}' >> /tmp/orc-events.log"
  ],

  "PostMasterFailoverProcesses": [
    "/usr/local/bin/orc-proxysql-sync.sh {successorHost} {successorPort}"
  ],

  "PostFailoverProcesses": [
    "echo 'Failover complete. New master: {successorHost}:{successorPort}' | \
      curl -s -X POST https://hooks.dingtalk.com/xxx -H 'Content-Type: application/json' \
      -d '{\"msgtype\":\"text\",\"text\":{\"content\":\"MySQL Failover: {failureClusterAlias} -> {successorHost}\"}}'"
  ],

  "HostnameResolveMethod": "none",
  "MySQLHostnameResolveMethod": "@@hostname",

  "DetachLostReplicasAfterMasterFailover": true,
  "MasterFailoverLostInstancesDowntimeMinutes": 0
}
```

**在 MySQL 上创建 Orchestrator 监控用户：**

```sql
CREATE USER 'orchestrator'@'%' IDENTIFIED WITH mysql_native_password BY 'OrcTopologyPass!';
GRANT SUPER, PROCESS, REPLICATION SLAVE, RELOAD ON *.* TO 'orchestrator'@'%';
GRANT SELECT ON mysql.slave_master_info TO 'orchestrator'@'%';
GRANT SELECT ON performance_schema.replication_group_members TO 'orchestrator'@'%';
GRANT SELECT ON performance_schema.replication_group_member_stats TO 'orchestrator'@'%';
FLUSH PRIVILEGES;
```

### Orchestrator + ProxySQL 联动脚本

当 Orchestrator 检测到主节点故障并完成 failover 后，自动调用脚本更新 ProxySQL 后端列表：

```bash
cat > /usr/local/bin/orc-proxysql-sync.sh << 'SCRIPT'
#!/bin/bash
# 参数：$1=新主IP, $2=新主Port
NEW_MASTER_HOST=$1
NEW_MASTER_PORT=$2
PROXYSQL_ADMIN="mysql -u admin -padmin -h 127.0.0.1 -P 6032"

echo "[$(date)] Failover detected. New master: ${NEW_MASTER_HOST}:${NEW_MASTER_PORT}"

# 获取当前配置的 Primary
OLD_PRIMARY=$(${PROXYSQL_ADMIN} -e \
  "SELECT hostname FROM mysql_servers WHERE hostgroup_id=10 LIMIT 1;" \
  --skip-column-names 2>/dev/null | tr -d ' ')

if [ -z "$OLD_PRIMARY" ]; then
  echo "Failed to get old primary from ProxySQL"
  exit 1
fi

echo "Old primary: ${OLD_PRIMARY}, New primary: ${NEW_MASTER_HOST}"

# 将旧主移到读组（不直接删除，等待其恢复）
${PROXYSQL_ADMIN} -e "
  UPDATE mysql_servers
  SET hostgroup_id=20, weight=100
  WHERE hostname='${OLD_PRIMARY}' AND hostgroup_id=10;

  UPDATE mysql_servers
  SET hostgroup_id=10, weight=1000
  WHERE hostname='${NEW_MASTER_HOST}' AND hostgroup_id!=10;

  -- 从读组移除新主（writer_is_also_reader=0 的情况）
  DELETE FROM mysql_servers
  WHERE hostname='${NEW_MASTER_HOST}' AND hostgroup_id=20;

  LOAD MYSQL SERVERS TO RUNTIME;
  SAVE MYSQL SERVERS TO DISK;
"

echo "[$(date)] ProxySQL updated. New write target: ${NEW_MASTER_HOST}:${NEW_MASTER_PORT}"
SCRIPT

chmod +x /usr/local/bin/orc-proxysql-sync.sh
```

### 注册 MGR 集群到 Orchestrator

```bash
# 启动 Orchestrator
orchestrator -config /etc/orchestrator/orchestrator.conf.json http &

# 注册集群入口（只需注册一个节点，Orchestrator 会自动发现其他成员）
orchestrator-client -c discover -i 192.168.1.201:3306

# 查看拓扑
orchestrator-client -c topology -i 192.168.1.201:3306

# 手动 failover（测试用）
orchestrator-client -c graceful-master-takeover-auto -i 192.168.1.201:3306

# 查看集群状态
orchestrator-client -c clusters
orchestrator-client -c which-master -i 192.168.1.202:3306
```

访问 `http://192.168.1.200:3000` 可以看到拓扑可视化界面，节点连线表示复制关系，故障节点会变红并显示延迟。

---

## 第六步：监控集成

### mysqld_exporter

```bash
wget https://github.com/prometheus/mysqld_exporter/releases/download/v0.15.1/mysqld_exporter-0.15.1.linux-amd64.tar.gz
tar xzf mysqld_exporter-0.15.1.linux-amd64.tar.gz
mv mysqld_exporter-0.15.1.linux-amd64/mysqld_exporter /usr/local/bin/

# 创建监控用户
mysql -e "
CREATE USER 'exporter'@'localhost' IDENTIFIED WITH mysql_native_password BY 'ExporterPass!';
GRANT PROCESS, REPLICATION CLIENT, SELECT ON *.* TO 'exporter'@'localhost';
GRANT SELECT ON performance_schema.* TO 'exporter'@'localhost';
FLUSH PRIVILEGES;
"

# 配置文件
cat > /etc/mysql/.mysqld_exporter.cnf << 'EOF'
[client]
user = exporter
password = ExporterPass!
host = 127.0.0.1
port = 3306
EOF

# systemd service
cat > /etc/systemd/system/mysqld_exporter.service << 'EOF'
[Unit]
Description=MySQL Exporter
After=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/mysqld_exporter \
  --config.my-cnf=/etc/mysql/.mysqld_exporter.cnf \
  --collect.info_schema.innodb_metrics \
  --collect.info_schema.innodb_tablespaces \
  --collect.info_schema.processlist \
  --collect.perf_schema.replication_group_members \
  --collect.perf_schema.replication_group_member_stats \
  --collect.perf_schema.replication_applier_status_by_worker \
  --collect.global_status \
  --collect.global_variables \
  --collect.slave_status \
  --web.listen-address=:9104
Restart=always

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable mysqld_exporter
systemctl start mysqld_exporter
```

### 关键 Prometheus 告警规则

```yaml
groups:
  - name: mysql-mgr
    rules:
      - alert: MySQLDown
        expr: mysql_up == 0
        for: 1m
        labels:
          severity: critical
        annotations:
          summary: "MySQL 实例 {{ $labels.instance }} 无法连接"

      - alert: MySQLMGRMemberNotOnline
        expr: mysql_perf_schema_replication_group_members_count{member_state!="ONLINE"} > 0
        for: 2m
        labels:
          severity: warning
        annotations:
          summary: "MGR 成员 {{ $labels.instance }} 状态非 ONLINE"

      - alert: MySQLReplicationLag
        expr: mysql_slave_status_seconds_behind_master > 30
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "副本 {{ $labels.instance }} 复制延迟 {{ $value }}s"

      - alert: MySQLTooManyConnections
        expr: mysql_global_status_threads_connected / mysql_global_variables_max_connections > 0.8
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "{{ $labels.instance }} 连接数超过最大值的 80%"

      - alert: MySQLSlowQueries
        expr: rate(mysql_global_status_slow_queries[5m]) > 5
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "{{ $labels.instance }} 慢查询速率异常：{{ $value }}/s"

      - alert: MySQLInnoDBBufferPoolHitRateLow
        expr: |
          rate(mysql_global_status_innodb_buffer_pool_reads[5m]) /
          rate(mysql_global_status_innodb_buffer_pool_read_requests[5m]) > 0.01
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "{{ $labels.instance }} InnoDB Buffer Pool 命中率低"
```

Grafana Dashboard 推荐使用官方 **MySQL Overview（ID: 7362）** 和 **MySQL Replication（ID: 7371）**，导入即用。

---

## 第七步：XtraBackup 备份策略

MGR 环境下**从任意 Secondary 节点备份**，不影响主节点写入性能。

```bash
# 安装 XtraBackup 8.0
wget https://downloads.percona.com/downloads/percona-xtrabackup-8.0/8.0.35-30/binary/debian/jammy/x86_64/percona-xtrabackup-80_8.0.35-30-1.jammy_amd64.deb
dpkg -i percona-xtrabackup-80_8.0.35-30-1.jammy_amd64.deb
```

**全量备份脚本 `/usr/local/bin/mysql-full-backup.sh`：**

```bash
#!/bin/bash
set -euo pipefail

BACKUP_DIR="/data/mysql/backup"
DATE=$(date +%Y%m%d_%H%M%S)
FULL_BACKUP_DIR="${BACKUP_DIR}/full_${DATE}"
LOG_FILE="/var/log/mysql/backup.log"
RETENTION_DAYS=7

echo "[$(date)] Starting full backup..." | tee -a ${LOG_FILE}

xtrabackup \
  --backup \
  --user=root \
  --password="RootPass!" \
  --host=127.0.0.1 \
  --target-dir=${FULL_BACKUP_DIR} \
  --compress \
  --compress-threads=4 \
  --parallel=4 \
  --throttle=400 \
  2>>${LOG_FILE}

# prepare 阶段（备份完成后立即做，否则备份不可用）
xtrabackup --prepare --target-dir=${FULL_BACKUP_DIR} 2>>${LOG_FILE}

echo "[$(date)] Full backup completed: ${FULL_BACKUP_DIR}" | tee -a ${LOG_FILE}
du -sh ${FULL_BACKUP_DIR} | tee -a ${LOG_FILE}

# 清理过期备份
find ${BACKUP_DIR} -maxdepth 1 -name "full_*" -mtime +${RETENTION_DAYS} -exec rm -rf {} \;
echo "[$(date)] Old backups cleaned (>${RETENTION_DAYS} days)" | tee -a ${LOG_FILE}

# 上传到 S3（可选）
# aws s3 sync ${FULL_BACKUP_DIR} s3://your-bucket/mysql-backup/$(hostname)/full_${DATE}/
```

**增量备份脚本（基于最近一次全量）：**

```bash
#!/bin/bash
set -euo pipefail

BACKUP_DIR="/data/mysql/backup"
DATE=$(date +%Y%m%d_%H%M%S)
LOG_FILE="/var/log/mysql/backup.log"

# 找到最新的全量备份
LAST_FULL=$(ls -td ${BACKUP_DIR}/full_* 2>/dev/null | head -1)
if [ -z "${LAST_FULL}" ]; then
  echo "No full backup found, run full backup first" | tee -a ${LOG_FILE}
  exit 1
fi

# 找到最新的增量（如果存在）或以全量为基准
LAST_INCR=$(ls -td ${BACKUP_DIR}/incr_* 2>/dev/null | head -1)
BASEDIR=${LAST_INCR:-${LAST_FULL}}

INCR_DIR="${BACKUP_DIR}/incr_${DATE}"

echo "[$(date)] Starting incremental backup based on ${BASEDIR}" | tee -a ${LOG_FILE}

xtrabackup \
  --backup \
  --user=root \
  --password="RootPass!" \
  --host=127.0.0.1 \
  --target-dir=${INCR_DIR} \
  --incremental-basedir=${BASEDIR} \
  --compress \
  --compress-threads=4 \
  2>>${LOG_FILE}

echo "[$(date)] Incremental backup completed: ${INCR_DIR}" | tee -a ${LOG_FILE}
```

**cron 配置：**

```cron
# 每天凌晨 2 点全量备份（在 node2 上执行）
0 2 * * * /usr/local/bin/mysql-full-backup.sh

# 每 4 小时增量备份
0 6,10,14,18,22 * * * /usr/local/bin/mysql-incremental-backup.sh
```

**从备份恢复：**

```bash
# 解压压缩的备份
xtrabackup --decompress --target-dir=/data/mysql/backup/full_20260412_020000

# 恢复到数据目录（前提：MySQL 已停止，datadir 已清空）
systemctl stop mysql
rm -rf /var/lib/mysql/*

xtrabackup --copy-back \
  --target-dir=/data/mysql/backup/full_20260412_020000 \
  --datadir=/var/lib/mysql

chown -R mysql:mysql /var/lib/mysql
systemctl start mysql
```

---

## 常见问题速查

**Q：MGR 写性能为什么比单主差这么多？**

MGR 提交事务前需要所有节点完成认证（Paxos 多数确认），网络 RTT 直接叠加在写延迟上。同机房 RTT < 1ms 影响有限，跨 AZ/跨城部署时影响显著。建议：同机房部署、事务尽量小、批量写入改为 bulk insert。

**Q：ProxySQL 主节点故障期间写请求会报错吗？**

会。ProxySQL 的健康检查间隔默认 2s，加上 MGR 自动选主耗时（通常 10-30s），这期间写请求会返回连接错误。应用层需要实现重试逻辑，建议配合 Orchestrator 钩子脚本尽快更新 ProxySQL 路由。

**Q：`group_replication_start_on_boot` 设为 ON 后重启节点总是形成脑裂？**

因为多个节点同时带 `bootstrap_group=ON` 启动或者带 `start_on_boot=ON` 启动时，可能各自形成独立集群。正确做法：只有在**初始化第一个节点**时临时设 `bootstrap=ON`，之后所有节点都用 `start_on_boot=ON` 正常加入。如果担心网络分区后的脑裂，设置 `group_replication_unreachable_majority_timeout` 让少数节点主动退出。

**Q：XtraBackup 备份期间 MGR 成员有什么影响？**

XtraBackup 在备份期间会对 InnoDB 加全局锁（redo log 阶段短暂），但不影响 MGR 复制流。在 Secondary 上备份不影响 Primary 的写入，Secondary 本身会有短暂的 IO 压力，监控显示复制延迟可能短暂增加，通常可接受。
