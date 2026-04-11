---
title: "Linux 用户权限与安全管理"
date: 2025-12-09T10:00:00+08:00
draft: false
tags: ["Linux", "运维", "安全", "权限", "SSH"]
categories: ["Linux"]
description: "用户管理、文件权限、sudo 配置、SSH 安全加固、审计日志和 PAM 配置的完整实操手册"
summary: "从 useradd/usermod 用户管理到 SUID/SGID 特殊权限，从 sudoers 配置到 fail2ban 防暴力破解，覆盖 Linux 系统安全加固的核心操作。"
toc: true
math: false
diagram: false
keywords: ["用户管理", "chmod", "sudo", "SSH安全", "fail2ban", "PAM", "审计日志"]
params:
  reading_time: true
---

## 一、用户管理

### 1.1 useradd / adduser

```bash
# 创建用户
useradd appuser                          # 基础创建（无家目录，无 shell）
useradd -m -s /bin/bash appuser          # 创建家目录，指定 shell
useradd -m -s /bin/bash -G sudo,docker appuser  # 附加组
useradd -u 1500 -g 1500 appuser          # 指定 UID/GID
useradd -r -s /sbin/nologin appuser      # 系统用户（不能登录）
useradd -d /opt/appuser -m appuser       # 自定义家目录

# adduser（交互式，Debian/Ubuntu 推荐）
adduser appuser
```

### 1.2 usermod

```bash
usermod -aG docker appuser               # 追加附加组（-a 必须和 -G 配合，否则会覆盖）
usermod -G sudo,docker appuser           # 设置附加组（覆盖式）
usermod -g appgroup appuser              # 修改主组
usermod -s /bin/bash appuser            # 修改 shell
usermod -s /sbin/nologin appuser         # 禁止登录
usermod -d /new/home -m appuser          # 修改并移动家目录
usermod -l newname appuser              # 重命名用户
usermod -L appuser                       # 锁定账户（密码前加 !）
usermod -U appuser                       # 解锁账户
usermod -e 2025-12-31 appuser            # 设置账户过期日期
```

### 1.3 userdel

```bash
userdel appuser                          # 删除用户（保留家目录）
userdel -r appuser                       # 删除用户及其家目录和邮件
userdel -f appuser                       # 强制删除（即使当前登录）

# 删除前先查找该用户拥有的文件
find / -user appuser 2>/dev/null -ls
find / -uid 1500 2>/dev/null             # 用 UID 找（防止改名后遗漏）
```

### 1.4 passwd 密码管理

```bash
passwd appuser                           # 为用户设置密码
passwd -l appuser                        # 锁定账户
passwd -u appuser                        # 解锁账户
passwd -e appuser                        # 强制下次登录修改密码
passwd -d appuser                        # 清空密码（允许空密码登录，危险）
passwd --status appuser                  # 查看密码状态

# 修改密码策略（chage）
chage -l appuser                         # 查看密码有效期信息
chage -M 90 appuser                      # 密码最长90天有效
chage -m 7 appuser                       # 密码最短7天才能修改
chage -W 14 appuser                      # 过期前14天提醒
chage -E 2025-12-31 appuser              # 账户过期日期
```

### 1.5 /etc/passwd 和 /etc/shadow 结构

`/etc/passwd` 每行格式：
```
username:x:UID:GID:comment:home:shell
appuser:x:1001:1001:App User:/home/appuser:/bin/bash
```

| 字段 | 含义 |
|------|------|
| username | 用户名 |
| x | 密码占位（实际密码在 shadow 中）|
| UID | 用户 ID（0=root，1-999=系统用户，1000+=普通用户）|
| GID | 主组 ID |
| comment | 注释（GECOS 字段）|
| home | 家目录 |
| shell | 登录 shell |

`/etc/shadow` 每行格式：
```
username:$6$salt$hash:lastchange:min:max:warn:inactive:expire:reserved
```

