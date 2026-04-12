---
title: "高级运维/DevOps 工程师面试题精选：系统设计与深度考察"
date: 2025-12-11T12:51:00+08:00
draft: false
tags: ["面试", "DevOps", "SRE", "Kubernetes", "系统设计", "职业发展"]
categories: ["职业发展"]
description: "针对高级运维/DevOps/SRE 工程师面试，整理系统设计题 5 道和深度技术题 10 道，每题给出答题框架和关键思路，不只是标准答案。"
summary: "高级运维面试考什么？本文整理 5 道系统设计题和 10 道深度技术题，每题给出答题框架。从监控体系设计到 K8s 调度器原理，从生产事故复盘到新技术引入决策，帮你建立完整的回答思路。"
toc: true
math: false
diagram: false
keywords: ["DevOps 面试题", "SRE 面试", "K8s 系统设计", "运维面试", "高级工程师面试"]
params:
  reading_time: true
---

## 写在前面：高级岗位面试的核心差异

初级运维考"会不会用"，高级运维考"为什么这么用"和"出了问题怎么办"。面试官真正想评估的是：

- **系统化思维**：遇到问题能否拆解成子问题，逐层解决
- **取舍意识**：知道每个方案有什么代价，不会无脑推荐最复杂的
- **生产经验**：踩过哪些坑，从故障中学到什么
- **技术深度**：核心组件的原理，而不只是使用姿势

回答系统设计题时，不要直接给方案，先问清楚约束条件（规模、SLA、成本预算、团队规模），然后展开设计。

---

## 系统设计题

### 题1：设计支持 100 个微服务的监控告警体系

**答题框架：明确目标 → 分层设计 → 数据流 → 告警策略 → 运维闭环**

**先问约束**：
- 每秒指标量级？（100 服务 × 200 指标 × 60s 采集 ≈ 约 20 万 samples/min）
- 日志量级？（估算每天总日志 GB 数）
- RTO/告警响应时间要求？
- 团队规模？On-call 排班？

**分层设计**：

**第一层：数据采集**
- 指标：Prometheus + ServiceMonitor（K8s 场景）或 Prometheus 联邦，每个集群一个 Prometheus 实例负责抓取，通过 remote_write 写到中央存储
- 日志：各服务 stdout/stderr → Fluent Bit（轻量 sidecar 或 DaemonSet 方式）→ Kafka（缓冲）→ Loki 或 Elasticsearch
- 链路追踪：OpenTelemetry SDK → OTLP 协议 → Tempo 或 Jaeger

**第二层：存储**
- 指标：VictoriaMetrics 集群（相比 Prometheus 本地存储，压缩率更高，支持长期保留）
- 日志：Loki（索引少、成本低，适合云原生场景）或 Elasticsearch（全文检索能力更强）
- 关联：通过 TraceID 在日志、链路、指标之间跳转

**第三层：告警**
- 告警规则写在 VictoriaMetrics 的 vmalert 里（或 Prometheus AlertManager）
- 告警分级：P0（立即处理，5 分钟内响应）、P1（1 小时）、P2（工作日处理）
- 告警路由：按服务 label 路由到对应团队
- 告警抑制：批量故障时只发根因告警，抑制下游

**第四层：可视化与 On-call**
- Grafana：通用 Dashboard + 业务大盘
- On-call 平台：PagerDuty 或 OpsGenie，排班 + 升级策略
- 事后：告警收敛率、MTTR、MTTD 指标定期 Review

**踩坑点要提**：告警噪音是最大问题。初期先做到"告警必须可操作"，每个告警要有 Runbook 链接，无法操作的告警先 Silence 掉，不要让 On-call 工程师对告警脱敏。

---

### 题2：设计零停机发布方案

**答题框架：先问业务特性 → 分析三种方案 → 给出选择逻辑 → 回滚策略**

**三种方案对比**：

| 维度 | 滚动发布 | 蓝绿发布 | 金丝雀发布 |
|------|----------|----------|------------|
| 资源成本 | 低（复用现有节点） | 高（需要双倍资源） | 中 |
| 风险控制 | 中（逐步替换） | 低（可瞬间切换） | 最低（小比例验证） |
| 回滚速度 | 慢（需要反向滚动） | 快（切换流量） | 快（降低流量比例） |
| 复杂度 | 低 | 高（需管理两套环境） | 高（需流量染色/分割） |

