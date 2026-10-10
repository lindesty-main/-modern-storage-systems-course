# 现代存储系统课程

一套面向 Linux/C/C++ 开发者的现代存储工程课程：M00–M15 共 34 课文已完成，从 NVMe 与 Linux IO、文件系统、用户态存储、本地 KV/对象 API 到分布式系统、性能、可靠性、运维与 MiniStore 综合项目。课程文档/本地测试完成不代表所有候选软件栈或硬件组合均已集成验证。

## 从这里开始

- [课程规划](课程规划.md)：课程定位、34 次课路线、MiniStore 项目、目录结构和课程路线。
- [课程地图](课程大纲/课程地图.md)：模块索引与依赖。
- [参考资料与代码仓库](参考资料与代码仓库.md)：按课程主题整理的官方文档、论文、标准、课程对标和上游 Git 仓库。
- [模块索引与建设进度](modules/README.md)：M00–M15、34 课文与本地实验/候选栈验证状态。
- [M00 课程导览模块](modules/M00-课程导览/README.md)：存储全景、端到端数据路径、工作负载/SLO/故障模型，以及本地自检与需求卡实验。
- [M00-L01：存储全景与端到端数据路径](modules/M00-课程导览/M00-L01-存储全景与端到端数据路径.md)：介绍存储分层并画出各类存储数据路径。
- [M00-L02：工作负载、SLO、故障模型与需求卡](modules/M00-课程导览/M00-L02-工作负载、SLO、故障模型与需求卡.md)：附只读环境自检、需求卡校验器和自动测试。
- [M01-L01：块设备、LBA 与安全的块级 I/O](modules/M01-设备与块存储/M01-L01-块设备与LBA.md)：完整课文样章，附可运行 C 程序和自动化实验。
- [M03 文件系统与 FUSE 模块](modules/M03-文件系统与FUSE/README.md)：VFS/inode/dentry/持久化边界、用户态 FUSE 设计与内存核心实验。
- [M04 用户态存储与 SPDK（扩展/选修）](modules/M04-用户态存储与SPDK/README.md)：内核与用户态轮询路径、设备隔离、bdev/blobstore、回调生命周期与 NUMA 取舍；配套普通机器可运行的 callback/queue 模型自测。真实 SPDK SDK/NVMe 仅安全选做，候选版本未验证。
- [M05 本地 KV 引擎](modules/M05-本地KV引擎/README.md)：三课讲清 KV API、WAL/恢复、SSTable/LSM/compaction；提供无需硬件和第三方库的 C++20 MiniStore 教学原型与自动测试（非生产级）。
- [M06 索引与内存管理](modules/M06-索引与内存管理/README.md)：两课完整正文覆盖哈希/跳表/树索引、内存分配、LRU/FIFO 缓存策略；附 C++20 IndexCacheLab 固定种子正确性与命中率自动验收（非性能基准）。
- [M07 持久性与崩溃一致性](modules/M07-持久性与崩溃一致性/README.md)：两课讲清 `fdatasync/fsync`、WAL/compaction 写入顺序、故障模型与持久性边界；MiniStore 临时目录中的进程级 SIGKILL/阶段故障测试及恢复断言不模拟物理掉电。
- [M08 对象存储服务](modules/M08-对象存储服务/README.md)：两课区分对象 API、metadata/data plane、一致性产品保证、S3 风格协议兼容、multipart 失败清理与权限；MiniStore 增加明确有限的本地 object CLI 和项目自带测试，不宣称 S3/SigV4 兼容。AWS CLI/RGW 仅作选修，不依赖云服务。
- [M09 分布式存储与 Ceph](modules/M09-分布式存储与Ceph/README.md)：两课从故障模型、多数派和故障域引入 Ceph，覆盖 RADOS/OSD/MON/PG/CRUSH、复制/EC、读写数据路径、恢复重建与尾延迟，以及 RBD/CephFS/RGW 职责；Lab 09 为无特权本地模拟及测试。明确 MON 使用 Paxos-based 机制/变体而非 Raft；真实 Ceph 部署纯选做，不默认要求集群、root 或真实数据盘。
- [M10 复制与共识](modules/M10-复制与共识/README.md)：两课讲 Raft 安全性、leader election/term/log replication/commit/applied、快照、joint consensus 与网络分区；Lab 10 提供硬件无关、确定性输入的 Python 内存模拟器及 12 项故障/安全测试，明确不是生产协议实现。对照 etcd Raft v3.6.0 固定源码阅读边界，并与 Ceph MON Paxos-based 机制区分。
- [M11 分布式事务与数据库](modules/M11-分布式事务与数据库/README.md)：两课讲 MVCC/隔离级别/冲突与 write skew、2PC 阻塞和恢复、Percolator primary/secondary 思想，以及 TiDB/TiKV/PD/TiFlash 职责与 Region 事务路径；Lab 11 提供纯本地 Python 模拟和 11 项自动测试。案例固定 TiDB/TiKV v8.5.8，明确区分经典论文、产品版本文档与现代实现；TiUP Playground 不作为必做路径，也不默认追最新版本。
- [M12 性能工程](modules/M12-性能工程/README.md)：两课覆盖工作负载、吞吐/延迟分位数/重复误差、缓存、饱和与背压；Lab 12 提供安全临时普通文件基准工具（输出 p50/p95/p99、固定种子/参数与 SHA-256 校验）及 MiniStore 风格 LRU/FIFO 命中率模拟。可选 fio/RocksDB 对照固定版本与边界；不访问裸设备/云服务，不给无硬件条件的伪结论。
- [M12-L01：负载建模、延迟分位数与饱和背压](modules/M12-性能工程/M12-L01-负载建模、延迟分位数与饱和背压.md) · [M12-L02：fio、RocksDB 与缓存策略对照](modules/M12-性能工程/M12-L02-fio、RocksDB与缓存策略对照.md) · [Lab 12](labs/lab-12-performance-benchmark/README.md)
- [M13 云原生运维与可观测性](modules/M13-云原生运维与可观测性/README.md)：两课覆盖指标/日志/trace、健康与容量、RPO/RTO/备份恢复，容器/CSI 卷创建挂载快照恢复边界及 Ceph 运维案例；[Lab 13](labs/lab-13-local-observability/README.md) 使用 localhost JSONL、合成 metrics/trace 和故障诊断，不依赖 SaaS/云/集群。Kubernetes/Rook/Ceph 仅按固定候选版本选修；单节点不证明 HA。
- [M13-L01：存储可观测性、健康与恢复目标](modules/M13-云原生运维与可观测性/M13-L01-存储可观测性、健康与恢复目标.md) · [M13-L02：容器、CSI 卷生命周期与 Ceph 运维](modules/M13-云原生运维与可观测性/M13-L02-容器CSI卷生命周期与Ceph运维.md)
- [M14 安全与可靠性](modules/M14-安全与可靠性/README.md)：一课完整覆盖存储威胁模型/信任边界、AuthN/AuthZ、最小权限/租户隔离、静态数据和密钥、依赖供应链、备份恢复及危险操作审查；[M14-L01 课文](modules/M14-安全与可靠性/M14-L01-存储威胁模型、数据保护与安全变更.md)附 NIST/OWASP/Linux/OpenSSF 来源，[Lab 14](labs/lab-14-security-audit/README.md) 对自建临时目录做权限、输入/校验和错误路径审计（8 个演示检查、10 项自动测试），全程离线且不触碰用户数据/裸盘。明确 MiniStore 是无生产认证、授权、租户隔离、加密/密钥、备份及供应链保证的教学原型。
- [M03-L01：VFS、inode/dentry 与持久化边界](modules/M03-文件系统与FUSE/M03-L01-VFS、inode-dentry与持久化边界.md)。
- [M03-L02：FUSE 用户态文件系统设计与实现](modules/M03-文件系统与FUSE/M03-L02-FUSE用户态文件系统设计与实现.md)：含调用链、缓存/错误/生命周期与安全边界。
- [Lab 04：VFS 与 FUSE 内存文件系统](labs/lab-04-vfs-fuse/README.md)：默认测试无需挂载；真实 libfuse3 挂载可选。
- [MiniStore 项目](projects/MiniStore/README.md)：本地目录安全边界、KV/有限对象 CLI 契约、持久化格式、恢复、`make ci` 与 MiniStore CLI 基准说明。
- [M15 MiniStore 综合项目](modules/M15-综合项目/README.md)：三课串联 M05 KV/WAL、M07 进程级故障恢复、M08 本地对象 API 和 M12 性能验证；包含 [L01 架构与阶段集成](modules/M15-综合项目/M15-L01-架构、接口契约与阶段集成.md)、[L02 端到端验收演练](modules/M15-综合项目/M15-L02-端到端正确性、重启与验收演练.md)、[L03 基准/发布/限制复盘](modules/M15-综合项目/M15-L03-基准报告、发布清单与限制复盘.md)。MiniStore 是单机教学原型，不宣称生产安全、S3 或分布式一致性。
- [Lab 06：MiniStore 进程级崩溃一致性](labs/lab-06-crash-consistency/README.md)：仅对测试临时库做 SIGKILL/阶段性注入，验证 WAL/compaction 恢复；不模拟物理掉电。
- [Lab 07：MiniStore 本地对象 API](labs/lab-07-object-api/README.md)：项目自带的本地黑盒客户端测试对象二进制读写、覆盖、删除、列举及锁/错误语义；无需 AWS CLI、RGW、云账号或网络服务。
- [课程课文模板](templates/lesson-template.md)

> M00–M15 的 34 篇课文与模块 README 已完成；各 lab/project 的实测进度及未验证项见[模块索引](modules/README.md)。文档完成、当前机器本地测试通过、候选发行版/硬件/云/Kubernetes/SPDK 等软件栈集成验证是不同状态。必做实验按本地优先设计；上游包、源码和镜像可预先缓存，运行时不依赖第三方云服务。使用外部产品和软件栈时，按版本矩阵核实环境、依赖与安全边界。
