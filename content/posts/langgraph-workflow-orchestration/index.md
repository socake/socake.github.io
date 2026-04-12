---
title: "LangGraph 工作流编排：构建有状态的 AI 应用"
date: 2026-04-12T14:30:00+08:00
draft: false
tags: ["LangGraph", "工作流", "状态机", "AI Agent", "LangChain"]
categories: ["大模型"]
description: "用LangGraph构建有状态AI工作流：状态机原理、条件分支、Human-in-the-loop与持久化Checkpoint"
summary: "从LangChain Chain的局限出发，讲清楚LangGraph的状态机模型、Graph/Node/Edge的设计方式，以及条件分支、循环、人工介入、Checkpoint持久化的工程实现，最后用一个运维诊断工作流串起来所有概念。"
toc: true
math: false
diagram: false
keywords: ["LangGraph", "AI工作流", "状态机", "Human-in-the-loop", "运维诊断", "Checkpoint"]
params:
  reading_time: true
---

LangChain 的 Chain 解决了"把几个 LLM 调用串联起来"的问题，但遇到需要循环、条件分支、中间等待人工介入、或者需要跨请求保持状态的场景，Chain 就显得力不从心。LangGraph 用状态机模型解决了这些问题。

## 为什么需要 LangGraph

看一个具体的痛点——你想实现一个"先分析问题，如果需要更多信息则继续追问，否则给出答案"的 Agent：

用 LangChain Chain 实现：
```python
# 问题：如果 LLM 决定需要继续追问，你无法在 Chain 内部做循环
chain = prompt | llm | output_parser
# 只能单次执行，无法根据 LLM 的判断决定是否继续
```

用 LangGraph 实现：
```python
# 可以定义：如果 LLM 输出了 "need_more_info"，就跳回收集信息节点
# 直到 LLM 输出 "ready_to_answer" 才前进到答案节点
```

**LangGraph 的核心价值**：
1. **循环支持**：可以无限迭代直到满足条件
2. **条件分支**：根据状态或 LLM 输出决定走哪条路
3. **Human-in-the-loop**：在关键节点暂停等待人工确认
4. **状态持久化**：用 Checkpoint 保存中间状态，支持断点续跑和多轮对话
5. **并行执行**：多个无依赖的节点可以并发跑

---

## 核心概念

LangGraph 的三要素：

- **State**：图的"记忆"，是一个类型化的字典，在所有节点间共享和传递
- **Node**：普通 Python 函数，接收 State，返回对 State 的更新
- **Edge**：节点间的连接，可以是固定边（A → B）或条件边（根据状态决定走哪里）

```
State = 一个 TypedDict，记录整个工作流的所有数据
Node = def my_node(state: State) -> dict（返回要更新的字段）
Edge = 固定连接 或 条件函数（返回下一个节点的名字）
```

---

## 环境安装

```bash
pip install langgraph langchain-openai langchain-core
```

---

## 基础示例：问答 + 自我反思

