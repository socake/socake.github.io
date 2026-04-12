---
title: "Nginx 运维完全指南：反向代理、负载均衡、HTTPS 与限流"
date: 2024-10-24T12:06:00+08:00
draft: false
tags: ["Nginx", "运维", "反向代理", "负载均衡", "HTTPS", "Linux"]
categories: ["Linux"]
description: "系统梳理 Nginx 核心配置结构、反向代理与负载均衡策略、HTTPS 自动证书、限流防护、性能调优要点，以及常见生产故障的排查方法论。"
summary: "Nginx 知道怎么装，但真的会用吗？本文从配置结构说起，完整覆盖反向代理、负载均衡策略、Let's Encrypt 证书、限流配置、日志分析和性能调优，附常见 502/SSL 故障排查。"
toc: true
math: false
diagram: false
keywords: ["Nginx 反向代理", "Nginx 负载均衡", "HTTPS 配置", "Nginx 限流", "Nginx 性能调优"]
params:
  reading_time: true
---

## Nginx 配置结构总览

Nginx 配置文件采用层级结构，从外到内依次是：

```
main              # 全局配置（进程、用户、日志路径）
├── events {}     # 网络连接处理
└── http {}       # HTTP 协议相关
    ├── upstream {} # 后端服务器组（负载均衡）
    └── server {}   # 虚拟主机
        └── location {} # URL 路由匹配
```

```nginx
# /etc/nginx/nginx.conf

# ---- main 块 ----
user nginx;
worker_processes auto;          # 自动匹配 CPU 核数
worker_rlimit_nofile 65535;     # Worker 进程最大文件描述符数
error_log /var/log/nginx/error.log warn;
pid /run/nginx.pid;

# ---- events 块 ----
events {
    worker_connections 10240;   # 每个 Worker 最大并发连接数
    use epoll;                  # Linux 高性能事件模型
    multi_accept on;            # 一次接受多个连接
}

# ---- http 块 ----
http {
    include       /etc/nginx/mime.types;
    default_type  application/octet-stream;

    # 日志格式（下面章节详细介绍）
    log_format main '$remote_addr - $request_time - "$request" $status $body_bytes_sent';
    access_log /var/log/nginx/access.log main;

    sendfile        on;
    tcp_nopush      on;
    tcp_nodelay     on;
    keepalive_timeout 65;
    gzip on;

    # 引入各站点配置
    include /etc/nginx/conf.d/*.conf;
}
```

配置文件分散管理：每个站点一个文件放在 `/etc/nginx/conf.d/`，避免单文件过长。

## 反向代理配置

### 基础反向代理

```nginx
# /etc/nginx/conf.d/myapp.conf
upstream myapp_backend {
    server 192.168.1.10:8080;
    server 192.168.1.11:8080;

    # 保持连接（避免每次请求都重新建 TCP 连接）
    keepalive 32;
}

server {
    listen 80;
    server_name app.example.com;

    location / {
        proxy_pass http://myapp_backend;

        # 传递真实客户端 IP
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # 传递 Host 头（后端依赖 Host 做虚拟主机路由时必须）
        proxy_set_header Host $host;

        # 超时配置
        proxy_connect_timeout 10s;
        proxy_read_timeout 60s;
        proxy_send_timeout 60s;

        # 启用长连接复用
        proxy_http_version 1.1;
        proxy_set_header Connection "";

        # 失败重试（只对幂等请求）
        proxy_next_upstream error timeout http_502 http_503;
        proxy_next_upstream_tries 2;
    }

    # 健康检查端点不记录日志（减少噪音）
    location /health {
        proxy_pass http://myapp_backend;
        access_log off;
    }
}
```

### 主动健康检查

Nginx 开源版只支持被动健康检查（请求失败后标记 server 不可用）。主动健康检查需要 Nginx Plus 或第三方模块 `nginx_upstream_check_module`：

```nginx
upstream myapp_backend {
    server 192.168.1.10:8080;
    server 192.168.1.11:8080;

    # 开源版被动检查：连续 3 次失败则标记不可用，30秒后重试
    # 这些参数加在 server 后面
}

# server 指令参数
upstream myapp_backend {
    server 192.168.1.10:8080 max_fails=3 fail_timeout=30s;
    server 192.168.1.11:8080 max_fails=3 fail_timeout=30s;
}
```

## 负载均衡策略

### 轮询（默认）

请求依次分发给每个 server，最简单：

```nginx
upstream backend {
    server 192.168.1.10:8080;
    server 192.168.1.11:8080;
    server 192.168.1.12:8080;
}
```

### 权重轮询

性能不同的机器按权重分流：

```nginx
upstream backend {
    server 192.168.1.10:8080 weight=3;  # 承担 60% 流量
    server 192.168.1.11:8080 weight=2;  # 承担 40% 流量
    server 192.168.1.12:8080 backup;    # 备用，前两台都挂了才启用
}
```

### ip_hash

同一 IP 的请求始终打到同一台 server，适合需要会话黏连的应用（不推荐，会话应该放 Redis，不应该依赖 ip_hash）：

