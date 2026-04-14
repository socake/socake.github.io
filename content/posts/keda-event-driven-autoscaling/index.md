---
title: "KEDA 事件驱动弹性伸缩实战：从 HPA 的尽头到真正按业务信号扩缩"
date: 2025-02-08T10:12:00+08:00
draft: false
tags: ["Kubernetes", "KEDA", "弹性伸缩", "HPA", "消息队列"]
categories: ["云原生"]
description: "围绕 KEDA 2.19 展开的生产实战笔记：为什么 HPA 不够用、ScaledObject / ScaledJob 的正确用法、Kafka / RabbitMQ / Cron / Prometheus scaler 的真实配置、冷启动与抖动、Fallback、MultiKueue 之前的批处理队列兜底，以及我们踩过的十几个坑。"
summary: "HPA 只能看 CPU/内存，但生产环境真正的扩缩信号往往是 Kafka lag、RabbitMQ 队列深度、Prometheus 自定义指标、甚至 cron。本文把 KEDA 的架构、核心 CRD、常见 scaler 的坑和运维动作写成一份资深工程师的备忘录，不讲理论，只讲什么样的配置能在凌晨 3 点把你从告警里救出来。"
toc: true
math: false
diagram: false
keywords: ["KEDA", "ScaledObject", "ScaledJob", "HPA", "Kafka lag", "RabbitMQ", "Prometheus scaler", "event driven autoscaling"]
params:
  reading_time: true
---

## 为什么要再写一篇 KEDA

Kubernetes 自带的 HPA 已经很好用了，但只要你在生产里跑过一段时间，就会遇到几类 HPA 解决不了的场景：

- 消费 Kafka 的服务，消费者 CPU 水位只有 20%，但 consumer lag 已经冲到几十万；
- RabbitMQ 消费者 pod 数量根据队列深度扩缩，CPU 完全不是瓶颈；
- 定时任务场景：每天 09:00 业务开始，09:00 前把副本从 2 提前拉到 30，08:59 让 HPA 救你已经晚了；
- 某个业务指标只能从 Prometheus 查出来（比如 `http_requests_per_second{route="/checkout"}`），HPA 自带的 metrics server 拿不到；
- 任务型 Pod，一条消息起一个 Job，HPA 完全无法覆盖。

早年大家用的是 Prometheus Adapter + HPA external metrics，但配置链路长，维护一次等于把 Kubernetes 的半张脸撕下来。KEDA（Kubernetes Event-driven Autoscaling）就是在这个痛点上长出来的，把所有"外部事件驱动扩缩"这件事抽象成两个 CRD：`ScaledObject` 和 `ScaledJob`，再通过几十个 scaler 对接各种事件源。

这篇文章是我这一年多在多个生产集群（US/CN、qa/pre/prod 共五个集群）把 KEDA 从 2.12 升到 2.19 的笔记，只写实际会撞到的东西。

## KEDA 的架构必须先讲清楚

KEDA 不是一个"新的 HPA"，它的精髓是：**KEDA 做事件层，HPA 做扩缩执行层**。它内部的组件大致是：

```
                +----------------------+
                |  External Event      |
                |  (Kafka / MQ / ...)  |
                +----------+-----------+
                           |
                           v
  +------------------------+--------------------------+
  |                                                   |
  |    keda-operator (watches ScaledObject/Job)       |
  |       |                                           |
  |       | reconcile                                 |
  |       v                                           |
  |   创建对应的 HPA (metric: external)               |
  |                                                   |
  +------------------------+--------------------------+
                           |
                           v
               +-----------+------------+
               | keda-metrics-apiserver |  <-- HPA 通过它拿外部指标
               +-----------+------------+
                           |
                           v
                     HPA 扩缩 Deployment
```

几个关键事实：

