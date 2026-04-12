---
title: "RAG 系统设计与实战：检索增强生成完全指南"
date: 2025-11-11T11:41:00+08:00
draft: false
tags: ["RAG", "大模型", "向量数据库", "Embedding", "LangChain", "检索增强生成"]
categories: ["大模型"]
description: "RAG 系统从设计到生产的完整指南：文档分块、Embedding选型、混合检索、Rerank重排序、RAGAS评估，以及生产踩坑记录。"
summary: "RAG（检索增强生成）是目前企业落地 LLM 最主流的方式。本文覆盖 RAG 系统的完整设计：文档处理管线、分块策略、向量检索与关键词混合检索、Rerank 重排序、上下文压缩，以及用 RAGAS 框架评估 RAG 质量，最后分享生产环境踩坑记录。"
toc: true
math: false
diagram: false
series: ["AI 工程化实战"]
keywords: ["RAG", "检索增强生成", "向量数据库", "Embedding", "Rerank", "RAGAS", "混合检索", "文档分块"]
params:
  reading_time: true
---

RAG（Retrieval-Augmented Generation，检索增强生成）是目前最主流的 LLM 落地方式。它的核心思路很简单：与其把所有知识塞进模型参数里（Fine-tuning），不如在用户提问时实时检索相关文档，把文档内容放进上下文让模型回答。

这个架构解决了 LLM 两个核心问题：知识截止日期，以及私有知识无法直接使用。

---

## RAG vs Fine-tuning：怎么选

先说清楚两者的适用边界：

| 维度 | RAG | Fine-tuning |
|-----|-----|------------|
| 知识更新频率 | 高（随时更新） | 低（重新训练成本高） |
| 需要的数据量 | 有文档即可 | 需要大量标注数据 |
| 知识边界 | 清晰（可追溯来源） | 模糊（嵌入参数里） |
| 推理成本 | 每次检索有开销 | 无额外开销 |
| 适合场景 | 知识库问答、文档查询 | 风格迁移、特定格式输出 |

**实践结论**：
- 你有大量文档需要 LLM 能回答？→ RAG
- 你需要模型以特定风格/格式输出？→ Fine-tuning 或 Prompt Engineering
- 两者都需要？→ Fine-tuning 基础模型 + RAG 叠加知识库（最佳效果，最高成本）

---

## RAG 系统整体架构

```
离线流程（Indexing Pipeline）：
文档 → 解析 → 分块 → Embedding → 向量数据库

在线流程（Query Pipeline）：
用户问题 → Query改写 → 检索（向量+关键词）→ Rerank → 上下文组装 → LLM生成 → 答案
```

---

## 文档处理管线

### 支持的文档类型

实际项目里往往要处理各种格式：

```python
from pathlib import Path
from typing import Protocol

class DocumentParser(Protocol):
    def parse(self, file_path: Path) -> str:
        ...

class PDFParser:
    def parse(self, file_path: Path) -> str:
        # 推荐 pymupdf（fitz），比 pdfplumber 快且准
        import fitz
        doc = fitz.open(str(file_path))
        text = ""
        for page in doc:
            text += page.get_text()
        return text

class WordParser:
    def parse(self, file_path: Path) -> str:
        from docx import Document
        doc = Document(str(file_path))
        return "\n".join(para.text for para in doc.paragraphs)

class HTMLParser:
    def parse(self, file_path: Path) -> str:
        from bs4 import BeautifulSoup
        content = file_path.read_text(encoding="utf-8")
        soup = BeautifulSoup(content, "html.parser")
        # 移除脚本和样式
        for tag in soup(["script", "style", "nav", "footer"]):
            tag.decompose()
        return soup.get_text(separator="\n", strip=True)

def get_parser(file_path: Path) -> DocumentParser:
    parsers = {
        ".pdf": PDFParser(),
        ".docx": WordParser(),
        ".html": HTMLParser(),
        ".htm": HTMLParser(),
        ".md": lambda p: p.read_text(),
        ".txt": lambda p: p.read_text(),
    }
    suffix = file_path.suffix.lower()
    parser = parsers.get(suffix)
    if not parser:
        raise ValueError(f"不支持的文件格式: {suffix}")
    return parser
```

