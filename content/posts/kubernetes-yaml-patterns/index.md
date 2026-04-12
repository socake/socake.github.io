---
title: "Kubernetes YAML 工程化：常用资源模板与生产最佳实践"
date: 2026-04-11T08:00:00+08:00
draft: false
tags: ["Kubernetes", "YAML", "DevOps", "运维", "云原生"]
categories: ["Kubernetes"]
description: "从反模式到生产级模板，覆盖 Deployment、StatefulSet、HPA、PDB、NetworkPolicy 等常用资源的工程化配置要点，以及实际踩坑记录。"
summary: "写好 Kubernetes YAML 不只是语法问题，更多是工程经验的沉淀。本文梳理了生产环境中常见的 YAML 反模式，并给出各类资源的完整可用模板。"
toc: true
math: false
diagram: false
series: ["K8s 完全指南"]
keywords: ["Kubernetes", "YAML", "Deployment", "StatefulSet", "HPA", "PDB", "NetworkPolicy", "生产最佳实践"]
params:
  reading_time: true
---

维护 Kubernetes 集群这几年，看过太多「能跑但不可靠」的 YAML 配置。Pod 没有资源限制、探针缺失、以 root 身份运行——这些在测试环境看起来无关紧要的问题，一旦到了生产就是定时炸弹。本文整理了我日常使用的资源模板和踩坑经验，希望能帮到同样在摸索的同学。

## YAML 反模式：最常见的几个坑

在讲模板之前，先说说反模式——这些错误我自己也犯过。

### 1. 不设置 resource limits

这是最常见也是危害最大的问题。没有 `limits` 的容器可以无限制消耗节点资源，一个内存泄漏的应用可以把整个节点打挂，进而触发连锁雪崩。

```yaml
# 错误示例 - 没有资源限制
containers:
  - name: app
    image: myapp:latest

# 正确示例
containers:
  - name: app
    image: myapp:latest
    resources:
      requests:
        cpu: "100m"
        memory: "128Mi"
      limits:
        cpu: "500m"
        memory: "512Mi"
```

`requests` 影响调度，`limits` 影响运行时限制。两者都要设，而且比例不要差太远——limits 是 requests 的 2-4 倍比较合理，否则节点超卖严重。

### 2. 没有 readinessProbe

没有就绪探针，Pod 一启动就会被加入 Service 的 Endpoints，但此时应用可能还没完成初始化。后果是新版本滚动发布时，流量打到了还没准备好的 Pod 上，用户看到 500 错误。

```yaml
# 缺少 readinessProbe 是生产事故的常见来源
readinessProbe:
  httpGet:
    path: /health
    port: 8080
  initialDelaySeconds: 10
  periodSeconds: 5
  failureThreshold: 3
```

### 3. 以 root 用户运行

容器里的 root 和宿主机的 root 不完全隔离，一旦容器逃逸，攻击者直接获得宿主机 root 权限。生产环境必须配置 `securityContext`：

```yaml
securityContext:
  runAsNonRoot: true
  runAsUser: 1000
  runAsGroup: 1000
  allowPrivilegeEscalation: false
  readOnlyRootFilesystem: true
  capabilities:
    drop:
      - ALL
```

### 4. imagePullPolicy: Always 滥用

`Always` 意味着每次 Pod 启动都要拉取镜像，在镜像仓库故障时无法启动任何 Pod。对于固定 tag 的镜像，`IfNotPresent` 更合理；只有 `latest` 这类浮动 tag 才需要 `Always`。

---

## Deployment 生产级模板

下面这个模板是我在生产环境实际使用的基础版本，覆盖了大部分生产需要的配置：

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: myapp
  namespace: production
  labels:
    app: myapp
    version: "1.0.0"
    managed-by: helm
