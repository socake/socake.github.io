---
title: "TensorRT-LLM 推理加速实战：从 engine 编译到 kernel 调优"
date: 2026-03-07T14:20:00+08:00
draft: false
tags: ["TensorRT-LLM", "TensorRT", "推理加速", "CUDA", "Kernel"]
categories: ["推理部署"]
description: "TensorRT-LLM 是 NVIDIA 端到端推理栈的关键一环，这篇把 engine 编译流程、plugin 机制、量化策略、inflight batching、kernel 调优和生产踩坑都梳理清楚。"
summary: "TensorRT-LLM 是 NVIDIA 端到端推理栈的关键一环，这篇把 engine 编译流程、plugin 机制、量化策略、inflight batching、kernel 调优和生产踩坑都梳理清楚。"
toc: true
math: false
diagram: false
keywords: ["TensorRT-LLM", "TensorRT", "FP8", "Inflight Batching", "CUDA Graph"]
params:
  reading_time: true
---

## 为什么在 vLLM 之外还要学 TensorRT-LLM

做 LLM 推理部署，大多数团队第一反应是 vLLM——开源、社区活跃、上手门槛低。但当你遇到这些场景，就会开始认真看 TensorRT-LLM（下面简称 TRT-LLM）：

- 业务对**首 token 延迟**有硬要求（50ms 以内），vLLM 调到极限仍然差一点
- H100 / H200 上要把 **FP8** 吃透，不只是存储 FP8 而是计算也 FP8
- 要用 **NVIDIA Triton Inference Server** 做统一推理网关
- 自研模型结构，想做**定制 plugin**
- 要在 **Jetson / Orin** 这种边缘设备上跑

TRT-LLM 的本质是 TensorRT 在 LLM 上的垂直栈：编译期做激进的图优化和 kernel 融合，运行期靠 inflight batching 和 paged KV cache 吃吞吐。它不是 vLLM 的替代品，是 NVIDIA 给自家硬件做的"极致性能推理盒"。上手曲线比 vLLM 陡峭不止一个档次，但对延迟敏感的业务确实能压出最后那 20% 的性能。

这篇文章按我实际把 LLaMA 70B 和一个自研 30B 模型迁到 TRT-LLM 的顺序来写：架构理解 → engine 编译 → 运行期配置 → 和 Triton 集成 → 调优和踩坑。

## 一、整体架构

TRT-LLM 可以看作三层：

```
 ┌──────────────────────────────────────┐
 │   应用层（Triton / 自己的 Server）     │
 │   ├─ HTTP / gRPC                      │
 │   └─ tokenizer / scheduler            │
 ├──────────────────────────────────────┤
 │   TRT-LLM Runtime (C++ / Python)      │
 │   ├─ GptManager / Executor            │
 │   ├─ Inflight Batcher                 │
 │   ├─ Paged KV Cache Manager           │
 │   └─ Sampling                         │
 ├──────────────────────────────────────┤
 │   TensorRT Engine (.engine 文件)      │
 │   ├─ 融合好的 CUDA kernels            │
 │   ├─ 选定的 plugin (GPT Attention 等)  │
 │   └─ 权重 (FP16/BF16/FP8/INT4/INT8)   │
 └──────────────────────────────────────┘
```

和 vLLM 最大的区别：**模型要先离线编译成 .engine 文件**，这一步把图结构、算子选择、kernel 选型全部固化，运行期只做前向和调度。好处是极限性能和零抖动；坏处是参数改一个就得重新编译，动态形状的自由度低得多。

### 1.1 编译期关键概念

- **Builder**：把 HuggingFace 权重转成 TRT 可以吃的中间表示
- **Network Definition**：定义模型计算图
- **Plugin**：TRT 原生算子覆盖不了的部分（attention、rmsnorm、rotary embedding 等）通过 plugin 注入，plugin 是 CUDA C++ 写的 kernel
- **Builder Config**：指定精度、workspace、max batch/seq 等
- **Optimization Profile**：定义动态形状的 min/opt/max 三个值，TRT 会为这个区间选最优 kernel

