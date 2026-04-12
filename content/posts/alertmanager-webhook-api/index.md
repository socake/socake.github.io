---
title: "Alertmanager Webhook 开发：自定义告警处理与 API 集成"
date: 2026-04-11T09:00:00+08:00
draft: false
tags: ["Alertmanager", "Webhook", "Prometheus", "Python", "告警"]
categories: ["可观测性"]
description: "从零实现 Alertmanager Webhook 接收器，包含钉钉推送、告警去重、Alertmanager API 调用（查询激活告警、创建静默），以及容器化部署和避坑指南。"
summary: "Alertmanager 内置的通知渠道不支持钉钉、飞书等国内工具，Webhook 是扩展告警通知的标准方式。本文用 Python Flask 实现完整的 Webhook 接收器，涵盖消息格式化、降噪去重、Alertmanager API 集成和 K8s 部署。"
toc: true
math: false
diagram: false
series: ["可观测性实战"]
keywords: ["Alertmanager", "Webhook", "告警推送", "钉钉", "Silence API", "Python Flask"]
params:
  reading_time: true
---

## Alertmanager Webhook 机制

Prometheus 负责采集数据和生成告警规则，Alertmanager 负责接收告警、去重、分组、路由，最终发送通知。Alertmanager 内置支持 Email、Slack、PagerDuty、企业微信等，但对于国内团队最常用的钉钉、飞书，需要通过 Webhook 自行实现。

Webhook 的工作流程：

1. Prometheus 触发告警规则，向 Alertmanager 发送告警
2. Alertmanager 按路由规则处理后，向配置的 Webhook URL 发送 HTTP POST 请求
3. Webhook 服务接收请求，解析告警数据，调用目标通知渠道（钉钉/飞书/企微等）
4. Webhook 返回 2xx 状态码，Alertmanager 确认发送成功

### Alertmanager 侧的配置

在 `alertmanager.yml` 中配置 Webhook 接收器：

```yaml
route:
  group_by: ['alertname', 'team']
  group_wait: 30s        # 同组告警等待时间，用于合并
  group_interval: 5m     # 同组后续告警发送间隔
  repeat_interval: 4h    # 未恢复告警重复发送间隔
  receiver: 'webhook-default'
  routes:
    - match:
        severity: critical
      receiver: 'webhook-critical'
      repeat_interval: 1h   # critical 告警更频繁重复

receivers:
  - name: 'webhook-default'
    webhook_configs:
      - url: 'http://alert-webhook:5001/webhook'
        send_resolved: true  # 告警恢复时也发送通知
        max_alerts: 20       # 单次最多发送的告警数量

  - name: 'webhook-critical'
    webhook_configs:
      - url: 'http://alert-webhook:5001/webhook'
        send_resolved: true
```

---

## Webhook 请求数据结构

Alertmanager 向 Webhook 发送的是 JSON 格式的 POST 请求，结构如下：

```json
{
  "receiver": "webhook-default",
  "status": "firing",
  "alerts": [
    {
      "status": "firing",
      "labels": {
        "alertname": "HighCpuUsage",
        "instance": "10.0.1.5:9100",
        "job": "node-exporter",
        "severity": "warning",
        "team": "infra"
      },
      "annotations": {
        "summary": "CPU 使用率过高",
        "description": "节点 10.0.1.5 CPU 使用率超过 85%，当前值 92%",
        "value": "0.92"
      },
      "startsAt": "2026-04-11T08:30:00.000Z",
      "endsAt": "0001-01-01T00:00:00Z",
      "generatorURL": "http://prometheus:9090/graph?g0.expr=...",
      "fingerprint": "a1b2c3d4e5f6"
    }
  ],
  "groupLabels": {
    "alertname": "HighCpuUsage"
  },
  "commonLabels": {
    "job": "node-exporter",
    "severity": "warning",
    "team": "infra"
  },
  "commonAnnotations": {
    "summary": "CPU 使用率过高"
  },
  "externalURL": "http://alertmanager:9093",
  "truncatedAlerts": 0
}
```

