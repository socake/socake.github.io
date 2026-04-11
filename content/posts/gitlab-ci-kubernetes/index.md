---
title: "GitLab CI/CD + Kubernetes：从代码提交到生产部署全流程"
date: 2026-04-11T08:00:00+08:00
draft: false
tags: ["GitLab", "CI/CD", "Kubernetes", "Docker", "DevOps"]
categories: ["CI/CD"]
description: "基于实际生产经验，详解 GitLab Runner 在 Kubernetes 上的部署配置、kaniko 无特权镜像构建、ECR 镜像推送认证，以及 GitOps 风格的部署流程，附完整踩坑记录。"
summary: "从 GitLab Runner 的 Kubernetes executor 配置，到 kaniko 替代 DinD 的镜像构建方案，再到通过更新 GitOps 仓库完成生产部署——记录一套在真实 AWS EKS 环境跑通的 CI/CD 全流程。"
toc: true
math: false
diagram: false
keywords: ["GitLab CI", "Kubernetes", "kaniko", "ECR", "GitOps", "DevOps"]
params:
  reading_time: true
---

在我们团队从传统 Jenkins 迁移到 GitLab CI 的过程中，最大的挑战不是写 `.gitlab-ci.yml`，而是让 Runner 在 Kubernetes 上稳定运行，同时解决镜像构建的特权问题。这篇文章把整个过程从头梳理一遍，包括那些让我们折腾了好几天的坑。

## 整体架构

代码提交触发 Pipeline 之后，流程大致如下：

```
git push → GitLab → webhook → GitLab Runner (K8s Pod)
  → test stage (单元测试)
  → build stage (kaniko 构建镜像)
  → push stage (推送 ECR)
  → deploy stage (更新 GitOps 仓库)
  → ArgoCD 监听变更 → 滚动更新到 K8s
```

核心选型原则：
- Runner 跑在 K8s 上，executor 用 kubernetes，按需创建 Job Pod
- 镜像构建用 kaniko，不需要 DinD，不需要特权容器
- 部署走 GitOps，pipeline 只更新 image tag，不直接 kubectl apply

## GitLab Runner 部署

### Helm 安装

官方 Helm Chart 是最省心的方式：

```bash
helm repo add gitlab https://charts.gitlab.io
helm repo update

helm install gitlab-runner gitlab/gitlab-runner \
  --namespace gitlab-runner \
  --create-namespace \
  -f runner-values.yaml
```

`runner-values.yaml` 的关键配置：

```yaml
gitlabUrl: https://gitlab.example.com

# Runner 注册 token，从 GitLab 项目设置里拿
runnerRegistrationToken: "your-registration-token"

rbac:
  create: true
  # Runner 需要在 cicd namespace 创建 Job Pod
  rules:
    - apiGroups: [""]
      resources: ["pods", "pods/exec", "pods/attach", "secrets", "configmaps"]
      verbs: ["get", "list", "watch", "create", "patch", "delete", "update"]
    - apiGroups: ["batch"]
      resources: ["jobs"]
      verbs: ["get", "list", "watch", "create", "patch", "delete"]

runners:
  config: |
    [[runners]]
      [runners.kubernetes]
        namespace = "cicd"
        image = "alpine:latest"
        # 关键：不开特权
        privileged = false
        # Pod 跑完自动清理
        poll_interval = 3
        poll_timeout = 180
        
        # 资源限制，防止 CI job 把节点打爆
        cpu_request = "100m"
        memory_request = "128Mi"
        cpu_limit = "2"
        memory_limit = "2Gi"
        
        # service account，用于访问 K8s API（deploy 阶段需要）
        service_account = "gitlab-runner"
        
        # 镜像拉取策略
        image_pull_secrets = ["regcred"]

# Runner Pod 自身的资源配置
resources:
  requests:
    cpu: 100m
    memory: 128Mi
  limits:
    cpu: 500m
    memory: 512Mi
```

### RBAC 权限配置

