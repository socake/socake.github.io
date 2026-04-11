---
title: "Elasticsearch 备份与恢复：快照管理与跨集群迁移实践"
date: 2026-04-11T10:00:00+08:00
draft: false
tags: ["Elasticsearch", "ELK", "备份", "运维", "S3"]
categories: ["ELK Stack"]
description: "Snapshot API 配置、S3 IRSA 认证、定时快照脚本，以及跨集群迁移三种方案的对比与实战踩坑。"
summary: "Snapshot API 配置、S3 IRSA 认证、定时快照脚本，以及跨集群迁移三种方案的对比与实战踩坑。"
toc: true
math: false
diagram: false
keywords: ["Elasticsearch", "快照备份", "S3", "跨集群迁移", "reindex"]
params:
  reading_time: true
---

ES 集群的备份是很多人最容易忽视的部分，直到某天数据丢了才开始重视。我们曾经经历过一次数据节点 EBS 卷故障，幸好快照策略提前配好了，恢复只花了两个小时。这篇把快照配置、定时备份脚本、数据恢复流程，以及跨集群迁移的几种方案都整理出来。

## Snapshot 基础概念

ES 的 Snapshot 是增量备份——第一次快照是全量的，之后每次只备份自上次以来变化的数据（新增的 segment 文件）。这意味着快照速度很快，但恢复时需要按顺序依赖之前的快照。

快照存储在 Repository（仓库）里，支持多种后端：
- S3（AWS S3 或兼容 S3 协议的存储）
- GCS（Google Cloud Storage）
- Azure Blob Storage
- HDFS
- 共享文件系统（NFS）

生产环境强烈推荐 S3——可靠、便宜、与 K8s 上的 ES 集群（ECK）集成简单。下面以 AWS S3 为例。

## S3 Repository 配置（IRSA 认证）

传统方式是把 AWS Access Key/Secret Key 直接配置到 ES Keystore 里，安全性差，密钥轮换麻烦。在 EKS 上运行的 ECK 集群，推荐使用 IRSA（IAM Roles for Service Accounts）——给 ES 的 Service Account 绑定 IAM Role，不需要任何静态密钥。

### 第一步：创建 S3 Bucket

```bash
aws s3 mb s3://es-backup-prod-logging --region us-west-2

# 配置 bucket 策略：只允许特定角色访问
aws s3api put-bucket-policy --bucket es-backup-prod-logging --policy '{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "AWS": "arn:aws:iam::123456789012:role/es-logging-snapshot-role"
      },
      "Action": [
        "s3:ListBucket",
        "s3:GetBucketLocation"
      ],
      "Resource": "arn:aws:s3:::es-backup-prod-logging"
    },
    {
      "Effect": "Allow",
      "Principal": {
        "AWS": "arn:aws:iam::123456789012:role/es-logging-snapshot-role"
      },
      "Action": [
        "s3:PutObject",
        "s3:GetObject",
        "s3:DeleteObject"
      ],
      "Resource": "arn:aws:s3:::es-backup-prod-logging/*"
    }
  ]
}'
```

### 第二步：创建 IAM Role

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "s3:ListBucket",
        "s3:GetBucketLocation",
        "s3:ListBucketMultipartUploads",
        "s3:ListBucketVersions"
      ],
      "Resource": "arn:aws:s3:::es-backup-prod-logging"
    },
    {
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:AbortMultipartUpload",
        "s3:ListMultipartUploadParts"
      ],
      "Resource": "arn:aws:s3:::es-backup-prod-logging/*"
    }
  ]
}
```

Trust Policy 里允许 EKS OIDC Provider 代入这个角色：

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::123456789012:oidc-provider/oidc.eks.us-west-2.amazonaws.com/id/EXAMPLED539D4633E53DE1B71EXAMPLE"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "oidc.eks.us-west-2.amazonaws.com/id/EXAMPLED539D4633E53DE1B71EXAMPLE:sub": "system:serviceaccount:logging:es-logging-es",
          "oidc.eks.us-west-2.amazonaws.com/id/EXAMPLED539D4633E53DE1B71EXAMPLE:aud": "sts.amazonaws.com"
        }
      }
    }
  ]
}
```

