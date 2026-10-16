<!-- SPDX-License-Identifier: Apache-2.0 -->

# Baton Space 行为规格

状态：`SPEC_READY`。本版冻结已核实的产品语义和 Baton 存储/API 不变量，供实现与独立验收使用；不代表实现已通过，也不代表 ChatGPT Space 界面等价性已验收。核验日期：2026-10-17。

范围：替换 Agent Mail 为 Baton 自有的空间、页面、评论、提及、任务与协作协议。采用 PostgreSQL，优先实现数据库与 HTTP 行为；不连接真实 ChatGPT 空间，不做同步、导入既有账户或协议互操作。

## 证据与标记

每条规则区分三个状态：**官方**仅表示链接原文明确陈述的产品行为；**x_扩展**表示 Baton 自行设计的协议或行为；**待界面实测**表示允许来源不足以确定，当前不得宣称复刻完成。

已直接抓取并逐条核验的允许来源：

- [S1：Space 概览](https://learn.chatgpt.com/docs/space/)。
- [S2：Pages](https://learn.chatgpt.com/docs/space/pages)。
- [S3：Collaboration](https://learn.chatgpt.com/docs/space/collaboration)。
- [S4：Agents](https://learn.chatgpt.com/docs/space/agents)。
- [S5：Getting started](https://learn.chatgpt.com/docs/space/getting-started)。
- [S6：DevDay 2026 回顾](https://learn.chatgpt.com/docs/whats-new/devday-2026#chatgpt-space)。

按任务要求另启动独立 Codex CLI 研究：全局 `--search`，`exec --ignore-user-config --ignore-rules --ephemeral --sandbox read-only`，禁止读取凭据、个人配置或真实空间，输出保存在仓库外。本版正文以作者已核验的上述原文为依据；CLI 完成报告尚待收齐后交叉核对，不能预先标成研究完成。

这些产品页面没有给出可据以实现的 Space CRUD HTTP API、数据库模型或 wire schema。本规格不把这一检索范围内的缺失推断成“任何地方都不存在 API”；所有下述 HTTP、JSON、数据库、状态机与 MCP 映射均属于 **x_扩展**。概览及 DevDay 中的其他产品链接不构成 Space API 证据。[官方来源范围：S1](https://learn.chatgpt.com/docs/space/)、[S6](https://learn.chatgpt.com/docs/whats-new/devday-2026#chatgpt-space)。

## 已核实的官方行为

**O1 · 官方 · 空间与页面。** Space 汇集可访问的页面、文件和共享工作；一个 space 可按主题或团队组织页面。页面可以独立存在，也可有子页面。Baton 的同名实体必须保留这一区别，不把 workspace、space、page 合并为一个权限对象。[S1](https://learn.chatgpt.com/docs/space/)、[S5](https://learn.chatgpt.com/docs/space/getting-started#group-related-pages-in-a-space)。

**O2 · 官方 · 页面块。** 页面由块组织。文档明确列出标题、段落、列表、清单、表格、代码、强调、图片、文件、子页面/页面链接、文件/页面/聊天引用、可视化、Prompt、Task、Agent Instructions。块名称不是公开 JSON schema；Baton 的类型名、字段、顺序编码和执行方式均另标 x_扩展。不能把一个任务块的存在等同于某任务已经被调度。[S2](https://learn.chatgpt.com/docs/space/pages#add-content-with-the-insert-menu)、[S4](https://learn.chatgpt.com/docs/space/agents#verify-recurring-work-separately)。

**O3 · 官方 · 访问级别。** view 用于阅读，comment 用于阅读并反馈，edit 用于修改内容。修改他人的访问权限还需要管理分享的权限，不能仅凭“可编辑内容”推断“可分享”。可用接收者及分享方式受账户、workspace 和接收者类型影响；Team 邀请可能仅有 view/edit，不提供 comment。[S3](https://learn.chatgpt.com/docs/space/collaboration#choose-an-access-level)、[S6](https://learn.chatgpt.com/docs/whats-new/devday-2026#chatgpt-space)。

**O4 · 官方 · 继承与移除。** 共享 Space 的访问权适用于其页面；页面分享也赋予对子页面的访问权。直接授权和继承授权应可区分。移除页面的直接邀请，不能撤销仍来自父页面或 Space 的继承权；需要在相应来源修改授权。[S3](https://learn.chatgpt.com/docs/space/collaboration#understand-inherited-access)。

**O5 · 官方 · 评论与提及。** 可在页面选定文本后评论，或用评论讨论问题、请求修改。对人的评论/提及是给对方的反馈；在页面内或评论中以 `@ChatGPT`/`@dot` 加请求，是请该 agent 行动。原文要求检查接收者并审阅回复及页面改动，不支持“每个 @ 字符都必然执行任务”的推断。[S2](https://learn.chatgpt.com/docs/space/pages#work-with-selected-text)、[S4](https://learn.chatgpt.com/docs/space/agents#choose-how-to-ask-for-help)。

**O6 · 官方 · 提案与审阅。** 选区的 Ask for change 在可用时用于请求修订，应先审阅提议的修改再接受。Generate 文本可接受、继续要求修改或撤销。原文没有定义这几个动作的事务顺序、冲突处理或撤销范围。[S4](https://learn.chatgpt.com/docs/space/agents#revise-a-launch-brief)、[S2](https://learn.chatgpt.com/docs/space/pages#draft-content-with-generate)。

**O7 · 官方 · 贡献归属。** 页面在功能可用时提供 Show attribution，用于查看贡献者。原文没有保证逐字符、逐块、逐操作或永久历史的具体粒度。Baton 的归属字段及审计记录不得冒充该未公开数据模型。[S2](https://learn.chatgpt.com/docs/space/pages#comment-and-collaborate)。

**O8 · 官方 · Agent 与权限背景。** Space 中可用的 agent、连接工具和控件取决于访问条件；不同协作者可以有不同 agent、工具及指令。DevDay 回顾另说明 workspace connections 使用被连接账户的权限，但这不直接给出 Space 每个评论请求的授权算法。Baton 必须显式定义自己的主体和委托边界。[S4](https://learn.chatgpt.com/docs/space/agents)、[S6](https://learn.chatgpt.com/docs/whats-new/devday-2026#workspace-connections)。

**O9 · 官方 · 来源内容权限。** 链接的源文件保留自身权限；复制或总结到页面的内容则对页面读者可见。分享页面不自动分享私人聊天或已保存记忆。Baton 不因页面可读而声称外部引用也可读。[S1](https://learn.chatgpt.com/docs/space/#connect-your-sources)、[S3](https://learn.chatgpt.com/docs/space/collaboration#review-together)。

## Baton API 与存储契约

以下规则全部是 **x_扩展**。官方链接只标明所对应的产品背景，不是这些实现细节的来源或 OpenAI API 声明。

**X1 · x_protocol。** HTTP 统一采用 `POST /v1/space/{operation}`，协议标识为 `x_protocol: "baton.space.v1"`。URL 不必逐段含 `x_`，但文档、响应和客户端不得把它称作官方 Space API。操作名及方法参数由同期实现 contract 明列允许列表；禁止按输入反射调用任意存储方法。未实现操作必须明确拒绝，不能伪装成成功。[官方背景：S1](https://learn.chatgpt.com/docs/space/)。

**X2 · 主体与授权。** 认证层确定 `principal`，区分 human、agent、service；请求体不能覆盖真实 actor。内容写入、评论、分享、任务提交、领取及审阅必须在服务端重新检查当前有效权限。`codex-worker` 是 Baton 服务主体，只能使用自动任务所需的窄接口；它不是用户的 connected account，不能借此获得任意页面编辑或分享权。`requested_by` 与 `performed_by` 必须分别保留，服务执行不能冒充用户署名。[官方背景：S3](https://learn.chatgpt.com/docs/space/collaboration#choose-an-access-level)、[S4](https://learn.chatgpt.com/docs/space/agents#work-with-others)。

**X3 · 授权存储。** 直接授权记录资源、接收主体及 view/comment/edit；继承权由 Space/父页面的当前有效授权计算，不能复制成不会失效的直接邀请。Baton 自定义有效权限为可用授权的最高级别，`view < comment < edit`；这是自身的冲突规则，不是官方已公开算法。分享管理权限独立检查。首版不默认为每个 Team 提供 comment；未知接收者类型应拒绝或声明不支持。[官方背景：S3](https://learn.chatgpt.com/docs/space/collaboration#understand-inherited-access)。

**X4 · 页面、块与归属。** PostgreSQL 保存稳定的 Space、Page、Block 标识和父子关系；拒绝循环父子关系。页面内容版本单调递增，归属至少保留实际 actor、提交版本和数据库时间。块 payload、顺序表示、版本比较及历史保留是 Baton 自有格式。内容变更必须携带实现 contract 定义的基线版本；陈旧基线返回冲突，不静默覆盖。首版不声称 CRDT、实时协同光标或自动合并与 Space 一致。[官方背景：S2](https://learn.chatgpt.com/docs/space/pages#rearrange-blocks)、[S2 归属](https://learn.chatgpt.com/docs/space/pages#comment-and-collaborate)。

**X5 · 评论、提及与 inbox。** 评论线程绑定页面或任务；任务与其报告线程使用同一个 UUID。提及保存显式目标 principal，而不是把显示名称当授权身份。对 agent 的执行请求与普通提及应明确区分，保存请求者、接收者和上下文；接收者无有效权限时不得执行。`fetch_inbox` 是 Baton 对“发给当前接收账户/principal 的 mentions + assignments”的查询视图，不是官方 Inbox API，不返回该 Space 内所有评论。结果在读取时过滤当前访问权；不会仅凭知道接收者 ID 就允许读取他人的 inbox。是否触发执行必须遵守 X2、X12，而非由前端 @ 文本直接决定。[官方背景：S4](https://learn.chatgpt.com/docs/space/agents#choose-how-to-ask-for-help)。

**X6 · 幂等与事务。** 普通 mutation 使用稳定 UUID `operation_id`；同一效果的重试复用它。PostgreSQL 在同一事务内按 actor + operation_id 记录规范化请求摘要、业务变更及可重放结果；同键同请求不再产生效果，同键异请求返回 `409`。`submit_task` 另保留现有稳定业务 `idempotency_key`，同 actor + key 不创建第二任务，不能用每次随机生成 operation_id 绕过提交去重。幂等请求摘要包含操作及所有影响效果的字段，不记录秘密原值。重放仍重新检查当前读/操作权限，失权后不得泄漏旧结果。[官方背景仅为产品协作：S3](https://learn.chatgpt.com/docs/space/collaboration#review-together)。

**X7 · HTTP 结果。** 未认证返回 `401`；已认证但无操作权限返回 `403`；不可见资源允许用不泄漏存在性的 `404`。陈旧版本、键复用冲突、作用域冲突或陈旧 fence 返回 `409`，无效参数返回 `422`。错误只含稳定错误码与安全说明；不暴露凭据、内部 SQL、其他主体的不可见任务详情。写事务失败必须整体回滚，不留下半条评论、幂等记录或成功审计事件。[官方背景：S3](https://learn.chatgpt.com/docs/space/collaboration#resolve-an-access-problem)。

## x_claim、x_scope 与 x_handoff

**X8 · x_claim 租约与 fencing。** 一个任务同一时刻只有一个有效持有人。有效性由 PostgreSQL 的当前时间判断，不能信任客户端时钟；事务等待锁之后必须使用新的数据库时间判断期限。`lease_expires_at <= database_now` 即失效。每次新取得或交接所有权都提高该任务单调 fence；续租不降低或重置 fence。持有人、有效租约、当前 fence 三项必须同时匹配，才能续租、提交结果或交接。已过期或旧 fence 的请求拒绝，即使原持有人稍后恢复。续租重放不会再次延长期限。过期释放独占权无需依赖及时运行的清理任务。[官方背景仅为协作：S4](https://learn.chatgpt.com/docs/space/agents#work-with-others)。

**X9 · x_scope 锁域。** 第一版所有 Space 强制使用 `global` repository namespace；`create_space` 不提供可变 namespace，若实现保留 `scope_repository` 字段则只接受 `global`。排斥范围是同一 Baton 实例或共用 PostgreSQL 权威内的跨 Space 领取，不能声称独立数据库之间有分布式互斥。新 namespace 仅能通过未来管理员契约扩展，不能由请求自由指定以绕过冲突。领取事务须串行化该 namespace 的“查重叠 → 占用”步骤，防止并发检查同时为空。[官方背景仅为分工：S4](https://learn.chatgpt.com/docs/space/agents#work-with-others)。

**X10 · x_scope 规范化与重叠。** 文件和模块都转换到同一仓库相对路径域。采用 `/` 分隔符，去除重复分隔符与 `.` 段，拒绝绝对路径、驱动器前缀及 `..` 越界段；空/根范围规范化为仓库根。路径相等，或一方是另一方的完整路径段前缀，即重叠；`src/a` 与 `src/ab` 不因字符串前缀而冲突。模块只能经管理员定义的模块→路径根映射展开；没有管理员映射或模块未知时，保守覆盖整个 global 仓库根。客户端提交的模块映射不得改变此规则，不能把未知模块名当成独立锁域。大小写、符号链接或别名无法可靠规范化时必须保守扩大范围或拒绝，不能声称无冲突。migration scope 使用规范化后的完整 migration 标识精确相等比较，不做路径前缀匹配。文件范围与 migration 范围均需保护时，任务必须同时声明两种范围。对外返回的冲突信息仍受 X2 权限限制。[官方背景仅为分工：S4](https://learn.chatgpt.com/docs/space/agents#work-with-others)。

**X11 · x_handoff。** 交接必须在同一事务内校验原持有人、未过期租约、旧 fence 和接收者资格，然后保留任务的 base commit、上下文、证据引用和线程，切换持有人并提升 fence。base commit 在交接中不可变；context/evidence 仅追加，不覆盖、删除或因空缺字段清空旧材料。作用域不得在交接时无检查地扩大，扩大必须重新检查重叠。原持有人在提交后不能继续使用旧 fence 写入。失败交接不改变所有权或丢失材料，幂等重放不得重复追加上下文或证据。[官方背景仅为协作上下文：S3](https://learn.chatgpt.com/docs/space/collaboration#review-together)。

## x_gate、任务兼容层与 x_audit

**X12 · 状态与自动执行。** Baton canonical task state 为 `queued / claimed / awaiting_gate / done / failed / interrupted`。正常路径为 queued → claimed → awaiting_gate → done；执行失败与执行结果不确定分别为 failed、interrupted。手工候选结果被 gate 拒绝后返回 queued，之后新领取取得新的 epoch。lease 过期释放领取权，不能依赖旧行的 claimed 文本继续阻塞或授予写权。旧 MCP `get_task` 兼容视图将 claimed 映射为 working、done 映射为 completed；兼容映射不能隐藏 failed/interrupted，也不能把 awaiting_gate 提前映射为 completed。`submit_task` 创建的自动任务可以进入 Codex worker 队列；手工创建/领取的任务不自动 dispatch。任务线程 UUID 保持不变。[官方背景仅为任务请求：S2](https://learn.chatgpt.com/docs/space/pages#add-content-with-the-insert-menu)、[S4](https://learn.chatgpt.com/docs/space/agents#choose-how-to-ask-for-help)。

**X13 · x_gate。** 手工 `submit_result` 必须持有 live lease，在同一事务中冻结候选结果的 `result_epoch`（对应当次 claim 的 epoch/fence）、基线与证据，释放 claim/scopes 并进入 awaiting_gate；之后审阅不依赖仍在续租的 claim。gate 请求携带固定 `expected_epoch`，必须匹配当前待审候选。只有配置的 reviewer 可以批准，且 reviewer 不能是该候选的持有人；客户端不能直接把 canonical state 写成 done。批准后才 done；拒绝后回到 queued，旧候选及决定作为历史保留，下次 claim 使用新 epoch，旧批准不能完成新一代任务。默认自动 `submit_task` 明确使用 `worker_success` gate，只有已授权 worker 的成功结果能满足它。这个自动 gate 是 Baton 自有最小验收，不等同人工 review、代码质量保证或生产发布批准。失败/不确定 worker 结果不能过 gate。[官方审阅背景：S4](https://learn.chatgpt.com/docs/space/agents#revise-a-launch-brief)。

**X14 · 提案审阅。** 页面修改提案与任务 gate 是不同对象。Baton 可以保存拟议修改、目标版本、发起者及审阅决定；接受时重新检查权限和目标版本，在同一事务中应用修改并记录决定。拒绝或继续修订不得被记录成接受。Baton 的原子接受和版本冲突策略属于 x_扩展；Space 的精确 undo 行为仍待界面实测，不用自身实现冒充已对齐。[官方背景：S2](https://learn.chatgpt.com/docs/space/pages#draft-content-with-generate)、[S4](https://learn.chatgpt.com/docs/space/agents#revise-a-launch-brief)。

**X15 · 读取视图。** `list_tasks` 默认只列当前 principal 自己的任务，不默认枚举整个共享 Space；`get_task` 在当前有效 view 权限允许时可读取共享任务；`fetch_inbox` 按 X5 的当前接收主体返回 mentions 与 assignments。这三种不同可见性都是 Baton 自有 API 规则，不从官方 Space 的 sidebar 或 sharing 菜单臆造查询协议。[官方背景：S3](https://learn.chatgpt.com/docs/space/collaboration#choose-an-access-level)。

**X16 · x_audit 不可变追加。** 业务事件在产生业务效果的同一 PostgreSQL 事务中追加，至少关联真实 actor、operation/task/thread 标识、数据库时间、结果状态和必要的 fence/版本；幂等重放不能再追加一条代表同一效果的新事件。应用数据库角色不得 UPDATE/DELETE 审计行，迁移所有者与应用权限分离。凭据、完整 prompt/评论正文及证据秘密不进入审计，仅保存必要元数据和引用。该约束不声称能抵抗数据库超级用户或宿主失陷；也不是官方 attribution 的存储形式。[官方背景仅为归属：S2](https://learn.chatgpt.com/docs/space/pages#comment-and-collaborate)。

## 红线与验收

下表中官方语义验收对应 O1–O9 的原文链接；数据库与 HTTP 验收对应 X1–X16 的 x_契约。测试必须通过公开接口或实际 PostgreSQL 事务证明，不以函数返回“成功”、单进程内存锁或 SQLite 替身代替并发与权限结论。

| 编号 | 红线/触发 | 可执行验收结果 |
| --- | --- | --- |
| A1 | Space、父页面及直接授权并存 | 移除直接邀请后继承权仍在；移除继承来源后再次请求重新计算，不能凭旧缓存继续访问。 |
| A2 | view/comment/edit 与分享管理 | view 不能写评论或内容；comment 能反馈不能改内容；edit 不自动获得管理分享权；伪造 actor 无效。 |
| A3 | 并发页面变更 | 相同基线的冲突更新仅有一个生效，另一方返回明确冲突；归属记录真实操作者。 |
| A4 | 提及人与 agent | 人类反馈不会自动派活；agent 请求有请求者和接收者；他人 inbox 不可读，失权后不泄漏旧提及。 |
| A5 | 幂等并发与异载荷重放 | 相同 actor/key 或 operation_id 仅产生一次效果；不同有效载荷冲突；事务失败无半成品。 |
| A6 | 两个 Space 同时领取重叠 scope | 同一 PG/global namespace 内最多一个成功；非重叠路径可并行；未知模块与仓库根冲突；migration 标识仅精确相等冲突。 |
| A7 | 租约过期及进程恢复 | 无清理器也可重新领取；新 fence 增长；旧持有人续租、提交、交接全部被拒绝。 |
| A8 | 交接及重放 | base 不可改；context/evidence 仅追加；thread 保留；新持有人获得更高 fence；重放不再提升 fence 或追加材料；失败不切换持有人。 |
| A9 | 手工 gate 与自动 gate | live lease 提交后冻结 result_epoch、释放 scope 并待审；持有人自批失败；正确 reviewer + expected_epoch 才能批准为 done；拒绝回 queued 后新 claim 提高 epoch；旧批准失败。默认 worker_success 仅接受自动 worker 成功，demo 继续显式标注。 |
| A10 | 手工领取与兼容层 | 手工领取不触发自动 dispatch；claimed→working、done→completed；awaiting_gate 不提前 completed；同一任务/线程 UUID。 |
| A11 | 真实 PostgreSQL 审计约束 | 应用角色尝试修改/删除审计行失败；每个已提交效果有审计；失败事务无成功事件；不含凭据或正文。 |
| A12 | 既有链路回归 | submit → gate 后 completed → 同线程报告 → request/task/worker 审计关联仍可验证；demo 结果不能被当作真实 Codex 通过。 |

验收完成前不得声称已复刻 Space 全部行为、已提供官方 Space API、已有跨独立数据库分布锁，或已完成真实账户/界面兼容测试。共享数据库上线或旧数据迁移另走 shadow → enforce → bake；本规格本身不授权迁移真实数据。[官方能力边界背景：S3](https://learn.chatgpt.com/docs/space/collaboration)、[S4](https://learn.chatgpt.com/docs/space/agents)。

## 待界面实测与明确非目标

**U1 · 待界面实测 · 冲突解决。** 官方说明可共同编辑，未定义并发覆盖、CRDT、合并或冲突 UI。先测试两个编辑者修改同一选区及祖先块，再记录观察日期、版本与边界；当前不得填入猜测。[S2](https://learn.chatgpt.com/docs/space/pages#comment-and-collaborate)。

**U2 · 待界面实测 · 通知时机。** 提及请求的动作已证实；通知渠道、到达延时、去重、离线与恢复时序未建立。Baton durable inbox 的事务规则不代表 Space 通知行为。[S4](https://learn.chatgpt.com/docs/space/agents#choose-how-to-ask-for-help)。

**U3 · 待界面实测 · 选区锚点。** 选区评论/Ask for change 已证实；文本变动、块移动、删除后的定位、重锚定与孤立线程处理未建立。Baton 若保存 quote/block/version，只是自己的上下文格式。[S2](https://learn.chatgpt.com/docs/space/pages#work-with-selected-text)。

**U4 · 待界面实测 · 精确接受、修订与撤销。** 官方有审阅、接受、要求修改和 undo，但未明确撤销跨哪些操作、谁可撤销、多人交错和重启后的恢复语义。页面提案与任务 gate 的实现必须分别标 x_。[S2](https://learn.chatgpt.com/docs/space/pages#draft-content-with-generate)、[S4](https://learn.chatgpt.com/docs/space/agents#revise-a-launch-brief)。

**U5 · 待界面实测 · Team 角色与账户差异。** Team 可能仅有 view/edit 是已证实限制；具体账户、套餐、workspace、接收者组合的可选角色矩阵仍需在获授权环境观察。不能把人员 comment 权限机械推广到所有 Team。[S3](https://learn.chatgpt.com/docs/space/collaboration#choose-an-access-level)。

**U6 · 待界面实测 · 归属粒度。** Show attribution 在可用时存在；粒度、历史保留、粘贴/生成/接受提案后的署名显示未知。Baton 的 actor + revision/audit 只保证自身协议。[S2](https://learn.chatgpt.com/docs/space/pages#comment-and-collaborate)。

**U7 · 待界面实测 · Agent 运行身份与审批。** 原文证明访问与可用工具相关，但不足以确定每一种 Space 提及、dot 和连接器的实际权限交集或具体审批触发条件。Baton 的 service principal 和 gate 是显式自有边界，不承诺与这些未验证行为等价。[S4](https://learn.chatgpt.com/docs/space/agents)、[S6](https://learn.chatgpt.com/docs/whats-new/devday-2026#workspace-connections)。

本阶段不运行以上真实界面实验；不读取真实 Space、账户、私人聊天、记忆、凭据或个人配置；不提供 Space 同步器。后续只在另行授权的环境记录观察，未知项保持未知。
