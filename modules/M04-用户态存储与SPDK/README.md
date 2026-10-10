# M04：用户态存储与 SPDK（扩展/选修）

## 模块目标

从 M02 的 Linux I/O 与 M03 的文件系统边界出发，比较内核管理设备的路径和 SPDK 的用户态轮询路径；理解设备所有权、DMA/IOMMU 隔离、bdev 与 blobstore 的职责边界；跟踪异步 callback、消息队列、SPDK thread、I/O channel、buffer 与对象的生命周期；分析吞吐、CPU、尾延迟、功耗和 NUMA 的取舍。

M04 是扩展模块：概念课文和无设备模型实验可在普通 Linux 机器完成；SPDK SDK 构建、NVMe 直通及真实设备实验均为选做，不作为必修环境或性能验收。

## 前置知识与安全边界

- 前置：M01–M03；熟悉 C、文件/块 I/O、异步完成、偏移/队列和 `fsync` 持久化边界。
- 必做 [Lab 05](../../labs/lab-05-spdk-callback-model/README.md) 只在进程内运行 C11 callback/queue 模型；无 SPDK 依赖、不访问文件或设备、不需要 root、hugepages、网络或 NVMe。
- SPDK 使用真实 PCIe 设备时通常需要设备独占及环境配置。绑定驱动可能使内核 `/dev/nvme*` 节点消失；错误设备选择会破坏数据并影响其他服务。**本模块没有默认 root 命令、设备绑定脚本、裸盘写命令或 NVMe 直通要求。**
- 若做 SDK 选修，先用 SPDK 自带的纯软件/内存 bdev 与上游示例，不接管物理设备。真实 NVMe 仅允许在隔离、可销毁 VM 中使用专用空白虚拟盘，且在核对 PCI BDF、IOMMU group、挂载/使用状态、备份和回滚方案后单独审批。

## 课次

| 课次 | 主题 | 产出 |
|---|---|---|
| [M04-L01：内核 I/O、用户态轮询与 SPDK 架构](M04-L01-内核IO、用户态轮询与SPDK架构.md) | 内核 VFS/块层/NVMe 路径与 SPDK EAL/reactor/polling/bdev 路径；PCI 设备绑定、DMA/IOMMU 与隔离；CPU、队列、功耗、尾延迟和 NUMA 取舍 | 能画出两条路径，说明谁拥有设备、完成如何被发现、轮询成本由谁承担，并设计公平比较 |
| [M04-L02：bdev/blobstore、回调数据路径与生命周期](M04-L02-bdev-blobstore回调数据路径与生命周期.md) | bdev 与 blobstore 层次、blob/cluster/io unit、SPDK thread/message/channel、异步状态机与 callback context、资源归属、错误/清理、持久化边界 | 能画出 init→create/open→resize/sync→IO→close/unload 状态机，并用模型测试成功和失败清理 |

## 实验

[Lab 05：SPDK callback/queue 无设备模型](../../labs/lab-05-spdk-callback-model/README.md) 使用普通 C11 构建，模拟异步事件队列和 `init → create → open → resize → sync_md → write → read → close → unload` 链，验证回读、顺序、故障传播及清理。

```sh
make -C 'labs/lab-05-spdk-callback-model' clean all test
```

实验模型只教学 callback/queue 的控制流，不实现 SPDK API、不执行 DMA、不建 bdev/blobstore，也不声称数据持久化或代表真实设备性能。SDK/设备选做的版本和前置条件见 Lab 05；SPDK 候选 tag v26.05 尚未在本课程环境验证，需按该 tag 官方文档重新检查 API、依赖和命令。

## 完成标准

- 能分别从 `pread`/文件系统和 SPDK bdev 的应用入口追踪到设备队列与完成；知道 `O_DIRECT` 仍使用内核存储栈，用户态轮询也不等于完全没有内核参与。
- 能解释绑定 SPDK 驱动后内核设备节点/消费者失去访问的原因；能说明 VFIO/IOMMU 与 UIO 的隔离差异，且不把 `setup.sh` 当普通安全命令。
- 能区分 bdev（块接口）与 blobstore（对象/分配/元数据层），不把 blob 当 POSIX 文件，也不把 malloc bdev 当持久盘。
- 能追踪 callback 参数与 buffer、blob handle、I/O channel、blobstore、线程的生命周期；错误不继续推进，异步请求未完成前不释放其上下文。
- 能区分 `spdk_bs_unload` 的正常卸载与销毁/重新格式化语义；说明 callback 成功、bdev 写完成与掉电持久性并非同义词。
- 能用 Lab 05 成功链、写失败清理和初始化失败测试；性能讨论包含 CPU 核、轮询、NUMA、缓存/同步语义、设备、队列深度和尾延迟。
- 将真实 NVMe、直通与性能测量标为选修；不在宿主系统盘、挂载盘或共享设备做写测试。

## 导航与学习边界

- 前置：[M03 文件系统与 FUSE](../M03-文件系统与FUSE/README.md)；后续：[M05 本地 KV 引擎](../M05-本地KV引擎/README.md)（MiniStore 项目与三课正文已完成）。
- 规划：[课程规划中的 M04 定位](../../课程规划.md)；[候选版本矩阵](../../课程大纲/版本与实验环境.md)。
- 课文：[M04-L01](M04-L01-内核IO、用户态轮询与SPDK架构.md) 介绍内核与 SPDK 数据路径、设备隔离和比较方法；[M04-L02](M04-L02-bdev-blobstore回调数据路径与生命周期.md) 介绍 bdev/blobstore 生命周期、回调与上层文件/KV 服务边界。
