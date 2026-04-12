---
title: "k6 压测实战：从脚本编写到性能分析"
date: 2026-04-12T17:00:00+08:00
draft: false
tags: ["k6", "load-testing", "performance", "prometheus", "grafana"]
categories: ["运维工具"]
description: "从 k6 脚本编写、VU 配置、自定义指标到与 Prometheus/Grafana 集成的完整压测实战指南。"
summary: "压测不是跑一个脚本看能不能撑住，而是通过有设计的负载模型暴露系统瓶颈。本文记录了我用 k6 做生产级性能测试的完整实践：脚本设计、阈值配置、与 Grafana 集成，以及几个典型性能问题的定位过程。"
toc: true
math: false
diagram: false
keywords: ["k6", "压测", "性能测试", "load testing", "prometheus", "grafana"]
params:
  reading_time: true
---

## 为什么选 k6

在用过 JMeter、Locust 和 k6 之后，我基本上把日常压测工作全切到 k6 了。原因很简单：

- **脚本即代码**：JavaScript 编写，支持模块化，可以像对待业务代码一样 Code Review
- **资源消耗低**：单机可以模拟数千 VU，不需要分布式集群就能做中等规模压测
- **CLI 友好**：一行命令跑测试，天然适合 CI/CD 集成
- **Prometheus 集成开箱即用**：指标直接推到 Prometheus，Grafana 实时可视化

JMeter 的 XML 配置维护起来太痛苦，Locust 需要搭 Python 环境，k6 是目前体验最顺滑的。

---

## 安装

```bash
# macOS
brew install k6

# Linux (Debian/Ubuntu)
sudo gpg -k
sudo gpg --no-default-keyring --keyring /usr/share/keyrings/k6-archive-keyring.gpg \
     --keyserver hkp://keyserver.ubuntu.com:80 --recv-keys C5AD17C747E3415A3642D57D77C6C491D6AC1D69
echo "deb [signed-by=/usr/share/keyrings/k6-archive-keyring.gpg] https://dl.k6.io/deb stable main" \
     | sudo tee /etc/apt/sources.list.d/k6.list
sudo apt-get update && sudo apt-get install k6

# 或直接用 Docker
docker run --rm -i grafana/k6 run - < script.js
```

---

## 脚本结构

一个 k6 脚本有固定的生命周期：

```javascript
// script.js

// 1. 初始化阶段（每个 VU 只执行一次，不计入负载统计）
import http from 'k6/http';
import { check, sleep } from 'k6';
import { Rate, Counter, Trend } from 'k6/metrics';

// 自定义指标
const errorRate = new Rate('error_rate');
const apiLatency = new Trend('api_latency', true);  // true = 单位毫秒

// 2. 场景配置
export const options = {
  stages: [
    { duration: '2m', target: 50 },   // 2分钟内从0爬升到50 VU
    { duration: '5m', target: 50 },   // 维持50 VU 5分钟
    { duration: '2m', target: 200 },  // 2分钟内爬升到200 VU（压力测试）
    { duration: '5m', target: 200 },  // 维持200 VU 5分钟
    { duration: '2m', target: 0 },    // 2分钟内归零
  ],
  thresholds: {
    // 成功率必须 > 99%
    'http_req_failed': ['rate<0.01'],
    // P95 延迟必须 < 500ms
    'http_req_duration': ['p(95)<500', 'p(99)<1000'],
    // 自定义指标阈值
    'error_rate': ['rate<0.01'],
  },
};

// 3. 主函数（每个 VU 反复执行）
export default function() {
  const payload = JSON.stringify({
    user_id: Math.floor(Math.random() * 10000),
    action: 'query',
  });

  const params = {
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${__ENV.API_TOKEN}`,
    },
    timeout: '10s',
  };

  const start = Date.now();
  const res = http.post('https://api.example.com/v1/query', payload, params);
  apiLatency.add(Date.now() - start);

  const success = check(res, {
    '状态码 200': (r) => r.status === 200,
    '响应有 data 字段': (r) => r.json('data') !== undefined,
    '延迟 < 500ms': (r) => r.timings.duration < 500,
  });

  errorRate.add(!success);

  // 模拟用户思考时间（1-3秒随机）
  sleep(Math.random() * 2 + 1);
}

