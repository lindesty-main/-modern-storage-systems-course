# M12-L02：fio、RocksDB 与缓存策略对照

> 状态：已完成　必做内容不依赖 fio/RocksDB 安装；可选工具只对课程临时目录中的普通文件或新建临时数据库运行。禁止真实设备、云服务和用户数据目录。

## 学习目标

- 为 fio 比较固定同一文件、访问模式、请求尺寸、队列/线程、缓存状态与同步语义。
- 解释 fio 文件 IO 与 RocksDB `db_bench` 引擎操作处于不同层级，不能把结果直接排行。
- 把 MiniStore 的实现边界和缓存策略模拟分清：MiniStore 全库驻留内存，没有可调 block cache。
- 交付“配置 + 功能校验 + 每轮指标 + 环境 + 限制”的可审查报告，而非宣传数字。

## 一、基准比较先确保语义相同

`fio` 常用来生成文件/块 I/O 工作负载，指定 `ioengine`、`rw`、`bs`、`iodepth`、`numjobs`、`direct` 和同步策略。`db_bench` 通过 RocksDB API 执行 `fillrandom`、`readrandom` 等 KV 引擎场景，耗时还包括引擎索引、MemTable/SST、WAL、压缩、缓存和配置。两者的数据路径/操作定义不同：fio 的 4 KiB 文件随机读不能和 RocksDB 每秒 KV `Get` 做“谁更快”横向排行。

有效比较至少固定以下项：工具/引擎版本及编译选项、工作负载 seed/大小/读写比例、value/key 或 block 大小、线程/并发、队列深度、direct/buffered、同步语义、压缩/校验、缓存容量/预热、运行时间/请求量、重复次数及统计输出。报告 fio `IOPS` 与 RocksDB `ops/sec` 时必须解释单位操作；文件吞吐不是数据库事务吞吐。

## 二、可选 fio：只用新建临时普通文件

版本矩阵把 fio 3.36 列作候选依赖，但系统实际版本须由 `fio --version` 记录。下面的 shell 片段只在 `mktemp` 创建的目录中生成普通文件和 JSON 报告；不需要 root。`trap` 仅清理刚创建的 `$TMP`。请勿将路径替换为设备、已有数据目录或其他用户文件。

```sh
set -eu
TMP=$(mktemp -d "${TMPDIR:-/tmp}/m12-fio.XXXXXX")
trap 'rm -rf -- "$TMP"' EXIT HUP INT TERM

# 初始化并由 fio 写入/校验模式数据；目录内唯一数据目标是普通文件。
fio --name=prepare --filename="$TMP/data.bin" --size=16m \
  --rw=write --bs=128k --ioengine=psync --iodepth=1 --numjobs=1 \
  --direct=0 --end_fsync=1 --verify=crc32c --verify_fatal=1 \
  --group_reporting --output-format=json --output="$TMP/prepare.json"

# 一样的随机读负载、同一生成文件；可选对照的内核 io_uring 若被策略禁用，
# 记录错误并跳过该项，不提权绕过。两项都只是 buffered regular-file、QD=1。
fio --name=randread-psync --filename="$TMP/data.bin" --size=16m \
  --rw=randread --bs=4k --ioengine=psync --iodepth=1 --numjobs=1 \
  --direct=0 --randrepeat=1 --randseed=20261009 --group_reporting \
  --output-format=json --output="$TMP/psync.json"
fio --name=randread-uring --filename="$TMP/data.bin" --size=16m \
  --rw=randread --bs=4k --ioengine=io_uring --iodepth=1 --numjobs=1 \
  --direct=0 --randrepeat=1 --randseed=20261009 --group_reporting \
  --output-format=json --output="$TMP/uring.json"
```

先看 `fio --version` 和 `fio --cmdhelp=verify`/`fio --help`，因为选项和 JSON 字段須以已安装版本为准；如果发行版 fio 不接受 `--randseed`，不要静默删 seed 后继续宣称轨迹完全相同，应记录差异或使用固定 job file 复核。预处理 `--verify` 校验 fio 写入的数据；fio 输出里的 `clat`/`lat` percentile 是 fio 定义的 completion/total latency，不一定与应用从发起到处理完成的端到端延迟相同。将两个 job 的 json 中 p50/p95/p99、IOPS、带宽、错误及运行时间保留，不只截取终端摘要。

