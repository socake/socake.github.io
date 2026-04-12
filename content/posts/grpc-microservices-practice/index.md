---
title: "gRPC 微服务实践：协议、负载均衡与 Kubernetes 集成"
date: 2026-04-12T10:00:00+08:00
draft: false
tags: ["gRPC", "微服务", "Kubernetes", "负载均衡", "Protobuf", "Go", "Istio", "OpenTelemetry"]
categories: ["云原生"]
description: "深入讲解 gRPC 在生产环境微服务架构中的实践，涵盖 Protobuf 设计、Go 服务端实现、K8s 负载均衡陷阱与解法、健康检查、链路追踪以及 grpc-gateway 同端口共存方案。"
summary: "从协议原理到 Kubernetes 生产落地，系统梳理 gRPC 微服务的核心实践：Protobuf 向后兼容设计、拦截器链（日志/限流/OTel）、长连接负载不均问题（headless Service + round_robin vs Envoy L7）、健康检查 Probe 配置、以及 grpc-gateway REST 共存方案。"
toc: true
math: false
diagram: false
keywords: ["gRPC", "Protobuf", "微服务", "Kubernetes", "负载均衡", "headless service", "grpc-gateway", "OpenTelemetry", "Envoy", "Istio"]
params:
  reading_time: true
---

## 为什么内部微服务选 gRPC 而不是 REST

在面向外部用户的 API 中，REST + JSON 是无可争议的首选——生态成熟、调试简单、前端友好。但在内部微服务之间的调用场景，gRPC 有几个结构性优势：

**协议效率**：Protobuf 二进制编码比 JSON 体积通常小 3-10 倍，序列化/反序列化 CPU 开销也更低。在高频 RPC（如每秒数万次的服务间调用）场景下，这个差距会直接反映在延迟和机器成本上。

**强类型契约**：`.proto` 文件是服务间接口的唯一真相来源，IDL 驱动生成客户端/服务端骨架代码，避免了 REST 文档与实现不同步的问题。字段类型不匹配在编译期就能发现，不会等到运行时。

**HTTP/2 多路复用**：gRPC 基于 HTTP/2，单连接可并发多个 stream，消除了 HTTP/1.1 的队头阻塞。四种调用模式（Unary、Server Streaming、Client Streaming、Bidirectional Streaming）可以覆盖推送、大文件分片、实时事件等复杂场景。

**生态完整**：拦截器机制统一处理认证、限流、链路追踪；gRPC-Web 可以让浏览器直接调用；grpc-gateway 可以将 gRPC 服务同时暴露为 REST 接口，兼顾存量系统。

当然 gRPC 也有代价：调试没有 curl 方便（需要 grpcurl 或 BloomRPC）、浏览器原生支持需要额外代理、错误码体系与 HTTP 状态码不对应需要转换层。

---

## Protobuf 设计最佳实践

### 字段编号与向后兼容

Protobuf 的字段编号一旦发布就不能变更，这是向后兼容的基础。几条核心规则：

```protobuf
syntax = "proto3";

package user.v1;

option go_package = "github.com/yourorg/proto/user/v1;userv1";

message User {
  // 1-15 编号只占 1 个字节，用于高频字段
  int64  id          = 1;
  string name        = 2;
  string email       = 3;
  UserStatus status  = 4;

  // 16-2047 占 2 个字节，用于低频或后加字段
  string avatar_url  = 16;
  int64  created_at  = 17;  // Unix timestamp，避免 Timestamp 类型跨语言问题

  // 废弃字段：不能复用编号，用 reserved 保留
  reserved 5, 6;
  reserved "old_nickname";
}

// 枚举第 0 值必须是 UNSPECIFIED，表示未设置，不能作为业务值
enum UserStatus {
  USER_STATUS_UNSPECIFIED = 0;
  USER_STATUS_ACTIVE      = 1;
  USER_STATUS_SUSPENDED   = 2;
  USER_STATUS_DELETED     = 3;
}
```

**向后兼容规则**：
- 只能新增字段，不能删除或重命名（可用 `reserved` 保护废弃编号）
- 不能修改已有字段类型（`int32` → `int64` 在 wire format 上不兼容）
- 不能修改字段编号
- 可以将 `optional` 字段改为 `repeated`（反之不行）

### oneof 处理多态请求

