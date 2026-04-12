---
title: "Python 对接 Prometheus：查询监控数据与告警状态自动化"
date: 2024-11-25T11:44:00+08:00
draft: false
tags: ["Python", "Prometheus", "监控", "自动化", "运维"]
categories: ["编程"]
description: "介绍如何用 Python 查询 Prometheus 指标数据和 Alertmanager 告警状态，实现定时巡检、服务可用率日报，并附完整的钉钉推送代码。"
summary: "用 Python 直接调 Prometheus HTTP API，实现服务存活巡检、可用率日报生成，最后接入钉钉每日自动推送集群健康摘要。"
toc: true
math: false
diagram: false
keywords: ["Python", "Prometheus", "PromQL", "Alertmanager", "钉钉", "监控自动化", "巡检"]
params:
  reading_time: true
---

Prometheus 提供了完整的 HTTP API，不依赖任何 SDK 就可以用 `requests` 库直接查询。但对于需要频繁操作的场景，用 `prometheus-api-client` 库会省不少样板代码。这篇文章介绍两种方式，重点放在实际运维场景：定时巡检各服务 UP 状态、生成每日可用率报告、获取 Alertmanager 激活告警，最后整合成一个完整的钉钉推送脚本。

## Prometheus HTTP API 基础

Prometheus 的查询接口就两个核心端点：

| 端点 | 用途 |
|------|------|
| `/api/v1/query` | 即时查询（instant query），返回当前时刻的值 |
| `/api/v1/query_range` | 范围查询（range query），返回时间序列 |

响应结构统一为：

```json
{
  "status": "success",
  "data": {
    "resultType": "vector",
    "result": [
      {
        "metric": {"__name__": "up", "job": "api-server", "instance": "10.0.0.1:8080"},
        "value": [1744329600, "1"]
      }
    ]
  }
}
```

`value` 数组第一个是 Unix 时间戳，第二个是字符串格式的值（注意：即使是数字也是字符串，需要自己转 `float`）。

---

## 封装 Prometheus 客户端

不依赖第三方库的简洁封装：

```python
import time
import requests
from datetime import datetime
from typing import Optional


class PrometheusClient:
    def __init__(self, base_url: str, timeout: int = 30, token: Optional[str] = None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"

    def query(self, promql: str, timestamp: Optional[float] = None) -> list:
        """即时查询，返回 result 列表"""
        params = {"query": promql}
        if timestamp:
            params["time"] = timestamp
        resp = self.session.get(
            f"{self.base_url}/api/v1/query",
            params=params,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        if data["status"] != "success":
            raise RuntimeError(f"Prometheus 查询失败: {data.get('error')}")
        return data["data"]["result"]

    def query_range(
        self,
        promql: str,
        start: float,
        end: float,
        step: str = "60s",
    ) -> list:
        """范围查询"""
        params = {
            "query": promql,
            "start": start,
            "end": end,
            "step": step,
        }
        resp = self.session.get(
            f"{self.base_url}/api/v1/query_range",
            params=params,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        if data["status"] != "success":
            raise RuntimeError(f"Prometheus 范围查询失败: {data.get('error')}")
        return data["data"]["result"]

    def get_scalar(self, promql: str) -> Optional[float]:
        """查询单个标量值，查不到返回 None"""
        results = self.query(promql)
        if not results:
            return None
        return float(results[0]["value"][1])
```

---

## 场景一：定时巡检各服务 UP 状态

`up` 指标是 Prometheus 最基础的健康检查，值为 1 表示 target 存活，0 表示 down：

```python
from dataclasses import dataclass

@dataclass
class ServiceStatus:
    job: str
    instance: str
    status: str   # "up" / "down"


def check_services(client: PrometheusClient) -> list[ServiceStatus]:
    """获取所有 targets 的当前状态"""
    results = client.query("up")
    statuses = []
    for r in results:
        job = r["metric"].get("job", "unknown")
        instance = r["metric"].get("instance", "unknown")
        val = float(r["value"][1])
        statuses.append(ServiceStatus(
            job=job,
            instance=instance,
            status="up" if val == 1.0 else "down",
        ))
    return statuses


def print_health_report(statuses: list[ServiceStatus]):
    down = [s for s in statuses if s.status == "down"]
    up_count = len(statuses) - len(down)
    print(f"健康状态：{up_count}/{len(statuses)} 正常")
    if down:
        print("异常服务：")
        for s in down:
            print(f"  [DOWN] {s.job} / {s.instance}")
```

---

## 场景二：生成每日可用率报告

可用率 = 过去 24 小时内 `up == 1` 的时间占比：

```python
def calc_availability(client: PrometheusClient, job: str, hours: int = 24) -> float:
    """计算指定 job 过去 N 小时的平均可用率"""
    end = time.time()
    start = end - hours * 3600

    # avg_over_time 对 up 指标做时间平均，即可用率
    promql = f'avg_over_time(up{{job="{job}"}}[{hours}h])'
    result = client.query(promql)

    if not result:
        return 0.0

    values = [float(r["value"][1]) for r in result]
    return sum(values) / len(values) * 100


def build_daily_report(client: PrometheusClient, jobs: list[str]) -> str:
    lines = [f"== 服务可用率日报 {datetime.now().strftime('%Y-%m-%d')} ==\n"]
    for job in jobs:
        avail = calc_availability(client, job)
        emoji = "正常" if avail >= 99.9 else ("降级" if avail >= 95 else "故障")
        lines.append(f"[{emoji}] {job}: {avail:.2f}%")
    return "\n".join(lines)
```

