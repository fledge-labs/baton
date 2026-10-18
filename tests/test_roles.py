# SPDX-License-Identifier: Apache-2.0
"""Role queues on actual PostgreSQL: role validation, derived roles, assignment authority, the role queue, claims by role,
role SLA notifications and the board panel.

Every identity, prompt and task is synthetic. Waiting time is moved with database statements instead of sleeping.
"""

import asyncio
import datetime
import json
import os
import pathlib
import threading
import uuid

import psycopg
import pytest
from psycopg import sql

from bridge import space_store
from bridge.space_store import (DEFAULT_ROLE_SLA_MINUTES, ESCALATION_LOCK_ID, SERVICE_ACTOR, Actor, StoreError, derive_role,
                                normalize_role_rules, normalize_role_sla, role_sla_minutes)
from test_bridge import api, op, shared_page  # noqa: F401  (api is a fixture)
from test_notify import events, execute, notes, query
from test_response_time import run_concurrently

PROMPT = "synthetic-role-prompt"


@pytest.fixture
def team(api):
    client, call, identities, tokens, registry, records = api
    space, page = shared_page(call, identities)
    return call, identities, space, page


def who(identities, label):
    item = identities[label]
    return Actor(item["agent"], item["account"], item["kind"])


def principal(identities, label):
    return identities[label]["agent"]


def submit(call, page, role=None, label="owner", scopes=None, **extra):
    if role is not None:
        extra["role"] = role
    if scopes is not None:
        extra["scopes"] = scopes
    return call(label, "submit_task", operation_id=op(), idempotency_key="role-" + uuid.uuid4().hex[:12], prompt=PROMPT,
                page_id=page["id"], **extra)


def path_scope(value):
    return [{"kind": "path", "value": value}]


def age(store, task_id, minutes):
    execute(store, "UPDATE baton.tasks SET created_at=clock_timestamp()-%s*interval '1 minute' WHERE id=%s", (minutes, task_id))


def assign(call, space, owner_label, target, role, expected=200):
    return call(owner_label, "assign_role", expected, operation_id=op(), space_id=space["id"], principal=target, role=role)


def audit(store, event, space_id=None, task_id=None):
    return query(store, """SELECT * FROM baton.x_audit WHERE event=%s AND (%s::uuid IS NULL OR space_id=%s::uuid)
        AND (%s::uuid IS NULL OR task_id=%s::uuid) ORDER BY time,id""", (event, space_id, space_id, task_id, task_id))


def sla_rows(store, task_id):
    return query(store, "SELECT recipient FROM baton.x_notify WHERE event='role.sla_breached' AND object_id=%s ORDER BY seq", (task_id,))


def task_column(store, task_id, name):
    # name is a literal chosen by this file, never request input.
    return query(store, f"SELECT {name} AS value FROM baton.tasks WHERE id=%s", (task_id,))[0]["value"]


def sweep(store):
    return store.escalate_overdue(SERVICE_ACTOR)


# --- validation -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("role", ["review.primary", "review.second", "review.docs", "test.engineer", "impl.long", "ops.duty",
                                  "maint", "a", "a" + "b" * 63, "triage", "x_y-z.0"])
def test_valid_role_names_are_accepted_on_submit(team, role):
    call, identities, space, page = team
    task = submit(call, page, role)
    assert task["role"] == role and task["role_source"] == "explicit"
    assert call("owner", "get_task", task_id=task["task_id"])["role"] == role


@pytest.mark.parametrize("role", ["", "Review", "1review", ".review", "-review", "_review", "a" * 65, "review primary", "review\n",
                                  "review/primary", "revié", "REVIEW.PRIMARY", "review*"])
def test_invalid_role_names_are_rejected_everywhere(team, role):
    call, identities, space, page = team
    error = call("owner", "submit_task", 422, operation_id=op(), idempotency_key="role-bad-" + uuid.uuid4().hex[:8], prompt=PROMPT,
                 page_id=page["id"], role=role)
    assert error["code"] == "invalid" and {"field": "role", "expected": space_store.ROLE_EXPECTED} in error["details"]
    assert role not in json.dumps(error) or role == ""
    task = submit(call, page)
    assert call("owner", "set_task_role", 422, operation_id=op(), task_id=task["task_id"], role=role)["code"] == "invalid"
    assert assign(call, space, "owner", principal(identities, "worker"), role, 422)["code"] == "invalid"
    assert call("owner", "ready_for_role", 422, role=role)["code"] == "invalid"
    assert call("owner", "set_role_rules", 422, operation_id=op(), space_id=space["id"],
                rules=[{"role": role, "match": {}}])["code"] == "invalid"


def test_role_must_be_a_string_and_rejects_unknown_fields(team):
    call, identities, space, page = team
    for value in (7, True, ["review.primary"], {"role": "x"}):
        assert call("owner", "submit_task", 422, operation_id=op(), idempotency_key="role-type-" + uuid.uuid4().hex[:8], prompt=PROMPT,
                    page_id=page["id"], role=value)["code"] == "invalid"
    assert call("owner", "assign_role", 422, operation_id=op(), space_id=space["id"], principal=principal(identities, "worker"),
                role="maint", actor={"principal": "x"})["code"] == "invalid"


def test_role_is_part_of_the_submit_request_digest_only_when_given(team):
    call, identities, space, page = team
    key = "role-digest-" + uuid.uuid4().hex[:8]
    base = {"idempotency_key": key, "prompt": PROMPT, "page_id": page["id"]}
    first = call("owner", "submit_task", operation_id=op(), **base)
    assert first["role"] is None and first["role_source"] is None
    again = call("owner", "submit_task", operation_id=op(), **base)
    assert again["task_id"] == first["task_id"] and again["replay"] is True
    assert call("owner", "submit_task", 409, operation_id=op(), role="maint", **base)["code"] == "operation_conflict"
    keyed = {**base, "idempotency_key": key + "-b"}
    second = call("owner", "submit_task", operation_id=op(), role="maint", **keyed)
    assert call("owner", "submit_task", operation_id=op(), role="maint", **keyed)["task_id"] == second["task_id"]
    assert call("owner", "submit_task", 409, operation_id=op(), role="review.docs", **keyed)["code"] == "operation_conflict"


def test_create_task_block_accepts_a_role(team):
    call, identities, space, page = team
    task = call("owner", "create_task_block", operation_id=op(), page_id=page["id"], prompt=PROMPT,
                reviewers=[principal(identities, "reviewer")], role="test.engineer")
    assert task["role"] == "test.engineer" and task["role_source"] == "explicit"


# --- explicit role changes --------------------------------------------------------------------------------------------

def test_owner_and_root_set_the_role_of_a_queued_task_and_every_change_is_audited(team, pg_store, approver):
    call, identities, space, page = team
    task = submit(call, page, label="reviewer")
    task_id = task["task_id"]
    assert task["role"] is None
    set_by_owner = call("reviewer", "set_task_role", operation_id=op(), task_id=task_id, role="review.docs")
    assert set_by_owner["role"] == "review.docs" and set_by_owner["role_source"] == "explicit"
    changed = call("owner", "set_task_role", operation_id=op(), task_id=task_id, role="review.second")
    assert changed["role"] == "review.second"
    rows = audit(pg_store, "task.role_set", task_id=task_id)
    assert [(row["metadata"].get("role"), row["metadata"].get("from_role"), row["agent"]) for row in rows] == [
        ("review.docs", None, principal(identities, "reviewer")), ("review.second", "review.docs", principal(identities, "owner"))]
    # Setting the same role again changes nothing and writes nothing.
    call("owner", "set_task_role", operation_id=op(), task_id=task_id, role="review.second")
    assert len(audit(pg_store, "task.role_set", task_id=task_id)) == 2
    cleared = call("owner", "set_task_role", operation_id=op(), task_id=task_id, role=None)
    assert cleared["role"] is None and cleared["role_source"] is None
    assert len(audit(pg_store, "task.role_cleared", task_id=task_id)) == 1


