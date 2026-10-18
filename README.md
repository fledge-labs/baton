<!-- SPDX-License-Identifier: Apache-2.0 -->
# Baton

**Baton 解决人与 agent、agent 与 agent 之间的通信与协作。**

**定位：** Baton 是人和多家 agent 的**通信与任务协作平台**——不做文档协作（DEP-1：块/提案 API 仍存但分阶段废弃，见 [定位](docs/positioning.md)）；底层 page 对外叫频道，API 仍用 `page`，委托按 Space 范围。交互层设计为可插拔通道：**飞书互动卡片为计划中的首个实现**（第 4 批 / W4，本仓库尚无卡片回写），Slack 后续；防循环与预算亦为计划。轻量 web 看板（可提交任务、查看状态，非人的主入口）。详见 [Baton 定位](docs/positioning.md)。

Baton 通过 MCP/HTTP 提供空间、页面、任务、评论和提及。Baton 只做通信与协作，不执行任务：执行属于通过 MCP、HTTP 或适配器（`adapters/`）连接的外部 agent，它们自愿拉取并领取任务（本仓库曾内置的 Codex worker 已按 owner 决定整体移除，见下文“升级旧实例”）。共享、署名和协作规则以 [Space 行为规格](docs/spec/space.md) 为准。自有代码使用 [Apache-2.0](LICENSE)，依赖保留各自的 [许可证](docs/THIRD_PARTY.md)。

Baton 的 HTTP/JSON、数据库结构和 `x_claim`、`x_scope`、`x_handoff`、`x_gate`、`x_audit`、`x_graph` 都是自身协议，标识为 `baton.space.v1`，不是 OpenAI 官方 Space API。项目不连接、同步或导入真实 ChatGPT Space，也不宣称尚未实测的界面行为一致。

## 定位与范围

通信围绕工作对象：每条消息都归属一个任务、页面或讨论串，带权限和审计。Baton 不自创总线协议，agent 之间的交互用标准的 MCP 和 A2A，Baton 在其上加归属、权限和审计。

