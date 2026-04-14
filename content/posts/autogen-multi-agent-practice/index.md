---
title: "AutoGen 多 Agent 协作实战：从 Group Chat 到生产落地"
date: 2026-04-06T11:30:00+08:00
draft: false
tags: ["AutoGen", "Multi-Agent", "LLM Agent", "协作框架", "Python"]
categories: ["AI 工程"]
description: "AutoGen 把多 Agent 协作从玩具推向生产。本文讲清它的核心抽象 (Conversable Agent / Group Chat / 工具调用)，以及从 demo 到生产要处理的那些事。"
summary: "AutoGen 把多 Agent 协作从玩具推向生产。本文讲清它的核心抽象 (Conversable Agent / Group Chat / 工具调用)，以及从 demo 到生产要处理的那些事。"
toc: true
math: false
diagram: false
keywords: ["AutoGen", "Multi-Agent", "Group Chat", "Agent Framework", "LLM"]
params:
  reading_time: true
---

## 为什么是多 Agent 而不是单 Agent

单 Agent（一个 LLM + 一组工具 + 一个 while loop）在简单任务上已经够用：查天气、算账单、写总结。但一旦任务复杂到"**需要多种角色协作**"，单 Agent 的瓶颈就很明显：

- 同一个 prompt 既要会写代码又要会审计代码，模型容易精神分裂
- 工具超过 20 个后 system prompt 爆炸，模型 tool selection 出错率上升
- 长链路任务，上下文不断累积，后期响应越来越慢
- 一个错误判断会污染整条链路，没有"第二双眼睛"

多 Agent 的思路很直白：**把一个大任务拆给多个专职 Agent，让它们通过对话协作完成**。代码写手、代码审阅、执行环境、产品经理、QA 各司其职，每个 Agent 自己的 prompt 只管自己的职责，工具集也缩小到相关的几个。

AutoGen 是目前这个方向上最成熟的开源框架之一。它不是第一个做多 Agent 的（CrewAI、LangGraph、MetaGPT 都在做），但它在**可编程性**和**生产化**上走得比较深。这篇文章按我用 AutoGen 做过的一个代码生成 Agent 的经验来写。

## 一、定位和版本说明

AutoGen 在 2024 年底有一次大的重写，从 `pyautogen` / `autogen-agentchat` 等合并演变到 0.2 → 0.4 架构。新架构（0.4+）的核心抽象和老版本不同：

- **0.2**：基于 `ConversableAgent` + `GroupChat` + `UserProxyAgent`，API 简单
- **0.4+**：分层：`autogen-core`（低层消息/Actor 抽象）+ `autogen-agentchat`（高层对话抽象）+ `autogen-ext`（各种扩展）

这篇文章以 **0.4+ 的 agentchat 层** 为主讲解，因为这是官方推荐的生产路径。老 0.2 API 仍能跑但逐渐进入维护模式。

## 二、核心抽象

### 2.1 Model Client

AutoGen 把 LLM 调用抽象成 `ChatCompletionClient`，支持 OpenAI、Azure、Anthropic，以及任何 OpenAI 兼容接口（vLLM / LiteLLM / DeepSeek）。

```python
from autogen_ext.models.openai import OpenAIChatCompletionClient

model_client = OpenAIChatCompletionClient(
    model="gpt-4o",
    api_key="sk-xxx",
)

# 或者指向 LiteLLM 网关
model_client = OpenAIChatCompletionClient(
    model="fast-medium",
    base_url="http://litellm:4000",
    api_key="sk-virtual-xxx",
    model_info={
        "vision": False,
        "function_calling": True,
        "json_output": True,
        "family": "unknown",
    },
)
```

`model_info` 字段告诉 AutoGen 这个模型支持什么能力。指向自建服务时必须手动传 `model_info`，否则 AutoGen 无法判断能不能用 tool calling。

### 2.2 Agent

