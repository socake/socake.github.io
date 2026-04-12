---
title: "学习路线图"
description: "DevOps / SRE / AI工程化 三条成长路径，每个知识点都有掌握标准"
showDate: false
showReadingTime: false
---

本站整理了三条适合运维/后端工程师的成长路径，每条路径来源于真实工作经验。**路线图是主干**，定义了每个知识点你需要达到的掌握标准；**站内文章是叶子**，帮你在实践中达到该标准。

路径并不互斥：SRE 与 DevOps 高度重叠，AI 工程化可作为任意路径的延伸。

---

## 路径一：DevOps 工程师

**目标受众**：传统运维/系统管理员，向 DevOps / 平台工程方向转型。

**核心目标**：从"手工运维"转向"工程化运维"——能独立搭建 CI/CD 平台、设计云原生基础设施、用代码管理一切。

---

### 阶段一：工具链基础（1–3 个月）

打好地基，否则后续所有高级话题都会卡壳。顺序建议：Linux → Docker → Shell → Git → 网络。

---

#### Linux 系统管理

> **是什么**：操作系统层面的核心能力——进程、文件系统、权限、网络、systemd 服务管理。
>
> **为什么学**：所有服务跑在 Linux 上，容器底层也是 Linux。不懂 Linux，线上出问题你只能猜。
>
> **掌握标准**：
> - 能用 `ps/top/htop/lsof` 定位异常进程并分析其资源占用
> - 能理解和修改文件权限、`/proc` 文件系统、ulimit 参数
> - 能用 `journalctl/systemctl` 管理 systemd 服务，看懂 service 启动失败的日志
> - 能用 `sar/vmstat/iostat` 定位 CPU 飙高、内存泄漏、IO 等待的根因
> - 能写 `/etc/sysctl.conf` 调整内核参数并解释每个参数的含义
>
> 📖 **深入阅读**：[Linux 性能调优实战：CPU、内存、IO、网络](/posts/linux-performance-tuning/)

---

#### Docker 与容器化

> **是什么**：将应用和依赖打包为镜像，用容器运行时隔离进程的技术。
>
> **为什么学**：现代应用交付的基础单元是容器。不会 Docker，你无法进入 K8s 时代。
>
> **掌握标准**：
> - 能写多阶段 Dockerfile，镜像体积控制在合理范围（Go 应用 < 50MB，Python 应用 < 200MB）
> - 能解释 Layer Cache 机制，并优化构建缓存命中率
> - 能用 Docker Compose 编排多服务本地开发环境，包括网络和 Volume 配置
> - 能用 `docker inspect/stats/exec` 排查运行中容器的问题
> - 理解 namespace 和 cgroup 是容器隔离的底层机制
>
> 📖 **深入阅读**：[Docker 最佳实践：从 Dockerfile 到生产部署](/posts/docker-best-practices/)

---

#### Shell 脚本自动化

> **是什么**：用 Bash 脚本将重复手工操作变成可复用的自动化任务。
>
> **为什么学**：运维有大量重复操作（备份、巡检、部署检查），不自动化就是在低效内耗。
>
> **掌握标准**：
> - 能写带参数解析、错误处理（`set -euo pipefail`）、日志输出的生产级 Shell 脚本
> - 能用 `cron` 和 `systemd timer` 设置定时任务，理解两者的区别
> - 能用 `awk/sed/grep/jq` 处理文本和 JSON 数据
> - 能写带重试逻辑的轮询脚本（等待服务就绪、检查 HTTP 状态码等）
> - 脚本出错时不会无声退出，能正确捕获并上报异常
>
> 📖 **深入阅读**：[Shell 脚本自动化：运维任务工程化](/posts/shell-script-automation/)

---

#### Git 工作流

> **是什么**：版本控制系统，以及围绕它建立的团队协作规范。
>
> **为什么学**：代码、配置、IaC 都应该在 Git 里。Git 用不好会导致协作混乱、变更无法追溯。
>
> **掌握标准**：
> - 能解释 GitFlow 和 Trunk-based Development 的适用场景和取舍
> - 能用 `rebase -i` 整理提交历史，用 `cherry-pick` 选择性合并
> - 能处理复杂 merge conflict，理解 3-way merge 的原理
> - 能设计 `.gitignore`，理解 submodule vs subtree 的区别
> - 能写 pre-commit hook 做代码格式和安全检查
>
> 📖 **深入阅读**：[Git 工作流实践：团队协作与分支管理](/posts/git-workflow-practice/)

---

#### 基础网络与 Nginx

> **是什么**：TCP/IP、DNS、HTTP 协议基础，以及 Nginx 作为流量入口的配置。
>
> **为什么学**：90% 的线上故障都和网络有关。不懂网络，连 ping 不通和端口不通都分不清楚。
>
> **掌握标准**：
> - 能用 `tcpdump/wireshark` 抓包，读懂 TCP 三次握手和四次挥手
> - 能通过 `ss/netstat` 分析连接状态，定位 TIME_WAIT 堆积、端口占用问题
> - 能配置 Nginx 反向代理、负载均衡、SSL 终止，并调优 `worker_processes/keepalive` 参数
> - 能解释 HTTP/1.1 vs HTTP/2 的区别，理解 `Connection: keep-alive` 的作用
> - 能从 DNS 解析、TCP 连接、HTTP 响应三个层面排查连接超时问题
>
> 📖 **深入阅读**：
> - [Nginx 运维完全指南：反向代理、负载均衡与调优](/posts/nginx-ops-complete/)
> - [TCP 网络故障排查实战：从抓包到根因定位](/posts/tcp-network-troubleshooting/)

---

#### 阶段一完成检验

