---
title: "Ansible 批量运维自动化：从临时命令到 Role 工程化"
date: 2025-02-12T12:06:00+08:00
draft: false
tags: ["Ansible", "自动化", "运维", "配置管理", "DevOps"]
categories: ["Linux"]
description: "从 Ansible 核心设计理念出发，系统讲解 Inventory 管理、常用模块、Playbook 工程化、Role 目录结构和 Vault 敏感变量管理，结合批量部署 Node Exporter 和 K8s 节点调优的真实案例。"
summary: "Ansible 无 Agent、SSH 推送、幂等性三大特性让它成为 Linux 批量运维的利器。本文从入门用法到 Role 工程化实践，梳理了日常运维中高频场景的完整操作思路和踩坑经验。"
toc: true
math: false
diagram: false
keywords: ["Ansible", "Playbook", "Role", "Inventory", "Ansible Vault", "批量运维", "配置管理"]
params:
  reading_time: true
---

## Ansible 的核心优势

做过运维的人大概都经历过这个阶段：机器少的时候用 for 循环 + ssh 命令搞定，机器多了就维护一堆 shell 脚本，每次还要担心"这台机器有没有执行过这个脚本"、"环境变量对不对"。Ansible 的出现解决了这些痛点。

**无 Agent**：不需要在目标机器上安装任何客户端，只要 SSH 通就能管。这点在你接手一批已有机器时特别重要，不用先装一遍 Agent 再操作。

**SSH 推送**：控制机推送任务到目标机执行，权限边界清晰。对比 Puppet/Chef 的 pull 模式，Ansible 的 push 模式在紧急场景下响应更快，不需要等 Agent 的轮询间隔。

**幂等性**：大多数 Ansible 模块设计为幂等的，执行一次和执行十次效果相同。`package` 模块会检查软件包是否已安装，`file` 模块会检查文件是否已存在，`service` 模块会检查服务是否已是目标状态。这让你可以放心地重复执行 Playbook，不用担心副作用。

**YAML 描述配置**：用声明式语言描述目标状态，而不是命令式地描述操作步骤。理解起来更直观，也更容易做 Code Review。

## Inventory 管理

Inventory 定义了 Ansible 要管理的主机列表，以及如何对它们分组。

### 静态 Inventory

适合机器数量固定、不常变化的场景：

```ini
# inventory/hosts
[web]
web-01.example.com
web-02.example.com ansible_port=2222

[db]
db-01.example.com ansible_user=ubuntu ansible_become=true
db-02.example.com

[monitor]
prometheus-01.example.com

# 嵌套组
[production:children]
web
db
monitor

# 组变量
[web:vars]
nginx_worker_processes=4
app_env=production
```

YAML 格式的 Inventory（更推荐，结构更清晰）：

```yaml
# inventory/hosts.yml
all:
  children:
    web:
      hosts:
        web-01.example.com:
        web-02.example.com:
          ansible_port: 2222
    db:
      hosts:
        db-01.example.com:
          ansible_user: ubuntu
          ansible_become: true
        db-02.example.com:
    production:
      children:
        web:
        db:
```

### 动态 Inventory（AWS EC2）

机器在 AWS 上动态扩缩，不可能手动维护 Inventory。Ansible 提供了 `aws_ec2` 插件：

```yaml
# inventory/aws_ec2.yml
plugin: aws_ec2
regions:
  - us-west-2
filters:
  instance-state-name: running
  tag:Environment: production
keyed_groups:
  # 按 Tag:Role 自动分组
  - key: tags.Role
    prefix: role
  # 按实例类型分组
  - key: instance_type
    prefix: type
hostnames:
  - private-ip-address  # 内网 IP 作为主机名（VPN 场景）
compose:
  ansible_host: private_ip_address
```

```bash
# 测试动态 Inventory
ansible-inventory -i inventory/aws_ec2.yml --list
ansible-inventory -i inventory/aws_ec2.yml --graph
```

### Inventory 变量组织

```
inventory/
├── hosts.yml
├── group_vars/
│   ├── all.yml          # 所有主机共用的变量
│   ├── web.yml          # web 组的变量
│   └── production.yml   # production 组的变量
└── host_vars/
    └── db-01.example.com.yml  # 单台主机的变量
```

变量优先级：`host_vars` > `group_vars/<specific-group>` > `group_vars/all`。

## 常用模块速查

### 文件操作