**选择逻辑**：
- **无状态服务、数据库 schema 向前兼容**：滚动发布够用，K8s Deployment 自带支持
- **有状态服务或需要快速验证**：蓝绿发布，通过 Service 切换 selector 实现瞬间切流
- **核心服务、需要 A/B 测试或用户实验**：金丝雀发布，Istio/APISIX 的流量权重路由

**K8s 滚动发布配置要点**：

```yaml
strategy:
  type: RollingUpdate
  rollingUpdate:
    maxSurge: 1          # 最多多出 1 个 Pod
    maxUnavailable: 0    # 发布过程中始终保持所有 Pod 可用
```

`maxUnavailable: 0` + `maxSurge: 1` 是零停机的关键——先起新 Pod，新 Pod Ready 后再删旧 Pod。

**必须配合 ReadinessProbe**：没有 ReadinessProbe，K8s 不知道新 Pod 是否真正 Ready，可能把流量转发给启动中的服务。

**数据库 Schema 变更是最难的**：必须做到向前兼容（先加字段不删字段，先用双写再切到新逻辑），否则再好的发布策略也会出问题。

---

### 题3：如何保证 K8s 集群高可用

**答题框架：分层设计（控制面 / 工作节点 / 应用层）**

**控制面高可用**：
- etcd：奇数节点（3 或 5），跨可用区部署，Raft 保证一致性。节点故障时能容忍 (N-1)/2 个节点宕机（3节点容1，5节点容2）
- API Server：多实例（通常 3 个），前面挂 LB（AWS NLB 或内部 LB）。API Server 本身是无状态的，可以任意水平扩展
- Scheduler/ControllerManager：多实例，但同时只有一个 leader（通过 Leader Election 机制保证）

**工作节点高可用**：
- 跨可用区：Node 分布在至少 3 个 AZ，Pod 反亲和性规则确保关键服务不全在同一 AZ
- PodDisruptionBudget（PDB）：明确约束发布或节点维护时允许不可用的 Pod 数量
- HPA + CA（Cluster Autoscaler）：水平自动伸缩应对流量波动

**应用层高可用**：
- 多副本：重要服务至少 3 个副本
- ReadinessProbe/LivenessProbe：有问题的 Pod 及时下线
- 资源 Request/Limit：防止 OOM 影响其他 Pod

**一个容易忽视的点**：CoreDNS 高可用。K8s 集群内所有 DNS 解析依赖 CoreDNS，默认 2 个副本，建议提高到 3-4 个并配置反亲和性。CoreDNS 挂掉会导致所有 Service 发现失败。

---

### 题4：设计多云灾备方案

**答题框架：先定 RTO/RPO → 分层分析 → 方案设计 → 成本权衡**

**关键指标先确认**：
- RTO（Recovery Time Objective）：可以接受多久的中断时间？分钟级？小时级？
- RPO（Recovery Point Objective）：最多丢失多少数据？0 数据丢失（同步复制）？还是允许几分钟的丢失（异步复制）？

**方案层次**：

**冷备（低成本，RTO 4-8 小时）**：
- 数据定期备份到目标云（S3 Cross-Region Replication 或手动同步）
- 基础设施用 IaC（OpenTofu）描述，灾难时快速在目标云重建
- 适合对可用性要求不高的内部系统

**温备（中等成本，RTO 30 分钟-2 小时）**：
- 目标云保持基础设施框架（K8s 集群），但副本数缩到最小（省资源）
- 数据库异步复制到目标云的只读副本
- 发生灾难时：切 DNS → 目标云扩容 → 数据库提升为主库

**热备/多活（高成本，RTO < 5 分钟，近乎 RPO 0）**：
- 两个云同时承载流量（DNS 权重或 Anycast）
- 数据库同步双写（延迟和一致性是挑战，通常只做读多写少场景的多活）
- 全球 Load Balancer（Cloudflare、AWS Global Accelerator）做流量调度

**实践建议**：大多数公司做到"温备"性价比最高。真正的多活成本极高，一般只有电商大促、金融核心链路才值得投入。

---

### 题5：CI/CD 流水线如何做到安全

