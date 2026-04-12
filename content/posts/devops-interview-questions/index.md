---
title: "DevOps/运维工程师面试题精选：K8s、Linux、网络高频考点"
date: 2025-12-07T13:07:00+08:00
draft: false
tags: ["面试", "Kubernetes", "Linux", "网络", "运维", "DevOps"]
categories: ["职业发展"]
description: "整理 Kubernetes、Linux、网络方向的高频面试题，每题给出简洁答案，并点出面试官真正想考察的核心点。"
summary: "基于真实面试经验整理的运维/DevOps 面试题，覆盖 K8s 调度、故障排查、Linux 内核、网络协议等方向，附「面试官真正想考的点」，帮你把答案说到位。"
toc: true
math: false
diagram: false
keywords: ["Kubernetes面试题", "运维面试", "DevOps面试", "Linux面试题", "K8s面试"]
params:
  reading_time: true
---

## 前言

这篇文章整理自我参加和组织面试的经历。运维/DevOps 方向的面试越来越偏向「原理 + 排查思路」，光会敲命令已经不够了，面试官想看的是你在系统出问题时的分析框架。

每道题我会给出**简洁答案**和**面试官真正想考的点**——因为面试回答要有重点，不是把知识点全背出来，而是要命中考察维度。

---

## Kubernetes 高频题

### 1. Pod 的调度流程是什么？

**答**：

1. 用户提交 Pod Spec，API Server 写入 etcd，状态为 Pending
2. kube-scheduler 监听到未绑定的 Pod，执行两个阶段：
   - **过滤（Filter）**：排除不满足条件的节点（资源不足、污点不容忍、亲和性不满足等）
   - **打分（Score）**：对剩余节点按多维度打分（资源利用率、亲和性优先级等）
3. Scheduler 选出得分最高的节点，通过 Binding 写回 API Server
4. 对应节点的 kubelet 监听到绑定事件，拉取镜像、创建容器

**面试官想考的点**：是否了解 Filter 和 Score 两阶段，以及为什么调度器是独立组件（可替换、可扩展）。进阶追问：自定义调度器怎么做？

---

### 2. Pod 状态 CrashLoopBackOff 怎么排查？

**答**：

CrashLoopBackOff 表示容器反复启动、崩溃，K8s 在指数退避后不断重试。排查步骤：

```bash
# 第一步：看事件和状态
kubectl describe pod <pod-name> -n <namespace>

# 第二步：看容器日志（包括上一次崩溃的日志）
kubectl logs <pod-name> -n <namespace> --previous

# 第三步：如果日志不够，临时覆盖 entrypoint
kubectl debug -it <pod-name> --image=busybox --target=<container>
```

常见原因：
- 应用启动报错（配置错误、连不上数据库）
- OOM 被 kill（`kubectl describe` 里看 `OOMKilled`）
- 健康检查失败导致反复重启
- 镜像 entrypoint 脚本有 bug

**面试官想考的点**：排查思路是否有序，是否知道 `--previous` 这个参数（很多人不知道），是否区分了 CrashLoopBackOff 和 OOMKilled 两种情况。

---

### 3. OOMKilled 怎么处理？

**答**：

OOMKilled 表示容器内存超过 `resources.limits.memory`，被内核 OOM Killer 杀掉。

```bash
# 确认是 OOM
kubectl describe pod <pod-name> | grep -A5 "Last State"
# 会看到 Reason: OOMKilled, Exit Code: 137
```

处理方向：
1. **短期**：调大 memory limit
2. **中期**：分析内存泄漏，用 `memory_profiler`（Python）或 pprof（Go）
3. **系统层**：合理设置 `requests` 和 `limits`，避免 limits 设得过小

注意区分 `requests`（调度依据）和 `limits`（运行时上限），不要把两者设成一样大（会导致节点负载预测不准）。

**面试官想考的点**：是否知道 `requests` vs `limits` 的语义差别，Exit Code 137 的含义（128 + 9，SIGKILL）。

---

### 4. Service 的三种类型区别？

**答**：

| 类型 | 访问范围 | 实现原理 |
|------|----------|----------|
| ClusterIP | 集群内部 | kube-proxy 写 iptables/ipvs 规则，VIP 只在集群内路由 |
| NodePort | 集群外，通过节点 IP | 在每个节点开固定端口（30000-32767），流量转发到 ClusterIP |
| LoadBalancer | 集群外，通过云 LB | 在 NodePort 基础上，调用云厂商 API 创建外部负载均衡器 |

