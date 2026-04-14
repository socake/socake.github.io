---
title: "数据库变更管理：从 gh-ost 到 Flyway 的完整工程化路径"
date: 2025-01-08T10:00:00+08:00
draft: false
tags: ["数据库", "DB 变更", "gh-ost", "pt-osc", "Liquibase", "Flyway"]
categories: ["数据库"]
description: "数据库变更是所有工程团队的痛点：ALTER 一张大表要锁几小时、线上和预发 schema 对不齐、回滚只能靠备份。这篇笔记从 Online DDL 工具（gh-ost vs pt-osc vs Spirit）讲到版本管理（Flyway vs Liquibase），再到变更流程与回滚策略，给出一套从工具到流程的完整方案。"
summary: '很多团队把"数据库变更管理"当成几条 SQL + 一个工单，实际上这是工程化程度最低的一块地方。一边是开发随手写 ALTER 把线上锁住，一边是 DBA 手动盯着进度条祈祷不出事。这篇文章把我总结的 DB 变更管理最佳实践分成工具、流程、组织三个层面讲，每一层都有可以直接落地的方案。'
toc: true
math: false
diagram: false
keywords: ["gh-ost", "pt-online-schema-change", "Spirit", "Liquibase", "Flyway", "DB 变更", "Schema Migration"]
params:
  reading_time: true
---

## 为什么要写这篇

我见过太多团队在数据库变更上踩坑：

- 开发直接 `ALTER TABLE t ADD COLUMN`，10 亿行的表锁了 3 小时
- DBA 周末加班盯着 pt-osc 跑完大表迁移
- 预发和线上 schema 不一致，上线后才发现少了一个字段
- 回滚时发现连"当前 schema 是什么"都说不清楚
- 一个涉及数据回填的变更，没人知道是不是跑完了

这些问题的共同点是**缺少工程化**。数据库变更管理不应该是 DBA 一个人的问题，它应该像代码一样进 git、过 code review、走 CI/CD，有明确的流程和回滚方案。

这篇文章分三个层次：

1. **工具层**：Online DDL 工具对比（gh-ost / pt-osc / Spirit）
2. **框架层**：schema 版本管理（Flyway / Liquibase）
3. **流程层**：从开发提交到生产执行的完整流程

## 一、Online DDL 的必要性

MySQL 原生 `ALTER TABLE` 在 8.0 之后支持 Instant 和 In-Place 两种算法，但仍然有不少场景做不到真正的 Online：

| 变更类型                  | 8.0 原生支持      | 备注                          |
|---------------------------|-------------------|-------------------------------|
| 加列 (ADD COLUMN)         | INSTANT（末尾）   | 非末尾会走 INPLACE/COPY       |
| 删列 (DROP COLUMN)        | COPY              | 锁表                          |
| 修改类型 (MODIFY)         | 视情况            | 多数要 COPY                   |
| 加索引                    | INPLACE           | 不锁表但写阻塞                |
| 删索引                    | INPLACE           | 快                            |
| 改字符集                  | COPY              | 锁表                          |
| 加/改/删 FK              | COPY              | 锁表                          |

**INSTANT** 是在末尾加列，修改元数据即可，毫秒级。
**INPLACE** 不重建表，但仍然会对 metadata lock 敏感，长事务会阻塞。
**COPY** 是最传统的方式，重建表，期间写入阻塞。

对于 10 亿行以上的大表，COPY 算法跑几小时起步。这期间：

1. 写入阻塞，业务不可用
2. binlog 暴涨，从库延迟飙升
3. 磁盘空间翻倍
4. 一旦失败就要回滚，又一次代价

这就是为什么需要 Online DDL 工具：在不阻塞业务的情况下完成变更。

## 二、三个主流 Online DDL 工具

### 2.1 pt-online-schema-change (pt-osc)

Percona 家的经典工具，诞生最早，原理简单：

1. 创建一个影子表 `_t_new`，结构是变更后的 schema
2. 给原表加三个触发器（INSERT/UPDATE/DELETE），把变更同步到影子表
3. 批量复制原表数据到影子表（chunk by chunk）
4. 复制完成后 `RENAME TABLE t TO _t_old, _t_new TO t`，原子切换
5. 删除 `_t_old`

