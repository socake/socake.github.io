---
title: "SGLang 结构化生成实战：RadixAttention、约束解码与多轮对话优化"
date: 2026-03-14T16:45:00+08:00
draft: false
tags: ["SGLang", "LLM", "推理部署", "结构化生成", "RadixAttention"]
categories: ["推理部署"]
description: "SGLang 是被低估的 LLM 推理框架，RadixAttention 对多轮对话和 Agent 场景收益巨大。本文讲清 SGLang 的核心机制、前端 DSL、约束解码、部署方式和踩坑。"
summary: "SGLang 是被低估的 LLM 推理框架，RadixAttention 对多轮对话和 Agent 场景收益巨大。本文讲清 SGLang 的核心机制、前端 DSL、约束解码、部署方式和踩坑。"
toc: true
math: false
diagram: false
keywords: ["SGLang", "RadixAttention", "Constrained Decoding", "JSON Schema", "LLM Inference"]
params:
  reading_time: true
---

## 为什么我开始用 SGLang

接触 SGLang 之前我一直在 vLLM 和 TRT-LLM 之间打转。两个工具在"单次 completion"这件事上都做到了接近极限，但一旦场景变复杂——**Agent 多轮调用、大量共享 prompt 前缀、需要严格 JSON 输出、带工具调用的循环**——单纯的 vLLM 开始显得笨重。

触发我认真看 SGLang 是一次 Agent 线上故障。我们的 Agent 逻辑大致是：

1. 给模型一个非常长的 system prompt（约 6K token，包含工具定义、示例）
2. 用户每次发消息，模型决定调用哪个工具
3. 工具返回结果拼回 prompt，再让模型生成回复
4. 一个完整对话平均 5-8 轮，每轮都把前面历史塞回去

这种负载对传统的 KV Cache 不友好——每次都是"前缀高度重复，后缀不同"。即使开了 vLLM 的 prefix caching，命中率也会被前缀匹配的精确性吃掉一大块。P95 首 token 延迟稳定在 800ms，不可接受。

切到 SGLang 之后同样的场景首 token 降到 250ms 附近。原因只有一个：**RadixAttention**。这是 SGLang 区别于 vLLM 最核心的武器。这篇文章把 SGLang 的核心机制、前端、部署都讲清楚。

## 一、RadixAttention：核心创新

### 1.1 KV Cache 的共享问题

LLM 推理的 KV Cache 按请求分配，每个请求独立。但实际业务里很多请求的 prompt 前缀高度重合：

- Chat 应用：system prompt 固定
- RAG：检索到的文档大部分稳定
- Agent：工具定义、示例每轮都带
- 多轮对话：前 N 轮历史每次都塞

vLLM 的 prefix caching 把这些重复前缀缓存下来，命中了就直接用缓存的 KV，省掉 prefill 计算。但 vLLM 的实现是**请求级别的精确匹配**——你要么完全命中一段前缀，要么不命中。

### 1.2 RadixAttention 的做法

SGLang 的做法是把所有当前活跃请求的 KV Cache 组织成一棵 **radix tree**（基数树）：

```
             [root]
              │
      ┌───────┴───────┐
   system A         system B
   (固定)            (固定)
      │               │
   ┌──┴──┐         ┌──┴──┐
 user1 user2     user3 user4
  │     │         │     │
 ...   ...       ...   ...
```

每个 prompt 从 root 开始沿树往下找最长公共前缀，找到的部分直接复用已有 KV，只对剩余部分做 prefill。这比"请求粒度"的 prefix caching 细很多：

- 不同请求可以共享**任意长度**的公共前缀
- 新请求加入树后，它的 KV 也对后续请求可见
- LRU 淘汰整条路径，保证活跃前缀常驻

实际效果：

- 多轮对话场景前 N-1 轮的 KV 完全不用重算
- Agent 场景工具定义的 KV 常驻，每个请求只 prefill "用户 query + 模型输出"
- 跑 benchmark 流程的 few-shot prompt，首 token 延迟接近零

### 1.3 和 vLLM prefix caching 的差别

|  | vLLM Prefix Caching | SGLang RadixAttention |
|---|---|---|
| 粒度 | block 级（16 token） | token 级 |
| 数据结构 | hash 表 + 引用计数 | radix tree |
| 共享范围 | 请求完成即淘汰 | 请求完成仍可共享 |
| 命中率 | 中 | 高 |
| 管理复杂度 | 低 | 较高 |

