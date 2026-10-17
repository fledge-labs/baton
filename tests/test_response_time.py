# SPDX-License-Identifier: Apache-2.0
"""Response-time agreements (communication principle 3) on actual PostgreSQL.

Every identity, prompt and comment is synthetic. Deadlines are moved with database
statements instead of sleeping, so the suite stays fast and deterministic.
"""

import asyncio
import hashlib
import json
import threading
import uuid
from dataclasses import asdict

import psycopg
import pytest

from adapters import mcp_stdio
from bridge import space_store
from bridge.space_store import DEFAULT_RESPONSE_MINUTES, ESCALATION_LOCK_ID, SERVICE_ACTOR, Actor, SpaceStore, _canonical, parse_response_minutes
from test_bridge import api, op, shared_page  # noqa: F401  (api is a fixture)
from test_notify import events, execute, notes, query

PROMPT = "synthetic-response-prompt"


@pytest.fixture
def team(api):
    client, call, identities, tokens, registry, records = api
    space, page = shared_page(call, identities)
    return call, identities, space, page


def make_task(call, identities, page, assignee="worker", **extra):
    if assignee:
        extra["assignee_principal"] = identities[assignee]["agent"]
    return call("owner", "create_task_block", operation_id=op(), page_id=page["id"], prompt=PROMPT,
                scopes=[{"kind": "path", "value": "src/response/" + uuid.uuid4().hex[:8]}],
                reviewers=[identities["reviewer"]["agent"]], **extra)


def minutes_left(store, task_id):
    row = query(store, "SELECT extract(epoch FROM respond_by-clock_timestamp())/60 AS m FROM baton.tasks WHERE id=%s", (task_id,))[0]
    return None if row["m"] is None else float(row["m"])


def make_overdue(store, task_id, minutes=5):
    execute(store, "UPDATE baton.tasks SET respond_by=clock_timestamp()-%s*interval '1 minute' WHERE id=%s", (minutes, task_id))


def column(store, task_id, name):
    # name is a literal chosen by this file, never request input.
    return query(store, f"SELECT {name} AS value FROM baton.tasks WHERE id=%s", (task_id,))[0]["value"]


def audit_events(store, task_id):
    return [row["event"] for row in query(store, "SELECT event FROM baton.x_audit WHERE task_id=%s ORDER BY time,id", (task_id,))]


def overdue_rows(store, task_id=None):
    return query(store, "SELECT recipient,event FROM baton.x_notify WHERE event='task.overdue' AND (%s::uuid IS NULL OR object_id=%s::uuid) ORDER BY seq",
                 (task_id, task_id))


def close_to(value, expected, slack=1.5):
    return value is not None and expected - slack <= value <= expected


def test_deadline_is_set_on_assignment_according_to_priority(team, pg_store):
    call, identities, space, page = team
    for priority, minutes in (("urgent", 20), ("normal", 60), ("low", 1440)):
        task = make_task(call, identities, page, priority=priority)
        assert task["priority"] == priority and task["responded_at"] is None and task["escalated_at"] is None
        assert task["overdue"] is False and task["respond_by"]
        assert close_to(minutes_left(pg_store, task["task_id"]), minutes)
    default = make_task(call, identities, page)
    assert default["priority"] == "normal" and close_to(minutes_left(pg_store, default["task_id"]), 60)
    assert default["escalate_to"] == identities["owner"]["agent"] and default["fallback_assignee"] is None
    assert call("worker", "get_task", task_id=default["task_id"])["respond_by"] == default["respond_by"]

    submitted = call("owner", "submit_task", operation_id=op(), idempotency_key="response-submit-" + uuid.uuid4().hex[:8], prompt=PROMPT,
                     page_id=page["id"], assignee_principal=identities["worker"]["agent"], priority="urgent")
    assert close_to(minutes_left(pg_store, submitted["task_id"]), 20)


