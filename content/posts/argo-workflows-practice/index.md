---
title: "Argo Workflows 工作流实战：批处理与 ML Pipeline"
date: 2026-04-12T11:00:00+08:00
draft: false
tags: ["Argo Workflows", "Kubernetes", "ML Pipeline", "批处理", "DevOps", "GitOps", "Argo Events"]
categories: ["云原生"]
description: "从选型对比到生产落地，系统讲解 Argo Workflows 核心概念、DAG 并行数据处理、ML 训练 Pipeline、CronWorkflow 定时任务、参数化模板、资源管控与监控实践。"
summary: "Argo Workflows 是 Kubernetes 原生的工作流引擎，适合批处理和 ML Pipeline 场景。本文涵盖与 Airflow/Temporal 的选型对比、核心资源模型、三个完整实战（DAG 数据处理、ML 训练 Pipeline、定时备份）、资源管控（Semaphore/Node Selector）、Argo Events 事件驱动触发，以及 Prometheus 监控和常见问题处理。"
toc: true
math: false
diagram: false
keywords: ["Argo Workflows", "ML Pipeline", "DAG", "CronWorkflow", "Kubernetes", "Argo Events", "WorkflowTemplate", "批处理", "Semaphore", "artifact"]
params:
  reading_time: true
---

## 选型对比：Argo Workflows vs Airflow vs Prefect vs Temporal

在选择工作流引擎之前，先明确几个维度：执行单元是什么、调度模型是什么、与 Kubernetes 的集成深度如何。

| 维度 | Argo Workflows | Apache Airflow | Prefect | Temporal |
|------|---------------|----------------|---------|----------|
| 执行单元 | Kubernetes Pod | Python 进程/算子 | Python 进程/Task | Activity（进程级） |
| 调度模型 | 事件驱动 + Cron | DAG + Cron | Flow + Cron | Workflow + Signal |
| K8s 集成 | 原生（CRD） | 插件（K8s Executor） | 插件（K8s Work Pool） | 需要额外部署 |
| 语言耦合 | 无（容器即任务） | Python | Python | SDK 多语言 |
| 状态管理 | etcd（K8s） | 外部 DB（PostgreSQL） | 外部 DB + API Server | Cassandra/PostgreSQL |
| 长时间任务 | 弱（Pod 级） | 弱 | 弱 | 强（工作流可运行数月） |
| 适合场景 | 批处理/ML Pipeline/CI | 数据工程/ETL | 数据工程/MLOps | 业务流程编排/Saga |

**结论**：

- **Argo Workflows**：你的工作负载已经在 Kubernetes 上，任务天然容器化，需要 DAG 并行、资源隔离、Artifact 传递——首选。
- **Airflow**：数据工程团队以 Python 为主，需要大量内置算子（Spark、BigQuery、Snowflake）——Airflow 生态更成熟。
- **Temporal**：需要跨服务的长时间业务流程编排、精确的 at-least-once 语义、工作流需要 Signal/Query 交互——Temporal 更合适。
- **Prefect**：想要 Airflow 的易用性但不想维护调度器，接受 SaaS 模式——Prefect Cloud 是好选择。

---

## 核心概念

### 资源模型

```
Workflow                  # 一次具体的工作流执行实例
WorkflowTemplate          # 可复用的工作流模板
ClusterWorkflowTemplate   # 集群级模板（跨 namespace）
CronWorkflow              # 定时触发的工作流
```

**Template 类型**：

- `container`：运行单个容器（最常用）
- `script`：内联脚本（Python/Bash），适合轻量逻辑
- `dag`：有向无环图，定义任务间依赖
- `steps`：线性步骤列表（支持并行 step）
- `suspend`：暂停等待人工审批或外部信号
- `resource`：对 K8s 资源执行 create/apply/delete
- `http`：调用 HTTP 接口

### 执行流程

```
CronWorkflow/Webhook → Workflow（实例）
                         ↓
                    EntryPoint Template
                         ↓
                    DAG / Steps
                    ↙    ↓    ↘
                 Task-A  Task-B  Task-C（并行）
                    ↘    ↓    ↙
                      Task-D（依赖前三个）
```

每个 Task 对应一个 Pod，Pod 完成后 Argo 根据状态决定是否触发下游任务。

