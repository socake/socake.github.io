---
title: "自动化发版实战：semantic-release、release-please、changesets 对比选型"
date: 2026-02-25T10:00:00+08:00
draft: false
tags: ["Release", "SemVer", "Changelog", "Conventional Commits", "自动化"]
categories: ["DevOps"]
description: "Conventional Commits + 自动化 release 工具是现代发版的事实标准。本文对比 semantic-release、release-please、changesets 三大主流方案的差异，讲清楚各自适合什么场景，以及 monorepo 里的最佳实践。"
summary: "手动维护 CHANGELOG.md、手动打 git tag、手动写 release notes——这些都是十年前的工作方式。现代发版应该是：每次合并 PR 时工具自动决定下一个版本号、自动生成 changelog、自动打 tag、自动发布。本文讲清楚三种方案的差异和选型。"
toc: true
math: false
diagram: true
keywords: ["semantic-release", "release-please", "changesets", "Conventional Commits", "SemVer"]
params:
  reading_time: true
---

## 发版不该是人做的工作

先看一个常见场景。你的项目从 1.2.3 到现在合并了 25 个 PR，要发新版本。你需要：

1. 决定下一个版本号：1.2.4？1.3.0？2.0.0？
2. 翻 25 个 PR 的 commit message 或 description
3. 分类归纳：哪些是 feature、哪些是 bug fix、哪些是破坏性变更
4. 写一段 release notes 到 `CHANGELOG.md`
5. `git tag v1.3.0 && git push --tags`
6. 触发 CI 打镜像 / 发 npm 包 / 上传 GitHub Release
7. 通知用户

这七步里第 1-4 步是**人的判断**，消耗 1-2 个小时，而且质量不稳定（写急了 changelog 漏东西、分类错）。第 5-7 步是**机械操作**，应该早就自动化了。

现代发版工具的核心思路是：**把第 1-4 步也从人的工作变成工具的工作，前提是 commit 遵循规范**。

### Conventional Commits

Conventional Commits 是一个轻量级 commit message 规范：

```
<type>[optional scope]: <description>

[optional body]

[optional footer(s)]
```

常见 type：

| type | 含义 | 影响版本 |
|------|------|---------|
| `feat` | 新功能 | minor (1.2.3 → 1.3.0) |
| `fix` | bug 修复 | patch (1.2.3 → 1.2.4) |
| `perf` | 性能优化 | patch |
| `refactor` | 重构 | 无 |
| `docs` | 文档 | 无 |
| `style` | 格式 | 无 |
| `test` | 测试 | 无 |
| `build` | 构建 | 无 |
| `ci` | CI 配置 | 无 |
| `chore` | 杂项 | 无 |

破坏性变更用 `!` 标记或 footer：

```
feat(api)!: remove deprecated /v1 endpoint

BREAKING CHANGE: /v1/* is removed. Use /v2/*.
```

这会触发 major 版本升级（1.2.3 → 2.0.0）。

**关键洞察**：如果全团队都用 Conventional Commits，工具就能根据 commit 历史自动算出下一个版本号和 changelog 内容，不需要人介入。

## 三大方案横向对比

当前主流的自动发版工具三个：

| 维度 | semantic-release | release-please | changesets |
|------|------------------|----------------|------------|
| 维护方 | 社区 | Google | Vercel |
| 触发方式 | 每次 push 到 main | 创建 Release PR | 每 PR 带 changeset 文件 |
| 人工介入 | 零 | 合并 Release PR | 写 changeset 文件 |
| 发版时机 | 立即 | 合 Release PR 时 | 合 Release PR 时 |
| Changelog 数据源 | commit message | commit message | changeset 文件 |
| Monorepo 支持 | 一般 | 好 | 原生 |
| 语言/生态 | Node 生态为主 | 多语言 | Node 生态 |
| 适合规模 | 小-中型项目 | 小-中-大 | monorepo |

三者的哲学差异是：

- **semantic-release**：极致自动化。相信 commit message，每次 push 就发版。
- **release-please**：半自动化。工具准备 PR，人审核内容后合并触发发版。
- **changesets**：手动驱动。每个 PR 必须带一个 "changeset" 文件说明影响，release PR 是工具生成的。

## 方案一：semantic-release

最老、最激进的自动化方案。核心理念："**如果你的 commit message 写对了，所有发版动作都不需要人**"。

### 基本工作流

