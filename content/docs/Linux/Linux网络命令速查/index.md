---
title: "Linux 网络命令速查"
date: 2025-12-09T10:00:00+08:00
draft: false
tags: ["Linux", "运维", "网络", "tcpdump", "iptables"]
categories: ["Linux"]
description: "ss/ip/iptables/tcpdump/curl/dig/mtr 等网络命令全面速查，覆盖日常排障和抓包分析场景"
summary: "系统整理 Linux 网络排查工具链，包含 ss 连接状态过滤、tcpdump 过滤语法、iptables NAT 配置、curl 响应时间分析及 DNS 工具使用方法。"
toc: true
math: false
diagram: false
keywords: ["ss命令", "tcpdump", "iptables", "curl", "dig", "网络排查"]
params:
  reading_time: true
---

## 一、ss / netstat 连接状态查看

### 1.1 ss 基础用法

`ss` 是 `netstat` 的现代替代品，速度更快，信息更丰富。

```bash
ss -tlnp          # 监听中的 TCP 端口及进程（最常用）
ss -ulnp          # 监听中的 UDP 端口
ss -antp          # 所有 TCP 连接含进程信息
ss -s             # 汇总统计（各状态数量）
ss -i             # 显示 TCP 内部信息（rtt/retrans等）
```

### 1.2 连接状态过滤

```bash
# 只看 ESTABLISHED
ss -ant state established

# 只看 TIME-WAIT
ss -ant state time-wait

# 只看 LISTEN
ss -ant state listening

# 多状态组合
ss -ant '( state established or state time-wait )'
```

TCP 状态含义速查：

| 状态 | 含义 |
|------|------|
| LISTEN | 本端在监听，等待连接 |
| SYN-SENT | 已发送 SYN，等待对端回复 |
| SYN-RECV | 收到 SYN，已回复 SYN-ACK |
| ESTABLISHED | 连接已建立 |
| FIN-WAIT-1 | 主动关闭方，已发 FIN |
| FIN-WAIT-2 | 等待对端 FIN |
| TIME-WAIT | 等待 2MSL，防止最后 ACK 丢失 |
| CLOSE-WAIT | 被动关闭方，收到 FIN 未关闭本端 |
| LAST-ACK | 被动关闭方，已发 FIN，等 ACK |
| CLOSED | 连接关闭 |

### 1.3 按端口过滤

```bash
# 本端端口
ss -ant '( sport = :80 )'
ss -ant '( sport = :80 or sport = :443 )'

# 对端端口
ss -ant '( dport = :3306 )'

# 目标 IP
ss -ant dst 192.168.1.100

# 源 IP
ss -ant src 10.0.0.5
```

### 1.4 按进程过滤

```bash
# 看 nginx 的连接
ss -antp | grep nginx

# 看指定 PID 的连接
ss -antp | grep "pid=1234"

# 统计各进程连接数
ss -antp | grep -oP 'users:\(\("\K[^"]+' | sort | uniq -c | sort -rn
```

### 1.5 netstat 兼容命令

```bash
netstat -tlnp          # 与 ss -tlnp 功能相同
netstat -s             # 协议统计（含 TCP 重传、错误等）
netstat -r             # 路由表
netstat -i             # 网卡统计
```

---

## 二、ip 命令

### 2.1 地址管理

```bash
ip addr show                         # 查看所有网卡地址（简写 ip a）
ip addr show eth0                    # 只看 eth0
ip addr add 192.168.1.100/24 dev eth0  # 添加 IP
ip addr del 192.168.1.100/24 dev eth0  # 删除 IP
ip addr flush dev eth0               # 清空网卡所有 IP
```

### 2.2 路由管理

```bash
ip route show                        # 查看路由表（简写 ip r）
ip route show table all              # 包含所有路由表
ip route add default via 192.168.1.1  # 添加默认网关
ip route add 10.0.0.0/8 via 172.16.0.1 dev eth1  # 添加静态路由
ip route del 10.0.0.0/8              # 删除路由
ip route get 8.8.8.8                 # 查询到达目标的出接口和网关

# 策略路由
ip rule show                         # 查看路由策略
ip rule add from 192.168.1.0/24 lookup 100  # 添加策略
ip route add default via 10.0.0.1 table 100  # 在表100中添加路由
```

### 2.3 link 管理

