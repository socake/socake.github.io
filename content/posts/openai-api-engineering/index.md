---
title: "OpenAI API 工程化实践：从 Hello World 到生产"
date: 2026-04-12T14:00:00+08:00
draft: false
tags: ["OpenAI", "大模型", "API开发", "Function Calling", "Structured Output", "Batch API", "Embeddings"]
categories: ["大模型"]
description: "OpenAI API 生产级实践：Chat Completions vs Assistants API选择、Function Calling、Structured Output、Batch API、成本优化策略和完整错误处理。"
summary: "OpenAI API 是大多数 LLM 应用开发者的起点，但从 Hello World 到真正可靠的生产系统，中间有很多工程细节需要处理。本文覆盖 Function Calling、Structured Output、Batch API、Embeddings 的完整实践，以及速率限制、错误处理和成本控制的系统方案。"
toc: true
math: false
diagram: false
keywords: ["OpenAI API", "Function Calling", "Structured Output", "Batch API", "Embeddings", "Chat Completions", "GPT-5.4", "成本优化"]
params:
  reading_time: true
---

OpenAI API 是目前文档最完善、生态最丰富的 LLM API。但它也是"坑"最多的——从版本兼容问题到速率限制，从成本失控到 Assistants API 的复杂性，不少团队在生产中都踩过。

本文专注工程实践，覆盖从基础调用到生产部署的完整链路。

---

## 安装与客户端配置

```bash
pip install openai
```

```python
from openai import OpenAI
import os

client = OpenAI(
    api_key=os.environ.get("OPENAI_API_KEY"),
    # 可选：通过 Azure OpenAI
    # api_key=os.environ.get("AZURE_OPENAI_KEY"),
    # azure_endpoint=os.environ.get("AZURE_OPENAI_ENDPOINT"),
    # api_version="2024-02-01",
    
    # 超时配置（建议显式设置）
    timeout=30.0,
    max_retries=3,
)
```

---

## Chat Completions vs Assistants API

这是很多人的第一个困惑：什么时候用哪个？

### Chat Completions API（推荐首选）

无状态，你管理所有状态：

```python
response = client.chat.completions.create(
    model="gpt-5.4-mini",
    messages=[
        {"role": "system", "content": "你是一个代码助手"},
        {"role": "user", "content": "写一个快速排序"},
    ],
    temperature=0.1,
    max_tokens=2048,
)

print(response.choices[0].message.content)
print(f"usage: {response.usage}")
```

**适合场景**：
- 单次问答、文本处理
- 自己管理对话历史
- 需要可预测的行为和成本
- 大多数生产应用

### Assistants API

有状态，OpenAI 管理 Thread（对话历史）：

```python
# 创建 Assistant（一次性，持久化）
assistant = client.beta.assistants.create(
    name="代码审查助手",
    instructions="你是专业的代码审查工程师...",
    model="gpt-5.4",
    tools=[{"type": "code_interpreter"}],  # 内置代码执行
)

# 创建对话 Thread
thread = client.beta.threads.create()

# 发送消息
client.beta.threads.messages.create(
    thread_id=thread.id,
    role="user",
    content="审查这段代码：..."
)

# 运行（异步，需要轮询）
run = client.beta.threads.runs.create_and_poll(
    thread_id=thread.id,
    assistant_id=assistant.id,
)

if run.status == "completed":
    messages = client.beta.threads.messages.list(thread_id=thread.id)
    print(messages.data[0].content[0].text.value)
```

**适合场景**：
- 需要 Code Interpreter（代码执行沙箱）
- 需要内置的文件搜索（RAG 功能）
- 长期多会话场景且不想自己管理状态

**Assistants API 的缺点**：
- 状态在 OpenAI 服务器端，排查问题困难
- 成本不透明（Thread 存储也收费）
- 延迟比 Chat Completions 高
- 对话历史无法精确控制

**结论**：除非你需要 Code Interpreter 或内置 File Search，否则一律用 Chat Completions，自己管理状态。新项目如需有状态对话管理，建议评估 **Responses API**（OpenAI 新一代接口，取代部分 Assistants API 场景，延迟更低、状态管理更灵活）。

---

## Function Calling 详解

Function Calling 是让 LLM 与外部系统交互的标准方式。

### 基础用法