Agent 是消息的"收发者 + 处理者"。几个内置 Agent 类型：

- **AssistantAgent**：最常用，一个 LLM + 工具 + system prompt
- **UserProxyAgent**：代表人类用户，可以触发输入、执行代码
- **CodeExecutorAgent**：专门执行代码的 Agent
- **SocietyOfMindAgent**：把一个子 team 封装成单 Agent（多层嵌套）

```python
from autogen_agentchat.agents import AssistantAgent

planner = AssistantAgent(
    name="planner",
    model_client=model_client,
    system_message=(
        "你是规划专家。用户给出目标，你负责把目标拆成可执行的步骤列表。"
        "每一步要具体、可验证。不负责写代码。"
    ),
)

coder = AssistantAgent(
    name="coder",
    model_client=model_client,
    tools=[read_file, write_file, run_python],
    system_message="你是 Python 工程师，根据规划步骤写代码并执行。",
)

reviewer = AssistantAgent(
    name="reviewer",
    model_client=model_client,
    system_message="你是代码审阅。检查 coder 的代码，找 bug 和可优化点。通过时说 APPROVED。",
)
```

### 2.3 Team

Team 把多个 Agent 组合成协作单元。最常见的几种：

- **RoundRobinGroupChat**：轮流发言，顺序固定
- **SelectorGroupChat**：由一个 selector（LLM 或函数）动态决定下一个发言者
- **Swarm**：基于 handoff 的路由（Agent 自己说"把控制权交给谁"）
- **MagenticOne**：官方提供的通用多 Agent 模板

```python
from autogen_agentchat.teams import SelectorGroupChat
from autogen_agentchat.conditions import TextMentionTermination, MaxMessageTermination

termination = TextMentionTermination("APPROVED") | MaxMessageTermination(20)

team = SelectorGroupChat(
    participants=[planner, coder, reviewer],
    model_client=model_client,
    termination_condition=termination,
    selector_prompt=(
        "根据当前对话选择下一个要发言的 Agent。\n"
        "- 规划没出来 → planner\n"
        "- 规划有了但代码没写 → coder\n"
        "- 代码写完没审 → reviewer\n"
        "- reviewer 未通过 → 回到 coder\n\n"
        "参与者: {participants}\n"
        "历史: {history}\n"
    ),
    allow_repeated_speaker=False,
)
```

`termination_condition` 决定会话什么时候结束。AutoGen 提供了几个常用的组合算子：

- `TextMentionTermination(text)`：某 Agent 说了某关键词就停
- `MaxMessageTermination(n)`：总消息数上限
- `TokenUsageTermination(limit)`：token 使用到上限停
- `TimeoutTermination(seconds)`：时间超时
- 逻辑运算：`a | b`、`a & b`

### 2.4 Tool

工具用标准 Python 函数 + type hint + docstring 定义：

```python
from typing import Annotated

async def search_docs(
    query: Annotated[str, "搜索关键词"],
    top_k: Annotated[int, "返回结果数量"] = 5,
) -> list[dict]:
    """在内部知识库中搜索文档。"""
    results = await my_vector_db.search(query, top_k)
    return results
```

`Annotated` 类型提示会被转成 JSON schema 发给 LLM。AutoGen 把 tool 的返回值自动序列化成字符串塞回对话。

异步/同步都支持，生产环境推荐异步。

## 三、一个完整的例子：代码生成 Team

下面这个例子是个能跑的代码生成 Team：用户给需求，planner 拆步骤，coder 写代码，runner 执行，reviewer 审阅，直到通过。

