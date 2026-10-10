# M03-L01：VFS、inode/dentry 与持久化边界

> 状态：课程正文　｜　建议课时：105–120 分钟　｜　先修：[M02-L01：POSIX 读写、页缓存与 `O_DIRECT`](../M02-LinuxIO/M02-L01-POSIX读写、页缓存与O_DIRECT.md)。本课以 Linux VFS 为例讲解接口和对象关系；不同内核/文件系统内部实现会演进，不把示意图当作某个文件系统的磁盘布局。

## 学习目标与节奏

学完本课，你应能：

1. 画出路径名查找、`open`、`read`/`write` 从进程经 VFS 到文件系统和页缓存的关键路径，解释一次系统调用为什么不必等于一次磁盘请求。
2. 区分 superblock、inode、dentry、`struct file`、文件描述符和目录内容；理解名字、对象身份、打开实例和持久化记录不是同一个东西。
3. 沿“目录项查找 → inode → open file → read/write”描述一个普通文件请求，解释缓存命中与未命中时的差异。
4. 划分进程可见成功、页缓存已修改、文件系统已提交、设备确认及掉电后可恢复等不同边界；能正确说明 `fsync` 和父目录同步的目的及限制。
5. 识别硬链接、重命名、负 dentry、挂载点与 namespace 对路径语义的影响，并避免把内存中的 VFS 对象误当作磁盘格式。

| 时间 | 内容 |
|---|---|
| 0–10 分钟 | 文件名、文件对象与持久化究竟分别指什么？ |
| 10–30 分钟 | VFS 抽象及 superblock/inode/dentry/file |
| 30–50 分钟 | 路径查找、lookup、open 与 fd 生命周期 |
| 50–70 分钟 | read/write、页缓存与回写 |
| 70–90 分钟 | 文件系统的持久化边界、崩溃与同步 |
| 90–105 分钟 | 常见误区、代码/文档阅读与练习 |
| 105–120 分钟 | 结合 Lab 04 的内存文件系统画出映射关系 |

## 1. 问题：一个路径字符串如何指向数据？

调用 `open("/home/alice/notes.txt", O_RDONLY)` 时，应用给出的只是路径和访问要求。内核需要确定调用进程所在的 mount namespace、根目录/当前目录、每段路径对应的目录项、最终对象类型、权限及挂载边界。找到对象之后，还要创建本次打开状态；后续 `read(fd, ...)` 的 `fd` 并不是文件名，也不是磁盘 inode 号。

可以先把关系画成：

```text
进程的 fd 表                         内核 VFS 对象/缓存                         文件系统与存储
 fd 3 ────────────────► struct file ──────────────► dentry ──► inode ────────► 数据/元数据
                           │                         名字/父子关系  属性/操作      页缓存/块/对象
                           └─ f_pos、flags、f_op

路径：/home/alice/notes.txt
  mount namespace → 根 dentry → "home" → "alice" → "notes.txt"
```

这是概念关系，不是每个箭头都对应一个磁盘读取。VFS（Virtual Filesystem Switch）为多种文件系统提供共同的内核接口；底层可以是 ext4、XFS、tmpfs、网络文件系统、FUSE 或其他实现。VFS 把通用的路径、对象和操作规则与文件系统专属存储格式隔开。

## 2. 四个常被混在一起的对象

### Superblock：一次已挂载文件系统的全局上下文

内核 `struct super_block` 表示一个已挂载文件系统实例的通用状态，关联文件系统类型、根 inode/dentry、挂载标志、超级块操作、资源统计和回写/同步工作等。它和某个磁盘格式中名为“superblock”的字节区域**相关但不等同**：磁盘格式由 ext4、XFS 等各自定义；VFS 的 `struct super_block` 是运行时内核对象。一个文件系统类型可挂载多次，也可来自内存、网络或用户态服务，并不保证每次挂载都对应一个可读的本地 superblock 扇区。

### Inode：对象身份、类型和元数据

VFS `struct inode` 表示一个文件系统对象的运行时身份与通用元数据，包括文件类型、权限、所有者、大小、时间戳、链接计数和文件系统私有数据；它还通过 `i_op`、`i_fop` 等操作表连接到对象/文件操作。普通文件内容不是必须内嵌在 inode 结构里的：可能由 extents、间接块、树、对象 ID、远端 RPC 或内存页表示。磁盘上的 inode 编码也是具体文件系统的格式，并不等于运行时 `struct inode`。

`st_ino`/`i_ino` 通常只在某个文件系统设备/实例范围内有意义；跨文件系统辨认对象还需考虑设备/挂载信息。硬链接说明 inode 与路径名不是一对一：多个目录项可指向同一个 inode，删除一个名字减少链接关系，不一定立即释放仍被其他链接或已打开文件引用的对象。

