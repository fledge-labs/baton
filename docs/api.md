<!-- SPDX-License-Identifier: Apache-2.0 -->

# Baton Space HTTP 与 MCP API

这是 Baton 自有的 `baton.space.v1` 协议。产品语义来源和尚未验证的行为见 [行为规格](spec/space.md)；这些 URL、JSON 字段、MCP 工具和数据库状态都不是 OpenAI Space 的公开 wire protocol，也不提供 Space 同步。

## 连接与身份

任意 runtime 可向 `https://example.com/v1/space/{operation}` 发送 JSON `POST`，使用部署者签发的 bearer token。所有请求经过相同的 gateway client ACL 与 bridge 身份验证；内部 dispatcher token 不能使用这些公共操作。`GET https://example.com/v1/space/schema` 在认证后返回完整参数 JSON Schema、读写标记、别名和协议版本。

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
- `list_comments(page_id: str, thread_id: str | None = None)` — 读取。
- `mention(operation_id: str, comment_id: str, target_type: str, target_id: str)` — 写入。
- `proposal_create(operation_id: str, block_id: str, expected_revision: int, proposed_content: Any, selection: Any = None)` — 写入。
- `proposal_review(operation_id: str, proposal_id: str, decision: str, content: Any = None)` — 写入。
- `get_proposal(proposal_id: str)` — 读取。
- `list_proposals(block_id: str)` — 读取。
- `list_attribution(block_id: str)` — 读取。
- `create_task_block(operation_id: str, page_id: str, prompt: str, scopes: list[dict], reviewers: list[str], base_commit: str | None = None, context: Any = None, evidence: Any = None, assignee_principal: str | None = None, depends_on: list[str] | None = None, work_face: dict | None = None, require_work_face: bool = False, task_kind: str = "construction")` — 写入。
- `submit_task(operation_id: str, idempotency_key: str, prompt: str, page_id: str | None = None, scopes: list[dict] | None = None, assignee_principal: str | None = None, depends_on: list[str] | None = None, work_face: dict | None = None, require_work_face: bool = False, task_kind: str = "construction")` — 写入。
- `assign_task(operation_id: str, task_id: str, target_principal: str)` — 写入。
- `plan(operation_id: str, graph: list[dict])` — 写入。
- `set_work_face(operation_id: str, task_id: str, work_face: dict, base_commit: str | None = None)` — 写入。
- `ready(page_id: str | None = None, limit: int = 50)` — 读取。
- `get_task(task_id: str)` — 读取。
- `list_tasks(limit: int = 50)` — 读取。
- `fetch_inbox(limit: int = 30)` — 读取。
- `wait_inbox(after_seq: int = 0, timeout_seconds: int = 25, limit: int = 50)` — 读取（长轮询，见下文）。
- `webhook_subscribe(operation_id: str, url: str, events: list[str] | None = None)` — 写入；仅此一次返回 `signing_secret`。
- `webhook_list()` — 读取。
- `webhook_delete(operation_id: str, subscription_id: str)` — 写入。
- `claim(operation_id: str, task_id: str, lease_seconds: int = 300)` — 写入。
- `renew(operation_id: str, task_id: str, epoch: int, lease_seconds: int = 300)` — 写入。
- `submit_result(operation_id: str, task_id: str, epoch: int, result: str, evidence: Any = None)` — 写入。
- `handoff(operation_id: str, task_id: str, epoch: int, target_principal: str, base_commit: str | None = None, context: Any = None, evidence: Any = None, lease_seconds: int = 300)` — 写入。
- `gate(operation_id: str, task_id: str, expected_epoch: int, decision: str, evidence: Any = None)` — 写入。
- `get_claim(task_id: str)` — 读取。
- `list_claims()` — 读取。
- `get_scopes(task_id: str)` — 读取。
- `list_audit(task_id: str | None = None, space_id: str | None = None, limit: int = 100)` — 读取。

`x_claim`、`x_scope`、`x_handoff`、`x_gate`、`x_audit` 分别是 `claim`、`get_scopes`、`handoff`、`gate`、`list_audit` 的别名；它们共用同一验证、授权和存储路径。`list_audit` 必须给出 task_id 或 space_id。

`share.resource_type` 为 space/page；subject_type 为 person/team；role 为 view/comment/edit，null 删除直接授权。Team 首版不提供 comment 授权。删除直接授权不会抹除仍然有效的继承授权。只有资源管理者能分享，普通 edit 不等于管理分享权。

