---
title: "混沌工程实战：Chaos Mesh 在 K8s 中注入故障"
date: 2026-04-12T14:00:00+08:00
draft: false
tags: ["chaos-engineering", "chaos-mesh", "kubernetes", "resilience", "SRE"]
categories: ["Kubernetes"]
description: "从混沌工程理念出发，用 Chaos Mesh 实战演示 PodChaos、NetworkChaos、IOChaos 及 Workflow 编排。"
summary: "混沌工程不是破坏系统，而是在可控环境中提前暴露脆弱点。本文记录了我用 Chaos Mesh 在生产级 K8s 集群中设计并执行混沌演练的完整过程，包括安装、实验配置、Workflow 编排和游戏日流程设计。"
toc: true
math: false
diagram: false
series: ["K8s 完全指南"]
keywords: ["chaos mesh", "混沌工程", "kubernetes", "故障注入", "弹性测试"]
params:
  reading_time: true
---

## 为什么需要混沌工程

2023年底，我们的服务在一次数据库主从切换中整体宕机超过20分钟。事后复盘发现：应用的重试逻辑没有做退避、连接池没有设置超时、健康检查探针没有覆盖依赖服务。这些问题在代码审查时都"看起来没问题"，却在真实故障时集体暴雷。

混沌工程（Chaos Engineering）的核心理念是：**在受控条件下主动制造故障，验证系统假设**。Netflix 的 Chaos Monkey 是这个领域的开山之作，而 [Chaos Mesh](https://chaos-mesh.org/) 则把这套理念带进了 Kubernetes 生态。

混沌工程不是随机破坏，它有严格的科学方法：

1. 定义稳态（Steady State）：系统正常运行时的可观测指标基线
2. 提出假设：注入 X 故障后，系统应该 Y（降级服务/自动恢复）
3. 设计最小爆炸半径的实验
4. 观察实际结果 vs 假设
5. 修复差距，循环迭代

## Chaos Mesh 安装

### 前置条件

- Kubernetes >= 1.20
- Helm 3
- 集群需要有 CRD 安装权限

### Helm 安装

```bash
# 添加 Helm repo
helm repo add chaos-mesh https://charts.chaos-mesh.org
helm repo update

# 创建专用命名空间
kubectl create ns chaos-mesh

# 安装（启用 Dashboard）
helm install chaos-mesh chaos-mesh/chaos-mesh \
  --namespace=chaos-mesh \
  --set dashboard.securityMode=false \
  --version 2.6.3
```

验证安装：

```bash
kubectl get pods -n chaos-mesh
# NAME                                        READY   STATUS
# chaos-controller-manager-xxx               3/3     Running
# chaos-daemon-xxx (DaemonSet)               1/1     Running
# chaos-dashboard-xxx                        1/1     Running
```

`chaos-daemon` 以 DaemonSet 方式运行在每个节点上，负责实际执行故障注入（进程信号、iptables 规则、文件系统操作等）。

### 访问 Dashboard

```bash
kubectl port-forward -n chaos-mesh svc/chaos-dashboard 2333:2333
```

浏览器打开 `http://localhost:2333`，这里可以图形化创建和管理实验。不过我更倾向 YAML 方式，便于纳入 Git 管理。

---

## PodChaos：Pod 层面故障

### 实验1：随机杀 Pod（模拟节点失联/OOM Kill）

```yaml
apiVersion: chaos-mesh.org/v1alpha1
kind: PodChaos
metadata:
  name: pod-kill-test
  namespace: chaos-testing
spec:
  action: pod-kill
  mode: one           # 每次随机杀1个
  selector:
    namespaces:
      - production
    labelSelectors:
      app: api-server
  scheduler:
    cron: "@every 2m"  # 每2分钟触发一次
```

**观察要点**：
- Deployment 的 `replicas` 是否自动补充
- 滚动窗口内 P99 延迟是否抬升
- 上游服务的重试是否正确触发

### 实验2：容器 CPU 压测

```yaml
apiVersion: chaos-mesh.org/v1alpha1
kind: StressChaos
metadata:
  name: cpu-stress-test
  namespace: chaos-testing
spec:
  mode: one
  selector:
    namespaces: [production]
    labelSelectors:
      app: worker
  stressors:
    cpu:
      workers: 4       # 4个goroutine跑满CPU
      load: 80         # CPU利用率目标80%
  duration: "5m"
```

这个实验帮我们发现了一个问题：Worker 服务的 HPA 配置 `targetCPUUtilizationPercentage: 80`，但 metrics-server 采集延迟约 30s，导致扩容总是慢半拍，队列已经积压了才开始扩。

---

## NetworkChaos：网络层故障

网络故障是最贴近真实的故障类型：机房网络抖动、跨可用区延迟、DNS 劫持……

### 实验3：服务间网络延迟

```yaml
apiVersion: chaos-mesh.org/v1alpha1
kind: NetworkChaos
metadata:
  name: network-delay-api-to-db
  namespace: chaos-testing
spec:
  action: delay
  mode: all
  selector:
    namespaces: [production]
    labelSelectors:
      app: api-server
  delay:
    latency: "200ms"
    correlation: "25"   # 延迟相关性（模拟真实抖动）
    jitter: "50ms"      # 抖动范围
  direction: egress     # 出方向延迟
  target:
    selector:
      namespaces: [production]
      labelSelectors:
        app: mysql
    mode: all
  duration: "10m"
```

执行这个实验时，我们发现 ORM 的默认查询超时是 30s，而前端接口超时是 10s——意味着 DB 慢查询还在跑，但用户早就看到了 504。调整后把 DB 查询超时改成了 8s，并在查询层加了熔断。

### 实验4：网络分区（模拟 Pod 完全断网）

```yaml
apiVersion: chaos-mesh.org/v1alpha1
kind: NetworkChaos
metadata:
  name: network-partition-test
spec:
  action: partition
  mode: one
  selector:
    namespaces: [production]
    labelSelectors:
      app: cache-service
  direction: both       # 双向断网
  duration: "3m"
```

这个实验把我们的 Redis 客户端问题暴露了出来：客户端在连接断开后没有主动重连，而是一直等待，导致整个服务调用链被阻塞。解决方案是设置 `ReadTimeout/WriteTimeout` 并配合连接池的 `MaxConnAge`。

### 实验5：DNS 故障

```yaml
apiVersion: chaos-mesh.org/v1alpha1
kind: DNSChaos
metadata:
  name: dns-error-test
spec:
  action: error       # 返回 NXDOMAIN
  mode: all
  selector:
    namespaces: [production]
    labelSelectors:
      app: third-party-client
  patterns:
    - "*.external-api.example.com"   # 只影响外部API域名
  duration: "5m"
```

---

## IOChaos：文件系统故障

这类故障容易被忽视，但写日志、写本地缓存、写临时文件的服务都会受影响。

### 实验6：磁盘 IO 延迟

```yaml
apiVersion: chaos-mesh.org/v1alpha1
kind: IOChaos
metadata:
  name: io-latency-test
spec:
  action: latency
  mode: one
  selector:
    namespaces: [production]
    labelSelectors:
      app: log-aggregator
  volumePath: /var/log/app   # 目标路径
  path: "**/*.log"            # 只影响 .log 文件
  delay: "100ms"
  percent: 50                # 50% 的 IO 操作受影响
  duration: "10m"
```

### 实验7：磁盘写入错误

```yaml
apiVersion: chaos-mesh.org/v1alpha1
kind: IOChaos
metadata:
  name: io-fault-test
spec:
  action: fault
  mode: one
  selector:
    namespaces: [production]
    labelSelectors:
      app: data-writer
  volumePath: /data
  path: "**"
  errno: 28    # ENOSPC（磁盘满）
  percent: 10  # 10% 的写操作返回错误
  duration: "5m"
```

执行这个实验后发现，Data Writer 服务遇到 ENOSPC 直接 panic 退出，没有任何降级处理，也没有告警。修复后加了磁盘使用率监控和优雅的错误处理。

---

## Workflow：编排多步骤故障

单个实验验证单点脆弱性，而 Workflow 可以模拟级联故障——这才是真实大型故障的形态。

```yaml
apiVersion: chaos-mesh.org/v1alpha1
kind: Workflow
metadata:
  name: cascading-failure-drill
  namespace: chaos-testing
spec:
  entry: main-sequence
  templates:
    # 主序列：顺序执行
    - name: main-sequence
      templateType: Serial
      deadline: 30m
      children:
        - prepare
        - inject-db-latency
        - inject-cache-down
        - observe-and-wait
        - cleanup

    # 并行准备
    - name: prepare
      templateType: Parallel
      children:
        - baseline-check

    - name: baseline-check
      templateType: Suspend
      deadline: 2m    # 等待人工确认基线正常

    # 第一步：数据库延迟（模拟DB慢查询风暴）
    - name: inject-db-latency
      templateType: NetworkChaos
      networkChaos:
        action: delay
        mode: all
        selector:
          namespaces: [production]
          labelSelectors:
            app: api-server
        delay:
          latency: "500ms"
          jitter: "100ms"
        direction: egress
        target:
          selector:
            namespaces: [production]
            labelSelectors:
              app: mysql
          mode: all
        duration: 5m

    # 第二步：缓存层断网（雪上加霜）
    - name: inject-cache-down
      templateType: NetworkChaos
      networkChaos:
        action: partition
        mode: all
        selector:
          namespaces: [production]
          labelSelectors:
            app: api-server
        direction: egress
        target:
          selector:
            namespaces: [production]
            labelSelectors:
              app: redis
          mode: all
        duration: 3m

    # 观察窗口
    - name: observe-and-wait
      templateType: Suspend
      deadline: 10m

    # 清理（Chaos Mesh 到期会自动清理，这里显式列出）
    - name: cleanup
      templateType: Suspend
      deadline: 1m
```

这个 Workflow 模拟了一个典型的级联故障：DB 变慢 → 接口超时堆积 → 此时缓存又断 → 系统完全不可用。通过这个演练，我们验证了：

1. 熔断器能否在 DB 延迟超阈值时触发
2. 缓存 miss 兜底逻辑能否在缓存断线时优雅降级
3. 告警能否在 2 分钟内触达 on-call

---

## 观察系统行为

混沌实验期间要同步观察多个维度，我用的组合是：

### Prometheus + Grafana

关键指标：
```
# 接口成功率
sum(rate(http_requests_total{status=~"2.."}[1m])) / sum(rate(http_requests_total[1m]))

# P99 延迟
histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket[5m])) by (le, service))

# Pod 重启次数
increase(kube_pod_container_status_restarts_total[10m])
```

### 实时追踪

```bash
# 观察 Pod 状态变化
kubectl get pods -n production -w

# 观察 Endpoints 变化（验证服务发现健康）
kubectl get endpoints -n production -w

# 实时日志追踪
kubectl logs -n production -l app=api-server -f --since=1m
```

### Chaos Mesh 自带的实验状态

```bash
# 查看当前活跃实验
kubectl get podchaos,networkchaos,iochaos -n chaos-testing

# 查看实验详情
kubectl describe networkchaos network-delay-api-to-db -n chaos-testing
```

---

## 游戏日（Game Day）流程设计

游戏日是将混沌工程仪式化的重要实践，我们的执行流程如下：

### 准备阶段（提前1周）

1. **确定实验范围**：本次演练覆盖哪些服务，排除哪些（例如付款链路提前豁免）
2. **定义成功指标**：例如"DB延迟500ms时，P99接口延迟不超过2s，错误率不超过1%"
3. **准备回滚预案**：Chaos Mesh 实验可以随时 delete 停止，同时准备业务层回滚脚本
4. **通知相关团队**：让 on-call 工程师知情，避免误报告警被当成真实故障处理

### 执行阶段

```bash
# D-Day 检查清单
# 1. 确认监控面板正常
# 2. 确认告警规则启用
# 3. 确认参与人员就位（对讲/Slack频道）
# 4. 记录实验开始时间（便于后续Loki日志查询）

# 应用实验
kubectl apply -f game-day-workflow.yaml

# 开始记录观察
# 实验期间每5分钟汇报一次状态到频道
```

### 复盘阶段

复盘模板：

```markdown
## 游戏日复盘 - 2026-04-12

### 实验概述
- 注入类型：DB网络延迟500ms + Redis分区
- 持续时间：15分钟
- 影响范围：production namespace，api-server组

### 假设 vs 实际
| 假设 | 实际结果 | 差距 |
|------|----------|------|
| 熔断器30s触发 | 实际45s触发 | 配置阈值偏高 |
| 错误率<1% | 峰值达到3.2% | 降级逻辑有bug |
| 告警2min内触达 | 8min触达 | 告警规则需调整 |

### 行动项
- [ ] 调整熔断器阈值：降至20s
- [ ] 修复降级逻辑中的NPE
- [ ] 优化告警灵敏度
```

---

## 几点坑和建议

**1. 从小范围开始**

第一次不要直接打生产。先在 staging 环境把所有实验跑通，理解各类 Chaos 的实际效果，再逐步引入生产。

**2. 设置合理的 duration**

每个实验都要设置 `duration`，不要依赖手动删除。我见过忘记删实验导致网络延迟持续了2小时的事故。

**3. 注意 RBAC**

Chaos Mesh 需要较高权限（需要操作 iptables、发送进程信号）。在多租户集群里，建议用 `Chaos` 对象的 `namespace` + `labelSelector` 精确限制爆炸半径，不要用 `mode: all` + 全命名空间选择。

**4. 实验结果要存档**

每次实验的 YAML 和复盘结果存入 Git，形成"弹性积累"。半年后回头看，可以清晰看到系统韧性的成长曲线。

混沌工程是一个长期投入，不是一次性活动。每次发布新服务后，把对应的混沌实验也纳入验收清单——这才是真正把弹性工程化。
