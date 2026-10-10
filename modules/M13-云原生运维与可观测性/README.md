# M13：云原生运维与可观测性

> 状态：两课正文与 Lab 13 本地可观测性演示/诊断练习已完成。必做仅使用绑定 `127.0.0.1` 的短命 HTTP 服务、自动创建的私有临时文件和合成指标/trace；不需要 Kubernetes、Ceph、SaaS 或云账号。

## 模块目标

- 从存储用户 SLO 设计指标、结构化日志和 trace，能关联同一请求并指出三类信号各自的丢失边界。
- 解释健康、liveness、readiness 与容量水位；区分设备/池/文件系统/租户的容量口径。
- 把 RPO/RTO 写成可检验目标，设计备份、恢复演练和证据记录；不把快照当备份或把“备份成功”当“可恢复”。
- 追踪 Kubernetes、容器运行时、kubelet、CSI、Rook、Ceph 的职责边界，讲清卷创建、调度/attach、mount、快照与恢复 PVC 的状态流转。
- 用只读状态与分层排查思路分析 Ceph 运维事件；不把单节点实验包装成 HA 证明。

## 课次与实验

| 课次 | 内容 | 产出 |
|---|---|---|
| [M13-L01：存储可观测性、健康与恢复目标](M13-L01-存储可观测性、健康与恢复目标.md) | 指标/日志/trace、存储 SLI/SLO、标签基数、遥测丢失、健康探针、容量、RPO/RTO、备份恢复 | 写出一页观测与恢复卡：指标口径/来源、阈值、信号缺失行为、RPO/RTO 和恢复证据 |
| [M13-L02：容器、CSI 卷生命周期与 Ceph 运维](M13-L02-容器CSI卷生命周期与Ceph运维.md) | 容器与 Kubernetes 控制面、CSI controller/node 职责、PVC/PV/快照/恢复边界、Ceph 运维诊断案例 | 画出从 PVC 到挂载及快照恢复的数据/控制路径；按证据分层定位一个故障 |
| [Lab 13：本地日志、合成指标与故障诊断](../../labs/lab-13-local-observability/README.md) | loopback JSONL 日志、Prometheus 文本子集、关联 trace、自检和 5 类故障场景 | 通过 9 项标准库测试；提交健康/延迟/容量/备份/遥测缺口场景的诊断与不确定性说明 |

## 必做验收

```sh
cd labs/lab-13-local-observability
python3 -m unittest -v test_observability_demo.py
python3 observability_demo.py --scenario healthy --requests 24 --seed 20261013
python3 observability_demo.py --scenario latency --requests 24 --seed 20261013
python3 observability_demo.py --scenario capacity --requests 24 --seed 20261013
python3 observability_demo.py --scenario backup --requests 24 --seed 20261013
python3 observability_demo.py --scenario errors --requests 24 --seed 20261013
python3 observability_demo.py --scenario metrics-gap --requests 24 --seed 20261013
```

将 `healthy` 和每个故障场景的 JSON `analysis.findings`、证据字段及人工判断放入报告。重点说明 `metrics-gap` 为什么不是“请求数为零”，`errors` 为什么可表现为进程仍 live 但 not ready，以及高容量警告为什么不能单独确定扩容还是清理数据。

## 可选 Kubernetes/Rook/Ceph

版本只用[课程版本矩阵](../../课程大纲/版本与实验环境.md)中的整组候选：Ubuntu Server 22.04.5 隔离 VM、Kubernetes v1.36.5、containerd v2.3.6、Flannel v0.28.9、Rook v1.21.0、Ceph CSI v3.18.1、external-snapshotter v8.6.0、Ceph Tentacle v20.2.4。版本组合和镜像 digest 在完成课程发布前的集成验证前仍是**候选**；不得仅挑一项追新或把未验证组合写成已通过。

选修练习限于隔离 VM 中的测试 namespace 与专用可丢弃虚拟盘，按固定 tag 的官方 Rook/Ceph CSI 文档安装；先核对节点、设备、CRD、镜像 digest 和销毁路径。练习 PVC 创建/挂载、创建 VolumeSnapshot、等待 readyToUse、从快照创建**新的** PVC 并校验测试文件；所有资源只用课程专属名称。不要在真实数据卷执行格式化/清理命令。单节点只能验证软件流程与接口，不能证明副本跨独立故障域、控制面高可用、CSI 故障切换或生产恢复能力。没有该环境不影响完成模块。

## 完成标准

- 能为容量、错误率和 p95/p99 选择指标，并说明单位、采样/聚合窗口、标签集合与告警动作；不使用 object/key/request/trace ID 作为 Prometheus label。
- 能从 `health` / liveness / readiness 的状态组合判断“进程活着但未就绪”，不因单一失败探针盲目重启造成级联。
- 能把 RPO（可接受的数据丢失窗口）和 RTO（恢复服务的时间目标）与备份点、恢复实测时长及测试新鲜度联系起来；知道合成值不是承诺。
- 能说明 CSI 与 Kubernetes/Ceph 各自负责什么，为什么 PVC Bound、VolumeSnapshot ready 和 Pod 挂载成功是不同状态。
- 能指出 VolumeSnapshot 通常是存储系统内的时间点副本/引用，独立于原卷和故障域的备份还需单独设计；恢复为新 PVC 仍要校验应用一致性、权限、容量和数据。
- 知道 Ceph 健康告警应先收集只读状态、判断 MON quorum/OSD/PG/容量/慢请求，再按版本与运行手册处理；不随意执行 `repair`、`purge`、擦盘或删除对象。

## 边界

Lab 13 是标准库教学模型：日志来自真实 localhost HTTP 请求，metrics、存储响应、容量、备份时间和恢复结果都是合成的；不打开磁盘/volume，不会发出真实备份、恢复、云 API 或集群操作。其 p95 只用于练习诊断，不是硬件基准。Prometheus 解析器只接受本演示输出的有限 exposition 子集，不是通用 Prometheus/OpenMetrics 实现。指标、日志、trace 可能分别丢失或被采样，任何单一信号都不构成完整审计记录。
