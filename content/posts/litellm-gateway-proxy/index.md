---
title: "LiteLLM 网关实战：多模型统一接入、限流、成本追踪与故障切换"
date: 2026-04-02T14:00:00+08:00
draft: false
tags: ["LiteLLM", "LLM 网关", "OpenAI API", "成本控制", "多模型"]
categories: ["AI 工程"]
description: "LiteLLM 是 LLM 多模型接入的事实标准。本文讲清它的 Proxy 模式部署、Model Config、Virtual Key、Router Fallback、成本追踪和踩坑实录。"
summary: "LiteLLM 是 LLM 多模型接入的事实标准。本文讲清它的 Proxy 模式部署、Model Config、Virtual Key、Router Fallback、成本追踪和踩坑实录。"
toc: true
math: false
diagram: false
keywords: ["LiteLLM", "LLM Gateway", "OpenAI Compatible", "Fallback", "Rate Limit"]
params:
  reading_time: true
---

## 为什么需要一个 LLM 网关

做 LLM 应用到了一定规模，就会遇到这几个问题：

1. 业务方同时接 OpenAI、Azure、Anthropic、国产云的 Qwen/DeepSeek/文心，每家 API 格式不同，业务代码里写一堆适配
2. 不同业务线有各自的 Key，财务月底对不起账，到底每个业务花了多少
3. 单个 API 挂了没有兜底，业务跟着挂
4. 某个业务突然暴刷把别人的 quota 吃了
5. 安全合规团队问："你们所有 LLM 调用有日志吗"，你没法回答

解决办法不是让每个业务自己搞，而是**中间插一个网关**：业务统一调网关，网关负责路由、鉴权、限流、兜底、计费、审计。

LiteLLM 是目前开源里做这件事最完整的工具。它的定位很清晰：**所有 LLM 都翻译成 OpenAI 兼容接口，然后做网关该做的事**。这篇文章按实战部署一个生产 LiteLLM 网关的流程来写。

## 一、LiteLLM 的两种模式

LiteLLM 有两个形态经常让人混淆：

### 1.1 SDK 模式

```python
from litellm import completion
response = completion(
    model="gpt-4o-mini",
    messages=[{"role": "user", "content": "hello"}],
    api_key="sk-...",
)
```

这是 Python 库形态，一个函数统一调各家 API。业务代码直接 import，不需要部署任何东西。

优点：零依赖，单进程可用
缺点：每个进程独立，没有集中管控，没有审计日志，没有配额

### 1.2 Proxy 模式

启动一个 HTTP 服务（本质上是 FastAPI + LiteLLM SDK），对外暴露 OpenAI 兼容接口：

```bash
litellm --config config.yaml --port 4000
```

业务调这个服务：

```python
import openai
client = openai.OpenAI(
    base_url="http://litellm-gateway:4000",
    api_key="sk-business-key-xxx",
)
client.chat.completions.create(model="gpt-4o-mini", ...)
```

优点：集中管控、配额、审计、Fallback、Virtual Key
缺点：多一跳网络，需要运维一个服务

**生产环境只用 Proxy 模式**。SDK 模式只适合本地脚本。

## 二、核心概念

- **Model**：一个后端模型实例。可以是 OpenAI 的 gpt-4o、Azure 的 deployment、本地的 vLLM 实例
- **Model Group（alias）**：一组同类模型的别名，业务方用别名调，LiteLLM 决定实际走哪个
- **Virtual Key**：业务 Key。每个业务线一个或多个，配额、限流、可访问模型都可以独立设置
- **Team**：组织单位，可以给 Team 分配预算
- **Router**：决定请求落到哪个底层 Model 的路由算法
- **Fallback**：失败时切换到另一个 Model 或 Model Group
- **Budget**：月度/天级预算上限
- **Logger**：请求日志、成本记录的后端（Postgres / Langfuse / custom callback）

## 三、部署

### 3.1 最小可用配置

`config.yaml`：