**场景题**：线上 Java 服务突然响应变慢，P99 延迟从 100ms 升到 3s。没有代码变更，只是流量增加了 2 倍。请描述你的排查思路，并指出你会在哪些层面用哪些命令定位根因。

> 参考思路提示：CPU 使用率 → JVM GC → 数据库连接池 → 网络 I/O → 系统调用

---

### 阶段二：容器编排与 CI/CD（3–6 个月）

进入容器编排和流水线核心地带。建议先把 K8s 基础吃透，再学 Helm，最后把 CI/CD 和 GitOps 联动起来。

---

#### Kubernetes 核心

> **是什么**：容器编排平台，负责调度、自愈、扩缩容、服务发现。
>
> **为什么学**：K8s 是云原生基础设施的标准。不懂 K8s，你无法管理现代微服务应用。
>
> **掌握标准**：
> - 能描述一个 Pod 从 `kubectl apply` 到运行的完整生命周期（API Server → etcd → Scheduler → Kubelet）
> - 能设计合理的 `resources.requests/limits`，解释 QoS 分类（Guaranteed/Burstable/BestEffort）
> - 能配置 HPA，理解 `targetAverageUtilization` 的计算方式
> - 能排查 CrashLoopBackOff、Pending、ImagePullBackOff 等常见故障状态
> - 能设计 `Liveness/Readiness/Startup` 三种探针，避免探针误判导致的频繁重启
> - 能用 `kubectl top/describe/logs/exec` 全面诊断服务问题
>
> 📖 **深入阅读**：[Kubernetes 入门指南：核心概念与快速上手](/posts/kubernetes-beginner-guide/)

---

#### Helm 工程化

> **是什么**：K8s 的包管理工具，用 Chart 模板化应用配置，支持多环境管理。
>
> **为什么学**：手写 K8s YAML 不可维护。Helm 让配置模板化、版本化、可复用。
>
> **掌握标准**：
> - 能从零创建 Helm Chart，包括 `values.yaml` 分层设计（公共值 + 环境覆盖）
> - 能用 `helm template/lint/diff` 在部署前验证渲染结果
> - 能管理 Chart 依赖（`Chart.yaml dependencies`），理解子 Chart 值覆盖规则
> - 能用 `Hooks`（pre-install/post-upgrade）处理数据库迁移等有序操作
> - 能排查 Helm Release 状态异常（Pending-upgrade/Failed）并正确回滚
>
> 📖 **深入阅读**：[Helm 工程化实践：从 Chart 开发到多环境管理](/posts/helm-engineering-practice/)

---

#### CI/CD 流水线

> **是什么**：从代码提交到生产部署的自动化流水线，包括构建、测试、推送镜像、触发部署。
>
> **为什么学**：手工部署是事故温床。CI/CD 让发布变得可预期、可回滚、有审计记录。
>
> **掌握标准**：
> - 能设计覆盖 lint → test → build → push → deploy 的完整流水线
> - 能实现蓝绿部署和金丝雀发布的流水线逻辑
> - 能实现制品版本管理：镜像 tag 策略（commit hash / semver）
> - 能配置流水线缓存（依赖缓存、Docker layer 缓存）降低构建时间
> - 能在流水线中集成安全扫描（镜像漏洞扫描、SAST）
>
> 📖 **深入阅读**：[CI/CD 流水线设计：从代码提交到生产部署](/posts/cicd-pipeline-design/)

---

#### Prometheus + Grafana 监控

> **是什么**：指标采集（Prometheus）+ 可视化告警（Grafana）的监控组合。
>
> **为什么学**：服务跑起来只是第一步，没有监控你不知道它跑得好不好。
>
> **掌握标准**：
> - 能写 PromQL 查询：rate、histogram_quantile、label_replace 等核心函数
> - 能设计 Recording Rules 优化高频查询性能
> - 能写告警规则，理解 `for` 持续时间和 `severity` 分级的设计考量
> - 能用 Grafana 构建包含 SLI 指标的 Dashboard，设置合理的变量和刷新间隔
> - 能配置 ServiceMonitor/PodMonitor（Prometheus Operator 模式）实现自动发现
>
> 📖 **深入阅读**：[Prometheus + Grafana：监控体系从零搭建](/posts/prometheus-grafana/)

---

#### 阶段二完成检验

**场景题**：你负责将一个单体 Java 应用迁移到 K8s，要求：零停机部署、多环境配置管理（dev/staging/prod）、自动扩缩容、部署失败自动回滚。请设计整套方案，包括 Helm Chart 结构、流水线步骤、HPA 配置思路。

---

### 阶段三：平台工程与高级实践（6–12 个月）

掌握大规模集群管理与平台工程能力。这一阶段的知识点联系紧密，建议按顺序推进：GitOps → Karpenter → Istio → IaC → 平台工程。

---

#### GitOps 与 ArgoCD

> **是什么**：以 Git 为唯一事实来源，通过 CD 工具将 Git 状态同步到集群。ArgoCD 是主流实现。
>
> **为什么学**：GitOps 解决了"谁在什么时候部署了什么"的审计问题，也是多集群管理的基础。
>
> **掌握标准**：
> - 能用 ApplicationSet 实现多集群、多环境的统一部署管理
> - 能配置 Sync Policy（自动同步 vs 手动同步）和 Sync Waves 控制部署顺序
> - 能设计 GitOps 目录结构（base + overlays / 按集群/按环境组织）
> - 能排查 ArgoCD OutOfSync 状态，区分"资源变更"和"drift"
> - 能实现 Image Updater 自动更新镜像 tag，触发 GitOps 流程
>
> 📖 **深入阅读**：[GitOps 与 ArgoCD：声明式部署的完整实践](/posts/gitops-argocd/)

