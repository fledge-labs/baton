# SPDX-License-Identifier: Apache-2.0
"""Integration checks against an explicitly supplied, disposable PostgreSQL DB.

Set BATON_TEST_DATABASE_CONFIG to the runtime-role JSON file. Optional
BATON_TEST_DATABASE_ADMIN_CONFIG enables owner-trigger checks. No real identities
or model credentials are read. The fixture never drops or truncates shared data.
"""

import json
import hashlib
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest

from bridge.space_store import Actor, SERVICE_ACTOR, SpaceStore, StoreError, LOCK_ID, normalize_scopes


def operation():
    return str(uuid.uuid4())


@pytest.fixture
def pg():
    path = os.environ.get("BATON_TEST_DATABASE_CONFIG")
    if not path:
        pytest.skip("Real PostgreSQL integration requires BATON_TEST_DATABASE_CONFIG")
    store = SpaceStore(path)
    store.check_schema(SERVICE_ACTOR)
    prefix = "test-" + uuid.uuid4().hex[:20]
    owner = Actor(prefix + "-owner", prefix + "-account", "human")
    editor = Actor(prefix + "-editor", prefix + "-editor-account", "agent")
    reader = Actor(prefix + "-reader", prefix + "-reader-account", "human")
    reviewer = Actor(prefix + "-reviewer", prefix + "-reviewer-account", "human")
    space = store.create_space(owner, operation(), prefix)
    page = store.create_page(owner, operation(), space["id"], "Synthetic page")
    for actor in (editor, reviewer):
        store.share(owner, operation(), "space", space["id"], "person", actor.account, "edit")
    fixture = SimpleNamespace(store=store, prefix=prefix, owner=owner, editor=editor, reader=reader,
                              reviewer=reviewer, space=space, page=page)
    yield fixture
    # Release only this test's synthetic claims. Immutable audit history remains.
    with store._transaction(write=True) as conn:
        conn.execute("""UPDATE baton.x_claim c SET lease_expires_at=clock_timestamp()-interval '1 second'
            FROM baton.tasks t WHERE t.id=c.task_id AND t.owner LIKE %s""", (prefix + "%",))
        conn.execute("UPDATE baton.tasks SET state='failed' WHERE owner LIKE %s AND state IN ('queued','claimed')", (prefix + "%",))


def manual(pg, suffix="task", scopes=None, assignee=None, **kwargs):
    return pg.store.create_task_block(pg.owner, operation(), pg.page["id"], "Synthetic work " + suffix,
        scopes if scopes is not None else [{"kind": "path", "value": pg.prefix + "/" + suffix}],
        [pg.reviewer.principal], assignee=assignee, **kwargs)


def expire(pg, task_id):
    with pg.store._transaction(write=True) as conn:
        conn.execute("UPDATE baton.x_claim SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE task_id=%s", (task_id,))


def expect(code, callable_):
    with pytest.raises(StoreError) as caught:
        callable_()
    assert caught.value.code == code


def test_roles_inherit_and_direct_removal_does_not_remove_inheritance(pg):
    s = pg.store
    child = s.create_page(pg.owner, operation(), pg.space["id"], "Child", pg.page["id"])
    s.share(pg.owner, operation(), "space", pg.space["id"], "person", pg.reader.account, "view")
    s.share(pg.owner, operation(), "page", pg.page["id"], "person", pg.reader.account, "comment")
    s.share(pg.owner, operation(), "page", child["id"], "person", pg.reader.account, "edit")
    assert s.effective_role(pg.reader, "page", child["id"])["role"] == "edit"
    s.share(pg.owner, operation(), "page", child["id"], "person", pg.reader.account, None)
    assert s.effective_role(pg.reader, "page", child["id"])["role"] == "comment"
    s.share(pg.owner, operation(), "page", pg.page["id"], "person", pg.reader.account, None)
    assert s.effective_role(pg.reader, "page", child["id"])["role"] == "view"
    expect("forbidden", lambda: s.comment(pg.reader, operation(), child["id"], "Not permitted"))
    expect("forbidden", lambda: s.share(pg.editor, operation(), "page", child["id"], "person", pg.reader.account, "edit"))
    s.share(pg.owner, operation(), "space", pg.space["id"], "person", pg.reader.account, None)
    expect("forbidden", lambda: s.get_page(pg.reader, child["id"]))


def test_comment_role_can_reply_but_not_edit_or_share_and_cycles_are_rejected(pg):
    s = pg.store
    s.share(pg.owner, operation(), "space", pg.space["id"], "person", pg.reader.account, "comment")
    assert s.comment(pg.reader, operation(), pg.page["id"], "Allowed feedback")["author_principal"] == pg.reader.principal
    expect("forbidden", lambda: s.create_block(pg.reader, operation(), pg.page["id"], "text", "Not an editor"))
    expect("forbidden", lambda: s.share(pg.reader, operation(), "page", pg.page["id"], "person", pg.editor.account, "edit"))
    child = s.create_page(pg.owner, operation(), pg.space["id"], "Child", pg.page["id"])
    with s._transaction() as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            with conn.transaction():
                conn.execute("UPDATE baton.pages SET parent_id=%s WHERE id=%s", (child["id"], pg.page["id"]))


def test_created_page_does_not_bypass_revocation_and_team_owner_is_separate(pg):
    s = pg.store
    page = s.create_page(pg.editor, operation(), pg.space["id"], "Editor authored")
    assert page["owner_account"] == pg.owner.account
    team = s.create_team(pg.owner, operation(), "Synthetic team")
    expect("forbidden", lambda: s.set_team_member(pg.editor, operation(), team["id"], pg.editor.account, True))
    s.set_team_member(pg.owner, operation(), team["id"], pg.editor.account, True)
    s.share(pg.owner, operation(), "space", pg.space["id"], "team", team["id"], "edit")
    s.share(pg.owner, operation(), "space", pg.space["id"], "person", pg.editor.account, None)
    block_op = operation()
    s.create_block(pg.editor, block_op, page["id"], "text", "Synthetic content")
    s.set_team_member(pg.owner, operation(), team["id"], pg.editor.account, False)
    expect("forbidden", lambda: s.get_page(pg.editor, page["id"]))
    expect("forbidden", lambda: s.create_block(pg.editor, block_op, page["id"], "text", "Synthetic content"))
    expect("invalid", lambda: s.share(pg.owner, operation(), "space", pg.space["id"], "team", team["id"], "comment"))


def test_operation_replays_reauthorize_and_changed_payload_conflicts(pg):
    s = pg.store
    block = s.create_block(pg.owner, operation(), pg.page["id"], "text", "Initial")
    op = operation()
    first = s.update_block(pg.editor, op, block["id"], 1, "Updated")
    replay = s.update_block(pg.editor, op, block["id"], 1, "Updated")
    assert first["revision"] == replay["revision"] == 2 and replay["replay"]
    expect("operation_conflict", lambda: s.update_block(pg.editor, op, block["id"], 1, "Different"))
    s.share(pg.owner, operation(), "space", pg.space["id"], "person", pg.editor.account, None)
    expect("forbidden", lambda: s.update_block(pg.editor, op, block["id"], 1, "Updated"))
    assert len(s.list_attribution(pg.owner, block["id"])) == 2