### Dentry：名字与父子关系的查找缓存

Linux dentry（directory entry）把某个名字与父目录及 inode 联系起来。dentry cache 是路径遍历的重要缓存：路径组件已经缓存时，内核可能不再向文件系统询问该名字；缓存中也能保存“目前不存在”的**负 dentry**，减少重复失败查找。dentry 表示运行时名字关系，不普遍等同于一个通用、固定格式的磁盘目录记录。

目录自身是文件系统对象。持久化文件系统通常有它自己的目录数据结构，把名称关联到对象标识/类型等信息；查目录项可能读缓存或落到底层元数据块。硬链接可产生多个名字指向同一 inode；目录重命名、挂载点跨越和 namespace 又会影响名字树，因此不能把 dentry 简化成“文件名字符串加 inode 的永久唯一指针”。

### `struct file` 与 fd：一个打开实例和进程中的句柄

每次成功的 `open` 通常创建一个 `struct file`（Linux 的 open file description），其中有打开标志、当前文件偏移 `f_pos`、文件操作表和对 dentry/inode 的引用。内核把它放入调用进程的 fd table，用户拿到小整数 fd。`dup` 或 `fork` 后的某些描述符可共享同一个打开实例及偏移；重新调用 `open` 通常得到另一个打开实例。`pread`/`pwrite` 显式给偏移，避免使用/更新共享 `f_pos`，见 M02。

```text
进程 A fd 3 ─┐
             ├──► 同一 struct file：f_pos、状态、操作表 ─► dentry/inode
进程 B fd 8 ─┘    （例如 dup/fork 共享）
进程 A fd 4 ─────► 另一个 struct file ────────────────────► 同一 inode
                   （再次 open 同一路径）
```

`close(fd)` 释放这个进程的描述符引用；只有相应引用生命周期结束后，内核才可能最终释放打开实例。unlink 路径名不必让已打开 fd 立刻失效，因为 open file 仍引用对象。这也是“文件名不存在”与“已有打开者仍能读写”的区别。

| 名称 | 主要回答的问题 | 是否是一个通用持久磁盘结构？ |
|---|---|---|
| superblock | 这个挂载实例/文件系统如何工作？ | VFS superblock 是运行时对象；磁盘 superblock 是格式专属 |
| inode | 对象是什么、属性与操作是什么？ | 运行时 inode 与磁盘编码不能画等号 |
| dentry | 此父目录下这个名字当前指向什么？ | 主要是 VFS 路径缓存；磁盘目录格式由 FS 决定 |
| `struct file` | 某次 open 的偏移、标志和操作状态是什么？ | 运行时打开对象，不是文件内容的持久副本 |
| fd | 这个进程用哪个小整数引用打开实例？ | 进程句柄；进程退出后不会变成文件名 |

一个常见简化是把 `file`、inode、directory 说成“file 是属性、inode 存数据”。在 Linux VFS 中，`struct file` 指打开实例，不是通用的“文件属性对象”；inode 描述对象元数据和操作，不保证自己直接存放内容；目录是有类型的文件系统对象，路径名字关系由文件系统格式与 VFS dentry/cache 共同体现。教学对照不能当成内核结构的一一对应规范。

## 3. 路径 lookup 与 open：系统调用发生了什么

以下是高层路径。具体函数分层、缓存锁、RCU-walk/ref-walk 和文件系统回调依内核版本变化；这里不要求把某个函数名当稳定 ABI。

```text
openat(dirfd, path, flags, mode)
  ├─ 解析相对/绝对路径、当前目录与 mount namespace
  ├─ 逐段 walk：查 dentry cache；未命中时调用 FS lookup 等查找对象
  ├─ 检查目录遍历权限、最终对象类型、访问/创建/截断规则
  ├─ 建立 struct file，关联 inode/dentry、flags、f_pos 与 f_op
  └─ 返回进程 fd

read(fd, buf, len)
  ├─ fd table → struct file；检查访问模式
  ├─ 调用对应 file operation，使用/更新 f_pos（pread 用显式 offset）
  ├─ 文件系统/通用 page-cache 路径取得数据
  └─ 拷贝到用户 buffer，返回字节数、EOF 0 或 errno
```

**lookup 不保证每个 `stat` 或 `open` 都做一次磁盘目录读。**命中 dentry/inode cache 时可以用已有运行时对象；未命中时文件系统可能从页缓存/元数据缓存或持久介质查询。文件系统返回/获得 inode 后，VFS 建立或复用 dentry。权限检查可能在 VFS、文件系统、LSM、挂载选项或远程服务端共同完成，取决于能力和配置。

