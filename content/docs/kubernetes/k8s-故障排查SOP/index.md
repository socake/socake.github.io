---
title: "Kubernetes 故障排查 SOP"
date: 2025-12-09T11:00:00+08:00
draft: false
tags: ["Kubernetes", "运维", "故障排查", "SOP"]
categories: ["Kubernetes"]
description: "系统化的 Kubernetes 故障排查标准操作流程，覆盖 Pod、Node、Service、网络、存储等常见故障场景，附完整排查命令合集。"
summary: "从现象到根因的 K8s 故障排查全流程：Pod 异常状态、Node NotReady、Service 不通、存储挂载失败等场景的系统化排查方法。"
toc: true
math: false
diagram: false
keywords: ["Kubernetes", "故障排查", "Pod", "CrashLoopBackOff", "OOMKilled", "Node", "Service"]
params:
  reading_time: true
---

## 排查总体思路

遇到 K8s 故障，不要一上来就乱翻日志。先建立一个清晰的排查路径：

```
现象确认 → 定位资源 → 查看状态 → 分析事件 → 读取日志 → 找到根因 → 修复验证
```

**黄金三问：**
1. 什么资源出了问题？（Pod / Node / Service / PVC）
2. 什么时候开始的？（events 的 FirstSeen）
3. 发生了什么变更？（发布、配置修改、节点替换）

---

## Pod 常见状态排查

### 快速查看集群整体状态

```bash
# 查看所有 namespace 的异常 Pod（非 Running/Completed 状态）
kubectl get pods -A --field-selector='status.phase!=Running,status.phase!=Succeeded'

# 查看某 namespace 下所有 Pod
kubectl get pods -n <namespace> -o wide

# 查看 Pod 详情（事件是排查的关键）
kubectl describe pod <pod-name> -n <namespace>

# 查看 Pod 日志（当前容器）
kubectl logs <pod-name> -n <namespace>

# 查看上一次崩溃容器的日志
kubectl logs <pod-name> -n <namespace> --previous

# 实时跟踪日志
kubectl logs -f <pod-name> -n <namespace> -c <container-name>
```

---

### Pending — Pod 无法调度

| 原因 | 排查命令 | 解决方法 |
|------|----------|----------|
| 资源不足（CPU/内存） | `kubectl describe pod` → Events 看 Insufficient | 扩容节点 / 降低 requests |
| 没有满足 nodeSelector/affinity 的节点 | `kubectl describe pod` → MatchNodeSelector | 修正 label 或放宽 affinity |
| Taint 未容忍 | `kubectl describe node` → Taints | 添加对应 toleration |
| PVC 未绑定 | `kubectl get pvc -n <ns>` | 检查 StorageClass / PV |
| 调度器宕机 | `kubectl get pods -n kube-system` | 重启 kube-scheduler |

```bash
# 查看节点资源剩余
kubectl describe nodes | grep -A 5 "Allocated resources"

# 查看节点 Taint
kubectl get nodes -o custom-columns=NAME:.metadata.name,TAINTS:.spec.taints

# 检查 PVC 状态
kubectl get pvc -n <namespace>
kubectl describe pvc <pvc-name> -n <namespace>
```

---

### CrashLoopBackOff — 容器反复重启

**排查思路：** 容器启动后立刻退出，K8s 会以指数退避方式重启。

```bash
# 查看退出码（关键）
kubectl describe pod <pod-name> -n <namespace> | grep -A 5 "Last State"

# 查看崩溃时的日志
kubectl logs <pod-name> -n <namespace> --previous

# 查看重启次数
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.status.containerStatuses[0].restartCount}'
```

| 退出码 | 含义 | 常见原因 |
|--------|------|----------|
| 1 | 应用错误 | 配置错误、依赖缺失 |
| 2 | bash 误用 | shell 脚本语法错误 |
| 137 | OOMKilled（128+9） | 内存超限 |
| 139 | Segfault（128+11） | 程序段错误 |
| 143 | 正常终止（128+15） | SIGTERM 处理不当 |

```bash
# 临时注入调试：覆盖启动命令让容器保持运行
kubectl patch deployment <name> -n <namespace> -p '{"spec":{"template":{"spec":{"containers":[{"name":"<container>","command":["sleep","3600"]}]}}}}'
```

---

### OOMKilled — 内存超限被杀

```bash
# 确认 OOMKilled
kubectl describe pod <pod-name> | grep -i oom

# 查看节点上的 OOM 日志
kubectl get events -n <namespace> --field-selector=reason=OOMKilling

# 查看当前 Pod 资源限制
kubectl get pod <pod-name> -o jsonpath='{.spec.containers[0].resources}'
```

