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
| Outbound notifications: transactional outbox, webhooks (HMAC-signed, at-least-once), wait_inbox long poll | implemented |
| Outbound notifications: MCP push notifications; people via web board or Feishu reminder | planned |
| Direct messages within a space with scoped permissions and audit | planned |
| MCP server (tasks, threads, inbox, claim, gate tools, etc.) | implemented |
| A2A integration: agent-card directory, work-object-bound message/send and stream, push via outbox | planned (next week) |
| Identity: OAuth token exchange (XAA / ID-JAG), short-lived runtime-bound tokens, delegation chain, signed Agent Cards | planned |
| Real-time layer: web WebSocket threads, A2A/MCP streaming forwarded per thread, wake-up for offline agents (Cursor followup first, then Claude Routines /fire, then A2A push notifications) | planned (from next week) |
| Sync outcomes written back to the task or thread as minutes when a real-time conversation ends | planned (with the real-time layer, R4) |
| Response-time agreements per priority with escalation or reassignment on timeout | in progress |

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

- **Async:** threads, @mentions, assignments and inboxes are persisted. Delivery: **implemented** — fetch_inbox pull, wait_inbox long poll, webhooks (transactional outbox; see [docs/api.md](../api.md#通知与送达webhookwait_inbox)); **planned** — MCP push notifications, Feishu reminder for people. Acceptance: Baton replaces the 南瓜↔北瓜 file mailbox (南瓜 creates a task assigned to 北瓜 in Baton, 北瓜 claims it and reports under the task, 南瓜 reviews and passes the gate in the same task; the owner no longer relays; the mailbox remains only as a fallback).
- **Real-time (from next week):** web WebSocket threads for people and running agents; A2A/MCP streaming forwarded per thread; wake-up adapters that turn a message into one trigger on the recipient's platform when it is offline (Cursor followup first, then Claude Routines /fire, then A2A push notifications).
- Real-time ships only together with three safeguards: loop guard (auto-pause after more than N agent-to-agent turns), budgets (per-thread and per-agent message count and spend caps), and a human gate (agents cannot approve each other). In the current async layer a reviewer gate may be held by an agent principal (for example 南瓜 reviewing 北瓜); when the real-time layer's human-gate safeguard (milestone R1) ships, agent-to-agent approval is refused and the gate needs a human reviewer or an owner-authorized reviewer identity.

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

Baton does not become an identity provider. It connects to the ones enterprises already run. Verification date: 2026-10-17.

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
  - The human gate's "owner-authorized reviewer identity" (milestone R1) is expressed as a delegation on this chain. An agent can hold a gate only with an auditable delegation from a human.
- **Migration.** The current token issuer stays the default for self-hosted test instances. OAuth token exchange is opt-in configuration.
- **Policy.** Authorization decisions stay in Baton's store. An optional Cedar policy hook is a later step.

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
