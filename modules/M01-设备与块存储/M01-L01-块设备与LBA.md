# M01-L01：块设备、LBA 与安全的块级 I/O

> 状态：审阅样章　｜　建议课时：105–115 分钟（含本地实验）　｜　适用环境：Linux、C/POSIX；课程候选版本见[版本与实验环境](../../课程大纲/版本与实验环境.md)。本课实验只操作程序创建的普通文件，不访问真实块设备。

## 学习目标

学完本课后，你应能：

1. 从应用 API 画出到 Linux 块层、驱动、NVMe 控制器及完成通知的读写路径，并指出每层的职责与边界。
2. 区分 LBA、namespace 逻辑块大小、逻辑扇区、物理扇区、Linux `bio`/块层 sector 和字节偏移；给定块大小计算任意 LBA 的字节位置。
3. 解释 NVMe namespace 与 Linux `/dev/nvme…` 节点的关系，以及 NLB/`nblocks` 零起计数的含义、适用接口和长度限制。
4. 使用 `pread`/`pwrite` 在带有自有格式头的普通文件映像上安全地读写一个块，并验证映射、边界、覆盖和错误处理。
5. 识别关于 512 B 假设、POSIX 与 NVMe ioctl 路径、裸盘 `dd` 和 `nblocks` 的常见误区与安全风险。

## 先修与教学节奏

先修：会使用 Linux shell，理解 C 指针、数组、整数和文件描述符；了解进程调用系统调用即可，不要求 NVMe 硬件、root 权限或内核编程经验。

| 时间 | 内容 |
|---|---|
| 0–10 分钟 | 场景：文件系统如何定位数据，为什么设备接口采用块地址 |
| 10–25 分钟 | 从应用到设备的分层路径；读请求和写请求怎样完成 |
| 25–45 分钟 | LBA、逻辑/物理扇区、512B `sector` 与字节偏移换算 |
| 45–60 分钟 | namespace、NVMe NLB/`nblocks`，POSIX/ioctl 接口边界 |
| 60–65 分钟 | 休息与映像实验安全检查 |
| 65–90 分钟 | 编译并操作普通文件映像：几何、映射、覆盖、边界、header 拒绝 |
| 90–105 分钟 | 读代码、解释结果、排查失败及讨论持久化边界 |
| 105–115 分钟 | 练习题与复盘；若课时只有 90 分钟，课后完成练习 |

## 场景：文件系统要把“第几个块”变成设备请求

设想要把文件系统的超级块放到介质开头，再用位图记录哪些数据块已分配。文件系统不能只说“把超级块写进去”，还必须定义它在哪里、占多少字节、越界怎样处理，以及写完后何时能认为请求完成。块设备将存储空间呈现为可定位的固定大小逻辑块；上层据此组织读写请求，而不是依赖闪存芯片内部的物理页地址。

“块”并不自动意味着 512 字节，也不意味着一个系统调用只处理一个块。LBA 是逻辑块地址（Logical Block Address），即在一个逻辑地址空间中的块编号；它本身不是字节地址。一个 I/O 可以从起始 LBA 开始连续传输多个逻辑块。逻辑地址到闪存 NAND 页、通道或芯片的映射通常由设备固件/FTL 管理，不能从 LBA 推导出物理位置。

## 分层数据路径：接口不同，不代表内核消失

```text
应用 / 文件系统 / 存储引擎
  │  read/write、pread/pwrite（字节偏移）；或特定设备 ioctl
  ▼
系统调用与 VFS
  ├─ 普通文件：文件系统把文件偏移映射到文件数据块；通常经过页缓存
  └─ 块设备节点：块设备文件操作；不会因此绕过 Linux 块层
  ▼
Linux 块层：bio → request / blk-mq 队列 → 调度、合并、分发
  ▼
NVMe 主机驱动：提交队列、doorbell、DMA 映射与请求跟踪
  ▼
控制器 / namespace：执行命令，访问控制器管理的介质映射
  │
  └─ 完成队列 / 中断或轮询 → 驱动结束请求 → 系统调用返回/异步完成
```

