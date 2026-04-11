---
title: "Consul 服务注册与发现：从入门到生产级健康检查"
date: 2026-04-11T09:30:00+08:00
draft: false
tags: ["Consul", "服务发现", "微服务", "运维", "HashiCorp"]
categories: ["基础设施"]
description: "深入讲解 Consul 的架构设计、K8s 环境部署方式、多种服务注册和健康检查方案，以及与 Prometheus 的集成实践，含 ACL 和跨数据中心踩坑记录。"
summary: "微服务时代，动态 IP 和服务健康状态管理是绕不过去的问题。Consul 提供了一套完整的服务发现解决方案，本文从实操角度梳理其核心用法和生产踩坑。"
toc: true
math: false
diagram: false
keywords: ["Consul", "服务发现", "健康检查", "DNS服务发现", "Prometheus服务发现", "ACL"]
params:
  reading_time: true
---

## 为什么需要服务发现

传统单体应用时代，一个服务对应一个固定 IP，在配置文件里写死就行了。进入微服务时代，这个方式彻底失效：

1. **动态扩缩容**：自动扩出来的 Pod 或 EC2 IP 每次都不一样，你没法提前知道
2. **服务实例不稳定**：容器随时可能因为 OOM、健康检查失败被 K8s 重启，IP 随之变化
3. **健康检查问题**：负载均衡器需要知道哪些实例当前是健康的，避免把流量打到已经挂掉的节点

服务发现的解决思路：引入一个"注册中心"作为中间层。服务启动时主动把自己的 IP:Port 注册上去，下线时注销，注册中心持续做健康检查。调用方不再依赖固定 IP，而是向注册中心查询"我要调用 user-service，当前有哪些健康的实例？"

Consul 是 HashiCorp 出品的服务发现工具，除了服务发现还支持 KV 存储、Service Mesh、ACL 权限管理，在微服务基础设施领域用得很广。

## Consul 架构：Server vs Agent

Consul 的部署分两个角色：

**Server 节点**负责存储和复制所有状态数据，参与 Raft 选举，维护集群一致性。生产环境建议部署 3 或 5 个 Server 节点，理由和 ETCD 一样——奇数节点规避"浪费"，3 节点可容忍 1 个故障，5 节点可容忍 2 个故障。

**Client/Agent 节点**是轻量级代理，运行在每台需要注册或发现服务的机器上，负责：
- 将本地服务注册到 Server
- 将查询请求转发给 Server
- 对本地服务执行健康检查
- 参与 Gossip 协议（LAN Gossip Pool）

Client 是无状态的，资源开销极小，在 K8s 环境下通常以 DaemonSet 方式部署，每个节点一个 Agent Pod。

两者的通信协议：
- Client ↔ Server：RPC
- Server ↔ Server（跨数据中心）：WAN Gossip
- 同数据中心节点间：LAN Gossip（基于 UDP，用于成员发现和故障检测）

## K8s 环境部署 Consul

用官方 Helm Chart 是最省事的方式：

```bash
# 添加 HashiCorp Helm 仓库
helm repo add hashicorp https://helm.releases.hashicorp.com
helm repo update

# 查看可用版本
helm search repo hashicorp/consul
```

创建 values 文件 `consul-values.yaml`：

```yaml
global:
  name: consul
  datacenter: dc1
  tls:
    enabled: true
    verify: true
  acls:
    manageSystemACLs: true

server:
  enabled: true
  replicas: 3
  # Server 用 StatefulSet，保证稳定的网络标识
  storage: 10Gi
  storageClass: gp3
  resources:
    requests:
      memory: "256Mi"
      cpu: "100m"
    limits:
      memory: "512Mi"
      cpu: "500m"
  affinity: |
    podAntiAffinity:
      requiredDuringSchedulingIgnoredDuringExecution:
        - labelSelector:
            matchLabels:
              app: {{ template "consul.name" . }}
              release: "{{ .Release.Name }}"
              component: server
          topologyKey: kubernetes.io/hostname

client:
  enabled: true
  # DaemonSet，每个节点都部署
  resources:
    requests:
      memory: "100Mi"
      cpu: "50m"
    limits:
      memory: "200Mi"

ui:
  enabled: true
  service:
    type: ClusterIP

connectInject:
  enabled: false  # 暂时不启用 Service Mesh 注入
```

