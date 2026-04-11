---
title: "Go 错误处理最佳实践"
date: 2025-12-09T10:00:00+08:00
draft: false
tags: ["Go", "编程", "运维", "错误处理"]
categories: ["Go"]
description: "Go 错误处理完整指南：error 接口、错误包装、自定义错误类型、panic/recover 适用边界、常见反模式，以及运维工具中的实战错误处理策略"
summary: "在运维工具中正确处理错误：错误包装与解包、可重试判断、统一错误输出格式、带上下文的错误信息，避免常见的错误处理反模式"
toc: true
math: false
diagram: false
keywords: ["Go", "错误处理", "error", "errors.Is", "errors.As", "运维", "panic"]
params:
  reading_time: true
---

## error 接口基础

Go 的 `error` 就是一个只有一个方法的接口：

```go
type error interface {
    Error() string
}
```

任何实现了 `Error() string` 方法的类型都满足 `error` 接口。这是 Go 所有错误处理的基础。

```go
// 最简单的使用
func divide(a, b float64) (float64, error) {
    if b == 0 {
        return 0, errors.New("division by zero")
    }
    return a / b, nil
}

result, err := divide(10, 0)
if err != nil {
    fmt.Println("错误:", err)
    return
}
fmt.Println(result)
```

惯用规则：**返回 error 的函数，调用后立即检查 error**，不要跳过、不要延迟处理。

---

## errors.New vs fmt.Errorf

```go
import (
    "errors"
    "fmt"
)

// errors.New：静态错误信息，无上下文
var ErrNotFound = errors.New("not found")

// fmt.Errorf：动态信息，可以把上下文嵌入错误消息
func getConfig(key string) (string, error) {
    val, ok := store[key]
    if !ok {
        return "", fmt.Errorf("config key %q not found", key)
    }
    return val, nil
}

// %w：包装错误（保留错误链，供 errors.Is/As 使用）
func loadAndParse(path string) (*Config, error) {
    data, err := os.ReadFile(path)
    if err != nil {
        return nil, fmt.Errorf("loadAndParse: read %s: %w", path, err)
    }
    // ...
    return nil, nil
}
```

`%v` 和 `%w` 的区别：
- `%v`：只是把错误消息嵌入字符串，**断开**错误链，`errors.Is` 无法穿透
- `%w`：**保留**错误链，`errors.Is`/`errors.As` 可以往下找

---

## 错误包装与解包

### errors.Is：检查错误链

```go
var ErrNotFound = errors.New("not found")
var ErrPermission = errors.New("permission denied")

func findUser(id int) error {
    return fmt.Errorf("findUser %d: %w", id, ErrNotFound) // 包装
}

err := findUser(42)

// errors.Is 会遍历整个错误链
if errors.Is(err, ErrNotFound) {
    fmt.Println("用户不存在，可以创建")
}

// 标准库的 sentinel errors
if errors.Is(err, os.ErrNotExist) {
    fmt.Println("文件不存在")
}
if errors.Is(err, context.DeadlineExceeded) {
    fmt.Println("请求超时")
}
if errors.Is(err, context.Canceled) {
    fmt.Println("请求被取消")
}
```

### errors.As：提取特定类型的错误

```go
// 自定义错误类型
type HTTPError struct {
    StatusCode int
    Body       string
}

func (e *HTTPError) Error() string {
    return fmt.Sprintf("HTTP %d: %s", e.StatusCode, e.Body)
}

func callAPI(url string) error {
    // ...
    return fmt.Errorf("callAPI: %w", &HTTPError{StatusCode: 503, Body: "Service Unavailable"})
}

err := callAPI("https://api.example.com")

// errors.As 提取链中特定类型的错误
var httpErr *HTTPError
if errors.As(err, &httpErr) {
    fmt.Printf("HTTP 状态码: %d\n", httpErr.StatusCode)
    if httpErr.StatusCode >= 500 {
        fmt.Println("服务端错误，可以重试")
    }
}

// 提取 *os.PathError
var pathErr *os.PathError
if errors.As(err, &pathErr) {
    fmt.Printf("操作: %s, 路径: %s\n", pathErr.Op, pathErr.Path)
}
```

