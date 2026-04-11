---
title: "Go 运维工具开发实战"
date: 2025-12-09T10:00:00+08:00
draft: false
tags: ["Go", "编程", "运维", "CLI", "Kubernetes"]
categories: ["Go"]
description: "用 Go 开发运维工具的完整实践：CLI 框架、系统命令执行、文件操作、HTTP 客户端、日志配置，附完整 K8s Pod 状态检查告警工具"
summary: "从零写一个 Go 运维工具：cobra CLI 框架、执行 kubectl 命令、调用 K8s API、配置 zap 日志、viper 配置管理，完整可运行的代码示例"
toc: true
math: false
diagram: false
keywords: ["Go", "运维工具", "cobra", "CLI", "Kubernetes", "viper", "zap"]
params:
  reading_time: true
---

## 命令行工具开发

### flag 包（内置，够用就行）

```go
package main

import (
    "flag"
    "fmt"
    "os"
)

func main() {
    host := flag.String("host", "localhost", "目标主机")
    port := flag.Int("port", 80, "端口")
    verbose := flag.Bool("verbose", false, "详细输出")
    timeout := flag.Duration("timeout", 30*time.Second, "超时时间")

    flag.Parse()

    // 非 flag 参数
    args := flag.Args()

    if *verbose {
        fmt.Printf("host=%s port=%d timeout=%v args=%v\n",
            *host, *port, *timeout, args)
    }

    if *host == "" {
        fmt.Fprintln(os.Stderr, "error: --host is required")
        flag.Usage()
        os.Exit(1)
    }
}
```

### cobra（推荐，子命令场景）

```bash
go get github.com/spf13/cobra@latest
```

标准项目结构：

```
ops-tool/
├── main.go
├── cmd/
│   ├── root.go
│   ├── check.go
│   └── deploy.go
└── internal/
    └── checker/
```

```go
// cmd/root.go
package cmd

import (
    "fmt"
    "os"

    "github.com/spf13/cobra"
    "github.com/spf13/viper"
)

var cfgFile string

var rootCmd = &cobra.Command{
    Use:   "ops-tool",
    Short: "运维工具集",
    Long:  "一套用于日常运维操作的命令行工具",
}

func Execute() {
    if err := rootCmd.Execute(); err != nil {
        fmt.Fprintln(os.Stderr, err)
        os.Exit(1)
    }
}

func init() {
    cobra.OnInitialize(initConfig)
    rootCmd.PersistentFlags().StringVar(&cfgFile, "config", "", "配置文件 (默认: $HOME/.ops-tool.yaml)")
    rootCmd.PersistentFlags().BoolP("verbose", "v", false, "详细输出")
}

func initConfig() {
    if cfgFile != "" {
        viper.SetConfigFile(cfgFile)
    } else {
        home, _ := os.UserHomeDir()
        viper.AddConfigPath(home)
        viper.SetConfigName(".ops-tool")
        viper.SetConfigType("yaml")
    }
    viper.AutomaticEnv() // 自动读取 OPS_TOOL_XXX 环境变量
    viper.ReadInConfig()
}
```

```go
// cmd/check.go
package cmd

import (
    "fmt"
    "github.com/spf13/cobra"
)

var checkCmd = &cobra.Command{
    Use:   "check [hosts...]",
    Short: "检查服务健康状态",
    Args:  cobra.MinimumNArgs(1),
    RunE: func(cmd *cobra.Command, args []string) error {
        timeout, _ := cmd.Flags().GetDuration("timeout")
        return runCheck(args, timeout)
    },
}

func init() {
    rootCmd.AddCommand(checkCmd)
    checkCmd.Flags().Duration("timeout", 5*time.Second, "检查超时时间")
    checkCmd.Flags().IntP("concurrency", "c", 10, "并发数")
}

func runCheck(hosts []string, timeout time.Duration) error {
    fmt.Printf("检查 %d 个主机，超时: %v\n", len(hosts), timeout)
    // 实际检查逻辑
    return nil
}
```

---

## os/exec 执行系统命令

### 基本用法

```go
import "os/exec"

// 捕获 stdout
func runCmd(name string, args ...string) (string, error) {
    out, err := exec.Command(name, args...).Output()
    if err != nil {
        return "", fmt.Errorf("command %s %v: %w", name, args, err)
    }
    return strings.TrimSpace(string(out)), nil
}

// 使用
version, err := runCmd("kubectl", "version", "--client", "--short")
```

### 同时捕获 stdout 和 stderr

