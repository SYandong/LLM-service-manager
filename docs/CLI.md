# CLI 使用说明

`cli/llm` 是 Python 3.10+ 标准库脚本。只复制这一个文件即可运行，无需安装仓库、`rich` 或 `textual`。当前提供只读 `status` 和可选全屏面板，只请求 scheduler 的 `GET /v1/state` 与 `GET /v1/events`。

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
# 每次 HTTP 请求的超时秒数，必须为正数；默认 10。
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

## 可选只读 TUI

包含 TUI 的发布包使用 `pip install 'llmsvc[tui]'` 安装。也可将仓库中的实际 `tui/` 目录复制到独立 `llm` 脚本旁，再安装 `textual>=0.70`。仅复制 `llm` 仍可使用所有只读 CLI 功能。

从源代码目录试用：

```sh
python -m pip install '.[tui]'
LLM_URL=http://scheduler:8011 python cli/llm
```

状态面板每 5 秒刷新，显示 GPU 占用条、内存预算和可选择的模型表。100×30 时事件流在模型表右侧；小于 100 列时上方改为单列、事件流移到模型表下方。空间不足时模型表保留名称、状态、GPU 和显存，选中模型的来源与 pin 在下方显示。鼠标可滚动较长的详情、命令结果和事件。

- `↑` / `↓` 选择模型，`r` 刷新，`/` 聚焦命令框，`?` 帮助，`q` 退出。用 `Tab` 聚焦详情或结果滚动区后，可用方向键翻动长内容；长错误不会限制在可见的两行内。
- 输入框复用 CLI 解析器，支持 `status`、`status --json` 和 `--help`。连接参数固定为启动时的配置；修改地址需退出后重新运行。
- 请求在后台线程执行，慢请求不会叠加轮询或阻塞按键。连接失败保留上一份快照，并显示错误；新快照到达后保留选中模型。
- 当前是只读界面。`free`、`pin`、`wake`、`reserve` 操作与对应快捷键，以及 usage 视图在各自 issue 中接入；当前不会发送写请求。

### Scheduler 事件流

事件读取在独立线程中进行，界面每 0.1 秒接收已到达的事件。日志按 UTC 时间排序并按事件种类着色，保留最近 200 条；单条可见文本最多 2048 字符。连接失败后以 1–30 秒退避重连，携带上次完整接收的 `since` / `Last-Event-ID`，重复事件不会再显示。退出界面会中断活动流并等待读取线程关闭。

当前 daemon 的事件历史有界且仅保存在内存中，游标不跨重启持久化。**确认 daemon 已重启后**按 `Ctrl+R`，仅在本地清空事件历史和游标，从 0 重新订阅。普通网络断线不会自动归零；当前接口没有可用于自动识别重启的实例标识。后端已淘汰的历史不能重建，ID 缺口会提示不可用事件数量；客户端 256 条投递队列超限也会明确提示丢失数量。

当前面板只读取 scheduler 的 `/v1/events`，不直连 llama-swap。两来源合流仍等待 scheduler 的数据面事件转发接口；合成事件的显示时延测试不是实际 free 操作或双来源验收。

## 开发与验证

`cli/llm` 是唯一实现源；`cli/llm.py` 是指向它的符号链接。TUI 接收该脚本的解析器、客户端与执行函数，不维护另一套命令语义。`cli/tui` 是源代码布局的便利链接；复制安装时应复制实际 `tui/` 目录。

```sh
python -m pytest -q tests/test_llm.py
python -m pytest -q tests/test_llm_tui.py tests/test_tui.py
python -m pytest -q tests/test_llm_events.py tests/test_tui_events.py
```

测试使用核心包的状态结构生成 JSON，并在临时 loopback HTTP 服务上验证单文件复制、无 site-packages 的 Python 启动、JSON 保真与窄屏。Python 3.10 可用时直接执行该解释器的复制测试；CI 使用 Python 3.10。客户端测试不访问生产服务或 GPU。

没有安装 Textual 时，headless UI 测试明确跳过；CLI 降级测试仍运行。界面测试使用 Textual 的 [headless Pilot](https://textual.textualize.io/guide/testing/) 检查尺寸、选择、定时刷新、输入与错误恢复。

SSE 解码按 [事件流格式](https://html.spec.whatwg.org/dev/server-sent-events.html#the-event-stream-format) 处理 UTF-8、BOM、注释、换行和空行提交，并按 scheduler 契约要求每个事件带匹配的数字 ID 与 JSON 记录。单行和单帧上限分别为 64 KiB、256 KiB；不完整尾帧不会提交游标。loopback 测试覆盖重连、明确重启后归零与退出清理。

<!-- Generated-By: Codex / gpt-6-astra -->
