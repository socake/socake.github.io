---
title: "Python 自动化运维：从脚本到完整工具的工程化实践"
date: 2024-11-12T11:01:00+08:00
draft: false
tags: ["Python", "自动化", "运维", "DevOps", "CLI"]
categories: ["编程"]
description: "用 Python 构建运维自动化工具的工程实践：boto3、K8s SDK、CLI 框架、数据库运维、钉钉通知，从脚本到可维护工具"
summary: "系统梳理 Python 运维自动化的工程化方法：boto3 操作 AWS 资源、Kubernetes Python SDK 使用、Click/Typer CLI 框架选型、数据库批量运维脚本、钉钉 Webhook 集成，以及类型注解与错误处理的实践经验。"
toc: true
math: false
diagram: false
keywords: ["Python", "boto3", "Kubernetes", "click", "typer", "钉钉", "自动化运维", "DevOps"]
params:
  reading_time: true
---

写过十几个内部运维工具之后最大的感受是：能跑的脚本不等于能放生产的工具。Python 生态趁手（boto3、k8s client、DB 驱动一应俱全），但工具写得糙一点，出问题的时候全是自己擦屁股。这篇记的是怎么把脚本写到同事在你休假时也敢跑的程度。

## 项目结构与依赖管理

工程化的第一步是项目结构规范。哪怕是内部运维脚本，也值得像对待真正的工程那样组织：

```
ops-tools/
├── pyproject.toml          # 依赖声明（推荐 uv 管理）
├── src/
│   └── ops_tools/
│       ├── __init__.py
│       ├── aws/
│       │   ├── ec2.py
│       │   ├── eks.py
│       │   └── cost.py
│       ├── k8s/
│       │   └── client.py
│       ├── notify/
│       │   └── dingtalk.py
│       └── cli.py          # 主入口
└── tests/
```

依赖管理推荐使用 `uv`，比 pip 快得多，锁文件可靠：

```bash
# 初始化项目
uv init ops-tools
cd ops-tools

# 添加依赖
uv add boto3 kubernetes click typer rich pymysql psycopg2-binary

# 生成锁文件（提交到 git）
uv lock

# 安装到虚拟环境
uv sync
```

---

## 用 boto3 操作 AWS 资源

### 基础配置与 Session 管理

```python
import boto3
from typing import Optional

def get_boto3_session(
    profile: Optional[str] = None,
    region: str = "us-west-2"
) -> boto3.Session:
    """
    创建 boto3 Session，支持多 profile 切换。
    生产环境使用 IAM Role，本地开发使用 profile。
    """
    if profile:
        return boto3.Session(profile_name=profile, region_name=region)
    return boto3.Session(region_name=region)


def get_client(service: str, region: str = "us-west-2"):
    session = get_boto3_session(region=region)
    return session.client(service)
```

### EC2 实例管理

```python
from dataclasses import dataclass
from typing import Iterator
import boto3

@dataclass
class EC2Instance:
    instance_id: str
    instance_type: str
    state: str
    private_ip: str
    tags: dict[str, str]

    @property
    def name(self) -> str:
        return self.tags.get("Name", "unnamed")


def list_running_instances(
    region: str = "us-west-2",
    filters: Optional[list[dict]] = None
) -> Iterator[EC2Instance]:
    """列出运行中的 EC2 实例，支持过滤条件。"""
    ec2 = boto3.client("ec2", region_name=region)

    default_filters = [{"Name": "instance-state-name", "Values": ["running"]}]
    all_filters = default_filters + (filters or [])

    paginator = ec2.get_paginator("describe_instances")
    for page in paginator.paginate(Filters=all_filters):
        for reservation in page["Reservations"]:
            for inst in reservation["Instances"]:
                tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
                yield EC2Instance(
                    instance_id=inst["InstanceId"],
                    instance_type=inst["InstanceType"],
                    state=inst["State"]["Name"],
                    private_ip=inst.get("PrivateIpAddress", ""),
                    tags=tags,
                )
```

