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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest

from bridge.space_store import Actor, SERVICE_ACTOR, SpaceStore, StoreError, LOCK_ID, normalize_scopes, normalize_work_face


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
    expect("not_found", lambda: s.get_page(pg.reader, child["id"]))


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
    expect("not_found", lambda: s.get_page(pg.editor, page["id"]))
    expect("not_found", lambda: s.create_block(pg.editor, block_op, page["id"], "text", "Synthetic content"))
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
    expect("not_found", lambda: s.update_block(pg.editor, op, block["id"], 1, "Updated"))
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


@pytest.mark.parametrize("nested", [False, True], ids=["same_declared_path", "nested_path"])
def test_cross_space_scope_claim_race_is_serialized_in_postgres(pg, nested):
    s = pg.store
    other_space = s.create_space(pg.editor, operation(), "Other Space")
    other_page = s.create_page(pg.editor, operation(), other_space["id"], "Other page")
    scopes = [{"kind": "path", "value": pg.prefix + "/src"}]
    first = manual(pg, scopes=scopes)
    other_scopes = [{"kind": "path", "value": pg.prefix + "/src/nested"}] if nested else scopes
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


def test_path_declarations_are_preserved_and_conflict_by_prefix_and_module(pg):
    s = pg.store
    first = manual(pg, "first", [{"kind": "path", "value": pg.prefix + "/src/a"}])
    sibling = manual(pg, "sibling", [{"kind": "path", "value": pg.prefix + "/src/ab"}])
    other_tree = manual(pg, "other-tree", [{"kind": "path", "value": pg.prefix + "/docs/b"}])
    child = manual(pg, "child", [{"kind": "path", "value": pg.prefix + "/src/a/file.py"}])
    parent = manual(pg, "parent", [{"kind": "path", "value": pg.prefix + "/src"}])
    unknown = manual(pg, "module", [{"kind": "module", "value": "unknown"}])
    root = manual(pg, "root", [])
    s.claim(pg.owner, operation(), first["id"])
    for task in (child, parent, unknown, root):
        expect("scope_conflict", lambda task=task: s.claim(pg.owner, operation(), task["id"]))
    for task in (sibling, other_tree):
        assert s.claim(pg.owner, operation(), task["id"])["state"] == "claimed"
    assert s.get_scopes(pg.owner, sibling["id"])["scopes"] == [{"kind": "path", "value": pg.prefix + "/src/ab"}]
    assert normalize_scopes([{"kind": "path", "value": "Src//./A/"}]) == [{"kind": "path", "value": "Src/A"}]


def test_parallel_claims_with_disjoint_paths_succeed_and_overlapping_claims_are_refused(pg):
    s = pg.store
    src = manual(pg, "src", [{"kind": "path", "value": pg.prefix + "/src/a"}])
    docs = manual(pg, "docs", [{"kind": "path", "value": pg.prefix + "/docs/b"}])
    inside = manual(pg, "inside", [{"kind": "path", "value": pg.prefix + "/src/a/deep/file.py"}])
    enclosing = manual(pg, "enclosing", [{"kind": "path", "value": pg.prefix + "/docs"}])
    assert s.claim(pg.owner, operation(), src["id"])["state"] == "claimed"
    assert s.claim(pg.owner, operation(), docs["id"])["state"] == "claimed"
    expect("scope_conflict", lambda: s.claim(pg.owner, operation(), inside["id"]))
    expect("scope_conflict", lambda: s.claim(pg.owner, operation(), enclosing["id"]))


def test_ready_uses_prefix_overlap(pg):
    s = pg.store
    held = manual(pg, "held", [{"kind": "path", "value": pg.prefix + "/src/a"}], assignee=pg.editor)
    disjoint = manual(pg, "disjoint", [{"kind": "path", "value": pg.prefix + "/docs/b"}], assignee=pg.editor)
    nested = manual(pg, "nested", [{"kind": "path", "value": pg.prefix + "/src/a/x"}], assignee=pg.editor)
    s.claim(pg.editor, operation(), held["id"])
    ready = {task["id"] for task in s.ready(pg.editor, pg.page["id"])}
    assert disjoint["id"] in ready and nested["id"] not in ready


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


def test_manual_gates_reject_self_review_and_old_candidate(pg, approver):
    s = pg.store
    approver(s, pg.reviewer, by=pg.owner)
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


def test_assigned_tasks_can_only_be_claimed_by_their_recipient(pg):
    s = pg.store
    task = s.submit_task(pg.owner, operation(), "assigned", "Assigned work", page_id=pg.page["id"], assignee=pg.editor,
                         scopes=[{"kind": "path", "value": pg.prefix}])
    assert task["assignee"] == pg.editor.principal and task["execution_mode"] == "manual"
    expect("forbidden", lambda: s.claim(pg.reviewer, operation(), task["id"]))
    claim = s.claim(pg.editor, operation(), task["id"])
    expect("stale_claim", lambda: s.assign_task(pg.owner, operation(), task["id"], pg.reviewer))
    s.submit_result(pg.editor, operation(), task["id"], claim["epoch"], "Assignment done")
    s.gate(pg.owner, operation(), task["id"], claim["epoch"], "pass")
    expect("invalid", lambda: s.submit_task(pg.owner, operation(), "self", "No self approval", page_id=pg.page["id"], assignee=pg.owner))
    expect("forbidden", lambda: s.submit_task(pg.owner, operation(), "unshared", "No implicit grants", page_id=pg.page["id"], assignee=pg.reader))
    assert not any(task["owner"] == pg.reader.principal for task in s.list_tasks(pg.owner))


def test_agent_on_the_owner_account_is_not_root_and_owner_account_work_needs_the_flag(pg, approver):
    s = pg.store
    approver(s, pg.reviewer, by=pg.owner)
    agent = Actor(pg.prefix + "-connected", pg.owner.account, "agent")
    task = s.create_task_block(pg.owner, operation(), pg.page["id"], "Synthetic connected", [{"kind": "path", "value": pg.prefix}],
                               [pg.reviewer.principal], assignee=agent)
    claim = s.claim(agent, operation(), task["id"])
    s.submit_result(agent, operation(), task["id"], claim["epoch"], "Connected result")
    expect("forbidden", lambda: s.gate(agent, operation(), task["id"], claim["epoch"], "pass"))
    expect("forbidden", lambda: s.gate(pg.owner, operation(), task["id"], claim["epoch"], "pass"))
    assert s.gate(pg.reviewer, operation(), task["id"], claim["epoch"], "pass")["state"] == "done"


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


def to_legacy(pg, task_id, *, state="queued", dispatched=False, backend=None, error=None, result=None, epoch=0, claim_lapsed=False):
    """Rewrite a task into the shape the removed built-in worker left behind; nothing writes these values any more."""
    with pg.store._transaction(write=True) as conn:
        conn.execute("""UPDATE baton.tasks SET execution_mode='codex',gate_mode='worker_success',reviewers='[]'::jsonb,scopes=%s::jsonb,
            assignee=NULL,state=%s,backend=%s,error=%s,result=%s,result_epoch=%s,epoch=%s,
            dispatch_started_at=CASE WHEN %s THEN clock_timestamp() END WHERE id=%s""",
                     (json.dumps(ROOT), state, backend, error, result, epoch if result is not None else None, epoch, dispatched, task_id))
        if claim_lapsed:
            conn.execute("""INSERT INTO baton.x_claim(task_id,holder,holder_account,holder_kind,epoch,lease_expires_at,scopes)
                VALUES (%s,'codex-worker','baton-service','agent',%s,clock_timestamp()-interval '1 second',%s::jsonb)""",
                         (task_id, epoch, json.dumps(ROOT)))
    return task_id


def test_legacy_codex_rows_stay_readable_and_no_automatic_path_can_touch_them(pg):
    s = pg.store
    scopes = [{"kind": "path", "value": pg.prefix + "/legacy"}]
    queued = s.submit_task(pg.owner, operation(), "legacy-queued", "Legacy queued prompt", page_id=pg.page["id"], scopes=scopes)
    to_legacy(pg, queued["id"])
    view = s.get_task(pg.owner, queued["id"])
    assert (view["execution_mode"], view["gate_mode"], view["status"], view["scopes"]) == ("codex", "worker_success", "queued", ROOT)
    assert "prompt" not in view and view["approvals_required"] is None
    assert s.submit_task(pg.owner, operation(), "legacy-queued", "Legacy queued prompt", page_id=pg.page["id"], scopes=scopes)["id"] == queued["id"]
    assert queued["id"] not in ready_ids(pg, pg.editor)
    expect("forbidden", lambda: s.claim(pg.editor, operation(), queued["id"]))
    expect("forbidden", lambda: s.gate(pg.owner, operation(), queued["id"], 1, "pass"))
    expect("forbidden", lambda: s.get_task(SERVICE_ACTOR, queued["id"]))
    for removed in ("claim_next", "recover_auto", "mark_dispatched", "finish_auto", "fail_auto", "interrupt_auto", "renew_auto"):
        assert not hasattr(s, removed), removed

    dispatched = s.submit_task(pg.owner, operation(), "legacy-dispatched", "Legacy dispatched", page_id=pg.page["id"])
    to_legacy(pg, dispatched["id"], state="claimed", dispatched=True, backend="codex", epoch=1, claim_lapsed=True)
    view = s.get_task(pg.owner, dispatched["id"])
    assert view["status"] == "interrupted" and view["dispatch_started_at"] is not None and view["backend"] == "codex"
    expect("stale_claim", lambda: s.assign_task(pg.owner, operation(), dispatched["id"], pg.editor))

    finished = s.submit_task(pg.owner, operation(), "legacy-done", "Legacy done", page_id=pg.page["id"])
    to_legacy(pg, finished["id"], state="done", dispatched=True, backend="demo", result="Demo output", epoch=1)
    view = s.get_task(pg.owner, finished["id"])
    assert (view["status"], view["result"], view["backend"], view["approvals_required"]) == ("completed", "Demo output", "demo", None)
    assert finished["id"] in {row["id"] for row in s.list_tasks(pg.owner)}

    assigned = s.assign_task(pg.owner, operation(), queued["id"], pg.editor)
    assert (assigned["execution_mode"], assigned["gate_mode"], assigned["assignee"]) == ("manual", "reviewer", pg.editor.principal)
    assert assigned["reviewers"] == [pg.owner.principal]


def test_unassigned_submit_task_is_a_manual_task_with_the_manual_audit_chain(pg):
    s = pg.store
    task = s.submit_task(pg.owner, operation(), "open", "Synthetic open work", page_id=pg.page["id"], scopes=[{"kind": "path", "value": pg.prefix + "/open"}])
    assert (task["execution_mode"], task["gate_mode"], task["state"], task["assignee"]) == ("manual", "reviewer", "queued", None)
    assert task["reviewers"] == [pg.owner.principal] and task["respond_by"] is None and task["prompt"] == "Synthetic open work"
    assert task["id"] in ready_ids(pg, pg.editor) and task["id"] in ready_ids(pg, pg.reviewer)
    claim = s.claim(pg.editor, operation(), task["id"])
    assert claim["holder"] == pg.editor.principal
    s.comment(pg.editor, operation(), pg.page["id"], "Working on it", thread_id=task["id"])
    s.submit_result(pg.editor, operation(), task["id"], claim["epoch"], "Open result")
    expect("forbidden", lambda: s.gate(pg.editor, operation(), task["id"], claim["epoch"], "pass"))
    done = s.gate(pg.owner, operation(), task["id"], claim["epoch"], "pass")
    assert done["state"] == "done" and done["status"] == "completed"
    events = {row["event"] for row in s.list_audit(pg.owner, task_id=task["id"])}
    assert events >= {"task.submitted", "thread.submitted", "task.claimed", "comment.created", "task.result_submitted", "gate.reviewed",
                      "task.completed", "thread.completed"}
    assert not any(event.startswith("worker.") or event in {"task.dispatched", "task.interrupted"} for event in events)
    assert any(row["thread_id"] == task["id"] and row["body_md"] == "Open result" for row in s.fetch_inbox(pg.owner)["messages"])


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


def test_graph_blocks_claim_until_dependency_has_current_epoch_pass(pg, approver):
    s = pg.store
    approver(s, pg.reviewer, by=pg.owner)
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
    expect("not_found", lambda: s.plan(pg.editor, op, graph))


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


def test_graph_cannot_modify_any_previously_claimed_or_executed_task(pg, approver):
    s = pg.store
    approver(s, pg.reviewer, by=pg.owner)
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
    assigned = manual(pg, "assigned", [{"kind": "path", "value": pg.prefix + "/shared"}], assignee=pg.editor)
    other = manual(pg, "other", [{"kind": "path", "value": pg.prefix + "/shared/sub"}])
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
    expect("not_found", lambda: s.get_task(pg.editor, private_task["id"]))
    before = len(s.list_blocks(pg.owner, pg.page["id"]))
    for automatic in (False, True):
        op = operation()
        if automatic:
            call = lambda: s.submit_task(pg.editor, op, "invisible-dep", "Forbidden node", page_id=pg.page["id"], depends_on=[private_task["id"]])
        else:
            call = lambda: s.create_task_block(pg.editor, op, pg.page["id"], "Forbidden node", [], [pg.reviewer.principal], depends_on=[private_task["id"]])
        expect("not_found", call)
        with s._transaction() as conn:
            assert not conn.execute("SELECT 1 FROM baton.operations WHERE operation_id=%s", (op,)).fetchone()
    assert len(s.list_blocks(pg.owner, pg.page["id"])) == before


def test_creation_dependencies_are_atomic_and_keep_an_unassigned_task_unclaimable_until_the_gate_passes(pg, approver):
    s = pg.store
    approver(s, pg.reviewer, by=pg.owner)
    parent = manual(pg, "parent")
    child = s.submit_task(pg.owner, operation(), "open-dependent", "Synthetic dependent open task",
                         page_id=pg.page["id"], depends_on=[parent["id"]])
    assert child["depends_on"] == [parent["id"]] and child["assignee"] is None
    assert child["id"] not in ready_ids(pg, pg.editor)
    expect("dependency_blocked", lambda: s.claim(pg.editor, operation(), child["id"]))
    parent_claim = s.claim(pg.editor, operation(), parent["id"])
    s.submit_result(pg.editor, operation(), parent["id"], parent_claim["epoch"], "Candidate")
    expect("dependency_blocked", lambda: s.claim(pg.editor, operation(), child["id"]))
    s.gate(pg.reviewer, operation(), parent["id"], parent_claim["epoch"], "pass")
    assert child["id"] in ready_ids(pg, pg.editor)
    claimed = s.claim(pg.editor, operation(), child["id"])
    assert claimed["task_id"] == child["id"] and claimed["depends_on"] == [parent["id"]]


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
    claim = s.claim(pg.owner, operation(), parent["id"])
    s.submit_result(pg.owner, operation(), parent["id"], claim["epoch"], "Private dependency output")
    s.gate(pg.owner, operation(), parent["id"], claim["epoch"], "pass", self_approval=True)
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


