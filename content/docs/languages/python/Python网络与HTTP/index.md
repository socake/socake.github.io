---
title: "Python 网络编程与 HTTP 请求"
date: 2025-12-09T10:00:00+08:00
draft: false
tags: ["Python", "编程", "运维"]
categories: ["Python"]
description: "Python HTTP 请求实战：requests 库全面用法、自动重试、httpx 异步请求、并发健康检查，附完整多端点健康检查脚本"
summary: "从 requests 基础到 httpx 异步，再到并发健康检查脚本，覆盖运维工程师日常 HTTP 操作场景"
toc: true
math: false
diagram: false
keywords: ["Python", "requests", "httpx", "HTTP", "健康检查", "并发"]
params:
  reading_time: true
---

## requests 库基础

```bash
pip install requests
```

### GET 请求

```python
import requests
from requests import Response

# 最简单的 GET
resp = requests.get("https://httpbin.org/get")
print(resp.status_code)           # 200
print(resp.text)                  # 原始文本
print(resp.json())                # 解析 JSON（自动根据 Content-Type）
print(resp.headers)               # 响应头字典
print(resp.elapsed.total_seconds())  # 响应时间（秒）

# 带查询参数
params = {"page": 1, "per_page": 100, "status": "active"}
resp = requests.get("https://api.example.com/servers", params=params)
# 实际 URL: https://api.example.com/servers?page=1&per_page=100&status=active
print(resp.url)

# 带请求头
headers = {
    "Authorization": "Bearer eyJhbGci...",
    "Content-Type": "application/json",
    "User-Agent": "myops-bot/1.0",
}
resp = requests.get("https://api.example.com/nodes", headers=headers)

# 设置超时（推荐总是设置，避免永久挂起）
# timeout=(connect_timeout, read_timeout)
resp = requests.get("https://api.example.com/health", timeout=(3, 10))
```

### POST 请求

```python
# 发送 JSON body
payload = {
    "service": "nginx",
    "action": "restart",
    "env": "prod",
}
resp = requests.post(
    "https://ops-api.internal/actions",
    json=payload,              # 自动设置 Content-Type: application/json
    headers={"Authorization": "Bearer token"},
    timeout=10,
)
resp.raise_for_status()        # 非 2xx 时抛出 HTTPError

# 发送 form 表单
resp = requests.post(
    "https://example.com/login",
    data={"username": "admin", "password": "secret"},
)

# 上传文件
with open("/tmp/report.tar.gz", "rb") as f:
    resp = requests.post(
        "https://storage.example.com/upload",
        files={"file": ("report.tar.gz", f, "application/gzip")},
        data={"description": "daily report"},
        timeout=60,
    )

# 发送原始字节
import json
raw_body = json.dumps(payload).encode("utf-8")
resp = requests.post(
    "https://api.example.com/events",
    data=raw_body,
    headers={"Content-Type": "application/json"},
    timeout=10,
)
```

### 响应处理

```python
import requests
from requests.exceptions import (
    HTTPError,
    ConnectionError,
    Timeout,
    RequestException,
)

def safe_get(url: str, **kwargs) -> dict | None:
    """安全的 GET 请求，返回 JSON 或 None。"""
    try:
        resp = requests.get(url, timeout=10, **kwargs)
        resp.raise_for_status()
        return resp.json()
    except HTTPError as e:
        print(f"HTTP 错误: {e.response.status_code} {url}")
        return None
    except ConnectionError:
        print(f"连接失败: {url}")
        return None
    except Timeout:
        print(f"请求超时: {url}")
        return None
    except RequestException as e:
        print(f"请求异常: {e}")
        return None


# 检查状态码
resp = requests.get("https://example.com/health", timeout=5)

if resp.status_code == 200:
    print("服务正常")
elif resp.status_code == 401:
    print("认证失败")
elif resp.status_code == 429:
    retry_after = resp.headers.get("Retry-After", "未知")
    print(f"限速，{retry_after}秒后重试")
elif resp.status_code >= 500:
    print(f"服务端错误: {resp.status_code}")

# 解析 JSON（带错误处理）
try:
    data = resp.json()
except ValueError:
    print(f"响应不是 JSON: {resp.text[:200]}")
    data = {}

# 流式下载大文件
def download_file(url: str, dest: str, chunk_size: int = 65536) -> None:
    with requests.get(url, stream=True, timeout=30) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("Content-Length", 0))
        downloaded = 0
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=chunk_size):
                f.write(chunk)
                downloaded += len(chunk)
                if total:
                    pct = downloaded / total * 100
                    print(f"\r下载进度: {pct:.1f}%", end="", flush=True)
        print()
```

## Session 与连接复用

