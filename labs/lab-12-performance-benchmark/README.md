# Lab 12：安全本地基准与缓存模拟

本实验提供两个互相独立的本地教学工具：

1. **普通文件观察**：生成器只在自动创建的私有临时目录创建一个普通文件，使用固定种子执行 buffered `pread/pwrite`，输出逐轮及 aggregate 的 p50/p95/p99、吞吐、参数、环境和 SHA-256 校验。
2. **MiniStore 风格缓存模拟**：对同一条合成 key 访问轨迹比较 no-cache/LRU/FIFO 的 hits、misses、evictions 和校验摘要；不计时。

## 安全边界

- 依赖 Python 3.10+ 标准库，不安装第三方包，不需要 root，不启动服务，不访问云或网络。
- 文件模式**不接受路径参数**；数据集大小最多 64 MiB、操作数最多 20,000、重复最多 10。脚本在自己的临时目录创建/删除数据，只对该普通文件进行偏移读写。
- 不访问 `/dev`、块设备、分区、loop 设备、用户数据路径或云盘，不用 `O_DIRECT`。没有 `fio`/RocksDB 时仍可完成必做实验。
- 不使用全局清缓存/提权来控制页缓存。重复运行可能复用 OS 页缓存，状态在输出中标为不可控。
- 生成器的临时文件在退出时删除；JSON 默认输出到 stdout。若想保存报告，Shell 重定向的目标文件由操作者自行选择，建议放在专用实验目录。

## 文件基准

```sh
cd labs/lab-12-performance-benchmark
python3 -m unittest -v test_bench.py
python3 bench.py file \
  --mode randread \
  --blocks 1024 \
  --block-size 4096 \
  --operations 2000 \
  --repetitions 3 \
  --seed 20261009
```

模式：

- `seqread`：按 block 顺序重复读取。
- `randread`：固定 seed 的随机 block 读取。
- `mixed`：随机读取与写入；默认写入比例 10%，可用 `--write-percent 0..100` 改变。每轮初始数据和轨迹相同，写入数据由 seed/操作序号确定。

示例报告用法：

```sh
python3 bench.py file --mode mixed --blocks 1024 --operations 1000 \
  --repetitions 3 --seed 20261009 > file-report.json
python3 -m json.tool file-report.json >/dev/null
```

JSON 包括 `config`、主机 Python/内核信息、IO 语义、`correctness`、逐次 `runs` 与 `aggregate`。每轮给出操作数、读写字节、观察吞吐和延迟 `p50/p95/p99/min/max`；`aggregate` 合并所有运行的请求样本，并另计合并 wall duration。分位数使用最近秩法。逐轮分布仍须保留，不要只看合并值或最好值。

### 指标解释

- 功能检查不计入单次 I/O latency timer：读取值与预期 block 比较，结束后全文件逐 block 验证，输出最终 SHA-256。`correctness.passed=true` 只表示生成器的数据路径校验通过。
- 延迟是 Python 进程中单 worker/单请求 outstanding（QD=1）的 `pread/pwrite` 调用时间，短传输续作也计入该次逻辑操作；不是应用端到端服务时延或内核设备延迟。
- 吞吐以所有操作/字节除以测量循环 wall time，包含 Python 循环开销；文件是 buffered regular file，不含逐次 `fdatasync/fsync`，写入可能停留在页缓存。
- 初始化文件时会在测量前 `fsync`；每轮复用同一临时文件路径并重新填充初始数据，页缓存没有强制清除。程序**不**宣称冷盘、耐久性、硬件 IOPS 或设备带宽。
- 数据集、随机种子和请求数固定能复现功能结果及访问轨迹；纳秒计时会随机器、内核、文件系统、负载、温控、电源模式与后台活动变化，不能要求不同机器出现同一个数。

## MiniStore 风格缓存实验

```sh
python3 bench.py cache \
  --keys 1024 \
  --operations 12000 \
  --capacity 32 \
  --seed 20261009
```

程序用固定 seed 构建同一条热点/全域混合轨迹，比较 `none`、LRU、FIFO。报告包含 trace SHA-256、每种策略的命中/未命中/驱逐、驻留项数量、命中率和返回值 SHA-256；`correctness.passed` 要求各策略对相同轨迹返回相同结果。可改变容量、key 数和 seed 做单变量对照。命中率是确定性模拟结果，策略执行未计时，不能推导 wall-clock latency、MiniStore 或设备性能。

**重要边界：**当前 MiniStore 每次启动时把整个库恢复进内存 map，本身没有可调 block cache，也没有所需命中指标。本子实验是“MiniStore 风格只读查询上加一个 bounded cache”的模型，不改写/不冒充 MiniStore 产品代码。MiniStore 功能回归仍独立运行：

```sh
make -C 'projects/MiniStore' clean test
```

## 可选 fio / RocksDB 对照

分步实验、锁定参数、临时目录清理、fio/RocksDB 的版本边界见[M12-L02](../../modules/M12-性能工程/M12-L02-fio、RocksDB与缓存策略对照.md)。

- fio 只可在自己新建的临时目录普通文件上运行。不得将真实 NVMe 设备作为 `dd`/fio 输出目标；本实验任何命令都不应以 `/dev/...` 为目标。
- 使用 `fio --version`、`fio --help` 和已安装版本文档核对参数；内核/容器策略拒绝 io_uring 时可记录 SKIP，不加权限绕过。
- RocksDB `db_bench` 是可选项，课程候选 v11.8.1；仅在临时 DB 目录运行并留存实际 tag/commit、构建选项和初始数据状态。Lab 不下载/构建 RocksDB、不默认从网络连接数据库。
- fio 文件 IO 与 RocksDB KV 操作的层次与指标口径不同，不做跨工具单数字排名。

## 自动测试

```sh
cd labs/lab-12-performance-benchmark
python3 -m unittest -v test_bench.py
```

8 项测试校验 nearest-rank 分位数和参数错误、固定 seed 多轮文件读写/最终数据校验、不同 seed 数据差异、LRU/FIFO 的预期不同驱逐行为、no-cache/LRU/FIFO 对相同访问序列返回一致值/容量约束，以及 CLI 拒绝外部文件路径。测试不比较、不接受任何具体延迟或吞吐数值。

## 性能报告必须写明

- 机器/CPU/内存、内核、文件系统/挂载、使用环境是否 VM、负载时系统状态。
- 工具/依赖/编译版本；数据规模、seed、请求分布/尺寸、读写比、线程/队列深度。
- 缓存与预热、buffered/direct、写入同步/校验、工作集、错误和功能校验摘要。
- 每次重复的 p50/p95/p99、吞吐/运行时；运行级中位数/范围或置信区间和样本量。
- 所有遗漏条件及结论边界。没有硬件条件时只报告“此环境下观测到的值”，不能伪造一个典型硬件数字或声称产品级比较。

关联课文：[M12-L01 负载/分位数/饱和](../../modules/M12-性能工程/M12-L01-负载建模、延迟分位数与饱和背压.md)、[M12-L02 fio/RocksDB/缓存对照](../../modules/M12-性能工程/M12-L02-fio、RocksDB与缓存策略对照.md)、[M06 IndexCacheLab](../../projects/IndexCacheLab/README.md)。