// 4. 收尾函数（整个测试结束后执行一次）
export function teardown(data) {
  console.log('压测结束，清理测试数据...');
}
```

---

## VU 模型与 Stages

### VU（Virtual User）

k6 的 VU 是协程，不是线程，资源消耗极低。每个 VU 独立执行脚本，有自己的 HTTP 连接、Cookie Jar、变量。

### Stages vs Scenarios

`stages` 是最简单的配置方式，适合单一负载模型。`scenarios` 更灵活，可以并行运行多种负载模型：

```javascript
export const options = {
  scenarios: {
    // 场景1：稳定负载（模拟正常流量）
    steady_load: {
      executor: 'constant-vus',
      vus: 50,
      duration: '10m',
    },
    // 场景2：突发流量（模拟营销活动）
    spike_load: {
      executor: 'ramping-vus',
      startVUs: 0,
      stages: [
        { duration: '30s', target: 500 },
        { duration: '1m', target: 500 },
        { duration: '30s', target: 0 },
      ],
      startTime: '5m',  // 5分钟后开始
    },
    // 场景3：固定 RPS（Requests Per Second）
    constant_rps: {
      executor: 'constant-arrival-rate',
      rate: 100,         // 100 req/s
      timeUnit: '1s',
      duration: '10m',
      preAllocatedVUs: 50,
      maxVUs: 200,
    },
  },
};
```

`constant-arrival-rate` 特别适合测试实际 RPS 场景，因为 VU 模型下如果响应慢，实际 RPS 会下降；而 arrival rate 模式会保持 RPS 稳定（通过动态增加 VU 来补偿）。

---

## HTTP 场景

### 处理登录态

```javascript
import http from 'k6/http';
import { check } from 'k6';

// setup 阶段获取 token，传给所有 VU
export function setup() {
  const loginRes = http.post('https://api.example.com/auth/login', JSON.stringify({
    username: 'test-user',
    password: __ENV.TEST_PASSWORD,
  }), { headers: { 'Content-Type': 'application/json' } });

  check(loginRes, { 'login success': (r) => r.status === 200 });
  return { token: loginRes.json('access_token') };
}

export default function(data) {
  const headers = {
    'Authorization': `Bearer ${data.token}`,
    'Content-Type': 'application/json',
  };

  // 模拟用户行为序列
  // Step 1: 获取列表
  const listRes = http.get('https://api.example.com/v1/items', { headers });
  check(listRes, { 'list ok': (r) => r.status === 200 });

  // Step 2: 查看详情
  const items = listRes.json('items');
  if (items && items.length > 0) {
    const itemId = items[Math.floor(Math.random() * items.length)].id;
    const detailRes = http.get(`https://api.example.com/v1/items/${itemId}`, { headers });
    check(detailRes, { 'detail ok': (r) => r.status === 200 });
  }
}
```

### 批量请求（Batch）

```javascript
// 并发发出多个请求
const responses = http.batch([
  ['GET', 'https://api.example.com/v1/users', null, { headers }],
  ['GET', 'https://api.example.com/v1/products', null, { headers }],
  ['GET', 'https://api.example.com/v1/orders', null, { headers }],
]);

for (const res of responses) {
  check(res, { 'ok': (r) => r.status === 200 });
}
```

---

## gRPC 场景

```javascript
import grpc from 'k6/net/grpc';
import { check } from 'k6';

const client = new grpc.Client();
client.load(['./proto'], 'service.proto');

export default function() {
  client.connect('grpc.example.com:50051', { plaintext: false });

  const response = client.invoke('example.Service/GetData', {
    id: Math.floor(Math.random() * 1000),
  });

  check(response, {
    'status OK': (r) => r.status === grpc.StatusOK,
    'data not null': (r) => r.message.data !== null,
  });

  client.close();
}
```

---

## 自定义指标

```javascript
import { Rate, Counter, Trend, Gauge } from 'k6/metrics';

// Rate：成功/失败比率
const successRate = new Rate('success_rate');

// Counter：累计次数
const cacheHits = new Counter('cache_hits');

// Trend：延迟分布（支持 avg/min/max/p50/p90/p95/p99）
const dbQueryTime = new Trend('db_query_time_ms', true);

// Gauge：当前值（如队列长度）
const queueDepth = new Gauge('queue_depth');

