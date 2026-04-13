---
title: "Temporal 分布式工作流引擎实战：Worker、Activity、重试语义与生产部署"
date: 2026-04-08T10:00:00+08:00
draft: false
tags: ["Temporal", "工作流", "分布式系统", "微服务", "Go", "Kubernetes"]
categories: ["分布式系统"]
description: "系统讲解 Temporal 的编程模型（Workflow/Activity）、确定性执行与 event history replay、重试与补偿、Signal/Query、生产集群部署（Cassandra/PostgreSQL 后端）与容量规划。"
summary: "长流程业务编排历来头疼——状态机、定时器、补偿、幂等、失败恢复都要自己写。Temporal 用 event sourcing + 确定性 replay 把这些问题一次性解决。本文以 Go SDK 为主线，从编程模型、Workflow 确定性约束、Activity 重试、Signal/Query、child workflow、到生产集群部署、监控和容量规划，给出可直接落地的范式。"
toc: true
math: false
diagram: false
keywords: ["Temporal", "Workflow Engine", "Saga", "Event Sourcing", "Go SDK", "分布式编排", "Activity 重试"]
params:
  reading_time: true
---

## 一、长流程业务编排的老大难

做后端久了，总会遇到一类业务：它不是一次 HTTP 请求能解决的，也不是纯粹的离线批处理，而是介于中间——一个订单从"用户下单"到"包裹签收"，跨越支付、库存、物流、售后，时间跨度从几秒到几十天，中间任何一步都可能失败、超时、人工介入，还要求可追溯、可补偿、可重试。

我们先把这类业务命名为"长流程业务编排"。它的典型特征：

- **跨服务、跨进程**：一个流程要调 order-service、payment-service、inventory-service、shipping-service 四五个下游，任何一个挂掉都不能让整条流程死在半路。
- **时间跨度大**：从几分钟（退款审核）到几个月（分期付款）不等，进程会重启、机器会下线、甚至整个集群会迁移，流程状态不能丢。
- **需要补偿**：支付扣款成功但库存预留失败，必须回滚支付；半路取消订单，要释放库存、退优惠券。
- **定时与外部事件交织**：过了支付超时要关单；收到物流回调要推进状态；用户随时可能发起退款 Signal。
- **幂等性要求极高**：一切重试、补偿、回放都不能产生副作用重复。

### 1.1 常规方案为什么难用

大多数团队第一版方案都长得差不多：**业务表 + 状态字段 + 定时扫描**。

```sql
-- order 表加个 status 列和 next_retry_at
SELECT * FROM orders
WHERE status IN ('paying', 'reserving', 'shipping')
  AND next_retry_at < NOW()
LIMIT 100;
```

跑一个 cron 每分钟扫一把，根据状态推进下一步。这套方案的病在哪里？

1. **状态机散落在代码各处**：`if status == paying` 的分支散落在 handler、cron、MQ consumer 里，没有人能一眼看清"订单到底有多少状态，之间怎么流转"。
2. **重试策略不统一**：支付失败退避 5s、库存失败退避 30s、物流失败退避 5min，每处手写，没人维护。
3. **补偿难**：想实现 Saga，得手写"反向状态"，`paid → refunding → refunded`，和正向流程同等复杂度，但更难测试。
4. **定时器不可靠**："30 分钟后自动关单"这种需求，要么占用 cron 扫表资源，要么依赖 Redis delayed queue，数据漂移一次性暴露。
5. **失败恢复**：进程挂在"扣款成功但还没写 DB"的中间态，重启后没人知道该干嘛，只能人工捞数据。
6. **可观测性差**：想看"订单 1234 现在卡在哪一步"，要查 DB + 日志 + 链路三套系统拼起来。

### 1.2 Saga 手写成本

稍微高级一点的团队会读 Garcia-Molina 1987 年那篇 Saga 论文，试图把流程拆成 `T1 T2 T3 ... Tn`，每个 `Ti` 配一个 `Ci`，失败时反向执行补偿。手写 Saga 大致长这样：

```go
// 伪代码
compensations := []func() error{}
defer func() {
    if err != nil {
        for i := len(compensations) - 1; i >= 0; i-- {
            compensations[i]()
        }
    }
}()

if err = payment.Charge(ctx, orderID); err != nil { return }
compensations = append(compensations, func() error {
    return payment.Refund(ctx, orderID)
})

if err = inventory.Reserve(ctx, orderID); err != nil { return }
compensations = append(compensations, func() error {
    return inventory.Release(ctx, orderID)
})
// ...
```

问题在于：**这段代码必须一次跑完**。进程崩溃，`compensations` 数组丢了，所有已执行 step 的补偿就没人做了。要让它可恢复，就必须把"已执行到哪一步"和"补偿闭包参数"持久化到 DB，每一步前后写两次日志——这已经是在手写一个简陋的 event sourced workflow engine 了。

于是我们终于有了正当理由去看 Temporal。

## 二、Temporal 的定位

Temporal 来自 Cadence 社区——Uber 开源的同类项目——原作者另起炉灶的版本，目前是分布式工作流领域最活跃的方案之一。它自称 "durable execution"，不太准确但抓住了最核心的卖点：**你写一段看似普通的业务代码，它能跨进程、跨机器、跨时间维度地"活下去"。**

### 2.1 和同类方案的区别

初次接触 Temporal 的人最容易把它和这几个东西搞混：

| 维度 | Temporal | Airflow | Argo Workflows | 自研状态机 |
|---|---|---|---|---|
| 面向 | 业务流程编排 | 数据管道调度 | CI/CD 与批处理 | 业务流程 |
| 编排粒度 | 代码级（SDK） | DAG 节点 | K8s Pod | 代码+DB |
| 流程长度 | 毫秒~数月 | 小时~天 | 分钟~小时 | 任意 |
| 状态持久化 | Event History | metadata DB | CRD + etcd | 业务 DB |
| 重试 | 原生细粒度 | task_instance | retryStrategy | 手写 |
| 外部信号 | Signal | Sensor | — | 手写 |
| 主要场景 | 订单/支付/审批 | ETL | 构建/训练 | — |

一句话：**Airflow 是给数据工程师跑 ETL DAG 的，Argo Workflows 是给 SRE 编排 Pod 任务的，Temporal 是给后端工程师写业务流程的**。如果你在 Airflow 里跑"订单履约"，你会很快因为 scheduler 延迟、task instance 状态不可控而崩溃。

### 2.2 Temporal 的核心承诺

Temporal 官方文档反复提到一个概念叫 "Workflow as code"——你用 Go/Java/Python/TypeScript 写一段普普通通的函数，长这样：

```go
func OrderFulfillmentWorkflow(ctx workflow.Context, orderID string) error {
    if err := workflow.ExecuteActivity(ctx, ChargePayment, orderID).Get(ctx, nil); err != nil {
        return err
    }
    if err := workflow.ExecuteActivity(ctx, ReserveInventory, orderID).Get(ctx, nil); err != nil {
        return err
    }
    return workflow.ExecuteActivity(ctx, ShipOrder, orderID).Get(ctx, nil)
}
```

然后 Temporal 保证：

1. **持久化执行**：这段函数"每一步"都会被记录到 event history，进程挂了重新起来能从上次的点继续。
2. **可靠重试**：`ChargePayment` 失败会按配置的 RetryPolicy 自动重试，直到成功或彻底放弃。
3. **可靠定时器**：`workflow.Sleep(ctx, 30*time.Minute)` 真的能睡 30 分钟，即使中间重启了进程。
4. **可外部驱动**：外部代码可以通过 Signal 注入事件，通过 Query 读取当前状态。
5. **可追溯**：每一个工作流实例的完整执行历史都能在 Web UI 里看到。

这些承诺背后是 event sourcing + 确定性 replay 的组合拳，下面会详细拆。

## 三、核心概念梳理

入门 Temporal 前先把词汇表对齐，否则读文档会一脸懵。

### 3.1 Workflow

