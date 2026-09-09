# M4：完整权重登记、LoRA 与 reload 验证记录

关联 #19、#20、#21。日期：2026-09-08。初始支持分支是完整权重登记；
`add --lora` 暂不启用。本记录不替代 `DESIGN.md` 的决策流程：§3 中
LoRA「不额外占显存」没有实测支持，不能作为准入依据。#21 仍需测量及设计结论。

## 已观察与尚未测量

以下是 registry lane 在容器内执行只读命令得到的结果；没有调用 sleep、wake、
LoRA 装载接口，没有启动 GPU 工作负载，也没有修改线上配置或服务。

| 检查 | 观察结果 |
|---|---|
| llama-swap `-version` | `v252 (e31a1ad)`，构建时间 `2026-08-31T16:54:46Z` |
| vLLM 包元数据 | `0.28.0`，安装于 Python 3.12 虚拟环境 |
| 容器默认 Python | `3.10.12`；本仓库仍以 Python 3.10 为目标 |
| llama-swap 服务参数 | 已启用 `-watch-config` |
| wrapper `sleep -help` | 有 `sleep-level`、`stop-pid`、`vllm-url`；没有 wait-mode 参数 |
| 已安装 sleep API 源码 | 读取 query 参数 `mode`，默认 `abort`，传入 engine sleep |

尚未测量：适配器 sleep/wake 后是否仍可正确推理、适配器显存/RAM 增量、
吞吐/首 token/唤醒延迟、完整权重首次冷启动、reload 窗口内被中断的请求数。
离线 mock 通过不等于这些验收完成。

## 固定版本的上游证据

