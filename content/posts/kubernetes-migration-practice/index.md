---
title: "业务上云实战：传统应用容器化迁移的踩坑与经验"
date: 2025-05-19T12:36:00+08:00
draft: false
tags: ["Kubernetes", "迁移", "容器化", "DevOps", "云原生"]
categories: ["Kubernetes"]
description: "从评估、改造到流量切换，记录一次真实的传统应用容器化迁移过程，包括有状态应用处理、JVM 参数调优、时区问题等实际踩坑。"
summary: "把一批跑在虚拟机上的 Java 应用迁移到 Kubernetes，踩过的坑比想象中多。本文记录整个迁移过程的关键决策和教训。"
toc: true
math: false
diagram: false
keywords: ["Kubernetes", "容器化迁移", "Docker", "JVM", "EFS", "蓝绿部署", "灰度发布"]
params:
  reading_time: true
---

去年我们把一批运行了三四年的 Java 微服务从 EC2 虚拟机迁移到 Kubernetes，历时大概四个月。整个过程踩了不少坑，也积累了一些可以复用的方法论。本文按迁移流程顺序梳理，希望能给有类似需求的团队一些参考。

## 迁移前评估：先问清楚这几个问题

并不是所有应用都适合容器化，盲目迁移只会带来麻烦。评估阶段最重要的是搞清楚以下几点：

### 应用的状态类型

**无状态应用（优先迁移）：**
- API 服务、Web 应用
- 不依赖本地文件系统存储业务数据
- 启动/停止不影响数据完整性

**有状态应用（谨慎迁移）：**
- 自建数据库（MySQL、Redis、Elasticsearch）
- 依赖本地磁盘的文件处理服务
- 会话亲和性要求强的应用

一般建议第一批迁移无状态的 API 服务，积累经验后再处理有状态部分。自建数据库如果不是非常必要，建议直接用云服务（RDS、ElastiCache），不值得在 K8s 里自己运维。

### 依赖清单梳理

画一张应用依赖图，列出：
- 依赖哪些中间件（数据库、消息队列、缓存）
- 有没有依赖宿主机的特定路径或工具
- 有没有硬编码的 IP 地址（这个坑太多了）
- 服务间调用是否有直接用 IP 的情况

```bash
# 查看应用实际建立的网络连接，了解依赖
ss -antp | grep <pid>
lsof -p <pid> | grep -E 'IPv4|IPv6'

# 检查应用配置文件中的硬编码 IP
grep -r '10\.\|192\.168\.\|172\.' /app/config/
```

### 评估结果矩阵

我们用一个简单的矩阵来决定迁移优先级：

| 维度 | 低分 | 高分 |
|------|------|------|
| 有状态程度 | 无状态 | 强依赖本地存储 |
| 外部依赖 | 依赖少且标准化 | 依赖多且复杂 |
| 启动速度 | 秒级 | 分钟级 |
| 配置外化程度 | 已全部外化 | 大量硬编码 |

总分低的先迁，高的后迁或暂时不迁。

---

## 容器化改造步骤

### Dockerfile 编写

一个好的生产级 Dockerfile，要考虑镜像大小、安全性和构建缓存效率。

```dockerfile
# 多阶段构建：构建环境和运行环境分离
FROM maven:3.9-eclipse-temurin-17 AS builder
WORKDIR /build

# 先复制 pom.xml，利用 Docker 层缓存
# 只要依赖不变，这一层就不会重新下载
COPY pom.xml .
RUN mvn dependency:go-offline -q

COPY src ./src
RUN mvn package -DskipTests -q

# 运行镜像：使用精简基础镜像
FROM eclipse-temurin:17-jre-alpine
WORKDIR /app

# 创建非 root 用户
RUN addgroup -S appgroup && adduser -S appuser -G appgroup

# 复制构建产物
COPY --from=builder /build/target/app.jar app.jar

# 调整文件所有权
RUN chown appuser:appgroup app.jar

USER appuser

EXPOSE 8080

# JVM 参数通过环境变量注入，容器环境感知
ENV JAVA_OPTS="-XX:+UseContainerSupport \
  -XX:MaxRAMPercentage=75.0 \
  -XX:InitialRAMPercentage=50.0 \
  -XX:+ExitOnOutOfMemoryError \
  -Djava.security.egd=file:/dev/./urandom"

ENTRYPOINT ["sh", "-c", "exec java $JAVA_OPTS -jar app.jar"]
```