关键字段说明：

- `status`：整个 batch 的状态，`firing` 或 `resolved`
- `alerts`：告警数组，一次推送可能包含多个告警
- `alerts[].fingerprint`：告警唯一标识，由 labels 哈希生成，用于去重
- `alerts[].endsAt`：`0001-01-01` 表示告警还在触发中；有具体时间表示已恢复
- `truncatedAlerts`：超过 `max_alerts` 被截断的告警数量，不为 0 时需要注意

---

## Python Flask Webhook 实现

下面是一个完整的 Webhook 接收器实现，支持钉钉推送、告警去重和按 severity 分级处理。

### 项目结构

```
alert-webhook/
├── app.py          # 主程序
├── notifier.py     # 通知渠道（钉钉）
├── dedup.py        # 去重模块
├── requirements.txt
└── Dockerfile
```

### app.py

```python
import logging
from flask import Flask, request, jsonify
from notifier import DingTalkNotifier
from dedup import AlertDedup

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# 初始化钉钉通知器（从环境变量读取 Token）
notifier = DingTalkNotifier()

# 告警去重器，5 分钟窗口
dedup = AlertDedup(window_seconds=300)


def format_alert_message(alert: dict, status_text: str) -> str:
    """格式化单条告警消息"""
    labels = alert.get('labels', {})
    annotations = alert.get('annotations', {})

    severity = labels.get('severity', 'unknown')
    severity_emoji = {'critical': '🔴', 'warning': '🟡', 'info': '🔵'}.get(severity, '⚪')

    lines = [
        f"{severity_emoji} **{status_text}**",
        f"**告警名称**：{labels.get('alertname', 'N/A')}",
        f"**告警级别**：{severity}",
        f"**所属团队**：{labels.get('team', 'N/A')}",
        f"**影响实例**：{labels.get('instance', labels.get('job', 'N/A'))}",
        f"**描述**：{annotations.get('description', annotations.get('summary', 'N/A'))}",
    ]

    if alert.get('status') == 'resolved':
        lines.append(f"**恢复时间**：{alert.get('endsAt', 'N/A')[:19].replace('T', ' ')} UTC")
    else:
        lines.append(f"**触发时间**：{alert.get('startsAt', 'N/A')[:19].replace('T', ' ')} UTC")

    return '\n'.join(lines)


@app.route('/webhook', methods=['POST'])
def webhook():
    """接收 Alertmanager Webhook 请求"""
    try:
        payload = request.get_json(force=True)
    except Exception as e:
        logger.error(f"解析请求体失败: {e}")
        return jsonify({'error': 'invalid json'}), 400

    if not payload:
        return jsonify({'error': 'empty body'}), 400

    logger.info(f"收到告警请求，状态: {payload.get('status')}, 告警数: {len(payload.get('alerts', []))}")

    truncated = payload.get('truncatedAlerts', 0)
    if truncated > 0:
        logger.warning(f"本次推送有 {truncated} 条告警被截断，请检查 max_alerts 配置")

    messages = []
    for alert in payload.get('alerts', []):
        fingerprint = alert.get('fingerprint', '')
        status = alert.get('status', '')

        # 去重检查：firing 状态的告警 5 分钟内不重复发送
        if status == 'firing':
            if dedup.is_duplicate(fingerprint):
                logger.info(f"告警 {fingerprint} 在去重窗口内，跳过发送")
                continue
            dedup.mark_sent(fingerprint)

        status_text = '告警触发' if status == 'firing' else '告警恢复'
        msg = format_alert_message(alert, status_text)
        messages.append((msg, alert.get('labels', {}).get('severity', 'info')))

    if not messages:
        logger.info("所有告警均已去重，无需发送")
        return jsonify({'result': 'deduped'}), 200

    # 按 severity 分级：critical 单独发送，其他合并发送
    critical_msgs = [m for m, s in messages if s == 'critical']
    other_msgs = [m for m, s in messages if s != 'critical']

    for msg in critical_msgs:
        try:
            notifier.send_markdown(title="[CRITICAL] 告警通知", content=msg, at_all=True)
        except Exception as e:
            logger.error(f"发送 critical 告警失败: {e}")

    if other_msgs:
        combined = '\n\n---\n\n'.join(other_msgs)
        try:
            notifier.send_markdown(
                title=f"告警通知 ({len(other_msgs)} 条)",
                content=combined,
                at_all=False
            )
        except Exception as e:
            logger.error(f"发送告警合并消息失败: {e}")

    return jsonify({'result': 'ok', 'sent': len(messages)}), 200


@app.route('/health')
def health():
    return jsonify({'status': 'ok'}), 200


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, threaded=True)
```

