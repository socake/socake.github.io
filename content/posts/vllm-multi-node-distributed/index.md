---
title: "vLLM 多机多卡分布式推理：Tensor Parallel 调优与踩坑实录"
date: 2026-03-03T09:30:00+08:00
draft: false
tags: ["vLLM", "LLM", "推理部署", "分布式", "Tensor Parallel"]
categories: ["推理部署"]
description: "从单机 8 卡讲到多机多卡，把 vLLM 的 TP/PP 拆分、Ray 启动方式、NCCL 调优、PagedAttention 显存核算和常见翻车场景串成一条完整的落地路径。"
summary: "从单机 8 卡讲到多机多卡，把 vLLM 的 TP/PP 拆分、Ray 启动方式、NCCL 调优、PagedAttention 显存核算和常见翻车场景串成一条完整的落地路径。"
toc: true
math: false
diagram: false
keywords: ["vLLM", "Tensor Parallel", "Pipeline Parallel", "NCCL", "PagedAttention"]
params:
  reading_time: true
---

## 写在最前面

单机 8 卡 H100 是舒适区，70B 模型 FP16 也塞得下。真正让人头疼的是两种场景：

一是 405B 这种量级，单机塞不下，必须跨机器；二是 70B 要做高并发低延迟，单机 TP=8 吞吐已经到瓶颈，想继续堆机器把 QPS 再抬一档。这两种场景对 vLLM 的挑战完全不同，前者是**正确性问题**——怎么让一个模型正确地分片到多机；后者是**性能问题**——怎么让多机的通信开销不吃掉并行收益。

这篇文章把我在生产环境里踩过的坑按层次梳理一遍。不谈原理八股，也不贴一堆我没跑过的 benchmark，只写我实际调过的参数、用过的拓扑、以及翻车后怎么定位。

## 一、分布式推理的维度

LLM 推理里常见的并行维度有四个：TP（Tensor Parallel）、PP（Pipeline Parallel）、DP（Data Parallel）、EP（Expert Parallel，MoE 专用）。vLLM 0.5 之前主推 TP，0.6+ 版本把 PP 补齐，MoE 的 EP 也陆续进来。实战里绝大多数场景用 TP+PP 组合就够了。

### 1.1 Tensor Parallel 在做什么

TP 是**模型单层内部**的切分。一层 Transformer Block 里最贵的是两个矩阵乘：

- QKV Projection：`[hidden] @ [hidden, 3*hidden]`
- FFN：`[hidden] @ [hidden, 4*hidden]` 再 `[4*hidden] @ [4*hidden, hidden]`

Megatron-LM 论文里提出的经典切分方式是：第一个矩阵乘按**列切**（每个 rank 持有一部分输出列），第二个矩阵乘按**行切**（每个 rank 持有一部分输入行），这样中间结果 `XW1` 就可以不做通信，激活值只在 FFN 尾端做一次 AllReduce。Attention 也是同样的思路：QKV 按 head 维度切，每个 rank 独立算自己那部分 head，最后 output projection 做一次 AllReduce。

所以 TP 的通信代价是**每层 2 次 AllReduce**（Attention 和 FFN 各一次）。对于一个 80 层的 70B 模型，前向一次就有 160 次 AllReduce。这个数字看着不吓人，但每次 AllReduce 要传的是整个激活值 `[batch*seq, hidden]`，对 LLaMA 70B 来说 hidden=8192，batch×seq=4096 的话一次就是 128MB FP16。160 次就是 20GB 级别的跨卡流量。单机 NVLink 900GB/s 是无感的，跨机 100Gbps RDMA 就会明显掉速。

**结论一**：TP 在单机 NVLink 内可以随便用，一旦跨机要慎重。经验上 TP 尺寸不建议超过单机的 NVLink 域大小（通常是 8）。

### 1.2 Pipeline Parallel 在做什么