| 字段 | 含义 |
|------|------|
| 加密密码 | `$6$`=SHA-512，`$5$`=SHA-256，`$1$`=MD5，`!`或`*`=锁定 |
| lastchange | 上次修改密码距1970-01-01的天数 |
| min | 最短使用天数 |
| max | 最长使用天数 |
| warn | 提前警告天数 |
| inactive | 过期后宽限天数 |
| expire | 账户过期日期（天数）|

---

## 二、组管理

```bash
# 组操作
groupadd appgroup                        # 创建组
groupadd -g 2000 appgroup               # 指定 GID
groupmod -n newgroup appgroup           # 重命名组
groupdel appgroup                        # 删除组（先移除所有成员）

# 查看用户所属组
id appuser                               # UID/GID/所有组
groups appuser                           # 只显示组名
cat /etc/group | grep appuser            # 在 /etc/group 中查

# /etc/group 格式
# groupname:x:GID:member1,member2
cat /etc/group | grep docker

# gpasswd 管理组成员
gpasswd -a appuser docker               # 添加到组
gpasswd -d appuser docker               # 从组移除
gpasswd -M user1,user2 appgroup         # 设置组成员（覆盖式）
gpasswd -A appuser appgroup             # 设置组管理员
```

---

## 三、文件权限

### 3.1 权限数字表示

权限由三组三位二进制组成：`所有者(u) | 所属组(g) | 其他用户(o)`

| 权限 | 数字 | 文件含义 | 目录含义 |
|------|------|----------|----------|
| r | 4 | 可读 | 可列目录 |
| w | 2 | 可写 | 可在目录中增删文件 |
| x | 1 | 可执行 | 可进入目录 |

```bash
# 示例
chmod 755 /opt/myapp      # rwxr-xr-x（所有者全权，组和其他可读可执行）
chmod 644 /etc/myapp.conf # rw-r--r--（所有者读写，其他只读）
chmod 600 ~/.ssh/id_rsa   # rw-------（只有所有者可读写）
chmod 700 ~/.ssh          # rwx------（只有所有者可进入）

# 符号方式
chmod u+x script.sh       # 所有者添加执行权限
chmod go-w /etc/app.conf  # 组和其他移除写权限
chmod a+r /var/log/app.log # 所有人添加读权限
chmod u=rwx,go=rx /opt/app # 精确设置

# 递归修改
chmod -R 755 /opt/myapp
```

### 3.2 chown / chgrp

```bash
chown appuser /opt/myapp          # 修改所有者
chown appuser:appgroup /opt/myapp # 修改所有者和组
chown :appgroup /opt/myapp        # 只改组（等同 chgrp）
chown -R appuser:appgroup /opt/myapp  # 递归修改

chgrp appgroup /opt/myapp
chgrp -R appgroup /opt/myapp
```

### 3.3 特殊权限

**SUID（Set User ID）**：以文件所有者权限执行（而非调用者）
```bash
chmod u+s /usr/bin/passwd     # 设置 SUID
chmod 4755 /usr/bin/myprogram  # 数字方式（4=SUID）
ls -l /usr/bin/passwd
# -rwsr-xr-x ... passwd    <- s 表示 SUID

# 查找系统中所有 SUID 文件（安全审计）
find / -perm -4000 -type f 2>/dev/null
```

**SGID（Set Group ID）**：执行时获得文件所属组权限；目录中创建的文件继承目录所属组
```bash
chmod g+s /shared/teamdir    # 设置 SGID 目录（团队共享目录必备）
chmod 2755 /shared/teamdir   # 数字方式（2=SGID）
ls -ld /shared/teamdir
# drwxrwsr-x ... teamdir    <- s 表示 SGID

# 查找 SGID 文件
find / -perm -2000 -type f 2>/dev/null
```

**Sticky Bit**：只有文件所有者和 root 才能删除目录中的文件（/tmp 经典用例）
```bash
chmod +t /shared/uploads     # 设置 Sticky Bit
chmod 1777 /tmp              # /tmp 的典型权限
ls -ld /tmp
# drwxrwxrwt ... tmp        <- t 表示 Sticky Bit
```

