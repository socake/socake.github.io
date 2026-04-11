---
title: "kubectl 命令速查手册"
date: 2025-12-08T11:00:00+08:00
draft: false
tags: ["Kubernetes", "kubectl", "运维"]
categories: ["Kubernetes"]
description: "kubectl 高频命令速查，覆盖日常运维、故障排查、资源管理全场景"
summary: "kubectl 实用命令手册，按场景分类整理，涵盖资源查看、Pod调试、日志查看、滚动更新、扩缩容、强制删除等高频操作。"
toc: true
math: false
diagram: false
keywords: ["kubectl", "kubernetes", "命令", "速查"]
params:
  reading_time: true
---

## 基础配置

### kubeconfig 多集群管理

```bash
# 查看当前 kubeconfig 配置
kubectl config view

# 查看所有可用 context（集群）
kubectl config get-contexts

# 切换到指定 context
kubectl config use-context my-cluster-prod

# 查看当前 context
kubectl config current-context

# 合并多个 kubeconfig 文件（临时生效）
export KUBECONFIG=~/.kube/config:~/.kube/config-prod:~/.kube/config-dev

# 永久合并：先合并再保存（操作前备份原文件）
KUBECONFIG=~/.kube/config:~/.kube/new-cluster.yaml kubectl config view --flatten > /tmp/merged.yaml
mv /tmp/merged.yaml ~/.kube/config
```

### Context 管理

```bash
# 创建新 context（指定集群、用户、命名空间）
kubectl config set-context my-context \
  --cluster=my-cluster \
  --user=my-user \
  --namespace=default

# 修改已有 context 的默认命名空间
kubectl config set-context --current --namespace=production

# 删除 context
kubectl config delete-context old-context

# 重命名 context（常用于 EKS 自动生成的超长名称）
kubectl config rename-context arn:aws:eks:us-west-2:123456789:cluster/my-cluster my-cluster-prod
```

### 命名空间切换

```bash
# 查看所有命名空间
kubectl get namespaces

# 切换默认命名空间（无需每次加 -n）
kubectl config set-context --current --namespace=production

# 使用 kubens 工具快速切换（推荐安装 kubectx/kubens）
# 安装：brew install kubectx  或  https://github.com/ahmetb/kubectx
kubens                    # 列出所有命名空间
kubens production         # 切换到 production
kubens -                  # 切换到上一个命名空间（类似 cd -）

# 使用 kubectx 切换集群
kubectx                   # 列出所有 context
kubectx my-cluster-prod   # 切换 context
kubectx -                 # 切换到上一个 context
```

---

## 查看资源

### 基础查看

```bash
# 查看 Pod（当前命名空间）
kubectl get pods

# 查看所有命名空间的 Pod（排查全局问题时用）
kubectl get pods -A
kubectl get pods --all-namespaces

# 查看更多信息：节点、IP、状态
kubectl get pods -o wide

# 实时监控 Pod 状态变化（等价于 watch）
kubectl get pods -w
kubectl get pods --watch

# 查看常用资源类型
kubectl get nodes
kubectl get services
kubectl get deployments
kubectl get statefulsets
kubectl get daemonsets
kubectl get ingress
kubectl get configmap
kubectl get secret
kubectl get pvc
kubectl get pv
kubectl get hpa
kubectl get cronjobs
kubectl get jobs

# 同时查看多种资源
kubectl get pods,svc,deploy -n production
```

### 输出格式

```bash
# YAML 格式输出（用于导出配置、对比差异）
kubectl get pod my-pod -o yaml

# JSON 格式（方便 jq 处理）
kubectl get pod my-pod -o json

# 自定义列输出（只看关心的字段）
kubectl get pods -o custom-columns=NAME:.metadata.name,STATUS:.status.phase,NODE:.spec.nodeName

# 只输出名称（方便脚本处理）
kubectl get pods -o name

# jsonpath 提取特定字段（见后文详细说明）
kubectl get pod my-pod -o jsonpath='{.status.podIP}'
```

### 标签选择器

