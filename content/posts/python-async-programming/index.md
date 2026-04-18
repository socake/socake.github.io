---
title: "Python 异步编程实战：asyncio 在 AI 应用中的使用"
date: 2024-11-22T12:44:00+08:00
draft: false
tags: ["Python", "asyncio", "异步编程", "FastAPI", "AI工程化", "LLM"]
categories: ["编程"]
series: ["AI 工程化实战"]
description: "深入理解 asyncio 核心机制，并发调用多个 LLM API、流式输出处理、异步数据库操作——AI 应用必须掌握的异步编程实战"
summary: "AI 应用天然是 I/O 密集型的：等 LLM 响应、等向量数据库检索、等多个工具调用返回。同步写法在这里是性能杀手。这篇文章从 event loop 原理讲到实际的 AI 应用模式，重点是 asyncio.gather 并发调用、SSE 流式输出处理和常见陷阱排查。"
toc: true
math: false
diagram: false
keywords: ["asyncio", "async/await", "event loop", "coroutine", "asyncio.gather", "SSE流式输出", "FastAPI", "异步数据库"]
params:
  reading_time: true
---

我在做第一个 LLM 应用时犯过一个典型错误：用同步方式调用 OpenAI API，串行处理用户请求。测试的时候没问题，上了 10 个并发用户就开始排队，响应时间从 2 秒飙到 20 秒。

这篇文章从「为什么 AI 应用必须用异步」出发，系统地讲 asyncio 的核心概念和在 AI 应用场景中的实战用法。

## 同步 vs 异步：本质区别

先用一个具体例子说清楚区别。假设你要从 5 个不同的 LLM 获取答案（多模型路由场景）：

**同步做法（线性等待）：**

```python
import time
import openai

def call_llm_sync(model, prompt):
    """同步调用，会阻塞线程"""
    client = openai.OpenAI()
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.choices[0].message.content

def get_multi_model_answers(prompt):
    models = ["gpt-4o", "gpt-4o-mini", "gpt-3.5-turbo"]
    results = []

    start = time.time()
    for model in models:
        result = call_llm_sync(model, prompt)  # 串行等待，每个约 2 秒
        results.append(result)

    print(f"总耗时: {time.time() - start:.1f}s")  # ~6 秒
    return results
```

**异步做法（并发等待）：**

```python
import asyncio
import time
import openai

async def call_llm_async(model, prompt):
    """异步调用，释放线程给其他任务"""
    client = openai.AsyncOpenAI()
    response = await client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.choices[0].message.content

async def get_multi_model_answers(prompt):
    models = ["gpt-4o", "gpt-4o-mini", "gpt-3.5-turbo"]

    start = time.time()
    results = await asyncio.gather(
        *[call_llm_async(model, prompt) for model in models]  # 并发等待
    )

    print(f"总耗时: {time.time() - start:.1f}s")  # ~2 秒（取最慢的那个）
    return results
```

从 6 秒降到 2 秒——这就是异步的价值。

**本质区别**：同步代码在等待 I/O（网络请求、磁盘读写）时，线程会被阻塞，什么都不能做。异步代码在遇到 `await` 时，会把控制权交还给 event loop，让 event loop 去处理其他任务，I/O 完成后再回来继续执行。

## asyncio 核心概念

### Event Loop：单线程的任务调度器

Event loop 是 asyncio 的核心——一个不断循环的调度器，监听各种事件（I/O 完成、定时器到期），并在事件发生时调用对应的回调函数或恢复对应的协程。

```python
import asyncio

# 获取当前 event loop（Python 3.10+）
loop = asyncio.get_event_loop()

# 运行一个协程
asyncio.run(main())  # 这会创建一个新的 event loop，运行完后关闭
```

关键认知：**asyncio 是单线程的**。所有协程在同一个线程中运行，通过主动让出控制权（`await`）来实现并发。这意味着：
- 没有 GIL 问题（本来就单线程）
- 协程之间的切换是协作式的，不是抢占式的
- CPU 密集型任务不适合 asyncio（会阻塞整个 loop）

