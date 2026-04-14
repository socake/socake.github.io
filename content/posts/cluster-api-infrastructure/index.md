---
title: "Cluster API 实战：用声明式的方式管理 Kubernetes 集群的生命周期"
date: 2025-04-05T14:15:00+08:00
draft: false
tags: ["Cluster API", "Kubernetes", "IaC", "集群管理"]
categories: ["基础设施"]
description: "Cluster API v1.12 在生产中的完整打法：Management Cluster / Workload Cluster 的分工、CAPA / CAPZ / CAPG 等 provider、ClusterClass 的复用、MachineDeployment 生命周期、KubeadmControlPlane 的滚动升级、和 ArgoCD / Terraform 的协作以及常见踩坑。"
summary: "用 Terraform 建集群是起手式，但集群一旦多起来 Terraform 的代码量和状态管理开始爆炸。Cluster API 把'集群'本身做成了 Kubernetes CRD——你在 Management Cluster 里 kubectl apply 一个 Cluster 对象，就能得到一个新集群。这是 Kubernetes 治理 Kubernetes 的一种优雅解法。"
toc: true
math: false
diagram: false
keywords: ["Cluster API", "CAPI", "CAPA", "ClusterClass", "KubeadmControlPlane", "Kubernetes lifecycle"]
params:
  reading_time: true
---

## 为什么 Terraform 不够

用 Terraform 建 Kubernetes 集群是绝大多数团队的起点。一开始它非常好用，几百行代码起一个 EKS，参数化 variables.tf，模块化 module，CI/CD 一跑就有。

但是当你的集群数量从 1 个涨到 10 个、20 个，问题就开始出现：

1. **drift 管理**：有人手动改了 ASG 配置、有人在控制台加了 SG rule，Terraform state 和实际不一致。你要么 `terraform apply` 把它改回来，要么手动 import 进 state。人多了每次都撞。
2. **版本升级**：要升级 Kubernetes 从 1.28 到 1.29，你在 Terraform 里改一个版本号，apply 可能直接给你 recreate 整个集群（取决于 provider 是否支持 in-place 升级）。
3. **批量操作**：一次升级 10 个集群，你要跑 10 次 Terraform、维护 10 个 state。
4. **多云**：AWS Terraform、GCP Terraform、Azure Terraform 的 module 结构都不一样，团队要学三套。
5. **"集群" 这个概念在 Terraform 里没有统一抽象**：对 EKS 是 `aws_eks_cluster`，对 GKE 是 `google_container_cluster`，对自建的 kubeadm 集群是一堆 ec2 + user-data。

Cluster API 给出的答案：**把集群本身做成 Kubernetes 的 CRD**。一个 `Cluster` 对象就是一个 Kubernetes 集群，你在一个"管理集群" (Management Cluster) 上 kubectl apply，Management Cluster 里的 controller 负责把这个 Cluster 变成真实的基础设施和 Kubernetes。

## CAPI 的基本术语

读文档之前先搞懂这几个词：

- **Management Cluster**：跑 Cluster API controller 的集群。它自己不跑业务 workload，只管理其他 workload cluster 的生命周期。
- **Workload Cluster** (有时叫 Target Cluster)：由 Management Cluster 创建和管理的集群，跑业务 workload。
- **Provider**：把 CAPI 的抽象翻译成具体云 / 基础设施的组件。几种：
  - **Infrastructure Provider**：CAPA (AWS)、CAPG (GCP)、CAPZ (Azure)、CAPV (vSphere)、CAPO (OpenStack)、CAPM (Metal3 / 裸金属) 等等；
  - **Bootstrap Provider**：决定新节点怎么加入集群。最常用的是 `kubeadm` (CABPK)，也有 Talos。
  - **Control Plane Provider**：怎么跑控制平面。`KubeadmControlPlane` (KCP) 最常见。
