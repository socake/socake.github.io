---
title: "Nacos 一文通：从零基础到生产精通的配置中心与服务发现实战"
date: 2026-04-18T14:00:00+08:00
draft: false
tags: ["Nacos", "配置中心", "服务发现", "微服务", "Spring Cloud Alibaba", "运维"]
categories: ["中间件"]
description: "一篇文章吃透 Nacos：从核心概念、架构原理、单机与集群部署，到 Java/Go/Python SDK 接入、配置热更新、服务注册发现、生产调优、监控告警、真实故障复盘、安全加固、多集群同步，以及和 Apollo/Consul/etcd 的选型对比。"
summary: "Nacos 同时承担配置中心和服务注册发现两个核心职责，是 Spring Cloud Alibaba 生态的基石。本文系统梳理 Nacos 的数据模型、一致性协议、长轮询推送机制、临时实例健康检查、生产集群部署、多语言 SDK 接入、灰度发布、权限控制、常见故障排查（配置不生效/密码漂移/集群脑裂）以及云原生时代的定位，适合从入门到生产运维的完整参考。"
toc: true
math: false
diagram: false
keywords: ["Nacos", "配置中心", "服务发现", "Spring Cloud Alibaba", "Distro", "Raft", "长轮询", "命名空间", "灰度发布", "Apollo", "Consul"]
params:
  reading_time: true
---

我们这套系统的配置中心和服务发现都跑在 Nacos 上，接手了两年多。真正把它从"能跑"用到"放心扛生产"中间踩过的坑不少——鉴权、密码漂移、临时实例健康检查、长轮询卡住、集群同步失败。这篇把概念、部署、SDK 接入、运维踩坑、选型对比一起整理下来，基于 Nacos 2.x。

---

## 一、Nacos 到底是什么

Nacos 全称 **Dynamic Naming And Configuration Service**，"动态命名与配置服务"。名字直接告诉了你它做两件事：

1. **Naming**——服务注册与发现（替代 Eureka、Consul 的部分职责）
2. **Configuration**——配置中心（替代 Spring Cloud Config、Apollo 的部分职责）

**为什么两件事合在一起？** 从架构角度看，它们都是"数据要被一堆客户端监听、数据变了客户端要收到通知"。底层的长连接、watch 机制、一致性协议是共用的，合并能减少一整套组件。缺点是耦合：Nacos 挂了，既没配置又没服务列表，影响面比单独的配置中心大。

简单画一下它在系统里的位置：

```
  ┌────────────────┐     配置变更       ┌────────────────┐
  │ 运维/开发控制台 ├──────推送─────────>│                │
  └────────────────┘                   │                │
                                       │     Nacos      │
  ┌────────────────┐     注册/心跳     │    Cluster     │
  │  微服务实例 A  │<─────────────────>│                │
  │  微服务实例 B  │<─────────────────>│  (Server 3节点)│
  │  微服务实例 C  │<─────────────────>│                │
  └────────────────┘     订阅/推送     └───────┬────────┘
                                               │
                                               │ 持久化
                                               v
                                       ┌────────────────┐
                                       │  MySQL (外置)  │
                                       └────────────────┘
```

客户端同时承担两个角色：作为服务实例注册自己+订阅别人，同时作为配置消费者监听配置变更。

---

## 二、核心概念：四层资源模型

Nacos 的数据模型比 ZK/etcd 直观得多，不是一棵树，而是四层隔离：

```
Namespace (命名空间)          ← 环境隔离（dev/qa/prod）
  └── Group (分组)            ← 业务/项目隔离
        └── DataId (配置) 或 Service (服务)
              └── Cluster / Instance
```

**Namespace（命名空间）**

默认 `public`，生产一定要建新的，不要用 public。典型用法是按环境切：`dev`、`qa`、`pre`、`prod` 各一个 namespace，**彼此完全不可见**。Namespace 用 UUID 作为真正的 ID，控制台显示的是名字。SDK 里填的是 UUID，**别填名字**，这是新手常见的第一个坑。

**Group（分组）**

Namespace 内部的二级隔离，默认 `DEFAULT_GROUP`。常见用法：

- 按业务线分组：`ORDER_GROUP`、`USER_GROUP`、`PAY_GROUP`
- 按产品线分组：`PRODUCT_A`、`PRODUCT_B`
- 灰度组：`BETA_GROUP` 专放灰度配置

