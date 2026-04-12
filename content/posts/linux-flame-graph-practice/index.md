---
title: "Linux 火焰图实战：从采集到定位问题"
date: 2026-04-12T14:00:00+08:00
draft: false
tags: ["火焰图", "perf", "性能分析", "Go", "JVM", "Python", "Pyroscope", "Kubernetes"]
categories: ["性能调优"]
description: "系统讲解 CPU/Off-CPU/Memory 火焰图的读法，perf/async-profiler/py-spy 的实际用法，Pyroscope 持续 profiling，以及 K8s 容器内 profiling 的两种方案。附一个 Go 服务 CPU 飙高的完整排查案例。"
summary: "CPU 飙高、响应慢、内存泄漏——这三类问题用火焰图都能快速定位。本文从怎么读火焰图开始，讲到 perf、async-profiler、py-spy 各自的适用场景，最后用一个真实的 Go 服务案例走完完整排查流程。"
toc: true
math: false
diagram: false
keywords: ["火焰图", "CPU profiling", "perf", "async-profiler", "py-spy", "Pyroscope", "Go 性能", "K8s profiling", "FlameGraph"]
params:
  reading_time: true
---

火焰图是 Brendan Gregg 2013 年发明的可视化工具，现在已经是性能排查的标配。它能把几千个采样调用栈压缩成一张可交互的 SVG，让你 10 秒内看出 CPU 时间花在哪里。但实际使用时，很多人卡在"如何采集"和"看到图不知道该关注哪里"这两步。

这篇文章把采集 → 生成 → 分析的完整流程走一遍，覆盖 Go/JVM/Python 三种常见场景，最后讲 K8s 容器环境下怎么做。

## 怎么读火焰图

先把读图方式说清楚，否则后面讲采集没有意义。

### X 轴和 Y 轴

**X 轴（宽度）= 采样比例**，不是时间顺序。一个函数在 X 轴上越宽，说明它在采样期间占用 CPU 的时间越多。同一层的多个函数是按字母顺序排列的，不是执行顺序。

**Y 轴（高度）= 调用栈深度**，越往上是越靠近叶子函数（真正在执行的代码），越往下是调用者。底部通常是 `main`、线程入口等。

**看哪里**：

1. 找 X 轴上最宽的"平顶"——那就是 CPU 热点，如果一个函数宽但上面没有子函数，说明时间花在这个函数本身而不是它调用的子函数里。
2. 找"悬崖"——宽的父函数下面突然变窄，说明父函数的大部分时间花在直接执行而不是调用子函数。
3. 忽略调用栈的绝对深度，那通常是语言框架的层次，和性能无关。

### 三种火焰图的区别

**CPU 火焰图（On-CPU Flame Graph）**：采样进程在 CPU 上运行时的调用栈。适合 CPU 使用率高的问题。颜色通常是暖色（红/橙）。

**Off-CPU 火焰图**：采样进程等待（sleep、I/O、锁、系统调用）时的调用栈。适合请求慢但 CPU 不高的问题。进程在等什么，等多久，一目了然。颜色通常是冷色（蓝）。

**Memory 火焰图**：采样内存分配时的调用栈，按分配字节数加权。适合内存泄漏和频繁 GC 问题。

```
场景 → 选哪种火焰图：

CPU 高 → On-CPU
响应慢但 CPU 低 → Off-CPU
内存持续涨 → Memory（Allocation）
```

## 用 perf 生成 CPU 火焰图

perf 是内核自带的 profiler，适合 C/C++ 程序，Go 程序也能用（有一些限制）。

### 安装

```bash
# Ubuntu
apt install -y linux-tools-common linux-tools-$(uname -r)
# 验证
perf stat ls

# 如果报 "No permission"
echo -1 | tee /proc/sys/kernel/perf_event_paranoid
echo 0 | tee /proc/sys/kernel/kptr_restrict
```

### 采集 CPU profile

```bash
# 对指定 PID 采集 30 秒，99Hz 采样
perf record -F 99 -p $PID -g -- sleep 30

# -g：采集调用栈（必须，否则只有函数名没有栈）
# -F 99：采样频率 99Hz（避免和系统定时器 100Hz 同频）
# 输出文件：perf.data（在当前目录）

# 如果要采集整个系统（所有进程）
perf record -F 99 -a -g -- sleep 30

# 导出为文本格式（给 FlameGraph 工具用）
perf script > /tmp/perf_out.txt
```

