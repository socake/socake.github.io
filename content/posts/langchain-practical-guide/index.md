---
title: "LangChain 从入门到实战：构建 LLM 应用的工程框架"
date: 2026-04-12T12:00:00+08:00
draft: false
tags: ["LangChain", "大模型", "LLM", "Agent", "LangGraph", "RAG", "FastAPI"]
categories: ["大模型"]
description: "LangChain 核心概念到生产实战：LCEL、Agent、LangGraph 状态机、LangSmith 调试，以及与 FastAPI 集成的完整示例。"
summary: "LangChain 是构建 LLM 应用最流行的框架，但也是踩坑最多的框架之一。本文从 LCEL 表达式、ReAct Agent、LangGraph 工作流到生产部署，梳理真正有用的部分，并指出哪些功能实际工程中应该避免。"
toc: true
math: false
diagram: false
keywords: ["LangChain", "LCEL", "LangGraph", "Agent", "RAG", "LangSmith", "FastAPI", "LLM应用开发"]
params:
  reading_time: true
---

LangChain 是 LLM 应用开发里最知名的框架，也是评价最两极的框架——有人说它是必备工具，有人说它是"过度抽象的噩梦"。

我用了将近两年，实际感受是：**LangChain 的某些部分很有用（LCEL、LangGraph、集成库），但它的早期抽象（Chain、Memory）确实有点乱**。本文聚焦真正能在生产中用好的部分。

---

## 安装与基础配置

```bash
pip install langchain langchain-openai langchain-anthropic langchain-community
pip install langsmith  # 调试用
```

```python
import os
from langchain_openai import ChatOpenAI
from langchain_anthropic import ChatAnthropic

# OpenAI
llm_openai = ChatOpenAI(
    model="gpt-4o-mini",
    temperature=0.1,
    api_key=os.environ["OPENAI_API_KEY"],
)

# Anthropic Claude
llm_claude = ChatAnthropic(
    model="claude-3-5-haiku-20241022",
    temperature=0.1,
    api_key=os.environ["ANTHROPIC_API_KEY"],
)
```

---

## LCEL：LangChain Expression Language

LCEL 是 LangChain v0.2 之后的核心，用管道操作符 `|` 组合组件，取代了早期的 `LLMChain`。

### 基础用法

```python
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_openai import ChatOpenAI

llm = ChatOpenAI(model="gpt-4o-mini")
parser = StrOutputParser()

prompt = ChatPromptTemplate.from_messages([
    ("system", "你是一位代码审查专家，专注于 Python 最佳实践。"),
    ("human", "请审查以下代码并给出改进建议：\n\n{code}"),
])

# 用 | 组合成链
chain = prompt | llm | parser

result = chain.invoke({"code": "def f(x): return x*2"})
print(result)
```

### 并行执行

```python
from langchain_core.runnables import RunnableParallel

# 同时调用多个链，合并结果
parallel_chain = RunnableParallel(
    summary=summary_chain,
    keywords=keyword_chain,
    sentiment=sentiment_chain,
)

results = parallel_chain.invoke({"text": "某段文本内容..."})
# results = {"summary": "...", "keywords": [...], "sentiment": "positive"}
```

### 条件路由

```python
from langchain_core.runnables import RunnableLambda, RunnableBranch

def classify_intent(inputs: dict) -> str:
    """简单意图分类"""
    query = inputs["query"].lower()
    if any(kw in query for kw in ["代码", "函数", "bug", "错误"]):
        return "code"
    elif any(kw in query for kw in ["文档", "报告", "总结"]):
        return "document"
    return "general"

router = RunnableBranch(
    (lambda x: classify_intent(x) == "code", code_chain),
    (lambda x: classify_intent(x) == "document", doc_chain),
    general_chain,  # 默认分支
)

result = router.invoke({"query": "帮我审查这段代码"})
```

### 异步流式输出

```python
import asyncio

async def stream_response(query: str):
    async for chunk in chain.astream({"query": query}):
        print(chunk, end="", flush=True)

# 在 FastAPI 里：
from fastapi.responses import StreamingResponse

async def chat_stream(query: str):
    async def generate():
        async for chunk in chain.astream({"query": query}):
            yield f"data: {chunk}\n\n"
        yield "data: [DONE]\n\n"
    
    return StreamingResponse(generate(), media_type="text/event-stream")
```

---

## 文档加载与 RAG 集成

LangChain 最值钱的部分之一是其丰富的文档加载器：