```yaml
# copy：上传本地文件
- name: Upload config file
  copy:
    src: files/nginx.conf
    dest: /etc/nginx/nginx.conf
    owner: root
    group: root
    mode: '0644'
    backup: yes  # 覆盖前备份原文件

# template：渲染 Jinja2 模板后上传
- name: Render and upload template
  template:
    src: templates/app.conf.j2
    dest: /etc/app/app.conf
    mode: '0640'

# file：创建目录/文件，设置权限，创建软链接
- name: Create log directory
  file:
    path: /var/log/myapp
    state: directory
    owner: www-data
    mode: '0755'

# lineinfile：确保文件中包含某一行（幂等修改）
- name: Set ulimit in limits.conf
  lineinfile:
    path: /etc/security/limits.conf
    line: '* soft nofile 65536'
    regexp: '^\* soft nofile'
```

### 包管理

```yaml
# yum/dnf（RHEL 系）
- name: Install required packages
  yum:
    name:
      - curl
      - wget
      - htop
    state: present

# apt（Debian 系）
- name: Install packages
  apt:
    name: "{{ packages }}"
    state: present
    update_cache: yes
    cache_valid_time: 3600  # 缓存有效期，避免每次都 apt update
  vars:
    packages:
      - curl
      - jq
      - net-tools
```

### 服务管理

```yaml
- name: Ensure nginx is running and enabled
  service:
    name: nginx
    state: started
    enabled: yes

# systemd 模块（更多控制选项）
- name: Reload systemd and start service
  systemd:
    name: myapp
    state: started
    enabled: yes
    daemon_reload: yes  # 等同于 systemctl daemon-reload
```

### 命令执行

```yaml
# command：执行命令，不经过 shell，不支持管道/重定向（更安全）
- name: Check disk usage
  command: df -h /data
  register: disk_info
  changed_when: false  # 查询操作不算 changed

# shell：经过 /bin/sh，支持管道/重定向（需要时才用）
- name: Get active connections
  shell: ss -tn | grep ESTABLISHED | wc -l
  register: conn_count
  changed_when: false

# 用 register 捕获输出，用 debug 打印
- debug:
    msg: "Active connections: {{ conn_count.stdout }}"
```

## Playbook 结构

一个完整的 Playbook 示例——部署 Node Exporter：

```yaml
# playbooks/deploy-node-exporter.yml
---
- name: Deploy Prometheus Node Exporter
  hosts: all
  become: true
  vars:
    node_exporter_version: "1.7.0"
    node_exporter_user: "node_exporter"
    install_dir: "/opt/node_exporter"
    listen_port: 9100

  pre_tasks:
    - name: Check if node_exporter is already installed
      stat:
        path: "{{ install_dir }}/node_exporter"
      register: binary_stat

    - name: Check current version
      command: "{{ install_dir }}/node_exporter --version"
      register: current_version
      when: binary_stat.stat.exists
      changed_when: false
      ignore_errors: true

  tasks:
    - name: Create node_exporter user
      user:
        name: "{{ node_exporter_user }}"
        system: yes
        shell: /usr/sbin/nologin
        home: /nonexistent
        create_home: no

    - name: Create install directory
      file:
        path: "{{ install_dir }}"
        state: directory
        owner: "{{ node_exporter_user }}"
        mode: '0755'

    - name: Download node_exporter
      get_url:
        url: "https://github.com/prometheus/node_exporter/releases/download/v{{ node_exporter_version }}/node_exporter-{{ node_exporter_version }}.linux-amd64.tar.gz"
        dest: "/tmp/node_exporter-{{ node_exporter_version }}.tar.gz"
        timeout: 60
      when: not binary_stat.stat.exists or node_exporter_version not in (current_version.stdout | default(''))

    - name: Extract node_exporter
      unarchive:
        src: "/tmp/node_exporter-{{ node_exporter_version }}.tar.gz"
        dest: /tmp/
        remote_src: yes
      when: not binary_stat.stat.exists or node_exporter_version not in (current_version.stdout | default(''))

    - name: Copy binary
      copy:
        src: "/tmp/node_exporter-{{ node_exporter_version }}.linux-amd64/node_exporter"
        dest: "{{ install_dir }}/node_exporter"
        owner: "{{ node_exporter_user }}"
        mode: '0755'
        remote_src: yes
      notify: Restart node_exporter

    - name: Create systemd service
      template:
        src: templates/node_exporter.service.j2
        dest: /etc/systemd/system/node_exporter.service
        mode: '0644'
      notify:
        - Reload systemd
        - Restart node_exporter

    - name: Ensure node_exporter is running
      service:
        name: node_exporter
        state: started
        enabled: yes

  handlers:
    - name: Reload systemd
      systemd:
        daemon_reload: yes

    - name: Restart node_exporter
      service:
        name: node_exporter
        state: restarted

  post_tasks:
    - name: Wait for node_exporter to be ready
      wait_for:
        port: "{{ listen_port }}"
        timeout: 30

    - name: Verify metrics endpoint
      uri:
        url: "http://localhost:{{ listen_port }}/metrics"
        status_code: 200
      changed_when: false
```