Runner 需要在 `cicd` namespace 里创建 Job Pod，同时 deploy 阶段要更新其他 namespace 的 Deployment：

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: gitlab-runner
  namespace: gitlab-runner
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: gitlab-runner
rules:
  - apiGroups: [""]
    resources: ["pods", "pods/exec", "pods/attach", "secrets", "configmaps", "namespaces"]
    verbs: ["get", "list", "watch", "create", "patch", "delete", "update"]
  - apiGroups: ["apps"]
    resources: ["deployments", "replicasets"]
    verbs: ["get", "list", "watch", "patch", "update"]
  - apiGroups: ["batch"]
    resources: ["jobs"]
    verbs: ["get", "list", "watch", "create", "patch", "delete"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: gitlab-runner
subjects:
  - kind: ServiceAccount
    name: gitlab-runner
    namespace: gitlab-runner
roleRef:
  kind: ClusterRole
  name: gitlab-runner
  apiGroup: rbac.authorization.k8s.io
```

**踩坑：** 最开始只给了 namespace 级别的 Role，deploy 阶段死活无法更新 `production` namespace 里的 Deployment，报 403。换成 ClusterRoleBinding 后解决。如果安全要求严格，可以针对每个目标 namespace 单独绑定 Role，不要图省事直接 ClusterRole。

## .gitlab-ci.yml 完整示例

下面是一个 Go 服务的完整 pipeline 配置：

```yaml
variables:
  # AWS ECR 配置
  AWS_REGION: us-west-2
  ECR_REGISTRY: 123456789.dkr.ecr.us-west-2.amazonaws.com
  IMAGE_NAME: $ECR_REGISTRY/my-service
  IMAGE_TAG: $CI_COMMIT_SHORT_SHA
  
  # GitOps 仓库
  GITOPS_REPO: gitlab.example.com/devops/k8s-manifests.git
  
  # Go 缓存
  GOPATH: $CI_PROJECT_DIR/.go
  GOCACHE: $CI_PROJECT_DIR/.go/cache

# 缓存 Go modules，加快构建速度
cache:
  key: "$CI_PROJECT_NAME-go-modules"
  paths:
    - .go/pkg/mod/
    - .go/cache/

stages:
  - test
  - build
  - push
  - deploy

# 单元测试
unit-test:
  stage: test
  image: golang:1.22-alpine
  script:
    - go test -v -race -coverprofile=coverage.out ./...
    - go tool cover -func=coverage.out | tail -1
  coverage: '/total:\s+\(statements\)\s+(\d+\.\d+)%/'
  artifacts:
    reports:
      coverage_report:
        coverage_format: cobertura
        path: coverage.xml
    expire_in: 7 days

# lint 检查
lint:
  stage: test
  image: golangci/golangci-lint:v1.57
  script:
    - golangci-lint run --timeout=5m
  allow_failure: false

# kaniko 构建镜像
build-image:
  stage: build
  # kaniko 官方镜像，无需特权
  image:
    name: gcr.io/kaniko-project/executor:v1.21.0-debug
    entrypoint: [""]
  script:
    # 配置 ECR 认证
    # 这里用的是 IRSA（IAM Roles for Service Accounts），不需要明文 AK/SK
    - mkdir -p /kaniko/.docker
    - |
      cat > /kaniko/.docker/config.json << EOF
      {
        "credHelpers": {
          "$ECR_REGISTRY": "ecr-login"
        }
      }
      EOF
    # 构建并推送，同时打两个 tag：commit sha 和 branch 名
    - /kaniko/executor
        --context $CI_PROJECT_DIR
        --dockerfile $CI_PROJECT_DIR/Dockerfile
        --destination $IMAGE_NAME:$IMAGE_TAG
        --destination $IMAGE_NAME:$CI_COMMIT_BRANCH
        --cache=true
        --cache-repo=$ECR_REGISTRY/my-service/cache
        --snapshot-mode=redo
        --use-new-run
  rules:
    - if: '$CI_COMMIT_BRANCH == "main" || $CI_COMMIT_BRANCH == "develop"'

# 更新 GitOps 仓库触发部署
deploy-staging:
  stage: deploy
  image: alpine/git:latest
  script:
    - git config --global user.email "ci@example.com"
    - git config --global user.name "GitLab CI"
    # 使用 deploy token 克隆 GitOps 仓库
    - git clone https://gitlab-ci-token:$GITOPS_DEPLOY_TOKEN@$GITOPS_REPO /tmp/gitops
    - cd /tmp/gitops
    # 更新 staging 环境的 image tag
    - sed -i "s|image: $IMAGE_NAME:.*|image: $IMAGE_NAME:$IMAGE_TAG|g" envs/staging/my-service/deployment.yaml
    - git add .
    - git commit -m "ci: update my-service to $IMAGE_TAG [skip ci]"
    - git push
  rules:
    - if: '$CI_COMMIT_BRANCH == "develop"'
  environment:
    name: staging
    url: https://staging.example.com

deploy-production:
  stage: deploy
  image: alpine/git:latest
  script:
    - git clone https://gitlab-ci-token:$GITOPS_DEPLOY_TOKEN@$GITOPS_REPO /tmp/gitops
    - cd /tmp/gitops
    - sed -i "s|image: $IMAGE_NAME:.*|image: $IMAGE_NAME:$IMAGE_TAG|g" envs/production/my-service/deployment.yaml
    - git add .
    - git commit -m "ci: update my-service to $IMAGE_TAG [skip ci]"
    - git push
  rules:
    - if: '$CI_COMMIT_BRANCH == "main"'
  # 生产环境需要手动确认
  when: manual
  environment:
    name: production
    url: https://example.com
```

## kaniko 镜像构建详解

### 为什么不用 DinD

Docker-in-Docker（DinD）需要 `privileged: true`，在多租户 K8s 集群里是安全隐患。kaniko 在用户态完成镜像构建，不需要 Docker daemon，不需要特权模式。

kaniko 的工作原理：直接解析 Dockerfile，把每一层的文件系统变更打包成 OCI 格式，最后推送到 registry。

### ECR 认证的正确姿势

**方案一：IRSA（推荐，AWS EKS 环境）**

给 Runner 的 ServiceAccount 绑定 IAM Role，Role 有 ECR 推送权限。kaniko 通过 credential helper 自动获取临时凭证：

```bash
# IAM Policy 需要包含
{
  "Effect": "Allow",
  "Action": [
    "ecr:GetAuthorizationToken",
    "ecr:BatchCheckLayerAvailability",
    "ecr:GetDownloadUrlForLayer",
    "ecr:BatchGetImage",
    "ecr:PutImage",
    "ecr:InitiateLayerUpload",
    "ecr:UploadLayerPart",
    "ecr:CompleteLayerUpload"
  ],
  "Resource": "*"
}
```

**方案二：CI/CD Variables（非 AWS 托管集群）**

在 GitLab 项目设置 → CI/CD → Variables 中添加：
- `AWS_ACCESS_KEY_ID`：masked，不保护（让所有 branch 可用）
- `AWS_SECRET_ACCESS_KEY`：masked

然后在 job 里：

```yaml
build-image:
  before_script:
    - apk add --no-cache aws-cli
    - aws ecr get-login-password --region $AWS_REGION | 
        docker login --username AWS --password-stdin $ECR_REGISTRY
```

**踩坑：** `masked` 变量在 log 里不显示，但如果变量值包含特殊字符（比如 `+` `/` `=`），AWS SDK 解析会报错。建议把 AK/SK Base64 编码后存，使用时 decode。

### kaniko cache 加速

kaniko 支持把中间层缓存推到 registry，第二次构建时直接复用：

```bash
/kaniko/executor \
  --cache=true \
  --cache-repo=$ECR_REGISTRY/my-service/cache \
  --cache-ttl=24h \
  --snapshot-mode=redo
```

`--snapshot-mode=redo` 比默认的 `full` 模式快很多，但在极少数情况下可能漏掉文件变更。如果遇到奇怪的构建问题，先换回 `full` 排查。

## 变量管理策略

### CI/CD Variables vs K8s Secrets

这两个不是替代关系，各有用途：

| 场景 | 使用方式 |
|------|---------|
| 构建阶段需要的密钥（AK/SK、registry token） | GitLab CI/CD Variables |
| 运行时应用需要的密钥（DB 密码、JWT secret） | K8s Secrets |
| 构建配置（镜像名、环境 URL）| GitLab CI/CD Variables |
| 应用配置（DB host、feature flags）| ConfigMap 或 Nacos |

GitLab Variables 的几个注意点：
- **Protected**：只在 protected branch/tag 上可用，main 和 release/* 分支才能用
- **Masked**：值不在 log 里显示，但有长度和字符限制
- **File type**：变量内容写到临时文件，适合存证书、kubeconfig 等

### 在 deploy job 里用 K8s Secret

如果 deploy 阶段需要直接 `kubectl apply` 而不是走 GitOps，可以把 kubeconfig 存为 File 类型的 Variable：

```yaml
deploy:
  script:
    # $KUBECONFIG 是 File 类型变量，GitLab 自动写到临时文件
    - kubectl --kubeconfig=$KUBECONFIG set image deployment/my-service
        my-service=$IMAGE_NAME:$IMAGE_TAG -n production
```

## pipeline 并发控制

默认情况下 GitLab 会尽量并发运行 job，但有些场景需要控制：

```yaml
# 同一个项目的 deploy 不能并发
deploy-production:
  resource_group: production
  # 同一时间只有一个 job 持有这个 resource_group
```

对于 monorepo，用 `rules: changes` 只在相关文件变更时触发：

```yaml
build-service-a:
  rules:
    - changes:
        - services/service-a/**/*
        - shared/**/*
```

## 踩坑记录

**坑1：Runner Pod 拉不到私有镜像**

症状：job 里指定的 image 一直 `ImagePullBackOff`。

原因：Runner 创建 Job Pod 时，Pod 的 `imagePullSecrets` 需要在 `runners.config` 里配置，而不是在 runner 自身的 Pod 上配置。

```toml
[runners.kubernetes]
  image_pull_secrets = ["ecr-regcred"]
```

这个 Secret 必须在 Runner 创建 Job Pod 的那个 namespace（`cicd`）里存在。

**坑2：kaniko 构建时 `/workspace` 里缺文件**

症状：`COPY` 指令报文件不存在，但本地构建没问题。

原因：`.dockerignore` 文件排除了需要的文件，或者 `--context` 指向了错误的目录。kaniko 的 context 是 `$CI_PROJECT_DIR`，确认 Dockerfile 里的路径相对于项目根目录。

**坑3：pipeline 并发导致 GitOps 仓库 push 冲突**

症状：多个 branch 同时触发 deploy，git push 报 `non-fast-forward`。

解法：用 `resource_group` 或者在脚本里加重试：

```bash
for i in $(seq 1 5); do
  git pull --rebase origin main && git push && break
  sleep $((RANDOM % 10 + 1))
done
```

**坑4：group_wait 导致 job 长时间 Pending**

症状：Job Pod 创建成功，但 runner 日志显示一直在等 executor 响应。

原因：K8s 节点资源不足，Pod 调度 Pending，runner 的 `poll_timeout`（默认 180s）超时后标记 job 失败。

解法：要么加节点，要么配置 Karpenter/Cluster Autoscaler 自动扩容，同时把 `poll_timeout` 适当调大到 300s。

---

整个方案跑通之后，开发提交代码到 main，大约 4-6 分钟后 ArgoCD 检测到 GitOps 仓库变更，开始滚动更新，整个过程完全自动。kaniko 的构建速度在开启 cache 之后比 DinD 快了约 30%，而且彻底解决了特权容器的安全审计问题。