```python
import asyncio
from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.teams import SelectorGroupChat
from autogen_agentchat.conditions import TextMentionTermination, MaxMessageTermination
from autogen_agentchat.ui import Console
from autogen_ext.models.openai import OpenAIChatCompletionClient
from autogen_ext.code_executors.docker import DockerCommandLineCodeExecutor

async def main():
    model_client = OpenAIChatCompletionClient(
        model="gpt-4o",
        api_key="sk-xxx",
    )

    # 执行器（隔离在 Docker 里跑代码）
    code_executor = DockerCommandLineCodeExecutor(
        image="python:3.11-slim",
        timeout=60,
        work_dir="/tmp/autogen-work",
    )
    await code_executor.start()

    async def run_code(code: str, language: str = "python") -> str:
        """执行一段代码并返回输出。"""
        from autogen_core.code_executor import CodeBlock
        result = await code_executor.execute_code_blocks(
            [CodeBlock(code=code, language=language)],
            cancellation_token=None,
        )
        return result.output

    planner = AssistantAgent(
        name="planner",
        model_client=model_client,
        system_message=(
            "你是技术规划专家。根据用户需求列出 3-7 个可执行步骤，"
            "每步一行。不写代码、不执行。规划完成后说 PLAN_DONE。"
        ),
    )

    coder = AssistantAgent(
        name="coder",
        model_client=model_client,
        tools=[run_code],
        system_message=(
            "你是 Python 工程师。根据 planner 的步骤写代码并用 run_code 工具执行。"
            "执行成功后总结结果。代码有问题立刻修复重试。"
        ),
        reflect_on_tool_use=True,
    )

    reviewer = AssistantAgent(
        name="reviewer",
        model_client=model_client,
        system_message=(
            "你是代码审阅。阅读 coder 的代码和执行结果，检查正确性、异常处理、边界条件。"
            "有问题详细指出，交给 coder 修复；全部通过时只说 APPROVED。"
        ),
    )

    termination = TextMentionTermination("APPROVED") | MaxMessageTermination(30)

    team = SelectorGroupChat(
        participants=[planner, coder, reviewer],
        model_client=model_client,
        termination_condition=termination,
        allow_repeated_speaker=True,
    )

    task = "写一个 Python 函数 check_palindrome(s: str) -> bool，忽略大小写和非字母数字字符。写完后自测通过 3 个 case。"
    await Console(team.run_stream(task=task))

    await code_executor.stop()

asyncio.run(main())
```

几个要点：

- `DockerCommandLineCodeExecutor` 把代码跑在 Docker 容器里，隔离安全
- `run_code` 作为工具给 coder 使用
- `reflect_on_tool_use=True` 让 coder 在工具调用后再思考一步（通常输出会更高质量）
- `allow_repeated_speaker=True` 允许 coder 连续发言（比如修完代码立刻再测）
- 终止条件是 reviewer 说 APPROVED 或者总消息 30 条

## 四、消息流和状态

AutoGen 的 Team 本质上是一个状态机：

```
      ┌─────────────────────────┐
      │   Initial task message  │
      └────────────┬────────────┘
                   │
                   ▼
          ┌────────────────┐
          │    Selector    │  选下一个发言者
          └────────┬───────┘
                   │
                   ▼
          ┌────────────────┐
          │  Agent 处理    │
          │  - LLM 思考    │
          │  - 可选工具调用 │
          │  - 返回消息     │
          └────────┬───────┘
                   │
                   ▼
          ┌────────────────┐
          │  Termination?  │
          └────┬───────┬───┘
               │       │
            否  │       │  是
               │       │
               └───┐   ▼
                   ▼  ┌──────┐
          (回到 Selector)│ 结束  │
                       └──────┘
```

每个 Agent 看到的消息是**整个 Team 的对话历史**，不是只看和自己相关的。这和 LangGraph 里的"节点只看自己订阅的 state 片段"思路不同。优势是 Agent 能感知其他 Agent 的讨论，劣势是上下文会线性增长。

### 4.1 状态持久化

0.4+ 的 AutoGen 支持 team 状态的保存和恢复：

```python
state = await team.save_state()

# 持久化到 DB
await store_state(session_id, state)

# 恢复
state = await load_state(session_id)
await team.load_state(state)
result = await team.run(task="继续上次的工作")
```

