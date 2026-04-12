---
title: "用 Go 写 K8s 运维工具：client-go 实战"
date: 2025-08-25T09:08:00+08:00
draft: false
tags: ["golang", "kubernetes", "client-go", "devops-tools", "informer"]
categories: ["编程"]
description: "从 client-go 初始化、List/Watch、Informer 机制到实战运维工具开发的完整 Go K8s 编程指南。"
summary: "kubectl 能解决 80% 的日常问题，剩下 20% 需要你自己写工具。本文用实际可运行的 Go 代码，展示如何用 client-go 构建批量重启 Deployment、Pod 资源报告、过期 ConfigMap 清理等运维工具，并用 cobra 封装成 CLI。"
toc: true
math: false
diagram: false
keywords: ["client-go", "golang", "kubernetes", "controller", "informer", "运维工具"]
params:
  reading_time: true
---

## 为什么要自己写工具

`kubectl` 加上 shell 脚本能处理大多数运维需求，但遇到以下场景就有些捉襟见肘：

- 需要**跨命名空间批量操作**并输出结构化报告
- 需要**实时 Watch** 资源变化并触发自定义逻辑
- 需要将 K8s 操作集成到内部平台（审计日志、RBAC 联动等）
- 复杂的**条件过滤**（例如找出所有 CPU 请求/限制比超过 5 的 Pod）

`client-go` 是 Kubernetes 官方的 Go 客户端库，是 kubectl、controller-manager 等工具的基础。掌握它，基本上就是在写"自己的 kubectl"。

## 项目初始化

```bash
mkdir k8s-ops-tools && cd k8s-ops-tools
go mod init github.com/example/k8s-ops-tools

# 核心依赖
go get k8s.io/client-go@v0.29.3
go get k8s.io/api@v0.29.3
go get k8s.io/apimachinery@v0.29.3

# CLI 框架
go get github.com/spf13/cobra@v1.8.0

# 输出格式化
go get github.com/olekukonko/tablewriter@v0.0.5
```

`go.mod` 关键部分：

```go
require (
    k8s.io/api v0.29.3
    k8s.io/apimachinery v0.29.3
    k8s.io/client-go v0.29.3
    github.com/spf13/cobra v1.8.0
)
```

---

## client-go 初始化

`client-go` 支持两种初始化方式，需要根据运行环境选择。

### InCluster 模式（在 Pod 内运行）

```go
package k8sclient

import (
    "k8s.io/client-go/kubernetes"
    "k8s.io/client-go/rest"
)

func NewInClusterClient() (*kubernetes.Clientset, error) {
    // 自动从 Pod 的 ServiceAccount 读取 Token 和 CA
    config, err := rest.InClusterConfig()
    if err != nil {
        return nil, fmt.Errorf("InCluster config failed: %w", err)
    }
    return kubernetes.NewForConfig(config)
}
```

这种方式依赖 Pod 挂载的 ServiceAccount，需要相应的 RBAC 权限：

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: ops-tool-role
rules:
  - apiGroups: ["apps"]
    resources: ["deployments"]
    verbs: ["get", "list", "watch", "update", "patch"]
  - apiGroups: [""]
    resources: ["pods", "configmaps", "namespaces"]
    verbs: ["get", "list", "watch", "delete"]
```

### kubeconfig 模式（本地开发）

```go
package k8sclient

import (
    "os"
    "path/filepath"

    "k8s.io/client-go/kubernetes"
    "k8s.io/client-go/tools/clientcmd"
)

