---
title: "Grafana API 自动化：用代码管理 Dashboard、数据源和告警"
date: 2025-03-18T11:26:00+08:00
draft: false
tags: ["Grafana", "API", "自动化", "可观测性", "运维"]
categories: ["可观测性"]
description: "从 IaC 角度系统讲解 Grafana HTTP API 的使用方式，包括 Service Account 认证、Dashboard 批量导出导入、数据源管理和告警规则 provisioning，含完整 Python 脚本和踩坑记录。"
summary: "手动点 UI 管理 Grafana Dashboard 在多环境场景下是噩梦。用 API 把 Dashboard 代码化，实现版本控制和环境同步，才是正确姿势。本文提供完整的 Python 工具脚本和实战踩坑。"
toc: true
math: false
diagram: false
series: ["可观测性实战"]
keywords: ["Grafana API", "Dashboard as Code", "Service Account", "Grafana provisioning", "可观测性自动化"]
params:
  reading_time: true
---

## 为什么要用 API 管理 Grafana

刚开始用 Grafana 时，大家都是直接在 UI 上拖拖拽拽创建 Dashboard，方便快捷。但随着环境越来越多（QA、预发、生产，再加上多区域），问题开始暴露：

- 生产上精心调好的 Dashboard，想同步到 QA 得手动导出 JSON 再导入，还经常忘记
- 新来的同事不小心改了生产 Dashboard，没有回滚机制，恢复不了历史状态
- 想审计"这个 Dashboard 是什么时候被谁改的"，完全没有记录
- 批量修改数据源地址（比如迁移 Prometheus 集群），得一个个手动改

这些问题的根源是：**把 Grafana 当做手动操作的 UI 工具，而不是一个有 API 的服务**。

把 Dashboard 代码化（Dashboard as Code）解决了这些问题：Dashboard JSON 存 Git，通过 API 或 provisioning 部署，多环境同步变成 CI/CD pipeline 的一个步骤。这就是 IaC（Infrastructure as Code）思路在可观测性领域的应用。

## Service Account Token 认证

Grafana 8.x 以后推荐用 Service Account 替代旧版 API Key，权限管理更精细，Token 可以设置过期时间。

在 Grafana UI 中：Administration → Service Accounts → Add service account

```bash
# 用 API 创建 Service Account（需要 Admin token 或 Basic Auth）
curl -s -X POST http://admin:password@grafana.example.com/api/serviceaccounts \
  -H "Content-Type: application/json" \
  -d '{
    "name": "automation-bot",
    "role": "Admin",
    "isDisabled": false
  }'

# 响应包含 service account id，记录下来
# {"id": 1, "name": "automation-bot", ...}

# 为 Service Account 创建 Token
curl -s -X POST http://admin:password@grafana.example.com/api/serviceaccounts/1/tokens \
  -H "Content-Type: application/json" \
  -d '{
    "name": "ci-token",
    "secondsToLive": 7776000
  }'
# 响应中的 key 字段是 Token，只显示一次，务必保存
```

后续所有 API 调用都在 Header 里带上 Token：

```bash
curl -H "Authorization: Bearer glsa_xxxxxxxxxxxx" \
  http://grafana.example.com/api/dashboards/home
```

## 常用 API 速览

### Dashboard API

```bash
# 搜索所有 Dashboard（返回列表，包含 uid、folder 等信息）
GET /api/search?type=dash-db

# 获取指定 Dashboard（通过 uid）
GET /api/dashboards/uid/<uid>

# 创建或更新 Dashboard
POST /api/dashboards/db
Body: {
  "dashboard": { ...dashboard json... },
  "folderUid": "abc123",
  "overwrite": true,
  "message": "Update via CI"
}

# 删除 Dashboard
DELETE /api/dashboards/uid/<uid>
```

### Folder API

```bash
# 列出所有 Folder
GET /api/folders

# 创建 Folder
POST /api/folders
Body: { "title": "Platform Team", "uid": "platform-team" }
```

### 数据源 API

```bash
# 列出所有数据源
GET /api/datasources

# 创建数据源
POST /api/datasources

# 更新数据源
PUT /api/datasources/<id>

# 删除数据源
DELETE /api/datasources/<id>
```

