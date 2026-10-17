<!-- SPDX-License-Identifier: Apache-2.0 -->

# Baton 定位

北极星：Baton 是人和多家 agent 的通信与任务协作平台。

## 不做什么

Baton **不做文档协作**（产品方向）：不以块编辑、页面正文协同编辑或修改建议为主战场；文档、知识和代码交给飞书文档、GitHub 等现有工具，Baton 只放链接。

**DEP-1（文档协作 API，分阶段废弃）：** 历史原因，[`api.md`](api.md) 与 [`spec/space.md`](spec/space.md) 仍保留 `create_block` / `update_block`、提案审阅（`proposal_*`）及块正文存储（见 [`architecture.md`](architecture.md) 数据模型）。这些能力**已标为废弃**，新集成不应依赖；将分阶段从对外契约与实现中移除。定位上的「不做文档协作」指目标形态与新增用法，不是声称库里已无块或提案数据。

## 术语

底层的 **page** 保留为容器（权限、任务、讨论串挂在上面，schema 不变）；**审批人委托（`x_delegation`）按 Space 范围**（`space_id` 为空表示根拥有的全部 Space），无 page 外键，页面分享不带来批准权。对外叫**频道（channel）**，API 名暂时保留 `page`。

## 底座，不做 UI 优先的应用

Baton 提供通信、权限、审计和工作流秩序；执行属于外部 agent 和本地 runner，Baton 不内置模型。

人的**交互层**设计为可插拔通道（**planned**，本仓库尚无飞书/Slack 桥或卡片回写代码；能力表见 [README](../README.md) 与 [路线图](design/roadmap.md)）：

| 状态 | 内容 |
| --- | --- |
| **现已提供** | MCP/HTTP、webhook、`wait_inbox`、任务与讨论串 API、审批关卡与委托链（经 API/CLI）、只读 web 看板（运维视图，非人的主入口）；**A2A v1.0 公共接口**（签名 Agent Card、`SendMessage` / 流式等，见 [spec A39](spec/space.md) 与 `bridge/a2a_public.py`） |
| **计划（第 4 批 / W4 等）** | **飞书**为第一个通道实现：互动卡片（审批：通过 / 拒绝 / 要修改；状态：派出、完成、超时；**预算告警**待防循环与预算能力落地后随状态卡片一并提供），同一对象只更新同一张卡片；飞书用户映射到 Baton 人类身份，按钮回写并按委托链校验、全程审计。**Slack** 等后续接入。 |

防循环与预算在 README 能力表中仍为**计划**（审批关卡已实现）；与上表「预算告警」卡片一致，均属未来工作，不在此文档中表述为已上线。

**A2A 交付状态（避免误读）：** 上表「A2A v1.0 公共接口」以规格 **A39** 与 `bridge/a2a_public.py` 为准，属**已实现**。 [README](../README.md) 能力表「A2A 集成」一行仍写「计划」，措辞未与 A39 同步；本文件不重复「部分已实现」说法，也不把 A2A 与 webhook 混为一档。

## 投入方向

下列为产品投入全景（**未标注「已实现」的项以 README 能力表为准**）。

**通信**

- 讨论串、@ 提及、收件箱和通知（已实现）
- webhook（已实现，见 README）
- A2A v1.0 公共接口（已实现，见 spec A39；README 能力表一行待同步）
- 飞书卡片和链接（**planned**，第 4 批）

**任务协作**

- 派单、认领、租约、作用域、交接、依赖图、审批和委托、审计（已实现，见规格与 API）
- 岗位队列（按岗位/角色的就绪与派单，**planned**；owner 产品用语，README 与 [路线图](design/roadmap.md) 尚未单独立项）
- 防循环和预算（**planned**）

## 必须守住的能力

