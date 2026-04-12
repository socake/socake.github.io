---
title: "Langfuse：LLM 应用可观测性平台实战"
date: 2026-04-12T10:30:00+08:00
draft: false
tags: ["Langfuse", "可观测性", "LLM", "LangChain", "Prompt管理"]
categories: ["大模型"]
description: "LLM应用可观测性从0到1：Langfuse自托管、SDK集成、Prompt版本管理与成本分析"
summary: "讲清楚为什么LLM应用必须要可观测性，以及如何用Langfuse从链路追踪、Prompt版本管理、评估实验到成本分析做到全覆盖，包含Docker自托管部署和Python SDK完整集成示例。"
toc: true
math: false
diagram: false
keywords: ["Langfuse", "LLM可观测性", "Prompt管理", "LangChain集成", "成本追踪"]
params:
  reading_time: true
---

传统应用的可观测性（日志、指标、链路追踪）用在 LLM 应用上只解决了一半问题。LLM 应用还面临一类特有的可观测性需求：prompt 改了效果是否变好、哪个用户问了哪些问题、某次回答为什么不对、token 消耗在哪些地方最多。Langfuse 是目前开源生态里最完整解决这个问题的工具。

## 为什么 LLM 应用需要专门的可观测性

一个生产 RAG 系统的一次请求链路大概是这样：

```
用户提问
  → 问题改写（LLM call #1）
  → 向量检索（Milvus）
  → 重排序（reranker）
  → 生成回答（LLM call #2，含3000 token上下文）
  → 返回用户
```

如果回答质量不好，你需要知道：
- 是 LLM call #1 改写得有问题导致检索偏了？
- 还是检索结果本来就不相关？
- 还是 LLM call #2 的 prompt 没有引导好？

没有结构化的追踪数据，只能靠猜。Langfuse 让你把整个链路的每个步骤都记录下来，包括输入输出、延迟、token 消耗，还能打用户评分、做 A/B 实验。

---

## 自托管部署

Langfuse 提供云服务，但很多场景需要自托管（数据不出境、成本控制）。

### Docker Compose 部署

```bash
# 克隆仓库（有 compose 文件）
git clone https://github.com/langfuse/langfuse.git
cd langfuse

# 或者直接用官方提供的最小化 compose
```

```yaml
# docker-compose.yml
version: "3.8"

services:
  langfuse-server:
    image: langfuse/langfuse:2
    depends_on:
      db:
        condition: service_healthy
    ports:
      - "3000:3000"
    environment:
      - DATABASE_URL=postgresql://postgres:password@db:5432/langfuse
      - NEXTAUTH_SECRET=your-nextauth-secret-32chars-min
      - SALT=your-salt-32chars-min
      - ENCRYPTION_KEY=your-encryption-key-32chars
      - NEXTAUTH_URL=http://localhost:3000
      - TELEMETRY_ENABLED=false
      # 邮件配置（可选）
      # - SMTP_CONNECTION_URL=smtp://user:pass@smtp.example.com:587

  db:
    image: postgres:15-alpine
    environment:
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: password
      POSTGRES_DB: langfuse
    volumes:
      - langfuse_pg_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]
      interval: 5s
      timeout: 5s
      retries: 10

volumes:
  langfuse_pg_data:
```

```bash
docker-compose up -d

# 访问 http://localhost:3000，注册第一个账号即为管理员
# 创建 Project，获取 Public Key 和 Secret Key
```

### Kubernetes 部署

```yaml
# langfuse-deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: langfuse
  namespace: monitoring
spec:
  replicas: 2
  selector:
    matchLabels:
      app: langfuse
  template:
    metadata:
      labels:
        app: langfuse
    spec:
      containers:
      - name: langfuse
        image: langfuse/langfuse:2
        ports:
        - containerPort: 3000
        env:
        - name: DATABASE_URL
          valueFrom:
            secretKeyRef:
              name: langfuse-secrets
              key: database-url
        - name: NEXTAUTH_SECRET
          valueFrom:
            secretKeyRef:
              name: langfuse-secrets
              key: nextauth-secret
        - name: SALT
          valueFrom:
            secretKeyRef:
              name: langfuse-secrets
              key: salt
        - name: NEXTAUTH_URL
          value: "https://langfuse.internal.example.com"
        resources:
          requests:
            memory: "512Mi"
            cpu: "250m"
          limits:
            memory: "1Gi"
            cpu: "1"
```

