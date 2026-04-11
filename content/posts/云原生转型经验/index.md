---
title: "云原生转型实践：从传统运维到 K8s 的迁移经验"
date: 2025-12-09T21:00:00+08:00
draft: false
tags: ["Kubernetes", "云原生", "运维", "DevOps", "职业发展"]
categories: ["博客"]
description: "云原生转型的真实经验分享：迁移动机、踩过的坑、团队适应过程，以及转型后实际收益与给后来者的建议"
summary: "这是一篇个人经验向的文章，记录了从传统虚拟机运维转向 Kubernetes 的全过程：为什么要迁移、迁移中踩了哪些坑、团队如何度过学习曲线，以及回头看哪些事情当时做对了。"
toc: true
math: false
diagram: false
keywords: ["Kubernetes", "云原生迁移", "容器化", "DevOps转型", "微服务"]
params:
  reading_time: true
---

## 为什么要上 K8s

决定迁移之前，我们面临的核心痛点有几个，说出来可能很多人都有共鸣：

**痛点一：部署慢，流程长**

上线一个服务，需要提工单申请虚拟机、等运维审批、手动配置环境、部署、测试。整个流程走完快的要两三天，慢的要一周。开发同学觉得运维是瓶颈，运维同学觉得开发不懂规范。两边摩擦越来越大。

**痛点二：资源利用率低**

按峰值申请机器，平时利用率只有 20-30%。一台 16 核机器跑一个服务，大部分时间 CPU 在 5% 以下。说出来让人心疼。

**痛点三：扩容慢，弹性差**

活动来了，流量起来，先提工单申请机器，等机器到位应急期都过了。或者长期保留大量备用机器，成本巨高。

**痛点四：环境不一致**

"在我机器上能跑" 是开发和运维关系的永恒矛盾。每个环境都是手工配置的，配置漂移不可避免。

这四个问题，K8s 都有对应的解法。但在开始之前，我想说一句实话：**K8s 不是银弹，它解决了上面的问题，但带来了新的复杂度**。网络模型、存储、安全、升级……每一个都有学习曲线。

---

## 迁移前的准备

### 应用评估：先做可行性分类

不是所有应用都适合立刻上 K8s。我们做了一个简单的三分类：

**可以直接上**：无状态服务（Web API、异步 Worker）、已经容器化的服务、配置通过环境变量注入的服务。

**需要改造**：日志写本地文件（需要改成写 stdout/stderr）、配置硬编码在代码里（需要外部化）、启动时需要初始化操作（可以用 Init Container）。

**暂时不上**：强依赖本地文件系统状态的服务、需要特殊内核版本的服务、外购软件无法修改的服务。

```bash
# 快速评估应用是否容易容器化的检查清单
# 1. 是否有本地状态？
ls /var/app/data 2>/dev/null && echo "有本地状态，需要处理" || echo "无本地状态"

# 2. 日志写哪里？
grep -r "log4j\|logging\|logback" src/ | grep "file\|FileAppender" | head -5

# 3. 配置如何读取？
grep -r "config\|properties" src/ | grep "File\|FileSystem" | head -10

# 4. 启动脚本有哪些副作用？
cat start.sh
```

### 有状态服务的处理原则

数据库（MySQL、PostgreSQL）、消息队列（RabbitMQ、Kafka）、缓存（Redis）——这些有状态服务要最后迁移，甚至可以永远不迁移。

我们的策略是：有状态服务继续跑在云上的托管服务（RDS、ElastiCache、MSK），K8s 只跑无状态的应用层。这个决策避免了大量麻烦，K8s 上的有状态服务数据管理复杂，初期不值得踩。

### 网络规划

K8s 集群的网络与现有 VPC 的互联很关键，特别是应用还在迁移期间需要新老混跑：

```
VPC CIDR: 10.0.0.0/16
├── Public Subnet: 10.0.0.0/20  (ALB/NAT)
├── Private Subnet: 10.0.16.0/20  (EC2/Node)
└── Pod CIDR: 10.100.0.0/16  (不要和现有 VPC 重叠！)
```

Pod CIDR 的选择很容易踩坑：如果和现有 VPC 或者公司内网有重叠，Pod 到其他服务的流量会路由错误。提前把网段规划好，比迁移后再改容易得多。

---

## 迁移过程中踩的坑

### 坑一：日志收集

在虚拟机上，日志写文件，logrotate 处理，集中收集相对简单。到了 K8s，Pod 随时可能漂移到不同节点，不能再依赖本地文件。

我们的解决方案是强制所有应用输出到 stdout/stderr，然后用 Fluent Bit 以 DaemonSet 的方式在每个节点采集，发送到 Loki 或 Elasticsearch。