### 3.4 ACL（访问控制列表）

当标准权限无法满足需求（如多用户不同权限）时使用 ACL。

```bash
# 查看 ACL
getfacl /opt/myapp

# 给特定用户添加权限
setfacl -m u:devuser:rx /opt/myapp
setfacl -m g:devteam:rwx /opt/myapp

# 递归设置
setfacl -R -m u:devuser:rx /opt/myapp

# 设置默认 ACL（新创建的文件继承）
setfacl -d -m u:devuser:rx /opt/myapp

# 删除 ACL
setfacl -x u:devuser /opt/myapp
setfacl -b /opt/myapp           # 删除所有 ACL
```

---

## 四、sudo 配置

### 4.1 /etc/sudoers 结构

```bash
# 必须使用 visudo 编辑（防止语法错误锁死）
visudo
# 或
visudo -f /etc/sudoers.d/myconfig   # 在独立文件中配置（推荐）
```

```bash
# /etc/sudoers 语法
# user/group  host=(run_as_user:run_as_group)  commands

# 给 appuser 所有权限（等同 root）
appuser  ALL=(ALL:ALL)  ALL

# 无需密码执行 systemctl
appuser  ALL=(ALL)  NOPASSWD: /bin/systemctl

# 只允许重启 nginx
appuser  ALL=(ALL)  NOPASSWD: /bin/systemctl restart nginx

# 允许组管理服务
%sysops  ALL=(ALL)  NOPASSWD: /bin/systemctl, /usr/sbin/service

# 使用别名简化
Cmnd_Alias  SERVICES = /bin/systemctl start *, /bin/systemctl stop *, /bin/systemctl restart *
Cmnd_Alias  NETWORK  = /sbin/ip, /sbin/iptables
appuser  ALL=(ALL)  NOPASSWD: SERVICES, NETWORK

# 禁止特定命令（防绕过）
appuser  ALL=(ALL)  ALL, !/bin/bash, !/bin/sh, !/usr/bin/vi
```

### 4.2 独立配置文件（推荐）

```bash
# 在 /etc/sudoers.d/ 下创建独立文件
cat > /etc/sudoers.d/appteam << 'EOF'
# App team deployment permissions
%appteam  ALL=(ALL)  NOPASSWD: /bin/systemctl restart myapp, \
                                /bin/systemctl start myapp, \
                                /bin/systemctl stop myapp
EOF
chmod 440 /etc/sudoers.d/appteam
visudo -c                          # 检查语法
```

### 4.3 sudo 日志

```bash
# 默认日志位置
tail -f /var/log/auth.log          # Debian/Ubuntu
tail -f /var/log/secure            # RHEL/CentOS

# 搜索 sudo 操作
grep sudo /var/log/auth.log | grep -v "pam_unix"

# 日志格式示例
# Dec  9 10:30:00 server sudo: appuser : TTY=pts/0 ; PWD=/home/appuser ; USER=root ; COMMAND=/bin/systemctl restart nginx

# 配置 sudo 日志（在 sudoers 中）
Defaults  logfile="/var/log/sudo.log"
Defaults  log_year
Defaults  log_host
```

---

## 五、SSH 安全加固

### 5.1 禁用 root 登录

```bash
# /etc/ssh/sshd_config
PermitRootLogin no              # 禁止 root 登录（推荐）
# PermitRootLogin prohibit-password  # 只禁止密码，允许密钥
# PermitRootLogin forced-commands-only  # 只允许指定命令

# 修改后重载
systemctl reload sshd
```

### 5.2 密钥认证配置

```bash
# 生成密钥对（客户端执行）
ssh-keygen -t ed25519 -C "user@example.com"      # 推荐 ed25519
ssh-keygen -t rsa -b 4096 -C "user@example.com"  # 兼容性更好

# 分发公钥到服务器
ssh-copy-id -i ~/.ssh/id_ed25519.pub user@server
# 或手动追加
cat ~/.ssh/id_ed25519.pub >> ~/.ssh/authorized_keys
chmod 700 ~/.ssh
chmod 600 ~/.ssh/authorized_keys

# /etc/ssh/sshd_config 密钥相关配置
PubkeyAuthentication yes
AuthorizedKeysFile  .ssh/authorized_keys
PasswordAuthentication no          # 禁用密码登录（确认密钥可用后再改）
ChallengeResponseAuthentication no
```