一句话：vLLM 的 prefix cache 偏"幸运命中"，SGLang 的 RadixAttention 是"主动共享"。

## 二、架构总览

```
 ┌───────────────────────────────────────────┐
 │               SGLang Frontend             │
 │  (Python DSL: @sgl.function, sgl.gen ...) │
 └───────────────┬───────────────────────────┘
                 │  HTTP / 本地调用
 ┌───────────────▼───────────────────────────┐
 │              SGLang Runtime               │
 │  ┌─────────────────────────────────────┐  │
 │  │  Tokenizer Manager                  │  │
 │  └──────────────┬──────────────────────┘  │
 │                 │                          │
 │  ┌──────────────▼──────────────────────┐  │
 │  │  Scheduler (Radix 树管理)           │  │
 │  │  - 最长前缀匹配                      │  │
 │  │  - Continuous Batching              │  │
 │  │  - Chunked Prefill                  │  │
 │  └──────────────┬──────────────────────┘  │
 │                 │                          │
 │  ┌──────────────▼──────────────────────┐  │
 │  │  Model Worker (各种 attention 后端) │  │
 │  │  FlashInfer / FlashAttention /      │  │
 │  │  Triton kernel                      │  │
 │  └──────────────┬──────────────────────┘  │
 │                 │                          │
 │  ┌──────────────▼──────────────────────┐  │
 │  │  KV Cache Manager                   │  │
 │  │  (Token 级 Radix Tree)              │  │
 │  └─────────────────────────────────────┘  │
 └───────────────────────────────────────────┘
```

SGLang 分前端和后端两部分：

- **前端**：一个 Python DSL，让你把复杂 prompt 流程（条件生成、并行采样、多轮交互）写成函数式代码
- **后端**：推理运行时，提供 OpenAI 兼容 API 和原生 SGLang API

只用后端服务 OpenAI API 接口是最常见的部署方式，不一定非要用前端 DSL。

## 三、部署后端

### 3.1 启动命令

```bash
python -m sglang.launch_server \
    --model-path /models/Llama-3.1-70B-Instruct \
    --tp-size 8 \
    --mem-fraction-static 0.88 \
    --context-length 8192 \
    --max-running-requests 256 \
    --schedule-policy lpm \
    --disable-radix-cache=false \
    --host 0.0.0.0 --port 30000
```

关键参数：

| 参数 | 含义 | 推荐值 |
|---|---|---|
| `--tp-size` | Tensor Parallel 度 | 看卡数 |
| `--mem-fraction-static` | 类似 vLLM 的 gpu-memory-utilization | 0.85~0.92 |
| `--context-length` | 最大上下文 | 按业务 |
| `--max-running-requests` | 同时跑的请求数 | 128~512 |
| `--schedule-policy` | 调度策略：fcfs / lpm（最长前缀匹配优先） | 多轮场景用 lpm |
| `--disable-radix-cache` | 禁用 radix cache | 除非 debug 否则别关 |
| `--chunked-prefill-size` | chunked prefill 粒度 | 8192 |
| `--attention-backend` | attention kernel 后端 | flashinfer / triton |

### 3.2 attention backend 怎么选

SGLang 支持多个 attention backend：

- **FlashInfer**：专门为 LLM 推理做的 attention 库，对 paged KV 和 RadixAttention 有深度优化
- **FlashAttention 2/3**：老牌 Flash，通用
- **Triton**：SGLang 自己用 Triton 写的 kernel，通用 GPU 支持

实测 H100 上 FlashInfer 最快。A100 上 FlashAttention 2 更稳。L40S / 消费卡用 Triton backend 兼容性最好。

### 3.3 启动验证

```bash
curl http://localhost:30000/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d '{
        "model": "default",
        "messages": [{"role": "user", "content": "你好"}],
        "max_tokens": 64
    }'
```

SGLang 原生支持 OpenAI 兼容接口，直接用 `openai` SDK 调就行：

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:30000/v1", api_key="EMPTY")
resp = client.chat.completions.create(
    model="default",
    messages=[{"role": "user", "content": "写一首五言绝句"}],
    max_tokens=128,
)
```

## 四、前端 DSL：高阶用法

SGLang 的前端 DSL 是它区别于其他框架的另一个亮点。它把 prompt 流程写成 Python 函数，支持条件分支、并行采样、结构化输出。看例子。

### 4.1 基础：一次生成

```python
import sglang as sgl

