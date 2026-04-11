---
title: "Vim 速查手册"
date: 2025-12-09T10:00:00+08:00
draft: false
tags: ["Linux", "运维", "Vim", "编辑器"]
categories: ["Linux"]
description: "运维工程师必备的 Vim 完整速查：模式切换、移动、编辑、搜索替换、多文件操作与高频实战场景"
summary: "覆盖 Vim 四种模式、所有移动方式、宏录制与寄存器、.vimrc 推荐配置，以及批量删除空行、注释多行、列操作等运维高频场景。"
toc: true
math: false
diagram: false
keywords: ["Vim", "vim速查", "vim宏", "vim寄存器", "vimrc", "运维编辑器"]
params:
  reading_time: true
---

## 一、模式切换

Vim 是模态编辑器，操作必须在对应模式下进行。

| 模式 | 含义 | 进入方式 |
|------|------|----------|
| Normal | 普通模式（默认）| Esc / Ctrl+c |
| Insert | 插入模式（输入文本）| i/a/o 等 |
| Visual | 可视模式（选择文本）| v/V/Ctrl+v |
| Command | 命令行模式 | : |
| Replace | 替换模式 | R |

```
Normal  --i/a/o/I/A/O/s/S/c-->  Insert
Normal  --v/V/Ctrl+v---------->  Visual
Normal  --:-------------------->  Command
Normal  --R-------------------->  Replace
Insert/Visual/Command  --Esc-->  Normal
```

### 1.1 进入 Insert 模式的方式

| 按键 | 含义 |
|------|------|
| `i` | 光标前插入 |
| `a` | 光标后插入 |
| `I` | 行首插入（第一个非空字符前）|
| `A` | 行尾插入 |
| `o` | 在当前行下方新建一行并插入 |
| `O` | 在当前行上方新建一行并插入 |
| `s` | 删除当前字符并进入插入模式 |
| `S` | 删除当前行内容并进入插入模式 |
| `c` + 动作 | 删除指定范围并进入插入模式（如 cw 删词）|
| `C` | 删除到行尾并进入插入模式 |

---

## 二、移动

### 2.1 基础移动

```
h  左
j  下
k  上
l  右

# 推荐关闭方向键依赖，强迫自己用 hjkl
```

### 2.2 词级移动

| 按键 | 含义 |
|------|------|
| `w` | 下一个词首（word，以标点分隔）|
| `W` | 下一个词首（WORD，以空白分隔）|
| `b` | 上一个词首 |
| `B` | 上一个词首（WORD）|
| `e` | 当前词末（或下一词末）|
| `E` | 当前词末（WORD）|
| `ge` | 上一词末 |

### 2.3 行内移动

| 按键 | 含义 |
|------|------|
| `0` | 行首（第0列）|
| `^` | 行首第一个非空字符 |
| `$` | 行尾 |
| `g_` | 行尾最后一个非空字符 |
| `f{char}` | 行内向右找 char（光标移到 char 上）|
| `F{char}` | 行内向左找 char |
| `t{char}` | 行内向右找 char（光标移到 char 前一位）|
| `T{char}` | 行内向左找 char |
| `;` | 重复上一次 f/F/t/T |
| `,` | 反向重复上一次 f/F/t/T |
| `%` | 跳转到匹配的括号/括弧 |

### 2.4 行级移动

| 按键 | 含义 |
|------|------|
| `gg` | 文件首行 |
| `G` | 文件末行 |
| `{n}G` | 跳到第 n 行 |
| `{n}gg` | 跳到第 n 行 |
| `:{n}` | 跳到第 n 行（命令模式）|
| `+` | 下一行行首 |
| `-` | 上一行行首 |

### 2.5 屏幕移动

| 按键 | 含义 |
|------|------|
| `H` | 屏幕顶部第一行 |
| `M` | 屏幕中间行 |
| `L` | 屏幕底部最后一行 |
| `Ctrl+f` | 向下翻页 |
| `Ctrl+b` | 向上翻页 |
| `Ctrl+d` | 向下翻半页 |
| `Ctrl+u` | 向上翻半页 |
| `Ctrl+e` | 屏幕向下滚动一行（光标不动）|
| `Ctrl+y` | 屏幕向上滚动一行 |
| `zz` | 将当前行移到屏幕中央 |
| `zt` | 将当前行移到屏幕顶部 |
| `zb` | 将当前行移到屏幕底部 |

---

## 三、编辑操作

### 3.1 删除