---

#### Karpenter 节点自动扩缩

> **是什么**：下一代 K8s 节点自动扩缩器，直接调用云 API 创建最优节点，比 Cluster Autoscaler 更快更灵活。
>
> **为什么学**：节点成本是 K8s 集群最大的开销。Karpenter 合理配置可降低节点成本 40–60%。
>
> **掌握标准**：
> - 能设计 NodePool 和 EC2NodeClass，定义机型族、架构、Spot/On-demand 比例
> - 能配置 Disruption 策略（Drift、Consolidation、Expiration）并理解各自的副作用
> - 能用 `karpenter.sh/capacity-type` 标签控制工作负载的节点调度策略
> - 能排查 Karpenter 无法扩容的常见原因（Quota 不足、IAM 权限、SG 配置等）
> - 能估算 Consolidation 对服务稳定性的影响并设置合理的 PDB
>
> 📖 **深入阅读**：
> - [Karpenter 深度解析：Kubernetes 节点自动扩缩实战](/posts/karpenter-deep-dive/)
> - [K8s 成本优化实战：从资源治理到弹性降本](/posts/k8s-成本优化实战/)

---

#### Istio 服务网格

> **是什么**：在 K8s 上运行的服务网格，通过 Sidecar 代理实现流量管理、mTLS、可观测性。
>
> **为什么学**：微服务间的流量治理（金丝雀、熔断、重试）不应该写死在代码里，网格层统一处理。
>
> **掌握标准**：
> - 能配置 VirtualService 实现金丝雀发布（按权重/Header 路由）
> - 能启用 mTLS PeerAuthentication 并验证服务间通信加密
> - 能用 Kiali 分析服务拓扑，定位延迟异常的调用链
> - 能配置 DestinationRule 设置熔断（连接池限制、异常点检测）
> - 能排查 Envoy Sidecar 注入失败、证书轮换问题
>
> 📖 **深入阅读**：[Istio 服务网格实战：流量管理与可观测性](/posts/istio-service-mesh-practice/)

---

#### IaC（OpenTofu / Terraform）

> **是什么**：用代码声明基础设施资源（VPC、RDS、EKS 等），状态文件追踪实际资源。
>
> **为什么学**：手工点云控制台不可复现、不可审计、容易出错。IaC 让基础设施像代码一样被版本化管理。
>
> **掌握标准**：
> - 能用模块化设计拆分大型 Terraform 工程（network/compute/database 分离）
> - 能配置 remote backend（S3 + DynamoDB 锁）并解释 state locking 的意义
> - 能用 `plan/apply/destroy` 的完整工作流，在 CI 中集成 Terraform Lint 和 Plan Review
> - 能处理 state drift（手工资源被 Terraform 管理）和 import 已有资源
> - 能设计 Workspace 或目录结构区分多环境（dev/staging/prod）
>
> 📖 **深入阅读**：[OpenTofu/Terraform 实践：基础设施即代码](/posts/opentofu-terraform-practice/)

---

#### 安全供应链与合规

> **是什么**：容器镜像漏洞扫描（Trivy）、镜像签名（Cosign）、K8s 准入控制策略（OPA/Kyverno）。
>
> **为什么学**：安全是平台工程不可忽视的维度。合规要求越来越严，提前建立体系远比事后补救代价低。
>
> **掌握标准**：
> - 能在 CI 流水线中集成 Trivy 扫描，设置阻断策略（Critical 漏洞不允许部署）
> - 能用 Cosign 对镜像签名并在 Kyverno 策略中验证签名
> - 能写 Kyverno ClusterPolicy 强制要求 resources.limits、非 root 运行、禁止特权容器
> - 能用 Vault + External Secrets Operator 管理 K8s Secret，替代明文存储
>
> 📖 **深入阅读**：
> - [Trivy + Cosign：容器供应链安全实战](/posts/trivy-cosign-supply-chain/)
> - [OPA/Kyverno 策略即代码：Kubernetes 合规治理实战](/posts/opa-kyverno-admission-control/)
> - [Vault + External Secrets：K8s 密钥管理实践](/posts/vault-external-secrets/)

---

#### 平台工程

> **是什么**：构建内部开发者平台（IDP），为应用团队提供标准化的"黄金路径"（脚手架、部署、监控、日志一键就绪）。
>
> **为什么学**：平台工程是 DevOps 的下一阶段演进。好的 IDP 能让 10 个运维工程师支撑 200 个开发者。
>
> **掌握标准**：
> - 能描述 CNCF 平台工程参考架构，解释 Portal/Pipeline/Infrastructure 三层分工
> - 能用 Backstage 或类似工具搭建开发者自助服务入口
> - 能定义"黄金路径"模板，让新服务一键接入监控、日志、CI/CD
> - 能用 DORA 指标（部署频率、变更前置时间、故障恢复时间、变更失败率）衡量平台效果
>
> 📖 **深入阅读**：[平台工程实践：构建内部开发者平台](/posts/platform-engineering-practice/)

---

#### 阶段三完成检验

**场景题**：公司有 3 个 AWS 区域，每个区域 2 个 EKS 集群（staging/prod），共 6 个集群。30 个微服务团队各自管理部署。现在需要统一治理：部署规范执行、成本可视化、多集群发布协调、Secret 安全管理。请设计整体方案，说明每个工具的职责划分。

**预计总时间**：9–15 个月（视基础和投入时间而定）

---

## 路径二：SRE 可靠性工程师

**目标受众**：有一定运维基础，希望专注于系统稳定性、故障处理和可靠性工程。

