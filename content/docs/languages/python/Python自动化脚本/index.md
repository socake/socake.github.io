---
title: "Python 自动化运维脚本实战"
date: 2025-12-09T10:00:00+08:00
draft: false
tags: ["Python", "编程", "运维"]
categories: ["Python"]
description: "Python 自动化脚本实战：argparse 参数解析、logging 规范配置、YAML/JSON 配置处理、钉钉/企微告警、并发执行，附完整脚本模板"
summary: "系统化讲解 Python 自动化运维脚本的标准结构，包含命令行解析、日志、配置、告警和并发执行的完整最佳实践"
toc: true
math: false
diagram: false
keywords: ["Python", "argparse", "logging", "钉钉告警", "自动化", "运维脚本"]
params:
  reading_time: true
---

## argparse 命令行参数解析

argparse 是标准库中最完整的命令行解析方案，适合运维脚本对外暴露参数。

### 基础用法

```python
import argparse

parser = argparse.ArgumentParser(
    description="服务部署工具",
    formatter_class=argparse.RawDescriptionHelpFormatter,
    epilog="""
示例:
  %(prog)s deploy api --version v1.2.3 --env prod
  %(prog)s rollback api --env staging
""",
)

# 位置参数（必填，无 --）
parser.add_argument("service", help="服务名称")

# 可选参数
parser.add_argument("--version", "-v", required=True, help="版本号，如 v1.2.3")
parser.add_argument("--env", "-e", choices=["dev", "staging", "prod"], default="staging")
parser.add_argument("--replicas", type=int, default=2, metavar="N", help="副本数（默认 2）")
parser.add_argument("--dry-run", action="store_true", help="模拟运行，不实际执行")
parser.add_argument("--tags", nargs="+", metavar="TAG", help="附加标签，可多个")
parser.add_argument("--timeout", type=float, default=300.0, help="超时秒数")

args = parser.parse_args()
print(args.service, args.version, args.env, args.dry_run)
```

### 子命令（subparsers）

```python
import argparse
import sys

def cmd_deploy(args: argparse.Namespace) -> int:
    print(f"部署 {args.service} {args.version} 到 {args.env}")
    return 0

def cmd_rollback(args: argparse.Namespace) -> int:
    print(f"回滚 {args.service} 在 {args.env}")
    return 0

def cmd_status(args: argparse.Namespace) -> int:
    print(f"查询 {args.service} 状态 (namespace={args.namespace})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="运维工具集")

    # 全局参数（所有子命令共享）
    parser.add_argument("--debug", action="store_true", help="开启调试日志")
    parser.add_argument("--config", default="~/.ops/config.yaml", help="配置文件路径")

    subs = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    # deploy 子命令
    p_deploy = subs.add_parser("deploy", help="部署服务")
    p_deploy.add_argument("service", help="服务名")
    p_deploy.add_argument("--version", required=True)
    p_deploy.add_argument("--env", choices=["staging", "prod"], default="staging")
    p_deploy.set_defaults(func=cmd_deploy)

    # rollback 子命令
    p_roll = subs.add_parser("rollback", help="回滚服务")
    p_roll.add_argument("service")
    p_roll.add_argument("--env", choices=["staging", "prod"], default="staging")
    p_roll.set_defaults(func=cmd_rollback)

    # status 子命令
    p_status = subs.add_parser("status", help="查看状态")
    p_status.add_argument("service")
    p_status.add_argument("--namespace", "-n", default="default")
    p_status.set_defaults(func=cmd_status)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
```

### 参数校验

```python
import argparse
import re
from pathlib import Path


def validate_version(val: str) -> str:
    if not re.match(r"^v\d+\.\d+\.\d+$", val):
        raise argparse.ArgumentTypeError(f"版本格式错误: {val}（期望 vX.Y.Z）")
    return val


def validate_existing_file(val: str) -> Path:
    p = Path(val)
    if not p.exists():
        raise argparse.ArgumentTypeError(f"文件不存在: {val}")
    return p


parser = argparse.ArgumentParser()
parser.add_argument("--version", type=validate_version)
parser.add_argument("--config", type=validate_existing_file)
parser.add_argument("--port", type=int, choices=range(1024, 65536), metavar="[1024-65535]")
```

## logging 规范配置

### 基础配置

```python
import logging
import sys

# 最简单的全局配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)

logger = logging.getLogger(__name__)
logger.info("服务启动")
logger.warning("磁盘使用率超过 80%%")
logger.error("连接数据库失败")
logger.debug("调试信息（INFO 级别不会显示）")
```

