---
title: "Pod Security Standards 生产落地：从 PSP 到 PSA 的迁移实战"
date: 2025-11-21T10:00:00+08:00
draft: false
tags: ["Pod Security", "PSA", "PSS", "Kubernetes", "容器安全"]
categories: ["云原生"]
description: "一份从 PSP 迁移到 Pod Security Standards 的实战笔记：对比 Baseline 与 Restricted 两套 profile 的实际约束、Pod Security Admission 的三种 mode、如何一次性迁移 200+ 命名空间、和 Kyverno/OPA 互补使用的最佳实践，以及遗留业务 securityContext 改造的典型模式。"
summary: "一份从 PSP 迁移到 Pod Security Standards 的实战笔记：对比 Baseline 与 Restricted 两套 profile 的实际约束、Pod Security Admission 的三种 mode、如何一次性迁移 200+ 命名空间、和 Kyverno/OPA 互补使用的最佳实践，以及遗留业务 securityContext 改造的典型模式。"
toc: true
math: false
diagram: false
keywords: ["Pod Security Standards", "PSA", "Pod Security Admission", "Kubernetes 安全"]
params:
  reading_time: true
---

## PSP 死了，然后呢

Kubernetes 从 1.25 开始彻底移除了 PodSecurityPolicy（PSP）。我接触过的一大批团队直到 1.28 升级时才意识到这件事，然后陷入一段时间的迷茫——PSP 的替代品 Pod Security Admission（PSA）到底该怎么用？Baseline 和 Restricted 的区别具体是什么？遗留业务跑不了 Restricted 怎么办？升级集群后所有 Pod 都被拒绝怎么办？

这篇文章是我给两个生产集群（加起来 150+ namespace）做完 PSP→PSA 迁移之后的沉淀。基于 **Kubernetes 1.29~1.33** 的实际经验。读完你应该能回答：

1. PSA 到底是什么，和 PSP 有什么本质区别
2. Baseline 和 Restricted 的约束条件具体是什么、怎么影响应用
3. 怎么给一个现有集群做迁移而不把业务搞挂
4. PSA 之外还需要哪些工具配合（Kyverno、准入 Webhook 等）

## 一、从 PSP 到 PSA：认知升级

### 1.1 PSP 的死因

PodSecurityPolicy 是 K8s 1.8 引入的，1.21 deprecated，1.25 删除。整个生命周期不到 7 年。死因：

1. **RBAC 耦合**：你要通过 RBAC 把 "use this PSP" 的权限授予 SA/User，导致"怎么给 Pod 应用 PSP"变得极其复杂。你必须理解 "谁创建了 Pod、用什么 SA、有没有 use 权限" 这个链条。
2. **Mutation 行为意外**：PSP 既能 validate 也能 mutate，很多人不知道 PSP 会偷偷修改你的 Pod spec，调试困难。
3. **选择 PSP 的算法不确定**：多个 PSP 都能 match 时选哪个？靠字母序。这个行为极其反直觉，生产事故频发。
4. **扩展性差**：PSP 不能自定义字段，企业需求只能通过外部 webhook 补。

基本上就是"设计失败，推倒重来"。

### 1.2 PSA 的设计哲学

Pod Security Admission（PSA）从 1.22 引入，1.25 稳定。它的设计明显吸取了 PSP 的教训：

1. **不做 mutation，只做 validation**：PSA 只会拒绝不合规的 Pod，不会修改它。这让行为变得可预测。
2. **不和 RBAC 耦合**：PSA 的约束通过 **namespace label** 生效，不再需要理解"哪个 SA 能 use 哪个 policy"。
3. **只有三套固定 profile**：Privileged、Baseline、Restricted。你不能自定义"微调版" PSA，要精细化就用 Kyverno/OPA 这类通用 policy engine。
4. **每个 namespace 可以独立设置 enforce/audit/warn 三档**，逐级灰度。

本质上 PSA 是"极简版 admission controller"，它只解决一件事——**容器能不能以特权模式运行**——其他事情留给更专业的工具。

### 1.3 三种 profile