### Coroutine：可暂停的函数

用 `async def` 定义的函数是协程函数，调用它会返回一个协程对象，不会立即执行。

```python
async def my_coroutine():
    print("开始执行")
    await asyncio.sleep(1)  # 让出控制权 1 秒
    print("继续执行")

# 错误：直接调用不会执行
# my_coroutine()  # 只是创建了一个协程对象，没有运行

# 正确：用 await 或 asyncio.run()
asyncio.run(my_coroutine())
```

### Task：已调度的协程

Task 是把协程包装成可以并发运行的任务。`asyncio.gather` 和 `asyncio.create_task` 都会创建 Task。

```python
async def main():
    # 方式一：create_task 立即调度（不等待）
    task1 = asyncio.create_task(fetch_data("url1"))
    task2 = asyncio.create_task(fetch_data("url2"))
    # 此时 task1 和 task2 已经在运行了

    # 做其他事情...
    await asyncio.sleep(0)  # 给 task1/task2 一个执行机会

    # 最后等待结果
    result1 = await task1
    result2 = await task2

    # 方式二：gather 并发等待
    results = await asyncio.gather(
        fetch_data("url1"),
        fetch_data("url2"),
        return_exceptions=True  # 某个任务失败不影响其他任务
    )
```

## async/await 语法精要和常见错误

### 基础用法

```python
import asyncio
import aiohttp

async def fetch_url(session, url):
    async with session.get(url) as response:  # async with：异步上下文管理器
        return await response.text()

async def fetch_multiple():
    urls = [
        "https://api.example.com/data/1",
        "https://api.example.com/data/2",
        "https://api.example.com/data/3",
    ]

    async with aiohttp.ClientSession() as session:
        tasks = [fetch_url(session, url) for url in urls]
        results = await asyncio.gather(*tasks)
    return results
```

### 常见错误 1：忘记 await

```python
# 错误：没有 await，response 是一个协程对象，不是结果
async def wrong():
    response = client.chat.completions.create(...)  # 忘记 await
    print(response.choices)  # AttributeError: coroutine has no attribute 'choices'

# 正确
async def correct():
    response = await client.chat.completions.create(...)
    print(response.choices[0].message.content)
```

### 常见错误 2：在异步函数中调用同步阻塞函数

```python
import time
import asyncio

# 错误：time.sleep 会阻塞整个 event loop！
async def wrong_sleep():
    await some_async_work()
    time.sleep(2)  # 这 2 秒内，整个程序都被冻结
    await more_async_work()

# 正确：用 asyncio.sleep
async def correct_sleep():
    await some_async_work()
    await asyncio.sleep(2)  # 让出控制权，其他任务可以运行
    await more_async_work()

# 对于无法避免的阻塞调用（如 CPU 密集计算、旧的同步库），用线程池
async def run_blocking_in_thread():
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None,  # 使用默认 ThreadPoolExecutor
        blocking_function,  # 阻塞函数
        arg1, arg2
    )
    return result
```

## 并发调用多个 LLM API：asyncio.gather 实战

这是 AI 应用中最常见的异步模式——同时向多个模型发请求，或者并发执行多个独立的 LLM 任务。

### 多模型并发与结果聚合

