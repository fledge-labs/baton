<!-- SPDX-License-Identifier: Apache-2.0 -->
# Codex 交接

2026-10-17。新主控以本文件、当前分支和负责人保存的私有检查点继续；Codex 已按要求停在安全点。

交接基线是 Baton `34e55c9`。另附实验 worker 的冻结源码 `7101874`，供恢复、审查和后续移植。两套实现分别验收，实验源码在 `docs/handoff/` 中归档，未接入 Baton 应用运行路径。

## 已完成

- Baton 当前代码包含 Space 共享、身份归属、提案与审阅、作用域冲突和租约 fencing、依赖图与 ready/gate、同线程消息及 HTTP/MCP 验收入口。具体契约见 [Space 规格](spec/space.md)、[API](api.md) 和 [安全模型](security-model.md)。
- 适配器白名单已集成到 `d466a38`；默认只允许 `submit_task,get_task,list_tasks,fetch_inbox`，发现与调用均受限，拒绝行为有审计。
- 带哈希依赖锁已集成到 `ca956e0`；安装保留 `--require-hashes --only-binary=:all:`。身份与凭据交付边界在 `34e55c9` 的安全说明中。
- 实验 worker 已完成安全错误分类、失败证据与同线程失败报告、资源和执行探针、容器健康检查接线、PID 余量以及 640 MiB 内存限额。相关源码进度为 `45c044f → 85e2ae4 → b8e3c32 → 453bf1e → bfd27bf → dfce8ad`。
- 最新实验源码 `7101874` 将 85% 用量判断改为 `max(0, memory.current - inactive_file)`；保留 OOM/OOM-kill 的 error、max/high 的 degraded；快照在原 memory 节点同时输出 `current/working_set/anon`，统计读取失败保持 unknown。新增 21 个参数化用例，原工作树和独立复核均为 **98 passed**。
- [冻结源码归档](handoff/lab-source-7101874.txt)包含 24 个恢复文件，已在空目录恢复、逐文件核对哈希，并离线重跑 **98 passed**。文件清单、模式及来源见 [索引](handoff/lab-source-index.json)。入口默认值已改为示例域名；依赖生成注释中的操作者路径已移除。
- 本次交接对 Baton 代码离线复核为 **74 passed / 70 skipped**。70 个需要真实 PostgreSQL 配置的测试未运行；这不构成 PostgreSQL 或部署验收通过。

## 进行中：已暂停，待新主控接手

working-set 源码已完成，线上部署验收尚未完成。

同一冻结部署脚本的三次候选启动均因新 canary 的 `CODEX_TIMEOUT` 自动回退，分别约 30.177、30.744、30.378 秒。源码、包内源码哈希、离线镜像 shadow、启动前运行契约和业务行/表结构指纹门禁均通过；这些步骤不能代替实际执行成功。

安全点已确认：三次尝试均为 `ROLLED_BACK`，旧 worker 镜像已恢复；bridge、worker、邮件服务、gateway 都在运行且没有 paused 容器，worker 的资源与 canary 状态均为 healthy。内存限额仍为 640 MiB。回退没有覆盖业务数据库，也没有调整鉴权、30 秒探针预算、CPU、PID 或沙箱策略。

本地公网采样已关闭，交接后 Codex 不再发部署、诊断或模型请求。服务自身已有的后台监控照常运行。

## 未开始

- 最新 working-set 版本部署成功后的同一份 **253 行真实审查**。当前没有该版本的成功业务任务或对应完整峰值结论。
- 新版本正式的 `current / working_set / anon` 峰值、事件终值及完整分段公网统计验收。
- 将实验 worker 的错误/健康模块正式移植到 Baton 应用；需按 Baton 的现有架构重新审查。
- 放宽启动 readiness 的例外。曾讨论过对单一 `CODEX_TIMEOUT` 明确标记不健康部署的窄例外，但**未实施、未获部署审批**。旧脚本审批不能用于新契约。
- 真实签署人确认与正式上游贡献准备。历史实验提交使用占位签署，不能据此声称真实 DCO 已核验；当前归档用于交接审阅，不代表可直接合并或发布。

## 已知问题与证据边界

