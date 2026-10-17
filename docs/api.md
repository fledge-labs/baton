<!-- SPDX-License-Identifier: Apache-2.0 -->

# Baton Space HTTP 与 MCP API

这是 Baton 自有的 `baton.space.v1` 协议。产品语义来源和尚未验证的行为见 [行为规格](spec/space.md)；这些 URL、JSON 字段、MCP 工具和数据库状态都不是 OpenAI Space 的公开 wire protocol，也不提供 Space 同步。

## 连接与身份

任意 runtime 可向 `https://example.com/v1/space/{operation}` 发送 JSON `POST`，使用部署者签发的 bearer token。所有请求经过相同的 gateway client ACL 与 bridge 身份验证；没有内部服务账号可以使用这些公共操作。`GET https://example.com/v1/space/schema` 在认证后返回完整参数 JSON Schema、读写标记、别名和协议版本。

身份由管理员注册表确定，包含 principal、connected account 和 human/agent kind；每次请求重读注册表，以便撤销立即生效。请求体不能设置当前 actor。`share.role`、`set_team_member.account`、`create_block.kind` 是对应业务对象的目标字段，不会改变调用者身份。`handoff.target_principal` 由服务端重新解析为目标账户；客户端不能提交目标 Actor 对象。

成功返回 `{"x_protocol":"baton.space.v1","result":...}`。结果可能是对象、列表或空值，以操作类型为准。所有响应使用 `Cache-Control: no-store`。不把 token 放进 URL、请求正文、命令参数或日志。

```python
# SPDX-License-Identifier: Apache-2.0
import json
import uuid
from pathlib import Path
from urllib.request import Request, urlopen

token = Path("/path/to/client.token").read_text().strip()
# 将 operation_id 与该次业务操作一起保存；重试复用，不要重新生成。
operation_id = str(uuid.uuid4())
request = Request(
    "https://example.com/v1/space/create_space",
    data=json.dumps({"operation_id": operation_id, "name": "Project"}).encode(),
    headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    method="POST",
)
with urlopen(request) as response:
    space = json.load(response)["result"]
```

示例使用系统信任的 TLS CA。私有 CA 应显式配置到客户端信任链；不要关闭证书验证。上述地址是占位值，示例不访问真实账户。

## 操作与参数

以下是公开允许列表。`operation_id` 必须是 UUID，同一 principal、同一 operation_id、同一请求只产生一次效果；异载荷重用返回 `409`。读取操作无 operation_id。ID 参数均为 UUID，只有 idempotency_key、principal、account 和 scope 值使用各自的字符串格式。

- `create_space(operation_id: str, name: str)` — 写入。
- `get_space(space_id: str)` — 读取。
- `list_spaces()` — 读取。
- `create_page(operation_id: str, space_id: str, title: str, parent_id: str | None = None)` — 写入。
- `get_page(page_id: str)` — 读取。
- `list_pages(space_id: str, parent_id: str | None = None)` — 读取。
- `create_block(operation_id: str, page_id: str, kind: str, content: Any, position: int | None = None)` — 写入。
- `get_block(block_id: str)` — 读取。
- `list_blocks(page_id: str)` — 读取。
- `update_block(operation_id: str, block_id: str, expected_revision: int, content: Any)` — 写入。
- `revert_block(operation_id: str, block_id: str, expected_revision: int, target_revision: int)` — 写入。
- `share(operation_id: str, resource_type: str, resource_id: str, subject_type: str, subject_id: str, role: str | None)` — 写入。
- `effective_role(resource_type: str, resource_id: str)` — 读取。
- `create_team(operation_id: str, name: str)` — 写入。
- `set_team_member(operation_id: str, team_id: str, account: str, member: bool)` — 写入。
- `comment(operation_id: str, page_id: str, body: str, thread_id: str | None = None, selection: Any = None)` — 写入。
- `list_comments(page_id: str | None = None, thread_id: str | None = None, task_id: str | None = None)` — 读取。给 `page_id`（可加 `thread_id`），或只给 `task_id`：服务端解析该任务所在的页面和线程（线程 ID 等于任务 ID），做与其他读取相同的权限检查。`task_id` 不能与 `page_id`/`thread_id` 同用，两者都不给返回 `422 invalid`；看不到该任务返回 `404 not_found`。
- `mention(operation_id: str, comment_id: str, target_type: str, target_id: str)` — 写入。
- `proposal_create(operation_id: str, block_id: str, expected_revision: int, proposed_content: Any, selection: Any = None)` — 写入。
- `proposal_review(operation_id: str, proposal_id: str, decision: str, content: Any = None)` — 写入。
- `get_proposal(proposal_id: str)` — 读取。
- `list_proposals(block_id: str)` — 读取。
- `list_attribution(block_id: str)` — 读取。
- `create_task_block(operation_id: str, page_id: str, prompt: str, reviewers: list[str], scopes: list[dict] | None = None, base_commit: str | None = None, context: Any = None, evidence: Any = None, assignee_principal: str | None = None, depends_on: list[str] | None = None, work_face: dict | None = None, require_work_face: bool = False, task_kind: str = "construction", approvals_required: int | None = None)` — 写入；`approvals_required` 为 1 或 2，见“审批关卡”。
- `submit_task(operation_id: str, idempotency_key: str, prompt: str, page_id: str | None = None, scopes: list[dict] | None = None, assignee_principal: str | None = None, depends_on: list[str] | None = None, work_face: dict | None = None, require_work_face: bool = False, task_kind: str = "construction", approvals_required: int | None = None)` — 写入；`approvals_required` 为 1 或 2，见“审批关卡”。
- `assign_task(operation_id: str, task_id: str, target_principal: str)` — 写入。
- `plan(operation_id: str, graph: list[dict])` — 写入。
- `set_work_face(operation_id: str, task_id: str, work_face: dict, base_commit: str | None = None)` — 写入。
- `mark_blocked(operation_id: str, task_id: str, reason_code: str, note: str | None = None)` — 写入；见下文“阻塞标记”。
- `ready(page_id: str | None = None, limit: int = 50)` — 读取。
- `get_task(task_id: str)` — 读取。
- `list_tasks(limit: int = 50)` — 读取。
- `fetch_inbox(limit: int = 30)` — 读取。
- `wait_inbox(after_seq: int = 0, timeout_seconds: int = 25, limit: int = 50)` — 读取（长轮询，见下文）。
- `webhook_subscribe(operation_id: str, url: str, events: list[str] | None = None, include_preview: bool = False)` — 写入；仅此一次返回 `signing_secret`。`include_preview` 见下文“webhook 预览”。
- `webhook_list()` — 读取。
- `webhook_delete(operation_id: str, subscription_id: str)` — 写入。
- `webhook_rotate_secret(operation_id: str, subscription_id: str)` — 写入；仅此一次返回新的 `signing_secret`，旧密钥在宽限期内继续参与签名。见“密钥轮换”。
- `webhook_revoke_previous_secret(operation_id: str, subscription_id: str)` — 写入；立即结束宽限期，旧密钥从下一次投递尝试起不再签名。见“密钥轮换”。
- `claim(operation_id: str, task_id: str, lease_seconds: int = 300)` — 写入。
- `renew(operation_id: str, task_id: str, epoch: int, lease_seconds: int = 300)` — 写入。
- `submit_result(operation_id: str, task_id: str, epoch: int, result: str, evidence: Any = None)` — 写入。
- `handoff(operation_id: str, task_id: str, epoch: int, target_principal: str, base_commit: str | None = None, context: Any = None, evidence: Any = None, lease_seconds: int = 300)` — 写入。
- `gate(operation_id: str, task_id: str, expected_epoch: int, decision: str, evidence: Any = None, self_approval: bool = False, edited_result: str | None = None, reason: str | None = None)` — 写入；`decision` 为 `pass`、`fail` 或 `edit`（带修改批准，见“审批 UX”）；`self_approval=true` 只对 Space owner（根）批准自己提交或执行的工作有效，见下文。
- `delegation_create(operation_id: str, delegate_principal: str, scope: str = "approve", space_id: str | None = None, expires_at: str | None = None, may_delegate: bool = False, reason: str)` — 写入；Space owner 创建根委托，持有 `may_delegate` 委托的主体创建子委托，授权人就是调用者。
- `delegation_revoke(operation_id: str, delegation_id: str)` — 写入；仅授权人本人。
- `delegation_list()` — 读取；返回调用者作为授权人或被委托方的委托。
- `get_claim(task_id: str)` — 读取。
- `list_claims()` — 读取。
- `get_scopes(task_id: str)` — 读取。
- `list_audit(task_id: str | None = None, space_id: str | None = None, limit: int = 100)` — 读取。