**核心目标**：建立系统化的可靠性思维——能设计 SLO 体系、主导故障排查、推动混沌工程落地，让稳定性成为可度量、可驱动的工程目标。

---

### 阶段一：可靠性思维基础（1–3 个月）

先建立 SRE 认知框架，再学可观测性工具。顺序很重要：概念 → 监控 → 性能调优。

---

#### SRE 核心理念

> **是什么**：Google 提出的站点可靠性工程方法论，核心是用软件工程的方法解决运维问题。
>
> **为什么学**：SRE 提供了一套让"稳定性"可量化、可驱动的方法论，是告别救火模式的认知基础。
>
> **掌握标准**：
> - 能解释 SLI（指标）、SLO（目标）、SLA（协议）三者的关系和区别
> - 能解释 Error Budget 的概念：可用性 = 1 - Error Budget 消耗速率
> - 能区分 Toil（重复手工工作）和工程工作，并给出降低 Toil 的具体方案
> - 能描述 Google SRE 的"错误预算策略"：Error Budget 耗尽时冻结功能发布
> - 能解释为什么 SRE 不追求 100% 可用性，以及 99.9% vs 99.99% 的实际代价差异
>
> 📖 **深入阅读**：[SRE 概念与原则：从 Google SRE 到工程实践](/posts/sre-concepts-and-principles/)

---

#### 可观测性三支柱

> **是什么**：Metrics（指标）、Logs（日志）、Traces（链路追踪）三种数据类型的综合运用。
>
> **为什么学**：只有监控指标，你知道服务慢了，但不知道哪里慢。三支柱联动才能快速定位根因。
>
> **掌握标准**：
> - 能描述三种数据类型各自擅长回答的问题（What/Why/Where）
> - 能用 Exemplar 将 Metrics 异常点关联到具体的 Trace ID
> - 能设计告警策略：Metrics 触发告警 → Traces 定位调用链 → Logs 查详细上下文
> - 能评估现有系统的可观测性成熟度，识别盲区（什么情况下三支柱都无法定位问题）
>
> 📖 **深入阅读**：[可观测性三支柱：Metrics、Logs、Traces 体系化实践](/posts/observability-three-pillars/)

---

#### Prometheus 监控实战

> **是什么**：时序指标采集和告警系统，Pull 模型，强大的 PromQL 查询语言。
>
> **为什么学**：Prometheus 是 K8s 生态的监控事实标准，SRE 必须精通。
>
> **掌握标准**：
> - 能写 PromQL 计算服务 P50/P99 延迟、错误率、吞吐量（SLI 核心指标）
> - 能用 `histogram_quantile` 正确计算分位数，理解其精度限制
> - 能设计 Error Budget 燃烧率告警（`burn rate > 14.4` 触发紧急告警）
> - 能配置 Alertmanager 路由树：按 severity 分级，按团队路由，避免告警风暴
> - 能评估 Prometheus vs VictoriaMetrics 的适用场景（数据量、查询复杂度、高可用要求）
>
> 📖 **深入阅读**：
> - [Prometheus + Grafana：监控体系从零搭建](/posts/prometheus-grafana/)
> - [Prometheus 进程监控：Process Exporter 完整实践](/posts/prometheus-process-monitoring/)
> - [VictoriaMetrics：Prometheus 的高性能替代方案](/posts/victoriametrics-prometheus/)

---

#### Linux 性能调优

> **是什么**：系统层面的性能诊断能力，覆盖 CPU、内存、IO、网络四个维度。
>
> **为什么学**：应用层问题经常根因在系统层。不会性能分析，你只能治标不治本。
>
> **掌握标准**：
> - 能用 `perf top/record/report` 定位 CPU 热点，理解火焰图的读法
> - 能区分 Minor Page Fault 和 Major Page Fault，判断是否存在内存换页压力
> - 能用 `iostat -x` 分析磁盘瓶颈（%util、await、r/s、w/s）
> - 能用 `ss -s` 分析 TCP 连接状态分布，判断是否存在连接池耗尽
> - 能用 `strace/ltrace` 追踪系统调用，定位 "D state" 进程卡在哪里
>
> 📖 **深入阅读**：[Linux 性能调优实战：CPU、内存、IO、网络](/posts/linux-performance-tuning/)

---

#### 阶段一完成检验

**场景题**：你的服务 SLO 是 99.9% 可用性（每月 Error Budget = 43.8 分钟）。本月已消耗 38 分钟。产品经理要求这周发布一个高风险功能变更。你作为 SRE 如何决策？请给出具体的决策框架和沟通方案。

---

### 阶段二：故障排查与告警体系（3–6 个月）

先掌握方法论再学工具，否则容易陷入"会用工具但不知道什么时候用"的困境。

---

#### 故障排查方法论

> **是什么**：系统化的故障定位框架，包括 USE Method、RED Method、5 Whys、故障树分析。
>
> **为什么学**：凭直觉排查容易走弯路，方法论让你在压力下也能有条不紊地定位根因。
>
> **掌握标准**：
> - 能用 USE Method（Utilization/Saturation/Errors）对系统资源做全面体检
> - 能用 RED Method（Rate/Errors/Duration）诊断微服务的外部可见性能
> - 能主持故障复盘（Post-Mortem），写出包含时间线、根因、预防措施的 RCA 文档
> - 能区分"症状"和"根因"，避免只处理表象的错误倾向
> - 能在 15 分钟内完成初步定界（网络/应用/数据库/基础设施）
>
> 📖 **深入阅读**：
> - [故障排查方法论：系统化定位与根因分析](/posts/故障排查方法论/)
> - [故障排查实录：Terway IP 泄漏问题全程复盘](/posts/故障排查-terway-ip泄漏/)

---

#### 告警体系设计

