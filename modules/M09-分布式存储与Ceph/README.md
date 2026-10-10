# M09：分布式存储与 Ceph

> 状态：两课正文与本地安全模拟实验已完成。主线无需 Ceph 集群、root、专用设备或网络服务；真实部署仅为纯选做，不构成课程验收。

## 学习目标

- 在阅读 Ceph 前说明 crash、慢节点、网络分区、仲裁和 failure domain 的差异。
- 追踪 Ceph RADOS 的对象 → PG → CRUSH → OSD 放置链，以及 MON/OSD 的不同职责。
- 画出副本/纠删码下读写、peering、恢复与重建路径，解释后台恢复流量为何影响前台尾延迟。
- 区分 RBD、CephFS、RGW 在协议、数据路径和组件职责上的边界。
- 清楚表述：Ceph MON 使用基于 Paxos 的一致性机制（Ceph 的 Paxos 变体/实现），**不是 Raft**；M10 才系统讲 Raft。

## 课次与实验

| 课次 | 内容 | 产出 |
|---|---|---|
| [M09-L01：故障模型、故障域与 Ceph 放置基础](M09-L01-故障模型、故障域与Ceph放置基础.md) | 分布式先导、MON quorum、OSD 副本状态、RADOS/OSD/MON/PG/CRUSH、副本与纠删码的放置直觉 | 能根据故障场景解释哪些控制状态仍可达、哪些 PG/对象处于降级状态 |
| [M09-L02：RADOS 数据路径、恢复重建与接口职责](M09-L02-RADOS数据路径、恢复重建与接口职责.md) | 读写路径、peering/recovery/backfill、恢复对尾延迟的影响、RBD/CephFS/RGW 分工 | 完成[Lab 09 故障域与重建压力模拟](../../labs/lab-09-ceph-failure-simulation/README.md)并解释模型边界 |

## 安全实验路径

Lab 09 仅使用 Python 标准库，在内存里模拟确定性副本放置、故障域丢失、重建副本数量和合成前后台负载。它不会访问真实数据或系统资源。必做内容就是课文、模拟器和测试；Ceph 部署、Kubernetes/Rook、真实盘、root 权限都不是先修或默认要求。

若教师另行提供 Ceph 选修部署，必须放在隔离 VM，OSD 仅使用该 VM 内专用、可丢弃的虚拟盘，明确盘会被清空并逐盘确认。不得照抄旧讲义对主机目录执行 `rm -rf`，不得使用宿主机系统盘/用户数据盘，也不得把单节点测试描述为高可用证明。

## 资料来源与版本

- 课程案例版本：Ceph Tentacle / v20.2.4，见[版本与实验环境](../../课程大纲/版本与实验环境.md)；这是课程候选阅读基线，不表示本模块已部署或集成验证该集群版本。
- 上游版本化文档：[架构](https://docs.ceph.com/en/tentacle/architecture/)、[高可用与扩展性](https://docs.ceph.com/en/tentacle/architecture/scalability-high-availability/)、[CRUSH map](https://docs.ceph.com/en/tentacle/rados/operations/crush-map/)、[PG](https://docs.ceph.com/en/tentacle/rados/operations/placement-groups/)、[纠删码](https://docs.ceph.com/en/tentacle/rados/operations/erasure-code/)及 [RBD](https://docs.ceph.com/en/tentacle/rbd/)、[CephFS](https://docs.ceph.com/en/tentacle/cephfs/)、[RGW](https://docs.ceph.com/en/tentacle/radosgw/) 文档。引用产品行为时须按实际 release/configuration 复核。
- 本模块使用版本化 Ceph 官方文档核对架构和运维边界。真实部署为选修；应按目标 release 使用受支持的 cephadm 或 Rook 流程，并在隔离环境和专用虚拟盘中操作。
- 版本与术语核对日期：2026-10-09；课程候选版本及集成状态以后续锁定记录为准。
