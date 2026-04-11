---
title: "Go 标准库速查：运维工程师常用"
date: 2025-12-09T10:00:00+08:00
draft: false
tags: ["Go", "编程", "运维", "标准库"]
categories: ["Go"]
description: "Go 标准库运维向速查：os、io/bufio、strings/strconv、time、encoding/json、net/http、regexp、sort，每节含实用代码片段"
summary: "不查文档快速写出对的代码——整理了运维场景最常用的 Go 标准库用法，每节都是可直接复制的代码片段"
toc: true
math: false
diagram: false
keywords: ["Go", "标准库", "os", "json", "http", "regexp", "运维"]
params:
  reading_time: true
---

## os 包

### 文件操作

```go
import "os"

// 读取整个文件
data, err := os.ReadFile("/etc/hostname")
if err != nil {
    if errors.Is(err, os.ErrNotExist) {
        // 文件不存在
    }
    return err
}
hostname := strings.TrimSpace(string(data))

// 写入文件（覆盖）
err = os.WriteFile("/tmp/result.txt", []byte("content"), 0644)

// 打开文件（精细控制）
f, err := os.Open("/var/log/app.log")          // 只读
f, err = os.Create("/tmp/output.txt")           // 创建/截断写
f, err = os.OpenFile("/var/log/app.log",        // 追加
    os.O_APPEND|os.O_WRONLY|os.O_CREATE, 0644)
defer f.Close()

// 获取文件信息
info, err := os.Stat("/var/log/app.log")
if err != nil {
    return err
}
fmt.Printf("大小: %d bytes, 修改时间: %s, 是目录: %v\n",
    info.Size(),
    info.ModTime().Format("2006-01-02 15:04:05"),
    info.IsDir(),
)

// 文件不存在的惯用检查
if _, err := os.Stat("/tmp/lock"); os.IsNotExist(err) {
    fmt.Println("文件不存在")
}
```

### 目录操作

```go
// 创建目录
err := os.Mkdir("/tmp/mydir", 0755)        // 只创建一层
err = os.MkdirAll("/tmp/a/b/c", 0755)      // 递归创建

// 删除
err = os.Remove("/tmp/file.txt")            // 删除文件或空目录
err = os.RemoveAll("/tmp/mydir")            // 递归删除

// 重命名/移动
err = os.Rename("/tmp/old.txt", "/tmp/new.txt")

// 读取目录内容
entries, err := os.ReadDir("/var/log")
for _, entry := range entries {
    info, _ := entry.Info()
    fmt.Printf("%-30s %8d %s\n",
        entry.Name(),
        info.Size(),
        info.ModTime().Format("01-02 15:04"),
    )
}

// 临时文件
f, err := os.CreateTemp("/tmp", "ops-*.txt")
fmt.Println(f.Name()) // /tmp/ops-1234567890.txt
defer os.Remove(f.Name())

// 临时目录
dir, err := os.MkdirTemp("", "ops-work-*")
defer os.RemoveAll(dir)
```

### 环境变量

```go
// 读取
val := os.Getenv("HOME")
val, ok := os.LookupEnv("KUBECONFIG") // 区分 "未设置" 和 "设置为空"
if !ok {
    val = filepath.Join(os.Getenv("HOME"), ".kube", "config")
}

// 设置（只影响当前进程）
os.Setenv("MY_VAR", "value")
os.Unsetenv("MY_VAR")

// 获取所有环境变量
for _, env := range os.Environ() {
    parts := strings.SplitN(env, "=", 2)
    key, value := parts[0], parts[1]
    if strings.HasPrefix(key, "KUBE") {
        fmt.Printf("%s=%s\n", key, value)
    }
}
```

### 进程与信号

```go
// 退出
os.Exit(0)   // 正常退出
os.Exit(1)   // 异常退出（注意：defer 不会执行）

// 获取 PID
pid := os.Getpid()
ppid := os.Getppid()
fmt.Printf("PID: %d, PPID: %d\n", pid, ppid)

// 监听系统信号（优雅退出）
sigCh := make(chan os.Signal, 1)
signal.Notify(sigCh, syscall.SIGTERM, syscall.SIGINT)

go func() {
    sig := <-sigCh
    fmt.Printf("收到信号 %v，开始优雅退出...\n", sig)
    // 清理资源
    os.Exit(0)
}()
```