### 生成火焰图

```bash
# 安装 FlameGraph 工具（Brendan Gregg 维护）
git clone https://github.com/brendangregg/FlameGraph /opt/flamegraph

# 折叠调用栈 + 生成 SVG
perf script | \
    /opt/flamegraph/stackcollapse-perf.pl | \
    /opt/flamegraph/flamegraph.pl \
    --title "CPU Flame Graph" \
    --color "hot" \
    > /tmp/cpu_flame.svg

# 在浏览器打开就能交互（点击可以展开某一段栈）
```

### Go 程序的特殊处理

Go 默认不保留 frame pointer（Go 1.12+ x86-64 默认开启，但 arm64 等架构可能没有），perf 的栈采集可能不完整：

```bash
# 检查 Go 版本（1.12+ x86-64 应该有 frame pointer）
go version

# 编译时显式保留 frame pointer（所有平台）
GOFLAGS="-buildmode=exe" go build -gcflags="-e" .

# 如果 perf script 输出里看到大量 [unknown] 调用帧，
# 说明 frame pointer 缺失，改用 async-profiler 或 pprof
```

Go 自带 pprof，生产服务建议直接暴露 pprof HTTP 端点：

```go
import _ "net/http/pprof"

// 在 main 里启动
go func() {
    log.Println(http.ListenAndServe("localhost:6060", nil))
}()
```

```bash
# 采集 CPU profile（30 秒）
go tool pprof http://localhost:6060/debug/pprof/profile?seconds=30

# 在 pprof 交互界面生成火焰图
(pprof) web   # 用 Graphviz，生成 call graph
(pprof) list funcname  # 显示函数级别的 CPU 时间

# 直接生成火焰图 SVG（需要 Graphviz）
go tool pprof -http=:8080 /tmp/cpu.pprof
# 浏览器访问 localhost:8080，点击 Flame Graph 标签
```

## 用 async-profiler 对 JVM 应用生成火焰图

perf 无法正确解析 JVM 的 JIT 编译代码的符号，async-profiler 专门解决了这个问题。

### 安装

```bash
# 下载最新版（支持 Linux x64 和 aarch64）
wget https://github.com/async-profiler/async-profiler/releases/latest/download/async-profiler-3.0-linux-x64.tar.gz
tar xf async-profiler-3.0-linux-x64.tar.gz -C /opt/
ln -s /opt/async-profiler-3.0-linux-x64 /opt/async-profiler

# 验证
ls /opt/async-profiler/
# 应该有：asprof, lib/libasyncProfiler.so, converter.jar
```

### 采集 CPU 火焰图

```bash
# 找到 JVM 进程 PID
JVM_PID=$(pgrep -f "java.*your-app")

# 采集 30 秒 CPU profile，直接输出 SVG
/opt/async-profiler/asprof \
    -d 30 \
    -f /tmp/cpu_flame.html \
    -o flamegraph \
    $JVM_PID

# -d 30：采集 30 秒
# -o flamegraph：输出格式（flamegraph/collapsed/jfr）
# -f：输出文件（.html 是交互式，.svg 是静态）

# 输出 JFR 格式（可以用 JDK Mission Control 打开）
/opt/async-profiler/asprof \
    -d 30 \
    -f /tmp/recording.jfr \
    $JVM_PID
```

### 采集 Allocation（内存分配）火焰图

```bash
# 按分配字节数统计，找内存热点
/opt/async-profiler/asprof \
    -e alloc \
    -d 30 \
    -f /tmp/alloc_flame.html \
    -o flamegraph \
    $JVM_PID

# 如果要过滤小对象，只看大于 512KB 的分配
/opt/async-profiler/asprof \
    -e alloc \
    --alloc 512k \
    -d 30 \
    -f /tmp/alloc_flame.html \
    $JVM_PID
```

### 采集 Off-CPU（锁/I/O 等待）火焰图

```bash
# Wall-clock mode：无论 on-CPU 还是 off-CPU 都采样
# 适合找到底在哪等（比 CPU 模式更全面）
/opt/async-profiler/asprof \
    -e wall \
    -t \
    -d 30 \
    -f /tmp/wall_flame.html \
    $JVM_PID

# -t：按线程分组（可以对比不同线程的耗时分布）
```

### Spring Boot 应用的常见问题

