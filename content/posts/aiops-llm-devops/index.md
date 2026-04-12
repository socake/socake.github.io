---
title: "大模型赋能运维：LLM 在故障排查和自动化中的实际应用"
date: 2026-01-31T12:06:00+08:00
draft: false
tags: ["AI", "大模型", "LLM", "运维", "AIOPS", "Claude"]
categories: ["AIOPS"]
description: "从告警摘要、日志分析到运维助手和每日简报，记录大模型在实际运维工作中的落地场景，以及踩过的坑和边界认知。"
summary: "LLM 不能替代运维工程师，但确实能把重复性、低价值的工作自动化掉。本文分享我在实际工作中用 Claude 落地的几个场景。"
toc: true
math: false
diagram: false
series: ["AI 工程化实战"]
keywords: ["AIOPS", "LLM", "Claude", "大模型", "运维自动化", "告警摘要", "日志分析", "Anthropic SDK"]
params:
  reading_time: true
---

我开始在运维工作里用大模型大概是一年半前，从最开始的「让它帮我写脚本」到现在构建了几个真正在跑的自动化流程，对这件事的认知变化挺大的。本文不谈概念，只讲我实际在用的东西，以及这个过程里踩过的坑。

## AIOPS 的现实与幻想

先说结论：**LLM 不能替代运维工程师，但能显著放大单个工程师的效率。**

有几件事 LLM 目前确实做不了或做不好：
- 自主执行有风险的操作（删库、扩缩容）而不需要人工确认
- 理解你们公司特有的业务上下文（除非你把上下文喂给它）
- 保证 100% 准确率（它会幻觉，生成的命令可能有 bug）

但有些事 LLM 做起来远比人工高效：
- 把大量原始信息（日志、告警、监控数据）压缩成人可读的摘要
- 根据描述生成初稿（K8s YAML、Shell 脚本、Python 代码）
- 解释错误信息和提供排查方向

这个认知很重要。把 LLM 当「助手」而不是「替代者」，落地效果会好很多。

---

## 实际落地场景

### 场景一：告警事件智能摘要

我们用 Alertmanager 管理告警，之前每次告警风暴来了，Slack/钉钉里刷几十条重复告警，根本没法快速判断核心问题是什么。

现在的方案：Alertmanager Webhook → Lambda/Serverless 函数 → LLM 生成摘要 → 推送钉钉。

```python
import anthropic
import json
from typing import Any

client = anthropic.Anthropic()

def summarize_alerts(alerts: list[dict]) -> str:
    """将多条告警转化为可读的摘要和处理建议"""
    
    # 整理告警信息
    alert_text = ""
    for alert in alerts:
        alert_text += f"""
告警名称: {alert.get('labels', {}).get('alertname', 'Unknown')}
严重程度: {alert.get('labels', {}).get('severity', 'unknown')}
服务: {alert.get('labels', {}).get('service', 'unknown')}
命名空间: {alert.get('labels', {}).get('namespace', 'unknown')}
摘要: {alert.get('annotations', {}).get('summary', '')}
详情: {alert.get('annotations', {}).get('description', '')}
触发时间: {alert.get('startsAt', '')}
---
"""
    
    message = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1024,
        messages=[
            {
                "role": "user",
                "content": f"""以下是 Kubernetes 集群当前触发的告警，请帮我：
1. 分析核心问题（可能有关联告警，找出根因）
2. 评估影响范围和严重程度
3. 给出优先处理顺序和初步排查建议

告警信息：
{alert_text}

请用简洁的中文回复，格式：
**核心问题**：...
**影响范围**：...
**处理建议**：
1. ...
2. ...
"""
            }
        ]
    )
    
    return message.content[0].text


def alertmanager_webhook_handler(event: dict, context: Any) -> dict:
    """Alertmanager Webhook 处理函数"""
    body = json.loads(event.get('body', '{}'))
    alerts = body.get('alerts', [])
    
    if not alerts:
        return {"statusCode": 200}
    
    # 只处理 firing 状态的告警
    firing_alerts = [a for a in alerts if a.get('status') == 'firing']
    if not firing_alerts:
        return {"statusCode": 200}
    
    summary = summarize_alerts(firing_alerts)
    
    # 推送到钉钉（简化示例）
    send_dingtalk(
        title=f"[告警] {len(firing_alerts)} 条告警需要处理",
        content=summary
    )
    
    return {"statusCode": 200}
```