还有 ExternalName（CNAME 映射）和 Headless Service（无 ClusterIP，直接返回 Pod IP）。

**面试官想考的点**：NodePort 和 LoadBalancer 的关系（LoadBalancer 包含 NodePort），Headless Service 的使用场景（StatefulSet、服务发现）。

---

### 5. Deployment 滚动更新原理和回滚命令？

**答**：

滚动更新由 `spec.strategy.rollingUpdate` 控制：

```yaml
strategy:
  type: RollingUpdate
  rollingUpdate:
    maxSurge: 1        # 最多超出期望副本数 1 个
    maxUnavailable: 0  # 更新过程中不允许不可用
```

更新时，ReplicaSet 会先创建新版本的 Pod，等新 Pod Ready 后，再缩减旧 Pod，交替进行直到全部替换。每次 Deployment 变更都会生成一个新的 ReplicaSet，旧 ReplicaSet 被保留（数量由 `revisionHistoryLimit` 控制，默认 10）。

```bash
# 查看发布历史
kubectl rollout history deployment/myapp -n prod

# 查看某个版本的详情
kubectl rollout history deployment/myapp --revision=3 -n prod

# 回滚到上一版本
kubectl rollout undo deployment/myapp -n prod

# 回滚到指定版本
kubectl rollout undo deployment/myapp --to-revision=2 -n prod

# 查看滚动更新状态
kubectl rollout status deployment/myapp -n prod
```

**面试官想考的点**：ReplicaSet 和 Deployment 的关系，`maxSurge` / `maxUnavailable` 的含义，知道历史版本存在 ReplicaSet 里而不是 Deployment 里。

---

### 6. RBAC 工作机制？

**答**：

K8s RBAC 有四个核心对象：

- **Role / ClusterRole**：权限规则集合（对哪些资源做哪些操作）
- **RoleBinding / ClusterRoleBinding**：把 Role 绑定到用户/ServiceAccount

```yaml
# 创建 Role：允许读 Pods
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  namespace: production
  name: pod-reader
rules:
- apiGroups: [""]
  resources: ["pods"]
  verbs: ["get", "list", "watch"]

---
# 把 Role 绑定到 ServiceAccount
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: read-pods
  namespace: production
subjects:
- kind: ServiceAccount
  name: monitoring-sa
  namespace: production
roleRef:
  kind: Role
  name: pod-reader
  apiGroup: rbac.authorization.k8s.io
```

`Role` 作用于单个 Namespace，`ClusterRole` 作用于全集群（适合跨 Namespace 或操作集群级资源如 Node）。

**面试官想考的点**：Role 和 ClusterRole 的区别，最小权限原则的理解，ServiceAccount 是 Pod 的身份凭证这一概念。

---

### 7. HPA 扩缩容原理？

**答**：

HPA（Horizontal Pod Autoscaler）通过 Metrics Server 定期拉取指标，根据公式计算期望副本数：

```
期望副本数 = ceil(当前副本数 × (当前指标值 / 目标指标值))
```

比如当前 2 个 Pod，平均 CPU 利用率 80%，目标 50%：
```
期望副本数 = ceil(2 × 80/50) = ceil(3.2) = 4
```

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
spec:
  minReplicas: 2
  maxReplicas: 20
  metrics:
  - type: Resource
    resource:
      name: cpu
      target:
        type: Utilization
        averageUtilization: 50
  behavior:
    scaleDown:
      stabilizationWindowSeconds: 300  # 缩容冷却 5 分钟，防抖