### 手动解包（Unwrap）

```go
// errors.Unwrap 取出包装的下一层
wrapped := fmt.Errorf("outer: %w", fmt.Errorf("inner: %w", io.EOF))
fmt.Println(errors.Unwrap(wrapped))          // "inner: EOF"
fmt.Println(errors.Unwrap(errors.Unwrap(wrapped))) // "EOF"

// errors.Join（Go 1.20+）：合并多个错误
errs := []error{
    errors.New("error 1"),
    errors.New("error 2"),
    errors.New("error 3"),
}
combined := errors.Join(errs...)
fmt.Println(combined)
// error 1
// error 2
// error 3

// errors.Is 对 Join 的结果也有效
errors.Is(combined, errs[0]) // true
```

---

## 自定义错误类型

### 携带更多上下文

```go
// 操作错误：包含操作名、资源、原因
type OperationError struct {
    Op       string // 操作名：read/write/connect
    Resource string // 操作对象：文件路径、主机名等
    Err      error  // 原始错误
}

func (e *OperationError) Error() string {
    if e.Err != nil {
        return fmt.Sprintf("%s %s: %v", e.Op, e.Resource, e.Err)
    }
    return fmt.Sprintf("%s %s: unknown error", e.Op, e.Resource)
}

// 实现 Unwrap 以支持 errors.Is/As 穿透
func (e *OperationError) Unwrap() error {
    return e.Err
}

// 使用
func readConfig(path string) (*Config, error) {
    data, err := os.ReadFile(path)
    if err != nil {
        return nil, &OperationError{
            Op:       "read",
            Resource: path,
            Err:      err,
        }
    }
    // ...
    return nil, nil
}

err := readConfig("/etc/myapp/config.yaml")
var opErr *OperationError
if errors.As(err, &opErr) {
    fmt.Printf("操作 %q 在 %q 上失败\n", opErr.Op, opErr.Resource)
    // 原始错误仍可用
    if errors.Is(opErr.Err, os.ErrNotExist) {
        fmt.Println("配置文件不存在，使用默认配置")
    }
}
```

### 可重试错误类型

```go
type RetryableError struct {
    Err       error
    RetryAfter time.Duration
}

func (e *RetryableError) Error() string {
    return fmt.Sprintf("%v (retry after %v)", e.Err, e.RetryAfter)
}

func (e *RetryableError) Unwrap() error { return e.Err }

func IsRetryable(err error) (bool, time.Duration) {
    var retryErr *RetryableError
    if errors.As(err, &retryErr) {
        return true, retryErr.RetryAfter
    }
    // 网络错误通常可重试
    var netErr net.Error
    if errors.As(err, &netErr) && netErr.Timeout() {
        return true, time.Second
    }
    return false, 0
}

// 带重试的调用
func callWithRetry(ctx context.Context, maxRetries int, fn func() error) error {
    var lastErr error
    for i := 0; i <= maxRetries; i++ {
        if err := fn(); err != nil {
            lastErr = err
            retryable, delay := IsRetryable(err)
            if !retryable || i == maxRetries {
                break
            }
            fmt.Printf("第 %d 次重试，等待 %v: %v\n", i+1, delay, err)
            select {
            case <-time.After(delay):
            case <-ctx.Done():
                return ctx.Err()
            }
            continue
        }
        return nil
    }
    return fmt.Errorf("after %d retries: %w", maxRetries, lastErr)
}
```

---

## panic / recover 适用场景

### 什么时候用 panic

**panic 不是普通错误处理机制**，只应在以下情况使用：

1. **程序初始化失败**（无法继续运行）
2. **编程错误**（nil 指针解引用、数组越界这类 bug）
3. **不变量被破坏**（"不应该发生"的状态）

