---
title: "Advanced RAG：超越 Naive RAG 的高级检索增强技术"
date: 2026-04-12T11:00:00+08:00
draft: false
tags: ["RAG", "向量检索", "AI", "LangChain", "大模型", "知识库"]
categories: ["AI/机器学习"]
series: ["AI 工程化实践路径"]
description: "Advanced RAG 完整指南：从 HyDE 假设文档、查询改写、混合检索，到 Parent-Child 分块和 Reranker 重排序，系统解决 Naive RAG 的检索失败问题"
summary: "系统拆解 Naive RAG 的三类失败模式，提供混合检索、HyDE、查询改写、Parent-Child 分块等高级技术的完整实现"
toc: true
math: false
diagram: false
keywords: ["RAG", "HyDE", "混合检索", "Reranker", "查询改写", "Parent-Child分块", "RAGAS"]
params:
  reading_time: true
---

Naive RAG 的流程极其简单：切文档 → Embed → 存向量库 → 查询时检索 Top-K → 塞给 LLM。这个流程在 demo 阶段看起来很美，但上生产之后各种问题就来了。我见过最典型的案例是：用户问"我们的退款政策是什么"，RAG 系统返回的是"退货政策"相关文档，而退款和退货政策明明在同一个 PDF 的相邻两段，但就是召回不了退款的那段。

这篇文章系统梳理 Naive RAG 失败的根因，以及对应的 Advanced RAG 技术。

## Naive RAG 的三类失败

**失败类型 1：检索召回失败（Recall Failure）**

相关文档压根没被找到。原因通常是：
- Query 和文档的语义表达差异太大（用户问"涨价了吗"，文档里写的是"价格调整方案"）
- 文档切块不合理，关键信息被切断了
- Embedding 模型对该领域的语义理解不够好

**失败类型 2：检索精度失败（Precision Failure）**

找到了，但返回的 Top-K 里混入了太多噪声文档，LLM 被干扰了。原因：
- 纯向量相似度不能区分"语义相近但答案不同"的文档
- Top-K 设置太大，召回了一堆弱相关文档
- 缺少 Reranker 对候选结果重新排序

**失败类型 3：生成失败（Generation Failure）**

文档找到了，但 LLM 没有正确利用。原因：
- 相关段落被埋在大量上下文中间（Lost in the Middle 问题）
- Prompt 设计不合理，LLM 忽略了检索结果
- 文档格式（表格、代码）没有被正确处理

不同的失败类型需要不同的解决方案，下面逐一展开。

## 混合检索：Dense + Sparse + RRF

纯向量检索（Dense Retrieval）的软肋是对精确关键词不敏感。如果用户搜索"GPT-4.1 的 context window 是多少"，向量检索可能召回很多"GPT 系列模型对比"的泛泛文章，而不是直接包含"GPT-4.1"这个词的精确文档。

解决方案是把向量检索和 BM25（稀疏检索）结合，用 **RRF（Reciprocal Rank Fusion）** 融合排名：

```python
from rank_bm25 import BM25Okapi
import numpy as np
from typing import Any

def reciprocal_rank_fusion(
    rankings: list[list[int]],
    k: int = 60
) -> list[tuple[int, float]]:
    """
    RRF 融合多路检索结果
    rankings: 每路检索返回的文档 ID 列表（按相关性降序）
    k: RRF 平滑参数，默认 60
    """
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking):
            scores[doc_id] = scores.get(doc_id, 0) + 1.0 / (k + rank + 1)
    
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


class HybridRetriever:
    def __init__(self, docs: list[str], embed_fn, vector_index):
        self.docs = docs
        self.embed_fn = embed_fn
        self.vector_index = vector_index  # FAISS 或 Milvus 等
        
        # 初始化 BM25
        tokenized_docs = [doc.split() for doc in docs]
        self.bm25 = BM25Okapi(tokenized_docs)
    
    def retrieve(self, query: str, top_k: int = 20) -> list[str]:
        # 1. 向量检索
        query_vec = self.embed_fn([query])[0]
        dense_ids = self.vector_index.search(query_vec, top_k)
        
        # 2. BM25 检索
        tokenized_query = query.split()
        bm25_scores = self.bm25.get_scores(tokenized_query)
        bm25_ids = np.argsort(bm25_scores)[::-1][:top_k].tolist()
        
        # 3. RRF 融合
        fused = reciprocal_rank_fusion([dense_ids, bm25_ids])
        
        # 返回 Top-K 文档
        return [self.docs[doc_id] for doc_id, _ in fused[:top_k//2]]
```