| 按键 | 含义 |
|------|------|
| `x` | 删除光标处字符 |
| `X` | 删除光标前字符（相当于 Backspace）|
| `dw` | 删除到词尾（含空格）|
| `diw` | 删除词（不含空格，inner word）|
| `dd` | 删除当前行 |
| `D` | 删除到行尾（同 d$）|
| `d0` | 删除到行首 |
| `d^` | 删除到行首第一个非空字符 |
| `dG` | 删除到文件末尾 |
| `dgg` | 删除到文件开头 |
| `{n}dd` | 删除 n 行 |
| `dit` | 删除 HTML 标签内容（inner tag）|
| `di"` | 删除双引号内内容 |
| `di(` | 删除括号内内容 |
| `da"` | 删除双引号及其内容（around）|

> 注意：Vim 的"删除"实际上是剪切，内容存入寄存器。

### 3.2 复制与粘贴

| 按键 | 含义 |
|------|------|
| `yy` | 复制当前行 |
| `Y` | 复制当前行（同 yy）|
| `yw` | 复制到词尾 |
| `yiw` | 复制当前词 |
| `y$` | 复制到行尾 |
| `{n}yy` | 复制 n 行 |
| `p` | 粘贴到光标后（行则在下方）|
| `P` | 粘贴到光标前（行则在上方）|
| `]p` | 粘贴并调整缩进 |

### 3.3 替换

| 按键 | 含义 |
|------|------|
| `r{char}` | 替换当前字符为 char |
| `R` | 进入替换模式（逐字符覆盖）|
| `~` | 切换大小写 |
| `g~{motion}` | 切换指定范围大小写 |
| `gU{motion}` | 转大写（如 gUiw 当前词转大写）|
| `gu{motion}` | 转小写 |

### 3.4 撤销与重做

| 按键 | 含义 |
|------|------|
| `u` | 撤销 |
| `U` | 撤销当前行所有修改 |
| `Ctrl+r` | 重做（反撤销）|
| `.` | 重复上一个修改操作（极其强大）|

---

## 四、搜索与替换

### 4.1 搜索

```
/pattern     向下搜索（支持正则）
?pattern     向上搜索
n            下一个匹配
N            上一个匹配
*            搜索光标处单词（向下）
#            搜索光标处单词（向上）
g*           搜索含光标处词的所有词（不限整词）

# 清除搜索高亮
:noh
# 或
:nohlsearch
```

### 4.2 替换语法（:s 命令）

```vim
:s/old/new/          " 替换当前行第一个匹配
:s/old/new/g         " 替换当前行所有匹配
:%s/old/new/g        " 全文替换所有匹配
:%s/old/new/gc       " 全文替换，逐个确认（y/n/a/q/l）
:%s/old/new/gi       " 全文替换，忽略大小写
:5,20s/old/new/g     " 替换第5-20行

" 正则替换示例
:%s/\s\+$//          " 删除行尾空白
:%s/^/    /          " 每行行首添加4个空格
:%s/\t/  /g          " Tab 替换为2个空格
:%s/foo\(bar\)/\1/g  " 删除 foo，保留 bar（捕获组）

" 确认替换时的响应键
" y  替换
" n  跳过
" a  全部替换（不再确认）
" q  退出
" l  替换当前后退出
" Ctrl+e/y  滚动屏幕查看上下文
```

### 4.3 全局命令 :g

```vim
:g/pattern/d         " 删除所有包含 pattern 的行
:g/^$/d              " 删除所有空行
:g/^#/d              " 删除所有注释行（以#开头）
:g/pattern/p         " 打印所有包含 pattern 的行
:g!/pattern/d        " 删除不包含 pattern 的行（:v 同效）
:g/pattern/m$        " 将匹配行移到文件末尾
:g/pattern/norm dw   " 对每个匹配行执行 normal 命令
```

---

## 五、多文件操作

### 5.1 Buffer（缓冲区）

```vim
:ls                  " 列出所有 buffer
:b 2                 " 切换到 buffer 2
:bn                  " 下一个 buffer
:bp                  " 上一个 buffer
:bd                  " 关闭当前 buffer（不退出 Vim）
:e filename          " 打开文件到新 buffer
:w                   " 保存当前 buffer
:wa                  " 保存所有 buffer
:qa                  " 关闭所有 buffer（全部退出）
:qa!                 " 强制关闭所有（放弃修改）
```

### 5.2 Tab（标签页）

```vim
:tabnew              " 新建 tab
:tabnew filename     " 在新 tab 打开文件
:tabn                " 下一个 tab（gt）
:tabp                " 上一个 tab（gT）
:tabc                " 关闭当前 tab
:tabo                " 关闭其他所有 tab
:tabs                " 列出所有 tab
{n}gt                " 切换到第 n 个 tab
```

### 5.3 Split（分屏）

