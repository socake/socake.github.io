---
title: "大模型核心概念：工程师需要理解的 LLM 基础"
date: 2026-04-12T14:00:00+08:00
draft: false
tags: ["LLM", "大模型", "Token", "Embedding", "Prompt Engineering", "AI工程化"]
categories: ["大模型"]
series: ["AI 工程化实战"]
description: "不讲数学公式，从工程师视角解释 Token、上下文窗口、Temperature、幻觉、Embedding 等 LLM 核心概念，以及这些概念如何影响你的应用设计决策"
summary: "同事第一次用 GPT-4 API 写代码时问我：为什么我发了一段中文，token 消耗比英文多那么多？为什么模型有时候会一本正经地胡说八道？这篇文章把我认为工程师必须理解的 LLM 概念系统整理了一遍，不涉及 Transformer 数学，只讲对你写代码有帮助的部分。"
toc: true
math: false
diagram: false
keywords: ["Token", "上下文窗口", "Temperature", "幻觉", "Embedding", "System Prompt", "推理成本", "LLM工程"]
params:
  reading_time: true
---

我在日常工作中接触过不少工程师，他们能熟练调用 OpenAI API，写出功能完整的 RAG 系统，但对 LLM 的工作机制只有模糊的认知。这种「知其然不知其所以然」的状态，会在做架构决策时埋下很多坑——比如把一本书塞进上下文窗口然后奇怪为什么效果差，或者用 Temperature=1.0 去做需要精确格式的数据提取。

这篇文章不讲 Transformer 的数学，只讲工程师在构建 LLM 应用时需要理解的核心概念。

## Token：LLM 的最小计算单元

Token 是 LLM 的基本处理单位，不是字符，不是单词，而是比单词更细粒度的**子词片段（subword）**。

理解 Token 的最直接方式是用 OpenAI 的 tokenizer：

```python
import tiktoken

enc = tiktoken.encoding_for_model("gpt-4o")

# 英文
text_en = "Hello, how are you today?"
tokens_en = enc.encode(text_en)
print(f"英文: {len(tokens_en)} tokens")  # 6 tokens
print(enc.decode_tokens_bytes(tokens_en))  # 每个 token 对应的字节

# 中文
text_zh = "你好，今天感觉怎么样？"
tokens_zh = enc.encode(text_zh)
print(f"中文: {len(tokens_zh)} tokens")  # 约 14 tokens
```

输出示例：
```
英文: 6 tokens
中文: 14 tokens
```

**为什么中文更贵？**

GPT-4 的分词器（BPE，Byte Pair Encoding）在大量英文语料上训练，对英文词汇的压缩率高——一个常见英文单词通常就是 1 个 token。而中文字符在训练语料中相对稀少，分词器对中文的压缩率低，1个中文字符通常需要 1-3 个 token 表示。

工程意义：
- 计费按 token 不按字符，中文应用的 API 成本比同等信息量的英文应用高 2-3 倍
- 上下文窗口限制也是按 token 算，存同样的信息，中文占的窗口空间更多
- 如果你的应用需要极致成本控制，考虑在 Prompt 中用更简洁的中文表达

## 上下文窗口：LLM 的「工作记忆」

上下文窗口（Context Window）是模型在生成回复时能「看到」的最大 token 数量。目前主流模型的窗口大小：

| 模型 | 上下文窗口 |
|------|-----------|
| GPT-4o | 128K tokens |
| Claude 3.5 Sonnet | 200K tokens |
| Gemini 1.5 Pro | 1M tokens |
| Llama 3.1 70B | 128K tokens |

**为什么不能无限大？**

这是个计算复杂度问题。Transformer 的 Self-Attention 机制的计算复杂度是 **O(n²)**——n 是 token 数量。上下文长度翻倍，计算量变成 4 倍，显存占用也大幅增加。

更重要的是：**长上下文≠好效果**。研究表明（Lost in the Middle），模型对放在上下文中间的信息注意力会显著下降，放在开头和结尾的信息才容易被「记住」。

