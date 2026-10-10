# M11-L02：两阶段提交、Percolator 思想与 TiDB 数据路径

> 建议课时：100–115 分钟（含 Lab 11 故障恢复演练）｜案例版本：TiDB/TiKV/PD/TiFlash v8.5.8，TiUP v1.17.1（按课程版本矩阵固定；本课不要求部署集群）

## 学习目标

1. 解释跨多个数据分片的一致提交为何需要事务协调，并画出 2PC 的 prepare 与 commit/abort 阶段。
2. 说明 2PC 的阻塞窗口：参与者已经 prepared，却无法获知全局决定时为什么不能自行解锁；描述决定日志恢复路径。
3. 解释经典 Percolator 的 primary/secondary lock、primary 决定点和延迟 resolve 思想，同时不把论文模型等同 TiDB v8.5.8 的完整实现。
4. 画出 TiDB、TiKV、PD、TiFlash 的职责及 SQL→Region→事务路径，区分 Region 内 Raft 复制与跨 Region 原子提交。
5. 使用本地模拟器重现未知决定阻塞和 primary 决定后的 secondary 恢复；无需云服务或真实集群。

## 1. 2PC：先准备，再发布全局决定

一个事务若修改多个参与者（例如多个 Region），逐个独立提交可能造成部分成功。抽象两阶段提交由协调者和参与者组成：

```text
阶段 1：Prepare / Prewrite（投票）
协调者 ──prepare──▶ P1 ──写入准备状态/锁──▶ yes
         ├────────▶ P2 ──写入准备状态/锁──▶ yes
         └────────▶ P3 ──失败或超时───────▶ no / unknown

阶段 2：Commit 或 Abort（全局决定）
全部 yes：协调者先持久记录 commit 决定，再通知参与者提交
任一 no：协调者记录 abort 决定，再通知参与者回滚
```

参与者准备成功后已经承诺可以按协议提交，但尚未看到决定前不能把暂存写当作已提交数据。协调者必须把决定持久化后再发送依赖该决定的消息。收到回复数量不足、超时或网络断开，**不等同于 no，也不等同于 commit**。

### 阻塞窗口与恢复

若所有参与者已经 prepared，而协调者在决定可查询前崩溃，参与者只知道“我投了 yes”，并不知道其他节点的投票和最终决定。它不能自行提交，也不能安全地擅自回滚：其他参与者可能已经收到 commit。经典 2PC 因此可能保留锁并阻塞冲突操作，直到协调者恢复并从持久日志恢复决定，或协议提供的恢复规则能证明结果。

```text
P1: PREPARED + lock ──┐
P2: PREPARED + lock ──┼── coordinator unavailable, decision unknown
                      └── conflicting access waits/blocks
                         coordinator recovers durable COMMIT/ABORT
                         → participants finish and release locks
```

2PC 提供事务决定协议，不自动解决协调者持久性、超时误判、事务重试或应用幂等。事务参与者通常还运行自己的复制协议；“参加事务投票”与“为本地数据取得 Raft quorum”是不同层次的条件。

## 2. Percolator 的 primary/secondary 思想

Google Percolator 论文提出在 MVCC/锁基础上的分布式事务方案。可用于教学的抽象流程：

1. 选择一个写入 key 作为 **primary**，其余写入是 **secondary**。
2. **Prewrite：**参与者检查冲突并写入带有事务 `start_ts`、primary 引用和值的锁；若任一参与者不能准备，则事务不能进入成功提交。
3. **Primary commit：**primary 写入 `commit_ts` 决定，是该事务提交/中止状态的权威查询点。提交决定形成后不可改成 abort。
4. **Resolve secondary：**secondary 锁可在随后被清理；发现锁的读者/恢复任务按协议查询 primary，确认 commit 后补成已提交版本，若确认 abort 则清锁。

