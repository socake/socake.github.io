---
title: "Linux 系统管理精要——DevOps 工程师必知的系统层知识"
date: 2024-09-16T13:36:00+08:00
draft: false
tags: ["Linux", "DevOps", "系统管理", "systemd", "运维"]
categories: ["Linux"]
series: ["DevOps 工程师成长路径"]
description: Linux 系统管理精要：DevOps 工程师必须掌握的进程管理、systemd、ulimit、/proc 文件系统和内核参数调优实战
summary: 做了多年 DevOps，我越来越觉得 Linux 系统层的知识是一切排障的基础。当 Kubernetes Pod 莫名被杀、Java 服务突然无响应、磁盘 IO 飙高导致整机卡顿——最终都要落到系统层来定位。这篇文章把我在生产中最常用的系统管理技能系统梳理一遍。
toc: true
math: false
diagram: false
keywords: ["Linux", "进程管理", "systemd", "ulimit", "内核参数", "性能调优", "vmstat", "iostat"]
params:
  reading_time: true
---

做了多年 DevOps，我越来越觉得 Linux 系统层的知识是一切排障的基础。当 Kubernetes Pod 莫名被杀、Java 服务突然无响应、磁盘 IO 飙高导致整机卡顿——最终都要落到系统层来定位。这篇文章把我在生产中最常用的系统管理技能系统梳理一遍。

## 进程诊断：找出异常的那个家伙

### ps aux 的正确读法

`ps aux` 是最常用的快照工具，但很多人只会看 PID 和 COMMAND，忽略了关键字段：

```bash
# 按内存使用量排序，找出内存大户
ps aux --sort=-%mem | head -20

# 按 CPU 使用率排序
ps aux --sort=-%cpu | head -20

# 查找特定进程的完整信息
ps aux | grep java | grep -v grep
```

输出字段解读：
- **%CPU**：上次刷新到现在的 CPU 使用率，不是实时的
- **%MEM**：进程使用的物理内存占总内存的百分比
- **VSZ**：虚拟内存大小（含 mmap 的文件），通常比 RSS 大很多
- **RSS**：实际占用的物理内存（Resident Set Size），这个才是真实内存占用
- **STAT**：进程状态，`S` 睡眠、`R` 运行、`D` 不可中断睡眠（通常是 IO 等待）、`Z` 僵尸

有一次生产 Java 服务内存报警，RSS 一直涨到 12GB 不释放。通过 `ps` 发现 VSZ 是 RSS 的两倍多，基本可以确定是堆外内存泄漏（DirectByteBuffer），后来用 `-XX:MaxDirectMemorySize` 加限制并配合 NMT（Native Memory Tracking）定位到了具体代码。

### top：实时监控和 Load Average 解读

```bash
# top 交互模式常用按键
top
# 按 M 按内存排序
# 按 P 按 CPU 排序
# 按 1 展开每个 CPU 核心
# 按 H 查看线程（对 Java 多线程排查很有用）
```

**Load Average 的正确理解**：

```
load average: 3.20, 2.85, 2.41
              1分钟  5分钟  15分钟
```

Load Average 表示运行队列中等待 CPU 或等待 IO 的进程数。关键是要结合 CPU 核心数来判断：

- 4 核机器 load average = 4.0，说明满负荷但不超载
- 4 核机器 load average = 8.0，说明有进程在排队等待，系统过载
- Load 持续升高（1min > 5min > 15min）说明问题在恶化

有一次值班，load average 跑到了 24（8 核机器），但 CPU 使用率只有 30%。这个组合说明不是 CPU 瓶颈，而是 IO 等待导致进程阻塞。后来用 `iostat -x` 确认了磁盘 %util 达到 100%。

### lsof：找出文件句柄泄漏

```bash
# 查看某进程打开的所有文件
lsof -p <PID>

# 统计进程打开的文件数量（排查 fd 泄漏）
lsof -p <PID> | wc -l

# 查找哪个进程占用了某个端口
lsof -i :8080

# 查找已被删除但还被占用的文件（磁盘空间删文件后不释放的元凶）
lsof | grep deleted

# 找出打开文件数最多的进程（Top 10）
lsof | awk '{print $2}' | sort | uniq -c | sort -rn | head -10
```

**经典陷阱**：磁盘明明删了日志文件，`df -h` 显示空间没减少。这就是因为进程还持有文件句柄，文件虽然从目录项删除，但 inode 和数据块还在。`lsof | grep deleted` 能找到这些文件，重启对应服务或让进程重新打开日志文件（`kill -USR1`）即可。

---

## systemd 实战：管好你的服务

### Unit File 结构解析

