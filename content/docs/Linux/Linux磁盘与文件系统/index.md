---
title: "Linux 磁盘与文件系统管理"
date: 2025-12-09T10:00:00+08:00
draft: false
tags: ["Linux", "运维", "磁盘", "LVM", "文件系统"]
categories: ["Linux"]
description: "覆盖分区、文件系统、LVM 卷管理、磁盘性能测试与故障恢复的完整操作手册"
summary: "从 fdisk 分区到 LVM 扩容快照，从 ext4 vs xfs 对比到 fsck 故障恢复，以及 /proc 和 /sys 中与存储相关的关键路径速查。"
toc: true
math: false
diagram: false
keywords: ["LVM", "fdisk", "parted", "ext4", "xfs", "fsck", "fio", "磁盘管理"]
params:
  reading_time: true
---

## 一、分区管理

### 1.1 查看磁盘与分区

```bash
lsblk                          # 树形显示块设备
lsblk -f                       # 同时显示文件系统类型和挂载点
lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,UUID

fdisk -l                       # 列出所有磁盘分区表
fdisk -l /dev/sdb              # 只看 sdb

parted -l                      # 支持 GPT 和 MBR

blkid                          # 查看所有块设备的 UUID 和类型
blkid /dev/sdb1
```

### 1.2 fdisk（MBR 分区，适合 <2TB）

```bash
fdisk /dev/sdb

# 交互命令
# m    显示帮助
# p    打印当前分区表
# n    新建分区（p主分区，e扩展分区）
# d    删除分区
# t    修改分区类型（8e=Linux LVM，82=swap，83=Linux）
# w    写入并退出
# q    不保存退出

# 非交互创建分区（脚本用）
echo -e "n\np\n1\n\n+50G\nw" | fdisk /dev/sdb

# 通知内核重读分区表
partprobe /dev/sdb
# 或
partx -u /dev/sdb
```

### 1.3 parted（支持 GPT，适合 >2TB）

```bash
parted /dev/sdc

# 交互命令
# print                  查看分区表
# mklabel gpt            创建 GPT 分区表（会清空数据）
# mkpart primary ext4 0% 100%   用整块盘创建一个分区
# mkpart primary ext4 0 500GB   指定大小
# rm 1                   删除第1个分区
# quit

# 非交互操作
parted -s /dev/sdc mklabel gpt
parted -s /dev/sdc mkpart primary ext4 0% 100%
parted -s /dev/sdc align-check optimal 1   # 检查分区对齐
```

---

## 二、文件系统

### 2.1 创建文件系统

```bash
# ext4
mkfs.ext4 /dev/sdb1
mkfs.ext4 -L mydata /dev/sdb1          # 带卷标
mkfs.ext4 -b 4096 /dev/sdb1            # 指定块大小

# xfs
mkfs.xfs /dev/sdb1
mkfs.xfs -L mydata /dev/sdb1
mkfs.xfs -f /dev/sdb1                   # 强制覆盖已有文件系统

# swap
mkswap /dev/sdb2
swapon /dev/sdb2
swapon -a                               # 启用 fstab 中所有 swap
swapoff /dev/sdb2
```

### 2.2 ext4 vs xfs 对比

| 特性 | ext4 | xfs |
|------|------|-----|
| 最大文件系统大小 | 1 EB | 8 EB |
| 最大单文件大小 | 16 TB | 8 EB |
| 碎片整理 | `e4defrag` 支持 | 不支持在线整理 |
| 在线扩容 | `resize2fs` | `xfs_growfs` |
| 在线缩容 | 不支持（需卸载）| 不支持 |
| 日志模式 | data/ordered/writeback | 只有 writeback |
| 延迟分配 | 支持 | 支持 |
| 大目录性能 | 一般 | 优秀（B+树索引）|
| 适合场景 | 通用、小文件多 | 大文件、高并发 IO |
| RHEL/CentOS 默认 | CentOS 6 | CentOS 7+ |

### 2.3 挂载

