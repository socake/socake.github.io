---
title: "Shell 脚本实战：Bash 自动化运维从入门到工程化"
date: 2024-10-02T13:03:00+08:00
draft: false
tags: ["Shell", "Bash", "自动化", "Linux", "运维"]
categories: ["Linux"]
series: ["DevOps 工程师成长路径"]
description: "从 Bash 语法精要到工程化脚本设计，涵盖运维常用脚本模式、参数解析、信号处理、调试技巧与典型踩坑，帮你把一次性脚本做成可复用工具。"
summary: "Shell 脚本是 SRE 的第一生产力工具。本文从语法精要出发，覆盖批量操作、日志轮转、健康检查等常用运维模式，再到 getopts、trap 信号处理和脚本工程化思路，最后总结引号地狱、变量作用域等经典踩坑。"
toc: true
math: false
diagram: false
keywords: ["bash", "shell脚本", "自动化运维", "getopts", "trap", "脚本调试", "运维工程化"]
params:
  reading_time: true
---

入行运维第一年，我写的脚本基本是"能跑就行"——没有参数校验、没有错误处理、变量命名随意，三个月后自己都看不懂。后来经历了一次线上事故，一个没有 `set -e` 的脚本在中间步骤失败后继续执行，把错误数据写进了数据库，我才开始认真对待脚本工程化这件事。

这篇文章把我这几年积累的 Bash 实战经验系统化整理出来，从语法精要到工程化思路，尽量给出可以直接参考的代码。

## 语法精要：先把基础搞扎实

### 变量与字符串

Bash 的变量没有类型，所有值本质上都是字符串。几个容易搞混的点：

```bash
# 赋值：等号两边不能有空格
NAME="web-server"
PORT=8080

# 引用变量：推荐始终用双引号包裹
echo "$NAME"
echo "${NAME}-backup"  # 变量名边界不清晰时用花括号

# 字符串操作
FILE="/var/log/app/access.log"
echo "${FILE##*/}"    # 取文件名：access.log（从左贪心删到最后一个/）
echo "${FILE%/*}"     # 取目录名：/var/log/app（从右删到第一个/）
echo "${FILE%.log}"   # 去掉扩展名：/var/log/app/access
echo "${#FILE}"       # 字符串长度：22

# 默认值
DB_HOST="${DB_HOST:-localhost}"      # 未设置时用默认值
DB_PORT="${DB_PORT:=5432}"          # 未设置时赋值并返回
: "${REQUIRED_VAR:?'REQUIRED_VAR must be set'}"  # 未设置时报错退出
```

### 数组

```bash
# 普通数组
SERVERS=("web-01" "web-02" "web-03")
echo "${SERVERS[0]}"       # 第一个元素
echo "${SERVERS[@]}"       # 所有元素
echo "${#SERVERS[@]}"      # 元素数量
echo "${SERVERS[@]:1:2}"   # 切片（从索引1开始取2个）

# 遍历数组
for server in "${SERVERS[@]}"; do
    echo "Processing $server"
done

# 关联数组（需要 Bash 4+）
declare -A CONFIG
CONFIG["host"]="localhost"
CONFIG["port"]="5432"
echo "${CONFIG[host]}"
for key in "${!CONFIG[@]}"; do
    echo "$key = ${CONFIG[$key]}"
done
```

### 函数

函数是脚本复用的核心单元。几个关键点：函数内部变量用 `local` 声明避免污染全局作用域，通过 `return` 返回状态码（0=成功），通过 `echo` 返回数据。

```bash
# 函数定义与局部变量
check_port() {
    local host="${1:?'host required'}"
    local port="${2:?'port required'}"
    local timeout="${3:-3}"

    if nc -z -w "$timeout" "$host" "$port" 2>/dev/null; then
        return 0  # 成功
    else
        return 1  # 失败
    fi
}

# 函数返回数据
get_pod_count() {
    local namespace="${1:-default}"
    kubectl get pods -n "$namespace" --no-headers 2>/dev/null | wc -l | tr -d ' '
}

# 调用方式
if check_port "db.example.com" 5432; then
    echo "DB port is open"
fi

count=$(get_pod_count "production")
echo "Pod count: $count"
```

### 条件判断

