# M13-L02：容器、CSI 卷生命周期与 Ceph 运维

> 状态：课程正文　｜　建议课时：105–125 分钟　｜　主线以架构图、清单/状态阅读和 Lab 13 模拟为主；Kubernetes/Rook/Ceph 集群为选修，不是课程验收依赖。

## 学习目标

1. 区分容器运行时、kubelet、Kubernetes 控制器、CSI 驱动和存储后端职责。
2. 从 PVC 追踪 provision → schedule/attach → node stage/publish → mount → unmount/detach，定位不同层的 Pending/Failed 状态。
3. 解释 CSI snapshot API、snapshot controller、驱动、后端快照和从快照恢复新 PVC 的边界。
4. 运用只读状态证据分析 Ceph 容量、PG/OSD、MON quorum、客户端 slow ops 和恢复/重建压力。
5. 说明单节点实验、软件模拟和实际生产 HA 各自能证明什么。

## 1. 云原生存储请求并非一个“容器挂盘”动作

Kubernetes API 接收 PVC/Pod/StorageClass 等声明。控制器观察期望状态并协调资源；scheduler 为 Pod 选择节点；kubelet 在节点上协调 volume lifecycle；container runtime 创建容器及 mount namespace。**CSI（Container Storage Interface）**是编排系统和存储厂商驱动之间的 RPC/API 契约，不是文件系统，也不直接提供副本一致性或备份策略。

```text
应用 Pod/PVC
   │ claim + StorageClass
Kubernetes API / PV controller / scheduler
   ├── CSI external-provisioner → CSI controller → 后端创建 volume → PV
   ├── attach/detach controller → external-attacher → CSI ControllerPublish
   └── 被调度节点上的 kubelet
          → CSI node plugin: NodeStageVolume / NodePublishVolume
          → 节点设备/文件系统 mount namespace
          → 容器中的目标路径
```

是否调用 ControllerPublish 取决于驱动的 `attachRequired` 与卷模式；并非所有 CSI 卷都 attach 到云 VM。CSI sidecar 是 Kubernetes controller API 与 CSI driver 的协作组件，不是每个都由 CSI 规范规定；sidecar 版本要与 Kubernetes/driver 版本成组验证。

### 角色简表

| 组件 | 主要职责 | 不自动负责 |
|---|---|---|
| Container runtime（如 containerd） | 拉取/运行容器、namespace/cgroup 等运行时工作 | 给应用定义持久化语义或复制策略 |
| kubelet | 节点 Pod 生命周期、调用 volume manager/CSI node 服务、挂载到 Pod | 创建底层数据副本或判断业务数据是否正确 |
| PV/PVC/StorageClass controllers | 绑定 claim、动态供给工作流、回收策略编排 | 保证后端 API/权限/磁盘永不故障 |
| CSI external-provisioner/attacher/snapshotter 等 | 观察 Kubernetes 对象并调用 CSI controller RPC | 代替驱动/后端实现存储；提供跨版本兼容保证 |
| CSI controller plugin | Create/DeleteVolume、ControllerPublish/Unpublish、Create/DeleteSnapshot 等（按 capability） | 容器内文件系统语义或应用一致性 |
| CSI node plugin | NodeStage/Publish/Unpublish/Unstage，节点设备/挂载工作（按 capability） | 多节点同时写的文件一致性语义 |
| Rook operator（Ceph 场景） | Kubernetes 上编排 Ceph daemon/CR/升级等控制工作 | Ceph 数据面、应用备份或通用 CSI 替代品 |
| Ceph CSI | 把 PVC/PV/snapshot 请求映射到 Ceph RBD/CephFS 资源和 mount/attach 操作 | MON/OSD 故障域、复制、PG 恢复和容量设计 |
| Ceph | RADOS、复制/EC、MON quorum、OSD/PG/CRUSH 等存储与控制机制 | Kubernetes 对应用生命周期/权限的完整决策 |

