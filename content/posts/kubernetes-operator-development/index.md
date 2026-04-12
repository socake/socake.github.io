---
title: "Kubernetes Operator 开发实战：Go + controller-runtime 完全指南"
date: 2025-12-03T14:00:00+08:00
draft: false
tags: ["kubernetes", "operator", "go", "controller-runtime", "kubebuilder", "crd"]
categories: ["Kubernetes"]
description: "从零到生产级 Kubernetes Operator 完整开发指南，涵盖 CRD 设计、Reconcile 核心逻辑、Finalizer、Leader Election、Webhook 验证、envtest 测试，以及完整的 RBAC 权限设计。"
summary: "用 Go + controller-runtime 开发生产级 Kubernetes Operator 的完整实战指南。以 DatabaseCluster Operator 为例，深入讲解 CRD 设计、Reconcile 模式、Status Conditions、Finalizer 防孤儿资源、Leader Election、指标暴露、Webhook 验证，以及 envtest + Kind 测试策略。"
toc: true
math: false
diagram: false
keywords: ["kubernetes operator", "controller-runtime", "kubebuilder", "CRD", "reconcile", "finalizer", "envtest"]
params:
  reading_time: true
---

## Operator 解决什么问题

Helm Chart 和 Operator 经常被混淆，但它们解决的是完全不同层次的问题。

**Helm Chart** 是打包和部署工具：把一堆 YAML 模板化，一条命令安装到集群。它是**幂等的部署**，但不是**持续协调**。你 `helm install` 之后，如果有人手动改了 Deployment 的副本数，Helm 不会帮你纠正，下次 `helm upgrade` 才会覆盖回来。

**Operator** 实现的是**运维知识的代码化**：把领域专家的操作经验编码成控制循环，持续监控实际状态和期望状态的差距并自动修复。

举个具体例子——管理一个 MySQL 集群：

| 任务 | Helm Chart 能做吗 | Operator 能做吗 |
|------|-------------------|-----------------|
| 初始部署 | ✅ | ✅ |
| 扩容（加 replica） | 需要 `helm upgrade` 触发 | ✅ 自动检测并处理 |
| 主节点故障自动切换 | ❌ | ✅ Reconcile 检测并重选主 |
| 备份调度 | ❌ | ✅ 内建 CronJob 逻辑 |
| 版本升级（rolling） | 部分 | ✅ 蓝绿/滚动升级编排 |
| 密码轮换 | ❌ | ✅ 监听 Secret 变化触发 |

Operator 的核心是**控制循环（Control Loop）**：

```
          Watch (事件)
            ↓
       Work Queue
            ↓
       Reconcile()
       ┌─────────────────────────────┐
       │ 1. Observe: 读取当前状态    │
       │ 2. Analyze: 和期望状态对比  │
       │ 3. Act: 调用 API 纠正差距   │
       └─────────────────────────────┘
            ↓
       (更新 Status)
            ↓
       等待下次事件触发
```

---

## controller-runtime vs client-go

`client-go` 是 K8s 官方 Go 客户端库，提供：
- Typed/Untyped API 客户端
- Informer/Lister 缓存机制
- WorkQueue 实现

`controller-runtime` 是在 `client-go` 之上的高级封装，由 `kubebuilder` 和 `operator-sdk` 共同维护，提供：
- `Manager`：统一管理 Controller 生命周期、Leader Election、健康检查
- `Reconciler` 接口：标准化 Reconcile 模式
- `Builder`：声明式注册 Watch 和事件过滤
- `envtest`：集成测试框架

除非你需要极细粒度控制（比如自定义 Informer 的 ResyncPeriod、自定义 WorkQueue 限速算法），否则直接用 controller-runtime，不要从 client-go 从头写。

---

## 用 kubebuilder 初始化项目

```bash
# 安装 kubebuilder
curl -L -o kubebuilder \
  "https://go.kubebuilder.io/dl/latest/$(go env GOOS)/$(go env GOARCH)"
chmod +x kubebuilder && mv kubebuilder /usr/local/bin/

# 初始化项目
mkdir database-operator && cd database-operator
kubebuilder init \
  --domain example.com \
  --repo github.com/example/database-operator \
  --project-name database-operator

# 创建 API（生成 CRD 和 Controller 脚手架）
kubebuilder create api \
  --group database \
  --version v1alpha1 \
  --kind DatabaseCluster \
  --resource --controller
```

