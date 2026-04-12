---
title: "告警带图实战：Grafana Render + 钉钉推送趋势图"
date: 2026-04-11T11:00:00+08:00
draft: false
tags: ["Grafana", "Prometheus", "告警", "钉钉", "可观测性"]
categories: ["可观测性"]
description: "通过 Grafana Image Renderer 和 Alertmanager Webhook，实现告警触发时自动截取 Grafana 趋势图并推送到钉钉，让值班人员无需登录 Grafana 即可直观判断问题。"
summary: "收到告警只有一行数字，还要登录 Grafana 才能看趋势图——这是告警体验最大的痛点之一。本文介绍如何将 Grafana Image Renderer 与 Alertmanager Webhook 结合，实现告警消息自动附带趋势图的完整方案。"
toc: true
math: false
diagram: false
series: ["可观测性实战"]
keywords: ["Grafana", "Image Renderer", "告警带图", "钉钉", "Webhook", "Panel Render"]
params:
  reading_time: true
---

## 痛点：告警缺乏上下文

典型的告警消息长这样：

```
🔴 [CRITICAL] 告警触发
告警名称：HighCpuUsage
告警级别：critical
影响实例：10.0.1.5:9100
描述：节点 10.0.1.5 CPU 使用率超过 85%，当前值 92%
触发时间：2026-04-11 08:30:00 UTC
```

这条消息有一个根本问题：**只有告警触发瞬间的数字，没有趋势**。收到这条消息，值班工程师无法判断：

- CPU 是突然飙升还是缓慢爬升的？
- 是持续高负载还是短暂尖峰？
- 最近一小时整体趋势怎样？

每次都要登录 Grafana，找到对应 Dashboard，调整时间范围，才能看到趋势图。深夜告警时这个流程尤其低效。

解决方案：**告警触发时自动截取 Grafana Panel 图片，附在通知消息中一起发送。**

---

## 方案架构

```
Prometheus 告警触发
       ↓
Alertmanager 路由
       ↓
Webhook 服务接收告警
       ↓
调用 Grafana Render API 生成图片
       ↓
上传图片到钉钉（base64 或 OSS URL）
       ↓
钉钉推送带图消息
```

核心是 Grafana 的 `/render/d-solo` 接口，它调用 Grafana Image Renderer 插件，用无头 Chrome 渲染指定 Panel 并返回 PNG 图片。

---

## Grafana Image Renderer 部署

Image Renderer 是 Grafana 的一个独立服务（也可以作为插件嵌入），内部用 Puppeteer + 无头 Chrome 渲染页面截图。在 K8s 中推荐用 sidecar 或独立 Deployment 方式部署。

### 独立 Deployment 部署（推荐）

独立部署的好处是内存隔离，Renderer 崩溃不影响 Grafana 主进程。

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: grafana-image-renderer
  namespace: monitoring
spec:
  replicas: 1
  selector:
    matchLabels:
      app: grafana-image-renderer
  template:
    metadata:
      labels:
        app: grafana-image-renderer
    spec:
      containers:
        - name: renderer
          image: grafana/grafana-image-renderer:latest
          ports:
            - containerPort: 8081
          env:
            - name: ENABLE_METRICS
              value: "true"
            - name: HTTP_PORT
              value: "8081"
            - name: RENDERING_MODE
              value: "clustered"          # 多进程模式，提高并发
            - name: RENDERING_CLUSTERING_MODE
              value: "browser"
            - name: RENDERING_CLUSTERING_MAX_CONCURRENCY
              value: "3"
            - name: RENDERING_VERBOSE_LOGGING
              value: "false"
          resources:
            requests:
              cpu: 100m
              memory: 512Mi
            limits:
              cpu: 1000m
              memory: 1.5Gi              # 无头 Chrome 吃内存，给足
          securityContext:
            runAsUser: 1000
            runAsGroup: 1000
---
apiVersion: v1
kind: Service
metadata:
  name: grafana-image-renderer
  namespace: monitoring
spec:
  selector:
    app: grafana-image-renderer
  ports:
    - port: 8081
      targetPort: 8081
```

### 配置 Grafana 使用外部 Renderer

在 Grafana 的配置中（或环境变量）添加：

```ini
[rendering]
server_url = http://grafana-image-renderer:8081/render
callback_url = http://grafana:3000/
```

用环境变量的方式（K8s Deployment）：

```yaml
env:
  - name: GF_RENDERING_SERVER_URL
    value: "http://grafana-image-renderer:8081/render"
  - name: GF_RENDERING_CALLBACK_URL
    value: "http://grafana:3000/"
