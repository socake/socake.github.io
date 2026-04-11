---
title: "Thanos 实战：多 K8s 集群 Prometheus 统一监控与长期存储"
date: 2026-04-11T14:00:00+08:00
draft: false
tags: ["Thanos", "Prometheus", "Kubernetes", "可观测性", "多集群"]
categories: ["可观测性"]
description: "管理三套 EKS 集群时，从独立 Prometheus 迁移到 Thanos 统一监控的完整实践，包含 Sidecar 部署、S3 长期存储、Compact 降采样及多集群 Grafana 变量配置。"
summary: "记录我们将三套 EKS 集群的独立 Prometheus 迁移到 Thanos 统一监控体系的全过程，重点覆盖选型决策、生产配置和踩坑总结。"
toc: true
math: false
diagram: false
keywords: ["Thanos", "Prometheus", "多集群监控", "S3长期存储", "Kubernetes可观测性"]
params:
  reading_time: true
---

## 痛点：三套独立 Prometheus 的日常折磨

我们有三套 EKS 集群：US-Prod、CN-Prod 和 QA。最开始每套集群各自部署了一个 Prometheus，独立采集、独立存储。这个方案在早期只有一个集群时完全够用，但随着多集群并存，问题越来越明显。

**问题一：告警规则维护三份**。每次新增一条告警规则，要登三套集群分别操作。有一次 QA 的告警规则更新了，但 Prod 忘记同步，导致生产环境缺少一条关键告警，漏报了一个问题。

**问题二：跨集群数据无法联合查询**。业务上有一个需求：对比 US-Prod 和 CN-Prod 的某个接口 P99 延迟。两套 Prometheus 完全隔离，这个查询根本做不到，只能分开看再手动对比。

**问题三：数据保留期太短**。Prometheus 本地存储默认保留 15 天。扩大到 30 天，内存和磁盘占用会增长很多，PVC 成本显著上升。但业务方偶尔需要查 3 个月前的监控数据做容量规划。

**问题四：高可用做不到**。单个 Prometheus 实例如果挂掉，那段时间的数据就丢了。双实例方案需要自己处理数据去重，很麻烦。

这几个问题叠加起来，让我们决定引入统一的多集群监控方案。

---

## 选型：Thanos vs VictoriaMetrics

做调研时主要对比了 Thanos 和 VictoriaMetrics（以下简称 VM），两者都是解决"Prometheus 扩展性不足"这个问题的主流方案，但思路完全不同。

### VictoriaMetrics 的思路

VM 是替代 Prometheus 的存储引擎，直接提供一套兼容 Prometheus 查询协议的高性能时序数据库。它的优势是：

- **部署简单**：单二进制 VictoriaMetrics 就能替代 Prometheus + 存储，极简
- **性能好**：写入和查询性能比原生 Prometheus 强很多，压缩率也高
- **vmagent 很轻量**：用于替代 Prometheus 的采集端，支持远程写入

但对我们来说有一个顾虑：我们已经在三套集群上运行了 Prometheus，包括 kube-prometheus-stack 全套（Prometheus Operator、Alertmanager、各种 ServiceMonitor）。迁移到 VM 意味着把现有的 Prometheus 生态全部替换，改造成本非常高。另外 VM 的运维和社区资料相对 Thanos 少一些，排查问题时能找到的参考不多。

### Thanos 的思路

Thanos 不替换 Prometheus，而是在 Prometheus 旁边加一个 Sidecar，把 Prometheus 的数据"搬运"到对象存储，再提供一个全局查询层把多个 Prometheus 的数据聚合起来。对已有 Prometheus 部署几乎无侵入。

Thanos 的核心组件：
- **Sidecar**：贴着 Prometheus 跑，上传 TSDB block 到对象存储，同时暴露 gRPC 接口供 Query 查询
- **Query**：全局查询入口，聚合多个 Sidecar 和 Store Gateway 的数据，处理去重
- **Store Gateway**：从对象存储中读取历史数据，暴露 gRPC 接口
- **Compact**：对对象存储中的数据做降采样和合并，减少存储空间
- **Rule**：全局告警规则引擎，跨集群统一管理告警

**最终选择 Thanos**，原因很简单：保留现有 Prometheus Operator 生态，对业务方（开发团队自己维护的 ServiceMonitor 和 PrometheusRule）透明，只需要在运维层面加几个组件。