这对**长任务**和**断点续跑**场景很重要。状态里包含对话历史、Agent 内部状态、Selector 状态等。

## 五、工具调用深入

### 5.1 工具的类型

- **Python 函数**：最常见
- **MCP 工具**：0.4+ 支持 Model Context Protocol，动态从 MCP Server 拉工具
- **其他 Agent 当工具**：`AgentTool(other_agent)`，把另一个 Agent 变成工具

MCP 是目前 Agent 工具生态最被看好的方向，AutoGen 的集成让你可以直接接入已有的 MCP Server（文件系统、数据库、Git 等）。

```python
from autogen_ext.tools.mcp import McpWorkbench, StdioServerParams

server_params = StdioServerParams(
    command="npx",
    args=["-y", "@modelcontextprotocol/server-filesystem", "/data"],
)
workbench = McpWorkbench(server_params)
await workbench.start()

tools = await workbench.get_tools()

agent = AssistantAgent(
    name="filesystem_agent",
    model_client=model_client,
    tools=tools,
)
```

### 5.2 工具错误处理

工具抛异常时 AutoGen 默认会把异常消息塞回对话让 LLM 看到并重试。但对生产场景这样还不够，要加一层：

```python
async def safe_run_code(code: str) -> str:
    try:
        return await run_code_internal(code)
    except TimeoutError:
        return "执行超时，请简化代码或拆分步骤"
    except MemoryError:
        return "内存不足，当前无法执行"
    except Exception as e:
        # 记录到监控
        logger.exception("tool error")
        return f"执行失败: {type(e).__name__}: {str(e)[:200]}"
```

把错误转换成**LLM 能理解的自然语言**，而不是裸的堆栈。

### 5.3 工具调用循环保护

常见坑：Agent 陷入"调用工具 → 出错 → 再调同样的工具 → 出错 → ..."。防护：

- `MaxMessageTermination` 作为最后兜底
- 工具层自己做幂等和短路（同样输入 5 秒内不允许重复调）
- Agent 的 system prompt 明确"同一个工具失败 3 次后停下来告诉用户"

## 六、流式输出

Agent 的 LLM 调用是支持流式的。Team 也支持 streaming 对话：

```python
async for message in team.run_stream(task="..."):
    if hasattr(message, "content"):
        print(f"[{message.source}] {message.content}")
```

每个消息是一个完整的 Agent 发言。如果要 token 级流式：

```python
agent = AssistantAgent(
    name="streaming_agent",
    model_client=model_client,
    model_client_stream=True,
)
```

`model_client_stream=True` 启用后会有 `ModelClientStreamingChunkEvent` 事件流出，可以实时更新 UI。

## 七、UserProxy 和人类介入

很多场景要"人在环路"：Agent 不确定时问用户，危险操作前让用户批准。

```python
from autogen_agentchat.agents import UserProxyAgent

async def human_input(prompt: str) -> str:
    # 这里接你的前端/IM
    return await send_to_user_and_wait(prompt)

user_proxy = UserProxyAgent(
    name="user",
    input_func=human_input,
)

team = SelectorGroupChat(
    participants=[planner, coder, user_proxy],
    ...
)
```

`input_func` 可以是异步函数，连到 WebSocket、企业微信、钉钉 bot，实现"Agent 过程中向真人确认"。

## 八、部署形态

### 8.1 脚本模式

最简单：写个 Python 脚本，命令行跑。适合单次任务。

### 8.2 长驻服务

把 Team 封装成 FastAPI 服务，每次请求新建或复用 Team 实例：

```python
from fastapi import FastAPI
from uuid import uuid4

app = FastAPI()
sessions = {}

@app.post("/chat")
async def chat(req: dict):
    session_id = req.get("session_id") or str(uuid4())
    task = req["task"]

    if session_id not in sessions:
        sessions[session_id] = create_team()
    team = sessions[session_id]

    messages = []
    async for msg in team.run_stream(task=task):
        messages.append({"source": getattr(msg, "source", ""), "content": str(msg.content)})
    return {"session_id": session_id, "messages": messages}
```

