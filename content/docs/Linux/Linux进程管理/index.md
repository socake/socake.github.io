---
title: "Linux 进程管理与作业控制"
date: 2025-12-09T10:00:00+08:00
draft: false
tags: ["Linux", "运维", "进程管理", "systemd", "tmux"]
categories: ["Linux"]
description: "涵盖进程查看、信号管理、优先级调整、后台作业控制、systemd 服务管理与资源限制的完整手册"
summary: "从 ps/pstree 进程查看到 kill/pkill 信号发送，从 nice/ionice 优先级调整到 screen/tmux 会话管理，结合 systemctl/journalctl 和 ulimit 资源控制。"
toc: true
math: false
diagram: false
keywords: ["进程管理", "systemctl", "tmux", "kill信号", "nice", "ulimit", "journalctl"]
params:
  reading_time: true
---

## 一、进程查看

### 1.1 ps 命令

```bash
ps aux                          # BSD 风格，显示所有用户进程
ps -ef                          # UNIX 风格，显示所有进程含父进程
ps aux --sort=-%cpu | head -11  # 按 CPU 降序
ps aux --sort=-%mem | head -11  # 按内存降序
ps -u nginx                     # 只看 nginx 用户的进程
ps -p 1234,5678                 # 看指定 PID
ps -o pid,ppid,comm,%cpu,%mem,stat,start,time  # 自定义列
```

`ps aux` 各列含义：

| 列 | 含义 |
|----|------|
| USER | 运行用户 |
| PID | 进程 ID |
| %CPU | CPU 使用率 |
| %MEM | 内存使用率（RSS/总物理内存）|
| VSZ | 虚拟内存大小（KB）|
| RSS | 实际物理内存（KB）|
| TTY | 终端（? 表示无终端）|
| STAT | 进程状态（见下表）|
| START | 启动时间 |
| TIME | 累计 CPU 时间 |
| COMMAND | 命令行 |

### 1.2 进程状态含义

| 状态码 | 含义 |
|--------|------|
| R | Running，运行中或在运行队列等待 |
| S | Sleeping，可中断睡眠（等待事件）|
| D | Disk Sleep，不可中断睡眠（等待 IO，不能 kill）|
| Z | Zombie，僵尸进程（已退出但父进程未回收）|
| T | Stopped，被信号暂停（如 Ctrl+Z）|
| t | Traced，被调试器暂停 |
| I | Idle，空闲内核线程 |
| W | 换页中（历史状态，现代内核几乎不出现）|
| X | Dead，已死亡（不应在 ps 中看到）|

附加状态符号：

| 符号 | 含义 |
|------|------|
| < | 高优先级（nice 值为负）|
| N | 低优先级（nice 值为正）|
| L | 有内存锁（mlockall）|
| s | 会话领导者 |
| l | 多线程 |
| + | 前台进程组 |

```bash
# D 状态进程（等待 IO），正常 D 状态是暂时的
# 如果持续 D 状态，通常是存储问题
ps aux | awk '$8 ~ /^D/ {print $0}'

# 查找僵尸进程
ps aux | grep Z
ps -eo pid,ppid,stat,comm | awk '$3 == "Z"'

# 清理僵尸进程（通过 kill 父进程让 init 接管）
kill -CHLD $(ps -o ppid= -p <zombie_pid>)
```

### 1.3 pstree 进程树

```bash
pstree                          # 显示进程树
pstree -p                       # 显示 PID
pstree -u                       # 显示用户
pstree -a                       # 显示命令行参数
pstree 1234                     # 以指定 PID 为根
pstree -H 1234                  # 高亮指定 PID 的路径
```

### 1.4 其他进程查看工具

```bash
# pidstat（来自 sysstat 包）
pidstat 1                       # 每秒显示所有进程 CPU
pidstat -u -p 1234 1            # 指定进程 CPU
pidstat -r 1                    # 内存统计
pidstat -d 1                    # IO 统计
pidstat -w 1                    # 上下文切换统计

# lsof 查看进程打开的文件
lsof -p 1234                    # 指定进程的文件
lsof -u nginx                   # 指定用户的文件
lsof +D /var/log                # 谁在使用某目录
lsof -i :80                     # 谁在使用80端口
lsof -i TCP:1-1024              # TCP 1-1024 端口
```

---

## 二、信号管理

### 2.1 常用信号含义

```bash
kill -l                         # 列出所有信号
```

