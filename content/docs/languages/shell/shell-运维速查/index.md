---
title: "Shell 脚本运维速查手册"
date: 2025-12-08T10:00:00+08:00
draft: false
tags: ["Shell", "Bash", "运维", "脚本", "Linux"]
categories: ["Shell"]
description: "运维常用 Shell 脚本技巧速查：文本处理、进程管理、网络排查、批量操作模板"
summary: "Shell 运维速查手册，包含文本处理（awk/sed/grep）、进程排查、网络诊断、批量操作模板，以及实用的脚本编写规范。"
toc: true
math: false
diagram: false
keywords: ["shell", "bash", "awk", "sed", "grep", "运维脚本"]
params:
  reading_time: true
---

## 脚本规范头

每个生产用途的脚本都应从标准头部开始，这三行能在脚本出问题时救你一命：

```bash
#!/bin/bash
set -euo pipefail

# set -e  : 任意命令非零退出时立即终止
# set -u  : 引用未声明变量时报错退出（而非静默展开为空）
# set -o pipefail : 管道中任意命令失败则整个管道返回失败状态
```

标准脚本结构模板：

```bash
#!/bin/bash
set -euo pipefail

# ============================================================
# 脚本名称：deploy.sh
# 功能描述：应用部署脚本
# 作者：ops-team
# 创建时间：2025-12-08
# 使用方式：./deploy.sh <env> <version>
# ============================================================

# ---- 全局常量 ----
readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_NAME="$(basename "$0")"
readonly LOG_FILE="/var/log/${SCRIPT_NAME%.sh}.log"
readonly LOCK_FILE="/tmp/${SCRIPT_NAME%.sh}.lock"

# ---- 颜色定义 ----
readonly RED='\033[0;31m'
readonly YELLOW='\033[1;33m'
readonly GREEN='\033[0;32m'
readonly NC='\033[0m'  # No Color

# ---- 函数定义区 ----
log_info()  { echo -e "$(date '+%Y-%m-%d %H:%M:%S') ${GREEN}[INFO]${NC}  $*" | tee -a "$LOG_FILE"; }
log_warn()  { echo -e "$(date '+%Y-%m-%d %H:%M:%S') ${YELLOW}[WARN]${NC}  $*" | tee -a "$LOG_FILE"; }
log_error() { echo -e "$(date '+%Y-%m-%d %H:%M:%S') ${RED}[ERROR]${NC} $*" | tee -a "$LOG_FILE" >&2; }

usage() {
    cat <<EOF
用法: $SCRIPT_NAME <env> <version>
  env     : 部署环境 (qa|pre|prod)
  version : 镜像版本号
示例:
  $SCRIPT_NAME qa v1.2.3
EOF
    exit 1
}

# ---- 参数校验 ----
[[ $# -lt 2 ]] && usage
ENV="$1"
VERSION="$2"

# ---- 主逻辑 ----
main() {
    log_info "开始部署 env=$ENV version=$VERSION"
    # ... 业务逻辑
    log_info "部署完成"
}

main "$@"
```

---

## 文本处理三剑客

### grep

```bash
# 基本选项速查
grep -v "pattern" file       # 反向匹配（不含 pattern 的行）
grep -r "pattern" ./dir/     # 递归搜索目录
grep -l "pattern" *.log      # 只输出匹配的文件名（不输出内容）
grep -i "pattern" file       # 忽略大小写
grep -E "pat1|pat2" file     # 扩展正则（等价于 egrep）
grep -c "pattern" file       # 统计匹配行数
grep -n "pattern" file       # 显示行号
grep -A 3 "pattern" file     # 匹配行及后 3 行（After）
grep -B 3 "pattern" file     # 匹配行及前 3 行（Before）
grep -C 3 "pattern" file     # 匹配行及前后各 3 行（Context）

# 实用场景
# 1. 从日志里找 ERROR，排除健康检查噪声
grep -E "ERROR|FATAL" app.log | grep -v "health_check"

# 2. 查找包含 IP 地址的行
grep -E "\b([0-9]{1,3}\.){3}[0-9]{1,3}\b" access.log

# 3. 统计每种 HTTP 状态码出现次数
grep -oE "HTTP/[0-9.]+ [0-9]+" access.log | sort | uniq -c | sort -rn

# 4. 找出所有含敏感词的配置文件（不进入 .git 目录）
grep -r --include="*.yaml" --include="*.conf" \
    -l "password\|secret\|token" . \
    --exclude-dir=.git

# 5. 实时跟踪日志中的错误
tail -f /var/log/app.log | grep --line-buffered "ERROR"
```

