# CLI 使用说明

`cli/llm` 是 Python 3.10+ 标准库脚本。只复制这一个文件即可运行，无需安装仓库、`rich` 或 `textual`。提供只读 `status`、`usage`、pin/unpin 记录、free/wake、reserve 预览与结果客户端和可选全屏面板；所有请求都发给配置的 scheduler。

```sh
python3 llm --help
LLM_URL=http://scheduler:8011 python3 llm status
LLM_URL=http://scheduler:8011 python3 llm status --json
```

上面的主机与端口是示例，需替换为部署方提供的 scheduler 地址。不要填 llama-swap 的 OpenAI API 地址。脚本没有内置服务地址。

## 配置

配置默认读取 `~/.config/llm/config`，可用 `LLM_CONFIG` 或 `--config` 指定其他文件。文件为 UTF-8，支持带注释的 INI 格式；`[llm]` 节头可省略。

```ini
[llm]
# 改成部署方提供的 scheduler 地址；支持 http 与 https。
url = http://scheduler:8011
# 普通 HTTP 请求超时秒数，必须为正数；默认 10。free/wake/reserve 单独使用 --wait。
timeout = 10
```

优先级为命令行参数 > 环境变量 `LLM_URL` / `LLM_TIMEOUT` > 配置文件。全局参数放在子命令前：

```sh
python3 llm --config ./client.conf --timeout 3 status
python3 llm --url http://scheduler:8011 status --json
```

缺少地址、连接失败、HTTP 错误和无效响应会在 stderr 输出解释，并返回退出码 1；参数错误返回 2，Ctrl-C 返回 130。HTTP 重定向不会自动跟随，配置应使用最终服务地址。

## 状态输出

以下是由测试中的合成 `StateSnapshot` 生成的 100 列示例，**不是服务器实测**：

```text
GPU0  87/144 GiB  llmsvc 77  external 10  free 57
GPU1  ?/? GiB  llmsvc ?  external ?  free ?
RAM   llmsvc 86/200 GiB budget  host available 823 GiB

MODEL                       STATE    GPU MEM    USED   10m  FROM       PIN (UTC)
default-model *             sleeping 0   1.6G   25m    0    ctr-a      -
research-model              awake    0   73G    1m     12   ctr-b      01-15 09:00Z (ctr-b)
cold-model                  stopped  -   ?G     ?      ?    -          -
  cold start estimate: 3.5m
unknown-model               unknown  -   ?G     ?      ?    -          -
WARNING GPU1 probe unavailable
* default model | ? unknown | sampled 01-15 08:00Z | read-only
```

- 显存与内存单位为 GiB。GPU 三段为实际观测的服务占用、外部占用、空闲量；不会把 sleeping 模型的放置预算当成物理占用。
- `MEM` 是模型实际驻留显存，`USED` 是相对快照时间的上次请求间隔，`10m` 是最近十分钟请求数，`FROM` 是来源标签。
- `*` 表示默认模型，`?` 表示探测未知；未知不会被当成零或 stopped。停止模型的冷启动时长来自后端估计或测量。
- PIN 显示 UTC 到期时间及设置者；有效 reserve、未完成或 stale 的租约、策略阻塞与采集错误显示在表后。
- 小于 100 列时收窄模型表，将活动与 pin 详情放在模型下一行；超长标识符以 `~` 标出截断。更窄的输出会折行。`--json` 保留完整字段，适用于脚本与排查长名称。
- 无参数且 stdout 是 TTY、可选 TUI 可导入时启动全屏面板；否则输出 `status`，末尾提示 `pip install 'llmsvc[tui]'`。非 TTY 不导入或启动 TUI。显式 `status` / `status --json` 不附加提示，便于脚本读取。

## 用量统计

```sh
LLM_URL=http://scheduler:8011 python3 llm usage
LLM_URL=http://scheduler:8011 python3 llm usage --days 30
LLM_URL=http://scheduler:8011 python3 llm usage --days 7 --by model --json
```

窗口为最近 7 或 30 天，默认 7 天；分组支持 `container`（默认）、`ip`、`model`。输出请求数、输入 token、输出 token 的整数总量，保留完整精度。窄屏按分组分行显示，`--json` 保留后端完整响应。