> **是什么**：从告警规则设计、分级路由、降噪策略到 on-call 轮班的完整告警体系。
>
> **为什么学**：告警太多 = 告警疲劳 = 真正的故障被淹没。好的告警体系让工程师只被值得起床的事叫醒。
>
> **掌握标准**：
> - 能设计三级告警体系（P1 紧急/P2 重要/P3 提醒）并定义各级响应 SLA
> - 能用告警分组、路由树、抑制规则减少告警风暴
> - 能区分"症状告警"（用户可感知）和"原因告警"（内部指标），解释为什么症状告警优先
> - 能设计 on-call 轮班制度，包括 escalation policy 和 runbook 链接
> - 能量化告警质量：Alert Fatigue Rate、MTTA（Mean Time to Acknowledge）
>
> 📖 **深入阅读**：
> - [告警体系设计：从告警风暴到精准通知](/posts/告警体系设计/)
> - [Alertmanager Webhook API：自定义告警接收与处理](/posts/alertmanager-webhook-api/)

---

#### SLO 落地实践

> **是什么**：将抽象的"可靠性目标"转化为具体的 SLI 指标、SLO 数值、Error Budget 告警规则。
>
> **为什么学**：没有 SLO，稳定性改进是无法驱动的。SLO 把"服务要稳定"变成"Error Budget 消耗 < X%"的可量化目标。
>
> **掌握标准**：
> - 能为不同类型服务（HTTP API / 消息队列 / 批处理任务）选择合适的 SLI 定义方式
> - 能配置基于 Error Budget 燃烧率的多窗口告警（1h/6h/24h/3d）
> - 能用 PromQL 计算 Error Budget 剩余百分比并在 Grafana 上可视化
> - 能在 Error Budget 耗尽时启动 Freeze（冻结发布）流程并与产品团队沟通
>
> 📖 **深入阅读**：[SLO/SLI/Error Budget 实战：从理论到落地](/posts/slo-sli-error-budget-practice/)

---

#### 混沌工程

> **是什么**：通过主动注入故障（网络延迟、节点宕机、CPU 压力）验证系统韧性，在真实故障前发现薄弱点。
>
> **为什么学**：只有测试过，你才知道系统真的能抗住故障。等真实故障来测试代价太高。
>
> **掌握标准**：
> - 能描述混沌工程的四个原则（稳态假设、实验最小爆炸半径、生产环境验证、自动化）
> - 能用 Chaos Mesh 设计 PodChaos/NetworkChaos/StressChaos 实验
> - 能在实验前定义可观测的"爆炸半径"和回滚条件
> - 能将混沌实验集成进 CI/CD 流水线做回归验证
> - 能设计 GameDay 演练，让多团队参与故障响应演练
>
> 📖 **深入阅读**：[Chaos Mesh 混沌工程实战：系统韧性验证](/posts/chaos-mesh-practice/)

---

#### 阶段二完成检验

**场景题**：凌晨 2 点，你收到告警：某核心支付接口错误率从 0.1% 突增到 8%，已持续 5 分钟。你没有收到任何代码变更通知。请描述接下来 30 分钟内的完整响应动作，包括你会看哪些指标、执行哪些命令、如何判断是否需要回滚。

---

### 阶段三：大规模可靠性治理（6–12 个月）

从单集群排障走向多集群体系化治理，建立可扩展的可靠性工程能力。

---

#### 多集群运维

> **是什么**：多个 K8s 集群的统一管理、统一监控、统一发布协调。
>
> **为什么学**：单集群是不够的：跨区高可用、环境隔离、合规要求都需要多集群。但多集群带来了新的运维复杂性。
>
> **掌握标准**：
> - 能设计多集群监控聚合方案（Thanos/VictoriaMetrics 联邦查询）
> - 能用 ArgoCD ApplicationSet 统一管理跨集群应用部署
> - 能设计跨集群服务发现和流量路由（Submariner、Istio 多集群）
> - 能制定多集群 Kubernetes 升级策略（滚动升级、金丝雀节点升级）
> - 能设计跨集群 Backup/Restore 方案（Velero）
>
> 📖 **深入阅读**：[多集群 K8s 管理：联邦、统一观测与运维实践](/posts/multi-cluster-k8s-management/)

---

#### 高级可观测性：ELK 与链路追踪

> **是什么**：日志管道（Filebeat → Logstash → Elasticsearch → Kibana）与分布式追踪（OpenTelemetry）的深度实践。
>
> **为什么学**：Metrics 告诉你系统状态，Logs 告诉你发生了什么，Traces 告诉你为什么慢。三者缺一不可。
>
> **掌握标准**：
> - 能设计高吞吐日志管道（10 万 EPS），合理设置 Logstash 线程和 Batch Size
> - 能写 Elasticsearch DSL 查询（Bool/Range/Aggregation），用 Kibana 构建运维 Dashboard
> - 能用 OpenTelemetry SDK 给应用插桩，实现跨服务链路追踪
> - 能根据 Trace 火焰图定位具体的慢函数调用
>
> 📖 **深入阅读**：
> - [Filebeat → Logstash 日志管道：高吞吐采集架构](/posts/filebeat-logstash-pipeline/)
> - [Elasticsearch DSL 查询实战：从入门到聚合分析](/posts/elasticsearch-dsl-query/)
> - [Kibana 可视化指南：运维 Dashboard 构建实战](/posts/kibana-visualization-guide/)

---

#### K8s 安全加固

