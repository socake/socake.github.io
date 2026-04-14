---
title: "SBOM 生成与 Dependency-Track 漏洞管理实战"
date: 2025-10-24T10:00:00+08:00
draft: false
tags: ["SBOM", "Dependency-Track", "CycloneDX", "供应链安全", "漏洞管理"]
categories: ["DevSecOps"]
description: "一份基于生产环境的 SBOM 实战指南：讲清楚 CycloneDX 与 SPDX 的格式差异、Syft/cdxgen/Trivy 三款主流生成器的对比，部署 Dependency-Track 4.12 做持续漏洞监测，通过策略违规自动化处置 CVE，并分享 SBOM 消费链路上的真实踩坑。"
summary: "一份基于生产环境的 SBOM 实战指南：讲清楚 CycloneDX 与 SPDX 的格式差异、Syft/cdxgen/Trivy 三款主流生成器的对比，部署 Dependency-Track 4.12 做持续漏洞监测，通过策略违规自动化处置 CVE，并分享 SBOM 消费链路上的真实踩坑。"
toc: true
math: false
diagram: false
keywords: ["SBOM", "Dependency-Track", "CycloneDX", "供应链安全"]
params:
  reading_time: true
---

## SBOM 到底解决什么问题

问个具体问题：如果明天又爆一个 Log4Shell 级别的漏洞，你能在 10 分钟内告诉老板"我们有多少个服务受影响、部署在哪些集群、哪个版本有风险"吗？

我接触过的 99% 团队的答案是"不能"。你能打开 Jira 开个事故单，然后派人去每个仓库里翻 `pom.xml`、`package.json`、`go.mod`、`requirements.txt`，花几个小时甚至几天才能拼出一份不完整的清单。Log4Shell 的时候整个业界就这样手忙脚乱了一周，事后我们才意识到：**你根本不知道自己的软件里有什么东西**。

SBOM（Software Bill of Materials，软件物料清单）就是为了回答这个问题而生的。一份 SBOM 完整列出了一个制品（镜像、二进制、源码包）里所有的依赖组件——直接依赖和传递依赖都有——以及每个组件的名字、版本、license、源地址、哈希。有了 SBOM，上面那个 Log4Shell 问题可以变成一次数据库查询：`SELECT component FROM sbom WHERE name='log4j-core' AND version BETWEEN '2.0' AND '2.14.1'`。

这篇文章讲 SBOM 从生成到消费的完整链路，基于 2025 年主流工具：**CycloneDX 1.6**、**Syft 1.14+**、**cdxgen 11+**、**Dependency-Track 4.12+**。

## 一、SBOM 格式之争：CycloneDX vs SPDX

开局先讲清楚这件事，选错格式会让你整个供应链后续都难受。

### 1.1 两大格式的出身

**SPDX**（Software Package Data Exchange）是 Linux 基金会主导的格式，2010 年就开始做了，最早为了解决开源 license 合规问题。它是 ISO/IEC 5962:2021 国际标准。SPDX 的优势是法律合规和审计视角做得好，劣势是 schema 比较重，对"漏洞管理"这类使用场景支持有限。

**CycloneDX** 是 OWASP 主导的格式，2017 年才开始做，专门为**安全与供应链风险**设计。它的 schema 更轻量，原生支持 VEX（Vulnerability Exploitability eXchange）、依赖图、服务组件、硬件物料、SBOM 签名等扩展。目前是 CNCF 以及绝大多数安全工具的首选。

### 1.2 该选哪个？

**我的建议非常明确：CycloneDX**。三个理由：

1. **工具生态完整**：Syft、cdxgen、Trivy、Snyk、Mend、JFrog Xray 都默认输出 CycloneDX。SPDX 的生成工具生态小得多。
2. **漏洞管理更顺**：Dependency-Track 已经在 4.x 里**移除了 SPDX 的支持**，明确只接受 CycloneDX。大部分安全平台类似。
3. **VEX 和签名支持**：CycloneDX 内置 VEX 和 BOM 签名，SPDX 需要扩展。

SPDX 的生存空间主要是"合规和 license 审计"。如果你公司有专门的 license 合规团队、要对接 FOSSology 或者政府采购的 license disclosure，那 SPDX 可能还是必需品。大部分企业**只需要 CycloneDX**。

**如果需要 SPDX 输出**，可以用 CycloneDX CLI 做转换：

```bash
cyclonedx-cli convert --input-file sbom.cdx.json \
                      --output-file sbom.spdx.json \
                      --output-format spdxjson
```