func NewKubeconfigClient(kubeconfig string) (*kubernetes.Clientset, error) {
    if kubeconfig == "" {
        home, _ := os.UserHomeDir()
        kubeconfig = filepath.Join(home, ".kube", "config")
    }

    config, err := clientcmd.BuildConfigFromFlags("", kubeconfig)
    if err != nil {
        return nil, fmt.Errorf("build config: %w", err)
    }

    // 调整连接参数（生产工具建议显式配置）
    config.QPS = 50
    config.Burst = 100

    return kubernetes.NewForConfig(config)
}
```

### 统一工厂（推荐）

```go
// 自动感知运行环境
func NewClient(kubeconfig string) (*kubernetes.Clientset, error) {
    // 优先 InCluster
    if config, err := rest.InClusterConfig(); err == nil {
        return kubernetes.NewForConfig(config)
    }
    return NewKubeconfigClient(kubeconfig)
}
```

---

## List 与 Watch

### 基础 List

```go
func ListPodsWithHighMemory(ctx context.Context, client *kubernetes.Clientset, namespace string, threshold int64) {
    pods, err := client.CoreV1().Pods(namespace).List(ctx, metav1.ListOptions{
        // 服务端过滤（效率高于客户端过滤）
        LabelSelector: "app=api-server",
        FieldSelector: "status.phase=Running",
    })
    if err != nil {
        log.Fatalf("list pods: %v", err)
    }

    for _, pod := range pods.Items {
        for _, container := range pod.Spec.Containers {
            memLimit := container.Resources.Limits.Memory()
            if memLimit != nil && memLimit.Value() > threshold {
                fmt.Printf("Pod: %s/%s, Container: %s, MemLimit: %s\n",
                    pod.Namespace, pod.Name, container.Name, memLimit.String())
            }
        }
    }
}
```

### Watch 资源变化

```go
func WatchPodEvents(ctx context.Context, client *kubernetes.Clientset, namespace string) error {
    watcher, err := client.CoreV1().Pods(namespace).Watch(ctx, metav1.ListOptions{
        LabelSelector: "app=api-server",
    })
    if err != nil {
        return err
    }
    defer watcher.Stop()

    for {
        select {
        case event, ok := <-watcher.ResultChan():
            if !ok {
                return fmt.Errorf("watch channel closed")
            }
            pod, ok := event.Object.(*corev1.Pod)
            if !ok {
                continue
            }
            switch event.Type {
            case watch.Added:
                fmt.Printf("[ADD] %s/%s\n", pod.Namespace, pod.Name)
            case watch.Modified:
                fmt.Printf("[MOD] %s/%s -> %s\n", pod.Namespace, pod.Name, pod.Status.Phase)
            case watch.Deleted:
                fmt.Printf("[DEL] %s/%s\n", pod.Namespace, pod.Name)
            }
        case <-ctx.Done():
            return nil
        }
    }
}
```

---

## Informer 机制

直接 Watch 有个问题：连接断开后需要自己处理重连、从 ResourceVersion 断点续传。Informer 帮你解决了这些问题，还提供了本地缓存。

```go
package informer

import (
    "context"
    "time"

    corev1 "k8s.io/api/core/v1"
    "k8s.io/client-go/informers"
    "k8s.io/client-go/kubernetes"
    "k8s.io/client-go/tools/cache"
)

func StartPodInformer(ctx context.Context, client *kubernetes.Clientset) {
    // 创建 SharedInformerFactory（所有 Informer 共享 ListWatch 连接）
    factory := informers.NewSharedInformerFactoryWithOptions(
        client,
        30*time.Second,   // resync 周期
        informers.WithNamespace("production"),
    )

    podInformer := factory.Core().V1().Pods().Informer()

    // 注册事件处理器
    podInformer.AddEventHandler(cache.ResourceEventHandlerFuncs{
        AddFunc: func(obj interface{}) {
            pod := obj.(*corev1.Pod)
            fmt.Printf("Pod 创建: %s/%s\n", pod.Namespace, pod.Name)
        },
        UpdateFunc: func(oldObj, newObj interface{}) {
            oldPod := oldObj.(*corev1.Pod)
            newPod := newObj.(*corev1.Pod)
            if oldPod.Status.Phase != newPod.Status.Phase {
                fmt.Printf("Pod 状态变化: %s/%s %s -> %s\n",
                    newPod.Namespace, newPod.Name,
                    oldPod.Status.Phase, newPod.Status.Phase)
            }
        },
        DeleteFunc: func(obj interface{}) {
            pod := obj.(*corev1.Pod)
            fmt.Printf("Pod 删除: %s/%s\n", pod.Namespace, pod.Name)
        },
    })

    // 启动，等待缓存同步
    factory.Start(ctx.Done())
    if !cache.WaitForCacheSync(ctx.Done(), podInformer.HasSynced) {
        panic("cache sync timeout")
    }

    fmt.Println("Informer 就绪，开始监听...")
    <-ctx.Done()
}
```

Informer 的本地缓存可以直接查询，无需向 API Server 发请求：

```go
lister := factory.Core().V1().Pods().Lister()
pods, err := lister.Pods("production").List(labels.Everything())
```

---

## 实战案例1：批量重启 Deployment

```go
// cmd/restart.go
package cmd