---

## 场景三：获取 Alertmanager 激活告警

Alertmanager 有独立的 REST API（默认端口 9093）：

```python
def get_active_alerts(alertmanager_url: str, token: Optional[str] = None) -> list[dict]:
    """
    获取当前激活的告警列表
    返回格式：[{"name": str, "instance": str, "severity": str, "summary": str, "fired_at": str}]
    """
    url = f"{alertmanager_url.rstrip('/')}/api/v2/alerts"
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    resp = requests.get(url, headers=headers, timeout=10)
    resp.raise_for_status()

    alerts = []
    for alert in resp.json():
        labels = alert.get("labels", {})
        annotations = alert.get("annotations", {})
        alerts.append({
            "name":     labels.get("alertname", "unknown"),
            "instance": labels.get("instance", ""),
            "severity": labels.get("severity", "unknown"),
            "summary":  annotations.get("summary", annotations.get("message", "")),
            "fired_at": alert.get("startsAt", ""),
        })

    return alerts
```

---

## 完整示例：每日钉钉推送集群健康摘要

```python
import json
import time
import requests
from datetime import datetime
from typing import Optional


PROMETHEUS_URL  = "http://prometheus.monitoring.svc:9090"
ALERTMANAGER_URL = "http://alertmanager.monitoring.svc:9093"
DINGTALK_WEBHOOK = "https://oapi.dingtalk.com/robot/send?access_token=YOUR_TOKEN"

JOBS_TO_CHECK = [
    "api-gateway",
    "user-service",
    "payment-service",
    "worker",
]


def send_dingtalk(webhook: str, title: str, content: str):
    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": title,
            "text": content,
        },
    }
    resp = requests.post(webhook, json=payload, timeout=10)
    resp.raise_for_status()
    result = resp.json()
    if result.get("errcode") != 0:
        raise RuntimeError(f"钉钉推送失败: {result}")


def build_report() -> str:
    prom = PrometheusClient(PROMETHEUS_URL)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 1. 服务存活状态
    statuses = check_services(prom)
    down_services = [s for s in statuses if s.status == "down"]
    up_count = len(statuses) - len(down_services)

    # 2. 可用率
    avail_lines = []
    for job in JOBS_TO_CHECK:
        avail = calc_availability(prom, job)
        icon = "✅" if avail >= 99.9 else ("⚠️" if avail >= 95 else "❌")
        avail_lines.append(f"{icon} **{job}**: {avail:.2f}%")

    # 3. 激活告警
    try:
        active_alerts = get_active_alerts(ALERTMANAGER_URL)
    except Exception as e:
        active_alerts = []
        print(f"获取告警失败: {e}")

    # 组装 Markdown
    lines = [
        f"## 集群健康日报 - {now_str}",
        "",
        f"### 服务存活",
        f"> 共 {len(statuses)} 个 target，{up_count} 正常，{len(down_services)} 异常",
    ]

    if down_services:
        lines.append("")
        lines.append("**异常服务：**")
        for s in down_services:
            lines.append(f"- ❌ `{s.job}` / `{s.instance}`")

    lines += [
        "",
        "### 24h 可用率",
    ] + avail_lines

    if active_alerts:
        lines += ["", "### 当前激活告警"]
        for a in active_alerts[:10]:   # 最多展示 10 条
            lines.append(f"- **[{a['severity'].upper()}]** {a['name']} - {a['summary']}")
    else:
        lines += ["", "### 告警状态", "> 当前无激活告警 ✅"]

    return "\n".join(lines)


def main():
    report = build_report()
    send_dingtalk(DINGTALK_WEBHOOK, "集群健康日报", report)
    print("推送成功")


if __name__ == "__main__":
    main()
```

配合 cron 每天早上 9 点执行：

```cron
0 9 * * * /usr/bin/python3 /opt/scripts/cluster_health_report.py >> /var/log/health_report.log 2>&1
```

---

## 踩坑记录

**时间范围参数格式**

`/api/v1/query_range` 的 `start` 和 `end` 参数接受 Unix 时间戳（浮点数）或 RFC3339 格式字符串（`2026-04-11T00:00:00+08:00`）。常见错误是传入 `datetime.strftime` 格式的字符串，Prometheus 会返回 400。最稳妥的做法是统一用 `time.time()` 生成时间戳。

**大时间范围查询 timeout**

`step` 参数决定返回的数据点数量。查询 7 天数据如果 `step=1s`，会返回 60 万个数据点，很容易触发 Prometheus 的 `--query.max-samples` 限制（默认 5000 万）或客户端超时。建议按时间范围自动计算步长：

```python
def auto_step(start: float, end: float, max_points: int = 1000) -> str:
    seconds = int((end - start) / max_points)
    return f"{max(seconds, 1)}s"
```

**认证配置**

Prometheus 本身不内置认证，通常通过 nginx/traefik 反代加 Basic Auth 或 Bearer Token。如果用 Basic Auth，`requests.Session` 设置 `auth=("user", "pass")`；Bearer Token 放 `Authorization` header。注意不要把 Token 硬编码在脚本里，从环境变量读取。

**`avg_over_time` 注意事项**

`avg_over_time(up[24h])` 是基于 scrape 采样点做平均，不是真正的时间加权可用率。如果 scrape interval 是 15s，一小时内 up=1 的采样点占比就是可用率的近似值。短暂的网络抖动可能不被采到，导致可用率略高于实际值。
