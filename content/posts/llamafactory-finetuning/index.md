---
title: "LLaMA Factory 微调工具链实战：从数据准备到 LoRA 合并的全流程"
date: 2026-03-18T11:20:00+08:00
draft: false
tags: ["LLaMA Factory", "微调", "LoRA", "DPO", "LLM"]
categories: ["AI 工程"]
description: "LLaMA Factory 把大模型微调的很多 trick 工程化了。本文按一个完整项目的节奏讲：数据、SFT、LoRA、DPO、合并、评估和常见坑。"
summary: "LLaMA Factory 把大模型微调的很多 trick 工程化了。本文按一个完整项目的节奏讲：数据、SFT、LoRA、DPO、合并、评估和常见坑。"
toc: true
math: false
diagram: false
keywords: ["LLaMA Factory", "SFT", "LoRA", "DPO", "QLoRA"]
params:
  reading_time: true
---

## 为什么是 LLaMA Factory

做大模型微调有太多选择：官方 `trl` + `peft` 组合、Axolotl、Unsloth、LLaMA Factory、以及各家云厂商的托管服务。真要落地到业务，决策维度其实就三个：

1. **覆盖的模型和方法够不够全**
2. **参数够不够暴露，能不能调**
3. **出坑后的可 debug 程度**

LLaMA Factory 在这三点上是目前开源里综合最好的一个。它把 SFT、LoRA、QLoRA、DPO、KTO、ORPO、PPO、预训练 continuous pretrain、reward model 训练全部统一在一套 CLI 和 WebUI 里，模型覆盖 LLaMA/Qwen/Mistral/DeepSeek/ChatGLM 等主流家族，配置全部 YAML 驱动，debug 时能一层层打开看。

这篇文章按我实际做一个垂直领域模型（基于 Qwen2.5-14B 做 LoRA SFT + DPO）的全流程节奏来写，把每一步的关键参数、踩过的坑、踩坑后的应对都记下来。

## 一、整体流程

```
 ┌──────────────┐
 │  原始数据     │ (业务对话日志、人工标注、外部采购)
 └───────┬──────┘
         │ 清洗 / 去重 / 脱敏
         ▼
 ┌──────────────┐
 │  训练数据集   │ (JSONL, alpaca / sharegpt 格式)
 └───────┬──────┘
         │ register in dataset_info.json
         ▼
 ┌──────────────┐         ┌───────────────┐
 │  LoRA SFT    │────────▶│  LoRA Adapter │
 └───────┬──────┘         └───────┬───────┘
         │                        │
         │ 人工偏好标注              │
         ▼                        │
 ┌──────────────┐                 │
 │   DPO pair   │                 │
 └───────┬──────┘                 │
         │                        │
         ▼                        │
 ┌──────────────┐         ┌───────▼───────┐
 │  LoRA DPO    │────────▶│ LoRA Adapter  │
 └───────┬──────┘         │  (stage 2)    │
         │                └───────┬───────┘
         │                        │
         └──────┬─────────────────┘
                ▼
        ┌───────────────┐
        │  Merge to base │
        └───────┬───────┘
                ▼
        ┌───────────────┐
        │   Eval + 部署  │
        └───────────────┘
```

每一步都对应 LLaMA Factory 的一个子命令：`llamafactory-cli train`、`llamafactory-cli export`、`llamafactory-cli eval`、`llamafactory-cli webui`。

## 二、环境准备

### 2.1 依赖版本

LLaMA Factory 本身是 Python 包，但对底层库要求严格：

- Python 3.10+
- PyTorch 2.3+ / CUDA 12.1+
- `transformers` 4.41+
- `peft` 0.11+
- `trl` 0.9+
- `accelerate` 0.30+
- `bitsandbytes` 0.43+（QLoRA 需要）
- `flash-attn` 2.5+（H100 推荐 2.6+）
- `deepspeed` 0.14+（全参数或大模型分布式需要）

这堆版本互相耦合很严重。我建议两种方式：

1. 用 LLaMA Factory 官方 Docker 镜像，直接 `docker pull`
2. 用 conda + `pip install -e .[torch,metrics]` 自己装，但锁定一个 commit hash

### 2.2 硬件