### 第三步：给 ECK 的 ServiceAccount 打 annotation

ECK 会自动为每个 ES 集群创建 ServiceAccount，名称格式是 `<cluster-name>-es`。给它打上 IAM Role 的 annotation：

```bash
kubectl annotate serviceaccount es-logging-es \
  -n logging \
  eks.amazonaws.com/role-arn=arn:aws:iam::123456789012:role/es-logging-snapshot-role
```

然后重启 ES Pod 使 annotation 生效（ECK 会触发滚动重启）。

### 第四步：安装 S3 插件并注册 Repository

ECK 支持在 CRD 里直接配置插件安装：

```yaml
spec:
  nodeSets:
    - name: data-hot
      # ...
      podTemplate:
        spec:
          initContainers:
            - name: install-plugins
              command:
                - sh
                - -c
                - |
                  bin/elasticsearch-plugin install --batch repository-s3
```

插件安装后，通过 ES API 注册 S3 Repository：

```bash
PUT _snapshot/s3-backup
{
  "type": "s3",
  "settings": {
    "bucket": "es-backup-prod-logging",
    "region": "us-west-2",
    "base_path": "snapshots/prod"
  }
}
```

验证 Repository 是否正常工作：

```bash
POST _snapshot/s3-backup/_verify
```

返回 `{"nodes": {...}}` 表示各节点都能访问 S3，没有 error。

## 定时快照脚本

手动管理快照很容易出错，用 ES 内置的 SLM（Snapshot Lifecycle Management）自动化是更好的选择。

### SLM 策略配置

```json
PUT _slm/policy/daily-snapshots
{
  "name": "<logs-{now/d}>",
  "schedule": "0 30 2 * * ?",  // 每天凌晨 2:30
  "repository": "s3-backup",
  "config": {
    "indices": ["logs-*", ".kibana*"],
    "ignore_unavailable": true,
    "include_global_state": false
  },
  "retention": {
    "expire_after": "30d",
    "min_count": 5,    // 至少保留 5 个快照
    "max_count": 50    // 最多保留 50 个快照
  }
}
```

`ignore_unavailable: true` 很重要——如果某个索引正在 rollover 或者临时不可用，不要让整个快照失败。

`include_global_state: false`：全局状态包含集群设置、模板等，备份之后恢复到另一个集群可能产生冲突，日常备份通常只备索引数据即可。

验证和手动触发：

```bash
# 查看 SLM 策略
GET _slm/policy/daily-snapshots

# 手动触发一次
POST _slm/policy/daily-snapshots/_execute

# 查看快照列表
GET _snapshot/s3-backup/*?verbose=false

# 查看最近快照状态
GET _slm/policy/daily-snapshots
```

### Python 脚本方式（老集群没有 SLM 时）

如果 ES 版本较老（7.4 以下没有 SLM），可以用 Python 脚本 + cron 实现定时快照：

