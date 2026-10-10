# Lab 10：Raft 确定性状态机模拟

> **安全边界：**只运行 Python 标准库代码，在内存里模拟节点和显式消息队列。不创建 socket、不访问磁盘设备或真实数据、不需要 root/集群/网络服务。程序用于教学，不是可部署的 Raft 协议实现。

## 目标

- 逐事件追踪 election、term、RequestVote 与多数派。
- 区分本地 append、复制、commitIndex 和 lastApplied；验证旧 term 条目不能单独被 leader 提交。
- 注入节点 crash/restart、网络分区与恢复，观察旧 leader 退位和未提交后缀的冲突修复。
- 观察 lagging follower 安装 snapshot，以及 membership joint quorum 如何要求新旧配置各自多数派。
- 对照 Raft 算法模型和 etcd Raft 源码，但不把模拟器结果说成产品实现验证。

## 环境与运行

Python 3.10+，无第三方依赖。从本目录运行：

```bash
python3 -m unittest -v test_raft_sim.py
python3 raft_sim.py --demo
```

测试会确定性运行 12 项场景。示例程序期望输出：

```text
leader=n1 term=1
committed_index=1 proposal_index=1
applied=['put block-7 checksum=abc123']
```

## 模拟器 API 与一段故障轨迹

`RaftCluster` 提供显式事件方法：

- `start_election(node_id)`：输入一个本地 election-timeout 事件，节点开始新 term 选举。
- `run()`：按 FIFO 处理当前可达消息，直至队列为空；分区/宕机导致的消息会被丢弃并记录到 `trace`。
- `partition((...), (...))` / `heal()`：在分组间切断/恢复双向链路。
- `propose(command, leader_id=...)`：让指定 leader 本地 append 命令并发送复制请求。
- `crash(node_id)` / `restart(node_id)`：模拟节点停止/恢复；重启保留 term/vote/log/snapshot，提交和应用位置从快照恢复后再向 leader 追赶。
- `snapshot(node_id)`：对已应用状态做 snapshot；落后节点通过 `InstallSnapshot` 追赶。
- `add_node()`、`begin_membership_change()`、`change_membership()`、`finalize_membership()`：模拟 learner 加入、joint quorum 和 C_new 最终配置阶段。
- `assert_safety()`：检查本次模拟可观察到的同 term 单 leader、已提交日志前缀与已应用状态机一致性。

例：隔离旧 leader 后，少数派不能提交；多数派可选新 leader：

```python
from raft_sim import RaftCluster

c = RaftCluster(("n1", "n2", "n3"))
c.start_election("n1")
c.run()
c.partition(("n1",), ("n2", "n3"))
c.propose("write-x", leader_id="n1")
c.run()
assert c.nodes["n1"].commit_index == 0

c.start_election("n2")
c.run()
assert c.nodes["n2"].role == "leader"
# 网络恢复后，n1 收到更高 term 并退位；新 leader 修复日志。
c.heal()
c.replicate_from("n2")
c.run()
c.assert_safety()
```

更深入地查看 `c.trace`，按事件解释哪个节点发消息、哪条链接不可达、哪个 index 已复制/提交/应用，以及为什么。不要只提交最终 `leader` 名称。

## 测试覆盖

`test_raft_sim.py` 检查：

1. 无 quorum 时不能选出 leader，恢复连通后按 term 再选举。
2. 分区少数派旧 leader 与多数派新 leader 的不同视图；恢复后旧 leader 退位。
3. 多数派复制前不 commit/apply，恢复连通后按序提交并应用。
4. 新 leader 的 up-to-date 规则拒绝缺少已提交日志的候选人。
5. 新 leader 替换旧 leader 的冲突未提交后缀，已提交条目不丢失。
6. 旧 term entry 需等当前 term entry 复制到 quorum 后才能共同提交。
7. leader crash 后 survivors 选举并复制，重启节点再追赶。
8. 落后节点安装快照并继续追赶。
9. joint entry 本身要求旧、新 voter 集合各自 quorum；缺少新配置多数派时不能提交。
10. joint entry 提交后，最终 C_new entry 可在旧配置分区时通过新配置 quorum 完成。
11. 演练完整成员切换与落后节点补收配置条目，检查已提交配置后追赶的日志/状态。

## 算法状态机的教学抽象

模型包括 follower/candidate/leader、单调 term、每 term 一票、按 `(lastLogTerm,lastLogIndex)` 投票、AppendEntries 前缀匹配、冲突未提交后缀回退、当前 term commit 规则、commit/apply 分离、snapshot/InstallSnapshot、crash/restart，以及稳定/联合配置的多数派规则。

消息和输入全是本地 Python 对象，节点 ID 与测试事件固定，不用随机种子、CPU 计时、磁盘设备或真实网络。测试可重复；其含义只限于该模型内的断言。

## 明确的模型边界

- 不实现完整 Raft：无真实超时/随机 election backoff、pre-vote、leader transfer、真实时钟/租约、消息并发/乱序、磁盘 I/O/持久化、网络故障探测和生产级恢复逻辑。
- 无状态机崩溃一致性或 fsync 语义。`crash/restart` 是协议字段生命周期模型，不是进程 SIGKILL、内核故障或掉电测试。
- 没有客户端线性一致读、request de-duplication、API 错误重试、背压、资源限制或性能测量。
- `run()` 处理队列直至为空；这是离散事件教学模型，不是网络调度/可用性证明。
- 快照状态由已应用命令序列表示；为便于测试成员变更，模拟器在 leader 追加 joint/final 配置 entry 时由 cluster 对象集中切换当前配置，省略真实节点按各自日志逐步观察配置的过程。joint entry 的提交要求 C_old 与 C_new 双多数派；最终 C_new entry 使用 C_new quorum。真实实现的局部状态与时序须按论文和固定版本源码核对。
- 为保持简化，成员变更要求当前 leader 留在新配置中；不模拟 leader 被移除后的领导权转移、复杂 learner 管理或崩溃期间配置恢复。
- 不模拟拜占庭故障、恶意节点、磁盘静默损坏、客户端身份或数据加密。

因此，该实验不能证明“我的 Raft 实现正确”、etcd/Ceph 部署安全、高可用达到某个 SLA、磁盘已持久化或吞吐/延迟表现。

## 阅读来源

- 算法：[Raft 论文（Ongaro & Ousterhout, USENIX ATC 2014）](https://raft.github.io/raft.pdf)。
- 实现边界：[etcd Raft v3.6.0 README](https://github.com/etcd-io/raft/blob/v3.6.0/README.md)、[`raft.go`](https://github.com/etcd-io/raft/blob/v3.6.0/raft.go)、[`rawnode.go`](https://github.com/etcd-io/raft/blob/v3.6.0/rawnode.go)、[`confchange/confchange.go`](https://github.com/etcd-io/raft/blob/v3.6.0/confchange/confchange.go)。tag `v3.6.0` peeled commit 为 [`f35a02212416d045c574e30aa59d4b07c4b8d732`](https://github.com/etcd-io/raft/commit/f35a02212416d045c574e30aa59d4b07c4b8d732)；该源码只作阅读基线，不是 Lab 10 依赖，也未由本实验构建/集成验证。
- M09 产品对照：[Ceph Tentacle MON 高可用与扩展性](https://docs.ceph.com/en/tentacle/architecture/scalability-high-availability/)，其中 Ceph MON 使用 Paxos-based 机制；不要把它称为 Raft。
