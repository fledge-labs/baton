# SPDX-License-Identifier: Apache-2.0
"""Dogfooding batch 1 on actual PostgreSQL: richer notifications, task-addressed comments, field-level
validation errors, blocked markers, invisible-object 404s and legacy root-scope handling.

Every identity, prompt, comment and token is synthetic.
"""

import asyncio
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from bridge.space_store import Actor, SERVICE_ACTOR, StoreError
from test_bridge import api, mcp_call, op, shared_page  # noqa: F401  (api is a fixture)

ROOT = Path(__file__).resolve().parents[1]
LONG_PROMPT = "Prepare the quarterly   synthetic report " + "x" * 300


def wait(call, label, after=0):
    return call(label, "wait_inbox", after_seq=after, timeout_seconds=0)["notifications"]


def manual_task(call, identities, page, prompt="Synthetic assignment", assignee="worker", **extra):
    return call("owner", "create_task_block", operation_id=op(), page_id=page["id"], prompt=prompt,
                reviewers=[identities["reviewer"]["agent"]], assignee_principal=identities[assignee]["agent"], **extra)


def audit_events(store, task_id):
    with store._transaction() as conn:
        return [row["event"] for row in conn.execute("SELECT event FROM baton.x_audit WHERE task_id=%s ORDER BY time,id", (task_id,))]


# --- DF-1 richer notifications --------------------------------------------------------------------

def test_wait_inbox_rows_carry_actor_subject_and_a_200_character_preview(api, pg_store):
    _, call, identities, _, _, _ = api
    _, page = shared_page(call, identities)
    task = manual_task(call, identities, page, prompt=LONG_PROMPT)
    row = wait(call, "worker")[0]
    assert row["actor"] == {"principal": identities["owner"]["agent"], "kind": "human"}
    assert row["subject"] == "[assigned] Prepare the quarterly synthetic report " + "x" * 21
    assert len(row["subject"]) == len("[assigned] ") + 60
    assert row["body_preview"] == LONG_PROMPT[:200] and len(row["body_preview"]) == 200

    comment = "c" * 250
    call("replacement", "comment", operation_id=op(), page_id=page["id"], body=comment, thread_id=task["task_id"])
    mine = wait(call, "worker", row["seq"])[0]
    assert mine["event"] == "comment.created"
    assert mine["actor"] == {"principal": identities["replacement"]["agent"], "kind": "agent"}
    assert mine["subject"] == "comment on " + task["task_id"] and mine["body_preview"] == comment[:200]

    quoted = call("owner", "comment", operation_id=op(), page_id=page["id"], body="Plain page comment")
    call("owner", "mention", operation_id=op(), comment_id=quoted["id"], target_type="agent", target_id=identities["worker"]["agent"])
    mention = wait(call, "worker", mine["seq"])[0]
    assert mention["event"] == "mention.created" and mention["subject"] == "mention in thread " + quoted["thread_id"]
    assert mention["body_preview"] == "Plain page comment"
    assert mention["actor"]["principal"] == identities["owner"]["agent"]


def test_previews_are_computed_at_read_time_and_never_stored(api, pg_store):
    _, call, identities, _, _, _ = api
    _, page = shared_page(call, identities)
    task = manual_task(call, identities, page, prompt="Synthetic stored-text probe")
    call("owner", "comment", operation_id=op(), page_id=page["id"], body="Synthetic stored-comment probe", thread_id=task["task_id"])
    with pg_store._transaction() as conn:
        outbox = json.dumps([dict(row) for row in conn.execute("SELECT * FROM baton.x_notify")], default=str)
    assert "probe" not in outbox, "the outbox keeps identifiers only"
    assert "probe" in json.dumps(wait(call, "worker"))


def test_preview_is_omitted_when_the_reader_lost_view_permission(api, pg_store):
    _, call, identities, _, _, _ = api
    space, page = shared_page(call, identities)
    task = manual_task(call, identities, page, prompt="Synthetic revoked-text probe")
    assert wait(call, "worker")[0]["body_preview"] == "Synthetic revoked-text probe"
    call("owner", "share", operation_id=op(), resource_type="space", resource_id=space["id"],
         subject_type="person", subject_id=identities["worker"]["account"], role=None)
    assert call("worker", "wait_inbox", after_seq=0, timeout_seconds=0)["notifications"] == []
    assert call("worker", "get_task", 404, task_id=task["task_id"])["code"] == "not_found"
    call("owner", "share", operation_id=op(), resource_type="space", resource_id=space["id"],
         subject_type="person", subject_id=identities["worker"]["account"], role="view")
    assert wait(call, "worker")[0]["body_preview"] == "Synthetic revoked-text probe"


