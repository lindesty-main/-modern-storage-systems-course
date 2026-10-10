# M04-L02：bdev/blobstore、回调数据路径与生命周期

> 状态：课程正文　｜　建议课时：120–135 分钟（含 Lab 05）　｜　前置：[M04-L01：内核 I/O、用户态轮询与 SPDK 架构](M04-L01-内核IO、用户态轮询与SPDK架构.md)、C callback/指针生命周期、队列与错误处理。

## 学习目标

学完后，你应能：

1. 区分 bdev、`spdk_bs_dev`、blobstore、blob/blob ID、I/O channel、文件系统和 POSIX fd 的职责。
2. 从一次 write 追踪 bdev channel/设备队列、completion callback、SPDK thread poll 与上层 continuation。
3. 给 callback context、buffer、blob handle、channel、blobstore 和线程确定所有权/存活范围；解释为什么同一 reactor 上阻塞等 callback 会死锁。
4. 设计 init/create/open/resize/sync/IO/close/unload 成功及失败状态机，保留首个错误并按依赖顺序异步清理。
5. 说明数据/元数据同步边界，区分 unload 与 destroy，不把内存 bdev、I/O callback 或单次读回测试称作掉电持久性。
6. 使用无需 SPDK 的 Lab 05 模型验证控制流，并指出模型未覆盖的硬件行为。

## 1. 分层：块设备之上并不自动变成文件系统

```text
应用/存储引擎
   │ 业务对象、key/value 或自定义 API
   ▼
可选上层文件系统（如基于 blobstore 的 BlobFS）
   │ 目录/路径/文件属性/文件内容映射（是否具备由实现决定）
   ▼
Blobstore：blob 分配、cluster/元数据、blob ID、open handle
   │ 逻辑对象 I/O，按 blobstore 的 I/O unit 对齐
   ▼
spdk_bs_dev：把 blobstore 接到下层 bdev 的设备适配
   ▼
bdev：异步块设备接口、channel、队列与模块化后端
   ▼
NVMe bdev / malloc bdev / 文件或其他后端
```

- **bdev** 是 SPDK 的 block device 抽象与模块层。上层提交按块/设备约束表达的异步 IO，不必自己处理每个后端的设备队列细节。NVMe bdev、malloc bdev、AIO/file 等模块的可用性依 SPDK 构建选项/版本。bdev 并不等于磁盘文件系统。
- **`spdk_bs_dev`** 是 blobstore 对块设备的适配端；**blobstore** 在它之上管理元数据、cluster 分配和 blob 对象。`spdk_bs_init` 创建/格式化新的 blobstore；要打开已有存储应走对应版本的 load 流程。把 init 用在已有数据上可能重新初始化元数据，必须区分首次格式化与 mount/recovery。
- **blob** 是 blobstore 管理的数据对象，可分配/扩容/打开/关闭/删除；blob ID 是标识符。它不自带 pathname、POSIX inode、目录项、owner/mode、fd、路径解析、rename 原子性或通用 `fsync` 合约。
- Blobstore 上可以继续构建 BlobFS 或自定义文件/KV 层，但目录、文件属性、空间回收、路径并发、崩溃恢复和 POSIX 语义是上层设计责任。`blob ≈ file` 仅能作为“对象承载数据”的粗略类比，不能视为等价。
- **malloc bdev 是内存后端。**它可用于验证配置、队列、对象 API 和 callback；进程/机器重启后数据不因此保留，不能代表 NVMe 持久化或硬件性能。

SPDK subsystem JSON 中的 `bdev_malloc_create` 等配置项描述 bdev 管理操作。JSON-RPC 是管理控制面接口；正常 bdev 数据 IO 通过库内 API/队列提交，不是每个请求都经过 RPC socket。

## 2. 一次 callback 链的依赖关系

以一次新建对象、写入并回读为例，概念流程如下。回调符号和函数签名随 SPDK 版本变化，下面是依赖顺序，不是可直接粘贴编译的 SDK 程序。

```text
SPDK app/environment + bdev subsystem ready
  → 找到/建立 spdk_bs_dev
  → spdk_bs_init（新 store）或 spdk_bs_load（已有 store）
  → [callback: blobstore handle]
  → spdk_bs_create_blob
  → [callback: blob ID]
  → spdk_bs_open_blob
  → [callback: blob handle]
  → resize（若需要）→ sync metadata（按设计要求）
  → 在所属 SPDK thread 分配/取得 blob I/O channel
  → 准备满足大小/对齐/DMA 规则的 buffer
  → spdk_blob_io_write(..., completion, request_ctx)
  → [poller 收到设备/bdev 完成，completion callback]
  → 可继续提交 read/校验/其他工作
  → 等所有 IO 完成 → close blob → free channel → unload blobstore
```