这个示例的 `direct=0` 明确走 buffered 普通文件路径，重复测试可能命中页缓存；`end_fsync` 只出现在数据初始化阶段，随机读没有写持久性承诺。`psync` 与 `io_uring` 的 QD 都是 1，不是高并发驱动比较，也不是设备上限测试。若需扫并发度，应让两种实现都确实提交同样数量的未完成 I/O，并再次检查语义/内核支持；不能只把 `iodepth` 配置写大，就假定同步引擎产生了并发。

> **禁止操作：**不使用 `/dev/nvme*`、`/dev/sd*`、分区、loop 设备或任何设备路径；不对既有文件目录用 fio 的写入 job。不运行 `sudo`，不绑定设备或清盘。

## 三、可选 RocksDB `db_bench`：固定新数据库与工作集

课程案例候选版本是 RocksDB v11.8.1，实际运行需记录 git tag/commit、编译器、编译选项和 `db_bench --help` 输出。先按版本文档确认该构建中 `--seed`、`--use_existing_db`、`--verify_checksum` 等参数的实际含义。以下示例把 DB 放入私有临时目录，且不连接服务：

```sh
set -eu
TMP=$(mktemp -d "${TMPDIR:-/tmp}/m12-rocksdb.XXXXXX")
trap 'rm -rf -- "$TMP"' EXIT HUP INT TERM

# 新建并填充；每轮对照必须清理此临时 DB 后重建相同初始状态。
db_bench --db="$TMP/db" --benchmarks=fillrandom \
  --num=100000 --reads=100000 --key_size=16 --value_size=256 \
  --threads=1 --seed=20261009 --use_existing_db=0 --statistics

# 同一 DB 上执行随机读；记录读负载之前是否已经被填充/预热。
db_bench --db="$TMP/db" --benchmarks=readrandom \
  --num=100000 --reads=100000 --key_size=16 --value_size=256 \
  --threads=1 --seed=20261009 --use_existing_db=1 --statistics
```

该流程演示“写入随机 KV，再读同一工作集”，第二阶段不是冷缓存；数据库默认选项、BlockBasedTable、block cache、Bloom filter、压缩、WAL/sync、compaction 和校验都影响结果，应从指定版本 `--help`/源码确认后记入报告。`db_bench` 自己每项的吞吐/计时边界、seed 用法和是否覆盖数据库也以所固定版本为准。完成后保留命令、完整标准输出、版本与数据校验方式；运行前先检查实际二进制确属 RocksDB db_bench，而不是同名其他程序。此实验是可选工具对照，不是 Lab 12 的依赖或已验证的性能数字。

可做单变量实验：不同 value size、读写比、工作集、线程、block cache size、compression、write buffer 或 compaction 配置；每个配置都用**全新且参数相同**的 DB，完成正确性校验后重复运行。参数改变可能连带改变 WAL/持久化和磁盘空间行为，报告不能把它们当成纯粹的“某种缓存更快”。避免同时改多个选项后把因果归给其中一个。

## 四、MiniStore 与缓存策略：不要跑一个不存在的配置项

当前 [MiniStore](../../projects/MiniStore/README.md) 每次打开都把完整 SSTable/WAL 状态装入内存有序 map；没有磁盘 block cache、可替换的 cache policy、异步后台读取或命中计数器。对其 CLI 做高频 `get` 时，每条命令还要启动新进程、打开/恢复整个库，启动和恢复成本会污染结果；不能将此误称为 MiniStore LRU/FIFO 对照。

因此 Lab 12 提供 MiniStore 风格的**只读点查 overlay-cache 模拟**：固定 seed 生成同一条热点/长尾轨迹，分别比较 no-cache、LRU、FIFO；每策略对同一个内存 backend 返回相同 key/value，并输出 trace/result SHA-256、hits/misses/evictions 与 hit rate。它验证缓存逻辑和工作负载敏感性，**不计时，也不是 MiniStore 产品代码性能观测**。改变容量/热点/扫描比例，观察不同策略的命中率变化，并解释为什么不能从 hit rate 推导毫秒延迟、吞吐或设备 I/O。