**关键点：** 永远用 `paginator` 而不是直接调用 API，AWS 大多数列表 API 有分页，不用 paginator 会漏数据。

### Cost Explorer：成本查询

```python
from datetime import datetime, timedelta
import boto3

def get_daily_costs(
    days: int = 7,
    group_by: str = "SERVICE"
) -> list[dict]:
    """查询最近 N 天的每日成本，按服务分组。"""
    client = boto3.client("ce", region_name="us-east-1")  # CE 只在 us-east-1

    end = datetime.now().date()
    start = end - timedelta(days=days)

    response = client.get_cost_and_usage(
        TimePeriod={
            "Start": start.strftime("%Y-%m-%d"),
            "End": end.strftime("%Y-%m-%d"),
        },
        Granularity="DAILY",
        Metrics=["UnblendedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": group_by}],
    )

    results = []
    for time_period in response["ResultsByTime"]:
        date = time_period["TimePeriod"]["Start"]
        for group in time_period["Groups"]:
            service = group["Keys"][0]
            cost = float(group["Metrics"]["UnblendedCost"]["Amount"])
            if cost > 0.01:  # 过滤掉接近 0 的条目
                results.append({"date": date, "service": service, "cost": cost})

    return results
```

---

## Kubernetes Python SDK

### 初始化客户端

```python
from kubernetes import client, config, watch
from kubernetes.client.exceptions import ApiException
import os

def get_k8s_client(context: Optional[str] = None) -> tuple:
    """
    返回 (v1, apps_v1) 客户端对。
    集群内运行时自动使用 ServiceAccount，本地使用 kubeconfig。
    """
    if os.path.exists("/var/run/secrets/kubernetes.io/serviceaccount"):
        # 在 Pod 内运行
        config.load_incluster_config()
    else:
        config.load_kube_config(context=context)

    v1 = client.CoreV1Api()
    apps_v1 = client.AppsV1Api()
    return v1, apps_v1
```

### 查询 Pod 和读取 ConfigMap

```python
def list_pods_by_label(
    namespace: str,
    label_selector: str,
    context: Optional[str] = None
) -> list[dict]:
    """
    查询指定 namespace 中匹配标签的 Pod。
    label_selector 格式：'app=my-app,env=prod'
    """
    v1, _ = get_k8s_client(context=context)

    pods = v1.list_namespaced_pod(
        namespace=namespace,
        label_selector=label_selector,
    )

    result = []
    for pod in pods.items:
        result.append({
            "name": pod.metadata.name,
            "phase": pod.status.phase,
            "node": pod.spec.node_name,
            "restart_count": sum(
                cs.restart_count
                for cs in (pod.status.container_statuses or [])
            ),
            "ready": all(
                cs.ready
                for cs in (pod.status.container_statuses or [])
            ),
        })

    return result


def get_configmap_data(
    name: str,
    namespace: str,
    context: Optional[str] = None
) -> dict[str, str]:
    """读取 ConfigMap 的 data 字段，返回空 dict 而不是抛出异常（不存在时）。"""
    v1, _ = get_k8s_client(context=context)
    try:
        cm = v1.read_namespaced_config_map(name=name, namespace=namespace)
        return cm.data or {}
    except ApiException as e:
        if e.status == 404:
            return {}
        raise
```

### Watch 事件流

```python
def watch_pod_events(namespace: str, timeout_seconds: int = 60) -> None:
    """监听 Pod 事件，适合部署验证场景。"""
    v1, _ = get_k8s_client()
    w = watch.Watch()

    print(f"开始监听 {namespace} 的 Pod 事件，超时 {timeout_seconds}s...")
    for event in w.stream(
        v1.list_namespaced_event,
        namespace=namespace,
        timeout_seconds=timeout_seconds,
    ):
        obj = event["object"]
        if obj.involved_object.kind == "Pod":
            print(
                f"[{event['type']}] {obj.involved_object.name}: "
                f"{obj.reason} - {obj.message}"
            )
```