---

## 安装与 RBAC 配置

### 安装

```bash
# 安装 Argo Workflows（推荐指定版本）
kubectl create namespace argo
kubectl apply -n argo -f https://github.com/argoproj/argo-workflows/releases/download/v3.5.5/install.yaml

# 生产环境建议用 Helm
helm repo add argo https://argoproj.github.io/argo-helm
helm repo update

helm install argo-workflows argo/argo-workflows \
  --namespace argo \
  --create-namespace \
  --values values-production.yaml
```

`values-production.yaml` 关键配置：

```yaml
# values-production.yaml
server:
  extraArgs:
    - --auth-mode=server   # 生产环境用 SSO，开发用 server 模式
  ingress:
    enabled: true
    hosts:
      - argo.internal.yourorg.com
    annotations:
      nginx.ingress.kubernetes.io/auth-url: "https://sso.yourorg.com/oauth2/auth"

controller:
  workflowWorkers: 32       # 并发 workflow 数
  podWorkers: 32            # 并发 pod 处理数
  resourceRateLimit:
    limit: 20
    burst: 1
  persistence:
    connectionPool:
      maxIdleConns: 100
    nodeStatusOffLoad: true  # 节点状态卸载到对象存储，避免 etcd 压力

artifactRepository:
  s3:
    endpoint: s3.amazonaws.com
    bucket: yourorg-argo-artifacts
    region: us-west-2
    useSDKCreds: true  # 使用 IRSA，不硬编码 AK/SK

executor:
  resources:
    requests:
      cpu: 100m
      memory: 64Mi
    limits:
      cpu: 500m
      memory: 512Mi
```

### RBAC 配置

```yaml
# workflow-rbac.yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: workflow-sa
  namespace: ml-pipeline
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: workflow-role
  namespace: ml-pipeline
rules:
  # Argo Workflows controller 需要操作 Pod、ConfigMap、PVC
  - apiGroups: [""]
    resources: ["pods", "pods/log"]
    verbs: ["get", "list", "watch", "create", "delete", "patch"]
  - apiGroups: [""]
    resources: ["configmaps"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["argoproj.io"]
    resources: ["workflows", "workflowtemplates", "cronworkflows"]
    verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
  # 如果 workflow 需要操作其他 K8s 资源（如创建 Job、Deployment）
  - apiGroups: ["batch"]
    resources: ["jobs"]
    verbs: ["get", "create", "watch", "delete"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: workflow-rb
  namespace: ml-pipeline
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: workflow-role
subjects:
  - kind: ServiceAccount
    name: workflow-sa
    namespace: ml-pipeline
```

---

## 实战 1：DAG 并行数据处理管道

场景：每天对用户行为日志做数据清洗 → 特征提取 → 多维度聚合（并行）→ 写入数仓。

