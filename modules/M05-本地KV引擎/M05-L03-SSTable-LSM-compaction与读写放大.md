# M05-L03：SSTable、LSM、compaction 与读写放大

> 状态：课程正文　｜　建议课时：120–135 分钟（含文件观察实验）　｜　先修：[M05-L01](M05-L01-KV-API与本地持久化数据路径.md)、[M05-L02](M05-L02-WAL记录格式与崩溃恢复.md)；理解排序、WAL 与 `fdatasync` 边界。

## 学习目标

学完后你应能：

1. 解释 MemTable/WAL/SSTable 的写入、读取、flush 和 compaction 的职责，比较追加写与有序不可变文件。
2. 区分“单个排序 SSTable 快照”与完整多层 LSM 引擎；准确描述 MiniStore 当前读路径和未实现能力。
3. 解释 tombstone 为什么必须保留到可以安全清理旧值的阶段，以及 compaction 的版本选择/原子发布问题。
4. 使用可复现操作观察 WAL 累积、快照生成、墓碑清理与日志轮换，按指定口径讨论读/写/空间放大。
5. 比较 tiered/universal 与 leveled 等压实策略在读放大、写放大、空间放大、延迟之间的取舍，不把某一种策略写成 LSM 的唯一定义。

## 教学节奏

| 时间 | 内容 |
|---|---|
| 0–15 分钟 | WAL 越来越长，为什么需要排序的不可变文件 |
| 15–40 分钟 | MemTable、SSTable 与 LSM 的读写路径 |
| 40–65 分钟 | 删除墓碑、版本合并和 compaction |
| 65–85 分钟 | 放大指标与 leveled/tiered 取舍 |
| 85–110 分钟 | MiniStore 文件实验与结果分析 |
| 110–135 分钟 | RocksDB 版本边界、扩展实验与复盘 |

## 场景：日志追加解决恢复，不解决长期查找

WAL 适合顺序追加和崩溃重放，但同一 key 的历史版本会不断堆积。若启动后只能顺序读完整 WAL，数据量变大时恢复成本也会变大；若每次 `get` 都线性查历史日志，还必须找出最新版本并处理删除。把一批内存数据按 key 排序，写成不可变 SSTable（Sorted String Table），可以用 key 范围/索引快速定位，并在后台将多个 sorted run 合并、去重和清理过时版本。LSM-tree 是围绕“内存更新 + 磁盘 sorted runs + merge”组织数据的存储架构，不是仅靠文件扩展名或一棵普通树定义的结构。

## 1. 经典 LSM 的读写路径

```text
写：API → WAL 追加并按配置同步 → mutable MemTable
                                  │ 写满/达到阈值
                                  ▼
                          immutable MemTable
                                  │ flush
                                  ▼
                         L0 SSTable(s)
                             │ compaction
                    L1 → L2 → ... → Ln

读：MemTable → immutable memtables → 候选 L0 runs（新到旧）
      → 每层候选文件/索引/Bloom filter → SST data block → value
```

- **MemTable** 是可变内存索引；常见实现有跳表、平衡树或可配置结构。WAL 提供恢复日志，不能把“写进 MemTable”单独当作断电持久化。
- **Immutable MemTable** 是不再接收写入、等待刷盘的内存状态。新写进入新的 mutable MemTable，后台可继续 flush。
- **SSTable** 按 key 排序、生成后不原地修改；通常含 data blocks、索引、Bloom filter、footer 等。真实格式由引擎版本/表格式选项决定。
- **Compaction** 合并一个或多个 sorted runs，按 sequence/version 规则选择当前值、清理可安全丢弃的旧版本和 tombstone，并输出新的不可变文件。
- **Bloom filter** 对“不存在”可以给出确定性排除，对“可能存在”允许 false positive；它不保存 value，也不能判断 key 的最新版本。

不同 LSM 引擎的 flush、WAL 轮转、manifest、层级、并发压实和恢复协议并不相同。RocksDB 多个 column family 还会共享日志/管理状态；不能把简图直接当作具体代码调用时序。

## 2. MiniStore 的“最小 LSM 教学切片”

MiniStore 仅实现一个在内存中的有序 map 和**一个** SSTable：

