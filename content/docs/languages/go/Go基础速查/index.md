---
title: "Go 语言基础速查（运维向）"
date: 2025-12-09T10:00:00+08:00
draft: false
tags: ["Go", "编程", "运维"]
categories: ["Go"]
description: "面向运维工程师的 Go 语言基础速查，涵盖变量、控制流、函数、数据结构、错误处理及常用标准库"
summary: "用 Go 写运维工具前必须掌握的语言基础，聚焦运维场景常用特性，配合实用代码示例"
toc: true
math: false
diagram: false
keywords: ["Go", "运维", "速查", "基础语法", "标准库"]
params:
  reading_time: true
---

## 变量、常量与类型

### 变量声明

Go 有三种声明方式，运维脚本里最常用的是短变量声明 `:=`。

```go
package main

import "fmt"

func main() {
    // 显式声明
    var host string = "192.168.1.1"
    var port int = 8080

    // 类型推断
    var timeout = 30 // int
    var ready = true // bool

    // 短变量声明（函数内部）
    addr := fmt.Sprintf("%s:%d", host, port)
    retries := 3

    fmt.Println(addr, timeout, ready, retries)

    // 批量声明
    var (
        maxConn  = 100
        logLevel = "info"
        debug    = false
    )
    fmt.Println(maxConn, logLevel, debug)
}
```

### 零值

Go 所有变量都有零值，不会出现未初始化的野指针问题。

```go
var i int       // 0
var f float64   // 0.0
var s string    // ""
var b bool      // false
var p *int      // nil
var sl []string // nil（但 len(sl) == 0 是安全的）
var m map[string]int // nil（读 nil map 不 panic，写会 panic）
```

### 常量

```go
const MaxRetry = 3
const DefaultTimeout = 30 * time.Second // 注意：这是 untyped constant

// iota 枚举
type LogLevel int
const (
    DEBUG LogLevel = iota // 0
    INFO                  // 1
    WARN                  // 2
    ERROR                 // 3
)
```

---

## 控制流

### for 循环（Go 唯一的循环）

```go
// 传统 C 风格
for i := 0; i < 10; i++ {
    fmt.Println(i)
}

// while 风格
count := 0
for count < 5 {
    count++
}

// 无限循环
for {
    // 轮询、daemon 场景常用
    time.Sleep(5 * time.Second)
    if shouldStop() {
        break
    }
}

// range 遍历
hosts := []string{"10.0.0.1", "10.0.0.2", "10.0.0.3"}
for i, host := range hosts {
    fmt.Printf("[%d] checking %s\n", i, host)
}

// 只要 key
for i := range hosts {
    fmt.Println(i)
}

// map 遍历（顺序不确定）
labels := map[string]string{"env": "prod", "app": "nginx"}
for k, v := range labels {
    fmt.Printf("%s=%s\n", k, v)
}
```

### switch

```go
env := "production"

switch env {
case "development", "dev":
    fmt.Println("开发环境")
case "staging", "pre":
    fmt.Println("预发布环境")
case "production", "prod":
    fmt.Println("生产环境")
default:
    fmt.Println("未知环境:", env)
}

// 无表达式 switch（等价于 if-else if）
port := 443
switch {
case port < 1024:
    fmt.Println("well-known port")
case port < 49152:
    fmt.Println("registered port")
default:
    fmt.Println("dynamic port")
}
```

### defer

defer 语句在函数返回前执行，常用于资源清理。多个 defer 按 LIFO 顺序执行。

```go
func readConfig(path string) ([]byte, error) {
    f, err := os.Open(path)
    if err != nil {
        return nil, err
    }
    defer f.Close() // 无论后面怎么 return，都会执行

    return io.ReadAll(f)
}

func connectDB() {
    db := openDB()
    defer db.Close()

    tx := db.Begin()
    defer tx.Rollback() // 如果 Commit 成功，Rollback 是 no-op

    // ... 操作 ...
    tx.Commit()
}
```

---

## 函数

### 多返回值

这是 Go 错误处理的核心机制。

```go
func parsePort(s string) (int, error) {
    port, err := strconv.Atoi(s)
    if err != nil {
        return 0, fmt.Errorf("invalid port %q: %w", s, err)
    }
    if port < 1 || port > 65535 {
        return 0, fmt.Errorf("port %d out of range [1, 65535]", port)
    }
    return port, nil
}

// 调用时必须处理 error
port, err := parsePort("8080")
if err != nil {
    log.Fatal(err)
}
```

### 可变参数（variadic）

```go
func logFields(level string, fields ...string) {
    fmt.Printf("[%s]", level)
    for _, f := range fields {
        fmt.Printf(" %s", f)
    }
    fmt.Println()
}

logFields("INFO", "host=10.0.0.1", "port=8080", "status=ok")

// 展开 slice 传入
args := []string{"env=prod", "app=api"}
logFields("DEBUG", args...)
```

### 命名返回值

适合给返回值加文档，或在 defer 中修改返回值。

