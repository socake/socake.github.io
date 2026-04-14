---
title: "Triton Inference Server 生产部署：模型编排、动态批处理与多框架混部"
date: 2026-03-11T10:00:00+08:00
draft: false
tags: ["Triton", "推理服务", "模型编排", "动态批处理", "NVIDIA"]
categories: ["推理部署"]
description: "把 Triton 从一个陌生的 NVIDIA 推理服务器讲清楚：model repository、backend、动态批处理、ensemble、BLS、Python backend、生产监控和踩坑实录。"
summary: "把 Triton 从一个陌生的 NVIDIA 推理服务器讲清楚：model repository、backend、动态批处理、ensemble、BLS、Python backend、生产监控和踩坑实录。"
toc: true
math: false
diagram: false
keywords: ["Triton Inference Server", "TensorRT", "Dynamic Batching", "Ensemble", "BLS"]
params:
  reading_time: true
---

## 一句话定位 Triton

Triton Inference Server 是 NVIDIA 做的**通用推理服务器**。它不是只给 LLM 用的——CV、NLP、推荐系统、传统机器学习甚至规则模型都能塞进去。它的价值在于把"部署一个模型"这件事从框架里剥离出来，给你一个统一的 HTTP/gRPC 接口、统一的批处理、统一的并发控制、统一的监控指标。

很多团队第一次接触 Triton 是因为要用 TensorRT-LLM（TRT-LLM backend 就是 Triton 上的一个 backend）。但 Triton 本身覆盖的范围远不止 LLM：它支持 TensorRT、ONNX Runtime、PyTorch TorchScript、TensorFlow SavedModel、OpenVINO、Python、FIL（XGBoost/LightGBM/Forest）、DALI 等十来种 backend。多模型混部、流水线编排、A/B 测试这些能力都是现成的。

这篇文章按我实际用 Triton 做一套多模型推理平台的经验来写：核心概念 → model repository → 几个 backend 的实战 → 动态批处理 → ensemble 和 BLS → 监控和踩坑。

## 一、核心架构

```
 ┌──────────────────────────────────────────────┐
 │               Triton Server                  │
 │                                              │
 │  ┌───────────────┐  ┌────────────────────┐   │
 │  │  HTTP (:8000) │  │   gRPC (:8001)     │   │
 │  └───────┬───────┘  └─────────┬──────────┘   │
 │          │                    │              │
 │  ┌───────▼────────────────────▼──────────┐   │
 │  │        Frontend / Scheduler           │   │
 │  │  ┌────────────┐  ┌──────────────────┐ │   │
 │  │  │ Dynamic    │  │ Sequence Batcher │ │   │
 │  │  │ Batcher    │  │                  │ │   │
 │  │  └─────┬──────┘  └────────┬─────────┘ │   │
 │  └────────┼──────────────────┼───────────┘   │
 │           │                  │                │
 │  ┌────────▼──────────────────▼───────────┐   │
 │  │            Backends                   │   │
 │  │ ┌──────────┐ ┌──────────┐ ┌─────────┐ │   │
 │  │ │TensorRT  │ │ONNX RT   │ │Python   │ │   │
 │  │ └──────────┘ └──────────┘ └─────────┘ │   │
 │  │ ┌──────────┐ ┌──────────┐ ┌─────────┐ │   │
 │  │ │TorchScript││ OpenVINO │ │ FIL     │ │   │
 │  │ └──────────┘ └──────────┘ └─────────┘ │   │
 │  │ ┌─────────────────────────┐           │   │
 │  │ │  tensorrtllm (LLM)      │           │   │
 │  │ └─────────────────────────┘           │   │
 │  └───────────────────────────────────────┘   │
 │                                               │
 │  ┌──────────────────────────────────────┐    │
 │  │ Metrics (:8002 Prometheus)           │    │
 │  └──────────────────────────────────────┘    │
 └──────────────────────────────────────────────┘
```

Triton 内部有两个核心抽象：

- **Backend**：真正负责执行模型的运行时。每种框架是一个 backend。
- **Scheduler**：决定请求怎么分到模型的不同 instance 以及怎么组装 batch。

这两个抽象在 Triton 里是**通过配置组合**的，不用改代码。

## 二、Model Repository

Triton 启动的时候要指定 `--model-repository=<path>`。这个目录里的布局直接决定了 Triton 看到哪些模型。