```go
// 正确使用：初始化时的不可恢复错误
var db *sql.DB

func init() {
    var err error
    db, err = sql.Open("postgres", os.Getenv("DATABASE_URL"))
    if err != nil {
        panic(fmt.Sprintf("无法连接数据库: %v", err))
    }
}

// 正确使用：MustXxx 辅助函数（测试/初始化场景）
func MustParseTemplate(tmpl string) *template.Template {
    t, err := template.New("").Parse(tmpl)
    if err != nil {
        panic(err) // 模板语法错误是编程错误
    }
    return t
}

// 正确使用：断言不变量
func (s *Server) handleRequest(id int) {
    if id < 0 {
        panic(fmt.Sprintf("handleRequest: negative id %d", id))
    }
}
```

### recover 的用法

`recover` 只在 defer 函数中有效，用于捕获 panic 并转换为 error（常见于 HTTP handler）。

```go
// HTTP server 中防止一个 panic 导致整个服务崩溃
func safeHandler(h http.HandlerFunc) http.HandlerFunc {
    return func(w http.ResponseWriter, r *http.Request) {
        defer func() {
            if rec := recover(); rec != nil {
                // 记录堆栈
                buf := make([]byte, 4096)
                n := runtime.Stack(buf, false)
                log.Printf("panic: %v\n%s", rec, buf[:n])

                http.Error(w, "Internal Server Error", http.StatusInternalServerError)
            }
        }()
        h(w, r)
    }
}

// 将 panic 转换为 error（goroutine 边界）
func safeRun(fn func()) (err error) {
    defer func() {
        if rec := recover(); rec != nil {
            switch v := rec.(type) {
            case error:
                err = v
            default:
                err = fmt.Errorf("panic: %v", v)
            }
        }
    }()
    fn()
    return nil
}
```

### 不应该用 panic 的场景

```go
// ❌ 普通业务错误不要用 panic
func getUser(id int) *User {
    user, err := db.FindUser(id)
    if err != nil {
        panic(err) // 错误！应该返回 error
    }
    return user
}

// ✅ 正确方式
func getUser(id int) (*User, error) {
    return db.FindUser(id)
}

// ❌ 网络错误不要用 panic
func fetchData(url string) []byte {
    resp, err := http.Get(url)
    if err != nil {
        panic(err) // 网络错误是正常情况！
    }
    defer resp.Body.Close()
    data, _ := io.ReadAll(resp.Body)
    return data
}
```

---

## Sentinel Error 模式

Sentinel error 是包级别的预定义错误值，用于表示特定的错误状态。

```go
package checker

import "errors"

// 包级别导出的 sentinel errors
var (
    ErrHostUnreachable = errors.New("host unreachable")
    ErrTimeout         = errors.New("check timeout")
    ErrUnhealthy       = errors.New("service unhealthy")
    ErrNotConfigured   = errors.New("checker not configured")
)

// 使用 sentinel error
func (c *Checker) Check(host string) error {
    if c.client == nil {
        return ErrNotConfigured
    }

    resp, err := c.client.Get("http://" + host + "/health")
    if err != nil {
        if isTimeout(err) {
            return fmt.Errorf("check %s: %w", host, ErrTimeout)
        }
        return fmt.Errorf("check %s: %w", host, ErrHostUnreachable)
    }
    defer resp.Body.Close()

    if resp.StatusCode != 200 {
        return fmt.Errorf("check %s: %w (status=%d)", host, ErrUnhealthy, resp.StatusCode)
    }
    return nil
}

// 调用方
err := checker.Check("10.0.0.1:8080")
switch {
case errors.Is(err, checker.ErrNotConfigured):
    log.Fatal("配置错误，程序退出")
case errors.Is(err, checker.ErrTimeout):
    fmt.Println("超时，稍后重试")
case errors.Is(err, checker.ErrUnhealthy):
    sendAlert(host, err)
case err != nil:
    fmt.Printf("未知错误: %v\n", err)
}

func isTimeout(err error) bool {
    var netErr net.Error
    return errors.As(err, &netErr) && netErr.Timeout()
}
```

