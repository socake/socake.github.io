---
title: "学习路线图"
description: "DevOps / SRE / AI工程化 三条成长路径"
showDate: false
showReadingTime: false
---

本站整理了三条适合运维/后端工程师的成长路径，每条路径均来源于实际工作经验沉淀。选择最符合你当前处境的一条，按阶段推进即可。

---

## 路径一：DevOps 工程师成长路径

**目标受众**：传统运维工程师、系统管理员，希望向 DevOps/平台工程方向转型。

**核心目标**：从"手工运维"转向"工程化运维"，能够独立搭建和维护 CI/CD 平台与云原生基础设施。

---

### 阶段一：入门（1–3 个月）

打好地基，掌握日常工作的基础工具链。

| 知识点 / 工具 | 说明 |
|---|---|
| Linux 系统管理 | 文件系统、进程、网络、权限、systemd |
| Docker | 镜像构建、容器生命周期、Compose |
| Git | 分支模型、rebase、cherry-pick、冲突解决 |
| Shell 脚本 | Bash 脚本、任务自动化、cron |
| 基础网络 | TCP/IP、DNS、HTTP/HTTPS、iptables |

**推荐站内文章**：
- [Docker 入门到实战：从容器化到生产部署](/posts/docker-getting-started/)
- [Shell 脚本实战：自动化运维任务](/posts/shell-script-automation/)

---

### 阶段二：进阶（3–6 个月）

进入容器编排和流水线核心地带。

| 知识点 / 工具 | 说明 |
|---|---|
| Kubernetes | Pod、Deployment、Service、Ingress、ConfigMap |
| CI/CD | GitHub Actions / Jenkins / 云效 Flow |
| Helm | Chart 开发、values 管理、依赖管理 |
| Prometheus + Grafana | 指标采集、告警规则、Dashboard |
| Nginx | 反向代理、负载均衡、SSL 配置 |

**推荐站内文章**：
- [Kubernetes 从入门到实战：核心概念与生产实践](/posts/kubernetes-getting-started/)
- [Helm 深度实战：从 Chart 开发到生产部署](/posts/helm-deep-dive/)
- [Prometheus 监控体系搭建实战](/posts/prometheus-monitoring-setup/)

---

### 阶段三：高级（6–12 个月）

掌握大规模集群管理与平台工程能力。

| 知识点 / 工具 | 说明 |
|---|---|
| GitOps / ArgoCD | 声明式部署、ApplicationSet、多集群同步 |
| Karpenter | 节点自动扩缩、NodePool 设计、成本优化 |
| Istio | 服务网格、流量管理、mTLS、可观测性 |
| SLO / Error Budget | 服务可用性目标设计与落地 |
| IaC（Terraform） | 基础设施即代码、状态管理、模块化 |

**推荐站内文章**：
- [Karpenter 深度解析：Kubernetes 节点自动扩缩实战](/posts/karpenter-deep-dive/)
- [SLO/SLI/Error Budget 实战：从理论到落地](/posts/slo-sli-error-budget-practice/)
- [Istio 服务网格实战：流量管理与可观测性](/posts/istio-service-mesh-practice/)

**预计总时间**：6–12 个月（视基础和投入时间而定）

---

## 路径二：SRE 可靠性工程师路径

**目标受众**：有一定运维基础，希望专注于系统稳定性、故障处理和可靠性工程的工程师。

**核心目标**：建立系统化的可靠性思维，能够设计告警体系、主导故障排查、推动混沌工程落地。

---

### 阶段一：入门（1–3 个月）

建立 SRE 思维框架，掌握可观测性基础。

| 知识点 / 工具 | 说明 |
|---|---|
| SRE 理念 | 阅读《Google SRE》，理解 Error Budget 思想 |
| 监控告警 | Prometheus、Alertmanager、PagerDuty |
| Linux 性能调优 | CPU/内存/IO/网络瓶颈定位，perf/strace/tcpdump |
| 日志体系 | ELK/Loki 日志采集、查询与分析 |

**推荐站内文章**：
- [SLO/SLI/Error Budget 实战：从理论到落地](/posts/slo-sli-error-budget-practice/)
- [Linux 性能调优实战：CPU、内存、IO、网络](/posts/linux-performance-tuning/)

---

### 阶段二：进阶（3–6 个月）

故障排查方法论与稳定性治理实践。

| 知识点 / 工具 | 说明 |
|---|---|
| 故障排查方法论 | USE Method、RED Method、5 Whys |
| 告警体系设计 | 告警分级、降噪、on-call 轮班、runbook |
| Chaos Engineering | 故障注入原则、Chaos Mesh 实践 |
| SLO 实战 | 多服务 SLO 设计、Error Budget 燃烧率告警 |
| 可观测性三支柱 | Metrics + Logs + Traces 联动 |

