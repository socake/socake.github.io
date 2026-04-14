---
title: "自研 Kubernetes Admission Webhook 开发实战：从零到生产"
date: 2025-04-12T11:00:00+08:00
draft: false
tags: ["Kubernetes", "Admission Webhook", "ValidatingAdmissionPolicy", "CEL"]
categories: ["Kubernetes"]
description: "写一个真正生产可用的 Kubernetes Admission Webhook：ValidatingWebhook / MutatingWebhook 的区别、webhook 的执行顺序、certificate 的生命周期、CEL 替代方案、failurePolicy 的安全边界、性能、集成测试、以及什么时候你应该放弃 webhook 改用 ValidatingAdmissionPolicy。"
summary: "Kubernetes 的 admission 体系是一个强大但脆弱的扩展点。webhook 挂了能让集群所有 Pod 创建卡死。写一个能上生产的 webhook 不难，但要让它在面对各种怪异请求、证书轮换、集群升级、大流量突发时都不挂，就是另一回事了。这是一份从零到生产的工程笔记。"
toc: true
math: false
diagram: false
keywords: ["admission webhook", "MutatingAdmissionWebhook", "ValidatingAdmissionPolicy", "Kubernetes extension", "CEL"]
params:
  reading_time: true
---

## 为什么还要自己写 webhook

Kubernetes 1.30 把 ValidatingAdmissionPolicy (VAP) GA 了，用 CEL (Common Expression Language) 在 kube-apiserver 里直接跑校验逻辑，不用 webhook。大多数"字段校验"类需求可以直接用 VAP 解决，不用再写 webhook。

那为什么还要讲 webhook？

1. **mutation 还得 webhook**：VAP 当前只做 validating。要做 mutating（注入 sidecar、改 labels、设置 resource requests 默认值等），目前还只能 webhook。Kubernetes 1.33 引入了 MutatingAdmissionPolicy 的实验性支持，但离 GA 还早，生产别用。
2. **外部信息依赖**：VAP 是 in-process 的 CEL，不能调外部 API。如果你的校验逻辑要调 Vault 查密钥、调 CMDB 查应用 metadata、访问数据库——只能 webhook。
3. **复杂的条件逻辑**：CEL 能表达不少东西，但遇到"多资源联动"或者"需要 cache 上下文"的场景，CEL 写起来非常难看。
4. **对老 Kubernetes 兼容**：VAP GA 在 1.30，你的集群如果还是 1.28/1.29，只能 webhook。

所以现实是：**能 VAP 就 VAP，搞不定的才 webhook**。这篇讲 webhook 怎么写好，并且在适当的时候告诉你"这里应该用 VAP"。

## Admission 链路回顾

一个 kubectl apply 的请求到 kube-apiserver 后，大概走这么一条路：

```
  kubectl apply
       │
       ▼
  kube-apiserver
       │
   1. 认证 (authentication)
       │
   2. 授权 (authorization)
       │
   3. Mutating Admission (顺序: built-in → MutatingAdmissionPolicy → MutatingWebhook)
       │
   4. Object schema validation
       │
   5. Validating Admission (built-in → ValidatingAdmissionPolicy → ValidatingWebhook)
       │
   6. etcd 写入
```

两个 admission 阶段之间有严格顺序：
- **Mutating 先**：可以改 object 内容；
- **Validating 后**：只能接受或拒绝，不能改。

webhook 是最后执行的，在 built-in 和 policy 之后。这意味着你的 webhook 看到的 object 已经被其他 plugin 改过了。

## Webhook 的两种类型

### MutatingAdmissionWebhook

可以修改请求对象。典型用途：

- 注入 sidecar 容器（Istio、Linkerd、kmesh）；
- 给 Pod 加 label 或 annotation；
- 自动设置 resource requests / limits；
- 注入 imagePullSecrets；
- 改 nodeSelector 让 Pod 落到特定节点池。

返回值是 JSON Patch 或者 JSON Merge Patch。

### ValidatingAdmissionWebhook

只能决定接受或拒绝。典型用途：