### 1.3 CycloneDX 1.6 结构速览

一份典型的 CycloneDX JSON 长这样：

```json
{
  "bomFormat": "CycloneDX",
  "specVersion": "1.6",
  "serialNumber": "urn:uuid:3e671687-395b-41f5-a30f-a58921a69b79",
  "version": 1,
  "metadata": {
    "timestamp": "2025-10-20T08:00:00Z",
    "tools": {
      "components": [
        {"type": "application", "name": "syft", "version": "1.14.2"}
      ]
    },
    "component": {
      "type": "container",
      "name": "ghcr.io/myorg/payment-service",
      "version": "sha256:abcdef...",
      "purl": "pkg:oci/payment-service@sha256:abcdef..."
    }
  },
  "components": [
    {
      "type": "library",
      "bom-ref": "pkg:maven/org.apache.logging.log4j/log4j-core@2.17.1",
      "group": "org.apache.logging.log4j",
      "name": "log4j-core",
      "version": "2.17.1",
      "purl": "pkg:maven/org.apache.logging.log4j/log4j-core@2.17.1",
      "licenses": [{"license": {"id": "Apache-2.0"}}],
      "hashes": [{"alg": "SHA-256", "content": "xxxxx..."}]
    }
  ],
  "dependencies": [
    {
      "ref": "pkg:maven/com.example/payment-app@1.0.0",
      "dependsOn": [
        "pkg:maven/org.apache.logging.log4j/log4j-core@2.17.1"
      ]
    }
  ]
}
```

**几个关键字段**：

- `purl`（Package URL）：跨生态通用的组件标识符，格式 `pkg:<type>/<namespace>/<name>@<version>`。这是漏洞关联的核心，所有安全数据库（NVD、GHSA、OSV）都在用 purl。
- `bom-ref`：文档内的引用 ID，dependency graph 用它建立边。
- `dependencies`：显式依赖图。没有它的 SBOM 只是"平面清单"，无法做传递影响分析。

**提醒**：不是所有生成器都输出完整的 `dependencies` 数组。Syft 对某些生态（比如 Python wheel）只输出 flat list，导致你没办法判断"我是直接用的还是传递依赖的"。生产里选工具要验证这一点。

## 二、SBOM 生成器对比：Syft、cdxgen、Trivy

主流的三款我都用过，下面是实战对比：

### 2.1 Syft

**Anchore 出品**，和 Grype（漏洞扫描）配套。生成速度最快，镜像扫描支持最全。

```bash
syft ghcr.io/myorg/app@sha256:abc... \
     --output cyclonedx-json=sbom.cdx.json \
     --source-name payment-service \
     --source-version v1.2.3
```

优点：
- 镜像分析强，能识别 APT/APK/RPM/Python/Node/Go/Java/Ruby/Rust 等几乎所有主流生态
- 速度快，内存占用低
- 内置 attestation 支持，可以直接挂到镜像
- 可插拔的 cataloger，能定制特定生态的扫描逻辑

缺点：
- **依赖图不完整**：对 Java、Node 的传递依赖关系识别一般，对 Python 尤其弱
- Go 二进制的 license 信息常常缺失

Syft 最适合的场景是**镜像 SBOM**。如果你只从最终镜像生成 SBOM，Syft 是首选。

### 2.2 cdxgen

**CycloneDX 官方**工具（AppThreat/cdxgen），用 JavaScript 写的。它的定位是"源代码级 SBOM"，直接分析 build 配置文件（`pom.xml`、`package.json`、`go.mod` 等），能生成非常精确的依赖图。

```bash
cdxgen -t java -o sbom.cdx.json ./my-project
cdxgen -t docker -o sbom.cdx.json ghcr.io/myorg/app:latest
```

优点：
- **依赖图最完整**：原生 parse 构建文件，能识别 scope（compile/runtime/test）
- 支持 30+ 种语言和包管理器，包括一些小众生态（Dart、Swift、Elixir）
- 输出符合最新的 CycloneDX 1.6 spec

缺点：
- 速度比 Syft 慢一截（尤其是大型 Java monorepo）
- 需要 Node.js 运行时
- 对镜像扫描的深度不如 Syft

cdxgen 最适合的场景是 **CI 里对源码做 SBOM**。因为它能看到完整的构建元数据。

### 2.3 Trivy

**Aqua Security 出品**，本身是漏洞扫描器，顺带支持 SBOM 输出。好处是一次扫描拿到 SBOM + 漏洞结果。