### 文件 + 控制台双输出 + 按日期轮转

```python
import logging
import logging.handlers
import sys
from pathlib import Path


def setup_logging(
    name: str = "ops",
    level: int = logging.INFO,
    log_dir: str = "/var/log/ops",
    max_bytes: int = 50 * 1024 * 1024,   # 50MB
    backup_count: int = 7,
) -> logging.Logger:
    """
    配置日志：
    - 控制台：INFO+（带颜色）
    - 文件：DEBUG+，按大小轮转，保留 backup_count 个
    """
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)

    # 避免重复添加 handler
    if logger.handlers:
        return logger

    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)-8s] %(name)s:%(lineno)d %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # ── 控制台 handler ──
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(fmt)
    logger.addHandler(console_handler)

    # ── 文件 handler（按大小轮转）──
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        filename=log_path / f"{name}.log",
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    return logger


# 按日期轮转（每天一个文件，保留30天）
def setup_timed_logging(name: str, log_dir: str = "/var/log/ops") -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    if logger.handlers:
        return logger

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    Path(log_dir).mkdir(parents=True, exist_ok=True)

    handler = logging.handlers.TimedRotatingFileHandler(
        filename=f"{log_dir}/{name}.log",
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
    )
    handler.suffix = "%Y-%m-%d"
    handler.setFormatter(fmt)
    logger.addHandler(handler)
    return logger


# 使用
logger = setup_logging("deploy-tool", level=logging.INFO, log_dir="/var/log/deploy")
logger.info("部署开始")
logger.error("部署失败: %s", "连接超时")   # 用 % 格式避免提前字符串拼接

# 临时调整级别（不重启脚本）
logging.getLogger("deploy-tool").setLevel(logging.DEBUG)
```

## YAML/JSON 配置文件处理

```python
import json
import os
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None   # type: ignore


# ===== JSON 配置 =====
def load_json_config(path: str | Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json_config(data: dict, path: str | Path, indent: int = 2) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=indent, ensure_ascii=False)


# ===== YAML 配置 =====
def load_yaml_config(path: str | Path) -> dict[str, Any]:
    if yaml is None:
        raise ImportError("请安装 PyYAML: pip install pyyaml")
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def save_yaml_config(data: dict, path: str | Path) -> None:
    if yaml is None:
        raise ImportError("请安装 PyYAML: pip install pyyaml")
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)


# ===== 带环境变量覆盖的配置加载 =====
class Config:
    """
    从 YAML 加载配置，支持环境变量覆盖。

    config.yaml:
      database:
        host: localhost
        port: 5432
      app:
        debug: false

    环境变量 APP_DATABASE_HOST=10.0.2.10 会覆盖 database.host
    """

    def __init__(self, path: str, prefix: str = "APP"):
        self._data = load_yaml_config(path)
        self._prefix = prefix
        self._apply_env_overrides()

    def _apply_env_overrides(self) -> None:
        prefix = self._prefix + "_"
        for key, val in os.environ.items():
            if not key.startswith(prefix):
                continue
            parts = key[len(prefix):].lower().split("_")
            d = self._data
            for part in parts[:-1]:
                d = d.setdefault(part, {})
            # 类型推断
            existing = d.get(parts[-1])
            if isinstance(existing, bool):
                d[parts[-1]] = val.lower() in ("true", "1", "yes")
            elif isinstance(existing, int):
                d[parts[-1]] = int(val)
            elif isinstance(existing, float):
                d[parts[-1]] = float(val)
            else:
                d[parts[-1]] = val

    def get(self, *keys: str, default: Any = None) -> Any:
        d = self._data
        for k in keys:
            if not isinstance(d, dict):
                return default
            d = d.get(k, default)
        return d

    def __getitem__(self, key: str) -> Any:
        return self._data[key]


# 使用
# cfg = Config("config.yaml", prefix="APP")
# db_host = cfg.get("database", "host", default="localhost")
```

## 钉钉 / 企微 Webhook 告警

### 钉钉