def test_real_transactions_allow_only_one_revision_update(pg):
    s = pg.store
    block = s.create_block(pg.owner, operation(), pg.page["id"], "text", "Initial")
    barrier = threading.Barrier(2)
    def update(actor):
        barrier.wait()
        try:
            s.update_block(actor, operation(), block["id"], 1, actor.principal)
            return "updated"
        except StoreError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, [pg.owner, pg.editor]))
    assert sorted(results) == ["revision_conflict", "updated"]
    history = s.list_attribution(pg.owner, block["id"])
    assert len(history) == 2 and history[-1]["author_principal"] in {pg.owner.principal, pg.editor.principal}


def test_proposals_require_human_review_current_revision_and_revision_checked_revert(pg):
    s = pg.store
    block = s.create_block(pg.owner, operation(), pg.page["id"], "text", "Original")
    proposal = s.proposal_create(pg.editor, operation(), block["id"], 1, "Agent draft", {"quote": "Original"})
    expect("forbidden", lambda: s.proposal_review(pg.editor, operation(), proposal["id"], "accept"))
    revised = s.proposal_review(pg.owner, operation(), proposal["id"], "revise", "Human revision")
    assert revised["status"] == "pending" and s.get_block(pg.owner, block["id"])["content"] == "Original"
    s.proposal_review(pg.owner, operation(), proposal["id"], "accept")
    assert s.get_block(pg.owner, block["id"])["content"] == "Human revision"
    stale = s.proposal_create(pg.editor, operation(), block["id"], 2, "Stale draft")
    s.update_block(pg.owner, operation(), block["id"], 2, "Later edit")
    expect("revision_conflict", lambda: s.proposal_review(pg.owner, operation(), stale["id"], "accept"))
    expect("revision_conflict", lambda: s.revert_block(pg.owner, operation(), block["id"], 2, 1))
    assert s.revert_block(pg.owner, operation(), block["id"], 3, 1)["content"] == "Original"
    assert [r["revision"] for r in s.list_attribution(pg.owner, block["id"])] == [1, 2, 3, 4]


def test_comment_mentions_inbox_are_recipient_and_permission_filtered(pg):
    s = pg.store
    comment = s.comment(pg.owner, operation(), pg.page["id"], "Synthetic feedback")
    mention = s.mention(pg.owner, operation(), comment["id"], "agent", pg.editor.principal)
    assert mention["action_requested"] is True
    assert len(s.fetch_inbox(pg.editor)["messages"]) == 1
    assert s.fetch_inbox(pg.reviewer)["messages"] == []
    s.share(pg.owner, operation(), "space", pg.space["id"], "person", pg.editor.account, None)
    assert s.fetch_inbox(pg.editor)["messages"] == []
    assert s.claim_next(SERVICE_ACTOR, operation()) is None


def test_person_mentions_target_principal_not_shared_account(pg):
    s = pg.store
    human = Actor(pg.prefix + "-human", pg.reader.account, "human")
    bot = Actor(pg.prefix + "-bot", pg.reader.account, "agent")
    s.share(pg.owner, operation(), "space", pg.space["id"], "person", human.account, "view")
    comment = s.comment(pg.owner, operation(), pg.page["id"], "Feedback for one person")
    s.mention(pg.owner, operation(), comment["id"], "person", human.principal)
    messages = s.fetch_inbox(human)["messages"]
    assert len(messages) == 1 and messages[0]["to"] == [human.principal]
    assert messages[0]["action_requested"] is False
    assert s.fetch_inbox(bot)["messages"] == []


def test_inbox_preserves_actual_mention_requester_and_aggregated_requests(pg):
    s = pg.store
    recipient = Actor(pg.prefix + "-recipient", pg.reviewer.account, "agent")
    comment = s.comment(pg.owner, operation(), pg.page["id"], "Original author's context")
    s.mention(pg.owner, operation(), comment["id"], "agent", recipient.principal)
    s.mention(pg.editor, operation(), comment["id"], "agent", recipient.principal)
    message = s.fetch_inbox(recipient)["messages"][0]
    assert message["from"] == pg.owner.principal
    assert message["requested_by"] == pg.editor.principal and message["action_requested"] is True
    assert {request["requested_by"] for request in message["requests"]} == {pg.owner.principal, pg.editor.principal}
    later = s.comment(pg.owner, operation(), pg.page["id"], "Later context", thread_id=comment["thread_id"])
    s.mention(pg.owner, operation(), later["id"], "agent", recipient.principal)
    messages = s.fetch_inbox(recipient)["messages"]
    assert len(messages) == 1 and messages[0]["body_md"] == "Later context"
    assert len(messages[0]["requests"]) == 3
    assert any(request["requested_by"] == pg.editor.principal and request["comment_id"] == comment["id"]
               for request in messages[0]["requests"])


def test_submit_business_key_and_operation_key_are_both_idempotent(pg):
    s = pg.store
    op = operation()
    args = {"page_id": pg.page["id"], "scopes": [{"kind": "path", "value": pg.prefix}]}
    first = s.submit_task(pg.owner, op, "stable-key", "Synthetic task", **args)
    assert s.submit_task(pg.owner, operation(), "stable-key", "Synthetic task", **args)["id"] == first["id"]
    assert s.submit_task(pg.owner, op, "stable-key", "Synthetic task", **args)["replay"]
    expect("operation_conflict", lambda: s.submit_task(pg.owner, operation(), "stable-key", "Changed", **args))
    assert len(s.list_tasks(pg.owner)) == 1
    assert s.list_tasks(pg.editor) == []
    assert s.get_task(pg.editor, first["id"])["id"] == first["id"]


def test_concurrent_operation_and_business_retries_have_one_effect(pg):
    s = pg.store
    op = operation()
    barrier = threading.Barrier(2)
    def create(_):
        barrier.wait()
        return s.create_block(pg.owner, op, pg.page["id"], "text", "Same effect")
    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(create, range(2)))
    assert rows[0]["id"] == rows[1]["id"]
    assert sorted(row["replay"] for row in rows) == [False, True]
    assert len(s.list_blocks(pg.owner, pg.page["id"])) == 1
    barrier = threading.Barrier(2)
    def submit(_):
        barrier.wait()
        return s.submit_task(pg.owner, operation(), "concurrent-business", "Same task", page_id=pg.page["id"],
                             scopes=[{"kind": "path", "value": pg.prefix}])
    with ThreadPoolExecutor(max_workers=2) as pool:
        tasks = list(pool.map(submit, range(2)))
    assert tasks[0]["id"] == tasks[1]["id"]
    assert sorted(row["replay"] for row in tasks) == [False, True]
    assert len([row for row in s.list_audit(pg.owner, task_id=tasks[0]["id"]) if row["event"] == "task.submitted"]) == 1


