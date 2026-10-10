# M09-L01：故障模型、故障域与 Ceph 放置基础

> 状态：课程正文　｜　建议课时：100–115 分钟　｜　案例版本：Ceph Tentacle（v20.2.4 候选阅读基线；非已部署验证环境）　｜　先修：[M07 持久性与崩溃一致性](../M07-持久性与崩溃一致性/README.md)、[M08 对象存储服务](../M08-对象存储服务/README.md)

## 学习目标

学完后你应能：

1. 描述故障模型：宕机/重启、慢节点、丢包/分区、磁盘故障和相关故障；区分检测到“不可达”与证明“已永久损坏”。
2. 解释 failure domain 如何把副本放置到不同设备、主机、机架/电源域，并指出逻辑拓扑只有在对应物理故障独立时才有意义。
3. 用静态三 MON 示例计算 quorum，多数派容忍故障数，并解释为什么它属于控制面，不是每次对象写入的多数投票。
4. 画出 RADOS 的 Pool/Object → PG → CRUSH → OSD 路径，区分 OSD/MON/MGR/MDS 和 PG/OSD 的职责。
5. 说明副本与纠删码的空间/容错取舍，避免把 Ceph 的 MON 说成 Raft，或把“一个副本还活着”当作所有 Ceph 操作都可用。

## 教学节奏

| 时间 | 内容 |
|---|---|
| 0–15 分钟 | 分布式故障模型与检测不确定性 |
| 15–35 分钟 | 故障域、共因故障、复制与纠删码 |
| 35–55 分钟 | MON quorum 与 OSD 数据可用性的区别 |
| 55–85 分钟 | RADOS、OSD、MON、PG、CRUSH、数据放置 |
| 85–105 分钟 | 例子、误区、放置边界与检查题 |

## 1. 从单机故障到分布式故障模型

单机 M07 的崩溃一致性会问：“进程/内核/设备在某一步停下，哪些写入可见？”分布式系统还须处理不同节点之间的**部分失败**：一个组件可能不可达，其他组件却继续运行；网络延迟与丢包使“故障”和“很慢”难以即时区分。

| 故障类别 | 观察到的现象 | 设计含义 |
|---|---|---|
| Crash-stop | 节点停机且不再恢复 | 数据副本/仲裁必须在可容忍节点数内 |
| Crash-recovery | 节点停机后带着本地盘恢复 | 必须重新加入、核对 epoch/历史并 peering；旧盘不能未经验证直接当最新副本 |
| Omission / 网络分区 | 心跳、请求或响应丢失；双方视角不一致 | 超时用于怀疑，不证明另一端已死；分区两边不能都无条件接受冲突写 |
| Slow / overload | 节点仍活着，但排队或响应超时 | 故障检测可能误判，恢复流量又可能加剧拥塞 |
| 磁盘损坏/静默错误 | 设备离线、读错或校验失败 | 需要冗余、校验、故障隔离和从正确副本修复 |
| 相关/共因故障 | 同机架断电、同交换机故障、误操作、勒索软件 | “三份数据”若共享同一故障域，不等于三份独立保护 |

本课程不假设拜占庭节点会任意恶意造假。Ceph 的典型运维故障模型是故障、超时、分区、容量和恢复异常，不是通用拜占庭容错协议。**超时是检测策略，不是数学证明。**选取更短 timeout 会更快发现故障，但会在拥塞时增加误判和反复恢复风险。

## 2. Failure domain：复制必须跨越真实的共因边界

```text
Region / 电源域
├── Rack A（机架级故障域）       Rack B                  Rack C
│   ├── Host A1                  ├── Host B1              ├── Host C1
│   │   ├── OSD A1.1             │   └── OSD B1.1         │   └── OSD C1.1
│   │   └── OSD A1.2             └── Host B2              └── Host C2
│   └── Host A2                      └── OSD B2.1              └── OSD C2.1
└── 交换机/电源/机房边界须按实际物理拓扑核对
```

一个 pool 的 CRUSH rule 可指定尽可能按 `host`、`rack` 等层级分散副本；“failure domain = host”只表示规则选择时避免把同一 PG 的副本落在同一 host，不会自动让 host 有独立供电、独立网络、独立地域或独立管理员。若所有节点都依赖同一个交换机/电源，顶层仍可能是共同失效点。

**三副本例子：**假设 3 个副本按 host 放置，但三个 OSD 实际运行在同一物理机上：丢这台机仍丢三副本。改为 host 域，分散在三台机器可抵抗一台主机故障；若三台 VM 共享一个物理宿主机，则只能演练软件节点故障，不代表抵御物理主机或机架故障。

