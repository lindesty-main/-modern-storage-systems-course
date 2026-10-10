# M03-L02：FUSE 用户态文件系统设计与实现

> 状态：课程正文　｜　建议课时：120–135 分钟（含核心实验）　｜　先修：[M03-L01：VFS、inode/dentry 与持久化边界](M03-L01-VFS、inode-dentry与持久化边界.md)、M02 的 POSIX 文件 I/O。必做程序只在内存中运行；真实 FUSE 挂载是明确可选项。

## 学习目标与节奏

学完本课，你应能：

1. 从应用 `ls/open/read/write` 追踪 VFS、FUSE 内核接口、`/dev/fuse`、libfuse daemon 和 callback 的请求/响应链。
2. 说明 high-level libfuse `getattr`、`readdir`、`open`、`read`、`write` 各自收到什么、应返回什么，以及 callback 不是一 syscall 一次的全量事件审计。
3. 设计可测的只读与内存版小文件系统，处理路径、偏移、EOF、短读写、洞、`O_TRUNC`、`O_APPEND`、容量和 errno。
4. 区分内核 dentry/属性/页缓存与 daemon 数据状态，知道缓存可减少 callback 次数，也会影响动态内容和一致性。
5. 说明挂载、前台运行、卸载、daemon 退出及故障恢复的生命周期，并能评估 FUSE 对权限、隔离、性能、持久性与能力的边界。
6. 按目标 libfuse API 检查 callback、错误处理和资源生命周期。

| 时间 | 内容 |
|---|---|
| 0–10 分钟 | 为什么把文件系统逻辑放在用户态？ |
| 10–30 分钟 | FUSE 内核/用户态架构和请求路径 |
| 30–55 分钟 | lookup/getattr/readdir/open/read/write 的调用链 |
| 55–80 分钟 | 只读/内存文件系统的数据模型、错误与偏移 |
| 80–95 分钟 | 缓存、持久化、挂载生命周期和并发 |
| 95–115 分钟 | 安全/能力限制、真实临时挂载演示或代码阅读 |
| 115–135 分钟 | 常见实现边界、练习与复盘 |

## 1. FUSE 放置的位置与用途

FUSE（Filesystem in Userspace）让用户态程序实现一部分文件系统逻辑，再由 Linux 内核 FUSE 模块将 VFS 请求转给它。它不把整个 VFS 移到用户态，也不意味着绕过 VFS：应用仍通过常规路径、fd 与文件 API 工作，内核负责名称解析、mount tree、缓存、请求协议和用户态进程通信。

```text
应用：ls /mnt  /  open/read/write/stat
                 │ 系统调用
                 ▼
VFS：路径、dentry/inode、fd、权限及缓存
                 │ 发现 /mnt 属于 FUSE mount；按需发出请求
                 ▼
Linux FUSE 内核模块 ─────── /dev/fuse 请求队列/协议 ───────┐
                                                           │
          内核接收 daemon 回复 ◄── libfuse ◄── callback 函数
                                                           │
                                   用户态 FUSE daemon ─────┘
                                   (进程内状态/网络/后端)
```

在 high-level libfuse 中，应用填写 `struct fuse_operations`，由库把协议请求转换为面向路径的 callback；低层 API 则显式管理 inode 编号、lookup 引用、`forget` 等对象生命周期。内核仍运行通用 VFS 与 FUSE 客户端代码，用户态进程通常负责命名空间、属性、数据来源、错误策略和服务端协议。

FUSE 适合快速试验对象/归档后端、用户目录视图、加密/压缩/内容转换、用户态文件格式、远程挂载和原型验证。读时水印是可讨论的例子：同一原始对象可根据请求身份生成不同视图，不一定要存多份副本。但 FUSE 不是“免费拦截一切文件操作”的安全监控机制：缓存可满足请求、程序可通过其他 mount/路径访问原文件、直接设备访问不经过这个 mount，且并非每种操作都有已实现 callback。若目标是完整审计，需要另外设计可信的系统观测与覆盖策略。