**DataId（配置文件 ID）**

一个 DataId 就是一份配置。命名约定（Spring Cloud Alibaba 默认规则）：

```
${prefix}-${spring.profiles.active}.${file-extension}
例：user-service-prod.yaml
```

不用 Spring 的话约定你自己的。推荐规则：`服务名-用途-环境.扩展名`，例如 `payment-datasource-prod.yaml`。

**Service（服务）**

服务发现侧的主体，和 DataId 平级。一个 Service 下有多个 Instance（实例），Instance 可以再按 Cluster 分组（常用来区分机房/AZ，做就近调用）。

---

## 三、架构与一致性：Raft 还是 AP

Nacos 1.x/2.x 架构差别很大，但一致性模型是相似的：**同时支持 CP 和 AP，按数据类型切换**。

### 两种一致性协议

**CP 模式：Raft（JRaft 实现）**

用在：
- **持久化服务实例**（`ephemeral=false`）：比如基础中间件实例，强一致
- **配置数据**：Nacos 2.x 后配置走 Raft 保证强一致

特点：写入需要过半节点确认，Leader 故障期间短暂不可写。

**AP 模式：Distro（自研，类 Gossip + 分片）**

用在：
- **临时服务实例**（`ephemeral=true`，**默认**）：普通微服务注册走这里

特点：
- 每个 Nacos 节点负责自己分片上的实例数据
- 其他节点通过异步 Gossip 同步
- 节点挂了分片重新分配，15s 内完成
- 牺牲强一致换高可用：写入只要本节点成功就返回

**一句话总结**：默认的微服务注册走 AP，配置和持久实例走 CP。除非你在写注册中间件或数据库之类的基础设施，否则别改 `ephemeral=false`。

### 配置推送是怎么做到"变了马上通知"的

Nacos 1.x 用的是 **HTTP 长轮询**（long polling），不是 WebSocket，不是服务端推送：

1. 客户端发 HTTP 请求问：`user-service-prod.yaml` 变了吗？
2. 服务端 hold 住这个请求最多 **29.5 秒**
3. 这 29.5 秒内如果配置变了，服务端立即返回变更
4. 如果没变，29.5 秒后返回"没变"，客户端再次发起

好处：穿透企业防火墙/NAT 无压力，HTTP 层设施都能直接用。
代价：每个客户端每 30 秒一次请求，万级实例时 Nacos 承压可观。

Nacos 2.x 改成了 **gRPC 长连接**，同机器配置和服务发现共用一条连接，推送延迟大幅下降，服务端压力也小了。新项目直接上 2.x。

### 临时实例的健康检查

客户端通过 **心跳** 保活：

- 默认 5 秒发一次心跳
- 15 秒没心跳标记为不健康（从服务列表摘除）
- 30 秒没心跳实例被删除

持久实例则是服务端主动健康检查（TCP/HTTP/MySQL 探活），类似 Consul。

---

## 四、部署：从 Docker 到生产集群

### 单机起飞（本地开发）

最快方案是 Docker：

```bash
docker run -d \
  --name nacos \
  -p 8848:8848 -p 9848:9848 -p 9849:9849 \
  -e MODE=standalone \
  -e JVM_XMS=512m -e JVM_XMX=512m \
  nacos/nacos-server:v2.3.2
```

三个端口的职责：
- `8848`：HTTP API + 控制台
- `9848`：客户端 gRPC（2.x 新增）
- `9849`：服务端间 gRPC

控制台访问 `http://localhost:8848/nacos`，默认 `nacos/nacos`。**上线前必改**。

### 生产集群（3 节点 + 外置 MySQL）

单机版的数据是存嵌入式 Derby 的，**不可用于生产**。生产必须：

1. **最少 3 节点**（Raft 过半，挂 1 个还能选主）
2. **外置 MySQL**（存配置数据、用户、命名空间等元数据）

拓扑：

```
      ┌─────────────┐
      │  SLB/Nginx  │ (VIP: nacos.internal:8848)
      └──────┬──────┘
             │
   ┌─────────┼─────────┐
   v         v         v
┌─────┐  ┌─────┐  ┌─────┐
│node1│  │node2│  │node3│  集群内 Raft / Distro 同步
└──┬──┘  └──┬──┘  └──┬──┘
   │        │        │
   └────────┼────────┘
            v
      ┌──────────┐
      │  MySQL   │ (主备/RDS)
      └──────────┘
```

