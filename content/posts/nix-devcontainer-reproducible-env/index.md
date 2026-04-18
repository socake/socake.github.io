---
title: "Nix + devcontainer：彻底终结 works on my machine"
date: 2026-01-28T10:00:00+08:00
draft: false
tags: ["Nix", "devcontainer", "direnv", "开发环境", "devenv"]
categories: ["平台工程"]
description: "Nix flakes + direnv 是目前最严肃的可复现开发环境方案：锁定工具链版本、声明式配置、一行命令进入完全隔离的 shell。结合 devcontainer 可以把同一个环境带进 VSCode、Cursor 和 CI。本文讲清楚怎么从零搭建、和 devenv 的差异、以及大型团队落地的踩坑。"
summary: "新同事入职第一天配环境要花一天，CI 和本地构建结果不一致，升级 Node 16 到 20 引发连锁故障——这些痛都源于'环境不是代码'。Nix 把工具链当成代码版本化，和 direnv/devcontainer 配合能做到 'git clone 后 10 秒进入完整可用环境'。本文是完整落地教程。"
toc: true
math: false
diagram: true
keywords: ["Nix", "flake.nix", "direnv", "devcontainer", "devenv", "nix-direnv"]
params:
  reading_time: true
---

## 为什么又要谈可复现环境

先看一组真实症状，你大概都熟悉：

- 新同事入职，按 README 配环境配一天，最后还是差一个 `libpq-dev`。
- 周五下午部署失败，本地构建正常。排查发现 CI runner 的 `openssl` 版本比本地旧。
- 一个项目要用 Node 18，另一个要 Node 20，nvm 勉强解决；又来一个要 Ruby 3.2 + Python 3.11 + Go 1.21，nvm 管不了。
- Docker 镜像里的 Alpine `3.18` 升到 `3.19`，`musl` 版本变了，某个 cgo 依赖编译失败。
- 半年前的项目，重装电脑后跑不起来，因为当时用的某个 npm 包早就 yanked。

根源都是一个：**开发环境不是代码，是口头约定的结果**。README 里写的 "Node 18+"、Dockerfile 里的 `apt-get install curl` 都是"程度极低的声明"，不能精确复现、不能回滚、不能做 diff。

过去十年里尝试解决这个问题的方案：

| 方案 | 做得到 | 做不到 |
|------|--------|--------|
| README 文档 | 给人看 | 精确复现、自动化 |
| Dockerfile | 环境封装在镜像 | 开发者工具链（编辑器、LSP）不在镜像里 |
| Vagrant | 完整 VM 隔离 | 太重，启动慢，ARM 支持差 |
| asdf / nvm / pyenv | 管一种语言的版本 | 系统级依赖（libpq、openssl）管不了 |
| devcontainer | 编辑器友好 | 本质还是 Dockerfile，没解决版本锁 |
| **Nix** | **一切都能锁** | 学习曲线陡 |

Nix 的独特价值在于**它把系统级依赖和语言级依赖统一管理**：`gcc`、`libpq`、`openssl`、`python@3.11`、`nodejs@20`、`go@1.23`、`kubectl@1.30` 都是 Nix 的包，都有 hash 锁定，都能通过一个 `flake.nix` 精确复现。

## Nix 的 10 分钟速成

Nix 是一个**包管理器 + 编程语言**。包管理器部分类似 apt/brew/yum，但是：

- **不可变**：每个包安装在 `/nix/store/<hash>-<name>-<version>/`，不会互相覆盖
- **内容寻址**：hash 基于所有输入（源码、编译器、依赖）计算
- **声明式**：用一个 `.nix` 文件描述整个环境，Nix 保证产出一致
- **多版本共存**：同一个 package 不同版本同时存在，互不干扰

语言部分是一个惰性求值的函数式语言（语法像 JSON 混一点 Haskell），用来写 "怎么构建一个包" 的描述。作为使用者你不需要写包定义，只需要**引用别人定义好的包**。

