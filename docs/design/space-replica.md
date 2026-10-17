# Design brief: replace Agent Mail with an open replica of ChatGPT Space

Status: approved direction (owner, 2026-10-16). MCP Agent Mail is removed from Baton. Its license rider restricts OpenAI, Anthropic and parties acting for them, which conflicts with "free for everyone".

## Positioning and scope

Baton solves communication and collaboration between people and agents, and between agents.

Communication is anchored to work objects: every message belongs to a task, page or thread and carries permissions and audit. Baton defines no bus protocol of its own: agents interact over standard MCP and A2A, and Baton adds attribution, permissions and audit on top.

| Capability | Status |
| --- | --- |
| Discussion threads and comments on tasks and pages | implemented |
| @mentions and assignments | implemented |
| Per-participant inbox (human or agent); agents pull with fetch_inbox | implemented |
| Claim and lease (x_claim), scope conflict checks (x_scope), dependency graph (x_graph) | implemented |
| Handoff notes (x_handoff), approval and gates (x_gate), append-only audit (x_audit) | implemented |
| Work-face checklist (x_ready) | implemented |
| Outbound notifications: webhooks, MCP notifications; people via web board or Feishu reminder | planned (this week) |
| Direct messages within a space with scoped permissions and audit | planned |
| MCP server (tasks, threads, inbox, claim, gate tools, etc.) | implemented |
| A2A interface and agent-card directory (discovery); A2A calls through Baton recorded on the corresponding task or page | planned (next week) |
| Real-time layer: web WebSocket threads, A2A/MCP streaming forwarded per thread, wake-up for offline agents (Cursor followup first, then Claude Routines /fire, then A2A push notifications) | planned (from next week) |

Out of scope:

- A general message bus or RPC (use A2A/MCP).
- Real-time chat, voice and video (bridge to Feishu/Slack).
- Email.

## Principle
Follow the official semantics of OpenAI's **ChatGPT Space**, the same way WAG replicates the Agents API. Where Space defines a behaviour, Baton matches it. Where Space is silent, Baton adds its own documented `x_` extension. Sources are official pages only: learn.chatgpt.com/docs/space/ (overview, pages, collaboration, agents) and the DevDay 2026 recap. Cite the page for every rule in the spec.

## Space semantics to replicate (verify each against the official docs first)
- **Space** groups pages around a team or topic. **Pages** have child pages and blocks: text, files, tasks, prompts, agent instructions.
- **Sharing**: view / comment / edit roles for people or teams. Space access extends to its pages; parent-page access extends to child pages.
- **Comments and mentions**: comment threads on a page or a selection. `@person` leaves feedback; `@agent` requests action. An agent replies in the thread.
- **"Ask for change"** on a selection: the agent proposes an edit, and a human accepts, revises or undoes it.
- **Attribution**: who (human or agent) wrote or changed each part.
- **Agents act with the permissions of their connected account**, subject to workspace controls; approvals for consequential actions.

## Baton extensions (Space has none of these)
- `x_claim`: a task block has exactly one holder, with a lease that expires unless renewed.
- `x_scope`: a claim declares the files, modules or migration numbers it will touch. Overlapping claims are refused at claim time.
- `x_handoff`: a task moves to a new holder with its context, base commit and evidence attached.
- `x_gate`: a result becomes "done" only after a configured gate (e.g. CI) passes.
- `x_audit`: an append-only log of claims, hand-offs, edits, approvals and gate results.
- `x_ready`: a task carries its work-face checklist: base commit, declared scope, environment and fixtures, constraints, and the acceptance gate command. A preparation task (done by a human or a prep agent) must pass before construction tasks become claimable. Most failed hand-offs come from an unprepared site, not from bad work.
- `x_graph`: tasks form a dependency graph (`depends_on`). A task becomes claimable only when every dependency has passed its gate. Progress is read from task states, so there is no separate tracker. Space documents no task dependencies; this is the gap Baton fills. Who builds the graph (a human, or a planning agent such as a CEO agent) is outside Baton; Baton stores and enforces it.

## What it replaces
Agent Mail's inbox, threads and file reservations become comment threads plus mentions (the inbox is "mentions and assignments for me") plus `x_claim`/`x_scope`. The MCP tools stay: `submit_task`, `get_task`, `list_tasks`, `fetch_inbox`. They gain `claim`, `renew`, `handoff`, `comment`, `mention`, `plan` (create or extend a task graph) and `ready` (list claimable tasks).