- **Cluster**：CAPI 的核心 CRD，代表一个 workload cluster。
- **Machine**：代表一个 node（虚拟机或物理机）。
- **MachineDeployment / MachineSet / MachinePool**：类似 Deployment / ReplicaSet / Pod，管理一组 Machine 的副本。
- **KubeadmControlPlane**：控制平面的 Machine 组。

## 一个 CAPA（AWS）的完整示例

假设你要用 CAPI 在 AWS 上建一个自管理的 Kubernetes 集群（不是 EKS）。完整链路：

### 步骤 1：准备 Management Cluster

Management Cluster 自己可以是任何 Kubernetes：本地 kind、EKS、甚至一个小的 k3s。生产建议：**独立的小 EKS 集群**，不要和业务集群混。

```bash
# 用 clusterctl 初始化
clusterctl init --infrastructure aws
```

这条命令会装：

- `capi-system`：CAPI 核心 controller；
- `capi-kubeadm-bootstrap-system`：CABPK；
- `capi-kubeadm-control-plane-system`：KCP；
- `capa-system`：CAPA controller。

### 步骤 2：IAM 准备

CAPA 需要 AWS 权限。推荐用 `clusterawsadm`：

```bash
export AWS_REGION=us-west-2
clusterawsadm bootstrap iam create-cloudformation-stack
```

这会在 AWS 里建 IAM role `controllers.cluster-api-provider-aws.sigs.k8s.io` 等等。CAPA controller 通过 IRSA 假借这些 role 操作 AWS。

### 步骤 3：声明一个集群

```yaml
apiVersion: cluster.x-k8s.io/v1beta1
kind: Cluster
metadata:
  name: app-prod
  namespace: clusters
spec:
  clusterNetwork:
    pods:
      cidrBlocks: ["192.168.0.0/16"]
    services:
      cidrBlocks: ["10.128.0.0/12"]
  controlPlaneRef:
    apiVersion: controlplane.cluster.x-k8s.io/v1beta1
    kind: KubeadmControlPlane
    name: app-prod-cp
  infrastructureRef:
    apiVersion: infrastructure.cluster.x-k8s.io/v1beta2
    kind: AWSCluster
    name: app-prod
---
apiVersion: infrastructure.cluster.x-k8s.io/v1beta2
kind: AWSCluster
metadata:
  name: app-prod
  namespace: clusters
spec:
  region: us-west-2
  sshKeyName: default
  network:
    vpc:
      cidrBlock: "10.0.0.0/16"
    subnets:
      - cidrBlock: "10.0.0.0/24"
        availabilityZone: us-west-2a
      - cidrBlock: "10.0.1.0/24"
        availabilityZone: us-west-2b
      - cidrBlock: "10.0.2.0/24"
        availabilityZone: us-west-2c
---
apiVersion: controlplane.cluster.x-k8s.io/v1beta1
kind: KubeadmControlPlane
metadata:
  name: app-prod-cp
  namespace: clusters
spec:
  replicas: 3
  version: v1.30.5
  machineTemplate:
    infrastructureRef:
      apiVersion: infrastructure.cluster.x-k8s.io/v1beta2
      kind: AWSMachineTemplate
      name: app-prod-cp
  kubeadmConfigSpec:
    initConfiguration:
      nodeRegistration:
        kubeletExtraArgs:
          cloud-provider: external
    clusterConfiguration:
      apiServer:
        extraArgs:
          cloud-provider: external
    joinConfiguration:
      nodeRegistration:
        kubeletExtraArgs:
          cloud-provider: external
---
apiVersion: infrastructure.cluster.x-k8s.io/v1beta2
kind: AWSMachineTemplate
metadata:
  name: app-prod-cp
  namespace: clusters
spec:
  template:
    spec:
      instanceType: m5.large
      ami:
        id: ami-0123456789abcdef0
      iamInstanceProfile: "control-plane.cluster-api-provider-aws.sigs.k8s.io"
---
apiVersion: cluster.x-k8s.io/v1beta1
kind: MachineDeployment
metadata:
  name: app-prod-md-0
  namespace: clusters
spec:
  clusterName: app-prod
  replicas: 3
  selector:
    matchLabels: {}
  template:
    spec:
      bootstrap:
        configRef:
          apiVersion: bootstrap.cluster.x-k8s.io/v1beta1
          kind: KubeadmConfigTemplate
          name: app-prod-md-0
      clusterName: app-prod
      infrastructureRef:
        apiVersion: infrastructure.cluster.x-k8s.io/v1beta2
        kind: AWSMachineTemplate
        name: app-prod-md-0
      version: v1.30.5
---
apiVersion: infrastructure.cluster.x-k8s.io/v1beta2
kind: AWSMachineTemplate
metadata:
  name: app-prod-md-0
  namespace: clusters
spec:
  template:
    spec:
      instanceType: m5.2xlarge
      ami:
        id: ami-0123456789abcdef0
      iamInstanceProfile: "nodes.cluster-api-provider-aws.sigs.k8s.io"
---
apiVersion: bootstrap.cluster.x-k8s.io/v1beta1
kind: KubeadmConfigTemplate
metadata:
  name: app-prod-md-0
  namespace: clusters
spec:
  template:
    spec:
      joinConfiguration:
        nodeRegistration:
          kubeletExtraArgs:
            cloud-provider: external
```

