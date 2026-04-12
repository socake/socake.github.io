---
title: "Claude API 开发完全指南：从调用到生产应用"
date: 2026-02-24T11:26:00+08:00
draft: false
tags: ["Claude", "Anthropic", "大模型", "API开发", "Tool Use", "Prompt Caching", "Vision"]
categories: ["大模型"]
description: "Anthropic Claude API 完整开发指南：Messages API、流式输出、Tool Use、Vision、Prompt Caching，以及生产级错误处理和成本优化。"
summary: "Claude API 的设计哲学和 OpenAI 有些不同，但一旦理解其模式，就会发现它在长文本、代码生成和工具调用上非常可靠。本文覆盖从 SDK 配置到 Prompt Caching、Tool Use、Vision 的完整开发实践，以及生产中的错误处理与成本控制策略。"
toc: true
math: false
diagram: false
series: ["AI 工程化实战"]
keywords: ["Claude API", "Anthropic SDK", "Tool Use", "Prompt Caching", "Vision", "流式输出", "Messages API"]
params:
  reading_time: true
---

用 Claude API 的时间越长，越能感受到它和 OpenAI API 在设计理念上的差别：Claude 更强调"遵循指令"，在代码和长文本任务上更稳定，同时它的 Prompt Caching 机制是目前主流 API 里最成熟的成本优化工具之一。

本文从工程实践角度覆盖 Claude API 的完整用法，重点放在 Python 示例和生产踩坑。

---

## 安装与基础配置

```bash
pip install anthropic
```

```python
import anthropic
import os

client = anthropic.Anthropic(
    api_key=os.environ.get("ANTHROPIC_API_KEY"),  # 从环境变量读取，不要硬编码
    # 可选配置：
    # base_url="https://api.anthropic.com",
    # max_retries=3,
    # timeout=60.0,
)
```

---

## Messages API 详解

### 基础调用

```python
message = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=1024,
    messages=[
        {"role": "user", "content": "请解释什么是 B 树，并给出 Python 实现示例"}
    ]
)

print(message.content[0].text)
print(f"输入 tokens: {message.usage.input_tokens}")
print(f"输出 tokens: {message.usage.output_tokens}")
```

### System Prompt

Claude 的 system prompt 是独立参数，不在 messages 列表里：

```python
message = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=2048,
    system="""你是一名资深 Python 工程师，专注于代码质量和性能优化。
    
你的代码风格：
- 遵循 PEP 8 规范
- 添加类型注解
- 编写清晰的 docstring
- 优先考虑可读性

当提供代码示例时，包含必要的错误处理和注释。""",
    messages=[
        {"role": "user", "content": "写一个异步 HTTP 客户端，支持重试和超时控制"}
    ]
)
```

### 多轮对话

Claude 不保存会话状态，需要客户端维护历史：

```python
class ClaudeConversation:
    def __init__(self, system: str = "", model: str = "claude-haiku-4-5-20251001"):
        self.client = anthropic.Anthropic()
        self.model = model
        self.system = system
        self.messages = []
    
    def chat(self, user_message: str) -> str:
        self.messages.append({"role": "user", "content": user_message})
        
        response = self.client.messages.create(
            model=self.model,
            max_tokens=2048,
            system=self.system,
            messages=self.messages,
        )
        
        assistant_message = response.content[0].text
        self.messages.append({"role": "assistant", "content": assistant_message})
        
        return assistant_message
    
    def truncate_history(self, max_turns: int = 10):
        """保留最近 N 轮对话，避免 token 超限"""
        if len(self.messages) > max_turns * 2:
            # 保留最近的对话
            self.messages = self.messages[-(max_turns * 2):]


# 使用示例
conv = ClaudeConversation(
    system="你是一个代码助手",
    model="claude-sonnet-4-6"
)

print(conv.chat("帮我写一个快速排序"))
print(conv.chat("改成迭代而非递归实现"))
print(conv.chat("添加单元测试"))
```

---

## 流式输出

对于需要实时显示结果的场景（聊天界面、长文本生成），流式输出可以大幅改善用户体验：