FUSE callback 中做网络、数据库或图像计算在技术上可行，但每个同步阻塞动作都可能延长前台请求；网络停顿、超时和远端写失败将暴露给本地 `read`/`write`。备份/同步通常需要队列、重试、去重、顺序、空间上限和失败回报，不是“收到一次 write 就顺手发包”即可获得可靠分布式存储。

## 2. `lookup → open → read/write` 的调用链

以下用 `/mnt/hello.txt` 说明。实际请求会受 dentry/属性/页缓存、内核能力、libfuse API、挂载选项和操作合并影响，因此它是**可能发生的逻辑链**，不是每个命令固定触发相同数量的 callback。

### 名字解析和属性

`ls /mnt` 会先查目录和挂载点。VFS 遍历 dentry；缓存未命中时，内核可向 FUSE 请求查找子项（协议层的 LOOKUP），libfuse high-level 路径通常由 lookup/getattr 相关逻辑提供对象信息。`stat`/`ls -l` 需要属性，如 `st_mode` 文件类型/权限、`st_size`、所有者、链接数和 inode 号。缺少类型或大小信息会让 `stat`/`ls` 产生错误或错误展示。

读取目录时走 `readdir`。high-level callback 通过 filler 返回 `.`、`..` 和目录项；offset 是恢复枚举使用的 cookie，不应忽略。filler 返回缓冲区已满时应停止继续填充。给出目录名字不等于文件内容已存在/属性永远新鲜；后续 lookup/getattr 仍可能发生。

### open 和读请求

```text
应用 open(path, flags)
  → VFS 做路径解析/权限等检查
  → FUSE kernel OPEN 请求（如 dentry 缓存中尚无可复用状态）
  → libfuse operations.open(path, fi)
  → daemon 检查 flags，建立/返回打开上下文
  → fd 返回给应用

应用 read(fd, buf, size)
  → VFS 从 fd 找 file/inode；可能先从内核页缓存满足请求
  → 缓存未命中/需要 daemon 时发送 FUSE_READ(inode/handle, offset, size)
  → operations.read(path, out_buf, size, offset, fi)
  → daemon 填充实际字节并返回 count，或返回 -errno
  → 内核向应用完成 read，更新相应状态/缓存
```

`open` callback 的 `fi->flags` 包含访问模式及 `O_TRUNC`/`O_APPEND` 等标志。文件系统必须让属性和数据状态一致；成功打开不等于已经读取数据。daemon 可把 per-open 对象放到 `fi->fh`，让后续请求识别一个对象/连接；Lab 04 的固定单文件原型不需要单独打开句柄。

`read` 的 offset 是文件视图中的字节偏移，不假定等于上次 callback 的末尾。程序可能并发、重试、随机读、预读或从不同 fd 访问。callback 返回写入输出 buffer 的**实际字节数**，到 EOF 返回 0，错误返回负 errno（如 `-EIO`）；不要返回 libc 的 `-1` 并依赖设置 `errno`。若正在实现有变换的视图，文件长度、offset 与映射后的内容必须共同定义。例如逐次 read 都在 chunk 前加一个水印前缀会重复插入前缀、破坏偏移和 EOF；正确模型要把转换后文件作为稳定的虚拟字节流，再按请求范围切片。

### 写请求

`write(fd, buf, size)` 可能进入 FUSE_WRITE，callback 收到路径/句柄、字节数和 offset。要在完成数据变更之前验证 mode、容量、偏移、整数范围和权限；成功回复的数量必须与实际处理一致，并遵守当前 high-level callback 对短完成/错误的约定。Lab 04 的纯内存核心提供 POSIX 风格短写 helper，而挂载适配器先检查剩余容量：一个 FUSE 请求超出容量则返回 `-ENOSPC`，不会把部分写入伪报成完整成功。

FUSE 不自动提供“创建任意文件”。若 operations 未实现 `create`/`mknod`，就应明确文件已预置且新增路径返回错误；若增加创建，需要原子分配 inode/数据和目录关系，并处理并发 `O_CREAT|O_EXCL`、失败清理、`unlink`、`rename` 和容量。写入的 `offset > EOF` 若支持稀疏洞，应保证洞读作零，并正确扩大 `st_size`；不能用未初始化内存泄漏旧内容。

### Callback 结果与 errno