这个方案上线后最直观的感受是：同样的告警量，oncall 工程师能更快判断是否需要介入，以及从哪里开始查。

### 场景二：日志异常分析

遇到线上报错，以前要先看日志、搜文档、翻 StackOverflow，整个过程花 20-30 分钟很正常。现在直接把错误日志丢给 Claude，通常 30 秒能得到一个有效的排查方向。

```python
def analyze_error_logs(logs: str, service_context: str = "") -> str:
    """分析错误日志，提取关键信息和可能的原因"""
    
    prompt_parts = [
        "以下是一段服务错误日志，请帮我：",
        "1. 提取关键错误信息（去掉重复和无关内容）",
        "2. 分析可能的根因",
        "3. 给出下一步排查建议",
    ]
    
    if service_context:
        prompt_parts.append(f"\n服务背景：{service_context}")
    
    prompt_parts.append(f"\n日志内容：\n```\n{logs}\n```")
    
    message = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=2048,
        messages=[{"role": "user", "content": "\n".join(prompt_parts)}]
    )
    
    return message.content[0].text
```

实际使用时，我会配合 kubectl 命令做一个简单的封装：

```bash
#!/bin/bash
# log-analyze.sh - 快速分析 Pod 错误日志

NAMESPACE=${1:-default}
DEPLOYMENT=${2}
LINES=${3:-100}

if [ -z "$DEPLOYMENT" ]; then
    echo "Usage: $0 <namespace> <deployment> [lines]"
    exit 1
fi

echo "获取 $NAMESPACE/$DEPLOYMENT 最近 $LINES 行日志..."
LOGS=$(kubectl logs -n "$NAMESPACE" deploy/"$DEPLOYMENT" \
    --tail="$LINES" 2>&1 | grep -i -E "error|exception|fatal|panic")

if [ -z "$LOGS" ]; then
    echo "没有发现错误日志"
    exit 0
fi

echo "=== 错误日志 ==="
echo "$LOGS"
echo ""
echo "=== AI 分析 ==="

python3 -c "
import sys
sys.path.insert(0, '/opt/ops-tools')
from log_analyzer import analyze_error_logs
logs = '''$LOGS'''
print(analyze_error_logs(logs, service_context='$NAMESPACE/$DEPLOYMENT'))
"
```

### 场景三：K8s YAML 配置生成

这个是使用频率最高的场景。用自然语言描述需求，直接生成可用的 YAML，然后 review 一遍再 apply。

实际效果：一个 Deployment + Service + HPA + PDB 的组合，手写大概需要 15-20 分钟（还容易漏字段），描述需求让 Claude 生成只需要 2-3 分钟。

在 Claude Code 里直接问就行，不需要额外写代码。有几个使用技巧：
- 要明确说明生产/测试/开发环境，三者的资源配置差很多
- 把公司内部的命名规范、注解规则也告诉它，不然需要手动修改很多地方
- 生成后一定要 review，特别是 resources、probe 的参数，LLM 给的默认值不一定适合你的业务

### 场景四：运维脚本辅助编写

Claude Code 的实际使用体验比我预期的好很多。对于运维脚本，我的使用模式通常是：

1. 描述需求（比如「写一个批量检查所有 namespace 的 PVC 使用率的脚本，超过 80% 的发告警」）
2. Claude 生成初版
3. 我 review 并指出需要调整的地方（错误处理不够、格式不对、逻辑有 bug）
4. 迭代 1-2 轮

这个过程比从零写快 3-5 倍，前提是你对脚本逻辑有清晰的判断，能快速发现 Claude 生成的问题。如果你不懂脚本在做什么，直接运行 AI 生成的脚本在生产环境是非常危险的。

---

## 构建简单的 LLM 运维助手

把上面的场景整合成一个命令行工具，在日常运维中方便调用。