### 1.2 运行期关键概念

- **Executor API**（0.9+ 推荐）：取代老的 GptManager，支持 inflight batching、CUDA graph、disaggregated serving 等
- **Inflight Batching**（又叫 continuous batching）：同一个 batch 里不同请求处于不同阶段（prefill / decode），请求完成立刻出队，新请求立刻加入
- **Paged KV Cache**：和 vLLM 的 PagedAttention 同一套思路，block_size 默认 64

## 二、环境准备

### 2.1 版本对齐

TRT-LLM 对版本的耦合度比 vLLM 更高：

- CUDA：12.1 / 12.2 / 12.3（跟 TRT-LLM 小版本严格对应）
- TensorRT：10.x（0.9+ 版本要求）
- PyTorch：2.2+（只是编译阶段用）
- Python：3.10 / 3.12
- GPU 架构：Ampere（A100）/ Hopper（H100/H200）/ Ada（L40/4090）/ Blackwell（B200）

**原则**：直接用 NVIDIA 官方 NGC 镜像 `nvcr.io/nvidia/tensorrt-llm/release:<tag>`，别自己装。自己装最少会踩 3 个库不兼容的坑。

### 2.2 拉取源码

```bash
git clone https://github.com/NVIDIA/TensorRT-LLM.git
cd TensorRT-LLM
git lfs pull  # 权重 checkpoints 是 lfs
```

源码主要结构：

```
TensorRT-LLM/
├── tensorrt_llm/           # Python 包
│   ├── models/             # 各模型实现（LLaMA / GPT / Mixtral / Falcon ...）
│   ├── quantization/       # 量化算法
│   ├── runtime/            # 运行期
│   └── plugin/             # plugin 绑定
├── examples/               # 每个模型一个目录，带 convert/build/run 脚本
├── cpp/                    # C++ runtime
└── docs/
```

**重要**：90% 的使用场景你只需要跟 `examples/<model>/` 打交道。

## 三、Engine 编译流程

以 LLaMA 70B 为例，编译流程分三步：

1. HF checkpoint → TRT-LLM checkpoint（权重格式转换）
2. TRT-LLM checkpoint → TensorRT engine（实际编译）
3. （可选）量化 calibration

### 3.1 权重转换

```bash
cd examples/llama
python convert_checkpoint.py \
    --model_dir /models/Llama-3.1-70B-Instruct \
    --output_dir /tmp/llama70b_ckpt \
    --dtype float16 \
    --tp_size 8 \
    --pp_size 1
```

几个要注意的参数：

| 参数 | 说明 |
|---|---|
| `--dtype` | 存储精度，`float16` / `bfloat16` / `float8` |
| `--tp_size` | Tensor Parallel 切分度，影响后续 engine 的拓扑 |
| `--pp_size` | Pipeline Parallel |
| `--use_weight_only` | 只量化权重，激活保持 FP16 |
| `--weight_only_precision` | `int8` / `int4` |
| `--load_by_shard` | 大模型分片加载，70B 以上必须开 |

转换后 `/tmp/llama70b_ckpt` 里会有 `config.json` 和 8 份 `rank*.safetensors`，每份对应一个 TP rank。

### 3.2 编译 engine

```bash
trtllm-build \
    --checkpoint_dir /tmp/llama70b_ckpt \
    --output_dir /engines/llama70b_fp16_tp8 \
    --gemm_plugin float16 \
    --gpt_attention_plugin float16 \
    --context_fmha enable \
    --paged_kv_cache enable \
    --remove_input_padding enable \
    --max_batch_size 64 \
    --max_input_len 4096 \
    --max_seq_len 8192 \
    --max_num_tokens 16384 \
    --use_paged_context_fmha enable \
    --use_fused_mlp enable \
    --workers 8
```

这条命令是 TRT-LLM 的核心，每个参数我都踩过至少一次坑：