生成的目录结构：

```
database-operator/
├── api/
│   └── v1alpha1/
│       ├── databasecluster_types.go     # CRD 类型定义
│       ├── groupversion_info.go
│       └── zz_generated.deepcopy.go     # 自动生成，不要手改
├── internal/controller/
│   └── databasecluster_controller.go    # Reconcile 逻辑
├── config/
│   ├── crd/                             # 生成的 CRD YAML
│   ├── rbac/                            # RBAC manifests
│   ├── manager/                         # Deployment manifests
│   └── default/                         # Kustomize base
├── main.go
└── Makefile
```

---

## 定义 CRD

编辑 `api/v1alpha1/databasecluster_types.go`：

```go
package v1alpha1

import (
    corev1 "k8s.io/api/core/v1"
    "k8s.io/apimachinery/pkg/api/resource"
    metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// DatabaseClusterSpec 定义用户期望的状态
type DatabaseClusterSpec struct {
    // +kubebuilder:validation:Enum=mysql;postgresql
    // +kubebuilder:default=mysql
    Engine string `json:"engine"`

    // +kubebuilder:validation:Minimum=1
    // +kubebuilder:validation:Maximum=9
    // +kubebuilder:default=1
    Replicas int32 `json:"replicas"`

    // +kubebuilder:validation:Pattern=`^\d+\.\d+\.\d+$`
    Version string `json:"version"`

    Storage StorageSpec `json:"storage"`

    // +optional
    Resources *corev1.ResourceRequirements `json:"resources,omitempty"`

    // 备份配置，可选
    // +optional
    Backup *BackupSpec `json:"backup,omitempty"`

    // 指向存储密码的 Secret
    PasswordSecretRef corev1.SecretKeySelector `json:"passwordSecretRef"`
}

type StorageSpec struct {
    // +kubebuilder:validation:Pattern=`^(\+|-)?(([0-9]+(\.[0-9]*)?)|(\.[0-9]+))(([KMGTPE]i)|[numkMGTPE]|([eE](\+|-)?(([0-9]+(\.[0-9]*)?)|(\.[0-9]+))))?$`
    Size resource.Quantity `json:"size"`

    // +optional
    StorageClassName *string `json:"storageClassName,omitempty"`
}

type BackupSpec struct {
    // Cron 表达式
    // +kubebuilder:validation:Pattern=`^(@(annually|yearly|monthly|weekly|daily|hourly))|(\S+ \S+ \S+ \S+ \S+)$`
    Schedule string `json:"schedule"`

    // 保留备份数量
    // +kubebuilder:default=7
    Retention int32 `json:"retention"`

    S3Bucket string `json:"s3Bucket"`
}

// DatabaseClusterStatus 记录 Operator 观察到的实际状态
type DatabaseClusterStatus struct {
    // 标准 Condition 列表
    // +optional
    // +listType=map
    // +listMapKey=type
    Conditions []metav1.Condition `json:"conditions,omitempty"`

    // 当前就绪的 replica 数
    ReadyReplicas int32 `json:"readyReplicas"`

    // 当前主节点 Pod 名
    // +optional
    PrimaryPod string `json:"primaryPod,omitempty"`

    // 集群阶段
    // +kubebuilder:validation:Enum=Pending;Initializing;Running;Degraded;Upgrading;Deleting
    Phase string `json:"phase,omitempty"`

    // 当前运行版本
    // +optional
    CurrentVersion string `json:"currentVersion,omitempty"`

    // 下次备份时间
    // +optional
    NextBackupTime *metav1.Time `json:"nextBackupTime,omitempty"`
}

// Condition Type 常量
const (
    ConditionReady    = "Ready"
    ConditionDegraded = "Degraded"
    ConditionUpgrading = "Upgrading"
)

// Phase 常量
const (
    PhasePending      = "Pending"
    PhaseInitializing = "Initializing"
    PhaseRunning      = "Running"
    PhaseDegraded     = "Degraded"
    PhaseUpgrading    = "Upgrading"
    PhaseDeleting     = "Deleting"
)

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:printcolumn:name="Engine",type="string",JSONPath=".spec.engine"
// +kubebuilder:printcolumn:name="Replicas",type="integer",JSONPath=".spec.replicas"
// +kubebuilder:printcolumn:name="Ready",type="integer",JSONPath=".status.readyReplicas"
// +kubebuilder:printcolumn:name="Phase",type="string",JSONPath=".status.phase"
// +kubebuilder:printcolumn:name="Age",type="date",JSONPath=".metadata.creationTimestamp"
type DatabaseCluster struct {
    metav1.TypeMeta   `json:",inline"`
    metav1.ObjectMeta `json:"metadata,omitempty"`

    Spec   DatabaseClusterSpec   `json:"spec,omitempty"`
    Status DatabaseClusterStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true
type DatabaseClusterList struct {
    metav1.TypeMeta `json:",inline"`
    metav1.ListMeta `json:"metadata,omitempty"`
    Items           []DatabaseCluster `json:"items"`
}

func init() {
    SchemeBuilder.Register(&DatabaseCluster{}, &DatabaseClusterList{})
}
```