```
model_repository/
├── resnet50_trt/
│   ├── 1/                 # 版本 1
│   │   └── model.plan     # TensorRT engine
│   ├── 2/                 # 版本 2
│   │   └── model.plan
│   └── config.pbtxt       # 模型配置
├── bert_onnx/
│   ├── 1/
│   │   └── model.onnx
│   └── config.pbtxt
├── preprocessing_py/
│   ├── 1/
│   │   └── model.py       # Python backend
│   └── config.pbtxt
└── classification_pipeline/
    ├── 1/                 # ensemble 可以只放一个空目录
    └── config.pbtxt       # ensemble 配置
```

几个规则：

- 一级目录名就是 model_name，API 里用这个名字调用
- 二级目录是版本号，必须是整数
- 每个模型有一个 `config.pbtxt`，用 Protocol Buffer 文本格式写
- 特殊 backend（ensemble/python）有自己的存放约定

### 2.1 最小 config.pbtxt

一个 ResNet50 TensorRT engine 的最小配置：

```
name: "resnet50_trt"
platform: "tensorrt_plan"
max_batch_size: 64

input [
  {
    name: "input"
    data_type: TYPE_FP32
    dims: [3, 224, 224]
    format: FORMAT_NCHW
  }
]

output [
  {
    name: "output"
    data_type: TYPE_FP32
    dims: [1000]
  }
]

instance_group [
  {
    count: 2
    kind: KIND_GPU
    gpus: [0]
  }
]
```

几个关键字段：

| 字段 | 说明 |
|---|---|
| `name` | 模型名，和目录名一致 |
| `platform` 或 `backend` | 决定走哪个 backend |
| `max_batch_size` | 最大 batch，0 表示禁用批处理，非 0 表示输入会多一个 batch 维度 |
| `input/output` | 张量描述，dims 不含 batch 维 |
| `instance_group` | 每个 GPU 上起几个实例 |

### 2.2 版本策略

`config.pbtxt` 里可以加 `version_policy`：

```
version_policy: { latest { num_versions: 1 } }
# 或
version_policy: { all { } }
# 或
version_policy: { specific: { versions: [1, 3] } }
```

生产环境一般用 `latest`，版本切换通过滚动更新目录实现。A/B 测试用 `specific`，业务侧明确指定 version。

### 2.3 动态加载模式

Triton 启动时有三种模型管理模式：

- `--model-control-mode=none`：启动时一次性加载所有模型，之后不变
- `--model-control-mode=poll` + `--repository-poll-secs=30`：轮询目录，文件变了自动 reload
- `--model-control-mode=explicit`：只通过 API 加载/卸载

生产上推荐 **explicit** + 发布系统调用 `POST /v2/repository/models/<name>/load`。poll 模式看着省事，但对共享存储写入原子性有要求，出过事故。

## 三、实战 backend 一：TensorRT

TensorRT engine 是 Triton 上最快的 backend。编译流程和部署分开：

**编译**（在一台有 GPU 的机器上，离线完成）：

```bash
trtexec \
    --onnx=resnet50.onnx \
    --saveEngine=model.plan \
    --fp16 \
    --workspace=2048 \
    --minShapes=input:1x3x224x224 \
    --optShapes=input:32x3x224x224 \
    --maxShapes=input:64x3x224x224
```

**部署**：把 `model.plan` 放到 `resnet50_trt/1/model.plan`，写好 config.pbtxt，Triton 自动加载。

注意：

- **engine 必须在目标 GPU 型号上编译**。H100 编的不能在 A100 跑，反之亦然
- `--fp16` 开启半精度
- dynamic shape 通过 `minShapes/optShapes/maxShapes` 三元组定义

## 四、实战 backend 二：ONNX Runtime

ONNX 是跨框架中间格式。PyTorch、TensorFlow、sklearn、XGBoost 都能导出成 ONNX。ONNX Runtime backend 的好处是**启动快、兼容性好**，代价是比 TensorRT engine 慢一些。

config 示例：

```
name: "bert_onnx"
backend: "onnxruntime"
max_batch_size: 32

input [
  { name: "input_ids", data_type: TYPE_INT64, dims: [-1] },
  { name: "attention_mask", data_type: TYPE_INT64, dims: [-1] }
]

output [
  { name: "logits", data_type: TYPE_FP32, dims: [-1, 2] }
]

optimization {
  execution_accelerators {
    gpu_execution_accelerator : [
      { name : "tensorrt"
        parameters { key: "precision_mode" value: "FP16" }
        parameters { key: "max_workspace_size_bytes" value: "1073741824" }
      }
    ]
  }
}

instance_group [ { count: 2, kind: KIND_GPU } ]
```