```yaml
# data-pipeline.yaml
apiVersion: argoproj.io/v1alpha1
kind: WorkflowTemplate
metadata:
  name: data-pipeline
  namespace: ml-pipeline
spec:
  serviceAccountName: workflow-sa
  entrypoint: main-dag
  
  # Artifact 仓库配置（引用 controller 全局配置）
  artifactRepositoryRef:
    configMap: artifact-repositories
    key: default

  # 全局参数
  arguments:
    parameters:
      - name: date
        value: "2026-04-12"
      - name: s3-bucket
        value: "yourorg-data-lake"

  templates:
    # 主 DAG
    - name: main-dag
      dag:
        tasks:
          - name: extract-logs
            template: extract-logs-tmpl
            arguments:
              parameters:
                - name: date
                  value: "{{workflow.parameters.date}}"

          # 依赖 extract-logs 完成后并行执行
          - name: clean-events
            dependencies: [extract-logs]
            template: data-clean-tmpl
            arguments:
              parameters:
                - name: input-type
                  value: "events"
              artifacts:
                - name: raw-data
                  from: "{{tasks.extract-logs.outputs.artifacts.raw-events}}"

          - name: clean-sessions
            dependencies: [extract-logs]
            template: data-clean-tmpl
            arguments:
              parameters:
                - name: input-type
                  value: "sessions"
              artifacts:
                - name: raw-data
                  from: "{{tasks.extract-logs.outputs.artifacts.raw-sessions}}"

          # 三路聚合，互相独立并行
          - name: agg-dau
            dependencies: [clean-events]
            template: aggregation-tmpl
            arguments:
              parameters:
                - name: metric
                  value: "dau"
              artifacts:
                - name: clean-data
                  from: "{{tasks.clean-events.outputs.artifacts.clean-data}}"

          - name: agg-retention
            dependencies: [clean-events, clean-sessions]
            template: aggregation-tmpl
            arguments:
              parameters:
                - name: metric
                  value: "retention"
              artifacts:
                - name: clean-data
                  from: "{{tasks.clean-events.outputs.artifacts.clean-data}}"

          - name: agg-funnel
            dependencies: [clean-sessions]
            template: aggregation-tmpl
            arguments:
              parameters:
                - name: metric
                  value: "funnel"
              artifacts:
                - name: clean-data
                  from: "{{tasks.clean-sessions.outputs.artifacts.clean-data}}"

          # 所有聚合完成后写入数仓
          - name: load-to-warehouse
            dependencies: [agg-dau, agg-retention, agg-funnel]
            template: warehouse-load-tmpl
            arguments:
              parameters:
                - name: date
                  value: "{{workflow.parameters.date}}"

    # 提取模板
    - name: extract-logs-tmpl
      inputs:
        parameters:
          - name: date
      outputs:
        artifacts:
          - name: raw-events
            path: /data/output/events
            s3:
              key: "raw/{{inputs.parameters.date}}/events"
          - name: raw-sessions
            path: /data/output/sessions
            s3:
              key: "raw/{{inputs.parameters.date}}/sessions"
      container:
        image: yourorg/data-extractor:v2.1.0
        command: [python, extract.py]
        args:
          - --date={{inputs.parameters.date}}
          - --output-dir=/data/output
        resources:
          requests:
            cpu: "500m"
            memory: "1Gi"
          limits:
            cpu: "2"
            memory: "4Gi"
        env:
          - name: S3_BUCKET
            value: "{{workflow.parameters.s3-bucket}}"

    # 清洗模板（可复用，通过参数区分 events/sessions）
    - name: data-clean-tmpl
      inputs:
        parameters:
          - name: input-type
        artifacts:
          - name: raw-data
            path: /data/input
      outputs:
        artifacts:
          - name: clean-data
            path: /data/output
            s3:
              key: "clean/{{inputs.parameters.input-type}}"
      container:
        image: yourorg/data-cleaner:v1.5.0
        command: [python, clean.py]
        args:
          - --type={{inputs.parameters.input-type}}
          - --input=/data/input
          - --output=/data/output
        resources:
          requests:
            cpu: "1"
            memory: "2Gi"

    # 聚合模板
    - name: aggregation-tmpl
      inputs:
        parameters:
          - name: metric
        artifacts:
          - name: clean-data
            path: /data/input
      outputs:
        artifacts:
          - name: agg-result
            path: /data/output
      container:
        image: yourorg/data-aggregator:v1.3.0
        command: [python, aggregate.py]
        args:
          - --metric={{inputs.parameters.metric}}
        resources:
          requests:
            cpu: "2"
            memory: "4Gi"
          limits:
            cpu: "4"
            memory: "8Gi"

    # 数仓加载（写操作，设置重试）
    - name: warehouse-load-tmpl
      inputs:
        parameters:
          - name: date
      retryStrategy:
        limit: "3"
        retryPolicy: "OnFailure"
        backoff:
          duration: "30s"
          factor: "2"
          maxDuration: "5m"
      container:
        image: yourorg/warehouse-loader:v1.0.0
        command: [python, load.py]
        args:
          - --date={{inputs.parameters.date}}
```

---

## 实战 2：ML 训练 Pipeline

场景：数据预处理 → 模型训练（GPU） → 评估 → 条件注册（准确率达标才注册）。