---

## io / bufio

### io 基础接口

```go
import (
    "io"
    "bytes"
    "strings"
)

// 从 Reader 读取所有内容
data, err := io.ReadAll(r)

// 限制读取量（防止恶意大文件）
data, err = io.ReadAll(io.LimitReader(r, 10*1024*1024)) // 最多 10MB

// 复制
n, err := io.Copy(dst, src) // dst: io.Writer, src: io.Reader

// 丢弃输出
io.Copy(io.Discard, resp.Body) // 读完 body 但不需要内容时（保持连接复用）

// 将 string 包装成 Reader
r := strings.NewReader("hello world")
r2 := bytes.NewReader([]byte{0x01, 0x02, 0x03})
```

### bufio.Scanner 按行读取

```go
import (
    "bufio"
    "os"
    "strings"
)

// 按行读取文件
func parseHostsFile(path string) (map[string]string, error) {
    f, err := os.Open(path)
    if err != nil {
        return nil, err
    }
    defer f.Close()

    result := make(map[string]string)
    scanner := bufio.NewScanner(f)

    for scanner.Scan() {
        line := strings.TrimSpace(scanner.Text())
        if line == "" || strings.HasPrefix(line, "#") {
            continue
        }
        fields := strings.Fields(line)
        if len(fields) >= 2 {
            ip := fields[0]
            for _, hostname := range fields[1:] {
                result[hostname] = ip
            }
        }
    }
    return result, scanner.Err()
}

// 自定义分隔符（按词分割）
scanner := bufio.NewScanner(strings.NewReader("a b c d"))
scanner.Split(bufio.ScanWords)
for scanner.Scan() {
    fmt.Println(scanner.Text())
}
```

### bufio.Writer 批量写入

```go
f, err := os.Create("/tmp/big-output.txt")
if err != nil {
    return err
}
defer f.Close()

w := bufio.NewWriter(f)
for i := 0; i < 100000; i++ {
    fmt.Fprintf(w, "line %d\n", i)
}
// 务必 Flush，否则缓冲区数据会丢失
if err := w.Flush(); err != nil {
    return err
}
```

---

## strings / strconv

### strings 常用操作

```go
import "strings"

s := "  Hello, World!  "

// 去除空白
strings.TrimSpace(s)          // "Hello, World!"
strings.Trim(s, " !")         // "Hello, World"
strings.TrimPrefix(s, "  ")  // "Hello, World!  "
strings.TrimSuffix(s, "!  ") // "  Hello, World"

// 包含/前缀/后缀
strings.Contains("kubernetes", "kube")    // true
strings.HasPrefix("nginx-abc", "nginx")   // true
strings.HasSuffix("app.log", ".log")      // true
strings.ContainsAny("abc", "aeiou")       // true（包含任意字符）

// 分割
parts := strings.Split("a:b:c", ":")         // ["a", "b", "c"]
parts = strings.SplitN("a:b:c", ":", 2)       // ["a", "b:c"]
strings.Fields("  a  b   c  ")               // ["a", "b", "c"]（按空白分割）

// 连接
strings.Join([]string{"a", "b", "c"}, ", ")  // "a, b, c"

// 替换
strings.Replace("foo foo foo", "foo", "bar", 1)  // "bar foo foo"
strings.ReplaceAll("foo foo foo", "foo", "bar")  // "bar bar bar"

// 大小写
strings.ToUpper("hello") // "HELLO"
strings.ToLower("HELLO") // "hello"

// 计数/查找
strings.Count("cheese", "e")    // 3
strings.Index("hello", "ll")    // 2
strings.LastIndex("go gopher", "go") // 3

// 构建字符串（大量拼接用 Builder，比 + 高效）
var sb strings.Builder
for i := 0; i < 100; i++ {
    fmt.Fprintf(&sb, "item-%d,", i)
}
result := strings.TrimRight(sb.String(), ",")

// 字符串是否为空/纯空白
isEmpty := strings.TrimSpace(s) == ""
```

