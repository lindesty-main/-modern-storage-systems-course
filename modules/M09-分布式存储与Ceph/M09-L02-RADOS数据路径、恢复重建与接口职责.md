# M09-L02：RADOS 数据路径、恢复重建与接口职责

> 状态：课程正文　｜　建议课时：105–120 分钟（含 Lab 09）　｜　案例版本：Ceph Tentacle（v20.2.4 候选阅读基线；未在本课程环境部署验证）　｜　先修：[M09-L01：故障模型与 Ceph 放置](M09-L01-故障模型、故障域与Ceph放置基础.md)

## 学习目标

1. 描述客户端如何用集群 map 将对象映射到 PG/OSD，并解释 MON 不在每一条 payload 数据路径上。
2. 画出 replicated pool 与 EC pool 的读/写/恢复大致路径，区别 Primary、replica/shard、up set、acting set 与 PG peering。
3. 区分故障检测、peering、recovery、backfill 和 rebalancing，说明这些后台工作如何影响前台 IO 和尾延迟。
4. 明确 RBD、CephFS、RGW 的协议和职责：RBD 暴露块语义；CephFS 提供文件 namespace/MDS；RGW 提供 HTTP 对象 API；三者借用 RADOS，但彼此不等价。
5. 运行并测试本地合成故障模拟器，解释其简化假设、重建限速与业务负载的取舍。

## 教学节奏

| 时间 | 内容 |
|---|---|
| 0–20 分钟 | 客户端 map、PG 与复制写入路径 |
| 20–40 分钟 | 读路径、PG peering、复制与纠删码 |
| 40–65 分钟 | down/out、recovery、backfill、再平衡和尾延迟 |
| 65–80 分钟 | RBD/CephFS/RGW 对外职责 |
| 80–110 分钟 | Lab 09 故障与重建压力模拟、误区和检查题 |

## 1. 客户端放置：查 map，不逐对象询问中心目录

客户端启动时通过配置/Monitor 等途径获得认证与集群 map；随后，具备 RADOS 客户端能力的接口按 pool 和对象标识计算 PG，再依据 OSD/CRUSH map 找出当前 placement。具体接口有不同封装与缓存行为，示意如下：

```text
             MON quorum
          （发布 cluster maps）
                  │ map / epoch
                  ▼
应用 → RBD/librados/CephFS client → RGW 前端（如 S3 请求）
                  │ pool + object locator
                  ▼
            PGID = map_to_pg(...)
                  │ CRUSH(PGID, rule, weights, topology)
                  ▼
       up set / acting set: [Primary, replica/shards]
                  │
                  ├──── OSD.12（Primary）
                  ├──── OSD.04（副本或 shard）
                  └──── OSD.29（副本或 shard）

OSD/PG 状态变化 → MON/OSD map 更新 → 客户端刷新 map → 新目标集合
```

MON 提供权威控制状态和 map，但在稳定的数据访问路径中，客户端与 OSD 直接通信；RGW 是一类前端服务，不能因此推导所有 RBD/CephFS 数据都必须绕过客户端走 RGW。Primary OSD 协调某 PG 的操作。发生 map 变化或请求异常时，客户端可能刷新 map、重试或收到错误，实际语义取决于 API、请求和具体 release。

PG 在池内组织一组对象，并作为 peering、复制/编码状态、恢复和统计等管理单元。一个 pool 有很多 PG；一个 PG 的放置集合含多个 OSD；一个 OSD 会承载很多 PG。PG 数、pool 数和每 OSD PG 数有资源开销，不存在适用于所有集群的固定“每 OSD PG 数”。新建/扩展池时使用目标版本的 PG autoscaler/最佳实践，并结合实际规模核对。

## 2. Replicated pool 的写路径与读路径

以下为概念时序，省略了认证、重试、日志/设备实现细节。数据写入在哪一阶段确认、是否等待某个副本以及故障状态允许什么操作，均受 PG 状态和配置（例如 `size`、`min_size`）、接口语义与 release 影响；请勿把简化图当成无条件的“所有副本落盘即强一致”承诺。

```text
客户端                   Primary OSD                Replica OSDs
  │                           │                            │
  │ 使用 map: pool/object→PG  │                            │
  │ CRUSH→acting set          │                            │
  │                           │                            │
  ├──写请求 + payload────────►│                            │
  │                           ├──副本写/复制───────────────►│
  │                           │◄──副本处理/确认────────────┤
  │                           │                            │
  │◄──客户端响应/结果─────────┤                            │
```

### 写路径关注点