@pytest.mark.parametrize("alias", [False, True], ids=["same_declared_path", "unverified_alias"])
def test_cross_space_scope_claim_race_is_serialized_in_postgres(pg, alias):
    s = pg.store
    other_space = s.create_space(pg.editor, operation(), "Other Space")
    other_page = s.create_page(pg.editor, operation(), other_space["id"], "Other page")
    scopes = [{"kind": "path", "value": pg.prefix + "/src"}]
    first = manual(pg, scopes=scopes)
    other_scopes = [{"kind": "path", "value": pg.prefix + "/unverified-link"}] if alias else scopes
    second = s.create_task_block(pg.editor, operation(), other_page["id"], "Other work", other_scopes, [pg.reviewer.principal])
    barrier = threading.Barrier(2)
    def claim(pair):
        actor, task = pair
        barrier.wait()
        try:
            return s.claim(actor, operation(), task["id"])["state"]
        except StoreError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, [(pg.owner, first), (pg.editor, second)]))
    assert sorted(results) == ["claimed", "scope_conflict"]


def test_path_declarations_are_preserved_but_all_paths_and_modules_conflict(pg):
    s = pg.store
    first = manual(pg, "first", [{"kind": "path", "value": pg.prefix + "/src/a"}])
    sibling = manual(pg, "sibling", [{"kind": "path", "value": pg.prefix + "/src/ab"}])
    child = manual(pg, "child", [{"kind": "path", "value": pg.prefix + "/src/a/file.py"}])
    unknown = manual(pg, "module", [{"kind": "module", "value": "unknown"}])
    root = manual(pg, "root", [])
    s.claim(pg.owner, operation(), first["id"])
    for task in (sibling, child, unknown, root):
        expect("scope_conflict", lambda task=task: s.claim(pg.owner, operation(), task["id"]))
    assert s.get_scopes(pg.owner, sibling["id"])["scopes"] == [{"kind": "path", "value": pg.prefix + "/src/ab"}]
    assert normalize_scopes([{"kind": "path", "value": "Src//./A"}]) == [{"kind": "path", "value": "src/a"}]


@pytest.mark.parametrize("value", ["/absolute", "../escape", "src/../escape", "C:/drive", "src/*.py", "src/[ab]", "src\\file"])
def test_unsafe_scope_inputs_are_rejected(value):
    expect("invalid", lambda: normalize_scopes([{"kind": "path", "value": value}]))


def test_migration_scopes_compare_full_canonical_identifier(pg):
    s = pg.store
    first = manual(pg, "migration-a", [{"kind": "migration", "value": pg.prefix + "-1"}])
    second = manual(pg, "migration-b", [{"kind": "migration", "value": pg.prefix + "-10"}])
    same = manual(pg, "migration-same", [{"kind": "migration", "value": pg.prefix + "-1"}])
    s.claim(pg.owner, operation(), first["id"])
    s.claim(pg.owner, operation(), second["id"])
    expect("scope_conflict", lambda: s.claim(pg.owner, operation(), same["id"]))
    assert normalize_scopes([{"kind": "migration", "value": "001"}])[0]["value"] == "1"


def test_expired_claim_releases_without_cleaner_and_stale_replays_fail(pg):
    s = pg.store
    task = manual(pg)
    first = s.claim(pg.owner, operation(), task["id"])
    renew_op = operation()
    renewed = s.renew(pg.owner, renew_op, task["id"], first["epoch"])
    assert s.renew(pg.owner, renew_op, task["id"], first["epoch"])["lease_expires_at"] == renewed["lease_expires_at"]
    expire(pg, task["id"])
    expect("stale_claim", lambda: s.renew(pg.owner, renew_op, task["id"], first["epoch"]))
    next_claim = s.claim(pg.editor, operation(), task["id"])
    assert next_claim["epoch"] > first["epoch"]
    expect("stale_claim", lambda: s.submit_result(pg.owner, operation(), task["id"], first["epoch"], "Late result"))
    expect("stale_claim", lambda: s.handoff(pg.owner, operation(), task["id"], first["epoch"], pg.editor))


def test_lease_validity_uses_database_clock_after_waiting_for_lock(pg):
    s = pg.store
    task = manual(pg)
    claim = s.claim(pg.owner, operation(), task["id"], lease_seconds=1)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with s._transaction(write=True) as conn:
            future = pool.submit(s.renew, pg.owner, operation(), task["id"], claim["epoch"], 300)
            conn.execute("SELECT pg_sleep(1.2)")
        expect("stale_claim", future.result)


def test_handoff_preserves_context_and_fences_previous_holder(pg):
    s = pg.store
    task = manual(pg, base_commit="base-commit", context={"note": "start"}, evidence=["synthetic-evidence-1"])
    claim = s.claim(pg.owner, operation(), task["id"])
    op = operation()
    transferred = s.handoff(pg.owner, op, task["id"], claim["epoch"], pg.editor, context={"note": "continued"}, evidence=["synthetic-evidence-2"])
    assert transferred["base_commit"] == "base-commit" and transferred["thread_id"] == task["id"]
    assert transferred["context"] == [{"note": "start"}, {"note": "continued"}]
    assert transferred["evidence"] == ["synthetic-evidence-1", "synthetic-evidence-2"]
    assert s.handoff(pg.owner, op, task["id"], claim["epoch"], pg.editor, context={"note": "continued"}, evidence=["synthetic-evidence-2"])["epoch"] == transferred["epoch"]
    expect("stale_claim", lambda: s.renew(pg.owner, operation(), task["id"], claim["epoch"]))
    assert s.get_task(pg.editor, task["id"])["prompt"] == "Synthetic work task"
    assignment = next(row for row in s.fetch_inbox(pg.editor)["messages"] if row["task_id"] == task["id"])
    assert assignment["prompt"] == "Synthetic work task"
    expect("revision_conflict", lambda: s.handoff(pg.editor, operation(), task["id"], transferred["epoch"], pg.owner, base_commit="changed"))
    assert s.get_task(pg.owner, task["id"])["holder"] == pg.editor.principal


def test_handoff_cannot_fill_an_originally_null_base_commit(pg):
    s = pg.store
    task = manual(pg)
    claim = s.claim(pg.owner, operation(), task["id"])
    expect("revision_conflict", lambda: s.handoff(pg.owner, operation(), task["id"], claim["epoch"], pg.editor, base_commit="new-base"))
    assert s.get_task(pg.owner, task["id"])["holder"] == pg.owner.principal
    transferred = s.handoff(pg.owner, operation(), task["id"], claim["epoch"], pg.editor)
    assert transferred["base_commit"] is None