Workflow 是一段代码，也是一个运行时实例。**代码维度**的 workflow 是你写的那个 Go 函数；**实例维度**的 workflow 是"某个订单 ID 触发的一次执行"，在 Temporal 内部用 `WorkflowID + RunID` 唯一标识。

几个关键属性：

- Workflow 函数**必须是确定性的**（原因见第六节）。
- Workflow 函数不能直接做 I/O、不能直接调下游服务，所有副作用都要通过 Activity。
- Workflow 函数可以 sleep 任意长时间，可以等外部 signal，可以开 child workflow，但不能 `go func()` 开原生 goroutine。

### 3.2 Activity

Activity 就是你实际要做的"副作用操作"：扣款、扣库存、调第三方、写 DB、发 MQ、读文件。

- Activity 是**普通 Go 函数**，想怎么写怎么写，没有确定性要求。
- Activity **会被重试**，所以必须幂等。
- Activity 有完整的超时 + 重试配置，Worker 崩了 server 会重新派发。
- Activity 可以 heartbeat 上报进度，长任务尤其重要。

### 3.3 Worker

Worker 是一个进程，它同时做两件事：
1. **Workflow Worker**：从 task queue 拉 workflow task，执行/replay 你的 workflow 代码，把决策（"下一步要执行哪个 activity"）推回给 server。
2. **Activity Worker**：从 task queue 拉 activity task，执行 activity 函数，把结果推回给 server。

一个 Worker 进程可以同时注册多个 workflow 和 activity，也可以只注册一种。生产上经常把 activity worker 单独拆出来（CPU 密集型、网络密集型分池），workflow worker 则轻量。

### 3.4 Task Queue

Task Queue 是 worker 和 server 之间的"工单池"。你在 client 启动 workflow 时指定 `TaskQueue: "order-fulfillment"`，只有订阅这个 task queue 的 worker 才能拿到任务。

Task queue 没有"创建"操作，第一次有 worker 订阅或有任务入队时自动存在。它也是**水平扩展单位**：一个 task queue 对应一类业务，worker 池大小独立伸缩。

### 3.5 Namespace

Namespace 是逻辑租户隔离。一个 Temporal cluster 可以服务多个 namespace，每个 namespace 有独立的 workflow、retention、archival、search attributes 配置。生产上通常按业务线分 namespace：`order`、`payment`、`user-growth` 各一个。

### 3.6 Event History

每个 workflow 实例都有一条完整的 event history，形如：

```
1  WorkflowExecutionStarted
2  WorkflowTaskScheduled
3  WorkflowTaskStarted
4  WorkflowTaskCompleted
5  ActivityTaskScheduled (ChargePayment)
6  ActivityTaskStarted
7  ActivityTaskCompleted (result: "txn-abc")
8  WorkflowTaskScheduled
...
```

这就是 Temporal 的"真相之源"。worker 宕机重启后，server 会把整条 history 发给新的 worker，让它 replay workflow 函数来重建内存状态，这就是"durable execution"的底层原理。

## 四、系统架构

Temporal server 由四类服务组成，生产部署大多把它们跑在同一个集群里但按角色分 pod：

### 4.1 Frontend Service

对外 gRPC 入口，负责鉴权、限流、路由。Client SDK、Worker、Web UI 都连 frontend。它本身无状态，水平扩展。

### 4.2 History Service

维护 workflow 的 event history 和 mutable state，是整个系统最核心也最重的组件。History service 按 **shard** 分片，每个 shard 是一组 workflow 实例的归属单位。集群初始化时 shard 数量固定（常见 512 或 4096），后面不能动态改。

History service 的写路径是：
1. 接收 workflow task 完成事件
2. append 新 event 到 history
3. 更新 mutable state
4. 事务持久化到后端 DB

如果 history service 成为瓶颈，通常是 shard 数不够导致单 shard 太热，或者后端 DB 写入跟不上。

### 4.3 Matching Service

Task queue 的实现者。负责把 workflow task / activity task 从 queue 派发给 worker。matching 也按 task queue 分片，支持 sticky task queue（workflow task 倾向于回到原 worker，提升 cache 命中）。

matching 出问题常见症状是 task queue backlog 增长，worker 明明空闲但拿不到任务。

### 4.4 Worker Service（server 内部）

注意这个 "Worker service" 不是你自己写的 worker，是 server 自带的内部 worker，用来跑 archival、scanner、replication、batch operation 等系统级任务。默认 namespace 里的一些"后台清理"都由它完成。

### 4.5 持久化后端

Temporal 官方支持的后端：

| 后端 | 适用规模 | 优点 | 缺点 |
|---|---|---|---|
| Cassandra | 超大规模 | 水平扩展、官方首推 | 运维复杂 |
| PostgreSQL | 中小规模 | 运维简单、事务强 | 单点扩展上限 |
| MySQL | 中小规模 | 团队熟 | 同上 |

**决策建议**：日均 workflow 启动数 < 100 万、history event < 5000 万/天，PostgreSQL 够用；超过这个量级就上 Cassandra。切换后端不是零成本的，前期选型要看清楚。

### 4.6 可见性存储

Temporal 的"列表 workflow"功能（按 ID、状态、自定义 search attribute 查询）默认写到同一个主库，叫 "standard visibility"。但这玩意儿 scale 差，生产基本都要启用 **Elasticsearch advanced visibility**：Temporal 会把每次 workflow 状态变更推到 ES，ES 提供全文检索。

## 五、Hello World：OrderFulfillment Workflow

上手感受一下。我们写一个订单履约流程：扣款 → 扣库存 → 发货，完整的 Go SDK 代码。

### 5.1 Activity 定义

`activities/order.go`:

```go
package activities

import (
    "context"
    "errors"
    "fmt"

    "go.temporal.io/sdk/activity"
)

type OrderActivities struct {
    PaymentClient   PaymentClient
    InventoryClient InventoryClient
    ShippingClient  ShippingClient
}

type PaymentClient interface {
    Charge(ctx context.Context, orderID string, amount int64) (string, error)
    Refund(ctx context.Context, txnID string) error
}

type InventoryClient interface {
    Reserve(ctx context.Context, orderID string, sku string, qty int) (string, error)
    Release(ctx context.Context, reservationID string) error
}

type ShippingClient interface {
    CreateShipment(ctx context.Context, orderID string) (string, error)
    CancelShipment(ctx context.Context, shipmentID string) error
}

// ChargePayment 扣款，返回交易 ID
func (a *OrderActivities) ChargePayment(ctx context.Context, orderID string, amount int64) (string, error) {
    logger := activity.GetLogger(ctx)
    logger.Info("ChargePayment start", "orderID", orderID, "amount", amount)

    txnID, err := a.PaymentClient.Charge(ctx, orderID, amount)
    if err != nil {
        // 业务层确定不该重试的错误，用 NonRetryable 包一层
        if errors.Is(err, ErrInsufficientFunds) {
            return "", NewNonRetryable("insufficient_funds", err)
        }
        return "", fmt.Errorf("charge failed: %w", err)
    }
    return txnID, nil
}

// RefundPayment 补偿：退款
func (a *OrderActivities) RefundPayment(ctx context.Context, txnID string) error {
    activity.GetLogger(ctx).Info("RefundPayment", "txnID", txnID)
    return a.PaymentClient.Refund(ctx, txnID)
}

// ReserveInventory 扣库存
func (a *OrderActivities) ReserveInventory(ctx context.Context, orderID, sku string, qty int) (string, error) {
    logger := activity.GetLogger(ctx)
    logger.Info("ReserveInventory start", "orderID", orderID, "sku", sku, "qty", qty)

    resvID, err := a.InventoryClient.Reserve(ctx, orderID, sku, qty)
    if err != nil {
        if errors.Is(err, ErrOutOfStock) {
            return "", NewNonRetryable("out_of_stock", err)
        }
        return "", fmt.Errorf("reserve failed: %w", err)
    }
    return resvID, nil
}

// ReleaseInventory 补偿：释放库存
func (a *OrderActivities) ReleaseInventory(ctx context.Context, reservationID string) error {
    return a.InventoryClient.Release(ctx, reservationID)
}

// CreateShipment 创建物流单
func (a *OrderActivities) CreateShipment(ctx context.Context, orderID string) (string, error) {
    return a.ShippingClient.CreateShipment(ctx, orderID)
}

// CancelShipment 补偿：取消物流
func (a *OrderActivities) CancelShipment(ctx context.Context, shipmentID string) error {
    return a.ShippingClient.CancelShipment(ctx, shipmentID)
}

var (
    ErrInsufficientFunds = errors.New("insufficient_funds")
    ErrOutOfStock        = errors.New("out_of_stock")
)
```