def test_only_the_task_owner_or_root_may_set_a_role_and_only_while_queued(team, pg_store, approver):
    call, identities, space, page = team
    approver(pg_store, identities["reviewer"], by=identities["owner"])
    task = submit(call, page, label="worker")
    task_id = task["task_id"]
    for label in ("reviewer", "replacement"):
        assert call(label, "set_task_role", 403, operation_id=op(), task_id=task_id, role="maint")["code"] == "forbidden"
    assert call("stranger", "set_task_role", 404, operation_id=op(), task_id=task_id, role="maint")["code"] == "not_found"
    assert call("owner", "set_task_role", operation_id=op(), task_id=task_id, role="maint")["role"] == "maint"
    assert assign(call, space, "owner", principal(identities, "worker"), "maint")["changed"] is True
    claimed = call("worker", "claim", operation_id=op(), task_id=task_id)
    assert call("owner", "set_task_role", 409, operation_id=op(), task_id=task_id, role="ops.duty")["code"] == "stale_claim"
    call("worker", "submit_result", operation_id=op(), task_id=task_id, epoch=claimed["epoch"], result="done")
    assert call("owner", "set_task_role", 409, operation_id=op(), task_id=task_id, role="ops.duty")["code"] == "stale_claim"
    assert call("owner", "get_task", task_id=task_id)["role"] == "maint"


def test_an_expired_claim_leaves_the_task_queued_so_its_role_can_change(team, pg_store):
    call, identities, space, page = team
    assign(call, space, "owner", principal(identities, "worker"), "maint")
    task_id = submit(call, page, "maint")["task_id"]
    call("worker", "claim", operation_id=op(), task_id=task_id, lease_seconds=60)
    execute(pg_store, "UPDATE baton.x_claim SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE task_id=%s", (task_id,))
    assert call("owner", "set_task_role", operation_id=op(), task_id=task_id, role="ops.duty")["role"] == "ops.duty"


# --- role rules -------------------------------------------------------------------------------------------------------

RULES = [
    {"role": "review.docs", "match": {"scope_prefix": "docs"}},
    {"role": "test.engineer", "match": {"path_glob": "tests/**/test_*.py"}},
    {"role": "ops.duty", "match": {"kind": "migration"}},
    {"role": "impl.long", "match": {"kind": "prep"}},
    {"role": "review.primary", "match": {"scope_prefix": "src", "kind": "path"}},
    {"role": "maint", "match": {}},
]


def test_rules_are_validated_and_normalized():
    assert normalize_role_rules([]) == []
    assert normalize_role_rules([{"role": "review.docs", "match": {"scope_prefix": "docs/"}}]) == [
        {"role": "review.docs", "match": {"scope_prefix": "docs"}}]
    for bad in (None, {}, "x", [1], [{"role": "maint"}], [{"match": {}}], [{"role": "maint", "match": []}],
                [{"role": "maint", "match": {"unknown": "x"}}], [{"role": "maint", "match": {"scope_prefix": ""}}],
                [{"role": "maint", "match": {"scope_prefix": "/abs"}}], [{"role": "maint", "match": {"scope_prefix": "a/../b"}}],
                [{"role": "maint", "match": {"scope_prefix": 3}}], [{"role": "maint", "match": {"path_glob": "../x"}}],
                [{"role": "maint", "match": {"path_glob": "/x"}}], [{"role": "maint", "match": {"path_glob": ""}}],
                [{"role": "maint", "match": {"path_glob": "a b"}}], [{"role": "maint", "match": {"kind": "repo"}}],
                [{"role": "Maint", "match": {}}], [{"role": "maint", "match": {}, "extra": 1}],
                [{"role": "maint", "match": {}}] * (space_store.ROLE_RULES_MAX + 1)):
        with pytest.raises(StoreError) as error:
            normalize_role_rules(bad)
        assert error.value.code == "invalid" and error.value.details[0]["field"] == "rules"


def test_first_matching_rule_wins_and_matching_semantics():
    path = lambda value: [{"kind": "path", "value": value}]  # noqa: E731
    assert derive_role(RULES, path("docs"), "construction") == "review.docs"
    assert derive_role(RULES, path("docs/design/a.md"), "construction") == "review.docs"
    assert derive_role(RULES, path("docsy"), "construction") == "maint", "a prefix ends on a path boundary"
    assert derive_role(RULES, path("tests/unit/deep/test_a.py"), "construction") == "test.engineer"
    assert derive_role(RULES, path("tests/test_a.py"), "construction") == "test.engineer", "** may match no directory"
    assert derive_role(RULES, path("tests/unit/helper.py"), "construction") == "maint"
    assert derive_role(RULES, [{"kind": "migration", "value": "7"}], "construction") == "ops.duty"
    assert derive_role(RULES, [{"kind": "none"}], "prep") == "impl.long", "kind may name the task kind"
    assert derive_role(RULES, path("src/app/x.py"), "construction") == "review.primary"
    assert derive_role(RULES, path("docs"), "prep") == "review.docs", "order decides, not specificity"
    assert derive_role(RULES, path(""), "construction") == "maint", "the repository root is under no prefix"
    assert derive_role(RULES[:-1], path("lib"), "construction") is None
    assert derive_role([], path("docs"), "construction") is None and derive_role(None, path("docs"), "construction") is None
    star = [{"role": "maint", "match": {"path_glob": "src/*.py"}}]
    assert derive_role(star, path("src/a.py"), "construction") == "maint"
    assert derive_role(star, path("src/pkg/a.py"), "construction") is None, "* stays inside one segment"
    assert derive_role([{"role": "maint", "match": {"path_glob": "src/?.py"}}], path("src/ab.py"), "construction") is None
    both = [{"role": "maint", "match": {"scope_prefix": "docs", "kind": "migration"}}]
    assert derive_role(both, path("docs"), "construction") is None, "every key must hold"
    dir_glob = [{"role": "maint", "match": {"path_glob": "docs/**"}}]
    assert derive_role(dir_glob, path("docs"), "construction") == "maint"
    assert derive_role(dir_glob, path("docs/a/b.md"), "construction") == "maint"


def test_rules_derive_the_role_at_creation_and_record_the_source(team, pg_store):
    call, identities, space, page = team
    before = submit(call, page, scopes=path_scope("docs/early"))
    assert before["role"] is None
    result = call("owner", "set_role_rules", operation_id=op(), space_id=space["id"], rules=RULES)
    assert result["rules"] == RULES
    docs = submit(call, page, scopes=path_scope("docs/guide"))
    assert (docs["role"], docs["role_source"]) == ("review.docs", "derived")
    assert call("owner", "get_task", task_id=docs["task_id"])["role_source"] == "derived"
    assert submit(call, page, scopes=path_scope("tests/api/test_x.py"))["role"] == "test.engineer"
    assert submit(call, page, scopes=[{"kind": "migration", "value": "12"}])["role"] == "ops.duty"
    assert submit(call, page, task_kind="prep")["role"] == "impl.long"
    assert submit(call, page, scopes=path_scope("src/core"))["role"] == "review.primary"
    assert submit(call, page, scopes=path_scope("lib/x"))["role"] == "maint"
    block = call("owner", "create_task_block", operation_id=op(), page_id=page["id"], prompt=PROMPT, reviewers=[principal(identities, "reviewer")],
                 scopes=path_scope("docs/x"))
    assert (block["role"], block["role_source"]) == ("review.docs", "derived")
    explicit = submit(call, page, "ops.duty", scopes=path_scope("docs/guide"))
    assert (explicit["role"], explicit["role_source"]) == ("ops.duty", "explicit"), "an explicit role is never derived over"
    assert call("owner", "get_task", task_id=before["task_id"])["role"] is None, "existing tasks are not rewritten"
    derived = audit(pg_store, "task.role_derived", task_id=docs["task_id"])
    assert len(derived) == 1 and (derived[0]["metadata"]["role"], derived[0]["metadata"]["role_source"]) == ("review.docs", "derived")
    assert len(audit(pg_store, "task.role_set", task_id=explicit["task_id"])) == 1


