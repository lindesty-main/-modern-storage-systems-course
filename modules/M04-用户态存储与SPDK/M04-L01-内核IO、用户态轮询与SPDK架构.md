# M04-L01：内核 I/O、用户态轮询与 SPDK 架构

> 状态：课程正文　｜　建议课时：110–120 分钟　｜　定位：扩展/选修，不要求真实设备。前置：[M02 Linux I/O](../M02-LinuxIO/README.md)、[M03 文件系统与 FUSE](../M03-文件系统与FUSE/README.md)、块设备与 NVMe 基础。

## 学习目标

学完后，你应能：

1. 画出 buffered/direct POSIX I/O、内核块/NVMe 路径及 SPDK 用户态轮询路径，指出各自省掉/保留了哪些层。
2. 解释 SPDK 为什么强调设备独占、DMA 内存、PCI 绑定、IOMMU/VFIO 与 CPU 轮询；不会把“用户态”误解成无内核参与或零拷贝保证。
3. 比较中断/内核调度和 busy polling 在低负载延迟、吞吐、CPU 消耗、功耗与隔离上的代价。
4. 说明 NUMA、PCIe root complex、内存/核亲和及队列数如何影响结果。
5. 识别内核与 SPDK 数据路径的常见简化，设计等价、可复现且不破坏设备的比较。

## 1. 问题：再少几层能换来什么？

一般文件应用选择 POSIX 文件 API，内核负责文件名/权限、页缓存、文件系统元数据、块层调度、NVMe 驱动和设备共享。SPDK 则把支持的 PCIe 设备分配给用户态进程中的用户态驱动和轮询执行框架，让应用或存储引擎直接拥有队列、DMA buffer 和请求处理循环。它减少了一些 syscall、通用内核数据路径、调度/中断与拷贝开销的机会，但把驱动管理、设备独占、CPU 调度、缓存/元数据、错误恢复和生命周期的责任移到了应用。

SPDK 不是一个泛称“比内核快的 NVMe 驱动”，也不是对所有应用透明的 `read()` 加速层。要使用 SPDK，通常要把程序重构为异步队列/回调，并接受其线程、内存、资源和设备管理模型。只是在 POSIX API 上加一层 hook，不能自动获得文件系统、持久化与安全隔离语义。

## 2. 两条数据路径

### 2.1 常规内核文件 I/O

```text
应用 read/pread/write/pwrite
  → syscall / VFS / fd 与权限
  → 文件系统（路径、inode、extent/映射、日志等）
  → 页缓存（buffered I/O；O_DIRECT 尝试绕过数据页缓存）
  → bio / blk-mq / request queue
  → Linux NVMe 驱动和内核控制器/namespace 管理
  → NVMe submission queue / doorbell
  → 控制器和介质
  → completion queue → 中断、内核轮询或其他完成路径
  → 内核唤醒/完成调用并返回应用
```

这是逻辑层次图，不代表每次 `read()` 都到设备：页缓存命中可直接返回；读-ahead/writeback、文件系统日志、块层合并/调度、设备写缓存和同步标志都会改变请求。`O_DIRECT` 主要影响文件数据缓存及对齐要求，仍经过 VFS、文件系统及内核块/NVMe 驱动；它不是用户态 NVMe 驱动，也不保证无拷贝或持久化。直接对 `/dev/nvme...` 做 ioctl 仍是内核驱动在处理控制器命令，只是没有普通文件系统路径。

### 2.2 SPDK 用户态路径

```text
SPDK 应用 / 存储引擎
  → SPDK bdev API（异步请求、模块化块设备）
  → bdev module（NVMe / malloc / 其他受支持后端）
  → SPDK NVMe 用户态驱动
  → 用户态进程管理的 NVMe queues / doorbells
  → 控制器和介质
  → completion queue
  → reactor/thread poller 轮询完成并调用 callback
```