```yaml
# Fluent Bit DaemonSet 配置片段
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: fluent-bit
  namespace: logging
spec:
  selector:
    matchLabels:
      app: fluent-bit
  template:
    spec:
      containers:
        - name: fluent-bit
          image: fluent/fluent-bit:2.2
          volumeMounts:
            - name: varlog
              mountPath: /var/log
              readOnly: true
            - name: containers
              mountPath: /var/lib/docker/containers
              readOnly: true
      volumes:
        - name: varlog
          hostPath:
            path: /var/log
        - name: containers
          hostPath:
            path: /var/lib/docker/containers
```

推这个改动到开发团队时遇到不少阻力——有些服务的日志框架已经写了十年，改起来有历史包袱。最终我们给了两个月的改造期，提供了各语言的日志配置模板，才比较顺利推完。

### 坑二：健康检查

Kubernetes 依赖 `livenessProbe` 和 `readinessProbe` 来判断 Pod 健康状态。配置不当会导致两个经典问题：

**livenessProbe 配置太激进**：应用启动慢，还没加载完就被 K8s 认为不健康，不停重启，永远起不来。

**readinessProbe 不准确**：应用实际没有 ready（还在预热缓存），但 probe 已经返回成功，流量进来导致大量错误。

```yaml
containers:
  - name: api-server
    livenessProbe:
      httpGet:
        path: /healthz/live     # 只检查进程是否存活，不检查依赖
        port: 8080
      initialDelaySeconds: 30   # 给应用足够的启动时间
      periodSeconds: 10
      failureThreshold: 3       # 连续失败 3 次才重启
    readinessProbe:
      httpGet:
        path: /healthz/ready    # 检查是否真正可以接收流量（包括依赖可用）
        port: 8080
      initialDelaySeconds: 10
      periodSeconds: 5
      failureThreshold: 2
    startupProbe:               # 处理启动慢的应用（K8s 1.18+）
      httpGet:
        path: /healthz/live
        port: 8080
      failureThreshold: 30      # 最多等 30 × 10s = 5 分钟
      periodSeconds: 10
```

一个应用应该暴露两个不同的 health endpoint：`/healthz/live`（我还活着）和 `/healthz/ready`（我准备好接流量了）。混用一个会带来坑。

### 坑三：配置外部化

大量服务的配置是通过配置文件管理的，而且配置文件里有环境差异（dev/staging/prod 用不同的数据库地址）。

迁移时需要把这些配置外部化到 ConfigMap 或者配置中心（Nacos/Apollo）。

```yaml
# ConfigMap 方式（适合非敏感配置）
apiVersion: v1
kind: ConfigMap
metadata:
  name: app-config
data:
  APP_ENV: production
  LOG_LEVEL: info
  DB_HOST: prod-db.internal
---
# Secret 方式（敏感信息，base64 编码）
apiVersion: v1
kind: Secret
metadata:
  name: app-secret
type: Opaque
stringData:                    # 用 stringData 不用手动 base64
  DB_PASSWORD: "super-secret"
  API_KEY: "abc123"
```

```yaml
# Pod 引用配置
envFrom:
  - configMapRef:
      name: app-config
  - secretRef:
      name: app-secret
```

对于复杂的配置（多层嵌套的 YAML/TOML 配置文件），可以把整个文件挂载进去：

```yaml
volumes:
  - name: config
    configMap:
      name: app-config-file
volumeMounts:
  - name: config
    mountPath: /app/config
    readOnly: true
```

### 坑四：资源 Request 和 Limit 的设置

不设 Resource Request 和 Limit 是早期最常见的错误。不设 Request，调度器无法合理分配节点；不设 Limit，一个应用内存泄漏会把整个节点拖垮。

```yaml
resources:
  requests:
    cpu: "200m"      # 调度时保证的资源
    memory: "256Mi"
  limits:
    cpu: "1000m"     # 上限（CPU 超限会被 throttle，不会被杀）
    memory: "512Mi"  # 上限（内存超限直接 OOMKill）
```

**内存 Limit 的坑**：Java 应用的 JVM 默认会使用宿主机内存的 1/4 作为堆大小，在 K8s 里会读到 Node 的内存，而不是容器的 Limit，导致实际分配远超 Limit，频繁 OOMKill。

```bash
# Java 容器需要设置 JVM 参数
JAVA_OPTS="-XX:+UseContainerSupport -XX:MaxRAMPercentage=75.0"
# UseContainerSupport 让 JVM 读容器的内存限制而不是宿主机
```

---

## 团队适应：比技术更难的是人

工具迁移是 3 个月的事，团队心智迁移是 1-2 年的事。

### 学习曲线

K8s 的学习曲线是陡的。不是说它有多难，而是概念多，而且概念之间的关系需要有整体视角才能理解。

我们的做法是：
1. **全员培训**：找了一天做工作坊，把 Pod、Deployment、Service、Ingress 这几个核心概念讲清楚，每个人动手跑一个简单的 Hello World
2. **配对排查**：前三个月，每次有人遇到 K8s 问题，资深的人不直接给答案，而是坐在一起排查，边排查边解释
3. **文档沉淀**：踩了坑就写文档，不是等有空了再写，是当天就写，趁着记忆还新鲜