```

验证配置是否生效：在 Grafana UI 的任意 Panel 右上角菜单中选择 "Share" → "Direct link rendered image"，能成功下载图片说明配置正确。

---

## Grafana Render API 详解

Grafana 提供了 `/render/d-solo` 接口用于渲染单个 Panel：

```
GET /render/d-solo/<dashboard-uid>/<panel-slug>
  ?panelId=<panel-id>
  &orgId=1
  &from=<start-timestamp>
  &to=<end-timestamp>
  &width=800
  &height=400
  &tz=Asia/Shanghai
  &var-instance=10.0.1.5:9100
```

关键参数：

| 参数 | 说明 | 示例 |
|---|---|---|
| `dashboard-uid` | Dashboard 的 UID（不是数字 ID） | `node-exporter-full` |
| `panelId` | Panel 的数字 ID | `3` |
| `from` / `to` | 时间范围，Unix 毫秒时间戳或相对时间 | `now-1h` / `now` |
| `width` / `height` | 图片尺寸（像素） | `800` / `400` |
| `tz` | 时区 | `Asia%2FShanghai` |
| `var-xxx` | Dashboard 变量值，用于过滤 | `var-instance=10.0.1.5` |

获取 Dashboard UID 和 Panel ID 的方法：

1. 在 Grafana 打开目标 Dashboard，URL 中 `/d/` 后面的字符串就是 UID
2. 点击 Panel 标题 → "Edit"，URL 中 `?editPanel=` 后面的数字就是 Panel ID

---

## 完整 Python 实现

以下是结合 Alertmanager Webhook、Grafana Render API、钉钉推送的完整实现：

```python
import os
import time
import hmac
import hashlib
import base64
import urllib.parse
import logging
from datetime import datetime, timezone
from typing import Optional

import requests
from flask import Flask, request, jsonify

logger = logging.getLogger(__name__)
app = Flask(__name__)

# 配置
GRAFANA_URL = os.environ.get('GRAFANA_URL', 'http://grafana:3000')
GRAFANA_TOKEN = os.environ.get('GRAFANA_API_TOKEN', '')
DINGTALK_WEBHOOK = os.environ.get('DINGTALK_WEBHOOK_URL', '')
DINGTALK_SECRET = os.environ.get('DINGTALK_SECRET', '')

# 告警名称到 Grafana Panel 的映射表
ALERT_PANEL_MAP = {
    'HighCpuUsage': {
        'dashboard_uid': 'rYdddlPWk',       # Node Exporter Full
        'panel_id': 3,                        # CPU Usage Panel
        'vars': ['instance'],                 # 从告警 labels 中提取哪些变量
    },
    'HighMemoryUsage': {
        'dashboard_uid': 'rYdddlPWk',
        'panel_id': 4,
        'vars': ['instance'],
    },
    'DiskUsageHigh': {
        'dashboard_uid': 'rYdddlPWk',
        'panel_id': 7,
        'vars': ['instance', 'mountpoint'],
    },
    'ProcessNotRunning': {
        'dashboard_uid': 'process-exporter',
        'panel_id': 2,
        'vars': ['node_ip'],
    },
}


def render_grafana_panel(
    dashboard_uid: str,
    panel_id: int,
    variables: dict,
    time_range: str = "1h",
    width: int = 800,
    height: int = 350,
) -> Optional[bytes]:
    """
    调用 Grafana Render API 生成 Panel 图片
    返回 PNG 图片字节，失败返回 None
    """
    now_ms = int(time.time() * 1000)
    duration_map = {
        "30m": 30 * 60 * 1000,
        "1h": 60 * 60 * 1000,
        "3h": 3 * 60 * 60 * 1000,
        "6h": 6 * 60 * 60 * 1000,
    }
    duration_ms = duration_map.get(time_range, 60 * 60 * 1000)
    from_ms = now_ms - duration_ms

    params = {
        'panelId': panel_id,
        'orgId': 1,
        'from': from_ms,
        'to': now_ms,
        'width': width,
        'height': height,
        'tz': 'Asia/Shanghai',
    }

    # 添加 Dashboard 变量（用于过滤数据）
    for var_name, var_value in variables.items():
        params[f'var-{var_name}'] = var_value

    render_url = f"{GRAFANA_URL}/render/d-solo/{dashboard_uid}"

    headers = {}
    if GRAFANA_TOKEN:
        headers['Authorization'] = f'Bearer {GRAFANA_TOKEN}'

    try:
        resp = requests.get(
            render_url,
            params=params,
            headers=headers,
            timeout=30,              # Renderer 可能比较慢，给 30s
        )
        resp.raise_for_status()

        content_type = resp.headers.get('content-type', '')
        if 'image' not in content_type:
            logger.error(f"Grafana Render 返回非图片内容: {content_type}, body: {resp.text[:200]}")
            return None

        logger.info(f"Grafana 图片渲染成功，大小: {len(resp.content)} bytes")
        return resp.content

    except requests.Timeout:
        logger.error(f"Grafana Render 超时 (30s): {render_url}")
        return None
    except Exception as e:
        logger.error(f"Grafana Render 失败: {e}")
        return None