| 参数 | 作用 | 坑 |
|---|---|---|
| `--gemm_plugin` | 用 TRT-LLM 的 GEMM plugin 替代 TRT 原生 matmul | 不开会慢 30% |
| `--gpt_attention_plugin` | Masked MHA plugin | 必开 |
| `--context_fmha` | Flash Attention for prefill | H100 必开 |
| `--paged_kv_cache` | Paged KV，类似 vLLM block | 默认开 |
| `--remove_input_padding` | batch 内不填 padding | 必开，节省大量计算 |
| `--max_batch_size` | 编译期 batch 上限 | 设得比运行期大一点 |
| `--max_input_len` | prompt 最大长度 | 影响 profile，别设过大 |
| `--max_seq_len` | 总长度上限 | 输入 + 生成 |
| `--max_num_tokens` | 一步最大 token 数 | inflight batching 的关键旋钮 |
| `--use_fused_mlp` | FFN 融合 | 默认开 |
| `--workers` | 并行编译 rank 数 | 等于 tp_size 最快 |

编译期是 CPU+GPU 混合，70B/8 卡在 H100 上大约 15-25 分钟，工作目录会生成 8 个 `rank*.engine` 文件，每个 20~40GB。

### 3.3 max_num_tokens 怎么选

`max_num_tokens` 是 inflight batching 的核心参数。它定义**一步（一次 forward）最多处理的 token 总数**，包括 prefill 的输入 token 和 decode 的 1-token/请求。

- 大了：一步能塞更多请求，吞吐高，但单步延迟变长，首 token 抖动
- 小了：吞吐受限

经验值：

| 场景 | max_num_tokens |
|---|---|
| 纯 decode 为主（chat） | 4096 ~ 8192 |
| 长 prompt（RAG） | 16384 ~ 32768 |
| 混合负载 | 8192 |
| 低延迟（首 token < 100ms） | 2048 ~ 4096 |

编译期设了上限，运行期可以再调小，但不能调大。

## 四、量化策略

TRT-LLM 支持的量化方式比 vLLM 更全，挑的时候要按**精度需求 + 硬件代差**选：

| 方法 | 权重 | 激活 | KV Cache | 硬件 | 适合场景 |
|---|---|---|---|---|---|
| FP16 / BF16 | 16bit | 16bit | 16bit | 任意 | baseline |
| Weight-Only INT8 | 8bit | 16bit | 16bit | Ampere+ | 显存吃紧但精度敏感 |
| Weight-Only INT4 (AWQ) | 4bit | 16bit | 16bit | Ampere+ | 单卡跑 70B |
| SmoothQuant INT8 | 8bit | 8bit | 16bit | Ampere+ | 通用加速 |
| FP8 (per-tensor) | 8bit | 8bit | 8bit | **Hopper+** | 高并发首选 |
| FP8 KV Cache | — | — | 8bit | Hopper+ | 省 KV 显存 |

**选择建议**：

- H100/H200：无脑 FP8
- A100：Weight-Only INT4（AWQ）或 SmoothQuant INT8
- L40S：FP8 可用，实际性能不如 H100 极致
- 精度 > 性能：BF16

### 4.1 FP8 编译示例

FP8 需要一个 calibration 步骤（后训练量化）。TRT-LLM 0.9+ 走 ModelOpt 工具链：

```bash
# Step 1: calibration
python examples/quantization/quantize.py \
    --model_dir /models/Llama-3.1-70B-Instruct \
    --output_dir /tmp/llama70b_fp8 \
    --dtype float16 \
    --qformat fp8 \
    --kv_cache_dtype fp8 \
    --calib_size 512 \
    --tp_size 8

# Step 2: build
trtllm-build \
    --checkpoint_dir /tmp/llama70b_fp8 \
    --output_dir /engines/llama70b_fp8_tp8 \
    --gemm_plugin fp8 \
    --gpt_attention_plugin float16 \
    --use_fp8_context_fmha enable \
    --max_batch_size 64 \
    --max_input_len 4096 \
    --max_seq_len 8192 \
    --workers 8
```

