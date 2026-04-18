---
title: "AI Agent 设计模式：从单步到复杂工作流"
date: 2026-01-29T09:17:00+08:00
draft: false
tags: ["AI Agent", "设计模式", "ReAct", "Multi-Agent", "运维自动化"]
categories: ["AI应用"]
description: "Agent核心模式、Tool设计、Multi-Agent协作、运维Agent实战案例"
summary: "Agent不是更智能的ChatGPT调用，它是一个能自主规划和执行多步骤任务的循环系统。本文拆解ReAct推理循环、Tool调用设计原则、Multi-Agent协作模式、Human-in-the-loop设计，以及告警分析Agent和巡检Agent的实战实现。"
toc: true
math: false
diagram: false
series: ["AI 工程化实战"]
keywords: ["AI Agent", "ReAct", "Tool调用", "Multi-Agent", "告警分析", "运维自动化"]
params:
  reading_time: true
---

很多工程师对"Agent"的理解是"更聪明的ChatGPT"，这个理解偏差会导致对Agent能力的误判——期待过高，或者根本没用到它的核心价值。

Agent的本质是一个**循环执行的系统**：LLM作为大脑，规划下一步行动；工具函数作为手脚，执行具体操作；观察结果反馈给LLM，继续规划，直到任务完成。下面从运维场景出发把这套机制和常见陷阱讲清楚。

---

## Agent vs 普通LLM调用

先弄清楚区别，才知道什么时候该用Agent。

**普通LLM调用**：
```
用户输入 → LLM → 输出（一次性）
```

**Agent**：
```
用户输入 → LLM规划 → 执行工具 → 观察结果 → LLM再规划 → 执行工具 → ... → 输出
```

适合用Agent的场景特征：
- 任务需要多步骤才能完成
- 每一步的输入依赖上一步的结果
- 需要与外部系统交互（查数据、执行命令、调API）
- 任务路径事先不确定（要根据中间结果决定下一步）

不适合用Agent的场景：
- 单次问答（RAG问答、文本生成）
- 任务流程固定（直接用工作流即可）
- 对延迟要求极高（Agent的多轮循环会增加延迟）

---

## ReAct推理循环

ReAct（Reasoning + Acting）是目前最主流的Agent推理框架，理解它是理解所有Agent框架的基础。

### 核心循环

每一步，LLM接收到这样的输入：

```
系统提示词（定义Agent角色和可用工具）

历史行动记录：
Thought: [之前的思考]
Action: [之前执行的工具]
Observation: [工具返回的结果]

当前任务：[用户的原始输入]
```

LLM输出：

```
Thought: [分析当前情况，决定下一步]
Action: [要调用的工具名称]
Action Input: [工具的输入参数]
```

系统解析这个输出，执行工具，把结果作为`Observation`加入历史，然后再次调用LLM，直到LLM输出`Final Answer`。

### 完整示例

任务：检查服务器的CPU使用率，如果超过80%，找出占用最高的进程并发告警。

```
Thought: 我需要先检查CPU使用率。我会用get_cpu_usage工具。
Action: get_cpu_usage
Action Input: {"host": "prod-web-01"}

Observation: {"cpu_usage": 92.3, "load_avg_1m": 8.5, "load_avg_5m": 7.2}

Thought: CPU使用率是92.3%，超过了80%的阈值。我需要找出占用CPU最高的进程。
Action: get_top_processes
Action Input: {"host": "prod-web-01", "limit": 5}

Observation: [
  {"pid": 12345, "name": "java", "cpu": 45.2, "mem": 2048},
  {"pid": 12346, "name": "python", "cpu": 30.1, "mem": 512},
  ...
]

Thought: 找到了，主要是java进程（PID 12345）占用了45.2%的CPU。
现在需要发送告警，包含：主机名、CPU使用率、主要进程信息。
Action: send_alert
Action Input: {
  "severity": "warning",
  "title": "CPU使用率过高",
  "message": "主机 prod-web-01 CPU使用率 92.3%，主要进程：java(PID:12345, CPU:45.2%)",
  "channel": "ops-alerts"
}

Observation: {"status": "sent", "alert_id": "ALT-20240412-001"}

Thought: 告警已发送成功。任务完成。
Final Answer: prod-web-01 的 CPU 使用率为 92.3%（超过阈值 80%），主要消耗来自 java 进程（PID: 12345，占用 45.2%）。已发送告警到 ops-alerts 频道（告警ID: ALT-20240412-001）。
```

---

## Tool调用设计

工具是Agent的行动能力，设计好工具是Agent工程里最重要的环节。