def test_rules_are_per_space_and_can_be_replaced_or_emptied(team, pg_store):
    call, identities, space, page = team
    other = call("owner", "create_space", operation_id=op(), name="Other")
    other_page = call("owner", "create_page", operation_id=op(), space_id=other["id"], title="Work")
    call("owner", "set_role_rules", operation_id=op(), space_id=space["id"], rules=[{"role": "maint", "match": {}}])
    assert submit(call, page)["role"] == "maint"
    assert submit(call, other_page)["role"] is None
    call("owner", "set_role_rules", operation_id=op(), space_id=space["id"], rules=[])
    assert submit(call, page)["role"] is None
    assert call("owner", "list_roles", space_id=space["id"])["rules"] == []


def test_only_the_space_root_sets_rules_and_sla_and_both_are_audited(team, pg_store, approver):
    call, identities, space, page = team
    approver(pg_store, identities["reviewer"], by=identities["owner"], may_delegate=True)
    for label, code in (("reviewer", 403), ("worker", 403), ("stranger", 404)):
        assert call(label, "set_role_rules", code, operation_id=op(), space_id=space["id"], rules=RULES)["code"] in {"forbidden", "not_found"}
        assert call(label, "set_role_sla", code, operation_id=op(), space_id=space["id"], sla_minutes={"maint": 5})["code"] in {"forbidden", "not_found"}
    assert audit(pg_store, "role_rules.set", space["id"]) == [] and audit(pg_store, "role_sla.set", space["id"]) == []
    operation = op()
    call("owner", "set_role_rules", operation_id=operation, space_id=space["id"], rules=RULES)
    replay = call("owner", "set_role_rules", operation_id=operation, space_id=space["id"], rules=RULES)
    assert replay["replay"] is True
    call("owner", "set_role_sla", operation_id=op(), space_id=space["id"], sla_minutes={"maint": 5})
    rules_audit = audit(pg_store, "role_rules.set", space["id"])
    assert len(rules_audit) == 1 and rules_audit[0]["agent"] == principal(identities, "owner")
    assert rules_audit[0]["metadata"]["count"] == len(RULES) and "docs" not in json.dumps(rules_audit[0]["metadata"])
    assert len(audit(pg_store, "role_sla.set", space["id"])) == 1


# --- assignment authority ---------------------------------------------------------------------------------------------

def test_root_assigns_and_revokes_roles_with_audited_authority(team, pg_store):
    call, identities, space, page = team
    worker = principal(identities, "worker")
    held = assign(call, space, "owner", worker, "review.primary")
    assert held["changed"] is True and held["principal"] == worker and held["authority"] == "root" and held["kind"] == "agent"
    assert assign(call, space, "owner", worker, "review.primary")["changed"] is False
    assign(call, space, "owner", worker, "test.engineer")
    assign(call, space, "owner", principal(identities, "replacement"), "review.primary")
    listing = call("owner", "list_roles", space_id=space["id"])
    by_role = {entry["role"]: [holder["principal"] for holder in entry["holders"]] for entry in listing["roles"]}
    assert by_role == {"review.primary": [worker, principal(identities, "replacement")], "test.engineer": [worker]}
    assigned = audit(pg_store, "role.assigned", space["id"])
    assert len(assigned) == 3, "the repeated assignment changed nothing and wrote nothing"
    first = assigned[0]
    assert first["metadata"]["authority"] == "root" and first["metadata"]["root"] is True and first["metadata"]["role"] == "review.primary"
    assert first["metadata"]["target_principal"] == worker and first["agent"] == principal(identities, "owner")
    revoked = call("owner", "revoke_role", operation_id=op(), space_id=space["id"], principal=worker, role="review.primary")
    assert revoked["changed"] is True
    again = call("owner", "revoke_role", operation_id=op(), space_id=space["id"], principal=worker, role="review.primary")
    assert again["changed"] is False
    log = audit(pg_store, "role.revoked", space["id"])
    assert len(log) == 1 and log[0]["metadata"]["authority"] == "root" and log[0]["metadata"]["target_principal"] == worker
    by_role = {entry["role"]: [h["principal"] for h in entry["holders"]] for entry in call("owner", "list_roles", space_id=space["id"])["roles"]}
    assert by_role["review.primary"] == [principal(identities, "replacement")] and by_role["test.engineer"] == [worker]


def test_a_delegated_approver_assigns_with_the_delegation_in_the_audit(team, pg_store, approver):
    call, identities, space, page = team
    delegation = approver(pg_store, identities["reviewer"], by=identities["owner"])
    held = assign(call, space, "reviewer", principal(identities, "worker"), "review.docs")
    assert held["authority"] == f'delegated by {principal(identities, "owner")} via {delegation["id"]}'
    record = audit(pg_store, "role.assigned", space["id"])[0]
    assert record["agent"] == principal(identities, "reviewer")
    assert record["metadata"]["authority"] == held["authority"] and record["metadata"]["root"] is False
    assert record["metadata"]["delegation_id"] == delegation["id"] and record["metadata"]["grantor"] == principal(identities, "owner")
    assert record["metadata"]["chain"] == [delegation["id"]]
    call("reviewer", "revoke_role", operation_id=op(), space_id=space["id"], principal=principal(identities, "worker"), role="review.docs")
    revoked = audit(pg_store, "role.revoked", space["id"])[0]
    assert revoked["metadata"]["authority"] == held["authority"] and revoked["metadata"]["delegation_id"] == delegation["id"]


def test_a_chained_delegation_is_honoured_and_named_by_its_leaf(team, pg_store, approver):
    call, identities, space, page = team
    root_link = approver(pg_store, identities["reviewer"], by=identities["owner"], may_delegate=True)
    child = pg_store.delegation_create(who(identities, "reviewer"), op(), who(identities, "worker"), reason="role chain test")
    held = assign(call, space, "worker", principal(identities, "replacement"), "impl.long")
    assert held["authority"] == f'delegated by {principal(identities, "reviewer")} via {child["id"]}'
    record = audit(pg_store, "role.assigned", space["id"])[0]
    assert record["metadata"]["chain"] == [child["id"], root_link["id"]]
    # Revoking the parent disables everything below it, for roles exactly as for gates.
    pg_store.delegation_revoke(who(identities, "owner"), op(), root_link["id"])
    assert assign(call, space, "worker", principal(identities, "stranger"), "impl.long", 403)["code"] == "delegation_required"
    assert assign(call, space, "reviewer", principal(identities, "stranger"), "impl.long", 403)["code"] == "delegation_required"


def test_non_approvers_are_refused_and_nothing_is_written(team, pg_store, approver):
    call, identities, space, page = team
    approver(pg_store, identities["reviewer"], by=identities["owner"])
    target = principal(identities, "replacement")
    assert assign(call, space, "worker", target, "maint", 403)["code"] == "delegation_required"
    assert assign(call, space, "replacement", target, "maint", 403)["code"] == "delegation_required"
    assert assign(call, space, "stranger", target, "maint", 404)["code"] == "not_found"
    assert call("worker", "revoke_role", 403, operation_id=op(), space_id=space["id"], principal=target, role="maint")["code"] == "delegation_required"
    assert audit(pg_store, "role.assigned", space["id"]) == []
    assert query(pg_store, "SELECT count(*) AS n FROM baton.x_role_holder")[0]["n"] == 0
    assign(call, space, "owner", target, "maint")
    assert call("worker", "revoke_role", 403, operation_id=op(), space_id=space["id"], principal=target, role="maint")["code"] == "delegation_required"
    assert query(pg_store, "SELECT count(*) AS n FROM baton.x_role_holder")[0]["n"] == 1
    assert call("stranger", "revoke_role", 404, operation_id=op(), space_id=space["id"], principal=target, role="maint")["code"] == "not_found"