### notifier.py（钉钉推送）

```python
import os
import time
import hmac
import hashlib
import base64
import urllib.parse
import requests
import logging

logger = logging.getLogger(__name__)


class DingTalkNotifier:
    def __init__(self):
        self.webhook_url = os.environ.get('DINGTALK_WEBHOOK_URL', '')
        self.secret = os.environ.get('DINGTALK_SECRET', '')
        if not self.webhook_url:
            raise ValueError("DINGTALK_WEBHOOK_URL 环境变量未设置")

    def _sign(self) -> dict:
        """生成钉钉签名（加签安全模式）"""
        if not self.secret:
            return {}
        timestamp = str(round(time.time() * 1000))
        sign_str = f"{timestamp}\n{self.secret}"
        hmac_code = hmac.new(
            self.secret.encode('utf-8'),
            sign_str.encode('utf-8'),
            digestmod=hashlib.sha256
        ).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
        return {'timestamp': timestamp, 'sign': sign}

    def send_markdown(self, title: str, content: str, at_all: bool = False):
        """发送 Markdown 格式消息"""
        params = self._sign()
        url = self.webhook_url
        if params:
            url += '&' + '&'.join(f"{k}={v}" for k, v in params.items())

        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": title,
                "text": content
            },
            "at": {
                "isAtAll": at_all
            }
        }

        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        result = resp.json()
        if result.get('errcode') != 0:
            raise RuntimeError(f"钉钉 API 返回错误: {result}")
        logger.info(f"钉钉消息发送成功: {title}")
```

### dedup.py（告警去重）

```python
import time
import threading
from collections import defaultdict


class AlertDedup:
    """
    基于内存的告警去重器。
    同一 fingerprint 的 firing 告警在 window_seconds 内只发送一次。
    """

    def __init__(self, window_seconds: int = 300):
        self.window = window_seconds
        self._sent: dict[str, float] = {}
        self._lock = threading.Lock()

    def is_duplicate(self, fingerprint: str) -> bool:
        with self._lock:
            sent_at = self._sent.get(fingerprint)
            if sent_at is None:
                return False
            return (time.time() - sent_at) < self.window

    def mark_sent(self, fingerprint: str):
        with self._lock:
            self._sent[fingerprint] = time.time()
            # 清理过期记录，避免内存无限增长
            now = time.time()
            expired = [k for k, v in self._sent.items() if now - v > self.window * 2]
            for k in expired:
                del self._sent[k]
```

---

## Alertmanager API 使用

Alertmanager 提供了 REST API（v2），可以通过代码查询激活告警、创建和删除静默规则，适合与运维平台、工单系统集成。

### 查询激活告警

```python
import requests

ALERTMANAGER_URL = "http://alertmanager:9093"

def get_active_alerts(filter_labels: dict = None) -> list:
    """查询当前激活的告警"""
    params = {'active': 'true', 'silenced': 'false', 'inhibited': 'false'}
    if filter_labels:
        # 格式：team="infra" 或 severity="critical"
        params['filter'] = [f'{k}="{v}"' for k, v in filter_labels.items()]

    resp = requests.get(f"{ALERTMANAGER_URL}/api/v2/alerts", params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


# 示例：查询 team=infra 的所有 critical 告警
alerts = get_active_alerts({'team': 'infra', 'severity': 'critical'})
for alert in alerts:
    print(alert['labels']['alertname'], alert['annotations'].get('description'))
```

