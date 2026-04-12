---
title: "故障排查实录：Terway CRD IPAM IP 泄漏导致 Pod 无法调度"
date: 2025-12-08T20:00:00+08:00
draft: false
tags: ["故障排查", "Kubernetes", "Terway", "网络", "阿里云", "IPAM"]
categories: ["Kubernetes"]
description: "记录一次阿里云 ACK 集群中因 DiskPressure 引发 Pod 驱逐、进而导致 Terway IP 未回收、VPC 弹性网卡 IP 耗尽的完整排查过程"
summary: "一次真实的连锁故障：节点磁盘告警 → Pod 被驱逐 → Terway IPAM IP 未正常回收 → 节点 ENI IP 耗尽 → 新 Pod 无法调度。排查链路、根因分析与修复方案完整记录。"
toc: true
math: false
diagram: false
series: ["SRE 实战手册"]
keywords: ["terway", "ipam", "kubernetes", "阿里云", "ip泄漏", "故障排查"]
params:
  reading_time: true
---

# 故障排查实录：Terway CRD IPAM IP 泄漏导致 Pod 无法调度

这是一次让我花了将近三个小时才搞清楚根因的故障。表面上看是"Pod 调度失败"，实际上是一条从磁盘告警出发，经过 kubelet 驱逐，最终触达网络层的连锁反应。完整记录下来，希望后来人碰到类似现象时能少走弯路。

---

## 一、告警触发：新 Pod 全部卡在 Pending

事情发生在一个工作日的下午。监控告警显示某个 Deployment 的滚动更新卡住了，新 Pod 长时间处于 Pending 状态。

### 第一反应：看 Pod 事件

```bash
kubectl describe pod my-app-7d9f8b-xxxxx -n production
```

Events 里看到一条很陌生的报错：

```
Warning  FailedScheduling  3m   default-scheduler
  0/6 nodes are available: 6 Insufficient aliyun/vpc-eni-ip.
```

`Insufficient aliyun/vpc-eni-ip`——这不是常见的 CPU/内存不足，而是 ENI IP 资源耗尽。这个报错我之前没遇到过，一时有点懵。

### 确认影响范围

```bash
kubectl get pods -n production | grep Pending
```

输出了十几行，不止一个服务的 Pod 在 Pending。再看节点：

```bash
kubectl get nodes
```

```
NAME                    STATUS   ROLES    AGE   VERSION
cn-hangzhou.x.x.x.x    Ready    <none>   30d   v1.28.3-aliyun.1
cn-hangzhou.x.x.x.x    Ready    <none>   30d   v1.28.3-aliyun.1
cn-hangzhou.x.x.x.x    Ready    <none>   30d   v1.28.3-aliyun.1
...
```

节点状态都显示 Ready，没有明显异常。这就更奇怪了——节点都正常，但 Pod 就是调度不上去。

---

## 二、初步排查：从时间线找线索

遇到这种"现象和直觉不符"的情况，我的习惯是先把事件时间线拉出来，看看故障前后发生了什么。

### 拉取全局事件

```bash
kubectl get events -A --sort-by='.metadata.creationTimestamp' | tail -60
```

关键片段（时间已脱敏）：

```
production   Warning   Evicted       Pod/my-app-old-aaaa    kubelet   The node was low on resource: ephemeral-storage. Threshold quantity: 10%, available: 7%.
production   Warning   Evicted       Pod/worker-old-bbbb    kubelet   The node was low on resource: ephemeral-storage. Threshold quantity: 10%, available: 6%.
production   Warning   Evicted       Pod/another-svc-cccc   kubelet   The node was low on resource: ephemeral-storage. Threshold quantity: 10%, available: 5%.
kube-system  Warning   NodeHasDiskPressure  Node/cn-hangzhou.x.x.x.x  ...
kube-system  Warning   NodeHasDiskPressure  Node/cn-hangzhou.x.x.x.x  ...
```

**发现了关键线索**：在新 Pod Pending 之前，有大量 `Evicted` 事件，原因是 `ephemeral-storage` 不足——即磁盘压力。

### 确认 DiskPressure