优点：

- 成熟稳定，版本兼容性最好（MySQL 5.6/5.7/8.0、MariaDB、Galera/PXC 都行）
- **支持外键**（gh-ost 不支持）
- 社区支持好，踩坑信息多

缺点：

- **三个触发器**：每次写入都额外写影子表，主库压力 +30-50%
- **触发器和用户 UPDATE 在同一个事务**，死锁概率变大
- 复制速度受限于主库 IO

用法：

```bash
pt-online-schema-change \
  --alter "ADD COLUMN new_col VARCHAR(255) NOT NULL DEFAULT ''" \
  --host=master-1 --user=admin --password=xxx \
  --database=mydb \
  --chunk-size=1000 \
  --chunk-time=0.5 \
  --max-load="Threads_running=50" \
  --critical-load="Threads_running=200" \
  --max-lag=5 \
  --check-slave-lag=replica-1 \
  --execute \
  D=mydb,t=orders
```

几个关键参数：

- `--chunk-size`：每批处理行数，默认 1000，大表调到 2000-5000
- `--chunk-time`：每批目标耗时，动态调整 chunk-size
- `--max-load`：负载阈值，超过就 sleep
- `--check-slave-lag`：从库延迟监控
- `--execute`：实际执行（先跑 `--dry-run` 验证）

### 2.2 gh-ost

GitHub 开源，和 pt-osc 完全不同的路子：**无触发器，基于 binlog**。

1. 创建影子表 `_t_gho`
2. 订阅一个从库的 binlog
3. 对原表做全表扫描复制到影子表
4. 同时 binlog 里的变更 apply 到影子表
5. 复制完成后做原子 cut-over 切换

优点：

- **无触发器**：主库负载几乎无感
- **可暂停、可恢复**：写一个文件到 throttle-flag-file 就暂停
- **细粒度限流**：支持超过 20 种 throttle 条件
- **cut-over 可控**：可以交互式控制切换时机
- 适合大规模生产

缺点：

- **不支持外键**
- **不支持 Galera/PXC**（因为 cut-over 的锁语义）
- **依赖 RBR binlog**（现代 MySQL 默认就是）
- 需要一个可用的 replica 来订阅 binlog

用法：

```bash
gh-ost \
  --host=master-1 \
  --port=3306 \
  --user=ghost \
  --password=xxx \
  --database=mydb \
  --table=orders \
  --alter="ADD COLUMN new_col VARCHAR(255) NOT NULL DEFAULT ''" \
  --chunk-size=2000 \
  --max-load="Threads_running=50" \
  --critical-load="Threads_running=200" \
  --throttle-control-replicas="replica-1,replica-2" \
  --throttle-flag-file=/tmp/gh-ost.flag \
  --postpone-cut-over-flag-file=/tmp/gh-ost.postpone \
  --initially-drop-ghost-table \
  --execute
```

两个关键 flag：

- `--throttle-flag-file`：touch 这个文件暂停复制，删除恢复
- `--postpone-cut-over-flag-file`：即使复制完成也不自动切换，等人手动删除这个文件才切

这两个 flag 是 gh-ost 的杀手锏：**你可以白天跑 gh-ost，快到 cut-over 前停一下，晚上低峰期再切换**。业务影响最小化。

### 2.3 Spirit：新一代的挑战者

2024 年出现的新工具，由前 MySQL performance engineer Morgan Tocker 主导开发。理念是解决 gh-ost 和 pt-osc 的痛点：

- 比 gh-ost 快（并行复制）
- 比 pt-osc 轻（无触发器）
- 支持 MySQL 8.0 的新特性（INSTANT DDL fallback）
- Go 写的，部署简单

目前还比较新，生产验证不够多。我在 staging 测试过几次，速度比 gh-ost 快 30-50%。但在关键系统上我还没敢用。

### 2.4 三者对比