def test_tasks_without_an_assignee_have_no_deadline_until_assigned(team, pg_store):
    call, identities, space, page = team
    queued = call("owner", "submit_task", operation_id=op(), idempotency_key="response-auto-" + uuid.uuid4().hex[:8], prompt=PROMPT,
                  page_id=page["id"], priority="urgent")
    assert queued["priority"] == "urgent" and queued["respond_by"] is None and queued["overdue"] is False
    assigned = call("owner", "assign_task", operation_id=op(), task_id=queued["task_id"], target_principal=identities["worker"]["agent"])
    assert close_to(minutes_left(pg_store, queued["task_id"]), 20) and assigned["responded_at"] is None


def test_assign_and_handoff_to_a_new_assignee_restart_the_clock(team, pg_store):
    call, identities, space, page = team
    task = make_task(call, identities, page)
    make_overdue(pg_store, task["task_id"])
    execute(pg_store, "UPDATE baton.tasks SET escalated_at=clock_timestamp() WHERE id=%s", (task["task_id"],))
    call("owner", "assign_task", operation_id=op(), task_id=task["task_id"], target_principal=identities["replacement"]["agent"])
    assert close_to(minutes_left(pg_store, task["task_id"]), 60)
    assert column(pg_store, task["task_id"], "escalated_at") is None and column(pg_store, task["task_id"], "responded_at") is None

    before = column(pg_store, task["task_id"], "respond_by")
    call("owner", "assign_task", operation_id=op(), task_id=task["task_id"], target_principal=identities["replacement"]["agent"])
    assert column(pg_store, task["task_id"], "respond_by") == before, "the same assignee keeps the original window"

    claimed = call("replacement", "claim", operation_id=op(), task_id=task["task_id"])
    assert claimed["responded_at"]
    make_overdue(pg_store, task["task_id"])
    handed = call("replacement", "handoff", operation_id=op(), task_id=task["task_id"], epoch=claimed["epoch"],
                  target_principal=identities["worker"]["agent"])
    assert close_to(minutes_left(pg_store, task["task_id"]), 60) and handed["responded_at"] is None
    assert call("worker", "comment", operation_id=op(), page_id=page["id"], body="on it", thread_id=task["task_id"])
    assert call("worker", "get_task", task_id=task["task_id"])["responded_at"], "the new holder answers in the thread"


def test_claim_and_assignee_or_holder_comment_set_responded_at_once(team, pg_store):
    call, identities, space, page = team
    by_claim = make_task(call, identities, page)
    claimed = call("worker", "claim", operation_id=op(), task_id=by_claim["task_id"])
    first = claimed["responded_at"]
    assert first and call("worker", "get_task", task_id=by_claim["task_id"])["overdue"] is False
    call("worker", "comment", operation_id=op(), page_id=page["id"], body="later note", thread_id=by_claim["task_id"])
    assert column(pg_store, by_claim["task_id"], "responded_at").isoformat() == first, "only the first response counts"

    by_comment = make_task(call, identities, page)
    task_id = by_comment["task_id"]
    for label in ("owner", "reviewer", "replacement"):
        call(label, "comment", operation_id=op(), page_id=page["id"], body="question", thread_id=task_id)
        assert column(pg_store, task_id, "responded_at") is None, label + " is not the assignee or holder"
    assert column(pg_store, task_id, "state") == "queued"
    call("worker", "comment", operation_id=op(), page_id=page["id"], body="acknowledged", thread_id=task_id)
    assert column(pg_store, task_id, "responded_at") is not None
    assert column(pg_store, task_id, "state") == "queued", "a comment is a response, not a claim"

    unrelated = make_task(call, identities, page)
    other = call("owner", "comment", operation_id=op(), page_id=page["id"], body="elsewhere")
    call("worker", "comment", operation_id=op(), page_id=page["id"], body="same page, other thread", thread_id=other["thread_id"])
    assert column(pg_store, unrelated["task_id"], "responded_at") is None