注意：

- `gpt_attention_plugin` 保持 float16，这是 plugin 内部精度，不是激活精度
- `use_fp8_context_fmha` 打开后 prefill 阶段的 Flash Attention 也跑 FP8，H100 效果明显
- `kv_cache_dtype fp8` 直接把 KV cache 压缩到 FP8，显存翻倍利用

### 4.2 AWQ 编译示例

A100 上跑 70B 单卡的方案：

```bash
python examples/quantization/quantize.py \
    --model_dir /models/Llama-3.1-70B-Instruct \
    --output_dir /tmp/llama70b_awq \
    --dtype float16 \
    --qformat int4_awq \
    --awq_block_size 128 \
    --calib_size 512

trtllm-build \
    --checkpoint_dir /tmp/llama70b_awq \
    --output_dir /engines/llama70b_awq \
    --gemm_plugin float16 \
    --gpt_attention_plugin float16 \
    --per_group_size 128 \
    --max_batch_size 32 \
    --max_seq_len 4096
```

70B INT4 权重大约 35GB，单 A100 80G 绰绰有余。

### 4.3 量化后精度验证

量化后一定要跑精度 eval，不能只看 PPL。我的做法是：

1. 用业务真实 prompt 跑 100-200 条，对比量化前后输出的 ROUGE/BLEU
2. 特定任务（代码、数学、推理）跑几个小 benchmark
3. 人工抽查 20 条，看有没有胡言乱语

FP8 一般无痛，INT8 有轻微退化，INT4-AWQ 在长生成场景偶尔露馅。

## 五、运行期：Executor API

0.9 之前 TRT-LLM 有两套 API：低层 `Session` 和高层 `GptManager`。0.9+ 推荐统一用 `Executor`，下面的示例都基于 Executor。

### 5.1 Python 最小示例

```python
from tensorrt_llm.executor import GenerationExecutor, SamplingParams

executor = GenerationExecutor.create(
    engine_dir="/engines/llama70b_fp8_tp8",
    max_beam_width=1,
)

sampling = SamplingParams(
    max_tokens=256,
    temperature=0.7,
    top_p=0.9,
    stop=["</s>"],
)

# 同步
out = executor.generate("你好，介绍一下 TensorRT-LLM", sampling)
print(out.outputs[0].text)

# 流式
for chunk in executor.generate_async(prompt, sampling, streaming=True):
    print(chunk.outputs[0].text_diff, end="", flush=True)
```

### 5.2 C++ Executor

生产环境绝大部分人不会直接写 C++，而是让 Triton Inference Server 的 `tensorrtllm_backend` 去调用 C++ Executor。这个组合是 NVIDIA 官方推荐的生产路径。

## 六、和 Triton Inference Server 集成

Triton 是 NVIDIA 的统一推理服务层，天然支持 TRT-LLM backend。典型部署结构：

```
 ┌──────────────────────────────────────┐
 │        Triton Inference Server       │
 │                                      │
 │  ┌────────────┐   ┌───────────────┐  │
 │  │ ensemble   │──▶│ preprocessor  │  │
 │  └────┬───────┘   │  (tokenize)   │  │
 │       │           └───────┬───────┘  │
 │       │                   │          │
 │       │           ┌───────▼───────┐  │
 │       │           │   tensorrtllm │  │
 │       │           │    (engine)   │  │
 │       │           └───────┬───────┘  │
 │       │                   │          │
 │       │           ┌───────▼───────┐  │
 │       │◀──────────│ postprocessor │  │
 │       │           │  (detokenize) │  │
 │       │           └───────────────┘  │
 └──────────────────────────────────────┘
```

### 6.1 model repository 结构