```bash
# 按标签筛选（最常用）
kubectl get pods -l app=nginx
kubectl get pods -l app=nginx,env=production

# 不等于
kubectl get pods -l app!=nginx

# 集合操作（in / notin）
kubectl get pods -l 'env in (production, staging)'
kubectl get pods -l 'env notin (development)'

# 查看 Pod 的标签
kubectl get pods --show-labels

# 给 Pod 打标签
kubectl label pod my-pod version=v2

# 覆盖已有标签（需加 --overwrite）
kubectl label pod my-pod version=v3 --overwrite

# 删除标签（标签名后加 -）
kubectl label pod my-pod version-
```

### field-selector 过滤

```bash
# 按节点过滤 Pod
kubectl get pods --field-selector spec.nodeName=node-01

# 按状态过滤
kubectl get pods --field-selector status.phase=Running
kubectl get pods --field-selector status.phase!=Running

# 组合过滤：指定节点上非 Running 的 Pod
kubectl get pods --field-selector spec.nodeName=node-01,status.phase!=Running -A
```

### 排序

```bash
# 按重启次数排序（找频繁重启的 Pod）
kubectl get pods --sort-by='.status.containerStatuses[0].restartCount'

# 按创建时间排序
kubectl get pods --sort-by=.metadata.creationTimestamp

# 按 CPU 使用排序（需要 metrics-server）
kubectl top pods --sort-by=cpu
kubectl top pods --sort-by=memory
```

---

## Pod 调试

### 日志查看

```bash
# 查看 Pod 日志
kubectl logs my-pod

# 实时跟踪日志（生产最常用）
kubectl logs -f my-pod

# 查看上一个（已崩溃）容器的日志（排查 CrashLoopBackOff 必用）
kubectl logs my-pod --previous
kubectl logs my-pod -p

# 多容器 Pod 指定容器
kubectl logs my-pod -c my-container

# 只看最后 N 行
kubectl logs my-pod --tail=100

# 查看最近一段时间的日志
kubectl logs my-pod --since=1h
kubectl logs my-pod --since=30m
kubectl logs my-pod --since=2006-01-02T15:04:05Z  # RFC3339 格式

# 组合：实时跟踪最后 50 行
kubectl logs -f my-pod --tail=50

# 通过标签选择 Pod 查看日志（同一 Deployment 多副本时用）
kubectl logs -l app=nginx --tail=100

# 同时查看多个容器日志（需要 stern 工具）
# stern my-pod                  # 匹配 pod 名前缀
# stern -l app=nginx            # 按标签
# stern my-pod --since 15m      # 最近 15 分钟
```

### 进入容器

```bash
# 进入容器交互式 shell（最常用）
kubectl exec -it my-pod -- /bin/bash
kubectl exec -it my-pod -- /bin/sh   # 如果没有 bash

# 多容器 Pod 指定容器
kubectl exec -it my-pod -c my-container -- /bin/bash

# 执行单条命令（不进入交互）
kubectl exec my-pod -- ls /app
kubectl exec my-pod -- env | grep DATABASE
kubectl exec my-pod -- cat /etc/resolv.conf  # 排查 DNS 配置

# 在指定节点上的 Pod 执行命令
kubectl exec -it $(kubectl get pod -l app=nginx -o jsonpath='{.items[0].metadata.name}') -- /bin/bash
```

### describe 查看事件

```bash
# 查看 Pod 详情（包含 Events，排查启动失败必看）
kubectl describe pod my-pod

# 查看 Node 详情（排查节点压力、taint、分配情况）
kubectl describe node node-01

# 查看 Deployment
kubectl describe deployment my-deployment

# 查看 Service（排查网络问题）
kubectl describe service my-service

# 查看 PVC（排查存储挂载问题）
kubectl describe pvc my-pvc
```

### port-forward 本地调试

```bash
# 本地 8080 转发到 Pod 的 80 端口（直连 Pod，绕过 Service）
kubectl port-forward pod/my-pod 8080:80

# 通过 Service 转发（更稳定，Pod 重启不断）
kubectl port-forward svc/my-service 8080:80

# 通过 Deployment 转发
kubectl port-forward deployment/my-deployment 8080:80

# 监听所有本机网卡（让其他机器也能访问）
kubectl port-forward svc/my-service 8080:80 --address=0.0.0.0

# 后台运行
kubectl port-forward svc/my-service 8080:80 &
```

