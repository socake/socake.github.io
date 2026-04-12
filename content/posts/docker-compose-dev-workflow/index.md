---
title: "Docker Compose 本地开发工作流：多服务环境搭建最佳实践"
date: 2024-09-27T12:36:00+08:00
draft: false
tags: ["Docker", "Docker Compose", "开发环境", "DevOps", "容器", "本地开发"]
categories: ["Docker"]
series: ["DevOps 工程师成长路径"]
description: 'Docker Compose 本地开发工作流最佳实践：多服务依赖管理、healthcheck 启动顺序、代码热更新配置、网络隔离，打造高效的本地开发环境'
summary: "用 Docker Compose 搭建包含数据库、缓存、消息队列的完整本地环境，配合 healthcheck 确保启动顺序、bind mount 实现热更新，还有 override 模式分离开发和生产配置。这篇文章覆盖所有关键细节和常见踩坑。"
toc: true
math: false
diagram: false
keywords: ["Docker Compose", "本地开发", "healthcheck", "热更新", "compose watch", "多服务"]
params:
  reading_time: true
---

我见过太多团队本地开发环境一团糟：数据库版本不统一、Redis 有人装系统版有人用 Docker、消息队列根本没有本地环境所以某些功能只能在 QA 测。Docker Compose 的本意就是解决这个问题，但很多人只用了它 20% 的功能。这篇文章从一个真实的多服务项目出发，覆盖从基础配置到高级技巧的完整工作流。

## Compose v2 vs v1：先搞清楚用哪个

Docker Compose 有两个版本的命令行：

- **v1（旧）**：`docker-compose`，独立 Python 程序，已停止维护
- **v2（新）**：`docker compose`（中间是空格），Go 重写，内置在 Docker CLI 中

现在所有新项目都应该用 v2。配置文件名也有变化：官方推荐用 `compose.yaml`（而不是 `docker-compose.yml`），但两者都支持。

v2 还带来了几个重要变化：
- `version` 字段不再需要（历史遗留，写了也没关系）
- `depends_on` 支持 `condition: service_healthy`（这个 v1 也支持，但更稳定）
- `watch` 模式（Compose Watch）是 v2 的新特性
- profiles 功能可以按需启动服务子集

## 完整示例：FastAPI + PostgreSQL + Redis + Kafka

先给一个完整的 `compose.yaml`，后面逐一解释关键部分：

```yaml
services:
  # 应用服务
  api:
    build:
      context: .
      dockerfile: Dockerfile.dev
    ports:
      - "8000:8000"
    volumes:
      - ./app:/app/app          # 代码热更新
      - ./tests:/app/tests
    environment:
      DATABASE_URL: postgresql://dev:devpass@postgres:5432/mydb
      REDIS_URL: redis://redis:6379/0
      KAFKA_BROKERS: kafka:9092
      LOG_LEVEL: debug
    env_file:
      - .env.local              # 本地覆盖（不入 Git）
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
      kafka:
        condition: service_healthy
    command: uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

  # PostgreSQL
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: dev
      POSTGRES_PASSWORD: devpass
      POSTGRES_DB: mydb
    volumes:
      - postgres_data:/var/lib/postgresql/data
      - ./scripts/init.sql:/docker-entrypoint-initdb.d/init.sql  # 初始化脚本
    ports:
      - "5432:5432"             # 暴露到本机，方便 DBeaver 连接
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U dev -d mydb"]
      interval: 5s
      timeout: 5s
      retries: 10
      start_period: 10s         # 给 Postgres 初始化时间

  # Redis
  redis:
    image: redis:7-alpine
    command: redis-server --appendonly yes --maxmemory 256mb --maxmemory-policy allkeys-lru
    volumes:
      - redis_data:/data
    ports:
      - "6379:6379"
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 5

  # Zookeeper（Kafka 依赖）
  zookeeper:
    image: confluentinc/cp-zookeeper:7.6.0
    environment:
      ZOOKEEPER_CLIENT_PORT: 2181
      ZOOKEEPER_TICK_TIME: 2000
    healthcheck:
      test: ["CMD-SHELL", "echo srvr | nc localhost 2181 | grep -q Mode"]
      interval: 10s
      timeout: 5s
      retries: 5

  # Kafka
  kafka:
    image: confluentinc/cp-kafka:7.6.0
    depends_on:
      zookeeper:
        condition: service_healthy
    environment:
      KAFKA_BROKER_ID: 1
      KAFKA_ZOOKEEPER_CONNECT: zookeeper:2181
      KAFKA_ADVERTISED_LISTENERS: PLAINTEXT://kafka:9092,PLAINTEXT_HOST://localhost:9094
      KAFKA_LISTENER_SECURITY_PROTOCOL_MAP: PLAINTEXT:PLAINTEXT,PLAINTEXT_HOST:PLAINTEXT
      KAFKA_INTER_BROKER_LISTENER_NAME: PLAINTEXT
      KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR: 1
      KAFKA_AUTO_CREATE_TOPICS_ENABLE: "true"
    ports:
      - "9094:9094"             # 暴露给本机，用于调试
    healthcheck:
      test: ["CMD-SHELL", "kafka-broker-api-versions --bootstrap-server localhost:9092"]
      interval: 10s
      timeout: 10s
      retries: 10
      start_period: 30s

  # 数据库迁移（一次性任务）
  db-migrate:
    build:
      context: .
    command: alembic upgrade head
    environment:
      DATABASE_URL: postgresql://dev:devpass@postgres:5432/mydb
    depends_on:
      postgres:
        condition: service_healthy
    restart: "no"               # 不自动重启

volumes:
  postgres_data:
  redis_data:
```

