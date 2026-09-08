# ROADMAP

本文件是唯一的路线图。每个 milestone 对应 GitHub 上的同名 milestone，每一行对应一个 issue。改计划先改 issue，再同步这里。设计细节在 `DESIGN.md`。

## 一句话

把 LLM-service-manager 做成 llama-swap 之上的控制面：**闲时多占显存、有压力就让、默认模型永远可用、用户能用 `free` / `pin` / `reserve` 说话，并有一个 Claude Code 风格的终端 UI**。

## 原则

- llama-swap 负责路由、排队、sleep/wake，不重做。
- 算法只处理"放得下就放、放不下按代价腾位"，其余靠人显式声明意图，不做预测。
- 每个 milestone 结束都是一个可以停留的稳定状态；M1 结束前线上行为不变。
- 每一步先在验证端口跑，再切生产，可回滚。

## 里程碑

| Milestone | 截止 | 交付 | 线上行为变化 |
|---|---|---|---|
| M0 工程基建 | 09-09 | 规范、模板、CI、路线图 | 无 |
| M1 只读可观测 | 09-11 | scheduler 只读采集、`/v1/state`、`llm status` | 无，只多一个只读服务 |
| M2 主动休眠与保护 | 09-14 | `free` `wake` `pin` `reserve`；内存预算取代 2 小时硬停 | reaper 下线；用户可主动释放 |
| M3 放置与压力调度 | 09-21 | 放置算法、压力驱动 sleep、等待代替失败 | vllm-launch 变薄客户端；不再无效驱逐 |
| M4 临时模型 | 09-28 | `add` `rm`、安静时刻改配置、LoRA 结论 | fine-tune 模型可自助登记 |
| M5 全屏 TUI | 10-05 | textual 应用：面板、事件流、命令行 | 无 |
| M6 收尾 | 10-12 | README、下线 legacy、用量统计 | 旧代理彻底退役 |

日期是目标，不是承诺。M1 到 M2 排在第一个周末前，因为多人同时用的情况最可能在周末出现。

## Issue 清单

### M0 工程基建

- #1 建立协作规范：AGENTS.md、PR/issue 模板、CI
- #2 开启 main 分支保护（需要仓库 admin）
- #3 决定新包名与目录布局，冻结 legacy 代码

### M1 只读可观测

- #4 状态采集器：GPU、vllm-* unit、vLLM 睡眠态、llama-swap 状态
- #5 活动统计读取：按模型与来源 IP 聚合，IP 映射容器名
- #6 scheduler 守护进程骨架与 `GET /v1/state`
- #7 `llm status`：单文件、仅标准库的一屏状态
- #8 部署脚本：安装到 llmsvc，与现有 vllm-launch/reaper 共存

### M2 主动休眠与保护

- #9 `llm free`
- #10 `llm wake`
- #11 pin / unpin 与 reserve。为了让 pin 在 M2 就能验收，本 issue 包含**最小保护接入**：llama-swap 的 `globalTTL` 设 0，scheduler 先实现一个固定 10 分钟的空闲 sleep（与今天行为等价）并对 pin 免疫；旧 `vllm-launch` 加一行"跳过 pin 住的 unit"。reserve 在 M2 验收记账与挪走睡着的模型；reserve 对放置的影响在 #14 验收（`/v1/place` 到 M3 才接管冷启动）
- #12 内存预算取代 2 小时硬停；吸收 vllm-reaper（停 keep_value 最低的；默认模型与 pin 的准入失败保持 awake 并回报阻塞）
- #13 调整 concurrencyLimit，消除批处理 429

### M3 放置与压力调度

- #14 vllm-launch 改为薄客户端：`POST /v1/place` 与租约协议（含 reserve 期间不选该卡的验收）
- #15 放置算法：先判可行、每卡最小代价腾位集合、保护默认/pin/在用
- #16 共享卡压力驱动 sleep：外部进程检测与分卡 TTL（把 #11 的固定 10 分钟 sleep 升级为分卡 TTL 加压力信号）
- #17 放不下时等待最多 2 分钟，错误说明占用者
- #18 策略回放测试夹具

### M4 临时模型

- #19 `llm add / rm`
- #20 安静时刻改配置
- #21 调研 LoRA 路径

### M5 全屏 TUI

- #22 textual 应用骨架
- #23 事件流面板
- #24 命令行内执行操作并即时刷新
- #25 usage 视图

### M6 收尾

- #26 README 重写
- #27 下线 legacy 代码
- #28 停掉其他容器里的旧 vLLM 服务并公告

## 依赖关系

```
#3 ─┬─ #4 ─┬─ #6 ─┬─ #7 ─ #8
    │      └─ #5 ─┘        │
    │                      ├─ #9 #10 #11 #12 ──┬─ #14 ─ #15 ─ #17
    │                      │                   └─ #16
    │                      └─ #18（与 #15 #16 同步补夹具）
    └─ #19 ─ #20 ; #21 独立
#22 ─ #23 ─ #24 ─ #25（依赖 #6 与 M2 的 API）
#26 #27 #28 在 M5 后
```

## 待定问题（阻塞项）

