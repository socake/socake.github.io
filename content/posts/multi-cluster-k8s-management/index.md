---
title: "多集群 Kubernetes 运维：跨集群管理与统一可观测"
date: 2026-04-12T17:00:00+08:00
draft: false
tags: ["Kubernetes", "多集群", "ArgoCD", "Thanos", "Loki", "Victoria Metrics", "SRE"]
categories: ["Kubernetes"]
series: ["SRE 实战手册"]
description: "多集群 K8s 运维实战：ArgoCD ApplicationSet 管理多集群、Thanos/VictoriaMetrics 跨集群监控聚合、Loki 多集群日志方案、kubeconfig 管理技巧、跨集群应用迁移"
summary: "从单集群到多集群，运维复杂度不是线性增加，而是指数级。这篇文章总结了我们管理跨地域、跨环境多套 K8s 集群的实际经验：如何用 ArgoCD ApplicationSet 统一部署、如何用 Thanos 聚合多集群指标、以及一次真实的跨集群迁移过程。"
toc: true
math: false
diagram: false
keywords: ["多集群Kubernetes", "ArgoCD ApplicationSet", "Thanos", "VictoriaMetrics", "Loki多集群", "kubeconfig管理", "集群迁移", "Hub-Spoke"]
params:
  reading_time: true
---

我们现在管理着横跨两个云平台、四套环境（生产US、生产CN、预发布、QA）的 K8s 集群。这个局面不是一开始设计好的，而是随着业务发展自然演化出来的。每增加一个集群，运维复杂度都要上一个台阶——多一套 kubeconfig、多一套监控告警、多一套日志系统，更别说跨集群的应用部署和故障排查了。

这篇文章把我们积累的多集群运维经验整理出来，重点是「统一」——统一部署、统一监控、统一日志。

## 为什么需要多集群

多集群不是追求技术复杂度，而是由实际需求驱动的：

**1. 故障隔离**
最核心的原因。单集群意味着控制平面是单点——etcd 挂了、API Server OOM 了，所有应用都完蛋。两套生产集群（US/CN）互相独立，一个区域的故障不影响另一个。

**2. 地域分布**
我们有全球用户，CN 用户访问 CN 集群延迟低。两套集群分别部署在不同云平台，也避免了对单一云厂商的锁定。

**3. 环境隔离**
生产、预发布、QA 共享集群虽然可以用 namespace 隔离，但容量争抢、配置误操作的风险始终存在。独立集群让环境隔离更彻底。

**4. 合规要求**
CN 生产数据需要在国内存储，这个监管要求本身就驱动了多集群。

**多集群的代价**：

| 代价 | 影响 |
|------|------|
| 运维复杂度 | 每个集群独立维护，升级、配置变更都要多操作一遍 |
| 资源成本 | 每个集群都有控制平面成本（managed K8s 有最低费用） |
| 跨集群通信 | 服务间调用如果跨集群，延迟和可靠性都有挑战 |
| 可观测性 | 监控和日志分散，需要聚合层 |

## 多集群拓扑模式

三种主要模式：

### Hub-Spoke 模式

一个中心集群（Hub）负责管理和部署，多个工作负载集群（Spoke）运行实际服务。Hub 不跑业务，只跑管理工具（ArgoCD、监控聚合层等）。

**适合场景**：统一的多环境管理，ArgoCD 多集群就是典型的 Hub-Spoke 实现。

### 联邦（Federation）模式

KubeFed v2 或 Admiralty 等工具，把多个集群虚拟成一个大集群来使用，支持跨集群调度。

**适合场景**：需要跨集群负载均衡、统一资源池的场景。**实际成本**：配置复杂，网络要求高，我们评估后没有采用。

### 独立集群模式

各集群完全独立运行，通过统一的部署工具（GitOps）保持配置一致性，可观测性层面通过 Thanos/Loki 聚合。

**适合场景**：大多数中型团队，包括我们目前的方案。简单，可靠，问题容易隔离。

## ArgoCD 多集群管理

我们把 ArgoCD 部署在一个独立的管理集群（argocd-cluster），通过注册外部集群的方式管理所有工作负载集群。

### 注册外部集群