### 用户管理 API

```bash
# 列出所有用户
GET /api/users

# 邀请用户加入 Org
POST /api/org/invites
Body: { "email": "user@example.com", "role": "Viewer" }
```

## Python 脚本：批量导出所有 Dashboard

这个脚本把 Grafana 中所有 Dashboard 按 Folder 导出为 JSON 文件，方便做版本控制：

```python
#!/usr/bin/env python3
"""
grafana_export.py - 批量导出 Grafana Dashboard 到本地文件
"""
import json
import os
import sys
import re
import requests
from pathlib import Path

class GrafanaExporter:
    def __init__(self, base_url: str, token: str, output_dir: str = "grafana-dashboards"):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        })
        self.output_dir = Path(output_dir)

    def _get(self, path: str, params: dict = None) -> dict | list:
        resp = self.session.get(f"{self.base_url}{path}", params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _sanitize_name(self, name: str) -> str:
        """把 Dashboard 名称转换为合法的文件名"""
        name = re.sub(r'[^\w\s\-]', '', name)
        name = re.sub(r'\s+', '-', name.strip())
        return name.lower()

    def get_all_folders(self) -> list[dict]:
        """获取所有 Folder 列表（包含默认 General）"""
        folders = self._get("/api/folders")
        # 加入 General（Folder ID = 0，无 uid）
        folders.append({"uid": "general", "title": "General"})
        return folders

    def get_dashboards_in_folder(self, folder_uid: str) -> list[dict]:
        """获取指定 Folder 中的所有 Dashboard"""
        if folder_uid == "general":
            # General folder 用 folderIds=0 查询
            results = self._get("/api/search", params={"type": "dash-db", "folderIds": "0"})
        else:
            results = self._get("/api/search", params={"type": "dash-db", "folderUid": folder_uid})
        return results

    def get_dashboard(self, uid: str) -> dict:
        """获取 Dashboard 完整 JSON"""
        result = self._get(f"/api/dashboards/uid/{uid}")
        return result

    def export_all(self):
        """导出所有 Dashboard，按 Folder 分目录保存"""
        self.output_dir.mkdir(parents=True, exist_ok=True)

        folders = self.get_all_folders()
        total_count = 0

        for folder in folders:
            folder_uid = folder["uid"]
            folder_title = folder["title"]
            folder_dir = self.output_dir / self._sanitize_name(folder_title)

            dashboards = self.get_dashboards_in_folder(folder_uid)
            if not dashboards:
                continue

            folder_dir.mkdir(exist_ok=True)

            # 保存 folder 元数据
            with open(folder_dir / "_folder.json", "w") as f:
                json.dump({"uid": folder_uid, "title": folder_title}, f, indent=2)

            for db_meta in dashboards:
                uid = db_meta["uid"]
                title = db_meta["title"]

                try:
                    db_data = self.get_dashboard(uid)
                    dashboard_json = db_data["dashboard"]
                    
                    # 清理不应该跨环境同步的字段
                    dashboard_json.pop("id", None)         # 数字 ID 各环境不同
                    dashboard_json.pop("version", None)    # 版本号各环境不同

                    filename = folder_dir / f"{self._sanitize_name(title)}.json"
                    with open(filename, "w", encoding="utf-8") as f:
                        json.dump(dashboard_json, f, indent=2, ensure_ascii=False)

                    print(f"  [OK] {folder_title}/{title} -> {filename}")
                    total_count += 1

                except Exception as e:
                    print(f"  [ERROR] Failed to export {title} ({uid}): {e}", file=sys.stderr)

        print(f"\nExported {total_count} dashboards to {self.output_dir}")


def main():
    base_url = os.environ.get("GRAFANA_URL", "http://localhost:3000")
    token = os.environ.get("GRAFANA_TOKEN", "")

    if not token:
        print("Error: GRAFANA_TOKEN environment variable is required", file=sys.stderr)
        sys.exit(1)

    exporter = GrafanaExporter(base_url, token)
    exporter.export_all()


if __name__ == "__main__":
    main()
```

使用方式：