PP 是**模型层间**的切分，把 80 层切成几段分别放到不同机器上。通信只发生在段的边界，传输的是段末的激活值，跟 TP 的每层 AllReduce 相比，通信量小一个数量级。代价是：

- 存在**流水线气泡**（bubble），第一个请求的首 token 要等所有段都过一遍
- PP 对 batch 要求更苛刻，小 batch 时气泡占比更大
- 推理场景不像训练那么容易用 1F1B 这类调度填满气泡

**结论二**：跨机优先用 PP，单机内优先用 TP。典型组合是 `TP=8, PP=2`（2 台 8 卡），或者 `TP=8, PP=4`（4 台 8 卡）。

### 1.3 Data Parallel

DP 其实不是真的把一个请求拆开，而是同一个完整模型拷贝多份，每份独立服务一部分请求。vLLM 自身不直接做 DP——DP 是**上层网关**的事情，比如前面挂一个 LiteLLM 或 Envoy，轮询到不同的 vLLM 实例。所以如果你只是想扩吞吐而不扩模型，**不要用 vLLM 的多机 TP，而是起多个单机 vLLM + 网关分流**，这是最省心的方案。

## 二、什么时候必须上多机

决定"要不要上多机"前先过一遍这个流程：

```
显存需求 ≈ 模型权重 + KV Cache + 激活 + 一点点 workspace

模型权重：
  FP16  → 参数量 × 2  byte
  FP8   → 参数量 × 1  byte
  INT4  → 参数量 × 0.5 byte

KV Cache 每 token：
  2 × num_layers × num_kv_heads × head_dim × dtype_bytes
```

举几个典型例子（H100 80GB 单卡）：

| 模型 | 精度 | 权重 | 每卡留给 KV Cache | 单机 8 卡能装的最大并发 token 数 |
|---|---|---|---|---|
| LLaMA 70B | FP16 | 140 GB | 约 280 GB（TP=8 平摊后剩余） | 百万级 |
| LLaMA 70B | FP8 | 70 GB | 约 490 GB | 数百万 |
| LLaMA 405B | FP16 | 810 GB | **装不下** | — |
| LLaMA 405B | FP8 | 405 GB | 约 235 GB | 中等并发 |
| DeepSeek V2/V3 236B MoE | FP8 | 236 GB | 约 400 GB | 较高 |

所以判断很简单：

- 70B / FP16 单机够用 → **不要上多机，起多实例 DP**
- 405B / FP16 → **必须跨机**
- 405B / FP8 → **单机勉强**，但留给 KV 的显存太少，高并发还是要跨机
- 70B 想冲吞吐极限 → **优先 DP，实在要上 TP 也别超出单机**

## 三、架构图：vLLM 多机启动的两种模式

vLLM 分布式有两种底层驱动：**Ray** 和 **MultiProcessing**。MP 只能单机用，跨机必须 Ray。

### 3.1 单机多卡（MP 模式）

```
 ┌─────────────────────────────────────────────┐
 │              Node A (单机 8×H100)            │
 │  ┌────────────────────────────────────┐     │
 │  │           vLLM LLMEngine           │     │
 │  │  ┌─────────┐  ┌─────────┐          │     │
 │  │  │ Worker0 │  │ Worker1 │  ... ×8  │     │
 │  │  │ GPU 0   │  │ GPU 1   │          │     │
 │  │  └────┬────┘  └────┬────┘          │     │
 │  │       └──NCCL──────┘               │     │
 │  └────────────────────────────────────┘     │
 │         NVLink/NVSwitch 域                   │
 └─────────────────────────────────────────────┘
```

### 3.2 多机多卡（Ray 模式）

```
                ┌──────────────┐
                │  Ray Head    │
                │ (Node A GPU0)│
                │ vLLM Engine  │
                └──────┬───────┘
                       │ Ray RPC
         ┌─────────────┼─────────────┐
         │             │             │
   ┌─────▼────┐  ┌─────▼────┐  ┌─────▼────┐
   │ Worker A │  │ Worker B │  │ Worker C │
   │ TP rank  │  │ TP rank  │  │ TP rank  │
   │ 0..7     │  │ 8..15    │  │ 16..23   │
   │ Node A   │  │ Node B   │  │ Node C   │
   └────┬─────┘  └─────┬────┘  └─────┬────┘
        │              │             │
        └──── NCCL over RDMA/IB ─────┘
              (100/200/400 Gbps)
```