def test_overdue_creates_exactly_one_notification_and_audit(team, pg_store):
    call, identities, space, page = team
    task = make_task(call, identities, page)
    task_id = task["task_id"]
    assert pg_store.escalate_overdue(SERVICE_ACTOR)["escalated"] == []
    make_overdue(pg_store, task_id)
    assert call("owner", "get_task", task_id=task_id)["overdue"] is True

    first = pg_store.escalate_overdue(SERVICE_ACTOR)
    assert first["escalated"] == [task_id] and first["reassigned"] == [] and first["skipped"] is False
    assert pg_store.escalate_overdue(SERVICE_ACTOR)["escalated"] == []
    assert [row["recipient"] for row in overdue_rows(pg_store, task_id)] == [identities["owner"]["agent"]]
    assert audit_events(pg_store, task_id).count("task.escalated") == 1
    assert events(notes(call, "owner"))[-1] == "task.overdue"
    assert "task.overdue" not in events(notes(call, "worker")), "the late assignee is not told by this event"

    view = call("owner", "get_task", task_id=task_id)
    assert view["escalated_at"] and view["overdue"] is True and view["assignee"] == identities["worker"]["agent"]
    assert PROMPT not in json.dumps(call("owner", "list_audit", task_id=task_id)), "audit never carries prompt text"


def test_escalation_goes_to_escalate_to_and_the_owner(team, pg_store):
    call, identities, space, page = team
    task = make_task(call, identities, page, escalate_to=identities["reviewer"]["agent"])
    assert task["escalate_to"] == identities["reviewer"]["agent"]
    make_overdue(pg_store, task["task_id"])
    pg_store.escalate_overdue(SERVICE_ACTOR)
    pg_store.escalate_overdue(SERVICE_ACTOR)
    assert sorted(row["recipient"] for row in overdue_rows(pg_store, task["task_id"])) == sorted(
        [identities["reviewer"]["agent"], identities["owner"]["agent"]])
    assert events(notes(call, "reviewer"))[-1] == "task.overdue"

    hidden = make_task(call, identities, page, escalate_to=identities["stranger"]["agent"])
    make_overdue(pg_store, hidden["task_id"])
    pg_store.escalate_overdue(SERVICE_ACTOR)
    assert [row["recipient"] for row in overdue_rows(pg_store, hidden["task_id"])] == [identities["owner"]["agent"]], \
        "a recipient without page access gets no row"


def test_responded_unassigned_and_not_yet_due_tasks_are_not_escalated(team, pg_store):
    call, identities, space, page = team
    answered = make_task(call, identities, page)
    call("worker", "comment", operation_id=op(), page_id=page["id"], body="seen", thread_id=answered["task_id"])
    make_overdue(pg_store, answered["task_id"])
    early = make_task(call, identities, page)
    auto = call("owner", "submit_task", operation_id=op(), idempotency_key="response-none-" + uuid.uuid4().hex[:8], prompt=PROMPT, page_id=page["id"])
    assert answered["task_id"] not in pg_store.escalate_overdue(SERVICE_ACTOR)["escalated"]
    assert call("owner", "get_task", task_id=answered["task_id"])["overdue"] is False
    assert overdue_rows(pg_store, early["task_id"]) == [] and overdue_rows(pg_store, auto["task_id"]) == []
    assert overdue_rows(pg_store, answered["task_id"]) == []