```bash
export GRAFANA_URL="https://grafana.example.com"
export GRAFANA_TOKEN="glsa_xxxxxxxxxxxx"

python3 grafana_export.py

# 导出结果目录结构：
# grafana-dashboards/
# ├── general/
# │   ├── _folder.json
# │   └── kubernetes-overview.json
# ├── platform-team/
# │   ├── _folder.json
# │   ├── service-latency.json
# │   └── error-rate.json
# └── infrastructure/
#     ├── _folder.json
#     └── node-exporter-full.json
```

## Python 脚本：批量导入 Dashboard（新环境初始化）

```python
#!/usr/bin/env python3
"""
grafana_import.py - 从文件批量导入 Dashboard 到 Grafana
用于新环境初始化，或从 Git 同步最新 Dashboard
"""
import json
import os
import sys
import requests
from pathlib import Path


class GrafanaImporter:
    def __init__(self, base_url: str, token: str, datasource_mapping: dict = None):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        })
        # datasource_mapping: 源环境 datasource uid -> 目标环境 datasource uid
        # 解决跨环境 datasource uid 不一致问题（后面的踩坑会详细解释）
        self.datasource_mapping = datasource_mapping or {}

    def _post(self, path: str, data: dict) -> dict:
        resp = self.session.post(f"{self.base_url}{path}", json=data, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _put(self, path: str, data: dict) -> dict:
        resp = self.session.put(f"{self.base_url}{path}", json=data, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def ensure_folder(self, uid: str, title: str) -> str:
        """确保 Folder 存在，不存在则创建，返回 folder uid"""
        if uid == "general":
            return None

        try:
            resp = self.session.get(f"{self.base_url}/api/folders/{uid}", timeout=10)
            if resp.status_code == 200:
                return uid
        except Exception:
            pass

        # Folder 不存在，创建它
        result = self._post("/api/folders", {"uid": uid, "title": title})
        print(f"  [Folder] Created: {title} (uid={uid})")
        return result["uid"]

    def remap_datasources(self, dashboard_json: dict) -> dict:
        """替换 Dashboard 中的 datasource uid，适配目标环境"""
        if not self.datasource_mapping:
            return dashboard_json

        dashboard_str = json.dumps(dashboard_json)
        for src_uid, dst_uid in self.datasource_mapping.items():
            dashboard_str = dashboard_str.replace(
                f'"uid": "{src_uid}"',
                f'"uid": "{dst_uid}"'
            )
        return json.loads(dashboard_str)

    def import_dashboard(self, dashboard_json: dict, folder_uid: str = None, message: str = "") -> bool:
        """导入单个 Dashboard"""
        # 重新映射 datasource
        dashboard_json = self.remap_datasources(dashboard_json)

        # 清除 id，让 Grafana 自动分配
        dashboard_json.pop("id", None)

        payload = {
            "dashboard": dashboard_json,
            "overwrite": True,
            "message": message or "Imported via API",
        }
        if folder_uid and folder_uid != "general":
            payload["folderUid"] = folder_uid

        result = self._post("/api/dashboards/db", payload)
        return result.get("status") == "success"

    def import_from_directory(self, source_dir: str, commit_msg: str = ""):
        """从导出目录批量导入"""
        source_path = Path(source_dir)
        if not source_path.exists():
            print(f"Error: directory {source_dir} does not exist", file=sys.stderr)
            sys.exit(1)

        success_count = 0
        error_count = 0

        # 遍历所有子目录（每个子目录是一个 Folder）
        for folder_dir in sorted(source_path.iterdir()):
            if not folder_dir.is_dir():
                continue

            # 读取 Folder 元数据
            folder_meta_file = folder_dir / "_folder.json"
            if not folder_meta_file.exists():
                continue

            with open(folder_meta_file) as f:
                folder_meta = json.load(f)

            folder_uid = folder_meta["uid"]
            folder_title = folder_meta["title"]

            # 确保 Folder 存在
            self.ensure_folder(folder_uid, folder_title)

            # 导入该 Folder 下的所有 Dashboard
            for json_file in sorted(folder_dir.glob("*.json")):
                if json_file.name.startswith("_"):
                    continue  # 跳过元数据文件

                with open(json_file, encoding="utf-8") as f:
                    dashboard_json = json.load(f)

                title = dashboard_json.get("title", json_file.stem)
                try:
                    ok = self.import_dashboard(
                        dashboard_json,
                        folder_uid=folder_uid,
                        message=commit_msg,
                    )
                    if ok:
                        print(f"  [OK] {folder_title}/{title}")
                        success_count += 1
                    else:
                        print(f"  [FAIL] {folder_title}/{title}: unexpected response", file=sys.stderr)
                        error_count += 1
                except requests.HTTPError as e:
                    print(f"  [ERROR] {folder_title}/{title}: {e.response.text}", file=sys.stderr)
                    error_count += 1

        print(f"\nImport complete: {success_count} success, {error_count} errors")
        return error_count == 0


def main():
    base_url = os.environ.get("GRAFANA_URL", "http://localhost:3000")
    token = os.environ.get("GRAFANA_TOKEN", "")
    source_dir = os.environ.get("DASHBOARD_DIR", "grafana-dashboards")

    # 从环境变量读取 datasource mapping（JSON 格式）
    # 例如：DATASOURCE_MAPPING='{"prod-prometheus-uid": "qa-prometheus-uid"}'
    mapping_str = os.environ.get("DATASOURCE_MAPPING", "{}")
    datasource_mapping = json.loads(mapping_str)

    if not token:
        print("Error: GRAFANA_TOKEN is required", file=sys.stderr)
        sys.exit(1)

    # 从 Git commit message 获取变更描述
    commit_msg = os.environ.get("CI_COMMIT_MESSAGE", "Sync via CI")

    importer = GrafanaImporter(base_url, token, datasource_mapping)
    success = importer.import_from_directory(source_dir, commit_msg)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
```