```python
#!/usr/bin/env python3
"""ES 快照管理脚本"""

import requests
import json
from datetime import datetime, timedelta
import sys

ES_HOST = "https://es-logging:9200"
ES_AUTH = ("elastic", "your-password")
SNAPSHOT_REPO = "s3-backup"
RETENTION_DAYS = 30


def create_snapshot():
    """创建新快照"""
    today = datetime.now().strftime("%Y.%m.%d")
    snapshot_name = f"logs-{today}"

    url = f"{ES_HOST}/_snapshot/{SNAPSHOT_REPO}/{snapshot_name}"
    payload = {
        "indices": "logs-*",
        "ignore_unavailable": True,
        "include_global_state": False
    }

    response = requests.put(
        url,
        json=payload,
        auth=ES_AUTH,
        verify="/etc/ssl/certs/es-ca.crt",
        timeout=60
    )

    if response.status_code == 200:
        print(f"Snapshot {snapshot_name} started successfully")
        return snapshot_name
    else:
        print(f"Failed to create snapshot: {response.text}", file=sys.stderr)
        sys.exit(1)


def wait_for_snapshot(snapshot_name: str, max_wait_seconds: int = 3600):
    """等待快照完成"""
    import time
    url = f"{ES_HOST}/_snapshot/{SNAPSHOT_REPO}/{snapshot_name}"

    for _ in range(max_wait_seconds // 10):
        response = requests.get(url, auth=ES_AUTH, verify="/etc/ssl/certs/es-ca.crt")
        state = response.json()["snapshots"][0]["state"]

        if state == "SUCCESS":
            print(f"Snapshot {snapshot_name} completed successfully")
            return True
        elif state in ("FAILED", "PARTIAL"):
            print(f"Snapshot {snapshot_name} failed with state: {state}", file=sys.stderr)
            return False

        time.sleep(10)

    print(f"Snapshot {snapshot_name} timed out", file=sys.stderr)
    return False


def delete_old_snapshots():
    """删除过期快照"""
    url = f"{ES_HOST}/_snapshot/{SNAPSHOT_REPO}/*"
    response = requests.get(url, auth=ES_AUTH, verify="/etc/ssl/certs/es-ca.crt")
    snapshots = response.json()["snapshots"]

    cutoff_date = datetime.now() - timedelta(days=RETENTION_DAYS)

    for snapshot in snapshots:
        # 快照名称格式：logs-YYYY.MM.DD
        try:
            snap_date = datetime.strptime(snapshot["snapshot"], "logs-%Y.%m.%d")
            if snap_date < cutoff_date:
                delete_url = f"{ES_HOST}/_snapshot/{SNAPSHOT_REPO}/{snapshot['snapshot']}"
                requests.delete(delete_url, auth=ES_AUTH, verify="/etc/ssl/certs/es-ca.crt")
                print(f"Deleted old snapshot: {snapshot['snapshot']}")
        except ValueError:
            pass  # 跳过非日期命名的快照


if __name__ == "__main__":
    snap_name = create_snapshot()
    success = wait_for_snapshot(snap_name)
    if success:
        delete_old_snapshots()
```

## 恢复流程与数据验证

快照恢复是高压操作，正式执行前务必在测试环境演练一遍。

### 全量恢复

```bash
# 查看可用快照
GET _snapshot/s3-backup/*?verbose=false

# 恢复特定快照的全部索引
POST _snapshot/s3-backup/logs-2026.04.01/_restore
{
  "indices": "*",
  "ignore_unavailable": true,
  "include_global_state": false,
  "rename_pattern": "(.+)",
  "rename_replacement": "restored_$1"  // 加前缀避免与现有索引冲突
}
```

`rename_pattern` 和 `rename_replacement` 在恢复到同一个集群时很有用，避免和当前正在运行的索引冲突。

### 恢复特定索引

```bash
POST _snapshot/s3-backup/logs-2026.04.01/_restore
{
  "indices": "logs-payment-service-*",
  "ignore_unavailable": true,
  "include_global_state": false
}
```

**重要：** 恢复索引之前，目标索引必须是 `closed` 状态或者不存在。如果恢复到同名索引，需要先关闭它：

```bash
POST logs-payment-service-000001/_close
```

这是一个常见坑，下面踩坑记录里会详细说。

### 监控恢复进度

```bash
GET _recovery?human=true&active_only=true
```

输出里可以看到每个分片的恢复进度（`index.percent`）、来源（`source.type: snapshot`）和预估剩余时间。

### 数据验证

恢复完成后，验证数据完整性：