| 维度                  | pt-osc         | gh-ost         | Spirit         |
|-----------------------|----------------|----------------|----------------|
| 原理                  | 触发器         | binlog         | binlog + 并行  |
| 主库负载              | 中-高          | 低             | 低             |
| 速度                  | 中             | 中             | 快             |
| 可暂停                | 否             | 是             | 是             |
| 外键                  | 支持           | 不支持         | 不支持         |
| Galera/PXC            | 支持           | 不支持         | 不支持         |
| 成熟度                | 最成熟         | 成熟           | 新             |
| 监控                  | 日志           | 丰富 API       | 丰富           |
| 推荐场景              | PXC、有外键    | 现代 8.0 生产  | 评估中         |

我自己的使用习惯：

- **默认用 gh-ost**：灵活、可控、对主库友好
- **遇到外键或 PXC 用 pt-osc**
- **Spirit 持续观察，等 2026 年稳定后考虑切换**

## 三、Schema 版本管理

有了 Online DDL 工具，还缺一个"版本管理"层。不然：

- 不知道某个环境当前 schema 版本
- 不知道哪些变更已经执行过
- 多人协作时变更顺序混乱
- 回滚没有明确版本

这是 Flyway 和 Liquibase 解决的问题。它们的思路都是一样的：**把 DB 变更变成"迁移脚本"，用 git 管理，按版本号顺序执行**。

### 3.1 Flyway

Flyway 的哲学是"SQL-first"，所有变更都是原生 SQL 文件：

```
db/migration/
├── V1__init_schema.sql
├── V2__add_users_email.sql
├── V3__create_orders_table.sql
└── V4__add_orders_index.sql
```

文件命名规则：`V<version>__<description>.sql`。Flyway 在数据库里维护一张 `flyway_schema_history` 表，记录已执行的版本。

执行：

```bash
flyway -url=jdbc:mysql://master-1:3306/mydb \
       -user=admin \
       -password=xxx \
       -locations=filesystem:db/migration \
       migrate
```

Flyway 的优势是简单直接，缺点是**不支持回滚**（至少免费版不支持）。官方哲学是"向前修复"（roll forward），遇到问题就写一个新的 V5 来修。

### 3.2 Liquibase

Liquibase 更"重"，用 XML/YAML/JSON 描述变更（也支持 SQL），支持回滚：

```yaml
databaseChangeLog:
  - changeSet:
      id: 1
      author: wzh
      changes:
        - addColumn:
            tableName: users
            columns:
              - column:
                  name: email_verified
                  type: BOOLEAN
                  defaultValueBoolean: false
      rollback:
        - dropColumn:
            tableName: users
            columnName: email_verified
```

Liquibase 的优势：

- 支持回滚脚本，虽然对 DROP 列这种不可逆操作也无解
- 抽象层面跨数据库（同一套 yaml 能生成 MySQL 和 PG 的 SQL）
- 支持 preconditions、contexts 等高级功能

缺点是**抽象层太厚**，有时候想写一个特殊 SQL 要绕半天。

### 3.3 我的选择

我推荐 **Flyway + SQL**，理由：

1. 简单直接，学习成本低
2. SQL 最准确，不会被抽象层误导
3. 回滚本来就该走"向前修复"，不是脚本化 rollback
4. 工具轻量，集成 Spring Boot / Maven / Gradle 都方便

Liquibase 唯一的优势是跨数据库，但大部分团队只用一种 DB，这个优势用不上。

### 3.4 和 Online DDL 集成

Flyway 默认用原生 `ALTER`，大表会锁。解决方案是**外置 OSC 执行器**：

```sql
-- V5__add_orders_index.sql
-- GHOST: ALTER TABLE orders ADD INDEX idx_created_at (created_at);
```

然后写一个包装脚本，识别注释里的 GHOST 标记，用 gh-ost 执行：

```bash
#!/bin/bash
for sql in $(flyway info | grep Pending); do
  if grep -q "^-- GHOST:" "$sql"; then
    ALTER=$(grep "^-- GHOST:" "$sql" | sed 's/^-- GHOST: //')
    TABLE=$(echo "$ALTER" | grep -oP 'ALTER TABLE \K\w+')
    gh-ost --alter "$ALTER" --table "$TABLE" ...
  else
    flyway migrate -target=$sql
  fi
done
```