```
model_repository/
├── ensemble/
│   ├── 1/
│   └── config.pbtxt
├── preprocessing/
│   ├── 1/model.py
│   └── config.pbtxt
├── tensorrt_llm/
│   ├── 1/      # 放 engine 文件
│   └── config.pbtxt
└── postprocessing/
    ├── 1/model.py
    └── config.pbtxt
```

`tensorrt_llm/config.pbtxt` 是关键：

```
name: "tensorrt_llm"
backend: "tensorrtllm"
max_batch_size: 64

model_transaction_policy {
  decoupled: true
}

input [
  { name: "input_ids", data_type: TYPE_INT32, dims: [-1] },
  { name: "input_lengths", data_type: TYPE_INT32, dims: [1], reshape: { shape: [] } },
  { name: "request_output_len", data_type: TYPE_INT32, dims: [1] },
  { name: "temperature", data_type: TYPE_FP32, dims: [1], optional: true },
  { name: "top_p", data_type: TYPE_FP32, dims: [1], optional: true },
  { name: "stop_words_list", data_type: TYPE_INT32, dims: [2, -1], optional: true },
  { name: "bad_words_list", data_type: TYPE_INT32, dims: [2, -1], optional: true }
]

output [
  { name: "output_ids", data_type: TYPE_INT32, dims: [-1, -1] }
]

instance_group [
  {
    count: 1
    kind: KIND_CPU
  }
]

parameters: {
  key: "engine_dir"
  value: { string_value: "/engines/llama70b_fp8_tp8" }
}
parameters: {
  key: "batching_strategy"
  value: { string_value: "inflight_fused_batching" }
}
parameters: {
  key: "kv_cache_free_gpu_mem_fraction"
  value: { string_value: "0.9" }
}
parameters: {
  key: "enable_chunked_context"
  value: { string_value: "true" }
}
parameters: {
  key: "max_tokens_in_paged_kv_cache"
  value: { string_value: "65536" }
}
parameters: {
  key: "enable_kv_cache_reuse"
  value: { string_value: "true" }
}
```

几个关键字段解释：

- `decoupled: true` → 支持 streaming，每个 token 单独返回
- `batching_strategy=inflight_fused_batching` → inflight batching 模式
- `kv_cache_free_gpu_mem_fraction` → 和 vLLM 的 gpu-memory-utilization 类似
- `enable_kv_cache_reuse` → 类似 prefix caching，RAG 场景强烈建议开
- `enable_chunked_context` → chunked prefill，低延迟场景开

### 6.2 启动 Triton

```bash
tritonserver \
    --model-repository=/opt/model_repository \
    --grpc-port=8001 \
    --http-port=8000 \
    --metrics-port=8002 \
    --log-verbose=1
```

Triton 会自动扫描 model_repository 加载 ensemble。

### 6.3 客户端调用

Triton 原生支持 HTTP/gRPC。生产上更常见的是在前面再挂一层 OpenAI 兼容网关（自研或者 LiteLLM）把 Triton 的协议翻译成 `/v1/chat/completions`。

## 七、CUDA Graph 和 kernel 调优

TRT-LLM 的 kernel 几乎都是 NVIDIA 官方手写的 CUDA，调优空间比想象中大。这里列几个我常用的旋钮。

### 7.1 CUDA Graph

CUDA Graph 把一连串 kernel launch 记录下来作为一张图，之后重复执行时不走 launch 路径，对小 batch decode 阶段提升巨大（首 token 后 decode 的 kernel launch 开销可占 20-40%）。

TRT-LLM 中通过 runtime 参数开启：

```
--enable_cuda_graph true
--cuda_graph_cache_size 2048
```

注意：

- CUDA Graph 对**形状敏感**，形状变一次就要重新 capture
- `cache_size` 控制 capture 过的 graph 数量上限
- inflight batching 下形状变化频繁，TRT-LLM 做了分桶处理

开 CUDA Graph 后我的实际观测：H100 上 70B FP8 decode TPS 提升 15-25%。

### 7.2 context_fmha vs masked_mha

