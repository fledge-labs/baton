<!-- SPDX-License-Identifier: Apache-2.0 -->
# 安全模型

## 凭据与认证主体

客户端 token 由系统随机数生成，原值只在 0600 秘密文件中；gateway 和注册表保存 SHA-256 及身份元数据。摘要也是运行状态，不能提交。gateway 和应用双层校验，只接受 `client` 角色。

仅离线管理员可设定 `kind=human|agent` 与 `account` 的认证映射。轮换默认保留映射，改变映射需要新的显式管理操作；业务请求不能自行声明真实 actor 或提升为 human。Baton 不再有内部服务账号的客户端凭据：旧注册表里的 `dispatcher` 记录不再能认证，`init` 会把它置为 inactive，也不会写入 gateway；名称 `codex-worker` 作为旧行中的历史服务主体保留，不能签发或导入。运行时对缺失映射失败闭锁；兼容旧注册表由管理员 `init` 显式补齐，不在 API 中猜测。

`initialize`/ops `admin` 是离线、短生命周期的 root 管理进程，仅额外持 CHOWN、DAC_OVERRIDE、FOWNER 来初始化指定卷。Docker 管理员属于可信边界。导出文件默认 0600，`--uid/--gid` 仅调整明确指定的交付文件。多文件凭据轮换没有跨文件事务；进程中断须核对注册表、操作账本和秘密文件，不能假装原子成功。

## 数据库角色与秘密文件

数据库管理卷保存 `postgres.password`、`app.password`，仅 PostgreSQL 与一次性 migrate 服务挂载，文件及目录归 UID/GID 999。应用数据库卷单独保存 UID/GID 10001 的 `database.json`；bridge 通过 `BATON_DATABASE_CONFIG=/run/secrets/database.json` 读取，不持 bootstrap 凭据。bridge 不再挂载其他秘密卷。

PostgreSQL 使用文件型 `POSTGRES_PASSWORD_FILE`，Compose 不注入密码环境值；迁移器在进程内从文件读取，通过 psycopg 传递，不把秘密放进 argv、URL、日志或 SQL 错误输出。重复 init 保留原密码，已初始化数据库缺失凭据时拒绝随机重建。管理工具不会自动把旧实例的 SQLite/消息状态移入 PostgreSQL。

DBA 登录是 `baton_bootstrap`；它只由数据库初始化与短期迁移服务使用。迁移持固定 advisory transaction lock，在单事务中创建/校验角色、以 `baton_owner NOLOGIN` 执行 schema，再设置授权。应用角色 `baton_app` 非 superuser、非 owner、无角色/数据库创建权、无 owner membership、无 schema CREATE。schema_version 对应用只读；普通业务表授予所需 DML，审计表仅 SELECT/INSERT。

`x_audit` 的 owner 触发器拒绝 UPDATE/DELETE/TRUNCATE；应用没有这些表权限，也无法 DISABLE TRIGGER 或改变所有者。它保证应用角色下的追加约束，不抵抗 PostgreSQL 超级用户、Docker 管理员或宿主失陷。业务授权仍由服务端验证，不能以持有数据库应用密码代替用户权限模型。

## 委托链与审批关卡

审批关卡：只有持有审批人委托的主体（人或 agent）才能批准，任何人不能批准自己提交或执行的工作。**Space owner 是根审批人**：根是创建 Space 时记录的一个精确身份（`spaces.owner_principal` + `owner_account` + `owner_kind`，不分 kind），天然拥有审批权，不需要委托记录；共用账户不等于根（A15 的精确 principal 匹配）：同账户的其他 principal、人类账户下的 agent、同名但 kind 不同的主体都必须持有委托。没有 `owner_principal` 的 Space（旧数据无法由 `space.created` 审计事件回填）没有根，直到运维人员用 `python -m bridge.delegations set-space-owner`（要求账户等于该 Space 现有 owner 账户，写 `space.owner_set` 审计）设置；该命令改变谁是根，信任边界与运维路径相同；委托只从根向下流动。其他主体：配置的 reviewer 决定请谁审阅，覆盖该任务的有效审批人委托决定其能否批准，两者缺一不可，人类也不例外。旧版内置 worker 写下的 `worker_success` 历史行仍可读取，但不再有任何路径写入或批准它们。

