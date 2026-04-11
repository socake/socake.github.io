---
title: "Python 基础速查（运维向）"
date: 2025-12-09T10:00:00+08:00
draft: false
tags: ["Python", "编程", "运维"]
categories: ["Python"]
description: "面向运维工程师的 Python 基础速查手册，涵盖变量类型、字符串处理、控制流、函数、异常处理、文件操作及标准库速览"
summary: "运维工程师必备的 Python 基础知识速查，从变量类型到标准库，聚焦实际使用场景"
toc: true
math: false
diagram: false
keywords: ["Python", "运维", "速查", "基础", "标准库"]
params:
  reading_time: true
---

## 变量与类型

Python 是动态类型语言，变量不需要声明类型。运维脚本中最常用的类型：

```python
# 基本类型
hostname = "web-01.prod"          # str
port = 8080                        # int
threshold = 0.85                   # float
is_healthy = True                  # bool
pid = None                         # NoneType

# 类型转换
port_str = str(port)               # "8080"
port_num = int("9090")             # 9090
ratio = float("0.75")              # 0.75

# 查看类型
print(type(hostname))              # <class 'str'>
print(isinstance(port, int))       # True

# 多重赋值
host, port, proto = "localhost", 3306, "tcp"
a = b = c = 0                      # 全部赋值为 0
```

## 字符串格式化

f-string 是最推荐的方式，Python 3.6+：

```python
host = "db-primary"
port = 5432
latency_ms = 12.456

# f-string（推荐）
msg = f"连接 {host}:{port}，延迟 {latency_ms:.1f}ms"
# 连接 db-primary:5432，延迟 12.5ms

# 格式控制
print(f"{host:>20}")               # 右对齐，宽度20
print(f"{port:05d}")               # 补零：05432
print(f"{latency_ms:.2f}")        # 保留2位小数：12.46
print(f"{1024 * 1024:,}")         # 千分位：1,048,576

# 多行 f-string
report = (
    f"Host  : {host}\n"
    f"Port  : {port}\n"
    f"Status: {'UP' if latency_ms < 100 else 'SLOW'}"
)

# 常用字符串方法
url = "  https://API.Example.COM/v1/health  "
print(url.strip())                 # 去首尾空白
print(url.lower())                 # 转小写
print(url.upper())                 # 转大写
print(url.replace("https", "http"))
print(url.split("/"))              # 按分隔符切分
print(",".join(["a", "b", "c"]))  # 拼接
print(url.startswith("  https"))   # True
print(url.endswith("health  "))    # True
print("API" in url)                # True

# 字符串解析
line = "2025-12-09 10:23:45 ERROR web-01 connection refused"
parts = line.split(maxsplit=4)     # 最多切4刀
date, time, level, host, msg = parts
```

## 列表、字典、集合

```python
# ===== 列表 =====
servers = ["web-01", "web-02", "db-01"]

servers.append("cache-01")         # 追加
servers.insert(0, "lb-01")         # 指定位置插入
servers.remove("db-01")            # 删除指定值
popped = servers.pop()             # 弹出末尾
servers.sort()                     # 原地排序
sorted_srv = sorted(servers)       # 返回新列表
servers.reverse()                  # 原地翻转
print(len(servers))                # 长度
print("web-01" in servers)        # 成员判断

# 切片
first_two = servers[:2]
last_one = servers[-1:]
reversed_list = servers[::-1]      # 翻转副本

# ===== 字典 =====
server_info = {
    "host": "web-01",
    "ip": "10.0.1.10",
    "port": 80,
    "tags": ["nginx", "prod"],
}

# 读取
host = server_info["host"]
port = server_info.get("port", 80)           # 带默认值
region = server_info.get("region", "unknown")

# 修改
server_info["status"] = "healthy"
server_info.update({"version": "1.2.3", "weight": 100})

# 遍历
for key, val in server_info.items():
    print(f"  {key}: {val}")

keys = list(server_info.keys())
vals = list(server_info.values())

# 删除
del server_info["weight"]
popped_val = server_info.pop("version", None)

# 嵌套字典
inventory = {
    "web-01": {"ip": "10.0.1.10", "cpu": 4, "mem_gb": 8},
    "web-02": {"ip": "10.0.1.11", "cpu": 4, "mem_gb": 16},
}
print(inventory["web-01"]["ip"])

# ===== 集合 =====
healthy = {"web-01", "web-02", "db-01"}
degraded = {"web-02", "cache-01"}

print(healthy & degraded)          # 交集：{'web-02'}
print(healthy | degraded)          # 并集
print(healthy - degraded)          # 差集（只在healthy中）
print(healthy ^ degraded)          # 对称差集

# 去重
hosts_with_dup = ["web-01", "web-02", "web-01", "db-01"]
unique_hosts = list(set(hosts_with_dup))
```