```python
from langchain_community.document_loaders import (
    PyMuPDFLoader,       # PDF
    UnstructuredWordDocumentLoader,  # Word
    WebBaseLoader,       # 网页
    DirectoryLoader,     # 目录批量加载
    GitLoader,           # Git 仓库代码
)
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_openai import OpenAIEmbeddings
from langchain_community.vectorstores import Qdrant

# 加载文档
loader = DirectoryLoader(
    "./docs",
    glob="**/*.md",
    loader_cls=TextLoader,
    loader_kwargs={"encoding": "utf-8"},
)
documents = loader.load()

# 分块
splitter = RecursiveCharacterTextSplitter(
    chunk_size=512,
    chunk_overlap=50,
)
chunks = splitter.split_documents(documents)

# 向量化并存储
embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
vectorstore = Qdrant.from_documents(
    chunks,
    embeddings,
    url="http://localhost:6333",
    collection_name="docs",
)

# 构建 RAG Chain
retriever = vectorstore.as_retriever(
    search_type="similarity",
    search_kwargs={"k": 5},
)

from langchain_core.prompts import PromptTemplate
from langchain_core.runnables import RunnablePassthrough

rag_prompt = PromptTemplate.from_template("""
基于以下参考资料回答问题。如果资料中没有相关信息，请说"根据现有资料无法回答"。

参考资料：
{context}

问题：{question}

答案：""")

def format_docs(docs):
    return "\n\n".join(doc.page_content for doc in docs)

rag_chain = (
    {"context": retriever | format_docs, "question": RunnablePassthrough()}
    | rag_prompt
    | llm
    | StrOutputParser()
)

answer = rag_chain.invoke("RAG 系统如何选择 chunk size？")
```

---

## ReAct Agent

Agent 是 LangChain 里争议最大的功能：设计思路好，但早期实现不稳定。用 ReAct（Reasoning + Acting）模式的 Agent 相对稳定。

### 定义工具

```python
from langchain_core.tools import tool
from langchain_community.tools import DuckDuckGoSearchRun
import subprocess

@tool
def execute_python(code: str) -> str:
    """在安全沙箱里执行 Python 代码，返回输出结果。
    
    Args:
        code: 要执行的 Python 代码字符串
    """
    try:
        result = subprocess.run(
            ["python3", "-c", code],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout
        else:
            return f"错误：{result.stderr}"
    except subprocess.TimeoutExpired:
        return "执行超时（>10秒）"

@tool
def search_docs(query: str) -> str:
    """从内部知识库检索相关文档。
    
    Args:
        query: 搜索关键词
    """
    docs = retriever.invoke(query)
    return "\n\n".join(doc.page_content for doc in docs[:3])

@tool
def get_current_time() -> str:
    """获取当前日期和时间。"""
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

tools = [execute_python, search_docs, get_current_time]
```

### 创建 Agent

```python
from langchain.agents import create_react_agent, AgentExecutor
from langchain import hub

# 使用标准的 ReAct prompt
react_prompt = hub.pull("hwchase17/react")

agent = create_react_agent(
    llm=ChatOpenAI(model="gpt-4o", temperature=0),
    tools=tools,
    prompt=react_prompt,
)

agent_executor = AgentExecutor(
    agent=agent,
    tools=tools,
    verbose=True,          # 打印推理过程
    max_iterations=10,     # 防止无限循环
    handle_parsing_errors=True,  # 解析失败时重试
)

result = agent_executor.invoke({
    "input": "查一下 RAG 的最佳实践，然后用 Python 生成一个 5 分制的评分卡"
})
```

**Agent 的实际使用建议：**
- 工具数量不超过 8-10 个，太多模型会"选择困难"
- 工具的 docstring 要写清楚，这是模型判断何时调用的依据
- `temperature=0` 对 Agent 很重要，避免随机跳步
- 在生产中总是设置 `max_iterations`，防止费用失控

---

## LangGraph：状态机工作流

对于比简单 Agent 复杂的场景（多步骤、有分支、需要人工审批），LangGraph 是更好的选择。

### 核心概念

LangGraph 把工作流建模为有向图：
- **Node**：执行某个操作的函数
- **Edge**：节点间的连接，可以是条件跳转
- **State**：在图中流转的共享状态

### 实际示例：代码审查工作流

