---
title: "学习路线图"
description: "DevOps / SRE / AI工程化 三条成长路径"
showDate: false
showReadingTime: false
---

本站整理了三条适合运维/后端工程师的成长路径，每条路径均来源于实际工作经验沉淀。选择最符合你当前处境的一条，按阶段推进即可。

三条路径并不互斥：SRE 和 DevOps 高度重叠，AI 工程化可以作为任意路径的延伸方向。

---

## 路径一：DevOps 工程师成长路径

**目标受众**：传统运维工程师、系统管理员，希望向 DevOps/平台工程方向转型。

**核心目标**：从"手工运维"转向"工程化运维"，能够独立搭建和维护 CI/CD 平台与云原生基础设施。

---

### 阶段一：入门（1–3 个月）

打好地基，掌握日常工作的基础工具链。先从 Docker 和 Shell 入手，再把 Linux 性能和网络调试能力补上，否则后续排障会很吃力。

| 知识点 / 工具 | 说明 |
|---|---|
| Linux 系统管理 | 文件系统、进程、网络、权限、systemd |
| Docker | 镜像构建、容器生命周期、Compose |
| Git | 分支模型、rebase、cherry-pick、冲突解决 |
| Shell 脚本 | Bash 脚本、任务自动化、cron |
| 基础网络 | TCP/IP、DNS、HTTP/HTTPS、iptables |

**推荐站内文章**：
- [Docker 最佳实践：从 Dockerfile 到生产部署](/posts/docker-best-practices/)（容器化基础，学会写高质量 Dockerfile，避免镜像臃肿和安全问题）
- [Shell 脚本自动化：运维任务工程化](/posts/shell-script-automation/)（把重复手工操作变成可复用脚本，cron 定时任务等核心实践）
- [Git 工作流实践：团队协作与分支管理](/posts/git-workflow-practice/)（掌握 GitFlow/Trunk-based 等主流工作流，解决多人协作混乱问题）
- [Linux 性能调优实战：CPU、内存、IO、网络](/posts/linux-performance-tuning/)（定位系统瓶颈的核心工具集：perf、iostat、tcpdump 实战）
- [TCP 网络故障排查实战：从抓包到根因定位](/posts/tcp-network-troubleshooting/)（网络出问题时的排查套路，理解 TCP 状态机是基础）
- [Nginx 运维完全指南：反向代理、负载均衡与调优](/posts/nginx-ops-complete/)（线上流量入口的完整配置与常见故障处理）

---

### 阶段二：进阶（3–6 个月）

进入容器编排和流水线核心地带。看完 Kubernetes 入门再看 Helm，CI/CD 配合 GitOps 理念理解效果最佳。

| 知识点 / 工具 | 说明 |
|---|---|
| Kubernetes | Pod、Deployment、Service、Ingress、ConfigMap |
| CI/CD | GitHub Actions / Jenkins / 云效 Flow |
| Helm | Chart 开发、values 管理、依赖管理 |
| Prometheus + Grafana | 指标采集、告警规则、Dashboard |
| Nginx | 反向代理、负载均衡、SSL 配置 |

**推荐站内文章**：
- [Kubernetes 入门指南：核心概念与快速上手](/posts/kubernetes-beginner-guide/)（零基础理解 Pod/Service/Deployment，快速建立 K8s 心智模型）
- [Helm 工程化实践：从 Chart 开发到多环境管理](/posts/helm-engineering-practice/)（看完 K8s 入门后必读，解决应用配置管理的工程化问题）
- [CI/CD 流水线设计：从代码提交到生产部署](/posts/cicd-pipeline-design/)（流水线设计原则与常见陷阱，适合从零搭建或重构流水线）
- [Jenkins + Kubernetes：容器化 CI/CD 实战](/posts/jenkins-kubernetes-cicd/)（在 K8s 上跑 Jenkins Agent 的完整方案）
- [GitLab CI + Kubernetes：云原生流水线实战](/posts/gitlab-ci-kubernetes/)（GitLab Runner 与 K8s 深度集成，适合 GitLab 用户）
- [Prometheus + Grafana：监控体系从零搭建](/posts/prometheus-grafana/)（指标采集、告警规则、Dashboard 模板，监控入门必读）

