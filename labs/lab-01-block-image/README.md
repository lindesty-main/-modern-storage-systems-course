# Lab 01：安全的 LBA 块映像

本实验是 M01-L01 的本地配套材料。用带有本实验自有 magic/header 的**普通文件**模拟通用 LBA 空间，观察字节偏移、整块覆盖和边界拒绝。

> **安全边界：**不访问裸盘、分区、loop 设备或 NVMe 命令接口，不要求 root。程序只接收普通文件，并在 `magic/version/header checksum/几何/文件长度` 全部有效时才读写。初始化使用 `O_EXCL`，不覆盖已有路径。请勿把程序改成 `/dev/...` 设备写入工具。

## 能力范围与限制

- C11 + POSIX 文件 I/O；GCC 13 是课程主线候选编译器。构建仅需 GCC 和 GNU make；运行 `make test` 还需要 POSIX shell、GNU coreutils（如 `dd`、`cmp`、`truncate`）、`awk` 和 `grep` 等基础环境。不需要 NVMe SDK 或第三方库。
- 支持块大小 **512 或 4096 字节**，块数 **1–65536**，最大数据区 256 MiB。映像开头有 64 字节 header；数据区从文件偏移 64 开始。
- `write` 每次接受一个命令行文本 payload，剩余空间补零；再次写同一 LBA 会覆盖该完整块。`read` 显示前最多 64 字节的 hex 和转义文本。
- `nlb BLOCK_SIZE NLB` 仅演示 NVMe NVM Read/Write 的零起计数字段换算（块大小 512/4096，NLB 0..65535），输出实际块数和数据字节数；不访问设备，也不表示设备最大传输长度或 ioctl 一定接受该请求。
- 程序显式计算和反向检查 `file_offset = HEADER_SIZE + LBA × block_size`；所有读写检查 LBA 边界。
- I/O 循环处理 `EINTR` 和短读/短写；写数据后调用并检查 `fdatasync`。初始化错误路径尽力删除本次 `O_EXCL` 创建的残缺文件。程序不 `fsync` 父目录，也不承诺新文件目录项的崩溃持久性。
- header 的 FNV-1a 校验仅用于发现意外损坏，不是认证或抗恶意篡改机制。程序假设单用户教学操作，不防止另一进程并发修改映像。
- 这是**通用 LBA/文件映像模型**，不是 NVMe 控制器、命令队列、FTL、闪存物理页、掉电恢复或性能仿真。普通文件 I/O 会经过主机内核和文件系统路径；本实验不测量性能，也不证明真实硬件持久性。

## 构建与自动化验证

在本目录运行：

```sh
make clean all test
```

`make test` 会编译 `block-image.c` 并运行 `test.sh`。测试在独立 `mktemp` 目录创建映像，覆盖以下路径：

1. 512 B 和 4096 B 两种几何初始化、`info` 输出及边界 LBA 映射；
2. 写入、读回、零填充和同一 LBA 的整块覆盖；另将一个块先写满 512 字节非零内容，再用短 payload 覆盖，并比较整个余量确认均为零；
3. NLB 0/7 在 512/4096 B 块大小下的字段换算、16 位上限 NLB=65535 的合法换算，以及非法块大小、非法输入和 NLB 越界；
4. 读/写越界、负数/非法 LBA、超长 payload、非法块大小/块数和最大数量限制；
5. `O_EXCL` 拒绝现有文件；
6. magic/header checksum 损坏、合法 header 但数据区截短（由总文件长度验证拒绝）、非映像普通文件、目录、符号链接和 `/dev/null` 在打开前按文件类型拒绝。

成功时输出类似：

```text
PASS: init/info, LBA mapping, read/write, zero padding after nonzero overwrite, NLB conversion, bounds, invalid input, O_EXCL, truncated data, and file/header safety
```

检查/报告可复现环境：

```sh
gcc --version
make --version
uname -a
```

提交实验报告时写明以上版本、执行的 `make clean all test` 命令和 PASS 结果。课程建议基线是环境文档列出的 Ubuntu Server 24.04.5 amd64 / GA Linux 6.8.0-146.146 / GCC 13.3 候选组合；它是**建议矩阵，不是已完成的集成验证声明**。在别的发行版构建成功只证明该本机样例通过，不能据此声称候选矩阵、NVMe ioctl 或设备驱动已验证。

## 手工操作

先在本目录构建，再选一个不存在的临时文件路径：

```sh
set -eu
make
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

这个流程在私有临时目录内创建新文件；退出时只清理该目录。`init` 使用 `O_EXCL`，路径意外已存在时安全失败而不是覆盖。

关键预期输出（格式可能随程序版本略有变化）：

```text
block_size=512
block_count=8
LBA range=0..7 (zero-based)
write complete: LBA=2 -> image byte offset=1088 ... mapping verified ...
read complete: LBA=2 -> image byte offset=1088; mapping verified
hex preview (64/512 bytes): 68 65 6c 6c 6f 00 ...
text preview: hello
```

偏移为 `64 + 2×512 = 1088`：前 64 字节是映像格式 header，块 LBA 2 位于数据区第 2 块。写入 `overwritten` 后再读，应显示新内容而非 `hello`，因为操作以完整块写入且其余字节清零。

也可在另一个独立临时目录练习 4096 B 几何：

```sh
set -eu
TMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/m01-4k.XXXXXX")
trap 'rm -rf -- "$TMP_DIR"' 0
trap 'exit 1' HUP INT TERM
IMAGE="$TMP_DIR/demo.img"
./block-image init "$IMAGE" 4096 2
./block-image write "$IMAGE" 1 block-1
./block-image read "$IMAGE" 1
```

LBA 1 文件偏移是 `64 + 1×4096 = 4160`。`init` 使用 `O_EXCL`，路径意外存在时失败；上述私有目录只会清理本次创建的映像，不会触碰预先存在的数据。

## 故障排查与清理

- `几何参数超出限制`：只接受 512/4096，块数范围为 1–65536。
- `路径必须指向普通文件`：预期安全拒绝；不要用设备文件、目录或符号链接绕过限制。
- `magic 不匹配`、header 校验失败、文件长度不符：映像不是本程序创建的，或已被修改/截断；使用新文件重新初始化，不要尝试把任意文件“修复”为映像。
- `LBA 越界`：块数为 N 时合法编号是 0 到 N−1。
- `File exists`：`O_EXCL` 保护预期生效；`info` 验证归属，确定不再需要后再删除。
- `fdatasync` 失败：写请求可能已经修改文件，但持久化结果不确定；保留错误输出，不要把该映像当成可靠测试结果。
- `make clean` 只删除编译产物 `block-image`。手工运行创建的映像须明确检查路径后自行删除；自动化测试在退出时清理自己的临时目录。

## 文件索引

- `block-image.c`：映像格式、安全检查、CLI 与块级读写实现。
- `Makefile`：GCC 构建及 `make test` 入口。
- `test.sh`：安全的临时文件自动化自测。
- 关联课文：[M01-L01：块设备、LBA 与安全的块级 I/O](../../modules/M01-设备与块存储/M01-L01-块设备与LBA.md)。
