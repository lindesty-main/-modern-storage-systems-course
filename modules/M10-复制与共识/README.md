# M10：复制与共识

> 状态：两课正文与 Lab 10 确定性模拟器已完成。Raft 代码是教学模型，不是可部署协议或生产实现。

## 学习目标

- 按 term、角色、日志索引和多数派规则推演 Raft 的选举、日志复制与分区恢复。
- 严格区分“写入本地日志”“复制到多数派”“提交（commit）”和“应用（apply）”，说明 Raft 的核心安全性限制。
- 解释 `commitIndex`、`lastApplied`、快照边界与 joint consensus 成员变更的关系。
- 将论文中的算法状态机与固定版本 etcd Raft 的 API/持久化边界对照；不把实现细节当成算法定义。
- 说明 Ceph MON 使用基于 Paxos 的机制，而不是 Raft；两者即使都使用多数派也不是同一协议。

## 课次与实验

| 课次 | 内容 | 产出 |
|---|---|---|
| [M10-L01：Raft 安全核心、选举与日志复制](M10-L01-Raft安全核心、选举与日志复制.md) | 角色/term、RequestVote、日志新旧比较、AppendEntries、日志匹配、commit/apply、当前 term 提交规则、分区与恢复 | 能逐条解释一次选举、一次提交和一次网络分区后旧日志如何被覆盖，且不把“收到多数派 ACK”含糊称为“已经 apply” |
| [M10-L02：快照、成员变更与实现边界](M10-L02-快照、成员变更与实现边界.md) | 日志压缩与 InstallSnapshot、joint consensus、etcd Raft v3.6.0 源码/API 对照、Ceph MON Paxos 对比 | 完成 [Lab 10 Raft 确定性状态机模拟](../../labs/lab-10-raft-simulation/README.md)，提交一段故障轨迹和边界说明 |

## 安全实验路径

Lab 10 只使用 Python 标准库，在内存中按显式事件和 FIFO 消息队列推进三到五节点的简化状态机。不打开网络 socket、不访问设备或真实数据、不需要 root、集群或第三方服务。固定节点 ID、事件顺序和消息投递规则使测试可重复；它不是完整 Raft 兼容实现，也不作为生产实现模板。

必做验收是两课、12 项自动测试和至少一段自行解释的故障轨迹。真实 etcd/Ceph 集群、磁盘和网络故障注入均不要求。

## 课程边界与来源

- 算法定义主线：[Ongaro 与 Ousterhout, Raft, USENIX ATC 2014](https://raft.github.io/raft.pdf)及 [Raft 博士论文](https://github.com/ongardie/dissertation)。论文用于解释协议模型；它不是 etcd 或 Ceph 的实现手册。
- 具体实现阅读基线：[`etcd-io/raft` v3.6.0](https://github.com/etcd-io/raft/tree/v3.6.0)。仅作为固定源码阅读版本，未在本课程中构建/运行该库，也不代表 etcd server 的完整版本锁。API、成员变更细节须以该 tag 的 README/源码为准。
- Ceph 对照：[Tentacle MON 高可用与扩展性文档](https://docs.ceph.com/en/tentacle/architecture/scalability-high-availability/)。Ceph MON 使用 Paxos-based 机制维护集群控制状态；本课的 Raft 状态机不能用于说明 MON 的内部实现。
- Lab 10 模型有意省略真实时间、随机 election timeout、磁盘持久化、并发/乱序网络、多数优化、客户端线性一致读和生产级配置恢复；见实验说明的模型边界。