```python
from typing import TypedDict, Annotated, Literal
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

# 1. 定义 State
class QAState(TypedDict):
    messages: Annotated[list, add_messages]  # add_messages 是追加语义，不是覆盖
    question: str
    answer: str
    needs_revision: bool
    revision_count: int

# 2. 定义节点
llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)

def generate_answer(state: QAState) -> dict:
    """生成初始答案"""
    response = llm.invoke([
        SystemMessage(content="你是一个专业的技术助手，给出准确详细的回答。"),
        HumanMessage(content=state["question"])
    ])
    return {
        "answer": response.content,
        "messages": [response],
        "revision_count": state.get("revision_count", 0)
    }

def review_answer(state: QAState) -> dict:
    """自我审查答案质量"""
    review_prompt = f"""审查以下回答的质量：

问题：{state['question']}
回答：{state['answer']}

如果回答不够完整、有明显错误或需要补充，返回 "needs_revision"。
否则返回 "looks_good"。

只返回这两个选项之一，不要其他内容。"""

    response = llm.invoke([HumanMessage(content=review_prompt)])
    needs_revision = "needs_revision" in response.content.lower()

    return {
        "needs_revision": needs_revision,
        "revision_count": state.get("revision_count", 0)
    }

def revise_answer(state: QAState) -> dict:
    """修改答案"""
    response = llm.invoke([
        SystemMessage(content="你是一个专业的技术助手，请改进你的回答。"),
        HumanMessage(content=f"请改进以下对 '{state['question']}' 的回答，使其更完整准确：\n\n{state['answer']}")
    ])
    return {
        "answer": response.content,
        "revision_count": state["revision_count"] + 1
    }

# 3. 条件边函数
def should_revise(state: QAState) -> Literal["revise", "end"]:
    """决定是否需要修改"""
    if state["needs_revision"] and state.get("revision_count", 0) < 2:
        return "revise"
    return "end"

# 4. 构建图
builder = StateGraph(QAState)

builder.add_node("generate", generate_answer)
builder.add_node("review", review_answer)
builder.add_node("revise", revise_answer)

builder.set_entry_point("generate")
builder.add_edge("generate", "review")
builder.add_conditional_edges(
    "review",
    should_revise,
    {
        "revise": "revise",
        "end": END
    }
)
builder.add_edge("revise", "review")  # 修改后再审查，形成循环

graph = builder.compile()

# 5. 执行
result = graph.invoke({
    "question": "如何在Kubernetes中实现蓝绿部署？",
    "answer": "",
    "needs_revision": False,
    "revision_count": 0,
    "messages": []
})

print(f"最终答案（经过 {result['revision_count']} 次修改）：")
print(result["answer"])
```

---

## Human-in-the-loop：人工介入节点

生产中最重要的功能之一——在执行敏感操作前等待人工确认：

```python
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import StateGraph, END
from typing import TypedDict

class OperationState(TypedDict):
    user_request: str
    plan: str          # LLM 制定的执行计划
    approved: bool     # 人工是否批准
    result: str

def plan_operations(state: OperationState) -> dict:
    """LLM 分析请求，制定操作计划"""
    response = llm.invoke([
        SystemMessage(content="""你是运维助手。分析用户请求，制定详细执行计划。
计划必须包含：
1. 影响范围
2. 具体操作步骤
3. 潜在风险
4. 回滚方案"""),
        HumanMessage(content=state["user_request"])
    ])
    return {"plan": response.content}

def execute_operations(state: OperationState) -> dict:
    """执行实际操作（人工批准后才会到达这里）"""
    if not state["approved"]:
        return {"result": "操作已取消"}

    # 实际执行逻辑
    # result = run_kubectl(state["plan"])
    result = f"已按计划执行：{state['plan'][:100]}..."
    return {"result": result}

# 构建需要人工介入的图
builder = StateGraph(OperationState)
builder.add_node("plan", plan_operations)
builder.add_node("execute", execute_operations)
builder.set_entry_point("plan")

# plan → 中断（等待人工） → execute
builder.add_edge("plan", "execute")

# 关键：在 plan 节点后设置中断点
graph = builder.compile(
    checkpointer=MemorySaver(),
    interrupt_after=["plan"]   # 在 plan 节点执行后暂停
)

# ---- 第一阶段：LLM 制定计划 ----
thread_config = {"configurable": {"thread_id": "op-001"}}

state_after_plan = graph.invoke(
    {"user_request": "将 nginx deployment 的副本数从2改为5"},
    config=thread_config
)

print("=== 执行计划 ===")
print(state_after_plan["plan"])
print("\n请确认是否执行？(yes/no)")

# ---- 等待人工输入 ----
user_input = input()

# ---- 第二阶段：根据人工决定继续执行 ----
approved = user_input.lower() == "yes"

# 更新状态中的 approved 字段
graph.update_state(
    thread_config,
    {"approved": approved}
)

# 从中断点继续执行
final_state = graph.invoke(None, config=thread_config)
print(f"\n执行结果：{final_state['result']}")
```

---

## Checkpoint 持久化