---

## Python SDK 集成

### 基础 Trace

```python
import os
from langfuse import Langfuse
from openai import OpenAI

# 初始化
langfuse = Langfuse(
    public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
    secret_key=os.environ["LANGFUSE_SECRET_KEY"],
    host=os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com")
)

openai_client = OpenAI()

def answer_question(user_id: str, session_id: str, question: str) -> str:
    # 创建 trace（一次完整的用户交互）
    trace = langfuse.trace(
        name="qa-pipeline",
        user_id=user_id,
        session_id=session_id,
        input={"question": question},
        metadata={"version": "1.2.0"}
    )

    try:
        # 记录检索步骤
        retrieval_span = trace.span(
            name="vector-retrieval",
            input={"query": question}
        )

        # 实际检索逻辑（这里用伪代码）
        chunks = do_vector_search(question)  # 你的检索函数

        retrieval_span.end(
            output={"chunk_count": len(chunks)},
            metadata={"collection": "knowledge_base"}
        )

        # 记录 LLM 调用
        context = "\n".join([c["text"] for c in chunks])
        messages = [
            {"role": "system", "content": "根据上下文回答问题。"},
            {"role": "user", "content": f"上下文：\n{context}\n\n问题：{question}"}
        ]

        generation = trace.generation(
            name="answer-generation",
            model="gpt-4o-mini",
            model_parameters={"temperature": 0.1, "max_tokens": 1024},
            input=messages
        )

        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            temperature=0.1,
            max_tokens=1024
        )

        answer = response.choices[0].message.content

        generation.end(
            output=answer,
            usage={
                "input": response.usage.prompt_tokens,
                "output": response.usage.completion_tokens,
                "total": response.usage.total_tokens,
                "unit": "TOKENS"
            }
        )

        # 更新 trace 的最终输出
        trace.update(output={"answer": answer})

        return answer

    except Exception as e:
        trace.update(
            output={"error": str(e)},
            metadata={"status": "error"}
        )
        raise
    finally:
        # 确保数据发送
        langfuse.flush()
```

### 用户反馈收集

```python
def collect_user_feedback(trace_id: str, score: float, comment: str = None):
    """
    score: 0-1，1表示好
    在前端收集用户点赞/踩后调用
    """
    langfuse.score(
        trace_id=trace_id,
        name="user-feedback",
        value=score,
        comment=comment,
        data_type="NUMERIC"
    )
```

---

## 与 LangChain 集成

LangChain 集成是最简单的方式，通过 callback 自动记录所有链路：

```python
from langchain_openai import ChatOpenAI
from langchain.schema import HumanMessage
from langfuse.callback import CallbackHandler

# 创建 Langfuse callback handler
langfuse_handler = CallbackHandler(
    public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
    secret_key=os.environ["LANGFUSE_SECRET_KEY"],
    host=os.environ.get("LANGFUSE_HOST"),
    # 可以在这里指定 trace 属性
    user_id="user-123",
    session_id="session-456",
    trace_name="langchain-chat"
)

# 在 LangChain 调用时传入 callback
llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)

response = llm.invoke(
    [HumanMessage(content="解释一下向量数据库的工作原理")],
    config={"callbacks": [langfuse_handler]}
)

print(langfuse_handler.get_trace_url())  # 打印 trace 链接，方便调试
```

### LangChain LCEL 链

```python
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_openai import ChatOpenAI

prompt = ChatPromptTemplate.from_messages([
    ("system", "你是一个专业的技术文档助手。"),
    ("human", "{question}")
])

llm = ChatOpenAI(model="gpt-4o-mini")
chain = prompt | llm | StrOutputParser()

# 每次调用传入 callback
result = chain.invoke(
    {"question": "什么是RAG？"},
    config={"callbacks": [langfuse_handler]}
)
```

---

## 与 LlamaIndex 集成

```python
from llama_index.core import Settings
from llama_index.core.callbacks import CallbackManager
from langfuse.llama_index import LlamaIndexCallbackHandler

langfuse_callback_handler = LlamaIndexCallbackHandler(
    public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
    secret_key=os.environ["LANGFUSE_SECRET_KEY"],
)

# 设置为全局 callback
Settings.callback_manager = CallbackManager([langfuse_callback_handler])

# 之后所有 LlamaIndex 操作自动被追踪
from llama_index.core import VectorStoreIndex, SimpleDirectoryReader

documents = SimpleDirectoryReader("./docs").load_data()
index = VectorStoreIndex.from_documents(documents)
query_engine = index.as_query_engine()

response = query_engine.query("如何配置Kubernetes资源限制？")
```