---

### 阶段三：高级（6–12 个月）

掌握大规模集群管理与平台工程能力。这一阶段的知识点联系紧密：GitOps 是部署底座，Karpenter 解决成本，Istio 解决服务治理，IaC 解决基础设施一致性。

| 知识点 / 工具 | 说明 |
|---|---|
| GitOps / ArgoCD | 声明式部署、ApplicationSet、多集群同步 |
| Karpenter | 节点自动扩缩、NodePool 设计、成本优化 |
| Istio | 服务网格、流量管理、mTLS、可观测性 |
| SLO / Error Budget | 服务可用性目标设计与落地 |
| IaC（Terraform/OpenTofu） | 基础设施即代码、状态管理、模块化 |
| 平台工程 | Internal Developer Platform、黄金路径 |

**推荐站内文章**：
- [GitOps 与 ArgoCD：声明式部署的完整实践](/posts/gitops-argocd/)（GitOps 是现代 DevOps 的部署底座，ArgoCD 多集群同步实战）
- [Karpenter 深度解析：Kubernetes 节点自动扩缩实战](/posts/karpenter-deep-dive/)（Spot 实例混用、NodePool 设计，节点成本可降 60%+）
- [K8s 成本优化实战：从资源治理到弹性降本](/posts/k8s-成本优化实战/)（Request/Limit 治理、VPA/HPA 配合，系统性降低集群成本）
- [Istio 服务网格实战：流量管理与可观测性](/posts/istio-service-mesh-practice/)（mTLS、金丝雀发布、流量镜像，微服务治理的高级手段）
- [OpenTofu/Terraform 实践：基础设施即代码](/posts/opentofu-terraform-practice/)（IaC 核心工作流、模块化设计，告别手工点云控制台）
- [平台工程实践：构建内部开发者平台](/posts/platform-engineering-practice/)（IDP 设计理念、黄金路径、开发者体验提升）
- [Crossplane + GitOps：云资源声明式管理](/posts/crossplane-gitops-cloud/)（用 K8s 声明式管理云资源，GitOps 延伸到基础设施层）

**预计总时间**：6–12 个月（视基础和投入时间而定）

---

## 路径二：SRE 可靠性工程师路径

**目标受众**：有一定运维基础，希望专注于系统稳定性、故障处理和可靠性工程的工程师。

**核心目标**：建立系统化的可靠性思维，能够设计告警体系、主导故障排查、推动混沌工程落地。

---

### 阶段一：入门（1–3 个月）

建立 SRE 思维框架，掌握可观测性基础。建议先读 SRE 概念文章建立认知，再看监控实践，两者配合理解会快很多。

| 知识点 / 工具 | 说明 |
|---|---|
| SRE 理念 | Error Budget、Toil、SLO/SLI/SLA 核心概念 |
| 监控告警 | Prometheus、Alertmanager、告警路由 |
| Linux 性能调优 | CPU/内存/IO/网络瓶颈定位，perf/strace/tcpdump |
| 日志体系 | 日志采集、结构化查询与分析 |
| 可观测性三支柱 | Metrics + Logs + Traces 联动 |