```go
func runCmdVerbose(name string, args ...string) (stdout, stderr string, err error) {
    cmd := exec.Command(name, args...)
    var outBuf, errBuf bytes.Buffer
    cmd.Stdout = &outBuf
    cmd.Stderr = &errBuf

    err = cmd.Run()
    return outBuf.String(), errBuf.String(), err
}

// 检查退出码
out, errOut, err := runCmdVerbose("kubectl", "get", "pods", "-n", "default")
if err != nil {
    var exitErr *exec.ExitError
    if errors.As(err, &exitErr) {
        fmt.Printf("命令退出码: %d\nstderr: %s\n", exitErr.ExitCode(), errOut)
    }
    return err
}
fmt.Println(out)
```

### 带超时控制

```go
func runWithTimeout(timeout time.Duration, name string, args ...string) (string, error) {
    ctx, cancel := context.WithTimeout(context.Background(), timeout)
    defer cancel()

    cmd := exec.CommandContext(ctx, name, args...)
    out, err := cmd.CombinedOutput()

    if ctx.Err() == context.DeadlineExceeded {
        return "", fmt.Errorf("命令超时（%v）: %s %v", timeout, name, args)
    }
    if err != nil {
        return "", fmt.Errorf("命令失败: %w\noutput: %s", err, out)
    }
    return string(out), nil
}

// 执行 kubectl，最多等 10 秒
output, err := runWithTimeout(10*time.Second, "kubectl",
    "get", "pods", "-n", "production", "-o", "json")
```

### 流式输出（实时打印）

```go
func runStreaming(name string, args ...string) error {
    cmd := exec.Command(name, args...)
    cmd.Stdout = os.Stdout
    cmd.Stderr = os.Stderr
    return cmd.Run()
}

// 实时看 kubectl logs
runStreaming("kubectl", "logs", "-f", "my-pod", "-n", "default")
```

---

## 文件操作

### 读写文件

```go
// 一次性读取（文件不大时）
data, err := os.ReadFile("/etc/hosts")
if err != nil {
    return err
}

// 一次性写入
err = os.WriteFile("/tmp/report.txt", []byte("内容"), 0644)

// 追加写入
f, err := os.OpenFile("/var/log/ops.log",
    os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644)
if err != nil {
    return err
}
defer f.Close()
fmt.Fprintf(f, "%s [INFO] 操作完成\n", time.Now().Format(time.RFC3339))
```

### 按行读取大文件

```go
func readLines(path string) ([]string, error) {
    f, err := os.Open(path)
    if err != nil {
        return nil, err
    }
    defer f.Close()

    var lines []string
    scanner := bufio.NewScanner(f)
    // 如果行很长，需要增大缓冲
    scanner.Buffer(make([]byte, 1024*1024), 1024*1024)

    for scanner.Scan() {
        line := strings.TrimSpace(scanner.Text())
        if line != "" && !strings.HasPrefix(line, "#") {
            lines = append(lines, line)
        }
    }
    return lines, scanner.Err()
}
```

### 目录遍历

```go
// 遍历目录（递归）
err := filepath.Walk("/var/log", func(path string, info os.FileInfo, err error) error {
    if err != nil {
        return err // 跳过无权限目录
    }
    if info.IsDir() {
        return nil
    }
    if strings.HasSuffix(path, ".log") {
        fmt.Printf("%s  %d bytes  %s\n",
            path, info.Size(), info.ModTime().Format("2006-01-02"))
    }
    return nil
})

// Go 1.16+ 推荐用 fs.WalkDir（更高效）
err = filepath.WalkDir("/var/log", func(path string, d os.DirEntry, err error) error {
    if err != nil {
        return nil // 忽略权限错误，继续
    }
    if d.IsDir() && d.Name() == "archive" {
        return filepath.SkipDir // 跳过整个子目录
    }
    info, _ := d.Info()
    if !d.IsDir() && info.Size() > 100*1024*1024 { // > 100MB
        fmt.Printf("大文件: %s (%d MB)\n", path, info.Size()/1024/1024)
    }
    return nil
})
```

---

## HTTP 客户端

### 带超时和重试的客户端

