---
title: "Python 系统与文件操作实战"
date: 2025-12-09T10:00:00+08:00
draft: false
tags: ["Python", "编程", "运维"]
categories: ["Python"]
description: "Python 运维向系统操作实战：os/pathlib 路径管理、subprocess 命令执行、shutil 文件操作、psutil 进程管理，附完整日志清理脚本"
summary: "深入讲解 Python 系统操作，含 subprocess 进程管理、psutil 系统监控，以及一个完整的生产级日志清理脚本"
toc: true
math: false
diagram: false
keywords: ["Python", "subprocess", "psutil", "pathlib", "运维", "日志清理"]
params:
  reading_time: true
---

## os 与 pathlib 路径操作

pathlib 是 Python 3.4+ 引入的面向对象路径库，比 `os.path` 更直观，优先使用。

```python
from pathlib import Path
import os

# ===== 路径构造 =====
home = Path.home()                          # /home/ubuntu
cwd = Path.cwd()                            # 当前工作目录
log_dir = Path("/var/log/myapp")
config_file = home / ".config" / "app.yaml"

# 字符串转 Path
p = Path("/etc/nginx/nginx.conf")

# ===== 路径信息 =====
print(p.name)        # nginx.conf
print(p.stem)        # nginx
print(p.suffix)      # .conf
print(p.suffixes)    # ['.conf']
print(p.parent)      # /etc/nginx
print(p.parts)       # ('/', 'etc', 'nginx', 'nginx.conf')
print(p.root)        # /

# 文件状态
print(p.exists())
print(p.is_file())
print(p.is_dir())
print(p.is_symlink())

stat = p.stat()
print(stat.st_size)                         # 字节大小
print(stat.st_mtime)                        # 修改时间戳

# ===== 目录操作 =====
new_dir = Path("/tmp/myapp/logs/2025")
new_dir.mkdir(parents=True, exist_ok=True)   # 递归创建

# 遍历目录（一层）
for item in Path("/var/log").iterdir():
    if item.is_file():
        print(f"文件: {item.name}  大小: {item.stat().st_size}")

# glob 匹配
for log in Path("/var/log").glob("*.log"):
    print(log)

# rglob 递归匹配
for conf in Path("/etc").rglob("*.conf"):
    print(conf)

# ===== 文件操作 =====
src = Path("/tmp/source.txt")
dst = Path("/tmp/dest.txt")

src.write_text("hello world\n", encoding="utf-8")
content = src.read_text(encoding="utf-8")
data = src.read_bytes()                     # 二进制读

dst.write_text(content)
src.rename(Path("/tmp/renamed.txt"))        # 移动/重命名
src.unlink(missing_ok=True)                 # 删除（不存在时不报错）

# ===== os 模块补充 =====
# 环境变量
print(os.environ.get("HOME", "/root"))
os.environ["MY_VAR"] = "value"

# 进程信息
print(os.getpid())
print(os.getppid())
print(os.getuid())

# 目录操作
os.makedirs("/tmp/a/b/c", exist_ok=True)
os.chdir("/tmp")                            # 切换工作目录
print(os.listdir("/etc"))                   # 列出目录

# 文件权限
os.chmod("/tmp/script.sh", 0o755)

# 路径操作（兼容老代码时使用 os.path）
import os.path
print(os.path.abspath("../etc"))
print(os.path.expanduser("~/logs"))
print(os.path.join("/var", "log", "app"))
```

## subprocess 执行外部命令

