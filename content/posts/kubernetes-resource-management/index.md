---
title: "Kubernetes 资源管理实战——QoS、ResourceQuota、VPA 体系化实践"
date: 2026-04-12T11:00:00+08:00
draft: false
tags: ["Kubernetes", "资源管理", "QoS", "HPA", "VPA", "性能"]
categories: ["Kubernetes"]
series: ["K8s 完全指南"]
description: Kubernetes 资源管理体系：从 QoS 设计到 HPA/VPA 自动扩缩，用正确的 requests/limits 彻底解决 OOMKilled 和资源争抢问题
summary: 我在生产中见过太多因为资源配置不当导致的事故：不设 limits 的服务把节点内存吃光导致 OOM 驱逐、requests 设得过高导致 Pod 调度不上去、HPA 配置错误导致扩缩失灵。这篇文章把 K8s 资源管理体系从头到尾捋一遍，让你建立完整的资源治理思路。
toc: true
math: false
diagram: false
keywords: ["Kubernetes", "QoS", "requests", "limits", "HPA", "VPA", "OOMKilled", "ResourceQuota", "LimitRange"]
params:
  reading_time: true
---

我在生产中见过太多因为资源配置不当导致的事故：不设 limits 的服务把节点内存吃光导致 OOM 驱逐、requests 设得过高导致 Pod 调度不上去、HPA 配置错误导致扩缩失灵。这篇文章把 K8s 资源管理体系从头到尾捋一遍，让你建立完整的资源治理思路。

## QoS 三级：K8s 的资源保障优先级

K8s 根据容器的 requests 和 limits 设置，自动将 Pod 分为三个 QoS 等级，在节点资源紧张时决定谁先被驱逐。

### Guaranteed（最高保障）

所有容器都设置了相同的 CPU 和内存 requests/limits，且 requests == limits：

```yaml
resources:
  requests:
    cpu: "500m"
    memory: "512Mi"
  limits:
    cpu: "500m"
    memory: "512Mi"
```

**特点**：节点 OOM 时最后被杀，适合核心数据库、关键业务服务。代价是资源利用率低（不能 burst）。

### Burstable（可突发）

至少一个容器设置了 requests，但 requests != limits（或者只设了 limits 没设 requests）：

```yaml
resources:
  requests:
    cpu: "100m"
    memory: "128Mi"
  limits:
    cpu: "1000m"
    memory: "1Gi"
```

**特点**：平时按 requests 调度，空闲时可以 burst 到 limits。节点压力大时，按 OOM score 决定驱逐顺序（使用内存超出 requests 越多，越容易被驱逐）。适合大多数 Web 服务。

### BestEffort（无保障）

所有容器都没有设置任何 requests 和 limits：

```yaml
# 没有 resources 字段，或者 resources: {}
```

**特点**：节点资源紧张时第一个被驱逐。只适合临时 Job 或非关键批处理任务，**生产服务禁止使用**。

---

## requests vs limits：设计原则

理解这两个概念的本质是做好资源管理的前提。

**requests**：调度器用来决定把 Pod 放到哪个节点。节点的 `Allocatable` 减去所有 Pod 的 `requests` 之和就是剩余可调度资源。

**limits**：容器运行时的上限。超过 CPU limits 会被 throttle（限速），超过内存 limits 会被 OOMKill。

```bash
# 查看节点的可分配资源
kubectl describe node <node-name> | grep -A 5 "Allocatable"

# 查看节点上已分配的资源（requests 之和）
kubectl describe node <node-name> | grep -A 10 "Allocated resources"
```

### CPU 和内存的本质区别

**CPU 是可压缩资源**：超出 limits 时，进程被限速但不会被杀，只是变慢。1000m = 1 核，500m = 半核。

**内存是不可压缩资源**：超出 limits 时，进程直接被 OOMKill（`exit code 137`），没有缓冲区。

这个区别决定了设置策略：