```bash
kubectl describe node cn-hangzhou.x.x.x.x | grep -A 10 Conditions
```

```
Conditions:
  Type                 Status  ...  Reason                       Message
  ----                 ------  ...  ------                       -------
  MemoryPressure       False   ...  KubeletHasSufficientMemory   kubelet has sufficient memory
  DiskPressure         True    ...  KubeletHasDiskPressure       kubelet has disk pressure
  PIDPressure          False   ...  KubeletHasSufficientPID      kubelet has sufficient PID
  Ready                True    ...  KubeletReady                 kubelet is posting ready status
```

多个节点都有 `DiskPressure: True`。

逻辑上已经能串起来了：磁盘满 → DiskPressure → kubelet 触发驱逐 → 批量 Pod 被强制删除。但问题是，Pod 被驱逐之后，系统会重新调度新 Pod，为什么反而调度不上去了？

这说明 Pod 驱逐之后还有后续影响，问题出在网络层。

---

## 三、深入排查：Terway IPAM 层的 IP 泄漏

### Terway 是什么

阿里云 ACK 集群默认使用 Terway 作为网络插件。它的 IPAM（IP 地址管理）工作原理如下：

- 每个节点挂载一个或多个弹性网卡（ENI，Elastic Network Interface）
- 每张 ENI 可以挂载多个辅助私网 IP（Secondary IP）
- Terway 将这些辅助 IP 分配给 Pod，实现 Pod 直接使用 VPC IP 地址
- 每个 Pod 占用一个辅助 IP，Pod 删除后，对应的 IP 应该被回收到可用池

当 Terway 使用 CRD 模式（`terway-eniip` 模式）时，IP 分配和回收状态会记录在集群内的 CRD 对象中。

### 查看 ENI 资源状态

Terway CRD 模式下，可以通过以下命令查看每个节点的 ENI 分配情况：

```bash
kubectl get nodeeni -A
```

```
NAME                    AVAILABLE   TOTAL   STATUS
cn-hangzhou.x.x.x.x    0           14      Ready
cn-hangzhou.x.x.x.x    0           14      Ready
cn-hangzhou.x.x.x.x    2           14      Ready
```

前两个节点：`AVAILABLE=0`，也就是节点上的 ENI 辅助 IP 全部已分配，没有剩余可用 IP 给新 Pod 使用。

但此时实际运行的 Pod 数量远不到 14 个：

```bash
kubectl get pods -A -o wide | grep cn-hangzhou.x.x.x.x | grep Running | wc -l
# 输出：6
```

6 个 Running Pod，但 14 个 IP 全部"已分配"——这就是 IP 泄漏：有 IP 处于"已分配"状态，但实际上没有对应的 Pod 在使用它。

### 进一步确认泄漏

查看具体的 nodeeni 对象：

```bash
kubectl get nodeeni cn-hangzhou.x.x.x.x -o yaml
```

在 `status.enis` 下可以看到每张 ENI 的 IP 分配情况，其中有些 `podInfo` 字段指向了已经不存在的 Pod（被驱逐的那些）：

```yaml
status:
  enis:
    - id: eni-xxxxxx
      assignedPrivateIPs:
        - ip: 192.168.1.100
          podInfo:
            name: my-app-old-aaaa      # 这个 Pod 已经被驱逐了
            namespace: production
            podUID: abcd-1234-...
        - ip: 192.168.1.101
          podInfo:
            name: worker-old-bbbb      # 这个也不存在了
            namespace: production
            podUID: efgh-5678-...
```

Pod 已经不在了，但 Terway 的 CRD 状态没有同步清理，IP 依然显示"已分配"。

### 查看 terway-daemon 日志

```bash
kubectl logs -n kube-system -l app=terway-daemon --tail=200 | grep -i "error\|recycle\|release\|evict"
```

日志里有大量类似的报错：

```
ERR  failed to release IP for pod production/my-app-old-aaaa: pod not found, skip cleanup
ERR  failed to recycle ENI IP 192.168.1.100: resource version conflict, retrying...
WARN gc: pod production/worker-old-bbbb already deleted, but IP 192.168.1.101 still allocated, will retry
```

