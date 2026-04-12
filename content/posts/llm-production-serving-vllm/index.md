---
title: 'LLM 生产服务化：vLLM 部署与 GPU 推理优化实战'
date: 2026-04-12T10:00:00+08:00
draft: false
tags: ["AI", "vLLM", "LLM", "GPU", "推理", "Kubernetes", "MLOps"]
categories: ["AI/机器学习"]
series: ["AI 工程化实践路径"]
description: 'LLM 生产推理部署实战：vLLM Continuous Batching 与 PagedAttention 原理，Kubernetes GPU 部署配置，性能基准测试，以及 vLLM vs TGI vs Ollama 选型指南'
summary: '团队把 Ollama 搬上生产后，高峰期请求排队超过 30 秒，用户纷纷反映 AI 功能不可用。这篇文章记录我们迁移到 vLLM 的全过程，包括 PagedAttention、Continuous Batching 原理，以及 Kubernetes GPU 部署的完整配置。'
toc: true
math: false
diagram: false
keywords: ["vLLM", "PagedAttention", "Continuous Batching", "GPU推理", "Kubernetes GPU", "LLM部署", "推理优化"]
params:
  reading_time: true
---

我们在早期把 Ollama 部署到测试服务器上，效果很好。工程师们兴奋地把它接入了几个内部 AI 功能——文档摘要、代码审查、客服回复建议。然后有一天，用户量上来了，高峰期同时有 20 个请求进来，Ollama 开始串行处理，响应时间从 2 秒飙到 40 秒。

这是很多团队走过的弯路：开发环境用 Ollama 验证可行性，然后直接搬到生产。Ollama 没有问题，只是它的设计目标从来就不是生产高并发。

这篇文章记录我们迁移到 vLLM 的过程，重点讲清楚为什么 vLLM 能做到高并发，以及在 Kubernetes 上的完整部署方案。

## 为什么 Ollama 不适合生产

先说清楚 Ollama 的定位：它是面向开发者本地体验设计的推理工具，核心目标是"一行命令跑起来模型"。这个目标它完成得很好。

但生产环境需要的是：
- **并发请求处理**：10-100 个请求同时到来，要能高效调度
- **可预期的延迟 SLA**：P99 延迟要在接受范围内
- **资源利用率**：GPU 显存不能浪费，吞吐量要最大化
- **可观测性**：Prometheus metrics，知道系统现在处于什么状态

Ollama 的并发模型是简单的请求队列，一次处理一个（或少量几个）。它没有实现 Continuous Batching，KV Cache 管理也比较朴素。在 1-2 个并发请求的场景下感知不到差异，但并发稍高，GPU 大量时间都在等待，吞吐量急剧下降。

**选型结论先放这里：**

| 工具 | 适合场景 | 不适合场景 |
|------|----------|------------|
| Ollama | 本地开发、单人使用、快速验证 | 生产高并发、SLA 要求 |
| TGI (Text Generation Inference) | HuggingFace 生态、需要 HF 模型直接加载 | 需要 OpenAI 兼容 API（需额外配置） |
| vLLM | 生产部署、高并发、OpenAI 兼容 API | 超低显存设备（<16GB） |

## PagedAttention：解决 KV Cache 的内存碎片

要理解 vLLM 为什么快，先要理解它解决的核心问题：**KV Cache 的内存碎片**。

LLM 在推理时，每一层 Transformer 都需要保存当前序列的 Key 和 Value 矩阵，这就是 KV Cache。它的作用是避免重复计算已生成的 token——生成第 100 个 token 时，前 99 个 token 的注意力结果已经算好了，直接用缓存。

**问题在于：传统实现需要预先分配连续的显存空间。**

以 Llama-3-8B 为例，一个序列的 KV Cache 大约是：
- 每层：`2 × seq_len × num_heads × head_dim × dtype_bytes`
- 32 层，4096 序列长度，FP16 精度：约 512MB