```text
Store::open：读完整 TABLE.sst → 重放 WAL → 得到内存 map
写入：      WAL append + fdatasync → 更新 map
读取：      从已重建的内存 map 查找 / 遍历
compact：   当前 live map 排序编码到新 TABLE.sst → 原子发布 → 轮换 WAL
```

它的 `TABLE.sst` 是排序快照，`WAL` 是快照之后的日志；启动把两者完整读到内存。代码没有 L0 多文件、后台 flush、immutable memtable、磁盘索引、Bloom filter 或多层 leveled compaction。因此**当前 MiniStore 的一次 get 不是逐层查询磁盘 SST，也没有 RocksDB 的读路径**。这项限制是教学上的诚实切片：学生可以检查一套真实可运行的 WAL/格式/重启/原子发布代码，不需要把课程任务膨胀成完整 RocksDB。

实现文件：[`projects/MiniStore/src/ministore.cpp`](../../projects/MiniStore/src/ministore.cpp)。`load_table()/encode_table()` 使用 48-byte 显式 header 和 key 升序记录；`recover_wal()` 把 sequence 大于表 watermark 的记录应用到 map；`compact()` 只保存当前 live key 并清理删除墓碑。整表重写成本为 `O(N)` 数据量，输出最大受 256 MiB 上限约束。

### 删除不是立即从所有层物理抹除

在多层 LSM 中，旧值可能仍在较旧 SSTable。若写入新 tombstone 后立即丢弃墓碑，而更老的旧值还没合并删除，下一次读可能重新读出旧值（resurrection）。所以 tombstone 一般要留到相关旧版本已被覆盖/参与合并且安全删除条件满足。是否能清理还可能依赖快照、迭代器、时间戳、复制水位等引擎语义。

MiniStore 只有单一完整快照和一个 WAL，compaction 在独占进程锁下读取已经重放的最终 map；新表保存当前 live 值，不保存 tombstone，再安全轮换已包含的 WAL。若崩溃发生在新表发布后、旧 WAL 轮换前，表的 high-water sequence 使启动跳过旧 WAL。此条件来自本项目的简单文件代际协议，不能套用到多个 SST 文件且存在并发读者的引擎。

## 3. Compaction 的原子发布与资源代价

创建快照不是“写一个文件就结束”。MiniStore 的持久化顺序是：

1. 生成 `TABLE.sst.tmp`，写完整 header、记录与 checksum；
2. 对临时表 `fdatasync`；
3. `renameat` 为 `TABLE.sst`，再 `fsync` 数据库目录；
4. 生成并同步空 `WAL.tmp`；
5. 原子 rename 覆盖 `WAL`，再 `fsync` 目录。

故障窗口和恢复逻辑详见 [M05-L02](M05-L02-WAL记录格式与崩溃恢复.md)。旧表或新表若在 rename 前后出现，旧 WAL 都能与表 watermark 配合恢复。实际多层引擎需要管理更多状态：CURRENT/MANIFEST、SST 文件编号、正在运行的 compaction、并发迭代器/快照、旧文件何时可删除、空间不足和后台限速等。

Compaction 还要消耗读取/写入带宽、CPU、临时空间和 page cache，可能与前台请求竞争并影响尾延迟。后台不够快时，L0 文件积压可能增大读放大，触发写停顿或 stall。把 compaction 设置成“越积极越好”会增加重写量；完全不压实又会积累版本、空间和读取成本。

## 4. 读、写、空间放大：先约定测量边界

下列是常用的概念口径；必须声明测量范围和分子/分母：

```text
读放大 RA = 存储引擎/设备实际读取字节 / API 返回的逻辑字节
写放大 WA = 存储引擎/设备实际写入字节 / API 接收的逻辑写入字节
空间放大 SA = 当前存储占用字节 / 当前 live logical key+value 字节
```

工程里“设备写放大”“引擎文件写放大”“WAL bytes/API bytes”可能是不同指标。压缩、校验、元数据、删除、缓存命中、页缓存、文件系统 CoW、设备内部 FTL 都会改变实际分子；不能拿 `stat` 文件长度直接当 SSD NAND 写入量。

LSM 常见取舍：