## 控制流与推导式

```python
# ===== 条件 =====
status_code = 503

if status_code == 200:
    print("OK")
elif status_code in (301, 302):
    print("重定向")
elif 500 <= status_code < 600:
    print(f"服务端错误: {status_code}")
else:
    print("未知状态")

# 三元表达式
label = "健康" if status_code == 200 else "异常"

# ===== 循环 =====
servers = ["web-01", "web-02", "db-01"]

for srv in servers:
    print(srv)

for i, srv in enumerate(servers, start=1):
    print(f"{i}. {srv}")

# zip 同步迭代
ips = ["10.0.1.10", "10.0.1.11", "10.0.2.10"]
for srv, ip in zip(servers, ips):
    print(f"{srv} -> {ip}")

# while
retries = 0
max_retries = 3
while retries < max_retries:
    # ... 尝试连接 ...
    retries += 1

# break / continue
for srv in servers:
    if srv.startswith("db"):
        continue                   # 跳过数据库节点
    if srv == "web-02":
        break                      # 提前退出

# ===== 推导式 =====
# 列表推导式
web_servers = [s for s in servers if s.startswith("web")]

# 字典推导式
port_map = {srv: 80 for srv in servers}
upper_map = {k: v.upper() for k, v in {"a": "x", "b": "y"}.items()}

# 集合推导式
prefixes = {srv.split("-")[0] for srv in servers}  # {'web', 'db'}

# 生成器表达式（不构建列表，节省内存）
total = sum(len(s) for s in servers)
any_down = any(s.endswith("-bad") for s in servers)
all_prod = all("prod" in s for s in servers)
```

## 函数

```python
from typing import Optional, List, Dict, Any

# 基础函数
def check_port(host: str, port: int) -> bool:
    """检查端口是否可达。"""
    import socket
    try:
        with socket.create_connection((host, port), timeout=3):
            return True
    except (socket.timeout, ConnectionRefusedError, OSError):
        return False


# 默认参数
def retry(func, max_attempts: int = 3, delay: float = 1.0):
    import time
    for attempt in range(1, max_attempts + 1):
        try:
            return func()
        except Exception as e:
            if attempt == max_attempts:
                raise
            time.sleep(delay)


# *args 和 **kwargs
def log_event(level: str, *messages: str, **context: Any) -> None:
    """
    log_event("INFO", "服务启动", "监听端口", host="web-01", port=8080)
    """
    msg = " ".join(messages)
    ctx = " ".join(f"{k}={v}" for k, v in context.items())
    print(f"[{level}] {msg} | {ctx}")


# 仅关键字参数（* 后面的参数必须用关键字传递）
def deploy(service: str, *, version: str, env: str = "prod") -> None:
    print(f"部署 {service} v{version} 到 {env}")


deploy("api", version="1.2.3")           # 正确
# deploy("api", "1.2.3")                 # 报错

# lambda（适合简单的单行函数）
servers = [
    {"name": "web-02", "weight": 50},
    {"name": "web-01", "weight": 100},
]
servers.sort(key=lambda s: s["weight"], reverse=True)

# 返回多个值（实际是元组）
def parse_endpoint(endpoint: str) -> tuple[str, int]:
    host, port_str = endpoint.rsplit(":", 1)
    return host, int(port_str)

host, port = parse_endpoint("10.0.1.10:8080")
```

## 异常处理