“primary”是这笔事务的决定锚点，不是 TiKV 集群中负责整个数据库的永久 leader，也不能因名称相同就等同 Region Raft leader。去中心化思想是让事务相关存储节点承载决定/恢复职责，而不是让所有事务都依赖一个外部集中事务服务。

Lab 11 将 primary 决定记录留在模拟器内存对象中：

- 所有 key 先 prewrite；未知决定时，读到锁会阻塞。
- primary commit 后，secondary 可暂不物化；恢复/读取时查询 primary 并按 commit 决定 resolve。
- primary 仍无决定时不会猜测结果；演练者恢复 primary 后显式记录 abort，释放锁。

该行为是**简化的 Percolator-inspired 教学抽象**，不是原论文的逐字实现，也不模拟 TiDB 的 lock TTL、事务回滚、异步提交、1PC、悲观事务、重试、Raft 日志或 RocksDB 落盘。

## 3. 四个部件与一条事务路径

```text
SQL Client
   │ MySQL 协议
   ▼
TiDB Server ──SQL 解析/优化/执行与事务协调逻辑──┐
   │ KV API / 路由元信息                         │ TSO / 元信息 / 调度
   ▼                                             ▼
TiKV Region A (key range, Raft group) ◀──────── PD
   │ MVCC versions / locks                       │
TiKV Region B (another key range, Raft group) ◀──┘
   │
   └── replicated changes → TiFlash analytical replica (where configured)
```

| 组件/概念 | 本课中的职责 | 不应推断 |
|---|---|---|
| **TiDB Server** | SQL 协议、解析/优化/执行，并将 KV 操作路由到相应 TiKV Region；可以有多个无状态计算实例 | “无状态”不代表事务没有状态；事务上下文、连接状态和恢复语义需按版本/API 查证 |
| **TiKV** | 分布式事务 KV 存储；保存 MVCC 版本/锁；按 Region 组织 key range 与副本 | 一个 TiKV 进程不等于一个事务参与者或一个 Region |
| **Region** | 有边界的 key range 管理单位；其副本构成独立 Raft group | Region 不是一台机器，也不是数据库表；分裂/合并会改变路由边界 |
| **PD / TSO** | 集群元信息与调度服务；提供事务时间戳相关的全局排序机制 | TSO 数值不等于严格墙钟时间，也不代表所有事务路径无等待 |
| **TiFlash** | 配置后提供分析型列存副本/读取路径；课程概念图把它画作从 TiKV 接收复制数据的 learner-style 分析副本 | 不是 2PC 协调者，也不是 TiKV 行存事务参与者的简单同义词；同步实现、可读状态和优化器选择按 v8.5.8 发布说明/源码 tag 核对 |

### 一笔跨 Region 写入的概念路径

1. TiDB 解析 SQL、生成计划，并依据表/索引编码把数据操作映射为 KV key；具体编码格式按固定版本源码核对，课内不要求手工构造线上 key。
2. 事务取得快照时间戳 `start_ts`，按可见性规则读取 MVCC 版本；事务将写集暂存。
3. 跨越多个 Region 的写集要经过事务提交协调：冲突检查、写入 prewrite/锁，再形成全局提交决定与 `commit_ts`。TiKV 的各 Region 处理相应的事务操作。
4. 每个 Region 内部仍通过自己的 Raft group 复制状态；事务 2PC 跨 Region 协调原子结果。两者职责互补，不是“Raft 等于 2PC”。
5. TiFlash 若已配置，可作为分析查询数据路径（例如优化器在副本满足要求时将算子下推）；它不改变 OLTP 事务的逻辑提交边界。具体副本同步、可读状态和计划选择以 v8.5.8 发布说明/源码 tag 为准。

## 4. 固定版本边界：论文模型与现代产品各有用途

**课程案例基线**来自[版本与实验环境](../../课程大纲/版本与实验环境.md)：TiDB/TiKV/PD/TiFlash v8.5.8，TiUP v1.17.1。它是指定的阅读/可选部署组合，不代表这里已下载、部署或集成验证。