**解决方法：**

```yaml
# 调整 resources limit
resources:
  requests:
    memory: "256Mi"
    cpu: "100m"
  limits:
    memory: "512Mi"   # 根据实际用量设置，不要过小
    cpu: "500m"
```

```bash
# 用 metrics-server 查看实时内存用量
kubectl top pods -n <namespace> --sort-by=memory

# 查看节点内存压力
kubectl describe node <node-name> | grep -A 10 "Conditions:"
```

---

### Evicted — Pod 被驱逐

```bash
# 查看所有被驱逐的 Pod
kubectl get pods -A | grep Evicted

# 清理 Evicted 状态的 Pod
kubectl get pods -A | grep Evicted | awk '{print $1, $2}' | xargs -L1 bash -c 'kubectl delete pod $1 -n $0'

# 查看驱逐原因
kubectl describe pod <evicted-pod> | grep -A 5 "Message"
```

| 驱逐原因 | 说明 | 处理方法 |
|----------|------|----------|
| `memory.available` 低于阈值 | 节点内存压力 | 扩容节点 / 降低内存用量 |
| `nodefs.available` 低于阈值 | 节点磁盘压力 | 清理日志 / 镜像 |
| `imagefs.available` 低于阈值 | 容器镜像磁盘压力 | `docker system prune` |

---

### ImagePullBackOff / ErrImagePull — 镜像拉取失败

```bash
# 查看事件中的具体错误
kubectl describe pod <pod-name> | grep -A 10 "Events:"
```

| 原因 | 排查方式 | 解决方法 |
|------|----------|----------|
| 镜像名/tag 错误 | describe 看 image 字段 | 修正镜像名 |
| 私有仓库未配置凭证 | describe 看 unauthorized | 创建 imagePullSecret |
| 网络无法访问 registry | 在节点上 `curl registry` | 配置代理 / 修改网络 |
| 镜像不存在 | 到 registry 确认 | 重新推送镜像 |

```bash
# 创建 Docker Registry Secret
kubectl create secret docker-registry regcred \
  --docker-server=<registry-url> \
  --docker-username=<username> \
  --docker-password=<password> \
  -n <namespace>

# 在 Pod/Deployment 中引用
# spec.imagePullSecrets:
# - name: regcred
```

---

### Terminating 卡住 — Pod 无法删除

```bash
# 查看 Pod 的 finalizers
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.metadata.finalizers}'

# 强制删除（最后手段，确认无副作用后使用）
kubectl delete pod <pod-name> -n <namespace> --force --grace-period=0

# 如果有 finalizer 导致卡住，先清除 finalizer
kubectl patch pod <pod-name> -n <namespace> -p '{"metadata":{"finalizers":[]}}' --type=merge
```

**Terminating 卡住常见原因：**
- 容器内进程未响应 SIGTERM（需要应用处理优雅退出）
- 存储卷 unmount 失败（查看节点 kubelet 日志）
- 自定义 finalizer 逻辑卡死

---

## Node 问题排查

### Node NotReady

```bash
# 查看 Node 状态
kubectl get nodes
kubectl describe node <node-name>

# 查看 Node 上的 kubelet 日志（在节点上执行）
journalctl -u kubelet -f --since "10 minutes ago"

# 查看 Node 上的系统日志
journalctl -k | grep -i "oom\|killed\|error" | tail -50
```

| 原因 | 判断方法 | 处理方法 |
|------|----------|----------|
| kubelet 宕机 | `systemctl status kubelet` | `systemctl restart kubelet` |
| 磁盘压力 DiskPressure | `kubectl describe node` → Conditions | 清理磁盘 |
| 内存压力 MemoryPressure | `kubectl describe node` → Conditions | 扩容/驱逐 Pod |
| 网络分区 | ping 节点 IP | 检查网络设备/安全组 |
| 证书过期 | kubelet 日志看 TLS error | 轮转证书 |

```bash
# 检查节点磁盘使用
df -h
du -sh /var/lib/docker/* 2>/dev/null | sort -rh | head -10

# 清理无用镜像（在节点上）
docker system prune -af
# 或（containerd）
crictl rmi --prune

# 检查节点内存
free -h
cat /proc/meminfo | grep -E "MemTotal|MemFree|MemAvailable"
```

### 节点磁盘/内存压力处理