```bash
mount /dev/sdb1 /data                   # 挂载
mount -t xfs /dev/sdb1 /data            # 指定文件系统类型
mount -o ro /dev/sdb1 /mnt              # 只读挂载
mount -o remount,rw /data               # 重新挂载为读写
mount -o noatime,nodiratime /dev/sdb1 /data  # 禁用访问时间（提升性能）

umount /data
umount -l /data                         # 懒卸载（等待无进程使用）
umount -f /data                         # 强制卸载（NFS 断连时用）

# 查看谁在占用（卸载报 busy 时）
fuser -mv /data
lsof +D /data
```

### 2.4 /etc/fstab 配置

```bash
# 格式：设备  挂载点  类型  选项  dump  fsck顺序
UUID=xxxxxxxx  /data  ext4  defaults,noatime  0  2
/dev/sdb1      /data  xfs   defaults          0  0

# 常用挂载选项说明
# defaults    = rw,suid,dev,exec,auto,nouser,async
# noatime     不更新访问时间（减少写 IO）
# nofail      挂载失败不阻止系统启动（云盘常用）
# ro          只读
# noexec      禁止执行文件（安全加固）
# nosuid      禁止 SUID 位（安全加固）
# _netdev     网络文件系统，等网络就绪后挂载

# 验证 fstab（不实际挂载）
mount -a --fake

# 查看当前挂载
cat /proc/mounts
findmnt                                 # 更美观的输出
findmnt --target /data
```

---

## 三、LVM 卷管理

### 3.1 创建 LVM

```bash
# 第一步：创建物理卷（PV）
pvcreate /dev/sdb1 /dev/sdc1
pvs                                     # 查看 PV
pvdisplay /dev/sdb1

# 第二步：创建卷组（VG）
vgcreate vg_data /dev/sdb1 /dev/sdc1
vgs                                     # 查看 VG
vgdisplay vg_data

# 第三步：创建逻辑卷（LV）
lvcreate -L 100G -n lv_app vg_data     # 指定大小
lvcreate -l 100%FREE -n lv_app vg_data  # 使用全部空闲空间
lvs                                     # 查看 LV
lvdisplay /dev/vg_data/lv_app

# 第四步：格式化并挂载
mkfs.ext4 /dev/vg_data/lv_app
mount /dev/vg_data/lv_app /app
```

### 3.2 LV 扩容

```bash
# 方法1：先扩 LV 再扩文件系统（两步）
lvextend -L +50G /dev/vg_data/lv_app
resize2fs /dev/vg_data/lv_app          # ext4
xfs_growfs /app                         # xfs（需要已挂载，参数是挂载点）

# 方法2：一步完成（ext4 专用）
lvextend -L +50G -r /dev/vg_data/lv_app  # -r 自动 resize 文件系统

# 如果 VG 空间不足，先扩 VG（新增磁盘）
pvcreate /dev/sdd
vgextend vg_data /dev/sdd
vgdisplay vg_data | grep "Free PE"
```

### 3.3 LVM 快照

```bash
# 创建快照（需要预留一定空间用于 COW）
lvcreate -L 10G -s -n lv_app_snap /dev/vg_data/lv_app

# 挂载快照（只读备份）
mount -o ro /dev/vg_data/lv_app_snap /mnt/snap

# 基于快照备份
mount -o ro /dev/vg_data/lv_app_snap /mnt/snap
tar czf /backup/app_$(date +%Y%m%d).tar.gz -C /mnt/snap .
umount /mnt/snap

# 快照回滚（危险，会覆盖数据）
umount /app
lvconvert --merge /dev/vg_data/lv_app_snap

# 删除快照
lvremove /dev/vg_data/lv_app_snap
```

### 3.4 LVM 常用管理命令

```bash
# 移除 LV（先卸载）
umount /app
lvremove /dev/vg_data/lv_app

# 缩容（ext4，需先卸载）
umount /app
e2fsck -f /dev/vg_data/lv_app          # 先做文件系统检查
resize2fs /dev/vg_data/lv_app 80G      # 缩小文件系统到80G
lvreduce -L 80G /dev/vg_data/lv_app    # 再缩 LV

# 重命名 LV
lvrename vg_data lv_app lv_application

# 查看 PE 使用情况
pvdisplay -m /dev/sdb1
```

