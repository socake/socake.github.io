---
title: "Kubernetes HPA/VPA 弹性伸缩配置"
date: 2025-12-09T10:00:00+08:00
draft: false
tags: ["Kubernetes", "HPA", "VPA", "KEDA", "弹性伸缩", "运维"]
categories: ["Kubernetes"]
description: "深入讲解 Kubernetes HPA v2 完整配置、扩缩行为防抖控制、VPA 四种模式对比、KEDA 基于事件伸缩（Kafka/RabbitMQ 触发器），以及 HPA 不触发/扩容缓慢等常见问题排查。"
summary: "从 HPA v2 到 KEDA 事件驱动伸缩，覆盖 CPU/内存/自定义指标配置、防抖参数调优、VPA 推荐器集成和生产级弹性伸缩最佳实践。"
toc: true
math: false
diagram: false
keywords: ["Kubernetes", "HPA", "VPA", "KEDA", "弹性伸缩", "metrics-server", "自定义指标", "Prometheus Adapter"]
params:
  reading_time: true
---

## HPA 工作原理

```
metrics-server / Prometheus Adapter / Custom Metrics API
        ↓  每 15s 采集一次
HPA Controller（kube-controller-manager 内）
        ↓  计算期望副本数
        ↓  期望副本数 = ceil(当前副本数 × (当前指标值 / 目标指标值))
Deployment / StatefulSet / ReplicaSet
        ↓  调整 replicas
Pod 扩缩容
```

核心公式：

```
desiredReplicas = ceil(currentReplicas × (currentMetricValue / desiredMetricValue))
```

例：当前 3 个副本，CPU 使用率 90%，目标 50%：
```
desiredReplicas = ceil(3 × (90 / 50)) = ceil(5.4) = 6
```

---

## 安装 metrics-server

```bash
# 安装 metrics-server（HPA CPU/内存指标依赖）
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml

# 私有集群需要禁用 TLS 证书验证（添加启动参数）
kubectl -n kube-system patch deployment metrics-server \
  --type=json \
  -p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'

# 验证安装
kubectl top nodes
kubectl top pods -n production
```

---

## HPA v2 完整配置

### CPU + 内存双指标

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: my-app-hpa
  namespace: production
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: my-app
  minReplicas: 2
  maxReplicas: 20
  metrics:
    # 1. CPU 使用率（基于 requests）
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 60   # 目标 CPU 使用率 60%

    # 2. 内存使用量（绝对值）
    - type: Resource
      resource:
        name: memory
        target:
          type: AverageValue
          averageValue: 512Mi      # 每个 Pod 平均内存不超过 512Mi
```

### 自定义指标（Prometheus Adapter）

```yaml
# 先安装 Prometheus Adapter 并配置规则
# ConfigMap：prometheus-adapter-config
rules:
  - seriesQuery: 'http_requests_per_second{namespace!="",pod!=""}'
    resources:
      overrides:
        namespace: {resource: "namespace"}
        pod: {resource: "pod"}
    name:
      matches: "^(.*)_per_second"
      as: "${1}_per_second"
    metricsQuery: 'sum(rate(<<.Series>>{<<.LabelMatchers>>}[2m])) by (<<.GroupBy>>)'
```

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: my-app-hpa-custom
  namespace: production
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: my-app
  minReplicas: 2
  maxReplicas: 50
  metrics:
    # 3. 自定义指标：每个 Pod 的 QPS
    - type: Pods
      pods:
        metric:
          name: http_requests_per_second
        target:
          type: AverageValue
          averageValue: "100"     # 每个 Pod 处理 100 QPS

    # 4. External 指标：队列深度（来自外部系统）
    - type: External
      external:
        metric:
          name: rabbitmq_queue_messages
          selector:
            matchLabels:
              queue: "orders"
        target:
          type: AverageValue
          averageValue: "30"      # 队列消息数超过 30 触发扩容
```

---

## 扩缩行为控制（防抖）

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: my-app-hpa
  namespace: production
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: my-app
  minReplicas: 2
  maxReplicas: 20
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 60
  behavior:
    scaleUp:
      stabilizationWindowSeconds: 60    # 扩容稳定窗口：60s 内持续超阈值才扩
      policies:
        - type: Pods
          value: 4                       # 每次最多扩 4 个 Pod
          periodSeconds: 60
        - type: Percent
          value: 100                     # 或每次最多扩 100%（翻倍）
          periodSeconds: 60
      selectPolicy: Max                  # 选择两个 policy 中较大的

    scaleDown:
      stabilizationWindowSeconds: 300   # 缩容稳定窗口：5分钟内持续低于阈值才缩
      policies:
        - type: Pods
          value: 2                       # 每次最多缩 2 个 Pod
          periodSeconds: 60
        - type: Percent
          value: 10                      # 或每次最多缩 10%
          periodSeconds: 60
      selectPolicy: Min                  # 选择最保守的 policy（较小值）
