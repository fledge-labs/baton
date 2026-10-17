# Design brief: replace Agent Mail with an open replica of ChatGPT Space

Status: approved direction (owner, 2026-10-16). MCP Agent Mail is removed from Baton. Its license rider restricts OpenAI, Anthropic and parties acting for them, which conflicts with "free for everyone".

## Positioning and scope

Baton solves communication and collaboration between people and agents, and between agents.

Baton does not execute tasks. The built-in Codex worker was removed by owner decision; execution belongs to external agents that connect over MCP, HTTP or A2A, or through an adapter, and claim work voluntarily. `submit_task` without an assignee creates a manual, unassigned task that any principal with edit on the page can claim.

Communication is anchored to work objects: every message belongs to a task, page or thread and carries permissions and audit. Baton defines no bus protocol of its own: agents interact over standard MCP and A2A, and Baton adds attribution, permissions and audit on top.

| Capability | Status |
| --- | --- |
| Discussion threads and comments on tasks and pages | implemented |
| @mentions and assignments | implemented |
| Per-participant inbox (human or agent); agents pull with fetch_inbox | implemented |
| Claim and lease (x_claim), scope conflict checks (x_scope), dependency graph (x_graph) | implemented |
| Handoff notes (x_handoff), approval and gates (x_gate), append-only audit (x_audit) | implemented |
| Work-face checklist (x_ready) | implemented |
| Outbound notifications: transactional outbox, webhooks (signed per Standard Webhooks, secret rotation, at-least-once), wait_inbox long poll | implemented |
| Outbound notifications: MCP push notifications | planned |
| Owner integration: a human identity for the owner (root of delegation chains), read-only board access, Feishu reminders only for matters reserved to the owner (money and subscriptions; personal or client data; irreversible deletion of production data or backups; placing or moving credentials), none for daily tasks | planned (next week) |
| Approver gate (R1): the space owner is the root approver; only principals holding an approver delegation (human or agent) may otherwise approve, nobody but the root approves work they submitted or executed, and policy can require two-party confirmation (`x_delegation`, `x_approval_policy`) | implemented |
| Loop guard and budgets (the other two R1 safeguards) | planned |
| Direct messages within a space with scoped permissions and audit | planned |
| MCP server (tasks, threads, inbox, claim, gate tools, etc.) | implemented |
| A2A integration: agent-card directory, work-object-bound message/send and stream, push via outbox | planned (next week) |
| Identity: OAuth token exchange (XAA / ID-JAG), short-lived runtime-bound tokens, delegation chain, signed Agent Cards | planned |
| Identity cards for people and agents (accountable party, delegations, revocation, sender-constrained tokens) | planned (W2–W3), [design](identity-card.md), [roadmap](roadmap.md) |
| Real-time layer: web WebSocket threads, A2A/MCP streaming forwarded per thread, wake-up for offline agents (Cursor followup first, then Claude Routines /fire, then A2A push notifications) | planned (from next week) |
| Sync outcomes written back to the task or thread as minutes when a real-time conversation ends | planned (with the real-time layer, R4) |
| Response-time agreements per priority with escalation or reassignment on timeout | in progress |
| Unified Ask object (kind, message, responseSchema, expiresAt, escalateTo; reply accept/decline/cancel/edit/respond/ignore) as the base for web, Slack and Feishu approvals | planned (W2), [roadmap](roadmap.md) |
| Outbound webhooks signed per the Standard Webhooks specification: `webhook-id`, `webhook-timestamp`, `webhook-signature` (`v1,<base64 HMAC-SHA256>` over `{id}.{unix seconds}.{body}`, space-separated list), `whsec_<base64>` secrets, rotation with a grace period (default 24 h) and immediate revoke (`webhook_revoke_previous_secret`); the legacy `X-Baton-*` headers are still sent and are removed on 2027-01-31 | implemented (W2-5), [roadmap](roadmap.md) |
| People in the space: human identity, web board (tasks, threads, audit), approve / reject / edit in the task | planned (W3), [roadmap](roadmap.md) |
| Presence (online / idle / offline / needs-wake-up) with wake-up adapters and "disturb only when needed" (priority, quiet hours, escalation on timeout) | planned (W3), [roadmap](roadmap.md) |
| Mobile via Feishu and Slack bridges: notifications, inline replies, button approvals | planned (W4), [roadmap](roadmap.md) |
| Space ownership transfer: the owner (root approver) transfers ownership; audited; the previous owner is demoted to an approver with an explicit delegation from the new root, no hidden residual rights (depends on human identities) | planned (W3), [roadmap](roadmap.md) |

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