```yaml
# ml-training-pipeline.yaml
apiVersion: argoproj.io/v1alpha1
kind: WorkflowTemplate
metadata:
  name: ml-training-pipeline
  namespace: ml-pipeline
spec:
  serviceAccountName: workflow-sa
  entrypoint: training-dag

  arguments:
    parameters:
      - name: model-name
        value: "user-intent-classifier"
      - name: dataset-version
        value: "v20260412"
      - name: accuracy-threshold
        value: "0.85"

  templates:
    - name: training-dag
      dag:
        tasks:
          - name: preprocess
            template: preprocess-tmpl
            arguments:
              parameters:
                - name: dataset-version
                  value: "{{workflow.parameters.dataset-version}}"

          - name: train
            dependencies: [preprocess]
            template: train-tmpl
            arguments:
              parameters:
                - name: model-name
                  value: "{{workflow.parameters.model-name}}"
              artifacts:
                - name: train-data
                  from: "{{tasks.preprocess.outputs.artifacts.train-data}}"
                - name: val-data
                  from: "{{tasks.preprocess.outputs.artifacts.val-data}}"

          - name: evaluate
            dependencies: [train]
            template: evaluate-tmpl
            arguments:
              artifacts:
                - name: model
                  from: "{{tasks.train.outputs.artifacts.model}}"
                - name: test-data
                  from: "{{tasks.preprocess.outputs.artifacts.test-data}}"

          # 条件注册：只有评估通过才执行
          - name: register-model
            dependencies: [evaluate]
            template: register-tmpl
            when: "{{tasks.evaluate.outputs.parameters.accuracy}} > {{workflow.parameters.accuracy-threshold}}"
            arguments:
              parameters:
                - name: model-name
                  value: "{{workflow.parameters.model-name}}"
                - name: accuracy
                  value: "{{tasks.evaluate.outputs.parameters.accuracy}}"
              artifacts:
                - name: model
                  from: "{{tasks.train.outputs.artifacts.model}}"

          # 不管注册是否执行，都发送通知
          - name: notify
            dependencies: [evaluate]
            template: notify-tmpl
            arguments:
              parameters:
                - name: model-name
                  value: "{{workflow.parameters.model-name}}"
                - name: accuracy
                  value: "{{tasks.evaluate.outputs.parameters.accuracy}}"
                - name: threshold
                  value: "{{workflow.parameters.accuracy-threshold}}"

    - name: preprocess-tmpl
      inputs:
        parameters:
          - name: dataset-version
      outputs:
        artifacts:
          - name: train-data
            path: /data/train
          - name: val-data
            path: /data/val
          - name: test-data
            path: /data/test
      container:
        image: yourorg/ml-preprocess:v3.0.0
        command: [python, preprocess.py]
        args:
          - --dataset-version={{inputs.parameters.dataset-version}}
          - --output-dir=/data
          - --train-ratio=0.8
          - --val-ratio=0.1
        resources:
          requests:
            cpu: "4"
            memory: "16Gi"

    # GPU 训练任务
    - name: train-tmpl
      inputs:
        parameters:
          - name: model-name
        artifacts:
          - name: train-data
            path: /data/train
          - name: val-data
            path: /data/val
      outputs:
        artifacts:
          - name: model
            path: /model/output
            s3:
              key: "models/{{inputs.parameters.model-name}}/{{workflow.name}}"
      # 调度到 GPU 节点
      nodeSelector:
        node.kubernetes.io/gpu: "true"
      tolerations:
        - key: "nvidia.com/gpu"
          operator: "Exists"
          effect: "NoSchedule"
      container:
        image: yourorg/ml-trainer:v2.5.0-cuda12
        command: [python, train.py]
        args:
          - --train-data=/data/train
          - --val-data=/data/val
          - --output=/model/output
          - --epochs=50
          - --batch-size=256
        resources:
          requests:
            cpu: "8"
            memory: "32Gi"
            nvidia.com/gpu: "1"
          limits:
            cpu: "16"
            memory: "64Gi"
            nvidia.com/gpu: "1"
        env:
          - name: MLFLOW_TRACKING_URI
            valueFrom:
              configMapKeyRef:
                name: ml-config
                key: mlflow-uri

    - name: evaluate-tmpl
      inputs:
        artifacts:
          - name: model
            path: /model
          - name: test-data
            path: /data/test
      outputs:
        parameters:
          # 从文件读取评估结果，供后续条件判断使用
          - name: accuracy
            valueFrom:
              path: /tmp/metrics/accuracy.txt
          - name: f1-score
            valueFrom:
              path: /tmp/metrics/f1.txt
        artifacts:
          - name: eval-report
            path: /tmp/metrics
      container:
        image: yourorg/ml-evaluator:v1.2.0
        command: [python, evaluate.py]
        args:
          - --model=/model
          - --test-data=/data/test
          - --output-dir=/tmp/metrics
        resources:
          requests:
            cpu: "4"
            memory: "8Gi"

    - name: register-tmpl
      inputs:
        parameters:
          - name: model-name
          - name: accuracy
        artifacts:
          - name: model
            path: /model
      container:
        image: yourorg/model-registry-client:v1.0.0
        command: [python, register.py]
        args:
          - --model-name={{inputs.parameters.model-name}}
          - --accuracy={{inputs.parameters.accuracy}}
          - --model-path=/model
          - --stage=staging  # 先推到 staging，人工审核后再 promote 到 production
        env:
          - name: REGISTRY_URL
            valueFrom:
              configMapKeyRef:
                name: ml-config
                key: registry-url

    - name: notify-tmpl
      inputs:
        parameters:
          - name: model-name
          - name: accuracy
          - name: threshold
      script:
        image: python:3.11-slim
        command: [python]
        source: |
          import os, json, urllib.request

          accuracy = float("{{inputs.parameters.accuracy}}")
          threshold = float("{{inputs.parameters.threshold}}")
          model_name = "{{inputs.parameters.model-name}}"
          registered = accuracy > threshold

          msg = {
              "model": model_name,
              "accuracy": accuracy,
              "threshold": threshold,
              "registered": registered,
              "workflow": os.environ.get("ARGO_WORKFLOW_NAME", "unknown"),
          }
          # 发送到钉钉/Slack
          webhook = os.environ.get("NOTIFICATION_WEBHOOK", "")
          if webhook:
              data = json.dumps({"text": str(msg)}).encode()
              urllib.request.urlopen(urllib.request.Request(webhook, data=data))
          print(json.dumps(msg))
        env:
          - name: NOTIFICATION_WEBHOOK
            valueFrom:
              secretKeyRef:
                name: notification-secret
                key: webhook-url
```

