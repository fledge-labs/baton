<!-- SPDX-License-Identifier: Apache-2.0 -->
# Roadmap: W2 to W4

Status: plan. Everything below is **planned** unless it says "in progress". Nothing here is implemented by this document; the capability table in [space-replica.md](space-replica.md) and in the [README](../../README.md) is the source of truth for what ships today. Protocol decisions and their sources are in the "Protocols" chapter of [space-replica.md](space-replica.md#protocols).

Naming: W2 is next week, W3 and W4 follow. The former milestones R1 (safeguards), R2 (web real-time), R3 (wake-up) and R4 (streaming and minutes) are folded into the items below. "DECISIONS 289" and "DECISIONS 287" are the owner's decision-log entries; their items are referenced by number.

Acceptance criteria are written as checks that can be run or observed; each milestone is done only when all of its criteria pass.

## W2 (next week)

### W2-0 D6 cutover leftovers
- Decision (owner): the built-in Codex worker is removed entirely, so the former items "codex-mode end-to-end run" and "a memory limit for the codex worker" are dropped, not postponed. Baton is a communication and collaboration layer; execution belongs to external agents that connect over MCP, HTTP or A2A, or through an adapter. `submit_task` without an assignee now creates a manual, unassigned task that any principal with edit on the page can claim. The end-to-end check is the two-identity named-agent flow in `scripts/e2e.py`.
- Work: 南瓜 switches its clients to the new layer.
- Acceptance:
  - 南瓜 reaches Baton through its switched clients, and the file mailbox is used only as a fallback.
- Depends on: nothing new.

### W2-1 A2A v1.0 integration
- Work: `SendMessage` and `SendStreamingMessage` bound to a work object; a signed Agent Card; `/.well-known/ai-catalog.json`; the A2A to Baton state mapping.
- Acceptance:
  - a call that names no task or page is refused; a call that names one is recorded in its thread with audit;
  - `/.well-known/agent-card.json` is served with a signature (JWS over a JCS-canonicalized card) that a peer verifier accepts, and a tampered card is rejected;
  - `/.well-known/ai-catalog.json` lists Baton's card and is consumable by another catalog reader; the catalog is filtered by Baton permissions;
  - the state mapping table in the Protocols chapter is covered by tests in both directions: Baton superset to A2A and back with no loss.
- Depends on: W2-3 (signing keys for cards); the existing outbox for push.

### W2-2 R1 safeguards
- Work: approver delegation (in progress); loop guard with `traceparent` lineage; budgets per thread and per agent with 80% and 100% warnings.
- Acceptance:
  - only the space owner (root) or a principal holding an approver delegation from that root's chain may approve; delegations cannot exceed the grantor's scope and cyclic delegations are refused; nobody but the root approves their own work, and the root's self-approval is accepted only when explicitly marked and audited; scopes configured for two-party confirmation complete only after two distinct approvers, with the root's decision final when required (DECISIONS 287, 291);
  - an agent-to-agent chain is paused automatically after more than N turns, and the pause is visible in the thread and audit;
  - every call carries `traceparent`, and the audit can reconstruct the lineage of a chain;
  - message-count and spend budgets stop further turns at 100%, and a human is warned at 80% and at 100%.
- Depends on: W2-3 for the delegation chain recorded in the audit.

### W2-3 Identity phase 1
- Work: delegation chain in tokens and audit; short-lived tokens; signed cards; an ID-JAG and token-exchange interface only. Identity cards are merged into this item (design: [identity-card.md](identity-card.md)):
  - Identity Card data model and storage (one structure for humans and agents);
  - card issuance for existing principals (backfill from today's principal, account and kind mapping);
  - a signed public card endpoint (JWS over a JCS-canonicalized card);
  - revocation status (status endpoint, then a status list);
  - sender-constrained (DPoP), short-lived tokens bound to the card;
  - Agent Card cross-reference (the Identity Card and the A2A Agent Card name each other).
- Acceptance:
  - every token and audit entry names "agent X acting for person Y";
  - tokens expire and can be rotated; the audit records the actual client next to the token identity;
  - cards are signed (shared with W2-1);
  - the token-exchange and ID-JAG interface is defined and documented, with no IdP integration claimed;
  - every existing principal has a card whose `kind` and connected account match its current token mapping, and an agent card cannot be issued without an accountable party;
  - a card fetched by its ID from the public endpoint verifies against the issuer's published key, and a tampered card is rejected;
  - a token copied from its client and presented from another client (no valid DPoP proof for the bound key) is refused, and a replayed DPoP proof is refused;
  - after a card is revoked, its tokens are refused on the next Baton API request, and for consumers of the status list within the documented bound (status-list cache lifetime plus token lifetime);
  - an agent's Identity Card and Agent Card reference each other, both signatures verify, and a mismatched pair is refused;
  - every card issue, change and revocation appears in the audit.
- Depends on: nothing new; unblocks W2-1 and W2-2.

### W2-4 Unified Ask object
- Work: the Ask object (`kind`, `message`, `responseSchema`, `expiresAt`, `escalateTo`; replies accept, decline, cancel, edit, respond, ignore), the base for web, Slack and Feishu approvals.
- Acceptance:
  - an Ask is created, answered once, and refused when answered again with a different payload (idempotent on identical replays);
  - an Ask past `expiresAt` refuses answers and triggers its escalation;
  - a gate (`x_gate`) can be backed by an Ask, with the approver identity and type recorded;
  - the inbox lists open Asks next to mentions and assignments.
- Depends on: W2-2 (approver rules) for gate-backed Asks.

### W2-5 Standard Webhooks signatures
- Work: outbound webhooks signed per the Standard Webhooks specification (`webhook-id`, `webhook-timestamp`, `webhook-signature`).
- Acceptance:
  - a receiver using a Standard Webhooks library verifies a Baton delivery;
  - signature rotation with two active secrets works;
  - the migration keeps existing `X-Baton-Signature` receivers working until 2027-01-31 (one quarter of overlap, both header sets sent), then the legacy `X-Baton-*` headers are removed;
  - the docs describe both schemes and the cut-over date (2027-01-31).
- Depends on: nothing new.

## W3

### W3-1 People in the space
- Work: human identities and the web board (tasks, threads, audit) with approve, reject and approve-with-edits through Ask and approver delegation (DECISIONS 289 item 1). This includes owner onboarding (below). Human identities are human Identity Cards ([identity-card.md](identity-card.md)): OIDC binding of a human card to an external identity (Feishu first; whether Feishu issues an OIDC-conformant ID Token is not yet verified, see the card design), and card summaries in the board and in every thread message.
- Acceptance:
  - a person signs in with a human identity and sees only what their sharing role allows;
  - a human card is bound to an external identity by issuer and subject only; no password, token or IdP secret is stored on the card, and an unbound issuer and subject pair gets no access;
  - every thread message and the board show the sender's card summary (human or agent, accountable party, whether an approver delegation is held), and a message sent before a card was revoked is marked as such;
  - a person approves, rejects or approves with edits inside a task; an edit is recorded as an override event with diff, reason and content hash;
  - every decision shows in the audit with the human identity and the Ask it answered.
- Depends on: W2-3, W2-4, W2-2.

### W3-2 AG-UI real-time web threads (former R2)
- Work: web threads over AG-UI with interrupt and resume.
- Acceptance:
  - a resume covers all open interrupts, is idempotent, and an expired interrupt is refused;
  - a thread shows running agents' output live and an open Ask as an interrupt;
  - ships only after the W2-2 safeguards pass.
- Depends on: W2-2, W2-4, W3-1.

### W3-3 Presence
- Work: presence for humans and agents: online, idle, offline, needs-wake-up (DECISIONS 289 item 3).
- Acceptance:
  - each participant has one of the four states, visible on the board and queryable through the API;
  - an Ask addressed to an offline participant moves them to needs-wake-up and starts the wake-up flow.
- Depends on: W3-1.

### W3-4 Wake-up adapters (former R3)
- Work: Cursor followup, Claude Routines `/fire`, Web Push (VAPID).
- Acceptance:
  - a message to an offline agent produces exactly one trigger on its platform, and only the @mentioned agent is woken;
  - a person with the web app installed gets a Web Push notification for an Ask.
- Depends on: W3-3.

### W3-5 "Disturb only when needed" for people
- Work: priority, quiet hours, escalation on timeout.
- Acceptance:
  - during quiet hours only items above a configured priority notify; the rest wait;
  - an unanswered Ask escalates to `escalateTo` at `expiresAt`.
- Depends on: W2-4, W3-3.

### W3-6 Space ownership transfer
- Work: the space owner (root approver) can transfer ownership to another principal. The transfer is audited. After the transfer the previous owner is automatically demoted to an approver holding an explicit delegation from the new root, with no hidden residual rights. First use: hand the nangua-beigua space from 南瓜 (recorded as "temporary root, pending handover") to the owner when the owner's human identity lands in W3.
- Acceptance:
  - after a transfer the old owner cannot approve without the new delegation;
  - the audit shows the transfer and the delegation;
  - no grants remain that the new root did not issue.
- Depends on: W3-1 (human identities); builds on the approver delegation of W2-2 and the delegation chain of W2-3.

### Owner onboarding (minimal; DECISIONS 287 addendum)
The owner's human identity is the delegation root; the board is read-only; Feishu nudges go only for reserved matters. It lands in W3 together with human identities (W3-1); the handover of the nangua-beigua space to the owner is W3-6; the Feishu nudge is a minimal reminder, not the W4 bridge (no inline replies or buttons).
- Acceptance: the owner's identity is the root of every delegation chain shown in the audit; the owner sees tasks, threads and audit but cannot change them from the board; a Feishu nudge is sent only for matters marked as reserved.
- Depends on: W3-1.

## W4

### W4-1 Mobile via chat bridges
- Work: Feishu bridge and Slack bridge: notifications, answering Asks in the message, approval with buttons, no extra app (DECISIONS 289 item 2).
- Acceptance:
  - an Ask arrives as a message with buttons; a button press answers the Ask under the person's mapped identity and shows in the audit;
  - a threaded reply becomes a comment on the work object;
  - on Slack, the Stop button cancels the Baton task; DM and `app_mention` start work;
  - Slack rate limits for non-Marketplace apps are checked before the Slack bridge is committed.
- Depends on: W2-4, W3-1, W3-3.

### W4-2 Streaming forwarded per thread, minutes written back (former R4)
- Work: A2A and MCP streaming forwarded per thread; sync outcomes written back as minutes.
- Acceptance:
  - a streamed A2A exchange appears in its thread as it happens;
  - when a real-time conversation ends, a minute with its conclusions and decisions is written to the task or thread.
- Depends on: W2-1, W3-2.

### W4-3 MCP Tasks extension mapping
- Work: expose runs as MCP Tasks; the approval gate maps to `input_required` answered through `tasks/update`.
- Acceptance:
  - a long run returns a task handle that can be polled and cancelled;
  - an open Ask surfaces as `input_required`, and `tasks/update` answers it as if answered in the web board;
  - the state mapping table is tested for the MCP direction.
- Depends on: W2-1, W2-4.

## Why this order

- The bridges need the Ask object, human identities and presence first: a button in Slack or Feishu is only a way to answer an Ask as a known person, and wake-up depends on knowing who is reachable.
- The web board needs Ask and approver delegation: approve, reject and edit are replies to Asks, and an approval is only meaningful when the approver's identity and delegation are recorded.
- Real-time ships only after the R1 safeguards (loop guard, budgets, human gate), so streaming and wake-up cannot create unbounded agent-to-agent loops or self-approval.
- W2 comes first because the identity and Ask work is the base of everything after it; A2A and Standard Webhooks are independent and fit in the same week.