---

## 常见反模式

### 反模式1：忽略 error

```go
// ❌ 忽略 error
os.Remove("/tmp/lock")
io.Copy(dst, src)
json.Unmarshal(data, &v)

// ✅ 至少要 log
if err := os.Remove("/tmp/lock"); err != nil {
    log.Printf("清理锁文件失败: %v", err)
    // 根据情况决定是否 return
}
```

### 反模式2：过度包装

```go
// ❌ 每一层都包装，最终错误消息像洋葱
return fmt.Errorf("error occurred: %w", fmt.Errorf("something went wrong: %w", err))
// 输出：error occurred: something went wrong: file not found

// ✅ 每层添加有意义的上下文
return fmt.Errorf("loadConfig(%s): %w", path, err)
// 输出：loadConfig(/etc/app.yaml): open /etc/app.yaml: no such file or directory
```

### 反模式3：panic 当错误流

```go
// ❌ 把 panic 当 exception 用
func processRequest(req *Request) {
    user := mustGetUser(req.UserID) // 内部 panic
    // ...
}

func mustGetUser(id int) *User {
    user, err := db.Find(id)
    if err != nil {
        panic(err) // 当 exception 抛出
    }
    return user
}

// ✅ 正确方式
func processRequest(req *Request) error {
    user, err := getUser(req.UserID)
    if err != nil {
        return fmt.Errorf("processRequest: %w", err)
    }
    // ...
    return nil
}
```

### 反模式4：字符串比较错误

```go
// ❌ 字符串比较错误（脆弱，依赖错误消息文本）
if err.Error() == "not found" {
    // ...
}

// ✅ 用 errors.Is 或 errors.As
if errors.Is(err, ErrNotFound) {
    // ...
}
```

---

## 实战：运维工具中的错误处理策略

### 带上下文的错误链

运维工具的错误信息要让人一眼看出"哪个操作，在哪个资源上，出了什么问题"。

```go
// 错误从底层到顶层逐层添加上下文
// 最终错误消息：deploy production: scale deployment nginx: kubectl: exit status 1: Error from server: not found

func kubectl(args ...string) (string, error) {
    out, err := exec.Command("kubectl", args...).CombinedOutput()
    if err != nil {
        return "", fmt.Errorf("kubectl: %w: %s", err, strings.TrimSpace(string(out)))
    }
    return string(out), nil
}

func scaleDeployment(name, namespace string, replicas int) error {
    _, err := kubectl("scale", "deployment", name,
        "--replicas", strconv.Itoa(replicas),
        "-n", namespace)
    if err != nil {
        return fmt.Errorf("scale deployment %s: %w", name, err)
    }
    return nil
}

func deployToProduction(app string) error {
    if err := scaleDeployment(app, "production", 3); err != nil {
        return fmt.Errorf("deploy %s: %w", app, err)
    }
    return nil
}
```

### 统一错误输出格式

```go
type ExitError struct {
    Code    int
    Message string
    Cause   error
}

func (e *ExitError) Error() string {
    if e.Cause != nil {
        return fmt.Sprintf("%s: %v", e.Message, e.Cause)
    }
    return e.Message
}

func (e *ExitError) Unwrap() error { return e.Cause }

// 统一的错误输出和退出
func die(code int, format string, args ...any) {
    msg := fmt.Sprintf(format, args...)
    fmt.Fprintf(os.Stderr, "ERROR: %s\n", msg)
    os.Exit(code)
}

func main() {
    cfg, err := loadConfig(cfgPath)
    if err != nil {
        die(1, "加载配置失败: %v", err)
    }

    if err := run(cfg); err != nil {
        var exitErr *ExitError
        if errors.As(err, &exitErr) {
            fmt.Fprintf(os.Stderr, "ERROR: %v\n", exitErr)
            os.Exit(exitErr.Code)
        }
        die(1, "%v", err)
    }
}
```

### 可重试判断与错误分类

