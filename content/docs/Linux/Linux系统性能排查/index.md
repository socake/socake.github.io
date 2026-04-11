---
title: "Linux 系统性能排查手册"
date: 2025-12-09T10:00:00+08:00
draft: false
tags: ["Linux", "运维", "性能排查", "监控"]
categories: ["Linux"]
description: "系统性梳理 CPU、内存、磁盘 IO、网络四个维度的性能排查命令与方法论"
summary: "覆盖 top/htop/mpstat/vmstat/iostat/sar 等核心命令，结合 iowait/softirq/CPU 窃取等指标含义，提供完整排查流程和组合命令速查。"
toc: true
math: false
diagram: false
keywords: ["Linux性能排查", "CPU排查", "内存泄漏", "iostat", "sar", "vmstat"]
params:
  reading_time: true
---

## 一、排查思路总览

性能问题排查遵循"先定位资源瓶颈，再定位进程，最后定位代码"的三层模型。

```
用户反馈慢/超时
       |
  [整体资源概览] top / uptime / dstat
       |
  ┌────┴────┐
  CPU异常   内存异常   IO异常   网络异常
  mpstat    free       iostat   sar/iftop
  vmstat    /proc      iotop    ss/nethogs
  perf      meminfo    dstat    tcpdump
       |
  定位进程 (top P/M, iotop, nethogs)
       |
  定位代码 (strace, perf top, pprof)
```

---

## 二、CPU 排查

### 2.1 top 基础用法

```bash
top                     # 交互模式
top -b -n 3 -d 2        # 批量输出3次，间隔2秒
top -p 1234,5678        # 只看指定PID
top -u www-data         # 只看指定用户

# top 交互键
# P  按 CPU 排序
# M  按内存排序
# 1  展开每个 CPU 核
# c  显示完整命令行
# H  显示线程
# k  kill 进程
# q  退出
```

top 首部各字段含义：

| 字段 | 含义 |
|------|------|
| us | 用户态 CPU |
| sy | 内核态 CPU |
| ni | nice 优先级调整的进程 |
| id | 空闲 CPU |
| wa | 等待 IO（iowait） |
| hi | 硬件中断 |
| si | 软中断（softirq） |
| st | 被宿主机窃取的 CPU（steal） |

### 2.2 CPU 窃取（steal time）

在虚拟机/云主机上，`st` 值偏高（>5%）说明宿主机过载，本实例分配不到足够 CPU 时间。这是云上性能问题的常见隐因。

```bash
# 持续观察 steal
vmstat 1 10 | awk '{print $1, $15, $16, $17}'
# 输出列: r(运行队列), us, sy, id, wa, st 等视版本而定

# 用 top -1 展开所有核，看各核 st 值
top -b -n 1 | grep -E "^%Cpu|Cpu"
```

### 2.3 iowait 含义与排查

`wa`（iowait）表示 CPU 空闲且有进程在等待 IO 完成的时间占比。**iowait 高不等于 IO 慢**，需结合 iostat 确认磁盘实际利用率。

```bash
# 判断 iowait 是否真正因为磁盘饱和
iostat -x 1 5
# 关注 %util（设备利用率），接近 100% 说明磁盘饱和
# await 是平均 IO 等待时间(ms)，r_await/w_await 分读写
```

### 2.4 softirq 高的排查

softirq 高通常出现在高网络流量或高频定时器触发场景。

```bash
# 查看各类 softirq 的计数
cat /proc/softirqs

# 哪个 CPU 核在处理网络 softirq
watch -n 1 'grep -E "NET_RX|NET_TX" /proc/softirqs'

# 网卡多队列绑定（避免所有中断集中到 cpu0）
cat /proc/interrupts | grep eth0
# 使用 irqbalance 或手动设置 /proc/irq/N/smp_affinity
```

### 2.5 mpstat 多核详情

```bash
mpstat -P ALL 1 5       # 每秒输出一次，共5次，所有 CPU 核
mpstat -P 0,1,2 1       # 只看 0/1/2 号核
mpstat -I SUM 1         # 中断汇总统计
```

### 2.6 vmstat CPU 相关列

```bash
vmstat 1 10
# r  运行队列长度（持续 > CPU核数 说明 CPU 饱和）
# b  阻塞在 IO 的进程数
# us/sy/id/wa/st 同 top
```

运行队列 `r` 是判断 CPU 是否饱和的最直接指标。

---

## 三、内存排查

### 3.1 free 命令

```bash
free -h                 # 人类可读单位
free -m                 # MB 单位
free -s 2 -c 5         # 每2秒刷新，共5次

# 输出解析
#               total   used    free    shared  buff/cache  available
# Mem:          15Gi    8.2Gi   1.1Gi   512Mi   5.9Gi       6.8Gi
# available = free + 可回收的 buff/cache，是实际可用内存
```

### 3.2 /proc/meminfo 详细分析