### 5.3 其他加固配置

```bash
# /etc/ssh/sshd_config 加固选项
Port 22222                         # 修改默认端口（减少扫描干扰）
ListenAddress 0.0.0.0

# 允许/拒绝特定用户
AllowUsers appuser deploy          # 白名单（只允许这些用户）
AllowGroups sshusers               # 允许组
DenyUsers nobody                   # 黑名单

# 超时和连接限制
LoginGraceTime 30                  # 30秒内未完成登录则断开
MaxAuthTries 3                     # 最多尝试3次认证
MaxSessions 10                     # 最多10个并发会话
ClientAliveInterval 300            # 5分钟无活动检测
ClientAliveCountMax 2              # 2次无响应后断开

# 禁用不安全功能
X11Forwarding no
AllowTcpForwarding no              # 禁止端口转发（必要时开启）
AllowAgentForwarding no
PermitEmptyPasswords no
UseDNS no                          # 不做 DNS 反向解析（加快连接速度）

# 加密算法限制（仅允许强算法）
KexAlgorithms curve25519-sha256,diffie-hellman-group16-sha512
Ciphers chacha20-poly1305@openssh.com,aes256-gcm@openssh.com
MACs hmac-sha2-512-etm@openssh.com,hmac-sha2-256-etm@openssh.com
```

### 5.4 fail2ban 防暴力破解

```bash
# 安装
apt install -y fail2ban            # Debian/Ubuntu
yum install -y fail2ban            # RHEL/CentOS

# 配置（/etc/fail2ban/jail.local，不要改 jail.conf）
cat > /etc/fail2ban/jail.local << 'EOF'
[DEFAULT]
bantime  = 3600         # 封禁1小时
findtime  = 600         # 10分钟内
maxretry = 5            # 失败5次封禁
ignoreip = 127.0.0.1/8 10.0.0.0/8  # 白名单

[sshd]
enabled = true
port    = 22
filter  = sshd
logpath = /var/log/auth.log
maxretry = 3            # SSH 更严格，3次就封禁
EOF

systemctl enable --now fail2ban

# 查看封禁状态
fail2ban-client status
fail2ban-client status sshd

# 手动解封
fail2ban-client set sshd unbanip 1.2.3.4

# 手动封禁
fail2ban-client set sshd banip 1.2.3.4
```

---

## 六、审计日志

### 6.1 登录记录查看

```bash
# 查看当前登录用户
who
w                                  # 更详细，包含空闲时间和执行命令

# 历史登录记录（读 /var/log/wtmp）
last
last -n 20                         # 最近20条
last appuser                       # 特定用户
last reboot                        # 重启记录

# 登录失败记录（读 /var/log/btmp）
lastb
lastb -n 20 | head -30

# 最近一次登录（读 /var/log/lastlog）
lastlog
lastlog -u appuser

# 实时查看认证日志
tail -f /var/log/auth.log          # Debian/Ubuntu
tail -f /var/log/secure            # RHEL/CentOS

# 统计 SSH 登录失败 IP
grep "Failed password" /var/log/auth.log | \
  awk '{print $(NF-3)}' | sort | uniq -c | sort -rn | head -20
```

### 6.2 auditd 系统审计