如果同时有 10 个请求，需要预分配 5GB 显存给 KV Cache。但问题是，你不知道每个请求最终会生成多长的回复——所以要么按最大长度分配（浪费），要么动态调整（频繁内存拷贝，碎片严重）。

**PagedAttention 的方案：类比操作系统的虚拟内存分页。**

操作系统不会给每个进程分配连续的物理内存，而是把物理内存分成固定大小的页（Page），通过页表映射到进程的虚拟地址空间。进程看到的是连续的虚拟内存，实际物理内存可以是离散的。

PagedAttention 把显存分成固定大小的 **Block**（默认 16 个 token），KV Cache 按 Block 分配，不需要连续。每个序列维护一个 Block Table，记录逻辑块到物理块的映射。

效果非常显著：
- 显存利用率从约 60% 提升到 96%+
- 支持更多并发请求共享 GPU
- 支持 **Prefix Sharing**：多个请求共享相同前缀（如系统提示词）的 KV Cache

## Continuous Batching：让 GPU 永远保持忙碌

理解了显存管理，再看调度策略：Continuous Batching。

**Static Batching（传统方式）**：把一批请求打包，等这批全部完成，再处理下一批。问题是，不同请求的输出长度差异很大——有的回复 10 个 token，有的回复 500 个。短请求完成后，GPU 要等长请求，造成大量空闲。

**Continuous Batching（vLLM 实现）**：也叫 Iteration-level Scheduling。每生成一个新 token（一次 forward pass），调度器就检查：哪些请求已经完成？有没有等待中的请求可以加入？

```
时间步 1: [请求A, 请求B, 请求C] → 各生成第1个token
时间步 2: [请求A, 请求B, 请求C] → 各生成第2个token
时间步 3: 请求A完成(生成了EOS) → 调度器立即把请求D加入批次
         [请求B, 请求C, 请求D] → 继续推理
```

GPU 的利用率大幅提升，因为它几乎不需要等待。根据 vLLM 论文的测试数据，在相同硬件上，Continuous Batching 相比 Static Batching 吞吐量提升 **3-10 倍**，具体取决于请求长度的方差。

## vLLM 部署：完整命令与参数解释

### 基础部署

```bash
pip install vllm

# 部署 Qwen3-72B，4 卡张量并行
vllm serve Qwen/Qwen3-72B-Instruct \
  --tensor-parallel-size 4 \
  --gpu-memory-utilization 0.9 \
  --max-model-len 32768 \
  --served-model-name qwen3-72b \
  --host 0.0.0.0 \
  --port 8000
```

关键参数解释：

- `--tensor-parallel-size 4`：把模型切分到 4 张 GPU 上，每张 GPU 只持有 1/4 的权重。72B 模型 FP16 需要 ~144GB 显存，4 张 A100-80G 刚好装下
- `--gpu-memory-utilization 0.9`：用 90% 的显存给 KV Cache，剩余 10% 给模型权重和其他开销。调高这个值可以支持更多并发
- `--max-model-len 32768`：最大上下文长度。设置得越大，每个请求占用的 KV Cache 越多，并发数越低
- `--served-model-name`：API 中 `model` 字段使用的名字，方便客户端无感知切换

### 使用 OpenAI 兼容 API

vLLM 默认暴露 OpenAI 兼容的 `/v1/chat/completions` 接口，Python SDK 可以直接用：

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://your-vllm-host:8000/v1",
    api_key="not-needed",  # vLLM 默认不校验 key
)

response = client.chat.completions.create(
    model="qwen3-72b",
    messages=[
        {"role": "system", "content": "你是一个专业的代码审查助手"},
        {"role": "user", "content": "请帮我 review 这段 Python 代码：\n```python\ndef add(a, b):\n    return a + b\n```"}
    ],
    temperature=0.3,
    max_tokens=1024,
)