```go
type HTTPClient struct {
    client  *http.Client
    retries int
    delay   time.Duration
}

func NewHTTPClient(timeout time.Duration, retries int) *HTTPClient {
    return &HTTPClient{
        client: &http.Client{
            Timeout: timeout,
            Transport: &http.Transport{
                MaxIdleConns:        100,
                MaxIdleConnsPerHost: 10,
                IdleConnTimeout:     90 * time.Second,
            },
        },
        retries: retries,
        delay:   500 * time.Millisecond,
    }
}

func (c *HTTPClient) Get(ctx context.Context, url string) (*http.Response, error) {
    var lastErr error
    for i := 0; i <= c.retries; i++ {
        if i > 0 {
            select {
            case <-ctx.Done():
                return nil, ctx.Err()
            case <-time.After(c.delay * time.Duration(i)): // 指数退避
            }
        }

        req, err := http.NewRequestWithContext(ctx, "GET", url, nil)
        if err != nil {
            return nil, err
        }
        req.Header.Set("User-Agent", "ops-tool/1.0")

        resp, err := c.client.Do(req)
        if err != nil {
            lastErr = err
            continue
        }

        // 5xx 错误也重试
        if resp.StatusCode >= 500 {
            resp.Body.Close()
            lastErr = fmt.Errorf("server error: %d", resp.StatusCode)
            continue
        }

        return resp, nil
    }
    return nil, fmt.Errorf("after %d retries: %w", c.retries, lastErr)
}

// 发送 JSON 请求
func (c *HTTPClient) PostJSON(ctx context.Context, url string, payload any) ([]byte, error) {
    data, err := json.Marshal(payload)
    if err != nil {
        return nil, err
    }

    req, err := http.NewRequestWithContext(ctx, "POST", url,
        bytes.NewReader(data))
    if err != nil {
        return nil, err
    }
    req.Header.Set("Content-Type", "application/json")
    req.Header.Set("Authorization", "Bearer "+os.Getenv("API_TOKEN"))

    resp, err := c.client.Do(req)
    if err != nil {
        return nil, err
    }
    defer resp.Body.Close()

    body, err := io.ReadAll(io.LimitReader(resp.Body, 10*1024*1024)) // 最多读 10MB
    if err != nil {
        return nil, err
    }

    if resp.StatusCode >= 400 {
        return nil, fmt.Errorf("API error %d: %s", resp.StatusCode, body)
    }

    return body, nil
}
```

---

## 日志配置

### 标准 log 包（简单场景）

```go
import "log"

// 设置输出格式
log.SetFlags(log.Ldate | log.Ltime | log.Lshortfile)
log.SetPrefix("[ops-tool] ")

log.Printf("检查主机 %s", host)
log.Fatalf("致命错误: %v", err) // 打印后调用 os.Exit(1)
```

### zap（生产推荐）

```bash
go get go.uber.org/zap
```

```go
import "go.uber.org/zap"

// 开发环境：彩色、易读
logger, _ := zap.NewDevelopment()
defer logger.Sync()

// 生产环境：JSON 结构化日志
logger, _ = zap.NewProduction()

// 使用
logger.Info("检查完成",
    zap.String("host", "10.0.0.1"),
    zap.Int("statusCode", 200),
    zap.Duration("latency", 234*time.Millisecond),
)

logger.Error("检查失败",
    zap.String("host", "10.0.0.2"),
    zap.Error(err),
)

// 自定义配置
cfg := zap.Config{
    Level:       zap.NewAtomicLevelAt(zap.InfoLevel),
    Development: false,
    Encoding:    "json",
    EncoderConfig: zapcore.EncoderConfig{
        TimeKey:    "ts",
        LevelKey:   "level",
        MessageKey: "msg",
        EncodeTime: zapcore.ISO8601TimeEncoder,
    },
    OutputPaths:      []string{"stdout", "/var/log/ops-tool.log"},
    ErrorOutputPaths: []string{"stderr"},
}
logger, _ = cfg.Build()
```

---

## 配置文件解析（viper）

```bash
go get github.com/spf13/viper
```