```yaml
model_list:
  - model_name: gpt-4o-mini
    litellm_params:
      model: openai/gpt-4o-mini
      api_key: os.environ/OPENAI_API_KEY

  - model_name: qwen-max
    litellm_params:
      model: openai/qwen-max       # DashScope 兼容 OpenAI API
      api_key: os.environ/DASHSCOPE_API_KEY
      api_base: https://dashscope.aliyuncs.com/compatible-mode/v1

  - model_name: claude-sonnet
    litellm_params:
      model: anthropic/claude-3-5-sonnet-20241022
      api_key: os.environ/ANTHROPIC_API_KEY

  - model_name: llama-70b-internal
    litellm_params:
      model: openai/default
      api_base: http://vllm-llama70b:8000/v1
      api_key: EMPTY

litellm_settings:
  drop_params: true
  set_verbose: false
  success_callback: ["langfuse"]
  failure_callback: ["langfuse"]
  cache: true
  cache_params:
    type: redis
    host: redis.default.svc
    port: 6379

general_settings:
  master_key: sk-master-xxxxxxxxxxxx
  database_url: postgresql://litellm:pass@postgres:5432/litellm
  store_model_in_db: true
```

启动：

```bash
docker run --rm -p 4000:4000 \
    -v $PWD/config.yaml:/app/config.yaml \
    -e OPENAI_API_KEY=sk-xxx \
    -e DASHSCOPE_API_KEY=sk-xxx \
    -e ANTHROPIC_API_KEY=sk-ant-xxx \
    ghcr.io/berriai/litellm:main-latest \
    --config /app/config.yaml --port 4000
```

### 3.2 几个关键字段

- `model_name`：业务方看到的名字，跨 provider 可以重命名成统一风格
- `litellm_params.model`：真正后端模型，格式是 `<provider>/<model_id>`
- `drop_params: true`：业务方传了后端不支持的参数自动丢弃（比如 Azure 不支持 `logprobs` 就不传）
- `success_callback / failure_callback`：每次成功/失败调用触发回调，内置支持 Langfuse、PostHog、S3 等
- `cache: true`：开启 response cache，对完全相同的请求直接返回历史结果
- `general_settings.master_key`：管理员 Key，用于调用 `/key/generate` 等管理接口
- `general_settings.database_url`：Postgres 连接串，用于存 Virtual Key、Budget、Spend 等

### 3.3 K8s 部署

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: litellm-proxy
  namespace: llm-gateway
spec:
  replicas: 3
  selector:
    matchLabels: { app: litellm }
  template:
    metadata:
      labels: { app: litellm }
    spec:
      containers:
        - name: litellm
          image: ghcr.io/berriai/litellm:main-latest
          args:
            - --config=/app/config.yaml
            - --port=4000
            - --num_workers=4
          ports:
            - containerPort: 4000
          envFrom:
            - secretRef:
                name: litellm-secrets
          volumeMounts:
            - name: config
              mountPath: /app/config.yaml
              subPath: config.yaml
          readinessProbe:
            httpGet: { path: /health/readiness, port: 4000 }
            periodSeconds: 10
          livenessProbe:
            httpGet: { path: /health/liveness, port: 4000 }
            periodSeconds: 30
      volumes:
        - name: config
          configMap:
            name: litellm-config
```

几点注意：

- `num_workers=4` 让 uvicorn 起 4 个 worker，高 QPS 建议 4-8
- readiness 和 liveness 端点不同
- Secret 里放 `OPENAI_API_KEY`、`DASHSCOPE_API_KEY` 等
- 前面挂 Service / Ingress 就行

## 四、Virtual Key 和多租户

### 4.1 生成 Key

启动后通过管理 API 创建 Virtual Key：

```bash
curl http://litellm:4000/key/generate \
  -H "Authorization: Bearer sk-master-xxxxxxxxxxxx" \
  -H "Content-Type: application/json" \
  -d '{
    "key_alias": "team-nlp-prod",
    "models": ["gpt-4o-mini", "claude-sonnet", "llama-70b-internal"],
    "max_budget": 500.0,
    "budget_duration": "30d",
    "tpm_limit": 100000,
    "rpm_limit": 500,
    "metadata": {"team": "nlp", "env": "prod"}
  }'
