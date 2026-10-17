<!-- SPDX-License-Identifier: Apache-2.0 -->
# 架构与范围

nginx 提供静态看板与反向代理；agentgateway 校验 bearer、区分 client/dispatcher，转发 MCP HTTP 并记录 A2A 协议元数据。应用继续使用官方 MCP Python SDK 和 A2A Python SDK。空间产品语义及未验证范围见 [规格](spec/space.md)；`baton.space.v1` HTTP/JSON、表结构和全部 `x_` 协作协议属于 Baton，不是官方 Space API。

## 服务与权限

普通 Compose 启动只有 `initialize` 构建应用镜像。该离线管理进程创建命名卷中的秘密文件；`database` 随后启动固定版本 PostgreSQL，`migrate` 复用应用镜像执行单事务 schema 初始化及授权。bridge 必须等待迁移成功；业务进程不会在收到请求时执行 DDL。ops 下的 `admin` 可单独构建，用于新 clone 初始化和凭据管理。

数据库与迁移器使用 UID/GID 999；bridge/worker 使用 10001。数据库仅位于独立 internal 网络，bridge 和 migrate 是其客户端；没有发布数据库端口。worker 只接入任务服务网络及自身 egress，不持数据库密码。TLS override 仅改变 edge 的公开入口。

参考 D5 机型（2 vCPU、约 3.9 GiB 内存）上空闲 Compose 栈约 164 MiB 的观测值，codex 服务默认 `CODEX_MEM_LIMIT=640m`（`mem_limit` 与 `memswap_limit` 相同，禁止额外 swap）。worker 健康采样把 cgroup `memory.max` 暴露在 `/healthz` 的 `resources.memory.limit` 中；超限会由内核终止 Codex CLI，任务以 `WORKER_MEMORY_EXHAUSTED` 失败，而不是拖垮同机其他服务。可按模型与并发在 `.env` 中调高该上限。

`baton_bootstrap` 的文件凭据只在 database/migrate 的管理秘密卷；它是运维 DBA，不能视作业务主体。schema、表和触发器由 `baton_owner NOLOGIN` 拥有。bridge 只拿 `baton_app` 的 JSON 配置：`host`、`port`、`dbname`、`user`、`password`；它不是 schema/table owner，没有创建 schema、管理角色、继承 owner 或变更审计触发器的权限。

## 数据与执行

PostgreSQL 的 `baton` schema 保存 spaces、pages、blocks、分享、归属、评论、mentions、任务、claims、幂等记录、gate 和 `x_audit`。版本记录只读提供给应用。任务与报告线程共享 UUID；评论回报与 mentions/assignments 组成兼容 `fetch_inbox`。业务效果、幂等回放结果和审计在同一数据库事务提交。

自动任务由 bridge 派发，经内部 gateway 的 A2A 路由送给 worker。worker 保留独立的 SQLite A2A task store 和执行去重回执；真实 Codex 与 demo backend 在结果和审计中明确区分。完成候选必须经过配置的 gate 才能成为 done；兼容视图才将 done 映射为 completed。手工领取不自动进入 worker 队列。

worker 的失败由封闭词汇（`bridge/failures.py`）分类。worker 报告的失败由 bridge 以错误码写入任务 `error`、状态置为 failed，并在任务线程中以评论回报，与完成回报一致；结果不确定时仍保持 interrupted。worker 同时运行 `bridge/worker_health.py`：每 30 秒采样 cgroup v2 资源，每 15 分钟经与业务任务相同的执行路径和执行锁运行 canary，探针账本保存在 worker 的 SQLite；`/healthz` 返回快照（含最近一次 canary 的各阶段耗时）。demo backend 不调用模型，其 canary 只是合成检查，快照以 `backend` 字段标明。gateway 只以 `service_started` 依赖 codex：冷启动 canary 在 30 秒预算内超时是已知情况，若让 gateway 等待 `service_healthy`，一次超时就会阻止整个栈启动；worker 自身的严格健康检查不变，因此 Docker 与 `compose up --wait` 仍如实报告 worker 不健康。每次 Codex 执行的阶段耗时（spawn、首个 `--json` 事件、首个 `item.*`、turn 结束、进程退出）记录在 `executions` 与 `health_probes` 行的 `phases` 列。

租约与 fence 以数据库时间和事务锁为权威；`x_scope` 统一 global namespace，覆盖同一 PostgreSQL 权威中的跨 Space 冲突；path 声明按目录前缀判定重叠，不解析符号链接等别名（见 spec X10/A17）。交接在事务内保留 base/context/evidence/thread，并使旧 fence 失效。它不提供跨独立数据库的分布式锁，也不承诺 PostgreSQL 与外部 Codex 进程的 exactly-once。

## 运维范围

`x_audit` 的业务记录只允许应用追加/读取，并由所有者触发器拒绝修改、删除和清空。请求/worker JSONL 另用于连接入口 request ID、task ID、A2A ID、backend；这些文件不提供抗管理员篡改保证。数据库超级用户和 Docker 管理员属于可信边界。

本次替换只用于全新命名卷实例，不自动迁移旧 SQLite 或旧消息数据，不连接真实账户、ChatGPT Space 或既有私有部署。正式 schema 升级、备份恢复与生产迁移需要单独的版本及验收过程。
