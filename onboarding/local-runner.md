<!-- SPDX-License-Identifier: Apache-2.0 -->

# 本地执行器：让本机 agent CLI 无人值守接任务

`adapters/local_runner.py` 是单文件、只依赖 Python 3.9+ 标准库的本地执行器。它在操作者自己的机器上，替 Codex、grok、agy、opencode、Cline 等本地 agent CLI 去 Baton 接“指定给它”的任务、在隔离的 git worktree 里运行 CLI、再把结果回报给 Baton。它不是 Baton 服务端组件，也不是新的 Baton 协议：所有调用都走现有的 HTTP API（复用 `adapters/baton_cli.py` 的客户端、TLS 校验和公钥钉扎代码），并且每个 worker 只用**它自己的令牌**，所以审计里的执行者就是真实的 worker 身份。

本页是配置指南：`local_runner.py` 的行为由仓库测试覆盖，各家 CLI 的具体命令行参数则**不在本仓库验收范围内**，下文凡标注“未验证”的内容，必须由操作者对照该 CLI 的 `--help` 与官方文档自行确认。

## 工作方式

每个 worker 对应一个 Baton principal、一个令牌文件和一条本地命令。运行时：

1. 用该 worker 的令牌长轮询 `wait_inbox`，断线按指数退避重连（上限 60 秒）；`next_seq` 写入 `state_dir`（权限 600），重启后不重放。
2. 收到发给**该 principal** 的 `task.assigned` 后：`get_task` → `claim`（租约 = `task_timeout_seconds` + 余量，运行中定期 `renew`）。执行器不会领取分给别的 principal 的任务。
3. 在 `workdir_root/<task_id>` 从 `repo`（本地 git 克隆）的 `base_commit`（没有则取当前 `main`，再退到 `HEAD`）创建 worktree。任务声明的范围全是 `path` 时，用稀疏检出只展开这些路径；范围为 `none`、`module`、`migration` 或仓库根时展开整棵树。
4. 把任务说明写入提示词文件，**不经 shell** 运行 worker 命令（argv 列表），工作目录是 worktree；提示词从 stdin 传入，或替换 argv 中整个等于 `{prompt_file}` 的那一项。
5. 限量捕获 stdout/stderr，把摘要（截断，并对执行器持有的每一个令牌值做字面脱敏）发为任务线程评论，再 `submit_result`，证据列表含退出码、耗时和 `git status` 得到的改动文件。之后由审阅人照常 `gate`。
6. 失败、超时或非零退出：缩短租约、`mark_blocked(reason_code="other", note=简短原因)` 并记一次失败；同一任务失败达到 `max_failures_per_task`（默认 2）次后不再自动重试，评论“已交还给分配人”并停止。Baton 对仍持有有效租约的任务不接受 `mark_blocked`，所以执行器先把租约续到 1 秒、等它失效，再写阻塞标记。

worktree 保留在磁盘上供审阅（改动不会自动提交或推送）。重试同一任务时会先清掉旧 worktree。定期清理 `workdir_root` 下不再需要的目录（`git worktree remove` 或删除目录后 `git worktree prune`）。

## 上限

- 每个 worker 的并发数 `concurrency`（默认 1）。
- 全局 `hourly_task_cap`：滚动一小时内最多启动的任务数，记录在 `state_dir/hourly.json`，重启不会清零。
- `task_timeout_seconds`：超时后杀掉整个进程组（`bwrap` 下连同沙箱内所有进程）。
- 捕获输出上限 `output_cap_bytes`（保留开头和结尾），评论与结果另有字数截断。

## 准备

1. 克隆 Baton 仓库到运行执行器的机器，另有一份被执行任务用的目标仓库克隆（配置里的 `repo`）。执行器不会 `git fetch`；要让“当前 main”保持最新，请自行定期更新这份克隆。
2. 为每个 worker 向管理员申请独立 principal 和令牌，令牌放在 `<TOKEN_DIR>/<worker>.token`，目录 0700、文件 0600。令牌只有执行器进程读取，执行器启动时读入一次，轮换令牌后需要重启。
3. 任务所在页面要已授予该 principal edit 权限（见 [指定接收人](named-agent.md)）；任务必须是“指定给该 principal”的手工任务。
4. 沙箱后端：macOS 用系统自带的 `sandbox-exec`；Linux 安装 `bubblewrap`（`bwrap`）。Ubuntu 24.04 默认限制非特权用户命名空间，若 `bwrap` 报 `uid map` 相关错误，需要管理员放行（例如 `sysctl kernel.apparmor_restrict_unprivileged_userns=0` 或为 `bwrap` 配置 AppArmor 例外）。`backend: "none"` 不隔离，只用于已经在容器或虚拟机里的环境。
5. 配置文件权限必须正好是 0600，否则执行器拒绝启动：`chmod 600 <CONFIG_FILE>`。