`kubectl apply` 这个 yaml 到 management cluster 里，等几分钟，一个新的 Kubernetes 集群就出来了。

### 步骤 4：拿到 kubeconfig

```bash
clusterctl get kubeconfig app-prod -n clusters > app-prod.kubeconfig
export KUBECONFIG=app-prod.kubeconfig
kubectl get nodes
```

看到 3 个 master + 3 个 worker，一个 CAPI workload cluster 就起好了。

这时候的集群还不能跑 workload，因为 CNI 还没装。下一步：

```bash
kubectl apply -f https://raw.githubusercontent.com/projectcalico/calico/v3.28.0/manifests/calico.yaml
```

或者用你选的 CNI。

## ClusterClass：集群模板化

写一个完整 Cluster + KubeadmControlPlane + MachineDeployment + AWSMachineTemplate 的 yaml 大概要 200-300 行。集群一多，复制粘贴成灾。

ClusterClass 就是解决这个的。它把"集群的模板"抽象出来：

```yaml
apiVersion: cluster.x-k8s.io/v1beta1
kind: ClusterClass
metadata:
  name: standard-aws
  namespace: clusters
spec:
  controlPlane:
    ref:
      apiVersion: controlplane.cluster.x-k8s.io/v1beta1
      kind: KubeadmControlPlaneTemplate
      name: standard-aws-cp
    machineInfrastructure:
      ref:
        apiVersion: infrastructure.cluster.x-k8s.io/v1beta2
        kind: AWSMachineTemplate
        name: standard-aws-cp
  infrastructure:
    ref:
      apiVersion: infrastructure.cluster.x-k8s.io/v1beta2
      kind: AWSClusterTemplate
      name: standard-aws
  workers:
    machineDeployments:
      - class: default-worker
        template:
          bootstrap:
            ref:
              apiVersion: bootstrap.cluster.x-k8s.io/v1beta1
              kind: KubeadmConfigTemplate
              name: standard-aws-md-0
          infrastructure:
            ref:
              apiVersion: infrastructure.cluster.x-k8s.io/v1beta2
              kind: AWSMachineTemplate
              name: standard-aws-md-0
  variables:
    - name: region
      required: true
      schema:
        openAPIV3Schema:
          type: string
    - name: controlPlaneReplicas
      required: false
      schema:
        openAPIV3Schema:
          type: integer
          default: 3
    - name: workerReplicas
      required: false
      schema:
        openAPIV3Schema:
          type: integer
          default: 3
    - name: instanceType
      required: false
      schema:
        openAPIV3Schema:
          type: string
          default: "m5.2xlarge"
  patches:
    - name: region
      definitions:
        - selector:
            apiVersion: infrastructure.cluster.x-k8s.io/v1beta2
            kind: AWSClusterTemplate
            matchResources:
              infrastructureCluster: true
          jsonPatches:
            - op: replace
              path: "/spec/template/spec/region"
              valueFrom:
                variable: region
    - name: instanceType
      definitions:
        - selector:
            apiVersion: infrastructure.cluster.x-k8s.io/v1beta2
            kind: AWSMachineTemplate
            matchResources:
              machineDeploymentClass:
                names: [default-worker]
          jsonPatches:
            - op: replace
              path: "/spec/template/spec/instanceType"
              valueFrom:
                variable: instanceType
```