`activities/errors.go`:

```go
package activities

import "go.temporal.io/sdk/temporal"

// NewNonRetryable 把一个业务错误包成 Temporal 的非重试错误
func NewNonRetryable(code string, cause error) error {
    return temporal.NewNonRetryableApplicationError(cause.Error(), code, nil)
}
```

### 5.2 Workflow 定义

`workflows/order_fulfillment.go`:

```go
package workflows

import (
    "time"

    "go.temporal.io/sdk/temporal"
    "go.temporal.io/sdk/workflow"

    "example.com/orders/activities"
)

// OrderRequest 工作流入参
type OrderRequest struct {
    OrderID string
    UserID  string
    SKU     string
    Qty     int
    Amount  int64 // 分
}

// OrderResult 工作流返回值
type OrderResult struct {
    OrderID    string
    TxnID      string
    ShipmentID string
}

// OrderFulfillmentWorkflow 订单履约
func OrderFulfillmentWorkflow(ctx workflow.Context, req OrderRequest) (*OrderResult, error) {
    logger := workflow.GetLogger(ctx)
    logger.Info("OrderFulfillment start", "orderID", req.OrderID)

    // Activity 通用选项
    ao := workflow.ActivityOptions{
        StartToCloseTimeout: 30 * time.Second,
        RetryPolicy: &temporal.RetryPolicy{
            InitialInterval:    time.Second,
            BackoffCoefficient: 2.0,
            MaximumInterval:    time.Minute,
            MaximumAttempts:    5,
        },
    }
    ctx = workflow.WithActivityOptions(ctx, ao)

    var a *activities.OrderActivities // 运行时由 worker 注册，nil 只是用来引用方法名

    // Step 1: 扣款
    var txnID string
    if err := workflow.ExecuteActivity(ctx, a.ChargePayment, req.OrderID, req.Amount).Get(ctx, &txnID); err != nil {
        return nil, err
    }

    // Step 2: 扣库存；失败必须回滚扣款
    var resvID string
    if err := workflow.ExecuteActivity(ctx, a.ReserveInventory, req.OrderID, req.SKU, req.Qty).Get(ctx, &resvID); err != nil {
        _ = workflow.ExecuteActivity(ctx, a.RefundPayment, txnID).Get(ctx, nil)
        return nil, err
    }

    // Step 3: 创建物流单；失败要回滚库存和扣款
    var shipmentID string
    if err := workflow.ExecuteActivity(ctx, a.CreateShipment, req.OrderID).Get(ctx, &shipmentID); err != nil {
        _ = workflow.ExecuteActivity(ctx, a.ReleaseInventory, resvID).Get(ctx, nil)
        _ = workflow.ExecuteActivity(ctx, a.RefundPayment, txnID).Get(ctx, nil)
        return nil, err
    }

    logger.Info("OrderFulfillment done", "orderID", req.OrderID)
    return &OrderResult{
        OrderID:    req.OrderID,
        TxnID:      txnID,
        ShipmentID: shipmentID,
    }, nil
}
```

### 5.3 Worker 启动入口

`cmd/worker/main.go`:

```go
package main

import (
    "log"
    "os"
    "os/signal"
    "syscall"

    "go.temporal.io/sdk/client"
    "go.temporal.io/sdk/worker"

    "example.com/orders/activities"
    "example.com/orders/workflows"
)

func main() {
    c, err := client.Dial(client.Options{
        HostPort:  getenv("TEMPORAL_ADDRESS", "temporal-frontend.example.com:7233"),
        Namespace: getenv("TEMPORAL_NAMESPACE", "order"),
    })
    if err != nil {
        log.Fatalf("dial temporal: %v", err)
    }
    defer c.Close()

    // Activity 依赖注入：真实 worker 里 PaymentClient 等是 gRPC stub
    acts := &activities.OrderActivities{
        PaymentClient:   newPaymentClient(),
        InventoryClient: newInventoryClient(),
        ShippingClient:  newShippingClient(),
    }

    w := worker.New(c, "order-fulfillment", worker.Options{
        MaxConcurrentActivityExecutionSize:     200,
        MaxConcurrentWorkflowTaskExecutionSize: 100,
    })

    w.RegisterWorkflow(workflows.OrderFulfillmentWorkflow)
    w.RegisterActivity(acts)

    // 优雅退出
    stop := make(chan os.Signal, 1)
    signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
    go func() {
        <-stop
        w.Stop()
    }()

    if err := w.Run(worker.InterruptCh()); err != nil {
        log.Fatalf("worker run: %v", err)
    }
}

func getenv(k, def string) string {
    if v := os.Getenv(k); v != "" {
        return v
    }
    return def
}
```

### 5.4 启动一次 workflow

`cmd/starter/main.go`:

```go
package main

import (
    "context"
    "log"

    "go.temporal.io/sdk/client"

    "example.com/orders/workflows"
)

func main() {
    c, err := client.Dial(client.Options{
        HostPort:  "temporal-frontend.example.com:7233",
        Namespace: "order",
    })
    if err != nil {
        log.Fatalf("dial: %v", err)
    }
    defer c.Close()

    req := workflows.OrderRequest{
        OrderID: "ord-20260408-0001",
        UserID:  "u-1001",
        SKU:     "sku-book-a",
        Qty:     1,
        Amount:  9900,
    }

    run, err := c.ExecuteWorkflow(context.Background(),
        client.StartWorkflowOptions{
            ID:        "order-" + req.OrderID,
            TaskQueue: "order-fulfillment",
        },
        workflows.OrderFulfillmentWorkflow, req)
    if err != nil {
        log.Fatalf("start workflow: %v", err)
    }

    var result workflows.OrderResult
    if err := run.Get(context.Background(), &result); err != nil {
        log.Fatalf("workflow failed: %v", err)
    }
    log.Printf("done: %+v", result)
}
```

跑起来，你就有了一个"会自动重试、会持久化、进程挂了能恢复"的订单履约流程。

## 六、确定性约束：最容易踩的坑

上面那个 workflow 函数有一条隐形规则：**它必须是确定性的**。这是 Temporal 最违反直觉的一点，新人十有八九要踩坑。

### 6.1 为什么必须确定性

再看一次 event history 的工作原理：

1. 第一次执行 workflow，worker 跑到 `ExecuteActivity(ChargePayment)`，server 把这件事记到 history，派发 activity。
2. Activity 完成，server 在 history 追加"ActivityCompleted, result=txn-abc"。
3. 下一次 workflow task 进来，worker **从头重新执行 workflow 函数**，一路走到 `ExecuteActivity(ChargePayment)` 时，**不会真的再调**，而是从 history 里读出"这一步当时返回 txn-abc"，把 future 填上结果，继续往下跑。
4. 一直 replay 到还没发生过的那一行，才真正产生新的决策。

这个 replay 机制要求 workflow 函数每次执行都走**完全相同的分支、调**完全相同的 Activity、按**完全相同的顺序**。否则 replay 出来的 history 对不上 server 存的 history，worker 直接抛 `Non-Deterministic Error`（业内简称 NDE），workflow 卡死。

### 6.2 禁止的操作

直接列清单，牢记：

