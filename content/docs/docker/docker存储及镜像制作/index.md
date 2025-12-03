---
title: "Docker存储及镜像制作"
date: 2025-12-03T22:26:23+08:00
draft: false
tags: ["Docker"]
categories: ["Docker"]
author: "map[bio:幽默模块加载中...加载失败，请重新启动 email:17691281867@163.com headline:个人文档管理 image:img/111.png imagequality:96 links:[map[email:17691281867@163.com]] name:Wenzhuo Huang]"
description: "docker存储测试"
featured_image: ""
toc: true
math: false
diagram: false
keywords: []
params:
  reading_time: true   
---

# 五、Docker存储管理

### 1. **Bind Mounts (绑定挂载)**

- **描述**：绑定挂载将宿主机的文件或目录映射到容器内的某个路径。当你使用绑定挂载时，容器的数据将直接存储在宿主机的文件系统中，因此容器和宿主机之间共享文件系统。如果宿主机路径不存在，Docker 会自动创建它。

- **特点**：

  - **直接依赖宿主机的文件系统**：容器访问和修改的数据直接存储在宿主机上。
  - **与宿主机紧密耦合**：宿主机的文件或目录位置固定，因此，如果宿主机上的文件丢失，容器的数据也会丢失。。

- **创建命令**：

  ```
  docker run -v /host/path:/container/path mycontainer
  ```

  其中 `/host/path` 是宿主机上的文件或目录路径，`/container/path` 是容器内的路径。

  这将宿主机上的 `/home/user/data` 目录挂载到容器内的 `/data` 目录。

### 2. **Volumes (数据卷)**

- **描述**：数据卷是 Docker 提供的一种更为抽象的持久化存储方式。Docker 会将数据存储在宿主机的特定目录中，但用户无需直接管理这个目录。Docker 会自动处理数据存储的位置、生命周期等。
- **特点**：
  - **抽象化管理**：数据卷由 Docker 管理，宿主机的位置和细节对用户透明。
  - **持久性**：即使容器被删除，数据卷中的数据仍然存在，可以随时重新挂载到其他容器中。
  - **便于共享**：多个容器可以挂载同一个数据卷，方便容器之间共享数据。
  - **自动化管理**：Docker 可以自动清理不再使用的数据卷。
  - **优化性能**：数据卷是为持久化数据优化的，性能通常较好。

<img src="https://imagebed99.oss-cn-beijing.aliyuncs.com/typora_image/Image00085.jpg" alt="Image00085" style="zoom:50%;" />

- **创建命令**：

  ```
  docker volume create myvolume
  docker run -v myvolume:/container/path mycontainer
  ```

  其中 `myvolume` 是创建的数据卷的名称，`/container/path` 是容器内的挂载路径。

- **查看和管理数据卷**：

  - 查看所有数据卷：

    ```
    docker volume ls
    ```

  - 查看某个数据卷的详细信息：

    ```
    docker volume inspect mydbdata
    ```

  - 删除数据卷：

    ```
    docker volume rm mydbdata
    ```

- **注意事项**：

  - Docker 会将数据卷存储在宿主机的默认位置（通常是 `/var/lib/docker/volumes/`）中，但用户不需要关心该位置。
  - 数据卷支持容器间的共享，如果多个容器挂载同一个数据卷，容器间的数据修改会实时同步。

### 3. **tmpfs Mounts (临时文件系统挂载)**

- **描述**：`tmpfs` 是将容器的文件系统挂载到宿主机的内存中，通常用于存储临时数据。`tmpfs` 挂载提供的是一个**临时的内存存储**，数据不会写入到磁盘，在容器停止或重启时会丢失。

- **适用场景**：

  - 存储需要高性能、临时且不持久化的数据，例如缓存文件、会话信息等。
  - 不希望数据保留在宿主机上，也不希望数据在容器重启后丢失的场景。

- **特点**：

  - **内存存储**：所有数据都存储在内存中，速度较快。
  - **非持久化**：容器停止或重启时，数据会丢失。
  - **有限容量**：`tmpfs` 占用宿主机的内存，因此需要适当配置内存限制。
  - **临时文件存储**：适用于需要临时存储的数据，避免占用磁盘空间。