之后你只要写一个短的 Cluster：

```yaml
apiVersion: cluster.x-k8s.io/v1beta1
kind: Cluster
metadata:
  name: team-b-prod
  namespace: clusters
spec:
  clusterNetwork:
    pods: {cidrBlocks: ["192.168.0.0/16"]}
    services: {cidrBlocks: ["10.128.0.0/12"]}
  topology:
    class: standard-aws
    version: v1.30.5
    controlPlane:
      replicas: 3
    workers:
      machineDeployments:
        - class: default-worker
          name: md-0
          replicas: 5
    variables:
      - name: region
        value: us-east-1
      - name: instanceType
        value: m5.4xlarge
```

30 行 yaml 出一个集群。CAPI 会根据 ClusterClass 渲染出所有需要的对象。**这是 CAPI 相对 Terraform 最明显的优势**：模板复用的成本低、变更追踪清晰、所有集群状态都在 Management Cluster 的 etcd 里。

## 升级：CAPI 最强的卖点

从 v1.30 升到 v1.31 只要改一个字段：

```yaml
spec:
  topology:
    version: v1.31.5
```

apply 之后发生的事：

1. KCP 触发滚动升级，一台一台新 master 上线、老 master 下线；
2. 等控制平面升级完，MachineDeployment 的 template 被更新；
3. Worker 按 MachineDeployment 的 rollingUpdate 策略一台一台替换；
4. 整个过程对 workload 而言是滚动 drain + reschedule。

**这一套和 Deployment 滚动升级非常像**，如果你用过 Deployment 升级，CAPI 升级的心智模型是完全一致的。

关键配置：

```yaml
spec:
  template:
    spec:
      rolloutStrategy:
        type: RollingUpdate
        rollingUpdate:
          maxSurge: 1        # 一次多起几台做替换
          maxUnavailable: 0  # 升级期间允许的不可用 node 数
```

生产建议：

- Control Plane 升级：`maxSurge=1`, `maxUnavailable=0`，**永远保持 3 个 master 健康**；
- Worker 升级：`maxSurge=25%`, `maxUnavailable=25%`；
- 跨大版本升级不要一次跳多版本，遵循 Kubernetes 的 skew policy。

## 和 GitOps 的协作

CAPI 最香的地方是：**集群定义本身就是 Kubernetes 对象，GitOps 天然可用**。

典型模式：

1. Git 仓库存所有 Cluster / ClusterClass yaml；
2. Management Cluster 上跑 ArgoCD / Flux；
3. ArgoCD 把 Cluster 对象同步到 Management Cluster；
4. CAPI controller 创建实际集群；
5. 集群创建好后，又可以用 ArgoCD 管 workload cluster 的 workload（Cluster API Provider 还有一个 "cluster autodiscovery" 特性，ArgoCD 可以直接接管新集群）。

一个典型的 ArgoCD ApplicationSet + Cluster API 组合：