**答题框架：攻击面分析 → 每个阶段的安全措施**

**攻击面**：代码注入 → 依赖投毒 → 构建环境被攻陷 → 镜像篡改 → 部署配置泄露

**各阶段安全措施**：

**代码阶段**：
- SAST（静态应用安全测试）：Semgrep、SonarQube，在 PR 阶段扫描代码漏洞
- Secret 扫描：Gitleaks、TruffleHog 防止密码提交进 Git
- 依赖扫描：Trivy、Snyk 检测第三方库 CVE

**构建阶段**：
- Runner 隔离：不同项目用不同 Runner，防止横向污染
- 构建不出网（或只允许白名单域名），防止构建时拉恶意依赖
- 不在 Runner 上存长期 Secret，通过 Vault 或 CI Secret Manager 动态注入

**镜像阶段**：
- 镜像扫描：Trivy 扫描构建出的镜像，HIGH/CRITICAL 漏洞阻断发布
- 镜像签名：Cosign 对镜像签名，记录 Provenance（哪个 CI Job 在哪个 commit 构建的）
- 基础镜像：用 Distroless 或 Alpine，减少攻击面

**部署阶段**：
- Admission Webhook：K8s 集群只允许签名的镜像部署（Policy Controller）
- GitOps：基础设施变更通过 PR + Review，不允许直接 kubectl apply 到生产
- 最小权限：服务账号只有必要的 RBAC 权限

---

## 深度技术题

### 题1：K8s 调度器工作原理

**答题框架：三个阶段 → 每个阶段的关键点 → 扩展机制**

调度器（kube-scheduler）把 Pending 的 Pod 分配到合适的 Node，分三个阶段：

**预选（Filtering）**：过滤掉不满足条件的 Node

常见过滤插件：
- `NodeResourcesFit`：Node 剩余资源是否满足 Pod 的 Request
- `NodeAffinity`：nodeSelector / nodeAffinity 规则
- `TaintToleration`：Pod 是否 Tolerate Node 上的 Taint
- `PodTopologySpread`：跨 Zone/Node 的拓扑分布约束
- `VolumeBinding`：PVC 是否能绑定到该 Node（特别是 Local Volume）

经过预选后得到"可行节点"列表。如果列表为空，Pod 保持 Pending，事件里会有 "Insufficient cpu/memory" 或 "no nodes available" 等提示。

**优选（Scoring）**：对可行节点打分（0-100），选出最优节点

常见打分插件：
- `LeastAllocated`：优先选剩余资源多的 Node（均衡资源使用）
- `NodeAffinityPriority`：匹配 preferred nodeAffinity 的 Node 得高分
- `InterPodAffinityPriority`：考虑 Pod 间亲和性

**绑定（Binding）**：把 Pod 绑定到打分最高的 Node，写入 etcd

**扩展机制**：
- Scheduler Extension（Webhook）：在过滤/打分时调用外部服务（性能较差）
- Scheduler Framework Plugin：在调度框架内部插件点扩展（推荐）
- 多调度器：可以为特殊 Pod 指定自定义调度器（`schedulerName`字段）

---

### 题2：Pod OOMKilled 排查与预防

**答题框架：发现 → 定位根因 → 短期处置 → 长期预防**

**发现 OOMKilled**：

```bash
kubectl describe pod <pod-name>
# 看到 OOMKilled 和 Last State 的 Exit Code: 137

kubectl get events --field-selector reason=OOMKilling
```

**定位根因**：

```bash
# 看历史内存用量（Prometheus/Grafana）
container_memory_working_set_bytes{container="myapp", pod=~"myapp-.*"}

# 看 OOM 发生前的内存趋势：
# 1. 内存持续增长直到 OOM → 内存泄漏
# 2. 内存在某个时间点突然飙升 → 流量洪峰或大内存操作（如批量导出）
# 3. 内存一直接近 Limit 然后 OOM → Limit 设置过低

# Java 应用特别注意：JVM Heap 之外还有 Native Memory，
# Limit 要大于 -Xmx 至少 20-30%
```

**短期处置**：调高 Limit（但这是治标，要搞清楚根因）