### 文件复制

```bash
# 从 Pod 复制文件到本地
kubectl cp my-pod:/app/logs/app.log ./app.log

# 从本地复制文件到 Pod（上传配置文件调试时用）
kubectl cp ./config.yaml my-pod:/app/config.yaml

# 多容器 Pod 指定容器
kubectl cp my-pod:/app/logs/app.log ./app.log -c my-container

# 复制整个目录
kubectl cp my-pod:/app/logs ./logs/
```

### 资源使用监控

```bash
# 查看 Pod 资源使用（需要 metrics-server）
kubectl top pods
kubectl top pods -n production
kubectl top pods -A  # 所有命名空间

# 查看节点资源使用
kubectl top nodes

# 按 CPU 排序找出最耗资源的 Pod
kubectl top pods -A --sort-by=cpu | head -20

# 按内存排序
kubectl top pods -A --sort-by=memory | head -20
```

### 临时容器调试（ephemeral containers）

```bash
# Kubernetes 1.23+ 支持，无需重启 Pod 注入调试工具
# 向运行中的 Pod 注入调试容器（使用 busybox 镜像）
kubectl debug -it my-pod --image=busybox --target=my-container

# 使用更完整的调试镜像
kubectl debug -it my-pod --image=nicolaka/netshoot --target=my-container

# 调试节点（在节点上创建特权容器）
kubectl debug node/node-01 -it --image=busybox

# 复制 Pod 并修改（适用于不支持 ephemeral containers 的旧版本）
# 创建一个副本，覆盖 command 以阻止崩溃
kubectl debug my-pod -it --image=busybox --copy-to=my-pod-debug -- sh

# 调试 CrashLoopBackOff（修改副本的 command，让它不崩溃）
kubectl debug my-pod --copy-to=my-pod-debug --image=my-app:latest -- sleep 3600
```

---

## 部署与更新

### 基础操作

```bash
# 应用配置文件（创建或更新，幂等）
kubectl apply -f deployment.yaml

# 应用整个目录
kubectl apply -f ./manifests/

# 递归应用
kubectl apply -f ./manifests/ -R

# 创建资源（文件中有同名资源会报错，不如 apply）
kubectl create -f deployment.yaml

# 删除资源
kubectl delete -f deployment.yaml
kubectl delete pod my-pod
kubectl delete deployment my-deployment -n production

# 强制删除（见后文排查部分）
kubectl delete pod my-pod --grace-period=0 --force
```

### 滚动更新管理

```bash
# 查看 Deployment 滚动更新状态（CI/CD 中等待部署完成）
kubectl rollout status deployment/my-deployment
kubectl rollout status deployment/my-deployment -n production --timeout=5m

# 查看更新历史
kubectl rollout history deployment/my-deployment

# 查看某个版本的详情（需要 --record 或 change-cause annotation）
kubectl rollout history deployment/my-deployment --revision=2

# 回滚到上一个版本（生产故障时最快操作）
kubectl rollout undo deployment/my-deployment

# 回滚到指定版本
kubectl rollout undo deployment/my-deployment --to-revision=2

# 暂停滚动更新（批量修改配置时先暂停，避免多次触发）
kubectl rollout pause deployment/my-deployment

# 恢复滚动更新
kubectl rollout resume deployment/my-deployment

# 重启 Deployment（触发滚动重启所有 Pod，常用于更新 ConfigMap 后）
kubectl rollout restart deployment/my-deployment
kubectl rollout restart deployment/my-deployment -n production
```

### 更新镜像

```bash
# 更新 Deployment 的镜像（CI/CD 常用）
kubectl set image deployment/my-deployment my-container=my-image:v2.0

# 更新并立即查看状态
kubectl set image deployment/my-deployment my-container=my-image:v2.0 && \
  kubectl rollout status deployment/my-deployment

# 更新多个容器
kubectl set image deployment/my-deployment \
  app=my-image:v2.0 \
  sidecar=sidecar-image:v1.1

# 更新 DaemonSet / StatefulSet
kubectl set image daemonset/my-ds my-container=my-image:v2.0
kubectl set image statefulset/my-sts my-container=my-image:v2.0
```

