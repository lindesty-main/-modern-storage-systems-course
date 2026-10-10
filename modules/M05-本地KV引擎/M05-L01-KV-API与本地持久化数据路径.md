# M05-L01：KV API 与本地持久化数据路径

> 状态：课程正文　｜　建议课时：110–125 分钟（含 MiniStore 实验）　｜　前置：[M01–M04](../README.md) 的文件/IO、异步完成与持久化边界概念；会用 shell，并能读基础 C++。默认实验只用新建的本地临时目录。

## 学习目标

学完后你应能：

1. 从 `put/get/delete/list` 的调用追踪到内存表、WAL、SSTable 与文件同步点；区分 API 返回、进程重启恢复和断电持久化承诺。
2. 为 key/value、覆盖、删除、列举、缺失键、非法输入及错误退出码写出明确契约，而不是依赖函数名猜测语义。
3. 解释为什么“把 key 和 value 填进一个 NVMe 块”还没有定义索引、覆盖、空间回收或恢复协议。
4. 编译并操作 C++20 MiniStore，验证覆盖、删除、排序列举和新进程读取。
5. 找到项目安全路径与限制：指定目录、校验文件类型、symlink 拒绝、单实例锁、容量限制及非生产级持久性边界。

## 教学节奏

| 时间 | 内容 |
|---|---|
| 0–12 分钟 | 从旧 SPDK 块读写示例到“真正的 KV 语义” |
| 12–35 分钟 | API 契约、错误语义与数据路径 |
| 35–55 分钟 | 内存 map、key/value 校验、序号和存储目录 |
| 55–85 分钟 | 构建项目并完成 CLI 实验 |
| 85–105 分钟 | 读 `Store::put/get/erase/list`、定位同步点 |
| 105–125 分钟 | 故障边界、常见实现误区与练习 |

## 场景：LBA 读写还不是 KV 服务

底层设备 I/O 和上层 key/value 查找是不同职责。一个 NVMe 命令写入 4 KiB，只能说明某个地址范围提交给了设备接口；它没有自动记录 key 的位置、覆盖规则、枚举方式、半条记录处理或重启后的索引恢复。KV 引擎必须明确定义这些语义。

定长数组和线性查找可以帮助解释小数据集的 API；但 `strncpy` 本身不能保证长度校验或字符串终止，内存数组也没有自动提供持久化、恢复、覆盖和删除语义。MiniStore 先明确接口，再定义文件格式和重启路径；它使用标准容器 `std::map`，不将易失内存结构当作持久化实现。

## 1. API 契约先于数据结构

MiniStore 提供的本地 API 子集如下：

| 操作 | 输入与成功语义 | 错误语义 |
|---|---|---|
| `put(key,value)` | 对 key 做 upsert；若已存在，整值替换；空 value 是合法值；每次成功操作分配一个递增序号 | key/value 超限或非法时拒绝；同步写失败时报告错误，操作最终结果可能不确定 |
| `get(key)` | 返回最近一次成功 put 的完整 byte value | 不存在/已删除返回退出码 3，不伪造空字符串 |
| `delete(key)` | 对现存 key 写入删除记录；后续 get/list 看不到该 key | key 不存在也返回退出码 3，不追加空操作 |
| `list()` | 返回当前存活 key，按 ASCII 字节序排序；value 用 hex 输出 | 空集合为空输出；数据库/校验错误报错 |

key 限为 1–128 个 `[A-Za-z0-9._-]` ASCII 字符，避免大小写、编码和路径字符带来的歧义。value 为 0–1 MiB 字节串；shell argv 不能承载 NUL，所以 CLI 不提供 NUL 输入方式；显示采用 hex，保证换行和制表符不会破坏一行一条结果。项目不做多 key 批次、事务、范围查询或跨调用快照。

API 语义必须描述“返回成功究竟意味着什么”。单次 `put/delete` 返回 `OK seq=N` 前，程序完成 WAL append 和 `fdatasync`，然后更新当前进程内存 map。它不是跨 key 原子事务，也不是对任意掉电、设备固件错误或文件系统缺陷的保证。每条 CLI 命令都是独立进程，下一次命令重开目录并重放 WAL，所以自测可以验证“重启/进程退出后的恢复”，但不能替代真实电源故障实验。