```python
import requests

# Session 会复用 TCP 连接，并自动携带 cookies
session = requests.Session()

# 设置全局头部（每次请求都带）
session.headers.update({
    "Authorization": "Bearer mytoken",
    "User-Agent": "ops-tool/2.0",
})

# 设置全局超时（通过 mount 无法直接设，但可以在请求时指定）
resp1 = session.get("https://api.example.com/nodes", timeout=10)
resp2 = session.get("https://api.example.com/pods", timeout=10)

# 基本认证
session.auth = ("admin", "password")

# ===== 封装带认证的 API 客户端 =====
class OpsAPIClient:
    def __init__(self, base_url: str, token: str, timeout: int = 10):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        })

    def get(self, path: str, **kwargs) -> dict:
        resp = self._session.get(
            f"{self.base_url}{path}",
            timeout=self.timeout,
            **kwargs,
        )
        resp.raise_for_status()
        return resp.json()

    def post(self, path: str, data: dict, **kwargs) -> dict:
        resp = self._session.post(
            f"{self.base_url}{path}",
            json=data,
            timeout=self.timeout,
            **kwargs,
        )
        resp.raise_for_status()
        return resp.json()

    def close(self):
        self._session.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# 使用（自动关闭）
with OpsAPIClient("https://ops-api.internal", token="abc123") as client:
    nodes = client.get("/v1/nodes")
    result = client.post("/v1/deploy", {"service": "api", "version": "1.2.3"})
```

## 自动重试（urllib3 Retry）

```python
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def build_session(
    total_retries: int = 3,
    backoff_factor: float = 0.5,
    status_forcelist: tuple[int, ...] = (429, 500, 502, 503, 504),
) -> requests.Session:
    """
    创建带自动重试的 Session。

    backoff_factor:
        第1次重试等待 0.5s
        第2次重试等待 1.0s
        第3次重试等待 2.0s
        公式: {backoff_factor} * 2^(retry_number - 1)
    """
    session = requests.Session()

    retry = Retry(
        total=total_retries,
        backoff_factor=backoff_factor,
        status_forcelist=status_forcelist,
        allowed_methods={"GET", "POST", "PUT", "DELETE", "HEAD"},
        raise_on_status=False,    # 不让 Retry 自动 raise，让调用方处理
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


# 使用
session = build_session(total_retries=3, backoff_factor=1.0)
resp = session.get("https://api.example.com/health", timeout=10)
resp.raise_for_status()
```

## httpx 简介（异步 HTTP）

```bash
pip install httpx
```

```python
import httpx
import asyncio

# ===== 同步用法（可替代 requests）=====
with httpx.Client(timeout=10.0) as client:
    resp = client.get("https://httpbin.org/get")
    print(resp.json())

# ===== 异步用法 =====
async def fetch(client: httpx.AsyncClient, url: str) -> dict:
    resp = await client.get(url, timeout=10.0)
    resp.raise_for_status()
    return resp.json()


async def fetch_all(urls: list[str]) -> list[dict]:
    async with httpx.AsyncClient() as client:
        tasks = [fetch(client, url) for url in urls]
        return await asyncio.gather(*tasks, return_exceptions=True)


# 运行
urls = [
    "https://httpbin.org/get",
    "https://httpbin.org/ip",
    "https://httpbin.org/uuid",
]
results = asyncio.run(fetch_all(urls))
for r in results:
    print(r)
```

## socket 基础：TCP 端口检测

```python
import socket
from contextlib import closing

def is_port_open(host: str, port: int, timeout: float = 3.0) -> bool:
    """检测 TCP 端口是否可达。"""
    try:
        with closing(socket.create_connection((host, port), timeout=timeout)):
            return True
    except (socket.timeout, ConnectionRefusedError, OSError):
        return False


def check_services(endpoints: list[tuple[str, int]]) -> dict[str, bool]:
    """批量检测服务端口。"""
    results = {}
    for host, port in endpoints:
        key = f"{host}:{port}"
        results[key] = is_port_open(host, port)
    return results


# 示例
services = [
    ("10.0.1.10", 80),
    ("10.0.2.10", 5432),
    ("10.0.3.10", 6379),
]
for endpoint, ok in check_services(services).items():
    status = "UP" if ok else "DOWN"
    print(f"  {endpoint:25s} {status}")
```

## 并发 HTTP 请求

### ThreadPoolExecutor