**控制面和数据面不同。** `kubectl apply` 成功只表示 API 对象被接受；PVC `Bound` 表示绑定关系完成，不一定说明 Pod 已调度、节点 attach/mount 成功、容器有权限写入或应用数据恢复。要沿事件、controller、CSI sidecar/driver 日志、节点 mount 状态和后端状态逐层确认。

## 2. 卷创建、挂载和使用边界

典型 RBD block-mode 文件系统卷路径（实际调用取决于驱动版本、volume mode、attach capability）：

1. 应用提交 PVC，引用 StorageClass、容量、access mode 和 `volumeMode`。StorageClass 参数选择 backend pool、image features、filesystem 等实现细节；它不是所有集群通用配置。
2. `external-provisioner` 调 CSI `CreateVolume`，Ceph CSI 在 RBD/CephFS 后端创建资源并返回 volume handle；PV 保存绑定与生命周期属性。
3. scheduler 决定 Pod 节点。若驱动声明需要 attach，external-attacher 调用 `ControllerPublishVolume`，建立后端映射/访问控制；并不是数据复制或“卷已可写”的证明。
4. kubelet 调用节点 CSI 插件 `NodeStageVolume`（可选）准备块设备并做必要的全局 mount，再调用 `NodePublishVolume` 将卷 bind mount 到 Pod target path；访问模式和节点约束在此生效。
5. 应用在容器路径读写。成功写入的持久性、同步点、fsync/flush、数据库恢复仍由 filesystem/block stack/后端语义决定；CSI 成功返回不自动意味着应用事务或设备掉电安全。
6. 删除 Pod/PVC 会触发反向 unpublish、unstage、unpublish controller、删除 PV/后端资源等动作，受 reclaim policy、finalizer、driver 和 CSI capability 影响。`Retain` 与 `Delete` 的数据生命周期不同；删除对象前要确认后端对象和保留要求。

**症状分层：**PVC `Pending` 可来自 StorageClass、provisioner 未运行、配额/容量、权限或调度拓扑；Pod `ContainerCreating` / `FailedMount` 可来自 attach、node plugin、设备映射、filesystem、权限、路径冲突；应用 I/O error 还可能在成功挂载后由网络/OSD/池状态导致。不要先 `rm -rf` mount path、强制删除 PV 或重新格式化卷。

## 3. 快照与恢复 PVC 的真实边界

CSI snapshot 使用 Kubernetes VolumeSnapshot API（外置 snapshot controller + CSI snapshotter sidecar，和支持 snapshot 的驱动）。常见对象关系：

```text
PVC → PV / CSI volume handle
                  │ CreateSnapshot
VolumeSnapshot → VolumeSnapshotContent → driver/backend snapshot
                  │ dataSource (snapshot)
                  └── 新 PVC → 新 PV/volume → 新 Pod 挂载
```

- `VolumeSnapshot` 是 namespaced 的用户请求/状态；`VolumeSnapshotContent` 描述集群范围的后端快照与源 volume handle；`snapshotClass`/`deletionPolicy` 影响 provisioner、删除策略及后端生命周期。要核对 `readyToUse`、`restoreSize`、错误字段和 CSI capability，而不是只看对象创建成功。
- 应用一致性需另外安排：静默写入、数据库 checkpoint/backup API、文件系统 freeze/unfreeze 或 crash-consistent 方案。在线 snapshot 通常不能假定多卷/数据库事务原子性。恢复后执行数据库恢复、校验 checksum/schema/业务不变量。
- 从 snapshot 创建新的 PVC 会请求驱动创建/克隆后端卷或按实现恢复数据；CSI 规范本身不承诺所有驱动均为瞬时复制、同一延迟/空间成本，也不保证跨集群可移植。容量、卷拓扑、access mode、加密/密钥和驱动支持需核对。
- 快照往往仍占源池空间，并依赖同一 Ceph 集群/管理员凭据/故障域；误删、勒索、池损坏或区域故障可能连原卷和 snapshot 一起影响。备份应具有独立故障域、保留/不可变策略、凭据隔离和定期恢复验证。
- `VolumeSnapshot.readyToUse` 只说明控制面/驱动报告的快照状态，不证明业务一致、数据可解密、目标容量足够、恢复完成、应用可启动或 RPO/RTO 达标。