### 工具粒度原则

**原则1：单一职责**

每个工具只做一件事。不要设计"超级工具"：

```python
# 不好：做了太多事，LLM难以决定何时调用
@tool
def manage_k8s_deployment(action: str, name: str, namespace: str, ...):
    """管理K8s Deployment：可以查看、更新、重启、回滚"""
    ...

# 好：每个操作独立
@tool
def get_deployment_status(name: str, namespace: str) -> dict:
    """获取Deployment的当前状态、副本数、镜像版本"""
    ...

@tool
def scale_deployment(name: str, namespace: str, replicas: int) -> dict:
    """调整Deployment的副本数"""
    ...

@tool
def rollback_deployment(name: str, namespace: str, revision: int = 0) -> dict:
    """回滚Deployment到指定版本，revision=0表示回滚到上一个版本"""
    ...
```

**原则2：工具描述要精确**

LLM靠工具的描述（docstring）决定什么时候调用哪个工具。描述含糊会导致工具被错误调用：

```python
# 不好：太模糊
@tool
def check_k8s(name: str, namespace: str) -> dict:
    """检查K8s资源"""
    ...

# 好：明确说明用途、参数、返回值
@tool
def get_pod_logs(
    pod_name: str,
    namespace: str,
    container: str = "",
    previous: bool = False,
    tail_lines: int = 100
) -> str:
    """
    获取Pod的容器日志。
    
    参数：
    - pod_name: Pod名称（完整名称，不是Deployment名）
    - namespace: 命名空间
    - container: 容器名，如果Pod只有一个容器可以不填
    - previous: True表示获取上次崩溃前的日志（用于排查CrashLoopBackOff）
    - tail_lines: 获取最后N行，默认100，最大1000
    
    返回：日志文本，如果Pod不存在返回错误信息
    
    注意：不要用这个工具获取运行中服务的实时流式日志，只用于事后分析
    """
    ...
```

**原则3：工具要有防御性**

工具函数是Agent直接作用于真实系统的接口，需要比普通代码更健壮：

```python
@tool
def execute_kubectl_command(command: str, dry_run: bool = True) -> dict:
    """
    执行kubectl命令。
    警告：dry_run=False时会真实修改集群，谨慎使用。
    建议：先用dry_run=True验证命令正确性。
    """
    import subprocess
    import shlex
    
    # 安全检查：不允许某些危险操作
    dangerous_patterns = ['delete', 'drain', 'cordon', 'taint']
    if any(p in command.lower() for p in dangerous_patterns) and not dry_run:
        return {
            "success": False,
            "error": f"危险操作需要明确确认。请先用dry_run=True验证，然后由人工确认后执行。"
        }
    
    # 在dry_run模式下添加--dry-run=client
    if dry_run and 'apply' in command or 'create' in command:
        command = command + ' --dry-run=client'
    
    try:
        result = subprocess.run(
            shlex.split(f"kubectl {command}"),
            capture_output=True,
            text=True,
            timeout=30
        )
        return {
            "success": result.returncode == 0,
            "stdout": result.stdout[:5000],  # 限制输出长度
            "stderr": result.stderr[:1000],
            "dry_run": dry_run
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "命令执行超时（30秒）"}
    except Exception as e:
        return {"success": False, "error": str(e)}
```

### 错误处理

工具返回的错误信息对Agent的后续决策至关重要。错误信息要有足够信息量：

```python
# 不好：Agent不知道该怎么办
return {"error": "failed"}

# 好：Agent能根据错误信息调整策略
return {
    "success": False,
    "error_type": "not_found",
    "error": f"Pod '{pod_name}' 在 namespace '{namespace}' 中不存在",
    "suggestion": "请用 list_pods(namespace='{namespace}') 查看可用的Pod列表"
}
```

---

## Multi-Agent协作模式

单个Agent处理复杂任务时可能力不从心：上下文太长（工具调用历史很占token）、任务需要并行处理、不同子任务需要不同的专业角色。

Multi-Agent把大任务分解给多个专门的Agent处理。

### Orchestrator-Worker模式

最常见的模式：一个Orchestrator负责任务分解和协调，多个Worker负责执行具体子任务。