```

返回一个 `sk-xxxx` 开头的 Key。字段含义：

| 字段 | 含义 |
|---|---|
| `models` | 这个 Key 能用的 model_name 白名单 |
| `max_budget` | 总预算上限（美元） |
| `budget_duration` | 预算周期，`30d` / `1mo` / `1w` 等 |
| `tpm_limit` | tokens per minute |
| `rpm_limit` | requests per minute |
| `metadata` | 自定义标签，用于报表 |

### 4.2 使用 Key

业务方拿到 Key 后直接当 OpenAI Key 用：

```python
from openai import OpenAI
client = OpenAI(
    base_url="http://litellm:4000",
    api_key="sk-xxxx-virtual-key",
)
client.chat.completions.create(model="gpt-4o-mini", messages=[...])
```

网关自动：

- 校验 Key 有效性
- 检查 Key 能不能访问 `gpt-4o-mini`
- 检查 RPM/TPM 限流
- 检查预算是否超
- 路由到真实后端
- 计费 / 审计日志

### 4.3 Team 和层级预算

```bash
curl http://litellm:4000/team/new \
  -H "Authorization: Bearer sk-master-xxx" \
  -d '{
    "team_alias": "nlp-department",
    "max_budget": 5000.0,
    "budget_duration": "30d",
    "models": ["gpt-4o-mini", "gpt-4o", "claude-sonnet"]
  }'
```

然后创建 Key 时指定 team_id，Key 的花费累加到 Team：

```bash
curl http://litellm:4000/key/generate \
  -d '{"team_id": "team-id-xxx", "max_budget": 500}'
```

结构上一个 Team 有多个 Key，Team 和 Key 各自有预算，**取最严**。

## 五、Router 和 Fallback

Router 是 LiteLLM 的核心能力。同一个 `model_name` 可以对应多个底层配置（Azure + OpenAI + 多个 deployment），请求来了按策略选一个。

### 5.1 配置多个后端

```yaml
model_list:
  - model_name: gpt-4o-mini
    litellm_params:
      model: openai/gpt-4o-mini
      api_key: os.environ/OPENAI_API_KEY
    model_info:
      tier: "paid"

  - model_name: gpt-4o-mini
    litellm_params:
      model: azure/gpt-4o-mini
      api_base: https://my-azure.openai.azure.com
      api_key: os.environ/AZURE_API_KEY
      api_version: "2024-06-01"
    model_info:
      tier: "paid"

  - model_name: gpt-4o-mini
    litellm_params:
      model: azure/gpt-4o-mini-backup
      api_base: https://my-azure-eu.openai.azure.com
      api_key: os.environ/AZURE_API_KEY_EU
      api_version: "2024-06-01"

router_settings:
  routing_strategy: simple-shuffle
  num_retries: 2
  timeout: 30
  fallbacks:
    - gpt-4o-mini: ["claude-sonnet", "qwen-max"]
  context_window_fallbacks:
    - gpt-4o-mini: ["claude-sonnet"]
  allowed_fails: 3
  cooldown_time: 60
```

### 5.2 路由策略

`routing_strategy` 支持：

- `simple-shuffle`：随机轮询
- `least-busy`：最少并发（需要 Redis）
- `usage-based-routing-v2`：基于 TPM/RPM 使用率
- `latency-based-routing`：基于历史延迟
- `cost-based-routing`：每次选最便宜的

中小规模 `simple-shuffle` 够用。高并发、多 deployment 的场景 `usage-based-routing-v2` 能更好地分散压力。

### 5.3 Fallback

`fallbacks` 配置中一条 `gpt-4o-mini: ["claude-sonnet", "qwen-max"]` 的意思是：

- 请求目标是 `gpt-4o-mini`
- 所有 `gpt-4o-mini` 的后端都失败后
- 按顺序尝试 `claude-sonnet`、`qwen-max`

`context_window_fallbacks` 是特殊情况：**prompt 超过当前模型上下文时**自动切到大上下文模型。这个在业务长文本场景非常有用，原本应该 400 的请求被自动兜到更大的模型。

`cooldown_time`：某个后端失败超过 `allowed_fails` 次后进入冷却，暂停分配 60 秒。

### 5.4 Retry

`num_retries: 2` 是 LiteLLM 内部的重试次数。重试范围包括 HTTP 5xx、连接失败、timeout。4xx 默认不重试。

## 六、缓存

Response cache 对同 prompt 直接返回历史结果：

```yaml
litellm_settings:
  cache: true
  cache_params:
    type: redis
    host: redis
    port: 6379
    password: os.environ/REDIS_PASSWORD
    ttl: 3600
    mode: default_off    # 默认关，业务方请求里 cache=true 才生效
