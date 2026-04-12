---
title: "Dify 私有化部署与 RAG 应用构建实战"
date: 2026-04-12T18:00:00+08:00
draft: false
tags: ["Dify", "RAG", "私有化部署", "知识库", "LLM应用"]
categories: ["AI应用"]
description: "从Docker Compose部署到RAG知识库、工作流编排的Dify完整实战指南"
summary: "Dify是当前私有化部署最成熟的LLM应用构建平台。本文覆盖Docker Compose部署、多模型Provider配置、知识库创建与切片调优、RAG对话应用构建、工作流编排，以及API发布与生产监控。"
toc: true
math: false
diagram: false
keywords: ["Dify", "RAG", "知识库", "私有化部署", "工作流", "LLM应用平台"]
params:
  reading_time: true
---

在做内部AI应用时，选型通常会在Dify和FastGPT之间纠结。Dify的定位更偏"平台"——它不只是知识库问答，还支持复杂的工作流编排、多种应用类型（聊天机器人、文本生成、Agent）。如果你的需求超出"问知识库"的范畴，Dify是更合适的选择。

这篇文章记录从零开始部署Dify并构建一个运维知识库问答应用的完整过程。

---

## Docker Compose部署

Dify官方提供了Docker Compose配置，适合自托管场景。

### 系统要求

- CPU：4核以上
- 内存：8GB以上（跑embedding模型需要更多）
- 磁盘：50GB以上（向量数据库 + 文档存储）
- Docker 20.10+，Docker Compose 2.x

### 部署步骤

**克隆仓库**：

```bash
git clone https://github.com/langgenius/dify.git
cd dify/docker
```

**配置环境变量**：

```bash
cp .env.example .env
```

编辑`.env`，关键配置：

```bash
# 必须修改的配置
SECRET_KEY=your-random-secret-key-here  # 随机字符串，用于加密
INIT_PASSWORD=your-admin-password       # 初始管理员密码

# 数据库（默认用docker-compose里的postgres，生产建议用外部数据库）
DB_USERNAME=postgres
DB_PASSWORD=your-db-password
DB_HOST=db
DB_PORT=5432
DB_DATABASE=dify

# Redis
REDIS_HOST=redis
REDIS_PORT=6379
REDIS_PASSWORD=your-redis-password

# 向量数据库（默认weaviate，也支持pgvector/qdrant/milvus）
VECTOR_STORE=weaviate

# 存储（本地文件系统或S3）
STORAGE_TYPE=local
STORAGE_LOCAL_PATH=storage

# 如果用S3：
# STORAGE_TYPE=s3
# S3_ENDPOINT=https://s3.amazonaws.com
# S3_BUCKET_NAME=your-bucket
# S3_ACCESS_KEY=your-access-key
# S3_SECRET_KEY=your-secret-key
# S3_REGION=us-east-1
```

**启动服务**：

```bash
docker compose up -d
```

第一次启动会拉取所有镜像（大约5-10分钟），启动后访问`http://your-server-ip`。

**验证服务**：

```bash
docker compose ps
# 应该看到以下服务都是 Up 状态：
# dify-api-1, dify-worker-1, dify-web-1
# dify-db-1, dify-redis-1, dify-weaviate-1, dify-nginx-1
```

### 生产部署注意事项

**数据库外置**：Docker Compose里的PostgreSQL不适合生产。建议用外部RDS，修改`.env`里的`DB_HOST`指向外部数据库。

**持久化存储**：确保`docker-compose.yaml`里的volume挂载点在有足够空间的磁盘上：

```bash
# 检查当前挂载
docker volume ls | grep dify
# dify_db_data
# dify_weaviate_data
# dify_app_data
```

**反向代理**：在Nginx前面加SSL终止，生产环境必须用HTTPS，涉及API Key等敏感信息。

**资源限制**：给各容器设置resource limits，避免单个服务耗尽宿主机资源：

```yaml
# docker-compose.yaml
services:
  api:
    deploy:
      resources:
        limits:
          cpus: '2'
          memory: 4G
```

---

## 配置LLM Provider

部署完成后，第一步是配置语言模型。Dify支持多种Provider。

### 进入设置

管理员登录后，右上角 → 设置 → 模型供应商。

### 配置OpenAI

点击OpenAI旁边的"设置"：
- API Key：`sk-your-openai-api-key`
- 如果需要代理：设置Base URL为代理地址

添加后，点击"验证"确认连接正常。

### 配置Anthropic

- API Key：`sk-ant-your-anthropic-api-key`
- 支持Claude 3系列模型

### 配置本地模型（Ollama）

如果有GPU机器跑本地模型：

1. 先在本地跑Ollama：`ollama serve`
2. 拉取模型：`ollama pull llama3`
3. 在Dify里选择"Ollama"Provider
4. 设置Base URL：`http://your-ollama-host:11434`
5. 填入模型名：`llama3`

### 配置Embedding模型