### awk

awk 把每行按分隔符切成字段，`$1` 是第一列，`$NF` 是最后一列，`NR` 是当前行号。

```bash
# 字段提取
awk '{print $1, $3}' file              # 打印第 1、3 列
awk -F: '{print $1}' /etc/passwd       # 以 : 为分隔符，打印用户名
awk -F, '{print $2}' data.csv          # CSV 第 2 列
awk '{print NR, $0}' file              # 给每行加行号
awk 'NR==5,NR==10 {print}' file        # 打印第 5 到第 10 行

# 条件过滤
awk '$3 > 100 {print $1, $3}' file     # 第 3 列大于 100 才打印
awk '/ERROR/ {print $0}' app.log       # 包含 ERROR 的行
awk '!/DEBUG/ {print}' app.log         # 不含 DEBUG 的行
awk 'NF > 3 {print}' file              # 只处理字段数大于 3 的行

# 统计与聚合
# 对第 2 列求和
awk '{sum += $2} END {print "total:", sum}' data.txt

# 统计各值出现频次（模拟 sort | uniq -c）
awk '{count[$1]++} END {for (k in count) print count[k], k}' file | sort -rn

# 计算平均响应时间（日志第 5 列是耗时 ms）
awk '{sum+=$5; n++} END {printf "avg: %.2f ms\n", sum/n}' access.log

# 实用场景
# 1. 从 ps 输出中找内存占用 >1% 的进程
ps aux | awk '$4 > 1.0 {print $1, $2, $4, $11}'

# 2. 打印 nginx access.log 中响应时间超过 1 秒的请求
awk '$NF > 1.0 {print $7, $NF}' /var/log/nginx/access.log | sort -k2 -rn | head -20

# 3. 从 /proc/net/dev 提取网卡流量
awk 'NR>2 {printf "%-10s RX: %s MB  TX: %s MB\n", $1, $2/1024/1024, $10/1024/1024}' \
    /proc/net/dev

# 4. 多文件统计（自动带文件名）
awk '{print FILENAME, NR, $0}' *.log | grep "ERROR"
```

### sed

```bash
# 基本替换语法：sed 's/old/new/flags'
sed 's/foo/bar/'          file   # 每行第一个 foo 替换为 bar
sed 's/foo/bar/g'         file   # 全局替换
sed 's/foo/bar/gi'        file   # 全局替换，忽略大小写
sed -i 's/foo/bar/g'      file   # 原地修改文件（慎用）
sed -i.bak 's/foo/bar/g'  file   # 原地修改，备份为 .bak

# 行操作
sed -n '5,10p' file              # 打印第 5 到 10 行（-n 抑制默认输出）
sed '3d' file                    # 删除第 3 行
sed '/^#/d' file                 # 删除注释行（以 # 开头）
sed '/^$/d' file                 # 删除空行
sed -n '/START/,/END/p' file     # 打印 START 到 END 之间的行

# 在指定行前/后插入
sed '3i\新增这行内容' file        # 在第 3 行前插入
sed '3a\新增这行内容' file        # 在第 3 行后追加
sed '/pattern/a\追加内容' file    # 在匹配行后追加

# 多文件批量处理
# 批量替换当前目录下所有 yaml 文件的镜像 tag
find . -name "*.yaml" -exec \
    sed -i "s|image: myapp:.*|image: myapp:v2.1.0|g" {} \;

# 批量去掉 Windows 换行符 \r
find . -name "*.sh" -exec sed -i 's/\r$//' {} \;

# 实用场景
# 1. 提取配置文件中某 section 的内容
sed -n '/\[database\]/,/\[/p' config.ini | sed '$d'

# 2. 在文件头部插入一行（用于批量添加版权注释）
sed -i '1i# Copyright 2025 MyCompany. All rights reserved.' *.py

# 3. 删除日志中的 ANSI 颜色转义码
sed 's/\x1b\[[0-9;]*m//g' colored.log > clean.log
```

