# CLI 使用说明

`cli/llm` 是 Python 3.10+ 标准库脚本。只复制这一个文件即可运行，无需安装仓库、`rich` 或 `textual`。当前切片实现只读 `status`，只请求 scheduler 的 `GET /v1/state`。

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
- 无参数当前等同于 `status`；管道与非 TTY 也输出文本。可选 TUI 及命令操作在后续客户端切片接入。

## 开发与验证

`cli/llm` 是唯一实现源；`cli/llm.py` 是指向它的符号链接，供后续 TUI 复用 `build_parser`、`SchedulerClient` 和 `format_status`。复制独立脚本只需要 `cli/llm`。

```sh
python -m pytest -q tests/test_llm.py
```

测试使用核心包的状态结构生成 JSON，并在临时 loopback HTTP 服务上验证单文件复制、无 site-packages 的 Python 启动、JSON 保真与窄屏。Python 3.10 可用时直接执行该解释器的复制测试；CI 使用 Python 3.10。客户端测试不访问生产服务或 GPU。

<!-- Generated-By: Codex / gpt-6-astra -->