SPDK 启动/设备准备通常由 DPDK 环境抽象（EAL）等组件管理 hugepage、CPU/PCI 资源、DMA 可访问内存、PCI 绑定和 IOMMU/VFIO。数据路径中的提交/完成主要由用户态线程轮询，不需要每个设备完成都以传统内核中断方式唤醒应用线程。但“用户态路径”仍依赖内核配置、PCI/IOMMU、内存映射、调度、文件系统（若 bdev 后端是文件/AIO）以及系统权限；SPDK 也有不同后端/驱动和中断能力，不能泛化为所有路径完全没有中断。

**polling 的本质是用持续消耗的 CPU 换取及时检查完成队列。** 空闲时 poller 也可能持续占据一个核心；因此端到端延迟下降并不自动代表系统效率更高。

## 3. 设备绑定、独占与隔离

### 3.1 为什么必须处理设备所有权

典型 SPDK NVMe PCIe 路径让目标 PCI function 从 Linux `nvme` 驱动解绑，再绑定到用户态可访问驱动（常见为 `vfio-pci`，历史/特定环境还会见到 UIO 驱动）。解绑后内核不再为该 function 管理 NVMe controller/namespace，因此 `/dev/nvmeXnY` 可能消失，依赖它的文件系统、LVM、交换区、数据库或监控工具也无法继续访问。对 controller 中其他 namespace 的影响要按硬件、绑定粒度、虚拟化与版本核实，不能假设“只接管一个分区”。

设备归属一次只能有一个驱动负责；不能让 Linux 文件系统和 SPDK 同时对同一 namespace 任意读写。设备名称或 BDF 识别错误会把系统盘/生产盘交给用户态，造成数据丢失或主机不可用。

### 3.2 IOMMU、VFIO 与 UIO

- **VFIO + IOMMU：**提供用户态设备访问与 DMA 地址映射/隔离能力；PCI 设备常按 IOMMU group 作为隔离单位。group 里其他 function、主板拓扑、固件和内核配置都影响能否安全直通。VFIO 是受内核管理的用户态设备接口，不是“卸载所有内核代码”。
- **UIO：**可将设备映射给用户态代码，但一般不提供与 VFIO+IOMMU 等同的 DMA 隔离保证。对可编程 DMA 设备，缺失隔离会增加内存安全风险；不应为了简单把它当生产环境通用替代。
- **Hugepages/DMA memory：**大小、NUMA 节点、锁定内存与 IOMMU 映射需匹配工作集和设备需求；不是可以无限申请，也不能仅凭地址对齐证明 buffer 可 DMA。
- **权限：**设备节点/hugepage/PCI 权限依发行版和 SPDK build 配置而异。真实设备常需要管理员准备，但课程程序默认不提权；容器/VM 内还受宿主机设备授权和 IOMMU 直通策略控制。

SPDK 上游的 `scripts/setup.sh` 会探测并改绑 PCI 设备，是设备管理工具，不是无害的安装命令；在宿主机上盲目运行会改变设备归属并使 `/dev/nvme*` 节点消失。M04 必做实验完全不触碰绑定。

## 4. reactor、thread、poller 与队列

SPDK application framework 通常在选定的 CPU lcore 上运行 reactor。`spdk_thread` 是 SPDK 的逻辑执行上下文；消息可投递到某 thread，由其 reactor/poll 循环处理；poller 是周期性执行的任务。不要把 SPDK thread 等同于一个永久独立 pthread，也不要把“一次 spdk_app_start 就只有一个线程”当普遍架构。应用可配置多 reactor、多个 SPDK thread 和 CPU mask；具体调度/迁移语义以锁定版本文档为准。

典型工作线程持续推进：处理消息 → 取待发请求 → 批量提交队列 → 检查完成队列 → 调用完成回调 → 推进上层状态机。提交多个独立 I/O 可以利用设备并行性；若 callback 做阻塞文件访问、长时间计算或等待另一个必须由当前 thread 执行的回调，会阻塞该 thread 的队列进展。

消息队列和设备队列不是同一件事。`spdk_thread_send_msg` 是进程内向目标 SPDK thread 投递工作；bdev completion callback 是 I/O 完成通知；它们都不表示每次访问 controller 都经过传统网络 RPC。把“bdev 与 controller 之间都是 RPC”用于解释 callback 链会混淆控制面和数据面：JSON-RPC 可配置/管理 SPDK subsystem，而常规 NVMe 数据 I/O 不是每次都调用 JSON-RPC。