```vim
:sp filename         " 水平分屏打开文件
:vsp filename        " 垂直分屏打开文件
Ctrl+w s             " 水平分屏（当前文件）
Ctrl+w v             " 垂直分屏
Ctrl+w h/j/k/l       " 在分屏间移动
Ctrl+w H/J/K/L       " 将当前分屏移到对应方向
Ctrl+w =             " 均分所有分屏
Ctrl+w +/-           " 调整高度
Ctrl+w >/<           " 调整宽度
Ctrl+w _             " 最大化当前分屏高度
Ctrl+w |             " 最大化当前分屏宽度
Ctrl+w q             " 关闭当前分屏
```

---

## 六、实用技巧

### 6.1 宏录制与执行

```vim
qa        " 开始录制宏到寄存器 a
...       " 执行一系列操作
q         " 停止录制
@a        " 执行寄存器 a 中的宏
@@        " 重复执行上一次宏
10@a      " 执行宏 10 次

" 示例：给每行末尾添加分号
" 将光标移到第一行
qa        " 开始录制
A;        " 行尾插入 ;
Esc       " 退出插入
j         " 下移一行
q         " 停止录制
100@a     " 重复100次（多执行无影响）
```

### 6.2 寄存器

```vim
"ayy      " 复制当前行到寄存器 a
"ap       " 粘贴寄存器 a 的内容
"byiw     " 复制当前词到寄存器 b

:reg      " 查看所有寄存器内容
:reg a    " 查看寄存器 a

" 特殊寄存器
" ""   未命名寄存器（默认 d/y 操作存到这里）
" "0   最近一次 yank 的内容（不受 d 影响）
" "+   系统剪贴板
" "*   选择区（X11 中间键粘贴）
" "/   最后一次搜索
" ":   最后一次命令
" ".   最后插入的文本
" "%   当前文件名

" 粘贴系统剪贴板（需要编译支持 +clipboard）
"+p
```

### 6.3 Marks（书签）

```vim
ma        " 在当前位置设置书签 a（小写=文件内，大写=全局）
'a        " 跳转到书签 a 所在行的行首
`a        " 跳转到书签 a 的精确位置
:marks    " 查看所有书签

" 特殊书签
`.        " 最后修改的位置
`"        " 上次退出时光标位置
`[        " 上次修改的起始位置
`]        " 上次修改的结束位置
''        " 上次跳转前的位置
```

### 6.4 折叠

```vim
zf{motion}   " 手动创建折叠（如 zf5j 折叠下5行）
zo           " 打开折叠
zc           " 关闭折叠
za           " 切换折叠状态
zR           " 打开所有折叠
zM           " 关闭所有折叠
zd           " 删除当前折叠

" .vimrc 中设置折叠方式
set foldmethod=indent    " 按缩进折叠（Python 友好）
set foldmethod=syntax    " 按语法折叠
set foldmethod=marker    " 按标记折叠（{{{ 和 }}}）
```

---

## 七、运维工程师高频场景

### 7.1 批量删除空行

```vim
" 方法1：全局命令
:g/^$/d

" 方法2：替换（将多个连续空行压缩成一个）
:%s/\n\{2,}/\r\r/g

" 方法3：只删除真正的空行（含空格的行也要删）
:g/^\s*$/d
```

### 7.2 注释多行

```vim
" 方法1：Visual Block 模式（推荐）
Ctrl+v           " 进入列选择模式
{j/k 选择行}
I                " 大写 I，行首插入
#                " 输入注释符
Esc              " 退出，所有选中行自动添加 #

" 取消注释（Visual Block 选中 # 后 x 删除）
Ctrl+v
{j/k 选择行}
{选中注释符列}
d                " 删除选中字符

" 方法2：替换命令
:5,20s/^/# /     " 第5-20行行首添加 # 
:5,20s/^# //     " 第5-20行删除行首 # 
```

### 7.3 列操作（Visual Block）

```vim
" 场景：批量在某列插入内容
Ctrl+v              " 进入 Visual Block
{选择行列范围}
I                   " 在选中块左侧插入
{输入内容}
Esc                 " 所有选中行同步插入

" 场景：批量替换某列字符
Ctrl+v
{选择区域}
r{新字符}           " 替换为新字符

" 场景：选择矩形区域后执行替换
Ctrl+v
{选择}
:s/old/new/g        " 只在选中区域内替换
```

### 7.4 读取命令输出

```vim
:r !date            " 将 date 命令输出插入到当前行下方
:r !cat /etc/hosts  " 将文件内容插入
:r !ls -la          " 将目录列表插入