---

## 进程与系统排查

### 进程查看

```bash
# ps 常用组合
ps aux                            # 查看所有进程（BSD 风格）
ps aux | grep java | grep -v grep # 找 Java 进程
ps aux --sort=-%mem | head -15    # 按内存降序排前 15
ps aux --sort=-%cpu | head -15    # 按 CPU 降序排前 15
ps -ef --forest                   # 树形显示进程父子关系

# 查看某 PID 的详细信息
ps -p 1234 -o pid,ppid,cmd,%cpu,%mem,etime,user

# 查看进程打开的文件描述符数量（排查 fd 泄漏）
ls /proc/1234/fd | wc -l
cat /proc/1234/limits | grep "open files"

# 查看进程的线程数
ps -p 1234 -o nlwp
cat /proc/1234/status | grep Threads

# top 快捷键（交互式）
# P  : 按 CPU 排序
# M  : 按内存排序
# k  : kill 进程
# 1  : 展开所有 CPU 核
# q  : 退出
```

### 端口与网络连接

```bash
# 查找端口占用（推荐用 ss，比 netstat 快）
ss -tlnp                          # 查看所有监听的 TCP 端口
ss -tlnp | grep :8080             # 查看 8080 端口
ss -tunlp                         # 同时显示 TCP/UDP
ss -s                             # 连接状态统计汇总

# netstat（老机器没有 ss 时用）
netstat -tlnp                     # 监听端口
netstat -an | grep ESTABLISHED | wc -l   # 当前建立连接数
netstat -an | grep TIME_WAIT | wc -l     # TIME_WAIT 连接数

# 通过端口反查进程
lsof -i :8080
fuser 8080/tcp

# 查看连接数最多的 IP（排查 DDoS 或异常客户端）
ss -tn state established | awk '{print $5}' | cut -d: -f1 | sort | uniq -c | sort -rn | head -20
```

### 查找大文件与磁盘

```bash
# 磁盘使用概览
df -hT                            # 显示所有分区（含文件系统类型）
df -ih                            # 查看 inode 使用情况

# 找出占用空间最大的目录（排查磁盘满）
du -sh /*  2>/dev/null | sort -rh | head -10
du -sh /var/log/* | sort -rh | head -10
du --max-depth=2 /data | sort -rn | head -20

# 找大文件
find / -type f -size +100M -exec ls -lh {} \; 2>/dev/null | sort -k5 -rh | head -20
find /var/log -name "*.log" -size +50M -mtime +7  # 7 天前且 >50M 的日志

# 查找并删除超过 30 天的旧日志（先 dry-run 看看）
find /var/log/app -name "*.log.gz" -mtime +30 -print          # 先列出
find /var/log/app -name "*.log.gz" -mtime +30 -delete         # 确认后再删

# 快速找出哪些文件最近被修改
find /etc -newer /etc/passwd -type f 2>/dev/null
```

### 内存排查

