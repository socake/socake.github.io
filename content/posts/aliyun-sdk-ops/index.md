---
title: "阿里云 SDK 运维自动化：ECS/ACK/RDS 资源管理与巡检脚本"
date: 2025-12-04T12:56:00+08:00
draft: false
tags: ["阿里云", "Python", "SDK", "自动化", "运维"]
categories: ["编程"]
description: "介绍阿里云 Python SDK 的初始化最佳实践，以及 ECS、ACK、RDS 的常用运维操作，最终整合为一个自动化巡检报告并推送钉钉。"
summary: "用阿里云 Python SDK 实现 ECS 实例查询与监控、ACK 节点状态检查、RDS 慢查询巡检，整合成 HTML 格式巡检报告自动推送钉钉。"
toc: true
math: false
diagram: false
keywords: ["阿里云SDK", "Python", "ECS", "ACK", "RDS", "自动化巡检", "钉钉", "RAM", "STS"]
params:
  reading_time: true
---

阿里云 SDK 是运维自动化的基础工具。查询实例状态、拉取监控数据、巡检安全组规则——这些手动在控制台操作很低效，写成脚本定时执行才是正确方式。这篇文章介绍我日常用到的几个核心模块：ECS、ACK、RDS，最后整合成一个自动化巡检脚本。

## SDK 初始化与认证

### 安装依赖

```bash
pip install alibabacloud-tea-openapi
pip install alibabacloud-ecs20140526    # ECS
pip install alibabacloud-cs20151215     # ACK（容器服务）
pip install alibabacloud-rds20140815    # RDS
pip install alibabacloud-cms20190101    # 云监控
```

### AK/SK 认证（长期凭据）

```python
from alibabacloud_tea_openapi import models as open_api_models

def get_config(region: str = "cn-hangzhou") -> open_api_models.Config:
    import os
    config = open_api_models.Config(
        access_key_id=os.environ["ALIYUN_ACCESS_KEY_ID"],
        access_key_secret=os.environ["ALIYUN_ACCESS_KEY_SECRET"],
    )
    config.endpoint = f"ecs.{region}.aliyuncs.com"
    return config
```

永远不要把 AK/SK 硬编码在代码里，从环境变量或 Secret Manager 读取。

### STS 临时凭据（推荐）

ECS 实例上运行的脚本，推荐用实例 RAM 角色（Instance RAM Role）获取临时凭据，不需要在机器上存 AK/SK：

```python
import requests
from alibabacloud_credentials.client import Client as CredentialClient
from alibabacloud_credentials.models import Config as CredentialConfig

def get_sts_credential():
    """从实例元数据服务获取 RAM 角色临时凭据"""
    # 先获取绑定的 RAM 角色名
    role_url = "http://100.100.100.200/latest/meta-data/ram/security-credentials/"
    role_name = requests.get(role_url, timeout=3).text.strip()

    # 获取临时凭据
    cred_url = f"{role_url}{role_name}"
    cred = requests.get(cred_url, timeout=3).json()

    return {
        "access_key_id":     cred["AccessKeyId"],
        "access_key_secret": cred["AccessKeySecret"],
        "security_token":    cred["SecurityToken"],
        "expiration":        cred["Expiration"],
    }
```

### 最小权限原则

为巡检脚本创建专用 RAM 用户，只授予只读权限：

```json
{
  "Statement": [
    {
      "Action": [
        "ecs:Describe*",
        "ecs:ListTagResources",
        "cms:QueryMetricList",
        "cs:DescribeClusterNodes",
        "cs:DescribeClusterKubeconfig",
        "rds:DescribeDBInstances",
        "rds:DescribeSlowLogs",
        "rds:DescribeBackupPolicy"
      ],
      "Effect": "Allow",
      "Resource": "*"
    }
  ],
  "Version": "1"
}
```

---

## ECS 操作

### 查询实例列表

