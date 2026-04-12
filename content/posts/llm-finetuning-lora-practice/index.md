---
title: "LLM 微调入门：LoRA 让大模型适配私有场景"
date: 2026-01-14T09:56:00+08:00
draft: false
tags: ["LoRA", "微调", "QLoRA", "Unsloth", "TRL"]
categories: ["大模型"]
description: "从微调决策到LoRA/QLoRA实战，覆盖数据准备、训练、导出部署完整流程及踩坑记录"
summary: "什么时候该微调、什么时候该用提示工程？本文给出决策框架，然后用Unsloth+QLoRA实战微调Qwen2.5-7B，覆盖数据格式、训练监控、权重合并、部署到vLLM测试，以及10个真实踩坑记录。"
toc: true
math: false
diagram: false
series: ["AI 工程化实战"]
keywords: ["LoRA微调", "QLoRA", "Unsloth", "TRL", "大模型微调", "Qwen2.5"]
params:
  reading_time: true
---

微调不是万灵药，也不总是必要的。先搞清楚要不要微调，再讨论怎么微调。

## 微调 vs 提示工程：决策框架

先问自己几个问题：

**不需要微调的情况：**
- 任务可以通过详细的 system prompt + few-shot 例子解决
- 你有的数据量少于几百条
- 需要快速迭代验证业务逻辑
- 任务需要实时联网或外部工具调用

**应该考虑微调的情况：**
- 需要特定的输出格式/风格，提示工程总是偶尔出错
- 有大量高质量领域数据（1000条以上）
- 推理成本压力大，需要用小模型替换 GPT-4
- 需要把业务知识"内化"到模型，而不是每次都在上下文里塞
- 隐私要求，不能把数据发给第三方 API

**通用建议**：先把提示工程做到极致，如果还是不满足需求，再考虑微调。微调的边际收益在数据质量不够高时往往小于预期。

---

## LoRA 原理（工程视角）

不需要完整推导，但要理解核心思路：

全参数微调要更新模型的所有权重（70亿参数模型约 14GB fp16），显存和计算开销巨大。LoRA 的核心洞察是：微调时的权重变化矩阵 ΔW 是低秩的，可以分解为两个小矩阵的乘积：

```
W_new = W_original + ΔW = W_original + A × B
```

其中 A 是 (d × r) 矩阵，B 是 (r × k) 矩阵，r 是 rank（远小于 d 和 k）。

**实际效果**：7B 模型全参数微调需要约 80GB 显存，LoRA (rank=16) 只需要 16GB，QLoRA（4bit 量化 + LoRA）只需要约 8GB，一张 RTX 4090 就能跑。

**QLoRA** 在 LoRA 基础上用 NF4 格式对基础模型做 4bit 量化，进一步降低显存。质量损失可以接受（通常 < 1%），是目前消费级 GPU 微调的主流方案。

---

## 数据集准备

数据质量 >> 数据数量。500条高质量数据比5000条低质量数据效果好得多。

### 数据格式

主流格式是 ShareGPT 或 Alpaca：

```python
# ShareGPT 格式（推荐，支持多轮对话）
{
    "conversations": [
        {"from": "human", "value": "如何查看K8s Pod的资源使用情况？"},
        {"from": "gpt", "value": "使用kubectl top命令：\n```bash\n# 查看所有Pod资源\nkubectl top pods -n <namespace>\n\n# 查看特定Pod\nkubectl top pod <pod-name> -n <namespace>\n```\n\n如果显示error: Metrics API not available，需要安装metrics-server。"}
    ]
}

# Alpaca 格式（单轮，适合指令微调）
{
    "instruction": "解释以下Kubernetes错误",
    "input": "OOMKilled: container exceeded memory limit",
    "output": "OOMKilled表示容器因超过内存限制被系统强制终止。排查步骤：\n1. 查看容器的内存limit设置...\n2. 用kubectl top确认实际内存使用...\n3. 检查是否有内存泄漏..."
}
```

### 数据质量检查

```python
import json
from pathlib import Path