```python
#!/usr/bin/env python3
"""
ops-assistant.py - 轻量级 LLM 运维助手
"""
import anthropic
import argparse
import subprocess
import sys

client = anthropic.Anthropic()

SYSTEM_PROMPT = """你是一个经验丰富的 Kubernetes 运维工程师。
回答要准确、简洁，给出可直接执行的命令而不是泛泛的建议。
遇到危险操作（删除、重启、扩缩容）时，必须先说明风险和确认步骤。"""


def chat(user_input: str, context: str = "") -> str:
    """单轮对话"""
    messages = []
    
    if context:
        messages.append({
            "role": "user",
            "content": f"背景信息：\n{context}\n\n问题：{user_input}"
        })
    else:
        messages.append({"role": "user", "content": user_input})
    
    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=messages
    )
    
    return response.content[0].text


def get_cluster_context() -> str:
    """获取当前集群基本状态作为上下文"""
    try:
        result = subprocess.run(
            ["kubectl", "get", "nodes", "-o", "wide"],
            capture_output=True, text=True, timeout=10
        )
        return f"当前集群节点状态：\n{result.stdout}"
    except Exception:
        return ""


def main():
    parser = argparse.ArgumentParser(description="LLM 运维助手")
    parser.add_argument("question", nargs="?", help="要问的问题")
    parser.add_argument("--context", "-c", help="额外上下文信息")
    parser.add_argument("--cluster", action="store_true", help="自动获取集群状态作为上下文")
    parser.add_argument("--interactive", "-i", action="store_true", help="交互模式")
    
    args = parser.parse_args()
    
    context = args.context or ""
    if args.cluster:
        context = get_cluster_context() + "\n" + context
    
    if args.interactive:
        print("进入交互模式，输入 'exit' 退出")
        print("-" * 50)
        history = []
        while True:
            try:
                user_input = input("你：").strip()
            except (EOFError, KeyboardInterrupt):
                break
            
            if user_input.lower() == 'exit':
                break
            if not user_input:
                continue
            
            response = chat(user_input, context)
            print(f"\nAssistant：{response}\n")
    
    elif args.question:
        response = chat(args.question, context)
        print(response)
    
    else:
        # 从 stdin 读取（方便 pipe）
        if not sys.stdin.isatty():
            stdin_content = sys.stdin.read().strip()
            if stdin_content:
                response = chat(stdin_content, context)
                print(response)
        else:
            parser.print_help()


if __name__ == "__main__":
    main()
```

使用示例：

```bash
# 直接问问题
python3 ops-assistant.py "K8s Pod 一直 CrashLoopBackOff 怎么排查"

# 把日志 pipe 进去分析
kubectl logs deploy/myapp --tail=50 | \
  python3 ops-assistant.py --context "production namespace 的 myapp 服务"

# 带集群上下文的交互模式
python3 ops-assistant.py --cluster --interactive
```

---

## 每日运维简报自动生成

这是我觉得落地最顺滑的一个场景，目前已经稳定运行了几个月。

流程：定时任务（每天早上 9 点）→ 采集昨日关键指标 → LLM 生成可读简报 → 推送钉钉群。

```python
import anthropic
from datetime import datetime, timedelta
import requests

client = anthropic.Anthropic()

def collect_daily_metrics() -> dict:
    """采集昨日关键运维指标"""
    yesterday = datetime.now() - timedelta(days=1)
    
    metrics = {}
    
    # 从 Prometheus 查询关键指标（简化示例）
    prometheus_base = "http://prometheus.monitoring.svc.cluster.local:9090"
    
    queries = {
        "avg_cpu_usage": 'avg(rate(container_cpu_usage_seconds_total[1d]))',
        "avg_memory_usage": 'avg(container_memory_working_set_bytes)',
        "total_errors": 'sum(increase(http_requests_total{status=~"5.."}[1d]))',
        "pod_restarts": 'sum(increase(kube_pod_container_status_restarts_total[1d]))',
    }
    
    for metric_name, query in queries.items():
        try:
            resp = requests.get(
                f"{prometheus_base}/api/v1/query",
                params={"query": query},
                timeout=10
            )
            data = resp.json()
            if data["status"] == "success" and data["data"]["result"]:
                metrics[metric_name] = float(data["data"]["result"][0]["value"][1])
        except Exception as e:
            metrics[metric_name] = f"获取失败: {e}"
    
    return metrics


def generate_daily_report(metrics: dict) -> str:
    """用 LLM 生成可读的日报"""
    
    metrics_text = "\n".join([f"- {k}: {v}" for k, v in metrics.items()])
    date_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    
    message = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1500,
        messages=[
            {
                "role": "user",
                "content": f"""请根据以下 {date_str} 的运维指标，生成一份简洁的日报。

指标数据：
{metrics_text}

要求：
1. 用中文，语气简洁专业
2. 指出需要关注的异常点（如有）
3. 给出简要的运行状况总结
4. 格式：标题 + 3-5 条要点 + 总结

如果数据正常，也要明确说明「整体运行平稳」。"""
            }
        ]
    )
    
    return message.content[0].text


def send_to_dingtalk(content: str, webhook_url: str):
    """推送到钉钉"""
    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": f"每日运维简报 {datetime.now().strftime('%m/%d')}",
            "text": content
        }
    }
    requests.post(webhook_url, json=payload, timeout=10)


if __name__ == "__main__":
    import os
    
    metrics = collect_daily_metrics()
    report = generate_daily_report(metrics)
    
    webhook = os.environ.get("DINGTALK_WEBHOOK")
    if webhook:
        send_to_dingtalk(report, webhook)
    else:
        print(report)
```