def test_automatic_task_prompts_stay_private_in_notification_rows(api, pg_store, modules):
    _, call, identities, _, _, _ = api
    _, server, _ = modules
    _, page = shared_page(call, identities)
    task = call("owner", "submit_task", operation_id=op(), idempotency_key="df1-auto", prompt="Synthetic automatic probe", page_id=page["id"])
    claim = pg_store.claim_next(server.SERVICE, op(), lease_seconds=300)
    pg_store.mark_dispatched(server.SERVICE, op(), claim["id"], claim["epoch"])
    pg_store.fail_auto(server.SERVICE, op(), claim["id"], claim["epoch"], "CODEX_TIMEOUT", str(uuid.uuid4()))
    row = wait(call, "owner")[0]
    assert row["event"] == "task.failed" and row["subject"] == "[failed] " + task["task_id"]
    assert "body_preview" not in row and "probe" not in json.dumps(row)
    assert row["actor"] == {"principal": SERVICE_ACTOR.principal, "kind": "service"}


def test_fetch_inbox_messages_gain_actor_and_preview_without_changing_subject(api):
    _, call, identities, _, _, _ = api
    _, page = shared_page(call, identities)
    task = manual_task(call, identities, page, prompt=LONG_PROMPT)
    message = next(item for item in call("worker", "fetch_inbox")["messages"] if item["thread_id"] == task["task_id"])
    assert message["actor"] == {"principal": identities["owner"]["agent"], "kind": "human"}
    assert message["body_preview"] == LONG_PROMPT[:200] and message["body_md"] == LONG_PROMPT
    assert message["subject"] == "[submitted] " + task["task_id"]


# --- DF-2 list_comments by task -------------------------------------------------------------------

def test_list_comments_accepts_a_task_id_over_http_and_mcp(api):
    client, call, identities, tokens, _, _ = api
    _, page = shared_page(call, identities)
    task = manual_task(call, identities, page)
    other = manual_task(call, identities, page, prompt="Another task")
    call("replacement", "comment", operation_id=op(), page_id=page["id"], body="On the task", thread_id=task["task_id"])
    call("replacement", "comment", operation_id=op(), page_id=page["id"], body="On the other", thread_id=other["task_id"])
    by_thread = call("worker", "list_comments", page_id=page["id"], thread_id=task["task_id"])
    by_task = call("worker", "list_comments", task_id=task["task_id"])
    assert by_task == by_thread and len(by_task) >= 2
    assert {item["thread_id"] for item in by_task} == {task["task_id"]}
    assert "On the task" in json.dumps(by_task) and "On the other" not in json.dumps(by_task)
    assert len(call("worker", "list_comments", page_id=page["id"])) > len(by_task)

    mcp, raw = mcp_call(client, tokens["worker"], "list_comments", {"task_id": task["task_id"]})
    assert not raw.get("isError") and mcp["x_protocol"] == "baton.space.v1"
    assert [item["id"] for item in mcp["result"]] == [item["id"] for item in by_task]
    schema = client.get("/v1/space/schema", headers={"Authorization": "Bearer " + tokens["owner"]}).json()
    properties = schema["operations"]["list_comments"]["request"]
    assert "task_id" in properties["properties"] and "required" not in properties


def test_list_comments_by_task_checks_permission_and_rejects_ambiguous_input(api):
    _, call, identities, _, _, _ = api
    _, page = shared_page(call, identities)
    task = manual_task(call, identities, page)
    assert call("stranger", "list_comments", 404, task_id=task["task_id"])["code"] == "not_found"
    assert call("stranger", "list_comments", 404, page_id=page["id"])["code"] == "not_found"
    assert call("worker", "list_comments", 404, task_id=str(uuid.uuid4()))["code"] == "not_found"
    for body in ({}, {"task_id": task["task_id"], "page_id": page["id"]},
                 {"task_id": task["task_id"], "thread_id": task["task_id"]}, {"thread_id": task["task_id"]}):
        assert call("worker", "list_comments", 422, **body)["code"] == "invalid"
    error = call("worker", "list_comments", 422, task_id="not-a-uuid")
    assert error["details"] == [{"field": "task_id", "expected": "UUID string in canonical lowercase form"}]