```bash
# 安装
apt install -y auditd
yum install -y audit
systemctl enable --now auditd

# 添加审计规则
auditctl -l                        # 查看当前规则
auditctl -w /etc/passwd -p wa -k passwd_changes  # 监控 /etc/passwd 写操作
auditctl -w /etc/sudoers -p rwa    # 监控 sudoers 读写追加
auditctl -a always,exit -F arch=b64 -S execve -k exec_log  # 记录所有命令执行

# 查询审计日志
ausearch -k passwd_changes         # 按规则键查询
ausearch -m USER_LOGIN             # 按消息类型查询
ausearch -ui 1001                  # 按 UID 查询
ausearch -ts today                 # 今天的记录
ausearch -ts recent -k exec_log | aureport -x  # 最近执行的命令

# 持久化规则（/etc/audit/rules.d/audit.rules）
cat >> /etc/audit/rules.d/audit.rules << 'EOF'
-w /etc/passwd -p wa -k identity
-w /etc/shadow -p wa -k identity
-w /etc/sudoers -p rwa -k sudoers
-w /var/log/auth.log -p wa -k auth_log
-a always,exit -F arch=b64 -S execve -k exec
EOF
service auditd restart
```

---

## 七、PAM 简介

PAM（Pluggable Authentication Modules）是 Linux 认证框架，控制用户认证、账户、会话和密码策略。

```bash
# PAM 配置目录
ls /etc/pam.d/

# 常见服务配置文件
# /etc/pam.d/sshd       SSH 登录认证
# /etc/pam.d/sudo       sudo 认证
# /etc/pam.d/login      本地登录
# /etc/pam.d/common-*  Debian/Ubuntu 公共配置
```

PAM 配置行格式：`类型  控制标志  模块  参数`

| 类型 | 含义 |
|------|------|
| auth | 认证（验证身份）|
| account | 账户管理（过期、限制等）|
| password | 密码策略 |
| session | 会话管理（登录/登出操作）|

| 控制标志 | 含义 |
|----------|------|
| required | 必须成功，失败继续执行后续模块但最终拒绝 |
| requisite | 必须成功，失败立即拒绝 |
| sufficient | 成功即通过，失败继续 |
| optional | 可选，不影响最终结果 |

```bash
# 常用 PAM 模块
# pam_unix.so         标准 Unix 密码认证
# pam_limits.so       ulimit 资源限制
# pam_env.so          设置环境变量
# pam_google_authenticator.so  Google 二次验证
# pam_time.so         基于时间的访问控制
# pam_access.so       基于 /etc/security/access.conf 的访问控制

# 示例：配置密码复杂度（Debian/Ubuntu）
# /etc/pam.d/common-password
# password requisite pam_pwquality.so retry=3 minlen=12 dcredit=-1 ucredit=-1 ocredit=-1 lcredit=-1

# 安装 pwquality
apt install -y libpam-pwquality

# 配置密码策略
cat > /etc/security/pwquality.conf << 'EOF'
minlen = 12         # 最短12位
dcredit = -1        # 至少1个数字
ucredit = -1        # 至少1个大写
lcredit = -1        # 至少1个小写
ocredit = -1        # 至少1个特殊字符
maxrepeat = 3       # 同一字符最多重复3次
EOF
```

---

## 八、安全检查清单

```bash
# 1. 找出空密码账户
awk -F: '($2 == "" ) {print $1}' /etc/shadow

# 2. 找出 UID 为 0 的账户（只应有 root）
awk -F: '($3 == 0) {print $1}' /etc/passwd

# 3. 找出可登录的系统账户
awk -F: '($3 < 1000 && $7 != "/sbin/nologin" && $7 != "/usr/sbin/nologin" && $7 != "/bin/false") {print $1, $7}' /etc/passwd

# 4. 找出全球可写目录
find / -type d -perm -0002 -not -path "/proc/*" 2>/dev/null

# 5. 找出无主文件
find / -nouser -o -nogroup 2>/dev/null | grep -v "^/proc"

# 6. 检查 crontab（各用户）
for user in $(cut -f1 -d: /etc/passwd); do
  crontab -l -u $user 2>/dev/null | grep -v "^#" | grep -v "^$" | \
    awk -v u=$user '{print u": "$0}'
done

# 7. 检查监听端口
ss -tlnp
# 对比已知应监听的端口，发现异常端口

# 8. 检查 /etc/hosts.allow 和 /etc/hosts.deny
cat /etc/hosts.allow
cat /etc/hosts.deny
```