- 7B QLoRA：1×24GB（3090 / 4090 / A10）
- 7B LoRA：1×40GB（A100）
- 13B QLoRA：1×40GB
- 13B LoRA：1×80GB 或 2×40GB
- 14B/20B LoRA：单机 2-4×A100
- 70B QLoRA：2×80GB 或 单 H100 80GB
- 70B LoRA：4-8×A100/H100
- 70B 全参数：不建议，8×H100 还要 DeepSpeed ZeRO-3

## 三、数据准备：魔鬼都在这里

微调效果的 70% 由数据决定。这一节是最该花时间的。

### 3.1 数据格式

LLaMA Factory 原生支持两种常见格式：

**alpaca 格式**（单轮）：

```json
{
  "instruction": "把下面这句中文翻译成英文",
  "input": "今天天气真好",
  "output": "The weather is really nice today."
}
```

**sharegpt 格式**（多轮）：

```json
{
  "conversations": [
    {"from": "human", "value": "你好"},
    {"from": "gpt", "value": "你好，有什么可以帮你？"},
    {"from": "human", "value": "写一首五言绝句"},
    {"from": "gpt", "value": "..."}
  ],
  "system": "你是一个诗人",
  "tools": ""
}
```

业务场景我几乎都用 **sharegpt 格式**——天然支持多轮、system prompt、甚至 function calling。

### 3.2 注册到 dataset_info.json

LLaMA Factory 要求所有数据集先在 `data/dataset_info.json` 里注册：

```json
{
  "my_business_sft": {
    "file_name": "my_business_sft.jsonl",
    "formatting": "sharegpt",
    "columns": {
      "messages": "conversations",
      "system": "system"
    },
    "tags": {
      "role_tag": "from",
      "content_tag": "value",
      "user_tag": "human",
      "assistant_tag": "gpt"
    }
  },
  "my_business_dpo": {
    "file_name": "my_business_dpo.jsonl",
    "formatting": "sharegpt",
    "ranking": true,
    "columns": {
      "messages": "conversations",
      "chosen": "chosen",
      "rejected": "rejected"
    }
  }
}
```

`ranking: true` 标识 DPO 数据。每个数据集的字段映射可以自定义，不需要改文件格式。

### 3.3 数据清洗 checklist

这是我做过几个项目后沉淀下来的清洗清单：

```
[ ] 去重（按输入+输出完全匹配 + 按输入的 MinHash 近似去重）
[ ] 去除超短样本（< 10 token 的 output）
[ ] 去除超长样本（> 模型 max_length 的 80%）
[ ] 敏感信息脱敏（手机号、身份证、邮箱、公司名）
[ ] 过滤拒答样本（"我不能回答"、"我无法提供"）除非业务就要这个
[ ] 格式异常过滤（JSON 截断、代码未闭合）
[ ] 用小模型 embed 算语义簇，手动检查每个簇有没有脏数据
[ ] 至少人工 review 500 条样本
[ ] 划分 train / eval，eval 集固定后不动
```

垃圾进垃圾出这句话在 LLM 微调里百分百成立。我见过业务数据有 30% 的噪声直接把 7B SFT 整成胡言乱语，清洗一遍后模型立刻正常。

### 3.4 数据量经验值

| 任务类型 | 建议数据量 |
|---|---|
| 风格改造（口吻、格式） | 1k~5k |
| 领域适配（金融、法律、医疗） | 10k~50k |
| 复杂任务（代码、数学推理） | 50k+ |
| 通用指令 follow | 100k+ |

少于 1k 条的 LoRA SFT 基本是玄学，能训出啥全看运气。

## 四、SFT：配置和启动

LLaMA Factory 的训练入口是 YAML 配置文件 + `llamafactory-cli train config.yaml`。一个典型的 LoRA SFT 配置：

```yaml
### model
model_name_or_path: /models/Qwen2.5-14B-Instruct
trust_remote_code: true

### method
stage: sft
do_train: true
finetuning_type: lora
lora_target: all
lora_rank: 32
lora_alpha: 64
lora_dropout: 0.05

### dataset
dataset: my_business_sft
template: qwen
cutoff_len: 4096
max_samples: 20000
overwrite_cache: true
preprocessing_num_workers: 16
dataloader_num_workers: 4

### output
output_dir: /checkpoints/qwen14b-biz-sft-lora
logging_steps: 10
save_steps: 500
plot_loss: true
overwrite_output_dir: true
save_total_limit: 3

### train
per_device_train_batch_size: 2
gradient_accumulation_steps: 8
learning_rate: 1.0e-4
num_train_epochs: 3.0
lr_scheduler_type: cosine
warmup_ratio: 0.03
bf16: true
ddp_timeout: 180000000
flash_attn: fa2
gradient_checkpointing: true

### eval
val_size: 0.02
per_device_eval_batch_size: 4
eval_strategy: steps
eval_steps: 500

### deepspeed (optional)
# deepspeed: examples/deepspeed/ds_z2_config.json
```