def validate_dataset(file_path: str):
    data = []
    with open(file_path) as f:
        for line in f:
            data.append(json.loads(line.strip()))

    issues = []
    for i, item in enumerate(data):
        # 检查格式
        if "conversations" not in item:
            issues.append(f"Line {i}: missing conversations field")
            continue

        convs = item["conversations"]

        # 检查长度（太短的回答质量通常差）
        for conv in convs:
            if conv["from"] == "gpt" and len(conv["value"]) < 50:
                issues.append(f"Line {i}: response too short ({len(conv['value'])} chars)")

        # 检查 token 数（超过模型最大长度的样本会被截断）
        total_chars = sum(len(c["value"]) for c in convs)
        if total_chars > 8000:  # 粗略估计，实际要用 tokenizer
            issues.append(f"Line {i}: possibly too long ({total_chars} chars)")

    print(f"Total samples: {len(data)}")
    print(f"Issues found: {len(issues)}")
    for issue in issues[:10]:
        print(f"  - {issue}")

    return len(issues) == 0

validate_dataset("train.jsonl")
```

### 数据量参考

| 任务类型 | 最小数据量 | 推荐数据量 |
|---------|---------|---------|
| 风格/格式对齐 | 200-500 | 1000+ |
| 领域知识注入 | 1000 | 5000+ |
| 特定技能学习 | 500 | 2000+ |
| 聊天机器人人格 | 100-300 | 500+ |

---

## Unsloth + QLoRA 微调实战

Unsloth 是目前最快的微调框架，比原版 HuggingFace 快 2-5x，显存节省 30-70%。

### 环境安装

```bash
# 推荐用 conda 管理环境
conda create -n finetune python=3.11 -y
conda activate finetune

# 安装 Unsloth（会自动匹配 CUDA 版本）
pip install "unsloth[colab-new] @ git+https://github.com/unslothai/unsloth.git"
pip install --no-deps trl peft accelerate bitsandbytes

# 验证
python -c "import unsloth; print(unsloth.__version__)"
```

### 训练脚本

```python
from unsloth import FastLanguageModel
from trl import SFTTrainer
from transformers import TrainingArguments
import torch

# 1. 加载模型（4bit量化）
model, tokenizer = FastLanguageModel.from_pretrained(
    model_name="Qwen/Qwen2.5-7B-Instruct",  # 也可以用本地路径
    max_seq_length=4096,
    dtype=None,           # None 自动检测，通常是 bfloat16
    load_in_4bit=True,    # QLoRA
)

# 2. 添加 LoRA adapter
model = FastLanguageModel.get_peft_model(
    model,
    r=16,                          # rank，常用值：8/16/32/64
    target_modules=[               # 作用于哪些层
        "q_proj", "k_proj", "v_proj", "o_proj",
        "gate_proj", "up_proj", "down_proj"
    ],
    lora_alpha=16,                 # 缩放因子，通常等于r或2*r
    lora_dropout=0.05,
    bias="none",
    use_gradient_checkpointing="unsloth",  # Unsloth 优化的梯度检查点
    random_state=42,
)

print(model.print_trainable_parameters())
# 输出类似：trainable params: 41,943,040 || all params: 7,677,517,824 || trainable%: 0.5462

# 3. 准备数据集
from datasets import load_dataset

# 本地 jsonl 文件
dataset = load_dataset("json", data_files={"train": "train.jsonl"})["train"]

# 格式化为 Qwen 的 chat template
def format_chat(example):
    conversations = example["conversations"]
    text = tokenizer.apply_chat_template(
        [
            {"role": "user" if c["from"] == "human" else "assistant", "content": c["value"]}
            for c in conversations
        ],
        tokenize=False,
        add_generation_prompt=False
    )
    return {"text": text}

dataset = dataset.map(format_chat)

# 4. 配置训练参数
training_args = TrainingArguments(
    output_dir="./checkpoints",
    num_train_epochs=3,
    per_device_train_batch_size=2,
    gradient_accumulation_steps=4,  # 有效 batch size = 2 * 4 = 8
    warmup_steps=50,
    learning_rate=2e-4,
    fp16=not torch.cuda.is_bf16_supported(),
    bf16=torch.cuda.is_bf16_supported(),
    logging_steps=10,
    save_steps=100,
    save_total_limit=3,
    optim="adamw_8bit",            # 8bit 优化器，节省显存
    weight_decay=0.01,
    lr_scheduler_type="cosine",
    seed=42,
    report_to="tensorboard",       # 或 "wandb"
)

trainer = SFTTrainer(
    model=model,
    tokenizer=tokenizer,
    train_dataset=dataset,
    dataset_text_field="text",
    max_seq_length=4096,
    dataset_num_proc=4,
    args=training_args,
)