```bash
# Spring Boot 应用常见热点（火焰图里经常出现）：
# 1. com/fasterxml/jackson → JSON 序列化/反序列化耗时
#    解法：开启 Jackson 的 afterburner 模块或切换 fastjson2
# 2. org/springframework/web/servlet/DispatcherServlet → 反射路由
#    解法：升级 Spring 版本或减少 AOP 层级
# 3. java/util/regex → 正则表达式
#    解法：预编译 Pattern.compile()，不要在循环里用 String.matches()

# 过滤特定包（只看应用代码，过滤框架噪音）
/opt/async-profiler/asprof \
    -e cpu \
    -d 30 \
    --include "com/yourcompany/**" \
    -f /tmp/app_flame.html \
    $JVM_PID
```

## 用 py-spy 对 Python 应用生成火焰图

Python GIL 的存在让 CPU profile 变得复杂，py-spy 是目前最好的 Python profiler，不需要修改代码，也不需要重启进程。

### 安装

```bash
pip install py-spy
# 或者用独立的二进制（推荐，不影响应用 Python 环境）
wget https://github.com/benfred/py-spy/releases/latest/download/py-spy-x86_64-unknown-linux-musl.tar.gz
tar xf py-spy-x86_64-unknown-linux-musl.tar.gz
mv py-spy /usr/local/bin/
```

### 生成火焰图

```bash
# 对运行中的进程生成 30 秒 CPU 火焰图
py-spy record \
    --pid $PYTHON_PID \
    --duration 30 \
    --output /tmp/py_flame.svg \
    --format flamegraph

# 采样频率（默认 100Hz，可调）
py-spy record \
    --pid $PYTHON_PID \
    --rate 200 \
    --duration 30 \
    --output /tmp/py_flame.svg

# 从头采集（运行程序同时采集）
py-spy record \
    --output /tmp/py_flame.svg \
    -- python myapp.py --args
```

### 实时查看热点（top 模式）

```bash
# 类似 htop，实时显示 Python 函数级别的 CPU 占用
py-spy top --pid $PYTHON_PID

# 输出示例：
# OwnTime  TotalTime  Function (filename:line)
# 45.00%   45.00%     json_encode (/app/utils.py:123)
# 23.00%   68.00%     process_request (/app/handler.py:45)
```

### 多进程/多线程

```bash
# Gunicorn 多 worker 场景：对每个 worker 单独采集
# 找所有 worker PID
pgrep -f "gunicorn worker" | while read pid; do
    py-spy record --pid $pid --duration 15 \
        --output /tmp/worker_${pid}_flame.svg &
done
wait
echo "All workers profiled"

# uvicorn async 应用：py-spy 能采集协程栈
py-spy record \
    --pid $PID \
    --duration 30 \
    --output /tmp/async_flame.svg \
    --native  # 同时采集 C 扩展的栈（如 numpy、pandas）
```

### 常见 Python 热点

```bash
# 火焰图里常见的 Python 性能问题：
# 1. ujson/json 序列化在 X 轴很宽
#    → 考虑 orjson（比标准库快 10x）
# 2. re.compile/re.match 在循环里
#    → 移到循环外预编译
# 3. SQLAlchemy ORM 的 N+1 查询（每次循环都触发一次 DB）
#    → 用 joinedload/selectinload 预加载
# 4. requests/httpx 的 DNS 解析（每次 HTTP 请求都 resolve）
#    → 维持连接池，或用 aiodns
```

## 用 Pyroscope 做持续 profiling

一次性采集只能看当前状态，线上问题往往是偶发的。Pyroscope 是个持续 profiling 平台，能把每分钟的 profile 都存下来，出问题后可以回溯。

### 部署 Pyroscope

```yaml
# pyroscope.yaml（K8s 部署）
apiVersion: apps/v1
kind: Deployment
metadata:
  name: pyroscope
  namespace: monitoring
spec:
  replicas: 1
  selector:
    matchLabels:
      app: pyroscope
  template:
    metadata:
      labels:
        app: pyroscope
    spec:
      containers:
      - name: pyroscope
        image: grafana/pyroscope:latest
        ports:
        - containerPort: 4040
        env:
        - name: PYROSCOPE_STORAGE_PATH
          value: /data
        volumeMounts:
        - name: data
          mountPath: /data
      volumes:
      - name: data
        persistentVolumeClaim:
          claimName: pyroscope-pvc
---
apiVersion: v1
kind: Service
metadata:
  name: pyroscope
  namespace: monitoring
spec:
  selector:
    app: pyroscope
  ports:
  - port: 4040
    targetPort: 4040
```