### 扩缩容

```bash
# 手动扩缩容
kubectl scale deployment my-deployment --replicas=5

# 缩到 0（临时下线，但保留配置）
kubectl scale deployment my-deployment --replicas=0

# 基于当前状态条件扩容（只有当前是 3 副本时才执行，防误操作）
kubectl scale deployment my-deployment --replicas=5 --current-replicas=3

# 批量扩容（同一 namespace 下多个 deployment）
kubectl scale deployment/deploy-a deployment/deploy-b --replicas=3

# 查看 HPA 状态（自动扩缩容）
kubectl get hpa
kubectl describe hpa my-hpa
```

### patch 局部更新

```bash
# strategic merge patch：更新 replicas
kubectl patch deployment my-deployment -p '{"spec":{"replicas":3}}'

# 更新资源 limits
kubectl patch deployment my-deployment -p '
{
  "spec": {
    "template": {
      "spec": {
        "containers": [{
          "name": "my-container",
          "resources": {
            "limits": {"cpu": "500m", "memory": "512Mi"},
            "requests": {"cpu": "250m", "memory": "256Mi"}
          }
        }]
      }
    }
  }
}'

# JSON patch（精确操作，按 path 修改）
kubectl patch deployment my-deployment --type='json' \
  -p='[{"op": "replace", "path": "/spec/replicas", "value": 5}]'

# 添加 annotation（例如记录变更原因）
kubectl patch deployment my-deployment --type='json' \
  -p='[{"op": "add", "path": "/metadata/annotations/change-cause", "value": "upgrade to v2.0"}]'

# merge patch（整体替换指定 path，适合 configmap 内容更新）
kubectl patch configmap my-config --type=merge \
  -p '{"data":{"key":"new-value"}}'
```

### edit 实时编辑

```bash
# 直接编辑资源（保存后立即生效）
kubectl edit deployment my-deployment

# 指定编辑器
EDITOR=vim kubectl edit deployment my-deployment

# 编辑 ConfigMap
kubectl edit configmap my-config -n production
```

---

## 节点管理

### 禁止/恢复调度

```bash
# 标记节点为不可调度（节点维护前执行）
kubectl cordon node-01

# 恢复节点调度（维护完成后执行）
kubectl uncordon node-01

# 查看节点状态（看 STATUS 列是否有 SchedulingDisabled）
kubectl get nodes
```

### 驱逐节点上的 Pod

```bash
# 驱逐节点上所有 Pod（= cordon + 驱逐，维护节点标准操作）
kubectl drain node-01

# 生产常用参数：
kubectl drain node-01 \
  --ignore-daemonsets \    # 忽略 DaemonSet（无法被驱逐）
  --delete-emptydir-data \ # 删除使用 emptyDir 的 Pod（否则报错）
  --grace-period=60 \      # 给 Pod 60 秒优雅停止时间
  --timeout=300s           # 超时 5 分钟

# 强制驱逐（无 PodDisruptionBudget 保护时慎用）
kubectl drain node-01 --ignore-daemonsets --delete-emptydir-data --force
```

### taint 管理

```bash
# 查看节点的 taint
kubectl describe node node-01 | grep Taint

# 给节点打 taint（只有容忍这个 taint 的 Pod 才能调度上去）
kubectl taint nodes node-01 dedicated=gpu:NoSchedule

# 删除 taint（taint key:effect 后加 -）
kubectl taint nodes node-01 dedicated:NoSchedule-

# Taint effect 说明：
# NoSchedule     — 新 Pod 不能调度，已有 Pod 不影响
# PreferNoSchedule — 尽量不调度，资源不足时仍可调度
# NoExecute      — 不能调度，且会驱逐已有 Pod
```

### 节点标签管理