def face(**changes):
    return {"environment": ["python 3.12"], "fixtures": ["seed.sql"], "constraints": ["no network"],
            "migration": None, "acceptance_command": "pytest -q", **changes}


def disjoint(pg, suffix):
    """Migration scopes conflict only on equal identifiers, so tasks can be held concurrently."""
    return [{"kind": "migration", "value": pg.prefix + "-" + suffix}]


def ready_ids(pg, actor):
    return {row["id"] for row in pg.store.ready(actor, pg.page["id"])}


def test_incomplete_work_face_blocks_claim_and_ready_until_owner_completes(pg, approver, tmp_path):
    s = pg.store
    approver(s, pg.reviewer, by=pg.owner)
    marker = tmp_path / "executed"
    task = manual(pg, "wf-gate", scopes=disjoint(pg, "gate"), assignee=pg.editor, require_work_face=True)
    assert task["require_work_face"] is True and task["task_kind"] == "construction" and task["work_face"] is None
    assert task["work_face_missing"] == ["base_commit", "work_face"]
    expect("work_face_incomplete", lambda: s.claim(pg.editor, operation(), task["id"]))
    assert task["id"] not in ready_ids(pg, pg.editor)
    completed = s.set_work_face(pg.owner, operation(), task["id"], face(acceptance_command=f"touch {marker}"), base_commit="base-1")
    assert completed["work_face_missing"] == [] and completed["base_commit"] == "base-1"
    assert completed["work_face"]["acceptance_command"] == f"touch {marker}" and completed["state"] == "queued"
    assert task["id"] in ready_ids(pg, pg.editor)
    claimed = s.claim(pg.editor, operation(), task["id"])
    assert claimed["state"] == "claimed" and claimed["work_face"] == completed["work_face"]
    s.submit_result(pg.editor, operation(), task["id"], claimed["epoch"], "Candidate")
    assert s.gate(pg.reviewer, operation(), task["id"], claimed["epoch"], "pass")["state"] == "done"
    assert not marker.exists()


def test_work_face_missing_names_each_absent_part_and_scopes_cannot_be_repaired(pg):
    s = pg.store
    only_face = manual(pg, "missing-base", scopes=disjoint(pg, "m1"), require_work_face=True, work_face=face())
    assert only_face["work_face_missing"] == ["base_commit"]
    only_base = manual(pg, "missing-face", scopes=disjoint(pg, "m2"), require_work_face=True, base_commit="base-1")
    assert only_base["work_face_missing"] == ["work_face"]
    no_scopes = manual(pg, "missing-scopes", scopes=[], require_work_face=True, base_commit="base-1", work_face=face())
    assert no_scopes["work_face_missing"] == ["scopes"]
    nothing = manual(pg, "missing-all", scopes=[], require_work_face=True)
    assert nothing["work_face_missing"] == ["base_commit", "scopes", "work_face"]
    still = s.set_work_face(pg.owner, operation(), nothing["id"], face(), base_commit="base-1")
    assert still["work_face_missing"] == ["scopes"]
    expect("work_face_incomplete", lambda: s.claim(pg.owner, operation(), still["id"]))
    assert not {only_face["id"], only_base["id"], no_scopes["id"], nothing["id"]} & ready_ids(pg, pg.owner)
    assert s.set_work_face(pg.owner, operation(), only_face["id"], face(), base_commit="base-1")["work_face_missing"] == []


def test_claim_replay_rechecks_work_face(pg):
    s = pg.store
    task = manual(pg, "wf-replay", scopes=disjoint(pg, "replay"), assignee=pg.editor, require_work_face=True,
                  base_commit="base-1", work_face=face())
    claim_op = operation()
    assert s.claim(pg.editor, claim_op, task["id"])["state"] == "claimed"
    assert s.claim(pg.editor, claim_op, task["id"])["replay"]
    with s._transaction(write=True) as conn:
        conn.execute("UPDATE baton.tasks SET work_face=NULL WHERE id=%s", (task["id"],))
    expect("work_face_incomplete", lambda: s.claim(pg.editor, claim_op, task["id"]))


def test_prep_task_gates_construction_through_graph_and_holder_sets_work_face(pg, approver):
    s = pg.store
    approver(s, pg.reviewer, by=pg.owner)
    prep = manual(pg, "prep", scopes=disjoint(pg, "prep"), assignee=pg.editor, task_kind="prep")
    build = manual(pg, "build", scopes=disjoint(pg, "build"), assignee=pg.editor, depends_on=[prep["id"]], require_work_face=True)
    later = manual(pg, "later", scopes=disjoint(pg, "later"), assignee=pg.editor, depends_on=[prep["id"]], require_work_face=True)
    assert prep["task_kind"] == "prep" and prep["require_work_face"] is False and build["depends_on"] == [prep["id"]]
    expect("forbidden", lambda: s.set_work_face(pg.editor, operation(), build["id"], face(), "prep-base"))
    prep_claim = s.claim(pg.editor, operation(), prep["id"])
    done = s.set_work_face(pg.editor, operation(), build["id"], face(), base_commit="prep-base")
    assert done["work_face_missing"] == [] and done["base_commit"] == "prep-base" and done["work_face"] == face()
    expect("dependency_blocked", lambda: s.claim(pg.editor, operation(), build["id"]))
    s.submit_result(pg.editor, operation(), prep["id"], prep_claim["epoch"], "Site prepared")
    expect("forbidden", lambda: s.set_work_face(pg.editor, operation(), later["id"], face(), "late-base"))
    expect("dependency_blocked", lambda: s.claim(pg.editor, operation(), build["id"]))
    assert build["id"] not in ready_ids(pg, pg.editor)
    s.gate(pg.reviewer, operation(), prep["id"], prep_claim["epoch"], "pass")
    assert build["id"] in ready_ids(pg, pg.editor) and later["id"] not in ready_ids(pg, pg.editor)
    expect("work_face_incomplete", lambda: s.claim(pg.editor, operation(), later["id"]))
    claimed = s.claim(pg.editor, operation(), build["id"])
    assert claimed["state"] == "claimed" and claimed["work_face"] == face()


def test_set_work_face_requires_edit_and_owner_reviewer_or_live_prep_holder(pg):
    s = pg.store
    task = manual(pg, "wf-auth", scopes=disjoint(pg, "auth"), require_work_face=True)
    def attempt(actor):
        return lambda: s.set_work_face(actor, operation(), task["id"], face())
    expect("not_found", attempt(pg.reader))
    s.share(pg.owner, operation(), "space", pg.space["id"], "person", pg.reader.account, "view")
    expect("forbidden", attempt(pg.reader))
    s.share(pg.owner, operation(), "space", pg.space["id"], "person", pg.reviewer.account, "view")
    expect("forbidden", attempt(pg.reviewer))
    s.share(pg.owner, operation(), "space", pg.space["id"], "person", pg.reviewer.account, "edit")
    owner_twin = Actor(pg.prefix + "-owner-twin", pg.owner.account, "human")
    reviewer_twin = Actor(pg.prefix + "-reviewer-twin", pg.reviewer.account, "human")
    assert s.effective_role(owner_twin, "page", pg.page["id"])["role"] == "edit"
    expect("forbidden", attempt(owner_twin))
    expect("forbidden", attempt(reviewer_twin))
    expect("forbidden", attempt(pg.editor))
    expect("forbidden", lambda: s.set_work_face(SERVICE_ACTOR, operation(), task["id"], face()))

    other = manual(pg, "wf-other", scopes=disjoint(pg, "other"), assignee=pg.editor)
    s.claim(pg.editor, operation(), other["id"])
    expect("forbidden", attempt(pg.editor))
    s.plan(pg.owner, operation(), [{"task_id": task["id"], "depends_on": [other["id"]]}])
    expect("forbidden", attempt(pg.editor))
    expire(pg, other["id"])

    unrelated = manual(pg, "wf-unrelated-prep", scopes=disjoint(pg, "unrelated"), assignee=pg.editor, task_kind="prep")
    s.claim(pg.editor, operation(), unrelated["id"])
    expect("forbidden", attempt(pg.editor))
    prep = manual(pg, "wf-related-prep", scopes=disjoint(pg, "related"), assignee=pg.editor, task_kind="prep")
    s.plan(pg.owner, operation(), [{"task_id": task["id"], "depends_on": [prep["id"]]}])
    s.claim(pg.editor, operation(), prep["id"])
    expire(pg, prep["id"])
    expect("forbidden", attempt(pg.editor))
    assert not [row for row in s.list_audit(pg.owner, task_id=task["id"]) if row["event"] == "task.work_face_set"]

    s.claim(pg.editor, operation(), prep["id"])
    assert s.set_work_face(pg.editor, operation(), task["id"], face())["work_face"] == face()
    assert s.set_work_face(pg.reviewer, operation(), task["id"], face(constraints=["reviewer edit"]))["work_face"]["constraints"] == ["reviewer edit"]
    assert s.set_work_face(pg.owner, operation(), task["id"], face(constraints=["owner edit"]))["work_face"]["constraints"] == ["owner edit"]
    events = [row for row in s.list_audit(pg.owner, task_id=task["id"]) if row["event"] == "task.work_face_set"]
    assert [row["performed_by"] for row in events] == [pg.editor.principal, pg.reviewer.principal, pg.owner.principal]


def test_work_face_is_frozen_once_started_and_base_commit_is_set_once(pg, approver):
    s = pg.store
    approver(s, pg.reviewer, by=pg.owner)
    task = manual(pg, "wf-frozen", scopes=disjoint(pg, "frozen"), assignee=pg.editor, require_work_face=True)
    s.set_work_face(pg.owner, operation(), task["id"], face(), base_commit="base-1")
    expect("operation_conflict", lambda: s.set_work_face(pg.owner, operation(), task["id"], face(constraints=["changed"]), base_commit="base-2"))
    unchanged = s.get_task(pg.owner, task["id"])
    assert unchanged["base_commit"] == "base-1" and unchanged["work_face"] == face()
    updated = s.set_work_face(pg.owner, operation(), task["id"], face(constraints=["changed"]), base_commit="base-1")
    assert updated["base_commit"] == "base-1" and updated["work_face"]["constraints"] == ["changed"]
    assert s.set_work_face(pg.owner, operation(), task["id"], face(), base_commit=None)["base_commit"] == "base-1"

    created = manual(pg, "wf-created-base", scopes=disjoint(pg, "created"), require_work_face=True, base_commit="created-base")
    expect("operation_conflict", lambda: s.set_work_face(pg.owner, operation(), created["id"], face(), base_commit="other-base"))
    assert s.set_work_face(pg.owner, operation(), created["id"], face(), base_commit="created-base")["work_face_missing"] == []

    claim = s.claim(pg.editor, operation(), task["id"])
    expect("stale_claim", lambda: s.set_work_face(pg.owner, operation(), task["id"], face(), base_commit="base-1"))
    s.submit_result(pg.editor, operation(), task["id"], claim["epoch"], "Candidate")
    expect("stale_claim", lambda: s.set_work_face(pg.owner, operation(), task["id"], face()))
    assert s.gate(pg.reviewer, operation(), task["id"], claim["epoch"], "fail")["state"] == "queued"
    expect("stale_claim", lambda: s.set_work_face(pg.owner, operation(), task["id"], face()))
    assert s.get_task(pg.owner, task["id"])["work_face"] == face()


def test_set_work_face_idempotency_and_creation_digests(pg):
    s = pg.store
    task = manual(pg, "wf-idempotent", scopes=disjoint(pg, "idem"), require_work_face=True)
    op = operation()
    first = s.set_work_face(pg.owner, op, task["id"], face(), base_commit="idem-base")
    replay = s.set_work_face(pg.owner, op, task["id"], face(environment=["  python 3.12  "]), base_commit="idem-base")
    assert first["replay"] is False and replay["replay"] is True and replay["work_face"] == first["work_face"]
    expect("operation_conflict", lambda: s.set_work_face(pg.owner, op, task["id"], face(fixtures=["other.sql"]), base_commit="idem-base"))
    expect("operation_conflict", lambda: s.set_work_face(pg.owner, op, task["id"], face(), base_commit="other-base"))
    expect("operation_conflict", lambda: s.set_work_face(pg.owner, op, task["id"], face()))
    assert len([row for row in s.list_audit(pg.owner, task_id=task["id"]) if row["event"] == "task.work_face_set"]) == 1
    s.share(pg.owner, operation(), "space", pg.space["id"], "person", pg.owner.account, None)
    assert s.set_work_face(pg.owner, op, task["id"], face(), base_commit="idem-base")["replay"]

    scopes, reviewers = disjoint(pg, "digest"), [pg.reviewer.principal]
    def create(op, **kwargs):
        return s.create_task_block(pg.owner, op, pg.page["id"], "Digest task", scopes, reviewers, **kwargs)
    op = operation()
    created = create(op, work_face=face(), require_work_face=True, task_kind="prep")
    assert create(op, work_face=face(), require_work_face=True, task_kind="prep")["replay"]
    for changed in ({"work_face": None, "require_work_face": True, "task_kind": "prep"},
                    {"work_face": face(migration="0042"), "require_work_face": True, "task_kind": "prep"},
                    {"work_face": face(), "require_work_face": False, "task_kind": "prep"},
                    {"work_face": face(), "require_work_face": True, "task_kind": "construction"}):
        expect("operation_conflict", lambda: create(op, **changed))
    assert created["task_kind"] == "prep" and created["require_work_face"] is True

    submit = lambda op, key, **kwargs: s.submit_task(pg.owner, op, key, "Digest submit", page_id=pg.page["id"], scopes=disjoint(pg, "digest2"), **kwargs)
    op = operation()
    submitted = submit(op, "wf-digest", work_face=face(), require_work_face=True, task_kind="prep")
    assert submit(op, "wf-digest", work_face=face(), require_work_face=True, task_kind="prep")["replay"]
    assert submit(operation(), "wf-digest", work_face=face(), require_work_face=True, task_kind="prep")["id"] == submitted["id"]
    for changed in ({"require_work_face": True, "task_kind": "prep"}, {"work_face": face(), "task_kind": "prep"},
                    {"work_face": face(), "require_work_face": True}):
        expect("operation_conflict", lambda: submit(op, "wf-digest", **changed))
        expect("operation_conflict", lambda: submit(operation(), "wf-digest", **changed))


