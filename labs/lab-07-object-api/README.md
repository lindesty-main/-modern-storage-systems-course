# Lab 07：MiniStore 本地对象 API

> 目标：用项目自带的黑盒客户端验证 MiniStore 自己定义的有限对象 API，并将其与 S3 风格 HTTP API 分开。必做实验仅访问 Python 自动测试创建的临时目录；不需要 root、网络、AWS CLI、云账号或 Ceph RGW。

## 0. 边界和安全要求

本 Lab 中的 `object-put/get/delete/list` 是本地 CLI，不是 HTTP 服务，不接受 AWS CLI/SDK，也不实现 S3 REST、SigV4、权限策略、multipart、用户 metadata 或对象版本。不要把它写成“兼容 S3”。

所有自动测试使用 `tempfile.TemporaryDirectory` 创建并清理独立数据库。手工演示也只能使用 `mktemp` 新建目录，并只清理该脚本刚刚创建、保存在变量里的临时根。不要将 `DB`/`TMP_ROOT` 改成用户目录、仓库、已存在 MiniStore 数据库、挂载目录或真实数据位置。不要对真实设备、网络 bucket 或共享 RGW 执行故障/删除操作。`trap rm -rf` 仅对本脚本创建的临时路径安全；若 `init` 中途失败，先检查而不是扩大清理范围。

## 1. 项目自带客户端自动验收（必做）

从仓库根目录：

```sh
make -C 'projects/MiniStore' clean test
```

`make test` 编译生产 CLI 和隔离的 test CLI，然后 Python 项目测试对临时数据库发起请求。它不需要外网/对象服务。M08 对象用例覆盖：

| 行为 | 自动断言 |
|---|---|
| 二进制 put/get | payload 含 NUL 和 `0xff`，GET 输出与输入逐字节相同 |
| 覆盖 | 第二次 put 整体替换旧 payload，size 为新长度 |
| list | 按 key 排序，区分 bucket，前缀过滤，返回派生长度 |
| delete | 存在对象删除后不可读；重复删除与缺失 GET 返回 not-found 退出码 3 |
| 空/非法/过大 | 空对象可与不存在区分；坏 bucket/key 及超过 1 MiB 输入退出码 2、恰好 1 MiB 成功，超限输入不能产生对象 |
| 名字隔离 | 内部对象记录不出现在普通 KV list；合法的旧式 `obj-...` 普通 KV key 仍可读，不与对象冲突 |
| 并发适用范围 | 另一个进程持有 DB lock 时对象请求以存储错误退出码 1，不排队、不伪装并发成功 |
| 回归 | M05 KV、M07 WAL/恢复/崩溃测试仍然一起运行 |

可单独执行对象相关用例（从项目目录先构建两个 CLI）：

```sh
cd 'projects/MiniStore'
make build/ministore build/ministore_test
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest -v \
  tests.test_ministore.MiniStoreTest.test_object_api_put_get_overwrite_delete_and_list \
  tests.test_ministore.MiniStoreTest.test_empty_object_and_object_data_survive_compaction \
  tests.test_ministore.MiniStoreTest.test_object_api_rejects_invalid_names_and_oversized_payload_without_mutation \
  tests.test_ministore.MiniStoreTest.test_object_namespace_does_not_collide_with_ordinary_kv_keys \
  tests.test_ministore.MiniStoreTest.test_object_api_rejects_a_competing_process_while_database_is_locked
```

自动化用例是客户端对**本项目契约**的黑盒验证，不是 S3 兼容认证、跨实现互操作或性能基准。锁冲突测试验证 MiniStore 的“第二进程立即失败”语义，不证明可并发服务。M07 的进程故障挂点依然只测试用户态协议，不代表 OS 崩溃/断电。

## 2. CLI 约定

```text
ministore init --dir DB
ministore object-put    --dir DB BUCKET KEY < OBJECT_BYTES
ministore object-get    --dir DB BUCKET KEY > OBJECT_BYTES
ministore object-delete --dir DB BUCKET KEY
ministore object-list   --dir DB BUCKET [PREFIX]
```

- bucket：1–63 个 ASCII 字母/数字；不是 S3 bucket 名称验证器。
- key：一个以上 `[A-Za-z0-9._-]` ASCII 字符；不接受 `/`、空格、Unicode；长度受内部 KV key 128-byte 总长限制，bucket 越长 key 越短。
- PUT：stdin 中 0–1 MiB 原始 bytes，包含 NUL；成功时输出 `OK seq=N size=S`。不是 append，重复 PUT 覆盖完整旧对象。
- GET：stdout 输出原始 payload，不加 header/hex 前缀；缺失码 3。
- DELETE：只删除存在对象；重复删除码 3。
- LIST：输出 `object<TAB>KEY<TAB>size<TAB>N`，按 key 字节顺序；可选前缀；一次全量扫描，没有 continuation token。
- 退出码：0 成功、1 锁/IO/格式错误、2 参数或输入范围错误、3 对象不存在。

代码实现、API 限制、S3/产品语义区分和错误结果不确定性详见 [M08-L01](../../modules/M08-对象存储服务/M08-L01-对象API、元数据与一致性.md) 与 [MiniStore 项目 README](../../projects/MiniStore/README.md)。

## 3. 手工演示（可选，仅新建临时目录）

准备并检查 binary round-trip：

