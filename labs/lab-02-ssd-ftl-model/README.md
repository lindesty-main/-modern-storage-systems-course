# Lab 02：SSD FTL、垃圾回收与队列模型

配套课文：[M01-L02：SSD 内部行为与块存储路径](../../modules/M01-设备与块存储/M01-L02-SSD内部行为与块存储路径.md)。本实验用 Python 标准库在内存中运行一个可重复的页映射 FTL 玩具模型，并计算本地/远程块请求的简化 FCFS 队列。它**不访问、打开或写入任何 `/dev`、loop 设备、磁盘映像、SSD、网络服务或云资源**；不需要 root、fio、第三方包或硬件。程序只读取 JSON 配置并向终端输出结果。

## 内容与限制

- `ssd_ftl_model.py`：页映射、顺序写入、旧页失效、贪心 GC、有效页搬迁、块擦除计数、FIFO/最低擦除次数优先的动态分配，以及简化队列模型。CLI 只接收普通配置文件，符号链接和非普通文件会在读取前拒绝。
- `model.example.json`：固定几何、热点/均匀/顺序工作负载、两种分配策略与本地/远程队列参数。
- `test_ssd_ftl_model.py`：标准库 `unittest` 自测；覆盖 GC 搬迁和写放大、非普通配置路径在打开前拒绝、配置边界、重复运行确定性、磨损计数对照、队列等待与网络 RTT 分离。

FTL 初始映射视为已存在，**不计入**运行期 host writes / NAND page programs / GC；每次测量写入都恰好覆盖一个逻辑页。模型只实现简化动态磨损均衡（分配空闲块时比较擦除次数），不实现静态磨损均衡、冷数据迁移、坏块/ECC、元数据写入、缓存、压缩、TRIM、断电一致性、多 die/channel 调度或具体 SSD 固件策略。队列实验是单一固定到达序列、确定服务时间及有限同质服务 lane 的抽象；不是 fio，也不拟合真实设备。

模型参数、趋势和输出**不能用于预测某款 SSD 的 WA、耐久度、IOPS、吞吐或时延**。若不同配置得出相反结论，先解释模型策略/输入的变化，而不是把某一组数字当成硬件规律。

## 运行与验收

在本目录运行：

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest -v test_ssd_ftl_model.py
PYTHONDONTWRITEBYTECODE=1 python3 -B ssd_ftl_model.py model.example.json
```

首条命令应显示 `Ran 6 tests` 与 `OK`。第二条命令只读示例 JSON 并打印 JSON 结果，其中包含 6 条 FTL 对照结果（3 个 trace × 2 种分配策略）及 `queue.local` / `queue.remote` 汇总。示例配置的均匀写 trace 中，`least_worn` 擦除次数 spread 不大于 `fifo`；合成队列中远程的 RTT 和较慢服务设置导致更高 p99 与排队等待。这只是模型参数的推论，不是普遍的本地/远程性能结论。精确数字随配置内容而变。

无效配置会打印 `error:` 并以状态码 2 退出。例如只改临时配置的 `logical_pages`，使几何不再留下两个初始空闲块，程序应拒绝输入；示例文件无需修改。运行过程不生成输出文件或测试数据。建议确认入口和数据边界：

```sh
rg -n 'open\(|/dev/|subprocess|socket|pwrite|write\(' ssd_ftl_model.py test_ssd_ftl_model.py
```

程序只通过 `Path.open("r")` 读取用户提供的 JSON；代码里没有设备访问、网络、shell 子进程或磁盘写入逻辑。该文本搜索是人工复核辅助，不是安全证明。

## 读懂 FTL 输出

以 `model.example.json` 的几何为例：4 KiB/page × 4 pages/block × 8 physical blocks，共 32 个物理页；20 个逻辑页已预置映射，另有 12 个初始空闲页（3 个整块）。按 `(32−20)/20` 定义，模型过量配置比率为 60%。这是简化教学几何，不是厂商的可用容量/OP 标注。

| 输出字段 | 含义 |
|---|---|
| `host_writes` / `logical_bytes` | 运行期主机逻辑页写入数和字节数 |
| `physical_programs` / `physical_programmed_bytes` | 运行期主机新写页 + GC 搬迁页；不含预置映射 |
| `gc_copied_pages` / `gc_runs` | GC 搬迁的仍有效页数 / 回收次数 |
| `write_amplification` | `physical_programs ÷ host_writes`；本实验所有页大小相同，字节比相同 |
| `block_erases` | 回收时擦除的块数，不等于写放大 |
| `erase_count_min/max/spread` | 全部物理块的擦除计数最小/最大/差值；未用冷块也计入 |
| `free_blocks` | 测量结束时仍空闲的整块数 |

比较前确认 workload 的 seed、写次数、地址局部性和几何完全相同。热点 trace 的局部性、均匀随机 trace 的有效页比例，以及是否把初始填充计入，会改变 GC 搬迁量；因此 WA 不是只由“顺序/随机”一个标签决定。

## 尝试与讨论

1. 将 `hot_pages` 从 4 增到 20，其他参数不变。比较两条 trace 的 GC 搬迁和 WA；用有效页比例解释结果。
2. 把 `allocation_policies` 仅保留 `fifo` 或仅保留 `least_worn`，确认同一 trace 的物理写数量与擦除分布可以分开讨论。
3. 将队列 `interarrival_us` 改为 250，或把 `remote.lanes` 增至 4，重新运行并观察 queue wait / p99。你改变了到达率还是服务能力？模型里 RTT 是否被当成后端占用时间？
4. 修改远程 RTT 为 0，观察排队仍在时的结果；解释“网络传播时延”与“目标端排队”不是一回事。
5. 说明该模型为什么不能从结果推导真实 SSD 的 TBW、最大 IOPS、NVMe 队列数，也不能用于证明云块盘 SLA。

## 清理

无需清理：命令不创建临时映像或外部资源。若自行重定向 JSON 输出，请仅写到自己指定的普通文件，并自行管理它；课程自动测试无需该步骤。