生产化时 sessions 要落 Redis/DB，避免 Pod 重启丢状态。用 `team.save_state()` / `team.load_state()` 持久化。

### 8.3 Ray Serve 部署

对大型多 Agent 应用，每个 Agent 变成 Ray Serve Deployment 有额外好处：

- Agent 间的调用是跨 Actor 的，自动获得并发和容错
- 长链路任务不怕 Pod 重启
- 单个 Agent 可以独立扩缩

但这个方案相对重，适合已经有 Ray 基建的团队。

## 九、可观测性

Multi-Agent 系统最难的是 debug——哪个 Agent 哪句话导致了跑偏？

### 9.1 日志

AutoGen 使用标准 Python logging，每个 Agent 和 LLM 调用都有 logger：

```python
import logging
logging.basicConfig(level=logging.INFO)
logging.getLogger("autogen_agentchat").setLevel(logging.INFO)
logging.getLogger("autogen_core.events").setLevel(logging.INFO)
```

### 9.2 OpenTelemetry

0.4+ 官方集成了 OTel，每个 Agent 动作、LLM 调用、工具调用都是一个 span：

```python
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

provider = TracerProvider()
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint="otel-collector:4317")))
trace.set_tracer_provider(provider)

# AutoGen 会自动在 OTel tracer 上打 span
```

把 trace 送到 Jaeger / Tempo / Langfuse，可以可视化整条 Agent 对话链路。

### 9.3 Langfuse 集成

通过 LiteLLM 作为 model_client 的 base_url，把 LLM 调用自动 trace 到 Langfuse 是最省事的做法：

```python
model_client = OpenAIChatCompletionClient(
    model="fast-medium",
    base_url="http://litellm:4000",
    api_key="sk-virtual-xxx",
)
```

LiteLLM 的 Langfuse callback 自动抓取每次 LLM 调用，你在 Langfuse 里看到一条完整的 session 记录。

## 十、成本控制

Multi-Agent 最大的坑是**成本爆炸**。5 个 Agent × 平均 20 轮对话 × 每轮 3k token，一个 session 就是 30 万 token。用 GPT-4 可能就是几十刀。

### 10.1 控制策略

- **上下文裁剪**：用 AutoGen 的 `ContextTransform`，每次调 LLM 前裁掉太旧的消息
- **Agent 分级**：规划 / 审阅用 GPT-4，代码执行反馈用 GPT-4o-mini
- **termination_condition 严格**：别让对话无止境
- **cache 固定 prompt**：system prompt 很长的话开 Anthropic prompt cache

```python
from autogen_agentchat.agents import AssistantAgent
from autogen_core.model_context import BufferedChatCompletionContext

agent = AssistantAgent(
    name="coder",
    model_client=model_client,
    model_context=BufferedChatCompletionContext(buffer_size=10),
)
```

`BufferedChatCompletionContext(buffer_size=10)` 只保留最近 10 条消息，避免历史无限增长。

### 10.2 预算绑死

上线前一定绑定 LiteLLM 的 Virtual Key，给每个 Agent 任务一个预算上限。超预算就断，不要等用户发现。

## 十一、生产踩坑合集

### 坑 1：Selector 选不出合理的下一个发言者

`SelectorGroupChat` 的 selector 本身是一个 LLM 调用，用 GPT-4o-mini 成本低但有时候选错人。解决：

- selector_prompt 写清楚每种状态下应该选谁
- 用正则兜底：如果 selector 返回不在 participants 里，回退到 round-robin
- 用 `allow_repeated_speaker=True` 让 selector 有连续选同一 Agent 的选项

### 坑 2：Agent 无限循环

