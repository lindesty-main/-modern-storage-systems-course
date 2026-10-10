# M05-L02：WAL 记录格式与崩溃恢复

> 状态：课程正文　｜　建议课时：115–130 分钟（含故障注入练习）　｜　先修：[M05-L01：KV API 与数据路径](M05-L01-KV-API与本地持久化数据路径.md)、M02 的文件读写与同步接口。课程故障实验只修改本课刚创建的临时数据库。

## 学习目标

学完后你应能：

1. 解释 write-ahead log 的“先日志、后内存/快照”顺序和一个成功 put 的同步点。
2. 按字节解析 MiniStore WAL record，说明版本、长度、序号、操作码与 checksum 如何帮助恢复。
3. 区分尾部不完整记录、完整记录校验失败、缺失序号和已同步但客户端未收到响应等情况。
4. 画出 MiniStore 的 open/recovery 与 compaction 两阶段文件更新顺序，推理关键崩溃窗口。
5. 在隔离临时目录故意制造截断和损坏，观察“恢复可修复尾部”与“损坏 fail closed”的不同结果。
6. 准确说明 `fdatasync`、文件系统、设备缓存、掉电原子性与课程模型的保证边界。

## 教学节奏

| 时间 | 内容 |
|---|---|
| 0–15 分钟 | 进程内存为什么需要 WAL |
| 15–40 分钟 | 记录字段、字节序、长度与校验 |
| 40–60 分钟 | 启动恢复状态机与错误分类 |
| 60–85 分钟 | `fdatasync`、系统调用错误与确认语义 |
| 85–110 分钟 | 临时目录故障注入与自动测试 |
| 110–130 分钟 | compaction 崩溃窗口、错误认识和练习 |

## 场景：重启后 MemTable 不再存在

`put` 成功后，最新值可能同时位于进程内存和日志文件中。进程正常退出时 map 消失；如果没有可恢复记录，下一次启动既不知道 key，也无法区分“从未写入”和“曾写入但丢失”。WAL（write-ahead log）把操作按序追加，启动时从稳定存储重放，以恢复内存状态。所谓 write-ahead 的关键不是文件名，而是协议顺序：**先让一条足够重建状态的日志记录通过规定的同步点，再对外确认，并在它之后更新易失状态**。

```text
put(key,value)
  validate → seq=N → encode WAL record → append → fdatasync(WAL)
                                                       │成功
                                                       ▼
                                         update in-memory map → print OK

restart:
  load SSTable snapshot → scan WAL records in order → rebuild map → serve request
```

设备命令完成说明调用链得到该接口定义的完成结果；KV 恢复还需要定义记录边界、key 的索引状态、同步点、重放顺序、去重/序号及错误策略。单有 callback 或一次读回不能替代 WAL 协议。

## 1. MiniStore 的 WAL 格式

MiniStore 使用明确编码的 little-endian 字节序，不把 C++ struct 直接 `write()` 到磁盘（否则会依赖 padding、ABI 和 host endian）。每条记录布局：

```text
偏移      字段                  大小       说明
0         magic "MWR1"          4 bytes    record 标识
4         sequence              u64        从 1 单调递增
12        operation             u8         1=put，2=delete
13        key_length            u16        1..128
15        value_length          u32        0..1 MiB
19        key                   key_length ASCII bytes
...       value                 value_length bytes；delete 必须为 0
末尾      crc32                 u32        IEEE CRC32，覆盖 magic 到 value
```

固定 header 为 19 bytes；总 record 长度为 `19 + key_length + value_length + 4`。格式限值必须先验证，再计算偏移或分配内存；不能依磁盘中不可信的长度直接申请任意大小。CRC32 可以检测非恶意损坏，不提供认证、抗篡改或密码学安全。sequence 让恢复检测重排/丢记录，也使 compaction 后能识别已包含的日志前缀。

实现位置：`encode_wal_record()` 负责编码；`recover_wal()` 做魔数、操作码、key/length、CRC 和序号检查；`append_operation()` 检查最大 WAL 尺寸、完整写入、同步并更新 map。所有可持久化整数通过 `append_u*()/read_u*()` 显式转换。

## 2. 启动恢复状态机

MiniStore 首先取得独占 LOCK，再校验 FORMAT 和 SSTable，最后重放 WAL；在互斥锁范围内，其他 MiniStore 进程不能同时追加。每条 record 只有在完整且校验通过后才能应用到 map。

```text
WAL offset = 0
while offset < file_size:
    若剩余字节不足固定 header          → 不完整尾部：截断到 offset
    校验 magic / opcode / 长度          → 失败：拒绝打开
    计算总 record 长度
    若文件剩余不足总长度                → 不完整尾部：截断到 offset
    校验完整 record CRC                 → 失败：拒绝打开，不猜测修复
    校验 sequence 连续、key 格式有效     → 失败：拒绝打开
    若 seq 大于 SSTable watermark        → 应用 put/delete 到 map
    否则                                 → 该日志操作已在快照中，跳过
    offset += record_length
```

