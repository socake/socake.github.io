---
title: "Prometheus 进程监控：process-exporter 实战与告警配置"
date: 2026-04-11T07:00:00+08:00
draft: false
tags: ["Prometheus", "process-exporter", "监控", "运维", "可观测性"]
categories: ["可观测性"]
description: "通过 process-exporter 实现裸机和 VM 上的进程级监控，涵盖部署配置、核心指标采集、告警规则设计以及常见踩坑总结。"
summary: "K8s 有完善的 Pod 监控体系，但裸机和 VM 上运行的进程如何监控？本文介绍 process-exporter 的部署与配置实践，覆盖进程组匹配、核心指标、告警规则设计及实际踩坑经验。"
toc: true
math: false
diagram: false
series: ["可观测性实战"]
keywords: ["process-exporter", "Prometheus", "进程监控", "告警", "DaemonSet", "Linux"]
params:
  reading_time: true
---

## 为什么需要进程级监控

在 K8s 集群里，Prometheus 通过 kube-state-metrics 和 cAdvisor 能采集到丰富的 Pod 状态和容器资源指标。但实际运维中总有一些场景超出这个范畴：

- **节点上的系统进程**：kubelet、containerd、chronyd、sshd 这些进程不以容器形式运行，Pod 监控覆盖不到它们。如果 kubelet 崩溃了，节点会进入 NotReady 状态，但你在第一时间收到的是节点告警而不是进程告警，定位慢。
- **裸机或 VM 上的自建服务**：etcd 用二进制部署在裸机上、nginx 跑在物理机上、老旧的 Java 服务没有容器化——这些场景到处都有。
- **进程异常重启检测**：容器的 restart count 可以监控，但裸机进程被 systemd 拉起后重启计数是隐藏的，process-exporter 能暴露进程的启动时间，可以推算出重启频率。

node-exporter 只能给出节点整体的 CPU/内存/磁盘，无法区分哪个进程在消耗资源。process-exporter 填补了这个空白，它读取 `/proc` 文件系统，按配置的规则对进程分组，暴露每组进程的 CPU、内存、线程、文件描述符、IO 等指标。

---

## process-exporter 配置详解

process-exporter 的配置文件是 YAML 格式，核心是 `process_names` 字段，定义需要监控哪些进程以及如何分组。

### 进程名模板

每个分组都需要指定一个 `name` 模板，决定在 Prometheus 指标中如何标识这个组。可用的模板变量：

| 模板变量 | 来源 | 说明 |
|---|---|---|
| `{{.Comm}}` | `/proc/<pid>/stat` | 可执行文件原始名，最多 15 个字符 |
| `{{.ExeBase}}` | `/proc/<pid>/exe` | 可执行文件名（默认值） |
| `{{.ExeFull}}` | `/proc/<pid>/exe` | 可执行文件完整路径 |
| `{{.Username}}` | `/proc/<pid>/status` | 运行进程的用户名 |
| `{{.Matches}}` | cmdline 正则匹配结果 | 包含所有正则捕获组，**推荐使用** |
| `{{.PID}}` | — | 进程 PID，不推荐（每次重启会变） |
| `{{.StartTime}}` | — | 进程启动时间，不推荐用于分组 |

**推荐使用 `{{.Matches}}`**，原因是它基于 `cmdline` 正则匹配结果，组名稳定、语义清晰。

### 基础配置示例

```yaml
process_names:
  # 监控 nginx 主进程
  - name: "{{.Matches}}"
    cmdline:
      - 'nginx'

  # 监控 etcd，用具名捕获组让组名更可读
  - name: "etcd"
    cmdline:
      - 'etcd'

  # 监控所有 java 进程（统一归组）
  - name: "java-app"
    cmdline:
      - 'java.*-jar'

  # 监控 sshd
  - name: "{{.Matches}}"
    cmdline:
      - 'sshd'
```

### K8s 场景下的完整配置