对应的 systemd 模板：

```ini
# templates/node_exporter.service.j2
[Unit]
Description=Prometheus Node Exporter
After=network.target

[Service]
User={{ node_exporter_user }}
Group={{ node_exporter_user }}
Type=simple
ExecStart={{ install_dir }}/node_exporter \
  --web.listen-address=:{{ listen_port }} \
  --collector.filesystem.mount-points-exclude="^/(sys|proc|dev|host|etc)($$|/)"
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
```

执行 Playbook：

```bash
# 语法检查
ansible-playbook playbooks/deploy-node-exporter.yml --syntax-check

# dry run（不实际执行，只显示会做什么）
ansible-playbook playbooks/deploy-node-exporter.yml --check --diff

# 只在特定主机组执行
ansible-playbook playbooks/deploy-node-exporter.yml -l web

# 只执行特定 tags
ansible-playbook playbooks/deploy-node-exporter.yml --tags "install,config"

# 从某个 task 开始执行
ansible-playbook playbooks/deploy-node-exporter.yml --start-at-task "Copy binary"
```

## Role 工程化

当多个 Playbook 里有重复逻辑时，应该抽成 Role。Role 是一种标准化的目录结构，可以跨 Playbook 复用，也可以发布到 Ansible Galaxy 供他人使用。

### Role 目录结构

```
roles/
└── node_exporter/
    ├── defaults/
    │   └── main.yml      # 默认变量（优先级最低，可被覆盖）
    ├── vars/
    │   └── main.yml      # 角色内部变量（优先级较高，一般不对外暴露）
    ├── tasks/
    │   ├── main.yml      # 任务入口（include 其他任务文件）
    │   ├── install.yml
    │   └── configure.yml
    ├── handlers/
    │   └── main.yml      # handler 定义
    ├── templates/
    │   └── node_exporter.service.j2
    ├── files/
    │   └── static_files   # 静态文件
    ├── meta/
    │   └── main.yml      # Role 元数据，依赖声明
    └── README.md
```

```yaml
# roles/node_exporter/defaults/main.yml
node_exporter_version: "1.7.0"
node_exporter_port: 9100
node_exporter_user: "node_exporter"
node_exporter_install_dir: "/opt/node_exporter"
node_exporter_extra_args: []
```

```yaml
# roles/node_exporter/tasks/main.yml
---
- import_tasks: install.yml
  tags: [install]

- import_tasks: configure.yml
  tags: [config]
```

在 Playbook 中使用 Role：

```yaml
# site.yml
---
- name: Setup monitoring
  hosts: all
  become: true
  roles:
    - role: node_exporter
      vars:
        node_exporter_version: "1.8.0"
        node_exporter_port: 9100
    
    - role: filebeat
      when: ansible_os_family == "Debian"
```

### 批量修改 K8s 节点 sysctl

真实案例：K8s 集群新加节点后需要统一调整内核参数：

```yaml
# roles/k8s_node_tuning/tasks/main.yml
---
- name: Load required kernel modules
  modprobe:
    name: "{{ item }}"
    state: present
  loop:
    - br_netfilter
    - overlay
    - ip_vs
    - ip_vs_rr
    - ip_vs_wrr
    - ip_vs_sh

- name: Ensure modules load on boot
  copy:
    dest: /etc/modules-load.d/k8s.conf
    content: |
      br_netfilter
      overlay
      ip_vs
      ip_vs_rr
      ip_vs_wrr
      ip_vs_sh

- name: Set K8s required sysctl parameters
  sysctl:
    name: "{{ item.key }}"
    value: "{{ item.value }}"
    sysctl_file: /etc/sysctl.d/99-kubernetes.conf
    reload: yes
  loop:
    - { key: 'net.bridge.bridge-nf-call-iptables', value: '1' }
    - { key: 'net.bridge.bridge-nf-call-ip6tables', value: '1' }
    - { key: 'net.ipv4.ip_forward', value: '1' }
    - { key: 'net.ipv4.tcp_max_syn_backlog', value: '65536' }
    - { key: 'net.core.somaxconn', value: '65536' }
    - { key: 'fs.file-max', value: '1000000' }
    - { key: 'vm.swappiness', value: '0' }
    - { key: 'vm.overcommit_memory', value: '1' }

- name: Disable swap
  command: swapoff -a
  when: ansible_swaptotal_mb > 0
  changed_when: true

- name: Remove swap from fstab
  lineinfile:
    path: /etc/fstab
    regexp: '^.*\sswap\s'
    state: absent
```

## Ansible Vault：加密敏感变量

数据库密码、API Token 不应该明文存在代码仓库里，Vault 解决这个问题。

