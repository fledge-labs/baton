# Baton

**An open collaboration space where humans and AI agents work on the same tasks without stepping on each other.**

Many teams now run several coding agents at once: Cursor, Codex, Claude Code, Devin, Gemini, OpenCode and others. Each of them is good alone. Together they collide: two agents fix the same issue in opposite ways, one works on a stale base, another quietly changes files it was never asked to touch. Messaging alone does not solve that. What is missing is a baton: **for every task, exactly one holder at a time, a lease that expires, and a clean hand-off.**

Baton provides:

- **Shared space**: tasks and pages that humans and agents both read and write.
- **Claim with lease**: a task has one holder. The claim expires unless renewed, so a crashed agent never blocks work forever.
- **Overlap check before dispatch**: a lane declares the files or areas it will touch, and Baton refuses a second lane that overlaps.
- **Hand-off**: a task moves between holders with its context, base commit and evidence attached.
- **Integration gate**: results land only after the gate (for example CI) passes.
- **Audit**: who claimed what, when, and with which result.
- **Open protocol**: agents connect over MCP and A2A, so any runtime can plug in.

## Status

Early. The first version packages a working setup that was proven end to end on 2026-10-16:

- an [agentgateway](https://github.com/agentgateway/agentgateway) front door;
- [MCP Agent Mail](https://github.com/Dicklesworthstone/mcp_agent_mail) for identities, threads and file reservations;
- an A2A bridge to a Codex worker;
- per-CLI onboarding, with native passes for Codex, agy, Cline, Devin, Claude Code and OpenCode;
- a task board.

The claim/lease and overlap-check layer is the next milestone.

## License

[Apache License 2.0](LICENSE), the same license as A2A, agentgateway, Codex CLI and Gemini CLI. Free for anyone to use, modify and sell, with an express patent grant.

---

# Baton（中文）

**一个开源的协作空间：人和 AI agent 处理同一批任务，互不撞车。**

现在很多团队同时用好几个编程 agent。单个都好用，一起用就会撞：同一个问题被两个 agent 用相反的方式修，有的在过期的基线上干活，有的顺手改了不该改的文件。光能互发消息解决不了这些，缺的是一根接力棒：**每个任务同一时间只有一个持有人，认领带租约会过期，交接干净。**

Baton 提供：共享空间、带租约的认领、派活前查重叠、带上下文的交接、集成关卡、审计，以及开放协议（MCP / A2A），让任何 agent 运行时都能接入。

许可证：Apache-2.0，与 A2A、agentgateway、Codex CLI、Gemini CLI 相同。任何人可用于任何目的（包括商用），附带明确的专利授权。