```sh
cd 'projects/MiniStore'
make all
TMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/ministore-lab07.XXXXXX")
trap 'rm -rf -- "$TMP_ROOT"' EXIT HUP INT TERM
DB="$TMP_ROOT/db"                 # 由本脚本 init 创建的新目录
./build/ministore init --dir "$DB"
printf '\000lab-object\377' | ./build/ministore object-put --dir "$DB" images sample.bin
./build/ministore object-list --dir "$DB" images
./build/ministore object-get --dir "$DB" images sample.bin > "$TMP_ROOT/readback.bin"
```

确认列举显示 `sample.bin` 和正确 payload 长度，再查看 bytes：

```sh
od -An -tx1 "$TMP_ROOT/readback.bin"
# 预期：00 6c 61 62 2d 6f 62 6a 65 63 74 ff
cmp "$TMP_ROOT/readback.bin" <(printf '\000lab-object\377')
```

`cmp <(...)` 是 Bash process substitution；若使用不支持它的 shell，可另把 `printf` 输出写入 `"$TMP_ROOT/expected.bin"` 后用 `cmp` 比较。覆盖和空对象：

```sh
printf 'replacement' | ./build/ministore object-put --dir "$DB" images sample.bin
./build/ministore object-get --dir "$DB" images sample.bin > "$TMP_ROOT/readback.bin"
printf 'replacement' > "$TMP_ROOT/expected.bin"
cmp "$TMP_ROOT/expected.bin" "$TMP_ROOT/readback.bin"
./build/ministore object-put --dir "$DB" images empty.bin < /dev/null
./build/ministore object-get --dir "$DB" images empty.bin > "$TMP_ROOT/empty.bin"
test ! -s "$TMP_ROOT/empty.bin"
./build/ministore object-list --dir "$DB" images
./build/ministore object-delete --dir "$DB" images sample.bin
```

所有写操作都仅影响本次新建的 `DB`。为了确认删除错误码，可在自己的这个临时 DB 上运行：

```sh
set +e
./build/ministore object-get --dir "$DB" images sample.bin > /dev/null
status=$?
set -e
test "$status" -eq 3
```

## 4. 结果解释与限制

- 对象 payload 存在 MiniStore 共用 KV/WAL/SSTable 中；`object-list` 从内存有序 map 扫描内部 object namespace，size 从值长度派生。它不是独立 metadata service + data nodes 的分布式实现。
- object PUT 的应用提交顺序沿用 WAL：完整记录写入并 `fdatasync` 后更新内存/返回成功。若同步出错或调用结果未送达，结果可能不确定；重新打开 DB 查询，而不是武断认定对象一定不存在。
- 一个请求期间 DB 取独占非等待锁；若另一个 MiniStore CLI 同时持锁，新请求返回锁错误。不是 FIFO 队列、并发读写隔离或多 key transaction。
- API 不创建分片 session，也没有 partial upload staging；超过 1 MiB 在记录写入前拒绝。AWS S3 multipart 或某个 RGW 的行为不能由本地实验推断。
- `object-list` 只是有限前缀过滤、没有分页/快照 token；对象 key 限制刻意比 S3 窄。`/` 不被接受，不存在目录/delimiter 语义。
- 内部名字 `@obj:<bucket>:<key>` 使用 KV 用户 key 不允许的 `@`，普通 KV key（包括 `obj-...`）不会碰撞；对象 payload 和通用 KV value 共用资源限制。

## 5. 可选：RGW 与 AWS CLI 互操作，不是必做依赖

有独立本机 Ceph RGW 选修环境时，可按 [M08-L02](../../modules/M08-对象存储服务/M08-L02-S3协议、分片上传与权限.md) 对已选 operation 做小规模互操作。课程候选是 Ceph v20.2.4 / RGW + AWS CLI v2 2.37.11；版本矩阵声明这些需要安装组合 smoke test，不表示本 Lab 已验证。只用 `127.0.0.1`/隔离虚拟网段上的 endpoint、专用测试用户和测试 bucket；endpoint 端口按本机 RGW 实际配置核实。

- 不需要 AWS S3 账号、云 bucket、付费资源或公网 endpoint；不要为了完成 Lab 创建云资源。
- AWS CLI 配置/凭据放在专用临时位置并限制为 `0600`；只用本地 RGW 密钥，测试完核对并清理仅由自己创建的 bucket/object。
- 若没有 RGW、CLI 或足够资源，跳过选修部分；项目自身自动测试是完整必做验收。
- RGW 通过一组 CLI 请求，仅证明该目标、版本、配置和操作范围内观察到的兼容性；不证明 AWS S3 产品承诺、全套 S3 API、生产持久性或高可用。

## 提交物与复盘问题

完成 Lab 后提交：

1. `make clean test` 的输出或 CI 结果，记录编译器版本和 OS；不必上传临时对象数据。
2. MiniStore API 契约表（bucket/key 范围、payload 上限、错误码、覆盖、delete、list、并发/持久性边界）。
3. 一张对象/metadata/data path 图，并明确图中哪些是 MiniStore 的实际实现、哪些是一般对象服务设计。
4. 回答：上传命令返回错误但重开后 GET 到新内容时如何解释？锁冲突是否意味着请求被队列化？为什么不能以本 Lab 通过作为 SigV4/多段上传验证？
5. 如果选做 RGW，另附目标 Ceph/CLI 版本、endpoint（不附密钥）、操作列表、实际观察结果与未测语义；没运行则标注 N/A。

课文导航：[M08 模块总览](../../modules/M08-对象存储服务/README.md) · [M08-L01 对象 API/元数据/一致性](../../modules/M08-对象存储服务/M08-L01-对象API、元数据与一致性.md) · [M08-L02 协议/分片/权限](../../modules/M08-对象存储服务/M08-L02-S3协议、分片上传与权限.md)。
