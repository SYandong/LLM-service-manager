# DESIGN — llama-swap 之上的调度器与终端 UI

状态：v1.1 草案，2026-09-07（吸收 PR #29 第一轮 review：唯一写者与 TTL、放置租约、安静时刻协议、keep_value 统一、sleep 内存准入、TP=2 移出本版、dry_run、TUI 降级）。改动本文件前先开 `type:design` issue。

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
- **scheduler 是唯一写者**：所有 sleep / stop / 放置 / 改配置都经它，一把全局锁串行化。为此 llama-swap 的 `globalTTL` 设为 `0`，且不写每模型 `ttl`（默认 -1 继承全局；注意 v252 里 `ttl: 0` 表示永不卸载，`-1` 才是继承）：**数据面不再自行触发 sleep**，所有 sleep 由 scheduler 按 §4.1 的规则调用 `POST /api/models/unload/{id}` 执行，pin 等保护规则因此对 TTL 同样生效。
- **唤醒不经 scheduler**：请求打到 sleeping 模型时由 vllm-wrapper 直接 `/wake_up`，1 到 3 秒。这之所以安全，是因为 sleeping 模型在 scheduler 的记账里**保留全额预算**（§4.2），我方不会把这部分空间分给别的模型。但记账管不住外部进程：在共享卡上，外部进程可能在两次采样之间占满显存，此时唤醒会 OOM，15 秒一轮的 §4.3 检查来不及阻止。**可用性边界**因此写明：
  - 独占卡（GPU0）上 sleeping 模型的唤醒是**有保证的**（没有外部进程）。
  - 共享卡上 sleeping 模型的唤醒是**尽力而为**。唤醒失败的恢复协议不能依赖 vllm-wrapper 的退出：v252 的 wrapper 在 `/health` 通过时会忽略 `/wake_up` 的错误继续代理。scheduler 自己判定"唤醒失败"，任一信号成立即视为失败：(a) `vllm-<id>.service` 退出（唤醒 OOM 通常让引擎崩溃）；(b) llama-swap 里该模型状态为 ready 但 daemon 的 `/is_sleeping` 持续 10 秒仍为 true；(c) daemon `/health` 连续失败。判定后 scheduler 立即 stop 该模型、清除记账、向 llama-swap `POST /api/models/unload/{id}` 让代理退出，并写事件；下一次请求走 `/v1/place` 冷启动到有空间的卡。这期间的请求可能收到 5xx 或超时，用户指南要求客户端重试一次。
  - 不在共享卡上放置默认模型，保证默认模型的唤醒永远走有保证的路径。
- **冷启动经 scheduler**：vllm-launch 退化为薄客户端，`POST /v1/place` 取得 GPU 号与租约后再 `systemd-run`。腾位由 scheduler 在返回前完成。租约协议见 §5。
- scheduler 的 HTTP 只监听容器网，不做鉴权，与现有信任模型一致（能访问服务器即能用 LLM）。

## 3. 模型的三种来源

| 来源 | 登记方式 | 预算 | 生命周期 |
|---|---|---|---|
| 常驻模型 | llama-swap 配置里的固定块 | 每模型 `util` 比例（占一张卡） | 永久 |
| 临时模型（完整权重 fine-tune） | `llm add <path> --name X --base <常驻模型>` | 继承 base 的配置块与预算 | 7 天无人用自动注销 |
| LoRA 适配器 | `llm add --lora <path> --base <常驻模型>` | 不额外占显存 | 随 base |

临时模型需要改 llama-swap 配置，而 reload 会重建进程表并让所有醒着的模型 sleep，在途请求会被中断。llama-swap 没有"暂停接收"接口，所以做不到完全原子；scheduler 用下面的**安静时刻协议**把风险压到最小，并把残余风险写明：