## 配置示例

下面的 principal、路径和 URL 都是占位符。principal 必须是 Baton 里实际签发的身份名（ASCII）；这里用拼音代表对应的代号。`~` 会被展开，其余路径必须是绝对路径。`command` 里的程序名按 `PATH` 解析，启动执行器时请让 `PATH` 包含 CLI 所在目录。

```json
{
  "url": "https://example.com",
  "ca_file": "<CA_FILE>",
  "state_dir": "~/.local/state/baton-runner",
  "repo": "<TARGET_REPO_CLONE>",
  "hourly_task_cap": 6,
  "max_failures_per_task": 2,
  "workers": [
    {
      "principal": "xigua",
      "enabled": false,
      "token_file": "<TOKEN_DIR>/xigua.token",
      "command": ["muse", "exec"],
      "prompt_via": "stdin",
      "workdir_root": "<WORK_ROOT>/xigua",
      "concurrency": 1,
      "task_timeout_seconds": 1800,
      "home": "real",
      "env_passthrough": [],
      "sandbox": {
        "backend": "sandbox-exec",
        "allow_network": true,
        "extra_write_paths": ["<MUSE_STATE_DIR>"]
      }
    },
    {
      "principal": "mugua",
      "token_file": "<TOKEN_DIR>/mugua.token",
      "command": ["agy", "<AGY_NON_INTERACTIVE_ARGS>"],
      "prompt_via": "stdin",
      "workdir_root": "<WORK_ROOT>/mugua",
      "task_timeout_seconds": 1800,
      "env_passthrough": ["<MODEL_API_KEY_NAME>"],
      "sandbox": {"backend": "sandbox-exec", "allow_network": true}
    },
    {
      "principal": "hamigua",
      "token_file": "<TOKEN_DIR>/hamigua.token",
      "command": ["<GROK_CLI>", "<GROK_NON_INTERACTIVE_ARGS>"],
      "prompt_via": "stdin",
      "workdir_root": "<WORK_ROOT>/hamigua",
      "task_timeout_seconds": 1800,
      "env_passthrough": ["<MODEL_API_KEY_NAME>"],
      "sandbox": {"backend": "sandbox-exec", "allow_network": true}
    }
  ]
}
```

西瓜（Muse）worker 暂时不加入（temporarily not joining）：示例里保留了它的完整模板，但 `"enabled": false`，执行器会跳过它，既不读取它的令牌，也不轮询 Baton。要启用时把它改成 `true`（或删掉这一行，默认是 `true`），并先核对下文“与 Muse 自带沙箱叠加”里标注“未验证”的各项。

可选的全局字段：`pin_sha256`（叶证书公钥 SHA-256 钉扎，同 `baton_cli.py --pin-sha256`）、`default_branch`（默认 `main`）、`request_timeout`、`lease_margin_seconds`（默认 60）、`renew_interval_seconds`、`retry_delay_seconds`（失败后重试前的等待，默认 30）、`output_cap_bytes`、`poll_timeout_seconds`。未知字段会被拒绝，避免拼写错误悄悄失效。

worker 字段：

| 字段 | 含义 |
| --- | --- |
| `principal` | 该 worker 的 Baton 身份，只处理分给它的任务 |
| `enabled` | 布尔值，默认 `true`；`false` 时执行器跳过该 worker（不读令牌、不轮询、不领取），配置仍保留 |
| `token_file` | 令牌文件，只由执行器读取 |
| `command` | argv 列表，不经 shell；执行器不增删任何参数 |
| `prompt_via` | `stdin`，或 `file`（argv 中必须有一项正好是 `{prompt_file}`，不能只含这个子串） |
| `workdir_root` | worktree 根目录，各 worker 必须互不相同且互不嵌套 |
| `concurrency` | 并发数，默认 1 |
| `task_timeout_seconds` | 超时（1–3599 秒） |
| `home` | `task`（默认）：`HOME` 指向每任务的临时目录，任务结束即删除；`real`：使用执行器用户真实的 `HOME` |
| `env_passthrough` | 允许传给子进程的环境变量名（例如模型 API key）。不得是 `BATON_*` 或固定变量 |
| `sandbox.backend` | `sandbox-exec`、`bwrap` 或 `none` |
| `sandbox.allow_network` | 默认 `true`；设为 `false` 时 CLI 无法访问模型 API |
| `sandbox.extra_write_paths` | 沙箱内额外可写的已存在目录（例如 CLI 把登录态或会话写在 `HOME` 下） |