### 4.1 关键参数详解

#### lora_target

`lora_target: all` 让 LoRA 应用到所有线性层（q/k/v/o + gate/up/down）。早期版本默认只挂 q/v，效果差很多。大部分场景用 `all`，模型能力提升明显，显存多花一点。

#### lora_rank / lora_alpha

- `rank` 小 → 参数少，欠拟合；大 → 参数多，过拟合风险
- `alpha` 通常取 `2 * rank`
- 经验：rank 8~32 适合大多数场景，64+ 只在数据量很大、任务复杂时上

#### cutoff_len

这是 tokenize 后的截断长度。cutoff_len 越大：

- 单样本能容纳更长的上下文
- 显存占用近似线性增长（attention 是 O(n²) 但 Flash Attention 把内存摊平到 O(n)）

业务大部分 SFT 数据在 1k-2k token 之内，设 4096 足够。只有代码、长文档场景才需要 8192+。

#### per_device_train_batch_size + gradient_accumulation_steps

有效 batch size = `per_device * accumulate * num_gpus`。经验：

- 14B LoRA：有效 batch 16-32 合适
- 7B LoRA：有效 batch 32-64
- 70B LoRA：有效 batch 16-32

显存撑不住就增加 `gradient_accumulation`，不要减小 `cutoff_len`。

#### learning_rate

LoRA 的 LR 比全参数大 10 倍：

- LoRA：1e-4 ~ 3e-4
- QLoRA：1e-4 ~ 5e-4
- 全参 SFT：1e-5 ~ 5e-5

一般从 1e-4 起步，loss 不降再调到 2e-4。

#### gradient_checkpointing

开了能省 30% 显存，代价是慢 20-30%。大模型 LoRA 必开。

#### bf16 vs fp16

- Hopper (H100)：无脑 bf16
- Ampere (A100)：bf16
- Turing/Volta（V100/T4）：fp16（不支持 bf16）

bf16 数值范围更广，训练稳定性比 fp16 好很多，除非硬件不支持不要用 fp16。

#### flash_attn

`flash_attn: fa2` 开启 Flash Attention 2。前提是 flash-attn 库装好且模型支持（主流 decoder-only 都支持）。能降 20-40% 显存 + 提速。

### 4.2 启动训练

单机：

```bash
llamafactory-cli train config/qwen14b_sft.yaml
```

多卡 DDP：

```bash
FORCE_TORCHRUN=1 llamafactory-cli train config/qwen14b_sft.yaml
```

多机（需要每台机器同时启动）：

```bash
FORCE_TORCHRUN=1 \
NNODES=2 NODE_RANK=0 MASTER_ADDR=10.0.1.10 MASTER_PORT=29500 \
llamafactory-cli train config/qwen14b_sft.yaml
```

### 4.3 训练监控

LLaMA Factory 自带 Loss 曲线 `plot_loss: true`，训练结束后在 `output_dir` 里生成 `training_loss.png`。

更正规做法是接 wandb 或 swanlab：

```yaml
report_to: wandb
run_name: qwen14b-biz-sft-v1
```

我的观察顺序：

1. **train_loss 曲线**：前 100 步降得快，之后稳步下降，最后趋平。如果一直不降或突然爆炸 → LR 太大、数据有问题
2. **eval_loss**：跟 train_loss 应该接近，间隔拉大就是过拟合信号
3. **gradient norm**：稳定在某个值，突然尖峰可能是脏数据 batch
4. **learning rate**：按 cosine 正常衰减

## 五、QLoRA：资源吃紧的选择

QLoRA 是 4bit 量化 base 模型 + LoRA 增量。显存需求降一半以上，精度几乎无损。

配置变化：

```yaml
### model
model_name_or_path: /models/Qwen2.5-14B-Instruct
quantization_bit: 4
quantization_type: nf4
double_quantization: true

### method
finetuning_type: lora
lora_target: all
lora_rank: 32
```

