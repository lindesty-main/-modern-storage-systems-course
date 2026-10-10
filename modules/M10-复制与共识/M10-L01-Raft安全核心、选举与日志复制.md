# M10-L01：Raft 安全核心、选举与日志复制

> 状态：课程正文　｜　建议课时：100–115 分钟　｜　先修：[M09 分布式存储与 Ceph](../M09-分布式存储与Ceph/README.md)、M07 持久性与崩溃一致性　｜　算法主线：Raft 论文（USENIX ATC 2014）

## 学习目标

学完后你应能：

1. 给出 Raft 的角色、term、持久/易失状态，并逐步推演选举和日志复制。
2. 根据最后日志的 `(lastLogTerm, lastLogIndex)` 比较候选人是否足够新，解释为何“先到先得”不安全。
3. 区分日志已追加、已复制到多数派、已提交和已应用四个状态。
4. 解释 `AppendEntries` 如何验证共同前缀、删除冲突的未提交后缀，以及 leader 为什么只能直接提交自己当前 term 的日志条目。
5. 分析网络分区时旧 leader、少数派和多数派各自能/不能做什么，并描述恢复连接后的 term 与日志修复。

## 教学节奏

| 时间 | 内容 |
|---|---|
| 0–15 分钟 | 状态机复制：命令为何需要同一顺序 |
| 15–35 分钟 | 角色、term、投票规则和 leader election |
| 35–60 分钟 | `AppendEntries`、日志匹配与冲突修复 |
| 60–80 分钟 | commit、apply、当前 term 提交限制与安全性 |
| 80–105 分钟 | 分区演练、检查题与 Lab 10 准备 |

## 1. Raft 要解决的是什么

多个节点各自保存一份日志，让它们在 crash/recovery、消息延迟或分区时仍对一串操作达成同一顺序。确定性状态机（deterministic state machine）把已提交日志逐条应用到本地状态；若各节点从同一初始状态应用相同命令序列，就得到相同状态。

```text
客户端命令
   │
   ▼
Raft leader ──复制有序日志条目──► followers
   │                                │
   └──多数派确认并推进 commitIndex─┘
                     │
                     ▼
           各节点按序 apply 到状态机
```

Raft 不会自动定义应用 API、磁盘格式、客户端重试去重、快照备份策略或线性一致读 API。它提供复制日志的一致性协议；把它变成数据库/协调服务还需要应用层、持久化、传输和读路径设计。

本课使用 crash/omission 故障模型：节点可能停机/恢复，消息可能延迟或丢失/分区；不处理拜占庭节点任意撒谎。多数派能在**配置有效且节点通信恢复到足以组成多数派**时继续推进，但不能让任意网络分区两侧都成功写入。

## 2. 角色、term 和状态

任何时刻每个节点处于三种角色之一：

- **Follower**：默认状态；响应投票请求和 leader 的 `AppendEntries`/心跳。
- **Candidate**：本地选举超时后发起新 term 的选举。
- **Leader**：选举获胜后接收新命令，协调日志复制。稳定配置通常每个 term 至多一个有效 leader。

`term` 是单调递增的逻辑任期，可看成选举轮次。RPC 都带 term。收到更大的 term，节点必须更新 `currentTerm`、清空旧 `votedFor` 并退回 follower；旧 term 的投票、响应和 leader 身份不再有效。term 不是墙钟时间，也不表示租约还剩多久。

Raft 节点状态按持久性分层（论文中的核心状态；真实实现还会有更多运行状态）：

| 状态 | 持久性 | 含义 |
|---|---|---|
| `currentTerm` | 持久 | 已知的最高 term；重启后不能倒退 |
| `votedFor` | 持久 | 当前 term 已投给谁；防止同一节点在同一 term 投两票 |
| `log[]` | 持久 | 索引递增的命令/配置条目及各自 term |
| `commitIndex` | 易失 | 已知提交的最高日志索引 |
| `lastApplied` | 易失 | 已应用到状态机的最高索引 |
| `nextIndex[peer]` | leader 易失 | 下一次发送给该 follower 的预计日志索引 |
| `matchIndex[peer]` | leader 易失 | 已确认复制到该 follower 的最高索引 |

真实节点必须遵循“持久状态先于依赖它的协议响应/消息”的实现约束。内存模型本身不能提供掉电后持久性。快照会压缩一段旧日志，快照边界及状态见 M10-L02。

日志以 1 为首个索引，空日志的基准为 `(index=0, term=0)`。每条日志记录其创建 term。**索引表示顺序，term 表示谁在哪个任期创建条目**；相同 term 和 index 的条目必须相同（Log Matching Property）。