# --- DF-3 field-level validation errors -----------------------------------------------------------

def test_invalid_requests_name_the_field_and_the_expected_shape_without_echoing_values(api):
    client, call, identities, tokens, _, _ = api
    secret = "synthetic-secret-input-value"
    error = call("owner", "create_space", 422, operation_id=op(), name=secret, surprise=secret)
    assert error["code"] == "invalid" and {"field": "surprise", "expected": "not a field of this operation; see GET /v1/space/schema"} in error["details"]

    error = call("owner", "create_space", 422, operation_id=op())
    assert error["details"] == [{"field": "name", "expected": "required string"}]

    error = call("owner", "create_space", 422, operation_id=op(), name=5)
    assert error["details"] == [{"field": "name", "expected": "string"}]
    error = call("owner", "wait_inbox", 422, after_seq=secret)
    assert error["details"] == [{"field": "after_seq", "expected": "integer"}]
    error = call("owner", "webhook_subscribe", 422, operation_id=op(), url=secret, include_preview="yes")
    assert {"field": "include_preview", "expected": "boolean"} in error["details"]

    error = call("owner", "create_space", 422, operation_id=secret, name="Fine")
    assert error["details"] == [{"field": "operation_id", "expected": "UUID string in canonical lowercase form"}]
    error = call("owner", "get_task", 422, task_id=secret)
    assert error["details"] == [{"field": "task_id", "expected": "UUID string in canonical lowercase form"}]

    error = call("owner", "plan", 422, operation_id=op(), graph=[{"task_id": 7, "depends_on": secret}])
    assert {"field": "graph[0].task_id", "expected": "string"} in error["details"]
    assert {"field": "graph[0].depends_on", "expected": "array"} in error["details"]

    # Only identifier-shaped unknown keys are named; anything else is not echoed at all.
    error = call("owner", "create_space", 422, operation_id=op(), name="Fine", **{"bad key " + secret: 1})
    assert error["details"] == [{"field": "<unknown>", "expected": "not a field of this operation; see GET /v1/space/schema"}]
    assert not any(secret in json.dumps(item) for item in (error,))

    unknown = client.post("/v1/space/no_such_operation", json={"x": secret}, headers={"Authorization": "Bearer " + tokens["owner"]})
    assert unknown.status_code == 422 and secret not in unknown.text and "no_such_operation" not in unknown.text
    assert unknown.json()["error"]["details"][0]["field"] == "operation"
    assert client.post("/v1/space/create_space", json=[secret], headers={"Authorization": "Bearer " + tokens["owner"]}).json()[
        "error"]["details"] == [{"field": "body", "expected": "JSON object"}]


def test_store_validators_report_the_field_that_failed(api):
    _, call, identities, _, _, _ = api
    space, page = shared_page(call, identities)
    cases = [
        ("comment", {"page_id": page["id"], "body": "   "}, "body", "non-blank string of at most 16000 characters without NUL"),
        ("create_page", {"space_id": space["id"], "title": ""}, "title", "non-blank string of at most 200 characters without NUL"),
        ("create_block", {"page_id": page["id"], "kind": "nonsense", "content": "x"}, "kind", "one of text, file, prompt, agent_instructions"),
        ("share", {"resource_type": "space", "resource_id": space["id"], "subject_type": "robot", "subject_id": "x", "role": "view"}, "subject_type", "one of person, team"),
        ("share", {"resource_type": "space", "resource_id": space["id"], "subject_type": "person", "subject_id": "x", "role": "boss"}, "role", "one of view, comment, edit, or null to remove the grant"),
        ("submit_task", {"idempotency_key": "ok", "prompt": "Fine", "priority": "now"}, "priority", "one of urgent, normal, low"),
        ("submit_task", {"idempotency_key": "ok", "prompt": "Fine", "scopes": [{"kind": "path", "value": "/abs"}]}, "scopes", None),
        ("submit_task", {"idempotency_key": "bad key!", "prompt": "Fine"}, "idempotency_key", None),
        ("set_work_face", {"task_id": str(uuid.uuid4()), "work_face": {"environment": []}}, None, None),
        ("claim", {"task_id": str(uuid.uuid4()), "lease_seconds": 0}, "lease_seconds", "integer between 1 and 3600"),
        ("webhook_subscribe", {"url": "https://example.com/hook", "events": ["nope"]}, "events", "non-empty array of known notification event names"),
    ]
    for operation, body, field, expected in cases:
        error = call("owner", operation, 422, operation_id=op(), **body)
        assert error["code"] == "invalid"
        if operation == "set_work_face":
            assert error["details"][0]["field"].startswith("work_face.")
        else:
            assert error["details"][0]["field"] == field
            assert expected is None or error["details"][0]["expected"] == expected
        assert "/abs" not in json.dumps(error) and "bad key" not in json.dumps(error)


