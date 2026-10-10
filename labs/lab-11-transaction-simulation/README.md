# Lab 11：MVCC 与分布式事务恢复模拟

> **安全/依赖边界：**纯 Python 标准库、确定性内存状态；不启动 TiDB/TiKV，不创建 socket，不接触外部服务/云资源/真实数据，不需要 root。模拟器是简化的教学模型，不是数据库或生产事务实现。

## 目标

- 追踪 MVCC 的 `start_ts` / `commit_ts` 可见性和 SI 写写冲突。
- 重现 snapshot isolation 下不同 key 并发写导致的 write skew。
- 跟踪跨 participant 的 prewrite 锁、primary commit 决定和 secondary lock resolve。
- 注入 primary/secondary 节点不可用，解释未知决定为何阻塞，以及恢复后如何完成/回滚。

## 运行

要求 Python 3.10+，无第三方包：

```bash
cd labs/lab-11-transaction-simulation
python3 -m unittest -v test_transaction_sim.py
```

预期：11 项测试通过。测试不依赖固定 CPU 时间或随机输入。

## 模型 API 和事务轨迹

`transaction_sim.py` 的主要接口：

- `MVCCCluster({key: participant})`：固定 key 到 participant 的映射；时间戳由单进程逻辑计数器生成。
- `seed(key, value)`：安装初始提交版本。
- `begin(txid)`：创建固定快照事务；事务支持 `get`、`put`、`prepare`、`commit`、`abort`。
- `crash(name)` / `restart(name)`：让一个内存 participant 停止/恢复服务；其内存字段保留，以表示本模型假设的协议记录恢复。
- `read(key, read_ts)`：读取 `commit_ts <= read_ts` 的最新版本；若遇到锁则按 primary 决定 resolve，否则安全阻塞。

演示未知决定的阻塞与恢复：

```python
from transaction_sim import MVCCCluster, TransactionBlocked

c = MVCCCluster({"a": "r1", "b": "r2"})
c.seed("a", "a0")
c.seed("b", "b0")
tx = c.begin("tx-1")
tx.put("a", "a1")
tx.put("b", "b1")
tx.prepare()                  # 两个 participant 都持有 prewrite lock
c.crash("r1")               # primary 尚未记录提交/回滚决定
try:
    c.read("b", c.clock)     # secondary 无法安全猜测结果
except TransactionBlocked:
    pass
c.restart("r1")
tx.abort()                   # 恢复后协调路径记录 abort 并清理锁
assert c.read("b", c.clock) == "b0"
```

## 故障阻塞与恢复解释

- **Prepared + primary 决定未知：**参与者的锁代表可能仍有其他 yes，也可能协调者正要提交。它不能只因本地超时就把写入公开或删除锁；对冲突读取/写入会阻塞或报锁冲突。课程演练在 primary 恢复后显式选择 abort；真实系统按其持久状态、事务恢复协议、TTL/resolve 策略和版本实现处理，不可照搬该手工调用。
- **Primary commit 已记录：**这是不可反悔的提交决定。secondary 锁可留待之后 resolve；读到锁的一方查询 primary 决定后补出版本。secondary 宕机期间该 key 仍不可访问；primary commit 不代表所有副本/参与者都已完成物化。
- **Participant 离线：**即使全局决定已写入，访问离线数据仍会失败；节点恢复后还需重放/恢复/resolve。模型不会把“已有 commit 决定”偷换成“每个节点都立刻可读”。

## 测试覆盖

1. 快照读可见性、读己之写。
2. 同 key 的 first-committer-wins 写冲突，锁冲突及部分 prewrite 清理。
3. 不同 key 写集的 SI write skew。
4. 显式 abort 保留旧版本。
5. prepared 且 primary 不可用时阻塞，恢复后记录 abort 并释放锁。
6. primary commit 后 secondary 延迟 resolve，保留旧快照可见性。
7. secondary 已投 prepared YES 后离线，primary 仍记录提交决定；secondary 恢复后按决定 resolve。
8. commit 决定不能被反转；participant 缺席时 prepare 失败。

## 明确的教学抽象与限制

- **MVCC：**只实现 key/value 字符串的版本列表、单一逻辑时钟和 SI 的写写冲突；不实现 SQL、读已提交/可串行化、谓词锁、唯一索引、范围扫描、死锁、版本 GC、TTL、并发线程或持久化。
- **事务提交：**是 Percolator-inspired 的 primary/secondary 两阶段教学模型，不是完整经典 2PC、Google Percolator 原论文代码、TiDB 或 TiKV。participant 的决定记录放在 Python 对象里；`crash/restart` 保留对象状态，不模拟 SIGKILL、磁盘写入、WAL、fsync、真实协调器高可用或网络消息。
- **失败注入：**同步方法调用与布尔在线标志代替真实网络和时间；不模拟乱序、分区恢复、租约、客户端重试、锁 TTL、async commit、1PC、悲观事务优化、Raft replication 或真实 Region 元数据变更。
- **隔离语义：**SI 下仅作写写冲突验证，读集无串行化验证，故测试刻意允许 write skew。不同产品/版本的隔离与事务路径须另查官方文档。

因此，测试通过不能证明 2PC/Percolator/TiDB 正确、高可用或持久；它只检验这里写明的教学状态机。

## 阅读来源

- [M11-L01：MVCC、隔离级别与冲突判断](../../modules/M11-分布式事务与数据库/M11-L01-MVCC、隔离级别与冲突判断.md)
- [M11-L02：两阶段提交、Percolator 思想与 TiDB 数据路径](../../modules/M11-分布式事务与数据库/M11-L02-两阶段提交、Percolator思想与TiDB数据路径.md)
- [版本与实验环境](../../课程大纲/版本与实验环境.md)：TiDB/TiKV/PD/TiFlash v8.5.8、TiUP v1.17.1；Playground 为可选项，不允许默认跟随最新版。
- Google, [Percolator paper](https://research.google/pubs/large-scale-incremental-processing-using-distributed-transactions-and-notifications/)；论文是思想来源，不是现代 TiDB 实现说明。