`create_block.kind` 为 text/file/prompt/agent_instructions；手工任务必须通过 `create_task_block` 创建，不能用普通块绕过 gate。`content` 是 Baton 的 JSON 内容格式。更新和提案接受都检查 expected_revision。`proposal_review.decision` 为 accept/reject/revise；接受或修订以及 revert_block 需要 human 身份和当前 edit 权限。

`scopes` 是 `[{"kind":"path","value":"src/component"}]` 形式的列表；kind 可为 path/module/migration。全部 Space 共享 global namespace。首版未建立可信仓库路径映射，因此 path/module 均保守占用仓库根；migration 使用规范化标识的精确相等检查。不能凭不同路径文本宣称可并行领取。`reviewers` 是获准审核该手工任务的 principal ID 列表；服务端还检查这些主体当前权限与独立账户边界。

`comment` 返回绑定 page/thread 的评论；`mention.target_type` 为 person/agent，target_id 始终是显式 principal。服务端从有效 registry 验证 person 对应 human、agent 对应 agent；connected account 不是人物收件地址。提及不会直接触发任意运行时执行；接收者在读取或行动时仍需当前权限。评论、提及和 assignments 存入 PostgreSQL；fetch_inbox 只读取发给当前主体且当前仍可访问的条目。返回的 requested_by、action_requested 以及每线程 requests 列表保留实际提及请求者，评论作者不会覆盖后来发起提及的人。

## 任务、租约与 gate

`submit_task` 未指定接收人时创建自动任务，保留稳定业务 idempotency_key；即使换 operation_id，同一 principal/key 也不会创建第二任务。自动任务只能由窄服务接口派发给 worker，采用明确标注的 worker_success gate。Demo backend 始终返回 backend=demo，不代表真实 Codex 验收。

指定 `assignee_principal` 的 submit_task 进入手工路径，默认 reviewer 为请求者；目标必须已具有页面 edit 权限，服务端不会自动分享。submit_task 不能把请求者自己设为接收人。`assign_task` 只允许为尚未领取或旧租约已失效的任务指定接收人；有效租约内通过 handoff 交接。已有接收人的任务只能由该 principal 领取，其他具有 edit 的人也不能抢占。fetch_inbox 将当前有效的 assignments 与 mentions 合并。

`create_task_block` 创建手工任务，绝不自动派发。领取和交接返回 task view，其中 epoch 是单调 fence，lease_expires_at 来自数据库时钟。renew、submit_result 和 handoff 必须带当前 epoch；续租重放不延长第二次，过期旧 fence 即使重放旧操作也失败。base_commit 是任务创建后的不可变基线（包括 null）；handoff 只能省略它或重复原值。未提供的 context/evidence 保留，thread_id 不变。

手工 submit_result 只进入 awaiting_gate。gate 必须携带对应候选的 expected_epoch 和 pass/fail；只有配置 reviewer、具备当前 edit 权限且不是结果作者或持有人账户，才可通过。只有 gate 通过才会 done。

Task view 包含 id/task_id/thread_id、work_face/require_work_face/task_kind/work_face_missing、priority/respond_by/responded_at/escalated_at/overdue、page_id/block_id、owner/owner_account、execution_mode、state/status、epoch、holder/holder_account、lease_expires_at/lease_valid、scopes、base_commit、context、evidence、result/result_epoch、a2a_id/backend、gate_mode/reviewers 和错误元数据。canonical state 为 queued/claimed/awaiting_gate/done/failed/interrupted；兼容 status 将 claimed→working、done→completed。手工任务在当前 page view 权限下返回 prompt，方便接收人读取指令；自动任务的旧兼容状态视图仍省略 prompt。所有状态视图都不返回 idempotency key 或凭据。

自动派发前先持久化 dispatch_started，并且每次结果写入检查 holder、当前 epoch 和未过期租约。网络结果不确定时进入 interrupted；自动恢复不会再次执行可能已派发的任务，旧 worker 不能覆盖新持有人。worker 明确报告失败时，任务进入 failed，`error` 只保存封闭词汇中的错误码（如 `CODEX_TIMEOUT`、`CODEX_AUTH_UNAUTHORIZED`；无法接受的诊断为 `A2A_FAILED_UNCLASSIFIED`），并在任务线程追加固定文本的 `[failed]` 评论；worker 的原始错误文本不会进入任务、线程或审计。