- 校验 image 必须来自内部 registry；
- 禁止某些 annotation / label 的组合；
- 要求每个 Deployment 必须设置 resource limits；
- 检查 PVC 大小不超过配额；
- 防止删除特定资源（删 namespace 前先检查空）。

实际生产中两种经常一起写。一个 webhook 进程里同时注册 mutating 和 validating 路径。

## 一个最小的 webhook：Go 实现

Go 是写 webhook 的主流语言（因为和 client-go / apimachinery 的类型对齐最好）。一个最小 mutating webhook：

```go
package main

import (
    "context"
    "encoding/json"
    "fmt"
    "net/http"

    admissionv1 "k8s.io/api/admission/v1"
    corev1 "k8s.io/api/core/v1"
    metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
    "k8s.io/apimachinery/pkg/runtime"
    "k8s.io/apimachinery/pkg/runtime/serializer"
)

var (
    scheme       = runtime.NewScheme()
    codecs       = serializer.NewCodecFactory(scheme)
    deserializer = codecs.UniversalDeserializer()
)

func init() {
    _ = corev1.AddToScheme(scheme)
    _ = admissionv1.AddToScheme(scheme)
}

type patchOp struct {
    Op    string      `json:"op"`
    Path  string      `json:"path"`
    Value interface{} `json:"value,omitempty"`
}

func mutatePods(w http.ResponseWriter, r *http.Request) {
    body := make([]byte, r.ContentLength)
    if _, err := r.Body.Read(body); err != nil && err.Error() != "EOF" {
        http.Error(w, err.Error(), http.StatusBadRequest)
        return
    }

    ar := admissionv1.AdmissionReview{}
    if _, _, err := deserializer.Decode(body, nil, &ar); err != nil {
        http.Error(w, err.Error(), http.StatusBadRequest)
        return
    }

    req := ar.Request
    var pod corev1.Pod
    if err := json.Unmarshal(req.Object.Raw, &pod); err != nil {
        http.Error(w, err.Error(), http.StatusBadRequest)
        return
    }

    var patches []patchOp

    // 为没有 resource requests 的容器设置默认
    for i, c := range pod.Spec.Containers {
        if c.Resources.Requests == nil {
            patches = append(patches, patchOp{
                Op:   "add",
                Path: fmt.Sprintf("/spec/containers/%d/resources/requests", i),
                Value: map[string]string{
                    "cpu":    "100m",
                    "memory": "128Mi",
                },
            })
        }
    }

    patchBytes, _ := json.Marshal(patches)
    pt := admissionv1.PatchTypeJSONPatch

    resp := admissionv1.AdmissionReview{
        TypeMeta: metav1.TypeMeta{
            APIVersion: "admission.k8s.io/v1",
            Kind:       "AdmissionReview",
        },
        Response: &admissionv1.AdmissionResponse{
            UID:       req.UID,
            Allowed:   true,
            Patch:     patchBytes,
            PatchType: &pt,
        },
    }

    out, _ := json.Marshal(resp)
    w.Header().Set("Content-Type", "application/json")
    w.Write(out)
}

func main() {
    http.HandleFunc("/mutate-pods", mutatePods)
    server := &http.Server{
        Addr: ":8443",
    }
    _ = server.ListenAndServeTLS("/tls/tls.crt", "/tls/tls.key")
}
```

这是能跑的最小版本。它做了一件事：给没有 resource requests 的容器加默认 100m/128Mi。

但这个代码离生产还差十万八千里。让我们一项项补。

## 证书生命周期

Kubernetes 调 webhook 必须是 HTTPS。kube-apiserver 会验证 webhook 的证书是否由它信任的 CA 签发。

**三种证书方案**：

### 方案 1：自签名 CA + 手工管理

最原始。写个脚本生成 CA + webhook cert，然后把 CA 填到 `webhookConfiguration.webhooks[].clientConfig.caBundle`。

缺点：证书到期就得手动续，经常被忘记。千万别选。

### 方案 2：cert-manager 管理