```bash
trivy image --format cyclonedx \
            --output sbom.cdx.json \
            ghcr.io/myorg/app:latest
```

优点：
- 一站式：扫描和 SBOM 一起出
- CI 集成文档最成熟
- 镜像 + 文件系统 + Git repo 都能扫
- 对 Kubernetes manifest 也能生成 SBOM

缺点：
- SBOM 字段不如 Syft/cdxgen 全
- 依赖图偶有缺失

Trivy 最适合的场景是**已经在用 Trivy 做漏洞扫描**，那就顺手把 SBOM 也给它干了。

### 2.4 实战组合

我的生产方案：

- **源码 SBOM**：cdxgen，在每次 CI 构建时跑一次，输出到 artifact
- **镜像 SBOM**：Syft，在镜像 push 之后、签名之前跑一次
- **两份都上传 Dependency-Track**，让它合并去重

为什么要两份？因为它们看到的东西不一样。cdxgen 能看到 dev 依赖、build 工具、license 细节；Syft 能看到最终镜像里实际存在的二进制文件和 OS 层包（APT 软件包、CA 证书等）。合起来才是完整视图。

## 三、部署 Dependency-Track

Dependency-Track 是 OWASP 旗舰项目，是目前开源圈最成熟的 SBOM 消费平台。它做几件事：

1. 接收 SBOM 上传（API / UI / 自动）
2. 把 SBOM 里的组件和漏洞数据库（NVD、GitHub Advisory、OSS Index、Snyk、VulnDB）匹配
3. 持续监测：即便你不重新上传 SBOM，新漏洞爆发时也会自动关联到你已有的组件
4. 策略引擎：违反策略（比如有高危漏洞、有禁用 license）自动触发事件
5. 报表和仪表盘

### 3.1 架构

```
┌─────────────┐      ┌─────────────┐      ┌───────────────┐
│   Frontend  │─────▶│   API Svr   │─────▶│  PostgreSQL   │
│   (React)   │      │   (Java)    │      │  (元数据)     │
└─────────────┘      └──────┬──────┘      └───────────────┘
                            │
                            │ async analysis
                            ▼
                     ┌─────────────┐      ┌───────────────┐
                     │ Vuln Analyzer│────▶│ NVD / GHSA /  │
                     │              │     │ OSV / Snyk    │
                     └─────────────┘      └───────────────┘
```

从 4.11 开始，Dependency-Track 支持"独立 frontend + apiserver + vuln-mirror" 的拆分架构，vuln-mirror 专门负责镜像漏洞数据库，减轻 apiserver 压力。

### 3.2 Helm 部署

官方 chart 在 `evryfs/dependency-track`，也可以直接写 Deployment。我倾向于直接写：

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: dependency-track-apiserver
spec:
  replicas: 2
  template:
    spec:
      containers:
        - name: apiserver
          image: dependencytrack/apiserver:4.12.2
          env:
            - name: ALPINE_DATABASE_MODE
              value: external
            - name: ALPINE_DATABASE_URL
              value: jdbc:postgresql://dt-db.prod:5432/dtrack
            - name: ALPINE_DATABASE_DRIVER
              value: org.postgresql.Driver
            - name: ALPINE_DATABASE_USERNAME
              valueFrom: { secretKeyRef: { name: dt-db, key: user } }
            - name: ALPINE_DATABASE_PASSWORD
              valueFrom: { secretKeyRef: { name: dt-db, key: password } }
            - name: EXTRA_JAVA_OPTIONS
              value: "-Xmx4g -XX:MaxRAMPercentage=75"
            - name: ALPINE_METRICS_ENABLED
              value: "true"
            - name: ALPINE_OIDC_ENABLED
              value: "true"
            - name: ALPINE_OIDC_ISSUER
              value: "https://keycloak.example.com/realms/corp"
            - name: ALPINE_OIDC_CLIENT_ID
              value: "dependency-track"
            - name: ALPINE_OIDC_USERNAME_CLAIM
              value: "preferred_username"
            - name: ALPINE_OIDC_TEAMS_CLAIM
              value: "groups"
          resources:
            requests: { cpu: 1, memory: 4Gi }
            limits: { cpu: 4, memory: 8Gi }
          volumeMounts:
            - name: data
              mountPath: /data
      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: dt-apiserver-data