```python
import json

# 定义工具
tools = [
    {
        "type": "function",
        "function": {
            "name": "get_stock_price",
            "description": "获取股票的实时价格",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {
                        "type": "string",
                        "description": "股票代码，如 AAPL, GOOGL"
                    },
                    "currency": {
                        "type": "string",
                        "enum": ["USD", "CNY"],
                        "description": "返回价格的货币单位",
                    }
                },
                "required": ["symbol"],
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "calculate_portfolio_value",
            "description": "计算投资组合的总价值",
            "parameters": {
                "type": "object",
                "properties": {
                    "holdings": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "symbol": {"type": "string"},
                                "shares": {"type": "number"},
                            }
                        },
                        "description": "持仓列表"
                    }
                },
                "required": ["holdings"],
            }
        }
    }
]

# 工具实现
def get_stock_price(symbol: str, currency: str = "USD") -> dict:
    # 实际项目里调用行情 API
    prices = {"AAPL": 195.5, "GOOGL": 175.2, "MSFT": 420.0}
    price = prices.get(symbol.upper(), 0)
    if currency == "CNY":
        price *= 7.2
    return {"symbol": symbol, "price": price, "currency": currency}

def calculate_portfolio_value(holdings: list[dict]) -> dict:
    total = sum(
        get_stock_price(h["symbol"])["price"] * h["shares"]
        for h in holdings
    )
    return {"total_value": round(total, 2), "currency": "USD"}

FUNCTIONS = {
    "get_stock_price": get_stock_price,
    "calculate_portfolio_value": calculate_portfolio_value,
}

# 完整的 Function Calling 循环
def run_with_tools(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    
    while True:
        response = client.chat.completions.create(
            model="gpt-5.4",
            messages=messages,
            tools=tools,
            tool_choice="auto",
        )
        
        choice = response.choices[0]
        messages.append(choice.message)  # 把 assistant 消息加入历史
        
        if choice.finish_reason == "stop":
            return choice.message.content
        
        elif choice.finish_reason == "tool_calls":
            # 执行所有工具调用
            for tool_call in choice.message.tool_calls:
                func_name = tool_call.function.name
                func_args = json.loads(tool_call.function.arguments)
                
                print(f"调用: {func_name}({func_args})")
                
                result = FUNCTIONS[func_name](**func_args)
                
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": json.dumps(result, ensure_ascii=False),
                })
        else:
            break
    
    return "无法完成请求"


result = run_with_tools("我持有 100 股 AAPL 和 50 股 GOOGL，总价值是多少？")
print(result)
```

### Parallel Tool Calls

gpt-5.4 支持同时调用多个工具，可以并行执行：

```python
import asyncio

async def execute_tool_calls_parallel(tool_calls: list) -> list:
    """并行执行多个工具调用"""
    async def execute_single(tool_call):
        func_name = tool_call.function.name
        func_args = json.loads(tool_call.function.arguments)
        
        # 如果工具是异步的，直接 await
        if asyncio.iscoroutinefunction(FUNCTIONS[func_name]):
            result = await FUNCTIONS[func_name](**func_args)
        else:
            # 同步工具在线程池里运行
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None, lambda: FUNCTIONS[func_name](**func_args)
            )
        
        return {
            "role": "tool",
            "tool_call_id": tool_call.id,
            "content": json.dumps(result, ensure_ascii=False),
        }
    
    return await asyncio.gather(*[execute_single(tc) for tc in tool_calls])
```

---

## Structured Output（JSON Schema 绑定）

OpenAI 的 Structured Output 功能保证输出严格符合给定的 JSON Schema：

### 使用 Pydantic 模型

```python
from pydantic import BaseModel, Field
from typing import Literal
from openai import OpenAI

client = OpenAI()

class BugReport(BaseModel):
    severity: Literal["critical", "high", "medium", "low"]
    component: str = Field(description="出现 bug 的组件或模块")
    description: str = Field(description="问题描述，50字以内")
    reproduction_steps: list[str] = Field(description="复现步骤")
    suggested_fix: str | None = Field(description="建议的修复方案，如果无法确定则为 null")

class CodeReviewResult(BaseModel):
    overall_score: int = Field(ge=1, le=10, description="代码质量评分 1-10")
    bugs: list[BugReport]
    style_issues: list[str] = Field(description="代码风格问题列表")
    approved: bool

response = client.beta.chat.completions.parse(
    model="gpt-5.4",  # gpt-5.4 及以上支持 Structured Output
    messages=[
        {"role": "system", "content": "你是代码审查专家，按照要求的格式输出审查结果。"},
        {"role": "user", "content": f"审查以下代码：\n\n```python\n{code_to_review}\n```"}
    ],
    response_format=CodeReviewResult,
)

result = response.choices[0].message.parsed
print(f"评分: {result.overall_score}/10")
print(f"发现 {len(result.bugs)} 个 bug")
for bug in result.bugs:
    print(f"  [{bug.severity.upper()}] {bug.description}")
```

