---
title: 'LLM 成本优化实战：从 Token 预算到模型路由'
date: 2026-04-12T11:00:00+08:00
draft: false
tags: ["AI", "成本优化", "LLM", "Token", "大模型", "工程实践"]
categories: ["AI/机器学习"]
series: ["AI 工程化实践路径"]
description: 'LLM 成本优化完整指南：Token 预算设计、Prompt Caching、Batch API、模型路由策略，让 AI 应用在不降低质量的前提下将 API 成本降低 50-80%'
summary: '我们的 AI 功能上线第一个月，LLM API 账单是 $18,000。通过模型路由、Prompt Caching 和 Batch API，第三个月降到了 $3,200。这篇文章记录具体怎么做到的。'
toc: true
math: false
diagram: false
keywords: ["LLM成本优化", "Prompt Caching", "模型路由", "Batch API", "Token预算", "LiteLLM", "AI成本"]
params:
  reading_time: true
---

我们的 AI 功能上线第一个月，Claude API 账单是 $18,000。产品经理看到账单后让我们"尽快想办法"。

最开始我们以为要大幅降低功能质量来省钱，但实际上经过系统分析，发现 80% 的成本来自几个低效点：所有请求用同一个旗舰模型、每次请求都重新发送相同的长系统提示词、大量可以离线处理的任务用了实时 API。

三个月后月账单降到 $3,200，用户感知质量没有下降。

## 成本构成分析

先搞清楚钱花在哪里。LLM API 的计费通常分两部分：

- **Input tokens（提示词）**：通常比 Output 便宜 3-5 倍
- **Output tokens（生成内容）**：这才是大头

以 Claude Sonnet 为例：$3/M input，$15/M output。如果你让模型生成一篇 1000 字的文章（约 1500 output tokens），仅生成费用就是 $0.0225。一天 1000 篇 = $22.5，一个月 = $675，只是一个功能点。

### 2026 主流模型成本对比

| 模型 | Input（$/M tokens） | Output（$/M tokens） | 适合场景 |
|------|--------------------|--------------------|----------|
| Claude Sonnet 4.6 | $3.00 | $15.00 | 复杂推理、代码生成 |
| Claude Haiku 3.5 | $0.80 | $4.00 | 简单分类、快速响应 |
| GPT-4.1 | $2.00 | $8.00 | 通用任务 |
| GPT-4.1-mini | $0.40 | $1.60 | 简单任务 |
| DeepSeek V3.2 | $0.27 | $1.10 | 成本敏感、中文场景 |
| Gemini 2.5 Pro | $1.25 | $10.00 | 长上下文（1M） |
| Qwen3-72B（自托管） | GPU 成本约 $0.5-1.5/M | 同左 | 高调用量、合规要求 |

**关键洞察**：Claude Sonnet 的 Output 成本比 DeepSeek V3.2 贵 **14 倍**。如果你的任务 DeepSeek 能完成，这个差价非常值得考虑。

## Token 预算设计

### 上下文窗口管理

多轮对话是成本黑洞。用户聊了 20 轮后，每次请求都要带上全部历史，Input tokens 可能高达 50,000+。

```python
from anthropic import Anthropic

client = Anthropic()

class ContextManager:
    def __init__(self, max_tokens: int = 8000, summary_threshold: int = 6000):
        self.max_tokens = max_tokens
        self.summary_threshold = summary_threshold
        self.messages = []
        self.summary = ""
    
    def add_message(self, role: str, content: str):
        self.messages.append({"role": role, "content": content})
        
        # 估算当前 token 数（粗略：4 字符 ≈ 1 token）
        total_chars = sum(len(m["content"]) for m in self.messages)
        estimated_tokens = total_chars // 4
        
        if estimated_tokens > self.summary_threshold:
            self._compress_history()
    
    def _compress_history(self):
        """把旧对话压缩成摘要"""
        # 保留最近 4 轮对话，其余压缩
        recent_messages = self.messages[-8:]
        old_messages = self.messages[:-8]
        
        if not old_messages:
            return
        
        # 用小模型（便宜）生成摘要
        summary_prompt = f"""请将以下对话历史压缩成 200 字以内的摘要，保留关键信息：

{chr(10).join(f'{m["role"]}: {m["content"][:500]}' for m in old_messages)}"""
        
        response = client.messages.create(
            model="claude-haiku-3-5-20241022",  # 用便宜模型做摘要
            max_tokens=300,
            messages=[{"role": "user", "content": summary_prompt}]
        )
        
        self.summary = response.content[0].text
        self.messages = recent_messages
        print(f"历史压缩：{len(old_messages)} 条 → 摘要 {len(self.summary)} 字")
    
    def get_messages_with_context(self) -> list[dict]:
        if self.summary:
            # 把摘要作为第一条系统消息注入
            return [
                {"role": "user", "content": f"[对话背景摘要]\n{self.summary}"},
                {"role": "assistant", "content": "已了解背景信息。"},
                *self.messages
            ]
        return self.messages
```