RAG知识库需要单独配置Embedding模型（用于向量化文档）。

进入设置 → 模型供应商 → 找到你的Provider → 配置Embedding模型。

推荐组合：
- 高质量：OpenAI的`text-embedding-3-large`
- 性价比：`text-embedding-3-small`
- 本地：`nomic-embed-text`（通过Ollama）

**重要**：Embedding模型一旦用于知识库，不要随意更换。更换后所有文档需要重新向量化。

---

## 创建知识库

知识库是RAG应用的核心，这里重点讲切片配置对效果的影响。

### 新建知识库

主页 → 知识库 → 创建知识库

输入名称，选择使用的Embedding模型。

### 上传文档

支持格式：PDF、Markdown、TXT、HTML、CSV、Word。

上传方式三种：
1. **本地上传**：直接拖拽文件
2. **同步网站**：输入URL，Dify会爬取页面（适合文档站点）
3. **Notion集成**：通过OAuth连接Notion，同步指定页面

### 切片配置

这是影响RAG效果最大的配置，值得认真调。

**自动切分 vs 手动切分**

对于结构良好的文档（有标题层级、段落清晰），用自动切分：
- 按段落分割
- 最大token数：500-1000
- 重叠：50-100 token

对于日志、表格、代码等非结构化内容，建议手动切分（上传前处理好格式）。

**切片大小的权衡**

- 切片太大：检索到的文本包含太多无关信息，影响LLM输出质量
- 切片太小：单个切片上下文不足，答案可能不完整

经验值：
- 普通文档：500-800 token每片
- 技术文档/手册：800-1200 token（一个完整的操作步骤）
- FAQ：按问题切割，每问一片

**索引方式**

- **高质量**（推荐）：向量检索 + 关键词检索，效果最好，但需要配置LLM和Embedding模型
- **经济**：只用关键词检索（BM25），不消耗LLM token，效果一般

### 文档预处理技巧

上传前对文档做预处理，能显著提升效果：

```python
# 预处理示例：去掉PDF导出时的页眉页脚
import re

def clean_pdf_text(text):
    # 去掉页码
    text = re.sub(r'\n\d+\n', '\n', text)
    # 去掉重复的页眉
    text = re.sub(r'公司内部文档 \d{4}-\d{2}-\d{2}\n', '', text)
    # 合并被换行打断的段落
    text = re.sub(r'(?<!\n)\n(?!\n)', ' ', text)
    return text
```

---

## 构建RAG对话应用

知识库准备好后，创建一个基于知识库的对话应用。

### 创建应用

主页 → 工作室 → 创建应用 → 聊天助手

### 配置提示词

系统提示词对最终效果影响很大。一个运维知识库问答的系统提示词示例：

```
你是一个运维技术助手，专门回答关于我们内部运维系统的问题。

**回答规范**：
1. 只基于提供的知识库内容回答，如果知识库里没有相关信息，明确说"我在知识库里没有找到相关信息"
2. 回答要具体、可操作，直接给出步骤或命令
3. 如果问题涉及风险操作（生产环境变更、数据删除等），要在回答里加上风险提醒
4. 引用具体的文档名称和章节，方便用户查找原文

**不要做的事**：
- 不要基于通用知识臆测，只用知识库内容
- 不要给出模糊的答案，如果不确定，说明不确定
```

### 关联知识库

在"上下文"区域，点击"添加"，选择刚才创建的知识库。

关键配置：
- **召回策略**：N选一（多路召回效果更好）
- **召回条数（TopK）**：默认3-5，运维文档通常5-8个片段
- **相似度分数阈值**：0.5-0.7，低于此分数的结果不返回

### 测试和调优

应用调试界面里，用真实问题测试：

1. 问题覆盖面：核心场景都能正确回答
2. 边界情况：知识库没有的问题，是否正确拒绝
3. 引用准确性：答案里引用的文档是否真实存在

**常见问题排查**：

**Q：回答正确但没有引用来源**
- 检查提示词是否要求引用
- 在变量设置里开启"引用与归因"

**Q：相似问题答错了**
- 查看"召回测试"里，这类问题检索到了哪些片段
- 可能是切片问题：关键信息和问题分布在不同切片了
- 调整切片策略重新索引

**Q：回答里有幻觉（编造了知识库没有的内容）**
- 强化提示词里的"只基于知识库回答"限制
- 降低模型的temperature参数（在模型设置里）

---

## 工作流编排

Dify的工作流（Workflow）是比聊天机器人更强大的应用类型，支持条件分支、循环、多步骤处理。

### 创建工作流应用

创建应用 → 工作流

工作流是可视化节点图，支持拖拽连线。

### 核心节点类型

**LLM节点**：调用语言模型，可以配置提示词模板

**知识检索节点**：从知识库检索相关内容，输出召回的文本

**代码节点**：执行Python代码，适合数据处理、格式转换

