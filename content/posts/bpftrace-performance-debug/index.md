---
title: "bpftrace 实战：线上问题排查的瑞士军刀"
date: 2026-04-12T10:00:00+08:00
draft: false
tags: ["bpftrace", "eBPF", "性能调优", "Linux", "可观测性", "Kubernetes"]
categories: ["性能调优"]
description: "从语法基础到四个真实排查场景，讲清楚 bpftrace 怎么用：慢系统调用定位、CPU 热点追踪、TCP 丢包分析、K8s 容器内跨 namespace 追踪。附常用 one-liner 速查表。"
summary: "strace 太重、perf 太原始、BCC 工具集要装一堆依赖——bpftrace 是这三者之间的平衡点。本文用四个真实场景讲清楚 bpftrace 的工作方式，帮你把它变成日常排查工具。"
toc: true
math: false
diagram: false
keywords: ["bpftrace", "eBPF", "系统调用", "性能分析", "火焰图", "TCP 丢包", "K8s 追踪"]
params:
  reading_time: true
---

strace 一挂上去进程就慢了一半，perf 的输出要花时间解析，BCC 工具集在生产机器上装不了——实际排查时这三个问题经常同时出现。bpftrace 是个折中选择：单文件可执行、语法接近 awk/DTrace、overhead 在可接受范围内，内核 4.9+ 就能用。

这篇文章不讲 eBPF 的实现原理，直接讲怎么用 bpftrace 解决实际问题。

## 和 strace/perf/BCC 的定位区别

先把几个工具的边界说清楚，免得用错场景：

| 工具 | 适合场景 | 主要缺点 |
|------|---------|---------|
| strace | 单进程系统调用序列追踪，问题已经明确 | ptrace 实现，overhead 极高（3-10x 慢），不能做聚合统计 |
| perf stat/record | CPU 计数器、采样 profiling、硬件性能分析 | 输出原始，需要后处理；内核符号需要 kallsyms |
| BCC tools | 完整的预制分析工具集（opensnoop、biolatency 等） | 依赖 LLVM/clang，生产机不一定能装 |
| bpftrace | 临时写脚本做一次性或低频调查，语法接近高级语言 | 复杂聚合逻辑不如 BCC 灵活，单文件不支持 BTF 的老内核跑不了 |

bpftrace 的定位：**你知道要看什么，但没有现成工具，需要快速写个 10-30 行的脚本跑一次**。

## 安装

```bash
# Ubuntu 22.04+
apt install -y bpftrace

# 验证内核支持（需要 4.9+ 且开启 CONFIG_BPF=y）
bpftrace --version
bpftrace -e 'BEGIN { printf("ok\n"); exit(); }'

# 生产机没有包管理器时，用静态编译版本
# 从 https://github.com/bpftrace/bpftrace/releases 下载
wget https://github.com/bpftrace/bpftrace/releases/latest/download/bpftrace
chmod +x bpftrace
./bpftrace -e 'BEGIN { printf("ok\n"); exit(); }'
```

内核 5.8+ 启用了 BTF（BPF Type Format），bpftrace 可以直接访问内核结构体成员而不需要额外的头文件，排查会方便很多。

## 语法核心：probe、filter、action

bpftrace 程序由若干 `probe / filter / { action }` 块组成：

```
probe [/ filter /] {
    action
}
```

### Probe 类型

```bash
# kprobe：挂载到内核函数入口
kprobe:vfs_read

# kretprobe：挂载到内核函数返回
kretprobe:vfs_read

# tracepoint：内核稳定 tracepoint（推荐，不随内核版本变化）
tracepoint:syscalls:sys_enter_openat

# uprobe：用户态函数（需要调试符号或知道偏移）
uprobe:/usr/bin/nginx:ngx_http_process_request

# usdt：应用内置的 USDT probe（Go runtime、Python、Node.js 等）
usdt:/usr/bin/python3:function__entry

# software/hardware：软硬件性能事件
software:cpu-clock:100     # 每 100 个 cpu-clock 触发一次
hardware:cache-misses:1000

# interval：定时触发
interval:s:5               # 每 5 秒触发

# BEGIN/END：脚本开始/结束时触发
BEGIN
END
```

### 内置变量