```bash
# 1. 检查索引状态
GET _cat/indices/restored_logs-*?v&h=index,status,pri,rep,docs.count,store.size

# 2. 比对文档数（与快照元数据对比）
GET _snapshot/s3-backup/logs-2026.04.01
# 返回的 indices 里有每个索引的文档数，和恢复后的文档数对比

# 3. 采样查询，验证数据内容
GET restored_logs-payment-service-000001/_search
{
  "query": {
    "range": {
      "@timestamp": {
        "gte": "2026-04-01T00:00:00",
        "lte": "2026-04-01T23:59:59"
      }
    }
  },
  "size": 5
}
```

## 跨集群迁移方案对比

我们在把日志平台从裸机 ES 迁到 ECK 的过程中，评估了三种方案：

### 方案一：Snapshot Restore（快照恢复）

**适用场景：** 迁移存量历史数据，允许短暂停写，数据量大（TB 级以上）

**流程：**
1. 停止或暂停数据写入（或者用 read-only 锁定源集群）
2. 在源集群创建最终快照
3. 在目标集群注册相同的 S3 Repository
4. 在目标集群执行 restore

```bash
# 在目标集群注册同一个 S3 bucket
PUT _snapshot/s3-backup
{
  "type": "s3",
  "settings": {
    "bucket": "es-backup-prod-logging",
    "region": "us-west-2",
    "base_path": "snapshots/prod",
    "readonly": true  // 只读模式，防止误写
  }
}

# 恢复
POST _snapshot/s3-backup/logs-2026.04.01/_restore
{
  "indices": "logs-*",
  "include_global_state": false
}
```

**优点：** 速度快（S3 直接传输，不经过 ES），适合大数据量
**缺点：** 需要停写，有停机窗口

### 方案二：Reindex from Remote

**适用场景：** 在线迁移，数据量中等（几十 GB 到几百 GB），不能停写

```bash
# 在目标集群执行（目标集群需要能访问源集群的 HTTP 端口）
POST _reindex
{
  "source": {
    "remote": {
      "host": "https://source-es:9200",
      "username": "elastic",
      "password": "source-password",
      "socket_timeout": "1m",
      "connect_timeout": "10s"
    },
    "index": "logs-payment-service-*",
    "query": {
      "range": {
        "@timestamp": {
          "gte": "2026-01-01"
        }
      }
    },
    "size": 1000  // 每批拉取文档数
  },
  "dest": {
    "index": "logs-payment-service"
  }
}
```

reindex 默认是同步的，数据量大时容易超时。使用异步模式：

```bash
POST _reindex?wait_for_completion=false
{...}
# 返回 task_id，用以下命令监控进度
GET _tasks/<task_id>
```

**优点：** 不需要停写，可以在线迁移
**缺点：** 速度慢（数据经过源 ES → 网络 → 目标 ES，两次序列化反序列化），对源集群有查询压力

### 方案三：Cross-Cluster Replication（CCR）

**适用场景：** 需要持续同步的多集群架构，或者零停机迁移

CCR 是 ES 的付费功能（需要 Platinum 许可证），支持把索引从一个集群实时同步到另一个集群。

```bash
# 在目标集群配置远程集群连接
PUT _cluster/settings
{
  "persistent": {
    "cluster.remote.source-cluster.seeds": [
      "source-es-master-0:9300",
      "source-es-master-1:9300",
      "source-es-master-2:9300"
    ]
  }
}

# 创建 follower 索引
PUT logs-payment-service-follower/_ccr/follow
{
  "remote_cluster": "source-cluster",
  "leader_index": "logs-payment-service"
}
```

迁移完成后，把 follower 索引 promote 成独立索引：

```bash
POST logs-payment-service-follower/_ccr/unfollow
```

**优点：** 零停机，实时同步，适合灾备场景
**缺点：** 需要 Platinum 许可证（费用较高），延迟通常在秒级

### 方案对比总结