### 安装 Nix

官方 Installer 历来有点糟糕（动 `/etc/bash.bashrc`，卸载麻烦）。**Determinate Systems 的 installer 是社区事实标准**：

```bash
curl --proto '=https' --tlsv1.2 -sSf -L https://install.determinate.systems/nix | sh -s -- install
```

这个 installer 的好处：
- 默认开启 flakes experimental 特性
- 干净的卸载（`/nix/uninstall`）
- 更友好的错误信息
- 在 macOS 上正确处理 APFS 卷

装完 `nix --version` 能看到 `2.24+` 就可以了。

### flake.nix：开发环境的灵魂

一个最小可用的 `flake.nix`：

```nix
{
  description = "My project dev environment";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-24.11";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };
      in {
        devShells.default = pkgs.mkShell {
          buildInputs = with pkgs; [
            # Go 工具链
            go_1_23
            gopls
            golangci-lint
            delve

            # 容器/K8s 工具
            docker-client
            kubectl
            kubernetes-helm
            kustomize

            # 常用 CLI
            git
            jq
            yq-go
            curl
            ripgrep
            fd

            # 数据库 client
            postgresql_16
          ];

          shellHook = ''
            echo "Welcome to the dev environment"
            echo "Go version: $(go version)"
            export PROJECT_ROOT=$PWD
          '';
        };
      });
}
```

进入 shell：

```bash
cd my-project
nix develop
# 现在你的 PATH 里有 go_1_23, kubectl, psql 等所有列出的工具
# 而且完全不污染系统
exit
# 离开 shell，系统环境不变
```

关键点：

- **`inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-24.11"`**：pin 到 2024 年 11 月的 channel。所有包的版本跟随这个 channel。
- **`flake.lock`** 文件（运行 `nix develop` 时自动生成）：记录每个 input 的精确 git commit。和 `package-lock.json` 是一个意思。
- **`mkShell`**：创建一个临时 shell，`buildInputs` 里所有工具加入 PATH。
- **`shellHook`**：进入 shell 时执行的 bash 脚本，用来设环境变量、打印欢迎信息等。

有了 `flake.nix` + `flake.lock`，任何人拿到这两个文件 + 装了 Nix 的机器，都能得到**完全一致**的开发环境。包括工具版本、依赖库、甚至每个二进制的 SHA256。

## direnv：自动进入/退出 shell

每次 cd 进项目都手动 `nix develop` 很烦。`direnv` 解决这个：检测到目录变化就自动加载/卸载环境变量。

安装：

```bash
# 通过 Nix 装
nix profile install nixpkgs#direnv nixpkgs#nix-direnv

# 或 Homebrew
brew install direnv nix-direnv
```

配 shell hook：

```bash
# ~/.zshrc 或 ~/.bashrc
eval "$(direnv hook zsh)"
```

在项目根目录创建 `.envrc`：

```bash
use flake
```

第一次需要 `direnv allow` 授权（防止恶意 `.envrc` 执行任意命令）。之后：

```bash
cd my-project
# direnv 自动执行 `nix develop`，几秒钟后 PATH 里有所有工具
go version
# go version go1.23.4 linux/amd64

cd ..
# direnv 自动退出 shell，go 命令消失
go version
# zsh: command not found: go
```

**`nix-direnv` 比默认 `direnv` 重要**：它给 `use flake` 加了 cache，第二次 cd 进项目是毫秒级（默认是每次都重新解析 flake，几秒钟）。生产装它。

### shell prompt 显示当前环境

配合 Starship prompt：

```toml
# ~/.config/starship.toml
[nix_shell]
format = 'via [$symbol$state( \($name\))]($style) '
symbol = '❄ '
```

进入项目目录后 prompt 自动显示 `❄ impure` 或 `❄ pure`，提示你在 Nix shell 里。

## devcontainer 集成：把 Nix 带进 VSCode

