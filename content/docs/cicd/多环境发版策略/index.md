---
title: "多环境发版策略设计"
date: 2025-12-09T15:00:00+08:00
draft: false
tags: ["CI/CD", "发版", "GitOps"]
categories: ["CI/CD"]
description: "从环境划分、分支策略、镜像 tag 规范到金丝雀发布，设计一套完整的多环境发版体系"
summary: "覆盖环境划分标准、分支策略（GitFlow vs Trunk-based）、镜像 tag 策略、自动/手动审批节点、金丝雀发布、蓝绿部署，以及发版后验证 checklist。"
toc: true
math: false
diagram: false
keywords: ["CI/CD", "多环境", "发版策略", "gitops", "金丝雀发布", "蓝绿部署"]
params:
  reading_time: true
---

## 一、环境划分标准

在一套成熟的研发流程中，至少需要三个独立环境，各自承担不同职责：

| 环境 | 定位 | 谁可以访问 | 数据 | 变更频率 |
|------|------|-----------|------|---------|
| **DEV / QA** | 功能验证、集成测试 | 研发、测试 | 脱敏测试数据 | 随时 |
| **PRE / Staging** | 灰度验证、性能测试、产品验收 | 研发、测试、产品 | 类生产数据量级（脱敏）| 发版前 |
| **PROD** | 生产环境 | 运维、on-call | 真实数据 | 受控窗口 |

几点说明：
- DEV 和 QA 可以合并为一个环境，降低维护成本；PRE 和 Staging 同义
- QA 允许随时推送，不需要审批，快速迭代
- PRE 应尽量和 PROD 配置对齐（副本数可以少，但配置项必须一致）
- PROD 变更需要审批记录，回溯时能知道是谁在何时做了什么

---

## 二、分支策略

### GitFlow（适合发版节奏固定的团队）

```
main（只接受 release 分支的 merge，永远是稳定状态）
  ↑
release/1.2.0（从 develop 切出，修 bugfix，打 tag 后合回 main 和 develop）
  ↑
develop（功能集成分支，对应 QA 环境）
  ↑
feature/xxx（功能分支，开发完后 merge 到 develop）
```

触发规则：
- `feature/*` push → 跑单测，不部署
- `develop` push → 自动部署 QA
- `release/*` push → 自动部署 PRE
- `v*.*.*` tag → 自动部署 PROD（可加人工审批）

**优点**：分支职责清晰，适合迭代周期固定（如双周发版）的团队  
**缺点**：分支多，合并冲突频繁，维护成本高

### Trunk-based（适合高频发版团队）

```
main（唯一长期分支，所有开发者频繁合入）
  ↑
short-lived/feature-xxx（最长 2 天生命周期，merge 回 main 即删除）
```

发版靠 tag：
- `main` push → 自动部署 QA
- `main` push + 人工触发 → 部署 PRE
- 打 semver tag → 部署 PROD

**优点**：集成频繁，冲突少，CI 反馈快  
**缺点**：需要 Feature Flag 支持未完成功能的隔离，对团队纪律要求高

### 实际推荐

- 团队 < 10 人，发版节奏快 → Trunk-based
- 团队 > 10 人，有固定发版窗口 → GitFlow 或简化版（去掉 develop 分支，直接 feature → main）

---

## 三、镜像 Tag 策略

镜像 tag 决定了每次部署的可追溯性。常见策略对比：

| 策略 | 示例 | 优点 | 缺点 |
|------|------|------|------|
| `latest` | `app:latest` | 简单 | 不可追溯，回滚困难 |
| 分支名 | `app:main` | 可以区分分支 | 同一 tag 内容不断变化 |
| commit SHA | `app:a1b2c3d` | 完全可追溯 | 不够直观 |
| semver | `app:v1.2.0` | 语义清晰 | 需要手动打 tag |
| 日期+commit | `app:20251209-a1b2c3d` | 可追溯+可排序 | 稍长 |

**推荐组合**：
- QA 环境：`{branch}-{short-sha}`，例如 `app:main-a1b2c3d`
- PRE 环境：`{branch}-{short-sha}` 或 release 分支名
- PROD 环境：`v{semver}` 或 `{short-sha}`，**禁止使用 `latest`**