```bash
pid        # 进程 ID
tid        # 线程 ID
comm       # 进程名（comm，最多 16 字节）
uid        # 用户 ID
cpu        # 当前 CPU 核编号
nsecs      # 当前时间（纳秒）
elapsed    # 脚本启动到现在的纳秒数
curtask    # 指向 task_struct 的指针（内核 5.8+ BTF 可直接访问成员）
retval     # kretprobe/uretprobe 中的函数返回值
args       # tracepoint 的参数结构体
arg0..argN # kprobe 的寄存器参数（按 ABI 顺序）
```

### 数据结构

```bash
# map：全局 key-value，支持聚合
@latency[comm] = hist(nsecs);      # histogram
@count[pid]++;                     # 计数
@bytes = sum(arg2);                # 求和

# 临时变量（$前缀，单个 probe 内有效）
$start = nsecs;

# 关联数组（用 tid 做 key，跨 probe 传递数据）
@start[tid] = nsecs;
```

## 实战场景 1：定位慢系统调用

**问题背景**：服务 p99 延迟高，但 APM 显示业务代码本身很快，怀疑是 I/O 系统调用慢。

### 找出哪个 syscall 慢

```bash
# 追踪所有进程的 open/read/write，统计延迟分布
# 运行 10 秒后输出直方图
bpftrace -e '
tracepoint:syscalls:sys_enter_openat,
tracepoint:syscalls:sys_enter_read,
tracepoint:syscalls:sys_enter_write
{
    @start[tid] = nsecs;
    @syscall[tid] = probe;   // 记录是哪个 syscall
}

tracepoint:syscalls:sys_exit_openat,
tracepoint:syscalls:sys_exit_read,
tracepoint:syscalls:sys_exit_write
/ @start[tid] /
{
    $delta = (nsecs - @start[tid]) / 1000;  // 转微秒
    // 只记录超过 1ms 的
    if ($delta > 1000) {
        @slow[comm, @syscall[tid]] = lhist($delta, 0, 100000, 1000);
    }
    delete(@start[tid]);
    delete(@syscall[tid]);
}

interval:s:10 { exit(); }
'
```

### 锁定具体进程和文件

发现是 openat 慢之后，进一步看是打开哪些文件：

```bash
# 只追踪名为 myapp 的进程，打印慢 open（>5ms）的文件路径和调用栈
bpftrace -e '
tracepoint:syscalls:sys_enter_openat
/ comm == "myapp" /
{
    @start[tid] = nsecs;
    @fname[tid] = str(args->filename);
}

tracepoint:syscalls:sys_exit_openat
/ @start[tid] && comm == "myapp" /
{
    $delta = (nsecs - @start[tid]) / 1000000;  // 毫秒
    if ($delta > 5) {
        printf("[%s] openat(%s) took %d ms\n", comm, @fname[tid], $delta);
        // 打印内核栈，定位是哪个内核路径慢（比如 dentry cache miss）
        print(kstack);
    }
    delete(@start[tid]);
    delete(@fname[tid]);
}
'
```

### read/write 的字节分布

```bash
# 统计 read 系统调用的请求大小分布，帮助判断是否有大量小 I/O
bpftrace -e '
tracepoint:syscalls:sys_enter_read
/ pid == $1 /              // $1 是命令行传入的 PID
{
    @read_size = hist(args->count);
}

interval:s:5 {
    print(@read_size);
    clear(@read_size);
}
'
# 用法：bpftrace script.bt 12345
```

## 实战场景 2：追踪进程 CPU 热点函数

**问题背景**：某 Go 服务 CPU 持续 80%，需要定位到具体是哪个函数在消耗。

### 采样用户态调用栈

```bash
# 对 myapp 进程每秒采样 99 次用户态调用栈（99Hz 避免与定时器同频）
bpftrace -e '
profile:hz:99
/ comm == "myapp" /
{
    @stacks = count();    // 简单计数
    @[ustack] = count();  // 按调用栈聚合
}

interval:s:30 {
    print(@);
    exit();
}
'
```

输出是折叠格式的调用栈，可以直接喂给 FlameGraph 工具生成火焰图：