```

```bash
# 查看 HPA 状态和扩缩历史
kubectl get hpa my-app-hpa -n production
kubectl describe hpa my-app-hpa -n production

# 输出中关注：
# Events 部分会显示扩缩容历史
# Conditions 显示当前状态（ScalingAllowed/ScalingLimited 等）
# Current Metrics 显示实时指标值
```

---

## VPA（Vertical Pod Autoscaler）

VPA 自动调整 Pod 的 CPU/内存 requests，三个组件：

| 组件 | 作用 |
|------|------|
| **Recommender** | 持续监控指标，生成资源推荐值 |
| **Updater** | 驱逐资源设置不合理的 Pod（触发重建） |
| **Admission Controller** | 在 Pod 创建时注入推荐的资源值 |

### 安装 VPA

```bash
git clone https://github.com/kubernetes/autoscaler.git
cd autoscaler/vertical-pod-autoscaler
./hack/vpa-install.sh

# 验证
kubectl get pods -n kube-system | grep vpa
```

### VPA 四种模式

| 模式 | 说明 | 适用场景 |
|------|------|----------|
| **Off** | 仅生成推荐值，不自动修改 | 分析资源用量，制定 requests |
| **Initial** | 仅在 Pod 创建时设置，不更新运行中 Pod | 新 Pod 优化，避免滚动更新 |
| **Recreate** | 超出范围时驱逐 Pod 重建 | 可以接受短暂中断的应用 |
| **Auto** | 自动选择 Initial/Recreate（未来支持原地更新） | 推荐生产使用 |

```yaml
apiVersion: autoscaling.k8s.io/v1
kind: VerticalPodAutoscaler
metadata:
  name: my-app-vpa
  namespace: production
spec:
  targetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: my-app
  updatePolicy:
    updateMode: "Auto"       # Off / Initial / Recreate / Auto
  resourcePolicy:
    containerPolicies:
      - containerName: "*"    # 应用到所有容器
        minAllowed:
          cpu: 100m
          memory: 128Mi
        maxAllowed:
          cpu: "4"
          memory: "8Gi"
        controlledResources: ["cpu", "memory"]
        controlledValues: RequestsAndLimits
```

```bash
# 查看 VPA 推荐值
kubectl describe vpa my-app-vpa -n production

# 输出示例：
# Recommendation:
#   Container Recommendations:
#     Container Name: my-app
#     Lower Bound:    cpu: 200m, memory: 256Mi
#     Target:         cpu: 500m, memory: 512Mi  ← 建议设置这个值
#     Upper Bound:    cpu: 1,    memory: 2Gi
```

---

## KEDA：基于事件的弹性伸缩

KEDA（Kubernetes Event-Driven Autoscaling）支持基于外部事件源（消息队列、数据库、HTTP 流量等）进行弹性伸缩，可从 0 扩容到 N，也可缩容到 0。

```bash
# 安装 KEDA
helm repo add kedacore https://kedacore.github.io/charts
helm install keda kedacore/keda --namespace keda --create-namespace

# 验证
kubectl get pods -n keda
```

### Kafka 触发器

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: kafka-consumer-scaler
  namespace: production
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: kafka-consumer
  pollingInterval: 15          # 每 15s 检查一次
  cooldownPeriod: 300          # 缩容冷却时间 5 分钟
  minReplicaCount: 1           # 最小 1 个（设为 0 则可完全缩容）
  maxReplicaCount: 30
  triggers:
    - type: kafka
      metadata:
        bootstrapServers: kafka.production.svc.cluster.local:9092
        consumerGroup: order-processor
        topic: orders
        lagThreshold: "50"     # 每个 Pod 处理 50 条消息的积压
        offsetResetPolicy: latest
      authenticationRef:
        name: kafka-auth       # 引用认证配置（如需）
```

### RabbitMQ 触发器

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: rabbitmq-consumer-scaler
  namespace: production
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: rabbitmq-worker
  minReplicaCount: 0           # 无消息时缩容到 0
  maxReplicaCount: 20
  triggers:
    - type: rabbitmq
      metadata:
        host: amqp://rabbitmq.production.svc.cluster.local:5672
        queueName: email-notifications
        mode: QueueLength        # QueueLength 或 MessageRate
        value: "10"              # 每个 Pod 处理 10 条消息
      authenticationRef:
        name: rabbitmq-auth

---
# RabbitMQ 认证配置
apiVersion: keda.sh/v1alpha1
kind: TriggerAuthentication
metadata:
  name: rabbitmq-auth
  namespace: production
