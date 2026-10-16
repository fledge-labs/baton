<!-- SPDX-License-Identifier: Apache-2.0 -->

# Devin CLI 接入 Baton

先完成[适配器公共准备](codex.md#准备适配器)，替换 `<HOST>`、`<agent-id>`、`<TOKEN_FILE>` 和示例 HTTPS 地址。此处针对本地 Devin CLI，不是 Devin Cloud 的连接器配置，也不是本仓库的原生客户端验收声明。

在 `$HOME/.config/devin/mcp_config.json` 的 `mcpServers` 中增量合并以下条目。项目配置也可放在 `.devin/mcp_config.json`；个人覆盖使用 `.devin/mcp_config.local.json`。先备份，只新增或更新 `baton`，保留现有服务器和模型设置。

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
      "disabled": false
    }
  }
}
```

上游在 `v3000.3` 将 MCP 配置从主 `config.json` 拆到 `mcp_config.json`；旧版使用主配置中的 `mcpServers`。先确认所装版本及有效配置位置，不要同时维护两份相同服务器条目。

运行 `devin mcp list`，重启会话后用 `/mcp` 查看 `baton`，按当前权限策略允许 `fetch_inbox` 检查认证。完整任务链按[公共检查步骤](codex.md#检查连接)验证；无需改变全局审批模式。

SSH 仅连接适配器主机，token 文件留在该主机；适配器通过 TLS 向 gateway 发送认证信息。自定义 `BATON_CA_FILE` 或 `BATON_PYTHON` 也在该主机的适配器命令中配置。

依据：[Devin CLI 官方 MCP 配置](https://docs.devin.ai/cli/extensibility/mcp/configuration)。