### 分块策略

文档分块（Chunking）是 RAG 质量最关键的环节之一，直接影响检索精度。

**固定大小分块**（最简单）：

```python
from langchain.text_splitter import RecursiveCharacterTextSplitter

splitter = RecursiveCharacterTextSplitter(
    chunk_size=512,      # 每块约512字符
    chunk_overlap=50,    # 相邻块重叠50字符，避免语义在边界处断裂
    separators=["\n\n", "\n", "。", "！", "？", " ", ""],
)

chunks = splitter.split_text(document_text)
```

**语义分块**（效果更好，成本更高）：

```python
from langchain_experimental.text_splitter import SemanticChunker
from langchain_openai import OpenAIEmbeddings

semantic_splitter = SemanticChunker(
    embeddings=OpenAIEmbeddings(),
    breakpoint_threshold_type="percentile",
    breakpoint_threshold_amount=95,  # 语义相似度低于95分位数则分块
)

chunks = semantic_splitter.split_text(document_text)
```

**按文档结构分块**（对有标题层级的文档最好）：

```python
from langchain.text_splitter import MarkdownHeaderTextSplitter

headers_to_split_on = [
    ("#", "h1"),
    ("##", "h2"),
    ("###", "h3"),
]

md_splitter = MarkdownHeaderTextSplitter(
    headers_to_split_on=headers_to_split_on,
    strip_headers=False,
)

md_header_splits = md_splitter.split_text(markdown_document)
```

**chunk_size 选择的经验值：**
- 技术文档、FAQ：256-512 tokens
- 长篇报告、书籍章节：512-1024 tokens
- 代码片段：按函数/类分块，不按固定大小

---

## Embedding 模型选型

Embedding 质量直接决定检索质量。

### 主流选择对比

| 模型 | 维度 | 最大输入 | 中文支持 | 成本 |
|-----|------|--------|---------|-----|
| text-embedding-3-large | 3072 | 8191 tokens | 良好 | $0.13/1M tokens |
| text-embedding-3-small | 1536 | 8191 tokens | 良好 | $0.02/1M tokens |
| BGE-M3 | 1024 | 8192 tokens | 优秀 | 开源，自部署 |
| BCE-embedding-base | 768 | 512 tokens | 优秀 | 开源，自部署 |
| Jina-embeddings-v3 | 1024 | 8192 tokens | 良好 | API或自部署 |

**实践选型建议：**
- 中文为主的业务：BGE-M3 或 BCE（BAAI 出品，专门针对中文优化）
- 需要多语言：text-embedding-3-large
- 成本敏感：text-embedding-3-small（质量下降可接受）
- 私有部署（数据不出内网）：BGE-M3（1张 T4 可部署）

```python
# BGE-M3 本地部署示例（使用 FlagEmbedding）
from FlagEmbedding import BGEM3FlagModel

model = BGEM3FlagModel(
    "BAAI/bge-m3",
    use_fp16=True,  # 节省显存
    device="cuda"
)

embeddings = model.encode(
    ["文本1", "文本2"],
    batch_size=32,
    max_length=8192,
    return_dense=True,      # 稠密向量，用于语义检索
    return_sparse=True,     # 稀疏向量，可与 BM25 结合
    return_colbert_vecs=False
)

dense_vecs = embeddings["dense_vecs"]
```

---

## 向量数据库选型

### 主流向量数据库对比