## 2. 数据路径与边界

```text
命令行 argv
  │ parse command / validate key and value / check explicit --dir
  ▼
open DB directory + LOCK (flock, single process)
  │ FORMAT 校验；加载 TABLE.sst；重放和校验 WAL
  ▼
有序内存 map（本原型的 MemTable analogue）
  ├─ get/list：读取最新可见 Entry；墓碑不返回
  └─ put/delete：生成 seq 和 WAL record → append → fdatasync
                                     │ 成功后才更新内存状态并输出 OK
                                     ▼
                         下一次 CLI 重开时恢复同一状态

compact：有序存活项 → 临时 SSTable → fdatasync → rename + fsync(dir)
         → 新空 WAL → fdatasync → rename + fsync(dir)
```

接口层负责输入校验和错误码；`Store` 负责内存状态、WAL、快照和锁；文件系统负责文件名、页缓存和同步请求；设备/内核继续位于下层。MiniStore 不直接操作块设备，也不依赖 SPDK。`put` 的顺序刻意是“先记录可恢复意图，再修改易失内存”：如果先改 map 后写 WAL，系统调用失败后内存与磁盘可能分叉。

存储目录由用户显式传给 `--dir`。`init` 只创建一个不存在的新末级目录，权限为 `0700`；不存在的父目录不会递归创建。key 不是磁盘文件名，数据文件通过固定名字相对 directory fd 打开，使用 `O_NOFOLLOW` 并检查普通文件、owner、权限与硬链接数。每个打开者取独占 `flock`；同时第二个进程直接报错，而不是假装支持并发服务。不会递归清理数据库目录，也不覆写既有 init 目标。

## 3. 关键代码导读

源码：[`projects/MiniStore/src/ministore.cpp`](../../projects/MiniStore/src/ministore.cpp)，完整运行与安全说明见[项目 README](../../projects/MiniStore/README.md)。

1. **`run()`** 解析固定 CLI 子集；`init` 与常规访问分开，参数错误返回 2。
2. **`valid_key()/require_key()`** 在 key 进入容器或 WAL 前限制字符集合。输入验证是存储格式的一部分，不应只靠调用者“保证传得对”。
3. **`Store::open()`** 打开目录、获取 LOCK、校验 `FORMAT`，再载入 snapshot 和 WAL。存储损坏与 key not found 区分为退出码 1 和 3。
4. **`Store::put()`** 先做长度/容量检查，再进 `append_operation()`；写入完整 WAL record，`fdatasync` 成功才更新 `entries_` 和 `max_sequence_`。
5. **`Store::get()/list()`** 读取同一 map。`std::map` 让 list 本身按 key 排序，墓碑记录保留在内存但隐藏于 API。
6. **`Entry`** 保存最新序号、tombstone 标志和值；空 value 的 `vector` 仍可表示一条存在记录，和“不存在”不同。
7. **错误处理**：短读/短写循环处理 `EINTR` 与短进展；EOF、长度、版本、CRC、序号等异常不以默认值继续。RAII `Fd` 统一关闭描述符。

这里的有序 map 是一个教学选择，不是大规模最优索引：点查找/插入为 `O(log n)`，全量 list 为 `O(n)`；进程每次启动都要读完整表和 WAL。后续 M06 才会更系统地讨论索引/内存布局。

## 4. 实验：本地创建、覆盖、删除、重启读取

在仓库根目录执行：

```sh
make -C 'projects/MiniStore' clean test
cd 'projects/MiniStore'
TMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/ministore-l01.XXXXXX")
trap 'rm -rf -- "$TMP_ROOT"' EXIT HUP INT TERM
DB="$TMP_ROOT/db"
./build/ministore init --dir "$DB"
./build/ministore put --dir "$DB" color blue
./build/ministore get --dir "$DB" color
./build/ministore put --dir "$DB" color green
./build/ministore get --dir "$DB" color
./build/ministore put --dir "$DB" empty ''
./build/ministore list --dir "$DB"
./build/ministore delete --dir "$DB" color
./build/ministore get --dir "$DB" color || test "$?" -eq 3
```

