# M02：Linux I/O

## 模块目标

理解 Linux 应用程序如何通过普通文件和 socket API 发出 I/O 请求，区分文件偏移与缓存/持久化语义、向量读写和异步完成模型；能按工作负载、内核能力、正确性约束和测量条件，在 POSIX、readiness 和 `io_uring` 之间做出有依据的选择。课程不把 `O_DIRECT` 或 `io_uring` 视为性能开关。

必做实践使用纯 C、程序自行创建的临时普通文件和本地 `AF_UNIX socketpair`，不访问裸盘、分区、设备节点或远端服务，不需要 root。liburing/io_uring 专项属于可选构建，依赖明确列出，受限内核可安全跳过。

## 前置知识

- 完成 M00；理解进程、文件描述符、C 指针和基本 Linux shell。
- M01 的块设备路径有助于理解文件系统 I/O 如何到达块层，但本模块不需要 NVMe 设备。
- 会使用 C11 编译器和 GNU make；可选 io_uring 需要 Linux 内核、liburing 开发头文件/库及 pkg-config。

## 课次

| 课次 | 主题 | 产出 |
|---|---|---|
| [M02-L01：POSIX 读写、页缓存与 `O_DIRECT`](M02-L01-POSIX读写、页缓存与O_DIRECT.md) | `read`/`write` 与 `pread`/`pwrite`、短传输和错误、普通文件页缓存、写回/同步边界、`O_DIRECT` 对齐与限制 | 能编写完整传输循环，并区分 API 返回、页缓存和持久化语义 |
| [M02-L02：向量 I/O 与同步/异步接口](M02-L02-向量IO与同步异步接口.md) | `readv`/`writev`、`sendmsg`/`recvmsg`、SCM_RIGHTS、同步/异步、阻塞/非阻塞、就绪/完成模型 | 能处理跨 iovec 的部分完成，解释 stream framing 和 fd 生命周期 |
| [M02-L03：`io_uring` 队列与适用边界](M02-L03-io_uring队列完成与适用边界.md) | SQ/SQE、提交、CQ/CQE、请求/缓冲区生命周期、内核和策略能力、与 POSIX/epoll 对照及基准方法 | 完成接口选型分析；可选构建 liburing 临时文件演示，不把 io_uring 宣称为必然更快 |

## 模块实验

[Lab 03：Linux 普通文件与本地 socket I/O](../../labs/lab-03-linux-io/README.md) 对应三课。以下命令从仓库根目录运行：

```sh
make -C 'labs/lab-03-linux-io' clean all test
```

它用 C 验证显式偏移、向量 I/O、短传输循环、非法边界、临时文件清理和本地 socketpair。`O_DIRECT` 在当前普通临时文件上的尝试如不受支持会明确 SKIP；该实验不进行性能比较。可选专项需 `liburing` 开发依赖：

```sh
make -C 'labs/lab-03-linux-io' uring test-uring
```

若内核/沙箱禁止 io_uring，专项以退出码 77 报告 SKIP；无需提权，必做实验仍可完成。

## 完成标准

- 能写出正确处理 EINTR、短读写、EOF、EAGAIN 与错误的文件/stream I/O 状态机；知道普通文件与 socket 的返回语义区别。
- 能说明 `pread`/`pwrite` 的显式字节偏移与共享文件偏移差异，正确处理范围溢出。
- 能解释普通文件页缓存、脏页回写、`fdatasync`/`fsync` 与目录项同步边界；不把 `write` 成功等同于断电持久化。
- 能列举 `O_DIRECT` 的地址/偏移/长度对齐要求，并说明它仍走文件系统/内核 I/O 路径、不是零拷贝或性能保证。
- 能正确推进 `readv`/`writev` 部分完成的 iovec 游标，理解 stream socket 不保留应用消息边界。
- 能区分同步/异步、阻塞/非阻塞、readiness/completion；能解释 `io_uring` SQE/CQE、负错误码和请求生命周期。
- 能基于工作负载、队列深度、缓存状态、同步语义、CPU/尾延迟和运行时能力比较 POSIX、epoll 与 io_uring，而不是复述“必然更快”。
- 通过必做 [Lab 03](../../labs/lab-03-linux-io/README.md) 自动测试；提交记录编译器/内核/命令/输出。liburing 专项是选修，不因沙箱限制而扣必做验收。

## 安全、结果边界与来源

必做/可选程序都只访问内部创建的普通文件或本地 socket，不接受设备路径，不绑定公网端口。扩展测试不得对真实设备写入，也不得将环境受限作为提权理由。`O_DIRECT` 的 4096 B 演示对齐仅是一次请求条件；各文件系统可能要求不同对齐或不支持。io_uring 专项展示操作完成 API，不证明设备并发或性能优势。

本模块讲解 SQ/CQ、io_uring echo、fio 方法、向量 I/O 和 fd 传递，并讨论接口语义、实现安全和性能结论的适用边界。详细说明、参考来源和检查题见三课正文。

模块下三篇课文与 Lab 03 已完成；其他模块建设进度见[课程进度索引](../README.md)和[课程地图](../../课程大纲/课程地图.md)。
