---
title: "RAG 评估体系：RAGAS 指标与幻觉检测实践"
date: 2026-02-05T10:20:00+08:00
draft: false
tags: ["RAG", "RAGAS", "大模型评估", "幻觉检测", "AI工程化", "向量数据库"]
categories: ["大模型"]
series: ["AI 工程化实战"]
description: "系统化评估 RAG 系统：RAGAS 四大指标详解、评估数据集构建、幻觉检测方法、检索质量评估，以及如何把评估集成进 CI/CD 流水线"
summary: "RAG 系统上线后，'感觉回答质量还不错'不是一个可持续的评估方式。RAGAS 提供了一套可量化的评估框架，让你能追踪 Faithfulness、Answer Relevancy 等指标随时间的变化，并在每次改动后自动验证系统质量没有退化。"
toc: true
math: false
diagram: false
keywords: ["RAGAS", "RAG评估", "Faithfulness", "Answer Relevancy", "Context Precision", "幻觉检测", "MRR", "NDCG", "评估数据集"]
params:
  reading_time: true
---

我们团队的 RAG 系统上线三个月后，产品经理过来说：「感觉最近回答质量变差了。」这句话让我非常被动——「感觉」是无法量化的，我也没办法证明「其实没变差」，更没办法定位是哪个环节出了问题。

这次经历让我下决心建立系统化的 RAG 评估体系。这篇文章记录了我们从「靠感觉」到「靠数据」的转型过程。

## 为什么 RAG 系统需要系统化评估

RAG 系统的质量由多个环节共同决定：

```
用户问题 → 检索 → 上下文拼装 → LLM 生成 → 最终回答
```

每个环节都可能出问题：
- 检索环节：相关文档没被找到（召回率低）
- 检索环节：检索到了不相关的文档（精确率低）
- 生成环节：LLM 没有基于检索内容回答（幻觉）
- 生成环节：回答没有针对用户问题（相关性差）

**主观评估的问题**：
1. 无法追踪趋势——每次改动后无法知道质量是提升还是下降
2. 评估者的主观标准不一致，A 觉得好 B 觉得差
3. 无法支撑 A/B 测试——不知道改进方案是否真的有效
4. 无法大规模评估——人工评估 100 个问题就已经很费力了

**RAGAS 解决的问题**：提供可量化、可自动化的评估指标，让评估可以持续运行、可以集成进 CI/CD、可以用数据驱动优化决策。

## RAGAS 四大指标详解

RAGAS（Retrieval Augmented Generation Assessment）提供了四个核心评估指标：

### 指标 1：Faithfulness（忠实度）

**衡量什么**：生成的回答是否忠实于检索到的上下文，即有没有「编造」上下文中不存在的信息。

**计算方式**：
1. 把回答分解成一组原子性陈述（claims）
2. 对每个陈述，用 LLM 判断它是否能从上下文中推断出来
3. `Faithfulness = 可以从上下文推断的陈述数 / 总陈述数`

**分数范围**：0 到 1，越高越好。低 Faithfulness 意味着高幻觉风险。

示例：
- 上下文：「产品 A 的价格是 299 元，支持 7 天退换货。」
- 回答：「产品 A 价格是 299 元，支持 7 天退换货，并且提供两年保修。」
- 「两年保修」这个陈述无法从上下文推断 → Faithfulness < 1

### 指标 2：Answer Relevancy（回答相关性）

**衡量什么**：回答是否针对了用户的问题，有没有答非所问或者废话连篇。

**计算方式**：
1. 让 LLM 根据回答反向生成 N 个可能的问题
2. 计算这些反向问题和原始问题的相似度（Embedding 余弦相似度）
3. 取平均值作为 Answer Relevancy

**分数范围**：0 到 1。注意：Answer Relevancy 不衡量事实准确性，只衡量相关性——如果回答很相关但内容是错的，分数依然高。

### 指标 3：Context Precision（上下文精确率）

**衡量什么**：检索到的上下文中，有多少比例是真正有用的（signal vs noise）。

**计算方式**：
- 对每个检索到的文档块，判断它是否对生成正确回答有帮助
- `Context Precision = 有用的文档块数 / 总检索文档块数`

低 Context Precision 意味着检索引入了太多噪声，可能让 LLM 被无关内容干扰。

### 指标 4：Context Recall（上下文召回率）