`x_claim`、`x_scope`、`x_handoff`、`x_gate`、`x_audit` 分别是 `claim`、`get_scopes`、`handoff`、`gate`、`list_audit` 的别名；它们共用同一验证、授权和存储路径。`list_audit` 必须给出 task_id 或 space_id。

`share.resource_type` 为 space/page；subject_type 为 person/team；role 为 view/comment/edit，null 删除直接授权。Team 首版不提供 comment 授权。删除直接授权不会抹除仍然有效的继承授权。只有资源管理者能分享，普通 edit 不等于管理分享权。

`create_block.kind` 为 text/file/prompt/agent_instructions；手工任务必须通过 `create_task_block` 创建，不能用普通块绕过 gate。`content` 是 Baton 的 JSON 内容格式。更新和提案接受都检查 expected_revision。`proposal_review.decision` 为 accept/reject/revise；接受或修订以及 revert_block 需要 human 身份和当前 edit 权限。

`scopes` 是 `[{"kind":"path","value":"src/component"}]` 形式的列表；kind 可为 path/module/migration/none。`{"kind":"none"}`（没有 `value`）表示任务不触碰任何仓库路径、模块或 migration：它不与任何 claim 冲突，不能与其他 scope 条目组合（否则 `422 invalid`）。省略 `scopes` 时，所有任务（`create_task_block`，以及不论是否带 `assignee_principal` 的 `submit_task`）都默认存储 `[{"kind":"none"}]`。显式传 `[]` 保持旧行为，存储仓库根占位范围 `[{"kind":"path","value":""}]`；旧版内置 worker 写下的行也保留这个占位范围。已存在的任务不会被改写；省略 `scopes` 的请求在计算幂等摘要时仍按旧的仓库根占位范围处理，因此已持久化的请求重放不受影响。`create_task_block` 的 `scopes` 现在可省略。全部 Space 共享 global namespace。path 冲突按目录前缀判定：规范化（仓库相对、`/` 分隔、去除重复分隔符、结尾 `/` 与 `.` 段，拒绝绝对路径与 `..`）后两个路径相等，或其中一个是另一个在 `/` 边界上的目录前缀时重叠（`docs/design` 与 `docs/design/roadmap.md`、`docs` 重叠，与 `docs/designer`、`src` 不重叠）；空路径（仓库根占位范围）与任何 path 重叠。声明互不重叠路径的任务（如 `src/a` 与 `docs/b`）可以同时 `claim`；重叠的 `claim` 返回 `409 scope_conflict`。`claim` 和 `ready` 使用同一规则，已有 claim 不受影响。比较默认大小写敏感；运营者设置环境变量 `BATON_SCOPE_CASE_INSENSITIVE=1`（默认关闭）后改为大小写不敏感。module 覆盖整个仓库，与 path、module 和任意 migration 都冲突；migration 使用规范化标识的精确相等检查；`none` 永不重叠。**已知限制：** Baton 不解析符号链接、硬链接或其他别名；通过不同路径指向同一批文件的声明不会被检测为重叠（见 spec A17），请用真实的规范路径声明。`reviewers` 是获准审核该手工任务的 principal ID 列表；服务端还检查这些主体当前权限与独立账户边界。

`comment` 返回绑定 page/thread 的评论；`mention.target_type` 为 person/agent，target_id 始终是显式 principal。服务端从有效 registry 验证 person 对应 human、agent 对应 agent；connected account 不是人物收件地址。提及不会直接触发任意运行时执行；接收者在读取或行动时仍需当前权限。评论、提及和 assignments 存入 PostgreSQL；fetch_inbox 只读取发给当前主体且当前仍可访问的条目。返回的 requested_by、action_requested 以及每线程 requests 列表保留实际提及请求者，评论作者不会覆盖后来发起提及的人。

## 任务、租约与 gate

Baton 不执行任务。`submit_task` 总是创建一条手工任务（`execution_mode=manual`、`gate_mode=reviewer`），保留稳定业务 idempotency_key；即使换 operation_id，同一 principal/key 也不会创建第二任务。reviewer 默认是请求者，gate 规则不变（见下文“审批关卡”），范围省略时为 `none`。MCP 工具签名没有变化。

- **不带 `assignee_principal`：** 创建未指派（`assignee` 为 null）的任务，没有响应时限。任何对该任务所在页面有 edit 权限的主体都会在 `ready` 里看到它，并可以 `claim`；先领取的持有租约，其余人看到的是 `scope_conflict`/不再 ready。没有 edit 权限的主体看不到它（`404 not_found`）。默认页面（省略 `page_id` 时创建的请求者私有 Space）只有请求者有权限，要让别人领取，请传共享页面的 `page_id`，或先共享该页面。
- **带 `assignee_principal`：** 进入指派路径，只有该 principal 能领取；目标必须已具有页面 edit 权限，服务端不会自动分享。submit_task 不能把请求者自己设为接收人。

旧版内置 worker 写下的 `execution_mode='codex'`、`gate_mode='worker_success'` 行是历史值：仍可读取（视图继续省略 prompt），但 `claim`、`gate`、`mark_blocked` 等手工操作对它们返回 `403 forbidden`，也不会出现在 `ready`；`list_tasks` 仍能看到它们。`submit_task` 用同一 idempotency_key 重放早先的无接收人请求，会返回那条历史任务。对排队中、从未被派发的历史任务，`assign_task` 可以把它显式交给一个具名 agent，之后按手工路径处理。`assign_task` 只允许为尚未领取或旧租约已失效的任务指定接收人；有效租约内通过 handoff 交接。已有接收人的任务只能由该 principal 领取，其他具有 edit 的人也不能抢占。fetch_inbox 将当前有效的 assignments 与 mentions 合并。

`create_task_block` 创建手工任务（`reviewers` 必填）。领取和交接返回 task view，其中 epoch 是单调 fence，lease_expires_at 来自数据库时钟。renew、submit_result 和 handoff 必须带当前 epoch；续租重放不延长第二次，过期旧 fence 即使重放旧操作也失败。base_commit 是任务创建后的不可变基线（包括 null）；handoff 只能省略它或重复原值。未提供的 context/evidence 保留，thread_id 不变。

手工 submit_result 只进入 awaiting_gate。gate 必须携带对应候选的 expected_epoch 和 pass/fail/edit（edit 是带修改的批准，计为一次 pass）；只有 Space owner（根），或配置 reviewer、具备当前 edit 权限、持有审批人委托且不是结果作者或执行者的主体，才可通过；需要双人确认时还要两个不同审批人都通过。只有 gate 通过才会 done。