```
开发者 commit: "feat: add user profile page"
  ↓
push 到 main
  ↓
GitHub Actions 触发 semantic-release
  ↓
semantic-release 分析自上次 release 以来的所有 commit
  ↓
决定：有 feat → 下一个版本是 1.3.0
  ↓
生成 CHANGELOG.md 条目
  ↓
git tag v1.3.0
  ↓
GitHub Release 发布
  ↓
npm publish
```

整个过程无人介入。

### 配置示例

`.releaserc.json`：

```json
{
  "branches": [
    "main",
    { "name": "beta", "prerelease": true },
    { "name": "alpha", "prerelease": true }
  ],
  "plugins": [
    "@semantic-release/commit-analyzer",
    "@semantic-release/release-notes-generator",
    [
      "@semantic-release/changelog",
      { "changelogFile": "CHANGELOG.md" }
    ],
    [
      "@semantic-release/npm",
      { "pkgRoot": "." }
    ],
    [
      "@semantic-release/git",
      {
        "assets": ["CHANGELOG.md", "package.json"],
        "message": "chore(release): ${nextRelease.version} [skip ci]\n\n${nextRelease.notes}"
      }
    ],
    "@semantic-release/github"
  ]
}
```

GitHub Actions：

```yaml
name: Release
on:
  push:
    branches: [main, beta]

permissions:
  contents: write
  issues: write
  pull-requests: write
  id-token: write

jobs:
  release:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0  # 必须是完整历史，不能浅 clone
      - uses: actions/setup-node@v4
        with:
          node-version: 22
          registry-url: https://registry.npmjs.org
      - run: npm ci
      - run: npm run build
      - run: npm test
      - name: Release
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          NPM_TOKEN: ${{ secrets.NPM_TOKEN }}
        run: npx semantic-release
```

这套跑下来，**任何 commit 到 main 都可能立即发版**。如果没有 feat/fix 类 commit（全是 docs/chore），semantic-release 会跳过这次 release。

### semantic-release 的优势

1. **真正零负担**：你只写代码、写 commit message，别的不用操心
2. **多分支策略**：beta、alpha、next 分支可以并行发 prerelease
3. **插件生态丰富**：GitHub、GitLab、Slack、Dockerhub、JIRA 都有插件
4. **无人值守**：适合 lib 类项目，能持续发版

### semantic-release 的问题

**问题 1：强迫完美的 commit 文化**

它 100% 依赖 commit message 正确。如果有个同事写了 `feat: fix a small bug` 而实际是个 bug fix，semantic-release 会错误地发 minor 版本。

工程上要用 **commitlint + husky**（pre-commit hook）强制格式检查：

```json
// package.json
{
  "devDependencies": {
    "@commitlint/cli": "^19.0.0",
    "@commitlint/config-conventional": "^19.0.0",
    "husky": "^9.0.0"
  }
}
```

```javascript
// commitlint.config.js
module.exports = { extends: ['@commitlint/config-conventional'] };
```

```
# .husky/commit-msg
npx --no-install commitlint --edit "$1"
```

但即使这样，也只能保证格式对，不能保证 **type 用得对**。有些团队用 squash merge + PR title 作为 commit message 源，比单 commit 规范更容易维护。

**问题 2：发版太激进**

每个 commit 都可能触发发版，短时间合并 10 个 PR 会发 10 次版本。对 lib 型项目是好事，对应用型项目有点吵。

**问题 3：Monorepo 支持弱**

semantic-release 原生只支持单包仓库。Monorepo 要用 `semantic-release-monorepo` 或 `multi-semantic-release`，配置复杂，坑多。

**问题 4：Release notes 质量依赖 commit message 质量**

如果 commit 写得很简略，changelog 就很简略。release-please 和 changesets 都让你单独维护一个叙述性的发版说明，质量可控。

## 方案二：release-please

Google 开源，设计哲学是"**半自动化 + 人工 gate**"。

### 工作流

```
开发者 commit: "feat: add user profile page"
  ↓
push 到 main
  ↓
release-please GitHub Action 运行
  ↓
扫描未发布的 commits
  ↓
创建 / 更新 "Release PR"
  ↓ (人看这个 PR)
合并 Release PR
  ↓
release-please 打 tag、发 GitHub Release、触发 downstream CI
```

Release PR 的内容：

```markdown
# chore(main): release 1.3.0

## 1.3.0 (2026-02-25)

### Features

* **api:** add user profile endpoint ([#234](https://github.com/org/repo/pull/234)) (a1b2c3d)
* **ui:** add dark mode toggle ([#238](https://github.com/org/repo/pull/238)) (d4e5f6a)

### Bug Fixes

* **auth:** handle expired JWT gracefully ([#240](https://github.com/org/repo/pull/240)) (b7c8d9e)
```