INVALID_FACES = {
    "extra key": {**face(), "owner": "someone"},
    "missing environment": {key: value for key, value in face().items() if key != "environment"},
    "missing fixtures": {key: value for key, value in face().items() if key != "fixtures"},
    "missing constraints": {key: value for key, value in face().items() if key != "constraints"},
    "missing migration": {key: value for key, value in face().items() if key != "migration"},
    "missing command": {key: value for key, value in face().items() if key != "acceptance_command"},
    "empty object": {},
    "list is a string": face(environment="python"),
    "list is null": face(constraints=None),
    "item is integer": face(fixtures=[1]),
    "item is null": face(fixtures=[None]),
    "item is boolean": face(constraints=[True]),
    "item is nested list": face(environment=[["python"]]),
    "item is empty": face(environment=[""]),
    "item is blank": face(environment=["  \t "]),
    "item is too long": face(fixtures=["x" * 501]),
    "too many items": face(constraints=["rule"] * 21),
    "item has NUL": face(environment=["a\x00b"]),
    "migration is integer": face(migration=42),
    "migration is empty": face(migration=""),
    "migration is blank": face(migration="   "),
    "migration is too long": face(migration="m" * 201),
    "command is null": face(acceptance_command=None),
    "command is integer": face(acceptance_command=7),
    "command is empty": face(acceptance_command=""),
    "command is blank": face(acceptance_command=" \n "),
    "command is too long": face(acceptance_command="c" * 2001),
    "command has NUL": face(acceptance_command="pytest\x00"),
}


@pytest.mark.parametrize("work_face", INVALID_FACES.values(), ids=INVALID_FACES.keys())
def test_work_face_validation_rejects_malformed_input_before_storage(work_face):
    store = SpaceStore("/synthetic/nonexistent-config.json")
    actor = Actor("synthetic-principal", "synthetic-account", "human")
    expect("invalid", lambda: normalize_work_face(work_face))
    expect("invalid", lambda: store.set_work_face(actor, operation(), operation(), work_face))
    expect("invalid", lambda: store.create_task_block(actor, operation(), operation(), "Prompt", [], ["reviewer"], work_face=work_face))
    expect("invalid", lambda: store.submit_task(actor, operation(), "synthetic-key", "Prompt", work_face=work_face))


@pytest.mark.parametrize("work_face", [None, [], "text", 7, [face()]], ids=["null", "list", "text", "integer", "list of faces"])
def test_set_work_face_requires_an_object(work_face):
    store = SpaceStore("/synthetic/nonexistent-config.json")
    actor = Actor("synthetic-principal", "synthetic-account", "human")
    expect("invalid", lambda: store.set_work_face(actor, operation(), operation(), work_face))
    expect("invalid", lambda: store.set_work_face(actor, operation(), operation(), face(), base_commit="  "))


def test_work_face_limits_are_inclusive_and_values_are_trimmed():
    limit = face(environment=["e" * 500] * 20, fixtures=[" f "], constraints=[], migration="m" * 200, acceptance_command="  " + "c" * 2000 + "\n")
    assert normalize_work_face(limit) == {"environment": ["e" * 500] * 20, "fixtures": ["f"], "constraints": [],
                                          "migration": "m" * 200, "acceptance_command": "c" * 2000}
    assert normalize_work_face(face(migration="  0042  "))["migration"] == "0042"
    assert normalize_work_face(face(acceptance_command="make\ncheck"))["acceptance_command"] == "make\ncheck"


def test_invalid_creation_inputs_leave_no_task(pg):
    s = pg.store
    before = len(s.list_tasks(pg.owner))
    expect("invalid", lambda: manual(pg, "bad-face", work_face=face(extra=1)))
    expect("invalid", lambda: manual(pg, "bad-flag", require_work_face="true"))
    expect("invalid", lambda: manual(pg, "bad-flag-int", require_work_face=1))
    expect("invalid", lambda: manual(pg, "bad-kind", task_kind="ops"))
    expect("invalid", lambda: manual(pg, "null-kind", task_kind=None))
    expect("invalid", lambda: s.submit_task(pg.owner, operation(), "bad-submit-kind", "Prompt", page_id=pg.page["id"], task_kind="Prep"))
    assert len(s.list_tasks(pg.owner)) == before


def test_work_face_audit_has_key_names_and_digest_only(pg):
    s = pg.store
    marker = "synthetic-secret-acceptance-body"
    checklist = face(acceptance_command="run " + marker, fixtures=[marker + "-fixture"], migration="0042")
    task = manual(pg, "wf-audit", scopes=disjoint(pg, "audit"), require_work_face=True)
    op = operation()
    s.set_work_face(pg.owner, op, task["id"], checklist, base_commit="audit-base")
    s.set_work_face(pg.owner, op, task["id"], checklist, base_commit="audit-base")
    rows = [row for row in s.list_audit(pg.owner, task_id=task["id"]) if row["event"] == "task.work_face_set"]
    assert len(rows) == 1
    canonical = json.dumps(checklist, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    assert rows[0]["metadata"] == {"work_face_keys": sorted(checklist), "work_face_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
                                   "thread_id": task["id"]}
    assert rows[0]["performed_by"] == pg.owner.principal and rows[0]["task_id"] == task["id"]
    assert marker not in json.dumps(s.list_audit(pg.owner, task_id=task["id"]), default=str)
    assert marker not in json.dumps(s.list_audit(pg.owner, space_id=pg.space["id"]), default=str)


def test_tasks_without_work_face_requirement_behave_as_before(pg):
    s = pg.store
    plain = manual(pg, "plain", scopes=disjoint(pg, "plain1"), assignee=pg.editor)
    decorated = manual(pg, "plain-face", scopes=disjoint(pg, "plain2"), assignee=pg.editor, work_face=face())
    for task in (plain, decorated):
        assert task["require_work_face"] is False and task["task_kind"] == "construction" and task["work_face_missing"] == []
        assert task["base_commit"] is None
    assert plain["work_face"] is None and decorated["work_face"] == face()
    assert {plain["id"], decorated["id"]} <= ready_ids(pg, pg.editor)
    assert s.claim(pg.editor, operation(), plain["id"])["state"] == "claimed"
    assert s.claim(pg.editor, operation(), decorated["id"])["state"] == "claimed"
    open_task = s.submit_task(pg.owner, operation(), "plain-open", "Plain unassigned work", page_id=pg.page["id"], scopes=disjoint(pg, "plain3"))
    assert open_task["work_face_missing"] == [] and open_task["task_kind"] == "construction"
    assert s.claim(pg.editor, operation(), open_task["id"])["task_id"] == open_task["id"]
    with s._transaction() as conn:
        columns = {row["column_name"]: row for row in conn.execute(
            "SELECT column_name,column_default,is_nullable FROM information_schema.columns WHERE table_schema='baton' AND table_name='tasks'")}
    assert columns["require_work_face"]["column_default"] == "false" and columns["require_work_face"]["is_nullable"] == "NO"
    assert columns["task_kind"]["column_default"].startswith("'construction'") and columns["task_kind"]["is_nullable"] == "NO"
    assert columns["work_face"]["is_nullable"] == "YES"
    with s._transaction(write=True) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            with conn.transaction():
                conn.execute("UPDATE baton.tasks SET task_kind='other' WHERE id=%s", (plain["id"],))


# --- explicit "none" scope -------------------------------------------------------------------

NONE = [{"kind": "none"}]
ROOT = [{"kind": "path", "value": ""}]


def test_none_scope_normalization_and_validity():
    assert normalize_scopes(NONE) == NONE
    assert normalize_scopes(NONE + NONE) == NONE
    assert normalize_scopes(None) == ROOT and normalize_scopes([]) == ROOT
    for invalid in (
        NONE + [{"kind": "path", "value": "src"}],
        [{"kind": "migration", "value": "0001"}, {"kind": "none"}],
        NONE + [{"kind": "module", "value": "core"}],
        NONE + ROOT,
        [{"kind": "none", "value": ""}],
        [{"kind": "none", "value": "x"}],
        [{"kind": "none", "extra": 1}],
        [{"kind": ["none"]}],
        [{"kind": "None"}],
        [{}],
    ):
        expect("invalid", lambda invalid=invalid: normalize_scopes(invalid))


def test_none_scope_never_overlaps_anything():
    from bridge.space_store import scopes_overlap
    others = (NONE, ROOT, [{"kind": "path", "value": "src/a"}], [{"kind": "module", "value": "core"}],
              [{"kind": "migration", "value": "7"}])
    for other in others:
        assert not scopes_overlap(NONE, other) and not scopes_overlap(other, NONE)
    assert scopes_overlap(ROOT, ROOT) and scopes_overlap([{"kind": "module", "value": "a"}], ROOT)


def path(value):
    return [{"kind": "path", "value": value}]


def overlap(left, right):
    from bridge.space_store import scopes_overlap
    left, right = normalize_scopes(left), normalize_scopes(right)
    result = scopes_overlap(left, right)
    assert scopes_overlap(right, left) == result, "overlap must be symmetric"
    return result


@pytest.mark.parametrize("left,right,expected", [
    ("docs/design", "docs/design", True),
    ("docs/design", "docs/design/roadmap.md", True),
    ("docs/design/roadmap.md", "docs/design", True),
    ("docs/design", "docs", True),
    ("docs", "docs/design/roadmap.md", True),
    ("docs/design", "docs/designer", False),
    ("docs/designer", "docs/design", False),
    ("docs/design", "src", False),
    ("src/a", "docs/b", False),
    ("src/a", "src/ab", False),
    ("src/a", "src/a/b/c.py", True),
    ("src/a.py", "src/a", False),
])
def test_path_scopes_overlap_only_on_directory_prefix(left, right, expected):
    assert overlap(path(left), path(right)) is expected


@pytest.mark.parametrize("left,right", [
    ("src/a/", "src/a"),
    ("src/a/", "src/a/b"),
    ("./src/a", "src/a"),
    ("src/./a", "src/a/b"),
    ("src//a", "src/a"),
    ("src//a///b", "src/a/b"),
    ("./", ""),
    (".", "src"),
])
def test_path_scope_boundaries_are_normalized_before_comparison(left, right):
    assert overlap(path(left), path(right))
    assert not overlap(path("src/a/"), path("src/ab/"))
    assert not overlap(path("src//a"), path("docs/./a"))
    assert normalize_scopes(path("src//./a/")) == path("src/a")


def test_root_placeholder_overlaps_every_path_and_module_but_not_migration_or_none():
    for value in ("", "src", "src/a/b.py", "docs/designer"):
        assert overlap(ROOT, path(value))
    assert overlap(normalize_scopes(None), path("src/a"))
    assert overlap(normalize_scopes([]), path("docs/b"))
    assert overlap(ROOT, [{"kind": "module", "value": "core"}])
    assert not overlap(ROOT, [{"kind": "migration", "value": "7"}])
    assert not overlap(ROOT, NONE)


def test_module_covers_the_whole_repository_while_migration_stays_exact():
    module = [{"kind": "module", "value": "core"}]
    for other in (path("src/a"), path(""), module, [{"kind": "module", "value": "other"}], [{"kind": "migration", "value": "7"}]):
        assert overlap(module, other)
    assert not overlap(module, NONE)
    assert overlap([{"kind": "migration", "value": "7"}], [{"kind": "migration", "value": "007"}])
    assert not overlap([{"kind": "migration", "value": "7"}], [{"kind": "migration", "value": "8"}])
    assert not overlap([{"kind": "migration", "value": "7"}], path("src/a"))


def test_none_never_overlaps_a_path_scope():
    for value in ("", "src", "docs/b"):
        assert not overlap(NONE, path(value))
    assert not overlap(NONE, NONE)


def test_multi_scope_declarations_overlap_when_any_pair_overlaps():
    left = normalize_scopes([{"kind": "path", "value": "src/a"}, {"kind": "migration", "value": "9"}])
    assert overlap(left, [{"kind": "migration", "value": "9"}])
    assert overlap(left, path("src/a/x"))
    assert not overlap(left, path("src/b") + [{"kind": "migration", "value": "10"}])


def test_path_case_is_preserved_and_compared_case_sensitively_by_default(monkeypatch):
    monkeypatch.delenv("BATON_SCOPE_CASE_INSENSITIVE", raising=False)
    assert normalize_scopes(path("Src/A")) == path("Src/A")
    assert not overlap(path("Src/A"), path("src/a"))
    assert not overlap(path("Src"), path("src/a/b"))
    for value in ("", "0", "true", "yes"):
        monkeypatch.setenv("BATON_SCOPE_CASE_INSENSITIVE", value)
        assert not overlap(path("Src/A"), path("src/a"))


def test_case_insensitive_option_folds_path_comparison(monkeypatch):
    monkeypatch.setenv("BATON_SCOPE_CASE_INSENSITIVE", "1")
    assert overlap(path("Src/A"), path("src/a"))
    assert overlap(path("SRC"), path("src/a/b"))
    assert overlap(path("src/a/b"), path("Src/A"))
    assert not overlap(path("Src/A"), path("src/ab"))
    assert not overlap(path("Docs/B"), path("src/a"))
    assert normalize_scopes(path("Src/A")) == path("Src/A"), "stored declarations are not rewritten by the option"


def test_aliases_through_different_paths_are_not_detected_as_overlapping():
    assert not overlap(path("current/app.py"), path("releases/42/app.py"))
    assert not overlap(path("link-to-src"), path("src/a"))


def test_policy_and_request_digests_stay_case_stable():
    from bridge.space_store import _policy_matches, _request_scopes
    assert _policy_matches("path", "docs", path("Docs/Design"))
    assert _policy_matches("path", "Docs", path("docs/design"))
    assert not _policy_matches("path", "docs", path("Documents"))
    assert _request_scopes(normalize_scopes([{"kind": "path", "value": "Src/A"}, {"kind": "path", "value": "src/a"}])) == path("src/a")


def test_none_with_other_scopes_is_rejected_by_the_store_entry_points(pg):
    s = pg.store
    mixed = NONE + [{"kind": "path", "value": pg.prefix + "/x"}]
    expect("invalid", lambda: s.create_task_block(pg.owner, operation(), pg.page["id"], "Mixed", mixed, [pg.reviewer.principal]))
    expect("invalid", lambda: s.submit_task(pg.owner, operation(), "mixed-none", "Mixed", page_id=pg.page["id"], scopes=mixed, assignee=pg.editor))


def test_manual_tasks_without_scopes_default_to_none_and_are_claimed_together(pg):
    s = pg.store
    block = s.create_task_block(pg.owner, operation(), pg.page["id"], "Review without footprint", None, [pg.reviewer.principal])
    submitted = s.submit_task(pg.owner, operation(), "no-scopes-manual", "Doc task", page_id=pg.page["id"], assignee=pg.editor)
    explicit = s.create_task_block(pg.owner, operation(), pg.page["id"], "Explicit none", NONE, [pg.reviewer.principal])
    for task in (block, submitted, explicit):
        assert s.get_scopes(pg.owner, task["id"])["scopes"] == NONE
        assert task["scopes"] == NONE
    assert {block["id"], submitted["id"], explicit["id"]} <= ready_ids(pg, pg.owner) | ready_ids(pg, pg.editor)
    assert s.claim(pg.owner, operation(), block["id"])["state"] == "claimed"
    assert s.claim(pg.editor, operation(), submitted["id"])["state"] == "claimed"
    assert s.claim(pg.reviewer, operation(), explicit["id"])["state"] == "claimed"


def test_none_claims_neither_block_nor_are_blocked_by_other_scopes(pg):
    s = pg.store
    none_task = manual(pg, "none-a", NONE)
    path_task = manual(pg, "none-path", [{"kind": "path", "value": pg.prefix + "/p"}])
    module_task = manual(pg, "none-module", [{"kind": "module", "value": "unknown"}])
    root_task = manual(pg, "none-root", [])
    s.claim(pg.owner, operation(), path_task["id"])
    assert s.claim(pg.owner, operation(), none_task["id"])["state"] == "claimed"
    expect("scope_conflict", lambda: s.claim(pg.owner, operation(), module_task["id"]))
    expect("scope_conflict", lambda: s.claim(pg.owner, operation(), root_task["id"]))
    later = manual(pg, "none-b", NONE)
    assert later["id"] in ready_ids(pg, pg.owner)
    assert s.claim(pg.owner, operation(), later["id"])["state"] == "claimed"
    expire(pg, path_task["id"])
    s.claim(pg.owner, operation(), module_task["id"])
    assert manual(pg, "none-c", NONE)["id"] in ready_ids(pg, pg.owner)
    assert s.claim(pg.owner, operation(), manual(pg, "none-d", NONE)["id"])["state"] == "claimed"


def test_unassigned_tasks_without_scopes_default_to_none_and_are_claimed_together(pg):
    s = pg.store
    first = s.submit_task(pg.owner, operation(), "open-none-1", "Unassigned one", page_id=pg.page["id"])
    second = s.submit_task(pg.owner, operation(), "open-none-2", "Unassigned two", page_id=pg.page["id"])
    for task in (first, second):
        assert s.get_scopes(pg.owner, task["id"])["scopes"] == NONE and task["scopes"] == NONE
    assert s.claim(pg.editor, operation(), first["id"])["state"] == "claimed"
    assert s.claim(pg.owner, operation(), second["id"])["state"] == "claimed"
    unrelated = manual(pg, "open-none-path", [{"kind": "path", "value": pg.prefix + "/other"}])
    assert s.claim(pg.owner, operation(), unrelated["id"])["state"] == "claimed"


def test_work_face_completeness_treats_none_as_undeclared(pg):
    s = pg.store
    task = s.create_task_block(pg.owner, operation(), pg.page["id"], "No scopes, checklist required", None, [pg.reviewer.principal],
                               require_work_face=True, base_commit="base-1", work_face=face())
    assert task["scopes"] == NONE and task["work_face_missing"] == ["scopes"]
    explicit = manual(pg, "none-face", NONE, require_work_face=True)
    assert explicit["work_face_missing"] == ["base_commit", "scopes", "work_face"]
    done = s.set_work_face(pg.owner, operation(), explicit["id"], face(), base_commit="base-1")
    assert done["work_face_missing"] == ["scopes"]
    expect("work_face_incomplete", lambda: s.claim(pg.owner, operation(), task["id"]))
    assert not {task["id"], explicit["id"]} & ready_ids(pg, pg.owner)
    plain = manual(pg, "none-no-face", NONE)
    assert plain["work_face_missing"] == [] and plain["id"] in ready_ids(pg, pg.owner)


def test_omitted_scopes_keep_their_legacy_digests(pg):
    s = pg.store
    actor_fields = {"principal": pg.owner.principal, "account": pg.owner.account, "kind": pg.owner.kind}
    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()).hexdigest()
    create_op = operation()
    block = s.create_task_block(pg.owner, create_op, pg.page["id"], "Legacy omitted", None, [pg.reviewer.principal])
    legacy_create = {"page_id": pg.page["id"], "prompt": "Legacy omitted", "scopes": ROOT, "reviewers": [pg.reviewer.principal],
                     "base_commit": None, "context": None, "evidence": None, "assignee": None}
    submit_op = operation()
    submitted = s.submit_task(pg.owner, submit_op, "legacy-omitted", "Legacy omitted submit", page_id=pg.page["id"], assignee=pg.editor)
    legacy_submit = {"idempotency_key": "legacy-omitted", "prompt": "Legacy omitted submit", "page_id": pg.page["id"], "scopes": ROOT,
                     "assignee": {"principal": pg.editor.principal, "account": pg.editor.account, "kind": pg.editor.kind}}
    auto_op = operation()
    auto = s.submit_task(pg.owner, auto_op, "legacy-omitted-auto", "Legacy omitted auto", page_id=pg.page["id"])
    assert auto["scopes"] == NONE
    legacy_auto = {"idempotency_key": "legacy-omitted-auto", "prompt": "Legacy omitted auto", "page_id": pg.page["id"], "scopes": ROOT, "assignee": None}
    with s._transaction() as conn:
        for task, operation_name, op, args in ((block, "create_task_block", create_op, legacy_create),
                                               (submitted, "submit_task", submit_op, legacy_submit),
                                               (auto, "submit_task", auto_op, legacy_auto)):
            assert conn.execute("SELECT input_hash FROM baton.tasks WHERE id=%s", (task["id"],)).fetchone()["input_hash"] == digest(args)
            assert conn.execute("SELECT request_hash FROM baton.operations WHERE principal=%s AND operation_id=%s",
                                (pg.owner.principal, op)).fetchone()["request_hash"] == digest([actor_fields, operation_name, args])
    assert s.create_task_block(pg.owner, create_op, pg.page["id"], "Legacy omitted", None, [pg.reviewer.principal])["replay"]
    assert s.submit_task(pg.owner, operation(), "legacy-omitted", "Legacy omitted submit", page_id=pg.page["id"], assignee=pg.editor)["id"] == submitted["id"]
    expect("operation_conflict", lambda: s.submit_task(pg.owner, operation(), "legacy-omitted", "Legacy omitted submit", page_id=pg.page["id"],
                                                       scopes=NONE, assignee=pg.editor))
    legacy_row = s.create_task_block(pg.owner, operation(), pg.page["id"], "Stored root", [], [pg.reviewer.principal])
    assert s.get_scopes(pg.owner, legacy_row["id"])["scopes"] == ROOT


