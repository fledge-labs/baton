<!-- SPDX-License-Identifier: Apache-2.0 -->

产品定位见 [Baton 定位](positioning.md)。

# 架构与范围

nginx 提供静态看板与反向代理；agentgateway 校验 bearer、只放行 client 角色，转发 MCP 与 HTTP 请求并记录请求元数据。应用使用官方 MCP Python SDK。Baton 是通信与协作层，不执行任务：执行属于通过 MCP、HTTP 或适配器（`adapters/`）连接的外部 agent。空间产品语义及未验证范围见 [规格](spec/space.md)；`baton.space.v1` HTTP/JSON、表结构和全部 `x_` 协作协议属于 Baton，不是官方 Space API。

人机交互通道（聊天平台中的卡片与按钮，飞书优先；设计，尚未实现）见 [通道设计](design/channels.md)。

## 服务与权限

普通 Compose 启动只有 `initialize` 构建应用镜像。该离线管理进程创建命名卷中的秘密文件；`database` 随后启动固定版本 PostgreSQL，`migrate` 复用应用镜像执行单事务 schema 初始化及授权。bridge 必须等待迁移成功；业务进程不会在收到请求时执行 DDL。ops 下的 `admin` 可单独构建，用于新 clone 初始化和凭据管理。

数据库与迁移器使用 UID/GID 999；bridge 使用 10001。数据库仅位于独立 internal 网络，bridge 和 migrate 是其客户端；没有发布数据库端口。TLS override 仅改变 edge 的公开入口。

`baton_bootstrap` 的文件凭据只在 database/migrate 的管理秘密卷；它是运维 DBA，不能视作业务主体。schema、表和触发器由 `baton_owner NOLOGIN` 拥有。bridge 只拿 `baton_app` 的 JSON 配置：`host`、`port`、`dbname`、`user`、`password`；它不是 schema/table owner，没有创建 schema、管理角色、继承 owner 或变更审计触发器的权限。

## 数据与执行

PostgreSQL 的 `baton` schema 保存 spaces、pages、blocks、分享、归属、评论、mentions、任务、claims、幂等记录、gate 和 `x_audit`。版本记录只读提供给应用。任务与报告线程共享 UUID；评论回报与 mentions/assignments 组成兼容 `fetch_inbox`。业务效果、幂等回放结果和审计在同一数据库事务提交。

没有内置执行端。`submit_task` 不带接收人时创建一条手工、未指派的任务（范围默认 `none`，reviewer 默认是请求者）：任何对该页面有 edit 权限的主体都能在 `ready` 里看到并 `claim` 它；带接收人时只有该接收人能领取。bridge 的后台循环只做 webhook 投递和响应时限升级，不派发任务。完成候选必须经过配置的 gate 才能成为 done；兼容视图才将 done 映射为 completed。

旧版内置 worker 写下的行（`execution_mode='codex'`、`gate_mode='worker_success'`，以及 `a2a_id`、`backend`、`dispatch_started_at` 列）是历史数据：仍可读取（提示词不在视图中显示），但没有任何路径会领取、执行或批准它们；schema 的检查约束继续允许这些值，所以旧数据库可以原样迁移。对排队中且从未被派发的旧行，操作者可以用 `assign_task` 把它显式交给一个具名 agent，它随后按手工路径处理。

租约与 fence 以数据库时间和事务锁为权威；`x_scope` 统一 global namespace，覆盖同一 PostgreSQL 权威中的跨 Space 冲突；path 声明按目录前缀判定重叠，不解析符号链接等别名（见 spec X10/A17）。交接在事务内保留 base/context/evidence/thread，并使旧 fence 失效。它不提供跨独立数据库的分布式锁，也不承诺 PostgreSQL 与外部 agent 执行之间的 exactly-once。

## 运维范围

edge 的 nginx 用 Docker 内置 DNS（`resolver 127.0.0.11 valid=10s ipv6=off`）并通过变量（`set $upstream http://gateway:3000`）做 `proxy_pass $upstream$request_uri`，每次请求重新解析 `gateway`；客户端的原始请求 URI、请求头、超时和流式（SSE）设置与原先静态 `proxy_pass http://gateway:3000;` 完全一致（测试用同一批原始 URI 对比两种配置），gateway 被重建、换了容器 IP 后无需重启 edge。