- **`time.Now()`**：每次 replay 拿到的时间不同。用 `workflow.Now(ctx)`。
- **`math/rand` 直接用**：随机数每次不同。用 `workflow.SideEffect` 或 `workflow.NewRandom`。
- **`uuid.NewRandom()` 直接用**：同上，包进 SideEffect 或 Activity。
- **`os.Getenv`, 读配置文件, 读数据库**：I/O 必须在 Activity 里做。
- **原生 `go func()`**：用 `workflow.Go`。
- **原生 `time.Sleep`**：用 `workflow.Sleep`。
- **原生 channel**：用 `workflow.Channel`。
- **`map` 的 range 迭代顺序**：Go map 迭代顺序随机，对 key 先 sort。
- **全局变量读写**：进程内变量不受 Temporal 管理，replay 后状态不一致。
- **引入 goroutine、mutex、原子变量做同步**：全部要换成 Temporal 自己的原语。

### 6.3 正确的写法

```go
// 错：
// id := uuid.NewString()
// now := time.Now()

// 对：
var id string
_ = workflow.SideEffect(ctx, func(ctx workflow.Context) interface{} {
    return uuid.NewString()
}).Get(&id)

now := workflow.Now(ctx)

// sleep
_ = workflow.Sleep(ctx, 30*time.Minute)

// goroutine
workflow.Go(ctx, func(ctx workflow.Context) {
    _ = workflow.ExecuteActivity(ctx, someAct).Get(ctx, nil)
})
```

`SideEffect` 是逃生舱：它告诉 Temporal"这个值第一次怎么算的，记到 history 里，下次 replay 直接返回记下来的值"。

### 6.4 用 replayer 在 CI 里做守护

人肉不可能记住所有规则，Temporal SDK 提供 replayer，把生产的 event history 抓一份下来，在 CI 里跑一遍：

```go
func TestOrderWorkflow_Replay(t *testing.T) {
    replayer := worker.NewWorkflowReplayer()
    replayer.RegisterWorkflow(workflows.OrderFulfillmentWorkflow)

    err := replayer.ReplayWorkflowHistoryFromJSONFile(nil, "testdata/order-history.json")
    if err != nil {
        t.Fatalf("replay failed (NDE?): %v", err)
    }
}
```

**每次改 workflow 代码前后都要跑一遍 replay 测试**，确保没破坏现有运行中的实例。这一步是生产项目的底线，不做就等着线上 workflow 集体卡死。

## 七、Event History 深入

### 7.1 事件类型粗略分类

Temporal event 类型几十种，常见的几类：

- **Workflow 生命周期**：`WorkflowExecutionStarted`, `WorkflowExecutionCompleted`, `WorkflowExecutionFailed`, `WorkflowExecutionTimedOut`, `WorkflowExecutionCancelRequested`, `WorkflowExecutionTerminated`, `WorkflowExecutionContinuedAsNew`
- **Workflow Task**：`WorkflowTaskScheduled`, `WorkflowTaskStarted`, `WorkflowTaskCompleted`, `WorkflowTaskFailed`, `WorkflowTaskTimedOut`
- **Activity Task**：`ActivityTaskScheduled`, `ActivityTaskStarted`, `ActivityTaskCompleted`, `ActivityTaskFailed`, `ActivityTaskTimedOut`, `ActivityTaskCancelRequested`
- **Timer**：`TimerStarted`, `TimerFired`, `TimerCanceled`
- **Signal/Query**：`WorkflowExecutionSignaled`, `MarkerRecorded`

### 7.2 History 大小的限制

**硬限制**：单个 workflow 实例的 event history 不能超过 51200 个 event 或 50 MiB（Temporal 官方默认值，可配但不建议调）。超过就会强制 Terminate。

**软限制**：到 10240 events 或 10 MiB 时 server 会推荐你 `ContinueAsNew`。

这个限制意味着：**你不能写一个"常驻"workflow 把所有订单塞进一个循环处理一辈子**。每个业务实例一个 workflow，长周期 workflow 要用 ContinueAsNew 截断 history（详见第十一节）。

### 7.3 Workflow Task 和 Activity Task 的分工

搞清楚谁做什么很重要：

- **Workflow Task**：是"决策任务"。worker 收到后执行 workflow 函数，决定"下一步要做什么"（比如：开一个新 activity、开一个 timer、等一个 signal、完成 workflow）。workflow task 必须很快完成，默认 StartToClose 10 秒。
- **Activity Task**：是"副作用任务"。worker 收到后执行 activity 函数，产生真实副作用，结果回传到 server 后写进 history。

一个 workflow 实例的一生就是这两种 task 交替出现。

## 八、Activity 重试策略详解

Activity 的重试是 Temporal 最实用的功能，但参数多、容易搞错。

### 8.1 RetryPolicy 字段

```go
RetryPolicy{
    InitialInterval:        time.Second,       // 第一次失败后等多久重试
    BackoffCoefficient:     2.0,               // 每次退避翻倍
    MaximumInterval:        time.Minute,       // 退避上限
    MaximumAttempts:        5,                 // 最多试几次，0 表示无限
    NonRetryableErrorTypes: []string{          // 匹配到这些错误类型直接放弃
        "out_of_stock",
        "insufficient_funds",
    },
}
```

两点特别强调：

1. **`MaximumAttempts = 0` 等于无限重试**，配合 Activity 的 `ScheduleToClose` 超时使用——告诉它"无限重试，但整体不超过 24 小时"。
2. **`NonRetryableErrorTypes` 只匹配 `ApplicationError` 的 type 字段**，不是 Go 的 error type。要用 `temporal.NewNonRetryableApplicationError` 或者 `temporal.NewApplicationErrorWithCause` 显式标记。

### 8.2 非重试错误

有些业务错误重试没意义：库存真没了、账户冻结了、参数非法。这种要显式告诉 Temporal 别重试：

```go
// 方案 A: 预设 NonRetryable 错误类型，workflow 配 NonRetryableErrorTypes
return "", temporal.NewNonRetryableApplicationError(
    "out of stock for sku "+sku,
    "out_of_stock", // type 字段，匹配 NonRetryableErrorTypes
    nil,
)

// 方案 B: 直接标记 non-retryable
return "", temporal.NewApplicationError(
    "bad request",
    "bad_request",
).(*temporal.ApplicationError) // 需要 cast 设置 nonRetryable
```

### 8.3 Heartbeat

长任务（>30 秒）**必须** heartbeat。原因：

1. Worker 挂掉时 server 没法立刻知道，它会等到 `HeartbeatTimeout` 超时才把任务重派给别的 worker。
2. Activity 里可以通过 heartbeat 传递**进度**，重启后从中断的地方续跑，省掉从头重来。

写法：

```go
func (a *OrderActivities) BulkExport(ctx context.Context, jobID string) error {
    // 读取上次的 heartbeat details
    var lastOffset int
    if activity.HasHeartbeatDetails(ctx) {
        _ = activity.GetHeartbeatDetails(ctx, &lastOffset)
    }

    for offset := lastOffset; offset < 1_000_000; offset += 1000 {
        if err := processBatch(ctx, offset); err != nil {
            return err
        }
        activity.RecordHeartbeat(ctx, offset)
    }
    return nil
}
```

Activity 选项里要配 `HeartbeatTimeout`：

```go
workflow.ActivityOptions{
    StartToCloseTimeout: time.Hour,
    HeartbeatTimeout:    30 * time.Second,
}
```

**坑**：如果你的 activity 跑了一小时但没调 `RecordHeartbeat`，那 `HeartbeatTimeout` 不会触发（没 heartbeat 就不检查），但一旦你配了 HeartbeatTimeout 又几分钟不发心跳，server 会判定"这个 activity 挂了"，把它 timeout 并重派——而原 worker 还在傻傻地跑完。结果就是**重复副作用**。

## 九、超时语义：四个 Timeout 的区别

新手看到 Activity 有四种超时会崩溃。实际生产上你只需要记住两个，但四个都要知道意思：