```bash
ip link show                         # 查看所有网卡状态（简写 ip l）
ip link set eth0 up                  # 启用网卡
ip link set eth0 down                # 禁用网卡
ip link set eth0 mtu 9000            # 设置 MTU
ip link set eth0 txqueuelen 10000    # 设置发送队列长度
ip -s link show eth0                 # 显示收发包统计
```

### 2.4 邻居表（ARP）

```bash
ip neigh show                        # 查看 ARP 表
ip neigh del 192.168.1.1 dev eth0   # 删除 ARP 条目
ip neigh flush dev eth0              # 清空 ARP 表
```

---

## 三、iptables 基础

### 3.1 查看规则

```bash
iptables -L -n -v            # 查看 filter 表所有规则（-n不解析DNS，-v显示计数）
iptables -L -n -v --line-numbers  # 带行号
iptables -t nat -L -n -v     # 查看 nat 表
iptables -t mangle -L -n -v  # 查看 mangle 表

# 保存规则
iptables-save > /etc/iptables/rules.v4
iptables-restore < /etc/iptables/rules.v4
```

### 3.2 添加与删除规则

```bash
# 放行
iptables -A INPUT -p tcp --dport 80 -j ACCEPT
iptables -A INPUT -s 192.168.1.0/24 -j ACCEPT
iptables -I INPUT 1 -p tcp --dport 22 -j ACCEPT  # 插入到第1行

# 拒绝
iptables -A INPUT -p tcp --dport 3306 -j DROP
iptables -A INPUT -p tcp --dport 3306 -j REJECT --reject-with tcp-reset

# 删除规则（按行号）
iptables -D INPUT 3

# 清空链
iptables -F INPUT
iptables -F             # 清空所有链

# 设置默认策略
iptables -P INPUT DROP
iptables -P FORWARD DROP
iptables -P OUTPUT ACCEPT
```

### 3.3 NAT 配置

```bash
# SNAT（出口 IP 固定）
iptables -t nat -A POSTROUTING -s 10.0.0.0/8 -o eth0 -j SNAT --to-source 1.2.3.4

# MASQUERADE（出口 IP 动态，适合 DHCP 场景）
iptables -t nat -A POSTROUTING -s 10.0.0.0/8 -o eth0 -j MASQUERADE

# DNAT（端口转发）
iptables -t nat -A PREROUTING -p tcp --dport 8080 -j DNAT --to-destination 192.168.1.10:80

# 开启 IP 转发
sysctl -w net.ipv4.ip_forward=1
echo "net.ipv4.ip_forward = 1" >> /etc/sysctl.conf

# 限速（每秒最多60个新连接）
iptables -A INPUT -p tcp --dport 80 -m limit --limit 60/s --limit-burst 100 -j ACCEPT
iptables -A INPUT -p tcp --dport 80 -j DROP
```

### 3.4 连接跟踪

```bash
# 查看 conntrack 表
conntrack -L
conntrack -L | wc -l    # 当前连接跟踪数

# 查看 conntrack 最大值和当前值
sysctl net.netfilter.nf_conntrack_max
sysctl net.netfilter.nf_conntrack_count

# 清空 conntrack 表（慎用）
conntrack -F
```

---

## 四、tcpdump 抓包

### 4.1 基础过滤语法

```bash
# 抓指定网卡
tcpdump -i eth0

# 过滤主机
tcpdump host 192.168.1.1
tcpdump src host 192.168.1.1
tcpdump dst host 192.168.1.1

# 过滤端口
tcpdump port 80
tcpdump port 80 or port 443
tcpdump portrange 8080-8090

# 过滤协议
tcpdump tcp
tcpdump udp
tcpdump icmp

# 组合过滤
tcpdump -i eth0 host 1.2.3.4 and tcp port 443
tcpdump -i eth0 'tcp[tcpflags] & tcp-syn != 0'   # SYN 包
tcpdump -i eth0 'tcp[tcpflags] == tcp-rst'        # RST 包
```

### 4.2 抓包写文件

```bash
# 写入文件（-w）
tcpdump -i eth0 -w /tmp/capture.pcap

# 限制文件大小和数量（按 100MB 滚动，最多5个文件）
tcpdump -i eth0 -w /tmp/cap.pcap -C 100 -W 5

# 限制抓包时间（60秒后停止）
timeout 60 tcpdump -i eth0 -w /tmp/cap.pcap

# 抓包数量限制
tcpdump -i eth0 -c 1000 -w /tmp/cap.pcap

# 读取 pcap 文件分析
tcpdump -r /tmp/capture.pcap -n
tcpdump -r /tmp/capture.pcap -n 'port 80'
```

