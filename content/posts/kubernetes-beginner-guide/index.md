---
title: "Kubernetes 从零开始：工程师视角的入门指南"
date: 2024-10-20T09:17:00+08:00
draft: false
tags: ["Kubernetes", "容器化", "云原生", "Docker", "入门"]
categories: ["Kubernetes"]
series: ["K8s 完全指南"]
description: "用工程师的视角讲清楚 Kubernetes 核心概念，覆盖从 Docker Compose 到 K8s 的动机、核心对象类比、kubectl 常用命令、第一个完整应用部署，以及常见报错解读。"
summary: "Docker Compose 能运行多个容器，为什么还需要 Kubernetes？本文从这个问题出发，用类比的方式讲清楚 Pod/Deployment/Service/Ingress 等核心概念，给出最常用的 kubectl 命令和完整的入门部署示例。"
toc: true
math: false
diagram: false
keywords: ["kubernetes入门", "kubectl", "pod", "deployment", "service", "ingress", "k8s教程", "容器编排"]
params:
  reading_time: true
---

我第一次接触 Kubernetes 时，被铺天盖地的概念搞得云里雾里：Pod、ReplicaSet、Deployment、Service、Ingress、ConfigMap、Secret、Namespace……文档写得很全，但就是搞不清楚这些东西之间的关系，也不明白为什么需要这么多层抽象。

后来管理了生产集群，才逐渐理解这些设计背后的逻辑。这篇文章试图用工程师最容易理解的方式，把 Kubernetes 的核心概念讲清楚。

## 为什么需要 Kubernetes

先从你已经知道的东西出发。

用 Docker Compose 运行一个三层应用：

```yaml
# docker-compose.yml
services:
  web:
    image: myapp:v1.0
    ports:
      - "80:8080"
    environment:
      - DB_HOST=db
  db:
    image: postgres:15
    volumes:
      - pgdata:/var/lib/postgresql/data
  nginx:
    image: nginx:alpine
    depends_on:
      - web
```

这能解决"在一台机器上运行多个容器"的问题。但当你的业务增长，单台机器的问题开始暴露：

- **单点故障**：那台机器挂了，所有服务都挂
- **无法横向扩展**：流量增大，你只能给那台机器加 CPU/内存（垂直扩展），有上限
- **部署更新要停机**：更新镜像时服务要中断
- **资源分配靠感觉**：不知道每个容器实际用了多少 CPU/内存，导致资源浪费或互相抢占

Kubernetes 解决的就是这些问题。它把多台机器组成一个集群，然后：

- 在集群中自动调度容器（你告诉它"运行3个副本"，它决定放在哪台机器）
- 发现容器挂了自动重启
- 支持滚动更新（新旧版本逐步替换，不停机）
- 基于资源声明做调度（"我需要 0.5 CPU 和 512MB 内存"）

## 核心概念：用类比讲清楚

### Node：机器

Node 就是集群里的机器（物理机或虚拟机）。分两种：

- **Control Plane Node（控制面）**：集群大脑，负责调度决策、存储集群状态（etcd）、接收 API 请求。小集群通常是1台，生产环境是3台做高可用。
- **Worker Node（工作节点）**：真正运行容器的机器。

```bash
# 查看集群节点
kubectl get nodes
# NAME           STATUS   ROLES           AGE   VERSION
# controlplane   Ready    control-plane   30d   v1.29.0
# worker-01      Ready    <none>          30d   v1.29.0
# worker-02      Ready    <none>          30d   v1.29.0
```

### Pod：容器的最小部署单元

**类比：Pod 是宿舍，容器是住在宿舍里的人。**

宿舍里的人共享同一个地址（IP）和一些公共资源（localhost 网络、共享 Volume）。绝大多数情况下一个 Pod 里住一个容器；Sidecar 模式下会住两个（应用容器 + 日志收集/服务网格代理）。

Pod 是 K8s 调度的最小单位——K8s 不单独调度容器，而是调度 Pod。