```nginx
upstream backend {
    ip_hash;
    server 192.168.1.10:8080;
    server 192.168.1.11:8080;
}
```

### least_conn

把请求发给当前活跃连接数最少的 server，适合请求处理时间差异大的场景：

```nginx
upstream backend {
    least_conn;
    server 192.168.1.10:8080;
    server 192.168.1.11:8080;
}
```

### hash（一致性哈希）

按自定义 key（如 URL、请求参数）做一致性哈希，常用于缓存场景，同一个 key 的请求打到同一台后端：

```nginx
upstream backend {
    hash $request_uri consistent;
    server 192.168.1.10:8080;
    server 192.168.1.11:8080;
}
```

## HTTPS 配置：Let's Encrypt 证书

### 申请证书（Certbot）

```bash
# 安装 certbot
apt install certbot python3-certbot-nginx

# 申请证书并自动配置 Nginx（Nginx 必须已经监听 80 并能访问 /.well-known/）
certbot --nginx -d app.example.com -d www.example.com

# 或者 standalone 模式（临时停掉 Nginx）
certbot certonly --standalone -d app.example.com
```

证书存在 `/etc/letsencrypt/live/app.example.com/`。

### HTTPS 配置

```nginx
server {
    listen 80;
    server_name app.example.com;
    # HTTP 强制跳转 HTTPS
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name app.example.com;

    ssl_certificate     /etc/letsencrypt/live/app.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/app.example.com/privkey.pem;

    # Mozilla 推荐的安全配置
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384;
    ssl_prefer_server_ciphers off;

    # HSTS（一年内强制 HTTPS）
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;

    # OCSP Stapling（减少证书验证延迟）
    ssl_stapling on;
    ssl_stapling_verify on;
    ssl_trusted_certificate /etc/letsencrypt/live/app.example.com/chain.pem;

    # Session 复用（减少 TLS 握手开销）
    ssl_session_cache shared:SSL:10m;
    ssl_session_timeout 10m;

    location / {
        proxy_pass http://myapp_backend;
        # ... 其他 proxy 配置
    }
}
```

### 自动续期

Let's Encrypt 证书 90 天过期，certbot 安装后会自动创建 systemd timer：

```bash
# 检查自动续期 timer
systemctl status certbot.timer

# 手动测试续期（不会真正续期，只是演练）
certbot renew --dry-run

# 续期后重载 Nginx 的 hook
# 在 /etc/letsencrypt/renewal-hooks/post/ 创建脚本
cat > /etc/letsencrypt/renewal-hooks/post/reload-nginx.sh << 'EOF'
#!/bin/bash
nginx -s reload
EOF
chmod +x /etc/letsencrypt/renewal-hooks/post/reload-nginx.sh
```

## 限流：防止滥用和 DDoS

### 请求频率限制（漏桶算法）

`limit_req_zone` 定义限流规则，`limit_req` 应用到 location：

```nginx
http {
    # 按 IP 限流：每秒最多 10 个请求，用 10MB 内存存状态（约 16万 IP）
    limit_req_zone $binary_remote_addr zone=api_limit:10m rate=10r/s;

    # 按 URL + IP 组合限流
    limit_req_zone $binary_remote_addr$request_uri zone=url_limit:20m rate=5r/s;

    server {
        location /api/ {
            # burst=20：允许突发 20 个请求（漏桶容量）
            # nodelay：突发请求不排队延迟，直接处理（超出 burst 才 503）
            limit_req zone=api_limit burst=20 nodelay;
            limit_req_status 429;

            proxy_pass http://myapp_backend;
        }

        location /api/login {
            # 登录接口更严格：每分钟 5 次
            limit_req_zone $binary_remote_addr zone=login_limit:5m rate=5r/m;
            limit_req zone=login_limit burst=3 nodelay;
            limit_req_status 429;

            proxy_pass http://myapp_backend;
        }
    }
}
```

### 并发连接限制

防止单 IP 建立大量连接：

```nginx
http {
    # 每 IP 最多 10 个并发连接
    limit_conn_zone $binary_remote_addr zone=conn_limit:10m;

    server {
        location / {
            limit_conn conn_limit 10;
            limit_conn_status 503;

            proxy_pass http://myapp_backend;
        }
    }
}
```

## 日志格式与分析

### 自定义日志格式

```nginx
log_format detailed escape=json
    '{'
    '"time":"$time_iso8601",'
    '"remote_addr":"$remote_addr",'
    '"method":"$request_method",'
    '"uri":"$request_uri",'
    '"status":$status,'
    '"body_bytes":$body_bytes_sent,'
    '"request_time":$request_time,'
    '"upstream_time":"$upstream_response_time",'
    '"upstream_addr":"$upstream_addr",'
    '"http_referer":"$http_referer",'
    '"http_user_agent":"$http_user_agent",'
    '"http_x_forwarded_for":"$http_x_forwarded_for"'
    '}';

access_log /var/log/nginx/access.log detailed;
```

### awk 统计分析

