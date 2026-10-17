<!-- SPDX-License-Identifier: Apache-2.0 -->

# 标准库命令行客户端：没有 MCP SDK 的主机

**首次运行。** 复制下面一个代码块即可：它从固定提交下载 `baton_cli.py` 并读取一次收件箱。把三个尖括号占位符换成自己的值（`<BATON_URL>` 形如 `https://example.com`，`<TOKEN_FILE>` 是 0600 的令牌文件，`<CA_FILE>` 是私有 CA 的 PEM；使用系统信任的 CA 时去掉 `--ca-file` 一行）。

```sh
# SPDX-License-Identifier: Apache-2.0
mkdir -p "$HOME/baton/adapters" && \
curl -fsSL -o "$HOME/baton/adapters/baton_cli.py" \
  https://raw.githubusercontent.com/fledge-labs/baton/2a386723d3e1081a3586cddf778a2a5de782190f/adapters/baton_cli.py && \
python3 "$HOME/baton/adapters/baton_cli.py" inbox \
  --url <BATON_URL> --token-file <TOKEN_FILE> --ca-file <CA_FILE>
```

URL 中的 40 位十六进制是固定的提交，下载内容不会随分支更新而改变。需要 `comments`、`block` 或新的 `watch` 行为时，把它换成你确认过的、包含这些功能的提交，再重复上面的下载。以下各节假定文件位于 `$HOME/baton/adapters/baton_cli.py`。

`adapters/baton_cli.py` 是单文件、零依赖的 Baton 命令行客户端，只需要 Python 3.9+ 标准库，不需要 venv、pip 或 MCP SDK。适用于只能运行系统自带 `python3` 的主机，例如一台没有安装任何第三方包的 VPS。

它直接调用 Baton 自有的 HTTP API（`POST /v1/space/{operation}`，见 [API](../docs/api.md)），不使用 MCP。每个子命令对应一个操作，成功时把操作的 `result` 以 JSON 打印到 stdout。权限、租约和 gate 规则与 MCP 完全相同，客户端不会扩大任何权限。需要 MCP 工具的 agent 仍按 [指定接收人](named-agent.md) 配置适配器。

## 安全约束

- 令牌只从 `--token-file` 指向的文件读取，不接受命令行参数或环境变量里的令牌值，也没有缩写匹配（`--token` 不会被当成 `--token-file`）。令牌文件应为 0600；令牌不会出现在 stdout、stderr 和任何错误信息中。
- TLS 校验始终开启，不提供关闭选项。私有 CA 用 `--ca-file`（或 `BATON_CA_FILE`）；`--pin-sha256` 额外要求服务端叶证书的公钥（SPKI）SHA-256 与给定值一致，不一致时在发送任何请求（包括令牌）之前断开。
- 只接受 `https://`。`http://` 仅允许主机为 `localhost` 或 `127.0.0.1`，其他主机直接拒绝。URL 不能带用户信息、查询串或片段。
- 不读取 `HTTP_PROXY`/`HTTPS_PROXY`，也不跟随重定向，令牌只发给 `--url` 指定的主机。
- 错误信息只显示 Baton 的稳定错误码和 HTTP 状态，例如 `baton_cli: error: stale_claim (HTTP 409)`；网络和 TLS 错误只显示固定类别，不含服务端文本或 URL。

退出码：`0` 成功；`1` Baton 返回错误；`2` 用法或本地配置错误；`3` 网络或 TLS 错误（`watch` 除外：它遇到这些错误会自动重连，见下文）。

遇到 `422 invalid` 时，服务端会给出字段级提示，CLI 在错误码之后每个字段打印一行 `baton_cli:   <字段>: expected <期望的类型或格式>`。提示不含你提交的任何值；只有由普通可打印字符组成的字段名和说明才会显示。

## 准备

