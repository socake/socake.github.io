---
title: "Ollama 在 K8s 上跑大模型：本地 LLM 的运维实践"
date: 2026-03-30T09:08:00+08:00
draft: false
tags: ["Ollama", "LLM", "Kubernetes", "AI", "GPU", "运维"]
categories: ["AIOPS"]
description: "在 Kubernetes 上部署 Ollama 运行本地大模型，从 GPU 调度到 CPU 推理降级，再到运维场景的实际集成，记录完整的踩坑与实践过程。"
summary: "在 Kubernetes 上部署 Ollama 运行本地大模型，从 GPU 调度到 CPU 推理降级，再到运维场景的实际集成，记录完整的踩坑与实践过程。"
toc: true
math: false
diagram: false
series: ["AI 工程化实战"]
keywords: ["Ollama", "LLM", "Kubernetes", "GPU", "本地大模型", "AI运维"]
params:
  reading_time: true
---

## 为什么要在本地跑大模型

去年团队开始大量引入 AI 工具来辅助运维工作，最初全部走云端 API——OpenAI、Claude、通义千问轮流用。但很快就碰到了几个让人难受的问题：

**日志数据不敢发出去。** 线上日志里夹着用户 ID、内部服务地址、甚至偶尔有 token 信息。把这些直接喂给第三方 API，合规审计那边就会来找麻烦。

**延迟不稳定。** 用 Claude API 分析一段错误日志，快的时候 2 秒出结果，慢的时候 15 秒都没响应。在 PagerDuty 告警响应链路里这种抖动完全不可接受。

**成本随用量线性增长。** 批量分析场景下，一个月的 token 消耗能让财务找你谈话。

本地 LLM 能解决这三个问题——数据不出境、延迟可控、固定成本（算力折旧）。Ollama 是目前本地部署 LLM 体验最好的方案，支持 Llama 3、DeepSeek-R2、Qwen3、Gemma3 等主流模型，一条命令拉起，REST API 简洁，镜像也有官方维护。

## Ollama 是什么

Ollama 本质上是一个模型运行时 + HTTP Server 的封装。它做了这几件事：

- 统一模型格式（GGUF），屏蔽底层推理引擎细节
- 自动管理模型文件下载、缓存、版本
- 提供兼容 OpenAI 格式的 REST API
- 支持 CUDA / Metal / CPU 多后端推理

从使用者角度看，Ollama 就是一个你本地起的"私有 OpenAI 接口"。现有调用 OpenAI API 的代码，改一下 base_url 就能切过来。

## 在 K8s 上部署 Ollama

### 前置条件

集群里需要装好 GPU 驱动和 NVIDIA Device Plugin（如果要用 GPU）：

```bash
# 确认 node 上的 GPU 资源已注册
kubectl get nodes -o json | jq '.items[].status.allocatable | select(."nvidia.com/gpu")'
```

### 模型存储 PVC

模型文件普遍比较大，Llama3-8B 量化版约 5GB，DeepSeek-R1-70B 量化版接近 40GB。必须用 PVC 持久化，否则 Pod 重启就要重新拉模型，Ollama 镜像启动时会从 ollama.com 下载，在国内网络环境下体验很糟糕。

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ollama-models
  namespace: ai-ops
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: gp3
  resources:
    requests:
      storage: 100Gi
```

Storage Class 选 gp3 或者本地 SSD，模型推理对磁盘 IO 有一定要求，机械盘的 IOPS 会成为瓶颈。

### Deployment 配置（GPU 模式）

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ollama
  namespace: ai-ops
spec:
  replicas: 1
  selector:
    matchLabels:
      app: ollama
  template:
    metadata:
      labels:
        app: ollama
    spec:
      nodeSelector:
        nvidia.com/gpu.present: "true"
      tolerations:
        - key: nvidia.com/gpu
          operator: Exists
          effect: NoSchedule
      containers:
        - name: ollama
          image: ollama/ollama:0.6.5
          ports:
            - containerPort: 11434
          env:
            - name: OLLAMA_MODELS
              value: /models
            - name: OLLAMA_NUM_PARALLEL
              value: "2"
            - name: OLLAMA_MAX_LOADED_MODELS
              value: "1"
          resources:
            limits:
              nvidia.com/gpu: "1"
              memory: "16Gi"
            requests:
              nvidia.com/gpu: "1"
              memory: "8Gi"
          volumeMounts:
            - name: models
              mountPath: /models
          livenessProbe:
            httpGet:
              path: /api/tags
              port: 11434
            initialDelaySeconds: 30
            periodSeconds: 10
      volumes:
        - name: models
          persistentVolumeClaim:
            claimName: ollama-models
---
apiVersion: v1
kind: Service
metadata:
  name: ollama
  namespace: ai-ops
spec:
  selector:
    app: ollama
  ports:
    - port: 11434
      targetPort: 11434
  type: ClusterIP
```