| Timeout | 意思 | 必须配？ |
|---|---|---|
| `StartToCloseTimeout` | Activity **开始执行后**，多久内必须完成 | 是 |
| `ScheduleToStartTimeout` | Activity **入队列**到**开始执行**之间的最大等待 | 否 |
| `ScheduleToCloseTimeout` | 从入队列到最终完成的**总时长**（含所有重试） | 否 |
| `HeartbeatTimeout` | 两次 heartbeat 的最大间隔 | 长任务必须 |

### 9.1 推荐配置

绝大多数业务场景只需要配 `StartToCloseTimeout` + `RetryPolicy`：

```go
ao := workflow.ActivityOptions{
    StartToCloseTimeout: 30 * time.Second,  // 单次执行不能超过 30s
    RetryPolicy: &temporal.RetryPolicy{
        InitialInterval:    time.Second,
        BackoffCoefficient: 2.0,
        MaximumInterval:    time.Minute,
        MaximumAttempts:    5,
    },
}
```

长任务加 HeartbeatTimeout：

```go
ao := workflow.ActivityOptions{
    StartToCloseTimeout: time.Hour,
    HeartbeatTimeout:    30 * time.Second,
    RetryPolicy: &temporal.RetryPolicy{
        MaximumAttempts: 3,
    },
}
```

对整体完成时间有要求时加 ScheduleToClose：

```go
ao := workflow.ActivityOptions{
    StartToCloseTimeout:    time.Minute,
    ScheduleToCloseTimeout: 10 * time.Minute, // 不管重试多少次，总共不超过 10 分钟
}
```

**不要同时配所有 timeout**，互相冲突时很难调。

### 9.2 StartToClose 太小的后果

生产最常见的 bug：`StartToClose = 10s` 但下游接口 P99 是 15s，于是所有请求都会先失败重试一次再成功，下游流量 double。配之前务必先看下游的 P99 延迟。

## 十、Signal 与 Query

Workflow 运行中经常需要和外部交互：用户取消订单、管理员调整参数、前端轮询状态。

### 10.1 Signal：外部往 workflow 推事件

```go
const SignalCancelOrder = "cancel-order"

func OrderFulfillmentWorkflow(ctx workflow.Context, req OrderRequest) (*OrderResult, error) {
    cancelCh := workflow.GetSignalChannel(ctx, SignalCancelOrder)

    ao := workflow.ActivityOptions{StartToCloseTimeout: 30 * time.Second}
    ctx = workflow.WithActivityOptions(ctx, ao)

    var a *activities.OrderActivities

    // Step 1: 扣款
    var txnID string
    if err := workflow.ExecuteActivity(ctx, a.ChargePayment, req.OrderID, req.Amount).Get(ctx, &txnID); err != nil {
        return nil, err
    }

    // 等库存扣减，同时接受取消 signal
    var resvID string
    stockFuture := workflow.ExecuteActivity(ctx, a.ReserveInventory, req.OrderID, req.SKU, req.Qty)

    sel := workflow.NewSelector(ctx)
    var canceled bool
    sel.AddReceive(cancelCh, func(c workflow.ReceiveChannel, more bool) {
        var reason string
        c.Receive(ctx, &reason)
        canceled = true
    })
    sel.AddFuture(stockFuture, func(f workflow.Future) {
        _ = f.Get(ctx, &resvID)
    })
    sel.Select(ctx)

    if canceled {
        _ = workflow.ExecuteActivity(ctx, a.RefundPayment, txnID).Get(ctx, nil)
        return nil, temporal.NewApplicationError("order canceled", "canceled")
    }

    // ... 继续后续步骤
    return nil, nil
}
```

外部发送 signal：

```go
_ = c.SignalWorkflow(context.Background(),
    "order-ord-20260408-0001", // workflow ID
    "",                         // run ID 留空 = 当前 run
    SignalCancelOrder,
    "user requested")
```

### 10.2 Query：外部读取 workflow 状态

Query 不会修改 workflow 状态（也不允许修改），只是让外部能看到当前进度：

```go
const QueryStatus = "status"

type OrderStatus struct {
    Step    string
    TxnID   string
    ResvID  string
}

func OrderFulfillmentWorkflow(ctx workflow.Context, req OrderRequest) (*OrderResult, error) {
    status := &OrderStatus{Step: "init"}

    if err := workflow.SetQueryHandler(ctx, QueryStatus, func() (*OrderStatus, error) {
        return status, nil
    }); err != nil {
        return nil, err
    }

    // 后面每推进一步更新 status.Step
    status.Step = "charging"
    // ...
    return nil, nil
}
```

查询：

```go
resp, _ := c.QueryWorkflow(ctx, "order-ord-xxx", "", QueryStatus)
var status workflows.OrderStatus
_ = resp.Get(&status)
fmt.Println(status.Step)
```

### 10.3 Update（较新特性）

Temporal 较新版本加入了 Update API，介于 Signal 和 Query 之间：**能改状态、能返回值、带校验**。适合"请求-响应"语义的交互（比如调整订单金额并返回新金额）。不在本文重点。

## 十一、Child Workflow 与 ContinueAsNew

### 11.1 Child Workflow

需要把一个复杂子流程拆出来独立复用时用 child workflow。从父 workflow 里调：

```go
cwo := workflow.ChildWorkflowOptions{
    WorkflowID: "shipment-" + req.OrderID,
    TaskQueue:  "shipping",
}
ctx = workflow.WithChildOptions(ctx, cwo)
var shipmentID string
if err := workflow.ExecuteChildWorkflow(ctx, ShippingWorkflow, req.OrderID).Get(ctx, &shipmentID); err != nil {
    return nil, err
}
```

**child workflow 有独立的 event history、独立的 workflowID**，可以被独立查询、重试、取消。父子之间通过 future 同步。

注意：child workflow 的 signal、query 要直接发到 child 的 workflowID，不是父的。

### 11.2 长生命周期 workflow 的 history 膨胀

假设你要写一个"用户订阅"workflow，每月扣费一次持续 10 年——120 次循环很快就把 history 干爆。`ContinueAsNew` 的作用是：**主动结束当前 run，开一个新 run，继续跑同样的 workflow 但 history 从零开始**。

```go
func SubscriptionWorkflow(ctx workflow.Context, state SubState) error {
    for i := 0; i < 12; i++ { // 一年循环 12 次就换 run
        _ = workflow.Sleep(ctx, 30*24*time.Hour)
        _ = workflow.ExecuteActivity(ctx, ChargeMonthly, state.UserID).Get(ctx, nil)
        state.MonthsPaid++
    }
    // 开新 run，带上最新状态
    return workflow.NewContinueAsNewError(ctx, SubscriptionWorkflow, state)
}
```

对外看仍然是"同一个 workflowID"，但 runID 换了。Web UI 会显示"continued as new"链上一个 run 和下一个 run。

**判断何时 ContinueAsNew 的实用经验**：每次循环完检查 `workflow.GetInfo(ctx).GetCurrentHistoryLength()`，超过 5000 event 就换。

## 十二、幂等性：WorkflowID Reuse Policy 与 Activity 幂等键

### 12.1 WorkflowID Reuse Policy

如果两次用同一个 workflowID 启动 workflow 会发生什么？取决于 `WorkflowIDReusePolicy`：

| Policy | 行为 |
|---|---|
| `AllowDuplicate` | 同 ID 的旧 run 必须已结束，允许新 run；**默认** |
| `AllowDuplicateFailedOnly` | 旧 run 必须是失败状态才允许 |
| `RejectDuplicate` | 同 ID 永远不允许第二次 |
| `TerminateIfRunning` | 如果旧 run 还在跑，强制终止它再起新的 |

业务上推荐：**把 workflowID 绑定业务主键（订单 ID、用户 ID），用 `RejectDuplicate`**。这样天然去重，外部重复点"下单"按钮不会产生两个履约流程。

```go
client.StartWorkflowOptions{
    ID:                    "order-" + orderID,
    TaskQueue:             "order-fulfillment",
    WorkflowIDReusePolicy: enums.WORKFLOW_ID_REUSE_POLICY_REJECT_DUPLICATE,
}
```

