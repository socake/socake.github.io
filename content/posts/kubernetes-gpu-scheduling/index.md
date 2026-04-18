---
title: "Kubernetes GPU 调度实战：AI 训练与推理基础设施"
date: 2025-11-05T14:00:00+08:00
draft: false
tags: ["Kubernetes", "GPU", "AI基础设施", "NVIDIA", "深度学习"]
categories: ["Kubernetes"]
description: "系统梳理 Kubernetes GPU 支持的完整架构，覆盖 Device Plugin、GPU Operator、MIG/vGPU 共享方案、Karpenter 弹性节点池、DCGM 监控体系、训练与推理调度优化，以及成本控制策略"
summary: "GPU 是 AI 基础设施的核心资源，如何在 Kubernetes 上高效调度和管理 GPU 直接影响训练效率和推理成本。本文从底层驱动安装到上层调度策略，完整覆盖 K8s GPU 基础设施的搭建、监控和优化实践。"
toc: true
math: false
diagram: false
keywords: ["Kubernetes", "GPU调度", "NVIDIA Device Plugin", "GPU Operator", "MIG", "DCGM", "Triton", "Karpenter", "GPU共享", "AI训练"]
params:
  reading_time: true
---

一张 A100 每月云上租用成本超过 2000 美元，但我们接手过的集群里，GPU 利用率常年不到 30%。Kubernetes 给了管理 GPU 的基础框架，但默认配置远远不够——驱动、device plugin、MIG、监控、调度策略每一层都有坑。这篇把我在这套栈上踩过的东西写下来。

## K8s GPU 支持架构

### 整体技术栈

```
应用层：PyTorch / TensorFlow / Triton Inference Server
   ↕
K8s 调度层：Device Plugin + 资源请求
   ↕
运行时层：nvidia-container-toolkit（容器化 GPU 访问）
   ↕
驱动层：NVIDIA Driver + CUDA
   ↕
硬件层：GPU（A100/H100/V100/T4 等）
```

理解这个分层结构很重要。上层出问题，优先看资源请求和 Device Plugin；下层出问题，优先看驱动和容器运行时。

### NVIDIA Device Plugin

Device Plugin 是 K8s 的扩展机制，允许第三方硬件厂商将设备资源（如 GPU）暴露给 K8s 调度器。NVIDIA Device Plugin 以 DaemonSet 形式运行在每个 GPU 节点上：

- 定期向 kubelet 上报本节点 GPU 数量
- 在 Pod 调度时，将 GPU 设备文件（`/dev/nvidia*`）挂载到容器
- 管理 GPU 设备的分配和释放

**安装 NVIDIA Device Plugin：**

```bash
kubectl apply -f https://raw.githubusercontent.com/NVIDIA/k8s-device-plugin/v0.14.5/nvidia-device-plugin.yml
```

或通过 Helm（推荐，可定制配置）：

```bash
helm repo add nvdp https://nvidia.github.io/k8s-device-plugin
helm repo update

helm install nvdp nvdp/nvidia-device-plugin \
  --namespace nvidia-device-plugin \
  --create-namespace \
  --version 0.14.5 \
  --set failOnInitError=false \
  --set compatWithCPUManager=true
```

安装成功后，节点会出现 `nvidia.com/gpu` 资源：

```bash
kubectl describe node gpu-node-1 | grep nvidia
# Capacity:
#   nvidia.com/gpu: 8
# Allocatable:
#   nvidia.com/gpu: 8
```

### GPU Operator：一站式 GPU 管理

NVIDIA GPU Operator 是更完整的解决方案，自动管理 GPU 驱动、Device Plugin、容器运行时、DCGM 监控等所有组件的安装和升级：

```bash
helm repo add nvidia https://helm.ngc.nvidia.com/nvidia
helm repo update

helm install gpu-operator nvidia/gpu-operator \
  --namespace gpu-operator \
  --create-namespace \
  --set driver.enabled=true \
  --set driver.version="550.54.15" \
  --set toolkit.enabled=true \
  --set devicePlugin.enabled=true \
  --set dcgm.enabled=true \
  --set dcgmExporter.enabled=true \
  --set mig.strategy=mixed
```

GPU Operator 的核心优势：节点不需要预装驱动，Operator 会自动探测 GPU 型号，下载并安装对应驱动。这对弹性伸缩场景（新节点自动加入）非常重要。