运行：

```sh
cd labs/lab-12-performance-benchmark
python3 bench.py cache --keys 1024 --operations 12000 --capacity 32 --seed 20261009
```

若需要真实引擎层缓存对照，应选能暴露 cache 配置/命中指标的引擎版本，固定配置和数据集、读取引擎统计，并先验证校验和；不能修改 M05 MiniStore 仅为了制造看似真实的设备读取路径。

## 五、报告模板与判读

每个配置记录：

```text
目的/假设：
功能正确性：测试命令、PASS/FAIL、校验方式/数据摘要、错误/超时
环境：主机/CPU/内存、内核、文件系统与挂载、设备/虚拟机、功耗/资源限制
软件：fio/RocksDB/编译器/OS 包版本、构建选项
数据：生成种子、数据集尺寸、初始状态、cache/预热、读写比例、key/value/block size
并发：worker/client、实际 outstanding I/O/队列深度、到达模型、运行窗口
语义：buffered/direct、sync/fsync、压缩、WAL、校验/复制设置
每轮结果：吞吐/IOPS、成功/失败数、p50/p95/p99、CPU/队列/内存等
波动：逐轮值、运行级中位数与范围/置信区间；未计入的误差来源
结论与边界：只描述这台机器/这组配置观察到什么，后续要验证什么
```

- 先呈现 correctness，再展示性能。校验失败的数据行不可拿来证明“速度更快”。
- 逐轮报告吞吐和延迟分位数；包含预热策略和缓存状态。不把运行中间样本混成大量“独立重复”。
- 观察饱和曲线而不是单点：吞吐平台、p95/p99 上升、CPU/IO 等待、队列长度和回落后的恢复。说明客户端是否自己成为瓶颈。
- 如硬件/环境信息不全，报告可用于练习方法和同一环境的回归检查，但**不发布硬件能力结论**。不能补造一个“典型 NVMe 速度”来填空。

## 设备基准的安全与可比性边界

任何裸设备写入都可能破坏数据、分区或文件系统元数据，因此本课不向 `/dev/nvme*`、`/dev/sd*` 或分区写入。不同引擎、队列深度、缓存状态、同步语义和版本之间的单次结果不能形成通用排行。课程练习仅使用独立临时文件、新建临时数据库或合成轨迹；隔离设备测试须另行确认目标可丢弃、访问权属、恢复路径和版本化测量条件。

详细的批量大小、队列深度和性能报告方法见 [M12-L01](M12-L01-负载建模、延迟分位数与饱和背压.md)；块设备单位和写入风险见 [M01-L02](../M01-设备与块存储/M01-L02-SSD内部行为与块存储路径.md)。

## 课后问题

1. 为什么 `fio psync QD=1` 和 `io_uring QD=64` 不是公平 API 对照？
2. fio 初始化写阶段的 CRC 校验通过，是否意味着设备断电后文件必然在？为什么？
3. RocksDB `readrandom` 热缓存和冷启动初次读各包含哪些不同成本？如何在不清全局页缓存的前提下描述实验？
4. `db_bench ops/sec` 与 fio IOPS 的操作单位分别是什么？何时不能比较？
5. MiniStore 的全库内存加载会如何污染“磁盘缓存策略”实验？要增加真正 block cache 至少先补哪些语义与测试？

## 参考资料与来源

- [Lab 12：脚本、测试和安全边界](../../labs/lab-12-performance-benchmark/README.md)
- [fio 官方文档](https://fio.readthedocs.io/en/latest/fio_doc.html)，以及实际发行版的 `fio --help`/`--cmdhelp`。
- [RocksDB Benchmarking tools](https://github.com/facebook/rocksdb/wiki/Benchmarking-tools)；工具选项按 v11.8.1 tag/实际 build 验证。
- [课程版本矩阵](../../课程大纲/版本与实验环境.md)、[MiniStore 项目限制](../../projects/MiniStore/README.md)、[IndexCacheLab 轨迹边界](../../projects/IndexCacheLab/README.md)。