# R1 approver gate (DECISIONS 287/291): the space owner is the root approver; everyone else approves only under a
# delegation that flows down from the root; nobody but the root approves their own work; policy can require two parties.

def principal(pg, name, kind="agent"):
    return Actor(pg.prefix + "-" + name, pg.prefix + "-" + name + "-account", kind)


def awaiting(pg, suffix, reviewers, space=None, page=None, executor=None, **task_kwargs):
    """A submitted candidate for a task whose configured reviewers are the given principals, none of them delegated yet."""
    s = pg.store
    space = space or pg.space
    page = page or pg.page
    executor = executor or pg.editor
    creator = task_kwargs.pop("creator", pg.owner)
    for actor in (*reviewers, executor):
        s.share(pg.owner, operation(), "space", space["id"], "person", actor.account, "edit")
    task = s.create_task_block(creator, operation(), page["id"], "Synthetic work " + suffix,
                               [{"kind": "path", "value": pg.prefix + "/" + suffix}], [actor.principal for actor in reviewers], **task_kwargs)
    claim = s.claim(executor, operation(), task["id"])
    s.submit_result(executor, operation(), task["id"], claim["epoch"], "Candidate " + suffix)
    return task, claim


def grant(pg, delegate_to, by=None, **kwargs):
    return pg.store.delegation_create(by or pg.owner, operation(), delegate_to, reason=kwargs.pop("reason", "DECISIONS 291"), **kwargs)


def raw_delegation(pg, delegate_to, parent_id=None, grantor=None, created_hours=-2, expires_hours=-1, may_delegate=False, space_id=None):
    """Insert a row directly with the runtime role's INSERT right, for states the API will not create."""
    grantor = grantor or pg.owner
    identity = uuid.uuid4()
    with pg.store._transaction(write=True) as conn:
        conn.execute("""INSERT INTO baton.x_delegation(id,grantor,grantor_account,grantor_kind,delegate,delegate_account,delegate_kind,
            scope,space_id,may_delegate,parent_id,reason,created_via,created_by,created_at,expires_at) VALUES (%s,%s,%s,%s,%s,%s,%s,'approve',%s,%s,%s,
            'DECISIONS 291','api',%s,clock_timestamp()+make_interval(hours=>%s),
            CASE WHEN %s::int IS NULL THEN NULL ELSE clock_timestamp()+make_interval(hours=>%s::int) END)""",
                     (identity, grantor.principal, grantor.account, grantor.kind, delegate_to.principal, delegate_to.account, delegate_to.kind,
                      space_id, may_delegate, parent_id, grantor.principal, created_hours, expires_hours, expires_hours))
    return str(identity)


def gate_rows(pg, task_id):
    with pg.store._transaction() as conn:
        return conn.execute("SELECT * FROM baton.x_gate WHERE task_id=%s ORDER BY created_at", (task_id,)).fetchall()


def reviewed_events(pg, task_id):
    return [row for row in pg.store.list_audit(pg.owner, task_id=task_id) if row["event"] == "gate.reviewed"]


def test_the_space_owner_is_root_and_approves_without_a_delegation(pg):
    s = pg.store
    task, claim = awaiting(pg, "root", [pg.reviewer])
    assert s.delegation_list(pg.owner) == []
    done = s.gate(pg.owner, operation(), task["id"], claim["epoch"], "pass")
    assert done["state"] == "done" and done["approvals_required"] == 1 and done["approvals_passed"] == 1
    row = gate_rows(pg, task["id"])[0]
    assert (row["root"], row["self_approval"], row["delegation_id"], row["grantor"]) == (True, False, None, None)
    event = reviewed_events(pg, task["id"])[0]
    assert event["metadata"]["root"] is True and event["metadata"]["self_approval"] is False and event["metadata"]["chain"] == []
    # An account that owns a different space has no authority over this one.
    stranger_space = s.create_space(pg.reader, operation(), pg.prefix + "-stranger")
    assert stranger_space["owner_account"] == pg.reader.account
    s.share(pg.owner, operation(), "space", pg.space["id"], "person", pg.reader.account, "edit")
    second, second_claim = awaiting(pg, "root-two", [pg.reader])
    expect("delegation_required", lambda: s.gate(pg.reader, operation(), second["id"], second_claim["epoch"], "pass"))


def test_non_root_without_a_covering_delegation_is_refused_humans_included(pg):
    s = pg.store
    judge = principal(pg, "judge")
    task, claim = awaiting(pg, "nodel", [pg.reviewer, judge])
    for reviewer in (pg.reviewer, judge):
        expect("delegation_required", lambda reviewer=reviewer: s.gate(reviewer, operation(), task["id"], claim["epoch"], "pass"))
        expect("delegation_required", lambda reviewer=reviewer: s.gate(reviewer, operation(), task["id"], claim["epoch"], "fail"))
    assert s.get_task(pg.owner, task["id"])["state"] == "awaiting_gate"
    outsider = principal(pg, "outsider", "human")
    s.share(pg.owner, operation(), "space", pg.space["id"], "person", outsider.account, "edit")
    grant(pg, outsider)
    expect("forbidden", lambda: s.gate(outsider, operation(), task["id"], claim["epoch"], "pass"))
    grant(pg, pg.reviewer)
    assert s.gate(pg.reviewer, operation(), task["id"], claim["epoch"], "pass")["state"] == "done"


def test_delegated_approval_records_the_chain_in_the_gate_audit(pg):
    s = pg.store
    judge = principal(pg, "judge-chain")
    task, claim = awaiting(pg, "chain", [judge])
    root = grant(pg, judge)
    assert root["active"] and root["created_via"] == "api" and root["chain"] == [root["id"]] and root["parent_id"] is None
    assert s.gate(judge, operation(), task["id"], claim["epoch"], "pass", ["synthetic-evidence"])["state"] == "done"
    reviewed = reviewed_events(pg, task["id"])
    assert len(reviewed) == 1 and reviewed[0]["agent"] == judge.principal
    assert reviewed[0]["metadata"]["delegation_id"] == root["id"] and reviewed[0]["metadata"]["grantor"] == pg.owner.principal
    assert reviewed[0]["metadata"]["chain"] == [root["id"]] and reviewed[0]["metadata"]["root"] is False
    row = gate_rows(pg, task["id"])[0]
    assert (str(row["delegation_id"]), row["grantor"], row["root"]) == (root["id"], pg.owner.principal, False)