生成 CRD YAML：

```bash
make generate   # 生成 zz_generated.deepcopy.go
make manifests  # 生成 config/crd/bases/*.yaml
```

---

## Reconcile 核心逻辑

`internal/controller/databasecluster_controller.go`：

```go
package controller

import (
    "context"
    "fmt"
    "time"

    appsv1 "k8s.io/api/apps/v1"
    corev1 "k8s.io/api/core/v1"
    "k8s.io/apimachinery/pkg/api/errors"
    "k8s.io/apimachinery/pkg/api/meta"
    metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
    "k8s.io/apimachinery/pkg/runtime"
    "k8s.io/apimachinery/pkg/types"
    ctrl "sigs.k8s.io/controller-runtime"
    "sigs.k8s.io/controller-runtime/pkg/client"
    "sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
    "sigs.k8s.io/controller-runtime/pkg/log"

    databasev1alpha1 "github.com/example/database-operator/api/v1alpha1"
)

const (
    finalizerName    = "database.example.com/finalizer"
    requeueAfter     = 30 * time.Second
)

type DatabaseClusterReconciler struct {
    client.Client
    Scheme *runtime.Scheme
}

// +kubebuilder:rbac:groups=database.example.com,resources=databaseclusters,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=database.example.com,resources=databaseclusters/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=database.example.com,resources=databaseclusters/finalizers,verbs=update
// +kubebuilder:rbac:groups=apps,resources=statefulsets,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups="",resources=services;configmaps;secrets;persistentvolumeclaims,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups="",resources=pods,verbs=get;list;watch
// +kubebuilder:rbac:groups=batch,resources=cronjobs,verbs=get;list;watch;create;update;patch;delete

func (r *DatabaseClusterReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
    logger := log.FromContext(ctx)

    // === Observe：获取对象 ===
    db := &databasev1alpha1.DatabaseCluster{}
    if err := r.Get(ctx, req.NamespacedName, db); err != nil {
        if errors.IsNotFound(err) {
            return ctrl.Result{}, nil  // 对象已删除，忽略
        }
        return ctrl.Result{}, fmt.Errorf("get DatabaseCluster: %w", err)
    }

    // === 处理删除：Finalizer 逻辑 ===
    if !db.DeletionTimestamp.IsZero() {
        return r.handleDeletion(ctx, db)
    }

    // 注册 Finalizer（首次创建时）
    if !controllerutil.ContainsFinalizer(db, finalizerName) {
        controllerutil.AddFinalizer(db, finalizerName)
        if err := r.Update(ctx, db); err != nil {
            return ctrl.Result{}, fmt.Errorf("add finalizer: %w", err)
        }
        return ctrl.Result{Requeue: true}, nil
    }

    // === Analyze + Act：对比期望状态，执行调和 ===
    result, err := r.reconcileComponents(ctx, db)
    if err != nil {
        // 更新 Degraded Condition
        r.setCondition(ctx, db, databasev1alpha1.ConditionDegraded, metav1.ConditionTrue,
            "ReconcileError", err.Error())
        return ctrl.Result{}, err
    }

    return result, nil
}

func (r *DatabaseClusterReconciler) reconcileComponents(
    ctx context.Context,
    db *databasev1alpha1.DatabaseCluster,
) (ctrl.Result, error) {
    logger := log.FromContext(ctx)

    // 1. 确保 StatefulSet 存在且配置正确
    if err := r.reconcileStatefulSet(ctx, db); err != nil {
        return ctrl.Result{}, fmt.Errorf("reconcile StatefulSet: %w", err)
    }

    // 2. 确保 Service 存在
    if err := r.reconcileServices(ctx, db); err != nil {
        return ctrl.Result{}, fmt.Errorf("reconcile Services: %w", err)
    }

    // 3. 确保备份 CronJob（如果启用）
    if db.Spec.Backup != nil {
        if err := r.reconcileBackupCronJob(ctx, db); err != nil {
            return ctrl.Result{}, fmt.Errorf("reconcile backup CronJob: %w", err)
        }
    }

    // 4. 更新 Status
    if err := r.updateStatus(ctx, db); err != nil {
        return ctrl.Result{}, fmt.Errorf("update status: %w", err)
    }

    logger.Info("reconcile complete", "phase", db.Status.Phase)
    // 30 秒后重新 Reconcile，持续检查状态
    return ctrl.Result{RequeueAfter: requeueAfter}, nil
}

func (r *DatabaseClusterReconciler) reconcileStatefulSet(
    ctx context.Context,
    db *databasev1alpha1.DatabaseCluster,
) error {
    desired := r.buildStatefulSet(db)

    // 用 controllerutil.CreateOrUpdate 实现幂等
    sts := &appsv1.StatefulSet{}
    _, err := controllerutil.CreateOrUpdate(ctx, r.Client, sts, func() error {
        // 设置 OwnerReference（db 删除时 sts 自动回收）
        if err := controllerutil.SetControllerReference(db, sts, r.Scheme); err != nil {
            return err
        }
        // 只更新关键字段，避免覆盖其他控制器的修改
        sts.Namespace = desired.Namespace
        sts.Name = desired.Name
        sts.Labels = desired.Labels
        sts.Spec.Replicas = desired.Spec.Replicas
        sts.Spec.Template = desired.Spec.Template
        // 注意：VolumeClaimTemplates 不能更新，StatefulSet 创建后不可变
        if sts.CreationTimestamp.IsZero() {
            sts.Spec.VolumeClaimTemplates = desired.Spec.VolumeClaimTemplates
            sts.Spec.Selector = desired.Spec.Selector
            sts.Spec.ServiceName = desired.Spec.ServiceName
        }
        return nil
    })
    return err
}
```