```bash
# 查看当前可用的 kubeconfig context
kubectl config get-contexts

# 注册目标集群到 ArgoCD
# ArgoCD CLI 会创建一个 ServiceAccount 和 ClusterRole，获取 token
argocd cluster add prod-us-context \
  --name prod-us \
  --server https://argocd.internal.example.com

argocd cluster add prod-cn-context \
  --name prod-cn \
  --server https://argocd.internal.example.com

# 验证集群注册
argocd cluster list
# NAME     SERVER                          VERSION  STATUS
# prod-us  https://k8s-us.example.com      1.28     Successful
# prod-cn  https://k8s-cn.example.com      1.28     Successful
# qa       https://k8s-qa.example.com      1.27     Successful
```

### ApplicationSet：一套模板管理多集群

ApplicationSet 是 ArgoCD 的多集群部署利器，一个 ApplicationSet 资源可以在多个集群上自动创建 Application。

```yaml
# applicationset-all-clusters.yaml
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: order-service
  namespace: argocd
spec:
  generators:
    # 集群生成器：遍历所有注册的集群
    - clusters:
        selector:
          matchLabels:
            env: production  # 只对打了 production 标签的集群生效
        values:
          revision: main

    # 也可以用 matrix 生成器做集群×服务的笛卡尔积
  template:
    metadata:
      name: "order-service-{{name}}"  # {{name}} 是集群名
    spec:
      project: default
      source:
        repoURL: https://github.com/your-org/gitops-config
        targetRevision: "{{values.revision}}"
        path: "apps/order-service/overlays/{{metadata.labels.env}}-{{metadata.labels.region}}"
      destination:
        server: "{{server}}"  # {{server}} 是集群 API 地址
        namespace: order-service
      syncPolicy:
        automated:
          prune: true
          selfHeal: true
        syncOptions:
          - CreateNamespace=true
          - PrunePropagationPolicy=foreground
```

更复杂的场景——为不同环境使用不同的配置值：

```yaml
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: all-services
  namespace: argocd
spec:
  generators:
    - matrix:
        generators:
          # 维度一：集群列表
          - list:
              elements:
                - cluster: prod-us
                  url: https://k8s-us.example.com
                  env: prod
                  region: us
                  replicas: "3"
                  resources_preset: large
                - cluster: prod-cn
                  url: https://k8s-cn.example.com
                  env: prod
                  region: cn
                  replicas: "3"
                  resources_preset: large
                - cluster: qa
                  url: https://k8s-qa.example.com
                  env: qa
                  region: us
                  replicas: "1"
                  resources_preset: small
          # 维度二：服务列表（从 Git 目录结构自动发现）
          - git:
              repoURL: https://github.com/your-org/gitops-config
              revision: HEAD
              directories:
                - path: apps/*/
  template:
    metadata:
      name: "{{path.basename}}-{{cluster}}"
    spec:
      project: default
      source:
        repoURL: https://github.com/your-org/gitops-config
        targetRevision: HEAD
        path: "apps/{{path.basename}}/overlays/{{env}}"
        helm:
          parameters:
            - name: replicaCount
              value: "{{replicas}}"
            - name: resourcesPreset
              value: "{{resources_preset}}"
      destination:
        server: "{{url}}"
        namespace: "{{path.basename}}"
      syncPolicy:
        automated:
          prune: true
          selfHeal: true
```

### kubeconfig 多集群管理技巧

```bash
# 合并多个 kubeconfig 文件
KUBECONFIG=~/.kube/prod-us.yaml:~/.kube/prod-cn.yaml:~/.kube/qa.yaml \
  kubectl config view --merge --flatten > ~/.kube/config

# 给 context 起有意义的别名
kubectl config rename-context \
  arn:aws:eks:us-west-2:123456:cluster/prod \
  prod-us

# 查看所有 context
kubectl config get-contexts

# 快速切换（推荐安装 kubectx）
kubectx prod-us    # 切换到 US 生产
kubectx qa         # 切换到 QA

# 临时在指定集群执行命令（不切换当前 context）
kubectl --context=prod-us get pods -n order-service

# 同时查看多个集群的同一资源（需要安装 kubens）
for ctx in prod-us prod-cn qa; do
  echo "=== $ctx ==="
  kubectl --context=$ctx get pods -n order-service 2>/dev/null || echo "命名空间不存在"
done
```

