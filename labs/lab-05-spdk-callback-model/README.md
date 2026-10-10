# Lab 05：SPDK callback/queue 无设备模型

配套课文：[M04-L01：内核 I/O、用户态轮询与 SPDK 架构](../../modules/M04-用户态存储与SPDK/M04-L01-内核IO、用户态轮询与SPDK架构.md)、[M04-L02：bdev/blobstore、回调数据路径与生命周期](../../modules/M04-用户态存储与SPDK/M04-L02-bdev-blobstore回调数据路径与生命周期.md)。

## 先读：安全边界与实验承诺

- **必做模型**只在进程内的 C 结构、固定大小内存数组和普通事件队列中运行；不创建文件、不访问网络、不打开 `/dev`、块设备、分区、loop 设备或 NVMe，不依赖 SPDK，不需要 root/hugepages/设备直通。
- 模型是“异步调用返回、稍后事件队列执行、完成 callback 推进下一阶段”的教学模拟；它不模拟 SPDK 的真实线程调度、DMA、PCI、IOMMU、NVMe queue 或实际并行，不保证持久性，也不是 SPDK API 兼容层。
- **可选 SPDK SDK 构建/内存 bdev**不需要物理 NVMe，但其 hugepage、环境和权限要求由 OS/EAL 配置决定。若普通用户不能运行就记录 SKIP；不要为绕过策略提权。
- **真实 NVMe 是另外的高级选修，不是默认路径。**设备绑定会从内核卸载目标设备；blobstore 初始化会写 metadata。只能在隔离、可销毁的 VM 中用专用空白虚拟 NVMe 设备，先核对 PCI BDF、设备身份、IOMMU group、挂载/使用状态、备份及快照/回滚。不得用宿主机系统盘、含用户数据的盘、共享 controller 或生产设备。本文不提供绑定/解绑、格式化、裸盘 fio 写入命令；不允许把上游 `setup.sh` 直接用于宿主机。

## 目标

- 理解 callback 参数如何通过 request context 关联一条 I/O 生命周期。
- 观察队列完成次序、依赖状态推进与错误传播。
- 验证写失败后停止 read 并执行 close/unload；初始化失败不进入后续步骤。
- 把“模型测试通过”和“真实 SPDK、硬件或持久性已验证”严格区分。

## 普通机器构建与自测（必做）

依赖：C11 编译器（GCC/Clang）与 GNU make。

```sh
make clean all test
```

从仓库根目录也可运行：

```sh
make -C 'labs/lab-05-spdk-callback-model' clean all test
```

预期：

```text
SPDK callback/queue model tests: PASS
```

编译使用 `-std=c11 -Wall -Wextra -Wpedantic -Werror`。测试断言完整顺序：

```text
init → create → open → resize → sync_md → write → read → close → unload
```

并注入以下错误：

1. **write `-EIO`：**保留首个错误，不提交 read，继续 close/unload，队列归空。
2. **init `-EIO`：**停止流程，不创建对象、不打开 blob、不尝试 unload 未初始化的 store。
3. **close `-EIO`：**停止卸载，不把仍打开的对象伪报为清理完成。
4. 成功路径：写入固定 payload、回读比较、close/unload，验证操作顺序和资源标记。

可检查环境：`cc --version`、`make --version`、`uname -a`。该结果仅说明课程模型在当前编译器上通过，不是 SPDK v26.05 构建或硬件 IOPS 结果。

## 模型的数据结构与限制

- `callback_model.c`：固定长度 FIFO 事件队列、模拟操作执行器、状态转换、callback continuation、错误与 cleanup。
- `callback_model.h`：操作 enum、结果结构和模型入口。
- `test_model.c`：成功链、write/init 故障与 close 清理失败的 assert 自测。
- `Makefile`：默认构建与测试完全不查找/链接 SPDK。

事件不是在提交 API 的调用栈内同步完成。`model_run` 驱动自己的 event loop；消费到事件时模拟操作结果，再调用 callback；成功 callback 向队列追加下一步。结构体现回调链，但为了确定性只运行一个逻辑请求、每次一个待处理步骤，不模拟多 I/O 并发或乱序完成。

`storage_released` 是对模拟对象状态的检查，不代表释放了实际设备或验证持久化。模型故障注入只覆盖指定操作 `-EIO`，没有模拟内存耗尽、超时/取消、late callback、设备移除、数据损坏和多线程竞态。

## 推荐阅读与扩展