**衡量什么**：ground truth 回答中的关键信息，有多少比例能在检索到的上下文中找到。

**计算方式**：
- 把 ground truth 回答分解成原子性陈述
- 对每个陈述，判断它是否能从检索到的上下文中归因
- `Context Recall = 能在上下文中找到来源的陈述数 / 总陈述数`

需要 ground truth，适合有标注数据集的场景。

**四个指标的关系总结：**

| 指标 | 评估对象 | 需要 ground truth？ | 解决的问题 |
|------|---------|-------------------|-----------|
| Faithfulness | 生成质量 | 否 | 检测幻觉 |
| Answer Relevancy | 生成质量 | 否 | 检测答非所问 |
| Context Precision | 检索质量 | 否 | 检测检索噪声 |
| Context Recall | 检索质量 | 是 | 检测检索遗漏 |

## 如何构建评估数据集

评估数据集是整个评估体系的基础。构建方式分两类：

### 方法一：LLM 自动生成（快速启动）

RAGAS 提供了 `TestsetGenerator`，可以从你的文档库自动生成问答对：

```python
from ragas.testset.generator import TestsetGenerator
from ragas.testset.evolutions import simple, reasoning, multi_context
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_community.document_loaders import DirectoryLoader

# 加载知识库文档
loader = DirectoryLoader("./docs", glob="**/*.md")
documents = loader.load()

# 初始化生成器
generator_llm = ChatOpenAI(model="gpt-4o")
critic_llm = ChatOpenAI(model="gpt-4o")
embeddings = OpenAIEmbeddings()

generator = TestsetGenerator.from_langchain(
    generator_llm,
    critic_llm,
    embeddings
)

# 生成测试集
testset = generator.generate_with_langchain_docs(
    documents,
    test_size=50,
    distributions={
        simple: 0.5,        # 简单问题（50%）
        reasoning: 0.25,    # 需要推理的问题（25%）
        multi_context: 0.25 # 需要多文档综合的问题（25%）
    }
)

# 转换为 DataFrame 查看
df = testset.to_pandas()
print(df[["question", "ground_truth", "context"]].head())

# 保存
df.to_csv("evaluation_dataset.csv", index=False)
```

### 方法二：人工标注（高质量）

LLM 生成的测试集质量参差不齐，最好做一轮人工审核：

```python
import pandas as pd
import json

def create_annotation_template(questions: list[str]) -> pd.DataFrame:
    """创建人工标注模板"""
    return pd.DataFrame({
        "question": questions,
        "ground_truth": [""] * len(questions),  # 标注人填写
        "reference_docs": [""] * len(questions),  # 相关文档路径
        "difficulty": ["medium"] * len(questions),  # easy/medium/hard
        "category": ["general"] * len(questions),  # 问题分类
        "notes": [""] * len(questions)
    })

# 标注规范
ANNOTATION_GUIDE = """
标注规范：
1. ground_truth：写完整、准确的参考答案，不要太简短
2. reference_docs：填写这个问题答案来源的文档路径（可多个，逗号分隔）
3. difficulty：easy（直接查找）/ medium（需要理解）/ hard（需要推理或多文档综合）
4. 如果问题本身有歧义，在 notes 中说明
"""
```

### 测试集质量检查

```python
def validate_testset(df: pd.DataFrame) -> dict:
    """检查测试集质量"""
    issues = []

    # 检查 ground_truth 是否太短
    short_answers = df[df["ground_truth"].str.len() < 20]
    if len(short_answers) > 0:
        issues.append(f"{len(short_answers)} 条 ground_truth 过短（<20字符）")

    # 检查重复问题
    duplicates = df[df["question"].duplicated()]
    if len(duplicates) > 0:
        issues.append(f"{len(duplicates)} 条重复问题")

    # 检查问题多样性（简单用长度分布）
    q_lengths = df["question"].str.len()
    print(f"问题长度分布: min={q_lengths.min()}, median={q_lengths.median():.0f}, max={q_lengths.max()}")

    return {
        "total": len(df),
        "issues": issues,
        "quality_score": 1 - len(issues) / 10  # 粗略质量分
    }
```

## 用 RAGAS 跑评估：完整代码示例