API 大致形态（示意）：

```c
/* 概念示意：精确声明、返回值、选项要读锁定 release 的头文件。 */
spdk_bs_init(bsdev, opts, bs_init_done, fs_ctx);
/* bs_init_done 中成功后 create/open；每次提交结果先检查 error。 */
spdk_blob_io_write(blob, channel, dma_buf, io_offset, io_count,
                   write_done, request_ctx);
```

各 callback 通常通过 `cb_arg` 带回一个调用者 context，并携带 status 及当步产生的对象/ID。下一步只能在前一步成功后发起；错误不能仍使用空/无效 handle 继续走。对真正的异步操作，“函数已经返回”只说明请求已提交或接口接受了操作，不表示数据已经可读/已持久化。

## 3. SPDK thread、message、channel 与 callback 上下文

### 3.1 Thread 与事件进展

SPDK 代码通常由 reactor 驱动的 SPDK thread 执行。把工作 `spdk_thread_send_msg` 给另一个 thread，只是投递一条进程内消息；目标 thread 运行并处理该消息后，才执行其函数。提交 bdev/blob I/O 后，completion 需要对应的队列/poller 推进。应用要保证 reactor 继续运行。

危险模式是：在发出 I/O 的 SPDK thread 上 `while (!done)` 忙等或睡眠，而完成 callback 又必须由该 thread 的 poller 执行。当前 thread 不再 poll，callback 永远得不到运行机会——这是自我死锁，不是超时值太短。也不能从另一个线程随意调用 `spdk_thread_poll` 代替它；thread 的并发/执行归属有自身约束。

优先保持 async flow：提交请求、callback 校验并推进后续状态，最终向应用发完成消息。如果上层确实需要同步接口，应由不负责推进该 SPDK thread 的调用者等待，并确保有独立的 owner/reactor 继续 poll；还要处理 timeout、取消、关闭与 context 存活。promise/future、协程和显式状态机可以改善结构，但不会消除异步生命周期。

### 3.2 I/O channel 是线程相关资源

SPDK bdev/blobstore 的 I/O channel 用于线程局部访问队列/资源。通常每个执行 I/O 的 SPDK thread 获取自己的 channel，并在该 thread 归属/使用范围内发请求和释放 channel；不要任意由多个并发线程共享一个 channel。准确 API 名、channel 创建/释放时机及线程约束请按目标 tag 的 bdev/blob 文档核实。

Channel 不是“网络连接”，也不是“每个 blob 一条 controller RPC”。它是和 SPDK thread/设备访问配套的 I/O 资源。多 channel/thread 可增加并行性，但也增加队列、内存和 CPU 成本。

### 3.3 Context、buffer 与对象生命周期

为每个异步请求定义稳定 context（文件系统/设备级 context 和单请求/单文件 context 分开），例如：

```c
struct fs_context {             /* 多个操作共享：store、配置、线程归属 */
    struct spdk_blob_store *bs;
    /* bdev/bs_dev、状态、引用计数等按设计加入 */
};

struct request_context {        /* 单次操作：buffer、offset、长度、结果 */
    struct fs_context *fs;
    struct spdk_blob *blob;
    struct spdk_io_channel *channel;
    void *buffer;
    size_t length;
    int status;
};
```

这是教学结构，不是规定 ABI。需要回答以下 ownership 问题：

- `cb_arg` 指向对象在**最后一个引用 callback/取消完成之前**保持有效。不能把栈变量地址交给异步 API 后让函数返回；不能因超时就释放仍可能被 callback 使用的 context。
- buffer 在对应 I/O 完成/取消确认为终态前保持有效；DMA buffer 的分配、对齐、注册/释放方式必须符合后端/API 要求。普通 `malloc` 不自动满足任何 DMA/对齐合同。
- blob handle 关闭前不再提交新 IO；所有在途 IO 完成后再 close；所有依赖该 blobstore/channel 的对象收尾后才 unload。
- 每个异步请求有唯一请求身份，回调只处理对应请求；避免单例 `finished`、全局读写 buffer 和多个操作互相覆盖。
- cleanup 路径幂等或有清晰状态；对象只释放一次。先保留首个业务错误，再单独记录 cleanup 错误，不用 cleanup 成功掩盖原始失败。

