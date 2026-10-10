# Lab 04：VFS 与 FUSE 内存文件系统

配套课文：[M03-L01：VFS、inode/dentry 与持久化边界](../../modules/M03-文件系统与FUSE/M03-L01-VFS、inode-dentry与持久化边界.md)、[M03-L02：FUSE 用户态文件系统设计与实现](../../modules/M03-文件系统与FUSE/M03-L02-FUSE用户态文件系统设计与实现.md)。

## 安全边界先读

- **必做核心与单测**只操作进程内的 `struct memfs_state`，不创建挂载、不读写文件、不打开 `/dev`、块设备、分区或物理硬件；不需要 `libfuse3`、root 或网络。
- **可选真实挂载**通过 libfuse3 和 Linux 内核 FUSE 接口进行，只将程序挂到脚本用 `mktemp` 在 `${TMPDIR:-/tmp}` 下创建的私有临时目录；不接受任意目标路径，不加 `allow_other`，不访问裸盘/块设备/其他设备，也不需要 root。FUSE 内核接口本身必须使用 `/dev/fuse`；若“完全不访问任何设备节点”是硬性要求，请不要运行这个可选挂载目标，只运行核心测试。
- 可选脚本检测自己是否为 root（是则 SKIP），挂载进程以 `-f -s` 前台单线程运行，并在正常退出、失败或常见信号时尝试卸载、停止进程及删除临时目录。不要对真实数据目录或手工指定的挂载点运行，也不要用 `sudo` 绕过拒绝挂载的策略。
- 内存版重启即丢失全部写入；该程序不是持久文件系统，不支持创建/删除文件、符号链接、硬链接、扩展属性、锁、`fsync` 持久化或并发一致性承诺。

## 环境与必做测试

必做部分需要 C11 编译器和 GNU make：

```sh
make clean all test
```

成功输出：

```text
memfs core tests: PASS
```

测试覆盖只读/内存模式、目录与普通文件属性、精确路径匹配、`open` 标志、偏移读写、EOF、空洞补零、覆盖与 `O_APPEND`、truncate、固定容量部分写入/ENOSPC、目录错误和无效参数。编译采用 `-Wall -Wextra -Wpedantic -Werror`；程序与测试都不依赖 FUSE API。

## 可选 libfuse3 构建和真实挂载

只有在 Linux 上有可用的 libfuse3 开发头文件/库、`pkg-config`、`fusermount3`、`mountpoint`、可访问的 `/dev/fuse`，且当前用户的挂载策略允许时才运行：

```sh
pkg-config --modversion fuse3
make fuse
make test-fuse
```

示例使用 `FUSE_USE_VERSION 31` 和 libfuse3 high-level API。程序支持两种模式：

```text
fuse-memfs --readonly [FUSE options] MOUNTPOINT
fuse-memfs --memory   [FUSE options] MOUNTPOINT
```

`test-fuse.sh` 会在私有临时目录中先挂载内存版，验证 `ls`、初始文件、覆盖写、追加、读回及卸载；再挂只读版，验证内容与拒绝写入。任何挂载失败/中断都走退出清理；若当前用户不允许访问 `/dev/fuse` 或沙箱禁止挂载，显示 `SKIP`，不提权重试。手工探索也只能用 `mktemp -d` 建临时 mountpoint、前台 `-f -s` 启动，并通过 `fusermount3 -u` 卸载，随后删除自己创建的目录。

**API 验证状态：**本次开发环境的 `pkg-config fuse3` 返回 **3.18.3**；本地已用该版本严格编译，并由 `make test-fuse` 完成两种模式的真实无 root 临时挂载/卸载测试。课程环境表中的 `libfuse3-dev 3.14.0-5build1` 是候选版本，本机不是该候选包，故**3.14.0 组合未验证**。其他发行版、内核、mount namespace 与容器策略需自行记录实际结果。若 libfuse3 不可用，默认核心构建/测试仍完整可用，不应把可选 API 状态写成已通过。

## 实现索引

- `memfs_core.h` / `memfs_core.c`：无外部依赖的路径查找、属性、open、偏移读写核心。接口成功返回字节数/0，错误返回负 errno；调用方须序列化访问。写到固定容量末尾时，核心按 POSIX 风格可返回短写；没有空间写入时返回 `-ENOSPC`。
- `test_memfs_core.c`：无需挂载或设备访问的断言式单元测试。
- `fuse_memfs.c`：可选 libfuse3 high-level 适配层，将 callback 错误码、`fi->flags`、路径和偏移交给核心；互斥锁保护共享状态。为遵循 high-level write callback 的完整请求约定，超出剩余容量的单次 FUSE 写入会先检查并返回 `-ENOSPC`，不把核心 helper 的短写直接伪装成完整 FUSE 写成功。
- `test.sh` / `test-fuse.sh`：核心测试与临时挂载测试；后者在缺少能力時明确 SKIP。
- `Makefile`：默认 `all`/`test` 不查找 FUSE 依赖；`fuse` 与 `test-fuse` 是显式可选目标；`clean` 只删除本目录生成的二进制。

清理构建产物：

```sh
make clean
```