**Privileged**：完全无约束，允许 Pod 做任何事情，包括 hostPath、privileged、runAsUser 0、hostNetwork、hostPID 等。系统组件 namespace（kube-system、cilium-system、logging）一般用这个级别。

**Baseline**：阻止已知的特权提升路径，但允许"普通应用"的常见行为。具体禁止：
- 不允许 `hostNetwork: true`
- 不允许 `hostPID: true` / `hostIPC: true`
- 不允许 `privileged: true`
- 不允许 `allowPrivilegeEscalation: true`（显式 true）
- 不允许 `hostPath` 卷（除了特定受控路径）
- 不允许 `capabilities.add` 除 `NET_BIND_SERVICE` 外的任何 Linux capability
- 不允许 SELinux 超出默认类型
- 不允许 AppArmor 自定义 profile 之外的
- 不允许 `/proc/*` 挂载
- 不允许 `sysctls` 非 safe 集合

Baseline 的设计目标是"**95% 的应用应该能直接跑**"。大部分业务 Pod 不需要任何改动就能通过 Baseline。

**Restricted**：更严格，在 Baseline 基础上额外要求：
- 必须 `runAsNonRoot: true`（不能用 root 用户）
- 必须 `seccompProfile` 设置为 RuntimeDefault 或 Localhost
- 必须 drop 所有 capabilities，只允许 add `NET_BIND_SERVICE`
- 必须 `allowPrivilegeEscalation: false`
- 必须 `readOnlyRootFilesystem: true`（推荐但非强制）
- 卷类型只允许安全集合：configMap/secret/emptyDir/projected/PVC/downwardAPI 等

Restricted 的目标是"**生产环境应当追求的安全基线**"。但**强推 Restricted 会炸很多业务**，因为很多镜像里没有 nonroot 用户，很多应用默认写根文件系统。

### 1.4 三种 mode

PSA 支持对同一个 namespace 同时设三种 mode：

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: payments
  labels:
    pod-security.kubernetes.io/enforce: baseline
    pod-security.kubernetes.io/enforce-version: v1.29
    pod-security.kubernetes.io/audit: restricted
    pod-security.kubernetes.io/audit-version: v1.29
    pod-security.kubernetes.io/warn: restricted
    pod-security.kubernetes.io/warn-version: v1.29
```

- **enforce**：违反会被 apiserver 直接拒绝
- **audit**：违反会记录到 audit log，但允许创建
- **warn**：违反会在 `kubectl apply` 返回警告文本，但允许创建

一个典型迁移策略是 "**enforce baseline + audit/warn restricted**"：业务必须达到 Baseline（强制），但鼓励向 Restricted 靠拢（warning 和审计日志让开发看到差距）。

**version 字段**非常重要：它锁定了 "按哪个版本的 PSS 标准"判定。避免集群升级时规则悄悄变化破坏兼容性。我生产都显式写 version。

## 二、迁移策略：不把业务搞挂的前提下收紧

### 2.1 迁移前的评估：Dry Run

在动任何 label 之前，先做一次**全集群 dry run**。原理是给所有 namespace 统一加 `warn=baseline`，然后让开发正常创建 Pod，apiserver 会输出违规警告但不会拒绝。收集 7 天的警告数据，就知道有多少业务会被挡。

但这种方法有个问题——`kubectl` warn 是反馈给创建者的，不会被记录到日志里（1.27 之前）。更好的方案是用 PSA 的 audit 模式，audit log 集中收集：

```yaml
metadata:
  labels:
    pod-security.kubernetes.io/audit: baseline
    pod-security.kubernetes.io/audit-version: latest
```

然后从 kube-apiserver 的 audit log 里提取：

```bash
jq 'select(.annotations."pod-security.kubernetes.io/audit-violations") |
    {user: .user.username, ns: .objectRef.namespace, pod: .objectRef.name,
     violations: .annotations."pod-security.kubernetes.io/audit-violations"}' \
    /var/log/kube-audit.log