### 系统提示词精简

一个"随手写"的系统提示词可能有 2000 tokens，精简到 300 tokens 后效果相当：

```python
# 精简前：2100 tokens（每次都要付这 2100 的 input 费用）
VERBOSE_SYSTEM_PROMPT = """
你是一个专业的客服助手，名叫小智。你由我们公司的工程师精心打造，
具备丰富的产品知识和出色的沟通能力。你的性格友善、耐心、专业。

你的职责包括但不限于：
1. 回答用户关于产品功能的问题
2. 帮助用户解决使用过程中遇到的技术问题
3. 收集用户反馈并记录
4. 在必要时引导用户联系人工客服
...（继续写了 10 条）

当用户问到你的身份时，你应该这样回答：...
当用户情绪激动时，你应该这样处理：...
"""

# 精简后：280 tokens
CONCISE_SYSTEM_PROMPT = """你是客服助手小智。
职责：回答产品问题、解决技术问题、必要时转人工客服。
规则：只基于知识库内容回答；无法确定时说"我来帮您查一下"；保持友善简洁。"""
```

这个例子减少了 1820 tokens 的 input。如果每天有 10,000 次对话，每次 10 轮，就是 1820 × 100,000 tokens = 1.82 亿 input tokens，按 Claude Sonnet 的价格节省 **$546/天**。

## Prompt Caching：最高 ROI 的优化手段

Prompt Caching 允许你把提示词的前缀"存起来"，后续请求命中缓存时，费用大幅降低甚至免费。

**Claude 的 Prompt Caching：缓存的 input tokens 费用降至 10%（cache miss 时有一次性的写入费用，约 1.25x）**

### Cache 控制字段

```python
from anthropic import Anthropic

client = Anthropic()

# 系统提示词（很长，适合缓存）
LONG_SYSTEM_PROMPT = """你是一个专业的代码审查助手。以下是我们公司的代码规范：

## Python 规范
- 使用 Black 格式化，行长度 88
- 类型注解必须完整
- 所有公开函数必须有 docstring
- 禁止使用 global 变量
- 异步函数使用 asyncio，不使用 threading
...（500+ 行规范内容）

## Go 规范  
- 使用 gofmt 格式化
- 错误必须显式处理，不能忽略
- context 作为第一个参数
...（又 300 行）
"""

def review_code(code: str, language: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1024,
        system=[
            {
                "type": "text",
                "text": LONG_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"}  # 标记为可缓存
            }
        ],
        messages=[
            {
                "role": "user",
                "content": f"请 review 以下 {language} 代码：\n\n```{language}\n{code}\n```"
            }
        ]
    )
    
    # 检查缓存命中情况
    usage = response.usage
    print(f"Input tokens: {usage.input_tokens}")
    print(f"Cache creation: {usage.cache_creation_input_tokens}")
    print(f"Cache read: {usage.cache_read_input_tokens}")
    
    return response.content[0].text
```

**缓存命中时的实际成本**（假设系统提示词 5000 tokens，用户消息 200 tokens，输出 800 tokens）：
- 无缓存：5000 × $3 + 200 × $3 + 800 × $15 = $0.0279
- 有缓存（命中）：200 × $3 + 5000 × $0.3 + 800 × $15 = $0.0141（节省 **49%**）

缓存有效期为 5 分钟（Claude），如果你的系统每分钟有多个请求，命中率会很高。

### 哪些内容适合缓存