1. 改配置请求进入队列，scheduler 先 `llama-swap -validate` 新配置，失败即回报。
2. 使用经过设计审核的可信在途来源，等待在途请求为 0 且**连续 5 秒**保持为 0。来源必须覆盖所有受 reload 影响的请求，并有明确的初始化/重连边界、丢失检测与新鲜度保证；未知、断线、丢失、过期或来源代际变化都使安静期证明失效，重新建立可信基线后再累计完整五秒。**当前已核实的 llama-swap v252 `/api/events` 会静默丢弃队列事件，且没有序号/游标或周期心跳，因此不能作为该证明的来源，当前部署的自动 reload 保持禁用。** 两次零快照、仅保持 TCP 连接或本地产生心跳均不能代替完整证据；把实现里的 `ordered_source` 设为 true 也不构成来源保证。可信来源及替代方案在 #53 确定并同步到本设计后，才可解除此门槛；本条不批准上游修改、代理接入、升级或生产启用。即使证明有效，也只覆盖最后一次可信观测之前的区间，不消除第 4–5 条的最终检查与 reload 之间的竞态。
3. **reload 前过保护规则与内存准入**（§4、§4.3）：reload 会让所有 awake 模型 sleep，所以 (a) 只要有 pin 住的模型处于 awake，就不能 reload；(b) 对将要一起 sleep 的**整批** awake 模型做 §4.3 的内存准入，即它们的权重总和加上现有 sleeping 总量不超过预算、宿主可用内存减去这批权重仍高于下限。任一不满足则请求继续排队并回报 `blocked_by`（`pinned_until` 或 `memory_budget`），不会用"先 stop 谁"来腾内存，因为这批里可能有默认模型。默认模型 awake 本身不阻塞（它允许 sleep）。
4. 以经过版本实测确认的**单一触发途径**执行已验证配置的原子替换与 reload；启用 `-watch-config` 时不得再叠加 SIGHUP，触发模式或采用接口未知时不落盘。分别记录「文件已提交」「候选配置已被当前实例采用」「旧 server 退出及资源收尾已确认」；文件摘要、HTTP 200、模型名可见或通用完成日志均不能单独替代后两项。采用与收尾共用剩余 monotonic deadline，任何一项未知、失败或超时都保留恢复标记并阻塞冲突动作，不自动补发信号、重写或回滚。只有采用及收尾均获得可核对证据后才可返回 `applied`。当前没有已通过验收的生产采用/收尾适配器，不因隔离实验通过而启用写入；#60 的版本证据、回调约定与未测项见 [LORA.md](LORA.md#reload-的采用与收尾契约)。检查、触发、采用可见及退出完成的时间分别记录，窗口以实测为准，不承诺百毫秒或零中断；第 2–3 条的 quiet、保护与 RAM 前置门槛不变。
5. 落在这个窗口里的请求会被 llama-swap 中断，客户端收到 5xx；这是**接受的残余风险**，用户指南要求客户端对 5xx 做一次重试。
6. 等待超过 10 分钟仍无安静时刻（或一直被 pin 阻塞）则通知调用方，不强行执行。
7. M4 的 #20 验证一项缓解：把 `cmdStop` 换成带 `mode=wait` 的 sleep（vLLM 0.28 支持），让 reload 触发的 sleep 等在途生成完成而不是中止。验证通过则窗口内的请求也不再被中断。

LoRA 路径待 M4 调研（base 开 `--enable-lora` + 运行时装载，是否仍需 reload 注册别名）。

## 4. 状态机与规则

模型三态：

| 态 | 显存 | 内存 | 恢复代价 |
|---|---|---|---|
| awake | 预算全额 | 少量 | 0 |
| sleeping | 约 2 GB 残留 | 权重大小（pinned） | 唤醒 0.5 到 3 秒 |
| stopped | 0 | 0 | 冷启动 1 到 5 分钟 |

每条边由独立规则管，规则只吃三个纯数据结构（GPU 状态、模型状态、活动统计），输出动作列表。