用 cert-manager 签发 webhook 证书。示例：

```yaml
apiVersion: cert-manager.io/v1
kind: Issuer
metadata:
  name: selfsigned-issuer
  namespace: webhook-system
spec:
  selfSigned: {}
---
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: webhook-cert
  namespace: webhook-system
spec:
  secretName: webhook-tls
  dnsNames:
    - webhook-service.webhook-system.svc
    - webhook-service.webhook-system.svc.cluster.local
  issuerRef:
    name: selfsigned-issuer
```

然后 MutatingWebhookConfiguration 用 annotation 引用：

```yaml
apiVersion: admissionregistration.k8s.io/v1
kind: MutatingWebhookConfiguration
metadata:
  name: pod-defaults
  annotations:
    cert-manager.io/inject-ca-from: webhook-system/webhook-cert
```

cert-manager 有个 `cainjector` controller，看到这个 annotation 会把 CA 证书自动注入到 `caBundle` 字段。证书到期前自动续期。

这是生产最推荐的方式。简单、有续期、和 cert-manager 标准运维对齐。

### 方案 3：Kubernetes API 自签

通过 Kubernetes CSR API 请求集群 CA 签发证书。`controller-runtime` 的 webhook server 支持这个模式。

这个方案的好处：不依赖 cert-manager。坏处：证书轮换要你自己写代码。

除非你不能装 cert-manager，方案 2 是最好的。

## WebhookConfiguration 的关键字段

一个完整的 MutatingWebhookConfiguration：

```yaml
apiVersion: admissionregistration.k8s.io/v1
kind: MutatingWebhookConfiguration
metadata:
  name: pod-defaults
  annotations:
    cert-manager.io/inject-ca-from: webhook-system/webhook-cert
webhooks:
  - name: pod-defaults.example.com
    clientConfig:
      service:
        name: webhook-service
        namespace: webhook-system
        path: /mutate-pods
        port: 443
    rules:
      - apiGroups: [""]
        apiVersions: ["v1"]
        resources: ["pods"]
        operations: ["CREATE"]
        scope: Namespaced
    admissionReviewVersions: ["v1"]
    sideEffects: None
    failurePolicy: Fail
    timeoutSeconds: 10
    reinvocationPolicy: IfNeeded
    namespaceSelector:
      matchExpressions:
        - key: admission.example.com/skip
          operator: DoesNotExist
    objectSelector:
      matchExpressions:
        - key: app.kubernetes.io/managed-by
          operator: NotIn
          values: ["helm"]
```

字段详解：

### failurePolicy

webhook 不可达时怎么办：

- `Ignore`：忽略错误，请求照常通过；
- `Fail`：直接拒绝请求。

**这是所有生产 webhook 最关键的字段**。选错能让集群全局瘫痪。

原则：

- **能 Ignore 就 Ignore**：比如注入 sidecar 这种非安全相关的 mutating，webhook 挂了不应该阻塞所有 Pod 创建。
- **必须 Fail 的场景**：安全策略校验（禁止 root 容器、禁止外部 image），不允许 bypass。
- **Fail 的 webhook 必须有 namespaceSelector 排除核心 namespace**：不然 kube-system 的 Pod 都起不来。

### namespaceSelector / objectSelector

限定 webhook 只对哪些 namespace / object 生效。

**生产必须做的**：排除 kube-system、kube-public、webhook 自己所在的 namespace。否则 webhook 还没起来，它自己依赖的组件先崩。

```yaml
namespaceSelector:
  matchExpressions:
    - key: kubernetes.io/metadata.name
      operator: NotIn
      values:
        - kube-system
        - kube-public
        - webhook-system
```

或者更保守的 opt-in：

```yaml
namespaceSelector:
  matchLabels:
    webhook.example.com/enabled: "true"
```

然后给要启用 webhook 的 namespace 打 label。这是"最安全"的策略。

### sideEffects

告诉 kube-apiserver 你的 webhook 会不会产生副作用（比如调外部 API 改别的资源）：

