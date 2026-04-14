---
title: "混沌工程 GameDay 实战指南：从第一次演练到常态化故障注入"
date: 2025-08-27T10:00:00+08:00
draft: false
tags: ["混沌工程", "GameDay", "SRE", "可靠性"]
categories: ["SRE"]
description: "我们如何从一次仓促上线的 GameDay 开始，逐步把混沌工程做成常态化流程。覆盖 GameDay 方法论、假设驱动实验、Chaos Mesh / LitmusChaos 工具选型、场景目录、复盘模板、安全护栏和组织推进。"
summary: "别把混沌工程理解成随便 kill pod。真正有价值的是一套假设驱动的演练方法论：演练前写下假设，演练中验证，复盘后改进系统和流程。"
toc: true
math: false
diagram: false
keywords: ["混沌工程", "GameDay", "故障注入", "Chaos Mesh"]
params:
  reading_time: true
---

## 先说一个教训

我们团队第一次做 GameDay 是 2024 年春天。那次计划得非常简单：周五下午在 staging 环境注入一些故障，看业务反应。结果混沌实验跑了一半，我们发现 staging 的监控告警居然没接钉钉，一堆故障打进去业务团队什么都不知道；有几个关键告警的路由错了，发到了一个离职同事的邮箱；还有一个 alert 本身 `for: 30m`，根本没在演练窗口内触发。

我们那次的结论是：**连演练都做不成，因为系统根本没准备好被演练**。这其实就是混沌工程的价值——它不是为了证明系统「能扛」，而是为了发现系统在哪儿没准备好。

后来我们把混沌工程当成一个长期工作来做：有方法论、有工具、有目录、有复盘、有安全护栏。这篇文章是一年多时间踩过的所有坑和得到的所有经验。

## 一、混沌工程的几个误区

先把误区说清楚，不然后面都白讲。

### 误区 1：混沌工程 = 随便 kill pod

kill pod 是最基础的一类故障，但「每小时随机 kill 一个 pod 看看」不是混沌工程，是骚扰。有价值的演练都有**明确的假设**：我相信 A 在 B 条件下会 C，演练就是为了验证这个信念。

### 误区 2：先有完美系统再演练

恰恰相反。系统越不完美，演练越值得做。我们第一次演练暴露的全是监控、告警、文档这些「本该有」的东西，价值极大。

### 误区 3：只在测试环境做

Chaos Engineering 的原始论文（Netflix Principles of Chaos）就强调 prod 演练的必要性。测试环境永远无法复现 prod 的拓扑、负载、故障模式。但是，**prod 演练必须有 blast radius 限制和安全护栏**。我们的路线：先 dev → staging → prod（非高峰）→ prod（任意时段），每一步都要先稳定跑一段时间。

### 误区 4：混沌工具选型是最重要的

远远不是。工具选型是 20%，演练设计和团队文化是 80%。一个正确设计的 `kubectl delete pod` 比一个复杂的 Chaos Mesh workflow 价值大得多。

## 二、GameDay 的基本结构

GameDay 是混沌工程里一种特定形式的活动：时间固定、参与者固定、有主题、有假设、有复盘。相比之下「常态化自动故障注入」是另一种形式，更轻量但参与度低。我建议两种都做，GameDay 解决团队学习和文化问题，常态化解决持续验证问题。

一次完整的 GameDay 通常有这几个阶段：

1. **Pre-GameDay（提前 1~2 周）**：确定主题和场景，写假设文档，审阅风险；
2. **Pre-flight（当天早上）**：确认环境状态、监控/告警、值班人员、回滚路径；
3. **Execution（30~90 分钟）**：按顺序执行实验，记录现象；
4. **Debrief（立即复盘 15~30 分钟）**：团队一起走一遍时间轴；
5. **Postmortem（48 小时内）**：写正式文档，列出 action items。

### 假设驱动实验

我们的假设模板：