## 4. 生命周期状态机与错误路径

### 4.1 显式状态机

```text
NEW
 ├─ init/load 失败 ─────────────────────────────→ DONE(error)
 └─ store ready
      → CREATE → OPEN → RESIZE? → SYNC_MD? → IO
                   │                     │
                   └──任一步失败─────────┘
                          ↓
                   停止新提交，记录首错
                          ↓
          drain/取消并收割所有在途 IO
                          ↓
          close 已打开 blob（成功路径和失败路径）
                          ↓
          free thread-owned I/O channel
                          ↓
          unload store（不能等同 destroy）
                          ↓
                       DONE
```

对一个真实多 I/O 文件/引擎，cleanup 必须等待多个在途请求完成，并避免新请求和 close/unload 竞态。一个简单 bool `finished` 无法代表“尚有 N 个 IO 在途、一个 IO 失败、另一个 callback 尚未回收”的状态；应考虑 outstanding count、状态枚举、对象引用或任务完成组。

### 4.2 建立、打开与关闭

- 首次创建 blobstore 与重新加载已有 blobstore 是不同生命周期。init/create 可能改写持久元数据；启动时应根据已存在格式走恢复/load，而不能每次都格式化。
- create blob 的 callback 返回标识符；open 根据该 ID 得到当前打开 handle。长期文件层应把 blob ID/文件元数据放在持久目录/索引，不能依赖仅在运行期存在的全局变量。
- resize、metadata sync、实际 data IO 的顺序取决于格式和上层原子性要求；失败时要定义重启后对象是未创建、旧大小、扩容后空区，还是新版本。测试必须覆盖格式恢复，不只测试同一进程 read-after-write。
- 所有在途 IO 完成后 close blob，再按 API 所有权释放 channel，然后 unload blobstore。正常卸载保留已有存储；destroy 是破坏性删除/重新初始化语义，不能把 destroy 当作普通 unload。

### 4.3 错误、超时与取消

检查每个 API 的立即返回状态和异步完成状态；确认某个失败是“请求未入队”还是“已提交但最后失败”。错误可来自 bdev 不存在、空间不足、内存分配失败、media I/O、元数据更新、超时、设备移除或关闭竞态。上层需区分错误含义、停止依赖步骤、避免重复回调、完成清理并向调用者报告。

超时仅说明调用方停止等待，不等于设备请求已停止。异步请求仍可能使用 buffer/context；必须按 API cancellation/completion 语义 drain。不要在超时之后 free 后继续假设 callback 不会发生。

## 5. Blob 的单位、对齐与字节文件映射

Blobstore 使用自己的 I/O unit 和 cluster 分配单位。`spdk_blob_io_*` 的 offset/count 参数按目标 API 定义的单位解释，通常不是 POSIX 字节偏移；查该版本头文件/文档，不能把 `io_unit_size`、`cluster_size`、bdev block size 和文件字节 offset 混为一谈。

若上层要实现字节文件 API，应设计 `byte_offset → blob/io-unit` 的转换、跨边界读写和 EOF 规则。未对齐写可能需要读改写、内部 staging buffer，或由文件层组合多个 I/O；扩容要明确逻辑 size 与已分配 cluster 数；空洞读零、短读/EOF、append/并发写和错误原子性都属于上层语义。DMA 对齐与 I/O length 限制也必须同时满足。

一次传入一个 I/O unit、固定 offset=0 并回读 `memcmp`，只验证了小型单次数据路径；这没有实现通用 `read(fd, buf, count)`、共享文件偏移、部分传输、多文件目录或并发正确性。

## 6. 写完成、元数据同步与持久性

以下事件要分开理解：

1. callback 表示某个 SPDK API 操作按该 API/后端语义完成或失败。
2. blob 数据 IO 完成代表 bdev 报告该 IO 完成；是否已离开设备 volatile write cache 取决于 bdev、NVMe flush/FUA/缓存设置和设备保证。
3. `spdk_blob_sync_md` 是 blobstore 元数据同步相关操作，不等同于“同步所有文件数据并形成 POSIX `fsync` 的崩溃一致性协议”。
4. 对象元数据持久后数据可能仍处于不同阶段；上层需定义 write/rename/目录更新次序、flush/barrier 与恢复检查。
5. malloc bdev 的完成只作用于内存模型，不可能证明断电后数据存在。