```bash
cat /proc/meminfo

# 关键字段
# MemTotal       物理内存总量
# MemFree        完全空闲
# MemAvailable   实际可用（包含可回收缓存）
# Buffers        块设备读写缓冲
# Cached         文件系统页缓存
# SwapCached     已被 swap 但又读回内存的页
# Active(anon)   活跃匿名页（进程堆/栈）
# Inactive(anon) 不活跃匿名页（候选 swap out）
# Shmem          共享内存（包括 tmpfs）
# Slab           内核 slab 分配器使用量
# SReclaimable   可回收 slab（如 dentry cache）
# SUnreclaim     不可回收 slab
# Committed_AS   所有进程申请的虚拟内存总量
# VmallocTotal   vmalloc 区域大小
```

### 3.3 内存泄漏判断

```bash
# 方法1：观察进程 RSS 是否持续增长
watch -n 5 'ps aux --sort=-%mem | head -20'

# 方法2：valgrind（需重新运行程序）
valgrind --leak-check=full ./myapp

# 方法3：smem 按 PSS 统计（更准确）
smem -tk -s pss | tail -20

# 方法4：观察 /proc/PID/status
cat /proc/$(pgrep myapp)/status | grep -E "VmRSS|VmSize|VmSwap"

# 方法5：pmap 查看进程内存映射
pmap -x $(pgrep myapp) | tail -5
```

RSS 持续增长且未触发 GC/释放，是内存泄漏的典型特征。

### 3.4 OOM 事件查看

```bash
# 查看 OOM kill 记录
dmesg | grep -i "oom\|killed process"
journalctl -k | grep -i oom

# 查看当前 OOM 分数
cat /proc/$(pgrep myapp)/oom_score
cat /proc/$(pgrep myapp)/oom_adj

# 保护关键进程不被 OOM kill
echo -1000 > /proc/$(pgrep sshd)/oom_score_adj
```

### 3.5 swap 分析

```bash
# 查看 swap 使用
swapon -s
cat /proc/swaps

# 哪些进程在用 swap
for pid in /proc/[0-9]*; do
  comm=$(cat $pid/comm 2>/dev/null)
  swap=$(grep VmSwap $pid/status 2>/dev/null | awk '{print $2}')
  [ -n "$swap" ] && [ "$swap" -gt 0 ] && echo "$comm: ${swap}kB"
done | sort -t: -k2 -rn | head -20
```

---

## 四、磁盘 IO 排查

### 4.1 iostat 详解

```bash
iostat -x 1 5           # 扩展统计，1秒间隔，5次
iostat -x -d sda 1      # 只看 sda 设备
iostat -x -m 1          # MB 单位

# 关键列说明
# r/s      每秒读请求数
# w/s      每秒写请求数
# rMB/s    读吞吐量
# wMB/s    写吞吐量
# rrqm/s   每秒合并的读请求数（合并说明顺序IO）
# wrqm/s   每秒合并的写请求数
# r_await  读平均等待时间(ms)
# w_await  写平均等待时间(ms)
# aqu-sz   平均队列深度（>1 说明设备有排队）
# %util    设备利用率（接近100%说明饱和）
```

判断读写瓶颈：

| 指标 | 正常 | 警戒 | 含义 |
|------|------|------|------|
| %util | <70% | >90% | 磁盘饱和度 |
| r_await | <10ms | >50ms | 读延迟（SSD应<1ms） |
| w_await | <10ms | >50ms | 写延迟 |
| aqu-sz | <1 | >4 | IO 排队严重 |

### 4.2 iotop 定位进程

```bash
iotop                   # 需要 root，实时显示
iotop -o                # 只显示有 IO 的进程
iotop -b -n 5 -d 2     # 批量输出5次
iotop -p 1234           # 只看指定 PID
```

### 4.3 dstat 综合统计

```bash
dstat -cdngy 1          # cpu/disk/net/page/sys
dstat --top-io          # 显示 IO 最高进程
dstat --top-cpu --top-io --top-mem 1   # 各维度 top 进程

# 输出到文件
dstat --output /tmp/dstat.csv 1 60
```

### 4.4 blktrace / blkparse（深度分析）

```bash
# 追踪块设备 IO
blktrace -d /dev/sda -o trace
blkparse trace.blktrace.0 | head -50

# 更简单的替代
biotop-bpfcc 1          # 需要 bpfcc-tools
```

---

## 五、网络排查

### 5.1 sar 网络统计

```bash
sar -n DEV 1 5          # 网卡吞吐量（rxpck/s, txpck/s, rxMB/s, txMB/s）
sar -n EDEV 1 5         # 网卡错误统计（丢包、错误）
sar -n SOCK 1 5         # socket 统计
sar -n TCP,ETCP 1 5     # TCP 连接/错误统计

# 查历史数据
sar -n DEV -f /var/log/sysstat/sa$(date +%d)
```

### 5.2 nethogs 按进程统计带宽

```bash
nethogs                 # 实时，按进程
nethogs eth0            # 指定网卡
nethogs -t -d 1 eth0   # 每秒刷新，文本模式
```