**推荐站内文章**：
- [Chaos Mesh 混沌工程实战：系统韧性验证](/posts/chaos-mesh-practice/)
- [TCP 故障排查实战：从抓包到根因定位](/posts/tcp-troubleshooting/)
- [CoreDNS 原理与故障排查实战](/posts/coredns-deep-dive/)

---

### 阶段三：高级（6–12 个月）

多集群运维、成本治理与平台工程。

| 知识点 / 工具 | 说明 |
|---|---|
| 多集群运维 | ArgoCD 多集群、统一观测、跨集群网络 |
| 成本优化 | Karpenter Spot 策略、资源 Request/Limit 治理 |
| 混沌工程体系化 | GameDay 演练、韧性评分、CI 集成 |
| 平台工程 | Internal Developer Platform、黄金路径 |
| OPA / Kyverno | 策略即代码，K8s 合规治理 |

**推荐站内文章**：
- [Karpenter 深度解析：Kubernetes 节点自动扩缩实战](/posts/karpenter-deep-dive/)
- [OPA/Kyverno 策略即代码：Kubernetes 合规治理实战](/posts/opa-kyverno-policy/)
- [K8s RBAC 权限体系深度解析](/posts/kubernetes-rbac-deep-dive/)

**预计总时间**：8–15 个月

---

## 路径三：AI 工程化实践路径

**目标受众**：运维/后端工程师，希望将 AI 能力引入工程实践，或转型 AI 工程化方向。

**核心目标**：从零掌握大模型应用开发，能够独立设计和落地 RAG 系统、AI Agent，并将 AI 融入运维工作流。

---

### 阶段一：入门（1–2 个月）

理解大模型基础，快速上手 API 开发。

| 知识点 / 工具 | 说明 |
|---|---|
| 大模型基础概念 | Transformer、Token、上下文窗口、Temperature |
| API 调用 | Claude API、OpenAI API、流式输出 |
| Prompt Engineering | 系统提示词、Few-shot、CoT、结构化输出 |
| Python 异步编程 | asyncio、aiohttp，AI 应用常用基础 |

**推荐站内文章**：
- [Prompt Engineering 完全指南：从入门到高级技巧](/posts/prompt-engineering-guide/)
- [Claude API 开发实战：从入门到生产级应用](/posts/claude-api-development-guide/)

---

### 阶段二：进阶（2–4 个月）

构建真实的 AI 应用系统。

| 知识点 / 工具 | 说明 |
|---|---|
| RAG 系统 | 文档分块、向量检索、重排序、混合检索 |
| LangChain / LangGraph | Chain 编排、Agent 框架、状态图 |
| 向量数据库 | Milvus、Qdrant：索引类型、相似度计算 |
| 结构化输出 | Function Calling、JSON Schema 约束 |
| 评估体系 | RAG 评估指标、幻觉检测、RAGAS |

**推荐站内文章**：
- [RAG 系统设计实战：从文档到智能问答](/posts/rag-system-design-practice/)
- [LangChain 实战：构建生产级 AI 应用](/posts/langchain-practical-guide/)
- [Milvus 向量数据库实战：从部署到生产](/posts/milvus-vector-database-practice/)

---

### 阶段三：高级（4–6 个月）

AI Agent 设计、可观测性与运维落地。

| 知识点 / 工具 | 说明 |
|---|---|
| AI Agent 设计 | ReAct、工具调用、多 Agent 协作、记忆管理 |
| LLM 可观测性 | Token 追踪、延迟分析、成本监控、LangSmith |
| 微调实践 | LoRA、QLoRA、数据集构建、评估流程 |
| AI 运维落地 | 智能告警、日志分析、故障自愈、AIOps |
| MCP 协议 | Model Context Protocol，工具扩展标准 |

**推荐站内文章**：
- [AI Agent 架构设计：从单智能体到多智能体系统](/posts/ai-agent-architecture-design/)
- [LLM 可观测性实战：监控、追踪与成本优化](/posts/llm-observability-practice/)
- [AIOps 实战：AI 驱动的智能运维体系](/posts/aiops-intelligent-operations/)

**预计总时间**：4–8 个月

---

## 如何选择路径？

| 我的情况 | 推荐路径 |
|---|---|
| 传统运维，想做平台/工具开发 | 路径一：DevOps |
| 想专注稳定性、故障处理 | 路径二：SRE |
| 想做 AI 应用开发或 AIOps | 路径三：AI 工程化 |
| 已有 DevOps 基础，想加 AI 能力 | 路径一高级阶段 + 路径三 |

三条路径并不互斥，SRE 和 DevOps 高度重叠，AI 工程化可以作为任意路径的延伸方向。

> 有问题欢迎在文章评论区交流，或直接在 [GitHub](https://github.com/socake) 提 Issue。