在 K8s 集群中用 ConfigMap 管理配置，监控节点上的关键系统进程：

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: process-exporter-config
  namespace: monitoring
data:
  process-exporter-config.yaml: |-
    process_names:
    - name: "{{.Matches}}"
      cmdline:
      - 'kubelet'
    - name: "{{.Matches}}"
      cmdline:
      - 'containerd'
    - name: "{{.Matches}}"
      cmdline:
      - 'etcd'
    - name: "{{.Matches}}"
      cmdline:
      - 'chronyd'
    - name: "{{.Matches}}"
      cmdline:
      - 'sshd'
    - name: "{{.Matches}}"
      cmdline:
      - 'nginx'
```

---

## DaemonSet 部署

process-exporter 需要读取宿主机的 `/proc` 目录，所以必须以 DaemonSet 部署，并且需要将宿主机 `/proc` 挂载进容器。

```yaml
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: process-exporter
  namespace: monitoring
  labels:
    app: process-exporter
spec:
  selector:
    matchLabels:
      app: process-exporter
  template:
    metadata:
      labels:
        app: process-exporter
      annotations:
        prometheus.io/scrape: "true"
        prometheus.io/port: "9256"
    spec:
      hostPID: true
      hostNetwork: true
      nodeSelector:
        kubernetes.io/os: linux
      tolerations:
        - operator: Exists  # 允许调度到所有节点，包括 master
      containers:
        - name: process-exporter
          image: ncabatoff/process-exporter:0.7.10
          args:
            - -config.path=/config/process-exporter-config.yaml
          ports:
            - name: metrics
              containerPort: 9256
              hostPort: 9256
          resources:
            requests:
              cpu: 10m
              memory: 20Mi
            limits:
              cpu: 200m
              memory: 200Mi
          securityContext:
            runAsNonRoot: true
            runAsUser: 65534
          volumeMounts:
            - name: proc
              mountPath: /proc
              readOnly: true
            - name: config
              mountPath: /config
      volumes:
        - name: proc
          hostPath:
            path: /proc
        - name: config
          configMap:
            name: process-exporter-config
```

几个关键配置说明：

- `hostPID: true`：允许容器看到宿主机的所有进程，否则只能看到自己的 PID namespace 内的进程
- `hostNetwork: true`：使用宿主机网络，metrics 端口直接绑定到节点 IP，Prometheus 用节点 IP 采集
- `tolerations: - operator: Exists`：容忍所有污点，确保 master 节点也被监控

### Prometheus 采集配置

利用 K8s 服务发现自动发现所有节点的 process-exporter：

```yaml
- job_name: 'process-exporter'
  scrape_interval: 60s
  scrape_timeout: 30s
  kubernetes_sd_configs:
    - role: node
  relabel_configs:
    - source_labels: [__address__]
      regex: '(.*):10250'
      replacement: '${1}:9256'
      target_label: __address__
      action: replace
    - action: labelmap
      regex: __meta_kubernetes_node_label_(.+)
    - source_labels: [__meta_kubernetes_node_address_InternalIP]
      action: replace
      target_label: node_ip
```

这里通过 relabel 把采集地址从 10250（kubelet）替换为 9256（process-exporter），同时保留节点标签方便过滤。

---

## 核心指标解析

process-exporter 暴露的所有指标都以 `namedprocess_namegroup_` 开头，`groupname` label 对应配置中的进程组名。

### 进程存活与状态

```
# 进程数量，值为 0 说明进程已死
namedprocess_namegroup_num_procs{groupname="..."} 1

