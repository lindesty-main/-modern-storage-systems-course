# M03：文件系统与 FUSE

## 模块目标

沿着 M02 的 POSIX I/O 继续向下理解文件系统接口：掌握 VFS、superblock、inode、dentry 与打开文件对象的关系，区分路径缓存、元数据缓存、页缓存与持久化格式；进一步通过 FUSE 理解如何在用户态实现一个文件系统，并在无特权的纯内存核心中验证只读/可写语义、偏移、错误和生命周期。

本模块的 FUSE 原型只有根目录和 `/hello.txt`，容量固定为 8192 字节，不是生产文件系统，不实现创建/删除、磁盘格式、WAL、崩溃恢复、网络一致性或耐久承诺。

## 前置知识与安全边界

- 完成 M00–M02，理解路径、fd、POSIX read/write、页缓存和 `fsync` 边界；熟悉 C11 与 Linux shell。
- 必做核心测试只使用进程内内存状态，无 root、libfuse3、设备节点、裸盘或外网依赖。
- 可选 libfuse3 实际挂载只在脚本自建的私有临时目录进行；需当前用户可用的 `/dev/fuse` 和允许 FUSE 的内核/容器策略，不用 root，不访问物理/块设备。FUSE 挂载本身必须通过 `/dev/fuse`，若不能接受任何设备节点访问，只做必做核心测试。
- 挂载失败/策略限制会显示 SKIP，不以提权绕过。可选测试在退出时卸载并清理临时目录。

## 课次

| 课次 | 主题 | 产出 |
|---|---|---|
| [M03-L01：VFS、inode/dentry 与持久化边界](M03-L01-VFS、inode-dentry与持久化边界.md) | 路径解析、VFS 对象、lookup/open/read/write 数据路径、页缓存/元数据与持久化、fsync 与目录项边界 | 能解释名字、对象身份、打开实例和磁盘格式差异；正确画出持久化层次 |
| [M03-L02：FUSE 用户态文件系统设计与实现](M03-L02-FUSE用户态文件系统设计与实现.md) | 内核/user daemon 请求链、`getattr/readdir/open/read/write`、错误、缓存/并发、挂载生命周期、安全/能力限制、旧操作复核 | 能实现受限只读/内存文件系统并测试 offset、EOF、truncate、append 和容量错误 |

## 实验

[Lab 04：VFS 与 FUSE 内存文件系统](../../labs/lab-04-vfs-fuse/README.md) 默认提供无需 FUSE 的 C11 核心程序与断言测试：

```sh
make -C 'labs/lab-04-vfs-fuse' clean all test
```

可选 high-level libfuse3 示例通过 `make fuse` 构建、`make test-fuse` 在脚本临时目录挂载两种模式。当前开发环境的 API 验证为 libfuse3 3.18.3；课程候选 libfuse3 3.14.0 未验证，详见实验 README。可选 mount 受限不影响必做验收。

## 完成标准

- 能区分 VFS 运行时 superblock 与磁盘 superblock；能区分 inode、dentry、目录内容、`struct file` 和 per-process fd。
- 能叙述 pathname lookup、缓存命中/未命中、open file 建立、数据 read/write 与设备/后端请求的端到端关系；不把系统调用等同于一次物理 I/O。
- 能解释 write 返回、页缓存变脏、文件系统同步、设备确认与重启后恢复的区别；知道新建/rename 后同步父目录的用途及限制。
- 核心 API 返回负 errno，检查路径、mode、offset、EOF、容量和整数边界；洞明确清零，`st_size` 与数据保持一致。
- 能解释 FUSE 缓存会减少 callback 次数；动态内容/身份相关内容需要明确缓存一致性和隔离策略。
- 能描述以普通用户挂载、前台诊断、正常卸载与失败清理流程；不需要 root，也不访问裸盘/块设备。
- 能检查缓冲区边界、`sample_size`、readdir offset、水印偏移及 libfuse 版本 API。
- 通过 [Lab 04 核心测试](../../labs/lab-04-vfs-fuse/README.md)；真实 libfuse3 mount 是可选的，并按本机依赖/策略如实记录 PASS 或 SKIP。

## 导航与参考

- 前置：[M02 Linux I/O](../M02-LinuxIO/README.md)；后续：[M04 用户态存储与 SPDK（扩展/选修）](../M04-用户态存储与SPDK/README.md)（两课正文及无设备模型实验已完成）。
- 课程规划：[M03 课程定位](../../课程规划.md)；课程地图：[M03 所处路径](../../课程大纲/课程地图.md)。
- 安全检查与验证边界见两篇课文的参考资料章节；默认实验使用课程内的内存核心，不依赖外部代码。