## 告警规则：Provisioning 方式

Grafana 8.x 以后的统一告警（Unified Alerting）有专门的 provisioning API，可以用 YAML 文件描述告警规则并通过 API 推送，比在 UI 里配置要可靠得多。

```bash
# 导出当前所有告警规则（YAML 格式）
curl -s -H "Authorization: Bearer ${GRAFANA_TOKEN}" \
  "http://grafana.example.com/api/v1/provisioning/alert-rules/export" \
  -o alert-rules-export.yaml
```

通过 API 创建告警规则组：

```bash
curl -s -X POST \
  -H "Authorization: Bearer ${GRAFANA_TOKEN}" \
  -H "Content-Type: application/yaml" \
  -H "X-Disable-Provenance: true" \
  --data-binary @alert-rules.yaml \
  "http://grafana.example.com/api/v1/provisioning/alert-rules"
```

告警规则 YAML 示例：

```yaml
# alert-rules.yaml
apiVersion: 1
groups:
  - orgId: 1
    name: platform-alerts
    folder: Platform Team
    interval: 1m
    rules:
      - uid: high-error-rate
        title: High Error Rate
        condition: C
        data:
          - refId: A
            relativeTimeRange:
              from: 300
              to: 0
            datasourceUid: prometheus-prod
            model:
              expr: sum(rate(http_requests_total{status=~"5.."}[5m])) / sum(rate(http_requests_total[5m])) > 0.05
              instant: true
              intervalMs: 1000
              maxDataPoints: 43200
              refId: A
          - refId: C
            datasourceUid: "-100"  # 固定值，表示 expression
            model:
              conditions:
                - evaluator:
                    params: [0]
                    type: gt
                  operator:
                    type: and
                  query:
                    params: [A]
                  reducer:
                    type: last
              refId: C
              type: classic_conditions
        noDataState: NoData
        execErrState: Error
        for: 5m
        labels:
          severity: critical
          team: platform
        annotations:
          summary: "Error rate > 5% for 5 minutes"
          runbook: "https://wiki.example.com/runbook/high-error-rate"
```

## CI/CD 集成示例

把 Dashboard 同步集成到 CI pipeline（以 GitLab CI 为例）：