def test_mcp_errors_carry_the_same_details(api):
    client, call, identities, tokens, _, _ = api
    secret = "synthetic-secret-input-value"
    _, raw = mcp_call(client, tokens["owner"], "create_space", {"operation_id": op(), "name": 3, "extra": secret})
    text = raw["content"][0]["text"]
    assert raw["isError"] is True and text.startswith("invalid: Invalid request; details=")
    details = json.loads(text.split("details=", 1)[1])
    assert {"field": "name", "expected": "string"} in details
    assert {"field": "extra", "expected": "not a field of this operation; see GET /v1/space/schema"} in details
    assert secret not in text

    _, raw = mcp_call(client, tokens["owner"], "submit_task", {"idempotency_key": "k", "unknown": secret})
    details = json.loads(raw["content"][0]["text"].split("details=", 1)[1])
    assert details == [{"field": "unknown", "expected": "not a field of this operation; see GET /v1/space/schema"}]
    _, raw = mcp_call(client, tokens["owner"], "submit_task", {"idempotency_key": "k"})
    assert {"field": "prompt", "expected": "required string"} in json.loads(raw["content"][0]["text"].split("details=", 1)[1])
    _, raw = mcp_call(client, tokens["owner"], "get_task", {"task_id": 5})
    assert json.loads(raw["content"][0]["text"].split("details=", 1)[1]) == [{"field": "task_id", "expected": "UUID string"}]
    _, raw = mcp_call(client, tokens["owner"], "comment", {"operation_id": op(), "page_id": str(uuid.uuid4()), "body": " "})
    assert json.loads(raw["content"][0]["text"].split("details=", 1)[1])[0]["field"] == "body"
    _, raw = mcp_call(client, tokens["stranger"], "get_task", {"task_id": str(uuid.uuid4())})
    assert raw["content"][0]["text"] == "not_found: Resource not found", "other errors keep their exact text"


# --- DF-7 blocked marker --------------------------------------------------------------------------

def test_mark_blocked_posts_notifies_marks_the_view_and_clears_on_claim(api, pg_store):
    _, call, identities, _, _, _ = api
    _, page = shared_page(call, identities)
    task = manual_task(call, identities, page, prompt="Synthetic blocked probe")
    task_id = task["task_id"]
    assert task["blocked"] is None
    owner_seen = wait(call, "owner")
    assert owner_seen == []

    note = "Waiting for the src/ claim to clear"
    blocked = call("worker", "mark_blocked", operation_id=(operation := op()), task_id=task_id, reason_code="scope_conflict", note=note)
    assert blocked["blocked"]["reason_code"] == "scope_conflict" and blocked["blocked"]["by"] == identities["worker"]["agent"]
    assert blocked["blocked"]["at"] and blocked["state"] == "queued" and blocked["replay"] is False
    assert blocked["responded_at"] is not None, "marking blocked is a response for the response-time agreement"
    assert call("owner", "get_task", task_id=task_id)["blocked"] == {k: blocked["blocked"][k] for k in ("reason_code", "by", "at")}

    thread = call("owner", "list_comments", task_id=task_id)
    mark = [item for item in thread if item["body"].startswith("[blocked]")]
    assert len(mark) == 1 and mark[0]["body"] == "[blocked] scope_conflict: " + note
    assert mark[0]["author_principal"] == identities["worker"]["agent"] and mark[0]["subject"] == "[blocked] " + task_id

    row = wait(call, "owner")[0]
    assert row["event"] == "task.blocked" and row["object_id"] == task_id
    assert row["subject"] == "[blocked] Synthetic blocked probe"
    assert row["actor"] == {"principal": identities["worker"]["agent"], "kind": "agent"}
    assert audit_events(pg_store, task_id).count("task.blocked") == 1
    with pg_store._transaction() as conn:
        metadata = conn.execute("SELECT metadata FROM baton.x_audit WHERE event='task.blocked'").fetchone()["metadata"]
    assert metadata == {"reason_code": "scope_conflict", "thread_id": task_id}, "the note stays out of the audit trail"

    replay = call("worker", "mark_blocked", operation_id=operation, task_id=task_id, reason_code="scope_conflict", note=note)
    assert replay["replay"] is True and audit_events(pg_store, task_id).count("task.blocked") == 1
    assert call("worker", "mark_blocked", 409, operation_id=operation, task_id=task_id, reason_code="other")["code"] == "operation_conflict"

    claim = call("worker", "claim", operation_id=op(), task_id=task_id)
    assert claim["blocked"] is None and call("owner", "get_task", task_id=task_id)["blocked"] is None
    assert call("worker", "mark_blocked", 409, operation_id=op(), task_id=task_id, reason_code="other")["code"] == "stale_claim", \
        "the holder is not blocked"