```python
# 同步流式
with client.messages.stream(
    model="claude-sonnet-4-6",
    max_tokens=2048,
    messages=[{"role": "user", "content": "写一篇关于 Kubernetes 网络模型的技术文章"}],
) as stream:
    for text in stream.text_stream:
        print(text, end="", flush=True)
    
    # 获取最终的完整 message（包含 usage 统计）
    final_message = stream.get_final_message()
    print(f"\n\n总 tokens: {final_message.usage.input_tokens + final_message.usage.output_tokens}")
```

```python
# 异步流式（用于 FastAPI 等异步框架）
import asyncio

async def stream_claude_response(prompt: str):
    async with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        async for text in stream.text_stream:
            yield text

# FastAPI 集成
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

app = FastAPI()

@app.post("/chat/stream")
async def chat_stream(query: str):
    async def generate():
        async for chunk in stream_claude_response(query):
            yield f"data: {chunk}\n\n"
        yield "data: [DONE]\n\n"
    
    return StreamingResponse(generate(), media_type="text/event-stream")
```

---

## Tool Use（函数调用）

Claude 的 Tool Use 是其最强大的能力之一，支持复杂的多工具调用场景。

### 定义工具

```python
tools = [
    {
        "name": "get_weather",
        "description": "获取指定城市的当前天气信息",
        "input_schema": {
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "description": "城市名称，如 '北京', '上海'"
                },
                "unit": {
                    "type": "string",
                    "enum": ["celsius", "fahrenheit"],
                    "description": "温度单位",
                }
            },
            "required": ["city"],
        }
    },
    {
        "name": "search_knowledge_base",
        "description": "在内部知识库中搜索技术文档",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词"
                },
                "top_k": {
                    "type": "integer",
                    "description": "返回结果数量，默认 5",
                    "default": 5,
                }
            },
            "required": ["query"],
        }
    }
]
```

### 完整的工具调用循环

```python
import json
from typing import Any

# 工具实现函数
def get_weather(city: str, unit: str = "celsius") -> dict:
    # 实际项目里调用天气 API
    return {"city": city, "temperature": 22, "unit": unit, "condition": "晴天"}

def search_knowledge_base(query: str, top_k: int = 5) -> list[dict]:
    # 实际项目里调用向量检索
    return [{"content": f"关于 {query} 的文档内容...", "score": 0.9}]

TOOL_FUNCTIONS = {
    "get_weather": get_weather,
    "search_knowledge_base": search_knowledge_base,
}

def run_tool_loop(user_message: str, max_rounds: int = 5) -> str:
    """执行工具调用循环，直到模型给出最终答案"""
    messages = [{"role": "user", "content": user_message}]
    
    for round_num in range(max_rounds):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            tools=tools,
            messages=messages,
        )
        
        # 检查停止原因
        if response.stop_reason == "end_turn":
            # 模型给出了最终答案
            for block in response.content:
                if hasattr(block, "text"):
                    return block.text
        
        elif response.stop_reason == "tool_use":
            # 模型想要调用工具
            messages.append({
                "role": "assistant",
                "content": response.content
            })
            
            # 执行所有工具调用
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    print(f"调用工具: {block.name}({block.input})")
                    
                    tool_fn = TOOL_FUNCTIONS.get(block.name)
                    if tool_fn:
                        try:
                            result = tool_fn(**block.input)
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": json.dumps(result, ensure_ascii=False),
                            })
                        except Exception as e:
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": f"工具执行错误: {str(e)}",
                                "is_error": True,
                            })
            
            messages.append({"role": "user", "content": tool_results})
        
        else:
            break
    
    return "未能获得答案"


result = run_tool_loop("北京今天天气怎么样？帮我查一下 RAG 的相关文档")
print(result)
```

### 强制工具调用

有时需要强制模型调用某个工具（如强制输出 JSON）：

```python
response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=1024,
    tools=tools,
    tool_choice={"type": "tool", "name": "search_knowledge_base"},  # 强制调用
    messages=[{"role": "user", "content": "查找关于 Kubernetes 网络的文档"}],
)
```

---

## Vision：图像理解

Claude 支持直接分析图片内容：