```python
from concurrent.futures import ThreadPoolExecutor, as_completed, Future
import requests

def check_health(url: str, timeout: int = 5) -> dict:
    try:
        resp = requests.get(url, timeout=timeout)
        return {
            "url": url,
            "status": resp.status_code,
            "ok": resp.ok,
            "latency_ms": resp.elapsed.total_seconds() * 1000,
        }
    except requests.exceptions.Timeout:
        return {"url": url, "status": 0, "ok": False, "latency_ms": -1, "error": "timeout"}
    except requests.exceptions.ConnectionError:
        return {"url": url, "status": 0, "ok": False, "latency_ms": -1, "error": "connection_error"}
    except Exception as e:
        return {"url": url, "status": 0, "ok": False, "latency_ms": -1, "error": str(e)}


def batch_health_check(urls: list[str], max_workers: int = 10) -> list[dict]:
    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_url: dict[Future, str] = {
            executor.submit(check_health, url): url
            for url in urls
        }
        for future in as_completed(future_to_url):
            results.append(future.result())
    return sorted(results, key=lambda x: x["url"])
```

### asyncio + httpx（更高效）

```python
import asyncio
import httpx
from dataclasses import dataclass

@dataclass
class CheckResult:
    url: str
    ok: bool
    status_code: int
    latency_ms: float
    error: str = ""


async def check_one(client: httpx.AsyncClient, url: str) -> CheckResult:
    import time
    start = time.monotonic()
    try:
        resp = await client.get(url, timeout=5.0)
        elapsed = (time.monotonic() - start) * 1000
        return CheckResult(
            url=url,
            ok=resp.is_success,
            status_code=resp.status_code,
            latency_ms=elapsed,
        )
    except httpx.TimeoutException:
        return CheckResult(url=url, ok=False, status_code=0, latency_ms=-1, error="timeout")
    except httpx.ConnectError:
        return CheckResult(url=url, ok=False, status_code=0, latency_ms=-1, error="connect_error")
    except Exception as e:
        return CheckResult(url=url, ok=False, status_code=0, latency_ms=-1, error=str(e))


async def async_batch_check(urls: list[str]) -> list[CheckResult]:
    async with httpx.AsyncClient(follow_redirects=True) as client:
        tasks = [check_one(client, url) for url in urls]
        return await asyncio.gather(*tasks)
```

## 实战：批量健康检查脚本

完整脚本，支持从 YAML/命令行读取端点，并发检查，输出格式化报告：