### nvidia-container-toolkit

这是容器运行时层的关键组件，负责让容器内的进程能访问 GPU 硬件。它通过修改 OCI Runtime Spec，在容器启动时注入 NVIDIA 相关的设备文件和库路径。

安装后，containerd 的配置需要更新：

```toml
# /etc/containerd/config.toml
[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.nvidia]
  runtime_type = "io.containerd.runc.v2"
  [plugins."io.containerd.grpc.v1.cri".containerd.runtimes.nvidia.options]
    BinaryName = "/usr/bin/nvidia-container-runtime"
```

---

## GPU 节点配置

### 驱动版本管理

NVIDIA 驱动有两个版本分支：
- **生产分支（Production Branch）**：稳定性优先，适合生产环境。如 550.x。
- **新功能分支（New Feature Branch）**：包含最新特性，变更频繁。

CUDA 与驱动的兼容性遵循最低版本要求，但**向上兼容**：驱动 550 可以运行 CUDA 12.x 编译的程序，也可以运行 CUDA 11.x 的程序。因此推荐使用最新稳定驱动，镜像内可以使用不同 CUDA 版本。

**查看驱动与 CUDA 版本：**

```bash
# 在 GPU 节点上
nvidia-smi

# 输出示例：
# Driver Version: 550.54.15   CUDA Version: 12.4
# GPU 0: NVIDIA A100 80GB PCIe  
# Memory-Usage: 0MiB / 81920MiB
```

### 节点标签与污点

GPU 节点应该打上标签，用于精确调度：

```bash
# 按 GPU 型号打标签
kubectl label node gpu-node-1 nvidia.com/gpu.product=A100-SXM4-80GB
kubectl label node gpu-node-2 nvidia.com/gpu.product=T4

# 按用途区分训练/推理节点
kubectl label node gpu-node-1 workload=training
kubectl label node gpu-node-2 workload=inference

# 添加污点，防止普通 Pod 占用 GPU 节点资源
kubectl taint node gpu-node-1 nvidia.com/gpu=present:NoSchedule
```

GPU Operator 会自动添加 `nvidia.com/gpu.product`、`nvidia.com/gpu.memory` 等标签，非常方便。

### MIG：多实例 GPU

MIG（Multi-Instance GPU）是 NVIDIA A100/H100 的特性，允许将一张 GPU 物理切分为多个独立实例，每个实例有独立的显存和计算资源，互不干扰。

**A100 80GB 的 MIG 切分选项：**

| Profile | GPU 实例 | 显存 | SM 切片 |
|---------|---------|------|---------|
| 1g.10gb | 7 个 | 10GB | 1/7 |
| 2g.20gb | 3 个 | 20GB | 2/7 |
| 3g.40gb | 2 个 | 40GB | 3/7 |
| 7g.80gb | 1 个 | 80GB | 7/7（整卡） |

**启用 MIG 并配置切分：**

```bash
# 在节点上启用 MIG 模式
nvidia-smi -mig 1

# 创建 GPU 实例（切分为 7 个 1g.10gb）
nvidia-smi mig -cgi 1g.10gb,1g.10gb,1g.10gb,1g.10gb,1g.10gb,1g.10gb,1g.10gb -C
```

在 K8s 中使用 MIG 实例，需要配置 GPU Operator 的 MIG Manager：

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: default-mig-parted-config
  namespace: gpu-operator
data:
  config.yaml: |
    version: v1
    mig-configs:
      all-1g.10gb:
        - devices: all
          mig-enabled: true
          mig-devices:
            "1g.10gb": 7
      all-2g.20gb:
        - devices: all
          mig-enabled: true
          mig-devices:
            "2g.20gb": 3
```

然后为节点打标签触发 MIG 配置：

```bash
kubectl label node gpu-node-1 nvidia.com/mig.config=all-1g.10gb
```

---

## 资源调度

### requests 与 limits 配置

GPU 资源比较特殊：requests 和 limits **必须相等**，且必须是整数（整卡分配）。MIG 模式下可以请求分数 GPU。

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: gpu-training-job
spec:
  containers:
    - name: trainer
      image: pytorch/pytorch:2.1.0-cuda12.1-cudnn8-runtime
      resources:
        limits:
          nvidia.com/gpu: 4        # 请求 4 张整卡
          memory: "64Gi"
          cpu: "16"
        requests:
          nvidia.com/gpu: 4
          memory: "64Gi"
          cpu: "16"
      env:
        - name: NVIDIA_VISIBLE_DEVICES
          value: all
        - name: NVIDIA_DRIVER_CAPABILITIES
          value: compute,utility
```