---

## 实战 3：CronWorkflow 定时备份

```yaml
# db-backup-cron.yaml
apiVersion: argoproj.io/v1alpha1
kind: CronWorkflow
metadata:
  name: db-backup-daily
  namespace: ops
spec:
  # 每天凌晨 2:00 UTC 执行
  schedule: "0 2 * * *"
  timezone: "UTC"
  concurrencyPolicy: Forbid     # 如果上次还没结束，跳过本次
  startingDeadlineSeconds: 1800 # 调度延迟超过 30min 则跳过
  successfulJobsHistoryLimit: 7 # 保留最近 7 次成功记录
  failedJobsHistoryLimit: 3

  workflowSpec:
    serviceAccountName: backup-sa
    entrypoint: backup-steps

    templates:
      - name: backup-steps
        steps:
          # 并行备份多个数据库
          - - name: backup-mysql-user
              template: mysql-backup
              arguments:
                parameters:
                  - name: db-host
                    value: "mysql-user.production.svc"
                  - name: db-name
                    value: "user_db"
            - name: backup-mysql-order
              template: mysql-backup
              arguments:
                parameters:
                  - name: db-host
                    value: "mysql-order.production.svc"
                  - name: db-name
                    value: "order_db"
            - name: backup-postgres
              template: postgres-backup
              arguments:
                parameters:
                  - name: db-host
                    value: "postgres.production.svc"
                  - name: db-name
                    value: "analytics"

          # 备份完成后验证
          - - name: verify-backups
              template: verify-backup
              arguments:
                parameters:
                  - name: backup-date
                    value: "{{workflow.creationTimestamp.Y}}-{{workflow.creationTimestamp.m}}-{{workflow.creationTimestamp.d}}"

          # 清理 30 天前的备份
          - - name: cleanup-old-backups
              template: cleanup-backups
              arguments:
                parameters:
                  - name: retention-days
                    value: "30"

      - name: mysql-backup
        inputs:
          parameters:
            - name: db-host
            - name: db-name
        container:
          image: mysql:8.0
          command: [sh, -c]
          args:
            - |
              DATE=$(date +%Y%m%d)
              FILENAME="${{inputs.parameters.db-name}}_${DATE}.sql.gz"
              mysqldump \
                -h {{inputs.parameters.db-host}} \
                -u $MYSQL_USER \
                -p$MYSQL_PASSWORD \
                --single-transaction \
                --routines \
                --triggers \
                {{inputs.parameters.db-name}} | gzip > /backup/${FILENAME}
              
              # 上传到 S3
              aws s3 cp /backup/${FILENAME} \
                s3://$S3_BUCKET/mysql/{{inputs.parameters.db-name}}/${FILENAME}
              
              echo "Backup completed: ${FILENAME}"
          env:
            - name: MYSQL_USER
              valueFrom:
                secretKeyRef:
                  name: db-backup-secret
                  key: mysql-user
            - name: MYSQL_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: db-backup-secret
                  key: mysql-password
            - name: S3_BUCKET
              value: "yourorg-db-backups"
          volumeMounts:
            - name: backup-tmp
              mountPath: /backup
        volumes:
          - name: backup-tmp
            emptyDir:
              sizeLimit: 10Gi

      - name: postgres-backup
        inputs:
          parameters:
            - name: db-host
            - name: db-name
        container:
          image: postgres:15
          command: [sh, -c]
          args:
            - |
              DATE=$(date +%Y%m%d)
              FILENAME="${{inputs.parameters.db-name}}_${DATE}.dump"
              pg_dump \
                -h {{inputs.parameters.db-host}} \
                -U $POSTGRES_USER \
                -Fc \
                {{inputs.parameters.db-name}} > /backup/${FILENAME}
              
              aws s3 cp /backup/${FILENAME} \
                s3://$S3_BUCKET/postgres/{{inputs.parameters.db-name}}/${FILENAME}
          env:
            - name: PGPASSWORD
              valueFrom:
                secretKeyRef:
                  name: db-backup-secret
                  key: postgres-password
            - name: POSTGRES_USER
              valueFrom:
                secretKeyRef:
                  name: db-backup-secret
                  key: postgres-user
            - name: S3_BUCKET
              value: "yourorg-db-backups"

      - name: verify-backup
        inputs:
          parameters:
            - name: backup-date
        script:
          image: python:3.11-slim
          command: [python]
          source: |
            import boto3, sys
            from datetime import datetime

            s3 = boto3.client('s3')
            bucket = 'yourorg-db-backups'
            date = "{{inputs.parameters.backup-date}}".replace('-', '')
            
            expected = ['mysql/user_db', 'mysql/order_db', 'postgres/analytics']
            missing = []
            
            for prefix in expected:
                resp = s3.list_objects_v2(Bucket=bucket, Prefix=f"{prefix}/{prefix.split('/')[-1]}_{date}")
                if resp.get('KeyCount', 0) == 0:
                    missing.append(prefix)
            
            if missing:
                print(f"MISSING BACKUPS: {missing}", file=sys.stderr)
                sys.exit(1)
            
            print(f"All backups verified for {date}")
```