```
【实验标题】MySQL 主库 failover 时业务影响
【假设】在 MySQL Primary pod 被删除时：
  1. 业务 p99 latency 不超过 2s；
  2. 业务错误率不超过 0.5%；
  3. 总恢复时间小于 60s；
  4. 告警会在 30s 内被触发并通知值班；
  5. 运维文档里的 failover 步骤能被严格执行。
【演练动作】kubectl delete pod mysql-primary-0 -n database
【稳态指标】
  - sum(rate(http_requests_total{status!~"5.."}[1m])) / sum(rate(http_requests_total[1m])) > 0.995
  - histogram_quantile(0.99, rate(http_request_duration_seconds_bucket[1m])) < 2
【回滚条件】
  - 错误率 > 5% 持续 60s，立即结束实验
  - 非预期的影响蔓延到无关服务
【参与人】SRE A, DBA B, 业务 owner C, 主持 D
【时间窗口】2025-08-14 14:00~15:00 (低峰)
```

每次演练都从这个模板开始。如果你连假设都写不清楚，说明对系统理解还不够，不该做这个实验。

## 三、场景目录：我们的 40 个故障剧本

一年下来我们整理了一个故障剧本库，按 impact 维度分类。部分清单：

### 基础设施层

1. Worker node 强制 drain（模拟 AZ 故障）
2. Karpenter 节点突然被回收
3. kubelet 不可用导致 pod 全 NotReady
4. 某 node 磁盘写满 `/var/lib/kubelet`
5. 某 node 时钟偏移 5 分钟
6. 某 node 网络 500ms 延迟
7. 某 node 网络 10% 丢包
8. 整个 AZ 出流量被拒（模拟跨区故障）

### Pod / 应用层

9. 业务 pod 被 kill
10. 业务 pod OOM
11. 业务 pod CPU 被 throttle 到 100%
12. 业务 pod 磁盘写满 `/tmp`
13. 业务 pod 被 stop-the-world（SIGSTOP）
14. sidecar（envoy）被 kill

### 网络层

15. DNS 解析失败（coredns 全挂）
16. DNS 解析慢（1s latency）
17. 业务 pod 无法访问 service ClusterIP
18. 跨 namespace 通信被 NetworkPolicy 拒绝
19. 业务 pod 到外部 API 的出口丢包
20. TLS 握手失败（CA 证书过期）

### 存储层

21. MySQL Primary 被 kill（failover）
22. PostgreSQL 主从切换
23. Redis 主节点失联
24. Kafka broker 被 kill
25. S3 区域性不可用（通过 networkchaos 模拟）
26. EFS 挂载变成 IO 抖动

### 中间件

27. Istio Pilot 重启
28. NGINX Ingress 全部重启
29. Cert-manager 停止工作
30. 集群 CA 证书过期
31. etcd leader 选举

### 业务依赖

32. 上游 API 返回 500
33. 上游 API 延迟 10s
34. 上游 API 断连
35. 消息队列消费停止
36. 下游数据库只读

### 人为故障

37. 误删一个关键 Deployment
38. GitOps 配置错误触发雪崩部署
39. Helm upgrade 失败
40. 误改 DNS 记录

每个剧本都有一份「预期行为 + 观测手段 + 回滚步骤」的文档。这是一年下来最大的资产，远比任何工具选型重要。

## 四、工具选型：Chaos Mesh vs LitmusChaos vs 自研脚本

### Chaos Mesh

PingCAP 开源，CNCF 毕业项目。在 K8s 原生性和 UI 上做得最好。

优点：

- K8s CRD 原生，`kubectl apply -f pod-kill.yaml` 就能跑；
- 丰富的故障类型：pod/network/stress/dns/io/time/kernel/http；
- Dashboard 直接看实验状态；
- Schedule 支持 cron 常态化注入；
- Workflow 支持复杂组合场景。

缺点：

- 对非 K8s 环境（EC2、裸机）支持差；
- RBAC 粒度不够细，容易给太多权限；
- `StressChaos` 依赖 stress-ng，容器镜像要自己构建。

```yaml
apiVersion: chaos-mesh.org/v1alpha1
kind: PodChaos
metadata:
  name: kill-order-api
  namespace: chaos-testing
spec:
  action: pod-kill
  mode: one
  selector:
    namespaces:
      - order
    labelSelectors:
      app: order-api
  duration: "30s"
```

```yaml
apiVersion: chaos-mesh.org/v1alpha1
kind: NetworkChaos
metadata:
  name: latency-to-db
spec:
  action: delay
  mode: all
  selector:
    namespaces: [order]
    labelSelectors: { app: order-api }
  delay:
    latency: 500ms
    jitter: 100ms
  target:
    mode: all
    selector:
      namespaces: [database]
      labelSelectors: { app: mysql }
  direction: to
  duration: 5m
```