找到了：`pod already deleted, but IP still allocated`。terway-daemon 的 GC 逻辑没有及时处理被强制驱逐的 Pod 所占用的 IP。

---

## 四、根因确认

现在整个链条已经完全清晰了：

```
1. 节点磁盘使用率超过阈值（> 90%）
         ↓
2. kubelet 检测到 DiskPressure，触发 Pod 驱逐
   驱逐是强制删除，不走正常的 Graceful Termination 流程
         ↓
3. Terway 的 preStop / Pod 删除钩子在极端情况下未能正常执行
   或者 terway-daemon 处理驱逐事件时遇到竞态条件
         ↓
4. Pod 被删除，但对应的 ENI 辅助 IP 未从 Terway CRD 状态中回收
   IP 持续标记为"已分配"
         ↓
5. 节点所有 ENI IP 耗尽，新 Pod 调度时找不到可用 IP
   调度器报：Insufficient aliyun/vpc-eni-ip
         ↓
6. 所有新 Pod 卡在 Pending，业务不可用
```

这个 bug 的触发条件比较苛刻：必须同时满足"Terway CRD 模式"+"Pod 被驱逐（非正常删除）"，日常很难遇到，一旦遇到现象又比较迷惑。

---

## 五、修复方案

### 短期修复一：手动释放泄漏的 IP

对于已经泄漏的 IP，需要调用 AWS/阿里云 API 手动从 ENI 上解绑。对应的阿里云 ECS API 是 `UnassignPrivateIpAddresses`。

先查出需要回收的 IP 列表（从 nodeeni 对象中提取没有对应 Pod 的 IP）：

```bash
# 获取所有节点上"泄漏" IP 的清单
kubectl get nodeeni -A -o json | jq '
  .items[] |
  .metadata.name as $node |
  .status.enis[]? |
  .id as $eni |
  .assignedPrivateIPs[]? |
  select(.podInfo != null) |
  {node: $node, eni: $eni, ip: .ip, pod: .podInfo.name}
'
```

然后对照实际运行的 Pod 过滤出孤儿 IP，调用阿里云 CLI 释放：

```bash
# 阿里云 CLI 示例（需要提前配置 AK/SK 或 RAM Role）
aliyun ecs UnassignPrivateIpAddresses \
  --RegionId cn-hangzhou \
  --NetworkInterfaceId eni-xxxxxxxxxxxxxx \
  --PrivateIpAddress.1 192.168.1.100 \
  --PrivateIpAddress.2 192.168.1.101
```

释放之后，terway-daemon 会重新同步 CRD 状态，可用 IP 数量恢复，新 Pod 很快就能调度成功。

对于使用 AWS + Terway（自建）场景，对应的 API 是 `UnassignPrivateIpAddresses`（EC2 API），格式类似：

```bash
aws ec2 unassign-private-ip-addresses \
  --network-interface-id eni-xxxxxxxxxxxxxxxxx \
  --private-ip-addresses 192.168.1.100 192.168.1.101
```

### 短期修复二：清理节点磁盘

解决 DiskPressure，防止进一步驱逐：

```bash
# 登录节点（或通过 kubectl exec 进入特权容器）
# 查找磁盘占用大户
du -sh /var/log/pods/* 2>/dev/null | sort -rh | head -20
du -sh /var/lib/docker/containers/* 2>/dev/null | sort -rh | head -10

# 清理已停止的容器
docker container prune -f

# 清理未使用的镜像（注意：运行中的 Pod 镜像不会被删除）
docker image prune -a -f

# 或者使用 crictl（containerd）
crictl rmi --prune
```

磁盘清理后，DiskPressure condition 通常几分钟内会自动消除，kubelet 恢复正常调度。

### 长期修复一：调低磁盘告警阈值，提前干预

默认 kubelet 的 eviction 阈值：

```yaml
# kubelet 配置
evictionHard:
  nodefs.available: "10%"       # 磁盘可用不足 10% 开始驱逐
  nodefs.inodesFree: "5%"
```

建议在达到 80% 时就触发 Prometheus 告警，给运维人员足够的时间清理磁盘，避免走到 kubelet 强制驱逐这一步：