1. 阅读 `submit_next`、`execute_event` 和 `on_*`，画出每次 callback 接收到的上下文及下一步提交。
2. 修改失败点为 `CREATE`、`OPEN`、`RESIZE`、`SYNC_MD`，列出合理 cleanup 序列并补测试；不要让错误回调重复提交同一 cleanup 或覆盖首错。
3. 增加第二个 request context，交错两条队列事件并验证 buffer/状态不会串；对同一 blob 的 close 需等待所有 outstanding I/O。
4. 增加超时模拟：超时只改变调用者等待状态，不能在仍有 event 持有 context 时释放它；最终 callback 必须仍能安全回收。
5. 对照课程 SPDK 候选 tag 的官方 `blob.h`/bdev examples，核对 callback 签名、IO 单位、channel 所属线程、错误编码和卸载顺序。不要根据模型结构猜 API ABI。

## SDK / SPDK 选做（默认无物理设备）

课程版本表把 SPDK **v26.05** 列为候选版本。**此候选目前没有在本实验环境验证；下面是待按上游 tag 核对的构建路线，不表示已经构建/运行通过。**不要换成 `master`，也不要假设其他 release API 兼容。

在有独立开发环境且预装依赖、确认不需提权安装依赖后，可在自己的用户目录取固定 tag：

```sh
git clone --branch v26.05 --recursive https://github.com/spdk/spdk.git "$HOME/src/spdk-v26.05"
cd "$HOME/src/spdk-v26.05"
git rev-parse HEAD
git submodule status --recursive
./configure
make -j2
```

如果 clone/tag、依赖或 build 与候选不符，停止并记录版本/错误；不要执行 `scripts/pkgdep.sh` 中任何可能修改系统/安装软件的动作，除非管理员单独审阅。SPDK 可能需要 hugepages/EAL 权限或环境准备，普通用户无法运行时按限制跳过，不要 sudo 重试。

构建后选做任务是**只使用纯软件内存 bdev** 阅读并运行该 release 随附的 blob/hello 示例：先检查该 tag 下 examples 和 README 的准确命令，确认 bdev 创建配置仅有 `Malloc`/内存后端，并没有 `bdev_nvme_attach_controller`、PCI BDF 或外部盘。用小容量配置完成 store 初始化、blob 数据写入/读回和正常 unload；将结果标为“内存 backend 过程验证”，不可写成设备持久化验证。不要直接复用未经核验的配置文件或构建命令；先按 v26.05 源码确认示例、内存 bdev 配置和 API，再记录实际步骤。

### 真实 NVMe 直通：单独审批的高级选修

只有在上一步 SDK/内存 bdev 已完成、具备设备维护经验时才继续。先在只读观察阶段记录：

```sh
uname -a
lspci -nn
lsblk -o NAME,TYPE,SIZE,FSTYPE,MOUNTPOINTS,MODEL,SERIAL
findmnt
```

这些检查不授权任何后续设备操作。必须确认虚拟机独立、目标是专用且可销毁的空白 NVMe controller/namespace、无宿主/guest mount/LVM/swap/其他用户、IOMMU/VFIO group 隔离符合平台策略、VM snapshot/销毁回滚已验证，并由设备所有者批准。NVMe 直通通常接管 PCI function 而非普通文件路径；不能只凭 `lsblk` 一个设备名判断 controller 与 group 的安全范围。

本 lab 不给出任何 `setup.sh`、driver bind/unbind、`nvme format`、`dd`、裸盘 `fio` 或 `spdk_bs_init` 命令。实际设备实验需教师/管理员提供**版本匹配、限定到该专用 VM 的书面步骤和清理/复原方案**；blobstore init/写入视为破坏性操作。条件任一不满足，就不运行设备实验，改做架构图/配置阅读和 Lab 05 模型。

## 清理

```sh
make clean
```

只移除此目录产生的 `callback-model-test` 二进制。自测不创建其他文件、临时数据、设备配置或后台进程。

## 验收记录

提交实验记录：编译器、内核、命令、完整测试输出、模型限制。若进行可选 SDK 实验，另记录 tag/commit、recursive submodule、构建参数、bdev 类型和是否真实运行；不运行写“未验证/跳过”，不能与模型 PASS 混写。真实硬件活动需另附设备 BDF/型号、VM/隔离/NUMA、授权和回滚步骤及日志，但禁止提交设备序列号、凭据或主机敏感信息。