| 数据库 | 适合场景 | 特点 |
|-------|---------|-----|
| Milvus | 大规模生产 | 功能最全，运维复杂 |
| Qdrant | 中等规模生产 | Rust 实现，性能好，API 简洁 |
| Weaviate | 企业级 | 内置混合检索，GraphQL 查询 |
| Chroma | 开发/原型 | 轻量，纯 Python，零配置 |
| pgvector | 已有 PostgreSQL | 无需新组件，SQL 友好 |
| FAISS | 离线批处理 | Meta 出品，无持久化 |

**我的选择经验**：
- 开发阶段：Chroma（本地文件存储，不需要部署任何服务）
- 中小规模生产（<1000万向量）：Qdrant 或 pgvector
- 大规模生产（>1亿向量）：Milvus

```python
# Qdrant 使用示例
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct

client = QdrantClient("localhost", port=6333)

# 创建集合
client.create_collection(
    collection_name="knowledge_base",
    vectors_config=VectorParams(
        size=1536,          # embedding 维度
        distance=Distance.COSINE
    ),
)

# 批量插入
points = [
    PointStruct(
        id=i,
        vector=embedding,
        payload={
            "text": chunk_text,
            "source": doc_path,
            "chunk_index": chunk_idx,
        }
    )
    for i, (embedding, chunk_text, doc_path, chunk_idx) 
    in enumerate(zip(embeddings, texts, sources, indices))
]

client.upsert(collection_name="knowledge_base", points=points)

# 搜索
results = client.search(
    collection_name="knowledge_base",
    query_vector=query_embedding,
    limit=10,
    with_payload=True,
)
```

---

## 混合检索：向量 + 关键词

纯向量检索有个缺陷：对于包含专有名词、代码、人名的查询，语义相似度不如关键词匹配准确。混合检索结合两者的优势。

### BM25 + 向量的混合检索

```python
from rank_bm25 import BM25Okapi
import numpy as np

class HybridRetriever:
    def __init__(self, chunks: list[str], embeddings: np.ndarray):
        self.chunks = chunks
        self.embeddings = embeddings
        
        # BM25 索引
        tokenized = [chunk.split() for chunk in chunks]
        self.bm25 = BM25Okapi(tokenized)
    
    def retrieve(
        self,
        query: str,
        query_embedding: np.ndarray,
        top_k: int = 10,
        alpha: float = 0.5,   # 向量检索权重，1-alpha 为 BM25 权重
    ) -> list[tuple[int, float]]:
        # BM25 分数
        bm25_scores = self.bm25.get_scores(query.split())
        bm25_scores = (bm25_scores - bm25_scores.min()) / (bm25_scores.max() - bm25_scores.min() + 1e-8)
        
        # 向量相似度分数
        vec_scores = np.dot(self.embeddings, query_embedding)
        vec_scores = (vec_scores - vec_scores.min()) / (vec_scores.max() - vec_scores.min() + 1e-8)
        
        # 加权融合
        hybrid_scores = alpha * vec_scores + (1 - alpha) * bm25_scores
        
        top_indices = np.argsort(hybrid_scores)[::-1][:top_k]
        return [(int(idx), float(hybrid_scores[idx])) for idx in top_indices]
```

**Reciprocal Rank Fusion (RRF)** 是另一种常用的融合方法，不需要分数归一化：

```python
def reciprocal_rank_fusion(
    ranked_lists: list[list[int]], 
    k: int = 60
) -> list[tuple[int, float]]:
    """
    ranked_lists: 多个排序列表，每个元素是文档ID列表
    k: RRF 常数，通常设为60
    """
    scores = {}
    for ranked_list in ranked_lists:
        for rank, doc_id in enumerate(ranked_list):
            if doc_id not in scores:
                scores[doc_id] = 0
            scores[doc_id] += 1 / (k + rank + 1)
    
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)
```

---

## Rerank 重排序

初步检索（召回）的目标是不漏，Rerank 的目标是精准。两个阶段分工明确：

- **召回阶段**：向量检索，取 top-50 或 top-100，速度快
- **Rerank 阶段**：交叉编码器精排，取 top-5 或 top-10，质量高

