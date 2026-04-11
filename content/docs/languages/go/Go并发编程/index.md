---
title: "Go 并发编程：goroutine 与 channel 实践"
date: 2025-12-09T10:00:00+08:00
draft: false
tags: ["Go", "编程", "运维", "并发"]
categories: ["Go"]
description: "面向运维工程师的 Go 并发编程实践，涵盖 goroutine、channel、sync 包、常见并发模式及实战健康检查示例"
summary: "用 Go 并发特性加速运维工具：批量检查服务状态、并发执行 SSH 命令、控制超时与取消，都在这篇文章里"
toc: true
math: false
diagram: false
keywords: ["Go", "goroutine", "channel", "并发", "运维", "worker pool"]
params:
  reading_time: true
---

## goroutine 基础

goroutine 是 Go 的轻量级线程，由 Go runtime 调度，启动开销极小（初始栈约 2KB）。运维工具里，并发检查一批服务器是否存活，比串行快几个数量级。

```go
package main

import (
    "fmt"
    "time"
)

func checkHost(host string) {
    // 模拟网络检查
    time.Sleep(100 * time.Millisecond)
    fmt.Printf("checked: %s\n", host)
}

func main() {
    hosts := []string{"10.0.0.1", "10.0.0.2", "10.0.0.3"}

    // 串行：总耗时 = n * 100ms
    for _, h := range hosts {
        checkHost(h)
    }

    // 并发：总耗时 ≈ 100ms
    for _, host := range hosts {
        go checkHost(host) // 启动 goroutine
    }

    // 注意：main 退出会杀死所有 goroutine
    // 需要等待完成，见后面的 WaitGroup
    time.Sleep(500 * time.Millisecond)
}
```

### GMP 调度模型（简述）

- **G**（Goroutine）：协程，包含栈和执行状态
- **M**（Machine）：OS 线程
- **P**（Processor）：逻辑处理器，持有本地运行队列

Go runtime 默认 `GOMAXPROCS = CPU核数`，goroutine 在 P 的本地队列中调度，遇到阻塞（syscall、channel）自动切换，无需手动管理线程。

```bash
# 查看当前 GOMAXPROCS
GOMAXPROCS=4 go run main.go

# 在代码中设置
runtime.GOMAXPROCS(2)
```

---

## channel

channel 是 goroutine 之间通信的管道，遵循 CSP 模型：**通过通信共享内存，而不是通过共享内存通信**。

### 无缓冲 channel

发送和接收必须同步发生，适合同步信号。

```go
done := make(chan struct{})

go func() {
    fmt.Println("任务执行中...")
    time.Sleep(100 * time.Millisecond)
    close(done) // 发送完成信号
}()

<-done // 阻塞等待
fmt.Println("任务完成")
```

### 有缓冲 channel

发送方最多写入 cap 个元素不阻塞，适合解耦生产者/消费者。

```go
// 结果收集
results := make(chan string, 10)

for _, host := range hosts {
    go func(h string) {
        // 检查逻辑...
        results <- fmt.Sprintf("%s: ok", h)
    }(host)
}

for i := 0; i < len(hosts); i++ {
    fmt.Println(<-results)
}
```

### channel 方向

函数参数中明确 channel 方向，编译器会帮你检查误用。

```go
func producer(out chan<- string) { // 只能发送
    out <- "message"
}

func consumer(in <-chan string) { // 只能接收
    msg := <-in
    fmt.Println(msg)
}
```

### select + 超时

`select` 监听多个 channel，哪个先就绪就执行哪个，是 Go 并发的核心控制结构。

```go
func checkWithTimeout(host string, timeout time.Duration) (bool, error) {
    result := make(chan bool, 1)

    go func() {
        conn, err := net.DialTimeout("tcp", host+":80", timeout)
        if err != nil {
            result <- false
            return
        }
        conn.Close()
        result <- true
    }()

    select {
    case ok := <-result:
        return ok, nil
    case <-time.After(timeout):
        return false, fmt.Errorf("timeout after %v", timeout)
    }
}
```

### channel 关闭与 range