```python
from langgraph.graph import StateGraph, END
from typing import TypedDict, Annotated
import operator

class ReviewState(TypedDict):
    code: str
    issues: list[str]
    severity: str        # low/medium/high
    suggestions: str
    approved: bool
    review_count: int

# 节点1：静态分析
def static_analysis(state: ReviewState) -> ReviewState:
    code = state["code"]
    issues = []
    
    if "print(" in code and "logging" not in code:
        issues.append("使用了 print 而非 logging")
    if "except:" in code:
        issues.append("裸 except 子句，应指定异常类型")
    if len(code.split("\n")) > 100 and "def " not in code:
        issues.append("函数过长，考虑拆分")
    
    return {**state, "issues": issues, "review_count": state.get("review_count", 0) + 1}

# 节点2：LLM 深度审查
def llm_review(state: ReviewState) -> ReviewState:
    issues_str = "\n".join(f"- {i}" for i in state["issues"])
    
    prompt = f"""
审查以下代码，已发现的问题：
{issues_str}

代码：
```python
{state["code"]}
```

请评估严重程度（low/medium/high）和改进建议。
以 JSON 格式返回：{{"severity": "...", "suggestions": "..."}}
"""
    response = llm.invoke(prompt)
    import json
    data = json.loads(response.content)
    
    return {
        **state,
        "severity": data["severity"],
        "suggestions": data["suggestions"],
    }

# 节点3：审批决策
def approval_decision(state: ReviewState) -> ReviewState:
    approved = state["severity"] in ("low",) or len(state["issues"]) == 0
    return {**state, "approved": approved}

# 条件路由
def should_escalate(state: ReviewState) -> str:
    if state["severity"] == "high":
        return "escalate"
    return "approve"

# 构建图
workflow = StateGraph(ReviewState)

workflow.add_node("static_analysis", static_analysis)
workflow.add_node("llm_review", llm_review)
workflow.add_node("approval_decision", approval_decision)

workflow.set_entry_point("static_analysis")
workflow.add_edge("static_analysis", "llm_review")

workflow.add_conditional_edges(
    "llm_review",
    should_escalate,
    {
        "escalate": END,        # 严重问题直接结束，不审批
        "approve": "approval_decision",
    }
)
workflow.add_edge("approval_decision", END)

app = workflow.compile()

# 运行工作流
result = app.invoke({
    "code": "def process(data):\n    print(data)\n    try:\n        pass\n    except:\n        pass",
})

print(f"审批结果: {'通过' if result['approved'] else '拒绝'}")
print(f"严重程度: {result['severity']}")
print(f"改进建议: {result['suggestions']}")
```

### 带人工介入的工作流

LangGraph 支持在节点间插入"等待人工确认"的逻辑：

```python
from langgraph.checkpoint.memory import MemorySaver

# 使用内存 checkpointer（生产用 PostgreSQL）
memory = MemorySaver()
app = workflow.compile(
    checkpointer=memory,
    interrupt_before=["approval_decision"],  # 在这个节点前暂停，等待人工输入
)

config = {"configurable": {"thread_id": "review-001"}}

# 第一步：运行到暂停点
result = app.invoke({"code": "..."}, config=config)
print("等待人工审批...")

# 人工审核后，恢复执行
# 可以修改 state 后继续
app.update_state(config, {"approved": True})  # 人工覆盖决策
final_result = app.invoke(None, config=config)  # None 表示从暂停点继续
```

---

## 与 FastAPI 集成部署

```python
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import asyncio

app = FastAPI()

class ChatRequest(BaseModel):
    message: str
    session_id: str = "default"
    stream: bool = False

class ChatResponse(BaseModel):
    answer: str
    sources: list[str] = []

@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    try:
        result = await rag_chain.ainvoke(request.message)
        return ChatResponse(answer=result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/chat/stream")
async def chat_stream(request: ChatRequest):
    async def generate():
        async for chunk in rag_chain.astream(request.message):
            if chunk:
                yield f"data: {chunk}\n\n"
        yield "data: [DONE]\n\n"
    
    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # 禁用 Nginx 缓冲
        }
    )

@app.post("/agent/run")
async def run_agent(request: ChatRequest):
    """运行 Agent，支持多步骤推理"""
    try:
        result = await asyncio.wait_for(
            agent_executor.ainvoke({"input": request.message}),
            timeout=60.0,  # Agent 运行超时 60 秒
        )
        return {"output": result["output"]}
    except asyncio.TimeoutError:
        raise HTTPException(status_code=408, detail="Agent 执行超时")
```

---

## LangSmith 调试

LangSmith 是 LangChain 官方的可观测性平台，对调试复杂的 Agent/RAG 流程非常有帮助：

```python
import os

# 开启 LangSmith 追踪
os.environ["LANGCHAIN_TRACING_V2"] = "true"
os.environ["LANGCHAIN_API_KEY"] = "ls__xxxx"
os.environ["LANGCHAIN_PROJECT"] = "my-rag-app"

# 之后的所有 LangChain 调用都会自动被追踪
result = rag_chain.invoke("测试问题")
# 在 https://smith.langchain.com 可以看到完整的调用链
```

LangSmith 可以看到：
- 每个节点的输入输出
- Token 消耗统计
- 延迟数据
- 错误堆栈

---

## 不推荐的用法

几个容易挖坑的功能，实际工程中建议绕开：

**1. `ConversationBufferMemory`**：把所有历史都塞进上下文，对话长了之后 token 爆炸。建议自己管理对话历史，只保留最近 N 轮或做摘要压缩。

**2. `SequentialChain`（旧版 Chain 系列）**：LCEL 之前的产物，接口混乱，维护困难。新项目一律用 LCEL。

**3. `initialize_agent` 快捷函数**：隐藏了太多细节，出问题很难调试。建议用 `create_react_agent` + `AgentExecutor` 手动创建。

**4. 过深的 LangChain 抽象**：当你的逻辑变复杂时，与其叠加更多 LangChain 组件，不如退一步用原始 SDK 直接写。LangChain 的价值在于集成（向量库、加载器、工具）和 LCEL 的组合能力，不在于替代所有逻辑。
