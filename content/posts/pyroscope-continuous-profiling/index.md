---
title: "Pyroscope 持续性能剖析生产实战：给每一行代码一个性能画像"
date: 2025-07-02T10:00:00+08:00
draft: false
tags: ["Pyroscope", "Profiling", "可观测性", "Go", "pprof"]
categories: ["可观测性"]
description: "Pyroscope 1.x 从 0 到 1 的生产落地笔记，覆盖架构、pprof 接入、eBPF 无侵入采集、Go/Java/Python 接入、存储成本、火焰图读法以及两次线上问题定位案例。"
summary: "为什么 metrics/logs/traces 之外还需要 profiling，它解决的是什么问题，Pyroscope 的架构是什么，怎样以 2%~5% overhead 把它铺到整个 K8s 集群。"
toc: true
math: false
diagram: false
keywords: ["Pyroscope", "pprof", "火焰图", "profiling", "性能分析"]
params:
  reading_time: true
---

## 为什么需要持续性能剖析

过去一年我带团队做了一件事：把 Pyroscope 铺到整个后端所有 Go / Java / Python 服务。之前我们能靠 Prometheus 看 QPS、latency、错误率，靠 Tempo 看某个请求的 span 时序，靠 Loki 看日志；但每当线上出现「这台 pod CPU 70% 但 latency 还不错，另一台 pod CPU 35% 却有零星 p99 毛刺」这类问题，我们只能现场抓 pprof、本地打火焰图、肉眼对比——效率极低。

持续性能剖析（continuous profiling）要解决的就是这个盲区。它的核心主张是：**profile 不是出问题时才抓一次，而是每个 pod 每天每秒都在被轻量采集，历史数据按时间轴存，任何时候都能回查**。

所谓轻量，是因为它用的是采样式 profiler，比如每秒 100 次（100Hz）抓一次调用栈，每 10 秒聚合一次上报，整体 overhead 大概 2%~5% CPU，这是业界验证过的数字。换来的价值是：

- 线上 p99 抖动了两分钟？调出那两分钟的 CPU 火焰图对比前后；
- 某次上线后内存慢慢涨？打开 alloc_space 的 diff 视图；
- 想知道整个公司哪个服务最烧 CPU？按 service 做 top，排序拿数据；
- 性能回归自动化：CI 里比对 merge 前后火焰图差值。

Pyroscope 是目前开源里最成熟的答案。这篇文章按生产视角把架构、接入、运维、案例讲清楚，给打算从零做持续剖析的团队一份参考。

## 一、持续剖析的基本概念

先把几个概念对齐，不然后面看配置和 UI 会懵。

### Profile type

Pyroscope 把 profile 类型标准化成 `type:subtype`：

- **`process_cpu:cpu:nanoseconds`**：CPU wall time（采样式）
- **`memory:alloc_space:bytes`**：分配的总字节数（累计）
- **`memory:alloc_objects:count`**：分配的对象数
- **`memory:inuse_space:bytes`**：当前仍在使用的内存
- **`goroutine:goroutine:count`**（Go 特有）：goroutine 数
- **`block:contentions:count`** / **`mutex:contentions:count`**：锁竞争
- **`process_cpu:samples:count`**（eBPF）：CPU 采样次数

每种类型的数据都是独立存储的 time series，所以查询时你会在 Grafana 的 Profile Explorer 里先选 profile type。

### Flame graph

Pyroscope 把每个采样周期内的调用栈聚合成 flame graph：宽度 = 被采样的次数（可以理解为耗费的资源），层级 = 调用路径。持续剖析的 Pyroscope 把时间维度叠上去：选一段时间范围，它把范围内所有样本合并成一张 flame graph；如果选两段时间，就能拿到 diff flame graph，红色表示变慢了，绿色表示变快。

### 采样 vs instrumented

所有 profiler 分两类：采样式（sampling，例如 Go pprof 的 CPU profile）和插桩式（instrumented，例如 Java async-profiler 的 wall-clock mode）。前者 overhead 低但有统计误差，后者精确但通常不适合长期生产。Pyroscope 默认用采样式，这是它 overhead 能压到 2%~5% 的关键。

## 二、Pyroscope 的整体架构

Pyroscope 在 2023 年被 Grafana 收购，1.0 重写了后端并和 Loki/Mimir/Tempo 对齐架构。组件可以和它们一一对应：