---

## 架构设计

```
US-Prod EKS                    CN-Prod EKS                   QA EKS
┌─────────────────┐            ┌─────────────────┐            ┌─────────────────┐
│  Prometheus     │            │  Prometheus     │            │  Prometheus     │
│  + Thanos       │            │  + Thanos       │            │  + Thanos       │
│    Sidecar      │            │    Sidecar      │            │    Sidecar      │
└────────┬────────┘            └────────┬────────┘            └────────┬────────┘
         │                              │                              │
         └──────────────────────────────┼──────────────────────────────┘
                                        │ 上传 TSDB blocks
                                        ▼
                                 AWS S3 Bucket
                                 (thanos-metrics)
                                        │
                          ┌─────────────┼─────────────┐
                          ▼             ▼             ▼
                    Store Gateway   Compact       Rule
                          │
                          └──────────────┐
                                         ▼
                                    Thanos Query
                                         │
                                         ▼
                                      Grafana
```

Thanos 的全局组件（Query、Store Gateway、Compact、Rule）部署在一个专门的"监控集群"（我们复用了 QA 集群里的一个独立 namespace），不放在生产集群里，避免影响业务。

---

## Prometheus + Sidecar 配置

使用 kube-prometheus-stack helm chart，通过 `additionalContainers` 给 Prometheus StatefulSet 注入 Thanos Sidecar。

关键的 values.yaml 配置：

```yaml
prometheus:
  prometheusSpec:
    # external_labels 是多集群区分的核心，每个集群必须不同
    externalLabels:
      cluster: us-prod
      region: us-west-2

    # 保留本地数据 2 小时，更长的数据依赖对象存储
    retention: 2h
    retentionSize: 10GB

    thanos:
      image: quay.io/thanos/thanos:v0.35.0
      objectStorageConfig:
        secret:
          type: S3
          config:
            bucket: thanos-metrics-prod
            endpoint: s3.us-west-2.amazonaws.com
            region: us-west-2

    # Sidecar 需要读取 Prometheus TSDB 目录
    storageSpec:
      volumeClaimTemplate:
        spec:
          storageClassName: gp3
          accessModes: ["ReadWriteOnce"]
          resources:
            requests:
              storage: 50Gi
```

Sidecar 在 Prometheus Pod 里跑，共享同一个 TSDB 数据卷。当 Prometheus 生成完整的 2 小时 block 后，Sidecar 负责把这个 block 上传到 S3。

上传期间 Prometheus 会继续写下一个 block，所以本地只需要保留 2-4 小时的数据，大大减少了 PVC 容量需求。

---

## S3 Bucket 配置

Thanos 需要对 S3 bucket 有 `GetObject`、`PutObject`、`DeleteObject`、`ListBucket` 权限。我们使用 IRSA（IAM Roles for Service Accounts）给 Thanos 相关 Pod 赋权，避免在配置文件里写 AK/SK。

S3 bucket policy 要注意几点：

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ThanosReadWrite",
      "Effect": "Allow",
      "Principal": {
        "AWS": "arn:aws:iam::ACCOUNT_ID:role/thanos-sidecar-role"
      },
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:ListBucket",
        "s3:GetBucketLocation"
      ],
      "Resource": [
        "arn:aws:iam::ACCOUNT_ID:s3:::thanos-metrics-prod",
        "arn:aws:iam::ACCOUNT_ID:s3:::thanos-metrics-prod/*"
      ]
    }
  ]
}
```

注意 `s3:GetBucketLocation` 权限容易漏掉，Thanos 在初始化时会调用这个 API 确认 bucket 所在区域，缺少这个权限会报一个不太直观的错误。

另外 CN-Prod 集群在阿里云 ACK 上，无法直接访问 AWS S3，我们给 CN 集群单独申请了一个阿里云 OSS bucket，Thanos 的 S3 兼容接口可以直接用：

```yaml
type: S3
config:
  bucket: thanos-metrics-cn
  endpoint: oss-cn-shanghai.aliyuncs.com
  access_key: <OSS_AK>
  secret_key: <OSS_SK>
  # 阿里云 OSS 不需要 region 字段，留空
  region: ""
  # 必须关闭 signature_version2，OSS 默认用 v4
  signature_version2: false