```bash
# 文件/目录检查
[[ -f "/etc/config.yml" ]] && echo "file exists"
[[ -d "/var/log/app" ]] || mkdir -p "/var/log/app"
[[ -r "/etc/secret" ]] || { echo "no read permission"; exit 1; }

# 字符串比较（用 [[ ]] 而不是 [ ]，支持正则和更安全的语法）
[[ "$ENV" == "production" ]] && echo "prod mode"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || echo "invalid version format"
[[ -z "$VAR" ]] && echo "empty"   # 空字符串
[[ -n "$VAR" ]] && echo "not empty"  # 非空

# 数值比较
[[ $COUNT -gt 10 ]] && echo "too many"
(( COUNT > 10 )) && echo "too many"  # 算术上下文，更简洁

# 命令退出码
if kubectl get ns production &>/dev/null; then
    echo "namespace exists"
fi
```

## 安全脚本的四个开关

每个生产脚本第一行之后，我都会加这四个选项：

```bash
#!/usr/bin/env bash
set -euo pipefail

# -e: 任何命令返回非零退出码时立即退出
# -u: 引用未定义变量时报错（防止 $TYPO 静默变成空字符串）
# -o pipefail: 管道中任意命令失败时，整个管道返回失败
# 三者组合是最基础的安全网
```

`set -u` 是很多人忽略的选项，但它能防止非常隐蔽的 bug。假设你写了 `rm -rf "$BUILD_DIR/"` 但 `BUILD_DIR` 没有被设置，没有 `-u` 的情况下这条命令会变成 `rm -rf /`，后果可想而知。

## 常用运维脚本模式

### 模式一：批量远程操作

```bash
#!/usr/bin/env bash
set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly LOG_FILE="/tmp/batch-ops-$(date +%Y%m%d-%H%M%S).log"
readonly SSH_OPTS="-o ConnectTimeout=5 -o StrictHostKeyChecking=no -o BatchMode=yes"

# 从文件读取主机列表，忽略空行和注释
load_hosts() {
    local hosts_file="${1:?'hosts file required'}"
    [[ -f "$hosts_file" ]] || { echo "ERROR: $hosts_file not found"; exit 1; }
    grep -v '^\s*#' "$hosts_file" | grep -v '^\s*$'
}

# 带超时的远程执行，结果写入日志
remote_exec() {
    local host="$1"
    local cmd="$2"
    local user="${3:-ubuntu}"

    echo "[$(date '+%H:%M:%S')] Executing on $host" | tee -a "$LOG_FILE"
    if ssh $SSH_OPTS "${user}@${host}" "$cmd" >> "$LOG_FILE" 2>&1; then
        echo "[OK] $host" | tee -a "$LOG_FILE"
        return 0
    else
        echo "[FAIL] $host (exit code: $?)" | tee -a "$LOG_FILE"
        return 1
    fi
}

# 并行执行（控制并发数）
parallel_exec() {
    local hosts_file="$1"
    local cmd="$2"
    local max_parallel="${3:-5}"
    local failed=0

    while IFS= read -r host; do
        # 控制并发：当后台任务数达到上限时等待
        while [[ $(jobs -r | wc -l) -ge $max_parallel ]]; do
            sleep 0.5
        done
        remote_exec "$host" "$cmd" &
    done < <(load_hosts "$hosts_file")

    # 等待所有后台任务完成
    wait
    echo "Done. Log: $LOG_FILE"
}
```

### 模式二：日志轮转与清理

```bash
#!/usr/bin/env bash
set -euo pipefail

# 配置区（统一管理，方便修改）
readonly LOG_DIR="/var/log/myapp"
readonly MAX_DAYS=30
readonly MAX_SIZE_MB=500
readonly COMPRESS_AFTER_DAYS=3

cleanup_logs() {
    local log_dir="${1:-$LOG_DIR}"

    echo "=== Log cleanup started: $(date) ==="

    # 压缩超过N天的日志
    find "$log_dir" -name "*.log" -mtime +"$COMPRESS_AFTER_DAYS" ! -name "*.gz" | while read -r f; do
        gzip "$f" && echo "Compressed: $f"
    done

    # 删除超过保留期的日志
    local deleted
    deleted=$(find "$log_dir" -name "*.log.gz" -mtime +"$MAX_DAYS" -delete -print | wc -l)
    echo "Deleted $deleted old log files"

    # 检查目录总大小
    local dir_size_mb
    dir_size_mb=$(du -sm "$log_dir" | cut -f1)
    if [[ $dir_size_mb -gt $MAX_SIZE_MB ]]; then
        echo "WARNING: Log dir size ${dir_size_mb}MB exceeds limit ${MAX_SIZE_MB}MB"
        # 按时间排序，删除最旧的文件直到低于阈值
        find "$log_dir" -name "*.log.gz" -printf '%T+ %p\n' | sort | while read -r _ file; do
            [[ $(du -sm "$log_dir" | cut -f1) -le $MAX_SIZE_MB ]] && break
            rm "$file" && echo "Force deleted: $file"
        done
    fi

    echo "=== Cleanup done ==="
}
```