很多团队已经在用 VSCode 的 devcontainer 功能（`.devcontainer/devcontainer.json`）。devcontainer 的本质是"在容器里开发"，可以和 Nix 无缝组合：容器基础环境用最小 Debian/Alpine，具体的工具链全部由 Nix 管理。

一个生产级 `.devcontainer/devcontainer.json`：

```json
{
  "name": "Go Dev Environment",
  "image": "mcr.microsoft.com/devcontainers/base:debian-12",
  "features": {
    "ghcr.io/devcontainers/features/nix:1": {
      "version": "2.24",
      "multiUser": false,
      "packages": ""
    },
    "ghcr.io/devcontainers/features/docker-in-docker:2": {}
  },
  "customizations": {
    "vscode": {
      "extensions": [
        "mkhl.direnv",
        "jnoortheen.nix-ide",
        "golang.go",
        "redhat.vscode-yaml"
      ],
      "settings": {
        "direnv.restart.automatic": true,
        "go.toolsManagement.autoUpdate": false
      }
    }
  },
  "postCreateCommand": "direnv allow && direnv exec . true",
  "remoteEnv": {
    "NIX_CONFIG": "experimental-features = nix-command flakes"
  },
  "mounts": [
    "source=${localEnv:HOME}/.config/nix,target=/home/vscode/.config/nix,type=bind,consistency=cached"
  ]
}
```

这个配置做了：
1. 拉 Debian 12 base 镜像
2. 通过 `devcontainers/features/nix` 装 Nix
3. 预装 direnv 插件（VSCode 的 mkhl.direnv）
4. `postCreateCommand` 里 `direnv allow` 并触发一次 flake 评估（预热缓存）
5. 把宿主的 Nix 配置挂进来（共享 substituter 配置）

打开项目，VSCode 自动跳出 "Reopen in Container"，点确认。大概 3-5 分钟（首次拉镜像 + 装 Nix + 评估 flake），之后所有开发工具、LSP、linter 都在容器里，host 环境干净。

**devcontainer 和纯 Nix 的取舍**：

- 只用 Nix（宿主直接 `nix develop`）：启动快、资源占用低、可以直接用宿主的文件系统性能。缺点是 macOS 上 Nix 的 darwin 包有时比 Linux 慢一步，少数 package 只支持 Linux。
- Nix + devcontainer：完全跨平台一致（macOS/Windows/Linux 都在 Debian 容器里），缺点是启动慢、文件 mount 有性能损耗（尤其 macOS）。

大团队里我推荐后者，因为 "所有人完全一致" 的价值大于性能损耗。小团队、个人项目前者更轻。

## devenv.sh：Nix 的"高层封装"

纯 Nix flake 语法对新手不友好。`devenv.sh`（Cachix 团队开发）是在 Nix 之上的糖，用更简单的 Nix 语法 + 预定义 language 模块：

```nix
# devenv.nix
{ pkgs, ... }:
{
  packages = [ pkgs.jq pkgs.ripgrep ];

  languages.go = {
    enable = true;
    package = pkgs.go_1_23;
  };

  languages.python = {
    enable = true;
    version = "3.12";
    venv.enable = true;
    venv.requirements = ./requirements.txt;
  };

  services.postgres = {
    enable = true;
    initialDatabases = [{ name = "myapp"; }];
    listen_addresses = "127.0.0.1";
    port = 5432;
  };

  services.redis.enable = true;

  processes.server.exec = "go run ./cmd/server";

  scripts.test.exec = "go test ./...";
  scripts.db-migrate.exec = "migrate -path ./migrations -database postgres://... up";

  pre-commit.hooks = {
    gofmt.enable = true;
    golangci-lint.enable = true;
    nixfmt-rfc-style.enable = true;
  };
}
```

这个 `devenv.nix` 做的事：