这个 PR 会自动更新（每次新 commit 都增量追加），直到你合并它。合并即发版。

### 配置示例

`release-please-config.json`：

```json
{
  "packages": {
    ".": {
      "release-type": "node",
      "changelog-path": "CHANGELOG.md",
      "bump-minor-pre-major": true,
      "bump-patch-for-minor-pre-major": true,
      "include-component-in-tag": false
    }
  }
}
```

`.release-please-manifest.json`：

```json
{
  ".": "1.2.3"
}
```

GitHub Actions：

```yaml
name: release-please
on:
  push:
    branches: [main]

permissions:
  contents: write
  pull-requests: write

jobs:
  release-please:
    runs-on: ubuntu-latest
    steps:
      - uses: googleapis/release-please-action@v4
        id: release
        with:
          token: ${{ secrets.GITHUB_TOKEN }}
          config-file: release-please-config.json
          manifest-file: .release-please-manifest.json

      - uses: actions/checkout@v4
        if: ${{ steps.release.outputs.release_created }}

      - name: Build and publish
        if: ${{ steps.release.outputs.release_created }}
        run: |
          npm ci
          npm run build
          npm publish
        env:
          NODE_AUTH_TOKEN: ${{ secrets.NPM_TOKEN }}
```

### Monorepo 支持

release-please 原生支持 monorepo，在 config 里声明多个 package：

```json
{
  "packages": {
    "packages/api": {
      "release-type": "node",
      "package-name": "@org/api"
    },
    "packages/ui": {
      "release-type": "node",
      "package-name": "@org/ui"
    },
    "services/worker": {
      "release-type": "go",
      "package-name": "worker"
    }
  }
}
```

每个 package 独立 changelog、独立版本号、独立 Release PR。一个 repo 里可以有三种不同语言的 package 共存。

release-please 还支持**语言感知的 bump**：Node 包 bump `package.json` 和 `CHANGELOG.md`，Go 包 bump `README.md` 里的版本链接和 Git tag，Python 包 bump `pyproject.toml`，Java 包 bump `pom.xml`。

这是 release-please 相比 semantic-release 最大的优势：**多语言 monorepo 的一等公民支持**。

### release-please 的优势

1. **人有最后一道关**：合并 Release PR 就是审核时刻
2. **多语言支持**：Go、Python、Java、Rust 都有 release-type
3. **Monorepo 原生**：不用额外配置
4. **Release PR 可读性强**：changelog 在 PR 里先看见

### release-please 的问题

1. **发版延迟**：必须有人合并 Release PR。周末没人发版（可能是好事也可能是问题）
2. **Commit 多了 Release PR 冲突**：大量 commit 快速合并时 Release PR 偶尔冲突
3. **配置复杂度中等**：比 semantic-release 多一个 manifest 文件

## 方案三：changesets

Vercel 团队维护，为 Monorepo 而生。

### 工作流

```
开发者写 PR 并运行 `pnpm changeset`
  ↓
工具问：影响哪些包？是什么级别？(patch/minor/major)
  ↓
在 .changeset/<random-name>.md 生成描述文件
  ↓
开发者把 .changeset/xxx.md 也 commit 进 PR
  ↓
PR 合并到 main
  ↓
changesets GitHub Action 运行
  ↓
扫描 .changeset/*.md 文件
  ↓
创建 / 更新 "Version Packages" PR
  ↓ (人审核)
合并 Version Packages PR
  ↓
changesets 打 tag、发布包、清空 .changeset/
```

### changeset 文件长这样

`.changeset/pretty-lamps-fly.md`：

```markdown
---
"@org/api": minor
"@org/ui": patch
---

Add user profile page to API, fix avatar rendering in UI
```

前 matter 声明影响哪些包和级别，正文是 changelog 条目。

### 配置示例

`.changeset/config.json`：

```json
{
  "$schema": "https://unpkg.com/@changesets/config@3.0.0/schema.json",
  "changelog": "@changesets/cli/changelog",
  "commit": false,
  "access": "restricted",
  "baseBranch": "main",
  "updateInternalDependencies": "patch",
  "ignore": []
}
```

GitHub Actions：

```yaml
name: Release
on:
  push:
    branches: [main]

permissions:
  contents: write
  pull-requests: write

jobs:
  release:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: pnpm/action-setup@v4
      - uses: actions/setup-node@v4
        with:
          node-version: 22
          cache: pnpm

      - run: pnpm install --frozen-lockfile
      - run: pnpm build

      - name: Create Release PR or Publish
        uses: changesets/action@v1
        with:
          publish: pnpm changeset publish
          version: pnpm changeset version
          commit: "chore: version packages"
          title: "chore: version packages"
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          NPM_TOKEN: ${{ secrets.NPM_TOKEN }}
```