### 模式三：健康检查脚本

```bash
#!/usr/bin/env bash
set -euo pipefail

# 颜色输出（终端友好）
readonly RED='\033[0;31m'
readonly GREEN='\033[0;32m'
readonly YELLOW='\033[1;33m'
readonly NC='\033[0m'

FAILED_CHECKS=0

check_result() {
    local name="$1"
    local status="$2"  # 0=ok, 1=warn, 2=fail
    local message="$3"

    case $status in
        0) echo -e "[${GREEN}OK${NC}] $name: $message" ;;
        1) echo -e "[${YELLOW}WARN${NC}] $name: $message" ;;
        2) echo -e "[${RED}FAIL${NC}] $name: $message"; (( FAILED_CHECKS++ )) ;;
    esac
}

# 检查 HTTP 端点
check_http() {
    local name="$1"
    local url="$2"
    local expected_code="${3:-200}"

    local actual_code
    actual_code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "$url" 2>/dev/null || echo "000")

    if [[ "$actual_code" == "$expected_code" ]]; then
        check_result "$name" 0 "HTTP $actual_code"
    else
        check_result "$name" 2 "Expected HTTP $expected_code, got $actual_code"
    fi
}

# 检查磁盘使用率
check_disk() {
    local mount="${1:-/}"
    local warn_threshold="${2:-80}"
    local crit_threshold="${3:-90}"

    local usage
    usage=$(df "$mount" | awk 'NR==2 {print $5}' | tr -d '%')

    if [[ $usage -ge $crit_threshold ]]; then
        check_result "disk:$mount" 2 "${usage}% used (threshold: ${crit_threshold}%)"
    elif [[ $usage -ge $warn_threshold ]]; then
        check_result "disk:$mount" 1 "${usage}% used (threshold: ${warn_threshold}%)"
    else
        check_result "disk:$mount" 0 "${usage}% used"
    fi
}

# 检查进程是否运行
check_process() {
    local name="$1"
    local pattern="$2"

    if pgrep -f "$pattern" > /dev/null 2>&1; then
        local count
        count=$(pgrep -f "$pattern" | wc -l)
        check_result "process:$name" 0 "$count process(es) running"
    else
        check_result "process:$name" 2 "not running"
    fi
}

run_all_checks() {
    echo "=== Health Check: $(date) ==="
    check_http "api-health" "http://localhost:8080/health"
    check_http "metrics" "http://localhost:9090/-/healthy"
    check_disk "/" 80 90
    check_disk "/var" 85 95
    check_process "nginx" "nginx: master"
    check_process "app" "myapp-server"

    echo ""
    if [[ $FAILED_CHECKS -gt 0 ]]; then
        echo -e "${RED}${FAILED_CHECKS} check(s) FAILED${NC}"
        exit 1
    else
        echo -e "${GREEN}All checks passed${NC}"
    fi
}

run_all_checks
```

## getopts：规范的参数解析

脚本参数多了之后，`$1 $2 $3` 这种写法就不够用了。`getopts` 是 Bash 内建的参数解析工具，比手动 `case` 更规范：

```bash
#!/usr/bin/env bash
set -euo pipefail

# 默认值
ENVIRONMENT="staging"
DRY_RUN=false
VERBOSE=false
OUTPUT_FILE=""

usage() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS] <service-name>

Options:
  -e ENV       Target environment (default: staging)
  -o FILE      Output file path
  -n           Dry run mode (no actual changes)
  -v           Verbose output
  -h           Show this help

Examples:
  $(basename "$0") -e production -v myservice
  $(basename "$0") -n -o /tmp/report.txt myservice
EOF
    exit "${1:-0}"
}

# getopts: 冒号表示该选项需要参数，开头冒号表示静默错误处理
while getopts ":e:o:nvh" opt; do
    case $opt in
        e) ENVIRONMENT="$OPTARG" ;;
        o) OUTPUT_FILE="$OPTARG" ;;
        n) DRY_RUN=true ;;
        v) VERBOSE=true ;;
        h) usage 0 ;;
        :) echo "ERROR: -$OPTARG requires an argument"; usage 1 ;;
        \?) echo "ERROR: Unknown option -$OPTARG"; usage 1 ;;
    esac
done

# 移除已解析的选项，$@ 剩余为位置参数
shift $((OPTIND - 1))

# 校验必须的位置参数
[[ $# -lt 1 ]] && { echo "ERROR: service name required"; usage 1; }
SERVICE_NAME="$1"

# 参数校验
[[ "$ENVIRONMENT" =~ ^(staging|production|qa)$ ]] || {
    echo "ERROR: invalid environment: $ENVIRONMENT"
    exit 1
}

$VERBOSE && echo "DEBUG: env=$ENVIRONMENT service=$SERVICE_NAME dry_run=$DRY_RUN"
```