**MIG 实例请求：**

```yaml
resources:
  limits:
    nvidia.com/mig-1g.10gb: 1    # 请求 1 个 1g.10gb MIG 实例
```

### 节点亲和性与反亲和性

训练作业通常对 GPU 型号有要求，推理对延迟更敏感。通过 affinity 精确控制调度位置：

```yaml
spec:
  affinity:
    nodeAffinity:
      requiredDuringSchedulingIgnoredDuringExecution:
        nodeSelectorTerms:
          - matchExpressions:
              # 必须是 A100
              - key: nvidia.com/gpu.product
                operator: In
                values:
                  - A100-SXM4-80GB
                  - A100-PCIE-80GB
      preferredDuringSchedulingIgnoredDuringExecution:
        - weight: 100
          preference:
            matchExpressions:
              # 优先选择 NVLink 互联的节点
              - key: nvidia.com/gpu.count
                operator: Gt
                values: ["4"]
    # 多副本推理服务：分散到不同节点
    podAntiAffinity:
      preferredDuringSchedulingIgnoredDuringExecution:
        - weight: 50
          podAffinityTerm:
            labelSelector:
              matchLabels:
                app: inference-server
            topologyKey: kubernetes.io/hostname
```

### GPU 拓扑感知调度

对于多 GPU 分布式训练，GPU 之间的通信带宽至关重要。NVLink 连接的 GPU 之间带宽可达 600GB/s，而 PCIe 只有约 16GB/s。

Kubernetes 的 Topology Manager 可以确保 GPU 和 CPU 在同一 NUMA 节点上分配，减少跨 NUMA 访问延迟：

```yaml
# kubelet 配置
--topology-manager-policy=best-effort
--topology-manager-scope=pod
--cpu-manager-policy=static
```

对于大规模训练（如 8 卡 A100），建议配置：

```yaml
# Pod 请求整机所有 GPU
resources:
  limits:
    nvidia.com/gpu: 8
# 加注解强制绑定到同一物理节点
metadata:
  annotations:
    scheduler.alpha.kubernetes.io/tolerations: '[]'
```

---

## GPU 共享方案对比

### 整卡独占（默认）

K8s 默认方案，一个容器独占一张完整 GPU。优点是隔离性强，性能可预期；缺点是资源浪费，一个小推理服务用不满整卡。

**适用场景：** 大模型训练、需要最大显存的推理服务。

### MIG：物理切分

前面已介绍，A100/H100 专属特性。物理层面隔离，每个实例有独立的显存和 SM，完全隔离没有竞争。

**适用场景：** 多租户环境、需要硬隔离的 SaaS 场景。

**限制：** 只支持 A100/H100，切分规格固定，不够灵活。

### 时间片共享（NVIDIA Time-Slicing）

通过 GPU 时间分片让多个进程共享同一 GPU，类似 CPU 的分时复用：

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: time-slicing-config
  namespace: gpu-operator
data:
  any: |-
    version: v1
    flags:
      migStrategy: none
    sharing:
      timeSlicing:
        renameByDefault: false
        failRequestsGreaterThanOne: false
        resources:
          - name: nvidia.com/gpu
            replicas: 4  # 将 1 张 GPU 虚拟为 4 个资源
```

应用后，一张 GPU 会虚拟出 4 个 `nvidia.com/gpu` 资源，4 个 Pod 可以各自请求 1 个。但注意：**没有显存隔离**，任何一个进程都可以申请全部显存，可能导致 OOM。

**适用场景：** 开发测试环境、显存需求小的推理服务（如 embedding 模型）。

### MPS：Multi-Process Service

MPS（CUDA Multi-Process Service）允许多个 CUDA 进程并发使用同一 GPU 的 SM，不同于时间片的轮转方式，MPS 是真正的空间并发：

```bash
# 在节点上启动 MPS Server
nvidia-cuda-mps-control -d
echo "set_default_active_thread_percentage 50" | nvidia-cuda-mps-control
```

**MPS vs 时间片：**
- MPS 延迟更低（并发执行而非轮转）
- MPS 适合多个小任务同时跑，吞吐更高
- MPS 需要进程间信任（有内存隔离但不完全安全隔离）

**适用场景：** 同一租户的多个推理进程并发共享一张 GPU。

---

## Karpenter：弹性 GPU 节点池

GPU 实例按需创建、用完即销毁，是降低 GPU 成本的关键。Karpenter 比 Cluster Autoscaler 更适合这个场景，因为它支持更细粒度的实例类型选择。

### GPU NodePool 配置

```yaml
apiVersion: karpenter.sh/v1
kind: NodePool
metadata:
  name: gpu-training