### 处理 refusal

模型可能拒绝生成某些内容：

```python
response = client.beta.chat.completions.parse(
    model="gpt-5.4",
    messages=[...],
    response_format=MySchema,
)

choice = response.choices[0]
if choice.message.refusal:
    # 模型拒绝了请求
    print(f"模型拒绝: {choice.message.refusal}")
else:
    result = choice.message.parsed
```

---

## Embeddings API

Embedding 用于将文本转换为向量，是 RAG 系统的基础。

```python
# 单条 embedding
response = client.embeddings.create(
    model="text-embedding-3-small",
    input="这是需要向量化的文本",
    encoding_format="float",
)

embedding = response.data[0].embedding  # list[float]，维度 1536
print(f"向量维度: {len(embedding)}")

# 批量 embedding（更高效）
texts = ["文本1", "文本2", "文本3", ...]

response = client.embeddings.create(
    model="text-embedding-3-small",
    input=texts,  # 最多 2048 条
)

embeddings = [item.embedding for item in response.data]
```

### 降维节省成本

text-embedding-3 系列支持降维，减少存储和计算成本：

```python
# 使用 256 维代替默认 1536 维（quality 略降，成本和检索速度大幅改善）
response = client.embeddings.create(
    model="text-embedding-3-small",
    input=texts,
    dimensions=256,  # 降维
)
```

### 计算语义相似度

```python
import numpy as np

def cosine_similarity(a: list[float], b: list[float]) -> float:
    a, b = np.array(a), np.array(b)
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))

# 示例：找最相似的文档
def find_most_similar(query: str, documents: list[str]) -> tuple[str, float]:
    all_texts = [query] + documents
    response = client.embeddings.create(
        model="text-embedding-3-small",
        input=all_texts,
    )
    
    embeddings = [item.embedding for item in response.data]
    query_emb = embeddings[0]
    doc_embs = embeddings[1:]
    
    similarities = [cosine_similarity(query_emb, doc_emb) for doc_emb in doc_embs]
    best_idx = max(range(len(similarities)), key=lambda i: similarities[i])
    
    return documents[best_idx], similarities[best_idx]
```

---

## Batch API：批量处理降本 50%

对于不需要实时响应的任务（离线标注、批量摘要、数据处理），Batch API 价格是普通 API 的一半，且有 24 小时的处理窗口。

```python
import json
from pathlib import Path

# 准备批量请求文件（JSONL 格式）
requests = []
for idx, text in enumerate(documents_to_summarize):
    requests.append({
        "custom_id": f"doc-{idx}",
        "method": "POST",
        "url": "/v1/chat/completions",
        "body": {
            "model": "gpt-5.4-mini",
            "max_tokens": 200,
            "messages": [
                {"role": "system", "content": "用一句话总结以下文本"},
                {"role": "user", "content": text}
            ]
        }
    })

# 写入 JSONL 文件
batch_file_path = Path("/tmp/batch_requests.jsonl")
with batch_file_path.open("w") as f:
    for req in requests:
        f.write(json.dumps(req, ensure_ascii=False) + "\n")

# 上传文件
with batch_file_path.open("rb") as f:
    batch_file = client.files.create(file=f, purpose="batch")

# 创建批量任务
batch = client.batches.create(
    input_file_id=batch_file.id,
    endpoint="/v1/chat/completions",
    completion_window="24h",
    metadata={"description": "文档摘要批量处理"},
)

print(f"Batch ID: {batch.id}")
print(f"状态: {batch.status}")
```

```python
import time

def wait_for_batch(batch_id: str, poll_interval: int = 60) -> dict:
    """等待 Batch 任务完成并返回结果"""
    while True:
        batch = client.batches.retrieve(batch_id)
        print(f"状态: {batch.status}, 完成: {batch.request_counts.completed}/{batch.request_counts.total}")
        
        if batch.status == "completed":
            # 下载结果
            result_file = client.files.content(batch.output_file_id)
            results = {}
            for line in result_file.text.strip().split("\n"):
                result = json.loads(line)
                custom_id = result["custom_id"]
                if result["error"] is None:
                    content = result["response"]["body"]["choices"][0]["message"]["content"]
                    results[custom_id] = content
                else:
                    results[custom_id] = None
                    print(f"请求 {custom_id} 失败: {result['error']}")
            return results
        
        elif batch.status in ("failed", "expired", "cancelled"):
            raise RuntimeError(f"Batch 任务失败: {batch.status}")
        
        time.sleep(poll_interval)


results = wait_for_batch(batch.id)
```