```python
from alibabacloud_ecs20140526.client import Client as EcsClient
from alibabacloud_ecs20140526 import models as ecs_models


def list_ecs_instances(region: str, tag_key: str = None, tag_value: str = None) -> list[dict]:
    """分页查询 ECS 实例列表"""
    config = open_api_models.Config(
        access_key_id=os.environ["ALIYUN_ACCESS_KEY_ID"],
        access_key_secret=os.environ["ALIYUN_ACCESS_KEY_SECRET"],
        endpoint=f"ecs.{region}.aliyuncs.com",
    )
    client = EcsClient(config)

    instances = []
    page_number = 1
    page_size = 100

    while True:
        request = ecs_models.DescribeInstancesRequest(
            region_id=region,
            page_number=page_number,
            page_size=page_size,
        )
        if tag_key:
            request.tag = [
                ecs_models.DescribeInstancesRequestTag(key=tag_key, value=tag_value)
            ]

        resp = client.describe_instances(request)
        batch = resp.body.instances.instance

        for inst in batch:
            instances.append({
                "id":       inst.instance_id,
                "name":     inst.instance_name,
                "status":   inst.status,
                "type":     inst.instance_type,
                "ip":       inst.inner_ip_address.ip_address[0] if inst.inner_ip_address.ip_address else "",
                "region":   inst.region_id,
                "zone":     inst.zone_id,
            })

        # 检查是否还有下一页（分页查询必须用 PageSize + PageNumber）
        total = resp.body.total_count
        if page_number * page_size >= total:
            break
        page_number += 1

    return instances
```

**注意**：分页查询一定要用 `PageSize + PageNumber` 循环，直到 `PageNumber * PageSize >= TotalCount`。不能用 `NextToken` 方式（ECS 新版 API 支持，但老接口不支持）。

### 获取 CPU/内存监控数据

```python
from alibabacloud_cms20190101.client import Client as CmsClient
from alibabacloud_cms20190101 import models as cms_models
from datetime import datetime, timedelta
import json


def get_ecs_metrics(
    instance_id: str,
    region: str,
    metric_name: str = "CPUUtilization",
    minutes: int = 60,
) -> list[dict]:
    """
    获取 ECS 监控数据
    metric_name 参考：CPUUtilization / memory_usedutilization / disk.io.read / disk.io.write
    """
    config = open_api_models.Config(
        access_key_id=os.environ["ALIYUN_ACCESS_KEY_ID"],
        access_key_secret=os.environ["ALIYUN_ACCESS_KEY_SECRET"],
        endpoint="metrics.cn-hangzhou.aliyuncs.com",
    )
    client = CmsClient(config)

    end_time = datetime.utcnow()
    start_time = end_time - timedelta(minutes=minutes)

    request = cms_models.DescribeMetricListRequest(
        namespace="acs_ecs_dashboard",
        metric_name=metric_name,
        dimensions=json.dumps([{"instanceId": instance_id}]),
        start_time=start_time.strftime("%Y-%m-%d %H:%M:%S"),
        end_time=end_time.strftime("%Y-%m-%d %H:%M:%S"),
        period="60",   # 60 秒粒度
    )

    resp = client.describe_metric_list(request)
    if resp.body.code != "200":
        raise RuntimeError(f"获取监控数据失败: {resp.body.message}")

    data_points = json.loads(resp.body.datapoints or "[]")
    return [{"timestamp": p["timestamp"], "average": p.get("Average", 0)} for p in data_points]


def get_instance_cpu_avg(instance_id: str, region: str) -> float:
    """获取最近 1 小时平均 CPU 使用率"""
    points = get_ecs_metrics(instance_id, region, "CPUUtilization", 60)
    if not points:
        return 0.0
    return sum(p["average"] for p in points) / len(points)
```

### 安全组规则检查

检查是否有向 `0.0.0.0/0` 开放高危端口：