类似方案在 GitHub、Slack 等公司都有，大家的命名不同但思路一致。

更成熟的方案是 **Bytebase** 或 **Skeema**，它们把 Flyway 的版本管理和 Online DDL 执行集成在一起，有 UI、审批流、权限管理。适合团队规模大的情况。

## 四、完整变更流程

说了工具，接下来是流程。一个健康的 DB 变更流程至少包含：

### 4.1 开发阶段

1. **开发写 migration SQL**：放在 repo 的 `db/migration/` 目录
2. **本地跑 Flyway 验证**：确保 SQL 在本地 MySQL 能执行成功
3. **单元测试跑在 migrated schema 上**：CI 里 Flyway 跑一遍再跑测试

### 4.2 提交与 Review

1. **PR 要求 DBA review**：涉及大表、锁表操作的变更，DBA 是 required reviewer
2. **自动化 lint**：用 `sqlcheck` 或自研脚本检查常见问题（无 default 加列、无索引的删除等）
3. **影响评估**：PR 模板要求填写影响的表、估计的数据量、执行时间、回滚方案

### 4.3 预发环境

1. **自动执行**：PR merge 后 CI 自动在 staging 跑 Flyway
2. **验证**：staging 数据量通常小，验证 SQL 正确性和业务回归
3. **性能测试**：有条件的话跑一下性能回归测试

### 4.4 生产执行

1. **审批**：在 Bytebase/自研工单系统走审批流
2. **变更窗口**：大表变更限制在低峰期，小变更随时
3. **执行方式**：
   - 小变更（<100 万行、<10MB）：Flyway 直接执行
   - 大变更：gh-ost 手动执行
4. **监控**：执行期间看 slow log、主库 CPU、复制延迟
5. **验证**：变更完成后跑一套 smoke test 确认业务正常

### 4.5 回滚策略

DB 变更的回滚比代码回滚难得多。几个原则：

1. **优先向前修复**：90% 的情况下，下一个 migration 修复比 rollback 简单
2. **破坏性操作要分两步**：
   - 第一步：加列/加索引/加表
   - 第二步：下线代码中对旧字段的引用
   - 第三步（几天后）：删列/删索引
   - 每一步都可回滚
3. **数据修改要备份**：UPDATE/DELETE 之前把影响的行备份到临时表
4. **不可逆操作要审批到最高级**：DROP TABLE/COLUMN 必须 CTO 或架构师签字

## 五、常见变更类型的正确姿势

### 5.1 加列

```sql
-- 不好：NOT NULL 无默认，插入老行失败
ALTER TABLE users ADD COLUMN phone VARCHAR(20) NOT NULL;

-- 好：NOT NULL 有默认
ALTER TABLE users ADD COLUMN phone VARCHAR(20) NOT NULL DEFAULT '';

-- 最好：允许 NULL，后面慢慢回填
ALTER TABLE users ADD COLUMN phone VARCHAR(20);
```

MySQL 8.0 的 INSTANT 加列只对末尾有效，所以加到末尾最快。

### 5.2 修改列类型

```sql
-- 扩展不会重建表（INPLACE）
ALTER TABLE users MODIFY name VARCHAR(200);  -- 从 VARCHAR(100) 扩到 200

-- 缩小会重建表（COPY）
ALTER TABLE users MODIFY name VARCHAR(50);  -- 必须走 gh-ost
```

### 5.3 加索引

```sql
-- 小表直接加
ALTER TABLE small_table ADD INDEX idx_name (name);

-- 大表用 gh-ost
gh-ost --alter "ADD INDEX idx_created_at (created_at)" ...
```

### 5.4 加字段 + 回填数据

典型场景：用户表加一个冗余字段 `country`，从 `address` 解析出来。

```
步骤 1: gh-ost ADD COLUMN country VARCHAR(10)
步骤 2: 后台任务分批回填
        UPDATE users SET country = extract_country(address)
        WHERE id BETWEEN ? AND ? AND country IS NULL
步骤 3: 应用侧读新字段
步骤 4: 应用侧写新字段
步骤 5: 加 NOT NULL 约束（可选）
```