### LitmusChaos

CNCF incubating。设计理念是「实验即 CR」，每个实验是一个 ChaosExperiment 资源，加上一个 ChaosEngine 去触发。

优点：

- ChaosHub 有丰富的公共实验；
- 更强调「实验参数化 + 可复用」；
- 和 Argo Workflow 集成好，工作流场景丰富。

缺点：

- 学习曲线比 Chaos Mesh 陡；
- UI 偏管理视角，不如 Chaos Mesh 易用。

### 自研脚本

对于简单场景，一个 Bash 脚本 + kubectl + tc + iptables 就够了。我们生产里相当一部分演练还是靠脚本跑，因为：

- 透明可控：脚本里每一步都能看到；
- 不引入额外依赖；
- RBAC 直接用执行脚本的人的身份。

例子：模拟某个 service 到外部 API 的丢包：

```bash
#!/usr/bin/env bash
set -euo pipefail

NAMESPACE=order
POD=$(kubectl get pod -n $NAMESPACE -l app=order-api -o name | head -1)

kubectl exec -n $NAMESPACE $POD -- \
  tc qdisc add dev eth0 root netem loss 10% delay 200ms

trap 'kubectl exec -n $NAMESPACE $POD -- tc qdisc del dev eth0 root' EXIT

echo "注入完成，5 分钟后自动清理"
sleep 300
```

这段脚本的价值在于「trap 兜底回滚」，任何异常退出都会清理 tc 规则。

### 我们的选型

- GameDay 用 Chaos Mesh + 脚本混合：CR 定义标准场景，脚本处理非 CR 场景；
- 常态化注入用 Chaos Mesh Schedule；
- 跨集群和非 K8s 故障用 AWS Fault Injection Simulator 或脚本。

## 五、安全护栏：演练不能变成事故

prod 演练必须有护栏，否则一旦失控就是真事故。我们的护栏机制：

### 1. Blast Radius 限制

每次实验只影响最小单元：

- Pod-level：只 kill 一个 pod；
- Node-level：只影响一个 node；
- Service-level：只对一个 service 注入；
- Tenant-level：只影响一个租户。

Chaos Mesh 的 `mode: one` 就是这个意思。mode 有 `one/all/fixed/fixed-percent/random-max-percent` 几种，生产演练永远用 `one` 或 `fixed-percent` 配合小比例。

### 2. Stop-Loss 自动回滚

基于稳态指标的自动熔断：

```yaml
# Chaos Mesh 1.x 没有原生 auto-abort，我们用外围 cron 监控
apiVersion: batch/v1
kind: CronJob
metadata:
  name: chaos-stop-loss
spec:
  schedule: "* * * * *"
  jobTemplate:
    spec:
      template:
        spec:
          containers:
          - name: checker
            image: curlimages/curl
            command: ["sh", "-c"]
            args:
              - |
                err_rate=$(curl -s "http://prometheus/api/v1/query?query=..." | jq ...)
                if [ "$err_rate" -gt "5" ]; then
                  kubectl delete -n chaos-testing podchaos --all
                  kubectl delete -n chaos-testing networkchaos --all
                  echo "熔断：错误率 $err_rate%"
                fi
```

Chaos Mesh 2.x 开始有 `StatusCheck` 资源，可以作为实验的前置和过程检查，到期或指标越界就自动停。

### 3. 时间窗口限制

我们只在「工作日 14:00-16:30」窗口做 prod 演练。这段时间值班人齐、团队清醒、用户流量没到晚高峰。演练脚本开头强制检查当前时间：

```bash
HOUR=$(date +%H)
DAY=$(date +%u)
if [ "$DAY" -ge 6 ] || [ "$HOUR" -lt 14 ] || [ "$HOUR" -ge 17 ]; then
    echo "不在允许的演练窗口"
    exit 1
fi
```

### 4. 审批和公告

- 演练前 2 天在团队频道公告；
- 涉及 prod 的演练需要 SRE Lead 和业务 owner 审批；
- 演练中在全员频道实时播报；
- 结束后写简报。

### 5. 权限隔离

执行 chaos 实验的 ServiceAccount 权限尽量窄。Chaos Mesh 有 `ChaosMeshAllowList` 机制，限制能操作的命名空间：