```python
from langchain.agents import AgentExecutor, create_react_agent
from langchain_core.tools import tool

class OrchestratorAgent:
    """负责任务分解和协调"""
    
    def __init__(self, llm, worker_agents):
        self.llm = llm
        self.workers = worker_agents
        # Orchestrator的工具是调用各个Worker
        self.tools = [self._create_worker_tool(name, agent) 
                      for name, agent in worker_agents.items()]
    
    def _create_worker_tool(self, name: str, agent: AgentExecutor):
        @tool(name=f"assign_to_{name}")
        def worker_tool(task: str) -> str:
            f"""将子任务分配给{name}Agent处理。
            适用场景：{agent.agent.llm_chain.prompt.template[:200]}
            """
            result = agent.invoke({"input": task})
            return result["output"]
        return worker_tool
    
    def run(self, task: str) -> str:
        agent = create_react_agent(self.llm, self.tools, orchestrator_prompt)
        executor = AgentExecutor(agent=agent, tools=self.tools, verbose=True)
        result = executor.invoke({"input": task})
        return result["output"]

# 使用示例
log_analysis_agent = create_log_analysis_agent(llm, log_tools)
metric_analysis_agent = create_metric_analysis_agent(llm, metric_tools)
alert_agent = create_alert_agent(llm, alert_tools)

orchestrator = OrchestratorAgent(
    llm=llm,
    worker_agents={
        "log_analyst": log_analysis_agent,
        "metric_analyst": metric_analysis_agent,
        "alert_sender": alert_agent,
    }
)

result = orchestrator.run("prod-api服务出现5xx错误激增，请分析原因并发送告警")
```

### 并行执行模式

对于相互独立的子任务，并行执行可以显著减少总耗时：

```python
import asyncio
from concurrent.futures import ThreadPoolExecutor

async def parallel_analysis(service_name: str):
    """并行执行多个独立分析任务"""
    
    loop = asyncio.get_event_loop()
    executor = ThreadPoolExecutor(max_workers=3)
    
    # 三个独立任务并行执行
    tasks = [
        loop.run_in_executor(executor, analyze_logs, service_name),
        loop.run_in_executor(executor, analyze_metrics, service_name),
        loop.run_in_executor(executor, check_dependencies, service_name),
    ]
    
    log_result, metric_result, dep_result = await asyncio.gather(*tasks)
    
    # 综合分析
    summary_agent = create_summary_agent(llm)
    final_result = summary_agent.run(
        f"服务：{service_name}\n"
        f"日志分析：{log_result}\n"
        f"指标分析：{metric_result}\n"
        f"依赖检查：{dep_result}\n"
        "请综合以上信息给出根因分析和处置建议"
    )
    return final_result
```

---

## Human-in-the-loop设计

完全自动化的Agent有时候不是最好的选择，特别是涉及生产环境操作时。Human-in-the-loop在关键步骤加入人工审批。

### 需要人工审批的场景

- 向生产环境写入/修改数据
- 重启生产服务
- 变更网络/防火墙规则
- 删除任何资源
- 超过一定金额的费用操作

### 实现方式

**方式1：工具层拦截**

```python
class HumanApprovalRequired(Exception):
    def __init__(self, action: str, details: dict):
        self.action = action
        self.details = details
        super().__init__(f"此操作需要人工审批：{action}")

@tool
def restart_production_service(service_name: str, namespace: str) -> dict:
    """重启生产服务（需要人工审批）"""
    
    # 发送审批请求
    approval_id = send_approval_request(
        action="restart_service",
        details={"service": service_name, "namespace": namespace},
        timeout_seconds=300  # 5分钟内审批
    )
    
    # 等待审批结果
    approved = wait_for_approval(approval_id, timeout=300)
    
    if not approved:
        raise HumanApprovalRequired(
            action="restart_service",
            details={"service": service_name, "approved": False}
        )
    
    # 执行实际操作
    result = _do_restart_service(service_name, namespace)
    return {"success": True, "approval_id": approval_id, **result}
```

**方式2：Plan-then-Execute模式**

Agent先规划整个执行方案，人工审批后再执行：

```python
def run_with_approval(task: str, llm, tools) -> str:
    # 第一阶段：只规划，不执行
    planner_prompt = """
    分析任务并给出执行计划，只描述步骤，不要实际执行任何操作。
    对每个步骤标注风险等级（低/中/高）。
    """
    plan = planner_agent.run(task)
    
    # 输出计划，等待人工审批
    print("=== 执行计划 ===")
    print(plan)
    
    # 这里可以集成Slack/钉钉审批
    approval = input("确认执行以上计划？(yes/no): ")
    
    if approval.lower() != 'yes':
        return "操作已取消"
    
    # 第二阶段：执行
    executor_prompt = "按照以下计划执行操作：\n" + plan
    return executor_agent.run(executor_prompt)
```

---

## 内存与状态管理

Agent的"记忆"影响多轮对话和长任务的效果。

### 对话历史（Short-term Memory）