`OLLAMA_NUM_PARALLEL` 控制并发请求数，一张 GPU 通常设 2 就够，设太高会 OOM。`OLLAMA_MAX_LOADED_MODELS` 控制同时加载的模型数，显存有限时保持 1。

### 初始化模型

Pod 起来后，exec 进去拉模型：

```bash
kubectl exec -it -n ai-ops deploy/ollama -- ollama pull qwen2.5:7b-instruct-q4_K_M
kubectl exec -it -n ai-ops deploy/ollama -- ollama pull llama3.1:8b-instruct-q5_K_M
```

或者用 initContainer 来自动化这个过程：

```yaml
initContainers:
  - name: pull-models
    image: ollama/ollama:0.6.5
    command:
      - sh
      - -c
      - |
        ollama serve &
        sleep 5
        ollama pull qwen2.5:7b-instruct-q4_K_M
        wait
    env:
      - name: OLLAMA_MODELS
        value: /models
    volumeMounts:
      - name: models
        mountPath: /models
```

## 无 GPU 时的 CPU 推理

团队不是所有集群都有 GPU 节点，开发测试环境跑 CPU 推理完全可以接受。

Ollama 支持 CPU-only 模式，不需要任何额外配置，只要去掉 GPU 相关的 `resources.limits` 和 `nodeSelector` 就行。

关键是选对模型量化版本：

| 量化级别 | 文件大小（7B 模型） | 速度 | 质量 |
|---------|------------------|------|------|
| Q2_K | ~2.7GB | 最快 | 明显下降 |
| Q4_K_M | ~4.1GB | 中等 | 推荐 |
| Q5_K_M | ~4.8GB | 略慢 | 接近 FP16 |
| Q8_0 | ~7.7GB | 慢 | 最佳 |

CPU 推理实测（8 核 16GB 内存，Qwen2.5-7B-Q4_K_M）：
- 首 token 延迟：约 3 秒
- 生成速度：约 8-12 tokens/s
- 适合做异步分析，不适合实时对话

对于日志分析这类场景，CPU 推理完全够用——你扔进去一段错误日志，等个 30 秒出结果，比人肉看日志快多了。

## Ollama REST API 使用

### /api/generate

最基础的生成接口，单轮对话：

```bash
curl http://ollama.ai-ops.svc.cluster.local:11434/api/generate \
  -d '{
    "model": "qwen2.5:7b-instruct-q4_K_M",
    "prompt": "以下是一段 K8s 错误日志，请分析根因：\nOOMKilled: container exceeded memory limit",
    "stream": false,
    "options": {
      "temperature": 0.1,
      "num_predict": 512
    }
  }'
```

`temperature` 调低（0.1-0.3），运维分析场景要确定性输出，不需要创意。

### /api/chat

多轮对话接口，格式兼容 OpenAI：

```bash
curl http://ollama.ai-ops.svc.cluster.local:11434/api/chat \
  -d '{
    "model": "qwen2.5:7b-instruct-q4_K_M",
    "messages": [
      {
        "role": "system",
        "content": "你是一个 SRE 专家，负责分析 Kubernetes 集群问题。"
      },
      {
        "role": "user",
        "content": "Pod 频繁 CrashLoopBackOff，日志显示 connection refused，可能是什么原因？"
      }
    ],
    "stream": false
  }'
```

## 运维场景集成：日志异常分析

这是我们团队实际在用的脚本，每当 Alertmanager 触发 P2 告警，自动拉取相关 Pod 日志送给本地 Ollama 分析：