```python
import subprocess
from subprocess import run, PIPE, STDOUT, CalledProcessError, Popen

# ===== subprocess.run（推荐，简单场景）=====

# 执行并获取输出
result = run(
    ["df", "-h", "/"],
    capture_output=True,
    text=True,
    timeout=10,
)
print(result.stdout)
print(result.returncode)   # 0=成功

# 失败时抛出异常
try:
    run(["ls", "/nonexistent"], check=True, capture_output=True, text=True)
except CalledProcessError as e:
    print(f"命令失败，退出码 {e.returncode}")
    print(f"stderr: {e.stderr}")

# shell=True（注意注入风险，参数不要来自用户输入）
result = run("ps aux | grep nginx | grep -v grep", shell=True, capture_output=True, text=True)

# 带环境变量
import os
env = os.environ.copy()
env["KUBECONFIG"] = "/root/.kube/config"
result = run(["kubectl", "get", "nodes"], capture_output=True, text=True, env=env)

# ===== 管道链 =====
def pipe_commands(cmds: list[list[str]]) -> str:
    """执行管道命令链，返回最终输出。"""
    procs = []
    prev_stdout = None

    for i, cmd in enumerate(cmds):
        stdin = prev_stdout
        stdout = PIPE
        p = Popen(cmd, stdin=stdin, stdout=stdout, stderr=PIPE)
        if prev_stdout:
            prev_stdout.close()  # 让上一个进程收到 SIGPIPE
        prev_stdout = p.stdout
        procs.append(p)

    output, _ = procs[-1].communicate()
    for p in procs[:-1]:
        p.wait()

    return output.decode()


# 等价于：ps aux | grep nginx | awk '{print $2}'
pids = pipe_commands([
    ["ps", "aux"],
    ["grep", "nginx"],
    ["awk", "{print $2}"],
])

# ===== Popen（长时间运行/实时输出）=====
def run_with_realtime_output(cmd: list[str]) -> int:
    """执行命令并实时打印输出，返回退出码。"""
    with Popen(cmd, stdout=PIPE, stderr=STDOUT, text=True, bufsize=1) as proc:
        for line in proc.stdout:
            print(line, end="")
    return proc.returncode


# ===== 封装工具函数 =====
def shell(cmd: str | list, timeout: int = 30, check: bool = True) -> str:
    """
    执行命令，返回 stdout。失败时抛 RuntimeError。

    Args:
        cmd: 命令字符串（shell=True）或列表（shell=False）
        timeout: 超时秒数
        check: 非零退出码是否抛异常
    """
    shell_mode = isinstance(cmd, str)
    try:
        r = run(
            cmd,
            shell=shell_mode,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=check,
        )
        return r.stdout.strip()
    except CalledProcessError as e:
        raise RuntimeError(
            f"命令失败 (exit={e.returncode}):\n"
            f"  cmd: {cmd}\n"
            f"  stderr: {e.stderr.strip()}"
        ) from e
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"命令超时 ({timeout}s): {cmd}")


# 使用
hostname = shell("hostname")
disk_info = shell(["df", "-h", "/"])
```

## shutil 文件操作

```python
import shutil
from pathlib import Path

# ===== 复制 =====
shutil.copy("/etc/nginx/nginx.conf", "/tmp/nginx.conf.bak")        # 复制文件（不含元数据）
shutil.copy2("/etc/nginx/nginx.conf", "/tmp/nginx.conf.bak")       # 复制文件（含时间戳等元数据）
shutil.copytree("/etc/nginx", "/tmp/nginx-backup")                  # 递归复制目录
shutil.copytree("/etc/nginx", "/tmp/nginx-backup2", dirs_exist_ok=True)  # 目标存在也继续

# ===== 移动 =====
shutil.move("/tmp/nginx.conf.bak", "/backup/nginx.conf")

# ===== 删除 =====
shutil.rmtree("/tmp/old-dir", ignore_errors=True)   # 递归删除目录

# ===== 压缩打包 =====
# 打包为 tar.gz
archive_path = shutil.make_archive(
    base_name="/tmp/backup-2025-12-09",   # 输出文件名（不含扩展名）
    format="gztar",                        # 格式：zip/tar/gztar/bztar/xztar
    root_dir="/var/log/myapp",             # 被打包的根目录
    base_dir=".",                          # 打包该目录下的内容
)
print(f"归档: {archive_path}")

# 解压
shutil.unpack_archive("/tmp/backup-2025-12-09.tar.gz", "/tmp/restored")

# ===== 磁盘使用 =====
usage = shutil.disk_usage("/")
print(f"总计: {usage.total / 1024**3:.1f} GB")
print(f"已用: {usage.used / 1024**3:.1f} GB")
print(f"空闲: {usage.free / 1024**3:.1f} GB")
print(f"使用率: {usage.used / usage.total * 100:.1f}%")

# ===== 查找可执行文件 =====
kubectl = shutil.which("kubectl")
if kubectl:
    print(f"kubectl 位于: {kubectl}")
else:
    print("kubectl 未安装")
```

## 环境变量与 dotenv

```python
import os
from pathlib import Path

# 直接读取
db_host = os.environ["DB_HOST"]                    # 不存在则 KeyError
db_port = os.environ.get("DB_PORT", "5432")        # 带默认值
debug = os.environ.get("DEBUG", "false").lower() == "true"

# 设置（仅影响当前进程及子进程）
os.environ["APP_LOG_LEVEL"] = "INFO"

# ===== python-dotenv =====
# pip install python-dotenv
from dotenv import load_dotenv

# 加载 .env 文件（默认不覆盖已有环境变量）
load_dotenv()
load_dotenv("/etc/myapp/.env")              # 指定路径
load_dotenv(override=True)                  # 覆盖模式

# .env 文件格式：
# DB_HOST=10.0.2.10
# DB_PORT=5432
# APP_SECRET=mysecret

# 手动解析 .env（不依赖第三方库）
def load_env_file(path: str) -> dict[str, str]:
    env: dict[str, str] = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, _, val = line.partition("=")
                    env[key.strip()] = val.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return env
```

## psutil 进程与系统监控