spec:
  template:
    metadata:
      labels:
        workload: training
        nvidia.com/gpu: "true"
    spec:
      nodeClassRef:
        apiVersion: karpenter.k8s.aws/v1
        kind: EC2NodeClass
        name: gpu-nodeclass
      requirements:
        - key: karpenter.sh/capacity-type
          operator: In
          values: ["spot", "on-demand"]
        - key: node.kubernetes.io/instance-type
          operator: In
          values:
            - p4d.24xlarge    # 8x A100 40GB
            - p3.2xlarge      # 1x V100
            - p3.8xlarge      # 4x V100
            - g5.xlarge       # 1x A10G
            - g5.12xlarge     # 4x A10G
        - key: kubernetes.io/arch
          operator: In
          values: ["amd64"]
      taints:
        - key: nvidia.com/gpu
          value: "present"
          effect: NoSchedule
  limits:
    nvidia.com/gpu: "64"   # 最多 64 张 GPU
  disruption:
    consolidationPolicy: WhenEmpty
    consolidateAfter: 30m  # GPU 节点空闲 30 分钟后回收
---
apiVersion: karpenter.k8s.aws/v1
kind: EC2NodeClass
metadata:
  name: gpu-nodeclass
spec:
  amiFamily: AL2
  role: KarpenterNodeRole-my-cluster
  subnetSelectorTerms:
    - tags:
        karpenter.sh/discovery: "my-cluster"
  securityGroupSelectorTerms:
    - tags:
        karpenter.sh/discovery: "my-cluster"
  instanceStorePolicy: RAID0
  userData: |
    #!/bin/bash
    # 安装 NVIDIA 驱动和容器工具包
    yum install -y kernel-devel-$(uname -r)
    # GPU Operator 会自动处理驱动安装
```

### Spot GPU 中断处理

使用 Spot GPU 实例可以节省 60-90% 成本，但需要处理实例中断：

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: training-job
spec:
  template:
    spec:
      # 配置优雅终止时间
      terminationGracePeriodSeconds: 120
      containers:
        - name: trainer
          lifecycle:
            preStop:
              exec:
                command:
                  - /bin/sh
                  - -c
                  - |
                    # 保存 checkpoint 后退出
                    kill -SIGTERM $(pgrep -f train.py)
                    sleep 100
```

结合 AWS Node Termination Handler，提前收到 Spot 中断通知（2 分钟预告），触发优雅 checkpoint 保存：

```bash
helm install aws-node-termination-handler \
  eks/aws-node-termination-handler \
  --namespace kube-system \
  --set enableSpotInterruptionDraining=true \
  --set enableRebalanceMonitoring=true \
  --set webhookURL=${SLACK_WEBHOOK_URL}
```

---

## 监控体系：DCGM Exporter

### 核心指标

DCGM（Data Center GPU Manager）提供 GPU 的全面监控指标：

| 指标 | 说明 | 告警阈值 |
|------|------|---------|
| `DCGM_FI_DEV_GPU_UTIL` | GPU SM 利用率（%） | < 20%（浪费）> 95%（过载）|
| `DCGM_FI_DEV_MEM_COPY_UTIL` | 显存带宽利用率（%） | > 90% |
| `DCGM_FI_DEV_FB_USED` | 已用显存（MiB） | > 95% 容量 |
| `DCGM_FI_DEV_GPU_TEMP` | GPU 温度（℃） | > 80℃ 警告，> 90℃ 严重 |
| `DCGM_FI_DEV_POWER_USAGE` | 功耗（W） | > TDP 的 95% |
| `DCGM_FI_DEV_NVLINK_BANDWIDTH_TOTAL` | NVLink 带宽 | 分布式训练监控 |
| `DCGM_FI_DEV_ECC_SBE_VOL_TOTAL` | 单比特 ECC 错误 | > 0 关注，积累增加告警 |
| `DCGM_FI_DEV_ECC_DBE_VOL_TOTAL` | 双比特 ECC 错误（不可修复）| > 0 立即告警 |