- `quantization_bit: 4` 启用 4bit
- `quantization_type: nf4` 用 NormalFloat 4（比 fp4 精度更好）
- `double_quantization: true` 进一步压缩量化元信息

代价：

- 训练慢 10-30%（量化/反量化开销）
- 某些层数值精度受影响，复杂任务上收敛慢

QLoRA 的适用判断很简单：**显存不够用就 QLoRA**，够用就 LoRA。

## 六、DPO：对齐人类偏好

SFT 之后模型学会了"怎么说"，DPO 是教它"说哪个更好"。

### 6.1 DPO 数据格式

每条样本包含：prompt + chosen + rejected。

```json
{
  "conversations": [
    {"from": "human", "value": "帮我写一段产品描述"}
  ],
  "chosen": {"from": "gpt", "value": "这是更好的描述..."},
  "rejected": {"from": "gpt", "value": "这是差的描述..."}
}
```

DPO 数据来源：

- 对同一 prompt 让 SFT 模型采样多个答案，人工标注选好的
- 用更强的模型（GPT-4o 级）作为 judge 自动打分
- 已有业务日志里"用户点赞/点踩"的记录

### 6.2 DPO 配置

```yaml
### model
model_name_or_path: /models/Qwen2.5-14B-Instruct
adapter_name_or_path: /checkpoints/qwen14b-biz-sft-lora

### method
stage: dpo
do_train: true
finetuning_type: lora
lora_target: all
lora_rank: 32
lora_alpha: 64
pref_beta: 0.1
pref_loss: sigmoid

### dataset
dataset: my_business_dpo
template: qwen
cutoff_len: 4096
max_samples: 5000

### train
per_device_train_batch_size: 1
gradient_accumulation_steps: 16
learning_rate: 5.0e-6
num_train_epochs: 2.0
lr_scheduler_type: cosine
warmup_ratio: 0.03
bf16: true
flash_attn: fa2
```

关键参数：

- `adapter_name_or_path`：**基于已有的 SFT LoRA 继续训**，不是从头
- `pref_beta`：DPO 的 KL 惩罚系数，控制模型和 ref 模型的偏离程度。太小 → 训飞，太大 → 学不到东西。0.1 是通用默认
- `pref_loss: sigmoid`：标准 DPO loss。其他选择有 hinge、ipo
- `learning_rate`：DPO 比 SFT 小 20 倍，5e-6 比较安全。SFT 常用 1e-4 的话 DPO 就 5e-6

### 6.3 DPO 常见现象

- **reward margin 不升**：chosen 和 rejected 区分度不够，或数据质量差
- **KL 发散快**：LR 太大或 beta 太小，模型偏离 ref 太多，可能生成胡言乱语
- **eval loss 先降后升**：过拟合，减少 epochs 或降低 LR

**建议**：DPO 只跑 1-2 个 epoch。数据量 3k-10k 条效果最好，太多反而容易过拟合到标注偏见。

## 七、合并 LoRA

LoRA 训完是个小 adapter 文件，部署时要决定：**合并到 base 模型**还是**作为 adapter 动态挂载**。

### 7.1 合并

```bash
llamafactory-cli export config/merge.yaml
```

merge.yaml：

```yaml
### model
model_name_or_path: /models/Qwen2.5-14B-Instruct
adapter_name_or_path: /checkpoints/qwen14b-biz-dpo-lora
template: qwen
finetuning_type: lora

### export
export_dir: /models/qwen14b-biz-merged
export_size: 4
export_legacy_format: false
```

- `export_dir`：合并后的模型输出目录
- `export_size`：safetensors 分片大小 GB
- 不能和 `quantization_bit` 一起用（合并要求加载完整 base）

合并后得到一个完整的 `Qwen14B` 模型，可以直接被 vLLM/SGLang/TRT-LLM 加载。

### 7.2 不合并，动态挂 adapter

vLLM 和 SGLang 都支持多 LoRA：

```bash
python -m vllm.entrypoints.openai.api_server \
    --model /models/Qwen2.5-14B-Instruct \
    --enable-lora \
    --lora-modules biz=/checkpoints/qwen14b-biz-dpo-lora \
    --max-loras 4
```

请求时带 `model: biz` 就用这个 LoRA。

**合并 vs 动态挂载的选择**：