```python
#!/usr/bin/env python3
"""
health_check.py — 批量服务健康检查

用法:
    python health_check.py --urls https://web-01/health https://web-02/health
    python health_check.py --config endpoints.yaml
    python health_check.py --config endpoints.yaml --workers 20 --timeout 3
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ── 日志 ──────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ── 数据结构 ──────────────────────────────────────────────────────────────────
@dataclass
class Endpoint:
    url: str
    name: str = ""
    expected_status: int = 200
    timeout: float = 5.0

    def __post_init__(self):
        if not self.name:
            self.name = self.url


@dataclass
class CheckResult:
    endpoint: Endpoint
    ok: bool
    status_code: int
    latency_ms: float
    error: str = ""
    checked_at: str = field(default_factory=lambda: datetime.now().isoformat())


# ── HTTP 客户端 ───────────────────────────────────────────────────────────────
def make_session(retries: int = 1) -> requests.Session:
    session = requests.Session()
    retry = Retry(total=retries, backoff_factor=0.3, status_forcelist=(500, 502, 503))
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers["User-Agent"] = "health-checker/1.0"
    return session


_session: requests.Session | None = None


def get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = make_session()
    return _session


# ── 检查逻辑 ──────────────────────────────────────────────────────────────────
def check_endpoint(ep: Endpoint) -> CheckResult:
    """检查单个端点，返回结果。"""
    session = get_session()
    start = time.monotonic()

    try:
        resp = session.get(ep.url, timeout=ep.timeout, allow_redirects=True)
        latency = (time.monotonic() - start) * 1000
        ok = resp.status_code == ep.expected_status
        return CheckResult(
            endpoint=ep,
            ok=ok,
            status_code=resp.status_code,
            latency_ms=round(latency, 2),
            error="" if ok else f"期望 {ep.expected_status}，实际 {resp.status_code}",
        )
    except requests.exceptions.Timeout:
        return CheckResult(
            endpoint=ep, ok=False, status_code=0,
            latency_ms=round((time.monotonic() - start) * 1000, 2),
            error="timeout",
        )
    except requests.exceptions.ConnectionError as e:
        return CheckResult(
            endpoint=ep, ok=False, status_code=0,
            latency_ms=round((time.monotonic() - start) * 1000, 2),
            error=f"connection_error: {e}",
        )
    except Exception as e:
        return CheckResult(
            endpoint=ep, ok=False, status_code=0,
            latency_ms=round((time.monotonic() - start) * 1000, 2),
            error=str(e),
        )


def run_checks(endpoints: list[Endpoint], max_workers: int = 10) -> list[CheckResult]:
    """并发检查所有端点。"""
    results: list[CheckResult] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {executor.submit(check_endpoint, ep): ep for ep in endpoints}
        for future in as_completed(future_map):
            results.append(future.result())
    return sorted(results, key=lambda r: r.endpoint.name)


# ── 配置加载 ──────────────────────────────────────────────────────────────────
def load_from_yaml(path: str) -> list[Endpoint]:
    """
    endpoints.yaml 格式：
    endpoints:
      - name: web-01
        url: https://web-01.prod/health
        expected_status: 200
        timeout: 5
      - url: https://web-02.prod/health
    """
    try:
        import yaml
    except ImportError:
        logger.error("需要安装 PyYAML: pip install pyyaml")
        sys.exit(1)

    with open(path) as f:
        data = yaml.safe_load(f)

    endpoints = []
    for item in data.get("endpoints", []):
        endpoints.append(
            Endpoint(
                url=item["url"],
                name=item.get("name", ""),
                expected_status=item.get("expected_status", 200),
                timeout=float(item.get("timeout", 5)),
            )
        )
    return endpoints


# ── 报告输出 ──────────────────────────────────────────────────────────────────
def print_report(results: list[CheckResult]) -> None:
    ok_count = sum(1 for r in results if r.ok)
    fail_count = len(results) - ok_count

    print("\n" + "=" * 68)
    print(f"  健康检查报告  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 68)
    print(f"  {'名称':<25} {'状态':<8} {'HTTP':<6} {'延迟':>8}  {'错误'}")
    print("-" * 68)

    for r in results:
        status = "OK" if r.ok else "FAIL"
        status_str = f"\033[32m{status}\033[0m" if r.ok else f"\033[31m{status}\033[0m"
        latency = f"{r.latency_ms:.0f}ms" if r.latency_ms >= 0 else "N/A"
        print(
            f"  {r.endpoint.name:<25} {status:<8} {r.status_code:<6} "
            f"{latency:>8}  {r.error}"
        )

    print("=" * 68)
    print(f"  总计: {len(results)}  正常: {ok_count}  异常: {fail_count}")
    print()


# ── 入口 ──────────────────────────────────────────────────────────────────────
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="批量 HTTP 健康检查")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--urls", nargs="+", metavar="URL", help="直接指定 URL 列表")
    group.add_argument("--config", metavar="YAML", help="从 YAML 配置文件读取端点")
    parser.add_argument("--workers", type=int, default=10, help="并发数（默认 10）")
    parser.add_argument("--timeout", type=float, default=5.0, help="超时秒数（默认 5）")
    parser.add_argument("--output-json", metavar="FILE", help="将结果写入 JSON 文件")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.config:
        endpoints = load_from_yaml(args.config)
        logger.info(f"从配置文件加载 {len(endpoints)} 个端点: {args.config}")
    else:
        endpoints = [Endpoint(url=u, timeout=args.timeout) for u in args.urls]

    if not endpoints:
        logger.error("没有可检查的端点")
        return 1

    logger.info(f"开始检查 {len(endpoints)} 个端点（并发={args.workers}）...")
    start = time.monotonic()
    results = run_checks(endpoints, max_workers=args.workers)
    elapsed = time.monotonic() - start

    print_report(results)
    logger.info(f"检查完成，耗时 {elapsed:.2f}s")

    if args.output_json:
        out = [
            {
                "name": r.endpoint.name,
                "url": r.endpoint.url,
                "ok": r.ok,
                "status_code": r.status_code,
                "latency_ms": r.latency_ms,
                "error": r.error,
                "checked_at": r.checked_at,
            }
            for r in results
        ]
        Path(args.output_json).write_text(
            json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        logger.info(f"结果已写入: {args.output_json}")

    failed = [r for r in results if not r.ok]
    if failed:
        logger.error(f"{len(failed)} 个端点异常")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

### 配置文件示例

```yaml
# endpoints.yaml
endpoints:
  - name: web-01-health
    url: https://web-01.prod.example.com/health
    expected_status: 200
    timeout: 5

  - name: web-02-health
    url: https://web-02.prod.example.com/health
    expected_status: 200
    timeout: 5

  - name: api-gateway
    url: https://api.prod.example.com/v1/ping
    expected_status: 200
    timeout: 3

  - name: grafana
    url: http://grafana.monitoring:3000/api/health
    expected_status: 200
    timeout: 10
```

### 运行示例

```bash
# 直接指定 URL
python health_check.py --urls https://web-01/health https://web-02/health

# 从配置读取，输出 JSON
python health_check.py --config endpoints.yaml --workers 20 --output-json result.json

# 非零退出码可用于 CI/监控
python health_check.py --config endpoints.yaml || echo "有服务异常"
```
