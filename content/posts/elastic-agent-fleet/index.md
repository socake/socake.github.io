---
title: "Elastic Agent + Fleet：下一代统一日志采集管理实践"
date: 2025-03-06T11:44:00+08:00
draft: false
tags: ["Elastic Agent", "Fleet", "ELK", "日志", "K8s"]
categories: ["ELK Stack"]
description: "深入介绍 Elastic Agent 的统一采集架构和 Fleet 中央管理体系，涵盖 ECK 方式在 K8s 上的部署、Integration 配置、与 Filebeat 的选型对比以及生产踩坑经验。"
summary: "Filebeat + Metricbeat + Auditbeat 三个 Agent 各管一摊，配置分散难以维护。Elastic Agent 将它们统一为一个 All-in-One Agent，配合 Fleet 实现中央化管理。本文记录从部署到踩坑的完整实践过程。"
toc: true
math: false
diagram: false
series: ["ELK Stack 完全手册"]
keywords: ["Elastic Agent", "Fleet Server", "ECK", "K8s 日志", "Integration", "Filebeat 对比"]
params:
  reading_time: true
---

## Elastic Agent 是什么

在 Elastic Agent 出现之前，Elastic 生态有一堆 Beat：Filebeat 采日志、Metricbeat 采指标、Auditbeat 采审计事件、Packetbeat 采网络流量。每个 Beat 都要单独部署、单独配置、单独升级，在几十个节点上维护四五种 Beat 是一场噩梦。

Elastic Agent 是 Elastic 从 7.x 开始推出的统一采集代理，核心思路是 **All-in-One**：

- 一个二进制，覆盖日志、指标、安全事件、网络数据等所有采集场景
- 通过 **Integration** 的概念封装具体的采集配置（一个 Integration 对应一个数据源，比如 Nginx、MySQL、K8s）
- 通过 **Fleet** 实现中央化管理，无需登录每台机器修改配置文件

### 核心概念

**Fleet Server**：Agent 和 Elasticsearch/Kibana 之间的控制面。Agent 连接到 Fleet Server，获取策略（Policy），Fleet Server 把配置变更推送给所有 Agent。

**Policy（策略）**：一组 Integration 配置的集合，决定 Agent 采集哪些数据、如何处理、发往哪里。

**Integration**：封装特定数据源采集逻辑的包，从 Kibana 界面一键安装，不需要手写 Filebeat YAML。

**Enrollment Token**：Agent 注册时使用的认证令牌，绑定到特定 Policy，注册后 Agent 自动拉取该 Policy 的配置。

---

## 架构设计

```
K8s 节点（DaemonSet）
  └── Elastic Agent Pod
        ├── filebeat input（容器日志）
        ├── metricbeat input（节点/Pod 指标）
        └── auditbeat input（安全事件）
              ↓
         Fleet Server（K8s Deployment）
              ↓ 策略下发
         Elasticsearch
              ↓
         Kibana（Fleet UI + Discover + Dashboard）
```

Fleet Server 既是控制面（接收 Agent 注册、下发策略），也是数据面代理（某些场景下 Agent 数据先到 Fleet Server 再转发到 ES，但推荐直连 ES 减少延迟）。

---

## 使用 ECK 部署

ECK（Elastic Cloud on Kubernetes）是 Elastic 官方的 K8s Operator，用声明式配置管理 Elasticsearch、Kibana、Fleet Server、Elastic Agent 全套组件。

### 安装 ECK Operator

```bash
# 安装 CRD 和 Operator
kubectl create -f https://download.elastic.co/downloads/eck/2.13.0/crds.yaml
kubectl apply -f https://download.elastic.co/downloads/eck/2.13.0/operator.yaml

# 确认 Operator 运行正常
kubectl get pods -n elastic-system
# NAME                             READY   STATUS    RESTARTS
# elastic-operator-0               1/1     Running   0
```

### 部署 Elasticsearch

```yaml
apiVersion: elasticsearch.k8s.elastic.co/v1
kind: Elasticsearch
metadata:
  name: elasticsearch
  namespace: elastic-system
spec:
  version: 8.13.0
  nodeSets:
    - name: default
      count: 3
      config:
        node.roles: ["master", "data", "ingest"]
        xpack.security.enabled: true
      podTemplate:
        spec:
          containers:
            - name: elasticsearch
              resources:
                requests:
                  memory: 4Gi
                  cpu: 1
                limits:
                  memory: 4Gi
                  cpu: 2
              env:
                - name: ES_JAVA_OPTS
                  value: "-Xms2g -Xmx2g"
      volumeClaimTemplates:
        - metadata:
            name: elasticsearch-data
          spec:
            accessModes: [ReadWriteOnce]
            storageClassName: gp3
            resources:
              requests:
                storage: 100Gi
```