**保护规则**（所有产生 sleep / stop / 驱逐动作的分支都必须先过这一关，包括 §4.1、§4.2 第 3 和第 4 步、§4.3 的三条与内存准入、§4.4 的 free）：

| 对象 | sleep | stop / 驱逐 | 例外 |
|---|---|---|---|
| 有在途请求的模型 | 否 | 否 | 无 |
| pin 住的模型 | 否 | 否 | 无 |
| 默认模型 | 可以，但排在最后 | **永不** | 无 |
| 其他模型 | 按 keep_value | 按 keep_value | 无 |

某个分支需要 stop 却只剩受保护的模型时，该分支**放弃动作并回报阻塞**（`blocked_by: [{model, reason}]`），进入事件流和 status；不会退化为违反保护规则的动作。

**唯一例外是故障清理**（§2 的唤醒失败、unit 崩溃）：保护表保护的是一个还在正常服务的模型，而故障模型已经不在服务，它名下的"在途请求"实际上已经失败。因此故障清理的 stop 不受保护表限制，但必须满足：只在三个失败信号之一成立时触发；pin 记录**保留**，模型重新放置后 pin 继续生效；事件流里标明这是故障清理而不是策略驱逐。

### 4.1 awake → sleeping

触发之一即可：

- **空闲 TTL**：独占卡（GPU0）60 分钟；共享卡 5 分钟。
- **压力**：共享卡上出现外部进程，或空闲显存低于阈值；有放置请求放不下；用户跑了 `llm free`。
- 压力下按 **keep_value**（越高越值得留着）从低到高逐个睡，直到压力解除。全文只用这一个量，§4.2 与 §4.3 都复用它：

```
keep_value = (1 + requests_last_hour) * cold_start_seconds / (1 + idle_minutes)
pinned 或有在途请求 → 不可睡、不可驱逐
排序键 = (is_default, keep_value)：先睡所有非默认模型（keep_value 低者先），
默认模型单独一层排最后，不靠加权
```

回放测试要包含反例：默认模型 keep_value = 1、某普通模型 keep_value = 100 时，仍先睡普通模型。

体积不进 keep_value，只进可行性判断（§4.2）。`cold_start_seconds` 用该模型最近一次实测冷启动时长，没有则用配置里的估计值。回放测试必须包含"同体积、一冷一热"的场景，断言先睡冷的。

### 4.2 放置（stopped → awake）

记账规则先说清楚，因为它决定了什么算"腾位"：

- **awake 与 sleeping 都按全额预算记账**。sleeping 只是把物理显存还给外部进程用，它的预算仍然留在那张卡上，保证随时能醒（§2）。因此 **sleep 不会为放置新模型腾出预算，只有 stop 会**。
- **已发出的租约**（§5）也按全额预算记账，即使 unit 还没起来。
- 可用 = 总量 − 该卡所有 daemon 与租约的预算 − 外部占用（外部占用只算非我方进程的实际显存）。

放置步骤：

1. 候选卡：GPU0 永远在；共享卡仅当其外部占用低于阈值且无 `reserve`。
2. 逐卡先判**可行性**：按上面的可用量，放得下直接放。
3. 都放不下：对每张卡求"最小代价腾位集合"（模型数小，直接穷举），只在代价最低且确定可行的那张卡上驱逐。**代价 = 被驱逐模型的 keep_value 之和**（§4.1），驱逐 = stop；同一决策中最终要 stop 的 awake 模型，其先前 sleep 动作合并为一次 stop，保留 stop 的位置、统一保护检查及 sleep 路径的内存准入与投影记账；独立的 sleep 动作不受影响。这里不假定 `systemctl stop` 会调用 llama-swap 的 `cmdStop` 或保证 wrapper 先执行 sleep，预算只在确认资源退出后释放（设计讨论：#68）。默认模型、pin、在用的不进集合。
4. 全部在用且放不下：**等最多 2 分钟**，期间一旦有**非默认、非 pin** 的模型空闲超过 30 秒，就按第 3 条的驱逐与动作合并规则腾位；超时返回错误，错误里写明哪张卡被谁的什么模型占着，以及哪些模型因保护规则不能动。
5. 决策、保护与内存准入校验、动作执行和租约记账持有同一把全局锁，串行完成。等待可用资源时，通过关联这把锁的条件变量释放锁，让采集结果发布、`confirm`、`release` 与 `free` 可以推进；唤醒后重新持锁，读取最新状态并重验可行性、保护规则、内存与租约预算，每次动作前重验，不复用等待前的驱逐计划。整个等待使用同一个单调时钟截止时间，最多 120 秒，唤醒不重置期限。返回放置结果前仍须原子登记租约；`systemd-run` 前确认同名 unit 不存在。（设计讨论：#37；实现验收：#17。）
6. 本版**只做单卡放置**。TP=2 跨卡变体不在本版调度范围内（§8）。