### Prometheus + Grafana 集成

GPU Operator 部署时如果启用了 `dcgmExporter.enabled=true`，会自动创建 DCGM Exporter DaemonSet 和对应的 ServiceMonitor。

**Grafana Dashboard 关键面板：**

```json
{
  "panels": [
    {
      "title": "GPU 利用率",
      "targets": [{
        "expr": "avg by (gpu, node) (DCGM_FI_DEV_GPU_UTIL{namespace='gpu-operator'})"
      }]
    },
    {
      "title": "显存使用率",
      "targets": [{
        "expr": "DCGM_FI_DEV_FB_USED / DCGM_FI_DEV_FB_FREE * 100"
      }]
    },
    {
      "title": "GPU 温度",
      "targets": [{
        "expr": "DCGM_FI_DEV_GPU_TEMP"
      }],
      "thresholds": [{"value": 80, "color": "yellow"}, {"value": 90, "color": "red"}]
    }
  ]
}
```

### Prometheus 告警规则

```yaml
groups:
  - name: gpu-alerts
    rules:
      - alert: GPUHighTemperature
        expr: DCGM_FI_DEV_GPU_TEMP > 85
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "GPU {{ $labels.gpu }} 温度过高"
          description: "节点 {{ $labels.node }} GPU {{ $labels.gpu }} 温度 {{ $value }}℃，已超过 85℃ 阈值"

      - alert: GPUMemoryAlmostFull
        expr: DCGM_FI_DEV_FB_USED / (DCGM_FI_DEV_FB_USED + DCGM_FI_DEV_FB_FREE) > 0.95
        for: 2m
        labels:
          severity: critical
        annotations:
          summary: "GPU 显存接近用满"
          description: "节点 {{ $labels.node }} GPU {{ $labels.gpu }} 显存使用率 {{ $value | humanizePercentage }}"

      - alert: GPULowUtilization
        expr: |
          avg_over_time(DCGM_FI_DEV_GPU_UTIL[30m]) < 10
          and on(node) kube_node_labels{label_workload="training"} == 1
        for: 30m
        labels:
          severity: warning
        annotations:
          summary: "训练节点 GPU 利用率持续偏低"
          description: "节点 {{ $labels.node }} GPU 30 分钟平均利用率仅 {{ $value }}%，疑似空转"

      - alert: GPUUncorrectableError
        expr: DCGM_FI_DEV_ECC_DBE_VOL_TOTAL > 0
        for: 0m
        labels:
          severity: critical
        annotations:
          summary: "GPU 发生不可修复 ECC 错误"
          description: "节点 {{ $labels.node }} GPU {{ $labels.gpu }} 检测到双比特 ECC 错误，硬件可能损坏"
```

---

## 训练作业调度

### 分布式训练架构

大模型训练通常需要多机多卡，K8s 上主要使用 Kubeflow 的 Training Operator 管理分布式训练作业：

```yaml
apiVersion: kubeflow.org/v1
kind: PyTorchJob
metadata:
  name: llm-pretrain
  namespace: training
spec:
  pytorchReplicaSpecs:
    Master:
      replicas: 1
      restartPolicy: OnFailure
      template:
        spec:
          tolerations:
            - key: nvidia.com/gpu
              operator: Exists
              effect: NoSchedule
          containers:
            - name: pytorch
              image: pytorch/pytorch:2.1.0-cuda12.1-cudnn8-devel
              resources:
                limits:
                  nvidia.com/gpu: 8
                  memory: "640Gi"
                  cpu: "96"
              command:
                - torchrun
                - --nproc_per_node=8
                - --nnodes=4
                - --node_rank=$(RANK)
                - --master_addr=$(MASTER_ADDR)
                - --master_port=23456
                - train.py
                - --model_size=7B
              volumeMounts:
                - name: training-data
                  mountPath: /data
                - name: checkpoint
                  mountPath: /checkpoint
          volumes:
            - name: training-data
              persistentVolumeClaim:
                claimName: training-dataset-pvc
            - name: checkpoint
              persistentVolumeClaim:
                claimName: checkpoint-pvc
    Worker:
      replicas: 3
      restartPolicy: OnFailure
      template:
        spec:
          tolerations:
            - key: nvidia.com/gpu
              operator: Exists
              effect: NoSchedule
          affinity:
            nodeAffinity:
              requiredDuringSchedulingIgnoredDuringExecution:
                nodeSelectorTerms:
                  - matchExpressions:
                      - key: nvidia.com/gpu.product
                        operator: In
                        values: ["A100-SXM4-80GB"]
          containers:
            - name: pytorch
              image: pytorch/pytorch:2.1.0-cuda12.1-cudnn8-devel
              resources:
                limits:
                  nvidia.com/gpu: 8
```