### 12.2 Activity 幂等键

Activity 会重试，所以必须幂等。幂等的实现分两类：

1. **下游原生幂等**：支付网关支持 idempotency key，把 workflow 传下来的 ID 直接用上。
2. **下游不幂等，你要自己做**：在 Activity 开头先查"这个操作有没有做过"，做过直接返回上次结果。

```go
func (a *OrderActivities) ChargePayment(ctx context.Context, orderID string, amount int64) (string, error) {
    // 用 orderID 做幂等键
    idempotencyKey := "charge-" + orderID
    return a.PaymentClient.ChargeWithKey(ctx, idempotencyKey, amount)
}
```

**重要提醒**：`activity.GetInfo(ctx).Attempt` 是当前重试次数，但它**不适合做幂等键**，因为重试 attempt 会变。幂等键必须只和业务输入有关，而非执行轮次。

## 十三、Saga 补偿模式：用 defer 写回滚

前面第五节的 OrderFulfillment 手写了三组 if/else 补偿，重复劳动。Temporal 的惯用手法是用 `defer` + 补偿栈：

```go
package workflows

import (
    "time"

    "go.temporal.io/sdk/temporal"
    "go.temporal.io/sdk/workflow"

    "example.com/orders/activities"
)

// compensation 是一个待执行的补偿动作
type compensation struct {
    name string
    fn   func(ctx workflow.Context) error
}

// saga 收集补偿链
type saga struct {
    comps []compensation
}

func (s *saga) add(name string, fn func(ctx workflow.Context) error) {
    s.comps = append(s.comps, compensation{name, fn})
}

// compensate 从后往前执行所有补偿；单个失败继续执行下一个
func (s *saga) compensate(ctx workflow.Context) {
    logger := workflow.GetLogger(ctx)
    for i := len(s.comps) - 1; i >= 0; i-- {
        c := s.comps[i]
        logger.Info("compensate", "name", c.name)
        if err := c.fn(ctx); err != nil {
            logger.Error("compensate failed", "name", c.name, "err", err)
        }
    }
}

func OrderFulfillmentSagaWorkflow(ctx workflow.Context, req OrderRequest) (*OrderResult, error) {
    ao := workflow.ActivityOptions{
        StartToCloseTimeout: 30 * time.Second,
        RetryPolicy: &temporal.RetryPolicy{
            InitialInterval:    time.Second,
            BackoffCoefficient: 2.0,
            MaximumInterval:    time.Minute,
            MaximumAttempts:    5,
        },
    }
    ctx = workflow.WithActivityOptions(ctx, ao)

    s := &saga{}
    var a *activities.OrderActivities

    defer func() {
        if r := recover(); r != nil {
            s.compensate(ctx)
            panic(r)
        }
    }()

    // Step 1: 扣款
    var txnID string
    if err := workflow.ExecuteActivity(ctx, a.ChargePayment, req.OrderID, req.Amount).Get(ctx, &txnID); err != nil {
        return nil, err
    }
    s.add("refund", func(ctx workflow.Context) error {
        return workflow.ExecuteActivity(ctx, a.RefundPayment, txnID).Get(ctx, nil)
    })

    // Step 2: 扣库存
    var resvID string
    if err := workflow.ExecuteActivity(ctx, a.ReserveInventory, req.OrderID, req.SKU, req.Qty).Get(ctx, &resvID); err != nil {
        s.compensate(ctx)
        return nil, err
    }
    s.add("release-inventory", func(ctx workflow.Context) error {
        return workflow.ExecuteActivity(ctx, a.ReleaseInventory, resvID).Get(ctx, nil)
    })

    // Step 3: 创建物流单
    var shipmentID string
    if err := workflow.ExecuteActivity(ctx, a.CreateShipment, req.OrderID).Get(ctx, &shipmentID); err != nil {
        s.compensate(ctx)
        return nil, err
    }
    s.add("cancel-shipment", func(ctx workflow.Context) error {
        return workflow.ExecuteActivity(ctx, a.CancelShipment, shipmentID).Get(ctx, nil)
    })

    return &OrderResult{
        OrderID:    req.OrderID,
        TxnID:      txnID,
        ShipmentID: shipmentID,
    }, nil
}
```

这个模式的好处：

1. 每一步的补偿**紧挨着正向步骤**声明，读代码不用翻来翻去。
2. 新增步骤只需要 append 补偿函数，不会漏。
3. 补偿执行顺序天然反向。

**关键原则：补偿 Activity 自己也要是幂等的**（可能被重试多次），也要有自己的 RetryPolicy。补偿失败后要走人工介入通道——所以我们在 `compensate` 里不中断而是继续下一个，避免因一个小错误导致整个回滚半途而废。

## 十四、版本化：滚动升级 workflow 代码

你上线了 OrderFulfillmentWorkflow v1，跑了 1 万单。现在要改逻辑：加一步"风控校验"。怎么改？

**错误做法**：直接在代码里插一行：

```go
// 在 ChargePayment 前面加
_ = workflow.ExecuteActivity(ctx, a.RiskCheck, req.OrderID).Get(ctx, nil)
```

上线后，所有"已经执行到 ChargePayment 之后"的老实例 replay 时会发现：history 里没有 RiskCheck，但代码说要有——NDE，全部卡死。

**正确做法**：`workflow.GetVersion`。

```go
v := workflow.GetVersion(ctx, "add-risk-check", workflow.DefaultVersion, 1)
if v == 1 {
    if err := workflow.ExecuteActivity(ctx, a.RiskCheck, req.OrderID).Get(ctx, nil); err != nil {
        return nil, err
    }
}

// 后续 ChargePayment 保持不变
```

`GetVersion` 的语义：
- **老实例 replay**：history 里有一条 `MarkerRecorded(changeID=add-risk-check, version=DefaultVersion)`，返回 DefaultVersion，跳过 RiskCheck。
- **新实例第一次跑**：返回 max version = 1，执行 RiskCheck，同时在 history 里写 marker。
- **新实例 replay**：marker 已经在 history 里，返回 1。

多次迭代后代码会变成：

```go
v := workflow.GetVersion(ctx, "add-risk-check", workflow.DefaultVersion, 2)
if v >= 1 { /* ... */ }
if v == 2 { /* v2 的新逻辑 */ }
```

**清理旧版本**：等所有老实例都跑完、从 DB 里消失了，可以移除 `DefaultVersion` 分支，但 `GetVersion` 本身建议保留（除非你 100% 确定没有 in-flight 实例）。

## 十五、生产部署

### 15.1 Helm 安装

Temporal 官方维护 Helm chart，部署到 Kubernetes：

```bash
helm repo add temporal https://go.temporal.io/helm-charts
helm repo update

helm install temporal temporal/temporal \
  --namespace temporal \
  --create-namespace \
  --values values.yaml
```

`values.yaml` 关键项：

```yaml
server:
  replicaCount: 3
  config:
    persistence:
      default:
        driver: "sql"
        sql:
          driver: "postgres12"
          host: "pg.example.com"
          port: 5432
          database: "temporal"
          user: "temporal"
          existingSecret: "temporal-db-secret"
          maxConns: 50
          maxConnLifetime: "1h"
      visibility:
        driver: "elasticsearch"
        elasticsearch:
          version: "v7"
          url:
            scheme: "https"
            host: "es.example.com:9200"
          indices:
            visibility: "temporal_visibility_v1_prod"
    numHistoryShards: 512

cassandra:
  enabled: false
elasticsearch:
  enabled: false  # 用外部 ES
prometheus:
  enabled: true
grafana:
  enabled: true

web:
  replicaCount: 2
  ingress:
    enabled: true
    hosts:
      - temporal-ui.example.com
```

### 15.2 后端选型决策