### 部署 Kibana

```yaml
apiVersion: kibana.k8s.elastic.co/v1
kind: Kibana
metadata:
  name: kibana
  namespace: elastic-system
spec:
  version: 8.13.0
  count: 1
  elasticsearchRef:
    name: elasticsearch
  config:
    xpack.fleet.packages:
      - name: system
        version: latest
      - name: elastic_agent
        version: latest
      - name: fleet_server
        version: latest
      - name: kubernetes
        version: latest
    xpack.fleet.agentPolicies:
      - name: Fleet Server Policy
        id: fleet-server-policy
        namespace: default
        monitoring_enabled: []
        package_policies:
          - package:
              name: fleet_server
            name: fleet_server
            id: fleet_server
      - name: K8s Monitoring Policy
        id: k8s-monitoring-policy
        namespace: default
        monitoring_enabled:
          - logs
          - metrics
        package_policies:
          - package:
              name: kubernetes
            name: kubernetes
            id: kubernetes
          - package:
              name: system
            name: system
            id: system
```

### 部署 Fleet Server

```yaml
apiVersion: agent.k8s.elastic.co/v1alpha1
kind: Agent
metadata:
  name: fleet-server
  namespace: elastic-system
spec:
  version: 8.13.0
  kibanaRef:
    name: kibana
  elasticsearchRefs:
    - name: elasticsearch
  mode: fleet
  fleetServerEnabled: true
  policyID: fleet-server-policy
  deployment:
    replicas: 1
    podTemplate:
      spec:
        serviceAccountName: fleet-server
        automountServiceAccountToken: true
        securityContext:
          runAsUser: 0
        containers:
          - name: agent
            resources:
              requests:
                memory: 256Mi
                cpu: 100m
              limits:
                memory: 512Mi
                cpu: 500m
```

### 部署 Elastic Agent（DaemonSet）

```yaml
apiVersion: agent.k8s.elastic.co/v1alpha1
kind: Agent
metadata:
  name: elastic-agent
  namespace: elastic-system
spec:
  version: 8.13.0
  kibanaRef:
    name: kibana
  fleetServerRef:
    name: fleet-server
  mode: fleet
  policyID: k8s-monitoring-policy
  daemonSet:
    podTemplate:
      spec:
        serviceAccountName: elastic-agent
        automountServiceAccountToken: true
        securityContext:
          runAsUser: 0
        tolerations:
          - operator: Exists    # 允许调度到所有节点
        containers:
          - name: agent
            resources:
              requests:
                memory: 350Mi
                cpu: 100m
              limits:
                memory: 700Mi
                cpu: 500m
            volumeMounts:
              - name: varlog
                mountPath: /var/log
                readOnly: true
              - name: varlibdockercontainers
                mountPath: /var/lib/docker/containers
                readOnly: true
              - name: proc
                mountPath: /hostfs/proc
                readOnly: true
              - name: cgroup
                mountPath: /hostfs/sys/fs/cgroup
                readOnly: true
        volumes:
          - name: varlog
            hostPath:
              path: /var/log
          - name: varlibdockercontainers
            hostPath:
              path: /var/lib/docker/containers
          - name: proc
            hostPath:
              path: /proc
          - name: cgroup
            hostPath:
              path: /sys/fs/cgroup
```

### RBAC 配置