### 4.3 sleeping → stopped（硬停）

不再按时间。只有三条：

- **常驻内存预算**：llmsvc 所有 sleeping 模型的权重总和不超过预算（初值 200 GB）。超出停 keep_value 最低的。
- **宿主可用内存下限**（初值 150 GB）：低于则再停一个。
- **不能唤醒**：睡着的模型所在卡已经放不下它醒来。近一小时有请求的改为换卡冷启动，否则停。
- 默认模型永不停。24 小时清扫可选，默认模型除外。

**sleep 前的内存准入**（§4.1 的每一次 sleep 都先过这一关）：sleep 会把权重搬进 pinned 内存，所以执行前检查 `宿主可用内存 − 该模型权重 ≥ 下限` 且 `sleeping 总量 + 该模型权重 ≤ 预算`。不满足时先按 keep_value 从低到高 stop 已经睡着的、不受保护的模型腾出内存；仍不满足则：普通模型直接 stop 而不是 sleep（下次请求走冷启动）；**默认模型或 pin 住的模型保持 awake 并回报阻塞**（保护规则）。这样硬停永远发生在内存被占用**之前**，不会先 OOM 再补救，也不会为了腾内存违反保护规则。

### 4.4 用户意图

| 命令 | 语义 | 到期 |
|---|---|---|
| `free [--gpu N] [--ram] [--need 80G]` | 立即睡掉可睡的（无在途、空闲 >30 s）；`--ram` 则从内存清掉 sleeping 的 | 一次性 |
| `pin <model> --for 8h` | 不受 TTL、不被驱逐、不被 free 碰 | 必填到期 |
| `reserve --gpu 1 --size 80G --for 3h` | 该卡视为外部占用，不再放置并提前挪走睡着的 | 必填到期 |
| `wake <model>` | 预热 | 一次性 |

pin / reserve 记录设置者（来源 IP → 容器名）与到期时间，status 里可见。

**reserve 的持久化与清退结果**（#107）：预留意图与清退结果分开表示。请求字段仍为
`{gpu, size_gb, until, by}`；`gpu` 必须有效，`size_gb` 必须为有限正数，`until`
必须为有限且尚未到达的到期时间。记录中的 `by` 以连接来源 IP 的容器映射为准，
不信任请求体或转发头中的归属声明。有效意图先持久化，然后尝试清退；生效期间
整张 GPU 都被放置策略排除，`size_gb` 是请求注记，不是实测释放量或物理容量保证。

持久化成功后，POST 返回 HTTP 200：
`{id, gpu, size_gb, until, by, evacuation: {status, stopped, skipped, error?}}`。
客户端必须检查 `evacuation.status`，不能把 HTTP 200 当作清退完成：

- `complete`：已无仍需清退的受管理模型或不确定记账；不承诺外部进程不存在或 GPU
  物理占用为零。