| 信号编号 | 信号名 | 含义 |
|----------|--------|------|
| 1 | SIGHUP | 挂起/重载配置（daemon 常用）|
| 2 | SIGINT | 键盘中断（Ctrl+C）|
| 3 | SIGQUIT | 键盘退出（Ctrl+\，产生 core dump）|
| 9 | SIGKILL | 强制终止（不可屏蔽，进程无法捕获）|
| 10 | SIGUSR1 | 用户自定义信号1（各程序含义不同）|
| 12 | SIGUSR2 | 用户自定义信号2 |
| 15 | SIGTERM | 优雅终止（默认，进程可以清理后退出）|
| 17 | SIGCHLD | 子进程退出通知（父进程处理僵尸）|
| 18 | SIGCONT | 继续运行（配合 SIGSTOP 使用）|
| 19 | SIGSTOP | 暂停进程（不可屏蔽）|
| 20 | SIGTSTP | 键盘暂停（Ctrl+Z，可屏蔽）|

常见程序对 SIGUSR1/SIGHUP 的约定：

| 程序 | SIGHUP | SIGUSR1 |
|------|--------|---------|
| nginx | 重载配置 | 重开日志 |
| apache | 优雅重启 | 重开日志 |
| rsyslog | 重载配置 | — |
| logrotate | — | copytruncate 后发此信号 |

### 2.2 kill

```bash
kill 1234                       # 发送 SIGTERM（默认）
kill -9 1234                    # 发送 SIGKILL
kill -SIGTERM 1234              # 等同于 kill -15
kill -l                         # 列出所有信号
kill -0 1234                    # 测试进程是否存在（不发实际信号）

# 批量 kill
kill $(ps aux | grep myapp | grep -v grep | awk '{print $2}')
```

### 2.3 pkill / killall

```bash
pkill nginx                     # 按名称 kill（发 SIGTERM）
pkill -9 nginx                  # 发 SIGKILL
pkill -HUP nginx                # 重载（发 SIGHUP）
pkill -u www-data               # kill 某用户所有进程
pkill -f "python manage.py"     # 按完整命令行匹配（-f）

killall nginx                   # 精确名称匹配（比 pkill 更严格）
killall -w nginx                # 等待进程退出
killall -v nginx                # 显示被 kill 的进程
```

### 2.4 pgrep 查找进程 PID

```bash
pgrep nginx                     # 返回 PID
pgrep -l nginx                  # 返回 PID + 名称
pgrep -a nginx                  # 返回 PID + 完整命令行
pgrep -u www-data               # 指定用户的进程 PID
pgrep -f "python manage.py"     # 按完整命令行
pgrep -c nginx                  # 只返回匹配数量
```

---

## 三、优先级调整

### 3.1 nice / renice（CPU 优先级）

nice 值范围 -20（最高优先级）到 19（最低优先级），默认 0。

```bash
# 以低优先级启动程序（nice 值越高，优先级越低）
nice -n 10 tar czf /backup/data.tar.gz /data
nice -n 19 ./long_running_job.sh    # 最低优先级

# 调整已运行进程的 nice 值（需要 root 才能降低 nice 值）
renice -n 10 -p 1234            # 调整指定 PID
renice -n 5 -u www-data         # 调整某用户所有进程
renice -n -5 -p 1234            # 提升优先级（需要 root）

# 查看当前 nice 值
ps -o pid,ni,comm -p 1234
top  # 在 top 中按 r 键可以对指定 PID renice
```

### 3.2 ionice（IO 优先级）

```bash
# IO 调度类别
# 0 = none（由内核决定）
# 1 = realtime（最高 IO 优先级，分8个级别0-7）
# 2 = best-effort（默认，分8个级别0-7）
# 3 = idle（只在 IO 空闲时才运行，不影响其他进程）

# 以 idle IO 优先级运行
ionice -c 3 rsync -av /data /backup/

# 设置 best-effort 7级（最低）
ionice -c 2 -n 7 -p 1234

# 查看进程的 IO 优先级
ionice -p 1234

# 组合使用（低 CPU 低 IO）
nice -n 19 ionice -c 3 ./batch_process.sh
```

---

## 四、后台作业控制

### 4.1 内置作业控制

```bash
command &                       # 后台运行
Ctrl+Z                          # 暂停当前作业，放到后台
jobs                            # 列出后台作业
jobs -l                         # 包含 PID
fg                              # 将最近的后台作业放到前台
fg %2                           # 将作业编号2放到前台
bg                              # 继续执行暂停的后台作业
bg %2                           # 继续作业编号2

# 查看后台进程
ps T                            # 只看当前终端的进程
```

