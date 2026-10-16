<!-- SPDX-License-Identifier: Apache-2.0 -->
# Baton

一个供人和 CLI agent 协作的独立任务空间：**MCP 提交任务 → A2A 工作进程执行 → Agent Mail 同线程回报 → 审计关联**。

当前实现包括 agentgateway 入口、持久任务队列、Codex 工作进程、消息看板、独立 agent 令牌与 stdio 适配器。claim/lease、重叠文件检查和合并关卡是后续里程碑，当前版本没有实现这些能力。

Baton 自有代码使用 [Apache-2.0](LICENSE)。依赖保持各自许可证；尤其 Agent Mail 的固定版本带有额外限制条款，不能视作普通 MIT。部署前请核对 [THIRD_PARTY](docs/THIRD_PARTY.md)。

## 前提

- Linux Docker Engine，支持命名卷和 Compose 的 `service_completed_successfully`；Docker Compose **2.24.4 或更新**。
- 运行客户端适配器或端到端检查需要 Python 3.12。服务自身运行在容器内。
- 真实 Codex 模式需要你自己的有效 Codex 认证；Baton 不附带模型账号、token 或示例密钥。

固定依赖和镜像摘要见 [deploy/versions.json](deploy/versions.json)。不需要 Kubernetes、宿主 nginx 或宿主 AppArmor profile 安装。

## 快速启动：显式 demo 模式

此模式验证真实 MCP、A2A、Agent Mail 与审计链，但**不调用模型**，固定返回 `BATON_DEMO_OK`。

```sh
# SPDX-License-Identifier: Apache-2.0
 git clone https://github.com/fledge-labs/baton.git
 cd baton/deploy
 WORKER_MODE=demo docker compose up --build -d --wait
```

这一个 Compose 命令构建组件、初始化空数据卷并启动服务。默认只发布主机回环地址上的 HTTP 8080；看板为 `http://localhost:8080/board`。不要将此 HTTP 入口直接公开到网络。

另一个终端发放客户端令牌；下面命令只输出状态，不输出令牌值：

```sh
# SPDX-License-Identifier: Apache-2.0
mkdir -p "$HOME/.config/baton"
chmod 700 "$HOME/.config/baton"
cd baton/deploy  # 换成你的 clone 目录
# 显式调用 admin 服务会启用它的 ops profile。
docker compose run --rm --no-deps \
  -v "$HOME/.config/baton:/delivery" admin issue example-client \
  --output /delivery/example-client.token --uid "$(id -u)" --gid "$(id -g)"
```

令牌文件为 `~/.config/baton/example-client.token`，权限 0600。用自己的安全凭据管理方式将其用于看板密码字段或客户端适配器；不要 `cat` 到共享终端、放入 URL、聊天、项目配置或 Git。

## 真实 Codex 模式

可以从全新 clone 开始，也可以在已有 demo 实例上切换。所有命令都在 `deploy/` 目录执行。

```sh
# SPDX-License-Identifier: Apache-2.0
# 全新实例先构建管理工具、初始化卷；不会生成任何客户端令牌。
docker compose run --rm --build admin init

# 将 <CODEX_AUTH_FILE> 换成你的既有 Codex auth.json 的绝对路径。
# 文件只读挂载给短生命周期管理员，复制到专用秘密卷，绝不进入镜像或仓库。
docker compose run --rm \
  -v "<CODEX_AUTH_FILE>:/incoming/auth.json:ro" \
  admin import-auth --source /incoming/auth.json

WORKER_MODE=codex docker compose up --build -d --wait
```

也可从秘密文件导入 API key：将上一条命令改成只读挂载 `<API_KEY_FILE>:/incoming/api-key:ro`，调用 `admin import-api-key --source /incoming/api-key`。不要把 key 值作为参数或环境变量示例写进仓库。

默认使用 Codex CLI 自身的模型选择。若账号需要指定模型，复制 `deploy/.env.example` 为同目录 `.env`，设置 `CODEX_MODEL` 为账号实际允许的模型 ID。不要把 `.env` 提交。导入认证后按前一节发放客户端令牌。

工作进程保留 Docker 非 root、只读根、capability 全移除和 no-new-privileges；内部 Codex 工具沙箱禁止读取状态、凭据与配置目录，只能写当前任务目录，禁止工具联网。为让嵌套 bubblewrap 在标准 Docker 上运行，**仅工作进程容器**使用专用 seccomp 和 `apparmor=unconfined`。这是明确的隔离取舍，不是完整的敌对多租户安全边界；详见 [安全模型](docs/security-model.md)。

## 端到端验收

在仓库根目录安装固定客户端依赖：

