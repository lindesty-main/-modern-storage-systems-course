# MiniStore：本地持久化 KV 教学项目

MiniStore 是 M05 的可运行 C++20 单机 KV 原型；M08 在同一存储引擎上增加一个有限的**本地对象 API**。实现范围刻意收窄：一个显式指定的本地目录、单进程独占、CLI KV 与对象操作、追加 WAL、内存有序表、一个排序 SSTable 和手动整表 compaction。它用于追踪 API 到 WAL、恢复和压实的数据路径，**不是生产级数据库或对象服务**，不提供网络服务、S3/SigV4 兼容、事务批次、并发读写、多层 LSM、加密、复制或生产级崩溃验证。M07 增加的进程级故障测试只验证协议/恢复逻辑，不是 OS 崩溃或真实设备掉电认证。

## 构建与自测

依赖 Linux、G++ 13 或兼容的 C++20 编译器、GNU Make 和 Python 3 标准库；无第三方库、无 root、无设备、无网络。

```sh
cd projects/MiniStore
make clean test
# 或在仓库根目录使用干净重建的本地 CI 门禁：
make -C 'projects/MiniStore' ci
make clean
```

`make test` 编译正常的 `build/ministore` 和带测试挂点的 `build/ministore_test`，并运行 `tests/` 下的黑盒 API、故障恢复和本地 benchmark 测试。`make ci` 强制先清理再构建并运行全套测试；`make clean` 只删除项目 `build/`。只有测试二进制编入 `MINISTORE_ENABLE_TEST_FAILPOINTS`；测试挂点覆盖 WAL 写入/同步边界和 compaction rename 阶段，测试子进程可被 `SIGKILL` 后由新进程验证恢复。同步错误挂点在真正 `fdatasync` 调用前合成错误，不模拟内核或设备错误。测试只创建、检查和删除各自的临时目录；不访问真实设备、网络服务或用户现有数据目录。

**重要边界：**`SIGKILL` 只终止用户进程，OS、页缓存、文件系统和设备仍运行。通过测试不等于内核重启/主机掉电安全，也不验证控制器缓存、flush/FUA、介质原子写或硬件保护。

## 快速体验

```sh
cd projects/MiniStore
TMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/ministore-demo.XXXXXX")
trap 'rm -rf -- "$TMP_ROOT"' EXIT HUP INT TERM
DB="$TMP_ROOT/db"                 # 必须是尚不存在的新目录名

./build/ministore init --dir "$DB"
./build/ministore put --dir "$DB" user-7 'Ada Lovelace'
./build/ministore get --dir "$DB" user-7
./build/ministore put --dir "$DB" user-7 'Ada Byron'
./build/ministore list --dir "$DB"
./build/ministore delete --dir "$DB" user-7
./build/ministore get --dir "$DB" user-7 || test "$?" -eq 3
./build/ministore compact --dir "$DB"
```

关键输出形态（sequence 编号依实际执行递增）：

```text
initialized /tmp/ministore-demo.../db
OK seq=1
key    user-7    value_hex    416461204c6f76656c616365
OK seq=2
key    user-7    value_hex    416461204279726f6e
OK seq=3
not found: key not found: user-7
compacted seq=3 live_keys=0
```

实际输出中的列分隔符是 TAB。`get/list` 把 value 输出为十六进制，避免换行、制表符和任意非 NUL 字节破坏行格式。交互式 `put KEY VALUE` 从一个 argv 接收值，不支持在单条命令参数中携带 NUL；持久化记录本身按长度保存 bytes。`list` 按 ASCII key 升序输出，空库输出空行。

## CLI 与 API 语义

```text
ministore init    --dir PATH
ministore put     --dir PATH KEY VALUE
ministore get     --dir PATH KEY
ministore delete  --dir PATH KEY
ministore list    --dir PATH
ministore object-put    --dir PATH BUCKET KEY < OBJECT
ministore object-get    --dir PATH BUCKET KEY > OBJECT
ministore object-delete --dir PATH BUCKET KEY
ministore object-list   --dir PATH BUCKET [PREFIX]
ministore compact --dir PATH
```

