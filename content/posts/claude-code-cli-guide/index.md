---
title: "Claude Code CLI 使用指南：AI 驱动的终端编程助手"
date: 2026-02-26T12:27:00+08:00
draft: false
tags: ["Claude Code", "CLI", "AI编程", "终端", "DevOps自动化"]
categories: ["AI工具"]
description: "Claude Code的安装、代码库对话、多文件编辑、CLAUDE.md配置与DevOps实战用法"
summary: "Claude Code是Anthropic推出的终端AI编程助手，不同于编辑器插件，它在终端里直接操作文件、执行命令、理解整个代码库。本文覆盖安装配置、核心交互模式、CLAUDE.md自定义、K8s排障和自动化脚本场景。"
toc: true
math: false
diagram: false
series: ["AI 工程化实战"]
keywords: ["Claude Code", "CLI", "代码库分析", "CLAUDE.md", "K8s调试", "运维自动化"]
params:
  reading_time: true
---

Claude Code的定位是**terminal-native autonomous agent**——不是编辑器插件，不是Chat补全，而是可以在终端里独立完成整个任务的自主代理。它能读文件、写文件、执行Shell命令，还能直接对接GitHub/GitLab API：读Issue、提PR、跑CI，全程无需手动介入。

2026年它是工程师处理"重型任务"使用最多的工具，常见组合是：Cursor负责日常代码编辑，Claude Code负责复杂的跨文件重构、自动化运维、以及需要完整任务闭环的场景。

这篇文章从工程师视角介绍Claude Code的实际用法，重点放在DevOps场景。

---

## 安装与配置

### 安装

```bash
npm install -g @anthropic-ai/claude-code
```

需要Node.js 18+。安装后通过`claude`命令启动。

### API Key配置

Claude Code使用Anthropic API，首次运行会提示配置：

```bash
claude
# 首次运行会提示：
# > Please enter your Anthropic API key:
# sk-ant-...（输入你的API Key）
```

也可以通过环境变量配置：

```bash
export ANTHROPIC_API_KEY="sk-ant-your-key-here"
```

推荐在`.bashrc`或`.zshrc`里配置，避免每次重新输入。

### 基本命令

```bash
# 交互模式（最常用）
claude

# 单次对话（适合脚本里调用）
claude -p "解释一下这个错误：$(cat error.log)"

# 指定工作目录
claude --cwd /path/to/project

# 查看帮助
claude --help
```

---

## 核心交互模式

Claude Code的终端交互和浏览器里的Chat不同，它有几个独特的能力：

### 直接读写文件

不需要你手动复制粘贴代码，Claude Code可以直接读取文件内容：

```
> 读一下 deploy/kubernetes/deployment.yaml，告诉我这个Deployment的资源限制是否合理
```

它会调用Read工具读取文件，然后给出分析。修改文件时也是直接写入：

```
> 把 deployment.yaml 里的内存限制从 256Mi 改成 512Mi，CPU limit从 200m改成 500m
```

修改前Claude Code会展示diff，确认后才写入。

### 执行Shell命令

Claude Code可以执行命令，这是它和普通Chat最大的区别：

```
> 检查一下当前集群里有多少个Pod处于非Running状态
```

它会执行`kubectl get pods -A`，分析输出，然后给出结论。

```
> 找出 /var/log/app/ 目录下最近1小时内有ERROR日志的文件
```

它会执行`find`和`grep`，然后展示结果。

重要：Claude Code执行命令前会告诉你它要运行什么命令，涉及写操作或删除操作时会额外确认。不要盲目接受所有操作请求。

### 搜索代码库

```
> 搜索一下项目里所有用了 time.Sleep 的地方，这在生产代码里不应该出现
```

Claude Code会用Glob和Grep工具搜索，找出所有匹配位置，然后分析是否真的有问题。

---

## 代码库对话：理解架构

这是Claude Code最实用的场景之一：快速理解一个不熟悉的代码库。

### 了解整体架构

```
> 这个项目是干什么的？主要有哪些模块？数据流是怎样的？
```

Claude Code会自动：
1. 读取README（如果有）
2. 浏览目录结构
3. 读取主要入口文件
4. 整理出架构说明

### 追踪具体逻辑

```
> 当一个HTTP请求进来，从入口到最终响应，完整的调用链是什么？
以 POST /api/v1/orders 为例
```

它会跟着代码一路读下去，把调用链梳理清楚。

### 找实现

```
> 项目里有没有现成的分布式锁实现？找出来，告诉我怎么用
```

Claude Code会搜索代码库，找到相关实现，展示代码并解释用法。

---

## 多文件编辑工作流

Claude Code最有价值的能力是处理需要修改多个文件的任务。

### 描述任务，让它规划

```
> 我需要给所有的API接口加上统一的请求日志中间件：
> - 记录：请求方法、路径、耗时、状态码、请求ID
> - 用 structlog 格式
> - 不要记录请求体（可能包含敏感信息）
> - 在 internal/middleware/ 下新建 logging.go
> - 修改 cmd/server/main.go 注册这个中间件
```