```

你会得到类似这样的列表：

```
ns=payments pod=checkout-xxx violations="hostPath volumes are forbidden (volume 'data-dir')"
ns=logging pod=fluentd-yyy violations="hostNetwork=true is forbidden"
ns=monitoring pod=node-exporter-zzz violations="privileged container 'node-exporter'"
```

这是后面修改业务 spec 的清单。

### 2.2 用 Kyverno 批量扫描（更方便）

Kyverno 内置了 PSA policy，可以直接生成违规报告，不用翻 audit log：

```bash
kubectl apply -f https://raw.githubusercontent.com/kyverno/policies/main/pod-security/baseline.yaml
```

然后：

```bash
kubectl get policyreport -A -o custom-columns=NS:.metadata.namespace,NAME:.metadata.name,PASS:.summary.pass,FAIL:.summary.fail
```

或者更好的工具：**kubesec**、**kube-bench** 也能扫。但 Kyverno 的 policyreport 资源形式最工程化。

### 2.3 分层推广

真实环境不可能一天把所有 namespace 都切到 baseline。我的路线：

**第 1 阶段（1 周）**：所有 namespace 加 `audit=baseline`（不 enforce，只记录）。收集违规数据。

**第 2 阶段（2~4 周）**：跟相关业务 team 沟通整改，常见修改：
- 移除不必要的 `hostNetwork`
- 把 hostPath 卷换成 emptyDir 或 PVC
- 移除不必要的 capabilities.add
- 去掉 `privileged: true`

**第 3 阶段（3~4 周）**：干净的 namespace 切到 `enforce=baseline`。先切"非核心业务"和"新建 namespace"，最后才是生产核心业务。核心业务切换必须有回滚预案。

**第 4 阶段（持续）**：`warn=restricted`，让开发看到距离。有余力的业务做 Restricted 改造，改造完成的 namespace 单独 enforce restricted。

### 2.4 例外命名空间

有些 namespace 必须保留 privileged 级别，典型：
- `kube-system`：kube-proxy、coredns 等
- `cilium-system`、`istio-system`、`tigera-operator`：CNI 和 service mesh
- `logging`：fluentd/filebeat 需要 hostPath 读 /var/log
- `monitoring`：node-exporter 需要 hostPID、hostNetwork
- `falco`、`tetragon`：运行时安全工具

这些 namespace 明确打标签为 privileged：

```yaml
labels:
  pod-security.kubernetes.io/enforce: privileged
  pod-security.kubernetes.io/audit: baseline
  pod-security.kubernetes.io/warn: baseline
```

注意即便 privileged，我们依然保留 audit/warn baseline——这样当团队后续优化掉不必要的特权时，审计日志会显示"它其实已经符合 baseline 了"，推动进一步收紧。

## 三、应用整改指南：常见模式

### 3.1 从 root 用户改到 nonroot

很多老镜像默认用 root 跑。改造模式：

**Dockerfile 改造**：

```dockerfile
FROM debian:12-slim
RUN groupadd -r app && useradd -r -g app -u 10001 app
# 应用代码拷贝
COPY --chown=app:app ./app /opt/app
USER app
ENTRYPOINT ["/opt/app/bin/server"]
```

**注意**：
- 明确指定 UID（比如 10001）而不是只用 name，因为 K8s 运行时 nonroot 检查是按 UID 做的
- 文件所有权要改对，否则启动读不了配置
- `/tmp` 这类目录可能需要预创建并 chown

**K8s spec 改造**：

```yaml
spec:
  securityContext:
    runAsNonRoot: true
    runAsUser: 10001
    runAsGroup: 10001
    fsGroup: 10001
  containers:
    - name: app
      image: myapp:v1.2.3
      securityContext:
        allowPrivilegeEscalation: false
        capabilities:
          drop: ["ALL"]
        readOnlyRootFilesystem: true
      volumeMounts:
        - name: tmp
          mountPath: /tmp
        - name: cache
          mountPath: /app/cache
  volumes:
    - name: tmp
      emptyDir: {}
    - name: cache
      emptyDir: {}