```protobuf
message NotificationRequest {
  string title   = 1;
  string content = 2;

  oneof channel {
    EmailChannel  email  = 10;
    SmsChannel    sms    = 11;
    PushChannel   push   = 12;
  }
}

message EmailChannel {
  repeated string to  = 1;
  string          cc  = 2;
}

message SmsChannel {
  string phone    = 1;
  string template = 2;
}
```

`oneof` 确保只有一个字段被设置，避免调用方同时填入多个渠道导致歧义。代码侧通过类型断言或 switch 处理不同 case，比用 string 类型标记再解析 JSON 更安全。

### 版本管理策略

推荐按 `package` 版本化（`user.v1`、`user.v2`），而非文件名。破坏性变更（如字段语义变化）发新版本 package，旧版本继续运行直到迁移完成。目录结构：

```
proto/
├── user/
│   ├── v1/
│   │   └── user.proto
│   └── v2/
│       └── user.proto
└── notification/
    └── v1/
        └── notification.proto
```

---

## Go 实现 gRPC 服务端

### 项目结构

```
.
├── cmd/server/main.go
├── internal/
│   ├── handler/         # gRPC handler 实现
│   ├── interceptor/     # 拦截器
│   └── service/         # 业务逻辑
├── proto/               # .proto 文件
└── gen/                 # protoc 生成代码
```

### 服务实现

```go
// internal/handler/user.go
package handler

import (
    "context"
    "time"

    "google.golang.org/grpc/codes"
    "google.golang.org/grpc/status"

    userv1 "github.com/yourorg/proto/user/v1"
    "github.com/yourorg/svc-user/internal/service"
)

type UserHandler struct {
    userv1.UnimplementedUserServiceServer
    svc service.UserService
}

func NewUserHandler(svc service.UserService) *UserHandler {
    return &UserHandler{svc: svc}
}

func (h *UserHandler) GetUser(ctx context.Context, req *userv1.GetUserRequest) (*userv1.GetUserResponse, error) {
    if req.GetId() <= 0 {
        return nil, status.Errorf(codes.InvalidArgument, "id must be positive, got %d", req.GetId())
    }

    user, err := h.svc.GetByID(ctx, req.GetId())
    if err != nil {
        if errors.Is(err, service.ErrNotFound) {
            return nil, status.Errorf(codes.NotFound, "user %d not found", req.GetId())
        }
        return nil, status.Errorf(codes.Internal, "internal error: %v", err)
    }

    return &userv1.GetUserResponse{User: toProto(user)}, nil
}

// Server Streaming 示例：批量导出用户
func (h *UserHandler) ListUsers(req *userv1.ListUsersRequest, stream userv1.UserService_ListUsersServer) error {
    cursor := int64(0)
    for {
        users, nextCursor, err := h.svc.List(stream.Context(), cursor, 100)
        if err != nil {
            return status.Errorf(codes.Internal, "list error: %v", err)
        }
        for _, u := range users {
            if err := stream.Send(&userv1.ListUsersResponse{User: toProto(u)}); err != nil {
                return err // client 断开，直接返回
            }
        }
        if nextCursor == 0 {
            break
        }
        cursor = nextCursor
    }
    return nil
}
```

### 拦截器链

拦截器是 gRPC 中横切关注点的标准实现位置。使用 `grpc.ChainUnaryInterceptor` 组合多个拦截器，执行顺序与注册顺序一致。

```go
// internal/interceptor/logging.go
package interceptor

import (
    "context"
    "time"

    "go.uber.org/zap"
    "google.golang.org/grpc"
    "google.golang.org/grpc/status"
)

func UnaryLogging(logger *zap.Logger) grpc.UnaryServerInterceptor {
    return func(ctx context.Context, req any, info *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (any, error) {
        start := time.Now()
        resp, err := handler(ctx, req)
        st, _ := status.FromError(err)
        logger.Info("grpc call",
            zap.String("method", info.FullMethod),
            zap.Duration("duration", time.Since(start)),
            zap.String("code", st.Code().String()),
            zap.Error(err),
        )
        return resp, err
    }
}
```