- 总量已知但历史来源缺失时，`unknown` 分组保留这些请求与 token，归属列明确标记未知。
- `IP only` 表示有来源 IP、没有容器映射；不会推测容器名。按 model 分组时，归属信息标为未按来源分组。
- 后端返回不可用响应（503、`known: false`）时显示原因与 `?`；`--json` 保留 null 总量，退出码为 1。来源可用且窗口确实为空时，零总量才是有效结果。连接或协议错误沿用前述非零退出码与 stderr 说明。
- 客户端核对行计数与总计一致，并拒绝缺失、负数、非整数或窗口不匹配的响应。

## Pin / unpin 记录

```sh
LLM_URL=http://scheduler:8011 python3 llm pin model --for 8h --dry-run
LLM_URL=http://scheduler:8011 python3 llm pin model --for 8h
LLM_URL=http://scheduler:8011 python3 llm unpin model
```

`pin` 必须给出 `--for`，支持正数与 `s` / `m` / `h` / `d`，例如 `30m`、`1.5h`、`2d`。不提供永久 pin。`unpin` 无需时长，重复解除已经不存在的记录也可成功。两个命令都支持 `--dry-run` 和 `--json`。

- 服务器默认只读；只有部署方明确允许的服务才接受实际记录写入。客户端不会更改服务器配置，`read_only` / `operation_not_enabled` 会作为失败显示并返回非零退出码。
- 成功结果中的 owner（pin）或 actor（unpin）来自服务器对连接来源的判断。客户端按协议提供兼容 `body.by`，但它不决定实际归属；没有映射时保留服务器返回的 `ip:...` 标记。
- Dry-run 不改变记录，输出计划或阻塞原因；阻塞计划返回退出码 1。预览 JSON 的 `by` 是假设标签，不是经服务器确认的实际操作者。
- 模型名作为原样 JSON 值或 URL 编码路径段发送。带空格、斜杠、Unicode 或 `?/#/%` 的名称用 shell 引号包住；以 `-` 开头的名称可写为 `pin --for 8h -- '-model'` 或 `unpin -- '-model'`。
- 写入成功表示 pin 记录已保存；不会预热模型。实际 TTL/reaper 接入、reserve 及完整保护验收仍是后续部署事项。请求不会自动重试；遇到不确定的连接/响应错误时先用 `status` 核对。

## Free / wake

```sh
LLM_URL=http://scheduler:8011 python3 llm free --gpu 0 --need 80G --dry-run
LLM_URL=http://scheduler:8011 python3 llm free --gpu 0 --need 80G
LLM_URL=http://scheduler:8011 python3 llm free --ram --need 40G
LLM_URL=http://scheduler:8011 python3 llm wake 'org/model name'
LLM_URL=http://scheduler:8011 python3 llm wake --wait 1200 -- '-model'
```

这些命令只适用于部署方明确允许的服务。服务器默认 `read_only: true`、`model_actions_enabled: false`；客户端不会改变开关。`read_only` / `operation_not_enabled` 是失败，不会绕过或改走数据面接口。

- `free` 不带 `--gpu` 时选择初始观测中的全部 GPU；`--need` 是希望**额外**释放的 GiB，可写 `80G`、`80GiB` 或 `80`，支持非负小数。省略数量表示不设置数量目标；`--need 0` 不执行模型动作。`--ram` 请求从宿主内存回收，可能停止符合条件的模型，之后需要冷启动。
- `wake MODEL` 使用 URL 编码的模型路径和空请求体，显示服务器返回的 `ready`、`cold_start` 与耗时。状态 `ready` 才作为干净成功；即使 `partial` 同时带 `ready: true`，仍保留错误并返回非零退出码。
- 两个命令均支持 `--dry-run` 与 `--json`。预览显示 `would` / `blocked_by`，明确把 `estimated_freed_gb` 标为策略估算；无模型动作、额外采集或持久化写入。阻塞预览返回退出码 1。
- **HTTP 200 不等于操作成功。** Free 的 `complete`、wake 的 `ready` 返回 0；`blocked`、`partial`、`failed`、`timeout`、`no_progress` 返回 1。保留已确认的 slept/stopped、全部 skipped 原因与附带来源/在途字段，以及 error/error_model；`--json` 保留完整响应。
- Free 显示的是所选 GPU 空闲显存或宿主 MemAvailable 的**实测净变化**，包括同期外部活动，不是逐模型释放归因或给用户保留的容量。`freed_gb: null` 显示 unknown；不以估计值或预算替代。`measurement_complete: false` 明确表示不是最终/当前总量，即使保留了此前已确认的部分实测值。采样时刻显示为 Unix 秒。模型可能先睡后停而出现在两份列表中；最新状态以 `status` 为准。