生产中必须用持久化存储而不是内存，支持：跨进程恢复、服务重启后继续、多用户对话隔离。

### 使用 PostgreSQL 持久化

```bash
pip install langgraph-checkpoint-postgres psycopg2-binary
```

```python
from langgraph.checkpoint.postgres import PostgresSaver
import psycopg2

# 连接数据库
conn = psycopg2.connect(
    host="localhost",
    database="langgraph",
    user="postgres",
    password="password"
)

# 初始化 checkpoint 表（首次运行）
checkpointer = PostgresSaver(conn)
checkpointer.setup()  # 创建必要的表结构

# 编译图时传入持久化 checkpointer
graph = builder.compile(checkpointer=checkpointer)

# 使用 thread_id 区分不同对话/任务
thread_config = {"configurable": {"thread_id": "user-123-session-456"}}

# 第一次调用
result1 = graph.invoke(initial_state, config=thread_config)

# 程序重启后，用同一个 thread_id 恢复状态
# graph 会从上次中断的地方继续
result2 = graph.invoke(None, config=thread_config)
```

### 查看历史快照

```python
# 查看某个 thread 的所有历史快照
history = list(graph.get_state_history(thread_config))
for snapshot in history:
    print(f"Step {snapshot.step}: {snapshot.values.keys()}")

# 回滚到某个历史状态
old_snapshot = history[-3]  # 三步前
graph.update_state(thread_config, old_snapshot.values)
```

---

## 实战：运维诊断工作流

整合以上所有概念，构建一个完整的运维诊断 Agent：

```
收集基本信息 → 分析症状 → [需要更多信息？] → 循环收集
                              ↓ 信息足够
                          生成诊断结论
                              ↓
                     [操作风险高？] → 人工确认 → 执行修复
                              ↓ 低风险
                          自动执行修复
                              ↓
                          验证结果 → [是否成功？] → 结束/重试
```