```go
func divide(a, b float64) (result float64, err error) {
    if b == 0 {
        err = errors.New("division by zero")
        return // naked return，返回命名变量当前值
    }
    result = a / b
    return
}
```

### 函数作为值

```go
type CheckFn func(host string) bool

func runChecks(hosts []string, check CheckFn) []string {
    var failed []string
    for _, h := range hosts {
        if !check(h) {
            failed = append(failed, h)
        }
    }
    return failed
}

// 闭包
func makeTimeoutChecker(timeout time.Duration) CheckFn {
    return func(host string) bool {
        conn, err := net.DialTimeout("tcp", host+":80", timeout)
        if err != nil {
            return false
        }
        conn.Close()
        return true
    }
}
```

---

## 数组、Slice、Map

### Slice

```go
// 创建
var s []string              // nil slice
s = []string{}              // 空 slice（非 nil）
s = make([]string, 0, 10)  // 预分配容量，避免频繁扩容

// 常用操作
s = append(s, "node1")
s = append(s, "node2", "node3")

other := []string{"node4", "node5"}
s = append(s, other...) // 展开追加

// 切片
fmt.Println(s[1:3])  // [node2 node3]
fmt.Println(s[:2])   // [node1 node2]
fmt.Println(s[2:])   // [node3 node4 node5]

// 长度与容量
fmt.Println(len(s), cap(s))

// 陷阱：slice 共享底层数组
a := []int{1, 2, 3, 4, 5}
b := a[1:3]  // b = [2, 3]，与 a 共享内存
b[0] = 99   // a[1] 也变成 99！
// 需要独立副本时用 copy
c := make([]int, len(b))
copy(c, b)
```

### Map

```go
// 创建
m := make(map[string]string)
m["env"] = "prod"
m["region"] = "us-west-2"

// 字面量初始化
labels := map[string]string{
    "app":  "nginx",
    "tier": "frontend",
}

// 安全读取（检查 key 是否存在）
val, ok := labels["app"]
if !ok {
    fmt.Println("key not found")
}

// 删除
delete(labels, "tier")

// 遍历
for k, v := range labels {
    fmt.Printf("%s: %s\n", k, v)
}

// map 作为集合使用
seen := make(map[string]struct{})
hosts := []string{"a", "b", "a", "c"}
unique := make([]string, 0)
for _, h := range hosts {
    if _, exists := seen[h]; !exists {
        seen[h] = struct{}{}
        unique = append(unique, h)
    }
}
```

---

## 结构体、方法与接口

### 结构体

```go
type Server struct {
    Host    string
    Port    int
    Tags    []string
    Healthy bool
}

// 初始化
s := Server{
    Host:    "10.0.0.1",
    Port:    8080,
    Tags:    []string{"prod", "api"},
    Healthy: true,
}

// 匿名结构体（适合临时数据）
config := struct {
    Timeout int
    Retries int
}{
    Timeout: 30,
    Retries: 3,
}
_ = config
```

### 方法

```go
// 值接收者：不修改原始数据
func (s Server) Address() string {
    return fmt.Sprintf("%s:%d", s.Host, s.Port)
}

// 指针接收者：修改原始数据，或避免大结构体拷贝
func (s *Server) SetHealthy(healthy bool) {
    s.Healthy = healthy
}

// 使用
srv := &Server{Host: "10.0.0.1", Port: 8080}
fmt.Println(srv.Address())
srv.SetHealthy(false)
```

### 接口（鸭子类型）

接口由方法签名定义，只要实现了所有方法就满足接口，无需显式声明。

```go
// 定义接口
type HealthChecker interface {
    Check() (bool, error)
    Name() string
}

// HTTP 检查器
type HTTPChecker struct {
    URL     string
    Timeout time.Duration
}

func (h HTTPChecker) Check() (bool, error) {
    client := &http.Client{Timeout: h.Timeout}
    resp, err := client.Get(h.URL)
    if err != nil {
        return false, err
    }
    defer resp.Body.Close()
    return resp.StatusCode == http.StatusOK, nil
}

func (h HTTPChecker) Name() string {
    return "HTTP:" + h.URL
}

// TCP 检查器
type TCPChecker struct {
    Addr    string
    Timeout time.Duration
}

func (t TCPChecker) Check() (bool, error) {
    conn, err := net.DialTimeout("tcp", t.Addr, t.Timeout)
    if err != nil {
        return false, err
    }
    conn.Close()
    return true, nil
}

func (t TCPChecker) Name() string {
    return "TCP:" + t.Addr
}

// 统一处理
func runAllChecks(checkers []HealthChecker) {
    for _, c := range checkers {
        ok, err := c.Check()
        if err != nil {
            fmt.Printf("[ERROR] %s: %v\n", c.Name(), err)
            continue
        }
        status := "UP"
        if !ok {
            status = "DOWN"
        }
        fmt.Printf("[%s] %s\n", status, c.Name())
    }
}
```

---

## 指针基础

