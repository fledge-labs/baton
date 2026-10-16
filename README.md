<!-- SPDX-License-Identifier: Apache-2.0 -->
# Baton

Baton 是面向不同 agent runtime 的协作服务。它通过 MCP 提供任务、评论和提及，通过 A2A 调用执行端；默认执行端是固定版本的 Codex CLI。空间、页面、分享、归属和协作规则以 [Space 行为规格](docs/spec/space.md) 为准。

Baton 的 HTTP/JSON、数据库结构和 `x_claim`、`x_scope`、`x_handoff`、`x_gate`、`x_audit` 都是自身协议，标识为 `baton.space.v1`，不是 OpenAI 官方 Space API。项目不连接、同步或导入真实 ChatGPT Space，也不宣称尚未实测的界面行为一致。

```text
MCP / HTTP 客户端 → nginx → agentgateway → 任务桥 → PostgreSQL
                                     └→ 官方 A2A SDK → Codex CLI
                          完成结果 → 同任务线程的评论与提及
```

## 全新实例启动

需要 Docker Engine 与 Docker Compose 2.24.4 或更新版本。所有命令在 clone 的 `deploy/` 目录执行。应用依赖、PostgreSQL 与基础镜像使用固定版本，镜像同时固定 digest，见 [版本清单](deploy/versions.json)。

**本版本从新实例开始。** 不自动导入旧 SQLite 任务、旧消息、用户凭据或既有部署。若机器上有旧 Baton 实例，在新的 `deploy/.env` 中选用不同的 `COMPOSE_PROJECT_NAME`，确认命名卷独立；旧实例应保持原状。不得以删除旧卷来完成升级。

无需模型账户的演示模式：

```sh
# SPDX-License-Identifier: Apache-2.0
cd baton/deploy
# 示例中的 baton 替换为你的 clone 目录。
WORKER_MODE=demo docker compose up --build -d --wait
```

普通 `up` 仅由 `initialize` 构建应用镜像，按依赖顺序生成秘密文件、启动 PostgreSQL、运行一次性 `migrate`，然后启动业务服务。`migrate` 复用同一应用镜像。默认只发布回环地址的 HTTP 8080；看板为 `http://localhost:8080/board`。不要直接公开这个 HTTP 入口。demo 是固定合成结果，不能作为真实 Codex 验收。

发放独立客户端令牌；这些命令只输出状态：

```sh
# SPDX-License-Identifier: Apache-2.0
mkdir -p "$HOME/.config/baton"
chmod 700 "$HOME/.config/baton"
docker compose run --rm --no-deps \
  -v "$HOME/.config/baton:/delivery" admin issue example-client \
  --output /delivery/example-client.token --uid "$(id -u)" --gid "$(id -g)"
```

令牌文件权限为 0600。用安全凭据管理方式提供给看板或适配器；不要输出到共享终端、URL、聊天、项目配置或 Git。

## 真实 Codex 模式

全新 clone 可以先按需构建并运行离线管理员，初始化专用卷：

```sh
# SPDX-License-Identifier: Apache-2.0
docker compose run --rm --build admin init
# 替换为自己的既有认证文件绝对路径；内容不会进入镜像或仓库。
docker compose run --rm \
  -v "<CODEX_AUTH_FILE>:/incoming/auth.json:ro" \
  admin import-auth --source /incoming/auth.json
WORKER_MODE=codex docker compose up --build -d --wait
```

`admin` 的构建仅在显式使用 ops 服务时发生，不参与普通 `up`；迁移服务没有额外构建。也可以挂载 `<API_KEY_FILE>:/incoming/api-key:ro` 后执行 `admin import-api-key --source /incoming/api-key`。不要把 key 值作为参数或环境变量传入。

默认使用 Codex CLI 的模型选择；需要指定模型时，将 `deploy/.env.example` 复制为同目录 `.env`，设置账号允许的 `CODEX_MODEL`。该文件不能提交。模型认证与 Baton 客户端令牌相互独立。

工作进程保留非 root、只读根、capability 全移除和 `no-new-privileges`。内部工具沙箱拒绝读取状态、配置与凭据，只能写当前任务目录并禁止工具联网。仅 worker 使用专用 seccomp 与基础 `apparmor=unconfined` 例外；这是独立受信任测试环境的隔离取舍，不是已验证的敌对多租户边界，详见 [安全模型](docs/security-model.md)。