| 问题 | 影响 | 谁定 |
|---|---|---|
| fine-tune 是 LoRA 还是完整权重 | #19 / #21 走哪条路 | 有 fine-tune 需求的同学 |
| 共享卡外部占用阈值 | #16 | 用 M1 记录的一周数据定 |
| 内存预算 200 GB、宿主下限 150 GB | #12 | 与内存大户确认 |
| 分支保护 | #2 | 仓库所有者 |

## 并行执行与验收（#30）

本节补充执行分工与验收顺序；上面的 milestone 日期、issue 范围和 `DESIGN.md` 的产品决策继续有效。每个 issue 分别记录「代码就绪」「审核合入」「环境验收」的证据，PR 存在或合入不代表 milestone 完成。未满足全部验收的跨任务 issue 保持打开，部分交付 PR 使用 `Refs #N`，最后满足完整验收时才使用 `Closes #N`。

### 责任归属与交接

每个 issue 只有一个验收牵头任务；协作任务交付其拥有的模块，牵头任务汇总证据，integration 核对完整验收。

| 任务 | 牵头 issue | 文件责任与协作边界 |
|---|---|---|
| core | #3、#6、#9–#12、#14、#17 | `llmsvc/` 的包入口、配置、状态、存储、HTTP、scheduler、动作与租约模块；`pyproject.toml`、CI 与核心测试。发布统一 dataclass/JSON 契约；接入 policy、telemetry、registry 的接口。#9–#12 由 client 提供命令，policy 提供纯策略，ops 提供切换验证；#14/#17 接入 policy 放置逻辑与 ops launcher。 |
| telemetry | #4、#5 | `llmsvc/collectors/`、`llmsvc/activity.py`、采集/活动/usage 测试及对应夹具；协作 #25 用量后端、#18 脱敏现场快照。向 core 提供状态与故障信号。 |
| policy | #15、#16、#18 | `llmsvc/policy/`、策略测试与回放夹具；协作 #9/#11/#12 纯策略和 #14/#17 放置决策。输入 GPU、模型、活动数据，输出动作，不执行 I/O；#16 由 ops 提供一周观察证据，#18 由 telemetry 提供现场来源。 |
| client | #7、#22–#25 | `cli/`、`tui/`、客户端测试和 `docs/CLI.md`；协作 #9–#11/#19 命令。#25 汇总 telemetry 后端、core HTTP 接入和 usage 视图验收；包元数据修改交给 core。 |
| registry | #19–#21 | `llmsvc/registry.py`、`llmsvc/reload.py`、对应测试及 `docs/LORA.md`；core 接入 HTTP 与全局动作锁，client 提供 add/rm，ops 执行获准的短测。 |
| ops | #2、#8、#13、#26–#28 | `deploy/`、部署测试、`docs/OPERATIONS.md`，以及 #26 的 README/发布说明；协作 #11/#12 TTL 与 reaper 切换、#14 薄 launcher、各项现场验收。#2 管理员操作仍由仓库所有者完成。 |
| integration | #30 | 本路线图的执行记录、依赖协调、review 反馈派发、集成验证与合并；不代改其他任务拥有的模块。 |

交接必须明确记录，避免双写：

- #3 的 CLI 占位与 README legacy 提示由 core 一次性建立；随后 `cli/` 交给 client，README 后续补充（含 #7 状态示例）与 #26 重写交给 ops。公共接口与包元数据始终由 core 发布，其他任务消费契约，不另建同名状态结构。
- telemetry 提供 `DESIGN.md` §2 的三种失败信号；core 负责故障判定、清理/恢复、保留 pin 记录；policy 保证默认模型只放独占 GPU。故障清理与自动策略驱逐分别验证。
- #27 退役门槛满足后，ops 才接管删除 `vllm_service/`、`tools/dashboard.py`、`config/server.yaml` 及对应 legacy 测试；core 同步调整包元数据与 CI。此前这些路径保持冻结。

### 执行波次

| 波次 | 可并行工作 | 集成前置与停留条件 |
|---|---|---|
| M0 | core 完成 #3，ops 准备 #2；其余任务可做只读取证、测试场景和 #21 调研 | 先发布可安装的 Python 3.10 骨架与共享状态/JSON 契约；后续实现绑定该契约。#2 的 admin 门槛不阻止本地开发。 |
| M1 | telemetry #4/#5、core #6、client #7 | ops #8 消费集成后的只读 API；部署与卸载先在隔离目录验证。保持只读，线上行为不变。 |
| M2 | policy 保护/内存策略、core 意图持久化与动作、client 命令；ops 准备 TTL/reaper/concurrency 切换及回滚 | 保护规则与 dry-run 验证通过后，按 DESIGN §7 在独立配置的验证端口完整运行一天 dry-run 并记录起止时间和结果，再进入生产动作启用门槛。短 GPU smoke 不替代这一天。 |
| M3 | policy 放置/压力/回放、core 原子租约与等待、ops 薄 launcher | 锁范围、等待与恢复后的重验遵循 DESIGN §4.2 第 5 条（#37）；#17 验证等待能被其他操作解除且保持原子记账。 |
| M4/M5 | registry/reload 与 TUI 可在稳定契约上并行开发 | registry 接入依赖全局锁、内存准入和事件/在途计数；TUI 只调用 scheduler API。LoRA 结论需版本依据与验证，完整权重登记按现有设计实施。 |
| M6 | README、回滚演练与退役证据准备 | 按原依赖在 M5 后验收；无 legacy 消费者后才删除；#28 涉及他人服务的停止需其所有者同意。 |