```python
# 代码节点示例：从日志中提取错误信息
def main(log_text: str) -> dict:
    import re
    errors = re.findall(r'ERROR.*', log_text)
    return {
        "error_count": len(errors),
        "errors": errors[:10]  # 最多返回10条
    }
```

**条件分支节点**：根据条件选择不同执行路径

**HTTP请求节点**：调用外部API，适合集成内部系统

**迭代节点**：对列表数据循环处理

### 实战：告警分析工作流

一个实用的工作流：接收告警信息，自动查询知识库给出处理建议。

节点连接：

```
开始（输入：告警内容）
  ↓
代码节点（提取关键字段：告警名、严重级别、涉及服务）
  ↓
知识检索节点（用提取的信息检索运维手册）
  ↓
LLM节点（综合告警信息和检索结果，生成处理建议）
  ↓
条件分支（判断严重级别）
  ├─ Critical → HTTP请求节点（发送紧急通知）
  └─ Warning  → LLM节点（生成工单摘要）
  ↓
结束（输出处理建议和通知状态）
```

代码节点示例（解析告警）：

```python
def main(alert_text: str) -> dict:
    import re
    
    # 解析Prometheus格式告警
    name_match = re.search(r'alertname="([^"]+)"', alert_text)
    severity_match = re.search(r'severity="([^"]+)"', alert_text)
    service_match = re.search(r'service="([^"]+)"', alert_text)
    
    return {
        "alert_name": name_match.group(1) if name_match else "unknown",
        "severity": severity_match.group(1) if severity_match else "unknown",
        "service": service_match.group(1) if service_match else "unknown",
        "search_query": f"{name_match.group(1) if name_match else ''} {service_match.group(1) if service_match else ''} 处理方法"
    }
```

---

## API发布与集成

Dify应用可以通过API对外发布，集成到现有系统。

### 获取API Key

应用详情页 → API访问 → 创建API Key

### API调用示例

**发送消息**：

```bash
curl -X POST 'https://your-dify-domain/v1/chat-messages' \
  -H 'Authorization: Bearer app-your-api-key' \
  -H 'Content-Type: application/json' \
  -d '{
    "inputs": {},
    "query": "K8s节点NotReady怎么排查？",
    "response_mode": "streaming",
    "user": "ops-user-001"
  }'
```

**Python集成**：

```python
import requests
import json

class DifyClient:
    def __init__(self, api_key: str, base_url: str):
        self.api_key = api_key
        self.base_url = base_url
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
    
    def chat(self, query: str, user_id: str, conversation_id: str = "") -> str:
        payload = {
            "inputs": {},
            "query": query,
            "response_mode": "blocking",
            "user": user_id,
        }
        if conversation_id:
            payload["conversation_id"] = conversation_id
        
        response = requests.post(
            f"{self.base_url}/v1/chat-messages",
            headers=self.headers,
            json=payload,
            timeout=60
        )
        response.raise_for_status()
        result = response.json()
        return result["answer"]

# 使用
client = DifyClient(
    api_key="app-your-api-key-here",
    base_url="https://your-dify-domain"
)
answer = client.chat("Prometheus告警规则怎么写？", "user-123")
print(answer)
```

### 集成钉钉机器人

通过工作流的HTTP节点，在回答生成后自动推送到钉钉：

```python
# 工作流里的代码节点：格式化钉钉消息
def main(answer: str, question: str) -> dict:
    message = {
        "msgtype": "markdown",
        "markdown": {
            "title": "运维知识库回答",
            "text": f"**问题**：{question}\n\n**回答**：\n{answer}"
        }
    }
    return {"dingtalk_payload": json.dumps(message)}
```

---

## 监控与日志

### 查看使用统计

主页 → 概览 可以看到：
- API调用次数
- Token消耗
- 活跃用户数
- 平均响应时间

### 查看对话日志

应用详情 → 日志 可以查看所有对话记录，包括：
- 用户输入
- 系统回答
- 召回的文档片段
- Token消耗
- 响应时间

这是排查问题最重要的入口。

### 标注与优化

在日志里找到回答质量差的对话，点击"标注"：
- 可以写下正确答案
- 这些标注可以用于微调（需要Pro版）
- 也可以作为Few-shot示例加入提示词

### 监控关键指标

通过Dify的API可以拿到监控数据，接入Prometheus：

值得监控的指标：
- 响应时间P99（超过10秒通常有问题）
- Token消耗速率（成本控制）
- 错误率（API调用失败率）
- 知识库召回率（查询有没有召回到相关文档）

---

## 版本升级

Dify迭代很快，升级流程：

```bash
cd dify/docker

# 备份数据库
docker exec dify-db-1 pg_dump -U postgres dify > dify_backup_$(date +%Y%m%d).sql

# 拉取新版本
git pull
docker compose pull

# 重启服务
docker compose down
docker compose up -d

# 检查服务状态
docker compose ps
docker compose logs api --tail=50
```

升级后注意检查：数据库migration是否自动完成（看api日志），核心功能是否正常。