```python
from typing import TypedDict, Annotated, Literal
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage
import subprocess

llm = ChatOpenAI(model="gpt-4o", temperature=0)

class DiagnosticState(TypedDict):
    # 问题描述
    issue_description: str
    # 收集到的诊断信息
    collected_info: list[str]
    # 是否需要更多信息
    need_more_info: bool
    # 需要收集什么信息
    info_to_collect: list[str]
    # 诊断结论
    diagnosis: str
    # 修复方案
    fix_plan: str
    # 操作风险等级
    risk_level: Literal["low", "medium", "high"]
    # 是否已人工批准
    human_approved: bool
    # 执行结果
    execution_result: str
    # 是否成功
    is_resolved: bool
    # 迭代次数
    iteration_count: int

def collect_basic_info(state: DiagnosticState) -> dict:
    """自动收集基础诊断信息"""
    collected = []

    # 收集 K8s 状态（实际场景中替换为真实命令）
    commands = {
        "pods_status": "kubectl get pods -A --field-selector=status.phase!=Running 2>/dev/null | head -20",
        "recent_events": "kubectl get events -A --sort-by=.lastTimestamp 2>/dev/null | tail -20",
        "node_status": "kubectl get nodes 2>/dev/null",
    }

    for name, cmd in commands.items():
        try:
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=10
            )
            if result.stdout.strip():
                collected.append(f"[{name}]\n{result.stdout.strip()}")
        except Exception as e:
            collected.append(f"[{name}] 收集失败: {e}")

    return {"collected_info": collected}

def analyze_and_plan(state: DiagnosticState) -> dict:
    """分析症状，制定诊断和修复计划"""
    info_text = "\n\n".join(state["collected_info"])

    response = llm.invoke([
        SystemMessage(content="""你是资深SRE工程师，根据收集到的运维信息进行诊断。

返回JSON格式：
{
    "need_more_info": false,
    "info_to_collect": [],
    "diagnosis": "根本原因分析",
    "fix_plan": "具体修复步骤",
    "risk_level": "low|medium|high",
    "explanation": "诊断说明"
}

risk_level判断：
- low: 只读操作或查询命令
- medium: 配置变更或滚动重启
- high: 删除资源、扩缩容、涉及生产数据库"""),
        HumanMessage(content=f"""问题描述：{state['issue_description']}

已收集的信息：
{info_text}

请分析并给出诊断方案。""")
    ])

    import json, re
    result_text = response.content
    json_match = re.search(r'\{.*\}', result_text, re.DOTALL)
    if json_match:
        result = json.loads(json_match.group())
        return {
            "need_more_info": result.get("need_more_info", False),
            "info_to_collect": result.get("info_to_collect", []),
            "diagnosis": result.get("diagnosis", ""),
            "fix_plan": result.get("fix_plan", ""),
            "risk_level": result.get("risk_level", "high"),
            "iteration_count": state.get("iteration_count", 0) + 1
        }
    return {"diagnosis": result_text, "risk_level": "high", "iteration_count": state.get("iteration_count", 0) + 1}

def collect_targeted_info(state: DiagnosticState) -> dict:
    """根据 LLM 要求收集特定信息"""
    new_info = state["collected_info"].copy()

    for info_request in state["info_to_collect"]:
        # 让 LLM 生成具体的 kubectl 命令
        cmd_response = llm.invoke([
            HumanMessage(content=f"生成一个 kubectl 命令来获取以下信息（只返回命令本身）：{info_request}")
        ])
        cmd = cmd_response.content.strip().replace("```bash", "").replace("```", "").strip()

        try:
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=15
            )
            new_info.append(f"[{info_request}]\n{result.stdout.strip() or result.stderr.strip()}")
        except Exception as e:
            new_info.append(f"[{info_request}] 执行失败: {e}")

    return {"collected_info": new_info, "need_more_info": False}

def execute_fix(state: DiagnosticState) -> dict:
    """执行修复操作（需要人工批准或低风险自动执行）"""
    if state["risk_level"] == "high" and not state.get("human_approved"):
        return {"execution_result": "等待人工审批", "is_resolved": False}

    # 实际执行修复命令
    # result = run_fix_commands(state["fix_plan"])
    execution_result = f"已执行修复计划。风险等级：{state['risk_level']}"

    return {"execution_result": execution_result, "is_resolved": True}

def verify_fix(state: DiagnosticState) -> dict:
    """验证修复是否有效"""
    # 重新检查状态
    verify_result = subprocess.run(
        "kubectl get pods -A --field-selector=status.phase!=Running 2>/dev/null | wc -l",
        shell=True, capture_output=True, text=True
    )
    count = int(verify_result.stdout.strip() or "0")
    is_resolved = count <= 1  # 0条结果 + 1条header = 1

    return {"is_resolved": is_resolved}

# 条件边函数
def need_more_info_or_analyze(state: DiagnosticState) -> Literal["collect_targeted", "execute"]:
    if state["need_more_info"] and state["iteration_count"] < 3:
        return "collect_targeted"
    return "execute"

def check_risk_level(state: DiagnosticState) -> Literal["auto_execute", "wait_approval"]:
    if state["risk_level"] == "low":
        return "auto_execute"
    return "wait_approval"

def check_resolution(state: DiagnosticState) -> Literal["resolved", "retry"]:
    if state["is_resolved"]:
        return "resolved"
    return "retry"

# 构建图
builder = StateGraph(DiagnosticState)

builder.add_node("collect_basic", collect_basic_info)
builder.add_node("analyze", analyze_and_plan)
builder.add_node("collect_targeted", collect_targeted_info)
builder.add_node("execute", execute_fix)
builder.add_node("verify", verify_fix)

builder.set_entry_point("collect_basic")
builder.add_edge("collect_basic", "analyze")
builder.add_conditional_edges("analyze", need_more_info_or_analyze, {
    "collect_targeted": "collect_targeted",
    "execute": "execute"
})
builder.add_edge("collect_targeted", "analyze")  # 收集后重新分析
builder.add_edge("execute", "verify")
builder.add_conditional_edges("verify", check_resolution, {
    "resolved": END,
    "retry": "analyze"   # 修复无效，重新分析
})

# 在高风险操作前设置中断点
graph = builder.compile(
    checkpointer=MemorySaver(),
    interrupt_before=["execute"]
)

# 使用工作流
def run_diagnostic(issue: str):
    thread_config = {"configurable": {"thread_id": f"diag-{hash(issue)}"}}

    # 阶段1：收集信息和分析
    state = graph.invoke(
        {
            "issue_description": issue,
            "collected_info": [],
            "iteration_count": 0,
            "human_approved": False,
            "is_resolved": False,
        },
        config=thread_config
    )

    print(f"\n=== 诊断结论 ===")
    print(f"根本原因：{state['diagnosis']}")
    print(f"\n=== 修复方案 ===")
    print(f"{state['fix_plan']}")
    print(f"\n风险等级：{state['risk_level']}")

    # 阶段2：人工确认（对于中/高风险操作）
    if state["risk_level"] in ["medium", "high"]:
        confirm = input("\n是否执行修复？(yes/no): ")
        if confirm.lower() != "yes":
            print("操作已取消")
            return

        graph.update_state(thread_config, {"human_approved": True})

    # 阶段3：执行修复
    final_state = graph.invoke(None, config=thread_config)
    print(f"\n执行结果：{final_state['execution_result']}")
    print(f"是否解决：{'是' if final_state['is_resolved'] else '否'}")

# 触发诊断
run_diagnostic("生产环境有多个Pod处于CrashLoopBackOff状态")
```