```python
import requests
import json
import hashlib
import hmac
import base64
import time
from urllib.parse import quote


def send_dingtalk(
    webhook_url: str,
    title: str,
    content: str,
    secret: str | None = None,
    is_at_all: bool = False,
    at_mobiles: list[str] | None = None,
) -> bool:
    """
    发送钉钉 Markdown 消息。

    Args:
        webhook_url: 机器人 Webhook URL
        secret: 签名密钥（加签模式，可选）
        is_at_all: 是否 @所有人
        at_mobiles: 要 @的手机号列表
    """
    url = webhook_url

    # 加签
    if secret:
        timestamp = str(round(time.time() * 1000))
        sign_str = f"{timestamp}\n{secret}"
        sign = base64.b64encode(
            hmac.new(secret.encode(), sign_str.encode(), digestmod=hashlib.sha256).digest()
        ).decode()
        url += f"&timestamp={timestamp}&sign={quote(sign)}"

    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": title,
            "text": f"## {title}\n\n{content}",
        },
        "at": {
            "atMobiles": at_mobiles or [],
            "isAtAll": is_at_all,
        },
    }

    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        result = resp.json()
        if result.get("errcode") != 0:
            print(f"钉钉告警失败: {result.get('errmsg')}")
            return False
        return True
    except Exception as e:
        print(f"发送钉钉告警异常: {e}")
        return False


def alert_deploy_success(webhook: str, service: str, version: str, env: str) -> None:
    content = (
        f"> **服务**: {service}  \n"
        f"> **版本**: {version}  \n"
        f"> **环境**: {env}  \n"
        f"> **时间**: {time.strftime('%Y-%m-%d %H:%M:%S')}  \n"
    )
    send_dingtalk(webhook, f"部署成功: {service}", content)


def alert_error(webhook: str, title: str, error_msg: str, host: str = "") -> None:
    content = (
        f"> **错误**: {error_msg}  \n"
        f"> **主机**: {host or '未知'}  \n"
        f"> **时间**: {time.strftime('%Y-%m-%d %H:%M:%S')}  \n"
    )
    send_dingtalk(webhook, f"告警: {title}", content, is_at_all=True)
```

### 企业微信

```python
def send_wecom(webhook_url: str, title: str, content: str) -> bool:
    """发送企业微信 Markdown 消息。"""
    payload = {
        "msgtype": "markdown",
        "markdown": {
            "content": f"# {title}\n{content}",
        },
    }
    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        resp.raise_for_status()
        result = resp.json()
        if result.get("errcode") != 0:
            print(f"企微告警失败: {result}")
            return False
        return True
    except Exception as e:
        print(f"发送企微告警异常: {e}")
        return False
```

## 并发执行：ThreadPoolExecutor

```python
from concurrent.futures import ThreadPoolExecutor, as_completed, Future
import logging

logger = logging.getLogger(__name__)


def ssh_exec(host: str, command: str, timeout: int = 30) -> dict:
    """
    SSH 执行命令（示例，实际需要 paramiko 或 fabric）。
    pip install paramiko
    """
    import subprocess
    result = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
         host, command],
        capture_output=True, text=True, timeout=timeout,
    )
    return {
        "host": host,
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
        "ok": result.returncode == 0,
    }


def batch_ssh_exec(
    hosts: list[str],
    command: str,
    max_workers: int = 10,
    timeout: int = 30,
) -> list[dict]:
    """在多台主机上并发执行命令，返回结果列表。"""
    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures: dict[Future, str] = {
            executor.submit(ssh_exec, host, command, timeout): host
            for host in hosts
        }
        for future in as_completed(futures):
            host = futures[future]
            try:
                result = future.result()
                results.append(result)
                status = "OK" if result["ok"] else "FAIL"
                logger.info(f"[{status}] {host}: {result['stdout'][:100]}")
            except Exception as e:
                logger.error(f"[ERROR] {host}: {e}")
                results.append({"host": host, "ok": False, "error": str(e)})
    return sorted(results, key=lambda r: r["host"])


# ===== 带超时的批量 API 调用 =====
def batch_api_call(
    items: list[dict],
    call_fn,
    max_workers: int = 5,
) -> list[dict]:
    """
    通用批量调用框架。

    Args:
        items: 输入参数列表，每个元素传给 call_fn
        call_fn: 单次调用函数，接受 dict，返回 dict
        max_workers: 并发数
    """
    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {executor.submit(call_fn, item): item for item in items}
        for future in as_completed(future_map):
            item = future_map[future]
            try:
                results.append(future.result())
            except Exception as e:
                results.append({"input": item, "error": str(e), "ok": False})
    return results
```

## 完整脚本模板

以下是一个符合生产标准的完整运维脚本模板，包含：参数解析、配置加载、日志、主逻辑、异常处理、退出码。