```yaml
# 推荐的生产配置策略
resources:
  requests:
    cpu: "100m"      # 保守设置，只占调度资源，不影响 burst
    memory: "256Mi"  # 接近实际使用量，给调度器准确参考
  limits:
    cpu: "2000m"     # CPU 可以设大，超了只是慢不会崩
    memory: "512Mi"  # 内存必须留足裕量，超了就 OOMKill
```

---

## OOMKilled 排查实战

有一次凌晨告警，一个 Python 服务频繁重启，查看 Pod 状态：

```bash
kubectl describe pod <pod-name> -n production
```

```
Last State: Terminated
  Reason: OOMKilled
  Exit Code: 137
  Started: Sun, 12 Apr 2026 02:14:23 +0800
  Finished: Sun, 12 Apr 2026 02:19:45 +0800
```

**容器 OOM vs 节点 OOM 的区分**：

- `OOMKilled` + Exit Code 137：容器超出了自己的 memory limit，只有这个 Pod 受影响
- 节点日志 `kernel: Out of memory: Kill process`：节点级别 OOM，会触发驱逐

```bash
# 查看容器实际内存使用（需要 metrics-server）
kubectl top pod <pod-name> -n production --containers

# 查看 Pod 的 OOM 事件
kubectl get events -n production --field-selector reason=OOMKilling
```

**确定合理的 memory limit**：

1. 用 `kubectl top pod` 观察正常负载下的内存使用（P95）
2. limit 设置为 P95 * 1.5 到 2 倍（留 buffer）
3. 如果内存一直线性增长，先排查泄漏，不要无脑加 limit

---

## HPA：水平自动扩缩

### 基础配置

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: myapp-hpa
  namespace: production
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: myapp
  minReplicas: 2
  maxReplicas: 20
  metrics:
  - type: Resource
    resource:
      name: cpu
      target:
        type: Utilization
        averageUtilization: 60   # 目标 CPU 利用率 60%
  - type: Resource
    resource:
      name: memory
      target:
        type: Utilization
        averageUtilization: 70
  behavior:
    scaleUp:
      stabilizationWindowSeconds: 30   # 扩容稳定窗口（快速响应）
      policies:
      - type: Percent
        value: 100
        periodSeconds: 60
    scaleDown:
      stabilizationWindowSeconds: 300  # 缩容稳定窗口（避免抖动）
      policies:
      - type: Percent
        value: 20
        periodSeconds: 60
```

### targetAverageUtilization 计算公式

HPA 的扩缩决策公式：

```
期望副本数 = ceil(当前副本数 × (当前平均利用率 / 目标利用率))
```

例如：当前 4 个副本，CPU 利用率 80%，目标 60%：

```
期望副本数 = ceil(4 × (80 / 60)) = ceil(5.33) = 6
```

**重要**：这里的"利用率"是相对于 **requests** 的百分比，不是节点 CPU 的百分比。如果 requests 设得很小，即使容器实际 CPU 不高，利用率百分比也会很大，导致 HPA 频繁扩容。

```bash
# 查看 HPA 当前状态
kubectl get hpa -n production
kubectl describe hpa myapp-hpa -n production
```

---

## ResourceQuota + LimitRange：命名空间资源隔离

### ResourceQuota：总量限制

```yaml
apiVersion: v1
kind: ResourceQuota
metadata:
  name: production-quota
  namespace: production
spec:
  hard:
    # 计算资源
    requests.cpu: "40"
    requests.memory: 80Gi
    limits.cpu: "100"
    limits.memory: 200Gi
    # 对象数量
    pods: "200"
    services: "50"
    persistentvolumeclaims: "30"
    # 存储
    requests.storage: 500Gi
```

### LimitRange：单个容器的默认值和范围

```yaml
apiVersion: v1
kind: LimitRange
metadata:
  name: production-limitrange
  namespace: production