Lab 13 只用模拟输出解释状态；不创建 Kubernetes PVC、卷或 snapshot，也不宣称 CSI 集成通过。

## 4. Ceph 运维案例：容量升高且客户端尾延迟恶化

**事件简报：**一个使用 RBD PVC 的对象服务在写流量高峰后出现 p99 上升；Ceph 集群容量从 72% 升到 91%，同时报告少量 OSD `down` 和 PG `degraded`。应用有重试，部分写入超时。不能把“容量 91%”直接当根因：down OSD 会触发 peering/recovery，重建争用网络/磁盘，热点 PG、队列积压或副本不满足写确认也会拉高尾延迟；多个因素可能互相放大。

### 先采证据，后做状态变更

1. **用户/应用层：**按 API/卷/租户观察成功率、timeout、重试、p50/p99、未完成请求和队列；确认是否只有某个 Pod/节点或某类操作受影响。用 trace 关联一次慢调用，但不把采样 trace 当完整审计。
2. **Kubernetes/CSI：**查看 Pod/节点事件、PVC/PV、VolumeAttachment（若适用）、CSI controller/node plugin health 与日志、node mount、volume mode 和近期变更；确认错误发生在 provisioning、attach、mount 还是已挂载数据面。比较失败节点和健康节点。
3. **Ceph 控制面：**按固定版本查看只读 health detail、MON quorum、OSD up/in、PG states、pool `size/min_size`、容量 nearfull/backfillfull/full 阈值、CRUSH failure domain 和恢复/backfill 进度。指标取 dashboard/Prometheus/管理命令真实含义，不能把合成 Lab 值套入生产。
4. **资源/路径：**检查相关 OSD/主机的磁盘延迟与队列、CPU、网络丢包/饱和、慢请求、热点 pool/PG、RBD 客户端内核/CSI 版本。容量以 pool raw/replicated 使用和剩余可写空间解释，不只看 PVC 请求量或某个节点百分比。
5. **安全缓解：**遵照生产 runbook，先评估写保护、限流、停止非必要后台负载、增加安全容量/恢复健康故障域的计划和影响范围；变更需要审批、回滚和数据保护。未经核对不设置 `min_size`、不强行标记 OSD、`purge`、`repair`、`zap` 或清理用户数据。修复可能引发数据迁移/复制和额外负载。
6. **复盘：**恢复之后确认 degraded/recovery 完成、客户端错误回落、数据校验/应用一致性、备份恢复点和恢复演练记录；写出触发器、响应时间、误报/盲区和容量/故障域改进。

### 常见误读

- **`OSD down` 不等于数据已丢失，`PG degraded` 也不等于恢复完成。**要看副本数、acting set、`min_size`、PG 状态和故障域。
- **增加副本或降低 `min_size` 不是无成本“修复”。**数据安全与可写性权衡依配置、故障范围和版本，应由值班流程评估。
- **自动重启 CSI Pod 不会修复 pool 空间、MON 多数派或 OSD 后端问题。**先定位层次，避免制造挂载/重试风暴。
- **单节点 Ceph status healthy 不表示 HA。**一个宿主机的多个容器/VM/OSD 共用电源、内核、控制器和网络故障域；所有 VM 都在一台物理主机也一样。
- **snapshot 不替代外部备份。**Ceph 集群内的 clone/snapshot 可能共享池和管理面；全群失陷时它们共同消失。

## 5. Lab 13 与可选集群