```bash
# 给新手的几条最有用的命令
# 查看 Pod 为什么起不来
kubectl describe pod <pod-name> -n <namespace>

# 看 Pod 日志（包括已退出的容器）
kubectl logs <pod-name> -n <namespace> --previous

# 进入 Pod 调试
kubectl exec -it <pod-name> -n <namespace> -- /bin/sh

# 临时暴露服务做调试（不要用在生产）
kubectl port-forward svc/<service-name> 8080:80 -n <namespace>
```

### 开发和运维的边界重划

迁移 K8s 是一个机会，重新讨论开发和运维的边界。

我们最终确定的分工：开发团队写 Dockerfile 和 K8s manifest（他们最了解自己的服务需要什么），运维团队管理集群、网络、存储、安全（他们有平台层的专业知识）。双方共同维护 CI/CD 流程。

这个分工在开始推的时候有阻力——"写 Kubernetes YAML 不应该是开发的事"。但推完后反而是开发同学受益更大，他们可以自己控制服务的发布、扩容、灰度，不需要再等运维开工单。

---

## 迁移后的实际收益

做了大量铺垫，现在说说真实的收益数据（数量级供参考，具体数字各个团队差异很大）：

**部署速度**：从工单审批 + 手动部署（平均 2 天）到 CI/CD 自动发布（平均 15 分钟），端到端时间缩短 90%+。

**资源利用率**：CPU 利用率从平均 20% 提升到 50-60%，直接减少了约 40% 的机器数量，对应节省了相应的云账单。

**弹性能力**：有了 HPA，流量高峰时自动扩容，不再需要人工盯着。一次促销活动，流量 5 分钟内涨了 8 倍，K8s 自动扩到了需要的副本数，全程无人干预。

**故障恢复**：Pod 挂掉自动重启，节点挂掉 Pod 自动漂移到其他节点。MTTR（平均恢复时间）从分钟级降到秒级（对于可自愈的故障）。

**环境一致性**：开发、测试、生产跑同一个镜像，"在我机器上能跑"的问题基本消失了。

---

## 给后来者的建议

### 1. 循序渐进，不要一刀切

先迁移一两个不重要的服务练手，踩坑成本低。积累经验和信心后，再迁移核心服务。不要一开始就把最复杂的服务搬上去。

### 2. 先治理 Dockerfile，再谈编排

很多团队迁 K8s 失败，根因在 Dockerfile 写得太烂——镜像几个 GB，启动要 5 分钟，依赖关系混乱。在优化 K8s 配置之前，先把镜像做好。

```dockerfile
# 多阶段构建，减小镜像体积
FROM golang:1.22 AS builder
WORKDIR /app
COPY go.mod go.sum ./
RUN go mod download                    # 利用 layer 缓存
COPY . .
RUN CGO_ENABLED=0 go build -o server .

FROM gcr.io/distroless/static:nonroot  # 最小基础镜像
COPY --from=builder /app/server /server
USER nonroot
ENTRYPOINT ["/server"]
```

### 3. 保留回退路径

迁移期间，保持老的部署方式仍然可用，不要在迁移完成前拆掉。这样遇到 K8s 搞不定的问题，可以快速回退到老方式，不影响业务。

具体做法：在 DNS 层做切流，新旧服务并行跑，通过调整 DNS 权重或 ALB 权重来渐进切流。

### 4. 监控先行

在第一个服务迁移之前，先把监控搭好（Prometheus + Grafana，或者云厂商的托管监控）。没有监控，K8s 就是个黑盒，出了问题不知道从哪看。

```yaml
# 至少要有这几个基础告警
- 节点 CPU/内存使用率 > 85%
- Pod 持续重启（重启次数 > 5）
- PVC 使用率 > 80%
- 关键服务副本数 < 期望副本数
```

### 5. 不要被最佳实践压垮

K8s 生态里最佳实践太多了——Service Mesh、GitOps、OPA Policy、PodSecurityPolicy……全上的话，光是维护这些工具就能把团队累死。

分清楚哪些是必要的（监控、日志、基础安全），哪些是可以等业务稳定了再做的（Service Mesh、细粒度 RBAC）。一步一步来，不要一上来就搭一个超级复杂的平台。

---

## 回头看

迁到 K8s 大概一年后，现在回头看，当时最正确的几个决定是：

**把有状态服务留在托管服务上**：省去了大量麻烦，让团队专注在应用层的迁移。

**开发也要会写 K8s manifest**：一开始推有阻力，但现在开发同学对自己的服务有了更多掌控权，整体效率更高。

**不求完美，先跑起来**：第一个版本的 manifest 配置很粗糙，没有 PodDisruptionBudget，没有细粒度的资源配置。但先跑起来，再慢慢优化，比追求完美等六个月都没迁完要强。

最后，最难的不是技术，是说服人，是改变协作方式。技术问题都有解法，人的问题最需要耐心。
