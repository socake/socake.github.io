---
title: "GitHub Actions CI/CD 实战：从镜像构建到 K8s 部署"
date: 2025-12-08T10:00:00+08:00
draft: false
tags: ["GitHub Actions", "CI/CD", "Docker", "Kubernetes", "ECR"]
categories: ["CI/CD"]
description: "GitHub Actions 工作流设计实践：多阶段构建、推送至 ECR、触发 ArgoCD 同步，覆盖多环境发版策略"
summary: "完整的 GitHub Actions CI/CD 流水线设计：Docker 多阶段构建优化、ECR 推送、Kustomize 更新 GitOps 仓库触发 ArgoCD 自动部署，以及多环境（QA/PRE/PROD）的分支策略。"
toc: true
math: false
diagram: false
keywords: ["github actions", "CI/CD", "docker", "ecr", "kubernetes", "argocd"]
params:
  reading_time: true
---

# GitHub Actions CI/CD 实战：从镜像构建到 K8s 部署

## 一、CI/CD 流程总览

在基于 GitOps 的现代部署体系中，GitHub Actions 负责"构建侧"，ArgoCD 负责"部署侧"，两者通过 GitOps 仓库解耦。整体流程如下：

```
代码提交（Push / PR）
        │
        ▼
┌─────────────────────┐
│   GitHub Actions CI  │
│  1. 代码检出          │
│  2. 单元测试          │
│  3. Docker 多阶段构建  │
│  4. 推送镜像到 ECR    │
└─────────┬───────────┘
          │  触发 CD workflow
          ▼
┌─────────────────────┐
│   GitHub Actions CD  │
│  5. 检出 GitOps 仓库  │
│  6. kustomize 更新   │
│     image tag        │
│  7. commit + push    │
└─────────┬───────────┘
          │  Git 变更触发
          ▼
┌─────────────────────┐
│      ArgoCD          │
│  8. 检测仓库变更      │
│  9. 同步到 K8s 集群   │
│ 10. 滚动更新 Pod      │
└─────────────────────┘
```

这套架构的核心优势：构建产物（镜像）和部署配置（YAML）分离，ArgoCD 始终以 Git 为单一事实来源，回滚只需要 `git revert`。

---

## 二、GitHub Actions 核心概念速览

### 层级关系

| 层级 | 含义 | 说明 |
|------|------|------|
| Workflow | 工作流 | 一个 `.yml` 文件，定义整个自动化流程 |
| Job | 作业 | Workflow 中的独立执行单元，默认并行运行 |
| Step | 步骤 | Job 中按顺序执行的操作，共享同一个 runner 环境 |
| Action | 动作 | 可复用的步骤封装，来自 Marketplace 或自定义 |

### Trigger 触发条件

```yaml
on:
  push:
    branches: ["main", "release/*"]
    tags: ["v*"]
  pull_request:
    branches: ["main"]
  workflow_dispatch:           # 手动触发，支持自定义输入参数
    inputs:
      environment:
        description: "部署目标环境"
        required: true
        default: "qa"
        type: choice
        options: ["qa", "pre", "prod"]
```

### Runner 选择

| 类型 | 优点 | 缺点 | 适用场景 |
|------|------|------|----------|
| GitHub-hosted | 零维护，免费额度 | 构建慢，无法访问内网 | 开源项目、轻量 CI |
| Self-hosted | 速度快，可访问内网资源 | 需要自行维护 | 私有部署、大型项目 |

