---
title: "Crossplane：用 GitOps 方式管理云资源（AWS/阿里云）"
date: 2026-04-11T15:00:00+08:00
draft: false
tags: ["Crossplane", "GitOps", "AWS", "云原生", "基础设施即代码", "2026"]
categories: ["云原生"]
description: "Crossplane 把 AWS RDS、S3、EKS 变成 K8s CRD，用 GitOps 方式持续协调云资源状态。记录从概念到落地的实践过程和踩坑经验。"
summary: "Crossplane 把 AWS RDS、S3、EKS 变成 K8s CRD，用 GitOps 方式持续协调云资源状态。记录从概念到落地的实践过程和踩坑经验。"
toc: true
math: false
diagram: false
keywords: ["Crossplane", "GitOps", "AWS", "基础设施即代码", "ArgoCD", "Kubernetes"]
params:
  reading_time: true
---

## 为什么不用 Terraform 就好了

这个问题我被问过好几次，值得认真回答。

Terraform 很好用，我们团队用了三年，积累了大量 module。但随着云资源规模增长，Terraform 的工作流开始暴露问题：

**状态文件是单点瓶颈。** `terraform.tfstate` 要存在 S3，多人操作时要加锁。跑 plan 需要先 pull state，大型项目的 state 文件能有几 MB，每次操作都要全量 refresh，慢。

**apply 是一次性操作，不是持续协调。** Terraform apply 之后，如果有人在控制台手动改了云资源（改了安全组、调了配置），Terraform 不知道。除非你定期跑 `terraform plan` 去 detect drift，而且 detect 到了还得人工决定要不要 apply。

**与 K8s GitOps 流水线割裂。** 应用部署走 ArgoCD，云资源变更走 Terraform + 手动 CI，两套体系，审计和回滚逻辑不统一。

Crossplane 的设计思路不同：它是一个 K8s operator，把云资源建模成 K8s CRD，然后用 reconcile loop 持续协调——就像 K8s 确保 Pod 运行一样，Crossplane 确保云资源的实际状态和期望状态一致。

有人手动在控制台改了 RDS 配置？下一个 reconcile 周期（默认 10 分钟），Crossplane 会把它改回来。这才是真正意义上的 IaC。

## 核心概念

**Provider**：对接特定云厂商的插件。官方维护 AWS、GCP、Azure Provider，社区有阿里云 Provider（`provider-alibaba`）。每个 Provider 会在集群里注册一批 CRD，对应云厂商的各种资源类型。

**Managed Resource (MR)**：Provider 注册的具体资源 CRD，比如 `RDSInstance`、`S3Bucket`、`VPC`。每个 MR 实例对应云上一个真实资源，1:1 映射。

**Composite Resource (XR) + Composition**：这是 Crossplane 的高阶功能。你可以定义自己的抽象资源类型（比如 `AppDatabase`），背后 Composition 定义它怎么组合成多个 MR（比如 RDS 实例 + 参数组 + 子网组）。业务团队操作 `AppDatabase`，不用关心底层 AWS 资源细节。

## 安装 Crossplane

```bash
helm repo add crossplane-stable https://charts.crossplane.io/stable
helm repo update

helm install crossplane \
  crossplane-stable/crossplane \
  --namespace crossplane-system \
  --create-namespace \
  --version 1.17.0
```

安装 AWS Provider：

```bash
cat <<EOF | kubectl apply -f -
apiVersion: pkg.crossplane.io/v1
kind: Provider
metadata:
  name: provider-aws-rds
spec:
  package: xpkg.upbound.io/upbound/provider-aws-rds:v1.14.0
EOF
```

Crossplane 的 Provider 是分包的，按需安装（`provider-aws-rds`、`provider-aws-s3`、`provider-aws-ec2` 等），避免把整个 AWS Provider 装进来引入几千个 CRD。

配置 Provider 凭据：

```bash
# 创建 AWS 凭据 Secret
kubectl create secret generic aws-creds \
  -n crossplane-system \
  --from-literal=credentials="[default]
aws_access_key_id = AKIAIOSFODNN7EXAMPLE
aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"

# 创建 ProviderConfig
cat <<EOF | kubectl apply -f -
apiVersion: aws.upbound.io/v1beta1
kind: ProviderConfig
metadata:
  name: default
spec:
  credentials:
    source: Secret
    secretRef:
      namespace: crossplane-system
      name: aws-creds
      key: credentials
EOF
```

生产环境推荐用 IRSA（IAM Roles for Service Accounts），不要把 AK/SK 存 Secret：

```yaml
spec:
  credentials:
    source: InjectedIdentity
```

## 实战：用 YAML 创建 AWS RDS

### 直接创建 Managed Resource

```yaml
apiVersion: rds.aws.upbound.io/v1beta1
kind: Instance
metadata:
  name: prod-mysql
  annotations:
    crossplane.io/external-name: prod-mysql-01  # 云上资源名
spec:
  forProvider:
    region: us-west-2
    dbInstanceClass: db.t3.medium
    engine: mysql
    engineVersion: "8.0"
    allocatedStorage: 100
    storageType: gp3
    dbName: appdb
    username: admin
    skipFinalSnapshot: false
    finalSnapshotIdentifier: prod-mysql-final-snapshot
    multiAz: true
    backupRetentionPeriod: 7
    deletionProtection: true
    vpcSecurityGroupIdRefs:
      - name: rds-sg
    dbSubnetGroupNameRef:
      name: rds-subnet-group
    passwordSecretRef:
      name: rds-password
      namespace: crossplane-system
      key: password
  writeConnectionSecretsToRef:
    name: prod-mysql-conn
    namespace: production
```

