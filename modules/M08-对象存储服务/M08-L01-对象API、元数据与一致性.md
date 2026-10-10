# M08-L01：对象 API、元数据与一致性

> 状态：课程正文　｜　建议课时：110–125 分钟（含 MiniStore 本地实验）　｜　先修：[M05 本地 KV 引擎](../M05-本地KV引擎/README.md)、[M07 持久性与崩溃一致性](../M07-持久性与崩溃一致性/README.md)。默认实验只访问测试自行创建的临时目录。

## 学习目标

学完后你应能：

1. 用 bucket/key/content 解释对象 API，指出对象与 POSIX 文件、块地址、KV value 的关联和差异。
2. 为 PUT/GET/DELETE/LIST 写出明确契约，区分对象 key、系统元数据、用户元数据和应用数据库记录。
3. 画出请求控制路径、元数据路径和对象字节数据路径，并解释分离它们的设计收益与一致性成本。
4. 区分 AWS S3 产品保证、S3 API 兼容实现和本课程 MiniStore 本地子集；不从协议兼容推导相同可靠性、配额或权限语义。
5. 运行 MiniStore 自带对象客户端测试并描述它的容量、命名、列举和并发边界。

## 教学节奏

| 时间 | 内容 |
|---|---|
| 0–15 分钟 | 从文件/块/KV 回顾命名与存储语义 |
| 15–40 分钟 | bucket、key、payload 与对象元数据；API 契约 |
| 40–65 分钟 | 控制面、元数据面、数据面；上传/读取路径 |
| 65–85 分钟 | 覆盖、列举、并发和产品一致性范围 |
| 85–110 分钟 | MiniStore 代码与本地实验 |
| 110–125 分钟 | FastDFS 对照、误区和检查题 |

## 1. 从字节地址到对象名

块设备以 LBA/长度访问字节范围；文件系统以目录和 inode 提供层次化名字、权限与文件操作；KV 引擎把应用 key 映射到 value；对象服务则通常以 `(bucket, key)` 标识对象，并对完整内容及其元数据提供服务。它们可以复用下层文件、块、KV 或分布式存储，但上层 API 契约并不因此相同。

对象 key 看起来像路径（例如 `reports/2026/q1.csv`），这不自动意味着对象服务有 POSIX 目录、rename 或目录锁。很多服务把 `/` 当作 key 中普通字符，控制台只将公共前缀显示成“文件夹”。一个 `LIST prefix=reports/` 是按名字前缀查询，不等同于遍历文件系统目录树。

对象本身可以拆成几类信息：

| 信息 | 例子 | 主要用途 | 常见保存位置 |
|---|---|---|---|
| 标识 | bucket、key、可选 version ID | 查找对象 | 元数据索引/对象名映射 |
| 系统元数据 | 长度、ETag、校验和、修改时间、存储类别 | 服务端校验、响应、生命周期/计费 | 对象索引、manifest 或服务内部元数据 |
| 用户元数据 | `x-amz-meta-*` 一类应用属性 | 应用自定义标签 | 对象元数据记录 |
| payload | 图片、备份、日志、数据文件字节 | 实际数据 | 数据面，可能由一个或多个 extent/chunk 组成 |
| 授权/策略 | principal、action、resource、条件 | 判断谁能执行什么 | IAM、桶/访问策略、服务配置 |

这只是通用设计分解，不是某个产品的内部文件格式。元数据有时与 payload 放在同一对象记录/索引里；大对象可能先进入临时片段，再由 manifest 指向片段。实现选择需要兼顾点查找、列举、原子发布、恢复、校验和垃圾回收。

## 2. API 契约先于实现

任何 storage API 都应先说清调用者观察到什么，而不是只列出操作名。一个典型 API 族包含：

- 创建/检查 bucket：命名规则、所有权、区域或 namespace 规则因产品而异。
- `PUT bucket/key`：写入新对象或替换现有对象；请求需定义 payload 长度、校验、条件头和返回信息。
- `GET/HEAD bucket/key`：取 payload 或仅取元数据；缺失、无权和临时故障必须能区分。
- `DELETE bucket/key`：永久删除、写删除标记、版本化删除或“不存在也成功”都可能出现，取决于产品及版本配置。
- `LIST bucket?prefix=...&continuation-token=...`：列举规则、排序、分页、delimiter、“目录”的呈现方式都是 API 契约的一部分。