- **创建命令**：

  ```
  docker run --mount type=tmpfs,target=/container/path mycontainer
  ```

  这将为容器 `/container/path` 创建一个 `tmpfs` 挂载。

- **示例**：

  ```
  docker run --mount type=tmpfs,target=/tmp mycontainer
  ```

  这将把容器的 `/tmp` 目录挂载为一个临时的内存文件系统。

- **注意事项**：

  - 内存挂载不会持久化数据，容器停止后数据会丢失。
  - `tmpfs` 挂载通常用于高性能要求的临时存储（如缓存或日志数据）。

------

### **总结对比：**

| 特性           | **Bind Mounts**                          | **Volumes**                    | **tmpfs Mounts**             |
| -------------- | ---------------------------------------- | ------------------------------ | ---------------------------- |
| **存储位置**   | 宿主机文件系统的指定路径                 | Docker 管理的宿主机目录        | 容器的内存                   |
| **数据持久化** | 容器停止后数据不会丢失（依赖宿主机路径） | 容器删除后数据不会丢失         | 容器停止后数据丢失           |
| **使用场景**   | 容器与宿主机共享文件（开发环境）         | 持久化数据存储和容器间共享数据 | 临时数据存储（缓存、日志等） |
| **性能**       | 较慢（文件系统挂载）                     | 较快，专为持久化数据优化       | 非常快（内存存储）           |
| **共享数据**   | 容器和宿主机间共享，容器间不共享         | 容器间可以共享                 | 不适用于容器间共享           |
| **管理难易**   | 用户手动管理宿主机路径                   | Docker 自动管理                | 需要内存资源限制             |

# 六、Docker仓库管理

### 1. 私有register

```bash
docker pull registry
docker run -idt --name registry -v  /opt/registry:/var/lib/registry -p 5000:5000
```



### 2. harbor仓库

Harbor 是由 VMWare 公司开源的容器镜像仓库。事实上， Harbor 是在 Docker Registry 上进行了相应的企业级扩展，从而获得了更加广泛的应用，这些新的企业级特性包括：管理用户界面，基于角色的访问控制 ，AD/LDAP 集成以及审计日志等，足以满足基本企业需求。

官方 : https://goharbor.io/

Github: https://github.com/goharbor/harbor

服务器硬件配置：

 最低要求： CPU2 核 / 内存 4G/ 硬盘 40GB

 推荐： CPU4 核 / 内存 8G/ 硬盘 160GB

# 七、DockerFile