def test_reassignment_to_fallback_only_when_unclaimed_and_fallback_has_edit(team, pg_store):
    call, identities, space, page = team
    fallback = identities["replacement"]["agent"]
    task = make_task(call, identities, page, fallback_assignee=fallback)
    task_id = task["task_id"]
    assert task["fallback_assignee"] == fallback
    make_overdue(pg_store, task_id)
    result = pg_store.escalate_overdue(SERVICE_ACTOR)
    assert result["escalated"] == [task_id] and result["reassigned"] == [task_id]

    view = call("replacement", "get_task", task_id=task_id)
    assert view["assignee"] == fallback and view["execution_mode"] == "manual" and view["escalated_at"] is None
    assert view["responded_at"] is None and view["overdue"] is False and close_to(minutes_left(pg_store, task_id), 60)
    trail = audit_events(pg_store, task_id)
    assert trail.count("task.escalated") == 1 and trail.count("task.reassigned") == 1
    assert trail.index("task.escalated") < trail.index("task.reassigned")
    assert "task.assigned" in events(notes(call, "replacement")) and events(notes(call, "owner"))[-1] == "task.overdue"
    assert [row["recipient"] for row in overdue_rows(pg_store, task_id)] == [identities["owner"]["agent"]]

    assert pg_store.escalate_overdue(SERVICE_ACTOR)["escalated"] == []
    assert call("replacement", "claim", operation_id=op(), task_id=task_id)["holder"] == fallback
    assert call("worker", "claim", 403, operation_id=op(), task_id=task_id)["code"] == "forbidden", "the first assignee lost the task"

    stranger = make_task(call, identities, page, fallback_assignee=identities["stranger"]["agent"])
    make_overdue(pg_store, stranger["task_id"])
    result = pg_store.escalate_overdue(SERVICE_ACTOR)
    assert result["escalated"] == [stranger["task_id"]] and result["reassigned"] == []
    assert call("owner", "get_task", task_id=stranger["task_id"])["assignee"] == identities["worker"]["agent"]

    viewer = make_task(call, identities, page, fallback_assignee=identities["reviewer"]["agent"])
    call("owner", "share", operation_id=op(), resource_type="page", resource_id=page["id"], subject_type="person",
         subject_id=identities["reviewer"]["account"], role="comment")
    execute(pg_store, "DELETE FROM baton.grants WHERE resource_type='space' AND subject_id=%s", (identities["reviewer"]["account"],))
    make_overdue(pg_store, viewer["task_id"])
    result = pg_store.escalate_overdue(SERVICE_ACTOR)
    assert result["escalated"] == [viewer["task_id"]] and result["reassigned"] == [], "comment access is not enough"

    gone = make_task(call, identities, page, fallback_assignee="no-such-principal")
    make_overdue(pg_store, gone["task_id"])
    assert pg_store.escalate_overdue(SERVICE_ACTOR)["reassigned"] == [], "an unknown fallback is skipped"
    assert call("owner", "get_task", task_id=gone["task_id"])["escalated_at"]


def test_second_deadline_for_the_new_assignee_escalates_once_more_without_looping(team, pg_store):
    call, identities, space, page = team
    task = make_task(call, identities, page, fallback_assignee=identities["replacement"]["agent"])
    task_id = task["task_id"]
    make_overdue(pg_store, task_id)
    assert pg_store.escalate_overdue(SERVICE_ACTOR)["reassigned"] == [task_id]
    make_overdue(pg_store, task_id)
    result = pg_store.escalate_overdue(SERVICE_ACTOR)
    assert result["escalated"] == [task_id] and result["reassigned"] == [], "the fallback is already the assignee"
    assert audit_events(pg_store, task_id).count("task.escalated") == 2
    assert pg_store.escalate_overdue(SERVICE_ACTOR)["escalated"] == []


def test_claimed_tasks_are_never_escalated_or_reassigned(team, pg_store):
    call, identities, space, page = team
    task = make_task(call, identities, page, fallback_assignee=identities["replacement"]["agent"])
    task_id = task["task_id"]
    claimed = call("worker", "claim", operation_id=op(), task_id=task_id)
    # Defeat the responded guard so only the claim protects the task.
    execute(pg_store, "UPDATE baton.tasks SET responded_at=NULL,respond_by=clock_timestamp()-interval '10 minutes' WHERE id=%s", (task_id,))
    assert pg_store.escalate_overdue(SERVICE_ACTOR)["escalated"] == []
    view = call("worker", "get_task", task_id=task_id)
    assert view["assignee"] == identities["worker"]["agent"] and view["holder"] == identities["worker"]["agent"]
    assert view["epoch"] == claimed["epoch"] and view["escalated_at"] is None
    assert "task.reassigned" not in audit_events(pg_store, task_id)

    execute(pg_store, "UPDATE baton.x_claim SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE task_id=%s", (task_id,))
    execute(pg_store, "UPDATE baton.tasks SET responded_at=clock_timestamp() WHERE id=%s", (task_id,))
    assert pg_store.escalate_overdue(SERVICE_ACTOR)["escalated"] == [], "a worker that already responded is not escalated after a lapsed lease"

    late = make_task(call, identities, page, fallback_assignee=identities["replacement"]["agent"])
    execute(pg_store, "UPDATE baton.tasks SET state='claimed' WHERE id=%s", (late["task_id"],))
    execute(pg_store, """INSERT INTO baton.x_claim(task_id,holder,holder_account,holder_kind,epoch,lease_expires_at,scopes)
        VALUES (%s,%s,%s,'agent',1,clock_timestamp()+interval '5 minutes','[]')""",
            (late["task_id"], identities["worker"]["agent"], identities["worker"]["account"]))
    make_overdue(pg_store, late["task_id"])
    assert pg_store.escalate_overdue(SERVICE_ACTOR)["reassigned"] == []
    assert column(pg_store, late["task_id"], "assignee") == identities["worker"]["agent"]