```go
jobs := make(chan string, 5)

// 生产者关闭 channel
go func() {
    for _, job := range []string{"job1", "job2", "job3"} {
        jobs <- job
    }
    close(jobs) // 关闭后，消费者读完所有数据后会收到零值
}()

// 消费者用 range 读，channel 关闭后自动退出循环
for job := range jobs {
    fmt.Println("processing:", job)
}

// 检测 channel 是否已关闭
val, ok := <-jobs
if !ok {
    fmt.Println("channel closed")
}
_ = val
```

---

## sync 包

### Mutex / RWMutex

```go
type MetricsStore struct {
    mu      sync.RWMutex
    counts  map[string]int
}

func NewMetricsStore() *MetricsStore {
    return &MetricsStore{counts: make(map[string]int)}
}

// 写操作：独占锁
func (m *MetricsStore) Inc(key string) {
    m.mu.Lock()
    defer m.mu.Unlock()
    m.counts[key]++
}

// 读操作：共享锁，允许多个 goroutine 并发读
func (m *MetricsStore) Get(key string) int {
    m.mu.RLock()
    defer m.mu.RUnlock()
    return m.counts[key]
}
```

### WaitGroup

等待一批 goroutine 全部完成。

```go
var wg sync.WaitGroup

hosts := []string{"10.0.0.1", "10.0.0.2", "10.0.0.3", "10.0.0.4"}

for _, host := range hosts {
    wg.Add(1)
    go func(h string) {
        defer wg.Done()
        // 执行检查
        fmt.Printf("checking %s\n", h)
        time.Sleep(100 * time.Millisecond)
    }(host)
}

wg.Wait()
fmt.Println("所有主机检查完毕")
```

### sync.Once

确保某段代码只执行一次，常用于单例初始化。

```go
var (
    instance *Client
    once     sync.Once
)

func GetClient() *Client {
    once.Do(func() {
        instance = &Client{
            // 初始化只发生一次，即使多个 goroutine 同时调用
            HTTPClient: &http.Client{Timeout: 30 * time.Second},
        }
    })
    return instance
}
```

### sync.Map

并发安全的 map，适合读多写少的场景（如缓存）。

```go
var cache sync.Map

// 存储
cache.Store("10.0.0.1", "healthy")

// 读取
val, ok := cache.Load("10.0.0.1")
if ok {
    fmt.Println(val.(string))
}

// 存储或返回已有值
actual, loaded := cache.LoadOrStore("10.0.0.2", "unknown")
fmt.Println(actual, loaded)

// 遍历
cache.Range(func(key, value any) bool {
    fmt.Printf("%v: %v\n", key, value)
    return true // 返回 false 停止遍历
})
```

---

## 常见并发模式

### Worker Pool

控制并发数量，避免同时打开几千个连接把目标打挂。

```go
func workerPool(hosts []string, concurrency int) []string {
    jobs := make(chan string, len(hosts))
    results := make(chan string, len(hosts))

    // 启动固定数量的 worker
    var wg sync.WaitGroup
    for i := 0; i < concurrency; i++ {
        wg.Add(1)
        go func() {
            defer wg.Done()
            for host := range jobs {
                // 模拟检查
                time.Sleep(50 * time.Millisecond)
                results <- fmt.Sprintf("%s: ok", host)
            }
        }()
    }

    // 投递任务
    for _, h := range hosts {
        jobs <- h
    }
    close(jobs)

    // 等待所有 worker 完成后关闭 results
    go func() {
        wg.Wait()
        close(results)
    }()

    // 收集结果
    var out []string
    for r := range results {
        out = append(out, r)
    }
    return out
}
```

### Fan-out / Fan-in

一个输入源，分发给多个 worker 处理，再汇总结果。