### strconv 类型转换

```go
import "strconv"

// string → int
n, err := strconv.Atoi("42")
n64, err := strconv.ParseInt("42", 10, 64)   // base=10, bitSize=64
u64, err := strconv.ParseUint("42", 10, 64)

// int → string
s := strconv.Itoa(42)
s = strconv.FormatInt(int64(42), 10)
s = strconv.FormatInt(int64(255), 16) // "ff"（十六进制）

// string → float
f, err := strconv.ParseFloat("3.14", 64)

// float → string
s = strconv.FormatFloat(3.14159, 'f', 2, 64) // "3.14"
s = strconv.FormatFloat(3.14159, 'e', 2, 64) // "3.14e+00"

// string → bool
b, err := strconv.ParseBool("true")   // true
b, err = strconv.ParseBool("1")       // true
b, err = strconv.ParseBool("false")   // false

// bool → string
s = strconv.FormatBool(true) // "true"

// 解析端口等场景
func mustParsePort(s string) int {
    n, err := strconv.Atoi(s)
    if err != nil || n < 1 || n > 65535 {
        panic(fmt.Sprintf("invalid port: %s", s))
    }
    return n
}
```

---

## time

### 基本操作

```go
import "time"

// 当前时间
now := time.Now()
utc := time.Now().UTC()

// 时间格式化（Go 的魔法参考时间：2006-01-02 15:04:05 -0700）
now.Format("2006-01-02 15:04:05")
now.Format("2006-01-02T15:04:05Z07:00") // RFC3339
now.Format(time.RFC3339)
now.Format("01/02 15:04")               // 自定义

// 解析时间
t, err := time.Parse("2006-01-02", "2025-12-09")
t, err = time.Parse(time.RFC3339, "2025-12-09T10:00:00+08:00")

// 时区处理
loc, err := time.LoadLocation("Asia/Shanghai")
t = t.In(loc)

// 时间计算
tomorrow := now.Add(24 * time.Hour)
yesterday := now.Add(-24 * time.Hour)
nextWeek := now.AddDate(0, 0, 7)

// 计算间隔
duration := time.Since(startTime) // 等价于 time.Now().Sub(startTime)
elapsed := end.Sub(start)
fmt.Printf("耗时: %v\n", elapsed.Round(time.Millisecond))

// 时间比较
now.Before(deadline)
now.After(deadline)
now.Equal(other)

// Unix 时间戳
ts := now.Unix()       // 秒
tsMs := now.UnixMilli() // 毫秒
t2 := time.Unix(ts, 0) // 从时间戳恢复
```

### 定时器与 Ticker

```go
// 一次性定时器
timer := time.NewTimer(5 * time.Second)
select {
case <-timer.C:
    fmt.Println("5秒到了")
case <-ctx.Done():
    timer.Stop()
    return ctx.Err()
}

// 简化版（不需要提前取消时）
<-time.After(5 * time.Second)

// 周期性 Ticker（运维巡检场景）
ticker := time.NewTicker(30 * time.Second)
defer ticker.Stop()

for {
    select {
    case t := <-ticker.C:
        fmt.Printf("[%s] 执行定期检查\n", t.Format("15:04:05"))
        performCheck()
    case <-ctx.Done():
        return
    }
}
```

### 测量执行时间

```go
func withTiming(name string, fn func() error) error {
    start := time.Now()
    err := fn()
    elapsed := time.Since(start)
    if err != nil {
        fmt.Printf("[ERROR] %s 失败，耗时 %v: %v\n", name, elapsed, err)
    } else {
        fmt.Printf("[OK] %s 完成，耗时 %v\n", name, elapsed.Round(time.Millisecond))
    }
    return err
}

// 使用
withTiming("数据库备份", func() error {
    return backupDatabase()
})
```

---

## encoding/json

### 基本序列化