在写入 dispatch_started 前，bridge 重新读取 registry，要求提交者仍是 active client，且 principal、connected account 和 kind 与任务保存的身份一致。撤销或改变映射后，排队任务以 cancelled 停止，不向 A2A 发出请求；这一检查不承诺取消此前已经发出的执行。页面及依赖权限仍由 store 以任务保存的原提交者身份重新校验，内部 service 身份不扩大执行授权。

## x_graph 依赖图

create_task_block 和 submit_task 接受可选 `depends_on` 前置任务 UUID 列表，依赖和任务在同一事务创建，且依赖字段属于幂等请求摘要。有依赖的自动任务应在提交时传入该字段，避免 worker 在之后的 plan 请求前先领取任务。

`plan` 原子建立或扩展已有任务之间的依赖；只允许尚未执行、epoch 为 0 的 queued 任务，不能利用过期 claim 或执行后重新排队来改图。每个 graph 条目必须严格为 `{"task_id":"任务UUID","depends_on":["前置任务UUID"]}`；未知字段、错误类型或无效 UUID 返回 invalid，环返回 graph_cycle，整次变更回滚。新依赖、已有依赖及重放都检查当前可见性。plan 不创建任务，也不调用 planner 或模型；成功结果为 `{graph:[{task_id,depends_on}],replay:boolean}`。

`ready` 返回 task view 列表，只列出当前调用者可以手动领取的任务，可按 page_id 过滤；task view 中的 depends_on 仅包含当前可见的前置任务 UUID，has_hidden_dependencies 标识是否还有不可见依赖。隐藏依赖不会出现在 ready 中，也不能用旧已知 ID 绕过领取检查。前置任务必须处于当前 done 状态，且它当前 epoch 的 gate 已通过。claim 与内部自动 claim_next 在领取时重新检查依赖，因此先前的 ready 结果不是领取许可，也不能绕过撤权、租约、作用域或指定接收人的限制。依赖未满足时返回 dependency_blocked；不要仅检查历史 completed 文本。

例如先创建 A 与 B，再发送 `{"operation_id":"稳定UUID","graph":[{"task_id":"B的UUID","depends_on":["A的UUID"]}]}` 到 `https://example.com/v1/space/plan`。重试使用同一个 operation_id；只有 A 的当前结果经 gate 通过，B 才可能出现在接收人的 ready 列表中。此依赖协议是 Baton 的 x_ 扩展。

## x_ready 施工面清单

x_ready 让任务携带施工面清单，并可要求清单完整前不得领取。**它不是上一节的 `ready` 查询**：`ready` 列出调用者当前可领取的任务，不改变任何任务，保持不变（只是不再返回清单不完整的任务）；x_ready 是任务上的清单数据加领取门控。

`work_face` 的格式严格为 `{"environment":[str],"fixtures":[str],"constraints":[str],"migration":str|null,"acceptance_command":str}`：五个键都必须给出，不允许多余的键；三个列表各最多 20 项，每项去掉首尾空白后非空且不超过 500 字符；`acceptance_command` 去掉首尾空白后非空且不超过 2000 字符；`migration` 为 null 或非空且不超过 200 字符。违反格式返回 `422 invalid`。`create_task_block` 与 `submit_task` 可在创建时传入 `work_face`（默认 null）、`require_work_face`（默认 false）、`task_kind`（`construction` 默认，或 `prep`）；三者计入幂等请求摘要。已有任务按默认值处理，行为不变。

`require_work_face=true` 的任务，只有 `base_commit` 非空、`scopes` 有声明（省略声明时的仓库根占位范围不算声明）且 `work_face` 非空并合法，清单才完整。task view 的 `work_face_missing` 列出缺失项（只会是 `base_commit`、`scopes`、`work_face`），不要求清单的任务恒为 `[]`。清单不完整时，`claim` 返回 `409 work_face_incomplete`（含重放路径），内部自动 `claim_next` 同样不会领取，`ready` 也不返回它。

准备任务复用 x_graph：让施工任务 `depends_on` 一个 `task_kind="prep"` 的任务即可。准备任务 done 并通过当前 epoch 的 gate 之前，施工任务按既有规则返回 `dependency_blocked`；之后才可能出现在 `ready` 中并被领取。

