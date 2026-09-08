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

触发方式决策由 [#60](https://github.com/SYandong/LLM-service-manager/issues/60)
关联 #20 跟踪；实际 `notify_reload` 实现 PR 必须同步权威 DESIGN §3 第 4 条
并取得 Fable 当前 SHA 审核。可靠连续 quiet 数据源另由
[#53](https://github.com/SYandong/LLM-service-manager/issues/53) 跟踪。本次局部
YAML 修复不决定触发方式，也不授权启用写入。

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