sgl.set_default_backend(sgl.RuntimeEndpoint("http://localhost:30000"))

@sgl.function
def greet(s, name):
    s += "用户: 你好，我是 " + name + "\n"
    s += "助手: " + sgl.gen("reply", max_tokens=64, stop="\n")

state = greet.run(name="张三")
print(state["reply"])
```

`@sgl.function` 声明一个带状态的 prompt 函数，`s +=` 往对话里加内容，`sgl.gen` 让模型生成一段。整个函数像是在写一段"伪代码 prompt"。

### 4.2 并行采样

```python
@sgl.function
def multi_answer(s, question):
    s += "问题: " + question + "\n"
    forks = s.fork(3)
    forks += "答案: " + sgl.gen("ans", max_tokens=200, temperature=0.9)
    forks.join()
    s += "最终答案: " + sgl.gen("final", max_tokens=400)
```

`s.fork(3)` 让 prompt 分叉成 3 条并行分支，每条独立采样，之后 `join` 回主干。这种模式下 SGLang 会自动让 3 条分支共享公共前缀的 KV Cache，只对后缀并行采样。

### 4.3 条件分支

```python
@sgl.function
def classify_then_generate(s, text):
    s += "文本: " + text + "\n"
    s += "这是关于什么类别？选项: [科技, 体育, 娱乐]\n"
    s += "类别: " + sgl.gen("cat", choices=["科技", "体育", "娱乐"])
    if s["cat"] == "科技":
        s += "\n简要解释这个科技概念：" + sgl.gen("tech_explain", max_tokens=200)
    elif s["cat"] == "体育":
        s += "\n给出一条相关体育新闻：" + sgl.gen("sports_news", max_tokens=200)
    else:
        s += "\n推荐一个相关作品：" + sgl.gen("ent_rec", max_tokens=200)
```

`sgl.gen(..., choices=[...])` 是"强制选项"生成，模型只能输出给定选项之一。然后 `s["cat"]` 在 Python 层可以直接 if/else 分支，不用自己做二次推理。

### 4.4 结构化 JSON 输出

```python
json_schema = r"""{
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "age": {"type": "integer"},
        "skills": {"type": "array", "items": {"type": "string"}}
    },
    "required": ["name", "age", "skills"]
}"""

@sgl.function
def extract(s, text):
    s += "从文本抽取信息返回 JSON：\n" + text + "\n"
    s += sgl.gen("json_out", max_tokens=256, regex=None, json_schema=json_schema)
```

`json_schema` 告诉模型输出必须严格符合 schema，SGLang 在解码时做 token 级掩码，保证非法 token 不会被采样。

## 五、约束解码深入

LLM 结构化输出的落地有三种技术方案：

1. **Prompt 提示**（最原始）：system prompt 里让模型自己按 JSON 输出。不靠谱，长尾时会崩。
2. **Post-hoc 校验**：生成完用 JSON parser 校验，失败就重试。浪费 token 且不稳定。
3. **约束解码**（constrained decoding）：在每个 token 采样前，用一个 FSM/自动机裁剪合法 token 集合。

SGLang 支持的约束类型：

- **正则**：`sgl.gen(..., regex=r"\d{3}-\d{4}")`
- **选项**：`sgl.gen(..., choices=[...])`
- **JSON Schema**：`sgl.gen(..., json_schema=...)`
- **EBNF**：更强大的上下文无关文法

### 5.1 约束解码的性能影响

约束解码不是零成本。每个 step 要维护 FSM 状态、计算当前允许的 token 集合、做 logits mask。对复杂文法，这个开销可能让 decode 延迟上升 20%。

SGLang 的做法：

- 把常见的约束（简单正则、JSON schema）预编译成 compressed FSM
- 对 FSM 的状态转移做缓存
- 实际开销一般降到 <5%

依然注意：

- 输入给 JSON schema 的规则越严格，搜索空间越小，压缩 FSM 越有效
- 嵌套深的 schema 会让 FSM 爆炸
- 自由文本字段（`"type": "string"`）基本没被约束，体积大

### 5.2 业务实战：工具调用

Agent 场景用约束解码做工具调用是非常自然的：

```python
tool_schema = r"""{
    "type": "object",
    "properties": {
        "tool": {"type": "string", "enum": ["search", "calculator", "weather"]},
        "arguments": {"type": "object"}
    },
    "required": ["tool", "arguments"]
}"""