**推荐站内文章**：
- [SRE 概念与原则：从 Google SRE 到工程实践](/posts/sre-concepts-and-principles/)（SRE 的核心理念，读完能建立 Error Budget 思维，是整条路径的认知地基）
- [SLO/SLI/Error Budget 实战：从理论到落地](/posts/slo-sli-error-budget-practice/)（读完概念篇后落地实践，如何定义服务可用性目标并驱动稳定性改进）
- [可观测性三支柱：Metrics、Logs、Traces 体系化实践](/posts/observability-three-pillars/)（建立可观测性全局视角，理解三者如何联动定位问题）
- [Prometheus + Grafana：监控体系从零搭建](/posts/prometheus-grafana/)（Metrics 监控的完整实现，采集、存储、告警、可视化一条龙）
- [Linux 性能调优实战：CPU、内存、IO、网络](/posts/linux-performance-tuning/)（排查系统层面性能问题的必备技能，SRE 日常排障核心工具集）
- [TCP 网络故障排查实战：从抓包到根因定位](/posts/tcp-network-troubleshooting/)（网络层问题的系统排查方法，tcpdump 实战）

---

### 阶段二：进阶（3–6 个月）

故障排查方法论与稳定性治理实践。先掌握方法论再看具体工具，否则容易陷入"会用工具但不知道什么时候用"的困境。

| 知识点 / 工具 | 说明 |
|---|---|
| 故障排查方法论 | USE Method、RED Method、5 Whys |
| 告警体系设计 | 告警分级、降噪、on-call 轮班、runbook |
| Chaos Engineering | 故障注入原则、Chaos Mesh 实践 |
| SLO 实战 | 多服务 SLO 设计、Error Budget 燃烧率告警 |
| Alertmanager | 告警路由、分组、抑制、Webhook 集成 |

**推荐站内文章**：
- [故障排查方法论：系统化定位与根因分析](/posts/故障排查方法论/)（USE/RED Method 等核心方法论，让故障排查从"凭经验"变成"有框架"）
- [告警体系设计：从告警风暴到精准通知](/posts/告警体系设计/)（告警分级、降噪策略、on-call 轮班设计，解决告警疲劳问题）
- [Alertmanager 路由配置实战：告警分组与抑制](/posts/alertmanager-routing-config/)（Alertmanager 核心配置详解，路由树、分组、静默实战）
- [Chaos Mesh 混沌工程实战：系统韧性验证](/posts/chaos-mesh-practice/)（故障注入原则与 Chaos Mesh 实战，把混沌工程落地到 K8s 环境）
- [CoreDNS 原理与故障排查实战](/posts/coredns-troubleshooting-guide/)（K8s 内 DNS 故障是高频问题，掌握 CoreDNS 排查是 SRE 必备技能）
- [故障排查实录：Terway IP 泄漏问题全程复盘](/posts/故障排查-terway-ip泄漏/)（真实生产故障复盘，从现象到根因到修复的完整思路展示）

---

### 阶段三：高级（6–12 个月）

多集群运维、成本治理与高级可观测性。这一阶段重点是"系统化"——把单点能力串联成体系。

| 知识点 / 工具 | 说明 |
|---|---|
| 多集群运维 | ArgoCD 多集群、统一观测、跨集群网络 |
| 成本优化 | Karpenter Spot 策略、资源 Request/Limit 治理 |
| 混沌工程体系化 | GameDay 演练、韧性评分、CI 集成 |
| 平台工程 | Internal Developer Platform、黄金路径 |
| OPA / Kyverno | 策略即代码，K8s 合规治理 |
| 分布式追踪 | OpenTelemetry、链路追踪与全链路分析 |

**推荐站内文章**：
- [SRE 实践心得：大规模系统可靠性工程经验总结](/posts/SRE实践心得/)（高级 SRE 的系统性经验沉淀，适合阶段性回顾与查漏补缺）
- [多集群 K8s 管理：联邦、统一观测与运维实践](/posts/multi-cluster-k8s-management/)（大规模 K8s 运维的核心挑战：多集群一致性、统一监控入口）
- [OPA/Kyverno 策略即代码：Kubernetes 合规治理实战](/posts/opa-kyverno-admission-control/)（用策略代码替代手工审查，K8s 合规治理自动化）
- [Kubernetes RBAC 安全实践：权限体系设计与最小权限原则](/posts/kubernetes-rbac-security/)（权限治理是安全合规的基础，RBAC 设计与审计实战）
- [OpenTelemetry 实战：统一可观测性数据采集](/posts/opentelemetry-practice/)（OTel 是可观测性的未来标准，统一 Metrics/Logs/Traces 采集）
- [Thanos 多集群监控：Prometheus 高可用与长期存储](/posts/thanos-multi-cluster/)（解决 Prometheus 单点和存储瓶颈，多集群统一查询）
- [零信任网络实践：从概念到 K8s 环境落地](/posts/零信任网络实践/)（SRE 视角的安全加固，mTLS + 最小权限网络策略）