关键点：
- 多阶段构建把编译工具链排除在最终镜像之外，镜像从 800MB 降到 200MB 左右
- `UseContainerSupport` 让 JVM 感知容器的 CPU 和内存限制（后面会重点说这个坑）
- `ExitOnOutOfMemoryError` 让 OOM 时直接退出，而不是僵死，配合 K8s 的重启策略效果更好

### 配置外化

传统 Java 应用的配置通常硬编码在 `application.properties` 里。迁移时必须把所有环境相关的配置（数据库地址、服务端口、功能开关）都外化出来。

Spring Boot 的优先级：命令行参数 > 环境变量 > 配置文件，可以直接用环境变量覆盖。

```yaml
# ConfigMap：非敏感配置
apiVersion: v1
kind: ConfigMap
metadata:
  name: myapp-config
  namespace: production
data:
  SPRING_PROFILES_ACTIVE: "prod"
  SERVER_PORT: "8080"
  LOGGING_LEVEL_ROOT: "WARN"
  LOGGING_LEVEL_COM_EXAMPLE: "INFO"
  # 数据库连接（非敏感部分）
  DB_HOST: "mysql.production.svc.cluster.local"
  DB_PORT: "3306"
  DB_NAME: "myapp"
---
# Secret：敏感配置（生产建议配合 External Secrets）
apiVersion: v1
kind: Secret
metadata:
  name: myapp-secret
  namespace: production
type: Opaque
stringData:
  DB_PASSWORD: "your-password"
  JWT_SECRET: "your-jwt-secret"
```

### 日志标准化

容器化之后，日志要输出到 stdout/stderr，不能再写本地文件（容器重启就丢了，而且无法被日志采集器收集）。

```yaml
# logback-spring.xml 调整
<configuration>
  <appender name="STDOUT" class="ch.qos.logback.core.ConsoleAppender">
    <encoder class="net.logstash.logback.encoder.LogstashEncoder">
      <!-- 输出 JSON 格式，方便日志系统解析 -->
      <fieldNames>
        <timestamp>@timestamp</timestamp>
        <message>message</message>
      </fieldNames>
    </encoder>
  </appender>
  
  <root level="INFO">
    <appender-ref ref="STDOUT" />
  </root>
</configuration>
```

输出 JSON 格式日志有个重要好处：Fluentd/Vector 这类日志采集器可以直接解析，不需要写复杂的正则表达式。

---

## 有状态应用的处理

### 数据库迁移策略

自建 MySQL 迁移到 RDS 的基本步骤：

```bash
# 1. 全量导出
mysqldump \
  --single-transaction \
  --routines \
  --triggers \
  --databases myapp \
  -h old-mysql-host \
  -u root -p > myapp_full.sql

# 2. 导入到 RDS
mysql -h new-rds-endpoint -u admin -p myapp < myapp_full.sql

# 3. 开启 binlog 增量同步（使用 DMS 或 Canal）
# 保持源库和目标库持续同步，直到流量切换完成

# 4. 验证数据一致性
# 比较关键表的行数和 checksum
mysql -h new-rds-endpoint -e "
SELECT table_name, table_rows 
FROM information_schema.tables 
WHERE table_schema = 'myapp'
ORDER BY table_name;"
```

### 文件存储：EFS/NFS 挂载

有些应用需要在多个 Pod 间共享文件（比如用户上传的图片、报表文件）。K8s 里用 ReadWriteMany 的 PV 来解决，在 AWS 上对应 EFS。