- `init` 要求 PATH 最后一段**不存在**，以 `mkdir` 创建 mode `0700` 的目录；从不复用或清空既有目录。
- key 是 1–128 个 ASCII 字符，只允许 `[A-Za-z0-9._-]`；没有大小写折叠，比较按 ASCII 字节序。此规则也避免 key 被当成文件路径。
- value 是 0–1 MiB 的字节串；空值与“key 不存在”不同。重复 `put` 是原子可恢复的**整值替换/upsert**，不是追加或局部修改。
- `get` 返回最后一次成功 put 的完整 value；不存在或已删除的 key 返回退出码 3。
- `delete` 对存在的 key 写 tombstone；删除不存在的 key 返回退出码 3，不生成 WAL 记录。`list` 排除墓碑，按 key 排序。
- 每个成功的单条 `put/delete` 分配单调递增 sequence，先追加 WAL 并 `fdatasync`，再更新本进程内存状态。`OK seq=N` 表示同步调用成功，不是跨机器或硬件掉电认证。
- 退出码：0 成功，1 存储/IO/格式错误，2 参数/key/value 错误，3 key 不存在。
- 一个进程执行一个 CLI 操作；打开数据库时以 `flock(LOCK_EX|LOCK_NB)` 获取独占锁。并发进程不会自动排队；第二个打开者得到明确错误。锁是协作式本机锁，不抵御忽略锁协议的进程或恶意同 UID 操作者。
- 当前 live key/value 总 payload 限制为 256 MiB，单个 WAL 上限 64 MiB；到达 WAL 上限后拒绝写入并提示执行 `compact`。不自动后台压实。

## 本地对象 API（M08 有限子集）

```sh
# OBJECT 由 stdin 提供原始字节；GET 将原始字节写到 stdout
ministore object-put    --dir PATH BUCKET KEY < OBJECT
ministore object-get    --dir PATH BUCKET KEY > OBJECT
ministore object-delete --dir PATH BUCKET KEY
ministore object-list   --dir PATH BUCKET [PREFIX]
```

这是一个用于教学的本地 CLI/API 子集，不是 HTTP 服务，也**不宣称 S3 或 SigV4 兼容**。桶名为 1–63 位 ASCII 字母/数字；对象 key 为 1 个以上 `[A-Za-z0-9._-]` 字符，并受内部 KV key 总长 128 字节约束（key 长度上限随桶名长度减少）。对象 key 不允许 `/`、空格、UTF-8 或任意 Unicode。对象数据从 stdin 读取、输出到 stdout，支持 NUL 和其他二进制字节，单对象上限 1 MiB；空对象和不存在的对象可区分。

对象以内部键 `@obj:<bucket>:<key>` 保存在同一 WAL / map / SSTable 中；`@` 不属于普通 KV key 的允许字符，因此内部对象记录与所有原有普通 KV key（包括 `obj-` 前缀）不冲突，也不会出现在普通 `list` 中。已有 M05 数据可由 M08 版本读取；写入对象后不要降级回 M05 旧二进制（它不认识内部 key，会 fail closed），项目不承诺跨版本降级兼容。每次 `object-put` 都是整对象覆盖/upsert，单条 WAL 记录和 MiniStore 的同步提交路径给出本地原子替换行为；`object-delete` 删除存在对象，重复删除返回退出码 3；`object-get` 缺失也返回 3。`object-list` 返回 key 字节序排序的 `key + size` 行，可给简单前缀，但每次扫描全量内存 map；没有分页/续传 token、定界符/目录语义、版本、条件请求、Range、批量删除或多 key 事务。对象 API 不支持用户自定义 metadata、Content-Type、ACL、ETag/对象级校验和；size 是从 payload 派生的元数据，不是独立可配置字段。

互斥和错误边界沿用 MiniStore：进程持锁期间第二个请求立即失败（退出码 1），不排队，也不保证并发读写；写入错误可能有结果不确定性，重新打开并 `get/list` 核实。成功的单对象操作可由后续 CLI 进程读取；不代表网络服务、跨对象一致性或真实掉电保证。

## 数据路径与持久化顺序

```text
CLI KV/object put/delete
    │ validate key/value or bucket/key; allocate seq
    ▼
append encoded WAL record ── fdatasync(WAL) ──► commit acknowledged
    │
    └── update ordered in-memory map (MemTable analogue)

CLI get/list/object-get/object-list: open DB + exclusive lock → load TABLE.sst → replay WAL → query map

compact:
  sorted live entries → TABLE.sst.tmp → fdatasync → rename(TABLE.sst) → fsync(db dir)
  create empty WAL.tmp → fdatasync → rename(WAL) → fsync(db dir)
```

