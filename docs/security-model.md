<!-- SPDX-License-Identifier: Apache-2.0 -->
# 安全模型

## 凭据与认证主体

客户端 token 由系统随机数生成，原值只在 0600 秘密文件中；gateway 和注册表保存 SHA-256 及身份元数据。摘要也是运行状态，不能提交。gateway 和应用双层校验，client 与内部 dispatcher 分角色授权。

仅离线管理员可设定 `kind=human|agent` 与 `account` 的认证映射。轮换默认保留映射，改变映射需要新的显式管理操作；业务请求不能自行声明真实 actor 或提升为 human。内部 dispatcher 使用 service 记录，不可作为用户 connected account，也不能导出给客户端。运行时对缺失映射失败闭锁；兼容旧注册表由管理员 `init` 显式补齐，不在 API 中猜测。

`initialize`/ops `admin` 是离线、短生命周期的 root 管理进程，仅额外持 CHOWN、DAC_OVERRIDE、FOWNER 来初始化指定卷。Docker 管理员属于可信边界。导出文件默认 0600，`--uid/--gid` 仅调整明确指定的交付文件。多文件凭据轮换没有跨文件事务；进程中断须核对注册表、操作账本和秘密文件，不能假装原子成功。

## 数据库角色与秘密文件

数据库管理卷保存 `postgres.password`、`app.password`，仅 PostgreSQL 与一次性 migrate 服务挂载，文件及目录归 UID/GID 999。应用数据库卷单独保存 UID/GID 10001 的 `database.json`；bridge 通过 `BATON_DATABASE_CONFIG=/run/secrets/database.json` 读取，不持 bootstrap 凭据。dispatcher 在嵌套的 `/run/secrets/bridge/dispatcher.token`。worker 没有数据库秘密卷或数据库网络。

PostgreSQL 使用文件型 `POSTGRES_PASSWORD_FILE`，Compose 不注入密码环境值；迁移器在进程内从文件读取，通过 psycopg 传递，不把秘密放进 argv、URL、日志或 SQL 错误输出。重复 init 保留原密码，已初始化数据库缺失凭据时拒绝随机重建。管理工具不会自动把旧实例的 SQLite/消息状态移入 PostgreSQL。

DBA 登录是 `baton_bootstrap`；它只由数据库初始化与短期迁移服务使用。迁移持固定 advisory transaction lock，在单事务中创建/校验角色、以 `baton_owner NOLOGIN` 执行 schema，再设置授权。应用角色 `baton_app` 非 superuser、非 owner、无角色/数据库创建权、无 owner membership、无 schema CREATE。schema_version 对应用只读；普通业务表授予所需 DML，审计表仅 SELECT/INSERT。

`x_audit` 的 owner 触发器拒绝 UPDATE/DELETE/TRUNCATE；应用没有这些表权限，也无法 DISABLE TRIGGER 或改变所有者。它保证应用角色下的追加约束，不抵抗 PostgreSQL 超级用户、Docker 管理员或宿主失陷。业务授权仍由服务端验证，不能以持有数据库应用密码代替用户权限模型。

## 网络与执行隔离

默认 HTTP 仅绑定回环地址；外部访问必须启用 TLS override 与有效证书。后端没有 published ports。PostgreSQL 使用单独 internal network，仅 bridge 和 migrate 连接；worker 的 harness 通过自身 egress 调用模型，工具网络由内层沙箱禁止。页面 Host/Origin 白名单由 PUBLIC_URL 推导。

应用使用固定依赖、非 root、只读根、cap_drop=ALL、no-new-privileges；没有 privileged、host network 或 Docker socket。PostgreSQL 与 migrate 的 UID/GID 是 999，应用是 10001；管理初始化权限独立列出，不混入长期业务服务。

Codex 0.154.0 工具沙箱拒绝读取 `/data`、`/config`、`/run/secrets`、`/home/agent/.codex`，只允许当前任务目录写入，禁止工具联网。harness 的模型认证与沙箱工具分离。结果的已知秘密过滤只是额外措施，不能替代访问控制。

worker 以 `codex exec --json` 运行，只依据带类型的 CLI 错误事件和少量固定 stderr 模式分类失败；原始 stderr、stdout、prompt 和模型文本不写日志、审计或 A2A。A2A 失败只携带带版本的 `baton_failure` 数据片段（`version`、`code`、`stage`），bridge 只接受 worker 的已知错误码，其余一律降为固定的 `A2A_FAILED_UNCLASSIFIED`；任务 `error` 只保存错误码，失败说明以固定中文文本写入任务线程评论。

worker `/healthz` 位于现有 dispatcher 认证之后，仅在 cgroup v2 资源采样新鲜且最近一次真实 canary 成功时返回 200，否则 503；容器健康检查使用只读的 `python -m bridge.worker_health check`，只读取 worker 发布的快照文件。canary 与业务任务走同一条执行路径和同一把执行锁，预算仍为 30 秒（业务 300 秒），沙箱权限不变。每次 Codex 执行（含 canary）记录各阶段的单调时钟毫秒数、cgroup `cpu.stat`/`io.stat`/`pgmajfault` 增量和是否冷启动；审计行及执行/探针记录只含这些数字和固定集合内的事件类型名，不含任何消息文本，无法读取的值记为 null 而不是 0。