## trap：信号处理与清理

`trap` 让脚本在退出或收到信号时执行清理代码，是编写健壮脚本的关键：

```bash
#!/usr/bin/env bash
set -euo pipefail

# 临时文件/目录统一在这里管理
TEMP_DIR=""
LOCK_FILE="/tmp/my-script.lock"

cleanup() {
    local exit_code=$?

    # 清理临时目录
    [[ -n "$TEMP_DIR" && -d "$TEMP_DIR" ]] && rm -rf "$TEMP_DIR"

    # 释放锁文件
    [[ -f "$LOCK_FILE" ]] && rm -f "$LOCK_FILE"

    if [[ $exit_code -ne 0 ]]; then
        echo "Script failed with exit code $exit_code" >&2
    fi

    exit $exit_code
}

# EXIT：任何退出（正常/异常）都会触发
# INT：Ctrl+C
# TERM：kill 命令
trap cleanup EXIT INT TERM

# 防止脚本重复运行（文件锁）
if [[ -f "$LOCK_FILE" ]]; then
    local pid
    pid=$(cat "$LOCK_FILE")
    if kill -0 "$pid" 2>/dev/null; then
        echo "ERROR: Script already running (PID: $pid)"
        exit 1
    fi
fi
echo $$ > "$LOCK_FILE"

# 创建临时目录
TEMP_DIR=$(mktemp -d)
echo "Working in $TEMP_DIR"

# 主逻辑...
# 即使中间 exit 或者 Ctrl+C，trap 也会确保清理执行
```

更高级的用法——在长时间操作中捕获中断信号，做优雅退出：

```bash
INTERRUPTED=false

handle_interrupt() {
    echo ""
    echo "Interrupted! Finishing current task before exit..."
    INTERRUPTED=true
}
trap handle_interrupt INT

for item in "${ITEMS[@]}"; do
    $INTERRUPTED && { echo "Stopped by user"; break; }
    process_item "$item"
done
```

## 脚本调试技巧

```bash
# 方法一：启动时开启 xtrace
bash -x myscript.sh

# 方法二：在脚本内部局部开启（调试特定区块）
set -x
some_complex_function
set +x

# 方法三：只做语法检查，不执行
bash -n myscript.sh

# 方法四：打印每行但不展开变量（用于调试引号问题）
set -v

# 方法五：查看脚本某个位置的变量状态
debug_vars() {
    echo "=== DEBUG at line ${BASH_LINENO[0]} ===" >&2
    local var
    for var in "$@"; do
        echo "  $var=${!var}" >&2
    done
}
# 使用：debug_vars HOST PORT USER
```

## 脚本工程化：从一次性脚本到可复用工具

运维脚本写多了，你会发现很多逻辑是重复的：日志函数、错误处理、配置加载。把这些抽成库文件，各脚本通过 `source` 引入：

```bash
# lib/logging.sh
readonly LOG_LEVEL_DEBUG=0
readonly LOG_LEVEL_INFO=1
readonly LOG_LEVEL_WARN=2
readonly LOG_LEVEL_ERROR=3

CURRENT_LOG_LEVEL=${LOG_LEVEL:-$LOG_LEVEL_INFO}

_log() {
    local level="$1"
    local level_name="$2"
    local message="$3"
    [[ $level -ge $CURRENT_LOG_LEVEL ]] || return 0
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [$level_name] $message" >&2
}

log_debug() { _log $LOG_LEVEL_DEBUG "DEBUG" "$*"; }
log_info()  { _log $LOG_LEVEL_INFO  "INFO"  "$*"; }
log_warn()  { _log $LOG_LEVEL_WARN  "WARN"  "$*"; }
log_error() { _log $LOG_LEVEL_ERROR "ERROR" "$*"; }
```