关键点是 `execution_accelerators` 里指定 TensorRT 作为 EP（Execution Provider），ONNX 会把能跑 TRT 的 subgraph 自动切下来走 TRT，剩下的走 CUDA EP。这种组合既有 ONNX 的兼容性又有 TRT 的速度，部署折中方案。

## 五、实战 backend 三：Python

Python backend 是 Triton 最灵活的 backend。你写一个 `model.py`，里面定义 `TritonPythonModel` 类，实现 `initialize`、`execute`、`finalize` 三个方法。

用途：

- 前处理 / 后处理（tokenize、图像 resize、特征工程）
- 用 Triton 不直接支持的库（HuggingFace Transformers、sentence-transformers）
- 规则模型、特征加工、A/B 流量分配
- 自己写的复杂逻辑

### 5.1 典型 preprocessing 示例

`preprocessing/1/model.py`：

```python
import json
import numpy as np
import triton_python_backend_utils as pb_utils
from transformers import AutoTokenizer

class TritonPythonModel:
    def initialize(self, args):
        self.model_config = json.loads(args["model_config"])
        tokenizer_dir = self.model_config["parameters"]["tokenizer_dir"]["string_value"]
        self.tokenizer = AutoTokenizer.from_pretrained(tokenizer_dir)

    def execute(self, requests):
        responses = []
        for request in requests:
            text_tensor = pb_utils.get_input_tensor_by_name(request, "QUERY").as_numpy()
            texts = [t.decode("utf-8") for t in text_tensor.flatten()]

            encoded = self.tokenizer(
                texts,
                padding="max_length",
                max_length=512,
                truncation=True,
                return_tensors="np",
            )

            input_ids = pb_utils.Tensor("input_ids", encoded["input_ids"].astype(np.int64))
            attn_mask = pb_utils.Tensor("attention_mask", encoded["attention_mask"].astype(np.int64))

            responses.append(pb_utils.InferenceResponse(output_tensors=[input_ids, attn_mask]))
        return responses

    def finalize(self):
        pass
```

对应的 config.pbtxt：

```
name: "preprocessing"
backend: "python"
max_batch_size: 64

input [
  { name: "QUERY", data_type: TYPE_STRING, dims: [-1] }
]

output [
  { name: "input_ids", data_type: TYPE_INT64, dims: [-1] },
  { name: "attention_mask", data_type: TYPE_INT64, dims: [-1] }
]

parameters [
  { key: "tokenizer_dir", value: { string_value: "/models/bert-base" } }
]

instance_group [ { count: 4, kind: KIND_CPU } ]
```

### 5.2 Python backend 的坑

- Python backend 每个 instance 是一个**独立进程**，通过 shared memory 和 Triton 主进程通信
- `instance_group.count` 数量 = 进程数，CPU backend 可以开多一点
- Python 依赖要在**启动时装好**，不能动态 pip install
- 加载大模型（比如 SentenceTransformer）会让启动变慢，startup probe 要给够时间
- 不支持 tensor 直接在 GPU 上零拷贝传递（0.9+ 加了实验性 DLPack 支持）

## 六、动态批处理

动态批处理是 Triton 最核心的性能优化。原理很简单：把短时间内到达的多个请求合并成一个 batch 送进模型。

### 6.1 基本配置

```
dynamic_batching {
  preferred_batch_size: [ 4, 8, 16, 32 ]
  max_queue_delay_microseconds: 2000
}
```

字段含义：

- `preferred_batch_size`：优先凑够的 batch 大小，支持多个目标
- `max_queue_delay_microseconds`：最多等多久。等到就凑不够也发，过期就发

### 6.2 怎么选参数

这是个吞吐 vs 延迟的权衡：

- `max_queue_delay` 越大，越能凑够大 batch → 吞吐高，延迟高
- `max_queue_delay` 越小 → 延迟好，但小 batch 多吞吐低

经验值：