### 状态更新

**重要原则**：不要在 Spec 里读回 Status 做决策，Status 只是观测结果。

```go
func (r *DatabaseClusterReconciler) updateStatus(
    ctx context.Context,
    db *databasev1alpha1.DatabaseCluster,
) error {
    // 查询实际 Pod 状态
    podList := &corev1.PodList{}
    if err := r.List(ctx, podList,
        client.InNamespace(db.Namespace),
        client.MatchingLabels{"app": db.Name, "role": "database"},
    ); err != nil {
        return err
    }

    readyCount := int32(0)
    for _, pod := range podList.Items {
        for _, cond := range pod.Status.Conditions {
            if cond.Type == corev1.PodReady && cond.Status == corev1.ConditionTrue {
                readyCount++
            }
        }
    }

    // 深拷贝，避免修改缓存中的对象
    dbCopy := db.DeepCopy()
    dbCopy.Status.ReadyReplicas = readyCount

    // 判断 Phase
    switch {
    case readyCount == 0:
        dbCopy.Status.Phase = databasev1alpha1.PhaseInitializing
        r.setConditionOnCopy(dbCopy, databasev1alpha1.ConditionReady,
            metav1.ConditionFalse, "NoReadyReplicas", "No replicas are ready yet")
    case readyCount < db.Spec.Replicas:
        dbCopy.Status.Phase = databasev1alpha1.PhaseDegraded
        r.setConditionOnCopy(dbCopy, databasev1alpha1.ConditionReady,
            metav1.ConditionFalse, "InsufficientReplicas",
            fmt.Sprintf("%d/%d replicas ready", readyCount, db.Spec.Replicas))
    default:
        dbCopy.Status.Phase = databasev1alpha1.PhaseRunning
        r.setConditionOnCopy(dbCopy, databasev1alpha1.ConditionReady,
            metav1.ConditionTrue, "AllReplicasReady", "All replicas are ready")
    }

    // Status 子资源更新，不触发 Spec 的 Watch
    return r.Status().Update(ctx, dbCopy)
}

func (r *DatabaseClusterReconciler) setConditionOnCopy(
    db *databasev1alpha1.DatabaseCluster,
    condType string,
    status metav1.ConditionStatus,
    reason, message string,
) {
    meta.SetStatusCondition(&db.Status.Conditions, metav1.Condition{
        Type:               condType,
        Status:             status,
        Reason:             reason,
        Message:            message,
        LastTransitionTime: metav1.Now(),
        ObservedGeneration: db.Generation,
    })
}
```