### changesets 的独特优势

1. **每个 PR 都强制写 changelog**：这逼着开发者思考"我这个 PR 对用户的影响是什么"
2. **显式选包**：Monorepo 里一个 PR 改了多个 package，开发者明确指定影响哪些
3. **changeset 先于代码合并**：Review 的时候可以一起 review changelog 质量
4. **与 Conventional Commits 解耦**：不强制要求 commit message 格式

### 使用体验

开发流程：

```bash
# 写代码...
git checkout -b feature/profile

# 写 changeset
pnpm changeset
# ? Which packages would you like to include? › (Press <space> to select)
#   ◉ @org/api
#   ◉ @org/ui
#   ◯ @org/shared
# ? Which packages should have a major bump? ›
#   (none)
# ? Which packages should have a minor bump? ›
#   ◉ @org/api
# ? Which packages should have a patch bump? ›
#   ◉ @org/ui
# ? Please enter a summary for this change › Add profile page

git add .changeset/
git commit -m "feat: add profile page"
git push
```

合并 PR 到 main 后，changesets action 自动创建 "Version Packages" PR，里面包含：

- 更新 `package.json` 的版本号
- 更新 `CHANGELOG.md`
- 删除 `.changeset/*.md` 文件

合并这个 PR 即发版。

### changesets 的问题

1. **强制写 changeset 很烦**：每个 PR 都要跑 `changeset`，遗忘率高。需要 CI 检查 "没 changeset 的 PR 不能合并"。
2. **纯 Node 生态**：对 Go、Python、Rust 支持弱（只能手动搞）
3. **初学者门槛**：工具心智模型比 semantic-release 复杂
4. **和 Conventional Commits 没有绑定**：如果你们团队已经在用 CC，切换到 changesets 要双轨

## 选型建议

### 场景 1：开源库，小团队，单包

**推荐 semantic-release**。零人工介入，你只要写代码。适合那种"一个 maintainer + 几个贡献者"的 OSS 项目。

### 场景 2：产品型 app，commit 规范参差

**推荐 release-please**。半自动化但有人审核 Release PR，commit 规范没那么严也能忍。

### 场景 3：Monorepo（Node/TS 为主）

**推荐 changesets**。就是为 monorepo 而生。强制每 PR 写 changeset 的习惯一旦养成，changelog 质量远超自动生成。

### 场景 4：多语言 Monorepo（Node + Go + Python）

**推荐 release-please**。目前唯一原生支持多语言的方案。changesets 的 monorepo 只覆盖 JS 生态。

### 场景 5：内部服务，不发包到 npm/pypi，只打 Docker 镜像

**都可以，推荐 release-please**。你不需要 semantic-release 的激进自动化，你需要的是 "打 tag + 生成 changelog + 触发 Docker 构建" 这条链路。release-please 和 GitHub Release + Docker Action 结合最顺。

## Conventional Commits 落地的细节

### commitlint 强制格式

前面讲过，用 commitlint + husky 强制。但 CI 也要再查一遍，防止有人绕过 hook：

```yaml
# .github/workflows/lint-commit.yml
on: [pull_request]
jobs:
  commit-lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: wagoid/commitlint-github-action@v6
```

### Squash merge vs merge commit

三种合并策略影响很大：

- **Squash merge**：PR 所有 commit 被压成一个。PR title 必须符合 CC 格式（`feat: ...`、`fix: ...`）。
- **Merge commit**：保留所有 commit，每个 commit 都要符合 CC。
- **Rebase merge**：commit 一条条 rebase 上去，每个 commit 必须符合。

**推荐 squash merge**。理由：
- 开发者 PR 过程中的 "wip"、"fix typo" commit 不需要进主线历史
- 只需要关注 PR title 是否符合 CC，不用管每个 commit
- 合并后主线历史干净，一个 PR = 一个 commit = 一个 changelog 条目

配 GitHub 的 "PR title 必须符合 CC" 的 action：

```yaml
- uses: amannn/action-semantic-pull-request@v5
  env:
    GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

### scope 的用法

`feat(api): ...` 里的 `api` 是 scope，表示影响哪个模块。常见 scope：

- `api`：后端 API
- `ui`：前端 UI
- `deps`：依赖升级（给 Renovate 用）
- `ci`：CI 配置
- `docs`：文档
- `release`：发版本身

monorepo 里 scope 常对应包名：`feat(@org/api): ...`。release-please 和 changesets 都能自动识别。

### BREAKING CHANGE 的写法

三种写法都合法：

```
feat!: remove deprecated endpoint