## depends_on + healthcheck：等待服务真正就绪

`depends_on` 默认只等待容器启动（`condition: service_started`），不等服务就绪。PostgreSQL 容器启动后，还需要几秒钟初始化数据库，这段时间连接会报错。

`condition: service_healthy` 让 Compose 等到 healthcheck 通过后再启动依赖服务。

几个关键 healthcheck 配置：

**PostgreSQL：用 pg_isready**
```yaml
healthcheck:
  test: ["CMD-SHELL", "pg_isready -U dev -d mydb"]
  interval: 5s
  timeout: 5s
  retries: 10
  start_period: 10s    # 重要！前 10 秒不检查，给初始化时间
```

`start_period` 非常重要。没有它，Postgres 在初始化期间（创建数据库、运行 init.sql）会连续失败多次 healthcheck，达到 `retries` 上限后被标记为 unhealthy，导致依赖它的服务也无法启动。

**Redis：redis-cli ping**
```yaml
healthcheck:
  test: ["CMD", "redis-cli", "ping"]
```
Redis 启动很快，这个检查通常第一次就能过。

**HTTP 服务：curl endpoint**
```yaml
healthcheck:
  test: ["CMD-SHELL", "curl -f http://localhost:8000/health || exit 1"]
  interval: 10s
  timeout: 5s
  retries: 3
```

## Volume 挂载热更新

代码热更新的关键是把本地代码目录挂载到容器内：

```yaml
volumes:
  - ./app:/app/app     # 本地 ./app 目录挂载到容器 /app/app
```

配合开发服务器的 `--reload` 参数（uvicorn、nodemon 等），文件变化会自动触发重载：

```yaml
command: uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

**挂载排除**：`node_modules`、Python 的 `.venv` 等大型依赖目录不要挂载，否则本机的版本会覆盖容器内的版本（而且 Mac 上 bind mount 大量文件性能很差）。

```yaml
services:
  frontend:
    volumes:
      - ./src:/app/src          # 只挂代码
      - /app/node_modules       # 匿名 volume，防止本机 node_modules 覆盖容器内的
```

`- /app/node_modules` 这个写法创建一个匿名 volume 挂载到 `/app/node_modules`，优先级高于外层目录挂载，有效屏蔽本机的 `node_modules`。

## Compose Watch：更现代的热更新方案

Docker Compose v2.22+ 引入了 `watch` 模式，比 bind mount 更智能。它监听文件变化，根据规则决定执行同步还是重建：

```yaml
services:
  api:
    build:
      context: .
    develop:
      watch:
        # 代码变更：同步到容器（不重建）
        - action: sync
          path: ./app
          target: /app/app
          ignore:
            - __pycache__/
            - "*.pyc"

        # 依赖变更：重建镜像
        - action: rebuild
          path: requirements.txt

        # 配置变更：重启服务
        - action: sync+restart
          path: ./config
          target: /app/config