```
Application / eBPF agent
        │  pprof HTTP / gRPC push
        ▼
Distributor (无状态)
        │  hash ring
        ▼
Ingester (有状态, RF=3)
        │  build blocks
        ▼
Object Storage (S3/GCS/OSS)
        ▲
Querier ─▶ Store Gateway
        ▲
Query Frontend
        ▲
Grafana
```

- **Distributor**：接收 push/scrape 数据，校验、按 `service_name` label hash，打到 ingester；
- **Ingester**：维护内存索引，周期性把 profile 数据写成 block（Parquet 格式），上传对象存储；
- **Store Gateway**：从对象存储读 block，响应 querier；
- **Querier**：查询路径聚合 ingester + store gateway 的结果；
- **Query Frontend**：拆分查询 + 缓存；
- **Compactor**：合并 block，执行 retention。

1.x 之后 Pyroscope 支持单进程模式（`-target=all`，适合小集群）和微服务模式（每个 target 独立，适合大集群）。我们生产用微服务模式，日均 8TB profile 数据。

### Parquet block 存储

Pyroscope 的 block 是 Parquet，跟 Mimir 的 TSDB 完全不同。选择 Parquet 的原因：

1. profile 数据是宽表，行数少但列多（symbol table 特别大）；
2. Parquet 的列式压缩对 symbol table 效果极好，压缩率常到 10x；
3. 社区工具链成熟，用 DuckDB 能直接拿 block 做 ad-hoc 分析。

每个 block 包含：

- `profiles.parquet`：按时间排序的 profile 样本；
- `symdb/`：符号表（函数名、文件名）；
- `meta.json`：元信息；
- `index.tsdb`：label 倒排索引。

## 三、采集：push 还是 pull？怎么选

Pyroscope 支持两种采集方式：

### 1. Pull（scrape）：适合 Go/Java 服务暴露 pprof 端点的场景

应用像暴露 `/metrics` 一样暴露 `/debug/pprof/profile`，Pyroscope 周期性来抓。优势是应用侧零改动、零依赖；劣势是跨网络调用多，且 scraper 要能访问到所有 pod。

```yaml
scrape_configs:
  - job_name: 'kubernetes-pods'
    kubernetes_sd_configs:
      - role: pod
    relabel_configs:
      - action: keep
        source_labels: [__meta_kubernetes_pod_annotation_pyroscope_io_scrape]
        regex: "true"
      - source_labels: [__meta_kubernetes_pod_annotation_pyroscope_io_port]
        action: replace
        target_label: __address__
        regex: (.+)
        replacement: $1
    profiling_config:
      pprof_config:
        cpu:
          enabled: true
          path: /debug/pprof/profile
          delta: true
        memory:
          enabled: true
          path: /debug/pprof/heap
        goroutine:
          enabled: true
          path: /debug/pprof/goroutine
```

**`delta: true` 必须开**。pprof CPU profile 本身是累积的，delta 模式让 Pyroscope 上报两次采集之间的差值，避免重复计算。

### 2. Push（SDK / Agent）：适合需要 tag 或 eBPF 场景

Go SDK：

```go
import "github.com/grafana/pyroscope-go"

func main() {
    pyroscope.Start(pyroscope.Config{
        ApplicationName: "api-gateway",
        ServerAddress:   "http://pyroscope-distributor.pyroscope.svc:4040",
        Logger:          pyroscope.StandardLogger,
        Tags: map[string]string{
            "region":  "ap-southeast-1",
            "cluster": "prod",
            "version": os.Getenv("APP_VERSION"),
        },
        ProfileTypes: []pyroscope.ProfileType{
            pyroscope.ProfileCPU,
            pyroscope.ProfileAllocObjects,
            pyroscope.ProfileAllocSpace,
            pyroscope.ProfileInuseObjects,
            pyroscope.ProfileInuseSpace,
            pyroscope.ProfileGoroutines,
            pyroscope.ProfileMutexCount,
            pyroscope.ProfileBlockCount,
        },
        UploadRate: 15 * time.Second,
    })
}
```

Java 用 grafana-pyroscope-java agent，Python 用 pyroscope-io，Ruby/Node/.NET 都有官方 SDK。

### 3. eBPF agent：最省心的全量采集方式