```python
import asyncio
import pandas as pd
from datasets import Dataset
from ragas import evaluate
from ragas.metrics import (
    faithfulness,
    answer_relevancy,
    context_precision,
    context_recall
)
from ragas.llms import LangchainLLMWrapper
from langchain_openai import ChatOpenAI
from langchain_anthropic import ChatAnthropic

# 你的 RAG 系统
class YourRAGSystem:
    def __init__(self, vector_store, llm):
        self.vector_store = vector_store
        self.llm = llm

    async def retrieve(self, question: str, k: int = 5) -> list[str]:
        """检索相关文档"""
        docs = await self.vector_store.asimilarity_search(question, k=k)
        return [doc.page_content for doc in docs]

    async def generate(self, question: str, contexts: list[str]) -> str:
        """基于上下文生成回答"""
        context_text = "\n\n".join(contexts)
        prompt = f"""基于以下参考资料回答问题。如果资料中没有相关信息，请说明无法从资料中找到答案。

参考资料：
{context_text}

问题：{question}
"""
        response = await self.llm.ainvoke(prompt)
        return response.content

    async def query(self, question: str) -> tuple[str, list[str]]:
        contexts = await self.retrieve(question)
        answer = await self.generate(question, contexts)
        return answer, contexts

async def run_evaluation(rag_system: YourRAGSystem, testset_path: str) -> dict:
    """运行 RAGAS 评估"""
    # 加载测试集
    df = pd.read_csv(testset_path)
    print(f"评估测试集大小: {len(df)} 条")

    # 对每个问题运行 RAG 系统，收集结果
    results = []
    for _, row in df.iterrows():
        question = row["question"]
        ground_truth = row.get("ground_truth", "")

        answer, contexts = await rag_system.query(question)
        results.append({
            "question": question,
            "answer": answer,
            "contexts": contexts,
            "ground_truth": ground_truth
        })

    # 转换为 RAGAS Dataset 格式
    eval_dataset = Dataset.from_list(results)

    # 配置评估用的 LLM（可以和 RAG 系统用不同的模型）
    evaluator_llm = LangchainLLMWrapper(ChatOpenAI(model="gpt-4o"))

    # 运行评估
    metrics_to_use = [faithfulness, answer_relevancy, context_precision]
    if df["ground_truth"].notna().all() and (df["ground_truth"] != "").all():
        metrics_to_use.append(context_recall)

    result = evaluate(
        eval_dataset,
        metrics=metrics_to_use,
        llm=evaluator_llm,
    )

    # 输出结果
    scores = result.to_pandas()
    summary = {
        "faithfulness": scores["faithfulness"].mean(),
        "answer_relevancy": scores["answer_relevancy"].mean(),
        "context_precision": scores["context_precision"].mean(),
    }
    if "context_recall" in scores.columns:
        summary["context_recall"] = scores["context_recall"].mean()

    return summary, scores

# 运行
async def main():
    rag = YourRAGSystem(vector_store, llm)
    summary, detailed = await run_evaluation(rag, "evaluation_dataset.csv")

    print("\n=== 评估结果 ===")
    for metric, score in summary.items():
        status = "✓" if score > 0.7 else "✗"
        print(f"{status} {metric}: {score:.3f}")

    # 保存详细结果
    detailed.to_csv("eval_results.csv", index=False)

asyncio.run(main())
```

## 幻觉检测：判断答案是否基于检索内容

除了 RAGAS 的 Faithfulness 指标，实际应用中还需要一个更轻量的幻觉检测机制——能在运行时实时检测，而不只是离线评估。