---

## 流式输出

```python
# 同步流式
with client.chat.completions.stream(
    model="gpt-5.4-mini",
    messages=[{"role": "user", "content": "写一篇关于云原生的文章"}],
) as stream:
    for text in stream.text_stream:
        print(text, end="", flush=True)

# 获取最终统计
final = stream.get_final_completion()
print(f"\nTokens: {final.usage}")
```

```python
# 异步流式（FastAPI 集成）
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from openai import AsyncOpenAI

async_client = AsyncOpenAI()
app = FastAPI()

@app.post("/chat/stream")
async def chat_stream(message: str):
    async def generate():
        async with async_client.chat.completions.stream(
            model="gpt-5.4-mini",
            messages=[{"role": "user", "content": message}],
        ) as stream:
            async for text in stream.text_stream:
                yield f"data: {text}\n\n"
        yield "data: [DONE]\n\n"
    
    return StreamingResponse(generate(), media_type="text/event-stream")
```

---

## 错误处理与重试

```python
import time
import logging
from openai import (
    OpenAI,
    RateLimitError,
    APIConnectionError,
    APITimeoutError,
    APIStatusError,
    AuthenticationError,
    BadRequestError,
)

logger = logging.getLogger(__name__)

class OpenAIClient:
    def __init__(self):
        self.client = OpenAI(
            max_retries=0,  # 关闭 SDK 内置重试，自己控制
            timeout=30.0,
        )
    
    def chat(
        self,
        messages: list[dict],
        model: str = "gpt-5.4-mini",
        max_retries: int = 5,
        **kwargs
    ) -> str:
        for attempt in range(max_retries):
            try:
                response = self.client.chat.completions.create(
                    model=model,
                    messages=messages,
                    **kwargs
                )
                return response.choices[0].message.content
            
            except AuthenticationError:
                # API Key 无效，不重试
                logger.error("OpenAI API Key 无效")
                raise
            
            except BadRequestError as e:
                # 请求格式错误，不重试
                logger.error(f"请求格式错误: {e}")
                raise
            
            except RateLimitError as e:
                if attempt == max_retries - 1:
                    raise
                # 429 错误，等待后重试
                retry_after = int(
                    e.response.headers.get("x-ratelimit-reset-requests", "10")
                    if hasattr(e, 'response') and e.response else "10"
                )
                wait = min(retry_after, 2 ** attempt * 5)
                logger.warning(f"速率限制，等待 {wait}s (attempt {attempt + 1})")
                time.sleep(wait)
            
            except (APIConnectionError, APITimeoutError) as e:
                if attempt == max_retries - 1:
                    raise
                wait = 2 ** attempt
                logger.warning(f"连接/超时错误，{wait}s 后重试: {e}")
                time.sleep(wait)
            
            except APIStatusError as e:
                if e.status_code >= 500:
                    if attempt == max_retries - 1:
                        raise
                    wait = 2 ** attempt * 5
                    logger.warning(f"服务器错误 {e.status_code}，{wait}s 后重试")
                    time.sleep(wait)
                else:
                    raise
```

---

## 成本优化技巧

### 1. 选择合适的模型

```python
# 根据任务复杂度自动选择模型
# 注意：GPT-4o 已于 2026 年 2 月 13 日退役，请使用 gpt-5.4 系列
MODEL_ROUTING = {
    "classification": "gpt-5.4-nano",   # 最轻量，适合简单分类
    "extraction": "gpt-5.4-mini",
    "summarization": "gpt-5.4-mini",
    "code_generation": "gpt-5.4",       # 旗舰，参考官方最新定价
    "complex_reasoning": "o4-mini",     # 推理模型，取代旧版 o1-mini
    "analysis": "gpt-5.4",
}
```

### 2. 精确控制 max_tokens

```python
import tiktoken

def count_tokens(text: str, model: str = "gpt-5.4") -> int:
    encoder = tiktoken.encoding_for_model(model)
    return len(encoder.encode(text))

def estimate_output_tokens(task_type: str) -> int:
    """根据任务类型估算输出 tokens"""
    estimates = {
        "classification": 10,
        "extraction": 100,
        "summarization": 200,
        "code_generation": 1000,
    }
    return estimates.get(task_type, 500)
```

### 3. 缓存重复请求