```bash
# 内存概览
free -h                           # 人类可读格式
free -h -s 2                      # 每 2 秒刷新一次

# 详细内存信息
cat /proc/meminfo | grep -E "MemTotal|MemFree|MemAvailable|Cached|Buffers|SwapTotal|SwapFree"

# 按内存使用量排序进程（单位 KB）
ps aux --sort=-%mem | awk 'NR<=10 {printf "%-8s %-6s %s\n", $4, $6/1024"MB", $11}'

# 查看某进程的内存映射
cat /proc/1234/status | grep -E "VmRSS|VmSwap|VmSize"

# OOM 事件排查
dmesg | grep -i "oom\|killed process" | tail -20
grep -i "out of memory\|oom" /var/log/messages | tail -20

# Swap 使用情况（按进程）
for pid in $(ls /proc | grep -E '^[0-9]+$'); do
    swap=$(grep -s VmSwap /proc/$pid/status | awk '{print $2}')
    [[ -n "$swap" && "$swap" -gt 0 ]] && \
        echo "$swap KB  PID:$pid  $(cat /proc/$pid/cmdline 2>/dev/null | tr '\0' ' ' | cut -c1-60)"
done | sort -rn | head -10
```

---

## 网络排查

### curl 调试

```bash
# 基本 HTTP 调试
curl -v https://api.example.com/health          # 详细请求/响应头
curl -I https://api.example.com/health          # 只看响应头（HEAD 请求）
curl -s -o /dev/null -w "%{http_code}" URL      # 只看状态码

# 超时控制
curl --connect-timeout 5 --max-time 30 URL      # 连接超时5s，总超时30s

# 带认证
curl -H "Authorization: Bearer $TOKEN" URL
curl -u username:password URL

# POST JSON
curl -X POST \
     -H "Content-Type: application/json" \
     -d '{"key":"value"}' \
     https://api.example.com/endpoint

# 测量响应时间（生产排查利器）
curl -s -o /dev/null -w "
  DNS解析:        %{time_namelookup}s
  TCP建连:        %{time_connect}s
  TLS握手:        %{time_appconnect}s
  首字节时间:     %{time_starttransfer}s
  总耗时:         %{time_total}s
  下载大小:       %{size_download} bytes
  HTTP状态码:     %{http_code}
" https://api.example.com/

# 走代理
curl -x http://proxy.internal:3128 https://external-api.com/

# 跟随重定向，限制最多 5 次
curl -L --max-redirs 5 https://example.com/
```

### 端口连通性测试

```bash
# nc (netcat) 测试 TCP 连通性
nc -zv database.internal 5432       # -z 不发数据，-v 详细输出
nc -zv -w 3 redis.internal 6379     # 3 秒超时
nc -zu dns.internal 53              # UDP 测试

# 批量测试多个端口
for port in 80 443 8080 9090; do
    nc -zv -w 2 api.example.com $port 2>&1 | grep -E "succeeded|refused|timeout"
done

# 测试 MySQL 连通性（不依赖 mysql 客户端）
nc -zv db.internal 3306 && echo "MySQL 端口通" || echo "MySQL 端口不通"

# 简单 HTTP 服务器（测试用，监听 8888 端口）
nc -lk 8888
```

### tcpdump 抓包

```bash
# 基本用法
tcpdump -i eth0                              # 抓 eth0 接口
tcpdump -i any                               # 抓所有接口
tcpdump -i eth0 -n                           # -n 不解析主机名（更快）
tcpdump -i eth0 -nn                          # 同时不解析端口名

# 过滤条件
tcpdump -i eth0 port 8080                    # 只抓 8080 端口
tcpdump -i eth0 host 10.0.1.50              # 抓特定主机的流量
tcpdump -i eth0 src 10.0.1.50              # 只看来源
tcpdump -i eth0 dst 10.0.1.50              # 只看目的

# 组合过滤
tcpdump -i eth0 'host 10.0.1.50 and port 5432'      # 特定主机的 PG 流量
tcpdump -i eth0 'port 80 or port 443'               # HTTP/HTTPS
tcpdump -i eth0 'tcp[tcpflags] & tcp-syn != 0'      # 只看 SYN 包（新连接）

# 保存到文件，之后用 Wireshark 分析
tcpdump -i eth0 -w capture.pcap port 8080
tcpdump -r capture.pcap -nn | head -50              # 读取 pcap 文件

# 抓包并显示内容（HTTP 文本协议调试）
tcpdump -i eth0 -A -s 0 port 8080 | grep -A 10 "HTTP"
```