```bash
# 保存输出并生成火焰图
bpftrace -e '
profile:hz:99 / comm == "myapp" / { @[ustack] = count(); }
interval:s:30 { exit(); }
' | tee /tmp/bpftrace_stacks.txt

# 用 flamegraph.pl 生成
# （需要 https://github.com/brendangregg/FlameGraph）
/opt/flamegraph/flamegraph.pl /tmp/bpftrace_stacks.txt > /tmp/cpu_flame.svg
```

Go 的符号需要确保二进制没有 strip，或者用 `-trimpath` 之外还保留了 DWARF 信息：

```bash
# 检查 Go 二进制是否有符号
nm /path/to/myapp | head -5
# 如果没有输出，说明 symbol table 被 strip 掉了
# 重新编译时去掉 -ldflags="-s -w"
```

### 同时采样内核态和用户态

```bash
# 混合栈采样，完整看清一次 CPU 时间的分配
bpftrace -e '
profile:hz:49 / pid == $1 / {
    @[ustack, kstack] = count();
}
interval:s:20 { exit(); }
' 12345
```

### 找出哪个函数被调用次数最多

```bash
# 统计 myapp 进程内所有用户函数的调用次数（uprobe 方式，overhead 较高）
# 先用 nm 找到感兴趣的函数前缀
nm /path/to/myapp | grep -i "handler\|process\|handle" | awk '{print $3}' | head -20

# 然后针对性挂载
bpftrace -e '
uprobe:/path/to/myapp:main.processRequest { @[probe] = count(); }
uprobe:/path/to/myapp:main.handleQuery    { @[probe] = count(); }
interval:s:10 { print(@); clear(@); }
'
```

## 实战场景 3：内核 TCP 超时和丢包分析

**问题背景**：服务间偶发超时，netstat 显示有 RetransSegs 在涨，但不知道是哪条连接在重传。

### 追踪 TCP 重传

```bash
# 打印每次 TCP 重传的四元组和重传原因
bpftrace -e '
#include <net/tcp.h>

kprobe:tcp_retransmit_skb
{
    $sk = (struct sock *)arg0;
    $skb = (struct sk_buff *)arg1;

    // 读取 socket 地址信息（需要 BTF，内核 5.8+）
    $dport = (uint16)($sk->__sk_common.skc_dport);
    $sport = (uint16)($sk->__sk_common.skc_num);
    $daddr = (uint32)($sk->__sk_common.skc_daddr);
    $saddr = (uint32)($sk->__sk_common.skc_rcv_saddr);

    printf("RETRANS: %s:%d -> %d.%d.%d.%d:%d | pid=%d comm=%s\n",
        ntop(AF_INET, $saddr), $sport,
        ($daddr >> 0) & 0xff, ($daddr >> 8) & 0xff,
        ($daddr >> 16) & 0xff, ($daddr >> 24) & 0xff,
        bswap($dport),
        pid, comm);
}
'
```

更简单的方式是用 tracepoint（不需要 BTF）：

```bash
# tcp:tcp_retransmit_skb tracepoint（内核 4.16+）
bpftrace -e '
tracepoint:tcp:tcp_retransmit_skb
{
    printf("RETRANS: %s:%d -> %s:%d state=%d\n",
        ntop(args->saddr),  args->sport,
        ntop(args->daddr),  args->dport,
        args->state);
    @retrans[ntop(args->daddr), args->dport]++;
}

interval:s:5 {
    print(@retrans);
    clear(@retrans);
}
'
```

### 追踪连接建立失败

```bash
# 找出 connect 失败的原因分布
bpftrace -e '
tracepoint:syscalls:sys_enter_connect
{
    @start[tid] = nsecs;
    @pid[tid] = pid;
    @comm[tid] = comm;
}

tracepoint:syscalls:sys_exit_connect
/ @start[tid] /
{
    if (args->ret < 0) {
        // args->ret 是错误码（负数）
        @errors[comm, - args->ret] = count();
    }
    delete(@start[tid]);
    delete(@pid[tid]);
    delete(@comm[tid]);
}

interval:s:10 { print(@errors); clear(@errors); }
'
# 常见错误码：110=ETIMEDOUT, 111=ECONNREFUSED, 113=EHOSTUNREACH
```

### TCP 连接延迟（三次握手耗时）