---

## Prompt 版本管理

这是 Langfuse 里最容易被忽视但最有价值的功能。把 prompt 放在 Langfuse 管理，而不是硬编码在代码里：

```python
# 在 Langfuse UI 创建 prompt，然后用 SDK 获取
prompt_template = langfuse.get_prompt(
    name="qa-system-prompt",
    version=3  # 不指定则获取最新生产版本
)

# 使用 prompt
compiled_prompt = prompt_template.compile(
    context=context_text,
    language="中文"
)

# 在 generation 中关联 prompt，方便后续追踪哪个版本效果好
generation = trace.generation(
    name="answer-gen",
    model="gpt-4o-mini",
    prompt=prompt_template,  # 关联 prompt 版本
    input=compiled_prompt
)
```

Prompt 版本管理的工作流：
1. 在 UI 里创建新版本 prompt
2. 先推给 staging 环境测试
3. 评估效果满意后标记为 Production
4. 代码中用 `version=None` 自动跟随 Production 版本

---

## 评估 Dataset 与实验对比

```python
# 创建评估数据集
dataset = langfuse.create_dataset(
    name="qa-eval-v1",
    description="QA 系统评估集，100条典型问题"
)

# 添加测试样本
items = [
    {"input": "如何重启Kubernetes Pod？", "expected_output": "kubectl delete pod <name>"},
    {"input": "查看Pod日志的命令？", "expected_output": "kubectl logs <pod-name>"},
    # ...
]

for item in items:
    dataset.create_item(
        input=item["input"],
        expected_output=item["expected_output"]
    )

# 跑评估实验
dataset = langfuse.get_dataset("qa-eval-v1")

for item in dataset.items:
    # 用你的系统回答
    answer = answer_question("eval-user", "eval-session", item.input)

    # 关联到 dataset item，记录这次实验结果
    item.link(
        trace_or_observation=trace,  # 刚才 answer_question 里创建的 trace
        run_name="experiment-v1.3"   # 实验名称
    )

    # 可以加上自动评分（比如用 LLM 作为 judge）
    langfuse.score(
        trace_id=trace.id,
        name="correctness",
        value=evaluate_with_llm(item.input, answer, item.expected_output),
        data_type="NUMERIC"
    )
```

在 Langfuse UI 的 Datasets 页面，可以横向对比不同 `run_name` 的指标，直观看出哪个版本更好。

---

## 成本追踪与分析

Langfuse 内置了基于 token 的成本计算，只要在 generation 里正确传入 usage：

```python
generation.end(
    output=response_text,
    usage={
        "input": prompt_tokens,
        "output": completion_tokens,
        "total": total_tokens,
        "unit": "TOKENS"
    }
)
```

Langfuse 会根据模型自动匹配单价（GPT-4o、Claude、Gemini 等都内置了），在 Dashboard 里可以看到：
- 按用户/项目的成本分布
- 按时间的成本趋势
- Token 使用效率（输入/输出比）

**成本优化的常见发现**：
1. 某个用户/功能的 token 消耗异常高 → 检查是否上下文窗口管理有问题
2. 输入 token 远多于输出 → 可能 system prompt 太长或检索到的 chunk 太多
3. 某些请求重复调用 → 考虑加缓存层

---

## 生产运维注意事项

**异步发送**：Langfuse SDK 默认异步批量发送，不阻塞主流程。但程序退出前要调用 `langfuse.flush()` 确保数据不丢。

**采样**：高并发场景下可以只记录部分 trace：
```python
import random

if random.random() < 0.1:  # 10% 采样
    trace = langfuse.trace(...)
else:
    trace = None  # 后续判断 trace is not None 再调用
```

**敏感信息过滤**：如果 prompt 包含用户 PII，在发送前脱敏：
```python
def sanitize_input(text: str) -> str:
    import re
    # 替换手机号
    text = re.sub(r'1[3-9]\d{9}', '[PHONE]', text)
    # 替换邮箱
    text = re.sub(r'\S+@\S+\.\S+', '[EMAIL]', text)
    return text
```

**多环境隔离**：在 Langfuse 里为 dev/staging/prod 各创建独立 Project，避免测试数据污染生产监控数据。