### Go 应用接入

```go
package main

import (
    "log"
    "github.com/grafana/pyroscope-go"
)

func main() {
    // 在 main 函数开头初始化
    profiler, err := pyroscope.Start(pyroscope.Config{
        ApplicationName: "my-go-service",
        ServerAddress:   "http://pyroscope:4040",
        // 标签：可以按 pod、版本、env 等维度过滤
        Tags: map[string]string{
            "version": os.Getenv("APP_VERSION"),
            "pod":     os.Getenv("POD_NAME"),
            "env":     os.Getenv("ENV"),
        },
        // 开启所有 profile 类型
        ProfileTypes: []pyroscope.ProfileType{
            pyroscope.ProfileCPU,
            pyroscope.ProfileAllocObjects,
            pyroscope.ProfileAllocSpace,
            pyroscope.ProfileInuseObjects,
            pyroscope.ProfileInuseSpace,
            pyroscope.ProfileGoroutines,
        },
    })
    if err != nil {
        log.Printf("pyroscope init failed: %v", err)
        // 不要因为 profiling 初始化失败就退出
    }
    defer profiler.Stop()

    // ... 正常业务逻辑
}
```

### Python 应用接入

```python
import pyroscope

pyroscope.configure(
    application_name="my-python-service",
    server_address="http://pyroscope:4040",
    tags={
        "version": os.getenv("APP_VERSION", "unknown"),
        "pod": os.getenv("POD_NAME", "unknown"),
    },
    # 可以只开 cpu，降低 overhead
    detect_subprocesses=False,
    oncpu=True,
    gil_only=False,    # False = 采集 native 代码（C 扩展）
    enable_logging=True,
)
```

### 对比两次 deploy 前后的 profile

这是 Pyroscope 最有价值的功能之一：

```bash
# 在 Pyroscope UI 里：
# 1. 选择 "Comparison" 视图
# 2. 左侧选 deploy 前的时间段（比如 10:00-10:30）
# 3. 右侧选 deploy 后的时间段（比如 10:45-11:15）
# 4. UI 会用差异着色：
#    - 红色：新版本比旧版本更慢的函数
#    - 绿色：新版本比旧版本更快的函数

# 通过 API 做自动化对比
BEFORE_FROM="2026-04-10T10:00:00Z"
BEFORE_UNTIL="2026-04-10T10:30:00Z"
AFTER_FROM="2026-04-10T10:45:00Z"
AFTER_UNTIL="2026-04-10T11:15:00Z"

# 导出两个时间段的 profile（collapsed 格式）
curl "http://pyroscope:4040/render?from=$BEFORE_FROM&until=$BEFORE_UNTIL&query=my-go-service.cpu&format=collapsed" \
    > /tmp/before.collapsed

curl "http://pyroscope:4040/render?from=$AFTER_FROM&until=$AFTER_UNTIL&query=my-go-service.cpu&format=collapsed" \
    > /tmp/after.collapsed

# 用 FlameGraph diff 工具生成差异图
/opt/flamegraph/difffolded.pl /tmp/before.collapsed /tmp/after.collapsed | \
    /opt/flamegraph/flamegraph.pl \
    --title "Deploy diff: before vs after" \
    --colors=RdYlGn \
    > /tmp/diff_flame.svg
```

## 实战：从 Go 服务 CPU 飙高定位到具体函数

这是一个真实案例的简化版本。现象：Go 服务的 CPU 从平时 20% 突然上升到 85%，持续了 15 分钟后自动恢复。Prometheus 报警触发时已经恢复，需要回溯。

### 第一步：确认 CPU 上升的时间范围

```bash
# 从 Prometheus 查询 CPU 使用率
curl -s "http://prometheus:9090/api/v1/query_range" \
    --data-urlencode 'query=rate(container_cpu_usage_seconds_total{pod=~"my-service-.*"}[1m])' \
    --data-urlencode 'start=2026-04-10T09:00:00Z' \
    --data-urlencode 'end=2026-04-10T10:00:00Z' \
    --data-urlencode 'step=60' | \
    python3 -c "
import sys, json
d = json.load(sys.stdin)
for r in d['data']['result']:
    for t, v in r['values']:
        if float(v) > 0.5:  # 超过 50% 的时间点
            import datetime
            print(datetime.datetime.fromtimestamp(float(t)), v)
"
```

