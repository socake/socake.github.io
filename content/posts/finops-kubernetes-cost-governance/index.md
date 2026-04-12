---
title: "FinOps 实践：Kubernetes 成本治理体系建设"
date: 2026-04-12T10:00:00+08:00
draft: false
tags: ["FinOps", "Kubernetes", "成本优化", "Karpenter", "OpenCost", "云原生"]
categories: ["云原生运维"]
description: "从云成本失控到建立完整的 FinOps 治理体系，涵盖 OpenCost 部署、成本分摊、VPA 推荐、Spot 策略和自动化告警，附真实降本案例。"
summary: "一套完整的 Kubernetes FinOps 落地路径：如何识别僵尸资源、配置成本分摊模型、利用 Karpenter 降低节点成本，以及如何将月账单从 $50k 压到 $30k。"
toc: true
math: false
diagram: false
keywords: ["FinOps", "Kubernetes 成本优化", "OpenCost", "Karpenter", "云成本治理", "成本分摊"]
params:
  reading_time: true
---

## 为什么 K8s 集群的账单总比预期高一倍

去年接手一个多云 K8s 平台，第一个月账单出来是 $52k，研发团队说"我们就跑了几个微服务"。花了两周把账单拆开看，发现：

- **节点闲置率 47%**：requests 填满了调度，但实际 CPU 使用率平均 18%
- **PVC 孤儿**：删了 Pod，没人删 PVC，有 60 多个共计 4TB 的 EBS 卷躺在那里计费
- **Spot 节点使用率接近零**：团队配置了 On-Demand 节点组，Spot 节点组"怕不稳定"没敢用
- **镜像仓库流量**：ECR 跨 AZ 拉镜像，一个月 image pull 流量费 $3,200
- **NAT Gateway 费用暗坑**：忘了配 VPC Endpoint，所有 S3/ECR 流量都走 NAT

这是一个典型的"云原生陷阱"——容器化之后资源调度变灵活了，但成本可见性反而变差了。FinOps 要解决的正是这个问题。

---

## FinOps 框架：三阶段不能跳级

FinOps Foundation 定义了三个成熟度阶段，实践中最常见的错误是跳过 Inform 直接做 Optimize，结果优化了一堆但不知道效果。

### Inform 阶段：先看清楚花在哪

没有可观测性就没有治理。这一阶段的目标是让每笔云支出都能对应到业务团队、服务、甚至功能。

**必须建立的标签体系（Label Schema）：**

```yaml
# 强制标签，所有 Deployment/StatefulSet 必须携带
required_labels:
  - app.kubernetes.io/name        # 服务名
  - app.kubernetes.io/component   # 组件类型: api/worker/scheduler
  - team                          # 负责团队
  - env                           # 环境: prod/staging/qa
  - cost-center                   # 成本中心编号
```

在 OPA/Kyverno 里用 Policy 强制校验，没标签的资源拒绝部署：

```yaml
# kyverno policy: require-labels.yaml
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: require-labels
spec:
  validationFailureAction: Enforce
  rules:
    - name: check-required-labels
      match:
        any:
          - resources:
              kinds: [Deployment, StatefulSet, DaemonSet]
      validate:
        message: "必须携带 team 和 cost-center 标签"
        pattern:
          metadata:
            labels:
              team: "?*"
              cost-center: "?*"
```

### Optimize 阶段：找到可以动的钱

可见之后才能优化，要按 ROI 排序，先动影响大的。

### Operate 阶段：固化流程，防止反弹

成本治理不是一次性项目，是持续的 SOP。最终要做到：工程师提 PR 改 requests 时，能看到预测的成本影响；月初自动发报告；超预算自动告警。

---

## OpenCost 部署与 Prometheus 集成

OpenCost 是 CNCF 沙箱项目，开源免费，适合自建 K8s。Kubecost 是商业版，有更多功能但核心模型相同。

### 选型建议