---

## CLI 工具工程化：argparse vs click vs typer

### 选型建议

- **argparse**：标准库，无额外依赖，适合简单单文件脚本
- **click**：功能完善，社区成熟，装饰器风格，适合中大型 CLI 工具
- **typer**：基于 click 封装，利用类型注解自动生成命令，代码量最少，**推荐新项目使用**

### Typer 实战示例

```python
# cli.py
import typer
from rich.console import Console
from rich.table import Table
from typing import Optional

app = typer.Typer(help="运维自动化工具集")
console = Console()


@app.command()
def pods(
    namespace: str = typer.Argument("default", help="Kubernetes namespace"),
    label: str = typer.Option("", "--label", "-l", help="标签选择器"),
    context: Optional[str] = typer.Option(None, "--context", "-c", help="kubeconfig context"),
    show_restart: bool = typer.Option(False, "--restarts", help="显示重启次数"),
):
    """列出 Pod 状态，支持标签过滤。"""
    pods_data = list_pods_by_label(namespace, label, context)

    table = Table(title=f"Pods in {namespace}")
    table.add_column("Name", style="cyan")
    table.add_column("Phase")
    table.add_column("Ready")
    table.add_column("Node")
    if show_restart:
        table.add_column("Restarts", justify="right")

    for pod in pods_data:
        ready_style = "green" if pod["ready"] else "red"
        row = [
            pod["name"],
            pod["phase"],
            f"[{ready_style}]{'✓' if pod['ready'] else '✗'}[/{ready_style}]",
            pod["node"] or "-",
        ]
        if show_restart:
            restart_style = "red" if pod["restart_count"] > 5 else "white"
            row.append(f"[{restart_style}]{pod['restart_count']}[/{restart_style}]")
        table.add_row(*row)

    console.print(table)


@app.command()
def costs(
    days: int = typer.Option(7, "--days", "-d", help="查询天数"),
    top: int = typer.Option(10, "--top", "-n", help="显示 Top N 服务"),
):
    """查询 AWS 成本，按服务汇总。"""
    data = get_daily_costs(days=days)

    # 汇总各服务总成本
    service_totals: dict[str, float] = {}
    for item in data:
        service_totals[item["service"]] = (
            service_totals.get(item["service"], 0) + item["cost"]
        )

    sorted_services = sorted(service_totals.items(), key=lambda x: x[1], reverse=True)

    table = Table(title=f"最近 {days} 天 AWS 成本（Top {top}）")
    table.add_column("Service", style="cyan")
    table.add_column("Total Cost (USD)", justify="right")

    for service, cost in sorted_services[:top]:
        table.add_row(service, f"${cost:.2f}")

    console.print(table)
    console.print(f"\n[bold]总计：${sum(service_totals.values()):.2f}[/bold]")


if __name__ == "__main__":
    app()
```

---

## 数据库运维脚本

### MySQL 批量查询

```python
import pymysql
from contextlib import contextmanager
from typing import Any, Generator

@contextmanager
def mysql_connection(
    host: str,
    port: int,
    user: str,
    password: str,
    database: str,
    charset: str = "utf8mb4",
) -> Generator[pymysql.connections.Connection, None, None]:
    """上下文管理器，确保连接被正确关闭。"""
    conn = pymysql.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
        charset=charset,
        cursorclass=pymysql.cursors.DictCursor,  # 返回字典而不是元组
        connect_timeout=5,
    )
    try:
        yield conn
    finally:
        conn.close()


def query_with_limit(
    conn: pymysql.connections.Connection,
    sql: str,
    params: tuple = (),
    limit: int = 1000,
) -> list[dict]:
    """
    执行查询，强制附加 LIMIT 防止全表扫描。
    运维规范：查询默认加 LIMIT。
    """
    # 粗略检查是否已有 LIMIT（不是完美解析，但能防止遗忘）
    if "LIMIT" not in sql.upper():
        sql = f"{sql.rstrip(';')} LIMIT {limit}"

    with conn.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall()
```

