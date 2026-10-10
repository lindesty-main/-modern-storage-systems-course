# M12：性能工程

> 状态：两课正文与 Lab 12 本地基准工具已完成。必做路径只使用 Python 标准库、自动创建的普通临时文件和合成工作负载；不访问裸设备、云服务或用户指定的数据路径。

## 模块目标

- 从 SLO 与工作负载出发设计比较，而不是先选工具或优化点。
- 同时报告吞吐、请求延迟分位数、运行间波动、缓存状态和饱和信号。
- 区分“结果正确”与“观察到的性能数值”；理解重复、误差、缓存预热和背压。
- 以安全普通文件做可复现 fio 对照，以锁定版本/参数的 RocksDB `db_bench` 作为独立引擎案例。
- 通过固定种子访问轨迹比较 MiniStore 风格的 LRU/FIFO 命中率，同时说明当前 MiniStore 没有可替换的 block cache，不将模拟数据写成 MiniStore 实测性能。

## 课次与实验

| 课次 | 内容 | 产出 |
|---|---|---|
| [M12-L01：负载建模、延迟分位数与饱和背压](M12-L01-负载建模、延迟分位数与饱和背压.md) | 工作负载维度、吞吐/延迟分布、样本与重复误差、应用/页/引擎缓存、队列饱和、尾延迟和背压 | 为一个 SLO 写出受控负载、正确性门槛、观察指标和运行间误差报告 |
| [M12-L02：fio、RocksDB 与缓存策略对照](M12-L02-fio、RocksDB与缓存策略对照.md) | 普通文件 fio 配置、RocksDB `db_bench` 边界、MiniStore 项目缓存观察、数据校验与安全复现 | 提交安全命令/参数、版本、分位数、校验结果与明确的不可推广结论 |
| [Lab 12：安全本地基准与缓存模拟](../../labs/lab-12-performance-benchmark/README.md) | 自动生成普通临时文件的 p50/p95/p99 基准，以及 MiniStore 风格 LRU/FIFO 固定轨迹模拟 | JSON 输出含种子、参数、环境、逐轮延迟分位数/吞吐、SHA-256 校验；8 项标准库测试 |

## 默认验收

```sh
cd labs/lab-12-performance-benchmark
python3 -m unittest -v test_bench.py
python3 bench.py file --mode mixed --blocks 1024 --operations 1000 --repetitions 3 --seed 20261009
python3 bench.py cache --keys 1024 --operations 12000 --capacity 32 --seed 20261009
```

文件基准只在内部创建的私有临时目录操作 buffered regular file，单进程、单 worker、QD=1；写入不逐条 `fsync`，页缓存状态不可控。它验证逻辑读写/最终数据 checksum，并观察 Python `pread/pwrite` 工作负载时间，不是设备带宽、持久性或引擎性能证明。Cache 子实验只报告合成轨迹的 hits/misses/evictions，并要求三种策略读取结果 checksum 一致；不计时。

## 安全与性能报告边界

- 默认实验不接受任意文件名、设备路径、目标目录、远端 endpoint，不用 root，不进行外网访问；生成数据量和请求数有上限。
- `fio` 是选修对照，只能把 `--filename` 固定在学生刚创建的临时目录普通文件；禁止 `/dev/*`、裸盘、loop 设备或现有用户文件。无 fio 环境时完成 Lab 12 即可。
- RocksDB 是选修源码/工具对照，课程候选为 v11.8.1；构建依赖未由 Lab 12 安装或验证。每轮使用新临时 DB，记录实际 tag/commit、编译选项和缓存参数。
- 功能正确性先于性能：脚本/引擎数据校验 PASS 不能证明性能优越；更高吞吐也不能补偿不正确结果。比较必须固定数据集、请求分布、块/值尺寸、线程/队列、同步语义、缓存策略和预热方式。
- 报告必须分列每次运行 p50/p95/p99、运行间中位数/范围或置信区间；不能只挑最好一次。机器、内核、文件系统/挂载、介质、功耗策略、依赖版本和并发活动缺失时，不下硬件性能结论。
- 单次 `dd` 结果不能代表设备性能；任何基准都不得把裸设备作为未经隔离的输出目标。课程实验只使用普通临时文件和合成模型，见 [M01-L02](../M01-设备与块存储/M01-L02-SSD内部行为与块存储路径.md)。

## 参考资料

- [fio documentation](https://fio.readthedocs.io/en/latest/fio_doc.html)：记录 fio 版本、job 文件和实际选项；发行版文档与运行时支持仍需核验。
- [RocksDB `db_bench` wiki](https://github.com/facebook/rocksdb/wiki/Benchmarking-tools)：基准参数随版本变化，课程阅读/运行基线见[版本矩阵](../../课程大纲/版本与实验环境.md)。
- [IndexCacheLab](../../projects/IndexCacheLab/README.md)：M06 已有固定轨迹正确性/命中率对照，不测延迟；M12 延伸工作负载与报告解释，不将 hit rate 当硬件性能。
- [MiniStore](../../projects/MiniStore/README.md)：整库载入内存，不具备可配置 block cache；其真实 GET 路径不适合伪造缓存命中延迟对比。