**审批关卡。** 只有持有审批人委托的主体（人或 agent）才能批准，任何人不能批准自己提交或执行的工作。Space 的 owner（创建时记录的精确身份：`owner_principal` + `owner_account` + `owner_kind`，不分 kind）是**根审批人**，天然拥有审批权，不需要委托，也不必在 reviewers 里；同账户的其他 principal、人类账户下的 agent、同名但 kind 不同的主体都不是根。没有 `owner_principal` 的 Space 没有根，直到运维人员用 `set-space-owner` 设置。其他主体做 gate 必须同时满足：（1）是任务配置的 reviewer，并有当前页面 edit 权限，否则 `403 forbidden`；（2）持有覆盖该任务的有效审批人委托（范围 `approve`，`space_id` 为空或等于任务所在 Space，被委托方 principal、account、kind 匹配，整条委托链未过期、未撤销，且链的根授权人正是任务所在 Space 的根身份），否则 `403 delegation_required`；（3）不是结果作者，也不是在被审批 epoch 持有过 claim 的 principal 或 account（含 handoff 的原持有人），否则 `403 forbidden`。只创建了请求、没有提交或执行结果的任务 owner 可以批准。**根可以批准自己提交或执行的工作，但必须带 `self_approval=true`**，否则 `403 forbidden`；非根带该标志仍被拒绝。没有任何服务写入的 gate。`gate.reviewed` 审计和 `x_gate` 记录带 `self_approval`、`root`，非根的批准还带 `delegation_id` 与 `grantor`，审计另含完整委托链 `chain` 以及已计入的 `approvals` 和 `approvals_required`；同一 `operation_id` 的重试会重新检查整条链，委托失效后同样返回 `delegation_required`。

**双人确认。** 任务可以要求两个批准：运维人员为某个范围配置策略（`python -m bridge.delegations policy-set`，见 README），或创建任务时在 `create_task_block`（或 `submit_task`）传 `approvals_required`（1 或 2；非法值返回 `422 invalid`）；任务的值可以提高策略要求，只有创建者是 Space owner 时才能降低。要求 2 个批准时，任务只有在两个不同的审批人（不同 principal 和 account）对同一个 `result_epoch` 都 pass 之后才变成 done，第一个 pass 后仍是 `awaiting_gate`，同一审批人再次 pass 返回 `403 forbidden`。策略的 `root_required` 要求根必须是其中之一，根 pass 之前任务不会 done。任何有权方的 fail（包括根的 fail）都把任务退回 `queued`，旧 epoch 的 pass 不再计数。task view 带有效的 `approvals_required`、已通过的 `approvals_passed` 和 `root_required`。

**审批 UX：带修改批准与审批人身份（DF-14′）。** 这是 Baton 自有 x_扩展。

- **`decision="edit"`（带修改批准）。** 必须带 `edited_result`（非空字符串，至多 32000 字符、无 NUL，且与当前结果不同）；`reason`（可选，至多 1000 字符）说明为何修改。`pass`、`fail` 带 `edited_result` 返回 `422 invalid`（`field` 为 `edited_result`），`decision` 不是 `pass`/`fail`/`edit` 也返回 `422 invalid`。授权与 `pass` 完全相同——只有 Space owner（根）或持有覆盖委托的配置 reviewer 可以 edit，委托失效、自批、同一审批人重复通过的规则同样适用（`delegation_required`、`forbidden`）；根 edit 自己提交或执行的工作同样需要 `self_approval=true`。edit 在 `x_gate` 中记为一次 `pass`（`edited=true`），计入双人确认的批准数，也可以完成任务。
- **两份结果都保留。** `x_gate` 行保存提交时的 `original_result` 和审批人的 `edited_result`（以及 `reason`）；任务的 `result` 变成修改后的文本，`result_author` 仍是原作者。双人确认时第二位审批人看到的就是已修改的文本，第二次 edit 的 `original_result` 是前一次修改后的文本。task view 增加 `original_result`（本 epoch 最初提交的文本，没有修改时为 `null`）和 `result_edits`（`[{"approver","reason","at","original_sha256","edited_sha256"}]`，按时间排序，没有修改时为空数组）。
- **覆盖事件。** 每次 edit 在 `gate.reviewed`（`decision` 记为 `pass`）之外追加一条审计 `gate.result_overridden`，含 `approver`、`reason`（若有）、`original_sha256`、`edited_sha256`（两份结果的 SHA-256，不含正文）、`epoch`、`root`、`self_approval`，以及非根时的 `delegation_id`、`grantor` 和 `chain`。
- **响应里的审批人身份。** `gate` 的返回值是 task view 加一个 `gate` 对象：`{"decision","counted_as","epoch","approver","approver_account","approver_kind","root","self_approval","authority","delegation_id","grantor","edited","approvals","approvals_required","complete"}`。`decision` 是请求的决定（`pass`/`fail`/`edit`），`counted_as` 对 edit 为 `pass`；`authority` 说明权限如何取得，只有三种写法：`root`、`root (self-approval)`、`delegated by <grantor> via <delegation id>`。同一 `operation_id` 的重放返回最初的 `gate` 对象。命令行 `gate` 的 `--summary` 行在 `task <id> state=… epoch=…` 之后追加 `gate=<decision> approver=<principal> authority=<…>`。
- **兼容。** 旧请求（不带 `edited_result`、`reason`）的幂等摘要不变；新增列由幂等迁移添加，`check_schema` 要求它们存在。

Task view 包含 `original_result`/`result_edits`（见“审批 UX”）、`blocked`（`null`，或 `{"reason_code","by","at"}`，见“阻塞标记”）、id/task_id/thread_id、work_face/require_work_face/task_kind/work_face_missing、priority/respond_by/responded_at/escalated_at/overdue、page_id/block_id、owner/owner_account、execution_mode、state/status、epoch、holder/holder_account、lease_expires_at/lease_valid、scopes、base_commit、context、evidence、result/result_epoch、a2a_id/backend（仅旧版 worker 行有值）、gate_mode/reviewers 和错误元数据。canonical state 为 queued/claimed/awaiting_gate/done/failed/interrupted；兼容 status 将 claimed→working、done→completed。手工任务在当前 page view 权限下返回 prompt，方便接收人读取指令；旧版 worker 行的状态视图仍省略 prompt。所有状态视图都不返回 idempotency key 或凭据。

每次结果写入都检查 holder、当前 epoch 和未过期租约，旧持有人不能覆盖新持有人；租约到期后任务回到 `queued`，由别的主体重新 `claim`，没有自动重跑。`failed` 与 `interrupted` 状态以及 `error` 列（如旧版的 `CODEX_TIMEOUT`、`dispatch_uncertain`）只会出现在旧版 worker 留下的行上，当前代码不再写入。

## 审批人委托（delegation）

审批人是靠委托授予的角色，委托只从根向下流动。**根委托**的授权人是 Space 的 owner（不分 kind）；持有该 Space 中有效 `may_delegate=true` 委托的主体可以授予**子委托**。被委托方永远不能超过授权人：Space 相同或更窄，`expires_at` 不晚于父委托（父委托有过期时间时子委托也必须有），祖先被撤销或过期时子委托失效，链最多 3 层。

`delegation_create`：`delegate_principal` 必须是注册表中已连接的主体（人或 agent），不能是调用者自己、service，也不能是调用者链上已有的人（包括上方的根；按 principal 判断），否则会形成环，返回 `422 invalid`；`scope` 目前只有 `approve`；`space_id` 为空表示根拥有的所有 Space；`expires_at` 是带 UTC 偏移、位于未来的 ISO 8601 时刻，省略表示不过期；`may_delegate` 默认 false；`reason` 必填，最多 500 字符（例如 `DECISIONS 291`）。授权人就是调用者：正是所指 Space 的根身份（不指定时为任一 Space 的根身份）的调用者创建根委托；其他调用者须持有覆盖所请求 Space 与过期时间、`may_delegate` 为真且链未满的有效审批人委托，它就是新委托的 `parent_id`。没有任何 `may_delegate` 委托返回 `403 forbidden`，请求超出父委托范围或链已满返回 `422 invalid`。返回的委托含 `id`、`grantor`、`grantor_kind`、`delegate`、`scope`、`space_id`、`may_delegate`、`parent_id`、`reason`、`created_via`（`api`）、`created_by`、`expires_at`、`revoked_at`、`revoked_by`、`chain`（从该委托到根的 ID）和表示整条链是否有效的 `active`。