## 5. 性能：绕过开销不等于免费

### 5.1 延迟、吞吐与 CPU

减少通用路径的每请求成本、用批处理降低每个 I/O 的固定开销、避免中断/调度往返，可以提升高并发、小 I/O、低延迟工作负载的吞吐或尾延迟。但实际效果取决于 CPU 是否是瓶颈、设备队列深度、请求大小、batch、DMA 和软件路径；如果瓶颈是 NAND、写放大、远程链路、文件系统语义或同步持久化，换 API 也不会消除它。

报告至少一起观察：IOPS/带宽、p50/p95/p99/p99.9 延迟、每核利用率和总核数、功耗/空闲占用、上下文切换/中断、队列深度、请求大小、写缓存/flush/FUA 语义、NUMA placement、温度与稳态。专用轮询核心必须计入成本，不能拿 SPDK 使用 4 个 busy-poll 核的吞吐和内核使用 1 个核的结果只比较 IOPS。

### 5.2 NUMA 与 CPU 亲和

在双路/多 NUMA 系统上，NVMe PCIe function 通常靠近某个 CPU socket/root complex。SPDK reactor 若跑在远端 socket、DMA buffer 从远端节点分配，访问可能穿过 UPI/Infinity Fabric 等互连，增加延迟、占用带宽，并使 poller 与设备队列竞争缓存/内存带宽。优化时要共同检查：

1. `lspci` 所属 NUMA node / PCIe 拓扑；
2. lcore/reactor CPU mask 和 hyperthread/housekeeping 分配；
3. hugepage 与 DMA buffer 的 NUMA node；
4. NVMe submission/completion queue 数、bdev channel 与线程归属；
5. 同 socket 的其他负载、IRQ 和内存带宽争用。

单 socket 笔记本/VM 没有足够拓扑时，不要伪造 NUMA 结论；可画拓扑和做概念题。CPU 核绑定不等于内存本地，NUMA 内存绑定也不等于设备中断/队列最优。

### 5.3 内核路径什么时候更合适

常规内核 I/O 通常更适合一般应用、文件系统语义、设备共享、低空闲功耗、较少专用 CPU、容器/主机权限隔离和较低运维复杂度。SPDK 适合愿意为高负载、严格延迟或可控存储引擎承担设备/CPU/内存/异步编程复杂度的场景。性能之外还要评估运维、故障恢复、升级兼容、设备监控、备份和应用 API 改造成本。

## 6. 如何做公平对照

公平对照应把“一个内核普通文件 + `io_uring`”和“一个 SPDK bdev”当作不同端到端系统，而非只比较 engine 名称。

- 同一物理设备/namespace（一次只能由一个驱动独占；需要分时重新绑定时确认无挂载/使用者）或同一性能等级设备；不能把 SPDK Malloc bdev 和内核真 NVMe 混作硬件对照。
- 同样读写类型、随机/顺序比例、块大小、总工作集、运行时、队列深度、队列数、线程数、并发、温度/稳态和预处理；明确对齐、direct/buffered、缓存状态。
- 对比相同的数据正确性和完成语义；明确写完成是否仅写到设备 volatile cache、是否需要 flush/FUA/fsync、元数据/校验工作是否包括在路径内。
- 充分预热并重复运行；记录错误、CPU core-seconds、p50/p99/p99.9，而非只看最大 IOPS。随机写不是所有应用工作负载的代表。
- 固定 fio/SPDK tag、fio job 文件和驱动/固件，记录输出与 `lspci`/NUMA/CPU 配置；不要引用一次虚拟机数据作为产品级指标。

## 7. 数据路径与性能比较边界