响应等待独立于普通 `--timeout` / `LLM_TIMEOUT`：free 默认 `--wait 150` 秒，wake 默认 `--wait 930` 秒，为服务器默认 120/900 秒动作期限各留 30 秒返回余量。`--wait` 是客户端 HTTP 等待时间，不修改服务器期限；部署方延长期限时需相应调整。显式缩短等待、断线或退出客户端不能撤销已受理的操作。未知结果会提示先检查 `status` 与事件；客户端不自动重试，刷新失败也不重放写入。

当前只消费 scheduler 事件，冷启动原始 llama-swap 日志转发与真实 sleeping-wake <3 秒、显存释放实测仍需 ops/integration 验收。命令发布不代表生产动作获准。

## Reserve

```sh
LLM_URL=http://scheduler:8011 python3 llm reserve --gpu 0 --size 80G --for 4h --dry-run
LLM_URL=http://scheduler:8011 python3 llm reserve --gpu 0 --size 80G --for 4h --dry-run --json
```

`--gpu`、`--size`、`--for` 都是必填项。GPU 为非负整数，size 是正 GiB（`80G` / `80GiB` / `80`），时长复用 pin 的正数 `s/m/h/d` 规则。不提供默认或永久预约；零大小、零时长、非有限数与无法表示的到期时间在请求前被拒绝。

- 当前已挂载的 `?dry_run=1` 返回 would/blocked_by：只预览预约和符合条件的 sleeping 模型清理，不持久化、不调用模型 transport、不分配预约 ID。`by` 是兼容请求标签，预览中标为假设值；实际保存时才按连接来源确定权威 owner。预览是纯策略计划，不代替实际执行前的租约、unit 身份和保护重验；预览成功也不保证实际 evacuation 完成。阻塞预览返回退出码 1；`--json` 保留完整内容。
- 采用 #112 接口的 scheduler 在 `read_only: false` 且有可写状态库时接受实际预约；默认只读仍返回 405。意图写入与 `model_actions_enabled` 分开：关闭模型动作也可保存意图，但不能据此认为清理完成。客户端显示服务器的 `read_only` / 旧版本 `operation_not_enabled`，不会自动改走预览、重试或更改服务器配置。
- 去掉 `--dry-run` 才发送实际预约。成功保存返回 `{id,gpu,size_gb,until,by,evacuation:{status,stopped,skipped,error?}}`。客户端显示服务端权威 owner 和保存回执，单独显示 evacuation 的 complete/blocked/partial 及全部确认停止、阻塞与错误详情。
- **HTTP 200、保存成功与清理完成是不同结果。** blocked/partial 返回退出码 1，但不撤销或隐藏已保存的 ID/意图，也不重发 POST。`size_gb` 只是请求的预约注记；有效预约从调度器放置中排除整张 GPU，不代表实测释放容量。complete 也不保证 GPU 物理使用为零或没有其他用户进程。
- 回执说明保存时的事实；到达客户端前可能已过期或被其他调用删除。用 `status` 查询当前状态。客户端采用 150 秒的独立 `--wait` 响应等待，覆盖服务端默认/上限 120 秒的 reserve 期限并留出返回余量，可显式调整；它不改变后端期限。超时或丢失响应时意图可能已保存，先核对状态，不能以自动重试解决歧义。