委托与审批相关参数（`scope`、`reason`、`expires_at`、`may_delegate`、`delegate_principal`、`delegation_id`、`approvals_required`、`self_approval`）校验失败时返回 `422 invalid`，`error.details` 给出字段名与期望类型，不回显输入值；`delegation_required` 是 `403`，与 `forbidden` 一样没有 details。

`delegation_revoke` 只允许授权人本人撤销，撤销父委托会使其下所有子委托失效；已撤销的委托再次撤销返回同一条记录，不重复审计。`delegation_list` 返回调用者作为授权人或被委托方的委托，包含已撤销和已过期的，用 `active` 区分。创建和撤销写入 `x_audit`（`delegation.created`、`delegation.revoked`），元数据只含 `delegation_id`、`scope`、`created_via`、`chain`，不含 reason。

owner 尚无人类身份时，用运维命令在 bridge 容器内记录 owner 的根委托：`python -m bridge.delegations create|revoke|list`（见 README）。它记录 owner 的决定由指定运维人员执行，行里 `created_via='operator'`、`created_by` 为运维人员名称，审计的 `requested_by` 是授权人、`performed_by` 是 `operator:<名称>`；授权人必须正是所指 Space 记录的根身份，并受同样的范围、过期和环规则约束；`set-space-owner` 为没有根的 Space 记录根身份。按 DECISIONS 287、291，owner 是根，南瓜持有 owner 的审批人委托。

## x_graph 依赖图

create_task_block 和 submit_task 接受可选 `depends_on` 前置任务 UUID 列表，依赖和任务在同一事务创建，且依赖字段属于幂等请求摘要。有依赖的任务应在提交时传入该字段，避免别的主体在之后的 plan 请求前先领取任务。

`plan` 原子建立或扩展已有任务之间的依赖；只允许尚未执行、epoch 为 0 的 queued 任务，不能利用过期 claim 或执行后重新排队来改图。每个 graph 条目必须严格为 `{"task_id":"任务UUID","depends_on":["前置任务UUID"]}`；未知字段、错误类型或无效 UUID 返回 invalid，环返回 graph_cycle，整次变更回滚。新依赖、已有依赖及重放都检查当前可见性。plan 不创建任务，也不调用 planner 或模型；成功结果为 `{graph:[{task_id,depends_on}],replay:boolean}`。

`ready` 返回 task view 列表，只列出当前调用者可以手动领取的任务，可按 page_id 过滤；task view 中的 depends_on 仅包含当前可见的前置任务 UUID，has_hidden_dependencies 标识是否还有不可见依赖。隐藏依赖不会出现在 ready 中，也不能用旧已知 ID 绕过领取检查。前置任务必须处于当前 done 状态，且它当前 epoch 的 gate 已通过。claim 在领取时重新检查依赖，因此先前的 ready 结果不是领取许可，也不能绕过撤权、租约、作用域或指定接收人的限制。依赖未满足时返回 dependency_blocked；不要仅检查历史 completed 文本。

例如先创建 A 与 B，再发送 `{"operation_id":"稳定UUID","graph":[{"task_id":"B的UUID","depends_on":["A的UUID"]}]}` 到 `https://example.com/v1/space/plan`。重试使用同一个 operation_id；只有 A 的当前结果经 gate 通过，B 才可能出现在接收人的 ready 列表中。此依赖协议是 Baton 的 x_ 扩展。

## x_ready 施工面清单

x_ready 让任务携带施工面清单，并可要求清单完整前不得领取。**它不是上一节的 `ready` 查询**：`ready` 列出调用者当前可领取的任务，不改变任何任务，保持不变（只是不再返回清单不完整的任务）；x_ready 是任务上的清单数据加领取门控。

`work_face` 的格式严格为 `{"environment":[str],"fixtures":[str],"constraints":[str],"migration":str|null,"acceptance_command":str}`：五个键都必须给出，不允许多余的键；三个列表各最多 20 项，每项去掉首尾空白后非空且不超过 500 字符；`acceptance_command` 去掉首尾空白后非空且不超过 2000 字符；`migration` 为 null 或非空且不超过 200 字符。违反格式返回 `422 invalid`。`create_task_block` 与 `submit_task` 可在创建时传入 `work_face`（默认 null）、`require_work_face`（默认 false）、`task_kind`（`construction` 默认，或 `prep`）；三者计入幂等请求摘要。已有任务按默认值处理，行为不变。

`require_work_face=true` 的任务，只有 `base_commit` 非空、`scopes` 有声明（省略声明时的仓库根占位范围和显式 `{"kind":"none"}` 都不算声明仓库范围，两者都会让 `work_face_missing` 保留 `scopes`）且 `work_face` 非空并合法，清单才完整。task view 的 `work_face_missing` 列出缺失项（只会是 `base_commit`、`scopes`、`work_face`），不要求清单的任务恒为 `[]`。清单不完整时，`claim` 返回 `409 work_face_incomplete`（含重放路径），内部自动 `claim_next` 同样不会领取，`ready` 也不返回它。

准备任务复用 x_graph：让施工任务 `depends_on` 一个 `task_kind="prep"` 的任务即可。准备任务 done 并通过当前 epoch 的 gate 之前，施工任务按既有规则返回 `dependency_blocked`；之后才可能出现在 `ready` 中并被领取。

`set_work_face(operation_id, task_id, work_face, base_commit=None)` 补全或替换清单。目标任务必须是 queued、epoch 为 0 且没有执行记录（与 `plan` 相同），否则返回 `409 stale_claim`。调用者必须对任务所在页面有当前 edit 权限，并且是任务 owner、在 `reviewers` 中（按 principal 精确匹配），或当前持有有效租约，且该租约对应的任务 `task_kind="prep"`、目标任务 `depends_on` 它；否则返回 `403 forbidden`。`base_commit` 只能在当前为 null 时设置一次：重复传原值是 no-op，传别的值返回 `409 operation_conflict`。同一 `operation_id` 加同样输入是重放，换了输入返回 `409 operation_conflict`。成功返回更新后的 task view，并写入审计事件 `task.work_face_set`，元数据只含键名列表和 work_face 规范 JSON 的 sha256，不含正文。

Baton 不会执行 `acceptance_command`。它只是显示给领取人和 gate 审阅人的文本，任务是否通过仍由 gate 审阅人决定。

示例：先用 `create_task_block` 创建准备任务（`"task_kind":"prep"`）和施工任务（`"depends_on":["准备任务UUID"],"require_work_face":true`），准备任务的持有人在领取后向 `https://example.com/v1/space/set_work_face` 发送 `{"operation_id":"稳定UUID","task_id":"施工任务UUID","work_face":{"environment":["python 3.12"],"fixtures":["seed.sql"],"constraints":["不访问网络"],"migration":null,"acceptance_command":"pytest -q"},"base_commit":"基线提交"}`。
## 通知与送达（webhook、wait_inbox）

此前 agent 只能反复调用 `fetch_inbox` 轮询。Baton 现在在同一 PostgreSQL 事务内写入通知 outbox，并提供两种异步送达：长轮询 `wait_inbox`，以及由 bridge 后台循环投递的 webhook。二者都只是“有新事项”的提示，不会启动任何执行；接收者仍须自愿读取、领取并行动，权限在读取与投递时重新检查。HTTP 与 MCP 使用同一 operation dispatcher：`POST https://example.com/v1/space/wait_inbox`，MCP 工具同名。