1. **命名和映射：**接口把操作映射到 pool/object/PG；客户端依据 map 找到当前 acting primary。
2. **Primary 协调：**primary 检查 PG 状态、版本/顺序和权限边界，协调 replica/shard 操作。
3. **副本数据处理：**副本 OSD 执行相应操作、记录状态并回报。不同 pool 类型和 release 的数据流并不完全一致。
4. **确认与可见性：**客户端成功返回表达某个接口与配置语义下的成功，不自动等价于“永远不丢”或“所有可能故障后都可读”。持久化屏障、介质故障模型、客户端重试、PG 复制阈值与故障域都会影响保证。
5. **异常：**primary 故障、OSD map 更新或网络分区可能触发重试、peering、降级、阻塞/错误。应读错误码、PG 状态和目标版本文档，而不是只看 `ceph -s` 一行。

### 读路径

读请求按对象映射到 PG/OSD，由 primary/当前 PG 路径协调读取并返回；具体是否直接从副本读、何时进行修复/校验/版本检查取决于 Ceph 配置与实现。校验和、数据版本和异常副本处理是完整性的组成部分。不要写成“MON 每次查找对象，再把请求转发到 OSD”。

## 3. Replication 与 Erasure Coding 的数据路径差别

```text
Replicated pool（size=3，逻辑示意）
  Primary: full object ──► Replica 1: full object
                       └► Replica 2: full object
  容量开销约 3x；修复通常从存活完整副本读出数据再补副本

EC pool（k=4, m=2，逻辑示意）
  输入条带 ──编码──► [D0 D1 D2 D3 P0 P1]
                         │  分布到不同 failure domain
                         └─ 任意符合编码容错条件的 4/6 shard 可重构
  容量开销理论约 6/4=1.5x；修复需读多片并解码，再写回缺片
```

EC 的重建需要足够的存活 shard、正确的编码与故障域规则；“最多 `m` 个分片丢失”不能跨过同一 failure domain 同时毁掉多个 shard 的问题。读取/写入、小对象和部分更新的代价也不同。课程不提供统一的性能比例；在目标版本和真实工作负载测量前，不能说 EC 一定更慢/更快或一定更便宜。

常见设计是把大对象数据和元数据/索引放入不同池或使用不同策略；RGW/其他上层服务的 pool 配置要按产品文档与版本检查。`RADOS Object`、API object、EC stripe 和客户端文件不是同一层的同义词。

## 4. 故障发现、peering、recovery、backfill 与再平衡

```text
OSD / 网络 / 磁盘故障或超时
       │ heartbeat / 请求超时（怀疑，不等于证明）
       ▼
OSD/MON map 状态变化：某 OSD down；经运维/策略可能标记 out
       │
       ├── PG peering：参与 OSD 核对 epoch、日志和可接受历史
       │       └── 确认 acting set / PG 能否服务；必要时等待或报错
       │
       ├── recovery：目标 PG/对象/版本缺失，按历史补齐
       │
       └── backfill / rebalancing：放置规则或目标集合变化后，搬移/补齐数据
                    │
                    ▼
             补回副本/EC shard → PG 最终重新 clean（条件依赖版本/状态）
```

- **Down 不等于 Out：**down 表示当前被判断不可达/未运行；out 表示集群不再把它作为正常放置目标并可能重分布其数据。状态改变和等待时间按配置/版本处理，不能把“检测到心跳失败”当作已经完成恢复。
- **Peering：**PG 的 acting OSD 协商 PG 历史/epoch，确定可服务状态与有效历史。发生分区、OSD 重启或日志历史冲突时，不能随意选“磁盘上看起来有文件的那台”作为最新副本。
- **Recovery：**把可确定缺失/落后的对象版本复制或重构到目标 OSD，恢复 PG 内的数据完整状态。
- **Backfill：**目标 OSD 缺少较大范围 PG 内容时，从 peer 扫描和补数据。和按对象逐条的小范围恢复有区别，具体实现随版本演进。
- **Rebalance：**CRUSH topology、OSD 权重、池配置或集群容量目标变化导致放置集调整，一部分 PG/对象需要搬迁。新增 OSD 后并非所有对象全量重写，也不保证“绝无业务影响”。

恢复既是正确性机制，也是资源争用负载：重建要读源盘、写目标盘、占用网络、CPU、锁/队列和内存。单个慢节点可能成为副本写或重建的长尾；恢复拥塞导致响应变慢，超时又可能触发更多故障判断，形成反馈环。

## 5. 为什么后台重建会推高尾延迟