```go
func fanOut(input <-chan string, n int) []<-chan string {
    channels := make([]<-chan string, n)
    for i := 0; i < n; i++ {
        ch := make(chan string, 10)
        channels[i] = ch
        go func(out chan<- string) {
            for v := range input {
                out <- process(v)
            }
            close(out)
        }(ch)
    }
    return channels
}

func fanIn(channels ...<-chan string) <-chan string {
    merged := make(chan string, 100)
    var wg sync.WaitGroup

    for _, ch := range channels {
        wg.Add(1)
        go func(c <-chan string) {
            defer wg.Done()
            for v := range c {
                merged <- v
            }
        }(ch)
    }

    go func() {
        wg.Wait()
        close(merged)
    }()
    return merged
}

func process(s string) string { return "[processed] " + s }
```

### Context 取消

`context` 是 Go 并发的标准取消机制，应当从最顶层传入所有子 goroutine。

```go
func runWithContext(ctx context.Context, hosts []string) error {
    var wg sync.WaitGroup
    errCh := make(chan error, len(hosts))

    for _, host := range hosts {
        wg.Add(1)
        go func(h string) {
            defer wg.Done()

            // 创建带超时的子 context
            checkCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
            defer cancel()

            if err := checkHost(checkCtx, h); err != nil {
                select {
                case errCh <- fmt.Errorf("host %s: %w", h, err):
                default:
                }
            }
        }(host)
    }

    wg.Wait()
    close(errCh)

    var errs []error
    for err := range errCh {
        errs = append(errs, err)
    }
    return errors.Join(errs...)
}

func checkHost(ctx context.Context, host string) error {
    // 检查 ctx 是否已取消
    select {
    case <-ctx.Done():
        return ctx.Err()
    default:
    }

    req, err := http.NewRequestWithContext(ctx, "GET", "http://"+host+"/health", nil)
    if err != nil {
        return err
    }
    resp, err := http.DefaultClient.Do(req)
    if err != nil {
        return err
    }
    defer resp.Body.Close()
    if resp.StatusCode != http.StatusOK {
        return fmt.Errorf("unhealthy, status=%d", resp.StatusCode)
    }
    return nil
}
```

---

## 并发安全

### Data Race 检测

```bash
# 编译时开启 race detector（有性能开销，只在测试用）
go run -race main.go
go test -race ./...
go build -race -o myapp main.go
```

典型 race condition：

```go
// ❌ 多个 goroutine 并发写同一个 map
results := make(map[string]bool)
for _, host := range hosts {
    go func(h string) {
        results[h] = true // DATA RACE！
    }(host)
}

// ✅ 方案1：用 channel 收集结果
// ✅ 方案2：用 sync.Map
// ✅ 方案3：用 Mutex 保护
var mu sync.Mutex
for _, host := range hosts {
    go func(h string) {
        ok := doCheck(h)
        mu.Lock()
        results[h] = ok
        mu.Unlock()
    }(host)
}
```

### 原子操作 sync/atomic

比 Mutex 更轻量，适合计数器场景。

```go
import "sync/atomic"

var successCount int64
var failCount int64

go func() {
    if check() {
        atomic.AddInt64(&successCount, 1)
    } else {
        atomic.AddInt64(&failCount, 1)
    }
}()

total := atomic.LoadInt64(&successCount) + atomic.LoadInt64(&failCount)
fmt.Printf("success: %d, fail: %d, total: %d\n",
    atomic.LoadInt64(&successCount),
    atomic.LoadInt64(&failCount),
    total,
)

func check() bool { return true }
```

---

## 实战：并发批量检测服务健康状态

下面是一个完整的运维场景示例：并发检测一批服务的 HTTP 健康状态，支持超时控制、并发限制和结构化结果输出。