### 通知 outbox

表 `baton.x_notify(id, recipient, recipient_account, event, object_type, object_id, thread_id, created_at, seq, actor, actor_kind, result_epoch, result_author)`（`actor`/`actor_kind` 只记录触发者的 principal 与种类，不含内容；`result_epoch`/`result_author` 只在 `task.awaiting_gate` 行填写）。`seq` 是 bigserial；所有写事务持同一把 advisory 事务锁串行提交，因此 seq 的提交顺序与大小顺序一致，读者按 `seq > after_seq` 推进不会漏掉较晚提交的较小编号。通知行与产生它的业务效果、`x_audit` 在同一事务写入：操作回滚则没有通知；幂等重放（同 operation_id 或同业务 key）不会再写。

| event | 触发 | 收件人 | object |
| --- | --- | --- | --- |
| `mention.created` | `mention` | 被提及的 principal（经 registry 解析） | comment；thread_id 为评论线程 |
| `task.assigned` | `submit_task`/`create_task_block` 带 assignee，或 `assign_task` | 新接收人 | task |
| `comment.created` | `comment` 写入某任务的线程 | 该任务的 owner、assignee、当前持有人、reviewers（不含评论作者） | comment |
| `task.awaiting_gate` | `submit_result` 使任务进入 awaiting_gate；通知带 `result_epoch` 与 `result_author`（见下） | reviewers | task |
| `gate.decided` | `gate` 决定（pass、fail 或 edit） | owner、assignee、持有人（提交结果的 holder） | task |
| `task.handed_off` | `handoff` | 新持有人 | task |
| `task.failed` | 旧版内置 worker 行的历史事件；当前没有任何路径产生它，保留是为了让旧订阅与旧 outbox 行继续有效 | owner | task |
| `task.overdue` | 后台升级循环发现任务超过 `respond_by` 仍无响应（见“响应时效与超时升级”） | `escalate_to`，与其不同时还有 owner | task |
| `task.blocked` | `mark_blocked`（见“阻塞标记”） | 任务 owner（不含操作者） | task |

操作者本人不会收到自己操作产生的通知，同一 principal 在一次事件中只收到一条。收件人在插入时必须对对象所在页面有 view 权限，否则不写行。outbox 与 webhook 默认负载只含元数据：`id`、`seq`、`event`、`object_type`、`object_id`、`thread_id`、`created_at`、`recipient`；`task.awaiting_gate` 另带 `result_epoch`（被通知的结果所属 epoch）与 `result_author`（提交结果的 principal），见下一段；outbox、审计和日志从不存放 prompt、评论正文或结果文本，接收者凭 object_id 再用 `get_task`、`list_comments` 等带权限检查的读取操作取完整内容。

**`task.awaiting_gate` 的 `result_epoch`（DF-15）。** `submit_result` 写入该通知时，同时把当次结果的 epoch 和作者记在 outbox 行（`x_notify.result_epoch`、`x_notify.result_author`，只是整数和 principal 名，不含结果文本）。`wait_inbox` 返回的该事件行与 webhook 负载（无论是否 `include_preview`）都带这两个字段，审批人可以直接用 `gate(expected_epoch=result_epoch)`，不必先 `get_task`。其他事件没有这两个字段。审批人收到的是通知发出时的 epoch：任务之后被退回并重新提交，会有一条新通知带新 epoch，用旧 epoch gate 返回 `409 stale_claim`。命令行 `gate --task <id>` 省略 `--epoch` 时用任务当前的 `result_epoch`（一次 `get_task`）；`watch --summary` 的通知行显示 `result_epoch=` 与 `result_author=`。

**`wait_inbox` 通知预览。** 为了不必为每条通知多读一次，`wait_inbox` 返回的每一行在上述元数据之外，在**读取时**由当前对象现算三个字段（不进入 outbox）：

- `actor`：`{"principal","kind"}`，触发者；`kind` 为 `human`、`agent`，或后台升级循环产生的 `service`。升级前写入的旧行没有记录触发者时，评论类通知取评论作者，其余省略该字段。
- `subject`：任务事件为 `[<事件>] <任务说明前 60 个字符>`（空白折叠为单个空格），如 `[assigned] 核对发布清单`，事件标签为 `assigned`、`handed_off`、`awaiting_gate`、`gate_decided`、`failed`、`overdue`、`blocked`；评论为 `comment on <任务 id>`（线程不是任务时为 `comment on thread <线程 id>`），提及为 `mention in <任务 id>` 或 `mention in thread <线程 id>`。
- `body_preview`：评论正文或手工任务说明的前 200 个字符，不加省略号。

只有读取时调用者仍对对象所在页面有 view 权限才会返回这些内容：失权的行整条不返回（见 `next_seq` 语义），重新获得权限后按当前内容再次可见。旧版 worker 行的 prompt 与任务视图一致保持不可见：其通知只有 `actor` 和 `[<事件>] <任务 id>` 形式的 `subject`，没有 `body_preview`。`fetch_inbox` 的每条 message 同样增加 `actor` 与 `body_preview`（`subject`、`body_md` 保持原样）。

### wait_inbox 长轮询

`wait_inbox(after_seq=0, timeout_seconds=25, limit=50)` 返回 `{"notifications":[...], "next_seq": n}`，只含发给当前主体、且读取时仍有 view 权限的通知。

- 有 `seq > after_seq` 的可见行时立即返回，最多 `limit`（1–100）条，按 seq 升序；`next_seq` 是最后一条的 seq。
- 没有时最多等待 `timeout_seconds`（0–55，0 表示不等待），到期返回空列表和 `next_seq`。`next_seq` 也会越过因撤权而不可见的行，调用者下次以它作为 `after_seq`；重新获得权限后，用更小的 `after_seq` 仍可重新读到。
- 等待不占用数据库连接或线程：bridge 每 2 秒短轮询 PostgreSQL，并在本进程有写事务提交时立即唤醒，不会忙等。每个 principal 同时最多 4 个等待、整个 bridge 最多 256 个，超出返回 `429 rate_limited`。MCP 与 HTTP 客户端的读取超时必须大于 `timeout_seconds`；默认 nginx 配置允许 370 秒。
- 参数超范围、布尔值或额外字段返回 `422 invalid`。

典型循环：保存最后处理的 `next_seq`，用 `fetch_inbox`/`get_task` 读取详情，成功处理后再用该 `next_seq` 继续调用；断线重连从持久化的 `next_seq` 继续，不会丢事件。

### webhook 订阅与投递

