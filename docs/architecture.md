<!-- SPDX-License-Identifier: Apache-2.0 -->
# 架构与范围

nginx 提供静态看板与反向代理；agentgateway 校验 bearer、只放行 client 角色，转发 MCP 与 HTTP 请求并记录请求元数据。应用使用官方 MCP Python SDK。Baton 是通信与协作层，不执行任务：执行属于通过 MCP、HTTP 或适配器（`adapters/`）连接的外部 agent。空间产品语义及未验证范围见 [规格](spec/space.md)；`baton.space.v1` HTTP/JSON、表结构和全部 `x_` 协作协议属于 Baton，不是官方 Space API。

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

`x_audit` 的业务记录只允许应用追加/读取，并由所有者触发器拒绝修改、删除和清空。边缘、网关与 bridge 的 JSONL 另用于连接入口 request ID 与 task ID；这些文件不提供抗管理员篡改保证。数据库超级用户和 Docker 管理员属于可信边界。

本次替换只用于全新命名卷实例，不自动迁移旧 SQLite 或旧消息数据，不连接真实账户、ChatGPT Space 或既有私有部署。正式 schema 升级、备份恢复与生产迁移需要单独的版本及验收过程。
