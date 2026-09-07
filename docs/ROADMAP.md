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
- #11 pin / unpin 与 reserve（M2 只验收 pin 生效、reserve 记账与挪走睡着的模型；reserve 对放置的影响在 #14 验收，因为 `/v1/place` 到 M3 才接管冷启动）
- #12 内存预算取代 2 小时硬停；吸收 vllm-reaper
- #13 调整 concurrencyLimit，消除批处理 429

### M3 放置与压力调度

- #14 vllm-launch 改为薄客户端：`POST /v1/place` 与租约协议（含 reserve 期间不选该卡的验收）
- #15 放置算法：先判可行、每卡最小代价腾位集合、保护默认/pin/在用
- #16 共享卡压力驱动 sleep：外部进程检测与分卡 TTL
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

<!-- Generated-By: Claude Code / claude-fable-5-1 -->