```yaml
# Prometheus 告警规则
- alert: NodeDiskUsageHigh
  expr: |
    (1 - (node_filesystem_avail_bytes{mountpoint="/"} /
          node_filesystem_size_bytes{mountpoint="/"})) * 100 > 80
  for: 5m
  labels:
    severity: warning
  annotations:
    summary: "节点磁盘使用率超过 80%，请及时清理"
    description: "节点 {{ $labels.instance }} 磁盘使用率 {{ $value | printf \"%.1f\" }}%"
```

同时建议配置 Fluentd/Filebeat 的日志轮转，防止 Pod 日志无限增长把磁盘撑满。

### 长期修复二：升级 Terway 版本

这个 IP 回收问题在较新版本的 Terway 中已经有改进，GC 逻辑更加健壮，能正确处理 evicted Pod 的 IP 回收。

```bash
# 查看当前 Terway 版本
kubectl get daemonset terway -n kube-system -o jsonpath='{.spec.template.spec.containers[0].image}'

# 通过 ACK 控制台升级（建议走控制台，避免手动改 DaemonSet）
# 路径：容器服务控制台 → 集群 → 运维管理 → 组件管理 → 更新 terway
```

### 长期修复三：ENI IP 使用率监控

这次故障的一个明显教训是：**ENI IP 使用率没有监控**。我们有 CPU、内存、磁盘的告警，但完全没有覆盖 Terway 层面的 IP 资源。

Terway 暴露了 Prometheus metrics，可以抓取：

```yaml
# 告警规则：节点 ENI 可用 IP 不足 3 个时告警
- alert: TerwayENIIPLow
  expr: terway_node_available_ip < 3
  for: 2m
  labels:
    severity: warning
  annotations:
    summary: "节点 ENI 可用 IP 不足"
    description: "节点 {{ $labels.node }} 当前可用 ENI IP 仅剩 {{ $value }} 个，可能影响新 Pod 调度"
```

---

## 六、经验总结

### 连锁故障的排查方法

这次故障的最大难点在于：**表面现象（Pod 无法调度）和根因（磁盘满）之间隔了两层**，直觉上很难把它们关联起来。

我用的排查思路是：

1. **先看时间线**，不要上来就盯着报错信息。`kubectl get events -A --sort-by='.metadata.creationTimestamp'` 是最快建立全局视角的手段
2. **逆向追溯**：Pending 的原因是没有 IP → IP 为什么耗尽 → 是什么操作消耗了 IP → 是什么触发了这些操作
3. **不要预设结论**：我最初以为是某个服务 Pod 数量暴增把 IP 用完了，结果和实际根因完全不同

### 监控覆盖盲区

这次故障暴露了一个监控盲区：网络层的 IP 资源。对于使用 Terway 的阿里云 ACK 集群，以下指标应该纳入监控体系：

| 监控项 | 告警阈值 | 说明 |
|--------|----------|------|
| 节点 ENI 可用 IP 数量 | < 3 个 | 剩余过少时提前告警 |
| 节点磁盘使用率 | > 80% | 比 kubelet 驱逐阈值提前 10% |
| Terway IP 分配成功率 | < 99% | 分配失败意味着网络层有异常 |

### DiskPressure 的危害远超磁盘本身

很多人对 DiskPressure 的认知停留在"磁盘满了，加存储就好"。但实际上：

- kubelet 的驱逐是**强制删除**，不等 preStop 完成，不等 Graceful Termination timeout
- 强制删除可能打断各类资源的清理逻辑（不只是 Terway，数据库连接池、消息队列消费者都可能因此产生问题）
- 大量 Pod 同时被驱逐，重新调度时可能形成"调度风暴"，进一步加重集群压力

所以，磁盘告警要早，处置要快，不要等到 kubelet 自己动手。

---

故障复盘到这里。整个排查花了约 3 小时，其中大部分时间花在理解 Terway CRD IPAM 工作机制上——这类插件的内部状态对大多数人来说是黑盒。希望这篇记录能帮助遇到类似问题的人少走一些弯路。