```python
#!/usr/bin/env python3
"""
ops_task.py — 运维任务脚本模板

用法:
    python ops_task.py --config config.yaml --env prod
    python ops_task.py --config config.yaml --env staging --dry-run --debug
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys
import time
from pathlib import Path
from typing import Any

# ── 常量 ──────────────────────────────────────────────────────────────────────
VERSION = "1.0.0"
DEFAULT_CONFIG = "~/.ops/config.yaml"


# ── 日志初始化 ────────────────────────────────────────────────────────────────
def setup_logging(debug: bool = False, log_file: str | None = None) -> logging.Logger:
    level = logging.DEBUG if debug else logging.INFO
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # 控制台
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(level)
    ch.setFormatter(fmt)
    root.addHandler(ch)

    # 文件（可选）
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=50 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        root.addHandler(fh)

    return logging.getLogger(__name__)


# ── 配置加载 ──────────────────────────────────────────────────────────────────
def load_config(path: str) -> dict[str, Any]:
    config_path = Path(os.path.expanduser(path))
    if not config_path.exists():
        raise FileNotFoundError(f"配置文件不存在: {config_path}")

    try:
        import yaml
        with open(config_path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except ImportError:
        import json
        with open(config_path, encoding="utf-8") as f:
            return json.load(f)


# ── 主逻辑 ────────────────────────────────────────────────────────────────────
def run_task(config: dict, env: str, dry_run: bool, logger: logging.Logger) -> int:
    """
    主业务逻辑，返回退出码（0=成功，非0=失败）。
    在这里替换为实际业务代码。
    """
    logger.info(f"开始执行任务  env={env}  dry_run={dry_run}")

    targets = config.get("targets", {}).get(env, [])
    if not targets:
        logger.warning(f"环境 {env} 没有配置目标，跳过")
        return 0

    errors = 0
    for target in targets:
        try:
            logger.info(f"处理目标: {target}")
            if dry_run:
                logger.info(f"  [DRY] 跳过实际操作: {target}")
                continue
            # ↓ 替换为实际操作
            time.sleep(0.1)   # 模拟操作
            logger.info(f"  完成: {target}")
        except Exception as e:
            logger.error(f"  失败: {target}: {e}")
            errors += 1

    if errors:
        logger.error(f"共 {errors} 个目标失败")
        return 1

    logger.info("所有目标处理完成")
    return 0


# ── 参数解析 ──────────────────────────────────────────────────────────────────
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=f"运维任务脚本 v{VERSION}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config", default=DEFAULT_CONFIG, metavar="FILE",
        help=f"配置文件路径（默认: {DEFAULT_CONFIG}）",
    )
    parser.add_argument(
        "--env", required=True,
        choices=["dev", "staging", "prod"],
        help="目标环境",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="模拟运行，不实际执行写操作",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="开启 DEBUG 级别日志",
    )
    parser.add_argument(
        "--log-file", metavar="FILE",
        help="日志文件路径（可选）",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {VERSION}",
    )
    return parser.parse_args()


# ── 入口 ──────────────────────────────────────────────────────────────────────
def main() -> int:
    args = parse_args()

    # 1. 初始化日志
    logger = setup_logging(debug=args.debug, log_file=args.log_file)
    logger.debug(f"参数: {vars(args)}")

    # 2. 安全提示
    if args.env == "prod" and not args.dry_run:
        logger.warning("目标环境为 PROD，将执行实际操作")

    # 3. 加载配置
    try:
        config = load_config(args.config)
        logger.info(f"配置加载成功: {args.config}")
    except FileNotFoundError as e:
        logger.error(str(e))
        return 1
    except Exception as e:
        logger.error(f"配置加载失败: {e}")
        return 1

    # 4. 执行主逻辑
    start = time.monotonic()
    try:
        exit_code = run_task(config, args.env, args.dry_run, logger)
    except KeyboardInterrupt:
        logger.warning("用户中断")
        return 130
    except Exception as e:
        logger.exception(f"未预期的异常: {e}")
        return 1
    finally:
        elapsed = time.monotonic() - start
        logger.info(f"总耗时: {elapsed:.2f}s")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
```

### 配置文件格式（config.yaml）

```yaml
targets:
  dev:
    - server-dev-01
    - server-dev-02
  staging:
    - server-stg-01
  prod:
    - server-prod-01
    - server-prod-02
    - server-prod-03

notifications:
  dingtalk:
    webhook: https://oapi.dingtalk.com/robot/send?access_token=xxx
    secret: SECxxx

settings:
  timeout: 30
  max_workers: 10
```