---

## Finalizer：防止孤儿资源

Finalizer 解决的问题：DatabaseCluster 被删除时，我们需要先执行清理逻辑（比如删除 S3 备份、通知监控系统），才能真正删除对象。

```go
func (r *DatabaseClusterReconciler) handleDeletion(
    ctx context.Context,
    db *databasev1alpha1.DatabaseCluster,
) (ctrl.Result, error) {
    logger := log.FromContext(ctx)

    if !controllerutil.ContainsFinalizer(db, finalizerName) {
        return ctrl.Result{}, nil  // 已清理完毕
    }

    logger.Info("handling deletion", "name", db.Name)

    // 更新 Phase 为 Deleting
    dbCopy := db.DeepCopy()
    dbCopy.Status.Phase = databasev1alpha1.PhaseDeleting
    if err := r.Status().Update(ctx, dbCopy); err != nil {
        return ctrl.Result{}, err
    }

    // 执行清理逻辑
    if err := r.cleanupExternalResources(ctx, db); err != nil {
        // 清理失败，不移除 Finalizer，等待重试
        return ctrl.Result{RequeueAfter: 10 * time.Second},
            fmt.Errorf("cleanup external resources: %w", err)
    }

    // 清理完成，移除 Finalizer → K8s 会真正删除对象
    controllerutil.RemoveFinalizer(db, finalizerName)
    if err := r.Update(ctx, db); err != nil {
        return ctrl.Result{}, fmt.Errorf("remove finalizer: %w", err)
    }

    logger.Info("deletion complete", "name", db.Name)
    return ctrl.Result{}, nil
}

func (r *DatabaseClusterReconciler) cleanupExternalResources(
    ctx context.Context,
    db *databasev1alpha1.DatabaseCluster,
) error {
    // 1. 通知监控系统删除 dashboard
    // 2. 清理 S3 备份（根据策略，可能只删元数据）
    // 3. 删除外部 DNS 记录
    // 这里的操作必须是幂等的，因为可能被重试多次
    return nil
}
```

---

## 生产化

### Leader Election

多副本 Operator 必须启用 Leader Election，防止多个实例同时 Reconcile 造成竞争。

`main.go` 中配置：

```go
mgr, err := ctrl.NewManager(ctrl.GetConfigOrDie(), ctrl.Options{
    Scheme: scheme,
    Metrics: metricsserver.Options{
        BindAddress: ":8080",
    },
    HealthProbeBindAddress: ":8081",
    LeaderElection:         true,
    LeaderElectionID:       "database-operator.example.com",
    // Leader Election 使用 ConfigMap/Lease，需要相应 RBAC
    LeaderElectionNamespace: "database-operator-system",

    // 缓存配置：只缓存特定 Namespace 的资源，减少内存
    Cache: cache.Options{
        DefaultNamespaces: map[string]cache.Config{
            // 空 map 表示 watch 所有 namespace
        },
    },
})
```

### 指标暴露

controller-runtime 内置 Prometheus 指标（Reconcile 耗时、错误率、队列深度）。自定义业务指标：