**覆盖**不是“同名写入也许会产生两个文件”。必须定义并发写入同一 key 时哪个版本可见、失败是否留下部分对象、响应丢失后如何查询，以及是否支持条件更新/版本历史。**列举**也不能用“能看到所有 key”概括：大集合常分页，客户端必须保留 continuation token；一个 ListObjects 请求的页数不是多 key 快照事务。

对 metadata 同样如此：`Content-Type`、缓存控制、自定义 metadata、ETag、校验和、标签、ACL、版本号、保留策略、生命周期，可能分别有不同 API 和语义。不要拿一个字符串字段或对象名字推断权限、完整性或保留行为。

### 单对象与多对象原子性

单个对象的发布通常要避免调用方读到“新对象只写了一半”。常见做法是在临时位置写完并校验，再原子切换对象索引/manifest；读者按已发布元数据定位完整代际。若数据分片，则 manifest 发布可成为逻辑提交点。具体实现、错误处理和持久性仍依赖产品。

这不意味着一组 key 有事务：`PUT a` 成功、`PUT b` 失败时，许多对象服务不会回滚 `a`。要构造业务原子更新，应用可能需要 manifest/版本指针、数据库事务、幂等请求 ID 或补偿流程；对象 API 的单 key 行为不能替代业务事务。

## 3. 控制路径、元数据面和数据面

术语没有唯一标准定义。本课将 **控制面** 用于身份、策略、bucket 配置、配额/生命周期与服务端点；**元数据路径**用于解析 bucket/key、校验请求、定位版本和数据位置；**数据面**负责传输和读取 payload。部分产品把控制面和元数据面合并，也可能让客户端直接访问数据节点。每次画图要写出你所描述的产品与版本。

```text
PUT 示例

客户端
  │ HTTPS 请求：bucket/key + headers + payload
  ▼
DNS / gateway / load balancer
  │ 认证签名、授权策略、限流、请求校验
  ▼
API 前端 / 元数据服务 ──检查 namespace、条件、版本──┐
  │                                               │
  │ 分配目标/写入临时数据                          │ 更新索引、长度、版本、校验
  ▼                                               ▼
数据节点 / chunk store  ──数据写入/校验──>  manifest 或对象索引提交
  │                                               │
  └────────────── 完成后返回状态/ETag ─────────────┘
```

一种可能的 GET 路径是：身份验证 → 元数据查询确定对象代际与数据位置 → 数据面读取/校验 → 响应流式返回。数据节点可以是本机磁盘、分布式块/对象布局或另一服务；客户端不应因看到 HTTP 接口就假设后端是普通文件。

### 为什么把元数据和字节数据分开

- **延迟与索引：**小型索引记录可放在内存/数据库，避免为每次 LIST 扫描巨大目录。
- **大对象流式访问：**数据可分块、并行写入或使用独立网络路径；元数据服务不必搬运全部 payload。
- **恢复和生命周期：**索引/manifest 记录对象的可见代际，后台任务可识别孤儿片段、旧版本和待清理内容。
- **横向扩展：**metadata 与 data 的容量、CPU、网络热点可以分别扩展；但两面之间出现部分成功时必须有恢复协议。

分离不是免费的：写数据成功但 metadata commit 失败，会留下孤儿 payload；metadata 先发布但数据未可靠落地，会产生指向缺失内容的索引；LIST 的记录数可能和实际数据暂时不匹配；重试可能写出重复片段。方案需要定义写入顺序、状态机、后台清理和结果不确定时的查询机制。M07 的 WAL/manifest 顺序思维在这里仍适用，但并不提供分布式事务。

## 4. 一致性：产品承诺、接口实现和观察结果

“一致性”必须带操作、对象范围、时序、配置、故障模型和产品版本。至少区分：

1. **可见性/读后写：**PUT 成功返回之后，后续 GET/HEAD 是否能看见该版本？LIST 何时包含它？
2. **并发同 key 写：**两个调用是否串行、后完成者胜出、用条件请求拒绝其中一个，或保留版本？
3. **多 key：**多个对象更新有没有事务/原子 snapshot？一般不能从单对象保证推导出来。
4. **副本可用性与持久性：**写确认依赖几份副本、纠删码/缓存、设备同步及后台恢复。可见性强不等于备份充分或免于错误删除。
5. **错误/超时：**客户端没收到响应时，服务端可能已经提交；重试前需要查询状态或使用幂等/条件语义。