def test_reviewers_may_mark_blocked_and_everyone_else_is_refused(api):
    _, call, identities, _, _, _ = api
    _, page = shared_page(call, identities)
    task = manual_task(call, identities, page)
    task_id = task["task_id"]
    assert call("stranger", "mark_blocked", 404, operation_id=op(), task_id=task_id, reason_code="other")["code"] == "not_found"
    assert call("replacement", "mark_blocked", 403, operation_id=op(), task_id=task_id, reason_code="other")["code"] == "forbidden"
    blocked = call("reviewer", "mark_blocked", operation_id=op(), task_id=task_id, reason_code="waiting_input")
    assert blocked["blocked"]["by"] == identities["reviewer"]["agent"] and blocked["responded_at"] is None
    thread = [item["body"] for item in call("owner", "list_comments", task_id=task_id) if item["body"].startswith("[blocked]")]
    assert thread == ["[blocked] waiting_input"]
    assert wait(call, "owner")[0]["event"] == "task.blocked"


def test_mark_blocked_validates_reason_note_and_task_kind(api):
    _, call, identities, _, _, _ = api
    _, page = shared_page(call, identities)
    task = manual_task(call, identities, page)
    for reason in ("nonsense", "", None, 3):
        assert call("worker", "mark_blocked", 422, operation_id=op(), task_id=task["task_id"], reason_code=reason)["code"] == "invalid"
    error = call("worker", "mark_blocked", 422, operation_id=op(), task_id=task["task_id"], reason_code="nonsense")
    assert error["details"] == [{"field": "reason_code", "expected": "one of scope_conflict, lease_held, dependency_blocked, waiting_input, other"}]
    assert call("worker", "mark_blocked", 422, operation_id=op(), task_id=task["task_id"], reason_code="other", note=" ")["code"] == "invalid"
    assert call("worker", "mark_blocked", 422, operation_id=op(), task_id=task["task_id"])["details"] == [
        {"field": "reason_code", "expected": "required string"}]
    auto = call("owner", "submit_task", operation_id=op(), idempotency_key="df7-auto", prompt="Automatic", page_id=page["id"])
    assert call("owner", "mark_blocked", 403, operation_id=op(), task_id=auto["task_id"], reason_code="other")["code"] == "forbidden"


def test_reassignment_clears_the_blocked_marker_and_done_tasks_cannot_be_marked(api):
    _, call, identities, _, _, _ = api
    _, page = shared_page(call, identities)
    task = manual_task(call, identities, page)
    call("worker", "mark_blocked", operation_id=op(), task_id=task["task_id"], reason_code="lease_held")
    moved = call("owner", "assign_task", operation_id=op(), task_id=task["task_id"], target_principal=identities["replacement"]["agent"])
    assert moved["blocked"] is None

    done = manual_task(call, identities, page, prompt="Finish me")
    claim = call("worker", "claim", operation_id=op(), task_id=done["task_id"])
    call("worker", "submit_result", operation_id=op(), task_id=done["task_id"], epoch=claim["epoch"], result="Synthetic result")
    assert call("worker", "mark_blocked", 409, operation_id=op(), task_id=done["task_id"], reason_code="other")["code"] == "stale_claim"