```bash
# 统计 TCP 连接建立耗时分布（ms 级直方图）
bpftrace -e '
tracepoint:sock:inet_sock_set_state
/ args->newstate == 1 /   // TCP_ESTABLISHED = 1
{
    // 连接建立，记录时间
    @conn_time[args->sport, args->dport] = nsecs;
}

tracepoint:tcp:tcp_destroy_sock
{
    // 连接关闭，计算生存时间（这里演示结构，生产中按需调整）
    @[comm] = count();
}

interval:s:10 { print(@); clear(@); }
'
```

## 实战场景 4：K8s 容器内进程追踪

容器内的进程在主机上完全可见，bpftrace 在宿主机上就能追踪容器内进程，关键是**正确过滤**。

### 通过容器名找到 PID

```bash
# 先找到 pod 里进程的 PID（在宿主机上）
# 方式一：通过 crictl
crictl ps | grep my-pod-name
crictl inspect <container_id> | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['info']['pid'])"

# 方式二：通过 /proc
# 找到容器的 cgroup
kubectl describe pod my-pod-xxx | grep "Container ID"
# docker://abc123 -> 取 abc123
cat /sys/fs/cgroup/memory/docker/abc123.../cgroup.procs | head -1
```

### 过滤特定 cgroup（推荐方式）

```bash
# 通过 cgroup id 过滤，比 PID 更稳定（进程重启后 cgroup id 不变）
# 先获取 cgroup id
CONTAINER_ID=$(kubectl get pod my-pod -o jsonpath='{.status.containerStatuses[0].containerID}' | cut -d/ -f3)
CGROUPID=$(cat /proc/$(crictl inspect $CONTAINER_ID | python3 -c "import sys,json;print(json.load(sys.stdin)['info']['pid'])")/cgroup | grep memory | awk -F: '{print $3}')

# 然后在 bpftrace 中用 cgroup 过滤
bpftrace -e "
tracepoint:syscalls:sys_enter_openat
/ cgroup == cgroupid(\"$CGROUPID\") /
{
    printf(\"%s opened %s\n\", comm, str(args->filename));
}
"
```

### 直接在节点上追踪指定 namespace 的进程

```bash
# 找出属于特定 pod 的所有 PID
PIDS=$(ls -la /proc/*/ns/pid | grep -l "$(readlink /proc/$(crictl inspect $CONTAINER_ID | python3 -m json.tool | grep '"pid"' | head -1 | grep -o '[0-9]*')/ns/pid)" 2>/dev/null | awk -F/ '{print $3}' | tr '\n' '|' | sed 's/|$//')

# 直接用 PID 过滤（适合短脚本）
TARGET_PID=12345
bpftrace -e "
profile:hz:99
/ pid == $TARGET_PID || pid == $TARGET_PID /
{
    @[ustack] = count();
}
interval:s:15 { exit(); }
"
```

### 在容器内使用 bpftrace（特权容器）

有时候需要从容器内部追踪，比如 sidecar 模式：

```yaml
# 临时注入特权调试容器
kubectl debug -it my-pod \
  --image=quay.io/iovisor/bpftrace:latest \
  --target=my-container \
  -- bash

# 容器内需要挂载宿主机 /sys/kernel/debug
# 或者用 --privileged 启动的调试 pod：
```

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: bpftrace-debug
  namespace: default
spec:
  hostPID: true          # 关键：能看到宿主机所有进程
  hostNetwork: true
  containers:
  - name: bpftrace
    image: quay.io/iovisor/bpftrace:latest
    securityContext:
      privileged: true   # 需要 CAP_BPF, CAP_SYS_ADMIN
    volumeMounts:
    - name: kernel-debug
      mountPath: /sys/kernel/debug
    command: ["sleep", "3600"]
  volumes:
  - name: kernel-debug
    hostPath:
      path: /sys/kernel/debug
  tolerations:
  - operator: Exists     # 调度到目标节点
  nodeSelector:
    kubernetes.io/hostname: node-xxx  # 指定到出问题的节点
```

## 常用 one-liner 速查表

```bash
# ====== 文件 I/O ======

# 打印所有 openat 调用（文件名 + 进程名）
bpftrace -e 'tracepoint:syscalls:sys_enter_openat { printf("%s %s\n", comm, str(args->filename)); }'

# 统计各进程读取字节总量（5 秒）
bpftrace -e 'tracepoint:syscalls:sys_exit_read / args->ret > 0 / { @[comm] = sum(args->ret); } interval:s:5 { print(@); exit(); }'