AWS 文档对 **Amazon S3 服务本身**说明 PUT/DELETE 等对象操作具有强读后写一致性，也包括 LIST 读取最新写入；这是 AWS 对其产品的承诺，不是 S3 协议条文对所有实现的要求。它并不提供任意多个 key 的 ACID 事务，也不能自动推出跨区域复制、账户配置或每一项附加功能具有同一时序。

Ceph RGW 实现一组 S3 风格接口；其兼容矩阵、对象布局、故障/读写语义要针对固定 Ceph release、配置和已启用功能查文档与测试。MiniStore 的本地实验只观察当前进程/文件系统上的应用协议，不测网络、副本或硬件持久性。切勿用 RGW 的行为替代 AWS 产品承诺，也不要用 AWS 文档替代 RGW 版本测试。

### 条件写、ETag 和重复上传

条件头（例如“仅当当前对象不存在”或基于已知版本/ETag 更新）可以帮助控制并发覆盖，但语义/支持范围要以目标 API 文档为准。ETag 的具体计算方式受分片、加密、上传路径和产品影响；不能普遍当作 MD5 或安全完整性证明。若需要强完整性校验，应使用服务声明支持的 checksum 算法，并验证端到端计算、存储、读取行为。

图床可以在业务表中保存内容摘要与引用计数，以实现“秒传/去重”；这属于业务层策略，不是 FastDFS 的自动去重保证。MD5 有碰撞风险；跨用户复用还可能泄露某个内容是否已存在，并把引用计数、权限和最后删除时机带入一致性问题。业务层应选择适当校验、授权和引用事务，不能把摘要查询当作通用安全方案。

## 5. FastDFS 案例：专用文件服务，不等于对象协议

FastDFS 图床展示了文件服务与业务系统协作时的职责边界：

```text
浏览器/业务 API → Nginx 上传入口 → 图床服务
                                  ├─ MySQL：用户、文件关系、分享/引用计数
                                  ├─ Redis：会话/计数等缓存
                                  └─ FastDFS client → Tracker → Storage group
客户端下载/HTTP 模块 ← file_id / URL ← Storage 数据
```

FastDFS 的 Tracker/Storage 与客户端协议负责节点查询、文件存储和同组同步；图床自己的业务数据库维护“谁拥有该文件、名字、分享状态和引用数”等应用关系。Storage 返回 file ID 供后续定位，不会因此提供 S3 bucket policy、版本控制、HTTP REST 兼容或跨系统事务。

部署和复制行为受 FastDFS 版本、配置及业务接入方式影响。集群设计需核对同步确认点、复制进度、重试与新节点追赶；异步复制期间的可见性和故障恢复不能仅凭接口名称推定。跨数据库、缓存与文件服务的更新还要处理部分成功和重建，不能假设它们构成分布式事务。

`multipart/form-data` 是浏览器把多个表单字段和文件组成一个 HTTP 请求 body 的编码；S3 Multipart Upload 是按 UploadId/PartNumber 管理并最终 Complete/Abort 的对象上传 API。两者名字相似，但状态机、重试和残留清理完全不同。HTTP `Range` / `206 Partial Content` 表示下载的字节范围响应，也不是上传续传会话。Reactor/线程池、数据库连接池和引用计数属于业务服务并发工程，不是 FastDFS 或 S3 自动提供的特性。

图床性能分析应分别观察 HTTP 请求、文件吞吐、网络、业务数据库查询/索引和缓存命中；任何数字都要附带版本、环境、负载和重复条件。FastDFS 是专用文件服务案例，不作为通用对象 API 或 MiniStore 的依赖。

## 6. MiniStore 本地对象 API 子集

M08 给 MiniStore 增加对象视图，底层继续使用现有 C++20 Store、WAL、排序 map、SSTable 和本机锁：