必做使用[Lab 13](../../labs/lab-13-local-observability/README.md)：真实 loopback 产生本机结构化日志，合成指标/trace/容量/备份恢复状态触发延迟、容量、RPO/RTO、错误与指标缺口诊断。根据日志、metrics 和 health 三种证据给出“已知/未知”。本机脚本不连接集群，不运行 `kubectl`/`ceph`，不读取任何节点日志。

Kubernetes/Rook/Ceph 选修版本候选固定在[版本矩阵](../../课程大纲/版本与实验环境.md)：Kubernetes v1.36.5、containerd v2.3.6、Flannel v0.28.9、Rook v1.21.0、Ceph CSI v3.18.1、external-snapshotter v8.6.0、Ceph v20.2.4；宿主使用隔离 Ubuntu 22.04.5 VM。Rook 镜像、CSI sidecar、CRD/controller 和 snapshotter 必须成套核验，并保存 digest。镜像清单/tag 不是集成验证证据；候选栈尚未验证时应停止集群练习或退回概念图/本地模拟。OSD 只可使用 VM 专用可丢弃虚拟盘。**单节点只验证部署/PVC/接口/snapshot 软件流，不证明 HA**；共享物理宿主的多 VM 也无法验证电源/机架/控制器等独立故障域。

## 6. 课堂任务与检查题

1. 画出 PVC provisioning、调度、attach（若适用）、node stage/publish、mount 和容器可见路径；每一步列出 owner、对象/日志证据和可能的失败。
2. 某 PVC `Bound` 但 Pod `FailedMount`；列出最多 5 个只读检查项，解释为什么不能立即删除 PVC/PV。
3. VolumeSnapshot 显示 ready，但 restore 后数据库启动校验失败。列出 snapshot 之外的四类可能原因和一条安全验证路径。
4. 用 Ceph 案例写一个故障树，至少包含容量、OSD/PG 恢复、网络和应用重试；给出每个假设的可证伪观测。
5. 比较 Kubernetes event、CSI node log、应用 log、Ceph health 和 trace 的时间戳/采样/丢失限制。
6. 为什么一个三节点 K8s 集群若节点都在同一宿主机上仍不能声称硬件 HA？需要哪些故障域和验收证据？

## 参考资料

- Kubernetes：[Storage Classes](https://kubernetes.io/docs/concepts/storage/storage-classes/)、[Persistent Volumes](https://kubernetes.io/docs/concepts/storage/persistent-volumes/)、[Volume Snapshots](https://kubernetes.io/docs/concepts/storage/volume-snapshots/)。
- CSI：[规范与架构](https://github.com/container-storage-interface/spec)、[Kubernetes CSI](https://kubernetes-csi.github.io/docs/)。具体 capabilities 以选定 driver 发布版本为准。
- Rook：[v1.21 Quickstart](https://rook.io/docs/rook/v1.21/Getting-Started/quickstart/)、[Ceph CSI integration](https://rook.io/docs/rook/v1.21/Storage-Configuration/Ceph-CSI/ceph-csi-drivers/)。课程候选未集成验证时只作阅读链接。
- Ceph：[Tentacle release/documentation](https://docs.ceph.com/en/tentacle/)、[health checks](https://docs.ceph.com/en/tentacle/rados/operations/health-checks/)、[Ceph 运维故障排查](https://docs.ceph.com/en/tentacle/rados/troubleshooting/)。命令和告警需按固定版本核对。
- CSI Snapshot：[external-snapshotter v8.6.0](https://github.com/kubernetes-csi/external-snapshotter/tree/v8.6.0)、[CSI spec v1.9.0 snapshot API](https://github.com/container-storage-interface/spec/blob/v1.9.0/spec.md)。
- 课程内：[版本与实验环境](../../课程大纲/版本与实验环境.md)、[M09 故障模型/Ceph](../M09-分布式存储与Ceph/README.md)、[Lab 13 本地演示](../../labs/lab-13-local-observability/README.md)。