故障域层级需要匹配业务目标：

```text
device → host → rack → room → power/network zone → region
```

故障域越大，隔离能力更强，但跨域写入的网络延迟、成本和可用容量约束也越大。不是“越多层越好”，而是将 SLO、RPO/RTO、网络和成本明确写出来。

## 3. 多数派与 Ceph MON：掌握集群地图，不代替数据副本

Monitor（MON）维护集群权威 map/状态（如 MON/OSD/PG/CRUSH 等 maps）及其版本，通过 MON 集群的一致性机制发布状态变更。固定成员数为 `N` 的简单多数派 quorum 是 `floor(N/2)+1`：3 个 MON 需要 2 个；可继续提交 MON map 更新并容忍 1 个 MON 不可用。1+1 网络分区两侧各自都不能形成 2/3 quorum。生产部署的 MON 应分布在互相独立的故障域；单机运行三个 MON 不能抵御该主机/电源故障。

```text
MON A (1 vote)   MON B (1 vote)   MON C (1 vote)
       N=3，quorum 至少 2/3；达成 quorum 后协调/提交控制状态

分区示意：    [MON A]       |       [MON B, MON C]
              1/3 无 quorum | 2/3 有 quorum

MON 发布集群 map/epoch；不是每个 payload I/O 的转发节点：
客户端/OSD ──数据 I/O──► Primary OSD ──► Replica OSD(s)
```

Ceph MON 使用**基于 Paxos 的一致性机制（Ceph 自己的 Paxos 变体/实现）**来协调 MON 状态和集群 maps；它不是 Raft，不能因为二者都用多数派就混称协议。MON quorum 的作用是让控制状态有一致权威来源，减少网络分区时两个“集群地图”同时被确认。

需要严格区分：

- **MON quorum**：MON 之间是否能提交/发布权威控制状态；比如某 OSD 状态变更和新 map。
- **PG/OSD 可用性**：某 PG 是否完成 peering、当前 acting set 是否可服务、池的 `size`/`min_size` 和 OSD/网络状态如何。
- **客户端对象写入**：客户端依据 map 找到 PG/primary，OSD 按 PG 状态和 pool 配置协调数据写入。客户端不对每一个对象请求向 MON 取“多数票”；MON 也不是每次 payload IO 的代理。

因此，MON 有 quorum 不代表每个 PG 都健康；MON 暂时无 quorum 也不能简单等于“所有已存在数据字节立即消失”。但控制状态无法正常推进会限制管理和故障变化处理，具体客户端操作结果取决于已有 map、PG 状态、版本和请求类型。不能只凭一个 quorum 数字承诺数据面可用。

## 4. 副本、纠删码与可用性的边界

### Replicated pool

一个逻辑对象保存多个完整副本。`size=3` 粗略表示目标三个副本；原始容量开销约 3 倍（还未计元数据、预留空间、BlueStore/文件系统开销和不均衡）。优点是读取/恢复逻辑直观、单份副本可直接复制；代价是容量和写入/网络资源。副本必须按 CRUSH rule 跨目标 failure domain 放置，`min_size`、PG peering 等配置决定降级情况下的操作条件，具体值需按 release 与 workload 评估。

### Erasure-coded pool

把数据编码为 `k` 个数据分片和 `m` 个编码分片，通常总分片数 `k+m`，理论原始容量开销约 `(k+m)/k`；在编码参数和 placement rule 支持的前提下，最多可丢 `m` 个分片并重构数据。例：`k=4,m=2`，六个 shard 保存 4 份数据等价容量，名义开销 1.5 倍；最多可容忍 2 个 shard 丢失。这里的“分片丢失”要落在不同有效 failure domain，否则两份 shard 同机架仍会同时损失。

| 方案 | 直觉成本 | 恢复方式 | 适用取舍 |
|---|---|---|---|
| 3 副本 | 约 3 倍原始容量 | 从存活完整副本复制 | 写入与恢复直观、容量开销大 |
| `k+m` EC | 约 `(k+m)/k` 倍 | 读多个 shard 解码重建 | 容量高效，但编码/小写/恢复计算和网络路径更复杂 |

这只是编码层开销模型，不是实际容量承诺。池配置、条带宽度、对象大小、故障域、元数据池、更新模式与恢复策略均影响结果。RGW 的桶索引/元数据不应假设都与大对象数据使用同一 EC 池策略。