```text
单机/集群资源预算（示意）
┌───────────────────────────────────────────────────────────┐
│ 网络带宽 / 磁盘 IOPS / CPU / 队列 / 内存 / PG 锁与调度     │
├───────────────────────────────────────────────────────────┤
│ 前台读写：业务负载，关心 p95/p99 和 SLO                    │
│ 后台恢复：读取源副本/EC shard + 传输 + 写入目标             │
│ 仲裁/peering/OSD map：控制与状态流量                         │
└───────────────────────────────────────────────────────────┘
```

前台 IO 负载接近饱和时，恢复流量即使不改变总字节量，也会增加共享队列等待和竞争，尾部请求会最先超过 SLO。提高 recovery/backfill 吞吐可以缩短处于降级状态的窗口，但会占更多前台资源；过度限速保护前台，却延长第二次故障前没有恢复冗余的时间。正确做法是为目标 workload 设置并观察限速/QoS、设备/网络利用率和业务尾延迟，在业务和恢复窗口间权衡，而不是简单停掉恢复。

### 算一个容量例子（只做量级估算）

假设有 10 TiB 的缺失副本数据需要重建，源端到目标端有效速度持续为 100 MiB/s：

```text
10 × 1024 × 1024 MiB ÷ 100 MiB/s ≈ 104,858 s ≈ 29.1 小时
```

这还未计入协议、校验、并发限制、随机 IO、源/目标争用、多个 shard 重构、失败重试、慢盘、业务流量和容量不均衡。该数值不等于真实 Ceph 的预测。并行 OSD 可能提高有效带宽；共享链路和瓶颈又可能显著降低它。

### 限速取舍例

设一个合成资源模型的总处理能力为 100 单位/秒，前台需求 75：

| 重建计划速率 | 合计需求 | 简化结论 |
|---:|---:|---|
| 10/s | 85/100 = 85% | 名义重建较慢，保留 15 单位余量 |
| 30/s | 105/100 = 105% | 理想速率更快，但超出预算 5 单位，需排队/背压或牺牲前台 |

这是 Lab 09 的线性“压力指标”，不把 85% 映射成某个 p99 数字。实际尾延迟需要在真实、隔离、可复现的 workload 和目标版本上测量；本课程必做模拟不接触集群，也不做硬件基准。

## 6. RBD、CephFS、RGW：共享 RADOS，不共享同一套上层职责

```text
RBD client / kernel rbd ──块地址 + 长度──┐
                                         │
CephFS client ──目录/inode/POSIX 文件───┼──► librados / RADOS ─► PG/OSD
      │                                  │
      └── MDS：namespace/元数据管理       │
                                         │
S3/Swift 客户端 ──HTTP API──► RGW ───────┘
                           （对象 API、鉴权、bucket/key、HTTP 行为）
```

| 接口 | 对客户提供 | 关键组件职责 | 不应该误称为 |
|---|---|---|---|
| **RBD** | 虚拟块设备；客户读写 block offset/length，通常由 Linux kernel `rbd` 或 `librbd` 接入 | RBD 客户端把卷范围映射到 RADOS 对象/池；上层客户机可自行建文件系统 | POSIX 共享文件系统；多个主机随意以普通文件系统并发读写一个 RBD 卷是安全的 |
| **CephFS** | POSIX 风格共享文件系统，目录、inode、权限、文件操作 | MDS 管 namespace/文件元数据和客户端 capability；文件数据实际由客户端经 RADOS/OSD 路径访问，存放在 data pool | “所有文件数据都经过 MDS”；MDS 不是普通数据块/对象的通用存储引擎 |
| **RGW** | S3/Swift 风格 HTTP 对象 API；bucket、key、HTTP 请求/权限语义 | RGW 网关翻译协议、处理认证/请求并管理相应 RADOS 数据/元数据池；具体兼容范围依 release | CephFS 或 RBD；也不是所有 S3 API 都天然等价/完全兼容 |

三者都建立在 Ceph/RADOS 数据服务基础上，但接口语义、客户端、元数据处理和故障表现不同。RGW 的 S3 兼容性是产品/版本范围问题；M08 对象 API 的一致性边界仍适用。CephFS 的 MDS 与 RADOS OSD 也不能混为一个“metadata server”。

## 7. 实验：Lab 09 副本与恢复压力模拟

执行 [Lab 09 README](../../labs/lab-09-ceph-failure-simulation/README.md) 中的命令与测试：

```bash
cd labs/lab-09-ceph-failure-simulation
python3 -m unittest -v test_ceph_failure_sim.py
python3 ceph_failure_sim.py --objects 60 \
  --domains rack-a,rack-b,rack-c,rack-d --replicas 3 \
  --failed-domain rack-a --min-size 2 \
  --foreground-iops 75 --rebuild-iops 10 --capacity-iops 100 --seed 19
```

