# M07：持久性与崩溃一致性

## 模块目标

在 M05 的 WAL/恢复设计上明确回答两个问题：一次写入何时可以确认，以及进程在每个持久化阶段退出后，重启应恢复成什么状态。区分内核页缓存、文件系统、目录项、设备缓存与介质承诺；用可重复的进程级 SIGKILL 和受控阶段故障验证 MiniStore 的 WAL、恢复与 compaction 顺序。

本模块的自动实验只使用 MiniStore 自己创建的临时普通文件目录。它不会访问裸盘、真实设备、loop 设备或用户现有数据；**SIGKILL/软件故障不等于断电实验**。测试不能证明 OS 崩溃、硬件掉电、设备缓存或生产文件系统的真实持久性。

## 前置知识与安全边界

- 前置：[M05 本地 KV 引擎](../M05-本地KV引擎/README.md)，尤其 WAL record、sequence、尾部修复、SSTable 发布；M02 文件 IO 与同步接口。
- 推荐复习：[M00-L02 故障模型](../M00-课程导览/M00-L02-工作负载、SLO、故障模型与需求卡.md)；了解 Linux 进程、信号、`write(2)`、`fdatasync(2)`、`fsync(2)`、`rename(2)`。
- 只在 MiniStore 自动测试的 `TemporaryDirectory` 中执行破坏性实验。测试中的可控 failpoint 只编入 `ministore_test`，正常 `ministore` 不含这些挂点。
- 禁止对真实设备断电、写裸盘、卸载正在使用的文件系统，或把注入脚本指向已有数据库。不要将进程 kill 结果描述为掉电/硬件保证。

## 课次

| 课次 | 核心内容 | 产出 |
|---|---|---|
| [M07-L01：持久性边界、同步接口与写入顺序](M07-L01-持久性边界、同步接口与写入顺序.md) | `write`、页缓存、`fdatasync`、`fsync`、目录 `fsync`、原子 rename 与持久性；写入顺序、设备缓存/flush 承诺及错误不确定性 | 为 WAL append 和 compaction 画出确认边界/持久化顺序图，并指出 syscall 成功依赖的下层条件 |
| [M07-L02：故障模型、崩溃注入与恢复断言](M07-L02-故障模型、崩溃注入与恢复断言.md) | 进程崩溃与系统/设备掉电的区别；WAL 截断、完整损坏 fail-closed、同步错误、响应丢失、compaction 阶段的进程故障注入 | 执行自动化 fault tests；按断言检查 WAL 修复、值/sequence 恢复和重新写入能力，并写出未覆盖的故障 |

## 配套实验与自动验收

实验说明与故障矩阵：[Lab 06：MiniStore 进程级崩溃一致性](../../labs/lab-06-crash-consistency/README.md)。实验由 MiniStore 的自动测试执行，不对真实设备做故障操作。

```sh
make -C 'projects/MiniStore' clean test
```

在原有 M05 测试上，Makefile 额外构建启用测试挂点的 `build/ministore_test`。自动化测试在临时目录对子进程设置一个明确的 failpoint，等待其报告到达阶段后以 `SIGKILL` 终止；随后启动全新 CLI 进程并断言恢复状态。覆盖：

- WAL header/payload 中途写入后杀进程：只截断不完整尾部，先前完整写仍可读，新 key 不出现。
- 完整 WAL record 写完但 `fdatasync` 前杀进程：当前内核仍运行，日志可能留在页缓存；测试观察到未确认写可被重放，不把它解释成“未同步也可靠”。
- `fdatasync` 成功后、更新内存/输出 `OK` 前杀进程：重开后读到记录，验证“已提交但响应丢失”的不确定窗口。
- `fdatasync` 错误路径的测试专用注入：错误返回且不确认；本机当前文件可见完整记录并在恢复中重放。注入发生在真正 syscall 前，不模拟设备返回错误或硬件行为。
- compaction 在发布 `TABLE.sst` 后、目录同步前，以及替换 `WAL` 后、目录同步前被杀：重开后检查快照/WAL 状态、读结果和后续写入。
- 测试挂点只编入 test binary；普通 `build/ministore` 忽略相同环境变量。

原有 WAL 校验、尾部恢复、完整 checksum 错误 fail-closed、旧 WAL + 新表 watermark、SSTable checksum、路径安全和锁测试仍一并回归。M06 的独立索引/缓存项目以固定轨迹测试复验，详见课程索引。

## 完成标准

学员能：

1. 用自己的话区分“写入系统调用已返回”“同步接口成功”“客户端看到成功响应”三个事件。
2. 说明为什么文件 `fsync` 和父目录 `fsync` 是不同的同步对象，rename 的运行时原子性为什么不等于掉电持久性。
3. 给 WAL append 与 TABLE/WAL compaction 列出先后依赖，推理每个进程故障窗口的恢复结果。
4. 对不完整尾部、完整但 checksum 错的 record、同步错误和同步后丢响应采用不同恢复/调用方策略。
5. 准确写出实验覆盖的故障模型及至少三项未覆盖条件；不声称课程测试验证了真实断电安全。

## 相关实现与后续

- [Lab 06：MiniStore 进程级崩溃一致性](../../labs/lab-06-crash-consistency/README.md)；[MiniStore 项目持久性与测试说明](../../projects/MiniStore/README.md)；[WAL/恢复实现](../../projects/MiniStore/src/ministore.cpp)；[进程级故障与恢复断言](../../projects/MiniStore/tests/test_ministore.py)。
- 前置：[M05 本地 KV 引擎](../M05-本地KV引擎/README.md)；索引/内存模型：[M06 索引与内存管理](../M06-索引与内存管理/README.md)；后续：[M08 对象存储服务](../README.md#单模块建议文件)。
- [课程规划：MiniStore 阶段里程碑](../../课程规划.md)，M05/M07/M08/M12/M15 分阶段扩展同一个教学项目。