```python
RISKY_PORTS = {22, 3306, 6379, 27017, 9200, 8080, 8443}

def check_security_groups(region: str) -> list[dict]:
    """检查安全组中是否存在高危开放规则"""
    config = open_api_models.Config(
        access_key_id=os.environ["ALIYUN_ACCESS_KEY_ID"],
        access_key_secret=os.environ["ALIYUN_ACCESS_KEY_SECRET"],
        endpoint=f"ecs.{region}.aliyuncs.com",
    )
    client = EcsClient(config)

    risks = []
    # 先获取所有安全组
    sg_resp = client.describe_security_groups(
        ecs_models.DescribeSecurityGroupsRequest(region_id=region, page_size=100)
    )

    for sg in sg_resp.body.security_groups.security_group:
        # 查询安全组规则
        rules_resp = client.describe_security_group_attribute(
            ecs_models.DescribeSecurityGroupAttributeRequest(
                region_id=region,
                security_group_id=sg.security_group_id,
                direction="ingress",
            )
        )

        for rule in rules_resp.body.permissions.permission:
            if rule.source_cidr_ip != "0.0.0.0/0":
                continue
            port_range = rule.port_range  # 格式如 "22/22" 或 "1/65535"
            start, end = (int(p) for p in port_range.split("/"))
            for risky_port in RISKY_PORTS:
                if start <= risky_port <= end:
                    risks.append({
                        "sg_id":   sg.security_group_id,
                        "sg_name": sg.security_group_name,
                        "port":    risky_port,
                        "rule":    f"{rule.ip_protocol}/{port_range}",
                    })

    return risks
```

---

## ACK 集群操作

### 查询节点状态

```python
from alibabacloud_cs20151215.client import Client as CsClient
from alibabacloud_cs20151215 import models as cs_models


def get_ack_nodes(cluster_id: str) -> list[dict]:
    """查询 ACK 集群所有节点状态"""
    config = open_api_models.Config(
        access_key_id=os.environ["ALIYUN_ACCESS_KEY_ID"],
        access_key_secret=os.environ["ALIYUN_ACCESS_KEY_SECRET"],
        endpoint="cs.aliyuncs.com",
    )
    client = CsClient(config)

    resp = client.describe_cluster_nodes(
        cluster_id,
        cs_models.DescribeClusterNodesRequest(page_size=100),
    )

    nodes = []
    for node in resp.body.nodes:
        nodes.append({
            "name":    node.node_name,
            "status":  node.state,     # "running" / "stopped"
            "ip":      node.ip_address,
            "type":    node.instance_type,
            "roles":   node.node_role,
        })

    not_running = [n for n in nodes if n["status"] != "running"]
    if not_running:
        print(f"异常节点：{[n['name'] for n in not_running]}")

    return nodes


def get_cluster_kubeconfig(cluster_id: str) -> str:
    """获取集群 kubeconfig（临时访问用）"""
    config = open_api_models.Config(
        access_key_id=os.environ["ALIYUN_ACCESS_KEY_ID"],
        access_key_secret=os.environ["ALIYUN_ACCESS_KEY_SECRET"],
        endpoint="cs.aliyuncs.com",
    )
    client = CsClient(config)

    resp = client.describe_cluster_user_kubeconfig(
        cluster_id,
        cs_models.DescribeClusterUserKubeconfigRequest(private_ip_address=True),
    )
    return resp.body.config
```

---

## RDS 操作

### 查询实例状态

```python
from alibabacloud_rds20140815.client import Client as RdsClient
from alibabacloud_rds20140815 import models as rds_models


def list_rds_instances(region: str) -> list[dict]:
    config = open_api_models.Config(
        access_key_id=os.environ["ALIYUN_ACCESS_KEY_ID"],
        access_key_secret=os.environ["ALIYUN_ACCESS_KEY_SECRET"],
        endpoint=f"rds.{region}.aliyuncs.com",
    )
    client = RdsClient(config)

    instances = []
    page_number = 1

    while True:
        resp = client.describe_dbinstances(
            rds_models.DescribeDBInstancesRequest(
                region_id=region,
                page_number=page_number,
                page_size=100,
            )
        )
        for inst in resp.body.items.dbinstance:
            instances.append({
                "id":       inst.dbinstance_id,
                "desc":     inst.dbinstance_description,
                "status":   inst.dbinstance_status,
                "engine":   f"{inst.engine} {inst.engine_version}",
                "class":    inst.dbinstance_class,
            })

        if page_number * 100 >= resp.body.total_record_count:
            break
        page_number += 1

    return instances
```