```python
import base64
from pathlib import Path

def analyze_image_from_file(image_path: str, prompt: str) -> str:
    """分析本地图片文件"""
    image_data = base64.standard_b64encode(
        Path(image_path).read_bytes()
    ).decode("utf-8")
    
    # 根据扩展名确定 media_type
    ext_to_media = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }
    suffix = Path(image_path).suffix.lower()
    media_type = ext_to_media.get(suffix, "image/jpeg")
    
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": image_data,
                        },
                    },
                    {
                        "type": "text",
                        "text": prompt,
                    }
                ],
            }
        ],
    )
    return message.content[0].text

# 分析 URL 图片（不需要下载）
def analyze_image_from_url(url: str, prompt: str) -> str:
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "url", "url": url},
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    )
    return message.content[0].text


# 实际应用示例：分析错误截图
result = analyze_image_from_file(
    "/tmp/error_screenshot.png",
    "这是一个应用报错截图，请分析错误原因并给出解决方案"
)
print(result)
```

---

## Extended Thinking：复杂推理场景

Claude Opus 4.6 和 Sonnet 4.6 支持 extended thinking，模型在给出最终答案前会进行更深入的推理过程。适合数学证明、复杂代码架构设计、多步骤分析等场景。

```python
# 启用 extended thinking（需要 claude-opus-4-6 或 claude-sonnet-4-6）
response = client.messages.create(
    model="claude-opus-4-6",
    max_tokens=16000,
    thinking={"type": "enabled", "budget_tokens": 10000},  # 给模型最多 10k tokens 用于思考
    messages=[{"role": "user", "content": "设计一个支持百万并发的消息队列系统，分析各个组件的权衡取舍"}]
)

# 响应中包含 thinking block 和 text block
for block in response.content:
    if block.type == "thinking":
        print(f"[思考过程]\n{block.thinking}\n")
    elif block.type == "text":
        print(f"[最终答案]\n{block.text}")
```

**使用建议**：
- `budget_tokens` 设置模型可用于思考的最大 tokens，实际消耗可能更少
- Opus 4.6 max output 128k，Sonnet 4.6 max output 64k，设置 `max_tokens` 时需包含 thinking tokens
- 简单任务不需要开 thinking，徒增延迟和成本；复杂推理、代码生成、数学问题效果显著

---

## Prompt Caching：成本优化利器

Prompt Caching 是 Claude API 的重要特性，对于包含大量重复内容（长 system prompt、固定的文档上下文）的调用，可以显著降低成本和延迟。

### 工作原理

- 被标记为 `cache_control` 的 prompt 部分会被缓存 5 分钟
- 缓存命中时，输入 token 费用降低 90%（写缓存费用是普通输入的 1.25 倍）
- 适用场景：相同 system prompt 的多次调用、RAG 文档上下文

### 使用方法

```python
# 长 system prompt 缓存
response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=1024,
    system=[
        {
            "type": "text",
            "text": """你是一名专业的代码审查工程师。

以下是我们团队的编码规范（约3000字）：

1. 命名规范
   - 变量名使用 snake_case
   - 类名使用 PascalCase
   ...（大量内容）...

5. 错误处理规范
   ...（大量内容）...
""",
            "cache_control": {"type": "ephemeral"}  # 标记此部分为可缓存
        }
    ],
    messages=[
        {"role": "user", "content": "审查以下代码：\n\n```python\ndef foo(x):\n    return x*2\n```"}
    ]
)

# 检查缓存命中情况
usage = response.usage
print(f"输入 tokens: {usage.input_tokens}")
print(f"缓存读取 tokens: {usage.cache_read_input_tokens}")   # 命中缓存的 tokens
print(f"缓存写入 tokens: {usage.cache_creation_input_tokens}")  # 首次写入缓存的 tokens
```

### RAG 文档上下文缓存

```python
def rag_with_caching(documents: list[str], query: str) -> str:
    """将检索到的文档缓存，减少相同文档集的重复计费"""
    
    # 将文档内容标记为可缓存（适用于多次对同一批文档提问的场景）
    doc_content = "\n\n---\n\n".join(documents)
    
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=[
            {
                "type": "text",
                "text": "你是一个问答助手，基于提供的参考资料回答问题。"
            },
            {
                "type": "text",
                "text": f"参考资料：\n\n{doc_content}",
                "cache_control": {"type": "ephemeral"}  # 缓存文档内容
            }
        ],
        messages=[
            {"role": "user", "content": query}
        ]
    )
    
    return response.content[0].text
```

**缓存策略建议**：
- system prompt 超过 2048 tokens 时开启缓存
- RAG 文档上下文在同一批次的多次查询中共享时开启缓存
- 不适合缓存：频繁变化的内容、短 prompt