```ini
# /etc/systemd/system/myapp.service
[Unit]
Description=My Application Service
Documentation=https://example.com/docs
# 依赖关系：network.target 启动后才启动本服务
After=network.target postgresql.service
# 强依赖：postgresql 挂了本服务也停
Requires=postgresql.service

[Service]
Type=simple
User=myapp
Group=myapp
WorkingDirectory=/opt/myapp
ExecStart=/opt/myapp/bin/server --config /etc/myapp/config.yaml
ExecReload=/bin/kill -HUP $MAINPID
# 异常退出自动重启，5 秒间隔，最多重启 3 次
Restart=on-failure
RestartSec=5s
StartLimitInterval=60s
StartLimitBurst=3

# 资源限制
LimitNOFILE=65536
LimitNPROC=4096

# 环境变量
EnvironmentFile=/etc/myapp/env
Environment=GOMAXPROCS=4

[Install]
WantedBy=multi-user.target
```

常用命令：

```bash
# 重新加载 unit 文件（修改配置后必须执行）
systemctl daemon-reload

# 启动/停止/重启/状态
systemctl start|stop|restart|status myapp

# 开机自启
systemctl enable myapp

# 查看服务依赖树
systemctl list-dependencies myapp
```

### journalctl 高效查询

```bash
# 查看服务最新日志（实时跟踪）
journalctl -u myapp -f

# 查看最近 100 行
journalctl -u myapp -n 100

# 查看最近 1 小时的日志
journalctl -u myapp --since "1 hour ago"

# 查看指定时间范围
journalctl -u myapp --since "2026-04-12 10:00:00" --until "2026-04-12 11:00:00"

# 只看 ERROR 级别
journalctl -u myapp -p err

# 输出为 JSON（便于脚本处理）
journalctl -u myapp -o json-pretty | head -50

# 查看上次启动的日志（排查启动失败时很有用）
journalctl -u myapp -b -1
```

### 常见启动失败排查

```bash
# 第一步：看状态和最近日志
systemctl status myapp

# 典型输出：
# ● myapp.service - My Application Service
#    Loaded: loaded (/etc/systemd/system/myapp.service; enabled)
#    Active: failed (Result: exit-code)
#   Process: 12345 ExecStart=/opt/myapp/bin/server (code=exited, status=1/FAILURE)

# 第二步：看完整日志
journalctl -u myapp -n 50 --no-pager
```

**常见失败原因**：

1. **ExecStart 路径错误**：二进制不存在或没有执行权限
   ```bash
   ls -la /opt/myapp/bin/server
   ```

2. **After= 依赖未就绪**：数据库服务慢启动，应用启动时连接失败
   ```ini
   # 解决方案：增加重试逻辑，或使用 ExecStartPre 做健康检查
   ExecStartPre=/bin/sh -c 'until pg_isready -h localhost; do sleep 1; done'
   ```

3. **端口被占用**：`address already in use`
   ```bash
   lsof -i :8080
   ```

4. **权限问题**：User= 指定的用户无法读取配置文件
   ```bash
   sudo -u myapp cat /etc/myapp/config.yaml
   ```

---

## ulimit 与 /proc：突破系统限制

### ulimit 参数管理

```bash
# 查看当前 shell 的所有限制
ulimit -a

# 关键参数：
# open files (-n): 文件描述符上限，默认 1024，高并发服务需要调大
# max user processes (-u): 线程/进程数上限
# stack size (-s): 栈大小，默认 8MB
# virtual memory (-v): 虚拟内存上限

# 临时修改（只对当前 shell 和子进程生效）
ulimit -n 65536

# 永久修改：编辑 /etc/security/limits.conf
cat >> /etc/security/limits.conf << 'EOF'
myapp soft nofile 65536
myapp hard nofile 65536
myapp soft nproc 32768
myapp hard nproc 32768
* soft core unlimited
EOF
```

**踩坑记录**：有一次 Nginx 在高峰期出现 `too many open files`，但明明 `ulimit -n` 已经改成了 65536。后来发现 systemd 管理的服务需要在 unit file 里单独设置 `LimitNOFILE=65536`，`/etc/security/limits.conf` 对 systemd 服务不生效。

### /proc 文件系统实战

```bash
# 查看进程的文件描述符使用情况
ls /proc/<PID>/fd | wc -l

# 查看进程打开的文件描述符详情
ls -la /proc/<PID>/fd

# 查看进程内存映射（分析内存使用组成）
cat /proc/<PID>/maps

# 查看进程状态详情
cat /proc/<PID>/status
# VmRSS: 实际物理内存
# VmPeak: 历史最高虚拟内存
# Threads: 线程数

# 查看进程的 ulimit 设置（运行中进程的实际限制）
cat /proc/<PID>/limits

# 实时调整内核参数（立即生效，重启失效）
echo 1 > /proc/sys/net/ipv4/tcp_tw_reuse
sysctl -w net.ipv4.tcp_tw_reuse=1
```

