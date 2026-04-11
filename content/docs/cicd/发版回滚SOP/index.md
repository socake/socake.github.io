---
title: "发版回滚 SOP"
date: 2025-12-09T16:00:00+08:00
draft: false
tags: ["CI/CD", "回滚", "运维"]
categories: ["CI/CD"]
description: "生产故障时如何快速决策并执行回滚：K8s rollout、ArgoCD、配置回滚，以及数据库变更的特殊处理"
summary: "涵盖回滚判断标准、K8s/ArgoCD/配置各层回滚操作、数据库变更的前向修复 vs 回滚取舍，以及完整的值班人员操作 SOP 模板。"
toc: true
math: false
diagram: false
keywords: ["回滚", "SOP", "kubectl rollout", "argocd", "数据库回滚", "故障处理"]
params:
  reading_time: true
---

## 一、什么时候应该回滚

**核心原则：宁可多回滚一次，不要在生产环境上试图修复一个未知问题。**

满足以下任一条件，应立即启动回滚流程：

```
判断标准（发版后 15 分钟内）
├── 错误率上升 > 1%（相比发版前基线）
├── P99 延迟上升 > 50%
├── 核心业务指标下降（下单量/转化率/支付成功率）
├── 出现 OOM / CrashLoopBackOff
├── 数据库连接池耗尽
├── 新增 CRITICAL 级别告警
└── 健康检查持续失败
```

**不要犹豫的场景**：问题明显由本次发版引入（发版前正常，发版后立刻异常），直接回滚，事后分析根因。

**可以先排查的场景**：告警是老问题，且有充足证据证明本次变更无关（例如只改了文案，告警是数据库慢查询）。

---

## 二、K8s 回滚

### 查看 Deployment 历史版本

```bash
kubectl rollout history deployment/my-app -n production

# 输出示例
REVISION  CHANGE-CAUSE
1         initial deploy
2         feat: add user profile API
3         feat: optimize query performance
4         feat: new checkout flow   ← 当前版本，问题就是这个
```

### 回滚到上一版本

```bash
kubectl rollout undo deployment/my-app -n production

# 验证回滚状态
kubectl rollout status deployment/my-app -n production --timeout=120s
```

### 回滚到指定版本

```bash
# 查看某个 revision 的详情
kubectl rollout history deployment/my-app -n production --revision=3

# 回滚到 revision 3
kubectl rollout undo deployment/my-app -n production --to-revision=3
```

### 回滚后验证

```bash
# 确认 Pod 镜像版本
kubectl get pods -l app=my-app -n production -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.containers[0].image}{"\n"}{end}'

# 确认所有 Pod 都 Ready
kubectl get pods -l app=my-app -n production

# 查看近期事件
kubectl describe deployment/my-app -n production | tail -30

# 验证错误率（依赖你的可观测性工具）
# 例如用 loki 查日志错误率
```

### 注意事项

`kubectl rollout undo` 依赖 Deployment 的 `.spec.revisionHistoryLimit`，默认 10。如果超出这个限制，老版本 ReplicaSet 已被清理，就无法通过 rollout undo 回滚，此时需要手动指定镜像 tag。

```bash
# 当 rollout undo 不可用时，直接指定镜像回滚
kubectl set image deployment/my-app \
  my-app=123456789.dkr.ecr.us-west-2.amazonaws.com/my-app:a1b2c3d \
  -n production
```

---

## 三、ArgoCD 回滚

如果你的集群用 ArgoCD 管理，`kubectl rollout undo` 会被 ArgoCD 的 sync 覆盖掉，需要通过 ArgoCD 的机制回滚。

### 方法一：ArgoCD UI 回滚

1. 打开 ArgoCD UI → 找到对应 Application
2. 点击 `History and Rollback`
3. 选择上一个健康版本的 sync 记录
4. 点击 `Rollback`

这会让 ArgoCD 临时 `OutOfSync`（回滚到历史 Git commit），但不修改 Git 仓库。

### 方法二：CLI 回滚

```bash
# 列出 sync 历史
argocd app history my-app

# 输出示例
ID  DATE                           REVISION
0   2025-12-09 10:00:00 +0000 UTC  abc1234
1   2025-12-09 14:30:00 +0000 UTC  def5678  ← 问题版本
```