spec:
  secretTargetRef:
    - parameter: host
      name: rabbitmq-secret
      key: connection-string
```

### HTTP 请求数触发器（需要 KEDA HTTP Add-on）

```yaml
apiVersion: http.keda.sh/v1alpha1
kind: HTTPScaledObject
metadata:
  name: my-app-http-scaler
  namespace: production
spec:
  hosts:
    - my-app.example.com
  targetPendingRequests: 100   # 每个 Pod 最多 100 个并发请求
  scaledownPeriod: 300
  replicas:
    min: 0
    max: 30
  scaleTargetRef:
    deployment: my-app
    service: my-app
    port: 80
```

---

## HPA 与 VPA 冲突问题

HPA 和 VPA 同时使用 CPU/内存指标时会产生冲突：VPA 修改 requests → HPA 重新计算 utilization → 触发错误扩缩容。

**解决方案**：

```yaml
# 方案1：HPA 使用自定义指标（非 CPU/内存），VPA 管理资源
# HPA 负责水平扩缩（基于 QPS/队列深度）
# VPA 负责垂直调整（CPU/内存 requests）

# 方案2：VPA 设置 controlledValues: RequestsOnly，HPA 用 Utilization 类型
# 但这仍然有竞争风险，不推荐

# 方案3：使用 Goldilocks（VPA Off 模式推荐值 → 人工设置 → HPA 基于这个值）
kubectl -n production annotate deployment my-app \
  goldilocks.fairwinds.com/enabled=true
```

---

## 常见问题排查

### HPA 不触发扩容

```bash
# 1. 确认 metrics-server 正常
kubectl get --raw "/apis/metrics.k8s.io/v1beta1/namespaces/production/pods" | jq .

# 2. 查看 HPA 详情和 Conditions
kubectl describe hpa my-app-hpa -n production
# 关注：
# - AbleToScale: True/False
# - ScalingActive: True/False（False 说明指标获取失败）
# - ScalingLimited: True/False（True 说明已达 min/max）

# 3. 常见原因：Pod 没有设置 CPU requests
kubectl get pod -n production -o jsonpath='{.items[*].spec.containers[*].resources.requests.cpu}'

# 4. 检查 kube-controller-manager 日志
kubectl -n kube-system logs kube-controller-manager-<node> | grep HPA | tail -50

# 5. 手动触发 CPU 压力测试
kubectl run load-test -n production --image=busybox --rm -it -- \
  sh -c "while true; do wget -q -O- http://my-app:80 > /dev/null; done"
```

### 扩容缓慢

```bash
# 检查 stabilizationWindowSeconds 设置
kubectl describe hpa my-app-hpa -n production | grep -A5 "Behavior"

# 检查 Pod 启动时间（readiness probe 影响扩容速度）
kubectl describe pod <new-pod> -n production | grep -A5 "Readiness"

# 优化：缩短 HPA 同步周期（默认 15s，最小 10s）
# 修改 kube-controller-manager 启动参数（需谨慎）
--horizontal-pod-autoscaler-sync-period=10s
```

### 缩容过激导致服务抖动

```bash
# 增大缩容稳定窗口（默认 300s，可适当增大）
kubectl patch hpa my-app-hpa -n production --type=merge -p '{
  "spec": {
    "behavior": {
      "scaleDown": {
        "stabilizationWindowSeconds": 600,
        "policies": [
          {"type": "Pods", "value": 1, "periodSeconds": 120}
        ]
      }
    }
  }
}'

# 查看扩缩容事件历史
kubectl describe hpa my-app-hpa -n production | grep -A30 "Events:"
```

---

## 生产配置建议

```yaml
# 完整的生产 HPA 配置模板
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: api-server-hpa
  namespace: production
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: api-server
  minReplicas: 3               # 最小副本数 >= 2 保证高可用
  maxReplicas: 50
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 65  # 留 35% 余量处理突发
    - type: Resource
      resource:
        name: memory
        target:
          type: Utilization
          averageUtilization: 80
  behavior:
    scaleUp:
      stabilizationWindowSeconds: 30   # 快速扩容
      policies:
        - type: Percent
          value: 100
          periodSeconds: 30
    scaleDown:
      stabilizationWindowSeconds: 600  # 慢速缩容，10 分钟
      policies:
        - type: Pods
          value: 2
          periodSeconds: 120
      selectPolicy: Min
```

```bash
# 监控 HPA 状态
watch kubectl get hpa -n production
# NAME             REFERENCE           TARGETS           MINPODS   MAXPODS   REPLICAS
# api-server-hpa   Deployment/api-server   45%/65%, 1Gi/4Gi   3         50        5
```