```

HPA 支持 CPU、内存（需要设 requests）、自定义指标（Prometheus Adapter）。

**面试官想考的点**：必须设置 `resources.requests` 才能让 HPA 工作（因为利用率 = 实际用量 / requests），以及 `stabilizationWindow` 防止频繁扩缩的设计。

---

### 8. 什么情况下 Pod 处于 Pending 状态？

**答**：

Pending 说明 Pod 已被 API Server 接收，但还没有被调度到节点，或者已调度但容器还没起来。常见原因：

```bash
kubectl describe pod <pod-name> | grep -A20 Events
```

- **资源不足**：集群没有满足 `requests` 的节点，Events 里会看到 `Insufficient cpu/memory`
- **节点选择器/亲和性不满足**：`nodeSelector` 或 `nodeAffinity` 没有匹配的节点
- **污点未容忍**：节点有 taint，Pod 没有对应 toleration
- **PVC 未绑定**：依赖的 PersistentVolumeClaim 处于 Pending 状态
- **镜像拉取中**：ImagePullBackOff 之前会短暂 Pending

**面试官想考的点**：是否有系统的排查思路，知道 `kubectl describe` 的 Events 是第一手信息源。

---

### 9. 如何不停机更新 ConfigMap？

**答**：

普通 ConfigMap 变更后，已运行的 Pod 不会自动感知（环境变量方式完全不会更新，Volume 挂载方式会在 kubelet 同步周期后更新，默认约 1 分钟）。

真正零停机更新的方式：

1. **Volume 挂载 + 应用热加载**：应用监听文件变化（inotify），ConfigMap 更新后应用自动重载配置，无需重启 Pod
2. **滚动重启**：`kubectl rollout restart deployment/myapp`，配合滚动更新策略实现不停机
3. **不可变 ConfigMap + 版本化命名**：每次配置变更创建新 ConfigMap（如 `app-config-v2`），更新 Deployment 引用，触发滚动更新

```bash
# 触发滚动重启（不改镜像版本的情况下重新部署）
kubectl rollout restart deployment/myapp -n production
```

**面试官想考的点**：是否知道 Volume 挂载和环境变量方式对 ConfigMap 更新的不同行为，以及不可变 ConfigMap 的最佳实践。

---

### 10. K8s 网络模型的核心规则？

**答**：

K8s 网络模型有三条基本规则：

1. 每个 Pod 有独立 IP，Pod 内所有容器共享网络命名空间
2. 所有 Pod 之间可以直接通信，不需要 NAT
3. Node 上的进程可以直接和 Pod 通信

这些规则由 CNI 插件实现（Flannel、Calico、Cilium、Terway 等）。不同插件实现方式不同：
- **Flannel**：VXLAN 隧道封包，简单但有额外开销
- **Calico**：BGP 路由，性能更好，支持网络策略
- **Cilium**：基于 eBPF，在内核层处理网络，性能最优，可替代 kube-proxy

**面试官想考的点**：三条规则能不能背出来，CNI 是插件化的（可替换），以及是否了解 eBPF 方向的趋势。

---

## Linux 高频题

### 11. 进程和线程的区别？

**答**：

进程是资源分配的基本单位，线程是 CPU 调度的基本单位。同一进程的线程共享地址空间、文件描述符、信号处理器，但每个线程有独立的栈和寄存器状态。

Linux 里线程用 `clone()` 实现（共享地址空间的轻量级进程），`fork()` 创建进程（完整复制），`exec()` 替换当前进程的镜像。

`fork()` 使用 Copy-on-Write，实际上父子进程共享物理内存页，只有写操作触发时才复制，所以 fork 的成本比想象中低。

**面试官想考的点**：COW 是高频追问点，线程共享什么/不共享什么要答清楚，以及 Go goroutine vs 系统线程的区别（有时会追问）。

---

### 12. TCP 三次握手/四次挥手？

**答**：

三次握手建立连接：
```
客户端 → SYN(seq=x)          → 服务端
客户端 ← SYN+ACK(seq=y,ack=x+1) ← 服务端
客户端 → ACK(ack=y+1)         → 服务端
```

四次挥手断开连接：
```
主动方 → FIN → 被动方
主动方 ← ACK ← 被动方
主动方 ← FIN ← 被动方  （被动方数据发完后）
主动方 → ACK → 被动方
主动方进入 TIME_WAIT，等待 2*MSL
```

TIME_WAIT 的目的：确保最后一个 ACK 到达（网络丢包情况下被动方会重发 FIN），以及让网络中残留的旧数据包消散。

**面试官想考的点**：为什么握手是三次不是两次（防止历史连接干扰），TIME_WAIT 的存在意义，以及实际问题：大量 TIME_WAIT 如何处理（`net.ipv4.tcp_tw_reuse`）。

---

### 13. 系统负载高如何排查？

**答**：

```bash
# 第一步：看负载和 CPU
top -b -n 1
# 看 load average（1/5/15 分钟），us/sy/wa/id 的比例