- **读放大**：最新 key 可能要检查多个内存结构、多个重叠的 L0 runs 和若干层文件。区间索引、Bloom filter、BlockCache 可减少 I/O，但 false positive、缓存 miss 和范围查询仍有代价。
- **写放大**：最初 WAL/MemTable 写入后，数据 flush 到 SST，再在 compaction 中被一层或多层重写；按层大小比例和策略不同，累计写入可能远大于 API 输入。
- **空间放大**：重复版本、tombstone、日志、临时 compaction 输出和尚未回收的旧文件共同增加占用；压实通常能降低历史冗余，但需要暂存空间。

策略取舍示意（术语在各引擎/配置下应复核）：

| 策略倾向 | 组织 | 常见优点 | 常见代价 |
|---|---|---|---|
| Tiered / size-tiered / universal 风格 | 多个 sorted runs 可以长期共存，达到阈值后成批合并 | 新写入可较少反复重写旧数据 | 读时要考虑更多 runs，短期空间放大可能更高 |
| Leveled 风格 | 较低层维护更严格的不重叠 key range，容量通常逐层增大 | 点查找可更快排除文件/控制候选范围，层间整理较规整 | 重叠范围合并时会重复写较多旧数据，写放大可能较高 |

这不是固定的三选一“不可能兼得”定理。具体引擎能以压实优先级、目标文件大小、层数、写入限流、缓存、Bloom bits、数据压缩等参数改变曲线；负载、数据分布、快照和读写混合决定结果。RocksDB 不同版本/选项下也有不同 compaction style 和实现细节。

### MiniStore 中可观测的一个局部口径

在本项目里可以计算：

```text
WA_project ≈ (所有 WAL 文件字节追加量 + 每次 compaction 写出的 TABLE.sst 字节量)
             / API 逻辑输入量
```

分子包括本课程定义下 WAL/header/checksum/SSTable 编码字节；分母需明确按 put 的 key+value 或只按 value 计，delete 另计 key/操作数。此口径没包括文件系统 journal、块层合并、设备内部 FTL，也没处理重启时读取量、目录元数据和临时文件峰值。要可比较，必须固定 workload、初始状态、compaction 次数、value 长度、删除率和计数口径。MiniStore 没有内置 benchmark，不能把几次 `stat` 结果宣称为性能结论。

## 5. 实验：看 WAL 到排序快照的变化

```sh
make -C 'projects/MiniStore' clean test
cd 'projects/MiniStore'
TMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/ministore-l03.XXXXXX")
trap 'rm -rf -- "$TMP_ROOT"' EXIT HUP INT TERM
DB="$TMP_ROOT/db"
./build/ministore init --dir "$DB"
./build/ministore put --dir "$DB" alpha one
./build/ministore put --dir "$DB" beta keep
./build/ministore put --dir "$DB" alpha two
./build/ministore delete --dir "$DB" beta
./build/ministore list --dir "$DB"
stat -c '%n %s bytes' "$DB/WAL"
./build/ministore compact --dir "$DB"
stat -c '%n %s bytes' "$DB/WAL" "$DB/TABLE.sst"
./build/ministore list --dir "$DB"
./build/ministore put --dir "$DB" alpha three
./build/ministore get --dir "$DB" alpha
```

预期关键内容：

```text
list（compaction 前）：只有 alpha，value_hex=74776f
compacted seq=4 live_keys=1
WAL 轮换后：WAL 大小为 0；TABLE.sst 已创建且非空
list（compaction 后）：仍只有 alpha/two
后续 put 后 get：value_hex=7468726565
```

不同文件系统对小文件 `stat` 字节长度相同，但 block 分配 `du` 输出可有最小分配单位差异。compaction 前日志包含每次 put/delete 的历史记录；快照只保存 live `alpha=two`，删除的 `beta` 已不在快照，WAL 被轮换。再次 put 产生新 WAL record，重启后从表载入再应用新日志。

## 6. 实验讨论与扩展