```go
package main

import (
    "context"
    "fmt"
    "net/http"
    "os"
    "sync"
    "time"
)

type CheckResult struct {
    Target   string
    Status   string // "healthy" | "unhealthy" | "timeout" | "error"
    Code     int
    Latency  time.Duration
    Error    string
}

type HealthChecker struct {
    client      *http.Client
    concurrency int
    timeout     time.Duration
}

func NewHealthChecker(concurrency int, timeout time.Duration) *HealthChecker {
    return &HealthChecker{
        client: &http.Client{
            Timeout: timeout,
            Transport: &http.Transport{
                MaxIdleConnsPerHost: concurrency,
            },
        },
        concurrency: concurrency,
        timeout:     timeout,
    }
}

func (hc *HealthChecker) Check(ctx context.Context, url string) CheckResult {
    start := time.Now()
    result := CheckResult{Target: url}

    req, err := http.NewRequestWithContext(ctx, "GET", url, nil)
    if err != nil {
        result.Status = "error"
        result.Error = err.Error()
        result.Latency = time.Since(start)
        return result
    }
    req.Header.Set("User-Agent", "health-checker/1.0")

    resp, err := hc.client.Do(req)
    result.Latency = time.Since(start)

    if err != nil {
        if ctx.Err() != nil {
            result.Status = "timeout"
            result.Error = "context deadline exceeded"
        } else {
            result.Status = "error"
            result.Error = err.Error()
        }
        return result
    }
    defer resp.Body.Close()

    result.Code = resp.StatusCode
    if resp.StatusCode >= 200 && resp.StatusCode < 300 {
        result.Status = "healthy"
    } else {
        result.Status = "unhealthy"
        result.Error = fmt.Sprintf("unexpected status code: %d", resp.StatusCode)
    }
    return result
}

func (hc *HealthChecker) CheckAll(ctx context.Context, urls []string) []CheckResult {
    jobs := make(chan string, len(urls))
    results := make(chan CheckResult, len(urls))

    var wg sync.WaitGroup
    for i := 0; i < hc.concurrency; i++ {
        wg.Add(1)
        go func() {
            defer wg.Done()
            for url := range jobs {
                // 每个检查有独立超时
                checkCtx, cancel := context.WithTimeout(ctx, hc.timeout)
                results <- hc.Check(checkCtx, url)
                cancel()
            }
        }()
    }

    for _, url := range urls {
        jobs <- url
    }
    close(jobs)

    go func() {
        wg.Wait()
        close(results)
    }()

    var out []CheckResult
    for r := range results {
        out = append(out, r)
    }
    return out
}

func printResults(results []CheckResult) {
    var healthy, unhealthy, errors int
    for _, r := range results {
        status := r.Status
        latency := r.Latency.Round(time.Millisecond)
        switch r.Status {
        case "healthy":
            healthy++
            fmt.Printf("  ✓ %-50s %s  %v\n", r.Target, status, latency)
        case "unhealthy":
            unhealthy++
            fmt.Printf("  ✗ %-50s %s  %v  (code=%d)\n", r.Target, status, latency, r.Code)
        default:
            errors++
            fmt.Printf("  ! %-50s %s  %v  (%s)\n", r.Target, status, latency, r.Error)
        }
    }
    fmt.Printf("\n总计: %d个目标  健康: %d  异常: %d  错误: %d\n",
        len(results), healthy, unhealthy, errors)
    if unhealthy > 0 || errors > 0 {
        os.Exit(1)
    }
}

func main() {
    targets := []string{
        "https://httpbin.org/status/200",
        "https://httpbin.org/status/500",
        "https://httpbin.org/delay/2",
        "https://example.com",
        "http://localhost:9999", // 不存在的服务
    }

    checker := NewHealthChecker(5, 3*time.Second)
    ctx := context.Background()

    fmt.Printf("开始检查 %d 个目标（并发: %d，超时: %v）...\n\n",
        len(targets), checker.concurrency, checker.timeout)

    start := time.Now()
    results := checker.CheckAll(ctx, targets)
    elapsed := time.Since(start)

    fmt.Printf("检查完成，耗时: %v\n\n", elapsed.Round(time.Millisecond))
    printResults(results)
}
```

运行效果：5个目标并发检查，总耗时接近最慢单个请求的耗时，而非所有请求之和。

```bash
go run main.go
# 开始检查 5 个目标（并发: 5，超时: 3s）...
# 检查完成，耗时: 1.234s
#   ✓ https://httpbin.org/status/200   healthy  234ms
#   ✗ https://httpbin.org/status/500   unhealthy 241ms  (code=500)
#   ! https://httpbin.org/delay/2      timeout   3.001s  (context deadline exceeded)
#   ✓ https://example.com              healthy  312ms
#   ! http://localhost:9999            error     1ms  (connection refused)
```
