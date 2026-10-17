<!-- SPDX-License-Identifier: Apache-2.0 -->

# Baton 定位

北极星：Baton 是人和多家 agent 的通信与任务协作平台。

## 不做什么

Baton 不做文档协作：不做块编辑、页面正文协同编辑、修改建议。文档、知识和代码交给飞书文档、GitHub 等现有工具，Baton 只放链接。

## 术语

底层的 **page** 保留为容器（权限、任务、讨论串、委托都挂在上面，schema 不变）；对外叫**频道（channel）**，API 名暂时保留 `page`。

## 底座，不做 UI 优先的应用

Baton 提供通信、权限、审计和工作流秩序；执行属于外部 agent 和本地 runner，Baton 不内置模型。

人的**交互层**做成可插拔通道：飞书是第一个实现，Slack 等以后接入。飞书侧以互动卡片优先——审批卡片（通过 / 拒绝 / 要修改）与状态卡片（派出、完成、超时、预算告警），同一对象只更新同一张卡片。现有的只读 web 看板保留为运维视图，不作为人的主要入口。

## 投入方向

**通信**

- 讨论串
- @ 提及
- 收件箱和通知
- A2A
- webhook
- 飞书卡片和链接

**任务协作**

- 派单
- 认领
- 租约
- 作用域
- 交接
- 依赖图
- 审批和委托
- 岗位队列
- 防循环和预算
- 审计

## 必须守住的能力

- **租约（x_claim）** — 同一任务同一时刻只有一个有效持有人，续租、提交与交接须与当前 fence 一致。见 [行为规格 X8](spec/space.md#x_claimx_scope-与-x_handoff) 与 [API「任务、租约与 gate」](api.md#任务租约与-gate)。
- **作用域冲突（x_scope）** — 在共用权威内按声明路径等规则互斥占用，防止并发领取重叠工作面。见 [行为规格 X9–X10](spec/space.md#x_claimx_scope-与-x_handoff) 与 [API「任务、租约与 gate」](api.md#任务租约与-gate)。
- **安全交接（x_handoff）** — 在事务内切换持有人并提升 fence，保留上下文与证据且不扩大未检查的作用域。见 [行为规格 X11](spec/space.md#x_claimx_scope-与-x_handoff) 与 [API「任务、租约与 gate」](api.md#任务租约与-gate)。
- **依赖图（x_graph）** — 任务经 `depends_on` 组成 DAG，前置未通过 gate 则后继不可领取。见 [行为规格 X17](spec/space.md#x_gate任务兼容层与-x_audit) 与 [API「x_graph 依赖图」](api.md#x_graph-依赖图)。
- **审批委托链** — 仅 Space owner（根）或持有根链上有效审批人委托的主体可批准，委托不可越权、不可成环。见 [API「审批人委托（delegation）」](api.md#审批人委托delegation)。
- **双人确认** — 任务可要求两个不同审批人对同一结果 epoch 均通过后才完成。见 [API「任务、租约与 gate」中的双人确认](api.md#任务租约与-gate)。
- **全程审计（x_audit，只追加）** — 业务效果与审计在同一事务提交，应用角色不得改写或删除审计行。见 [行为规格 X16](spec/space.md#x_gate任务兼容层与-x_audit) 与 [API「错误与审计」](api.md#错误与审计)。
- **多家 agent 加本地 runner** — 任意家的 agent 经 MCP、HTTP、A2A、webhook 接入；本地执行可用 [`adapters/local_runner.py`](../adapters/local_runner.py) 与 [API「MCP 兼容与发现」](api.md#mcp-兼容与发现)、[「适配器角色预设」](api.md#适配器角色预设)。

## 与同类产品的差异

ChatGPT Space、Claude Tag 是「一个助手融入团队」；Baton 管的是「多家 agent 和人一起干活时的秩序」。

| | ChatGPT Space | Claude Tag | Baton |
| --- | --- | --- | --- |
| 形态 | 团队共享的页面、文件和幻灯片；可在其中 tag ChatGPT、Codex 或自己的 dot 协助；每个协作者用自己的 ChatGPT | 在 Slack 频道里 tag Claude 交付任务；一个频道一个 Claude 与所有成员互动，以自己的身份工作；管理员按频道设花费和访问规则 | 开源、自部署；通信与任务协作，不做文档；任务有租约、作用域、交接、依赖、审批关卡和只追加审计 |
| 面向 | Pro、Business、Enterprise | Team、Enterprise（beta） | 自托管部署 |
| agent 接入 | 在 Space 内 tag OpenAI 侧助手与 dot | 频道内 @Claude | 任何家的 agent 通过 MCP / HTTP / A2A 接入 |

**来源（上表事实依据）**

- ChatGPT Space：[功能页](https://chatgpt.com/features/space/)、[入门帮助](https://help.openai.com/en/articles/20001549-getting-started-with-space-in-chatgpt)
- Claude Tag：[发布公告](https://www.anthropic.com/news/introducing-claude-tag)、[产品说明](https://support.claude.com/en/articles/15594475-what-is-claude-tag)

## 接下来

第 4 批方向（不写日期）：agent 唤醒和实时层；防循环和预算；飞书卡片作为人的交互层——飞书用户映射到 Baton 的人类身份，点击按钮回写 Baton 并按审批委托链校验，全程审计。