# 找出频繁打开同一文件的进程（可能是配置热加载 bug）
bpftrace -e 'tracepoint:syscalls:sys_enter_openat { @[str(args->filename), comm]++; } interval:s:10 { print(@); exit(); }'

# ====== CPU ======

# 采样 30 秒，输出用户态热点函数 top10
bpftrace -e 'profile:hz:99 { @[comm, ustack(5)] = count(); } interval:s:30 { print(@); exit(); }'

# 找出内核态 CPU 热点
bpftrace -e 'profile:hz:99 { @[kstack(5)] = count(); } interval:s:15 { print(@); exit(); }'

# 统计各进程 on-CPU 时间（微秒）
bpftrace -e 'software:cpu-clock:1000 { @[comm] = count(); } interval:s:10 { print(@); exit(); }'

# ====== 内存 ======

# 追踪 mmap 调用（找内存映射热点）
bpftrace -e 'tracepoint:syscalls:sys_enter_mmap { @[comm, args->len / 1024] = count(); } interval:s:10 { print(@); exit(); }'

# 统计各进程 brk 调用次数（堆扩张频率）
bpftrace -e 'tracepoint:syscalls:sys_enter_brk { @[comm]++; } interval:s:5 { print(@); exit(); }'

# ====== 网络 ======

# 统计各进程 TCP 发送字节（5 秒）
bpftrace -e 'kprobe:tcp_sendmsg { @[comm] = sum(arg2); } interval:s:5 { print(@); exit(); }'

# 打印 DNS 查询（追踪 /etc/resolv.conf 相关的 sendto）
bpftrace -e 'tracepoint:syscalls:sys_enter_sendto / args->addr != 0 / { printf("%s sendto len=%d\n", comm, args->len); }'

# TCP 重传计数（按目标 IP:port）
bpftrace -e 'tracepoint:tcp:tcp_retransmit_skb { @[ntop(args->daddr), args->dport]++; } interval:s:10 { print(@); exit(); }'

# ====== 进程 ======

# 打印所有新进程的命令行（fork + exec）
bpftrace -e 'tracepoint:sched:sched_process_exec { printf("exec: %s (pid=%d ppid=%d)\n", str(args->filename), pid, curtask->parent->pid); }'

# 统计进程退出码（找非 0 退出）
bpftrace -e 'tracepoint:sched:sched_process_exit { if (args->exit_code != 0) { printf("exit: %s code=%d\n", comm, args->exit_code >> 8); } }'

# 追踪 signal 发送
bpftrace -e 'tracepoint:signal:signal_generate { printf("signal %d -> pid %d from %s\n", args->sig, args->pid, comm); }'

# ====== 锁竞争 ======

# 统计 futex 等待时间（锁竞争热点）
bpftrace -e '
tracepoint:syscalls:sys_enter_futex / args->op == 0 / { @start[tid] = nsecs; }
tracepoint:syscalls:sys_exit_futex / @start[tid] / {
    @wait_us[comm] = hist((nsecs - @start[tid]) / 1000);
    delete(@start[tid]);
}
interval:s:10 { print(@wait_us); exit(); }
'
```

## 与 kubectl 结合的运维工作流

### 标准排查流程

```bash
#!/bin/bash
# debug-pod.sh：快速定位 pod 性能问题

POD=$1
NAMESPACE=${2:-default}
DURATION=${3:-30}

# 1. 找到 pod 所在节点
NODE=$(kubectl get pod $POD -n $NAMESPACE -o jsonpath='{.spec.nodeName}')
echo "[1] Pod $POD 在节点 $NODE"

# 2. 找到容器 PID（通过 kubectl exec 运行 /bin/sh -c 'echo $$'）
CONTAINER_PID=$(kubectl exec $POD -n $NAMESPACE -- /bin/sh -c 'cat /proc/1/status | grep Pid | head -1 | awk "{print \$2}"' 2>/dev/null)
echo "[2] 容器内 PID 1 = $CONTAINER_PID"

# 3. 在节点上找到对应的宿主机 PID
# （容器内 PID 1 对应宿主机上的某个 PID，需要用 nsenter 或 cgroup 方式）
echo "[3] 在节点 $NODE 上运行 bpftrace..."