尾部不完整是预期崩溃形态：进程可能在 write 中途退出，最后一条记录没有完整落入文件。只允许把**末尾**未完成片段截掉，并对修复后的 WAL 再 `fdatasync`。已经完整写入却 checksum 不匹配，或文件中部出现坏 magic/序号断裂，不知道后续记录是否依赖它，程序选择 fail closed，而不是安静地丢数据继续运行。CRC 不可用来判定“损坏一定只是最后一条”。

如果一次 `fdatasync` 返回成功，MiniStore 下次 open 会尝试用该记录恢复；但若 sync 报错，内核/设备可能已写入部分或全部记录。程序不更新本进程 map并返回错误，调用方要重开后查询。相反，记录已同步但进程在打印 `OK` 前退出，调用者没收到确认，但重启后记录可能存在：这是经典的**结果不确定**窗口，重试只能依赖操作幂等性或应用层 request ID（MiniStore 没有实现 request ID）。

## 3. 持久化顺序、错误传播与限制

| 事件/故障时点 | MiniStore 的处理/可能观察 | 不应推断 |
|---|---|---|
| 写 WAL 前失败 | 无新记录，map 不变 | 不会报告成功 |
| record 只写入一部分后进程退出 | 重启识别末尾短记录，截到最后完整边界 | 不是任意介质损坏都能修复 |
| 完整 record checksum 错 | 拒绝打开，保留故障证据 | 不静默回退到旧值 |
| append 完整、`fdatasync` 返回错误 | 操作结果可能不确定，命令报错 | 报错不代表绝对没有写入 |
| `fdatasync` 成功、打印成功前进程退出 | record 可能在重启后可见，但调用方没收到确认 | 不构成 exactly-once API |
| `fdatasync` 成功后异常断电 | 取决于内核、文件系统、控制器缓存、设备 flush 等 | 课程测试没有验证真实掉电安全 |

`fdatasync(WAL)` 请求同步文件数据以及之后读取数据必需的元数据（例如文件长度）；数据首先可能进入内核页缓存，最终可靠性仍依赖下层正确兑现 flush/持久性约定。一次 `write()`、一次 SPDK callback、普通 `close()` 都不能替代应用明确的同步策略。`fsync` 还同步更多文件元数据，但“多同步一些”也不能修复未定义的格式/排序协议。

WAL 追加通过完整写循环处理短写和 `EINTR`，再 `fdatasync`；短进展/IO 错误向上抛出。文件长度限制为 64 MiB，超过后停止接收写入，提示先 `compact`。这不是后台日志滚动。每条 CLI 操作有独占锁，但不实现跨操作事务；当前进程错误退出会释放 flock。

## 4. Compaction 与旧 WAL 的恢复窗口

MiniStore compaction 会把最新存活 key 的排序快照写到临时 SSTable。顺序不能交换：

1. 新表完整写入临时文件并 `fdatasync`；
2. rename 临时表为 `TABLE.sst`，并 `fsync` 数据库目录；
3. 写入并同步空 `WAL.tmp`；
4. rename 为 `WAL`，并再次同步数据库目录。

若崩溃在步骤 2 后、步骤 4 前，磁盘上是新表加旧 WAL。快照保存 `max_sequence`；恢复跳过所有 `seq <= max_sequence` 的旧日志记录，后续日志仍连续重放，不会把较新的快照值覆盖成旧值。若崩溃发生在步骤 2 前，旧表和完整旧 WAL 仍是恢复依据。若 WAL rename 成功，先前同步的表已经是稳定基线，空 WAL 可安全开始新 generation。这个两文件协议是本项目明确简化的教学实现，不等同于 RocksDB 的 MANIFEST、多个 WAL、列族和多版本生命周期。

## 5. 实验 A：验证截断尾部恢复

先构建并运行课程自动测试（包含截断 header/payload、旧 WAL 与已发布表的窗口）：

```sh
make -C 'projects/MiniStore' clean test
```

可选地手动操作一个**新建临时目录**：

```sh
cd 'projects/MiniStore'
TMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/ministore-wal.XXXXXX")
trap 'rm -rf -- "$TMP_ROOT"' EXIT HUP INT TERM
DB="$TMP_ROOT/db"
./build/ministore init --dir "$DB"
./build/ministore put --dir "$DB" stable visible
python3 - "$DB/WAL" <<'PY'
import pathlib, sys
path = pathlib.Path(sys.argv[1])
with path.open('ab') as f:
    f.write(b'MWR1\x01\x02')  # 只有部分末尾 header，不是有效记录
PY
./build/ministore get --dir "$DB" stable
```