| 方案 | 速度 | 停机时间 | 成本 | 适用数据量 |
|------|------|----------|------|------------|
| Snapshot Restore | 快 | 需要停写 | 低 | 任意 |
| Reindex from Remote | 慢 | 不需要 | 低 | <100GB |
| Cross-Cluster Replication | 实时 | 零停机 | 高（付费） | 任意 |

## 踩坑记录

**坑1：恢复索引时忘记 close 导致报错**

现象：执行 restore 报错 `"[logs-app-000001] index already exists`。

原因：目标集群已经存在同名索引，且是 open 状态，ES 不允许覆盖 open 的索引。

两种解法：

```bash
# 方法一：先关闭索引再恢复
POST logs-app-000001/_close
POST _snapshot/s3-backup/logs-2026.04.01/_restore
{
  "indices": "logs-app-*"
}

# 方法二：恢复时重命名
POST _snapshot/s3-backup/logs-2026.04.01/_restore
{
  "indices": "logs-app-*",
  "rename_pattern": "logs-app-(.*)",
  "rename_replacement": "restored-logs-app-$1"
}
```

**坑2：S3 权限错误的诊断**

现象：注册 S3 Repository 时成功，但 `_verify` 失败，报错 `Access Denied`。

诊断步骤：

```bash
# 1. 检查 ES 节点日志
kubectl logs -n logging es-logging-data-hot-0 | grep -i "s3\|repository\|access"

# 2. 在 ES Pod 里测试 AWS 权限
kubectl exec -it es-logging-data-hot-0 -n logging -- \
  env | grep AWS  # 检查 AWS_ROLE_ARN 和 AWS_WEB_IDENTITY_TOKEN_FILE 是否注入

# 3. 使用 AWS CLI 测试（先安装到 Pod 里）
aws s3 ls s3://es-backup-prod-logging/ --region us-west-2
```

常见原因：
- IRSA annotation 没有打上，或者 Pod 没有重启生效
- IAM Role 的 Trust Policy 里 `sub` 字段的 namespace 或 serviceaccount 名称写错了
- S3 Bucket Policy 没有允许该 Role 访问

**坑3：reindex 中途失败，数据不一致**

现象：reindex 执行到一半网络断了，任务失败，目标索引里只有部分数据。

ES 的 reindex 不是原子操作，中途失败不会自动回滚。

处理方法：

```bash
# 1. 查看失败的 reindex 任务
GET _tasks?actions=*reindex&detailed=true

# 2. 清空目标索引
DELETE logs-app-restored

# 3. 重新执行 reindex，或者使用 slices 并行加速
POST _reindex?wait_for_completion=false
{
  "source": {
    "remote": { ... },
    "index": "logs-app-*",
    "size": 500
  },
  "dest": { "index": "logs-app-restored" },
  "conflicts": "proceed"  // 如果文档已存在，跳过（用于增量同步）
}
```

分片式 reindex（slices）可以大幅加速：

```bash
POST _reindex?slices=auto&wait_for_completion=false
```

`slices=auto` 会自动根据源索引的分片数设置并发度。

**坑4：快照 PARTIAL 状态**

现象：快照状态是 `PARTIAL` 而不是 `SUCCESS`。

PARTIAL 表示快照部分成功——有些索引成功备份，有些失败了。用以下命令查看哪些失败了：

```bash
GET _snapshot/s3-backup/logs-2026.04.01?verbose=true
```

返回结果里 `failures` 字段会列出失败的分片。常见原因是 S3 网络超时，重试通常可以解决。如果持续失败，检查 S3 连通性和 IAM 权限。

PARTIAL 状态的快照可以用于恢复，但被标记为失败的分片不在快照里，对应的数据会丢失。

备份和恢复是 ES 运维的底线，一定要定期演练恢复流程，不然备份就是摆设。下一篇讲 Vector 日志采集管道，这是把日志送进 ES 的关键一环。