**预计总时间**：8–15 个月

---

## 路径三：AI 工程化实践路径

**目标受众**：运维/后端工程师，希望将 AI 能力引入工程实践，或转型 AI 工程化方向。

**核心目标**：从零掌握大模型应用开发，能够独立设计和落地 RAG 系统、AI Agent，并将 AI 融入运维工作流。

---

### 阶段一：入门（1–2 个月）

理解大模型基础，快速上手 API 开发。先建立概念认知，再学 Prompt Engineering，最后上手 API 开发，顺序不要反了。

| 知识点 / 工具 | 说明 |
|---|---|
| 大模型基础概念 | Transformer、Token、上下文窗口、Temperature |
| API 调用 | Claude API、OpenAI API、流式输出 |
| Prompt Engineering | 系统提示词、Few-shot、CoT、结构化输出 |
| Python 异步编程 | asyncio、aiohttp，AI 应用常用基础 |
| 大模型全景 | 主流模型对比与选型 |

**推荐站内文章**：
- [LLM 核心概念：大语言模型原理与工程师必知基础](/posts/llm-core-concepts/)（建立大模型认知底座，Token/上下文/Temperature 等核心概念彻底搞清楚）
- [大模型全景 2025：主流模型横评与工程选型指南](/posts/llm-landscape-2025/)（了解各模型能力边界，帮你选出最适合场景的模型）
- [Prompt Engineering 完全指南：从入门到高级技巧](/posts/prompt-engineering-guide/)（写出高质量提示词的系统方法，Few-shot、CoT、结构化输出实战）
- [Claude API 开发实战：从入门到生产级应用](/posts/claude-api-development-guide/)（Claude API 完整使用指南，流式输出、工具调用、上下文管理）
- [OpenAI API 工程实践：生产级应用开发指南](/posts/openai-api-engineering/)（OpenAI API 深度使用，Function Calling、批量处理、成本控制）
- [Python 异步编程：asyncio 在 AI 应用中的实战](/posts/python-async-programming/)（AI 应用高并发场景必备，asyncio 核心用法与常见陷阱）

---

### 阶段二：进阶（2–4 个月）

构建真实的 AI 应用系统。RAG 和 Agent 是核心，建议先把 RAG 做通，再做 Agent，两者都依赖向量数据库基础。

| 知识点 / 工具 | 说明 |
|---|---|
| RAG 系统 | 文档分块、向量检索、重排序、混合检索 |
| LangChain / LangGraph | Chain 编排、Agent 框架、状态图 |
| 向量数据库 | Milvus、Qdrant：索引类型、相似度计算 |
| 结构化输出 | Function Calling、JSON Schema 约束 |
| 评估体系 | RAG 评估指标、幻觉检测、RAGAS |
| 低代码 AI 平台 | Dify、FastGPT 快速搭建知识库应用 |