---

## 四、磁盘使用分析

### 4.1 df 文件系统使用情况

```bash
df -h                                   # 人类可读
df -hT                                  # 包含文件系统类型
df -i                                   # inode 使用情况
df -h /data                             # 只看 /data 所在文件系统
df --exclude-type=tmpfs -h              # 排除 tmpfs
```

### 4.2 du 目录占用分析

```bash
du -sh /var/log                         # 目录总大小
du -sh /var/log/*  | sort -rh | head -20  # 各子目录大小排序
du -sh --max-depth=2 /var              # 限制递归深度
du -ah /var/log | sort -rh | head -20  # 找最大文件

# 找出超过100M的文件
find /var -type f -size +100M -exec ls -lh {} \; 2>/dev/null

# 排除某个目录
du -sh --exclude=/var/log/journal /var
```

### 4.3 ncdu 交互式分析

```bash
ncdu /                                  # 分析根目录（交互界面）
ncdu -x /                              # 不跨越文件系统边界
ncdu --exclude /proc --exclude /sys /  # 排除虚拟文件系统

# 导出结果（便于远程分析）
ncdu -o /tmp/ncdu.json /var
ncdu -f /tmp/ncdu.json                 # 读取之前的分析结果
```

### 4.4 查找大文件

```bash
# 找当前目录下最大的20个文件
find . -type f -printf '%s %p\n' | sort -rn | head -20 | \
  awk '{printf "%.1fMB %s\n", $1/1024/1024, $2}'

# 找最近7天修改的大文件
find /var -type f -mtime -7 -size +50M -ls 2>/dev/null

# 找孤立文件（有进程打开但已删除，占用磁盘空间却不可见）
lsof +L1 2>/dev/null | grep -v "^COMMAND"
# 如果 SIZE 很大，重启对应进程即可释放
```

---

## 五、磁盘性能测试

### 5.1 dd 基础测试

```bash
# 顺序写测试（不使用缓存）
dd if=/dev/zero of=/tmp/testfile bs=1M count=1000 oflag=direct
# 结果示例：1048576000 bytes (1.0 GB) copied, 2.5 s, 419 MB/s

# 顺序读测试
echo 3 > /proc/sys/vm/drop_caches       # 清空页缓存
dd if=/tmp/testfile of=/dev/null bs=1M iflag=direct

# 写入后清理
rm /tmp/testfile

# 注意：dd 测试顺序 IO，不代表随机 IO 能力
```

### 5.2 fio 专业测试

```bash
# 安装
apt install -y fio   # Debian/Ubuntu
yum install -y fio   # RHEL/CentOS

# 顺序写
fio --name=seqwrite --ioengine=libaio --iodepth=32 \
  --rw=write --bs=1M --size=4G \
  --filename=/tmp/fio_test --direct=1

# 随机读（最常用，模拟数据库）
fio --name=randread --ioengine=libaio --iodepth=128 \
  --rw=randread --bs=4k --size=4G \
  --filename=/tmp/fio_test --direct=1 \
  --numjobs=4 --runtime=60 --group_reporting

# 混合随机读写（70%读30%写）
fio --name=mixed --ioengine=libaio --iodepth=64 \
  --rw=randrw --rwmixread=70 --bs=4k --size=4G \
  --filename=/tmp/fio_test --direct=1 --runtime=60

# 关键输出指标
# IOPS：每秒 IO 次数
# BW：带宽（KB/s 或 MB/s）
# lat (usec)：延迟（微秒）
# clat percentiles：尾延迟（p99, p99.9）
```

---

## 六、文件系统故障处理

### 6.1 fsck 文件系统检查