" 对选中文本执行 shell 命令（结果替换选中内容）
{选择文本}
!sort               " 对选中行排序
!awk '{print $2}'   " 只保留第2列
```

### 7.5 快速编辑 config 文件

```vim
" 删除所有注释行和空行（清理配置文件）
:g/^\s*#/d
:g/^\s*$/d

" 查找未注释的配置项
/^\s*[^#]

" 在多处做相同修改（使用 . 重复）
/MaxConnections
cwMaxConnections  " 修改第一处
n                 " 跳到下一处
.                 " 重复修改

" 提取所有配置值（不含注释行）
:g!/^\s*#/p
```

### 7.6 比较两个文件

```bash
# 命令行启动 vimdiff
vimdiff file1 file2
vim -d file1 file2

# vimdiff 操作
# ]c   跳到下一个差异
# [c   跳到上一个差异
# do   从另一个文件获取差异（diff obtain）
# dp   将差异推送到另一个文件（diff put）
```

---

## 八、推荐 .vimrc 配置

```vim
" ~/.vimrc

" === 基础配置 ===
set nocompatible          " 关闭 vi 兼容模式
syntax on                 " 开启语法高亮
set number                " 显示行号
set relativenumber        " 相对行号（配合 hjkl 更高效）
set cursorline            " 高亮当前行
set showcmd               " 显示未完成命令
set wildmenu              " 命令行补全菜单
set laststatus=2          " 始终显示状态栏

" === 缩进 ===
set tabstop=4             " Tab 显示为4个空格
set shiftwidth=4          " 自动缩进宽度
set expandtab             " Tab 展开为空格
set smartindent           " 智能缩进
set autoindent            " 自动缩进

" === 搜索 ===
set incsearch             " 增量搜索（边输入边高亮）
set hlsearch              " 搜索结果高亮
set ignorecase            " 搜索忽略大小写
set smartcase             " 有大写字母时区分大小写
nnoremap <Esc><Esc> :nohlsearch<CR>  " 双 Esc 清除高亮

" === 编辑体验 ===
set backspace=indent,eol,start  " 退格键正常工作
set scrolloff=5           " 光标距屏幕边缘保持5行
set wrap                  " 长行自动折行
set linebreak             " 在词边界折行
set history=200           " 命令历史数量
set undolevels=500        " 撤销步数

" === 文件处理 ===
set encoding=utf-8
set fileformats=unix,dos  " 文件格式优先 unix
set nobackup              " 不产生 ~ 备份文件
set noswapfile            " 不产生 swp 文件（运维常见问题源）

" === 运维相关 ===
" 自动去除行尾空白
autocmd BufWritePre * :%s/\s\+$//e

" 显示不可见字符
set list
set listchars=tab:→\ ,trail:·,eol:¶

" 状态栏显示文件信息
set statusline=%F%m%r%h%w\ [%Y]\ [%{&ff}]\ [%l/%L:%c]

" === 快捷键映射 ===
let mapleader = ","       " 前缀键设为逗号

" 快速保存
nnoremap <leader>w :w<CR>
nnoremap <leader>q :q<CR>

" 分屏导航
nnoremap <C-h> <C-w>h
nnoremap <C-j> <C-w>j
nnoremap <C-k> <C-w>k
nnoremap <C-l> <C-w>l

" 行移动（Visual 模式下上下移动选中行）
vnoremap J :m '>+1<CR>gv=gv
vnoremap K :m '<-2<CR>gv=gv

" 快速编辑 vimrc
nnoremap <leader>ev :e ~/.vimrc<CR>
nnoremap <leader>sv :source ~/.vimrc<CR>
```

---

## 九、常用命令速查表

### 保存与退出

| 命令 | 含义 |
|------|------|
| `:w` | 保存 |
| `:w filename` | 另存为 |
| `:q` | 退出（有修改则报错）|
| `:q!` | 强制退出（放弃修改）|
| `:wq` | 保存并退出 |
| `:x` | 有修改则保存后退出 |
| `ZZ` | 同 `:x` |
| `ZQ` | 同 `:q!` |

### 行号操作

| 命令 | 含义 |
|------|------|
| `:set nu` | 显示行号 |
| `:set nonu` | 隐藏行号 |
| `:{n}` | 跳到第 n 行 |
| `:.` | 当前行号 |
| `:$` | 最后一行行号 |

### 常用 Ex 命令

```vim
:!command          " 执行 shell 命令
:shell             " 临时进入 shell（exit 返回）
:pwd               " 显示当前工作目录
:cd /path          " 切换工作目录
:sort              " 对选中行排序
:sort!             " 逆序排序
:sort u            " 排序并去重
:%!python3 -m json.tool  " 格式化 JSON（用外部命令处理当前文件）
```