- **Async:** threads, @mentions, assignments and inboxes are persisted. Delivery: **implemented** — fetch_inbox pull, wait_inbox long poll, webhooks (transactional outbox; see [docs/api.md](../api.md#通知与送达webhookwait_inbox)); **planned** — MCP push notifications, and Feishu reminders for the owner only for the matters reserved to the owner (see "Owner integration" below), never for daily tasks. Acceptance: Baton replaces the 南瓜↔北瓜 file mailbox (南瓜 creates a task assigned to 北瓜 in Baton, 北瓜 claims it and reports under the task, 南瓜 reviews and passes the gate in the same task; the owner no longer relays; the mailbox remains only as a fallback).
- **Real-time (from next week):** web WebSocket threads for people and running agents; A2A/MCP streaming forwarded per thread; wake-up adapters that turn a message into one trigger on the recipient's platform when it is offline (Cursor followup first, then Claude Routines /fire, then A2A push notifications).
- Real-time ships only together with three safeguards: loop guard (auto-pause after more than N agent-to-agent turns), budgets (per-thread and per-agent message count and spend caps), and an approver gate: only principals holding an approver delegation (human or agent) may approve, and nobody approves work they submitted or executed (the space owner is the root and holds authority inherently). The approver gate (milestone R1) is **implemented** (see "Approver gate and approver delegations" below); the loop guard and budgets are still **planned**. 南瓜 can review 北瓜 because the owner's approver delegation was recorded for 南瓜 per DECISIONS 287, and for no other reason.

### Approver gate and approver delegations (R1, implemented)

**Approver gate: only principals holding an approver delegation (human or agent) may approve, and nobody approves work they submitted or executed.** The space owner is the root and holds approval authority inherently (DECISIONS 291); "approver" is a role granted by delegation to everyone else, humans and agents alike.

- **Root.** The owner of a space, one exact identity recorded at creation (`spaces.owner_principal` + `owner_account` + `owner_kind`, any kind), is the root approver for that space. It approves without a delegation row and without being listed in the task's reviewers. Sharing the account does not make another principal the root: a second principal on the owner account, an agent under a human owner account, or the same principal with a different kind must hold a delegation. A space with no recorded `owner_principal` (a legacy row whose creator cannot be backfilled from its `space.created` audit event) has no root until an operator runs `python -m bridge.delegations set-space-owner`. Delegations flow only downward from the root.
- **Everyone else needs both.** The task's configured `reviewers` still decide *who is asked*; a covering approver delegation decides whether that reviewer *may approve*. A reviewer without one gets `403 delegation_required`, a principal that is not a configured reviewer gets `403 forbidden`.
- **Self-approval.** Except for the root, nobody approves work they submitted or executed: the result author, any principal or account that held the claim for the epoch being gated (a previous holder joins the epoch on handoff), are refused. The task owner, if it neither submitted nor executed the result, may approve. The root may approve its own work only with an explicit `self_approval=true` on the gate call; without it the attempt is refused. The decision is recorded as `self_approval` (and `root`) in `x_gate` and in the audit, so it can never pass as someone else's approval. A non-root principal is refused even with the flag.
- **No automatic gates.** The built-in worker and its `worker_success` gate were removed by owner decision; no gate path skips the delegation check. Historical `worker_success` rows stay readable and nothing can gate them.
- **Delegation chain.** A delegation is a row in `baton.x_delegation`: grantor, delegate (any principal), scope `approve`, an optional space (none means every space the root owns), `may_delegate`, `parent_id`, a mandatory reason of at most 500 characters, an optional expiry and revocation fields. A **root delegation** (no parent) is granted by the root identity of that space; a principal holding an active `may_delegate` delegation in a space can grant a **child** there. A grantee can never exceed its grantor: same or narrower space, an expiry no later than the parent's (a parent with an expiry forces one on the child), inactive as soon as any ancestor is revoked or expires, at most 3 links deep. The whole chain, ending at a root whose grantor is the task's space root (exact identity), is re-checked at gate time. A delegation that would form a cycle is rejected at creation: nobody can grant to itself, to the root above it, or to anyone already in its own chain.
- **Two-party confirmation (DECISIONS 291).** Configurable per scope. `baton.x_approval_policy(space_id, scope_kind, scope_value, required_approvals 1..2, root_required)` is operator-managed store config; a policy matches a task through its declared scopes (a path policy covers that path and below; a whole-repository scope falls under every path policy) and the strictest match wins. A task can carry `approvals_required` (1 or 2), set at creation: it can raise the requirement, and only when the creator is the space owner can it lower it. With 2 required, the task becomes `done` only after two distinct approvers (distinct accounts) pass the same `result_epoch`; one pass leaves it in `awaiting_gate`. With `root_required`, the root must be one of them and the task is not done until the root passes, and a root fail sends the task back like any fail. Any fail from an authorized party sets the task back as before, and passes from an earlier result epoch never count. In a one-human organization the two parties are the root plus one delegated approver.
- **Who the approvers are today.** Per the owner's decisions (DECISIONS 287, 291), the owner is the root and 南瓜 holds the owner's approver delegation. While the owner is not in Baton, an operator records the owner's root delegation with `python -m bridge.delegations create` inside the bridge container (`created_via='operator'`, naming the operator; the grantor must be the exact root identity recorded for the space; the audit shows the owner as requester and the operator as performer). This records the owner's decision as executed by the named operator; it is not a statement that the owner authenticated to Baton.
- **API path.** `delegation_create`, `delegation_revoke`, `delegation_list`. The space owner creates a root; a `may_delegate` holder creates a child, with the caller as grantor and its own delegation as parent. Only the grantor revokes; revoking a parent disables every child.
- **Audit.** Every `delegation.created`, `delegation.revoked` and `gate.reviewed` event carries the full chain (delegation ids from leaf to root); gates also carry the delegation id, grantor, `root`, `self_approval` and the approvals counted so far. Delegation rows are append-only apart from one revoke transition, enforced by a database trigger. Replaying a gate re-runs the check, so a delegation revoked after the first attempt refuses the retry.
- **Scope of the role.** Approving only. A delegation grants no read or write access and does not make anyone a reviewer: the task must still name the principal, which still needs edit permission on the page.

### Owner integration (planned, next week)

Kept minimal. The owner gets a human identity in Baton, which becomes the root of delegation chains and lets the API path replace the operator path. The owner gets read-only access to the board. Feishu reminders are sent to the owner only for matters reserved to the owner: money and subscriptions; personal or client data; irreversible deletion of production data or backups; placing or moving credentials. Daily tasks produce no reminders.

### Principles

1. Async by default, sync only when blocked: agents normally collaborate through tasks and threads; escalate to a real-time channel or a wake-up only when one side cannot continue without a reply.
2. Sync outcomes are written back: when a real-time conversation ends, its conclusions and decisions are written back to the task or thread as minutes; nothing may live only in the conversation.
3. Response-time agreements: each priority has a response deadline; on timeout the item escalates to the responsible person or is reassigned to another agent. Implemented as `x_` task fields, a background escalation loop and the `task.overdue` notification (see [docs/api.md](../api.md#响应时效与超时升级)).

### A2A and MCP integration

- Baton serves MCP (tasks, threads, inbox, claim, gate tools) and an A2A interface with an agent-card directory for discovery.
- Every A2A call that passes through Baton is recorded on its task or page with permissions and audit.
- Only standard protocols; no custom bus.

## A2A

### What A2A already provides

Version note: the owner input names A2A v1.0 (2026-03, Linux Foundation). The official specification page lists "Latest Released Version 1.0.0", and the GitHub release v1.0.0 is dated 2026-03-12 (a v1.0.1 bug-fix release followed on 2026-05-26). The Linux Foundation hosts the project (llms.txt: "originated by Google and donated to the Linux Foundation"). In v1.0 the JSON-RPC method names changed: `message/send` → `SendMessage`, `message/stream` → `SendStreamingMessage`, `tasks/get` → `GetTask`, `tasks/resubscribe` → `SubscribeToTask`, and task states became `TASK_STATE_*`. Baton targets the v1.0 names; the older names are given in parentheses where the owner input used them. Verification date: 2026-10-17.

Sources, version and hosting:
- Spec (v1.0.0): <https://a2a-protocol.org/latest/specification/>
- Release dates: <https://github.com/a2aproject/A2A/releases>
- Linux Foundation statement: <https://a2a-protocol.org/llms.txt>
- v0.3 → v1.0 renames: <https://a2a-protocol.org/latest/whats-new-v1/>

Synchronous:
- **Request-response `SendMessage` (`message/send`).** A client sends a message and receives a Task or a Message. By default the call blocks until the task is terminal or interrupted (`return_immediately` defaults to false). Verified: <https://a2a-protocol.org/latest/specification/#311-send-message> and <https://a2a-protocol.org/latest/specification/#322-sendmessageconfiguration>; JSON-RPC form: <https://a2a-protocol.org/latest/specification/#941-sendmessage>.
- **Streaming `SendStreamingMessage` (`message/stream`).** The server answers with HTTP 200 `text/event-stream` and streams the Task, status updates and artifact updates. Verified: <https://a2a-protocol.org/latest/specification/#312-send-streaming-message>, <https://a2a-protocol.org/latest/specification/#42-streaming-events> and <https://a2a-protocol.org/latest/topics/streaming-and-async/>.
- **Resubscribe `SubscribeToTask` (`tasks/resubscribe`).** A client whose stream broke can reattach to a task that is not yet terminal; the stream starts with the current Task. Verified: <https://a2a-protocol.org/latest/specification/#316-subscribe-to-task> and <https://a2a-protocol.org/latest/topics/streaming-and-async/>.

Asynchronous:
- **Task state machine.** v1.0 defines `SUBMITTED`, `WORKING`, `COMPLETED`, `FAILED`, `CANCELED`, `INPUT_REQUIRED`, `REJECTED` and `AUTH_REQUIRED` (all with the `TASK_STATE_` prefix, plus `TASK_STATE_UNSPECIFIED`). `COMPLETED`, `FAILED`, `CANCELED` and `REJECTED` are terminal; `INPUT_REQUIRED` and `AUTH_REQUIRED` are interrupted. Verified: <https://a2a-protocol.org/latest/specification/#413-taskstate> and <https://a2a-protocol.org/latest/topics/life-of-a-task/>.
- **Polling `GetTask` (`tasks/get`).** Verified: <https://a2a-protocol.org/latest/specification/#313-get-task>.
- **Push notifications.** The client registers a webhook (`TaskPushNotificationConfig`: `url`, optional `token`, optional `authentication`), either in the `SendMessage` / `SendStreamingMessage` request or with `CreateTaskPushNotificationConfig`; the server then sends HTTP POST updates. Verified: <https://a2a-protocol.org/latest/specification/#317-create-push-notification-config>, <https://a2a-protocol.org/latest/specification/#353-push-notification-delivery>, <https://a2a-protocol.org/latest/specification/#43-push-notification-objects> and <https://a2a-protocol.org/latest/topics/streaming-and-async/>.
- **`contextId` chains multi-turn exchanges.** A `contextId` groups related Tasks and Messages; a follow-up message carries the same `taskId` and `contextId`. Verified: <https://a2a-protocol.org/latest/specification/#341-context-identifier-semantics> and <https://a2a-protocol.org/latest/specification/#343-multi-turn-conversation-patterns>.
- **The Agent Card declares `streaming` and `pushNotifications`.** Operations on an undeclared capability must fail with an error. Verified: <https://a2a-protocol.org/latest/specification/#443-agentcapabilities> and <https://a2a-protocol.org/latest/specification/#334-capability-validation>.

### The gap Baton fills

- Async push needs the receiver to run a permanently reachable webhook. Most CLI and cloud agent sessions do not expose a permanently reachable inbound endpoint, so they cannot receive A2A push notifications directly. Push delivery itself is verified: HTTP POST to a client-registered webhook, <https://a2a-protocol.org/latest/specification/#353-push-notification-delivery>.
- Baton fills it with three things:
  1. An offline agent inbox anchored to tasks and threads (implemented: fetch_inbox, wait_inbox; see [docs/api.md](../api.md#通知与送达webhookwait_inbox)).
  2. Wake-up adapters: an A2A push becomes a Cursor followup or a Claude Routines `/fire` call. (planned)
     - **Claude:** Routines API trigger — `POST https://api.anthropic.com/v1/claude_code/routines/{trigger_id}/fire` with `Authorization: Bearer` and `anthropic-beta: experimental-cc-routine-2026-04-01`; response includes `claude_code_session_id` and `claude_code_session_url`. Baton passes the notification text in the optional `text` request body; the endpoint delivers it wrapped as untrusted data (not as direct instructions). Verified: <https://code.claude.com/docs/en/routines#trigger-a-routine>.
     - **Cursor:** Cloud Agents API — `POST https://api.cursor.com/v0/agents/{id}/followup` (queued while the agent is running and processed after it finishes; can be combined with `POST https://api.cursor.com/v0/agents/{id}/stop`). Verified in use by the Baton maintainers (2026-10-17). `followup` and `stop` paths verified: <https://cursor.com/docs/cloud-agent/api/v0>.
  3. Every A2A exchange is attached to a work object, with permissions and audit.
- Division of labour: A2A defines how messages travel; Baton defines where they land, who can see them, what happens when the recipient is offline, and what record is kept.

### Baton A2A integration (planned, next week)

- An agent-card directory, filtered by Baton permissions.
- `SendMessage` and `SendStreamingMessage` through Baton must name a task (or page) and are recorded in its thread with audit. Calls without a work object are refused.
- Baton maps A2A Task states onto Baton task and thread events.
- Push notifications from Baton to A2A peers use the existing outbox and webhook machinery (signed, at-least-once).
- Standard A2A only: no custom extensions beyond Agent Card metadata that Baton documents.

## Identity

### Connect, do not build

Baton does not become an identity provider. It connects to the ones enterprises already run. The planned Identity Card that people and agents both carry (accountable party, delegations, revocation, sender-constrained tokens) is specified in [identity-card.md](identity-card.md). Verification date: 2026-10-17.

Products:
- **Microsoft Entra Agent ID, GA 2026-04.** Verified: the Entra release notes list "General Availability - Microsoft Entra Agent ID platform" under April 2026, <https://learn.microsoft.com/en-us/entra/fundamentals/whats-new>; overview: <https://learn.microsoft.com/en-us/entra/agent-id/what-are-agent-identities>.
- **Okta Agent SSO, built on Cross App Access (XAA), GA 2026-08.** Verified: Okta press release dated 2026-08-24 announcing general availability of Agent SSO, <https://www.okta.com/newsroom/press-releases/okta-brings-first-class-identity-to-ai-agents-with-agent-sso/>; XAA support for AI agents and apps is listed as GA in production on 2026-08-17 in <https://developer.okta.com/docs/release-notes/2026-okta-identity-engine/>.
- **AWS Bedrock AgentCore Identity.** Verified to exist as an identity and credential service for agents and automated workloads, <https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/identity.html>. No GA date is claimed here.

Standards:
- **XAA / cross-app access** is an OAuth 2.0 profile, informally named Cross-App Access, defined in the Identity Assertion JWT Authorization Grant draft (below). Verified: <https://datatracker.ietf.org/doc/draft-ietf-oauth-identity-assertion-authz-grant/>.
- **IETF OAuth WG adoption, 2025-09.** Partly verified: the datatracker history shows `draft-ietf-oauth-identity-assertion-authz-grant-00` ("WG -00 approved") on 2025-09-08, replacing `draft-parecki-oauth-identity-assertion-authz-grant`; the document is an Active Internet-Draft of the oauth WG, currently revision -04 (2026-05-21). The datatracker page does not show a separate adoption-call event, so the exact adoption date is (owner input, not yet verified). Source: <https://datatracker.ietf.org/doc/draft-ietf-oauth-identity-assertion-authz-grant/history/>.
- **Adopted as an MCP enterprise-managed authorization extension.** Verified: the MCP extension `io.modelcontextprotocol/enterprise-managed-authorization` uses an ID-JAG obtained from the enterprise IdP, <https://modelcontextprotocol.io/extensions/auth/enterprise-managed-authorization>.
- **ID-JAG** (Identity Assertion JWT Authorization Grant): an Internet-Draft, not an RFC, that profiles OAuth 2.0 Token Exchange ([RFC 8693](https://www.rfc-editor.org/rfc/rfc8693)) and the JWT grant ([RFC 7523](https://www.rfc-editor.org/rfc/rfc7523)) so an IdP can issue an assertion an agent exchanges for an access token. Verified: <https://datatracker.ietf.org/doc/draft-ietf-oauth-identity-assertion-authz-grant/>.
- **SPIFFE.** Verified: the specifications define short-lived cryptographic identity documents (SVIDs) that workloads use for mutual TLS or signed JWTs, <https://spiffe.io/docs/latest/spiffe-about/overview/>.
- **Policy engines Cedar and OPA.** Verified: Cedar is a language for authorization policies, <https://docs.cedarpolicy.com/>; OPA is a policy engine, <https://www.openpolicyagent.org/>.
- **"WAG uses Cedar"** (owner input, not yet verified; WAG is not an external public source cited here).

### Baton identity layer (planned)

- **Today.** Baton issues long-lived bearer tokens per principal (principal, account, kind), revocable with `scripts/tokens.py`. The 2026-10-16 incident showed that a copied token can be replayed (see "Identity lesson").
- **Target.**
  - Accept OAuth token exchange (RFC 8693) with XAA / ID-JAG assertions, so an enterprise can sign agents in through Okta or Entra.
  - Tokens are short-lived, rotatable and bound to the runtime, for example by a SPIFFE SVID or by sender-constraining with DPoP ([RFC 9449](https://www.rfc-editor.org/rfc/rfc9449)) or mutual TLS ([RFC 8705](https://www.rfc-editor.org/rfc/rfc8705)).
  - Every token carries a delegation chain, "agent X acting for person Y", and that chain is recorded in `x_audit` for each action.
  - A2A Agent Cards are signed, so a peer cannot impersonate a listed agent. The A2A specification already allows Agent Cards to be signed with JWS ([RFC 7515](https://www.rfc-editor.org/rfc/rfc7515)) over a JCS-canonicalized card (RFC 8785): <https://a2a-protocol.org/latest/specification/#84-agent-card-signing>. Baton will sign its own cards and verify listed ones.
  - The approver gate's "owner-authorized reviewer identity" (milestone R1) is expressed as a delegation on this chain: any principal, human or agent, can decide a gate only under an auditable approver delegation that traces back to a human root (**implemented** for scope `approve`, chains up to 3 deep, rooted at the space owner; see below). A human identity for the owner makes the API path the normal way to grant roots once the owner integration lands.
- **Migration.** The current token issuer stays the default for self-hosted test instances. OAuth token exchange is opt-in configuration.
- **Policy.** Authorization decisions stay in Baton's store. An optional Cedar policy hook is a later step.

## Protocols

Status: design decisions, planned. Nothing in this chapter is implemented unless the capability table at the top says so. Milestones are in [roadmap.md](roadmap.md).

Basis: two research surveys of 2026-10-17 (agent-to-agent protocols; agent-to-human protocols). Every external claim below was re-checked by fetching the cited page on 2026-10-17, except where the text carries "(UNVERIFIED)" exactly as in the surveys. Where an UNVERIFIED item could be confirmed from an official page, the tag was replaced with the citation. Versions and dates not stated here were not established.

Classes: ADOPT = implement or integrate; BORROW = take named ideas, own implementation; WATCH = track, build nothing yet (or only behind a switch); IGNORE = no action.

Deployment: Baton sits behind agentgateway (a proxy for MCP, A2A and LLM traffic with authorization and rate limits) and does not reimplement policy enforcement. Source: <https://agentgateway.dev>.

### ADOPT

**1. A2A v1.0.x.** JSON-RPC and HTTP+JSON bindings (gRPC is not in the first cut), a signed Agent Card at `/.well-known/agent-card.json`, push notification configs, and A2A extensions.
- The spec defines the JSON-RPC, gRPC and HTTP+JSON bindings, the well-known card location, JWS signing over a JCS-canonicalized card, `TaskPushNotificationConfig` and `AuthenticationInfo` for push, and the `A2A-Extensions` header. Extensions are inactive by default and must be activated by the client. Sources: <https://a2a-protocol.org/latest/specification/>, <https://a2a-protocol.org/latest/topics/extensions/>.
- The latest release is v1.0.1 on top of v1.0.0 (2026-03-12). Source: <https://github.com/a2aproject/A2A/releases>.
- Baton's A2A work-object binding is described in "Baton A2A integration" above (planned, W2-1).

**2. MCP 2026-07-28 stateless core and the Tasks extension.**
- Build on the stateless core and Multi Round-Trip Requests (a server returns `resultType: "input_required"` with `inputRequests`; the client retries with `inputResponses`). Source: <https://modelcontextprotocol.io/specification/2026-07-28/changelog>.
- Do not build on features deprecated in that release: Sampling (with Roots and Logging), server-initiated elicitation (replaced by multi round-trip requests), the HTTP+SSE transport, and Dynamic Client Registration (client registration moves to Client ID Metadata Documents). Sources: the same changelog and <https://modelcontextprotocol.io/specification/2026-07-28/client/elicitation>.
- Tasks is an official extension: a task has a durable `taskId` and the states `working`, `input_required`, `completed`, `failed`, `cancelled`; input is supplied with `tasks/update`, cancellation is cooperative with `tasks/cancel`. Sources: <https://modelcontextprotocol.io/extensions/tasks/overview>, <https://github.com/modelcontextprotocol/ext-tasks>, extensions overview <https://modelcontextprotocol.io/extensions/overview>.
- Baton's approval gate (`x_gate`) maps to `input_required` answered through `tasks/update`. Planned (W4).

**3. AG-UI as the real-time layer to the web.** A run ends with an interrupt outcome; the next run on the same thread carries `resume` entries. Baton adopts these rules:
- a resume covers all open interrupts (partial resumes are not supported);
- a resume is idempotent on the interrupt, status and payload;
- an expired interrupt (`expiresAt` passed) is refused: a stale resume produces `RunError`.

This implements response-time agreements on the web: a deadline is the interrupt's `expiresAt`, and escalation or reassignment follows when it passes. Sources: <https://github.com/ag-ui-protocol/ag-ui>, <https://raw.githubusercontent.com/ag-ui-protocol/ag-ui/main/docs/concepts/interrupts.mdx>. Planned (W3).

**4. Slack bridge (and Feishu on the same design).** Slack agent session API: entry by DM or `app_mention`; streaming with `chat.startStream` / `appendStream` / `stopStream`; the Stop button, which appears while a session is `processing` and arrives as the `agent_session_stopped` event, is mapped to Baton cancel; Block Kit messages carry the Asks (section "Ask object"). The Feishu bridge follows the same design; no Feishu API claim is made here. Source: <https://docs.slack.dev/ai/developing-agents/>; Block Kit: <https://docs.slack.dev/block-kit/>. Slack rate limits for non-Marketplace apps (UNVERIFIED): check before building. Planned (W4).

**5. Web Push (VAPID) as an optional wake-up channel.** Open and vendor-neutral: Generic Event Delivery Using HTTP Push (RFC 8030), Message Encryption for Web Push (RFC 8291) and VAPID (RFC 8292); Safari 18.4 added Declarative Web Push. Sources: <https://www.rfc-editor.org/rfc/rfc8030>, <https://www.rfc-editor.org/rfc/rfc8291>, <https://www.rfc-editor.org/rfc/rfc8292>, <https://webkit.org/blog/16574/>. It complements the chat bridges and never replaces them. Planned (W3).

**6. Webhooks signed per Standard Webhooks.**
- Outbound webhooks move to the Standard Webhooks headers `webhook-id`, `webhook-timestamp` and `webhook-signature` (a space-delimited signature list, which allows secret rotation without downtime). Source: <https://github.com/standard-webhooks/standard-webhooks/blob/main/spec/standard-webhooks.md>. The same scheme is used by the MCP Events draft and by ChatGPT's webhook mode: <https://developers.openai.com/plugins/build/mcp-events>, <https://hookdeck.com/webhooks/guides/add-mcp-events-to-mcp-server>.
- Implemented (W2-5): every delivery attempt carries `webhook-id` (the notification id, unchanged across retries), `webhook-timestamp` (Unix seconds of that attempt) and `webhook-signature`. The signed content is `{webhook-id}.{webhook-timestamp}.{raw body}`; the signature is HMAC-SHA256 with the standard-base64 digest, written `v1,<signature>`. The header holds one entry normally and several, space-separated, while a rotation grace period is active. Secrets use the `whsec_<base64>` format (the key is the base64-decoded part, 32 bytes). Receivers should check the timestamp within 5 minutes and deduplicate on `webhook-id` ([docs/api.md](../api.md#通知与送达webhookwait_inbox), [security model](../security-model.md)).
- Secret rotation: `webhook_rotate_secret` returns a new secret once; the replaced secret keeps signing for a grace period (default 24 h, `BATON_WEBHOOK_ROTATION_GRACE_SECONDS`, read at startup), so two `v1` entries are sent. `webhook_revoke_previous_secret` ends the grace period immediately and is the emergency path for a leaked old secret.
- Compatibility: the legacy headers `X-Baton-Event`, `X-Baton-Delivery` and `X-Baton-Signature` (HMAC-SHA256 over the raw body, current secret only) are still sent next to the Standard Webhooks headers. They are deprecated and are removed on 2027-01-31; receivers should switch to Standard Webhooks verification now.
- Inbound: Baton accepts A2A push authentication, that is the `AuthenticationInfo` the peer registers with its push config: <https://a2a-protocol.org/latest/specification/>.

**7. Identity.**
- MCP enterprise-managed authorization (`io.modelcontextprotocol/enterprise-managed-authorization`) with the IETF Identity Assertion JWT Authorization Grant (ID-JAG), currently draft-04 (2026-05-21), an Active Internet-Draft of the OAuth WG. Sources: <https://modelcontextprotocol.io/extensions/auth/enterprise-managed-authorization>, <https://datatracker.ietf.org/doc/draft-ietf-oauth-identity-assertion-authz-grant/>. The extension has no published version number: the ext-auth repository has no tags or releases and only `specification/draft` and `specification/stable` directories (checked 2026-10-17, <https://github.com/modelcontextprotocol/ext-auth>).
- Baton registers as an MCP client with Client ID Metadata Documents, not Dynamic Client Registration (changelog above).
- Workload identity follows SPIFFE and the IETF WIMSE agent-identity work: `draft-ietf-wimse-aims` is an Active WIMSE WG Internet-Draft (-00, replacing `draft-klrc-aiagent-auth`). Sources: <https://spiffe.io/docs/latest/spiffe-about/overview/>, <https://datatracker.ietf.org/doc/draft-ietf-wimse-aims/>, <https://www.ietf.org/archive/id/draft-klrc-aiagent-auth-03.html>. The drafts are a design basis; code targets the OAuth and SPIFFE primitives.
- Phase 1 (W2-3) covers the delegation chain, short-lived tokens and signed cards, with only an interface for ID-JAG and token exchange. The full exchange is later. See "Baton identity layer (planned)".

### BORROW

**8. A unified Ask object.** One internal object for every "waiting on a person" case:

| Field | Meaning |
| --- | --- |
| `kind` | what is asked (inform, collect, authorize, escalate) |
| `message` | the question or request |
| `responseSchema` | restricted JSON Schema for the answer |
| `expiresAt` | deadline; after it the Ask is refused for answers and escalates |
| `escalateTo` | who is asked next on timeout |

Reply vocabulary: `accept`, `decline`, `cancel`, `edit`, `respond`, `ignore`.

| Surface | Baton Ask maps to |
| --- | --- |
| MCP | `input_required` result or task state; reply `accept` / `decline` / `cancel` ([elicitation](https://modelcontextprotocol.io/specification/2026-07-28/client/elicitation)) |
| A2A | task state `INPUT_REQUIRED` ([spec](https://a2a-protocol.org/latest/specification/)) |
| AG-UI | interrupt with `responseSchema` and `expiresAt`; resume status resolved or cancelled ([interrupts](https://raw.githubusercontent.com/ag-ui-protocol/ag-ui/main/docs/concepts/interrupts.mdx)) |
| Slack / Feishu | interactive message (Block Kit on Slack; Feishu on the same design) |
| Review-type Asks | `accept` / `edit` / `respond` / `ignore`, the four responses of the archived LangChain Agent Inbox ([repo](https://github.com/langchain-ai/agent-inbox)) |

Baton converts the synchronous-looking MCP elicitation into a durable Ask. The kinds and the answer statuses (open, answered, declined, cancelled, expired) come from the agent-to-human survey and are a design proposal, not a standard. Planned (W2-4).

**9. A2A to MCP task-state mapping.** A2A has `SUBMITTED`, `AUTH_REQUIRED` and `REJECTED`, which MCP Tasks lacks. Baton keeps the superset as its source of truth and projects it to each protocol; the exact Baton state travels in a Baton-defined metadata field so that the projection can be reversed (whether and where the MCP Tasks extension allows such metadata is to be confirmed at implementation).

| Baton state | A2A (`TASK_STATE_*`) | MCP Tasks |
| --- | --- | --- |
| `submitted` | `SUBMITTED` | `working` (no counterpart) |
| `working` | `WORKING` | `working` |
| `input_required` | `INPUT_REQUIRED` | `input_required` |
| `auth_required` | `AUTH_REQUIRED` | `input_required` (no counterpart) |
| `completed` | `COMPLETED` | `completed` |
| `failed` | `FAILED` | `failed` |
| `canceled` | `CANCELED` | `cancelled` (spelling differs) |
| `rejected` | `REJECTED` | `failed` (no counterpart) |

Sources: A2A states <https://a2a-protocol.org/latest/specification/>; MCP states <https://modelcontextprotocol.io/extensions/tasks/overview>. Planned (W2-1 for A2A, W4 for MCP Tasks).

**10. Linear's ownership model.** The agent is the delegate and the human stays accountable (assigning an issue to an agent sets the agent as delegate, not assignee). Session states: `pending`, `active`, `awaitingInput`, `complete`, `error`, `stale`. An agent acknowledges a new session within about 10 s (Linear: emit a `thought` within 10 seconds). Sources: <https://linear.app/developers/agents>, <https://linear.app/developers/agent-interaction>. Linear's agent API is a Developer Preview. Planned.

**11. Approval evidence.**
- CHAP-style override events: a structured event with diff, reason and content hash, instead of an edit that vanishes into a chat thread. Source: <https://arxiv.org/abs/2606.09751>, <https://github.com/BrightbeamAI/chap>.
- A2H-style one-time approvals with idempotency (message idempotency, nonce binding, single-use approval links). Source: <https://www.twilio.com/en-us/blog/products/introducing-a2h-agent-to-human-communication-protocol>, <https://github.com/twilio-labs/Agent2Human>.
- An append-only log (`x_audit` already is one).
- Approver role: a human or an agent, by delegation from a human, never self-approval (see "Baton identity layer (planned)" and the R1 safeguards).

**12. Wake only the @mentioned agent, and speech-act intents.** Coral's model: threads are the unit of conversation, agents post with mentions, and an agent can suspend until it is mentioned in a thread (`coral_wait_for_mention`). Source: <https://docs.coralos.ai/concepts/threads>. Thread messages also carry an intent from the FIPA speech acts (communicative acts): `request`, `inform`, `propose`, `agree`, `refuse`. Sources: FIPA Communicative Act Library, SC00037J (fipa.org itself answered 403 to the check, so the archived copy was read: <https://web.archive.org/web/2023/http://www.fipa.org/specs/fipa00037/SC00037J.html>); background paper from the survey: <https://userpages.cs.umbc.edu/finin/papers/fss97.pdf>.

**13. Loop guard and budgets.**
- Lineage: record the call lineage with W3C `traceparent`. MCP defines `traceparent`, `tracestate` and `baggage` in `_meta` (SEP-414): <https://modelcontextprotocol.io/specification/2026-07-28/changelog>; trace context format: <https://www.w3.org/TR/trace-context/>. A2A's enterprise guidance says to propagate W3C Trace Context headers: <https://a2a-protocol.org/latest/topics/enterprise-ready/>. A separately named A2A tracing extension was not found in the official documentation, so none is assumed.
- Budgets: inspired by x402's `upto` scheme (a pre-authorized ceiling; the seller settles actual usage up to that cap, <https://github.com/x402-foundation/x402>) and AP2's open to closed mandates (open captures constraints, closed captures the final authorization, <https://ap2-protocol.org/index.md>).
- Human warnings at 80% and 100% of a budget, as in Rovo (Atlassian: admins are notified at 80% and 100% usage): <https://support.atlassian.com/jira-software-cloud/docs/collaborate-on-work-items-with-ai-agents/>.
- Agent output is private first and published after human confirmation (Rovo: agent activity is visible only to the person until they choose to share it, same page).
- Planned (W2-2).

**14. Discovery.** Publish and consume `/.well-known/ai-catalog.json`; A2A does not prescribe a standard API for curated registries ([discovery topic](https://a2a-protocol.org/latest/topics/agent-discovery/)). AI Catalog / ARD is a draft specification (updated 2026-07-31) that lists MCP servers and A2A agents from one fetch ([spec](https://specification.website/spec/agent-readiness/agentic-resource-discovery.md), [AAIF post](https://aaif.io/blog/before-agents-can-work-together-they-have-to-find-each-other)). Skills are classified with AGNTCY OASF, which annotates records with skills and domains: <https://github.com/agntcy/oasf>. Planned (W2-1).

**15. The inbox is not a separate product.** It stays a view over work objects, assignments and Asks. LangChain's Agent Inbox repository was archived on 2026-09-20: <https://github.com/langchain-ai/agent-inbox>. The current inbox (`fetch_inbox`, `wait_inbox`) is implemented; the Ask view of it is planned (W2-4, shown on the board in W3).

### WATCH

- **MCP Apps**: official extension for interactive UI (`ui://` resources); a possible later channel to show Baton cards inside MCP hosts. <https://modelcontextprotocol.io/extensions/apps/overview>.
- **A2UI**: current release v0.9.1, v1.0 is a candidate; revisit at v1.0. <https://a2ui.org/>.
- **Matrix**: a plausible optional bridge; MSC4564 (agent status in room messages) is an open proposal. <https://github.com/matrix-org/matrix-spec-proposals/pull/4564>.
- **Zed Agent Client Protocol**: editor to coding agent over JSON-RPC; JetBrains IDEs are listed as clients. Relevant only if Baton embeds in editors. <https://agentclientprotocol.com/overview/introduction>.
- **MCP Events (SEP-3415)**: still an open proposal (pull request, not merged). Ship a webhook-mode switch first, because ChatGPT already speaks webhook mode. <https://github.com/modelcontextprotocol/modelcontextprotocol/pull/3415>, <https://developers.openai.com/plugins/build/mcp-events>.
- **CHAP and A2H**: young drafts from single vendors or authors; revisit at v1.0 or when a second implementation exists. <https://github.com/BrightbeamAI/chap>, <https://github.com/twilio-labs/Agent2Human>.

### IGNORE

- **MCP-UI**: its packages now implement MCP Apps. <https://github.com/MCP-UI-Org/mcp-ui>.
- **ChatKit**: OpenAI's UI kit tied to its backend, not an interoperable protocol. <https://github.com/openai/chatkit-js>.
- **XMPP**: the survey found no 2026 agent-specific standard. <https://xmpp.org/about/compliance-suites/>.
- **ActivityPub**: public social federation, no approval semantics. <https://www.w3.org/TR/activitypub/>.
- **JMAP and email**: email is out of scope. <https://jmap.io/spec.html>.
- **ChatGPT Projects and Claude Projects**: no open API found. <https://support.claude.com/en/articles/9517075-what-are-projects>; see also "API reality check" below.
- **IBM ACP**: merged into A2A; the repository is archived (2025-08-27). <https://github.com/i-am-bee/acp>.

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