def test_an_approver_cannot_grant_a_role_to_itself_but_root_can(team, pg_store, approver):
    call, identities, space, page = team
    approver(pg_store, identities["reviewer"], by=identities["owner"], may_delegate=True)
    assert assign(call, space, "reviewer", principal(identities, "reviewer"), "review.primary", 403)["code"] == "forbidden"
    assert query(pg_store, "SELECT count(*) AS n FROM baton.x_role_holder")[0]["n"] == 0
    assert audit(pg_store, "role.assigned", space["id"]) == []
    assert assign(call, space, "reviewer", principal(identities, "worker"), "review.primary")["changed"] is True
    held = assign(call, space, "owner", principal(identities, "owner"), "review.primary")
    assert held["principal"] == principal(identities, "owner") and held["authority"] == "root"


def test_delegations_that_do_not_cover_the_space_or_have_lapsed_do_not_authorize(team, pg_store, approver):
    call, identities, space, page = team
    other = call("owner", "create_space", operation_id=op(), name="Elsewhere")
    approver(pg_store, identities["reviewer"], by=identities["owner"], space_id=other["id"])
    target = principal(identities, "worker")
    assert assign(call, space, "reviewer", target, "maint", 403)["code"] == "delegation_required"
    assert call("reviewer", "assign_role", 404, operation_id=op(), space_id=other["id"], principal=target, role="maint")["code"] == "not_found", \
        "an approver still needs access to the space"
    call("owner", "share", operation_id=op(), resource_type="space", resource_id=other["id"], subject_type="person",
         subject_id=identities["reviewer"]["account"], role="edit")
    assert assign(call, {"id": other["id"]}, "reviewer", target, "maint")["authority"].startswith("delegated by ")
    expires = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)).isoformat()
    expiring = approver(pg_store, identities["worker"], by=identities["owner"], space_id=space["id"], expires_at=expires)
    assert assign(call, space, "worker", principal(identities, "replacement"), "maint")["changed"] is True
    pg_store.delegation_revoke(who(identities, "owner"), op(), expiring["id"])
    assert assign(call, space, "worker", principal(identities, "stranger"), "maint", 403)["code"] == "delegation_required"


def test_replaying_an_assignment_rechecks_authority(team, pg_store, approver):
    call, identities, space, page = team
    delegation = approver(pg_store, identities["reviewer"], by=identities["owner"])
    operation = op()
    body = {"operation_id": operation, "space_id": space["id"], "principal": principal(identities, "worker"), "role": "maint"}
    assert call("reviewer", "assign_role", **body)["changed"] is True
    assert call("reviewer", "assign_role", **body)["replay"] is True
    pg_store.delegation_revoke(who(identities, "owner"), op(), delegation["id"])
    assert call("reviewer", "assign_role", 403, **body)["code"] == "delegation_required"
    assert call("reviewer", "assign_role", 403, **{**body, "role": "ops.duty"})["code"] == "delegation_required"


def test_assign_role_needs_a_registered_principal_and_never_the_service(team, pg_store):
    call, identities, space, page = team
    error = assign(call, space, "owner", "nobody-registered-" + uuid.uuid4().hex[:8], "maint", 422)
    assert error["details"] == [{"field": "principal", "expected": "a principal registered in Baton"}]
    with pytest.raises(StoreError) as refused:
        pg_store.assign_role(who(identities, "owner"), op(), space["id"], SERVICE_ACTOR, "maint")
    assert refused.value.code == "invalid"
    assert call("owner", "revoke_role", 422, operation_id=op(), space_id=space["id"], principal="bad principal", role="maint")["code"] == "invalid"


# --- the role queue ---------------------------------------------------------------------------------------------------

def test_ready_for_role_filters_by_role_assignment_scope_and_age(team, pg_store):
    call, identities, space, page = team
    worker, replacement = identities["worker"]["agent"], identities["replacement"]["agent"]
    assign(call, space, "owner", worker, "review.primary")
    assign(call, space, "owner", replacement, "review.second")
    newest = submit(call, page, "review.primary", scopes=path_scope("src/q/newest"))["task_id"]
    oldest = submit(call, page, "review.primary", scopes=path_scope("src/q/oldest"))["task_id"]
    for_me = submit(call, page, "review.primary", scopes=path_scope("src/q/mine"), assignee_principal=worker)["task_id"]
    for_other = submit(call, page, "review.primary", scopes=path_scope("src/q/other"), assignee_principal=replacement)["task_id"]
    second_role = submit(call, page, "review.second", scopes=path_scope("src/q/second"))["task_id"]
    plain = submit(call, page, scopes=path_scope("src/q/plain"))["task_id"]
    age(pg_store, oldest, 50)
    age(pg_store, for_me, 30)
    age(pg_store, newest, 10)

    ready = call("worker", "ready_for_role", role="review.primary")
    assert [task["task_id"] for task in ready] == [oldest, for_me, newest], "oldest first; the other principal's task is absent"
    assert all(task["role"] == "review.primary" for task in ready)
    assert [task["task_id"] for task in call("worker", "ready_for_role", role="review.primary", limit=2)] == [oldest, for_me]
    assert call("worker", "ready_for_role", role="review.second") == [], "worker does not hold review.second"
    assert [task["task_id"] for task in call("replacement", "ready_for_role", role="review.second")] == [second_role]
    assert [task["task_id"] for task in call("replacement", "ready_for_role", role="review.primary")] == [for_other], \
        "without the role only the task assigned to the caller is listed"
    assert call("worker", "ready_for_role", role="nonexistent.role") == []

    plain_ready = [task["task_id"] for task in call("worker", "ready")]
    assert plain in plain_ready and second_role not in plain_ready and for_other not in plain_ready
    assert {oldest, for_me, newest} <= set(plain_ready), "ready lists what the caller can claim, role tasks included"
    assert call("stranger", "ready_for_role", role="review.primary") == []


def test_ready_for_role_drops_tasks_that_conflict_with_a_live_claim_and_returns_them_after(team, pg_store):
    call, identities, space, page = team
    for label in ("worker", "replacement"):
        assign(call, space, "owner", principal(identities, label), "review.primary")
    blocker = submit(call, page, scopes=path_scope("src/conflict"), assignee_principal=principal(identities, "replacement"))
    clear = submit(call, page, "review.primary", scopes=path_scope("docs/free"))["task_id"]
    clashing = submit(call, page, "review.primary", scopes=path_scope("src/conflict/deep"))["task_id"]
    age(pg_store, clashing, 20)
    assert [task["task_id"] for task in call("worker", "ready_for_role", role="review.primary")] == [clashing, clear]
    claim = call("replacement", "claim", operation_id=op(), task_id=blocker["task_id"])
    assert [task["task_id"] for task in call("worker", "ready_for_role", role="review.primary")] == [clear]
    assert call("worker", "claim", 409, operation_id=op(), task_id=clashing)["code"] == "scope_conflict"
    call("replacement", "submit_result", operation_id=op(), task_id=blocker["task_id"], epoch=claim["epoch"], result="done")
    assert [task["task_id"] for task in call("worker", "ready_for_role", role="review.primary")] == [clashing, clear]


def test_ready_for_role_respects_dependencies_work_face_and_page_access(team, pg_store):
    call, identities, space, page = team
    assign(call, space, "owner", principal(identities, "worker"), "review.primary")
    prerequisite = submit(call, page, scopes=path_scope("docs/pre"))["task_id"]
    blocked = submit(call, page, "review.primary", depends_on=[prerequisite])["task_id"]
    incomplete = submit(call, page, "review.primary", require_work_face=True)["task_id"]
    free = submit(call, page, "review.primary")["task_id"]
    assert [task["task_id"] for task in call("worker", "ready_for_role", role="review.primary")] == [free]
    assert call("worker", "claim", 409, operation_id=op(), task_id=blocked)["code"] == "dependency_blocked"
    assert call("worker", "claim", 409, operation_id=op(), task_id=incomplete)["code"] == "work_face_incomplete"
    call("owner", "share", operation_id=op(), resource_type="space", resource_id=space["id"], subject_type="person",
         subject_id=identities["worker"]["account"], role="view")
    assert call("worker", "ready_for_role", role="review.primary") == [], "view is not enough to claim"


# --- claiming by role -------------------------------------------------------------------------------------------------