| 结果/条件 | 常见语义 |
|---|---|
| 成功返回 `0` | 属性、open 或目录操作成功；具体 callback 定义具体值 |
| `read` 返回 `0` | 已到 EOF；不是“读请求没有触发” |
| 返回正数 `n` | 仅表示已处理/填充 `n` 字节；检查 API 对短完成的要求 |
| `-ENOENT` | 路径不存在 |
| `-EISDIR` / `-ENOTDIR` | 操作要求的对象类型不匹配 |
| `-EROFS` / `-EACCES` / `-EPERM` | 只读策略、权限或能力拒绝 |
| `-EINVAL` | 参数、标志或偏移不合法 |
| `-ENOSPC` | 资源/容量耗尽 |
| `-EIO` | 后端/数据错误；不要伪装成功 |
| `-ENOSYS` | callback 未实现或接口不支持；内核/libfuse 对某些操作可能按“不支持”特殊处理，不是通用的业务错误码 |

错误要从调用方看得见的边界传播。比如 `cat` 读取时才发现后端错误，不能因为先前 `open` 成功就静默返回陈旧或部分伪造数据。`close`/release 与 `flush` 也有不同用途；libfuse 文档明确 `flush` 不等同 `fsync`，close 的错误还可能被应用忽略。不能靠 release callback 保证数据持久化。

## 3. Lab 04 的只读和内存版设计

配套实验：[Lab 04 README](../../labs/lab-04-vfs-fuse/README.md)。必做部分用 C11 做一个固定目录 `/` 与固定文件 `/hello.txt`，容量 8192 字节，不依赖 FUSE 头文件。核心接口以负 errno 返回，清晰分开逻辑与挂载胶水：

```text
memfs_init(state, mode)
memfs_lookup(state, path, stat)       → root/file attributes or -ENOENT
memfs_open(state, path, flags)        → O_TRUNC/O_EXCL/access mode checks
memfs_read(state, path, buf, n, off)  → count / EOF 0 / -errno
memfs_write(state, path, buf, n, off, append)
                                       → count (may be short) / -ENOSPC / -errno
```

### 数据与属性不变量

- **只读模式：**启动时把固定文本复制进进程内存，`st_mode` 是普通文件 `0444`，大小为固定内容字节数；读到 EOF 返回 0。写、截断返回 `-EROFS`。虽然名字始终存在，但没有目录项创建/删除功能。
- **内存模式：**单个文件初始为空、属性 `0644`，数据仅在结构体固定数组中。`O_TRUNC` 清空，覆盖写不错误缩小旧文件长度，`O_APPEND` 追加到当前 EOF；写到 EOF 之后的洞显式清零；读到末尾截短。访问固定容量之外返回 `-ENOSPC` 或纯 helper 按已有数据空间返回短写。
- **路径白名单：**只接受精确的 `/` 与 `/hello.txt`；`/../hello.txt`、前缀近似名及其他路径不能通过字符串前缀绕过。
- **错误和边界：**负 offset 为 `-EINVAL`；读目录返回 `-EISDIR`；未知路径 `-ENOENT`；成功读取返回实际字节数。先验证路径/对象类型，再访问数组。代码使用不会依赖 `offset + size` 直接相加的容量计算，避免有符号/无符号溢出。
- **并发：**纯核心注释要求调用者序列化访问；FUSE adapter 对共享状态加 mutex。单测只验证确定性单线程行为，不证明锁、取消或并发读写的更广泛语义。
- **持久性：**没有后端文件、WAL、`fsync` 实现或恢复格式。FUSE 挂载进程退出即丢失内存状态；再次启动新实例从初始内容开始。即使一次 FUSE 写成功、之后同一进程读取到它，也不意味着重启可恢复。

查看 `fuse_memfs.c` 的适配层可看到：`getattr` 转到 lookup；`readdir` 填入目录 cookie；`open` 转交 flags；`read`/`write` 将 path/size/offset 映射给核心；共享状态加锁。程序显式接收 `--readonly` 或 `--memory`，其余参数交给 libfuse；`-f` 前台运行便于诊断，`-s` 用于让可选实验与“调用者串行化”假设一致。核心 `test_memfs_core.c` 能在无 mount、无 `/dev/fuse`、无特权环境运行。