**长期预防**：
- 设置合理的 Request 和 Limit（不要随手填 10GB）
- 用 VPA（Vertical Pod Autoscaler）的 Recommendation 模式，自动建议合理的资源值
- Java 应用用 `-XX:+UseContainerSupport`（JDK 11+）让 JVM 感知容器内存限制
- 内存泄漏：增加 heap dump 触发配置（`-XX:+HeapDumpOnOutOfMemoryError`），分析 dump 找泄漏根源

---

### 题3：etcd 数据一致性保障机制

**答题框架：Raft 协议核心 → 数据写入流程 → 一致性保证 → 性能 vs 一致性的取舍**

etcd 使用 **Raft 共识算法**保证分布式一致性。

**Raft 核心机制**：
- 集群有且只有一个 **Leader**，所有写入都通过 Leader
- Leader 选举：心跳超时后开始选举，获得超过半数（quorum）节点投票的候选者当选
- 日志复制：Leader 收到写请求 → 追加到自己的日志（uncommitted）→ 并行发给所有 Follower → 超过半数确认后 commit → 返回客户端成功 → 通知 Follower 提交

**数据写入保证**：

客户端写入 etcd 的成功响应，意味着数据已经被**超过半数的节点持久化**。即使 Leader 此后立即宕机，数据也不会丢失（新选出的 Leader 一定包含已 commit 的数据）。

**一致性模型**：
- etcd 默认提供**线性一致性（Linearizability）**读——读操作会去 Leader 确认，保证读到最新数据
- 对性能要求高的场景可以用**序列化读**（`--consistency=s`），允许从任意节点读，但可能读到稍旧的数据（适合 Watch 场景）

**K8s 运维关键点**：
- etcd 3 节点可容忍 1 节点故障，5 节点可容忍 2 节点故障
- etcd 磁盘 IO 是关键：一定用 SSD（NVMe 最好），不要和其他高 IO 服务共享磁盘
- 定期备份：`etcdctl snapshot save`，灾难恢复时用 snapshot restore

---

### 题4：Prometheus 的 TSDB 存储原理

**答题框架：数据模型 → 存储结构 → 写入流程 → 查询流程**

**数据模型**：每个时序由 label 集合唯一标识，value 是 (timestamp, float64) 的序列

**存储结构**：

Prometheus TSDB 按时间分块（Block），每个 Block 默认 2 小时：
- 最近 2 小时：写入内存的 **Head Block**（WAL 保证崩溃恢复）
- 2 小时后：持久化为磁盘上的不可变 Block
- 定期 compaction：合并小 Block 为大 Block，减少文件数，提高查询效率

**Block 内部结构**：
- `chunks/`：实际的时序数据，用 XOR 压缩（相邻值差异编码）
- `index`：倒排索引，label 名 → label 值 → series 列表
- `tombstones`：标记删除（TSDB 不立即删除，等 compaction 时清理）

**写入流程**：
Sample → 写 WAL（保证崩溃恢复）→ 写 Head Block 内存（快速）→ 2小时后 flush 到磁盘

**查询流程**：
PromQL → 通过 label index 找到匹配的 series → 从 chunks 读取时间范围内的数据 → 聚合计算

**性能瓶颈**：高 cardinality（label 值组合爆炸，如把 UserID 作为 label）会导致 index 过大，查询内存暴涨，是 Prometheus OOM 的最常见原因。

---

### 题5：TCP 三次握手与 K8s Service 的关系

**答题框架：三次握手过程 → K8s Service 怎么处理 → 常见问题**

**三次握手**：
1. Client → Server：SYN（我要连接你）
2. Server → Client：SYN-ACK（好的，我也要连你）
3. Client → Server：ACK（确认）

**K8s Service 的实现（kube-proxy iptables 模式）**：

Client 访问 Service IP（ClusterIP），实际上是虚拟 IP，不对应任何网卡。kube-proxy 通过 iptables DNAT 规则，在数据包到达节点时把目标地址替换为某个 Pod IP。

三次握手在 DNAT 之后进行，所以 Pod 看到的是客户端的真实连接请求。

**Session 持久性问题**：kube-proxy 默认是随机选 Pod，同一个 Client 的不同 TCP 连接可能打到不同 Pod。如果应用有 session（不推荐），需要用 `sessionAffinity: ClientIP`（基于 IP 的会话保持）。