对于语言栈不统一、没法给每个服务加 SDK 的环境，Pyroscope Grafana Agent / Alloy 提供 eBPF profiler：

```yaml
pyroscope.ebpf "default" {
  forward_to = [pyroscope.write.default.receiver]
  targets    = discovery.kubernetes.pods.targets

  demangle       = "full"
  python_enabled = true
}

pyroscope.write "default" {
  endpoint {
    url = "http://pyroscope-distributor.pyroscope.svc:4040"
  }
  external_labels = {
    cluster = "prod",
  }
}
```

eBPF 的优势：

- 完全无侵入，部署一个 DaemonSet 就能抓全节点所有进程；
- 只抓 CPU profile，没法抓 heap；
- 对静态语言（Go、Rust、C++）的符号化需要 debug symbol，否则只能看到地址；
- 对 Python 3.11+ 支持原生 stack unwind（python_enabled=true）；
- 对 JVM 需要配合 perf-map-agent。

我们线上策略：Go/Java 用 SDK push（可以带 trace_id tag），Node/Python/运维脚本类走 eBPF DaemonSet。

## 四、Go 服务接入：pprof 已经在手边

Go 的标准库自带 pprof，接入 Pyroscope 只需要两步：

1. 暴露 pprof 端点（很多服务已经有了）；
2. 在 pod annotation 加 `pyroscope.io/scrape: "true"`。

如果你要带 trace_id tag 做关联（强烈推荐），用 SDK：

```go
pyroscope.TagWrapper(r.Context(), pyroscope.Labels(
    "endpoint", r.URL.Path,
    "method", r.Method,
), func(ctx context.Context) {
    handler(w, r.WithContext(ctx))
})
```

`TagWrapper` 会往 pprof 的 labels 里写入 key-value，Pyroscope 按 label 做聚合，你可以在 Grafana 里按 endpoint 过滤火焰图。

### Go 接入坑点

1. **runtime.SetMutexProfileFraction(5)** 必须在 `main()` 里显式开，否则 mutex profile 永远是空的；
2. **runtime.SetBlockProfileRate(time.Millisecond.Nanoseconds())** 同理，block profile 默认关；
3. **heap profile 的采样率** 由 `runtime.MemProfileRate` 控制，默认 512KB 一个采样点。太大会漏掉小对象分配问题，太小会 overhead 过高。我们保持默认；
4. **多进程服务**：如果你在一个 pod 里跑多个进程（不推荐），每个进程要有不同的 `application_name` tag。

## 五、Java 接入：async-profiler 背后的魔法

Pyroscope 的 Java agent 本质是 async-profiler 的 wrapper。它用 Linux perf + AsyncGetCallTrace 做无侵入采样。

```dockerfile
FROM openjdk:21
ADD https://github.com/grafana/pyroscope-java/releases/download/v0.15.0/pyroscope.jar /opt/pyroscope.jar
ENV JAVA_TOOL_OPTIONS="-javaagent:/opt/pyroscope.jar"
ENV PYROSCOPE_APPLICATION_NAME=order-service
ENV PYROSCOPE_SERVER_ADDRESS=http://pyroscope-distributor.pyroscope.svc:4040
ENV PYROSCOPE_PROFILER_EVENT=itimer
ENV PYROSCOPE_PROFILER_ALLOC=524288
ENV PYROSCOPE_PROFILER_LOCK=10ms
ENV PYROSCOPE_LABELS=cluster=prod,region=ap-southeast-1
```

几个关键环境变量：

- `PYROSCOPE_PROFILER_EVENT=itimer`：默认是 cpu，容器里大多用 itimer，因为 cpu 事件在 cgroup 内可能被 clamp；
- `PYROSCOPE_PROFILER_ALLOC=524288`：堆分配采样率，每 512KB 采一次；
- `PYROSCOPE_PROFILER_LOCK=10ms`：锁等待超过 10ms 的记一次；
- `PYROSCOPE_UPLOAD_INTERVAL=15s`：上传频率。

**注意 JDK 版本**：JDK 8 需要开启 `-XX:+UnlockDiagnosticVMOptions -XX:+DebugNonSafepoints`，否则采样到的栈会偏到 safepoint。JDK 11+ 默认就带了。

## 六、eBPF 采集的 3 个坑

eBPF profiler 看起来很美，部署 DaemonSet 就能端到端抓全节点，但坑不少：