两个 Agent 互相让来让去的情况很常见："A: 这事你来吧" "B: 不，你来吧"。防护：

- 加 `MaxMessageTermination` 硬上限
- 设计时让每个 Agent 有明确的"终止语"（比如 reviewer 必须说 APPROVED）
- Selector 里明确"连续 3 次同一 Agent 发言且无进展则终止"

### 坑 3：JSON 格式的对话内容被模型污染

有些 Agent 要输出结构化 JSON，但在对话里被其他 Agent 当成普通文本"评论"了几句，后续再解析就炸。解法：

- 关键结构化内容用 markdown 代码块包起来
- 用 `response_format={"type": "json_object"}`（OpenAI）或约束解码
- 让专门的 Parser Agent 负责从对话中提取 JSON

### 坑 4：工具执行环境安全

允许 Agent 执行代码是巨大的安全风险。必须：

- 用 Docker / gVisor / Firecracker 隔离
- 限制网络访问
- 限制文件系统访问
- 限制执行时间和内存
- 审计所有执行的代码

不要用本机 Python subprocess 跑 LLM 生成的代码，这是等着被黑的姿势。

### 坑 5：消息上下文无限增长

长会话下每个 Agent 的 context 线性增长，到后期每次 LLM 调用都是几万 token。强制使用 `BufferedChatCompletionContext` 或自定义的 context transform（比如只保留关键决策 + 最近 N 条）。

### 坑 6：Agent "假装"工具调用

模型偶尔会输出"我调用了 search_docs(...)"这样的文本而不是真的工具调用。原因通常是 system prompt 不清晰或者 model_client 没开 function_calling。对照：

- model_client 的 `model_info.function_calling` 必须 true
- system prompt 明确"使用工具而不是描述工具调用"

### 坑 7：并发 session 共享 team 对象

Team 对象有内部状态，多个并发请求共享会互相污染。每个 session 独立 team 实例。

### 坑 8：终止条件之后还有残留消息

有时候 TextMentionTermination 触发了，但管道里还有几条消息在流。处理流式输出的代码要能忽略终止后的消息。

### 坑 9：Agent 调用失败没有错误路径

LLM 调用 5xx、tool 超时等错误默认冒出来到 Team 层面，整个 team 异常退出。生产场景要包 try/except，降级为"告诉用户服务暂时不可用"而不是崩溃。

### 坑 10：MCP 工具 stdio 子进程僵尸

MCP stdio server 是子进程，Python 异常退出后子进程可能没被清理。用 `async with workbench:` 或明确 `await workbench.stop()`。

## 十二、和其他框架对比

| 维度 | AutoGen | CrewAI | LangGraph | Swarm (OpenAI) |
|---|---|---|---|---|
| 抽象层次 | 中（Agent + Team） | 高（Role + Task） | 低（图 + 状态） | 极简（handoff） |
| 灵活度 | 高 | 中 | 最高 | 高 |
| 上手 | 中 | 简单 | 陡 | 简单 |
| 多 Agent 模式 | Group Chat 为主 | Sequential / Hierarchy | 自定义图 | Handoff |
| 工具 | 函数 / MCP | 函数 | 函数 | 函数 |
| 官方背景 | Microsoft | 独立 | LangChain | OpenAI (实验性) |
| 生产化 | 较强 | 发展中 | 强（和 LangChain 一套） | 实验性 |
| 状态持久化 | 支持 | 一般 | 强（Checkpointer） | 弱 |

**选型建议**：

- 需要快速搭出角色化团队、业务理解的人能看懂：CrewAI
- 对流程控制和状态要求高：LangGraph
- 平衡灵活性和工程化：AutoGen
- 简单 handoff 模式 + 探索阶段：Swarm

我的实践：复杂流程 + 需要状态的长任务用 LangGraph；多角色对话式协作用 AutoGen。两者不是互斥的。

## 十三、一个生产化架构示例