def test_handed_off_target_that_never_responds_is_escalated_after_the_lease_lapses(team, pg_store):
    call, identities, space, page = team
    task = make_task(call, identities, page)
    task_id = task["task_id"]
    claimed = call("worker", "claim", operation_id=op(), task_id=task_id)
    call("worker", "handoff", operation_id=op(), task_id=task_id, epoch=claimed["epoch"], target_principal=identities["replacement"]["agent"])
    make_overdue(pg_store, task_id)
    assert pg_store.escalate_overdue(SERVICE_ACTOR)["escalated"] == [], "the live claim protects the task"
    execute(pg_store, "UPDATE baton.x_claim SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE task_id=%s", (task_id,))
    assert pg_store.escalate_overdue(SERVICE_ACTOR)["escalated"] == [task_id]


def run_concurrently(count, action):
    barrier, results, errors = threading.Barrier(count), [], []

    def runner():
        try:
            barrier.wait(timeout=10)
            results.append(action())
        except Exception as exc:  # surfaced below so a failure is never hidden in a thread
            errors.append(exc)
    threads = [threading.Thread(target=runner) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not errors, errors
    return results


def test_two_concurrent_runners_never_escalate_twice(team, pg_store):
    call, identities, space, page = team
    tasks = [make_task(call, identities, page, fallback_assignee=identities["replacement"]["agent"] if index == 0 else None)["task_id"]
             for index in range(4)]
    for task_id in tasks:
        make_overdue(pg_store, task_id)
    results = run_concurrently(4, lambda: pg_store.escalate_overdue(SERVICE_ACTOR))
    assert sorted(task for result in results for task in result["escalated"]) == sorted(tasks)
    assert [task for result in results for task in result["reassigned"]] == [tasks[0]]
    for task_id in tasks:
        assert len(overdue_rows(pg_store, task_id)) == 1
        assert audit_events(pg_store, task_id).count("task.escalated") == 1
    assert audit_events(pg_store, tasks[0]).count("task.reassigned") == 1


def test_the_escalated_at_guard_alone_prevents_double_escalation(team, pg_store, monkeypatch):
    call, identities, space, page = team
    task_id = make_task(call, identities, page)["task_id"]
    make_overdue(pg_store, task_id)
    original = pg_store._transaction
    monkeypatch.setattr(pg_store, "_transaction", lambda write=False, single_flight=None: original(write))
    results = run_concurrently(4, lambda: pg_store.escalate_overdue(SERVICE_ACTOR))
    assert [task for result in results for task in result["escalated"]] == [task_id]
    assert len(overdue_rows(pg_store, task_id)) == 1 and audit_events(pg_store, task_id).count("task.escalated") == 1


def test_a_held_advisory_lock_makes_other_runners_skip(team, pg_store):
    call, identities, space, page = team
    task_id = make_task(call, identities, page)["task_id"]
    make_overdue(pg_store, task_id)
    config = json.loads(pg_store.config_path.read_text())
    with psycopg.connect(**config) as holder:
        holder.execute("SELECT pg_advisory_xact_lock(%s)", (ESCALATION_LOCK_ID,))
        skipped = pg_store.escalate_overdue(SERVICE_ACTOR)
        assert skipped == {"skipped": True, "escalated": [], "reassigned": []}
        assert column(pg_store, task_id, "escalated_at") is None
    assert pg_store.escalate_overdue(SERVICE_ACTOR)["escalated"] == [task_id]


def test_only_the_service_identity_may_run_escalation(team, pg_store):
    call, identities, space, page = team
    with pytest.raises(space_store.StoreError) as error:
        pg_store.escalate_overdue(Actor(identities["owner"]["agent"], identities["owner"]["account"], "human"))
    assert error.value.code == "forbidden"


def test_server_escalation_pass_wakes_waiters_and_reports_errors(team, pg_store, modules):
    call, identities, space, page = team
    server = modules[1]
    task_id = make_task(call, identities, page)["task_id"]
    make_overdue(pg_store, task_id)
    asyncio.run(server.escalation_pass())
    assert column(pg_store, task_id, "escalated_at") is not None
    assert events(notes(call, "owner"))[-1] == "task.overdue"
    assert server.ESCALATION_SECONDS == 30


def test_policy_parsing_and_environment_override(team, pg_store, modules, monkeypatch):
    assert DEFAULT_RESPONSE_MINUTES == {"urgent": 20, "normal": 60, "low": 1440}
    assert parse_response_minutes(None) == DEFAULT_RESPONSE_MINUTES == parse_response_minutes("  ")
    assert parse_response_minutes("urgent=20,normal=60,low=1440") == DEFAULT_RESPONSE_MINUTES
    assert parse_response_minutes(" urgent = 5 , low=30 ") == {"urgent": 5, "normal": 60, "low": 30}
    for bad in ("urgent", "urgent=", "urgent=0", "urgent=-1", "urgent=1.5", "urgent=x", "high=5", "urgent=5,urgent=6", "urgent=5,", "urgent=999999999",
                "urgent=٣"):
        with pytest.raises(ValueError):
            parse_response_minutes(bad)

    call, identities, space, page = team
    monkeypatch.setenv("BATON_RESPONSE_MINUTES", "urgent=5,normal=10")
    modules[1]._STORE = None
    task = make_task(call, identities, page, priority="urgent")
    assert close_to(minutes_left(pg_store, task["task_id"]), 5, 1)
    assert close_to(minutes_left(pg_store, make_task(call, identities, page)["task_id"]), 10, 1)
    assert close_to(minutes_left(pg_store, make_task(call, identities, page, priority="low")["task_id"]), 1440)

    monkeypatch.setenv("BATON_RESPONSE_MINUTES", "urgent=soon")
    with pytest.raises(ValueError):
        SpaceStore.from_env()


def test_idempotency_digests_of_old_requests_are_unchanged(team, pg_store, api):
    call, identities, space, page = team
    owner = Actor(identities["owner"]["agent"], identities["owner"]["account"], "human")
    worker = Actor(identities["worker"]["agent"], identities["worker"]["account"], "agent")
    scopes = [{"kind": "path", "value": "src/digest"}]
    key = "response-digest-" + uuid.uuid4().hex[:8]

    task = call("owner", "submit_task", operation_id=op(), idempotency_key=key, prompt=PROMPT, page_id=page["id"],
                scopes=scopes, assignee_principal=worker.principal)
    legacy = {"idempotency_key": key, "prompt": PROMPT, "page_id": page["id"], "scopes": scopes, "assignee": asdict(worker)}
    assert column(pg_store, task["task_id"], "input_hash") == hashlib.sha256(_canonical(legacy).encode()).hexdigest()
    replay = call("owner", "submit_task", operation_id=op(), idempotency_key=key, prompt=PROMPT, page_id=page["id"],
                  scopes=scopes, assignee_principal=worker.principal, priority="normal", escalate_to=owner.principal)
    assert replay["replay"] is True and replay["task_id"] == task["task_id"], "explicit defaults hash like omitted ones"
    assert call("owner", "submit_task", 409, operation_id=op(), idempotency_key=key, prompt=PROMPT, page_id=page["id"],
                scopes=scopes, assignee_principal=worker.principal, priority="urgent")["code"] == "operation_conflict"
    assert call("owner", "submit_task", 409, operation_id=op(), idempotency_key=key, prompt=PROMPT, page_id=page["id"],
                scopes=scopes, assignee_principal=worker.principal, fallback_assignee=identities["replacement"]["agent"])["code"] == "operation_conflict"

    block_operation = op()
    block = call("owner", "create_task_block", operation_id=block_operation, page_id=page["id"], prompt=PROMPT, scopes=scopes,
                 reviewers=[identities["reviewer"]["agent"]])
    legacy_args = {"page_id": page["id"], "prompt": PROMPT, "scopes": scopes, "reviewers": [identities["reviewer"]["agent"]],
                   "base_commit": None, "context": None, "evidence": None, "assignee": None}
    expected = hashlib.sha256(_canonical([asdict(owner), "create_task_block", legacy_args]).encode()).hexdigest()
    assert query(pg_store, "SELECT request_hash FROM baton.operations WHERE operation_id=%s", (block_operation,))[0]["request_hash"] == expected
    assert call("owner", "create_task_block", operation_id=block_operation, page_id=page["id"], prompt=PROMPT, scopes=scopes,
                reviewers=[identities["reviewer"]["agent"]])["task_id"] == block["task_id"]
    assert call("owner", "create_task_block", 409, operation_id=block_operation, page_id=page["id"], prompt=PROMPT, scopes=scopes,
                reviewers=[identities["reviewer"]["agent"]], priority="low")["code"] == "operation_conflict"

    urgent_operation = op()
    call("owner", "create_task_block", operation_id=urgent_operation, page_id=page["id"], prompt=PROMPT, scopes=scopes,
         reviewers=[identities["reviewer"]["agent"]], priority="urgent")
    assert query(pg_store, "SELECT request_hash FROM baton.operations WHERE operation_id=%s", (urgent_operation,))[0]["request_hash"] != expected


def test_set_priority_recomputes_the_open_window_and_checks_authority(team, pg_store):
    call, identities, space, page = team
    task = make_task(call, identities, page)
    task_id = task["task_id"]
    assert close_to(minutes_left(pg_store, task_id), 60)
    operation = op()
    urgent = call("owner", "set_priority", operation_id=operation, task_id=task_id, priority="urgent")
    assert urgent["priority"] == "urgent" and close_to(minutes_left(pg_store, task_id), 20)
    replay = call("owner", "set_priority", operation_id=operation, task_id=task_id, priority="urgent")
    assert replay["replay"] is True and replay["priority"] == "urgent"
    assert call("owner", "set_priority", 409, operation_id=operation, task_id=task_id, priority="low")["code"] == "operation_conflict"
    assert audit_events(pg_store, task_id).count("task.priority_set") == 1

    assert call("reviewer", "set_priority", operation_id=op(), task_id=task_id, priority="low")["priority"] == "low"
    assert close_to(minutes_left(pg_store, task_id), 1440)
    for label in ("worker", "replacement"):
        assert call(label, "set_priority", 403, operation_id=op(), task_id=task_id, priority="urgent")["code"] == "forbidden"
    assert call("stranger", "set_priority", 404, operation_id=op(), task_id=task_id, priority="urgent")["code"] == "not_found"
    for bad in ("high", "", None, 5):
        assert call("owner", "set_priority", 422, operation_id=op(), task_id=task_id, priority=bad)["code"] == "invalid"

    call("worker", "claim", operation_id=op(), task_id=task_id)
    before = column(pg_store, task_id, "respond_by")
    assert call("owner", "set_priority", operation_id=op(), task_id=task_id, priority="urgent")["priority"] == "urgent"
    assert column(pg_store, task_id, "respond_by") == before, "a responded task keeps its deadline"

    queued = call("owner", "submit_task", operation_id=op(), idempotency_key="response-prio-" + uuid.uuid4().hex[:8], prompt=PROMPT,
                  page_id=page["id"])
    assert call("owner", "set_priority", operation_id=op(), task_id=queued["task_id"], priority="urgent")["respond_by"] is None
    assigned = call("owner", "assign_task", operation_id=op(), task_id=queued["task_id"], target_principal=identities["worker"]["agent"])
    assert assigned["priority"] == "urgent" and close_to(minutes_left(pg_store, queued["task_id"]), 20)


def test_set_priority_keeps_the_original_window_start_and_does_not_reopen_escalation(team, pg_store):
    call, identities, space, page = team
    task_id = make_task(call, identities, page)["task_id"]
    make_overdue(pg_store, task_id, minutes=30)
    call("owner", "set_priority", operation_id=op(), task_id=task_id, priority="low")
    assert close_to(minutes_left(pg_store, task_id), 1440 - 90, 2), "the window started 90 minutes ago, so a low deadline follows from that start"
    make_overdue(pg_store, task_id)
    assert pg_store.escalate_overdue(SERVICE_ACTOR)["escalated"] == [task_id]
    call("owner", "set_priority", operation_id=op(), task_id=task_id, priority="urgent")
    make_overdue(pg_store, task_id)
    assert pg_store.escalate_overdue(SERVICE_ACTOR)["escalated"] == []


def test_invalid_response_options_are_rejected(team):
    call, identities, space, page = team
    worker = identities["worker"]["agent"]
    for extra in ({"priority": "critical"}, {"priority": None}, {"priority": 1}, {"escalate_to": "bad name"}, {"escalate_to": ""},
                  {"fallback_assignee": "bad/name"}, {"fallback_assignee": worker}):
        error = call("owner", "create_task_block", 422, operation_id=op(), page_id=page["id"], prompt=PROMPT,
                     scopes=[{"kind": "path", "value": "src/invalid"}], reviewers=[identities["reviewer"]["agent"]],
                     assignee_principal=worker, **extra)
        assert error["code"] == "invalid"
    assert call("owner", "submit_task", 422, operation_id=op(), idempotency_key="response-bad", prompt=PROMPT, priority="x")["code"] == "invalid"


def structured(response):
    result = response.json()["result"]
    assert not result.get("isError", False)
    return result.get("structuredContent") or json.loads(result["content"][0]["text"])


def test_http_mcp_and_adapter_expose_set_priority(team, api):
    client, call, identities, tokens, registry, records = api
    schema = client.get("/v1/space/schema", headers={"Authorization": "Bearer " + tokens["owner"]}).json()["operations"]
    assert schema["set_priority"]["mutation"] is True
    assert set(schema["set_priority"]["request"]["required"]) == {"operation_id", "task_id", "priority"}
    for name in ("submit_task", "create_task_block"):
        assert {"priority", "escalate_to", "fallback_assignee"} <= schema[name]["request"]["properties"].keys()
        assert not {"priority", "escalate_to", "fallback_assignee"} & set(schema[name]["request"].get("required", []))
    headers = {"Authorization": "Bearer " + tokens["owner"], "Host": "bridge", "Accept": "application/json, text/event-stream"}
    tools = {tool["name"]: tool for tool in client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()["result"]["tools"]}
    assert "set_priority" in tools and "operation_id" in tools["set_priority"]["inputSchema"]["required"]
    assert {"priority", "escalate_to", "fallback_assignee"} <= tools["submit_task"]["inputSchema"]["properties"].keys()
    assert {"priority", "escalate_to", "fallback_assignee"} <= tools["create_task_block"]["inputSchema"]["properties"].keys()

    space, page = shared_page(call, identities)
    response = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
        "name": "submit_task", "arguments": {"idempotency_key": "response-mcp", "prompt": PROMPT, "page_id": page["id"],
                                             "assignee_principal": identities["worker"]["agent"], "priority": "urgent",
                                             "fallback_assignee": identities["replacement"]["agent"]}}})
    task = structured(response)
    assert task["priority"] == "urgent" and task["fallback_assignee"] == identities["replacement"]["agent"] and task["respond_by"]
    response = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
        "name": "set_priority", "arguments": {"operation_id": op(), "task_id": task["task_id"], "priority": "low"}}})
    assert structured(response)["result"]["priority"] == "low"

    assert "set_priority" in mcp_stdio.TOOLS
    assert mcp_stdio.DEFAULT_ALLOWED_TOOLS == frozenset({"submit_task", "get_task", "list_tasks", "fetch_inbox"})