**TIME_WAIT 和 K8s**：大量短连接场景（HTTP/1.0 或关闭 keepalive）会产生大量 TIME_WAIT。在 K8s 里，DNAT 重写了地址，TIME_WAIT 计数在每个节点上，但本质问题是连接复用不够，优先检查是否开启了 HTTP keepalive。

---

### 题6：大量 TIME_WAIT 如何处理

**答题框架：理解为什么有 TIME_WAIT → 判断是否真正有问题 → 对症处置**

**TIME_WAIT 存在的原因**：

TCP 主动关闭方（通常是客户端或处理完请求的服务端）会进入 TIME_WAIT 状态，等待 2MSL（约 60 秒）。目的是确保对端能收到最后的 ACK，以及让网络中残留的旧数据包消亡。

**是否真的是问题**：大量 TIME_WAIT 本身不是故障，只有以下情况才是问题：
- 本地端口耗尽（`ip_local_port_range` 默认 32768-60999，约 2.8 万个端口）
- 内存占用（每个 TIME_WAIT 约 300 bytes，一般不是瓶颈）

```bash
# 查看 TIME_WAIT 数量
ss -s | grep TIME-WAIT
# 或
netstat -an | grep TIME_WAIT | wc -l

# 查看端口使用情况
ss -s | grep estab
```

**处置方案**（按优先级）：

1. **开启 TCP keepalive + 长连接**：根本解决方案，减少连接建立和销毁频率
2. **调大本地端口范围**：
   ```bash
   sysctl -w net.ipv4.ip_local_port_range="1024 65535"
   ```
3. **开启 tcp_tw_reuse**（只对客户端有效，允许 TIME_WAIT 的端口被新连接复用）：
   ```bash
   sysctl -w net.ipv4.tcp_tw_reuse=1
   ```
4. 不要开启 `tcp_tw_recycle`，在 NAT 环境下会导致连接建立失败（已在 4.12 内核删除）

---

### 题7：容器 CPU Throttling 排查

**答题框架：什么是 Throttling → 如何发现 → 排查步骤 → 优化方向**

**CPU Throttling 的本质**：

Linux cgroups 的 CFS（Completely Fair Scheduler）带宽控制：设置 CPU Limit 后，内核会给容器分配 CPU 配额（period，默认 100ms）和在该 period 内能运行的时间（quota）。

设置 `limits.cpu: 1` 意味着每 100ms 最多运行 100ms。如果容器在 100ms 内用完了配额，就会被暂停（throttled），等到下一个 period。

**发现方式**：

```bash
# Prometheus 指标
rate(container_cpu_cfs_throttled_seconds_total[5m]) /
rate(container_cpu_cfs_periods_total[5m])
# 这个比值 > 25% 就需要关注
```

**排查步骤**：

1. 确认是否真的 Throttling（看上面的 Prometheus 指标）
2. 看 Throttling 的时间分布：是持续 Throttling 还是偶发？偶发很可能是 Java GC 或瞬时高 CPU 操作
3. 看应用的 CPU 使用模式：Java 等 JVM 语言在 GC 时会短时间 CPU 飙升，即使平均 CPU 不高也会频繁触发 Throttling
4. 优化方向：
   - 调高 CPU Limit（首选，代价是该节点其他 Pod 的可用 CPU 减少）
   - 拆分 Pod：把 CPU 密集型操作（如批处理）和主服务分离
   - Java 应用调优 GC 策略，减少 GC 时的 CPU 峰值
   - 使用 `cpu.shares` 而不是严格 Limit（即只设 Request 不设 Limit）——有争议，会影响调度

---

### 题8：K8s 网络故障排查方法论

**答题框架：分层分析 → 逐层验证**

K8s 网络问题可以按以下层次逐一排查，找到故障层：

**第一层：Pod 自身**
```bash
# Pod 是否 Running 且 Ready
kubectl get pod <pod> -o wide

# 进入 Pod 测试
kubectl exec -it <pod> -- curl http://localhost:8080/health
```

**第二层：同节点 Pod 间通信**
```bash
# 在同一 Node 上的另一个 Pod 里测试
kubectl exec -it <debug-pod> -- curl http://<pod-ip>:8080
```