输出显示 09:45 到 10:00 之间 CPU 飙高。

### 第二步：从 Pyroscope 查看这段时间的 profile

```bash
# 导出 09:45-10:00 的 CPU profile
curl "http://pyroscope:4040/render?from=2026-04-10T09:45:00Z&until=2026-04-10T10:00:00Z&query=my-go-service.cpu&format=collapsed" \
    > /tmp/high_cpu.collapsed

# 生成火焰图
/opt/flamegraph/flamegraph.pl /tmp/high_cpu.collapsed > /tmp/high_cpu_flame.svg
```

打开 SVG 后，发现一个宽约 40% 的"平顶"：

```
main.(*Server).handleRequest
  main.(*OrderService).processOrders
    main.(*OrderService).validateOrder     ← 这里占 38%
      regexp.(*Regexp).MatchString         ← 时间花在这里
```

`validateOrder` 里有大量正则匹配，38% 的 CPU 时间都在这里。

### 第三步：确认是不是正则编译问题

```bash
# 在代码里搜索 MatchString 的用法
grep -rn "MatchString\|regexp.MustCompile\|regexp.Compile" ./internal/order/ | head -20
```

找到问题代码（伪代码）：

```go
// 问题代码：每次调用都重新编译正则
func (s *OrderService) validateOrder(order *Order) error {
    // 这行每次都执行 regexp.Compile！
    re := regexp.MustCompile(`^[A-Z]{2}-\d{8}-[A-Z0-9]{6}$`)
    if !re.MatchString(order.ID) {
        return fmt.Errorf("invalid order ID format: %s", order.ID)
    }
    // ...
}

// 修复：移到包级变量（只编译一次）
var orderIDPattern = regexp.MustCompile(`^[A-Z]{2}-\d{8}-[A-Z0-9]{6}$`)

func (s *OrderService) validateOrder(order *Order) error {
    if !orderIDPattern.MatchString(order.ID) {
        return fmt.Errorf("invalid order ID format: %s", order.ID)
    }
    // ...
}
```

### 第四步：验证修复效果

```bash
# deploy 新版本后，用 Pyroscope comparison 对比
# 左边：旧版本 09:45-10:00（CPU 飙高期间）
# 右边：新版本 deploy 后的同等负载时间段

# 或者用 Go benchmark 验证
go test -bench=BenchmarkValidateOrder -benchtime=5s -benchmem ./internal/order/
# Before: 12345 ns/op  2048 B/op  23 allocs/op
# After:   234 ns/op     0 B/op   0 allocs/op
```

差异非常明显。这是 CPU 飙高最常见的模式之一：**某个请求量上涨触发了代码里本来就存在的低效路径**。

## K8s 容器内如何做 Profiling

### 方案 A：DaemonSet（推荐用于持续 profiling）

在每个节点部署一个特权 DaemonSet，用宿主机 PID 命名空间采集所有容器的 profile：

```yaml
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: parca-agent        # Parca 是一个持续 profiling 工具，类似 Pyroscope
  namespace: monitoring
spec:
  selector:
    matchLabels:
      app: parca-agent
  template:
    metadata:
      labels:
        app: parca-agent
    spec:
      hostPID: true          # 必须：能看到所有进程
      hostNetwork: true      # 可选：减少网络开销
      serviceAccountName: parca-agent
      tolerations:
      - operator: Exists     # 所有节点都部署（包括 tainted 节点）
      containers:
      - name: parca-agent
        image: ghcr.io/parca-dev/parca-agent:latest
        args:
        - /bin/parca-agent
        - --node=$(NODE_NAME)
        - --remote-store-address=parca.monitoring.svc:7070
        - --remote-store-insecure
        env:
        - name: NODE_NAME
          valueFrom:
            fieldRef:
              fieldPath: spec.nodeName
        securityContext:
          privileged: true
        volumeMounts:
        - name: proc
          mountPath: /host/proc
          readOnly: true
        - name: sys
          mountPath: /sys
          readOnly: true
        - name: cgroup
          mountPath: /sys/fs/cgroup
        - name: debugfs
          mountPath: /sys/kernel/debug
      volumes:
      - name: proc
        hostPath:
          path: /proc
      - name: sys
        hostPath:
          path: /sys
      - name: cgroup
        hostPath:
          path: /sys/fs/cgroup
      - name: debugfs
        hostPath:
          path: /sys/kernel/debug
```