- `context_fmha`：prefill 阶段的 Flash Attention，吃 H100 的 wgmma
- `masked_mha`：decode 阶段每次生成 1 个 token 的 attention

两个都是 plugin，默认都开，生产不要手动关。

### 7.3 use_fused_mlp

把 FFN 的两个 GEMM 和中间激活融合成一个 kernel，减少全局内存往返。默认开，但对 SwiGLU（LLaMA 用的）融合效果不如 GeLU，只有 10% 左右。

### 7.4 Medusa / Lookahead / Speculative Decoding

TRT-LLM 0.9+ 支持三种 speculative decoding：

- **Medusa**：额外训练几个"草稿头"，一次前向出多个 candidate token，主模型 verify
- **Lookahead**：无需训练，用 n-gram 猜测
- **Draft Model**：用小模型当 draft，大模型 verify

开启 Medusa 需要编译期加 `--speculative_decoding_mode medusa` 并提供 Medusa head 权重。吞吐提升 1.5x~2.5x，但精度会有极小波动。**生产环境先 A/B 再上**。

### 7.5 Tensor Parallel + Pipeline Parallel

和 vLLM 一样，TRT-LLM 也支持 TP+PP 组合。编译期 `--tp_size` 和 `--pp_size` 指定，跨机部署还需要配合 `mpirun`：

```bash
mpirun -n 16 --hostfile /etc/hosts.trtllm \
    python run.py \
    --engine_dir /engines/llama405b_fp8 \
    --tokenizer_dir /models/llama-3.1-405b
```

**TRT-LLM 跨机不走 Ray，走 MPI**，这一点和 vLLM 不同，运维模型也不一样。好处是更轻量，坏处是你得会配 hostfile 和 MPI 环境变量。

## 八、KV Cache 深入

TRT-LLM 的 paged KV cache 和 vLLM 思路一致但实现细节不同：

- 默认 block_size = 64（vLLM 是 16）
- 支持 **KV Cache Reuse**（类似 prefix caching），开关是 `enable_kv_cache_reuse`
- 支持 **FP8 KV Cache**（编译期 `--kv_cache_type fp8_e4m3`）
- 支持 **offload 到 Host Memory**（0.9+ 实验性）

block_size=64 的代价是小请求浪费多一点，好处是索引开销小一半，decode 阶段 attention kernel 更 cache 友好。除非你的请求都非常短（< 20 token），否则默认 64 合理。

### 8.1 KV Cache 显存估算

公式：

```
kv_bytes_per_token = 2 × num_layers × num_kv_heads × head_dim × dtype_bytes
```

以 LLaMA 3.1 70B 为例：

```
num_layers = 80
num_kv_heads = 8    （GQA，不是 num_attention_heads=64）
head_dim = 128

FP16: 2 × 80 × 8 × 128 × 2 = 327680 byte/token ≈ 320 KB/token
FP8:  2 × 80 × 8 × 128 × 1 = 163840 byte/token ≈ 160 KB/token
```

一张 H100 80GB 留给 KV 大约 30GB，FP16 能装 10 万 token，FP8 能装 20 万。TP=8 平摊后每卡负担除以 8。

### 8.2 KV Cache 调度

Executor 的 KV 调度策略有两种：

- **MAX_UTILIZATION**（默认）：贪心，尽量把 GPU 打满
- **GUARANTEED_NO_EVICT**：保证不淘汰已调度请求

生产环境**一定用 GUARANTEED_NO_EVICT**。MAX_UTILIZATION 下偶尔会把没跑完的请求换出到 CPU 再换回来，延迟抖动无法接受。

## 九、性能调优实战

下面是我调一个 70B FP8 + TP=8 部署时用过的调优顺序。

### Step 1：baseline

用 NVIDIA 官方 `benchmark.py` 或自己写脚本，发固定 prompt 长度（1024 in / 512 out）测吞吐和延迟：