PingCAP 在线事务文档提供当前产品行为说明，但其旧版本路径在本课核对时会重定向到 `stable`；实际 v8.5.8 路径还须结合对应发布说明和源码 tag，并注意 optimistic/pessimistic 模式、配置、语句类型、事务大小和协议优化影响。现代 TiDB 还存在 1PC、async commit 等优化/路径。因此：

- 课程用经典 2PC 讲清“投票—全局决定—恢复”的正确性问题；不要说 v8.5.8 每笔事务一定严格走教科书版两个完整 RTT。
- Percolator 是重要设计思想和论文来源，不等同 TiDB v8.5.8 的完整源码实现。
- 读写流程、Region/事务细节及 TiFlash 能力以 v8.5.8 发行说明和对应源码 tag 为准；PingCAP 在线 `stable` 文档仅作概念入口，不能替代锁定版本的产品承诺。
- 版本矩阵中 TiDB 8.5.8 的 Playground 是**纯选修**。若选择运行，必须显式指定 v8.5.8 并确认 TiDB/TiKV/PD/TiFlash 组件清单；绝不省略版本让工具默认追最新。必做实验完全本地、无需 TiUP、数据库、云服务或集群。

## 5. 故障演练与检查题

运行 Lab 11 测试：

```bash
cd labs/lab-11-transaction-simulation
python3 -m unittest -v test_transaction_sim.py
```

重点观察：

- prepared 后 primary crash、决定未知：secondary key 的读因未决锁阻塞；恢复 primary 并写入 abort 决定后，旧版本可读。
- primary commit 后 secondary 尚未 resolve：恢复 secondary 后依据已提交决定补出版本；不能把已提交事务改成 abort。
- secondary 自身离线时，提交决定可已确定，但访问该参与者仍会不可用；恢复后再 resolve。

问题：

1. 一个 Region Raft group 的多数派已确认本地日志，但另一个 Region 仍未完成 prewrite。整个事务是否因此可以宣告 commit？说明两个协议层次。
2. 若协调者在全部 yes 后、写入持久决定前崩溃，参与者为什么不能只靠超时擅自提交？
3. primary 已记录 commit，某 secondary 长时间离线。该事务状态是“未知”还是“已决定但未物化”？读取该 Region 时还可能遇到什么？
4. 选择 M11 固定版本后，哪些实现说法需要查 v8.5.8 发行说明/源码 tag，而不能直接从 Percolator 论文推断？

## 6. 来源


- Google, [Percolator: Large-scale Incremental Processing Using Distributed Transactions and Notifications](https://research.google/pubs/large-scale-incremental-processing-using-distributed-transactions-and-notifications/)。论文用于理解 primary/secondary 和通知/恢复思想，不是现代 TiDB 实现说明。
- PingCAP 当前在线文档：[TiDB 架构](https://docs.pingcap.com/tidb/stable/tidb-architecture/)、[事务概览](https://docs.pingcap.com/tidb/stable/transaction-overview/)、[乐观事务](https://docs.pingcap.com/tidb/stable/optimistic-transaction/)、[悲观事务](https://docs.pingcap.com/tidb/stable/pessimistic-transaction/)、[TiFlash 概览](https://docs.pingcap.com/tidb/stable/tiflash-overview/)；这些页面会随文档站更新，不是 v8.5.8 锁定文档。课程版本基线见[官方 8.5.8 发布说明](https://docs.pingcap.com/tidb/stable/release-8.5.8/)和环境矩阵，具体实现按下列 tag 源码核对。
- 源码阅读入口：[TiDB v8.5.8](https://github.com/pingcap/tidb/tree/v8.5.8)、[TiKV v8.5.8](https://github.com/tikv/tikv/tree/v8.5.8)。使用课程固定 tag 核对具体实现；本课程没有构建/集成验证该源码。
- 本地演练：[Lab 11：MVCC 与事务恢复模拟](../../labs/lab-11-transaction-simulation/README.md)。模拟器只验证自身有限规则。
