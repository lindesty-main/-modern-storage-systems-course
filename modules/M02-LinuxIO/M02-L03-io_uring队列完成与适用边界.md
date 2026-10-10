# M02-L03：`io_uring` 的队列、完成语义与适用边界

> 状态：正文完成　｜　建议课时：110–120 分钟（含可选实验）　｜　前置：M02-L01、M02-L02；理解文件描述符、向量 I/O 和异步请求生命周期。必做实验只访问程序创建的临时普通文件与本地进程资源，不需要 root。

## 学习目标

学完后，你应能：

1. 画出 `io_uring` 的 SQ/SQE、内核执行、CQ/CQE 及应用回收流程，并区分“准备”“提交”“完成”。
2. 正确检查提交结果、CQE 返回值、请求身份和 buffer 生命周期，处理队列空间不足、部分完成与错误。
3. 解释共享 ring 内存能减少哪些交互，又为何不等于传输数据零拷贝或保证真实硬件异步。
4. 比较同步 `pread`、readiness `epoll` 与 completion `io_uring`，按工作负载而非口号选型。
5. 运行纯 C 必做实验，并在依赖和内核允许时构建 liburing 可选演示；明确 `io_uring` 不必然更快。

## 从系统调用到用户态 ring

`io_uring` 是 Linux 提供的一组异步 I/O 接口。应用先通过 `io_uring_setup` 建立 ring；内核与用户态映射共享提交队列（SQ）和完成队列（CQ）相关内存。应用把请求描述写为 SQE（Submission Queue Entry），提交后内核执行；完成时内核发布 CQE（Completion Queue Entry），其中携带结果和应用用于关联请求的 `user_data`。常用 liburing 包装这些接口并管理 ring 内存。

```text
应用线程                         Linux 内核
┌─────────────────┐            ┌────────────────────────┐
│ get SQE / prep  │  SQE / SQ  │  检查请求、执行或排队   │
│ read/write/...  ├───────────►│  文件系统 / socket /   │
│                 │            │  worker / 驱动等路径    │
│ 取 CQE / 处理   │  CQE / CQ  │                        │
│ cqe_seen 回收   │◄───────────┤  发布完成与结果         │
└─────────────────┘            └────────────────────────┘
```

SQ/CQ 使用共享映射与环形索引，减少了逐请求调用内核提交/取回结果的必要性；应用仍要按接口规则通知内核有待处理 SQE、观察完成并推进 CQ。`io_uring_setup`、`io_uring_enter` 和 `io_uring_register` 是相关系统调用，但 register 属于可选能力，并不是每个应用都必经。具体映射布局、SQ/CQ 是否共用 ring、内存排序和辅助逻辑由内核 ABI 与 liburing 版本处理，不应手动用“移动指针”类比替代接口契约。

### 一次操作的生命周期

常见 liburing 流程如下：

```c
struct io_uring_sqe *sqe = io_uring_get_sqe(&ring);
if (sqe == NULL) {
    /* SQ 中没有可用槽位：先提交/收割，或施加背压 */
}
io_uring_prep_read(sqe, fd, buffer, length, offset);
sqe->user_data = request_id;

int submitted = io_uring_submit(&ring); /* 正数数量或负的 -errno */
/* 等 CQE；cqe->res 为字节数/结果，或负 errno */
io_uring_cqe_seen(&ring, cqe);           /* 已消费后推进 CQ */
```

重点不是 API 名字，而是四个边界：