## 4. 缓存：回调不等于每次用户操作

FUSE 的内核客户端可缓存 dentry/name lookup、属性和文件页数据，具体行为受超时、协议能力、`fi->keep_cache`、`kernel_cache`、`auto_cache`、`direct_io`、`writeback_cache` 与通知失效等配置影响。默认值与语义按 libfuse/内核版本核对；不要仅凭“我实现了 read”推断每次 `cat` 的每一次 read() 都会到 daemon。

- **dentry/lookup 缓存：**重复路径解析可用内核现存目录项，daemon 未必再次收到 LOOKUP。删除/重命名后要正确处理失效与引用生命周期。
- **属性缓存：**`getattr` 返回的 size/mode 可在内核缓存一段时间。外部后端发生变化时，若没有通知失效/合适 timeout，客户端可能短暂看到旧属性。
- **文件页缓存：**命中时 read 可不经过用户态；写回缓存还可能合并/延迟写请求。动态加水印、身份相关视图或远端一致性不能忽略缓存键与失效策略。
- **`direct_io` 不是持久化开关：**它调整特定数据缓存路径/短读写表现，不会自动保证后端数据同步、零拷贝或硬件耐久，也不等同于 `O_DIRECT` 的所有语义。
- **invalidate 需要协议：**远端数据有变化时，应用可用的 FUSE 通知/缓存时限必须与更新协议一起设计；盲目关闭所有缓存也有性能和资源代价。

M03 原型只有一个本地 daemon 进程作为写入者；实验验证文件接口和内容，不测试跨客户端 coherence、cache invalidation、writeback 可靠性或 FUSE 缓存性能。

## 5. 挂载生命周期与错误恢复

```text
准备空 mountpoint（拥有者私有临时目录）
 → libfuse 建立会话、通过 /dev/fuse 请求内核挂载
 → 前台 daemon 读取内核请求并应答
 → 应用经 VFS 使用挂载点
 → fusermount3 -u mountpoint 正常卸载
 → daemon/session 退出，清理临时目录
```

挂载点本身必须是存在的目录；已挂载时原目录内容被该视图覆盖，不会被删除。卸载会让原 mountpoint 背后的目录内容重新出现。`-f` 留在前台，`-d` 适合调试协议；进程保持前台也让 shell 脚本可以可靠等待并管理子进程。若某进程的 cwd、打开 fd 或子挂载仍使用挂载点，卸载可能报告 busy；先离开目录/关闭 fd，再以当前用户 `fusermount3 -u`。daemon 崩溃后常见 “Transport endpoint is not connected”，应检查进程、卸载遗留 mount，再重新启动；不要把 mountpoint 当成普通目录强行删除。

Lab 04 的 `test-fuse.sh` 自行 `mktemp -d` 并仅挂载到其私有子目录，不使用 root；正常/错误/中断退出时会卸载、等待进程并删除它创建的临时目录。容器或沙箱可禁止 FUSE mount，此时显示 SKIP，不以 `sudo`、`allow_other` 或访问其他设备来规避。`/dev/fuse` 是内核 FUSE 协议所需的设备接口；必做核心不会访问它，真实挂载则不可避免地使用它，但不会访问裸盘、块设备或物理存储设备。

## 6. 安全与能力边界

FUSE 把复杂度从内核模块转移到 daemon，不代表自动安全。daemon 处理来自其他进程的路径、标志、offset、长度与操作顺序；错误的边界检查/并发管理仍能越界、泄漏内存或返回错误数据。