自批：除根以外，gate 拒绝结果作者以及在被审批 epoch 持有过 claim 的 principal 或 account（`x_claim_holder` 追加记录，handoff 时原持有人计入新 epoch）；只创建请求的任务 owner 可以批准。根可以批准自己的工作，但必须在 gate 调用里显式给出 `self_approval=true`，否则被拒绝；这类批准在 `x_gate`（`self_approval`、`root`）和审计里如实标记，不会冒充别人的批准。因为按 account 判断，连接在 owner 账户上的 agent 也是根，同样受这条标志约束。

委托链：根委托的授权人是该 Space 的根身份；持有该 Space 中有效 `may_delegate=true` 委托的主体可以授予子委托，`parent_id` 指向它持有的那条。被委托方永远不能超过授权人（Space 相同或更窄，过期不晚于父委托），任何祖先被撤销、过期或不满足上述约束，子委托立即失效；gate 时在同一数据库事务内重新校验整条链，包括授权人必须是父委托的被委托方、链的根授权人必须正是任务所在 Space 的根身份（principal、account、kind 全部相等，所以委托不会伸进根授权人不拥有的 Space，也不会因同账户的别的主体而成立）、链最多 3 层，重放同一请求时再校验一次。创建时拒绝会形成环的委托：不能授予自己、自己上方的根或链上已有的人（按 principal 判断）。审批人角色只授予批准权，不授予读写权限，也不会让主体成为 reviewer。每次创建、撤销和 gate 都在审计中记录完整链（从叶到根的委托 ID）。

委托有两条创建路径。API 路径：Space owner 创建根委托，持有 `may_delegate` 委托的主体创建子委托，授权人就是调用者，service 一律拒绝；非 owner 且没有 `may_delegate` 委托的调用者被拒绝。运维路径：在 bridge 容器内运行 `python -m bridge.delegations`，用于 owner 尚无人类身份的阶段，记录 owner 的根委托，授权人不需要 token，但授权人必须正是所指 Space 记录的根身份，被委托方不能是授权人，且只能记录根委托，所以它受同样的规则约束。运维路径的信任边界是能进入 bridge 容器并读到应用数据库配置的人，属于已列为可信边界的 Docker 管理员；它的写入同样记入 `x_audit`，`requested_by` 是授权人，`performed_by` 是 `operator:<名称>`，名称来自命令行自报，不是认证结果。操作员能以根身份记录委托并设置根身份，这是该路径的用途，也是可信假设。被委托方必须是注册表中已连接且 account 一致的主体，防止别人事后注册同名主体继承委托。按 DECISIONS 287、291，owner 是根，南瓜持有 owner 的审批人委托。

双人确认：策略 `x_approval_policy` 是运维管理的存储配置，API 不暴露，变更只经 `python -m bridge.delegations policy-*` 并写审计（应用角色对该表有普通 DML 权限，与其他业务表一样不抵抗应用数据库凭据泄露）；任务创建者能提高要求，只有创建者是 Space owner 时才能降低，所以非 owner 的 agent 不能用任务覆盖绕过策略。要求两个批准时需要两个不同账户的审批人对同一 `result_epoch` 都通过；`root_required` 时根必须是其中之一；任何有权方的 fail 把任务退回，根的 fail 也一样；旧 epoch 的批准不计数。双人确认能防止单个被盗的委托独自完成关键任务，但它不抵抗 owner 账户本身失陷（根加一个委托审批人时，根仍是单点）。

`x_delegation` 只追加：触发器拒绝 DELETE 与 TRUNCATE，UPDATE 只允许对未撤销的行一次性写入 `revoked_at` 与 `revoked_by`；应用角色只有 SELECT、INSERT 和这两列的 UPDATE，`x_claim_holder` 只有 SELECT、INSERT。与 `x_audit` 相同，这不抵抗 PostgreSQL 超级用户、Docker 管理员或宿主失陷。委托和 gate 审计只记录标识、范围、Space、创建路径、链和计数，不含 reason 文本；reason 只存在于委托行。

## 网络与执行隔离

默认 HTTP 仅绑定回环地址；外部访问必须启用 TLS override 与有效证书。后端没有 published ports。PostgreSQL 使用单独 internal network，仅 bridge 和 migrate 连接；Baton 不执行任务，没有执行端容器、模型凭据或工具沙箱。页面 Host/Origin 白名单由 PUBLIC_URL 推导。

应用使用固定依赖、非 root、只读根、cap_drop=ALL、no-new-privileges；没有 privileged、host network 或 Docker socket。PostgreSQL 与 migrate 的 UID/GID 是 999，应用是 10001；管理初始化权限独立列出，不混入长期业务服务。