1. **取槽与准备不同。** 应用先 `io_uring_get_sqe` 得到 SQE，再由 `io_uring_prep_*` 填字段；prep 函数不会替应用自动分配 SQE。取不到槽位时不能覆盖仍待提交的 SQE，需提交、回收或施加背压。
2. **准备不等于提交。** SQE 填好后才调用 submit 路径。返回值可能小于期望数量；负值是 `-errno` 编码，不是普通 libc `-1` 并依赖全局 errno 的语义。调用方要明确处理部分提交。
3. **提交不等于完成。** 请求可在之后某时完成，顺序不必和提交顺序一致。用唯一 `user_data` 或稳定请求对象关联上下文，不能把 CQE 序号直接当作请求序号。
4. **CQE 不等于成功。** `cqe->res < 0` 表示负 errno；非负值可能是传输字节数，也可能是 `accept` 等操作的返回 fd。读写可能短完成，须按操作语义检查并续作或报错。每个被消费的 CQE 要恰当回收；不能重复处理同一 CQE。

请求的 buffer、fd 和业务状态必须至少活到该请求完成或确认取消。并发写不能复用仍被内核使用的 buffer；并发读不能让两个请求覆盖同一缓冲区而无明确同步。取消本身也是异步流程：收到取消请求不代表原操作已经停止，仍需收割最终状态。

## “异步”的实现不等于设备必定异步

`io_uring` 允许应用提前提交多个独立请求，并在等待期间做其他工作；这提供了组织并发和减少系统调用/批处理开销的机会。实际执行路径依 opcode、文件类型、标志、内核版本和驱动能力不同：操作可能直接推进、交给内核 worker（io-wq）或受文件系统/设备同步约束。对于 buffered 普通文件 I/O，缓存命中甚至可能很快完成；缓存未命中和具体内核实现则会影响实际执行。`io_uring` 的 API 异步，不表示每个请求都由硬件独立并行执行。

SQ 深度、应用并发度、文件系统队列、块层队列和硬件队列也不是同一个数字。配置 128 个 SQE 不代表 SSD 同时执行 128 个命令。队列过浅可能利用不了可并行的请求；队列过深会增加内存、排队与尾延迟，甚至制造更多拥塞。完成队列可能溢出或要求额外处理；生产代码还应按目标 liburing/内核文档监控并处理 overflow、资源耗尽与关闭流程。

## 不把共享队列说成“零拷贝”

SQE/CQE 和 ring 索引的共享映射能避免为每条描述符都重复拷贝一整份控制结构，也可批量提交和收割。这不意味着文件/网络 payload 没有复制。普通 buffered `read`/`write` 仍可能在用户内存与页缓存间复制；socket 和协议栈也有自身的数据处理。注册缓冲区/固定文件等机制可减少部分重复注册或映射成本，但仍非通用零拷贝。

内核提供特定零拷贝相关操作/标志时，收益与约束必须按该 opcode、内核版本、设备/网络栈、buffer 生命周期和完成语义单独阅读。不能因为 ring 是 `mmap` 就声称“SQ 到 CQ 没有任何 copy”，也不能把 descriptor 共享与 payload 数据路径混为一谈。

## `io_uring` 与 `epoll`：完成与就绪，而不是新旧胜负

`epoll` 是 readiness 模型：收到 fd 可读/可写通知后，应用再调用 `read`/`write` 并处理短传输、`EAGAIN` 等。`io_uring` 可以提交 `read`、`write`、`accept` 等实际操作并在 CQE 报告结果，适用于 completion 风格的状态机；它也有 poll/readiness 类操作，因此不是只能做 Proactor。实际服务常把多种操作组合起来。

| 维度 | 同步 `pread` / `pwrite` | `epoll` readiness | `io_uring` completion |
|---|---|---|---|
| 应用提交内容 | 现在执行一次具体读写 | fd 与想等待的就绪条件 | 描述具体读写/其他操作的 SQE |
| 何时得到结果 | 系统调用返回 | 就绪后应用另行发 I/O | 之后收割 CQE |
| 易于实现 | 最高；错误和生命周期局部 | 连接状态机、部分传输管理 | 请求状态、buffer 生命周期、队列与 CQE 管理 |
| 擅长场景 | 低并发、简单代码、缓存命中等 | 大量连接和就绪驱动网络处理 | 批量 I/O、多个可重叠操作、completion 模型 |
| 代价/约束 | 阻塞等待时线程被占用 | 每个就绪事件可能再有系统调用；读写状态仍由应用管理 | 内核能力/策略受限、批处理/队列管理复杂、调试成本更高 |