| 场景 | max_queue_delay | preferred_batch_size |
|---|---|---|
| 实时推理 (P99 < 50ms) | 1000~2000 μs | [4, 8] |
| 一般在线 (P99 < 200ms) | 5000~10000 μs | [8, 16, 32] |
| 离线/准实时 | 20000~50000 μs | [32, 64] |

这套参数要和模型本身的 latency curve 结合起来看——小 batch 和大 batch 在 GPU 上的 latency 不是线性的。一般做一次压测画出 latency vs batch_size 曲线，找拐点。

### 6.3 priority queue

动态批处理器支持优先级队列：

```
dynamic_batching {
  preferred_batch_size: [ 8, 16 ]
  max_queue_delay_microseconds: 5000
  priority_levels: 2
  default_priority_level: 2
  priority_queue_policy {
    key: 1
    value: {
      timeout_action: REJECT
      default_timeout_microseconds: 10000
      allow_timeout_override: true
      max_queue_size: 1000
    }
  }
}
```

priority=1 是高优，默认走 priority=2。业务可以在请求 header 里带 priority 覆盖默认。典型场景：付费用户高优、测试请求低优。

### 6.4 Sequence Batcher

针对**有状态模型**（比如 LSTM、或者多轮对话）的批处理器。它保证同一个 sequence 的多次请求被路由到同一个 instance，同时对多个 sequence 做 batching。

```
sequence_batching {
  max_sequence_idle_microseconds: 5000000
  control_input [
    {
      name: "START"
      control [ { kind: CONTROL_SEQUENCE_START, fp32_false_true: [0, 1] } ]
    },
    {
      name: "READY"
      control [ { kind: CONTROL_SEQUENCE_READY, fp32_false_true: [0, 1] } ]
    }
  ]
}
```

LLM 推理场景里 sequence_batcher 用得少（LLM 的多轮是应用层拼 prompt 解决，不用模型层的 sequence），但传统 RNN 或者视频流分析要用到。

## 七、模型编排：Ensemble

Ensemble 是 Triton 的"模型流水线"。它不是一个真 backend，而是定义多个模型之间的数据流，Triton 内部把请求按 DAG 串起来执行。

```
name: "classification_pipeline"
platform: "ensemble"
max_batch_size: 32

input [
  { name: "IMAGE_BYTES", data_type: TYPE_UINT8, dims: [-1] }
]

output [
  { name: "LABEL", data_type: TYPE_STRING, dims: [1] }
]

ensemble_scheduling {
  step [
    {
      model_name: "image_decode"
      model_version: -1
      input_map { key: "IMAGE_BYTES" value: "IMAGE_BYTES" }
      output_map { key: "IMAGE", value: "decoded_image" }
    },
    {
      model_name: "resnet50_trt"
      model_version: -1
      input_map { key: "input" value: "decoded_image" }
      output_map { key: "output" value: "logits" }
    },
    {
      model_name: "postprocess"
      model_version: -1
      input_map { key: "LOGITS" value: "logits" }
      output_map { key: "LABEL" value: "LABEL" }
    }
  ]
}
```

优势：

- 客户端只调用 ensemble，中间数据不用回客户端
- 每一步都可以独立优化（GPU 预处理、TRT 推理、CPU 后处理）
- 动态批处理在每一步独立生效

劣势：

- 纯顺序 DAG，不能写 if/else 条件分支
- 条件分支要用 BLS（后面讲）

## 八、更复杂的编排：Business Logic Scripting（BLS）

Ensemble 是静态 DAG。BLS 让你在 Python backend 里用代码发起对其他模型的调用，等于在 Triton 内部写编排逻辑。

```python
import triton_python_backend_utils as pb_utils
import numpy as np

class TritonPythonModel:
    def execute(self, requests):
        responses = []
        for request in requests:
            img = pb_utils.get_input_tensor_by_name(request, "IMAGE").as_numpy()

            # 调用检测模型
            det_req = pb_utils.InferenceRequest(
                model_name="detector_trt",
                requested_output_names=["boxes", "scores"],
                inputs=[pb_utils.Tensor("input", img)],
            )
            det_resp = det_req.exec()
            if det_resp.has_error():
                raise pb_utils.TritonModelException(det_resp.error().message())

            boxes = pb_utils.get_output_tensor_by_name(det_resp, "boxes").as_numpy()
            scores = pb_utils.get_output_tensor_by_name(det_resp, "scores").as_numpy()

            # 根据置信度决定要不要跑分类
            if scores.max() > 0.8:
                clf_req = pb_utils.InferenceRequest(
                    model_name="classifier_trt",
                    requested_output_names=["class_id"],
                    inputs=[pb_utils.Tensor("crops", self._crop(img, boxes))],
                )
                clf_resp = clf_req.exec()
                class_id = pb_utils.get_output_tensor_by_name(clf_resp, "class_id").as_numpy()
            else:
                class_id = np.array([-1], dtype=np.int64)

            responses.append(pb_utils.InferenceResponse(
                output_tensors=[pb_utils.Tensor("CLASS_ID", class_id)]
            ))
        return responses
```