在 GitHub Actions 中生成 tag：

```yaml
- name: 生成镜像 Tag
  id: meta
  run: |
    SHORT_SHA=$(echo $GITHUB_SHA | head -c 7)
    BRANCH=$(echo $GITHUB_REF_NAME | tr '/' '-')
    echo "tag=${BRANCH}-${SHORT_SHA}" >> $GITHUB_OUTPUT
    echo "sha=${SHORT_SHA}" >> $GITHUB_OUTPUT

- name: 构建并推送
  uses: docker/build-push-action@v5
  with:
    tags: |
      ${{ env.ECR_REGISTRY }}/${{ env.SERVICE_NAME }}:${{ steps.meta.outputs.tag }}
      ${{ env.ECR_REGISTRY }}/${{ env.SERVICE_NAME }}:${{ steps.meta.outputs.sha }}
```

---

## 四、发版流程设计

### 自动部署节点（无需人工干预）

```
代码 merge 到 develop/main
       ↓
CI: 单测 + 构建镜像 + 推送
       ↓
自动更新 GitOps 仓库（kustomize image tag）
       ↓
ArgoCD 检测到变更，自动同步到 QA
       ↓
QA 冒烟测试（自动化）
```

### 手动审批节点（PRE / PROD）

```
PRE 部署：
  - 触发方式：手动点击流水线 / PR 合并到 release 分支
  - 审批：无需审批，但需要 QA 验证通过
  - 通知：发 IM 通知相关方

PROD 部署：
  - 触发方式：打 semver tag / 手动触发
  - 审批：需要至少 1 人 approve（GitHub Environment protection rules）
  - 时间窗口：仅允许工作日 10:00–17:00
  - 通知：发 IM + 邮件通知
```

GitHub Actions 的 environment 审批配置：

```yaml
deploy-prod:
  needs: deploy-pre
  environment:
    name: production
    url: https://app.example.com
  runs-on: ubuntu-latest
  steps:
    - name: 部署到生产
      run: |
        # 更新 GitOps 仓库镜像 tag
        ./scripts/update-image-tag.sh $IMAGE_TAG
```

在 GitHub repo 的 Settings → Environments → production 中配置 Required reviewers，push 到该 environment 的 workflow 会暂停等待审批。

---

## 五、变更冻结窗口

变更冻结是降低发版风险的有效手段：

```yaml
# 流水线中检查是否在冻结期
- name: 检查变更冻结窗口
  run: |
    CURRENT_HOUR=$(TZ=Asia/Shanghai date +%H)
    CURRENT_DOW=$(TZ=Asia/Shanghai date +%u)  # 1=周一, 7=周日
    
    # 禁止周末部署生产
    if [ "$CURRENT_DOW" -ge 6 ]; then
      echo "❌ 禁止在周末部署生产环境"
      exit 1
    fi
    
    # 禁止非工作时间部署生产
    if [ "$CURRENT_HOUR" -lt 10 ] || [ "$CURRENT_HOUR" -ge 18 ]; then
      echo "❌ 仅允许 10:00-18:00 (CST) 部署生产"
      exit 1
    fi
    
    echo "✅ 在允许的发版窗口内"
```

节假日冻结通常通过配置文件或环境变量维护：

```bash
# 检查冻结列表
FROZEN_DATES="2025-12-24 2025-12-25 2025-12-31 2026-01-01"
TODAY=$(TZ=Asia/Shanghai date +%Y-%m-%d)
if echo "$FROZEN_DATES" | grep -qw "$TODAY"; then
  echo "❌ 今日为变更冻结期"
  exit 1
fi
```

---

## 六、金丝雀发布

金丝雀发布的核心是**先让少量流量验证新版本，确认无误后再全量切换**。

### 基于 Kubernetes 的流量切分

最简单的方式是利用多个 Deployment 副本数比例：

