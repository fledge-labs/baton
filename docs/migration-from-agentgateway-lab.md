# Migration: agentgateway-lab → Baton

Source: an existing private test deployment. Copy only reusable first-party code; leave that deployment unchanged.
Target: this repo, `fledge-labs/baton`, public, Apache-2.0.

## Goal
Anyone can clone Baton and bring up the same stack on their own machine with one documented command. The stack is: agentgateway front door → MCP tools (submit_task, get_task, list_tasks, fetch_inbox) → A2A bridge → Codex worker → MCP Agent Mail completion report, plus the board and per-CLI onboarding.

## Layout
- `deploy/`: compose file, gateway config, pinned versions/digests as today, `.env.example` with placeholders only.
- `bridge/`: the A2A bridge and the backend that maps gateway identities to Agent Mail senders.
- `adapters/`: `mcp_stdio.py`, `launch_mcp.sh`, generic over `<agent-id>` and `<TOKEN_FILE>`.
- `onboarding/`: one short doc per CLI (Codex, agy, Cline, Devin, Claude Code, OpenCode) with the config snippet. Note that remote clients connect over SSH-wrapped stdio, so the token never leaves the host.
- `scripts/`: `tokens.py` (issue, revoke, rotate) and the end-to-end acceptance test.
- `board/`: the task board.
- `docs/`: architecture, a security model (tokens, audit, revocation) and a THIRD_PARTY.md listing each dependency, its version and its license.

## Must remove (do not publish)
- Tokens, digests of real tokens, registry contents, Agent Mail databases, `secrets/`, `evidence/` receipts.
- Private deployment hostnames, routable IP addresses, tunnel details, personal filesystem paths, personal email addresses and account names.
- Use placeholders instead: `example.com`, `<HOST>`, `<TOKEN_FILE>`, `$HOME`.

## Third-party code
Reference agentgateway, MCP Agent Mail, the A2A SDK and Codex as pinned dependencies or images. Do not vendor their source. Keep each license as-is and list it in docs/THIRD_PARTY.md. If any file must be copied, keep its header and license, and note it in THIRD_PARTY.md.

## Acceptance
1. `deploy/` comes up on a clean Linux box with `docker compose up` and the documented token issuance. The e2e test then passes: submit → completed → Mail report on the same thread → audit entries linked by task id.
2. A secret scan (gitleaks or trufflehog) over the full git history is clean, and grep finds none of the real hostnames, IPs or paths above.
3. Every source file carries `SPDX-License-Identifier: Apache-2.0` (copied third-party files keep their own). Every commit is signed off (`git commit -s`).
4. The lab deployment keeps running unchanged. This migration copies; it never moves or edits the live lab.

The claim/lease and overlap-check layer that was out of scope for this step is now implemented; see docs/api.md.