# 第二步：看是 CPU 密集还是 IO 等待
# wa（iowait）高 → 磁盘/网络 IO 问题
# sy（system）高 → 内核调用频繁，可能是锁竞争或系统调用
# us（user）高 → 应用代码 CPU 密集

# 第三步：定位具体进程
ps aux --sort=-%cpu | head -20
ps aux --sort=-%mem | head -20

# 第四步：看进程在干什么
strace -p <pid> -e trace=all -c   # 统计系统调用
perf top -p <pid>                  # CPU 热点函数（需要 debug symbols）
cat /proc/<pid>/wchan              # 进程在等待什么

# 第五步：IO 诊断
iostat -x 1 5
iotop -ao   # 看哪个进程在做 IO
```

**面试官想考的点**：iowait 高和 CPU 高是两个不同方向，不要混为一谈。是否知道 `strace`、`perf` 这类进阶工具。

---

### 14. 文件描述符限制排查？

**答**：

常见症状：`Too many open files`，服务无法建立新连接。

```bash
# 查看系统级限制
cat /proc/sys/fs/file-max
cat /proc/sys/fs/file-nr   # 已分配 / 已用 / 最大

# 查看某进程的 fd 使用情况
ls -l /proc/<pid>/fd | wc -l
cat /proc/<pid>/limits | grep "open files"

# 查看进程打开的是什么文件
lsof -p <pid> | head -50
lsof -p <pid> | awk '{print $5}' | sort | uniq -c | sort -rn

# 修改进程级限制（/etc/security/limits.conf）
# * soft nofile 65536
# * hard nofile 65536

# 运行时修改（对已运行进程）
prlimit --nofile=65536 --pid=<pid>
```

容器场景下，fd 限制来自宿主机的 `ulimit`，需要在 pod spec 里设置：

```yaml
securityContext:
  sysctls:
  - name: fs.file-max
    value: "65536"
```

**面试官想考的点**：区分系统级限制和进程级限制，知道如何在不重启进程的情况下修改，以及容器场景下的处理方式。

---

### 15. iptables 的表和链？

**答**：

iptables 有 5 张表（按优先级）：raw、mangle、nat、filter、security。日常运维最常用的是 **filter**（包过滤）和 **nat**（地址转换）。

每张表有多个链，filter 表的核心链：
- `INPUT`：进入本机的包
- `OUTPUT`：本机发出的包
- `FORWARD`：经过本机转发的包

nat 表的核心链：
- `PREROUTING`：进入路由决策之前（做 DNAT，改目标 IP）
- `POSTROUTING`：路由决策之后（做 SNAT/MASQUERADE，改源 IP）

K8s 的 kube-proxy 大量使用 iptables/ipvs 规则，Service 的 ClusterIP 流量转发本质上就是 DNAT。

```bash
# 查看 filter 表规则（含行号）
iptables -L -n -v --line-numbers

# 查看 nat 表
iptables -t nat -L -n -v

# 查看 K8s 相关规则
iptables -t nat -L KUBE-SERVICES -n -v
```

**面试官想考的点**：表和链的关系，DNAT/SNAT 的区别，以及 K8s Service 实现和 iptables 的关联。

---

### 16. 如何找出占用端口的进程？

**答**：

```bash
# 方法一：ss（比 netstat 快）
ss -tlnp | grep :8080

# 方法二：lsof
lsof -i :8080

# 方法三：/proc 文件系统
cat /proc/net/tcp   # 十六进制端口号

# 输出示例：
# ss -tlnp | grep :8080
# LISTEN  0  128  0.0.0.0:8080  0.0.0.0:*  users:(("python3",pid=1234,fd=5))
```

**面试官想考的点**：知道 `ss` 比 `netstat` 更现代（netstat 已不再维护），能从输出里找到 pid 和 fd 信息。

---

### 17. grep/awk/sed 实战题

**答**：

面试里经常出现「给你一段日志，提取某列/统计某值」的实操题：

```bash
# 统计 Nginx 日志里各 HTTP 状态码的数量
awk '{print $9}' access.log | sort | uniq -c | sort -rn

# 提取最近 1000 行日志里的 ERROR 并显示前后 3 行
tail -n 1000 app.log | grep -A3 -B3 "ERROR"

# 替换配置文件里的地址（原地修改）
sed -i 's/old.host.com/new.host.com/g' config.yaml