feat(api)!: remove /v1

feat(api): remove /v1

BREAKING CHANGE: /v1 is removed. Use /v2.
```

第三种用 footer 的方式更详细，可以写多行说明迁移路径。

## Changelog 的最终形态

不管用哪个工具，生成的 `CHANGELOG.md` 应该长这样：

```markdown
# Changelog

## [1.3.0](https://github.com/org/repo/compare/v1.2.3...v1.3.0) (2026-02-25)

### Features

* **api:** add user profile endpoint ([#234](https://github.com/org/repo/pull/234)) ([a1b2c3d](https://github.com/org/repo/commit/a1b2c3d))
* **ui:** add dark mode toggle ([#238](https://github.com/org/repo/pull/238)) ([d4e5f6a](https://github.com/org/repo/commit/d4e5f6a))

### Bug Fixes

* **auth:** handle expired JWT gracefully ([#240](https://github.com/org/repo/pull/240)) ([b7c8d9e](https://github.com/org/repo/commit/b7c8d9e))

### Performance Improvements

* **db:** add index on users.email ([#245](https://github.com/org/repo/pull/245)) ([c1d2e3f](https://github.com/org/repo/commit/c1d2e3f))

## [1.2.3](https://github.com/org/repo/compare/v1.2.2...v1.2.3) (2026-02-20)
...
```

关键是 **commit hash 和 PR 都有链接**，方便追溯。三个工具生成的格式都类似，可以通过模板定制。

## 踩坑清单

### 坑 1：shallow clone 让工具找不到历史

semantic-release 和 release-please 都要读 Git 完整历史。GitHub Actions 默认 `fetch-depth: 1`（只拉最新 commit），工具会报 "can not find release history"。

固定加：

```yaml
- uses: actions/checkout@v4
  with:
    fetch-depth: 0
```

### 坑 2：GITHUB_TOKEN 权限不足

默认 `GITHUB_TOKEN` 不能创建 Release 或 push tag。要在 job 顶部声明：

```yaml
permissions:
  contents: write
  pull-requests: write
  issues: write
```

或在 repo 的 Settings → Actions → Workflow permissions 选 "Read and write"。

### 坑 3：[skip ci] 循环触发

semantic-release 发版后会自己 commit 更新 `CHANGELOG.md` 和 `package.json`。这个 commit 如果不加 `[skip ci]`，会再次触发 release workflow，死循环。

semantic-release 默认加 `[skip ci]`，但如果你自定义了 commit message，记得保留。

### 坑 4：npm publish 的 provenance

2025 年 npm 推了 provenance 签名。需要在 Action 里加：

```yaml
permissions:
  id-token: write

# 并且
- run: npm publish --provenance
```

这会用 Sigstore keyless 签名你的包。下游可以验证这个包真的是由你的 GitHub Actions 发布的。

### 坑 5：Monorepo 的版本同步

changesets 默认允许每个包独立版本号。但有些 Monorepo 想要所有包同步版本（比如 Babel 7.x 下所有 `@babel/*` 版本一致）。

changesets 支持 "fixed mode":

```json
{
  "fixed": [["@org/*"]]
}
```

release-please 类似，用 "linked versions" 功能。

## 结语

选型简版：

- **OSS 单包** → semantic-release
- **产品型 app** → release-please
- **JS Monorepo** → changesets
- **多语言 Monorepo** → release-please

前提都是团队愿意用 Conventional Commits。这个习惯我们用 commitlint + PR title 检查强推了两个月，之后就无感了，不强推很难成。

再往前一步：把 Renovate + release-please 串起来，依赖升级 PR → CI → patch 自动合并 → release-please 累积 → 周一合 Release PR 自动发版。我们跑通后确实做到了整条链路零人工，但前面两个月踩过不少 CI 竞态的坑，不要指望一次到位。

Sources:
- [semantic-release GitHub](https://github.com/semantic-release/semantic-release)
- [Conventional Commits spec](https://www.conventionalcommits.org/en/v1.0.0/)
- [NPM Release Automation Guide - Oleksii Popov](https://oleksiipopov.com/blog/npm-release-automation/)
- [Changesets vs Semantic Release - Brian Schiller](https://brianschiller.com/blog/2023/09/18/changesets-vs-semantic-release/)
- [Using semantic-release - LogRocket](https://blog.logrocket.com/using-semantic-release-automate-releases-changelogs/)