```python
# pip install psutil
import psutil
import os
from datetime import datetime

# ===== CPU =====
print(f"CPU 核数（逻辑）: {psutil.cpu_count()}")
print(f"CPU 核数（物理）: {psutil.cpu_count(logical=False)}")
print(f"CPU 使用率: {psutil.cpu_percent(interval=1):.1f}%")   # interval=1 等1秒后采样
cpu_per_core = psutil.cpu_percent(interval=1, percpu=True)     # 每核使用率

# ===== 内存 =====
mem = psutil.virtual_memory()
print(f"总内存: {mem.total / 1024**3:.1f} GB")
print(f"已用:   {mem.used / 1024**3:.1f} GB ({mem.percent:.1f}%)")
print(f"可用:   {mem.available / 1024**3:.1f} GB")

swap = psutil.swap_memory()
print(f"Swap: {swap.used / 1024**2:.0f}MB / {swap.total / 1024**2:.0f}MB")

# ===== 磁盘 =====
for partition in psutil.disk_partitions():
    try:
        usage = psutil.disk_usage(partition.mountpoint)
        print(f"{partition.mountpoint}: {usage.percent:.1f}% 已用")
    except PermissionError:
        pass

disk_io = psutil.disk_io_counters()
print(f"读: {disk_io.read_bytes / 1024**2:.0f} MB, 写: {disk_io.write_bytes / 1024**2:.0f} MB")

# ===== 网络 =====
net_io = psutil.net_io_counters()
print(f"发送: {net_io.bytes_sent / 1024**2:.0f} MB")
print(f"接收: {net_io.bytes_recv / 1024**2:.0f} MB")

for conn in psutil.net_connections(kind="tcp"):
    if conn.status == "LISTEN":
        print(f"监听端口: {conn.laddr.port}")

# ===== 进程管理 =====
# 列出所有进程
for proc in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
    try:
        if proc.info["memory_percent"] > 10.0:
            print(f"PID {proc.info['pid']} {proc.info['name']}: "
                  f"内存 {proc.info['memory_percent']:.1f}%")
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass

# 查找特定进程
def find_process(name: str) -> list[psutil.Process]:
    result = []
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if name.lower() in proc.info["name"].lower():
                result.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return result

nginx_procs = find_process("nginx")

# 杀进程
def kill_process(pid: int, force: bool = False) -> bool:
    try:
        proc = psutil.Process(pid)
        if force:
            proc.kill()    # SIGKILL
        else:
            proc.terminate()  # SIGTERM
        proc.wait(timeout=5)
        return True
    except (psutil.NoSuchProcess, psutil.TimeoutExpired) as e:
        print(f"终止进程失败: {e}")
        return False

# 获取进程详情
try:
    p = psutil.Process(os.getpid())
    print(f"当前进程: PID={p.pid}")
    print(f"  名称:   {p.name()}")
    print(f"  CMD:    {' '.join(p.cmdline())}")
    print(f"  内存:   {p.memory_info().rss / 1024**2:.1f} MB")
    print(f"  CPU:    {p.cpu_percent(interval=0.1):.1f}%")
    print(f"  启动:   {datetime.fromtimestamp(p.create_time())}")
    print(f"  文件数: {len(p.open_files())}")
except psutil.AccessDenied:
    pass
```

## 实战：批量检查文件分布 + 清理过期日志

以下是一个完整的生产级脚本，功能：
1. 扫描多个目录，统计日志文件分布
2. 找出超过保留天数的旧日志
3. 可选执行删除（dry-run 模式默认开启）
4. 汇总报告打印到终端