```python
# 一个说明上下文位置效应的实验框架
def test_position_effect(api_client, key_info, position="middle"):
    """把关键信息放在不同位置，测试模型是否能准确引用"""
    filler = "这是填充内容，用于测试上下文位置效应。" * 100  # 约 5000 tokens

    if position == "start":
        context = f"关键信息：{key_info}\n\n{filler}"
    elif position == "middle":
        context = f"{filler[:len(filler)//2]}\n\n关键信息：{key_info}\n\n{filler[len(filler)//2:]}"
    elif position == "end":
        context = f"{filler}\n\n关键信息：{key_info}"

    response = api_client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "user", "content": f"{context}\n\n请重复上面提到的关键信息。"}
        ]
    )
    return response.choices[0].message.content
```

工程建议：
- 把最重要的信息（核心指令、关键约束）放在 System Prompt 的开头
- 避免把 context 填满——留 20-30% 的空间给模型「思考」
- 如果信息量确实大，用 RAG 按需检索，而不是全部塞进去

## Temperature 和 Top-p：控制输出的「随机性旋钮」

这两个参数控制模型在生成每个 token 时如何从概率分布中采样。

### Temperature

Temperature 缩放 logits（原始预测分数）的分布。

- **Temperature = 0**：确定性输出，每次都选概率最高的 token
- **Temperature = 1**：按照模型原始概率分布采样
- **Temperature > 1**：概率分布变「平」，更多样化但也更随机，容易乱说

```python
import anthropic

client = anthropic.Anthropic()

prompt = "用一句话描述人工智能的未来"

# 低温度：输出稳定，适合数据提取、格式化输出
response_low = client.messages.create(
    model="claude-3-5-sonnet-20241022",
    max_tokens=100,
    temperature=0.1,
    messages=[{"role": "user", "content": prompt}]
)

# 高温度：输出多样，适合创意写作、头脑风暴
response_high = client.messages.create(
    model="claude-3-5-sonnet-20241022",
    max_tokens=100,
    temperature=1.0,
    messages=[{"role": "user", "content": prompt}]
)

print("低温度:", response_low.content[0].text)
print("高温度:", response_high.content[0].text)
```

### Top-p（核采样）

Top-p 限制候选 token 的范围：只从累积概率达到 p 的最小 token 集合中采样。

- `top_p=0.1`：只从概率最高的那一小批 token 中选，非常保守
- `top_p=0.9`：从覆盖 90% 概率质量的 token 中选，有适度多样性
- `top_p=1.0`：不限制，从所有 token 中采样

**实践建议（不要同时调两个）**：

| 场景 | 建议配置 |
|------|---------|
| JSON 数据提取、格式化输出 | temperature=0, top_p=1 |
| 问答、代码生成 | temperature=0.2~0.5 |
| 创意写作、多样性生成 | temperature=0.7~1.0 |
| 头脑风暴、产品创意 | temperature=1.0, top_p=0.9 |

## System Prompt 的工作机制

System Prompt 是在对话开始前注入的指令，用来定义模型的角色、行为规范和上下文。

```python
# System Prompt 的典型结构
system_prompt = """
你是一个专业的代码审查助手。你的工作是：

## 职责
1. 检查代码的正确性、性能问题和安全隐患
2. 给出具体可执行的改进建议
3. 解释为什么某段代码有问题

## 输出格式
始终以 JSON 格式返回，结构如下：
{
    "issues": [{"severity": "high/medium/low", "line": N, "description": "...", "suggestion": "..."}],
    "summary": "..."
}

## 约束
- 不要给出无法落地的笼统建议
- 如果代码没有问题，issues 返回空数组
- 不要修改代码本身，只给建议
"""
```

**System Prompt 和 User Prompt 的本质区别是什么？**

从技术角度，两者都进入 Transformer 的输入序列，模型并不会「更尊重」System Prompt。区别在于：System Prompt 通常在对话开始时出现，而 Transformer 的注意力机制对上下文位置是敏感的——放在开头的内容在后续生成中权重更高。

一个工程上重要的推论：重要指令不要只放一次，在复杂任务中，在 User Prompt 里也重申关键约束，效果会更好。