关键预期（TAB 分隔列）：

```text
initialized .../db
OK seq=1
key    color    value_hex    626c7565
OK seq=2
key    color    value_hex    677265656e
OK seq=3
key    color    value_hex    677265656e
key    empty    value_hex
OK seq=4
not found: key not found: color
```

每条命令独立启动，`get`/`list` 是重新 open、重读 snapshot/WAL 的实际路径。`list` 只显示存活项，`empty` 的空 hex 字段代表合法的空 value。检查退出码：非法 key（空格、`../x`、斜线）返回 2；不存在 key 返回 3；同一时刻另一个进程占有 LOCK 时访问返回 1。

**安全约束：**本实验只用脚本创建的临时目录。不要把 `DB` 改为家目录、仓库根目录、挂载目录或现存数据库路径；不要直接用 `rm -rf` 清理自己不确认归属的路径。测试使用各自 `TemporaryDirectory` 并自动清理。若 `init` 中途失败，MiniStore 不会删掉部分创建的目录，先检查再处理。

## 5. 常见误区与边界

- **“有 `put/get` 就已经是 KV 引擎。”** 还要定义 key 编码、覆盖/删除、缺失、排序、错误、并发、持久化和恢复。
- **“value 为空等同于 key 不存在。”** 本项目用 Entry/tombstone 区分；空 value 长度为 0，key 仍然存在。
- **“write 返回 / NVMe completion 表示数据已经安全落 NAND。”** 同步完成、设备命令完成、`fdatasync` 完成、控制器掉电保护和断电后恢复不是同一个保证。M04 callback 模型也只检查控制流，不证明设备掉电一致性。
- **“一个 `flock` 让 MiniStore 成为并发数据库。”** 它只让遵循锁协议的数据库进程互斥，当前选择的是拒绝第二个进程，不是并发读写、事务隔离或锁升级。
- **“限制 key 字符是任意简化。”** 明确有限 API 降低格式/展示歧义，并保证 key 永不拼成文件路径；它不是通用 Unicode KV API。
- **“实验在 `/tmp` 成功就验证硬盘掉电持久性。”** 测的是当前内核/文件系统/设备对本机 `fdatasync` 的行为，不包含真实断电注入或厂商持久性承诺。
- **不要把一次设备输出当成可重复的性能数据。** MiniStore 必做实验不依赖 SPDK、root 或硬件；真实设备测量须记录 PCI 拓扑、设备状态、软件版本、负载和重复分布。

## 检查题

1. `put(k, "")` 后 `get(k)` 与 `get(missing)` 如何区分？若 CLI 只用空字符串返回错误，会产生什么歧义？
2. 为什么 key 不能直接成为 `DB/<key>` 的文件名？目录 fd、`O_NOFOLLOW` 和字符白名单分别解决什么问题？
3. `fdatasync(WAL)` 成功后，程序崩溃但用户没看到 `OK`，重启时这个 put 可能存在吗？客户端如何处理不确定结果？
4. 描述内存先改后写日志与本项目先写日志后改内存的不同失败窗口。
5. 解释本项目 `list` 是排序结果，但不因此获得多命令事务快照。

## 参考来源与版本

- 项目实现与运行边界：[MiniStore README](../../projects/MiniStore/README.md)。
- [MiniStore 项目与代码](../../projects/MiniStore/README.md)：C++20 / 无第三方依赖；测试在本地临时目录运行。
- Linux [`fdatasync(2)` / `fsync(2)`](https://man7.org/linux/man-pages/man2/fdatasync.2.html)、[`flock(2)`](https://man7.org/linux/man-pages/man2/flock.2.html)、[`openat(2)`](https://man7.org/linux/man-pages/man2/open.2.html)；接口/错误语义以本机 Linux man-pages 为准。
- C++ 版本写入项目 `Makefile` 的 `-std=c++20`。没有锁定或依赖特定 NVMe、SPDK 或 RocksDB 运行环境；不声称跨平台或 production-ready。