TUI 输入框支持同一 reserve 命令与选项，沿用单写请求、迟到响应保护和执行后即时 GET 状态。有效预约的 GPU 标记为 `reserved for placement`；blocked/partial 回执仍显示并刷新当前预约。此切片未增加 unreserve、模型登记/删除接口或快捷键；完整 #11 的 live 预约/清理、TTL/reaper 与运行期验收继续单独跟进。

## 可选 TUI

包含 TUI 的发布包使用 `pip install 'llmsvc[tui]'` 安装。也可将仓库中的实际 `tui/` 目录复制到独立 `llm` 脚本旁，再安装 `textual>=0.70`。仅复制 `llm` 仍可使用全部 CLI 命令。

从源代码目录试用：

```sh
python -m pip install '.[tui]'
LLM_URL=http://scheduler:8011 python cli/llm
```

状态面板每 5 秒刷新，显示 GPU 占用条、内存预算和可选择的模型表。100×30 时事件流在模型表右侧；小于 100 列时上方改为单列、事件流移到模型表下方。空间不足时模型表保留名称、状态、GPU、显存和 PIN 标记，选中模型的来源、pin 到期与 owner 在下方显示。鼠标可滚动较长的详情、命令结果和事件。

- `↑` / `↓` 选择模型，`r` 刷新当前视图，`/` 聚焦命令框，`u` 在状态与 usage 之间切换，`?` 帮助，`q` 退出。用 `Tab` 聚焦详情或结果滚动区后，可用方向键翻动长内容；长错误不会限制在可见的两行内。
- `f` 聚焦命令框并预填 `free`，可补充 `--gpu` / `--need` / `--ram`；`p` 预填所选模型的 pin 命令，把光标放在空的 `--for` 参数处；`w` 预填所选模型的 wake 命令。**三者都不发请求，编辑/核对后按 Enter 才提交。** Pin 必须手动填入正时长（例如 `1h`），快捷键不提供默认或永久 pin；空时长/零时长沿用共享解析器报错。
- p/w 固定预填时的模型名，不因后续光标移动而改成另一模型；名称按 shell 引号规则保留并使用 `--` 分隔，支持空格、引号、Unicode、百分号和前导 `-`。没有选择、目标已从最新快照消失或选择失效时提示刷新/重新选择，不发送旧目标命令。服务器仍负责执行前的最新保护与状态检查。
- 命令框获得焦点时，`f` / `p` / `w` / `u` / `?` / `q` 等可打印按键都是输入文字；用 Tab 将焦点移出输入框后才能使用快捷键。已有非空草稿不会被快捷键覆盖；正在执行写请求时不会预填或排队另一个操作。RAM 确认框打开期间 f/p/w 不修改下面的命令，Esc 仍取消且不发请求。
- 输入框复用 CLI 解析器与执行路径，支持 `status`、`usage`、`pin MODEL --for 8h`、`unpin MODEL`、`free`、`wake MODEL`、`reserve --gpu N --size 80G --for 4h` 及各自选项。连接参数固定为启动时的配置；修改地址需退出后重新运行。
- Pin/unpin 成功后立即读取新状态并选中目标模型，PIN 标记与详情同步更新；写入前的旧查询不会覆盖该状态。结果保留服务端 owner/actor；刷新失败会单独说明，已成功的写入不会因此重试。同一时刻只执行一个写请求（pin/unpin/free/wake/reserve），不会把重复提交排队。
- usage 视图提供 7 天、30 天和返回状态按钮，每 5 秒刷新当前窗口；快速切换窗口时只排队读取最新选择，迟到结果不会覆盖新窗口。未知来源与不可用数据源的显示规则和 CLI 相同。
- 请求在后台线程执行，慢请求不会叠加轮询或阻塞按键。连接失败保留上一份快照，并显示错误；新快照到达后保留选中模型。
- 实际 `free --ram` 先弹出二次确认，显示原命令与停止/冷启动影响，默认聚焦取消；Esc 或取消按钮不发送写请求。`--dry-run` 直接显示预览。Free/wake 完成后立即刷新，部分结果与错误仍保留；后台执行期间 UI 与事件面板继续响应。
- Reserve 的预览、只读拒绝与实际回执语义见上节；unreserve、模型登记/删除尚未开放。不能用本客户端命令启用生产调度。