### DNS 排查

```bash
# dig 基本查询
dig api.example.com                          # 查 A 记录
dig api.example.com A                        # 明确指定类型
dig api.example.com MX                       # 查邮件记录
dig api.example.com CNAME                    # 查 CNAME
dig -x 1.2.3.4                              # 反向查询（PTR）

# 指定 DNS 服务器查询
dig @8.8.8.8 api.example.com               # 用 Google DNS 查
dig @10.0.0.2 api.example.com             # 用内部 DNS 查（排查解析差异）

# 查看完整解析链
dig +trace api.example.com                  # 从根服务器追踪

# 查询耗时（排查 DNS 慢）
dig api.example.com | grep "Query time"

# 简洁输出只看 IP
dig +short api.example.com
dig +short -x 1.2.3.4

# nslookup（简单场景）
nslookup api.example.com
nslookup api.example.com 8.8.8.8           # 指定 DNS
```

---

## 批量操作模板

### for 循环批量操作

```bash
# 批量 SSH 到多台服务器执行命令
SERVERS=(10.0.1.10 10.0.1.11 10.0.1.12 10.0.1.13)
for host in "${SERVERS[@]}"; do
    echo "=== $host ==="
    ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no \
        "ops@$host" "uptime && df -h / | tail -1"
done

# 批量检查服务健康状态
SERVICES=(api gateway worker scheduler)
for svc in "${SERVICES[@]}"; do
    status=$(systemctl is-active "$svc" 2>/dev/null || echo "not-found")
    printf "%-15s %s\n" "$svc" "$status"
done

# 批量处理文件（重命名，添加前缀）
for f in *.log; do
    mv "$f" "backup_$(date +%Y%m%d)_$f"
done

# 带序号的批量操作
for i in {1..5}; do
    echo "处理第 $i 项..."
    sleep 0.5
done
```

### while read 处理文件列表

```bash
# 从文件读取服务器列表，一行一个
while IFS= read -r host; do
    [[ -z "$host" || "$host" == \#* ]] && continue   # 跳过空行和注释
    echo "正在处理: $host"
    ssh "ops@$host" "hostname && free -h" 2>&1 || echo "[$host] 连接失败"
done < servers.txt

# 处理 CSV（第一列是主机，第二列是端口）
while IFS=',' read -r host port service; do
    result=$(nc -zv -w 2 "$host" "$port" 2>&1)
    if echo "$result" | grep -q "succeeded"; then
        echo "[OK] $service ($host:$port)"
    else
        echo "[FAIL] $service ($host:$port)"
    fi
done < endpoints.csv

# 处理带空格的文件路径
find /data/uploads -name "*.tmp" | while IFS= read -r file; do
    echo "删除: $file"
    rm -f "$file"
done
```

### xargs 并发执行

```bash
# 串行 xargs（默认）
cat servers.txt | xargs -I {} ssh ops@{} "uptime"

# 并发执行（-P 指定并发数）
cat servers.txt | xargs -P 10 -I {} ssh -o ConnectTimeout=5 ops@{} "df -h /"

# 并发 ping 检测存活
cat servers.txt | xargs -P 20 -I {} sh -c \
    'ping -c 1 -W 1 {} >/dev/null 2>&1 && echo "UP: {}" || echo "DOWN: {}"'

# 并发下载文件
cat urls.txt | xargs -P 5 -I {} wget -q -P /data/downloads {}

# 分批处理（-n 每次传几个参数）
echo "a b c d e f" | xargs -n 2 echo   # 每批 2 个
```

### 带重试的命令执行函数