---

## 内核参数：生产必备调优清单

编辑 `/etc/sysctl.conf` 永久生效，`sysctl -p` 加载：

```bash
# /etc/sysctl.conf 生产调优配置

# ============ 网络连接 ============
# TCP TIME_WAIT 连接复用（高并发短连接必开）
net.ipv4.tcp_tw_reuse = 1

# 监听队列大小（nginx/java 等高并发服务必调）
# 默认 128，高并发下会导致 connection refused
net.core.somaxconn = 65535
net.ipv4.tcp_max_syn_backlog = 65535

# 增大本地端口范围（防止端口耗尽）
net.ipv4.ip_local_port_range = 10000 65535

# TCP keepalive（减少无效连接占用，默认 2 小时太长）
net.ipv4.tcp_keepalive_time = 600
net.ipv4.tcp_keepalive_intvl = 30
net.ipv4.tcp_keepalive_probes = 3

# ============ 内存管理 ============
# 内存交换倾向（0=尽量不用 swap，100=积极使用）
# 数据库和 JVM 服务建议设 1，防止 swap 导致性能突降
vm.swappiness = 1

# 脏页回写策略
vm.dirty_ratio = 15
vm.dirty_background_ratio = 5

# ============ 文件系统 ============
# 系统级文件描述符上限（所有进程之和）
fs.file-max = 2097152

# inotify 监听数量（k8s 节点必调）
fs.inotify.max_user_watches = 1048576
fs.inotify.max_user_instances = 512
```

应用配置：

```bash
# 加载配置
sysctl -p /etc/sysctl.conf

# 验证生效
sysctl net.core.somaxconn
sysctl -a | grep tcp_tw_reuse
```

---

## 性能快照：vmstat/iostat/sar 三板斧

### vmstat：全局性能快照

```bash
# 每秒输出一次，共 10 次
vmstat 1 10
```

```
procs -----------memory---------- ---swap-- -----io---- -system-- ------cpu-----
 r  b   swpd   free   buff  cache   si   so    bi    bo   in   cs us sy id wa st
 2  0      0 1024M  512M  4096M    0    0     0   128 3200 6400 45  8 45  2  0
 ```

关键字段：
- **r**：运行队列中的进程数，持续 > CPU 核心数说明 CPU 不够
- **b**：等待 IO 的进程数，持续 > 0 说明 IO 有瓶颈
- **si/so**：swap in/out，非零就是在用 swap，服务性能会急剧下降
- **wa**（CPU wa）：CPU 等待 IO 的时间比例，持续 > 20% 说明 IO 是瓶颈

### iostat：磁盘 IO 深度分析

```bash
# -x 显示扩展信息，-d 只看磁盘，每秒刷新
iostat -xd 1

# 关注 nvme0n1 这块盘
Device    r/s   w/s  rMB/s  wMB/s  await  r_await  w_await  util
nvme0n1  150.0  800.0  5.0   40.0    2.5     1.2      2.8   85.0
```

关键字段：
- **await**：IO 请求的平均等待时间（毫秒），SSD 正常 < 5ms，HDD < 20ms
- **%util**：磁盘利用率，接近 100% 说明磁盘饱和
- **r_await vs w_await**：读写延迟分开看，帮助判断读密集还是写密集

### sar：历史性能分析

```bash
# 查看昨天的 CPU 使用情况
sar -u -f /var/log/sa/sa11  # sa + 日期

# 查看今天每小时的内存使用
sar -r 3600

# 查看网络流量历史
sar -n DEV 1 5

# 查看过去 1 小时的磁盘 IO
sar -d -s 10:00:00 -e 11:00:00
```

`sar` 的最大价值在于**历史数据**。有一次凌晨 3 点出现告警，但早上才处理，这时候 `top`/`vmstat` 已经看不到问题了，`sar` 的历史记录让我找到了凌晨负载飙高的精确时间点，对应到了定时任务的执行时间。

---

## 排查思路总结

遇到生产问题，我的排查顺序：

1. `uptime` — 看 load average 和运行时间
2. `vmstat 1 5` — 快速判断 CPU/内存/IO/swap 哪个有问题
3. `top` or `htop` — 找到占用资源最高的进程
4. `iostat -xd 1` — 如果怀疑 IO，深挖磁盘
5. `lsof -p <PID>` — 如果怀疑句柄泄漏
6. `journalctl -u <service> -n 100` — 如果是 systemd 服务，看日志
7. `cat /proc/<PID>/limits` — 确认进程的实际资源限制

系统层的知识是所有上层工具的基础，不管是 K8s、Docker 还是各种中间件，底层都是这些 Linux 原语。理解了这一层，很多"玄学"问题都会变得透明。