默认的ConversationBufferMemory会保留所有历史，长任务后context会爆炸。用滑动窗口或摘要：

```python
from langchain.memory import ConversationSummaryBufferMemory

memory = ConversationSummaryBufferMemory(
    llm=llm,
    max_token_limit=2000,  # 超过这个就压缩
    return_messages=True
)
```

### 任务状态（Working Memory）

对于多步骤任务，用结构化状态跟踪进度：

```python
from dataclasses import dataclass, field
from typing import List, Dict, Any
from enum import Enum

class TaskStatus(Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"

@dataclass
class AgentState:
    task_id: str
    original_task: str
    status: TaskStatus = TaskStatus.PENDING
    completed_steps: List[str] = field(default_factory=list)
    findings: Dict[str, Any] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    
    def add_finding(self, key: str, value: Any):
        self.findings[key] = value
    
    def to_context(self) -> str:
        """生成给LLM的状态上下文"""
        return f"""
当前任务：{self.original_task}
已完成步骤：{', '.join(self.completed_steps) if self.completed_steps else '无'}
发现：{self.findings}
错误：{self.errors if self.errors else '无'}
"""
```

### 外部记忆（Long-term Memory）

把重要的发现和解决方案存入知识库，下次遇到类似问题可以检索：

```python
@tool
def save_to_knowledge_base(title: str, problem: str, solution: str, tags: list) -> dict:
    """
    将本次解决的问题和方案保存到知识库，供以后参考。
    在每次成功解决问题后调用。
    """
    entry = {
        "title": title,
        "problem": problem,
        "solution": solution,
        "tags": tags,
        "timestamp": datetime.now().isoformat()
    }
    # 存入向量数据库
    vector_store.add_texts(
        texts=[f"{title}\n{problem}\n{solution}"],
        metadatas=[entry]
    )
    return {"saved": True, "id": entry["timestamp"]}
```

---

## 运维Agent实战

### 告警分析Agent

接收告警，自动分析根因并给出处置建议：

```python
from langchain_anthropic import ChatAnthropic
from langchain.agents import create_react_agent, AgentExecutor
from langchain_core.tools import tool
import subprocess

llm = ChatAnthropic(model="claude-3-5-sonnet-20241022")

@tool
def get_pod_status(namespace: str, label_selector: str = "") -> str:
    """获取指定namespace的Pod状态列表"""
    cmd = f"kubectl get pods -n {namespace}"
    if label_selector:
        cmd += f" -l {label_selector}"
    cmd += " -o wide"
    result = subprocess.run(cmd.split(), capture_output=True, text=True, timeout=15)
    return result.stdout or result.stderr

@tool
def get_recent_events(namespace: str, pod_name: str = "") -> str:
    """获取最近的K8s事件，按时间倒序"""
    if pod_name:
        cmd = f"kubectl get events -n {namespace} --field-selector involvedObject.name={pod_name} --sort-by=.lastTimestamp"
    else:
        cmd = f"kubectl get events -n {namespace} --sort-by=.lastTimestamp"
    result = subprocess.run(cmd.split(), capture_output=True, text=True, timeout=15)
    return result.stdout[-3000:] if result.stdout else result.stderr  # 只返回最近的

@tool
def get_pod_logs(namespace: str, pod_name: str, tail: int = 100, previous: bool = False) -> str:
    """获取Pod日志"""
    cmd = f"kubectl logs -n {namespace} {pod_name} --tail={tail}"
    if previous:
        cmd += " --previous"
    result = subprocess.run(cmd.split(), capture_output=True, text=True, timeout=30)
    return result.stdout[-4000:] if result.stdout else result.stderr

@tool
def get_deployment_info(namespace: str, deployment_name: str) -> str:
    """获取Deployment详情，包括replica状态和最近的更新历史"""
    result = subprocess.run(
        f"kubectl describe deployment {deployment_name} -n {namespace}".split(),
        capture_output=True, text=True, timeout=15
    )
    return result.stdout[:4000]

@tool
def notify_slack(channel: str, message: str, severity: str = "warning") -> dict:
    """发送Slack通知"""
    # 实际接入Slack Webhook
    webhook_url = "https://hooks.slack.com/services/YOUR/WEBHOOK/URL"
    color_map = {"critical": "#FF0000", "warning": "#FFA500", "info": "#36A64F"}
    payload = {
        "channel": channel,
        "attachments": [{
            "color": color_map.get(severity, "#FFA500"),
            "text": message,
            "footer": "AlertAnalysisAgent"
        }]
    }
    # requests.post(webhook_url, json=payload)
    return {"sent": True, "channel": channel}

# Agent系统提示词
ALERT_AGENT_PROMPT = """你是一个K8s运维专家Agent，负责分析告警并给出处置建议。

可用工具：
{tools}

工具名称列表：{tool_names}

工作流程：
1. 分析告警信息，理解告警的含义和可能的影响
2. 使用工具收集更多信息（Pod状态、事件、日志）
3. 根据收集到的信息分析根因
4. 给出明确的处置建议
5. 如果告警严重，发送Slack通知

重要规则：
- 不要执行任何修改操作，只做分析
- 如果信息不足，继续收集，不要猜测
- 最终给出：根因（一句话）、影响范围、处置建议（具体步骤）

格式：
{{
  "root_cause": "...",
  "impact": "...",
  "recommended_actions": ["步骤1", "步骤2", ...],
  "urgency": "critical/high/medium/low"
}}

使用这个格式：

Question: {{input}}
Thought: {{agent_scratchpad}}
"""

tools = [get_pod_status, get_recent_events, get_pod_logs, 
         get_deployment_info, notify_slack]

from langchain import hub
from langchain.prompts import PromptTemplate

prompt = PromptTemplate.from_template(ALERT_AGENT_PROMPT)
agent = create_react_agent(llm, tools, prompt)
alert_analyzer = AgentExecutor(
    agent=agent, 
    tools=tools, 
    verbose=True,
    max_iterations=10,
    handle_parsing_errors=True
)

# 使用：接到告警时调用
def handle_alert(alert: dict):
    alert_text = f"""
    告警名称：{alert['alertname']}
    严重级别：{alert['severity']}
    命名空间：{alert.get('namespace', 'unknown')}
    Pod：{alert.get('pod', 'unknown')}
    描述：{alert.get('summary', '')}
    触发时间：{alert.get('startsAt', '')}
    """
    
    result = alert_analyzer.invoke({"input": alert_text})
    return result["output"]
```