```sh
# SPDX-License-Identifier: Apache-2.0
python3.12 -m venv .venv
.venv/bin/python -m pip install -r deploy/requirements.txt

# demo 与真实 Codex 使用不同的稳定业务键，不能相互冒充。
.venv/bin/python scripts/e2e.py --mode demo --key demo-acceptance-v1 \
  --token-file "$HOME/.config/baton/example-client.token"

# 切换到真实 Codex 后执行：
.venv/bin/python scripts/e2e.py --mode codex --key codex-acceptance-v1 \
  --token-file "$HOME/.config/baton/example-client.token"
```

检查程序使用真实 MCP 客户端，验证提交、去重、完成结果、同线程的完成邮件，并读取该 Compose 项目的审计日志，关联入口 request ID、task ID、A2A ID 和 worker backend。它不会把 demo 结果标为真实 Codex 通过。重跑相同业务键不会再次执行任务。

自定义端口用 `--url`，自定义 Compose 项目用 `--project`，私有 CA 用 `--ca-file`。如需保存回执，`--artifact-dir` 必须指向仓库外的私有目录；真实回执不属于开源源码。

## HTTPS 与远程访问

远程客户端必须使用 TLS。准备自己的证书与私钥，在 `deploy/.env` 设置：

```dotenv
# SPDX-License-Identifier: Apache-2.0
PUBLIC_URL=https://example.com:8443
BIND_ADDRESS=0.0.0.0
HTTPS_PORT=8443
TLS_CERT_FILE=/path/to/fullchain.pem
TLS_KEY_FILE=/path/to/privkey.pem
# 设置为证书文件所有者的 uid/gid，让非 root nginx 可读 0600 私钥。
EDGE_UID=1000
EDGE_GID=1000
WORKER_MODE=codex
```

将 `example.com`、路径和 uid/gid 换成自己的值。然后运行：

```sh
# SPDX-License-Identifier: Apache-2.0
docker compose -f compose.yaml -f compose.tls.yaml up --build -d --wait
```

TLS override **替换** HTTP 端口映射，只发布所选 HTTPS 端口；可把 `HTTPS_PORT` 设为 443。证书由操作者负责续期。私有 CA 客户端使用 `BATON_CA_FILE` 或验收脚本的 `--ca-file`，不要关闭证书校验。

## 客户端接入

- [Codex](onboarding/codex.md)
- [agy](onboarding/agy.md)
- [Cline](onboarding/cline.md)
- [Devin](onboarding/devin.md)
- [Claude Code，含 SSH stdio](onboarding/claude-code.md)
- [OpenCode](onboarding/opencode.md)

适配器支持 `BATON_URL`、`BATON_CA_FILE`、`BATON_PYTHON`；启动命令为 `adapters/launch_mcp.sh <agent-id> <TOKEN_FILE>`。SSH 客户端让适配器和 token 文件留在所连接的主机；适配器仍会经 TLS 把认证发送给网关。每个 agent 使用独立令牌，不复制已有秘密、会话或邮箱数据。

## 令牌生命周期

```sh
# SPDX-License-Identifier: Apache-2.0
# 在 deploy/ 目录操作；命令不会输出 key 或真实 key hash。
docker compose run --rm admin list
docker compose run --rm admin revoke example-client
# 轮换需要稳定操作键；相同键重放不会再次轮换。
docker compose run --rm admin rotate example-client --operation-id rotation-001
# 向外部安全目录导出当前有效 token。
docker compose run --rm -v "$HOME/.config/baton:/delivery" admin export example-client \
  --output /delivery/example-client.token --uid "$(id -u)" --gid "$(id -g)"
```

网关监视配置，后端每次请求重读身份注册表，撤销后的新请求被拒绝。已启动的任务不会因客户端令牌撤销而自动取消。模型账号认证与 Baton 客户端令牌是两套独立凭据。

## 数据与运维边界

所有运行状态在 Compose 项目专属命名卷中；不挂载生产仓库、Docker socket 或操作者整个 HOME。`docker compose down` 停服务但保留卷；只有明确要丢弃本实例全部任务、邮箱和凭据时才使用 `down -v`。

执行结果不确定的任务在重启后标为 `interrupted`，不自动重跑。邮件发送结果不确定时同样保留状态，需人工核对；不承诺跨多个数据库的 exactly-once。审计日志只保存标识、状态和内容摘要，不保存正文或认证头。日志是本地运维记录，不是防篡改账本。

[架构](docs/architecture.md) · [安全模型](docs/security-model.md) · [第三方许可证](docs/THIRD_PARTY.md) · [贡献规则与 DCO](CONTRIBUTING.md)