## 为什么 LLM 会「幻觉」

幻觉（Hallucination）是 LLM 生成看似合理但实际错误的内容。理解它的机制有助于在系统设计上规避。

**工程师视角的解释：**

LLM 的训练目标是「预测下一个 token 的概率」，不是「只说真实的话」。模型在训练时见过大量文本，学会了「什么样的文字组合看起来合理」——这和「是否符合事实」是两个不同的优化目标。

当模型被问到它训练数据中没有的信息（比如最新事件、小众知识）时，它不会说「我不知道」——因为「我不知道」在概率上是低概率输出，模型倾向于生成看起来合理的内容，而这个内容可能就是编的。

```python
# 减少幻觉的工程策略
def ask_with_grounding(client, question, retrieved_context):
    """把检索到的事实作为锚点，要求模型基于这些事实回答"""
    return client.messages.create(
        model="claude-3-5-sonnet-20241022",
        max_tokens=1000,
        system="""
        你只能基于用户提供的参考资料回答问题。
        如果参考资料中没有足够信息，直接说「根据提供的资料，无法回答这个问题」。
        不要自行补充参考资料中没有的信息。
        """,
        messages=[{
            "role": "user",
            "content": f"""参考资料：
            {retrieved_context}

            问题：{question}"""
        }]
    )
```

几个减少幻觉的工程实践：
1. **给模型「退路」**：System Prompt 明确允许模型说「我不知道」
2. **要求引用来源**：让模型在回答中标注信息来自哪段上下文
3. **RAG 接地**：用检索到的实际文档作为事实锚点
4. **验证层**：对关键信息，用独立的模型调用或规则引擎做二次验证

## 模型参数规模与能力的关系

参数量（Parameters）是描述模型「大小」的常见指标，但它和能力的关系是非线性的。

```
7B  → 基础理解和生成，适合简单任务
13B → 代码生成质量明显提升
70B → 复杂推理、多步骤任务，接近早期 GPT-4 水平
405B → 最强开源模型（Llama 3.1 405B），需要多张 A100
```

一个实用的参考框架：

| 任务类型 | 推荐最小规模 |
|---------|------------|
| 分类、实体提取 | 7B 微调模型 |
| 代码补全 | 13B~34B |
| 复杂推理、规划 | 70B+ |
| 跨语言理解、细粒度指令跟随 | 70B+ 或 GPT-4 级 |

**量化（Quantization）对能力的影响**

实际部署时，全精度（FP16）的 70B 模型需要约 140GB 显存，大多数场景会用量化版本：

```
FP16 70B  → ~140GB VRAM，最佳质量
INT8 70B  → ~70GB VRAM，质量下降 1-3%
INT4 70B  → ~35GB VRAM，质量下降 5-15%（取决于任务）
```

## Embedding 向量的直觉理解

Embedding 是把文本映射到高维向量空间的技术，是 RAG 系统的基础。

直觉上，Embedding 捕捉了文本的「语义位置」——意思相近的文本在向量空间中距离近，意思相反的文本距离远。

```python
from openai import OpenAI
import numpy as np

client = OpenAI()

def get_embedding(text):
    response = client.embeddings.create(
        model="text-embedding-3-small",
        input=text
    )
    return np.array(response.data[0].embedding)

def cosine_similarity(a, b):
    return np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))

# 测试语义相似度
emb_dog = get_embedding("狗是人类的好朋友")
emb_cat = get_embedding("猫喜欢独处")
emb_car = get_embedding("汽车需要定期保养")

print(f"狗-猫相似度: {cosine_similarity(emb_dog, emb_cat):.3f}")   # ~0.8（同是动物话题）
print(f"狗-汽车相似度: {cosine_similarity(emb_dog, emb_car):.3f}") # ~0.6（话题差异大）
```

**工程师需要理解的 Embedding 关键点：**