print(response.choices[0].message.content)
```

流式输出：

```python
stream = client.chat.completions.create(
    model="qwen3-72b",
    messages=[{"role": "user", "content": "写一首关于工程师的诗"}],
    stream=True,
)

for chunk in stream:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)
```

## Kubernetes GPU 部署

### 前置条件

集群需要安装 NVIDIA device plugin：

```bash
kubectl apply -f https://raw.githubusercontent.com/NVIDIA/k8s-device-plugin/v0.16.0/deployments/static/nvidia-device-plugin.yml
```

### Deployment YAML

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: vllm-qwen3-72b
  namespace: ai-inference
spec:
  replicas: 1
  selector:
    matchLabels:
      app: vllm-qwen3-72b
  template:
    metadata:
      labels:
        app: vllm-qwen3-72b
    spec:
      # 节点亲和性：只调度到有 GPU 的节点
      nodeSelector:
        nvidia.com/gpu.present: "true"
        node.kubernetes.io/instance-type: "p4d.24xlarge"  # 8x A100-40G
      tolerations:
        - key: "nvidia.com/gpu"
          operator: "Exists"
          effect: "NoSchedule"
      containers:
        - name: vllm
          image: vllm/vllm-openai:v0.7.3
          command:
            - python
            - -m
            - vllm.entrypoints.openai.api_server
          args:
            - --model
            - /models/Qwen3-72B-Instruct
            - --tensor-parallel-size
            - "4"
            - --gpu-memory-utilization
            - "0.9"
            - --max-model-len
            - "32768"
            - --served-model-name
            - qwen3-72b
            - --host
            - "0.0.0.0"
            - --port
            - "8000"
          ports:
            - containerPort: 8000
              name: http
          resources:
            requests:
              cpu: "8"
              memory: "64Gi"
              nvidia.com/gpu: "4"   # 申请 4 张 GPU
            limits:
              cpu: "16"
              memory: "128Gi"
              nvidia.com/gpu: "4"
          volumeMounts:
            - name: model-storage
              mountPath: /models
          env:
            - name: HUGGING_FACE_HUB_TOKEN
              valueFrom:
                secretKeyRef:
                  name: hf-token
                  key: token
          # 启动探针：vLLM 加载 70B 模型需要 5-10 分钟
          startupProbe:
            httpGet:
              path: /health
              port: 8000
            failureThreshold: 60
            periodSeconds: 15
          livenessProbe:
            httpGet:
              path: /health
              port: 8000
            initialDelaySeconds: 30
            periodSeconds: 30
          readinessProbe:
            httpGet:
              path: /health
              port: 8000
            periodSeconds: 10
      volumes:
        - name: model-storage
          persistentVolumeClaim:
            claimName: model-storage-pvc
---
apiVersion: v1
kind: Service
metadata:
  name: vllm-qwen3-72b
  namespace: ai-inference
spec:
  selector:
    app: vllm-qwen3-72b
  ports:
    - port: 80
      targetPort: 8000
  type: ClusterIP
```

### PVC（模型文件存储）

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: model-storage-pvc
  namespace: ai-inference
spec:
  accessModes:
    - ReadWriteMany   # EFS 支持多节点挂载
  storageClassName: efs-sc
  resources:
    requests:
      storage: 200Gi  # Qwen3-72B FP16 约 144GB
```

**注意**：模型文件建议提前下载到 PVC，避免每次 Pod 重启都从 HuggingFace 拉取（网络慢，且国内访问不稳定）。可以用 init container 或单独的数据准备 Job 来完成。

## 性能调优

### Speculative Decoding（投机解码）

对于输出内容比较规律的场景（如代码补全、格式化输出），可以用小模型先"猜"几个 token，大模型一次验证多个，显著降低 TTFT 和提升 TPS：

```bash
vllm serve Qwen/Qwen3-72B-Instruct \
  --speculative-model Qwen/Qwen3-7B-Instruct \
  --num-speculative-tokens 5 \
  --tensor-parallel-size 4