def test_manual_gates_reject_self_review_and_old_candidate(pg):
    s = pg.store
    task = manual(pg)
    claim = s.claim(pg.editor, operation(), task["id"])
    awaiting = s.submit_result(pg.editor, operation(), task["id"], claim["epoch"], "Candidate one")
    assert awaiting["status"] == "awaiting_gate" and not awaiting["lease_valid"]
    assert any(row["author_principal"] == pg.editor.principal and row["body"] == "Candidate one"
               for row in s.list_comments(pg.owner, pg.page["id"], task["id"]))
    expect("forbidden", lambda: s.gate(pg.editor, operation(), task["id"], claim["epoch"], "pass"))
    assert s.gate(pg.reviewer, operation(), task["id"], claim["epoch"], "fail")["state"] == "queued"
    second = s.claim(pg.editor, operation(), task["id"])
    s.submit_result(pg.editor, operation(), task["id"], second["epoch"], "Candidate two")
    expect("stale_claim", lambda: s.gate(pg.reviewer, operation(), task["id"], claim["epoch"], "pass"))
    done = s.gate(pg.reviewer, operation(), task["id"], second["epoch"], "pass")
    assert done["state"] == "done" and done["status"] == "completed"
    assert any(row["thread_id"] == task["id"] and row["body_md"] == "Candidate two" for row in s.fetch_inbox(pg.owner)["messages"])


def test_assigned_tasks_can_only_be_claimed_by_recipient_and_never_auto_dispatched(pg):
    s = pg.store
    task = s.submit_task(pg.owner, operation(), "assigned", "Assigned work", page_id=pg.page["id"], assignee=pg.editor,
                         scopes=[{"kind": "path", "value": pg.prefix}])
    assert task["assignee"] == pg.editor.principal and task["execution_mode"] == "manual"
    assert s.claim_next(SERVICE_ACTOR, operation()) is None
    expect("forbidden", lambda: s.claim(pg.reviewer, operation(), task["id"]))
    claim = s.claim(pg.editor, operation(), task["id"])
    expect("stale_claim", lambda: s.assign_task(pg.owner, operation(), task["id"], pg.reviewer))
    s.submit_result(pg.editor, operation(), task["id"], claim["epoch"], "Assignment done")
    s.gate(pg.owner, operation(), task["id"], claim["epoch"], "pass")
    expect("invalid", lambda: s.submit_task(pg.owner, operation(), "self", "No self approval", page_id=pg.page["id"], assignee=pg.owner))
    expect("forbidden", lambda: s.submit_task(pg.owner, operation(), "unshared", "No implicit grants", page_id=pg.page["id"], assignee=pg.reader))
    assert not any(task["owner"] == pg.reader.principal for task in s.list_tasks(pg.owner))


def test_human_can_review_connected_account_agent_but_agent_cannot_self_approve(pg):
    s = pg.store
    agent = Actor(pg.prefix + "-connected", pg.owner.account, "agent")
    task = s.submit_task(pg.owner, operation(), "connected", "Synthetic work", page_id=pg.page["id"], assignee=agent,
                         scopes=[{"kind": "path", "value": pg.prefix}])
    claim = s.claim(agent, operation(), task["id"])
    s.submit_result(agent, operation(), task["id"], claim["epoch"], "Connected result")
    expect("forbidden", lambda: s.gate(agent, operation(), task["id"], claim["epoch"], "pass"))
    assert s.gate(pg.owner, operation(), task["id"], claim["epoch"], "pass")["state"] == "done"


def test_assignment_changes_only_when_released_and_inbox_rechecks_access(pg):
    s = pg.store
    task = manual(pg)
    assignment_op = operation()
    assigned = s.assign_task(pg.owner, assignment_op, task["id"], pg.editor)
    assert assigned["assignee"] == pg.editor.principal
    assert s.assign_task(pg.owner, assignment_op, task["id"], pg.editor)["replay"]
    claim = s.claim(pg.editor, operation(), task["id"])
    expect("stale_claim", lambda: s.assign_task(pg.owner, operation(), task["id"], pg.reviewer))
    expire(pg, task["id"])
    assert s.assign_task(pg.owner, operation(), task["id"], pg.reviewer)["assignee"] == pg.reviewer.principal
    expect("forbidden", lambda: s.claim(pg.editor, operation(), task["id"]))
    assert s.claim(pg.reviewer, operation(), task["id"])["epoch"] > claim["epoch"]
    s.share(pg.owner, operation(), "space", pg.space["id"], "person", pg.reviewer.account, None)
    assert s.fetch_inbox(pg.reviewer)["messages"] == []


def test_task_comments_mentions_and_completion_have_full_audit_chain(pg):
    s = pg.store
    task = manual(pg, assignee=pg.editor)
    comment = s.comment(pg.owner, operation(), pg.page["id"], "Synthetic request body", thread_id=task["id"])
    s.mention(pg.owner, operation(), comment["id"], "agent", pg.editor.principal)
    s.comment(pg.editor, operation(), pg.page["id"], "Synthetic reply body", thread_id=task["id"])
    audit = s.list_audit(pg.owner, task_id=task["id"])
    assert {row["event"] for row in audit} >= {"task.assigned", "comment.created", "mention.created", "task.submitted"}
    assert all(row["task_id"] == task["id"] for row in audit)
    serialized = json.dumps(audit)
    assert "Synthetic request body" not in serialized and "Synthetic reply body" not in serialized
    assert len([item for item in s.fetch_inbox(pg.editor)["messages"] if item["thread_id"] == task["id"]]) == 1


def test_auto_queue_has_narrow_service_authority_and_ambiguous_restart_never_reexecutes(pg):
    s = pg.store
    op = operation()
    task = s.submit_task(pg.owner, op, "auto", "Synthetic auto", page_id=pg.page["id"], scopes=[{"kind": "path", "value": pg.prefix}])
    expect("forbidden", lambda: s.get_task(SERVICE_ACTOR, task["id"]))
    expect("forbidden", lambda: s.claim_next(pg.owner, operation()))
    claim = s.claim_next(SERVICE_ACTOR, operation())
    assert claim["task_id"] == task["id"]
    s.mark_dispatched(SERVICE_ACTOR, operation(), task["id"], claim["epoch"])
    expire(pg, task["id"])
    s.recover_auto(SERVICE_ACTOR, operation())
    assert s.get_task(pg.owner, task["id"])["status"] == "interrupted"
    assert s.claim_next(SERVICE_ACTOR, operation()) is None
    assert s.submit_task(pg.owner, op, "auto", "Synthetic auto", page_id=pg.page["id"], scopes=[{"kind": "path", "value": pg.prefix}])["status"] == "interrupted"
    expect("stale_claim", lambda: s.finish_auto(SERVICE_ACTOR, operation(), task["id"], claim["epoch"], "Late", operation(), "demo"))