```go
package metrics

import (
    "github.com/prometheus/client_golang/prometheus"
    "sigs.k8s.io/controller-runtime/pkg/metrics"
)

var (
    DatabaseClustersTotal = prometheus.NewGaugeVec(
        prometheus.GaugeOpts{
            Name: "database_clusters_total",
            Help: "Total number of DatabaseCluster objects by phase",
        },
        []string{"namespace", "phase"},
    )

    ReconcileDuration = prometheus.NewHistogramVec(
        prometheus.HistogramOpts{
            Name:    "database_cluster_reconcile_duration_seconds",
            Help:    "Duration of DatabaseCluster reconcile in seconds",
            Buckets: prometheus.DefBuckets,
        },
        []string{"namespace", "result"},
    )
)

func init() {
    metrics.Registry.MustRegister(DatabaseClustersTotal, ReconcileDuration)
}
```

在 Reconcile 函数开头记录：

```go
func (r *DatabaseClusterReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
    start := time.Now()
    defer func() {
        duration := time.Since(start).Seconds()
        metrics.ReconcileDuration.WithLabelValues(req.Namespace, "success").Observe(duration)
    }()
    // ...
}
```

### Webhook 验证

生成 Webhook 脚手架：

```bash
kubebuilder create webhook \
  --group database \
  --version v1alpha1 \
  --kind DatabaseCluster \
  --defaulting --programmatic-validation
```

实现 `api/v1alpha1/databasecluster_webhook.go`：

```go
func (r *DatabaseCluster) ValidateCreate() (admission.Warnings, error) {
    return r.validateDatabaseCluster()
}

func (r *DatabaseCluster) ValidateUpdate(old runtime.Object) (admission.Warnings, error) {
    oldDB := old.(*DatabaseCluster)

    // 不允许修改 Engine
    if r.Spec.Engine != oldDB.Spec.Engine {
        return nil, field.Invalid(
            field.NewPath("spec", "engine"),
            r.Spec.Engine,
            "engine is immutable after creation",
        )
    }

    // 不允许缩容（数据库缩容需要手动操作）
    if r.Spec.Replicas < oldDB.Spec.Replicas {
        return admission.Warnings{
            "Reducing replicas may cause data loss, ensure manual data migration first",
        }, nil
    }

    return r.validateDatabaseCluster()
}

func (r *DatabaseCluster) validateDatabaseCluster() (admission.Warnings, error) {
    var allErrs field.ErrorList

    // 验证版本格式（额外的运行时校验，CRD 正则不够用时）
    validVersions := map[string][]string{
        "mysql":      {"8.0.36", "8.0.37", "8.4.0"},
        "postgresql": {"15.6", "16.2", "16.3"},
    }
    versions, ok := validVersions[r.Spec.Engine]
    if !ok {
        allErrs = append(allErrs, field.Invalid(
            field.NewPath("spec", "engine"), r.Spec.Engine, "unsupported engine"))
    } else {
        found := false
        for _, v := range versions {
            if v == r.Spec.Version {
                found = true
                break
            }
        }
        if !found {
            allErrs = append(allErrs, field.Invalid(
                field.NewPath("spec", "version"), r.Spec.Version,
                fmt.Sprintf("unsupported version for %s, valid: %v", r.Spec.Engine, versions)))
        }
    }

    if len(allErrs) > 0 {
        return nil, allErrs.ToAggregate()
    }
    return nil, nil
}
```

---

## 测试

### envtest 单元测试

envtest 启动真实的 `kube-apiserver` 和 `etcd`（二进制），不需要真实集群：