| 决策因素 | 选 PostgreSQL | 选 Cassandra |
|---|---|---|
| 日均 workflow 启动 | < 100 万 | > 100 万 |
| 运维能力 | 只熟 RDBMS | 有 Cassandra 经验 |
| 一致性需求 | 强 | 最终 |
| 扩展方向 | 纵向 + 读副本 | 水平 |
| 团队偏好 | SQL 生态 | NoSQL 容忍 |

**落地建议**：大多数团队从 PostgreSQL 起步没问题，出现瓶颈再迁 Cassandra。迁移成本不低但可行（双写 + 切流）。

### 15.3 History Shard 数量

History shard 数量是**集群初始化时固定**的，之后不能改。选错只能重建集群。

经验值：

- 小规模试水：**512 shard**。
- 中等规模（日 QPS 几千）：**4096 shard**。
- 超大规模：**16384 shard**。

shard 越多：单 shard 负载越小，history service 水平扩展更容易；但每个 shard 都有一个 mutable state cache 的内存占用，history pod 内存 footprint 更高。

**宁多勿少**。如果不确定，直接上 4096。

### 15.4 资源建议

起步配置（3 副本高可用，PostgreSQL 后端）：

```yaml
frontend:
  replicas: 3
  resources:
    requests:
      cpu: 500m
      memory: 1Gi
    limits:
      cpu: 2
      memory: 4Gi

history:
  replicas: 3
  resources:
    requests:
      cpu: 1
      memory: 2Gi
    limits:
      cpu: 4
      memory: 8Gi

matching:
  replicas: 3
  resources:
    requests:
      cpu: 500m
      memory: 1Gi
    limits:
      cpu: 2
      memory: 4Gi

worker:
  replicas: 2
  resources:
    requests:
      cpu: 200m
      memory: 512Mi
    limits:
      cpu: 1
      memory: 2Gi
```

PostgreSQL：16 vCPU / 64 GiB / SSD，至少主从。

## 十六、容量规划

### 16.1 从 workflow 到底层资源的推算链

假设业务目标：日 **50 万**订单，每个订单 workflow 产生 **40** 个 history event，保留 **7** 天。

- event 数量：50 万 × 40 = **2000 万 event/天**
- QPS：2000 万 / 86400 ≈ **230 event/秒**（平均）
- 峰值按 3 倍：**~700 event/秒**
- 存储：一条 event 平均 1 KB → **20 GB/天**，7 天 **140 GB**

这个量级 PostgreSQL 扛得住。但要注意峰值期 history service 的写入压力，numHistoryShards 给到 512 或 1024。

### 16.2 Task Queue 粒度

- **粗粒度**：一个业务一个 task queue（比如 `order-fulfillment`）。好处是 worker 简单；坏处是不同优先级/批量任务互相影响。
- **细粒度**：按优先级拆，`order-fulfillment-high`、`order-fulfillment-low`、`order-fulfillment-bulk`。worker 订阅多个 queue 时可以给每个配不同的 concurrency。

**生产经验**：有"在线 vs 批量"两类流量时必须拆 queue。批量任务很容易把在线 worker pool 占满，导致在线请求排队。

### 16.3 Worker Pool 规模

算一下需要多少 worker：

- 每个 activity 平均执行 500ms
- 单 worker 并发 200 个 activity（`MaxConcurrentActivityExecutionSize = 200`）
- 单 worker 理论吞吐 = 200 / 0.5 = **400 activity/秒**
- 峰值 700 event/秒 ≈ 350 activity/秒
- 需要 **1 个 worker**，留 3 副本做高可用

实际远比这粗暴：考虑 CPU 限制、下游 QPS 上限、内存 footprint 等。**结论**：worker 起始 3 副本，看 metrics 按需扩。

## 十七、监控与告警

### 17.1 Server 关键指标

Temporal server 暴露 Prometheus 指标，关键几个：

| 指标 | 含义 | 阈值建议 |
|---|---|---|
| `persistence_latency` | 后端 DB 写延迟 | P99 < 50ms |
| `persistence_errors` | 后端错误 | 任何非零都告警 |
| `task_latency` | task 从入队到被 worker 拿到的时间 | P99 < 500ms |
| `service_pending_requests` | 堆积请求 | 持续上涨告警 |
| `history_size` | 单 workflow history size 分布 | P99 < 10 MiB |
| `history_count` | 单 workflow event 数分布 | P99 < 10k |
| `workflow_terminate` | 强制终止数 | 任何非零都要查 |

### 17.2 SDK 指标

Worker 进程也暴露指标：

| 指标 | 含义 |
|---|---|
| `temporal_workflow_task_execution_latency` | workflow task 执行耗时 |
| `temporal_activity_execution_latency` | activity 执行耗时 |
| `temporal_workflow_task_replay_latency` | replay 耗时 |
| `temporal_workflow_endtoend_latency` | workflow 端到端耗时 |
| `temporal_worker_task_slots_available` | 空闲槽位数 |
| `temporal_sticky_cache_hit` | sticky cache 命中率 |

### 17.3 必配告警

1. **Workflow task backlog 持续 > 1min**：worker 跟不上，要扩容。
2. **Activity 失败率 > 5%**：下游服务有问题。
3. **NDE（Non-Deterministic Error）任何发生**：立刻回滚最近一次 workflow 代码发布。
4. **history size P99 > 5 MiB**：离强制终止不远了，检查是不是漏了 ContinueAsNew。
5. **persistence error**：后端 DB 有问题，立刻上后端。
6. **sticky cache 命中率 < 80%**：worker 频繁重启或容量不够。

### 17.4 Grafana Dashboard

Temporal 社区维护官方 dashboard，Grafana.com 上 ID 14000 左右的几套是比较新的（版本会变，自行搜索 "Temporal Server" 即可）。**不要自己从零画**，官方 dashboard 覆盖 95% 场景。

## 十八、与其他编排系统协同

实际项目里你不会只用 Temporal 解决所有问题：

- **Temporal**：业务流程编排——订单履约、支付对账、审批流、长周期订阅。
- **K8s CronJob**：简单的"每天凌晨跑个脚本"——日志归档、监控聚合。
- **Argo Workflows**：数据处理流水线、模型训练、CI/CD。
- **MQ (Kafka)**：纯事件流，下游无状态消费。

**选型判据**：

1. 流程需要**状态和补偿** → Temporal
2. 流程是**事件驱动的无状态消费** → MQ
3. 流程是**数据处理 DAG** → Argo Workflows
4. 任务是**简单定时脚本** → K8s CronJob

### 18.1 Temporal 替代 CronJob 的场景

Temporal 有 Schedule API，可以当 cron 用：

```go
_, _ = c.ScheduleClient().Create(ctx, client.ScheduleOptions{
    ID: "daily-reconcile",
    Spec: client.ScheduleSpec{
        CronExpressions: []string{"0 2 * * *"},
    },
    Action: &client.ScheduleWorkflowAction{
        ID:        "reconcile",
        Workflow:  ReconcileWorkflow,
        TaskQueue: "reconcile",
    },
})
```

比 K8s CronJob 强在哪：
- 上一次没跑完不会启动下一次（可配策略）
- 有完整的执行历史和可观测性
- 失败重试、补偿、signal 全都有

比 K8s CronJob 弱在：需要引入 Temporal 依赖，学习成本高。适合**已经在用 Temporal 的团队**顺手把脚本化定时任务也收编进来。

## 十九、坑位合集

这一节是血泪史。按出现频率排序：

### 19.1 NDE（Non-Deterministic Error）

**症状**：workflow 卡住，Web UI 报 `non-deterministic workflow`。

**根因**：workflow 代码改了但 replay 不兼容。

**修复**：
1. 立刻 revert 最近一次 workflow 代码变更。
2. 用 replayer 在本地用生产 history 跑一遍，复现问题。
3. 改代码时加 `workflow.GetVersion` 保护。

### 19.2 Event History 超限

**症状**：workflow 到某个点被强制终止，错误 `workflow history size exceeds limit`。

**根因**：长生命周期 workflow 没用 ContinueAsNew。