图中是概念路径，具体文件系统、驱动、队列数量、调度策略和轮询方式会依内核、设备及配置变化。读普通文件时，数据可能命中页缓存，系统调用无需每次都发设备 I/O；未命中时文件系统才组装块请求。写普通文件常先修改缓存中的脏页，稍后回写；`fdatasync`/`fsync` 会请求把相应数据/元数据同步到持久化层，但真正的断电保证还取决于文件系统、设备缓存、flush/FUA 支持和设备是否正确兑现承诺。

块层把上层请求规范化并分发给驱动，驱动把命令放入设备队列。NVMe 是队列式协议：主机提交命令后，控制器执行并产生完成项；驱动在中断或轮询中观察完成，唤醒等待者或完成异步请求。同步 `pread` 返回表示该次调用已按内核和设备接口规则完成，并不等同于“数据已永久写入 NAND”。系统调用完成、设备命令完成、缓存同步完成和掉电后可恢复，是不同的承诺边界。创建映像时 `fdatasync` 文件本身也不等同于同步父目录项；若应用需要保证新文件名在崩溃后仍存在，通常还要按平台约定同步包含该文件的目录。本实验不覆盖该级别的崩溃一致性。

## 地址与单位：先写公式，再代数字

### 四个容易混淆的概念

- **LBA**：逻辑块编号，通常从 namespace 的 0 起。某个 LBA 对应多少字节取决于所选逻辑块格式。
- **namespace 逻辑块大小 / Linux logical block size**：主机可寻址逻辑块的字节数。NVMe namespace 的 LBA 格式由 namespace 格式参数选择；常见值可能是 512 或 4096 字节，但不能假设所有 NVMe 都是 512 B。Linux 设备可查看 `/sys/block/<设备>/queue/logical_block_size`。
- **物理块/物理扇区大小**：设备或驱动报告的底层物理写入/对齐粒度，可能不同于逻辑块大小；可查看 `physical_block_size`。512e 类设备可以对主机暴露 512 B 逻辑扇区、以 4096 B 物理扇区为对齐建议。错位写可能带来读改写或性能影响，但具体行为依设备而异。
- **字节偏移**：软件接口常用的绝对字节位置。对于 namespace 数据区，若逻辑块大小为 `L`，则 `byte_offset = LBA × L`。若操作的是含 header 的映像文件，还要加上映像自身的数据区起点。

换算例子：若逻辑块大小是 512 B，则 LBA 7 对应 namespace 字节偏移 `7 × 512 = 3584`；若 namespace 格式是 4096 B，则 LBA 7 对应 `7 × 4096 = 28672`。LBA 10 的位置分别是 5120 B 和 40960 B。一个 4096 B 的上层块在 512 B 格式下跨 8 个 LBA；在 4096 B 格式下只跨 1 个 LBA。不要把“上层文件系统块”误称为介质 LBA。

Linux 块层 `bio` 使用的 `sector` 传统单位是 **512 字节扇区**（源码中常见 `SECTOR_SHIFT=9`），它不是设备的 `logical_block_size`，也不表示设备 LBA 一定为 512 B。若一个 4 KiB namespace LBA 的字节起点是 4096，那么用 512 B 单位表达时位置为 sector 8；这只是块层坐标单位换算，不会把设备逻辑块格式改成 512 B。系统还会检查设备逻辑块大小、对齐和请求边界。

### NVMe namespace 与 Linux 设备节点

NVMe 控制器可以暴露一个或多个 namespace；namespace 是带有独立逻辑地址空间、容量和 LBA 格式的可访问存储对象。Linux 常见 `/dev/nvme0` 是控制器管理/命令接口之一，`/dev/nvme0n1` 通常是控制器 0 上 namespace 1 的块设备节点。命名规律和可用 ioctl 受内核、设备及配置影响，不能只凭文件名推断 namespace 数量或格式。

可在有设备且仅做只读检查时，查看 `lsblk -o NAME,TYPE,SIZE,LOG-SEC,PHY-SEC`、`/sys/block/nvme0n1/queue/logical_block_size` 与 `physical_block_size`。namespace 的 LBA 格式和容量还可由设备管理工具/Identify Namespace 信息核对。**本课不对这些设备节点执行任何写操作，也不要求学生拥有真实 NVMe。**

## 多块请求与 `nblocks` / NLB 的零起计数