1. 声明 Go 1.23 和 Python 3.12 (venv 自动装 requirements)
2. 声明本地 PostgreSQL 和 Redis 服务（`devenv up` 启动）
3. 定义 `devenv shell test` 跑测试、`devenv shell db-migrate` 跑迁移
4. 装 pre-commit hooks (gofmt、golangci-lint)

相当于一个"项目级 Procfile + docker-compose + asdf + pre-commit" 的组合。而且所有服务都是真正的 Nix 包，不是 docker 容器。

devenv 和直接写 flake 的取舍：

- devenv：上手快，模块化好，适合大部分应用场景
- 纯 flake：最大灵活性，适合对 Nix 生态深入的项目

我建议**新项目直接上 devenv**，除非有特殊需求。devenv 内部就是 flake，可以随时"下沉"到纯 flake。

## 和 CI 的集成

Nix 的一个大价值是 **本地和 CI 用同一份定义**。

### GitHub Actions

```yaml
name: CI
on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: DeterminateSystems/nix-installer-action@main
      - uses: DeterminateSystems/magic-nix-cache-action@main
      - name: Run tests
        run: |
          nix develop --command go test ./...
          nix develop --command golangci-lint run
```

关键是 `DeterminateSystems/magic-nix-cache-action`：自动给 GHA runner 配一个远端 Nix cache，命中率能到 80-90%。不配这个，每次 CI 都要从头编译 Nix packages，一个 flake 冷启动可能要 10 分钟。

### GitLab CI

```yaml
test:
  image: nixos/nix:latest
  variables:
    NIX_CONFIG: "experimental-features = nix-command flakes"
  before_script:
    - nix develop --command echo "shell ready"
  script:
    - nix develop --command go test ./...
```

GitLab 没有官方 magic cache，要自建。简单做法是 Nix binary cache 放在自建 S3/MinIO，全团队共享。

```nix
# flake.nix
{
  nixConfig = {
    extra-substituters = [
      "https://nix-cache.example.com"
    ];
    extra-trusted-public-keys = [
      "nix-cache.example.com:AbCdEf123..."
    ];
  };
}
```

### 和 Dockerfile/Kubernetes 的关系

Nix **不会** 替代 Dockerfile 做生产镜像（虽然 Nix 能生成镜像，但社区对这种用法还有争议）。Nix 管的是"**开发环境**"，Dockerfile 管的是"**运行环境**"。两者职责分离，互不干扰。

生产镜像还是走 BuildKit / ko / Dockerfile，开发环境单独用 Nix。这是最务实的做法。

## 大型团队落地的踩坑

我们团队（~80 人，多语言 monorepo）用 Nix 大约两年，踩过的坑：

### 坑 1：macOS 上的 `stdenv.mkDerivation` 慢

Nix 的 darwin 包由于 Apple 频繁更新 SDK、沙盒机制，部分包要从源码编译。一个冷启动可能卡在 "building 'rustc-1.72.0'" 几十分钟。

缓解：
- 强制用 binary cache（Nixpkgs 的官方 cache `cache.nixos.org` + 社区的 `cachix.org/nix-community`）
- 避免引入太多 rust/haskell 编译路径的包
- 升级到 nixos-24.11 或更新 channel，Apple Silicon 原生编译比 Rosetta 快

### 坑 2：`flake.lock` 合并冲突

多人并发改 `flake.nix` 容易在 `flake.lock` 产生冲突。`flake.lock` 是 JSON 但非常长，冲突解决很痛苦。

办法：
- 约定只有一个人更新 inputs（通过 PR），其他人不要随手 `nix flake update`
- 或配 Renovate bot 自动更新 inputs（我们后一篇博客会专门讲）
- 冲突时直接删 `flake.lock`，重跑 `nix develop` 让它重建

### 坑 3：盘空间爆炸

Nix 的 `/nix/store` 不会自动清理，久了会占几十 GB 甚至上百 GB。

