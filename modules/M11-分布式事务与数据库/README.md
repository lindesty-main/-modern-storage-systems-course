# M11：分布式事务与数据库

> 状态：两课正文与 Lab 11 本地 MVCC/事务恢复模型已完成。课程案例锁定 TiDB/TiKV/PD/TiFlash v8.5.8；TiUP v1.17.1 Playground 仅为显式固定版本的选修项。必做路径不需要数据库、集群、云服务或外部依赖。

## 学习目标

- 用 MVCC 版本链、事务快照和逻辑时间戳解释可见性与写写冲突。
- 对比读已提交、可重复读、快照隔离和可串行化语义；识别 SI 的 write skew 边界。
- 描述两阶段提交 prepare/commit、prepared 锁和协调者故障后的阻塞/恢复。
- 说明 Percolator primary/secondary lock 与 primary 决定点的思想。
- 区分 TiDB、TiKV、PD、TiFlash 职责、SQL→Region→事务路径、Region 内 Raft 与跨 Region 事务。
- 明确区分论文模型、版本化产品文档、v8.5.8 发布说明/源码和本地教学模拟的边界。

## 课次与实验

| 课次 | 内容 | 产出 |
|---|---|---|
| [M11-L01：MVCC、隔离级别与冲突判断](M11-L01-MVCC、隔离级别与冲突判断.md) | start/commit timestamp、快照可见性、SI first-committer-wins、锁/写冲突、write skew | 能判断给定事务读到的版本、冲突失败原因和 SI 不能保护的跨 key 约束 |
| [M11-L02：两阶段提交、Percolator 思想与 TiDB 数据路径](M11-L02-两阶段提交、Percolator思想与TiDB数据路径.md) | 2PC 决定与阻塞、primary/secondary recovery、TiDB/TiKV/PD/TiFlash 职责、Region 与 Multi-Raft 层次 | 能解释 prepared lock 未知决定为何阻塞，以及恢复决定如何释放/resolve |
| [Lab 11：MVCC 与分布式事务恢复模拟](../../labs/lab-11-transaction-simulation/README.md) | Python 标准库内存模型与 11 项自动测试 | 提交测试输出和一段故障轨迹说明；无需 TiDB/网络/云/第三方包 |

## 默认本地路径

```bash
cd labs/lab-11-transaction-simulation
python3 -m unittest -v test_transaction_sim.py
```

Lab 11 用显式逻辑时间戳维护每个 key 的 MVCC 版本，用固定 key→participant 映射模拟多个 Region participant；其 Percolator-inspired 2PC 模型包含 prewrite lock、primary 决定、secondary 延迟 resolve、未知决定阻塞和恢复。模型有意允许 SI write skew。它不是生产数据库、经典 2PC 全功能实现或 TiDB/TiKV 兼容测试。

## 版本/实现边界

- TiDB/TiKV/PD/TiFlash v8.5.8 是课程案例版本矩阵固定的阅读基线，TiUP v1.17.1 是可选管理器基线；并未由该模块部署或集成验证。
- Playground 可选，运行时必须显式选择 v8.5.8 并核对组件版本清单；不省略版本、不自动追随最新组件。必做实验只用本地 Python 标准库。
- 经典 2PC、Percolator 论文帮助解释事务协议与设计思想；不能将其直接等同现代 TiDB v8.5.8 的事务具体实现。产品路径可能依事务模式、配置与优化（例如 1PC/async commit）不同，以版本化官方文档和源码为准。
- Region 是 key range 与 Raft group 的管理边界；Raft 解决 Region 副本复制，事务提交协调跨 Region 原子结果，两者互补但不是同一协议。TiFlash 是分析型副本/读取路径，不能混作事务协调者。

## 来源


- 论文：[Percolator](https://research.google/pubs/large-scale-incremental-processing-using-distributed-transactions-and-notifications/)、[ANSI 隔离级别批评](https://doi.org/10.1145/223784.223785)、[Snapshot Isolation 可串行化研究](https://doi.org/10.1145/1007568.1007622)。
- 产品案例：[TiDB 当前架构文档](https://docs.pingcap.com/tidb/stable/tidb-architecture/)、[当前事务概览](https://docs.pingcap.com/tidb/stable/transaction-overview/)及[课程版本与实验环境](../../课程大纲/版本与实验环境.md)。在线 `stable` 文档不是 v8.5.8 锁定文档；v8.5 文档路径在本课核验时重定向到 stable，版本细节以 v8.5.8 发布说明和 `v8.5.8` 源码 tag 为准。