| 维度 | 合并 | 动态 LoRA |
|---|---|---|
| 推理速度 | 快（无额外开销） | 慢 5-10% |
| 显存占用 | 只有一份 | base + adapter |
| 多业务复用 | 每个业务独立模型 | 一个 base 挂多个 |
| 迭代速度 | 慢（每次都合并） | 快 |

我的习惯：开发迭代期间用动态挂载，上线前合并（为了推理性能）。

## 八、评估

微调完不能只看 train_loss，必须有端到端 eval。

### 8.1 自动评估

LLaMA Factory 支持在 MMLU / CMMLU / C-Eval 等基准上跑：

```bash
llamafactory-cli eval \
    --model_name_or_path /models/qwen14b-biz-merged \
    --task mmlu \
    --split test \
    --lang en \
    --n_shot 5 \
    --batch_size 4
```

但要注意：**通用 benchmark 不一定反映业务表现**。一个专注业务的模型，MMLU 可能会降 2-5 个点，这很正常。

### 8.2 业务评估集

必须有一个业务侧的 gold eval 集，100-500 条精心标注的测试样本。跑完用几个维度打分：

- 任务准确率（业务定义）
- 格式符合率（JSON/特定模板）
- 长度合规（太长太短都扣分）
- 敏感信息泄漏率
- 回答相关性

用 GPT-4o 作为 judge 自动打分 + 人工抽检 20%。

### 8.3 回归测试

每个 SFT / DPO 版本训完都过这套 eval，记录成表格追踪。任何一次 eval 分数下降要有可解释的原因。

## 九、完整训练命令和加速

### 9.1 DeepSpeed ZeRO

大模型（70B LoRA）在多卡训练时要用 DeepSpeed ZeRO-2 或 ZeRO-3 切优化器状态。

`ds_z2_config.json`：

```json
{
  "train_batch_size": "auto",
  "train_micro_batch_size_per_gpu": "auto",
  "gradient_accumulation_steps": "auto",
  "gradient_clipping": 1.0,
  "zero_optimization": {
    "stage": 2,
    "offload_optimizer": {
      "device": "cpu",
      "pin_memory": true
    },
    "allgather_partitions": true,
    "allgather_bucket_size": 5e8,
    "overlap_comm": true,
    "reduce_scatter": true,
    "reduce_bucket_size": 5e8,
    "contiguous_gradients": true
  },
  "bf16": { "enabled": "auto" },
  "fp16": { "enabled": "auto" }
}
```

YAML 里加一行：

```yaml
deepspeed: config/ds_z2_config.json
```

- ZeRO-2：优化器状态分片，通用
- ZeRO-3：参数、梯度、优化器都分片，极限省显存，但通信代价大
- offload 到 CPU：再省一层显存，代价是更慢

选型经验：

- 7B~14B LoRA：不用 DeepSpeed
- 30B LoRA：ZeRO-2
- 70B LoRA：ZeRO-2 或 ZeRO-3
- 70B 全参：ZeRO-3 + offload

### 9.2 Unsloth 加速（可选）

LLaMA Factory 0.8+ 集成了 Unsloth 路径（`use_unsloth: true`），单机单卡 LoRA 能再快 30-70%。但仅限特定模型和特定 GPU，踩坑见另一篇。

### 9.3 数据加载优化

`preprocessing_num_workers` 决定 tokenize 并行度，大数据集一定要调。我一般设到 CPU 核心数的 80%。

## 十、踩坑合集

### 坑 1：OOM 不一定是显存不够

遇到 OOM 先检查：

1. `per_device_train_batch_size` 是不是太大
2. `cutoff_len` 是不是超过了实际需要
3. `gradient_checkpointing` 有没有开
4. `flash_attn` 有没有开
5. 有没有意外启了 eval（eval 时显存峰值比 train 高）

全部检查过还是 OOM 再上 ZeRO 或 QLoRA。

### 坑 2：Loss NaN

遇到 loss 变 NaN：

- 检查数据里有没有空 output
- 降低 LR 一半
- 关闭 fp16 改 bf16（硬件支持的话）
- 检查 `gradient_clipping` 是否开启（默认 1.0 一般够）

### 坑 3：template 不对导致模型废了

LLaMA Factory 的 `template` 必须和 base 模型的对话模板一致。`qwen` / `llama3` / `chatml` / `mistral` 不能混。template 错了的症状是模型合并后输出看似正常但行为奇怪，或者干脆不响应。