```yaml
# 最简单的 Pod（实际上你不会直接创建 Pod，而是通过 Deployment）
apiVersion: v1
kind: Pod
metadata:
  name: my-app
  labels:
    app: my-app
spec:
  containers:
  - name: app
    image: myapp:v1.0
    ports:
    - containerPort: 8080
    resources:
      requests:
        cpu: "100m"     # 0.1 CPU
        memory: "128Mi"
      limits:
        cpu: "500m"     # 0.5 CPU
        memory: "256Mi"
```

### Deployment：Pod 的管理者

**类比：Deployment 是连锁加盟品牌，Pod 是单家门店。**

你告诉品牌总部"我要开3家店"（replicas: 3），总部负责找地方开店、监控每家店的状态、某家店倒闭了就重新开一家。你发布新菜单（新镜像版本），总部会逐步把旧店改造成新菜单，而不是一次性全部关停。

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: my-app
  namespace: production
spec:
  replicas: 3                    # 始终维持3个Pod副本
  selector:
    matchLabels:
      app: my-app                # 管理带这个Label的Pod
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 1                # 更新时最多多出1个Pod
      maxUnavailable: 0          # 更新时不允许Pod不可用
  template:                      # Pod 的模板
    metadata:
      labels:
        app: my-app
    spec:
      containers:
      - name: app
        image: myapp:v1.0
        ports:
        - containerPort: 8080
```

Deployment 背后实际上创建了 ReplicaSet，ReplicaSet 再创建 Pod。通常你不需要直接操作 ReplicaSet。

### Service：稳定的访问入口

**类比：Service 是话务台，Pod 是接线员。**

Pod 是临时的——它随时可能被重启、被调度到不同节点，每次 IP 都会变。直接用 Pod IP 访问就像直接打接线员的工位电话，对方换了座位你就联系不上了。Service 提供一个稳定的"话务台号码"，背后对应哪个接线员（Pod）它来负责分配。

Service 通过 Label Selector 选择后端 Pod：

```yaml
apiVersion: v1
kind: Service
metadata:
  name: my-app-svc
spec:
  selector:
    app: my-app           # 选择所有带 app=my-app 标签的 Pod
  ports:
  - port: 80              # Service 对外暴露的端口
    targetPort: 8080      # 转发到 Pod 的端口
  type: ClusterIP         # 仅在集群内部可访问
```

Service 有四种类型：
- `ClusterIP`（默认）：只在集群内可访问，适合内部服务间通信
- `NodePort`：在每个节点上暴露一个端口，可从集群外访问（端口范围 30000-32767）
- `LoadBalancer`：在云环境中创建外部负载均衡器（AWS ALB、阿里云 SLB 等）
- `ExternalName`：把 Service 名映射到外部 DNS 名（适合访问外部服务）

### Ingress：HTTP 路由规则

**类比：Ingress 是大楼门卫的路由表，Service 是各个楼层。**

你告诉门卫："访问 /api/* 的人去三楼，访问 /web/* 的人去五楼"，门卫会把请求路由到对应的 Service。

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: my-ingress
  annotations:
    nginx.ingress.kubernetes.io/rewrite-target: /
spec:
  ingressClassName: nginx
  rules:
  - host: app.example.com
    http:
      paths:
      - path: /api
        pathType: Prefix
        backend:
          service:
            name: api-svc
            port:
              number: 80
      - path: /
        pathType: Prefix
        backend:
          service:
            name: frontend-svc
            port:
              number: 80
  tls:
  - hosts:
    - app.example.com
    secretName: app-tls-secret
```

Ingress 需要配合 Ingress Controller 使用（比如 Nginx Ingress Controller），Controller 才是真正的流量处理组件。

### Namespace：逻辑隔离

**类比：Namespace 是写字楼里的不同公司，大楼（集群）是共用的，但彼此有各自的门牌号。**

```bash
# 常见的 Namespace 划分
kubectl get namespaces
# default         # 没指定时的默认 Namespace
# kube-system     # K8s 系统组件（不要动）
# monitoring      # Prometheus/Grafana 等监控组件
# production      # 生产环境应用
# staging         # 预发布环境
```

