---
title: "MCP 协议实战：给 AI Agent 接上运维工具"
date: 2026-02-27T09:52:00+08:00
draft: false
tags: ["MCP", "AI Agent", "Claude", "运维", "自动化", "2026"]
categories: ["AIOPS"]
description: "Model Context Protocol 让 AI 能够标准化地调用外部工具。本文用 Python 实现一个运维 MCP Server，接入 kubectl、Prometheus、Loki，让 AI 直接查集群状态。"
summary: "Model Context Protocol 让 AI 能够标准化地调用外部工具。本文用 Python 实现一个运维 MCP Server，接入 kubectl、Prometheus、Loki，让 AI 直接查集群状态。"
toc: true
math: false
diagram: false
series: ["AI 工程化实战"]
keywords: ["MCP", "Model Context Protocol", "AI Agent", "运维自动化", "Claude", "kubectl"]
params:
  reading_time: true
---

## 从 "AI 给建议" 到 "AI 做操作"

用了一段时间的 AI 辅助运维之后，我发现有一道墙一直没突破——AI 给出分析结论之后，实际查数据、执行命令还是要人来做。

一个典型的流程是这样的：

1. 告警触发，我把错误信息贴给 Claude
2. Claude 说"可能是内存不足，建议查看 Pod 资源使用情况，命令是 `kubectl top pods -n xxx`"
3. 我去执行命令，把输出贴回来
4. Claude 继续分析
5. 循环 3-5 轮

这个模式有价值，但效率不高。每一轮都要人工搬运数据。

MCP（Model Context Protocol）解决的就是这个问题：让 AI 直接调用工具获取数据，而不是告诉你去执行什么命令。

## MCP 是什么

MCP 是 Anthropic 在 2024 年底提出的开放协议，目标是标准化 AI 模型与外部工具之间的交互方式。它定义了三类能力：

- **Resources**：AI 可以读取的数据源（文件、数据库查询结果、API 响应）
- **Tools**：AI 可以调用的操作（执行命令、发 HTTP 请求、写入数据）
- **Prompts**：可复用的提示词模板

从架构上看，MCP 是一个 Client-Server 模型：

```
Claude Desktop / Claude Code
        │
        │ MCP Protocol (JSON-RPC over stdio/SSE)
        │
   MCP Server（你写的）
        │
   kubectl / Prometheus / Loki / ...
```

AI 客户端（Claude Desktop、Claude Code 或任何支持 MCP 的应用）连接到 MCP Server，Server 暴露工具列表，AI 决定什么时候调用哪个工具。

## 为什么比直接调 API 更好

在 MCP 出现之前，给 AI 接工具通常有两种方式：

**方式一：在 prompt 里嵌 API 调用指令**，让 AI 生成调用代码，然后人工执行。麻烦且容易出错。

**方式二：用各家平台的 function calling**，比如 OpenAI 的 function calling、Claude 的 tool use。有效，但绑定特定平台，换个 AI 就要重写。

MCP 的优势在于：

- **标准化**：写一次 MCP Server，所有支持 MCP 的 AI 客户端都能用
- **工具复用**：社区里已经有大量现成的 MCP Server（GitHub、Slack、数据库、Docker 等）
- **安全隔离**：MCP Server 控制权限边界，AI 只能调用 Server 暴露的接口，不能直接访问底层系统
- **可审计**：所有工具调用都经过 Server 层，可以在这里加日志、限流、二次确认

## 实战：写一个运维 MCP Server

下面是一个完整的运维 MCP Server，暴露三个工具：查 Pod 状态、查 Prometheus 指标、搜索 Loki 日志。

### 依赖安装

```bash
pip install mcp httpx
```

MCP 官方 Python SDK 就叫 `mcp`，Anthropic 维护。

### 完整代码