---

## 并行节点执行

对于独立的诊断任务，可以并行执行：

```python
from langgraph.graph import StateGraph
from typing import TypedDict

class ParallelState(TypedDict):
    query: str
    pod_status: str
    node_status: str
    service_status: str
    summary: str

def check_pods(state: ParallelState) -> dict:
    result = subprocess.run("kubectl get pods -A", shell=True, capture_output=True, text=True)
    return {"pod_status": result.stdout}

def check_nodes(state: ParallelState) -> dict:
    result = subprocess.run("kubectl get nodes", shell=True, capture_output=True, text=True)
    return {"node_status": result.stdout}

def check_services(state: ParallelState) -> dict:
    result = subprocess.run("kubectl get svc -A", shell=True, capture_output=True, text=True)
    return {"service_status": result.stdout}

def summarize(state: ParallelState) -> dict:
    # 汇总三个并行检查的结果
    summary = llm.invoke([
        HumanMessage(content=f"""分析以下K8s集群状态：

Pods: {state['pod_status'][:500]}
Nodes: {state['node_status'][:500]}
Services: {state['service_status'][:500]}

给出简短的健康状态摘要。""")
    ])
    return {"summary": summary.content}

builder = StateGraph(ParallelState)
builder.add_node("check_pods", check_pods)
builder.add_node("check_nodes", check_nodes)
builder.add_node("check_services", check_services)
builder.add_node("summarize", summarize)

# 从 START 并行发散到三个检查节点
builder.set_entry_point("check_pods")  # 不能直接从 START 并行，需要用 fan-out

# 三个检查节点都完成后汇聚到 summarize
builder.add_edge("check_pods", "summarize")
builder.add_edge("check_nodes", "summarize")
builder.add_edge("check_services", "summarize")
builder.add_edge("summarize", END)
```

---

## 与 LangChain 的关系

LangGraph 是 LangChain 生态的一部分，可以在 Node 里直接用 LangChain 的所有组件：

```python
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI

# 在 Node 函数里用 LCEL chain
def my_node(state: MyState) -> dict:
    chain = ChatPromptTemplate.from_messages([
        ("system", "你是一个专家"),
        ("human", "{input}")
    ]) | ChatOpenAI() | StrOutputParser()

    result = chain.invoke({"input": state["user_input"]})
    return {"result": result}
```

**什么时候用 LangGraph，什么时候用普通 LangChain**：
- 单次线性流程（A→B→C）：用 LangChain LCEL 就够
- 需要循环、分支、状态保持：用 LangGraph
- 需要人工介入、恢复执行：必须用 LangGraph