```bash
# 将节点标记为不可调度
kubectl cordon <node-name>

# 驱逐节点上的 Pod
kubectl drain <node-name> --ignore-daemonsets --delete-emptydir-data

# 处理完后恢复调度
kubectl uncordon <node-name>
```

---

## Service 不通排查

### 排查路径

```
客户端 → DNS解析 → Service ClusterIP → Endpoints → Pod
```

```bash
# 1. 确认 Service 存在且 ClusterIP 正确
kubectl get svc <svc-name> -n <namespace>

# 2. 检查 Endpoints 是否有 Pod IP（关键！）
kubectl get endpoints <svc-name> -n <namespace>
# Endpoints 为空说明 selector 匹配不到 Pod

# 3. 确认 Pod label 与 Service selector 一致
kubectl get svc <svc-name> -n <namespace> -o jsonpath='{.spec.selector}'
kubectl get pods -n <namespace> --show-labels
```

### DNS 排查

```bash
# 在集群内部署测试 Pod
kubectl run dns-test --image=busybox:1.35 --restart=Never -it --rm -- sh

# 在测试 Pod 内执行
nslookup <svc-name>.<namespace>.svc.cluster.local
nslookup kubernetes.default.svc.cluster.local

# 查看 CoreDNS 状态
kubectl get pods -n kube-system -l k8s-app=kube-dns
kubectl logs -n kube-system -l k8s-app=kube-dns --tail=50
```

### kube-proxy 排查

```bash
# 查看 kube-proxy 状态
kubectl get pods -n kube-system -l k8s-app=kube-proxy
kubectl logs -n kube-system -l k8s-app=kube-proxy --tail=30

# 检查 iptables 规则是否存在（节点上执行）
iptables -t nat -L KUBE-SERVICES | grep <cluster-ip>

# ipvs 模式下
ipvsadm -Ln | grep <cluster-ip>
```

---

## 网络连通性排查工具

### netshoot 临时容器（推荐）

```bash
# 部署 netshoot 调试容器
kubectl run netshoot --image=nicolaka/netshoot --restart=Never -it --rm -n <namespace> -- bash

# 在 netshoot 中可用的工具：
# curl, dig, nslookup, ping, traceroute, ss, netstat, tcpdump, iperf3

# 测试 Service 连通性
curl -v http://<svc-name>.<namespace>.svc.cluster.local:<port>

# DNS 解析
dig <svc-name>.<namespace>.svc.cluster.local

# 测试 Pod 间连通
ping <pod-ip>
```

### 注入临时调试容器到运行中的 Pod（K8s 1.23+）

```bash
kubectl debug -it <pod-name> -n <namespace> \
  --image=nicolaka/netshoot \
  --target=<container-name>
```

### 在节点上抓包

```bash
# 找到 Pod 的 veth 接口（在节点上执行）
# 先找 Pod 的网络命名空间
POD_ID=$(crictl pods --name <pod-name> -q)
crictl inspectp $POD_ID | grep pid

# 抓包
nsenter -t <pid> -n -- tcpdump -i eth0 -w /tmp/capture.pcap port 8080
```

---

## 存储问题排查

### PVC Pending — 无法绑定

```bash
# 查看 PVC 状态
kubectl get pvc -n <namespace>
kubectl describe pvc <pvc-name> -n <namespace>

# 查看 StorageClass
kubectl get storageclass
kubectl describe storageclass <sc-name>

# 查看 PV 列表
kubectl get pv
```

| 原因 | 判断 | 解决 |
|------|------|------|
| 没有匹配的 StorageClass | describe PVC → no volume plugin | 创建正确的 SC |
| 静态 PV 未匹配 | PV 的 accessMode/storageClassName 不匹配 | 修正 PV 配置 |
| provisioner 不工作 | SC provisioner Pod 日志 | 修复 provisioner |
| 跨 AZ 问题 | Pod 与 PV 在不同 AZ | 配置 volumeBindingMode: WaitForFirstConsumer |

```yaml
# 推荐：延迟绑定，等 Pod 调度后再绑定 PV
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: standard
provisioner: kubernetes.io/aws-ebs
volumeBindingMode: WaitForFirstConsumer  # 关键配置
reclaimPolicy: Retain
```

### 存储挂载失败

```bash
# 查看 Pod 事件中的挂载错误
kubectl describe pod <pod-name> | grep -A 20 "Events:"

# 查看节点上 kubelet 的存储日志
journalctl -u kubelet | grep -i "volume\|mount\|attach" | tail -30

# 强制 detach 卡住的卷（谨慎使用）
kubectl patch pv <pv-name> -p '{"spec":{"claimRef": null}}'
```