关键配置 `conf/application.properties`：

```properties
# 指定外置 MySQL
spring.datasource.platform=mysql
db.num=1
db.url.0=jdbc:mysql://rds.internal:3306/nacos?characterEncoding=utf8&connectTimeout=1000&socketTimeout=3000&autoReconnect=true
db.user.0=nacos
db.password.0=<从 secret 注入>

# 鉴权（生产必开）
nacos.core.auth.enabled=true
nacos.core.auth.server.identity.key=<32字节随机串>
nacos.core.auth.server.identity.value=<32字节随机串>
nacos.core.auth.plugin.nacos.token.secret.key=<Base64,32字节>

# 集群节点（也可以放 conf/cluster.conf）
# 172.31.1.10:8848
# 172.31.1.11:8848
# 172.31.1.12:8848
```

**三个生产必改项**：

1. `nacos.core.auth.enabled=true`——默认是 `false`，不开等于裸奔
2. `nacos.core.auth.plugin.nacos.token.secret.key`——**必须换**，官方默认值在 GitHub 文档里人人可见，CVE 级风险
3. `nacos.core.auth.server.identity.*`——服务端间通信密钥，也必须换

### K8s 部署

官方 Helm Chart 或者 nacos-k8s 仓库都可以：

```bash
helm repo add nacos https://nacos-group.github.io/nacos-k8s/
helm install nacos nacos/nacos \
  --set global.mode=cluster \
  --set replicaCount=3 \
  --set persistence.enabled=true \
  --set mysql.enabled=false \
  --set nacos.storage.db.host=rds.internal \
  --set nacos.storage.db.name=nacos \
  --set nacos.storage.db.username=nacos \
  --set nacos.storage.db.password.existingSecret=nacos-db
```

**K8s 部署的几个坑**：

- **StatefulSet + Headless Service**：Nacos 节点需要稳定的 hostname 用于 Raft 选举，必须用 StatefulSet
- **不要用 ClusterIP 做集群内通信**：节点间走 Pod IP / hostname，别走 Service
- **Pod 重启 IP 变化**：客户端连的是 VIP，内部节点用 hostname 稳定
- **资源 request 给足**：4C8G 起步，128M 那种给开发玩的配置不要照抄

---

## 五、接入：各语言 SDK

### Java / Spring Cloud Alibaba（最主流）

Maven 依赖：

```xml
<dependency>
    <groupId>com.alibaba.cloud</groupId>
    <artifactId>spring-cloud-starter-alibaba-nacos-config</artifactId>
</dependency>
<dependency>
    <groupId>com.alibaba.cloud</groupId>
    <artifactId>spring-cloud-starter-alibaba-nacos-discovery</artifactId>
</dependency>
```

`bootstrap.yaml`（**必须是 bootstrap 不是 application**，Spring 加载顺序问题）：

```yaml
spring:
  application:
    name: user-service
  cloud:
    nacos:
      config:
        server-addr: nacos.internal:8848
        namespace: <UUID>      # 用 UUID，不是名字
        group: USER_GROUP
        file-extension: yaml
        username: user-service
        password: ${NACOS_PASSWORD}
      discovery:
        server-addr: nacos.internal:8848
        namespace: <UUID>
        group: USER_GROUP
        metadata:
          version: v1.2.0
          az: cn-hangzhou-a
```

配置热更新，字段上加 `@RefreshScope`：

```java
@Component
@RefreshScope
@ConfigurationProperties(prefix = "biz.pay")
public class PayConfig {
    private int timeout;
    private String gateway;
    // ...
}
```

**`@RefreshScope` 的隐形坑**：被它标记的 Bean 是懒加载的，第一次使用才创建。放到 `@Bean` 方法上时，如果这个 Bean 被其他单例 Bean 注入，热更新不会生效——被持有的还是旧引用。解决：用 `ObjectProvider<>` 或者 `ApplicationContextAware` 拿最新 Bean。

### Go（nacos-sdk-go）