```

**关键配置点**：

1. **数据库必须是 PostgreSQL**：4.8 之后推荐 PostgreSQL，老的 H2 模式只适合 demo。一定要外部 PG 并做备份。
2. **内存要给足**：JVM 堆至少 4GB，大规模部署 8~16GB。Dependency-Track 在分析时会把整个组件图加载到内存。
3. **OIDC 接入 SSO**：不要用 local user，必须走企业 SSO（Keycloak/Okta/Google）。
4. **持久化存储**：`/data` 目录存的是 NVD mirror 和 CPE 字典，ReadWriteOnce 即可，不要用 emptyDir。
5. **HA 模式**：4.11 之前 Dependency-Track 不支持真正的 HA（analyzer 是单例），4.11 引入了 KafkaStream 的模式可以真正水平扩展。我们 4.12 跑的是 2 副本 apiserver + 独立 vuln-mirror。

### 3.3 漏洞数据源配置

Dependency-Track 支持多个漏洞源，**越多越好**：

```
System > Vulnerability Sources
├── NVD (官方, 免费, 延迟 1~2 天)
├── GitHub Advisory (免费, 需 token, 覆盖 JS/Python/Ruby/Go)
├── OSS Index (Sonatype, 免费)
├── Snyk (商业, 商业漏洞数据最全)
├── VulnDB (商业, 硬件+软件, 最全)
└── OSV (Google, 免费, 覆盖 OSS 生态)
```

我的建议：**免费源全开 + GitHub Advisory 必开**。免费源里 GHSA 是最重要的，因为它对开源生态的覆盖远超 NVD（NVD 的漏洞录入延迟常常是数周）。预算够的话加 Snyk。

### 3.4 自动镜像漏洞数据库刷新

默认 Dependency-Track 每天刷新一次 NVD/GHSA 等数据库。国内网络访问 NVD 不稳，可以：

1. 把数据库镜像到内部 S3，配置 `ALPINE_MIRROR_NVD_ENABLED=true` + 内部 URL
2. 用 `vuln-mirror` 独立组件（4.11+）
3. 直接挂外网代理（我们早期的做法）

## 四、CI 流水线对接

### 4.1 上传 SBOM

Dependency-Track 提供 REST API，典型调用：

```bash
curl -X POST https://dtrack.example.com/api/v1/bom \
  -H "X-Api-Key: $DT_API_KEY" \
  -H "Content-Type: multipart/form-data" \
  -F "autoCreate=true" \
  -F "projectName=payment-service" \
  -F "projectVersion=v1.2.3" \
  -F "parentName=payments-platform" \
  -F "bom=@sbom.cdx.json"
```

**字段说明**：

- `autoCreate=true`：项目不存在则自动创建，适合 CI
- `projectName` / `projectVersion`：精确到版本，每次构建都是一个新 version
- `parentName`：让项目形成层级结构，比如 `payments-platform > payment-service`

**API key 管理**：到 `Administration > Access Management > Teams` 创建一个 `automation` team，赋权 `BOM_UPLOAD`、`PROJECT_CREATION_UPLOAD`、`VIEW_PORTFOLIO`，然后生成 API key 作为 CI secret。

### 4.2 GitHub Actions 集成

```yaml
- name: Generate SBOM
  run: |
    syft ghcr.io/${{ github.repository }}@${{ steps.build.outputs.digest }} \
         -o cyclonedx-json=sbom.cdx.json

- name: Upload to Dependency-Track
  uses: DependencyTrack/gh-upload-sbom@v3
  with:
    serverHostname: dtrack.example.com
    apiKey: ${{ secrets.DT_API_KEY }}
    projectName: ${{ github.repository }}
    projectVersion: ${{ github.sha }}
    autoCreate: true
    bomFilename: sbom.cdx.json

- name: Fail on policy violation
  run: |
    curl -s -H "X-Api-Key: ${{ secrets.DT_API_KEY }}" \
      "https://dtrack.example.com/api/v1/violation/project/$PROJECT_UUID" \
      | jq -e '. | length == 0'
```

最后一步是 "**策略违反则失败 CI**"，这是把 Dependency-Track 作为质量门禁的关键。但要注意：Dependency-Track 分析是**异步**的，上传 SBOM 后需要等几秒到几十秒，才能查策略违规。官方 action 已经支持 `--waitForAnalysis` 参数处理这个等待。

### 4.3 和签名流水线组合

再回顾一下完整的流水线：

```
build ──▶ syft/cdxgen SBOM ──┬──▶ Dependency-Track (记录 + 持续监控)
                             │
                             └──▶ cosign attest (挂到镜像作为 attestation)
                                        │
                                        ▼
                                 部署时 verify