def test_mark_blocked_is_published_over_mcp_and_in_the_adapter_known_tools(api):
    client, call, identities, tokens, _, _ = api
    _, page = shared_page(call, identities)
    task = manual_task(call, identities, page)
    result, raw = mcp_call(client, tokens["worker"], "mark_blocked", {"operation_id": op(), "task_id": task["task_id"], "reason_code": "dependency_blocked"})
    assert not raw.get("isError") and result["result"]["blocked"]["reason_code"] == "dependency_blocked"
    sys.path.insert(0, str(ROOT / "adapters"))
    try:
        import mcp_stdio
    finally:
        sys.path.pop(0)
    assert "mark_blocked" in mcp_stdio.TOOLS and "mark_blocked" not in mcp_stdio.DEFAULT_ALLOWED_TOOLS
    assert mcp_stdio.parse_allow_tools("mark_blocked") == frozenset({"mark_blocked"})


# --- DF-10 invisible objects are 404, visible-but-not-permitted stay 403 ---------------------------

def test_invisible_objects_are_404_while_insufficient_roles_stay_403(api):
    _, call, identities, _, _, _ = api
    space, page = shared_page(call, identities)
    task = manual_task(call, identities, page)
    block = call("owner", "create_block", operation_id=op(), page_id=page["id"], kind="text", content="Synthetic text")
    for operation, body in (("get_task", {"task_id": task["task_id"]}), ("get_page", {"page_id": page["id"]}),
                            ("get_space", {"space_id": space["id"]}), ("get_block", {"block_id": block["id"]}),
                            ("list_blocks", {"page_id": page["id"]}), ("list_comments", {"page_id": page["id"]}),
                            ("get_scopes", {"task_id": task["task_id"]}), ("get_claim", {"task_id": task["task_id"]}),
                            ("list_audit", {"task_id": task["task_id"]})):
        error = call("stranger", operation, 404, **body)
        assert error["code"] == "not_found" and error["message"] == "Resource not found", operation
    absent = str(uuid.uuid4())
    absent_error = call("stranger", "get_task", 404, task_id=absent)
    assert absent_error == call("stranger", "get_task", 404, task_id=task["task_id"]), "absent and invisible look the same"
    assert call("stranger", "comment", 404, operation_id=op(), page_id=page["id"], body="Hi")["code"] == "not_found"
    assert call("stranger", "update_block", 404, operation_id=op(), block_id=block["id"], expected_revision=1, content="x")["code"] == "not_found"
    assert call("stranger", "claim", 404, operation_id=op(), task_id=task["task_id"])["code"] == "not_found"
    assert call("stranger", "create_page", 404, operation_id=op(), space_id=space["id"], title="Nope")["code"] == "not_found"

    call("owner", "share", operation_id=op(), resource_type="space", resource_id=space["id"],
         subject_type="person", subject_id=identities["stranger"]["account"], role="view")
    assert call("stranger", "get_task", task_id=task["task_id"])["task_id"] == task["task_id"]
    assert call("stranger", "comment", 403, operation_id=op(), page_id=page["id"], body="Hi")["code"] == "forbidden"
    assert call("stranger", "claim", 403, operation_id=op(), task_id=task["task_id"])["code"] == "forbidden"
    assert call("stranger", "create_block", 403, operation_id=op(), page_id=page["id"], kind="text", content="x")["code"] == "forbidden"


def test_a_target_without_access_is_still_reported_as_forbidden_to_the_caller(api):
    _, call, identities, _, _, _ = api
    _, page = shared_page(call, identities)
    error = call("owner", "create_task_block", 403, operation_id=op(), page_id=page["id"], prompt="Needs access",
                 reviewers=[identities["reviewer"]["agent"]], assignee_principal=identities["stranger"]["agent"])
    assert error["code"] == "forbidden", "the caller can see the page; it is the assignee who lacks access"
    task = manual_task(call, identities, page)
    claim = call("worker", "claim", operation_id=op(), task_id=task["task_id"])
    assert call("worker", "handoff", 403, operation_id=op(), task_id=task["task_id"], epoch=claim["epoch"],
                target_principal=identities["stranger"]["agent"])["code"] == "forbidden"


# --- DF-13 legacy root scopes ---------------------------------------------------------------------