```go
import (
    "github.com/nacos-group/nacos-sdk-go/v2/clients"
    "github.com/nacos-group/nacos-sdk-go/v2/common/constant"
    "github.com/nacos-group/nacos-sdk-go/v2/vo"
)

sc := []constant.ServerConfig{
    *constant.NewServerConfig("nacos.internal", 8848),
}
cc := *constant.NewClientConfig(
    constant.WithNamespaceId("<UUID>"),
    constant.WithUsername("user-service"),
    constant.WithPassword(os.Getenv("NACOS_PASSWORD")),
    constant.WithTimeoutMs(5000),
    constant.WithLogDir("/var/log/nacos"),
    constant.WithCacheDir("/var/cache/nacos"),
)

// 配置客户端
configClient, _ := clients.NewConfigClient(vo.NacosClientParam{
    ClientConfig:  &cc,
    ServerConfigs: sc,
})

// 获取配置 + 监听变更
content, _ := configClient.GetConfig(vo.ConfigParam{
    DataId: "user-service-prod.yaml",
    Group:  "USER_GROUP",
})

configClient.ListenConfig(vo.ConfigParam{
    DataId: "user-service-prod.yaml",
    Group:  "USER_GROUP",
    OnChange: func(namespace, group, dataId, data string) {
        log.Printf("config changed: %s", data)
        // 解析并 atomic.Store 到运行时配置
    },
})
```

**Go SDK 两个坑**：

1. `OnChange` 回调里**不要做阻塞操作**（比如等数据库连接池重建），会阻住后续事件推送。需要重的操作发 channel 给 goroutine 处理。
2. `WithCacheDir` 一定要配到**持久化路径**，默认是进程工作目录。客户端启动时如果 Nacos 暂时不可用，会走本地缓存容灾。工作目录在容器里重启即清空。

### Python（nacos-sdk-python）

```python
import nacos

client = nacos.NacosClient(
    "nacos.internal:8848",
    namespace="<UUID>",
    username="user-service",
    password=os.getenv("NACOS_PASSWORD"),
)

# 获取配置
content = client.get_config("user-service-prod.yaml", "USER_GROUP")

# 监听
def callback(args):
    print(f"config changed: {args['content']}")

client.add_config_watcher(
    "user-service-prod.yaml", "USER_GROUP", callback
)
```

Python SDK 官方实现功能相对简洁，回调是同步的，自己控制好线程。

### HTTP API（兜底方案）

所有 SDK 底层都是 HTTP，语言不支持时直接调 API：

```bash
# 获取配置
curl "http://nacos.internal:8848/nacos/v1/cs/configs?dataId=user-service-prod.yaml&group=USER_GROUP&tenant=<namespace_uuid>" \
  -H "accessToken: <login_token>"

# 注册服务
curl -X POST "http://nacos.internal:8848/nacos/v1/ns/instance" \
  -d "serviceName=user-service" \
  -d "ip=10.0.1.5" \
  -d "port=8080" \
  -d "namespaceId=<UUID>" \
  -d "groupName=USER_GROUP"
```

---

## 六、配置中心实战

### 灰度发布（Beta 配置）

Nacos 支持给一批指定 IP 的客户端推新配置，其他客户端保持旧配置。控制台上编辑配置 → "发布 Beta"，填 IP 列表。

**常见用法**：

- 一批机器试新连接池参数
- 新版本的 Feature Flag 只对特定机器开放

**坑**：Beta IP 判断看的是客户端**注册时上报的 IP**。K8s 里 Pod IP 每次重建都变，Beta 实际上没法用——要么用元数据匹配（自己改 SDK），要么直接走 Group 切换（新建 `BETA_GROUP`，灰度机器读这个 Group）。

### 历史版本与回滚

控制台每次发布都会保存历史，保留时间默认 30 天。回滚就是"发布一个旧版本内容"。

运维视角要注意：**历史版本只在 MySQL 里**（`his_config_info` 表），不在 Raft 状态机里。DBA 清表需要和 SRE 对齐，别误删。

### 敏感配置加密

Nacos 本身不加密配置。生产里常见三种做法：

1. **KMS 加密**（阿里云版 Nacos 集成 KMS，开箱）
2. **Jasypt 客户端解密**（Java 生态常用）
3. **引用外部 Secret**（把 DB 密码放 Vault/K8s Secret，Nacos 只存引用）

推荐第 3 种，Nacos 里**不存明文密码**是最稳的，既不怕控制台泄漏也不怕 MySQL 备份外泄。

### DataId 命名约定（强烈推荐）