Ray Head 负责调度和 API 层，真正干活的是一组 Worker Actor，每个 Worker 绑定一张 GPU。NCCL 通信是 Worker 之间点对点直连，不经过 Ray Head。这意味着一旦 NCCL 初始化成功，Ray 本身的网络开销就可以忽略——Ray 只在请求分发、tokenize、采样结果回收时参与。

## 四、准备工作

### 4.1 硬件和网络

跨机 TP 最吃网络。最低要求：

- 节点间至少 **100Gbps** 级别的 RDMA / RoCEv2 / InfiniBand
- GPU Direct RDMA 打开（避免 PCIe 回传）
- NVLink / NVSwitch 域内 TP，跨域 PP

10Gbps TCP 别想了，LLaMA 70B TP=16 跨 10Gbps 会被网络吃死，首 token 延迟能从 80ms 飙到 2s 以上。

### 4.2 软件栈版本约束

一个真正头疼的点：vLLM、PyTorch、CUDA、NCCL、xformers、FlashAttention 这几个组件版本锁得死死的。我的习惯是**每次升级只动一个**：

- vLLM 0.6+ 支持 PP，之前只能 TP
- PyTorch 2.3+ 与 CUDA 12.1+ 配对比较稳
- NCCL 2.20+ 对 H100/H200 的 SHARP 支持更好
- FlashAttention 2.5+ 才对 Hopper 的 FP8 友好

升级流程一定是：**在测试集群双写一周 → 流量灰度 10% → 全量**，不要直接升 prod。

### 4.3 环境变量

跨机启动前几个环境变量必须配对：

```bash
# NCCL 基础
export NCCL_DEBUG=INFO              # 第一次上线开 INFO，稳定后改 WARN
export NCCL_IB_DISABLE=0            # 确保启用 IB
export NCCL_IB_GID_INDEX=3          # RoCEv2 常见值
export NCCL_SOCKET_IFNAME=eth0      # 管理网口，用于 bootstrap
export NCCL_IB_HCA=mlx5_0,mlx5_1    # 显式指定 IB HCA
export NCCL_P2P_LEVEL=NVL           # 单机内走 NVLink
export NCCL_NET_GDR_LEVEL=PHB       # 启用 GDR

# Ray
export RAY_DEDUP_LOGS=0
export RAY_USAGE_STATS_ENABLED=0

# vLLM
export VLLM_WORKER_MULTIPROC_METHOD=spawn
export VLLM_ENGINE_ITERATION_TIMEOUT_S=600
```

`NCCL_SOCKET_IFNAME` 是最常翻车的参数——不设会让 NCCL 选到 docker0、cali 之类的虚拟网卡，bootstrap 通不过，Worker 一直 hang 在 `ncclCommInitRank`。

## 五、启动流程：单机

单机 8 卡跑 70B，最小可用命令：

```bash
python -m vllm.entrypoints.openai.api_server \
    --model /models/Llama-3.1-70B-Instruct \
    --tensor-parallel-size 8 \
    --gpu-memory-utilization 0.92 \
    --max-model-len 8192 \
    --max-num-seqs 256 \
    --dtype float16 \
    --enforce-eager=false \
    --disable-log-requests \
    --port 8000
```

几个参数的含义：