```go
import "encoding/json"

type PodInfo struct {
    Name      string            `json:"name"`
    Namespace string            `json:"namespace"`
    Status    string            `json:"status"`
    Labels    map[string]string `json:"labels,omitempty"` // 空时省略
    CreatedAt time.Time         `json:"createdAt"`
    Age       int               `json:"-"`                // 不序列化
}

pod := PodInfo{
    Name:      "nginx-abc",
    Namespace: "default",
    Status:    "Running",
    Labels:    map[string]string{"app": "nginx"},
}

// 序列化
data, err := json.Marshal(pod)
// 格式化输出（调试用）
data, err = json.MarshalIndent(pod, "", "  ")

// 反序列化
var p PodInfo
err = json.Unmarshal(data, &p)
```

### 处理未知字段

```go
// 保留未知字段
type FlexibleConfig struct {
    Name    string                 `json:"name"`
    Version int                    `json:"version"`
    Extra   map[string]interface{} `json:"-"`
}

func (f *FlexibleConfig) UnmarshalJSON(data []byte) error {
    // 先反序列化到 map
    var raw map[string]json.RawMessage
    if err := json.Unmarshal(data, &raw); err != nil {
        return err
    }

    if v, ok := raw["name"]; ok {
        json.Unmarshal(v, &f.Name)
    }
    if v, ok := raw["version"]; ok {
        json.Unmarshal(v, &f.Version)
    }

    f.Extra = make(map[string]interface{})
    for k, v := range raw {
        if k != "name" && k != "version" {
            var val interface{}
            json.Unmarshal(v, &val)
            f.Extra[k] = val
        }
    }
    return nil
}
```

### 流式解码（大文件）

```go
// 不要 ReadAll 再 Unmarshal，对大 JSON 文件用 Decoder
f, err := os.Open("large.json")
if err != nil {
    return err
}
defer f.Close()

decoder := json.NewDecoder(f)
for decoder.More() {
    var item PodInfo
    if err := decoder.Decode(&item); err != nil {
        return err
    }
    process(item)
}

// 从 HTTP 响应解码（避免读到内存）
resp, err := http.Get(apiURL)
if err != nil {
    return err
}
defer resp.Body.Close()

var result APIResponse
if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
    return err
}
```

### json.RawMessage 延迟解析

```go
type Event struct {
    Type    string          `json:"type"`
    Payload json.RawMessage `json:"payload"` // 先不解析
}

var event Event
json.Unmarshal(data, &event)

// 根据 Type 再决定如何解析 Payload
switch event.Type {
case "pod_failed":
    var p PodEvent
    json.Unmarshal(event.Payload, &p)
case "node_down":
    var n NodeEvent
    json.Unmarshal(event.Payload, &n)
}
```

---

## net/http

### HTTP Server

```go
import "net/http"

func healthHandler(w http.ResponseWriter, r *http.Request) {
    w.Header().Set("Content-Type", "application/json")
    w.WriteHeader(http.StatusOK)
    json.NewEncoder(w).Encode(map[string]string{"status": "ok"})
}

func metricsHandler(w http.ResponseWriter, r *http.Request) {
    fmt.Fprintf(w, "# TYPE requests_total counter\nrequests_total 42\n")
}

func main() {
    mux := http.NewServeMux()
    mux.HandleFunc("/health", healthHandler)
    mux.HandleFunc("/metrics", metricsHandler)

    server := &http.Server{
        Addr:         ":8080",
        Handler:      mux,
        ReadTimeout:  10 * time.Second,
        WriteTimeout: 10 * time.Second,
        IdleTimeout:  120 * time.Second,
    }

    fmt.Println("Server started on :8080")
    if err := server.ListenAndServe(); err != http.ErrServerClosed {
        log.Fatal(err)
    }
}
```

### HTTP Client

