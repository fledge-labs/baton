<!-- SPDX-License-Identifier: Apache-2.0 -->

# Cline 接入 Baton

先完成[适配器公共准备](codex.md#准备适配器)，替换 `<HOST>`、`<agent-id>`、`<TOKEN_FILE>` 和示例 HTTPS 地址。这是配置指南，不是本仓库的原生客户端验收声明。

CLI 的默认配置为 `$HOME/.cline/data/settings/cline_mcp_settings.json`，可由 `CLINE_MCP_SETTINGS_PATH` 改写。IDE 扩展中打开 **MCP Servers → Configure → Configure MCP Servers**，编辑它实际使用的文件。先备份，只在现有 `mcpServers` 中合并 `baton`，保留其他配置。

配置示例：`SPDX-License-Identifier: Apache-2.0`（JSON 不支持注释）。

```json
{
  "mcpServers": {
    "baton": {
      "command": "ssh",
      "args": [
        "-T", "-o", "BatchMode=yes", "<HOST>",
        "BATON_URL=\"https://example.com/mcp\" exec \"$HOME/baton/adapters/launch_mcp.sh\" \"<agent-id>\" \"<TOKEN_FILE>\""
      ],
      "disabled": false,
      "autoApprove": []
    }
  }
}
```

选用当前可用、已授权且支持工具调用的模型；本指南不固定限时免费模型。保留工具审批，先审核并允许一次 `fetch_inbox`。CLI 可用 `cline config mcp` 检查配置，IDE 可在 MCP 面板重启服务器并查看工具。完整任务链按[公共检查步骤](codex.md#检查连接)验证。

SSH 仅连接适配器主机，token 文件留在该主机；适配器通过 TLS 向 gateway 发送认证信息。自定义 CA 或 Python 使用公共准备中的 `BATON_CA_FILE`、`BATON_PYTHON`，而不是把 token 放进 JSON。

依据：[Cline 官方 MCP 文档](https://docs.cline.bot/mcp/mcp-overview)。