**open 不保证数据已读入内存。**它主要解析名称、检查访问、建立打开状态，并可能触发创建/截断等操作。随后 `read` 使用 fd 对应的 file operation。内核可经文件系统/通用缓存返回缓存中的数据；若缓存未命中，文件系统把逻辑文件区间映射到自己的数据源，进一步发块 I/O、网络请求或用户态 FUSE 请求。一次 `read` 可以短读，一次底层请求也可能被合并、拆分、重试或由预读产生。fd 和 LBA、dentry 与物理块均不存在简单的一一映射。

## 4. 普通文件 read/write 与页缓存

许多本地文件系统的常规 buffered read 走页缓存：先找对应文件页/folio；命中时将已有数据复制到用户缓冲区，缺页时由文件系统读取或填充，再复制数据。预读可能额外取入后续数据。`O_DIRECT`、DAX、网络文件系统及不同内核路径会改变细节，但也不能将这些路径统称为“绕过所有内核和文件系统语义”。

常规 buffered write 通常先把用户数据复制进缓存中的文件页并标记脏，系统调用可以在数据真正写到设备之前返回。后台回写线程、内存回收或显式同步稍后把数据交给文件系统；文件系统可能在同一时间更新大小、时间、分配结构、日志/校验信息，然后经块层交付设备。write 成功意味着本次接口接受/处理了相应字节，不等价于断电后必然能恢复。

```text
write(fd, data)
  → VFS/file operation
  → 文件页缓存变脏、文件大小/元数据变更
  → write() 返回（数据可能尚未到设备）
  → 后台 writeback / fsync 路径
  → 文件系统日志/元数据排序、块层请求
  → 设备缓存/介质及 flush/FUA 语义
```

对应用，`read`/`write` 可能短传输、收到 `EINTR`，也要处理错误；管道/socket 和普通文件 EOF/阻塞语义另有差异。对文件系统，大小更新、块分配、目录项创建、日志提交与数据块写入是多类状态，不能用一个“文件写成功”覆盖全部事务。

## 5. 持久化边界：从缓存到重启后能看见

把成功层次分开：

1. **系统调用返回：**内核接口报告这次请求处理了多少字节或错误。没有一般性地承诺介质已持久化。
2. **脏页/内核缓存更新：**运行时内核能够读到新值；断电或崩溃仍可能丢失。
3. **文件系统提交/设备命令完成：**数据/元数据按相应文件系统和设备路径写出。设备易失性写缓存、控制器行为和 flush/FUA 支持仍影响承诺。
4. **`fdatasync`/`fsync` 成功：**请求同步该文件的数据及恢复这些数据所需的元数据（`fdatasync` 可省去与数据恢复无关的一些元数据）。这是应用可使用的重要边界，不是所有设备不论实现都绝对抵抗任意断电的数学保证。
5. **目录项持久化：**新建/重命名后的文件名属于父目录元数据。若应用要求崩溃后新文件名也确定存在，常需在文件同步后按平台语义同步包含它的父目录；同步文件 fd 本身不自动等价于目录项同步。

日志文件系统的 journal 通常用于恢复文件系统结构一致性，不等于应用多条写入自动成为一个原子事务。应用若有 WAL，仍须定义记录校验、提交顺序、文件与目录同步、重放和幂等性。CoW 文件系统、数据库和远程文件系统可能另有实现及保证，按产品文档和版本验证。

`rename` 在规定条件下提供命名空间操作的原子可见性，但它不自动使此前写入的数据已落盘；要实现“写临时文件、同步、rename、同步父目录”的耐崩溃替换流程，还需针对目标文件系统/OS 确认同步顺序与错误处理。`fsync()` 返回错误也不可忽略；延迟分配、空间耗尽、远程故障或回写错误可能到稍后的同步/关闭才暴露。`close()` 不能替代同步。

## 6. 跟踪一个 `open` 后的文件生命周期

例子：应用打开 `/tmp/result.bin`，写入 4 KiB，再读回并关闭。

1. `openat` 由路径解析经过 mount namespace 与 dentry tree；若最终 dentry 缓存未命中，VFS 询问对应文件系统查找目录项，并获得 inode/属性。
2. VFS/LSM/文件系统按实际配置检查搜索权限、文件 mode、凭据、ACL 和 flag 语义。成功后为此次打开创建 `struct file`，fd table 返回如 5 的编号。
3. `write(5, buf, 4096)` 交给对应 file operation。文件系统更新数据/大小/时间的运行时状态，常规 buffered 路径可能先将页置脏并返回；若路径是 FUSE，内核也可能向用户态 daemon 发 WRITE 请求。
4. `pread(5, out, 4096, 0)` 使用显式偏移读取。数据可能仍在页缓存中，所以应用能读回刚写入内容；这只能证明当前内核可见，不证明重启可恢复。
5. 应用调用 `fdatasync(5)` 或 `fsync(5)` 并检查返回值，之后关闭 fd。若该文件刚创建/替换且需保证名字持久，另考虑同步父目录。
6. 文件描述符关闭后，dentry/inode 可能仍留在缓存；这些缓存按内存压力/引用生命周期回收。持久文件系统的磁盘/远端元数据继续存在；tmpfs 或本课 memfs 的数据随其内存实例消失。