---

## 性能问题排查

### CPU Throttling

```bash
# 查看 Pod 资源使用
kubectl top pods -n <namespace>
kubectl top pods -n <namespace> --containers

# 查看具体容器的 throttle 情况（在节点上）
# 找到 cgroup 路径
cat /sys/fs/cgroup/cpu/kubepods/pod<pod-uid>/<container-id>/cpu.stat
# throttled_time 不断增加说明在 throttle
```

**处理方法：**

```yaml
# 方案1：提高 CPU limit（注意：不要设置过大）
resources:
  limits:
    cpu: "2"      # 从 500m 提高

# 方案2：移除 CPU limit（争议性方案，仅在资源充足时考虑）
resources:
  requests:
    cpu: "500m"
  # 不设置 limits.cpu
```

### 慢查询/高延迟定位

```bash
# 查看 Pod 的网络指标（需要 metrics-server）
kubectl top pods -n <namespace> --sort-by=cpu

# 检查是否有大量 TIME_WAIT（在 Pod 内或节点上）
ss -s

# 查看连接数
ss -tan | grep ESTABLISHED | wc -l
```

---

## 常用排查命令合集

### 快速诊断脚本

```bash
#!/bin/bash
# k8s-diagnose.sh — 快速诊断某个 namespace
NAMESPACE=${1:-default}

echo "=== Pods 状态 ==="
kubectl get pods -n $NAMESPACE -o wide

echo ""
echo "=== 异常 Pod ==="
kubectl get pods -n $NAMESPACE | grep -v "Running\|Completed\|NAME"

echo ""
echo "=== 最近的 Events（按时间排序）==="
kubectl get events -n $NAMESPACE --sort-by='.lastTimestamp' | tail -20

echo ""
echo "=== Node 状态 ==="
kubectl get nodes

echo ""
echo "=== PVC 状态 ==="
kubectl get pvc -n $NAMESPACE
```

### 常用命令速查表

```bash
# Pod 相关
kubectl get pods -A -o wide                          # 所有 Pod
kubectl describe pod <name> -n <ns>                  # Pod 详情+事件
kubectl logs <name> -n <ns> --previous               # 上次崩溃日志
kubectl exec -it <name> -n <ns> -- bash              # 进入容器
kubectl get pod <name> -n <ns> -o yaml               # 完整 YAML

# Node 相关
kubectl get nodes -o wide                            # 节点列表
kubectl describe node <name>                         # 节点详情
kubectl cordon <name>                                # 禁止调度
kubectl drain <name> --ignore-daemonsets             # 驱逐 Pod
kubectl uncordon <name>                              # 恢复调度

# 事件相关
kubectl get events -n <ns> --sort-by='.lastTimestamp'         # 按时间排序
kubectl get events -n <ns> --field-selector=type=Warning       # 只看 Warning
kubectl get events -A --field-selector=reason=OOMKilling        # 全局 OOM 事件

# Service/网络
kubectl get svc,endpoints -n <ns>                    # Service + Endpoints
kubectl port-forward svc/<name> 8080:80 -n <ns>      # 端口转发调试

# 资源使用
kubectl top pods -n <ns> --sort-by=memory            # 内存排序
kubectl top nodes                                    # 节点资源

# 批量操作
kubectl delete pods -n <ns> --field-selector=status.phase=Failed   # 清理 Failed Pod
kubectl delete pods -n <ns> --field-selector=status.phase=Evicted  # 清理 Evicted

# 调试
kubectl run debug --image=nicolaka/netshoot --restart=Never -it --rm -- bash
kubectl debug node/<node-name> -it --image=ubuntu    # 调试节点
```

### 获取集群整体健康状态

```bash
# 检查核心组件
kubectl get componentstatuses  # 老版本
kubectl get pods -n kube-system

# 检查 API Server 可达性
kubectl cluster-info

# 检查证书到期时间（kubeadm 集群）
kubeadm certs check-expiration

# 查看集群版本
kubectl version --short
```

---

## 排查 Checklist

在提交故障报告或升级之前，确认已经检查过：

- [ ] `kubectl describe pod` 的 Events 部分
- [ ] `kubectl logs --previous` 查看崩溃前日志
- [ ] `kubectl get events --sort-by=lastTimestamp` 时间线
- [ ] `kubectl top pods/nodes` 资源使用情况
- [ ] `kubectl get endpoints` 确认 Service 后端正常
- [ ] Node 状态是否 Ready
- [ ] PVC 是否 Bound
- [ ] 最近是否有发布变更（对照发布记录）