1. **维度不是越高越好**：`text-embedding-3-large` 是 3072 维，`text-embedding-3-small` 是 1536 维，后者成本低 5 倍，多数 RAG 任务效果差异不大
2. **跨语言能力**：好的多语言 Embedding 模型能捕捉跨语言的语义相似性——中文「苹果手机」和英文「iPhone」在向量空间中会很近
3. **长文本降质**：Embedding 模型通常有 512~8192 token 的输入限制，超过后一般截断，长文档需要分块处理

## 推理 vs 训练的成本差异

这个概念影响你对「自己训/微调」还是「调 API」的决策。

**训练**：需要对所有参数计算梯度，反向传播，更新权重。计算量是推理的 3-5 倍，显存需求更高（需要存储梯度和优化器状态）。

**推理**：只做前向传播，计算量相对小。但高并发下，推理的吞吐量瓶颈是显存带宽而不是计算量——模型参数每次推理都要从显存读到计算单元。

实际成本对比（近似数字）：

| 操作 | 成本量级 |
|------|---------|
| 从头训练 70B 模型 | 数百万美元（不现实） |
| 微调 70B 模型（LoRA） | 数百~数千美元 |
| 微调 7B 模型（LoRA） | 数十美元 |
| GPT-4o API 推理 100 万 token | ~$5（输出）/ ~$2.5（输入） |
| 自托管 70B（INT4）推理 | ~$0.3/百万 token（A100 按小时计） |

**决策框架**：

```
数据隐私要求高 → 自托管（考虑 Ollama + 开源模型）
   ↓
任务需要专业领域适配 → 微调（LoRA 是当前最经济的方式）
   ↓
通用任务，量不大 → 直接调 API（运维成本低于自托管）
   ↓
量大且任务简单 → 评估自托管 vs API 的盈亏平衡点
```

## 这些概念如何影响你的应用设计

把上面的概念串起来，对应用设计有几个直接影响：

**1. 系统 Prompt 要稳定，User Prompt 要精简**

System Prompt 是固定的，可以利用 API 的 Prompt Caching 功能（Anthropic Claude API 支持）大幅降低成本——相同的 System Prompt 只需付一次处理费。

```python
import anthropic

client = anthropic.Anthropic()

# 使用 cache_control 缓存长 System Prompt
response = client.messages.create(
    model="claude-3-5-sonnet-20241022",
    max_tokens=1024,
    system=[
        {
            "type": "text",
            "text": "你是一个...",  # 长达 10000 token 的 System Prompt
            "cache_control": {"type": "ephemeral"}  # 缓存这个 block
        }
    ],
    messages=[{"role": "user", "content": user_query}]
)
```

**2. 对精确格式的任务，Temperature 设为 0**

需要输出 JSON、SQL、特定格式数据时，Temperature=0 能显著减少格式错误率。

**3. 控制 Token 消耗，做好预算保护**

```python
import tiktoken

def estimate_cost(messages, model="gpt-4o"):
    """估算 API 调用成本"""
    enc = tiktoken.encoding_for_model(model)
    total_tokens = sum(
        len(enc.encode(m["content"]))
        for m in messages
    )
    # GPT-4o 输入价格：$2.5/百万 token
    estimated_cost = total_tokens / 1_000_000 * 2.5
    return total_tokens, estimated_cost

# 在实际调用前检查
tokens, cost = estimate_cost(messages)
if tokens > 100_000:
    raise ValueError(f"请求过大: {tokens} tokens，预计成本 ${cost:.4f}")
```

**4. 幻觉高风险场景必须加验证层**

涉及数字、日期、专有名词的输出，不要直接信任 LLM 的回答。构建验证管线：

```python
def extract_with_validation(text, schema):
    """带验证的结构化提取"""
    result = llm_extract(text, schema)

    # 验证层：检查必填字段、数值范围、日期格式等
    for field, validator in schema.items():
        if not validator(result.get(field)):
            # 重试，或返回低置信度标记
            return {"data": result, "confidence": "low", "needs_review": True}

    return {"data": result, "confidence": "high", "needs_review": False}
```

理解这些概念不是为了炫技，而是在遇到「为什么效果不好」「为什么成本这么高」这类问题时，能快速定位根因并找到解法。