Elastic Agent 需要访问 K8s API 来采集 Pod、Node 等元数据：

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: elastic-agent
  namespace: elastic-system
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: elastic-agent
rules:
  - apiGroups: [""]
    resources:
      - nodes
      - namespaces
      - events
      - pods
      - services
      - configmaps
      - persistentvolumes
      - persistentvolumeclaims
    verbs: ["get", "list", "watch"]
  - apiGroups: ["extensions"]
    resources: ["replicasets"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["apps"]
    resources: ["statefulsets", "deployments", "replicasets", "daemonsets"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["batch"]
    resources: ["jobs", "cronjobs"]
    verbs: ["get", "list", "watch"]
  - nonResourceURLs: ["/metrics"]
    verbs: ["get"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: elastic-agent
subjects:
  - kind: ServiceAccount
    name: elastic-agent
    namespace: elastic-system
roleRef:
  kind: ClusterRole
  name: elastic-agent
  apiGroup: rbac.authorization.k8s.io
```

---

## Integration 配置实践

部署完成后，在 Kibana Fleet 界面配置采集策略。以下是几个常用 Integration 的配置要点。

### Kubernetes Integration

K8s Integration 是最重要的 Integration，覆盖容器日志和集群指标。

在 Kibana → Fleet → Agent Policies → K8s Monitoring Policy → Add Integration → Kubernetes：

**日志采集配置**：
- Container logs：开启，路径 `/var/log/containers/*.log`，自动添加 Pod 元数据（namespace、pod name、container name、labels）
- Audit logs：按需开启（K8s API Server 审计日志路径 `/var/log/kubernetes/kube-apiserver-audit.log`）

**指标采集配置**：
- kubelet：采集节点/Pod/容器资源指标，地址 `https://${env.NODE_NAME}:10250`
- kube-state-metrics：采集 Deployment、StatefulSet 等对象状态，地址 `http://kube-state-metrics:8080`
- apiserver：采集 API Server 指标（可选）

### 自定义日志路径

如果应用日志写到非标准路径（比如 `/data/logs/app/*.log`），需要在 Integration 配置中添加自定义日志路径：

在 Kibana Fleet UI 的 Custom Logs Integration 中配置：

```yaml
# 日志路径（支持 glob 模式）
paths:
  - /data/logs/app/*.log
  - /data/logs/nginx/access*.log

# 多行日志合并（Java 异常堆栈）
multiline.pattern: '^\d{4}-\d{2}-\d{2}'
multiline.match: after
multiline.negate: true

# 自定义 tags
tags:
  - app-logs
  - production

# 自定义字段
fields:
  app: payment-service
  team: backend
```

### System Integration

System Integration 采集系统指标（CPU、内存、磁盘、网络）和系统日志（syslog、auth.log）。

在 DaemonSet 模式下，System Integration 读取宿主机的 `/var/log` 目录，需要确认 hostPath 挂载正确。

---

## 与 Filebeat/Fluent Bit 的对比

### 什么时候选 Elastic Agent

- **已有 Elastic Stack 环境**：ELK 全家桶用户，优先选 Elastic Agent，集成度最好
- **需要中央化管理**：10+ 节点，不想 SSH 到每台机器改配置，Fleet 的价值明显
- **需要同时采集日志+指标**：一个 Agent 搞定，减少运维开销
- **需要安全审计（EDR）**：Elastic Security 和 Elastic Agent 深度集成

### 什么时候选 Filebeat

- **Elastic Agent 版本不稳定期**：Filebeat 更成熟，长期维护，某些 Edge Case 处理更好
- **只需要日志采集**：不需要指标，Filebeat 更轻量
- **有大量现成的 Filebeat 配置**：迁移成本高，不值得切换

### 什么时候选 Fluent Bit

- **非 ELK 环境**：日志发往 Kafka、ClickHouse、OpenSearch、Loki 等，Fluent Bit 支持的 Output 更广
- **资源极度敏感**：Fluent Bit 用 C 写，内存占用 < 50MB，比 Go 写的 Filebeat 低得多
- **需要复杂的流处理**：Lua filter、多阶段 pipeline，Fluent Bit 更灵活

---

## Fleet 中央管理实操

### 批量升级 Agent

在 Kibana → Fleet → Agents 中，可以批量选中 Agent 执行升级：

1. 勾选需要升级的 Agent（可按 Policy 筛选）
2. 点击 "Upgrade" 按钮
3. 选择目标版本，确认

升级过程中 Agent 会重启，短暂中断采集（约 10-30 秒）。生产环境建议按批次升级，避免同时升级所有节点。

### 修改 Policy 配置

修改 Agent Policy 中的 Integration 配置后，Fleet Server 会自动将新配置推送给所有使用该 Policy 的 Agent，无需手动重启。

推送延迟通常在 30 秒以内，可以在 Agent 详情页查看 "Policy revision" 确认是否已更新。

### 查看 Agent 状态

```bash
# 如果有 kubectl 访问权限，可以查看 Agent 日志
kubectl logs -n elastic-system -l agent.k8s.elastic.co/name=elastic-agent -f

# 在 Kibana Fleet UI 中，每个 Agent 的状态一目了然：
# - Healthy：正常运行，策略已是最新版本
# - Updating：正在应用新策略
# - Degraded：某个 Integration 有错误，但 Agent 还在运行
# - Offline：Agent 失联超过心跳超时时间
```

---

## 踩坑记录

### Agent 版本必须与 ES 版本匹配

这是最容易踩的坑。Elastic Agent 和 Elasticsearch 的版本要保持一致（大版本相同），比如 ES 8.13.0 对应 Agent 8.13.x。版本不匹配会导致：

- Fleet Server 注册失败，报 `unsupported version` 错误
- Integration 包版本不兼容，策略下发失败

**解法**：始终保持 ECK 配置中所有组件的 `version` 字段一致，升级时先升 ES → Kibana → Fleet Server → Agent，顺序不能乱。

### Fleet Server 证书问题

Elastic Agent 与 Fleet Server 之间的通信默认使用 TLS，证书由 ECK 自动管理。常见问题：

1. **自签证书不受信任**：Agent 无法连接 Fleet Server，报 `x509: certificate signed by unknown authority`

   解法：在 Agent 注册命令中加 `--insecure` 参数（仅测试环境），或者正确配置 CA 证书路径。ECK 会在 `elastic-agent-fleet-server-ca` Secret 中存储 CA 证书。

2. **证书过期**：ECK 会自动轮换证书，但 Agent 有时没有及时更新，导致连接失败。

   解法：重启 Agent Pod，或在 Kibana Fleet 界面 "Unenroll" 再重新注册。

### K8s RBAC 权限不足

Elastic Agent 采集 K8s 指标需要访问 kubelet 的 `/metrics` 端点和 K8s API，常见错误：

```
Error: failed to get pods: pods is forbidden: User "system:serviceaccount:elastic-system:elastic-agent" cannot list resource "pods"
```

按照上文的 ClusterRole 配置添加所有必要权限，漏掉任何一个都会导致部分指标缺失。可以用以下命令检查权限：

```bash
kubectl auth can-i list pods \
  --as=system:serviceaccount:elastic-system:elastic-agent \
  --all-namespaces
```

### Agent 内存 OOM

默认的内存 limit 配置有时不够，特别是在节点上运行大量容器（100+）时，Agent 需要处理大量日志文件。

症状：Agent Pod 频繁重启，`kubectl describe pod` 显示 `OOMKilled`。

解法：适当提高内存 limit：

```yaml
resources:
  limits:
    memory: 1Gi    # 从 700Mi 提高到 1Gi
```

同时检查是否有日志采集循环（Agent 在 `/var/log/containers/` 采集到自己的日志，处理后又写日志，形成循环）。通过 `exclude_files` 排除 elastic-agent 自身的日志路径。

### containerd 日志格式问题

K8s 1.24+ 默认使用 containerd，日志格式与 Docker 不同：

- Docker：`/var/lib/docker/containers/<id>/<id>-json.log`，JSON 格式
- containerd：`/var/log/pods/<namespace>_<pod>_<uid>/<container>/0.log`，CRI 格式

Elastic Agent 8.x 自动处理 CRI 格式，不需要手动配置，但如果你用老版本（< 7.16）可能需要显式配置 `parsers`：

```yaml
- type: container
  paths:
    - /var/log/containers/*.log
  parsers:
    - container:
        stream: all
        format: auto   # 自动检测 docker 或 cri 格式
```

### 多集群管理

如果你有多个 K8s 集群（QA、Staging、Production），可以在同一个 Fleet 实例中管理不同集群的 Agent，通过 Policy 区分环境。建议：

- 为每个环境创建独立的 Agent Policy（qe-policy、staging-policy、prod-policy）
- 用 `tags` 区分 Agent 所属环境
- 在 Kibana 数据视图中通过 `tags` 过滤，避免环境数据混淆

---

## 数据索引与保留策略

Elastic Agent 采集的数据默认使用数据流（Data Stream），遵循 `logs-<type>-<namespace>` 和 `metrics-<type>-<namespace>` 的命名规范。

常见数据流：

| 数据流 | 内容 |
|---|---|
| `logs-kubernetes.container_logs-*` | K8s 容器日志 |
| `metrics-kubernetes.node-*` | 节点指标 |
| `metrics-kubernetes.pod-*` | Pod 指标 |
| `logs-system.syslog-*` | 系统 syslog |
| `metrics-system.cpu-*` | 系统 CPU 指标 |

通过 Index Lifecycle Management（ILM）配置数据保留策略：

```json
PUT _ilm/policy/logs-30d
{
  "policy": {
    "phases": {
      "hot": {
        "actions": {
          "rollover": {
            "max_size": "50gb",
            "max_age": "7d"
          }
        }
      },
      "warm": {
        "min_age": "7d",
        "actions": {
          "shrink": {"number_of_shards": 1},
          "forcemerge": {"max_num_segments": 1}
        }
      },
      "delete": {
        "min_age": "30d",
        "actions": {"delete": {}}
      }
    }
  }
}
```

Elastic Agent 是 Elastic 生态的未来方向，Filebeat 虽然还会维护，但新特性都在 Elastic Agent 上先落地。对于有 ELK 基础且规模在 20+ 节点以上的团队，迁移到 Elastic Agent + Fleet 能显著降低运维复杂度。