该表只是决策起点。若应用没有可并行的独立请求，异步队列不会凭空创造并行性；若负载被设备、CPU、内存带宽、锁或远程服务限制，改变提交 API 也不一定提高吞吐。`io_uring` 不替代正确的缓冲、错误、背压、超时和取消处理。

## 适用性与部署边界

`io_uring` 在上游 Linux 5.1 开始出现；内核版本、发行版回移、配置、安全策略和 opcode 能力共同决定可用性。把“内核至少 5.10”写成所有能力的统一门槛是不准确的：旧内核也可能具备基础接口，新能力则需要相应支持；容器 seccomp、系统策略或环境可能让 `io_uring_setup` 返回 `EPERM`/`EACCES`。启动时应探测实际操作支持并设计替代路径，而不是仅解析 `uname`。

liburing 是用户态开发库，不会替缺失/被禁用的内核功能补出完整内核实现。应用应根据目标系统的发行版包和 liburing API 管理依赖，并在运行时检查 `io_uring_queue_init`、操作提交及 CQE 的错误。课程候选 Ubuntu 24.04/GA 6.8 与 `liburing-dev` 版本见[环境矩阵](../../课程大纲/版本与实验环境.md)，仍应在发布环境验证，不把开发机结果冒充矩阵兼容性证明。

`IORING_SETUP_SQPOLL`、`IORING_SETUP_IOPOLL`、固定 buffer/file、linked SQE、多 shot 等是额外机制，可能减少某些路径开销，也引入 CPU 忙轮询、NUMA/权限/内存注册、完成生命周期及版本要求。它们不是初学者默认开关。应先用普通 ring 构建正确模型，再有针对性地启用并测量。

## 常见接口与性能误区

- **“有 ring 就是异步解耦。”** 队列本身不保证请求有并行进展；并发由提交的独立操作、内核路径和资源能力共同决定。
- **“SQE/CQE 的 mmap 让 payload 零拷贝。”** 共享的是队列元数据/状态，不是所有 IO 数据缓冲区。
- **“io_uring 从此不需要系统调用。”** 应用仍须通过内核接口建立、通知或等待 ring；批量路径可摊薄每请求成本，但不是通用无 syscall。
- **“io_uring 必然比 epoll 或 POSIX 更快。”** API 不能独立决定吞吐和延迟；低并发、浅队列、CPU/内存开销、缓存、安全策略与设备瓶颈都可能让它相当或更慢。
- **“Linux 必须是 5.10 以上。”** 不是基础接口的普适边界；基础接口始于上游 5.1，具体操作和特性须单独探测。
- **“libaio 不能用/会导致内核越界。”** Linux native AIO/libaio 是另一种接口，有自身的 buffered/direct 约束；应按目标 API 和版本比较，不能用不实的安全断言代替分析。
- **“io_uring 总是 Proactor，epoll 总是完全不同。”** 可作主抽象比较，但 `io_uring` 也包含 poll/readiness 能力，实际程序可以组合两类思想。
- **“课堂 echo 服务可以直接上生产。”** 网络服务还须证明并发 buffer、短写、取消、连接状态、输入长度和系统资源管理；教学流程不代表公网服务安全性。

文件或网络基准必须固定引擎、队列深度、缓存状态、持久化语义、重复误差、连接数、payload、内核和代码版本。没有这些条件的 fio/QPS 数字不能推出通用加速比例。

## 实验：普通 C 基线和可选 liburing

关联 [Lab 03：Linux 文件与本地 socket I/O](../../labs/lab-03-linux-io/README.md)。必做不依赖 liburing：

```sh
make clean test
```

