<!-- SPDX-License-Identifier: Apache-2.0 -->
# 架构与范围

入口 nginx 提供静态看板和反向代理；agentgateway 负责 bearer 校验、角色授权、MCP HTTP 转发和 A2A 协议日志。应用使用官方 MCP Python SDK 与官方 A2A Python SDK，不重新实现协议栈。

任务桥把认证身份绑定到私有的 Agent Mail 身份和 registration token。客户端不能传入别人的 sender；任务查询也按认证主体隔离。任务标识由主体和稳定幂等键确定；相同键改变输入会被拒绝。

队列采用 SQLite，单个 bridge 进程串行派发。派发通过内部 gateway 的 A2A 路由调用 worker，worker 先持久记录执行状态，再执行固定版本 Codex CLI。demo 模式明确标记并返回固定演示结果；两种 backend 均由审计和 A2A artifact 元数据区分。

真实结果回写任务数据库，并通过 Agent Mail 发送同 thread 的完成消息。执行完成和邮件投递状态独立展示。客户端依次调用 submit_task、get_task、fetch_inbox；不得只看 HTTP 200 或 worker 存活就认为任务成功。

审计关联入口 request ID、任务 ID、A2A task ID、邮件 ID。请求重放不新建执行，重启后的不确定状态不自动补跑，以避免重复外部效果。没有跨任务库、A2A 库、Mail 库的分布式事务。

当前范围是可复现的独立协作实验。多执行者 claim/lease、文件重叠检查、合并审批、集群调度和生产高可用不在本次迁移内。应用不连接已有私有部署；新实例必须使用新的卷和凭据。