```yaml
# .gitlab-ci.yml
stages:
  - export
  - sync

export-dashboards:
  stage: export
  image: python:3.11-slim
  script:
    - pip install requests
    - python3 scripts/grafana_export.py
  artifacts:
    paths:
      - grafana-dashboards/
  only:
    - schedules  # 每天定时触发，把最新 Dashboard 提交到 Git

sync-to-qa:
  stage: sync
  image: python:3.11-slim
  script:
    - pip install requests
    - python3 scripts/grafana_import.py
  variables:
    GRAFANA_URL: $QA_GRAFANA_URL
    GRAFANA_TOKEN: $QA_GRAFANA_TOKEN
    DASHBOARD_DIR: grafana-dashboards
    DATASOURCE_MAPPING: '{"$PROD_PROM_UID": "$QA_PROM_UID"}'
    CI_COMMIT_MESSAGE: "Sync from production: $CI_COMMIT_SHORT_SHA"
  only:
    - main
```

## 踩坑记录

### 坑 1：跨环境同步时 datasource uid 不一致

这是最常见、也最坑的问题。Grafana 中每个数据源都有一个 `uid`（字符串），Dashboard JSON 里的 panel 引用数据源时用的是这个 uid。

问题是：生产环境和 QA 环境的 Prometheus 数据源 uid 不一样（比如生产是 `xYz123`，QA 是 `abc456`），直接把生产的 Dashboard JSON 导入 QA，所有 panel 都会显示"datasource not found"。

解决方案：

1. **约定 uid**：在创建数据源时手动指定固定 uid，所有环境用相同的 uid

```bash
curl -s -X POST \
  -H "Authorization: Bearer ${GRAFANA_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Prometheus",
    "type": "prometheus",
    "uid": "prometheus-main",   # 各环境保持一致
    "url": "http://prometheus:9090",
    "access": "proxy"
  }' \
  "http://grafana.example.com/api/datasources"
```

2. **运行时替换**：如果无法约定 uid，在导入脚本中做字符串替换（前面的 `remap_datasources` 方法就是这个思路）

3. **使用 `${datasource}` 变量**：在 Dashboard 里用变量引用数据源，而不是硬编码 uid，这样跨环境时只需要修改变量默认值

### 坑 2：Dashboard 版本冲突导致覆盖失败

症状：导入时报错 `{"message":"A newer dashboard version already exists"}` 或导入后发现 Dashboard 被回滚。

原因：Grafana Dashboard JSON 里有 `version` 字段，每次通过 UI 编辑会自动 +1。`overwrite: true` 参数只允许覆盖相同或更低版本，如果目标环境的 Dashboard 版本比你导入的 JSON 版本更高，导入会失败或静默失败。

解决：在导出时清除 `version` 字段（前面导出脚本已经处理了），导入时 Grafana 会自动处理版本号。同时确保 `overwrite: true` 和 `message` 字段都设置了。

```python
# 导出时必须清除这两个字段
dashboard_json.pop("id", None)
dashboard_json.pop("version", None)
```

### 坑 3：Folder 的 uid 不稳定

老版本 Grafana（< 9.0）的 Folder API 用数字 ID，没有 uid。直接用数字 ID 跨环境同步会对不上。

升级到 Grafana 9.x 后，Folder 才有了稳定的 uid。如果还在用旧版本，暂时的解决方案是约定 Folder 名字相同，导入时按名字查找 Folder ID 再传给 Dashboard。

### 坑 4：API Token 权限不足导致静默失败

症状：导入脚本返回成功，但 Dashboard 实际没有出现在 Grafana 里，或者某些字段没更新。

Grafana 的 RBAC 比较复杂，Viewer 角色可以调用某些 GET API，但 POST 请求会返回 200 却什么都不做（而不是 403）。

解决：为自动化脚本使用的 Service Account 分配 **Admin** 角色，确保有足够权限。在 CI 环境下，用专门的 `ci-admin` Service Account，与普通只读 Token 分开管理。

验证权限：

```bash
# 调用 /api/user 确认当前 token 的身份和角色
curl -s -H "Authorization: Bearer ${GRAFANA_TOKEN}" \
  http://grafana.example.com/api/user | jq '.login, .orgRole'
```
