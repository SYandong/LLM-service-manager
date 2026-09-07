# DESIGN — llama-swap 之上的调度器与终端 UI

状态：v1 草案，2026-09-07。改动本文件前先开 `type:design` issue。

## 1. 背景与目标

线上是 llama-swap v252 + 官方 vllm-wrapper，7 个模型各自跑在 transient unit `vllm-<id>.service` 里，`--enable-sleep-mode`，组配置 `swap: false`。llama-swap 负责路由、排队、按 TTL 触发 sleep、按请求唤醒。它**没有**：多 GPU 感知、按显存驱逐、保底、用户显式意图。现在这些由两个 bash 脚本兜着：

- `vllm-launch`：冷启动时选卡，放不下就踢睡得最久的。
- `vllm-reaper`：每 5 分钟，睡满 2 小时 / 宿主内存紧 / 所在卡不够唤醒 → `systemctl stop`。

日志里坐实的问题：逐卡贪心导致无效驱逐；只看"睡得最久"，会踢掉最常用的默认模型；决策只在冷启动那一刻同步发生；`systemd-run` 竞态；2 小时硬停与压力无关，把 1 到 3 秒的唤醒变成 1 到 5 分钟的冷启动。

**目标**（按优先级）：

1. 闲时多占、有压力就让：显存空着就用，别人要用时主动退。
2. 保底：默认模型随时可用，永不被硬停。
3. 人能说话：`free` / `pin` / `reserve` 让用户声明意图，算法不做预测。
4. 看得懂：一屏看清每张卡谁在占、每个模型什么状态、谁在用。
5. deadline 期间两三个人同时用不同模型（含 fine-tune 后的模型）不互相踩。

**非目标**：鉴权与按人计费；替代 llama-swap 的路由与排队；对外提供 Web 服务。

## 2. 架构

```
用户容器                      llmsvc 容器
┌──────────┐   OpenAI API   ┌─────────────┐  cmd/cmdStop  ┌──────────────┐
│ 客户端    │ ─────────────▶ │ llama-swap  │ ────────────▶ │ vllm-wrapper │──▶ vLLM daemon
└──────────┘                └─────────────┘               └──────────────┘   (vllm-<id>.service)
┌──────────┐   HTTP :8001   ┌─────────────┐  /api/models/unload  ▲   ▲ systemctl / :81xx
│ llm CLI  │ ─────────────▶ │  scheduler  │ ─────────────────────┘   │
│ llm TUI  │                │ (本仓库)     │ ◀── POST /v1/place ── vllm-launch（薄客户端）
└──────────┘                └─────────────┘
                                  │ 只读
                                  ▼
                nvidia-smi · systemctl · vLLM /is_sleeping · llama-swap /running /api/events
                activity.sqlite（含来源 IP）
```

边界：

- **llama-swap 是数据面**，不改它的代码，运行期不改它的配置（reload 会让全部醒着的模型 sleep 并中断在途请求）。
- **scheduler 是唯一写者**：所有 sleep / stop / 放置 / 改配置都经它，一把全局锁串行化。
- **vllm-launch 退化为薄客户端**：冷启动时 `POST /v1/place`，拿到 GPU 号后 `systemd-run`。腾位由 scheduler 完成后再返回。
- scheduler 的 HTTP 只监听容器网，不做鉴权，与现有信任模型一致（能访问服务器即能用 LLM）。

## 3. 模型的三种来源

| 来源 | 登记方式 | 预算 | 生命周期 |
|---|---|---|---|
| 常驻模型 | llama-swap 配置里的固定块 | 每模型 `util` 比例（占一张卡） | 永久 |
| 临时模型（完整权重 fine-tune） | `llm add <path> --name X --base <常驻模型>` | 继承 base 的配置块与预算 | 7 天无人用自动注销 |
| LoRA 适配器 | `llm add --lora <path> --base <常驻模型>` | 不额外占显存 | 随 base |