```bash
# 回滚到 ID=0 的版本
argocd app rollback my-app 0

# 查看回滚状态
argocd app get my-app
```

### 方法三：Git revert（推荐，符合 GitOps 原则）

ArgoCD 回滚只是临时措施，正确做法是 revert GitOps 仓库的变更：

```bash
cd gitops-repo

# 找到问题提交
git log --oneline -10

# Revert 那次提交（会产生新的 commit，保留历史）
git revert HEAD --no-edit

git push origin main
```

ArgoCD 检测到 Git 变更后自动同步，这才是真正符合 GitOps 原则的回滚方式。

---

## 四、数据库变更回滚

数据库变更是回滚中最复杂的部分，需要区分两种情况：

### 情况一：纯加法变更（推荐做法，向前兼容）

如果数据库变更只是加字段、加表，不删除也不修改已有字段，旧版本代码通常可以正常运行，可以直接回滚应用代码，数据库变更无需回滚。

```sql
-- 这类变更是安全的，不影响回滚
ALTER TABLE orders ADD COLUMN shipping_note VARCHAR(500) NULL;
CREATE INDEX idx_orders_user_id ON orders(user_id);
```

### 情况二：破坏性变更（删列/改类型/重命名）

这类变更无法简单回滚，因为回滚后的旧代码依赖已不存在的列。

**策略：前向修复（Fix Forward）**

不回滚数据库，而是快速发布修复版本：

```
发现问题
  ↓
不要回滚数据库
  ↓
紧急修复代码（兼容新 schema）
  ↓
发布修复版本到生产
  ↓
事后补齐测试
```

**如果必须回滚数据库**：

```bash
# Flyway 回滚（需要提前写 undo 脚本）
flyway -url=... -user=... -password=... undo

# Liquibase 回滚（支持自动回滚简单变更）
liquibase --changelog-file=... rollback --tag=v1.2.0

# 手动回滚（最后手段）
# 在 SQL 文件中提前准备回滚脚本
```

**最佳实践**：每次数据库迁移脚本旁边放一个 `down` 脚本：

```
migrations/
  20251209_001_add_shipping_note.up.sql
  20251209_001_add_shipping_note.down.sql  ← 回滚脚本
```

```sql
-- down.sql
ALTER TABLE orders DROP COLUMN shipping_note;
```

### 原则总结

| 变更类型 | 回滚策略 |
|---------|---------|
| 新增表/列/索引 | 回滚应用代码，DB 变更留着 |
| 删列/改类型 | 优先前向修复，迫不得已才回滚 DB |
| 数据迁移（大量数据更新）| 提前备份，有 down 脚本时才考虑回滚 |

---

## 五、配置回滚

### Nacos / 配置中心

```bash
# 查看 Nacos 配置历史
curl "http://nacos:8848/nacos/v1/cs/history?dataId=my-service&group=DEFAULT_GROUP&tenant=qa&pageNo=1&pageSize=10"

# 在 Nacos UI 中：配置管理 → 历史版本 → 选择历史版本 → 回滚
```

### GitOps 配置回滚

如果配置已落入 Git，revert 是最干净的做法：

```bash
# 查看哪些提交改了配置
git log --oneline --all -- config/my-service/

# Revert 特定提交
git revert <commit-hash> --no-edit
git push origin main
```

### K8s ConfigMap / Secret 回滚

ConfigMap 变更不像 Deployment 有 rollout history，需要手动管理：

```bash
# 发版前备份当前 ConfigMap（建议加入发版流程）
kubectl get configmap my-app-config -n production -o yaml > /tmp/configmap-backup-$(date +%Y%m%d%H%M%S).yaml

# 回滚时恢复
kubectl apply -f /tmp/configmap-backup-20251209143000.yaml

# 重启 Pod 使配置生效（如果配置是通过环境变量注入）
kubectl rollout restart deployment/my-app -n production
```

---

## 六、回滚后验证 Checklist