Namespace 提供的是逻辑隔离，同一集群内不同 Namespace 的 Pod 默认可以互相通信（需要 NetworkPolicy 来限制）。

### ConfigMap 和 Secret：配置与密钥管理

ConfigMap 存非敏感配置，Secret 存敏感数据（密码、Token、证书）：

```yaml
# ConfigMap
apiVersion: v1
kind: ConfigMap
metadata:
  name: app-config
data:
  LOG_LEVEL: "info"
  SERVER_PORT: "8080"
  app.properties: |
    database.host=db-svc
    database.port=5432
---
# Secret（值需要 base64 编码）
apiVersion: v1
kind: Secret
metadata:
  name: app-secret
type: Opaque
data:
  DB_PASSWORD: cGFzc3dvcmQxMjM=   # echo -n 'password123' | base64
```

在 Pod 中使用：

```yaml
spec:
  containers:
  - name: app
    envFrom:
    - configMapRef:
        name: app-config    # 把整个 ConfigMap 注入为环境变量
    - secretRef:
        name: app-secret    # 把 Secret 注入为环境变量
    volumeMounts:
    - name: config-vol
      mountPath: /etc/config
  volumes:
  - name: config-vol
    configMap:
      name: app-config      # 挂载为文件
```

## kubectl 最常用命令

### 查看资源

```bash
# 基础查看
kubectl get pods                              # 当前 namespace 的 Pod
kubectl get pods -n production               # 指定 namespace
kubectl get pods -A                          # 所有 namespace
kubectl get pods -w                          # 持续监听变化
kubectl get all -n production                # 该 namespace 所有资源类型

# 详细信息（排错首选）
kubectl describe pod my-app-xxx -n production
kubectl describe node worker-01

# 以 YAML 格式输出（查看完整配置）
kubectl get deployment my-app -o yaml

# 自定义输出列
kubectl get pods -o wide                     # 显示节点IP等额外信息
kubectl get pods -o custom-columns='NAME:.metadata.name,STATUS:.status.phase,NODE:.spec.nodeName'

# 用标签过滤
kubectl get pods -l app=my-app,env=production
```

### 日志与调试

```bash
# 查看日志
kubectl logs my-app-xxx                      # 当前日志
kubectl logs my-app-xxx -f                   # 实时跟踪
kubectl logs my-app-xxx --previous           # 上一次（崩溃后的容器）的日志
kubectl logs my-app-xxx -c sidecar          # 多容器 Pod 指定容器
kubectl logs -l app=my-app --all-containers # 所有同标签 Pod 的日志

# 进入容器调试
kubectl exec -it my-app-xxx -- /bin/bash
kubectl exec -it my-app-xxx -c sidecar -- /bin/sh

# 临时启动调试容器（K8s 1.23+）
kubectl debug my-app-xxx -it --image=busybox --target=app

# 端口转发（本地调试，不走 Service）
kubectl port-forward pod/my-app-xxx 8080:8080
kubectl port-forward svc/my-app-svc 8080:80
```

### 部署与更新

```bash
# 应用配置文件
kubectl apply -f deployment.yaml
kubectl apply -f ./manifests/           # 应用目录下所有文件

# 更新镜像（滚动更新）
kubectl set image deployment/my-app app=myapp:v2.0

# 查看滚动更新状态
kubectl rollout status deployment/my-app

# 回滚
kubectl rollout undo deployment/my-app                  # 回滚到上一版本
kubectl rollout undo deployment/my-app --to-revision=2  # 回滚到指定版本
kubectl rollout history deployment/my-app               # 查看版本历史

# 扩缩容
kubectl scale deployment my-app --replicas=5

# 删除资源
kubectl delete pod my-app-xxx                # 删除后 Deployment 会自动重新创建
kubectl delete -f deployment.yaml            # 按文件删除
```

### 集群信息

