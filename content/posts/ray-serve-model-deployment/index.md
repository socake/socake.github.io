---
title: "Ray Serve 模型部署实战：Deployment、DAG 编排与弹性伸缩"
date: 2026-03-29T10:45:00+08:00
draft: false
tags: ["Ray", "Ray Serve", "模型部署", "分布式", "Python"]
categories: ["推理部署"]
description: "Ray Serve 是被很多团队忽视的模型服务框架。它在复杂 DAG、异构资源、弹性伸缩上的表现远超单纯的 FastAPI。本文讲清它的核心抽象和生产落地。"
summary: "Ray Serve 是被很多团队忽视的模型服务框架。它在复杂 DAG、异构资源、弹性伸缩上的表现远超单纯的 FastAPI。本文讲清它的核心抽象和生产落地。"
toc: true
math: false
diagram: false
keywords: ["Ray Serve", "Ray", "Model Deployment", "Autoscaling", "DAG"]
params:
  reading_time: true
---

## Ray Serve 定位

很多人把 Ray 和 Ray Serve 混为一谈。说清楚：

- **Ray** 是一个通用的分布式 Python 运行时，让你像写单机 Python 那样写分布式代码
- **Ray Serve** 是建立在 Ray 之上的模型/服务部署库，专门解决"如何把 Python 函数/类部署成一个可伸缩的在线服务"

和 Triton、TorchServe 比，Ray Serve 的定位差别很大：

- Triton 是"模型服务器"，你把训练好的 engine 扔进去，它帮你服务
- Ray Serve 是"Python 代码服务器"，你写一个类，它帮你部署并且支持动态扩缩、多模型 DAG、异构资源

Ray Serve 的核心价值在这几个场景：

1. **复杂流水线**：一个请求要经过 embedding → 向量检索 → rerank → LLM → 后处理，每一步用不同的库、不同的硬件
2. **异构硬件混部**：CPU 做前处理、GPU 做推理、CPU 做后处理，要能在一套代码里协调起来
3. **Python 工程师友好**：不用学 Triton 的 pbtxt，不用学 K8s CRD，写 Python 装饰器就能部署
4. **动态多模型**：一个服务里挂几十个小模型，根据请求参数动态路由

这篇文章按我实际用 Ray Serve 做过的一套多模型推理平台的经验来写：核心概念 → Deployment → DAG 组合 → 弹性伸缩 → 和 K8s 集成 → 踩坑。

## 一、核心抽象

### 1.1 Deployment

Ray Serve 里"部署一个模型"的基本单元叫 **Deployment**。一个 Deployment 就是一个 Python 类，经过 `@serve.deployment` 装饰后被 Ray Serve 管理：

```python
from ray import serve

@serve.deployment(num_replicas=3, ray_actor_options={"num_gpus": 0.5})
class Translator:
    def __init__(self):
        from transformers import pipeline
        self.model = pipeline("translation_en_to_fr", model="t5-small")

    def __call__(self, text: str) -> str:
        return self.model(text)[0]["translation_text"]
```

几个要点：

- `num_replicas=3`：这个 Deployment 起 3 个副本
- `num_gpus=0.5`：每个副本占半张 GPU（Ray 支持小数 GPU）
- `__init__` 里做一次性初始化（加载模型），`__call__` 处理请求
- 每个副本是一个 Ray Actor（有状态的 Ray Worker）

### 1.2 Application

一个或多个 Deployment 组合成一个 **Application**。Application 是部署/回滚的最小单元：

```python
from ray import serve

translator_app = Translator.bind()
serve.run(translator_app, route_prefix="/translate")
```

`bind()` 实例化这个 Deployment，`serve.run` 把 Application 启动起来。之后你可以通过 `http://<ray-head>:8000/translate` 访问。

### 1.3 Ingress

每个 Application 有一个 "ingress" Deployment——就是最外层那个。它可以用 FastAPI 装饰自己，获得完整的 HTTP 功能：