```go
// config.yaml
// server:
//   host: 0.0.0.0
//   port: 8080
// check:
//   timeout: 5s
//   concurrency: 20
// alerting:
//   webhook: https://hooks.example.com/xxx

type Config struct {
    Server struct {
        Host string `mapstructure:"host"`
        Port int    `mapstructure:"port"`
    } `mapstructure:"server"`
    Check struct {
        Timeout     time.Duration `mapstructure:"timeout"`
        Concurrency int           `mapstructure:"concurrency"`
    } `mapstructure:"check"`
    Alerting struct {
        Webhook string `mapstructure:"webhook"`
    } `mapstructure:"alerting"`
}

func LoadConfig(path string) (*Config, error) {
    viper.SetConfigFile(path)
    viper.SetConfigType("yaml")

    // 设置默认值
    viper.SetDefault("server.port", 8080)
    viper.SetDefault("check.timeout", "10s")
    viper.SetDefault("check.concurrency", 10)

    // 支持环境变量覆盖：OPS_CHECK_TIMEOUT=30s
    viper.SetEnvPrefix("OPS")
    viper.SetEnvKeyReplacer(strings.NewReplacer(".", "_"))
    viper.AutomaticEnv()

    if err := viper.ReadInConfig(); err != nil {
        if !errors.Is(err, viper.ConfigFileNotFoundError{}) {
            return nil, fmt.Errorf("读取配置文件: %w", err)
        }
        // 配置文件不存在时使用默认值
    }

    var cfg Config
    if err := viper.Unmarshal(&cfg); err != nil {
        return nil, fmt.Errorf("解析配置: %w", err)
    }
    return &cfg, nil
}
```

---

## 完整示例：K8s Pod 状态检查与告警工具

这是一个实际可运行的工具，检查指定 namespace 下的 Pod 状态，发现异常时发送钉钉 webhook 告警。