```python
# ops_mcp_server.py
import asyncio
import subprocess
import json
from datetime import datetime, timedelta
from typing import Any

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

PROMETHEUS_URL = "http://prometheus.monitoring.svc.cluster.local:9090"
LOKI_URL = "http://loki.monitoring.svc.cluster.local:3100"

app = Server("ops-tools")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="kubectl_get_pods",
            description="查询 Kubernetes Pod 状态。返回指定 namespace 下所有 Pod 的运行状态、重启次数和年龄。",
            inputSchema={
                "type": "object",
                "properties": {
                    "namespace": {
                        "type": "string",
                        "description": "K8s namespace，例如 production、staging",
                        "default": "default"
                    },
                    "label_selector": {
                        "type": "string",
                        "description": "Label selector 过滤，例如 app=api-server",
                        "default": ""
                    }
                },
                "required": []
            }
        ),
        Tool(
            name="query_prometheus",
            description="查询 Prometheus 监控指标。使用 PromQL 语法，返回当前时刻的指标值。",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "PromQL 查询语句，例如 rate(http_requests_total[5m])"
                    },
                    "time_range": {
                        "type": "string",
                        "description": "时间范围，例如 5m、1h、24h，用于 range query",
                        "default": ""
                    }
                },
                "required": ["query"]
            }
        ),
        Tool(
            name="search_logs",
            description="在 Loki 中搜索日志。支持 LogQL 语法，返回最近的日志行。",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "LogQL 查询，例如 {namespace=\"production\", app=\"api\"} |= \"error\""
                    },
                    "limit": {
                        "type": "integer",
                        "description": "返回日志行数，默认 50",
                        "default": 50
                    },
                    "since": {
                        "type": "string",
                        "description": "查询最近多久的日志，例如 10m、1h",
                        "default": "10m"
                    }
                },
                "required": ["query"]
            }
        )
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    if name == "kubectl_get_pods":
        return await handle_kubectl_get_pods(arguments)
    elif name == "query_prometheus":
        return await handle_query_prometheus(arguments)
    elif name == "search_logs":
        return await handle_search_logs(arguments)
    else:
        return [TextContent(type="text", text=f"未知工具: {name}")]


async def handle_kubectl_get_pods(args: dict) -> list[TextContent]:
    namespace = args.get("namespace", "default")
    label_selector = args.get("label_selector", "")

    cmd = ["kubectl", "get", "pods", "-n", namespace, "-o", "wide"]
    if label_selector:
        cmd.extend(["-l", label_selector])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return [TextContent(type="text", text=f"kubectl 执行失败: {result.stderr}")]

        # 同时获取 describe 中的 Events（有助于诊断问题）
        pods_output = result.stdout

        # 查找非 Running 状态的 Pod
        problem_pods = []
        for line in pods_output.split("\n")[1:]:  # 跳过 header
            if line and not line.startswith("NAME"):
                parts = line.split()
                if len(parts) >= 3 and parts[2] not in ("Running", "Completed"):
                    problem_pods.append(parts[0])

        summary = f"Namespace: {namespace}\n\n{pods_output}"
        if problem_pods:
            summary += f"\n\n⚠️ 异常 Pod: {', '.join(problem_pods)}"

        return [TextContent(type="text", text=summary)]
    except subprocess.TimeoutExpired:
        return [TextContent(type="text", text="kubectl 命令超时（30s）")]
    except Exception as e:
        return [TextContent(type="text", text=f"执行出错: {str(e)}")]


async def handle_query_prometheus(args: dict) -> list[TextContent]:
    query = args["query"]
    time_range = args.get("time_range", "")

    async with httpx.AsyncClient() as client:
        try:
            if time_range:
                # Range query
                end = datetime.utcnow()
                # 解析时间范围
                if time_range.endswith("m"):
                    delta = timedelta(minutes=int(time_range[:-1]))
                elif time_range.endswith("h"):
                    delta = timedelta(hours=int(time_range[:-1]))
                else:
                    delta = timedelta(hours=1)
                start = end - delta

                resp = await client.get(
                    f"{PROMETHEUS_URL}/api/v1/query_range",
                    params={
                        "query": query,
                        "start": start.timestamp(),
                        "end": end.timestamp(),
                        "step": "60"
                    },
                    timeout=15.0
                )
            else:
                # Instant query
                resp = await client.get(
                    f"{PROMETHEUS_URL}/api/v1/query",
                    params={"query": query},
                    timeout=15.0
                )

            resp.raise_for_status()
            data = resp.json()

            if data["status"] != "success":
                return [TextContent(type="text", text=f"Prometheus 查询失败: {data.get('error', '未知错误')}")]

            result = data["data"]["result"]
            if not result:
                return [TextContent(type="text", text=f"查询无结果: {query}")]

            # 格式化输出
            lines = [f"查询: {query}\n"]
            for item in result[:20]:  # 最多显示 20 条
                metric = item["metric"]
                metric_str = ", ".join(f'{k}="{v}"' for k, v in metric.items() if k != "__name__")

                if "value" in item:
                    lines.append(f"{metric_str}: {item['value'][1]}")
                elif "values" in item:
                    latest = item["values"][-1]
                    lines.append(f"{metric_str}: {latest[1]} (最新值)")

            return [TextContent(type="text", text="\n".join(lines))]

        except httpx.TimeoutException:
            return [TextContent(type="text", text="Prometheus 查询超时")]
        except Exception as e:
            return [TextContent(type="text", text=f"查询出错: {str(e)}")]


async def handle_search_logs(args: dict) -> list[TextContent]:
    query = args["query"]
    limit = args.get("limit", 50)
    since = args.get("since", "10m")

    # 解析 since 为 nanoseconds
    if since.endswith("m"):
        ns_ago = int(since[:-1]) * 60 * 1_000_000_000
    elif since.endswith("h"):
        ns_ago = int(since[:-1]) * 3600 * 1_000_000_000
    else:
        ns_ago = 600 * 1_000_000_000

    end_ns = int(datetime.utcnow().timestamp() * 1_000_000_000)
    start_ns = end_ns - ns_ago

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(
                f"{LOKI_URL}/loki/api/v1/query_range",
                params={
                    "query": query,
                    "start": str(start_ns),
                    "end": str(end_ns),
                    "limit": limit,
                    "direction": "backward"
                },
                timeout=15.0
            )
            resp.raise_for_status()
            data = resp.json()

            streams = data.get("data", {}).get("result", [])
            if not streams:
                return [TextContent(type="text", text=f"最近 {since} 内无匹配日志\n查询: {query}")]

            lines = [f"查询: {query}\n时间范围: 最近 {since}\n\n"]
            for stream in streams:
                labels = stream.get("stream", {})
                label_str = ", ".join(f'{k}={v}' for k, v in labels.items())
                lines.append(f"[{label_str}]")
                for ts, log_line in stream.get("values", []):
                    ts_dt = datetime.utcfromtimestamp(int(ts) / 1_000_000_000)
                    lines.append(f"  {ts_dt.strftime('%H:%M:%S')} {log_line}")
                lines.append("")

            return [TextContent(type="text", text="\n".join(lines))]

        except httpx.TimeoutException:
            return [TextContent(type="text", text="Loki 查询超时")]
        except Exception as e:
            return [TextContent(type="text", text=f"查询出错: {str(e)}")]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
```