```python
from fastapi import FastAPI
from ray import serve

app = FastAPI()

@serve.deployment
@serve.ingress(app)
class Frontend:
    def __init__(self, translator_handle):
        self.translator = translator_handle

    @app.post("/translate")
    async def translate(self, req: dict):
        text = req["text"]
        result = await self.translator.remote(text)
        return {"translation": result}
```

- `@serve.ingress(app)` 把 FastAPI app 挂到这个 Deployment 上
- 这个 Deployment 持有其他 Deployment 的 handle（通过构造函数注入）
- FastAPI 的路由、依赖注入、Pydantic 校验全部可用
- 内部调用其他 Deployment 用 `handle.remote()`，返回一个 ObjectRef，await 得到结果

## 二、DAG 组合

Ray Serve 的组合能力是它最让人舒服的地方。看一个 RAG 流水线的例子：

```python
from ray import serve

@serve.deployment(ray_actor_options={"num_cpus": 2})
class QueryRewriter:
    def __init__(self):
        from sentence_transformers import SentenceTransformer
        self.embed_model = SentenceTransformer("BAAI/bge-small-zh")

    async def __call__(self, query: str) -> dict:
        rewritten = self._rewrite(query)
        embedding = self.embed_model.encode(rewritten).tolist()
        return {"query": rewritten, "embedding": embedding}

    def _rewrite(self, q): return q.strip()

@serve.deployment(ray_actor_options={"num_cpus": 1})
class VectorSearch:
    def __init__(self):
        import pymilvus
        self.client = pymilvus.MilvusClient(uri="http://milvus:19530")

    async def __call__(self, embedding: list, top_k: int = 10) -> list:
        return self.client.search("docs", data=[embedding], limit=top_k)

@serve.deployment(ray_actor_options={"num_gpus": 0.25})
class Reranker:
    def __init__(self):
        from sentence_transformers import CrossEncoder
        self.rerank_model = CrossEncoder("BAAI/bge-reranker-large")

    async def __call__(self, query: str, docs: list) -> list:
        pairs = [[query, d["text"]] for d in docs]
        scores = self.rerank_model.predict(pairs)
        ranked = sorted(zip(docs, scores), key=lambda x: -x[1])
        return [d for d, _ in ranked[:5]]

@serve.deployment(ray_actor_options={"num_gpus": 1})
class LLMGenerator:
    def __init__(self):
        import openai
        self.client = openai.OpenAI(base_url="http://vllm:8000/v1", api_key="EMPTY")

    async def __call__(self, query: str, docs: list) -> str:
        context = "\n".join(d["text"] for d in docs)
        resp = self.client.chat.completions.create(
            model="default",
            messages=[
                {"role": "system", "content": f"根据以下资料回答：\n{context}"},
                {"role": "user", "content": query},
            ],
        )
        return resp.choices[0].message.content

from fastapi import FastAPI
app = FastAPI()

@serve.deployment
@serve.ingress(app)
class RAGService:
    def __init__(self, rewriter, searcher, reranker, generator):
        self.rewriter = rewriter
        self.searcher = searcher
        self.reranker = reranker
        self.generator = generator

    @app.post("/rag")
    async def rag(self, req: dict):
        q = req["query"]
        rw = await self.rewriter.remote(q)
        hits = await self.searcher.remote(rw["embedding"], top_k=20)
        top = await self.reranker.remote(rw["query"], hits)
        answer = await self.generator.remote(rw["query"], top)
        return {"answer": answer, "sources": [d["id"] for d in top]}

# 组装
rewriter = QueryRewriter.bind()
searcher = VectorSearch.bind()
reranker = Reranker.bind()
generator = LLMGenerator.bind()

rag_app = RAGService.bind(rewriter, searcher, reranker, generator)
serve.run(rag_app, route_prefix="/")
```

这段代码值得细看：