- `blocked`：仍有阻塞且没有已确认的 stop；`partial`：已有已确认的 stop，但仍有
  阻塞或未完成步骤。`skipped` 列出 `{model, reason, ...}`；`error` 可说明终止的
  传输、观测、超时、删除或到期原因。
- `stopped` 只列入已有真实退出证明且记账已释放的模型。传输成功、单次 stopped
  快照或策略估计均不能替代证明；晚到或无法确认的结果保留记账并如实回报。

只清退符合既有保护规则、拥有已确认记账且 unit 身份匹配的 sleeping 模型。
awake、pin、默认模型、在途或状态不明、无租约或身份不匹配均回报阻塞，不隐式
sleep awake 模型、不迁移、不收编孤儿 unit，也不强制清理。沿用同一把全局锁和
一个有界 monotonic deadline，执行一个动作后观测退出、核对记账并重新规划；
等待期间释放锁，每个下一步重新检查意图仍有效以及当前保护条件。

`read_only: false` 加可写状态库允许显式保存/删除意图，与 pin 的边界一致；实际
清退还必须启用 `model_actions_enabled`。仅启用意图写入不会开启模型动作。
清退被阻塞、部分完成或失败时，不回滚已保存的意图；但后续 DELETE 或到期仍会
终止其效力。POST 的记录字段描述本次保存的意图，不保证返回时它尚未被并发删除
或到期。请求校验失败返回 400，默认只读返回 405，状态库不可用返回 503；持久化
成功之后的清退失败仍返回含 ID 的 200 结果，避免隐藏已保存意图和已发生的效果。
POST 响应丢失时结果不明，不自动重试，以免创建重复意图。

`DELETE /v1/reserve/{id}` 幂等移除指定意图并返回 HTTP 200 `{id, by}`，`by` 表示
本次删除请求的真实调用来源。删除不唤醒或重启任何模型。删除/到期取消尚未提交的
后续清退步骤；已提交的动作仍按剩余观测期限核对，无法确认时不得虚报完成或提前
释放记账。记录跨重启保存，到期由读取过滤失效；启动不自动重跑中断的清退。
所有 reserve 写接口的 `dry_run=1` 只返回既有 `would` / `blocked_by` 预览，不分配
ID、不持久化、不追加动作事件、不启动清退传输，也不因预览额外采集或探测 unit。

## 5. scheduler

- Python 3.10，systemd 服务 `llmsvc-scheduler.service`，状态存 sqlite，15 秒一轮。
- 采集：`nvidia-smi --query-compute-apps`（区分我方 / 外部进程）、`systemctl show vllm-*`（GPU、预算、端口）、vLLM `/is_sleeping`、llama-swap `/running`、`/api/events`（SSE，四态）、`activity.sqlite`（最近请求时间、频率、来源 IP）。
- 执行：`POST /api/models/unload/{id}`（sleep）、`systemctl stop`（stop）、`GET /upstream/{id}/`（wake / 冷启动）、`systemd-run`（由 vllm-launch 执行）。
- 每个动作有 `dry_run`，写结构化日志到 journal，并进入事件表供 TUI 订阅。

### 放置租约协议

`POST /v1/place` 不是"算完就忘"：