llama-swap 源码固定于
[`e31a1adee494bb7a578e2a97ec891b3e809899dc`](https://github.com/mostlygeek/llama-swap/tree/e31a1adee494bb7a578e2a97ec891b3e809899dc)，
vLLM 固定于 [`v0.28.0`](https://github.com/vllm-project/vllm/tree/v0.28.0)。

- vLLM 支持启动时启用 LoRA，以及设置
  `VLLM_ALLOW_RUNTIME_LORA_UPDATING=True` 后的
  `/v1/load_lora_adapter`、`/v1/unload_lora_adapter`。这是后端能力，
  不证明当前 base 已开启它，也不证明 llama-swap 已为适配器建立路由。
  [LoRA 文档](https://github.com/vllm-project/vllm/blob/v0.28.0/docs/features/lora.md)
- LoRA 层按槽数、rank 等配置创建张量；其设备与模型层有关。文档也明确
  rank 设置影响内存与性能。因此适配器不是零成本；预分配可能让某次 load
  的 `nvidia-smi` 增量很小，仍不能推出启用 LoRA 没有成本。
  [LoRA 张量分配](https://github.com/vllm-project/vllm/blob/v0.28.0/vllm/lora/layers/base_linear.py#L129)
- sleep level 1 将权重备份到 CPU 并丢弃 KV；level 2 丢弃权重和 KV，
  再唤醒需要恢复权重。该通用说明没有承诺所有 LoRA 状态在每个 level 下
  都保留，必须检查适配器实际推理，不能只看 `/v1/models`。
  [sleep 文档](https://github.com/vllm-project/vllm/blob/v0.28.0/docs/features/sleep_mode.md)
- llama-swap `useModelName` 是上游模型名字符串；`aliases` 与
  `filters.setParamsByID` 的键在配置中登记别名。后者不能覆盖受保护的
  `model` 字段。推论：当前固定 `useModelName=base` 的配置加一个 alias，
  不足以选择后端 LoRA 适配器；这些静态字段也不是运行时注册 API。
  同一文件确认模型 `metadata` 可承载自定义数据并出现在模型列表中。
  [固定版本配置说明](https://github.com/mostlygeek/llama-swap/blob/e31a1adee494bb7a578e2a97ec891b3e809899dc/docs/config.example.yaml#L318)

## reload 的具体限制

v252 支持 `-config <file> -validate`，校验后退出；支持 SIGHUP 与配置监视器
触发 reload。reload 创建新 server、替换活动 server，再关闭旧 server。
并发 reload 有互斥保护，但先后发生的两次触发不是一个事务。
[入口源码](https://github.com/mostlygeek/llama-swap/blob/e31a1adee494bb7a578e2a97ec891b3e809899dc/llama-swap.go#L232)

监视器每 2 秒对配置路径 `stat`，比较 mtime/size；源码也处理原子替换目标。
因此通常会发现 rename，但不能把落盘至 reload 的窗口称为固定百毫秒；
只改变内容而保持相同 mtime/size 也不能依赖它检测。线上启用 watcher 时，
不能盲目在替换后再发 SIGHUP，否则可能产生第二次 reload。
[监视器源码](https://github.com/mostlygeek/llama-swap/blob/e31a1adee494bb7a578e2a97ec891b3e809899dc/internal/watcher/watcher.go)

wrapper sleep 向 `/sleep` POST JSON body，之后向 serve proxy 发 SIGTERM；
serve proxy 的 SIGTERM 处理还会再次调用 sleep。源码未传 query `mode=wait`。
因此只改第一条 sleep 请求不能证明整条关闭链路无中断。
[wrapper 源码](https://github.com/mostlygeek/llama-swap/blob/e31a1adee494bb7a578e2a97ec891b3e809899dc/cmd/vllm-wrapper/main.go#L171)

完整权重登记继承选定 base 的运行参数与 util 预算，要求权重格式与 base
兼容；配置验证不能代替一次实际冷启动。临时记录存于上游支持的每模型
`metadata.llmsvc_registry`，随配置一起原子落盘。登记仅插入新的模型块；
删除仅移除目标模型块，并局部修改相关 group 的 `members`。无关配置的
注释、锚点名称、别名、空白和字节保持不变，只有新增模型块由 PyYAML 生成。
支持 UTF-8、统一 LF/CRLF、文件末尾换行、显式 block 形式的根和 models
映射，以及 block 或单行 flow 成员列表。需要编辑的映射若是 flow、merge、
重复/复杂键或别名引用，或成员列表为多行 flow，则在写入前拒绝；不做
静默格式归一化。局部结果还要通过语义比对，移除会留下悬空锚点引用时也
拒绝。同样的检查适用于手工删除、七天过期注销和排队后的重新校验。
删除先确认路由移除，再由 core 保护检查后
停止目标 unit，并确认其不存在；七天未用自动注销也要重新检查活动与保护。

## reload 的采用与收尾契约

关联 [#60](https://github.com/SYandong/LLM-service-manager/issues/60) / #20。
本节供草稿设计 PR 审核，实际适配器实现和启用仍需独立验证及当前 SHA 的
Fable 审核。可靠 quiet 来源的 [#53](https://github.com/SYandong/LLM-service-manager/issues/53)
不因触发实验或字段解析修正而解决；本节不修改 DESIGN §3 第 2 条。

| 证据 | 已支持的结论 | 不能推出的结论 |
|---|---|---|
| [#79 固定提交的测量](https://github.com/SYandong/LLM-service-manager/blob/a65d6f508ae28926546a28f192ff491af0d244f8/deploy/reload-results-20260908.json)：真实 v252 swap/wrapper、fake 有限流后端，4 条在途，单次 SIGHUP | abort 为 4 条截断；candidate wait 为 4 条完成；sleep 调用分别是 abort/abort 与 wait/abort | watcher-only、真实 vLLM、持续新到达请求的排除、可靠 quiet 或旧 server 退出已证明 |
| #79 的 `adoption_seconds`，约 22 ms | 新模型标识在 `/v1/models` 可见的时间；该实例未启用 watcher | 原生 generation 采用证明、退出完成或真实中断窗口 |
| [#91 原生测量](https://github.com/SYandong/LLM-service-manager/blob/e98694635406019224d8bd7c280c65f43c6c99c3/deploy/watcher-witness-results-20260908.json)：固定 v252 二进制、CPU dummy 进程、mock 上游，8 个场景；零 reload 信号/兜底写入 | watcher-only 的 delayed-stop 场景中，候选字节已落盘时仍读到旧 G，约 1.692 秒后收到新 G；文件摘要及同一进程身份随读数记录 | 不是读取磁盘即算采用；观测时间不是指针切换精确时刻，也不是生产时延上限 |
| #91 stranded-stop / failed-stop | 新 G 约 1.673 秒可见；约 31.625 秒出现通用完成日志时旧 stop helper 仍存活。另一场景 cmdStop 退出码为 7，之后也有通用完成日志 | G 可见或完成日志均不证明旧资源收尾；不能把 cmdStop 失败等同于已观测到 `old.Shutdown` 返回错误 |
| #91 负例 | 相同 mtime/size 未触发 reload；无效候选保留旧 G且在观测期无自动重试；缺 witness 的 HTTP 200 工具错误在写入前阻塞；重启/超时仍保留失败分类 | 后续诊断成功或 fixture 最终清理不能追认 deadline 内采用/收尾成功；`status: ok` 只是测量工具完成 |

**触发约定。** #91 在与已有记录一致的 v252/e31a1ad 二进制上验证了单一
`-config PATH -watch-config`、一次原子替换、零 SIGHUP 的受控路径；七个
场景写入一次，缺 witness 场景不写。八个场景均清理了自身会话和临时目录。
这使 watcher-only 从仅有源码依据变为隔离实测支持，不改变生产门槛。采用它
仍须核对实际二进制、单一配置来源、实例身份、监视标志和可检测的 mtime/size
变化；未知即拒绝落盘，不补 SIGHUP。#79 的 signal-only 证据限于无 watcher
的隔离实例，不能据此切换生产模式。两秒轮询不是采用耗时上限。

**已实测的原生采用读数。** #91 使用 SHA256 为
`32aea60b5c1be987c27dde6ea4aaa84f9be7ad93eaede011295fad1e276e80ea`
的原始 v252 二进制，在临时配置中放入无业务引用的
`macros.llmsvc_reload_generation`，绑定前一代、新一代、精确文件摘要与
PID/start ticks。原生请求为 POST `/api/mcp`，头部是
`Mcp-Protocol-Version: 2026-07-28`、`Mcp-Method: tools/call`、
`Mcp-Name: config__get_config`，JSON-RPC body 为：

```json
{"jsonrpc":"2.0","id":"<fresh-id>","method":"tools/call","params":{"name":"config__get_config","arguments":{"path":"macros.llmsvc_reload_generation"}}}
```

成功响应的 id 匹配，没有 JSON-RPC error 或 `result.isError`；单一 text
内容包含该路径的说明及完整 YAML fence，解析后是期望的 `gen_<32位hex>`
标量。精确响应样例及时间在上述测量文件中；[复现说明](https://github.com/SYandong/LLM-service-manager/blob/e98694635406019224d8bd7c280c65f43c6c99c3/deploy/WATCHER_WITNESS.md)
区分请求开始和响应接收时间。采用可见性取响应接收时刻，不回填为请求开始。
缺 macro、错误协议版本和未知工具实际返回 HTTP 200 错误；不能只检查状态码。
docs-disabled/auth/redirect/截断或畸形响应并非全部做过真实二进制实验；离线
回归覆盖的故障不能写成已实测。测量后的错误 body 诊断修正另由离线测试覆盖，
成功测量所用 harness 摘要保留在证据文件中，没有为它重复成功场景。

这些读数证明受控同实例、同候选字节绑定下的新配置可见机制，不证明旧 server
收尾或生产可用性。所有可见 G 的案例仍为 `settlement_confirmed: false`，
且要求保留 barrier/reconciliation。重启即使返回新 G 也因实例绑定改变被拒绝；
验证 deadline 后的只读诊断不升级原分类。fixture 最终清理其自有进程不是
采用时刻的退出证明。生产 generation 局部编辑、持久化事务/屏障和 notifier
仍未因此实现；不能将临时 JSON 配置写法用于绕过 #61 的格式保留约束。

**现有回调签名，待验证的实现。** `notify_reload(*, deadline)` 只在候选采用
以及旧 server 收尾均已确认后返回；采用可见但退出未知必须抛错/超时，不能
触发 `after_apply(*, deadline)` 或提前解除事务阻塞。后者调用 core 的
`stop_model(name, *, deadline)` / `unit_absent(name, *, deadline)` 完成受保护的
目标 unit 清理，不能拿新实例的 `/running` 空表替代旧实例的退出证明。
`deadline = min(submitted_monotonic + 600, replacement_monotonic + operation_timeout)`；
现有 operation_timeout 默认 10 秒、最大 60 秒，每次 I/O 必须受剩余时间约束。

配置提交前发现未知能力则不写；提交后采用、退出或目标清理未知/失败/超时，
保留 `.llmsvc-pending` 并进入 `reconciliation_required`，不重触发或盲目回滚。
核对实例未更换、候选绑定、旧 server 收尾及目标 unit 清理后才可恢复；仅有
磁盘摘要相等不足以清除标记。观测回调保持 #57 的非阻塞行为；等待采用时即使
释放 action lock，也必须用明确的事务状态阻塞冲突写入，不能让新 pin 在旧
server 仍可能 sleep 时生效。`dry_run` 不创建候选、generation 或恢复状态。

验收须覆盖 watcher-only 与 signal-only 的隔离边界、同 mtime/size、原生
接口不存在/响应错误/截断/旧值、实例重启、采用先于延迟或失败退出、deadline、
恢复及 dry-run。ops 负责可执行证据；core/registry 根据获批设计实现适配器，
不把本节或 fixture 的合并视为实现完成、生产启用或新的发布许可。

`reload.py` 用同一 scheduler action lock 串行化最终校验与替换，要求连续
5 秒零在途、事件流未断、无 awake pin、整批 awake 权重满足 RAM 准入。
队列最长等待 600 秒；等待期间不占着锁睡眠。验证发生在同目录临时文件上，
成功后才原子替换；`dry_run` 不生成临时文件、不入队、不调用 reload/cleanup。

adoption 回调必须确认新配置已被采用，不能把「信号发送成功」当作完成。
确认与清理回调接收同一个 monotonic deadline（默认最多 10 秒，并受剩余
600 秒总限约束）；core 必须据此设置实际 HTTP/subprocess 超时。Python
回调不能在持锁时被安全强制终止，因此不接受没有 I/O 超时的线上适配器。
替换后确认或 unit 清理失败会留下 `.llmsvc-pending` 标记，重启后也阻止
后续写入；恢复必须核对配置摘要、上游采用情况及目标 unit 清理。
未提交的排队请求在重启后需重新提交。`apply_seconds` 仅记录本地替换到
确认完成的时长，不冒充 HTTP 中断窗口。**客户端仍须对 reload 窗口内的
5xx/断流按业务幂等性重试；当前没有零中断保证。**

### 可复用的只读 witness 适配器

`llmsvc.reload_witness.NativeGenerationReader(base_url).read(deadline=...)` 复用
#91 的固定请求/严格标量解析，返回带开始、接收时间和错误码的 `GenerationRead`。
只接受直接 HTTP IP 地址；DNS 名称、HTTPS、代理与重定向不在本版支持范围内。
固定协议头与 `PINNED_COMMIT` 对应；调用方仍须验证实际二进制和端点身份。
默认单次 I/O 总预算 0.5 秒（可显式配置，最大 60 秒），且不能延长调用方的
monotonic deadline；socket watchdog 终止停滞或持续滴流的读取，迟到结果不算成功。
响应 body 上限 64 KiB，原生文本上限 32 KiB；不重试、不回显远端错误正文。

纯函数 `check_visibility(expected, before, reading, after, now=..., max_age=5)`
检查调用方提供的 `CandidateBinding`（端点、唯一 generation、实例 PID/start
ticks、候选 SHA256）与前后 `BindingObservation`：观测须覆盖本次读取、
保持新鲜、身份和摘要匹配，且仍在读取 deadline 内。它不自行读文件或探测进程，
也不证明观测间未发生重启/文件替换再恢复。`candidate_generation_visible=False`
表示未确认；`settlement_confirmed` 始终为 `None`。这不是 notifier、`applied`
决策或 #53 quiet 证明，不写 generation/恢复标记、不发信号、不激活任何接口。
core 后续须在独立集成中提供可靠的前后观测及收尾证据。测试使用 fake HTTP 和
#91 已提交的原生响应，不重复长实验。

### 队列快照与重建后的恢复检查

`ReloadQueue.queue_snapshot()` / `ModelRegistry.queue_snapshot()` 返回分离的
`jobs`、`pending_ids`、`fenced` 和 `recovery`。任务包含读取时的 queued/blocked/
timed_out/终态，以及 `recorded_status`；读取不会出队、执行动作或发送事件，
实际超时推进仍由 `process_once()` 完成。仅标记中的未完成任务可在重建后显示，
不会虚构已丢失的内存队列或跨重启 monotonic 时长。

`inspect_recovery(reading=None, before=None, after=None, max_age=5)` 限量读取
64 KiB 常规非链接标记并验证结构、候选摘要；坏标记仍保持 fence。它不自动
联网。调用方可先用 `NativeGenerationReader.read(deadline=...)` 得到读数，再
传入包围该读取的 `BindingObservation`。`enqueue(..., witness_binding=...)`
可将已知 `CandidateBinding` 存入标记，提交前和重算后必须匹配精确候选摘要；
旧标记没有绑定时保持未知，不从当前实例猜测原始身份。即使 native G 可见，
恢复检查仍返回 `reconciliation_required`、`settlement_confirmed=None`，不清标记。

`reconcile(confirm, dry_run=False)` 现在要求 verifier 返回显式 `RecoveryProof`，
不再接受裸 bool、truthy 字典或只读可见性结果。证明须绑定当前 `marker_sha256`，
明确确认 generation、实例、旧 server 收尾、目标清理；如标记含实例绑定，证明
实例必须一致。标记和配置在 verifier 前后都要保持匹配。部分证明一律拒绝；
`dry_run` 不读标记、不调用 verifier、不清标记。这个类型只表达独立可信 verifier
的完整断言，不生成新的收尾/重启证明；目前只有显式 fixture 可以提供这些模拟
事实。core 后续负责 HTTP/config/main 接入，此处未挂载诊断或恢复写接口。

### 只读库存与变更预览

`ModelRegistry.inventory()` 返回当前配置中的模型元数据、独立的 runtime_state、
到期/移除保护信息，以及 `pending_changes` 和恢复 fence。`source=config`
不表示数据面已采用；探测缺失、错误或过期时运行态为 unknown。`expires_at`
仅是已知最后使用时间对应的七天空闲阈值，pin/活动等仍可能阻止注销；缺失历史
不伪造到期时间。输出不含 cmd/cmdStop 或配置全量内容。

`preview_add(body)` / `preview_remove(name)` 复用现有 dry-run 和局部候选生成，
按 pending FIFO 的投影计算候选摘要；add 提供计划端口、base 和原始 util_macro。
它们不入队、不写配置、不执行 cleanup，不预留所选端口；后续提交必须重算。
受保护的 remove 返回空 would 与 blocked_by，不能视为操作已完成。模型级
`removable` 或 would 不代表全局 quiet/RAM/recovery 准入；未完成恢复仍拒绝
候选预览，库存仍可显示该 fence。core 后续拥有 HTTP/UI 接入，本片不新增路由。

## 交给 ops 的有界实测计划

只有 ops 持有 `gpu-test.lock` 后执行。每次目标不超过 5 分钟；没有缓存的
兼容小 base、匹配适配器、可用独占空闲卡或时间不足就记录 skip，不能下载
大权重、停止/休眠现有模型来制造空闲。使用配置指定的测试路径/端口和唯一
测试 unit；只清理本次创建的资源。

1. 记录 UTC 时间、上述二进制版本与 GPU 进程/利用率/显存、系统 RAM、在途
   请求；确认没有外来计算进程、服务负载和显存不足。启动前再次检查。
2. 用相同缓存小 base，分别记录未启用/启用 LoRA 后的 steady GPU/RAM，
   然后装载一个匹配适配器，记录 load 增量、耗时及 base/adapter 推理结果。
   参数包括 dtype、rank、max_loras、max_cpu_loras、并发和 token 数。
3. 在测试实例做 level 1 sleep/wake；核对 base 和 adapter 的实际输出、
   HTTP 状态及延迟。level 2 仅在剩余预算足够时做，先按文档恢复权重；
   记录 adapter 是否需重载，不能把列表仍有名称视为权重有效。
4. 对独立测试 llama-swap：先证明 direct adapter 请求正确，再比较当前
   base `useModelName`、alias 和明确的 adapter 路由配置，记录上游实际 model。
   新 alias 的配置变更仍走测试 reload；不得改生产配置。
5. 在 mock 流式上游先跑 reload 竞态（无需 GPU），记录最后一次安全检查、
   rename、watcher/SIGHUP 的单一触发、reload 确认时间，逐请求统计 completed、
   5xx、断流、超时。分别测现有 wrapper 和候选 query `mode=wait` 关闭链路；
   持续负载、awake pin、整批 RAM 不足应排队/超时。GPU 版本仅在空闲预算
   内补测；任一外来负载出现就终止测试自身资源。
6. 记录结束时 GPU/进程/在途状态和清理结果。把命令、参数、逐请求结果及
   JSON/CSV 附到 #19/#20/#21。由协调器据此推进 DESIGN 决策与验收，registry
   的离线 PR 不关闭这些跨 owner issue。

<!-- Generated-By: Codex / gpt-6-astra -->