### 验收证据

每个 PR 都运行针对性测试、`python -m pytest -q tests`、`git diff --check`，并通过当前提交的 Python 3.10 CI；包安装与独立 CLI smoke 按改动范围执行。保留 legacy 测试至 #27 退役验收。策略改动附回放，合成回归场景与真实历史来源分别标注；mock 不代替现场数据，等待时间不代替实际观察。

| 范围 | 必须提供的证据 |
|---|---|
| M1 | 探针失败为 unknown；整轮采集 <2 秒；超过 2.1 万行 SQLite 聚合 <100 ms；state/CLI 一致；隔离安装/卸载通过。 |
| M2 | dry-run 不调用写操作、不持久化意图或租约；pin 经重启/TTL/free 仍受保护且到期有效；默认模型不被策略硬停；普通/pin/默认模型的 sleep 前内存准入及阻塞结果；完整一天 dry-run 记录。 |
| M3 | 至少六个回放场景总计 <5 秒；无可行放置时零驱逐；默认/pin/在途保护；sleeping 与租约保留全额预算；并发不重复分配、confirm 幂等、stale unit 保留预算、重启按模型去重、reserve 排除 GPU；等待可由其他操作解除且最多 120 秒。 |
| 故障恢复 | unit 退出、ready 但连续 10 秒仍 sleeping、health 连续失败三类用例；保留 pin，确认资源消失才释放无效记账；下一次请求可在可行 GPU 重新放置；默认模型仅使用独占 GPU。 |
| M4 | 路径校验、名称/端口唯一、到期尊重活动与保护；配置先校验；连续 5 秒零在途、awake pin 阻塞、整批 RAM 准入、10 分钟超时、原子替换与失败恢复；记录 reload 窗口残余风险及缓解实测。 |
| M5 | CLI 在无第三方依赖环境可单独运行；非 TTY 降级；100×30 与窄终端；SSE 重连；命令执行后刷新；usage 请求/token 数与源数据及数据面指标对账。 |
| M6 | 新用户按文档可操作；回滚按 DESIGN §7 顺序演练；legacy 零消费者证据；ops issue 链接对应现场验收记录。 |

### Fable 审核与合并门槛

实现与集成由 Codex / gpt-6-astra 完成，Fable 独立审核。沿用每 10 分钟运行的 Fable 审核流程，不另起重复 reviewer，也不以 Astra 自审代替批准。Fable 通过后的每次合并均绑定已核验的 head SHA；执行合并前重新读取 GitHub reviews、review threads、comments 与 checks，逐项核对：

1. Fable 审核明确标注 harness/model，并对 **PR 当前完整 head SHA** 无歧义地表示可以合并。共享账号发布的 `COMMENTED` review 可以作为证据，但沉默、旧提交批准、普通 bot 评论或作者自报通过均不算。不能核实归属或 head 时保持 `waiting_review`，列出缺失证据。
2. 当前 head 的 CI `test` 为 `SUCCESS`，没有未解决的阻塞反馈，依赖 PR 已合入。任何新提交都需重新取得 Fable 审核。
3. 使用 `gh pr merge --squash --match-head-commit SHA` 绑定已核验的提交；不 direct push main、不 force push，不用 admin/auto-review 绕过分支保护。Fable 证据不替代 GitHub 强制要求的其他成员批准；保护规则拒绝时保留待合并状态。

### 现场短测与外部门槛

GPU 空闲时的简短测试已获准，由 ops 独占调度并持有共享 `gpu-test.lock`。每次启动前重新记录进程、显存、利用率与在途请求：无外来计算进程、无会被打断的服务负载且内存充足才运行。使用已缓存的小模型、独立验证端口和唯一测试 unit，目标不超过 5 分钟；出现其他负载即中止，仅清理自己创建的资源并保存前后证据。不通过停止或休眠现有模型腾位，不为机会性短测下载大权重，不借此修改生产路由、TTL、reaper 或配置。

以下门槛保留在各自 issue，不能以代码或短测通过关闭：#2 的仓库 admin 操作；#8/#16 的真实一周观察及阈值依据；#28 的共享卡一周无外部 vLLM 常驻进程证据和相关服务所有者同意；#12 初始内存阈值的相关使用者确认；生产动作前完整一天 dry-run、已准备并验证的切换/回滚与最后授权。公开或群内公告尚未获准，#28 的公告验收继续待办。仅在这些外部门槛之外已无可推进工作时，暂停执行并逐项报告阻塞，不宣称全部 milestone 完成。

<!-- Generated-By: Claude Code / claude-fable-5-1 -->
<!-- Generated-By: Codex / gpt-6-astra -->