- **租约（x_claim）** — 同一任务同一时刻只有一个有效持有人，续租、提交与交接须与当前 fence 一致。见 [行为规格 X8](spec/space.md#x_claimx_scope-与-x_handoff) 与 [API「任务、租约与 gate」](api.md#任务租约与-gate)。
- **作用域冲突（x_scope）** — 在共用权威内按声明路径等规则互斥占用，防止并发领取重叠工作面。见 [行为规格 X9–X10](spec/space.md#x_claimx_scope-与-x_handoff) 与 [API「任务、租约与 gate」](api.md#任务租约与-gate)。
- **安全交接（x_handoff）** — 在事务内切换持有人并提升 fence，保留上下文与证据且不扩大未检查的作用域。见 [行为规格 X11](spec/space.md#x_claimx_scope-与-x_handoff) 与 [API「任务、租约与 gate」](api.md#任务租约与-gate)。
- **依赖图（x_graph）** — 任务经 `depends_on` 组成 DAG，前置未通过 gate 则后继不可领取。见 [行为规格 X17](spec/space.md#x_gate任务兼容层与-x_audit) 与 [API「x_graph 依赖图」](api.md#x_graph-依赖图)。
- **审批委托链** — 仅 Space owner（根）或持有根链上有效审批人委托的主体可批准，委托不可越权、不可成环；**非根**还须是任务配置的审阅人（reviewer）并对任务页面具**当前 edit 权限**，且除根在显式 `self_approval` 外不得批准自己提交或执行的结果（含该 epoch 的 claim 持有人）。见 [行为规格 X13 / X25](spec/space.md#x_gate任务兼容层与-x_audit)、[API「审批关卡」与「审批人委托（delegation）」](api.md#任务租约与-gate)。
- **双人确认** — 任务可要求**两个不同审批人**（不同 principal **且**不同 account）对同一 `result_epoch` 均通过后才完成。见 [行为规格 X25](spec/space.md#x_gate任务兼容层与-x_audit)、[API「双人确认」](api.md#任务租约与-gate)。
- **全程审计（x_audit，只追加）** — 业务效果与审计在同一事务提交，应用角色不得改写或删除审计行。见 [行为规格 X16](spec/space.md#x_gate任务兼容层与-x_audit) 与 [API「错误与审计」](api.md#错误与审计)。
- **多家 agent 加本地 runner** — 任意家的 agent 经 MCP、HTTP、A2A、webhook 接入；本地执行可用 [`adapters/local_runner.py`](../adapters/local_runner.py) 与 [API「MCP 兼容与发现」](api.md#mcp-兼容与发现)、[「适配器角色预设」](api.md#适配器角色预设)。

## 与同类产品的差异

ChatGPT Space、Claude Tag 是「一个助手融入团队」；Baton 管的是「多家 agent 和人一起干活时的秩序」。

| | ChatGPT Space | Claude Tag | Baton |
| --- | --- | --- | --- |
| 形态 | 团队共享的页面、文件和幻灯片；可在其中 tag ChatGPT、Codex 或自己的 dot 协助；每个协作者用自己的 ChatGPT | 在 Slack 频道里 tag Claude 交付任务；一个频道一个 Claude 与所有成员互动，以自己的身份工作；管理员按频道设花费和访问规则 | 开源、自部署；通信与任务协作，不做文档（DEP-1）；任务有租约、作用域、交接、依赖、审批关卡和只追加审计 |
| 面向 | Pro、Business、Enterprise | Team、Enterprise（beta） | 自托管部署 |
| agent 接入 | 在 Space 内 tag OpenAI 侧助手与 dot | 频道内 @Claude | 任何家的 agent 通过 MCP / HTTP / A2A 接入 |

**来源（上表事实依据）**

- ChatGPT Space：[功能页](https://chatgpt.com/features/space/)、[入门帮助](https://help.openai.com/en/articles/20001549-getting-started-with-space-in-chatgpt)
- Claude Tag：[发布公告](https://www.anthropic.com/news/introducing-claude-tag)、[产品说明](https://support.claude.com/en/articles/15594475-what-is-claude-tag)

## 与 WAG、WorkGoal 的边界

**说明（owner 口径，2026-10-17）：** 下表为生态分工的**目标陈述**，不是对本仓库或外部产品的实测结论；WorkGoal 仅在此文档出现。 [space-replica.md](design/space-replica.md) 第 48 行将「WAG 复刻 Agents API」写作设计前提，**未**单独标「尚未核实」；同文件仅第 170 行 owner 输入的 Cedar 说法标为尚未核实——勿把后者概括为「WAG 相关说法均已分级为未核实」。勿与 [spec/space.md](spec/space.md) 中已核实的 Space 证据混为一谈。

| | WAG | WorkGoal | Baton |
| --- | --- | --- | --- |
| 层级（目标） | agent 运行时（owner 表述为对标 OpenAI Agents API，**未在此核实**） | 项目层（owner 表述为参照 Claude Projects，**未在此核实**） | 通信与任务协作 |
| 职责（目标） | 会话、工具、记忆库、例程和预算 | 案件或委托即项目；共享知识库、项目指令、项目记忆、线程和例程；文档与知识在这一层 | 跨项目、跨机器，协调多家 agent 与人；**不以保存文档或知识为目标**，只引用 WorkGoal 项目、GitHub 和飞书文档的链接；协调与审批；长期记忆归 WorkGoal 或 WAG 的记忆库 |

WorkGoal 为私有仓库，此处只称名、不提供链接。

**接口约定（planned）：** Baton 任务可携带 `project_ref`（WorkGoal 项目 id 或 URL），只引用，不复制内容。

## 接下来

[路线图](design/roadmap.md)中的计划工作（不写日期，**均为计划**），按里程碑分开表述，勿与上文「计划（第 4 批 / W4）」飞书卡片表混为一档：

- **W2（W2-2 R1 safeguards）**：防循环和预算
- **W3（如 W3-2 实时层、W3-4 唤醒适配器等）**：agent 唤醒与实时层
- **W4（W4-1 移动/聊天桥）**：飞书互动卡片作为人的交互层（见上文「现已提供 / 计划」表）；**Slack** 等通道排在飞书之后