```go
// 简单 GET
resp, err := http.Get("https://api.example.com/status")
if err != nil {
    return err
}
defer resp.Body.Close()
body, _ := io.ReadAll(resp.Body)

// 带超时的 Client（生产必须设置 Timeout）
client := &http.Client{Timeout: 10 * time.Second}
resp, err = client.Get("https://api.example.com/status")

// 自定义请求头
req, err := http.NewRequest("GET", "https://api.example.com/pods", nil)
req.Header.Set("Authorization", "Bearer "+token)
req.Header.Set("Accept", "application/json")
resp, err = client.Do(req)
```

---

## regexp

```go
import "regexp"

// 编译正则（推荐用 MustCompile，启动时失败比运行时失败好）
var (
    ipPattern  = regexp.MustCompile(`^(\d{1,3}\.){3}\d{1,3}$`)
    logPattern = regexp.MustCompile(`(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}) \[(\w+)\] (.+)`)
)

// 匹配
ipPattern.MatchString("192.168.1.1")  // true
ipPattern.MatchString("999.999.999.999") // true（只验格式，不验范围）

// 提取匹配组
line := "2025-12-09T10:00:00 [ERROR] connection refused"
matches := logPattern.FindStringSubmatch(line)
if len(matches) == 4 {
    ts, level, msg := matches[1], matches[2], matches[3]
    fmt.Printf("时间: %s, 级别: %s, 消息: %s\n", ts, level, msg)
}

// 找所有匹配
re := regexp.MustCompile(`\d+\.\d+\.\d+\.\d+`)
text := "服务器 10.0.0.1 和 10.0.0.2 均已下线"
ips := re.FindAllString(text, -1) // ["10.0.0.1", "10.0.0.2"]

// 替换
result := re.ReplaceAllString(text, "***")
// 函数替换
result = re.ReplaceAllStringFunc(text, func(s string) string {
    return "[MASKED:" + s + "]"
})

// 分割
re2 := regexp.MustCompile(`\s+`)
words := re2.Split("  hello   world  ", -1)
```

---

## sort

### 内置类型排序

```go
import "sort"

// 整数
nums := []int{5, 2, 4, 1, 3}
sort.Ints(nums)            // [1 2 3 4 5]
sort.Sort(sort.Reverse(sort.IntSlice(nums))) // [5 4 3 2 1]

// 字符串
hosts := []string{"node3", "node1", "node2"}
sort.Strings(hosts) // [node1 node2 node3]

// 检查是否已排序
sort.IntsAreSorted(nums)
sort.StringsAreSorted(hosts)

// 二分查找（有序切片）
idx := sort.SearchInts(nums, 3) // 返回 3 在有序数组中的位置
```

### 自定义排序

```go
type Pod struct {
    Name     string
    Restarts int
    Age      time.Duration
}

pods := []Pod{
    {"nginx-a", 5, 2 * time.Hour},
    {"redis-b", 0, 24 * time.Hour},
    {"app-c", 12, 30 * time.Minute},
}

// 按重启次数降序排列（重启最多的排最前面）
sort.Slice(pods, func(i, j int) bool {
    return pods[i].Restarts > pods[j].Restarts
})

// 多字段排序：先按重启次数降序，相同则按 Age 升序
sort.Slice(pods, func(i, j int) bool {
    if pods[i].Restarts != pods[j].Restarts {
        return pods[i].Restarts > pods[j].Restarts
    }
    return pods[i].Age < pods[j].Age
})

// 稳定排序（保持相等元素原有顺序）
sort.SliceStable(pods, func(i, j int) bool {
    return pods[i].Name < pods[j].Name
})

// 打印排序结果
for _, p := range pods {
    fmt.Printf("%-20s restarts=%-3d age=%v\n", p.Name, p.Restarts, p.Age)
}
```

### 实用：对 map 的 key 排序后遍历

```go
// map 遍历顺序不确定，如果需要稳定输出需先排序 key
m := map[string]int{
    "memory": 80,
    "cpu":    45,
    "disk":   92,
}

keys := make([]string, 0, len(m))
for k := range m {
    keys = append(keys, k)
}
sort.Strings(keys)

for _, k := range keys {
    fmt.Printf("%-10s %d%%\n", k, m[k])
}
// cpu        45%
// disk       92%
// memory     80%
```