### 慢查询日志巡检

```python
from datetime import date, timedelta

def get_slow_queries(instance_id: str, region: str, days: int = 1) -> list[dict]:
    """获取 RDS 慢查询摘要"""
    config = open_api_models.Config(
        access_key_id=os.environ["ALIYUN_ACCESS_KEY_ID"],
        access_key_secret=os.environ["ALIYUN_ACCESS_KEY_SECRET"],
        endpoint=f"rds.{region}.aliyuncs.com",
    )
    client = RdsClient(config)

    end_date = date.today().strftime("%Y-%m-%dZ")
    start_date = (date.today() - timedelta(days=days)).strftime("%Y-%m-%dZ")

    resp = client.describe_slow_logs(
        rds_models.DescribeSlowLogsRequest(
            dbinstance_id=instance_id,
            start_time=start_date,
            end_time=end_date,
            page_size=50,
        )
    )

    slow_logs = []
    for item in (resp.body.items.sqlslowlog or []):
        slow_logs.append({
            "db":             item.dbname,
            "sql":            item.sqltext[:200],   # 截断避免太长
            "avg_time_ms":    item.avg_execution_time,
            "max_time_ms":    item.max_execution_time,
            "total_count":    item.total_execution_counts,
        })

    return sorted(slow_logs, key=lambda x: x["max_time_ms"], reverse=True)
```

### 备份状态巡检

```python
def check_backup_status(instance_id: str, region: str) -> dict:
    """检查 RDS 最近一次备份是否正常"""
    config = open_api_models.Config(
        access_key_id=os.environ["ALIYUN_ACCESS_KEY_ID"],
        access_key_secret=os.environ["ALIYUN_ACCESS_KEY_SECRET"],
        endpoint=f"rds.{region}.aliyuncs.com",
    )
    client = RdsClient(config)

    resp = client.describe_backups(
        rds_models.DescribeBackupsRequest(
            dbinstance_id=instance_id,
            backup_status="Success",
            page_size=1,
        )
    )

    items = resp.body.items.backup
    if not items:
        return {"status": "NO_BACKUP", "last_backup": None}

    last = items[0]
    return {
        "status":      last.backup_status,
        "last_backup": last.backup_end_time,
        "size_mb":     round(int(last.backup_size) / 1024 / 1024, 2),
    }
```

---

## 整合：自动化巡检报告推送钉钉