def test_only_the_space_owner_or_a_may_delegate_holder_can_grant(pg):
    s = pg.store
    middle, leaf, plain = principal(pg, "middle"), principal(pg, "leaf", "human"), principal(pg, "plain")
    task, claim = awaiting(pg, "redeleg", [leaf])
    expect("forbidden", lambda: s.delegation_create(pg.reviewer, operation(), leaf, reason="not the owner"))
    expect("forbidden", lambda: s.delegation_create(pg.reader, operation(), leaf, space_id=pg.space["id"], reason="not the owner"))
    other_space = s.create_space(pg.reader, operation(), pg.prefix + "-theirs")
    expect("forbidden", lambda: s.delegation_create(pg.owner, operation(), leaf, space_id=other_space["id"], reason="owner of another space"))
    grant(pg, plain)
    expect("forbidden", lambda: s.delegation_create(plain, operation(), leaf, reason="DECISIONS 291"))
    root = grant(pg, middle, may_delegate=True)
    assert root["may_delegate"] is True
    child = s.delegation_create(middle, operation(), leaf, reason="DECISIONS 291")
    assert child["parent_id"] == root["id"] and child["grantor"] == middle.principal and child["grantor_kind"] == "agent"
    assert child["chain"] == [child["id"], root["id"]] and child["created_by"] == middle.principal
    assert s.gate(leaf, operation(), task["id"], claim["epoch"], "pass")["state"] == "done"
    reviewed = reviewed_events(pg, task["id"])[0]
    assert reviewed["metadata"]["chain"] == [child["id"], root["id"]]
    assert reviewed["metadata"]["delegation_id"] == child["id"] and reviewed["metadata"]["grantor"] == middle.principal


def test_the_root_grantor_can_be_any_kind_and_the_operator_path_is_bounded_by_the_same_rules(pg):
    s = pg.store
    judge = principal(pg, "judge-operator")
    agent_space = s.create_space(pg.editor, operation(), pg.prefix + "-agent-owned")
    assert (agent_space["owner_principal"], agent_space["owner_kind"]) == (pg.editor.principal, "agent")
    twin = Actor(pg.prefix + "-owner-twin", pg.owner.account, "agent")
    expect("invalid", lambda: s.operator_delegation_create(pg.reader, judge, "approve", pg.space["id"], None, False, "not the owner", "operator-a"))
    expect("invalid", lambda: s.operator_delegation_create(twin, judge, "approve", pg.space["id"], None, False, "same account only", "operator-a"))
    expect("invalid", lambda: s.operator_delegation_create(Actor(pg.owner.principal, pg.owner.account, "agent"), judge, "approve",
                                                           pg.space["id"], None, False, "wrong kind", "operator-a"))
    expect("invalid", lambda: s.operator_delegation_create(pg.editor, pg.editor, "approve", agent_space["id"], None, False, "to itself", "operator-a"))
    expect("invalid", lambda: s.operator_delegation_create(pg.editor, judge, "gate", agent_space["id"], None, False, "old scope", "operator-a"))
    root = s.operator_delegation_create(pg.editor, judge, "approve", agent_space["id"], None, False, "DECISIONS 291", "operator-a")
    assert root["created_via"] == "operator" and root["created_by"] == "operator-a" and root["grantor_kind"] == "agent"
    assert root["grantor"] == pg.editor.principal and root["parent_id"] is None and root["active"] is True
    event = next(row for row in s.list_audit(pg.editor, space_id=agent_space["id"], limit=1000) if row["event"] == "delegation.created")
    assert event["requested_by"] == pg.editor.principal and event["performed_by"] == "operator:operator-a"
    page = s.create_page(pg.editor, operation(), agent_space["id"], "Agent page")
    s.share(pg.editor, operation(), "space", agent_space["id"], "person", pg.reviewer.account, "edit")
    s.share(pg.editor, operation(), "space", agent_space["id"], "person", judge.account, "edit")
    task = s.create_task_block(pg.editor, operation(), page["id"], "Synthetic agent owned", [{"kind": "path", "value": pg.prefix}], [judge.principal])
    claim = s.claim(pg.reviewer, operation(), task["id"])
    s.submit_result(pg.reviewer, operation(), task["id"], claim["epoch"], "Candidate")
    assert s.gate(judge, operation(), task["id"], claim["epoch"], "pass")["state"] == "done"


def test_delegation_cycles_are_rejected_at_creation(pg):
    s = pg.store
    a, b, c = (principal(pg, name) for name in ("cycle-a", "cycle-b", "cycle-c"))
    grant(pg, a, may_delegate=True)
    s.delegation_create(a, operation(), b, may_delegate=True, reason="DECISIONS 291")
    expect("invalid", lambda: s.delegation_create(a, operation(), a, reason="to itself"))
    expect("invalid", lambda: s.delegation_create(b, operation(), a, reason="to someone above it in its own chain"))
    expect("invalid", lambda: s.delegation_create(b, operation(), b, reason="to itself"))
    expect("invalid", lambda: s.delegation_create(b, operation(), pg.owner, reason="to the root"))
    expect("invalid", lambda: s.delegation_create(pg.owner, operation(), pg.owner, reason="the root to itself"))
    assert s.delegation_create(b, operation(), c, reason="downward is fine")["chain"].__len__() == 3
    with s._transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM baton.x_delegation WHERE delegate=%s", (a.principal,)).fetchone()["n"] == 1