1. **内核版本**。eBPF CO-RE 需要 5.4+，实际生产要 5.10+ 才稳定。CentOS 7 用户自己掂量一下。
2. **符号化**。Go 二进制默认保留符号，可以直接读；C/C++ 要 debug info，生产镜像常剥离了；JVM 需要 perf-map-agent 生成 `/tmp/perf-<pid>.map`。
3. **容器 PID 命名空间**。eBPF agent 跑在 host namespace，看到的是 host PID；要把 host PID 映射回容器内 PID 和容器元数据，靠的是 `/proc/<pid>/cgroup` 的 cgroup path 解析。旧的 cgroup v1 在 K8s 1.25 之前的某些发行版里格式不一致，agent 解析会出错。我们在 Amazon Linux 2 上踩过这个坑，后来迁到 AL2023 才解决。

此外 eBPF agent 只能抓 CPU，拿不到 alloc/heap。所以我们还是以 SDK 为主，eBPF 作为补充覆盖无法改代码的场景。

## 七、Pyroscope 服务端部署：微服务模式

Helm chart 里微服务模式的 values 文件骨架：

```yaml
pyroscope:
  structuredConfig:
    multitenancy_enabled: true
    storage:
      backend: s3
      s3:
        bucket_name: pyroscope-prod
        region: ap-southeast-1
    ingester:
      lifecycler:
        ring:
          replication_factor: 3
          kvstore:
            store: memberlist

    memberlist:
      join_members:
        - "pyroscope-memberlist.pyroscope.svc.cluster.local"

    compactor:
      data_dir: /data/compactor

    limits:
      ingestion_rate_mb: 20
      ingestion_burst_size_mb: 40
      max_global_series_per_tenant: 5000000
      max_label_name_length: 1024
      max_label_value_length: 2048
      max_label_names_per_series: 30
      retention_period: 30d

components:
  querier:
    kind: Deployment
    replicaCount: 6
  query-frontend:
    kind: Deployment
    replicaCount: 3
  query-scheduler:
    kind: Deployment
    replicaCount: 2
  distributor:
    kind: Deployment
    replicaCount: 4
  ingester:
    kind: StatefulSet
    replicaCount: 6
  compactor:
    kind: StatefulSet
    replicaCount: 3
  store-gateway:
    kind: StatefulSet
    replicaCount: 4
```

几点说明：

1. **ingester 是 StatefulSet**：因为要维护 ring 和本地 WAL。
2. **compactor 也是 StatefulSet**：每个 compactor 负责一部分 tenant，基于 sharding ring。
3. **store-gateway 需要本地磁盘**：从 S3 下载 block 到本地加速查询，跟 Mimir 一样。
4. **replication_factor=3 是底线**。单副本 ingester 挂了会丢 5~10 分钟数据。

## 八、多租户和配额

Pyroscope 支持多租户，`X-Scope-OrgID` header 区分。按团队切 tenant 是最省心的方案。配额配置：

```yaml
overrides:
  team-payments:
    ingestion_rate_mb: 50
    ingestion_burst_size_mb: 100
    max_global_series_per_tenant: 10000000
    retention_period: 60d
  team-ml:
    ingestion_rate_mb: 100
    max_global_series_per_tenant: 30000000
    retention_period: 7d   # ML 训练 profile 数据量大，保留短
```

**series 的概念在 Pyroscope 里略有不同**。每条 profile 样本的 series key 是 `(__name__, label set)`，也就是 `cpu{service="api",endpoint="/users"}` 这种。如果你的 tag 基数爆炸（比如 trace_id 放进 label），series 数会迅速打爆。

## 九、存储成本：profile 数据其实很小

很多人担心持续剖析的存储成本。实际数据（1.x 的 Parquet 格式下）：

- 每个 pod 每天产生大约 5~30MB profile 数据（依赖语言和函数复杂度）；
- 压缩后对象存储上大约 1~5MB/pod/天；
- 1000 个 pod 的集群，一个月 30~150GB；
- S3 standard 大约 $3~15/月。

Mimir 每月要几个 TB 的对象存储，Loki 几十个 TB，Pyroscope 只要几十个 GB。成本上是最便宜的一件套，真的没理由不上。

唯一需要注意：symbol table 占大头。如果你的服务每次发版都带新的 build id，symbol table 会膨胀。解决办法：在 ingester 里开 symbol dedup（1.4+ 默认开）。