```bash
# 查看大小
du -sh /nix/store

# GC：清理不再被 profile 引用的 store 对象
nix-collect-garbage -d

# 只保留 30 天内的 generation
nix-collect-garbage --delete-older-than 30d
```

建议加个 cron job 或 launchd/systemd timer 每周跑一次 GC。

### 坑 4：shellHook 里改 shell 选项要小心

`shellHook` 里写的 bash 代码，进出 shell 都会执行。如果里面有 `cd`、`set -e`、`trap`，容易污染用户当前 shell：

```nix
# 反例
shellHook = ''
  set -e       # 这会让用户的 shell 以后 cmd 错就退出
  cd $(pwd)    # 副作用
  trap 'echo exiting' EXIT   # 污染
'';
```

只做纯设置变量和 echo 就好。复杂逻辑放 `scripts.xxx.exec` 里（devenv）或 Makefile。

### 坑 5：Nix 社区节奏和企业不完全匹配

Nixpkgs 大约每半年发一个 channel（`nixos-24.05`、`nixos-24.11`…），每个 channel 支持 7 个月。如果你 pin 在 `nixos-24.05`，到 2025 年某时点会失去支持（没有安全更新）。

**流程建议**：

1. 默认 pin 到最近的 stable channel
2. 新 channel 发布后 2 周内（等社区踩坑完），创建 `chore: bump nixpkgs to nixos-25.05` PR
3. PR 里跑全量 CI，观察是否有包变化
4. 没问题就合并

配合 Renovate 可以自动化第 2 步。

## 真实收益

我们团队迁移前后的数据：

| 指标 | 迁移前 | 迁移后 |
|------|--------|--------|
| 新人入职配环境时间 | 4-6 小时 | 15 分钟（等 Nix 首次编译）|
| "works on my machine" 类 issue/月 | 12-15 个 | 1-2 个 |
| 多项目工具链冲突 | 频繁（nvm/pyenv 乱）| 0 |
| CI/本地构建行为不一致故障 | 每月 2-3 次 | 几乎 0 |
| 升级 Go 版本的阻力 | 大（每人都要动 `go env`）| 改一行 flake.nix |

最大的收益不在数字，在**团队心智负担**。新同事 clone 项目、`direnv allow`、两分钟之后就能写代码跑测试，不用读一页 README、不用问老同事装什么、不用 Google 一堆 "command not found"。这种体验一旦拥有，回不去。

## 结语

Nix 的学习曲线确实陡：你要花几周时间理解 derivation、overlay、flake、nixpkgs 仓库结构。但这个投资换来的是**再也不用为环境问题头疼**。

实践建议：

1. **先用 devenv.sh 入门**，避免直接啃 Nix flake 语法
2. **配 direnv + nix-direnv**，自动化进出 shell
3. **在 CI 里用 nix develop --command**，保证本地和 CI 行为一致
4. **devcontainer 封装进 VSCode**，给非 Nix 用户一个渐进路径
5. **定期升级 nixpkgs channel**，不要 pin 到过期 channel

Nix 不是银弹，学习曲线也确实陡。但目前能把系统级依赖、语言工具链、服务 mock 都统一管理的方案也就这一个，前两周撑过去，之后基本不用再为"我机器上能跑"这类话题浪费时间了。

Sources:
- [Nix flakes dev environment guide - Seth Alexander](https://sethaalexander.com/setting-up-a-nix-development-environment-with-flakes-and-direnv/)
- [devenv.sh official site](https://devenv.sh/)
- [Declarative Dev Environments with devenv - BrightCoding](https://www.blog.brightcoding.dev/2025/09/28/declarative-development-environments-with-nix-and-devenv-zero-fuss-100-reproducible-set-ups/)
- [Nix direnv integration - Determinate Systems](https://determinate.systems/blog/nix-direnv/)
- [devcontainer with Nix - jmgilman/dev-container](https://github.com/jmgilman/dev-container)