- 每个步骤是一个独立 Deployment，有自己的资源申请
- `QueryRewriter` 吃 CPU，`Reranker` 吃 0.25 张 GPU，`LLMGenerator` 吃 1 张 GPU
- 每个 Deployment 独立扩缩容
- 组装通过 `.bind()` + 构造函数注入完成，编译期就确定依赖关系

Ray Serve 的调度器会把不同 Deployment 放到不同 Worker 上，自动利用集群里的异构资源。你不用关心 CPU Pod 和 GPU Pod 怎么通信，Ray 的 ObjectRef 机制自动处理。

## 三、异步与并发

### 3.1 async 还是 sync

Ray Serve 的 `__call__` 可以是同步也可以是异步。**异步是默认推荐**：

- `async def`：同一个副本可以并发处理多个请求（上限由 `max_ongoing_requests` 控制）
- `def`：同步，一个副本一次只处理一个请求

对于 I/O 密集（调外部 API、读数据库）或者 batch 推理（用 async 写 batching 逻辑），async 版本吞吐高几倍。

### 3.2 max_ongoing_requests

```python
@serve.deployment(
    num_replicas=4,
    max_ongoing_requests=16,
)
class MyDeployment:
    ...
```

`max_ongoing_requests`（旧版叫 `max_concurrent_queries`）是每个副本同时处理请求的上限。超过后 Ray Serve 开始反压。这个参数要配合：

- 模型的 GPU 吞吐：一张 H100 跑 LLM decode 能同时处理 32 个请求，就设 32
- 内存：每个请求的中间 tensor 占多少，算好总显存

### 3.3 批处理装饰器

Ray Serve 提供了一个装饰器把多个独立请求合并成一个 batch：

```python
from ray import serve

@serve.deployment
class BatchModel:
    def __init__(self):
        import torch
        self.model = torch.load("/models/classifier.pt")

    @serve.batch(max_batch_size=32, batch_wait_timeout_s=0.01)
    async def __call__(self, inputs: list) -> list:
        import torch
        tensor = torch.stack([self._preprocess(x) for x in inputs])
        with torch.no_grad():
            out = self.model(tensor)
        return out.tolist()

    def _preprocess(self, x): ...
```

`@serve.batch` 让外部看起来是单请求接口，内部 Ray Serve 自动收集最多 32 个请求或等待 10ms 凑够就组 batch 推理。和 Triton 的 dynamic batching 一个思路。

## 四、弹性伸缩

Ray Serve 的 autoscaling 是它区别于纯 Python 服务的核心能力。

### 4.1 配置方式

```python
@serve.deployment(
    autoscaling_config={
        "min_replicas": 1,
        "initial_replicas": 2,
        "max_replicas": 20,
        "target_ongoing_requests": 5,
        "upscale_delay_s": 30,
        "downscale_delay_s": 600,
        "smoothing_factor": 1.0,
    },
)
class AutoscaledModel:
    ...
```

字段说明：

- `min/max_replicas`：副本数范围
- `initial_replicas`：启动时副本数
- `target_ongoing_requests`：每个副本**期望**并发处理的请求数，实际平均值偏离这个值时触发扩缩
- `upscale_delay_s`：扩容判断窗口，短一点响应快但容易抖动
- `downscale_delay_s`：缩容窗口，大一点避免频繁缩容
- `smoothing_factor`：平滑系数，越小越平滑

Ray Serve 自己算出"当前该有多少副本"，然后申请/释放 Actor。

### 4.2 和 K8s HPA 的区别

| 维度 | Ray Serve autoscaling | K8s HPA |
|---|---|---|
| 指标 | `ongoing_requests`（业务级） | CPU/内存/自定义指标 |
| 最小粒度 | Ray Actor（轻量） | Pod（重） |
| 扩容延迟 | 秒级 | 分钟级（Pod 冷启动） |
| 缩容触发 | 实时 | HPA 周期 |
| 资源申请 | Ray 内部调度 | K8s scheduler |

Ray Serve 的扩缩容粒度是 Ray Actor，比 Pod 级别的 HPA 快很多。但代价是 Ray 集群本身需要有足够的资源——如果 Ray 集群是固定大小，Ray Serve 只是在内部调度，扩容天花板被限制。