```yaml
apiVersion: chaos-mesh.org/v1alpha1
kind: ClusterRole
# 只允许对 namespace=order 下的 pod 操作
```

## 六、复盘：价值比执行大得多

**没复盘的演练等于没演练**。我们的复盘模板：

```
# GameDay 复盘 - 2025-08-14
## 演练主题
MySQL 主库 failover 场景

## 参与
主持 / 记录 / 红队 / 蓝队 / 观察员

## 时间轴
14:00 宣布开始
14:01 执行 kubectl delete pod mysql-primary-0
14:02 告警 MySQLPrimaryDown 触发，钉钉收到
14:02:30 业务 p99 从 150ms 涨到 800ms
14:03 新 primary 选举完成
14:04 p99 回落到 200ms
14:06 业务恢复至基线
14:10 实验结束，清理

## 假设验证
[✓] 业务 p99 不超过 2s
[✗] 业务错误率不超过 0.5%（实际观测 1.2%）
[✓] 总恢复时间 < 60s（实际 40s）
[✓] 告警 30s 内触发
[✗] 运维文档能被严格执行（step 3 描述不准）

## 发现的问题
1. 错误率超出阈值：客户端连接池没有快速剔除失效连接
2. 运维文档步骤 3 的命令已经过时
3. 告警聚合导致钉钉消息被合并，看起来只有一条
4. Grafana dashboard 的 p99 面板 delay 了 30s

## Action Items
- [ ] JDBC 连接池加 validateOnBorrow（@张三，1 周）
- [ ] 更新 failover runbook（@李四，本周）
- [ ] 告警聚合窗口从 5m 改 30s（@王五，本周）
- [ ] Grafana p99 面板加 1m 对比线（@王五，本周）

## 没验证到的
- 跨区域 failover 没测
- 长事务在切换时的行为没观察
```

**核心原则**：每条问题必须有对应的 action item，有 owner 和 deadline，下一次演练开始前要 review 上一次的 action item 执行状态。

## 七、常态化故障注入

GameDay 是手动的、低频的。真正把混沌工程变成日常保障是常态化注入。Chaos Mesh Schedule 示例：

```yaml
apiVersion: chaos-mesh.org/v1alpha1
kind: Schedule
metadata:
  name: daily-pod-kill
spec:
  schedule: "0 3 * * 1-5"   # 工作日凌晨 3 点
  historyLimit: 10
  type: PodChaos
  podChaos:
    action: pod-kill
    mode: one
    selector:
      namespaces: [order]
      labelSelectors:
        chaos-candidate: "true"
    duration: "30s"
```

关键做法：

1. 业务需要主动加 `chaos-candidate: "true"` label 才会被 kill，opt-in 制度。
2. 从「每周一次」开始，逐步提高到「每天一次」，最后到「每小时一次」。
3. 每次注入都通过钉钉简报形式发到团队频道，保持可见性。
4. 一旦业务失败率涨到阈值，自动停一周再重启。

常态化注入的价值不在于「发现新问题」，而在于「让系统持续维持可恢复性」。团队知道凌晨有自动 kill 后，对 deployment 的 graceful shutdown、readiness probe、retry 的重视程度都会上一个台阶。

## 八、案例复盘：一次「简单 kill pod」发现的 7 个问题

演练动作：`kubectl delete pod order-api-xxx`。你以为这是最简单的实验？我们从这一个动作中发现了 7 个真实问题：

1. **preStop 没配**：pod 被立刻 SIGTERM，in-flight 请求全挂；
2. **readiness probe 滞后**：新 pod 启动后第一时间 ready，但 JIT 还没 warm up，前 2 秒 p99 飙 5 倍；
3. **Service LB 刷新慢**：kube-proxy 刷新 iptables 规则有 10 秒延迟，kill 的 pod IP 还在转发列表里；
4. **下游重连不释放连接**：gRPC 长连接没及时探测 broken，持续往旧 pod 发请求；
5. **Prometheus up 没告警**：up{pod="order-api-xxx"}=0 状态没进告警规则；
6. **Runbook 没有「紧急复活」步骤**：业务团队不知道怎么处理单个 pod 被误 kill；
7. **PodDisruptionBudget 没配**：虽然单 kill 没触发，但我们发现整个 namespace 都没 PDB。