| 维度 | OpenCost | Kubecost Free | Kubecost Enterprise |
|------|----------|---------------|---------------------|
| 费用 | 免费 | 免费 | $$$|
| 数据保留 | 依赖 Prometheus | 15 天 | 无限 |
| 多集群 | 需自己聚合 | 单集群 | 原生支持 |
| 成本分摊 | 基础 | 中等 | 完整 Chargeback |
| OOTB 告警 | 无 | 有 | 有 |

**中小团队（<10 个集群）用 OpenCost + 自定义 Grafana 面板完全够用。** 超过 10 个集群或者需要对业务团队出 Chargeback 报表，考虑 Kubecost Enterprise。

### OpenCost 部署

```bash
# 添加 Helm repo
helm repo add opencost https://opencost.github.io/opencost-helm-chart
helm repo update

# 安装 OpenCost，接入已有 Prometheus
helm install opencost opencost/opencost \
  --namespace opencost \
  --create-namespace \
  --set opencost.prometheus.internal.enabled=false \
  --set opencost.prometheus.external.enabled=true \
  --set opencost.prometheus.external.url=http://kube-prometheus-stack-prometheus.monitoring:9090
```

AWS 用户需要配置节点价格，OpenCost 默认会查 AWS Price API，但需要给 ServiceAccount 配 IRSA 权限：

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["pricing:GetProducts"],
    "Resource": "*"
  }]
}
```

验证数据是否正常采集：

```bash
# 查询过去 24 小时各 namespace 的成本
kubectl port-forward -n opencost svc/opencost 9003:9003 &
curl -s "http://localhost:9003/allocation?window=24h&aggregate=namespace" | jq '.data[0] | to_entries[] | {namespace: .key, cost: .value.totalCost}'
```

### Prometheus 抓取配置

OpenCost 暴露了 `/metrics` 端点，需要在 Prometheus 里配置抓取：

```yaml
# prometheus-additional-scrape.yaml
- job_name: opencost
  honor_labels: true
  scrape_interval: 1m
  metrics_path: /metrics
  static_configs:
    - targets: ['opencost.opencost:9003']
```

关键指标：

```promql
# 各 namespace 每小时成本（美元）
sum(container_cpu_allocation * on(node) group_left() node_cpu_hourly_cost) by (namespace)
+ sum(container_memory_allocation_bytes * on(node) group_left() node_ram_hourly_cost / 1024 / 1024 / 1024) by (namespace)

# 资源浪费率：请求了但没用的 CPU
1 - (
  sum(rate(container_cpu_usage_seconds_total[1h])) by (namespace)
  /
  sum(kube_pod_container_resource_requests{resource="cpu"}) by (namespace)
)
```

---

## 成本分摊模型：Chargeback vs Showback

**Showback**：给团队看他们用了多少钱，但不实际扣款。适合起步阶段，先建立成本意识。

**Chargeback**：真的从团队预算里扣。需要更精确的分摊模型，否则引发内部争议。

### 分摊模型设计

K8s 成本主要分三类：

1. **直接分配成本**：Pod 独占的资源（容易，按 label 分）
2. **共享基础设施成本**：kube-system、monitoring、ingress 等（按请求比例分摊）
3. **闲置成本**：节点买了但没用满的部分（这部分最有争议）

推荐做法：闲置成本按各团队的实际使用比例分摊，而不是按请求比例——这样能激励团队把 requests 写准确。

```bash
# OpenCost API 查询按 team label 分摊的成本
curl -s "http://localhost:9003/allocation" \
  -d "window=lastmonth" \
  -d "aggregate=label:team" \
  -d "shareIdle=true" \
  -d "shareTenancyCosts=true" | \
  jq '.data[0] | to_entries[] | {team: .key, totalCost: (.value.totalCost | floor)}'
```

---

## 资源浪费识别：VPA 推荐值分析

VPA（Vertical Pod Autoscaler）的 `Recommender` 组件会基于历史用量给出推荐的 requests/limits，即使不启用自动更新模式，单纯用推荐值做分析也非常有价值。

### 部署 VPA（仅 Recommender 模式）

```bash
git clone https://github.com/kubernetes/autoscaler
cd autoscaler/vertical-pod-autoscaler

