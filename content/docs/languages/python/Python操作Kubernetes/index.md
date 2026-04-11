---
title: "Python 操作 Kubernetes：kubernetes-client 实战"
date: 2025-12-09T10:00:00+08:00
draft: false
tags: ["Python", "编程", "运维", "Kubernetes"]
categories: ["Python"]
description: "用 Python kubernetes-client 操作 K8s：认证配置、Pod/Deployment 管理、CRD 操作、Watch 机制，附完整巡检脚本检查 Pending Pod 和频繁重启容器"
summary: "系统介绍 Python kubernetes-client 的核心用法，从集群认证到资源操作，最终构建一个完整的 K8s 巡检脚本"
toc: true
math: false
diagram: false
keywords: ["Python", "Kubernetes", "kubernetes-client", "运维", "巡检", "K8s"]
params:
  reading_time: true
---

## 安装与认证

```bash
pip install kubernetes
```

### 认证方式

```python
from kubernetes import client, config
from kubernetes.client import ApiClient

# ── 方式1：读取本地 kubeconfig（开发/本地调试）──
config.load_kube_config()                          # 默认 ~/.kube/config
config.load_kube_config(config_file="/path/to/kubeconfig")
config.load_kube_config(context="prod-cluster")    # 指定 context

# ── 方式2：集群内认证（Pod 内运行时）──
# 读取 /var/run/secrets/kubernetes.io/serviceaccount/
config.load_incluster_config()

# ── 方式3：自动判断（推荐写法）──
def load_k8s_config(kubeconfig: str | None = None, context: str | None = None) -> None:
    """自动选择认证方式：优先 in-cluster，其次 kubeconfig。"""
    try:
        config.load_incluster_config()
        print("使用 in-cluster 认证")
    except config.config_exception.ConfigException:
        config.load_kube_config(config_file=kubeconfig, context=context)
        print(f"使用 kubeconfig 认证  context={context or 'default'}")


# ── 方式4：手动指定 API Server（适合多集群）──
configuration = client.Configuration()
configuration.host = "https://10.0.0.1:6443"
configuration.verify_ssl = False
configuration.api_key["authorization"] = "Bearer eyJhbGci..."

with ApiClient(configuration) as api_client:
    v1 = client.CoreV1Api(api_client)
    pods = v1.list_pod_for_all_namespaces()
```

## CoreV1Api：Pod 操作