临时模型需要改 llama-swap 配置，scheduler 把落盘排到**安静时刻**（零在途请求），reload 后醒着的模型进入 sleep，下次请求 1 到 3 秒唤醒。LoRA 路径待 M4 调研（base 开 `--enable-lora` + 运行时装载，是否仍需 reload 注册别名）。

## 4. 状态机与规则

模型三态：

| 态 | 显存 | 内存 | 恢复代价 |
|---|---|---|---|
| awake | 预算全额 | 少量 | 0 |
| sleeping | 约 2 GB 残留 | 权重大小（pinned） | 唤醒 0.5 到 3 秒 |
| stopped | 0 | 0 | 冷启动 1 到 5 分钟 |

每条边由独立规则管，规则只吃三个纯数据结构（GPU 状态、模型状态、活动统计），输出动作列表。

### 4.1 awake → sleeping

触发之一即可：

- **空闲 TTL**：独占卡（GPU0）60 分钟；共享卡 5 分钟。
- **压力**：共享卡上出现外部进程，或空闲显存低于阈值；有放置请求放不下；用户跑了 `llm free`。
- 压力下按分数排序逐个睡，直到压力解除：

```
score = idle_seconds * size_gb / (1 + requests_last_hour)
pinned 或有在途请求 → 不可睡
默认模型 → score / 10（最后才睡）
```

### 4.2 放置（stopped → awake）

1. 候选卡：GPU0 永远在；共享卡仅当其外部占用低于阈值且无 `reserve`。
2. 逐卡先判**可行性**：可用 = 总量 − 该卡所有 daemon 预算 − 外部占用；放得下直接放。
3. 都放不下：对每张卡求"最小代价腾位集合"（模型数小，直接穷举），只在代价最低且确定可行的那张卡上驱逐。代价 = 被驱逐模型的分数之和；默认模型、pin、在用的不进集合。
4. 全部在用且放不下：**等最多 2 分钟**，期间有模型空闲超过 30 秒即睡掉腾位；超时返回错误，错误里写明哪张卡被谁的什么模型占着。
5. 全程持锁；`systemd-run` 前确认同名 unit 不存在。

### 4.3 sleeping → stopped（硬停）

不再按时间。只有三条：

- **常驻内存预算**：llmsvc 所有 sleeping 模型的权重总和不超过预算（初值 200 GB）。超出停分数最高的。
- **宿主可用内存下限**（初值 150 GB）：低于则再停一个。
- **不能唤醒**：睡着的模型所在卡已经放不下它醒来。近一小时有请求的改为换卡冷启动，否则停。
- 默认模型永不停。24 小时清扫可选，默认模型除外。

### 4.4 用户意图

| 命令 | 语义 | 到期 |
|---|---|---|
| `free [--gpu N] [--ram] [--need 80G]` | 立即睡掉可睡的（无在途、空闲 >30 s）；`--ram` 则从内存清掉 sleeping 的 | 一次性 |
| `pin <model> --for 8h` | 不受 TTL、不被驱逐、不被 free 碰 | 必填到期 |
| `reserve --gpu 1 --size 80G --for 3h` | 该卡视为外部占用，不再放置并提前挪走睡着的 | 必填到期 |
| `wake <model>` | 预热 | 一次性 |

pin / reserve 记录设置者（来源 IP → 容器名）与到期时间，status 里可见。

## 5. scheduler

- Python 3.10，systemd 服务 `llmsvc-scheduler.service`，状态存 sqlite，15 秒一轮。
- 采集：`nvidia-smi --query-compute-apps`（区分我方 / 外部进程）、`systemctl show vllm-*`（GPU、预算、端口）、vLLM `/is_sleeping`、llama-swap `/running`、`/api/events`（SSE，四态）、`activity.sqlite`（最近请求时间、频率、来源 IP）。
- 执行：`POST /api/models/unload/{id}`（sleep）、`systemctl stop`（stop）、`GET /upstream/{id}/`（wake / 冷启动）、`systemd-run`（由 vllm-launch 执行）。
- 每个动作有 `dry_run`，写结构化日志到 journal，并进入事件表供 TUI 订阅。