1. KEDA Operator 看到 `ScaledObject` 之后会**在背后生成一个 HPA**，你 `kubectl get hpa` 能看到这个自动生成的 HPA。
2. KEDA 提供一个 External Metrics API Server（`keda-metrics-apiserver`），HPA 从它拿指标。
3. `minReplicaCount=0` 时，KEDA 不会让 HPA 来决定是否缩到 0，而是 keda-operator 直接操作 Deployment 的 `replicas=0`，这个过程叫 **activation**。HPA 是不能缩到 0 的，能缩到 0 这件事本身就是 KEDA 的招牌特性。
4. 2.19 之后的版本，scaler 的 trigger activity 状态会被记录在 `ScaledObject.status.triggersStatus` 里，排障时一定要看这里，不要只看 `kubectl describe`。

搞不清楚上面这几点，你调出来的 KEDA 一定是诡异的。

## 安装：别用随手搜到的老教程

我推荐的生产安装方式：

```bash
helm repo add kedacore https://kedacore.github.io/charts
helm repo update

helm upgrade --install keda kedacore/keda \
  --namespace keda-system --create-namespace \
  --version 2.19.x \
  --set prometheus.metricServer.enabled=true \
  --set prometheus.operator.enabled=true \
  --set webhooks.enabled=true \
  --set resources.operator.requests.cpu=100m \
  --set resources.operator.requests.memory=256Mi \
  --set resources.metricServer.requests.cpu=100m \
  --set resources.metricServer.requests.memory=256Mi
```

几点注意：

- `webhooks.enabled=true` 会启用 KEDA 自己的 admission webhook，会对 `ScaledObject` 做语法校验。我强烈推荐开，能挡掉 80% 的手误，比如同一个 Deployment 被两个 `ScaledObject` 绑定这种灾难。
- metricServer 和 operator 要分开定 resource。我们线上曾经出过 operator OOM 导致所有 `ScaledObject` 停摆的事故，operator 只给 128Mi 是不够的。
- 不要把 KEDA 装到应用同一个 namespace 下，放 `keda-system` 或者 `keda`。
- 版本一定要跟 Kubernetes 的版本对齐，KEDA 2.19 官方支持的 Kubernetes 范围是比较宽的，但老版本 KEDA 对 Kubernetes 1.30+ 的 HPA v2 行为有坑，别混用。

安装完后健康检查三件套：

```bash
kubectl -n keda-system get pods
kubectl get apiservice v1beta1.external.metrics.k8s.io -o yaml
kubectl get crd scaledobjects.keda.sh scaledjobs.keda.sh triggerauthentications.keda.sh
```

如果 `apiservice` 的 `available` 不是 `True`，后面任何 ScaledObject 都会报 `couldn't get external metric`，这是最常见的坑。

## ScaledObject 的字段逐个讲清楚

先看一个完整的例子，一边看一边讲：

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: order-consumer
  namespace: order
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: order-consumer
  pollingInterval: 15          # KEDA 去查 trigger 的间隔，秒
  cooldownPeriod: 300          # 从有流量缩到 0 之前的等待时间，秒
  initialCooldownPeriod: 60    # ScaledObject 刚创建后多少秒才开始走 cooldown 计时
  idleReplicaCount: 0          # 空闲时的副本数，必须小于 minReplicaCount
  minReplicaCount: 1
  maxReplicaCount: 50
  fallback:
    failureThreshold: 3
    replicas: 5                # scaler 连续失败 N 次后兜底副本数
  advanced:
    restoreToOriginalReplicaCount: false
    horizontalPodAutoscalerConfig:
      name: order-consumer-hpa
      behavior:
        scaleDown:
          stabilizationWindowSeconds: 300
          policies:
            - type: Percent
              value: 50
              periodSeconds: 60
        scaleUp:
          stabilizationWindowSeconds: 0
          policies:
            - type: Percent
              value: 100
              periodSeconds: 15
            - type: Pods
              value: 10
              periodSeconds: 15
          selectPolicy: Max
  triggers:
    - type: kafka
      metadata:
        bootstrapServers: kafka-bootstrap.kafka:9092
        consumerGroup: order-consumer
        topic: orders
        lagThreshold: "500"
        offsetResetPolicy: latest
        allowIdleConsumers: "false"
        scaleToZeroOnInvalidOffset: "false"