spec:
  replicas: 3
  revisionHistoryLimit: 3
  selector:
    matchLabels:
      app: myapp
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 1
      maxUnavailable: 0      # 滚动发布期间保证零中断
  template:
    metadata:
      labels:
        app: myapp
        version: "1.0.0"
      annotations:
        prometheus.io/scrape: "true"
        prometheus.io/port: "8080"
        prometheus.io/path: "/metrics"
    spec:
      serviceAccountName: myapp-sa
      terminationGracePeriodSeconds: 60   # 给应用足够时间优雅退出
      
      # Pod 反亲和：同一应用不同副本分散到不同节点
      affinity:
        podAntiAffinity:
          preferredDuringSchedulingIgnoredDuringExecution:
            - weight: 100
              podAffinityTerm:
                labelSelector:
                  matchLabels:
                    app: myapp
                topologyKey: kubernetes.io/hostname
      
      # 跨可用区均匀分布
      topologySpreadConstraints:
        - maxSkew: 1
          topologyKey: topology.kubernetes.io/zone
          whenUnsatisfiable: DoNotSchedule
          labelSelector:
            matchLabels:
              app: myapp
      
      containers:
        - name: myapp
          image: registry.example.com/myapp:1.0.0
          imagePullPolicy: IfNotPresent
          ports:
            - name: http
              containerPort: 8080
              protocol: TCP
          
          env:
            - name: POD_NAME
              valueFrom:
                fieldRef:
                  fieldPath: metadata.name
            - name: POD_NAMESPACE
              valueFrom:
                fieldRef:
                  fieldPath: metadata.namespace
          
          envFrom:
            - configMapRef:
                name: myapp-config
            - secretRef:
                name: myapp-secret
          
          resources:
            requests:
              cpu: "100m"
              memory: "256Mi"
            limits:
              cpu: "500m"
              memory: "512Mi"
          
          # 就绪探针：控制流量接入时机
          readinessProbe:
            httpGet:
              path: /ready
              port: 8080
            initialDelaySeconds: 10
            periodSeconds: 5
            successThreshold: 1
            failureThreshold: 3
            timeoutSeconds: 3
          
          # 存活探针：判断是否需要重启，要比 readiness 宽松
          livenessProbe:
            httpGet:
              path: /health
              port: 8080
            initialDelaySeconds: 30
            periodSeconds: 10
            failureThreshold: 5
            timeoutSeconds: 5
          
          # 启动探针：给慢启动应用留时间，避免被 liveness 误杀
          startupProbe:
            httpGet:
              path: /health
              port: 8080
            failureThreshold: 30
            periodSeconds: 10
          
          securityContext:
            runAsNonRoot: true
            runAsUser: 1000
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop:
                - ALL
          
          volumeMounts:
            - name: tmp
              mountPath: /tmp
            - name: config
              mountPath: /app/config
              readOnly: true
      
      volumes:
        - name: tmp
          emptyDir: {}
        - name: config
          configMap:
            name: myapp-config
```

几个设计决策说明：

- `maxUnavailable: 0` + `maxSurge: 1`：先创建新 Pod，确认就绪后再删除旧 Pod，零中断发布
- `revisionHistoryLimit: 3`：保留最近 3 个版本的 ReplicaSet，方便快速回滚，别设太大否则浪费 etcd 空间
- `terminationGracePeriodSeconds: 60`：给应用 60 秒处理在途请求，具体值看你的业务 SLA

---

## StatefulSet 模板

有状态应用（数据库、消息队列）用 StatefulSet 管理，核心差异在于稳定的网络标识和持久化存储。

```yaml
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: redis
  namespace: production
spec:
  serviceName: redis-headless    # 必须对应 headless service 名称
  replicas: 3
  podManagementPolicy: Parallel  # 并行启动，加快滚动速度；有严格顺序依赖时用 OrderedReady
  updateStrategy:
    type: RollingUpdate
    rollingUpdate:
      partition: 0               # 灰度发布时调整此值
  selector:
    matchLabels:
      app: redis
  template:
    metadata:
      labels:
        app: redis
    spec:
      terminationGracePeriodSeconds: 60
      containers:
        - name: redis
          image: redis:7.2-alpine
          ports:
            - containerPort: 6379
              name: redis
          command:
            - redis-server
            - /etc/redis/redis.conf
          resources:
            requests:
              cpu: "200m"
              memory: "512Mi"
            limits:
              cpu: "1000m"
              memory: "2Gi"
          readinessProbe:
            exec:
              command:
                - redis-cli
                - ping
            initialDelaySeconds: 5
            periodSeconds: 5
          livenessProbe:
            exec:
              command:
                - redis-cli
                - ping
            initialDelaySeconds: 15
            periodSeconds: 10
          volumeMounts:
            - name: data
              mountPath: /data
            - name: config
              mountPath: /etc/redis
      volumes:
        - name: config
          configMap:
            name: redis-config
  
  # PVC 模板：每个 Pod 会自动创建独立的 PVC
  volumeClaimTemplates:
    - metadata:
        name: data
      spec:
        accessModes:
          - ReadWriteOnce
        storageClassName: gp3
        resources:
          requests:
            storage: 20Gi
```

StatefulSet 的 Pod 名称是确定的（`redis-0`、`redis-1`、`redis-2`），通过 headless service 可以直接用 DNS 访问：`redis-0.redis-headless.production.svc.cluster.local`。

---

## ConfigMap 与 Secret 管理

### env vs volume mount 如何选择

**用 env 注入的场景：**
- 少量简单的 key-value 配置
- 框架直接读取环境变量的情况（12-factor app）
- 不需要热更新

**用 volume mount 的场景：**
- 配置文件格式（nginx.conf、application.yaml）
- 配置量大，结构复杂
- 需要热更新（ConfigMap 变更后 volume 会自动同步，env 不会）

```yaml
# 推荐：敏感配置用 Secret，普通配置用 ConfigMap
# Secret 通过 volume 挂载，避免出现在进程环境变量中（ps aux 可见）
volumes:
  - name: db-credentials
    secret:
      secretName: db-secret
      defaultMode: 0400    # 只有 owner 可读

volumeMounts:
  - name: db-credentials
    mountPath: /run/secrets/db
    readOnly: true