def test_auto_gate_requires_dispatched_completed_evidence_and_same_thread(pg):
    s = pg.store
    task = s.submit_task(pg.owner, operation(), "auto-success", "Synthetic auto", page_id=pg.page["id"], scopes=[{"kind": "path", "value": pg.prefix}])
    claim = s.claim_next(SERVICE_ACTOR, operation())
    a2a = operation()
    expect("gate_required", lambda: s.finish_auto(SERVICE_ACTOR, operation(), task["id"], claim["epoch"], "Demo output", a2a, "demo"))
    s.mark_dispatched(SERVICE_ACTOR, operation(), task["id"], claim["epoch"])
    expect("gate_required", lambda: s.finish_auto(SERVICE_ACTOR, operation(), task["id"], claim["epoch"], "Demo output", a2a, "demo", "failed"))
    finish = operation()
    done = s.finish_auto(SERVICE_ACTOR, finish, task["id"], claim["epoch"], "Demo output", a2a, "demo")
    assert done["backend"] == "demo" and done["status"] == "completed"
    assert s.finish_auto(SERVICE_ACTOR, finish, task["id"], claim["epoch"], "Demo output", a2a, "demo")["replay"]
    rows = s.list_audit(pg.owner, task_id=task["id"])
    assert len([r for r in rows if r["event"] == "task.completed"]) == 1
    assert {row["event"] for row in rows} >= {"task.submitted", "thread.submitted", "worker.claimed", "task.dispatched", "worker.completed", "task.completed", "thread.completed"}
    assert any(row["thread_id"] == task["id"] and row["body_md"] == "Demo output" for row in s.fetch_inbox(pg.owner)["messages"])


def test_transaction_failure_rolls_back_business_operation_and_audit(pg, monkeypatch):
    s = pg.store
    name = pg.prefix + "-rolled-back"
    op = operation()
    def fail(*args, **kwargs):
        raise StoreError("invalid")
    monkeypatch.setattr(s, "_audit", fail)
    expect("invalid", lambda: s.create_space(pg.owner, op, name))
    with s._transaction() as conn:
        assert not conn.execute("SELECT 1 FROM baton.spaces WHERE name=%s", (name,)).fetchone()
        assert not conn.execute("SELECT 1 FROM baton.operations WHERE operation_id=%s", (op,)).fetchone()
        assert not conn.execute("SELECT 1 FROM baton.x_audit WHERE operation_id=%s", (op,)).fetchone()