生产上没约定会乱成一锅粥。参考规则：

```
<service>-<purpose>-<env>.<ext>

user-service-application-prod.yaml      # 主业务配置
user-service-datasource-prod.yaml        # 数据源
user-service-feature-prod.yaml           # Feature flag
user-service-ratelimit-prod.yaml         # 限流
```

粒度细 = 热更新范围精确 = 变更风险小。把 500 行配置塞一个 DataId 里，改一行触发全量重载，谁都不敢按。

---

## 七、服务发现实战

### 注册与元数据

注册时可以带 metadata，**这是 Nacos 的金矿**：

```yaml
spring:
  cloud:
    nacos:
      discovery:
        metadata:
          version: v2.1.0
          region: cn-hangzhou
          az: zone-a
          weight: "100"
```

消费方可以基于 metadata 做路由：

```java
// Spring Cloud LoadBalancer 自定义策略
public class VersionAwareLoadBalancer implements ReactorServiceInstanceLoadBalancer {
    public Mono<Response<ServiceInstance>> choose(Request request) {
        // 从请求头拿版本
        String targetVersion = request.getHeader("X-Version");
        // 从 nacos 服务列表过滤出同版本实例
        // ...
    }
}
```

**应用场景**：

- 灰度发布：`version=v2` 的客户端只路由到 `version=v2` 的实例
- 就近访问：只路由到同 AZ 的实例
- 染色测试：打 `tag=dev-zhangsan` 的实例只接特定流量

### 权重调度

每个实例可以设权重（默认 1.0），负载均衡按权重分流量。用法：

- **新版本试水**：新版本实例权重设 0.1，流量 10%
- **摘除不摘实例**：出问题的实例权重设 0，但还在列表里方便观察

### 临时 vs 持久实例

默认 `ephemeral=true`，断心跳就删。什么时候用 `ephemeral=false`：

- 数据库/中间件等基础设施，IP 稳定不希望因网络抖动被删
- 要做服务端主动健康检查（TCP/HTTP probe）
- **不要**给普通微服务设 persistent——上线下线要手工删，麻烦且易漏

**一个容易忽略的点**：同一个 serviceName 在同一命名空间里，**不能同时有临时和持久实例**，会导致注册冲突。

### 订阅与推送

客户端订阅后，服务列表变化会被推送过来。Spring Cloud Alibaba 的 LoadBalancer 自动处理，你一般不用管。非 Spring 生态需要自己维护：

```go
namingClient.Subscribe(&vo.SubscribeParam{
    ServiceName: "order-service",
    GroupName:   "ORDER_GROUP",
    SubscribeCallback: func(services []model.Instance, err error) {
        // 更新本地路由表
    },
})
```

---

## 八、生产调优

### JVM 参数（以 4C8G 节点为例）

```bash
JVM_XMS=4g
JVM_XMX=4g
JVM_XMN=2g        # 年轻代 1/2，配置/服务发现对象生命周期短
JVM_MS=128m
JVM_MMS=320m

# GC：G1（Nacos 2.x 官方默认）
-XX:+UseG1GC
-XX:MaxGCPauseMillis=200
-XX:InitiatingHeapOccupancyPercent=45

# GC 日志
-Xlog:gc*,safepoint:file=/var/log/nacos/gc.log:time,tags:filecount=10,filesize=100M
```

**Xms=Xmx** 是必须的，避免运行时堆扩缩造成 GC 抖动。

### 客户端参数

```properties
# 长轮询 timeout，默认 30s，别改
com.alibaba.nacos.client.config.longPollingTimeout=30000

# 本地缓存目录（容灾！）
com.alibaba.nacos.client.local.snapshot.path=/data/nacos/cache

# 心跳间隔，默认 5s，低负载场景不用动
```

### 集群规模参考

| 实例数 | 配置数 | 节点配置 | 节点数 | 备注 |
|-------|--------|---------|--------|------|
| <1000 | <500 | 2C4G | 3 | 小规模测试 |
| 1000-5000 | 500-2000 | 4C8G | 3 | 中等业务 |
| 5000-20000 | 2000-5000 | 8C16G | 3~5 | 中大型 |
| >20000 | >5000 | 8C16G+ | 5~7 | 考虑分集群+按业务隔离 |