```

`readOnlyRootFilesystem: true` 后，应用要写的路径必须挂 emptyDir。常见需要挂的：`/tmp`、日志目录、缓存目录。

### 3.2 绑定低端口

Restricted 不允许 root，但 80/443 这种端口需要 root 才能 bind。解决方案：

1. **应用监听高端口（8080/8443），Service 暴露低端口**。最推荐，改动最小。
2. **Container 加 `NET_BIND_SERVICE` capability**，Restricted 允许 add 这一个 cap：
   ```yaml
   capabilities:
     drop: ["ALL"]
     add: ["NET_BIND_SERVICE"]
   ```
3. **setcap 在 Dockerfile 里**：
   ```dockerfile
   RUN setcap 'cap_net_bind_service=+ep' /opt/app/bin/server
   ```

### 3.3 去除 hostNetwork / hostPort

很多老部署用 hostNetwork 是为了"容器能直接用主机 IP"。现代方案：
- 用 Service (ClusterIP/NodePort/LoadBalancer) 暴露
- 用 HostNetwork=false + hostAliases 做 hostname 解析
- 真的需要广播协议（mDNS 之类）的少数场景保持 privileged

hostPort 在 Baseline 里其实允许（只是不允许 < 1024），但最佳实践是避免。

### 3.4 去除 hostPath

hostPath 是 Baseline 拒绝最多的点。替代方案：
- **日志**：改用 stdout，让 kubelet 收，不要写主机文件系统
- **配置文件**：用 ConfigMap + volumeMount
- **数据库本地存储**：用 local PVC 或 CSI（OpenEBS、Longhorn）
- **必须挂主机路径的**：明确豁免（比如 node-exporter）或者用 csi-hostpath-driver 包装

### 3.5 seccomp profile

Restricted 要求 `seccompProfile.type` 设为 RuntimeDefault 或 Localhost。RuntimeDefault 用容器运行时（containerd / CRI-O）自带的默认 seccomp filter，拦截一批危险 syscall。最简单的做法：

```yaml
securityContext:
  seccompProfile:
    type: RuntimeDefault
```

绝大多数应用不会因为 RuntimeDefault 受影响。少数用了特殊 syscall 的（debugging 工具、某些数据库）会有兼容问题，需要 Localhost 模式加自定义 profile。

## 四、PSA 的局限与补充：Kyverno/OPA

PSA 只能表达"符合 Baseline / Restricted"这种粗粒度约束。生产里有很多更细的需求 PSA 表达不了，比如：

- 禁止用 `latest` tag
- 禁止用 DockerHub 镜像（只允许私有 registry）
- 必须有特定 label (owner、team、环境)
- 必须有 resource request/limit
- 必须有 livenessProbe
- 禁止某些 namespace 使用 LoadBalancer Service

这些就需要 Kyverno 或 OPA Gatekeeper 补位。我的生产架构：

```
Pod 创建请求
     │
     ▼
┌────────────────────┐
│ PSA                │   一道闸门：Baseline/Restricted
│ (内建 admission)    │
└──────┬─────────────┘
       │ passed
       ▼
┌────────────────────┐
│ Kyverno            │   二道闸门：业务规范、标签、资源、镜像源
│ (admission webhook)│
└──────┬─────────────┘
       │ passed
       ▼
┌────────────────────┐
│ Cosign/Policy Ctrl │   三道闸门：镜像签名验证
└──────┬─────────────┘
       │ passed
       ▼
    Pod 创建成功
```

PSA 做"底线"，Kyverno 做"规范"，Policy Controller 做"可信"。三层叠加形成完整的准入体系。

### 4.1 Kyverno 补充策略举例

**禁止 latest tag**：

```yaml
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: disallow-latest-tag
spec:
  validationFailureAction: Enforce
  rules:
    - name: require-image-tag
      match:
        any:
          - resources: { kinds: [Pod] }
      validate:
        message: "使用 latest tag 是不允许的"
        pattern:
          spec:
            containers:
              - image: "!*:latest"
```

**必须有 resource limits**：

```yaml
- name: require-limits
  match:
    any: [{ resources: { kinds: [Pod] }}]
  validate:
    message: "所有容器必须设置 CPU/memory limits"
    pattern:
      spec:
        containers:
          - resources:
              limits:
                memory: "?*"
                cpu: "?*"