## 5. RADOS、OSD、MON、PG、CRUSH：逐层定位数据

RADOS 是 Ceph 分布式对象存储核心。简化映射如下：

```text
应用/协议客户端
  ├─ librados API
  ├─ RBD 客户端（块接口）
  ├─ CephFS 客户端（POSIX 文件接口）
  └─ RGW（S3/Swift 风格 HTTP API）
          │ 解析/映射到 RADOS 对象
          ▼
    Pool + Object 名称/namespace
          │ hash(object locator / id, pool 参数)
          ▼
    PG（Placement Group，逻辑归置/恢复组）
          │ CRUSH map + rule + OSD map
          ▼
  up set / acting set：Primary OSD + replica/shard OSDs
     ├── osd.12（primary） ─── osd.4
     └─────────────────────── osd.29
       OSD daemon 管数据、PG、副本协作、恢复与回填
```

1. **Pool**：逻辑策略边界；池配置关联副本或 EC、CRUSH rule、配额/应用用途等。不同接口可使用多个池/元数据池；池不是单独一块磁盘。
2. **Object**：RADOS 的逻辑命名对象。客户端 API 所见文件、块设备范围或 RGW object，不保证和一个 RADOS object 一一等同。大文件/块卷往往映射成多个底层对象；实现对象大小和布局须按版本/接口确定。
3. **PG**：对象被映射到 PG；PG 是一组对象的逻辑管理/复制/恢复上下文，位于 pool 与 OSD 放置之间。PG 不是物理目录、不是一块磁盘，也不是“每个对象一个 PG”。
4. **CRUSH**：根据 CRUSH map 的设备拓扑、权重、rule 和 failure-domain 选择 OSD 集合。增删 OSD 或权重/规则变化会改变一部分 PG 的目标集合，触发再平衡/迁移；它不靠一个中心服务逐对象维护固定物理地址表。
5. **OSD daemon**：把设备、BlueStore/日志/校验、PG 状态、复制或 EC、peering 与恢复结合起来。Primary 是该 PG 当前协调客户端请求的 OSD；Replica/Shard OSD 参加 PG 的数据复制/编码集合。
6. **MON**：提供集群 map/quorum；不直接存储对象 payload。**MGR**提供管理/指标模块等，不替代 MON 或数据 OSD。**MDS**仅为 CephFS namespace/元数据路径服务，不是 RBD/RGW 的通用元数据服务器。

### CRUSH 放置细节

```text
object locator + pool → PGID → CRUSH(PGID, rule, topology, weights)
                                   │
                           选择不重叠故障域
                                   ▼
                        [osd.12, osd.4, osd.29]
```

CRUSH 是可扩展的伪随机放置算法/规则系统，不应简化成“Key 直接 hash 得物理 offset”。对象映射到逻辑 PG，再由当前 CRUSH/OSD map 算出 OSD 集合。正常请求使用客户端和 OSD 所持有的 cluster maps；map 变化会更新放置视图。

PG 有 `up`（依据当前期望映射的 OSD 集合）和 `acting`（当前参与服务/恢复的集合）等状态概念，两者在恢复或故障时可能不一致。**“期望放在哪”不等于“当前已有足够完整、可服务的副本”。**实际 `active+clean/degraded/undersized` 等状态须阅读指定 Ceph release 的状态说明。

## 6. Worked example：机架故障与 MON 网络分区

### 案例 A：三副本、四机架

- pool 期望 `size=3`，CRUSH rule 选择 `rack` 为 failure domain；一个对象的三个副本位于 A/B/C rack，D rack 未承载此对象副本。
- A 机架断电：该对象仍有 B/C 两份副本，但处于降级；此时仍可读不代表所有写请求一定可接受。
- 若有空余容量，系统可能把丢失的那一份重建到 D；重建流量与前台 IO 共享设备和网络。
- 如果刚好另一个机架/链路又失效，降级 PG 可能继续失去副本；这就是重建时间窗口需要被管理的原因。
- 若只有 A/B/C 三个故障域且三个域都在各 PG 里各放一份，A 故障后没有 spare failure domain，就无法在不违反分域规则的情况下恢复到三份；扩容不是只“加一个大容量值”，CRUSH 需要重新计算并搬迁数据。

这里的三副本只描述期望放置，不自动回答应用读写是否成功。要结合 PG peering、pool `min_size`、故障检测/OSD map 和客户端错误重试，查看目标 release 的实际状态。

