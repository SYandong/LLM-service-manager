# AGENTS.md — 给人和 AI agent 的协作约束

本文件是仓库的工作规范。无论是人还是 AI agent，动手前先读完它，再读 `docs/ROADMAP.md` 与 `docs/DESIGN.md`。

## 1. 项目是什么

LLM-service-manager 是 **llama-swap 之上的控制面**：

- llama-swap + 官方 vllm-wrapper 负责按 `model` 路由、请求排队、sleep/wake。这些不重做。
- 本仓库负责 llama-swap 没有的部分：**多 GPU 放置、按显存/内存压力决定谁休眠、保底模型、用户显式意图（free / pin / reserve）**，以及一个终端 UI。
- 线上环境是一台 4×H200 NVL 的共享服务器，服务跑在名为 `llmsvc` 的 LXC 容器里。仓库是公开的，文档里不要出现密码、隧道主机名、个人姓名。

## 2. 目录约定

| 路径 | 内容 | 状态 |
|---|---|---|
| `docs/ROADMAP.md` | 唯一的路线图，milestone 与 issue 对应 | 权威 |
| `docs/DESIGN.md` | 架构、状态机、策略、API、CLI/TUI 设计与已定决策 | 权威 |
| `llmsvc/` | 新代码：调度器守护进程、策略、HTTP API | 已有实现；按 milestone 继续集成与验收 |
| `cli/llm` | 单文件 CLI（仅标准库），用户复制到自己容器里用 | 已有实现；命令随对应 API 交付 |
| `tui/` | 全屏 TUI（textual） | 已有实现；完整 M5 验收以 issue 为准 |
| `deploy/` | systemd unit、安装脚本、llama-swap 配置模板 | 已有工具；现场启用与观察验收单独记录 |
| `tests/` | pytest；策略回放夹具放 `tests/fixtures/` | 现有 |

## 3. 工作流：issue 先行，PR 合并

1. **任何改动先有 issue**。发现问题、想改设计、想加功能，都先开 issue（用模板），说明动机与验收标准。做的过程中遇到新问题，另开 issue，不要在当前 PR 里顺手扩 scope。
2. **从 issue 开分支**：`<type>/<issue号>-<短描述>`，type 取 `feat` / `fix` / `docs` / `ops` / `refactor` / `chore`；`chore` 仅用于发版，需 issue 记录授权。例：`feat/7-llm-status`、`chore/76-release-alpha1`。
3. **`main` 不直接 push**。所有改动走 PR，PR 描述用模板；完整验收满足后才使用自动关单语法，部分交付使用 `Refs #N`。不要在否定句中组合自动关单关键字与 issue 引用；平台仍可能建立关单关联。
4. **至少一位其他成员 review 通过再合并**，用 squash merge。作者不能自己批准自己。
5. **CI 绿了才能合**：`pytest` 全过。策略类改动必须附带回放测试。
6. 设计层面的变更（改状态机、改驱逐规则、改 API 形状）先开 `type:design` issue 讨论，达成一致后再改 `docs/DESIGN.md` 与代码，两者在同一个 PR 里。
7. 提交信息：一行英文祈使句概括（≤72 字符），空一行，正文可中文，引用 issue 号。发版提交使用 `chore(release): <version>`；发版准备修订可使用同一前缀加简短英文动作描述。

## 4. 模型水印（强制）

凡是由 AI agent 生成或大幅修改的内容，**必须标注 harness 名称加模型版本**，格式统一为：

```
Generated-By: <harness> / <model-id>
```

例如 `Generated-By: Claude Code / claude-fable-5-1`。要求：

- **commit**：正文末尾一行 `Generated-By:`；若 harness 还提供 `Co-Authored-By` 之类的行，一并保留。
- **PR 描述**：末尾一行。
- **issue 与评论**：末尾一行。
- **文档**（`docs/*.md`、`AGENTS.md`、README）：文件末尾一行 HTML 注释 `<!-- Generated-By: ... -->`；人工修改后如果 AI 部分仍占主体，保留水印并追加人工修订说明。
- **代码文件**：文件头部注释一行。人工重写后可以去掉。
- 找不到确切模型版本时写 harness 名和"unknown model"，不要猜一个版本号。

目的：出了问题能追溯是哪个模型在什么 harness 下写的；review 时知道哪些地方需要多看一眼。

## 5. 编码约束

- Python **3.10**（llmsvc 容器里的解释器版本），不要用更高版本才有的语法。
- `cli/llm` 只允许标准库，保证任何容器复制过去就能跑。调度器可以依赖 `PyYAML`、`rich`；TUI 用 `textual`。依赖写进 `pyproject.toml`。
- **策略是纯函数**：输入"GPU 状态、模型状态、活动统计"三个数据结构，输出"动作列表"。不在策略函数里调 `subprocess`、发 HTTP。执行层单独一层。
- 所有会改变线上状态的动作（sleep、stop、放置、改配置）必须支持 `--dry-run`，并写一条结构化日志到 journal。
- 不硬编码 IP、端口、路径，全部走配置文件，配置文件有带注释的示例。
- 中文注释可以，标识符与日志用英文。

## 6. 线上安全红线