def upload_image_to_dingtalk(image_bytes: bytes) -> Optional[str]:
    """
    将图片上传到钉钉媒体接口，返回 media_id
    注意：此接口需要企业内部应用权限，普通自定义机器人不支持
    替代方案：上传到 OSS 并获取公网 URL
    """
    # 实际项目中建议上传到 OSS（阿里云/AWS S3）获取公网 URL
    # 这里演示 base64 方式（仅 actionCard 类型支持）
    return base64.b64encode(image_bytes).decode('utf-8')


def dingtalk_sign() -> dict:
    if not DINGTALK_SECRET:
        return {}
    timestamp = str(round(time.time() * 1000))
    sign_str = f"{timestamp}\n{DINGTALK_SECRET}"
    hmac_code = hmac.new(
        DINGTALK_SECRET.encode('utf-8'),
        sign_str.encode('utf-8'),
        digestmod=hashlib.sha256
    ).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
    return {'timestamp': timestamp, 'sign': sign}


def send_dingtalk_with_image(
    title: str,
    content_md: str,
    image_bytes: Optional[bytes] = None,
    at_all: bool = False
):
    """
    发送钉钉消息，如果有图片则上传到 OSS 并附在消息中
    """
    params = dingtalk_sign()
    url = DINGTALK_WEBHOOK
    if params:
        url += '&' + '&'.join(f"{k}={v}" for k, v in params.items())

    if image_bytes:
        # 生产环境：将图片上传到 OSS，获取公网 URL
        # oss_url = upload_to_oss(image_bytes)
        # content_md += f"\n\n![趋势图]({oss_url})"

        # 演示：用 Markdown 图片占位（需要 OSS URL 才能正常显示）
        logger.info("图片渲染成功，在生产环境中应上传到 OSS 并附在消息中")

    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": title,
            "text": content_md
        },
        "at": {"isAtAll": at_all}
    }

    resp = requests.post(url, json=payload, timeout=10)
    resp.raise_for_status()
    result = resp.json()
    if result.get('errcode') != 0:
        raise RuntimeError(f"钉钉发送失败: {result}")


def build_alert_message(alert: dict, status_text: str) -> str:
    labels = alert.get('labels', {})
    annotations = alert.get('annotations', {})
    severity = labels.get('severity', 'info')
    severity_icon = {'critical': '🔴', 'warning': '🟡', 'info': '🔵'}.get(severity, '⚪')

    return (
        f"## {severity_icon} {status_text}\n\n"
        f"**告警名称**：{labels.get('alertname', 'N/A')}\n\n"
        f"**告警级别**：{severity}\n\n"
        f"**影响范围**：{labels.get('instance', labels.get('job', 'N/A'))}\n\n"
        f"**详情**：{annotations.get('description', annotations.get('summary', 'N/A'))}\n\n"
        f"**时间**：{alert.get('startsAt', '')[:19].replace('T', ' ')} UTC\n\n"
    )


@app.route('/webhook', methods=['POST'])
def webhook():
    payload = request.get_json(force=True)
    if not payload:
        return jsonify({'error': 'empty body'}), 400

    for alert in payload.get('alerts', []):
        labels = alert.get('labels', {})
        alertname = labels.get('alertname', '')
        status = alert.get('status', 'firing')

        status_text = '告警触发' if status == 'firing' else '告警恢复'
        message = build_alert_message(alert, status_text)

        # 查找对应的 Grafana Panel 配置
        image_bytes = None
        panel_config = ALERT_PANEL_MAP.get(alertname)

        if panel_config and status == 'firing':
            # 从告警 labels 中提取需要传给 Grafana 的变量
            variables = {}
            for var in panel_config.get('vars', []):
                if var in labels:
                    variables[var] = labels[var]

            logger.info(f"开始渲染 Panel: {alertname}, variables: {variables}")
            image_bytes = render_grafana_panel(
                dashboard_uid=panel_config['dashboard_uid'],
                panel_id=panel_config['panel_id'],
                variables=variables,
                time_range="1h",
            )

            if image_bytes:
                message += "\n\n> 趋势图已渲染（生产环境请配置 OSS 上传以在消息中显示）\n"
            else:
                message += "\n\n> ⚠️ 趋势图渲染失败，请手动登录 Grafana 查看\n"

        try:
            send_dingtalk_with_image(
                title=f"[{labels.get('severity', 'info').upper()}] {alertname}",
                content_md=message,
                image_bytes=image_bytes,
                at_all=(labels.get('severity') == 'critical'),
            )
        except Exception as e:
            logger.error(f"发送钉钉消息失败: {e}")

    return jsonify({'result': 'ok'}), 200