# 4. 通过 kubectl debug 在节点上运行 bpftrace
kubectl debug node/$NODE -it \
  --image=quay.io/iovisor/bpftrace:latest \
  -- bpftrace -e "
profile:hz:99 / comm == \"$(kubectl exec $POD -n $NAMESPACE -- cat /proc/1/comm 2>/dev/null)\" / {
    @[ustack(8)] = count();
}
interval:s:$DURATION { print(@); exit(); }
"
```

### 配合 Grafana/Prometheus 做告警触发式采样

```bash
#!/bin/bash
# 当 CPU 告警触发时自动跑 bpftrace 采集 profile
# 可以挂在 AlertManager webhook 里

TARGET_POD=$1
NAMESPACE=$2

# 自动找节点，创建临时 bpftrace pod，采集 30 秒，结果上传 S3
NODE=$(kubectl get pod $TARGET_POD -n $NAMESPACE -o jsonpath='{.spec.nodeName}')
COMM=$(kubectl exec $TARGET_POD -n $NAMESPACE -- cat /proc/1/comm 2>/dev/null | tr -d '\n')
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

kubectl run bpftrace-auto-$TIMESTAMP \
  --image=quay.io/iovisor/bpftrace:latest \
  --restart=Never \
  --overrides="{
    \"spec\": {
      \"hostPID\": true,
      \"nodeName\": \"$NODE\",
      \"containers\": [{
        \"name\": \"bpftrace\",
        \"image\": \"quay.io/iovisor/bpftrace:latest\",
        \"securityContext\": {\"privileged\": true},
        \"command\": [\"bpftrace\", \"-e\",
          \"profile:hz:99 / comm == \\\"$COMM\\\" / { @[ustack(10)] = count(); } interval:s:30 { print(@); exit(); }\"]
      }]
    }
  }" \
  --attach=true \
  --rm=true 2>&1 | \
  /opt/flamegraph/flamegraph.pl > /tmp/auto_profile_$TIMESTAMP.svg

echo "Profile saved: /tmp/auto_profile_$TIMESTAMP.svg"
```

### 持久化常用脚本

```bash
# 建议在每台节点上放一个脚本目录
# /opt/bpftrace-scripts/

# slow-io.bt：追踪慢 I/O
cat > /opt/bpftrace-scripts/slow-io.bt << 'EOF'
// 使用方式: bpftrace slow-io.bt [进程名] [阈值ms]
// 默认追踪所有进程，阈值 10ms

tracepoint:syscalls:sys_enter_openat,
tracepoint:syscalls:sys_enter_read,
tracepoint:syscalls:sys_enter_write
{
    @start[tid] = nsecs;
}

tracepoint:syscalls:sys_exit_openat,
tracepoint:syscalls:sys_exit_read,
tracepoint:syscalls:sys_exit_write
/ @start[tid] /
{
    $delta_ms = (nsecs - @start[tid]) / 1000000;
    if ($delta_ms > 10) {
        printf("[SLOW] %s %s took %d ms\n", comm, probe, $delta_ms);
    }
    delete(@start[tid]);
}
EOF

# net-retrans.bt：实时 TCP 重传监控
cat > /opt/bpftrace-scripts/net-retrans.bt << 'EOF'
tracepoint:tcp:tcp_retransmit_skb
{
    @[ntop(args->saddr), args->sport, ntop(args->daddr), args->dport]++;
}

interval:s:5 {
    time("%H:%M:%S retransmit stats:\n");
    print(@);
    clear(@);
}
EOF
```

## 一些使用注意事项

**overhead 估算**：

- `profile:hz:99` 采样：overhead < 1%，可以在生产用
- kprobe/kretprobe 挂高频函数（如 `vfs_read`）：overhead 5-20%，谨慎用于生产
- 打印大量 printf：overhead 极高，生产环境用聚合（`@map`）替代打印

**符号解析**：Go 程序默认保留符号，但 `-ldflags="-s -w"` 会 strip 掉。Java/Python 的用户栈需要对应语言的 frame pointer 支持（JVM 需要 `-XX:+PreserveFramePointer`，Python 需要 `--enable-profiling` 编译）。

**内核版本**：kprobe 在不同内核版本函数签名可能变化，tracepoint 的 ABI 更稳定，优先用 tracepoint。