```python
# 适合缓存的内容（放在 messages 靠前的位置，且需要 cache_control 标记）：

# 1. 长系统提示词（规范、角色描述）
# 2. RAG 检索到的文档（多个问题基于同一批文档）
# 3. Few-shot 示例（同类任务的示例集）
# 4. 工具定义（Agent 场景下的工具列表通常很长）

def rag_query(question: str, documents: list[str]) -> str:
    docs_content = "\n\n".join(f"文档{i+1}：\n{doc}" for i, doc in enumerate(documents))
    
    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=512,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"<knowledge_base>\n{docs_content}\n</knowledge_base>",
                        "cache_control": {"type": "ephemeral"}  # 缓存知识库文档
                    },
                    {
                        "type": "text", 
                        "text": f"\n根据以上文档回答：{question}"
                        # 问题不缓存，每次都变
                    }
                ]
            }
        ]
    )
    return response.content[0].text
```

**OpenAI 的 Prompt Caching** 是自动触发的，无需额外配置，缓存命中时 input 费用降低 50%。

## 模型路由：对号入座

这是成本优化里影响最大的一个策略。核心思路：**不是所有任务都需要旗舰模型**。

### 任务复杂度分类

```python
from litellm import Router
import litellm

# 配置模型路由（使用 LiteLLM Router）
router = Router(
    model_list=[
        {
            "model_name": "fast-model",
            "litellm_params": {
                "model": "claude-haiku-3-5-20241022",
                "api_key": "your-key"
            }
        },
        {
            "model_name": "smart-model", 
            "litellm_params": {
                "model": "claude-sonnet-4-5",
                "api_key": "your-key"
            }
        },
        {
            "model_name": "cheap-model",
            "litellm_params": {
                "model": "deepseek/deepseek-chat",
                "api_key": "your-deepseek-key"
            }
        }
    ]
)

def classify_task_complexity(user_message: str) -> str:
    """
    用最小模型分类任务复杂度，决定路由到哪个主模型。
    分类本身的成本极低（Haiku ~0.5 cents/1000次）
    """
    classification_prompt = f"""将以下用户请求分类为：
- simple：简单问答、闲聊、基本信息查询
- medium：需要分析推理、代码生成、内容创作
- complex：需要深度推理、多步骤规划、专业领域复杂问题

只输出分类标签，不要解释。

用户请求：{user_message[:500]}"""
    
    response = router.completion(
        model="fast-model",
        messages=[{"role": "user", "content": classification_prompt}],
        max_tokens=10,
    )
    
    label = response.choices[0].message.content.strip().lower()
    return label if label in ["simple", "medium", "complex"] else "medium"


COMPLEXITY_TO_MODEL = {
    "simple": "fast-model",    # Haiku：$0.8/$4 per M
    "medium": "cheap-model",   # DeepSeek：$0.27/$1.1 per M
    "complex": "smart-model",  # Sonnet：$3/$15 per M
}

def smart_chat(user_message: str, conversation_history: list) -> str:
    complexity = classify_task_complexity(user_message)
    model = COMPLEXITY_TO_MODEL[complexity]
    
    response = router.completion(
        model=model,
        messages=conversation_history + [{"role": "user", "content": user_message}],
        max_tokens=1024,
    )
    
    # 记录路由决策，用于后续分析
    log_routing_decision(user_message[:100], complexity, model, response.usage)
    
    return response.choices[0].message.content
```

**路由效果实测**（基于我们的客服场景）：

| 任务分类 | 占比 | 路由模型 | 平均成本/次 |
|----------|------|---------|------------|
| simple（问候、基础 FAQ） | 45% | Haiku | $0.0008 |
| medium（产品问题、文档查询） | 40% | DeepSeek | $0.0015 |
| complex（技术支持、退款申诉） | 15% | Sonnet | $0.0180 |
| 加权平均 | - | - | $0.0044 |
| 全部 Sonnet | - | - | $0.0180 |
| **节省比例** | - | - | **76%** |

## Batch API：离线任务省 50%

大量任务不需要实时响应——数据清洗、批量内容分析、离线摘要生成、训练数据标注。这些任务可以用 Batch API，OpenAI 和 Anthropic 都提供 **50% 折扣**，处理时间通常在 1-24 小时内。