---

## 速率限制处理与重试

```python
import time
import anthropic
from anthropic import RateLimitError, APIStatusError, APIConnectionError

def call_claude_with_retry(
    messages: list[dict],
    model: str = "claude-haiku-4-5-20251001",
    max_retries: int = 5,
    **kwargs
) -> anthropic.types.Message:
    """带指数退避重试的 Claude 调用"""
    client = anthropic.Anthropic()
    
    for attempt in range(max_retries):
        try:
            return client.messages.create(
                model=model,
                messages=messages,
                **kwargs
            )
        
        except RateLimitError as e:
            if attempt == max_retries - 1:
                raise
            # 从响应头读取重试等待时间
            retry_after = int(e.response.headers.get("retry-after", 60))
            wait_time = min(retry_after, 2 ** attempt * 10)
            print(f"速率限制，等待 {wait_time}s 后重试 (attempt {attempt + 1}/{max_retries})")
            time.sleep(wait_time)
        
        except APIConnectionError as e:
            if attempt == max_retries - 1:
                raise
            wait_time = 2 ** attempt
            print(f"连接错误，{wait_time}s 后重试: {e}")
            time.sleep(wait_time)
        
        except APIStatusError as e:
            if e.status_code in (500, 529):  # 服务器错误，可重试
                if attempt == max_retries - 1:
                    raise
                wait_time = 2 ** attempt * 5
                print(f"服务器错误 {e.status_code}，{wait_time}s 后重试")
                time.sleep(wait_time)
            else:
                raise  # 4xx 客户端错误不重试
```

### 异步批量处理

```python
import asyncio
from anthropic import AsyncAnthropic

async def batch_process(
    prompts: list[str],
    concurrency: int = 5,   # 并发数量，注意不要超过速率限制
) -> list[str]:
    """异步批量处理多个 prompt"""
    client = AsyncAnthropic()
    semaphore = asyncio.Semaphore(concurrency)
    
    async def process_single(prompt: str) -> str:
        async with semaphore:
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text
    
    tasks = [process_single(p) for p in prompts]
    return await asyncio.gather(*tasks, return_exceptions=True)


# 使用示例
texts = ["文本1", "文本2", "文本3", ...]
results = asyncio.run(batch_process(
    [f"用一句话总结：{text}" for text in texts],
    concurrency=5
))
```

---

## TypeScript 示例

```typescript
import Anthropic from '@anthropic-ai/sdk';

const client = new Anthropic({
  apiKey: process.env.ANTHROPIC_API_KEY,
});

// 基础调用
async function chat(userMessage: string): Promise<string> {
  const message = await client.messages.create({
    model: 'claude-sonnet-4-6',
    max_tokens: 1024,
    messages: [{ role: 'user', content: userMessage }],
  });
  
  return message.content[0].type === 'text' ? message.content[0].text : '';
}

// 流式输出
async function streamChat(userMessage: string): Promise<void> {
  const stream = client.messages.stream({
    model: 'claude-haiku-4-5-20251001',
    max_tokens: 1024,
    messages: [{ role: 'user', content: userMessage }],
  });
  
  for await (const chunk of stream) {
    if (chunk.type === 'content_block_delta' && chunk.delta.type === 'text_delta') {
      process.stdout.write(chunk.delta.text);
    }
  }
  
  const finalMessage = await stream.finalMessage();
  console.log(`\n\nTokens used: ${finalMessage.usage.input_tokens + finalMessage.usage.output_tokens}`);
}
```

---

## 成本优化总结

| 场景 | 策略 | 预期节省 |
|-----|------|---------|
| 长 system prompt 重复调用 | Prompt Caching | 80-90% 输入成本 |
| 简单任务（分类、提取） | 改用 Haiku 4.5 | 节省 70-80% |
| 高频短文本处理 | 批量 + 并发 | 提升吞吐，降低单次成本 |
| 输出长度控制 | 设置合理的 max_tokens（Sonnet 4.6 最大 64k，Opus 4.6 最大 128k） | 避免输出冗余 |
| 开发测试 | 用 Haiku 4.5 替代 Sonnet 4.6 | 降低开发阶段成本 |
| 复杂推理任务 | 用 Opus 4.6 + Extended Thinking | 提升推理质量 |