### 巡检Agent

定时巡检集群健康状态，输出结构化报告：

```python
INSPECTION_PROMPT = """
你是一个K8s集群巡检Agent。

你的任务是系统性地检查集群健康状态，按照以下顺序检查：
1. 节点状态（有无NotReady节点）
2. 系统命名空间的Pod状态（kube-system, monitoring）
3. 业务命名空间的异常Pod（CrashLoopBackOff, OOMKilled）
4. 最近1小时的Warning级别事件
5. PVC使用状态

每项检查完成后记录结果，最终生成巡检报告。

报告格式：
## 巡检时间
## 总体健康度（正常/需关注/异常）
## 各项检查结果
## 需要跟进的问题
## 建议操作
"""

# 巡检任务通常通过cron触发
def run_daily_inspection():
    agent_executor = AgentExecutor(
        agent=create_react_agent(llm, inspection_tools, inspection_prompt),
        tools=inspection_tools,
        verbose=False,
        max_iterations=20
    )
    
    result = agent_executor.invoke({
        "input": "执行今日集群巡检，检查所有生产namespace"
    })
    
    # 保存报告
    with open(f"/reports/inspection_{date.today()}.md", "w") as f:
        f.write(result["output"])
    
    # 发送到钉钉
    send_dingtalk_report(result["output"])
```

---

## 常见陷阱

**陷阱1：工具太多**

给Agent提供超过15个工具时，LLM选择工具的准确率会明显下降。解决：
- 把工具分组，用不同的专门Agent处理不同类别的任务
- 或者用动态工具选择（先让LLM选择需要哪些工具，再实际提供）

**陷阱2：无限循环**

Agent陷入循环：A工具返回错误 → 换B工具 → B也报错 → 换回A...

防止方法：
- 设置`max_iterations`上限（通常10-15次足够）
- 在工具里检测并拒绝明显错误的参数
- 在提示词里明确"如果连续3次失败，停止尝试并报告原因"

**陷阱3：上下文膨胀**

长任务后工具调用历史会占满上下文，导致后续行动质量下降。

解决：
- 使用`ConversationSummaryBufferMemory`定期压缩历史
- 设计Agent在关键节点主动总结已知信息

**陷阱4：对工具结果过度信任**

LLM倾向于相信工具返回的结果，即使结果明显有问题。

在工具里加断言：

```python
@tool
def get_cpu_usage(host: str) -> dict:
    usage = _fetch_cpu_usage(host)
    # 断言结果合理
    assert 0 <= usage <= 100, f"CPU usage {usage} out of range [0, 100]"
    return {"host": host, "cpu_usage": usage}
```