```

讲字段：

**pollingInterval**：KEDA 拉外部指标的间隔。默认 30s，对 Kafka lag 这种实时性要求高的我们改成 10-15。不要小于 5，太快会打爆 scaler 后端。

**cooldownPeriod**：从"最近一次有事件"到"缩到 0（或 idleReplicaCount）"的等待时间。生产一定要够长，300 秒是起步。我见过有人设 30 秒，结果一个 bursty topic 让 Pod 频繁起停，镜像拉取费都能翻一倍。

**initialCooldownPeriod**：2.13 之后加的，解决一个非常实在的痛点——创建 ScaledObject 瞬间，trigger 还没拿到数据，cooldownPeriod 立即从 0 开始，结果 Pod 被立刻缩没。我的建议：生产上只要你允许缩到 0，这个值必须设 ≥ 60。

**idleReplicaCount vs minReplicaCount**：`idleReplicaCount` 是"空闲时的副本数"，只能小于 `minReplicaCount`。场景：没事件时你想保留 0 个，有事件时最少 1 个。这是真正的 "scale to zero"，是别的方案给不了的。

**fallback**：当 scaler 连续报错（比如 Kafka 临时不可达）时，自动把副本数固定到一个安全值。`failureThreshold` 是连续失败次数，不是时间。这是 2.19 一个非常关键的属性：**只对 Value / AverageValue 类型的 metric 生效，CPU/Memory trigger 是没有 fallback 的**。官方文档里写得不显眼，但很多人踩过。

**advanced.horizontalPodAutoscalerConfig.behavior**：直接透传到 HPA 的 behavior 字段。这是 KEDA 的设计哲学——**scale 的事情交给 HPA，不自己发明轮子**。很多人用 KEDA 不配 behavior，结果抖动严重，其实问题不在 KEDA。

**restoreToOriginalReplicaCount**：删除 ScaledObject 时是否恢复原始副本数。生产推荐 `false`，因为"原始副本数"这个概念在 Deployment 被多次改动之后已经没有意义了，恢复反而会造成惊吓。

## Kafka scaler：最常用也最多坑

Kafka 是 KEDA 场景里最高频的。几个真正要命的参数：

**lagThreshold**：每个 Pod 期望承担的 lag。不是总 lag！KEDA 是这么算目标副本数的：

```
desiredReplicas = ceil(totalLag / lagThreshold)
```

所以 `lagThreshold=500` 加 `totalLag=10000`，目标副本就是 20。

**allowIdleConsumers**：默认 `false`。意思是副本数不会超过 partition 数，因为多余的 consumer 是空闲的。在 Kafka 场景下，保持默认就对了。如果你硬要开，是因为你用的是 Cooperative Sticky 分区或者 Kafka Streams 这种特殊客户端，普通消费者不要动。

**scaleToZeroOnInvalidOffset**：Kafka consumer group 没消费过时没有 offset，KEDA 拿不到 lag 怎么办？默认会报错。如果你希望这种情况缩到 0，设成 `true`。生产上建议 `false` + 配合 fallback，因为一个 offset 读不到的错误可能是 Kafka 问题，让你至少留几个副本。

**excludePersistentLag**：2.12 之后加的，非常重要。有时候 consumer group 卡在某个分区上不动（比如消息解析失败、死循环 retry），这个分区 lag 永远涨，KEDA 就会一直拉副本。开了 `excludePersistentLag=true` 之后，KEDA 会判断一个分区是不是 "lag 不动但也没消费"，是的话就不把它算进 desiredReplicas。不开这个参数，你就会见到 "副本拉到 maxReplicaCount，但是 lag 一点都不降" 的经典现象。

**认证**：生产 Kafka 一定是 SASL+TLS。请用 `TriggerAuthentication`，不要把密码往 metadata 里塞：

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: kafka-auth
  namespace: order
type: Opaque
stringData:
  sasl: "scram_sha512"
  username: "order-consumer"
  password: "xxx"
  tls: "enable"
  ca: |
    -----BEGIN CERTIFICATE-----
    ...
---
apiVersion: keda.sh/v1alpha1
kind: TriggerAuthentication
metadata:
  name: kafka-trigger-auth
  namespace: order
spec:
  secretTargetRef:
    - parameter: sasl
      name: kafka-auth
      key: sasl
    - parameter: username
      name: kafka-auth
      key: username
    - parameter: password
      name: kafka-auth
      key: password
    - parameter: tls
      name: kafka-auth
      key: tls
    - parameter: ca
      name: kafka-auth
      key: ca
```

