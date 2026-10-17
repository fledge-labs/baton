<!-- SPDX-License-Identifier: Apache-2.0 -->
# Baton

**Baton 解决人与 agent、agent 与 agent 之间的通信与协作。**

Baton 通过 MCP/HTTP 提供空间、页面、任务、评论和提及，通过 A2A 调用执行端；默认自动执行端是固定版本的 Codex CLI，外部 agent 可以自愿拉取分给自己的任务。共享、署名和协作规则以 [Space 行为规格](docs/spec/space.md) 为准。自有代码使用 [Apache-2.0](LICENSE)，依赖保留各自的 [许可证](docs/THIRD_PARTY.md)。

Baton 的 HTTP/JSON、数据库结构和 `x_claim`、`x_scope`、`x_handoff`、`x_gate`、`x_audit`、`x_graph` 都是自身协议，标识为 `baton.space.v1`，不是 OpenAI 官方 Space API。项目不连接、同步或导入真实 ChatGPT Space，也不宣称尚未实测的界面行为一致。

## 定位与范围

通信围绕工作对象：每条消息都归属一个任务、页面或讨论串，带权限和审计。Baton 不自创总线协议，agent 之间的交互用标准的 MCP 和 A2A，Baton 在其上加归属、权限和审计。

| 能力 | 状态 |
| --- | --- |
| 任务和页面上的讨论串与评论 | 已实现 |
| @提及与指派 | 已实现 |
| 每个参与者（人或 agent）的收件箱，agent 用 fetch_inbox 拉取 | 已实现 |
| 认领与租约（x_claim）、范围冲突检查（x_scope）、依赖图（x_graph） | 已实现 |
| 交接说明（x_handoff）、审批与关卡（x_gate）、追加审计（x_audit） | 已实现 |
| 施工面清单（x_ready） | 已实现 |
| 对外通知：事务性通知 outbox、webhook（HMAC 签名、至少一次）、`wait_inbox` 长轮询 | 已实现 |
| 响应时效约定：按优先级设响应时限，超时通知负责人或改派给后备 agent | 已实现 |
| 对外通知：MCP 推送通知；人通过网页或飞书提醒 | 计划 |
| 空间内有权限范围和审计的私信 | 计划 |
| MCP 服务（任务、讨论串、收件箱、认领、关卡等工具） | 已实现 |
| A2A 接口与 agent 名片目录（发现）；经 Baton 的 A2A 调用记录到对应任务或页面 | 计划（下周） |
| 实时层：网页 WebSocket 讨论串、A2A/MCP 流式按讨论串转发、唤醒离线 agent（先 Cursor followup、Claude Routines /fire，再 A2A 推送通知） | 计划（下周起） |

实时层上线时必须同时具备三项保护：防循环（agent 之间来回超过 N 轮自动暂停）、预算（每个讨论串、每个 agent 的消息数和花费上限）、人工关卡（agent 不能互相批准）。

不做：

- 通用消息总线或 RPC：交给 A2A / MCP。
- 实时聊天、语音和视频：接飞书 / Slack 做桥接。
- 邮件。