超过 2w 实例强烈建议按业务线拆 Nacos 集群，单集群再牛也扛不住单点故障面。

### MySQL 侧

- 数据量本身小（几百 MB 级），但有很多 DDL/DML，I/O 要稳
- 开启 binlog 防误操作能恢复
- 推荐用 RDS，别自建，省心

---

## 九、监控告警

Nacos 内置 Prometheus metrics endpoint：`/nacos/actuator/prometheus`。

**必须覆盖的告警规则**：

```yaml
groups:
- name: nacos
  rules:
  # 1. 节点存活
  - alert: NacosInstanceDown
    expr: up{job="nacos"} == 0
    for: 1m

  # 2. 长轮询队列堆积（2.x 已弱化，但 1.x 看这个）
  - alert: NacosLongPollingHigh
    expr: nacos_monitor{name="longPolling"} > 10000
    for: 5m

  # 3. gRPC 连接数异常
  - alert: NacosGrpcConnDrop
    expr: delta(nacos_monitor{name="grpcConnectionCount"}[5m]) < -500
    for: 2m

  # 4. 配置变更频繁（潜在事故信号）
  - alert: NacosConfigChangeStorm
    expr: rate(nacos_monitor{name="configPublish"}[5m]) > 2

  # 5. DB 连接池
  - alert: NacosDBPoolExhausted
    expr: hikaricp_connections_active{application="nacos"} / hikaricp_connections_max > 0.9
    for: 3m

  # 6. Raft 选举抖动
  - alert: NacosRaftLeaderChange
    expr: changes(nacos_raft_leader[10m]) > 2
```

**仪表盘必看四项**：

1. 每节点 QPS（读/写分开）
2. gRPC/长轮询客户端连接数
3. JVM Old Gen 使用率 + Full GC 次数
4. MySQL 连接池占用率

---

## 十、故障排查：真实场景复盘

下面几个都是实打实踩过的坑，作为故障模型记下来比看原理更有用。

### 场景 1：服务注册不上来

**现象**：客户端启动日志无异常，Nacos 控制台看不到实例。

**排查顺序**：

1. **鉴权**：2.x 开启鉴权后，客户端没配 username/password，注册请求直接 403，但有些 SDK 不报错只 warn。查 Nacos `naming-server.log`。
2. **命名空间 ID 写的是名字而不是 UUID**：控制台看不到实例但 `public` 空间能看到——实例跑到默认空间去了。
3. **网络**：K8s 里跨 namespace 调用，NetworkPolicy 拦截了 8848/9848。`tcpdump` 在 Nacos 侧看是否收到注册请求。
4. **客户端 IP 选错**：多网卡主机，SDK 默认取第一块，可能是 docker0。用 `spring.cloud.nacos.discovery.ip` 手动指定。

### 场景 2：配置改了但服务没反应

**现象**：控制台显示配置已发布，服务依然用旧值。

**排查顺序**：

1. **客户端 namespace/group 错了**：改的是 `PROD` 空间，客户端连的是 `QA`。先 `curl` 客户端侧接口确认读到的是谁。
2. **`@RefreshScope` 没加**：Java 客户端最常见。字段上没加这个注解，配置推送来了但字段不会更新。
3. **`@Value` 读取的是单例 Bean 里的值**：该 Bean 没被 `@RefreshScope` 包裹。
4. **自己写的 `OnChange` 回调没处理对**：Go/Python 客户端手动写的监听，回调里只打了 log 没真正更新运行时。
5. **本地缓存命中**：`cacheDir` 里有旧文件，Nacos 不可达时客户端走缓存。查 `cacheDir/config/<tenant>/<group>/<dataId>` 时间戳。
6. **Nacos 本身没推送出去**：看 Nacos 侧 `config-push.log`，如果根本没有推送记录，说明 Nacos 集群同步出了问题。

### 场景 3：Nacos 里配的 DB 密码和 RDS 实际密码漂移

**现象**：Pod 重启才连不上 DB（`28P01 password authentication failed`），已启动的 Pod 一切正常。

**原因**：
- 运维在 RDS 侧改了密码，没同步改 Nacos 配置
- 存量 Pod 已经持有了有效连接，连接池长期持有不重连，问题被掩盖
- 直到 Pod 重启或连接池被驱逐，才触发重新认证，瞬间集体挂

**预防**：

