---
title: "Prometheus 服务发现深度解析：kubernetes_sd_configs 实战"
date: 2026-04-11T11:00:00+08:00
draft: false
tags: ["Prometheus", "服务发现", "Kubernetes", "可观测性", "运维"]
categories: ["可观测性"]
description: "深入解析 Prometheus kubernetes_sd_configs 五种角色的工作原理，结合生产实践讲解 relabel_configs 的正确用法，以及 ServiceMonitor 和原生 scrape config 的取舍。"
summary: "在 K8s 环境里手动维护 Prometheus scrape targets 是不现实的，kubernetes_sd_configs 配合 relabel_configs 是解决这个问题的核心机制。本文从原理到实践，把这套体系讲透。"
toc: true
math: false
diagram: false
series: ["可观测性实战"]
keywords: ["Prometheus", "kubernetes_sd_configs", "relabel_configs", "ServiceMonitor", "服务发现"]
params:
  reading_time: true
---

接手一个 K8s 集群的监控工作时，最头疼的不是 Prometheus 本身，而是怎么让它自动发现集群里几十上百个服务的 metrics endpoint。手动配置 `static_configs` 是死路——Pod 重启 IP 就变了，新服务上线要改配置文件再重新 reload，这不是监控应该有的样子。

`kubernetes_sd_configs` 就是为解决这个问题而生的。但它的配置学习曲线比较陡，`relabel_configs` 写错了可能导致所有 targets 都被 drop 掉，或者抓到一堆没用的 endpoint。这篇文章从我踩过的坑出发，把这套机制讲清楚。

## Pull 模型与服务发现的关系

Prometheus 是 pull 模型：由 Prometheus 主动去拉取 targets 的 metrics，而不是 target 主动推送给 Prometheus。

这在静态环境里没什么问题，但在 K8s 里，Pod 的 IP 随时在变，服务实例数也在弹性伸缩。如果还用 `static_configs` 写死 IP，运维成本会非常高。

`kubernetes_sd_configs` 的解法是让 Prometheus 直接对接 K8s API Server，实时获取集群里的资源列表，自动构建 targets。当 Pod 重启、Service 变更时，Prometheus 会自动更新 targets，无需人工干预。

这个机制的前提是 Prometheus 有权限调用 K8s API，所以 RBAC 配置是第一步。