1. scheduler 持锁完成腾位后，**在返回前登记一条租约** `{lease_id, model, gpu, util, expires_at}`，该卡的可用量立即扣减（§4.2）。并发的第二个放置请求看到的就是扣减后的数字，不会重复分配。
2. vllm-launch 拿到 `{gpu, lease_id}` 后 `systemd-run`，盯 unit 直到 `/health` 通过，然后 `POST /v1/place/{lease_id}/confirm`；租约转为正式的 daemon 记账。
3. unit 启动失败（vLLM 报错退出、OOM）时 vllm-launch `POST /v1/place/{lease_id}/release`，预算立即归还。
4. 租约超时（与 vllm-wrapper 的 `--wait-timeout` 一致，15 分钟）仍未 confirm 也未 release 时，按 unit 状态分三种：active 且健康 → 视为已确认；active 但不健康（还在加载或反复重启）→ 租约转为 `stale`，**预算继续保留**，scheduler 每轮检查直到 unit 退出才释放，并在事件流里报警；unit 不存在 → 释放。**预算只在确认进程不占显存后才归还**。
5. confirm 是**幂等**的：同一 `lease_id` 已经因第 4 条或第 7 条自动转正，再收到 confirm 返回 200，不做任何事。只有两种情况返回 409：租约已被**撤销**（超时后 unit 不存在而释放、或 release 过），或已被同一模型的**更新租约取代**。收到 409 的 vllm-launch 必须 stop 自己刚起的 unit；scheduler 发现没有有效租约也没有记账的 `vllm-*` unit 时同样 stop 它。
6. 记账的唯一键是**模型名**：同一模型同一时刻只有一份记账，要么是租约，要么是 daemon。confirm 是原子转正，不是新增一条。
7. 租约与 daemon 记账都持久化在 sqlite。scheduler 重启后先从 `systemctl` 重建 daemon 记账，再逐条处理残留租约：模型已有 daemon 记账 → 租约按第 6 条并入（不重复扣减），健康后转正；没有 unit → 释放；有 unit 但不健康 → 按第 4 条转 `stale`。

### HTTP API v1（容器网内，无鉴权）

所有写接口都接受 `?dry_run=1`。dry_run 返回 `{would: [动作列表]}`，**不写配置、不启动 unit、不调 unload、不持久化 pin / reserve / 租约**，只走策略函数。

| 方法 路径 | 说明 |
|---|---|
| `GET /v1/state` | 卡、模型、pin、reserve、租约、内存预算的完整快照 |
| `GET /v1/events?since=` | 事件流（SSE） |
| `POST /v1/place` | vllm-launch 调用：`{model, util}` → `{gpu, lease_id}` 或 `{error, blockers:[{gpu, model, user, in_flight}]}` |
| `POST /v1/place/{lease_id}/confirm` `POST /v1/place/{lease_id}/release` | 租约确认 / 释放 |
| `POST /v1/free` | `{gpu?, ram?, need_gb?}` → `{freed_gb, slept[], stopped[], skipped[{model, reason}]}` |
| `POST /v1/pin` `DELETE /v1/pin/{model}` | `{model, until, by}` |
| `POST /v1/reserve` | `{gpu, size_gb, until, by}` → `{id, gpu, size_gb, until, by, evacuation:{status, stopped, skipped, error?}}`，持久化与清退结果见 §4.4 |
| `DELETE /v1/reserve/{id}` | 幂等移除意图 → `{id, by}`；不唤醒模型，取消未提交的清退步骤（§4.4） |
| `POST /v1/wake/{model}` | |
| `POST /v1/models` `DELETE /v1/models/{name}` | 临时模型登记，走安静时刻协议（§3） |
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

**全屏 TUI**（M5，textual）：风格参考 Claude Code。安装与降级边界：

- `cli/llm` 始终是单文件、仅标准库，复制即用；它同时是可 import 的模块（`llm.py`），TUI 复用它的参数解析与 HTTP 客户端。
- TUI 是可选安装：`pip install llmsvc[tui]`（拉 textual），或从共享目录复制 `tui/` 目录到 `cli/llm` 旁边。
- `llm` 无参数时：`textual` 可 import **且** stdout 是 TTY → 进入 TUI；否则等价于 `llm status`，并在末尾打印一行安装 TUI 的提示。非 TTY（管道、cron）永远不进 TUI。

- 主面板：上方 GPU 条与内存预算，中间模型表，可上下选中。
- 右侧或下方：事件流（llama-swap `/api/events` + scheduler 事件），实时滚动。
- 底部一行命令输入：直接敲 `free --gpu 1`、`pin qwen3.8 --for 4h`，回车执行，结果内联显示，表格即时刷新。
- 快捷键：`f` free、`p` pin 选中模型、`w` wake、`u` usage 视图、`?` 帮助、`q` 退出。
- 所有操作都经 scheduler API，TUI 无本地副作用。