```bash
# 查看节点标签
kubectl get nodes --show-labels
kubectl get node node-01 -o jsonpath='{.metadata.labels}'

# 给节点打标签（用于 nodeSelector / nodeAffinity）
kubectl label node node-01 node-type=gpu

# 覆盖标签
kubectl label node node-01 node-type=highmem --overwrite

# 删除标签
kubectl label node node-01 node-type-

# 按标签筛选节点
kubectl get nodes -l node-type=gpu
```

---

## 排查高频命令组合

### 找 CrashLoopBackOff 的 Pod

```bash
# 列出所有 CrashLoopBackOff 的 Pod
kubectl get pods -A --field-selector=status.phase!=Running | grep -v Completed

# 更精确的方式：用 jsonpath 过滤
kubectl get pods -A -o json | \
  jq -r '.items[] | select(.status.containerStatuses[]?.state.waiting.reason=="CrashLoopBackOff") | "\(.metadata.namespace)/\(.metadata.name)"'

# 找到后立即看日志（看崩溃原因）
kubectl logs my-pod --previous -n my-namespace

# 查看 describe 里的 Events（看是 OOMKilled 还是 Exit Code）
kubectl describe pod my-pod -n my-namespace | tail -30
```

### 找 Pending Pod 并排查原因

```bash
# 列出所有 Pending 的 Pod
kubectl get pods -A | grep Pending

# 查看 Pending 原因（通常在 Events 里）
kubectl describe pod my-pending-pod -n my-namespace

# 常见 Pending 原因速查：
# Insufficient cpu/memory   → 节点资源不足，kubectl top nodes 确认
# No nodes are available    → 所有节点都有 taint，查 kubectl describe nodes
# PersistentVolumeClaim is not bound → PVC 未绑定，kubectl get pvc
# Unschedulable             → 检查 nodeSelector/affinity 是否匹配节点

# 检查是否有可用节点（看 Allocatable vs Requests）
kubectl describe nodes | grep -A 5 "Allocated resources"
```

### 查找资源使用最高的 Pod

```bash
# CPU 使用 Top 10
kubectl top pods -A --sort-by=cpu | head -11

# 内存使用 Top 10
kubectl top pods -A --sort-by=memory | head -11

# 查看节点资源水位
kubectl top nodes

# 查看某节点上的所有 Pod 及资源使用
kubectl top pods -A --sort-by=cpu | grep node-01
```

### 强制删除卡住的 Pod

```bash
# 普通删除（等待 terminationGracePeriodSeconds，默认 30s）
kubectl delete pod my-pod

# 强制删除（Pod 卡在 Terminating 状态时用）
kubectl delete pod my-pod --grace-period=0 --force

# 批量强制删除 Terminating 状态的 Pod
kubectl get pods -A | grep Terminating | awk '{print "kubectl delete pod " $2 " -n " $1 " --grace-period=0 --force"}' | bash

# 最后手段：直接删除 etcd 中的 finalizer（Pod 有 finalizer 卡住时）
kubectl patch pod my-pod -p '{"metadata":{"finalizers":null}}'
```

### 查看最近事件

```bash
# 查看当前命名空间的 Events（按时间排序）
kubectl get events --sort-by=.lastTimestamp

# 查看所有命名空间的 Events
kubectl get events -A --sort-by=.lastTimestamp

# 只看 Warning 事件
kubectl get events --field-selector type=Warning

# 只看某个 Pod 的事件
kubectl get events --field-selector involvedObject.name=my-pod

# 实时监控事件
kubectl get events -w

# 查看最近 1 小时内的事件（jq 过滤）
kubectl get events -A -o json | \
  jq -r '.items | sort_by(.lastTimestamp) | .[] | select(.type=="Warning") | "\(.lastTimestamp) \(.metadata.namespace) \(.involvedObject.name): \(.message)"'
```

### 排查网络连通性

