---
title: "Kubernetes 资源管理：requests/limits/QoS/配额"
date: 2025-12-09T10:00:00+08:00
draft: false
tags: ["Kubernetes", "资源管理", "QoS", "OOM", "运维"]
categories: ["Kubernetes"]
description: "深入解析 Kubernetes 资源管理体系：requests/limits 区别与调度机制、CPU CFS 限流原理、内存 OOMKill 机制、QoS 三种类型对调度和驱逐的影响、LimitRange/ResourceQuota 配置，以及驱逐机制与 PriorityClass 优先级。"
summary: "从 CPU throttling 到内存 OOMKill，从 QoS 分类到驱逐优先级，系统梳理 Kubernetes 资源管理机制与生产调优实践。"
toc: true
math: false
diagram: false
keywords: ["Kubernetes", "requests", "limits", "QoS", "OOMKill", "LimitRange", "ResourceQuota", "Eviction", "PriorityClass"]
params:
  reading_time: true
---

## requests 与 limits 的核心区别

```
requests：调度依据（影响节点选择）
  → kube-scheduler 只看 requests 决定 Pod 放哪个节点
  → 节点可分配资源 = 节点容量 - 所有 Pod requests 之和

limits：运行时上限（影响实际使用）
  → kubelet 用 cgroups 强制限制实际使用量
  → CPU 超限：被 throttle（不被杀死）
  → 内存超限：进程被 OOMKill
```

```yaml
resources:
  requests:
    cpu: "500m"       # 调度时保留 0.5 核（0.5 CPU = 500 milliCPU）
    memory: "512Mi"   # 调度时保留 512Mi 内存
  limits:
    cpu: "2"          # 运行时最多使用 2 核
    memory: "1Gi"     # 运行时最多使用 1Gi，超出即 OOMKill
```

```bash
# 查看节点可用资源（已分配 vs 总量）
kubectl describe node <node-name> | grep -A15 "Allocated resources"

# 输出示例：
# Allocated resources:
#   Resource           Requests     Limits
#   --------           --------     ------
#   cpu                6280m (78%)  12200m (152%)    ← limits 可以超配，requests 不能超过 100%
#   memory             12Gi (75%)   18Gi (112%)
```

---

## CPU 限流机制（CFS Quota）

Linux CFS（Completely Fair Scheduler）通过 `cpu.cfs_quota_us` 和 `cpu.cfs_period_us` 实现 CPU 限制：

```
period = 100ms（默认）
quota  = limits.cpu × period

例：limits.cpu = "2"
quota = 2 × 100ms = 200ms
含义：每 100ms 内，容器最多使用 200ms CPU 时间
```

### CPU Throttling 的性能影响

```bash
# 检查容器是否被 throttle（在容器所在节点执行）
# 找到容器的 cgroup 路径
cat /sys/fs/cgroup/cpu/kubepods/pod<pod-uid>/<container-id>/cpu.stat
# 关注：
# nr_periods：总调度周期数
# nr_throttled：被 throttle 的周期数
# throttled_time：被 throttle 的总时间（纳秒）

# throttle 率 = nr_throttled / nr_periods × 100%
# 生产建议：throttle 率 > 5% 则需要上调 limits 或优化代码
```

```bash
# 通过 Prometheus 查看 CPU throttle（需要 cAdvisor）
# 指标：container_cpu_cfs_throttled_periods_total / container_cpu_cfs_periods_total
rate(container_cpu_cfs_throttled_periods_total{namespace="production"}[5m])
/ rate(container_cpu_cfs_periods_total{namespace="production"}[5m])
```

**常见误区**：limits.cpu 设得很高，但 requests.cpu 很低。调度器只看 requests，导致节点超载，所有 Pod 都频繁 throttle。

---

## 内存 OOMKill 机制

内存超出 limits 时，Linux OOM Killer 直接杀死进程，容器重启（RestartPolicy 生效）：

```bash
# 查看 OOMKill 历史
kubectl describe pod <pod-name> -n production | grep -A5 "OOMKilled"
# State: Terminated
#   Reason: OOMKilled
#   Exit Code: 137

# 查看系统 OOM 日志（在节点上执行）
dmesg | grep -i "out of memory"
dmesg | grep -i oom | tail -20

# 查看容器重启原因
kubectl get pod <pod-name> -n production -o jsonpath='{.status.containerStatuses[0].lastState}'

# Prometheus 监控 OOMKill 事件
kube_pod_container_status_last_terminated_reason{reason="OOMKilled"}
```

```bash
# 内存使用分析
kubectl top pod <pod-name> -n production --containers

# 查看 Pod 内存使用历史（需要 metrics-server）
kubectl top pod -n production --sort-by=memory | head -20
```

---

## QoS 三种类型