```bash
# 重试函数：最多重试 N 次，失败则报错退出
retry() {
    local max_attempts="${1:-3}"
    local delay="${2:-5}"
    local cmd=("${@:3}")
    local attempt=1

    while true; do
        if "${cmd[@]}"; then
            return 0
        fi
        if [[ $attempt -ge $max_attempts ]]; then
            log_error "命令失败，已重试 $max_attempts 次: ${cmd[*]}"
            return 1
        fi
        log_warn "第 $attempt 次失败，${delay}s 后重试... (${cmd[*]})"
        sleep "$delay"
        ((attempt++))
    done
}

# 使用示例
retry 3 5 curl -sf https://api.example.com/health
retry 5 10 kubectl rollout status deployment/myapp -n production
```

---

## 实用函数库模板

以下是一套可直接 source 到脚本中的工具函数库，保存为 `lib.sh`：

```bash
#!/bin/bash
# lib.sh - 运维脚本公共函数库

# ============================================================
# 日志函数
# ============================================================
readonly _LOG_FILE="${LOG_FILE:-/tmp/script.log}"
readonly _RED='\033[0;31m'
readonly _YELLOW='\033[1;33m'
readonly _GREEN='\033[0;32m'
readonly _BLUE='\033[0;34m'
readonly _NC='\033[0m'

log_info()  { echo -e "$(date '+%Y-%m-%d %H:%M:%S') ${_GREEN}[INFO]${_NC}  $*" | tee -a "$_LOG_FILE"; }
log_warn()  { echo -e "$(date '+%Y-%m-%d %H:%M:%S') ${_YELLOW}[WARN]${_NC}  $*" | tee -a "$_LOG_FILE"; }
log_error() { echo -e "$(date '+%Y-%m-%d %H:%M:%S') ${_RED}[ERROR]${_NC} $*" | tee -a "$_LOG_FILE" >&2; }
log_debug() {
    [[ "${DEBUG:-0}" == "1" ]] && \
        echo -e "$(date '+%Y-%m-%d %H:%M:%S') ${_BLUE}[DEBUG]${_NC} $*" | tee -a "$_LOG_FILE"
}

# ============================================================
# 依赖检查
# ============================================================
check_command() {
    local missing=()
    for cmd in "$@"; do
        command -v "$cmd" &>/dev/null || missing+=("$cmd")
    done
    if [[ ${#missing[@]} -gt 0 ]]; then
        log_error "缺少必要命令: ${missing[*]}"
        log_error "请先安装: apt-get install ${missing[*]}  或  yum install ${missing[*]}"
        return 1
    fi
}

# 使用：check_command curl jq kubectl awscli

# ============================================================
# 重试函数
# ============================================================
retry() {
    local max="${1:-3}"
    local delay="${2:-5}"
    local cmd=("${@:3}")
    local i=1
    until "${cmd[@]}"; do
        [[ $i -ge $max ]] && { log_error "重试 $max 次仍失败: ${cmd[*]}"; return 1; }
        log_warn "第 $i 次失败，${delay}s 后重试 (最多 $max 次)..."
        sleep "$delay"
        ((i++))
    done
}

# ============================================================
# 锁文件防重入
# ============================================================
readonly _LOCK_FILE="${LOCK_FILE:-/tmp/$(basename "$0" .sh).lock}"

acquire_lock() {
    if [[ -f "$_LOCK_FILE" ]]; then
        local old_pid
        old_pid=$(cat "$_LOCK_FILE")
        if kill -0 "$old_pid" 2>/dev/null; then
            log_error "脚本已在运行 (PID: $old_pid)，退出"
            exit 1
        else
            log_warn "发现残留锁文件 (PID: $old_pid 已退出)，清理后继续"
            rm -f "$_LOCK_FILE"
        fi
    fi
    echo $$ > "$_LOCK_FILE"
    trap 'release_lock' EXIT INT TERM
}

release_lock() {
    rm -f "$_LOCK_FILE"
    log_info "锁已释放"
}

# ============================================================
# 确认提示（重要操作前使用）
# ============================================================
confirm() {
    local prompt="${1:-确认执行此操作？}"
    echo -e "${_YELLOW}[确认] $prompt (输入 yes 继续)${_NC}"
    read -r answer
    [[ "$answer" == "yes" ]] || { log_warn "已取消"; return 1; }
}

# ============================================================
# 超时执行
# ============================================================
run_with_timeout() {
    local timeout="$1"
    shift
    timeout "$timeout" "$@"
    local exit_code=$?
    [[ $exit_code -eq 124 ]] && { log_error "命令超时 (${timeout}s): $*"; return 1; }
    return $exit_code
}

# ============================================================
# 在脚本中使用（source 方式）
# ============================================================
# source /path/to/lib.sh
# check_command curl jq kubectl
# acquire_lock
# retry 3 5 curl -sf https://api.example.com/health
```