```python
import os
import json
import requests
from datetime import datetime


DINGTALK_WEBHOOK = os.environ["DINGTALK_WEBHOOK"]
REGION = "cn-hangzhou"
ACK_CLUSTER_ID = os.environ.get("ACK_CLUSTER_ID", "")
RDS_INSTANCE_IDS = os.environ.get("RDS_INSTANCE_IDS", "").split(",")


def send_dingtalk_markdown(title: str, content: str):
    payload = {
        "msgtype": "markdown",
        "markdown": {"title": title, "text": content},
    }
    resp = requests.post(DINGTALK_WEBHOOK, json=payload, timeout=10)
    result = resp.json()
    if result.get("errcode") != 0:
        raise RuntimeError(f"钉钉推送失败: {result}")


def run_inspection():
    lines = [
        f"## 阿里云资源巡检报告",
        f"> {datetime.now().strftime('%Y-%m-%d %H:%M')} | 区域：{REGION}",
        "",
    ]

    # 1. ECS 实例状态
    try:
        instances = list_ecs_instances(REGION)
        stopped = [i for i in instances if i["status"] != "Running"]
        lines += [
            "### ECS 实例",
            f"共 {len(instances)} 台，{len(instances) - len(stopped)} 台运行中，{len(stopped)} 台异常",
        ]
        for inst in stopped[:5]:
            lines.append(f"- ❌ `{inst['name']}` ({inst['id']}): {inst['status']}")
    except Exception as e:
        lines.append(f"### ECS 实例\n> 查询失败：{e}")

    lines.append("")

    # 2. 安全组高危规则
    try:
        risks = check_security_groups(REGION)
        if risks:
            lines += [
                "### 安全组高危规则",
                f"> 发现 {len(risks)} 条高危入站规则（源地址 0.0.0.0/0）",
            ]
            for r in risks[:5]:
                lines.append(f"- ⚠️ `{r['sg_name']}` 端口 {r['port']} 对公网开放")
        else:
            lines.append("### 安全组\n> 未发现高危规则 ✅")
    except Exception as e:
        lines.append(f"### 安全组\n> 检查失败：{e}")

    lines.append("")

    # 3. ACK 节点
    if ACK_CLUSTER_ID:
        try:
            nodes = get_ack_nodes(ACK_CLUSTER_ID)
            abnormal = [n for n in nodes if n["status"] != "running"]
            status_icon = "✅" if not abnormal else "❌"
            lines += [
                "### ACK 节点",
                f"{status_icon} 共 {len(nodes)} 个节点，{len(abnormal)} 个异常",
            ]
            for n in abnormal[:3]:
                lines.append(f"- ❌ `{n['name']}`: {n['status']}")
        except Exception as e:
            lines.append(f"### ACK 节点\n> 查询失败：{e}")

    lines.append("")

    # 4. RDS 备份状态
    if RDS_INSTANCE_IDS:
        lines.append("### RDS 备份状态")
        for inst_id in RDS_INSTANCE_IDS:
            if not inst_id:
                continue
            try:
                backup = check_backup_status(inst_id, REGION)
                icon = "✅" if backup["status"] == "Success" else "❌"
                lines.append(
                    f"{icon} `{inst_id}`: 最近备份 {backup['last_backup']} ({backup['size_mb']}MB)"
                )
            except Exception as e:
                lines.append(f"❌ `{inst_id}`: 查询失败 - {e}")

    content = "\n".join(lines)
    send_dingtalk_markdown("阿里云资源巡检报告", content)
    print("巡检报告推送成功")


if __name__ == "__main__":
    run_inspection()
```

---

## 踩坑记录

**分页查询必须用 PageSize + PageNumber**

阿里云 ECS/RDS 等老版本 API 的分页机制是 `PageNumber` 从 1 开始递增，直到 `PageNumber * PageSize >= TotalCount`。漏掉分页逻辑会导致只拿到前 100 条数据，当实例数量超过 100 时悄悄遗漏，巡检报告产生误报。新版 API（如 SLB、OSS）改用 `NextToken`，注意区分。

**监控数据有 2-3 分钟延迟**

云监控的指标数据从采集到可查询有 2-3 分钟延迟。查询当前时刻的监控数据如果 `endTime` 设为 `now()`，最后几条数据可能是空的。建议 `endTime` 向前推 5 分钟，或者用 `startTime = now - 10min` 拿最近 10 分钟数据再取最后一个非空点。

**RAM 权限最小化**

巡检脚本只需要 `Describe*` 和 `List*` 类只读权限。不要为了省事直接授予 `AdministratorAccess`。如果巡检脚本的 AK 泄露，只读权限的攻击面远小于管理员权限。建议给巡检 RAM 用户单独创建策略，并定期轮换 AK。

**STS Token 过期**

使用实例 RAM 角色时，临时凭据有效期通常是 6 小时，即将到期前会自动刷新。但如果把 STS Token 缓存在变量里长期使用，会遇到 `InvalidSecurityToken.Expired` 错误。建议每次请求都重新从元数据服务获取凭据，或者做好过期检测和自动刷新逻辑。
