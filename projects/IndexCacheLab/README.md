# IndexCacheLab：索引与缓存策略教学程序

IndexCacheLab 是 M06 配套的纯内存 C++20 教学程序：用统一操作对照分桶链地址哈希、简化跳表和 `std::map` 有序树；再在一条固定种子访问轨迹上比较 LRU/FIFO 淘汰策略。程序强调正确性、复杂度边界和可复现命中率，**不是 benchmark 或生产缓存/索引库**。

## 构建与自动测试

需要支持 C++20 的 G++/兼容编译器及 GNU Make；无第三方依赖、root、磁盘设备、网络或外部数据集。

```sh
make clean test
```

`make test` 编译报告程序与 C++ 自动测试，测试检查：

- 三种索引共同的 put/upsert/get/erase/`[lower, upper)` 范围行为；固定 seed 下 4,000 次随机化操作与 `std::map` 参考模型一致。
- 容量 3 的策略辨别轨迹（LRU 1 hit、FIFO 2 hits）、容量 0 全 miss。
- 12,000 次固定轨迹的复现校验和、策略 hits/misses 与命中率接受值。

单独打印确定性实验报告：

```sh
./build/index_cache_lab --report
```

关键输出（编译器行随实际工具链变化）：

```text
index_workload: entries=1024 puts=1024 point_gets=1024 range=[k000100,k000120) matches=20 correctness=PASS ...
trace: seed=0x4d3036 accesses=12000 checksum_fnv1a64=0x766801a21e475d3a ...
cache: capacity=32 entries; cold start; same trace
LRU:  hits=8251 misses=3749 hit_rate=68.7583%
FIFO: hits=8243 misses=3757 hit_rate=68.6917%
```

## 实现概览

- [`src/index_cache.hpp`](src/index_cache.hpp)：
  - `HashIndex`：链地址桶、FNV-1a 教学散列、负载超过 0.75 时扩容；range 全表扫描并排序。
  - `SkipListIndex`：最多 16 层的随机层高跳表，固定 seed 用于可重复测试；有序范围从前驱位置沿底层遍历。
  - `TreeIndex`：以 `std::map` 建模标准有序树 API；C++ 标准保证对应复杂度，不保证底层结构一定是红黑树。
  - `LruCache` 与 `FifoCache`：固定容量整数 key 条目；策略状态及命中/未命中计数。
  - `make_fixed_trace()`：固定 seed `0x4d3036`、12,000 次请求、热点/扫描/宽集合混合轨迹；模映射是本程序约定，避免不同标准库的 distribution 细节影响接受值。
- [`src/main.cpp`](src/main.cpp)：检查三种索引的 1,024 项 put/get/range 相同结果，输出编译器标识、轨迹校验和和策略命中率。没有测量/打印耗时。
- [`tests/test_index_cache.cpp`](tests/test_index_cache.cpp)：单元/参考模型测试和精确接受值。
- [`Makefile`](Makefile)：默认 `-std=c++20 -Wall -Wextra -Wpedantic -Wconversion -Wshadow`。

## 指标与限制

Cache 容量是 **32 个相同大小的 entry**，冷启动。轨迹每 1,000 次访问中，700 次抽取 24-key 热点，200 次访问 128-key 扫描集合，100 次抽取 256-key 宽集合。命中率按 `hits / 12,000` 计算。固定轨迹 hash 是 FNV-1a 64-bit 版本校验，不是密码学证明。

上述结果是轨迹、容量与当前策略代码的**正确性/验收结果**。此工作负载里 LRU 比 FIFO 多 8 次命中，不表示普遍优越。程序不统计墙钟时间、吞吐、延迟/尾延迟、RSS、分配次数、碎片、页缓存、设备 IO、并发或能耗；不把这些 hit rate 推断为实际应用/SSD 性能。真实 benchmark 需要独立控制版本、硬件、数据集、请求混合、线程数、缓存热度、预热、重复次数和统计口径。

索引样例无持久化/事务/WAL/崩溃恢复、容量上限、恶意输入保护和线程安全；`HashIndex` 的 FNV-1a 不防 HashDoS；跳表固定 seed 仅供教学；`std::map` 是标准库接口模型，不手写红黑树。缓存仅模拟易失内存命中，没有脏数据、写回、一致性/失效、TTL、变长对象或分层存储。程序不接入 M05 MiniStore，因此不会改动其语义；若日后接入，必须补回归测试和缓存失效/一致性验收。

课文：[M06 模块总览](../../modules/M06-索引与内存管理/README.md)、[M06-L01 索引](../../modules/M06-索引与内存管理/M06-L01-KV索引结构与工作负载选择.md)、[M06-L02 内存与缓存策略](../../modules/M06-索引与内存管理/M06-L02-内存分配与缓存淘汰策略.md)。
