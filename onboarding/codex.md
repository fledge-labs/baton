<!-- SPDX-License-Identifier: Apache-2.0 -->

# Codex CLI 接入 Baton

本页是配置指南，不代表已在这个仓库对每个客户端版本完成原生端到端验收。Codex 配置字段另按 `0.154.0` 的上游 schema 核对。所有示例均为 Apache-2.0。

## 准备适配器

这部分也适用于其他 onboarding 文档，不要求安装 Codex。

1. 按 [README](../README.md) 启动 Baton。远程连接使用部署者配置的 HTTPS 入口；下文的 `https://example.com/mcp` 只是占位地址，必须替换。
2. 在运行适配器的主机克隆仓库，例如 `$HOME/baton`。安装 Python 3.12，并为适配器建立独立环境：

   ```sh
   # SPDX-License-Identifier: Apache-2.0
   python3.12 -m venv "$HOME/baton/.venv"
   "$HOME/baton/.venv/bin/python" -m pip install --require-hashes --only-binary=:all: \
     -r "$HOME/baton/deploy/requirements.txt"
   ```

   适配器与应用使用同一哈希锁文件，包含固定版本的 MCP SDK 及其完整依赖；不要单独安装未带哈希的 SDK。

3. 按 README 签发与 `<agent-id>` 对应的 token，将其保存在该主机上的 `<TOKEN_FILE>`，限制为该用户可读写，例如 `chmod 600 "<TOKEN_FILE>"`。配置只记录文件路径，不记录 token 内容。
4. `<HOST>` 是运行适配器的 SSH 主机或已配置别名，不一定是 gateway 主机。先在终端完成 SSH 身份认证及主机指纹核验；客户端随后使用 `BatchMode=yes`，不会等待交互登录。

适配器入口是 `adapters/launch_mcp.sh <agent-id> <TOKEN_FILE>`。它默认使用仓库 `.venv`；需要其他已安装依赖的 Python 时，设置 `BATON_PYTHON`。`BATON_URL` 是 gateway 的 MCP 地址。使用本地 CA 时，将 `BATON_CA_FILE` 设为适配器主机上的 CA 文件路径，例如 `$HOME/certs/baton-ca.pem`。

远程模式中，CLI 通过 SSH 传递 MCP stdio；token 文件保留在适配器主机。适配器仍会经 TLS 向 gateway 发送认证信息。示例中的 `$HOME` 由远程 shell 展开；`<TOKEN_FILE>` 也必须指向远程主机上的文件。

## 增量配置

在 `$HOME/.codex/config.toml` 中合并以下条目；也可使用可信项目的 `.codex/config.toml`。先备份配置，只增加或更新 `mcp_servers.baton`，保留其他服务器和模型设置。若已有同名服务器，应核对后编辑，勿重复追加 TOML 表。

```toml
# SPDX-License-Identifier: Apache-2.0
[mcp_servers.baton]
command = "ssh"
args = ["-T", "-o", "BatchMode=yes", "<HOST>", 'BATON_URL="https://example.com/mcp" exec "$HOME/baton/adapters/launch_mcp.sh" "<agent-id>" "<TOKEN_FILE>"']
startup_timeout_sec = 30
tool_timeout_sec = 120
enabled_tools = ["submit_task", "get_task", "fetch_inbox", "list_tasks"]
default_tools_approval_mode = "prompt"

[mcp_servers.baton.tools.submit_task]
approval_mode = "approve"

[mcp_servers.baton.tools.get_task]
approval_mode = "approve"

[mcp_servers.baton.tools.fetch_inbox]
approval_mode = "approve"
```

这三项设置仅用于已获授权的任务提交、任务查询和mentions/assignments 读取。`list_tasks` 保持询问；不要扩大成全局自动允许。客户端的允许设置不能替代 gateway 的身份和权限检查。

若 CLI 与适配器在同一主机，将上述 `command` 改为 `sh`，`args` 改为以下数组；其余策略不变：

```toml
# SPDX-License-Identifier: Apache-2.0
args = ["-c", 'BATON_URL="https://example.com/mcp" exec "$HOME/baton/adapters/launch_mcp.sh" "<agent-id>" "<TOKEN_FILE>"']
```

此时 `$HOME` 和 token 文件路径都在本机解释。若需自定义 Python 或 CA，在同一个命令的 `exec` 前增加 `BATON_PYTHON="<PYTHON>"` 或 `BATON_CA_FILE="<CA_FILE>"`；设置客户端的本地环境变量不会自动将它们转发到 SSH 主机。

## 检查连接

运行 `codex mcp list`，重新启动会话后用 `/mcp` 检查 `baton`。先调用 `fetch_inbox`；再用一个明确授权的小任务调用 `submit_task`，用返回的任务 ID 调用 `get_task`，最后用 `fetch_inbox` 核对同一任务线程中的完成评论。连接成功或列出工具不等于任务链已成功。

失败时依次检查 SSH、适配器 Python 环境、token 文件权限、`BATON_URL` 和 CA。排查日志时不要打印 token，也不要将运行回执提交到仓库。

依据：[官方 MCP 文档](https://developers.openai.com/codex/mcp)、[Codex 0.154.0 配置 schema](https://github.com/openai/codex/blob/rust-v0.154.0/codex-rs/core/config.schema.json)。

## 空间协作与普通 HTTP

`fetch_inbox` 现在来自 PostgreSQL 的评论提及和任务分配视图；它只返回当前身份仍可访问的内容。任务状态中的 `submission_mail_status`、`mail_status`、`mail_error` 已分别更名为 `submission_thread_status`、`thread_status`、`thread_error`；适配器回执中的 `mail_thread_ids` 更名为 `thread_ids`。

空间、页面、块、评论、分享、提案和手工 claim/gate 工具的清单可通过 MCP 工具发现读取。需要使用新工具时，在已有配置内按实际授权扩展 `enabled_tools`，保留逐工具审批。没有 MCP 的任意 runtime 可直接调用 [Baton HTTP API](../docs/api.md)；协议标识为 `baton.space.v1`，这是 Baton 自有接口，不是 OpenAI Space wire protocol。

创建有依赖的任务时，在 submit_task 或 create_task_block 中同时传入 `depends_on=[前置任务UUID]`，确保任务与依赖原子建立，自动 worker 不会在后续 plan 前抢先领取。已有任务用 `plan(operation_id, graph)` 建立或扩展依赖图；graph 的每项只接受 task_id 和 depends_on。接收人用 `ready(page_id?, limit?)` 查看当前可领取任务，再调用 claim。ready 结果只是当前视图，领取时仍重新校验前置 gate、权限和租约。MCP enabled_tools 如需开放 plan/ready，应按本任务授权增量添加；普通 HTTP 使用同名操作，不需要特定 agent runtime。
