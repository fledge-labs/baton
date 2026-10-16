# Why agents collide, and what other industries already learned

## Four causes (observed in one day of running parallel coding agents)
1. **Two holders for one job.** Two lanes fixed the same review finding, one of them with the opposite design. Nobody was the single owner of that finding.
2. **Undeclared scope.** Two lanes each added a loader for the same credential, and two lanes took the same migration number. Nobody said "I am touching this area" before starting.
3. **Stale base.** Lanes started from an old commit and could not see designs already settled on the integration branch.
4. **Each part is right, the sum is wrong.** Git merged cleanly, but the combination failed: one change sent a batch that another change had just learned to refuse. Only testing the merged result catches this.

Agents collide more than people do. They run with high parallelism, they do not chat with each other, and nobody calls across the room "I am in that file".

## Mechanisms that already work elsewhere

| Mechanism | Where it comes from | Cause it fixes | Baton feature |
|---|---|---|---|
| Token / tablet working | Single-track railways: only one token per section; only the train holding it may enter | 1 | `x_claim` with lease (hence the name Baton) |
| Lock-out/tag-out, permit-to-work | Industrial maintenance: you put your own lock on the equipment before working on it | 1, 2 | `x_claim` + `x_scope`: scope is locked while held |
| Clash detection | Building information modelling: pipes and beams are checked for clashes in the model before construction | 2 | Overlap check at claim time; overlapping scopes are refused |
| CODEOWNERS | Code hosting: every path has a named owner | 2 | `x_scope` declared by path, module or migration number |
| Merge queue | GitHub merge queue, Bors, Uber SubmitQueue: each change is tested on top of the latest main plus the queue ahead of it | 3, 4 | `x_gate` runs on the current base, not the lane's own base |
| Leases and compare-and-swap | etcd/Chubby leases; optimistic locking in databases | 1 | Leases expire unless renewed; holder changes are compare-and-swap |
| Critical path / precedence | Project management (PERT/CPM) | ordering collisions | `x_graph`: a task is claimable only after its dependencies pass their gate |

Real-time co-editing (OT/CRDT, as in Google Docs) keeps every keystroke but cannot keep two pieces of code logically compatible. For code, the order is: claim first, merge later, and test before merging.

## How a construction site runs work in order

Railways teach "do not collide". Construction sites teach "work in order". A crew may enter only after its work face is opened.

| On the site | For agents | Baton feature |
|---|---|---|
| Site readiness (water, power, access, levelled ground) before any crew arrives | Base commit, environment, fixtures, identities and tokens are ready before work starts | `x_ready` (a preparation task gates the construction tasks) |
| Setting out: lines are marked on the ground, so every crew knows its area | Each lane declares the files, modules and migration numbers it will touch | `x_scope` |
| Work-face handover: the finished trade hands the face to the next one in writing, and both sign | A hand-off carries base commit, evidence and acceptance; it completes only when the receiver confirms | `x_handoff` |
| Concealed-work inspection: rebar and pipes are inspected before they are closed into walls | Anything others will build on (e.g. a database migration) must pass its gate before the next task builds on top | `x_gate` + `x_graph` |
| Trade sequencing: trades on separate faces work in parallel; trades sharing a face take turns | Non-overlapping scopes run in parallel; overlapping ones queue | overlap check + `x_graph` |
| Daily coordination meeting: progress, clashes, who enters which area tomorrow | Live task states and the audit log replace the meeting | `x_audit` |

Each kind of failure maps to a skipped step:
- duplicated migration numbers: no setting out;
- work on a stale base: no site readiness;
- a change that waits forever for an environment the test site does not have: the handover did not check site conditions;
- a regression that only appears after merging: concealed work was covered before inspection.

On real sites most disputes come from unclear handovers and skipped concealed-work inspections, because the boundary of responsibility is unclear. Baton's audit log is the signed handover sheet for every step.

## The point
Collisions are not new and not specific to AI. Railways, plants, construction sites and large monorepos all solved them with the same few moves:
- one token per section;
- lock before you touch;
- check clashes before you build;
- test on the latest base before you merge.

Baton turns these proven human rules into rules that agents follow automatically. The highest-value one is clash detection at claim time: most real collisions can be refused at the moment the work is handed out.
