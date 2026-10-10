# Lab 13：本地日志、合成指标与故障诊断

本实验在 `127.0.0.1` 上启动一个短命 Python HTTP 服务，由本机客户端发送合成存储请求。它输出结构化 JSONL 日志、有限 Prometheus exposition、关联 trace、health 和诊断报告；用确定性场景练习错误、尾延迟、容量、RPO/RTO 与遥测缺口。

## 安全与行为边界

- 仅依赖 Python 3.10+ 标准库；HTTP 服务显式绑定 `127.0.0.1` 的随机临时端口，HTTP 客户端禁用环境代理。没有监听 `0.0.0.0`、没有外网/SaaS/云请求。
- 程序只在本机 `/tmp/storage-lab13-*` 创建 mode 0700 的私有新目录，写入 mode 0600 的本课输出文件；不接受文件/设备/目标目录参数，不打开块设备或用户数据。程序不会自动删除输出，便于检查；确认路径属于本次运行后再手动清理。
- localhost HTTP 往返延迟和日志是实际本机观测；请求后端、容量、metrics、备份点与 restore rehearsal 是合成值。没有真实数据库、块设备、Kubernetes、Ceph、备份或恢复动作。
- exporter 的 label 集合有固定上界，不包含 object key/request ID/trace ID。request/trace ID 只在本地日志/trace 文件中用于关联。生产系统仍需隐私过滤、保留期限和访问控制。
- `metrics-gap` 故意不导出请求 counters/histogram 并标记 snapshot 不完整。缺失系列代表 **unknown / telemetry gap**，不是零请求。
- Prometheus 解析器只实现本 demo 输出格式子集，不是通用 Prometheus/OpenMetrics parser。p95 结果只供诊断练习，不是 M12 性能基准。

## 运行自测

```sh
cd labs/lab-13-local-observability
python3 -m unittest -v test_observability_demo.py
```

9 项标准库测试覆盖：Prometheus parser 的 label/科学计数/坏样本/重复序列；localhost JSONL、metrics、trace 关联；高尾延迟；容量高水位；过期备份/RPO；恢复演练/RTO 和新鲜度；错误率与 readiness；metrics 缺失时的 unknown 语义；有限标签基数。

## 运行与读取产物

```sh
python3 observability_demo.py --scenario healthy --requests 24 --seed 20261013
```

CLI 输出 JSON，包括 loopback endpoint、scenario、请求数、分析和唯一 `artifact_dir`。服务在报告前关闭。产物：

| 文件 | 内容 |
|---|---|
| `localhost.jsonl` | 每个 localhost 请求一行 JSON：operation/status/duration、peer IP、trace/span/parent span ID；不记 object key |
| `synthetic.prom` | 低基数 counters/histogram、live/ready、capacity、备份时间、RPO/RTO 和最近恢复演练数据 |
| `traces.jsonl` | 每次请求一条 client parent span + server child span，使用相同 trace ID |
| `health.json` | 最终合成 liveness/readiness 状态 |
| `analysis.json` | metrics/log/trace 交叉检查、p50/p95/p99、容量/RPO/RTO findings 和边界说明 |

先用 CLI 输出中的确切路径检查内容，例如：

```sh
OUT=/tmp/storage-lab13-填写本次运行输出的目录名
ls -l -- "$OUT"
python3 -m json.tool "$OUT/analysis.json"
head -n 3 "$OUT/localhost.jsonl"
head -n 20 "$OUT/synthetic.prom"
```

确认 `$OUT` 是本次生成的 `storage-lab13-*` 目录后，实验结束才运行 `rm -r -- "$OUT"`。不要复制通配符清理命令或替换成用户数据目录。

## 故障诊断矩阵

分别运行以下场景（每次运行产生独立目录），把 JSON `analysis.findings` 与数值证据保存到报告：

```sh
for scenario in latency capacity backup errors metrics-gap; do
  python3 observability_demo.py --scenario "$scenario" --requests 24 --seed 20261013
done
```

| 场景 | 预期证据/告警 | 诊断限制 |
|---|---|---|
| `healthy` | 日志、counter 与 trace span 数匹配；延迟/容量/恢复模型在目标内 | 只是模型内健康，不是实际存储健康证明 |
| `latency` | 每 5 个请求有一次额外合成后端等待，p95 高于预算且尾部拉开 | 无 CPU/设备/队列观测，不能直接归因设备或 Ceph |
| `capacity` | pool 使用率 96%，容量高水位告警；live/ready 仍为 true | 要进一步检查增长、快照、quota、池 raw/usable 与扩容时间 |
| `backup` | backup age 大于 1h RPO；restore rehearsal 超过 30m RTO 且超过新鲜度窗口 | 不做真实备份/restore；时间值不证明目标数据可恢复 |
| `errors` | 周期性 HTTP 503、错误率告警、process live 但 ready=false | 不能用重启掩盖依赖故障；health threshold 仅为教学值 |
| `metrics-gap` | 日志记录了请求，但 request metrics 缺失/标记 incomplete；计数显示 null/unknown | 不能把空指标当零流量；检查 exporter/scrape/采集队列/过滤和日志丢失 |

### 学生诊断提交模板

```text
场景与 seed：
直接观察到的日志/metrics/trace 事实：
模型给出的 finding 和数值：
最可能原因（至少一个替代假设）：
还缺少的生产证据：
安全的下一步只读检查：
为什么不能据此断言真实设备性能/数据丢失/备份可恢复：
```

至少提交 healthy、metrics-gap 和任意两个故障场景，解释日志、指标、trace 三者可能分别丢什么。指出为什么 trace ID 适合做关联字段但会造成高基数 label；将数据缺失标为 unknown，而不是健康或 0。

## 扩展题：CSI / Ceph 故障状态图（不要求集群）

将故障场景映射到 PVC Pending、Pod FailedMount、CSI controller/node plugin、Ceph MON/OSD/PG、OSD nearfull 和应用重试等层，画出控制/数据路径并列出每层只读证据。然后画出 VolumeSnapshot → VolumeSnapshotContent → 新 PVC → 新 Pod 的恢复流程，标出 `readyToUse` 不能证明的内容、应用一致性步骤、独立备份故障域和 RPO/RTO 实测证据。不得对真实集群执行更改/删除命令。

若自愿运行集群，唯一候选版本组合和虚拟盘隔离要求见[课程版本环境](../../课程大纲/版本与实验环境.md)及[M13-L02](../../modules/M13-云原生运维与可观测性/M13-L02-容器CSI卷生命周期与Ceph运维.md)。需要先在专用 VM/虚拟盘完成验证；单节点集群不能证明 HA。