```bash
python benchmarks/python/benchmark.py \
    -m llama_70b \
    --engine_dir /engines/llama70b_fp8_tp8 \
    --batch_size 1,4,16,32,64 \
    --input_output_len "1024,512"
```

记录每个 batch 的 latency、throughput、GPU util。

### Step 2：打开 inflight batching + chunked context

这是两个开关，Triton config 里加：

```
parameters: {
  key: "batching_strategy"
  value: { string_value: "inflight_fused_batching" }
}
parameters: {
  key: "enable_chunked_context"
  value: { string_value: "true" }
}
```

同样负载下 P50 延迟一般能降 20-35%。

### Step 3：开 KV Cache Reuse（RAG 场景）

如果 system prompt 固定，开 `enable_kv_cache_reuse`。首 token 延迟可能直接砍半。

### Step 4：开 CUDA Graph

```
parameters: {
  key: "enable_trt_overlap"
  value: { string_value: "true" }
}
```

decode 吞吐再提 15-25%。

### Step 5：调 max_num_tokens

按业务实际请求长度分布调。看 `triton_server` 指标里的 `nv_inference_request_duration_us` 分位数，抖动大就调小 max_num_tokens。

### Step 6：开 FP8 KV Cache

显存紧张时再开。会有**极微小**的精度损失，跑一遍业务 eval 再决定。

## 十、监控指标

Triton + TRT-LLM 的 Prometheus 指标非常丰富，生产必须盯的：

| 指标 | 含义 | 告警阈值 |
|---|---|---|
| `nv_inference_count` | 总请求数 | — |
| `nv_inference_exec_count` | 实际执行次数 | — |
| `nv_inference_request_duration_us` | 请求全程耗时 | P95 > SLA |
| `nv_inference_queue_duration_us` | 排队时间 | 持续 > 100ms 扩容 |
| `nv_inference_compute_input_duration_us` | 预处理 | 异常升高查 tokenizer |
| `nv_inference_compute_infer_duration_us` | GPU 推理 | 波动大查 KV 调度 |
| `nv_trt_llm_kv_cache_block_usage` | KV 使用率 | > 0.9 告警 |
| `nv_trt_llm_active_request_count` | 正在跑的请求 | — |
| `nv_trt_llm_num_scheduled_requests` | 调度进来的请求 | — |
| `nv_trt_llm_num_paused_requests` | 被换出的请求 | > 0 说明 MAX_UTIL 模式在 evict |
| `nv_gpu_utilization` | GPU SM 利用率 | < 50% 说明 idle |
| `nv_gpu_memory_used_bytes` | 显存占用 | — |

## 十一、踩坑合集

### 坑 1：编译 engine 时报 OOM

症状：trtllm-build 进度跑到一半挂掉，GPU OOM。

原因：编译期本身要在 GPU 上跑 kernel tactic 搜索，会吃一定显存。70B 编译需要至少 60GB 空闲显存。

解法：编译用空闲 GPU，或者加 `--workers 1` 一个 rank 一个 rank 来。

### 坑 2：engine 文件在不同 GPU 代际之间不通用

H100 编译的 engine 不能在 A100 上跑。**每个目标硬件必须单独编译**。

CI/CD 里要按硬件矩阵 × 模型矩阵做 engine 构建流水线，别想着"一次编译到处运行"。

### 坑 3：动态形状编译超慢

`--max_input_len` 和 `--max_seq_len` 差距过大时，TRT 要为很宽的形状区间搜 kernel，编译时间可能翻 3 倍。

解法：按业务实际分布切两个 engine（短上下文 engine + 长上下文 engine），前面网关分流。

### 坑 4：Triton 加载 engine 超时

默认 startup 超时 30 秒，70B engine 加载要 1-3 分钟。改 Triton 启动参数：

```
--model-load-timeout=600
```

K8s readiness 同步调大。

### 坑 5：Tokenizer 不一致