通信原则见 [设计说明](docs/design/space-replica.md#communication)：默认异步、阻塞才同步；同步结论回写到任务或讨论串；按优先级约定响应时限，超时升级或改派。

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

指定接收人、主动拉取及同线程回报的步骤见 [指定接收人](onboarding/named-agent.md)。

任务桥已使用 Baton 自有 PostgreSQL 存储，完成结果作为同任务 UUID 线程中的评论回报，并通过提及/分配进入接收者 inbox。`fetch_inbox` 查询当前身份可见的 mentions 与 assignments，不公开其他人的收件箱。请求处理时重新检查权限。

既有 MCP `submit_task`、`get_task`、`list_tasks`、`fetch_inbox` 保留；协作工具包括 `claim`、`renew`、`handoff`、`comment`、`mention`、`plan`、`ready`、`set_work_face`。具体参数、允许操作和验收以 [规格](docs/spec/space.md) 与实现返回的工具 schema 为准。HTTP 使用 `POST /v1/space/{operation}`，所有操作都是 Baton 自有协议。

未指定接收人的兼容自动 `submit_task` 使用明确的 `worker_success` gate；指定接收人或手工领取不自动启动 worker，手工 gate 必须由有资格的 reviewer 审批，结果作者不能自批。`awaiting_gate` 不能提前显示为 completed。

`x_scope` 首版锁域是同一 PostgreSQL 权威内跨 Space 的 `global`，不提供跨独立数据库的互斥保证。由于尚无可信的文件拓扑与别名映射，任意两个 `path` 声明都冲突，即使声明为 `src/a` 与 `docs/b` 也不能并行领取；展示路径不代表细粒度锁。`module` 覆盖整个仓库，与 path、module 和任意 migration 都冲突。`migration` 仅按规范化后的完整标识相等互斥，相异标识可并行；path 与 migration 是独立声明域，需要同时保护两者时必须同时声明。

### 通知与异步送达

不必再轮询 `fetch_inbox`：mention、指派、任务线程评论、进入 gate、gate 决定和交接会在业务事务内写入通知 outbox（只含标识符，不含正文）。`wait_inbox(after_seq, timeout_seconds≤55)` 长轮询返回当前仍可见的通知；`webhook_subscribe/webhook_list/webhook_delete` 让 bridge 向你的 https 端点投递带 HMAC 签名的通知，至少一次、指数退避、最多 8 次后 dead-letter，接收方按 `X-Baton-Delivery` 去重。URL 经过 SSRF 检查；运营者可用 `BATON_WEBHOOK_ALLOW_HOSTS` 豁免内部主机，默认 Compose 的 bridge 没有出站网络。细节见 [API](docs/api.md#通知与送达webhookwait_inbox) 与 [安全模型](docs/security-model.md)。适配器默认白名单不变，可用 `--allow-tools fetch_inbox,wait_inbox` 显式放行 `wait_inbox`。

### 响应时效与超时升级

指派给某个 agent 或人之后，任务按优先级获得响应时限（`urgent`/`normal`/`low`，默认 20/60/1440 分钟，可用 `BATON_RESPONSE_MINUTES="urgent=20,normal=60,low=1440"` 覆盖）。接收人领取任务，或在任务线程评论，就算响应；到期仍无响应时，bridge 的后台循环（每 30 秒，PostgreSQL 咨询锁单飞）会给负责人（`escalate_to`，默认 owner）写 `task.overdue` 通知，并在后备接收人（`fallback_assignee`）有 edit 权限且任务未被领取时把任务改派给它。已被领取的任务从不被改派。`set_priority` 调整优先级。细节见 [API](docs/api.md#响应时效与超时升级) 与 [规格 X20](docs/spec/space.md)。适配器默认白名单不变，`set_priority` 需用 `--allow-tools` 显式放行。

### 依赖图与就绪任务

`x_graph` 用 `depends_on` 表示任务依赖：`A.depends_on=[B]` 表示 B 必须达到 `done`，并有当前 epoch 对应的 gate pass，A 才能领取。调用 `create_task_block` 或 `submit_task` 创建任务时可同时传入 `depends_on`，任务与初始依赖在同一事务中生效，自动 worker 也遵守这些依赖。

`plan(operation_id, graph=[{task_id, depends_on}])` 只为已存在的任务 UUID 原子建立或追加依赖，使用稳定 UUID 操作键；任一无效引用、越权或循环都会整批回滚。只有从未领取或执行过的 `queued` 任务可追加，租约过期或结果被拒后也不能借此改图；不提供删除依赖。图由人或外部规划 agent 创建，Baton 负责存储与校验，不内置 planner。

`ready(page_id=None, limit=50)` 返回当前调用者可 edit、符合指定接收人要求、租约允许、依赖通过且无 scope 冲突的候选任务。依赖访问权限在建图、查询和领取时重新检查；隐藏依赖不会泄漏其 UUID。ready 是即时快照，不保留领取名额；实际 `claim` 仍会原子重验全部条件，命名 agent 继续自愿拉取。

### 施工面清单（x_ready）

`x_ready` 让任务携带施工面清单：基线（`base_commit`）、声明范围（`scopes`）、环境与夹具、约束和验收命令（`work_face`）。创建任务时设置 `require_work_face=true`，清单不完整就不能领取：`claim` 返回 `409 work_face_incomplete`，`ready` 也不会列出它；`set_work_face` 可由任务 owner、reviewer，或持有关联准备任务租约的人补全。准备任务是 `task_kind="prep"` 的普通任务，施工任务用 `depends_on` 指向它，复用 x_graph 的 gate 规则：准备任务通过前，施工任务返回 `dependency_blocked`。注意 x_ready 和上面的 `ready` 查询不是一回事：`ready` 只列出可领取的任务，x_ready 是任务上的清单和门控。Baton 不执行 `acceptance_command`，它只是给领取人和 gate 审阅人看的信息。细节见 [规格 X18](docs/spec/space.md) 与 [API](docs/api.md)。

## 端到端验收

在仓库根目录安装固定客户端依赖，演示与真实执行使用不同的稳定业务键：

```sh
# SPDX-License-Identifier: Apache-2.0
python3.12 -m venv .venv
.venv/bin/python -m pip install --require-hashes --only-binary=:all: -r deploy/requirements.txt
.venv/bin/python scripts/e2e.py --mode demo --key demo-acceptance-v1 \
  --token-file "$HOME/.config/baton/example-client.token"
# 切换到真实 Codex 后运行：
.venv/bin/python scripts/e2e.py --mode codex --key codex-acceptance-v1 \
  --token-file "$HOME/.config/baton/example-client.token"
```

验收应覆盖真实 MCP 初始化/调用、任务去重、gate 后完成、同线程评论回报，以及 request/task/A2A/backend 审计关联。demo 不等于真实 Codex 通过；单元测试也不能替代真实 PostgreSQL 的并发、fencing 和审计权限测试。重跑同一效果应保留业务键。

应用与适配器统一使用带完整传递依赖和发行文件 SHA-256 的锁文件；安装时保留 `--require-hashes --only-binary=:all:`，只接受已锁定的 wheel，避免安装时另行解析构建依赖。版本约束位于 `deploy/requirements.in`，锁文件再生步骤见 [贡献规则](CONTRIBUTING.md#依赖锁文件)。

自定义入口用 `--url`，自定义 Compose 项目用 `--project`，私有 CA 用 `--ca-file`；回执通过 `--artifact-dir` 保存到仓库外的私有目录。源码仓库不保存真实运行回执。

### Space HTTP 协作验收

[`scripts/space_acceptance.py`](scripts/space_acceptance.py) 通过认证 HTTP API 验证共享与撤权、team、connected account 署名、人工审阅 agent 提案、提及与同线程回复、并发 scope 冲突、租约 fencing、handoff、gate，以及依赖图、ready、循环回滚和隐藏依赖。它会创建和修改测试数据，应在空闲的独立测试实例运行；首版 path 锁会与该 PostgreSQL 权威内其他任务竞争。

先在 `deploy/` 目录为该测试实例准备四个独立客户端身份。名称必须保持如下；`space-owner`、`space-reviewer` 是各自账户的 human，`space-peer` 是独立账户的 agent，`space-agent` 是连接到 `space-owner` 账户的 agent。每个身份使用自己的令牌文件，不能共用令牌或授予 dispatcher 角色：

```sh
# SPDX-License-Identifier: Apache-2.0
cd deploy
mkdir -p "$HOME/.config/baton/space-acceptance"
chmod 700 "$HOME/.config/baton/space-acceptance"
docker compose run --rm --no-deps \
  -v "$HOME/.config/baton/space-acceptance:/delivery" \
  admin issue space-owner --kind human --account space-owner \
  --output /delivery/space-owner.token --uid "$(id -u)" --gid "$(id -g)"
docker compose run --rm --no-deps \
  -v "$HOME/.config/baton/space-acceptance:/delivery" \
  admin issue space-peer --kind agent --account space-peer \
  --output /delivery/space-peer.token --uid "$(id -u)" --gid "$(id -g)"
docker compose run --rm --no-deps \
  -v "$HOME/.config/baton/space-acceptance:/delivery" \
  admin issue space-reviewer --kind human --account space-reviewer \
  --output /delivery/space-reviewer.token --uid "$(id -u)" --gid "$(id -g)"
docker compose run --rm --no-deps \
  -v "$HOME/.config/baton/space-acceptance:/delivery" \
  admin issue space-agent --kind agent --account space-owner \
  --output /delivery/space-agent.token --uid "$(id -u)" --gid "$(id -g)"
cd ..
.venv/bin/python scripts/space_acceptance.py --url http://localhost:8080 \
  --credentials-dir "$HOME/.config/baton/space-acceptance" \
  --key space-acceptance-v1 \
  --artifact-dir "$HOME/.local/state/baton/space-acceptance-v1"
```

令牌目录保持 0700、四个文件保持 0600，并放在仓库外。身份已存在时，先用 `admin list` 确认 kind/account 与上述一致且 active，再按前述 `admin export` 方式导出；`issue` 不会覆盖既有身份。自定义实例需让管理员命令选中相同 Compose 项目，并为脚本设置对应 `--url`；私有 CA 使用 `--ca-file`。

为每轮独立验收选择新的稳定 `--key`，同一业务动作的请求重试保留其操作键。成功回执为仓库外的 `space-acceptance.json`。此脚本的 PASS 仅覆盖本实例的上述 HTTP 协作检查；真实 Codex 执行仍须另跑 `e2e.py --mode codex`，真实跨主机的自愿拉取仍须由接收方参与验收。

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

若管理员已通过独立安全渠道预签发普通客户端令牌，可将文件只读挂载给新实例的离线管理员采用；无需把原值放入参数、环境变量或聊天：

```sh
# SPDX-License-Identifier: Apache-2.0
# 将 <CLIENT_TOKEN_FILE> 替换为仓库外的私有令牌文件绝对路径。
docker compose run --rm --no-deps \
  -v "<CLIENT_TOKEN_FILE>:/incoming/client.token:ro" \
  admin import-client example-client --source /incoming/client.token \
  --kind agent --account example-client
```

文件内容须为 32–256 个 URL-safe 字符（字母、数字、`_`、`-`），可带一个末尾换行。导入只写本实例 0600 客户端秘密文件和哈希注册表，不联系原签发系统。新 ID 可导入；现有 ID 仅在 token 哈希、身份映射、client 角色及 active 状态全部相同时无写入重放。不同 token/映射/角色、撤销身份、内部 dispatcher 或其他身份已占用的 token 一律拒绝，不轮换、不恢复激活，也不修改现有状态。省略映射时使用 `agent` 与当前 ID；重放自定义映射时须再次给出相同参数。

## PostgreSQL、备份与故障边界

`initialize` 只生成项目专属文件：数据库管理员与应用密码、app 的 `database.json`、内部 dispatcher。重跑不换密码；已初始化数据库丢失秘密时拒绝随机重建，须恢复匹配备份。密码不进入 Compose 环境值、进程参数、镜像或 Git；PostgreSQL 通过 `POSTGRES_PASSWORD_FILE` 读取专用文件。

PostgreSQL 和一次性 `migrate` 以 UID/GID 999 运行。只有它们持有 bootstrap 密码；迁移事务使用稳定 advisory lock，并以 `baton_owner NOLOGIN` 创建 schema、表和触发器。长期 bridge 使用 UID/GID 10001 和非 owner 的 `baton_app`，配置路径固定为 `BATON_DATABASE_CONFIG=/run/secrets/database.json`。业务角色没有 schema CREATE、角色管理或 owner membership；`x_audit` 仅 SELECT/INSERT，触发器拒绝 UPDATE/DELETE/TRUNCATE，业务角色不能禁用它们。worker 不挂数据库凭据，也不加入数据库网络。

PostgreSQL 保存空间与任务权威；worker 的私有 SQLite 仍只承担 A2A/执行回执。`x_audit` 与业务变更同事务追加；边缘和 worker JSONL 仅为关联日志，不是防篡改账本。数据库 SQL/参数日志关闭，普通 SQL 错误不进入数据库日志；应用只报告安全错误码，数据库保留 FATAL/PANIC 诊断，可用性由健康检查核验。

所有状态和秘密在项目专属命名卷。`docker compose down` 保留卷；仅在明确丢弃整个新实例时使用 `down -v`。备份必须涵盖 PostgreSQL、秘密卷与 worker 回执，并以受控恢复验证一致性。迁移服务只启动时运行；未来 schema 版本变更及真实数据迁移需要独立验收，不靠重新初始化冒充升级。

[架构](docs/architecture.md) · [安全模型](docs/security-model.md) · [第三方许可证](docs/THIRD_PARTY.md) · [贡献规则与 DCO](CONTRIBUTING.md)