1. **最小权限：**默认以当前用户运行，不用 root；不要默认加 `allow_other`。该选项扩大可访问主体，且发行版可能要求 `/etc/fuse.conf` 显式允许。需据 FUSE 文档/本机设置核对权限路径。
2. **双重权限校验：**可用 `default_permissions` 请求内核按返回的 mode/owner 做权限检查；daemon 也要执行后端授权。若需要按请求者区别内容，可读取可信的 FUSE request context（uid/gid/pid），不要从文件名/客户端提供的字符串猜身份。
3. **路径与后端：**固定本实验仅做精确路径匹配。如果拓展为把 mount path 映射到宿主目录，不能用字符串拼接后直接 `open`；要处理 `..`、符号链接、竞态替换、硬链接、mount namespace 和安全的 dirfd/openat2 约束。
4. **内存和整数：**限制最大文件数/大小/请求长度；先验证负 offset 和加法溢出，再指针运算；检测 malloc、后端短 I/O、关闭、同步和校验错误。避免回调把原始路径当作安全文件名。
5. **并发与阻塞：**libfuse 默认可能并行调用。共享状态要锁或实现明确的同步；不能持锁执行会重入 daemon 的操作。网络/数据库超时、取消和 daemon 停止都要有上界。
6. **可信边界：**FUSE 用户态进程仍受 Linux 进程凭据、mount namespace、设备节点和系统策略限制；它不能因此直接调用内核任意函数，也不能拦截不经过此 mount 的操作。内核 FUSE 模块仍处于内核可信计算基内。
7. **缓存与身份：**若同一 inode 返回随用户变化的数据，普通 inode/page cache 可能把一个用户看到的结果供给另一个用户；必须设计缓存隔离、inode/handle 策略与失效，或禁用不适用的缓存。不要把演示水印方案当成安全授权。

FUSE 可能比本地内核文件系统多一次内核/用户态请求往返和 daemon 排队，进程调度、上下文切换、拷贝、后端 RPC、缓存命中率和请求批量都会影响性能。不可从单次 `ls`/`cat` 感觉推断性能，也不可宣称“FUSE 必然慢/必然快”；真正比较要固定 API 语义、缓存状态、访问模式、并发、尾延迟和后端。

### FUSE 与 FUSE-over-io_uring

传统 `/dev/fuse` 请求/响应路径是本课主线。FUSE-over-io_uring 是 Linux 内核与用户态协议的扩展，涉及特定内核接口、能力协商和 libfuse 支持；不是把任何 FUSE 文件系统简单改成提交 SQE 就能使用，也不是本 Lab 依赖。课程版本规划要求按目标 kernel/libfuse 核验，因此此处不声称候选内核/API 已验证。

## 7. 常见实现边界与安全检查

| 简化实现 | 需要定义的行为 |
|---|---|
| 假设挂载后所有 `read/write/close` 都会交给用户态 | 内核缓存可能直接满足请求；callback 次数不等同应用 syscall 数。未实现的操作可能失败或被标记为不支持，不能当作全量审计。 |
| 直接分配 buffer 后按 offset 复制 | 检查分配失败、负 offset、整数溢出、路径和容量；在指针运算前验证完整范围。 |
| 写入后把文件长度设为 `offset + size` | 覆盖文件中部时，正确长度一般为 `max(old_size, offset + actual_written)`；超出 EOF 的洞须按接口语义初始化。 |
| 允许 callback 返回任意短写 | 内存核心与 high-level FUSE adapter 的短写/错误约定可能不同；adapter 应在修改前预检，并遵循目标 libfuse API 的返回约定。 |
| 读取时直接计算 `size - offset` | offset 超过 EOF 时可能无符号下溢；先判断 EOF，再按请求长度和剩余字节计算实际读取量。 |
| 忽略 `readdir` offset 和 filler 结果 | 大目录需要可恢复的 cookie/offset；缓冲区满时停止填充，并保持 cookie 语义一致。 |
| 对每次 read 插入固定前缀或水印 | read 按 offset 读取字节流。应先定义完整的转换视图与长度，再按视图偏移返回切片；缓存还须满足身份隔离。 |
| 任意复制 `FUSE_USE_VERSION` 和 callback 原型 | libfuse API 版本会影响结构和签名；使用目标版本头文件、明确 API 级别并实际构建。 |
| 把卸载或直接 Ctrl-C 当作完整退出管理 | 正常路径先卸载，再等待 daemon 退出；忙碌、崩溃、当前工作目录和临时目录都要纳入清理。 |

持久化文件系统还需要磁盘格式、分配器、元数据/目录更新、恢复协议、并发与校验设计；不能从 FUSE callback 直接写裸设备就得到完整文件系统。本实验刻意止于 volatile 内存。

## 8. 实验、检查题与作业

先在实验目录完成无特权核心测试：