```

**关键原则**：Dependency-Track 是"知识库"，签名 attestation 是"制品封装"。两者都要做。

- DT 里的 SBOM 用于做**组合查询**（跨项目、跨时间、按漏洞筛选）
- attestation 里的 SBOM 用于**现场验证**（部署时不依赖外部服务）

## 五、策略引擎：把漏洞响应自动化

Dependency-Track 的策略引擎允许你定义"什么情况下违规"。比如：

```
Policy: No Critical Vulnerabilities in Production
  - Condition: Severity = Critical
  - Action: Fail
  - Applies to: Projects tagged "production"
```

```
Policy: No GPL License
  - Condition: License = GPL-3.0-only OR License = AGPL-3.0-only
  - Action: Warn
```

```
Policy: No Outdated Log4j
  - Condition: Component name = log4j-core AND version < 2.17.1
  - Action: Fail
```

策略违规会在 UI 里显眼地标出来，也可以通过 Webhook 推到 Slack/钉钉/PagerDuty。

### 5.1 Webhook 对接

```
Administration > Configuration > Notifications > Create
  Publisher: Webhook
  Scope: NEW_VULNERABILITY, POLICY_VIOLATION
  Destination: https://alertmanager.example.com/api/v1/alerts
  Template: {
    "labels": {
      "alertname": "DTrack-{{ notification.group }}",
      "severity": "{{ subject.vulnerability.severity }}",
      "component": "{{ subject.component.name }}",
      "project": "{{ subject.project.name }}"
    },
    ...
  }
```

我们生产里的规则：
- 新 Critical 漏洞 → 立即推钉钉 + Pager
- 新 High 漏洞 → 推钉钉，24h 内响应
- 新 Medium 漏洞 → 每日汇总推 Slack
- Low/Info → 只进 DT，不外推

### 5.2 VEX：已知但无需修复的漏洞

很多漏洞虽然在 SBOM 里命中，但**实际不可利用**。比如一个 Python 库有 SQL 注入漏洞，但你的代码从来没调用过那个脆弱函数。这种情况用 **VEX（Vulnerability Exploitability eXchange）** 标注。

VEX 是 CycloneDX 1.5+ 支持的状态字段，典型状态：

- `not_affected`：不受影响（代码未调用）
- `affected`：受影响（待修复）
- `fixed`：已修复
- `under_investigation`：调查中
- `false_positive`：误报

Dependency-Track 4.10+ 支持从 UI 或 API 设置 VEX 状态。一旦设了 `not_affected` + justification，该漏洞就不会再触发告警。这是**降噪的关键武器**。

**实战经验**：新系统刚接入 DT 时漏洞数会炸屏，动辄几千个。第一轮重点是把**明确可忽略**的漏洞批量 VEX 掉（比如 test 依赖的 CVE、build-only 工具的漏洞），剩下的真实威胁才能浮出水面。没有 VEX 治理的 DT 三个月就会被团队抛弃。

## 六、踩坑记录

### 6.1 `autoCreate` 创建的项目没 tag

上面 CI 示例里 `autoCreate=true` 会自动建项目，但新项目默认没有任何 tag，意味着它不会被"production"这类标签的策略覆盖。我们的解决办法是上传后立刻补 tag：

```bash
PROJECT_UUID=$(curl -s -H "X-Api-Key: $KEY" \
  "https://dtrack.example.com/api/v1/project/lookup?name=$NAME&version=$VER" \
  | jq -r .uuid)

curl -X PATCH -H "X-Api-Key: $KEY" -H "Content-Type: application/json" \
  "https://dtrack.example.com/api/v1/project/$PROJECT_UUID" \
  -d '{"tags":[{"name":"production"},{"name":"owner:payments-team"}]}'
