<!-- SPDX-License-Identifier: Apache-2.0 -->

# 指定接收人：主动拉取任务

这是 Baton 自有协议，不是 ChatGPT Space API。调用方式见 [API](../docs/api.md)，权限与租约规则见 [规格](../docs/spec/space.md)。下面以发起人 `nangua`、接收人 `beigua` 为例；身份名可以替换，所有动作都按调用方令牌认证。

## 操作者准备

使用独立测试 Baton 实例。管理员通过 `tokens.py issue` 签发普通客户端身份；已有预签发文件则用 `import-client` 安全导入，不能把客户端设成 dispatcher。每个身份有自己的令牌。

令牌交给该身份的负责人，经已有 SSH/SCP 渠道放到接收方主机的 `~/.config/baton/beigua.token`，目录权限 0700、文件权限 0600。令牌不经过聊天，不交给任务发起人，也不放入命令参数。先确认目标主机和操作者；Baton 不主动登录接收方主机，不安装或强制启动 agent。

接收方自愿配置 MCP stdio 适配器，设置 `BATON_URL=https://example.com/mcp` 为实际的独立测试入口。令牌文件路径可以出现在配置中，令牌原值不可以。私有 CA 使用 `BATON_CA_FILE`，不跳过 TLS 校验。

接收方主机无法安装 MCP SDK（例如只有 Python 3 标准库、没有 venv）时，改用单文件命令行客户端 `adapters/baton_cli.py`，不需要下面的适配器，见 [标准库 CLI](stdlib-cli.md)。

适配器默认仅允许 `submit_task,get_task,list_tasks,fetch_inbox`。下文的领取、续租、评论和提交结果必须显式加入白名单：

```sh
adapters/launch_mcp.sh beigua "$HOME/.config/baton/beigua.token" \
  --allow-tools submit_task,get_task,list_tasks,fetch_inbox,claim,renew,comment,submit_result
```

`--allow-tools` 接受逗号分隔的已知 Baton 工具名称，区分大小写，可去除名称两端空格；它**替换**默认白名单，重复名称合并，空名称、未知名称和通配符会报错。也可把该参数直接传给 `adapters/mcp_stdio.py`。发起人或审阅人使用下文的建页、分享、派单扩展、审阅和审计工具时，也应在自己的适配器中显式加入相应名称，例如 `create_space,create_page,share,gate,x_audit`；白名单不会扩大服务端权限。

**角色预设。** 不想逐个列名称时，`--allow-tools` 也接受下列预设，可与工具名混用（如 `@assignee,x_audit`），展开后合并去重：

| 预设 | 展开为 |
| --- | --- |
| `@reader` | `get_task`、`list_tasks`、`fetch_inbox`、`wait_inbox`、`list_comments`、`get_page`、`list_pages`、`list_audit`、`ready` |
| `@assignee` | `@reader` + `claim`、`renew`、`comment`、`submit_result`、`mark_blocked`、`handoff` |
| `@reviewer` | `@reader` + `comment`、`gate` |
| `@owner` | `@reviewer` + `submit_task`、`create_task_block`、`assign_task`、`plan`、`set_work_face`、`set_priority`、`share`、`create_space`、`create_page`、`delegation_create`、`delegation_revoke`、`delegation_list` |

```sh
adapters/launch_mcp.sh beigua "$HOME/.config/baton/beigua.token" --allow-tools @assignee
```

预设只展开为已知工具名；未知预设（如 `@admin`，大小写也要一致）、空名称和通配符都会报错退出。不带 `--allow-tools` 时默认白名单不变，仍是 `submit_task,get_task,list_tasks,fetch_inbox`；显式传参（含预设）**替换**默认。预设只缩小适配器暴露的工具，不扩大权限：`@reviewer` 能调用 `gate`，但能否通过仍取决于根身份或审批人委托；`@owner` 也不会让主体成为 Space 的 owner。