每次 CLI 命令都由新进程重新打开数据库，所以后续命令会执行进程级重新打开/恢复路径（不是 OS/主机重启）。恢复从 SSTable 装载快照，再顺序校验 WAL：有效记录按 sequence 更新 map；只有尾部不完整（短 header 或未写完 payload/checksum）的记录会被截去并同步；完整记录的 checksum、magic、长度或 sequence 有错时 fail closed，不静默丢弃可能已确认的记录。

Compaction 先将完整快照写入临时文件、同步文件、原子 rename 并同步数据库目录，再原子替换 WAL。若崩溃发生在新表发布后、旧 WAL 替换前，旧 WAL 仍然存在；表头 high-water sequence 使恢复跳过快照已包含的记录，避免旧日志把新值回滚。rename 之前的未引用临时文件不会成为恢复输入。初始化同步文件、数据库目录和父目录项。

`fdatasync` 成功仍依赖本机内核、文件系统、驱动、设备缓存/flush 实现正确兑现语义；它不保证应用/硬件缺陷之外的任意断电行为，不提供设备级掉电保护、跨文件系统或远程存储承诺。同步错误发生时，写的最终结果可能不确定，CLI 会报错；重新打开后用 `get/list` 检查，不要直接盲目重试具有外部副作用的上层操作。

## 目录、格式与安全边界

初始化后的目录只包含本项目使用的名字：

```text
DB/
├── FORMAT       # 固定 magic、版本和 CRC32
├── LOCK         # flock 协作锁
├── WAL          # append-only 记录流
└── TABLE.sst    # compact 后存在；一个排序快照
```

Compaction 短暂使用固定名字 `TABLE.sst.tmp`、`WAL.tmp`；恢复只读取正式文件。整数以 little-endian 明确编码，不直接 dump C++ struct，因此不依赖 ABI padding。WAL 记录布局为：`MWR1 | seq:u64 | op:u8 | key_len:u16 | value_len:u32 | key | value | crc32:u32`；CRC 覆盖 magic 至 value。SSTable 有 48-byte header（magic/version/count/max-sequence/payload-length/payload-CRC/reserved），随后是按 key 严格升序的 `seq:u64 | key_len:u16 | value_len:u32 | key | value` 记录。CRC32 用于检测意外损坏，不是加密签名或防恶意篡改。

路径安全约束：PATH 的父目录须存在；父路径先 canonicalize，数据库最后一段通过 `openat`/`O_NOFOLLOW` 打开；数据库目录要求属于当前用户且 mode `0700`，内部文件要求当前用户所有、单硬链接、无 group/other 权限且为普通文件。格式文件、WAL、SSTable、锁和临时文件都拒绝符号链接。键永远不是文件名；程序不递归删除任何目录，不覆盖已存在的 init 路径，不接受设备文件。父目录可能由文件系统路径解析经过符号链接，但操作最终相对于 canonical 的父目录 fd 进行。权限/锁是防误操作边界，不是同 UID 恶意进程的沙箱。

对练习只把数据库放在自己创建的临时/专用目录中。快速体验里的 `trap rm -rf` 只对该命令刚刚创建并保存在 `TMP_ROOT` 的目录清理；**不要改成指向用户目录、仓库根目录、挂载盘或任意现存路径**。如果 init 中途失败留下目录，程序不会自动清除它；先检查其内容，再由操作者决定如何处理。

## 实现导读

- `src/ministore.cpp:Store::put/erase/get/list`：CLI API 语义和有序内存 map；`put` 完整替换，`erase` 生成墓碑。
- `Store::append_operation`：序号分配、64 MiB 限制、完整写循环、`fdatasync` 后才确认并更新内存。
- `Store::recover_wal`：固定长度字段解析、边界/CRC/序号校验、尾部部分记录处理；完整损坏记录报错。
- `Store::load_table` / `encode_table`：显式 little-endian 快照格式与排序约束。
- `Store::compact`：临时文件同步、原子 rename、目录同步、WAL 轮换；此实现通过整体重写单个表来压实，并非 RocksDB 多层并发 compaction。
- `open_parent_and_name` / `open_database_directory` / `require_private_regular`：路径、目录权限、symlink 和文件类型检查；`Fd` 对象管理 fd 生命周期。