`set_work_face(operation_id, task_id, work_face, base_commit=None)` 补全或替换清单。目标任务必须是 queued、epoch 为 0 且没有执行记录（与 `plan` 相同），否则返回 `409 stale_claim`。调用者必须对任务所在页面有当前 edit 权限，并且是任务 owner、在 `reviewers` 中（按 principal 精确匹配），或当前持有有效租约，且该租约对应的任务 `task_kind="prep"`、目标任务 `depends_on` 它；否则返回 `403 forbidden`。`base_commit` 只能在当前为 null 时设置一次：重复传原值是 no-op，传别的值返回 `409 operation_conflict`。同一 `operation_id` 加同样输入是重放，换了输入返回 `409 operation_conflict`。成功返回更新后的 task view，并写入审计事件 `task.work_face_set`，元数据只含键名列表和 work_face 规范 JSON 的 sha256，不含正文。

Baton 不会执行 `acceptance_command`。它只是显示给领取人和 gate 审阅人的文本，任务是否通过仍由 gate 审阅人决定。

示例：先用 `create_task_block` 创建准备任务（`"task_kind":"prep"`）和施工任务（`"depends_on":["准备任务UUID"],"require_work_face":true`），准备任务的持有人在领取后向 `https://example.com/v1/space/set_work_face` 发送 `{"operation_id":"稳定UUID","task_id":"施工任务UUID","work_face":{"environment":["python 3.12"],"fixtures":["seed.sql"],"constraints":["不访问网络"],"migration":null,"acceptance_command":"pytest -q"},"base_commit":"基线提交"}`。
## 通知与送达（webhook、wait_inbox）

此前 agent 只能反复调用 `fetch_inbox` 轮询。Baton 现在在同一 PostgreSQL 事务内写入通知 outbox，并提供两种异步送达：长轮询 `wait_inbox`，以及由 bridge 内部 worker 投递的 webhook。二者都只是“有新事项”的提示，不会启动任何执行；接收者仍须自愿读取、领取并行动，权限在读取与投递时重新检查。HTTP 与 MCP 使用同一 operation dispatcher：`POST https://example.com/v1/space/wait_inbox`，MCP 工具同名。

### 通知 outbox

表 `baton.x_notify(id, recipient, recipient_account, event, object_type, object_id, thread_id, created_at, seq)`。`seq` 是 bigserial；所有写事务持同一把 advisory 事务锁串行提交，因此 seq 的提交顺序与大小顺序一致，读者按 `seq > after_seq` 推进不会漏掉较晚提交的较小编号。通知行与产生它的业务效果、`x_audit` 在同一事务写入：操作回滚则没有通知；幂等重放（同 operation_id 或同业务 key）不会再写。

| event | 触发 | 收件人 | object |
| --- | --- | --- | --- |
| `mention.created` | `mention` | 被提及的 principal（经 registry 解析） | comment；thread_id 为评论线程 |
| `task.assigned` | `submit_task`/`create_task_block` 带 assignee，或 `assign_task` | 新接收人 | task |
| `comment.created` | `comment` 写入某任务的线程 | 该任务的 owner、assignee、当前持有人、reviewers（不含评论作者） | comment |
| `task.awaiting_gate` | `submit_result` 使任务进入 awaiting_gate | reviewers | task |
| `gate.decided` | `gate` 决定（pass 或 fail），以及自动任务的 worker_success 通过 | owner、assignee、持有人（提交结果的 holder） | task |
| `task.handed_off` | `handoff` | 新持有人 | task |
| `task.failed` | 自动任务的 worker 确定性失败（`fail_auto`，同时写入 `[failed]` 线程评论） | owner | task |
| `task.overdue` | 后台升级循环发现任务超过 `respond_by` 仍无响应（见“响应时效与超时升级”） | `escalate_to`，与其不同时还有 owner | task |

操作者本人不会收到自己操作产生的通知，同一 principal 在一次事件中只收到一条。收件人在插入时必须对对象所在页面有 view 权限，否则不写行。负载只含元数据：`id`、`seq`、`event`、`object_type`、`object_id`、`thread_id`、`created_at`、`recipient`；任何通知、webhook 或审计都不含 prompt、评论正文或结果文本，接收者凭 object_id 再用 `get_task`、`list_comments` 等带权限检查的读取操作取内容。

