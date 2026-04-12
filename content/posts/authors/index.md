---
title: "关于我"
date: 2025-12-03T23:09:20+08:00
draft: false
description: "DevOps / SRE Engineer，专注云原生基础设施、GitOps 工程化与 AI 工具落地"
showDate: false
showReadingTime: false
showWordCount: false
showAuthor: false
showPagination: false
showRelatedContent: false
showTableOfContents: true
---

{{< typeit >}}
你好，我是黄文卓 —— 我的工作是让系统在你不注意的时候，把一切都悄悄运转好。
{{< /typeit >}}

---

## 职业时间线

| 时间 | 阶段 | 关键词 |
|------|------|--------|
| 2019 | 入行运维，从装系统开始 | Linux、Shell、手动部署 |
| 2020 | 接触容器化，第一次部署 K8s 集群 | Docker、Kubernetes、自建集群踩坑 |
| 2021 | 上云，开始管理 AWS EKS | EKS、ECS、IAM、EC2 费用第一次超预算 |
| 2022 | 引入 GitOps，基础设施开始版本化 | ArgoCD、Kustomize、多环境配置管理 |
| 2023 | 规模化：双云架构 + 多集群治理 | AWS + 阿里云 ACK、Karpenter 降本 |
| 2024 | 安全与可观测性补课 | Cilium、gVisor、Loki 跨集群、零信任改造 |
| 2025 | AI 工具全面融入工作流 | Claude Code CLI、Cursor、LLM 运维自动化 |
| 2026 | 平台工程 + AI Agent 落地探索 | Platform Engineering、Agent 自动化运维 |

---

## 技术栈

### 容器与编排
{{< badge >}}Kubernetes{{< /badge >}}
{{< badge >}}Docker{{< /badge >}}
{{< badge >}}Helm{{< /badge >}}
{{< badge >}}Karpenter{{< /badge >}}
{{< badge >}}ArgoCD{{< /badge >}}
{{< badge >}}Kustomize{{< /badge >}}
{{< badge >}}Istio{{< /badge >}}
{{< badge >}}Argo Rollouts{{< /badge >}}

### 云平台
{{< badge >}}AWS EKS / EC2 / EFS / S3 / IAM{{< /badge >}}
{{< badge >}}阿里云 ACK / RDS / OSS{{< /badge >}}

### CI/CD & GitOps
{{< badge >}}GitHub Actions{{< /badge >}}
{{< badge >}}云效 Flow{{< /badge >}}
{{< badge >}}GitOps{{< /badge >}}
{{< badge >}}ArgoCD ApplicationSet{{< /badge >}}

### 可观测性
{{< badge >}}Prometheus{{< /badge >}}
{{< badge >}}Grafana{{< /badge >}}
{{< badge >}}Loki{{< /badge >}}
{{< badge >}}Thanos{{< /badge >}}
{{< badge >}}OpenTelemetry{{< /badge >}}

### 中间件 & 存储
{{< badge >}}Kafka{{< /badge >}}
{{< badge >}}RabbitMQ{{< /badge >}}
{{< badge >}}Redis / Valkey{{< /badge >}}
{{< badge >}}MySQL{{< /badge >}}
{{< badge >}}PostgreSQL{{< /badge >}}
{{< badge >}}OpenSearch{{< /badge >}}
{{< badge >}}Neo4j{{< /badge >}}
{{< badge >}}Milvus{{< /badge >}}

### 网络 & 安全
{{< badge >}}Cilium{{< /badge >}}
{{< badge >}}Terway{{< /badge >}}
{{< badge >}}gVisor (runsc){{< /badge >}}
{{< badge >}}Headscale{{< /badge >}}
{{< badge >}}OPA / Kyverno{{< /badge >}}
{{< badge >}}Vault{{< /badge >}}

### 编程语言
{{< badge >}}Go{{< /badge >}}
{{< badge >}}Python{{< /badge >}}
{{< badge >}}Shell / Bash{{< /badge >}}

### AI 工具（日常在用）
{{< badge >}}Claude Code CLI{{< /badge >}}
{{< badge >}}Cursor{{< /badge >}}
{{< badge >}}LangChain{{< /badge >}}
{{< badge >}}LangGraph{{< /badge >}}
{{< badge >}}Dify{{< /badge >}}
{{< badge >}}RAG 工程化{{< /badge >}}
{{< badge >}}Prompt Engineering{{< /badge >}}

### AI 模型（会用，懂选型）
{{< badge >}}Claude Sonnet 4.6 / Opus 4.6{{< /badge >}}
{{< badge >}}GPT-5.4{{< /badge >}}
{{< badge >}}Gemini 2.5 Pro{{< /badge >}}

---

## 做过什么