- **不要在运行期改 `/etc/llama-swap/config.yaml`**，除非通过调度器的"安静时刻"机制（零在途请求时落盘），或本节限定的显式维护／首次受管启动协议。常规 llama-swap reload 会让所有醒着的模型进入 sleep，并中断在途请求；维护模式不得绕过其独立的实例退出、采用与结清证明。
- 调度器只允许操作 `vllm-*.service` 这些 transient unit 和 llama-swap 的 `/api/models/unload/{id}`、`/upstream/{id}/`。不碰其他用户的进程。
- **显式维护模式的限定例外（#60）**：仅当已有该次维护的操作授权，且同时配置 `catalog_mode: maintenance`、`catalog_enabled: true`、`model_actions_enabled: true`、`read_only: false`、绝对路径的 `maintenance_command` 和已核实的固定私有 profile 时，才可按 [DESIGN 的显式实例维护协议](docs/DESIGN.md#显式实例维护模式60) 扩展上述操作范围。适配器仅可对 profile 固定的 llama-swap unit（绑定 fragment SHA-256、可执行文件和当前实例，`Restart=no`）的精确主进程发送受身份保护的 SIGTERM，并启动该固定 unit；仅可创建和管理本次维护自有的 `llmsvc-maint-helper-*` oneshot jobs，以已核实的 helper/后端身份完成 level-1 sleep 后停止对应 wrapper。后端 unit 仍限于 `vllm-*.service`，不包括其他用户的进程。默认模型后端只允许 level-1 sleep，不允许硬停；wrapper 或代理退出不能当作后端退出或释放记账的证明。
- 该例外仍要求维护 preflight 及 `stop_old` / `stop_candidate` 前重新取得新鲜的零在途观察，保留 `reload_blockers` 的默认模型、pin 和 RAM 保护；未知或失败的实例、helper、清理及回滚结果保留持久化屏障与当前账户，不重发不明结果的动作。它不把两次零观察变成连续 quiet 证明，常规热重载仍须满足自身连续 quiet 门槛。只读自动升级器不得启用维护模式；每次实际维护按本节的生产操作记录规则留痕，并须通过当前配置、身份、保护与回滚条件核对。
- **首次受管启动的限定例外（#201）**：仅在已有该次迁移授权，且 `bootstrap_enabled`、`placement_enabled`、`catalog_enabled`、`model_actions_enabled` 均为 true、`read_only: false`、`catalog_mode: maintenance`、真实可写账本及经过核实的固定 bootstrap 配置同时具备时，才可按 [DESIGN 的首次受管启动协议](docs/DESIGN.md#首次默认模型的受管-bootstrap201) 执行迁移。固定配置须约束绝对路径的迁移命令、launcher 及其配置摘要、manifest 摘要、源配置前后摘要和唯一默认模型。先持久化声明及每次副作用的提交记录；只可改动已核实 manifest 列明的自有 source unit、native 配置、受管 launcher/helper/profile 及独立环境文件，不修改其他 unit、其他用户文件或固定 llm 挂载。初次 stage 仅把原 source unit 的 Restart 改为 no 并加入独立 attempt EnvironmentFile，保留其余字节；允许为该定义执行 daemon-reload，但必须重绑同一进程与批准的 fragment，并重验零在途及相关旧后端/任务缺席后，才可停止该精确 source。其余文件的原子暂存须在旧 source、actors、cgroup、相关后端及 helper 已被正面确认退出或缺席后进行；native 环境不得冒充 daemon 环境。
- bootstrap 仅通过既有 place／lease-aware launcher／health／confirm 路径启动并确认该默认模型，完整预算和声明关联须原子保留，沿用 unit/token/GPU/预留期限等检查；不收养旧进程、伪造账户、移除默认 preload 或驱逐其他模型。源暂时缺席不产生公开的零在途或健康值，只有绑定新鲜缺席证明的受限内部路径可使用独立物理观察。确认默认账户后才可提交 source 激活，并核对实际新实例、配置、wrapper 就绪及同一后端绑定；不明启动不重发。启动前的文件回滚也须证明相关资源缺席或原实例确实未停止且保持安全，不启动无租约旧 preload、不硬停运行中的默认模型、不恢复旧账本。dry-run 不建立账本或执行迁移；只读自动升级器不得启用 bootstrap。现场预检、已审核产物、回滚和操作留痕条件仍按本节执行，测试回执不代替现场证明。
- 默认模型永远不被硬停；pin 住的模型不被任何自动策略碰。
- 新版本先在容器内的另一个端口（历史上用 8010）验证，再切生产。切换步骤写在 `deploy/` 的脚本里，可回滚。
- 对生产做手工操作（stop unit、改 TTL）要在 issue 或 PR 里留记录。

## 7. 给 AI agent 的补充

- 进容器看现场：`sudo lxc exec llmsvc -- bash -lc '<cmd>'`（宿主机上执行）。只读命令随便跑；会改状态的命令先向人确认。
- 线上真实数据源：`journalctl -t vllm-launch -t vllm-reaper`、`/var/lib/llama-swap/activity.sqlite`、`systemctl list-units 'vllm-*'`、`nvidia-smi --query-compute-apps`。
- 旧单后端代理源码已按 #27 退役；不要恢复旧命令、示例或依赖。历史发布与路线图记录保留，但不是现行部署入口。现场的停用定义、归档副本和其他用户服务仍按各自操作授权处理。
- 完成一个 issue 的标准：验收标准逐条满足、测试通过、`docs/` 与代码一致、PR 描述写明验证方式、水印齐全。
- 交接或让出回合前，核对实际 PR/commit 状态并更新任务记录；已合并依赖不得继续标为待审核。目录已存在、部分 PR 已合并与完整验收完成分别记录。

<!-- Generated-By: Claude Code / claude-fable-5-1 -->
<!-- Generated-By: Codex / gpt-6-astra -->