```

实测在代码生成场景，TPS 提升约 40%。

### 量化（降低显存占用）

如果 GPU 显存不够装 FP16，可以用量化版本：

```bash
# AWQ 量化，模型大小减半，精度损失很小
vllm serve Qwen/Qwen3-72B-Instruct-AWQ \
  --quantization awq \
  --tensor-parallel-size 2  # 量化后只需 2 卡
```

AWQ（Activation-aware Weight Quantization）相比 GPTQ，精度保留更好，是目前生产环境最常用的 4-bit 量化方案。

## 性能指标与监控

vLLM 内置 Prometheus metrics，在 `/metrics` 路径暴露：

```python
# 关键指标
vllm:num_requests_running         # 当前正在处理的请求数
vllm:num_requests_waiting         # 等待队列中的请求数
vllm:gpu_cache_usage_perc         # KV Cache 使用率
vllm:time_to_first_token_seconds  # TTFT 分布
vllm:time_per_output_token_seconds # 每个 output token 的时间（=1/TPS）
vllm:e2e_request_latency_seconds  # 端到端延迟
```

Prometheus 采集配置：

```yaml
- job_name: 'vllm'
  static_configs:
    - targets: ['vllm-service:80']
  metrics_path: '/metrics'
```

**典型性能基准**（A100-80G × 4，Qwen3-72B FP16）：

| 指标 | 轻负载（并发 5） | 中负载（并发 20） | 重负载（并发 50） |
|------|-----------------|-----------------|-----------------|
| TTFT (P50) | 0.8s | 1.5s | 4.2s |
| TTFT (P99) | 1.2s | 3.8s | 12s |
| TPS | 450 tok/s | 380 tok/s | 290 tok/s |
| GPU 利用率 | 65% | 88% | 95% |

告警规则建议：

```yaml
- alert: VLLMHighQueueDepth
  expr: vllm:num_requests_waiting > 20
  for: 1m
  annotations:
    summary: "vLLM 请求队列积压，考虑扩容"

- alert: VLLMHighTTFT
  expr: histogram_quantile(0.99, vllm:time_to_first_token_seconds_bucket) > 10
  for: 2m
  annotations:
    summary: "P99 TTFT 超过 10 秒，服务质量下降"
```

## vLLM vs TGI vs Ollama 完整对比

| 维度 | vLLM | TGI | Ollama |
|------|------|-----|--------|
| **并发处理** | Continuous Batching，极强 | Continuous Batching，强 | 有限，串行为主 |
| **显存效率** | PagedAttention，95%+ | 较好，85%+ | 一般 |
| **OpenAI 兼容** | 原生支持 | 需配置，支持 | 原生支持 |
| **模型支持** | 主流开源模型 | HuggingFace 生态 | 主流开源模型 |
| **Speculative Decoding** | 支持 | 支持 | 不支持 |
| **量化支持** | AWQ/GPTQ/FP8 | GPTQ/BitsAndBytes | GGUF |
| **K8s 集成** | 成熟 | 成熟 | 可用但简单 |
| **上手复杂度** | 中 | 中 | 极低 |
| **生产稳定性** | 高 | 高 | 低 |
| **社区活跃度** | 非常高 | 高 | 高 |

**我的建议**：
- 新项目生产部署，首选 vLLM。社区最活跃，功能最全，OpenAI 兼容 API 让迁移成本极低
- 已经大量使用 HuggingFace Inference Pipeline 的项目，TGI 迁移成本更低
- 本地开发和原型验证，Ollama 无出其右

---

从 Ollama 迁移到 vLLM 后，我们的高峰期 P99 延迟从 40 秒降到 4 秒，GPU 利用率从 30% 提升到 88%，同等硬件支持的并发请求数提升了 8 倍。这不是 vLLM 的"黑魔法"，而是 PagedAttention + Continuous Batching 这两个工程决策带来的必然结果。

理解了原理，你才能在调优时知道应该拧哪几个旋钮。