Claude Code会先**列出修改计划**，让你确认再执行：

```
我计划做以下修改：
1. 新建 internal/middleware/logging.go
   - 实现 LoggingMiddleware 函数
   - 使用 structlog 记录请求信息
2. 修改 cmd/server/main.go
   - 导入新的middleware包
   - 在路由注册前添加 middleware.Logging()

是否继续？(y/n)
```

### 迭代修改

```
> 刚才实现的LoggingMiddleware有个问题：它记录了所有路径，
> 但 /healthz 和 /metrics 路径不应该记录日志，会产生太多噪音
> 加个配置选项，可以传入需要排除的路径列表
```

Claude Code会在上一次修改的基础上继续，保持上下文。

### 重构场景

```
> internal/handlers/ 下有很多handler，每个都在重复做参数校验和错误响应格式化
> 抽出一个公共的 validator.go 和 response.go，减少重复代码
> 修改所有现有handler使用这两个新模块
```

涉及多文件的重构，Claude Code会：
1. 先分析所有handler的公共模式
2. 设计抽取的接口
3. 新建公共模块
4. 逐一修改现有handler
5. 运行测试验证（如果你授权它执行命令）

---

## CLAUDE.md：项目级自定义配置

在项目根目录放置`CLAUDE.md`文件，内容会在Claude Code启动时自动加载，作为持久化上下文。

### CLAUDE.md的典型内容

```markdown
# 项目：运维平台后端

## 快速了解

这是一个K8s多集群管理平台的后端服务。主要功能：
- 多集群资源查看（Pod/Node/Service/ConfigMap等）
- 工作负载管理（Deployment滚动更新、回滚）
- 告警规则管理（与Prometheus集成）

## 技术栈
- Go 1.22
- Gin框架
- client-go 0.29
- PostgreSQL（元数据存储）
- Redis（缓存、Session）

## 目录结构
- cmd/server/：服务入口
- internal/api/：HTTP handlers
- internal/k8s/：K8s操作封装
- internal/db/：数据库操作
- internal/cache/：Redis缓存
- deploy/：K8s部署文件

## 开发规范
- 错误处理：fmt.Errorf("context: %w", err) 包装，不丢弃原始错误
- 日志：zerolog，JSON格式，包含trace_id字段
- 数据库操作：sqlx，不用ORM
- 配置：viper + 环境变量，本地开发用 .env 文件

## 常用命令
- 本地启动：make run
- 运行测试：make test
- 构建镜像：make docker-build
- 部署到QA：make deploy-qa

## 注意事项
- K8s客户端在 internal/k8s/client.go 初始化，多集群支持
- 数据库连接字符串从环境变量 DATABASE_URL 读取
- Redis连接从 REDIS_URL 读取
- 不要直接用 fmt.Println，统一用 log.Info().Msg()
```

有了这个文件，每次启动Claude Code就能立刻理解项目背景，不需要重新介绍。

### 全局CLAUDE.md

除了项目级，还有用户级的`~/.claude/CLAUDE.md`，对所有项目生效。适合放个人偏好：

```markdown
# 我的编码偏好

## 语言偏好
- 代码注释和commit message：英文
- 对话：中文

## 代码风格
- Go：标准gofmt格式，import按stdlib/外部库/内部包分组
- Python：black格式化，类型注解
- Shell：bash，set -euo pipefail，函数加注释

## 工具偏好
- 查K8s资源：kubectl，不用Helm CLI
- 查日志：stern或kubectl logs，不用k9s（终端里没有）
- JSON处理：jq

## 安全要求
- 删除操作之前必须先展示影响范围
- 生产环境操作（db、k8s prod）要我明确确认
- 不要把API key、密码写到代码里
```

---

## DevOps场景实战

### 场景1：调试K8s问题

一个常见的故障排查对话：

```
> 有个Pod一直CrashLoopBackOff，帮我排查
```

Claude Code会：
1. 执行`kubectl get pods`找到问题Pod
2. 执行`kubectl describe pod <name>`看事件
3. 执行`kubectl logs <name> --previous`看崩溃前的日志
4. 分析原因，给出修复建议

```
> 分析一下过去1小时集群里的异常事件，有没有值得关注的问题
```

```bash
# Claude Code会执行类似这样的命令：
kubectl get events -A --sort-by=.lastTimestamp | tail -50
# 然后过滤Warning级别事件分析
```

### 场景2：写K8s巡检脚本

```
> 帮我写一个K8s集群巡检脚本，检查以下项目：
> 1. 节点状态（NotReady的节点）
> 2. 所有namespace里restart次数>5的容器
> 3. PVC使用率超过80%的（需要metrics-server）
> 4. 最近24小时的OOMKill事件
> 5. 没有设置resource limits的Deployment
>
> 输出格式：分项展示，有问题的用红色标注，生成总结行
> 保存到 scripts/cluster-health-check.sh
```

Claude Code会写完脚本并直接保存到文件。

### 场景3：分析日志