### 4.3 常用场景

```bash
# 抓 HTTP 请求（非加密）
tcpdump -i eth0 -A -s 0 'tcp port 80 and (tcp[((tcp[12:1] & 0xf0) >> 2):4] = 0x47455420)'

# 抓 DNS 查询
tcpdump -i eth0 udp port 53 -n

# 抓 ICMP
tcpdump -i eth0 icmp -n

# 抓某个进程的流量（需要 strace 配合找 socket fd，或用 nsenter）

# 显示详细输出（-v -vv -vvv）
tcpdump -i eth0 -vv port 443

# 不解析主机名（-n）和端口名（-nn）
tcpdump -i eth0 -nn port 80
```

---

## 五、curl 高级用法

### 5.1 响应时间分析

```bash
# 创建时间分析格式文件
cat > /tmp/curl-format.txt << 'EOF'
    time_namelookup:  %{time_namelookup}s\n
       time_connect:  %{time_connect}s\n
    time_appconnect:  %{time_appconnect}s\n
   time_pretransfer:  %{time_pretransfer}s\n
      time_redirect:  %{time_redirect}s\n
 time_starttransfer:  %{time_starttransfer}s\n
                    ----------\n
         time_total:  %{time_total}s\n
EOF

curl -w "@/tmp/curl-format.txt" -o /dev/null -s https://example.com
```

| 字段 | 含义 |
|------|------|
| time_namelookup | DNS 解析耗时 |
| time_connect | TCP 连接建立耗时 |
| time_appconnect | TLS 握手耗时（仅 HTTPS）|
| time_pretransfer | 准备传输耗时 |
| time_starttransfer | 首字节到达耗时（TTFB）|
| time_total | 总耗时 |

### 5.2 证书与 TLS

```bash
# 查看证书信息
curl -vI https://example.com 2>&1 | grep -A 20 "Server certificate"

# 忽略证书错误（测试用）
curl -k https://example.com

# 指定 CA 证书
curl --cacert /path/to/ca.crt https://example.com

# 客户端证书认证
curl --cert client.crt --key client.key https://example.com

# 指定 TLS 版本
curl --tlsv1.2 https://example.com
```

### 5.3 代理与绕过

```bash
# 使用 HTTP 代理
curl -x http://proxy:8080 https://example.com

# 使用 SOCKS5 代理
curl --socks5 127.0.0.1:1080 https://example.com

# 绕过代理（no_proxy）
curl --noproxy "*.internal.com" https://service.internal.com

# 直接指定 IP（绕过 DNS，测试特定服务器）
curl --resolve example.com:443:1.2.3.4 https://example.com
```

### 5.4 请求构造

```bash
# POST JSON
curl -X POST https://api.example.com/v1/data \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer TOKEN" \
  -d '{"key": "value"}'

# 上传文件
curl -F "file=@/path/to/file.txt" https://upload.example.com

# 跟随重定向
curl -L https://example.com

# 保存响应头
curl -D /tmp/headers.txt https://example.com -o /dev/null

# 限速（测试网络质量）
curl --limit-rate 1M https://example.com -o /dev/null
```

### 5.5 并发测试

```bash
# 简单并发（shell 循环）
for i in $(seq 1 20); do
  curl -s -o /dev/null -w "%{http_code}\n" https://example.com &
done
wait

# 更精准的并发工具
ab -n 1000 -c 50 https://example.com/
wrk -t4 -c100 -d30s https://example.com/
```

---

## 六、DNS 排查

### 6.1 dig

```bash
dig example.com                    # 查 A 记录
dig example.com AAAA               # 查 AAAA（IPv6）
dig example.com MX                 # 查邮件服务器
dig example.com TXT                # 查 TXT 记录
dig example.com NS                 # 查权威 DNS
dig example.com SOA                # 查 SOA 记录

# 指定 DNS 服务器查询
dig @8.8.8.8 example.com
dig @1.1.1.1 example.com

# 追踪查询路径
dig +trace example.com

# 简洁输出
dig +short example.com

# 反向解析
dig -x 93.184.216.34
dig +short -x 93.184.216.34

# 查询时间统计
dig example.com | grep "Query time"

# 禁用递归（直接问权威 DNS）
dig +norecurse @ns1.example.com example.com
```