def test_runtime_role_cannot_change_delete_truncate_audit_or_own_schema(pg):
    with pg.store._transaction() as conn:
        role = conn.execute("SELECT current_user AS name").fetchone()
        assert role["name"] == "baton_app"
        for statement in ("UPDATE baton.x_audit SET event='tampered'", "DELETE FROM baton.x_audit", "TRUNCATE baton.x_audit"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with conn.transaction():
                    conn.execute(statement)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.transaction():
                conn.execute("SET ROLE baton_owner")


def test_audit_triggers_reject_even_owner_dml_and_truncate(pg):
    config_path = os.environ.get("BATON_TEST_DATABASE_ADMIN_CONFIG")
    if not config_path:
        pytest.skip("Owner-trigger check needs explicitly authorized test admin config")
    config = json.loads(Path(config_path).read_text())
    with psycopg.connect(**config) as conn:
        conn.execute("SET ROLE baton_owner")
        for statement in ("UPDATE baton.x_audit SET event='tampered'", "DELETE FROM baton.x_audit", "TRUNCATE baton.x_audit"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with conn.transaction():
                    conn.execute(statement)


def test_checkpoint_migration_is_repeatable_and_readiness_rejects_missing_assignee(pg):
    config_path = os.environ.get("BATON_TEST_DATABASE_ADMIN_CONFIG")
    if not config_path:
        pytest.skip("Checkpoint migration check needs explicitly authorized test admin config")
    config = json.loads(Path(config_path).read_text())
    schema = (Path(__file__).parents[1] / "bridge/space_schema.sql").read_text()
    with psycopg.connect(**config, autocommit=True) as conn:
        conn.execute("SET ROLE baton_owner")
        conn.execute(schema)
        conn.execute(schema)
        conn.execute("ALTER TABLE baton.tasks RENAME COLUMN assignee TO synthetic_missing_assignee")
        try:
            expect("storage_unavailable", lambda: pg.store.check_schema(SERVICE_ACTOR))
        finally:
            conn.execute("ALTER TABLE baton.tasks RENAME COLUMN synthetic_missing_assignee TO assignee")
    assert pg.store.check_schema(SERVICE_ACTOR)["status"] == "ok"


def test_connection_errors_do_not_contain_credentials(tmp_path):
    path = tmp_path / "synthetic-database.json"
    marker = "synthetic-password-never-display"
    path.write_text(json.dumps({"host": "127.0.0.1", "port": 1, "dbname": "synthetic", "user": "synthetic", "password": marker}))
    with pytest.raises(StoreError) as caught:
        SpaceStore(path).check_schema(SERVICE_ACTOR)
    assert caught.value.code == "storage_unavailable"
    assert str(caught.value) == "Space storage is unavailable"


def test_invalid_json_enum_shapes_have_safe_validation_errors():
    store = SpaceStore("/synthetic/nonexistent-config.json")
    actor = Actor("synthetic-principal", "synthetic-account", "human")
    expect("invalid", lambda: normalize_scopes([{"kind": [], "value": "src"}]))
    expect("invalid", lambda: store.create_block(actor, operation(), operation(), [], "content"))
    expect("invalid", lambda: store.share(actor, operation(), "space", operation(), "person", "reader", []))
    expect("invalid", lambda: store.gate(actor, operation(), operation(), 1, []))


def test_explicit_request_promotes_assignment_notice_and_sorts_by_latest_activity(pg):
    s = pg.store
    task = s.submit_task(pg.owner, operation(), "promote-notice", "Assigned instructions", page_id=pg.page["id"], assignee=pg.editor)
    submitted = s.list_comments(pg.owner, pg.page["id"], task["id"])[0]
    later = s.comment(pg.owner, operation(), pg.page["id"], "Another thread")
    s.mention(pg.owner, operation(), later["id"], "agent", pg.editor.principal)
    assert s.fetch_inbox(pg.editor, limit=1)["messages"][0]["thread_id"] == later["thread_id"]
    promoted = s.mention(pg.owner, operation(), submitted["id"], "agent", pg.editor.principal)
    assert promoted["action_requested"] is True
    message = s.fetch_inbox(pg.editor, limit=1)["messages"][0]
    assert message["thread_id"] == task["id"] and message["action_requested"] is True
    assert message["requested_by"] == pg.owner.principal
    assert message["created_ts"] > message["comment_created_at"]
    before = s.list_audit(pg.owner, task_id=task["id"])
    repeated = s.mention(pg.owner, operation(), submitted["id"], "agent", pg.editor.principal)
    assert repeated["created_at"] == promoted["created_at"] and repeated["replay"]
    assert len(s.list_audit(pg.owner, task_id=task["id"])) == len(before)


def test_graph_blocks_claim_until_dependency_has_current_epoch_pass(pg):
    s = pg.store
    parent = manual(pg, "parent")
    child = manual(pg, "child", assignee=pg.editor)
    plan = s.plan(pg.owner, operation(), [{"task_id": child["id"], "depends_on": [parent["id"]]}])
    assert plan["graph"] == [{"task_id": child["id"], "depends_on": [parent["id"]]}]
    expect("dependency_blocked", lambda: s.claim(pg.editor, operation(), child["id"]))
    assert child["id"] not in {row["id"] for row in s.ready(pg.editor, pg.page["id"])}
    parent_claim = s.claim(pg.editor, operation(), parent["id"])
    s.submit_result(pg.editor, operation(), parent["id"], parent_claim["epoch"], "Private dependency result")
    expect("dependency_blocked", lambda: s.claim(pg.editor, operation(), child["id"]))
    assert child["id"] not in {row["id"] for row in s.ready(pg.editor, pg.page["id"])}
    s.gate(pg.reviewer, operation(), parent["id"], parent_claim["epoch"], "pass")
    assert child["id"] in {row["id"] for row in s.ready(pg.editor, pg.page["id"])}
    # A historical PASS does not approve a later generation, even if its state
    # were incorrectly labelled done by an out-of-band repair.
    with s._transaction(write=True) as conn:
        conn.execute("UPDATE baton.tasks SET epoch=epoch+1,result_epoch=result_epoch+1 WHERE id=%s", (parent["id"],))
    expect("dependency_blocked", lambda: s.claim(pg.editor, operation(), child["id"]))
    assert child["id"] not in {row["id"] for row in s.ready(pg.editor, pg.page["id"])}
    with s._transaction(write=True) as conn:
        conn.execute("UPDATE baton.tasks SET state='awaiting_gate' WHERE id=%s", (parent["id"],))
    s.gate(pg.reviewer, operation(), parent["id"], parent_claim["epoch"] + 1, "pass")
    claimed = s.claim(pg.editor, operation(), child["id"])
    assert claimed["depends_on"] == [parent["id"]]
    assert "Private dependency result" not in json.dumps(claimed)


def test_graph_plan_appends_idempotently_and_reauthorizes_replays(pg):
    s = pg.store
    first, second, child = manual(pg, "first"), manual(pg, "second"), manual(pg, "child")
    op = operation()
    graph = [{"task_id": child["id"], "depends_on": [first["id"]]}]
    initial = s.plan(pg.editor, op, graph)
    assert s.plan(pg.editor, op, graph)["replay"]
    expect("operation_conflict", lambda: s.plan(pg.editor, op, [{"task_id": child["id"], "depends_on": [second["id"]]}]))
    added = s.plan(pg.editor, operation(), [{"task_id": child["id"], "depends_on": [second["id"]]}])
    assert added["graph"][0]["depends_on"] == sorted([first["id"], second["id"]])
    assert s.plan(pg.editor, operation(), [{"task_id": child["id"], "depends_on": []}])["graph"] == added["graph"]
    assert initial["graph"][0]["depends_on"] == [first["id"]]
    events = s.list_audit(pg.owner, task_id=child["id"])
    assert len([event for event in events if event["event"] == "task.dependencies_added"]) == 2
    s.share(pg.owner, operation(), "space", pg.space["id"], "person", pg.editor.account, None)
    expect("forbidden", lambda: s.plan(pg.editor, op, graph))


def test_graph_cycle_batch_is_atomic_and_concurrent_opposite_edges_cannot_both_commit(pg):
    s = pg.store
    a, b, c = manual(pg, "a"), manual(pg, "b"), manual(pg, "c")
    s.plan(pg.owner, operation(), [{"task_id": a["id"], "depends_on": [b["id"]]}])
    failed_op = operation()
    expect("graph_cycle", lambda: s.plan(pg.owner, failed_op, [
        {"task_id": b["id"], "depends_on": [c["id"]]}, {"task_id": c["id"], "depends_on": [a["id"]]},
    ]))
    assert s.get_task(pg.owner, b["id"])["depends_on"] == []
    assert s.get_task(pg.owner, c["id"])["depends_on"] == []
    with s._transaction() as conn:
        assert not conn.execute("SELECT 1 FROM baton.operations WHERE operation_id=%s", (failed_op,)).fetchone()
        assert not conn.execute("SELECT 1 FROM baton.x_audit WHERE operation_id=%s", (failed_op,)).fetchone()
    x, y = manual(pg, "x"), manual(pg, "y")
    barrier = threading.Barrier(2)
    def append(pair):
        target, dependency = pair
        barrier.wait()
        try:
            s.plan(pg.owner, operation(), [{"task_id": target["id"], "depends_on": [dependency["id"]]}])
            return "planned"
        except StoreError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(append, [(x, y), (y, x)]))
    assert sorted(results) == ["graph_cycle", "planned"]
    assert len(s.get_task(pg.owner, x["id"])["depends_on"]) + len(s.get_task(pg.owner, y["id"])["depends_on"]) == 1


def test_graph_cannot_modify_any_previously_claimed_or_executed_task(pg):
    s = pg.store
    parent, child = manual(pg, "parent"), manual(pg, "child")
    claim = s.claim(pg.editor, operation(), child["id"])
    graph = [{"task_id": child["id"], "depends_on": [parent["id"]]}]
    expect("stale_claim", lambda: s.plan(pg.owner, operation(), graph))
    expire(pg, child["id"])
    expect("stale_claim", lambda: s.plan(pg.owner, operation(), graph))
    with s._transaction() as conn:
        raw = conn.execute("SELECT state,epoch FROM baton.tasks WHERE id=%s", (child["id"],)).fetchone()
        assert raw == {"state": "claimed", "epoch": claim["epoch"]}
    assert s.get_task(pg.owner, child["id"])["depends_on"] == []
    expect("stale_claim", lambda: s.renew(pg.editor, operation(), child["id"], claim["epoch"]))
    terminal = manual(pg, "terminal")
    terminal_claim = s.claim(pg.editor, operation(), terminal["id"])
    # Another legitimate claim may sweep expired leases; a positive epoch still
    # proves prior ownership and cannot be treated as a fresh planning target.
    with s._transaction() as conn:
        assert conn.execute("SELECT state FROM baton.tasks WHERE id=%s", (child["id"],)).fetchone()["state"] == "queued"
    expect("stale_claim", lambda: s.plan(pg.owner, operation(), graph))
    s.submit_result(pg.editor, operation(), terminal["id"], terminal_claim["epoch"], "Candidate")
    terminal_graph = [{"task_id": terminal["id"], "depends_on": [parent["id"]]}]
    expect("stale_claim", lambda: s.plan(pg.owner, operation(), terminal_graph))
    s.gate(pg.reviewer, operation(), terminal["id"], terminal_claim["epoch"], "fail")
    assert s.get_task(pg.owner, terminal["id"])["state"] == "queued"
    expect("stale_claim", lambda: s.plan(pg.owner, operation(), terminal_graph))
    next_claim = s.claim(pg.editor, operation(), terminal["id"])
    s.submit_result(pg.editor, operation(), terminal["id"], next_claim["epoch"], "Revised candidate")
    s.gate(pg.reviewer, operation(), terminal["id"], next_claim["epoch"], "pass")
    expect("stale_claim", lambda: s.plan(pg.owner, operation(), terminal_graph))
    fresh = manual(pg, "never-claimed")
    assert s.plan(pg.owner, operation(), [{"task_id": fresh["id"], "depends_on": [parent["id"]]}])["graph"][0]["depends_on"] == [parent["id"]]


def test_ready_filters_assignment_role_and_scopes_and_is_only_a_hint(pg):
    s = pg.store
    assigned = manual(pg, "assigned", assignee=pg.editor)
    other = manual(pg, "other")
    assert assigned["id"] not in {row["id"] for row in s.ready(pg.owner, pg.page["id"])}
    s.share(pg.owner, operation(), "space", pg.space["id"], "person", pg.reader.account, "view")
    assert s.ready(pg.reader, pg.page["id"]) == []
    s.claim(pg.reviewer, operation(), other["id"])
    assert s.ready(pg.editor, pg.page["id"]) == []
    expire(pg, other["id"])
    assert assigned["id"] in {row["id"] for row in s.ready(pg.editor, pg.page["id"])}
    s.claim(pg.owner, operation(), other["id"])
    expect("scope_conflict", lambda: s.claim(pg.editor, operation(), assigned["id"]))
    s.share(pg.owner, operation(), "space", pg.space["id"], "person", pg.editor.account, None)
    assert s.ready(pg.editor) == []


def test_graph_requires_dependency_visibility_without_exposing_private_content(pg):
    s = pg.store
    private_space = s.create_space(pg.owner, operation(), "Private dependencies")
    private_page = s.create_page(pg.owner, operation(), private_space["id"], "Private page")
    private_task = s.create_task_block(pg.owner, operation(), private_page["id"], "PRIVATE-DEPENDENCY-BODY", [], [pg.owner.principal])
    child = manual(pg, "child", assignee=pg.editor, depends_on=[private_task["id"]])
    expect("forbidden", lambda: s.plan(pg.editor, operation(), [{"task_id": child["id"], "depends_on": [private_task["id"]]}]))
    visible = s.get_task(pg.editor, child["id"])
    assert visible["depends_on"] == [] and visible["has_hidden_dependencies"]
    assert private_task["id"] not in json.dumps(visible)
    assert "PRIVATE-DEPENDENCY-BODY" not in json.dumps(visible)
    inbox = next(row for row in s.fetch_inbox(pg.editor)["messages"] if row.get("task_id") == child["id"])
    assert inbox["depends_on"] == [] and inbox["has_hidden_dependencies"]
    assert private_task["id"] not in json.dumps(inbox)
    assert child["id"] not in {row["id"] for row in s.ready(pg.editor)}
    expect("forbidden", lambda: s.claim(pg.editor, operation(), child["id"]))
    expect("forbidden", lambda: s.get_task(pg.editor, private_task["id"]))
    before = len(s.list_blocks(pg.owner, pg.page["id"]))
    for automatic in (False, True):
        op = operation()
        if automatic:
            call = lambda: s.submit_task(pg.editor, op, "invisible-dep", "Forbidden node", page_id=pg.page["id"], depends_on=[private_task["id"]])
        else:
            call = lambda: s.create_task_block(pg.editor, op, pg.page["id"], "Forbidden node", [], [pg.reviewer.principal], depends_on=[private_task["id"]])
        expect("forbidden", call)
        with s._transaction() as conn:
            assert not conn.execute("SELECT 1 FROM baton.operations WHERE operation_id=%s", (op,)).fetchone()
    assert len(s.list_blocks(pg.owner, pg.page["id"])) == before


def test_creation_dependencies_are_atomic_with_auto_queue_admission(pg):
    s = pg.store
    parent = manual(pg, "parent")
    child = s.submit_task(pg.owner, operation(), "auto-dependent", "Synthetic dependent auto task",
                         page_id=pg.page["id"], depends_on=[parent["id"]])
    assert child["depends_on"] == [parent["id"]]
    assert s.claim_next(SERVICE_ACTOR, operation()) is None
    parent_claim = s.claim(pg.editor, operation(), parent["id"])
    s.submit_result(pg.editor, operation(), parent["id"], parent_claim["epoch"], "Candidate")
    assert s.claim_next(SERVICE_ACTOR, operation()) is None
    s.gate(pg.reviewer, operation(), parent["id"], parent_claim["epoch"], "pass")
    claimed = s.claim_next(SERVICE_ACTOR, operation())
    assert claimed["task_id"] == child["id"] and claimed["depends_on"] == [parent["id"]]
    s.mark_dispatched(SERVICE_ACTOR, operation(), child["id"], claimed["epoch"])
    assert s.finish_auto(SERVICE_ACTOR, operation(), child["id"], claimed["epoch"], "Synthetic graph demo", operation(), "demo")["state"] == "done"


def test_empty_dependencies_preserve_v1_persisted_idempotency_hashes(pg):
    s = pg.store
    scopes = [{"kind": "path", "value": pg.prefix}]
    actor_fields = {"principal": pg.owner.principal, "account": pg.owner.account, "kind": pg.owner.kind}
    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()).hexdigest()
    submit_op = operation()
    task = s.submit_task(pg.owner, submit_op, "legacy-no-deps", "Legacy auto task", page_id=pg.page["id"], scopes=scopes)
    legacy_submit_args = {"idempotency_key": "legacy-no-deps", "prompt": "Legacy auto task", "page_id": pg.page["id"], "scopes": scopes, "assignee": None}
    with s._transaction() as conn:
        assert conn.execute("SELECT input_hash FROM baton.tasks WHERE id=%s", (task["id"],)).fetchone()["input_hash"] == digest(legacy_submit_args)
        assert conn.execute("SELECT request_hash FROM baton.operations WHERE principal=%s AND operation_id=%s", (pg.owner.principal, submit_op)).fetchone()["request_hash"] == digest([actor_fields, "submit_task", legacy_submit_args])
    assert s.submit_task(pg.owner, submit_op, "legacy-no-deps", "Legacy auto task", page_id=pg.page["id"], scopes=scopes, depends_on=[])["replay"]
    assert s.submit_task(pg.owner, operation(), "legacy-no-deps", "Legacy auto task", page_id=pg.page["id"], scopes=scopes, depends_on=[])["id"] == task["id"]
    create_op = operation()
    manual_task = s.create_task_block(pg.owner, create_op, pg.page["id"], "Legacy manual task", scopes, [pg.reviewer.principal])
    legacy_create_args = {"page_id": pg.page["id"], "prompt": "Legacy manual task", "scopes": scopes, "reviewers": [pg.reviewer.principal],
                          "base_commit": None, "context": None, "evidence": None, "assignee": None}
    with s._transaction() as conn:
        assert conn.execute("SELECT input_hash FROM baton.tasks WHERE id=%s", (manual_task["id"],)).fetchone()["input_hash"] == digest(legacy_create_args)
        assert conn.execute("SELECT request_hash FROM baton.operations WHERE principal=%s AND operation_id=%s", (pg.owner.principal, create_op)).fetchone()["request_hash"] == digest([actor_fields, "create_task_block", legacy_create_args])
    s.plan(pg.owner, operation(), [{"task_id": task["id"], "depends_on": [manual_task["id"]]}])
    replay = s.submit_task(pg.owner, submit_op, "legacy-no-deps", "Legacy auto task", page_id=pg.page["id"], scopes=scopes)
    assert replay["depends_on"] == [manual_task["id"]] and replay["replay"]
    assert s.create_task_block(pg.owner, create_op, pg.page["id"], "Legacy manual task", scopes, [pg.reviewer.principal], depends_on=[])["replay"]