### wait_inbox 长轮询

`wait_inbox(after_seq=0, timeout_seconds=25, limit=50)` 返回 `{"notifications":[...], "next_seq": n}`，只含发给当前主体、且读取时仍有 view 权限的通知。

- 有 `seq > after_seq` 的可见行时立即返回，最多 `limit`（1–100）条，按 seq 升序；`next_seq` 是最后一条的 seq。
- 没有时最多等待 `timeout_seconds`（0–55，0 表示不等待），到期返回空列表和 `next_seq`。`next_seq` 也会越过因撤权而不可见的行，调用者下次以它作为 `after_seq`；重新获得权限后，用更小的 `after_seq` 仍可重新读到。
- 等待不占用数据库连接或线程：bridge 每 2 秒短轮询 PostgreSQL，并在本进程有写事务提交时立即唤醒，不会忙等。每个 principal 同时最多 4 个等待、整个 bridge 最多 256 个，超出返回 `429 rate_limited`。MCP 与 HTTP 客户端的读取超时必须大于 `timeout_seconds`；默认 nginx 配置允许 370 秒。
- 参数超范围、布尔值或额外字段返回 `422 invalid`。

典型循环：保存最后处理的 `next_seq`，用 `fetch_inbox`/`get_task` 读取详情，成功处理后再用该 `next_seq` 继续调用；断线重连从持久化的 `next_seq` 继续，不会丢事件。

### webhook 订阅与投递

- `webhook_subscribe(operation_id, url, events=None)` 返回 `{"subscription_id","signing_secret","replay"}`。`events` 为空（null）表示全部事件，否则是上表事件名的非空子集。签名密钥由服务端生成（32 个随机字节，以 64 位十六进制返回），只在首次成功响应中出现：它不写入 operations 重放结果、审计或日志，`webhook_list` 与同 operation_id 的重放都不再返回。丢失密钥时删除订阅并重建。每个 principal 最多 10 个订阅，超出返回 `429 rate_limited`。
- `webhook_list()` 只列调用者自己的订阅：`subscription_id`、`url`、`events`、`created_at` 和 pending/delivered/dead 计数，没有密钥。`webhook_delete(operation_id, subscription_id)` 只能删除自己的订阅（否则 `404`），并取消其待投递项。
- 订阅只投递调用者自己的、订阅创建之后产生的通知；不回放历史，历史用 `wait_inbox` 读取。

投递是对订阅 URL 的 `POST`，正文是紧凑 JSON（键排序）`{id, seq, event, object_type, object_id, thread_id, created_at, recipient}`，请求头：

- `X-Baton-Event`：事件名。
- `X-Baton-Delivery`：通知 id（所有重试相同）。
- `X-Baton-Signature`：`sha256=<十六进制 HMAC-SHA256>`，密钥是 `signing_secret` 十六进制解码得到的 32 字节，消息是未经改动的原始正文字节。

```python
# SPDX-License-Identifier: Apache-2.0
import hashlib
import hmac

def verify(secret_hex: str, raw_body: bytes, header: str) -> bool:
    expected = "sha256=" + hmac.new(bytes.fromhex(secret_hex), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)
```

**至少一次语义。** 2xx 视为成功；其他状态码、超时或网络错误都会重试（不跟随 3xx 重定向，3xx 记为失败）。同一通知可能重复到达，也不保证跨通知的顺序；接收方必须按 `X-Baton-Delivery` 去重，并在处理前验证签名。每次尝试 10 秒超时，最多 8 次，第 n 次失败后的等待为 30、60、120、240、480、960、1920 秒（总计约 64 分钟），之后进入 dead-letter，不再重试。投递前重新检查收件人的 view 权限，失权的投递直接 dead-letter（`forbidden`），不发出请求。

每次尝试写入 `x_webhook_attempt`（序号、结果、HTTP 状态、耗时）；每个最终结果写一条审计 `webhook.delivered` 或 `webhook.dead_lettered`，只含订阅/通知标识、事件名、尝试次数、状态码和错误类别，不含 URL（含查询串）、请求头、响应或密钥。