import (
    "context"
    "fmt"
    "time"

    metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
    "k8s.io/apimachinery/pkg/types"
    "github.com/spf13/cobra"
)

var restartCmd = &cobra.Command{
    Use:   "restart",
    Short: "批量重启 Deployment",
    Example: `
  # 重启 production 命名空间下 team=backend 的所有 Deployment
  k8s-ops restart --namespace production --selector team=backend

  # 重启所有命名空间（危险！需确认）
  k8s-ops restart --all-namespaces --selector app=config-hot-reload
`,
    RunE: func(cmd *cobra.Command, args []string) error {
        namespace, _ := cmd.Flags().GetString("namespace")
        selector, _ := cmd.Flags().GetString("selector")
        dryRun, _ := cmd.Flags().GetBool("dry-run")
        allNS, _ := cmd.Flags().GetBool("all-namespaces")

        if allNS {
            namespace = ""
        }

        ctx := context.Background()
        client := mustGetClient()

        deployments, err := client.AppsV1().Deployments(namespace).List(ctx, metav1.ListOptions{
            LabelSelector: selector,
        })
        if err != nil {
            return fmt.Errorf("list deployments: %w", err)
        }

        if len(deployments.Items) == 0 {
            fmt.Println("没有匹配的 Deployment")
            return nil
        }

        fmt.Printf("找到 %d 个 Deployment：\n", len(deployments.Items))
        for _, d := range deployments.Items {
            fmt.Printf("  - %s/%s\n", d.Namespace, d.Name)
        }

        if dryRun {
            fmt.Println("\n[dry-run] 未执行实际操作")
            return nil
        }

        // 通过更新 annotation 触发滚动重启（同 kubectl rollout restart）
        patchData := fmt.Sprintf(
            `{"spec":{"template":{"metadata":{"annotations":{"kubectl.kubernetes.io/restartedAt":"%s"}}}}}`,
            time.Now().Format(time.RFC3339),
        )

        for _, d := range deployments.Items {
            _, err := client.AppsV1().Deployments(d.Namespace).Patch(
                ctx, d.Name,
                types.MergePatchType,
                []byte(patchData),
                metav1.PatchOptions{},
            )
            if err != nil {
                fmt.Printf("  ✗ %s/%s: %v\n", d.Namespace, d.Name, err)
            } else {
                fmt.Printf("  ✓ %s/%s: 已触发重启\n", d.Namespace, d.Name)
            }
        }
        return nil
    },
}

func init() {
    restartCmd.Flags().StringP("namespace", "n", "default", "命名空间")
    restartCmd.Flags().StringP("selector", "l", "", "标签选择器")
    restartCmd.Flags().Bool("dry-run", false, "只输出不执行")
    restartCmd.Flags().Bool("all-namespaces", false, "操作所有命名空间")
}
```

---

## 实战案例2：Pod 资源使用报告

```go
// pkg/report/pod_resource.go
package report