预期仍输出 `key<TAB>stable<TAB>value_hex<TAB>76697369626c65`；重启时 WAL 尾部被截回原有效长度。把实验目录限定在 `TMP_ROOT`，绝不对用户目录或其他数据库使用这种“故意破坏文件”的命令。

## 6. 实验 B：完整损坏记录应失败关闭

使用另一个刚创建的实验数据库，写入一条记录后只翻转 WAL 最后一个 checksum byte：

```sh
python3 - "$DB/WAL" <<'PY'
import pathlib, sys
path = pathlib.Path(sys.argv[1])
data = bytearray(path.read_bytes())
data[-1] ^= 0x80
path.write_bytes(data)
PY
./build/ministore get --dir "$DB" stable
```

预期命令非零退出，stderr 包含 `checksum mismatch (refusing recovery)`；这次损坏 record 长度完整，不能误当成“尾部写到一半”。想验证表文件 checksum，可以执行 `compact` 后在临时测试中翻转 `TABLE.sst` payload 字节；项目自动化测试覆盖两种损坏的 fail-closed 行为。

实验 A/B 必须使用测试专属临时目录，脚本中的路径由 `$DB` 精确指向本次 init 创建的库。退出时只清理由该脚本刚创建的 `$TMP_ROOT`。

## 常见误区

- **“WAL 是一条 `write`，因此有原子性。”** 文件写可能短写、被信号打断或只留下部分记录；格式必须能识别完整边界，CRC 只检测损坏，不制造原子写保证。
- **“尾部 CRC 错就一定安全截断。”** checksum 错可能来自完整但已损坏/已确认的记录；项目明确拒绝静默丢弃。只截断结构上不完整的尾部。
- **“同步失败意味着数据没落盘。”** 错误结果可能不确定；恢复后查询，并由上层设计幂等重试。
- **“同步成功意味着断电必然保存。”** `fdatasync` 是向栈下发同步请求，不是课程对 SSD 固件、易失缓存或电源失效的认证。
- **“sequence 是事务 ID / 去重键。”** 它只排序单条本地操作、检查日志连续性、帮助快照跳过旧日志；客户端重试可能产生新的 sequence。
- **“重放就是无条件从 WAL 开始应用所有历史。”** 若有 snapshot，必须用一致的 checkpoint/watermark 处理旧 WAL；否则已有值可能被较老操作覆盖。

## 检查题

1. MiniStore 为什么在 WAL checksum 不匹配时停止，而在 EOF 处的短 record 可以截断？给出可能丢失已确认操作的反例。
2. 若 `write()` 完整返回但 `fdatasync()` 失败，客户看到什么？进程内 map 和下一次启动的状态分别可能是什么？
3. 若 `fdatasync()` 成功而进程在输出成功响应前崩溃，为什么“客户端重试一次”不等价于 exactly-once？
4. 假设 sequence 100 已包含在表中，但旧 WAL 还包含 1–100，sequence 101 追加在其后。恢复应如何应用？
5. MiniStore 的初始化、compaction rename 为什么同步目录？它们与同步 WAL 数据本身解决的故障范围有何不同？
6. 设计一个进程级 fault-injection 测试，但不要把它误称为真实掉电测试。

## 来源与复核边界

- [MiniStore 格式、恢复代码与测试](../../projects/MiniStore/README.md)、[实现](../../projects/MiniStore/src/ministore.cpp)、[故障测试](../../projects/MiniStore/tests/test_ministore.py)。
- 延伸阅读：[M05-L01](M05-L01-KV-API与本地持久化数据路径.md)、[M05-L03](M05-L03-SSTable-LSM-compaction与读写放大.md)；设备 IO 完成、日志同步与恢复语义必须分别定义。
- RocksDB 官方文档：[Write Ahead Log](https://github.com/facebook/rocksdb/wiki/Write-Ahead-Log-(WAL))；它描述成熟系统的设计，不意味着 MiniStore 复制了其格式/保障。
- Linux [`fdatasync(2)` / `fsync(2)`](https://man7.org/linux/man-pages/man2/fdatasync.2.html)、[`write(2)`](https://man7.org/linux/man-pages/man2/write.2.html)、[`rename(2)`](https://man7.org/linux/man-pages/man2/rename.2.html)、[`flock(2)`](https://man7.org/linux/man-pages/man2/flock.2.html)。文件系统与设备具体持久性须按部署栈验证。
- 版本：项目要求 C++20；实验用当前主机 Linux/G++ 与 Python 标准库。没有进行真实断电注入、设备缓存验证或长期文件系统故障测试。