@sgl.function
def agent_step(s, history, user_msg):
    s += history
    s += "\n用户: " + user_msg + "\n"
    s += "思考: " + sgl.gen("thought", max_tokens=200) + "\n"
    s += "工具调用: " + sgl.gen("tool_call", json_schema=tool_schema, max_tokens=256)
```

生成完 `tool_call` 之后 Python 层解析 JSON 去调真工具，结果拼回 `history`，循环。

## 六、多 LoRA 和多模型服务

### 6.1 多 LoRA

SGLang 支持同一个 base 模型挂多个 LoRA adapter，请求时通过参数指定用哪个：

```bash
python -m sglang.launch_server \
    --model-path /models/Llama-3.1-8B-Instruct \
    --lora-paths lora_a=/loras/finance lora_b=/loras/medical \
    --max-loras-per-batch 4
```

请求：

```json
{
  "model": "default",
  "messages": [...],
  "lora_path": "lora_a"
}
```

多 LoRA 对业务的意义：一个 base 模型服务多个定制方向，显存只多一点（LoRA 增量通常 <1% 参数），成本极低。

### 6.2 Multi-Model 路由

SGLang 本身是**一个进程一个模型**，多模型要起多个 SGLang server。上层用 LiteLLM 或自建网关做路由。

## 七、部署形态和 K8s

### 7.1 单机部署

和 vLLM 类似，单机 8 卡 70B 是舒适区：

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: sglang-llama70b
spec:
  replicas: 2
  template:
    spec:
      containers:
        - name: sglang
          image: lmsysorg/sglang:v0.x.x-cu121
          command: ["python", "-m", "sglang.launch_server"]
          args:
            - --model-path=/models/llama-3.1-70b
            - --tp-size=8
            - --mem-fraction-static=0.88
            - --context-length=8192
            - --max-running-requests=256
            - --host=0.0.0.0
            - --port=30000
          resources:
            limits:
              nvidia.com/gpu: 8
          volumeMounts:
            - { name: models, mountPath: /models }
            - { name: shm, mountPath: /dev/shm }
          readinessProbe:
            httpGet:
              path: /health
              port: 30000
            periodSeconds: 10
          startupProbe:
            httpGet:
              path: /health
              port: 30000
            failureThreshold: 60
            periodSeconds: 10
      volumes:
        - name: models
          persistentVolumeClaim:
            claimName: llm-models-pvc
        - name: shm
          emptyDir:
            medium: Memory
            sizeLimit: 16Gi
```

### 7.2 多机

SGLang 多机用类似 vLLM 的方式（Ray 或自建）。到 0.3+ 版本已经稳定支持多节点。启动示例：

```bash
# Node 0
python -m sglang.launch_server \
    --model-path /models/Llama-3.1-405B \
    --tp-size 16 \
    --nnodes 2 \
    --node-rank 0 \
    --dist-init-addr 10.0.1.10:20000 \
    --host 0.0.0.0 --port 30000

# Node 1
python -m sglang.launch_server \
    --model-path /models/Llama-3.1-405B \
    --tp-size 16 \
    --nnodes 2 \
    --node-rank 1 \
    --dist-init-addr 10.0.1.10:20000 \
    --host 0.0.0.0 --port 30000
```

和 vLLM 多机一样的网络要求：跨机至少 100Gbps RDMA，NCCL 环境变量配好。

## 八、RadixAttention 调优

### 8.1 如何验证 RadixAttention 生效

SGLang 的监控指标（`/metrics` Prometheus endpoint）会暴露缓存命中率：

- `sglang:cache_hit_rate`：token 级命中率
- `sglang:num_cached_tokens`：当前缓存的 token 总数
- `sglang:num_running_requests`
- `sglang:num_queue_requests`

多轮对话场景 `cache_hit_rate` 应该稳定在 60-85%，如果只有 5% 说明 RadixAttention 没发挥——一般是调度策略错了或者 prompt 前缀不稳定。