def test_child_delegation_never_exceeds_its_parent(pg):
    s = pg.store
    other = s.create_space(pg.owner, operation(), pg.prefix + "-narrow")
    first, second, grandchild = principal(pg, "first"), principal(pg, "second"), principal(pg, "third")
    soon = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    later = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
    root = grant(pg, first, may_delegate=True, space_id=pg.space["id"], expires_at=soon)
    expect("invalid", lambda: s.delegation_create(first, operation(), second, reason="wider: all spaces", expires_at=soon))
    expect("invalid", lambda: s.delegation_create(first, operation(), second, reason="other space", space_id=other["id"], expires_at=soon))
    expect("invalid", lambda: s.delegation_create(first, operation(), second, reason="no expiry", space_id=pg.space["id"]))
    expect("invalid", lambda: s.delegation_create(first, operation(), second, reason="later expiry", space_id=pg.space["id"], expires_at=later))
    child = s.delegation_create(first, operation(), second, reason="same scope", space_id=pg.space["id"], expires_at=soon)
    assert child["expires_at"] == root["expires_at"] and child["space_id"] == root["space_id"]
    earlier = s.delegation_create(first, operation(), grandchild, reason="narrower", space_id=pg.space["id"],
                                  expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
    assert earlier["active"]
    broad_actor = principal(pg, "broad")
    grant(pg, broad_actor, may_delegate=True)
    narrow = s.delegation_create(broad_actor, operation(), second, reason="narrow below broad", space_id=other["id"])
    assert narrow["space_id"] == other["id"] and len(narrow["chain"]) == 2


def test_delegation_chain_depth_is_capped_at_three(pg):
    s = pg.store
    a, b, c, d = (principal(pg, name) for name in ("depth-a", "depth-b", "depth-c", "depth-d"))
    grant(pg, a, may_delegate=True)
    s.delegation_create(a, operation(), b, may_delegate=True, reason="DECISIONS 291")
    third = s.delegation_create(b, operation(), c, may_delegate=True, reason="DECISIONS 291")
    assert len(third["chain"]) == 3
    expect("invalid", lambda: s.delegation_create(c, operation(), d, reason="a fourth link"))
    task, claim = awaiting(pg, "depth", [c])
    assert s.gate(c, operation(), task["id"], claim["epoch"], "pass")["state"] == "done"


def test_revoking_a_parent_disables_every_child_and_broken_chains_do_not_approve(pg):
    s = pg.store
    middle, leaf = principal(pg, "mid"), principal(pg, "leafy", "human")
    task, claim = awaiting(pg, "cascade", [leaf])
    root = grant(pg, middle, may_delegate=True)
    child = s.delegation_create(middle, operation(), leaf, reason="DECISIONS 291")
    assert next(row for row in s.delegation_list(leaf) if row["id"] == child["id"])["active"]
    s.delegation_revoke(pg.owner, operation(), root["id"])
    listed = {row["id"]: row for row in s.delegation_list(leaf)}
    assert listed[child["id"]]["active"] is False and listed[child["id"]]["revoked_at"] is None
    expect("delegation_required", lambda: s.gate(leaf, operation(), task["id"], claim["epoch"], "pass"))
    expect("forbidden", lambda: s.delegation_create(middle, operation(), principal(pg, "late"), reason="parent revoked"))
    other = principal(pg, "orphan")
    expired_parent = raw_delegation(pg, middle, may_delegate=True)
    inactive_child = raw_delegation(pg, other, parent_id=expired_parent, grantor=middle, expires_hours=None, created_hours=-1)
    assert [row["active"] for row in s.delegation_list(other) if row["id"] == inactive_child] == [False]
    holder = principal(pg, "no-redelegation", "human")
    unauthorized = raw_delegation(pg, leaf, parent_id=grant(pg, holder)["id"], grantor=holder, created_hours=0, expires_hours=None)
    assert [row["active"] for row in s.delegation_list(leaf) if row["id"] == unauthorized] == [False]
    not_owner = raw_delegation(pg, leaf, grantor=pg.reader, created_hours=0, expires_hours=None)
    not_owner_space = raw_delegation(pg, leaf, grantor=pg.reader, created_hours=0, expires_hours=None, space_id=pg.space["id"])
    assert [row["active"] for row in s.delegation_list(leaf) if row["id"] == not_owner_space] == [False]
    assert not_owner
    expect("delegation_required", lambda: s.gate(leaf, operation(), task["id"], claim["epoch"], "pass"))


def test_expired_and_revoked_delegations_are_refused_and_replay_rechecks(pg):
    s = pg.store
    judge = principal(pg, "judge-exp")
    task, claim = awaiting(pg, "expired", [judge])
    expired = raw_delegation(pg, judge)
    assert [row["active"] for row in s.delegation_list(judge) if row["id"] == expired] == [False]
    expect("delegation_required", lambda: s.gate(judge, operation(), task["id"], claim["epoch"], "pass"))
    first = grant(pg, judge)
    revoked = s.delegation_revoke(pg.owner, operation(), first["id"])
    assert revoked["revoked_by"] == pg.owner.principal and not revoked["active"]
    expect("delegation_required", lambda: s.gate(judge, operation(), task["id"], claim["epoch"], "pass"))
    second = grant(pg, judge, expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
    op = operation()
    assert s.gate(judge, op, task["id"], claim["epoch"], "pass")["state"] == "done"
    assert s.gate(judge, op, task["id"], claim["epoch"], "pass")["replay"] is True
    s.delegation_revoke(pg.owner, operation(), second["id"])
    expect("delegation_required", lambda: s.gate(judge, op, task["id"], claim["epoch"], "pass"))
    assert len(reviewed_events(pg, task["id"])) == 1


def test_a_grantee_is_bounded_to_the_space_and_roots_only_reach_spaces_their_grantor_owns(pg):
    s = pg.store
    other_space = s.create_space(pg.owner, operation(), pg.prefix + "-other")
    other_page = s.create_page(pg.owner, operation(), other_space["id"], "Other page")
    judge = principal(pg, "judge-space")
    home_task, home_claim = awaiting(pg, "home", [judge])
    foreign_task, foreign_claim = awaiting(pg, "foreign", [judge], other_space, other_page)
    scoped = grant(pg, judge, space_id=pg.space["id"])
    assert scoped["space_id"] == pg.space["id"]
    expect("delegation_required", lambda: s.gate(judge, operation(), foreign_task["id"], foreign_claim["epoch"], "pass"))
    assert s.gate(judge, operation(), home_task["id"], home_claim["epoch"], "pass")["state"] == "done"
    grant(pg, judge, space_id=other_space["id"])
    assert s.gate(judge, operation(), foreign_task["id"], foreign_claim["epoch"], "pass")["state"] == "done"


def test_all_spaces_delegation_covers_every_space_the_root_owns(pg):
    s = pg.store
    other_space = s.create_space(pg.owner, operation(), pg.prefix + "-all")
    other_page = s.create_page(pg.owner, operation(), other_space["id"], "All page")
    judge = principal(pg, "judge-all", "human")
    task, claim = awaiting(pg, "all", [judge], other_space, other_page)
    root = grant(pg, judge)
    assert root["space_id"] is None
    assert s.gate(judge, operation(), task["id"], claim["epoch"], "pass")["state"] == "done"
    foreign = s.create_space(pg.reader, operation(), pg.prefix + "-not-owned")
    foreign_page = s.create_page(pg.reader, operation(), foreign["id"], "Foreign page")
    s.share(pg.reader, operation(), "space", foreign["id"], "person", judge.account, "edit")
    s.share(pg.reader, operation(), "space", foreign["id"], "person", pg.editor.account, "edit")
    unowned = s.create_task_block(pg.reader, operation(), foreign_page["id"], "Synthetic foreign", [{"kind": "path", "value": pg.prefix + "/f"}], [judge.principal])
    held = s.claim(pg.editor, operation(), unowned["id"])
    s.submit_result(pg.editor, operation(), unowned["id"], held["epoch"], "Candidate")
    expect("delegation_required", lambda: s.gate(judge, operation(), unowned["id"], held["epoch"], "pass"))


def test_root_self_approval_needs_the_explicit_flag_and_is_recorded_as_such(pg):
    s = pg.store
    task = s.create_task_block(pg.owner, operation(), pg.page["id"], "Synthetic owner executes", [{"kind": "path", "value": pg.prefix + "/self"}], [pg.reviewer.principal])
    claim = s.claim(pg.owner, operation(), task["id"])
    s.submit_result(pg.owner, operation(), task["id"], claim["epoch"], "Owner candidate")
    expect("forbidden", lambda: s.gate(pg.owner, operation(), task["id"], claim["epoch"], "pass"))
    expect("forbidden", lambda: s.gate(pg.owner, operation(), task["id"], claim["epoch"], "pass", self_approval=False))
    expect("invalid", lambda: s.gate(pg.owner, operation(), task["id"], claim["epoch"], "pass", self_approval="yes"))
    assert s.get_task(pg.owner, task["id"])["state"] == "awaiting_gate"
    done = s.gate(pg.owner, operation(), task["id"], claim["epoch"], "pass", self_approval=True)
    assert done["state"] == "done"
    row = gate_rows(pg, task["id"])[0]
    assert (row["root"], row["self_approval"], row["reviewer"]) == (True, True, pg.owner.principal)
    event = reviewed_events(pg, task["id"])[0]
    assert event["metadata"]["self_approval"] is True and event["metadata"]["root"] is True and event["agent"] == pg.owner.principal
    other, other_claim = awaiting(pg, "owner-approves-others", [pg.reviewer])
    s.gate(pg.owner, operation(), other["id"], other_claim["epoch"], "pass", self_approval=True)
    assert gate_rows(pg, other["id"])[0]["self_approval"] is False


def test_non_root_self_approval_is_refused_even_with_the_flag(pg):
    s = pg.store
    first, second = principal(pg, "exec-first"), principal(pg, "exec-second")
    for who in (first, second, pg.editor):
        grant(pg, who)
    s.share(pg.owner, operation(), "space", pg.space["id"], "person", first.account, "edit")
    s.share(pg.owner, operation(), "space", pg.space["id"], "person", second.account, "edit")
    task = s.create_task_block(pg.owner, operation(), pg.page["id"], "Synthetic handed off", [{"kind": "path", "value": pg.prefix + "/exec"}],
                               [first.principal, second.principal, pg.editor.principal])
    claim = s.claim(first, operation(), task["id"])
    handed = s.handoff(first, operation(), task["id"], claim["epoch"], second)
    s.submit_result(second, operation(), task["id"], handed["epoch"], "Candidate")
    for refused in (second, first):
        for flag in (False, True):
            expect("forbidden", lambda refused=refused, flag=flag: s.gate(refused, operation(), task["id"], handed["epoch"], "pass", self_approval=flag))
    alias = Actor(pg.prefix + "-exec-alias", first.account, "agent")
    grant(pg, alias)
    other = s.create_task_block(pg.owner, operation(), pg.page["id"], "Synthetic alias", [{"kind": "path", "value": pg.prefix + "/alias"}], [alias.principal])
    held = s.claim(first, operation(), other["id"])
    s.submit_result(first, operation(), other["id"], held["epoch"], "Candidate")
    expect("forbidden", lambda: s.gate(alias, operation(), other["id"], held["epoch"], "pass", self_approval=True))
    # The task owner created the request but neither submitted nor executed the result, so it approves as root.
    assert s.gate(pg.owner, operation(), other["id"], held["epoch"], "pass")["state"] == "done"
    assert s.gate(pg.owner, operation(), task["id"], handed["epoch"], "pass")["state"] == "done"


def test_claim_holder_of_an_earlier_epoch_may_approve_a_later_epoch(pg):
    s = pg.store
    grant(pg, pg.editor)
    grant(pg, pg.reviewer)
    other = principal(pg, "later-worker")
    s.share(pg.owner, operation(), "space", pg.space["id"], "person", other.account, "edit")
    task = s.create_task_block(pg.owner, operation(), pg.page["id"], "Synthetic epochs", [{"kind": "path", "value": pg.prefix + "/epochs"}],
                               [pg.editor.principal, pg.reviewer.principal])
    first = s.claim(pg.editor, operation(), task["id"])
    s.submit_result(pg.editor, operation(), task["id"], first["epoch"], "Candidate one")
    assert s.gate(pg.reviewer, operation(), task["id"], first["epoch"], "fail")["state"] == "queued"
    second = s.claim(other, operation(), task["id"])
    s.submit_result(other, operation(), task["id"], second["epoch"], "Candidate two")
    assert s.gate(pg.editor, operation(), task["id"], second["epoch"], "pass")["state"] == "done"


def test_two_party_confirmation_by_task_override_needs_two_distinct_passes(pg):
    s = pg.store
    one, two = principal(pg, "party-one"), principal(pg, "party-two", "human")
    grant(pg, one)
    grant(pg, two)
    task, claim = awaiting(pg, "twoparty", [one, two], approvals_required=2)
    assert s.get_task(pg.owner, task["id"])["approvals_required"] == 2
    op = operation()
    first = s.gate(one, op, task["id"], claim["epoch"], "pass")
    assert first["state"] == "awaiting_gate" and first["approvals_passed"] == 1 and first["approvals_required"] == 2
    assert s.gate(one, op, task["id"], claim["epoch"], "pass")["replay"] is True
    expect("forbidden", lambda: s.gate(one, operation(), task["id"], claim["epoch"], "pass"))
    assert s.get_task(pg.owner, task["id"])["state"] == "awaiting_gate"
    assert not any(row["event"] == "task.completed" for row in s.list_audit(pg.owner, task_id=task["id"]))
    done = s.gate(two, operation(), task["id"], claim["epoch"], "pass")
    assert done["state"] == "done" and done["approvals_passed"] == 2
    events = reviewed_events(pg, task["id"])
    assert [(row["metadata"]["approvals"], row["metadata"]["approvals_required"]) for row in events] == [(1, 2), (2, 2)]
    assert len([row for row in s.list_audit(pg.owner, task_id=task["id"]) if row["event"] == "task.completed"]) == 1


def test_a_fail_from_any_party_sends_a_two_party_task_back_and_old_passes_do_not_count(pg):
    s = pg.store
    one, two = principal(pg, "fail-one"), principal(pg, "fail-two")
    grant(pg, one)
    grant(pg, two)
    task, claim = awaiting(pg, "twofail", [one, two], approvals_required=2)
    s.gate(one, operation(), task["id"], claim["epoch"], "pass")
    assert s.gate(two, operation(), task["id"], claim["epoch"], "fail")["state"] == "queued"
    second = s.claim(pg.editor, operation(), task["id"])
    s.submit_result(pg.editor, operation(), task["id"], second["epoch"], "Candidate two")
    again = s.gate(one, operation(), task["id"], second["epoch"], "pass")
    assert again["state"] == "awaiting_gate" and again["approvals_passed"] == 1
    assert s.gate(two, operation(), task["id"], second["epoch"], "pass")["state"] == "done"


def test_policy_requires_two_parties_for_matching_scopes_and_root_required_is_honoured(pg):
    s = pg.store
    one = principal(pg, "policy-one")
    grant(pg, one)
    expect("invalid", lambda: s.set_approval_policy(pg.space["id"], "path", pg.prefix, 3, False, "operator-a"))
    expect("invalid", lambda: s.set_approval_policy(pg.space["id"], "path", pg.prefix, 1, "yes", "operator-a"))
    expect("invalid", lambda: s.set_approval_policy(pg.space["id"], "glob", pg.prefix, 1, False, "operator-a"))
    expect("not_found", lambda: s.set_approval_policy(str(uuid.uuid4()), "path", pg.prefix, 1, False, "operator-a"))
    policy = s.set_approval_policy(pg.space["id"], "path", pg.prefix + "/guarded", 1, True, "operator-a")
    assert (policy["required_approvals"], policy["root_required"]) == (1, True)
    guarded, guarded_claim = awaiting(pg, "guarded/api", [one])
    plain, plain_claim = awaiting(pg, "unguarded", [one])
    assert s.get_task(pg.owner, plain["id"])["root_required"] is False
    assert s.gate(one, operation(), plain["id"], plain_claim["epoch"], "pass")["state"] == "done"
    view = s.get_task(pg.owner, guarded["id"])
    assert view["root_required"] is True and view["approvals_required"] == 1
    held = s.gate(one, operation(), guarded["id"], guarded_claim["epoch"], "pass")
    assert held["state"] == "awaiting_gate" and held["approvals_passed"] == 1
    assert s.gate(pg.owner, operation(), guarded["id"], guarded_claim["epoch"], "pass")["state"] == "done"
    assert [row["metadata"]["root"] for row in reviewed_events(pg, guarded["id"])] == [False, True]


def test_root_fail_is_final_when_the_root_is_required(pg):
    s = pg.store
    one = principal(pg, "final-one")
    grant(pg, one)
    s.set_approval_policy(pg.space["id"], "path", "", 1, True, "operator-a")
    task, claim = awaiting(pg, "final", [one])
    assert s.gate(one, operation(), task["id"], claim["epoch"], "pass")["state"] == "awaiting_gate"
    assert s.gate(pg.owner, operation(), task["id"], claim["epoch"], "fail")["state"] == "queued"
    second = s.claim(pg.editor, operation(), task["id"])
    s.submit_result(pg.editor, operation(), task["id"], second["epoch"], "Candidate two")
    assert s.gate(one, operation(), task["id"], second["epoch"], "pass")["state"] == "awaiting_gate"
    assert s.gate(pg.owner, operation(), task["id"], second["epoch"], "pass")["state"] == "done"


def test_one_human_organization_is_the_root_plus_one_delegated_approver(pg):
    s = pg.store
    helper = principal(pg, "only-helper")
    grant(pg, helper)
    s.set_approval_policy(pg.space["id"], "path", "", 2, True, "operator-a")
    task, claim = awaiting(pg, "one-human", [helper])
    assert s.gate(helper, operation(), task["id"], claim["epoch"], "pass")["state"] == "awaiting_gate"
    done = s.gate(pg.owner, operation(), task["id"], claim["epoch"], "pass")
    assert done["state"] == "done" and done["approvals_passed"] == 2


def test_task_override_can_raise_a_policy_but_only_the_space_owner_can_lower_it(pg):
    s = pg.store
    one = principal(pg, "override-one")
    grant(pg, one)
    s.set_approval_policy(pg.space["id"], "path", "", 2, False, "operator-a")
    by_owner = s.create_task_block(pg.owner, operation(), pg.page["id"], "Synthetic lowered", [{"kind": "path", "value": pg.prefix + "/o1"}],
                                   [one.principal], approvals_required=1)
    assert s.get_task(pg.owner, by_owner["id"])["approvals_required"] == 1
    s.share(pg.owner, operation(), "space", pg.space["id"], "person", pg.editor.account, "edit")
    by_editor = s.create_task_block(pg.editor, operation(), pg.page["id"], "Synthetic not lowered", [{"kind": "path", "value": pg.prefix + "/o2"}],
                                    [one.principal], approvals_required=1)
    assert s.get_task(pg.owner, by_editor["id"])["approvals_required"] == 2
    default = s.create_task_block(pg.owner, operation(), pg.page["id"], "Synthetic default", [{"kind": "path", "value": pg.prefix + "/o3"}], [one.principal])
    assert s.get_task(pg.owner, default["id"])["approvals_required"] == 2
    for bad in (3, 0, True, "2", 1.0):
        expect("invalid", lambda bad=bad: s.create_task_block(pg.owner, operation(), pg.page["id"], "Synthetic bad", [{"kind": "path", "value": pg.prefix}],
                                                              [one.principal], approvals_required=bad))
    unassigned = s.submit_task(pg.owner, operation(), "open-override", "Synthetic open", page_id=pg.page["id"],
                               scopes=[{"kind": "path", "value": pg.prefix + "/o5"}], approvals_required=2)
    assert s.get_task(pg.owner, unassigned["id"])["approvals_required"] == 2
    expect("invalid", lambda: s.submit_task(pg.owner, operation(), "open-override-bad", "Synthetic open", page_id=pg.page["id"], approvals_required=3))
    assigned = s.submit_task(pg.owner, operation(), "assigned-override", "Synthetic assigned", page_id=pg.page["id"], assignee=pg.editor,
                             scopes=[{"kind": "path", "value": pg.prefix + "/o4"}], approvals_required=2)
    assert s.get_task(pg.owner, assigned["id"])["approvals_required"] == 2


def test_policy_scopes_match_by_kind_and_prefix_and_changes_are_audited(pg):
    s = pg.store
    s.set_approval_policy(pg.space["id"], "path", pg.prefix + "/area", 2, False, "operator-a")
    s.set_approval_policy(pg.space["id"], "migration", "7", 2, True, "operator-a")
    task = s.create_task_block(pg.owner, operation(), pg.page["id"], "Synthetic scoped", [{"kind": "path", "value": pg.prefix + "/area/sub"}], [pg.reviewer.principal])
    assert (s.get_task(pg.owner, task["id"])["approvals_required"], s.get_task(pg.owner, task["id"])["root_required"]) == (2, False)
    sibling = s.create_task_block(pg.owner, operation(), pg.page["id"], "Synthetic sibling", [{"kind": "path", "value": pg.prefix + "/areas"}], [pg.reviewer.principal])
    assert s.get_task(pg.owner, sibling["id"])["approvals_required"] == 1
    migration = s.create_task_block(pg.owner, operation(), pg.page["id"], "Synthetic migration", [{"kind": "migration", "value": "007"}], [pg.reviewer.principal])
    view = s.get_task(pg.owner, migration["id"])
    assert (view["approvals_required"], view["root_required"]) == (2, True)
    listed = s.list_approval_policies(pg.space["id"])
    assert {(row["scope_kind"], row["scope_value"]) for row in listed} == {("path", pg.prefix + "/area"), ("migration", "7")}
    s.set_approval_policy(pg.space["id"], "migration", "7", 1, False, "operator-b")
    s.remove_approval_policy(pg.space["id"], "path", pg.prefix + "/area", "operator-b")
    expect("not_found", lambda: s.remove_approval_policy(pg.space["id"], "path", pg.prefix + "/area", "operator-b"))
    assert [(row["scope_kind"], row["required_approvals"]) for row in s.list_approval_policies(pg.space["id"])] == [("migration", 1)]
    events = [row for row in s.list_audit(pg.owner, space_id=pg.space["id"], limit=1000) if row["event"].startswith("approval_policy.")]
    assert [row["event"] for row in events] == ["approval_policy.set", "approval_policy.set", "approval_policy.set", "approval_policy.removed"]
    assert all(row["performed_by"].startswith("operator:operator-") and row["metadata"]["scope_kind"] in {"path", "migration"} for row in events)
    assert pg.prefix not in json.dumps(events)


def test_legacy_worker_success_rows_stay_readable_and_nobody_can_gate_them(pg):
    s = pg.store
    done = s.submit_task(pg.owner, operation(), "legacy-exempt", "Synthetic legacy", page_id=pg.page["id"], scopes=[{"kind": "path", "value": pg.prefix}])
    to_legacy(pg, done["id"], state="done", dispatched=True, backend="demo", result="Demo output", epoch=1)
    with pg.store._transaction(write=True) as conn:
        conn.execute("""INSERT INTO baton.x_gate(id,task_id,epoch,decision,reviewer,reviewer_account,evidence)
            VALUES (%s,%s,1,'pass','codex-worker','baton-service','{}'::jsonb)""", (uuid.uuid4(), done["id"]))
    view = s.get_task(pg.owner, done["id"])
    assert view["status"] == "completed" and view["gate_mode"] == "worker_success" and view["approvals_required"] is None
    row = gate_rows(pg, done["id"])[0]
    assert (row["delegation_id"], row["grantor"], row["root"], row["self_approval"]) == (None, None, False, False)
    for caller in (SERVICE_ACTOR, pg.owner, pg.reviewer):
        expect("forbidden", lambda caller=caller: s.gate(caller, operation(), done["id"], 1, "pass"))
    expect("invalid", lambda: s.delegation_create(pg.owner, operation(), Actor("codex-worker", "legacy-service", "agent"), reason="DECISIONS 291"))


def test_delegation_api_validation_and_authority(pg):
    s = pg.store
    judge = principal(pg, "judge-api")
    for caller in (SERVICE_ACTOR, pg.editor, judge, pg.reviewer):
        expect("forbidden", lambda caller=caller: s.delegation_create(caller, operation(), principal(pg, "target"), reason="DECISIONS 291"))
    expect("forbidden", lambda: s.delegation_revoke(SERVICE_ACTOR, operation(), str(uuid.uuid4())))
    expect("invalid", lambda: s.delegation_create(pg.owner, operation(), SERVICE_ACTOR, reason="DECISIONS 291"))
    expect("invalid", lambda: s.delegation_create(pg.owner, operation(), pg.owner, reason="DECISIONS 291"))
    expect("invalid", lambda: s.delegation_create(pg.owner, operation(), judge, scope="gate", reason="DECISIONS 291"))
    expect("invalid", lambda: s.delegation_create(pg.owner, operation(), judge, may_delegate="yes", reason="DECISIONS 291"))
    expect("invalid", lambda: s.delegation_create(pg.owner, operation(), judge, reason="x" * 501))
    expect("invalid", lambda: s.delegation_create(pg.owner, operation(), judge, reason="   "))
    expect("invalid", lambda: s.delegation_create(pg.owner, operation(), judge, reason=None))
    expect("invalid", lambda: s.delegation_create(pg.owner, operation(), judge, space_id="not-a-uuid", reason="DECISIONS 291"))
    expect("invalid", lambda: s.delegation_create(pg.owner, operation(), judge, expires_at="2099-01-01T00:00:00", reason="DECISIONS 291"))
    expect("invalid", lambda: s.delegation_create(pg.owner, operation(), judge, expires_at="soon", reason="DECISIONS 291"))
    expect("invalid", lambda: s.delegation_create(pg.owner, operation(), judge, expires_at="2001-01-01T00:00:00+00:00", reason="DECISIONS 291"))
    expect("not_found", lambda: s.delegation_create(pg.owner, operation(), judge, space_id=str(uuid.uuid4()), reason="DECISIONS 291"))
    assert len(grant(pg, judge, reason="r" * 500)["reason"]) == 500
    given = grant(pg, judge, space_id=pg.space["id"], expires_at="2099-01-01T00:00:00Z")
    expect("forbidden", lambda: s.delegation_revoke(pg.reviewer, operation(), given["id"]))
    expect("forbidden", lambda: s.delegation_revoke(judge, operation(), given["id"]))
    expect("not_found", lambda: s.delegation_revoke(pg.owner, operation(), str(uuid.uuid4())))
    op = operation()
    first = s.delegation_revoke(pg.owner, op, given["id"])
    assert s.delegation_revoke(pg.owner, op, given["id"])["replay"] is True
    assert s.delegation_revoke(pg.owner, operation(), given["id"])["revoked_at"] == first["revoked_at"]
    events = [row["event"] for row in s.list_audit(pg.owner, space_id=pg.space["id"], limit=1000) if row["metadata"].get("delegation_id") == given["id"]]
    assert events == ["delegation.created", "delegation.revoked"]
    create_op = operation()
    one = s.delegation_create(pg.owner, create_op, judge, reason="Same")
    assert s.delegation_create(pg.owner, create_op, judge, reason="Same")["id"] == one["id"]
    expect("operation_conflict", lambda: s.delegation_create(pg.owner, create_op, judge, reason="Different"))
    assert one["id"] in {row["id"] for row in s.delegation_list(pg.owner)} and one["id"] in {row["id"] for row in s.delegation_list(judge)}
    assert s.delegation_list(pg.reviewer) == [] and s.delegation_list(pg.editor) == []
    assert given["id"] in {row["id"] for row in s.delegation_list(judge) if not row["active"]}


def test_delegation_audit_metadata_is_identifiers_only(pg):
    s = pg.store
    middle, leaf = principal(pg, "meta-mid"), principal(pg, "meta-leaf")
    root = grant(pg, middle, may_delegate=True, space_id=pg.space["id"], reason="Synthetic private reason text")
    child = s.delegation_create(middle, operation(), leaf, space_id=pg.space["id"], reason="Synthetic private reason text")
    s.delegation_revoke(middle, operation(), child["id"])
    rows = [row for row in s.list_audit(pg.owner, space_id=pg.space["id"], limit=1000)
            if row["metadata"].get("delegation_id") in {root["id"], child["id"]}]
    assert [(row["event"], row["metadata"]["chain"]) for row in rows] == [
        ("delegation.created", [root["id"]]), ("delegation.created", [child["id"], root["id"]]), ("delegation.revoked", [child["id"], root["id"]])]
    for row in rows:
        assert set(row["metadata"]) == {"delegation_id", "scope", "created_via", "chain"}
        assert row["space_id"] == pg.space["id"] and "Synthetic private reason text" not in json.dumps(row)
    assert rows[0]["actor_kind"] == "human" and rows[1]["actor_kind"] == "agent" and rows[1]["agent"] == middle.principal


def test_runtime_role_can_only_revoke_delegations_and_only_append_claim_holders(pg):
    judge = principal(pg, "judge-immutable")
    given = grant(pg, judge)
    with pg.store._transaction() as conn:
        for statement in ("UPDATE baton.x_delegation SET reason='tampered'", "UPDATE baton.x_delegation SET expires_at=NULL",
                          "UPDATE baton.x_delegation SET parent_id=NULL", "DELETE FROM baton.x_delegation", "TRUNCATE baton.x_delegation",
                          "UPDATE baton.x_claim_holder SET holder='someone'", "DELETE FROM baton.x_claim_holder"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with conn.transaction():
                    conn.execute(statement)
    config_path = os.environ.get("BATON_TEST_DATABASE_ADMIN_CONFIG")
    if not config_path:
        pytest.skip("Owner-trigger check needs explicitly authorized test admin config")
    config = json.loads(Path(config_path).read_text())
    with psycopg.connect(**config) as conn:
        conn.execute("SET ROLE baton_owner")
        for statement in ("UPDATE baton.x_delegation SET reason='tampered'", "UPDATE baton.x_delegation SET revoked_at=NULL, revoked_by=NULL",
                          "DELETE FROM baton.x_delegation", "TRUNCATE baton.x_delegation CASCADE"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with conn.transaction():
                    conn.execute(statement)
    pg.store.delegation_revoke(pg.owner, operation(), given["id"])
    with psycopg.connect(**config) as conn:
        conn.execute("SET ROLE baton_owner")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.transaction():
                conn.execute("UPDATE baton.x_delegation SET revoked_by='someone-else' WHERE id=%s", (given["id"],))


def test_table_constraints_reject_bad_delegations_policies_and_overrides(pg):
    manual(pg, "constraint")
    base = """INSERT INTO baton.x_delegation(id,grantor,grantor_account,grantor_kind,delegate,delegate_account,delegate_kind,
        scope,reason,created_via,created_by,parent_id) VALUES (gen_random_uuid(),'g','a',%s,'d','da','agent',%s,%s,%s,'creator',%s)"""
    with pg.store._transaction() as conn:
        for params in (("service", "approve", "ok", "api", None), ("human", "gate", "ok", "api", None),
                       ("human", "approve", "", "api", None), ("human", "approve", "x" * 501, "api", None), ("human", "approve", "ok", "script", None)):
            with pytest.raises(psycopg.errors.CheckViolation):
                with conn.transaction():
                    conn.execute(base, params)
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            with conn.transaction():
                conn.execute(base, ("agent", "approve", "ok", "api", str(uuid.uuid4())))
        for required in (0, 3):
            with pytest.raises(psycopg.errors.CheckViolation):
                with conn.transaction():
                    conn.execute("INSERT INTO baton.x_approval_policy(space_id,scope_kind,scope_value,required_approvals) VALUES (%s,'path','',%s)",
                                 (pg.space["id"], required))
        with pytest.raises(psycopg.errors.CheckViolation):
            with conn.transaction():
                conn.execute("UPDATE baton.tasks SET approvals_required=3 WHERE owner LIKE %s", (pg.prefix + "%",))


def test_whole_space_policy_applies_to_tasks_with_scope_none_but_narrower_policies_do_not(pg):
    s = pg.store
    s.set_approval_policy(pg.space["id"], "path", pg.prefix + "/narrow", 2, True, "operator-a")
    undeclared = s.create_task_block(pg.owner, operation(), pg.page["id"], "Synthetic undeclared scope", None, [pg.reviewer.principal])
    assert undeclared["scopes"] == [{"kind": "none"}]
    view = s.get_task(pg.owner, undeclared["id"])
    assert (view["approvals_required"], view["root_required"]) == (1, False)
    s.set_approval_policy(pg.space["id"], "path", "", 2, False, "operator-a")
    assert s.get_task(pg.owner, undeclared["id"])["approvals_required"] == 2


def test_create_space_records_the_creating_identity_as_the_root(pg):
    assert (pg.space["owner_principal"], pg.space["owner_account"], pg.space["owner_kind"]) == (pg.owner.principal, pg.owner.account, "human")
    agent_space = pg.store.create_space(pg.editor, operation(), pg.prefix + "-agent-space")
    assert (agent_space["owner_principal"], agent_space["owner_kind"]) == (pg.editor.principal, "agent")
    default = pg.store.submit_task(pg.editor, operation(), "default-space-owner", "Synthetic default", scopes=[{"kind": "path", "value": pg.prefix}])
    with pg.store._transaction() as conn:
        row = conn.execute("SELECT s.owner_principal,s.owner_kind FROM baton.spaces s JOIN baton.pages p ON p.space_id=s.id WHERE p.id=%s",
                           (default["page_id"],)).fetchone()
    assert row == {"owner_principal": pg.editor.principal, "owner_kind": "agent"}


def test_another_principal_on_the_owner_account_is_not_root_and_needs_a_delegation(pg):
    s = pg.store
    twin = Actor(pg.prefix + "-owner-twin", pg.owner.account, "human")
    agent_twin = Actor(pg.prefix + "-owner-agent", pg.owner.account, "agent")
    wrong_kind = Actor(pg.owner.principal, pg.owner.account, "agent")
    task, claim = awaiting(pg, "twins", [twin, agent_twin, wrong_kind])
    for impostor in (twin, agent_twin, wrong_kind):
        assert s.effective_role(impostor, "space", pg.space["id"])["role"] == "edit"
        expect("delegation_required", lambda impostor=impostor: s.gate(impostor, operation(), task["id"], claim["epoch"], "pass"))
        expect("forbidden", lambda impostor=impostor: s.delegation_create(impostor, operation(), principal(pg, "twin-target"), reason="not the root identity"))
        expect("invalid", lambda impostor=impostor: s.operator_delegation_create(impostor, principal(pg, "twin-target"), "approve", pg.space["id"],
                                                                                 None, False, "not the root identity", "operator-a"))
    assert s.get_task(pg.owner, task["id"])["state"] == "awaiting_gate"
    given = grant(pg, agent_twin)
    done = s.gate(agent_twin, operation(), task["id"], claim["epoch"], "pass")
    assert done["state"] == "done" and gate_rows(pg, task["id"])[0]["root"] is False
    assert reviewed_events(pg, task["id"])[0]["metadata"]["chain"] == [given["id"]]


def test_a_root_delegation_must_come_from_the_exact_owner_identity(pg):
    s = pg.store
    twin = Actor(pg.prefix + "-owner-twin2", pg.owner.account, "human")
    judge = principal(pg, "judge-exact")
    task, claim = awaiting(pg, "exact-root", [judge])
    forged = raw_delegation(pg, judge, grantor=twin, created_hours=0, expires_hours=None, space_id=pg.space["id"])
    forged_all = raw_delegation(pg, judge, grantor=twin, created_hours=0, expires_hours=None)
    listed = {row["id"]: row for row in s.delegation_list(judge)}
    assert listed[forged]["active"] is False and listed[forged_all]["active"] is True
    expect("delegation_required", lambda: s.gate(judge, operation(), task["id"], claim["epoch"], "pass"))
    grant(pg, judge)
    assert s.gate(judge, operation(), task["id"], claim["epoch"], "pass")["state"] == "done"


def test_a_space_without_an_owner_principal_has_no_root_until_an_operator_sets_it(pg):
    s = pg.store
    space_id = str(uuid.uuid4())
    with s._transaction(write=True) as conn:
        conn.execute("INSERT INTO baton.spaces(id,name,owner_account) VALUES (%s,%s,%s)", (space_id, pg.prefix + "-legacy", pg.owner.account))
        conn.execute("INSERT INTO baton.pages(id,space_id,owner_account,title) VALUES (%s,%s,%s,'Legacy')", (str(uuid.uuid4()), space_id, pg.owner.account))
    legacy = s.get_space(pg.owner, space_id)
    assert legacy["owner_principal"] is None and legacy["owner_kind"] is None
    page = s.list_pages(pg.owner, space_id)[0]
    s.share(pg.owner, operation(), "space", space_id, "person", pg.editor.account, "edit")
    task = s.create_task_block(pg.owner, operation(), page["id"], "Synthetic legacy", [{"kind": "path", "value": pg.prefix}], [pg.owner.principal])
    claim = s.claim(pg.editor, operation(), task["id"])
    s.submit_result(pg.editor, operation(), task["id"], claim["epoch"], "Candidate")
    expect("delegation_required", lambda: s.gate(pg.owner, operation(), task["id"], claim["epoch"], "pass"))
    expect("forbidden", lambda: s.delegation_create(pg.owner, operation(), principal(pg, "legacy-target"), space_id=space_id, reason="no root yet"))
    expect("invalid", lambda: s.operator_delegation_create(pg.owner, principal(pg, "legacy-target"), "approve", space_id, None, False, "no root yet", "operator-a"))
    assert s.get_task(pg.owner, task["id"])["state"] == "awaiting_gate"
    expect("invalid", lambda: s.set_space_owner(space_id, pg.owner.principal, pg.reader.account, "human", "operator-a"))
    expect("invalid", lambda: s.set_space_owner(space_id, pg.owner.principal, pg.owner.account, "service", "operator-a"))
    expect("not_found", lambda: s.set_space_owner(str(uuid.uuid4()), pg.owner.principal, pg.owner.account, "human", "operator-a"))
    updated = s.set_space_owner(space_id, pg.owner.principal, pg.owner.account, "human", "operator-a")
    assert (updated["owner_principal"], updated["owner_kind"]) == (pg.owner.principal, "human")
    assert s.gate(pg.owner, operation(), task["id"], claim["epoch"], "pass")["state"] == "done"
    event = next(row for row in s.list_audit(pg.owner, space_id=space_id, limit=100) if row["event"] == "space.owner_set")
    assert event["performed_by"] == "operator:operator-a" and event["requested_by"] == pg.owner.principal
    assert event["metadata"] == {"owner_kind": "human"}


def test_migration_backfills_owner_principal_from_the_space_creation_audit(pg):
    config_path = os.environ.get("BATON_TEST_DATABASE_ADMIN_CONFIG")
    if not config_path:
        pytest.skip("Backfill check needs explicitly authorized test admin config")
    s = pg.store
    agent_space = s.create_space(pg.editor, operation(), pg.prefix + "-backfill")
    unknown = str(uuid.uuid4())
    mismatched = s.create_space(pg.reviewer, operation(), pg.prefix + "-mismatch")
    with s._transaction(write=True) as conn:
        conn.execute("UPDATE baton.spaces SET owner_principal=NULL,owner_kind=NULL WHERE id=ANY(%s::uuid[])",
                     ([pg.space["id"], agent_space["id"], mismatched["id"]],))
        conn.execute("UPDATE baton.spaces SET owner_account=%s WHERE id=%s", (pg.prefix + "-someone-else", mismatched["id"]))
        conn.execute("INSERT INTO baton.spaces(id,name,owner_account) VALUES (%s,%s,%s)", (unknown, pg.prefix + "-no-audit", pg.owner.account))
    schema = (Path(__file__).parents[1] / "bridge/space_schema.sql").read_text()
    config = json.loads(Path(config_path).read_text())
    with psycopg.connect(**config, autocommit=True) as conn:
        conn.execute("SET ROLE baton_owner")
        conn.execute(schema)
        conn.execute(schema)
    assert (s.get_space(pg.owner, pg.space["id"])["owner_principal"], s.get_space(pg.owner, pg.space["id"])["owner_kind"]) == (pg.owner.principal, "human")
    assert (s.get_space(pg.editor, agent_space["id"])["owner_principal"], s.get_space(pg.editor, agent_space["id"])["owner_kind"]) == (pg.editor.principal, "agent")
    with s._transaction() as conn:
        rows = {str(row["id"]): row for row in conn.execute("SELECT id,owner_principal,owner_kind FROM baton.spaces WHERE id=ANY(%s::uuid[])",
                                                            ([mismatched["id"], unknown],))}
    assert rows[mismatched["id"]]["owner_principal"] is None and rows[unknown]["owner_principal"] is None
    with s._transaction() as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            with conn.transaction():
                conn.execute("UPDATE baton.spaces SET owner_principal='x' WHERE id=%s", (unknown,))


# --- DF-14' approve with edits, approver identity in gate responses; DF-15 result epoch in awaiting_gate notifications ---

def sha(text):
    import hashlib
    return hashlib.sha256(text.encode()).hexdigest()


def overridden_events(pg, task_id):
    return [row for row in pg.store.list_audit(pg.owner, task_id=task_id) if row["event"] == "gate.result_overridden"]


def test_edit_decision_approves_with_edits_keeping_both_results_and_recording_the_override(pg):
    s = pg.store
    judge = principal(pg, "editor-judge")
    task, claim = awaiting(pg, "edit-one", [judge])
    original = "Candidate edit-one"
    root = grant(pg, judge)
    done = s.gate(judge, operation(), task["id"], claim["epoch"], "edit", edited_result="Corrected result", reason="Fixed the figures")
    assert done["state"] == "done" and done["result"] == "Corrected result" and done["approvals_passed"] == 1
    assert done["original_result"] == original and done["result_author"] == pg.editor.principal
    assert done["result_edits"][0]["approver"] == judge.principal and done["result_edits"][0]["reason"] == "Fixed the figures"
    assert done["result_edits"][0]["original_sha256"] == sha(original) and done["result_edits"][0]["edited_sha256"] == sha("Corrected result")
    assert done["gate"]["decision"] == "edit" and done["gate"]["counted_as"] == "pass" and done["gate"]["edited"] is True
    assert s.get_task(pg.owner, task["id"])["result"] == "Corrected result"
    row = gate_rows(pg, task["id"])[0]
    assert (row["decision"], row["edited"], row["original_result"], row["edited_result"], row["reason"]) == (
        "pass", True, original, "Corrected result", "Fixed the figures")
    assert str(row["delegation_id"]) == root["id"]
    event = overridden_events(pg, task["id"])[0]
    assert event["agent"] == judge.principal and event["metadata"]["approver"] == judge.principal
    assert event["metadata"]["original_sha256"] == sha(original) and event["metadata"]["edited_sha256"] == sha("Corrected result")
    assert event["metadata"]["reason"] == "Fixed the figures" and event["metadata"]["epoch"] == claim["epoch"]
    assert "Corrected result" not in json.dumps(event["metadata"]) and original not in json.dumps(event["metadata"])
    assert reviewed_events(pg, task["id"])[0]["metadata"]["decision"] == "pass"
    assert any(row["event"] == "task.completed" for row in s.list_audit(pg.owner, task_id=task["id"]))


def test_edit_decision_validation_and_a_no_op_edit_is_refused(pg):
    s = pg.store
    task, claim = awaiting(pg, "edit-invalid", [pg.reviewer])
    for bad in (None, "", "   ", 5, "x" * 32001, "bad\x00nul"):
        expect("invalid", lambda bad=bad: s.gate(pg.owner, operation(), task["id"], claim["epoch"], "edit", edited_result=bad))
    expect("invalid", lambda: s.gate(pg.owner, operation(), task["id"], claim["epoch"], "pass", edited_result="Not allowed here"))
    expect("invalid", lambda: s.gate(pg.owner, operation(), task["id"], claim["epoch"], "fail", edited_result="Not allowed here"))
    expect("invalid", lambda: s.gate(pg.owner, operation(), task["id"], claim["epoch"], "edit", edited_result="Fine", reason=" "))
    expect("invalid", lambda: s.gate(pg.owner, operation(), task["id"], claim["epoch"], "edit", edited_result="Fine", reason=7))
    expect("invalid", lambda: s.gate(pg.owner, operation(), task["id"], claim["epoch"], "approve"))
    expect("invalid", lambda: s.gate(pg.owner, operation(), task["id"], claim["epoch"], "edit", edited_result="Candidate edit-invalid"))
    assert s.get_task(pg.owner, task["id"])["state"] == "awaiting_gate" and gate_rows(pg, task["id"]) == []


def test_edit_is_allowed_only_for_approvers_and_follows_the_self_approval_rules(pg):
    s = pg.store
    judge = principal(pg, "edit-undelegated")
    task, claim = awaiting(pg, "edit-auth", [judge])
    expect("delegation_required", lambda: s.gate(judge, operation(), task["id"], claim["epoch"], "edit", edited_result="Sneaky"))
    expect("not_found", lambda: s.gate(pg.reader, operation(), task["id"], claim["epoch"], "edit", edited_result="Sneaky"))
    expect("forbidden", lambda: s.gate(pg.editor, operation(), task["id"], claim["epoch"], "edit", edited_result="Sneaky"))
    assert s.get_task(pg.owner, task["id"])["result"] == "Candidate edit-auth"
    own = s.create_task_block(pg.owner, operation(), pg.page["id"], "Synthetic owner executes", [{"kind": "path", "value": pg.prefix + "/edit-self"}], [pg.reviewer.principal])
    own_claim = s.claim(pg.owner, operation(), own["id"])
    s.submit_result(pg.owner, operation(), own["id"], own_claim["epoch"], "Owner candidate")
    expect("forbidden", lambda: s.gate(pg.owner, operation(), own["id"], own_claim["epoch"], "edit", edited_result="Owner edited"))
    done = s.gate(pg.owner, operation(), own["id"], own_claim["epoch"], "edit", edited_result="Owner edited", self_approval=True)
    assert done["state"] == "done" and done["gate"]["authority"] == "root (self-approval)"
    assert gate_rows(pg, own["id"])[0]["self_approval"] is True


def test_edit_counts_as_one_pass_in_two_party_confirmation_and_the_second_approver_sees_the_edit(pg):
    s = pg.store
    one, two = principal(pg, "edit-two-a"), principal(pg, "edit-two-b")
    grant(pg, one)
    grant(pg, two)
    task, claim = awaiting(pg, "edit-two", [one, two], approvals_required=2)
    first = s.gate(one, operation(), task["id"], claim["epoch"], "edit", edited_result="First edit")
    assert first["state"] == "awaiting_gate" and first["approvals_passed"] == 1 and first["result"] == "First edit"
    expect("forbidden", lambda: s.gate(one, operation(), task["id"], claim["epoch"], "pass"))
    expect("forbidden", lambda: s.gate(one, operation(), task["id"], claim["epoch"], "edit", edited_result="Again"))
    second = s.gate(two, operation(), task["id"], claim["epoch"], "edit", edited_result="Second edit", reason="Tightened wording")
    assert second["state"] == "done" and second["approvals_passed"] == 2 and second["result"] == "Second edit"
    rows = gate_rows(pg, task["id"])
    assert [(row["edited_result"], row["original_result"]) for row in rows] == [("First edit", "Candidate edit-two"), ("Second edit", "First edit")]
    assert second["original_result"] == "Candidate edit-two" and [edit["approver"] for edit in second["result_edits"]] == [one.principal, two.principal]
    assert len(overridden_events(pg, task["id"])) == 2


def test_edit_replays_with_the_same_operation_id_and_a_different_edit_conflicts(pg):
    s = pg.store
    task, claim = awaiting(pg, "edit-replay", [pg.reviewer])
    op = operation()
    first = s.gate(pg.owner, op, task["id"], claim["epoch"], "edit", edited_result="Edited once", reason="why")
    again = s.gate(pg.owner, op, task["id"], claim["epoch"], "edit", edited_result="Edited once", reason="why")
    assert again["replay"] is True and again["gate"] == first["gate"] and len(gate_rows(pg, task["id"])) == 1
    assert again["result"] == "Edited once"
    expect("operation_conflict", lambda: s.gate(pg.owner, op, task["id"], claim["epoch"], "edit", edited_result="Edited twice", reason="why"))
    expect("operation_conflict", lambda: s.gate(pg.owner, op, task["id"], claim["epoch"], "edit", edited_result="Edited once", reason="other"))
    assert len(overridden_events(pg, task["id"])) == 1


def test_a_failed_gate_after_an_edit_cannot_keep_the_pass_and_edit_needs_the_current_epoch(pg):
    s = pg.store
    task, claim = awaiting(pg, "edit-stale", [pg.reviewer])
    expect("stale_claim", lambda: s.gate(pg.owner, operation(), task["id"], claim["epoch"] + 1, "edit", edited_result="Late"))
    failed = s.gate(pg.owner, operation(), task["id"], claim["epoch"], "fail", reason="Not good enough")
    assert failed["state"] == "queued" and failed["gate"]["decision"] == "fail" and failed["gate"]["edited"] is False
    assert gate_rows(pg, task["id"])[0]["reason"] == "Not good enough"


def test_gate_responses_show_the_approver_and_how_authority_was_obtained(pg):
    s = pg.store
    judge = principal(pg, "who-judge")
    task, claim = awaiting(pg, "who-root", [pg.reviewer])
    done = s.gate(pg.owner, operation(), task["id"], claim["epoch"], "pass")
    assert done["gate"] == {
        "decision": "pass", "counted_as": "pass", "epoch": claim["epoch"], "approver": pg.owner.principal,
        "approver_account": pg.owner.account, "approver_kind": "human", "root": True, "self_approval": False,
        "authority": "root", "delegation_id": None, "grantor": None, "edited": False, "approvals": 1,
        "approvals_required": 1, "complete": True}
    delegated, delegated_claim = awaiting(pg, "who-delegated", [judge])
    root = grant(pg, judge)
    done = s.gate(judge, operation(), delegated["id"], delegated_claim["epoch"], "pass")
    assert done["gate"]["authority"] == f"delegated by {pg.owner.principal} via {root['id']}"
    assert (done["gate"]["root"], done["gate"]["approver"], done["gate"]["grantor"], done["gate"]["delegation_id"]) == (
        False, judge.principal, pg.owner.principal, root["id"])
    own = s.create_task_block(pg.owner, operation(), pg.page["id"], "Synthetic who self", [{"kind": "path", "value": pg.prefix + "/who-self"}], [pg.reviewer.principal])
    own_claim = s.claim(pg.owner, operation(), own["id"])
    s.submit_result(pg.owner, operation(), own["id"], own_claim["epoch"], "Owner candidate")
    done = s.gate(pg.owner, operation(), own["id"], own_claim["epoch"], "pass", self_approval=True)
    assert done["gate"]["authority"] == "root (self-approval)" and done["gate"]["self_approval"] is True
    replay_op = operation()
    other, other_claim = awaiting(pg, "who-replay", [pg.reviewer])
    first = s.gate(pg.owner, replay_op, other["id"], other_claim["epoch"], "pass")
    assert s.gate(pg.owner, replay_op, other["id"], other_claim["epoch"], "pass")["gate"] == first["gate"]


def test_edit_rows_must_be_internally_consistent(pg):
    s = pg.store
    task, claim = awaiting(pg, "edit-constraint", [pg.reviewer])
    with s._transaction(write=True) as conn:
        for original, edited, edit_flag, decision in (("a", None, True, "pass"), (None, "b", True, "pass"), ("a", "b", False, "pass"),
                                                       ("a", "b", True, "fail"), (None, None, True, "pass")):
            with pytest.raises(psycopg.errors.CheckViolation):
                with conn.transaction():
                    conn.execute("""INSERT INTO baton.x_gate(id,task_id,epoch,decision,reviewer,reviewer_account,evidence,edited,original_result,edited_result)
                        VALUES (%s,%s,%s,%s,'r','a','[]',%s,%s,%s)""", (uuid.uuid4(), task["id"], claim["epoch"], decision, edit_flag, original, edited))
