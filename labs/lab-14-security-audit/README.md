# Lab 14：本地权限、校验和错误路径审计

本实验以自建普通临时目录为边界，模拟一个**极简、非生产**的 tenant-scoped 本地对象记录。它验证目录/文件 mode、严格输入校验、租户范围拒绝、SHA-256 意外损坏检测、符号链接拒绝、不可覆盖创建，以及不记录原始输入的错误路径审计。目标是练习安全断言和限制说明，不是攻击服务或认证产品。

## 安全边界

- 需要 Linux/POSIX Python 3.10+ 标准库；利用目录 fd、`O_DIRECTORY`、`O_NOFOLLOW`、`dir_fd` 等本地文件操作。无需安装依赖、root、容器、云服务或网络。
- 脚本没有目标路径、设备、URL、用户目录或任意输入文件参数。每次运行只在系统临时目录中自动创建一个新 `storage-lab14-*` 目录，目录和 tenant 子目录为 `0700`、记录文件为 `0600`；退出时由 `TemporaryDirectory` 清除本次目录。
- 不访问 `/dev`、裸盘、loop、挂载点、MiniStore 数据、仓库内既有数据或任何真实服务；没有扫描、连接、写入/删除用户指定路径、联网、提权或绕过权限的步骤。
- 所有 payload 都是脚本生成的演示 bytes。JSON 结果只包含固定检查名称和错误码计数，不包含临时路径、payload、原始无效输入或异常全文。
- 输入 principal 是调用者提供的 `tenant` 标签，不是真实身份认证；同 UID/root 可绕过本地文件模式。SHA-256 是无密钥 checksum，不是 MAC、数字签名或防恶意篡改措施。本实验不认证 MiniStore 或其他系统。

## 运行

```sh
cd labs/lab-14-security-audit
python3 -m unittest -v test_security_audit.py
python3 security_audit.py
```

第二条命令输出 JSON，退出前自动删除实验目录。无需保存任何数据文件。若重定向输出，重定向目的由 shell 决定，建议仅使用自己新建的报告路径；脚本不负责清理用户指定的重定向文件。

## 实验检查和预期结果

演示报告包含 8 个断言：

| 检查 | 预期 | 它能说明什么 / 不能说明什么 |
|---|---|---|
| `private_root_and_tenant_modes` | PASS | 本次生成的目录分别为 `0700`；不隔离同 UID、root 或被攻陷的宿主 |
| `private_record_mode` | PASS | 记录为 owner-only `0600`；不提供静态加密 |
| `record_round_trip_and_checksum` | PASS | 合成数据经过编码/读取后相同，SHA-256 一致；不认证数据作者 |
| `tenant_scope_denied` | PASS | 请求主体标签与资源 tenant 不同时拒绝，且在打开记录前拒绝；不证明主体标签可信 |
| `path_traversal_rejected` | PASS | `../outside` 不是合法 tenant；不等价于对任意应用路径的全面证明 |
| `corrupt_record_rejected` | PASS | 人为改动的 digest 被检测；能同时重算无密钥 digest 的攻击者可伪造记录 |
| `symlink_rejected_without_following` | PASS | 记录以 `O_NOFOLLOW` 打开，拒绝本地符号链接；仍需按目标 OS/文件系统/并发威胁审查 |
| `overwrite_refused` | PASS | `O_EXCL` 拒绝覆盖已有演示对象；不代表真实更新事务/并发协议 |

预期错误审计计数：`ALREADY_EXISTS: 1`、`AUTHORIZATION_DENIED: 1`、`CHECKSUM_MISMATCH: 1`、`INVALID_COMPONENT: 1`、`UNSAFE_PATH: 1`。报告中的 `raw_input_logged` 和 `raw_exception_text_logged` 均为 `false`。任何检查为 FAIL 时程序返回非零状态，不应将结果写成通过。

### 已执行结果（2026-10-09）

在课程工作区的 Linux 环境使用 Python 3.14.7 执行：

```text
python3 -m unittest -v test_security_audit.py
Ran 10 tests ...
OK

python3 security_audit.py
result: PASS
checks: 8/8 PASS
expected rejection codes: 5 categories, one event each
network_used: false
device_paths_opened: false
```

这是上述本地模型/断言在当次 Python/Linux 环境的结果，不是生产审计、安全认证、通用文件系统保证或攻击防护证明。测试不依赖特定纳秒/吞吐值。

## 自动测试覆盖

10 项标准库测试检查：

1. 根/tenant/file 权限和成功 round-trip；
2. 不同 tenant 被拒绝；
3. 路径穿越/非法 key 拒绝；
4. 最大 payload 限制在写文件前生效；
5. checksum 篡改和跨租户元数据篡改拒绝（元数据异常先触发授权拒绝，避免暴露校验 oracle）；
6. malformed JSON、非对象和未知字段 fail closed；
7. symlink 文件拒绝；
8. 已存在记录不可覆盖；
9. 完整离线演示报告的 8 项检查、错误码和不记录输入断言；
10. root 本身的 symlink 拒绝（仅在 `O_NOFOLLOW`/`O_DIRECTORY` 可用的 POSIX 环境运行）。

## 复盘问题

1. 代码里哪一段是输入验证，哪一段是授权？实验何处没有认证？
2. 为什么 authorization 在读取文件前执行？改变检查顺序会暴露什么信息？
3. `0700`、`0600`、`O_NOFOLLOW`、`O_EXCL` 分别解决什么威胁，分别不能解决什么？
4. checksum 检测的攻击能力假设是什么？若攻击者能改数据和 digest，还需什么机制？
5. 真实多租户存储还需要哪些控制：身份到 tenant 映射、对象 LIST/DELETE 检查、配额、密钥、审计、备份、隔离进程或存储？
6. 列出一条把本实验扩展成生产安全评估仍缺失的证据。不要在真实服务上尝试验证。

## 代码和来源

- [实验实现 `security_audit.py`](security_audit.py)：只创建自有临时目录；CLI 不接受危险目标。
- [自动测试 `test_security_audit.py`](test_security_audit.py)：Python unittest 标准库。
- [M14 课文](../../modules/M14-安全与可靠性/M14-L01-存储威胁模型、数据保护与安全变更.md)：威胁模型、AuthN/AuthZ、租户隔离、密钥/静态数据、供应链、备份恢复、危险变更审查，以及 MiniStore 限制。
- 上游参考入口（详细题名/发布信息见课文来源表）：[Linux `open(2)`](https://man7.org/linux/man-pages/man2/open.2.html)、[Linux `openat2(2)`](https://man7.org/linux/man-pages/man2/openat2.2.html)、[Python tempfile](https://docs.python.org/3/library/tempfile.html)、[NIST SP 800-209 存储安全](https://csrc.nist.gov/pubs/sp/800/209/final)、[OWASP Authorization Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html)、[NIST SSDF SP 800-218](https://csrc.nist.gov/pubs/sp/800/218/final)。