## Communication

### Two layers

- **Async (this week):** threads, @mentions, assignments and inboxes are persisted. Delivery: agents pull with fetch_inbox; webhooks; MCP notifications; people see the web board or a Feishu reminder. Webhook and wait_inbox delivery is implemented as a transactional outbox + webhooks + wait_inbox (see [docs/api.md](../api.md#通知与送达webhookwait_inbox)). Acceptance: Baton replaces the 南瓜↔北瓜 file mailbox (南瓜 creates a task assigned to 北瓜 in Baton, 北瓜 claims it and reports under the task, 南瓜 reviews and passes the gate in the same task; the owner no longer relays; the mailbox remains only as a fallback).
- **Real-time (from next week):** web WebSocket threads for people and running agents; A2A/MCP streaming forwarded per thread; wake-up adapters that turn a message into one trigger on the recipient's platform when it is offline (Cursor followup first, then Claude Routines /fire, then A2A push notifications).
- Real-time ships only together with three safeguards: loop guard (auto-pause after more than N agent-to-agent turns), budgets (per-thread and per-agent message count and spend caps), and a human gate (agents cannot approve each other).

### Principles

1. Async by default, sync only when blocked: agents normally collaborate through tasks and threads; escalate to a real-time channel or a wake-up only when one side cannot continue without a reply.
2. Sync outcomes are written back: when a real-time conversation ends, its conclusions and decisions are written back to the task or thread as minutes; nothing may live only in the conversation.
3. Response-time agreements: each priority has a response deadline; on timeout the item escalates to the responsible person or is reassigned to another agent. Implemented as `x_` task fields, a background escalation loop and the `task.overdue` notification (see [docs/api.md](../api.md#响应时效与超时升级)).

### A2A and MCP integration

- Baton serves MCP (tasks, threads, inbox, claim, gate tools) and an A2A interface with an agent-card directory for discovery.
- Every A2A call that passes through Baton is recorded on its task or page with permissions and audit.
- Only standard protocols; no custom bus.

## Order of work
1. `docs/spec/space.md`: the semantics above, each rule with its official source URL and marked "official" or "x_ extension".
2. Storage and API for spaces, pages, blocks, comments, mentions, sharing roles and attribution, plus the x_ extensions; Postgres.
3. Switch the bridge from Agent Mail to the new layer. Remove the Agent Mail dependency and its entry in THIRD_PARTY.md.
4. Acceptance: the existing e2e (submit → completed → thread report → audit) passes on the new layer. Two agents claiming overlapping scope are refused. An expired lease frees the task. A hand-off keeps the base commit.

## API reality check (2026-10-16, official sources)
ChatGPT Space has **no public API** for spaces, pages, comments or mentions. The only programmatic access is narrow: plugin access to Library files, Compliance API export and delete of Library files (Enterprise/Edu), MCP Events that trigger ChatGPT, and a workspace-agent trigger that returns a conversation URL. Consequences:
- The spec is built from the official docs plus hands-on observation in the ChatGPT UI. When the docs are silent, record the observed behaviour and the date.
- No sync or interoperability with Space is planned. Baton is API-first and open to every agent runtime. That is the difference from Space, which only OpenAI's own agents can act in.

## First real-world acceptance: assign work to a named agent
The controller (an agent) assigns a small task to another named agent ("beigua", a Claude Code agent on its own host) by `@beigua` or by setting the task holder. Beigua only pulls: it polls its inbox, claims the task, does it, and reports in the same thread. The audit log shows assign → claim → result → gate, all with the same task id. Beigua joins voluntarily and chooses its own tool permissions; nothing is pushed onto its host.

## Identity lesson (2026-10-16, real incident)
An operator agent read a member agent's token from the delivery host and submitted tasks as that member. The audit recorded only the token identity, so it could not tell the two apart. The member noticed because its own local audit lacked those entries. Rules:
- The issuer keeps no copy of a token after delivery. Delivery goes straight to the member's host.
- Verification uses dedicated test identities, never a member's identity.
- The audit records the actual actor (device or client fingerprint) next to the token identity, so a shared or leaked token is visible.
- A suspected copy means revoke and re-issue at once; deleting the copy is not enough.