DaemonSet 方案的优点：零侵入，不需要改应用代码，适合全集群统一部署。缺点：特权模式有安全顾虑，需要运维团队统一管理。

### 方案 B：临时容器（适合一次性排查）

```bash
# 对已有 pod 注入临时调试容器（kubectl debug，K8s 1.23+ 推荐）
kubectl debug -it my-pod-xxx \
    --image=golang:1.22 \
    --target=my-container \    # 共享目标容器的进程命名空间
    --share-processes=true \
    -- bash

# 进入调试容器后，用 pprof 采集
# （目标容器必须暴露 pprof HTTP 端点）
curl http://localhost:6060/debug/pprof/profile?seconds=30 -o /tmp/cpu.pprof
go tool pprof -http=:8080 /tmp/cpu.pprof

# 另一种方式：用 nsenter 进入目标容器的命名空间（在节点上操作）
TARGET_PID=$(crictl inspect $CONTAINER_ID | python3 -c "import sys,json;print(json.load(sys.stdin)['info']['pid'])")
nsenter -t $TARGET_PID -n -p -m -- \
    /usr/local/bin/py-spy record --pid 1 --duration 30 -o /tmp/py_flame.svg
```

### 方案 B 的变体：专用调试 Pod

```yaml
# debug-profiler.yaml
# 调度到目标节点，共享 hostPID，用于一次性排查
apiVersion: v1
kind: Pod
metadata:
  name: profiler-debug
  namespace: default
spec:
  hostPID: true
  nodeName: node-xxx         # 替换为目标节点名
  restartPolicy: Never
  containers:
  - name: profiler
    image: ubuntu:22.04
    command:
    - bash
    - -c
    - |
      apt-get update -q && apt-get install -y -q wget python3-pip
      pip install py-spy -q
      # 找到目标进程
      TARGET_PID=$(pgrep -f "my-python-app" | head -1)
      echo "Profiling PID: $TARGET_PID"
      py-spy record --pid $TARGET_PID --duration 60 -o /tmp/flame.svg
      # 开一个 HTTP server 让外部下载
      cd /tmp && python3 -m http.server 8888
    ports:
    - containerPort: 8888
    securityContext:
      privileged: true
```

```bash
# 部署后通过 port-forward 下载 SVG
kubectl apply -f debug-profiler.yaml
kubectl wait pod/profiler-debug --for=condition=Ready --timeout=120s

# 等待 profiling 完成（大约 60 秒）
sleep 65

kubectl port-forward pod/profiler-debug 8888:8888 &
curl http://localhost:8888/flame.svg -o /tmp/remote_flame.svg
kubectl delete pod profiler-debug

# 用浏览器打开 /tmp/remote_flame.svg
```

## 实用工具链汇总

```bash
# 工具安装一键脚本（放到跳板机或调试镜像里）
#!/bin/bash

# FlameGraph（所有平台）
git clone https://github.com/brendangregg/FlameGraph /opt/flamegraph

# async-profiler（JVM）
wget -qO- https://github.com/async-profiler/async-profiler/releases/latest/download/async-profiler-3.0-linux-x64.tar.gz | \
    tar xz -C /opt/ && ln -sfn /opt/async-profiler-* /opt/async-profiler

# py-spy（Python）
pip install py-spy 2>/dev/null || \
    wget -qO /usr/local/bin/py-spy \
    https://github.com/benfred/py-spy/releases/latest/download/py-spy-x86_64-unknown-linux-musl && \
    chmod +x /usr/local/bin/py-spy

# perf（C/Go，需要内核工具）
apt install -y linux-tools-$(uname -r) linux-tools-generic 2>/dev/null

# 验证
which perf py-spy && ls /opt/async-profiler/asprof && ls /opt/flamegraph/flamegraph.pl
echo "All tools ready"
```

常见问题速查：

```bash
# "no symbols found" → 二进制被 strip，需要重新编译保留符号
# "[unknown]" 调用帧 → 缺少 frame pointer，用 --call-graph=lbr 或换用 async-profiler
# py-spy "permission denied" → 加 sudo 或在容器里加 SYS_PTRACE capability
# async-profiler "Could not start attach listener" → JVM 没有开 -XX:+EnableDynamicAgentLoading（JDK 21+）
# perf "cycles" event not supported → 虚拟机里用 -e cpu-clock 替代
```