在 `ScaledObject` 里引用：

```yaml
  triggers:
    - type: kafka
      metadata:
        bootstrapServers: kafka-0.kafka:9093
        consumerGroup: order-consumer
        topic: orders
        lagThreshold: "500"
      authenticationRef:
        name: kafka-trigger-auth
```

### 一个真实事故

曾经有一个 order-consumer，线上跑了半年都没事。某天业务上线了一个新格式，有一条消息反序列化抛异常、消费者 retry 死循环。KEDA 看到 lag 涨，10 分钟里把副本从 5 拉到 50（maxReplicaCount）。每一个副本都在对同一条消息 retry，CPU 打满，Kafka broker 上出现大量 rebalance，整个 topic 几乎不可用。

教训：
- 开 `excludePersistentLag=true`；
- 业务代码必须有 poison message 处理（转死信队列），不能死循环 retry；
- `maxReplicaCount` 不要真的放成 topic partition 数，留一定上限给自己兜底；
- 给 ScaledObject 配告警："副本数等于 maxReplicaCount 持续 5 分钟" 就是强烈的异常信号。

## RabbitMQ scaler：注意 vhost 和 queueLength

RabbitMQ scaler 相对简单但有两个陷阱：

```yaml
triggers:
  - type: rabbitmq
    metadata:
      protocol: auto
      queueName: orders
      mode: QueueLength
      value: "100"
      hostFromEnv: RABBIT_HOST
```

- **protocol**：默认 auto，KEDA 会自己猜是 amqp 还是 http。生产环境请显式写 `http`。http protocol 直接调 Management API 拿队列深度；amqp protocol 需要 queue declare，权限更大，也更容易出问题。
- **hostFromEnv vs TriggerAuthentication**：账号密码别明文写在 host 里，用 TriggerAuthentication。
- **mode: QueueLength**：目标值是"每个 Pod 的队列长度"，算法跟 Kafka 一样。
- **vhost**：在 URL 里 escape 一下，`%2F` 是默认 vhost。

RabbitMQ 和 Kafka 一个重要区别：Kafka 的 lag 是按 partition 分的，KEDA 能精细到每个 partition；RabbitMQ 没有 partition，所以你就是在 queue 上吃平均值。那就意味着：

**RabbitMQ 场景更怕抖动**。我们的做法是把 behavior 的 `scaleDown.stabilizationWindowSeconds` 开到 600，scaleUp 反而更激进，宁可多扩也不要在缩的路上抖。

## Prometheus scaler：自定义业务指标的终极方案

这个是我最喜欢的 scaler，因为它几乎能覆盖任何业务指标：

```yaml
triggers:
  - type: prometheus
    metadata:
      serverAddress: http://prometheus.monitoring:9090
      metricName: http_requests_per_second
      query: |
        sum(rate(http_requests_total{job="checkout",code!~"5.."}[2m]))
      threshold: "200"
      activationThreshold: "20"
      ignoreNullValues: "true"
```

关键字段：