## 7. 部署与验证

- 快速验证（#108，用户明确要求）：先在独立配置的验证端口以 `--dry-run` 做分钟级只读短测，记录真实起止、样本、缺口、错误与清理结果；典型窗口约 120 秒，GPU 测试仍须空闲且单次目标不超过 5 分钟。不再要求等满一天或一周才继续交付。相应功能用确定性回放、临时环境集成和必要短测验收；长期稳定性和长期占用分布明确标为未测。
- 短测通过不自动启用生产动作：保护、内存准入、可信连续 quiet、配置采用/退出确认、已验证回滚及相应操作授权仍须满足。连续 5 秒 quiet 是正确性条件，不能用日历等待的取消替代它。
- 回放测试：把 2026-09-06 / 09-07 的 `vllm-launch` 日志场景做成夹具，断言新算法不再出现无效驱逐与踢默认模型。
- 回滚顺序（因为 §2 把 llama-swap 的 TTL 设成了 0，只停 scheduler 会让所有醒着的模型永远不睡）：
  1. 停 scheduler，禁用它的 timer / 服务。
  2. 恢复原 `vllm-launch` 与 `vllm-reaper` 脚本，重新启用 `vllm-reaper.timer`。
  3. 恢复 scheduler 安装时备份的整份 llama-swap 配置（`config.yaml.bak-pre-scheduler`，包含原 `globalTTL: 600` 与每模型 `ttl`，因为 v252 里 `ttl: 0` 表示永不卸载，单改 globalTTL 会被模型级值覆盖），`llama-swap -validate`，然后按 §3 的安静时刻协议手工 reload（等在途请求为 0 且无 awake 的 pin，醒着的模型会进入 sleep）。
  4. 核对：`systemctl list-units 'vllm-*'` 与 `/running` 一致，10 分钟后空闲模型进入 sleep。
  回滚脚本 `deploy/rollback.sh` 按这个顺序执行，每步可单独重跑。

## 8. 已定决策

| 决策 | 结论 | 理由 |
|---|---|---|
| 是否 fork llama-swap | 否 | 外部控制面即可，保持可升级 |
| 是否在 llama-swap 前加代理 | 否 | 路由与排队它已做好，多一跳只加风险 |
| 2 小时硬停 | 取消，换内存预算 | 硬停与压力无关，代价是冷启动 |
| TP=2 跨卡 | **本版不做**，放置 API 与可行性判断只针对单卡 | NVLink 全互联技术上可行，但需要多卡预算与原子租约；等单卡策略稳定后另开 `type:design` issue |
| 自动把 bf16 请求换成 nvfp4 | 否 | 做实验需要固定 baseline，只在 status 里提示 |
| 鉴权 | 不做 | 能访问服务器即能用；用量按来源容器汇总 |

## 9. 待定问题

- 连续安静期的可信在途来源（#53）：当前 v252 SSE 不满足完整性与新鲜度要求；在来源/保证确定并验证前，#20 自动 reload 不可启用。触发与采用确认方式由 #60 单独讨论。
- fine-tune 是 LoRA 还是完整权重，决定 M4 走哪条路。
- 共享卡"外部占用低于阈值"的初始值，用当前短样本、确定性压力回放与保守配置验证；长期分布作为后续校准依据，不作为本轮日历门槛。
- 内存预算 200 GB 与宿主下限 150 GB 需要和内存大户确认。
- `mode=wait` 的 sleep 是否能让 reload 不中断在途请求（§3 第 6 条），M4 验证。
- TP=2 何时纳入：单卡策略稳定后再议。

<!-- Generated-By: Claude Code / claude-fable-5-1 -->
<!-- Generated-By: Codex / gpt-6-astra -->