### 6.2 nslookup

```bash
nslookup example.com
nslookup example.com 8.8.8.8      # 指定 DNS 服务器
nslookup -type=MX example.com     # 查 MX 记录
nslookup -type=TXT example.com
nslookup -debug example.com        # 调试模式
```

### 6.3 host

```bash
host example.com
host example.com 8.8.8.8
host -t MX example.com
host -a example.com                # 查所有记录
host 93.184.216.34                 # 反向解析
```

### 6.4 DNS 故障排查流程

```bash
# 1. 确认本地 DNS 配置
cat /etc/resolv.conf
systemd-resolve --status | grep "DNS Servers"

# 2. 测试本地 DNS 是否可达
dig @$(awk '/^nameserver/{print $2;exit}' /etc/resolv.conf) example.com

# 3. 对比公共 DNS 结果
diff <(dig +short example.com @8.8.8.8) <(dig +short example.com @1.1.1.1)

# 4. 检查 /etc/hosts 是否有覆盖
grep example.com /etc/hosts

# 5. 查看 nsswitch 解析顺序
grep ^hosts /etc/nsswitch.conf
```

---

## 七、连通性测试

### 7.1 ping

```bash
ping -c 4 example.com              # 发4个包后退出
ping -i 0.2 -c 20 example.com     # 间隔0.2秒，发20个
ping -s 1400 -c 10 example.com    # 1400字节包（测试 MTU）
ping -M do -s 1472 192.168.1.1    # 禁止分片，测试 MTU（本地段）
ping6 ::1                          # IPv6 ping
```

### 7.2 traceroute / tracepath

```bash
traceroute example.com             # 默认 UDP
traceroute -T -p 80 example.com   # TCP 模式，适合穿越防火墙
traceroute -I example.com          # ICMP 模式
traceroute -n example.com          # 不解析主机名
tracepath example.com              # 不需要 root，自动探测 MTU
```

### 7.3 mtr（推荐替代 traceroute）

```bash
mtr example.com                    # 交互模式
mtr -n --report -c 20 example.com # 不解析 DNS，报告模式，发20个包
mtr -T -P 443 example.com         # TCP 模式
mtr --json example.com            # JSON 输出（便于自动化）
```

mtr 输出列含义：

| 列 | 含义 |
|----|------|
| Loss% | 丢包率 |
| Snt | 已发送包数 |
| Avg | 平均 RTT |
| Best | 最小 RTT |
| Wrst | 最大 RTT |
| StDev | 标准差（越大抖动越严重）|

### 7.4 nc（netcat）端口测试

```bash
# TCP 端口连通性测试
nc -zv 192.168.1.1 80
nc -zv 192.168.1.1 80-100          # 扫描端口范围
nc -w 3 -zv 192.168.1.1 3306       # 3秒超时

# UDP 端口测试
nc -u -zv 192.168.1.1 53

# 简单监听（临时 TCP 服务器）
nc -l 8888

# 发送数据
echo "hello" | nc 192.168.1.1 8888

# 文件传输（配合管道）
# 接收端
nc -l 9999 > received.tar.gz
# 发送端
tar czf - /data | nc 192.168.1.1 9999
```

---

## 八、组合排查场景

```bash
# 场景1：某端口无响应，快速排查
nc -zv target 8080
ss -tlnp | grep 8080               # 本机是否监听
iptables -L -n | grep 8080         # 防火墙是否拦截
curl -v http://target:8080 2>&1 | head -20

# 场景2：DNS 解析慢
time curl -o /dev/null -s https://example.com  # 总时间
time dig example.com                            # DNS 时间

# 场景3：网络抖动排查
mtr -n --report -c 100 gateway_ip  # 对比各跳丢包
ping -i 0.1 -c 100 gateway_ip | tail -3  # 看 min/avg/max

# 场景4：抓取 HTTP 响应码统计
tcpdump -i eth0 -A 'tcp port 80' 2>/dev/null | \
  grep -oP 'HTTP/1\.[01] \K[0-9]+' | sort | uniq -c

# 场景5：找出连接数异常的 IP（防 DDoS 检查）
ss -ant state established | awk '{print $5}' | \
  grep -v '^[^0-9]' | cut -d: -f1 | sort | uniq -c | \
  sort -rn | head -20
```