```go
// internal/interceptor/ratelimit.go
package interceptor

import (
    "context"

    "golang.org/x/time/rate"
    "google.golang.org/grpc"
    "google.golang.org/grpc/codes"
    "google.golang.org/grpc/status"
)

func UnaryRateLimit(limiter *rate.Limiter) grpc.UnaryServerInterceptor {
    return func(ctx context.Context, req any, info *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (any, error) {
        if !limiter.Allow() {
            return nil, status.Errorf(codes.ResourceExhausted, "rate limit exceeded")
        }
        return handler(ctx, req)
    }
}
```

```go
// internal/interceptor/tracing.go
package interceptor

import (
    "context"

    "go.opentelemetry.io/otel"
    "go.opentelemetry.io/otel/propagation"
    "google.golang.org/grpc"
    "google.golang.org/grpc/metadata"
)

// 从 gRPC metadata 提取 trace context 并注入到 context
func UnaryTracing() grpc.UnaryServerInterceptor {
    propagator := otel.GetTextMapPropagator()
    tracer := otel.Tracer("grpc-server")

    return func(ctx context.Context, req any, info *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (any, error) {
        md, _ := metadata.FromIncomingContext(ctx)
        ctx = propagator.Extract(ctx, metadataCarrier(md))

        ctx, span := tracer.Start(ctx, info.FullMethod)
        defer span.End()

        return handler(ctx, req)
    }
}

// metadataCarrier 实现 propagation.TextMapCarrier
type metadataCarrier metadata.MD

func (c metadataCarrier) Get(key string) string {
    vals := metadata.MD(c).Get(key)
    if len(vals) == 0 {
        return ""
    }
    return vals[0]
}
func (c metadataCarrier) Set(key, val string) { metadata.MD(c).Set(key, val) }
func (c metadataCarrier) Keys() []string {
    keys := make([]string, 0, len(c))
    for k := range c {
        keys = append(keys, k)
    }
    return keys
}
```

```go
// cmd/server/main.go
package main

import (
    "net"

    "golang.org/x/time/rate"
    "google.golang.org/grpc"
    "google.golang.org/grpc/health"
    healthpb "google.golang.org/grpc/health/grpc_health_v1"
    "go.uber.org/zap"

    userv1 "github.com/yourorg/proto/user/v1"
    "github.com/yourorg/svc-user/internal/handler"
    "github.com/yourorg/svc-user/internal/interceptor"
    "github.com/yourorg/svc-user/internal/service"
)

func main() {
    logger, _ := zap.NewProduction()
    limiter := rate.NewLimiter(rate.Limit(1000), 100) // 1000 RPS，burst 100

    svc := service.New(/* deps */)
    userHandler := handler.NewUserHandler(svc)

    srv := grpc.NewServer(
        grpc.ChainUnaryInterceptor(
            interceptor.UnaryTracing(),
            interceptor.UnaryLogging(logger),
            interceptor.UnaryRateLimit(limiter),
        ),
        grpc.MaxRecvMsgSize(4*1024*1024), // 4MB
    )

    userv1.RegisterUserServiceServer(srv, userHandler)

    // 注册健康检查服务
    healthSrv := health.NewServer()
    healthpb.RegisterHealthServer(srv, healthSrv)
    healthSrv.SetServingStatus("user.v1.UserService", healthpb.HealthCheckResponse_SERVING)

    lis, _ := net.Listen("tcp", ":50051")
    logger.Info("gRPC server listening", zap.String("addr", ":50051"))
    if err := srv.Serve(lis); err != nil {
        logger.Fatal("serve failed", zap.Error(err))
    }
}
```

---

## Kubernetes 中 gRPC 负载均衡的陷阱

这是生产环境最容易踩的坑。

### 问题根因

HTTP/1.1 是短连接模型，K8s Service（ClusterIP + kube-proxy iptables）对每个新 TCP 连接做轮询，天然负载均衡。

gRPC 基于 HTTP/2，客户端与服务端建立**一条持久长连接**，所有 RPC 都在这条连接上复用。结果：如果你有 3 个 Pod 副本，某个客户端实例可能永远只打到其中一个 Pod，其他 Pod 空载。

### 解法 1：headless Service + 客户端 round_robin

**headless Service** 不分配 ClusterIP，DNS 解析直接返回所有 Pod IP，客户端自行做负载均衡。

```yaml
# headless-service.yaml
apiVersion: v1
kind: Service
metadata:
  name: svc-user-headless
  namespace: production
spec:
  clusterIP: None          # 关键：headless
  selector:
    app: svc-user
  ports:
    - name: grpc
      port: 50051
      targetPort: 50051
```