```bash
# 在集群内临时起一个调试 Pod（排查 DNS / 服务连通性）
kubectl run debug-pod --image=nicolaka/netshoot -it --rm -- /bin/bash

# 测试 DNS 解析
kubectl run debug-pod --image=busybox -it --rm -- nslookup my-service.production.svc.cluster.local

# 测试 Service 连通性
kubectl run debug-pod --image=busybox -it --rm -- wget -qO- http://my-service.production.svc.cluster.local:8080/health

# 查看 Service 的 Endpoints（确认 Pod 是否被正确选中）
kubectl get endpoints my-service
kubectl describe endpoints my-service
```

### 查看容器退出原因

```bash
# 查看 Exit Code（exit 137 = OOMKilled，exit 1 = 程序错误，exit 143 = SIGTERM）
kubectl get pod my-pod -o jsonpath='{.status.containerStatuses[0].lastState.terminated.exitCode}'
kubectl get pod my-pod -o jsonpath='{.status.containerStatuses[0].lastState.terminated.reason}'

# 综合查看（namespace 下所有容器的退出状态）
kubectl get pods -n production -o json | \
  jq -r '.items[] | .metadata.name as $name | .status.containerStatuses[]? | select(.lastState.terminated != null) | "\($name): exitCode=\(.lastState.terminated.exitCode) reason=\(.lastState.terminated.reason)"'
```

---

## RBAC 管理

### 权限检查

```bash
# 检查当前用户是否有某项权限
kubectl auth can-i get pods
kubectl auth can-i create deployments -n production
kubectl auth can-i '*' '*'  # 是否有超级管理员权限

# 检查指定用户/ServiceAccount 的权限
kubectl auth can-i get pods --as=system:serviceaccount:production:my-sa
kubectl auth can-i list secrets --as=user@example.com -n production

# 查看当前用户信息
kubectl auth whoami

# 列出某个 ServiceAccount 可以执行的操作（需要 kubectl-access-matrix 插件）
# kubectl access-matrix --sa production:my-sa
```

### ServiceAccount 操作

```bash
# 查看 ServiceAccount
kubectl get serviceaccount -n production
kubectl get sa -n production  # 缩写

# 查看 SA 绑定的 Role
kubectl get rolebindings -n production -o json | \
  jq -r '.items[] | select(.subjects[]?.name=="my-sa") | .metadata.name + " -> " + .roleRef.name'

# 查看 ClusterRoleBinding
kubectl get clusterrolebindings -o json | \
  jq -r '.items[] | select(.subjects[]?.name=="my-sa") | .metadata.name + " -> " + .roleRef.name'

# 列出所有 Role 和 ClusterRole
kubectl get roles -n production
kubectl get clusterroles | grep -v system:

# 查看 Role 详情（有哪些权限）
kubectl describe role my-role -n production
kubectl describe clusterrole my-cluster-role
```

### 常用 RBAC 资源

```bash
# 创建 ServiceAccount
kubectl create serviceaccount my-sa -n production

# 创建 Role（只有 pod 的 get/list 权限）
kubectl create role pod-reader \
  --verb=get,list,watch \
  --resource=pods \
  -n production

# 绑定 Role 到 SA
kubectl create rolebinding pod-reader-binding \
  --role=pod-reader \
  --serviceaccount=production:my-sa \
  -n production

# 绑定 ClusterRole（跨命名空间时用）
kubectl create clusterrolebinding my-binding \
  --clusterrole=cluster-admin \
  --serviceaccount=production:my-sa
```

---

## 实用技巧

### dry-run 生成模板

```bash
# 生成 Deployment YAML 模板（不实际创建）
kubectl create deployment my-app --image=nginx --dry-run=client -o yaml

# 生成 Service YAML
kubectl create service clusterip my-svc --tcp=80:8080 --dry-run=client -o yaml

# 生成 ConfigMap YAML
kubectl create configmap my-config --from-literal=key=value --dry-run=client -o yaml

# 生成 Secret YAML
kubectl create secret generic my-secret --from-literal=password=mypassword --dry-run=client -o yaml

# 导出已有资源为干净的 YAML（去掉 status 等运行时字段）
kubectl get deployment my-deployment -o yaml | \
  kubectl neat         # 需要安装 kubectl-neat 插件
```

### kubectl explain 查字段文档