1. 把 `adapters/baton_cli.py` 复制到主机，例如 `$HOME/baton/adapters/baton_cli.py`；该文件不依赖仓库里的其他文件。确认 `python3 --version` 为 3.9 或更新。
2. 令牌由管理员签发，经已有的 SSH/SCP 渠道放到 `$HOME/.config/baton/beigua.token`，目录 0700、文件 0600，见 [指定接收人](named-agent.md#操作者准备)。
3. 设置入口。`BATON_URL` 是基础地址，例如 `https://example.com`（不带 `/v1/space`）。它与 MCP 适配器共用同一个变量：以 `/mcp` 结尾的值会自动去掉这个后缀。私有 CA 设置 `BATON_CA_FILE`：

```sh
# SPDX-License-Identifier: Apache-2.0
export BATON_URL=https://example.com
export BATON_CA_FILE=$HOME/certs/baton-ca.pem

baton() {
  python3 "$HOME/baton/adapters/baton_cli.py" --token-file "$HOME/.config/baton/beigua.token" "$@"
}
baton tasks
```

私有 CA 需要是规范的 CA 证书（`basicConstraints` 标明 CA、带 key identifier）；Python 3.13 起默认启用严格的证书校验。

连接选项 `--url`、`--token-file`、`--ca-file`、`--pin-sha256`、`--request-timeout` 放在子命令前后都可以。

### 固定服务端公钥（可选）

在能信任的网络位置计算一次 SPKI 指纹，随后在每次调用中加上 `--pin-sha256`：

```sh
# SPDX-License-Identifier: Apache-2.0
openssl s_client -connect example.com:443 -servername example.com </dev/null 2>/dev/null \
  | openssl x509 -pubkey -noout | openssl pkey -pubin -outform DER \
  | openssl dgst -sha256 -binary | base64
```

固定的是证书里的公钥，证书续期但公钥不变时指纹不变；更换密钥时需要同时更新指纹。指纹校验是在正常的 CA 与主机名校验之外的额外要求，不能替代它们。

## 子命令

| 子命令 | 操作 | 主要参数 |
| --- | --- | --- |
| `inbox` | `fetch_inbox` | `--limit` |
| `wait` | `wait_inbox` | `--after-seq`（默认 0）、`--timeout`（默认 25，0–55 秒）、`--limit` |
| `task` | `get_task` | `TASK_ID` 或 `--task` |
| `comments` | `list_comments` | `--task`（按任务读取其页面和线程），或 `--page` 加可选的 `--thread`；`--task` 不能与 `--page`/`--thread` 同用 |
| `tasks` | `list_tasks` | `--limit` |
| `ready` | `ready` | `--page`、`--limit` |
| `submit` | `submit_task` | `--key`，`--prompt` 或 `--prompt-file`，`--page`、`--assignee`、`--scope kind=value`（可重复，kind 为 path/module/migration；`--scope none` 表示不触碰任何仓库路径，不能与其他 scope 组合）、`--depends-on`（可重复或逗号分隔） |
| `claim` | `claim` | `--task`、`--lease` |
| `renew` | `renew` | `--task`、`--epoch`、`--lease` |
| `comment` | `comment` | `--page`、`--thread`、`--body` 或 `--body-file` |
| `result` | `submit_result` | `--task`、`--epoch`，`--result` 或 `--result-file` |
| `gate` | `gate` | `--task`、`--epoch`（省略时取任务当前的 `result_epoch`）、`--decision pass\|fail\|edit`、`--edited-result` 或 `--edited-result-file`（仅 `edit`）、`--reason`、`--self-approval` |
| `block` | `mark_blocked` | `--task`、`--reason scope_conflict\|lease_held\|dependency_blocked\|waiting_input\|other`，可选 `--note` 或 `--note-file` |
| `audit` | `list_audit` | `--task` 或 `--space`、`--limit` |
| `call` | 任意操作 | `call <operation> --json '<参数对象>'`，或 `--json-file` |
| `watch` | 循环 `wait_inbox` | `--agent`、`--state`、`--timeout`、`--limit`、`--summary/--no-summary`、`--exec`、`--exec-timeout` |

- 写操作（`submit`、`claim`、`renew`、`comment`、`result`、`gate`、`block`）自动生成 UUIDv4 `operation_id`，也可用 `--operation-id` 指定。读操作不发送 `operation_id`。`call` 不替你生成：写操作需要在 `--json` 里放 `operation_id`，或用 `--operation-id`。
- `--prompt-file`、`--body-file`、`--result-file`、`--json-file` 传 `-` 表示读取 stdin。`--epoch` 在 `gate` 中对应 API 的 `expected_epoch`，即被审阅结果所属的 epoch；省略时见下文“审阅人流程”。`--edited-result-file` 同样可传 `-` 读取 stdin。
- stdout 始终是纯 JSON，并以 UTF-8 输出原文（`ensure_ascii=False`），中文不会被转义成 `\uXXXX`。
- `claim`、`renew`、`result`、`gate`、`block` 默认再向 stderr 打印一行摘要，如 `task <id> state=claimed epoch=3`；用 `--no-summary` 关闭，`submit`、`task`、`comment`、`call` 可用 `--summary` 打开。stdout 不受影响。`gate` 的摘要行还带审批人与权限来源，见下文。`watch` 的 `--summary` 含义不同，见下文。
- 省略 scope 时，所有任务（不论是否带 `--assignee`）默认声明 `none`（不占用任何 scope，多个无文件足迹的审阅、文档任务可同时领取）。不带 `--assignee` 的 `submit` 创建未指派的手工任务，任何对该页面有 edit 权限的主体都能用 `ready` 看到并 `claim` 它；Baton 不执行任务。
- 写操作失败后，stderr 会多打一行 `operation_id=...`。网络中断后不知道操作是否生效时，用同一个 `--operation-id` 重试，服务端保证只产生一次效果；不要重新生成。

## 命名接收人流程：wait → claim → comment → result

沿用 [指定接收人](named-agent.md) 的角色：发起人 `nangua`，接收人 `beigua`。下面的命令都在接收方主机上以 `beigua` 的令牌运行，使用上文定义的 `baton` 函数。

先定义一个读取 JSON 字段的小函数（只用标准库）：

```sh
# SPDX-License-Identifier: Apache-2.0
field() { python3 -c 'import json, sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1"; }
```

发起人在自己的主机上派单（页面已通过 `share` 给 `beigua` 授予 edit，页面 ID 与账户见管理员）：

```sh
# SPDX-License-Identifier: Apache-2.0
python3 "$HOME/baton/adapters/baton_cli.py" --token-file "$HOME/.config/baton/nangua.token" \
  submit --key release-notes-001 --prompt-file prompt.md \
  --page "$PAGE_ID" --assignee beigua --scope path=docs/release-notes
```

接收人：

```sh
# SPDX-License-Identifier: Apache-2.0
# 1. wait：从上次的位置长轮询，最多等 25 秒；下次以 next_seq 继续
baton wait --after-seq 0 --timeout 25
# {"notifications":[{"event":"task.assigned","object_type":"task","object_id":"<TASK_ID>","seq":7,
#   "actor":{"principal":"nangua","kind":"human"},"subject":"[assigned] 写发布说明","body_preview":"...",...}],"next_seq":7}
TASK=<上面的 object_id>

# 读取任务（通知里的 body_preview 只是前 200 个字符；完整说明用 task 读取），并核对请求者、范围
baton task "$TASK"

# 2. claim：保存返回的 epoch，后续写操作都要带它
EPOCH=$(baton claim --task "$TASK" --lease 300 | field epoch)
PAGE=$(baton task "$TASK" | field page_id)

# 做事期间按需续租；租约过期或 epoch 失效后不要再提交旧结果
baton renew --task "$TASK" --epoch "$EPOCH" --lease 300

# 3. comment：任务的线程 ID 等于任务 ID
baton comment --page "$PAGE" --thread "$TASK" --body "已开始，预计十分钟内给出结果"

# 4. result：任务进入 awaiting_gate，等审阅人决定
printf '%s\n' "发布说明已写入 docs/release-notes.md" \
  | baton result --task "$TASK" --epoch "$EPOCH" --result-file -
```

`result` 之后任务停在 `awaiting_gate`，结果还不算完成。接收人不能给自己的结果打 `gate`，这样的调用会得到 `forbidden (HTTP 403)`。

## 无法领取时：block

被指派的接收人（或任务的 reviewer）领取不了任务时，不必沉默等待。`block` 在任务线程发一条 `[blocked]` 评论、通知任务发起人（通知事件 `task.blocked`），并让任务视图出现 `blocked={reason_code, by, at}`，直到任务被领取或改派：

```sh
# SPDX-License-Identifier: Apache-2.0
# reason 为 scope_conflict、lease_held、dependency_blocked、waiting_input、other 之一
baton block --task "$TASK" --reason scope_conflict --note "src/ 目录被另一个任务占用"
# 任何有该任务查看权限的人都能读线程，不必知道页面 ID
baton comments --task "$TASK"
```

它不会领取任务，也不改变指派；接收人的响应时效按“已响应”计算。非接收人、非 reviewer 得到 `forbidden (HTTP 403)`，看不到该任务的身份得到 `not_found (HTTP 404)`。

## 审阅人流程：gate

审阅人是任务配置的 reviewer，默认就是发起人 `nangua`。只做 reviewer 还不够：做 gate 决定的主体（人或 agent）必须持有覆盖该任务的有效审批人委托，否则返回 `delegation_required (HTTP 403)`；执行或提交过该结果的主体（含同账户）不能批准。Space 的 owner 是根审批人，天然有审批权（不必是 reviewer，也不需要委托）；其他审批人持有由 owner 或其下游授予的审批人委托，运维人员可按 owner 的决定记录（见 [README](../README.md#审批关卡与委托)）。根批准自己提交或执行的工作需要 `--self-approval`。下面的例子里 `nangua` 创建了 Space，所以它是根，以它的令牌运行：

```sh
# SPDX-License-Identifier: Apache-2.0
baton() {
  python3 "$HOME/baton/adapters/baton_cli.py" --token-file "$HOME/.config/baton/nangua.token" "$@"
}

# 提交结果时审阅人会收到 task.awaiting_gate
baton wait --after-seq 0 --timeout 25

# 读取结果，并取出结果所属的 epoch（result_epoch）
baton task "$TASK"
RESULT_EPOCH=$(baton task "$TASK" | field result_epoch)

# 核对后给出决定；--epoch 必须是上面的 result_epoch
baton gate --task "$TASK" --epoch "$RESULT_EPOCH" --decision pass
# 不通过时：baton gate --task "$TASK" --epoch "$RESULT_EPOCH" --decision fail

# 省略 --epoch：自动取任务当前的 result_epoch（一次 get_task），并在 stderr 说明用了哪个 epoch
baton gate --task "$TASK" --decision pass
# baton_cli: gate: --epoch not given; using result_epoch=1 from get_task

# 带修改批准：用 --edited-result（或 --edited-result-file）给出替换后的完整结果，--reason 说明原因
baton gate --task "$TASK" --decision edit --edited-result "修订后的完整结果" --reason "补上了遗漏的数字"

# 核对全程审计：task.assigned → task.claimed → task.result_submitted → gate.reviewed
baton audit --task "$TASK"
```

只有 `pass`（或 `edit`）之后任务才变成 `done`（需要双人确认时要两位审批人都通过），接收人通过 `wait` 收到 `gate.decided`。`--epoch` 与当前候选不一致时返回 `stale_claim`，需要先重新读取任务。

**省略 `--epoch`。** `task.awaiting_gate` 通知本身已带 `result_epoch` 和 `result_author`（`baton watch --summary` 的通知行会显示），审批人可以直接使用。`gate --task <id>` 省略 `--epoch` 时，CLI 先调用一次 `get_task`，取任务当前的 `result_epoch` 作为 `expected_epoch`，并向 stderr 打印 `baton_cli: gate: --epoch not given; using result_epoch=<n> from get_task`。任务没有可审的结果（不在 `awaiting_gate`）时退出 2，不会发出 gate 请求。需要钉住某一代结果时仍应显式传 `--epoch`：任务在你读取之后又被退回重做，省略 `--epoch` 会审到新的结果，显式 epoch 则返回 `stale_claim`。

**带修改批准（`--decision edit`）。** 必须带 `--edited-result` 或 `--edited-result-file`（`-` 读 stdin），其他决定不能带；两者缺一或多余时退出 2，不发请求。替换文本须与当前结果不同，否则返回 `invalid (HTTP 422)`。只有能 `pass` 的人（根或持有委托的 reviewer）能 `edit`，它计为一次 pass；任务 `result` 变成修改后的文本，原文保存在 gate 记录里并出现在任务视图的 `original_result`，审计多一条 `gate.result_overridden`。

**审批人与权限来源。** `gate` 的 JSON 输出带 `gate` 对象，`--summary` 行（stderr）在 `task <id> state=<state> epoch=<n>` 之后追加决定、审批人和权限来源，例如：

```text
task 7d1c… state=done epoch=1 gate=pass approver=nangua authority=root
task 7d1c… state=done epoch=1 gate=pass approver=nangua authority=root (self-approval)
task 7d1c… state=done epoch=1 gate=edit approver=sunflower authority=delegated by nangua via 5b0e…
```

`authority` 只有这三种写法；只回显格式合法的字段。

## 常驻监听：watch

`watch` 循环调用 `wait_inbox`，每条通知在 stdout 打印一行 JSON，并把最后的 `next_seq` 保存到状态文件，下次启动从这里继续，断线或重启不丢事件：

```sh
# SPDX-License-Identifier: Apache-2.0
python3 "$HOME/baton/adapters/baton_cli.py" --token-file "$HOME/.config/baton/beigua.token" \
  watch --agent beigua
```

- 状态文件默认是 `~/.local/state/baton/<agent>.seq`（`--agent` 决定文件名），可用 `--state` 指定。文件权限 0600，目录 0700；内容损坏时以退出码 2 停止，不会悄悄从 0 重放。
- 先打印、再保存位置：进程在两步之间被杀时会重放最后一批，不会丢；处理通知的一方应按 `id` 去重。
- 通知行含 `actor`（`principal` 与 `kind`）、`subject`（如 `[assigned] 任务说明开头`、`comment on <任务 id>`）和 `body_preview`（评论或手工任务说明的前 200 个字符）。这些字段在读取时按当前查看权限计算，权限被撤销后整条通知不再返回；自动任务的说明保持不可见。通知只是提示，不会自动领取或执行任务。
- **自动重连。** 网络错误、TLS 错误和 5xx（以及 429）不会让 `watch` 退出：它在 stderr 写一行原因，按 1、2、4、8、16、32、60 秒的指数退避（上限 60 秒）重试，成功后退避归零，并无限期运行。4xx 等重试无法解决的错误（令牌无效 401、权限不足、参数错误）仍以退出码 1 停止；用法错误以 2 停止。
- **`--summary`（默认开启）。** 每条通知另向 stderr 打印一行便于人读的摘要，如 `notification seq=7 task.assigned task=<id> from=nangua(human) - [assigned] 核对发布清单`。`subject` 里的非可打印字符会被替换，不会向终端注入控制序列；`--no-summary` 关闭。stdout 始终只有 JSON 行。
- **`--exec '<命令>'`。** 每条通知运行一次该命令，通知的 JSON 从 stdin 传入。命令字符串原样交给 shell，通知里的任何字段都不会被拼进命令、参数或环境变量，因此 `subject` 中的 `$(...)`、反引号和分号不会被执行；命令自己从 stdin 读取并解析 JSON。命令的输出写到 stderr，退出码非 0 只记一行警告而不会停止 watch，运行超过 `--exec-timeout`（默认 300 秒）的命令连同它启动的进程一起被终止。位置在命令处理完这一批之后才保存。
- `SIGINT`（Ctrl-C）和 `SIGTERM` 让它在当前批次处理完后以 0 退出；等待中的请求会被放弃，几分之一秒内退出。仍可交给 systemd 之类的守护进程按 `Restart=on-failure` 管理。

把每条通知交给自己的处理程序，不必再自己写读取循环：

```sh
# SPDX-License-Identifier: Apache-2.0
python3 "$HOME/baton/adapters/baton_cli.py" --token-file "$HOME/.config/baton/beigua.token" \
  watch --agent beigua --exec 'python3 "$HOME/baton/handle_notification.py"'
```

## 排错

- 退出码 3 且提示 `tls: handshake or certificate verification failed`：CA 不受信任或主机名与证书不符。检查 `--ca-file`、证书的 SAN 以及 `--url` 里的主机名，不要尝试关闭校验（没有这个选项）。
- 退出码 3 且提示 `tls: server public key does not match --pin-sha256`：服务端公钥与指纹不一致。证书是否更换了密钥、是否连到了错误的主机，需要先确认，再更新指纹。
- 退出码 1 的 `unauthorized (HTTP 401)`：令牌无效或已撤销；`forbidden (HTTP 403)`：能看到对象但当前身份没有该操作的权限；`not_found (HTTP 404)`：对象不存在，或当前身份看不到它（两种情况不可区分）；其余错误码见 [API](../docs/api.md#错误与审计)。
- 退出码 2：用法或配置问题，stderr 的第一行说明了是哪一项，从不回显令牌或 URL。