## 3. Leader election：超时触发，投票按日志新旧约束

Raft 论文通过随机化 election timeout 减少多个候选人同时起选举的概率。定时器超时只是“当前 follower 没听到有效 leader”的本地事件，不证明 leader 已死。生产实现需要配置心跳/选举 tick 与网络/负载条件；本课程模拟器将“某节点超时”作为确定性输入，不运行真实计时器。

选举状态变化：

```text
Follower (term=t)
  └─ election timeout
       ├─ currentTerm := t+1
       ├─ role := Candidate；votedFor := self；投自己一票
       └─ 向其他投票成员发送 RequestVote(term, lastLogTerm, lastLogIndex)
              ├─ 获得当前配置要求的多数派 → Leader
              ├─ 收到更高 term → Follower
              └─ 多个候选人分票/无多数派 → 超时后下一轮再选
```

投票规则不是“第一个到就给”：接收者仅在候选人的日志足够新时投票。比较 `(lastLogTerm, lastLogIndex)` 的字典序：先比较最后一条日志的 term；term 相同才比较 index。候选人最后 term 较大，或 term 相同且 index 不小于本地，才算足够新。每个节点每个 term 至多投一票；只有成员配置认可的票才能计入 quorum。

固定配置有 `N` 个 voter 时，多数派为 `floor(N/2)+1`。三节点需 2 票，可容忍一个节点不可用；五节点需 3 票，可容忍两个节点不可用。多数派不是“任意一半”，也不能把 witness、非投票成员或不在配置中的节点随意计入。成员变更会改变 quorum 规则，不能在运行中私自改分母（见 M10-L02）。

获胜者初始化每个 follower 的 `nextIndex`，随后发送 `AppendEntries`（无新条目时即 heartbeat）以确立当前 term 的领导关系。候选人身份、leader 身份和已提交状态仍受日志/term 规则约束；孤立节点可以保留过期的本地 leader 视图，但不能据此获得多数派提交能力。

## 4. 日志复制：先确认前缀，再追加后缀

leader 追加新命令到自己的日志，并向 follower 发送：`term`、`prevLogIndex`、`prevLogTerm`、后续条目和 `leaderCommit`。

```text
Leader log:    (1,A) (1,B) (2,C) (2,D)
                          ▲ prevLogIndex=3, prevLogTerm=2
Follower log:  (1,A) (1,B) (2,C) (?)
                              前缀匹配后才接受后续条目
```

Follower 的关键步骤：

1. 若 RPC term 低于本地 term，拒绝；若 term 更高，先更新 term 并退回 follower。
2. 检查本地 `prevLogIndex` 处是否存在 `prevLogTerm`。前缀不匹配就拒绝并提示冲突位置；leader 回退 `nextIndex` 后重试。
3. 前缀匹配后，追加缺失条目。若同一索引出现不同 term 的冲突，删除冲突位置及其后的**未提交**后缀，再追加 leader 的条目。已提交日志不得被覆盖。
4. 用 `min(leaderCommit, lastLogIndex)` 推进本地提交索引；只应用到状态机，不越过已知提交位置。

leader 根据成功响应更新 `matchIndex`，并据此前进 `nextIndex`。某 follower 离线会让复制落后；它恢复后，leader 通过冲突回退补传日志，若前缀已压缩到快照内则发送快照。超时或失败响应表示“这次未确认”，不表示对方一定永久死亡。

## 5. 从复制到 commit，再到 apply

务必区分下列状态：

1. **Appended**：条目只存在于某节点本地日志。leader 本地追加后仍可能丢失/被后续 leader 的日志覆盖。
2. **Replicated**：条目已复制到若干 follower。复制数尚未达到有效 quorum 时仍不能确认提交。
3. **Committed**：leader 知道该条目属于不会被合法新 leader 回滚的日志前缀，推进 `commitIndex`。Raft 客户端通常在对应条目提交后才返回成功，但具体响应 API 由应用决定。
4. **Applied**：节点按索引顺序把已提交条目交给状态机，推进 `lastApplied`。commit 不等同于状态机已完成 apply；应用崩溃恢复也必须保存/重放状态。

**当前 term 提交规则：**leader 只可通过“该索引已复制到当前配置的多数派”直接推进到一条当前 term 的日志。旧 term 条目不能只凭“现在已经复制到多数派”而单独推进 commitIndex。若某条当前 term 条目提交，则它之前匹配的日志前缀（包括旧 term 条目）一并提交。