默认 C 程序验证 `pread`/`pwrite`、`readv`/`writev` 的完整传输语义，并通过本地 socketpair 交换小段数据。可选 liburing 实验只对它自己创建的临时普通文件发出两笔独立写和两笔读：使用 `user_data` 识别完成，验证短完成/负 errno，并核对读回数据。它不开服务器、不绑定外网、不打开设备路径。

可选构建依赖 `liburing` 开发头文件、库、`pkg-config` 和支持/允许 io_uring 的 Linux 内核：

```sh
pkg-config --modversion liburing
make uring test-uring
```

如果库不存在，默认 `make test` 仍可通过；如果 ring 被内核、seccomp 或系统策略禁止，专项程序以退出码 77 明确报告 `SKIP`，不要试图提权或绕过容器安全限制。实验输出不是性能基准。可查看实验编译时的 `cc --version`、`uname -a` 和 liburing 版本。

## 常见故障诊断

- `io_uring_queue_init: Operation not permitted`：可能是容器/系统策略限制；保留错误并使用同步 POSIX 必做路径，不要加 root 权限绕过。
- 编译找不到 `liburing.h` 或链接失败：可选开发依赖未安装或 pkg-config 路径不一致；必做实验不受影响。
- `io_uring_get_sqe` 返回 NULL：SQ 无空槽。收割/提交后重试或施加背压，不能覆盖仍待处理操作。
- `io_uring_submit` 返回负数/少于准备数量：解释为负 errno 或部分提交，按实际已提交请求收割完成；不能简单假设所有 SQE 都进入内核。
- CQE `res` 为负：它携带 `-errno`；不是数据长度。正数短于请求长度时也是一次有效的部分完成，需要续作或上报。
- 读回校验不一致：检查 offset、请求关联 ID、并发 buffer 是否复用以及是否等待所有写完成后才提交读取。
- `test-uring` 显示 SKIP：记录内核/策略环境；不要将该跳过误报为代码通过或性能结果。

## 课后检查与设计题

1. 画出一次 async read 从取得 SQE 到释放用户 buffer 的状态图；标出可能发生的失败点和必须保留的对象。
2. 两个 4 KiB 写请求同时提交，第二个先完成。应用如何正确识别哪个逻辑请求已完成？
3. 某 CQE 的 `res=-EINVAL`、`res=0`、`res=4096` 分别可能表示什么？解释时需要结合哪个 opcode？
4. 为什么 ring 的 128 个 SQE 不能推断设备队列深度为 128？列出主机到设备路径中的至少三个队列/限速点。
5. 针对“单线程每次读取小块并立即解析”的程序，列出你会先收集的证据，再决定改 `io_uring`、`readv`、缓存或数据布局。
6. 设计 `epoll` 与 `io_uring` 网络服务的公平比较：如何控制客户端、连接数、消息大小、CPU、内核、日志、完成语义和重复次数？哪些尾延迟/饱和指标需要一起报告？

## 参考资料与来源

- Linux 内核文档：[io_uring](https://docs.kernel.org/io_uring/)、[Linux 6.8 io_uring 文档](https://docs.kernel.org/6.8/io_uring/)；具体特性应与目标内核版本匹配。
- 官方接口文档：[io_uring_setup(2)](https://man7.org/linux/man-pages/man2/io_uring_setup.2.html)、[io_uring_enter(2)](https://man7.org/linux/man-pages/man2/io_uring_enter.2.html)、[io_uring_register(2)](https://man7.org/linux/man-pages/man2/io_uring_register.2.html)、[liburing 项目](https://github.com/axboe/liburing)。
- 上游参考：[Linux io_uring 文档](https://docs.kernel.org/io_uring/)；[liburing 项目](https://github.com/axboe/liburing)。
- 课程本机实验约束见 [Lab 03 README](../../labs/lab-03-linux-io/README.md) 与[版本和实验环境](../../课程大纲/版本与实验环境.md)。复核代码/操作行为时记录实际内核、发行版和 liburing 版本。