### Scheduler 与数据面事件流

事件读取在独立线程中进行，界面每 0.1 秒接收已到达的事件。面板标题为 `Events via scheduler`；每行显示 UTC 时间、`[scheduler]` 或 `[llama-swap]` 来源、全局 scheduler 事件 ID 与详情。日志按 scheduler 时间戳/ID 排序并着色，保留最近 200 条；单条可见文本最多 2048 字符，原始字段仍保存在这 200 条本地历史中。连接失败后以 1–30 秒退避重连，携带上次完整接收的 `since` / `Last-Event-ID`，重复事件不会再显示。退出界面会中断活动流并等待读取线程关闭。

当前 daemon 的事件历史有界且仅保存在内存中，游标不跨重启持久化。**确认 daemon 已重启后**按 `Ctrl+R`，仅在本地清空事件历史和游标，从 0 重新订阅。普通网络断线不会自动归零；当前接口没有可用于自动识别重启的实例标识。后端已淘汰的历史不能重建，ID 缺口会提示不可用事件数量；客户端 256 条投递队列超限也会明确提示丢失数量。

客户端始终只读取 scheduler 的 `/v1/events`，不直连 llama-swap。已接入桥接的 scheduler 可在部署方显式配置 `data_plane_events_enabled` 后，通过同一个 SSE 流转发 llama-swap 的脱敏状态、聚合在途计数、连接与错误摘要。该开关默认关闭；客户端不会启用它。没有转发事件不代表上游没有活动。一个 scheduler 管理一个共享订阅，而不是为每个 TUI 重建数据面连接。

- 数据面行保留 `source: llama-swap`、本地 `received_at` 和 `trusted_for_quiet: false`。蓝色 `data_plane_state` 是原始 starting/ready/stopping/stopped 摘要，**stopped 不代表 daemon 被硬停，也不证明显存或租约释放**；模型表只由 `/v1/state` 刷新。它们不能作为 reload/安静期的可信证据。
- `data_plane_dropped` 的黄色行显示本地条目丢弃计数及分项原因，明确上游丢失未知。`unlisted_model` 是允许列表之外模型的有意过滤；每次快照可能再次计数。`invalid_event` 包括模型负载或流的 framing/schema 错误；`buffer_full` 是本地缓冲容量；`limit_exceeded` 是本地来源限制。保留各自计数，不将过滤数叫做网络/传输丢失。
- 面板底部的 scheduler 历史缺口和客户端投递队列溢出提示仍单独计数，不与上述 relay 计数合并。上游 v252 丢失没有可测总量；本地接收/发布顺序也不是分布式时钟的全局顺序保证。
- 转发不包含请求正文、请求 ID、header/IP、模型显示名、原始异常或 logData。冷启动原始日志流仍不是本接口的交付内容。
- 关闭 TUI 只关闭自己的 scheduler SSE 读线程；daemon 负责共享数据面订阅与桥接生命周期。scheduler 正常停止时，客户端可接收其已成功发布的最后批次；忙锁导致仅写 journal 的未发布条目或断线期间无法投递的条目，不会被客户端声称收到。

CPU loopback 测试验证了两个来源从实际 HTTP、bridge、SSE 到 headless UI 的路径和清理；它不是生产 free 操作的一秒显示延迟证据。#23 的真实延迟及完整集成验收仍保持独立，桥接发布不授权 observer 或生产启用。

## 开发与验证

`cli/llm` 是唯一实现源；`cli/llm.py` 是指向它的符号链接。TUI 接收该脚本的解析器、客户端与执行函数，不维护另一套命令语义。`cli/tui` 是源代码布局的便利链接；复制安装时应复制实际 `tui/` 目录。

```sh
python -m pytest -q tests/test_llm.py
python -m pytest -q tests/test_llm_tui.py tests/test_tui.py
python -m pytest -q tests/test_llm_events.py tests/test_tui_events.py tests/test_tui_relay.py
python -m pytest -q tests/test_llm_usage.py tests/test_tui_usage.py
python -m pytest -q tests/test_llm_pin.py tests/test_tui_pin.py
python -m pytest -q tests/test_llm_reserve.py tests/test_llm_reserve_http.py tests/test_tui_reserve.py
python -m pytest -q tests/test_llm_actions.py tests/test_tui_actions.py
python -m pytest -q tests/test_tui_shortcuts.py
```