```bash
# 查看资源字段说明
kubectl explain pod
kubectl explain pod.spec
kubectl explain pod.spec.containers
kubectl explain pod.spec.containers.resources
kubectl explain deployment.spec.strategy

# 递归查看所有子字段
kubectl explain pod --recursive
kubectl explain pod.spec --recursive | grep -i affinity
```

### jsonpath 提取字段

```bash
# 提取单个 Pod 的 IP
kubectl get pod my-pod -o jsonpath='{.status.podIP}'

# 提取所有 Pod 的名称和 IP（用换行符分隔）
kubectl get pods -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.podIP}{"\n"}{end}'

# 提取所有节点的 ExternalIP
kubectl get nodes -o jsonpath='{.items[*].status.addresses[?(@.type=="ExternalIP")].address}'

# 提取 Deployment 的当前镜像
kubectl get deployment my-deployment -o jsonpath='{.spec.template.spec.containers[0].image}'

# 提取 Secret 的 base64 值并解码
kubectl get secret my-secret -o jsonpath='{.data.password}' | base64 -d

# 提取所有 ImagePullSecrets
kubectl get pods -o jsonpath='{range .items[*]}{.metadata.name}{": "}{.spec.imagePullSecrets[*].name}{"\n"}{end}'
```

### 别名推荐

```bash
# 加入 ~/.bashrc 或 ~/.zshrc

# 基础别名
alias k='kubectl'
alias kg='kubectl get'
alias kd='kubectl describe'
alias kdel='kubectl delete'
alias kl='kubectl logs'
alias klf='kubectl logs -f'
alias ke='kubectl exec -it'
alias kaf='kubectl apply -f'

# 带命名空间
alias kgp='kubectl get pods'
alias kgpa='kubectl get pods -A'
alias kgpw='kubectl get pods -w'
alias kgn='kubectl get nodes'
alias kgs='kubectl get svc'
alias kgd='kubectl get deployments'

# 快速查看 Pod 日志（上一个）
alias klp='kubectl logs --previous'

# 切换命名空间
alias kns='kubectl config set-context --current --namespace'

# 查看所有命名空间资源
alias kall='kubectl get all -A'

# 函数：快速进入 Pod（模糊匹配）
kexec() {
  local pod
  pod=$(kubectl get pods | grep "$1" | head -1 | awk '{print $1}')
  kubectl exec -it "$pod" -- /bin/bash
}

# 函数：实时看指定 label 的日志
klabel() {
  kubectl logs -f -l "app=$1" --all-containers
}
```

### 插件推荐

| 插件 | 安装 | 用途 |
|------|------|------|
| kubectx/kubens | `brew install kubectx` | 快速切换集群和命名空间 |
| stern | `brew install stern` | 多 Pod 日志聚合 |
| kubectl-neat | `kubectl krew install neat` | 导出干净的 YAML |
| kubectl-tree | `kubectl krew install tree` | 树形展示资源关系 |
| kubectl-node-shell | `kubectl krew install node-shell` | 直接进入节点 shell |
| k9s | `brew install k9s` | TUI 界面管理集群 |
| krew | 见官网 | kubectl 插件管理器 |

### 常用一行命令

```bash
# 重启某个 namespace 下所有 Deployment
kubectl rollout restart deployment -n production

# 删除某个 namespace 下所有 Completed 的 Job
kubectl delete jobs --field-selector status.successful=1 -n production

# 查看集群版本
kubectl version --short

# 查看 API 资源列表（支持哪些资源类型）
kubectl api-resources

# 查看 API 版本
kubectl api-versions

# 查看当前用户有权限操作的所有资源
kubectl auth can-i --list

# 强制同步（删除后重新 apply，谨慎使用）
kubectl delete -f deployment.yaml && kubectl apply -f deployment.yaml

# 等待 Deployment 就绪（CI/CD 中使用）
kubectl wait --for=condition=available deployment/my-deployment --timeout=300s

# 等待 Pod 就绪
kubectl wait --for=condition=ready pod -l app=my-app --timeout=120s

# 查看集群节点资源分配汇总
kubectl describe nodes | grep -A 8 "Allocated resources"
```