> **是什么**：RBAC 权限体系、网络策略、Pod 安全标准、零信任网络的综合安全实践。
>
> **为什么学**：K8s 默认配置不安全。权限过松会导致横向移动攻击，网络不隔离会导致东西向渗透。
>
> **掌握标准**：
> - 能设计最小权限 RBAC 体系：按角色定义 ClusterRole，用 RoleBinding 限制命名空间
> - 能配置 NetworkPolicy 实现命名空间隔离和服务间白名单
> - 能用 `kubectl auth can-i` 验证权限，用 `rakkess` 可视化权限矩阵
> - 能解释 Pod Security Standards（Privileged/Baseline/Restricted）三级区别
>
> 📖 **深入阅读**：
> - [Kubernetes RBAC 安全实践：权限体系设计](/posts/kubernetes-rbac-security/)
> - [零信任网络实践：从概念到 K8s 环境落地](/posts/零信任网络实践/)

---

#### 阶段三完成检验

**场景题**：你负责 6 个 K8s 集群（2 个区域 × 3 环境）的 SRE 工作，团队 3 人。现在要建立一套从"发现故障"到"修复故障"的完整流程，要求：MTTA < 5 分钟，MTTR < 30 分钟，每季度至少做一次 GameDay 演练。请设计整套 SRE 工程体系。

**预计总时间**：10–18 个月

---

## 路径三：AI 工程化实践

**目标受众**：运维/后端工程师，希望将 AI 能力引入工程实践，或转型 AI 工程化方向。

**核心目标**：从零掌握大模型应用开发，能独立设计 RAG 系统、AI Agent，并将 AI 融入运维工作流（AIOps）。

---

### 阶段一：大模型基础与 API 开发（1–2 个月）

建立认知底座，快速上手 API 开发。顺序：概念 → Prompt Engineering → API 开发，不要反过来。

---

#### 大模型核心概念

> **是什么**：Transformer 架构、Token、上下文窗口、Temperature 等大模型运作的基础原理。
>
> **为什么学**：不理解底层概念，你无法解释模型为什么"幻觉"，也无法设计出稳定可靠的 AI 应用。
>
> **掌握标准**：
> - 能解释 Token 的概念，估算一段文本的 Token 数量，理解 Token 与成本的关系
> - 能解释 Temperature 和 Top-P 对输出多样性的影响，知道什么场景用低/高 Temperature
> - 能描述上下文窗口的工作原理，解释为什么"超长上下文不等于无限记忆"
> - 能区分 Prompt Tokens 和 Completion Tokens，计算 API 调用成本
> - 能解释为什么大模型会"幻觉"，以及 RAG 如何缓解这个问题
>
> 📖 **深入阅读**：
> - [LLM 核心概念：大语言模型原理与工程师必知基础](/posts/llm-core-concepts/)
> - [大模型全景 2026：主流模型横评与工程选型指南](/posts/llm-landscape-2025/)

---

#### Prompt Engineering

> **是什么**：设计高质量提示词的系统方法，包括角色设定、Few-shot 示例、Chain-of-Thought、结构化输出。
>
> **为什么学**：同样的模型，不同的 Prompt 质量差距巨大。Prompt Engineering 是 AI 应用质量的杠杆点。
>
> **掌握标准**：
> - 能用 System Prompt 明确定义 AI 的角色、能力边界、输出格式
> - 能用 Few-shot 示例提升特定任务的准确率（代码生成、信息提取、分类任务）
> - 能用 Chain-of-Thought（思维链）提升复杂推理任务的准确性
> - 能用 JSON Schema 约束 AI 输出结构，实现可程序化处理的结构化响应
> - 能识别和防御 Prompt Injection 攻击
>
> 📖 **深入阅读**：[Prompt Engineering 完全指南：从入门到高级技巧](/posts/prompt-engineering-guide/)

---

#### API 开发实战

> **是什么**：直接调用 Claude / OpenAI / Gemini 等模型 API 开发 AI 功能，包括流式输出、工具调用、上下文管理。
>
> **为什么学**：LangChain 等框架封装太厚，生产问题难以调试。理解裸 API 调用是一切的基础。
>
> **掌握标准**：
> - 能实现流式输出（Server-Sent Events），正确处理流中断和重连
> - 能实现 Tool Use（函数调用）：定义工具 Schema，处理多轮工具调用循环
> - 能实现多轮对话的上下文管理：sliding window、摘要压缩等策略
> - 能实现请求重试（指数退避）、速率限制处理、超时控制
> - 能估算并控制每次对话的 Token 消耗，设计合理的截断策略
>
> 📖 **深入阅读**：
> - [Claude API 开发实战：从入门到生产级应用](/posts/claude-api-development-guide/)
> - [OpenAI API 工程实践：生产级应用开发指南](/posts/openai-api-engineering/)

---

#### Python 异步编程

> **是什么**：asyncio 异步编程模型，在 AI 应用中处理高并发 API 调用的核心技术。
>
> **为什么学**：AI 应用的瓶颈往往是 API 调用的 I/O 等待。异步并发可以将吞吐量提升 5–10 倍。
>
> **掌握标准**：
> - 能用 `asyncio.gather` 并发调用多个 AI API，正确处理异常和超时
> - 能用 `asyncio.Semaphore` 实现并发限速，避免触发 Rate Limit
> - 能将同步的 CPU 密集任务（如向量计算）放入 `ThreadPoolExecutor` 避免阻塞事件循环
> - 能调试异步代码中的死锁和资源泄漏问题
>
> 📖 **深入阅读**：[Python 异步编程：asyncio 在 AI 应用中的实战](/posts/python-async-programming/)

---

#### 阶段一完成检验

**场景题**：用 Python 实现一个多轮对话助手，要求：(1) 使用流式输出；(2) 上下文超过 8000 tokens 时自动做摘要压缩；(3) 支持用户调用"查看文件内容"和"执行 shell 命令"两个工具；(4) API 调用失败时自动重试最多 3 次（指数退避）。描述你的实现思路和核心代码结构。

