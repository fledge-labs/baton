<!-- SPDX-License-Identifier: Apache-2.0 -->

# 标准库命令行客户端：没有 MCP SDK 的主机

`adapters/baton_cli.py` 是单文件、零依赖的 Baton 命令行客户端，只需要 Python 3.9+ 标准库，不需要 venv、pip 或 MCP SDK。适用于只能运行系统自带 `python3` 的主机，例如一台没有安装任何第三方包的 VPS。

它直接调用 Baton 自有的 HTTP API（`POST /v1/space/{operation}`，见 [API](../docs/api.md)），不使用 MCP。每个子命令对应一个操作，成功时把操作的 `result` 以 JSON 打印到 stdout。权限、租约和 gate 规则与 MCP 完全相同，客户端不会扩大任何权限。需要 MCP 工具的 agent 仍按 [指定接收人](named-agent.md) 配置适配器。

## 安全约束

- 令牌只从 `--token-file` 指向的文件读取，不接受命令行参数或环境变量里的令牌值，也没有缩写匹配（`--token` 不会被当成 `--token-file`）。令牌文件应为 0600；令牌不会出现在 stdout、stderr 和任何错误信息中。
- TLS 校验始终开启，不提供关闭选项。私有 CA 用 `--ca-file`（或 `BATON_CA_FILE`）；`--pin-sha256` 额外要求服务端叶证书的公钥（SPKI）SHA-256 与给定值一致，不一致时在发送任何请求（包括令牌）之前断开。
- 只接受 `https://`。`http://` 仅允许主机为 `localhost` 或 `127.0.0.1`，其他主机直接拒绝。URL 不能带用户信息、查询串或片段。
- 不读取 `HTTP_PROXY`/`HTTPS_PROXY`，也不跟随重定向，令牌只发给 `--url` 指定的主机。
- 错误信息只显示 Baton 的稳定错误码和 HTTP 状态，例如 `baton_cli: error: stale_claim (HTTP 409)`；网络和 TLS 错误只显示固定类别，不含服务端文本或 URL。

退出码：`0` 成功；`1` Baton 返回错误；`2` 用法或本地配置错误；`3` 网络或 TLS 错误。

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
| `tasks` | `list_tasks` | `--limit` |
| `ready` | `ready` | `--page`、`--limit` |
| `submit` | `submit_task` | `--key`，`--prompt` 或 `--prompt-file`，`--page`、`--assignee`、`--scope kind=value`（可重复，kind 为 path/module/migration；`--scope none` 表示不触碰任何仓库路径，不能与其他 scope 组合）、`--depends-on`（可重复或逗号分隔） |
| `claim` | `claim` | `--task`、`--lease` |
| `renew` | `renew` | `--task`、`--epoch`、`--lease` |
| `comment` | `comment` | `--page`、`--thread`、`--body` 或 `--body-file` |
| `result` | `submit_result` | `--task`、`--epoch`，`--result` 或 `--result-file` |
| `gate` | `gate` | `--task`、`--epoch`、`--decision pass\|fail` |
| `audit` | `list_audit` | `--task` 或 `--space`、`--limit` |
| `call` | 任意操作 | `call <operation> --json '<参数对象>'`，或 `--json-file` |
| `watch` | 循环 `wait_inbox` | `--agent`、`--state`、`--timeout`、`--limit` |