因此写报告应说“操作 callback 成功”并列明实际 backend/flush 配置；没有 power-fail/重启验证和规范承诺就不要写“durable”。

## 7. 同步封装：不要把 reactor 自己堵住

用 `finished` 配合循环 `spdk_thread_poll`，甚至用固定循环次数作为“超时”，有多个问题：

- 固定 poll 次数不是 wall-clock deadline；CPU 速度/负载改变后语义完全不同。
- 如果回调由被阻塞的 thread 执行，等待会死锁；从外部 thread 轮询另一个 thread 也可能违反 thread ownership。
- `usleep` 只能减少忙占用，不负责唤醒条件；等待的 callback 仍需要 reactor 进展。
- 一个全局 finished 标志无法区分并发请求、错误值、超时后的迟到 callback。

推荐选择：让业务保持 callback/state-machine/协程异步；若需要同步包装，把等待放到独立的调用线程，同时保持目标 reactor/thread 正常进展，给请求分配引用计数/超时状态，并处理取消与晚到完成。仅为“代码看起来像 POSIX”而在每次 I/O 上忙等，可能抵消 SPDK 的 CPU/延迟收益。

## 8. 从存储 I/O 到 KV 网络服务的集成边界

一个 SPDK KV 网络服务可以由 socket server、命令解析、索引与持久化层组成：

```text
网络收包 → framing/协议解析 → KV 引擎 →（内存结构或持久化层）→ 应答
```

这条路径中网络 callback、协议和持久化分别有独立异步边界。TCP 是字节流，`recv` 一次不等于一条完整命令；要做 framing、最大长度、并发连接、部分 write 和 backpressure。`strtok` 是有共享状态的不可重入切分方法，多线程/嵌套处理应使用局部 parser。命令索引和命令数组必须做边界校验，错误协议不应落入未定义分支。只实现打印/echo 的协议骨架，接入 NVMe 也不等于 KV 已持久化或达到生产服务质量。

KV 引擎还要定义 SET/GET 线性化、WAL/恢复、重复/并发写、网络断连与持久化 callback 的排序。收到 SET 后立即回成功、后台才提交 bdev，会改变成功语义和丢失窗口；若声称持久，应在应答前等待所定义的持久化边界。组件拼接本身不等于完成的 SPDK KV 产品。

## 9. 实现边界与工程核对

实现 SPDK 文件/KV 服务时，按目标 release 的 API、线程模型和资源所有权逐项核验；本课不提供真实设备绑定或部署脚本。

| 简化实现或假设 | 工程边界 |
|---|---|
| init → create → open → resize → write/read callback 链 | 依赖顺序有教学价值；生产状态机还需要区分新建与加载、立即提交错误和 callback 错误、并发 IO drain、关闭与异常清理。 |
| “bdev/controller 调用就是 RPC” | JSON-RPC 管理控制面、SPDK 线程消息与 NVMe 数据面是不同机制。设备 IO 由本地 bdev/NVMe API/queue 推进，不把每个 callback 描述成网络 RPC。 |
| 用一个全局 context 保存所有文件与请求状态 | 单一操作能简化传参，但多文件/并发请求会互相覆盖。区分 fs/store、file/blob 与 per-I/O context；明确各资源的 owner 和最后释放点。 |
| 用固定循环次数判定 timeout，或在 callback owner 上 poll 等待 | 轮询次数不是时间期限，可能自锁/违反执行归属；保持异步状态机，或由独立线程等待且确保 reactor 可进展。 |
| `spdk_bs_destroy` 作为卸载前的清理 | destroy 会破坏 store 内容/生命周期，不能作为正常 unload 的替代。close handles、完成 IO、释放 channel、`spdk_bs_unload` 与格式化/destroy 分开。 |
| 一次 write/read `memcmp` 成功即可说明文件系统有效 | 仅表示同一 demo 生命周期中的内存/设备回读相等；目录、inode/属性、偏移、文件大小、WAL、崩溃恢复、fsync、并发均未因此实现。 |
| 把 `open/read/write/close` 直接 hook 成同名 libc 符号并用 syscall 绕过初始化递归 | ELF symbol interpose 还会影响库/运行时自身操作，参数 ABI、fd 冲突、错误传递、初始化和线程状态都复杂。新项目不把这种 hook 当文件系统 API；使用明确命名接口或实现标准 FUSE/kernel VFS 接口。 |
| 整数 fd bitmap 从 3 开始就具备 POSIX fd 语义 | 整数数组不等于进程 fd table；需处理并发、重复关闭、fd 复用、fork/exec/dup、errno、offset 与所有权。 |
| 拼接 socket、协议、索引和内存分配器即完成持久 KV | 代码的协议处理是教学阶段；请求 framing、短读写、并发、回包、错误和 bdev/WAL 集成都需要单独实现与测试。 |