| 参数 | 作用 | 常见坑 |
|---|---|---|
| `--tensor-parallel-size` | TP 并行度 | 必须能被 num_heads 整除，很多模型设 8 OK，设 6 就炸 |
| `--gpu-memory-utilization` | 允许 vLLM 占用的显存比例 | 默认 0.9，高并发时调到 0.92~0.95，再高容易 OOM |
| `--max-model-len` | 支持的最大上下文 | 直接影响 KV 池子大小，别开到模型上限 |
| `--max-num-seqs` | 同时在跑的序列数 | 限制并发度，和 `--max-num-batched-tokens` 联动 |
| `--enforce-eager` | 关闭 CUDA Graph | debug 时开 true，生产要 false |
| `--swap-space` | CPU swap 大小 GB | 0.6+ 版本默认 4GB，批量离线推理可以调大 |

### 5.1 gpu-memory-utilization 怎么定

这个参数看起来简单，其实隐含了一个公式：

```
可用显存 = 总显存 × utilization
     = 权重显存 + KV Cache 显存 + 激活 + workspace
```

vLLM 启动时会做一次 profiling，先加载权重，再用当前空闲显存去反算 KV Cache block 数（PagedAttention 的分页单位）。如果 utilization 留太小，KV 池子就小，高并发时请求堆积；留太大，激活显存和 NCCL workspace 挤不出来就 OOM。我的经验值：

- 70B FP16 TP=8：`0.92`
- 405B FP8 TP=8 PP=2：`0.90`
- 任何会跑长上下文（>32K）的场景：`0.88`，给激活留余量

## 六、启动流程：多机

### 6.1 先拉 Ray 集群

Node A（head）：

```bash
ray start --head \
    --node-ip-address=10.0.1.10 \
    --port=6379 \
    --num-gpus=8 \
    --dashboard-host=0.0.0.0 \
    --dashboard-port=8265
```

Node B（worker）：

```bash
ray start \
    --address=10.0.1.10:6379 \
    --node-ip-address=10.0.1.11 \
    --num-gpus=8
```

检查：

```bash
ray status
# 期望看到 CPU: xx, GPU: 16.0, Node: 2
```

**坑 1**：`ray start` 的 `--node-ip-address` 必须是**NCCL 能走的那张网卡的 IP**，不是管理网。很多人图省事写成 127.0.0.1 或者 eth0，结果 Ray 集群起来了，但 NCCL 初始化就挂。

**坑 2**：两台机器上 vLLM / PyTorch / CUDA / Python 版本必须**完全一致**。哪怕你 rsync 了同一份 conda env，也要确认 GPU driver 版本一致，不然 Worker 启动报各种 cuBLAS 符号找不到。

### 6.2 启动 vLLM

在 head 节点：

```bash
python -m vllm.entrypoints.openai.api_server \
    --model /shared/models/Llama-3.1-405B-Instruct-FP8 \
    --tensor-parallel-size 8 \
    --pipeline-parallel-size 2 \
    --distributed-executor-backend ray \
    --gpu-memory-utilization 0.90 \
    --max-model-len 16384 \
    --max-num-seqs 128 \
    --dtype auto \
    --trust-remote-code \
    --host 0.0.0.0 --port 8000
```

注意：

- 模型路径必须在**每台机器上都能访问**，要么 NFS/EFS 共享，要么提前 rsync
- `--distributed-executor-backend ray` 显式指定
- TP × PP 必须等于总 GPU 数（这里 8×2=16）

### 6.3 验证

启动后先发一个最小请求：

```bash
curl http://10.0.1.10:8000/v1/completions \
    -H "Content-Type: application/json" \
    -d '{
        "model": "/shared/models/Llama-3.1-405B-Instruct-FP8",
        "prompt": "hello",
        "max_tokens": 16
    }'
```

看不到响应就查：

1. Ray dashboard（:8265）Worker 是不是全绿
2. `ray logs` 里有没有 NCCL 的 WARN
3. head 节点 vLLM 日志最后一行停在哪
4. `nvidia-smi` 看两台机器 GPU 是不是都有进程占用

## 七、NCCL 调优实战

NCCL 是多机推理性能的命门。下面是我在 H100 × 2 节点（200Gbps ConnectX-7）环境里调过的参数。