```

**只允许特定 registry**：

```yaml
- name: registry-allowlist
  match:
    any: [{ resources: { kinds: [Pod] }}]
  validate:
    message: "只允许从 registry.example.com 拉镜像"
    pattern:
      spec:
        containers:
          - image: "registry.example.com/*"
```

### 4.2 Kyverno 性能与选型

Kyverno 的准入延迟大约 5~20ms，正常情况无感。但如果策略多且 background scan 频繁，Kyverno controller 的内存会膨胀。我们 1500 node 集群 Kyverno 占用 2GB~4GB 内存，给够就行。

OPA Gatekeeper 的 CEL 支持更灵活但语法学习曲线陡，新团队推荐 Kyverno。有 Rego 积累的老团队可以继续用 Gatekeeper。

## 五、踩坑记录

### 5.1 kube-system 被误设 enforce baseline

**事故**：某次迁移脚本写错，把所有 namespace（包括 kube-system）都打了 `enforce=baseline` label。结果 coredns 的下一个滚动更新失败（coredns 使用了 `NET_BIND_SERVICE` 但配置不对），集群 DNS 挂了 15 分钟。

**教训**：
1. 迁移脚本必须**显式 exclude 系统 namespace**
2. label 批量操作要有 dry-run 模式
3. kube-system 永远保持 privileged

修复后的迁移脚本：

```bash
SKIP_NS="kube-system kube-public kube-node-lease cilium-system istio-system monitoring logging falco"
for ns in $(kubectl get ns -o jsonpath='{.items[*].metadata.name}'); do
  if echo "$SKIP_NS" | grep -qw "$ns"; then
    echo "skip $ns"
    continue
  fi
  kubectl label ns "$ns" --overwrite \
    pod-security.kubernetes.io/audit=baseline \
    pod-security.kubernetes.io/audit-version=v1.29
done
```

### 5.2 StatefulSet 滚动失败

给一个 namespace 切到 `enforce=baseline` 之后，StatefulSet 的 rollout 卡住。原因是旧 Pod spec 里有 `hostPath`，PSA 拒绝新 Pod 创建。问题：**PSA 不会拒绝已存在的 Pod，只拒绝新建**，所以旧 Pod 还在跑，看起来一切正常，直到滚动更新才爆发。

**教训**：切 enforce 之前必须先 audit 一轮，修复所有违规**再切**。不能"切了再修"。

### 5.3 pause container 触发 runAsNonRoot

部分 CNI（早期版本的 Istio CNI、某些旧 sidecar injector）的 init container 用了 root，Restricted 直接拒绝。Istio 后续版本修了这个问题。**迁移前检查所有 sidecar 和 init container**。

### 5.4 PSA 版本字段漂移

没写 `enforce-version` 的 namespace，K8s 升级后 PSA 会自动按新版本的 PSS 标准判定。新版本可能引入新约束，原本通过的 Pod 突然被拒绝。

修复：所有 label 明确写 version：

```yaml
pod-security.kubernetes.io/enforce-version: v1.29
```

升级集群前修改到新版本，验证后再升。

### 5.5 kubectl warn 被忽略

`warn=restricted` 会在 `kubectl apply` 时输出警告，但 CI 流水线通常把 stderr 丢弃，开发看不到警告。解决：

1. CI 里专门 grep kubectl stderr 检查 "Warning:"
2. 或者用 `kubectl apply --validate=strict` + `--server-side` 更严格校验

### 5.6 Helm chart 默认值不符合 Baseline

许多社区 Helm chart 的默认值在 Baseline 下跑不了（比如 hostNetwork: true 或者 privileged: true）。常见踩坑：
- `prometheus-node-exporter` 默认 hostNetwork + hostPID，只能 privileged
- 某些数据库 chart 默认写 `/var/lib/data` hostPath
- 某些监控 agent 默认 `privileged: true`

这些 chart 要么放进 privileged namespace，要么修改 values 关掉不必要的特权。

## 六、Restricted 的现实：到底能不能做

聊到这里一定有人问：**我们到底应该追求 Baseline 还是 Restricted**？

我的真实观点：

- **新项目默认 Restricted**。从第一天就要求 nonroot + readOnlyRootFilesystem + drop all caps。改造成本最低。
- **存量项目默认 Baseline**。Restricted 改造成本对老业务太高，性价比低。除非有合规硬要求（等保、ISO27001 某些控制项），否则 Baseline 就够。
- **核心数据面做 Restricted**。payments、user-data、auth 这种敏感服务，花成本改 Restricted 是值的。
- **基础设施可以 Privileged**。别硬啃。

Restricted 本身不是终点，它只是 PSS 定义的"推荐级别"。再往上还有 seccomp 自定义 profile、AppArmor profile、gVisor 沙箱等更严的层次，那些是 PSS 没有涵盖的。

## 七、工具与可观测

### 7.1 审计日志聚合

PSA 的 audit 违规通过 kube-apiserver audit log 输出。必须把 audit log 收集到 Loki 或者 ELK，否则你根本看不到违规情况。

apiserver 配置：

```yaml
- --audit-log-path=/var/log/kube-audit/audit.log
- --audit-log-maxage=7
- --audit-log-maxbackup=10
- --audit-log-maxsize=100
- --audit-policy-file=/etc/kubernetes/audit-policy.yaml
```

audit-policy 里保留 `metadata` 级别即可：

```yaml
rules:
  - level: Metadata
    resources:
      - group: ""
        resources: ["pods"]