### 节点间高速互联

P4d.24xlarge 实例有 8x A100 通过 NVSwitch 全互联，多机之间通过 EFA（Elastic Fabric Adapter）高速互联，带宽可达 400Gbps。

配置 EFA 支持：

```yaml
containers:
  - name: pytorch
    resources:
      limits:
        hugepages-2Mi: "5120Mi"      # EFA 需要大页内存
        vpc.amazonaws.com/efa: "4"   # 请求 EFA 设备
    env:
      - name: NCCL_SOCKET_IFNAME
        value: "^lo"
      - name: NCCL_DEBUG
        value: "INFO"
      - name: FI_EFA_USE_DEVICE_RDMA
        value: "1"
      - name: FI_PROVIDER
        value: "efa"
```

---

## 推理部署优化

### Triton Inference Server

NVIDIA Triton 是专为 GPU 推理优化的服务框架，支持 TensorRT、ONNX、PyTorch、TensorFlow 等多种模型格式，内置动态 batching 和并发推理。

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: triton-server
  namespace: inference
spec:
  replicas: 2
  template:
    spec:
      tolerations:
        - key: nvidia.com/gpu
          operator: Exists
          effect: NoSchedule
      containers:
        - name: triton
          image: nvcr.io/nvidia/tritonserver:24.01-py3
          command:
            - tritonserver
            - --model-repository=s3://my-models/triton
            - --strict-model-config=false
            - --grpc-port=8001
            - --http-port=8000
            - --metrics-port=8002
          resources:
            limits:
              nvidia.com/gpu: 1
              memory: "32Gi"
              cpu: "8"
          ports:
            - containerPort: 8000
              name: http
            - containerPort: 8001
              name: grpc
            - containerPort: 8002
              name: metrics
          readinessProbe:
            httpGet:
              path: /v2/health/ready
              port: 8000
            initialDelaySeconds: 30
            periodSeconds: 10
          livenessProbe:
            httpGet:
              path: /v2/health/live
              port: 8000
            initialDelaySeconds: 60
```

**动态 batching 配置（`config.pbtxt`）：**

```protobuf
name: "my_model"
backend: "tensorrt"
max_batch_size: 32

input [
  {
    name: "input_ids"
    data_type: TYPE_INT32
    dims: [512]
  }
]

output [
  {
    name: "logits"
    data_type: TYPE_FP32
    dims: [32000]
  }
]

dynamic_batching {
  preferred_batch_size: [8, 16, 32]
  max_queue_delay_microseconds: 5000  # 等待最多 5ms 凑 batch
}

instance_group [
  {
    count: 2
    kind: KIND_GPU
    gpus: [0]
  }
]
```

### vLLM：LLM 推理的事实标准

对于 LLM 推理，vLLM 凭借 PagedAttention 显存管理和 Continuous Batching，已成为最主流的选择：

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: vllm-server
spec:
  template:
    spec:
      containers:
        - name: vllm
          image: vllm/vllm-openai:v0.4.3
          command:
            - python
            - -m
            - vllm.entrypoints.openai.api_server
            - --model
            - /models/Llama-3-8B-Instruct
            - --tensor-parallel-size
            - "1"
            - --gpu-memory-utilization
            - "0.90"
            - --max-num-seqs
            - "256"
            - --port
            - "8000"
          resources:
            limits:
              nvidia.com/gpu: 1
              memory: "40Gi"
          volumeMounts:
            - name: model-storage
              mountPath: /models
      volumes:
        - name: model-storage
          persistentVolumeClaim:
            claimName: model-pvc
```