Apply 之后，Crossplane 会调用 AWS API 创建 RDS 实例，并把连接信息写入 `production/prod-mysql-conn` Secret，应用直接从这个 Secret 读取数据库连接串。

### 用 Composition 做抽象

更推荐的做法是用 Composition 定义一个团队内部的抽象资源类型，业务团队操作这个抽象类型，不需要懂 AWS 细节。

**定义 CompositeResourceDefinition（XRD）**：

```yaml
apiVersion: apiextensions.crossplane.io/v1
kind: CompositeResourceDefinition
metadata:
  name: xappdatabases.platform.example.com
spec:
  group: platform.example.com
  names:
    kind: XAppDatabase
    plural: xappdatabases
  claimNames:
    kind: AppDatabase      # namespace-scoped 的使用方式
    plural: appdatabases
  versions:
    - name: v1alpha1
      served: true
      referenceable: true
      schema:
        openAPIV3Schema:
          type: object
          properties:
            spec:
              type: object
              properties:
                parameters:
                  type: object
                  properties:
                    size:
                      type: string
                      enum: [small, medium, large]
                      description: "数据库规格"
                    region:
                      type: string
                      default: us-west-2
                  required: [size]
```

**定义 Composition（具体实现）**：

```yaml
apiVersion: apiextensions.crossplane.io/v1
kind: Composition
metadata:
  name: appdatabases.aws.platform.example.com
  labels:
    provider: aws
spec:
  compositeTypeRef:
    apiVersion: platform.example.com/v1alpha1
    kind: XAppDatabase
  resources:
    - name: rds-instance
      base:
        apiVersion: rds.aws.upbound.io/v1beta1
        kind: Instance
        spec:
          forProvider:
            region: us-west-2
            engine: mysql
            engineVersion: "8.0"
            storageType: gp3
            multiAz: true
            backupRetentionPeriod: 7
            deletionProtection: true
      patches:
        - fromFieldPath: "spec.parameters.region"
          toFieldPath: "spec.forProvider.region"
        - fromFieldPath: "spec.parameters.size"
          toFieldPath: "spec.forProvider.dbInstanceClass"
          transforms:
            - type: map
              map:
                small: db.t3.small
                medium: db.t3.medium
                large: db.r6g.xlarge
        - fromFieldPath: "metadata.uid"
          toFieldPath: "spec.forProvider.finalSnapshotIdentifier"
          transforms:
            - type: string
              string:
                fmt: "snapshot-%s"
```

业务团队使用时只需要：

```yaml
apiVersion: platform.example.com/v1alpha1
kind: AppDatabase
metadata:
  name: order-service-db
  namespace: production
spec:
  parameters:
    size: medium
    region: us-west-2
  writeConnectionSecretsToRef:
    name: order-db-conn
```

这就是平台工程的价值：基础设施团队维护 Composition，定义规范和最佳实践；业务团队用简洁的接口自助开通资源，不用知道 AWS 的细节。

## GitOps 集成：ArgoCD 管理 Crossplane 资源

把 Crossplane 资源（XRD、Composition、MR 实例）都放进 Git repo，由 ArgoCD 同步，和应用部署走同一套 GitOps 流水线。

目录结构：

```
gitops/
├── platform/
│   ├── crossplane/
│   │   ├── compositions/
│   │   │   └── appdatabase-aws.yaml
│   │   └── xrds/
│   │       └── xappdatabase.yaml
│   └── kustomization.yaml
└── production/
    ├── databases/
    │   ├── order-db.yaml    # AppDatabase claim
    │   └── user-db.yaml
    └── kustomization.yaml
```

ArgoCD Application：

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: platform-crossplane
  namespace: argocd
spec:
  project: platform
  source:
    repoURL: https://github.com/yourorg/gitops
    targetRevision: main
    path: platform/crossplane
  destination:
    server: https://kubernetes.default.svc
    namespace: crossplane-system
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
```

这样，云资源的变更就和应用变更一样走 PR Review → merge → ArgoCD 自动同步的流程，有完整的 Git 历史，回滚就是 `git revert`。

## 踩坑

**Provider 权限配置。** Crossplane 需要的 IAM 权限是最小权限，不是 AdministratorAccess。但要整理出精确的权限列表比较费时，AWS 官方有 Provider 对应的权限文档，按文档来配，别偷懒直接给 `*`。

**Composition patch 语法。** patch 的 `fromFieldPath` / `toFieldPath` 支持点号路径，但遇到数组和嵌套对象时写法比较绕。官方文档有完整的 patch 类型列表（FromCompositeFieldPath、ToCompositeFieldPath、CombineFromComposite 等），新手最容易在这里卡住，建议多看示例。

**资源删除保护。** Crossplane 默认删除 K8s 资源会同步删除云资源（`DeletionPolicy: Delete`）。这在生产环境很危险——有人手滑 `kubectl delete` 了一个 RDS claim，数据库就没了。一定要在 RDS 这类有状态资源上加：

```yaml
spec:
  deletionPolicy: Orphan  # 只删 K8s 对象，不删云资源
```

或者在云资源层加 `deletionProtection: true`（AWS RDS 有这个选项），Crossplane 删不掉它，会 error，给你一个缓冲。

**Observe 模式导入已有资源。** 如果你有存量的 AWS 资源想用 Crossplane 管理，不需要删了重建。用 `managementPolicies: [Observe]` 先导入，Crossplane 会读取云资源的实际状态，之后再改成 `[Create, Update, Delete]` 接管控制权。

Crossplane 在 2026 年的成熟度已经比较高，生产可用。对于同时用 K8s 和多云的团队，它能把 GitOps 的一致性从应用层延伸到基础设施层，是值得投入学习的方向。