# 各状态进程数（Running/Sleeping/Other/Zombie）
namedprocess_namegroup_states{groupname="...", state="Sleeping"} 1
```

`num_procs == 0` 是最直接的进程消失检测指标。

### CPU 使用

```
# 用户态和内核态 CPU 时间（Counter）
namedprocess_namegroup_cpu_seconds_total{groupname="...", mode="user"} 123.4
namedprocess_namegroup_cpu_seconds_total{groupname="...", mode="system"} 45.6
```

通过 `rate(namedprocess_namegroup_cpu_seconds_total[5m])` 可以得到 CPU 使用率。

### 内存使用

```
# 物理内存（RSS）和虚拟内存（VSZ）
namedprocess_namegroup_memory_bytes{groupname="...", memtype="resident"} 104857600
namedprocess_namegroup_memory_bytes{groupname="...", memtype="virtual"} 2147483648
namedprocess_namegroup_memory_bytes{groupname="...", memtype="swapped"} 0
```

### 文件描述符

```
# 当前打开的文件描述符数
namedprocess_namegroup_open_filedesc{groupname="..."} 128
```

FD 泄漏是线上服务的常见问题，这个指标能提前预警。

### 线程与 IO

```
# 线程数
namedprocess_namegroup_thread_count{groupname="..."} 16

# 磁盘读写（Counter）
namedprocess_namegroup_read_bytes_total{groupname="..."} 1048576
namedprocess_namegroup_write_bytes_total{groupname="..."} 524288
```

---

## 告警规则设计

### 进程消失告警

最基础也最重要的告警：

```yaml
groups:
  - name: process-alerts
    rules:
      # 关键进程消失
      - alert: ProcessNotRunning
        expr: namedprocess_namegroup_num_procs == 0
        for: 2m
        labels:
          severity: critical
        annotations:
          summary: "节点 {{ $labels.node_ip }} 进程 {{ $labels.groupname }} 已停止"
          description: "进程组 {{ $labels.groupname }} 在节点 {{ $labels.node_ip }} 上运行数量为 0，持续超过 2 分钟"

      # Zombie 进程过多（可能是父进程泄漏）
      - alert: ZombieProcessTooMany
        expr: namedprocess_namegroup_states{state="Zombie"} > 5
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "节点 {{ $labels.node_ip }} 存在过多 Zombie 进程"
          description: "进程组 {{ $labels.groupname }} 有 {{ $value }} 个 Zombie 进程"
```

### 内存超阈值告警

```yaml
      # 进程内存超过 2GB（以 Java 服务为例）
      - alert: ProcessMemoryTooHigh
        expr: |
          namedprocess_namegroup_memory_bytes{memtype="resident", groupname=~"java.*"}
          > 2 * 1024 * 1024 * 1024
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "进程 {{ $labels.groupname }} 内存使用过高"
          description: "节点 {{ $labels.node_ip }} 上 {{ $labels.groupname }} RSS 为 {{ $value | humanize }}B"
```

### 文件描述符超限告警

```yaml
      # FD 使用超过 1000（根据 ulimit 调整阈值）
      - alert: ProcessFdTooMany
        expr: namedprocess_namegroup_open_filedesc > 1000
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "进程 {{ $labels.groupname }} 文件描述符数量过高"
          description: "节点 {{ $labels.node_ip }} 上 {{ $labels.groupname }} 打开了 {{ $value }} 个 FD，存在泄漏风险"
```

### CPU 持续高占用告警

```yaml
      # 进程 CPU 使用率超过 80% 持续 10 分钟
      - alert: ProcessCpuTooHigh
        expr: |
          rate(namedprocess_namegroup_cpu_seconds_total[5m]) > 0.8
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "进程 {{ $labels.groupname }} CPU 使用率过高"
          description: "节点 {{ $labels.node_ip }} 上 {{ $labels.groupname }} CPU 使用率为 {{ $value | humanizePercentage }}"
