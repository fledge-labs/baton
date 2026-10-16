<!-- SPDX-License-Identifier: Apache-2.0 -->

# Antigravity CLI（agy）接入 Baton

先完成[适配器公共准备](codex.md#准备适配器)，替换 `<HOST>`、`<agent-id>`、`<TOKEN_FILE>` 和示例 HTTPS 地址。这是配置指南，不是本仓库的原生客户端验收声明。

在 `$HOME/.gemini/config/mcp_config.json`，或项目 `.agents/mcp_config.json` 的 `mcpServers` 中增量合并 `baton`。先备份，保留其他服务器；不要用下面的片段覆盖整个文件。

配置示例：`SPDX-License-Identifier: Apache-2.0`（JSON 不支持注释）。

```json
{
  "mcpServers": {
    "baton": {
      "command": "ssh",
      "args": [
        "-T", "-o", "BatchMode=yes", "<HOST>",
        "BATON_URL=\"https://example.com/mcp\" exec \"$HOME/baton/adapters/launch_mcp.sh\" \"<agent-id>\" \"<TOKEN_FILE>\""
      ]
    }
  }
}
```

对于已授权的三种操作，将以下三条规则追加到 `$HOME/.gemini/antigravity-cli/settings.json` 的 `permissions.allow` 数组。保留已有的 allow、ask、deny 和其他设置：

配置示例：`SPDX-License-Identifier: Apache-2.0`。

```json
{
  "permissions": {
    "allow": [
      "mcp(baton/submit_task)",
      "mcp(baton/get_task)",
      "mcp(baton/fetch_inbox)"
    ]
  }
}
```

`list_tasks` 不在此预授权名单中。已有 deny 或 ask 规则可能优先于 allow；不要删除更严格的策略或使用 `mcp(*)` 来解决冲突。

重启 `agy`，用 `/mcp` 查看状态和工具，再调用 `fetch_inbox` 检查认证。完整任务链按[公共检查步骤](codex.md#检查连接)验证。模型应选择当前可用且获准使用的模型。

SSH 仅连接适配器主机，token 文件留在该主机；适配器通过 TLS 向 gateway 发送认证信息。`BATON_PYTHON`、`BATON_CA_FILE` 应按公共准备配置在适配器进程一侧。

依据：[官方 MCP 配置](https://antigravity.google/docs/mcp#antigravity-cli)、[CLI 权限](https://antigravity.google/docs/permissions#cli-fine-grained-permissions)。