---

## 常用 one-liner 合集

```bash
# ============================================================
# 系统信息
# ============================================================
# 查看系统负载与 CPU 核数
echo "Load: $(cat /proc/loadavg | cut -d' ' -f1-3)  CPU cores: $(nproc)"

# 查看系统运行时间
uptime -p

# 查看最近 10 条系统日志
journalctl -n 10 --no-pager

# ============================================================
# 进程与端口
# ============================================================
# 找出监听端口对应的进程名
ss -tlnp | awk 'NR>1 {print $4, $6}' | sed 's/.*,//' | sed 's/"//'

# 杀掉所有匹配的进程（谨慎使用）
pkill -f "python app.py"

# 找出僵尸进程
ps aux | awk '$8=="Z" {print $2, $11}'

# ============================================================
# 文件与文本
# ============================================================
# 统计文件行数、字数、字节数
wc -lwc filename.txt

# 去除重复行（保留顺序）
awk '!seen[$0]++' file.txt

# 随机打乱文件行顺序
shuf file.txt

# 比较两个文件差异（仅看不同的行）
diff <(sort file1.txt) <(sort file2.txt)

# 合并多个 CSV（去掉第 2 个文件起的头行）
awk 'FNR==1 && NR!=1 {next} {print}' *.csv > merged.csv

# 找出两个文件共有的行
comm -12 <(sort file1) <(sort file2)

# ============================================================
# 网络
# ============================================================
# 查看公网 IP
curl -s ifconfig.me

# 测试 DNS 解析速度（查询 10 次取平均）
for i in {1..10}; do dig +stats api.example.com 2>&1; done | \
    grep "Query time" | awk '{sum+=$4; n++} END {print "avg:", sum/n, "ms"}'

# 查看当前机器的所有网卡 IP
ip -4 addr show | grep -oP '(?<=inet\s)\d+(\.\d+){3}'

# 监控某端口的实时连接数
watch -n 1 'ss -tn state established "( dport = :8080 or sport = :8080 )" | wc -l'

# ============================================================
# 日志处理
# ============================================================
# 统计 nginx 日志中访问量最多的前 10 个 URL
awk '{print $7}' /var/log/nginx/access.log | sort | uniq -c | sort -rn | head -10

# 统计每分钟请求量（日志时间戳格式为 HH:MM:SS）
awk '{print substr($4,2,17)}' access.log | cut -d: -f1-3 | \
    uniq -c | awk '{print $2, $1}' | tail -20

# 提取 JSON 日志中的 error message（jq 可用时推荐用 jq）
grep "ERROR" app.log | python3 -c "import sys,json; [print(json.loads(l).get('msg','')) for l in sys.stdin]"

# ============================================================
# 磁盘与内存
# ============================================================
# 实时监控磁盘 IO
iostat -xz 2 5

# 查看 inode 使用率（inode 满也会导致无法创建文件）
df -i | awk '$5+0 > 80 {print}'

# 找出 24 小时内被修改过的文件（排查变更）
find /etc /opt /usr/local -newer /tmp/.baseline -type f 2>/dev/null

# 清理 journal 日志（释放磁盘）
journalctl --vacuum-size=500M
journalctl --vacuum-time=30d
```