---

## 参数化：WorkflowTemplate + argo submit

WorkflowTemplate 定义骨架，运行时通过 `argo submit --from` 覆盖参数，实现同一模板处理不同数据集：

```bash
# 直接提交，覆盖默认参数
argo submit --from workflowtemplate/ml-training-pipeline \
  --name ml-train-20260412 \
  --namespace ml-pipeline \
  -p dataset-version=v20260412 \
  -p accuracy-threshold=0.88 \
  -p model-name=user-intent-v3

# 查看执行状态
argo get ml-train-20260412 -n ml-pipeline

# 实时查看日志（某个 step）
argo logs ml-train-20260412 -n ml-pipeline --follow

# 重试失败的 workflow
argo retry ml-train-20260412 -n ml-pipeline

# 从某个失败的节点重新执行（跳过已成功的节点）
argo resubmit ml-train-20260412 -n ml-pipeline --memoize
```

---

## 资源管控

### Semaphore：并发限制

防止大量 workflow 同时跑把集群资源打爆：

```yaml
# semaphore-config.yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: semaphore-config
  namespace: ml-pipeline
data:
  # 最多同时 3 个 GPU 训练任务
  gpu-training: "3"
  # 最多同时 10 个数据处理任务
  data-processing: "10"
```

在 WorkflowTemplate 中引用：