import (
    "context"
    "fmt"
    "os"
    "sort"

    corev1 "k8s.io/api/core/v1"
    "k8s.io/apimachinery/pkg/api/resource"
    metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
    "k8s.io/client-go/kubernetes"
    "github.com/olekukonko/tablewriter"
)

type PodResourceRow struct {
    Namespace     string
    PodName       string
    Container     string
    CPURequest    string
    CPULimit      string
    MemRequest    string
    MemLimit      string
    CPURatio      float64  // limit/request 比值
}

func GeneratePodResourceReport(ctx context.Context, client *kubernetes.Clientset, namespace string) error {
    pods, err := client.CoreV1().Pods(namespace).List(ctx, metav1.ListOptions{
        FieldSelector: "status.phase=Running",
    })
    if err != nil {
        return err
    }

    var rows []PodResourceRow
    for _, pod := range pods.Items {
        for _, c := range pod.Spec.Containers {
            row := PodResourceRow{
                Namespace: pod.Namespace,
                PodName:   pod.Name,
                Container: c.Name,
            }

            if req, ok := c.Resources.Requests[corev1.ResourceCPU]; ok {
                row.CPURequest = req.String()
            } else {
                row.CPURequest = "<未设置>"
            }

            if lim, ok := c.Resources.Limits[corev1.ResourceCPU]; ok {
                row.CPULimit = lim.String()
                // 计算 limit/request 比值（找出超额分配的容器）
                if req, ok := c.Resources.Requests[corev1.ResourceCPU]; ok && req.Cmp(resource.MustParse("0")) > 0 {
                    row.CPURatio = float64(lim.MilliValue()) / float64(req.MilliValue())
                }
            } else {
                row.CPULimit = "<未设置>"
            }

            if req, ok := c.Resources.Requests[corev1.ResourceMemory]; ok {
                row.MemRequest = req.String()
            } else {
                row.MemRequest = "<未设置>"
            }

            if lim, ok := c.Resources.Limits[corev1.ResourceMemory]; ok {
                row.MemLimit = lim.String()
            } else {
                row.MemLimit = "<未设置>"
            }

            rows = append(rows, row)
        }
    }

    // 按 CPURatio 降序排列（超额分配最严重的排最前）
    sort.Slice(rows, func(i, j int) bool {
        return rows[i].CPURatio > rows[j].CPURatio
    })

    // 表格输出
    table := tablewriter.NewWriter(os.Stdout)
    table.SetHeader([]string{"Namespace", "Pod", "Container", "CPU Req", "CPU Lim", "Mem Req", "Mem Lim", "CPU比值"})
    table.SetBorder(false)
    table.SetAutoWrapText(false)

    for _, r := range rows {
        table.Append([]string{
            r.Namespace, r.PodName, r.Container,
            r.CPURequest, r.CPULimit,
            r.MemRequest, r.MemLimit,
            fmt.Sprintf("%.1f", r.CPURatio),
        })
    }

    table.Render()
    fmt.Printf("\n共 %d 个容器\n", len(rows))
    return nil
}
```

---

## 实战案例3：过期 ConfigMap 清理

```go
// pkg/cleaner/configmap.go
package cleaner

import (
    "context"
    "fmt"
    "time"

    metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
    "k8s.io/client-go/kubernetes"
)