健康检查分层：bridge 调用自己的 `/healthz`（读取数据库 schema 版本；仅容器内回环地址的 GET 免令牌），所以数据库或迁移出问题时 bridge 不健康；edge（HTTP 与 TLS 变体）要求 `/board` 为 200，且不带凭据的 `POST /mcp` 经 nginx 到达 gateway 并得到 401，TLS 变体经 `https://127.0.0.1:8443`，同时验证监听与握手。该 401 由 gateway 的鉴权策略在转发前产生，不经过 bridge，gateway 镜像也没有可执行探测的 shell 或客户端，所以 gateway→bridge 由 `scripts/deploy.py` 在部署后用冒烟令牌走一次完整 MCP 流程（`initialize`、`tools/list`、`fetch_inbox`）验证。

`scripts/deploy.py` 的部署顺序是：预检（迁移 SQL 必须存在；将要构建时，已跟踪且会进入构建上下文的路径必须对其它用户可读，`.dockerignore` 排除的私有文件除外，否则在任何 docker 命令之前拒绝；孤立服务须确认）→ 用 `flock` 锁住该项目（等到 `--lock-timeout` 为止，持有到进程返回，含回滚重打标签）→ 记录全部容器（含已停止的）镜像并按服务加保留标签 → 只要数据卷存在就确保数据库可连接（应用容器全停或已删也照样启动数据库）→ 在独立的临时 Postgres 容器（与数据库服务同一镜像、`--network none`、tmpfs、自带超级用户）里装入 schema-only 与不含口令的角色转储，按 `migrate.py` 的角色切换应用迁移。备份之前迁移 SQL 只在这个预检容器里执行，不在真实库上执行。目录对比含 `pg_roles`、`pg_auth_members`、权限、策略、行级安全、触发器 `tgenabled`，以及已有索引的唯一性、列和谓词；任何破坏性变化或预检失败都中止（fail closed）。预检容器删除之后才做 `pg_dump` 备份（目录 0700、文件 `O_EXCL|O_NOFOLLOW` 加 `fchmod` 0600、随机文件名，符号链接拒绝且不跟随）→ 构建（镜像在 `COPY` 之后 `chmod -R a+rX /app`，不加写，使 uid 999 的 migrate 能读到 schema）→ `up --force-recreate --remove-orphans` → 冒烟（读取期间的墙钟截止时间，以及 1 MiB 正文上限）；每步有硬超时，恢复路径也有。失败时只恢复镜像（不恢复数据库、已提交的授权或行级安全，也不恢复 Compose 拓扑），逐服务核对镜像 ID 后才报告回滚成功；因此迁移必须让上一版镜像仍能运行，备份用于破坏了这一约束时的手工恢复。信任边界：这段检查故意写得很粗（crude on purpose）。迁移是经过审查的仓库代码。预检失败即关闭（fail closed），从不尝试解析 SQL。粗记号只作用在相对已部署修订新增的行上（difflib 对比原文；已部署文本来自运行镜像的 `org.baton.deployed-schema` 标签，否则来自部署状态；未知时整份文件当作新增并在报告里写明）。只有一套扫描，不删注释也不删字符串，大小写不敏感，按词边界匹配。记号是 `role`、`session_authorization`、`set_config`、`authorization`、`reset`、`update`、`delete`、`truncate`、`merge`、`ALTER SYSTEM`、`ALTER DATABASE`、`COPY ... PROGRAM`、`CREATE EXTENSION`、`COMMIT`、`ROLLBACK`、`ABORT`、`SAVEPOINT`、`RELEASE`、`START TRANSACTION`、`PREPARE TRANSACTION`，以及单词 `DO`（仅 `ON CONFLICT DO NOTHING` 这一短语除外）。`END` 不是记号。`--allow-incompatible-migration` 只放行这些记号，不能越过预检失败。`DROP`、`GRANT` 等目录形语句和目录对比一样，只有 `--allow-breaking-catalog` 能越过。未改动的已部署文本不会再次被标出。已有内部触发器的 `tgenabled` 参与对比；新外键带来的内部触发器新增不算破坏。不为服务器端副作用再增加目录对比。

`x_audit` 的业务记录只允许应用追加/读取，并由所有者触发器拒绝修改、删除和清空。边缘、网关与 bridge 的 JSONL 另用于连接入口 request ID 与 task ID；这些文件不提供抗管理员篡改保证。数据库超级用户和 Docker 管理员属于可信边界。

本次替换只用于全新命名卷实例，不自动迁移旧 SQLite 或旧消息数据，不连接真实账户、ChatGPT Space 或既有私有部署。正式 schema 升级、备份恢复与生产迁移需要单独的版本及验收过程。