```

注意：原生 Kubernetes Secret 只是 base64 编码，不是加密。生产环境建议配合 External Secrets Operator 对接 AWS Secrets Manager 或 Vault。

---

## HPA + PDB 组合：弹性与可用性双保险

HPA（水平自动扩缩）和 PDB（中断预算）要配合使用，单独用一个都有缺陷。

```yaml
# HPA：根据 CPU/内存自动扩缩副本数
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
  minReplicas: 3
  maxReplicas: 20
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 60
    - type: Resource
      resource:
        name: memory
        target:
          type: Utilization
          averageUtilization: 70
  behavior:
    scaleUp:
      stabilizationWindowSeconds: 60    # 扩容窗口：1分钟内不重复扩
      policies:
        - type: Pods
          value: 4
          periodSeconds: 60
    scaleDown:
      stabilizationWindowSeconds: 300   # 缩容窗口：稳定5分钟后才缩
      policies:
        - type: Percent
          value: 10
          periodSeconds: 60
---
# PDB：保证节点维护/驱逐时的最小可用副本数
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: myapp-pdb
  namespace: production
spec:
  minAvailable: 2        # 或者用 maxUnavailable: 1
  selector:
    matchLabels:
      app: myapp
```

`minAvailable` 和 `maxUnavailable` 选一个就好。我更倾向用 `minAvailable`，语义更直接——"最少保持几个 Pod 在线"。

---

## NetworkPolicy：默认拒绝，按需开放

默认不配 NetworkPolicy，集群内所有 Pod 可以互相访问，这在安全上是不可接受的。

```yaml
# 第一步：默认拒绝所有入站和出站
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny-all
  namespace: production
spec:
  podSelector: {}    # 匹配 namespace 内所有 Pod
  policyTypes:
    - Ingress
    - Egress
---
# 第二步：按需开放，只允许前端访问后端的 8080 端口
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-frontend-to-backend
  namespace: production
spec:
  podSelector:
    matchLabels:
      app: backend
  policyTypes:
    - Ingress
  ingress:
    - from:
        - podSelector:
            matchLabels:
              app: frontend
      ports:
        - protocol: TCP
          port: 8080
---
# 允许 DNS 出站（不允许的话 Pod 连域名都解析不了）
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-dns-egress
  namespace: production
spec:
  podSelector: {}
  policyTypes:
    - Egress
  egress:
    - ports:
        - protocol: UDP
          port: 53
        - protocol: TCP
          port: 53
```

注意：NetworkPolicy 需要 CNI 插件支持，Calico、Cilium、Flannel（部分版本）都支持。AWS VPC CNI 原生不支持，需要额外安装 Network Policy Controller。

---

## 踩坑记录

### 坑1：terminationGracePeriodSeconds 设太短

默认值是 30 秒。如果你的应用处理一个请求需要超过 30 秒（比如长时间的报表计算），在滚动发布时 Pod 被 SIGKILL 强制终止，请求直接失败。

解决方案：根据业务最长处理时间设置，同时在应用侧处理 SIGTERM 信号优雅退出。

```python
import signal
import sys

def graceful_shutdown(signum, frame):
    print("收到 SIGTERM，开始优雅退出...")
    # 停止接收新请求
    # 等待在途请求处理完毕
    sys.exit(0)

signal.signal(signal.SIGTERM, graceful_shutdown)
```

### 坑2：LivenessProbe 过于激进

我见过有人把 LivenessProbe 的 `failureThreshold` 设成 1，`periodSeconds` 设成 2。稍微有点抖动，Pod 就被重启了。更惨的是遇到流量洪峰时，liveness 探针超时触发重启，重启又更慢导致更多超时，形成重启循环（crash loop）。

正确做法：liveness 要比 readiness 宽松得多，failureThreshold 设 5 以上，同时配合 `startupProbe` 给慢启动应用充足时间。

### 坑3：imagePullPolicy 默认值踩坑

很多人不知道 `imagePullPolicy` 有个隐含规则：如果 image tag 是 `latest`，默认策略是 `Always`；否则默认是 `IfNotPresent`。

这导致一个问题：你在测试时用了 `myapp:latest`，推了新镜像，Pod 自动拉取新版本——看起来很方便。但生产环境这是灾难，因为你无法准确知道每个节点跑的是哪个版本。生产环境务必使用固定 tag（最好是 commit SHA），彻底杜绝这个隐患。

### 坑4：readOnlyRootFilesystem 导致应用崩溃

开启 `readOnlyRootFilesystem: true` 后，应用如果往 `/tmp` 或其他目录写临时文件就会失败。解决方法是挂载 `emptyDir` 到需要写入的目录：

```yaml
volumeMounts:
  - name: tmp
    mountPath: /tmp
  - name: cache
    mountPath: /app/cache

volumes:
  - name: tmp
    emptyDir: {}
  - name: cache
    emptyDir:
      sizeLimit: 1Gi    # 限制临时目录大小，防止打满节点磁盘
```

---

## 小结

K8s YAML 的工程化不是一次性的工作，而是持续迭代的过程。建议团队维护一套内部的基础模板库，新服务从模板派生，减少重复踩坑。同时结合 OPA/Kyverno 等策略引擎，在 CI 阶段或 Admission 阶段自动拦截不符合规范的配置，让规范真正落地而不依赖人工 review。