### 4.2 nohup

```bash
nohup command &                 # 忽略 SIGHUP，输出到 nohup.out
nohup command > /var/log/cmd.log 2>&1 &  # 指定输出文件
```

### 4.3 disown

```bash
# 对于已经在前台运行的命令
Ctrl+Z                          # 先暂停
bg                              # 放到后台
disown %1                       # 从 jobs 列表移除，关闭终端不会 kill

# 查看是否已 disown
jobs -l                         # disown 后不再显示
```

### 4.4 screen vs tmux 对比

| 特性 | screen | tmux |
|------|--------|------|
| 会话持久化 | 支持 | 支持 |
| 窗口管理 | 支持 | 支持（更强大）|
| 垂直分屏 | 不支持（旧版）| 支持 |
| 水平分屏 | 支持 | 支持 |
| 脚本化控制 | 有限 | tmux send-keys 支持 |
| 配置复杂度 | 简单 | 中等 |
| 状态栏 | 简单 | 可深度定制 |
| 推荐程度 | 老系统兼容 | 推荐使用 |

### 4.5 tmux 核心操作

```bash
# 会话管理
tmux new -s mysession           # 新建命名会话
tmux ls                         # 列出所有会话
tmux attach -t mysession        # 重连会话（简写 tmux a -t mysession）
tmux kill-session -t mysession  # 关闭会话
tmux rename-session -t old new  # 重命名会话

# 会话内操作（前缀键默认 Ctrl+b）
# Ctrl+b d      detach（退出但保留会话）
# Ctrl+b $      重命名当前会话
# Ctrl+b s      切换会话（交互列表）

# 窗口（window）操作
# Ctrl+b c      新建窗口
# Ctrl+b ,      重命名窗口
# Ctrl+b n/p    下一个/上一个窗口
# Ctrl+b 0-9    切换到指定编号窗口
# Ctrl+b &      关闭当前窗口

# 面板（pane）操作
# Ctrl+b %      垂直分屏（左右）
# Ctrl+b "      水平分屏（上下）
# Ctrl+b o      切换到下一个面板
# Ctrl+b 方向键 移动到相邻面板
# Ctrl+b x      关闭当前面板
# Ctrl+b z      最大化/还原当前面板
# Ctrl+b Ctrl+方向键  调整面板大小

# 复制模式
# Ctrl+b [      进入复制模式（vi键）
# v             开始选择
# y             复制选中内容
# Ctrl+b ]      粘贴
```

---

## 五、systemd 服务管理

### 5.1 systemctl 常用命令

```bash
# 服务生命周期
systemctl start nginx
systemctl stop nginx
systemctl restart nginx
systemctl reload nginx          # 重载配置（不重启进程）
systemctl status nginx          # 查看状态

# 开机自启
systemctl enable nginx          # 启用开机自启
systemctl disable nginx         # 禁用开机自启
systemctl enable --now nginx    # 启用并立即启动
systemctl is-enabled nginx      # 查看是否开机自启

# 系统状态
systemctl list-units            # 列出所有运行中的 unit
systemctl list-units --all      # 包含未激活的
systemctl list-units --failed   # 只看失败的
systemctl list-unit-files       # 列出所有 unit 文件
systemctl daemon-reload         # 重新加载 unit 文件（修改配置后必须执行）

# 分析启动时间
systemd-analyze
systemd-analyze blame | head -20   # 各服务启动耗时排序
systemd-analyze critical-chain     # 关键路径分析
```

### 5.2 journalctl 查日志

```bash
journalctl -u nginx             # 查看 nginx 服务日志
journalctl -u nginx -f          # 实时追踪（类似 tail -f）
journalctl -u nginx --since "1 hour ago"
journalctl -u nginx --since "2025-12-01" --until "2025-12-09"
journalctl -u nginx -n 100      # 最新100行
journalctl -u nginx -p err      # 只看 error 及以上级别
journalctl -k                   # 内核日志（dmesg 替代）
journalctl -b                   # 本次启动的日志
journalctl -b -1                # 上次启动的日志
journalctl --disk-usage          # 日志占用磁盘
journalctl --vacuum-time=30d    # 清理30天前的日志
```

日志级别说明：