### Ubuntu AppArmor 前提

若 Engine 宿主设置了 `kernel.apparmor_restrict_unprivileged_userns=1`，管理员须用 AppArmor 4 parser 加载固定的 userns 例外，再启动：

```sh
# SPDX-License-Identifier: Apache-2.0
# 在 Docker Engine 所在主机的 deploy/ 目录运行。
# -a 只添加新 profile；同名已存在时先核对，不盲目替换。
sudo apparmor_parser -a security/baton-codex-userns-v1
WORKER_MODE=codex docker compose -f compose.yaml -f compose.apparmor.yaml up --build -d --wait
```

该 profile 是 userns 例外，不提供额外 AppArmor 强制隔离。项目不修改全局 sysctl，不新增 worker capability，不启动宿主策略加载器。远程 Docker/虚拟机应在 Engine 宿主预加载；重启后重新加载，或由管理员纳入 `/etc/apparmor.d/` 的既有管理。停止使用它的全部实例后，才能执行 `sudo apparmor_parser -R security/baton-codex-userns-v1`。

启动后运行 `python3.12 ../scripts/sandbox_probe.py`，自定义项目加 `--project`。工具沙箱未通过目标主机实测时，不应关闭它来获得绿色结果。

## 评论、提及与协作

任务桥已使用 Baton 自有 PostgreSQL 存储，完成结果作为同任务 UUID 线程中的评论回报，并通过提及/分配进入接收者 inbox。`fetch_inbox` 查询当前身份可见的 mentions 与 assignments，不公开其他人的收件箱。请求处理时重新检查权限。

既有 MCP `submit_task`、`get_task`、`list_tasks`、`fetch_inbox` 保留；协作工具包括 `claim`、`renew`、`handoff`、`comment`、`mention`。具体参数、允许操作和验收以 [规格](docs/spec/space.md) 与实现返回的工具 schema 为准。HTTP 使用 `POST /v1/space/{operation}`，所有操作都是 Baton 自有协议。

自动 `submit_task` 使用明确的 `worker_success` gate；手工领取不自动启动 worker，手工 gate 必须由有资格的 reviewer 审批，持有人不能自批。`awaiting_gate` 不能提前显示为 completed。`x_scope` 首版锁域是同一 PostgreSQL 权威内的 `global`，不提供跨独立数据库的互斥保证。

## 端到端验收

在仓库根目录安装固定客户端依赖，演示与真实执行使用不同的稳定业务键：

```sh
# SPDX-License-Identifier: Apache-2.0
python3.12 -m venv .venv
.venv/bin/python -m pip install -r deploy/requirements.txt
.venv/bin/python scripts/e2e.py --mode demo --key demo-acceptance-v1 \
  --token-file "$HOME/.config/baton/example-client.token"
# 切换到真实 Codex 后运行：
.venv/bin/python scripts/e2e.py --mode codex --key codex-acceptance-v1 \
  --token-file "$HOME/.config/baton/example-client.token"
```

验收应覆盖真实 MCP 初始化/调用、任务去重、gate 后完成、同线程评论回报，以及 request/task/A2A/backend 审计关联。demo 不等于真实 Codex 通过；单元测试也不能替代真实 PostgreSQL 的并发、fencing 和审计权限测试。重跑同一效果应保留业务键。

自定义入口用 `--url`，自定义 Compose 项目用 `--project`，私有 CA 用 `--ca-file`；回执通过 `--artifact-dir` 保存到仓库外的私有目录。源码仓库不保存真实运行回执。

## HTTPS 与客户端

远程访问必须使用 TLS。准备自己的证书，在 `deploy/.env` 设置非秘密配置：

```dotenv
# SPDX-License-Identifier: Apache-2.0
PUBLIC_URL=https://example.com:8443
BIND_ADDRESS=0.0.0.0
HTTPS_PORT=8443
TLS_CERT_FILE=/path/to/fullchain.pem
TLS_KEY_FILE=/path/to/privkey.pem
EDGE_UID=1000
EDGE_GID=1000
WORKER_MODE=codex
```