假设我们要做一个"代码助理"产品，用 AutoGen 做多 Agent 协作：

```
 ┌─────────────────────────────────────────┐
 │             Web 前端（React）             │
 └───────────────┬─────────────────────────┘
                 │ WebSocket (流式)
 ┌───────────────▼─────────────────────────┐
 │         FastAPI Gateway                  │
 │  - 鉴权                                  │
 │  - Session 管理                          │
 │  - WebSocket 转 Team stream              │
 └───────────────┬─────────────────────────┘
                 │
 ┌───────────────▼─────────────────────────┐
 │          AutoGen Team                    │
 │  ┌───────────┐  ┌──────────┐ ┌─────────┐ │
 │  │ Planner   │  │  Coder   │ │Reviewer │ │
 │  └─────┬─────┘  └────┬─────┘ └────┬────┘ │
 │        │             │            │      │
 │        └─────────┬───┴────────────┘      │
 │                  │                        │
 └──────────────────┼────────────────────────┘
                    │
        ┌───────────┼────────────┐
        │           │            │
  ┌─────▼────┐ ┌────▼────┐ ┌─────▼────┐
  │ LiteLLM  │ │ Code    │ │  MCP     │
  │ Gateway  │ │ Sandbox │ │ Servers  │
  │          │ │(Docker) │ │ (git,fs) │
  └─────┬────┘ └─────────┘ └──────────┘
        │
        ├─→ GPT-4o (planner / reviewer)
        ├─→ GPT-4o-mini (coder 低难度)
        └─→ Claude Sonnet (coder 难任务)
```

几个设计点：

- **LLM 统一走 LiteLLM**：成本控制 + 审计 + fallback
- **代码沙盒隔离**：Docker 容器，资源限制，网络禁用
- **MCP 作为工具源**：Git、文件系统都用现成 MCP Server，不自己写工具
- **Session 持久化 Redis**：Pod 重启不丢任务状态
- **WebSocket 流式输出**：用户能看到每个 Agent 的实时发言
- **OTel 全链路追踪**：出问题能定位到是哪个 Agent 哪一步

## 十四、上线 checklist

```
[ ] 代码执行环境隔离（Docker 或 gVisor）
[ ] 网络和文件系统权限最小化
[ ] LiteLLM 网关集中管理 LLM 调用
[ ] 每个 Team session 有预算上限
[ ] Termination condition 有硬上限
[ ] Context 有 buffer size 防止无限增长
[ ] 工具调用错误被捕获并转成 LLM 可读的消息
[ ] OTel 或 Langfuse 接入
[ ] Team 状态持久化到 Redis / DB
[ ] 长会话支持恢复（load_state）
[ ] WebSocket / SSE 流式输出
[ ] Agent system prompt 版本化管理
[ ] 出错时有降级方案：Agent 挂了返回清晰错误，不要 stack trace 到用户
[ ] 权限模型：不是所有用户都能触发所有工具
[ ] 成本告警：单 session 超 X 元就告警或中断
```

## 十五、收尾

多 Agent 不是银弹。在**能用单 Agent 解决的场景**坚决用单 Agent，少一层复杂度就少一层 bug。只有当你确实需要角色专业化、需要互相审阅、需要长链路协作时，多 Agent 才能体现价值。

选了多 Agent 之后，AutoGen 是目前最平衡的框架：

- 比 CrewAI 灵活
- 比 LangGraph 上手快
- 比 Swarm 成熟
- 生态和微软做的 MAGENTIC-ONE 等研究项目一起推进

但记住它和所有 Agent 框架一样面临的根本问题：**LLM 本身的不确定性**。多 Agent 不会让不确定性消失，只会把不确定性分散到更多节点上。你要做的是在每个节点上限制破坏范围：严格的 termination、隔离的执行环境、硬性的预算上限、完备的 trace。

把这些工程护栏做到位，多 Agent 才能从炫酷 demo 走向可靠产品。