- **threshold**：每个 Pod 期望承担的 QPS，算法同 Kafka。
- **activationThreshold**：从 0 到 1 启动的门槛。注意："从 0 到 1" 和 "普通扩缩" 走的是两套逻辑。如果你设 `activationThreshold=20`，那么指标必须 ≥ 20 才会从 0 启动；一旦启动，之后 desiredReplicas 是 `ceil(query / threshold)`，和 activationThreshold 无关。
- **ignoreNullValues**：查不到指标时怎么办。默认 `true`，当成 0。生产建议 `false` 并配合 fallback，因为查不到和值为 0 是两回事，前者是系统故障。
- **query**：一定要用 `rate()` + 时间窗口，不要用 instant。时间窗口建议 2m，不要小于 pollingInterval 的 2 倍。

### Prometheus scaler 的三个高频坑

**坑 1：查询返回多个 series**

KEDA 的 Prometheus scaler 要求 query 返回**单个 series**，如果返回多个，KEDA 会取第一个或者直接报错。写 query 时一定要 `sum(...)` 或者 `max(...)`。

**坑 2：时间窗口太小**

`rate(...[30s])` 会在 scrape interval 15s 的情况下非常抖，因为只有 2 个采样点。建议 rate 的窗口 ≥ 4 倍 scrape interval。

**坑 3：authModes**

Prometheus 带鉴权时用 `authModes` 配合 `TriggerAuthentication`：

```yaml
triggers:
  - type: prometheus
    metadata:
      serverAddress: https://prom.example.com
      metricName: foo
      query: sum(rate(foo_total[2m]))
      threshold: "100"
      authModes: "bearer"
    authenticationRef:
      name: prom-auth
```

## Cron scaler：定时任务的最优解

Cron scaler 是少见的不依赖外部系统的 scaler：

```yaml
triggers:
  - type: cron
    metadata:
      timezone: Asia/Shanghai
      start: "50 8 * * 1-5"
      end: "10 20 * * 1-5"
      desiredReplicas: "30"
```

陷阱：

- **timezone 是强制的**，默认是 UTC。别问我怎么知道的。
- **start 和 end 必须在同一天**。如果你要 20:00 到次日 02:00 这种跨天，就要拆两个 trigger。
- **Cron scaler 可以和其他 trigger 叠加**。KEDA 会取所有 trigger 的 "最大目标副本数"。生产上我非常喜欢 "cron 保底 + 事件驱动扩展" 的组合：Cron 保证上班时间至少 10 个副本，Kafka scaler 在 lag 涨的时候再往上加。

## ScaledJob：一条消息起一个 Job

ScaledObject 是扩缩 Deployment / StatefulSet 的，ScaledJob 是扩缩 Job 的。这个 CRD 适合的是"一条消息/一个任务各自独立、时间不可预测、执行完就完了"的场景，典型比如：视频转码、AI 推理任务、大报表生成。

一个最简单的示例：

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledJob
metadata:
  name: transcode-job
  namespace: media
spec:
  jobTargetRef:
    parallelism: 1
    completions: 1
    backoffLimit: 2
    template:
      spec:
        restartPolicy: Never
        containers:
          - name: worker
            image: registry.example.com/transcode:1.4.2
            resources:
              requests:
                cpu: "2"
                memory: "4Gi"
  pollingInterval: 15
  successfulJobsHistoryLimit: 50
  failedJobsHistoryLimit: 20
  maxReplicaCount: 200
  rollout:
    strategy: gradual
  scalingStrategy:
    strategy: "eager"
  triggers:
    - type: rabbitmq
      metadata:
        protocol: http
        queueName: transcode
        mode: QueueLength
        value: "1"
      authenticationRef:
        name: rabbit-auth