实际效果：在中文技术文档上，混合检索比纯向量检索的 Top-5 召回率通常提升 **10-20%**，对包含专有名词（产品名、版本号、API 名称）的查询提升更明显。

## Reranker 重排序

混合检索解决了召回问题，但还需要 Reranker 来提升精度。Reranker 使用 **cross-encoder** 架构，把 query 和每个候选文档一起输入，输出一个精确的相关性分数。

与 Embedding 的 bi-encoder（query 和 doc 分别 Embed 后算相似度）相比，cross-encoder 精度更高，但速度慢，所以通常在召回的 Top-20~50 个结果上跑，而不是全量文档。

```python
from FlagEmbedding import FlagReranker
from sentence_transformers import CrossEncoder

# 方案1：BGE-Reranker-v2-m3（推荐，支持中文）
reranker = FlagReranker('BAAI/bge-reranker-v2-m3', use_fp16=True)

def rerank_with_bge(query: str, candidates: list[str], top_n: int = 5) -> list[str]:
    pairs = [[query, doc] for doc in candidates]
    scores = reranker.compute_score(pairs, normalize=True)
    
    ranked = sorted(zip(candidates, scores), key=lambda x: x[1], reverse=True)
    return [doc for doc, _ in ranked[:top_n]]


# 方案2：Cohere Rerank 3（API，无需自托管）
import cohere

co = cohere.Client("YOUR_COHERE_API_KEY")

def rerank_with_cohere(query: str, candidates: list[str], top_n: int = 5) -> list[str]:
    response = co.rerank(
        query=query,
        documents=candidates,
        model="rerank-v3.5",
        top_n=top_n
    )
    return [candidates[r.index] for r in response.results]


# 完整 Pipeline：召回 Top-20，Reranker 精排到 Top-5
def retrieve_and_rerank(query: str, retriever, top_k: int = 20, top_n: int = 5):
    candidates = retriever.retrieve(query, top_k=top_k)
    return rerank_with_bge(query, candidates, top_n=top_n)
```

**BGE-Reranker-v2-m3 vs Cohere Rerank 3 怎么选：**
- 有 GPU 且追求数据不出境 → BGE-Reranker-v2-m3
- 想省运维成本 → Cohere Rerank 3，精度和 BGE 相当，但每次调用有费用

## HyDE：用假设答案弥合语义鸿沟

HyDE（Hypothetical Document Embeddings）是解决 query-doc 语义鸿沟的优雅方案。问题在于：用户的 query 往往很短、很口语化，而知识库里的文档是正式的长文本。直接用 query 的向量去检索，效果不好。

HyDE 的思路是：**先让 LLM 生成一个假设性的答案文档，再用这个假设文档的向量去检索**。假设文档的语言风格更接近知识库里的文档，语义对齐效果更好。

```python
from openai import OpenAI
from anthropic import Anthropic

openai_client = OpenAI()
anthropic_client = Anthropic()

def generate_hypothetical_document(query: str, use_claude: bool = True) -> str:
    """
    生成假设答案文档
    注意：这里不需要答案正确，只需要语义上接近真实文档
    """
    prompt = f"""请根据以下问题，生成一段可能在相关文档中出现的段落。
不需要答案完全准确，重点是生成与专业文档风格相似的文本。

问题：{query}

生成一段 100-200 字的相关文档段落："""

    if use_claude:
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.content[0].text
    else:
        response = openai_client.chat.completions.create(
            model="gpt-4.1",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300
        )
        return response.choices[0].message.content


def hyde_retrieve(query: str, retriever, embed_fn, top_k: int = 5) -> list[str]:
    # 生成假设文档
    hypothetical_doc = generate_hypothetical_document(query)
    
    # 用假设文档的向量检索
    hyde_vec = embed_fn([hypothetical_doc])[0]
    hyde_results = retriever.vector_index.search(hyde_vec, top_k)
    
    # 也用原始 query 检索，取并集
    query_vec = embed_fn([query])[0]
    query_results = retriever.vector_index.search(query_vec, top_k)
    
    # RRF 融合
    fused = reciprocal_rank_fusion([hyde_results, query_results])
    return [retriever.docs[doc_id] for doc_id, _ in fused[:top_k]]
```

**什么时候用 HyDE：**
- 用户的 query 和知识库的文档风格差异很大（比如用户说大白话，文档是技术规范）
- 专业领域知识库（法律、医疗、金融）
- **不适合** 简单的关键词查询，HyDE 在这类场景会引入噪声

## 查询改写：多查询与 Step-Back

### Multi-Query（多查询生成）

