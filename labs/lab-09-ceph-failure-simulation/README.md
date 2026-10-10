# Lab 09：故障域、副本与重建压力模拟

> **安全边界：**本实验只运行 Python 标准库程序和单元测试。不连接 Ceph、不要求集群或 root、不访问磁盘设备，也不会读取、删除或修改任何真实数据。真实 Ceph 部署为纯选做；如另行练习，只能使用隔离 VM 专用可丢虚拟盘，并遵守课程版本/安全说明。

## 目标

- 比较单个故障域失效前后的副本数和对象可读性。
- 观察有无 spare failure domain 对恢复重建的影响。
- 调整合成后台重建速率，比较恢复时间与前台容量余量。
- 通过测试理解“还可读”不等于“已恢复到目标副本数”，也不等于真实 Ceph 的写入可用性保证。

## 运行环境

Python 3.10+，无第三方依赖。进入本目录后运行：

```bash
python3 ceph_failure_sim.py \
  --objects 60 \
  --domains rack-a,rack-b,rack-c,rack-d \
  --replicas 3 \
  --failed-domain rack-a \
  --min-size 2 \
  --foreground-iops 75 \
  --rebuild-iops 10 \
  --capacity-iops 100 \
  --seed 19
```

JSON 输出中可关注：`degraded_objects`、`missing_replica_copies`、`rebuildable_copies`、`unrebuildable_copies`、`nominal_rebuild_seconds`、`combined_utilization_pct` 和 `overload_iops`。在本例中 60 个对象仍至少有一份副本可读；其中 49 份对象副本需要补齐。重建速率为 10 个合成副本操作/秒时，名义耗时 4.9 秒，前台与后台合成负载合计 85% 容量。

加快重建以观察容量压力：

```bash
python3 ceph_failure_sim.py --objects 60 \
  --domains rack-a,rack-b,rack-c,rack-d --replicas 3 \
  --failed-domain rack-a --min-size 2 \
  --foreground-iops 75 --rebuild-iops 30 --capacity-iops 100 --seed 19
```

合计请求负载超过模型容量，`overload_iops` 会大于 0；这表达了排队/背压压力，不是 p99 延迟预测。将故障域改为只有三个、且副本数仍为三，可观察无 spare domain 时无法在其余域恢复到原副本数：

```bash
python3 ceph_failure_sim.py --objects 60 \
  --domains rack-a,rack-b,rack-c --replicas 3 \
  --failed-domain rack-a --min-size 2
```

测试：

```bash
python3 -m unittest -v test_ceph_failure_sim.py
```

## 模型边界

- 每个命名故障域只代表一个等容量 OSD；对象副本用确定性 SHA-256 排序分散到不同域。这不是 CRUSH，也没有模拟权重、层级 bucket、PG、OSD map epoch 或负载均衡算法。
- 读可用性简化为“至少有一个存活副本”；`min_size_objects` 仅统计满足用户输入阈值的对象数。真实 Ceph 是否可读写还受 PG peering、acting set、池配置、网络、对象状态和具体版本影响；Ceph 写入不能仅用多数派或该计数推导。
- 模拟复制，不模拟纠删码。一个重建工作单元是“一个副本拷贝”，不是字节、IOPS 的硬件实测或网络带宽。
- 总负载/容量比例是线性抽象，用于比较重建限速取舍。真实尾延迟还取决于设备、网络、队列、QoS、热点、请求大小、并发、缓存、恢复策略及运行时状态。
- 无需在实验后清理；没有创建文件或资源。

## 思考题

1. 为什么三副本各放一个机架，通常比同机架的三个 OSD 更能承受机架故障？哪些共享电源/网络会让名义故障域失效？
2. 若只剩一个副本且仍可读，为什么不能据此断定该 PG 可以安全接受写入？
3. 为什么提高重建带宽会缩短降级窗口，却可能恶化业务尾延迟？在恢复时限和业务 SLO 之间如何选择限速？
4. 为何这个模型中的“多数”不等于 MON quorum，也不能直接替代 Ceph PG 的 `min_size` 和 peering 状态？