![Image00035](https://imagebed99.oss-cn-beijing.aliyuncs.com/typora_image/Image00035.jpg)

## 1. Dockerfile的指令

![在这里插入图片描述](https://imagebed99.oss-cn-beijing.aliyuncs.com/typora_image/202412072342206.png)

![在这里插入图片描述](https://imagebed99.oss-cn-beijing.aliyuncs.com/typora_image/202412072342947.png)

| **指令**        | **描述**                                                     | **示例**                                                |
| --------------- | ------------------------------------------------------------ | ------------------------------------------------------- |
| **FROM**        | 指定基础镜像，Dockerfile 的第一行通常是 `FROM`，它指定了构建镜像所依赖的基础镜像。 | `FROM ubuntu:20.04`                                     |
| **RUN**         | 执行命令并创建一个新的镜像层，常用于安装软件包或执行一些配置任务。 | `RUN apt-get update && apt-get install -y python3`      |
| **CMD**         | 容器启动时默认执行的命令，如果 `docker run` 中没有指定命令，`CMD` 指令指定的命令会被执行。 | `CMD ["python", "app.py"]`                              |
| **ENTRYPOINT**  | 设置容器启动时执行的主命令，`ENTRYPOINT` 和 `CMD` 配合使用，指定容器启动的命令和参数。 | `ENTRYPOINT ["python", "app.py"]`                       |
| **COPY**        | 将本地文件或目录复制到镜像中的指定路径。                     | `COPY ./localfile.txt /app/`                            |
| **ADD**         | 类似 `COPY`，但支持更多功能，比如自动解压 tar 文件，下载 URL 中的文件并添加到镜像中。 | `ADD ./archive.tar.gz /app/`                            |
| **EXPOSE**      | 声明容器在运行时监听的端口，帮助文档和 Docker 网络相关功能使用。 | `EXPOSE 80`                                             |
| **ENV**         | 设置环境变量，可以在容器内的运行时被引用。                   | `ENV APP_ENV=production`                                |
| **WORKDIR**     | 设置工作目录，所有后续的指令都会在该目录下执行。             | `WORKDIR /app`                                          |
| **VOLUME**      | 创建一个挂载点，挂载到容器内的指定目录。                     | `VOLUME ["/data"]`                                      |
| **USER**        | 指定容器内执行命令时使用的用户。                             | `USER myuser`                                           |
| **ARG**         | 定义构建时使用的参数，构建时通过 `--build-arg` 设置。        | `ARG VERSION=1.0`                                       |
| **LABEL**       | 为镜像添加元数据，常用于描述镜像的作者、版本等信息。         | `LABEL version="1.0" maintainer="yourname@example.com"` |
| **SHELL**       | 设置默认的 shell 类型和选项。默认是 `/bin/sh -c`，可以使用其他 shell，如 `/bin/bash -c`。 | `SHELL ["/bin/bash", "-c"]`                             |
| **HEALTHCHECK** | 定义容器的健康检查机制，用于确保容器在运行时的状态是否正常。 | `HEALTHCHECK CMD curl --fail http://localhost:8080/     |
| **STOPSIGNAL**  | 设置容器停止时使用的信号。默认是 SIGTERM，用户可以修改为其他信号。 | `STOPSIGNAL SIGINT`                                     |

## 2. 常用命令详解

### 2.1 FROM

```markdown
# 格式：
　　FROM <image>
　　FROM <image>:<tag>
　　FROM <image>@<digest>
　　FROM [ --platform=xxx] <image:tag> [AS <name>]     #指定基础镜像平台及二次构建的基础名称
　　
# 参数解释：
    --platfrom 用于指定平台镜像，参数有：linux/amd64 , linux/arm64 ,windows/amd64

# 示例：　　
	FROM mysql:5.6
# 注：
   tag或digest是可选的，如果不使用这两个值时，会使用latest版本的基础镜像

```

~~~Dockerfile
### 多阶段构建
# 第一阶段
FROM node:14 AS builder
WORKDIR /app
COPY package.json ./
RUN npm install
COPY . .
RUN npm run build

# 第二阶段
FROM nginx:alpine
COPY --from=builder /app/build /usr/share/nginx/html

~~~



### 2.2 ARG

```markdown
#指令语法
ARG <name>[=<dafault value] 

# 示例
ARG CODE=latets
FROM base:$CODE
```

说明：ARG仅在编译时生效，且可以出现在FROM之前。ENV指定的变量保存于镜像之中

### 2.3 RUN

```markdown
# RUN用于在构建镜像时执行命令，其有以下两种命令执行方式：
# shell执行
格式：
    RUN <command>
    RUN echo "Hello, World!"
# exec执行
格式：
    RUN ["executable", "param1", "param2"]
示例：
    RUN [ "/bin/bash","-c","echo", "Hello, World!"]

注：RUN指令创建的中间镜像会被缓存，并会在下次构建中使用。如果不想使用这些缓存镜像，
可以在构建时指定--no-cache参数，如：docker build --no-cache

```

#### 减少层数

```dockerfile
RUN apt-get update && \
    apt-get install -y curl && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*
```

#### 使用 `&&` 和 `||` 控制流程

~~~markdown
RUN make install && echo "Install succeeded" || echo "Install failed"
~~~

#### 使用 `&&` 和 `set -ex`

~~~markdown
RUN set -ex && \
    apt-get update && \
    apt-get install -y curl
构建过程中可以跟踪每个命令的执行情况，可以使用 set -ex。这将在命令执行失败时导致构建停止，并显示所有执行的命令
~~~

#### exec与cmd格式的区别

**Exec 格式**的语法如下：

```
dockerfile复制代码RUN ["executable", "param1", "param2", ...]
```

- **直接执行**：在 Exec 格式中，Docker 直接执行指定的可执行文件（如 `echo`）。它不会经过 shell 进程。这意味着任何通常在 shell 中处理的特性（如命令替换、变量扩展、管道、重定向等）将不会被执行。

- 缺少 shell 功能

  ：由于没有使用 shell，Exec 格式不会处理如下情况：

  - 环境变量扩展：在 Exec 格式中，您不能像在 shell 中那样使用 `$VAR` 来引用环境变量。
  - 命令连接：不能使用 `&&`、`||` 等逻辑操作符来连接命令。
  - 输入输出重定向：像 `>` 或 `<` 的重定向操作将不适用



### 2.4 CMD&ENTRYPORINT

#### CMD

```markdown
# 格式：
    CMD ["executable","param1","param2"] (执行可执行文件，优先)
    CMD ["param1","param2"] (设置了ENTRYPOINT，则直接调用ENTRYPOINT添加参数)
    CMD command param1 param2 (执行shell内部命令)
# 示例：
    CMD echo "This is a test." | wc -l
    CMD ["/usr/bin/wc","--help"]
```

注：CMD不同于RUN，CMD用于指定在容器启动时所要执行的命令，而RUN用于指定镜像构建时所要执行的命令。

#### ENTRYPOINT

~~~markdown
# 格式：
    ENTRYPOINT ["executable", "param1", "param2"] (可执行文件, 优先)
    ENTRYPOINT ["executable", "param1"] （后续可接收CMD的传参，或run命令的传参）
    ENTRYPOINT command param1 param2 (shell内部命令)
# 示例：
    FROM ubuntu
    ENTRYPOINT ["ls", "/usr/local"]
    CMD ["/usr/local/tomcat"]
  之后，docker run 传递的参数，都会先覆盖cmd,然后由cmd 传递给entrypoint ,做到灵活应用
  
# shell和exec的区别
shell 格式会阻止CMD的参数及run命令行参数被使用，但执行的命令会变成shell子命令，无法正常接收single
~~~

注：ENTRYPOINT与CMD非常类似，不同的是通过docker run执行的命令不会覆盖ENTRYPOINT，
 而docker run命令中指定的任何参数，都会被当做参数再次传递给CMD。
 Dockerfile中只允许有一个ENTRYPOINT命令，多指定时会覆盖前面的设置，
 而只执行最后的ENTRYPOINT指令。
 通常情况下，ENTRYPOINT 与CMD一起使用，ENTRYPOINT 写默认命令，当需要参数时候 使用CMD传参

#### CMD和ENYPORINT的联合使用

| **ENTRYPOINT**         | **CMD**         | **执行结果**                                          |
| ---------------------- | --------------- | ----------------------------------------------------- |
| 无入口 (无 ENTRYPOINT) | 无命令 (无 CMD) | 容器会退出，无任何动作                                |
| 无入口 (无 ENTRYPOINT) | exec 格式 CMD   | 执行 CMD 指定的命令                                   |
| 无入口 (无 ENTRYPOINT) | shell 格式 CMD  | 执行 CMD 指定的命令                                   |
| exec 格式 ENTRYPOINT   | 无命令 (无 CMD) | 执行 ENTRYPOINT 指定的命令                            |
| exec 格式 ENTRYPOINT   | exec 格式 CMD   | 以 CMD 的内容作为参数传递给 ENTRYPOINT 执行           |
| exec 格式 ENTRYPOINT   | shell 格式 CMD  | CMD 会被作为参数传递给 ENTRYPOINT 执行（shell 命令）  |
| shell 格式 ENTRYPOINT  | 无命令 (无 CMD) | 执行 ENTRYPOINT 指定的 shell 命令                     |
| shell 格式 ENTRYPOINT  | exec 格式 CMD   | CMD 会作为参数传递给 ENTRYPOINT 执行，执行 shell 命令 |
| shell 格式 ENTRYPOINT  | shell 格式 CMD  | CMD 会作为参数传递给 ENTRYPOINT 执行，执行 shell 命令 |

###  2.5 LABEL

```markdown
# 格式：
    LABEL <key>=<value> <key>=<value> <key>=<value> ...
# 示例：
　　LABEL version="1.0" description="这是一个Web服务器" by="IT笔录"
注：
　　使用LABEL指定元数据时，一条LABEL指定可以指定一或多条元数据，指定多条元数据时不同元数据
　　之间通过空格分隔。推荐将所有的元数据通过一条LABEL指令指定，以免生成过多的中间镜像。
```

### 2.6 ENV

~~~markdown
# 格式：
    ENV <key> <value>  #<key>之后的所有内容均会被视为其<value>的组成部分，因此，一次只能设置一个变量
    ENV <key>=<value> ...  #可以设置多个变量，每个变量为一个"<key>=<value>"的键值对，如果<key>中包含空格，可以使用\来进行转义，也可以通过""来进行标示；另外，反斜线也可以用于续行
# 示例：
    ENV myName="John Doe"
    ENV myDog=Rex\ The\ Dog	
    ENV myCat=fluffy

~~~

### 2.7 EXPOSE

~~~markdown
# 格式：
    EXPOSE <port> [<port>...]
# 示例：
    EXPOSE 80 443
    EXPOSE 8080    
    EXPOSE 11211/tcp 11211/udp
注：EXPOSE并不会让容器的端口访问到主机。要使其可访问，需要在docker run运行容器时通过-p来发布这些端口，或通过-P参数来发布EXPOSE导出的所有端口。只是增加dockerfile的可读性

如果没有暴露端口，后期也可以通过-p 8080:80方式映射端口，但是不能通过-P形式映射
~~~

### 2.8 ONBUILD

~~~markdown
# 格式：　
	ONBUILD [INSTRUCTION]
# 示例：
　　ONBUILD ADD . /app/src
　　ONBUILD RUN /usr/local/bin/python-build --dir /app/src
注：
　　NNBUID后面跟指令，当当前的镜像被用做其它镜像的基础镜像，该镜像中的触发器将会被钥触发
~~~

### 2.9 USER

~~~markdown
# 格式:　　
USER user　　
USER user:group　　
USER uid　　
USER uid:gid　　
USER user:gid　　
USER uid:group
 
# 示例：    　　
     USER www
 注：
　　使用USER指定用户后，Dockerfile中其后的命令RUN、CMD、ENTRYPOINT都将使用该用户。
　　镜像构建完成后，通过docker run运行容器时，可以通过-u参数来覆盖所指定的用户。

~~~

~~~markdown
# 格式
 SHELL ["executable", "parameters"]
- executable：要使用的 shell 可执行文件（如 /bin/bash 或 /bin/sh 等）。
- parameters：传递给 shell 的参数，通常是一个字符串数组
# 示例（默认情况下，Docker 使用 /bin/sh -c 来执行命令）
SHELL ["/bin/bash", "-c"]   #使用 bash 替代默认的 sh
RUN echo "Hello from Bash!"  # 后续命令将使用 bash 执行

# 注意
SHELL 指令在 Dockerfile 中是全局生效的，这意味着它会影响 Dockerfile 中后续的所有命令（如 RUN, CMD, ENTRYPOINT 等）。
SHELL 只影响容器内执行命令时使用的 shell 类型，不会影响容器启动时的 shell 类型
~~~

# 八、Docker日志管理

## 1. **Docker 日志驱动（Logging Drivers）**

Docker 提供了多种日志驱动，可以选择适合自己需求的方式来记录和管理容器日志。每个容器可以配置不同的日志驱动，Docker 默认使用的日志驱动是 `json-file`。在启动容器时，可以通过 `--log-driver` 参数指定日志驱动。

常见的日志驱动有：

| **日志驱动**  | **描述**                                                     | **示例**                                |
| ------------- | ------------------------------------------------------------ | --------------------------------------- |
| **json-file** | 默认的日志驱动，日志以 JSON 格式存储在宿主机上的文件中。每个日志条目包含了时间戳、日志级别、消息等信息。 | `docker run --log-driver=json-file ...` |
| **syslog**    | 将日志发送到宿主机的 syslog 服务。适用于需要集成系统日志收集的场景。 | `docker run --log-driver=syslog ...`    |
| **journald**  | 将日志发送到 `systemd` 的 journal 服务。适用于基于 `systemd` 的 Linux 系统。 | `docker run --log-driver=journald ...`  |
| **fluentd**   | 将日志发送到 Fluentd，用于集成日志收集和分析系统。           | `docker run --log-driver=fluentd ...`   |
| **gelf**      | 使用 Graylog Extended Log Format (GELF)，通常与 Graylog 配合使用。适用于大规模日志聚合和分析。 | `docker run --log-driver=gelf ...`      |
| **awslogs**   | 将日志发送到 AWS CloudWatch Logs，适用于在 AWS 环境中管理日志。 | `docker run --log-driver=awslogs ...`   |
| **splunk**    | 将日志发送到 Splunk 日志管理系统。                           | `docker run --log-driver=splunk ...`    |
| **none**      | 禁用容器日志，不收集任何日志。                               | `docker run --log-driver=none ...`      |

#### 设置日志驱动

可以在容器启动时通过 `--log-driver` 参数指定日志驱动。例如：

```
bashCopy Codedocker run --log-driver=syslog my-container
```

也可以在 Docker 守护进程配置文件中设置默认的日志驱动，这样所有容器都会使用该日志驱动。例如，在 `/etc/docker/daemon.json` 文件中添加：

```
jsonCopy Code{
  "log-driver": "fluentd"
}
```

## 2. **查看 Docker 容器日志**

不同的日志驱动存储日志的方式不同，但无论使用哪种驱动，Docker 都提供了基本的命令来查看容器的日志。

- **查看容器日志**：使用 `docker logs` 命令来查看某个容器的日志。该命令支持实时查看、过滤、分页等操作。

```
bashCopy Codedocker logs <container_id or container_name>
```

- 常用选项

  - `-f` 或 `--follow`：实时跟踪日志输出，类似于 `tail -f`。
  - `--since`：查看指定时间之后的日志。
  - `--tail`：显示最后 N 行日志。
  - `-t`：显示日志的时间戳。

例如，查看容器 `my-container` 的日志并实时跟踪输出：

```
bashCopy Codedocker logs -f --since="2024-12-05T10:00:00" my-container
```

## 3. **Docker 日志的存储与管理**

Docker 默认将容器日志存储在宿主机的 `/var/lib/docker/containers/<container-id>/` 目录中，日志文件名为 `container-id-json.log`。这对于 `json-file` 日志驱动有效，其他日志驱动可能将日志输出到不同的地方。

#### json-file 驱动日志格式：

- 时间戳
- 日志级别
- 日志消息

例如，默认的日志文件 `/var/lib/docker/containers/<container-id>/<container-id>-json.log` 可能包含以下内容：

```
jsonCopy Code{"log":"Hello, World!\n","stream":"stdout","time":"2024-12-05T10:00:00.000000000Z"}
{"log":"Error: Something went wrong!\n","stream":"stderr","time":"2024-12-05T10:01:00.000000000Z"}
```

### 日志轮转和日志大小限制

对于 `json-file` 驱动，Docker 提供了日志轮转功能，通过配置 `max-size` 和 `max-file` 来限制日志文件的大小和数量。

- `max-size`：每个日志文件的最大大小。
- `max-file`：最多保留多少个日志文件。

例如，在启动容器时设置日志轮转：

```
bashCopy Codedocker run --log-driver=json-file --log-opt max-size=10m --log-opt max-file=3 my-container
```

这表示每个日志文件的最大大小为 10MB，最多保留 3 个文件，达到限制时会进行轮转。

## 4. **集中式日志收集与分析**

对于生产环境中的容器，通常会使用集中式日志收集和分析工具来管理日志。常见的工具和方案包括：

- **ELK Stack**：Elasticsearch, Logstash, Kibana。Logstash 作为日志收集工具将日志发送到 Elasticsearch，Kibana 用于分析和可视化日志数据。
- **Fluentd**：作为日志收集和转发工具，将日志发送到其他日志管理系统（如 Elasticsearch、Kafka 等）。
- **Graylog**：一个开源的日志管理平台，支持集成多种日志来源。
- **Splunk**：一个商业化的日志分析平台，广泛用于企业级日志管理和监控。

## 5. **日志分析和监控**

除了查看容器日志外，日志分析和监控工具能够帮助自动化地识别和报警，尤其是在大规模容器化环境中。结合 Prometheus、Grafana 等工具，可以对日志进行集中的监控和可视化。

#### 使用 Prometheus + Grafana 监控 Docker 日志：

- **Prometheus**：收集和存储来自 Docker 容器的度量数据。
- **Grafana**：用来显示和分析这些度量数据。

通过安装 Docker 的 `cAdvisor` 或 `Prometheus` 采集器，可以定期抓取 Docker 容器的日志数据，进一步做健康检查、性能分析等。

### 总结

Docker 提供了多种日志管理机制，可以通过不同的日志驱动、配置选项和集中式日志系统来满足不同的需求。根据实际的生产环境需求，可以选择合适的日志驱动（如 `json-file`、`syslog`、`fluentd` 等），并配置日志的轮转、存储和分析机制，以实现高效的日志管理和问题排查。

# 九、Docker Compose

Docker Compose要解决的问题是部署和管理繁多的服务，Docker Compose并不是通过脚本和各种冗长的`docker` 命令来将应用组件组织起来，而是通过一个声明式的配置文件描述整个应用，从而使用一条命令完成部署。

下载[地址](https://github.com/docker/compose/releases/download)

## 1.docker-compose的使用

1. **多容器应用管理**
   Docker Compose 可以定义多个服务（服务是指 Docker 容器中的应用），这些服务可以在一个 YAML 文件中配置，Compose 会在启动时自动创建并启动这些服务。常见的场景如：Web 应用（前端）、数据库、缓存服务等多个容器的组合。
2. **开发与测试环境的自动化部署**
   在开发和测试阶段，开发人员往往需要启动多个依赖服务（例如数据库、缓存、消息队列等），`docker-compose` 可以一次性启动这些服务，确保开发和测试环境一致性。
3. **简化配置与扩展**
   使用 `docker-compose.yml` 配置文件，可以将服务的配置集中管理，通过声明式的方式方便修改和更新配置。对于不同的环境（如开发、测试、生产），可以使用不同的 Compose 配置文件，轻松切换。
4. **版本控制与共享**
   由于 `docker-compose.yml` 是文本文件，开发者可以将其添加到版本控制系统中（如 Git），并与团队成员共享，从而确保每个人在相同的环境下工作。

## 2. docker-compose的yaml

`docker-compose.yml` 文件是 Docker Compose 的核心配置文件，使用 YAML 格式定义，主要包括以下几个部分。

```bash
version: "3.8"  # 指定 Compose 文件的版本

services:  # 服务部分，定义各个容器
  web:  # 服务名称
    image: nginx:latest  # 使用的镜像
    ports:
      - "8080:80"  # 映射端口
    volumes:
      - ./html:/usr/share/nginx/html  # 映射本地文件夹到容器

  db:
    image: mysql:5.7
    environment:
      MYSQL_ROOT_PASSWORD: example
    volumes:
      - db-data:/var/lib/mysql  # 使用命名卷

  redis:
    image: redis:alpine
    ports:
      - "6379:6379"

volumes:  # 定义数据卷
  db-data:
```

#### 检测docker-compose配置

```bash
docker-compose config     # 检查配置
docker-compose config -q  # 检查配置，有问题才有输出
```



## 3. docker-compose的相关命令

### Docker Compose 常用命令

| 命令                     | 功能                                                         | 示例                                                      |
| ------------------------ | ------------------------------------------------------------ | --------------------------------------------------------- |
| `docker-compose up`      | 启动所有定义的服务容器，并在后台运行。如果服务未构建，会自动构建。 | `docker-compose up` 或 `docker-compose up -d`（后台运行） |
| `docker-compose down`    | 停止并删除所有容器、网络、卷等资源。                         | `docker-compose down`                                     |
| `docker-compose build`   | 构建或重新构建服务的镜像（如果配置了 `build`）。             | `docker-compose build`                                    |
| `docker-compose logs`    | 查看服务的日志输出。                                         | `docker-compose logs` 或 `docker-compose logs <服务名>`   |
| `docker-compose ps`      | 查看运行中的容器及其状态。                                   | `docker-compose ps`                                       |
| `docker-compose exec`    | 在运行中的容器内执行命令（例如进入容器的 bash）。            | `docker-compose exec web bash`                            |
| `docker-compose stop`    | 停止运行中的容器，但不删除。                                 | `docker-compose stop`                                     |
| `docker-compose restart` | 重启服务容器。                                               | `docker-compose restart`                                  |