```

---

## 全局 Query 配置

Thanos Query 需要知道所有 Sidecar 和 Store Gateway 的地址。由于跨集群，Sidecar 通过 LoadBalancer Service 或 Ingress 暴露 gRPC 端口（10901）。

Query 的部署配置：

```yaml
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
            - --log.level=info
            - --query.replica-label=prometheus_replica
            # 用于处理来自不同 Prometheus 副本的数据去重
            - --query.replica-label=replica
            # US-Prod Sidecar
            - --endpoint=thanos-sidecar.us-prod.example.com:10901
            # CN-Prod Sidecar
            - --endpoint=thanos-sidecar.cn-prod.example.com:10901
            # QA Sidecar（同集群，用内部地址）
            - --endpoint=prometheus-operated.monitoring.svc.cluster.local:10901
            # Store Gateway（查询历史数据）
            - --endpoint=thanos-store-gateway.monitoring.svc.cluster.local:10901
          ports:
            - name: http
              containerPort: 10902
            - name: grpc
              containerPort: 10901
```

`--query.replica-label` 这个参数很关键。如果你运行了两个 Prometheus 副本（HA 模式），两个副本会采集相同的 metrics，Query 在聚合时需要知道哪些 series 是"副本"，以便去重而不是叠加。

---

## Compact 降采样配置

Compact 是 Thanos 里比较容易被忽视的组件，但它对长期存储成本非常重要。

它做三件事：
1. **合并**：把 S3 里小的 block 合并成大的，减少文件数量
2. **降采样**：把原始数据（raw）降采样成 5 分钟精度（5m）和 1 小时精度（1h）
3. **清理过期数据**：根据 retention 配置删除旧数据

```yaml
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: thanos-compact
  namespace: monitoring
spec:
  replicas: 1  # Compact 必须单副本，不支持水平扩展
  template:
    spec:
      containers:
        - name: thanos-compact
          image: quay.io/thanos/thanos:v0.35.0
          args:
            - compact
            - --log.level=info
            - --data-dir=/var/thanos/compact
            - --objstore.config-file=/etc/thanos/objstore.yaml
            # 原始数据保留 30 天
            - --retention.resolution-raw=30d
            # 5 分钟精度数据保留 90 天
            - --retention.resolution-5m=90d
            # 1 小时精度数据保留 365 天
            - --retention.resolution-1h=365d
            - --wait
            - --wait-interval=5m
          volumeMounts:
            - name: data
              mountPath: /var/thanos/compact
  volumeClaimTemplates:
    - metadata:
        name: data
      spec:
        accessModes: ["ReadWriteOnce"]
        storageClassName: gp3
        resources:
          requests:
            storage: 100Gi
```

Compact 需要一个本地磁盘作为工作目录，用于下载 block、处理后再上传。100Gi 通常足够，但如果数据量很大可能需要调整。

---

## Grafana 多集群变量配置

有了 `external_labels` 中的 `cluster` 标签之后，在 Grafana 里可以做跨集群的下拉筛选。

在 Dashboard 变量配置里添加一个 `cluster` 变量：

```
Variable Type: Query
Query: label_values(up, cluster)
Datasource: Thanos Query
```

这样 Grafana 会查询所有存活的 series 里 `cluster` 标签的值，动态生成下拉选项（`us-prod`、`cn-prod`、`qa`）。

Panel 里的 PromQL 加上 `{cluster="$cluster"}` 过滤：

```promql
# 示例：按集群筛选的 HTTP 请求 P99 延迟
histogram_quantile(
  0.99,
  sum by (le, service) (
    rate(http_request_duration_seconds_bucket{cluster="$cluster"}[5m])
  )
)
```

也可以在同一个 Panel 里对比多个集群，直接去掉 cluster 过滤，用 `cluster` 作为图例分组：

```promql
histogram_quantile(
  0.99,
  sum by (le, cluster) (
    rate(http_request_duration_seconds_bucket[5m])
  )
)
```

---

## 踩坑记录

### 坑一：Sidecar 上传 S3 持续失败

上线后发现 US-Prod 的 Sidecar 日志里一直有报错：

```
level=error ts=... caller=... msg="upload failed"
err="context deadline exceeded"
```

排查过程：
1. 先检查 IAM 权限，通过 `aws s3 ls s3://thanos-metrics-prod` 验证权限正常
2. 查看 Sidecar 的网络出口，发现集群的 NAT Gateway 有带宽限制，在业务高峰期 S3 上传被排队
3. 检查 block 大小，发现某几个 block 达到了 2GB，上传超时默认是 5 分钟