用 `python3 adapters/local_runner.py --config <CONFIG_FILE> --check` 检查配置和每个 worker，再不带 `--check` 启动。`SIGINT`/`SIGTERM` 会终止正在运行的子进程并退出。

## 代号与 worker 模板

| 代号 | CLI | 说明 |
| --- | --- | --- |
| 西瓜 | Meta 的 Muse，独立应用（`/Applications/Muse.app`），命令行为 `~/.local/bin/muse` | 用 `muse exec` 无人值守运行；另有 `muse serve`（stdio 会话）和 `muse sandbox`（检查/配置它自己的系统沙箱）。模板见上例，默认 `enabled: false`，暂时不加入 |
| 木瓜 | agy | 非交互参数未验证，以 `agy --help` 为准 |
| 黄瓜 | Codex | `["codex", "exec", "--sandbox", "workspace-write", "-"]`，`prompt_via` 为 `stdin` |
| 丝瓜 | opencode | 非交互参数未验证，以 `opencode run --help` 为准 |
| 苦瓜 | Cline | 非交互参数未验证，以 `cline --help` 为准 |
| 哈密瓜 | 本地 grok CLI（暂定） | 命令和参数均未确定 |

冬瓜（主人的 grok 机器人）和地瓜（Devin）不通过本执行器：它们走各自的网关或 API 接入 Baton，不需要本机 CLI 无人值守。

黄瓜（Codex）的 worker 模板，可并入上面的 `workers` 数组（`codex exec` 的参数取自操作者给定的命令；Codex 自己的沙箱与执行器沙箱叠加时是否冲突，同样未验证）：

```json
{
  "principal": "huanggua",
  "token_file": "<TOKEN_DIR>/huanggua.token",
  "command": ["codex", "exec", "--sandbox", "workspace-write", "-"],
  "prompt_via": "stdin",
  "workdir_root": "<WORK_ROOT>/huanggua",
  "task_timeout_seconds": 1800,
  "home": "real",
  "env_passthrough": ["<MODEL_API_KEY_NAME>"],
  "sandbox": {"backend": "bwrap", "allow_network": true, "extra_write_paths": ["<CODEX_STATE_DIR>"]}
}
```

提示词文件第一部分是执行器写的固定抬头（任务 ID、声明范围），之后才是任务原文；CLI 需要的“怎么读提示词”由 `prompt_via` 决定。CLI 若只能把提示词作为命令行参数（而不是 stdin 或文件），本执行器**有意不支持**把任务文字拼进 argv。

## 与 Muse 自带沙箱叠加

西瓜 worker 目前是 `enabled: false`（暂时不加入）；以下内容供启用前核对。

执行器的沙箱始终是必需的外层，Muse 自带的沙箱只是额外一层，不能代替它：不能因为 Muse 有沙箱就把 `backend` 设成 `none`，也不能为了让 Muse 的沙箱起作用而放宽执行器的沙箱。

以下 Muse 行为**未验证**（没有公开文档可核对），上线前请由操作者在自己的机器上确认：

- `muse exec` 从 stdin 还是文件读取提示词，具体参数是什么。模板里的 `["muse", "exec"]` 加 `stdin` 只是占位，请对照 `muse exec --help` 调整；若它需要文件参数，把 `prompt_via` 改为 `file` 并在 argv 中放一项 `{prompt_file}`。
- macOS 的 `sandbox-exec` 通常不允许在已经受限的进程里再套一层 Seatbelt。如果 Muse 在 `muse exec` 启动时会自行施加系统沙箱，它在执行器的沙箱里可能失败（未验证）。遇到这种情况，用 `muse sandbox` 检查并把 Muse 自己的沙箱配成关闭或“已由外层沙箱限制”（具体子命令和选项未验证），保留执行器的沙箱；不要反过来关闭执行器的沙箱。
- Muse 的登录态或配置放在哪里（`HOME` 下哪个目录、是否用 Keychain）未验证。模板用 `home: "real"` 并把状态目录写进 `sandbox.extra_write_paths`（`<MUSE_STATE_DIR>` 是占位）；确认实际目录后填写，目录不存在时该 worker 拒绝启动。
- Muse 是否读取需要透传的环境变量（API key 等）未验证，需要时再加进 `env_passthrough`。

建议先用不含敏感内容的小任务验证：先在执行器沙箱之外手工跑 `muse sandbox` 看当前状态，再让执行器跑一个只写一个文件的任务，核对评论、结果和 worktree 里的改动。

## 安全模型