```yaml
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: workload-bootstrap
  namespace: argocd
spec:
  generators:
    - clusters:
        selector:
          matchLabels:
            argocd.argoproj.io/secret-type: cluster
  template:
    metadata:
      name: '{{name}}-bootstrap'
    spec:
      project: default
      source:
        repoURL: https://github.com/example/cluster-bootstrap
        targetRevision: main
        path: 'bootstrap'
      destination:
        server: '{{server}}'
        namespace: bootstrap
      syncPolicy:
        automated:
          prune: true
          selfHeal: true
```

有新 workload cluster 起来，ArgoCD 自动给它装 bootstrap 组件（CNI、metrics-server、cert-manager 等）。

这种"一条链到底"的体验是 Terraform 做不到的。Terraform 里你要先 apply 建集群，再在别的流水线里 apply workload。CAPI 把两步合一。

## MachinePool：给 ASG 的抽象

MachineDeployment 对标 K8s Deployment，在 AWS 场景下它实际是每个 Machine 一个 EC2 实例，CAPI 自己管实例生命周期。

但是 AWS 自己的 ASG 也有自己的管理能力：spot 替换、健康检查、cooldown。CAPI 提供了 MachinePool 作为"让 AWS ASG 自己管"的抽象：

```yaml
apiVersion: cluster.x-k8s.io/v1beta1
kind: MachinePool
metadata:
  name: app-prod-mp-0
  namespace: clusters
spec:
  clusterName: app-prod
  replicas: 5
  template:
    spec:
      clusterName: app-prod
      bootstrap:
        configRef:
          apiVersion: bootstrap.cluster.x-k8s.io/v1beta1
          kind: KubeadmConfigTemplate
          name: app-prod-mp-0
      infrastructureRef:
        apiVersion: infrastructure.cluster.x-k8s.io/v1beta2
        kind: AWSMachinePool
        name: app-prod-mp-0
      version: v1.30.5
---
apiVersion: infrastructure.cluster.x-k8s.io/v1beta2
kind: AWSMachinePool
metadata:
  name: app-prod-mp-0
spec:
  minSize: 3
  maxSize: 10
  mixedInstancesPolicy:
    instancesDistribution:
      onDemandBaseCapacity: 2
      onDemandPercentageAboveBaseCapacity: 25
      spotAllocationStrategy: capacity-optimized
    overrides:
      - instanceType: m5.2xlarge
      - instanceType: m5a.2xlarge
      - instanceType: m4.2xlarge
```

MachinePool 背后其实是一个 ASG，minSize / maxSize 是 ASG 的范围，mixed instance policy 让你用 spot + on-demand 混跑。

选择：

- **MachineDeployment**：CAPI 完全管生命周期。适合"我想在 CAPI 层看到每一个 Machine" 的场景；
- **MachinePool**：AWS 层的 ASG。适合 spot 混跑、快速扩缩的场景。

生产我的选择：worker 用 MachinePool，control plane 用 KubeadmControlPlane（它是特殊的 Machine 组）。

## 和 Karpenter 的关系

Karpenter 和 CAPI 是两个不同层次的东西，但经常被拿来比。

- **CAPI**：管 集群 本身的生命周期——建集群、升级集群、加 node pool、删集群。
- **Karpenter**：管 集群内 的 node 动态扩缩——pod pending 时拉 node、利用率低时缩 node。

它们是互补的。理想架构：

1. 用 CAPI 建集群，指定最小的一批 "系统 node"（比如 2-3 个 m5.large 作为 system workload 的承载）；
2. Karpenter 装在集群里，负责业务 workload 的动态 node；
3. CAPI 不管 Karpenter 创建的 node——Karpenter 的 NodeClaim 是另一个体系。

一个常见的错误是想让 CAPI MachineDeployment 自己做动态扩缩。CAPI 的 MachineDeployment 有一个 "replicas" 字段，但没有真正的 HPA 对应物（虽然有 experimental 的 MachineHealthCheck 和 cluster-autoscaler 集成）。生产上把动态扩缩交给 Karpenter 或 cluster-autoscaler 更靠谱。

## MachineHealthCheck：坏 node 自愈