基础配置仅 worker 使用 `apparmor=unconfined` 与 Moby 基线上六项 syscall 例外，以支持嵌套 bubblewrap。它减少外层 LSM 保护，不能当作原始 Docker 默认隔离。Ubuntu 的非特权 userns 限制可能仍阻止启动；可选 override 使用管理员预加载的 `baton-codex-userns-v1`，其 `flags=(unconfined)` 加 `userns` 是明确例外，不是完整 AppArmor 策略。项目不关闭全局限制或自动加载宿主策略。内层隔离必须由目标主机的 `scripts/sandbox_probe.py` 验证，不得用危险 bypass 获得成功。

这是受信任操作者的独立实验，不是已验证的敌对多租户代码执行平台；不接生产系统、生产数据或敏感源代码。浏览器只在内存中保留 bearer，渲染使用 textContent，不保存 localStorage 或记录 Authorization。

## Webhook 签名、SSRF 防护与至少一次送达

Webhook 签名密钥在订阅时由服务端以 32 字节随机数生成，只在创建响应中返回一次。它以 bytea 存在 `x_webhook`，仅为签名而保存（HMAC 需要原值，数据库备份与应用角色因此都是可信边界）；不写入 operations 重放结果、审计、日志或错误体，列表接口不返回。每次投递对原始 JSON 正文计算 HMAC-SHA256，放在 `X-Baton-Signature`；接收方必须用常数时间比较，并按 `X-Baton-Delivery` 去重。负载只含标识符等元数据，不含 prompt、评论正文或结果文本，所以 webhook 接收端泄漏也不会泄漏正文；接收方凭 ID 回来读取时仍经过 Baton 的权限检查，投递前也会重新检查收件人 view 权限。

订阅 URL 由用户提供，bridge 代为发起请求，因此有 SSRF 面。防护：只允许 https、禁止用户信息；订阅时与每次连接时都解析主机名，任一记录是环回、链路本地（含云元数据地址）、私有（RFC 1918、ULA）、组播、保留或其他非全局地址即拒绝；连接固定到通过检查的 IP 并复核实际对端，防止 DNS rebinding；不跟随重定向；10 秒超时，响应正文不读取、不记录。`BATON_WEBHOOK_ALLOW_HOSTS` 是运营者控制的精确主机名豁免，用于内部接收端或测试，不能由 API 调用者设置，也不放宽 https 要求。每个 principal 最多 10 个订阅，wait_inbox 并发有上限，限制资源占用。该防护不替代网络层出站控制：默认 Compose 的 bridge 没有出站路由，启用 webhook 须由运营者显式提供并限制出站网络。

送达语义是至少一次：重试可能使同一通知多次到达，跨通知不保证顺序，最多 8 次后 dead-letter 并写审计。逐次尝试状态保存在 `x_webhook_attempt`，终局结果写 `x_audit`，两者都不含 URL、查询串、请求头、响应或密钥。`x_notify` 与投递记录目前没有自动清理，保留与归档由运营者负责。

## 审计与诊断

业务变更及 `x_audit` 在同一 PostgreSQL 事务提交。审计只保存真实主体、操作/任务/线程标识、数据库时间、状态、版本/fence 和必要引用；不保存完整 prompt、评论正文或凭据。业务 API 只返回稳定安全错误码，不返回 SQL 或连接串。

PostgreSQL 全局关闭 SQL、慢查询采样和错误参数日志，普通 SQL 错误不记录到服务器日志；`log_min_messages=fatal` 只保留 FATAL/PANIC 诊断，错误 verbosity 为 terse。此选择牺牲详细 SQL 排障信息以避免错误消息、失败行或参数带出正文；可用性由 pg_isready、迁移结果和业务健康检查核验。含角色密码的迁移会话进一步抑制普通日志，异常只输出固定失败说明。相关参数见 [PostgreSQL 17 日志文档](https://www.postgresql.org/docs/17/runtime-config-logging.html)。

边缘与 worker 的白名单 JSONL 用于关联请求和执行，不是防篡改账本。Docker 日志轮转；数据库、worker 私有回执和秘密卷的备份、保留、访问控制由操作者负责。`x_audit` 不能替代受控备份，`down -v` 会删除项目命名卷，不能用于自动升级或修复。

撤销 token 阻断新请求，不自动取消已经启动的 worker。外部执行结果不确定时保持 interrupted 并人工核对，不盲目重跑。演示结果不能冒充真实 Codex；手工 gate 与自动 worker_success 必须区分，不等同生产发布批准。

发布前执行全历史秘密扫描、私有信息扫描、SPDX 与 Signed-off-by 检查。运行回执保存在仓库外；所有贡献提交遵守 [DCO](../CONTRIBUTING.md)。