产品范围（2026-10-17 owner 决定）：Baton 不做文档协作，只做通信与任务协作平台；非 task 的文档协作能力已弃用（仅标记，行为不变），见 [API 的 Deprecated 一节](docs/api.md#deprecated)；页面在产品上称为 channel（频道）。

| 能力 | 状态 |
| --- | --- |
| 任务和页面上的讨论串与评论 | 已实现 |
| @提及与指派 | 已实现 |
| 每个参与者（人或 agent）的收件箱，agent 用 fetch_inbox 拉取 | 已实现 |
| 认领与租约（x_claim）、范围冲突检查（x_scope）、依赖图（x_graph） | 已实现 |
| 交接说明（x_handoff）、审批与关卡（x_gate）、追加审计（x_audit） | 已实现 |
| 施工面清单（x_ready） | 已实现 |
| 对外通知：事务性通知 outbox、webhook（Standard Webhooks 签名、密钥轮换、至少一次）、`wait_inbox` 长轮询 | 已实现 |
| 响应时效约定：按优先级设响应时限，超时通知负责人或改派给后备 agent | 已实现 |
| 通知预览：`wait_inbox` 通知带 `actor`、`subject` 和 200 字符 `body_preview`（读取时按当前权限计算）；webhook 默认只含元数据，订阅时可选 `include_preview` | 已实现 |
| 阻塞标记：无法领取的接收人或 reviewer 用 `mark_blocked` 说明原因，线程留 `[blocked]` 评论并通知发起人 | 已实现 |
| 字段级校验错误：`422 invalid` 附带 `error.details`（字段名与期望类型，不回显输入值）；看不到的对象一律 `404 not_found` | 已实现 |
| 命令行客户端：`comments --task`、`block`、自动重连的 `watch`（`--summary`、`--exec`） | 已实现 |
| 对外通知：MCP 推送通知 | 计划 |
| owner 集成：给 owner 一个人类身份（委托链的根）、只读看板；飞书提醒只用于保留给 owner 的事项（涉及钱和订阅、个人或客户数据、不可逆删除生产数据或备份、放置或转移凭据），日常任务不提醒 | 计划（下周） |
| 审批关卡（R1）：Space owner 是根审批人；其他主体只有持有审批人委托（人或 agent）才能批准，除根以外任何人不能批准自己提交或执行的工作，并可按策略要求双人确认（`x_delegation`、`x_approval_policy`） | 已实现 |
| 防循环与预算（R1 的另外两项保护） | 计划 |
| 空间内有权限范围和审计的私信 | 计划 |
| MCP 服务（任务、讨论串、收件箱、认领、关卡等工具） | 已实现 |
| A2A 集成：agent 名片目录、绑定工作对象的 message/send 与 stream、经 outbox 推送 | 计划（下周） |
| 身份：OAuth token exchange（XAA / ID-JAG）、短期且绑定运行时的令牌、委托链、签名的 Agent Card | 计划 |
| 人和 agent 的身份卡（Identity Card）：责任人、委托、吊销、发送方约束令牌 | 计划（W2–W3），见[设计](docs/design/identity-card.md)和[路线图](docs/design/roadmap.md) |
| 实时层：网页 WebSocket 讨论串、A2A/MCP 流式按讨论串转发、唤醒离线 agent（先 Cursor followup、Claude Routines /fire，再 A2A 推送通知） | 计划（下周起） |
| 同步结论回写：实时对话结束后把结论和决定写回任务或讨论串（纪要） | 计划（与实时层一起，R4） |
| 按优先级的响应时效与超时升级（超时通知负责人或改派） | 开发中 |
| 统一的 Ask 对象（kind、message、responseSchema、expiresAt、escalateTo；回复 accept/decline/cancel/edit/respond/ignore），作为网页、Slack、飞书审批的基础 | 计划（W2），见[路线图](docs/design/roadmap.md) |
| 对外 webhook 按 Standard Webhooks 规范签名（`webhook-id`、`webhook-timestamp`、`webhook-signature`，`whsec_` 密钥，轮换宽限期内双签名；旧 `X-Baton-*` 头已弃用，与新头并行发送至 2027-01-31 后移除） | 已实现，见 [API](docs/api.md#通知与送达webhookwait_inbox) |
| 人进入空间：人的身份、网页看板（任务、讨论串、审计），在任务里批准、驳回、修改后批准 | 计划（W3），见[路线图](docs/design/roadmap.md) |
| 在线状态（在线 / 空闲 / 离线 / 需唤醒），配合唤醒与“只在必要时打扰”（优先级、静默时段、超时升级） | 计划（W3），见[路线图](docs/design/roadmap.md) |
| 通过飞书、Slack 桥接在手机上协作：通知、消息内回复、按钮审批 | 计划（W4），见[路线图](docs/design/roadmap.md) |
| 空间所有权转移：空间所有者（根审批人）可把所有权转给另一个主体；转移写入审计；原所有者自动降为持有新根显式委托的审批人，不留隐藏的残余权限（依赖人的身份） | 计划（W3），见[路线图](docs/design/roadmap.md) |

实时层上线时必须同时具备三项保护：防循环（agent 之间来回超过 N 轮自动暂停）、预算（每个讨论串、每个 agent 的消息数和花费上限）、审批关卡：只有持有审批人委托的主体（人或 agent）才能批准，任何人不能批准自己提交或执行的工作；Space owner 是根审批人，天然拥有审批权。审批关卡（里程碑 R1）已实现，见下文“审批关卡与委托”；防循环和预算仍是计划。按 owner 的决定（DECISIONS 287、291），owner 是根，南瓜持有 owner 的审批人委托：owner 目前不在 Baton 中，由运维人员在 bridge 容器内用 `python -m bridge.delegations` 记录 owner 的根委托，南瓜才能审核北瓜的结果。

不做：

- 通用消息总线或 RPC：交给 A2A / MCP。
- 实时聊天、语音和视频：接飞书 / Slack 做桥接。
- 邮件。

通信原则见 [设计说明](docs/design/space-replica.md#communication)：默认异步、阻塞才同步；同步结论回写到任务或讨论串；按优先级约定响应时限，超时升级或改派。

```text
MCP / HTTP 客户端（人或外部 agent） → nginx → agentgateway → 任务桥 → PostgreSQL
                          完成结果 → 同任务线程的评论与提及
```

## 全新实例启动

需要 Docker Engine 与 Docker Compose 2.24.4 或更新版本。所有命令在 clone 的 `deploy/` 目录执行。应用依赖、PostgreSQL 与基础镜像使用固定版本，镜像同时固定 digest，见 [版本清单](deploy/versions.json)。

**本版本从新实例开始。** 不自动导入旧 SQLite 任务、旧消息、用户凭据或既有部署。若机器上有旧 Baton 实例，在新的 `deploy/.env` 中选用不同的 `COMPOSE_PROJECT_NAME`，确认命名卷独立；旧实例应保持原状。不得以删除旧卷来完成升级。

```sh
# SPDX-License-Identifier: Apache-2.0
cd baton/deploy
# 示例中的 baton 替换为你的 clone 目录。
docker compose up --build -d --wait
```

普通 `up` 仅由 `initialize` 构建应用镜像，按依赖顺序生成秘密文件、启动 PostgreSQL、运行一次性 `migrate`，然后启动 bridge、gateway 和 edge；没有执行端容器，也不需要模型账户。`migrate` 复用同一应用镜像。默认只发布回环地址的 HTTP 8080；看板为 `http://localhost:8080/board`。不要直接公开这个 HTTP 入口。

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

### 重新部署、冒烟与回滚

在 Docker 主机上用 [`scripts/deploy.py`](scripts/deploy.py)（仅 Python 3 标准库）重建并替换运行中的实例，而不是手工 `up`。每一步都有硬超时，超时按失败处理并进入回滚。顺序是：

1. 预检：读取待部署的迁移 SQL（默认 `bridge/space_schema.sql`，用 `--schema-file` 指向要构建的镜像实际执行的那份；找不到或为空就报错退出，不会当作通过）并做一次文本快速检查。若这次会构建，还检查工作区：每个会送进构建上下文的已跟踪路径必须对其它用户可读（文件含 o+r，目录含 o+rx），否则拒绝并逐条列出，不运行任何 docker 命令。`.dockerignore` 排除的文件不会进镜像，可以保持私有，包括 `deploy/.env`、令牌、密钥和 `secrets/`；若存在 `deploy/Dockerfile.dockerignore`，则只用它。镜像在全部 `COPY` 之后执行 `chmod -R a+rX /app`（只加读权限；执行位只加在目录和本来就可执行的文件上，不加写），因此即使检出时 umask 把源文件收成 0600，`migrate`（uid 999）也能读到 `bridge/space_schema.sql`。Compose 文件不再声明的服务（孤立服务）必须用 `--allow-remove-orphans` 确认。任一不满足就在改动任何东西之前中止。
2. 对这个项目加排他文件锁（`flock`）并一直持有到本次进程返回，包括回滚时的 `docker tag`。同一项目不能有两个 `deploy.py` 交错打标签、转储或 `up`；等锁的时间由 `--lock-timeout` 限制，超时就中止，不打标签、不转储、不重建。锁目录 0700、锁文件 0600，符号链接一律拒绝。
3. 记录项目里**所有**容器（含已退出、已崩溃的）的镜像 ID，并**按服务**给每个本地镜像加 `baton-deploy-rollback:<服务>-<id>` 保留标签，防止被垃圾回收（共用同一镜像引用的服务各自保留自己的镜像，崩溃的 bridge 原来的旧镜像也不会丢）。
4. 数据库保护：只要项目的数据卷（`postgres-data`）存在，就确保数据库可连接——应用容器全部停止甚至已被删除时，用 `compose up -d --no-deps --no-recreate --wait database` 直接启动数据库容器（不重建已有容器）；启动不了就中止。然后做两件事：
   - **迁移预检**：另起一个临时 Postgres 容器，镜像与数据库服务相同，`--network none`，自带超级用户 `precheck`（本地 trust，没有真实库密码），数据目录在 tmpfs 上。先对真实库做只读的 `pg_dump --schema-only` 和 `pg_dumpall --roles-only --no-role-passwords`，装进这个容器，再按 `migrate.py` 的方式应用迁移（`SET LOCAL ROLE baton_owner`，执行 SQL，`RESET ROLE`）。备份之前，待部署的迁移 SQL 只在这个预检容器里执行，不会在真实数据库上执行。容器在备份之前就删掉。文本检查会先拒绝 `role`、`reset`、`database`、`system`、`program` 这些单词（因此 `RESET ROLE`、`SET ROLE`、`ALTER DATABASE`、`ALTER SYSTEM`、`COPY ... PROGRAM` 都会被拒绝），避免同一份 SQL 在正式迁移里重放；见下面的信任边界。psql 先发 `\restrict <随机密钥>`，文本检查仍拒绝未加引号的反斜杠。对比的目录除了表、列、类型、默认值、NOT NULL、约束、索引（唯一性、列、谓词，含同名删了重建）、触发器（含 `tgenabled`，含外键等内部触发器，停用已有触发器视为破坏性）、函数、类型、权限、策略和行级安全，还包括 `pg_roles` 和 `pg_auth_members`。角色属性、口令、连接数或成员关系一旦变化就视为破坏性：这种变更在临时集群里提交，删掉容器就没了，不会落到真实集群，`pg_dump` 也不含角色。预检任何一步失败或结果读不出来也按破坏性处理（fail closed）。只有 `--allow-breaking-catalog` 可以越过这次预检失败，结果仍写入报告。`--allow-incompatible-migration` 只放行粗记号，不能越过预检失败。备份仍在真正的迁移之前。
   - **备份**：用 `pg_dump`（custom 格式）把数据库直接从数据库容器转储到 `--backup-dir`（目录 0700、文件 0600，密码只在数据库容器内读取，不出现在命令行）。目录若是符号链接就拒绝，不跟随；已有目录也会收成 0700。文件用 `O_EXCL|O_NOFOLLOW` 新建再 `fchmod` 成 0600，文件名在秒级时间戳之外还有随机后缀，已存在的文件或符号链接不会被截断、跟随或删除。备份失败、超时或文件头无效则中止。没有数据卷（首次部署）时两者都跳过。
5. 构建（可用 `--build-network host`），然后 `docker compose … up -d --wait --force-recreate --remove-orphans`（此时迁移已执行并提交）。
6. 对入口做冒烟：`GET /board` 为 200、不带凭据的 `POST /mcp` 为 401，以及用冒烟令牌完成一次真实的 MCP `initialize`、`tools/list`、`fetch_inbox`。

`up` 或冒烟失败（含超时）时，脚本把之前的镜像重新打回原标签，再次 `up`，逐个服务核对是否都回到了之前的镜像 ID，并复测冒烟；只有核对和冒烟都通过才报告 `rolled_back`。重打标签或回滚 `up` 失败不会提前返回：其余镜像照常恢复，仍会执行 `up`，`up` 失败再做一次尽力恢复（`up -d --wait --wait-timeout … --remove-orphans`，同样带硬超时），最后在报告里给出逐服务的手工恢复命令并保留保留标签。标准输出是 JSON 报告，进度写到标准错误。

冒烟令牌是一个普通客户端身份，只通过文件路径传入，从不打印；文件权限保持 0600，放在仓库外：

```sh
# SPDX-License-Identifier: Apache-2.0
cd baton/deploy
docker compose run --rm --no-deps \
  -v "$HOME/.config/baton:/delivery" admin issue deploy-smoke \
  --output /delivery/deploy-smoke.token --uid "$(id -u)" --gid "$(id -g)"
python3 ../scripts/deploy.py -f compose.yaml -p baton \
  --smoke-token-file "$HOME/.config/baton/deploy-smoke.token" \
  --backup-dir "$HOME/baton-backups"
```

- 项目名与 `-f` 文件按你平时给 `docker compose` 的写法；TLS 实例加 `-f compose.tls.yaml`，并用 `--base-url https://<host>:<端口>`（私有 CA 用 `--ca-file`）。默认入口是 `http://127.0.0.1:8080`；明文 http 只允许回环地址，避免把令牌发往远端。
- 超时：`--wait-timeout`（默认 300 秒，传给 `up --wait-timeout`，脚本在其后 120 秒强杀 `up` 的整个进程组）、`--build-timeout`（1800）、`--backup-timeout`（900）、`--precheck-timeout`（迁移预检的每一步，300）、`--command-timeout`（其余 docker 命令，120）、`--lock-timeout`（等另一个部署释放互斥锁；默认是构建、备份、预检、up、冒烟和普通命令预算之和）。冒烟由 `--smoke-timeout` 和 `--request-timeout` 限定：截止时间在读取响应期间就生效（到点关闭套接字），响应正文超过 1 MiB 也判失败，对方持续缓慢发送或塞入超大正文都不能把部署或回滚拖过截止时间。回滚的 `up` 和尽力恢复的 `up` 同样有超时；强杀之后的等待也有上限，逃出进程组仍占着管道的子进程不会让部署一直卡住。
- `docker compose build` 没有网络参数；构建需要宿主网络时加 `--build-network host`，脚本会为带 `build` 的服务临时生成一个只含 `build.network` 的 Compose 覆盖文件，仅用于构建。`--no-build` 跳过构建，也跳过工作区可读性检查。
- 孤立服务：`up --remove-orphans` 会删除 `-f` 文件集合之外的服务容器，回滚不会重建它们（只恢复镜像）。脚本在预检时列出这些服务（报告里的 `orphans`），没有 `--allow-remove-orphans` 就中止；先确认没有漏传某个 `-f` 文件。这正是移除某个 Compose 服务后需要加该参数的场景。
- `--dry-run` 只执行只读的 docker 查询，列出将要运行的命令，不备份、不构建、不重建、不发请求；工作区可读性检查仍会执行（它不需要 Docker）。`--no-backup` 只放弃备份（写入报告），不放弃迁移预检；只有 `--no-backup` 加 `--allow-incompatible-migration` 同时给出时，数据库启动不了才允许继续。
- 退出码：0 已部署；1 失败但旧版本在运行（已回滚并核对，或预检、备份、构建阶段中止时从未被改动）；2 用法或配置错误（含工作区文件对其它用户不可读，或迁移文件缺失）；3 失败且旧版本没有核对通过（回滚失败或不完整，或首次部署没有可恢复的版本）。

**回滚的边界（务必先读）。** 回滚只恢复镜像：不恢复数据库，不恢复 Compose 拓扑，也不撤销已经提交的 `GRANT`/`REVOKE`、行级安全或策略。冒烟时迁移已经提交，所以旧镜像必须能在新 schema 上运行，即迁移必须向后兼容（expand/contract）：一个版本里只增加可空或带默认值的列和新表，不删除、重命名、改类型，也不对已有列加 `NOT NULL`，不改已有对象的权限，不对已有表启用行级安全或加策略；收缩（contract）放到再下一个版本，等旧镜像不再需要回滚之后。上面的隔离容器预检对着真实 schema 校验这一点；迁移里的数据语句在空的 schema 副本上看不出效果，由同一套原文记号兜底：`UPDATE`、`DELETE`、`TRUNCATE`、`MERGE`，以及 `DROP`、`RENAME`、改类型、`SET NOT NULL`、无默认值的 `NOT NULL` 列、`GRANT`/`REVOKE`、策略、行级安全和 psql 反斜杠元命令。注释和字符串都不去掉。当前 `bridge/space_schema.sql` 与已部署修订相同时，重新应用不需要覆盖参数。看不到已部署修订时，或者新增或删除的行里出现引号或注释分界时，整份文件当作新增，补数据的 `UPDATE`、`DO`、`ON DELETE` 和列名 `role` 会被报出来。目录对比仍兜住文本里没有这些词、但目录变了的情况。这仍不是兼容性证明。`deploy/migrate.py` 里的授权不在这份迁移 SQL 中，预检看不到。若仍然破坏了兼容，应先停止服务，再用备份手工恢复，例如（在 `deploy/` 目录，密码在容器内读取）：

```sh
# SPDX-License-Identifier: Apache-2.0
docker compose exec -T database sh -c \
  'PGPASSWORD="$(cat /run/secrets/database-admin/postgres.password)" exec pg_restore -U baton_bootstrap -d baton --clean --if-exists' \
  < "$HOME/baton-backups/<备份文件>.dump"
```

**信任边界。** 这段检查故意写得很粗（crude on purpose）。迁移是经过审查的仓库代码。预检失败即关闭（fail closed），从不尝试解析 SQL。默认只扫描相对当前已部署修订新增的行。已部署文本来自正在运行的应用镜像标签 `org.baton.deployed-schema`，否则来自记录下来的部署状态；用 difflib 对原文做差异，相等的行留空，行号仍对得上新文件。只要有一行新增或删除的文本里出现引号或注释分界（`$`、`'`、`"`、`/*`、`*/`、`--`），差异捷径就关掉，整份新 schema 按原文扫描。报告写明用的是 `whole-file` 还是 `added-lines`，以及原因。操作者核对之后，若报出的是已经部署过的已知记号，再用 `--allow-incompatible-migration`。两者都没有、标签读不出来或几份镜像不一致时，同样整份文件当作新增，并在报告里写明。整份文件扫描时，注释里已经存在的目录形词（例如 `GRANT`）仍要 `--allow-breaking-catalog`。不删注释，也不删字符串，大小写不敏感，按词边界匹配；注释或字符串里的记号同样算。只有这一套扫描，不另做去掉注释或字符串的第二遍。记号都是单个词，避免被注释拆开：`extension`、`program`、`system`、`database`、`role`、`session_authorization`、`set_config`、`authorization`、`commit`、`rollback`、`abort`、`savepoint`、`release`、`reset`、`update`、`delete`、`truncate`、`merge`、`prepare`、`transaction`，以及单词 `do`。唯一的例外是短语 `on conflict do nothing`：`do` 只有出现在这句原样短语里才不算，中间夹注释、`E'...'`、`U&'...'` 或别的语句都算。`INSERT ... SELECT` 不单列。`END` 不是记号，所以 `CASE ... END` 和 `BEGIN ATOMIC` 可以通过。`BEGIN;` 可以通过；`BEGIN TRANSACTION` 含有 `transaction`，会报出来。报告写出扫描模式、原因、记号和行号。`--allow-incompatible-migration` 只放行这些记号，不能越过目录预检失败。`DROP`、`GRANT`/`REVOKE`、改类型、`SET NOT NULL`、策略、行级安全、停用触发器和 psql 元命令也在同一份原文上匹配，和目录对比一样，只有 `--allow-breaking-catalog` 能越过。已经部署且未改动的 schema 文本不会再次被标出。`BEFORE UPDATE` 和 `ON DELETE` 因为含有 `update`/`delete` 这两个词，出现在新增行里也会被标出；误报接受。目录对比包含已有内部触发器的 `tgenabled`；新外键在已有表上新增的内部触发器不算破坏。不为服务器端副作用再增加目录对比。

**容器已停止或已删除但数据卷还在。** 这种情况脚本照样先启动数据库、做迁移预检和转储（见上面第 4 步），不会因为没有运行中的容器而跳过；转储失败就中止。区别只在回滚：已停止的容器（包括崩溃的 bridge）的镜像会被记录并保护，可以恢复；如果所有容器都已被删除，就没有"上一版镜像"可恢复，失败时报告 `failed`（退出码 3），数据库仍可用转储恢复。

按 digest 固定的镜像（如 `edge`、`gateway`、`database`）由 Compose 文件决定，不会被重新打标签；回滚到这些镜像要部署文件的上一个版本。若新版本改动了这些固定值，回滚核对会发现服务没有回到之前的镜像 ID，并报告 `rollback_failed`，而不是冒充成功。

edge 的 nginx 每次请求通过 Docker 内置 DNS（`127.0.0.11`，缓存 10 秒）解析 `gateway`，所以重建 gateway 后无需重启 edge；以前 nginx 只在启动时解析上游，gateway 换了容器 IP 就会一直 502。`proxy_pass $upstream$request_uri` 把客户端发来的原始 URI 原样转发，和原先静态 `proxy_pass http://gateway:3000;`（不带 URI）的行为一致，没有额外规范化。

健康检查：bridge 容器内用 `GET http://127.0.0.1:9000/healthz`，它会读取数据库里的 schema 版本，数据库不可达或缺迁移时 bridge 即不健康（只有容器内回环地址的这一条 GET 免令牌，其他容器包括 gateway 仍需令牌）。edge 除 `/board` 外还会对 `/mcp` 发一个不带凭据的 POST，必须得到 gateway 的 401，502/503/504 即不健康；TLS 实例的 edge 检查同样的两个请求，经 `https://127.0.0.1:8443`，因此同时验证端口在监听、证书能完成握手（回环探测不校验证书链）、上游可达。注意：这个 401 在 gateway 内部由鉴权策略直接产生，**不会**访问 bridge，gateway 镜像里也没有 shell、wget 或 curl，无法在它自己的容器里探测；bridge 由它自己的 `/healthz` 保证（gateway `depends_on` bridge healthy），gateway 到 bridge 这一段由 `deploy.py` 的带令牌 MCP 冒烟验证。

### 升级旧实例

早先版本的 Compose 带一个 `codex` 服务（内置 Codex worker）。它已被移除：不再有 `WORKER_MODE`、`CODEX_*` 设置、`admin import-auth`/`import-api-key`、`compose.apparmor.yaml`、专用 seccomp/AppArmor 配置和 `scripts/sandbox_probe.py`，镜像里也不再含 Node.js、Codex CLI 与 bubblewrap。升级时：

- 数据库不需要迁移。`execution_mode='codex'`、`gate_mode='worker_success'` 以及 `a2a_id`、`backend`、`dispatch_started_at` 是**历史值**：旧行仍可读取，schema 的检查约束继续允许它们，所以旧数据库按原样迁移；没有任何路径再领取、执行或批准这些行。排队中且从未派发的旧行可以用 `assign_task` 显式交给一个具名 agent。
- 重新运行 `admin init`（`up` 会自动运行 `initialize`）会把旧注册表里的内部 `dispatcher`（`codex-worker`）记录置为 inactive，并不再写入 gateway；客户端令牌不受影响。
- 旧命名卷 `codex-home`、`codex-data`、`bridge-secrets`（以及 `worker-egress` 网络和 `codex` 容器）不再被使用，也不会被自动删除；确认备份后由运维人员自行清理，例如 `docker compose up -d --remove-orphans` 清除孤立容器，`docker volume rm` 清除不再需要的卷。不得以 `down -v` 来完成升级。

## 评论、提及与协作

指定接收人、主动拉取及同线程回报的步骤见 [指定接收人](onboarding/named-agent.md)。没有 MCP SDK 的主机（只有 Python 3 标准库）可用零依赖的单文件客户端 `adapters/baton_cli.py`，见 [标准库 CLI](onboarding/stdlib-cli.md)。
想让本机的 agent CLI（Codex、agy、opencode、Cline 等）无人值守地接指定给它们的任务，用单文件本地执行器 `adapters/local_runner.py`，见 [本地执行器](onboarding/local-runner.md)。

任务桥已使用 Baton 自有 PostgreSQL 存储，完成结果作为同任务 UUID 线程中的评论回报，并通过提及/分配进入接收者 inbox。`fetch_inbox` 查询当前身份可见的 mentions 与 assignments，不公开其他人的收件箱。请求处理时重新检查权限。

既有 MCP `submit_task`、`get_task`、`list_tasks`、`fetch_inbox` 保留；协作工具包括 `claim`、`renew`、`handoff`、`comment`、`mention`、`plan`、`ready`、`set_work_face`、`mark_blocked`。具体参数、允许操作和验收以 [规格](docs/spec/space.md) 与实现返回的工具 schema 为准。HTTP 使用 `POST /v1/space/{operation}`，所有操作都是 Baton 自有协议。

`submit_task` 不带接收人时不再派发到任何地方：它创建一条**手工、未指派**的任务（范围省略时为 `none`），任何对该页面有 edit 权限的主体都能在 `ready` 里看到并 `claim` 它；reviewer 默认是请求者，gate 规则不变，工具签名保持兼容。带 `assignee_principal` 时只有该主体能领取。默认页面（省略 `page_id`）是请求者私有的，要让别人领取请传共享页面的 `page_id`。手工 gate 必须由 Space owner（根）或配置的 reviewer 且持有审批人委托的主体审批（见下文“审批关卡与委托”），提交或执行结果的人不能批准（根可显式 `self_approval`）。`awaiting_gate` 不能提前显示为 completed。

`x_scope` 首版锁域是同一 PostgreSQL 权威内跨 Space 的 `global`，不提供跨独立数据库的互斥保证。两个 `path` 声明仅在规范化后相等、或其中一个是另一个在 `/` 边界上的目录前缀时冲突：`docs/design` 与 `docs/design/roadmap.md`、`docs` 冲突，与 `docs/designer`、`src` 不冲突，因此声明了互不重叠路径的任务（如 `src/a` 与 `docs/b`）可以同时领取，空路径（仓库根占位范围）与任何路径冲突。规范化为仓库相对路径、`/` 分隔、去除重复分隔符、结尾 `/` 与 `.` 段，拒绝绝对路径与 `..`；默认大小写敏感，运营者可设置 `BATON_SCOPE_CASE_INSENSITIVE=1`（默认关闭，默认 Compose 会把宿主环境中的该变量传给 bridge）以大小写不敏感比较，适用于大小写不敏感的文件系统。`claim` 和 `ready` 使用同一规则，已有 claim 不受影响。**已知限制：** Baton 不解析符号链接、硬链接或其他别名；通过不同路径指向同一批文件的声明**不会**被检测为重叠，请用真实的规范路径声明。`module` 覆盖整个仓库，与 path、module 和任意 migration 都冲突。`migration` 仅按规范化后的完整标识相等互斥，相异标识可并行；path 与 migration 是独立声明域，需要同时保护两者时必须同时声明。显式 scope `{"kind":"none"}` 表示任务不触碰任何仓库路径、模块或 migration，与任何 claim 都不冲突，不能与其他 scope 组合；因此没有文件足迹的审阅、文档、试用类任务可以同时领取。省略 `scopes` 时，所有任务（`create_task_block` 和不论是否带 assignee 的 `submit_task`）默认 `none`；显式 `[]` 仍是仓库根占位范围，旧版内置 worker 写下的历史行也保留该占位范围。已有任务不会被改写，已持久化请求的幂等摘要不变。

### 通知与异步送达

不必再轮询 `fetch_inbox`：mention、指派、任务线程评论、进入 gate、gate 决定、交接和阻塞标记（`task.blocked`）会在业务事务内写入通知 outbox（只含标识符，不含正文）。`wait_inbox` 返回的通知在读取时附加 `actor`（`principal` 与 `kind`）、`subject`（如 `[assigned] 任务说明开头`、`comment on <任务 id>`）和 `body_preview`（评论或手工任务说明的前 200 个字符）；它们按调用者当前的查看权限现算、从不存入 outbox，无权查看时整条通知不返回。webhook 默认保持只含元数据，订阅时设置 `include_preview=true` 才在投递时附带同样的三个字段（投递前同样重新检查权限）；预览会离开 Baton 发往你的端点，只在信任该端点时开启。`wait_inbox(after_seq, timeout_seconds≤55)` 长轮询返回当前仍可见的通知；`webhook_subscribe/webhook_list/webhook_delete/webhook_rotate_secret/webhook_revoke_previous_secret` 让 bridge 向你的 https 端点投递按 Standard Webhooks 规范签名的通知（`webhook-id`、`webhook-timestamp`、`webhook-signature`，密钥为 `whsec_<base64>`；轮换宽限期内带两个签名；弃用的 `X-Baton-*` 头并行发送至 2027-01-31 后移除），至少一次、指数退避、最多 8 次后 dead-letter，接收方按 `webhook-id` 去重并检查 5 分钟时间戳容差。URL 经过 SSRF 检查；运营者可用 `BATON_WEBHOOK_ALLOW_HOSTS` 豁免内部主机，默认 Compose 的 bridge 没有出站网络。细节见 [API](docs/api.md#通知与送达webhookwait_inbox) 与 [安全模型](docs/security-model.md)。适配器默认白名单不变，可用 `--allow-tools fetch_inbox,wait_inbox` 显式放行 `wait_inbox`。

### 阻塞标记与错误提示

被指派的接收人或 reviewer 领取不了任务（`scope_conflict`、`lease_held`、`dependency_blocked`，或 `waiting_input`、`other`）时，调用 `mark_blocked(operation_id, task_id, reason_code, note)`：任务线程出现一条 `[blocked]` 评论，发起人收到 `task.blocked` 通知，响应时效约定记为已响应，任务视图出现 `blocked={reason_code, by, at}`，直到任务被领取或改派；审计事件 `task.blocked` 只记原因码。`list_comments` 可只给 `task_id`，不必知道页面和线程。

`422 invalid` 的 `error.details` 是 `[{"field": "<名称>", "expected": "<类型或格式>"}]`，覆盖未知字段、缺失必填字段和类型错误，MCP 错误文本末尾带同样的 JSON；不回显任何输入值。对调用者不可见的对象（包括已撤权）返回 `404 not_found`，只有“能看到但不允许该操作”才返回 `403 forbidden`。

运营者命令：`docker compose exec bridge python -m bridge.scopes migrate-manual-root-to-none [--dry-run]` 把尚未被领取、仍带旧仓库根占位范围的**手工**任务改为显式 `none`（每个任务一条 `task.scopes_migrated` 审计，输出一份 JSON 汇总）；旧版 `codex` 行和有效租约中的任务不受影响。

### 响应时效与超时升级

指派给某个 agent 或人之后，任务按优先级获得响应时限（`urgent`/`normal`/`low`，默认 20/60/1440 分钟，可用 `BATON_RESPONSE_MINUTES="urgent=20,normal=60,low=1440"` 覆盖）。接收人领取任务，或在任务线程评论，就算响应；到期仍无响应时，bridge 的后台循环（每 30 秒，PostgreSQL 咨询锁单飞）会给负责人（`escalate_to`，默认 owner）写 `task.overdue` 通知，并在后备接收人（`fallback_assignee`）有 edit 权限且任务未被领取时把任务改派给它。已被领取的任务从不被改派。`set_priority` 调整优先级。细节见 [API](docs/api.md#响应时效与超时升级) 与 [规格 X20](docs/spec/space.md)。适配器默认白名单不变，`set_priority` 需用 `--allow-tools` 显式放行。

### 依赖图与就绪任务

`x_graph` 用 `depends_on` 表示任务依赖：`A.depends_on=[B]` 表示 B 必须达到 `done`，并有当前 epoch 对应的 gate pass，A 才能领取。调用 `create_task_block` 或 `submit_task` 创建任务时可同时传入 `depends_on`，任务与初始依赖在同一事务中生效，别的主体不能在依赖建立前抢先领取。

`plan(operation_id, graph=[{task_id, depends_on}])` 只为已存在的任务 UUID 原子建立或追加依赖，使用稳定 UUID 操作键；任一无效引用、越权或循环都会整批回滚。只有从未领取或执行过的 `queued` 任务可追加，租约过期或结果被拒后也不能借此改图；不提供删除依赖。图由人或外部规划 agent 创建，Baton 负责存储与校验，不内置 planner。

`ready(page_id=None, limit=50)` 返回当前调用者可 edit、符合指定接收人要求、租约允许、依赖通过且无 scope 冲突的候选任务。依赖访问权限在建图、查询和领取时重新检查；隐藏依赖不会泄漏其 UUID。ready 是即时快照，不保留领取名额；实际 `claim` 仍会原子重验全部条件，命名 agent 继续自愿拉取。

### 施工面清单（x_ready）

`x_ready` 让任务携带施工面清单：基线（`base_commit`）、声明范围（`scopes`）、环境与夹具、约束和验收命令（`work_face`）。创建任务时设置 `require_work_face=true`，清单不完整就不能领取：`claim` 返回 `409 work_face_incomplete`，`ready` 也不会列出它；`set_work_face` 可由任务 owner、reviewer，或持有关联准备任务租约的人补全。准备任务是 `task_kind="prep"` 的普通任务，施工任务用 `depends_on` 指向它，复用 x_graph 的 gate 规则：准备任务通过前，施工任务返回 `dependency_blocked`。注意 x_ready 和上面的 `ready` 查询不是一回事：`ready` 只列出可领取的任务，x_ready 是任务上的清单和门控。Baton 不执行 `acceptance_command`，它只是给领取人和 gate 审阅人看的信息。细节见 [规格 X18](docs/spec/space.md) 与 [API](docs/api.md)。

### 审批关卡与委托

**审批关卡：只有持有审批人委托的主体（人或 agent）才能批准，任何人不能批准自己提交或执行的工作。** 规则（DECISIONS 287、291）：

- **根。** Space 的 owner（创建 Space 时记录的精确身份：`spaces.owner_principal` + `owner_account` + `owner_kind`，不分 human 或 agent）是该 Space 的根审批人，不需要委托记录，也不必出现在任务的 reviewers 里。同账户的其他 principal、同账户下的 agent，以及同名但 kind 不同的主体都不是根，必须持有委托。没有记录 `owner_principal` 的 Space（旧数据无法从创建它的 `space.created` 审计事件回填时）没有根，直到运维人员用 `set-space-owner` 设置。委托只从根向下流动。
- **其他主体两者缺一不可。** 任务配置的 reviewers 决定**请谁**审阅；持有覆盖该任务的有效审批人委托决定该 reviewer **能否**批准。没有委托返回 `403 delegation_required`，不是配置的 reviewer 返回 `403 forbidden`。
- **自批。** 除根以外，任何人不能批准自己提交或执行的工作：结果作者、在被审批 epoch 持有过 claim 的任何 principal 或 account（交接时原持有人也算该 epoch 的执行者）都返回 `forbidden`。只创建了请求、没有提交或执行结果的任务 owner 可以批准。根可以批准自己的工作，但必须在 gate 调用里显式给出 `self_approval=true`，否则被拒绝；非根即使带标志也被拒绝。这类批准在 `x_gate` 和审计里记为 `self_approval`（并标记 `root`），不会冒充别人的批准。
- **双人确认。** 可按范围配置：运维人员用 `policy-set` 写入 `baton.x_approval_policy(space_id, scope_kind, scope_value, required_approvals 1..2, root_required)`，策略通过任务声明的 scope 匹配（path 策略覆盖该路径及其下，整仓 scope 落在所有 path 策略之下，多条匹配取最严）；创建任务时 owner 还可设置 `approvals_required`（1 或 2）：它可以提高要求，只有创建者是 Space owner 时才能降低。要求 2 个批准时，只有两个不同的审批人（不同账户）对同一个 `result_epoch` 都 pass，任务才变成 done；一个 pass 后任务仍在 `awaiting_gate`。`root_required` 时其中必须有根，根通过之前任务不会 done；任何有权方的 fail（包括根的 fail）都按原有失败路径把任务退回，旧 epoch 的 pass 不再计数。单人组织里的两方就是根加一个被委托的审批人。task view 带有效的 `approvals_required`、`approvals_passed` 和 `root_required`。

委托链：`baton.x_delegation` 中一行委托含授权人、被委托人、范围 `approve`、可选的 Space（空表示根拥有的所有 Space）、`may_delegate`、`parent_id`、必填理由（至多 500 字符，例如 `DECISIONS 287`）、可选过期时间和撤销字段。**根委托**（无 parent）的授权人是该 Space 的根身份（owner，不分 kind）；持有该 Space 中有效 `may_delegate` 委托的主体可以再授予**子委托**。被委托方永远不能超过授权人：Space 范围相同或更窄，过期时间不晚于父委托（父委托有过期时间时子委托也必须有），任何祖先被撤销或过期，子委托立即失效，链最多 3 层。创建时拒绝任何会形成环的委托：不能授予自己、自己上方的根，或自己链上已有的人。gate 时检查整条链，链的根授权人必须是任务所在 Space 的根（精确身份）。创建、撤销和每次 gate 的审计都记录完整链（从叶到根的委托 ID）。委托行只追加，唯一允许的修改是一次撤销；撤销父委托会使其下所有子委托失效。重放同一个 gate 请求时会重新检查整条链。

owner 目前不在 Baton 中，所以现在走运维路径，南瓜持有的就是这样记录的 owner 根委托（沿用 bridge 现有的数据库配置，不需要 owner 的 token，输出为不含秘密的 JSON）：

```sh
# SPDX-License-Identifier: Apache-2.0
# 在 Docker Compose 所在的 deploy/ 目录运行。
docker compose exec bridge python -m bridge.delegations create \
  --grantor <Space 的 owner_principal> --grantor-account <Space 的 owner 账户> [--grantor-kind human|agent] \
  --delegate <南瓜的 principal> --delegate-account <账户> --scope approve \
  [--may-delegate] [--space <space-uuid>] [--expires <ISO8601，须带时区>] \
  --reason "DECISIONS 287" --operator <执行人姓名>
docker compose exec bridge python -m bridge.delegations revoke --id <委托 UUID> --operator <执行人姓名>
docker compose exec bridge python -m bridge.delegations list [--active]
# 双人确认策略（默认 path 范围为空，即整个 Space）
docker compose exec bridge python -m bridge.delegations policy-set --space <space-uuid> \
  [--scope-kind path --scope-value src/payments] --approvals 2 [--root-required] --operator <执行人姓名>
docker compose exec bridge python -m bridge.delegations policy-list [--space <space-uuid>]
# 为没有根身份的 Space 记录根（账户必须是该 Space 现有的 owner 账户；有审计）
docker compose exec bridge python -m bridge.delegations set-space-owner --space <space-uuid> \
  --principal <owner 的 principal> --account <owner 账户> --kind human|agent --operator <执行人姓名>
docker compose exec bridge python -m bridge.delegations policy-remove --space <space-uuid> [--scope-kind ... --scope-value ...] --operator <执行人姓名>
```

这条命令把 owner 的决定记录为由指定运维人员执行：委托行写 `created_via='operator'` 与 `created_by=<执行人>`，审计事件把授权人记为请求者、执行人记为执行者，两者都能查到；它受同样的规则约束：授权人（principal、账户、kind）必须正是所指 Space 记录的根身份，被委托方不能是 owner 自己，且只能记录根委托。被委托方必须是注册表中已连接的主体（人或 agent）；省略 `--space` 表示覆盖该根身份拥有的所有空间；`--may-delegate` 允许被委托方通过 API 再授予更窄的子委托。owner 拥有人类身份之后（计划下周），可以改用 API：Space owner 创建根委托，持有 `may_delegate` 委托的主体创建子委托：`delegation_create`、`delegation_revoke`、`delegation_list`，见 [API](docs/api.md) 与 [规格 X25](docs/spec/space.md)。

## 端到端验收

[`scripts/e2e.py`](scripts/e2e.py) 对运行中的栈走一遍两身份的具名 agent 流程：提交者把任务指派给一个 agent principal；agent 领取、在任务线程评论并提交结果；提交者以 Space 根的身份 gate 通过。Baton 不执行任何任务，这里的 agent 只是第二个客户端身份。先在 `deploy/` 目录为测试实例签发两个独立身份（提交者将成为它所创建 Space 的根，所以是创建 Space 的那个身份）：

```sh
# SPDX-License-Identifier: Apache-2.0
cd deploy
mkdir -p "$HOME/.config/baton/e2e"
chmod 700 "$HOME/.config/baton/e2e"
docker compose run --rm --no-deps \
  -v "$HOME/.config/baton/e2e:/delivery" \
  admin issue e2e-submitter --kind human --account e2e-submitter \
  --output /delivery/e2e-submitter.token --uid "$(id -u)" --gid "$(id -g)"
docker compose run --rm --no-deps \
  -v "$HOME/.config/baton/e2e:/delivery" \
  admin issue e2e-agent --kind agent --account e2e-agent \
  --output /delivery/e2e-agent.token --uid "$(id -u)" --gid "$(id -g)"
cd ..
```

在仓库根目录安装固定客户端依赖并运行，每轮使用新的稳定业务键：

```sh
# SPDX-License-Identifier: Apache-2.0
python3.12 -m venv .venv
.venv/bin/python -m pip install --require-hashes --only-binary=:all: -r deploy/requirements.txt
.venv/bin/python scripts/e2e.py --key e2e-acceptance-v1 \
  --credentials-dir "$HOME/.config/baton/e2e"
```

验收覆盖真实 MCP 初始化与调用、按业务键的任务去重、agent 领取与同线程评论、agent 自批被拒、提交者作为根 gate 后完成并在同线程收到完成评论、不带接收人的任务出现在 agent 的 `ready` 里，以及 request/task 在 bridge、gateway、edge 日志中的审计关联。`--submitter`、`--agent` 和 `--agent-account` 可改用其他身份名。旧的 `--mode demo|codex` 与 `--token-file` 已移除（不再有内置 worker 和 demo/codex 后端），传入会立即报错并说明原因。单元测试不能替代真实 PostgreSQL 的并发、fencing 和审计权限测试。重跑同一效果应保留业务键。

应用与适配器统一使用带完整传递依赖和发行文件 SHA-256 的锁文件；安装时保留 `--require-hashes --only-binary=:all:`，只接受已锁定的 wheel，避免安装时另行解析构建依赖。版本约束位于 `deploy/requirements.in`，锁文件再生步骤见 [贡献规则](CONTRIBUTING.md#依赖锁文件)。

自定义入口用 `--url`，自定义 Compose 项目用 `--project`，私有 CA 用 `--ca-file`；回执通过 `--artifact-dir` 保存到仓库外的私有目录。源码仓库不保存真实运行回执。

### Space HTTP 协作验收

[`scripts/space_acceptance.py`](scripts/space_acceptance.py) 通过认证 HTTP API 验证共享与撤权、team、connected account 署名、人工审阅 agent 提案、提及与同线程回复、并发 scope 冲突、租约 fencing、handoff、gate，以及依赖图、ready、循环回滚和隐藏依赖。它会创建和修改测试数据，应在空闲的独立测试实例运行；path 声明按目录前缀与该 PostgreSQL 权威内其他任务的声明竞争。

先在 `deploy/` 目录为该测试实例准备四个独立客户端身份。名称必须保持如下；`space-owner`、`space-reviewer` 是各自账户的 human，`space-peer` 是独立账户的 agent，`space-agent` 是连接到 `space-owner` 账户的 agent。每个身份使用自己的令牌文件，不能共用令牌：

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

为每轮独立验收选择新的稳定 `--key`，同一业务动作的请求重试保留其操作键。成功回执为仓库外的 `space-acceptance.json`。此脚本的 PASS 仅覆盖本实例的上述 HTTP 协作检查；经 MCP 的两身份流程另跑 `e2e.py`，真实跨主机的自愿拉取仍须由接收方参与验收。

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
```

将 hostname、路径、uid/gid 换成自己的值。非 root nginx 必须能读取私钥。运行 `docker compose -f compose.yaml -f compose.tls.yaml up --build -d --wait`。TLS override 替换 HTTP 映射，只发布选定 HTTPS 端口，可将 `HTTPS_PORT` 设为 443。证书由操作者续期，私有 CA 通过 `BATON_CA_FILE` 或 `--ca-file` 配置，不能关闭证书校验。

没有 MCP SDK 的主机可以用下面一个代码块下载固定提交的零依赖客户端并读一次收件箱（把尖括号占位符换成自己的值；使用系统信任的 CA 时去掉 `--ca-file` 一行）：

```sh
# SPDX-License-Identifier: Apache-2.0
mkdir -p "$HOME/baton/adapters" && \
curl -fsSL -o "$HOME/baton/adapters/baton_cli.py" \
  https://raw.githubusercontent.com/fledge-labs/baton/2a386723d3e1081a3586cddf778a2a5de782190f/adapters/baton_cli.py && \
python3 "$HOME/baton/adapters/baton_cli.py" inbox \
  --url <BATON_URL> --token-file <TOKEN_FILE> --ca-file <CA_FILE>
```

更多子命令见 [标准库 CLI](onboarding/stdlib-cli.md)。

客户端说明：[Codex](onboarding/codex.md)、[agy](onboarding/agy.md)、[Cline](onboarding/cline.md)、[Devin](onboarding/devin.md)、[Claude Code 与 SSH stdio](onboarding/claude-code.md)、[OpenCode](onboarding/opencode.md)。适配器支持 `BATON_URL`、`BATON_CA_FILE`、`BATON_PYTHON`，入口为 `adapters/launch_mcp.sh <agent-id> <TOKEN_FILE>`。每个客户端使用独立 token；适配器不授予额外业务权限。无法安装 MCP SDK 的主机用 [标准库 CLI](onboarding/stdlib-cli.md)（`adapters/baton_cli.py`，只需 Python 3.9+ 标准库，经 HTTP API 访问）。

## 身份映射与令牌生命周期

默认 `admin issue example-client` 创建 `kind=agent`、`account=example-client`。只有离线管理员可用 `--kind human|agent --account ID` 设置认证映射；请求体、HTTP API 和看板都不能修改自己的身份。human 与 agent 共享 connected account 时仍保留各自真实 actor；`codex-worker` 是旧版内置 worker 留在历史行里的服务主体名，保留不可签发或导入。

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

管理员可在新的 rotation operation 中显式更改映射；同 operation 不同映射被拒。`init` 可为旧身份注册表显式补充 agent/self 映射，不会静默导入旧业务数据。认证后端对缺失或无效映射失败闭锁；已启动任务不会因 token 撤销自动取消。旧注册表里的内部 `dispatcher` 记录已被 `init` 置为 inactive，不能导出、轮换或认证。

若管理员已通过独立安全渠道预签发普通客户端令牌，可将文件只读挂载给新实例的离线管理员采用；无需把原值放入参数、环境变量或聊天：

```sh
# SPDX-License-Identifier: Apache-2.0
# 将 <CLIENT_TOKEN_FILE> 替换为仓库外的私有令牌文件绝对路径。
docker compose run --rm --no-deps \
  -v "<CLIENT_TOKEN_FILE>:/incoming/client.token:ro" \
  admin import-client example-client --source /incoming/client.token \
  --kind agent --account example-client
```

文件内容须为 32–256 个 URL-safe 字符（字母、数字、`_`、`-`），可带一个末尾换行。导入只写本实例 0600 客户端秘密文件和哈希注册表，不联系原签发系统。新 ID 可导入；现有 ID 仅在 token 哈希、身份映射、client 角色及 active 状态全部相同时无写入重放。不同 token/映射/角色、撤销身份、旧版内部 `dispatcher` 记录或其他身份已占用的 token 一律拒绝，不轮换、不恢复激活，也不修改现有状态。省略映射时使用 `agent` 与当前 ID；重放自定义映射时须再次给出相同参数。

## PostgreSQL、备份与故障边界

`initialize` 只生成项目专属文件：数据库管理员与应用密码、app 的 `database.json`。重跑不换密码；已初始化数据库丢失秘密时拒绝随机重建，须恢复匹配备份。密码不进入 Compose 环境值、进程参数、镜像或 Git；PostgreSQL 通过 `POSTGRES_PASSWORD_FILE` 读取专用文件。

PostgreSQL 和一次性 `migrate` 以 UID/GID 999 运行。只有它们持有 bootstrap 密码；迁移事务使用稳定 advisory lock，并以 `baton_owner NOLOGIN` 创建 schema、表和触发器。长期 bridge 使用 UID/GID 10001 和非 owner 的 `baton_app`，配置路径固定为 `BATON_DATABASE_CONFIG=/run/secrets/database.json`。业务角色没有 schema CREATE、角色管理或 owner membership；`x_audit` 仅 SELECT/INSERT，触发器拒绝 UPDATE/DELETE/TRUNCATE，业务角色不能禁用它们。

PostgreSQL 保存空间与任务权威。`x_audit` 与业务变更同事务追加；边缘、网关和 bridge 的 JSONL 仅为关联日志，不是防篡改账本。数据库 SQL/参数日志关闭，普通 SQL 错误不进入数据库日志；应用只报告安全错误码，数据库保留 FATAL/PANIC 诊断，可用性由健康检查核验。

所有状态和秘密在项目专属命名卷。`docker compose down` 保留卷；仅在明确丢弃整个新实例时使用 `down -v`。备份必须涵盖 PostgreSQL 与秘密卷，并以受控恢复验证一致性。迁移服务只启动时运行；未来 schema 版本变更及真实数据迁移需要独立验收，不靠重新初始化冒充升级。

[架构](docs/architecture.md) · [安全模型](docs/security-model.md) · [第三方许可证](docs/THIRD_PARTY.md) · [贡献规则与 DCO](CONTRIBUTING.md)