```go
// main.go
package main

import (
    "bytes"
    "context"
    "encoding/json"
    "fmt"
    "io"
    "net/http"
    "os"
    "os/exec"
    "strings"
    "time"
)

// Pod 状态信息
type PodStatus struct {
    Name      string
    Namespace string
    Phase     string
    Ready     string
    Restarts  string
    Age       string
    Node      string
    Reason    string // 异常原因
}

// 钉钉告警消息
type DingAlert struct {
    MsgType  string          `json:"msgtype"`
    Markdown DingMarkdown    `json:"markdown"`
}

type DingMarkdown struct {
    Title string `json:"title"`
    Text  string `json:"text"`
}

// 执行 kubectl 命令
func kubectl(args ...string) (string, error) {
    ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
    defer cancel()

    cmd := exec.CommandContext(ctx, "kubectl", args...)
    var outBuf, errBuf bytes.Buffer
    cmd.Stdout = &outBuf
    cmd.Stderr = &errBuf

    if err := cmd.Run(); err != nil {
        return "", fmt.Errorf("kubectl %v: %w\nstderr: %s",
            args, err, errBuf.String())
    }
    return outBuf.String(), nil
}

// 获取异常 Pod 列表
func getUnhealthyPods(namespace string) ([]PodStatus, error) {
    args := []string{
        "get", "pods",
        "-n", namespace,
        "--no-headers",
        "-o", "custom-columns=" +
            "NAME:.metadata.name," +
            "READY:.status.containerStatuses[0].ready," +
            "STATUS:.status.phase," +
            "RESTARTS:.status.containerStatuses[0].restartCount," +
            "NODE:.spec.nodeName",
    }

    out, err := kubectl(args...)
    if err != nil {
        return nil, err
    }

    var unhealthy []PodStatus
    for _, line := range strings.Split(strings.TrimSpace(out), "\n") {
        if line == "" {
            continue
        }
        fields := strings.Fields(line)
        if len(fields) < 4 {
            continue
        }

        pod := PodStatus{
            Name:      fields[0],
            Namespace: namespace,
            Ready:     fields[1],
            Phase:     fields[2],
            Restarts:  fields[3],
        }
        if len(fields) >= 5 {
            pod.Node = fields[4]
        }

        // 判断是否异常
        isUnhealthy := false
        switch {
        case pod.Phase == "Failed":
            pod.Reason = "Pod Failed"
            isUnhealthy = true
        case pod.Phase == "Pending":
            pod.Reason = "Pod Pending"
            isUnhealthy = true
        case pod.Ready == "false" || pod.Ready == "<none>":
            pod.Reason = "Container NotReady"
            isUnhealthy = true
        case pod.Restarts != "0" && pod.Restarts != "<none>":
            // 高重启次数（实际场景可设置阈值）
            pod.Reason = fmt.Sprintf("High Restarts (%s)", pod.Restarts)
            isUnhealthy = true
        }

        if isUnhealthy {
            unhealthy = append(unhealthy, pod)
        }
    }
    return unhealthy, nil
}

// 发送钉钉告警
func sendDingAlert(webhook string, pods []PodStatus, namespace string) error {
    var sb strings.Builder
    sb.WriteString(fmt.Sprintf("## K8s Pod 异常告警\n\n"))
    sb.WriteString(fmt.Sprintf("**Namespace**: %s\n", namespace))
    sb.WriteString(fmt.Sprintf("**时间**: %s\n", time.Now().Format("2006-01-02 15:04:05")))
    sb.WriteString(fmt.Sprintf("**异常Pod数量**: %d\n\n", len(pods)))
    sb.WriteString("| Pod | 状态 | Ready | 重启次数 | 原因 |\n")
    sb.WriteString("|-----|------|-------|---------|------|\n")
    for _, pod := range pods {
        sb.WriteString(fmt.Sprintf("| %s | %s | %s | %s | %s |\n",
            pod.Name, pod.Phase, pod.Ready, pod.Restarts, pod.Reason))
    }

    alert := DingAlert{
        MsgType: "markdown",
        Markdown: DingMarkdown{
            Title: fmt.Sprintf("[告警] %s 发现 %d 个异常Pod", namespace, len(pods)),
            Text:  sb.String(),
        },
    }

    body, err := json.Marshal(alert)
    if err != nil {
        return err
    }

    ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
    defer cancel()

    req, err := http.NewRequestWithContext(ctx, "POST", webhook, bytes.NewReader(body))
    if err != nil {
        return err
    }
    req.Header.Set("Content-Type", "application/json")

    resp, err := http.DefaultClient.Do(req)
    if err != nil {
        return fmt.Errorf("发送告警失败: %w", err)
    }
    defer resp.Body.Close()

    respBody, _ := io.ReadAll(resp.Body)
    if resp.StatusCode != http.StatusOK {
        return fmt.Errorf("钉钉返回错误 %d: %s", resp.StatusCode, respBody)
    }

    // 钉钉成功响应：{"errcode":0,"errmsg":"ok"}
    var result struct {
        ErrCode int    `json:"errcode"`
        ErrMsg  string `json:"errmsg"`
    }
    if err := json.Unmarshal(respBody, &result); err == nil && result.ErrCode != 0 {
        return fmt.Errorf("钉钉错误 %d: %s", result.ErrCode, result.ErrMsg)
    }

    return nil
}

func main() {
    namespace := os.Getenv("NAMESPACE")
    if namespace == "" {
        namespace = "default"
    }

    webhook := os.Getenv("DING_WEBHOOK")
    dryRun := os.Getenv("DRY_RUN") == "true"

    fmt.Printf("[%s] 开始检查 namespace: %s\n",
        time.Now().Format("15:04:05"), namespace)

    unhealthy, err := getUnhealthyPods(namespace)
    if err != nil {
        fmt.Fprintf(os.Stderr, "获取 Pod 状态失败: %v\n", err)
        os.Exit(1)
    }

    if len(unhealthy) == 0 {
        fmt.Println("所有 Pod 状态正常")
        return
    }

    fmt.Printf("发现 %d 个异常 Pod:\n", len(unhealthy))
    for _, pod := range unhealthy {
        fmt.Printf("  - %-50s [%s] Ready=%s Restarts=%s 原因:%s\n",
            pod.Name, pod.Phase, pod.Ready, pod.Restarts, pod.Reason)
    }

    if dryRun {
        fmt.Println("[DRY_RUN] 跳过告警发送")
        return
    }

    if webhook == "" {
        fmt.Fprintln(os.Stderr, "未设置 DING_WEBHOOK，跳过告警")
        os.Exit(1)
    }

    if err := sendDingAlert(webhook, unhealthy, namespace); err != nil {
        fmt.Fprintf(os.Stderr, "发送告警失败: %v\n", err)
        os.Exit(1)
    }
    fmt.Println("告警已发送")
}
```

使用方式：

```bash
# 编译
go build -o pod-checker .

# 直接运行
NAMESPACE=production DING_WEBHOOK=https://oapi.dingtalk.com/robot/send?access_token=xxx ./pod-checker

# 测试模式（不发送告警）
NAMESPACE=production DRY_RUN=true ./pod-checker

# 作为 CronJob 每分钟检查
# kubectl apply -f cronjob.yaml
```

部署为 K8s CronJob：

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: pod-checker
spec:
  schedule: "*/5 * * * *"
  jobTemplate:
    spec:
      template:
        spec:
          serviceAccountName: pod-checker
          containers:
          - name: checker
            image: your-registry/pod-checker:latest
            env:
            - name: NAMESPACE
              value: "production"
            - name: DING_WEBHOOK
              valueFrom:
                secretKeyRef:
                  name: ding-webhook
                  key: url
          restartPolicy: OnFailure
```