```go
type ErrorKind int

const (
    ErrKindUnknown ErrorKind = iota
    ErrKindNetwork           // 网络错误，可重试
    ErrKindTimeout           // 超时，可重试
    ErrKindPermission        // 权限错误，不可重试
    ErrKindNotFound          // 资源不存在，不可重试
    ErrKindConflict          // 冲突，需人工介入
)

func classifyError(err error) ErrorKind {
    if err == nil {
        return ErrKindUnknown
    }

    // 超时
    if errors.Is(err, context.DeadlineExceeded) {
        return ErrKindTimeout
    }

    // 网络错误
    var netErr net.Error
    if errors.As(err, &netErr) {
        if netErr.Timeout() {
            return ErrKindTimeout
        }
        return ErrKindNetwork
    }

    // 文件系统
    if errors.Is(err, os.ErrNotExist) {
        return ErrKindNotFound
    }
    if errors.Is(err, os.ErrPermission) {
        return ErrKindPermission
    }

    // HTTP 状态码
    var httpErr *HTTPError
    if errors.As(err, &httpErr) {
        switch {
        case httpErr.StatusCode == 404:
            return ErrKindNotFound
        case httpErr.StatusCode == 409:
            return ErrKindConflict
        case httpErr.StatusCode == 403:
            return ErrKindPermission
        case httpErr.StatusCode >= 500:
            return ErrKindNetwork // 服务端错误可重试
        }
    }

    return ErrKindUnknown
}

func (k ErrorKind) IsRetryable() bool {
    return k == ErrKindNetwork || k == ErrKindTimeout
}

// 在运维工具的主逻辑中使用
func runCheck(ctx context.Context, target string) error {
    err := doCheck(ctx, target)
    if err == nil {
        return nil
    }

    kind := classifyError(err)
    switch {
    case kind.IsRetryable():
        // 加入重试队列
        fmt.Printf("  [RETRY] %s: %v\n", target, err)
        retryQueue = append(retryQueue, target)
    case kind == ErrKindNotFound:
        // 告警但不致命
        fmt.Printf("  [WARN] %s: 资源不存在\n", target)
    case kind == ErrKindPermission:
        // 立即失败，需要人工处理
        return fmt.Errorf("权限不足，请检查 RBAC 配置: %w", err)
    default:
        fmt.Printf("  [ERROR] %s: %v\n", target, err)
    }
    return nil
}

// 辅助类型（在实际代码中需要定义）
type HTTPError struct {
    StatusCode int
    Body       string
}

func (e *HTTPError) Error() string {
    return fmt.Sprintf("HTTP %d: %s", e.StatusCode, e.Body)
}

var retryQueue []string

func doCheck(ctx context.Context, target string) error { return nil }
func loadConfig(path string) (interface{}, error)      { return nil, nil }
func run(cfg interface{}) error                        { return nil }

const cfgPath = "/etc/ops-tool/config.yaml"
```

### 错误收集（批量操作）

```go
// 批量操作时收集所有错误，而不是遇到第一个就返回
type MultiError struct {
    Errors []error
}

func (m *MultiError) Error() string {
    if len(m.Errors) == 0 {
        return "no errors"
    }
    msgs := make([]string, len(m.Errors))
    for i, err := range m.Errors {
        msgs[i] = err.Error()
    }
    return fmt.Sprintf("%d errors:\n  - %s",
        len(m.Errors), strings.Join(msgs, "\n  - "))
}

func (m *MultiError) Add(err error) {
    if err != nil {
        m.Errors = append(m.Errors, err)
    }
}

func (m *MultiError) ToError() error {
    if len(m.Errors) == 0 {
        return nil
    }
    return m
}

// 使用
func restartServices(services []string) error {
    var errs MultiError
    for _, svc := range services {
        if err := restartService(svc); err != nil {
            errs.Add(fmt.Errorf("restart %s: %w", svc, err))
        }
    }
    return errs.ToError()
}

func restartService(name string) error { return nil }
```
