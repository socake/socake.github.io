---
title: "Docker简介"
date: 2025-12-03T22:26:23+08:00
draft: false
tags: ["Docker"]
categories: ["Docker"]
author: "map[bio:幽默模块加载中...加载失败，请重新启动 email:17691281867@163.com headline:个人文档管理 image:img/111.png imagequality:96 links:[map[email:17691281867@163.com]] name:Wenzhuo Huang]"
description: ""
summary: "Docker是一个开源的容器化平台。它彻底改变了软件的打包、分发和运行方式，使应用及其运行环境成为一个轻量级、可移植的“容器”，从而解决了“在本地环境能运行，在其他环境却失败”的经典难题"
featured_image: ""
toc: true
math: false
diagram: false
keywords: []
params:
  reading_time: true                 
---

# 一、Docker简介

## 1. 介绍

Docker是一种运行于Linux和Windows上的软件，用于创建、管理和编排容器。

Linux容器（Linux Containers)属于一个轻量级的应用程序隔离机制。允许将单个操作系统管理的资源划分到孤立的组中，以更好的在孤立组之间平衡有冲突的资源使用需求。

它与虚拟化相比，不需要指令级模拟，也不需要即使编译。所占用空间又比虚拟机等程序占用资源少的多。

![image-20241206000039849](https://imagebed99.oss-cn-beijing.aliyuncs.com/typora_image/image-20241206000039849.png)

## 2. 优点

- 敏捷度高,轻量级，启动快，部署快。
- 可移植、适应性强：应用程序和底层环境解耦
- docker images 的版本控制


## 3. 原理

Linux 容器是通过 kernel 中三个主要部件得以实现的 :

- 名称空间

- 资源控制

- SELinux 安全控制

### （1）命名空间（namespace）

命名空间是 Linux 内核提供的一种隔离机制，它可以把不同进程的资源隔离开。Docker 使用命名空间来实现容器的资源隔离。每个容器运行时都在一个独立的命名空间中

![image-20241206000758607](https://imagebed99.oss-cn-beijing.aliyuncs.com/typora_image/image-20241206000758607.png)

| **命名空间** | **作用**           | **介绍**                                                     |
| ------------ | ------------------ | ------------------------------------------------------------ |
| **PID**      | 隔离进程 ID        | 每个容器有自己的进程 ID 空间，容器内的进程无法看到其他容器的进程。 |
| **NET**      | 隔离网络栈         | 每个容器有自己的网络接口、IP 地址和路由表，网络流量相互隔离。 |
| **IPC**      | 隔离进程间通信     | 每个容器有自己的消息队列、信号量和共享内存，确保数据独立性。 |
| **UTS**      | 隔离主机名和域名   | 每个容器可以拥有自己的主机名和域名，不影响宿主机或其他容器。 |
| **MNT**      | 隔离文件系统挂载点 | 每个容器有自己的文件系统视图，允许挂载不同的存储介质和目录。 |
| **USER**     | 隔离用户和用户组   | 每个容器有自己的用户和用户组映射，提高安全性。               |



### （2）资源限制（Cgroups）

控制组（Cgroups）是 Linux 内核提供的另一种技术，用于限制和管理进程的资源使用。Docker 使用控制组来对容器的 CPU、内存、磁盘和网络带宽等资源进行限制，确保容器不会超过分配的资源，从而避免资源争用。

- **CPU 限制**：限制容器使用的 CPU 时间。
- **内存限制**：限制容器使用的内存。
- **磁盘 I/O 限制**：限制容器的磁盘读写速度。
- **网络带宽限制**：限制容器的网络传输速度

## 4. 相关名词

![图片](https://imagebed99.oss-cn-beijing.aliyuncs.com/typora_image/202412071052353.png)

### （1）Docker

Docker的容器运行时和编排引擎:Docker引擎是用于运行和编排容器的基础设施工具,其他Docker公司或第三方的产品都是围绕Docker引擎进行开发和集成的.

Docker 提供了用户接口、 API 、镜像格式和对Linux 容器管理的工具及命令

Docker 镜像是一个只读的模板，包含了：运行容器所需的文件系统内容、环境变量、程序配置等。

镜像可以基于其他镜像构建，并且是层叠的，每一层都代表了对原始镜像的修改，镜像可以从 Docker Hub 、或者私有仓库中获取。

![image-20241206001145029](https://imagebed99.oss-cn-beijing.aliyuncs.com/typora_image/image-20241206001145029.png)

Docker 属于一个标准的 systemd 的服务单元 :docker.service此服务可以通过 systemctl 等命令来进行管理。同时用户可以使用 docker 命令，来对容器进行

管理、配置。镜像一般存储在本地系统的 /var/lib/docker 目录上。但加载或卸载镜像仍然需要使用 docker 命令来完成

![DockerXMind_00](https://imagebed99.oss-cn-beijing.aliyuncs.com/typora_image/202412072146458.png)

### （2）Docker images

Docker 镜像（Docker Image）是 Docker 容器的基础，它是一个包含了应用程序及其运行所需依赖环境的只读文件系统。镜像可以在不同的环境中一致地运行，为容器提供一个可靠的、可重复的运行时环境。

镜像本身是只读的。这意味着镜像不能直接修改，而是通过启动容器并在容器中进行修改（例如写入数据、修改文件系统等）。这些修改是容器的部分，并不会影响镜像的内容。容器运行时会创建一个新的可写层，这个可写层位于镜像的顶层。

![image-20241206001443865](https://imagebed99.oss-cn-beijing.aliyuncs.com/typora_image/image-20241206001443865.png)

### （3）Docker container

容器是镜像的运行时实例。正如从虚拟机模板上启动VM一样，用户也同样可以从单个镜像上启动一个或多个容器。虚拟机和容器最大的区别是容器更快并且更轻量级——与虚拟机运行在完整的操作系统之上相比，容器会共享其所在主机的操作系统/内核

![image-20241206001902581](https://imagebed99.oss-cn-beijing.aliyuncs.com/typora_image/image-20241206001902581.png)

**镜像与容器的关系**

- **镜像（Image）** 是容器的模板，包含了应用程序和所有必要的依赖环境。镜像本身是静态的，存储在 Docker Registry 或本地。
- **容器（Container）** 是镜像的运行时实例。容器是一个独立的、可执行的环境，它基于镜像创建，并可以对镜像内容进行修改，但这些修改仅在容器内有效，不会影响镜像本身

![image-20241206003032445](https://imagebed99.oss-cn-beijing.aliyuncs.com/typora_image/image-20241206003032445.png)

**容器和虚拟机的区别**

容器和虚拟机都依赖于宿主机才能运行

- 虚拟机：先要开启物理机并启动Hypervisor引导程序占有机器上的全部物理资源（CPU、RAM、存储和NIC），Hypervisor将这些物理资源划分为虚拟资源，并且看起来与真实物理资源完全一致。然后Hypervisor会将这些资源打包进一个叫作虚拟机（VM）的软件结构当中。这样用户就可以使用这些虚拟机，并在其中安装操作系统和应用。前面提到需要在物理机上运行4个应用，所以在Hypervisor之上需要创建4个虚拟机并安装4个操作系统，然后安装4个应用

<img src="https://imagebed99.oss-cn-beijing.aliyuncs.com/typora_image/Image00032.jpg" alt="Image00032" style="zoom: 50%;" />

- 容器：Docker中可以选择Linux，或者内核支持内核中的容器原语的新版本Windows。与虚拟机模型相同，OS也占用了全部硬件资源。在OS层之上，需要安装容器引擎（如Docker）。容器引擎可以获取系统资源 ，比如进程树、文件系统以及网络栈，接着将资源分割为安全的互相隔离的资源结构，称之为容器。每个容器看起来就像一个真实的操作系统，在其内部可以运行应用。按照前面的假设，需要在物理机上运行4个应用。因此，需要划分出4个容器并在每个容器中运行一个应用

<img src="https://imagebed99.oss-cn-beijing.aliyuncs.com/typora_image/Image00033.jpg" alt="Image00033" style="zoom:50%;" />

<img src="https://imagebed99.oss-cn-beijing.aliyuncs.com/typora_image/image-20241206003002109.png" alt="image-20241206003002109" style="zoom:50%;" />

### （4） Docker registry

基本镜像是一个 tar 的归档文件。在使用docker 时可以手工加载或者通过其他的软件仓库进行下载。

![image-20241206002745588](https://imagebed99.oss-cn-beijing.aliyuncs.com/typora_image/image-20241206002745588.png)

## 5. Docker的安装

### 三种安装方式

#### 1. yum简单安装

~~~shell
卸载docker
 sudo yum remove docker \
                  docker-client \
                  docker-client-latest \
                  docker-common \
                  docker-latest \
                  docker-latest-logrotate \
                  docker-logrotate \
                  docker-selinux \
                  docker-engine-selinux \
                  docker-engine
#安装docker依赖
yum install -y yum-utils device-mapper-persistent-data lvm2
#安装镜像仓库
yum-config-manager --add-repo http://mirrors.aliyun.com/docker-ce/linux/centos/docker-ce.repo
#刷新缓存
yum makecache fast
#安装docker-ce
yum -y install docker-ce
#服务启动
systemctl start docker
systemctl enable --now docker
~~~

#### 2. 使用本地rpm包安装

```bash
#下载rpm包及依赖
yumdownloader --resolve yum-utils device-mapper-persistent-data lvm2
yumdownloader --resolve yum -y install docker-ce
#本地安装 
yum localinstall /path/to/package.rpm -y
#验证
rpm -qa | grep package_name
docker info 
docker -v
```

#### **3. 下载二进制包安装**

Docker[下载地址]([Index of linux/static/stable/x86_64/](https://download.docker.com/linux/static/stable/x86_64/))

Docker-compose[下载地址]([Releases · docker/compose](https://github.com/docker/compose/releases?page=1))

**下载文件并上传至需要安装的服务器**

```bash
#解压并复制
tar -zxvf docker-23.0.1.tgz -C /opt/     #注意版本号
cp -p /opt/docker/* /usr/bin

```

**服务注册**

`vim /usr/lib/systemd/system/docker.service`

```bash
$ `vim /usr/lib/systemd/system/docker.service`
[Unit]
Description=Docker Application Container Engine
Documentation=http://docs.docker.com
After=network.target docker.socket
[Service]
Type=notify
EnvironmentFile=-/run/flannel/docker
WorkingDirectory=/usr/local/bin
ExecStart=/usr/bin/dockerd \
                -H tcp://0.0.0.0:4243 \
                -H unix:///var/run/docker.sock \
                --selinux-enabled=false \
                --log-opt max-size=1g
ExecReload=/bin/kill -s HUP $MAINPID
# Having non-zero Limit*s causes performance problems due to accounting overhead
# in the kernel. We recommend using cgroups to do container-local accounting.
LimitNOFILE=infinity
LimitNPROC=infinity
LimitCORE=infinity
# Uncomment TasksMax if your systemd version supports it.
# Only systemd 226 and above support this version.
#TasksMax=infinity
TimeoutStartSec=0
# set delegate yes so that systemd does not reset the cgroups of docker containers
Delegate=yes
# kill only the docker process, not all processes in the cgroup
KillMode=process
Restart=on-failure
[Install]
WantedBy=multi-user.target

$ systemctl daemon-reload&&systemctl start docker
$ systemctl enable --now docker
$ docker version
```