- `None`：无副作用。推荐。
- `NoneOnDryRun`：dry-run 模式下没副作用。
- `Some`：有副作用（kubectl apply --dry-run 时会被拒绝执行）。

除非你真的要做副作用的事（一般不建议），否则一律 `None`。

### timeoutSeconds

webhook 响应超时。默认 10 秒，最大 30 秒。**生产建议 5-10 秒**，太长会让 apiserver 的请求堆积。

### reinvocationPolicy

mutating webhook 专有。当多个 mutating webhook 改动同一个对象时，你的 webhook 是否需要被"再次调用"一次，看其他 webhook 改动后的结果？

- `Never`：只调一次；
- `IfNeeded`：如果其他 webhook 在你之后改了对象，你会被再调一次。

`IfNeeded` 更安全但更慢。默认 `Never`。大多数场景 `Never` 就够。

### admissionReviewVersions

支持的 AdmissionReview API 版本。生产用 `["v1"]`，v1beta1 已经被 kube-apiserver 1.22+ 移除。

## 避免"打死自己"

webhook 最可怕的故障模式是：**webhook 挂了，导致所有 Pod 创建失败，包括 webhook 自己的 Pod**。然后整个集群无法自救。

几条铁律：

1. **webhook 的 Deployment 部署在一个专门的 namespace**（比如 `webhook-system`），给这个 namespace 打 label 排除在 webhook 之外；
2. **webhook 的 Pod 用 PriorityClass `system-cluster-critical`**，保证被优先调度；
3. **webhook 的 Deployment 至少 2 副本 + PDB**，保证不会同时全挂；
4. **readinessProbe / livenessProbe 要能正确反映 webhook 健康**；
5. **Service 用 topologyAwareRoutingTopologyKeys 不要**，因为 webhook Pod 可能只在一个 zone；
6. **webhook 请求路径要极快**：< 100ms，永远不要做任何 blocking I/O。

示例 Deployment：

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: webhook
  namespace: webhook-system
spec:
  replicas: 3
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxUnavailable: 0
      maxSurge: 1
  template:
    spec:
      priorityClassName: system-cluster-critical
      topologySpreadConstraints:
        - maxSkew: 1
          topologyKey: topology.kubernetes.io/zone
          whenUnsatisfiable: ScheduleAnyway
          labelSelector:
            matchLabels:
              app: webhook
      containers:
        - name: webhook
          image: registry.example.com/webhook:1.0.0
          ports:
            - containerPort: 8443
          readinessProbe:
            httpGet:
              path: /healthz
              port: 8443
              scheme: HTTPS
            periodSeconds: 5
          livenessProbe:
            httpGet:
              path: /healthz
              port: 8443
              scheme: HTTPS
            periodSeconds: 10
          resources:
            requests:
              cpu: 100m
              memory: 128Mi
            limits:
              cpu: 1
              memory: 512Mi
          volumeMounts:
            - name: tls
              mountPath: /tls
              readOnly: true
      volumes:
        - name: tls
          secret:
            secretName: webhook-tls
---
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: webhook-pdb
  namespace: webhook-system
spec:
  minAvailable: 2
  selector:
    matchLabels:
      app: webhook
```

## controller-runtime 的 webhook 框架

裸写 HTTP handler 非常繁琐。推荐用 `sigs.k8s.io/controller-runtime/pkg/webhook`：

```go
package main

import (
    "context"
    "fmt"

    corev1 "k8s.io/api/core/v1"
    ctrl "sigs.k8s.io/controller-runtime"
    "sigs.k8s.io/controller-runtime/pkg/webhook"
    "sigs.k8s.io/controller-runtime/pkg/webhook/admission"
)

type PodDefaulter struct{}

func (d *PodDefaulter) Default(ctx context.Context, obj runtime.Object) error {
    pod := obj.(*corev1.Pod)
    for i := range pod.Spec.Containers {
        c := &pod.Spec.Containers[i]
        if c.Resources.Requests == nil {
            c.Resources.Requests = corev1.ResourceList{
                corev1.ResourceCPU:    resource.MustParse("100m"),
                corev1.ResourceMemory: resource.MustParse("128Mi"),
            }
        }
    }
    return nil
}