```python
from sentence_transformers import CrossEncoder

# BGE-Reranker-v2-m3 在中英文混合场景效果很好
reranker = CrossEncoder("BAAI/bge-reranker-v2-m3", device="cuda")

def rerank(query: str, passages: list[str], top_k: int = 5) -> list[tuple[str, float]]:
    """
    query: 用户问题
    passages: 初步检索的文档列表（较多，如50个）
    top_k: 重排后保留的数量
    """
    pairs = [[query, passage] for passage in passages]
    scores = reranker.predict(pairs)
    
    ranked = sorted(
        zip(passages, scores),
        key=lambda x: x[1],
        reverse=True
    )
    return ranked[:top_k]
```

**Reranker 的 API 版本（不需要本地 GPU）**：

```python
import cohere

co = cohere.Client(api_key="your-api-key")

results = co.rerank(
    query="RAG 系统如何处理文档分块",
    documents=candidate_passages,
    top_n=5,
    model="rerank-multilingual-v3.0",  # 支持中文
)

reranked_passages = [result.document["text"] for result in results.results]
```

---

## 上下文压缩

检索到的文档可能包含很多与问题无关的内容，上下文压缩可以减少噪音和 token 消耗：

```python
from langchain.retrievers import ContextualCompressionRetriever
from langchain.retrievers.document_compressors import LLMChainExtractor
from langchain_openai import ChatOpenAI

llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)

# 用 LLM 从检索到的文档中提取只与问题相关的部分
compressor = LLMChainExtractor.from_llm(llm)

compression_retriever = ContextualCompressionRetriever(
    base_compressor=compressor,
    base_retriever=base_retriever,
)

compressed_docs = compression_retriever.invoke("什么是 RAG 的分块策略")
```

注意：LLM 压缩有额外的 API 调用成本，在高频场景下要评估是否值得。轻量替代方案是用嵌入相似度来过滤句子：

```python
def extract_relevant_sentences(
    query_embedding: np.ndarray,
    document: str,
    embedding_fn,
    threshold: float = 0.5
) -> str:
    """保留与 query 语义相似度高于阈值的句子"""
    sentences = document.split("。")
    sentence_embeddings = embedding_fn(sentences)
    
    similarities = np.dot(sentence_embeddings, query_embedding)
    relevant = [s for s, sim in zip(sentences, similarities) if sim > threshold]
    
    return "。".join(relevant)
```

---

## RAGAS 评估框架

RAG 系统的评估比普通 LLM 应用更复杂，因为有两个组件（检索和生成）都可能出问题。RAGAS 提供了一套标准化的评估指标：

```bash
pip install ragas
```

```python
from ragas import evaluate
from ragas.metrics import (
    faithfulness,           # 答案是否忠于检索文档（0-1）
    answer_relevancy,       # 答案是否回答了问题（0-1）
    context_precision,      # 检索文档的精确率（0-1）
    context_recall,         # 检索文档的召回率（0-1）
)
from datasets import Dataset

# 构建评测数据集
data = {
    "question": [
        "RAG 的全称是什么",
        "文档分块的 chunk_size 应该设多少",
    ],
    "answer": [
        "RAG 全称是 Retrieval-Augmented Generation，即检索增强生成。",
        "对于技术文档，建议使用 256-512 tokens；长篇报告可以用 512-1024 tokens。",
    ],
    "contexts": [
        ["RAG（Retrieval-Augmented Generation）是一种将..."],
        ["文档分块是 RAG 最关键的环节...", "chunk_size 的选择需要根据..."],
    ],
    "ground_truth": [
        "Retrieval-Augmented Generation",
        "取决于文档类型，一般 256-1024 tokens",
    ],
}

dataset = Dataset.from_dict(data)

result = evaluate(
    dataset,
    metrics=[faithfulness, answer_relevancy, context_precision, context_recall],
)

print(result)
# {'faithfulness': 0.85, 'answer_relevancy': 0.92, 'context_precision': 0.78, ...}
```