```bash
# 创建加密的变量文件
ansible-vault create group_vars/production/vault.yml

# 编辑加密文件
ansible-vault edit group_vars/production/vault.yml

# 加密已有明文文件
ansible-vault encrypt group_vars/production/secrets.yml

# 查看加密文件内容（不解密到磁盘）
ansible-vault view group_vars/production/vault.yml

# 修改 vault 密码
ansible-vault rekey group_vars/production/vault.yml
```

vault.yml 内容示例：

```yaml
# group_vars/production/vault.yml（加密后存储）
vault_db_password: "S3cur3P@ssw0rd"
vault_api_token: "eyJhbGci..."
vault_slack_webhook: "https://hooks.slack.com/..."
```

在普通变量文件中引用：

```yaml
# group_vars/production/vars.yml
db_password: "{{ vault_db_password }}"
api_token: "{{ vault_api_token }}"
```

执行时提供密码：

```bash
# 交互式输入密码
ansible-playbook site.yml --ask-vault-pass

# 从文件读取密码（CI/CD 场景）
echo "your-vault-password" > ~/.vault_pass
chmod 600 ~/.vault_pass
ansible-playbook site.yml --vault-password-file ~/.vault_pass

# 也可以在 ansible.cfg 中配置
# vault_password_file = ~/.vault_pass
```

## 踩坑记录

### 坑 1：become 权限问题——sudo 提示 TTY

症状：Playbook 中用了 `become: true`，执行时报错 `sudo: no tty present and no askpass program specified`。

原因：目标机器的 `/etc/sudoers` 里配置了 `Defaults requiretty`，要求 sudo 必须在终端中执行，而 Ansible 通过 SSH 的非交互式会话执行命令，没有 TTY。

解决方案：

```bash
# 方案1：在 sudoers 里为 ansible 用户关闭 requiretty
echo "ansible ALL=(ALL) NOPASSWD: ALL" | sudo tee /etc/sudoers.d/ansible
echo "Defaults:ansible !requiretty" | sudo tee -a /etc/sudoers.d/ansible

# 方案2：ansible.cfg 中设置 become_method
[privilege_escalation]
become_method = sudo
```

### 坑 2：SSH 连接超时，大批量执行卡住

症状：inventory 里有 200 台机器，执行 Playbook 时前几台很快，后来越来越慢，甚至有机器连不上。

原因：
1. 默认并发数（`forks`）只有 5，200 台机器要跑 40 批，速度慢是正常的
2. SSH 连接复用没有开启，每个 task 都重新建立 SSH 连接，开销大

优化 `ansible.cfg`：

```ini
[defaults]
forks = 50                    # 并发数调大
host_key_checking = False     # 避免首次连接的确认提示

[ssh_connection]
pipelining = True             # 减少 SSH 连接次数，显著提速
control_path_dir = /tmp/ansible-ssh
ssh_args = -o ControlMaster=auto -o ControlPersist=60s -o ConnectTimeout=10
```

`pipelining = True` 这个配置影响很大，开启后性能可以提升 2-3 倍，但需要目标机器的 sudoers 里没有 `requiretty`（前一个坑）。

### 坑 3：command 模块的幂等性陷阱

症状：Playbook 每次执行都显示所有 `command` task 为 changed，但实际上什么都没变。

原因：`command` 和 `shell` 模块本身不知道命令是否改变了什么，默认每次执行都报 changed。

解决：显式告诉 Ansible 什么情况算 changed：

```yaml
# 查询操作，永远不算 changed
- name: Get current timezone
  command: timedatectl show --property=Timezone
  register: tz_result
  changed_when: false

# 只有输出包含特定内容时才算 changed
- name: Initialize database
  command: /opt/scripts/init_db.sh
  register: init_result
  changed_when: "'Database initialized' in init_result.stdout"

# 配合 creates 参数实现幂等（文件存在时跳过）
- name: Initialize once
  command: /opt/scripts/one_time_setup.sh
  args:
    creates: /opt/.setup_done
```

### 坑 4：变量优先级踩坑

症状：明明在 `group_vars/all.yml` 里改了变量，但执行时还是用了旧值。

Ansible 变量有复杂的优先级体系（从低到高）：
1. `defaults/main.yml`（Role 默认值，最容易被覆盖）
2. `inventory/group_vars/all`
3. `inventory/group_vars/<group>`
4. `inventory/host_vars/<host>`
5. Playbook 中的 `vars:`
6. `vars_files:`
7. `--extra-vars`（命令行传入，最高优先级）

常见错误：在 Role 的 `vars/main.yml`（不是 `defaults/main.yml`）里定义了变量，`vars/` 目录的优先级比 `group_vars` 还高，导致外部传入的覆盖不生效。经验法则：**可配置的参数放 `defaults/`，不对外的内部常量才放 `vars/`**。