- DB 密码改动必须同步 4 个地方：**RDS + Nacos + 监控 exporter + 备份脚本**，做成 checklist
- 连接池配 `maxLifetime`（10-30 分钟）强制周期性重建，早暴露早发现
- Nacos 里 DB 密码用引用方式（见第六节加密部分），RDS 一次改，所有引用方自动拉新值

### 场景 4：集群"脑裂"式的数据不一致

**现象**：A 机房客户端看不到 B 机房注册的实例，但两侧 Nacos 控制台显示集群 3 个节点都是 UP。

**原因**：
- Distro 协议的分片同步走单次 HTTP，失败重试间隔较长
- 跨 AZ 网络闪断几十秒，分片同步失败但节点健康检查正常
- 不同节点上看到的实例列表出现差异

**排查**：
- `curl http://<node>:8848/nacos/v1/ns/operator/metrics` 看各节点的 `responsibleServiceCount`
- 三节点加起来应该等于总服务数，不等说明分片数据丢了
- 手工触发全量同步：`curl -X PUT http://<node>:8848/nacos/v1/ns/operator/distro/sync`

### 场景 5：启动特别慢（1+ 分钟）

**现象**：Nacos 节点冷启动需要 1-2 分钟才能对外提供服务。

**可能原因**：

- MySQL 网络延迟大，启动时加载全量配置/服务慢
- `cluster.conf` 里写了 DNS，启动时解析失败走超时
- 磁盘 I/O 拉胯，Raft 日志重放慢
- 配置项/服务数量太多（Nacos 不适合几万级配置，这时要拆集群）

---

## 十一、安全加固

Nacos 是**高度敏感**的组件，拿到管理员权限等于拿到所有微服务的配置和服务列表，甚至能推恶意配置让整个系统执行任意逻辑。

### 必做清单

1. **改默认密码**：`nacos/nacos` 首次登录强制改
2. **开启鉴权**：`nacos.core.auth.enabled=true`
3. **改默认 secret**：
   - `nacos.core.auth.plugin.nacos.token.secret.key`
   - `nacos.core.auth.server.identity.key/value`
4. **网络隔离**：8848/9848 只对内网 + 办公网 + 跳板机开放，**绝不能暴露公网**
5. **最小权限**：每个服务创建独立账号，按 namespace 分配只读/读写权限
6. **审计日志**：`nacos.core.auth.audit.enabled=true`，配置变更记录到日志
7. **TLS 开启**（2.x 支持）：集群内通信 + 客户端通信都走 HTTPS/gRPCs

### 历史 CVE 回顾

- **CVE-2021-29441**：未授权绕过，利用 User-Agent `Nacos-Server` 跳过鉴权。2.0.0-ALPHA.1 之前版本。
- **CVE-2021-29442**：默认 JWT secret 写死，所有默认部署可伪造 admin token。
- **未授权访问**：`nacos.core.auth.enabled=false` 默认就是未授权，大量暴露公网的 Nacos 直接被扫。

**保险做法**：版本升到 2.3.x+，所有 secret 全部自定义，内网访问 + 审计 + 定期扫描端口暴露。

---

## 十二、高级话题

### 多集群数据同步

场景：US Prod 和 CN Prod 各自有独立 Nacos 集群，某些配置需要一致。

方案：

1. **Nacos-Sync（官方工具）**：一对多同步配置或服务实例，支持 Nacos → Nacos、Eureka → Nacos、ZK → Nacos
2. **应用层双写**：发布工具同时往两个集群写（更可控，但要处理一致性）
3. **GitOps**：配置以 Git 为 SoT，CI 推到所有 Nacos 集群（最推荐，审计清楚）

### 配置模板化

大量服务有类似配置（连接池、日志级别、限流），每个服务一个 DataId 维护痛苦。

策略：

- 用 `shared-configs` 拆共享配置，业务 DataId 只放差异
- Spring Cloud Alibaba 支持 `shared-configs` 数组，顺序合并

```yaml
spring:
  cloud:
    nacos:
      config:
        shared-configs:
          - data-id: common-db-pool.yaml
            group: COMMON_GROUP
            refresh: true
          - data-id: common-log.yaml
            group: COMMON_GROUP
            refresh: true
```

### Nacos 2.x 关键新能力