```python
def generate_multi_queries(query: str, n: int = 3) -> list[str]:
    """生成多个语义等价但表述不同的查询"""
    prompt = f"""针对以下问题，生成 {n} 个不同角度的查询变体，用于检索相关文档。
原始问题：{query}

输出格式（每行一个）：
1. 查询变体1
2. 查询变体2
3. 查询变体3"""

    response = openai_client.chat.completions.create(
        model="gpt-4.1",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=200
    )
    
    lines = response.choices[0].message.content.strip().split('\n')
    queries = []
    for line in lines:
        # 去掉序号前缀
        cleaned = line.strip().lstrip('0123456789. ')
        if cleaned:
            queries.append(cleaned)
    
    return [query] + queries  # 包含原始 query


def multi_query_retrieve(query: str, retriever, embed_fn, top_k: int = 5) -> list[str]:
    queries = generate_multi_queries(query, n=3)
    
    all_rankings = []
    for q in queries:
        q_vec = embed_fn([q])[0]
        results = retriever.vector_index.search(q_vec, top_k * 2)
        all_rankings.append(results)
    
    fused = reciprocal_rank_fusion(all_rankings)
    
    # 去重
    seen = set()
    unique_docs = []
    for doc_id, _ in fused:
        doc = retriever.docs[doc_id]
        if doc not in seen:
            seen.add(doc)
            unique_docs.append(doc)
        if len(unique_docs) >= top_k:
            break
    
    return unique_docs
```

### Step-Back Prompting

Step-Back 的思路是：把具体问题抽象成更高层的原则性问题，先检索通用背景，再结合背景回答具体问题。

```python
def step_back_query(query: str) -> str:
    """将具体问题转化为更抽象的背景性问题"""
    prompt = f"""请将以下具体问题转化为一个更抽象、更通用的背景性问题，
用于先检索相关背景知识。

具体问题：{query}
背景性问题："""

    response = openai_client.chat.completions.create(
        model="gpt-4.1",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=100
    )
    return response.choices[0].message.content.strip()

# 示例：
# 原始: "GPT-4.1 的 context window 是多少？"
# Step-Back: "OpenAI 模型的 context window 是如何设计的？"
# 先检索"context window 设计"的背景知识，再检索具体数字
```

## Parent-Child 分块策略

这是解决"切块太小精度下降，切块太大噪声太多"矛盾的经典方案：

- **Child chunks（小块）**：200-400 tokens，用于向量检索（精度高）
- **Parent chunks（大块）**：1500-2000 tokens，检索命中后传给 LLM（上下文完整）

```python
from langchain.text_splitter import RecursiveCharacterTextSplitter
from dataclasses import dataclass

@dataclass
class Chunk:
    id: str
    text: str
    parent_id: str | None = None
    children_ids: list[str] = None

def create_parent_child_chunks(
    document: str,
    parent_chunk_size: int = 1500,
    child_chunk_size: int = 300,
    overlap: int = 50
) -> tuple[list[Chunk], list[Chunk]]:
    
    parent_splitter = RecursiveCharacterTextSplitter(
        chunk_size=parent_chunk_size,
        chunk_overlap=overlap
    )
    child_splitter = RecursiveCharacterTextSplitter(
        chunk_size=child_chunk_size,
        chunk_overlap=overlap
    )
    
    parent_texts = parent_splitter.split_text(document)
    parent_chunks = []
    child_chunks = []
    
    for p_idx, parent_text in enumerate(parent_texts):
        parent_id = f"parent_{p_idx}"
        child_ids = []
        
        child_texts = child_splitter.split_text(parent_text)
        for c_idx, child_text in enumerate(child_texts):
            child_id = f"child_{p_idx}_{c_idx}"
            child_chunks.append(Chunk(
                id=child_id,
                text=child_text,
                parent_id=parent_id
            ))
            child_ids.append(child_id)
        
        parent_chunks.append(Chunk(
            id=parent_id,
            text=parent_text,
            children_ids=child_ids
        ))
    
    return parent_chunks, child_chunks


class ParentChildRetriever:
    def __init__(self, document: str, embed_fn, vector_index):
        self.embed_fn = embed_fn
        self.vector_index = vector_index
        
        parent_chunks, child_chunks = create_parent_child_chunks(document)
        
        # 只把 child chunks 存入向量库
        self.child_map = {c.id: c for c in child_chunks}
        self.parent_map = {p.id: p for p in parent_chunks}
        
        child_texts = [c.text for c in child_chunks]
        child_vecs = embed_fn(child_texts)
        for child, vec in zip(child_chunks, child_vecs):
            vector_index.add(child.id, vec)
    
    def retrieve(self, query: str, top_k: int = 3) -> list[str]:
        query_vec = self.embed_fn([query])[0]
        child_ids = self.vector_index.search(query_vec, top_k * 2)
        
        # 去重：同一个 parent 只取一次
        seen_parents = set()
        parent_texts = []
        for child_id in child_ids:
            child = self.child_map.get(child_id)
            if child and child.parent_id not in seen_parents:
                parent = self.parent_map[child.parent_id]
                parent_texts.append(parent.text)
                seen_parents.add(child.parent_id)
            if len(parent_texts) >= top_k:
                break
        
        return parent_texts
```