```python
import socket
import json
from pathlib import Path

# 基本结构
def load_config(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"配置文件不存在: {path}")
        return {}
    except json.JSONDecodeError as e:
        print(f"JSON 解析失败: {e}")
        return {}
    except PermissionError:
        print(f"无权限读取: {path}")
        raise                          # 重新抛出
    finally:
        print("load_config 执行完毕")  # 无论如何都执行


# 捕获多个异常
def connect(host: str, port: int) -> socket.socket:
    try:
        s = socket.create_connection((host, port), timeout=5)
        return s
    except (socket.timeout, TimeoutError):
        raise RuntimeError(f"连接 {host}:{port} 超时")
    except ConnectionRefusedError:
        raise RuntimeError(f"{host}:{port} 拒绝连接")
    except OSError as e:
        raise RuntimeError(f"网络错误: {e}") from e


# 自定义异常
class DeployError(Exception):
    def __init__(self, service: str, reason: str):
        self.service = service
        self.reason = reason
        super().__init__(f"部署失败 [{service}]: {reason}")


class RollbackError(DeployError):
    pass


def deploy_service(service: str, version: str) -> None:
    if not version.startswith("v"):
        raise DeployError(service, f"版本格式错误: {version}")
    # ... 部署逻辑 ...


# 使用 contextlib.suppress 忽略特定异常
from contextlib import suppress

with suppress(FileNotFoundError):
    Path("/tmp/old-lock").unlink()     # 文件不存在时静默跳过
```

## 文件操作

```python
from pathlib import Path
import json
import yaml  # pip install pyyaml

# ===== pathlib（推荐）=====
p = Path("/var/log/nginx")

# 路径构造
log_file = p / "access.log"
config = Path.home() / ".config" / "myapp" / "config.yaml"

# 路径信息
print(log_file.name)               # "access.log"
print(log_file.stem)               # "access"
print(log_file.suffix)             # ".log"
print(log_file.parent)             # PosixPath('/var/log/nginx')
print(log_file.exists())
print(log_file.is_file())
print(log_file.is_dir())
size = log_file.stat().st_size     # 文件大小（字节）

# 创建目录
config.parent.mkdir(parents=True, exist_ok=True)

# 读写文件
text = log_file.read_text(encoding="utf-8")
log_file.write_text("内容", encoding="utf-8")

# 遍历目录
log_dir = Path("/var/log")
for f in log_dir.iterdir():
    print(f)

# glob 匹配
for log in log_dir.glob("*.log"):
    print(log)
for log in log_dir.rglob("error*.log"):   # 递归
    print(log)

# ===== with open（传统写法）=====
# 读文本
with open("/etc/hosts") as f:
    lines = f.readlines()          # 全部行列表
    # 或者逐行迭代（大文件推荐）
    # for line in f: ...

# 写文本
with open("/tmp/report.txt", "w", encoding="utf-8") as f:
    f.write("第一行\n")
    f.writelines(["line2\n", "line3\n"])

# 追加
with open("/tmp/report.txt", "a") as f:
    f.write("追加内容\n")

# ===== JSON =====
data = {"host": "web-01", "port": 80}

# 写
with open("/tmp/data.json", "w") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)

# 读
with open("/tmp/data.json") as f:
    loaded = json.load(f)

# 字符串互转
json_str = json.dumps(data, ensure_ascii=False)
parsed = json.loads(json_str)

# ===== YAML =====
# 读
with open("config.yaml") as f:
    cfg = yaml.safe_load(f)         # 用 safe_load，不用 load

# 写
with open("output.yaml", "w") as f:
    yaml.dump(data, f, allow_unicode=True, default_flow_style=False)
```

## 类型注解基础

Python 3.9+ 内置泛型，3.10+ 支持 `X | Y` 联合类型：

```python
from typing import Optional, Union, Callable
from collections.abc import Iterator, Generator

# 基本注解
def ping(host: str, count: int = 4) -> bool: ...

# 容器类型（Python 3.9+）
def filter_servers(servers: list[str], tag: str) -> list[str]: ...
def get_inventory() -> dict[str, dict]: ...

# 可选参数（Python 3.10+ 用 X | None）
def connect(host: str, proxy: str | None = None) -> None: ...
# 等价于：
def connect2(host: str, proxy: Optional[str] = None) -> None: ...

# Union（Python 3.10+ 用 X | Y）
def parse_port(val: str | int) -> int:
    return int(val)

# 可调用类型
def run_with_retry(fn: Callable[[], bool], retries: int) -> bool:
    for _ in range(retries):
        if fn():
            return True
    return False

# TypedDict（结构化字典）
from typing import TypedDict

class ServerInfo(TypedDict):
    host: str
    port: int
    healthy: bool

def check(info: ServerInfo) -> str:
    return f"{info['host']}:{info['port']}"
```

## 模块与包