# 只部署 recommender，不部署 updater（避免自动重启 Pod）
helm install vpa fairwinds-stable/vpa \
  --namespace vpa \
  --create-namespace \
  --set updater.enabled=false \
  --set admissionController.enabled=false \
  --set recommender.enabled=true
```

### 批量查看推荐值 vs 当前申请值的差距

```bash
#!/bin/bash
# vpa-waste-report.sh：找出 requests 虚高的 Deployment

kubectl get vpa -A -o json | jq -r '
  .items[] |
  .metadata.namespace as $ns |
  .metadata.name as $name |
  .status.recommendation.containerRecommendations[]? |
  {
    namespace: $ns,
    vpa: $name,
    container: .containerName,
    cpu_request_recommended: .lowerBound.cpu,
    cpu_request_upper: .upperBound.cpu,
    mem_recommended: .lowerBound.memory,
    mem_upper: .upperBound.memory
  }
' | jq -s 'sort_by(.namespace)'
```

实际经验：**超过 60% 的 Deployment，实际 CPU 使用量不到 requests 的 30%**。最常见的原因是工程师复制了别人的 YAML 没改 resources，或者"保险起见"申请了很多。

### 用 Goldilocks 可视化推荐

```bash
helm install goldilocks fairwinds-stable/goldilocks \
  --namespace goldilocks \
  --create-namespace

# 给要分析的 namespace 打标签
kubectl label namespace production goldilocks.fairwinds.com/enabled=true

# 访问 Dashboard
kubectl port-forward -n goldilocks svc/goldilocks-dashboard 8080:80
```

Goldilocks 会在 Web UI 里直接展示每个容器的推荐值，以及采纳推荐值能节省多少成本——这个报告直接发给研发团队，让他们自己改。

---

## Karpenter 节点策略：Spot + Consolidation

Karpenter 是目前 AWS EKS 上最好的节点自动化管理方案，核心优势是 **consolidation**（碎片整理）——能主动把负载合并到更少的节点，终止空闲节点。

### NodePool 配置：混合 Spot/On-Demand

```yaml
# nodepool-general.yaml
apiVersion: karpenter.sh/v1
kind: NodePool
metadata:
  name: general
spec:
  template:
    metadata:
      labels:
        node-type: general
    spec:
      nodeClassRef:
        group: karpenter.k8s.aws
        kind: EC2NodeClass
        name: default
      requirements:
        - key: karpenter.sh/capacity-type
          operator: In
          values: ["spot", "on-demand"]   # 优先 Spot
        - key: kubernetes.io/arch
          operator: In
          values: ["amd64"]
        - key: karpenter.k8s.aws/instance-category
          operator: In
          values: ["c", "m", "r"]
        - key: karpenter.k8s.aws/instance-generation
          operator: Gt
          values: ["3"]
      # Spot 中断时的驱逐策略
      expireAfter: 720h   # 节点最多跑 30 天，定期轮换避免长期运行问题
  disruption:
    consolidationPolicy: WhenUnderutilized  # 利用率低时主动合并
    consolidateAfter: 30s
  limits:
    cpu: "1000"
    memory: 4000Gi
```

### 关键：给无状态服务配置 PodDisruptionBudget

Consolidation 会驱逐 Pod，没有 PDB 的服务在合并时可能短暂中断：

```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: api-server-pdb
spec:
  minAvailable: 1   # 至少保留 1 个副本
  selector:
    matchLabels:
      app: api-server
```

有状态服务（数据库、消息队列）加到 Karpenter 的 `do-not-disrupt` 注解，阻止 consolidation 驱逐：

```yaml
# 给 StatefulSet Pod template 加注解
annotations:
  karpenter.sh/do-not-disrupt: "true"
```

### Spot 中断处理

安装 AWS Node Termination Handler，在 Spot 中断前 2 分钟优雅驱逐：

```bash
helm install aws-node-termination-handler \
  eks/aws-node-termination-handler \
  --namespace kube-system \
  --set enableSpotInterruptionDraining=true \
  --set enableRebalanceMonitoring=true \
  --set enableScheduledEventDraining=true