**四个核心指标的含义：**

- **Faithfulness（忠实度）**：答案中的事实是否都能从检索文档中找到依据。分数低说明模型在"发明"信息（幻觉）。
- **Answer Relevancy（答案相关性）**：答案是否真正回答了问题。分数低说明答案跑题。
- **Context Precision（上下文精确率）**：检索到的文档中，有多少是真正有用的。分数低说明检索引入了噪音。
- **Context Recall（上下文召回率）**：回答问题所需的信息，有多少被检索到了。分数低说明检索漏掉了关键信息。

---

## 生产踩坑记录

### 坑1：PDF 解析质量差

用 `pdfminer` 或 `pypdf` 解析双栏 PDF 时，文字顺序经常错乱（两栏的内容混在一起）。

解决方案：改用 `pymupdf`（fitz），对布局的处理更好；对于扫描版 PDF，需要先跑 OCR（推荐 `paddleocr`）。

### 坑2：向量数据库冷启动

Milvus 和 Qdrant 在内存里缓存向量，第一次查询时需要加载到内存，可能比较慢。

解决方案：在服务启动时做一次预热查询，或者对 Qdrant 配置 `on_disk: false` 强制内存存储。

### 坑3：Embedding 维度不一致

更换 Embedding 模型后，旧的向量无法直接使用（维度不同），需要重新跑全量 Embedding。

解决方案：在 metadata 里记录 embedding_model 字段，升级时用版本号区分集合，逐步迁移。

### 坑4：检索质量随文档量增加而下降

文档库增大后，检索精度下降是正常现象，但有些情况是因为文档质量参差不齐（大量低质量文档淹没了高质量的）。

解决方案：
1. 在索引阶段对文档质量打分，低于阈值的不入库
2. 使用 Metadata Filter 限定检索范围（如只检索某个时间段或某个类别的文档）

```python
# Qdrant 带 filter 的检索
results = client.search(
    collection_name="knowledge_base",
    query_vector=query_embedding,
    query_filter={
        "must": [
            {"key": "category", "match": {"value": "技术文档"}},
            {"key": "quality_score", "range": {"gte": 0.7}},
        ]
    },
    limit=10,
)
```

### 坑5：中文分词影响 BM25 效果

BM25 基于词频统计，中文需要先分词。直接用空格分割会导致 BM25 检索效果很差。

解决方案：使用 `jieba` 或 `pkuseg` 对中文进行分词：

```python
import jieba

def tokenize_zh(text: str) -> list[str]:
    return list(jieba.cut(text))

# 创建 BM25 索引时使用分词
tokenized_chunks = [tokenize_zh(chunk) for chunk in chunks]
bm25 = BM25Okapi(tokenized_chunks)

# 查询时也需要分词
query_tokens = tokenize_zh(query)
scores = bm25.get_scores(query_tokens)
```

### 坑6：上下文窗口溢出

检索到 10 个文档，每个 512 tokens，加上系统提示和问题，很容易超过模型的上下文限制。

解决方案：
1. 在组装 prompt 前统计 token 数，动态决定用几个文档
2. 对检索到的文档按相关性排序，优先用排名靠前的
3. 使用上下文压缩减少每个文档的 token 占用

```python
import tiktoken

def build_rag_prompt(
    query: str,
    retrieved_docs: list[str],
    system_prompt: str,
    max_context_tokens: int = 3000
) -> str:
    encoder = tiktoken.encoding_for_model("gpt-4o")
    
    context_parts = []
    used_tokens = 0
    
    for doc in retrieved_docs:
        doc_tokens = len(encoder.encode(doc))
        if used_tokens + doc_tokens > max_context_tokens:
            break
        context_parts.append(doc)
        used_tokens += doc_tokens
    
    context = "\n\n---\n\n".join(context_parts)
    return f"{system_prompt}\n\n参考资料：\n{context}\n\n问题：{query}"
```