### 案例 B：三个 MON 的分区

```text
分区前：         分区后：
A ─ B ─ C        [A]   |   [B ─ C]
 quorum=3/3       1/3       2/3 quorum
```

右侧两 MON 形成多数派，左侧单节点不能独自确认新控制状态。该多数派不意味着右侧对每个 PG 都有数据副本；同样，客户端是否能读取某个已映射 PG 还取决于数据 OSD 与 PG 状态。先问清“谁的 quorum？哪类请求？哪个 failure domain？”再谈系统整体是否可用。

## 7. 常见简化与部署边界

| 常见简化或做法 | 需要核对的边界 |
|---|---|
| “Ceph 强一致”“三副本写完即强一致”作为无条件承诺 | 具体操作与读写语义受 PG/acting set、配置、客户端接口和 release 约束；课程简图不代表完整一致性模型。 |
| 所有文件都固定切成 2/4 MB object，PG 数永不改变 | RADOS object 尺寸/切分按接口和配置；PG 数量及 autoscaler 随版本与规模变化。 |
| PG 是数据库索引，迁移一定是一个 PG 整体搬迁 | PG 是逻辑放置和恢复管理单元；迁移、recovery、backfill 的细节依赖实现、版本和对象布局。 |
| 一律三副本、MON 奇数且至少三个 | 多数派需要有效 MON quorum；生产拓扑应跨故障域设计。复制/EC 和最小副本阈值要按服务目标评估。 |
| 固定 PG 数或容量水位适用于所有集群 | 不使用通用固定值代替目标版本文档、容量/负载指标和变更窗口评估。 |

Ceph 部署选修必须使用目标 release 的 cephadm/Rook 文档，在隔离 VM 中配备专用可丢弃虚拟盘。不得对宿主机系统盘、真实数据目录或含用户数据的磁盘执行初始化和清理操作。单机 profile 只用于学习，不证明高可用。

## 8. 检查题

1. 三个 OSD 在同一台主机，副本按 `osd` 或按 `host` 作为 CRUSH failure domain，主机级故障保护为何不同？
2. 3 MON 里一个 MON down 与 3 副本里一个 OSD down 是不是同一类“少一票”？分别影响什么？
3. 一个对象有两份存活完整副本，为什么仍不能仅此断言对应 PG 一定可以写？至少列出两个还须核查的因素。
4. `k=6,m=3` 的 EC 理论容量开销是多少？“可失去任意 3 个 shard”在故障域放置上还要求什么？
5. 为何 CRUSH 不等价于“对象 Key hash 直接得到磁盘地址”？

## 常见误区与边界

- **“多数派 = 数据已多数复制。”** MON quorum 维护控制状态；OSD/PG 的数据副本状态是另一层。
- **“有一个存活副本就能写。”** 存活副本、PG peering、`min_size`、primary/acting set 和请求一致性共同决定操作结果。
- **“副本在不同 OSD 就是独立故障域。”** 同主机/机架/交换机/供电仍可能共因失效。
- **“PG 是实际目录/设备。”** PG 是逻辑放置和恢复单元。
- **“Ceph MON 用 Raft。”** 不正确；Ceph MON 采用 Paxos-based 机制/变体。M10 学的 Raft 是另一个协议模型。
- **“CEPH 的 Object 对应客户眼里的文件。”** API 对象、RBD extent、CephFS 文件和 RADOS object 的粒度/映射不必相同。
- 本课模型用于理解结构，不足以承诺某个生产配置的可用性、持久性、写入确认点或性能。

## 参考资料与版本复核

- **Ceph v20 / Tentacle（课程候选基线，2026-10-09）：**[架构](https://docs.ceph.com/en/tentacle/architecture/)、[高可用与扩展性/MON](https://docs.ceph.com/en/tentacle/architecture/scalability-high-availability/)、[CRUSH map](https://docs.ceph.com/en/tentacle/rados/operations/crush-map/)、[PG](https://docs.ceph.com/en/tentacle/rados/operations/placement-groups/)、[EC](https://docs.ceph.com/en/tentacle/rados/operations/erasure-code/)。版本来源：[Ceph v20.2.4 release](https://github.com/ceph/ceph/releases/tag/v20.2.4)。候选版本不是集群部署验收结果；行为需按实际锁定版本和配置复核。
- 下节：[M09-L02：RADOS 数据路径、恢复重建与接口职责](M09-L02-RADOS数据路径、恢复重建与接口职责.md)。