## 自适应 RAG：路由机制

不是所有问题都需要 RAG，一个好的 RAG 系统应该知道什么时候检索，什么时候直接回答：

```python
from enum import Enum

class QueryRoute(Enum):
    DIRECT = "direct"        # 直接用 LLM 回答
    RAG = "rag"              # 走向量检索
    WEB_SEARCH = "web_search"  # 走 Web 搜索（实时信息）

def route_query(query: str) -> QueryRoute:
    """路由决策，可以用规则也可以用 LLM"""
    prompt = f"""判断以下问题应该如何回答：
1. direct：通用知识，LLM 直接回答即可
2. rag：需要查询内部知识库
3. web_search：需要实时信息（新闻、当前价格、最新数据等）

问题：{query}
输出（只输出 direct/rag/web_search）："""

    response = openai_client.chat.completions.create(
        model="gpt-4.1",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=20,
        temperature=0
    )
    
    route_str = response.choices[0].message.content.strip().lower()
    try:
        return QueryRoute(route_str)
    except ValueError:
        return QueryRoute.RAG  # 默认走 RAG


def adaptive_rag_answer(query: str, retriever) -> str:
    route = route_query(query)
    
    if route == QueryRoute.DIRECT:
        # 直接用 LLM 回答
        response = openai_client.chat.completions.create(
            model="gpt-4.1",
            messages=[{"role": "user", "content": query}]
        )
        return response.choices[0].message.content
    
    elif route == QueryRoute.RAG:
        # 走完整 RAG 流程
        contexts = retriever.retrieve(query)
        context_str = "\n\n".join(contexts)
        response = openai_client.chat.completions.create(
            model="gpt-4.1",
            messages=[{
                "role": "user",
                "content": f"基于以下资料回答问题：\n\n{context_str}\n\n问题：{query}"
            }]
        )
        return response.choices[0].message.content
    
    elif route == QueryRoute.WEB_SEARCH:
        # 这里接入 Tavily 或 Bing Search API
        # 省略具体实现
        pass
```

## 用 RAGAS 评估定位问题

RAGAS 是目前最常用的 RAG 评估框架，能精确定位是检索问题还是生成问题：

```python
from ragas import evaluate
from ragas.metrics import (
    faithfulness,          # 生成内容是否忠实于检索到的文档
    answer_relevancy,      # 答案是否和问题相关
    context_precision,     # 检索到的文档是否都有用（精度）
    context_recall,        # 相关文档是否都被检索到（召回）
)
from datasets import Dataset

# 准备评测数据
eval_data = {
    "question": ["RAG 是什么？", "如何优化 Embedding 模型？"],
    "answer": ["RAG 是检索增强生成...", "可以通过选择合适的模型..."],
    "contexts": [["文档1", "文档2"], ["文档3"]],
    "ground_truth": ["标准答案1", "标准答案2"]
}

dataset = Dataset.from_dict(eval_data)
result = evaluate(
    dataset,
    metrics=[faithfulness, answer_relevancy, context_precision, context_recall]
)

print(result)
# Output 示例：
# {'faithfulness': 0.85, 'answer_relevancy': 0.78,
#  'context_precision': 0.72, 'context_recall': 0.68}
```

**如何用 RAGAS 定位问题：**
- `context_recall` 低 → 检索召回问题，尝试 HyDE 或多查询
- `context_precision` 低 → 检索精度问题，加 Reranker
- `faithfulness` 低 → 生成问题，检查 Prompt 或换更强的 LLM
- `answer_relevancy` 低 → 通常是 Prompt 设计问题

## 组合策略建议

不是所有高级技术都要同时上，优先级建议如下：

1. **首先**：加 Reranker（投入产出比最高，代码量少，效果显著）
2. **其次**：换好的 Embedding 模型（中文换 BGE-M3）
3. **再次**：Parent-Child 分块（解决长文档切块问题）
4. **进阶**：HyDE + 多查询（解决 query-doc 语义鸿沟）
5. **最后**：自适应路由（减少不必要的检索开销）

每加一层技术就用 RAGAS 跑一次评测，确认确实有提升再继续。过度工程化的 RAG 系统往往比简单的 Naive RAG + 好 Reranker 效果还差。