---

## 常见故障排查

### OOMKilled：显存不足

最常见的 GPU 问题。排查步骤：

```bash
# 查看 Pod 状态
kubectl describe pod <pod-name> | grep -A5 "Last State"

# 查看显存使用情况
kubectl exec -it <pod-name> -- nvidia-smi

# 检查 DCGM 指标
kubectl exec -it dcgm-exporter-xxx -- nvidia-smi dmon -s u
```

解决方案：
1. 增大 `nvidia.com/gpu` 请求（使用更多 GPU 分散显存）
2. 减小 batch size
3. 使用混合精度（FP16/BF16）减少显存占用
4. 开启梯度检查点（gradient checkpointing）

### Pod 调度到无 GPU 节点

症状：Pod pending，Event 显示 `Insufficient nvidia.com/gpu`，但实际有 GPU 节点空闲。

排查：

```bash
# 查看节点 GPU 资源
kubectl get nodes -o custom-columns=\
"NAME:.metadata.name,GPU:.status.capacity.nvidia\.com/gpu"

# 查看是否有污点未容忍
kubectl describe node gpu-node-1 | grep Taint

# 查看 Pod 是否配置了 toleration
kubectl get pod <pod-name> -o yaml | grep -A10 tolerations
```

常见原因：
- 忘记配置 `tolerations` 匹配 GPU 节点污点
- `nodeSelector` 条件与实际节点标签不匹配
- Device Plugin 未正常运行，节点 GPU 资源为 0

```bash
# 检查 Device Plugin 状态
kubectl get pod -n nvidia-device-plugin
kubectl logs -n nvidia-device-plugin nvidia-device-plugin-xxx
```

### 驱动版本不兼容

症状：Pod 启动失败，日志显示 `CUDA driver version is insufficient`。

```bash
# 查看节点驱动版本
kubectl exec -it <pod-name> -- nvidia-smi

# 检查 CUDA Toolkit 版本要求
# pytorch 镜像标签如 pytorch:2.1.0-cuda12.1，需要驱动 >= 525
```

解决方案：升级节点 NVIDIA 驱动，或使用与驱动版本兼容的镜像。

### NVLink 未启用导致训练慢

症状：多 GPU 训练比预期慢，NCCL all-reduce 通信成为瓶颈。

```bash
# 检查 NVLink 状态
nvidia-smi nvlink --status -i 0

# 检查 NVLink 带宽
nvidia-smi nvlink --getbandwidth -i 0
```

---

## 成本优化

### Spot GPU 实例策略

结合 Karpenter，训练作业使用 Spot 实例可以节省 60-70%：

```yaml
# Karpenter NodePool：优先 Spot，容量不足时自动切换 On-Demand
spec:
  template:
    spec:
      requirements:
        - key: karpenter.sh/capacity-type
          operator: In
          values: ["spot", "on-demand"]
        - key: node.kubernetes.io/instance-type
          operator: In
          values: ["p3.8xlarge", "p3.16xlarge", "p4d.24xlarge"]
  disruption:
    consolidationPolicy: WhenEmpty
    consolidateAfter: 10m
```

训练代码配合 checkpoint 机制，Spot 中断时自动从最近 checkpoint 恢复。

### GPU 利用率监控与优化

低 GPU 利用率是最大的浪费。设置 Grafana 告警：利用率持续 30 分钟低于 20% 则触发通知。

常见低利用率原因：
- 数据加载成为瓶颈（CPU 喂不饱 GPU）：增大 `num_workers`，使用 DALI 预处理
- batch size 太小：适当增大 batch size 提高 GPU 并行度
- 频繁同步操作：减少梯度同步频率（gradient accumulation）

### 节点自动缩容

训练完成后及时释放 GPU 节点，避免空转计费：

```yaml
# Karpenter 配置：节点空闲 10 分钟即回收
disruption:
  consolidationPolicy: WhenEmpty
  consolidateAfter: 10m
  # 预算控制：每次最多回收 50% 节点
  budgets:
    - nodes: "50%"
```

GPU 这块最容易浪费钱，节点弹性（Karpenter）、资源隔离（MIG/时间片）、监控告警（DCGM）这三件事不做扎实，后面所有调优都是瞎猜。