def test_graph_rejects_self_cycles_and_non_contract_payload_shapes(pg):
    s = pg.store
    task = manual(pg)
    expect("graph_cycle", lambda: s.plan(pg.owner, operation(), [{"task_id": task["id"], "depends_on": [task["id"]]}]))
    expect("invalid", lambda: s.plan(pg.owner, operation(), [{"task_id": task["id"], "depends_on": [], "state": "done"}]))
    expect("invalid", lambda: s.plan(pg.owner, operation(), [{"task_id": task["id"], "depends_on": None}]))
    expect("invalid", lambda: s.plan(pg.owner, operation(), [{"task_id": task["id"], "depends_on": []}] * 2))


def completed_private_dependency(pg, suffix="private"):
    s = pg.store
    space = s.create_space(pg.owner, operation(), "Private dependency " + suffix)
    page = s.create_page(pg.owner, operation(), space["id"], "Private dependency page")
    parent = s.submit_task(pg.owner, operation(), "dependency-" + suffix, "Private dependency instructions", page_id=page["id"])
    claim = s.claim_next(SERVICE_ACTOR, operation())
    assert claim["task_id"] == parent["id"]
    s.mark_dispatched(SERVICE_ACTOR, operation(), parent["id"], claim["epoch"])
    s.finish_auto(SERVICE_ACTOR, operation(), parent["id"], claim["epoch"], "Private dependency output", operation(), "demo")
    return page, parent