```python
import anthropic
import json

client = anthropic.Anthropic()

def batch_analyze_feedback(feedback_list: list[str]) -> list[dict]:
    """批量分析用户反馈，使用 Batch API 节省 50% 成本"""
    
    # 构建批次请求
    requests = []
    for i, feedback in enumerate(feedback_list):
        requests.append({
            "custom_id": f"feedback-{i}",
            "params": {
                "model": "claude-haiku-3-5-20241022",
                "max_tokens": 100,
                "messages": [
                    {
                        "role": "user",
                        "content": f"""分析以下用户反馈，输出 JSON：
{{"sentiment": "positive/neutral/negative", "category": "product/service/pricing/other", "priority": "high/medium/low"}}

反馈：{feedback}"""
                    }
                ]
            }
        })
    
    # 提交批次
    batch = client.messages.batches.create(requests=requests)
    print(f"批次提交成功，ID: {batch.id}，共 {len(requests)} 条")
    
    # 轮询等待完成（实际生产中建议用 webhook 或定时任务）
    import time
    while True:
        batch_status = client.messages.batches.retrieve(batch.id)
        if batch_status.processing_status == "ended":
            break
        print(f"处理中... {batch_status.request_counts}")
        time.sleep(60)
    
    # 获取结果
    results = []
    for result in client.messages.batches.results(batch.id):
        if result.result.type == "succeeded":
            try:
                analysis = json.loads(result.result.message.content[0].text)
                results.append({
                    "id": result.custom_id,
                    "analysis": analysis
                })
            except json.JSONDecodeError:
                results.append({"id": result.custom_id, "error": "parse_failed"})
    
    return results


# OpenAI Batch API 类似
from openai import OpenAI
import json

openai_client = OpenAI()

def openai_batch_classify(texts: list[str]) -> str:
    """返回 batch job ID，后续轮询结果"""
    
    # 构建 JSONL 格式的批次文件
    batch_lines = []
    for i, text in enumerate(texts):
        batch_lines.append(json.dumps({
            "custom_id": f"item-{i}",
            "method": "POST",
            "url": "/v1/chat/completions",
            "body": {
                "model": "gpt-4.1-mini",
                "messages": [{"role": "user", "content": f"分类：{text[:200]}"}],
                "max_tokens": 50
            }
        }))
    
    # 上传文件
    import io
    file_content = "\n".join(batch_lines).encode()
    batch_file = openai_client.files.create(
        file=io.BytesIO(file_content),
        purpose="batch"
    )
    
    # 创建批次任务
    batch = openai_client.batches.create(
        input_file_id=batch_file.id,
        endpoint="/v1/chat/completions",
        completion_window="24h"
    )
    
    return batch.id
```

**适合 Batch API 的任务类型**：
- 用户反馈情感分析
- 商品描述质量检查
- 历史数据清洗和标注
- SEO 关键词提取
- 内容合规审查（配合 LlamaGuard）

**不适合的**：用户实时交互、需要秒级响应的任何场景。

## 自托管 vs API：何时自建更划算

```python
def should_self_host(
    monthly_tokens: int,      # 月均 token 消耗量
    api_price_per_m: float,   # API 价格（$/M tokens）
    gpu_monthly_cost: float = 3500,  # A100×4 的月租（AWS p4d.24xlarge 约 $3500/月）
    self_host_capacity_m: int = 500  # 自托管月处理能力（M tokens）
) -> dict:
    """简单的自托建 vs API 成本分析"""
    
    api_monthly_cost = (monthly_tokens / 1_000_000) * api_price_per_m
    self_host_unit_cost = gpu_monthly_cost / self_host_capacity_m
    self_host_monthly_cost = (monthly_tokens / 1_000_000) * self_host_unit_cost
    
    breakeven_tokens = gpu_monthly_cost / (api_price_per_m - self_host_unit_cost) * 1_000_000
    
    return {
        "api_monthly_cost": f"${api_monthly_cost:.0f}",
        "self_host_monthly_cost": f"${self_host_monthly_cost:.0f}",
        "recommended": "self_host" if monthly_tokens > breakeven_tokens else "api",
        "breakeven_m_tokens": f"{breakeven_tokens/1_000_000:.1f}M tokens/月"
    }

# 示例：使用 Claude Sonnet 处理 Output tokens（$15/M）
# vs 自托管 Qwen3-72B（A100×4，约 $0.5/M 等效成本）
print(should_self_host(
    monthly_tokens=100_000_000,  # 1 亿 tokens/月
    api_price_per_m=15,          # Sonnet output 价格
    gpu_monthly_cost=3500,
    self_host_capacity_m=200
))
# 输出：{'api_monthly_cost': '$1500', 'self_host_monthly_cost': '$1750', 'recommended': 'api', ...}
# 1亿 tokens/月这个量，自托管还没有 API 划算！

print(should_self_host(
    monthly_tokens=1_000_000_000,  # 10 亿 tokens/月
    api_price_per_m=15,
    gpu_monthly_cost=3500,
    self_host_capacity_m=200
))
# 这个量才开始值得自托管
```