1. 在隔离目录里对同一个 key 写 10 个不同长度值，记录每次 WAL 大小增量；解释固定 header+CRC 的占比怎样随 value 长度改变。
2. 对比插入 10 个不同 key 与反复覆盖 1 个 key，在 compact 前的逻辑 live bytes、WAL bytes 和 compaction 后 TABLE bytes；分别计算并注明分母。
3. 先写入 key 后 delete，再 compact；解释为什么 compact 后旧 WAL 清空时不需要保留此 tombstone；说明多 SSTable 场景缺少何种安全条件会导致数据复活。
4. 每次实验重复固定 workload、运行前用新的临时 DB；不要复用已有用户目录。可在自动测试中增强“发布新表、旧 WAL 尚存”故障窗口（已有黑盒测试），验证老日志不会回滚新快照。
5. 设计下一版：将多个 L0 文件按 sequence 保留，读时优先新版本，后台 merge 生成新 run；列出需要新增的文件 manifest、崩溃原子性、删除墓碑和并发读者约束。无需把项目直接扩展成真机 benchmark。

## 常见误区

- **“LSM 是全程顺序 IO，所以一定比 B+ 树快。”** WAL 追加趋向顺序，但读取、flush、compaction、文件系统和设备路径并不等于所有操作顺序；吞吐和 p99 依负载/资源竞争而定。
- **“L0 一定 key 重叠、L1+ 一定不重叠”是所有 LSM 的定义。** 这是某些 leveled 配置的常见组织特征，universal/tiered 或具体引擎状态可能不同。
- **“Bloom filter 查到可能存在就能返回 value。”** Bloom 只排除不可能存在；仍要查文件索引/数据并选择最新版本。
- **“Delete 会立刻删掉所有物理副本。”** 多层中旧值可能仍在下层，墓碑要参与足够范围的 merge 才能安全清理。
- **“compact() 是 RocksDB 的 compaction。”** MiniStore 一次把全量当前 live map 写成一个表，是单机、同步、整表 snapshot rewrite；无 levels、并行后台作业、版本 manifest 或多文件读取路径。
- **“文件大小差就是设备写放大/性能。”** `stat` 观察逻辑文件长度，不报告 NAND 编程量、设备 WA、cache 状态、flush 延迟或稳态吞吐。
- **“放大三项不可兼得，像 CAP 一样严格。”** 它们是测量指标与工程权衡，不是任何配置/任意负载下的普适数学不可能定理。

## 检查题

1. 多个 SSTable 里同一 key 有多个版本时，读路径如何判断最新值？Bloom filter 能否直接回答？
2. 为什么 L0 有多份重叠 run 时读放大可能比单一排序表高？索引和 Bloom filter 分别减少哪类成本？
3. 同样写入 1 GiB API 数据，为什么一个 workload 可能只产生少量 compaction 重写，而另一个产生更多 WA？还需要记录哪些参数？
4. 如果下层 SST 还有旧 value，为什么移除上层 delete tombstone 会令已删 key “复活”？
5. MiniStore `get` 为什么不是“查磁盘 SST 文件”？该限制对启动延迟、内存占用和教学结论有什么影响？
6. 说明一次 compaction 的文件写入、目录同步、旧 WAL 删除/轮换中，哪个步骤负责数据快照，哪个步骤负责 namespace/file-name 更新。

## 参考资料与版本边界

- RocksDB 文件布局、默认选项、各层容量和性能均须按具体 RocksDB 版本与配置核对，不能推广为所有 LSM。
- MiniStore 项目：[实现、格式、运行边界](../../projects/MiniStore/README.md)；项目测试包含 compaction 后重启、墓碑清理与旧 WAL 故障窗口。
- RocksDB 官方资料：[Compaction](https://github.com/facebook/rocksdb/wiki/Compaction)、[Leveled Compaction](https://github.com/facebook/rocksdb/wiki/Leveled-Compaction)、[Universal Compaction](https://github.com/facebook/rocksdb/wiki/Universal-Compaction)、[Bloom Filter](https://github.com/facebook/rocksdb/wiki/RocksDB-Bloom-Filter)。Wiki 内容随版本演进；正式报告应记录引擎版本、配置和文档访问日期。
- 经典背景：[O'Neil et al., The Log-Structured Merge-Tree (LSM-Tree), Acta Informatica, 1996](https://doi.org/10.1007/s002360050048)。论文中的设计思想不是某一当前 RocksDB 配置说明。
- API 同步/rename 语义：[Linux `fdatasync(2)`](https://man7.org/linux/man-pages/man2/fdatasync.2.html)、[`rename(2)`](https://man7.org/linux/man-pages/man2/rename.2.html)。MiniStore 演示本机文件系统上的排序快照，不访问裸设备、不声明生产级故障一致性。