### 4.3 KubeRay + HPA 联动

生产常见的做法是 KubeRay Operator 管理 Ray 集群，Ray 集群本身用 K8s HPA（基于 CPU/GPU 利用率）扩缩，Ray Serve 在 Ray 集群内部做更细粒度的 Actor 扩缩。两层配合：

```
请求量 上升 → Ray Serve 扩 Actor → 占满 Ray 集群
              → 触发 K8s HPA → 扩 Ray Worker Pod
              → Ray 集群容量增加 → Ray Serve 继续扩 Actor
```

这种两层架构响应快、弹性大，但复杂度也高。需要仔细调两层阈值避免抖动。

## 五、部署到 K8s：KubeRay

Ray Serve 本身是进程级的，到了 K8s 里就要用 **KubeRay Operator** 管理。

### 5.1 安装 KubeRay

```bash
helm repo add kuberay https://ray-project.github.io/kuberay-helm/
helm install kuberay-operator kuberay/kuberay-operator -n kuberay-system --create-namespace
```

### 5.2 RayService CRD

KubeRay 提供一个 `RayService` CRD 专门描述"Ray 集群 + Ray Serve App"的组合：

```yaml
apiVersion: ray.io/v1
kind: RayService
metadata:
  name: rag-service
spec:
  serviceUnhealthySecondThreshold: 300
  deploymentUnhealthySecondThreshold: 300
  serveConfigV2: |
    applications:
      - name: rag
        import_path: rag_service.rag_app
        route_prefix: /
        runtime_env:
          pip:
            - "sentence-transformers==2.6.1"
            - "pymilvus==2.4.1"
            - "openai==1.30.0"
        deployments:
          - name: RAGService
            num_replicas: 2
          - name: QueryRewriter
            num_replicas: 3
            ray_actor_options:
              num_cpus: 2
          - name: Reranker
            num_replicas: 2
            ray_actor_options:
              num_gpus: 0.25
          - name: LLMGenerator
            num_replicas: 4
            ray_actor_options:
              num_gpus: 1
  rayClusterConfig:
    rayVersion: "2.x.x"
    headGroupSpec:
      rayStartParams:
        dashboard-host: "0.0.0.0"
      template:
        spec:
          containers:
            - name: ray-head
              image: rayproject/ray:2.x.x-py310
              resources:
                limits:
                  cpu: "4"
                  memory: "16Gi"
    workerGroupSpecs:
      - groupName: cpu-workers
        replicas: 4
        minReplicas: 2
        maxReplicas: 10
        rayStartParams: {}
        template:
          spec:
            containers:
              - name: ray-worker
                image: rayproject/ray:2.x.x-py310
                resources:
                  limits:
                    cpu: "16"
                    memory: "64Gi"
      - groupName: gpu-workers
        replicas: 2
        minReplicas: 1
        maxReplicas: 8
        rayStartParams: {}
        template:
          spec:
            containers:
              - name: ray-worker
                image: rayproject/ray:2.x.x-py310-gpu
                resources:
                  limits:
                    cpu: "16"
                    memory: "128Gi"
                    nvidia.com/gpu: 1
```

几个关键点：

- `serveConfigV2`：直接内嵌 Ray Serve 的部署配置，支持热更新（修改后 Operator 自动 reconfig）
- `runtime_env.pip`：运行时 pip 依赖，不用重打镜像就能变
- 多个 `workerGroupSpecs`：CPU worker 组和 GPU worker 组，分别扩缩
- `import_path`：你的 Python 代码里 `rag_app` 这个变量，KubeRay 会动态 import

### 5.3 代码怎么上传到集群

两种方式：

1. **打镜像**：把代码打进镜像，`import_path` 指向镜像里的路径
2. **runtime_env 拉 zip**：代码存 S3/GCS，runtime_env 里加 `"working_dir": "s3://bucket/code.zip"`

