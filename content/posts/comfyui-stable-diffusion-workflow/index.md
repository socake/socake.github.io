---
title: "ComfyUI + Stable Diffusion：工作流自动化图像生成"
date: 2026-04-12T13:30:00+08:00
draft: false
tags: ["ComfyUI", "Stable Diffusion", "FLUX", "图像生成", "工作流"]
categories: ["AI工具"]
description: "ComfyUI节点图编程、批量生成API、服务器部署与GPU配置完整实战指南"
summary: "对比SDXL/FLUX/SD3生态选型，讲清楚ComfyUI vs WebUI如何选，然后深入ComfyUI安装、节点图工作流设计、常用节点配置，重点讲API无头调用和服务器端批量生成部署方案。"
toc: true
math: false
diagram: false
keywords: ["ComfyUI", "Stable Diffusion", "FLUX", "SDXL", "图像生成", "API自动化"]
params:
  reading_time: true
---

Stable Diffusion 生态在 2024 年经历了大洗牌——FLUX 的出现让很多老工作流需要重新设计。本文以工程视角讲清楚现在该用什么、怎么部署、怎么通过 API 自动化调用。

## SD 生态现状（2024-2025）

### 主流基础模型对比

| 模型 | 出图质量 | 速度 | 显存需求 | 适合场景 |
|------|---------|------|---------|---------|
| **FLUX.1 [dev]** | 极强 | 较慢 | 16GB+ | 高质量写实/艺术 |
| **FLUX.1 [schnell]** | 强 | 快（4步） | 12GB+ | 快速生成/测试 |
| **SDXL** | 强 | 中 | 8GB+ | 成熟生态/LoRA丰富 |
| **SD 1.5** | 中 | 快 | 4GB+ | 低显存/大量微调模型 |
| **SD3 Medium** | 强 | 中 | 8GB+ | 文字渲染强 |

**现在的选型建议**：
- 追求质量/写实人像：FLUX.1 dev（需要 16GB 显存）
- 快速迭代/批量生成：FLUX.1 schnell 或 SDXL
- 低显存机器（8GB以下）：SDXL 或 SD 1.5 配合 fp8/fp16 量化
- 中文文字渲染需求：SD3 或带 OCR 后处理的方案

---

## ComfyUI vs WebUI 选型

**AUTOMATIC1111 WebUI**：
- 优点：界面友好、上手快、插件多
- 缺点：更新慢、架构老、不原生支持 FLUX、批量生产难

**ComfyUI**：
- 优点：节点图架构灵活、原生支持所有新模型、API 调用标准化、工作流可版本控制
- 缺点：学习曲线陡，纯 UI 操作比 WebUI 复杂

**结论**：如果只是个人偶尔用用，WebUI 够了。如果要自动化、批量生成、集成到业务系统，选 ComfyUI。本文专注 ComfyUI。

---

## ComfyUI 安装

### 方式一：本地安装

```bash
# 前提：已安装 CUDA 12.x 和 Python 3.11+
git clone https://github.com/comfyanonymous/ComfyUI.git
cd ComfyUI

# 安装依赖（CUDA版本）
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121
pip install -r requirements.txt

# 启动（默认监听 0.0.0.0:8188）
python main.py --listen 0.0.0.0 --port 8188
```

### 方式二：Docker 部署（服务器推荐）

```dockerfile
# Dockerfile
FROM nvidia/cuda:12.1.0-cudnn8-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y \
    python3.11 python3-pip git wget \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
RUN git clone https://github.com/comfyanonymous/ComfyUI.git .

RUN pip3 install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121
RUN pip3 install -r requirements.txt

# 安装常用自定义节点
RUN git clone https://github.com/ltdrdata/ComfyUI-Manager.git custom_nodes/ComfyUI-Manager
RUN pip3 install -r custom_nodes/ComfyUI-Manager/requirements.txt

EXPOSE 8188

CMD ["python3", "main.py", "--listen", "0.0.0.0", "--port", "8188"]
```