每一步都能独立回滚，业务无感。

## 六、监控与告警

变更执行期间必须密切监控：

```yaml
- alert: DBSchemaChangeSlowQuery
  expr: rate(mysql_global_status_slow_queries[1m]) > 10
  for: 2m
  annotations:
    summary: "变更期间慢查询暴涨"

- alert: DBSchemaChangeReplicationLag
  expr: mysql_slave_lag_seconds > 30
  for: 1m

- alert: DBSchemaChangeThreadsRunning
  expr: mysql_global_status_threads_running > 100
  for: 1m
```

gh-ost 自己也有状态文件和 socket，可以 `echo status | nc -U /tmp/gh-ost.sock` 查当前进度。

## 七、真实故障复盘

### 7.1 pt-osc 死锁

**现象**：某次大表加列，pt-osc 跑到 30% 时持续报 `Deadlock found`，最终 abort。

**根因**：pt-osc 的触发器和业务 UPDATE 争抢同一批行的锁，业务 tps 高时死锁概率骤增。

**修复**：改用 gh-ost，无触发器，问题消失。

**教训**：高并发写入的表不要用 pt-osc，gh-ost 是更好选择。

### 7.2 gh-ost 一次性 cut-over 导致业务阻塞

**现象**：gh-ost 跑完 cut-over 的几秒钟，业务 QPS 从 5000 掉到 0，持续 8 秒。

**根因**：cut-over 需要对原表和影子表都获取 metadata lock，期间有个长事务没结束，卡住了整个 cut-over 队列。

**修复**：

1. 执行 cut-over 前检查并 kill 长事务
2. `--cut-over-lock-timeout-seconds=3`，超时自动重试
3. 设 `--max-lag-millis=1500` 确保复制不延迟

**教训**：cut-over 不是"秒级"的，要预留 5-10 秒的业务影响窗口。

### 7.3 Flyway 在生产误执行 V9，应该先执行 V8

**现象**：一次发布时，CI 把 V9 的 SQL 误执行到生产，而 V8 还没 merge。结果 V9 依赖 V8 的表，报错回滚。

**根因**：团队用 feature branch 开发，多个 PR 并行，version 冲突没人检查。

**修复**：

1. Flyway 改用时间戳版本号 `V20250108120000__xxx.sql`，减少冲突
2. CI 增加"版本号连续性"检查
3. 长期迁移到 Bytebase，有版本依赖管理

## 八、工具推荐清单

```
Online DDL:     gh-ost（首选）, pt-osc（PXC/外键场景）, Spirit（关注）
版本管理:       Flyway（推荐）, Liquibase（跨 DB 场景）
集成平台:       Bytebase（开源/商业）, PlanetScale（SaaS）
Schema diff:    Skeema, mysqldiff
SQL Lint:       sqlcheck, squawk（PG）
审计:           Percona Audit Log Plugin, MaxScale
```

## 九、经验法则

- **DB 变更要像代码变更一样 git 管理**
- **用 Online DDL 工具，不要原生 ALTER 大表**
- **gh-ost 是现代生产首选**
- **Flyway 简单直接，不要被 Liquibase 的功能丰富迷惑**
- **破坏性变更分两步做**
- **大变更必须审批 + 变更窗口**
- **监控变更执行期间的主库负载**
- **预发环境必须真实执行一遍 migration**
- **版本号用时间戳，避免多人冲突**
- **回滚优先向前修复**

数据库变更管理是个容易被低估的领域。做好了能让开发和 DBA 都解放双手，做不好就是 3 点半被叫起来处理事故的根源。希望这篇笔记能帮你的团队把 DB 变更工程化起来。

参考资料：

- Percona Toolkit 官方文档的 pt-online-schema-change
- gh-ost GitHub repo 的 doc/ 目录
- Flyway 和 Liquibase 的官方文档
- Bytebase 博客 "gh-ost vs pt-online-schema-change"
- Morgan Tocker 的 hackmysql.com 博客，Spirit 项目的设计思路