```markdown
## 回滚验证 Checklist

### 立即验证（回滚后 3 分钟内）
- [ ] 所有 Pod 状态 Running，无 CrashLoopBackOff
- [ ] 错误率恢复到发版前水平
- [ ] 健康检查接口正常返回

### 深度验证（回滚后 10 分钟内）
- [ ] P99 延迟恢复正常
- [ ] 数据库连接池使用率正常
- [ ] 核心业务指标恢复（如下单量、支付成功率）
- [ ] 无新增告警
- [ ] 日志中无大量 ERROR

### 数据一致性（如有 DB 变更）
- [ ] 确认数据未损坏
- [ ] 检查是否有脏写（新旧版本并存期间）
- [ ] 必要时执行数据修复脚本

### 收尾
- [ ] 记录回滚时间和原因（在 ticket 中）
- [ ] 通知相关方（研发、产品、运营）
- [ ] 启动事后分析
```

---

## 七、事后分析

回滚结束不是终点，事后分析（Postmortem）才是防止下次重蹈覆辙的关键。

### 分析内容

```markdown
## 事后分析模板

### 基本信息
- 发生时间：2025-12-09 14:32 CST
- 发现时间：2025-12-09 14:35 CST（3 分钟后）
- 回滚完成：2025-12-09 14:41 CST（总影响 9 分钟）
- 影响范围：结账功能不可用，约 200 个请求失败

### 故障时间线
- 14:30 - 发版完成
- 14:32 - 错误告警触发（支付成功率从 99.2% 降至 60%）
- 14:35 - 值班工程师响应，判断为本次发版引入
- 14:36 - 开始执行回滚
- 14:41 - 回滚完成，错误率恢复正常

### 根因分析
本次变更新增了优惠券验证逻辑，在并发场景下出现死锁，
导致数据库连接池耗尽，请求全部超时。

### 为什么没有在 PRE 发现？
PRE 环境并发压力不足，测试数据量小，死锁场景未覆盖。

### 改进措施
- [ ] PRE 环境补充并发测试脚本（负责人：xx，截止：12/20）
- [ ] 代码层面：事务粒度拆分，减少锁竞争（负责人：xx，截止：12/15）
- [ ] 监控：新增数据库连接池使用率告警（阈值 80%）
- [ ] 流程：DB 锁相关变更发版前必须经过压测
```

---

## 八、值班人员操作手册

以下是给值班工程师的简明操作指引，在压力场景下按步骤执行，不遗漏。

```markdown
# 生产故障回滚手册（值班版）

## Step 1: 确认问题
□ 查看 Grafana 告警看板，确认指标异常趋势
□ 确认异常是在最近一次发版后出现
□ 若不确定，先查近 1 小时内是否有发版记录

## Step 2: 通知
□ 通知研发负责人（call/IM）
□ 通知产品/运营（如影响用户可见功能）
□ 在 incident 频道宣布开始处理

## Step 3: 执行回滚

### K8s 应用回滚
```bash
kubectl rollout undo deployment/[服务名] -n production
kubectl rollout status deployment/[服务名] -n production --timeout=120s
```

### ArgoCD 管理的集群
在 ArgoCD UI 执行 Rollback，或：
```bash
argocd app rollback [app名] [历史ID]
```

### 同时在 Git 中 revert
```bash
cd /path/to/gitops-repo
git revert HEAD --no-edit && git push
```

## Step 4: 验证恢复
□ Pod 全部 Running
□ 错误率恢复正常（对比发版前基线）
□ 核心功能验证

## Step 5: 记录
□ 在 incident ticket 记录：发现时间、回滚时间、影响范围
□ 通知相关方已恢复
□ 预约事后分析会议（24 小时内）
```

---

## 九、常见问题

**Q: ArgoCD 一直把我 rollout undo 的结果同步回去怎么办？**

先在 ArgoCD 中暂停自动同步：`argocd app set my-app --sync-policy none`，操作完后记得恢复。或者直接通过 ArgoCD 的 Rollback 功能操作，绕过这个问题。

**Q: 回滚后镜像 tag 怎么追踪？**

执行 `kubectl describe pod [pod-name] | grep Image`，应该能看到上一个版本的镜像 SHA。对照 CI 记录找到对应的 commit。

**Q: 数据库没有 down 脚本，已经执行了破坏性变更怎么办？**

优先选择前向修复：快速写一个兼容新 schema 的代码版本发布，比回滚 DB 安全得多。如果非要回滚，从 RDS 快照恢复是最后手段，但意味着丢失快照后的所有数据写入。