```python
import anthropic
import json
from typing import Literal

client = anthropic.Anthropic()

def detect_hallucination(
    question: str,
    answer: str,
    contexts: list[str]
) -> dict:
    """
    检测回答是否存在幻觉
    返回: {hallucinated: bool, unsupported_claims: list, confidence: float}
    """
    context_text = "\n\n---\n\n".join(
        [f"[文档 {i+1}]\n{ctx}" for i, ctx in enumerate(contexts)]
    )

    prompt = f"""你是一个事实核查助手。请分析以下回答中的每个声明是否有文档支撑。

参考文档：
{context_text}

问题：{question}

回答：{answer}

请执行以下步骤：
1. 将回答分解为独立的事实声明（每句话或每个具体说法）
2. 对每个声明，判断它是否能从参考文档中找到依据
3. 标记无法从文档中找到依据的声明

以 JSON 格式返回：
{{
    "claims": [
        {{"text": "声明内容", "supported": true/false, "source_doc": 1 或 null}}
    ],
    "overall_faithfulness": 0.0到1.0,
    "has_hallucination": true/false,
    "unsupported_claims": ["无支撑的声明1", ...]
}}"""

    response = client.messages.create(
        model="claude-3-5-sonnet-20241022",
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}]
    )

    try:
        result = json.loads(response.content[0].text)
        return {
            "hallucinated": result["has_hallucination"],
            "unsupported_claims": result["unsupported_claims"],
            "faithfulness_score": result["overall_faithfulness"],
            "detailed_claims": result["claims"]
        }
    except json.JSONDecodeError:
        return {
            "hallucinated": None,
            "error": "解析失败",
            "raw_response": response.content[0].text
        }

# 使用示例
result = detect_hallucination(
    question="我们产品的退款政策是什么？",
    answer="我们支持 30 天无理由退款，并且提供免费上门取件服务。",
    contexts=["本产品支持 30 天内无理由退款，退款需通过官网申请。运费由买家承担。"]
)
print(f"存在幻觉: {result['hallucinated']}")
print(f"无支撑声明: {result['unsupported_claims']}")
# 输出: 存在幻觉: True
# 无支撑声明: ["提供免费上门取件服务"]
```

## 检索质量评估

除了端到端的 RAGAS 指标，检索环节本身也需要评估。常用指标：

```python
import numpy as np
from typing import Optional

def hit_rate(retrieved_ids: list[str], relevant_ids: list[str]) -> float:
    """Hit Rate：检索到的文档中是否包含至少一个相关文档"""
    retrieved_set = set(retrieved_ids)
    relevant_set = set(relevant_ids)
    return 1.0 if retrieved_set & relevant_set else 0.0

def mrr(retrieved_ids: list[str], relevant_ids: list[str]) -> float:
    """Mean Reciprocal Rank：第一个相关文档出现在第几位（越前越好）"""
    relevant_set = set(relevant_ids)
    for i, doc_id in enumerate(retrieved_ids):
        if doc_id in relevant_set:
            return 1.0 / (i + 1)
    return 0.0

def ndcg(retrieved_ids: list[str], relevant_ids: list[str], k: Optional[int] = None) -> float:
    """Normalized Discounted Cumulative Gain：综合考虑相关性和排名"""
    relevant_set = set(relevant_ids)
    if k:
        retrieved_ids = retrieved_ids[:k]

    dcg = sum(
        1.0 / np.log2(i + 2)
        for i, doc_id in enumerate(retrieved_ids)
        if doc_id in relevant_set
    )

    # 理想 DCG：所有相关文档都排在前面
    ideal_hits = min(len(relevant_ids), len(retrieved_ids))
    idcg = sum(1.0 / np.log2(i + 2) for i in range(ideal_hits))

    return dcg / idcg if idcg > 0 else 0.0

def evaluate_retrieval(testset: list[dict]) -> dict:
    """
    评估检索质量
    testset: [{"question": str, "retrieved_ids": list, "relevant_ids": list}, ...]
    """
    hit_rates, mrrs, ndcgs = [], [], []

    for sample in testset:
        retrieved = sample["retrieved_ids"]
        relevant = sample["relevant_ids"]

        hit_rates.append(hit_rate(retrieved, relevant))
        mrrs.append(mrr(retrieved, relevant))
        ndcgs.append(ndcg(retrieved, relevant, k=5))

    return {
        "hit_rate@5": np.mean(hit_rates),
        "mrr@5": np.mean(mrrs),
        "ndcg@5": np.mean(ndcgs)
    }

# 评估示例
testset = [
    {
        "question": "产品退款政策",
        "retrieved_ids": ["doc_003", "doc_007", "doc_001", "doc_012", "doc_005"],
        "relevant_ids": ["doc_003", "doc_015"]  # ground truth 相关文档
    },
    # ...更多测试用例
]

metrics = evaluate_retrieval(testset)
print(f"Hit Rate@5: {metrics['hit_rate@5']:.3f}")
print(f"MRR@5:      {metrics['mrr@5']:.3f}")
print(f"NDCG@5:     {metrics['ndcg@5']:.3f}")
```

## CI 集成：每次改动自动跑评估

把评估集成进 CI/CD，确保每次改动不会导致质量退化：