**多集群 K8s 管理（US + CN 双云）**
同时维护生产、预发、QA 多套 Kubernetes 集群，覆盖 AWS EKS（us-west-2 + ap-southeast-1）与阿里云 ACK，管理数十个微服务的发布与稳定性。出过故障，也深夜扛过流量洪峰。

**GitOps 体系从零到落地**
主导设计基于 ArgoCD + Kustomize + ApplicationSet 的完整 GitOps 工作流，实现 base/overlay 多环境配置版本化管理，所有变更可追溯、可回滚。部署不再依赖人肉执行，而是 Git commit 驱动。

**降本优化，有数字说话**
通过 Karpenter 弹性节点策略 + 资源规格治理 + Spot 实例混用，单月云成本节省超 $2,000。同步推进 FinOps 意识，让每一台机器的账单都有据可查。

**CI/CD 流水线，多场景多云**
从零搭建并维护覆盖 GitHub Actions + 云效 Flow 的发版体系，支持 US / CN 独立部署链路、多分支策略、镜像 tag 版本化，彻底解决跨云竞态问题。

**跨集群可观测性**
基于 Grafana + Loki 构建跨 6 套集群的统一日志查询系统，支持并行多集群查询，告警覆盖核心服务。Prometheus + Thanos 实现指标聚合，不再靠肉眼看 terminal 判断集群健康。

**网络安全治理 & 零信任改造**
梳理全部公网暴露资产清单，规划并推进 Headscale VPN 零信任收敛方案；调研 Cilium 网络策略替代 kube-proxy，收紧生产环境东西向流量边界。

**gVisor 沙箱隔离**
在多租户 sandbox 环境落地基于 gVisor（runsc）的容器网络隔离方案，结合 Cilium CCNP 实现 workload 级别的网络隔离，验证可行性并提交 GitOps PR。

**AI 工具落地 & 运维自动化**
将 Claude Code CLI 深度集成进日常运维工作流，覆盖：故障排查自动化、跨集群日志分析、K8s 配置审查。基于 LLM 构建每日运维技术简报自动生成系统，14 个主题轮换，每天推送到钉钉群。

---

## 工程哲学

> **好的基础设施应该像空气一样，存在但不被感知。**

1. **可观测优先于可靠性** — 你无法修复你看不见的东西。在写代码之前先想清楚怎么 debug 它。

2. **配置即代码，Git 是唯一真相** — 任何不在 Git 里的变更都是定时炸弹，包括那条你"临时"改的 Nacos 配置。

3. **自动化的边界是人的判断** — 能自动化的都应该自动化，但报警触发之后"要不要回滚"这件事，还是要人拍板。

4. **降本不是省钱，是减少浪费** — 每一块钱都应该知道花在哪里；闲置资源是技术债，不是备用容量。

5. **工具选型要有退出路径** — 引入任何新工具之前，先想好怎么摘掉它。依赖一个你无法替换的组件，不叫技术选型，叫赌博。

---

## 当前在关注的方向

- **AI Agent 运维落地** — LLM 不只是聊天框，正在探索 Agent 自主执行运维操作（故障定位 → 修复建议 → GitOps PR 自动提交）的完整链路
- **eBPF 可观测性** — Cilium Hubble、Tetragon 在内核层面的追踪能力，比传统 sidecar 方案侵入性低一个量级
- **平台工程（Platform Engineering）** — 把运维能力封装成内部开发者平台，让研发可以自助而不是等待工单
- **LLM 与运维工具链融合** — 不是让 AI 替代运维，是让运维工程师用 AI 把能力放大 10 倍

---

## 关于这个博客

两个用途，都是真的：

1. **技术笔记本** — 把踩过的坑、研究过的方案、写过的脚本沉淀下来。人的记忆是不可靠的，尤其是凌晨两点刚解完故障之后。

2. **技术展示** — 记录真实的工作内容，证明这些年没白过。如果你是 HR 或 Hiring Manager，这里有比简历更诚实的东西。

内容方向：**Kubernetes 运维**、**云原生实践**、**CI/CD 工程化**、**基础设施降本**、**AI 工具落地**、**踩坑实录**。

---

## 一些真实信息

- 有在深夜为一行 YAML 缩进而抓狂的经历，不止一次
- 对 `kubectl get pods | grep CrashLoop` 有条件反射
- 坚信 `--dry-run=client` 是世界上最好的安全网之一
- 用 Claude Code CLI 写运维脚本，并且觉得这完全合理
- 会因为一个优雅的 Kustomize patch 设计感到满足

---

## 联系方式

- **GitHub**：[github.com/socake](https://github.com/socake)
- **Email**：17691281867@163.com

欢迎聊技术问题，尤其是 K8s 运维、云原生架构、或者 AI 工具怎么用到工程里。

> *如果你读到这里还没关掉页面，说明我们大概率可以聊得来。*