**SSRF 防护。** 只接受 `https` URL，不接受用户信息。订阅时解析主机名，解析结果中只要有环回、链路本地（含云元数据地址）、私有（RFC 1918、ULA）、组播、保留、未指定或其他非全局地址就拒绝（`422 invalid`）；IPv4 映射/NAT64 等嵌入形式按内嵌地址判断。每次投递在连接时重新解析并检查，并固定连接到检查通过的地址，再确认实际对端地址；因此订阅后改变 DNS 记录（rebinding）无法绕过。TLS 校验使用系统信任库，私有 CA 通过 `BATON_WEBHOOK_CA_BUNDLE` 指向 PEM 文件。运营者可用 `BATON_WEBHOOK_ALLOW_HOSTS`（逗号分隔的主机名或 IP 字面量，仅精确匹配）为内部接收端或开发测试豁免地址检查；豁免不放宽 https 要求。

默认 Compose 中 bridge 只接入内部网络，没有出站路由，webhook 投递会失败并最终 dead-letter；需要 webhook 时由运营者为 bridge 明确添加出站网络与相应防火墙策略。

## 响应时效与超时升级

响应时效约定（设计简报“沟通原则 3”）让指派不会无人问津：每个优先级有一个响应时限，到期仍无响应时通知负责人，必要时改派。这是 Baton 的 x_ 扩展。

**字段与默认值。** `submit_task` 与 `create_task_block` 接受可选 `priority`（`urgent`/`normal`/`low`，默认 `normal`）、`escalate_to`（principal，默认任务 owner）和 `fallback_assignee`（principal，可选，不能等于 assignee）；非法值返回 `422 invalid`。三者只在非默认时计入幂等请求摘要，不带它们的旧请求摘要不变；显式写默认值与省略等价。

**时限策略。** 时限只适用于有 assignee 的手工路径任务。时钟在 assignee 被设置时启动：创建时带 assignee，或 `assign_task`/`handoff` 改为新的 assignee（同一 assignee 重复指派不重置），此时 `respond_by = 数据库当前时间 + 该优先级的分钟数`，同时清空 `responded_at` 与 `escalated_at`。没有 assignee 的任务（包括自动任务）没有 `respond_by`，直到被指派。分钟数是 bridge 配置，默认 `urgent=20,normal=60,low=1440`，可用环境变量覆盖：

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

## MCP 兼容与发现

`submit_task(idempotency_key,prompt,page_id=None,scopes=None,assignee_principal=None,depends_on=None,work_face=None,require_work_face=False,task_kind="construction",priority="normal",escalate_to=None,fallback_assignee=None)`、`get_task(task_id)`、`list_tasks()`、`fetch_inbox()` 保持既有工具入口与结果形状。兼容 submit_task 在服务端从 principal + 业务 key 派生稳定 operation_id。其他工具与上列 HTTP 同名同参数，返回包含 x_protocol 和 result 的对象；它们通过同一个 operation dispatcher 执行，MCP 工具发现提供完整 JSON Schema。

旧显示字段 submission_mail_status/mail_status/mail_error 改为 submission_thread_status/thread_status/thread_error；适配器只记录经过校验的 thread_ids 等元数据，不记录正文。fetch_inbox.messages 仍提供 id/thread_id/subject/from/to/created_ts/body_md，来源是评论与提及视图。

## 错误与审计

- `401 unauthorized`：缺少、重复、无效或已撤销的认证。
- `403 forbidden`：当前身份或权限不允许操作。
- `404 not_found`：资源不存在或对调用者不可见。
- `409 operation_conflict / scope_conflict / stale_claim / revision_conflict / gate_required / dependency_blocked / graph_cycle / work_face_incomplete`：幂等、作用域、租约、版本、gate、依赖图或施工面清单不满足当前条件。
- `422 invalid`：未知操作、额外字段、无效类型或无效业务参数。
- `429 rate_limited`：webhook 订阅数或 wait_inbox 并发等待数超过上限。
- `503 storage_unavailable`：数据库不可用，不能当作成功或自动重复外部执行。

错误体为 `{"x_protocol":"baton.space.v1","error":{"code":"...","message":"固定安全说明"}}`。错误不包含 SQL、正文、凭据或不可见资源详情。MCP 错误使用相同稳定错误码。

list_audit 在当前资源权限内读取追加审计，内容只有 actor、operation、资源、时间、fence/版本等必要元数据。审计和业务效果在同一 PG 事务中提交；HTTP、MCP 和容器日志不记录 prompt、评论正文或 bearer token。