| 级别 | 数字 | 含义 |
|------|------|------|
| emerg | 0 | 系统不可用 |
| alert | 1 | 需要立即处理 |
| crit | 2 | 严重错误 |
| err | 3 | 错误 |
| warning | 4 | 警告 |
| notice | 5 | 重要通知 |
| info | 6 | 信息 |
| debug | 7 | 调试 |

### 5.3 service unit 文件结构

```ini
# /etc/systemd/system/myapp.service

[Unit]
Description=My Application Service
Documentation=https://example.com/docs
After=network.target network-online.target
Wants=network-online.target
# Requires= 强依赖，依赖失败则本服务也失败

[Service]
Type=simple                   # simple/forking/oneshot/notify/idle
User=appuser
Group=appgroup
WorkingDirectory=/opt/myapp
ExecStart=/opt/myapp/bin/myapp --config /etc/myapp/config.yaml
ExecStop=/bin/kill -SIGTERM $MAINPID
ExecReload=/bin/kill -SIGHUP $MAINPID
Restart=on-failure            # no/always/on-failure/on-abnormal
RestartSec=5s
StartLimitInterval=60s
StartLimitBurst=3

# 资源限制
LimitNOFILE=65536
LimitNPROC=4096
MemoryLimit=2G                # 超过会被 OOM kill
CPUQuota=200%                 # 最多使用2个核

# 安全加固
NoNewPrivileges=true
ProtectSystem=strict
PrivateTmp=true

# 环境变量
Environment=APP_ENV=production
EnvironmentFile=/etc/myapp/env

[Install]
WantedBy=multi-user.target
```

```bash
# 检查 unit 文件语法
systemd-analyze verify /etc/systemd/system/myapp.service

# 应用新的 unit 文件
systemctl daemon-reload
systemctl enable --now myapp
```

---

## 六、ulimit 资源限制

### 6.1 查看与设置

```bash
ulimit -a                       # 查看当前 shell 所有限制
ulimit -n                       # 查看最大文件描述符数
ulimit -u                       # 最大进程数（nproc）
ulimit -m                       # 最大内存（KB）
ulimit -s                       # 栈大小（KB）
ulimit -c                       # core dump 文件大小（0=禁止）

# 设置（只影响当前 shell 及子进程）
ulimit -n 65536                 # 设置文件描述符上限
ulimit -c unlimited             # 允许 core dump
ulimit -u 4096                  # 最大进程数

# 软限制和硬限制
ulimit -Sn 65536                # 设置软限制（进程可自行调高到硬限制）
ulimit -Hn                      # 查看硬限制
```

### 6.2 持久化配置

```bash
# /etc/security/limits.conf 或 /etc/security/limits.d/*.conf
# 格式：domain  type  item  value

cat >> /etc/security/limits.conf << 'EOF'
# 应用用户文件描述符限制
appuser   soft   nofile   65536
appuser   hard   nofile   65536
# 所有用户
*         soft   core     unlimited
*         hard   nproc    65536
# root 单独配置
root      soft   nofile   65536
root      hard   nofile   65536
EOF
```

常用 limits 条目：

| item | 含义 |
|------|------|
| nofile | 最大打开文件数（文件描述符）|
| nproc | 最大进程/线程数 |
| stack | 栈大小（KB）|
| core | core dump 文件大小（KB）|
| memlock | 可锁定内存大小（KB）|
| as | 虚拟地址空间大小（KB）|
| sigpending | 最大挂起信号数 |

### 6.3 查看进程实际限制

```bash
# 查看指定进程的资源限制
cat /proc/$(pgrep nginx | head -1)/limits

# 查看进程当前打开的文件描述符数
ls /proc/$(pgrep myapp | head -1)/fd | wc -l
# 或
cat /proc/sys/fs/file-nr          # 系统级：已用/空闲/最大 fd 数
```

---

## 七、进程跟踪与调试

```bash
# strace 跟踪系统调用
strace -p 1234                  # 跟踪已运行进程
strace -p 1234 -e trace=open,read,write  # 只看指定系统调用
strace -c command               # 统计各系统调用耗时
strace -T -p 1234               # 显示每个调用耗时

# ltrace 跟踪库函数调用
ltrace -p 1234

# 查看进程的文件描述符
ls -la /proc/1234/fd/

# 查看进程的内存映射
pmap -x 1234
cat /proc/1234/maps

# 查看进程的环境变量
cat /proc/1234/environ | tr '\0' '\n'

# 查看进程的命令行
cat /proc/1234/cmdline | tr '\0' ' '
```