MachineHealthCheck 是 CAPI 的"节点自愈"机制：

```yaml
apiVersion: cluster.x-k8s.io/v1beta1
kind: MachineHealthCheck
metadata:
  name: app-prod-mhc
  namespace: clusters
spec:
  clusterName: app-prod
  maxUnhealthy: 40%
  nodeStartupTimeout: 10m
  selector:
    matchLabels:
      cluster.x-k8s.io/deployment-name: app-prod-md-0
  unhealthyConditions:
    - type: Ready
      status: Unknown
      timeout: 300s
    - type: Ready
      status: "False"
      timeout: 300s
```

意思是：如果某个 Machine 对应的 Node 持续 5 分钟 Ready=False 或 Unknown，就认为它坏了，CAPI 删掉这个 Machine，MachineDeployment 会自动新开一个替补。

`maxUnhealthy`：防止集群整体故障时 CAPI 把所有 Machine 都干掉（出现网络分区时保护）。生产建议 40-50%。

这个特性非常实用，相当于给集群配了一个自愈 controller。node 挂了不用人管。

## Workload Cluster 的 addon 怎么装

CAPI 本身只管集群基础设施和 kubelet，**不管 CNI / CSI / 其他 addon**。这些得你自己装。几种模式：

### 模式 1：手动 apply

最简单，`kubectl --kubeconfig=new-cluster.kubeconfig apply -f calico.yaml`。适合 lab，不适合生产。

### 模式 2：Cluster Resource Set (CRS)

CAPI 自己的 addon 机制：

```yaml
apiVersion: addons.cluster.x-k8s.io/v1beta1
kind: ClusterResourceSet
metadata:
  name: calico
  namespace: clusters
spec:
  clusterSelector:
    matchLabels:
      cni: calico
  resources:
    - kind: ConfigMap
      name: calico-manifests
  strategy: ApplyOnce
```

然后把 Calico manifests 封装在 ConfigMap 里。给要装 Calico 的 Cluster 打 label `cni=calico`，CRS 自动在创建时 apply。

### 模式 3：ArgoCD ApplicationSet

最灵活，生产推荐。前面 GitOps 那节讲过。

**原则**：别用 CRS 做复杂 addon 管理。CRS 只适合装一次就不动的基础组件（CNI、cloud-provider）。需要持续 reconcile 的用 ArgoCD。

## 多 provider

CAPI 的一大卖点是一套 API 管多云。CAPA / CAPZ / CAPG 的 Cluster 对象都是一样的 `cluster.x-k8s.io/v1beta1 Cluster`，只是 `infrastructureRef` 指向不同 provider 的 CRD。

真正的多云用户可以在同一个 Management Cluster 上管 AWS + GCP + Azure 集群，用统一的 CRD 语义。这是 Terraform 做不到的——Terraform 的 AWS module 和 GCP module 代码是完全不同的。

但注意：**一个 Management Cluster 最好只用一个 infrastructure provider**。多 provider 装在一起虽然技术上可行，但 CRD 和权限会非常乱。生产建议：

- 每个云一个 Management Cluster（比如 us-aws-mgmt、cn-aliyun-mgmt）；
- 或者所有云用一个大 Management Cluster，但用 namespace 隔离。

我们线上是前者，每个云一个小的管理集群。

## 踩过的坑

### 坑 1：IAM 权限不够

CAPA 的 IAM 权限范围很大（要能建 VPC / EC2 / LoadBalancer / SG...）。第一次上生产时我以为用 `clusterawsadm bootstrap iam` 生成的就够，结果少了 `iam:CreateOpenIDConnectProvider`、`route53:*` 等。解决：看 `clusterawsadm` 最新版本的 permission，每次升级要对齐。

### 坑 2：CAPI 升级顺序

升级顺序必须：

1. 先升 Management Cluster 的 Kubernetes 版本（如果要升）；
2. 再升 CAPI core controllers (`capi-system`)；
3. 然后升 infrastructure provider (`capa-system`)；
4. 最后升 workload cluster 的 Kubernetes 版本。