```yaml
# .github/workflows/rag-eval.yml
name: RAG Quality Evaluation

on:
  pull_request:
    paths:
      - 'rag/**'          # RAG 代码变更触发
      - 'prompts/**'      # Prompt 变更触发
      - 'embeddings/**'   # Embedding 模型变更触发

jobs:
  evaluate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install dependencies
        run: pip install ragas langchain-openai pytest

      - name: Run RAG evaluation
        env:
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
        run: |
          python scripts/run_eval.py \
            --testset data/eval_testset.csv \
            --output eval_results.json \
            --baseline metrics/baseline.json

      - name: Check quality gates
        run: python scripts/check_quality_gates.py eval_results.json

      - name: Comment on PR
        uses: actions/github-script@v7
        with:
          script: |
            const fs = require('fs');
            const results = JSON.parse(fs.readFileSync('eval_results.json'));
            const body = `## RAG 评估结果
            | 指标 | 当前值 | 基准值 | 状态 |
            |------|--------|--------|------|
            | Faithfulness | ${results.faithfulness.toFixed(3)} | ${results.baseline.faithfulness.toFixed(3)} | ${results.faithfulness >= results.baseline.faithfulness * 0.95 ? '✅' : '❌'} |
            | Answer Relevancy | ${results.answer_relevancy.toFixed(3)} | ${results.baseline.answer_relevancy.toFixed(3)} | ${results.answer_relevancy >= results.baseline.answer_relevancy * 0.95 ? '✅' : '❌'} |
            | Context Precision | ${results.context_precision.toFixed(3)} | ${results.baseline.context_precision.toFixed(3)} | ${results.context_precision >= results.baseline.context_precision * 0.95 ? '✅' : '❌'} |
            `;
            github.rest.issues.createComment({
              issue_number: context.issue.number,
              owner: context.repo.owner,
              repo: context.repo.repo,
              body: body
            });
```

质量门禁脚本：

```python
# scripts/check_quality_gates.py
import json
import sys

def check_quality_gates(results_path: str):
    with open(results_path) as f:
        results = json.load(f)

    # 绝对值门禁：低于这个值直接失败
    ABSOLUTE_THRESHOLDS = {
        "faithfulness": 0.70,
        "answer_relevancy": 0.65,
        "context_precision": 0.60,
    }

    # 相对退化门禁：相比 baseline 退化超过 5% 失败
    REGRESSION_THRESHOLD = 0.05

    failures = []

    baseline = results.get("baseline", {})
    for metric, threshold in ABSOLUTE_THRESHOLDS.items():
        current = results.get(metric, 0)

        # 绝对值检查
        if current < threshold:
            failures.append(
                f"{metric} ({current:.3f}) 低于最低阈值 ({threshold})"
            )
            continue

        # 相对退化检查
        if baseline.get(metric):
            regression = (baseline[metric] - current) / baseline[metric]
            if regression > REGRESSION_THRESHOLD:
                failures.append(
                    f"{metric} 相比 baseline 退化 {regression*100:.1f}%"
                )

    if failures:
        print("❌ 质量门禁未通过：")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    else:
        print("✅ 所有质量门禁通过")

if __name__ == "__main__":
    check_quality_gates(sys.argv[1])
```

## 评估结果如何指导 RAG 优化

评估数据是优化的地图。根据不同的指标问题，优化方向不同：

| 问题 | 指标表现 | 优化方向 |
|------|---------|---------|
| 检索到的文档不相关 | Context Precision 低 | 优化 Embedding 模型、调整检索策略（混合检索）、添加元数据过滤 |
| 关键文档没被检索到 | Context Recall 低 | 优化分块策略（chunk size/overlap）、改进查询重写、添加关键词检索 |
| LLM 编造了上下文没有的信息 | Faithfulness 低 | 优化 System Prompt（明确要求基于文档回答）、添加引用要求 |
| 回答与问题关联度低 | Answer Relevancy 低 | 优化 Prompt 模板、添加问题理解步骤 |
| 全面偏低 | 所有指标 | 重新检查整体流程，可能是测试集质量问题 |

一个实用的「诊断优先」原则：**先看检索指标，再看生成指标**。如果 Context Precision/Recall 都很好，但 Faithfulness 低，那是生成环节的问题；如果检索指标本身就差，改 Prompt 没有用。

评估体系建一次累一次，之后每次优化都能拿数据说话，再也不用和 PM 扯"感觉"。