### 8.2 调度策略 lpm

`--schedule-policy lpm`（longest prefix match）让调度器优先选能命中最长前缀的请求执行。和 FCFS 比，lpm 在多轮对话场景能多榨出 10-20% 吞吐，代价是绝对公平性差一点（短 prompt 没前缀的请求可能排队久一些）。

### 8.3 mem-fraction-static 和 radix cache 的关系

SGLang 的显存分三部分：

- **static**：模型权重、激活、workspace，启动时确定
- **KV cache / radix tree**：剩下的都给缓存
- **其他**：NCCL workspace 等

`--mem-fraction-static` 控制 static 部分占总显存的比例，剩下的自动给 radix cache。调太小 → radix 很大，static 不够 OOM；调太大 → radix 小，缓存命中率低。经验值 0.85~0.88。

## 九、监控和告警

核心指标：

| 指标 | 告警阈值 |
|---|---|
| `sglang:num_queue_requests` | > 50 持续 5 分钟 |
| `sglang:cache_hit_rate` | 多轮场景 < 30% 异常 |
| `sglang:token_usage` | > 95% 预警 |
| P50/P95 首 token 延迟 | > SLA |
| P50/P95 token 间延迟 | > SLA |
| GPU util | < 40% 且有请求 → 调度异常 |
| GPU 显存 | — |

SGLang 的 metrics 设计比 vLLM 更偏工程化，Prometheus 接入非常直接。

## 十、踩坑合集

### 坑 1：RadixAttention 对 prompt 前缀稳定性敏感

如果 system prompt 里有"当前时间: 2026-03-14 15:30:42"这种变动字段，每次请求前缀都不同，RadixAttention 完全失效。解决：把动态字段挪到 user message 开头，system prompt 保持不变。

### 坑 2：约束解码和 streaming

流式输出时约束解码的 FSM 状态要保持一致。SGLang 处理了但高频场景有 CPU 开销。长 JSON schema + 高并发流式时观察 CPU 水位。

### 坑 3：多 LoRA 热切换慢

LoRA 文件第一次加载时要从磁盘读 + 应用到 base，100-500ms 级别。热点 LoRA 常驻显存，冷 LoRA 每次切换都慢。设置 `--max-loras-per-batch` 和 `--max-cpu-loras` 控制驻留策略。

### 坑 4：radix tree 淘汰抖动

当并发突然飙升，radix tree 大量淘汰已缓存路径，短期内缓存命中率掉到接近零，延迟瞬时尖刺。HPA 扩容要有预扩容策略（基于 queue 长度而不是当前 QPS）。

### 坑 5：FlashInfer kernel 对非 LLaMA 系模型支持差

FlashInfer 优先支持 LLaMA 架构。Falcon、Phi、DeepSeek V2 某些 attention 变体需要换 `--attention-backend triton`。

### 坑 6：前端 DSL 和后端版本绑定

SGLang 前端和后端版本要一致，不然会出现 API 不兼容（比如 `sgl.gen` 里某个新参数老后端不认）。生产环境固定版本。

### 坑 7：JSON schema 过于自由导致约束失效

`{"type": "string"}` 允许任意字符串，约束基本等于没加。schema 要具体到 pattern / maxLength，才能真正防止胡乱输出。

### 坑 8：上下文超限的错误码

请求超过 context-length 时 SGLang 直接返回 400，而不是像某些 API 那样截断。客户端要处理这个错误，不要把异常当服务故障上报。

### 坑 9：tokenizer 不一致

SGLang 使用 HF tokenizer 加载模型路径下的 tokenizer，如果你的模型目录混进了其他 tokenizer 文件（比如 `tokenizer.model` 和 `tokenizer.json` 不匹配），生成结果会乱。以干净目录加载。

### 坑 10：CUDA Graph 形状敏感

开了 CUDA Graph（默认开）之后形状变化会触发 recapture。chunk size、max batch 这些参数影响 capture 的形状集合，一次配置好不要频繁改。

## 十一、SGLang vs vLLM vs TRT-LLM