```python
from kubernetes import client, config
from kubernetes.client.rest import ApiException

config.load_kube_config()
v1 = client.CoreV1Api()


# ── 列出 Pod ──────────────────────────────────────────────────────────────────
def list_pods(namespace: str = "default", label_selector: str = "") -> list:
    """列出指定命名空间的 Pod。"""
    resp = v1.list_namespaced_pod(
        namespace=namespace,
        label_selector=label_selector,   # 如 "app=nginx,env=prod"
    )
    return resp.items


# 列出所有命名空间
def list_all_pods(field_selector: str = "") -> list:
    """列出所有命名空间的 Pod。"""
    resp = v1.list_pod_for_all_namespaces(field_selector=field_selector)
    return resp.items


# 使用示例
pods = list_pods("kube-system")
for pod in pods:
    phase = pod.status.phase
    node = pod.spec.node_name
    print(f"  {pod.metadata.name:<50} {phase:<12} {node}")


# ── 获取单个 Pod ──────────────────────────────────────────────────────────────
def get_pod(name: str, namespace: str = "default"):
    try:
        return v1.read_namespaced_pod(name=name, namespace=namespace)
    except ApiException as e:
        if e.status == 404:
            return None
        raise


# ── Pod 状态分析 ──────────────────────────────────────────────────────────────
def get_pod_status_summary(pod) -> dict:
    """提取 Pod 关键状态信息。"""
    meta = pod.metadata
    spec = pod.spec
    status = pod.status

    # 容器重启次数
    restart_counts = []
    container_statuses = status.container_statuses or []
    for cs in container_statuses:
        restart_counts.append({
            "name": cs.name,
            "restarts": cs.restart_count,
            "ready": cs.ready,
            "state": list(cs.state.to_dict().keys())[0] if cs.state else "unknown",
        })

    return {
        "name": meta.name,
        "namespace": meta.namespace,
        "phase": status.phase,
        "node": spec.node_name,
        "pod_ip": status.pod_ip,
        "start_time": meta.creation_timestamp,
        "containers": restart_counts,
        "conditions": [
            {"type": c.type, "status": c.status}
            for c in (status.conditions or [])
        ],
    }


# ── 删除 Pod ──────────────────────────────────────────────────────────────────
def delete_pod(name: str, namespace: str = "default", grace_period: int = 0) -> bool:
    try:
        v1.delete_namespaced_pod(
            name=name,
            namespace=namespace,
            grace_period_seconds=grace_period,
        )
        print(f"已删除 Pod: {namespace}/{name}")
        return True
    except ApiException as e:
        print(f"删除 Pod 失败: {e.status} {e.reason}")
        return False


# ── 在 Pod 中执行命令（exec）─────────────────────────────────────────────────
from kubernetes.stream import stream


def exec_in_pod(
    pod_name: str,
    namespace: str,
    command: list[str],
    container: str | None = None,
    timeout: int = 30,
) -> tuple[str, str]:
    """
    在 Pod 中执行命令，返回 (stdout, stderr)。

    示例:
        out, err = exec_in_pod("nginx-abc123", "default", ["nginx", "-t"])
    """
    kwargs = dict(
        name=pod_name,
        namespace=namespace,
        command=command,
        stderr=True,
        stdin=False,
        stdout=True,
        tty=False,
        _preload_content=False,
    )
    if container:
        kwargs["container"] = container

    resp = stream(v1.connect_get_namespaced_pod_exec, **kwargs)
    resp.run_forever(timeout=timeout)

    stdout = resp.read_stdout(timeout=5) or ""
    stderr = resp.read_stderr(timeout=5) or ""
    return stdout, stderr


# ── 获取 Pod 日志 ─────────────────────────────────────────────────────────────
def get_pod_logs(
    pod_name: str,
    namespace: str = "default",
    container: str | None = None,
    tail_lines: int = 100,
    previous: bool = False,
) -> str:
    """获取 Pod 日志。previous=True 获取上一次容器的日志（崩溃调试用）。"""
    try:
        return v1.read_namespaced_pod_log(
            name=pod_name,
            namespace=namespace,
            container=container,
            tail_lines=tail_lines,
            previous=previous,
        )
    except ApiException as e:
        return f"获取日志失败: {e.reason}"
```

## AppsV1Api：Deployment 操作