def scopes_of(call, label, task_id):
    return call(label, "get_task", task_id=task_id)["scopes"]


ROOT_SCOPE = [{"kind": "path", "value": ""}]
NONE_SCOPE = [{"kind": "none"}]


def make_legacy(call, identities, page, prompt, **extra):
    return manual_task(call, identities, page, prompt=prompt, scopes=[], **extra)


def test_migration_rewrites_only_unclaimed_manual_root_scope_tasks(api, pg_store, capsys):
    from bridge import scopes
    _, call, identities, _, _, _ = api
    _, page = shared_page(call, identities)
    legacy = make_legacy(call, identities, page, "Legacy manual")
    claimed = make_legacy(call, identities, page, "Claimed legacy")
    pg_store_claim = call("worker", "claim", operation_id=op(), task_id=claimed["task_id"])
    declared = manual_task(call, identities, page, prompt="Declared", scopes=[{"kind": "path", "value": "src/x"}])
    explicit = manual_task(call, identities, page, prompt="Explicit none", scopes=[{"kind": "none"}])
    automatic = call("owner", "submit_task", operation_id=op(), idempotency_key="df13-auto", prompt="Auto", page_id=page["id"])
    for task in (legacy, claimed, automatic):
        assert scopes_of(call, "owner", task["task_id"]) == ROOT_SCOPE

    dry = scopes.migrate_manual_root_to_none(True, pg_store)
    assert dry == {"dry_run": True, "matched": 1, "migrated": 0, "skipped_claimed": 1, "task_ids": [legacy["task_id"]]}
    assert scopes_of(call, "owner", legacy["task_id"]) == ROOT_SCOPE
    assert "task.scopes_migrated" not in audit_events(pg_store, legacy["task_id"])

    summary = scopes.migrate_manual_root_to_none(False, pg_store)
    assert summary == {"dry_run": False, "matched": 1, "migrated": 1, "skipped_claimed": 1, "task_ids": [legacy["task_id"]]}
    assert scopes_of(call, "owner", legacy["task_id"]) == NONE_SCOPE
    assert scopes_of(call, "owner", claimed["task_id"]) == ROOT_SCOPE, "a live claim is never touched"
    assert scopes_of(call, "owner", automatic["task_id"]) == ROOT_SCOPE, "automatic tasks keep the root placeholder"
    assert scopes_of(call, "owner", declared["task_id"]) == [{"kind": "path", "value": "src/x"}]
    assert scopes_of(call, "owner", explicit["task_id"]) == NONE_SCOPE
    assert audit_events(pg_store, legacy["task_id"]).count("task.scopes_migrated") == 1
    for untouched in (claimed, automatic, declared, explicit):
        assert "task.scopes_migrated" not in audit_events(pg_store, untouched["task_id"])
    assert scopes.migrate_manual_root_to_none(False, pg_store)["migrated"] == 0, "running it again changes nothing"
    assert pg_store_claim["epoch"] == 1 and call("owner", "get_task", task_id=claimed["task_id"])["state"] == "claimed"

    # Once the claim ends, the same command picks the task up.
    call("worker", "submit_result", operation_id=op(), task_id=claimed["task_id"], epoch=1, result="Synthetic result")
    assert scopes.migrate_manual_root_to_none(False, pg_store)["task_ids"] == [claimed["task_id"]]
    assert scopes_of(call, "owner", claimed["task_id"]) == NONE_SCOPE


def test_migrated_task_no_longer_conflicts_with_a_root_claim(api, pg_store):
    from bridge import scopes
    _, call, identities, _, _, _ = api
    _, page = shared_page(call, identities)
    holder = make_legacy(call, identities, page, "Holds the root")
    waiting = make_legacy(call, identities, page, "Waits", assignee="replacement")
    call("worker", "claim", operation_id=op(), task_id=holder["task_id"])
    assert call("replacement", "claim", 409, operation_id=op(), task_id=waiting["task_id"])["code"] == "scope_conflict"
    scopes.migrate_manual_root_to_none(False, pg_store)
    assert call("replacement", "claim", operation_id=op(), task_id=waiting["task_id"])["state"] == "claimed"