BLS 把复杂编排变成 Python 代码，灵活但慢一些。适合：

- 条件跳转（高置信度跳过某些步骤）
- 循环（迭代式检测）
- A/B 流量分配（根据 header 选择不同模型）
- 多模态系统（文本、图像、向量混合）

## 九、部署形态

### 9.1 单 Pod 多模型

最常见。一台 GPU 机器起一个 Triton，模型 repository 里放所有要服务的模型。优点是资源利用率高、运维简单；缺点是模型之间不隔离，一个模型 OOM 会带挂整个 Pod。

### 9.2 每模型一个 Pod

当模型差异大（有的要 H100，有的只要 T4），或者需要独立扩缩容时采用。每个模型单独打 Deployment，前面挂一个自建的网关路由。

### 9.3 混合形态

生产上常见的折中：按**资源画像**分组——大模型各自一个 Pod，小模型捆绑部署。画像指标是显存占用 × QPS × 延迟敏感度。

### 9.4 K8s 部署示例

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: triton-server
spec:
  replicas: 3
  selector:
    matchLabels:
      app: triton
  template:
    metadata:
      labels:
        app: triton
    spec:
      containers:
        - name: triton
          image: nvcr.io/nvidia/tritonserver:24.xx-py3
          args:
            - "tritonserver"
            - "--model-repository=/models"
            - "--model-control-mode=explicit"
            - "--load-model=*"
            - "--strict-model-config=false"
            - "--log-verbose=1"
            - "--exit-on-error=false"
          ports:
            - containerPort: 8000   # HTTP
            - containerPort: 8001   # gRPC
            - containerPort: 8002   # Metrics
          resources:
            limits:
              nvidia.com/gpu: 1
          volumeMounts:
            - name: models
              mountPath: /models
            - name: shm
              mountPath: /dev/shm
          readinessProbe:
            httpGet:
              path: /v2/health/ready
              port: 8000
            periodSeconds: 10
          livenessProbe:
            httpGet:
              path: /v2/health/live
              port: 8000
            periodSeconds: 30
          startupProbe:
            httpGet:
              path: /v2/health/ready
              port: 8000
            periodSeconds: 10
            failureThreshold: 60
      volumes:
        - name: models
          persistentVolumeClaim:
            claimName: triton-models
        - name: shm
          emptyDir:
            medium: Memory
            sizeLimit: 4Gi