```yaml
# PersistentVolume - EFS
apiVersion: v1
kind: PersistentVolume
metadata:
  name: efs-pv
spec:
  capacity:
    storage: 100Gi
  accessModes:
    - ReadWriteMany
  persistentVolumeReclaimPolicy: Retain
  storageClassName: efs-sc
  csi:
    driver: efs.csi.aws.com
    volumeHandle: fs-xxxxxxxxx   # EFS 文件系统 ID
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: shared-files-pvc
  namespace: production
spec:
  accessModes:
    - ReadWriteMany
  storageClassName: efs-sc
  resources:
    requests:
      storage: 100Gi
```

注意 EFS 的性能特点：延迟比 EBS 高（通常 1-5ms），适合低并发的文件访问。如果是高频读写的场景，考虑先缓存到本地 emptyDir，再异步同步到 EFS。

---

## 流量切换策略

流量切换是迁移中风险最高的环节，要有完整的回滚方案。

### 方案一：DNS 切换（最简单）

适合对短暂中断容忍度高的服务。

```bash
# 迁移前：DNS 指向旧 EC2
myapp.example.com -> 1.2.3.4 (EC2)

# 切换：将 DNS 改指向 K8s Ingress/ALB
myapp.example.com -> k8s-alb-xxxx.us-west-2.elb.amazonaws.com

# 注意事项：
# 1. 提前将 DNS TTL 降低到 60 秒，迁移完成后再恢复
# 2. 切换时间选择低峰期
# 3. 准备好快速回滚的 DNS 记录
```

### 方案二：蓝绿部署

新旧版本同时运行，通过负载均衡器切换流量，可以做到零中断切换。

```yaml
# 蓝色（旧版本）Service
apiVersion: v1
kind: Service
metadata:
  name: myapp-blue
spec:
  selector:
    app: myapp
    slot: blue
  ports:
    - port: 80
      targetPort: 8080
---
# 绿色（新版本）Service
apiVersion: v1
kind: Service
metadata:
  name: myapp-green
spec:
  selector:
    app: myapp
    slot: green
  ports:
    - port: 80
      targetPort: 8080
---
# 主 Service：通过修改 selector 切换流量
apiVersion: v1
kind: Service
metadata:
  name: myapp
spec:
  selector:
    app: myapp
    slot: green    # 改这一行实现切换，kubectl patch 即可
  ports:
    - port: 80
      targetPort: 8080
```

切换命令：
```bash
# 切到绿色（新版本）
kubectl patch service myapp -n production \
  -p '{"spec":{"selector":{"slot":"green"}}}'

# 发现问题，立即回滚到蓝色
kubectl patch service myapp -n production \
  -p '{"spec":{"selector":{"slot":"blue"}}}'
```

### 方案三：基于权重的灰度

使用 Ingress 的流量权重控制，先让 5% 的流量打到新版本，验证稳定后逐步提升。

```yaml
# 以 NGINX Ingress 为例
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: myapp-canary
  annotations:
    nginx.ingress.kubernetes.io/canary: "true"
    nginx.ingress.kubernetes.io/canary-weight: "10"  # 10% 流量到新版本
spec:
  rules:
    - host: myapp.example.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: myapp-green
                port:
                  number: 80
```

---

## 迁移后稳定性验证清单

流量切换后不是完事大吉，要系统性地验证：

```bash
# 1. 基础健康检查
kubectl get pods -n production -w
kubectl top pods -n production

# 2. 应用错误率（查日志）
kubectl logs -n production -l app=myapp --tail=200 | grep -i error

# 3. 资源使用是否在预期范围
kubectl describe hpa myapp-hpa -n production

# 4. 关键业务指标对比（对比迁移前后）
# - 接口 P99 延迟
# - 错误率
# - 吞吐量

# 5. 数据库连接数
# 确认连接池配置合理，容器化后副本数增加可能导致连接数暴涨

# 6. 外部依赖连通性
kubectl exec -n production deploy/myapp -- \
  curl -s http://external-api.example.com/health
```

建议迁移后保持旧版本（EC2）继续运行 1-2 周，期间密切监控，确认稳定后再下线。

---

## 踩坑记录

### 坑1：应用依赖宿主机路径

