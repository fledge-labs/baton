<!-- SPDX-License-Identifier: Apache-2.0 -->

# Claude Code 接入 Baton

先完成[适配器公共准备](codex.md#准备适配器)，替换 `<HOST>`、`<agent-id>`、`<TOKEN_FILE>` 和示例 HTTPS 地址。这是配置指南，不是本仓库的原生客户端验收声明。

从要使用 Baton 的项目目录运行以下命令。它只添加一个名为 `baton` 的服务器，使用当前项目的 local scope；不替换其他服务器。先用 `claude mcp list` 检查同名条目，已有条目应单独核对后编辑。

```sh
# SPDX-License-Identifier: Apache-2.0
claude mcp add --scope local --transport stdio baton -- \
  ssh -T -o BatchMode=yes '<HOST>' \
  'BATON_URL="https://example.com/mcp" exec "$HOME/baton/adapters/launch_mcp.sh" "<agent-id>" "<TOKEN_FILE>"'
```

local scope 保存在 `$HOME/.claude.json` 的对应项目条目中；它不同于项目共享的 `.mcp.json`。如需手工配置，以下是 `mcpServers` 内要合并的服务器定义；不要覆盖整个 `$HOME/.claude.json`。

配置示例：`SPDX-License-Identifier: Apache-2.0`（JSON 不支持注释）。

```json
{
  "baton": {
    "type": "stdio",
    "command": "ssh",
    "args": [
      "-T", "-o", "BatchMode=yes", "<HOST>",
      "BATON_URL=\"https://example.com/mcp\" exec \"$HOME/baton/adapters/launch_mcp.sh\" \"<agent-id>\" \"<TOKEN_FILE>\""
    ]
  }
}
```

运行 `claude mcp list`，重启会话后用 `/mcp` 查看工具。按现有权限策略允许 `fetch_inbox` 检查认证；完整任务链按[公共检查步骤](codex.md#检查连接)验证。无需启用跳过权限检查的模式。

整个远程命令用单引号包裹，因此 `$HOME` 由 SSH 主机展开。token 文件留在适配器主机；适配器通过 TLS 向 gateway 发送认证信息。自定义 CA 或 Python 按公共准备配置在远程命令中。

依据：[Claude Code 官方 MCP 文档](https://code.claude.com/docs/en/mcp#option-3-add-a-local-stdio-server)、[MCP 配置作用域](https://code.claude.com/docs/en/mcp#mcp-installation-scopes)。
