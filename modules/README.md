# 模块目录索引

课程规划和模块简介见 [课程规划](../课程规划.md) 与 [课程地图](../课程大纲/课程地图.md)。课程设计共 34 课；M00–M15 的 34 篇课文及模块 README 均已完成，表中同时记录本地 lab/project 的自动测试状态与明确未验证项。**课文完成不等于所有候选发行版、硬件、云服务或软件栈均已集成验证。**M00–M14 的本地实验各自说明可复现命令和可选依赖；M15 集成 M05 KV/WAL、M07 进程故障恢复、M08 本地对象 API 与 M12 本机性能观察，含 26 项 MiniStore 自动测试。关键边界：M07 的 SIGKILL 不模拟真实断电；M08 不宣称 S3/SigV4；M09 不要求 Ceph 集群/root/真实磁盘；M10 模拟器不是生产 Raft，etcd Raft v3.6.0 只作源码阅读、未集成验证；M12 文件实验不是硬件基准，cache 实验不代表 MiniStore 引擎性能；M13 的 metrics/trace/存储状态含合成数据，单节点不证明 HA，Kubernetes/Rook/Ceph 组合未声称部署验证；M14 的 tenant label 不是身份认证，CRC 不防恶意篡改；MiniStore 无生产安全、备份、分布式一致性保证。SPDK、真实 NVMe、RGW/AWS、CSI 等选修栈只有在明确记录真实运行结果后才能标为集成验证。