### PostgreSQL 连接与查询

```python
import psycopg2
import psycopg2.extras
from contextlib import contextmanager

@contextmanager
def pg_connection(dsn: str):
    """
    dsn 格式：postgresql://user:password@host:5432/dbname
    """
    conn = psycopg2.connect(dsn, connect_timeout=5)
    conn.set_session(readonly=True)  # 运维查询默认只读，防误操作
    try:
        yield conn
    finally:
        conn.close()


def explain_query(conn, sql: str) -> list[str]:
    """执行 EXPLAIN ANALYZE，用于慢查询分析。"""
    with conn.cursor() as cur:
        cur.execute(f"EXPLAIN ANALYZE {sql}")
        return [row[0] for row in cur.fetchall()]
```

---

## 钉钉 Webhook 通知集成

```python
import hashlib
import hmac
import time
import base64
import urllib.parse
import requests
import json
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class DingTalkConfig:
    webhook_url: str
    secret: Optional[str] = None  # 加签安全设置（推荐启用）


class DingTalkNotifier:
    def __init__(self, config: DingTalkConfig):
        self.config = config

    def _sign(self) -> dict[str, str]:
        """生成钉钉签名，防止 Webhook 被盗用。"""
        if not self.config.secret:
            return {}

        timestamp = str(round(time.time() * 1000))
        sign_str = f"{timestamp}\n{self.config.secret}"
        hmac_code = hmac.new(
            self.config.secret.encode("utf-8"),
            sign_str.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
        return {"timestamp": timestamp, "sign": sign}

    def send_text(self, content: str, at_mobiles: Optional[list[str]] = None) -> bool:
        """发送文本消息。"""
        payload = {
            "msgtype": "text",
            "text": {"content": content},
            "at": {
                "atMobiles": at_mobiles or [],
                "isAtAll": False,
            },
        }
        return self._send(payload)

    def send_markdown(
        self,
        title: str,
        text: str,
        at_mobiles: Optional[list[str]] = None,
    ) -> bool:
        """发送 Markdown 消息（支持格式化）。"""
        payload = {
            "msgtype": "markdown",
            "markdown": {"title": title, "text": text},
            "at": {
                "atMobiles": at_mobiles or [],
                "isAtAll": False,
            },
        }
        return self._send(payload)

    def send_alert(
        self,
        title: str,
        content: str,
        severity: str = "warning",
        at_all: bool = False,
    ) -> bool:
        """发送告警消息（带颜色标记）。"""
        color_map = {
            "info": "#0099FF",
            "warning": "#FF9900",
            "critical": "#FF0000",
        }
        color = color_map.get(severity, "#888888")

        text = (
            f"## {title}\n\n"
            f"> **级别：** <font color={color}>{severity.upper()}</font>\n\n"
            f"{content}\n\n"
            f"> 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}"
        )

        payload = {
            "msgtype": "markdown",
            "markdown": {"title": title, "text": text},
            "at": {"isAtAll": at_all},
        }
        return self._send(payload)

    def _send(self, payload: dict) -> bool:
        params = self._sign()
        url = self.config.webhook_url
        if params:
            url = f"{url}&timestamp={params['timestamp']}&sign={params['sign']}"

        try:
            resp = requests.post(
                url,
                json=payload,
                timeout=10,
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
            result = resp.json()
            if result.get("errcode") != 0:
                logger.error("钉钉发送失败：%s", result)
                return False
            return True
        except requests.RequestException as e:
            logger.error("钉钉请求异常：%s", e)
            return False


# 使用示例
def send_deployment_notification(
    service: str,
    version: str,
    env: str,
    status: str,
    webhook_url: str,
    secret: str,
):
    notifier = DingTalkNotifier(DingTalkConfig(webhook_url=webhook_url, secret=secret))

    severity = "info" if status == "success" else "critical"
    content = (
        f"- **服务：** {service}\n"
        f"- **版本：** {version}\n"
        f"- **环境：** {env}\n"
        f"- **状态：** {'✅ 成功' if status == 'success' else '❌ 失败'}"
    )
    notifier.send_alert(
        title=f"部署通知：{service} {env}",
        content=content,
        severity=severity,
    )
```

