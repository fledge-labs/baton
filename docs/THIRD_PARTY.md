<!-- SPDX-License-Identifier: Apache-2.0 -->

# 第三方组件

Baton 自有代码和原创配置示例使用 [Apache-2.0](../LICENSE)。第三方组件保留各自的许可证；Baton 的许可证不会将这些组件重新许可。本清单记录直接集成的主要组件，不是镜像内全部系统包和传递依赖的完整 SBOM。

依赖版本及容器镜像摘要以 [`deploy/versions.json`](../deploy/versions.json) 和相应构建文件为准。下列公开的上游版本、commit 和镜像摘要用于固定依赖，不是任何部署凭据、token 摘要或运行回执。第三方源码及其许可证文件不 vendor 到本仓库；唯一随仓库保存的第三方派生配置是下述 Moby seccomp profile。

## 固定依赖

- **agentgateway `v1.6.0`**：MCP/A2A 网关；SPDX `Apache-2.0`。[该版本 LICENSE](https://github.com/agentgateway/agentgateway/blob/v1.6.0/LICENSE)。镜像固定为 `sha256:9d3e6044ddcdc0878b1787f77bd401252b95e22684203fb5e874c4c42d2ed90c`，完整镜像引用见版本清单。
- **A2A Python SDK `a2a-sdk==1.2.2`**：A2A 协议实现；SPDX `Apache-2.0`。[`v1.2.2` LICENSE](https://github.com/a2aproject/a2a-python/blob/v1.2.2/LICENSE)。这里列的是实际使用的 Python SDK，不是用其他 A2A 实现的许可证代替。
- **OpenAI Codex CLI `0.154.0`**：工作进程；SPDX `Apache-2.0`。[`rust-v0.154.0` LICENSE](https://github.com/openai/codex/blob/rust-v0.154.0/LICENSE)。模型服务的访问权限和条款独立于 CLI 代码许可证。
- **MCP Python SDK `mcp==1.30.0`**：stdio 适配器及 MCP 协议支持；SPDX `MIT`。[`v1.30.0` LICENSE](https://github.com/modelcontextprotocol/python-sdk/blob/v1.30.0/LICENSE)。

## PostgreSQL 与驱动

- **PostgreSQL `17.11`**：数据库；SPDX `PostgreSQL`。采用 [PostgreSQL 官方许可证](https://www.postgresql.org/about/licence/)，对应源码的 [REL_17_11 COPYRIGHT](https://github.com/postgres/postgres/blob/REL_17_11/COPYRIGHT)。服务使用 Docker Official Images 的 `postgres:17.11-bookworm`，并固定多架构 index digest，完整引用见版本清单；[官方镜像构建来源](https://github.com/docker-library/postgres/tree/2603e26e245e558218728ee14e0a42dcb020dc7f/17/bookworm)。镜像脚本采用其自己的 [MIT LICENSE](https://github.com/docker-library/postgres/blob/2603e26e245e558218728ee14e0a42dcb020dc7f/LICENSE)。
- **Psycopg `psycopg[binary]==3.3.6` 与 `psycopg-binary==3.3.6`**：PostgreSQL Python 驱动；包的 license expression 为 SPDX `LGPL-3.0-only`。[固定版本 LICENSE.txt](https://github.com/psycopg/psycopg/blob/3.3.6/LICENSE.txt)、[psycopg 包元数据](https://pypi.org/project/psycopg/3.3.6/)、[binary 包元数据](https://pypi.org/project/psycopg-binary/3.3.6/)。两者都在 requirements 中精确固定。二进制 wheel 内含 libpq 等库，它们保留各自许可证；再分发镜像需保留 wheel 的相关声明，不能将其重新许可为 Apache-2.0。

驱动按上游标准 Python 包方式动态加载，不 vendor 或修改其源码。Baton 使用 LGPL 组件不改变 Baton 自有代码的许可证，也不免除分发方遵守第三方条款的义务；上游 [安装文档](https://www.psycopg.org/psycopg3/docs/basic/install.html) 说明 binary 分发方式。

## 随仓库保存的派生配置

[`deploy/security/codex-seccomp.json`](../deploy/security/codex-seccomp.json) 基于 Moby 项目贡献者的 [profiles 仓库 `seccomp/default.json`](https://github.com/moby/profiles/blob/4b8214fc4971e06172938a1f1554ccff6647ee80/seccomp/default.json)，固定基线为 `4b8214fc4971e06172938a1f1554ccff6647ee80`，SPDX `Apache-2.0`，原作者为 Moby 项目贡献者；[基线 LICENSE](https://github.com/moby/profiles/blob/4b8214fc4971e06172938a1f1554ccff6647ee80/LICENSE)。

Baton 对这份配置追加 `clone`、`unshare`、`mount`、`umount2`、`pivot_root`、`setns` 六个系统调用的允许规则，以支持 Codex 工作进程内部的隔离启动。它是经过修改的派生配置，不能当作未修改的 Moby 默认策略。文件保留来源与 Apache-2.0 标识；Baton 没有复制第三方运行时代码。

## 运行时与基础镜像

- **Python 3.12**：主要许可证为 SPDX `PSF-2.0`；历史许可和内含组件还带有其他条款。[Python 3.12 许可证与声明](https://docs.python.org/3.12/license.html)。
- **Node.js 22**：Node.js 自有部分为 SPDX `MIT`，其分发还包括其他组件的许可证与声明。[Node.js 22 LICENSE](https://github.com/nodejs/node/blob/v22.x/LICENSE)。
- **nginx**：上游服务器代码为 SPDX `BSD-2-Clause`。[nginx LICENSE](https://nginx.org/LICENSE)。所选镜像版本见版本清单。

上述运行时通过固定 digest 的基础镜像提供，具体 patch 版本由所选镜像决定。基础操作系统、容器构建工具及传递依赖分别保留其原许可证；再分发构建后的镜像时，需要核对镜像内对应版本的声明。本仓库没有把全部镜像内容重新许可为 Apache-2.0。

## 可选客户端

[onboarding](../onboarding/) 说明 Codex、Antigravity CLI、Cline、Devin CLI、Claude Code 和 OpenCode 如何连接 Baton。这些客户端由使用者独立安装；除上述工作进程使用的 Codex CLI 外，Baton 不分发它们，也不固定它们的版本、模型权限或服务条款。接入示例不表示厂商背书，也不是新仓库内逐一完成原生客户端测试的证明。