一个 `delete pod` 命令带出一串改进项。这就是混沌工程的真正价值。

## 九、组织推进：说服团队和管理层

推动混沌工程最难的不是技术，是组织。几个我们用过有效的切入角度：

1. **用事故讲故事**。每次真实事故复盘后，问一句「这个问题可以通过演练提前发现吗？」—— 有答案就推演练；
2. **小范围试点**。找一个有痛点的业务团队做第一次 GameDay，拿到 action items 和改进后的系统，作为样板；
3. **转化为 SLO 语言**。混沌工程的产出是「SLO 的可信度」，没有演练的 SLO 是未经验证的承诺；
4. **金字塔推广**：技术人员（SRE/开发）→ 技术主管 → 业务 owner → CTO。不要一开始就找高层要授权；
5. **不要神化**。混沌工程不是银弹，它只解决「系统在面对已知故障时是否足够健壮」这一类问题。不能解决需求质量、架构选型、人为流程错误。

## 十、工具之外的护栏：文化的四条准则

最后给出四条我们自己定的混沌工程文化准则：

1. **永远不在没有人盯着的时候做 prod 演练**。自动化注入可以无人值守，但 GameDay 必须有专人看监控。
2. **实验失败不是团队失败**。发现问题是演练的目标，把问题记录下来并改进比「没出事」有价值。
3. **不做没有假设的实验**。随机 kill pod 是 anti-pattern。
4. **不做没有复盘的实验**。演练完大家拍拍屁股走人，比没做还糟糕。

## 十一、踩坑清单

把一些踩过的坑列出来，供你避开：

- Chaos Mesh 的 `duration` 不是执行时长而是故障持续时间，`duration: 30s` 意味着 30s 后自动清理，不是执行完成时间。
- NetworkChaos 在跨节点场景可能对双向规则都生效，容易把自己断连。
- `StressChaos` 的 CPU 压力是进程级的，不会绕过 cgroup，如果 pod 的 limits 是 0.5 core，stress 打到 100% 也只是这 0.5core。
- `DNSChaos` 依赖 coredns，coredns 本身如果不在 target 上，chaos 可能没效果。
- Chaos Mesh 的 CR 删除是异步的，`kubectl delete podchaos` 可能要 30s 才真正清理。
- LitmusChaos 的 `probe` 会作为实验前置检查，probe 失败整个实验会被跳过，但这个行为默认是 silent 的。
- 容器内 tc 规则会在容器重启后自动消失，这是「好事」；但 iptables 规则可能残留。
- 使用 chaos mesh time skew 时间偏移会影响容器内 java 应用的 TLS 校验，导致所有外部 HTTPS 调用失败。

## 十二、落地路径

如果你要从零开始推混沌工程，建议按这个 4 阶段走：

1. **Month 1**：搭 Chaos Mesh，在 dev 环境跑 pod-kill，做一次完整 GameDay，重点跑通流程；
2. **Month 2~3**：扩到 staging，覆盖 10~15 个场景，建立场景库和文档；
3. **Month 4~6**：谨慎推 prod，先做非高峰期的 pod-kill 和 network-delay，建立审批和回滚机制；
4. **Month 6+**：常态化注入启动，每周 GameDay 常态化，每月做一次「复合故障」演练。

一年后你会看到两个明显变化：业务代码的 resiliency 显著提升（retry、超时、熔断普遍有了）；团队对线上事故的反应速度变快（流程熟了）。这两件事都是真金白银省下来的 downtime。

## 十三、延伸阅读

- Netflix 《Principles of Chaos Engineering》
- Casey Rosenthal / Nora Jones 《Chaos Engineering》（O'Reilly）
- Google SRE Book 第 14 章 Testing for Reliability
- PingCAP 《Chaos Mesh 2.x 官方文档》
- AWS Well-Architected Reliability Pillar

混沌工程不是一次性项目，而是长期文化。把这篇文章读完之后，我希望你能在下个月就排上第一次 GameDay，而不是等「时机成熟」。时机永远不会完美，第一次一定会翻车，但那正是你系统需要的第一课。

## 参考资料

- Chaos Mesh 官方文档 2.x
- LitmusChaos 文档与 ChaosHub
- Netflix Tech Blog Chaos Engineering 系列
- Google SRE Workbook 第 12 章