| 维度 | SGLang | vLLM | TRT-LLM |
|---|---|---|---|
| KV 共享机制 | RadixAttention 最强 | Prefix Caching 一般 | KV reuse 较强 |
| 多轮对话 | 最优 | 一般 | 较好 |
| Agent 场景 | 最优 | 一般 | 较好 |
| 结构化生成 | 原生 DSL 支持 | 支持但简单 | 支持但不如 SGLang |
| 吞吐（单请求） | 接近 vLLM | 高 | 最高 |
| 延迟（非共享场景） | 接近 vLLM | 接近 TRT-LLM | 最低 |
| 多 LoRA | 支持 | 支持 | 支持 |
| 上手难度 | 中 | 低 | 高 |
| 前端 DSL | ✓ | ✗ | ✗ |
| 硬件 | NVIDIA 为主 | 多 | 只 NVIDIA |

### 11.1 我的选型决策树

```
你的业务是不是以多轮 / Agent / RAG 固定 prompt 为主？
├─ 是 → SGLang
└─ 否
   ├─ 延迟敏感到极致 + Triton 栈已有 → TRT-LLM
   └─ 否 → vLLM（最省心）
```

很多团队最后会**混合部署**：Agent 服务走 SGLang，开放式 chat 走 vLLM，极限延迟服务走 TRT-LLM。用 LiteLLM 这类网关统一接入，业务层无感。

## 十二、一个完整的 Agent 落地示例

把上面的知识串起来，写一个小 Agent：

```python
import sglang as sgl
import json

sgl.set_default_backend(sgl.RuntimeEndpoint("http://sglang:30000"))

tool_schema = json.dumps({
    "type": "object",
    "properties": {
        "tool": {"type": "string", "enum": ["search", "calc", "done"]},
        "query": {"type": "string"}
    },
    "required": ["tool", "query"]
})

def do_search(q): return f"搜索结果: {q} 的答案..."
def do_calc(expr): return str(eval(expr))

@sgl.function
def agent(s, user_msg, max_steps=5):
    s += "你是一个 Agent，可以调用 search / calc / done 三个工具。\n"
    s += "用户: " + user_msg + "\n"
    for i in range(max_steps):
        s += f"第 {i+1} 步思考: " + sgl.gen(f"th_{i}", max_tokens=150) + "\n"
        s += "工具调用: " + sgl.gen(f"call_{i}", json_schema=tool_schema, max_tokens=200) + "\n"
        call = json.loads(s[f"call_{i}"])
        if call["tool"] == "done":
            s += "最终回答: " + sgl.gen("final", max_tokens=300)
            return
        elif call["tool"] == "search":
            result = do_search(call["query"])
        elif call["tool"] == "calc":
            result = do_calc(call["query"])
        s += "工具返回: " + result + "\n"

state = agent.run(user_msg="（3+5）*2 等于多少，顺便搜一下相关的数学史")
print(state["final"])
```

这段代码的几个关键点：

1. system prompt 前缀在所有请求中完全一致 → RadixAttention 命中
2. 工具 schema 用约束解码保证 JSON 合法 → 不用重试
3. 多步循环在 Python 层展开，每步一次 LLM 调用，每次都能命中前面步骤的 KV
4. 串行 step 中 radix tree 逐步生长，KV 充分复用

实测这种 pattern 下 Agent 的 P95 首 token 在 200-400ms，整条链 5 步跑完 2-4 秒，vLLM 跑同一链需要 8-15 秒。

## 十三、上线 checklist

```
[ ] 选对 attention backend (H100 用 flashinfer)
[ ] --schedule-policy lpm 启用
[ ] --mem-fraction-static 0.85~0.88
[ ] RadixAttention 没有被禁用
[ ] system prompt 前缀稳定无动态内容
[ ] 约束解码的 JSON schema 具体到 pattern
[ ] Prometheus /metrics 接入
[ ] cache_hit_rate 监控和告警
[ ] queue 长度告警（不要只看 QPS）
[ ] HPA 基于 queue_len + gpu_util 双指标
[ ] 前端 DSL 版本和后端锁定
[ ] 模型目录干净，tokenizer 文件一致
[ ] /dev/shm 足够大
[ ] 压测覆盖多轮对话、结构化输出、streaming 三种模式
```

SGLang 是一个被低估的框架。它的核心武器 RadixAttention 不是一个"优化"——它是一种对 LLM 工作负载模式的**重新建模**。如果你的业务确实有大量共享前缀，上 SGLang 的边际收益会比你预期的大很多。