```python
from kubernetes import client, config

config.load_kube_config()
apps_v1 = client.AppsV1Api()


# ── 列出 Deployment ───────────────────────────────────────────────────────────
def list_deployments(namespace: str = "default") -> list:
    resp = apps_v1.list_namespaced_deployment(namespace=namespace)
    return resp.items


# 全命名空间
def list_all_deployments() -> list:
    resp = apps_v1.list_deployment_for_all_namespaces()
    return resp.items


# ── 修改副本数 ────────────────────────────────────────────────────────────────
def scale_deployment(name: str, namespace: str, replicas: int) -> bool:
    """扩缩容 Deployment。"""
    try:
        body = {"spec": {"replicas": replicas}}
        apps_v1.patch_namespaced_deployment_scale(
            name=name,
            namespace=namespace,
            body=body,
        )
        print(f"已将 {namespace}/{name} 副本数调整为 {replicas}")
        return True
    except client.ApiException as e:
        print(f"扩缩容失败: {e.reason}")
        return False


# ── Rollout Restart（触发滚动重启）────────────────────────────────────────────
import datetime


def rollout_restart(name: str, namespace: str = "default") -> bool:
    """
    触发 Deployment 滚动重启（等价于 kubectl rollout restart deployment/xxx）。
    原理：给 spec.template.metadata.annotations 加一个时间戳 annotation。
    """
    now = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    body = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {
                        "kubectl.kubernetes.io/restartedAt": now,
                    }
                }
            }
        }
    }
    try:
        apps_v1.patch_namespaced_deployment(name=name, namespace=namespace, body=body)
        print(f"已触发 {namespace}/{name} 滚动重启")
        return True
    except client.ApiException as e:
        print(f"触发重启失败: {e.reason}")
        return False


# ── 更新镜像 ──────────────────────────────────────────────────────────────────
def update_image(
    deployment_name: str,
    namespace: str,
    container_name: str,
    new_image: str,
) -> bool:
    """更新指定容器的镜像。"""
    try:
        deploy = apps_v1.read_namespaced_deployment(deployment_name, namespace)
        for container in deploy.spec.template.spec.containers:
            if container.name == container_name:
                container.image = new_image
                break
        else:
            print(f"找不到容器: {container_name}")
            return False

        apps_v1.replace_namespaced_deployment(deployment_name, namespace, deploy)
        print(f"已更新 {namespace}/{deployment_name}/{container_name} -> {new_image}")
        return True
    except client.ApiException as e:
        print(f"更新镜像失败: {e.reason}")
        return False


# ── 获取 Deployment 状态 ──────────────────────────────────────────────────────
def get_deployment_status(name: str, namespace: str) -> dict:
    deploy = apps_v1.read_namespaced_deployment(name, namespace)
    status = deploy.status
    return {
        "name": name,
        "namespace": namespace,
        "desired": deploy.spec.replicas,
        "ready": status.ready_replicas or 0,
        "available": status.available_replicas or 0,
        "updated": status.updated_replicas or 0,
        "unavailable": status.unavailable_replicas or 0,
    }
```

## 自定义资源（CustomObjectsApi）

```python
from kubernetes import client, config

config.load_kube_config()
custom_api = client.CustomObjectsApi()

GROUP = "networking.istio.io"
VERSION = "v1alpha3"
PLURAL = "virtualservices"


# ── 列出 CRD 资源 ─────────────────────────────────────────────────────────────
def list_crd_resources(
    group: str,
    version: str,
    plural: str,
    namespace: str | None = None,
) -> list[dict]:
    if namespace:
        resp = custom_api.list_namespaced_custom_object(
            group=group, version=version, plural=plural, namespace=namespace
        )
    else:
        resp = custom_api.list_cluster_custom_object(
            group=group, version=version, plural=plural
        )
    return resp.get("items", [])


# ── 获取单个 CRD 资源 ─────────────────────────────────────────────────────────
def get_crd_resource(
    group: str, version: str, plural: str,
    name: str, namespace: str,
) -> dict | None:
    try:
        return custom_api.get_namespaced_custom_object(
            group=group, version=version, plural=plural,
            namespace=namespace, name=name,
        )
    except client.ApiException as e:
        if e.status == 404:
            return None
        raise


# ── 创建/更新 CRD 资源 ────────────────────────────────────────────────────────
def apply_crd_resource(
    group: str, version: str, plural: str,
    namespace: str, body: dict,
) -> dict:
    """Create or Replace（简单的 apply 实现）。"""
    name = body["metadata"]["name"]
    existing = get_crd_resource(group, version, plural, name, namespace)

    if existing:
        body["metadata"]["resourceVersion"] = existing["metadata"]["resourceVersion"]
        return custom_api.replace_namespaced_custom_object(
            group=group, version=version, plural=plural,
            namespace=namespace, name=name, body=body,
        )
    else:
        return custom_api.create_namespaced_custom_object(
            group=group, version=version, plural=plural,
            namespace=namespace, body=body,
        )


# 示例：操作 HPA（autoscaling/v2）
HPA_GROUP = "autoscaling"
HPA_VERSION = "v2"
HPA_PLURAL = "horizontalpodautoscalers"


def get_hpa_status(name: str, namespace: str) -> dict:
    v2 = client.AutoscalingV2Api()
    hpa = v2.read_namespaced_horizontal_pod_autoscaler(name, namespace)
    return {
        "name": name,
        "min_replicas": hpa.spec.min_replicas,
        "max_replicas": hpa.spec.max_replicas,
        "current_replicas": hpa.status.current_replicas,
        "desired_replicas": hpa.status.desired_replicas,
    }
```