| 目录 | 模块 | 课次 | 当前状态 |
|---|---|---:|---|
| [M00-课程导览](M00-课程导览/README.md) | 课程导览与存储全景 | 2 | 两课正文及本地需求卡实验已完成；含自动测试 |
| [M01-设备与块存储](M01-设备与块存储/README.md) | 设备与块存储 | 2 | L01 样章与 L02 完整课文已完成；分别配有普通文件映像实验和内存合成 FTL/队列模型及自测 |
| [M02-LinuxIO](M02-LinuxIO/README.md) | Linux IO | 3 | 三课完整正文已完成；配套普通 C 临时文件/本地 socket 自动测试通过，liburing 专项可选 |
| [M03-文件系统与FUSE](M03-文件系统与FUSE/README.md) | 文件系统与 FUSE | 2 | 两课正文及无特权内存核心实验已完成；可选 libfuse3 临时挂载示例通过本地 3.18.3 构建/运行验证，候选 3.14.0 尚未验证 |
| [M04-用户态存储与SPDK](M04-用户态存储与SPDK/README.md) | 用户态存储与 SPDK（扩展/选修） | 2 | 两课正文已完成；无设备 callback/queue 模型及故障清理自测已完成；SPDK v26.05/设备实验可选且未验证 |
| [M05-本地KV引擎](M05-本地KV引擎/README.md) | 本地 KV 引擎 | 3 | 三课正文及 C++20 MiniStore 项目已完成；支持 put/get/delete/list、WAL/重启恢复、SSTable/compact 和自动化安全测试；明确非生产级 |
| [M06-索引与内存管理](M06-索引与内存管理/README.md) | 索引与内存管理 | 2 | 两课完整正文已完成；覆盖哈希/跳表/树复杂度、arena/slab、LRU/FIFO 固定种子轨迹；C++20 IndexCacheLab 编译及自动正确性/命中率验收通过，不作性能基准 |
| [M07-持久性与崩溃一致性](M07-持久性与崩溃一致性/README.md) | 持久性与崩溃一致性 | 2 | 两课正文与 [Lab 06](../labs/lab-06-crash-consistency/README.md) 完成；覆盖 `fdatasync/fsync`、写顺序、目录项与设备持久性边界，以及 MiniStore WAL/compaction 阶段性进程 SIGKILL、合成 sync 错误和恢复断言；不模拟系统掉电 |
| [M08-对象存储服务](M08-对象存储服务/README.md) | 对象存储服务 | 2 | 两课正文与 [Lab 07](../labs/lab-07-object-api/README.md) 完成；涵盖对象/元数据/数据面、一致性产品边界、S3 风格协议/multipart/权限；MiniStore 实现有限本地对象 CLI 并覆盖 put/get/delete/list、覆盖、二进制、锁冲突与输入错误；不宣称 S3/SigV4 兼容；RGW/AWS CLI 仅选修 |
| [M09-分布式存储与Ceph](M09-分布式存储与Ceph/README.md) | 分布式存储与 Ceph | 2 | 两课正文与 [Lab 09](../labs/lab-09-ceph-failure-simulation/README.md) 已完成；先导入故障模型、MON 多数派与故障域，再讲 RADOS/OSD/MON/PG/CRUSH、复制/EC、读写路径、peering/恢复/重建与尾延迟，以及 RBD/CephFS/RGW 职责；明确 Ceph MON 使用 Paxos-based 机制/变体而非 Raft。Python 本地模拟含自动测试；真实 Ceph 部署纯选做，不要求集群/root/真实数据盘；部署时按目标 release 使用受支持的 cephadm 或 Rook 流程
| [M10-复制与共识](M10-复制与共识/README.md) | 复制与共识 | 2 | 两课正文与 [Lab 10](../labs/lab-10-raft-simulation/README.md) 完成；讲解 Raft 安全核心、term/选举、日志复制、commit/apply、当前 term 提交规则、分区行为、快照和 joint consensus；Python 确定性内存模型含 12 项选举/安全/故障测试，明确不是生产协议实现；对照固定源码基线 etcd Raft v3.6.0（未构建验证），并区分 Ceph MON Paxos-based 机制 |
| [M11-分布式事务与数据库](M11-分布式事务与数据库/README.md) | 分布式事务与数据库 | 2 | 两课正文及 Lab 11 本地 MVCC/Percolator-inspired 事务恢复模拟已完成；覆盖快照可见性、SI 写写冲突与 write skew、2PC 阻塞/恢复、TiDB/TiKV/PD/TiFlash 职责；Python 标准库 11 项测试通过。案例固定 TiDB/TiKV v8.5.8；不将 Percolator 论文等同现代实现，TiUP Playground 显式固定版本且仅选修 |
| [M12-性能工程](M12-性能工程/README.md) | 性能工程 | 2 | 两课完整正文与 Lab 12 已完成；覆盖负载建模、吞吐/p50/p95/p99、运行间误差、缓存状态、饱和与背压，说明单次 `dd` 测量不能推广到其他负载或硬件。Python 标准库工具只在私有临时普通文件观察 buffered QD=1 行为并校验数据；固定种子 LRU/FIFO MiniStore 风格模拟只验收命中率/结果一致性。fio/RocksDB 对照选修且明确版本/安全边界；不接触裸设备/云服务，也不声称硬件性能结论。 |
| [M13-云原生运维与可观测性](M13-云原生运维与可观测性/README.md) | 云原生运维与可观测性 | 2 | 两课与 Lab 13 已完成；涵盖指标/日志/trace、标签基数与遥测丢失、健康/容量/RPO/RTO/备份恢复、容器与 CSI 卷/快照/恢复边界及 Ceph 运维诊断。必做脚本只绑定 localhost 并使用合成 metrics/trace；9 项标准库测试通过。Kubernetes/Rook/Ceph 仅选修，固定候选版本但未声称集成验证；单节点不证明 HA。 |
| [M14-安全与可靠性](M14-安全与可靠性/README.md) | 安全与可靠性 | 1 | 一课威胁模型/数据保护/安全变更正文与 Lab 14 已完成；本地权限、输入/校验、租户范围、symlink/覆盖错误路径演示及 10 项标准库测试通过。不是安全认证；MiniStore 缺少生产 AuthN/AuthZ、多租户、加密/密钥、供应链证明和独立备份能力 |
| [M15-综合项目](M15-综合项目/README.md) | MiniStore 综合项目 | 3 | 三课正文完成：架构/接口与阶段集成、端到端正确性/重启/故障/并发验收、MiniStore CLI 基准报告/发布清单/限制复盘；MiniStore `make ci` 本地构建和 26 项黑盒/基准测试通过。此结论不覆盖候选外部软件栈/硬件/云/Kubernetes 集成验证，也不宣称生产安全或分布式一致性 |

课程完成状态仅描述文档及上文列出的本地实验/测试；请在发布报告中把“正文完成”“本机测试通过”“候选栈集成验证”作为独立状态，不以空目录或模型通过推断生产能力。

## 单模块建议文件

```text
Mxx-模块名/
├── README.md       # 目标、前置知识、课次清单、完成标准
├── Mxx-L01-主题.md
├── Mxx-L02-主题.md
└── exercises/      # 仅在课文不适合承载实验说明时添加
```