```python
import asyncio
import anthropic
from openai import AsyncOpenAI
from typing import Optional

openai_client = AsyncOpenAI()
anthropic_client = anthropic.AsyncAnthropic()

async def call_openai(prompt: str) -> Optional[str]:
    try:
        response = await openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            timeout=30
        )
        return response.choices[0].message.content
    except Exception as e:
        print(f"OpenAI 调用失败: {e}")
        return None

async def call_claude(prompt: str) -> Optional[str]:
    try:
        response = await anthropic_client.messages.create(
            model="claude-3-5-sonnet-20241022",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.content[0].text
    except Exception as e:
        print(f"Claude 调用失败: {e}")
        return None

async def ensemble_query(prompt: str) -> dict:
    """并发调用多个模型，返回所有结果"""
    results = await asyncio.gather(
        call_openai(prompt),
        call_claude(prompt),
        return_exceptions=True  # 不因一个失败而中断其他
    )

    return {
        "gpt4o": results[0] if not isinstance(results[0], Exception) else None,
        "claude": results[1] if not isinstance(results[1], Exception) else None,
    }

# 使用
async def main():
    result = await ensemble_query("用三句话解释量子纠缠")
    for model, answer in result.items():
        print(f"\n=== {model} ===")
        print(answer)

asyncio.run(main())
```

### 带并发限制的批量处理

并发调用 API 时要注意速率限制（Rate Limit），用 `asyncio.Semaphore` 控制并发数：

```python
import asyncio
from openai import AsyncOpenAI

client = AsyncOpenAI()

async def process_single(semaphore: asyncio.Semaphore, text: str) -> str:
    async with semaphore:  # 信号量控制并发数
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": f"总结以下文本：{text}"}],
        )
        return response.choices[0].message.content

async def batch_summarize(texts: list[str], max_concurrency: int = 5) -> list[str]:
    """批量总结，最多 5 个并发请求"""
    semaphore = asyncio.Semaphore(max_concurrency)
    tasks = [process_single(semaphore, text) for text in texts]
    return await asyncio.gather(*tasks, return_exceptions=True)

# 处理 100 条文本，每次最多 5 个并发
async def main():
    texts = [f"这是第 {i} 段需要总结的文本..." for i in range(100)]
    results = await batch_summarize(texts, max_concurrency=5)
    print(f"处理完成，共 {len(results)} 条")
```

## 流式输出（SSE/Streaming）的异步处理

LLM 流式输出是 AI 应用的标配——用户不需要等全部生成完才看到内容，可以逐 token 看到输出。

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

async def stream_response(prompt: str):
    """流式接收 LLM 输出，逐块打印"""
    async with client.messages.stream(
        model="claude-3-5-sonnet-20241022",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}]
    ) as stream:
        async for text in stream.text_stream:
            print(text, end="", flush=True)
    print()  # 换行
    return await stream.get_final_message()

asyncio.run(stream_response("写一首关于异步编程的诗"))
```

### FastAPI 中的 SSE 流式返回

在 Web 应用中，流式输出通常通过 Server-Sent Events（SSE）推送给前端：

```python
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
import anthropic
import json

app = FastAPI()
client = anthropic.AsyncAnthropic()

async def generate_stream(prompt: str):
    """异步生成器：产生 SSE 格式的数据"""
    async with client.messages.stream(
        model="claude-3-5-sonnet-20241022",
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}]
    ) as stream:
        async for text in stream.text_stream:
            # SSE 格式：data: {...}\n\n
            yield f"data: {json.dumps({'text': text, 'done': False})}\n\n"

        # 发送结束信号
        final = await stream.get_final_message()
        usage = {
            "input_tokens": final.usage.input_tokens,
            "output_tokens": final.usage.output_tokens
        }
        yield f"data: {json.dumps({'done': True, 'usage': usage})}\n\n"

@app.post("/chat/stream")
async def chat_stream(request: dict):
    prompt = request.get("prompt", "")
    return StreamingResponse(
        generate_stream(prompt),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # 禁用 Nginx 缓冲
        }
    )
```

前端接收 SSE：

```javascript
const response = await fetch('/chat/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ prompt: userInput })
});

const reader = response.body.getReader();
const decoder = new TextDecoder();

while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    const text = decoder.decode(value);
    const lines = text.split('\n').filter(l => l.startsWith('data: '));

    for (const line of lines) {
        const data = JSON.parse(line.slice(6));
        if (!data.done) {
            appendToOutput(data.text);
        }
    }
}
```

## 异步上下文管理器（async with）

`async with` 用于需要异步初始化和清理的资源，比如数据库连接池、HTTP 会话：

```python
import asyncio
import asyncpg