**推荐站内文章**：
- [RAG 系统设计实战：从文档到智能问答](/posts/rag-system-design-practice/)（RAG 核心架构详解，分块策略、向量检索、重排序完整流程）
- [Milvus 向量数据库实战：从部署到生产](/posts/milvus-vector-database-practice/)（向量数据库核心概念，索引类型选择与查询性能调优）
- [LangChain 实战：构建生产级 AI 应用](/posts/langchain-practical-guide/)（LangChain 核心组件与最佳实践，Chain/Agent/Memory 完整讲解）
- [LangGraph 工作流编排：复杂 AI 应用状态管理](/posts/langgraph-workflow-orchestration/)（用状态图构建复杂 AI 工作流，多步骤任务编排与错误处理）
- [RAG 评估实战：用 RAGAS 量化检索增强效果](/posts/rag-evaluation-ragas/)（RAG 不能靠主观感受，RAGAS 评估框架让效果优化有据可依）
- [Dify 自托管 RAG 实践：低代码构建知识库应用](/posts/dify-self-hosted-rag-practice/)（快速验证 RAG 场景可行性，适合需要快速交付的团队）
- [FastGPT 知识库实践：企业级问答系统搭建](/posts/fastgpt-knowledge-base-practice/)（另一款优秀的知识库平台，工作流设计与私有部署指南）

---

### 阶段三：高级（4–6 个月）

AI Agent 设计、可观测性与运维落地。这一阶段的重点是把 AI 能力与工程实践融合，不只是会用 API，而是能构建可维护的 AI 系统。

| 知识点 / 工具 | 说明 |
|---|---|
| AI Agent 设计 | ReAct、工具调用、多 Agent 协作、记忆管理 |
| LLM 可观测性 | Token 追踪、延迟分析、成本监控、Langfuse |
| 微调实践 | LoRA、QLoRA、数据集构建、评估流程 |
| AI 运维落地 | 智能告警、日志分析、故障自愈、AIOps |
| MCP 协议 | Model Context Protocol，工具扩展标准 |
| AI 编程工具 | Cursor、Claude Code 提升工程效率 |
| 多模态应用 | Vision 模型，图文理解与处理 |

**推荐站内文章**：
- [AI Agent 架构设计：从单智能体到多智能体系统](/posts/ai-agent-design-patterns/)（Agent 设计模式全景，ReAct/Plan-Execute/多 Agent 协作架构详解）
- [Langfuse LLM 可观测性：生产级 AI 应用监控实战](/posts/langfuse-llm-observability/)（AI 应用上线后的监控核心：Token 成本、延迟、质量全面追踪）
- [LLM 微调实战：LoRA/QLoRA 从数据准备到部署](/posts/llm-finetuning-lora-practice/)（私有数据微调大模型，从数据集构建到训练评估完整流程）
- [AIOps 实战：AI 驱动的智能运维体系](/posts/aiops-llm-devops/)（把大模型能力引入运维工作流，智能告警分析与故障自愈）
- [MCP 协议实践：DevOps 工具链 AI 化改造](/posts/mcp-protocol-devops/)（理解 MCP 标准，用 AI 工具扩展协议连接运维系统）
- [多模态 LLM 视觉实践：图文理解与处理工程指南](/posts/multimodal-llm-vision-practice/)（Vision 模型在工程实践中的应用，图表分析、OCR、监控截图解析）
- [Cursor AI 编辑器指南：AI 辅助编程工作流](/posts/cursor-ai-editor-guide/)（用 AI 编辑器大幅提升开发效率，从代码补全到项目级重构）
- [Claude Code CLI 指南：终端里的 AI 编程助手](/posts/claude-code-cli-guide/)（命令行 AI 编程工具深度使用，DevOps 自动化脚本生成实战）

**预计总时间**：4–8 个月

---

## 如何选择路径？

| 我的情况 | 推荐路径 |
|---|---|
| 传统运维，想做平台/工具开发 | 路径一：DevOps |
| 想专注稳定性、故障处理 | 路径二：SRE |
| 想做 AI 应用开发或 AIOps | 路径三：AI 工程化 |
| 已有 DevOps 基础，想加 AI 能力 | 路径一高级阶段 + 路径三 |
| 已有 SRE 基础，想提升可观测性深度 | 路径二进阶/高级阶段 |

---

> 有问题欢迎在文章评论区交流，或直接在 [GitHub](https://github.com/socake) 提 Issue。