---

### 阶段二：RAG 系统与 AI 应用开发（2–4 个月）

RAG 和 Agent 是核心，建议先把 RAG 做通，再做 Agent，两者都依赖向量数据库基础。

---

#### RAG 系统设计

> **是什么**：检索增强生成——将用户问题转化为向量检索，从知识库中找到相关文档，再交给 LLM 生成答案。
>
> **为什么学**：RAG 是当前企业 AI 应用落地最成熟的范式，解决了大模型无法访问私有数据和最新信息的核心问题。
>
> **掌握标准**：
> - 能设计合理的文档分块策略（固定大小/语义分块/Markdown 层级分块）
> - 能选择和评估 Embedding 模型（text-embedding-3-large vs BGE 系列等）
> - 能实现混合检索（向量检索 + BM25 关键词检索 + Reranker 重排序）
> - 能识别 RAG 失败的常见原因（检索失败 vs 生成失败）并针对性优化
> - 能用 RAGAS 框架量化 RAG 效果（Faithfulness、Answer Relevancy、Context Recall）
>
> 📖 **深入阅读**：
> - [RAG 系统设计实战：从文档到智能问答](/posts/rag-system-design-practice/)
> - [RAG 评估实战：用 RAGAS 量化检索增强效果](/posts/rag-evaluation-ragas/)

---

#### 向量数据库

> **是什么**：专门存储和检索高维向量的数据库，是 RAG 系统的核心存储层。
>
> **为什么学**：向量检索的质量直接决定 RAG 效果。理解索引类型和检索参数是调优的基础。
>
> **掌握标准**：
> - 能解释 HNSW 和 IVF 索引的原理和适用场景（精度 vs 速度取舍）
> - 能设计合理的 Collection Schema（向量字段 + 元数据字段 + 分区键）
> - 能用 Milvus 实现 Hybrid Search（向量 + 标量过滤组合查询）
> - 能评估和调优检索参数（ef、nprobe）平衡召回率和延迟
> - 能设计向量数据库的备份和数据更新策略（增量更新 vs 全量重建）
>
> 📖 **深入阅读**：[Milvus 向量数据库实战：从部署到生产](/posts/milvus-vector-database-practice/)

---

#### LangChain 与 LangGraph

> **是什么**：LangChain 是 AI 应用编排框架，LangGraph 在此基础上提供有状态的工作流编排能力。
>
> **为什么学**：复杂 AI 应用（多步骤推理、多工具调用、条件分支）需要编排框架，避免手工管理状态的复杂性。
>
> **掌握标准**：
> - 能用 LangChain LCEL 构建 RAG 管道，理解 Runnable 接口的设计思想
> - 能用 LangGraph 设计有状态的多步骤工作流，处理循环和条件分支
> - 能实现 Human-in-the-loop 节点，在关键决策处等待人工确认
> - 能用 LangSmith 追踪 LangChain 应用的运行轨迹，调试复杂链路
> - 能识别过度使用框架的反模式，知道何时应该直接调用裸 API
>
> 📖 **深入阅读**：
> - [LangChain 实战：构建生产级 AI 应用](/posts/langchain-practical-guide/)
> - [LangGraph 工作流编排：复杂 AI 应用状态管理](/posts/langgraph-workflow-orchestration/)

---

#### 低代码 AI 平台实践

> **是什么**：Dify、FastGPT 等低代码工具，提供可视化界面快速搭建知识库问答和工作流应用。
>
> **为什么学**：不是所有 AI 需求都值得写代码。低代码平台适合快速验证和非技术用户场景。
>
> **掌握标准**：
> - 能用 Dify 完整搭建一个 RAG 知识库应用并接入业务系统（API 方式）
> - 能设计 Dify 工作流处理多步骤任务（文档理解 → 提取信息 → 格式化输出）
> - 能判断场景应选低代码平台还是自行开发（复杂度、定制性、维护成本权衡）
> - 能配置私有化部署的 Dify，对接私有的 LLM 和 Embedding 模型
>
> 📖 **深入阅读**：
> - [Dify 自托管 RAG 实践：低代码构建知识库应用](/posts/dify-self-hosted-rag-practice/)
> - [FastGPT 知识库实践：企业级问答系统搭建](/posts/fastgpt-knowledge-base-practice/)

---

#### 阶段二完成检验

**场景题**：公司有 500 份运维 Runbook（PDF/Markdown），希望构建一个"运维知识库问答系统"：工程师用自然语言提问，系统找到相关 Runbook 片段并给出可执行的操作步骤，错误答案不允许出现。请设计完整的 RAG 系统架构，并说明如何验证答案的准确性。

---

### 阶段三：AI Agent 与工程化落地（4–6 个月）

把 AI 能力与工程实践深度融合，不只是会用 API，而是能构建可维护、可观测的生产 AI 系统。

---

#### AI Agent 设计

> **是什么**：具备自主推理和工具调用能力的 AI 系统，能够分解目标、选择工具、执行多步任务。
>
> **为什么学**：Agent 是 AI 应用的高级形态，能处理复杂的、需要多步推理的任务。
>
> **掌握标准**：
> - 能实现 ReAct（Reasoning + Acting）循环：思考 → 选择工具 → 执行 → 观察 → 再思考
> - 能设计多 Agent 协作架构：Orchestrator Agent 分发任务给专业 Sub-Agent
> - 能实现 Agent 的记忆管理：对话历史（短期）、用户偏好（长期）、工具结果（工作记忆）
> - 能在 Agent 中实现"不确定时主动确认"的安全机制，避免自动执行危险操作
> - 能评估 Agent 的执行质量，识别循环失败、工具滥用、任务偏离等问题
>
> 📖 **深入阅读**：[AI Agent 架构设计：从单智能体到多智能体系统](/posts/ai-agent-design-patterns/)