自动化测试在 `tests/`，只使用黑盒 CLI 与临时目录，包含 KV 读写、对象 put/get/delete/list、二进制对象、整值覆盖、空对象及对象数据 compaction 恢复、桶隔离、前缀列举、命名校验、1 MiB 边界/超限、名字空间隔离和持锁竞争错误；M15 另有 KV+对象混合 compact/restart 端到端回归。还覆盖空值、非法键、init 不覆盖、截断 WAL 尾部修复、完整记录/表校验失败关闭、compaction 故障窗口及 symlink 拒绝。M07 另用测试二进制在部分 record、完整 write 后/`fdatasync` 前、sync 成功后/响应前、table rename 后和 WAL rename 后杀进程；每次由新进程检查值、尾部修复和后续写入。一个合成 sync 错误用例检查报错与可见状态的不确定性。`tests/test_bench_ministore.py` 验证 MiniStore CLI 基准参数/统计与一个小型真实 CLI 正确性 smoke run；基准报告脚本只在自建临时数据库工作。挂点和用例详情见 [M07-L02：故障模型与恢复断言](../../modules/M07-持久性与崩溃一致性/M07-L02-故障模型、崩溃注入与恢复断言.md)；对象 API 契约见 [M08-L01](../../modules/M08-对象存储服务/M08-L01-对象API、元数据与一致性.md)。

## MiniStore CLI 基准报告（M15）

`bench_ministore.py` 在每次运行新建临时数据库，固定 seed 产生 GET/PUT 轨迹，校验每次 GET 与最终全部 key，再输出 JSON：每轮 p50/p95/p99/min/max、读写数、吞吐、trace 与最终值 SHA-256、环境及 release binary SHA-256。参数受限为最多 1000 操作、1024 key、64 KiB value、5 次重复和 32 MiB 最坏生成 payload；脚本不接受外部 DB 路径。

```sh
make clean all
TMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/ministore-bench.XXXXXX")
trap 'rm -rf -- "$TMP_ROOT"' EXIT HUP INT TERM
python3 bench_ministore.py --operations 60 --keys 8 --value-size 256 \
  --repetitions 3 --write-percent 30 --seed 20261015 > "$TMP_ROOT/report.json"
python3 -m json.tool "$TMP_ROOT/report.json" >/dev/null
```

该计时包含每个请求的 CLI 进程启动、打开/恢复 WAL、锁和输出；put 还包含 WAL `fdatasync`。一请求一进程、QD=1；不是常驻服务，不清全局页缓存。结果只表示当次本机软件路径，不能声称设备 IOPS、硬件持久性、并发服务、云端性能或与 RocksDB 的对比。Lab 12 的普通文件 benchmark 与缓存命中率模拟是独立负载，不是 MiniStore 实测。完整报告要求见[M15-L03](../../modules/M15-综合项目/M15-L03-基准报告、发布清单与限制复盘.md)。

## 明确未实现 / 不可推断

- 没有 mmap、块设备/SPDK、缓存管理器、Bloom filter、索引块、范围扫描 API、manifest 多代、Level-0 多 SSTable、leveled/tiered compaction、后台线程、写批次或跨 key 原子事务。
- 读取时将整个 SSTable/WAL 装入内存并扫描 WAL，复杂度和内存开销不适合大数据集；compaction 又同时构造 payload 与输出 buffer。容量限额仅保护教学实现的基本边界，不代表可扩展性。
- `bench_ministore.py` 提供受限的本机 CLI 端到端负载观察，包含进程启动、全 WAL replay、锁与 WAL 同步；它不是引擎内部 microbenchmark。读写放大需另行定义字节口径和工作负载，报告不能代表 RocksDB、真实设备或服务端吞吐。
- CRC32 不防恶意文件修改；没有身份认证/授权、对象级策略、租户隔离、静态/传输加密、密钥管理、安全审计、在线/异地/不可变备份、复制、灾备或供应链签名/SBOM/provenance 保证。目录 `0700`、内部 owner/mode 检查和 `flock` 仅是本机误用/协作边界，不隔离同 UID 或 root。进程级 SIGKILL 测试只验证应用协议与恢复分支；没有系统崩溃、真实断电、硬件缓存/flush、设备原子写或生产文件系统 fault-injection 认证，因此不宣称生产级崩溃一致性或安全性。

配套课文：[M05 本地 KV 引擎模块](../../modules/M05-本地KV引擎/README.md)、[M14 安全与可靠性](../../modules/M14-安全与可靠性/README.md)与[M15 MiniStore 综合项目](../../modules/M15-综合项目/README.md)。默认实验不要求 root、真实 SSD、`/dev` 节点、loop、SPDK 或第三方服务。