### 5.3 iftop 实时流量

```bash
iftop                   # 需要 root
iftop -i eth0           # 指定网卡
iftop -n                # 不解析主机名（更快）
iftop -B                # 以字节显示
```

### 5.4 连接数统计

```bash
# 各状态连接数
ss -ant | awk '{print $1}' | sort | uniq -c | sort -rn

# TIME_WAIT 数量
ss -ant | grep TIME-WAIT | wc -l

# 连接数最多的远端 IP
ss -ant | awk '/ESTABLISHED/{print $5}' | cut -d: -f1 | sort | uniq -c | sort -rn | head -10
```

---

## 六、综合排查流程

```
1. 先看整体负载
   uptime             <- load average 是否高于 CPU 核数
   top -b -n 1        <- 哪个资源最紧张

2. 如果 CPU 高
   mpstat -P ALL 1    <- 哪些核高
   top P              <- 哪个进程占 CPU
   pidstat -u 1       <- 进程级 CPU 明细

3. 如果 iowait 高
   iostat -x 1        <- 哪个磁盘，读还是写
   iotop -o           <- 哪个进程在做 IO
   lsof +D /path      <- 谁在访问某目录

4. 如果内存紧张
   free -h            <- available 还剩多少
   ps aux --sort=-%mem| head    <- 哪个进程占内存
   dmesg | grep oom   <- 是否有 OOM

5. 如果网络有问题
   sar -n DEV 1       <- 带宽是否打满
   ss -ant            <- 连接状态
   nethogs            <- 哪个进程占带宽
   tcpdump -i eth0 -w /tmp/cap.pcap   <- 抓包分析
```

---

## 七、常用组合命令速查

```bash
# 1. 快速全局概览（1分钟诊断脚本）
echo "=== Load ===" && uptime
echo "=== CPU ===" && mpstat 1 1 | tail -3
echo "=== Mem ===" && free -h
echo "=== Disk IO ===" && iostat -x 1 1 | tail -5
echo "=== Net ===" && sar -n DEV 1 1 | grep -v ^$

# 2. 找出 CPU 最高的10个进程
ps aux --sort=-%cpu | head -11

# 3. 找出内存最高的10个进程
ps aux --sort=-%mem | head -11

# 4. 找出最近5分钟的 OOM
dmesg -T | grep -i oom | tail -20

# 5. 磁盘 IO 热点进程（需要 root）
pidstat -d 1 5 | sort -k4 -rn | head -10

# 6. 按进程统计网络连接数
ss -antp | awk 'NR>1{print $6}' | grep -oP 'pid=\K[0-9]+' | \
  xargs -I{} sh -c 'echo $(cat /proc/{}/comm 2>/dev/null): {}' | \
  sort | uniq -c | sort -rn | head -10

# 7. 找出打开文件数最多的进程
lsof 2>/dev/null | awk '{print $2, $1}' | sort | uniq -c | sort -rn | head -10

# 8. 实时监控关键指标（每秒刷新）
watch -n 1 'echo "CPU:"; mpstat 1 1 | tail -1; echo "MEM:"; free -m | head -2'

# 9. 查看系统中断分布
watch -n 1 'cat /proc/interrupts | head -20'

# 10. 持续记录性能数据（用于事后分析）
sar -o /tmp/sar_output.bin 5 720 &   # 每5秒采集，采集1小时
# 事后分析
sar -f /tmp/sar_output.bin -u -n DEV
```

---

## 八、工具安装参考

```bash
# RHEL/CentOS
yum install -y sysstat iotop dstat nethogs iftop procps-ng

# Debian/Ubuntu
apt install -y sysstat iotop dstat nethogs iftop procps smem

# 启用 sysstat 数据采集
systemctl enable --now sysstat
# 或修改 /etc/default/sysstat，将 ENABLED="false" 改为 ENABLED="true"
```

---

## 九、/proc 关键路径速查

| 路径 | 用途 |
|------|------|
| `/proc/cpuinfo` | CPU 型号、核数、频率 |
| `/proc/meminfo` | 内存详细统计 |
| `/proc/loadavg` | 负载均值（1/5/15分钟）|
| `/proc/interrupts` | 中断计数 |
| `/proc/softirqs` | 软中断计数 |
| `/proc/diskstats` | 磁盘 IO 原始统计 |
| `/proc/net/dev` | 网卡收发包统计 |
| `/proc/net/tcp` | TCP 连接表 |
| `/proc/PID/status` | 进程状态和内存 |
| `/proc/PID/io` | 进程 IO 统计 |
| `/proc/PID/fd` | 进程打开的文件描述符 |
| `/proc/PID/maps` | 进程内存映射 |
| `/proc/PID/cmdline` | 完整命令行 |
| `/sys/block/sda/queue/scheduler` | IO 调度器 |
| `/sys/block/sda/queue/nr_requests` | IO 队列深度 |
