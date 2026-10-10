# Lab 00：本地环境自检与存储需求卡

本实验配套 [M00-L02](../../modules/M00-课程导览/M00-L02-工作负载、SLO、故障模型与需求卡.md)，练习将抽象的存储愿望写成可审阅的工作负载、SLO、故障模型和待确认问题。它**不**访问存储设备、不运行 IO 基准、不连接服务，也不推荐某个产品。

## 安全边界

- `m00_preflight.py check` 仅读取 Python/OS/内核信息，并通过 PATH 检查 gcc、make、cmake、git、fio 是否存在；不执行这些工具，不写文件、不安装软件、不读取设备、不挂载、不联网、不提权。
- `m00_preflight.py validate FILE` 只读取现有 JSON 普通文件；拒绝符号链接、非普通文件和超过 1 MiB 的文件，不执行文件内容、不改写输入。
- 自动测试只在系统临时目录创建测试文件并由测试框架清理。手工练习要求将示例复制到新建的随机临时目录，再编辑副本；结束时仅删除该目录。
- 不要把临时路径替换为 `$HOME`、系统目录、已有数据目录或设备路径；不需要 `sudo`、容器、虚拟机或云账号。
- 样例需求是虚构数据。请勿把真实敏感业务信息写入并提交仓库。

## 环境要求

- Python **3.10 或更新版本**，仅使用标准库；没有 pip 依赖。
- 运行课程其它模块时可能另需 GCC、make、fio 或固定内核；M00 自检只报告工具存在情况，不验证那些工具版本或兼容性。
- Ubuntu Server 24.04.5 amd64 / GA Linux 6.8.0-146.146 是课程版本文档中的**候选主线**，不是本实验已完成集成验证的结论；也不是完成本课的必要条件。

## 只读自检与样例校验

在本目录运行：

```sh
python3 m00_preflight.py check
python3 m00_preflight.py check --json
python3 m00_preflight.py validate requirement-card.example.json
```

自检显示 OS 标识、架构、内核 release、Python 版本和常用工具是否在 PATH。Ubuntu 24.04 系列与 amd64 的对照只作候选基线提示；`uname` 不能证明内核包的完整修订版本，工具路径存在也不代表版本合适。输出不含性能测量或兼容性认证。

样例卡成功时输出类似：

```text
VALID: "示例：内部图片缩略图服务（虚构需求）"
Schema and consistency checks passed; feasibility, cost, performance, and SLO attainment are not evaluated.
```

## 需求卡格式

样例文件 [`requirement-card.example.json`](requirement-card.example.json) 是可复制的 schema v1 示例。字段和约束如下：

| 字段 | 内容/约束 |
|---|---|
| `service` | 服务/练习场景说明，非空 |
| `data` | 初始容量 GiB（大于 0）、每日增长 GiB（不小于 0）、保留天数（正整数）、敏感级别说明 |
| `workload` | `read_percent` 和 `write_percent` 是按操作数统计的整数百分比且合计 100；每次代表请求字节、工作集 GiB、峰值 ops/s、并发和突发秒数均为正；`access_pattern` 为 `sequential`、`random` 或 `mixed` |
| `slo` | p99 毫秒、目标 ops/s、可用性百分比 0–100 和正的测量窗口秒数。目标定义/测量端点由作者补充；通过 schema 不表示目标合理 |
| `semantics` | 非空的一致性/持久性文字，以及非负整数 RPO/RTO 秒数。它们是目标与假设，不是已验证承诺 |
| `failure_model` | `in_scope`、`out_of_scope`、`assumptions` 均需含非空文字，明确覆盖范围及假设 |
| `constraints` | 部署边界、预算/成本、运维责任的非空说明，以及至少一项安全/合规约束 |
| `candidate_options` | 至少两个候选；每项写名称、适用点和风险 |
| `open_questions` | 未知事项列表；可为空，但建议尽量把未经验证的假设列在这里 |

验证器还检查 JSON 类型、数字有限性、百分比范围和 read/write 合计。它不是完整 JSON Schema 实现，也不判断容量/预算是否现实、百分位目标是否可达、方案是否符合安全法规、某产品是否实现所写语义或故障目标能否达到。

## 自己填写一张卡

```sh
set -eu
TMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/m00-card.XXXXXX")
trap 'rm -rf -- "$TMP_DIR"' EXIT
trap 'exit 1' HUP INT TERM
cp requirement-card.example.json "$TMP_DIR/card.json"
"${EDITOR:-vi}" "$TMP_DIR/card.json"
python3 m00_preflight.py validate "$TMP_DIR/card.json"
```

把样例中虚构服务替换为一个不含真实敏感信息的练习场景，修改工作负载、测量窗口、故障纳入/排除项、RPO/RTO 与两个候选项。`cp` 目标位于本次新建的空目录；编辑器只操作该临时副本。想练习验证器失败路径，可暂时把读写百分比改成不合计 100 的值，看到非零退出后再改回有效值。

结束后只会删除 `mktemp` 创建的 `$TMP_DIR`。若命令中断，确认 `TMP_DIR` 指向本次运行生成的 `/tmp/m00-card.*` 目录后再手动清理；不要运行宽泛的 `rm -rf` 或替换成其他路径。

## 自动测试

在本目录执行：

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest -v test_m00_preflight.py
```

测试覆盖：有效样例、schema 版本类型、读写比例、越界百分比/零时延、非有限数值/错误类型拒绝、故障项与至少两个候选的要求、非法 schema 文件失败且输入字节不变、符号链接拒绝，以及 JSON 形式的环境自检在调用目录不创建文件。用例可能在操作系统临时目录生成短暂测试文件，正常结束由 `TemporaryDirectory` 清理。

预期结尾为 `Ran 9 tests` 和 `OK`。这些是需求卡工具的单元测试，不是课程候选 OS、产品性能或数据持久性的测试。

## 文件索引

- `m00_preflight.py`：只读主机 inventory、需求卡 JSON 校验器和命令行入口。
- `requirement-card.example.json`：虚构样例需求卡，勿直接填入敏感数据。
- `test_m00_preflight.py`：Python `unittest` 自动化测试。
- [M00-L01](../../modules/M00-课程导览/M00-L01-存储全景与端到端数据路径.md) · [M00-L02](../../modules/M00-课程导览/M00-L02-工作负载、SLO、故障模型与需求卡.md) · [M00 模块总览](../../modules/M00-课程导览/README.md)。