```

---

## 实际场景：监控关键基础设施进程

### 监控 etcd

etcd 是 K8s 的大脑，它的健康状态至关重要。除了 etcd 自带的 metrics 之外，用 process-exporter 可以从 OS 层面补充监控：

- `num_procs == 0`：etcd 进程已退出
- `thread_count` 异常增长：goroutine 泄漏
- `open_filedesc` 接近 `ulimit -n`：文件描述符耗尽前预警
- `memory_bytes{memtype="resident"}` 持续上涨：内存泄漏

### 监控 nginx

nginx 采用 master + worker 多进程模型，`num_procs` 会大于 1（1 个 master + N 个 worker）。告警规则应该用 `< 2` 而不是 `== 0`，因为 master 进程死了但 worker 还活着时 num_procs 也不是 0。

```yaml
- alert: NginxMasterNotRunning
  expr: namedprocess_namegroup_num_procs{groupname=~".*nginx.*"} < 2
  for: 1m
  labels:
    severity: critical
```

### 监控 Java 服务

Java 进程的特点是线程数多、内存占用大，需要重点监控：

1. 内存增长趋势：`deriv(namedprocess_namegroup_memory_bytes{memtype="resident"}[1h]) > 0` 持续为正说明有泄漏
2. 线程数：正常 Java 服务线程数在几十到几百，突然涨到几千说明有问题
3. GC 导致 CPU 飙升：结合 JVM metrics 和 process CPU 指标综合判断

---

## 踩坑记录

### 进程名匹配失败

**现象**：配置了 `cmdline: ['nginx']`，但指标里没有出现 nginx 的数据。

**原因**：`cmdline` 字段做的是正则匹配，而且匹配的是 `/proc/<pid>/cmdline` 的完整命令行，包括参数。如果 nginx 以 `nginx: master process /usr/sbin/nginx -g daemon off;` 运行，那 `nginx` 这个字符串确实能匹配上。但如果进程名被截断（某些系统上 `/proc/<pid>/comm` 只有 15 个字符），用 `{{.Comm}}` 可能拿不到完整名字。

**解法**：使用 `cmdline` 正则匹配而不是依赖 `{{.Comm}}`，并且测试时先手动读取 `/proc/<pid>/cmdline` 确认真实的命令行内容：

```bash
cat /proc/$(pgrep nginx | head -1)/cmdline | tr '\0' ' '
```

### systemd service 与进程名的关系

systemd 拉起的服务，进程名不一定和 service 名一致。比如 `systemctl status docker` 管的进程实际名字是 `dockerd`，`systemctl status containerd` 的进程名是 `containerd`。

建议的做法：先 `ps aux | grep <service-keyword>` 确认实际进程名，再写 `cmdline` 规则。

### 一个进程只能属于一个组

process-exporter 的规则是从上到下匹配，第一个匹配的规则生效，后续规则不再处理同一个进程。如果有进程被多个规则都能匹配，只会被第一个规则归组。规则顺序很重要，越具体的规则放越前面。

### hostPID 缺失导致看不到进程

如果忘记配置 `hostPID: true`，process-exporter 只能看到自己容器内的进程，metrics 里只有 process-exporter 自身，没有其他进程数据。这个错误比较隐蔽，因为 exporter 本身是正常运行的。

### scrape_timeout 要小于 scrape_interval

进程数量多的节点，process-exporter 的 `/metrics` 响应比较慢，默认 10s 的 `scrape_timeout` 可能不够。建议：

```yaml
- job_name: 'process-exporter'
  scrape_interval: 60s
  scrape_timeout: 30s  # 给足时间
```

---

## Grafana Dashboard

process-exporter 官方提供了 Dashboard 模板，直接在 Grafana 中导入 ID **249** 即可使用，包含进程状态、CPU、内存、线程、IO 等面板，开箱即用。

如果需要自定义，关键 PromQL 参考：

```
# CPU 使用率
sum(rate(namedprocess_namegroup_cpu_seconds_total[5m])) by (groupname, node_ip)

# 内存使用（MB）
namedprocess_namegroup_memory_bytes{memtype="resident"} / 1024 / 1024

# FD 使用率（假设 ulimit 是 65535）
namedprocess_namegroup_open_filedesc / 65535 * 100
```

进程级监控和节点监控、Pod 监控形成三层覆盖，能大幅提升裸机环境下的故障发现速度。