# 5. 开始训练
trainer.train()
```

### 训练监控

```bash
# 启动 TensorBoard 监控 loss 曲线
tensorboard --logdir ./checkpoints/runs --port 6006

# 关注指标：
# - train/loss：应该稳定下降，最终在 0.5-1.5 范围正常
# - train/learning_rate：余弦衰减曲线
# - train/grad_norm：梯度范数，突然飙升说明有问题
```

**loss 曲线解读**：
- loss 一直不降：学习率太低，或数据格式有问题
- loss 很快降到接近0：过拟合，数据太少或训练轮次太多
- loss 震荡剧烈：学习率太高，调低 learning_rate 或增大 warmup_steps

---

## 权重合并与导出

```python
# 训练结束后合并 LoRA 权重到基础模型
model.save_pretrained_merged(
    "merged_model",
    tokenizer,
    save_method="merged_16bit",  # 合并后保存为fp16
)

# 或者保存为 GGUF 格式（Ollama 使用）
model.save_pretrained_gguf(
    "model_gguf",
    tokenizer,
    quantization_method="q4_k_m"  # 4bit量化，平衡质量和大小
)

# 或者只保存 LoRA adapter（体积小，之后再合并）
model.save_pretrained("lora_adapter")
tokenizer.save_pretrained("lora_adapter")
```

---

## 部署测试

### 用 Ollama 本地测试

```bash
# 从 GGUF 文件创建 Ollama 模型
cat > Modelfile << 'EOF'
FROM ./model_gguf/model-q4_k_m.gguf

SYSTEM "你是一个专业的运维工程师助手，擅长Kubernetes、Linux系统管理和故障排查。"

PARAMETER temperature 0.1
PARAMETER top_p 0.9
EOF

ollama create myops-model -f Modelfile
ollama run myops-model "如何排查K8s Pod CrashLoopBackOff问题？"
```

### 用 vLLM 部署服务

```bash
# 安装 vLLM
pip install vllm

# 启动服务（使用合并后的 fp16 模型）
python -m vllm.entrypoints.openai.api_server \
    --model ./merged_model \
    --port 8000 \
    --gpu-memory-utilization 0.9 \
    --max-model-len 4096 \
    --tensor-parallel-size 1  # 多卡时增大
```

```python
# 测试效果（兼容 OpenAI API）
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8000/v1",
    api_key="dummy"
)

response = client.chat.completions.create(
    model="merged_model",
    messages=[
        {"role": "user", "content": "Pod内存OOM了怎么排查？"}
    ],
    temperature=0.1
)
print(response.choices[0].message.content)
```

---

## 踩坑记录

**坑1：Chat template 格式不对**

不同模型有不同的对话格式（Qwen/Llama/Mistral 各不同），必须用 `tokenizer.apply_chat_template`，不要手动拼字符串。

**坑2：数据中混了"训练集泄露"**

如果测试集问题和训练集高度重叠，评估结果会虚高。评估时要用完全没见过的问题。

**坑3：LoRA rank 选太大收益递减**

rank=64 不一定比 rank=16 好。先用 r=16 建立基线，不满意再调大。

**坑4：忘记设置 pad_token**

```python
# 如果 tokenizer 没有 pad token，训练会报错
if tokenizer.pad_token is None:
    tokenizer.pad_token = tokenizer.eos_token
```

**坑5：gradient_accumulation 和 batch_size 配置混乱**

有效 batch size = per_device_train_batch_size × gradient_accumulation_steps × GPU数量。太小（<4）训练不稳，太大（>64）对于小数据集可能欠拟合。

**坑6：4bit 量化后推理显存估算不准**

QLoRA 训练时显存 ≠ 推理时显存。训练时还需要存优化器状态（2× 参数）。

**坑7：保存 checkpoint 但忘了保存 tokenizer**

合并模型时找不到 tokenizer，要一起保存：`tokenizer.save_pretrained("checkpoint-xxx")`

**坑8：学习率选了默认值 5e-5**

默认学习率是为全参数微调设计的。LoRA 通常用 1e-4 到 3e-4，学习率过低训练很慢。

**坑9：训练集没有随机打乱**

按顺序训练同类数据会导致"灾难性遗忘"。Dataset shuffle 是标配。

**坑10：直接拿 ChatGPT 生成的数据训练**

OpenAI ToS 禁止用其输出训练竞品模型。用开放许可的数据或自己标注。
