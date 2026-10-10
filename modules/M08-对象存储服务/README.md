# M08：对象存储服务

## 模块目标

从本地 KV 引擎转向对象 API：理解 bucket/key 与对象 payload，分清控制面、元数据路径和数据面；读懂 S3 风格 REST API 的兼容边界、认证/授权和大对象分片上传生命周期；能比较 AWS S3 产品承诺、Ceph RGW 的某版本接口实现、FastDFS 专用文件服务和 MiniStore 教学子集，而不把它们统称为“同一种对象存储”。

M08 将一个明确有限的**本地对象 API 子集**加入 MiniStore。它沿用本地 WAL、恢复和单进程独占锁，仅提供 bucket/key、整对象 put/get/delete、带前缀的排序 list；它不是网络服务，不宣称实现 S3 REST、AWS CLI 互操作或 SigV4。对象数据和 KV 共用存储引擎，但使用不与合法 KV key 冲突的内部名字空间。边界见 [MiniStore 项目说明](../../projects/MiniStore/README.md)。

## 学习成果

学完后能够：

1. 沿对象 PUT/GET 画出客户端、身份认证、API/gateway、元数据索引、对象数据和底层介质的路径，指出其中可分离但需协调的元数据面和数据面。
2. 解释 bucket/key 命名、对象覆盖、列举、删除、版本化和单对象/多对象事务之间的不同语义。
3. 写出 AWS S3 的产品保证与 S3 API 兼容声明的区别，并指出应按服务版本逐项验证的操作、错误和权限行为。
4. 解释 FastDFS Tracker/Storage 同组同步、图床业务数据库与对象 API 的边界；不把 FastDFS 专用协议包装成 S3。
5. 说明 multipart upload 的 upload ID、part、完成/中止和失败遗留清理；区分 HTTP `multipart/form-data` 表单上传。
6. 使用项目自带客户端测试 MiniStore 对象子集的字节传输、覆盖、缺失/删除、排序列举、锁冲突及容量错误。

## 前置与安全边界

- 前置：[M05 本地 KV 引擎](../M05-本地KV引擎/README.md)、[M07 持久性与崩溃一致性](../M07-持久性与崩溃一致性/README.md)，会用 Linux shell、文件和基本 HTTP/身份认证术语。
- 必做路径只使用 MiniStore 和自动化测试自己的临时目录；不需要 root、裸盘、云账号、AWS CLI、网络服务或外部 endpoint。
- Ceph RGW + AWS CLI 是**可选的本机互操作练习**，依赖版本矩阵中给出的组合；不连接 AWS S3。不得把真实访问密钥提交到仓库、命令历史或讲义；不得把管理端口公开到公网。
- MiniStore 的目录 mode `0700` 和本机 `flock` 是本地访问/互斥措施，不是 bucket policy、租户认证或对象授权。对象 API 数据上限 1 MiB；不要用测试命令对已有数据库或真实数据目录注入故障。

## 课次

| 课次 | 主题 | 建议时长 | 主要产出 |
|---|---|---:|---|
| [M08-L01：对象 API、元数据与一致性](M08-L01-对象API、元数据与一致性.md) | 对象与文件/KV 的差异；控制/元数据/数据面；bucket/key 与对象元数据；覆盖与列举；一致性与 AWS S3、RGW、FastDFS 的边界；MiniStore API 语义 | 110–125 分钟 | 画一条 PUT/GET 数据路径，给出一致性及 API 契约对照表；解释项目子集未实现的保证 |
| [M08-L02：S3 风格协议、分片上传与权限](M08-L02-S3协议、分片上传与权限.md) | S3 REST 与兼容矩阵；SigV4 与策略；multipart 状态机、失败清理、重试不确定性；安全本地 RGW 选修测试 | 115–130 分钟 | 绘制 multipart 状态/清理图；写最小权限与失败恢复计划；选做本机 RGW 的操作兼容记录 |

## 必做实验与自动测试

[Lab 07：MiniStore 本地对象 API](../../labs/lab-07-object-api/README.md) 使用项目随附的 Python 黑盒客户端测试，自动验证：

- 原始二进制 payload（包括 NUL）put/get/delete；空对象与缺失对象区分；重复 put 整体覆盖；bucket 隔离；对象记录经 compaction 后仍可恢复。
- 排序 `object-list`、前缀过滤、长度报告、对象记录对普通 KV `list` 的隔离；旧式合法 `obj-...` KV key 仍可用。
- 非法 bucket/key、恰好 1 MiB 成功与超过上限输入无写入、不存在的 get/delete 错误码，以及已有数据库锁时对象请求明确失败而非伪装并发支持。
- 项目所有 M05/M07 的 WAL、恢复、安全与进程级故障测试继续回归。

```sh
make -C 'projects/MiniStore' clean test
```

实验不制造 multipart 会话，因为 MiniStore 并未实现 multipart；不会因输入过大留下分片。同步错误/响应丢失的处理沿用 M07：将结果视为可能不确定，重新打开并查询，不盲目假定失败或 exactly-once。

## 完成标准

- 能把抽象概念落到一个 API 契约：输入范围、成功响应、错误码、覆盖、删除、list、并发和持久化范围全部写明。
- 能明确列出“产品保证 / 协议兼容声明 / 某版本实现 / 本地实验观察”四种证据来源，且不互相替代。
- 能解释 multipart 中已完成的部分为何在完成或中止前继续占用空间，以及 API 调用超时后为什么需查询状态而不是盲目重试。
- MiniStore 的自动测试通过；选修 RGW 测试若未运行，报告中标记“未验证”，不以 AWS 云服务替代。

## 资料衔接

- FastDFS 案例见 [M08-L01](M08-L01-对象API、元数据与一致性.md)：通过 Tracker/Storage、组内复制、HTTP 接入和业务数据库讨论专用文件服务的职责边界；其接口和运维模型不等同 S3。
- [MiniStore 本地对象 API 与测试](../../projects/MiniStore/README.md)、[实现](../../projects/MiniStore/src/ministore.cpp)、[黑盒测试](../../projects/MiniStore/tests/test_ministore.py)。
- [课程版本与实验环境](../../课程大纲/版本与实验环境.md)：主线本地测试不依赖外部产品；AWS CLI v2 与 Ceph RGW 只用于选修互操作，须按版本锁定并逐项验证。
- 后续：[M09 分布式存储与 Ceph](../README.md#单模块建议文件)；对象接口与底层集群实现分开理解。