```

几个要点：

- `/v2/health/ready` 要求所有模型加载完成才返回 200，作为 readiness 刚好
- `/v2/health/live` 只要 Triton 进程活就行
- `/dev/shm` 必须挂内存盘，Python backend 跨进程用
- `--exit-on-error=false` 防止一个模型加载失败整个 Triton 挂掉，生产必加

## 十、监控指标

Triton 自带 Prometheus endpoint 在 `:8002/metrics`。核心指标：

### 10.1 请求级别

| 指标 | 含义 |
|---|---|
| `nv_inference_request_success` | 成功请求数 |
| `nv_inference_request_failure` | 失败请求数 |
| `nv_inference_count` | 总推理次数（batch 内的每个样本计数） |
| `nv_inference_exec_count` | 实际执行次数（合并 batch 后） |
| `nv_inference_request_duration_us` | 请求总耗时 |
| `nv_inference_queue_duration_us` | 在队列中排队耗时 |
| `nv_inference_compute_input_duration_us` | 输入处理耗时 |
| `nv_inference_compute_infer_duration_us` | 实际推理耗时 |
| `nv_inference_compute_output_duration_us` | 输出处理耗时 |

`count` vs `exec_count` 的比值就是**平均 batch size**，是调优动态批处理的关键指标。理想情况 count/exec_count 接近 preferred_batch_size。

### 10.2 GPU 级别

| 指标 | 含义 |
|---|---|
| `nv_gpu_utilization` | GPU 利用率 |
| `nv_gpu_memory_total_bytes` | 总显存 |
| `nv_gpu_memory_used_bytes` | 已用显存 |
| `nv_gpu_power_usage` | 功耗 |

### 10.3 告警规则示例

- `queue_duration_p95 > 50ms` 持续 5 分钟 → 扩容信号
- `request_failure rate > 1%` → 紧急告警
- `gpu_utilization < 30% && request_count > 0` → 批处理没生效，查 config
- `exec_count / count > 0.8`（batch 平均 < 1.25）→ 批处理失效

## 十一、性能调优要点

### 11.1 instance_group 数量

一个模型在同一个 GPU 上可以起多个 instance，Triton 会并发调度。但这有代价：

- 多 instance 共享 GPU，互相抢占，未必比单 instance 快
- 显存占用翻倍
- 对 TensorRT engine，多 instance 能隐藏一部分 H2D / D2H 传输时间

经验：从 `count=1` 开始，压测看 GPU util，如果 util < 70% 再加 instance，边加边看吞吐提升。到 count=2 或 3 一般就到头了。

### 11.2 rate_limiter

高 QPS 下防止 GPU 被打爆可以用 rate limiter：

```
rate_limiter {
  resources [
    { name: "gpu_memory", count: 1 }
  ]
  priority: 1
}
```

### 11.3 Response Cache

Triton 0.8+ 支持 response cache，对相同输入直接返回缓存结果：

```
response_cache {
  enable: true
}
```

全局 cache 大小由启动参数 `--response-cache-byte-size=1073741824` 控制。

**适用场景极其有限**：纯确定性模型（分类、embedding），不适合 LLM（采样有随机性）。用之前确认输入 hash 的命中率，没命中 cache 反而增加开销。

### 11.4 CUDA Execution Policy

```
optimization {
  cuda {
    graphs: true
    graph_spec { batch_size: 1 }
    graph_spec { batch_size: 8 }
    graph_spec { batch_size: 32 }
    busy_wait_events: true
    output_copy_stream: true
  }
}
```

- `graphs: true` 对固定 batch 的模型开 CUDA Graph
- `busy_wait_events` 让 CUDA event 忙等，延迟微降，CPU 占用上升
- `output_copy_stream` 用独立 stream 做输出拷贝，减少阻塞

## 十二、客户端最佳实践

### 12.1 gRPC vs HTTP

gRPC 性能更好，连接复用、二进制 payload。但调试困难。开发阶段用 HTTP + curl，生产用 gRPC。

### 12.2 Python 客户端

```python
import tritonclient.grpc as grpcclient
import numpy as np

client = grpcclient.InferenceServerClient(url="triton:8001")

inputs = [
    grpcclient.InferInput("input", [1, 3, 224, 224], "FP32"),
]
inputs[0].set_data_from_numpy(image.astype(np.float32))

outputs = [grpcclient.InferRequestedOutput("output")]

result = client.infer(
    model_name="resnet50_trt",
    inputs=inputs,
    outputs=outputs,
    client_timeout=5.0,
    headers={"x-request-id": "abc123"},
)

pred = result.as_numpy("output")
```

### 12.3 Shared Memory 零拷贝

同机部署时客户端和 Triton 共享一段 shm，避免 tensor 在网络上跑：

```python
import tritonclient.utils.shared_memory as shm

shm_handle = shm.create_shared_memory_region("input_data", "/input_data", byte_size)
shm.set_shared_memory_region(shm_handle, [image])