### 坑 4：dataset_info.json 字段映射错

column mapping 错误会让数据被错误解析成单轮而不是多轮。第一次跑之前一定要 dry run 几条看 tokenize 后的结构：

```python
from llamafactory.data import get_dataset
# 在代码里 import 看前几条 tokenize 后的样子
```

### 坑 5：LoRA adapter 合并时报 shape 不匹配

一般是 base 模型换了（比如把原来的 Qwen 换成 Qwen-Instruct）但训练时 base 是另一个。LoRA adapter 必须对应精确的 base。

### 坑 6：SFT 后模型不会拒答了

原本 base 模型会拒答的敏感问题，微调后开始答了。原因是你的 SFT 数据里都是正常对话，模型把"拒答"这个能力忘了。解决：数据里混入 5-10% 拒答样本。

### 坑 7：DPO 后胡言乱语

beta 太小 + LR 大 + 数据偏差。先把 beta 调大（0.3），LR 减半。如果还是崩，减少 DPO epoch 到 1。

### 坑 8：多卡训练 hang 在启动

和 vLLM 一样的 NCCL 问题，`NCCL_DEBUG=INFO` + `NCCL_SOCKET_IFNAME` 选对网卡。

### 坑 9：cutoff_len 超出模型 max_position

Qwen 默认 32K，但你的 cutoff_len 也不要乱填到 32K，显存立刻爆炸。按**99 分位**数据长度设，不要按最大值。

### 坑 10：训练完 push 到 HF Hub 权限报错

`export_dir` 不要直接指到 HF 目录，先导出到本地再 `huggingface-cli upload`。

## 十一、一个实战经验：迭代节奏

分享我做一个业务模型的迭代节奏，供参考：

**Week 1**：收数据 + 清洗 + 跑 baseline（7B QLoRA，5k 样本，看模型能不能收敛）

**Week 2**：扩数据到 20k + 14B LoRA + 详细 eval + 人工 review badcase

**Week 3**：针对 badcase 补数据 + 再训一版 + DPO 数据标注

**Week 4**：DPO 训练 + 最终 eval + 合并 + 上灰度

**Week 5**：线上效果监控 + 收集反馈数据进下一轮

整个流程大约 1 个月一个版本，稳态后 2 周一版。不要追求一次训出完美模型，分阶段迭代收益更高。

## 十二、上线前 checklist

```
[ ] template 和 base 模型匹配
[ ] LoRA 合并后 safetensors 能被推理引擎加载（vLLM/SGLang 起一次 smoke test）
[ ] tokenizer_config.json 等辅助文件一起合并导出
[ ] 业务 eval 集分数不低于 baseline
[ ] 拒答能力未退化
[ ] 格式合规率 > 99%
[ ] 长文本场景未出现截断（cutoff_len 覆盖 P99）
[ ] 有效 batch size 和 LR 成比例（换卡数后重新计算）
[ ] 训练 log、config、数据 hash 都归档
[ ] 回滚方案：旧版本模型随时能切回
```

## 十三、和其他工具对比

| 维度 | LLaMA Factory | Axolotl | Unsloth | 原生 TRL/PEFT |
|---|---|---|---|---|
| 覆盖方法 | 最全 | 全 | 偏 SFT | 全，需拼装 |
| 模型覆盖 | 最广 | 广 | 主流 | 全 |
| 上手 | 中（YAML） | 中 | 简单 | 难 |
| 速度 | 中 | 中 | 快 | 中 |
| 社区 | 活跃 | 活跃 | 活跃 | 官方 |
| WebUI | ✓ | ✗ | ✗ | ✗ |
| 多机 | ✓ | ✓ | 弱 | 需要自己拼 |

**选择建议**：

- 新手、要跑通流程：LLaMA Factory
- 快速实验单卡 LoRA：Unsloth
- 科研或自定义 pipeline：Axolotl 或原生
- 生产化流程：LLaMA Factory（YAML 易接 CI）

## 十四、收尾

LLaMA Factory 的价值不在于它实现了什么新算法，而在于它把**业界已经验证的微调流程标准化了**。你不用再为 LoRA 挂哪些层、cutoff 怎么设、DPO loss 用哪种发愁——合理的默认值已经铺好，你只需要关心**数据质量**和**业务 eval**。

这两个不是 LLaMA Factory 能帮你解决的，是你自己的事。