def test_claim_of_an_unassigned_role_task_requires_the_role(team, pg_store):
    call, identities, space, page = team
    task_id = submit(call, page, "review.primary")["task_id"]
    for label in ("worker", "replacement", "owner", "reviewer"):
        assert call(label, "claim", 403, operation_id=op(), task_id=task_id)["code"] == "forbidden", label
    assert call("owner", "get_task", task_id=task_id)["state"] == "queued"
    assert call("stranger", "claim", 404, operation_id=op(), task_id=task_id)["code"] == "not_found"
    assert audit(pg_store, "task.claimed", task_id=task_id) == []

    assign(call, space, "owner", principal(identities, "worker"), "review.second")
    assert call("worker", "claim", 403, operation_id=op(), task_id=task_id)["code"] == "forbidden", "another role does not help"
    assign(call, space, "owner", principal(identities, "worker"), "review.primary")
    claimed = call("worker", "claim", operation_id=op(), task_id=task_id)
    assert claimed["state"] == "claimed" and claimed["holder"] == principal(identities, "worker") and claimed["epoch"] == 1
    assert call("replacement", "claim", 403, operation_id=op(), task_id=task_id)["code"] == "forbidden"
    record = audit(pg_store, "task.claimed", task_id=task_id)
    assert len(record) == 1 and record[0]["metadata"]["role"] == "review.primary" and record[0]["agent"] == principal(identities, "worker")


def test_claiming_requires_the_role_in_the_tasks_own_space_and_only_while_held(team, pg_store):
    call, identities, space, page = team
    other = call("owner", "create_space", operation_id=op(), name="Second space")
    other_page = call("owner", "create_page", operation_id=op(), space_id=other["id"], title="Work")
    call("owner", "share", operation_id=op(), resource_type="space", resource_id=other["id"], subject_type="person",
         subject_id=identities["worker"]["account"], role="edit")
    assign(call, {"id": other["id"]}, "owner", principal(identities, "worker"), "review.primary")
    task_id = submit(call, page, "review.primary")["task_id"]
    assert call("worker", "claim", 403, operation_id=op(), task_id=task_id)["code"] == "forbidden", "held elsewhere does not count"
    assert [task["task_id"] for task in call("worker", "ready_for_role", role="review.primary")] == []
    other_task = submit(call, other_page, "review.primary")["task_id"]
    assert call("worker", "claim", operation_id=op(), task_id=other_task)["state"] == "claimed"

    assign(call, space, "owner", principal(identities, "worker"), "review.primary")
    call("owner", "revoke_role", operation_id=op(), space_id=space["id"], principal=principal(identities, "worker"), role="review.primary")
    assert call("worker", "claim", 403, operation_id=op(), task_id=task_id)["code"] == "forbidden", "a revoked role no longer claims"


def test_a_task_assigned_to_the_caller_is_claimable_without_holding_the_role(team):
    call, identities, space, page = team
    task_id = submit(call, page, "review.primary", assignee_principal=principal(identities, "worker"))["task_id"]
    assert call("replacement", "claim", 403, operation_id=op(), task_id=task_id)["code"] == "forbidden"
    assert call("worker", "claim", operation_id=op(), task_id=task_id)["state"] == "claimed"


def test_assigning_a_role_task_to_a_principal_takes_it_out_of_the_queue(team):
    call, identities, space, page = team
    for label in ("worker", "replacement"):
        assign(call, space, "owner", principal(identities, label), "review.primary")
    task_id = submit(call, page, "review.primary")["task_id"]
    assert [task["task_id"] for task in call("replacement", "ready_for_role", role="review.primary")] == [task_id]
    call("owner", "assign_task", operation_id=op(), task_id=task_id, target_principal=principal(identities, "worker"))
    assert call("replacement", "ready_for_role", role="review.primary") == []
    assert call("replacement", "claim", 403, operation_id=op(), task_id=task_id)["code"] == "forbidden"
    assert [task["task_id"] for task in call("worker", "ready_for_role", role="review.primary")] == [task_id]


def test_claim_replay_still_returns_the_claim(team):
    call, identities, space, page = team
    assign(call, space, "owner", principal(identities, "worker"), "review.primary")
    task_id = submit(call, page, "review.primary")["task_id"]
    operation = op()
    first = call("worker", "claim", operation_id=operation, task_id=task_id)
    again = call("worker", "claim", operation_id=operation, task_id=task_id)
    assert again["replay"] is True and again["epoch"] == first["epoch"]


def test_two_concurrent_claimers_of_a_role_task_produce_exactly_one_winner(team, pg_store):
    call, identities, space, page = team
    contenders = [who(identities, "worker"), who(identities, "replacement")]
    for contender in contenders:
        assign(call, space, "owner", contender.principal, "review.primary")
    for round_number in range(3):
        task_id = submit(call, page, "review.primary", scopes=path_scope(f"src/race/{round_number}"))["task_id"]
        barrier = threading.Barrier(len(contenders))
        outcomes = []

        def attempt(contender):
            barrier.wait(timeout=10)
            try:
                outcomes.append(("won", contender.principal, pg_store.claim(contender, op(), task_id)))
            except StoreError as exc:
                outcomes.append(("lost", contender.principal, exc.code))

        threads = [threading.Thread(target=attempt, args=(contender,)) for contender in contenders]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        assert sorted(outcome[0] for outcome in outcomes) == ["lost", "won"], outcomes
        won = next(outcome for outcome in outcomes if outcome[0] == "won")
        assert next(outcome for outcome in outcomes if outcome[0] == "lost")[2] == "scope_conflict"
        assert won[2]["holder"] == won[1] and won[2]["epoch"] == 1
        holders = query(pg_store, "SELECT holder,epoch FROM baton.x_claim WHERE task_id=%s", (task_id,))
        assert [(row["holder"], row["epoch"]) for row in holders] == [(won[1], 1)]
        assert len(audit(pg_store, "task.claimed", task_id=task_id)) == 1


def test_many_claimers_and_many_role_tasks_never_double_claim(team, pg_store):
    call, identities, space, page = team
    holders = [who(identities, "worker"), who(identities, "replacement")]
    for holder in holders:
        assign(call, space, "owner", holder.principal, "impl.long")
    tasks = [submit(call, page, "impl.long", scopes=path_scope(f"src/many/{index}"))["task_id"] for index in range(4)]
    barrier = threading.Barrier(len(holders))
    results, errors = [], []

    def claim_all(holder):
        won = []
        try:
            barrier.wait(timeout=10)
            for task_id in tasks:
                try:
                    pg_store.claim(holder, op(), task_id)
                    won.append(task_id)
                except StoreError as exc:
                    assert exc.code == "scope_conflict"
        except Exception as exc:  # surfaced below so a failure is never hidden in a thread
            errors.append(exc)
        results.append(won)

    threads = [threading.Thread(target=claim_all, args=(holder,)) for holder in holders]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not errors, errors
    winners = [task for result in results for task in result]
    assert sorted(winners) == sorted(tasks), "every task is claimed exactly once"
    assert len(winners) == len(set(winners))


# --- SLA --------------------------------------------------------------------------------------------------------------

def test_default_sla_map_and_matching():
    assert DEFAULT_ROLE_SLA_MINUTES == {"review.*": 30, "triage": 15}
    sla = dict(DEFAULT_ROLE_SLA_MINUTES)
    assert role_sla_minutes(sla, "review.primary") == 30 and role_sla_minutes(sla, "review.docs") == 30
    assert role_sla_minutes(sla, "triage") == 15
    for role in ("review", "reviewer", "triage.extra", "impl.long", "maint", "test.engineer", "ops.duty"):
        assert role_sla_minutes(sla, role) is None
    custom = {"review.*": 30, "review.docs": 90, "review.a.*": 10}
    assert role_sla_minutes(custom, "review.docs") == 90, "an exact entry beats a wildcard"
    assert role_sla_minutes(custom, "review.a.b") == 10, "the longest wildcard wins"
    assert role_sla_minutes(custom, "review.z") == 30