```go
// internal/controller/suite_test.go
package controller_test

import (
    "context"
    "path/filepath"
    "testing"

    . "github.com/onsi/ginkgo/v2"
    . "github.com/onsi/gomega"
    "k8s.io/client-go/rest"
    ctrl "sigs.k8s.io/controller-runtime"
    "sigs.k8s.io/controller-runtime/pkg/envtest"

    databasev1alpha1 "github.com/example/database-operator/api/v1alpha1"
)

var (
    cfg       *rest.Config
    ctx       context.Context
    cancel    context.CancelFunc
    testEnv   *envtest.Environment
)

func TestControllers(t *testing.T) {
    RegisterFailHandler(Fail)
    RunSpecs(t, "Controller Suite")
}

var _ = BeforeSuite(func() {
    ctx, cancel = context.WithCancel(context.TODO())

    testEnv = &envtest.Environment{
        CRDDirectoryPaths: []string{
            filepath.Join("..", "..", "config", "crd", "bases"),
        },
        ErrorIfCRDPathMissing: true,
    }

    var err error
    cfg, err = testEnv.Start()
    Expect(err).NotTo(HaveOccurred())

    err = databasev1alpha1.AddToScheme(scheme)
    Expect(err).NotTo(HaveOccurred())

    mgr, err := ctrl.NewManager(cfg, ctrl.Options{Scheme: scheme})
    Expect(err).NotTo(HaveOccurred())

    err = (&DatabaseClusterReconciler{
        Client: mgr.GetClient(),
        Scheme: mgr.GetScheme(),
    }).SetupWithManager(mgr)
    Expect(err).NotTo(HaveOccurred())

    go func() {
        defer GinkgoRecover()
        err = mgr.Start(ctx)
        Expect(err).NotTo(HaveOccurred())
    }()
})

var _ = AfterSuite(func() {
    cancel()
    Expect(testEnv.Stop()).To(Succeed())
})
```

```go
// internal/controller/databasecluster_controller_test.go
var _ = Describe("DatabaseCluster controller", func() {
    Context("When creating a DatabaseCluster", func() {
        It("should create a StatefulSet", func() {
            db := &databasev1alpha1.DatabaseCluster{
                ObjectMeta: metav1.ObjectMeta{
                    Name:      "test-mysql",
                    Namespace: "default",
                },
                Spec: databasev1alpha1.DatabaseClusterSpec{
                    Engine:   "mysql",
                    Replicas: 3,
                    Version:  "8.0.36",
                    Storage: databasev1alpha1.StorageSpec{
                        Size: resource.MustParse("10Gi"),
                    },
                    PasswordSecretRef: corev1.SecretKeySelector{
                        LocalObjectReference: corev1.LocalObjectReference{Name: "mysql-password"},
                        Key: "password",
                    },
                },
            }
            Expect(k8sClient.Create(ctx, db)).To(Succeed())

            // 等待 StatefulSet 创建
            sts := &appsv1.StatefulSet{}
            Eventually(func() error {
                return k8sClient.Get(ctx, types.NamespacedName{
                    Name: "test-mysql", Namespace: "default",
                }, sts)
            }, "10s", "1s").Should(Succeed())

            Expect(*sts.Spec.Replicas).To(Equal(int32(3)))

            // 等待 Status 更新
            Eventually(func() string {
                _ = k8sClient.Get(ctx, types.NamespacedName{
                    Name: "test-mysql", Namespace: "default",
                }, db)
                return db.Status.Phase
            }, "15s", "1s").Should(Equal(databasev1alpha1.PhaseInitializing))
        })
    })
})
```

### Kind 集成测试

```bash
# 安装 Kind
go install sigs.k8s.io/kind@latest

# 创建测试集群
kind create cluster --name operator-test --config kind-config.yaml

# 安装 CRD 和 Operator
make install  # 安装 CRD
make deploy IMG=database-operator:test  # 部署 Operator

# 运行端到端测试
go test ./test/e2e/... -v -timeout 10m

# 清理
kind delete cluster --name operator-test
```

`kind-config.yaml`：

```yaml
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
  - role: worker
  - role: worker
```

---

## 部署到集群：RBAC 权限设计

kubebuilder 通过 `// +kubebuilder:rbac:` 注释自动生成 RBAC。生成命令：

```bash
make manifests  # 更新 config/rbac/role.yaml
```

生成的 `ClusterRole`（精简版）：

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: database-operator-manager-role
rules:
  # 核心：操作自定义资源
  - apiGroups: ["database.example.com"]
    resources: ["databaseclusters"]
    verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
  - apiGroups: ["database.example.com"]
    resources: ["databaseclusters/status"]
    verbs: ["get", "update", "patch"]
  - apiGroups: ["database.example.com"]
    resources: ["databaseclusters/finalizers"]
    verbs: ["update"]
  # 管理 StatefulSet
  - apiGroups: ["apps"]
    resources: ["statefulsets"]
    verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
  # 读写 Service、ConfigMap、Secret
  - apiGroups: [""]
    resources: ["services", "configmaps"]
    verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
  - apiGroups: [""]
    resources: ["secrets"]
    verbs: ["get", "list", "watch"]  # 只读密码，不写
  # 读 Pod 状态
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["get", "list", "watch"]
  # Leader Election 用的 Lease
  - apiGroups: ["coordination.k8s.io"]
    resources: ["leases"]
    verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
  # 发送 Event
  - apiGroups: [""]
    resources: ["events"]
    verbs: ["create", "patch"]