## 十、Grafana 里怎么读火焰图

Grafana 10.4+ 的 Profile Explorer 是正确姿势。几个入口：

1. **Service overview**：按 service 列出 CPU/memory 贡献 top N；
2. **Flame graph**：单个 service 的火焰图，支持按时间过滤；
3. **Diff flame graph**：选两段时间做对比，红=变慢，绿=变快；
4. **Explore Profiles**：像 Explore logs 一样的 ad-hoc 查询，支持 LabelQL 过滤。

### 火焰图读法 ABC

- **从底往上读**。最底是入口（比如 main、runtime.main 或 HTTP handler），往上是调用链。
- **宽度代表资源占用**。`alloc_space` 火焰图里，某个函数宽度 30% 意味着它贡献了 30% 的分配总量。
- **颜色不代表语义**，只是区分不同函数。不要被颜色吓到。
- **对比看 diff**。单张火焰图只能告诉你「谁占用高」，不能告诉你「谁变慢了」。持续剖析的核心价值在 diff。

### 常见模式

- **火焰图顶部宽且贴近 runtime**：GC 压力大，看 alloc_space；
- **某个业务函数占比 40%+**：热点函数，可能是 N+1 或缺少缓存；
- **runtime.futex / runtime.sysmon 宽**：锁争用或 GC 异常；
- **JIT compile 函数宽**（JVM）：class 加载风暴；
- **PyObject_GC_Collect 宽**（Python）：循环引用 + GC 频繁。

## 十一、案例一：Go 服务 p99 莫名翻倍

时间：2025 年 8 月。现象：订单服务 p99 从 120ms 涨到 240ms，CPU 使用率反而从 60% 降到 45%。metrics 和 trace 都看不出异常。

**排查**：

1. 打开 Pyroscope，选择事件前 1h 和事件后 1h 做 diff flame graph；
2. 红色最高的是 `runtime.chanrecv`，宽度从 3% 涨到 12%；
3. 往下看调用栈，发现是新上线的下游 gRPC client 用了 `context.WithTimeout` + goroutine pool，每个请求都会 select channel 等 timeout；
4. 原实现是单次 RPC 调用，新实现加了 retry 包装器，每次 retry 都新建 goroutine + channel；
5. 回退包装器之后 p99 立刻恢复。

没有 Pyroscope 的话，我们可能要花一天对比两个版本的 trace 才能定位。有了连续 profile + diff，15 分钟搞定。

## 十二、案例二：Java heap 缓慢增长

时间：2025 年 11 月。现象：支付服务 OldGen 每周涨 3%，7 周后 OOM。

**排查**：

1. Grafana Profiles 选 `memory:inuse_space`，按 7 周做 diff；
2. 变化最大的调用栈指向 `io.netty.buffer.PoolChunkList.add`，一个 Netty buffer pool；
3. 搜代码发现某个上线的新版本把 buffer 从「每请求一个」改成了「链接级长寿命」，但没做主动 release；
4. 改回每请求释放之后，7 天复测 OldGen 平稳。

注意点：inuse_space 是真实还占用的内存（heap dump 的等价物），alloc_space 是累计分配（包括已回收的）。排查内存泄漏用 inuse_space，排查 GC 压力用 alloc_space。

## 十三、和 Trace 的联动：Span Profiles

Pyroscope 1.x 和 Tempo 的集成方式叫 **Span Profiles**（以前叫 trace-to-profile）。原理是：

1. SDK 在处理请求时，把当前 trace_id 作为 pprof label 写进 profile 样本；
2. Pyroscope 存的 profile 里带 trace_id tag；
3. 在 Grafana 里查 Tempo trace，点某个 span 的「Profile」按钮，跳到 Pyroscope 并自动 filter `trace_id=xxx`；
4. 看到的是这一条请求对应的 CPU 火焰图。

Go SDK 带 trace_id 的写法：

```go
import "go.opentelemetry.io/otel/trace"

func handler(ctx context.Context) {
    span := trace.SpanFromContext(ctx)
    traceID := span.SpanContext().TraceID().String()
    pyroscope.TagWrapper(ctx, pyroscope.Labels("trace_id", traceID), func(ctx context.Context) {
        // real work
    })
}
```