例：term 1 的旧条目 `e1` 仅在少数节点；term 2 的 leader 后来把 `e1` 复制到某个多数派，但没有当前 term 的提交条目。leader 不能仅靠 `e1` 断言提交。它追加 term 2 条目 `e2`；当 `e2` 在多数派确认后，`e2` 及其之前匹配的 `e1` 一起提交。

这条规则与选举投票的新旧日志检查共同支撑 **Leader Completeness**（已提交条目会出现在后续 leader 日志中）和 **State Machine Safety**（不同节点不会把不同条目应用到同一索引）。它也解释了为什么 Raft leader 常会追加一个 no-op/current-term 条目来尽快提交之前的日志；本课程模拟器不自动追加该 no-op，以便显露规则本身。

## 6. 网络分区与节点故障：多数派可前进，少数派不能双写

```text
3 节点：旧 leader A | B、C
          1 票      | 2 票

A：仍可能在本地显示 term t 的 leader；无法取得 quorum，不能提交新写入。
B/C：可在更高 term 选出新 leader，并提交满足其日志安全条件的条目。
网络恢复：A 收到更高 term 后退为 follower，按新 leader 的共同前缀修复冲突的未提交后缀。
```

“少数派旧 leader 还活着”不等于存在两个能成功提交的 leader。Raft 的安全目标是避免冲突提交；分区时它牺牲少数派继续写入的能力。某个节点可以先返回超时/暂时错误，也可以保留本地未提交写，但应用不得把未提交结果当成一致提交。

分区恢复不是简单地把两边文件拼起来：新 term、RequestVote 日志新旧检查、AppendEntries 前缀匹配和未提交后缀替换共同决定唯一有效日志。已提交条目不能被删除。一个客户端请求若超时后重试，仍可能重复执行；命令 ID/去重是应用协议，不是 Raft 日志自动提供的业务语义。

Raft 写入 quorum 也不自动解决读取线性一致性。leader 在分区后必须确认自己仍有权服务读取；实际实现会采用 ReadIndex、适当的租约/检查或其它协议机制。不要仅凭“我是最近一次选出的 leader”就假定旧 leader 可以给出新鲜读。

## 7. Raft 与 M09 Ceph MON 的边界

两者都涉及复制状态和 quorum，但**共同使用多数派这个概念，不代表同一协议**：

- Raft 在本课作为通用复制状态机协议学习，具备 term、leader election、RequestVote、AppendEntries、日志匹配和 Raft 成员变更规则。
- Ceph MON 通过 Ceph 的 Paxos-based 机制协调集群 maps/控制状态。Ceph MON 不是按本课描述的 Raft `currentTerm`/`AppendEntries` 状态机来实现；不能把 Raft 教程中的 RPC、安全证明或实现差异直接套到 Ceph。
- Ceph OSD/PG 的数据复制可用性又是另一层；MON quorum 不是每个对象 payload 写入的 Raft 多数派投票。回顾 [M09-L01](../M09-分布式存储与Ceph/M09-L01-故障模型、故障域与Ceph放置基础.md)。

## 8. 检查题

1. 一个候选人的最后日志 `(term=4,index=8)`，接收者日志 `(term=5,index=6)`。谁更新？为什么 index 更大的候选人仍可能拿不到票？
2. 三节点集群中，leader 本地有一条新 entry，只有一个 follower 收到。它何时能推进 `commitIndex`？如果网络分成 1+2 呢？
3. 一条旧 term entry 已经在当前多数派上，但 leader 尚未复制任何当前 term entry。为什么不能直接宣布旧 entry 已提交？
4. `commitIndex=20,lastApplied=18` 说明什么？为什么不能把两个位置合并成“完成索引”？
5. 少数派中旧 leader 恢复网络后，哪些字段/消息让它放弃旧任期并收敛？

## 9. 参考与下节

- Diego Ongaro、John Ousterhout, [“In Search of an Understandable Consensus Algorithm (Extended Version)”](https://raft.github.io/raft.pdf), USENIX ATC 2014：协议术语、安全性和成员变更章节。
- [Raft dissertation](https://github.com/ongardie/dissertation)：论文扩展说明与证明背景；课程以论文模型为算法边界。
- 下一课：[M10-L02：快照、成员变更与实现边界](M10-L02-快照、成员变更与实现边界.md)。
- 配套实验：[Lab 10：Raft 确定性状态机模拟](../../labs/lab-10-raft-simulation/README.md)。