class DatabaseManager:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.pool = None

    async def __aenter__(self):
        self.pool = await asyncpg.create_pool(
            self.dsn,
            min_size=2,
            max_size=10
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.pool.close()

    async def fetch_user(self, user_id: int) -> dict:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM users WHERE id = $1", user_id
            )
            return dict(row) if row else None

# 使用
async def main():
    async with DatabaseManager("postgresql://user:pass@localhost/db") as db:
        user = await db.fetch_user(123)
        print(user)
```

## 异步数据库操作

AI 应用常用的异步数据库库：

| 数据库 | 同步库 | 异步库 |
|--------|--------|--------|
| PostgreSQL | psycopg2 | asyncpg / psycopg3 |
| MySQL | PyMySQL | aiomysql |
| MongoDB | pymongo | motor |
| Redis | redis-py | aioredis / redis.asyncio |

```python
import asyncio
import asyncpg
from motor.motor_asyncio import AsyncIOMotorClient

# PostgreSQL：存储对话历史
async def save_conversation(pool, session_id: str, messages: list):
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO conversations (session_id, messages, created_at)
            VALUES ($1, $2::jsonb, NOW())
            ON CONFLICT (session_id)
            DO UPDATE SET messages = $2::jsonb, updated_at = NOW()
            """,
            session_id,
            messages  # asyncpg 自动序列化为 JSON
        )

# MongoDB：存储非结构化的 LLM 交互日志
async def log_llm_call(mongo_client, prompt: str, response: str, metadata: dict):
    db = mongo_client.llm_logs
    collection = db.interactions
    await collection.insert_one({
        "prompt": prompt,
        "response": response,
        "metadata": metadata,
        "timestamp": asyncio.get_event_loop().time()
    })

# Redis：缓存 Embedding 结果
import redis.asyncio as aioredis

async def get_or_compute_embedding(redis_client, text: str) -> list[float]:
    cache_key = f"emb:{hash(text)}"

    # 先查缓存
    cached = await redis_client.get(cache_key)
    if cached:
        import json
        return json.loads(cached)

    # 缓存未命中，计算 Embedding
    embedding = await compute_embedding_async(text)

    # 写入缓存，TTL 1 天
    await redis_client.setex(cache_key, 86400, json.dumps(embedding))
    return embedding
```

## FastAPI 异步路由实战

FastAPI 本身是基于 asyncio 的，路由函数声明为 `async def` 就能利用异步优势：

```python
from fastapi import FastAPI, HTTPException, Depends
from contextlib import asynccontextmanager
import asyncpg
import anthropic

# 应用启动时初始化连接池
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时
    app.state.db_pool = await asyncpg.create_pool(
        "postgresql://user:pass@localhost/app_db",
        min_size=5,
        max_size=20
    )
    app.state.llm_client = anthropic.AsyncAnthropic()
    yield
    # 关闭时
    await app.state.db_pool.close()

app = FastAPI(lifespan=lifespan)

async def get_db_pool(request):
    return request.app.state.db_pool

async def get_llm_client(request):
    return request.app.state.llm_client

@app.post("/ask")
async def ask_question(
    request: dict,
    pool: asyncpg.Pool = Depends(get_db_pool),
    llm: anthropic.AsyncAnthropic = Depends(get_llm_client)
):
    question = request.get("question", "")
    user_id = request.get("user_id")

    if not question:
        raise HTTPException(status_code=400, detail="question 不能为空")

    # 并发执行：获取用户历史 + 调用 LLM
    async with pool.acquire() as conn:
        history_task = asyncio.create_task(
            conn.fetch(
                "SELECT role, content FROM chat_history WHERE user_id = $1 ORDER BY created_at DESC LIMIT 10",
                user_id
            )
        )

    # 获取历史记录
    history = await history_task
    messages = [{"role": r["role"], "content": r["content"]} for r in reversed(history)]
    messages.append({"role": "user", "content": question})

    # 调用 LLM
    response = await llm.messages.create(
        model="claude-3-5-sonnet-20241022",
        max_tokens=1024,
        messages=messages
    )
    answer = response.content[0].text

    # 保存对话记录
    async with pool.acquire() as conn:
        await asyncio.gather(
            conn.execute(
                "INSERT INTO chat_history (user_id, role, content) VALUES ($1, 'user', $2)",
                user_id, question
            ),
            conn.execute(
                "INSERT INTO chat_history (user_id, role, content) VALUES ($1, 'assistant', $2)",
                user_id, answer
            )
        )

    return {"answer": answer}
```

## 常见陷阱排查

### 陷阱 1：在同步代码中运行异步函数

```python
import asyncio

async def async_func():
    await asyncio.sleep(1)
    return "done"

# 错误：在普通函数中直接 await
def sync_caller():
    result = await async_func()  # SyntaxError！

# 常见误区：在 Jupyter Notebook 中（有自己的 event loop）
# asyncio.run(async_func())  # RuntimeError: cannot run nested event loop

# Notebook 中正确做法：直接 await（Jupyter 支持顶层 await）
# result = await async_func()

# 在普通同步代码中调用异步函数：
result = asyncio.run(async_func())  # 创建新 loop 运行
```

### 陷阱 2：未捕获的异步异常

```python
import asyncio

async def failing_task():
    await asyncio.sleep(0.1)
    raise ValueError("任务失败")

# 危险：异常被静默丢弃
async def dangerous():
    task = asyncio.create_task(failing_task())
    # 如果不 await task，异常会变成 unhandled exception warning
    await asyncio.sleep(1)  # task 已经失败，但没人知道

# 安全做法：设置异常回调或及时 await
async def safe():
    task = asyncio.create_task(failing_task())
    task.add_done_callback(
        lambda t: print(f"任务失败: {t.exception()}") if t.exception() else None
    )
    await asyncio.sleep(1)
```

### 陷阱 3：asyncio.gather 中一个失败导致其他取消

```python
import asyncio

async def task_a():
    await asyncio.sleep(1)
    return "A 完成"

async def task_b():
    await asyncio.sleep(0.5)
    raise RuntimeError("B 失败")

# 默认行为：B 失败会让 gather 抛出异常，A 可能被取消
async def wrong():
    try:
        results = await asyncio.gather(task_a(), task_b())
    except RuntimeError as e:
        print(f"异常: {e}")  # A 的结果丢失了

# 正确：return_exceptions=True 让失败作为返回值
async def correct():
    results = await asyncio.gather(task_a(), task_b(), return_exceptions=True)
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            print(f"任务 {i} 失败: {result}")
        else:
            print(f"任务 {i} 成功: {result}")

asyncio.run(correct())
```

### 调试技巧

```python
import asyncio
import logging

# 开启 asyncio 调试模式，检测慢速协程和未等待的协程
asyncio.run(main(), debug=True)

# 或在环境变量中设置
# PYTHONASYNCIODEBUG=1 python your_app.py

# 用 asyncio.current_task() 追踪当前任务
async def trace_task():
    task = asyncio.current_task()
    print(f"当前任务: {task.get_name()}")

# 检测 event loop 是否被阻塞（超过 100ms 视为问题）
import time

async def monitor_loop_latency():
    while True:
        start = time.monotonic()
        await asyncio.sleep(0.1)
        elapsed = time.monotonic() - start
        if elapsed > 0.2:  # 超过预期的 2 倍
            print(f"警告：event loop 延迟 {elapsed*1000:.0f}ms，可能有阻塞调用")
```

asyncio 的学习曲线不假，但一旦接受了 event loop 的协作式调度模型，后面的坑基本都能按图索骥。AI 应用大头就是 I/O 等待，从同步改异步通常一毛钱资源不加、并发就能翻 5-10 倍。