### 7.1 拓扑打印

第一次上线一定要看一次 NCCL 拓扑：

```bash
export NCCL_DEBUG=INFO
export NCCL_TOPO_DUMP_FILE=/tmp/nccl-topo.xml
# 启动 vLLM，然后看日志里的 Channel 信息
```

重点关注：

- `NCCL INFO Channel ... via NET/IB/0` → 走的是 IB，✓
- `NCCL INFO Channel ... via SOCKET` → 退化到 TCP，✗
- `NCCL INFO NET/IB : Using ...` → 看使用的 HCA 数量，理想情况是每张卡绑一个 HCA 走 GDR

### 7.2 关键参数表

| 参数 | 推荐值 | 说明 |
|---|---|---|
| `NCCL_IB_HCA` | `mlx5_0,mlx5_1,mlx5_2,mlx5_3` | 显式绑定可用 HCA |
| `NCCL_IB_GID_INDEX` | 3（RoCEv2）/ 0（IB） | 选错会 NCCL_IB_TIMEOUT |
| `NCCL_IB_TIMEOUT` | 22 | 2^22 ns，默认太短 |
| `NCCL_IB_RETRY_CNT` | 7 | 重试次数 |
| `NCCL_NET_GDR_LEVEL` | `PHB` 或 `PIX` | 开 GDR |
| `NCCL_P2P_LEVEL` | `NVL` | 单机走 NVLink |
| `NCCL_CROSS_NIC` | 1 | 多 NIC 场景打开 |
| `NCCL_MIN_NCHANNELS` | 16 | 大消息场景增加并行 channel |
| `NCCL_MAX_NCHANNELS` | 32 | — |
| `NCCL_ALGO` | `Tree,Ring` | 让 NCCL 自动选，除非你要测 |
| `NCCL_BUFFSIZE` | `8388608` | 8MB，大张量场景 |

### 7.3 NCCL hang 的典型定位

症状：vLLM 启动卡在 `initialize model parallel`，两台机器 GPU 占用有但 utilization 为 0。

排查顺序：

1. **先看是不是 bootstrap 挂了**：`NCCL_DEBUG=INFO` 输出有没有到 `NCCL INFO Bootstrap : Using eth0:10.0.1.10<0>` 这一行
2. 如果 bootstrap 没过，九成是 `NCCL_SOCKET_IFNAME` 选错网卡
3. bootstrap 过了但后面没了，看有没有 `NCCL WARN Connect failed` → 是 IB/RoCE 配置问题
4. 都没有但就是卡住 → `py-spy dump --pid <pid>` 看 Python 栈，八成卡在 `cudart.cudaDeviceSynchronize()`，这是 NCCL 在等对端
5. 跨机时间不同步会导致 NCCL_TIMEOUT，NTP 配对

## 八、PagedAttention 和 KV Cache 核算

PagedAttention 是 vLLM 的核心武器。原理上它把 KV Cache 切成固定大小的 block（默认 16 tokens），用类似虚拟内存分页的方式管理，请求之间共享物理页，消除了 KV Cache 的内部碎片。

对运维来说需要关心三件事：

### 8.1 block_size 怎么选

| block_size | 优点 | 缺点 |
|---|---|---|
| 8 | 小请求浪费少 | block 数多，索引开销大 |
| 16（默认） | 平衡 | — |
| 32 | 大 batch 吞吐高 | 短请求浪费 |

除非你的负载非常偏（纯短请求或纯长上下文），默认 16 最稳。

### 8.2 KV block 总数怎么算

启动日志里 vLLM 会打印类似：

```
INFO: # GPU blocks: 12345, # CPU blocks: 2048
```

每个 GPU block 能容纳 `block_size` 个 token 的 KV。总可服务 token 数 = GPU blocks × block_size。

举例：LLaMA 70B FP16，TP=8 在 H100 上，典型跑出来 GPU blocks 在 3-4 万级别，支持总 token 数 50-60 万。这个数字除以 `max_num_seqs` 就是每个请求平均能吃的上下文。