NVMe NVM Read/Write 等命令格式定义了起始 LBA（SLBA）和 NLB 字段。NLB 编码的是“块数减一”，也就是零起计数：对于定义该字段的命令，`实际逻辑块数 = NLB + 1`，`传输字节数 = (NLB + 1) × namespace_LBA_size`。Linux `struct nvme_user_io` 的 `nblocks` 对应这类编码；例如 `nblocks=0` 是 1 个逻辑块，`nblocks=7` 是 8 个逻辑块。若 LBA 大小为 512 B，`nblocks=7` 是 4096 B；若 LBA 大小为 4096 B，同一字段值是 32768 B。

这里的 `nblocks` 不是“把单个 LBA 改大”的参数，它描述一次命令传输多少个连续逻辑块。若起始 LBA 是 `s`、实际块数是 `c`，合法条件至少要求 `c ≥ 1` 且 `s + c` 不超过该 namespace 的可寻址范围；实际代码还须用不会溢出的写法检查边界，例如先保证 `s < capacity`，再检查 `c <= capacity - s`。

**限制与命名边界：**NVMe UAPI 的具体结构字段宽度、Linux ioctl 能力、命令集、namespace 格式、控制器最大传输量（如 MDTS）及驱动验证都会限制可执行请求。`nblocks` 这个名字也不是所有 POSIX、NVMe、SPDK 或其他用户态接口都使用的同一参数语义。不能由“字段是 16 位”推出所有设备都允许任意 65536 块请求；必须按具体命令、接口和设备限制校验。配套程序的 `nlb BLOCK_SIZE NLB` 子命令只在 512/4096 B 块大小和 NLB 0..65535 范围内打印零起计数换算结果；它不打开设备、不提交命令，不代表设备的最大传输长度或 ioctl 会接受该请求。

## POSIX 文件 I/O 与 NVMe ioctl：接口边界

`pread(fd, buf, len, offset)` / `pwrite(fd, buf, len, offset)` 用**字节偏移**访问文件；它们不依赖共享文件偏移，也无需先 `lseek`。若 `fd` 指向普通文件，调用通过 VFS/文件系统访问文件，通常会与页缓存交互；若 `fd` 指向 Linux 块设备，字节偏移 I/O 仍进入内核块设备路径和块层，不能简单概括为“普通 IO 只能经文件系统”。`read/write` 也不等于每次完整传输：可能短读/短写，可能被 `EINTR` 中断，程序要循环处理。

NVMe ioctl 是特定 Linux UAPI/驱动提供的设备命令接口。它仍由内核校验参数、映射缓冲区并经驱动与控制器交互；不能笼统说它“直通并必然绕过所有缓存”。命令接口可能绕过普通文件数据路径中的部分语义，但是否经页缓存、是否能提交命令、权限和同步保证均取决于具体 ioctl、设备节点、内核版本与命令。选择 ioctl 的理由是使用特定 NVMe 命令语义，而不是因为它天然更快或一定更直接。

### 追踪内核实现（代码阅读，不运行设备）

本节只阅读源码，不运行 NVMe ioctl、不打开设备，也不需要 root。源码阅读基线选上游 Linux **v6.8 tag**。对 namespace 块设备路径，可沿着下面的分发关系阅读：

```text
NVME_IOCTL_SUBMIT_IO
  → nvme_ioctl()
  → nvme_ns_ioctl()
  → nvme_submit_io()
```

v6.8 的 UAPI 在 `struct nvme_user_io` 中把 `nblocks` 定义为 `__u16`，并用 `NVME_IOCTL_SUBMIT_IO` 标识该 ioctl。`nvme_ioctl()` 将 namespace 命令分派给 `nvme_ns_ioctl()`；后者对该 ioctl 调用 `nvme_submit_io()`。在 `nvme_submit_io()` 中，Read/Write 命令的数据缓冲区长度按下面的字段换算：

```text
length = (nblocks + 1) << ns->lba_shift
```