再把 `--rebuild-iops` 改为 `30` 比较合成负载是否超过容量；将域缩为三个、仍使用三副本，观察降级但没有 spare domain 的情形。测试验证：副本跨故障域、损坏份数、可读性、无 spare 的重建边界、没有任何存活源时不“凭空重建”，以及限速的名义恢复时间/超载差异。

### 模拟器不能证明什么

它使用确定性哈希排序代替 CRUSH，每个 domain 是等容量单 OSD，不含 PG/peering/EC、OSD map epoch、网络、热点、具体恢复算法、设备延迟或故障检测。`readable_objects`/`min_size_objects` 是简化计数，并非真实 Ceph 可用性承诺；utilization 也不是延迟预测。它没有连接到集群或读写任何数据。

## 8. 部署与版本边界

Ceph 命令、配置项、PG 行为和客户端兼容性会随 release 变化。按课程版本矩阵选择版本，并以对应 release 的 Ceph 官方文档核对部署方式、PG autoscaler、容量和恢复配置。真实部署是选修内容；必须在隔离环境中使用专用虚拟盘，并在运行前检查将被清空的目标。Lab 09 的本地故障模型不需要 Ceph 集群、root 或真实磁盘，也不证明生产可用性。

## 9. 检查题与作业

1. 请分画 replicated PG 的稳态写路径和 OSD 故障后的恢复路径；把 MON、client、primary、replica 的消息标清。MON 是否收到每次 payload？
2. Pool `size=3`、`min_size=2` 时失去一个副本，你能确认什么？还不能确认什么？为什么不能把它简化成“二票就成功”？
3. 同样的数据量，EC 的恢复为何可能消耗更多读/CPU/网络资源？故障域应如何配置，才能让 `m` 的设计容错成立？
4. RBD、CephFS、RGW 分别由哪类客户端访问？MDS、RGW、OSD、MON 各自做什么？
5. 用 Lab 09 把 `foreground_iops`、`capacity_iops`、`rebuild_iops` 改为自选值，提交 JSON 和 5–8 句解释：为何重建完成时间与前台压力相反；至少列出两个模型没有包含的因素。

## 常见误区与边界

- **把错误/超时当成数据已经丢失。** 先区分故障检测、down/out、PG peering 和恢复完成。
- **把恢复等同于负载均衡。** recovery、backfill、rebalancing 的触发和工作集合不同，但都会争用资源。
- **认为扩容“立即均匀加空间且不扰业务”。** 新 OSD 改变 CRUSH 目标，部分 PG 需要搬迁；容量/网络/回填限速都重要。
- **把三种接口都叫“同一个对象 API”。** RBD=块，CephFS=文件系统，RGW=对象 HTTP API；上层语义并不互换。
- **把数据面一致性说成多数派共识。** MON 的 Paxos-based 控制状态机制与 PG/OSD 复制/EC 不是同一算法层次；Ceph MON 不是 Raft。
- **把“可读”说成“可写、健康、持久性已保证”。** 读写阈值、PG 状态、请求类型和介质保证都需分别核实。
- **把模拟结果说成 Ceph 实测性能。** 本实验没有真实 Ceph、设备、网络或基准测试。

## 参考资料与版本复核

- **Ceph Tentacle / v20.2.4 候选阅读基线（核验日期：2026-10-09）：**[数据路径/架构](https://docs.ceph.com/en/tentacle/architecture/)、[高可用与扩展性](https://docs.ceph.com/en/tentacle/architecture/scalability-high-availability/)、[PG](https://docs.ceph.com/en/tentacle/rados/operations/placement-groups/)、[恢复与 OSD 状态](https://docs.ceph.com/en/tentacle/rados/operations/monitoring-osd-pg/)、[CRUSH](https://docs.ceph.com/en/tentacle/rados/operations/crush-map/)、[EC](https://docs.ceph.com/en/tentacle/rados/operations/erasure-code/)、[RBD](https://docs.ceph.com/en/tentacle/rbd/)、[CephFS](https://docs.ceph.com/en/tentacle/cephfs/)、[RGW](https://docs.ceph.com/en/tentacle/radosgw/)。[v20.2.4 release](https://github.com/ceph/ceph/releases/tag/v20.2.4)；上述阅读候选不表示课程已部署集成测试。
- [Lab 09：故障域、副本与重建压力模拟](../../labs/lab-09-ceph-failure-simulation/README.md)：Python 3 标准库本地模拟和 `unittest`。
- [M09-L01：故障模型、故障域与 Ceph 放置基础](M09-L01-故障模型、故障域与Ceph放置基础.md)。