```yaml
# v1: stable，5 个副本
apiVersion: apps/v1
kind: Deployment
metadata:
  name: my-app-stable
spec:
  replicas: 5
  selector:
    matchLabels:
      app: my-app
      version: stable
  template:
    metadata:
      labels:
        app: my-app
        version: stable

---
# v2: canary，1 个副本（约 1/6 流量）
apiVersion: apps/v1
kind: Deployment
metadata:
  name: my-app-canary
spec:
  replicas: 1
  selector:
    matchLabels:
      app: my-app
      version: canary
  template:
    metadata:
      labels:
        app: my-app
        version: canary
```

Service selector 只匹配 `app: my-app`，流量按副本数比例分配。

### 分阶段放量流程

```
阶段 1: 10% 流量
  → 观察 5–10 分钟
  → 指标：错误率 < 0.1%，P99 延迟无明显上涨
  → 告警：无新增 CRITICAL 告警

阶段 2: 50% 流量
  → 观察 10–20 分钟
  → 同上

阶段 3: 100% 流量（全量切换）
  → 删除 stable Deployment
  → 下线金丝雀标记
```

如果任何阶段出现问题，立即缩减 canary 副本至 0，等于秒级回滚。

---

## 七、蓝绿部署

蓝绿部署维护两套完全相同的生产环境，切换时修改 Service selector：

```yaml
# 当前生产流量指向 blue
apiVersion: v1
kind: Service
metadata:
  name: my-app
spec:
  selector:
    app: my-app
    slot: blue    # 修改这里为 green 即可完成切换
  ports:
  - port: 80
    targetPort: 8080
```

切换脚本：

```bash
#!/bin/bash
set -e

CURRENT_SLOT=$(kubectl get svc my-app -o jsonpath='{.spec.selector.slot}')
NEW_SLOT=$([[ "$CURRENT_SLOT" == "blue" ]] && echo "green" || echo "blue")

echo "当前 slot: $CURRENT_SLOT → 切换到: $NEW_SLOT"

# 确认新 slot 的 Pod 都 Ready
kubectl rollout status deployment/my-app-${NEW_SLOT} --timeout=120s

# 切换 Service selector
kubectl patch svc my-app -p "{\"spec\":{\"selector\":{\"slot\":\"${NEW_SLOT}\"}}}"

echo "✅ 切换完成"
```

蓝绿的优点是回滚极快（把 Service selector 改回来），缺点是资源成本翻倍。

---

## 八、发版通知与审计

发版后应自动通知相关方，并留下可审计的记录：

```bash
# 钉钉通知示例
send_dingtalk_notification() {
  local env=$1
  local service=$2
  local version=$3
  local status=$4
  local operator=$5

  curl -s -X POST "$DINGTALK_WEBHOOK" \
    -H "Content-Type: application/json" \
    -d "{
      \"msgtype\": \"markdown\",
      \"markdown\": {
        \"title\": \"发版通知\",
        \"text\": \"### 发版通知\\n\"
          \"- **环境**: ${env}\\n\"
          \"- **服务**: ${service}\\n\"
          \"- **版本**: \`${version}\`\\n\"
          \"- **状态**: ${status}\\n\"
          \"- **操作人**: ${operator}\\n\"
          \"- **时间**: $(TZ=Asia/Shanghai date '+%Y-%m-%d %H:%M:%S')\\n\"
      }
    }"
}
```

---

## 九、发版后验证 Checklist

每次发版完成后，值班人员应执行以下验证：

```markdown
## 发版后验证 Checklist

### 基础指标（发版后 5 分钟内）
- [ ] Pod 全部 Ready（kubectl get pods -l app=xxx）
- [ ] 错误率与发版前持平（Grafana / DataDog）
- [ ] P99 延迟无明显上涨
- [ ] 无新增 CRITICAL/ERROR 日志
- [ ] 健康检查接口返回 200

### 业务验证（发版后 15 分钟内）
- [ ] 核心链路冒烟测试通过
- [ ] 数据库连接池无异常
- [ ] 缓存命中率正常
- [ ] 外部依赖（第三方 API）调用正常

### 收尾
- [ ] 更新 CHANGELOG / 发版记录
- [ ] 关闭对应 ticket/issue
- [ ] 如有数据库变更，确认迁移脚本执行完毕
- [ ] 通知产品/测试确认功能上线
```
