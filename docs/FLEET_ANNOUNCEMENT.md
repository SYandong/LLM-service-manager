# Fleet announcement draft (#310)

Only send after the deployment and shadow record have been accepted. Replace
any unresolved deployment detail before sending; this file is not an announcement
that the feature is already live.

## 中文

我们准备提供全员推理服务视图。上线后，用 `llm` 打开面板，或用
`llm status` 查看各容器服务、GPU 显存占用和最近活动；按卡视图也展示其他
任务的归属与显存，不展示它们的命令行。共享模型的旧状态视图用
`llm status --shared` 查看。

空闲上限默认六小时，只做展示，不会自动停止你的服务。需要继续占用时，
可以对自己的服务声明期限和原因，例如
`llm claim SERVICE_ID --until +3d --reason "ongoing experiment"`；最长七天，
结束后用 `llm unclaim CLAIM_ID` 撤销。声明不预留 GPU，也不会干预其他任务。
超限时管理员会先联系服务主人，再协调安排。

指标每分钟采样。未知、陈旧和采集中断会明确显示；活动分钟是采样估计，
不是连续推理时间。没有观测到活动不代表服务从未被使用。
共享服务管理命令继续可用；旧面板暂时可用 `llm legacy-tui` 打开，删除时点
另行通知。

## English

We plan to offer a fleet view of independently hosted inference services. Once
it is deployed, open `llm` or run `llm status` to see service ownership, GPU
memory and recent activity. The GPU view also shows ownership and memory for
other workloads, without their command lines. Use `llm status --shared` for the
existing shared-model view.

The default idle limit is six hours and is informational. Services are not
stopped automatically. Declare continued use of your own service with
`llm claim SERVICE_ID --until +3d --reason "ongoing experiment"`, for up to
seven days; revoke it with `llm unclaim CLAIM_ID` when finished. Claims do not
reserve GPU capacity or interfere with other workloads. The administrator will
contact the owner before coordinating an over-limit service.

Metrics are sampled every minute. Unknown data, stale snapshots and gaps stay
visible. Active minutes are a sampling estimate, not continuous inference time;
no observed activity does not mean a service has never been used. Shared-model
management commands remain available. The old panel is temporarily accessible
as `llm legacy-tui`; its removal date will be announced separately.

<!-- Generated-By: Codex / unknown model -->