## Watch 机制：监听资源变化

```python
from kubernetes import client, config, watch

config.load_kube_config()
v1 = client.CoreV1Api()


def watch_pods(namespace: str = "default", timeout_seconds: int = 60) -> None:
    """监听 Pod 事件（ADDED/MODIFIED/DELETED）。"""
    w = watch.Watch()
    print(f"开始监听 {namespace} 命名空间的 Pod 事件...")

    try:
        for event in w.stream(
            v1.list_namespaced_pod,
            namespace=namespace,
            timeout_seconds=timeout_seconds,
        ):
            event_type = event["type"]           # ADDED / MODIFIED / DELETED
            pod = event["object"]
            name = pod.metadata.name
            phase = pod.status.phase

            print(f"[{event_type}] Pod: {name}  Phase: {phase}")

            # 响应事件
            if event_type == "ADDED" and phase == "Pending":
                print(f"  新 Pending Pod: {name}")
            elif event_type == "MODIFIED" and phase == "Failed":
                print(f"  Pod 失败: {name}")
    except Exception as e:
        print(f"Watch 中断: {e}")
    finally:
        w.stop()


# ── 带重连的 Watch（生产可用）────────────────────────────────────────────────
import time
import logging

logger = logging.getLogger(__name__)


def watch_with_reconnect(
    list_func,
    event_handler,
    namespace: str | None = None,
    label_selector: str = "",
    reconnect_delay: float = 5.0,
) -> None:
    """带自动重连的 Watch，适合长期运行的控制器。"""
    resource_version = ""
    w = watch.Watch()

    while True:
        try:
            kwargs = {
                "timeout_seconds": 300,
                "resource_version": resource_version,
                "label_selector": label_selector,
            }
            if namespace:
                kwargs["namespace"] = namespace
                stream_iter = w.stream(list_func, **kwargs)
            else:
                stream_iter = w.stream(list_func, **kwargs)

            for event in stream_iter:
                obj = event["object"]
                resource_version = obj.metadata.resource_version
                event_handler(event["type"], obj)

        except client.ApiException as e:
            if e.status == 410:   # Gone，resource_version 过期
                resource_version = ""
                logger.warning("resource_version 过期，重新全量 List")
            else:
                logger.error(f"ApiException: {e}")
                time.sleep(reconnect_delay)
        except Exception as e:
            logger.error(f"Watch 异常: {e}，{reconnect_delay}s 后重连")
            time.sleep(reconnect_delay)
```

## 实战：K8s 巡检脚本

检查所有命名空间的 Pending Pod 和频繁重启的容器，输出报告，支持钉钉告警。