Baton 是通信与协作层，不执行任务：执行属于通过 MCP、HTTP 或适配器（如 `adapters/baton_cli.py`）连接的外部 agent，它们的运行环境、模型凭据和沙箱由各自的操作者负责，不在 Baton 的信任边界内。bridge 不对 agent 提交的结果做秘密过滤，因此客户端不应把凭据写进结果、评论或证据；服务端只靠权限模型和审计元数据约束可见范围。

这是受信任操作者的独立实验，不是已验证的敌对多租户平台；不接生产系统、生产数据或敏感源代码。浏览器只在内存中保留 bearer，渲染使用 textContent，不保存 localStorage 或记录 Authorization。

## Webhook 签名、SSRF 防护与至少一次送达

Webhook 签名密钥在订阅时由服务端以 32 字节随机数生成，只在创建响应中返回一次。它以 bytea 存在 `x_webhook`，仅为签名而保存（HMAC 需要原值，数据库备份与应用角色因此都是可信边界）；不写入 operations 重放结果、审计、日志或错误体，列表接口不返回。每次投递对原始 JSON 正文计算 HMAC-SHA256，放在 `X-Baton-Signature`；接收方必须用常数时间比较，并按 `X-Baton-Delivery` 去重。负载只含标识符等元数据，不含 prompt、评论正文或结果文本，所以 webhook 接收端泄漏也不会泄漏正文；接收方凭 ID 回来读取时仍经过 Baton 的权限检查，投递前也会重新检查收件人 view 权限。

订阅 URL 由用户提供，bridge 代为发起请求，因此有 SSRF 面。防护：只允许 https、禁止用户信息；订阅时与每次连接时都解析主机名，任一记录是环回、链路本地（含云元数据地址）、私有（RFC 1918、ULA）、组播、保留或其他非全局地址即拒绝；连接固定到通过检查的 IP 并复核实际对端，防止 DNS rebinding；不跟随重定向；10 秒超时，响应正文不读取、不记录。`BATON_WEBHOOK_ALLOW_HOSTS` 是运营者控制的精确主机名豁免，用于内部接收端或测试，不能由 API 调用者设置，也不放宽 https 要求。每个 principal 最多 10 个订阅，wait_inbox 并发有上限，限制资源占用。该防护不替代网络层出站控制：默认 Compose 的 bridge 没有出站路由，启用 webhook 须由运营者显式提供并限制出站网络。

送达语义是至少一次：重试可能使同一通知多次到达，跨通知不保证顺序，最多 8 次后 dead-letter 并写审计。逐次尝试状态保存在 `x_webhook_attempt`，终局结果写 `x_audit`，两者都不含 URL、查询串、请求头、响应或密钥。`x_notify` 与投递记录目前没有自动清理，保留与归档由运营者负责。

## 审计与诊断

业务变更及 `x_audit` 在同一 PostgreSQL 事务提交。审计只保存真实主体、操作/任务/线程标识、数据库时间、状态、版本/fence 和必要引用；不保存完整 prompt、评论正文或凭据。业务 API 只返回稳定安全错误码，不返回 SQL 或连接串。

PostgreSQL 全局关闭 SQL、慢查询采样和错误参数日志，普通 SQL 错误不记录到服务器日志；`log_min_messages=fatal` 只保留 FATAL/PANIC 诊断，错误 verbosity 为 terse。此选择牺牲详细 SQL 排障信息以避免错误消息、失败行或参数带出正文；可用性由 pg_isready、迁移结果和业务健康检查核验。含角色密码的迁移会话进一步抑制普通日志，异常只输出固定失败说明。相关参数见 [PostgreSQL 17 日志文档](https://www.postgresql.org/docs/17/runtime-config-logging.html)。

边缘、网关与 bridge 的白名单 JSONL 用于关联请求，不是防篡改账本。Docker 日志轮转；数据库和秘密卷的备份、保留、访问控制由操作者负责。`x_audit` 不能替代受控备份，`down -v` 会删除项目命名卷，不能用于自动升级或修复。

撤销 token 阻断新请求，不会取消外部 agent 已经在做的工作，也不会释放它持有的租约；租约到期后任务回到队列。手工 gate 不等同生产发布批准。

发布前执行全历史秘密扫描、私有信息扫描、SPDX 与 Signed-off-by 检查。运行回执保存在仓库外；所有贡献提交遵守 [DCO](../CONTRIBUTING.md)。