这里 `lba_shift` 表示逻辑块字节大小的以 2 为底的指数。注意上式是便于教学的记号：上游 v6.8 源码的具体写法是 `length = (io.nblocks + 1) << ns->head->lba_shift`。同一函数把 `io.nblocks` 原值编码到命令 `rw.length` 字段（仅做字节序转换），所以 NLB 7 表示 8 个块；例如 LBA 大小 512 B 时 `length=8<<9=4096`，4096 B 时 `length=8<<12=32768`。此处展示的是字段/缓冲区长度换算，不是设备执行结果。

长度算出来不等于设备肯定接受请求：驱动还会检查命令字段并处理用户缓冲区映射和提交，命令是否可用还受 namespace、访问权限、具体内核实现、控制器/设备能力及传输限制（例如 MDTS）影响；设备也可能返回命令错误。NLB 字段范围或本地程序算出的 `bytes` 都不能替代这些校验，也不能推出某设备支持的最大传输长度。

版本化源码链接：[Linux v6.8 `nvme_submit_io()` 实现](https://github.com/torvalds/linux/blob/v6.8/drivers/nvme/host/ioctl.c#L207-L267)、[`nvme_ioctl()` / `nvme_ns_ioctl()` 分发](https://github.com/torvalds/linux/blob/v6.8/drivers/nvme/host/ioctl.c#L549-L593)、[UAPI `nvme_user_io` 结构](https://github.com/torvalds/linux/blob/v6.8/include/uapi/linux/nvme_ioctl.h#L12-L25) 与 [ioctl 宏定义](https://github.com/torvalds/linux/blob/v6.8/include/uapi/linux/nvme_ioctl.h#L97-L100)。这些是上游 tag 源码阅读依据，不是本课对实际设备的实测。课程候选 Ubuntu 包内核 **6.8.0-146.146** 可能包含发行版补丁；其包级实现仍须在课程发布前下载源码包并复核，不能仅凭上游 tag 声称完全一致。

对块设备 fd，POSIX 接口按字节偏移访问，应用可以按已知逻辑块大小自行计算块边界，不需要 NVMe 专用 ioctl 才能读写块范围。ioctl 也不是所有块设备或 NVMe 命令的通用替代方案；接口是否涉及页缓存或其他内核路径，须按文件描述符类型和具体 UAPI 判断。

块设备读写必须检查分配失败、短读/短写和同步错误。512 B 不是所有 namespace 的逻辑 LBA 大小；必须读取目标设备的格式与接口定义。若特定 NVMe ioctl 的 NLB/`nblocks` 字段采用零起计数编码，字段值 `1` 表示 **2 个 LBA**，字段值 `4` 表示 **5 个 LBA**；这项解释只适用于相应命令字段，不能仅凭变量名推断实际块数。

特别警告：`sudo dd if=/dev/zero of=/dev/nvme…` 会覆盖目标设备上的数据与分区/文件系统元数据，**禁止运行**。裸盘的 `cat`、`dd`、错误 LBA 或设备名都可能造成不可逆数据损坏；本课不需要 root，不打开真实设备写入，使用下方带验证头的临时映像替代。

## 代码导读：安全地模拟一个通用 LBA 映像

配套实验目录：[`../../labs/lab-01-block-image/`](../../labs/lab-01-block-image/)。核心文件 `block-image.c` 使用固定 64 字节 header，包含 magic、格式版本、块大小、块数、数据区长度和轻量校验值。它只允许 512/4096 B 块、1–65536 个 LBA；映像最大数据区是 256 MiB。创建使用 `O_CREAT|O_EXCL`，所以存在路径绝不覆盖；访问时先检查普通文件及 inode，再验证 magic、版本、header 校验、几何参数和文件总长度。块设备、目录、FIFO、符号链接、普通非映像文件和损坏 header 都会拒绝。

逐段阅读：

1. `valid_geometry()` 对块大小、数量、乘法、header 加法及 `off_t` 范围作检查；`parse_u64()` 仅接收十进制数字，拒绝负号和尾随字符。
2. `encode_header()` / `validate_header()` 定义本实验自有的磁盘格式。校验值用于发现意外损坏，不是密码学认证；拥有者能重写 header 的恶意文件不在威胁模型内。
3. `open_image()` 使用 `lstat` 和 `fstat` 检查普通文件与 inode，避免符号链接和检查/打开时简单替换；仍按单用户、无并发恶意改写的教学环境使用。
4. `calculate_offset()` 实现并反向验证 `映像内偏移 = 64 + LBA × block_size`，检查 LBA 范围和块末端不越过数据区。它会在读和写时打印映射结果。
5. `command_write()` 把 payload 复制进清零的整块缓冲区，`pwrite_full()` 将一个完整块写至映像对应位置；后一次写同一 LBA 会覆盖整块，未使用字节为零。写后调用 `fdatasync()` 并检查错误。`fdatasync` 针对该文件的数据及读取这些数据所需的文件长度元数据；本实验没有同步父目录，也不宣称突然掉电后的目录项或设备固件行为有额外保证。
6. `command_read()` 用 `pread_full()` 读回整块，显示前 64 字节十六进制和转义文本。读写循环会在 `EINTR` 重试，也会处理短读/短写；意外零长度进展作为 `EIO` 报错。

映像中的数据起点不是文件字节 0：前 64 字节是 header，所以 512 B 几何下 LBA 1 映射到文件偏移 `64 + 1×512 = 576`。这个额外 64 B 是实验文件格式约定，不属于 NVMe LBA 定义。实验程序不模拟 NVMe 控制器、命令队列、FTL、闪存页、断电行为或真实性能；普通文件的 `pread/pwrite` 经过主机操作系统和文件系统缓存，结果不能拿来宣称设备 IOPS、NVMe ioctl 性能或硬件持久性。

## 实验：初始化、写入、覆盖和安全拒绝

完整操作、自动化用例、故障排查及限制见[实验说明](../../labs/lab-01-block-image/README.md)。在该目录运行：

```sh
set -eu
make clean all test
TMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/m01-demo.XXXXXX")
trap 'rm -rf -- "$TMP_DIR"' 0
trap 'exit 1' HUP INT TERM
IMAGE="$TMP_DIR/demo.img"
./block-image init "$IMAGE" 512 8
./block-image info "$IMAGE"
./block-image write "$IMAGE" 2 hello
./block-image read "$IMAGE" 2
./block-image write "$IMAGE" 2 overwritten
./block-image read "$IMAGE" 2
./block-image nlb 512 0
./block-image nlb 512 7
./block-image nlb 4096 7
```

预期关键输出包括 `block_size=512`、`block_count=8`、LBA 2 的文件偏移 `64 + 2×512 = 1088`、`mapping verified`、读回 `hello`，以及第二次读回只出现 `overwritten`。覆盖不是追加：同一个 LBA 的整块被新 payload 和零填充替换。`read` 只展示有限 preview，空字节会终止文本预览但仍能从 hex 查看；这是文本表示，不代表后续数据未读到。字段换算命令分别输出 `nlb 512 0` 的 `actual_blocks=1` / `bytes=512`、`nlb 512 7` 的 `8` / `4096`，以及 `nlb 4096 7` 的 `8` / `32768`；它们只演示换算，不发设备命令。

自动测试 `make test` 在 `mktemp` 创建的临时目录中覆盖初始化/几何显示、LBA 偏移、读写与零填充、先以 512 字节非零数据填满再短 payload 覆盖整个余量归零、NLB 字段换算和非法输入、越界与超长 payload、非法几何、`O_EXCL`、损坏 magic/校验值、合法 header 但数据区截短时由文件长度校验拒绝、非映像文件、目录、符号链接和 `/dev/null` 非普通文件拒绝。测试清理只移除自己的临时目录。不要把课程程序改造成可以接受裸设备的工具。

## 常见误区与故障排查

- **“NVMe LBA 一律 512 B。”** 错。看 namespace 所选格式和设备暴露的逻辑块大小；512 B、4 KiB 都可能存在，设备还可能暴露独立物理块/对齐信息。
- **“`nblocks=1` 就是一个块。”** 在本课讨论的 NVMe NLB/`nvme_user_io` 字段语境下，字段值是零起计数，1 表示 2 块。先确认字段定义，再看它是否映射到其他 API 的字节长度或块数。
- **“POSIX IO 只适合文件，NVMe ioctl 直达硬件。”** `pread/pwrite` 对块设备仍由内核块层处理；NVMe ioctl 也由内核驱动实施。两类接口语义和权限不同，不可用一句“缓存/直通”概括。
- **`init` 显示路径已存在：**这是 `O_EXCL` 安全预期；换一个新临时文件名，不要删除不确定归属的数据。
- **`拒绝映像`：**确认操作对象是由 `init` 新建且未被编辑/截断的普通文件；不要用普通文本文件或设备节点替代。
- **LBA 越界或几何参数非法：**LBA 从 0 开始，有 `block_count` 个块时最大合法值是 `block_count-1`；只接受 512/4096 和 1–65536 块。
- **找不到 `gcc` 或 make：**检查 `gcc --version`、`make --version`；不要求安装 NVMe 管理工具，也不要求 sudo。若本机编译器较旧，记录版本后使用支持 C11/POSIX 的 GCC 13 环境复核。
- **同步失败或进程中断：**本实验不保证一个普通文件块写入具备断电原子性。失败后不要假设写前或写后内容一定完整；重新创建映像并重跑测试。

## 练习题

1. namespace LBA 大小分别为 512 B 和 4096 B 时，LBA 123 的 namespace 内字节偏移各是多少？若 Linux 块层以 512 B sector 计数，4096 B namespace 的 LBA 123 对应多少 sector？
2. namespace 有 1000 个 LBA（编号 0–999），起始 SLBA=990，实际请求 10 块是否合法？请求 11 块呢？用不溢出的条件写出检查式。
3. 若 `nblocks=15` 且 namespace LBA 大小为 4 KiB，命令数据长度是多少？列出还需要考虑的至少两项设备/接口约束。
4. 为什么映像 LBA 2 对应文件偏移 1088 而不是 1024？如何修改本实验格式才能移动 header，而不改变 LBA 的定义？
5. 普通文件上的 `pwrite` 返回成功、`fdatasync` 返回成功，分别说明了什么？还不能证明什么？
6. 阅读自动测试中“先写满非零块再用短 payload 覆盖”与“合法 header 但数据区截短”的用例，说明它们分别能发现哪类实现错误；不得通过访问 `/dev` 设备来验证。

## 参考资料与版本边界

- [NVM Express 官方规范入口](https://nvmexpress.org/specifications/)：以课程发布时采用的 NVM Command Set 规范核对 NLB、SLBA、namespace 格式和命令限制；具体字段/条款随规范版本注明。
- Linux UAPI 与实现阅读基线：[Linux v6.8 NVMe ioctl 头文件](https://github.com/torvalds/linux/blob/v6.8/include/uapi/linux/nvme_ioctl.h)、[Linux v6.8 NVMe host ioctl 路径](https://github.com/torvalds/linux/blob/v6.8/drivers/nvme/host/ioctl.c)。课程候选包内核 6.8.0-146.146 的打包源码和补丁仍须发布前复核，不把实现细节当成协议普遍规则或设备实测结果。
- Linux 内核文档：[blk-mq](https://docs.kernel.org/block/blk-mq.html)、[block queue sysfs 属性](https://docs.kernel.org/block/queue-sysfs.html)；sector 单位还可核对内核 [`blk_types.h`](https://github.com/torvalds/linux/blob/master/include/linux/blk_types.h) / [`blkdev.h`](https://github.com/torvalds/linux/blob/master/include/linux/blkdev.h)。
- POSIX/Linux API：[pread/pwrite](https://man7.org/linux/man-pages/man2/pread.2.html)、[fdatasync/fsync](https://man7.org/linux/man-pages/man2/fdatasync.2.html)、[open](https://man7.org/linux/man-pages/man2/open.2.html)。
- 本地安全实验：[lab-01-block-image README](../../labs/lab-01-block-image/README.md) 及其 C 实现只操作程序创建的普通文件映像。

**版本复核说明：**课程环境文档把 Ubuntu Server 24.04.5 amd64、GA Linux 6.8.0-146.146、GCC 13.3 等列为建议候选矩阵，明确尚未整体集成验证。本样章不声称候选 OS/内核矩阵已实测，也不依赖特定设备。实验构建结果应单独记录本机 `uname -a`、`gcc --version`、`make --version`；它只验证本地普通文件实验，不验证 NVMe ioctl、真实硬件、设备掉电持久性或候选矩阵的集成兼容性。