```

启动 watch 模式：

```bash
docker compose watch
# 或者
docker compose up --watch
```

watch 模式的优势：
- 可以区分「同步文件」和「重建镜像」两种操作
- 不需要把整个目录都挂载进去，可以精确控制同步范围
- 在 Mac/Windows 上性能比 bind mount 好（底层用文件系统事件而非 inotify）

## 多项目共享基础设施层

实际项目里，前端和后端是两个独立的 Git 仓库，但都需要用同一个 PostgreSQL 和 Redis。为每个项目都启动一套基础设施既浪费资源又容易端口冲突。

解决方案：用 external network 让多个 Compose 项目共享同一套基础设施。

**第一步：创建基础设施层 Compose**（独立目录，比如 `~/infra/`）

```yaml
# ~/infra/compose.yaml
services:
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: dev
      POSTGRES_PASSWORD: devpass
    ports:
      - "5432:5432"
    networks:
      - shared-infra

  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
    networks:
      - shared-infra

networks:
  shared-infra:
    name: shared-infra          # 固定网络名（不加项目前缀）
```

**第二步：业务项目引用 external network**

```yaml
# ~/projects/backend/compose.yaml
services:
  api:
    build: .
    environment:
      DATABASE_URL: postgresql://dev:devpass@postgres:5432/mydb
    networks:
      - shared-infra            # 加入共享网络，可以访问 postgres/redis

networks:
  shared-infra:
    external: true              # 声明为外部网络，不自动创建
```

这样不同项目的服务可以通过服务名（`postgres`、`redis`）互相访问，基础设施只启动一份。

## 环境变量管理

Compose 支持多种方式注入环境变量，按优先级从高到低：

1. 直接在 `environment` 里写死（不推荐，会进 Git）
2. `env_file` 加载 `.env` 文件
3. Shell 环境变量
4. Compose 文件同目录的 `.env` 文件（自动加载）

推荐方案：`.env.example` 入 Git，`.env.local` 不入 Git：

```bash
# .env.example（入 Git，作为模板）
DATABASE_URL=postgresql://dev:devpass@postgres:5432/mydb
REDIS_URL=redis://redis:6379/0
SECRET_KEY=change-me-in-local

# .env.local（不入 Git，本地真实值）
SECRET_KEY=my-actual-local-secret-key-12345
OPENAI_API_KEY=sk-xxxx
```

```yaml
services:
  api:
    env_file:
      - .env.example    # 基础配置
      - .env.local      # 本地覆盖（如果存在）
```

Compose 允许 `env_file` 列表中的文件不存在（加 `required: false`）：

```yaml
    env_file:
      - path: .env.local
        required: false   # 文件不存在不报错
```

## compose.override.yaml：分离开发和生产配置

`compose.override.yaml` 是 Compose 的特性：如果存在这个文件，`docker compose up` 会自动合并它的内容。

基础文件（`compose.yaml`）写通用配置，开发专属配置放 `compose.override.yaml`：

```yaml
# compose.yaml（基础，也是生产 CI 用的版本）
services:
  api:
    image: myregistry/api:${IMAGE_TAG:-latest}
    environment:
      LOG_LEVEL: info

# compose.override.yaml（开发专属，不入 Git）
services:
  api:
    build:
      context: .            # 开发环境用本地构建替代镜像
    volumes:
      - ./app:/app/app      # 挂载代码
    environment:
      LOG_LEVEL: debug       # 覆盖日志级别
    command: uvicorn app.main:app --reload  # 覆盖启动命令
```

CI 环境用 `docker compose -f compose.yaml up` 忽略 override；本地开发直接 `docker compose up` 自动合并。

## 性能优化：Mac 专用技巧

Mac 上 Docker 的文件系统性能历来是痛点（Linux 虚拟机 + 跨 OS bind mount）。几个优化方向：

**1. 使用 VirtioFS**（Docker Desktop 4.6+）：在 Docker Desktop 设置里开启 VirtioFS，比旧的 gRPC FUSE 快 3-5 倍。

**2. 减少挂载文件数量**：只挂载实际需要热更新的目录，不要挂整个项目根目录。

**3. 排除大型目录**：
```yaml
volumes:
  - ./src:/app/src              # 只挂源码
  - /app/node_modules           # 屏蔽 node_modules
  - /app/.venv                  # 屏蔽 Python 虚拟环境
  - /app/.pytest_cache          # 屏蔽缓存
```

**4. 考虑迁移到 Compose Watch**：官方推荐的长期方向，性能更好，控制更精细。

一个运转良好的本地开发环境能节省大量「在我机器上没问题」的沟通成本。投入一天时间把 Compose 配置做好，往后每天都能省下至少 15 分钟的环境问题排查。