测试使用核心包的状态结构生成 JSON，并在临时 loopback HTTP 服务上验证单文件复制、无 site-packages 的 Python 启动、JSON 保真与窄屏。Python 3.10 可用时直接执行该解释器的复制测试；CI 使用 Python 3.10。客户端测试不访问生产服务或 GPU。

没有安装 Textual 时，headless UI 测试明确跳过；CLI 降级测试仍运行。界面测试使用 Textual 的 [headless Pilot](https://textual.textualize.io/guide/testing/) 检查尺寸、选择、定时刷新、输入与错误恢复。

SSE 解码按 [事件流格式](https://html.spec.whatwg.org/dev/server-sent-events.html#the-event-stream-format) 处理 UTF-8、BOM、注释、换行和空行提交，并按 scheduler 契约要求每个事件带匹配的数字 ID 与 JSON 记录。单行和单帧上限分别为 64 KiB、256 KiB；不完整尾帧不会提交游标。loopback 测试覆盖重连、明确重启后归零与退出清理。

Usage 测试使用临时 SQLite、实际 `ActivityReader` / `build_usage` 和 scheduler HTTP 服务，核对 CLI/TUI 显示与后端计数，并确认数据库字节不变。独立的历史线上对账证据保存在 [telemetry/live-final.json](../tests/fixtures/telemetry/live-final.json)：该快照记录的 31,605 请求、40,480,504 输入 token、4,847,906 输出 token 与当时的数据面 metrics 总数一致。这是 telemetry 的采样时刻证据；当前客户端测试证明显示与后端协议一致，完整 #25 的集成与来源对账验收仍需相应实机证据。

Pin/unpin 测试仅使用明确 opt-in 的临时 SQLite/loopback scheduler。覆盖伪造兼容标签后的权威 owner、编码模型名、空 DELETE body、写入陷阱下的零写入 dry-run，以及成功后的即时刷新与迟到响应。没有调用生产接口、模型执行器或 GPU；完整 #11/#24 验收保留在相应后续事项中。

Free/wake 验证使用实际 core HTTP、临时 SQLite 和显式模拟的模型状态变化；另一个 loopback 数据面夹具验证 unload/upstream 路径。包括超时传参、零写入预览、空 wake body、编码名称、HTTP 200 阻塞、未知/部分实测、二次确认、迟到结果和退出生命周期。模拟的 12 GiB GPU 净变化与 25 GiB 宿主变化只证明协议和显示，不是 GPU 实测。

快捷键测试通过 headless Pilot 实际按键，覆盖输入焦点、草稿保留、所选模型/失效目标、引号与前导连字符、空/零 pin 时长拒绝、显式提交与刷新、RAM 确认/取消，以及现有 usage/help/quit。它们复用上述临时服务，不进行实机模型操作。

Reserve 测试直接请求当前 SchedulerHTTPServer 的预览/默认只读 405 路径，并以临时 SQLite 字节、状态、事件和采集次数及写入陷阱验证零副作用。实际挂载的 POST/DELETE 通过 core 的临时 HTTP/SQLite 与模拟受管 unit 夹具验证：complete/blocked/partial、伪造标签后的权威 owner、保存 ID、不完整 evacuation 后保留意图、幂等删除、响应前删除/到期及真实保存后丢失回复不重试。TUI 也使用此实际 API 刷新预约；额外的响应夹具仅保留为无效响应/格式化单测，不代替实际链路。预览保留纯策略计划；它不是执行保证。实际执行时，未登记租约的 sleeping 模型会以 `unleased_model` 阻塞；即使此前预览成功，也不能跳过实际回执的 evacuation 状态。单文件 `-I -S`、非法参数、丢失响应无重试、线程屏障和退出回调也纳入检查。

<!-- Generated-By: Codex / gpt-6-astra -->