跳步很容易出不兼容。用 `clusterctl upgrade plan` 会告诉你推荐顺序。

### 坑 3：etcd 的性能

每个 Cluster 对象在 management cluster 的 etcd 里占用不少空间。100 个 workload cluster 时 management cluster 的 etcd 压力明显上升。给 management cluster 配独立的、大点的 etcd 节点。

### 坑 4：kubeconfig 失效

CAPI 给每个 workload cluster 自动生成一个 kubeconfig secret。这个 kubeconfig 里的 client certificate 有一年有效期（默认），过期后你突然就连不上集群了。

解决：
- 用 CAPI 的 `Cluster API Runtime SDK` 定期轮转；
- 或者不依赖这个 admin kubeconfig，而是在 workload cluster 里创建长期 ServiceAccount token 给 GitOps / 监控使用。

### 坑 5：Cluster 删除不干净

`kubectl delete cluster foo` 之后 cluster 对象的 finalizer 阻止它立刻删，CAPI 会先删 Machine 再删基础设施再删 Cluster。过程中任何一步卡住，集群就在 "Deleting" 状态挂着。

遇到这种情况的排查：
1. 看 Cluster 的 events；
2. 看每个 Machine 的 events；
3. 最后手段：remove finalizer 手动清理。但要先确认 AWS 资源都清了，不然会留孤儿。

### 坑 6：CAPI v1alpha/v1beta API 变更

CAPI 虽然是 v1beta1，但子 provider 的 API 版本可能是 v1alpha/v1beta2 混在一起。升级时可能有 breaking change。**读 release notes 是必须的**，不是可选的。每次升级前通读一遍上 1-2 个版本的 changelog。

## 什么场景不用 CAPI

- 集群数量 ≤ 3，Terraform 够了；
- 团队完全不懂 Kubernetes CRD，硬上 CAPI 心智负担大；
- 只用 EKS / GKE 这种 managed 集群，且从不自管 control plane——你可以用 EKS + Terraform + Karpenter，更简单；
- 多云的复杂度不在你的业务范围内。

CAPI 真正适合的是"多集群 + 要求声明式生命周期管理 + 有内部团队维护"的场景。如果你只有几个集群、或者已经在 Terraform 上走得很稳，强行切 CAPI 收益不大。

## 一些推荐的最佳实践

- Management Cluster 独立、小、稳定；
- 用 ClusterClass 做模板，所有业务集群引用模板；
- 所有 CAPI 对象放 Git，GitOps 管理；
- 每个 workload cluster 有明确的 "class" 和 "environment" label，配合 ArgoCD ApplicationSet；
- MachineHealthCheck 必开；
- 升级先在 dev cluster 试；
- 监控 Management Cluster 的 etcd 容量；
- 定期轮转 workload cluster 的 kubeconfig / cert；
- 和 Karpenter 分工：CAPI 管 "基础 node"，Karpenter 管 "弹性 node"；
- 不要把 addon 塞到 CRS 里，用 ArgoCD。

## 对比 Terraform 的一句话结论

**Terraform 是一个通用的 "基础设施代码"，CAPI 是 "用 Kubernetes 管理 Kubernetes"**。前者更通用、生态更大；后者在"大量 K8s 集群生命周期管理"这个细分场景里更强。

两者不是互斥的。很多团队的做法是：

- Terraform 管 Management Cluster 自己（含 VPC、IAM、RDS 等外围）；
- CAPI 管所有 workload cluster；
- ArgoCD 管 workload cluster 里的应用。

三段分工非常干净。我们过去一年就是这么做的，和之前全 Terraform 时相比运维体感明显好很多。CAPI 的学习成本前期不低，但过了那个坎之后，管 20 个集群和管 2 个集群的心智负担接近。

这是最让我欣赏的一点。