如果你看到 GPU blocks 只有几千，那说明 `gpu-memory-utilization` 给小了或者 `max-model-len` 给大了，vLLM 把太多显存留给了可能的 max-len 请求。

### 8.3 Prefix Caching

vLLM 0.4+ 支持 `--enable-prefix-caching`，对同 prefix 的请求共享 KV block。对 RAG 场景（system prompt 很长、doc 固定）效果拔群，能把首 token 延迟砍 30%-60%。代价是：

- 长尾显存回收策略会有轻微扰动
- prefix 必须是**精确匹配**（包括分词后的 token 序列一致）
- 动态改 system prompt 的业务受益有限

开启命令：`--enable-prefix-caching`，无副作用建议常开。

## 九、调优参数速查表

这张表是我日常用的 cheat sheet，按场景分类：

| 场景 | 关键参数 | 推荐值 |
|---|---|---|
| 通用高吞吐 | `max-num-batched-tokens` | 8192 ~ 16384 |
|  | `max-num-seqs` | 256 |
|  | `gpu-memory-utilization` | 0.92 |
| 低延迟（首 token） | `max-num-batched-tokens` | 2048 ~ 4096 |
|  | `max-num-seqs` | 64 |
|  | `enforce-eager` | false（必须用 CUDA Graph） |
|  | `enable-chunked-prefill` | true |
| 长上下文（>32K） | `max-model-len` | 明确设小一点，别到模型上限 |
|  | `gpu-memory-utilization` | 0.88 |
|  | `block-size` | 16 |
| 多机 PP | `pipeline-parallel-size` | 2 或 4 |
|  | 其余同上 |  |
| RAG / 固定 prompt | `enable-prefix-caching` | true |

### 9.1 Chunked Prefill

vLLM 0.5+ 加入 chunked prefill，允许把长 prompt 的 prefill 阶段切块，和 decode 阶段交织调度。效果是**长请求不会把整个 batch 憋死**，decode 延迟更平稳。默认不开，低延迟场景建议开。

```
--enable-chunked-prefill \
--max-num-batched-tokens 2048
```

注意 chunked prefill 开了以后 `max-num-batched-tokens` 要调小，因为这个参数就是每步的"预算"，太大就失去切块的意义。

## 十、生产踩坑合集

### 坑 1：Ray head 挂了整个集群崩

Ray head 是单点。生产环境要么：

- 把 Ray head 放在 K8s Deployment 里，挂了自动重启，但正在服务的请求会断
- 配双 head（Ray 2.5+ 支持 GCS HA），运维复杂度成倍上升

我的折中方案：head 放独立 Pod，vLLM engine 也放 head，worker 放 StatefulSet。head 挂了从 readinessProbe 下线，K8s Service 把流量切到备集群。不要试图让单个 Ray 集群具备 HA。

### 坑 2：显存碎片导致的 OOM

症状：跑了几小时后 `CUDA out of memory`，重启就好。典型是 NCCL workspace + KV Cache 的相互挤压。

定位：`nvidia-smi` 看不到满，但 PyTorch 报 OOM。这是**PyTorch caching allocator 的碎片**，不是真的没显存。

解法：

```bash
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
```

或者把 `gpu-memory-utilization` 再降 0.02。

### 坑 3：`max-model-len` 吃掉了所有 KV 池子

用户反馈 tps 不高，并发上不去。一查 `max-model-len` 设到了模型极限（比如 LLaMA 3.1 的 128K），但 vLLM 为了保证"理论上能服务一个 128K 请求"，会按 max-len 反推 block 数，结果池子里永远只能放几个请求。

解法：**按业务实际需要设**，99% 业务 8K 就够，就设 8192，别设 128K。

### 坑 4：P95 延迟忽然抖动

多机部署常见。定位三件套：