客户端 Go 代码使用 `dns` resolver + `round_robin` balancer：

```go
import (
    "google.golang.org/grpc"
    "google.golang.org/grpc/balancer/roundrobin"
    "google.golang.org/grpc/credentials/insecure"
    _ "google.golang.org/grpc/resolver/dns" // 注册 dns resolver
)

func NewUserClient(addr string) (userv1.UserServiceClient, error) {
    // addr 格式: "dns:///svc-user-headless.production.svc.cluster.local:50051"
    conn, err := grpc.NewClient(
        addr,
        grpc.WithTransportCredentials(insecure.NewCredentials()),
        grpc.WithDefaultServiceConfig(`{
            "loadBalancingPolicy": "round_robin",
            "methodConfig": [{
                "name": [{"service": "user.v1.UserService"}],
                "retryPolicy": {
                    "maxAttempts": 3,
                    "initialBackoff": "0.1s",
                    "maxBackoff": "1s",
                    "backoffMultiplier": 2,
                    "retryableStatusCodes": ["UNAVAILABLE"]
                },
                "timeout": "5s"
            }]
        }`),
    )
    if err != nil {
        return nil, err
    }
    return userv1.NewUserServiceClient(conn), nil
}
```

**注意**：DNS 解析有缓存，新 Pod 上线后客户端可能不会立即感知。生产中建议设置较短的 DNS TTL，或使用 `grpc.WithResolverBuildRegistry` 注入自定义 resolver（如 etcd/consul 服务发现）。

### 解法 2：Envoy/Istio L7 负载均衡

客户端侧负载均衡的问题：每个服务都要正确配置，维护成本高；服务发现逻辑下沉到应用。

更推荐的方案是让 **Envoy Sidecar（Istio）** 在 L7 做 gRPC 负载均衡，应用代码无感知，只需指向普通 ClusterIP Service。

```yaml
# VirtualService 配置 gRPC 路由（Istio）
apiVersion: networking.istio.io/v1beta1
kind: VirtualService
metadata:
  name: svc-user
  namespace: production
spec:
  hosts:
    - svc-user
  http:
    - match:
        - headers:
            content-type:
              prefix: "application/grpc"
      route:
        - destination:
            host: svc-user
            port:
              number: 50051
      timeout: 10s
      retries:
        attempts: 3
        perTryTimeout: 3s
        retryOn: "reset,connect-failure,retriable-status-codes"
        retryRemoteStatuses: 14  # UNAVAILABLE
```

```yaml
# DestinationRule：启用 gRPC 健康检查探测
apiVersion: networking.istio.io/v1beta1
kind: DestinationRule
metadata:
  name: svc-user
  namespace: production
spec:
  host: svc-user
  trafficPolicy:
    loadBalancer:
      simple: LEAST_CONN   # gRPC 场景下比 ROUND_ROBIN 更均匀
    connectionPool:
      http:
        h2UpgradePolicy: UPGRADE
        http2MaxRequests: 1000
    outlierDetection:
      consecutive5xxErrors: 5
      interval: 30s
      baseEjectionTime: 30s
```

---

## 健康检查：gRPC Health Protocol + K8s Probe