```yaml
spec:
  synchronization:
    semaphore:
      configMapKeyRef:
        name: semaphore-config
        key: gpu-training
```

也可以在单个 Template 级别设置：

```yaml
- name: train-tmpl
  synchronization:
    semaphore:
      configMapKeyRef:
        name: semaphore-config
        key: gpu-training
  container: ...
```

### 资源配额与 Node Affinity

```yaml
# 指定运行在特定节点池（如专用 ML 节点）
- name: train-tmpl
  nodeSelector:
    workload-type: ml-training
  tolerations:
    - key: "dedicated"
      value: "ml"
      effect: "NoSchedule"
  affinity:
    nodeAffinity:
      requiredDuringSchedulingIgnoredDuringExecution:
        nodeSelectorTerms:
          - matchExpressions:
              - key: "node.kubernetes.io/instance-type"
                operator: In
                values:
                  - "g4dn.xlarge"
                  - "g4dn.2xlarge"
    # 尽量不与其他 ML 任务在同一节点（减少 GPU 竞争）
    podAntiAffinity:
      preferredDuringSchedulingIgnoredDuringExecution:
        - weight: 100
          podAffinityTerm:
            labelSelector:
              matchLabels:
                workflows.argoproj.io/workflow: "{{workflow.name}}"
            topologyKey: "kubernetes.io/hostname"
```

---

## 与 Argo Events 集成：Webhook 触发

代码提交后自动触发训练 pipeline：

```yaml
# event-source.yaml - 接收 GitHub Webhook
apiVersion: argoproj.io/v1alpha1
kind: EventSource
metadata:
  name: github-webhook
  namespace: argo-events
spec:
  service:
    ports:
      - port: 12000
        targetPort: 12000
  github:
    training-trigger:
      repositories:
        - owner: yourorg
          names:
            - ml-datasets
      webhook:
        endpoint: /push
        port: "12000"
        method: POST
        url: https://argo-events.internal.yourorg.com
      events:
        - push
      filter:
        branches:
          - main
      contentType: json
      insecure: false
      secretRef:
        name: github-webhook-secret
        key: secret
---
# sensor.yaml - 响应事件，提交 workflow
apiVersion: argoproj.io/v1alpha1
kind: Sensor
metadata:
  name: training-trigger
  namespace: argo-events
spec:
  dependencies:
    - name: github-push
      eventSourceName: github-webhook
      eventName: training-trigger
      filters:
        data:
          # 只有 dataset/ 目录变更才触发
          - path: body.commits.#.modified.#
            type: string
            value:
              - "dataset/.*"
            comparator: "="
            template: "{{ (parseJSON .Input).commits | toJson }}"

  triggers:
    - template:
        name: ml-training-workflow
        argoWorkflow:
          operation: submit
          source:
            resource:
              apiVersion: argoproj.io/v1alpha1
              kind: Workflow
              metadata:
                generateName: ml-train-auto-
                namespace: ml-pipeline
              spec:
                workflowTemplateRef:
                  name: ml-training-pipeline
                arguments:
                  parameters:
                    - name: dataset-version
                      # 从事件 payload 提取 commit SHA
                      value: "{{ .Input.body.after | substr 0 8 }}"
      retryStrategy:
        steps: 3
        duration: 10s
```

---

## 监控：Prometheus + Grafana

Argo Workflows controller 默认暴露 Prometheus metrics：

```yaml
# ServiceMonitor（Prometheus Operator）
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: argo-workflows
  namespace: monitoring
spec:
  selector:
    matchLabels:
      app.kubernetes.io/name: argo-workflows-workflow-controller
  namespaceSelector:
    matchNames:
      - argo
  endpoints:
    - port: metrics
      interval: 30s
```

关键指标与告警规则：