```yaml
# docker-compose.yml
version: "3.8"
services:
  comfyui:
    build: .
    ports:
      - "8188:8188"
    volumes:
      - ./models:/app/models          # checkpoint/lora等模型文件
      - ./output:/app/output          # 生成图片输出
      - ./custom_nodes:/app/custom_nodes  # 自定义节点
      - ./workflows:/app/workflows    # 工作流文件（可选）
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
    environment:
      - NVIDIA_VISIBLE_DEVICES=all
```

```bash
# 启动
docker-compose up -d

# 查看日志
docker-compose logs -f comfyui
```

### 模型文件放置

```bash
models/
├── checkpoints/    # 基础模型（.safetensors）
│   ├── flux1-dev.safetensors
│   └── sd_xl_base_1.0.safetensors
├── loras/          # LoRA 文件
├── vae/            # VAE 文件
│   └── ae.safetensors   # FLUX 专用 VAE
├── clip/           # CLIP 文本编码器
│   ├── clip_l.safetensors
│   └── t5xxl_fp16.safetensors  # FLUX 需要
└── unet/           # FLUX 独立 UNet
    └── flux1-dev.safetensors
```

---

## 基础工作流：节点图编程思路

ComfyUI 的核心是**数据流图**：每个节点有输入和输出端口，通过连线传递数据。

### SDXL 基础文生图工作流

标准工作流的节点连接顺序：

```
[CheckpointLoaderSimple] → (MODEL, CLIP, VAE)
        ↓ CLIP                    ↓ VAE
[CLIPTextEncode(正向提示)] → CONDITIONING    [EmptyLatentImage] → LATENT
[CLIPTextEncode(负向提示)] → CONDITIONING         ↓
                                        [KSampler] ← MODEL
                                            ↓ LATENT
                                        [VAEDecode] ← VAE
                                            ↓ IMAGE
                                        [SaveImage]
```

对应的 API JSON 格式（工作流的核心是一个节点字典）：

```json
{
  "1": {
    "class_type": "CheckpointLoaderSimple",
    "inputs": {
      "ckpt_name": "sd_xl_base_1.0.safetensors"
    }
  },
  "2": {
    "class_type": "CLIPTextEncode",
    "inputs": {
      "text": "a photo of a cat sitting on a rooftop at sunset, photorealistic, detailed",
      "clip": ["1", 1]
    }
  },
  "3": {
    "class_type": "CLIPTextEncode",
    "inputs": {
      "text": "blurry, bad quality, watermark, nsfw",
      "clip": ["1", 1]
    }
  },
  "4": {
    "class_type": "EmptyLatentImage",
    "inputs": {
      "width": 1024,
      "height": 1024,
      "batch_size": 1
    }
  },
  "5": {
    "class_type": "KSampler",
    "inputs": {
      "model": ["1", 0],
      "positive": ["2", 0],
      "negative": ["3", 0],
      "latent_image": ["4", 0],
      "seed": 42,
      "steps": 20,
      "cfg": 7.0,
      "sampler_name": "euler",
      "scheduler": "normal",
      "denoise": 1.0
    }
  },
  "6": {
    "class_type": "VAEDecode",
    "inputs": {
      "samples": ["5", 0],
      "vae": ["1", 2]
    }
  },
  "7": {
    "class_type": "SaveImage",
    "inputs": {
      "images": ["6", 0],
      "filename_prefix": "output"
    }
  }
}
```

### 关键节点详解

**KSampler 参数**：
- `steps`：采样步数，SDXL 推荐 20-30，FLUX schnell 只需 4
- `cfg`：分类器自由引导强度，越高越听 prompt 但可能过饱和；SDXL 用 7-8，FLUX 用 1-3.5
- `sampler_name`：常用 `euler`、`dpm_2_ancestral`；FLUX 用 `euler`
- `scheduler`：常用 `normal`、`karras`；FLUX 用 `beta`

**LoRA 加载节点**：