```python
#!/usr/bin/env python3
"""
log_cleaner.py — 日志清理工具

用法:
    python log_cleaner.py --dirs /var/log/nginx /var/log/myapp --days 30
    python log_cleaner.py --dirs /var/log/nginx --days 7 --execute
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

# ── 日志配置 ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ── 数据类 ────────────────────────────────────────────────────────────────────
@dataclass
class FileRecord:
    path: Path
    size_bytes: int
    mtime: datetime
    age_days: float


@dataclass
class ScanResult:
    directory: Path
    total_files: int = 0
    total_size: int = 0
    expired_files: list[FileRecord] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ── 核心逻辑 ──────────────────────────────────────────────────────────────────
def scan_directory(
    directory: Path,
    patterns: list[str],
    max_age_days: int,
    now: datetime,
) -> ScanResult:
    """扫描目录，返回文件统计与过期文件列表。"""
    result = ScanResult(directory=directory)
    cutoff = now - timedelta(days=max_age_days)

    if not directory.exists():
        result.errors.append(f"目录不存在: {directory}")
        return result

    if not directory.is_dir():
        result.errors.append(f"不是目录: {directory}")
        return result

    for pattern in patterns:
        for path in directory.rglob(pattern):
            if not path.is_file():
                continue
            try:
                stat = path.stat()
                mtime = datetime.fromtimestamp(stat.st_mtime)
                age = (now - mtime).total_seconds() / 86400

                result.total_files += 1
                result.total_size += stat.st_size

                if mtime < cutoff:
                    result.expired_files.append(
                        FileRecord(
                            path=path,
                            size_bytes=stat.st_size,
                            mtime=mtime,
                            age_days=age,
                        )
                    )
            except (OSError, PermissionError) as e:
                result.errors.append(f"无法读取 {path}: {e}")

    # 按修改时间排序（最旧的在前）
    result.expired_files.sort(key=lambda r: r.mtime)
    return result


def format_size(n: int) -> str:
    """将字节数格式化为人类可读形式。"""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def delete_files(files: list[FileRecord], dry_run: bool) -> tuple[int, int]:
    """
    删除文件列表，返回 (成功数, 失败数)。
    dry_run=True 时只打印不删除。
    """
    success = 0
    failure = 0
    for rec in files:
        if dry_run:
            logger.info(f"  [DRY] 跳过删除: {rec.path}  ({format_size(rec.size_bytes)}, {rec.age_days:.1f}天)")
            success += 1
            continue
        try:
            rec.path.unlink()
            logger.info(f"  已删除: {rec.path}  ({format_size(rec.size_bytes)})")
            success += 1
        except OSError as e:
            logger.error(f"  删除失败: {rec.path}: {e}")
            failure += 1
    return success, failure


def print_report(results: list[ScanResult], dry_run: bool) -> None:
    """打印汇总报告。"""
    total_files = sum(r.total_files for r in results)
    total_size = sum(r.total_size for r in results)
    total_expired = sum(len(r.expired_files) for r in results)
    total_expired_size = sum(
        sum(f.size_bytes for f in r.expired_files) for r in results
    )

    print("\n" + "=" * 60)
    print("  日志清理报告")
    print("=" * 60)
    print(f"  扫描目录数:     {len(results)}")
    print(f"  总文件数:       {total_files:,}")
    print(f"  总占用空间:     {format_size(total_size)}")
    print(f"  过期文件数:     {total_expired:,}")
    print(f"  过期文件大小:   {format_size(total_expired_size)}")
    print(f"  模式:           {'DRY RUN（未实际删除）' if dry_run else '执行删除'}")
    print("=" * 60)

    for r in results:
        if r.errors:
            for err in r.errors:
                print(f"  [WARN] {err}")

    print()


# ── 入口 ──────────────────────────────────────────────────────────────────────
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="扫描并清理过期日志文件",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dirs",
        nargs="+",
        required=True,
        metavar="DIR",
        help="要扫描的目录列表",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="保留最近 N 天的文件（默认 30）",
    )
    parser.add_argument(
        "--patterns",
        nargs="+",
        default=["*.log", "*.log.*", "*.gz"],
        help="匹配的文件 glob 模式（默认 *.log *.log.* *.gz）",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        default=False,
        help="真正执行删除（默认 dry-run）",
    )
    parser.add_argument(
        "--min-size",
        type=int,
        default=0,
        metavar="BYTES",
        help="只处理大于该字节数的文件（默认 0，即全部）",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    dry_run = not args.execute
    now = datetime.now()

    if dry_run:
        logger.info("运行模式: DRY RUN（使用 --execute 参数执行真实删除）")

    results: list[ScanResult] = []
    total_deleted = 0
    total_failed = 0

    for dir_str in args.dirs:
        directory = Path(dir_str)
        logger.info(f"扫描: {directory}  (保留 {args.days} 天内的文件)")

        result = scan_directory(directory, args.patterns, args.days, now)
        results.append(result)

        logger.info(
            f"  发现 {result.total_files} 个文件，"
            f"共 {format_size(result.total_size)}，"
            f"其中过期 {len(result.expired_files)} 个"
        )

        # 过滤最小文件大小
        candidates = [
            f for f in result.expired_files if f.size_bytes >= args.min_size
        ]

        if candidates:
            ok, fail = delete_files(candidates, dry_run)
            total_deleted += ok
            total_failed += fail
        else:
            logger.info(f"  无需清理")

    print_report(results, dry_run)

    if total_failed > 0:
        logger.error(f"共 {total_failed} 个文件删除失败，请检查权限")
        return 1

    logger.info(f"完成：{'模拟处理' if dry_run else '已删除'} {total_deleted} 个文件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

### 运行示例

```bash
# 扫描两个目录，保留30天内的日志，dry-run
python log_cleaner.py --dirs /var/log/nginx /var/log/myapp --days 30

# 实际执行删除，只处理超过 1MB 的文件
python log_cleaner.py --dirs /var/log/nginx --days 7 --min-size 1048576 --execute
```