```bash
kubectl create namespace consul
helm install consul hashicorp/consul -n consul -f consul-values.yaml

# 等待 Server Pod 就绪
kubectl -n consul get pods -w

# 查看集群状态
kubectl -n consul exec -it consul-server-0 -- consul members
```

首次部署启用了 ACL，需要获取 bootstrap token：

```bash
kubectl -n consul get secret consul-bootstrap-acl-token -o jsonpath='{.data.token}' | base64 -d
```

## 服务注册方式

### 方式一：配置文件注册（推荐）

在 Consul Agent 的配置目录放入服务定义文件，Agent 启动时自动注册，适合固定服务。

```json
// /etc/consul.d/web-service.json
{
  "service": {
    "id": "web-1",
    "name": "web",
    "address": "192.168.1.10",
    "port": 8080,
    "tags": ["v2", "prod"],
    "meta": {
      "version": "2.1.0",
      "region": "us-west-2"
    },
    "check": {
      "id": "web-health",
      "name": "HTTP health check",
      "http": "http://192.168.1.10:8080/health",
      "interval": "10s",
      "timeout": "3s",
      "deregister_critical_service_after": "30s"
    }
  }
}
```

修改后 reload 不需要重启 Agent：

```bash
consul reload
# 或发送 SIGHUP
kill -HUP $(pidof consul)
```

### 方式二：HTTP API 注册（适合动态场景）

K8s 中服务实例动态变化，用 API 注册更灵活。可以在服务的启动脚本或 init container 中调用：

```bash
# 注册服务
curl -s -X PUT http://localhost:8500/v1/agent/service/register \
  -H "Content-Type: application/json" \
  -H "X-Consul-Token: ${CONSUL_TOKEN}" \
  -d '{
    "ID": "payment-service-pod-abc123",
    "Name": "payment-service",
    "Address": "10.0.1.45",
    "Port": 9090,
    "Tags": ["v1.2.0"],
    "Check": {
      "HTTP": "http://10.0.1.45:9090/healthz",
      "Interval": "15s",
      "Timeout": "5s",
      "DeregisterCriticalServiceAfter": "60s"
    }
  }'

# 注销服务（在 preStop hook 中调用）
curl -s -X PUT http://localhost:8500/v1/agent/service/deregister/payment-service-pod-abc123 \
  -H "X-Consul-Token: ${CONSUL_TOKEN}"
```

K8s Pod 的 lifecycle 配置：

```yaml
lifecycle:
  preStop:
    exec:
      command:
        - "/bin/sh"
        - "-c"
        - |
          curl -s -X PUT http://localhost:8500/v1/agent/service/deregister/${POD_NAME} \
            -H "X-Consul-Token: ${CONSUL_TOKEN}"
          sleep 5
```

## 健康检查类型

Consul 支持多种健康检查方式，根据服务类型选择合适的：

### HTTP 检查

最常用，适合有 HTTP 接口的服务：

```json
{
  "check": {
    "http": "http://localhost:8080/health",
    "method": "GET",
    "header": {
      "Authorization": ["Bearer internal-token"]
    },
    "interval": "10s",
    "timeout": "3s"
  }
}
```

### TCP 检查

适合数据库、缓存等没有 HTTP 接口的服务：

```json
{
  "check": {
    "tcp": "localhost:5432",
    "interval": "10s",
    "timeout": "3s"
  }
}
```

### Script 检查

执行自定义脚本，exit 0 为 healthy，exit 1 为 warning，exit 2 为 critical：