| QoS 类型 | 判断规则 | 调度优先级 | 驱逐优先级 |
|----------|----------|-----------|-----------|
| **Guaranteed** | 所有容器 CPU+内存都设了 requests = limits | 最优 | 最后驱逐 |
| **Burstable** | 至少一个容器设了 requests（不满足 Guaranteed） | 中等 | 中等驱逐 |
| **BestEffort** | 所有容器都没有设 requests 和 limits | 最低 | 最先驱逐 |

### 判断规则详解

```yaml
# Guaranteed：每个容器都必须同时满足 cpu requests=limits，memory requests=limits
spec:
  containers:
    - name: app
      resources:
        requests:
          cpu: "1"
          memory: "512Mi"
        limits:
          cpu: "1"          # 必须等于 requests
          memory: "512Mi"   # 必须等于 requests
    - name: sidecar
      resources:
        requests:
          cpu: "100m"
          memory: "64Mi"
        limits:
          cpu: "100m"       # 所有容器都必须满足
          memory: "64Mi"
# QoS Class: Guaranteed
```

```yaml
# Burstable：至少一个容器有 requests，但不满足 Guaranteed
spec:
  containers:
    - name: app
      resources:
        requests:
          cpu: "500m"
          memory: "256Mi"
        limits:
          cpu: "2"          # limits > requests → Burstable
          memory: "1Gi"
# QoS Class: Burstable
```

```yaml
# BestEffort：完全没有资源限制（不推荐生产使用）
spec:
  containers:
    - name: app
      # 没有 resources 字段
# QoS Class: BestEffort
```

```bash
# 查看 Pod QoS Class
kubectl get pod <pod-name> -n production -o jsonpath='{.status.qosClass}'

# 批量查看
kubectl get pods -n production -o custom-columns='NAME:.metadata.name,QOS:.status.qosClass'
```

### QoS 对调度和驱逐的影响

```
节点内存压力触发驱逐顺序：
1. BestEffort Pod（首先被驱逐）
2. Burstable Pod（实际使用超过 requests 的部分）
3. Guaranteed Pod（最后驱逐，OOM score adj = -997）

CPU 压力下（throttle 而非驱逐）：
- Guaranteed Pod 有独占 CPU 份额
- BestEffort Pod 在 CPU 紧张时几乎得不到时间片
```

---

## LimitRange：命名空间默认值

```yaml
apiVersion: v1
kind: LimitRange
metadata:
  name: production-limits
  namespace: production
spec:
  limits:
    # Container 级别限制
    - type: Container
      default:                    # 不设 limits 时的默认值
        cpu: "500m"
        memory: "512Mi"
      defaultRequest:             # 不设 requests 时的默认值
        cpu: "100m"
        memory: "128Mi"
      max:                        # 允许设置的最大值
        cpu: "8"
        memory: "16Gi"
      min:                        # 允许设置的最小值
        cpu: "10m"
        memory: "32Mi"
      maxLimitRequestRatio:       # limits/requests 最大比率（防止过度超配）
        cpu: "10"
        memory: "4"

    # Pod 级别限制（所有容器之和）
    - type: Pod
      max:
        cpu: "16"
        memory: "32Gi"

    # PVC 大小限制
    - type: PersistentVolumeClaim
      max:
        storage: "100Gi"
      min:
        storage: "1Gi"
```

```bash
# 查看 LimitRange
kubectl describe limitrange production-limits -n production

# 验证：创建没有 resources 的 Pod，会自动注入默认值
kubectl run test-pod --image=nginx -n production
kubectl describe pod test-pod -n production | grep -A10 "Limits"
```

---

## ResourceQuota：命名空间总量限制

```yaml
apiVersion: v1
kind: ResourceQuota
metadata:
  name: production-quota
  namespace: production
spec:
  hard:
    # 计算资源
    requests.cpu: "50"
    requests.memory: "100Gi"
    limits.cpu: "100"
    limits.memory: "200Gi"

    # 存储资源
    requests.storage: "500Gi"
    persistentvolumeclaims: "20"
    ebs-gp3.storageclass.storage.k8s.io/requests.storage: "200Gi"  # 特定 SC 配额

    # 对象数量
    pods: "100"
    services: "30"
    services.loadbalancers: "5"
    services.nodeports: "0"     # 禁止使用 NodePort
    secrets: "50"
    configmaps: "50"
    replicationcontrollers: "0"
    deployments.apps: "20"

    # 按 QoS 限制
    requests.cpu.Guaranteed: "20"  # Guaranteed 类 Pod 的 CPU requests 上限
```

```bash
# 查看配额使用情况
kubectl describe resourcequota production-quota -n production

# 输出示例：
# Name:            production-quota
# Namespace:       production
# Resource         Used    Hard
# --------         ---     ----
# limits.cpu       8500m   100
# limits.memory    17Gi    200Gi
# pods             12      100
# requests.cpu     4250m   50
# requests.memory  8Gi     100Gi
```

