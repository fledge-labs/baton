<!-- SPDX-License-Identifier: Apache-2.0 -->
# 安全模型

## 凭据与身份

客户端令牌使用系统随机数生成，只有独立文件保存原值；gateway 与应用注册表只保存 SHA-256。SHA-256 摘要也属于运行状态，不能提交。每个请求在 gateway 和应用两层校验，client 与内部 dispatcher 分角色授权。

管理员容器仅按需运行，离线、只读根，显式增加管理专属卷权限所需的 CHOWN、DAC_OVERRIDE、FOWNER。它能管理所有凭据，因此 Docker 管理员属于可信边界。导出默认 0600；`--uid/--gid` 只调整明确指定交付文件的所有权。内部 dispatcher 不能作为客户端令牌导出。

token、model auth、邮件 registration token、私钥和任务库均在专属卷或仓库外的秘密目录。不要用环境变量展开把 token 原值塞进 CLI argv，不要在日志、聊天、URL 或项目配置中粘贴它。模型认证只由 worker harness 使用；模型工具的环境不继承任意宿主环境变量。

## 网络

默认 HTTP 仅绑定主机回环地址，用于本机演示。对外使用 TLS override 和有效证书；后端没有 published ports，处于 internal Docker network。只有 worker 的 harness 具有外网连接能力以调用模型服务；模型工具网络由内部沙箱禁止。

真实外部 hostname 由 PUBLIC_URL 传入，MCP Host/Origin 白名单据此生成。认证成功不能绕过身份所有权检查。浏览器只在内存中持有 bearer，DOM 渲染使用 textContent，不存 localStorage，不记录请求体/Authorization。

## 执行隔离及明确取舍

服务使用固定镜像或依赖；应用 UID/GID 为 10001；非 root、根文件系统只读、cap_drop=ALL、no-new-privileges。没有 privileged、host network 或 Docker socket。

Codex 0.154.0 的工具沙箱拒绝读取 /data、/config、/run/secrets 和 /home/agent/.codex，只允许当前任务目录写入，禁用工具网络。最终结果在返回前按已知秘密值过滤；这只是额外措施，不能替代沙箱。

为避免要求使用者手工安装宿主 AppArmor profile，只有 Codex 容器设为 apparmor=unconfined，并在 Moby seccomp 基线上允许嵌套 bubblewrap 所需的六个 syscall；其他容器保留默认 Docker profile。禁用这一层 AppArmor 限制会减少宿主 LSM 保护，不能等同于原始 Docker 默认安全配置。内层沙箱必须在目标主机验证；若无法建立隔离应失败，不得添加危险的 CLI bypass 参数来掩盖问题。

这是独立、受信任操作者的测试环境，不是经验证的敌对多租户代码执行产品。不要接入生产系统、生产数据或敏感源代码。镜像与包还有各自许可证和安全更新责任，见 THIRD_PARTY。

## 撤销、故障与审计

撤销更新原子注册表文件；后端按请求读取，防止 gateway 配置刷新间隙继续接受旧 token。已运行任务不自动取消。轮换的稳定 operation-id 防止成功操作重放产生另一把新 token；若管理员进程在多文件更新期间崩溃，应核对注册表和秘密文件再恢复，不假装具有跨文件事务。

worker 只有一个执行记录；运行中进程退出后状态为 interrupted，不自动重新执行。Mail 的投递状态与执行结果独立；不确定的投递必须人工核对，不能通过盲目重试实现所谓 exactly-once。

stdout 和审计只记录白名单元数据，错误文本固定化。Docker 日志轮转；业务 JSONL/SQLite 的备份、保留与访问控制由操作者负责。不能把可由管理员改写的本地日志当作防篡改审计证明。

发布前运行 gitleaks 全历史扫描与私有信息扫描，检查源码 SPDX 和全部提交 Signed-off-by；运行回执一律保存到仓库外。详见 CONTRIBUTING。