def test_sla_map_validation():
    assert normalize_role_sla(None) is None and normalize_role_sla({}) == {}
    assert normalize_role_sla({"review.*": 30, "maint": 1}) == {"maint": 1, "review.*": 30}
    for bad in ([], "x", {"Review": 5}, {"review.*": 0}, {"review.*": 525601}, {"review.*": "5"}, {"review.*": True}, {"review.*": 1.5},
                {"*": 5}, {"review*": 5}, {"review.**": 5}, {"": 5}, {f"r{index}": 5 for index in range(space_store.ROLE_SLA_ENTRIES + 1)}):
        with pytest.raises(StoreError) as error:
            normalize_role_sla(bad)
        assert error.value.code == "invalid"


def test_sla_breach_notifies_root_and_approvers_once_and_not_repeatedly(team, pg_store, approver):
    call, identities, space, page = team
    approver(pg_store, identities["reviewer"], by=identities["owner"])
    task_id = submit(call, page, "review.primary", scopes=path_scope("src/sla/one"))["task_id"]
    age(pg_store, task_id, 29)
    assert sweep(pg_store)["role_sla"] == [] and sla_rows(pg_store, task_id) == [], "inside the 30 minute SLA"
    age(pg_store, task_id, 31)
    result = sweep(pg_store)
    assert result["role_sla"] == [task_id] and result["skipped"] is False
    recipients = sorted(row["recipient"] for row in sla_rows(pg_store, task_id))
    assert recipients == sorted([principal(identities, "owner"), principal(identities, "reviewer")])
    for _ in range(3):
        assert sweep(pg_store)["role_sla"] == []
    assert len(sla_rows(pg_store, task_id)) == 2, "one notification per recipient however often the sweep runs"
    assert len(audit(pg_store, "role.sla_breached", task_id=task_id)) == 1
    record = audit(pg_store, "role.sla_breached", task_id=task_id)[0]["metadata"]
    assert record["role"] == "review.primary" and record["sla_minutes"] == 30 and record["wait_minutes"] >= 31

    seen = notes(call, "owner")
    assert "role.sla_breached" in events(seen)
    note = [item for item in seen["notifications"] if item["event"] == "role.sla_breached"][0]
    assert note["object_type"] == "task" and note["object_id"] == task_id and note["thread_id"] == task_id
    assert PROMPT not in json.dumps({key: note[key] for key in note if key not in {"body_preview", "subject"}})
    assert "role.sla_breached" in events(notes(call, "reviewer"))
    for label in ("worker", "replacement", "stranger"):
        assert "role.sla_breached" not in events(notes(call, label)), "only root and approvers hear about it"


def test_sla_notification_repeats_for_the_same_task_only_after_sixty_minutes(team, pg_store):
    call, identities, space, page = team
    task_id = submit(call, page, "review.docs", scopes=path_scope("src/sla/repeat"))["task_id"]
    age(pg_store, task_id, 45)
    assert sweep(pg_store)["role_sla"] == [task_id]
    execute(pg_store, "UPDATE baton.tasks SET role_sla_notified_at=clock_timestamp()-interval '59 minutes' WHERE id=%s", (task_id,))
    assert sweep(pg_store)["role_sla"] == [] and len(sla_rows(pg_store, task_id)) == 1
    execute(pg_store, "UPDATE baton.tasks SET role_sla_notified_at=clock_timestamp()-interval '61 minutes' WHERE id=%s", (task_id,))
    assert sweep(pg_store)["role_sla"] == [task_id] and len(sla_rows(pg_store, task_id)) == 2
    assert sweep(pg_store)["role_sla"] == [] and len(sla_rows(pg_store, task_id)) == 2


def test_a_claim_resets_the_sla_notification_and_a_claimed_task_never_breaches(team, pg_store):
    call, identities, space, page = team
    assign(call, space, "owner", principal(identities, "worker"), "review.primary")
    task_id = submit(call, page, "review.primary", scopes=path_scope("src/sla/claim"))["task_id"]
    age(pg_store, task_id, 40)
    assert sweep(pg_store)["role_sla"] == [task_id]
    assert task_column(pg_store, task_id, "role_sla_notified_at") is not None
    call("worker", "claim", operation_id=op(), task_id=task_id, lease_seconds=60)
    assert task_column(pg_store, task_id, "role_sla_notified_at") is None
    assert sweep(pg_store)["role_sla"] == [], "a claimed task is not waiting"
    assert "role_sla_notified_at" not in call("owner", "get_task", task_id=task_id)
    execute(pg_store, "UPDATE baton.x_claim SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE task_id=%s", (task_id,))
    assert sweep(pg_store)["role_sla"] == [task_id], "released back to the queue and still late: notified again"
    assert len(sla_rows(pg_store, task_id)) == 2


def test_only_the_oldest_ready_task_of_a_role_counts_and_other_conditions_are_ignored(team, pg_store):
    call, identities, space, page = team
    oldest = submit(call, page, "review.primary", scopes=path_scope("src/sla/a"))["task_id"]
    younger = submit(call, page, "review.primary", scopes=path_scope("src/sla/b"))["task_id"]
    within = submit(call, page, "review.second", scopes=path_scope("src/sla/c"))["task_id"]
    no_sla = submit(call, page, "impl.long", scopes=path_scope("src/sla/d"))["task_id"]
    unroled = submit(call, page, scopes=path_scope("src/sla/e"))["task_id"]
    named = submit(call, page, "review.primary", scopes=path_scope("src/sla/f"), assignee_principal=principal(identities, "worker"))["task_id"]
    prerequisite = submit(call, page, scopes=path_scope("docs/sla/pre"))["task_id"]
    waiting_on = submit(call, page, "review.docs", scopes=path_scope("src/sla/g"), depends_on=[prerequisite])["task_id"]
    for task_id, minutes in ((oldest, 80), (younger, 50), (within, 10), (no_sla, 5000), (unroled, 5000), (named, 5000), (waiting_on, 5000)):
        age(pg_store, task_id, minutes)
    assert sweep(pg_store)["role_sla"] == [oldest]
    assert sla_rows(pg_store, younger) == [] and sla_rows(pg_store, no_sla) == [] and sla_rows(pg_store, unroled) == []
    assert sla_rows(pg_store, named) == [] and sla_rows(pg_store, waiting_on) == [] and sla_rows(pg_store, within) == []
    assign(call, space, "owner", principal(identities, "worker"), "review.primary")
    call("worker", "claim", operation_id=op(), task_id=oldest, lease_seconds=300)
    assert sweep(pg_store)["role_sla"] == [younger], "the next oldest takes its turn once the first is claimed"


def test_a_task_blocked_by_a_live_claims_scope_is_not_counted_as_ready(team, pg_store):
    call, identities, space, page = team
    holder = submit(call, page, scopes=path_scope("src/sla/held"), assignee_principal=principal(identities, "worker"))["task_id"]
    call("worker", "claim", operation_id=op(), task_id=holder, lease_seconds=600)
    blocked = submit(call, page, "review.primary", scopes=path_scope("src/sla/held/inner"))["task_id"]
    age(pg_store, blocked, 90)
    assert sweep(pg_store)["role_sla"] == []
    panel = call("owner", "list_roles", space_id=space["id"])["roles"][0]
    assert (panel["role"], panel["queue_length"], panel["not_ready"], panel["sla_state"]) == ("review.primary", 0, 1, "ok")