第一种更可控，第二种更灵活。我的做法是**基础镜像打稳定依赖，业务代码通过 runtime_env 拉取**，这样更新代码不用重建镜像。

## 六、观测与调试

### 6.1 Ray Dashboard

Ray 自带 dashboard（默认 :8265），可以看：

- 集群节点、Actor、资源占用
- Ray Serve 的每个 Deployment 副本数、状态
- 每个 Actor 的日志、堆栈

生产要把 dashboard 的端口限制在内网 + 加认证，默认无认证。

### 6.2 Metrics

Ray Serve 暴露 Prometheus 指标：

- `ray_serve_deployment_request_counter_total`：请求数
- `ray_serve_deployment_processing_latency_ms`：处理延迟
- `ray_serve_deployment_queued_queries`：排队请求数
- `ray_serve_num_ongoing_requests`：正在处理
- `ray_serve_deployment_replica_starts_total`：副本启动次数（扩容频繁则升高）

Grafana 官方有现成的 Ray dashboard JSON。

### 6.3 日志

Ray Actor 的日志通过 Ray 中心化收集，默认写到 `/tmp/ray/session_*/logs/`。生产建议把 Ray 的 log 目录挂到 sidecar，让 Fluent Bit 推到 Loki/ES。

## 七、多模型路由

一个常见的需求：**一个服务里挂多个版本/多个 LoRA，按请求参数路由**。

### 7.1 Deployment handle 路由

```python
@serve.deployment
@serve.ingress(app)
class ModelRouter:
    def __init__(self, model_a, model_b, model_c):
        self.models = {
            "legal": model_a,
            "medical": model_b,
            "finance": model_c,
        }

    @app.post("/predict")
    async def predict(self, req: dict):
        domain = req.get("domain", "legal")
        model = self.models.get(domain)
        if model is None:
            return {"error": "unknown domain"}
        result = await model.remote(req["text"])
        return {"result": result}
```

简单直接。每个模型是独立 Deployment，独立扩缩。

### 7.2 Multiplexing（0.6+ 推荐）

Ray Serve 0.6+ 提供了 `multiplexed` 装饰器，支持"一个 Deployment 副本动态加载多个模型":

```python
@serve.deployment
class MultiLoRAModel:
    def __init__(self):
        self.base_model = load_base()

    @serve.multiplexed(max_num_models_per_replica=4)
    async def get_model(self, lora_id: str):
        return load_lora(self.base_model, lora_id)

    async def __call__(self, request):
        lora_id = request.headers.get("X-LoRA-Id")
        lora_model = await self.get_model(lora_id)
        return lora_model(request.json())
```

- `@serve.multiplexed` 标记加载模型的方法，每个副本最多缓存 4 个
- 请求根据 `lora_id` 被 Ray Serve 路由到持有对应模型的副本（cache affinity）
- 冷 LoRA 自动被 LRU 淘汰

这个模式对"一个 base + 大量 LoRA" 的场景极友好，比每个 LoRA 起一个 Deployment 省资源得多。

## 八、和其他框架的集成

### 8.1 vLLM / SGLang / TRT-LLM

Ray Serve 不和这些推理引擎冲突，而是和它们**互补**。典型架构：

- Ray Serve 作为 DAG 编排层，前处理、后处理、路由、多模型
- 实际的 LLM 推理在独立的 vLLM / SGLang 服务里
- Ray Serve 的 Deployment 通过 HTTP 调 vLLM

好处是你不用把 vLLM 塞进 Ray Actor 里（Ray 里跑 vLLM 可以但多了一层复杂度），vLLM 保持独立部署独立扩缩。

也有团队选择**把 vLLM 跑在 Ray Actor 里**：