## 10. 实验：Lab 05 无设备模型

对应实验：[Lab 05 README](../../labs/lab-05-spdk-callback-model/README.md)。默认只需普通 C 编译器和 make：

```sh
make -C 'labs/lab-05-spdk-callback-model' clean all test
```

成功输出：

```text
SPDK callback/queue model tests: PASS
```

模型使用内存环形队列，每次提交一个模拟操作；event loop 执行完成事件并调用 callback，callback 成功后投递下一步。测试覆盖完整成功链、write 的 `-EIO` 传播与 close/unload 清理、close 失败时不谎报 unload 成功，以及 init 失败后停止后续链。它**不是** SPDK SDK 编译测试，也不模拟真实并行、DMA、NVMe queue、controller latency、取消或崩溃恢复。

### 选做 SDK / 设备练习

SPDK 版本矩阵中的 `v26.05` 是课程候选，不代表已验证。Lab README 提供版本核对、SPDK 内存 bdev/官方 blob 示例阅读与安全门槛。默认只对 malloc bdev 做 SDK 架构/构建实验；真实设备必须另外进入专用隔离 VM，不在当前普通实验脚本中操作。课程不提供默认设备绑定/裸盘写命令。

## 11. 检查题与设计作业

1. 给每一个步骤标出输入、输出对象和失败后要释放什么：bsdev、blobstore、blob ID、blob handle、channel、DMA buffer、request context。
2. `spdk_bs_init` 和已有 store 的 load 有什么区别？若重启时无条件 init 会有什么危险？
3. callback 报成功但调用者已经超时，谁持有 `request_context` 和 buffer？给出引用/取消/完成状态设计。
4. 线程 A 提交 bdev IO 后在同一线程 spin 等待 callback。假设 callback 由 A 所属 poll loop 分派，解释死锁，并给出一种不会阻塞 reactor 的结构。
5. 一次 4 KiB 字节文件写跨越 blob I/O unit 边界且偏移不对齐。文件层至少需要哪些读改写/扩容/同步步骤？
6. 何种条件下 bdev write callback 可被称为“应用写入完成”？何种额外证据才支持“断电持久”？
7. 一个 KV SET 通过网络收到后，何时返回成功才能满足“确认成功的数据可恢复”的契约？发生掉电的窗口有哪些？
8. 设计模型额外的 `CLOSE` 失败用例：为什么不能在仍有打开 handle 时假装 unload 成功？如何记录业务首错和资源泄漏/cleanup 错误？

作业任选一项：为 Lab 05 增加 CREATE/OPEN/READ 的故障注入表并验证禁止越序；扩展两个并发 request context 证明乱序 completion 不串数据；或根据锁定 SPDK tag 头文件，提交一个“版本/声明/API 生命周期对照表”（不要求绑定设备）。

## 参考资料与版本声明

- SPDK 官方：[Blobstore 用户指南/API](https://spdk.io/doc/blob.html)、[bdev](https://spdk.io/doc/bdev.html)、[BlobFS](https://spdk.io/doc/blobfs.html)、[线程/并发模型](https://spdk.io/doc/concurrency.html)、[环境抽象](https://spdk.io/doc/env.html)、[官方源码与 tags](https://github.com/spdk/spdk)。上游在线文档可能滚动，签名/单位/API 序列须对照实际 checkout 的 tag、头文件与 examples。
- NVMe/Linux 安全背景：[VFIO](https://docs.kernel.org/driver-api/vfio.html)、[NVMe](https://docs.kernel.org/nvme/index.html)。
- 课程版本矩阵列出 SPDK `v26.05` 候选版本；本次仅验证 Lab 05 自有模型，**没有**构建或运行该 SPDK tag，也未使用真实 NVMe / VFIO / IOMMU。所有 SDK/device 结果必须单独记录版本、commit、递归 submodule、构建选项、内核、CPU/NUMA、backend 和安全边界。
- 复核日期：2026-10-09。SPDK API、构建命令和性能结果须按固定 release、系统环境与设备拓扑核验；不得从模型实验推断真实设备表现。