---

## 资源设置最佳实践

### 如何合理设置 requests

```bash
# 方法1：查看历史 P95 使用量（Prometheus）
# CPU P95
histogram_quantile(0.95,
  rate(container_cpu_usage_seconds_total{namespace="production",container="my-app"}[7d])
)

# 内存 P95
quantile_over_time(0.95,
  container_memory_working_set_bytes{namespace="production",container="my-app"}[7d]
)

# 方法2：使用 VPA Off 模式获取推荐值
kubectl apply -f - <<EOF
apiVersion: autoscaling.k8s.io/v1
kind: VerticalPodAutoscaler
metadata:
  name: my-app-vpa-advisor
  namespace: production
spec:
  targetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: my-app
  updatePolicy:
    updateMode: "Off"   # 仅推荐，不自动修改
EOF

# 7天后查看推荐值
kubectl describe vpa my-app-vpa-advisor -n production
```

### 推荐的资源配置策略

| 应用类型 | requests | limits | QoS 目标 |
|----------|----------|--------|----------|
| 核心服务（API/DB） | P50 使用量 | P95-P99 | Guaranteed |
| 普通业务服务 | P50 使用量 | 2-3× requests | Burstable |
| 批处理任务 | 实际需求 | 实际需求 × 1.2 | Burstable |
| 开发/测试 | 最小可运行 | 适当放大 | BestEffort 可接受 |

```yaml
# 生产 API 服务推荐配置
resources:
  requests:
    cpu: "500m"       # 根据 P50 监控设置
    memory: "512Mi"
  limits:
    cpu: "2"          # 允许突发使用
    memory: "1Gi"     # 内存建议和 requests 接近，防止 OOM
```

---

## 驱逐（Eviction）机制

kubelet 在节点资源紧张时触发驱逐：

```yaml
# kubelet 驱逐阈值配置（/etc/kubernetes/kubelet-config.yaml）
evictionHard:
  memory.available: "200Mi"     # 可用内存低于 200Mi 触发驱逐
  nodefs.available: "10%"       # 节点磁盘剩余低于 10%
  nodefs.inodesFree: "5%"
  imagefs.available: "15%"

evictionSoft:
  memory.available: "500Mi"     # 软阈值，持续 2 分钟才触发
evictionSoftGracePeriod:
  memory.available: "2m"

evictionMinimumReclaim:         # 驱逐后至少回收多少资源
  memory.available: "500Mi"
  nodefs.available: "1Gi"
```

```bash
# 查看节点驱逐事件
kubectl describe node <node-name> | grep -A5 "Conditions"
kubectl get events --field-selector reason=Evicted -n production

# 查看被驱逐的 Pod
kubectl get pods -n production --field-selector=status.phase=Failed | grep Evicted

# 清理已驱逐的 Pod
kubectl get pods -n production --field-selector=status.phase=Failed \
  -o name | xargs kubectl delete -n production
```

---

## PriorityClass：调度与驱逐优先级

```yaml
# 定义优先级类（数值越大优先级越高）
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: critical-service
value: 1000000          # 系统级：~2147483647，用户自定义最大建议 1000000
globalDefault: false
preemptionPolicy: PreemptLowerPriority  # 允许抢占低优先级 Pod
description: "核心业务服务，不允许被驱逐"

---
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: batch-job
value: 100
globalDefault: false
preemptionPolicy: Never    # 不抢占，等待资源
description: "批处理任务"
```

```yaml
# Pod 引用 PriorityClass
spec:
  priorityClassName: critical-service
  containers:
    - name: app
      image: my-app:v1.0.0
```

```bash
# 查看集群内所有 PriorityClass
kubectl get priorityclass

# 内置优先级（不要手动创建同名）：
# system-cluster-critical：2000000000（CoreDNS 等）
# system-node-critical：2000001000（kube-proxy 等）
kubectl get priorityclass | grep system
```

---

## 综合排查命令

```bash
# 查看节点资源压力
kubectl describe nodes | grep -A5 "Conditions" | grep -E "MemoryPressure|DiskPressure|PIDPressure"

# 查看资源使用 Top
kubectl top nodes
kubectl top pods -n production --sort-by=cpu | head -20
kubectl top pods -n production --sort-by=memory | head -20

# 查看所有命名空间 ResourceQuota 使用情况
kubectl get resourcequota -A

# 找出没有设置 resources 的 Pod（BestEffort）
kubectl get pods -A -o json | jq '.items[] | select(.status.qosClass=="BestEffort") | {ns:.metadata.namespace, name:.metadata.name}'

# 找出内存使用超过 requests 80% 的 Pod（需要 Prometheus）
# container_memory_working_set_bytes / (kube_pod_container_resource_requests{resource="memory"}) > 0.8
```