```python
@serve.deployment(ray_actor_options={"num_gpus": 8})
class VLLMInference:
    def __init__(self):
        from vllm import AsyncLLMEngine, AsyncEngineArgs
        args = AsyncEngineArgs(
            model="/models/llama-3.1-70b",
            tensor_parallel_size=8,
            gpu_memory_utilization=0.9,
        )
        self.engine = AsyncLLMEngine.from_engine_args(args)

    async def __call__(self, prompt: str, **kwargs):
        from vllm import SamplingParams
        params = SamplingParams(**kwargs)
        async for out in self.engine.generate(prompt, params, request_id="..."):
            pass
        return out.outputs[0].text
```

这样 vLLM 的生命周期完全由 Ray 管理，扩缩和 Ray Serve 联动。代价是 Deployment 的初始化很慢（加载 70B 几分钟），需要仔细调 startup timeout。

### 8.2 PyTorch / HuggingFace

直接在 Deployment 里用，不需要特殊处理：

```python
@serve.deployment(ray_actor_options={"num_gpus": 1})
class SentimentAnalyzer:
    def __init__(self):
        from transformers import pipeline
        self.pipe = pipeline("sentiment-analysis", device=0)

    def __call__(self, text: str):
        return self.pipe(text)[0]
```

## 九、发布与回滚

### 9.1 原地更新

修改 `serveConfigV2` 后，KubeRay Operator 检测到变化，下发到 Ray Serve，Serve 做原地 reconfig：

- 仅参数变化（num_replicas、autoscaling）：Ray Serve 直接应用
- 代码变化（import_path / runtime_env）：Ray Serve 启动新副本，等新副本就绪后切流量，老副本 drain 下线

整个过程对调用方无感（如果设置了合理的 grace period）。

### 9.2 蓝绿部署

更保险的做法是**部署第二个 RayService**（rag-service-v2），切流量通过 Ingress 层控制。这样回滚直接切回老版本，中间完全隔离。

### 9.3 健康检查

Ray Serve 有两级健康检查：

- **Deployment 级**：每个副本启动后通过 `__init__` 成功视为就绪，失败重试
- **Application 级**：所有 Deployment 都就绪才返回 200 给 `/-/healthz`

K8s readiness probe 挂到 `/-/healthz`。

## 十、踩坑合集

### 坑 1：runtime_env 拉依赖慢

`runtime_env.pip` 每个新副本启动时都要 pip install，冷启动慢。生产建议把稳定依赖打进镜像，只有代码和极少数依赖走 runtime_env。

### 坑 2：Actor 数 vs 副本数

容易混淆：Deployment 的 `num_replicas` 指 Actor 数量，不是 Pod 数量。10 个 Actor 可能全挤在 3 个 Pod 里，也可能散在 10 个 Pod 里，取决于资源 packing。

### 坑 3：Handle 调用链路变长

多级 Deployment 嵌套时每次 `handle.remote()` 都是一次 Ray RPC，有微秒级开销。链路太深（5 层以上）会累积。实测层次加深一级 P50 延迟增加约 0.5-1ms（跟 Ray 版本有关）。

### 坑 4：`@serve.batch` 的坑

- 批处理窗口要和副本数、并发数协调好
- batch 里一个请求异常，整个 batch 都会被影响
- 异步和 batch 混用时要小心 deadlock

### 坑 5：内存泄漏追不到

Actor 长期运行后内存缓慢增长是常见问题。Ray Serve 提供 `max_concurrent_queries` 和重启机制——副本跑够一定时间/请求数后主动重启。

```python
@serve.deployment(
    num_replicas=4,
    graceful_shutdown_timeout_s=60,
    health_check_period_s=30,
)
class LeakyModel:
    ...
```

目前没有内置的"每 N 个请求重启"，需要自己在代码里计数手动触发 `serve.get_replica_context().exit()`。

### 坑 6：Ray 版本升级破坏性

Ray 主版本升级常有 API 变化。升级前仔细读 release note，测试集群先跑一周。

### 坑 7：网络分区 Ray head 挂

Ray head 是单点。head 挂了整个集群瘫。KubeRay 0.5+ 支持 GCS HA（Ray GCS 持久化到 Redis），生产必开：