## RBAC 权限配置

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: prometheus
  namespace: monitoring
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: prometheus
rules:
  - apiGroups: [""]
    resources:
      - nodes
      - nodes/proxy
      - nodes/metrics
      - services
      - endpoints
      - pods
      - ingresses
      - configmaps
    verbs: ["get", "list", "watch"]
  - apiGroups: ["extensions", "networking.k8s.io"]
    resources:
      - ingresses
    verbs: ["get", "list", "watch"]
  # 访问 kubelet metrics 需要这个
  - nonResourceURLs: ["/metrics", "/metrics/cadvisor"]
    verbs: ["get"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: prometheus
subjects:
  - kind: ServiceAccount
    name: prometheus
    namespace: monitoring
roleRef:
  kind: ClusterRole
  name: prometheus
  apiGroup: rbac.authorization.k8s.io
```

**踩坑**：最开始只配了 namespace-scoped Role，结果发现 Prometheus 只能发现 `monitoring` namespace 里的资源，其他 namespace 的 Pod 全部看不到。必须用 ClusterRole + ClusterRoleBinding。

## 五种角色详解

`kubernetes_sd_configs` 支持五种 `role`，每种角色对应不同的 K8s 资源类型，发现的对象和携带的元数据标签也不同。

### role: node

发现集群中的所有 Node，每个 Node 对应一个 target。适合抓取 Node Exporter、kubelet 指标。

默认的 `__address__` 是节点 IP + 端口 10250（kubelet 端口），通常需要 relabel 成 metrics 端口：

```yaml
- job_name: 'kubernetes-nodes'
  scheme: https
  tls_config:
    ca_file: /var/run/secrets/kubernetes.io/serviceaccount/ca.crt
    insecure_skip_verify: true
  bearer_token_file: /var/run/secrets/kubernetes.io/serviceaccount/token
  
  kubernetes_sd_configs:
    - role: node
  
  relabel_configs:
    # 把节点名作为 instance 标签
    - source_labels: [__meta_kubernetes_node_name]
      target_label: node
    # 把节点上的所有标签转成 Prometheus 标签
    - action: labelmap
      regex: __meta_kubernetes_node_label_(.+)
```

抓取 Node Exporter（假设 daemonset 用 hostNetwork，跑在 9100 端口）：

```yaml
- job_name: 'node-exporter'
  kubernetes_sd_configs:
    - role: node
  
  relabel_configs:
    # 把 __address__ 的端口改成 9100
    - source_labels: [__address__]
      regex: '(.*):10250'
      replacement: '${1}:9100'
      target_label: __address__
    - source_labels: [__meta_kubernetes_node_name]
      target_label: node
```

### role: pod

发现集群中的所有 Pod。这是最灵活的一种，可以通过 annotation 精细控制哪些 Pod 需要被抓取。

常用元数据标签：
- `__meta_kubernetes_namespace`：Pod 所在 namespace
- `__meta_kubernetes_pod_name`：Pod 名称
- `__meta_kubernetes_pod_ip`：Pod IP
- `__meta_kubernetes_pod_label_<labelname>`：Pod 标签
- `__meta_kubernetes_pod_annotation_<annotationname>`：Pod annotation
- `__meta_kubernetes_pod_container_name`：容器名
- `__meta_kubernetes_pod_container_port_number`：容器端口号

**通过 annotation 控制抓取的标准模式**：

```yaml
- job_name: 'kubernetes-pods'
  kubernetes_sd_configs:
    - role: pod
  
  relabel_configs:
    # 只抓取有 prometheus.io/scrape: "true" annotation 的 Pod
    - source_labels: [__meta_kubernetes_pod_annotation_prometheus_io_scrape]
      action: keep
      regex: "true"
    
    # 支持自定义 metrics path（默认 /metrics）
    - source_labels: [__meta_kubernetes_pod_annotation_prometheus_io_path]
      action: replace
      target_label: __metrics_path__
      regex: (.+)
    
    # 支持自定义端口（默认用 Pod 第一个端口）
    - source_labels: [__address__, __meta_kubernetes_pod_annotation_prometheus_io_port]
      action: replace
      regex: '([^:]+)(?::\d+)?;(\d+)'
      replacement: '$1:$2'
      target_label: __address__
    
    # 支持 http/https scheme 切换
    - source_labels: [__meta_kubernetes_pod_annotation_prometheus_io_scheme]
      action: replace
      target_label: __scheme__
      regex: (https?)
    
    # 把所有 Pod 标签转成 Prometheus 标签
    - action: labelmap
      regex: __meta_kubernetes_pod_label_(.+)
    
    # 添加 namespace 和 pod 标签
    - source_labels: [__meta_kubernetes_namespace]
      target_label: namespace
    - source_labels: [__meta_kubernetes_pod_name]
      target_label: pod
    - source_labels: [__meta_kubernetes_pod_container_name]
      target_label: container
```

这样，只需要在 Pod（或 Deployment template）的 annotation 里加：

```yaml
annotations:
  prometheus.io/scrape: "true"
  prometheus.io/port: "8080"
  prometheus.io/path: "/actuator/prometheus"
```

Prometheus 就会自动发现并抓取这个 Pod 的 metrics。

### role: service

发现集群中的所有 Service，target 地址是 Service 的 ClusterIP + 端口。适合做黑盒探测（HTTP 健康检查、TCP 检查）。

```yaml
- job_name: 'kubernetes-services-blackbox'
  metrics_path: /probe
  params:
    module: [http_2xx]
  
  kubernetes_sd_configs:
    - role: service
  
  relabel_configs:
    # 只探测有特定 annotation 的 Service
    - source_labels: [__meta_kubernetes_service_annotation_prometheus_io_probe]
      action: keep
      regex: "true"
    
    # 把 Service 地址作为探测目标
    - source_labels: [__address__]
      target_label: __param_target
    
    # Blackbox Exporter 地址
    - target_label: __address__
      replacement: blackbox-exporter:9115
    
    - source_labels: [__param_target]
      target_label: instance
    
    - source_labels: [__meta_kubernetes_namespace]
      target_label: namespace
    - source_labels: [__meta_kubernetes_service_name]
      target_label: service
```

### role: endpoints

发现 Service 背后的 Endpoint，每个 Endpoint 对应一个 target。这是最常用的角色之一，因为它结合了 Service 和 Pod 的元数据。

如果一个 Service 有 3 个 Pod，endpoints 角色会产生 3 个 targets，而 service 角色只产生 1 个。

```yaml
- job_name: 'kubernetes-service-endpoints'
  kubernetes_sd_configs:
    - role: endpoints
  
  relabel_configs:
    # 只抓取有 annotation 的 Service（Endpoint 会继承 Service 的 annotation）
    - source_labels: [__meta_kubernetes_service_annotation_prometheus_io_scrape]
      action: keep
      regex: "true"
    
    # 自定义 scheme
    - source_labels: [__meta_kubernetes_service_annotation_prometheus_io_scheme]
      action: replace
      target_label: __scheme__
      regex: (https?)
    
    # 自定义 path
    - source_labels: [__meta_kubernetes_service_annotation_prometheus_io_path]
      action: replace
      target_label: __metrics_path__
      regex: (.+)
    
    # 自定义端口
    - source_labels: [__address__, __meta_kubernetes_service_annotation_prometheus_io_port]
      action: replace
      target_label: __address__
      regex: '([^:]+)(?::\d+)?;(\d+)'
      replacement: '$1:$2'
    
    - action: labelmap
      regex: __meta_kubernetes_service_label_(.+)
    
    - source_labels: [__meta_kubernetes_namespace]
      target_label: namespace
    - source_labels: [__meta_kubernetes_service_name]
      target_label: service
    - source_labels: [__meta_kubernetes_pod_name]
      target_label: pod
```

### role: ingress

发现集群中的所有 Ingress 规则，每个 path 对应一个 target。主要用于对 HTTP 服务做外部可达性探测。

## relabel_configs 核心用法

relabel 是 Prometheus 服务发现最复杂也最强大的部分，弄错了会导致 target 全部丢失或者标签错乱。

### 五种常用 action

| action | 用途 |
|--------|------|
| `keep` | 只保留匹配 regex 的 targets |
| `drop` | 丢弃匹配 regex 的 targets |
| `replace` | 用 replacement 替换 target_label 的值 |
| `labelmap` | 把匹配 regex 的标签名批量重命名 |
| `labeldrop` | 删除匹配 regex 的标签 |
| `labelkeep` | 只保留匹配 regex 的标签 |

### 关键特殊标签

- `__address__`：target 的抓取地址（host:port），最终的请求地址
- `__metrics_path__`：metrics endpoint 路径，默认 `/metrics`
- `__scheme__`：协议，`http` 或 `https`，默认 `http`
- `__scrape_interval__`：抓取间隔
- 所有以 `__` 开头的标签在抓取完成后会被删除，不会出现在时序数据里

### 多字段拼接

`source_labels` 可以指定多个标签，它们会用 `;` 拼接后再匹配 regex：

```yaml
# 把 IP 和自定义端口拼成新的 __address__
- source_labels: [__address__, __meta_kubernetes_pod_annotation_prometheus_io_port]
  action: replace
  # __address__ 是 ip:port 格式，我们要替换掉端口
  # 分组1：IP 部分（非:的字符，后面可以跟 :数字 但不捕获）
  # 分组2：annotation 里的端口
  regex: '([^:]+)(?::\d+)?;(\d+)'
  replacement: '$1:$2'
  target_label: __address__
```

这个 regex 初看很费解，拆开来看：
- `([^:]+)`：捕获 IP 部分（不含冒号）
- `(?::\d+)?`：可能存在的原始端口（不捕获）
- `;`：source_labels 多字段的分隔符
- `(\d+)`：捕获 annotation 里配置的端口

### labelmap 批量重命名

把所有 K8s Pod 标签转成 Prometheus 标签：

```yaml
- action: labelmap
  regex: __meta_kubernetes_pod_label_(.+)
```

这会把 `__meta_kubernetes_pod_label_app` 变成 `app`，`__meta_kubernetes_pod_label_version` 变成 `version`，以此类推。括号里的捕获组就是新标签名。

## ServiceMonitor vs 原生 scrape config

如果使用 kube-prometheus-stack（Prometheus Operator），会引入 ServiceMonitor 这个 CRD，提供更高层的抽象。

### ServiceMonitor 示例

```yaml
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: my-service
  namespace: monitoring
  labels:
    # Prometheus Operator 会根据这个 label 来选择 ServiceMonitor
    release: prometheus
spec:
  # 选择哪些 namespace 的 Service
  namespaceSelector:
    any: true
  # 选择哪些 Service（通过 label）
  selector:
    matchLabels:
      app: my-service
  endpoints:
    - port: http-metrics
      path: /metrics
      interval: 30s
      # 如果需要 TLS
      scheme: https
      tlsConfig:
        insecureSkipVerify: true
```

### 取舍分析

ServiceMonitor 的优点：
- 声明式，和 K8s 资源风格一致
- 不需要改 Prometheus 配置文件，不需要 reload
- Prometheus Operator 自动处理服务发现和认证

ServiceMonitor 的缺点：
- 依赖 Prometheus Operator，增加运维复杂度
- 调试时不如直接看 Prometheus 配置直观
- 对于非标准的 scrape 场景（如修改 `__address__`），写起来比原生 relabel 麻烦

我的经验：如果整个监控体系基于 kube-prometheus-stack，就全用 ServiceMonitor，统一管理。如果是自建 Prometheus，原生 `kubernetes_sd_configs` 配合 relabel 更灵活，调试也更容易（Prometheus UI 的 /targets 页面可以直观看到 relabel 的效果）。

## 实际场景：发现所有带 annotation 的 Pod

这是最通用的一种配置，可以直接用在生产环境：

```yaml
scrape_configs:
  - job_name: 'kubernetes-pods-with-annotation'
    kubernetes_sd_configs:
      - role: pod
        # 只在特定 namespace 发现（可选）
        namespaces:
          names:
            - production
            - staging
    
    relabel_configs:
      # Step 1: 过滤，只保留需要抓取的 Pod
      - source_labels: [__meta_kubernetes_pod_annotation_prometheus_io_scrape]
        action: keep
        regex: "true"
      
      # Step 2: 过滤掉 Pending/Succeeded/Failed 状态的 Pod
      - source_labels: [__meta_kubernetes_pod_phase]
        action: drop
        regex: (Pending|Succeeded|Failed|Unknown)
      
      # Step 3: 替换 metrics path
      - source_labels: [__meta_kubernetes_pod_annotation_prometheus_io_path]
        action: replace
        regex: (.+)
        target_label: __metrics_path__
      
      # Step 4: 替换端口
      - source_labels: [__address__, __meta_kubernetes_pod_annotation_prometheus_io_port]
        action: replace
        regex: '([^:]+)(?::\d+)?;(\d+)'
        replacement: '$1:$2'
        target_label: __address__
      
      # Step 5: 把 Pod labels 转成 Prometheus 标签
      - action: labelmap
        regex: __meta_kubernetes_pod_label_(.+)
      
      # Step 6: 添加常用标签
      - source_labels: [__meta_kubernetes_namespace]
        target_label: namespace
      - source_labels: [__meta_kubernetes_pod_name]
        target_label: pod
      - source_labels: [__meta_kubernetes_pod_node_name]
        target_label: node
      - source_labels: [__meta_kubernetes_pod_container_name]
        target_label: container
```

## 踩坑记录

**坑1：relabel 顺序陷阱**

relabel 规则是顺序执行的，前面规则修改了 label，后面规则看到的是修改后的值。

典型错误：先用 `replace` 修改了 `__address__`，后面又想基于原始 `__address__` 做别的操作，但此时 `__address__` 已经是新值了。

解法：如果需要保留原始值，先 `replace` 把它存到另一个临时标签里：

```yaml
# 先把原始 address 存起来
- source_labels: [__address__]
  target_label: __tmp_original_address

# 然后再修改 __address__
- source_labels: [__address__, __meta_kubernetes_pod_annotation_prometheus_io_port]
  action: replace
  regex: '([^:]+)(?::\d+)?;(\d+)'
  replacement: '$1:$2'
  target_label: __address__
```

**坑2：__address__ 被意外覆盖**

症状：所有 target 的地址都变成了空字符串或者错误的地址。

原因：replace action 的 regex 没匹配到时，会把 `target_label` 设置为空字符串。如果 `target_label: __address__`，就会把抓取地址清空。

解法：给 replace 加上非空检查，只在有值的时候才替换：

```yaml
# 只在 annotation 存在时才替换端口
- source_labels: [__address__, __meta_kubernetes_pod_annotation_prometheus_io_port]
  action: replace
  regex: '([^:]+)(?::\d+)?;(\d+)'  # 注意：(\d+) 需要 annotation 存在才能匹配
  replacement: '$1:$2'
  target_label: __address__
# 如果 annotation 不存在，regex 不匹配，__address__ 保持不变
```

**坑3：RBAC 权限不足导致 target 列表为空**

症状：Prometheus 的 /targets 页面显示 0 个 targets，或者 service discovery 页面看到错误。

排查命令：

```bash
# 检查 Prometheus Pod 日志
kubectl logs -n monitoring deployment/prometheus -c prometheus | grep -i "error\|forbidden"

# 验证 ServiceAccount 权限
kubectl auth can-i list pods --as=system:serviceaccount:monitoring:prometheus -A
kubectl auth can-i list services --as=system:serviceaccount:monitoring:prometheus -A
```

**坑4：labelmap 把内部标签暴露出去**

症状：时序数据里出现了很多 `__meta_` 开头的标签（正常情况下这些标签应该在抓取后被删除）。

原因：regex 写太宽泛，把 `__meta_` 标签也匹配进去了：

```yaml
# 错误写法：会把所有以 _ 开头的标签都转换
- action: labelmap
  regex: _(.+)

# 正确写法：只转换 __meta_kubernetes_pod_label_ 前缀的标签
- action: labelmap
  regex: __meta_kubernetes_pod_label_(.+)
```

---

掌握 `kubernetes_sd_configs` 之后，K8s 监控的目标发现就不再是手动配置的体力活了。关键是理解 relabel 的执行顺序和各个特殊标签的语义，调试时善用 Prometheus UI 的 `/service-discovery` 和 `/targets` 页面，可以实时看到 relabel 后的标签结果。