```

字段重点：

- **rollout.strategy**：`default` 或 `gradual`。default 是更新 ScaledJob 时会杀掉存在的 Job 再按新 spec 重建；gradual 是让存在的 Job 自然跑完、新任务按新 spec 起。生产一律用 gradual，你不会想一个 `kubectl apply` 把 200 个转码任务全 kill 的。
- **scalingStrategy.strategy**：`default` / `custom` / `accurate` / `eager`。`eager` 是我们在多 Pending Job 的场景下用得最多的，它会把 Pending 的 Job 也算进 "已分配" 的资源，不会因为 Pending 没跑起来就再起一批重复的。
- **successfulJobsHistoryLimit**：默认 100。保留太多会让 etcd 里堆大量 completed job，我见过 10k+ 的。生产建议 ≤ 50。

## Scale to Zero 的陷阱

缩到 0 是 KEDA 的招牌特性，但坑也集中在这里：

1. **最小副本为 0，但业务 Pod 启动慢**。比如 JVM 应用 30 秒起步，在从 0 扩到 1 的这段时间内，所有请求都没人处理。解决方案：不要让 "网关 → 0 副本服务" 的路径直接暴露给用户。要么前面有队列兜底（Kafka/RabbitMQ），要么给 Deployment 加一个极小的 idleReplicaCount=1。
2. **PDB 和 scale to 0 冲突**。某些场景下 PDB 的 minAvailable=1 会阻止 HPA 缩到 0，虽然 KEDA 是直接改 Deployment replicas，不经过 PDB。但是如果你的 Deployment 有 rollout，PDB 又生效，行为会很诡异。建议给 scale-to-zero 的服务写 PDB 时用 `maxUnavailable` 而不是 `minAvailable`。
3. **Prometheus 指标消失**。副本为 0 的时候 exporter 也没了，上层的监控就断了。一些团队会拿 "指标消失" 当告警条件，结果缩到 0 秒天天告警。给这些服务的告警加 `absent()` 的容忍。

## 监控 KEDA 本身

KEDA 自带 Prometheus metrics，重点关注几个指标：

- `keda_scaler_errors_total` 按 scaler 类型、scaled object 名称打标签，scaler 连续出错立即告警；
- `keda_scaled_object_paused` 为 1 表示 ScaledObject 被人为 paused 了（通过 annotation），生产上这个一定要报；
- `keda_scaler_metrics_value` 是每个 trigger 当前的指标值，配合业务看非常直观；
- `keda_resource_totals{type="scaled_object"}` 看 ScaledObject 总数，突增突减基本都是有人在乱 apply。

一个我一直在用的告警规则：

```yaml
- alert: KedaScalerErrors
  expr: |
    sum by (namespace, scaledObject, scaler) (
      rate(keda_scaler_errors_total[5m])
    ) > 0
  for: 5m
  labels:
    severity: warning
  annotations:
    summary: "KEDA scaler {{ $labels.scaler }} 连续报错"
    description: "ScaledObject {{ $labels.namespace }}/{{ $labels.scaledObject }} 的 {{ $labels.scaler }} scaler 过去 5 分钟持续报错，fallback 可能已经生效。"

- alert: KedaScaledObjectAtMax
  expr: |
    kube_horizontalpodautoscaler_status_current_replicas{horizontalpodautoscaler=~"keda-hpa-.*"}
      == on(namespace, horizontalpodautoscaler)
      kube_horizontalpodautoscaler_spec_max_replicas
  for: 10m
  labels:
    severity: warning
  annotations:
    summary: "KEDA ScaledObject 已达 maxReplicaCount"
    description: "{{ $labels.namespace }}/{{ $labels.horizontalpodautoscaler }} 副本数已达上限 10 分钟，检查业务是否失速。"