```bash
# Top 10 访问 IP
awk '{print $1}' /var/log/nginx/access.log | sort | uniq -c | sort -rn | head -10

# Top 10 访问 URL
awk '{print $7}' /var/log/nginx/access.log | sort | uniq -c | sort -rn | head -10

# 统计各 HTTP 状态码数量
awk '{print $9}' /var/log/nginx/access.log | sort | uniq -c | sort -rn

# 找出响应时间超过 3 秒的请求（第 10 列是 request_time，取决于日志格式）
awk '$10 > 3 {print $0}' /var/log/nginx/access.log | head -20

# 统计某段时间内的 QPS
awk '{print substr($4, 2, 17)}' /var/log/nginx/access.log | uniq -c
```

## 性能调优要点

### worker 进程与连接数

```nginx
# worker 数等于 CPU 核数（auto 自动设置）
worker_processes auto;
worker_cpu_affinity auto;

# 每个 worker 的最大连接数（总并发 = worker_processes * worker_connections）
events {
    worker_connections 10240;
}

# 同步修改系统文件描述符限制
worker_rlimit_nofile 65535;
```

```bash
# 系统层面也要放开
echo "nginx soft nofile 65535" >> /etc/security/limits.conf
echo "nginx hard nofile 65535" >> /etc/security/limits.conf
```

### Keepalive 优化

```nginx
http {
    # 客户端 keepalive
    keepalive_timeout 65;
    keepalive_requests 1000;  # 单个 keepalive 连接最多处理 1000 个请求

    upstream backend {
        server 192.168.1.10:8080;
        keepalive 32;  # 与后端保持 32 个长连接
    }
}
```

### Gzip 压缩

```nginx
http {
    gzip on;
    gzip_min_length 1024;  # 小于 1KB 不压缩
    gzip_comp_level 6;     # 压缩级别 1-9，6 是性能和压缩率的平衡点
    gzip_types
        text/plain
        text/css
        text/javascript
        application/javascript
        application/json
        application/xml
        image/svg+xml;
    gzip_vary on;  # 添加 Vary: Accept-Encoding 响应头
}
```

### Sendfile 与静态文件

```nginx
http {
    sendfile on;        # 零拷贝传输文件（内核直接发送，不经用户空间）
    tcp_nopush on;      # 配合 sendfile，合并多个 TCP 包
    tcp_nodelay on;     # 禁用 Nagle 算法，减少小包延迟

    # 静态文件缓存
    open_file_cache max=1000 inactive=60s;  # 缓存 1000 个文件描述符
    open_file_cache_valid 80s;
    open_file_cache_min_uses 2;
}
```

## 常见故障排查

### upstream 502 Bad Gateway

502 表示 Nginx 无法从后端获取有效响应，排查顺序：

```bash
# 1. 检查 Nginx error log
tail -f /var/log/nginx/error.log

# 常见错误信息：
# "connect() failed (111: Connection refused)" → 后端服务没启动或端口错误
# "upstream timed out (110: Connection timed out)" → 后端响应太慢，调大 proxy_read_timeout
# "no live upstreams while connecting to upstream" → 所有 upstream server 都标记为不可用

# 2. 手动测试后端是否可达
curl -v http://192.168.1.10:8080/health

# 3. 检查 upstream server 状态（如果用了 nginx_upstream_check_module）
curl http://localhost/nginx_status

# 4. 检查 SELinux（某些 Linux 发行版默认开启，会阻断 Nginx 连接后端）
setsebool -P httpd_can_network_connect 1
```

### SSL 握手失败

```bash
# 用 openssl 测试 SSL 握手
openssl s_client -connect app.example.com:443 -tls1_2

# 检查证书有效期
echo | openssl s_client -connect app.example.com:443 2>/dev/null | openssl x509 -noout -dates

# 检查证书链是否完整（fullchain.pem 而不是 cert.pem）
openssl s_client -connect app.example.com:443 -showcerts

# 常见原因：
# 1. 用了 cert.pem 而不是 fullchain.pem（缺中间证书）
# 2. ssl_protocols 没有包含客户端支持的版本
# 3. 证书已过期
```

### location 匹配优先级

Nginx location 匹配有固定优先级，从高到低：

1. `location = /exact` — 精确匹配（最高优先级）
2. `location ^~ /prefix` — 前缀匹配，匹配后不再检查正则
3. `location ~ /regex` — 正则匹配（区分大小写）
4. `location ~* /regex` — 正则匹配（不区分大小写）
5. `location /prefix` — 普通前缀匹配（最低优先级）

```nginx
# 示例：请求 /api/user 会匹配哪个？
location = /api { }          # 不匹配（精确）
location ^~ /api/ { }        # 匹配！前缀匹配且加了 ^~，停止正则检查
location ~ \.php$ { }        # 不会检查（被 ^~ 中断）
location / { }               # 不会检查
```

调试 location 匹配可以加临时日志：

```nginx
location /api/ {
    add_header X-Location "api-block" always;
    proxy_pass http://backend;
}
```

请求后检查响应头里的 `X-Location`，确认走了哪个 location。

```bash
# 重载配置（平滑重启，不中断现有连接）
nginx -t && nginx -s reload

# 完全重启
systemctl restart nginx
```