### HTTP API v1（容器网内，无鉴权）

| 方法 路径 | 说明 |
|---|---|
| `GET /v1/state` | 卡、模型、pin、reserve、内存预算的完整快照 |
| `GET /v1/events?since=` | 事件流（SSE） |
| `POST /v1/place` | vllm-launch 调用：`{model, util}` → `{gpu}` 或 `{error, blockers}` |
| `POST /v1/free` | `{gpu?, ram?, need_gb?}` → `{freed_gb, slept[], skipped[{model, reason}]}` |
| `POST /v1/pin` `DELETE /v1/pin/{model}` | |
| `POST /v1/reserve` `DELETE /v1/reserve/{id}` | |
| `POST /v1/wake/{model}` | |
| `POST /v1/models` `DELETE /v1/models/{name}` | 临时模型登记 |
| `GET /v1/usage?days=7&by=container` | 按来源汇总 |

## 6. CLI 与 TUI

`cli/llm` 是单文件、仅标准库，用户复制到自己容器即可。子命令：`status` `top` `free` `wake` `pin` `unpin` `reserve` `add` `rm` `usage`。

`llm status` 一屏：

```
GPU0  87/144 GB  llmsvc 74  external 10  free 57
GPU1 133/144 GB  llmsvc  0  external 133 (vllm serve, container X)   不可用
RAM   llmsvc 86/200 GB 预算   宿主可用 823 GB

MODEL                      STATE     GPU  MEM   LAST USED  10min  FROM     PIN
gemma-4-26b-a4b-nvfp4 *    sleeping  0    1.6G  25m        0      ctr-a    -
qwen3.8-27b-unofficial     awake     0    73G   1m         12     ctr-b    18:00 (ctr-b)
gemma-4-31b-it-bf16        stopped   -    -     9h         0      -        冷启动约 3.5min
```

**全屏 TUI**（M5，textual）：`llm` 无参数进入，风格参考 Claude Code。

- 主面板：上方 GPU 条与内存预算，中间模型表，可上下选中。
- 右侧或下方：事件流（llama-swap `/api/events` + scheduler 事件），实时滚动。
- 底部一行命令输入：直接敲 `free --gpu 1`、`pin qwen3.8 --for 4h`，回车执行，结果内联显示，表格即时刷新。
- 快捷键：`f` free、`p` pin 选中模型、`w` wake、`u` usage 视图、`?` 帮助、`q` 退出。
- 所有操作都经 scheduler API，TUI 无本地副作用。

## 7. 部署与验证

- 安装到 llmsvc：`deploy/install.sh`，先以 8011 端口和 `--dry-run` 跑一天只观察不动作，再打开动作。
- 回放测试：把 2026-09-06 / 09-07 的 `vllm-launch` 日志场景做成夹具，断言新算法不再出现无效驱逐与踢默认模型。
- 回滚：停 scheduler，恢复原 `vllm-launch` / `vllm-reaper`。

## 8. 已定决策

| 决策 | 结论 | 理由 |
|---|---|---|
| 是否 fork llama-swap | 否 | 外部控制面即可，保持可升级 |
| 是否在 llama-swap 前加代理 | 否 | 路由与排队它已做好，多一跳只加风险 |
| 2 小时硬停 | 取消，换内存预算 | 硬停与压力无关，代价是冷启动 |
| TP=2 跨卡 | 仅作为 122B 的显式变体 | NVLink 全互联可行，但占两张卡且共享卡需同时空闲 |
| 自动把 bf16 请求换成 nvfp4 | 否 | 做实验需要固定 baseline，只在 status 里提示 |
| 鉴权 | 不做 | 能访问服务器即能用；用量按来源容器汇总 |

## 9. 待定问题

- fine-tune 是 LoRA 还是完整权重，决定 M4 走哪条路。
- 共享卡"外部占用低于阈值"的阈值取多少，先观察一周外部进程分布再定。
- 内存预算 200 GB 与宿主下限 150 GB 需要和内存大户确认。

<!-- Generated-By: Claude Code / claude-fable-5-1 -->