```

`mode: default_off` 避免无脑命中缓存（LLM 请求通常有 temperature 随机性）。业务请求里显式加参数：

```python
response = client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[...],
    extra_body={"cache": {"no-cache": False}},
)
```

对哪些场景有用：

- temperature=0 的确定性请求
- 系统初始化的 fixed prompt
- 频繁重复的查询

注意：缓存 Key 基于 messages 内容 hash，对 streaming 场景的处理要测过再开。

## 七、成本追踪和报表

### 7.1 存储结构

开了 `database_url` 后 LiteLLM 会在 Postgres 创建几张表：

- `LiteLLM_VerificationToken`：Virtual Keys
- `LiteLLM_TeamTable`：Teams
- `LiteLLM_SpendLogs`：每次调用的消费记录
- `LiteLLM_UserTable`：用户
- `LiteLLM_BudgetTable`：预算定义

`LiteLLM_SpendLogs` 是最重要的表，每条记录包含：

- `api_key`：用哪个 Virtual Key 调的
- `model`：实际调的底层 model
- `prompt_tokens` / `completion_tokens` / `total_tokens`
- `spend`：这次花费（美元）
- `startTime` / `endTime`
- `metadata`：透传的 metadata
- `request_tags`：请求级标签

### 7.2 简单报表查询

```sql
-- 最近 7 天各 Team 花费
SELECT
    team_id,
    SUM(spend) AS total_spend,
    SUM(total_tokens) AS total_tokens,
    COUNT(*) AS request_count
FROM "LiteLLM_SpendLogs"
WHERE startTime > NOW() - INTERVAL '7 days'
GROUP BY team_id
ORDER BY total_spend DESC;

-- 各业务 × 模型 的花费矩阵
SELECT
    metadata->>'business_line' AS business,
    model,
    SUM(spend) AS spend,
    SUM(prompt_tokens) AS in_tokens,
    SUM(completion_tokens) AS out_tokens
FROM "LiteLLM_SpendLogs"
WHERE startTime > NOW() - INTERVAL '30 days'
GROUP BY 1, 2
ORDER BY spend DESC;
```

### 7.3 Grafana 大盘

把 LiteLLM Postgres 接成 Grafana 数据源，做几个面板：

- 实时 QPS（按 model、按 team）
- 实时 TPM（in / out 分开）
- 各 Team 预算使用率
- 错误率（按 model、按 reason）
- 平均延迟 / P95 / P99
- 每个 Key 的花费 Top10

### 7.4 成本告警

写个简单规则：

- 单 Team 日消费 > 阈值 → 钉钉
- 某 Key 的 RPM 突增 > 5x 基线 → 风控告警
- 任一后端 model 错误率 > 10% 持续 5min → 运维告警

## 八、请求头和日志

### 8.1 透传业务 metadata

业务方可以在请求 header 或 body 里带 metadata，LiteLLM 会记录并可用于报表：

```python
response = client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[...],
    extra_headers={
        "x-litellm-metadata": '{"user_id": "u123", "session_id": "s456", "feature": "chat"}',
    },
)
```

或用 `user` 字段（OpenAI 标准）：

```python
client.chat.completions.create(model="gpt-4o-mini", messages=[...], user="u123")
```

### 8.2 集成 Langfuse

Langfuse 是 LLM 可观测性工具，LiteLLM 原生支持：

```yaml
litellm_settings:
  success_callback: ["langfuse"]
  failure_callback: ["langfuse"]
```

环境变量：

```
LANGFUSE_PUBLIC_KEY=pk-...
LANGFUSE_SECRET_KEY=sk-...
LANGFUSE_HOST=https://cloud.langfuse.com
```

每次调用都会往 Langfuse 推一条 trace，可以在 Langfuse UI 里看到完整的 prompt/response、耗时、token 数、成本。

### 8.3 Prometheus

LiteLLM Proxy 暴露 `/metrics`：

- `litellm_requests_total{model, team}`
- `litellm_request_duration_seconds{model}`
- `litellm_tokens_total{type=input|output, model}`
- `litellm_spend_metric{team, model}`
- `litellm_llm_api_failed_requests_total`

接进 Prometheus + Grafana 做实时告警。

## 九、高级场景

### 9.1 Guardrails

LiteLLM 0.14+ 支持 Guardrails，可以在请求前后挂安全过滤：

```yaml
guardrails:
  - guardrail_name: "pii-check"
    litellm_params:
      guardrail: presidio
      mode: pre_call
  - guardrail_name: "prompt-injection"
    litellm_params:
      guardrail: lakera
      api_key: os.environ/LAKERA_API_KEY
      mode: pre_call