解决方案：

```yaml
# 在 objstore 配置里增加超时设置
type: S3
config:
  bucket: thanos-metrics-prod
  endpoint: s3.us-west-2.amazonaws.com
  region: us-west-2
  http_config:
    # 上传超时改为 30 分钟
    response_header_timeout: 30m
    # 连接超时
    dial_timeout: 10s
```

另外把 Prometheus 的 block 持续时间从默认的 2 小时拆分成更小的时间窗口（通过 `--storage.tsdb.min-block-duration` 调整），避免单个 block 过大。

### 坑二：Compact 时间范围重叠导致查询数据重复

这是一个非常隐蔽的问题。表现是在 Grafana 里查某个 metric，值看起来是正常值的两倍。

原因：我们在初始部署时，同时运行了两个 Compact 实例（忘记设置单副本），两个实例各自对同一个时间范围的数据做了降采样，在 S3 里生成了两份 5m block，时间范围完全重叠。

Thanos Query 在查询时如果没有配置 replica deduplication，会把两份数据都拿回来相加。

**解决方案：**

第一步：立即停掉多余的 Compact 实例，确保只有一个在运行。

第二步：找出 S3 里重叠的 block，可以用 `thanos tools bucket inspect` 命令：

```bash
thanos tools bucket inspect \
  --objstore.config-file=objstore.yaml \
  --output=table
```

第三步：手动删除重复的 block。Thanos block 的目录名是 ULID 格式，每个 block 目录下有 `meta.json`，里面记录了时间范围。找到时间范围重叠的两个 block，删除其中一个。

第四步：给 Thanos Query 配置 deduplication，即使将来出现重复数据也能正确处理：

```yaml
args:
  - query
  - --query.replica-label=prometheus_replica
  - --query.auto-downsampling  # 自动选择合适的降采样精度
```

### 坑三：CN-Prod external_labels 配置错误

CN-Prod 投产后，在 Grafana 里发现 `cluster` 下拉里没有 `cn-prod` 选项，但能看到一个奇怪的 `us-prod` 重复出现了两次。

原因：CN-Prod 的 Prometheus values.yaml 配置 `external_labels` 时 `cluster: cn-prod` 这行缩进写错了，没有生效，导致继承了默认值，而默认值恰好和 US-Prod 配置一样。

Thanos Query 在 deduplicate 时把 CN-Prod 的数据当成了 US-Prod 的副本合并掉了，CN-Prod 的数据完全不见了。

这个问题教会我一件事：`external_labels` 配置必须在部署后立即验证：

```bash
# 验证 Prometheus 实际使用的 external_labels
kubectl exec -n monitoring prometheus-0 -- \
  wget -qO- http://localhost:9090/api/v1/labels | jq '.data'

# 或者查询一个带 cluster label 的 metric
kubectl exec -n monitoring prometheus-0 -- \
  wget -qO- 'http://localhost:9090/api/v1/query?query=up' | \
  jq '.data.result[0].metric.cluster'
```

---

## 上线后的效果

运行三个月后，几个主要痛点的解决情况：

- **告警规则统一**：全部迁移到 Thanos Rule，单一配置库，GitOps 管理，三套集群同步
- **跨集群查询**：US-Prod vs CN-Prod 的指标对比在 Grafana 一个 Panel 里就能看到
- **存储成本**：本地 PVC 从每集群 200Gi 降到 50Gi，S3 长期存储的成本比 PVC 低约 70%
- **数据保留**：原始数据 30 天，降采样数据 365 天，容量规划时能看历史趋势了

对于有多集群 Prometheus 统一监控需求的场景，Thanos 是目前最成熟的方案。它的学习曲线主要在组件理解和对象存储集成上，一旦跑通就很稳定。最需要注意的两个点：`external_labels` 的正确配置是整个多集群方案的基础，以及 Compact 必须单副本运行。