- 写操作（`submit`、`claim`、`renew`、`comment`、`result`、`gate`）自动生成 UUIDv4 `operation_id`，也可用 `--operation-id` 指定。读操作不发送 `operation_id`。`call` 不替你生成：写操作需要在 `--json` 里放 `operation_id`，或用 `--operation-id`。
- `--prompt-file`、`--body-file`、`--result-file`、`--json-file` 传 `-` 表示读取 stdin。`--epoch` 在 `gate` 中对应 API 的 `expected_epoch`，即被审阅结果所属的 epoch。
- stdout 始终是纯 JSON，并以 UTF-8 输出原文（`ensure_ascii=False`），中文不会被转义成 `\uXXXX`。
- `claim`、`renew`、`result`、`gate` 默认再向 stderr 打印一行摘要，如 `task <id> state=claimed epoch=3`；用 `--no-summary` 关闭，`submit`、`task`、`comment`、`call` 可用 `--summary` 打开。stdout 不受影响。
- 省略 scope 时，带 `--assignee` 的手工任务默认声明 `none`（不占用任何 scope，多个无文件足迹的审阅、文档任务可同时领取）；不带 `--assignee` 的自动任务仍使用保守的仓库根占位范围。
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
# {"notifications":[{"event":"task.assigned","object_type":"task","object_id":"<TASK_ID>","seq":7,...}],"next_seq":7}
TASK=<上面的 object_id>

# 读取任务（通知只含元数据；prompt 要用 task 读取），并核对请求者、范围
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

## 审阅人流程：gate

审阅人是任务配置的 reviewer，默认就是发起人 `nangua`。下面以 `nangua` 的令牌运行：

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

# 核对全程审计：task.assigned → task.claimed → task.result_submitted → gate.reviewed
baton audit --task "$TASK"
```

只有 `pass` 之后任务才变成 `done`，接收人通过 `wait` 收到 `gate.decided`。`--epoch` 与当前候选不一致时返回 `stale_claim`，需要先重新读取任务。

## 常驻监听：watch

`watch` 循环调用 `wait_inbox`，每条通知打印一行 JSON，并把最后的 `next_seq` 保存到状态文件，下次启动从这里继续，断线或重启不丢事件：

```sh
# SPDX-License-Identifier: Apache-2.0
python3 "$HOME/baton/adapters/baton_cli.py" --token-file "$HOME/.config/baton/beigua.token" \
  watch --agent beigua
```

- 状态文件默认是 `~/.local/state/baton/<agent>.seq`（`--agent` 决定文件名），可用 `--state` 指定。文件权限 0600，目录 0700；内容损坏时以退出码 2 停止，不会悄悄从 0 重放。
- 先打印、再保存位置：进程在两步之间被杀时会重放最后一批，不会丢；处理通知的一方应按 `id` 去重。
- 每条通知只含元数据，用 `task`、`inbox` 等读取操作取内容。通知只是提示，不会自动领取或执行任务。
- `SIGINT`（Ctrl-C）和 `SIGTERM` 让它在当前批次处理完后以 0 退出。网络或 TLS 错误以 3 退出，Baton 错误以 1 退出；交给 systemd 之类的守护进程按 `Restart=on-failure` 重启即可，重启后从状态文件继续。

把每行通知交给自己的处理程序：

```sh
# SPDX-License-Identifier: Apache-2.0
python3 "$HOME/baton/adapters/baton_cli.py" --token-file "$HOME/.config/baton/beigua.token" \
  watch --agent beigua | while IFS= read -r line; do
    printf '%s\n' "$line" | python3 "$HOME/baton/handle_notification.py"
  done
```

## 排错

- 退出码 3 且提示 `tls: handshake or certificate verification failed`：CA 不受信任或主机名与证书不符。检查 `--ca-file`、证书的 SAN 以及 `--url` 里的主机名，不要尝试关闭校验（没有这个选项）。
- 退出码 3 且提示 `tls: server public key does not match --pin-sha256`：服务端公钥与指纹不一致。证书是否更换了密钥、是否连到了错误的主机，需要先确认，再更新指纹。
- 退出码 1 的 `unauthorized (HTTP 401)`：令牌无效或已撤销；`forbidden (HTTP 403)`：当前身份没有该操作的权限；其余错误码见 [API](../docs/api.md#错误与审计)。
- 退出码 2：用法或配置问题，stderr 的第一行说明了是哪一项，从不回显令牌或 URL。