1. Grafana 看每台机器 NIC 流量，有没有某张卡带宽打满
2. `nvidia-smi dmon -s pucvmet` 看 GPU util 和 SM clock，有没有降频
3. Ray dashboard 看 Worker 有没有假死

有一次我们的症状是 P95 从 200ms 跳到 1.5s，最后定位是某台机器 BIOS 里 Power Profile 被改成了 "Balanced"，GPU boost clock 上不去。改回 "Maximum Performance" 就恢复了。

### 坑 5：FP8 权重的坑

FP8 模型（比如 Meta 官方放出的 Llama 3.1 405B FP8）在 vLLM 上要指定 `--quantization fp8` 或 `--dtype auto`。我踩过的坑是模型 config 里是 fp8，但 `--dtype` 又手动传了 float16，结果 vLLM 做了一次隐式反量化，显存直接翻倍 OOM。教训：**FP8 模型就让 dtype auto，不要手贱指定**。

### 坑 6：Prefix Caching 和动态 LoRA 冲突

vLLM 的 Multi-LoRA 功能（`--enable-lora`）和 `--enable-prefix-caching` 在 0.5 之前版本有兼容问题，会出现缓存命中但输出混了其他 LoRA 的情况。后续版本修了一部分，但生产环境我还是会关掉其中一个，优先保正确性。

### 坑 7：共享存储挂载慢导致启动超时

405B 模型权重 800GB+ 从 NFS 加载能耗 10 分钟以上。K8s 的 startupProbe 一定要给足 600s 甚至 1200s，不然 Pod 一直被 kill 重启。更好的方案是权重预下载到本地 NVMe。

### 坑 8：tokenizer 慢成瓶颈

高 QPS 下 tokenizer 可能成为 CPU 瓶颈。vLLM 支持 `--tokenizer-pool-size` 把 tokenize 放到独立进程池。对于 QPS > 500 的场景，调到 4~8 明显缓解。

## 十一、场景选型对比

下面这张表是我给业务方推荐方案时的决策树：

| 业务特征 | 推荐方案 | 原因 |
|---|---|---|
| 7B / 13B，QPS < 100 | 单卡 + DP 多实例 | 不用上 TP，浪费 NVLink |
| 70B FP16，QPS < 50 | 单机 8 卡 TP=8 | 舒适区 |
| 70B FP16，QPS > 200 | 多实例 × 单机 TP=8 | 横向扩，不要跨机 TP |
| 70B 超长上下文 128K | 单机 TP=8 + 减 max_num_seqs | KV 吃光显存，要牺牲并发 |
| 405B FP8，QPS 适中 | 单机 TP=8 | 能装下就别跨机 |
| 405B FP16 或超高并发 | 2 机 TP=8 PP=2 | 必须跨机 |
| MoE（DeepSeek / Mixtral） | TP + EP 组合 | EP 专门优化专家分布 |
| 在线 + 离线混合 | 拆两个集群 | 别混，调度策略完全不同 |

## 十二、监控与告警

上线后要盯的指标：

**GPU 层**：
- `DCGM_FI_DEV_GPU_UTIL`：SM 利用率，稳态应该 60%-85%
- `DCGM_FI_DEV_MEM_COPY_UTIL`：显存带宽利用率
- `DCGM_FI_DEV_SM_CLOCK`：有没有降频
- `DCGM_FI_DEV_POWER_USAGE`：功耗，降频先兆

**vLLM 层**（vLLM 自带 Prometheus endpoint）：
- `vllm:num_requests_running`：正在跑的序列数
- `vllm:num_requests_waiting`：排队数，大于 0 说明容量不够
- `vllm:gpu_cache_usage_perc`：KV Cache 使用率
- `vllm:time_to_first_token_seconds`：首 token 延迟
- `vllm:time_per_output_token_seconds`：每 token 延迟
- `vllm:e2e_request_latency_seconds`：端到端

**NCCL 层**：
- 节点间 NIC 发送/接收带宽
- IB/RoCE 错包数，有错包立刻告警

告警规则示例：