```json
{
  "check": {
    "args": ["/opt/scripts/check-queue-depth.sh"],
    "interval": "30s",
    "timeout": "10s"
  }
}
```

```bash
#!/bin/bash
# /opt/scripts/check-queue-depth.sh
QUEUE_DEPTH=$(redis-cli llen pending_jobs)
if [ "$QUEUE_DEPTH" -gt 10000 ]; then
  echo "Queue depth critical: $QUEUE_DEPTH"
  exit 2
elif [ "$QUEUE_DEPTH" -gt 5000 ]; then
  echo "Queue depth warning: $QUEUE_DEPTH"
  exit 1
fi
echo "Queue depth OK: $QUEUE_DEPTH"
exit 0
```

### TTL 检查

由服务自己主动定期 push 心跳，适合批处理任务：

```json
{
  "check": {
    "ttl": "30s",
    "deregister_critical_service_after": "5m"
  }
}
```

服务需要定期调用 API 更新状态：

```bash
# 每 20s push 一次心跳（TTL 30s 内必须收到）
curl -s -X PUT http://localhost:8500/v1/agent/check/pass/service:my-batch-job \
  -d '{"Output": "Last run: success at 2026-04-11 08:00:00"}'
```

## DNS 服务发现

Consul 内置 DNS 服务（默认监听 8600 端口），服务注册后可以通过 DNS 名称访问：

```
<service-name>.service.consul        # 返回所有健康实例的 A 记录
<service-name>.service.<dc>.consul   # 指定数据中心
<tag>.<service-name>.service.consul  # 按 tag 筛选
```

```bash
# 查询 web 服务（返回健康实例的 IP）
dig @127.0.0.1 -p 8600 web.service.consul A

# 查询 SRV 记录（同时返回端口）
dig @127.0.0.1 -p 8600 web.service.consul SRV

# 按 tag 查询（只返回 v2 版本的实例）
dig @127.0.0.1 -p 8600 v2.web.service.consul A
```

在 K8s 中，可以在 CoreDNS 配置里加 stub zone，把 `.consul` 域名转发给 Consul DNS：

```yaml
# coredns ConfigMap 追加
consul {
  errors
  cache 30
  forward . <consul-dns-service-ip>:8600
}
```

这样 K8s Pod 里直接 `curl http://web.service.consul:8080` 就能访问到健康的 web 实例，无需任何 Service 或 Endpoint 配置。

## Prometheus 集成：Consul 作为服务发现源

Prometheus 支持用 Consul 做服务发现（`consul_sd_configs`），这样新注册的服务会自动被 Prometheus 发现并开始采集，不需要手动修改 `prometheus.yml`。

```yaml
# prometheus.yml
scrape_configs:
  - job_name: 'consul-services'
    consul_sd_configs:
      - server: 'consul.consul.svc.cluster.local:8500'
        token: '<consul-read-token>'
        services: []  # 空表示发现所有服务，也可以指定服务名列表
    
    relabel_configs:
      # 只采集带有 prometheus_scrape=true tag 的服务
      - source_labels: [__meta_consul_tags]
        regex: '.*,prometheus_scrape=true,.*'
        action: keep
      
      # 用服务名作为 job label
      - source_labels: [__meta_consul_service]
        target_label: job
      
      # 用 Consul meta 中的 metrics_path 覆盖默认路径
      - source_labels: [__meta_consul_service_metadata_metrics_path]
        regex: '(.+)'
        target_label: __metrics_path__
      
      # 用数据中心作为 dc label
      - source_labels: [__meta_consul_dc]
        target_label: dc
```

服务注册时添加对应的 tag 和 meta：

```json
{
  "service": {
    "name": "order-service",
    "port": 8080,
    "tags": ["prometheus_scrape=true"],
    "meta": {
      "metrics_path": "/actuator/prometheus"
    }
  }
}
```