推荐工具组合：
- `kubectx`/`kubens`：快速切换 context 和 namespace
- `k9s`：TUI 界面，支持多集群切换
- `kubie`：在独立 shell 中切换 context，避免并发操作时的 context 混乱

## 统一监控：Thanos 跨集群指标聚合

每个集群部署独立的 Prometheus，Thanos 在上层聚合所有集群的指标。

### 架构图

```
┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐
│   Cluster: US   │  │   Cluster: CN   │  │   Cluster: QA   │
│                 │  │                 │  │                 │
│  Prometheus     │  │  Prometheus     │  │  Prometheus     │
│  +Thanos Sidecar│  │  +Thanos Sidecar│  │  +Thanos Sidecar│
└────────┬────────┘  └────────┬────────┘  └────────┬────────┘
         │ gRPC StoreAPI      │                    │
         └──────────┬─────────┘                    │
                    └──────────────────────────────┘
                              │
                    ┌─────────▼─────────┐
                    │  Thanos Query     │  ← 统一查询入口
                    │  (管理集群)        │
                    └─────────┬─────────┘
                              │
                    ┌─────────▼─────────┐
                    │  Grafana          │  ← 统一看板
                    └───────────────────┘
```

### Thanos Sidecar 配置

在每个集群的 Prometheus 旁边部署 Thanos Sidecar：

```yaml
# prometheus-with-thanos.yaml（每个集群部署）
apiVersion: monitoring.coreos.com/v1
kind: Prometheus
metadata:
  name: prometheus
  namespace: monitoring
spec:
  replicas: 2
  externalLabels:
    # 关键：给每个集群打唯一标签，Thanos Query 用这个区分来源
    cluster: prod-us
    region: us-west-2
    env: production

  thanos:
    image: quay.io/thanos/thanos:v0.35.0
    objectStorageConfig:
      secret:
        name: thanos-objstore-secret
        key: objstore.yml

  storage:
    volumeClaimTemplate:
      spec:
        storageClassName: gp3
        resources:
          requests:
            storage: 50Gi
```

对象存储配置（S3）：

```yaml
# objstore.yml（存储在 Secret 中）
type: S3
config:
  bucket: example-thanos-metrics
  region: us-west-2
  endpoint: s3.amazonaws.com
```

### Thanos Query 配置（管理集群）

```yaml
# thanos-query-deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: thanos-query
  namespace: monitoring
spec:
  replicas: 2
  template:
    spec:
      containers:
        - name: thanos-query
          image: quay.io/thanos/thanos:v0.35.0
          args:
            - query
            - --http-address=0.0.0.0:9090
            - --grpc-address=0.0.0.0:10901
            # 注册每个集群的 Thanos Sidecar 地址
            - --store=thanos-sidecar.prod-us.svc.cluster.local:10901
            - --store=thanos-sidecar.prod-cn.example.com:10901
            - --store=thanos-sidecar.qa.svc.cluster.local:10901
            # 重复数据删除：相同的 externalLabels 的 Prometheus 副本去重
            - --query.replica-label=prometheus_replica
            - --query.auto-downsampling
```

### VictoriaMetrics 替代方案

如果觉得 Thanos 组件太多，VictoriaMetrics 的集群版是更简单的选择：

```bash
# vminsert：统一的写入端点（每个集群的 Prometheus 远程写入到这里）
# vmselect：统一的查询端点
# vmstorage：数据存储

# Prometheus remote_write 配置
remote_write:
  - url: http://vminsert.monitoring.svc:8480/insert/0/prometheus/
    queue_config:
      max_shards: 10
    write_relabel_configs:
      - target_label: cluster
        replacement: prod-us  # 注入集群标签
```

## 统一日志：Loki 多集群标签方案

我们用 Grafana Loki 做统一日志，每个集群部署 Promtail（或 Vector）作为日志采集 Agent，统一推送到中央 Loki 集群。

### Promtail 配置（每个集群）