```text
本地 CLI API                          MiniStore 内部
────────────────────────────────────────────────────────────────
object-put BUCKET KEY <stdin>  ──→   @obj:<bucket>:<key> → WAL record + bytes
object-get BUCKET KEY          ──→   lookup in map → raw bytes to stdout
object-delete BUCKET KEY       ──→   WAL tombstone
object-list BUCKET [PREFIX]    ──→   scan map → key + derived size
```

内部 `@obj:` 不能由公开 KV key 字符集表示，因而不与既有普通 KV 名称（包括 `obj-` 开头的 key）冲突。M08 版本可以读取 M05 普通 KV 数据；写入对象记录后旧 M05 二进制不支持降级读取，会对未知 key fail closed。它只是这个演示项目的编码，不是标准对象元数据格式。对象 payload 与 KV value 共用 1 MiB 单值、256 MiB live payload、64 MiB WAL 限制；路径不是用户 key 的组成部分，因此对象名不会变成文件路径。

| 项目 | MiniStore M08 本地契约 |
|---|---|
| Bucket | 1–63 个 ASCII 字母/数字；大小写敏感；不是 S3 bucket 命名规范 |
| Object key | 1 个以上 `[A-Za-z0-9._-]` 字符；不接受斜线/空格/Unicode；受内部 128-byte key 总长限制，桶越长允许的 key 越短 |
| PUT | 从 stdin 读取 0–1 MiB 原始 bytes（可含 NUL）；对 bucket/key 整体 upsert；返回 `OK seq=N size=S` |
| GET | 将完整原始 bytes 写入 stdout；缺失返回退出码 3 |
| DELETE | 仅删除已存在对象并写 tombstone；缺失/重复删除返回 3 |
| LIST | 按内部有序 map 导出的对象 key 顺序输出 key 与 payload size；可选纯前缀过滤，无分页，整张 map 扫描 |
| 错误 | 非法输入/超限退出码 2；锁、IO/格式错误退出码 1；不存在退出码 3；同步错误结果可能不确定 |
| 并发 | 每个 CLI 进程对 DB 取 `LOCK_EX|LOCK_NB`；同时打开的竞争请求立即失败，不等待/排队、不承诺多客户端并发读写 |

size 是按读取到的 payload 长度计算，不是独立 metadata 字段。该子集没有 HTTP/REST listener、SigV4、身份认证/授权、bucket CRUD、用户 metadata、Content-Type、ETag/对象 checksum API、条件请求、Range、versioning、multipart、清单分页、delimiter、复制、生命周期或对象级保留。`object-list` 的单次结果在当前打开期间由内存 map 生成，不应包装成跨请求 snapshot 语义。

`object-put` 完整 record 经 `fdatasync` 成功后才提交应用状态并报告 OK；调用响应丢失或 sync 报错时依然按 M07 处理不确定结果，重开后查询对象。MiniStore 测试中的进程隔离仍不是 OS 重启/掉电实验。由于单对象最大 1 MiB，这个实验也不支持借 multipart 绕开上限。

## 7. 安全本地实验

从仓库根目录执行完整项目回归：

```sh
make -C 'projects/MiniStore' clean test
```

可进行可选手工演示（只在 `mktemp` 生成的私有路径工作）：

```sh
cd 'projects/MiniStore'
TMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/ministore-object-l01.XXXXXX")
trap 'rm -rf -- "$TMP_ROOT"' EXIT HUP INT TERM
DB="$TMP_ROOT/db"
./build/ministore init --dir "$DB"
printf '\000object-bytes\377' | ./build/ministore object-put --dir "$DB" images sample.bin
./build/ministore object-list --dir "$DB" images
./build/ministore object-get --dir "$DB" images sample.bin > "$TMP_ROOT/got.bin"
od -An -tx1 "$TMP_ROOT/got.bin"
./build/ministore object-put --dir "$DB" images sample.bin < /dev/null
./build/ministore object-get --dir "$DB" images sample.bin > "$TMP_ROOT/empty.bin"
test ! -s "$TMP_ROOT/empty.bin"
./build/ministore object-delete --dir "$DB" images sample.bin
```

输出的 `object-list` 展示 key 和 size，不打印 payload。手动脚本只允许 `trap` 清理刚创建并保存在 `TMP_ROOT` 的临时根；不要改成仓库、家目录、已有数据库或挂载盘。