def test_root_sets_a_custom_sla_map_and_can_restore_the_defaults(team, pg_store):
    call, identities, space, page = team
    listed = call("owner", "list_roles", space_id=space["id"])
    assert listed["sla_minutes"] == DEFAULT_ROLE_SLA_MINUTES and listed["sla_default"] is True
    result = call("owner", "set_role_sla", operation_id=op(), space_id=space["id"], sla_minutes={"impl.long": 5, "review.*": 120})
    assert result == {"space_id": space["id"], "sla_minutes": {"impl.long": 5, "review.*": 120}, "default": False, "replay": False}
    long_task = submit(call, page, "impl.long", scopes=path_scope("src/sla/long"))["task_id"]
    review = submit(call, page, "review.primary", scopes=path_scope("src/sla/review"))["task_id"]
    triage = submit(call, page, "triage", scopes=path_scope("src/sla/triage"))["task_id"]
    for task_id in (long_task, review, triage):
        age(pg_store, task_id, 10)
    assert sweep(pg_store)["role_sla"] == [long_task], "only impl.long has an SLA (5 minutes) now; review is 120 and triage has none"
    call("owner", "set_role_sla", operation_id=op(), space_id=space["id"], sla_minutes={})
    age(pg_store, review, 500)
    execute(pg_store, "UPDATE baton.tasks SET role_sla_notified_at=NULL")
    assert sweep(pg_store)["role_sla"] == [], "an empty map means no role has an SLA"
    restored = call("owner", "set_role_sla", operation_id=op(), space_id=space["id"], sla_minutes=None)
    assert restored["sla_minutes"] == DEFAULT_ROLE_SLA_MINUTES and restored["default"] is True
    assert sweep(pg_store)["role_sla"] == [review]
    assert call("owner", "set_role_sla", 422, operation_id=op(), space_id=space["id"], sla_minutes={"Bad": 1})["code"] == "invalid"
    assert call("owner", "set_role_sla", 422, operation_id=op(), space_id=space["id"], sla_minutes={"maint": 0})["code"] == "invalid"
    assert call("owner", "set_role_sla", 422, operation_id=op(), space_id=space["id"], sla_minutes={"maint": "5"})["code"] == "invalid"
    assert call("owner", "set_role_sla", 422, operation_id=op(), space_id=space["id"], sla_minutes=[5])["code"] == "invalid"


def test_triage_has_a_fifteen_minute_default(team, pg_store):
    call, identities, space, page = team
    task_id = submit(call, page, "triage", scopes=path_scope("src/sla/triage"))["task_id"]
    age(pg_store, task_id, 14)
    assert sweep(pg_store)["role_sla"] == []
    age(pg_store, task_id, 16)
    assert sweep(pg_store)["role_sla"] == [task_id]


def test_concurrent_sweeps_write_one_notification_per_recipient(team, pg_store, approver):
    call, identities, space, page = team
    approver(pg_store, identities["reviewer"], by=identities["owner"])
    task_id = submit(call, page, "review.primary", scopes=path_scope("src/sla/race"))["task_id"]
    age(pg_store, task_id, 45)
    results = run_concurrently(4, lambda: sweep(pg_store))
    assert sorted(task for result in results for task in result.get("role_sla", [])) == [task_id]
    assert len(sla_rows(pg_store, task_id)) == 2 and len(audit(pg_store, "role.sla_breached", task_id=task_id)) == 1


def test_guard_alone_prevents_duplicate_sla_notifications_without_the_advisory_lock(team, pg_store, monkeypatch):
    call, identities, space, page = team
    task_id = submit(call, page, "review.primary", scopes=path_scope("src/sla/guard"))["task_id"]
    age(pg_store, task_id, 45)
    original = pg_store._transaction
    monkeypatch.setattr(pg_store, "_transaction", lambda write=False, single_flight=None: original(write))
    results = run_concurrently(4, lambda: sweep(pg_store))
    assert [task for result in results for task in result.get("role_sla", [])] == [task_id]
    assert len(sla_rows(pg_store, task_id)) == 1


def test_a_held_advisory_lock_skips_the_sla_sweep_too(team, pg_store):
    call, identities, space, page = team
    task_id = submit(call, page, "review.primary", scopes=path_scope("src/sla/locked"))["task_id"]
    age(pg_store, task_id, 45)
    config = json.loads(pg_store.config_path.read_text())
    with psycopg.connect(**config) as holder:
        holder.execute("SELECT pg_advisory_xact_lock(%s)", (ESCALATION_LOCK_ID,))
        assert sweep(pg_store) == {"skipped": True, "escalated": [], "reassigned": []}
    assert sla_rows(pg_store, task_id) == []
    assert sweep(pg_store)["role_sla"] == [task_id]


def test_the_server_escalation_pass_runs_the_role_sla_sweep_and_wakes_waiters(team, pg_store, modules):
    call, identities, space, page = team
    server = modules[1]
    task_id = submit(call, page, "review.primary", scopes=path_scope("src/sla/server"))["task_id"]
    age(pg_store, task_id, 45)
    asyncio.run(server.escalation_pass())
    assert events(notes(call, "owner"))[-1] == "role.sla_breached"
    asyncio.run(server.escalation_pass())
    assert events(notes(call, "owner")).count("role.sla_breached") == 1


def test_a_recipient_without_page_access_gets_no_notification(team, pg_store, approver):
    call, identities, space, page = team
    approver(pg_store, identities["stranger"], by=identities["owner"])
    task_id = submit(call, page, "review.primary", scopes=path_scope("src/sla/hidden"))["task_id"]
    age(pg_store, task_id, 45)
    assert sweep(pg_store)["role_sla"] == [task_id]
    assert [row["recipient"] for row in sla_rows(pg_store, task_id)] == [principal(identities, "owner")]


def test_webhook_subscriptions_know_the_new_event_name():
    assert space_store.NOTIFY_EVENTS >= {"role.sla_breached"}
    assert space_store.SpaceStore._events(["role.sla_breached", "task.overdue"]) == ["role.sla_breached", "task.overdue"]
    with pytest.raises(StoreError):
        space_store.SpaceStore._events(["role.sla_unknown"])


# --- list_roles and the board -----------------------------------------------------------------------------------------

def test_list_roles_reports_holders_queue_age_and_sla_state(team, pg_store):
    call, identities, space, page = team
    assign(call, space, "owner", principal(identities, "worker"), "review.primary")
    assign(call, space, "owner", principal(identities, "replacement"), "review.primary")
    assign(call, space, "owner", principal(identities, "worker"), "maint")
    first = submit(call, page, "review.primary", scopes=path_scope("src/panel/a"))["task_id"]
    submit(call, page, "review.primary", scopes=path_scope("src/panel/b"))
    submit(call, page, "impl.long", scopes=path_scope("src/panel/c"))
    age(pg_store, first, 12)
    listing = call("owner", "list_roles", space_id=space["id"])
    panels = {entry["role"]: entry for entry in listing["roles"]}
    assert sorted(panels) == ["impl.long", "maint", "review.primary"]
    primary = panels["review.primary"]
    assert primary["queue_length"] == 2 and primary["oldest_task_id"] == first and primary["sla_state"] == "ok" and primary["sla_minutes"] == 30
    assert 12 * 60 <= primary["oldest_waiting_seconds"] < 14 * 60
    assert [holder["principal"] for holder in primary["holders"]] == [principal(identities, "worker"), principal(identities, "replacement")]
    assert {"principal", "kind", "granted_by", "authority", "since"} == set(primary["holders"][0])
    assert panels["maint"]["queue_length"] == 0 and panels["maint"]["oldest_waiting_seconds"] is None and panels["maint"]["sla_minutes"] is None
    assert panels["impl.long"]["holders"] == [] and panels["impl.long"]["sla_state"] == "ok"
    age(pg_store, first, 31)
    breached = {entry["role"]: entry for entry in call("owner", "list_roles", space_id=space["id"])["roles"]}["review.primary"]
    assert breached["sla_state"] == "breached", "the panel computes the state live, between sweeps"
    for label in ("worker", "reviewer"):
        assert call(label, "list_roles", space_id=space["id"])["roles"], "anyone who can see the space may read its roles"
    assert call("stranger", "list_roles", 404, space_id=space["id"])["code"] == "not_found"
    assert call("owner", "list_roles", 422, space_id="not-a-uuid")["code"] == "invalid"