export default function() {
  const res = http.get('https://api.example.com/v1/data');

  successRate.add(res.status === 200);

  // 从响应头读取自定义指标
  const fromCache = res.headers['X-Cache'] === 'HIT';
  if (fromCache) cacheHits.add(1);

  const dbTime = parseFloat(res.headers['X-DB-Time'] || '0');
  dbQueryTime.add(dbTime);
}
```

---

## Thresholds 配置

Thresholds 决定了测试是 pass 还是 fail，是 CI 集成的关键。

```javascript
export const options = {
  thresholds: {
    // 内置指标
    'http_req_duration': [
      'p(50)<100',    // 中位数 < 100ms
      'p(95)<500',    // P95 < 500ms
      'p(99)<1000',   // P99 < 1s
    ],
    'http_req_failed': ['rate<0.01'],  // 失败率 < 1%

    // 自定义指标
    'success_rate': ['rate>0.99'],

    // 针对特定 URL 的阈值（用 Tags 过滤）
    'http_req_duration{url:https://api.example.com/v1/critical}': ['p(95)<200'],

    // 终止测试的阈值（abortOnFail: 连续失败直接停止）
    'http_req_failed': [{
      threshold: 'rate<0.05',
      abortOnFail: true,
      delayAbortEval: '30s',  // 持续30秒才终止
    }],
  },
};
```

运行后，如果任何 Threshold 不满足，k6 返回非零退出码，CI 流水线会标记为失败：

```bash
k6 run script.js
# Threshold check failed:
# ✗ http_req_duration (p(95)<500): p(95)=823ms
# FAIL
echo $?  # 99
```

---

## 与 Prometheus/Grafana 集成

### 方案一：k6 Prometheus Remote Write（推荐）

k6 支持直接将指标推送到 Prometheus Remote Write 接口：

```bash
K6_PROMETHEUS_RW_SERVER_URL=http://prometheus.example.com:9090/api/v1/write \
K6_PROMETHEUS_RW_TREND_AS_NATIVE_HISTOGRAM=true \
k6 run --out experimental-prometheus-rw script.js
```

### 方案二：InfluxDB + Grafana

```bash
# 启动 InfluxDB（Docker）
docker run -d -p 8086:8086 \
  -e INFLUXDB_DB=k6 \
  influxdb:1.8

# 运行 k6 并推送到 InfluxDB
k6 run --out influxdb=http://localhost:8086/k6 script.js
```

Grafana Dashboard 直接导入官方模板 ID `2587`（k6 Load Testing Results）。

### 实时监控面板

测试运行时，你可以在 Grafana 实时看到：

- **VU 数量趋势**（验证 ramp-up 是否按预期）
- **RPS 和错误率**（发现压力下的错误尖峰）
- **P50/P95/P99 延迟分布**（找出慢请求的 tail latency）
- **自定义指标**（db_query_time、cache_hit_rate 等）

---

## 典型性能问题定位

### 问题1：P99 高但平均值正常

现象：P50=50ms，P99=2000ms，两者差距极大。

```
P50:  50ms  ████
P99: 2000ms ████████████████████████████████████████
```

定位方法：

```bash
# 在 k6 中按 URL 拆分 tag，找出慢 URL
export const options = {
  tags: { run_id: 'debug-2026-04-12' },
};

// 在脚本里给每个请求打 tag
const res = http.get(url, { tags: { endpoint: 'user-detail' } });
```

再到 Grafana 按 `endpoint` 过滤，定位到慢的 endpoint，结合后端 APM trace 找到根因（通常是慢查询、GC Pause、锁竞争）。

### 问题2：连接建立时间异常

```
http_req_connecting......: avg=250ms  # 远超正常的几ms
```

原因通常是：
- 连接池耗尽（并发高时 TCP 三次握手积压）
- Keep-Alive 没有正确配置
- 服务端 `SOMAXCONN`/`listen backlog` 太小

k6 默认启用 Keep-Alive，如果测试中 `http_req_connecting` 持续高，说明服务端没有正确处理持久连接。

### 问题3：阶梯式延迟上升

```
0-50 VU:  P95 = 100ms
50-100 VU: P95 = 800ms  ← 断层
100+ VU:  P95 = 3000ms+
```

这种断层通常对应一个资源上限：数据库连接池大小、线程池大小、某个锁的竞争临界点。找到 50 VU 时的系统指标快照，对比 100 VU 时的变化，重点看：

```bash
# 数据库连接数
SHOW STATUS LIKE 'Threads_connected';

# 连接等待
SHOW STATUS LIKE 'Threads_running';

# Go 应用 goroutine 数
# 通过 /debug/pprof/goroutine 端点查看
```

---

## CI 集成

```yaml
# .github/workflows/perf-test.yml
name: Performance Test

on:
  schedule:
    - cron: '0 2 * * *'    # 每天凌晨2点跑
  workflow_dispatch:

jobs:
  k6-load-test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Run k6 load test
        uses: grafana/k6-action@v0.3.1
        with:
          filename: tests/performance/api-load-test.js
          flags: --out json=results.json
        env:
          K6_PROMETHEUS_RW_SERVER_URL: ${{ secrets.PROMETHEUS_URL }}
          API_TOKEN: ${{ secrets.TEST_API_TOKEN }}

      - name: Upload results
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: k6-results
          path: results.json
```

压测是一个需要长期坚持的实践——不是发布前临时跑一次，而是作为 CI/CD 的常规门控。每次发布后指标对比，才能及早发现性能回退。