```json
{
  "8": {
    "class_type": "LoraLoader",
    "inputs": {
      "model": ["1", 0],
      "clip": ["1", 1],
      "lora_name": "detail_enhancer.safetensors",
      "strength_model": 0.8,
      "strength_clip": 0.8
    }
  }
}
```

加了 LoRA 后，把原来连 `["1", 0]` 的地方换成 `["8", 0]`（model），`["1", 1]` 换成 `["8", 1]`（clip）。

---

## API 模式：无头调用与批量生成

这是 ComfyUI 最有价值的能力——通过 WebSocket + HTTP API 实现完全自动化。

### Python 客户端封装

```python
import json
import uuid
import websocket
import httpx
from pathlib import Path

class ComfyUIClient:
    def __init__(self, host: str = "localhost", port: int = 8188):
        self.base_url = f"http://{host}:{port}"
        self.ws_url = f"ws://{host}:{port}/ws"
        self.client_id = str(uuid.uuid4())

    def queue_prompt(self, workflow: dict) -> str:
        """提交工作流到队列，返回 prompt_id"""
        payload = {
            "prompt": workflow,
            "client_id": self.client_id
        }
        response = httpx.post(
            f"{self.base_url}/prompt",
            json=payload,
            timeout=30
        )
        response.raise_for_status()
        return response.json()["prompt_id"]

    def wait_for_completion(self, prompt_id: str) -> dict:
        """等待任务完成，返回输出信息"""
        ws = websocket.WebSocket()
        ws.connect(f"{self.ws_url}?clientId={self.client_id}")

        try:
            while True:
                out = ws.recv()
                if isinstance(out, str):
                    message = json.loads(out)
                    if message["type"] == "executing":
                        data = message["data"]
                        if data["node"] is None and data["prompt_id"] == prompt_id:
                            break  # 执行完成
        finally:
            ws.close()

        return self.get_history(prompt_id)

    def get_history(self, prompt_id: str) -> dict:
        response = httpx.get(f"{self.base_url}/history/{prompt_id}")
        return response.json()[prompt_id]

    def get_output_images(self, history: dict) -> list[bytes]:
        """从历史记录中获取生成的图片"""
        images = []
        for node_id, node_output in history["outputs"].items():
            if "images" in node_output:
                for img_info in node_output["images"]:
                    params = {
                        "filename": img_info["filename"],
                        "subfolder": img_info["subfolder"],
                        "type": img_info["type"]
                    }
                    response = httpx.get(
                        f"{self.base_url}/view",
                        params=params,
                        timeout=30
                    )
                    images.append(response.content)
        return images

    def generate(
        self,
        workflow: dict,
        output_dir: str = "./outputs"
    ) -> list[str]:
        """提交工作流并等待生成，返回保存的文件路径"""
        prompt_id = self.queue_prompt(workflow)
        print(f"Queued: {prompt_id}")

        history = self.wait_for_completion(prompt_id)
        images = self.get_output_images(history)

        Path(output_dir).mkdir(parents=True, exist_ok=True)
        saved_paths = []
        for i, img_data in enumerate(images):
            path = f"{output_dir}/{prompt_id}_{i}.png"
            with open(path, "wb") as f:
                f.write(img_data)
            saved_paths.append(path)
            print(f"Saved: {path}")

        return saved_paths
```

### 批量生成

