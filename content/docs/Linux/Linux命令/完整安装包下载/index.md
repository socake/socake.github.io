---
title: "完整安装包下载"
date: 2025-12-03T22:53:28+08:00
draft: false
tags: []
categories: ["Linux"]
author: "map[bio:幽默模块加载中...加载失败，请重新启动 email:17691281867@163.com headline:个人文档管理 image:img/111.png imagequality:96 links:[map[email:17691281867@163.com]] name:Wenzhuo Huang]"
description: ""
featured_image: ""
toc: true
math: false
diagram: false
keywords: []
params:
  reading_time: true                 
---

#### **下载包到本地的几种方法**

```markdown
1. CentOS--使用 yumdownloader
sudo yum install yum-utils   #下载工具包
yumdownloader <package-name>
yumdownloader --resolve
eg：
yumdownloader wget   #下载wget到本地

> 或者
sudo yum install --downloadonly --downloaddir=<directory-path> <package-name>

2. 使用dnf
sudo dnf download <package-name>


3. 搜索  rpm包
yum search <search-term>

```

#### **安装本地包的命令**

```markdown
1. 使用 yum 安装本地包
sudo yum localinstall /path/to/package.rpm
eg:
sudo yum localinstall /tmp/example-package.rpm

2. 使用 使用 dnf 安装本地包 
sudo dnf install /path/to/package.rpm
eg：
sudo dnf install /tmp/example-package.rpm

3. 直接使用 rpm 安装包
sudo rpm -ivh /path/to/package.rpm
# 依赖关系: 当使用 rpm 命令安装时，系统不会自动处理依赖关系，可能会导致安装失败。使用 yum 或 dnf 会更方便，因为它们会自动解决依赖问题。
# 包的路径: 确保使用正确的包路径，若包在当前目录下，可以使用相对路径。
# 检查安装状态: 安装完成后，可以使用以下命令检查包是否安装成功：
rpm -qa | grep <package-name>
```