```yaml
# promtail-config.yaml
server:
  http_listen_port: 9080

positions:
  filename: /tmp/positions.yaml

clients:
  - url: https://loki.internal.example.com/loki/api/v1/push
    tenant_id: default
    external_labels:
      # 关键：集群标识标签
      cluster: prod-us
      env: production
      region: us-west-2

scrape_configs:
  - job_name: kubernetes-pods
    kubernetes_sd_configs:
      - role: pod
    pipeline_stages:
      - cri: {}
      - labeldrop:
          # 删掉高基数标签，减少 Loki 索引压力
          - filename
      - labels:
          app:
          namespace:
          pod:
          container:
    relabel_configs:
      - source_labels: [__meta_kubernetes_namespace]
        target_label: namespace
      - source_labels: [__meta_kubernetes_pod_name]
        target_label: pod
      - source_labels: [__meta_kubernetes_pod_container_name]
        target_label: container
      - source_labels: [__meta_kubernetes_pod_label_app]
        target_label: app
```

### Loki 查询多集群日志

在 Grafana 中，LogQL 支持按标签过滤多集群日志：

```logql
# 查看所有集群的 order-service 错误日志
{app="order-service"} |= "ERROR" | logfmt | level="error"

# 只看 US 生产集群
{app="order-service", cluster="prod-us"} |= "ERROR"

# 对比两个集群的错误率（用 metric 查询）
sum by (cluster) (
  rate({app="order-service"} |= "ERROR" [5m])
)

# 跨集群搜索某个 trace ID
{} |= "trace_id=abc123"  # 自动搜索所有流
```

## 多集群网络互通方案对比

| 方案 | 延迟 | 复杂度 | 适用场景 |
|------|------|--------|---------|
| VPN（WireGuard/IPSec） | 低（直连） | 低 | 同一云平台内，或小规模跨云 |
| Service Mesh（Istio/Linkerd） | 中（sidecar overhead） | 高 | 需要细粒度流量控制、mTLS |
| Submariner | 低 | 中 | 多集群 Pod 直连，适合 K8s-native |
| 公网 + TLS | 高（公网延迟） | 低 | 跨地域，延迟不敏感的场景 |

我们的选择：CN 和 US 之间通过公网+TLS，同地域内的集群通过 VPC 对等连接（Peering）。

## 跨集群应用迁移实战

去年我们把一批服务从旧的自建 K8s 集群迁移到托管集群，这是整个过程的记录。

### 迁移准备

```bash
# 1. 导出现有资源配置
kubectl --context=old-cluster -n target-ns get deploy,svc,configmap,secret \
  -o yaml > old-cluster-resources.yaml

# 2. 检查 PV 使用情况
kubectl --context=old-cluster get pvc -n target-ns
# NAME           STATUS   VOLUME           CAPACITY   STORAGECLASS
# mysql-data     Bound    pvc-xxx-xxx      100Gi      gp2

# 3. 检查服务间依赖（哪些服务会调用这个服务）
# 可以用 Istio kiali 或手工检查 Service Discovery 配置
```

### PV 数据迁移

这是迁移中最麻烦的部分。我们用 Velero 做带数据的集群迁移：

```bash
# 在源集群安装 Velero
velero install \
  --provider aws \
  --plugins velero/velero-plugin-for-aws:v1.9.0 \
  --bucket example-velero-backup \
  --backup-location-config region=us-west-2 \
  --snapshot-location-config region=us-west-2

# 备份目标 namespace（含 PV 快照）
velero backup create target-ns-backup \
  --include-namespaces target-ns \
  --snapshot-volumes \
  --wait

# 验证备份
velero backup describe target-ns-backup

# 在目标集群安装 Velero（同样的配置）
# 然后恢复
velero restore create \
  --from-backup target-ns-backup \
  --namespace-mappings target-ns:target-ns \
  --wait
```

### 流量切换

零停机迁移的关键在于流量切换的策略：

```
阶段一：双写 + 读旧集群
  ├── 在新集群启动服务（验证功能正常）
  ├── DNS: service.example.com → 旧集群
  └── 新集群作为备用（不承接流量）

阶段二：金丝雀切流
  ├── 使用 Weighted DNS 或 ALB 权重规则
  ├── 5% → 10% → 25% → 50% → 100%
  └── 每个阶段观察 30 分钟（错误率、延迟、业务指标）

阶段三：完成迁移
  ├── DNS 完全指向新集群
  └── 旧集群服务保留 1 周（快速回滚用），再下线
```

