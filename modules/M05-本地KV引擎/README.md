# M05：本地 KV 引擎

## 模块目标

从有明确定义的 KV API 出发，追踪写入、查找、删除和列举如何成为可恢复状态；理解 WAL 的记录格式/同步边界、内存索引、SSTable、LSM 与 compaction。课程先建立 KV API 和数据路径，再实现安全的单机持久化原型，最后分析它与完整 LSM 引擎的差距。

本模块必做项目是 [MiniStore](../../projects/MiniStore/README.md)：不依赖 SPDK/NVMe/第三方库的 C++20 CLI 项目。实现有限 key/value API、指定本地目录、CRC 校验 WAL、进程重启恢复、排序 snapshot、手动 compaction 和自动化黑盒测试。它是**教学原型，不是生产级数据库**。

## 前置知识与安全边界

- 前置：M01–M04 的 Linux 文件/IO、异步完成、文件同步与错误传播概念；熟悉 C++ 基本语法、Make 和 Python 标准库。
- 必做只运行普通 Linux 用户进程，使用课程自己创建的临时目录；无需 root、`/dev`、裸盘、loop 设备、NVMe 硬件、SPDK 或网络。
- `ministore init --dir PATH` 只接受不存在的新末级路径，不递归删除或复用已有目录。PATH 父目录须已存在；数据库目录及格式文件使用权限、owner、普通文件、硬链接和 symlink 检查。
- 操作前须确认 `DB` 指向本次实验所建临时目录。只清理自己创建并持有的临时根目录；不要复制故障注入、截断或 `rm -rf` 命令到用户目录/现存数据。
- CRC32 检查意外损坏，不防恶意篡改；本地 `fdatasync` 不等同于真实掉电认证。完整项目限制、错误行为和未实现能力见项目 README。

## 课次

| 课次 | 核心内容 | 产出 |
|---|---|---|
| [M05-L01：KV API 与本地持久化数据路径](M05-L01-KV-API与本地持久化数据路径.md) | put/upsert、get、delete、list 的有限契约；从 CLI、路径校验、锁、内存 map 到 WAL 的架构；KV 数据路径与 API 边界 | 编译 MiniStore，在临时目录验证覆盖、缺失、删除、排序列举和跨进程读取 |
| [M05-L02：WAL 记录格式与崩溃恢复](M05-L02-WAL记录格式与崩溃恢复.md) | little-endian record、长度/CRC/sequence、日志重放、截断尾部、fail-closed、`fdatasync` 和文件名持久化顺序 | 观察不完整末尾恢复、完整损坏拒绝以及 compaction 的旧日志故障窗口 |
| [M05-L03：SSTable、LSM、compaction 与读写放大](M05-L03-SSTable-LSM-compaction与读写放大.md) | MemTable/WAL/SST 路径、墓碑、多 sorted runs、tiered/leveled 权衡、读/写/空间放大 | 比较 compaction 前后的 WAL/表文件和存活 key；按明确口径计算局部放大 |

## MiniStore 项目验收

```sh
make -C 'projects/MiniStore' clean test
```

验收重点：

- 同一个 `put` 是完整值替换；空 value 与缺失 key 可区分；delete 记录墓碑；list 输出稳定排序。
- 每条写入先 WAL append、完整写和 `fdatasync`，再更新内存并确认；新 CLI 进程从 SSTable + WAL 重建状态。
- WAL/SSTable 的长度、版本、CRC、key、sequence 坏值拒绝；末尾不完整记录仅截到最后有效边界。
- `compact` 先同步并发布新快照，再轮换 WAL；覆盖快照已发布但旧日志仍存在的恢复窗口。
- init 不覆写既存路径；数据库目录、文件类型、symlink、路径/权限约束和独占锁有测试。
- 解释项目的单进程模型、64 MiB WAL / 256 MiB live-table 限制、whole-file startup/rewrite 成本、`fdatasync` 的设备边界及非生产定位。

## 课程地图与延伸阅读

- 前置：[M04 用户态存储与 SPDK](../M04-用户态存储与SPDK/README.md)；后续：[M06 索引与内存管理](../M06-索引与内存管理/README.md)、[M07 持久性与崩溃一致性](../M07-持久性与崩溃一致性/README.md)。M07 为 MiniStore 补充进程级故障注入与恢复断言，但不声称模拟设备断电。
- 概念延伸：[RocksDB 官方文档](https://github.com/facebook/rocksdb/wiki)介绍成熟 LSM 引擎的设计与版本化实现；本课程 MiniStore 不依赖 RocksDB。
- [课程规划：MiniStore 阶段里程碑](../../课程规划.md)；[版本与实验环境](../../课程大纲/版本与实验环境.md)。M05 完成 WAL/KV/进程重开恢复基线；[M07](../M07-持久性与崩溃一致性/README.md) 已增加进程级故障注入和恢复断言，不模拟 OS 崩溃或真实设备断电。