- **gRPC 长连接**：替代 HTTP 长轮询，降延迟、省资源
- **推送性能 10x**：大规模下优势明显
- **配置一致性升级**：从最终一致到强一致（Raft）

新项目直接 2.3.x。老项目 1.x 升级 2.x 客户端协议兼容，平滑可做。

---

## 十三、选型对比：到底什么时候用 Nacos

| 维度 | Nacos | Apollo | Consul | etcd | Eureka |
|------|-------|--------|--------|------|--------|
| 配置中心 | ✅ | ✅（更专业）| 基础 | 基础 KV | ❌ |
| 服务发现 | ✅ | ❌ | ✅ | 基础 | ✅ |
| 一致性 | AP + CP 混合 | 最终一致 | Raft CP | Raft CP | AP |
| 推送机制 | 长轮询/gRPC | HTTP 长轮询 | Watch | Watch | 定时拉 |
| 部署复杂度 | 中（外置 DB）| 中（多组件）| 低 | 低 | 低 |
| 控制台 | ✅ 好用 | ✅ 最好用 | 一般 | ❌ | ❌（停更）|
| K8s 集成 | 一般 | 一般 | 好 | 原生 | 一般 |
| 社区活跃度 | 国内活跃 | 国内活跃 | 全球活跃 | 全球最活跃 | 停更 |
| 典型场景 | Spring Cloud Alibaba | 纯配置中心 | 多语言/HashiCorp 栈 | K8s / Go 生态 | Spring Cloud Netflix |

**怎么选**：

- **Spring Cloud Alibaba 栈**：毫不犹豫用 Nacos，生态开箱
- **只要配置中心**：Apollo 更专业，灰度/审批/权限更细
- **多语言微服务、HashiCorp 栈**：Consul
- **云原生 / Go 生态 / 轻量**：etcd + 自己组合
- **Netflix 老项目**：Eureka 已停更，应该迁出
- **要多集群同步**：Nacos-Sync 成熟，或走 GitOps

---

## 十四、云原生时代的定位

K8s 已经自带 Service（服务发现）和 ConfigMap（配置），为什么还要 Nacos？

**Service/ConfigMap 的短板**：

- **ConfigMap 变更不自动热更新**：挂载文件需要手动重载，环境变量压根不生效
- **滚动更新成本高**：改 ConfigMap 后通常要重启 Pod
- **无审计、无灰度、无历史版本**：控制台等于没有
- **跨集群共享困难**：ConfigMap 是 namespace scoped，跨集群得靠 sync 工具
- **Service 不支持权重、元数据路由**：Istio 才能补齐，又是一套

**Nacos 的短板**：

- 多一套基础设施要运维
- 单点故障面大
- 客户端 SDK 侵入代码

**推荐定位**：

- **纯 K8s 内部 + 不需要灰度/审计的配置**：用 ConfigMap + Reloader/Kustomize 足够
- **需要动态配置热更新 + 跨集群 + 灰度**：Nacos 不可替代
- **服务发现**：K8s Service 够用，除非跨集群或需要元数据路由
- **混合部署（VM + K8s）**：Nacos 是最自然的选择，K8s Service 出了集群就失效

**实战经验**：生产上最常见是"K8s Service + Nacos 配置中心"的组合，服务发现用 K8s 原生，配置走 Nacos 享受热更新和灰度。

---

## 十五、总结

Nacos 本质是"动态数据 + 多客户端订阅"的通用解决方案，刚好在配置中心和服务发现两个场景同时命中。它在中文微服务生态的地位短期内无可替代，但也不是银弹：

- **入门友好**：Spring Cloud Alibaba 加两个依赖就能跑
- **深入有坑**：鉴权、命名空间、`@RefreshScope`、临时/持久实例、长轮询机制，每个都能踩半天
- **运维不简单**：Raft + Distro 混合模型、外置 MySQL、集群规模限制，生产必须有系统性监控
- **安全是重头**：默认配置就是裸奔，所有 secret 都得换

真正决定你能不能扛住生产 Nacos 的，不是会不会部署集群，而是有没有把典型故障模型过一遍：密码漂移、注册不上、配置不生效、Distro 同步失败——这几个坑几乎每个跑 Nacos 的团队都会撞一次。再加上一开始就把 namespace/group/DataId 的命名约定立死、默认临时实例走 AP、配置走 CP 这两条路径别混着理解，基本就能把黑盒变成工具。
