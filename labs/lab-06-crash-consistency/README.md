# Lab 06：MiniStore 进程级崩溃一致性

> 目标：在明确的 WAL/compaction 阶段终止 MiniStore 子进程，再由新进程执行恢复并验证状态。适用 Linux、C++20 编译器、Make、Python 3；不需要 root、块设备、loop、网络或第三方包。

## 安全边界（先读）

本实验只由自动测试在 Python `TemporaryDirectory` 下创建短暂的普通文件数据库。测试二进制 `build/ministore_test` 有编译期启用的 failpoint；它用 `SIGKILL` 停止一个 MiniStore 用户态进程。正常 `build/ministore` 不包含这些挂点。

**`SIGKILL` 不是物理掉电。**内核、页缓存、文件系统和设备仍有电，未 `fdatasync` 的数据在同一次系统启动中仍可能被重放。本实验禁止对真实设备断电、写裸盘、使用现存数据库目录或把 failpoint 环境变量传给非测试程序；绝不把测试结论描述成设备掉电/生产保证。

## 运行完整实验

从仓库根目录执行：

```sh
make -C 'projects/MiniStore' clean test
```

该命令构建正常 CLI 与测试 CLI，执行既有 M05 回归和本 Lab 的恢复断言；测试用临时目录会自动清理。单独运行部分 WAL 故障用例：

```sh
cd 'projects/MiniStore'
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest -v \
  tests.test_ministore.MiniStoreTest.test_sigkill_during_partial_wal_record_repairs_only_the_tail \
  tests.test_ministore.MiniStoreTest.test_sigkill_after_wal_write_before_sync_can_replay_unacknowledged_write \
  tests.test_ministore.MiniStoreTest.test_sigkill_after_fdatasync_before_ack_recovers_the_committed_write
```

Compaction 阶段与合成 sync 错误用例可分别选取：

```sh
cd 'projects/MiniStore'
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest -v \
  tests.test_ministore.MiniStoreTest.test_sigkill_during_compaction_publication_recovers_snapshot_and_wal \
  tests.test_ministore.MiniStoreTest.test_injected_wal_sync_error_reports_uncertain_outcome
```

## 注入点、oracle 和预期结果

| 阶段 | 自动注入 | 必须检查的恢复结果 | 解释边界 |
|---|---|---|---|
| record header 中段 | 仅 append 9 bytes 后停住，Harness 发 `SIGKILL` | 前一条完整记录仍在；新 key 不存在；recovery 把 WAL 截回最后完整边界并同步修复 | 只验证 EOF 结构不完整 tail |
| 完整 record 写完、sync 前 | `write_all` 后暂停并 `SIGKILL` | 无 `OK`；同一内核下新进程观察到 record 并重放 | 说明未确认写可能出现；不证明断电后保留 |
| `fdatasync` 成功、响应前 | `fdatasync` 返回后暂停并 `SIGKILL` | 无 `OK`；新进程能读回写入值 | 已过 MiniStore 提交点但调用方结果不确定，不是 exactly-once |
| 合成 `fdatasync` 错误 | 完整 append 后、真正 syscall 前抛测试专用 EIO | 命令报错且无 `OK`；新进程在本次启动中恢复可见记录 | 不模拟内核/设备实际 EIO；只检查错误传播 |
| TABLE 发布、目录同步前 | `TABLE.sst` rename 后暂停并 `SIGKILL` | 新表 + 旧 WAL 可恢复，旧日志不回滚 snapshot；后续可写入 | 仅检查当前 namespace 中的进程恢复 |
| WAL 轮换、目录同步前 | `WAL` rename 后暂停并 `SIGKILL` | 新表 + 空 WAL 可恢复，后续可写入 | 不证明掉电后目录项状态 |

完整 checksum/magic/sequence 错误必须 fail closed；本 Lab 不允许把损坏记录静默删除来“让测试通过”。测试代码及精确断言位于 [MiniStore 黑盒测试](../../projects/MiniStore/tests/test_ministore.py)，阶段挂点的编译隔离位于 [MiniStore Makefile](../../projects/MiniStore/Makefile) 和源码。

## 恢复与复盘

- **课程自动库：**断言失败时保留测试输出，查看失败的 test case；测试临时库由清理器删除。不要向生产/个人数据目录拷贝任何注入步骤。
- **自己运行的 MiniStore：**出现恢复错误后停止写入，保留原目录和诊断信息；先按数据保护流程制作副本。MiniStore 只修复结构不完整的 WAL 尾部，对完整 checksum/magic/sequence 错误会拒绝打开；它没有 WAL salvage/自动备份。sync 错误或响应丢失后重新查询 key，不能仅凭 CLI 报错认定写入未发生。
- **复盘报告：**对每一用例记录 (1) fault model，(2) 最后完成的 write/sync/rename，(3) 客户端是否收到成功，(4) 恢复 oracle，(5) 一个测试未覆盖的系统/设备条件。

详细概念与恢复矩阵见 [M07-L02：故障模型、崩溃注入与恢复断言](../../modules/M07-持久性与崩溃一致性/M07-L02-故障模型、崩溃注入与恢复断言.md)；同步边界见 [M07-L01](../../modules/M07-持久性与崩溃一致性/M07-L01-持久性边界、同步接口与写入顺序.md)。