### 接入 Claude Desktop

在 `~/.config/claude/claude_desktop_config.json` 里添加：

```json
{
  "mcpServers": {
    "ops-tools": {
      "command": "python",
      "args": ["/path/to/ops_mcp_server.py"],
      "env": {
        "KUBECONFIG": "/home/user/.kube/config"
      }
    }
  }
}
```

重启 Claude Desktop 后，在输入框右下角能看到工具图标，说明 MCP Server 连接成功。

## 实际效果演示

接入之后，一个真实的对话是这样的：

**我：** 最近 10 分钟 production namespace 有哪些 Pod 重启了？

**Claude：** （自动调用 `kubectl_get_pods`，namespace=production）
（自动调用 `search_logs`，query=`{namespace="production"} |= "OOMKilled" | = "Error"`，since=10m）

分析结果：
- `payment-service-7d8b-xk2p` 重启了 3 次，日志显示内存超限（OOMKilled）
- 当前内存使用 `1.8Gi / limit 2Gi`，建议将 limit 调整到 `3Gi` 或优化内存泄漏

**我：** 查一下它的 JVM 堆内存使用趋势

**Claude：** （自动调用 `query_prometheus`，query=`jvm_memory_used_bytes{pod=~"payment-service.*", area="heap"}`，time_range=1h）

过去 1 小时堆内存持续增长，从 800MB 增长到 1.7GB，没有 GC 回收迹象，典型内存泄漏特征。

整个过程中我没有执行任何命令，AI 自己拿到了需要的数据。

## 权限控制与安全设计

MCP Server 是工具的权限边界，必须认真设计：

**只读原则。** 诊断类工具全部设计为只读，不允许 AI 直接执行 `kubectl delete`、`kubectl apply` 等写操作。如果需要，可以单独暴露一个 `kubectl_apply_dry_run` 工具，先 dry-run 再让人确认。

**二次确认模式。** 对于有副作用的操作，在 Tool 的 description 里明确说明，并在 Server 层加确认逻辑：

```python
async def handle_restart_pod(args: dict) -> list[TextContent]:
    pod_name = args["pod_name"]
    namespace = args["namespace"]
    confirm = args.get("confirm", False)

    if not confirm:
        return [TextContent(
            type="text",
            text=f"将要重启 {namespace}/{pod_name}，如确认请用 confirm=true 再次调用"
        )]
    # 执行实际操作...
```

**环境隔离。** 生产集群和测试集群用不同的 MCP Server 实例，分别配置不同的 kubeconfig。AI 无法跨环境操作。

**调用日志。** 在 `call_tool` 入口统一记录所有调用，谁在什么时间调用了什么工具，参数是什么：

```python
import logging
logging.basicConfig(filename="/var/log/mcp-ops.log", level=logging.INFO)

@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    logging.info(f"tool_call name={name} args={json.dumps(arguments)}")
    # ...
```

MCP 目前的生态发展很快，2026 年已经有大量开源 MCP Server 覆盖常见工具（GitHub、Jira、PagerDuty、Datadog 等）。对于运维团队来说，把自己的内部工具包装成 MCP Server，是让 AI 真正有用而不只是"聊天机器人"的关键一步。