def test_the_board_data_carries_the_role_panels_and_the_page_renders_them(api):
    client, call, identities, tokens, registry, records = api
    space, page = shared_page(call, identities)
    assign(call, space, "owner", principal(identities, "worker"), "review.primary")
    submit(call, page, "review.primary", scopes=path_scope("src/board/a"))
    headers = lambda label: {"Authorization": "Bearer " + tokens[label]}  # noqa: E731
    data = client.get("/board/data", headers=headers("owner")).json()
    assert data["roles"] and data["roles"][0]["space_name"] == "Synthetic team"
    role = data["roles"][0]["roles"][0]
    assert role["role"] == "review.primary" and role["queue_length"] == 1 and role["holders"][0]["principal"] == principal(identities, "worker")
    assert client.get("/board/data", headers=headers("stranger")).json()["roles"] == []
    html = (pathlib.Path(__file__).parents[1] / "board/index.html").read_text()
    for needle in ("Role queues", "queue_length", "oldest_waiting_seconds", "sla_state", "holders", "textContent"):
        assert needle in html
    assert "<script src" not in html and "innerHTML" not in html, "no new frontend dependencies and no unescaped insertion"


# --- audit and schema -------------------------------------------------------------------------------------------------

def test_every_role_mutation_leaves_an_audit_row(team, pg_store, approver):
    call, identities, space, page = team
    approver(pg_store, identities["reviewer"], by=identities["owner"])
    call("owner", "set_role_rules", operation_id=op(), space_id=space["id"], rules=[{"role": "review.docs", "match": {"scope_prefix": "docs"}}])
    call("owner", "set_role_sla", operation_id=op(), space_id=space["id"], sla_minutes={"review.*": 20})
    derived = submit(call, page, scopes=path_scope("docs/a"))["task_id"]
    explicit = submit(call, page, "review.primary", scopes=path_scope("src/audit/a"))["task_id"]
    assign(call, space, "reviewer", principal(identities, "worker"), "review.primary")
    call("owner", "set_task_role", operation_id=op(), task_id=explicit, role="review.second")
    call("owner", "set_task_role", operation_id=op(), task_id=explicit, role=None)
    call("owner", "set_task_role", operation_id=op(), task_id=explicit, role="review.primary")
    call("worker", "claim", operation_id=op(), task_id=explicit)
    call("reviewer", "revoke_role", operation_id=op(), space_id=space["id"], principal=principal(identities, "worker"), role="review.primary")
    age(pg_store, derived, 25)
    sweep(pg_store)
    seen = [row["event"] for row in call("owner", "list_audit", space_id=space["id"], limit=1000)]
    for event in ("role_rules.set", "role_sla.set", "task.role_derived", "task.role_set", "role.assigned", "task.role_cleared",
                  "role.revoked", "task.claimed", "role.sla_breached"):
        assert event in seen, event
    assert seen.count("task.role_set") == 3 and seen.count("task.role_derived") == 1
    metadata = json.dumps([row["metadata"] for row in call("owner", "list_audit", space_id=space["id"], limit=1000)])
    assert PROMPT not in metadata
    for row in call("owner", "list_audit", space_id=space["id"], limit=1000):
        if row["event"] in {"role.assigned", "role.revoked"}:
            assert row["metadata"]["authority"].startswith("delegated by ")


def test_schema_upgrades_in_place_and_is_idempotent(team, pg_store):
    call, identities, space, page = team
    old_task = submit(call, page, scopes=path_scope("src/upgrade/old"))["task_id"]
    admin = json.loads(pathlib.Path(os.environ["BATON_TEST_DATABASE_ADMIN_CONFIG"]).read_text())
    app = json.loads(pg_store.config_path.read_text())
    schema = (pathlib.Path(__file__).parents[1] / "bridge/space_schema.sql").read_text()
    with psycopg.connect(**{**admin, "dbname": app["dbname"]}) as connection:
        connection.execute("SET ROLE baton_owner")
        connection.execute("DROP TABLE baton.x_role_holder")
        connection.execute("DROP TABLE baton.x_role_config")
        connection.execute("DROP INDEX baton.tasks_role_queue")
        connection.execute("ALTER TABLE baton.tasks DROP COLUMN role, DROP COLUMN role_source, DROP COLUMN role_sla_notified_at")
        connection.commit()
        with pytest.raises(StoreError) as old:
            pg_store.check_schema(SERVICE_ACTOR)
        assert old.value.code == "storage_unavailable", "the bridge refuses to start against a schema without the role objects"
        for _ in range(2):
            connection.execute(schema)
            connection.commit()
        role = sql.Identifier(app["user"])
        connection.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA baton TO {}").format(role))
        connection.execute(sql.SQL("REVOKE UPDATE, DELETE, TRUNCATE ON baton.x_audit FROM {}").format(role))
        connection.execute(sql.SQL("REVOKE ALL ON baton.x_claim_holder FROM {}").format(role))
        connection.execute(sql.SQL("GRANT SELECT, INSERT ON baton.x_claim_holder TO {}").format(role))
        connection.execute(sql.SQL("REVOKE ALL ON baton.x_delegation FROM {}").format(role))
        connection.execute(sql.SQL("GRANT SELECT, INSERT ON baton.x_delegation TO {}").format(role))
        connection.execute(sql.SQL("GRANT UPDATE (revoked_at, revoked_by) ON baton.x_delegation TO {}").format(role))
        connection.commit()
    assert pg_store.check_schema(SERVICE_ACTOR)["status"] == "ok"
    view = call("owner", "get_task", task_id=old_task)
    assert view["role"] is None and view["role_source"] is None, "tasks from before the upgrade read as roleless"
    assert assign(call, space, "owner", principal(identities, "worker"), "maint")["changed"] is True
    assert submit(call, page, "maint")["role"] == "maint"
    with psycopg.connect(**{**admin, "dbname": app["dbname"]}) as connection:
        connection.execute("SET ROLE baton_owner")
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute("UPDATE baton.tasks SET role='Bad Role',role_source='explicit' WHERE id=%s", (old_task,))
        connection.rollback()
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute("UPDATE baton.tasks SET role='maint' WHERE id=%s", (old_task,))
        connection.rollback()
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute("UPDATE baton.tasks SET role='maint',role_source='other' WHERE id=%s", (old_task,))
        connection.rollback()


# --- protocol surface -------------------------------------------------------------------------------------------------

def test_schema_and_mcp_expose_the_role_operations(team, api):
    client, call, identities, tokens, registry, records = api
    schema = client.get("/v1/space/schema", headers={"Authorization": "Bearer " + tokens["owner"]}).json()["operations"]
    expected = {"set_task_role": True, "set_role_rules": True, "set_role_sla": True, "assign_role": True, "revoke_role": True,
                "list_roles": False, "ready_for_role": False}
    for name, mutation in expected.items():
        assert schema[name]["mutation"] is mutation, name
    assert schema["assign_role"]["request"]["properties"]["principal"]["type"] == "string"
    for name in ("submit_task", "create_task_block"):
        assert "role" in schema[name]["request"]["properties"] and "role" not in schema[name]["request"].get("required", [])
    headers = {"Authorization": "Bearer " + tokens["owner"], "Host": "bridge", "Accept": "application/json, text/event-stream"}
    listing = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = {tool["name"]: tool for tool in listing.json()["result"]["tools"]}
    assert set(expected) <= tools.keys()
    assert "role" in tools["submit_task"]["inputSchema"]["properties"]
    assert "operation_id" in tools["assign_role"]["inputSchema"]["required"]
    space, page = team[2], team[3]
    result = None
    reply = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
        "name": "submit_task", "arguments": {"idempotency_key": "role-mcp-" + uuid.uuid4().hex[:8], "prompt": PROMPT,
                                              "page_id": page["id"], "role": "review.docs"}}})
    result = reply.json()["result"]
    assert not result.get("isError", False)
    assert (result.get("structuredContent") or json.loads(result["content"][0]["text"]))["role"] == "review.docs"
    bad = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
        "name": "submit_task", "arguments": {"idempotency_key": "role-mcp-" + uuid.uuid4().hex[:8], "prompt": PROMPT, "role": "BAD"}}})
    assert bad.json()["result"]["isError"] is True
    assert "BAD" not in json.dumps(bad.json())


def test_task_views_never_expose_the_internal_sla_marker(team, pg_store):
    call, identities, space, page = team
    task = submit(call, page, "review.primary")
    assert "role_sla_notified_at" not in task and task["role"] == "review.primary"
    assert all("role_sla_notified_at" not in row for row in call("owner", "list_tasks"))