1. **启动执行超时的具体慢因未记录。** 旧镜像的新启动也复现过超时；其后一个旧版控制 canary 用约 29.768 秒完成。允许读取的 CLI 日志表总行数为 0，不能归因鉴权、网络或模型容量。当前实例短窗口没有持续 CPU 节流，也不能倒推已销毁实例。不要继续无证据重试或改大时限取得绿色结果。
2. 超时探针期间资源采样保持 healthy；其中一次完整探针窗口有 599 条采样，working set 峰值约为限额的 34.86%。回退删除 cgroup 后，采样器缺少 summary/事件终值，不能把缺失值记成零。
3. 先前的 **88/88 healthy、零切换**只适用于从重启后 **51.305 秒**开始的公网窗口，未覆盖启动阶段。约 38 秒 503 来自后续复核补充；旧文件没有该段逐次 HTTP 原始记录。资源快照约在启动后 39.65 秒首次 healthy。
4. 先前峰值同刻的主机可用内存为 **632,041,472 字节**，也是当次窗口最小值；更早的 pre trace 没有这项。未来提高内存限额前，应重新确认宿主机余量。
5. 本轮维护前启动的公网采样包含三次尝试与回退，共 **1013 次**：200 为 **498** 次、503 为 **505** 次、无 HTTP 响应为 **10** 次；healthy/非 healthy 切换 **4** 次，HTTP 状态码变化 **23** 次。统计窗口为 04:44:59.817–05:02:06.893 UTC。没有最新版本的审查阶段，不能从这些数字得出审查通过。
6. 原始运行回执、配置、数据库、真实身份、凭据及凭据摘要留在私有归档。仓库只保存源码恢复材料和本交接摘要。历史 lane 工作树保留，接手以当前基线和已集成提交为准，避免直接合并陈旧分支。

私有工作区的相对索引是 `.codex/artifacts/baton-space/HANDOFF.md`；对应归档名为 `worker-failure-f5ad`、`worker-memory-review`、`worker-working-set`。其中包含原始 trace、探针表、审计白名单副本、部署脚本、三次回退和安全停止记录。由负责人按现有私有渠道交付访问方式，继续使用自有或专用测试身份验证。

## 怎么跑测试

### Baton 应用

运行依赖按 README 准备，测试工具使用独立受控环境。本次工具环境为 Python 3.12.13、pytest 9.1.1、pytest-asyncio 1.4.0；交接时没有升级依赖。

```sh
# SPDX-License-Identifier: Apache-2.0
python3.12 -m venv .venv
.venv/bin/python -m pip install --require-hashes --only-binary=:all: -r deploy/requirements.txt

# TEST_PYTHON 指向已准备好 pytest/pytest-asyncio 的绝对路径。
"$TEST_PYTHON" -m pytest -q
python3 scripts/check_public_tree.py
```

未设置数据库配置时，PostgreSQL 集成部分会 skip。完整运行需要独立测试数据库的两个配置文件；会修改测试数据，不能使用生产配置：

```sh
# SPDX-License-Identifier: Apache-2.0
BATON_TEST_DATABASE_CONFIG="<TEST_APP_CONFIG_FILE>" \
BATON_TEST_DATABASE_ADMIN_CONFIG="<TEST_ADMIN_CONFIG_FILE>" \
  "$TEST_PYTHON" -m pytest -q
```

HTTP/MCP、Space 和真实 Codex 的验收步骤分别见 [README](../README.md)及 `scripts/space_acceptance.py`、`scripts/assignment_acceptance.py`、`scripts/e2e.py`。真实 Codex 模式会调用模型；本次交接没有运行它们。保留稳定业务键，重复键应返回 replay。

### 恢复实验 worker 源码并运行 98 项测试

在空目录恢复冻结源码，不在 Baton 应用目录应用。以下只恢复源码；私有入口、秘密卷、注册表和真实回执由负责人单独管理。

```sh
# SPDX-License-Identifier: Apache-2.0
BATON_REPO="$(pwd)"
mkdir -p "$HOME/.local/state/baton"
RECOVERY_DIR="$(mktemp -d "$HOME/.local/state/baton/recovery.XXXXXX")"
python3 "$BATON_REPO/docs/handoff/restore_lab_source.py" --destination "$RECOVERY_DIR"
(
  cd "$RECOVERY_DIR"
  "$TEST_PYTHON" -m pytest -q
)
```

恢复环境需要相应运行依赖及上述测试工具。索引包含每个文件的 SHA-256 和模式；依赖声明及安全策略的来源说明随归档保存。该历史实验的依赖安装流程不替代 Baton 的哈希锁要求。正式移植前继续执行公开数据、完整历史秘密、SPDX 和签署检查。

接手的第一步是核对私有安全停止记录与实际线上旧版本，再决定是否继续诊断执行超时、重新审批部署契约和完成真实审查。Codex 在此停止。