```

第二条规则 extremely 重要，我在好几个团队推广过。它是抓"消费者死循环 retry"类事故最灵敏的告警。

## KEDA 和 HPA 的冲突

千万不要让一个 Deployment 同时被 KEDA 和手写的 HPA 管。虽然 2.19 之后 admission webhook 会挡住这种情况，但如果你以前用 HPA、现在要切到 KEDA，记得先 `kubectl delete hpa`。

**切换步骤（零停机）**：

1. 把原 HPA 的 min/max 记下来；
2. 创建 ScaledObject，不要带 `autoscaling.keda.sh/paused-replicas` 的 annotation；
3. 等 KEDA 自动生成 `keda-hpa-<name>`；
4. 确认新 HPA 正常后再 `kubectl delete hpa <old>`。

别反过来，否则从 "老 HPA 删除" 到 "新 HPA 生效" 之间会有几秒没人管的空窗期，能扩能缩能死。

## 常见 scaler 之外值得了解的

- **aws-sqs-queue**：标准的 AWS SQS scaler，注意要用 IRSA（IAM Roles for Service Accounts），不要挂静态 AK/SK。
- **azure-servicebus**：Azure 的对应物，和 SQS 类似。
- **postgresql / mysql**：拿数据库的某个 query 结果当指标，冷门但很救命。我用过一个场景：某个表里 status=pending 的记录数 > 1000 就扩。
- **external**：KEDA 允许你写一个 gRPC 服务当 scaler，协议是 `externalscaler.proto`。任何事件源都能接进来，非常灵活，但开发和运维成本高，除非真的找不到现成的 scaler，别优先走这条路。

## 升级 KEDA 的经验

KEDA 的 CRD 有过几次字段变动（比如 `excludePersistentLag` 是新加的，`scalingModifiers` 是 2.17 的新大字段），升级时以下几件事必做：

1. 先升级 CRD，再升级 Helm chart。Helm 安装的 chart 有时候不会更新 CRD（这是 Helm 的通用问题）：

   ```bash
   kubectl apply --server-side --force-conflicts -f \
     https://github.com/kedacore/keda/releases/download/v2.19.x/keda-2.19.x-crds.yaml
   ```

2. 升级前做一次 `kubectl get scaledobjects -A -o yaml > keda-backup.yaml`。
3. 升级后立刻检查 `kubectl get apiservice v1beta1.external.metrics.k8s.io`。这玩意是跨 namespace 的单点，挂了所有外部指标 HPA 全挂。
4. 看 `keda-operator` 的日志 5 分钟，确认没有 reconcile error。

我们有过一次因为 admission webhook 的 TLS 证书过期（KEDA 自带的 cert 一年一轮转），导致 `kubectl apply scaledobject` 全部失败的事故。以后装 KEDA 我都用 `--set certificates.autoGenerated=true --set certificates.certValidity=8760h`，并配一条告警看 webhook CA 还剩多久。

## 什么场景我不推荐 KEDA

KEDA 不是银弹：

- 纯 CPU/内存场景，HPA 就够了，不要引入 KEDA 徒增复杂度；
- 需要非常精细的调度（比如 GPU 资源分配、拓扑感知），用 Kueue 或者 Volcano 更合适，KEDA 做不到；
- 大量短任务（每秒几百个）的场景，ScaledJob 会把 API server 压到冒烟，用消息队列 + 长驻 Deployment + KEDA Prometheus scaler 更稳；
- 你的团队没有任何人愿意学 KEDA 的 scaler 语义，那就用 Prometheus Adapter + HPA，至少大家都能读懂。

## 最后的几条原则

- `pollingInterval` 短一点、`cooldownPeriod` 长一点，这两个方向的不对称配置能规避大部分抖动；
- scale to 0 前，先问自己"第一次冷启动的请求怎么办"，想不清楚就别开；
- Kafka 场景一定开 `excludePersistentLag`；
- 任何 ScaledObject 都要有 fallback 和 "达 max" 告警；
- Secret / 凭据走 TriggerAuthentication，不要塞 metadata；
- KEDA operator 自己的资源 requests/limits 一定要配；
- 升级 CRD 用 server-side apply。

这是一个"写下来就很简单，但是要全部踩一遍才真的会记住"的工具。希望你把这篇文章放在手边的时候，就不用再踩一次我踩过的坑了。