```bash
# 集群基本信息
kubectl cluster-info
kubectl get nodes -o wide

# 资源使用率（需要 metrics-server）
kubectl top nodes
kubectl top pods -n production

# 事件（排查问题常用）
kubectl get events -n production --sort-by='.lastTimestamp'
kubectl get events -n production --field-selector reason=OOMKilling
```

## 第一个完整应用部署

下面是一个完整的例子：部署一个 Go HTTP 服务，包含 Deployment + Service + ConfigMap + Ingress。

```yaml
# namespace.yaml
apiVersion: v1
kind: Namespace
metadata:
  name: myapp
---
# configmap.yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: myapp-config
  namespace: myapp
data:
  LOG_LEVEL: "info"
  PORT: "8080"
---
# deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: myapp
  namespace: myapp
  labels:
    app: myapp
    version: v1.0
spec:
  replicas: 2
  selector:
    matchLabels:
      app: myapp
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 1
      maxUnavailable: 0
  template:
    metadata:
      labels:
        app: myapp
        version: v1.0
    spec:
      containers:
      - name: myapp
        image: myapp:v1.0
        ports:
        - containerPort: 8080
        envFrom:
        - configMapRef:
            name: myapp-config
        resources:
          requests:
            cpu: "100m"
            memory: "128Mi"
          limits:
            cpu: "500m"
            memory: "256Mi"
        livenessProbe:
          httpGet:
            path: /health
            port: 8080
          initialDelaySeconds: 10
          periodSeconds: 15
        readinessProbe:
          httpGet:
            path: /ready
            port: 8080
          initialDelaySeconds: 5
          periodSeconds: 10
---
# service.yaml
apiVersion: v1
kind: Service
metadata:
  name: myapp-svc
  namespace: myapp
spec:
  selector:
    app: myapp
  ports:
  - port: 80
    targetPort: 8080
---
# ingress.yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: myapp-ingress
  namespace: myapp
spec:
  ingressClassName: nginx
  rules:
  - host: myapp.example.com
    http:
      paths:
      - path: /
        pathType: Prefix
        backend:
          service:
            name: myapp-svc
            port:
              number: 80
```

部署：

```bash
kubectl apply -f namespace.yaml
kubectl apply -f configmap.yaml
kubectl apply -f deployment.yaml
kubectl apply -f service.yaml
kubectl apply -f ingress.yaml

# 验证
kubectl get all -n myapp
kubectl rollout status deployment/myapp -n myapp
```

## K8s 网络模型简介

K8s 的网络模型要求：**每个 Pod 都有唯一 IP，所有 Pod 之间可以直接通信（不需要 NAT）**。

这是怎么实现的？依赖 CNI（Container Network Interface）插件，常见的有：

- **Flannel**：最简单，用 VXLAN 叠加网络，适合学习和小集群
- **Calico**：支持 BGP 路由，性能好，支持 NetworkPolicy，生产首选之一
- **Cilium**：基于 eBPF，性能最好，可观测性强，云原生首选

不同节点上的 Pod 通信路径（以 Flannel 为例）：

```
Pod A (node1, 10.244.1.5) → veth pair → cni0 bridge → flannel0 → VXLAN 封装
→ 物理网络 → node2 物理网卡 → flannel0 解封装 → cni0 bridge → veth pair → Pod B (node2, 10.244.2.8)
```

Service 的 IP（ClusterIP）是虚拟 IP，不对应任何网络接口。流量到达 Service IP 时，由每个节点上的 `kube-proxy` 通过 iptables 或 IPVS 规则转发到后端 Pod。

## 资源请求与限制：为什么重要

Pod 的资源配置有两个字段：

```yaml
resources:
  requests:
    cpu: "100m"     # 调度依据：K8s 保证这个 Pod 至少能用到 100m CPU
    memory: "128Mi" # 调度依据：K8s 保证这个 Pod 至少有 128Mi 内存
  limits:
    cpu: "500m"     # 上限：Pod 最多用 500m CPU（超出会被限流，不会 kill）
    memory: "256Mi" # 上限：Pod 最多用 256Mi 内存（超出会被 OOM Kill）
```