def test_migration_command_line_prints_one_json_summary(api, pg_store, capsys):
    from bridge import scopes
    _, call, identities, _, _, _ = api
    _, page = shared_page(call, identities)
    legacy = make_legacy(call, identities, page, "Legacy for the CLI")
    capsys.readouterr()
    assert scopes.main(["migrate-manual-root-to-none", "--dry-run"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["dry_run"] is True and printed["task_ids"] == [legacy["task_id"]] and printed["migrated"] == 0
    process = subprocess.run([sys.executable, "-m", "bridge.scopes", "migrate-manual-root-to-none"], cwd=ROOT,
                             capture_output=True, text=True, timeout=60)
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout) == {"dry_run": False, "matched": 1, "migrated": 1, "skipped_claimed": 0, "task_ids": [legacy["task_id"]]}
    assert "Legacy for the CLI" not in process.stdout + process.stderr
    assert subprocess.run([sys.executable, "-m", "bridge.scopes"], cwd=ROOT, capture_output=True, text=True).returncode == 2


def audit_records(common, event):
    path = common.STATE / "audit.jsonl"
    if not path.exists():
        return []
    return [row for row in map(json.loads, path.read_text().splitlines()) if row["event"] == event]


def test_scope_blocked_automatic_tasks_are_logged_once_per_five_minutes(api, pg_store, modules, monkeypatch):
    common, server, _ = modules
    _, call, identities, _, _, _ = api
    _, page = shared_page(call, identities)
    holder = manual_task(call, identities, page, prompt="Holds a path", scopes=[{"kind": "path", "value": "src/held"}])
    call("worker", "claim", operation_id=op(), task_id=holder["task_id"])
    stuck = call("owner", "submit_task", operation_id=op(), idempotency_key="df13-stuck", prompt="Stuck auto", page_id=page["id"],
                 scopes=[{"kind": "path", "value": "src/other"}])
    server._SCOPE_WARNED.clear()
    clock = [1000.0]
    monkeypatch.setattr(server.time, "monotonic", lambda: clock[0])

    for _ in range(3):
        assert asyncio.run(server.claim_next()) is None
    records = audit_records(common, "dispatch.blocked_by_scope")
    assert len(records) == 1, "rate limited to one warning per task"
    assert records[0]["task_id"] == stuck["task_id"] and records[0]["blocked_by"] == holder["task_id"]
    assert records[0]["level"] == "warning" and records[0]["agent"] == server.SERVICE.principal
    assert "Stuck auto" not in json.dumps(records[0])

    clock[0] += 299
    asyncio.run(server.claim_next())
    assert len(audit_records(common, "dispatch.blocked_by_scope")) == 1
    clock[0] += 2
    asyncio.run(server.claim_next())
    assert len(audit_records(common, "dispatch.blocked_by_scope")) == 2

    other = call("owner", "submit_task", operation_id=op(), idempotency_key="df13-second", prompt="Second stuck", page_id=page["id"],
                 scopes=[{"kind": "path", "value": "src/more"}])
    asyncio.run(server.claim_next())
    warned = {row["task_id"] for row in audit_records(common, "dispatch.blocked_by_scope")}
    assert warned == {stuck["task_id"], other["task_id"]}, "every skipped task gets its own warning"


def test_unblocked_automatic_tasks_log_nothing_and_a_failing_hook_cannot_change_the_claim(api, pg_store, modules):
    common, server, _ = modules
    _, call, identities, _, _, _ = api
    _, page = shared_page(call, identities)
    holder = manual_task(call, identities, page, prompt="Holds a path", scopes=[{"kind": "path", "value": "src/held"}])
    call("worker", "claim", operation_id=op(), task_id=holder["task_id"])
    free = call("owner", "submit_task", operation_id=op(), idempotency_key="df13-free", prompt="Free auto", page_id=page["id"],
                scopes=[{"kind": "migration", "value": "7"}])
    server._SCOPE_WARNED.clear()
    claimed = asyncio.run(server.claim_next())
    assert claimed["task_id"] == free["task_id"] and audit_records(common, "dispatch.blocked_by_scope") == []

    def broken(task_id, blocker):
        raise RuntimeError("log sink down")
    stuck = call("owner", "submit_task", operation_id=op(), idempotency_key="df13-hook", prompt="Stuck", page_id=page["id"])
    assert pg_store.claim_next(server.SERVICE, op(), on_scope_skip=broken) is None
    assert call("owner", "get_task", task_id=stuck["task_id"])["state"] == "queued"