func main() {
    mgr, _ := ctrl.NewManager(ctrl.GetConfigOrDie(), ctrl.Options{})
    mgr.GetWebhookServer().Register("/mutate-pods", &webhook.Admission{
        Handler: admission.CustomDefaulter(&corev1.Pod{}, &PodDefaulter{}),
    })
    _ = mgr.Start(ctrl.SetupSignalHandler())
}
```

controller-runtime 帮你处理 AdmissionReview 解码、patch 生成、TLS、指标等等。生产写 webhook 用这套框架是标准做法。

## 测试 webhook

测试 webhook 要测三个层面：

### 1. 单元测试

直接 call 你的 `Default` / `ValidateCreate` 函数，断言输入输出。最简单最快。

```go
func TestDefault(t *testing.T) {
    d := &PodDefaulter{}
    pod := &corev1.Pod{
        Spec: corev1.PodSpec{
            Containers: []corev1.Container{{Name: "app"}},
        },
    }
    err := d.Default(context.TODO(), pod)
    assert.NoError(t, err)
    assert.Equal(t, "100m", pod.Spec.Containers[0].Resources.Requests.Cpu().String())
}
```

### 2. AdmissionReview 集成测试

模拟 kube-apiserver 发 AdmissionReview JSON，检查 response。用 `httptest`。

### 3. envtest / kind 端到端测试

用 controller-runtime 的 envtest 起一个 kube-apiserver + etcd，装你的 webhook，然后 apply 真实资源，断言行为。

```go
func TestWebhookE2E(t *testing.T) {
    testEnv := &envtest.Environment{}
    cfg, _ := testEnv.Start()
    defer testEnv.Stop()

    // ... install webhook, apply pod, check mutation
}
```

生产级 webhook 我会要求所有三层测试都覆盖。单元测试快、覆盖率高；envtest 能抓"我写的 webhook configuration 是不是对"的问题。

## dry-run 支持

kubectl apply --dry-run=server 会把请求打到 kube-apiserver，apiserver 会执行所有 admission 包括 webhook，但不写 etcd。你的 webhook 应该正确处理 dry-run：

```go
if req.DryRun != nil && *req.DryRun {
    // 不做任何带副作用的事（比如调 Vault 写 secret）
}
```

对纯校验 / mutation 的 webhook 影响不大，对"会调外部 API 改东西"的 webhook 非常重要。

## 性能：webhook 在请求链路上

每次 Pod 创建都会走你的 webhook。一个中等集群每秒可能几百次 Pod 创建（滚动升级、批处理任务、CI）。webhook 的延迟直接变成 apiserver 延迟。

几个性能原则：

1. **不要同步调外部系统**。webhook 本体只读 local cache。如果必须查外部，用 goroutine + cache + TTL。
2. **不要加 mutex / global lock**。高并发时会被队列打穿。
3. **日志不要太多**。每次请求打十几条 log 会让日志组件崩溃，webhook 也慢。
4. **JSON Patch 越小越好**。一个 patch 里改十几个字段比一次性写一个大 merge patch 好得多。
5. **用 gRPC + JSON 都可以，但 kube-apiserver 调 webhook 走 HTTP/JSON**——别想换协议。

一个我用过的技巧：webhook 里不做 ConfigMap 查询，而是用 informer 把配置常驻内存，通过 watch 更新。这样每次 webhook 请求都是 O(1) 的 map lookup。

## 观测

webhook 的 Prometheus metrics 重点：

- `apiserver_admission_webhook_admission_duration_seconds`（apiserver 侧）：apiserver 看到的 webhook 响应时间；
- `apiserver_admission_webhook_rejection_count`（apiserver 侧）：webhook 拒绝了多少请求；
- 你自己的 webhook 也要暴露：`webhook_admission_requests_total`、`webhook_admission_duration_seconds`、`webhook_admission_errors_total`。

核心告警：

```yaml
- alert: WebhookLatencyHigh
  expr: |
    histogram_quantile(0.99,
      sum by (le, name) (
        rate(apiserver_admission_webhook_admission_duration_seconds_bucket[5m])
      )
    ) > 1
  for: 10m
  labels:
    severity: warning
  annotations:
    summary: "Webhook {{ $labels.name }} P99 延迟超过 1s"