机器可执行的所有断言集中在 [MiniStore 项目自带的黑盒测试](../../projects/MiniStore/tests/test_ministore.py)，Lab 07 给出单独操作和结果解释。测试还验证同一内部格式可以 compaction/WAL 恢复；它不启动监听端口，不要求 AWS CLI 或 Ceph。

## 8. 常见误区

- **“对象存储就是没有目录的文件系统。”** 名称看似路径不代表 POSIX rename、锁、inode、目录遍历或同一权限模型。
- **“S3 兼容就拥有 AWS S3 的所有保证。”** 兼容范围是接口/操作/行为矩阵；一致性、权限、配额、故障和耐久声明分别查产品版本资料。
- **“强读后写等于多 key 事务或高耐久。”** 单对象可见性与多 key 原子性、备份/复制和故障域是不同属性。
- **“ETag 就是 MD5。”** 多段上传、服务端加密和实现差异会改变 ETag 含义；用正式 checksum contract。
- **“LIST 是一次全局快照。”** 分页与后续页时点由服务决定；不要把多次分页自动视为事务快照。
- **“FastDFS 有 Tracker，所以等同 S3 控制面。”** Tracker 的节点发现/调度不是 IAM/API endpoint；图床业务数据库另有元数据和授权责任。
- **“MiniStore 对象 API 已能被 AWS CLI 访问。”** MiniStore 没有 HTTP、REST、SigV4。其必做客户端是项目自己的本地 CLI 黑盒测试。
- **“两个并发进程会排队顺序执行。”** MiniStore 锁是非阻塞独占，第二个进程失败；课程没有宣称并发服务语义。

## 检查题

1. 对象 key 中 `a/b` 常被显示成路径，为什么不能据此承诺目录 rename 或 POSIX 权限？
2. 分离 metadata 和 payload 后，指出“payload 已写但索引未更新”与“索引已发布但 payload 不可读”两个故障窗口，并给出一项恢复/垃圾回收办法。
3. AWS S3 的强读后写声明能推导出任意 S3-compatible RGW 都有相同保证吗？还缺哪些版本、配置和测试证据？
4. FastDFS 的 Tracker/Storage 与图床业务数据库分别负责什么？为什么文件定位和同组同步不等于 bucket/key REST 协议或用户授权？
5. MiniStore 对象命名长度如何取决于 bucket 长度？为什么普通 KV 的 `obj-foo` key 不会碰撞？
6. 另一个 MiniStore CLI 进程已经持锁时，object PUT 返回什么结果？这足以证明对象 API 支持并发写入吗？

## 参考资料与版本边界

核验/版本盘点日期：2026-10-09。产品文档是各自产品的语义来源，课程本地测试只证明 MiniStore 当前实现的观察结果。

- AWS 官方：[Amazon S3 用户指南](https://docs.aws.amazon.com/AmazonS3/latest/userguide/Welcome.html)、[S3 一致性模型](https://docs.aws.amazon.com/AmazonS3/latest/userguide/Welcome.html#ConsistencyModel)、[对象操作与元数据](https://docs.aws.amazon.com/AmazonS3/latest/userguide/UsingObjects.html)。用于 AWS 服务语义，不等于所有兼容实现承诺。
- Ceph：[Tentacle / Ceph 20.2 RGW S3 API](https://docs.ceph.com/en/tentacle/radosgw/s3/)、[Ceph v20.2.4 release](https://github.com/ceph/ceph/releases/tag/v20.2.4)。版本矩阵仅把它列为本地选修目标，课程未将其写成已集成验证。
- FastDFS：[上游 releases](https://github.com/happyfish100/fastdfs/releases)；课程候选版本 V6.18.1。版本号仅是候选锁定值，构建和兼容性仍需按目标环境实测。
- [MiniStore 源码](../../projects/MiniStore/src/ministore.cpp)、[项目契约](../../projects/MiniStore/README.md)、[自动测试](../../projects/MiniStore/tests/test_ministore.py)；本地测试无网络依赖，C++20 编译基线见项目 Makefile。
- 前后衔接：[M05 WAL/LSM](../M05-本地KV引擎/README.md)、[M07 持久性边界](../M07-持久性与崩溃一致性/README.md)、[M08-L02：S3 协议与 multipart](M08-L02-S3协议、分片上传与权限.md)。