```yaml
# PrometheusRule
groups:
  - name: argo-workflows
    rules:
      # 工作流成功率低于 90%
      - alert: ArgoWorkflowSuccessRateLow
        expr: |
          sum(rate(argo_workflows_count{phase="Succeeded"}[1h])) by (namespace)
          /
          sum(rate(argo_workflows_count[1h])) by (namespace)
          < 0.9
        for: 30m
        labels:
          severity: warning
        annotations:
          summary: "Argo Workflow success rate below 90% in {{ $labels.namespace }}"

      # 工作流队列积压
      - alert: ArgoWorkflowQueueDepth
        expr: argo_workflow_queue_depth_gauge > 50
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "Argo Workflow queue depth is {{ $value }}"

      # 工作流运行时间过长（超过 2 小时）
      - alert: ArgoWorkflowRunningTooLong
        expr: |
          argo_workflows_count{phase="Running"} > 0
          and
          (time() - argo_workflow_info) > 7200
        labels:
          severity: warning
```

Grafana Dashboard 核心面板：

```promql
# 每小时工作流完成数（按状态）
sum(increase(argo_workflows_count[1h])) by (phase, namespace)

# P95 执行时长（按工作流名称）
histogram_quantile(0.95, 
  sum(rate(argo_workflow_duration_seconds_bucket[1h])) by (le, workflow_name)
)

# 当前运行中的工作流数量
sum(argo_workflows_count{phase="Running"}) by (namespace)

# Pod 启动延迟
histogram_quantile(0.95,
  sum(rate(argo_pod_pending_seconds_bucket[30m])) by (le)
)
```

---

## 常见问题处理

### Pod 数量爆炸

**问题**：大型 DAG + 高并发提交，瞬间创建几百个 Pod，打爆 API Server 和调度器。

**解法**：

```yaml
# 方法1：Semaphore 限制并发（见上文）

# 方法2：workflow 级别的并发限制
spec:
  parallelism: 10  # 整个 workflow 最多 10 个 Pod 并行

# 方法3：controller 全局限制（values.yaml）
controller:
  maxWorkflowsPerNamespace: 50  # 每 namespace 最多并发 50 个 workflow
  resourceRateLimit:
    limit: 10   # 每秒最多创建 10 个 K8s 资源
    burst: 1
```

### Artifact 存储配置（S3/MinIO）

**artifact 下载失败**常见原因：IAM 权限不足、endpoint 配置错误、bucket 区域不匹配。

```yaml
# 完整的 S3 artifact 配置（controller ConfigMap）
apiVersion: v1
kind: ConfigMap
metadata:
  name: workflow-controller-configmap
  namespace: argo
data:
  artifactRepository: |
    s3:
      bucket: yourorg-argo-artifacts
      endpoint: s3.us-west-2.amazonaws.com
      region: us-west-2
      useSDKCreds: true   # 使用 Pod 的 IRSA，不需要 AK/SK
      insecure: false
      
  # 对于私有集群（无公网），使用 VPC endpoint
  # endpoint: s3.us-west-2.amazonaws.com
  # 换成：bucket.vpce-xxx.s3.us-west-2.vpce.amazonaws.com

# MinIO 配置（自托管）
  artifactRepository: |
    s3:
      bucket: argo-artifacts
      endpoint: minio.minio.svc:9000
      insecure: true
      accessKeySecret:
        name: minio-secret
        key: accesskey
      secretKeySecret:
        name: minio-secret
        key: secretkey
```

### 节点状态卸载（大规模 workflow 必配）

当 workflow 有数百个节点时，状态全存在 Workflow CRD 的 `.status` 字段会超过 etcd 的 1MB 对象限制：

```yaml
# controller ConfigMap
data:
  nodeStatusOffLoad: "true"   # 将节点状态卸载到 artifact 存储
  podGCStrategy: "OnWorkflowSuccess"  # 成功完成后清理 Pod（保留失败的便于排查）
```

---

## 小结

Argo Workflows 是 Kubernetes 生态中批处理和 ML Pipeline 的最佳选择，核心优势在于：

1. **完全 Kubernetes 原生**：无额外状态存储，调度、隔离、资源管控复用 K8s 能力
2. **DAG + Artifact 传递**：天然描述数据依赖关系，中间结果自动存储到 S3
3. **WorkflowTemplate 复用**：一次定义，多次参数化执行
4. **与 Argo Events 集成**：事件驱动，代码提交/API 调用/消息队列均可触发

生产落地时重点关注：Semaphore 防止资源打爆、nodeStatusOffLoad 避免 etcd 写入过大、Artifact 存储权限正确配置、以及监控指标与告警覆盖。