```python
import httpx
import subprocess
import json
from datetime import datetime, timedelta

OLLAMA_URL = "http://ollama.ai-ops.svc.cluster.local:11434"
MODEL = "qwen2.5:7b-instruct-q4_K_M"

SYSTEM_PROMPT = """你是一个 SRE 专家。分析给定的 Kubernetes Pod 日志，输出：
1. 错误类型（一句话）
2. 可能根因（2-3 条）
3. 建议操作（具体命令或步骤）
输出保持简洁，使用中文。"""

def get_pod_logs(namespace: str, pod_name: str, lines: int = 100) -> str:
    result = subprocess.run(
        ["kubectl", "logs", pod_name, "-n", namespace,
         "--tail", str(lines), "--previous"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        # 没有 previous 容器时去掉 --previous
        result = subprocess.run(
            ["kubectl", "logs", pod_name, "-n", namespace, "--tail", str(lines)],
            capture_output=True, text=True
        )
    return result.stdout

def analyze_logs(logs: str, pod_name: str) -> str:
    prompt = f"Pod 名称: {pod_name}\n\n日志内容:\n{logs}"

    response = httpx.post(
        f"{OLLAMA_URL}/api/chat",
        json={
            "model": MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt}
            ],
            "stream": False,
            "options": {"temperature": 0.1, "num_predict": 1024}
        },
        timeout=120.0
    )
    response.raise_for_status()
    return response.json()["message"]["content"]

def handle_alert(namespace: str, pod_name: str):
    print(f"[{datetime.now()}] 开始分析 {namespace}/{pod_name}")
    logs = get_pod_logs(namespace, pod_name)
    if not logs.strip():
        print("日志为空，跳过分析")
        return
    analysis = analyze_logs(logs, pod_name)
    print(f"\n=== AI 分析结果 ===\n{analysis}\n")
    # 可以进一步推送到 Slack/钉钉
    return analysis

if __name__ == "__main__":
    handle_alert("production", "api-server-7d4b8c9f6-xk2pq")
```

把这个脚本集成到告警 webhook 里，P2 告警触发时自动分析，结果附在告警消息里推送给 oncall。减少了 oncall 工程师的初步排查时间，大概能省 5-10 分钟的日志翻找。

## 部署 OpenWebUI

给团队一个可视化界面，不用每次都写 curl：

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: open-webui
  namespace: ai-ops
spec:
  replicas: 1
  selector:
    matchLabels:
      app: open-webui
  template:
    metadata:
      labels:
        app: open-webui
    spec:
      containers:
        - name: open-webui
          image: ghcr.io/open-webui/open-webui:v0.6.5
          ports:
            - containerPort: 8080
          env:
            - name: OLLAMA_BASE_URL
              value: http://ollama:11434
            - name: WEBUI_SECRET_KEY
              valueFrom:
                secretKeyRef:
                  name: open-webui-secret
                  key: secret-key
          volumeMounts:
            - name: data
              mountPath: /app/backend/data
      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: open-webui-data
```

通过 Ingress 或 Gateway API 暴露给内网，限制访问来源 IP，用 SSO 做认证（OpenWebUI 支持 OAuth2）。

## 踩过的坑

**GPU 不足时的降级问题。** 有一次 GPU 节点维护，Pod 调度到了 CPU 节点，但没有设置 nodeSelector，结果模型加载成功了，只是推理速度慢了 10 倍。现在我们分别部署两个 Deployment——一个 GPU 版本，一个 CPU 版本，通过 Service 层做路由，GPU 不可用时自动降级到 CPU。

**模型存储空间规划。** 早期 PVC 只申请了 20GB，跑了几个模型就满了，扩容 PVC 还得重建 Pod。现在一开始就申请 100GB，gp3 按用量计费，不用心疼。

**并发请求限制。** Ollama 默认并发是 1（串行处理），`OLLAMA_NUM_PARALLEL` 设太高会 OOM。我们的场景是异步分析，不需要高并发，保持默认就行。如果需要高并发，应该考虑多副本 + 负载均衡，而不是单实例加并发数。

**模型预热。** Pod 重启后第一次请求会有模型加载时间（几秒到几十秒不等），在 liveness probe 里加了 `initialDelaySeconds: 30`，同时用一个轻量 CronJob 每隔 5 分钟发一次空请求保持模型热加载状态。

本地 LLM 不是要替代云端 API，而是针对特定场景（敏感数据、批量分析、成本敏感）提供更合适的选项。Ollama + K8s 这套组合跑起来之后，我们的运维 AI 辅助能力覆盖面明显扩大了，以前不敢送出去的数据现在都能用上。