```

### 6.2 大 monorepo 上传后分析超时

一次我们上传一个 Java monorepo 的 SBOM，里面 4800 个组件，分析跑了 20 分钟还没完。根因是 apiserver 单线程分析，默认线程池 4，大 SBOM 会排队。解决办法：

```
ALPINE_WORKER_POOL_SIZE=16
```

以及：**拆分 SBOM**。monorepo 不应该是一个项目，应该按 module 拆成多个子项目，通过 `parent-child` 关联。DT 会自动 rollup。

### 6.3 NVD 数据库和 GHSA 不同步

NVD 经常比 GHSA 慢好几天，尤其是 OSS 漏洞。我们见过的最夸张例子：GHSA 已经发布了 12 天的 CVE，NVD 还没同步。DT 默认优先信 NVD 的严重度，这会导致同一个 CVE 在你系统里先是 "UNKNOWN"，几天后才变 "CRITICAL"。

**修复**：在 DT 里开启 `GitHub Advisory` 并设置为**首选源**，`System > Analyzers > GitHub Advisory` 打开 `Priority`。

### 6.4 传递依赖图不完整

前面说过，某些生成器不输出 `dependencies` 数组。如果 SBOM 里只有 `components`，DT 会认为所有组件都是"根组件"，无法做"某漏洞是直接依赖还是 5 层传递下来"的分析。

**检查方法**：

```bash
jq '.dependencies | length' sbom.cdx.json
```

如果是 0 或很小的数字（和 components 数不匹配），说明依赖图丢了。换用 cdxgen 通常能解决。

### 6.5 私有仓库的组件识别失败

如果你的代码里引用了内部私有仓库的包（比如 `@myorg/internal-lib`），Syft 可以识别出来，但 DT 无法从 NVD/GHSA 查到它的漏洞——它不在公共数据库里。

解决办法：
1. 给内部组件打标签（通过 CycloneDX 的 `properties`），DT 策略忽略它们
2. 自建内部漏洞数据库（比如用 Nexus IQ 或者 CSAF），DT 可以通过 OSS Index 扩展接入

### 6.6 BOM 上传被拒："invalid schema"

CycloneDX spec 在 1.4→1.5→1.6 之间字段有变化，老版本 DT 不接受新 spec 的 BOM。具体规则：

- DT 4.10 支持 CycloneDX 1.4/1.5
- DT 4.11+ 支持 1.4/1.5/1.6

如果你用最新 Syft（默认 1.6 输出）+ 旧 DT，会报 schema 错误。要么升级 DT，要么显式指定输出版本：

```bash
syft ... --output "cyclonedx-json@1.5=sbom.cdx.json"
```

## 七、SBOM 的真实价值：一次 CVE 响应实战

说一个真实例子。2025 年 6 月某天，OpenSSH 被爆 `CVE-2025-XXXX`（真实 CVE 就不写了），严重度 CRITICAL。我们早上 8 点收到告警。

**传统模式下的响应**：

1. 8:00 安全组群通知
2. 8:15 组织各业务线盘点哪些服务用了 OpenSSH（一般要一天）
3. 第二天 早上收齐清单
4. 第三天 各业务排期修复
5. 一周后全部修复

**有 SBOM + DT 的响应**：

1. 8:00 DT 收到 GHSA 推送，自动分析匹配
2. 8:02 DT 推送钉钉告警：**"47 个项目受 CVE-2025-XXXX 影响，其中 12 个 tag=production，严重度 CRITICAL，受影响组件 openssh-client@8.4p1"**
3. 8:03 打开 DT UI，点开漏洞详情，看到完整项目清单，每个都能点进去看精确的镜像 digest
4. 8:10 在 GitOps 仓库里批量替换基础镜像版本，发起 PR
5. 10:30 所有生产镜像滚动更新完成
6. 11:00 DT 上所有相关项目漏洞状态变绿

**从告警到修复结束，2.5 小时**。这就是 SBOM 的价值——它把"灾难"变成了"日常维护"。

## 八、落地建议

最后几条实战建议：

**1. 先把 SBOM 存下来，哪怕不立即分析**。生成 SBOM 的成本极低，但如果你现在不存，明年爆大洞时你没有历史数据。所有 CI 流水线先加 Syft 步骤，输出到 S3/Artifactory。

**2. Dependency-Track 不要一次性全量接入**。先接 10 个核心服务，跑通策略和告警流程，再推广。一次性几百个项目会让告警过量，团队直接放弃。

**3. VEX 治理要有专人**。至少半天/周的投入做误报治理。没做 VEX 的 DT 就是"漏洞数字显示器"，不产生真实价值。

**4. 把 DT 接入 SSO，并按 team 隔离视图**。payments-team 只看自己的项目，不要让所有人看到全公司所有漏洞，否则权限审计一团糟。

**5. SBOM 是手段不是目的**。最终目的是"10 分钟响应高危漏洞"。不要被"SBOM 合规检查"这种形式化动作带偏，重点永远是可用性和响应速度。

下一篇我会写 Cilium NetworkPolicy 的生产落地，那是零信任网络在数据平面的核心实现。