K8s CronJob 配置：

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: daily-ops-report
  namespace: ops-tools
spec:
  schedule: "0 9 * * 1-5"    # 工作日早上 9 点
  jobTemplate:
    spec:
      template:
        spec:
          restartPolicy: OnFailure
          containers:
            - name: report-generator
              image: registry.example.com/ops-tools:latest
              command: ["python3", "/app/daily_report.py"]
              env:
                - name: ANTHROPIC_API_KEY
                  valueFrom:
                    secretKeyRef:
                      name: llm-credentials
                      key: anthropic-api-key
                - name: DINGTALK_WEBHOOK
                  valueFrom:
                    secretKeyRef:
                      name: notification-webhooks
                      key: ops-dingtalk
```

---

## 注意事项：使用 LLM 的边界

### 敏感信息不要传给外部 LLM

这是最重要的安全红线。以下信息绝对不能发送给外部 LLM API：
- 数据库连接字符串、密码
- API Keys、Token
- 用户 PII 数据（姓名、邮箱、手机号）
- 内部 IP 地址、域名拓扑（可能暴露网络架构）

在把日志发给 LLM 分析之前，要先做脱敏处理：

```python
import re

def sanitize_logs(logs: str) -> str:
    """脱敏处理，移除敏感信息"""
    # 替换 IP 地址
    logs = re.sub(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', '[IP_REDACTED]', logs)
    
    # 替换可能的密码/token（常见格式）
    logs = re.sub(
        r'(password|passwd|token|secret|key|auth)["\s:=]+[^\s\'"&]+',
        r'\1=[REDACTED]',
        logs,
        flags=re.IGNORECASE
    )
    
    # 替换邮件地址
    logs = re.sub(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', 
                  '[EMAIL_REDACTED]', logs)
    
    return logs
```

### 结果验证是必要的

LLM 生成的命令和配置不一定正确，特别是涉及具体版本的 API 字段、公司内部系统的特殊配置。每次使用前要 review，不要无脑执行。

Claude Code 的一个好处是它会在执行命令前展示给你确认，这个交互设计让你自然地在关键步骤介入。

---

## 未来方向：Agent 自主执行的边界

现在大家都在讨论 AI Agent 自主执行运维任务。我的判断是：**Agent 可以自主执行的范围，应该严格限制在「可以快速无损恢复」的操作上。**

适合 Agent 自主执行：
- 查询类操作（kubectl get、日志查询、指标拉取）
- 重启 Pod（Deployment 会自动重新调度，风险可控）
- 扩容（增加副本数，不影响现有流量）
- 告警静默（不影响实际系统）

需要人工确认的：
- 缩容（可能影响可用性）
- 配置变更（Nacos、ConfigMap）
- 数据库操作（任何 DML/DDL）
- 删除操作（PVC 删了数据就没了）

真正做到让 AI Agent 在生产执行写操作，需要完善的审批链路、操作记录、自动回滚机制，目前这套体系还不成熟。与其做一个「能干但不可控」的 Agent，不如先做好「让工程师效率翻倍」这件事。

---

## 总结

用大模型辅助运维这一年多，最大的感受是：**把 LLM 嵌入到工作流而不是作为独立工具使用**，效果差很多。

告警摘要直接发到告警通知里、日志分析集成到排障命令行、YAML 生成通过 Claude Code 自然交互——这些都是把 LLM 嵌入到已有工作流的例子。每次打开一个独立的 Chat 窗口「问一下 AI」，上下文切换的成本会抵消掉很多效率收益。

另外，提示词质量很重要。「帮我分析这个问题」和「你是一个 Kubernetes 运维工程师，分析这段日志，给出根因和下一步排查步骤」，得到的回答质量差距很大。花时间打磨系统提示词，是最值得投入的部分。