**第三层：跨节点 Pod 间通信**
```bash
# 在不同 Node 的 Pod 里测试，排除网络插件（CNI）问题
kubectl exec -it <debug-pod-on-another-node> -- curl http://<pod-ip>:8080
```

**第四层：Service 访问**
```bash
# 通过 Service ClusterIP 访问
kubectl exec -it <pod> -- curl http://<service-clusterip>:<port>

# 通过 Service DNS 访问
kubectl exec -it <pod> -- curl http://<service-name>.<namespace>.svc.cluster.local

# 检查 Service Endpoints
kubectl get endpoints <service-name>
# 如果 Endpoints 为空，检查 selector 是否匹配 Pod label
```

**第五层：DNS 解析**
```bash
kubectl exec -it <pod> -- nslookup kubernetes.default.svc.cluster.local
kubectl exec -it <pod> -- cat /etc/resolv.conf
```

**第六层：出集群访问**
```bash
kubectl exec -it <pod> -- curl https://api.github.com
# 如果失败，可能是 NAT/出口 IP 问题，或 NetworkPolicy 限制
```

**NetworkPolicy 检查**：

```bash
# 列出 namespace 内所有 NetworkPolicy
kubectl get networkpolicy -n <namespace>

# 查看某个 Policy 详情
kubectl describe networkpolicy <policy-name>
```

---

### 题9：如何评估一个新技术是否值得引入

**答题框架：问题驱动 → 方案评估 → 风险评估 → 推进策略**

**先问"解决了什么问题"**：

新技术如果不是解决真实存在的痛点，只是"看起来很酷"，不要引入。明确：
- 现有方案的具体限制是什么？
- 这个新技术解决了哪些限制？
- 代价是什么？

**评估维度（STAMP 框架）**：

- **S（Safety）稳定性**：社区活跃度、版本成熟度、生产案例（谁在 prod 用？）、已知 Bug 和 CVE 情况
- **T（Team）团队适应性**：学习曲线、现有团队技能匹配度、出问题谁来 On-call
- **A（Architecture）架构兼容性**：和现有技术栈的集成复杂度、数据格式兼容性、依赖冲突
- **M（Migration）迁移成本**：从现有方案迁移有多难？能否灰度迁移？
- **P（Performance）性能**：Benchmark 数据，在自己场景下的实测结果

**推进策略**：

不要一上来就全量替换。正确路径：
1. 沙箱环境小范围测试，产出测评报告
2. 选一个非核心的服务先上（降低风险）
3. 跑 2-3 个月，收集数据，观察稳定性
4. 写内部决策文档（ADR，Architecture Decision Record），记录为什么选/不选
5. 团队 Review，决定是否推广

---

### 题10：讲一次你主导的生产事故复盘

**答题框架：STAR 法则 → 5W 根因 → 复盘结论**

这道题考的是**你从故障中学习和沉淀的能力**，不是考你有没有出过故障。

**STAR 法则**：
- **Situation**：什么时候、什么系统、影响面有多大（多少用户、多长时间、什么业务）
- **Task**：你在里面的角色和责任
- **Action**：发现 → 判断 → 处置的全过程（时间线）
- **Result**：最终恢复情况、后续改进项的落地情况

**复盘结构**（五问法）**：

1. **What happened**：故障现象、影响范围、持续时长
2. **Why（Timeline）**：逐步还原：首次告警 → 定位 → 操作 → 恢复。每个步骤卡在哪里？为什么
3. **Root Cause**：最终根因是什么？（技术原因、流程原因、管理原因）
4. **Contributing Factors**：是什么让故障变得更严重或持续更长？（告警太晚、监控盲区、Runbook 缺失）
5. **Action Items**：针对根因和 Contributing Factor 的具体改进措施，指定 Owner 和 Deadline

**回答要点**：
- 不要把复盘变成甩锅或自我辩护
- 强调流程和系统的改进，而不只是"下次小心"
- 展示你的 MTTR（平均恢复时间）有没有通过改进缩短
- 改进项要具体可落地，比如"加了这个告警规则"、"写了这个 Runbook"、"加了熔断机制"

高级工程师和初级工程师的区别在这道题上很清晰：初级工程师把故障当耻辱不愿意说，高级工程师把故障当最宝贵的学习素材，能把一次事故讲出系统性改进的故事。