```

**最小权限原则**：
- `secrets` 只给 `get/list/watch`，不给 `create/update`，防止 Operator 被利用创建特权 Secret
- 不给 `ClusterRole` 的 `create/update` 权限，防止权限提升
- 如果 Operator 只管理特定 Namespace，用 `Role + RoleBinding` 替代 `ClusterRole + ClusterRoleBinding`

```bash
# 部署
make deploy IMG=registry.example.com/database-operator:v0.1.0

# 验证
kubectl get pods -n database-operator-system
kubectl get crds | grep database.example.com

# 创建测试实例
kubectl apply -f - <<EOF
apiVersion: database.example.com/v1alpha1
kind: DatabaseCluster
metadata:
  name: my-mysql
  namespace: default
spec:
  engine: mysql
  version: "8.0.36"
  replicas: 3
  storage:
    size: 20Gi
    storageClassName: fast-ssd
  passwordSecretRef:
    name: mysql-root-password
    key: password
  backup:
    schedule: "0 2 * * *"
    retention: 7
    s3Bucket: my-db-backups
EOF

# 查看状态
kubectl get databasecluster my-mysql
kubectl describe databasecluster my-mysql
# 观察 Conditions 字段，Ready/Degraded 变化清晰可追踪
```

---

## 几个容易踩坑的地方

**1. 不要直接修改从 cache 中 Get 到的对象**

`r.Get()` 返回的对象是缓存的引用，直接修改会污染缓存。修改前必须 `DeepCopy()`：

```go
// 错误
db.Status.Phase = "Running"
r.Status().Update(ctx, db)  // 可能导致缓存脏数据

// 正确
dbCopy := db.DeepCopy()
dbCopy.Status.Phase = "Running"
r.Status().Update(ctx, dbCopy)
```

**2. Reconcile 必须是幂等的**

Reconcile 会被多次触发（重启、网络抖动、定时 Resync），每次执行结果必须一致。用 `CreateOrUpdate` 而不是 `Create`，用 `Apply` 而不是 `Replace`。

**3. 区分 Spec 更新和 Status 更新**

`r.Update()` 更新 Spec，触发 Generation 增加，进而触发新的 Reconcile。
`r.Status().Update()` 只更新 Status 子资源，不增加 Generation，不触发 Reconcile。
两者不要混用。

**4. 处理 Conflict 错误**

并发 Reconcile 可能导致 `Conflict` 错误（ResourceVersion 不匹配）。正确处理方式：

```go
if errors.IsConflict(err) {
    // 重新 Requeue，不要打印 Error 日志（这是正常情况）
    return ctrl.Result{Requeue: true}, nil
}
```

**5. Watch 关联资源**

默认 Reconcile 只监听 DatabaseCluster 的变化。要让 StatefulSet 的变化也触发 Reconcile，需要在 `SetupWithManager` 中配置：

```go
func (r *DatabaseClusterReconciler) SetupWithManager(mgr ctrl.Manager) error {
    return ctrl.NewControllerManagedBy(mgr).
        For(&databasev1alpha1.DatabaseCluster{}).
        Owns(&appsv1.StatefulSet{}).   // Watch 自己创建的 StatefulSet
        Owns(&corev1.Service{}).
        // Watch 其他 Namespace 的 Secret 变化（不 Own 但需要响应）
        Watches(
            &corev1.Secret{},
            handler.EnqueueRequestsFromMapFunc(r.findDatabasesForSecret),
            builder.WithPredicates(predicate.ResourceVersionChangedPredicate{}),
        ).
        WithOptions(controller.Options{
            MaxConcurrentReconciles: 5,  // 并发 Reconcile 数
        }).
        Complete(r)
}
```