Self-hosted runner 注册到 K8s 集群的方案可以参考 [actions-runner-controller](https://github.com/actions/actions-runner-controller)，按需弹性扩缩。

### Secrets 与 Variables 管理

- **Secrets**：加密存储，用于 AWS 凭证、token 等敏感信息，在日志中自动脱敏
- **Variables**：明文存储，用于非敏感配置（如 ECR 地址、集群名称）
- 作用域：Repository → Environment → Organization，优先级从低到高

```yaml
# 引用方式
env:
  AWS_REGION: ${{ vars.AWS_REGION }}
  ECR_REGISTRY: ${{ secrets.ECR_REGISTRY }}
```

---

## 三、完整 CI 工作流示例

下面是一个生产可用的 CI 工作流，包含测试、多阶段构建、缓存加速和 ECR 推送。

```yaml
# .github/workflows/ci.yml
name: CI - Build and Push

on:
  push:
    branches: ["main", "release/*"]
    tags: ["v*"]
  pull_request:
    branches: ["main"]

# 同一分支只保留最新一次运行，旧的自动取消
concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true

env:
  AWS_REGION: us-west-2
  ECR_REGISTRY: 123456789012.dkr.ecr.us-west-2.amazonaws.com
  IMAGE_NAME: my-app

jobs:
  test:
    name: 单元测试
    runs-on: ubuntu-latest
    steps:
      - name: 检出代码
        uses: actions/checkout@v4

      - name: 设置 Go 环境
        uses: actions/setup-go@v5
        with:
          go-version: "1.23"
          cache: true                   # 自动缓存 Go modules

      - name: 运行测试
        run: go test ./... -v -race -coverprofile=coverage.out

      - name: 上传覆盖率报告
        uses: actions/upload-artifact@v4
        with:
          name: coverage-report
          path: coverage.out

  build-and-push:
    name: 构建并推送镜像
    runs-on: ubuntu-latest
    needs: test                         # 测试通过后才构建
    # PR 不推送镜像，只验证能否构建成功
    if: github.event_name != 'pull_request'

    outputs:
      image-tag: ${{ steps.meta.outputs.image-tag }}
      image-digest: ${{ steps.build.outputs.digest }}

    steps:
      - name: 检出代码
        uses: actions/checkout@v4

      # 配置 OIDC 认证（推荐，无需长期 Access Key）
      - name: 配置 AWS 凭证
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: arn:aws:iam::123456789012:role/github-actions-ecr
          aws-region: ${{ env.AWS_REGION }}

      - name: 登录 ECR
        id: login-ecr
        uses: aws-actions/amazon-ecr-login@v2

      # 设置 Docker Buildx（支持多架构构建和高级缓存）
      - name: 设置 Docker Buildx
        uses: docker/setup-buildx-action@v3

      # 生成镜像 tag 策略
      - name: 生成镜像元数据
        id: meta
        run: |
          SHORT_SHA=$(echo "${{ github.sha }}" | cut -c1-8)
          REF_SLUG=$(echo "${{ github.ref_name }}" | sed 's/[^a-zA-Z0-9._-]/-/g')

          # tag 策略：
          # push to main   → sha-xxxxxxxx, main-latest
          # push to release/1.2 → sha-xxxxxxxx, release-1.2-latest
          # push tag v1.2.3 → sha-xxxxxxxx, v1.2.3, latest
          IMAGE_TAG="${{ env.ECR_REGISTRY }}/${{ env.IMAGE_NAME }}"

          if [[ "${{ github.ref }}" == refs/tags/* ]]; then
            TAGS="${IMAGE_TAG}:${{ github.ref_name }},${IMAGE_TAG}:${SHORT_SHA},${IMAGE_TAG}:latest"
          else
            TAGS="${IMAGE_TAG}:${SHORT_SHA},${IMAGE_TAG}:${REF_SLUG}-latest"
          fi

          echo "tags=${TAGS}" >> $GITHUB_OUTPUT
          echo "image-tag=${SHORT_SHA}" >> $GITHUB_OUTPUT

      # 利用 GitHub Actions Cache 加速 Docker 构建层缓存
      - name: 构建并推送镜像
        id: build
        uses: docker/build-push-action@v6
        with:
          context: .
          push: true
          tags: ${{ steps.meta.outputs.tags }}
          # 使用 registry cache（需要 ECR 支持 OCI artifacts）
          cache-from: type=gha
          cache-to: type=gha,mode=max
          # 构建参数
          build-args: |
            BUILD_DATE=${{ github.event.head_commit.timestamp }}
            GIT_COMMIT=${{ github.sha }}

      - name: 输出镜像信息
        run: |
          echo "镜像 digest: ${{ steps.build.outputs.digest }}"
          echo "镜像 tag: ${{ steps.meta.outputs.image-tag }}"
```

### 多阶段 Dockerfile 示例

```dockerfile
# ---- 构建阶段 ----
FROM golang:1.23-alpine AS builder

WORKDIR /app

# 先复制依赖文件，利用 Docker 层缓存
# 只要 go.mod/go.sum 不变，这一层就会命中缓存
COPY go.mod go.sum ./
RUN go mod download

COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build \
    -ldflags="-s -w -X main.version=${GIT_COMMIT}" \
    -o /app/server ./cmd/server

# ---- 最终镜像 ----
FROM gcr.io/distroless/static-debian12

WORKDIR /app
# 只复制编译产物，不包含源码和编译工具链
COPY --from=builder /app/server .

# 非 root 用户运行
USER nonroot:nonroot

EXPOSE 8080
ENTRYPOINT ["/app/server"]
```

多阶段构建的最终镜像通常只有几 MB，极大缩短了拉取时间，也减少了攻击面。

---

## 四、CD 触发：更新 GitOps 仓库

CI 构建完成后，触发 CD workflow 更新 GitOps 仓库中的镜像 tag，ArgoCD 检测到变更后自动同步到集群。

### CD 工作流完整示例

```yaml
# .github/workflows/cd.yml
name: CD - Update GitOps

on:
  # 由 CI workflow 成功后触发
  workflow_run:
    workflows: ["CI - Build and Push"]
    types: [completed]
    branches: ["main", "release/*"]

  # 也支持手动触发
  workflow_dispatch:
    inputs:
      image-tag:
        description: "镜像 tag（commit sha）"
        required: true
      environment:
        description: "目标环境"
        required: true
        type: choice
        options: ["qa", "pre"]

jobs:
  update-gitops:
    name: 更新 GitOps 仓库
    runs-on: ubuntu-latest
    # 只有 CI 成功时才触发（手动触发时跳过这个检查）
    if: |
      github.event_name == 'workflow_dispatch' ||
      github.event.workflow_run.conclusion == 'success'

    steps:
      - name: 确定镜像 tag
        id: vars
        run: |
          if [[ "${{ github.event_name }}" == "workflow_dispatch" ]]; then
            echo "image-tag=${{ inputs.image-tag }}" >> $GITHUB_OUTPUT
            echo "environment=${{ inputs.environment }}" >> $GITHUB_OUTPUT
          else
            # 从触发的 CI workflow 获取 tag（通过 artifact 或 API）
            SHORT_SHA=$(echo "${{ github.event.workflow_run.head_sha }}" | cut -c1-8)
            echo "image-tag=${SHORT_SHA}" >> $GITHUB_OUTPUT
            echo "environment=qa" >> $GITHUB_OUTPUT
          fi

      # 检出 GitOps 仓库（独立仓库）
      - name: 检出 GitOps 仓库
        uses: actions/checkout@v4
        with:
          repository: my-org/gitops-config
          token: ${{ secrets.GITOPS_TOKEN }}    # 需要写权限的 PAT 或 GitHub App token
          path: gitops

      - name: 安装 kustomize
        uses: imranismail/setup-kustomize@v2

      # 使用 kustomize 更新镜像 tag
      - name: 更新镜像 tag
        working-directory: gitops/overlays/${{ steps.vars.outputs.environment }}/my-app
        run: |
          IMAGE="${{ env.ECR_REGISTRY }}/my-app"
          TAG="${{ steps.vars.outputs.image-tag }}"

          kustomize edit set image "${IMAGE}=${IMAGE}:${TAG}"

          echo "已更新镜像: ${IMAGE}:${TAG}"
          cat kustomization.yaml

      # 提交变更
      - name: 提交并推送变更
        working-directory: gitops
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"

          git add -A
          git diff --cached --quiet && echo "无变更，跳过提交" && exit 0

          git commit -m "chore(deploy): update my-app to ${{ steps.vars.outputs.image-tag }}

          Environment: ${{ steps.vars.outputs.environment }}
          Triggered by: ${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}"

          git push
```

### 为什么用 kustomize edit set image

相比直接用 `sed` 替换，`kustomize edit set image` 是幂等的，不会误改其他字段，也不依赖文件格式细节。执行后 `kustomization.yaml` 中的 `images` 字段会被规范化更新：

```yaml
# kustomization.yaml（更新后）
images:
  - name: 123456789012.dkr.ecr.us-west-2.amazonaws.com/my-app
    newTag: a1b2c3d4
```

---

## 五、多环境分支策略

### 分支与环境映射

```
main ──────────────────────────────────→ QA（自动部署）
        │
release/1.x ──────────────────────────→ PRE（需手动审批）
        │
tag v1.x.x ───────────────────────────→ PROD（需手动审批 + 多人审核）
```

### 在 workflow 中实现环境路由

```yaml
jobs:
  deploy:
    strategy:
      matrix:
        include:
          - branch: main
            environment: qa
            auto-deploy: true
          - branch: release/*
            environment: pre
            auto-deploy: false

    environment:
      name: ${{ matrix.environment }}    # 关联 GitHub Environment
      # PRE/PROD environment 配置了 required reviewers，push 后会暂停等待审批
```

### Environment Protection Rules

在 GitHub 仓库 Settings → Environments 中为 `pre` 和 `prod` 配置：

- **Required reviewers**：指定必须审批的人员（建议至少 1 人）
- **Wait timer**：部署前等待时间（如 5 分钟冷静期）
- **Deployment branches**：限制只有特定分支/tag 可以部署到该环境
- **Environment secrets**：该环境专属的 secrets（如 PROD 专用的 AWS 角色）

```yaml
jobs:
  deploy-prod:
    environment:
      name: production
      url: https://app.example.com      # 部署完成后显示在 Actions 界面
    # 只有 tag 触发时才运行
    if: startsWith(github.ref, 'refs/tags/v')
```

---

## 六、实用技巧

### 并发控制

防止同一环境被多个 workflow 同时部署：

```yaml
concurrency:
  group: deploy-${{ github.ref }}-${{ inputs.environment }}
  cancel-in-progress: false    # 部署任务不要取消，让它跑完
```

### Job 依赖链

```yaml
jobs:
  test:
    runs-on: ubuntu-latest
    ...

  build:
    needs: test                 # test 通过后才 build
    ...

  deploy-qa:
    needs: build
    ...

  deploy-pre:
    needs: deploy-qa            # QA 验证后才部署 PRE
    if: startsWith(github.ref, 'refs/heads/release/')
    ...

  deploy-prod:
    needs: deploy-pre
    if: startsWith(github.ref, 'refs/tags/v')
    ...
```

### Matrix Strategy 多架构构建

```yaml
jobs:
  build:
    strategy:
      matrix:
        platform: [linux/amd64, linux/arm64]
    steps:
      - name: 构建
        uses: docker/build-push-action@v6
        with:
          platforms: ${{ matrix.platform }}
          # 使用 manifest list 合并多架构镜像
          outputs: type=image,push-by-digest=true,name-canonical=true,push=true
```

更完整的多架构合并推送方案可以参考 [docker/build-push-action 官方示例](https://docs.docker.com/build/ci/github-actions/multi-platform/)。

### workflow_dispatch 手动触发参数

```yaml
on:
  workflow_dispatch:
    inputs:
      image-tag:
        description: "要部署的镜像 tag"
        required: true
      dry-run:
        description: "仅预览，不实际执行"
        type: boolean
        default: false
      log-level:
        description: "日志级别"
        type: choice
        options: [debug, info, warn, error]
        default: info
```

---

## 七、常见问题

### ECR 权限：强烈推荐 OIDC 而非 Access Key

**不推荐的方式**：将 `AWS_ACCESS_KEY_ID` 和 `AWS_SECRET_ACCESS_KEY` 存入 Secrets，密钥需要定期轮换，一旦泄露后果严重。

**推荐的方式**：OIDC（OpenID Connect）联合身份认证，GitHub Actions 临时获取 AWS Token，无需长期凭证。

配置步骤：

```bash
# 1. 在 AWS IAM 创建 OIDC Provider
aws iam create-open-id-connect-provider \
  --url https://token.actions.githubusercontent.com \
  --client-id-list sts.amazonaws.com \
  --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1

# 2. 创建 IAM Role，信任策略如下：
# {
#   "Version": "2012-10-17",
#   "Statement": [{
#     "Effect": "Allow",
#     "Principal": {
#       "Federated": "arn:aws:iam::ACCOUNT_ID:oidc-provider/token.actions.githubusercontent.com"
#     },
#     "Action": "sts:AssumeRoleWithWebIdentity",
#     "Condition": {
#       "StringLike": {
#         "token.actions.githubusercontent.com:sub":
#           "repo:my-org/my-repo:*"
#       }
#     }
#   }]
# }

# 3. 给 Role 附加 ECR 推送权限策略（AmazonEC2ContainerRegistryPowerUser）
```

Workflow 中对应配置：

```yaml
- name: 配置 AWS 凭证（OIDC）
  uses: aws-actions/configure-aws-credentials@v4
  with:
    role-to-assume: arn:aws:iam::123456789012:role/github-actions-ecr
    aws-region: us-west-2
    # 注意：不需要配置任何 access key secret
```

### 私有仓库克隆子模块

```yaml
- name: 检出代码（含子模块）
  uses: actions/checkout@v4
  with:
    submodules: recursive
    token: ${{ secrets.GITHUB_TOKEN }}    # 同组织私有子模块需要有权限的 token
```

### 超时设置

避免 hung job 消耗 Actions 时长：

```yaml
jobs:
  build:
    timeout-minutes: 30        # job 级别超时

    steps:
      - name: 耗时操作
        timeout-minutes: 10    # step 级别超时（更精细）
        run: make build
```

---

## 八、小结

一套完整的 GitHub Actions CI/CD 流水线核心要点：

1. **CI 和 CD 分离**：CI 构建镜像，CD 更新 GitOps 仓库，职责清晰
2. **OIDC 认证**：摒弃长期 Access Key，安全性显著提升
3. **多阶段构建 + 层缓存**：构建时间从分钟级降到秒级
4. **Environment Protection Rules**：生产环境部署必须经过审批，防止误操作
5. **concurrency 并发控制**：同一环境不允许并发部署，避免竞态条件

整套方案不依赖任何自建 CI 系统，对中小团队非常友好，配合 ArgoCD 实现真正的 GitOps 闭环。