client.register_system_shared_memory("input_data", "/input_data", byte_size)
```

对大 tensor（图像、语音）收益明显。小 tensor 不值得。

## 十三、踩坑合集

### 坑 1：max_batch_size 和 dims 的关系

`max_batch_size > 0` 时 dims 不包含 batch 维，输入张量实际形状是 `[batch, *dims]`。很多新手写了 `max_batch_size: 32` 又在 dims 里写了 batch 维，Triton 一加载就报错维度不匹配。

### 坑 2：Python backend 的 CUDA 初始化

Python backend 里如果用了 PyTorch 或 TensorFlow，默认是每个请求初始化一次 CUDA。把模型加载放到 `initialize()` 里，一次性加载，`execute()` 只做前向。

### 坑 3：ensemble 的批处理维度对不齐

ensemble 各步之间的 tensor 如果 batch 维不一致（比如 detector 一个图出 N 个 box，后面步骤的 batch 不是原来的 batch），Triton 会报错。解决办法：用 Python backend 做 reshape 适配层，或者改用 BLS。

### 坑 4：模型热加载期间请求失败

`model-control-mode=poll` 下模型在重新加载时短暂不可用。生产改 `explicit`，发布走"先加载 v2 → 流量切过去 → 卸载 v1"。

### 坑 5：TensorRT engine 显存不回收

TensorRT engine 即使卸载 Triton 也不一定立刻释放显存，PyTorch 的 CUDA caching allocator 类似毛病。重启 Triton 才干净。设计发布策略时考虑这一点。

### 坑 6：gRPC keepalive 超时

默认 gRPC 连接空闲一段时间会被对端关闭，下次请求要重建连接，首请求抖动。客户端加 keepalive：

```python
client = grpcclient.InferenceServerClient(
    url="triton:8001",
    keepalive_time_ms=30000,
    keepalive_timeout_ms=5000,
    keepalive_permit_without_calls=True,
)
```

### 坑 7：模型文件权限

Kubernetes 里 PVC 挂进容器后文件 owner 是 root，Triton 进程用非 root 用户起就读不到。`fsGroup` 或者 `securityContext.runAsUser` 对齐。

### 坑 8：metrics 指标过多导致 Prometheus 拉取超时

Triton 的指标按 model × version 细粒度打标签，模型多的时候指标上万条。Prometheus scrape_timeout 要给够（10s 以上），或者关一些不需要的指标：

```
--metrics-config=counters=false
```

### 坑 9：Python backend 的 OOM

Python 进程堆外内存不受 Triton 控制，tokenizer 或 opencv 吃掉一两个 GB 很容易，K8s memory limit 要留够。

### 坑 10：shared memory 命名冲突

多个 Triton 实例同机部署用同一个 shm name 会互相干扰。命名加 pod 前缀。

## 十四、选型对比

和其他推理服务器比较：

| 维度 | Triton | TorchServe | KServe | BentoML | Seldon Core |
|---|---|---|---|---|---|
| 主推厂商 | NVIDIA | PyTorch/AWS | Kubeflow | 独立 | 独立 |
| 多框架 | ✓ 多 | PyTorch 为主 | 通过 serving runtime | ✓ | ✓ |
| 动态批处理 | 强 | 有 | 依赖 runtime | 弱 | 依赖 runtime |
| Ensemble/编排 | ✓ | 弱 | ✓ | ✓ | ✓ |
| LLM 专用 | ✓ (tensorrtllm) | 弱 | 依赖 runtime | 较弱 | 较弱 |
| K8s 原生 | 一般 | 一般 | 很好 | 一般 | 很好 |
| 监控 | 很全 | 一般 | 很好 | 一般 | 一般 |

**决策建议**：

- 纯 NVIDIA GPU、性能优先、多模型：Triton
- PyTorch 单一栈、简单需求：TorchServe
- 要 K8s 一等公民、支持多 runtime：KServe（底下可以套 Triton）
- 开发效率和工程化：BentoML

很多公司的栈是 **KServe + Triton**——KServe 做 K8s 层面的 CR 抽象和 autoscaler，Triton 做实际的推理执行。

## 十五、上线 checklist

```
[ ] 模型目录结构正确，config.pbtxt 校验通过
[ ] instance_group 和资源请求匹配
[ ] 动态批处理开启，max_queue_delay 按 SLA 设置
[ ] readiness/liveness/startup probe 齐全
[ ] model-control-mode=explicit，CI 接入加载 API
[ ] /dev/shm 挂内存盘
[ ] Prometheus 接入 + Grafana 大盘
[ ] gRPC 客户端 keepalive 配置
[ ] 压测过 batch vs latency 曲线
[ ] 模型回滚流程演练过
[ ] CUDA driver/runtime 版本对齐 Triton 镜像
[ ] nvidia-cuda-mps（如果开 MPS）配置正确
```

Triton 学习曲线中等，上手后是一个非常结实的推理底座。它的价值随着你部署的模型数量线性增长——部署 1 个模型可能觉得还不如直接写个 FastAPI，部署 20 个模型时你会庆幸早点选了它。