- alert: WebhookErrorRate
  expr: |
    sum by (name) (rate(apiserver_admission_webhook_admission_duration_seconds_count{rejected="true"}[5m])) > 1
  labels:
    severity: warning
```

latency 是最重要的指标。如果 webhook P99 超过 1 秒，apiserver 整体响应时间会被拉垮。

## 升级 webhook 的谨慎

升级 webhook 本身是件危险事：

1. **灰度**：先上 dev，再 staging，再 prod；
2. **Never recreate**：Deployment 升级用 RollingUpdate + maxUnavailable=0，不能让 webhook 出现"全部 Pod 都 not ready" 的时刻，不然 failurePolicy=Fail 会打死集群；
3. **证书提前验证**：cert-manager 的证书快到期时提前续，别在最后一天续然后 cert 有问题；
4. **回滚准备**：准备好"临时 patch 掉 MutatingWebhookConfiguration 的 failurePolicy=Ignore" 的应急操作。这是紧急自救。

## 什么时候应该用 ValidatingAdmissionPolicy 代替

Kubernetes 1.30 的 VAP 用 CEL 在 apiserver 进程内跑校验。对比 webhook：

| 维度 | Webhook | VAP (CEL) |
|---|---|---|
| 部署复杂度 | 需要 Pod / Service / 证书 | 只是 CRD |
| 性能 | 每次请求 HTTPS 往返 | 进程内 CEL |
| 可用性 | webhook 挂 = 集群挂 | 和 apiserver 同生命周期 |
| 可扩展性 | 任意代码逻辑 | 只能 CEL |
| 外部依赖 | 可以调任何 API | 不能调外部 |
| 调试 | 可以 kubectl logs | CEL 报错较难定位 |
| Mutation | 支持 | 不支持（1.33 实验） |

**简单规则**：

- 只是字段校验（image 前缀、label 存在、resource 有没有设）→ **用 VAP**
- 需要 mutation → **用 webhook**
- 需要调外部系统 → **用 webhook**

一个 VAP 例子，禁止使用 `latest` tag：

```yaml
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingAdmissionPolicy
metadata:
  name: no-latest-tag
spec:
  failurePolicy: Fail
  matchConstraints:
    resourceRules:
      - apiGroups: [""]
        apiVersions: ["v1"]
        operations: ["CREATE", "UPDATE"]
        resources: ["pods"]
  validations:
    - expression: "object.spec.containers.all(c, !c.image.endsWith(':latest') && c.image.contains(':'))"
      message: "image tag is required and must not be latest"
---
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingAdmissionPolicyBinding
metadata:
  name: no-latest-tag-binding
spec:
  policyName: no-latest-tag
  validationActions: [Deny]
  matchResources:
    namespaceSelector:
      matchExpressions:
        - key: vap.example.com/enabled
          operator: Exists
```

短、快、无依赖。如果我早两年能用 VAP，我会把至少一半 validating webhook 都迁过去。

## 真实生产中的几个案例

### 案例 1：image registry 白名单

需求：禁止 Pod 使用外部 registry 的 image，必须是 `registry.example.com/`。

早期用 webhook 实现。现在用 VAP 就行：

```yaml
validations:
  - expression: "object.spec.containers.all(c, c.image.startsWith('registry.example.com/'))"
    message: "image must be from internal registry"