```go
// 取地址
x := 42
p := &x      // p 是 *int
fmt.Println(*p) // 解引用：42

*p = 100    // 通过指针修改 x
fmt.Println(x) // 100

// 函数参数传指针（修改调用方的变量）
func increment(n *int) {
    *n++
}

count := 0
increment(&count)
fmt.Println(count) // 1

// new() 分配零值
s := new(Server) // s 是 *Server，所有字段是零值
s.Host = "localhost"
```

---

## 包管理（go mod 常用命令）

```bash
# 初始化模块
go mod init github.com/yourname/ops-tools

# 添加依赖
go get github.com/spf13/cobra@latest

# 整理依赖（删除未用的，补充缺少的）
go mod tidy

# 查看依赖树
go mod graph

# 下载依赖到本地缓存
go mod download

# vendor 模式（CI 环境或离网部署）
go mod vendor
go build -mod=vendor ./...

# 升级依赖
go get -u github.com/spf13/cobra
go get -u ./... # 升级所有

# 查看可用版本
go list -m -versions github.com/spf13/cobra

# 替换依赖（本地开发 / fork）
# go.mod 中添加：
# replace github.com/original/pkg => ../local-pkg
```

---

## 错误处理惯用法

```go
// 基本模式：每次调用后检查 error
data, err := os.ReadFile("/etc/hosts")
if err != nil {
    return fmt.Errorf("读取 hosts 文件失败: %w", err)
}

// errors.Is：检查错误链中是否包含特定错误
if errors.Is(err, os.ErrNotExist) {
    // 文件不存在，不是致命错误
    data = []byte{}
}

// errors.As：提取特定类型的错误
var pathErr *os.PathError
if errors.As(err, &pathErr) {
    fmt.Println("问题路径:", pathErr.Path)
}

// 错误包装（%w 保留错误链）
func loadConfig(path string) (*Config, error) {
    data, err := os.ReadFile(path)
    if err != nil {
        return nil, fmt.Errorf("loadConfig %s: %w", path, err)
    }
    // ...
    return nil, nil
}
```

---

## 运维工程师常用标准库一览

| 包 | 主要用途 | 关键类型/函数 |
|---|---|---|
| `os` | 文件、目录、环境变量、进程、信号 | `Open`, `ReadFile`, `Getenv`, `Exit`, `Signal` |
| `io` | I/O 原语、接口定义 | `Reader`, `Writer`, `ReadAll`, `Copy` |
| `bufio` | 带缓冲的 I/O，按行读取 | `Scanner`, `NewReader`, `NewWriter` |
| `fmt` | 格式化输出、字符串构建 | `Println`, `Sprintf`, `Fprintf`, `Errorf` |
| `strings` | 字符串操作 | `Contains`, `Split`, `TrimSpace`, `HasPrefix` |
| `strconv` | 类型转换 | `Atoi`, `Itoa`, `ParseBool`, `FormatFloat` |
| `time` | 时间、定时器 | `Now`, `Since`, `Sleep`, `Ticker`, `Format` |
| `encoding/json` | JSON 序列化/反序列化 | `Marshal`, `Unmarshal`, `Decoder` |
| `net/http` | HTTP 客户端和服务端 | `Get`, `Post`, `ListenAndServe`, `Client` |
| `os/exec` | 执行外部命令 | `Command`, `Output`, `CombinedOutput` |
| `log` | 简单日志 | `Printf`, `Fatal`, `SetFlags` |
| `path/filepath` | 路径操作（跨平台） | `Join`, `Dir`, `Base`, `Walk`, `Glob` |
| `regexp` | 正则表达式 | `Compile`, `MatchString`, `FindAll` |
| `sync` | 并发同步原语 | `Mutex`, `WaitGroup`, `Once` |
| `context` | 超时、取消传播 | `WithTimeout`, `WithCancel`, `Background` |
| `flag` | 命令行参数解析 | `String`, `Int`, `Bool`, `Parse` |

---

## 快速参考：常见陷阱

```go
// ❌ 陷阱1：在循环中使用 goroutine 捕获变量
for _, host := range hosts {
    go func() {
        fmt.Println(host) // 所有 goroutine 都会打印最后一个 host
    }()
}

// ✅ 正确方式：传参
for _, host := range hosts {
    go func(h string) {
        fmt.Println(h)
    }(host)
}

// ❌ 陷阱2：nil map 写入
var m map[string]string
m["key"] = "value" // panic: assignment to entry in nil map

// ✅ 正确方式
m := make(map[string]string)
m["key"] = "value"

// ❌ 陷阱3：忽略 error
os.Remove("/tmp/test") // 如果失败，你不会知道

// ✅ 正确方式
if err := os.Remove("/tmp/test"); err != nil {
    log.Printf("删除文件失败: %v", err)
}

// ❌ 陷阱4：slice append 后仍共享底层数组
a := make([]int, 3, 5)
b := a[:2]
b = append(b, 99) // 这会修改 a[2]！

// ✅ 用 full slice expression 限制容量
b = a[:2:2] // cap(b) == 2，append 会强制分配新数组
```