这样 order-service 一注册，Prometheus 就会自动开始采集其 `/actuator/prometheus` 接口。

## 踩坑记录

### 坑 1：ACL 配置导致服务无法注册

症状：启用了 ACL 后，新服务调用 `/v1/agent/service/register` 返回 403，日志显示 `Permission denied`。

原因：Consul 的 ACL 是基于 Token + Policy 的，需要为每个服务创建对应的 Policy 并绑定 Token。很多人开启 ACL 后直接用 bootstrap token 测试没问题，但给服务分配了权限不足的 token。

正确做法：为每个服务或服务组创建专用 Policy：

```hcl
# payment-service-policy.hcl
service "payment-service" {
  policy = "write"
}
service_prefix "" {
  policy = "read"
}
node_prefix "" {
  policy = "read"
}
```

```bash
# 创建 policy
consul acl policy create -name "payment-service" -rules @payment-service-policy.hcl

# 创建 token 并绑定 policy
consul acl token create -description "payment-service token" \
  -policy-name "payment-service"
```

ACL 调试时可以临时用 `consul acl token read -self` 确认当前 token 的权限范围。

### 坑 2：跨数据中心 Federation 延迟导致服务发现不一致

我们有 us-west-2 和 ap-southeast-1 两个数据中心，用 Consul WAN Federation 打通。

症状：在 ap-southeast-1 查询 us-west-2 的服务时，偶发查到已下线的实例，导致请求超时。

根因分析：
- WAN Gossip 在跨大洋的情况下延迟可能达到 150-200ms
- Server 节点间的状态同步依赖 WAN Gossip，并不是强一致的实时同步
- ap-southeast-1 的 Server 看到 us-west-2 服务状态的更新有几秒到几十秒的延迟

解决思路：
1. 优先使用本地数据中心的服务（通过 tag 区分 region，客户端优先选同 region 的实例）
2. 降低健康检查间隔，让故障实例更快被标记为 critical
3. `deregister_critical_service_after` 设置短一点（如 30s），让僵尸实例尽快被清理
4. 客户端做好重试和熔断，不依赖服务发现的强一致性

```json
{
  "check": {
    "interval": "5s",
    "timeout": "3s",
    "deregister_critical_service_after": "30s"
  }
}
```

### 坑 3：Agent 重启后本地服务注册丢失

通过 API 注册的服务，默认存在 Agent 的内存里，Agent 重启后丢失，服务需要重新注册。

解决：启动 Agent 时加 `-config-dir` 参数，并在服务启动时把注册信息写到配置目录：

```bash
# 写入持久化配置文件
cat > /etc/consul.d/$(hostname)-services.json <<EOF
{
  "services": [
    ...
  ]
}
EOF

# reload consul 让其读取新配置
consul reload
```

或者使用 `-data-dir` 持久化，Consul 会把注册信息写入磁盘，重启后自动恢复（但仍需注意版本兼容性）。

### 坑 4：健康检查 Script 执行权限问题

在 K8s 环境中，Consul Agent 容器默认非 root 用户运行，Script Check 里的脚本如果依赖 `sudo` 或访问特权端口会失败。

解决方案：改用 HTTP 健康检查接口，把复杂的检查逻辑封装成一个小 HTTP 服务，Consul 通过 HTTP 调用而不是直接执行脚本。这样权限隔离更清晰，也方便调试。

## 常用运维命令速查

```bash
# 查看所有成员
consul members -detailed

# 查看集群 Leader
consul operator raft list-peers

# 强制离开某个节点（节点宕机无法正常注销时）
consul force-leave <node-name>

# 查看所有注册的服务
consul catalog services

# 查询某个服务的健康实例
consul health service web --passing

# 查看服务的所有健康检查状态
consul health checks web

# KV 操作
consul kv put config/app/debug "false"
consul kv get config/app/debug
consul kv delete config/app/debug

# 实时监听 KV 变化
consul watch -type=key -key=config/app/debug cat
```