- `webhook_subscribe(operation_id, url, events=None)` 返回 `{"subscription_id","signing_secret","replay"}`。`events` 为空（null）表示全部事件，否则是上表事件名的非空子集。签名密钥由服务端生成（32 个随机字节），按 [Standard Webhooks](https://www.standardwebhooks.com/) 的格式以 `whsec_<base64>` 返回（旧版本返回 64 位十六进制，见下文“迁移”），只在首次成功响应中出现：它不写入 operations 重放结果、审计或日志，`webhook_list` 与同 operation_id 的重放都不再返回。丢失密钥时用 `webhook_rotate_secret` 换一个新密钥，或删除订阅并重建。每个 principal 最多 10 个订阅，超出返回 `429 rate_limited`。
- `webhook_list()` 只列调用者自己的订阅：`subscription_id`、`url`、`events`、`created_at` 和 pending/delivered/dead 计数，没有密钥。`webhook_delete(operation_id, subscription_id)` 只能删除自己的订阅（否则 `404`），并取消其待投递项。
- 订阅只投递调用者自己的、订阅创建之后产生的通知；不回放历史，历史用 `wait_inbox` 读取。

投递是对订阅 URL 的 `POST`，正文是紧凑 JSON（键排序）`{id, seq, event, object_type, object_id, thread_id, created_at, recipient}`（默认只含元数据，见下文“webhook 预览”）。签名遵循 [Standard Webhooks](https://www.standardwebhooks.com/) 规范（[规范仓库](https://github.com/standard-webhooks/standard-webhooks/blob/main/spec/standard-webhooks.md)），请求头：

- `webhook-id`：通知 id（所有重试相同，接收方据此去重）。
- `webhook-timestamp`：本次尝试发送时的 Unix 时间（秒，十进制整数）。重试时每次重新取值并重新签名。
- `webhook-signature`：空格分隔的签名列表，每项形如 `v1,<base64>`，其中 base64 是 `HMAC-SHA256(key, "{webhook-id}.{webhook-timestamp}.{原始正文}")` 的结果。平时只有一项；轮换宽限期内有两项（新密钥在前，旧密钥在后）。接收方只要有一项匹配即通过，应忽略版本不是 `v1` 的项。
- `Content-Type: application/json`、`User-Agent: baton-webhook/1`。

密钥格式是 `whsec_<base64>`：去掉 `whsec_` 前缀后做 base64 解码得到 `key`（32 字节），**不是**把整个字符串当作密钥。消息中的 `{原始正文}` 是未经改动的原始正文字节，不要先解析再重新序列化。官方各语言库（`standardwebhooks`）直接接受 `signing_secret`。

**接收方校验与时间戳容差。** 接收方应：(1) 三个头缺一即拒绝；(2) 检查 `webhook-timestamp` 与本地时间相差不超过 **5 分钟**（正负两个方向），超出即拒绝，以防重放；这要求接收方时钟与 NTP 同步，且 Baton 每次重试都使用新的时间戳，所以合法的延迟重试不会因超出容差被拒；(3) 用常数时间比较签名；(4) 按 `webhook-id` 去重，因为送达是至少一次。

```python
# SPDX-License-Identifier: Apache-2.0
import base64
import hashlib
import hmac
import time

TOLERANCE_SECONDS = 300

def verify(signing_secret: str, headers: dict, raw_body: bytes) -> bool:
    message_id = headers["webhook-id"]
    timestamp = headers["webhook-timestamp"]
    if abs(time.time() - int(timestamp)) > TOLERANCE_SECONDS:
        return False
    key = base64.b64decode(signing_secret.removeprefix("whsec_"))
    signed = f"{message_id}.{timestamp}.".encode() + raw_body
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    for entry in headers["webhook-signature"].split(" "):
        version, _, signature = entry.partition(",")
        if version == "v1" and hmac.compare_digest(signature, expected):
            return True
    return False
```

**密钥轮换。** `webhook_rotate_secret(operation_id, subscription_id)` 为自己的订阅生成新密钥，返回 `{"subscription_id","signing_secret","previous_secret_expires_at","replay"}`；新的 `whsec_` 密钥只在首次成功响应中出现，不写入重放结果、审计或日志。订阅不存在或不属于调用者返回 `404`。此后每次投递同时用新旧两个密钥签名（`webhook-signature` 里两项 `v1`），直到 `previous_secret_expires_at`（默认 24 小时，运营者可用 `BATON_WEBHOOK_ROTATION_GRACE_SECONDS` 设置 0 到 30 天的秒数）；到期后旧密钥不再签名，只剩一项（数据库中的旧密钥由后台约每分钟清理一次，签名时本来就忽略已过期的密钥）。`BATON_WEBHOOK_ROTATION_GRACE_SECONDS` 是 bridge **启动时**读取的默认值（必须是 0 到 2592000 的十进制整数；空值表示 24 小时，其他任何值都会让 bridge 拒绝启动，不会悄悄回退），只影响此后的轮换，不会改变已经开始的宽限期；默认 Compose 不把它传给 bridge，需要由运营者加入 bridge 服务的环境变量。推荐流程：轮换，把新密钥部署到接收端（接收端可同时接受新旧两个密钥），确认新密钥通过后让旧密钥过期，或用 `webhook_revoke_previous_secret` 提前结束宽限期。宽限期内再次轮换会让最旧的密钥立即失效：任何时刻最多两个密钥签名。

**立即作废旧密钥。** `webhook_revoke_previous_secret(operation_id, subscription_id)` 清除订阅正在宽限期内的旧密钥，返回 `{"subscription_id","revoked","replay"}`（没有旧密钥时 `revoked` 为 `false`，调用仍成功）；订阅不存在或不属于调用者返回 `404`。签名密钥在每次投递尝试发出前才读取，所以此后新发出的尝试只用当前密钥签名，已经排队或重试中的投递也一样；唯一残留的窗口是读取密钥到请求发出之间的几毫秒到数秒，作废前已发出的请求不会撤回。怀疑旧密钥泄漏时：先 `webhook_rotate_secret`，再立即 `webhook_revoke_previous_secret`，并把新密钥部署到接收端；这期间接收端还在用旧密钥会验证失败并触发重试，部署新密钥后重试即可通过。审计事件 `webhook.previous_secret_revoked` 只记录订阅标识。
审计事件 `webhook.secret_rotated` 只记录订阅标识和宽限秒数。

**迁移：已弃用的 `X-Baton-*` 头。** 为兼容现有接收端，这些旧头再保留一个版本，同时与 `webhook-*` 头一起发送；它们已弃用，下个版本会被移除，请迁移到上面的 `webhook-*` 头：

- `X-Baton-Event`（已弃用）：事件名。
- `X-Baton-Delivery`（已弃用）：通知 id，与 `webhook-id` 相同。
- `X-Baton-Signature`（已弃用）：`sha256=<十六进制 HMAC-SHA256>`，密钥与 `webhook-signature` 的密钥是同一串字节，消息是未经改动的原始正文字节（不含 id 和时间戳，因此没有重放保护）。轮换期间它只用当前（新）密钥，不会出现两个签名。

旧版本订阅返回的 64 位十六进制密钥就是同一串 32 字节，所以现有订阅不需要重建，旧签名也不变；要换成 Standard Webhooks 校验，把十六进制转换为 `"whsec_" + base64(bytes.fromhex(secret_hex))` 即可，或者直接轮换一次拿到新的 `whsec_` 密钥。

**webhook 预览（`include_preview`，默认 `false`）。** webhook 默认保持只含元数据。订阅时设置 `include_preview=true`，该订阅的每次投递才在正文里多带 `wait_inbox` 同样的 `actor`、`subject`、`body_preview`；它们在投递时现算，并且是在重新检查收件人 view 权限之后，失权的投递仍直接 dead-letter。预览是评论或任务说明的片段，会离开 Baton 发往订阅 URL，只对你信任的端点开启。`webhook_list` 返回每个订阅的 `include_preview`；该选项只在为 `true` 时计入幂等请求摘要，不带它的旧请求重放不变，同一 operation_id 换了取值返回 `409 operation_conflict`。审计与日志仍不含预览内容。

**至少一次语义。** 2xx 视为成功；其他状态码、超时或网络错误都会重试（不跟随 3xx 重定向，3xx 记为失败）。同一通知可能重复到达，也不保证跨通知的顺序；接收方必须按 `webhook-id` 去重，并在处理前验证签名与时间戳。每次尝试 10 秒超时，最多 8 次，第 n 次失败后的等待为 30、60、120、240、480、960、1920 秒（总计约 64 分钟），之后进入 dead-letter，不再重试。投递前重新检查收件人的 view 权限，失权的投递直接 dead-letter（`forbidden`），不发出请求。

每次尝试写入 `x_webhook_attempt`（序号、结果、HTTP 状态、耗时）；每个最终结果写一条审计 `webhook.delivered` 或 `webhook.dead_lettered`，只含订阅/通知标识、事件名、尝试次数、状态码和错误类别，不含 URL（含查询串）、请求头、响应或密钥。

**SSRF 防护。** 只接受 `https` URL，不接受用户信息。订阅时解析主机名，解析结果中只要有环回、链路本地（含云元数据地址）、私有（RFC 1918、ULA）、组播、保留、未指定或其他非全局地址就拒绝（`422 invalid`）；IPv4 映射/NAT64 等嵌入形式按内嵌地址判断。每次投递在连接时重新解析并检查，并固定连接到检查通过的地址，再确认实际对端地址；因此订阅后改变 DNS 记录（rebinding）无法绕过。TLS 校验使用系统信任库，私有 CA 通过 `BATON_WEBHOOK_CA_BUNDLE` 指向 PEM 文件。运营者可用 `BATON_WEBHOOK_ALLOW_HOSTS`（逗号分隔的主机名或 IP 字面量，仅精确匹配）为内部接收端或开发测试豁免地址检查；豁免不放宽 https 要求。

默认 Compose 中 bridge 只接入内部网络，没有出站路由，webhook 投递会失败并最终 dead-letter；需要 webhook 时由运营者为 bridge 明确添加出站网络与相应防火墙策略。

## 阻塞标记

`mark_blocked(operation_id, task_id, reason_code, note=None)` 让不能领取任务的人说明原因，而不是沉默。这是 Baton 的 x_ 扩展，不领取、不改派、不改变任何权限。

- **谁能调用。** 手工任务的 assignee，或 `reviewers` 中的 principal（精确匹配），且对页面至少有 comment 权限。看不到该任务返回 `404 not_found`；能看到但既不是 assignee 也不是 reviewer、或任务不是手工任务，返回 `403 forbidden`。
- **`reason_code`** 为 `scope_conflict`、`lease_held`、`dependency_blocked`、`waiting_input`、`other` 之一，否则 `422 invalid`（`error.details` 列出可选值）。`note` 可省略，最长 2000 个字符。
- **效果（同一事务）。** 任务线程追加一条评论，正文以 `[blocked] ` 开头，格式 `[blocked] <reason_code>` 或 `[blocked] <reason_code>: <note>`，作者是调用者；给任务 owner 写通知 `task.blocked`（owner 就是调用者时不通知）；调用者是 assignee 时，响应时效约定的 `responded_at` 在首次响应时设置；写审计 `task.blocked`，元数据只含 `reason_code`、线程和任务标识，不含 `note`。
- **任务视图。** `blocked` 为 `{"reason_code","by","at"}`（`at` 取数据库时间），直到任务被领取（`claim`）或改派（`assign_task`、升级改派）才恢复为 `null`。再次调用会覆盖标记并追加一条新评论。
- **状态限制。** 只在任务为 queued 或 claimed（含他人持有的有效租约，对应 `lease_held`）时可调用；任务已进入 awaiting_gate、done、failed、interrupted，或调用者自己就是有效持有人，返回 `409 stale_claim`。
- **幂等与重放。** 使用 `operation_id`：同输入重放返回当前 task view（`replay: true`），不再追加评论、通知或审计；异输入 `409 operation_conflict`。
- **接口。** HTTP `POST /v1/space/mark_blocked`；MCP 工具同名；适配器已知工具含 `mark_blocked`，默认白名单不变，需要时用 `--allow-tools` 显式放行；命令行为 `baton_cli.py block --task ... --reason ... [--note ...]`。

```python
# SPDX-License-Identifier: Apache-2.0
import uuid
import httpx

BASE = "https://example.com"  # 你的 Baton 地址
HEADERS = {"Authorization": "Bearer <你的令牌>"}

view = httpx.post(BASE + "/v1/space/mark_blocked", headers=HEADERS, json={
    "operation_id": str(uuid.uuid4()), "task_id": "<任务 UUID>",
    "reason_code": "scope_conflict", "note": "src/ 目录被另一个任务占用",
}).json()["result"]
print(view["blocked"])  # {"reason_code": "scope_conflict", "by": "...", "at": "..."}
```

## 历史 root scope 迁移与调度告警

**迁移命令。** 在 bridge 容器里运行：

```sh
# SPDX-License-Identifier: Apache-2.0
docker compose exec bridge python -m bridge.scopes migrate-manual-root-to-none --dry-run
docker compose exec bridge python -m bridge.scopes migrate-manual-root-to-none
```

它把**手工**任务（`execution_mode=manual`）中 scope 恰好等于旧仓库根占位范围 `[{"kind":"path","value":""}]`、且当前没有有效租约的任务改写为 `[{"kind":"none"}]`，每个任务写一条审计 `task.scopes_migrated`，并向 stdout 输出一份 JSON 汇总 `{"dry_run","matched","migrated","skipped_claimed","task_ids"}`（`skipped_claimed` 是因有效租约而跳过的数量）。`--dry-run` 只报告、不写入。旧版 `codex` 行（保留仓库根占位范围）和持有有效租约的任务不会被改动；命令可重复运行，第二次没有可迁移项。无法区分当初显式传 `scopes: []` 的手工任务，它们同样会被迁移。已存在请求的幂等摘要不受影响。命令失败时只向 stderr 输出错误码，不输出 SQL 或连接细节。

## 响应时效与超时升级

响应时效约定（设计简报“沟通原则 3”）让指派不会无人问津：每个优先级有一个响应时限，到期仍无响应时通知负责人，必要时改派。这是 Baton 的 x_ 扩展。

**字段与默认值。** `submit_task` 与 `create_task_block` 接受可选 `priority`（`urgent`/`normal`/`low`，默认 `normal`）、`escalate_to`（principal，默认任务 owner）和 `fallback_assignee`（principal，可选，不能等于 assignee）；非法值返回 `422 invalid`。三者只在非默认时计入幂等请求摘要，不带它们的旧请求摘要不变；显式写默认值与省略等价。

**时限策略。** 时限只适用于有 assignee 的手工路径任务。时钟在 assignee 被设置时启动：创建时带 assignee，或 `assign_task`/`handoff` 改为新的 assignee（同一 assignee 重复指派不重置），此时 `respond_by = 数据库当前时间 + 该优先级的分钟数`，同时清空 `responded_at` 与 `escalated_at`。没有 assignee 的任务（包括不带接收人的 `submit_task`）没有 `respond_by`，直到被指派。分钟数是 bridge 配置，默认 `urgent=20,normal=60,low=1440`，可用环境变量覆盖：

```text
BATON_RESPONSE_MINUTES="urgent=20,normal=60,low=1440"
```

默认 Compose 会把宿主环境中的该变量传给 bridge（留空即默认值）。可以只写部分优先级，其余保持默认；每项是 1 到 525600 的整数，格式非法时 bridge 启动失败，而不是静默回退。

**什么算响应。** assignee 领取任务（`claim`），或 assignee/当前持有人在该任务线程发表评论（`comment`）。`responded_at` 只在第一次响应时设置。其他人的评论、系统生成的线程消息都不算。

**task view。** 在原有字段之外返回 `priority`、`respond_by`、`responded_at`、`escalated_at`、`escalate_to`、`fallback_assignee` 和 `overdue`。`overdue` 为 true 表示 `respond_by` 已过、尚无响应且任务未结束（done/failed）。

**升级。** bridge 内的后台循环每 30 秒运行一次。每轮先以 PostgreSQL 咨询锁单飞（拿不到锁的副本直接跳过本轮），所以多个 bridge 副本不会重复升级；即使绕过咨询锁，`escalated_at` 守卫也保证同一 assignee 只升级一次。对 `respond_by` 已过、`responded_at` 与 `escalated_at` 为空、状态为 queued（含租约已失效的旧租约）的任务，在同一事务内：

1. 设置 `escalated_at`，写审计 `task.escalated`；
2. 向 `escalate_to` 写通知 `task.overdue`；`escalate_to` 与 owner 不同时 owner 也收到一条。收件人仍需页面 view 权限，否则不写行；
3. 若设置了 `fallback_assignee`，该 principal 当前对页面有 edit 权限且任务仍未被领取，则按 `assign_task` 的规则改派：审计 `task.reassigned`，通知新 assignee `task.assigned`，并为新 assignee 重置 `respond_by`/`escalated_at`。

已被领取的任务从不升级或改派；同一 assignee 不会被升级两次，新 assignee 有新的时限。改派后 fallback 已是 assignee 时，下一次超时只升级、不再改派，避免来回跳。升级和改派只发通知与改派，不替 agent 执行任何事；命名 agent 仍自愿 pull。

**`set_priority(operation_id, task_id, priority)`。** 限任务 owner 与 `reviewers`（其他人 `403 forbidden`，非法优先级 `422 invalid`），同 operation_id 同输入重放，异输入 `409 operation_conflict`。尚未响应且已有 `respond_by` 时，按新优先级重算期限：窗口起点不变，只有长度换成新优先级的分钟数。已响应或已升级的任务不会因此重新打开升级。成功写审计 `task.priority_set` 并返回 task view。

```python
# SPDX-License-Identifier: Apache-2.0
import uuid
import httpx

BASE = "https://example.com"  # 你的 Baton 地址
HEADERS = {"Authorization": "Bearer <你的令牌>"}

task = httpx.post(BASE + "/v1/space/submit_task", headers=HEADERS, json={
    "operation_id": str(uuid.uuid4()), "idempotency_key": "release-check-1", "prompt": "核对发布清单",
    "assignee_principal": "beigua", "priority": "urgent",
    "escalate_to": "nangua", "fallback_assignee": "backup-agent",
}).json()["result"]
print(task["priority"], task["respond_by"], task["overdue"])
```

MCP 与适配器：`submit_task` 工具和 `create_task_block` 带上述三个可选参数，`set_priority` 与其他 x_ 工具一样可用；适配器已知工具含 `set_priority`，默认白名单不变，需要时用 `--allow-tools` 显式放行。通知 `task.overdue` 见上文事件表。

## 适配器角色预设

`adapters/mcp_stdio.py --allow-tools`（以及 `launch_mcp.sh` 透传的同名参数）除工具名外还接受角色预设，可与工具名混用，重复合并：

| 预设 | 展开为 |
| --- | --- |
| `@reader` | `get_task`、`list_tasks`、`fetch_inbox`、`wait_inbox`、`list_comments`、`get_page`、`list_pages`、`list_audit`、`ready` |
| `@assignee` | `@reader` + `claim`、`renew`、`comment`、`submit_result`、`mark_blocked`、`handoff` |
| `@reviewer` | `@reader` + `comment`、`gate` |
| `@owner` | `@reviewer` + `submit_task`、`create_task_block`、`assign_task`、`plan`、`set_work_face`、`set_priority`、`share`、`create_space`、`create_page`、`delegation_create`、`delegation_revoke`、`delegation_list` |

预设只展开为已知工具名；以 `@` 开头的未知预设（包括大小写不同的 `@Reader`）、空名称和通配符都是错误（退出码 2，错误信息不回显输入）。默认白名单（`submit_task,get_task,list_tasks,fetch_inbox`）不变，显式 `--allow-tools` 仍然**替换**它。预设只缩小适配器暴露的工具，不扩大服务端权限：`@reviewer` 能调用 `gate`，不代表能通过 gate（仍需根身份或审批人委托）。

## MCP 兼容与发现

`submit_task(idempotency_key,prompt,page_id=None,scopes=None,assignee_principal=None,depends_on=None,work_face=None,require_work_face=False,task_kind="construction",priority="normal",escalate_to=None,fallback_assignee=None)`、`get_task(task_id)`、`list_tasks()`、`fetch_inbox()` 保持既有工具入口与结果形状。兼容 submit_task 在服务端从 principal + 业务 key 派生稳定 operation_id。其他工具与上列 HTTP 同名同参数，返回包含 x_protocol 和 result 的对象；它们通过同一个 operation dispatcher 执行，MCP 工具发现提供完整 JSON Schema。

旧显示字段 submission_mail_status/mail_status/mail_error 改为 submission_thread_status/thread_status/thread_error；适配器只记录经过校验的 thread_ids 等元数据，不记录正文。fetch_inbox.messages 仍提供 id/thread_id/subject/from/to/created_ts/body_md，来源是评论与提及视图。

## 错误与审计

- `401 unauthorized`：缺少、重复、无效或已撤销的认证。
- `403 forbidden`：对象对调用者可见，但当前权限不允许该操作（例如只有 view 权限却要评论、不是指定接收人却要领取）。
- `403 delegation_required`：gate 的调用者（人或 agent）不是根审批人，也没有覆盖该任务的有效审批人委托；错误体的 `error.details` 与其他错误相同，不回显输入值。
- `404 not_found`：资源不存在，或调用者对它没有 view 权限（包括已被撤权）；两种情况的响应完全相同，不泄漏存在性（规格 X7）。`get_task`、`list_comments`、`get_page`、`comment` 等读写一律如此。对他人目标（assignee、handoff 目标）缺权限的检查仍报告 `403`，因为调用者看得到该对象。
- `409 operation_conflict / scope_conflict / stale_claim / revision_conflict / gate_required / dependency_blocked / graph_cycle / work_face_incomplete`：幂等、作用域、租约、版本、gate、依赖图或施工面清单不满足当前条件。
- `422 invalid`：未知操作、额外字段、无效类型或无效业务参数。响应的 `error.details` 在可定位时给出字段级提示，见下。
- `429 rate_limited`：webhook 订阅数或 wait_inbox 并发等待数超过上限。
- `503 storage_unavailable`：数据库不可用，不能当作成功；重试须沿用同一 operation_id。

错误体为 `{"x_protocol":"baton.space.v1","error":{"code":"...","message":"固定安全说明"}}`。错误不包含 SQL、正文、凭据或不可见资源详情。MCP 错误使用相同稳定错误码。

**字段级提示。** `422 invalid` 可多带 `error.details`：`[{"field":"<名称>","expected":"<简短的类型或格式说明>"}]`，至多 20 项。覆盖三类问题：未知字段（`expected` 为 `not a field of this operation; see GET /v1/space/schema`）、缺失必填字段（如 `required string`）和类型错误（如 `integer`、`array of string`、`UUID string`、`one of urgent, normal, low`）；来自 pydantic 请求模型，以及 `bridge/space_store.py` 中能确定字段的校验器（长度、枚举、scope、work_face、UUID 等）。嵌套字段用点号和下标，如 `graph[0].task_id`、`work_face.environment`。`details` 只含字段名和固定说明文本，**绝不回显输入值**；不是标识符形状的未知键名显示为 `<unknown>`。无法归因到单个字段的校验失败（如依赖环之外的业务规则）仍只有 `code` 与 `message`。MCP 错误文本在固定说明之后追加同样的内容：`invalid: Invalid request; details=[{"field":"name","expected":"string"}]`，其余错误码的文本不变。

list_audit 在当前资源权限内读取追加审计，内容只有 actor、operation、资源、时间、fence/版本等必要元数据。审计和业务效果在同一 PG 事务中提交；HTTP、MCP 和容器日志不记录 prompt、评论正文或 bearer token。