**修复**：加 ContinueAsNew，新代码对存量数据无效，存量只能人工补偿。

**预防**：开发时用 `workflow.GetInfo(ctx).GetCurrentHistoryLength()` 在代码里主动检查，超过阈值就 ContinueAsNew。

### 19.3 Activity 永远不超时

**症状**：一个 activity 在 Web UI 显示 Running 几个小时不动。

**根因**：配了 `StartToCloseTimeout = 1h` 但 activity 进程早就挂了，server 没 heartbeat 超时检查，要等到 1h 结束才 timeout 重派。

**修复**：给长 activity 加 `HeartbeatTimeout` + 代码里定期 `RecordHeartbeat`。

### 19.4 Workflow Stuck

**症状**：workflow 半天不推进。

**排查顺序**：
1. Web UI 看 pending activities：是 activity task 没派发（task queue 空 worker）还是派发了没响应？
2. 看 worker 进程是不是还活着、task queue 名字是不是对。
3. 看 worker 的 task slot 够不够（`MaxConcurrentActivityExecutionSize`）。
4. 看 SDK 指标 `sticky_cache_miss`：sticky cache 失效 workflow 会卡一下等 replay。

### 19.5 Task Queue 倾斜

**症状**：部分 worker CPU 打满，部分空闲。

**根因**：matching service 的路由策略让热 workflow 集中在少数 partition；或者 sticky task queue 让 workflow task 总回到同一 worker。

**修复**：
- 增加 task queue partition 数（`matching.numTaskqueueReadPartitions` / `numTaskqueueWritePartitions`，默认 4，可以调到 8）。
- worker 多开实例让 sticky 更均匀。

### 19.6 TLS 证书过期

**症状**：worker 连不上 server，报 `x509: certificate has expired`。

**根因**：Temporal server 的 mTLS 证书过期，或者 client cert 过期。

**预防**：
- 告警加 SSL 过期监测。
- 用 cert-manager 自动续期。
- 定期手动验证一下 worker 到 frontend 的链路。

### 19.7 ContinueAsNew 时丢 signal

**症状**：主动 ContinueAsNew 时用户刚好发了 signal，结果新 run 收不到。

**根因**：ContinueAsNew 瞬间有 race condition，如果 signal 恰好在 Close 前到达，server 会把 workflow 变成 "WorkflowExecutionContinuedAsNew" 然后马上再起新 run，理论上 signal 会转发但有边界情况。

**缓解**：
- ContinueAsNew 前先 drain signal channel，把未处理的 signal 放进 `state`，下个 run 读取。
- 或用 child workflow 拆结构，避免长 run。

### 19.8 Activity 并发过高打爆下游

**症状**：workflow 启一堆，下游 API 限流，大量 activity 失败重试，越重试越爆。

**修复**：
- 配下游粒度的 `MaxConcurrentActivityExecutionSize`。
- 或用 task queue 隔离+限流 worker 数。
- RetryPolicy 的 `BackoffCoefficient` 调大（2.0 → 3.0）让重试稀疏。

### 19.9 Workflow 用 Go map 随机顺序

**症状**：偶发 NDE。

**根因**：

```go
for k, v := range myMap { // 迭代顺序随机
    workflow.ExecuteActivity(ctx, DoThing, k, v)
}
```

**修复**：先 sort key：

```go
keys := make([]string, 0, len(myMap))
for k := range myMap {
    keys = append(keys, k)
}
sort.Strings(keys)
for _, k := range keys {
    workflow.ExecuteActivity(ctx, DoThing, k, myMap[k])
}
```

### 19.10 workflowID reuse policy 默认值坑

**症状**：测试时发同样 workflowID 第二次启动报错 `workflow execution already started`。

**根因**：默认 `AllowDuplicate`，但"旧 run 必须已结束"。测试里经常忘记 cleanup。

**缓解**：测试用 `TerminateIfRunning` 或每次带时间戳后缀。生产严格禁止。

## 二十、落地 Checklist

把前面 19 节浓缩成一份真实项目可用的 checklist：

### 20.1 编码阶段

- [ ] Workflow 代码通过了 `go vet` 和自写的"确定性检查"（无 `time.Now`、`rand`、`go func()`、map range）
- [ ] 所有长任务 Activity 配了 `HeartbeatTimeout` 且代码里 `RecordHeartbeat`
- [ ] 所有 Activity 显式配了 `RetryPolicy`，`NonRetryableErrorTypes` 覆盖了业务非重试错误
- [ ] Saga 补偿成对出现，补偿函数本身幂等
- [ ] 关键 workflow 都实现了 Query handler 用于外部排查
- [ ] 长生命周期 workflow 用了 `ContinueAsNew`，且检查 history 大小
- [ ] 修改 workflow 代码时用了 `workflow.GetVersion` 兼容老实例
- [ ] `WorkflowID` 绑定业务主键，用 `RejectDuplicate`
- [ ] 测试套件包含 replay test，CI 里跑生产抓来的 history

### 20.2 部署阶段

- [ ] History shard 数量一次规划到位（建议 4096）
- [ ] 后端 DB 选型（PG/Cassandra）并做了压测
- [ ] 启用 Elasticsearch advanced visibility
- [ ] 启用 mTLS + 证书自动续期
- [ ] Frontend/History/Matching 各自 >= 3 副本
- [ ] Worker 独立部署，按 task queue 拆不同 pod
- [ ] PodDisruptionBudget 配好，滚动升级不中断
- [ ] Namespace 按业务线拆好，retention 和 archival 配好

### 20.3 运维阶段

- [ ] Prometheus 抓全 server + SDK 指标
- [ ] Grafana dashboard 装好官方版本
- [ ] 告警：NDE / task backlog / persistence error / history size / workflow terminate / failure rate
- [ ] Web UI 通过 VPN 或 Ingress 暴露给开发团队
- [ ] 定期演练：kill worker pod、kill history pod、DB 主备切换
- [ ] 应急手册：workflow stuck 排查、NDE 修复、扩容步骤
- [ ] 容量定期 review：每月看一次 shard 水位、DB 存储、worker 利用率

### 20.4 业务阶段

- [ ] 业务方知道怎么看 Web UI、怎么发 signal、怎么查 query
- [ ] 关键业务有 "运维开关"（signal 注入）用于人工干预
- [ ] 补偿失败进入人工通道（告警 + 工单）
- [ ] 流程版本迭代有 review 机制，避免随意改破坏 replay

## 二十一、小结

Temporal 不是银弹，它把"怎么让一段业务代码在任意环境下可靠执行"的问题用 event sourcing + 确定性 replay 的组合拳解了。代价是：

1. 要学一套新的编程模型（Workflow/Activity/Signal/Query）。
2. 要接受"不能在 workflow 里做任何 I/O"的强约束。
3. 要维护一个有状态的 server 集群（history service + 后端 DB）。
4. 要改变团队的 code review 流程——每次改 workflow 代码都要考虑 replay 兼容性。

回报是：

1. 业务流程变成一段**看得懂、改得动、测得了**的代码。
2. 失败恢复、定时器、补偿、重试一次性从业务代码里抽走。
3. 可观测性从"拼日志"变成"一眼看清整个执行时间线"。
4. 业务复杂度增加时，只需要加新的 Activity 和分支，不需要重写状态机。

在长流程业务编排这个领域，Temporal 基本是目前最成熟的开源答案。建议新项目从一个小场景切入（比如"下单"或"退款"单一流程），跑顺之后再扩到更多业务线。**不要上来就想用一个 Temporal 集群统一全公司所有长流程**——那会让引入阻力巨大且没有先例可参考。

最后放一句送给读到这里的你：**在 Temporal 里写代码的快感，来自于你终于不用再写第 18 次"if status == xxx then ..."的状态机**。

---

*参考：Temporal 官方文档 docs.temporal.io（概念、SDK 指南、部署指南各章节）。文中所有代码片段均为作者原创示例，真实项目请根据自己的业务输入输出做调整。*