这个例子同样适用于理解 MiniStore：底层的数据结构即使提供 `write`/`read` 函数，也还要明确打开状态、命名映射、持久化、错误传播和生命周期；M03 的内存实验只实现其中的最小子集。

## 7. 常见混淆与安全阅读

- **“inode 就是文件数据块。”** inode 是对象身份/属性和操作接口；内容布局由文件系统决定，可能不在 inode 本体。
- **“dentry 是磁盘 directory record。”** dentry 是 VFS 运行时路径项/缓存；持久目录格式是 FS 专属。
- **“fd 是 inode 编号。”** fd 是每进程的小整数，指向打开实例；两个 fd 可共享一个 `struct file`，不同打开实例也可能引用同一个 inode。
- **“所有 read 都到盘，所有 write 都立即落盘。”** 页缓存、预读、合并、延迟回写及设备缓存都会改变路径和时间点。
- **“journal=应用事务/掉电保证。”** 文件系统结构恢复机制和应用一致性协议是不同层次；设备是否正确处理 flush 也重要。
- **“一个 syscall 等于一个磁盘 request。”** 内核可以缓存、拆分、合并、重试、异步回写。用 `/proc` 某个计数器或一次系统调用不能直接推断设备命令数。
- **“每个路径都只有一个文件名。”** 硬链接、负 dentry、bind mount、容器 mount namespace、unlink 后已打开对象都打破这种简化。

可以只读观察当前机器的 mount 和文件元数据：

```sh
findmnt -T .
stat .
stat -f .
```

这些命令显示当前系统的观察结果，不暴露通用磁盘格式，也不要求 root。**本课不执行裸设备读取/写入，不建议为了“看 inode”运行未审计的块设备工具。**

## 8. 检查题

1. 一个文件被 `link()` 创建了第二个硬链接后，是否有两个 inode？两个路径的 dentry 是否可以不同？删除其中一个路径会怎样？
2. `dup(fd)` 与再次 `open` 同一文件在 `f_pos` 上有何不同？如果两个线程并发用 `read` 会有什么影响？`pread` 能避免什么共享状态？
3. buffered `write` 成功、随后本进程 `read` 到新数据、设备断电后数据丢失，这三件事为何并不矛盾？
4. 新建文件后只对文件 fd 调 `fsync`，与对父目录也同步分别在保障什么？为什么这不是跨所有文件系统的事务模板？
5. 目录名查找已经命中 dentry cache 时，为什么仍可能需要权限检查？负 dentry 缓存了什么？
6. 参考 [Lab 04](../../labs/lab-04-vfs-fuse/README.md)：本实验中 inode、dentry、目录内容、数据块和 superblock 分别由什么结构/状态代表？哪些状态一旦进程退出就消失？

## 参考资料与版本边界

- Linux 文档：[VFS](https://docs.kernel.org/filesystems/vfs.html)、[路径名查找](https://docs.kernel.org/filesystems/path-lookup.html)、[ext4 数据结构与磁盘布局](https://docs.kernel.org/filesystems/ext4/)、[文件系统数据/元数据错误处理](https://docs.kernel.org/filesystems/ext4/error_handling.html)。内核内部细节按上游当前文档阅读，发行版内核可能带回移补丁。
- POSIX/Linux API：[open/openat](https://man7.org/linux/man-pages/man2/open.2.html)、[read/pread](https://man7.org/linux/man-pages/man2/read.2.html)、[write/pwrite](https://man7.org/linux/man-pages/man2/write.2.html)、[fsync/fdatasync](https://man7.org/linux/man-pages/man2/fsync.2.html)、[rename](https://man7.org/linux/man-pages/man2/rename.2.html)。
- 课程衔接：[M02-L01：POSIX 读写、页缓存与 `O_DIRECT`](../M02-LinuxIO/M02-L01-POSIX读写、页缓存与O_DIRECT.md)、[M03-L02：FUSE 用户态文件系统](M03-L02-FUSE用户态文件系统设计与实现.md)。
- Linux 上游：[VFS 文档](https://docs.kernel.org/filesystems/vfs.html)、[文件系统目录](https://docs.kernel.org/filesystems/)。具体结构与实现细节按目标内核版本核对。

**复核边界：**本课描述 Linux 常见路径与语义，不断言所有 Linux 文件系统使用 ext4 inode 布局，也不承诺某个内核版本每条路径会调用固定的私有函数。崩溃持久性以目标文件系统、挂载参数、内核、设备及同步错误为条件；本课没有进行断电测试。
