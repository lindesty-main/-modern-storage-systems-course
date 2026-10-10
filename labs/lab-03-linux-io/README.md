# Lab 03：Linux 普通文件与本地 socket I/O

配套课程：[M02-L01：POSIX 读写、页缓存与 `O_DIRECT`](../../modules/M02-LinuxIO/M02-L01-POSIX读写、页缓存与O_DIRECT.md)、[M02-L02：向量 I/O 与同步/异步接口](../../modules/M02-LinuxIO/M02-L02-向量IO与同步异步接口.md)、[M02-L03：io_uring 队列与适用边界](../../modules/M02-LinuxIO/M02-L03-io_uring队列完成与适用边界.md)。

> **安全边界：**必做程序仅在 `/tmp` 下自行生成 `mkdtemp` 私有目录、`mkstemp` 普通文件和本地 `AF_UNIX socketpair`。可选 liburing 程序也只读写自己创建的普通临时文件。程序不接受文件路径参数，不打开 `/dev`、块设备、分区或 loop 设备，不创建外部监听端口，不访问网络，不需要 root。请勿把它改造成裸设备工具。

## 实验目标

- 验证 `pread`/`pwrite` 使用显式字节偏移且不移动当前文件偏移。
- 使用 `writev`/`readv` 处理多个不连续缓冲区，并正确推进部分完成后的 iovec 游标。
- 在本地流式 socketpair 上验证向量内容的顺序和字节流语义。
- 检查短读写、EINTR、EOF/零进展、错误、向量数量、长度溢出和非法偏移。
- 可选地在临时普通文件上观察 io_uring SQE/CQE 和 `user_data` 对应关系；内核不允许时可报告跳过。
- 在支持且满足对齐要求的文件系统上，尝试一次 4096 对齐的 `O_DIRECT` 临时文件读取；不支持时跳过，不做性能结论。

## 环境与构建

必做仅需 Linux、C11 编译器（GCC/Clang）、GNU make、POSIX shell 和基本 POSIX API。无需第三方库、fio、NVMe 工具、外网或 root。

```sh
make clean all test
```

成功时输出若干 `PASS` 行。例如：

```text
PASS: pread/pwrite offsets and full-transfer handling
PASS: writev/readv segment ordering and partial-vector handling
PASS: local socketpair vectored round-trip
PASS: invalid vector and offset bounds rejected
PASS: O_DIRECT aligned temporary-file read
PASS: private temporary files removed
```

`O_DIRECT` 行也可能是带原因的 `SKIP`：该文件系统/运行内核不支持所试约束时是预期结果。其它必做测试失败则返回非零。

记录实验环境：

```sh
cc --version
make --version
uname -a
```

课程候选环境见[版本与实验环境](../../课程大纲/版本与实验环境.md)，但开发机上编译通过不代表候选 OS/内核组合已经集成验证。

## 代码做了什么

### 必做普通 C：`io_lab.c`

1. `mkdtemp` 创建权限受限的私有临时目录，`mkstemp` 创建文件并用 `fstat` 确认是普通文件；文件路径由程序内部生成。每个用例结束时关闭并删除文件。
2. 通过 `writev_full` 把 header/payload/newline 三段写入文件。helper 校验 vector count、指针和长度总和；对 `EINTR` 重试；短写后推进副本 iovec 的 base/len；零进展报错。
3. 用不同长度的 `readv` segments 读回连续字节，并核验顺序。随后 `pread`/`pwrite` 使用明确偏移访问，并断言其不改变共享文件位置；`fdatasync` 返回值也会检查。
4. `AF_UNIX socketpair` 建立同进程本地流式 socket，使用向量 I/O 往返一个小 payload；无公网地址、监听 socket 或其他进程参与。
5. 负偏移、空向量和超过本实验 helper 上限的 iovec count 必须被拒绝。完整 helper 在零进展处退出，避免无休止循环。
6. `O_DIRECT` 子用例只在同一私有目录的普通文件上运行：先准备一页数据并同步，关闭 buffered fd，再以对齐内存、偏移和长度尝试 `O_DIRECT` 读取。系统/文件系统不支持时显示 `SKIP`。4096 是本例请求的对齐值，不是所有文件系统的规则。
7. 退出前检查清理私有目录。若进程被 `SIGKILL` 或机器掉电，内核无法执行用户态清理；此时若确实要清理，只能先由目录所有者检查并确认 `/tmp/m02-io.*` 属于本次实验，再删除，不要使用不加检查的通配清理命令。

### 可选 liburing：`uring_file.c`

这个专项程序并行提交两笔临时文件写入，再提交两笔读取，以 CQE `user_data` 区分请求并比较读回内容。它检查 ring 初始化、SQE 获取、部分提交、负 CQE errno、短完成、重复/未知 token 和临时文件清理。所有 buffer 在对应 CQE 收割前保持有效。

构建依赖：liburing 开发头文件/库、`pkg-config`，以及支持并允许 io_uring 的 Linux 内核。举例在 Debian/Ubuntu 上可由管理员预装 `liburing-dev pkg-config`；编译和运行程序本身不需要 root。可选测试：

```sh
pkg-config --modversion liburing
make uring test-uring
```

如果 `pkg-config` 无法找到 liburing，表示**可选依赖未安装**，不是必做测试失败。若内核或沙箱策略拒绝 `io_uring_setup`（例如 EPERM/EACCES/ENOSYS），程序退出码 77 并输出 `SKIP: io_uring unavailable or restricted`，测试脚本将其视为已识别的环境限制；不要为绕过策略而提权。成功输出：

```text
PASS: io_uring temporary-file write/read completions verified
```

专项程序不运行 server，不操作 socket、不监听端口，不打开任何设备节点。此练习理解异步完成流程，不比较吞吐/延迟。

## 练习与报告

1. 修改 iovec 长度，让第一次 `writev` 只写完前一段和后一段的一部分（可用非阻塞 socket 或故意小缓冲区扩展），手工追踪下一次调用的 base 和 len；不能丢字节或重复写。
2. 把 `readv_full` 改成只进行一次 `readv` 调用，说明为何它不能保证填满所有 iovec；分别讨论普通文件 EOF 与 stream socket 的数据边界。
3. 给可选 io_uring 程序添加一个 `user_data` 请求 token，证明乱序完成时仍能定位 buffer 和业务上下文；不应复用正在执行的 buffer。
4. 对比普通 buffered 与 `O_DIRECT` 的对齐和错误边界，说明为什么该实验不报告谁更快。任何扩展只能操作程序自行创建的临时普通文件。

报告至少包括：编译器、内核版本、`make clean all test` 输出；若运行专项，再记录 liburing 版本、`make uring test-uring` 输出及是 PASS 还是因内核/策略 SKIP。不得将运行耗时描述为硬件基准，也不得把文件系统缓存路径结果称作真实 SSD IOPS、持久性或 io_uring 加速比例。

## 清理

```sh
make clean
```

该命令仅删除当前实验目录中生成的 `io-lab` 与 `uring-file` 编译产物。程序正常结束时自行移除所创建的私有临时文件和目录。若测试中断，先确认目录路径归属再清理；不要对任意 `/tmp` 路径运行危险的批量删除。

## 文件索引

- `io_lab.c`：不依赖第三方库的普通 C/POSIX 实验程序。
- `uring_file.c`：可选 liburing 专项，只操作临时普通文件。
- `Makefile`：默认 `all`/`test` 与显式可选 `uring`/`test-uring` 目标。
- `test.sh`、`test-uring.sh`：自动验证必做和可选专项。