```python
# 导入方式
import os
import os.path
from pathlib import Path
from subprocess import run, PIPE, CalledProcessError
from typing import Optional
import json as j                          # 别名

# 条件导入（兼容）
try:
    import ujson as json                  # 更快的JSON库
except ImportError:
    import json

# __name__ 判断（脚本模式）
if __name__ == "__main__":
    main()

# 相对导入（包内部使用）
# from .utils import parse_config
# from ..common import logger
```

## 运维工程师必知标准库速览

| 库 | 主要用途 | 常用入口 |
|---|---|---|
| `os` | 环境变量、进程、路径操作 | `os.environ`, `os.getpid()`, `os.getcwd()` |
| `sys` | 解释器参数、退出、stdin/stdout | `sys.argv`, `sys.exit()`, `sys.path` |
| `subprocess` | 执行外部命令 | `subprocess.run()`, `Popen` |
| `pathlib` | 路径操作（推荐替代 os.path） | `Path(...)`, `glob()`, `rglob()` |
| `shutil` | 文件/目录复制、移动、打包 | `shutil.copy2()`, `copytree()`, `make_archive()` |
| `json` | JSON 序列化/反序列化 | `json.load()`, `json.dump()` |
| `yaml` | YAML 解析（第三方 PyYAML） | `yaml.safe_load()`, `yaml.dump()` |
| `re` | 正则表达式 | `re.search()`, `re.findall()`, `re.sub()` |
| `logging` | 结构化日志 | `logging.getLogger()`, `basicConfig()` |
| `argparse` | 命令行参数解析 | `ArgumentParser`, `add_argument()` |
| `datetime` | 日期时间处理 | `datetime.now()`, `timedelta`, `strftime()` |
| `time` | 时间戳、sleep | `time.time()`, `time.sleep()` |
| `socket` | 网络连接、端口检测 | `socket.create_connection()` |
| `threading` | 线程 | `Thread`, `Lock`, `Event` |
| `concurrent.futures` | 线程/进程池 | `ThreadPoolExecutor`, `as_completed()` |
| `hashlib` | MD5/SHA 哈希 | `hashlib.md5()`, `sha256()` |
| `base64` | Base64 编解码 | `base64.b64encode()`, `b64decode()` |
| `gzip` / `tarfile` | 压缩/解压 | `gzip.open()`, `tarfile.open()` |
| `tempfile` | 临时文件/目录 | `tempfile.mkdtemp()`, `NamedTemporaryFile` |
| `functools` | 高阶函数工具 | `lru_cache`, `partial`, `reduce` |
| `itertools` | 迭代器工具 | `chain`, `groupby`, `islice` |
| `collections` | 高级容器 | `defaultdict`, `Counter`, `deque` |

### 高频用法示例

```python
import os
import sys
import re
import hashlib
from datetime import datetime, timedelta
from collections import defaultdict, Counter

# os.environ
db_host = os.environ.get("DB_HOST", "localhost")
os.environ["APP_ENV"] = "prod"

# sys.exit
if not os.path.exists("/etc/app/config.yaml"):
    print("缺少配置文件", file=sys.stderr)
    sys.exit(1)

# re 正则
log_line = "2025-12-09 10:23:45 ERROR [web-01] connection refused (errno=111)"
pattern = r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (\w+) \[(.+?)\] (.+)"
m = re.match(pattern, log_line)
if m:
    ts, level, host, msg = m.groups()

# 提取所有 IP
text = "servers: 10.0.1.10, 10.0.1.11, and 192.168.0.1"
ips = re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text)

# datetime
now = datetime.now()
yesterday = now - timedelta(days=1)
ts_str = now.strftime("%Y-%m-%d %H:%M:%S")
parsed = datetime.strptime("2025-12-09 10:00:00", "%Y-%m-%d %H:%M:%S")

# Counter 统计日志级别分布
levels = ["INFO", "ERROR", "INFO", "WARN", "ERROR", "ERROR"]
counter = Counter(levels)
print(counter.most_common(3))   # [('ERROR', 3), ('INFO', 2), ('WARN', 1)]

# defaultdict 聚合
server_errors: dict[str, list[str]] = defaultdict(list)
events = [("web-01", "timeout"), ("web-02", "refused"), ("web-01", "500")]
for srv, err in events:
    server_errors[srv].append(err)

# hashlib 校验文件
def file_md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
```