def test_revoked_dependency_blocks_ready_claim_and_plan_replay_without_id_leak(pg):
    s = pg.store
    page, parent = completed_private_dependency(pg)
    s.share(pg.owner, operation(), "page", page["id"], "person", pg.editor.account, "view")
    child = s.create_task_block(pg.editor, operation(), pg.page["id"], "Public child", [], [pg.reviewer.principal],
                                assignee=pg.editor, depends_on=[parent["id"]])
    plan_op = operation()
    original_plan = [{"task_id": child["id"], "depends_on": []}]
    s.plan(pg.editor, plan_op, original_plan)
    assert child["id"] in {row["id"] for row in s.ready(pg.editor)}
    s.share(pg.owner, operation(), "page", page["id"], "person", pg.editor.account, None)
    views = [s.get_task(pg.editor, child["id"]), next(row for row in s.list_tasks(pg.editor) if row["id"] == child["id"]),
             next(row for row in s.fetch_inbox(pg.editor)["messages"] if row.get("task_id") == child["id"])]
    for view in views:
        assert view["depends_on"] == [] and view["has_hidden_dependencies"] is True
        assert parent["id"] not in json.dumps(view)
    assert child["id"] not in {row["id"] for row in s.ready(pg.editor)}
    for call in (lambda: s.claim(pg.editor, operation(), child["id"]),
                 lambda: s.plan(pg.editor, operation(), original_plan), lambda: s.plan(pg.editor, plan_op, original_plan)):
        with pytest.raises(StoreError) as caught:
            call()
        assert caught.value.code == "forbidden" and parent["id"] not in str(caught.value)
    s.share(pg.owner, operation(), "page", page["id"], "person", pg.editor.account, "view")
    other_page, other_parent = completed_private_dependency(pg, "later")
    s.plan(pg.owner, operation(), [{"task_id": child["id"], "depends_on": [other_parent["id"]]}])
    # Replaying an older operation must inspect edges added since that receipt.
    expect("forbidden", lambda: s.plan(pg.editor, plan_op, original_plan))
    projected = s.get_task(pg.editor, child["id"])
    assert projected["depends_on"] == [parent["id"]] and projected["has_hidden_dependencies"]
    assert other_parent["id"] not in json.dumps(projected)


@pytest.mark.parametrize("lost_permission", ["dependency_view", "task_edit"])
@pytest.mark.parametrize("after_claim", [False, True], ids=["before_claim", "before_dispatch"])
def test_auto_rechecks_saved_requester_authority_without_impersonating_audit_actor(pg, lost_permission, after_claim):
    s = pg.store
    page, parent = completed_private_dependency(pg)
    s.share(pg.owner, operation(), "page", page["id"], "person", pg.editor.account, "view")
    child = s.submit_task(pg.editor, operation(), "requester-authority", "Authorized child", page_id=pg.page["id"], depends_on=[parent["id"]])
    claim_op = operation()
    claim = s.claim_next(SERVICE_ACTOR, claim_op) if after_claim else None
    if after_claim:
        assert claim["task_id"] == child["id"]
    if lost_permission == "dependency_view":
        resource_type, resource_id = "page", page["id"]
    else:
        resource_type, resource_id = "space", pg.space["id"]
    s.share(pg.owner, operation(), resource_type, resource_id, "person", pg.editor.account, None)
    if after_claim:
        expect("forbidden", lambda: s.claim_next(SERVICE_ACTOR, claim_op))
        expect("forbidden", lambda: s.mark_dispatched(SERVICE_ACTOR, operation(), child["id"], claim["epoch"]))
        assert s.interrupt_auto(SERVICE_ACTOR, operation(), child["id"], claim["epoch"], "cancelled")["state"] == "interrupted"
    else:
        assert s.claim_next(SERVICE_ACTOR, claim_op) is None
    with s._transaction() as conn:
        raw = conn.execute("SELECT state,epoch,dispatch_started_at FROM baton.tasks WHERE id=%s", (child["id"],)).fetchone()
    assert raw["dispatch_started_at"] is None
    assert raw["epoch"] == (claim["epoch"] if after_claim else 0)
    rows = s.list_audit(pg.owner, task_id=child["id"])
    assert not any(row["event"] == "task.dispatched" for row in rows)
    service_rows = [row for row in rows if row["event"] in {"worker.claimed", "task.interrupted"}]
    assert all(row["performed_by"] == SERVICE_ACTOR.principal and row["requested_by"] == pg.editor.principal for row in service_rows)