### 创建告警静默

静默（Silence）是 Alertmanager 的核心功能之一，在计划维护期间可以通过 API 批量创建静默，避免告警轰炸：

```python
from datetime import datetime, timezone, timedelta

def create_silence(
    matchers: list[dict],
    duration_hours: int = 2,
    created_by: str = "ops-bot",
    comment: str = ""
) -> str:
    """
    创建告警静默规则
    
    matchers 示例：
    [{"name": "team", "value": "infra", "isRegex": False, "isEqual": True}]
    
    返回 silence ID
    """
    now = datetime.now(timezone.utc)
    ends_at = now + timedelta(hours=duration_hours)

    payload = {
        "matchers": matchers,
        "startsAt": now.strftime('%Y-%m-%dT%H:%M:%S.000Z'),
        "endsAt": ends_at.strftime('%Y-%m-%dT%H:%M:%S.000Z'),
        "createdBy": created_by,
        "comment": comment or f"Silence created by {created_by} at {now.strftime('%Y-%m-%d %H:%M')}"
    }

    resp = requests.post(
        f"{ALERTMANAGER_URL}/api/v2/silences",
        json=payload,
        timeout=10
    )
    resp.raise_for_status()
    silence_id = resp.json()['silenceID']
    print(f"静默规则已创建，ID: {silence_id}，有效期 {duration_hours} 小时")
    return silence_id


def delete_silence(silence_id: str):
    """删除（过期）指定 ID 的静默规则"""
    resp = requests.delete(
        f"{ALERTMANAGER_URL}/api/v2/silence/{silence_id}",
        timeout=10
    )
    resp.raise_for_status()
    print(f"静默规则 {silence_id} 已删除")


# 使用示例：发布前创建静默，发布完成后删除
silence_id = create_silence(
    matchers=[
        {"name": "team", "value": "payment", "isRegex": False, "isEqual": True}
    ],
    duration_hours=1,
    created_by="deploy-bot",
    comment="Payment service deployment window"
)

# ... 执行发布流程 ...

delete_silence(silence_id)
```

### 通过 API 直接推送告警

某些场景下（如 cron 脚本执行失败、批处理任务异常），不需要通过 Prometheus 采集，可以直接调用 Alertmanager API 推送告警：

```python
def push_alert(alertname: str, labels: dict, description: str, severity: str = "warning"):
    """直接向 Alertmanager 推送告警事件"""
    now = datetime.now(timezone.utc)
    payload = [{
        "startsAt": now.strftime('%Y-%m-%dT%H:%M:%S.000Z'),
        "labels": {
            "alertname": alertname,
            "severity": severity,
            **labels
        },
        "annotations": {
            "description": description,
            "summary": alertname
        }
    }]

    resp = requests.post(
        f"{ALERTMANAGER_URL}/api/v2/alerts",
        json=payload,
        timeout=10
    )
    resp.raise_for_status()


# 示例：备份脚本失败时推送告警
try:
    run_backup()
except Exception as e:
    push_alert(
        alertname="BackupJobFailed",
        labels={"job": "mysql-backup", "team": "dba"},
        description=f"MySQL 备份失败: {e}",
        severity="critical"
    )
```

---

## 容器化部署到 K8s

### Dockerfile

```dockerfile
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

USER nobody

CMD ["gunicorn", "--bind", "0.0.0.0:5001", "--workers", "2", "--timeout", "30", "app:app"]
```

### requirements.txt

```
flask==3.0.0
gunicorn==21.2.0
requests==2.31.0
```

### K8s Deployment

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: alert-webhook
  namespace: monitoring