spec:
  limits:
  - type: Container
    # 未设置 requests/limits 时的默认值
    default:
      cpu: "500m"
      memory: "512Mi"
    defaultRequest:
      cpu: "100m"
      memory: "128Mi"
    # 允许设置的范围
    max:
      cpu: "4"
      memory: "8Gi"
    min:
      cpu: "50m"
      memory: "64Mi"
  - type: Pod
    max:
      cpu: "8"
      memory: "16Gi"
```

**最佳实践**：每个命名空间都应该配 LimitRange，防止忘记设 resources 的服务成为 BestEffort 类型，被随时驱逐。

---

## VPA：垂直自动扩缩

VPA（Vertical Pod Autoscaler）自动推荐和调整 requests，解决手动设置不准确的问题。

### 推荐模式（生产常用）

```yaml
apiVersion: autoscaling.k8s.io/v1
kind: VerticalPodAutoscaler
metadata:
  name: myapp-vpa
  namespace: production
spec:
  targetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: myapp
  updatePolicy:
    updateMode: "Off"   # Off = 只推荐，不自动修改
  resourcePolicy:
    containerPolicies:
    - containerName: myapp
      minAllowed:
        cpu: 50m
        memory: 64Mi
      maxAllowed:
        cpu: 4
        memory: 4Gi
```

```bash
# 查看 VPA 推荐值
kubectl describe vpa myapp-vpa -n production
# 输出中的 Recommendation.containerRecommendations 包含：
# LowerBound: 保守下限
# Target: 推荐值
# UpperBound: 保守上限
```

### VPA 与 HPA 的配合

**重要限制**：VPA 和 HPA 不能同时基于 CPU/内存扩缩，否则会互相打架（VPA 调大 requests -> HPA 认为利用率下降 -> 缩容 -> VPA 推荐降低 requests...）。

**正确配合方式**：

| 场景 | 推荐方案 |
|------|---------|
| 单纯水平扩缩 | HPA（CPU/内存） |
| 单纯垂直调优 | VPA（Auto 模式） |
| 既要水平又要垂直 | HPA（CPU）+ VPA（内存，需配置 `containerPolicies` 排除 CPU） |
| 自定义指标扩缩 | KEDA（替代 HPA，功能更强） |

---

## 资源画像实战：识别浪费

```bash
# 查看所有 Pod 的实际资源使用
kubectl top pods -A --sort-by=memory

# 查看某命名空间所有容器的 requests vs 实际使用
kubectl top pod -n production --containers

# 识别资源严重浪费的服务（requests 远大于实际使用）
# 用 Prometheus 查询（需要 metrics-server 或 kube-state-metrics）
```

Prometheus 查询资源 Slack（浪费）：

```promql
# CPU requests 使用率（低说明 requests 设太高）
sum(rate(container_cpu_usage_seconds_total{namespace="production"}[5m])) by (pod)
/
sum(kube_pod_container_resource_requests{resource="cpu", namespace="production"}) by (pod)

# 内存 requests 使用率
sum(container_memory_working_set_bytes{namespace="production"}) by (pod)
/
sum(kube_pod_container_resource_requests{resource="memory", namespace="production"}) by (pod)
```

我在做成本优化时，用这个查询发现某几个服务的 CPU requests 利用率不到 5%，把 requests 从 500m 降到 50m 后，腾出了大量可调度空间，延缓了节点扩容。

---

## 常见陷阱总结

1. **requests 设 0**：Pod 变成 BestEffort，随时可能被驱逐
2. **limits >> requests（差距超过 10x）**：实际运行时很容易触碰 limits 被 OOMKill，但调度器以为资源充足
3. **CPU limits 设太低**：Java/Go 服务启动时 CPU 会 spike，limits 太低导致启动极慢（不是挂了，是被 throttle 了）
4. **没有配 LimitRange**：新人提交的 Pod 忘记写 resources，成为 BestEffort
5. **HPA + VPA 同时基于 CPU**：互相干扰，导致副本数和资源配置不稳定

资源管理是 K8s 集群稳定性的基础。先把 requests/limits 设合理，再上 HPA，最后用 VPA 做持续优化，这个顺序不能乱。