```

### 7.2 Grafana dashboard

写一个 dashboard 追踪：

- 每个 namespace 的 PSA audit 违规数（按 level）
- Top 违规规则（hostPath / privileged / runAsNonRoot）
- 新建但被 enforce 拒绝的 Pod 速率
- 从 warn 升级到 enforce 的 namespace 进度

LogQL 查询示例：

```
sum by (namespace) (
  count_over_time({job="kube-audit"}
    | json
    | annotations_pod_security_kubernetes_io_audit_violations != ""
    [1h])
)
```

### 7.3 定期扫描脚本

定期跑一次全集群 PSS 扫描，发报告：

```bash
#!/bin/bash
kubectl get ns -o json | jq -r '.items[] | 
  .metadata.name + "," +
  (.metadata.labels["pod-security.kubernetes.io/enforce"] // "none") + "," +
  (.metadata.labels["pod-security.kubernetes.io/warn"] // "none")' > pss-status.csv
```

推到 Slack 或者钉钉每周回顾，看 baseline/restricted 覆盖率是不是在提升。

## 八、落地路线总结

把整个流程梳理一遍作为一个可执行 checklist：

**Week 1**: 
- 所有 namespace 加 `audit=baseline` label
- 收集 audit log 违规
- Kyverno 部署（用于扫描而非 enforce）
- 出一份"违规清单"给各业务 team

**Week 2-4**: 
- 跟 team 对齐改造方案
- 每个违规建 Jira ticket 跟踪
- 系统 namespace 打 privileged
- 改造完成的 namespace 预切 `enforce=baseline`

**Week 5-8**: 
- 滚动推广 enforce baseline 到所有业务 namespace
- 开启 `warn=restricted` 让开发看到差距
- Kyverno 补充规则（registry 白名单、tag、resource limits）

**Month 3+**: 
- 新项目模板强制 Restricted
- 核心业务逐个做 Restricted 改造
- 定期扫描 + 违规回顾
- 和 Cosign、Falco、Cilium 联动形成完整 admission 链

## 九、结语

Pod Security Standards 是一个看似简单但涉及面很广的话题。它只是两套 profile 定义，真正考验的是**你怎么把它落进一个有几十个 team、几百个服务的真实生产环境里**。迁移过程中你会发现运维、安全、开发的大量协作问题比技术问题更难。

从 PSP 到 PSA 的这次换代也给我一个反思：**K8s 的原生安全能力其实是在逐步退缩的**，PSA 比 PSP 简化了很多，剩下的精细化需求交给社区 policy engine（Kyverno/OPA）。这个趋势未来应该会继续——K8s 内核只保留最小必要的安全原语，生态补充上层策略。作为运维，你需要同时理解原生能力的边界和社区工具的定位。

下一篇我会深入讲 Kyverno 的 policy as code 实践，那是上面"二道闸门"的完整玩法。