```

### 案例 2：sidecar 注入

需求：给带 `sidecar.example.com/inject=true` 的 Pod 自动注入一个监控 sidecar。

只能 webhook，因为要 mutation。注意点：

- 注入的 sidecar 本身依赖外部服务时，sidecar 所在 namespace 要能访问；
- 注入 sidecar 本身不能触发 webhook 再次调用自己（防止循环）——用 `reinvocationPolicy: Never`；
- 被注入的 Pod 删除时 sidecar 不需要"反注入"。

### 案例 3：PVC 大小上限

需求：禁止单个 PVC 大于 1TB（防止 dev 写错单位造数据量爆炸）。

VAP 能做：

```yaml
validations:
  - expression: "object.spec.resources.requests.storage <= quantity('1Ti')"
    message: "PVC cannot exceed 1Ti"
```

### 案例 4：基于 Vault 的密钥注入

需求：Pod 的 `vault.example.com/inject=role-xxx` 注解触发从 Vault 拉密钥，生成 Secret 并注入到 Pod env。

必须 webhook。调 Vault 是 external API call，VAP 做不了。注意：

- 调 Vault 要有 timeout (1-2 秒)；
- 失败要 graceful：webhook 里不阻塞太久，直接 deny 请求让用户重试比卡死好；
- 对 namespace 做 opt-in，不要所有 namespace 都触发。

## 踩过的几个坑

### 坑 1：时间飘导致证书无效

kube-apiserver 的时间和 webhook Pod 的时间不一致（一个飘了 5 分钟），cert not yet valid。解决：所有 node NTP 严格同步。

### 坑 2：webhook Service 的 ClusterIP 改变

Service 删掉重建 ClusterIP 变了，但 webhookConfiguration 里写的是 Service 名字（通过 CoreDNS 解析）——正常情况下没问题。但如果你写的是硬编码 IP 就会挂。教训：永远用 Service name。

### 坑 3：namespaceSelector 忘了排除自己

webhook 自己所在的 namespace 没排除，导致 webhook Pod 创建时要调用 webhook 自己，死锁。第一次 Pod 永远起不来。

教训：webhook 所在 namespace 打一个 label 比如 `admission.example.com/skip=true`，namespaceSelector 里显式排除。

### 坑 4：Mutating 写 patch 路径错

JSON Patch 的 path 写错。比如 `/spec/containers/-` 是 "append to array"，而 `/spec/containers/0/resources` 是 "第 0 个容器的 resources"。写成 `/spec/containers/0/resources/requests` 但父级不存在的话 patch 会失败。

教训：每次 patch 前先检查父路径存在，用 `add` 而不是 `replace`。

### 坑 5：kube-apiserver 升级导致 AdmissionReview 格式变化

v1beta1 已经被移除了。如果你的 webhook 只支持 v1beta1，升级后所有请求都失败。永远支持 `v1` 为主。

### 坑 6：慢查询把 webhook 拖垮

某个 validating webhook 里调了一次外部数据库查询，平时 50ms，数据库抖动时变 5 秒。webhook 请求堆积，然后 apiserver 请求堆积，集群 API 几乎不可用。

教训：webhook 里**永远不要同步调外部系统**。如果必须，严格 timeout (< 500ms) + fallback。

## 最后的几条原则

- 能用 VAP 就用 VAP，validating webhook 的新需求默认先考虑 VAP；
- Mutating webhook 仍然只能 webhook；
- failurePolicy 的选择是最重要的决策；
- namespaceSelector / objectSelector 必须排除基础设施；
- 用 cert-manager 管证书；
- 用 controller-runtime 框架而不是裸写；
- 3 副本 + PDB + PriorityClass；
- 响应时间 < 100ms，永不调外部 API；
- 单元 + envtest + 集成三层测试；
- 升级用灰度 + 快速回滚方案；
- 监控延迟和拒绝率。

写 webhook 是一件"看起来简单但要写对很难"的活。简单的 demo 几十行 Go 就能跑，但要能扛住生产的各种边界，代码量会是初版的 5 倍以上。好在大部分团队其实并不需要写自己的 webhook——开源的 OPA Gatekeeper、Kyverno、jsPolicy 已经覆盖了 90% 的策略需求。只有在"业务逻辑太特别、通用 policy 引擎表达不了"时才有写自研 webhook 的必要。

到了那一步的话，这篇文章就是给你准备的。