| 常见简化 | 适用边界 |
|---|---|
| `read/write`、`mmap`、`sendfile` 是同一条简单文件路径；SPDK“完全绕过内核” | 这些接口语义不同；`mmap` 不必然零拷贝，`sendfile` 也不是一般意义上的 mmap。SPDK 数据面绕过部分内核存储栈，但初始化、内存、PCI/IOMMU 与系统管理仍需内核支持。 |
| “SPDK 接管磁盘后所有盘都不可用” | 只有被正确识别并绑定的 PCI function 由相应驱动管理；影响范围按 namespace/controller/group 核实。设备归属变化可能让 Linux block device 消失。 |
| “bdev 与 controller 调用就是 RPC” | JSON-RPC 管理控制面、SPDK 线程消息与 NVMe 数据面是不同机制。常规设备 IO 由本地 bdev/NVMe API 与 queue 推进，不是每个 callback 都是网络 RPC。 |
| “某个 IOPS 数字代表 SPDK 普遍快几倍” | 结果绑定软件版本、设备/后端、fio 配置、队列与持久化条件。内存 bdev 测量的也不是 NVMe；没有等价语义的结果不能比较。 |
| “SPDK 只能 root / 一个 app 只有一个 thread” | 权限取决于 EAL、驱动和环境；`spdk_app_start` 不意味着整体只有单线程。按目标版本 API、运行权限和 reactor 配置验证。 |

公平比较须固定设备、工作负载、缓存、对齐、同步语义、队列、线程、温度、预处理和版本；重复运行并报告尾延迟与 CPU 成本。默认课程实验不改变设备绑定，也不写真实设备。

## 8. 练习与检查题

1. 对某个已有 ext4 文件运行 `pread` 与对 PCI NVMe 使用用户态 SPDK，分别标注页缓存、VFS、块层、驱动和用户态 queue；`O_DIRECT` 改变哪一层？
2. 为什么把 NVMe 绑定至 vfio-pci 后原 mount point 可能无法访问？设备是否能同时让 Linux filesystem 和 SPDK bdev 使用？解释 IOMMU group 的重要性。
3. 轮询使 p99 从 100 μs 降到 60 μs，但 CPU 从 5% 升至 100% 单核。怎样计算每次 IO 的 CPU 成本并判断是否符合服务 SLO/功耗预算？
4. 设计一个不用真实 NVMe 的 SPDK 课堂拓扑。哪些 bdev 后端能验证 API/回调控制流？哪些结论不能据此推出？
5. 两路 NUMA 机器上，NVMe 在 node 1，而 reactor 和 hugepage buffer 在 node 0；列出测量/调优项及其可能副作用。
6. 为什么“应用 write 返回成功”不能直接推出 NAND 已持久化？至少写出内核文件 API 和 SPDK bdev/blobstore 两端需要进一步核实的同步/缓存边界。
7. 写出一份公平 fio 对比报告最少应包含的设备、软件、队列、同步、缓存、CPU/NUMA 和延迟数据。

## 参考资料与复核边界

- SPDK 上游：[Documentation](https://spdk.io/doc/)、[Bdev 用户指南](https://spdk.io/doc/bdev.html)、[NVMe 用户指南](https://spdk.io/doc/nvme.html)、[线程与并发](https://spdk.io/doc/concurrency.html)、[环境抽象](https://spdk.io/doc/env.html)、[JSON-RPC](https://spdk.io/doc/jsonrpc.html)。在线文档可能指向滚动版本；代码/API 要对照所锁定的 release tag。
- Linux：[NVMe subsystem 文档](https://docs.kernel.org/nvme/index.html)、[blk-mq](https://docs.kernel.org/block/blk-mq.html)、[VFIO](https://docs.kernel.org/driver-api/vfio.html)、[IOMMU](https://docs.kernel.org/driver-api/iommu.html)。
- 课程版本矩阵把 SPDK **v26.05** 列为选修候选 tag；本篇不宣称该 tag 已在候选 Ubuntu/内核上构建或完成 PCI 实验。发课前记录 commit、recursive submodule 状态、内核、编译选项和设备拓扑。
- 课文以架构概念复核日 **2026-10-09** 编写。SPDK API、VFIO/IOMMU 能力和 release 文档需按实际课程 tag / OS / 内核再次核验。