有个应用依赖 `/data/config/app.properties` 这个路径，在 EC2 上每台机器都有这个文件。容器化后路径不存在，应用直接启动失败。

排查过程：
```bash
# 容器内找不到文件
kubectl exec -it pod/myapp-xxx -- ls /data/config/
# ls: /data/config/: No such file or directory

# 查应用日志
kubectl logs pod/myapp-xxx
# FileNotFoundException: /data/config/app.properties
```

解决：把文件内容放到 ConfigMap，通过 volume 挂载到相同路径。

### 坑2：时区问题

应用里有很多定时任务，迁移到 K8s 后发现定时任务的触发时间全乱了。原因是容器默认时区是 UTC，而旧 EC2 配置的是 Asia/Shanghai。

```dockerfile
# Dockerfile 里设置时区
RUN apk add --no-cache tzdata && \
    cp /usr/share/zoneinfo/Asia/Shanghai /etc/localtime && \
    echo "Asia/Shanghai" > /etc/timezone && \
    apk del tzdata
```

或者通过环境变量：

```yaml
env:
  - name: TZ
    value: "Asia/Shanghai"
```

注意：有些 JVM 版本不认 `TZ` 环境变量，还需要加 JVM 参数 `-Duser.timezone=Asia/Shanghai`。

### 坑3：JVM 在容器内 CPU/内存识别错误

这是最坑的一个，也是最容易被忽视的。JDK 8u191 之前的版本，JVM 不识别容器的 cgroup 限制，会读取宿主机的 CPU 核数和内存总量来设置 GC 线程数和堆大小。

举个例子：容器 limits 是 2 CPU、4GB 内存，但宿主机是 96 核、512GB。JVM 以为自己有 96 核可用，GC 线程数直接飙到 24 个，反而严重影响性能。

```bash
# 验证 JVM 实际看到的 CPU 核数
kubectl exec -it pod/myapp-xxx -- \
  java -XX:+PrintFlagsFinal -version 2>&1 | grep ParallelGCThreads
```

解决方案：

1. 升级到 JDK 11+ 或 8u191+，开启 `UseContainerSupport`（11+ 默认开启）
2. 如果无法升级，手动指定 JVM 参数：

```bash
# 固定 GC 线程数
JAVA_OPTS="-XX:ParallelGCThreads=4 -XX:ConcGCThreads=2"

# 或者固定堆大小（推荐用百分比，更灵活）
JAVA_OPTS="-XX:MaxRAMPercentage=75.0 -XX:InitialRAMPercentage=50.0"
```

### 坑4：连接池耗尽

从 EC2 迁移到 K8s 后，副本数从 2 个变成了 6 个（配合 HPA），数据库连接数从 40 个变成了 120 个，直接触发 MySQL 的 `max_connections` 限制。

应对措施：
- 检查每个应用的连接池配置（HikariCP 默认 maximumPoolSize 是 10）
- 估算峰值副本数 × 每副本连接池大小，确保不超过数据库限制
- 上层加 PgBouncer/ProxySQL 连接池中间件

```yaml
# application.yaml
spring:
  datasource:
    hikari:
      maximum-pool-size: 5    # 从默认 10 降到 5，避免连接数爆炸
      minimum-idle: 2
      connection-timeout: 30000
      idle-timeout: 600000
```

---

## 迁移经验总结

容器化迁移不是一个纯技术问题，也是一个工程组织问题。几条实践下来的经验：

1. **小步走**：一次迁移一个服务，验证稳定后再迁下一个
2. **保留退路**：旧环境不要急着下线，留 2 周的观察期
3. **监控先行**：迁移前就把监控和告警配好，迁移后才能快速发现问题
4. **文档驱动**：每次迁移都写迁移记录，下次迁移同类应用可以直接复用

最大的教训是不要低估有状态应用的复杂性。我们有一个老服务依赖本地文件系统做会话存储（是的，你没看错），容器化改造几乎等于重写这部分逻辑。如果时间紧，这类应用不如暂时不动，等有空了再做彻底重构。