trtllm-build 编译 engine 不带 tokenizer，Triton 侧的 preprocessing/postprocessing 用的是 HF 原始 tokenizer。**engine 和 tokenizer 必须来自同一个 checkpoint**，不然生成的 token id 对不上，模型输出乱码。

### 坑 6：`remove_input_padding` 忘记开

某些教程抄来的命令少了 `--remove_input_padding enable`，会看到吞吐诡异地低。必开。

### 坑 7：FP8 calibration 数据集质量差

calibration 用的 500 条样本如果跟业务分布差太远，量化后模型在业务 prompt 上胡说八道。建议从**业务真实 query 采样**做 calibration。

### 坑 8：GPU 驱动/CUDA 小版本不匹配

NGC 镜像用了 CUDA 12.3 但节点驱动太老支持不到，Triton 启动就挂。ManagedGPU 的 K8s 环境升级驱动要走变更流程，提前规划。

### 坑 9：inflight batching 和 beam search 冲突

beam_width > 1 时不能完全利用 inflight batching。大多数生产场景 beam=1，不用 beam search，直接忽略。

### 坑 10：MPI 跨机 bootstrap 挂了

和 vLLM 的 NCCL 问题类似。MPI 用的是 ssh 免密 + OMPI，免密不通、Pod 里没有 sshd、UCX 没配对都会挂。更推荐**一个 Pod 跑多卡 + 多 Pod 之间跑 NCCL**的方式。

## 十二、TRT-LLM vs vLLM vs SGLang

一张对比表方便选型：

| 维度 | TRT-LLM | vLLM | SGLang |
|---|---|---|---|
| 极致延迟 | 最好 | 次之 | 接近 vLLM |
| 吞吐 | 高 | 高 | 高（RadixAttention 在多轮对话强） |
| FP8 支持 | 最成熟 | 较好 | 较好 |
| INT4/AWQ | 成熟 | 成熟 | 有 |
| 上手难度 | 高 | 低 | 中 |
| 动态性 | 需要预编译 | 完全动态 | 较灵活 |
| 生态集成 | Triton 深度 | OpenAI API 原生 | OpenAI API |
| 社区节奏 | NVIDIA 自己推 | 开源快 | 学术+工业混合 |
| 硬件支持 | 只 NVIDIA | NVIDIA/AMD/TPU | 以 NVIDIA 为主 |
| 多节点 | MPI | Ray | Ray 或自定义 |
| 适合场景 | H100 极致性能、Triton 栈 | 通用、快速迭代 | Agent/RAG 多轮 |

**我的选择**：

- 新业务、快速上线：vLLM
- 延迟敏感、Triton 已有基建：TRT-LLM
- Agent / 多轮对话 / 复杂 prompt：SGLang
- 三者都试一遍的人：不存在

## 十三、上线 checklist

最后给一个上线前的 checklist：

```
[ ] engine 在目标 GPU 代际上编译，不是复用其他集群的
[ ] 量化后跑过业务 eval，输出质量符合要求
[ ] max_num_tokens 按真实请求分布设置，不是默认 8192
[ ] Triton config 开了 inflight_fused_batching
[ ] kv_cache_free_gpu_mem_fraction 给了合理值（0.85~0.92）
[ ] enable_kv_cache_reuse 在 RAG 场景开启
[ ] GUARANTEED_NO_EVICT 调度策略
[ ] enable_trt_overlap / CUDA Graph 开启
[ ] Triton 的 model-load-timeout 足够
[ ] K8s startupProbe 超时和 Triton 对齐
[ ] Prometheus 指标接入 Grafana
[ ] P50/P95/P99 延迟告警规则
[ ] KV 使用率告警
[ ] GPU 温度/功耗告警
[ ] 有压测 baseline 数据
[ ] 回滚方案：老 vLLM 部署还在，流量能切回去
```

TRT-LLM 不是 vLLM 的平替，是极限性能场景的专门武器。上手贵，收益明确。选型时想清楚自己需要的是"快速迭代"还是"压榨硬件最后一滴性能"，两个答案对应两个工具栈。