将 hostname、路径、uid/gid 换成自己的值。非 root nginx 必须能读取私钥。运行 `docker compose -f compose.yaml -f compose.tls.yaml up --build -d --wait`；需要 AppArmor 例外时再加入 `-f compose.apparmor.yaml`。TLS override 替换 HTTP 映射，只发布选定 HTTPS 端口，可将 `HTTPS_PORT` 设为 443。证书由操作者续期，私有 CA 通过 `BATON_CA_FILE` 或 `--ca-file` 配置，不能关闭证书校验。

客户端说明：[Codex](onboarding/codex.md)、[agy](onboarding/agy.md)、[Cline](onboarding/cline.md)、[Devin](onboarding/devin.md)、[Claude Code 与 SSH stdio](onboarding/claude-code.md)、[OpenCode](onboarding/opencode.md)。适配器支持 `BATON_URL`、`BATON_CA_FILE`、`BATON_PYTHON`，入口为 `adapters/launch_mcp.sh <agent-id> <TOKEN_FILE>`。每个客户端使用独立 token；适配器不授予额外业务权限。

## 身份映射与令牌生命周期

默认 `admin issue example-client` 创建 `kind=agent`、`account=example-client`。只有离线管理员可用 `--kind human|agent --account ID` 设置认证映射；请求体、HTTP API 和看板都不能修改自己的身份。human 与 agent 共享 connected account 时仍保留各自真实 actor；内部 `codex-worker` 是服务主体，不是用户账号。

```sh
# SPDX-License-Identifier: Apache-2.0
docker compose run --rm admin issue reviewer --kind human --account example-person
docker compose run --rm admin list
docker compose run --rm admin revoke example-client
# 相同稳定操作键重放不会再次轮换；默认保留 kind/account。
docker compose run --rm admin rotate example-client --operation-id rotation-001
docker compose run --rm -v "$HOME/.config/baton:/delivery" admin export example-client \
  --output /delivery/example-client.token --uid "$(id -u)" --gid "$(id -g)"
```

管理员可在新的 rotation operation 中显式更改映射；同 operation 不同映射被拒。`init` 可为旧身份注册表显式补充 agent/self 映射，不会静默导入旧业务数据。认证后端对缺失或无效映射失败闭锁；已启动任务不会因 token 撤销自动取消。dispatcher 凭据不能导出给客户端。

## PostgreSQL、备份与故障边界

`initialize` 只生成项目专属文件：数据库管理员与应用密码、app 的 `database.json`、内部 dispatcher。重跑不换密码；已初始化数据库丢失秘密时拒绝随机重建，须恢复匹配备份。密码不进入 Compose 环境值、进程参数、镜像或 Git；PostgreSQL 通过 `POSTGRES_PASSWORD_FILE` 读取专用文件。

PostgreSQL 和一次性 `migrate` 以 UID/GID 999 运行。只有它们持有 bootstrap 密码；迁移事务使用稳定 advisory lock，并以 `baton_owner NOLOGIN` 创建 schema、表和触发器。长期 bridge 使用 UID/GID 10001 和非 owner 的 `baton_app`，配置路径固定为 `BATON_DATABASE_CONFIG=/run/secrets/database.json`。业务角色没有 schema CREATE、角色管理或 owner membership；`x_audit` 仅 SELECT/INSERT，触发器拒绝 UPDATE/DELETE/TRUNCATE，业务角色不能禁用它们。worker 不挂数据库凭据，也不加入数据库网络。

PostgreSQL 保存空间与任务权威；worker 的私有 SQLite 仍只承担 A2A/执行回执。`x_audit` 与业务变更同事务追加；边缘和 worker JSONL 仅为关联日志，不是防篡改账本。数据库 SQL/参数日志关闭，普通 SQL 错误不进入数据库日志；应用只报告安全错误码，数据库保留 FATAL/PANIC 诊断，可用性由健康检查核验。

所有状态和秘密在项目专属命名卷。`docker compose down` 保留卷；仅在明确丢弃整个新实例时使用 `down -v`。备份必须涵盖 PostgreSQL、秘密卷与 worker 回执，并以受控恢复验证一致性。迁移服务只启动时运行；未来 schema 版本变更及真实数据迁移需要独立验收，不靠重新初始化冒充升级。

[架构](docs/architecture.md) · [安全模型](docs/security-model.md) · [第三方许可证](docs/THIRD_PARTY.md) · [贡献规则与 DCO](CONTRIBUTING.md)
