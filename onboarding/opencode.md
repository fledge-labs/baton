<!-- SPDX-License-Identifier: Apache-2.0 -->

# OpenCode 接入 Baton

先完成[适配器公共准备](codex.md#准备适配器)，替换 `<HOST>`、`<agent-id>`、`<TOKEN_FILE>` 和示例 HTTPS 地址。这是配置指南，不是本仓库的原生客户端验收声明。

在项目 `opencode.json`，或 `$HOME/.config/opencode/opencode.json` 中合并以下配置。先备份，只增加或更新 `mcp.baton`，保留其他服务器、模型和权限设置。OpenCode 的 `command` 是数组；本地启动的 SSH 进程使用 `type: "local"`。

配置示例：`SPDX-License-Identifier: Apache-2.0`（JSON 不支持注释）。

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "baton": {
      "type": "local",
      "command": [
        "ssh", "-T", "-o", "BatchMode=yes", "<HOST>",
        "BATON_URL=\"https://example.com/mcp\" exec \"$HOME/baton/adapters/launch_mcp.sh\" \"<agent-id>\" \"<TOKEN_FILE>\""
      ],
      "enabled": true,
      "timeout": 30000
    }
  }
}
```

运行 `opencode mcp list`，重新启动会话，明确要求使用 `baton` 的 `fetch_inbox` 工具检查认证。完整任务链按[公共检查步骤](codex.md#检查连接)验证。沿用当前获准使用的模型与工具审批策略；配置中的 `timeout` 只覆盖 MCP 初始化/发现等待，不保证任务已完成。

SSH 仅连接适配器主机，token 文件留在该主机；适配器通过 TLS 向 gateway 发送认证信息。OpenCode 的本地 `environment` 不会自动转发到远程 SSH 主机，自定义 `BATON_PYTHON` 或 `BATON_CA_FILE` 应写入远程适配器命令。

依据：[OpenCode 官方 MCP 文档](https://opencode.ai/docs/mcp-servers/)、[配置位置与合并规则](https://opencode.ai/docs/config/)。