```python
#!/usr/bin/env python3
"""
k8s_inspector.py — Kubernetes 集群巡检脚本

功能:
  1. 检查所有命名空间下的 Pending Pod（超过指定时间）
  2. 检查频繁重启的容器（重启次数超过阈值）
  3. 检查 Deployment 不可用副本
  4. 输出格式化报告
  5. 可选：通过 Webhook 发送告警

用法:
    python k8s_inspector.py
    python k8s_inspector.py --context prod-cluster --pending-minutes 10
    python k8s_inspector.py --restart-threshold 5 --webhook https://oapi.dingtalk.com/...
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any

import requests
from kubernetes import client, config
from kubernetes.client.rest import ApiException

# ── 日志 ──────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ── 数据结构 ──────────────────────────────────────────────────────────────────
@dataclass
class PendingPodIssue:
    namespace: str
    pod_name: str
    node: str
    pending_minutes: float
    reason: str


@dataclass
class RestartIssue:
    namespace: str
    pod_name: str
    container_name: str
    restart_count: int
    last_state: str


@dataclass
class DeploymentIssue:
    namespace: str
    deployment_name: str
    desired: int
    available: int
    unavailable: int


@dataclass
class InspectionReport:
    cluster_context: str
    checked_at: str
    pending_pods: list[PendingPodIssue] = field(default_factory=list)
    restart_issues: list[RestartIssue] = field(default_factory=list)
    deployment_issues: list[DeploymentIssue] = field(default_factory=list)

    @property
    def has_issues(self) -> bool:
        return bool(self.pending_pods or self.restart_issues or self.deployment_issues)

    @property
    def total_issues(self) -> int:
        return len(self.pending_pods) + len(self.restart_issues) + len(self.deployment_issues)


# ── 巡检逻辑 ──────────────────────────────────────────────────────────────────
def check_pending_pods(
    v1: client.CoreV1Api,
    pending_threshold_minutes: int = 5,
) -> list[PendingPodIssue]:
    """找出所有 Pending 超过阈值的 Pod。"""
    issues = []
    now = datetime.now(timezone.utc)

    try:
        pods = v1.list_pod_for_all_namespaces(
            field_selector="status.phase=Pending"
        )
    except ApiException as e:
        logger.error(f"列出 Pending Pod 失败: {e}")
        return issues

    for pod in pods.items:
        meta = pod.metadata
        status = pod.status

        created_at = meta.creation_timestamp
        if created_at is None:
            continue

        pending_seconds = (now - created_at).total_seconds()
        pending_minutes = pending_seconds / 60

        if pending_minutes < pending_threshold_minutes:
            continue

        # 提取 Pending 原因
        reason = "Unknown"
        for condition in (status.conditions or []):
            if condition.type == "PodScheduled" and condition.status != "True":
                reason = condition.reason or condition.message or "Unschedulable"
                break
            elif condition.type == "ContainersReady" and condition.status != "True":
                reason = condition.reason or "ContainersNotReady"

        issues.append(PendingPodIssue(
            namespace=meta.namespace,
            pod_name=meta.name,
            node=pod.spec.node_name or "未调度",
            pending_minutes=round(pending_minutes, 1),
            reason=reason,
        ))

    return issues


def check_restart_issues(
    v1: client.CoreV1Api,
    restart_threshold: int = 10,
) -> list[RestartIssue]:
    """找出重启次数超过阈值的容器。"""
    issues = []

    try:
        pods = v1.list_pod_for_all_namespaces()
    except ApiException as e:
        logger.error(f"列出所有 Pod 失败: {e}")
        return issues

    for pod in pods.items:
        meta = pod.metadata
        status = pod.status

        for cs in (status.container_statuses or []):
            if cs.restart_count < restart_threshold:
                continue

            # 获取上次退出状态
            last_state = "unknown"
            if cs.last_state and cs.last_state.terminated:
                t = cs.last_state.terminated
                last_state = f"exit={t.exit_code} reason={t.reason or 'unknown'}"

            issues.append(RestartIssue(
                namespace=meta.namespace,
                pod_name=meta.name,
                container_name=cs.name,
                restart_count=cs.restart_count,
                last_state=last_state,
            ))

    # 按重启次数降序
    return sorted(issues, key=lambda x: x.restart_count, reverse=True)


def check_deployment_issues(
    apps_v1: client.AppsV1Api,
) -> list[DeploymentIssue]:
    """找出有不可用副本的 Deployment。"""
    issues = []

    try:
        deploys = apps_v1.list_deployment_for_all_namespaces()
    except ApiException as e:
        logger.error(f"列出 Deployment 失败: {e}")
        return issues

    for deploy in deploys.items:
        meta = deploy.metadata
        status = deploy.status
        spec = deploy.spec

        desired = spec.replicas or 0
        available = status.available_replicas or 0
        unavailable = status.unavailable_replicas or 0

        if unavailable > 0 or available < desired:
            issues.append(DeploymentIssue(
                namespace=meta.namespace,
                deployment_name=meta.name,
                desired=desired,
                available=available,
                unavailable=unavailable,
            ))

    return issues


# ── 报告输出 ──────────────────────────────────────────────────────────────────
def format_report(report: InspectionReport) -> str:
    lines = []
    sep = "=" * 70

    lines.append(sep)
    lines.append(f"  K8s 巡检报告")
    lines.append(f"  集群: {report.cluster_context}")
    lines.append(f"  时间: {report.checked_at}")
    lines.append(f"  问题总计: {report.total_issues}")
    lines.append(sep)

    # Pending Pod
    lines.append(f"\n【Pending Pod】共 {len(report.pending_pods)} 个")
    if report.pending_pods:
        lines.append(f"  {'命名空间':<20} {'Pod 名称':<40} {'等待时长':>10}  {'原因'}")
        lines.append("  " + "-" * 66)
        for issue in report.pending_pods:
            lines.append(
                f"  {issue.namespace:<20} {issue.pod_name:<40} "
                f"{issue.pending_minutes:>8.1f}m  {issue.reason}"
            )
    else:
        lines.append("  无异常")

    # 重启问题
    lines.append(f"\n【频繁重启容器】共 {len(report.restart_issues)} 个（阈值已在参数中设定）")
    if report.restart_issues:
        lines.append(f"  {'命名空间':<20} {'Pod':<35} {'容器':<20} {'重启次数':>8}  {'上次状态'}")
        lines.append("  " + "-" * 80)
        for issue in report.restart_issues:
            lines.append(
                f"  {issue.namespace:<20} {issue.pod_name:<35} "
                f"{issue.container_name:<20} {issue.restart_count:>8}  {issue.last_state}"
            )
    else:
        lines.append("  无异常")

    # Deployment 问题
    lines.append(f"\n【Deployment 异常】共 {len(report.deployment_issues)} 个")
    if report.deployment_issues:
        lines.append(f"  {'命名空间':<20} {'Deployment':<40} {'期望':>6} {'可用':>6} {'不可用':>8}")
        lines.append("  " + "-" * 66)
        for issue in report.deployment_issues:
            lines.append(
                f"  {issue.namespace:<20} {issue.deployment_name:<40} "
                f"{issue.desired:>6} {issue.available:>6} {issue.unavailable:>8}"
            )
    else:
        lines.append("  无异常")

    lines.append("\n" + sep)
    return "\n".join(lines)


# ── 钉钉告警 ──────────────────────────────────────────────────────────────────
def send_webhook_alert(webhook_url: str, report: InspectionReport) -> None:
    if not report.has_issues:
        return

    lines = [f"**K8s 巡检告警** | 集群: {report.cluster_context}", ""]

    if report.pending_pods:
        lines.append(f"> **Pending Pod**: {len(report.pending_pods)} 个")
        for p in report.pending_pods[:5]:   # 只显示前5个
            lines.append(f"> - `{p.namespace}/{p.pod_name}` 等待 {p.pending_minutes:.0f}分钟，原因: {p.reason}")
        if len(report.pending_pods) > 5:
            lines.append(f"> - ... 还有 {len(report.pending_pods) - 5} 个")

    if report.restart_issues:
        lines.append(f"\n> **频繁重启**: {len(report.restart_issues)} 个容器")
        for r in report.restart_issues[:5]:
            lines.append(f"> - `{r.namespace}/{r.pod_name}/{r.container_name}` 重启 {r.restart_count} 次")

    if report.deployment_issues:
        lines.append(f"\n> **Deployment 异常**: {len(report.deployment_issues)} 个")
        for d in report.deployment_issues[:5]:
            lines.append(f"> - `{d.namespace}/{d.deployment_name}` 期望 {d.desired} 实际 {d.available}")

    content = "\n".join(lines)
    payload = {
        "msgtype": "markdown",
        "markdown": {"title": f"K8s 巡检告警 - {report.total_issues} 个问题", "text": content},
    }

    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        resp.raise_for_status()
        logger.info("告警已发送")
    except Exception as e:
        logger.error(f"发送告警失败: {e}")


# ── 主逻辑 ────────────────────────────────────────────────────────────────────
def run_inspection(
    context: str | None,
    pending_threshold: int,
    restart_threshold: int,
    webhook: str | None,
    output_json: str | None,
) -> InspectionReport:
    # 初始化认证
    try:
        config.load_incluster_config()
        ctx = "in-cluster"
    except config.config_exception.ConfigException:
        config.load_kube_config(context=context)
        contexts, active = config.list_kube_config_contexts()
        ctx = (active or {}).get("name", context or "unknown")

    logger.info(f"连接集群: {ctx}")

    v1 = client.CoreV1Api()
    apps_v1 = client.AppsV1Api()

    report = InspectionReport(
        cluster_context=ctx,
        checked_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )

    logger.info("检查 Pending Pod...")
    report.pending_pods = check_pending_pods(v1, pending_threshold)

    logger.info("检查容器重启次数...")
    report.restart_issues = check_restart_issues(v1, restart_threshold)

    logger.info("检查 Deployment 状态...")
    report.deployment_issues = check_deployment_issues(apps_v1)

    return report


# ── 入口 ──────────────────────────────────────────────────────────────────────
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="K8s 集群巡检工具")
    parser.add_argument("--context", help="kubectl context 名称（默认当前 context）")
    parser.add_argument(
        "--pending-minutes", type=int, default=5, metavar="N",
        help="Pending 超过 N 分钟才告警（默认 5）",
    )
    parser.add_argument(
        "--restart-threshold", type=int, default=10, metavar="N",
        help="容器重启次数超过 N 才告警（默认 10）",
    )
    parser.add_argument("--webhook", metavar="URL", help="告警 Webhook URL（钉钉/企微）")
    parser.add_argument("--output-json", metavar="FILE", help="将报告保存为 JSON")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    report = run_inspection(
        context=args.context,
        pending_threshold=args.pending_minutes,
        restart_threshold=args.restart_threshold,
        webhook=args.webhook,
        output_json=args.output_json,
    )

    # 打印报告
    print(format_report(report))

    # 发送告警
    if args.webhook and report.has_issues:
        send_webhook_alert(args.webhook, report)

    # 输出 JSON
    if args.output_json:
        import json as _json
        from pathlib import Path
        out = {
            "cluster": report.cluster_context,
            "checked_at": report.checked_at,
            "total_issues": report.total_issues,
            "pending_pods": [asdict(p) for p in report.pending_pods],
            "restart_issues": [asdict(r) for r in report.restart_issues],
            "deployment_issues": [asdict(d) for d in report.deployment_issues],
        }
        Path(args.output_json).write_text(
            _json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        logger.info(f"JSON 报告已写入: {args.output_json}")

    # 有问题返回非零退出码（CI/监控触发用）
    return 1 if report.has_issues else 0


if __name__ == "__main__":
    sys.exit(main())
```

### 运行示例

```bash
# 使用当前 kubeconfig context 巡检
python k8s_inspector.py

# 指定 context，调整告警阈值
python k8s_inspector.py --context prod-us-west --pending-minutes 10 --restart-threshold 5

# 输出 JSON + 发送钉钉告警
python k8s_inspector.py \
  --context prod-cluster \
  --webhook "https://oapi.dingtalk.com/robot/send?access_token=xxx" \
  --output-json /tmp/k8s-report.json
```

### 所需权限（RBAC）

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: k8s-inspector
rules:
  - apiGroups: [""]
    resources: ["pods", "pods/log", "nodes", "namespaces"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["apps"]
    resources: ["deployments", "replicasets", "statefulsets", "daemonsets"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["autoscaling"]
    resources: ["horizontalpodautoscalers"]
    verbs: ["get", "list"]
```