```sh
make -C 'labs/lab-04-vfs-fuse' clean all test
```

可选 libfuse3 编译/实际临时挂载说明见 [Lab 04 README](../../labs/lab-04-vfs-fuse/README.md)。本机验证过的 libfuse3 为 3.18.3；课程版本表候选 libfuse3 3.14.0 尚未验证。挂载若受限应 SKIP，不提权。

检查题：

1. `ls -l /mnt/hello.txt`、`cat /mnt/hello.txt`、`echo x >> /mnt/hello.txt` 可能各触发哪些操作？哪些可能由 VFS cache 满足？为什么不能用调用数直接推导 callback 数？
2. `read` 请求 offset=10，而内存文件当前只有 6 字节，应返回什么？offset 为负数又如何处理？属性 `st_size` 应是多少？
3. `open(O_WRONLY|O_TRUNC)` 与随后 `write` 分属什么状态变更？如果截断回调处理成功、数据写失败，文件状态会怎样？应如何报告而不是伪造原子性？
4. 内存核心对大写返回了部分 count，而 FUSE high-level write adapter 返回错误。为什么这两个层次可以有不同边界？发生容量错误前为何要预检？
5. daemon 在 write callback 中同步请求远端服务，远端超时对用户 `write`、锁持有和其他 callback 有何影响？给出一个明确的失败/重试策略。
6. 同一 inode 给不同 UID 返回不同水印数据时，内核文件页缓存会造成什么隔离风险？如何设计缓存键或失效策略？
7. 如果要求重新启动后数据仍存在，必须增加哪些格式、写入顺序、错误传播和恢复检查？为什么调用 `fdatasync` 不能凭空让只存在 RAM 的数组持久化？

作业任选其一：

- 扩展内存模型支持两个文件与目录枚举，并为 create、unlink、truncate 定义状态机与错误测试；不得只增加 callback 指针而没有元数据不变量。
- 将固定文本转换为一个只读生成文件（例如带 header 的版本清单），按随机 offset 读取并测试 EOF/短读；证明返回内容与 `getattr.st_size` 一致。
- 设计一个只读“水印视图”，先在内存中生成或计算固定的最终 byte stream，再验证不同 offset/size 片段拼接等于完整转换结果；明确身份与缓存隔离，不接入外网或图像大文件。
- 为未来持久版本写出崩溃状态表：目录更新、数据写入、同步、rename 与父目录同步分别在哪些时点可能失败，重启后应保留哪些状态。不要访问裸设备。

## 参考资料与验证边界

- Linux 内核：[FUSE 文档](https://docs.kernel.org/filesystems/fuse.html)、[FUSE-over-io_uring 文档与限制](https://docs.kernel.org/filesystems/fuse/fuse-io-uring.html)、[VFS 文档](https://docs.kernel.org/filesystems/vfs.html)。协议能力与内核版本有关。
- libfuse 上游：[libfuse 项目与 API 文档](https://github.com/libfuse/libfuse)、[`fuse_operations` header](https://github.com/libfuse/libfuse/blob/master/include/fuse.h)、[示例目录](https://github.com/libfuse/libfuse/tree/master/example)。在线 `master` 是参考入口，不代替本地版本的头文件和包验证。
- 手册：[fuse(4)](https://man7.org/linux/man-pages/man4/fuse.4.html)、[fuse(8)](https://man7.org/linux/man-pages/man8/fuse.8.html)、[mount.fuse(8)](https://man7.org/linux/man-pages/man8/mount.fuse.8.html)。发行版 helper 名称/策略可能不同，本实验使用 `fusermount3`。
- 课程源码与运行说明：[Lab 04](../../labs/lab-04-vfs-fuse/README.md)、[M03-L01](M03-L01-VFS、inode-dentry与持久化边界.md)。

**实际验证声明：**本次在 Linux 7.2.9、GCC 16.2.1、libfuse3 3.18.3 环境，以当前用户构建并成功挂载/卸载两种临时文件系统；这不代表已验证课程候选 Ubuntu 24.04.5/内核 6.8/libfuse3 3.14.0 组合，也不证明崩溃持久化、FUSE 性能、安全审计覆盖或其他 namespace/容器策略。