- `num_requests_waiting > 10 持续 5 分钟` → 容量告警，考虑扩容
- `time_to_first_token_p95 > 500ms` → 延迟告警，查 prefill 是不是被长请求憋死
- `gpu_cache_usage_perc > 0.95 持续 10 分钟` → KV 池子快满，看有没有 OOM 风险
- `NCCL 错包 > 0` → 立刻 P1

## 十三、一个完整的 K8s Deployment 骨架

给一个 2 机 16 卡 405B 的 StatefulSet 骨架，不是完整可跑，但关键字段都在：

```yaml
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: vllm-405b
spec:
  serviceName: vllm-405b-headless
  replicas: 2
  selector:
    matchLabels:
      app: vllm-405b
  template:
    metadata:
      labels:
        app: vllm-405b
    spec:
      hostNetwork: true  # 让 NCCL 直接用物理网卡
      nodeSelector:
        node.kubernetes.io/instance-type: p5.48xlarge
      tolerations:
        - key: nvidia.com/gpu
          operator: Exists
      containers:
        - name: vllm
          image: your-registry/vllm:0.6.x-cuda12.1
          command: ["/bin/bash", "-c"]
          args:
            - |
              if [ "${POD_NAME##*-}" = "0" ]; then
                ray start --head \
                  --node-ip-address=$POD_IP \
                  --port=6379 \
                  --num-gpus=8 \
                  --block &
                sleep 20
                python -m vllm.entrypoints.openai.api_server \
                  --model /models/llama-3.1-405b-fp8 \
                  --tensor-parallel-size 8 \
                  --pipeline-parallel-size 2 \
                  --distributed-executor-backend ray \
                  --gpu-memory-utilization 0.90 \
                  --max-model-len 16384 \
                  --host 0.0.0.0 --port 8000
              else
                sleep 30
                ray start \
                  --address=vllm-405b-0.vllm-405b-headless:6379 \
                  --node-ip-address=$POD_IP \
                  --num-gpus=8 \
                  --block
              fi
          env:
            - name: POD_NAME
              valueFrom:
                fieldRef:
                  fieldPath: metadata.name
            - name: POD_IP
              valueFrom:
                fieldRef:
                  fieldPath: status.podIP
            - name: NCCL_DEBUG
              value: "WARN"
            - name: NCCL_IB_DISABLE
              value: "0"
            - name: NCCL_SOCKET_IFNAME
              value: "eth0"
          resources:
            limits:
              nvidia.com/gpu: 8
              rdma/hca: 4
          volumeMounts:
            - name: models
              mountPath: /models
            - name: shm
              mountPath: /dev/shm
          startupProbe:
            httpGet:
              path: /health
              port: 8000
            periodSeconds: 10
            failureThreshold: 120  # 20 分钟启动预算
      volumes:
        - name: models
          persistentVolumeClaim:
            claimName: vllm-models-pvc
        - name: shm
          emptyDir:
            medium: Memory
            sizeLimit: 64Gi
```

几个要点：

- `hostNetwork: true` 让 NCCL 直接用节点网卡
- `/dev/shm` 挂内存盘，PyTorch 跨进程通信会用
- Pod 编号 0 当 Ray head，其余 join
- startupProbe 给足时间
- `rdma/hca` 需要先装 rdma-shared-dev-plugin

## 十四、收尾

分布式推理能不做就不做。优先级永远是：

1. 用量化（FP8 / INT4）把模型压进单机
2. 用多实例 + 网关做 DP 扩吞吐
3. 实在必须跨机，优先 PP 不是 TP
4. TP 跨机只在 NVLink 域被打穿后才考虑

真到了要上多机那一步，记住这篇文章里的那些环境变量、那张调优表、和那几个坑。大多数"vLLM 跑不起来"的问题最后都收敛到 NCCL 配置、网络不对称、或者版本不匹配这三类上。

祝你第一次拉起 16 卡 405B 时日志里出现的不是 `NCCL WARN Timeout`。