```bash
# ext4（必须在卸载状态下运行）
umount /dev/sdb1
fsck.ext4 /dev/sdb1
fsck.ext4 -y /dev/sdb1                 # 自动回答 yes
fsck.ext4 -f /dev/sdb1                 # 强制检查（即使标记为 clean）
fsck.ext4 -n /dev/sdb1                 # 只读检查，不修复

# xfs（未挂载时）
xfs_check /dev/sdb1
xfs_repair /dev/sdb1
xfs_repair -n /dev/sdb1                # 只读模式

# 根文件系统在下次重启时自动 fsck
touch /forcefsck
# 或 shutdown 时指定
shutdown -rF now
```

### 6.2 只读挂载（文件系统损坏时的保护模式）

当文件系统出现错误被内核自动切换到只读模式时：

```bash
# 查看内核日志确认是否为只读
dmesg | grep "EXT4-fs error\|Remounting filesystem read-only"
journalctl -k | grep -i "readonly\|read-only"

# 在只读状态下执行修复（需要 unbusy 后卸载）
fuser -km /mountpoint                   # 终止使用该挂载点的进程
umount /mountpoint
fsck.ext4 -y /dev/sdb1
mount /mountpoint
```

### 6.3 磁盘坏道检测

```bash
# 只读测试（不会写入，耗时较长）
badblocks -sv /dev/sdb

# 检查 SMART 状态
smartctl -a /dev/sda
smartctl -t short /dev/sda             # 运行短测试
smartctl -t long /dev/sda              # 运行长测试

# 查看测试结果
smartctl -l selftest /dev/sda
```

---

## 七、/proc 和 /sys 存储相关路径

| 路径 | 用途 |
|------|------|
| `/proc/mounts` | 当前挂载信息 |
| `/proc/partitions` | 分区信息 |
| `/proc/diskstats` | 磁盘 IO 原始统计（iostat 数据来源）|
| `/proc/filesystems` | 系统支持的文件系统类型 |
| `/proc/sys/vm/dirty_ratio` | 脏页比例上限（超过触发同步写）|
| `/proc/sys/vm/dirty_background_ratio` | 后台刷盘触发阈值 |
| `/proc/sys/vm/swappiness` | swap 使用倾向（0-100）|
| `/proc/sys/vm/drop_caches` | 写1/2/3清空缓存（会影响性能）|
| `/sys/block/sda/queue/scheduler` | IO 调度器（mq-deadline/kyber/none）|
| `/sys/block/sda/queue/nr_requests` | IO 队列深度 |
| `/sys/block/sda/queue/read_ahead_kb` | 预读大小 |
| `/sys/block/sda/queue/rotational` | 0=SSD，1=HDD |
| `/sys/block/sda/stat` | 设备 IO 统计 |

```bash
# 查看并修改 IO 调度器
cat /sys/block/sda/queue/scheduler
echo mq-deadline > /sys/block/sda/queue/scheduler

# 持久化 IO 调度器（GRUB 参数）
# 在 /etc/default/grub 的 GRUB_CMDLINE_LINUX 中添加
# elevator=deadline

# 调整脏页刷新（减少写延迟抖动）
sysctl -w vm.dirty_ratio=10
sysctl -w vm.dirty_background_ratio=5
```

---

## 八、常用操作速查表

```bash
# 新磁盘从零初始化流程（以 /dev/sdb 为例）
parted -s /dev/sdb mklabel gpt
parted -s /dev/sdb mkpart primary ext4 0% 100%
mkfs.ext4 -L appdata /dev/sdb1
mkdir -p /data/app
echo "UUID=$(blkid -s UUID -o value /dev/sdb1)  /data/app  ext4  defaults,noatime  0  2" >> /etc/fstab
mount -a
df -hT /data/app

# 磁盘满了快速定位
df -h | grep -v tmpfs | sort -k5 -rn | head -5
du -sh /var/log/* | sort -rh | head -10
lsof +L1 | awk 'NR>1 && $7>100000000 {printf "%.0fMB %s %s\n",$7/1024/1024,$1,$NF}'

# 快速扩容 LVM（假设 /dev/sdc 是新盘）
pvcreate /dev/sdc
vgextend vg_data /dev/sdc
lvextend -l +100%FREE -r /dev/vg_data/lv_app
df -h /app
```