```

业务请求中：

```python
client.chat.completions.create(
    ...,
    extra_body={"guardrails": ["pii-check", "prompt-injection"]},
)
```

请求被 guardrail 判定违规时直接拒绝，不走到 LLM。适合金融、医疗等合规要求高的场景。

### 9.2 Prompt Caching

针对支持的 Provider（如 Anthropic），LiteLLM 把 Prompt Caching 能力透传：

```python
messages = [
    {
        "role": "system",
        "content": "长长的系统 prompt ...",
        "cache_control": {"type": "ephemeral"},
    },
    {"role": "user", "content": "hi"},
]
```

LiteLLM 会把 `cache_control` 传给后端。这种 provider 级的 prompt cache 对重复前缀有折扣。

### 9.3 Function Calling 统一

各家 Provider 的 function calling / tool use 格式差异很大，LiteLLM 把它们统一到 OpenAI tools schema：

```python
tools = [{
    "type": "function",
    "function": {
        "name": "get_weather",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}
    }
}]

client.chat.completions.create(
    model="claude-sonnet",
    messages=[...],
    tools=tools,
)
```

即使后端是 Claude，业务代码照旧用 OpenAI 格式。LiteLLM 内部翻译。

## 十、踩坑合集

### 坑 1：drop_params 的陷阱

`drop_params: true` 会静默丢弃不支持的参数。调 bug 时有可能业务方以为参数生效了其实被丢了。高敏感场景关闭 `drop_params`，失败时显式报错。

### 坑 2：Postgres 连接数爆

多副本 LiteLLM + 每副本 4 个 worker + 每个 worker 连 Postgres，连接数很快上百。Postgres 要么放大 max_connections，要么前面挂 PgBouncer。

### 坑 3：cost 计算不对

LiteLLM 的 cost 计算基于内置价格表（`litellm/llms/cost.json`），新模型上线时价格表可能滞后。如果你对财务精度要求高，自己维护一份 cost override：

```yaml
model_list:
  - model_name: gpt-4o-mini
    litellm_params:
      model: openai/gpt-4o-mini
    model_info:
      input_cost_per_token: 0.00000015
      output_cost_per_token: 0.0000006
```

### 坑 4：Redis cache 对 streaming 不友好

开启 response cache 后 streaming 请求的缓存命中行为不直观。建议 streaming 请求默认不缓存。

### 坑 5:失败请求也算计费?

默认情况 LiteLLM 只对成功请求计费。但某些场景（请求到了后端但响应 5xx）也会被记录 spend。定期 check `LiteLLM_SpendLogs` 里的异常记录。

### 坑 6：Fallback 可能超预算

Fallback 链路里后面的模型可能比主模型贵（比如 gpt-4o-mini 兜底到 Claude Opus）。配置时注意优先级，不要兜到比主更贵。

### 坑 7：health check 误伤真实业务

LiteLLM 有 `/health` 端点，会真的去调后端 LLM 测试。高频 health check 会计入真实消费。关闭或限制频率：

```yaml
general_settings:
  disable_spend_logs: true   # health check 相关
  background_health_checks: true
  health_check_interval: 300
```

### 坑 8：key generate 之后无法撤销到底

Key 删除后历史 spend 日志仍然保留，但 Key 本体不可用。如果是安全事件要立刻 `/key/block`。

### 坑 9：master_key 泄漏危险

`master_key` 能做任何管理操作，一旦泄漏攻击者能创建无限预算的 Key。必须放 Secret，轮换流程写进 runbook。

### 坑 10：config.yaml 和 DB 里的 model 不一致

`store_model_in_db: true` 开了之后，config.yaml 里的 model 只是初始配置，后续在 Admin UI 或 API 改的不写回 yaml。GitOps 流要么完全用 yaml 要么完全用 DB，不要混着用。

## 十一、和其他网关对比

| 维度 | LiteLLM | OneAPI / new-api | Portkey | Helicone |
|---|---|---|---|---|
| 开源 | ✓ | ✓ | 部分 | 部分 |
| 多 Provider | 很多 | 多（偏国产） | 多 | 多 |
| Virtual Key | ✓ | ✓ | ✓ | ✓ |
| 预算 | ✓ | ✓ | ✓ | ✓ |
| Fallback | 强 | 一般 | 强 | 一般 |
| Guardrails | ✓ | ✗ | ✓ | ✗ |
| 观测性 | 依赖 Langfuse | 自带简单 | 自带 | 很强 |
| Python SDK 一致性 | 最好 | 一般 | 好 | 好 |
| 部署复杂度 | 中 | 低 | 托管为主 | 托管为主 |

**选型建议**：

- 想要完整开源方案：LiteLLM
- 国产模型为主、中文社区：OneAPI 系
- 可接受 SaaS 方案：Portkey / Helicone
- 有合规要求要自建：LiteLLM

## 十二、一个完整的落地案例

背景：公司有 10 个业务线，接了 OpenAI、Azure、DashScope、自建 vLLM 4 个 provider，需要统一管理。

**架构**：

```
业务 ─→ Ingress ─→ LiteLLM Proxy (3 副本) ─┬─→ OpenAI
                                          ├─→ Azure (us-east, us-west)
                                          ├─→ DashScope
                                          └─→ vLLM (llama70b, qwen72b)