spec:
  replicas: 2
  selector:
    matchLabels:
      app: alert-webhook
  template:
    metadata:
      labels:
        app: alert-webhook
    spec:
      containers:
        - name: alert-webhook
          image: your-registry/alert-webhook:latest
          ports:
            - containerPort: 5001
          env:
            - name: DINGTALK_WEBHOOK_URL
              valueFrom:
                secretKeyRef:
                  name: alert-webhook-secrets
                  key: dingtalk-webhook-url
            - name: DINGTALK_SECRET
              valueFrom:
                secretKeyRef:
                  name: alert-webhook-secrets
                  key: dingtalk-secret
          resources:
            requests:
              cpu: 50m
              memory: 64Mi
            limits:
              cpu: 200m
              memory: 256Mi
          livenessProbe:
            httpGet:
              path: /health
              port: 5001
            initialDelaySeconds: 10
            periodSeconds: 30
          readinessProbe:
            httpGet:
              path: /health
              port: 5001
            initialDelaySeconds: 5
            periodSeconds: 10
---
apiVersion: v1
kind: Service
metadata:
  name: alert-webhook
  namespace: monitoring
spec:
  selector:
    app: alert-webhook
  ports:
    - port: 5001
      targetPort: 5001
```

Secret 创建：

```bash
kubectl create secret generic alert-webhook-secrets \
  --from-literal=dingtalk-webhook-url="https://oapi.dingtalk.com/robot/send?access_token=xxx" \
  --from-literal=dingtalk-secret="SECxxx" \
  -n monitoring
```

---

## 踩坑记录

### Webhook 超时导致 Alertmanager 重试风暴

**现象**：Alertmanager 日志出现大量 `context deadline exceeded`，同一告警被重复推送多次。

**原因**：Alertmanager 的 Webhook 默认超时是 10 秒。如果 Webhook 服务处理慢（比如调用钉钉 API 超时），Alertmanager 会认为发送失败，按 `repeat_interval` 重试，而上一个请求实际上可能还在处理中，造成重复推送。

**解法**：
1. Webhook 服务要快速响应（200ms 以内），把耗时操作（调用钉钉 API）放到后台线程或消息队列
2. 对 Alertmanager 的 Webhook 配置设置合理的 `http_config.timeout`

```python
# 快速响应模式：收到请求后立即放入队列，返回 200
from queue import Queue
import threading

alert_queue = Queue(maxsize=1000)

@app.route('/webhook', methods=['POST'])
def webhook():
    payload = request.get_json(force=True)
    try:
        alert_queue.put_nowait(payload)
    except Exception:
        logger.error("告警队列已满，丢弃本次请求")
    return jsonify({'result': 'queued'}), 200  # 立即返回

# 后台消费线程
def consumer():
    while True:
        payload = alert_queue.get()
        try:
            process_alerts(payload)
        except Exception as e:
            logger.error(f"处理告警失败: {e}")

threading.Thread(target=consumer, daemon=True).start()
```

### 大量告警时钉钉触发限流

钉钉自定义机器人有频率限制：每分钟最多 20 条消息。告警风暴时（比如网络抖动导致几十个节点同时告警），容易触发 429。

解法：利用 Alertmanager 的 `group_by` 和 `group_wait` 合并同类告警，Webhook 侧也要对合并后的多条告警拼接成一条消息发送，而不是逐条发送。

### 去重状态在 Pod 重启后丢失

当前的内存去重在 Pod 重启后会清零，可能导致已发送的告警在重启后重复推送。生产环境建议用 Redis 存储去重状态：

```python
import redis

class RedisAlertDedup:
    def __init__(self, window_seconds=300):
        self.redis = redis.Redis(host='redis', port=6379, decode_responses=True)
        self.window = window_seconds
        self.prefix = "alert:dedup:"

    def is_duplicate(self, fingerprint: str) -> bool:
        return self.redis.exists(f"{self.prefix}{fingerprint}") == 1

    def mark_sent(self, fingerprint: str):
        self.redis.setex(f"{self.prefix}{fingerprint}", self.window, "1")
```