---

## 日志与错误处理最佳实践

### 结构化日志配置

```python
import logging
import sys
import json
from datetime import datetime

class JsonFormatter(logging.Formatter):
    """JSON 格式日志，方便 Loki/ELK 解析。"""

    def format(self, record: logging.LogRecord) -> str:
        log_data = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
        }
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_data, ensure_ascii=False)


def setup_logging(level: str = "INFO", json_format: bool = False) -> None:
    handler = logging.StreamHandler(sys.stdout)
    if json_format:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
    logging.basicConfig(level=getattr(logging, level), handlers=[handler])
```

### 重试装饰器

```python
import functools
import time
import logging
from typing import Callable, TypeVar, ParamSpec

P = ParamSpec("P")
T = TypeVar("T")

logger = logging.getLogger(__name__)


def retry(
    max_attempts: int = 3,
    delay: float = 1.0,
    backoff: float = 2.0,
    exceptions: tuple = (Exception,),
) -> Callable[[Callable[P, T]], Callable[P, T]]:
    """指数退避重试装饰器，适合 API 调用和网络请求。"""
    def decorator(func: Callable[P, T]) -> Callable[P, T]:
        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            current_delay = delay
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    if attempt == max_attempts:
                        logger.error(
                            "%s 失败（已重试 %d 次）：%s",
                            func.__name__, max_attempts, e
                        )
                        raise
                    logger.warning(
                        "%s 第 %d 次失败，%.1fs 后重试：%s",
                        func.__name__, attempt, current_delay, e
                    )
                    time.sleep(current_delay)
                    current_delay *= backoff
        return wrapper
    return decorator


# 使用
@retry(max_attempts=3, delay=2.0, exceptions=(requests.RequestException,))
def fetch_metrics(url: str) -> dict:
    resp = requests.get(url, timeout=5)
    resp.raise_for_status()
    return resp.json()
```

---

## 类型注解与运维脚本的平衡

类型注解在运维脚本中的价值：**不是为了静态类型检查，而是为了自文档化和 IDE 提示**。

实践建议：

1. **函数签名必须注解**：参数和返回值类型，让调用方一目了然
2. **局部变量适度注解**：复杂推断的地方加，简单赋值不必加
3. **用 `Optional[X]` 替代 `X | None`**（兼容 Python 3.9 以下）
4. **数据类用 `dataclass` 而不是 `dict`**：重要的数据结构定义成 dataclass，字段有类型、有默认值、有 repr

```python
# 不好：dict 表示结构化数据，字段不明确
def get_cluster_info(name: str) -> dict:
    ...

# 好：dataclass 明确结构
@dataclass
class ClusterInfo:
    name: str
    region: str
    node_count: int
    version: str
    status: str = "unknown"

def get_cluster_info(name: str) -> ClusterInfo:
    ...
```

**何时不需要类型注解：** 一次性脚本（用完即删）、30 行以内的简单工具。过度追求类型完整性会降低迭代速度，运维脚本要在正确性和开发效率之间找平衡。

---

## 工程化总结

从"能跑的脚本"到"可维护的工具"，关键差距在于：

1. **错误处理不能省**：网络抖动、权限不足、资源不存在，每种异常都要有明确处理
2. **日志要有意义**：不要只打 "start" / "done"，要打足够重现问题的上下文
3. **幂等性**：运维脚本往往需要重跑，确保重复执行不会出问题
4. **dry-run 模式**：危险操作（删除、修改配置）要支持 `--dry-run`，先打印将要执行的操作
5. **配置外化**：webhook URL、数据库连接等从环境变量读取，不要硬编码

一个好的运维工具，应该是同事在你不在时也能放心跑的东西。