```python
import hashlib
import json
from functools import lru_cache

class CachedOpenAIClient:
    def __init__(self):
        self.client = OpenAI()
        self._cache = {}  # 生产中用 Redis
    
    def chat(self, messages: list[dict], **kwargs) -> str:
        # 生成缓存键
        cache_key = hashlib.md5(
            json.dumps({"messages": messages, **kwargs}, sort_keys=True).encode()
        ).hexdigest()
        
        if cache_key in self._cache:
            return self._cache[cache_key]
        
        response = self.client.chat.completions.create(
            messages=messages, **kwargs
        )
        result = response.choices[0].message.content
        
        # 缓存结果（对于确定性任务，temperature=0 的结果可以缓存）
        if kwargs.get("temperature", 1.0) == 0:
            self._cache[cache_key] = result
        
        return result
```

### 4. 监控成本

```python
from dataclasses import dataclass, field
from collections import defaultdict

@dataclass
class UsageTracker:
    model_usage: dict = field(default_factory=lambda: defaultdict(lambda: {"input": 0, "output": 0}))
    
    # 价格以官方最新定价为准（https://openai.com/pricing）
    # GPT-4o 已于 2026 年 2 月 13 日退役
    PRICES = {
        "gpt-5.4": {"input": 0, "output": 0},        # 参考官方最新定价
        "gpt-5.4-mini": {"input": 0, "output": 0},   # 参考官方最新定价
        "gpt-5.4-nano": {"input": 0, "output": 0},   # 参考官方最新定价
        "o3": {"input": 0, "output": 0},              # 参考官方最新定价
        "o4-mini": {"input": 0, "output": 0},         # 参考官方最新定价
        "text-embedding-3-small": {"input": 0.02, "output": 0},
    }
    
    def record(self, model: str, input_tokens: int, output_tokens: int):
        self.model_usage[model]["input"] += input_tokens
        self.model_usage[model]["output"] += output_tokens
    
    def total_cost(self) -> float:
        total = 0
        for model, usage in self.model_usage.items():
            prices = self.PRICES.get(model, {"input": 0, "output": 0})
            total += usage["input"] * prices["input"] / 1_000_000
            total += usage["output"] * prices["output"] / 1_000_000
        return total
    
    def report(self):
        print(f"{'模型':<25} {'输入':<12} {'输出':<12} {'费用':<10}")
        print("-" * 60)
        for model, usage in self.model_usage.items():
            prices = self.PRICES.get(model, {"input": 0, "output": 0})
            cost = (
                usage["input"] * prices["input"] / 1_000_000 +
                usage["output"] * prices["output"] / 1_000_000
            )
            print(f"{model:<25} {usage['input']:<12} {usage['output']:<12} ${cost:.4f}")
        print(f"\n总费用: ${self.total_cost():.4f}")

tracker = UsageTracker()
```

---

## 完整生产示例：文档问答系统

```python
from openai import OpenAI
from pathlib import Path
import json

class DocumentQA:
    def __init__(self):
        self.client = OpenAI()
        self.documents = []
        self.embeddings = []
    
    def add_document(self, text: str, metadata: dict = None):
        response = self.client.embeddings.create(
            model="text-embedding-3-small",
            input=text,
        )
        self.documents.append({"text": text, "metadata": metadata or {}})
        self.embeddings.append(response.data[0].embedding)
    
    def query(self, question: str, top_k: int = 3) -> str:
        import numpy as np
        
        # Embed 问题
        q_response = self.client.embeddings.create(
            model="text-embedding-3-small",
            input=question,
        )
        q_emb = np.array(q_response.data[0].embedding)
        
        # 计算相似度，取 top_k
        all_embs = np.array(self.embeddings)
        similarities = all_embs @ q_emb / (
            np.linalg.norm(all_embs, axis=1) * np.linalg.norm(q_emb)
        )
        top_indices = np.argsort(similarities)[::-1][:top_k]
        
        # 组装上下文
        context = "\n\n---\n\n".join(
            self.documents[i]["text"] for i in top_indices
        )
        
        # 调用 Chat Completions
        response = self.client.chat.completions.create(
            model="gpt-5.4-mini",
            temperature=0,
            max_tokens=500,
            messages=[
                {
                    "role": "system",
                    "content": "基于提供的文档回答问题。如果文档中没有相关信息，明确说明。"
                },
                {
                    "role": "user",
                    "content": f"文档内容：\n{context}\n\n问题：{question}"
                }
            ]
        )
        
        return response.choices[0].message.content


# 使用示例
qa = DocumentQA()
qa.add_document("Kubernetes 是一个容器编排系统...", {"source": "k8s-intro.md"})
qa.add_document("RAG 系统通过检索增强生成质量...", {"source": "rag-guide.md"})

answer = qa.query("什么是 RAG？")
print(answer)
```