```python
import copy
import random

def batch_generate(
    client: ComfyUIClient,
    base_workflow: dict,
    prompts: list[str],
    output_dir: str = "./batch_output"
) -> list[str]:
    """批量生成，每个 prompt 对应一张图"""
    all_outputs = []

    # 找到 positive prompt 节点
    # （根据你的工作流调整节点 ID）
    POSITIVE_NODE_ID = "2"
    KSAMPLER_NODE_ID = "5"

    for i, prompt_text in enumerate(prompts):
        workflow = copy.deepcopy(base_workflow)

        # 修改提示词
        workflow[POSITIVE_NODE_ID]["inputs"]["text"] = prompt_text

        # 随机种子，每张图不同
        workflow[KSAMPLER_NODE_ID]["inputs"]["seed"] = random.randint(0, 2**32)

        print(f"Generating {i+1}/{len(prompts)}: {prompt_text[:50]}...")

        paths = client.generate(workflow, output_dir)
        all_outputs.extend(paths)

    return all_outputs

# 使用
client = ComfyUIClient(host="your-server", port=8188)

# 加载基础工作流
with open("base_workflow.json") as f:
    base_wf = json.load(f)

prompts = [
    "a serene mountain lake at dawn, mist over water, photorealistic",
    "cyberpunk city street at night, neon lights, rain reflection",
    "ancient japanese temple in autumn forest, detailed architecture",
]

outputs = batch_generate(client, base_wf, prompts)
print(f"Generated {len(outputs)} images")
```

---

## 服务器部署与 GPU 配置

### 多 GPU 配置

```bash
# 单 GPU 指定
CUDA_VISIBLE_DEVICES=0 python main.py --listen 0.0.0.0

# ComfyUI 不原生支持多 GPU 并行（单工作流），
# 多 GPU 要跑多个实例，用 nginx 做负载均衡

# GPU 0 实例（端口8188）
CUDA_VISIBLE_DEVICES=0 python main.py --listen 0.0.0.0 --port 8188

# GPU 1 实例（端口8189）
CUDA_VISIBLE_DEVICES=1 python main.py --listen 0.0.0.0 --port 8189
```

```nginx
# nginx 负载均衡配置
upstream comfyui_backends {
    least_conn;  # 最少连接数，适合长任务
    server 127.0.0.1:8188;
    server 127.0.0.1:8189;
}

server {
    listen 80;
    
    # WebSocket 支持
    location /ws {
        proxy_pass http://comfyui_backends;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 3600;
    }

    location / {
        proxy_pass http://comfyui_backends;
        proxy_read_timeout 300;
        client_max_body_size 100M;
    }
}
```

### 显存优化启动参数

```bash
# 低显存模式（6-8GB GPU）
python main.py \
    --listen 0.0.0.0 \
    --lowvram \            # 激进省显存，速度慢
    --fp8_e4m3fn-unet \   # UNet 用 fp8，节省约50%显存

# 中等显存（10-12GB GPU）  
python main.py \
    --listen 0.0.0.0 \
    --medvram \            # 中等省显存，平衡速度
    --fp16-vae             # VAE 用 fp16

# 高显存（16GB+）
python main.py \
    --listen 0.0.0.0 \
    --highvram             # 全部常驻显存，最快
```

### 生产监控

```python
def get_comfyui_status(client: ComfyUIClient) -> dict:
    """获取队列状态和系统信息"""
    queue_resp = httpx.get(f"{client.base_url}/queue")
    system_resp = httpx.get(f"{client.base_url}/system_stats")

    queue_data = queue_resp.json()
    system_data = system_resp.json()

    return {
        "queue_running": len(queue_data.get("queue_running", [])),
        "queue_pending": len(queue_data.get("queue_pending", [])),
        "gpu_vram_free": system_data.get("devices", [{}])[0].get("vram_free", 0),
        "gpu_vram_total": system_data.get("devices", [{}])[0].get("vram_total", 0),
    }
```

---

## 常见问题

**CUDA out of memory**：降低 batch_size 为 1，或用 `--lowvram` 启动，或把图片分辨率降到 512/768。

**模型加载很慢**：模型文件在 HDD 上速度慢，换 SSD。大模型第一次加载缓存到显存后之后就快了。

**FLUX 出图质量差**：FLUX 对 CFG scale 非常敏感，dev 版本用 1.0-3.5，不要用高 CFG（7-8）；步数至少 20，schnell 版本 4 步即可。

**WebSocket 连接断开**：长时间生成时 nginx 代理超时，调大 `proxy_read_timeout`（至少 600s）。