// CleanStaleConfigMaps 清理超过指定天数未被引用的 ConfigMap
// 通过 annotation "ops/last-used-at" 判断最后使用时间
func CleanStaleConfigMaps(ctx context.Context, client *kubernetes.Clientset, namespace string, olderThanDays int, dryRun bool) error {
    cms, err := client.CoreV1().ConfigMaps(namespace).List(ctx, metav1.ListOptions{
        LabelSelector: "ops/auto-cleanup=true",   // 只清理打了这个标签的
    })
    if err != nil {
        return err
    }

    threshold := time.Now().AddDate(0, 0, -olderThanDays)
    deleted := 0
    skipped := 0

    for _, cm := range cms.Items {
        // 检查最后使用时间 annotation
        lastUsedStr, ok := cm.Annotations["ops/last-used-at"]
        if !ok {
            // 没有 annotation，用创建时间
            if cm.CreationTimestamp.After(threshold) {
                skipped++
                continue
            }
        } else {
            lastUsed, err := time.Parse(time.RFC3339, lastUsedStr)
            if err != nil || lastUsed.After(threshold) {
                skipped++
                continue
            }
        }

        if dryRun {
            fmt.Printf("[dry-run] 将删除: %s/%s (创建于 %s)\n",
                cm.Namespace, cm.Name, cm.CreationTimestamp.Format("2006-01-02"))
        } else {
            err := client.CoreV1().ConfigMaps(cm.Namespace).Delete(ctx, cm.Name, metav1.DeleteOptions{})
            if err != nil {
                fmt.Printf("删除失败: %s/%s: %v\n", cm.Namespace, cm.Name, err)
                continue
            }
            fmt.Printf("已删除: %s/%s\n", cm.Namespace, cm.Name)
        }
        deleted++
    }

    fmt.Printf("\n统计: 删除 %d 个, 跳过 %d 个\n", deleted, skipped)
    return nil
}
```

---

## cobra CLI 封装

```go
// main.go
package main

import (
    "fmt"
    "os"

    "github.com/spf13/cobra"
    "github.com/example/k8s-ops-tools/cmd"
)

var (
    kubeconfig string
    rootCmd    = &cobra.Command{
        Use:   "k8s-ops",
        Short: "K8s 运维工具集",
        Long:  `一组用于日常 K8s 运维的实用工具`,
    }
)

func main() {
    rootCmd.PersistentFlags().StringVar(&kubeconfig, "kubeconfig", "",
        "kubeconfig 文件路径 (默认 $HOME/.kube/config)")

    rootCmd.AddCommand(
        cmd.NewRestartCmd(&kubeconfig),
        cmd.NewReportCmd(&kubeconfig),
        cmd.NewCleanCmd(&kubeconfig),
    )

    if err := rootCmd.Execute(); err != nil {
        fmt.Fprintln(os.Stderr, err)
        os.Exit(1)
    }
}
```

构建：

```bash
# 本地构建
go build -o k8s-ops .

# 交叉编译（部署到 Linux amd64）
GOOS=linux GOARCH=amd64 go build -o k8s-ops-linux-amd64 .

# 示例用法
./k8s-ops restart -n production -l team=backend --dry-run
./k8s-ops report pods -n production
./k8s-ops clean configmaps -n production --older-than 30 --dry-run
```

---

## 几点性能建议

**1. 善用 FieldSelector 和 LabelSelector**

在 `List` 时尽量在服务端过滤，而不是把全量数据拉到客户端再过滤。`FieldSelector` 支持的字段有限（主要是 status.phase、metadata.name 等），复杂过滤用 `LabelSelector`。

**2. 控制 QPS/Burst**

```go
config.QPS = 20    // 每秒最多20个请求
config.Burst = 40  // 突发上限
```

批量操作工具如果不控制速率，很容易把 API Server 打出限流。

**3. 使用分页 List**

大集群（几千个 Pod）要用分页，避免单次返回超大结果集：

```go
listOpts := metav1.ListOptions{Limit: 100}
for {
    pods, err := client.CoreV1().Pods(ns).List(ctx, listOpts)
    if err != nil { break }
    // 处理 pods.Items
    if pods.Continue == "" { break }
    listOpts.Continue = pods.Continue
}
```

**4. Informer 优先于频繁 List**

如果工具需要长期运行并响应变化，用 Informer 代替轮询。Informer 在初始化时 List 一次，之后通过 Watch 增量更新本地缓存，远比每分钟 List 一次高效。

client-go 是一个相当稳定的库，K8s 几乎每次版本都向后兼容。掌握了基础的 List/Watch/Informer，基本上可以构建任何复杂度的运维工具——从简单的批量操作脚本，到完整的自定义 Controller。