# 统计某个 IP 的请求量
awk '{print $1}' access.log | sort | uniq -c | sort -rn | head -20

# 提取 JSON 日志里的某字段
cat app.log | python3 -c "import sys,json; [print(json.loads(l)['level']) for l in sys.stdin]"
# 或者用 jq
cat app.log | jq -r '.level' | sort | uniq -c
```

---

### 18. 内存使用分析命令？

**答**：

```bash
# 系统内存概览
free -h
cat /proc/meminfo

# 按进程看内存（RSS = 实际占用物理内存）
ps aux --sort=-%rss | head -20

# 查看某进程详细内存分布
cat /proc/<pid>/status | grep -i vm
pmap -x <pid> | tail -1   # 汇总

# 看是否有内存泄漏趋势
watch -n 5 'ps -p <pid> -o pid,rss,vsz'
```

注意区分 VSZ（虚拟内存，包括未分配的）和 RSS（实际占用物理内存），OOM 触发看的是 RSS + Swap。

---

## 网络/存储

### 19. CNI 工作原理？

**答**：

CNI（Container Network Interface）是一个规范，kubelet 在创建 Pod 时调用 CNI 插件完成网络配置。流程：

1. kubelet 创建 Pod 的 network namespace（`/var/run/netns/`）
2. 调用 CNI 插件二进制（`/opt/cni/bin/` 下），传入网络配置（`/etc/cni/net.d/`）
3. 插件在 namespace 里创建 veth pair：一端放入 Pod namespace（重命名为 eth0），另一端放在宿主机上
4. 分配 IP，配置路由
5. 根据插件类型决定节点间的通信方式（VXLAN/BGP/eBPF）

**面试官想考的点**：veth pair 是基础，理解 Pod 网络包如何从 Pod 里出来、经过宿主机、到达另一个节点。

---

### 20. etcd 为什么用 Raft 共识算法？

**答**：

etcd 是 K8s 的分布式存储，存储所有集群状态，需要强一致性（任何时刻读到的数据都是最新提交的值）。

Raft 提供：
- **Leader 选举**：集群里只有一个 Leader 处理写请求，保证顺序性
- **日志复制**：Leader 把操作日志同步到多数节点后才返回成功（quorum write）
- **故障恢复**：Leader 宕机后自动选举新 Leader

与 Paxos 相比，Raft 更易理解和实现（这也是 etcd 选择 Raft 的原因之一）。

生产配置：etcd 需要奇数节点（3 或 5），允许 `(n-1)/2` 个节点故障。3 节点 etcd 允许 1 个节点挂掉，5 节点允许 2 个。

**面试官想考的点**：quorum（多数派）是核心概念，以及为什么不能用偶数节点（脑裂风险）。

---

### 21. PV / PVC / StorageClass 的关系？

**答**：

三层抽象：

- **PersistentVolume（PV）**：集群管理员创建的存储资源（或动态创建），描述实际存储（NFS 路径、云磁盘 ID 等）
- **PersistentVolumeClaim（PVC）**：Pod 对存储的「申请」，声明需要多大、什么访问模式
- **StorageClass**：存储的「类别」，定义动态创建 PV 的模板和参数

工作流程：
1. 用户创建 PVC，声明需要 10Gi RWO 存储
2. 如果有匹配的 PV（静态分配），直接绑定
3. 如果 PVC 指定了 StorageClass，Controller 调用 provisioner 动态创建 PV 并绑定
4. Pod 引用 PVC，挂载到容器路径

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: data-pvc
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: gp3
  resources:
    requests:
      storage: 20Gi
```

**面试官想考的点**：动态 provisioning 的流程，AccessMode（RWO/ROX/RWX）的含义，以及 PV 的回收策略（Retain/Delete）。

---

## 写在最后

面试里有个规律：背得出答案只能拿到 60 分，能说清楚「为什么这样设计」才能拿到 90 分。K8s 的很多设计决策（为什么 HPA 要设 requests、为什么 etcd 用 Raft、为什么 CNI 是插件化的）背后都有工程权衡，能说出这个层次的理解，是高级工程师和初级工程师的分水岭。

另一个建议：面试官问「你遇到过……吗」的时候，有真实踩坑经历的候选人远比照本宣科的候选人有说服力。多在生产上踩坑，把踩过的坑记下来，才是最好的面试准备。