```bash
# lib/retry.sh
retry() {
    local max_attempts="${1:?}"
    local delay="${2:?}"
    shift 2
    local cmd=("$@")
    local attempt=1

    while [[ $attempt -le $max_attempts ]]; do
        if "${cmd[@]}"; then
            return 0
        fi
        log_warn "Attempt $attempt/$max_attempts failed. Retrying in ${delay}s..."
        sleep "$delay"
        (( attempt++ ))
    done

    log_error "All $max_attempts attempts failed for: ${cmd[*]}"
    return 1
}

# 使用：retry 3 5 curl -f http://api.example.com/health
```

目录结构参考：

```
ops-scripts/
├── bin/              # 可执行脚本（符号链接或直接放这里）
│   ├── deploy.sh
│   └── health-check.sh
├── lib/              # 公共库
│   ├── logging.sh
│   ├── retry.sh
│   └── aws.sh
├── conf/             # 配置文件
│   └── environments.sh
└── tests/            # 测试（用 bats 框架）
    └── test_logging.bats
```

## 经典踩坑

### 踩坑一：引号地狱

```bash
FILE="my file with spaces.txt"

# 错误：文件名中的空格会被解释为参数分隔符
ls $FILE       # ls: my: No such file or directory
rm $FILE       # 删除了三个不存在的文件

# 正确：始终双引号包裹变量
ls "$FILE"
rm "$FILE"

# 更复杂的情况：数组传递给命令
FILES=("file one.txt" "file two.txt")
ls "${FILES[@]}"   # 正确：每个元素作为独立参数
ls ${FILES[@]}     # 错误：空格被当作分隔符
```

### 踩坑二：变量作用域

```bash
# 陷阱：管道在子 shell 中执行，变量修改对父 shell 不可见
COUNT=0
cat file.txt | while read -r line; do
    (( COUNT++ ))
done
echo "$COUNT"  # 输出 0！不是预期的行数

# 解法一：用进程替换代替管道
while read -r line; do
    (( COUNT++ ))
done < <(cat file.txt)
echo "$COUNT"  # 正确

# 解法二：lastpipe（Bash 4.2+，让管道最后一段在当前 shell 执行）
shopt -s lastpipe
cat file.txt | while read -r line; do
    (( COUNT++ ))
done
```

### 踩坑三：exit code 被吞

```bash
# set -e 下，某些写法会意外吞掉非零 exit code

# 错误：赋值语句的退出码是 0（赋值本身成功），不是命令的退出码
RESULT=$(failing_command)  # 即使 failing_command 失败，set -e 也不会退出

# 正确写法：先执行，再赋值
failing_command
RESULT=$?

# 或者：把赋值和检查分开
RESULT=$(failing_command) || { echo "Command failed"; exit 1; }
```

### 踩坑四：`[ ]` vs `[[ ]]`

```bash
# [ ] 是 POSIX 标准，在老脚本和 /bin/sh 中使用
# [[ ]] 是 Bash 扩展，功能更强、更安全

# [[ ]] 支持正则匹配
[[ "$version" =~ ^v[0-9]+ ]]

# [[ ]] 中变量不会被分词（不需要引号）
[[ $file == *.log ]]  # 不需要 "$file"

# [[ ]] 中 && 和 || 更符合直觉
[[ -f "$f" && -r "$f" ]]  # 不需要写 [ -f "$f" ] && [ -r "$f" ]

# 结论：写 Bash 脚本用 [[ ]]，写 POSIX sh 用 [ ]
```

## 总结

Shell 脚本工程化的核心是：**把脚本当代码写**。具体体现在：

1. 安全四件套（`set -euo pipefail`）每个脚本必加
2. 函数内部用 `local` 声明变量，避免全局污染
3. 用 `trap` 管理清理逻辑，保证无论如何退出都不留烂摊子
4. 用 `getopts` 处理参数，提供 `-h` 帮助信息
5. 公共逻辑抽成库文件，通过 `source` 复用
6. 变量始终双引号包裹，尤其是文件路径和用户输入

脚本质量反映了工程师对系统的理解深度。一个写了 `set -u` 的脚本，证明作者知道未定义变量的危险；一个有完善 `trap` 的脚本，证明作者考虑过各种异常退出场景。这些细节累积起来，就是生产环境的稳定性。