```

---

## 僵尸资源自动清理

### 清理孤儿 PVC

PVC 在 Pod 删除后仍然存在并计费，需要定期清理：

```bash
#!/bin/bash
# find-orphan-pvc.sh

echo "=== 未绑定任何 Pod 的 PVC ==="
kubectl get pvc -A -o json | jq -r '
  .items[] |
  select(.status.phase == "Bound") |
  .metadata.namespace as $ns |
  .metadata.name as $pvc |
  .spec.volumeName as $vol |
  "\($ns)/\($pvc) -> \($vol)"
' | while IFS='/' read -r ns rest; do
  pvc=$(echo "$rest" | cut -d' ' -f1)
  # 检查是否有 Pod 在使用这个 PVC
  used=$(kubectl get pods -n "$ns" -o json | jq --arg pvc "$pvc" '
    [.items[].spec.volumes[]? | select(.persistentVolumeClaim.claimName == $pvc)] | length
  ')
  if [ "$used" -eq 0 ]; then
    size=$(kubectl get pvc -n "$ns" "$pvc" -o jsonpath='{.status.capacity.storage}' 2>/dev/null)
    echo "ORPHAN: $ns/$pvc ($size)"
  fi
done
```

配合 CronJob 定期跑，输出报告后人工确认删除（别做全自动删除，PVC 删了不可恢复）。

### 清理未使用的 ConfigMap/Secret

```bash
# 找出没有被任何 Pod/Deployment 引用的 ConfigMap
kubectl get configmap -n production -o json | jq -r '.items[].metadata.name' | while read cm; do
  refs=$(kubectl get pods,deployments,statefulsets -n production -o json | \
    jq --arg cm "$cm" '[.. | objects | select(.configMap.name? == $cm or .name? == $cm)] | length')
  [ "$refs" -eq 0 ] && echo "UNUSED ConfigMap: $cm"
done
```

---

## Grafana 成本面板 + 月度超预算告警

### 核心 Grafana 面板配置

导入 OpenCost 官方 Dashboard（ID: 15714），再加几个自定义 Panel：

```json
{
  "title": "月度成本趋势 vs 预算",
  "type": "timeseries",
  "targets": [{
    "expr": "sum(increase(opencost_total_cost[1d])) * 30",
    "legendFormat": "预测月度成本"
  }, {
    "expr": "50000",
    "legendFormat": "月预算上限"
  }]
}
```

### AlertManager 告警规则

```yaml
# cost-alerts.yaml
groups:
  - name: finops
    rules:
      - alert: MonthlyCostProjectionExceeded
        expr: |
          (
            sum(increase(opencost_total_cost[24h])) * 30
          ) > 45000
        for: 1h
        labels:
          severity: warning
          team: platform
        annotations:
          summary: "月度成本预测超预算"
          description: "当前月度成本预测 {{ $value | printf \"%.0f\" }} 美元，超过预警线 $45k"

      - alert: NamespaceCostAnomaly
        expr: |
          (
            sum by (namespace) (increase(opencost_total_cost[1h]))
            /
            sum by (namespace) (increase(opencost_total_cost[1h] offset 7d))
          ) > 2
        for: 30m
        labels:
          severity: warning
        annotations:
          summary: "Namespace {{ $labels.namespace }} 成本异常翻倍"
          description: "相比上周同期，成本增加超过 100%"
```

---

## 实战案例：从 $52k 降到 $31k 的完整路径

以下是我们实际执行的操作，按 ROI 排序：

### 第一周：快速止血（节省约 $8k/月）

**1. 清理孤儿 PVC（$1,200/月）**
```bash
# 跑脚本发现 68 个孤儿 PVC，合计 3.8TB EBS gp3
# 逐一确认后删除，立即生效
kubectl delete pvc -n production $(kubectl get pvc -n production | grep -v Bound | awk '{print $1}')
```

**2. 配置 VPC Endpoint（$3,800/月）**
```bash
# 创建 S3 和 ECR 的 VPC Gateway/Interface Endpoint
# 消除跨 NAT Gateway 的 S3/ECR 流量费
aws ec2 create-vpc-endpoint \
  --vpc-id vpc-xxxxx \
  --service-name com.amazonaws.us-west-2.s3 \
  --route-table-ids rtb-xxxxx
```

**3. 关停开发环境夜间/周末节点（$3,000/月）**
```bash
# 使用 Karpenter NodePool 配置时间窗口，或者简单粗暴用 CronJob scale deployment to 0
kubectl patch deployment -n dev --all -p '{"spec":{"replicas":0}}'
```

### 第二周：节点优化（节省约 $7k/月）

**4. 启用 Karpenter Consolidation**

把原有的 Managed Node Group 迁移到 Karpenter 管理，开启 `WhenUnderutilized` 策略。第一个 72 小时内，节点数从 47 降到 29。

**5. 开启 Spot 节点（无状态服务全量迁移）**

修改各团队 Deployment 的 `nodeSelector`，统一切到带 Spot 支持的 NodePool。实际 Spot 中断率不到 2%，配合 PDB 几乎无感知。

### 第三周：精细化 Requests 调优（节省约 $6k/月）

**6. 批量按 VPA 推荐值降低 CPU Requests**

```bash
# 生成变更清单
kubectl get vpa -A -o json | jq -r '
  .items[] | 
  select(.status.recommendation != null) |
  [.metadata.namespace, .metadata.name, 
   (.status.recommendation.containerRecommendations[0].target.cpu // "N/A"),
   (.status.recommendation.containerRecommendations[0].target.memory // "N/A")] |
  @tsv
' | column -t > vpa-recommendations.txt

# 按 namespace 分发给各团队，让他们自己改 PR
```

集中 Sprint 完成后，集群整体 CPU 请求量从 3200 cores 降到 1800 cores，节点数进一步减少。

### 结果

| 成本项 | 优化前 | 优化后 | 节省 |
|--------|--------|--------|------|
| EC2 节点 | $32k | $19k | $13k |
| EBS 存储 | $6k | $2.8k | $3.2k |
| 数据传输 | $5k | $1.2k | $3.8k |
| 其他 | $9k | $8k | $1k |
| **合计** | **$52k** | **$31k** | **$21k** |

---

## 持续运营：防止反弹的机制

成本治理最大的敌人是"优化之后慢慢又涨回去"。防止反弹需要把约束内置到流程里：

1. **CI/CD 集成成本检查**：PR 里有 resource requests 变更时，自动跑 Infracost 估算月度影响
2. **季度 FinOps Review**：每季度各团队 Owner 对自己 namespace 的成本趋势负责
3. **Namespace 预算 Quota**：用 ResourceQuota 设置 CPU/Memory 上限，超过上限的 Pod 调度失败，倒逼团队做精细化管理
4. **自动报告**：每周一自动跑脚本，把各 namespace 成本发到对应团队的 Slack 频道

```bash
# weekly-cost-report.sh（放在 CronJob 里，每周一 9:00 跑）
#!/bin/bash
REPORT=$(curl -s "http://opencost.opencost:9003/allocation?window=lastweek&aggregate=label:team&shareIdle=true" | \
  jq -r '.data[0] | to_entries[] | "\(.key): $\(.value.totalCost | floor)"' | sort -t'$' -k2 -rn | head -10)

curl -X POST "$SLACK_WEBHOOK" \
  -H 'Content-type: application/json' \
  -d "{\"text\": \"*上周各团队 K8s 成本 Top 10*\n\`\`\`${REPORT}\`\`\`\"}"
```

---

FinOps 不是一个工具，是一种组织习惯。工具只是让浪费可见，真正的优化靠的是工程团队愿意为资源使用负责。建立这套体系最难的不是技术，是让研发团队相信"改小 requests 不会让服务挂掉"。