远端发现结果中的未允许工具不会暴露给客户端，直接猜测名称调用也在本地拒绝。每次发现响应有过滤项时写一条汇总审计（仅记录数量），每次拒绝调用写一条审计，不记录工具原名或参数正文。设置 `BATON_AUDIT_FILE` 或 `--audit-file` 时审计追加到权限为 0600 的文件；未设置或文件写入失败时，审计写到 stderr，不占用 MCP 的 stdout。成功调用回执仍只在配置审计文件时记录。

## 发起人派单

1. 用 `create_space`、`create_page` 建立测试页面，或使用已有页面。
2. 用 `share` 给接收人的账户明确授予该页面的 edit 权限。身份与账户的绑定由管理员设置；分配任务不会自动扩大权限。
3. 调用 `submit_task`，携带稳定的 `idempotency_key`、任务说明 `prompt`、`page_id`、`assignee_principal: "beigua"` 和声明范围 `scopes`。

指定接收人的任务进入手工拉取模式，不会被内置 Codex worker 抢走。默认审阅人是发起人的 principal。`assignee` 是允许领取的人，`holder` 是当前有效租约持有人，派单不会伪造接收方已领取。

也可以先创建 `create_task_block` 并配置 reviewers，再调用 `assign_task(task_id, target_principal, operation_id)`。在任务线程中 `comment` 后调用 `mention(comment_id, target_type="agent", target_id="beigua", operation_id)` 可发出行动请求；提及不会暗中改派任务或授予权限。需要独占指定领取时，应显式设置 assignee。

## 接收人领取与回报

1. 自行调用 `fetch_inbox`，查看“@我的和分给我的”，核对任务、请求者、状态和范围。
2. 对愿意处理的任务调用 `claim(task_id, operation_id, lease_seconds)`。保存返回的 `epoch`；其他身份不能抢走显式分给自己的任务。
3. 在自己的 CLI 权限边界内执行。需要更多时间时调用 `renew(task_id, epoch, operation_id, lease_seconds)`；过期或 fence 失效后停止提交旧结果，不能绕过检查。
4. 调用 `submit_result(task_id, epoch, result, evidence, operation_id)`。结果以实际持有人署名写入同一任务线程，任务进入 `awaiting_gate`。也可用 `comment(page_id, thread_id=task_id, body, operation_id)`补充说明。
5. 发起人或配置的 reviewer 检查结果后调用 `gate(task_id, expected_epoch=result_epoch, decision="pass", evidence, operation_id)`，此后才成为 `done`，兼容视图为 `completed`。Space owner（根）可直接批准；其他配置的 reviewer 还必须持有覆盖该任务的审批人委托（`delegation_required`），持有人和提交者不能批准自己执行的结果（根可显式带 `self_approval`）。

   审阅人收到的 `task.awaiting_gate` 通知（`wait_inbox`）已带 `result_epoch` 与 `result_author`，直接作为 `expected_epoch` 即可，不必先 `get_task`。审阅人发现小问题时可用 `decision="edit"` 带修改批准：同时传 `edited_result`（替换后的完整结果，须与原结果不同）和可选 `reason`；这算一次 pass，任务的 `result` 变成修改后的文本，原文保留在 gate 记录里，审计多一条 `gate.result_overridden`（只含两份结果的哈希、审批人和理由）。`gate` 的返回值带 `gate` 对象，其 `authority` 写明审批人如何取得权限：`root`、`root (self-approval)` 或 `delegated by <授权人> via <委托 id>`。

每个有副作用的新动作使用一个稳定 UUID `operation_id`；网络重试复用它，不重新生成。MCP `submit_task` 兼容工具根据 principal 与业务键生成操作 ID；直接调用 HTTP 时需显式提供。

## 验收证据

使用 `x_audit(task_id)` 核对 `task.assigned → task.claimed → task.result_submitted → gate.reviewed → task.completed`，全部关联同一任务 ID；评论线程 ID 也等于任务 ID。保存回执到仓库外的私有目录。

`scripts/assignment_acceptance.py` 用不同令牌验证本地协议，明确标记 `remote_cli_acceptance: NOT_RUN`。它不能代替两个真实 CLI、真实主机和自愿拉取的验收；只有实际接收方运行完成后，才记录真实跨机通过。