```yaml
headGroupSpec:
  rayStartParams:
    gcs-server-port: "6379"
  template:
    spec:
      containers:
        - name: ray-head
          env:
            - name: RAY_REDIS_ADDRESS
              value: "redis://redis:6379"
```

### 坑 8：dashboard 默认无认证

Ray dashboard 默认 :8265 没有认证，暴露到公网是事故。生产 K8s NetworkPolicy 限死、前面挂 OAuth2-Proxy。

### 坑 9：GPU 小数分配的碎片化

`num_gpus=0.25` 允许 4 个 Actor 共享一张 GPU。但 Ray 的资源分配只是**记账**，不做实际隔离。4 个 Actor 真的同时吃显存时照样 OOM。小数 GPU 只适合"一张 GPU 多模型但请求不会同时来"的场景。

### 坑 10：Serve Application 更新后老副本不退

偶尔碰到新副本启动成功了但老副本没被回收，集群资源泄漏。定位路径：`serve status`、`kubectl logs` 看 Serve Controller 日志，通常是 graceful shutdown 超时，副本卡在某个请求上。

## 十一、选型对比

| 维度 | Ray Serve | Triton | TorchServe | BentoML | FastAPI |
|---|---|---|---|---|---|
| Python 友好 | 最友好 | 一般 | 友好 | 最友好 | 最友好 |
| 多模型 DAG | ✓ | ✓ (ensemble) | 弱 | ✓ | 自己写 |
| 异构硬件 | ✓ | ✗（单 Triton 只管一个 GPU） | 弱 | 弱 | 弱 |
| 弹性伸缩 | 强 | 依赖 K8s | K8s | K8s | K8s |
| LLM 专用优化 | 弱（自己写） | 强（tensorrtllm backend） | 弱 | 弱 | 无 |
| 学习曲线 | 中 | 较陡 | 低 | 低 | 低 |
| 运维复杂度 | 中-高 | 中 | 低 | 低 | 低 |

**选型建议**：

- 纯 LLM 单模型服务：vLLM / SGLang / Triton
- 复杂 DAG、异构资源、多模型：**Ray Serve**
- Python 快速原型、小规模：FastAPI
- 业务偏工程化、CI/CD 完整：BentoML

很多团队的最佳组合是 **Ray Serve + vLLM**：Ray Serve 做编排和前后处理，vLLM 做实际的 LLM 推理。两者各自发挥长处。

## 十二、上线 checklist

```
[ ] 基础镜像打了稳定依赖，runtime_env 只带业务代码和少量新包
[ ] 每个 Deployment 的 resource request 算过，不要漏掉 CPU
[ ] max_ongoing_requests 调过，不是默认 100
[ ] autoscaling min/max 设置，避免冷启动和资源泄漏
[ ] KubeRay Operator 运行中
[ ] RayService 的 healthy threshold 合理
[ ] Ray Dashboard 有认证或只在内网
[ ] GCS HA 启用（生产必做）
[ ] Prometheus 指标接入 Grafana
[ ] 日志聚合到中心日志系统
[ ] 蓝绿发布方案验证过
[ ] 熔断/降级策略：下游 vLLM 挂了 Ray Serve 如何响应
[ ] GPU Pod 和 CPU Pod 的 worker group 分开
```

## 十三、收尾

Ray Serve 的学习曲线中等，回报在**复杂场景**下非常明显。它不会让一个简单的 `resnet.predict(image)` 跑得更快（那是 Triton 的领地），但会让你有 7 步流水线、3 种模型、2 种硬件的业务不用再自己缝合各种胶水。

我的使用原则：

- 单模型简单服务：不要用 Ray Serve，FastAPI 或 Triton 更简单
- 流水线 ≥ 3 步、有异构硬件：Ray Serve 值得投入
- LLM 推理：不要把 vLLM 塞进 Ray Actor，让 Ray Serve 做上层编排即可

用对了场景，Ray Serve 能把"一堆 Python 脚本串起来变成一个在线服务"这件事的开发成本降一个数量级。