```
> 帮我分析 /var/log/app/app.log，找出：
> 1. 最频繁出现的错误类型（按出现次数排序）
> 2. 错误高峰的时间段
> 3. 每种错误类型的一个代表性日志行
```

Claude Code会执行grep、awk、sort等命令处理日志，然后整理成清晰的报告。

### 场景4：自动化变更

```
> 我需要批量更新所有Deployment的镜像拉取策略：
> 把 imagePullPolicy: Always 改成 imagePullPolicy: IfNotPresent
> 只改 monitoring 和 staging namespace 的Deployment
> 先给我看会影响哪些Deployment，确认后再执行变更
```

Claude Code会先查询受影响的资源，展示列表，等你确认后再执行修改。

### 场景5：写Terraform

```
> 我需要在AWS上创建一个私有S3 bucket：
> - 用于存储应用日志
> - 加密：SSE-S3
> - 生命周期：超过90天的对象转移到Glacier，超过365天删除
> - 禁止公开访问
> - bucket名从变量读取
>
> 在 terraform/modules/log-bucket/ 下创建这个module
```

Claude Code会创建完整的Terraform module，包括`main.tf`、`variables.tf`、`outputs.tf`。

### 场景6：GitHub/GitLab Issue驱动开发

Claude Code可以直接调用GitHub/GitLab API，实现从Issue到PR的完整自动化：

```
> 读一下 GitHub Issue #234，按需求描述实现这个功能，
> 写完代码后跑测试，测试通过了提一个PR，关联这个Issue
```

Claude Code会：
1. 调用GitHub API读取Issue内容和评论
2. 理解需求，规划实现方案
3. 编写代码，修改相关文件
4. 执行测试命令，验证通过
5. 提交代码，创建PR并关联Issue

这是它与Cursor最大的差异点：Claude Code完成的是有明确起止点的完整任务，而不只是编辑器里的辅助操作。

---

## 与Cursor的对比

两个工具的定位不同，不是非此即彼的关系：

| 维度 | Claude Code | Cursor |
|------|------------|--------|
| 使用环境 | 终端 | 编辑器（VSCode / JetBrains） |
| 最适合 | 自主完成复杂任务、运维、Issue驱动开发 | 日常代码编辑、多文件重构 |
| 上下文加载 | 自动探索，也可以指定 | @符号显式引用 |
| 代码补全 | 无（不是IDE） | 强，实时Tab补全 |
| 命令执行 | 原生支持 | Agent/computer use支持 |
| GitHub集成 | 原生（读Issue、提PR） | 无 |
| 模型 | Claude系列 | 可选Claude Sonnet 4.6/GPT-5.4/Gemini 2.5 Pro |
| 适合场景 | 重型任务、服务器、CI/CD、自动化闭环 | 本地日常开发 |

**2026年推荐组合**：Cursor处理日常代码编辑和快速功能迭代，Claude Code处理复杂任务——大型重构、跨服务修改、Issue驱动的完整功能实现、运维自动化。两者各司其职，不是替代关系。

---

## 实用技巧

### 用pipe传入上下文

```bash
# 把命令输出直接传给Claude Code分析
kubectl get pods -A -o json | claude -p "找出所有处于非正常状态的Pod，分析原因"

# 分析日志文件
cat /var/log/app/error.log | claude -p "这些错误日志里最严重的问题是什么？"
```

### 在CI/CD里使用

```yaml
# .github/workflows/review.yaml
- name: Security Check
  run: |
    claude -p "检查这次PR的代码变更（见diff），有没有安全问题：SQL注入、敏感信息硬编码、不安全的依赖" \
      < git diff origin/main
```

### 限制权限

如果不想让Claude Code执行命令，只用对话模式：

```bash
# 只读模式（不允许写文件和执行命令）
claude --no-tools
```

### 会话继续

Claude Code支持保存和恢复会话，不会因为终端关闭丢失上下文：

```bash
# 查看历史会话
claude --list-sessions

# 继续某个会话
claude --resume <session-id>
```

---

## 注意事项

**定价**：Claude Code按使用量计费，月费约$20-200，取决于使用频率和任务复杂度。日常轻度使用一般在$20-50区间，重度自主任务（大型重构、频繁Issue驱动开发）可能到$100+。建议初期设置用量提醒，了解自己的使用模式后再决定是否需要控制。

**成本控制**：Claude Code每次对话都会调用API，费用按token计算。复杂的代码库分析任务单次可能消耗大量token。建议：
- 避免让它反复读取大文件
- 明确任务边界，避免开放式探索
- 对简单查询，用普通Chat而非Claude Code

**安全意识**：Claude Code有写文件和执行命令的能力，这意味着操作失误的代价比纯对话工具更高。规则：
- 在生产服务器上谨慎使用，或只用只读模式
- 所有写操作都仔细看diff再确认
- 不要在包含生产凭证的目录里启动Claude Code

**准确性**：Claude Code的代码理解能力很强，但不是100%准确，尤其是复杂的业务逻辑推断。它给的架构分析可以作为起点，但需要自己验证。