```bash
# 使用 Route53 权重路由实现流量切换
# 旧集群记录（权重 90）
aws route53 change-resource-record-sets \
  --hosted-zone-id XXXXX \
  --change-batch '{
    "Changes": [{
      "Action": "UPSERT",
      "ResourceRecordSet": {
        "Name": "service.example.com",
        "Type": "CNAME",
        "SetIdentifier": "old-cluster",
        "Weight": 90,
        "TTL": 60,
        "ResourceRecords": [{"Value": "old-cluster-lb.us-west-2.elb.amazonaws.com"}]
      }
    }, {
      "Action": "UPSERT",
      "ResourceRecordSet": {
        "Name": "service.example.com",
        "Type": "CNAME",
        "SetIdentifier": "new-cluster",
        "Weight": 10,
        "TTL": 60,
        "ResourceRecords": [{"Value": "new-cluster-lb.us-west-2.elb.amazonaws.com"}]
      }
    }]
  }'
```

## 典型故障案例

### 案例一：集群标签缺失导致监控数据混淆

**现象**：Grafana 上某些面板的数据莫名翻倍，告警误发。

**根因**：新接入一个集群时，忘记在 Prometheus 的 `externalLabels` 中配置 `cluster` 标签，导致 Thanos Query 把这个集群的数据和另一个相同 job 名称的集群数据混合了。

**修复**：

```yaml
# 在每个集群的 Prometheus 配置中强制添加 cluster 标签
externalLabels:
  cluster: <集群名>  # 每个集群唯一，不能省略
```

**预防措施**：在 ArgoCD 的 ApplicationSet 模板中，通过 Helm values 自动注入集群名，不依赖人工填写。

### 案例二：ArgoCD 集群凭据过期导致同步失败

**现象**：某天早上发现 ArgoCD 中 CN 集群的所有 Application 都变成了 `Unknown` 状态，无法同步。

**根因**：注册集群时创建的 ServiceAccount token 有过期时间（90 天），过期后 ArgoCD 无法访问集群 API。

**临时修复**：

```bash
# 重新注册集群（重新生成 ServiceAccount token）
argocd cluster rm https://k8s-cn.example.com
argocd cluster add prod-cn-context --name prod-cn
```

**永久修复**：改用 kubeconfig 中的静态凭据，或配置 token 自动续期机制。

### 案例三：多集群 event loop 导致 kubectl 操作打到错误集群

**现象**：SRE 同事在排查 QA 问题时，不小心在生产集群执行了 `kubectl delete pod`，幸好不是关键服务。

**根因**：多个 terminal 窗口，每个窗口的 kubectl context 不同，操作时注意力在日志上，忘记确认 context。

**改进措施**：
1. 使用 `kubie ctx` 代替 `kubectx`——kubie 在独立子 shell 中切换 context，关闭 shell 自动回到原 context
2. 在 shell prompt 中显示当前 context（生产集群用红色）：

```bash
# ~/.zshrc 添加
KUBE_PS1_SYMBOL_ENABLE=true
source /opt/homebrew/opt/kube-ps1/share/kube-ps1.sh

# 生产集群用红色告警
kube_ps1_color_context() {
  case "$1" in
    *prod*) echo "red" ;;
    *)      echo "green" ;;
  esac
}

PS1='$(kube_ps1) $ '
```

3. 对生产集群操作，添加 `kubectl` 别名强制要求确认：

```bash
# 生产环境只读别名
alias kprod='kubectl --context=prod-us'
alias kprod-ro='kubectl --context=prod-us --dry-run=server'
```

多集群运维的核心挑战是**一致性管理**——确保相同的配置变更能正确地在所有集群落地，确保监控和日志能无缝汇聚，确保操作者在任何时刻都清楚自己在操作哪个集群。好的工具（ArgoCD、Thanos、Loki）解决了大部分技术问题，剩下的是团队规范和操作习惯的问题。