---

#### LLM 可观测性

> **是什么**：对 AI 应用的 Token 消耗、延迟、质量、成本进行全面监控和追踪（以 Langfuse 为主要工具）。
>
> **为什么学**：AI 应用上线后是个黑盒，没有可观测性你不知道为什么用户不满意，也无法控制成本。
>
> **掌握标准**：
> - 能用 Langfuse 给 LLM 调用全面插桩，追踪完整的调用链（从 Prompt 到 Response）
> - 能监控关键指标：Token 成本/天、P99 延迟、错误率、用户反馈分布
> - 能用 Langfuse Evaluations 对 AI 输出做自动化质量评估
> - 能基于可观测性数据识别 Prompt 优化机会（哪些输入导致低质量输出）
> - 能设计 AI 应用的告警体系（成本异常、延迟劣化、质量下降）
>
> 📖 **深入阅读**：[Langfuse LLM 可观测性：生产级 AI 应用监控实战](/posts/langfuse-llm-observability/)

---

#### MCP 协议与 AI 工具链

> **是什么**：Model Context Protocol（MCP）是 AI 工具调用的开放标准，让 AI 可以连接任意外部系统。
>
> **为什么学**：MCP 正成为 AI 工具扩展的事实标准（Claude/Cursor/Cline 都已支持）。掌握 MCP 能让你快速构建 AI 与运维系统的集成。
>
> **掌握标准**：
> - 能解释 MCP 的 Server/Client 架构，理解 Tool/Resource/Prompt 三种能力类型
> - 能用 Python/TypeScript 实现一个 MCP Server，暴露运维工具（kubectl/查日志/查监控）
> - 能在 Claude Desktop 或 Cursor 中配置和调试自定义 MCP Server
> - 能评估 MCP vs 传统 Function Calling 的适用场景
>
> 📖 **深入阅读**：[MCP 协议实践：DevOps 工具链 AI 化改造](/posts/mcp-protocol-devops/)

---

#### AI 编程工具工程化

> **是什么**：Cursor、Claude Code 等 AI 辅助编程工具在工程团队中的规模化应用实践。
>
> **为什么学**：AI 编程工具能将开发效率提升 2–3 倍，但需要正确的工作流设计才能发挥最大效果。
>
> **掌握标准**：
> - 能用 Cursor Rules 为项目定制 AI 编程规范，确保生成代码符合团队约定
> - 能用 Claude Code 完成复杂的多文件重构、代码解释、测试生成任务
> - 能评估 AI 生成代码的质量，识别常见的安全问题和逻辑错误
> - 能设计团队 AI 工具使用规范，在效率提升和代码质量之间取得平衡
>
> 📖 **深入阅读**：
> - [Cursor AI 编辑器指南：AI 辅助编程工作流](/posts/cursor-ai-editor-guide/)
> - [Claude Code CLI 指南：终端里的 AI 编程助手](/posts/claude-code-cli-guide/)
> - [GitHub Copilot 工程实践：从代码补全到 PR 审查](/posts/github-copilot-engineering/)

---

#### 微调与本地部署

> **是什么**：用私有数据对开源模型进行微调（LoRA/QLoRA），以及在 K8s 上运行本地大模型（Ollama）。
>
> **为什么学**：通用模型在特定领域表现有限，微调可以显著提升垂直场景准确率。本地部署解决数据安全和成本问题。
>
> **掌握标准**：
> - 能构建高质量微调数据集（200–2000 条），理解数据质量对微调效果的决定性影响
> - 能用 QLoRA 在单张 A100 上微调 7B/13B 模型，控制显存使用
> - 能用 Ollama 在 K8s 上部署 Llama/Qwen 模型，配置 GPU 调度
> - 能用 MMLU/自定义测试集评估微调后的模型，判断是否有效果提升
>
> 📖 **深入阅读**：
> - [LLM 微调实战：LoRA/QLoRA 从数据准备到部署](/posts/llm-finetuning-lora-practice/)
> - [Ollama + Kubernetes：本地大模型私有化部署实战](/posts/ollama-kubernetes-llm/)

---

#### 阶段三完成检验

**场景题**：设计一个 AIOps 系统：当 Prometheus 触发告警时，系统自动调用 AI Agent 完成以下步骤：(1) 查询相关日志和指标；(2) 基于历史 Runbook 生成排查步骤；(3) 置信度 > 90% 时自动执行修复脚本，否则推送给 on-call 工程师并附上分析报告。请设计系统架构，并说明如何保证 AI 操作的安全边界。

**预计总时间**：5–9 个月

---

## 如何选择路径？

| 我的情况 | 推荐路径 |
|---|---|
| 传统运维，想做平台/工具开发 | 路径一：DevOps |
| 想专注稳定性、故障处理、on-call | 路径二：SRE |
| 想做 AI 应用开发或 AIOps | 路径三：AI 工程化 |
| 已有 DevOps 基础，想加 AI 能力 | 路径一高级阶段 + 路径三 |
| 已有 SRE 基础，想提升可观测性深度 | 路径二进阶阶段 + 可观测性三支柱 |
| 零基础入门 | 路径一阶段一（Linux + Docker + Shell）|

---

> 每个阶段的"检验题"没有标准答案，重点是检查自己能否把所学串联起来解决真实问题。有疑问欢迎在文章评论区讨论，或到 [GitHub](https://github.com/socake) 提 Issue。
