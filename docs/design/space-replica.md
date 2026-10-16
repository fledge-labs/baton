# Design brief: replace Agent Mail with an open replica of ChatGPT Space

Status: approved direction (owner, 2026-10-16). MCP Agent Mail is removed from Baton. Its license rider restricts OpenAI, Anthropic and parties acting for them, which conflicts with "free for everyone".

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

## What it replaces
Agent Mail's inbox, threads and file reservations become comment threads plus mentions (the inbox is "mentions and assignments for me") plus `x_claim`/`x_scope`. The MCP tools stay: `submit_task`, `get_task`, `list_tasks`, `fetch_inbox`. They gain `claim`, `renew`, `handoff`, `comment`, `mention`.

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