数据关联的前提是 trace 采样率和 profile 采样率都足够。我们生产 trace 采样 1%，profile 100%，profile 里只有 1% 的样本带 trace_id，对于普通 trace 足够用；针对高价值 trace（比如 p99 的 outlier），可以用 tail sampling 拉高采样率。

## 十四、CI 性能回归测试

Pyroscope 提供 HTTP API 可以程序化查询 profile，我们在 CI 里加了一步：

1. merge 前在 staging 跑 perf benchmark 10min；
2. merge 后再跑 10min；
3. 脚本调 Pyroscope `/render?from=X&to=Y&query=...&format=pprof` 拿 pprof 文件；
4. 用 `pprof --diff_base` 生成 diff；
5. 计算总 CPU 差值，超过阈值（比如 +5%）就在 PR 评论警告。

这个流程帮我们挡掉过好几个无意的性能回归，典型案例：某个 PR 把 `sync.Map` 换成 `map+RWMutex`，性能回退 12%，CI 自动提示后 reviewer 拒掉。

## 十五、监控 Pyroscope 自己

最核心的几个指标：

- `pyroscope_distributor_received_samples_total`：写入 QPS；
- `pyroscope_distributor_discarded_samples_total{reason=...}`：被丢弃的样本及原因；
- `pyroscope_ingester_memory_series`：ingester 内存 series；
- `pyroscope_ingester_shipper_uploads_failed_total`：block 上传失败；
- `pyroscope_bucket_store_blocks_loaded`：store gateway 加载的 block 数；
- `pyroscope_query_frontend_queries_in_progress`：查询并发。

配合 Grafana 官方 mixin dashboard 即可。

## 十六、容量规划

实际运行的粗略经验：

- 单 ingester 承载 2000~3000 个 pod 的 profile（采样率 15s 上传一次）；
- 单 store gateway 承载 100~200 个 tenant 的历史查询；
- compactor 每 GB block 的 compaction 大约 30 秒；
- 对象存储 retention 30 天，占用约 300GB（6000 pod 规模）。

## 十七、常见踩坑清单

最后按原因罗列几个典型坑，避免你们重新发现：

1. **Scrape 模式下 pprof timeout 太短**：profile endpoint 默认抓 30s CPU，HTTP 超时一定要配 60s 以上；
2. **SDK 和 Pyroscope 版本不兼容**：push 协议在 1.0 改过一次，老 SDK 要升级；
3. **Pod 没有 `pyroscope.io/scrape` annotation 但开了 SDK**：distributor 会拒绝不带 application name 的推送；
4. **Service name 有空格或特殊字符**：label 非法，Pyroscope 静默丢弃；
5. **Java agent 和 SkyWalking 冲突**：两个 -javaagent 合一起跑互相干扰，至少选一个；
6. **eBPF profile 看起来都是地址**：忘了给 binary 保留 symbol；Go 构建加 `-ldflags="-s=false -w=false"`；
7. **alloc_space 比实际大得多**：这是累积的，不是 in-use；
8. **Grafana 10.3 及以下没有 Profile Explorer**：一定要升 10.4+。

## 十八、落地路线建议

给想上 Pyroscope 的团队一份路线：

1. **Week 1**：单独部署一套 Pyroscope 微服务模式，接 1~2 个 Go 服务，验证可用性；
2. **Week 2**：在 Grafana 里建 dashboard，对接 Tempo，走一遍 span profiles；
3. **Week 3**：推广到一个业务线（10~30 个服务），收集团队反馈；
4. **Week 4**：评估存储成本和稳定性，决定是否全量铺；
5. **Month 2**：接入 Java/Python，考虑 eBPF agent 覆盖剩余；
6. **Month 3**：把性能回归测试接入 CI；
7. **Month 4+**：建立团队级性能画像，每月出 top N 性能热点报告。

我们铺下来这一年，最大的体会是：持续剖析的成本比 metrics 低一个数量级，但它能定位到行级别的性能问题，这是 metric 和 trace 永远做不到的。真要说"下一个可观测性落地点"，这个比其他候选都更值回票价。

## 参考资料

- Grafana Pyroscope 官方文档 1.x 架构与 profile type 章节
- Grafana Blog《Continuous profiling in production: A real-world example》
- grafana/pyroscope GitHub release notes
- Grafana Alloy `pyroscope.ebpf` 组件文档