gRPC 有标准健康检查协议（[grpc.health.v1](https://github.com/grpc/grpc/blob/master/src/proto/grpc/health/v1/health.proto)），比 HTTP `/healthz` 更原生。

服务端注册（已在上面 main.go 中展示），Kubernetes Probe 配置如下：

```yaml
# deployment.yaml（片段）
containers:
  - name: svc-user
    image: yourorg/svc-user:v1.2.0
    ports:
      - containerPort: 50051
        name: grpc
    livenessProbe:
      grpc:
        port: 50051
        service: "user.v1.UserService"  # 空字符串表示检查整体健康
      initialDelaySeconds: 10
      periodSeconds: 15
      failureThreshold: 3
    readinessProbe:
      grpc:
        port: 50051
        service: "user.v1.UserService"
      initialDelaySeconds: 5
      periodSeconds: 10
      failureThreshold: 2
    # startupProbe 适用于启动慢的服务（如需要预热缓存）
    startupProbe:
      grpc:
        port: 50051
      failureThreshold: 30
      periodSeconds: 2
```

**注意**：`grpc` probe 类型需要 Kubernetes 1.24+。旧版本集群需要用 `grpc_health_probe` 二进制作为 exec probe：

```yaml
livenessProbe:
  exec:
    command:
      - /bin/grpc_health_probe
      - -addr=:50051
      - -service=user.v1.UserService
  initialDelaySeconds: 10
```

---

## 反射 API 与 grpcurl 调试

生产环境建议只在 `dev`/`staging` 开启反射，`prod` 关闭（避免接口信息泄露）：

```go
import "google.golang.org/grpc/reflection"

if os.Getenv("GRPC_REFLECTION") == "true" {
    reflection.Register(srv)
}
```

常用 grpcurl 命令：

```bash
# 列出所有服务
grpcurl -plaintext localhost:50051 list

# 列出某服务的方法
grpcurl -plaintext localhost:50051 list user.v1.UserService

# 查看方法详情
grpcurl -plaintext localhost:50051 describe user.v1.UserService.GetUser

# 调用（JSON 请求体）
grpcurl -plaintext \
  -d '{"id": 123}' \
  localhost:50051 \
  user.v1.UserService/GetUser

# 带 metadata（模拟 trace header）
grpcurl -plaintext \
  -H 'x-b3-traceid: abc123' \
  -d '{"id": 123}' \
  localhost:50051 \
  user.v1.UserService/GetUser

# 从 proto 文件调用（不依赖反射）
grpcurl -plaintext \
  -proto proto/user/v1/user.proto \
  -import-path proto \
  -d '{"id": 123}' \
  localhost:50051 \
  user.v1.UserService/GetUser
```

---

## Prometheus Metrics 采集

使用 `go-grpc-prometheus` 库，自动暴露 gRPC 调用的 QPS、延迟直方图、错误率：

```go
import grpc_prometheus "github.com/grpc-ecosystem/go-grpc-prometheus"

srv := grpc.NewServer(
    grpc.ChainUnaryInterceptor(
        grpc_prometheus.UnaryServerInterceptor,  // 放在链首，确保所有请求都被计量
        interceptor.UnaryTracing(),
        interceptor.UnaryLogging(logger),
        interceptor.UnaryRateLimit(limiter),
    ),
    grpc.ChainStreamInterceptor(
        grpc_prometheus.StreamServerInterceptor,
    ),
)

// 初始化 metrics（在所有服务注册后调用）
grpc_prometheus.EnableHandlingTimeHistogram(
    grpc_prometheus.WithHistogramBuckets([]float64{.005, .01, .025, .05, .1, .25, .5, 1, 2.5, 5}),
)
grpc_prometheus.Register(srv)

// 暴露 /metrics 端点（独立端口，不与 gRPC 混用）
http.Handle("/metrics", promhttp.Handler())
go http.ListenAndServe(":9090", nil)
```

关键 Prometheus 指标：

```promql
# gRPC 请求 QPS（按方法、状态码分组）
sum(rate(grpc_server_handled_total[1m])) by (grpc_method, grpc_code)

# P99 延迟
histogram_quantile(0.99,
  sum(rate(grpc_server_handling_seconds_bucket[5m])) by (grpc_method, le)
)

# 错误率
sum(rate(grpc_server_handled_total{grpc_code!="OK"}[1m])) by (grpc_method)
/
sum(rate(grpc_server_handled_total[1m])) by (grpc_method)
```

---

## grpc-gateway：同端口暴露 REST 接口

在 `.proto` 文件中添加 HTTP 映射注解：

```protobuf
import "google/api/annotations.proto";

service UserService {
  rpc GetUser(GetUserRequest) returns (GetUserResponse) {
    option (google.api.http) = {
      get: "/v1/users/{id}"
    };
  }

  rpc CreateUser(CreateUserRequest) returns (CreateUserResponse) {
    option (google.api.http) = {
      post: "/v1/users"
      body: "*"
    };
  }
}
```

服务端使用 `cmux` 在同一端口同时处理 gRPC 和 HTTP：

```go
import (
    "net/http"

    "github.com/grpc-ecosystem/grpc-gateway/v2/runtime"
    "github.com/soheilhy/cmux"
    "google.golang.org/grpc"
    "google.golang.org/protobuf/encoding/protojson"
)

func main() {
    lis, _ := net.Listen("tcp", ":8080")
    m := cmux.New(lis)

    // HTTP/2 走 gRPC
    grpcL := m.MatchWithWriters(
        cmux.HTTP2MatchHeaderFieldSendSettings("content-type", "application/grpc"),
    )
    // 其余走 HTTP/1.1（REST）
    httpL := m.Match(cmux.HTTP1Fast())

    grpcSrv := buildGRPCServer()
    httpSrv := buildHTTPGateway()

    go grpcSrv.Serve(grpcL)
    go httpSrv.Serve(httpL)
    m.Serve()
}

func buildHTTPGateway() *http.Server {
    mux := runtime.NewServeMux(
        runtime.WithMarshalerOption(runtime.MIMEWildcard, &runtime.JSONPb{
            MarshalOptions: protojson.MarshalOptions{
                UseProtoNames:   true,  // 使用 proto 字段名，不做驼峰转换
                EmitUnpopulated: false,
            },
        }),
        // 从 HTTP Header 透传 Authorization 到 gRPC metadata
        runtime.WithIncomingHeaderMatcher(func(key string) (string, bool) {
            switch strings.ToLower(key) {
            case "authorization", "x-request-id":
                return key, true
            }
            return "", false
        }),
    )

    opts := []grpc.DialOption{grpc.WithTransportCredentials(insecure.NewCredentials())}
    userv1.RegisterUserServiceHandlerFromEndpoint(context.Background(), mux, "localhost:50051", opts)

    return &http.Server{Handler: mux}
}
```

---

## 生产问题排查

### 连接超时与 RST_STREAM

**现象**：gRPC 调用偶发 `transport is closing` 或 `RST_STREAM`。

**排查路径**：

1. 检查中间负载均衡器（ALB/NLB）的 idle timeout：AWS ALB 默认 60s，gRPC 长连接如果超过这个时间没有流量会被强制关闭。
   ```bash
   # 客户端配置 keepalive 参数
   ```
   ```go
   grpc.WithKeepaliveParams(keepalive.ClientParameters{
       Time:                20 * time.Second, // 每 20s 发一次 ping
       Timeout:             5 * time.Second,  // 5s 内没有响应则断开
       PermitWithoutStream: true,             // 空闲连接也发 ping
   })
   ```
   服务端对应配置：
   ```go
   grpc.KeepaliveParams(keepalive.ServerParameters{
       MaxConnectionIdle:     30 * time.Second,
       MaxConnectionAge:      2 * time.Minute,
       MaxConnectionAgeGrace: 5 * time.Second,
       Time:                  20 * time.Second,
       Timeout:               5 * time.Second,
   }),
   grpc.KeepaliveEnforcementPolicy(keepalive.EnforcementPolicy{
       MinTime:             10 * time.Second,
       PermitWithoutStream: true,
   }),
   ```

2. 检查流控窗口（flow control）：大量 streaming 调用时，如果 sender 速度远超 receiver 处理能力，会触发流控。通过 `GRPC_TRACE=flowcontrol` 环境变量开启 trace 日志分析。

### 排查工具

```bash
# 抓包分析 HTTP/2 帧
tcpdump -i eth0 -w /tmp/grpc.pcap port 50051
# 用 Wireshark 打开，过滤 http2，可以看到每个 stream 的帧类型和标志位

# 开启 gRPC 详细日志
GRPC_GO_LOG_VERBOSITY_LEVEL=99 GRPC_GO_LOG_SEVERITY_LEVEL=info ./server

# 查看连接状态
grpc.ClientConn.GetState() // IDLE/CONNECTING/READY/TRANSIENT_FAILURE/SHUTDOWN
```

---

## 总结

在 Kubernetes 内部微服务场景，gRPC 的协议效率和强类型优势显著，但需要额外关注：

1. **Protobuf 设计时就要考虑向后兼容**，reserved 保护废弃字段
2. **负载均衡是最容易忽视的陷阱**：ClusterIP Service + gRPC 长连接 = 负载不均，用 headless + round_robin 或 Istio L7
3. **拦截器链统一处理横切关注点**，顺序很重要（tracing 要最先执行）
4. **Keepalive 参数要与基础设施 idle timeout 匹配**，避免偶发连接重置
5. **grpc-gateway 是渐进迁移的好工具**，存量 REST 客户端无需改造