**requests 是调度的依据**：K8s 在选择节点时，确保节点上剩余的可分配资源 ≥ Pod 的 requests。不设 requests = K8s 以为你什么都不需要，可能把你调度到资源紧张的节点。

**limits 是运行时上限**：CPU 超出 limit 会被限流（throttle），进程不会 kill，但会变慢。内存超出 limit 会触发 OOM Kill，容器被强制重启。

不设置资源限制会导致：
- 一个有内存泄漏的 Pod 吃掉整个节点的内存，导致其他 Pod 被 OOM Kill
- 节点变成"吵闹的邻居"，影响所有人

## 常见报错解读

### CrashLoopBackOff

容器启动后立即崩溃，K8s 不断重启它，等待时间指数增长（1s → 2s → 4s → ...）。

```bash
# 排查步骤
kubectl describe pod my-app-xxx -n production
# 看 Events 部分，找 reason=OOMKilled 或 Back-off restarting

kubectl logs my-app-xxx -n production --previous
# 看上一次崩溃前的日志，通常能看到 panic/error

# 常见原因：
# 1. 应用启动失败（配置错误、连接不上数据库）
# 2. OOM Kill（内存 limit 太小）
# 3. 容器 entrypoint 命令写错了
# 4. 健康检查探针配置太激进，还没启动完就被 kill
```

### Pending

Pod 被创建但没有被调度到任何节点。

```bash
kubectl describe pod my-app-xxx
# 看 Events，通常有 Warning FailedScheduling
# 常见原因：
# 1. 集群资源不足（CPU/内存），没有节点能满足 requests
# 2. Node Selector/Affinity 没有匹配的节点
# 3. PVC 绑定失败（存储相关）
# 4. 节点都被 taint，Pod 没有对应的 toleration

# 查看节点资源
kubectl describe node worker-01 | grep -A 10 "Allocated resources"
```

### OOMKilled

容器因为内存使用超过 limit 被 Linux OOM Killer 杀掉。

```bash
kubectl describe pod my-app-xxx
# 看到：Last State: Terminated, Reason: OOMKilled

# 处理方式：
# 1. 短期：调高 memory limit
# 2. 中期：分析内存使用，看是泄漏还是正常需求
kubectl top pod my-app-xxx -n production  # 查看实时内存使用
```

### ImagePullBackOff / ErrImagePull

镜像拉取失败。

```bash
# 常见原因
# 1. 镜像名/tag 写错了
# 2. 私有镜像仓库没配 imagePullSecret
# 3. 网络问题（节点访问不到镜像仓库）

kubectl describe pod my-app-xxx
# Events 里会有具体的错误信息，比如 "unauthorized" 或 "not found"
```

### ContainerCreating 卡住

容器还没创建起来，通常是存储问题：

```bash
kubectl describe pod my-app-xxx
# 常见原因：
# 1. PVC 还没有绑定到 PV
# 2. 挂载的 ConfigMap/Secret 不存在
# 3. 节点存储问题
```

## 总结

K8s 的学习曲线确实陡，但核心思路其实很清晰：

**你描述期望状态，K8s 负责把现实状态收敛到期望状态**。你说"我要3个副本"，K8s 就确保始终有3个在跑；某个挂了，它立刻再起一个。这个"声明式 + 控制循环"的设计贯穿了所有 K8s 资源。

入门阶段的建议：
1. 先用 minikube 或 kind 在本地跑起来，动手比看文档重要
2. 把第一个真实应用部署到 K8s，把遇到的报错一个个解决掉
3. 理解 Deployment → ReplicaSet → Pod 的层次关系
4. 学会用 `kubectl describe` 和 `kubectl logs` 排查问题

这篇文章只是入门，K8s 还有更多进阶话题：StatefulSet（有状态应用）、DaemonSet（每节点一个 Pod）、HPA（自动扩缩容）、RBAC（权限控制）、NetworkPolicy（网络隔离）……每一个都值得单独深入。