LiteLLM 外接：
  - Postgres (Virtual Keys, Spend)
  - Redis (cache, router state)
  - Langfuse (trace)
  - Prometheus (metrics)
```

**Key 分配**：

- 每个业务线一个 Team，Team 里按环境（dev/prod）分 Key
- dev Key 限 RPM=20, 预算 $50/month
- prod Key 限 RPM=500, 预算按业务谈定
- 管理员（SRE）持有 master key，存 Vault

**Model alias 规划**：

- `fast-small`：gpt-4o-mini / qwen-turbo / llama 8B 轮询
- `fast-medium`：gpt-4o / qwen-max / llama 70B 轮询
- `smart`：claude-sonnet / gpt-4o / deepseek-v3
- `local-only`：只走自建 vLLM，用于内部数据敏感任务

业务方不直接选 `gpt-4o`，而是选 `fast-medium`，给运维留改动空间。

**Fallback**：

- `fast-small` → `fast-medium` → `smart`（容量降级）
- `fast-medium`（context 超限）→ `smart`
- `local-only` 不 fallback 到任何公网模型

**观测**：

- Grafana 大盘：按 Team × Model 的 QPS、Spend、Error rate
- 钉钉告警：
  - 任何 Team 当日 spend > 预算 50%
  - 任何 model 错误率 > 10% 持续 5 分钟
  - master key 被使用（任何调用）

**审计**：

- 所有请求的 prompt 和 response 通过 Langfuse 持久化
- 合规要求的业务线走 guardrails（PII 扫描 + prompt injection 检测）

这套跑了一年下来，效果：

- 新接一个 Provider 从 "改业务代码" 变成 "加 5 行 yaml"
- 财务每月有清晰报表
- 出了故障能在 5 分钟内切到 fallback
- 合规团队满意度上升

## 十三、上线 checklist

```
[ ] master_key 存 Secret / Vault，不在 yaml
[ ] Postgres 和 Redis 独立部署，不要和 LiteLLM 同 Pod
[ ] config.yaml 版本化（git）
[ ] store_model_in_db 策略一致（要么全 yaml 要么全 DB）
[ ] 每个业务线一个 Team
[ ] Virtual Key 有预算和限流
[ ] Fallback 链路不引入更贵的模型
[ ] cost 计算对新上的模型验证过
[ ] Prometheus metrics 接入 Grafana
[ ] 日志/trace 接入 Langfuse 或类似
[ ] Guardrails 针对敏感业务启用
[ ] health check 配置合理不产生真实调用
[ ] drop_params 根据场景调整
[ ] 连接池和 Postgres max_connections 核对
[ ] 出错告警规则完备
[ ] master_key 轮换流程写进 runbook
```

## 十四、收尾

LiteLLM 的价值是把"LLM 治理"这件事标准化了。治理这个词听起来很大但落到实处是些很具体的事：

- 别让一个新模型接入花掉一周
- 别月底对账时不知道钱花在哪
- 别让一个业务暴刷把整个组织的 quota 吃光
- 别让某个 Provider 挂了你的业务跟着挂
- 别让合规团队问起 LLM 调用日志时你哑口无言

这些事不做不会马上出事，但时间长了会变成技术债。上一个 LiteLLM 网关一次性把这些问题按住，是投入产出比非常高的动作。