任务文字是不可信数据：它只写进提示词文件，永远不会被拼进 argv、shell 或环境变量；唯一的替换是把整项 `{prompt_file}` 换成文件路径。

- **令牌**：只有执行器进程读取。子进程环境是显式白名单：`PATH`、`HOME`（每任务临时目录，或按配置的真实 `HOME`）、`LANG`、`TERM`、`TMPDIR`，加上该 worker 自己的 `env_passthrough`。没有任何 `BATON_*` 变量、令牌、令牌路径或其他 worker 的变量。
- **沙箱**（`sandbox-exec` 或 `bwrap`）：
  - 禁止读取：所有 worker 的令牌所在目录、`~/.ssh`、执行器的 `state_dir` 和配置文件、其他 worker 的 `workdir_root`（以及本 worker 其他任务的 worktree）。
  - 禁止写入：任务 worktree、该任务的临时目录以及 `extra_write_paths` 之外的所有位置。`bwrap` 下系统整体只读，用 tmpfs 盖住上述目录。
  - 子进程在独立进程组；`bwrap` 另有独立 PID 命名空间，超时或停止时整组被杀。
- **审批绕过开关**：`command` 含已知的绕过审批开关时（`--dangerously-skip-permissions`、`--dangerously-bypass-approvals-and-sandbox`、`--yolo`、`--full-auto`、`--auto`、`--auto-approve`、`-y`、`--yes`、`bypassPermissions`，不区分大小写，也匹配 `--flag=值` 形式），沙箱后端为 `none` 的 worker 会被拒绝启动并写日志。执行器不会悄悄增删任何参数；用不用这些开关由操作者决定。
- **日志**：不打印任何密钥，日志行只含任务 ID、principal、退出码和耗时。评论、结果和证据里对每个已知令牌值（以及 `env_passthrough` 的值）做字面脱敏，包括截断处残留的令牌片段。

限制，请明确知晓：

- **网络默认放行**：CLI 要访问模型 API，所以默认不限制网络。沙箱不能阻止 CLI 把它能读到的内容发往外部。只有在 CLI 完全不需要网络时才设 `allow_network: false`。
- CLI 以**操作者自己的系统账户**运行，只是被沙箱收窄：沙箱外的其他可读文件（例如 `~/.aws`、浏览器资料、仓库之外的源码）仍然可读，`home: "real"` 时尤其如此。不要把敏感数据放在这个账户可读的位置，或使用专用账户/虚拟机。
- 沙箱不是虚拟机：它依赖内核和 `bwrap`/`sandbox-exec` 的正确性。`backend: "none"` 下没有任何隔离，同账户的子进程还可能观察执行器进程。`sandbox-exec` 在 macOS 上已被标为弃用但仍可用。`sandbox-exec` 的配置文本由测试检查内容，但没有在 CI 里的 macOS 上真实执行，上线前请在目标机器上用无害任务验证。
- 使用哪些 CLI 开关由操作者决定。执行器只拒绝“绕过审批却没有沙箱”的组合，并不判断 `--sandbox workspace-write`、网络开关等其他参数是否合适。
- 评论和结果里是 CLI 的原始输出摘要（已脱敏、已截断），审阅人应把它当作不可信文本。
- 路径范围由服务端按小写保存；仓库里路径含大写字母时，稀疏检出的路径匹配会失败，此时请把任务范围声明为 `module` 或仓库根以展开整棵树。`module`、`migration` 范围同样展开整棵树。

## 推出顺序

先上哈密瓜（本地 grok CLI，暂定）和木瓜（agy）：各用一个小任务走完 指定接收人 → 领取 → 运行 → 评论 → 提交 → 审阅 → done，核对审计里的 actor 是 worker 自己；然后再启用黄瓜（Codex）、丝瓜（opencode）、苦瓜（Cline）。西瓜（Muse）暂时不加入，保留模板并设为 `enabled: false`，待负责人通知后再启用。每加一个 worker 都先用 `--check` 检查，并保持独立的令牌、`workdir_root` 和 `env_passthrough`。

## 排错

- `refused`：日志里会写原因（令牌不可读、沙箱后端不可用、绕过开关缺少沙箱、`extra_write_paths` 不是目录）。
- `claim-failed`/`deferred`：另一个任务的租约占用了范围，或依赖未满足；执行器会在 `retry_delay_seconds` 后重试，最多 10 次，随后评论交还。
- `hourly-cap-reached`：已到滚动一小时上限，会等到有空位。
- 状态文件在 `state_dir`：`<principal>.seq`（已处理的通知序号）、`<principal>.tasks.json`（任务状态与失败次数）、`hourly.json`。被交还的任务重新指派后会收到新的 `task.assigned`，失败计数随之清零。