**结论**：除非你有非常高的调用量（月均 10 亿+ output tokens），或者数据合规要求不能出境，否则 API 通常比自托管更划算，因为你还省去了运维成本。

## 监控成本：找到优化机会

```python
from prometheus_client import Counter, Histogram, start_http_server
import time

# Prometheus metrics
llm_token_usage = Counter(
    'llm_token_total',
    'Total LLM token usage',
    ['model', 'token_type', 'feature', 'user_tier']
)
llm_cost_dollars = Counter(
    'llm_cost_dollars_total',
    'Total LLM cost in dollars',
    ['model', 'feature']
)
llm_request_duration = Histogram(
    'llm_request_duration_seconds',
    'LLM request latency',
    ['model', 'feature']
)

MODEL_PRICES = {
    "claude-haiku-3-5": {"input": 0.8/1e6, "output": 4.0/1e6},
    "claude-sonnet-4-5": {"input": 3.0/1e6, "output": 15.0/1e6},
    "deepseek-chat": {"input": 0.27/1e6, "output": 1.1/1e6},
}

def tracked_llm_call(
    model: str, 
    messages: list, 
    feature: str,
    user_tier: str = "standard",
    **kwargs
) -> object:
    start_time = time.time()
    
    response = router.completion(model=model, messages=messages, **kwargs)
    
    duration = time.time() - start_time
    usage = response.usage
    prices = MODEL_PRICES.get(model, {"input": 0, "output": 0})
    
    # 记录 metrics
    llm_token_usage.labels(model=model, token_type="input", feature=feature, user_tier=user_tier).inc(usage.prompt_tokens)
    llm_token_usage.labels(model=model, token_type="output", feature=feature, user_tier=user_tier).inc(usage.completion_tokens)
    
    cost = usage.prompt_tokens * prices["input"] + usage.completion_tokens * prices["output"]
    llm_cost_dollars.labels(model=model, feature=feature).inc(cost)
    llm_request_duration.labels(model=model, feature=feature).observe(duration)
    
    return response
```

在 Grafana 中按 `feature` 维度看成本分布，通常会发现 20% 的功能消耗了 80% 的成本——这些就是优先优化的目标。

设置预算告警：

```yaml
# Prometheus alerting rule
- alert: LLMDailyCostHigh
  expr: increase(llm_cost_dollars_total[24h]) > 200
  annotations:
    summary: "LLM 日成本超过 $200，当前：{{ $value | printf \"%.2f\" }}"
    
- alert: LLMFeatureCostSpike  
  expr: |
    increase(llm_cost_dollars_total[1h]) 
    / increase(llm_cost_dollars_total[1h] offset 1d) > 3
  annotations:
    summary: "LLM 成本异常飙升（1小时成本是昨天同期的 3 倍）"
```

---

成本优化不是一次性的工作，而是一个持续的过程。我们的经验是：先把监控建起来，用数据找到成本大头，然后按 ROI 排序优化：

1. **模型路由**（ROI 最高，一次配置长期受益）
2. **Prompt Caching**（系统提示词一旦稳定就能持续省钱）
3. **Batch API**（离线任务立竿见影）
4. **上下文压缩**（对话密集型场景效果显著）

不要追求完美，80% 的成本优化来自 20% 的工作。把省下来的钱投入到更好的模型或更多的功能探索，才是正确姿势。