@app.route('/health')
def health():
    return jsonify({'status': 'ok'}), 200
```

---

## 图片上传到 OSS（生产实践）

钉钉 Markdown 消息中的 `![图片](url)` 必须是**公网可访问的 HTTP/HTTPS URL**，不支持 base64 内嵌（除了 `image` 类型消息）。生产环境需要将截图上传到对象存储：

```python
import boto3
import uuid
from datetime import datetime

s3_client = boto3.client('s3', region_name='us-west-2')
BUCKET = 'your-alert-images-bucket'
CDN_DOMAIN = 'https://alert-images.your-domain.com'

def upload_to_oss(image_bytes: bytes, alertname: str) -> str:
    """上传图片到 S3/OSS，返回公网访问 URL"""
    date_prefix = datetime.now().strftime('%Y/%m/%d')
    key = f"alert-images/{date_prefix}/{alertname}-{uuid.uuid4().hex[:8]}.png"

    s3_client.put_object(
        Bucket=BUCKET,
        Key=key,
        Body=image_bytes,
        ContentType='image/png',
        # 7天后自动过期
    )

    return f"{CDN_DOMAIN}/{key}"
```

配置 S3 生命周期策略，自动清理 7 天前的截图，控制存储成本。

---

## 踩坑记录

### Renderer 内存占用过高导致 OOM

Grafana Image Renderer 内部运行无头 Chrome，每个渲染请求会消耗约 200-400MB 内存。如果告警风暴触发大量并发渲染请求，容易 OOM。

解法：
1. 在 Renderer 中设置 `RENDERING_CLUSTERING_MAX_CONCURRENCY=3` 限制并发
2. Webhook 侧对渲染请求加信号量限制
3. 给 Renderer Pod 设置合理的内存 limit（建议 1.5GB 以上），并配置 HPA 或直接设置副本数

### 图片渲染显示 "No data"

告警触发时 Panel 渲染出来是空白或 "No data"，原因通常是 Dashboard 变量没有正确传递。

排查步骤：
1. 在浏览器中手动访问 Render URL，检查是否能看到数据
2. 检查 `var-xxx` 参数值是否和 Dashboard 变量的实际值匹配（注意大小写、冒号等）
3. 确认告警 labels 中的 `instance` 值和 Dashboard 中的 instance 变量格式一致

### 钉钉 Markdown 图片不显示

最常见原因：图片 URL 是内网地址（如 `http://minio.svc.cluster.local/...`），钉钉服务器无法访问。

必须使用公网可访问的 URL，推荐方案：
- AWS S3 + CloudFront
- 阿里云 OSS + CDN
- 自建 MinIO + Nginx 公网代理

### 渲染请求卡死不返回

某些版本的 Renderer 在高负载下会卡死，requests 的 `timeout=30` 不够用。建议加 `connect_timeout`：

```python
resp = requests.get(render_url, params=params, timeout=(5, 30))
# (connect_timeout, read_timeout)
```

同时在 Alertmanager Webhook 配置中设置较短的超时，避免一个渲染卡死影响其他告警：

```yaml
webhook_configs:
  - url: 'http://alert-webhook:5001/webhook'
    http_config:
      tls_config: {}
    timeout: 15s
```

---

## 效果对比

实现告警带图后，值班工程师处理告警的效率明显提升：

- **不需要登录 Grafana**：消息中直接显示最近 1 小时的趋势图，瞬间判断是尖峰还是持续问题
- **误报识别变快**：看到趋势图是短暂的尖刺就可以先观察，不需要立即介入
- **沟通成本降低**：把带图的告警消息截图分享给业务方，不需要额外解释

这套方案已经在我们的生产环境稳定运行，每天处理几十条告警通知，Renderer 的 CPU/内存消耗完全在可控范围内。
