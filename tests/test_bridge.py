# SPDX-License-Identifier: Apache-2.0
"""Public HTTP/MCP boundaries and dispatch fencing on actual PostgreSQL."""

import asyncio
import hashlib
import json
import uuid
from pathlib import Path

import pytest
from starlette.testclient import TestClient


def op():
    return str(uuid.uuid4())


@pytest.fixture
def api(modules, pg_store, make_context):
    common, server, _ = modules
    identities = {}
    tokens = {}
    records = {}
    for label in ("owner", "reviewer", "worker", "replacement", "stranger"):
        kind = "human" if label in {"owner", "reviewer"} else "agent"
        who = make_context(label, kind=kind).request_context.request.state.identity
        if label == "reviewer":
            # A person's principal and connected account are distinct so a
            # mention cannot accidentally route by account instead of person.
            who["account"] += "-account"
        token = "synthetic-" + uuid.uuid4().hex
        identities[label] = who
        tokens[label] = token
        records[label] = {**who, "active": True, "hash": hashlib.sha256(token.encode()).hexdigest()}
    registry = common.CONFIG / "identities.json"
    registry.write_text(json.dumps(records))
    with TestClient(server.app) as client:
        def call(label, operation, expected=200, **body):
            response = client.post("/v1/space/" + operation, json=body,
                                   headers={"Authorization": "Bearer " + tokens[label]})
            assert response.status_code == expected
            assert response.headers["cache-control"] == "no-store"
            data = response.json()
            assert data["x_protocol"] == "baton.space.v1"
            return data.get("result") if expected == 200 else data["error"]
        yield client, call, identities, tokens, registry, records


def shared_page(call, identities):
    space = call("owner", "create_space", operation_id=op(), name="Synthetic team")
    page = call("owner", "create_page", operation_id=op(), space_id=space["id"], title="Work")
    for label in ("worker", "replacement", "reviewer"):
        call("owner", "share", operation_id=op(), resource_type="space", resource_id=space["id"],
             subject_type="person", subject_id=identities[label]["account"], role="edit")
    return space, page


def test_schema_mcp_parameters_and_actor_injection_are_safe(api, modules, capsys):
    client, call, _, tokens, _, _ = api
    common, _, _ = modules
    private = "synthetic-private-body-marker"
    assert client.get("/v1/space/schema").status_code == 401
    schema = client.get("/v1/space/schema", headers={"Authorization": "Bearer " + tokens["owner"]}).json()
    operations = schema["operations"]
    assert {"create_space", "comment", "mention", "assign_task", "plan", "ready", "claim", "renew", "handoff", "gate", "list_audit"} <= operations.keys()
    assert not {"claim_next", "finish_auto", "fail_auto", "recover_auto", "check_schema"} & operations.keys()
    assert operations["handoff"]["request"]["properties"]["target_principal"]["type"] == "string"
    assert "target" not in operations["handoff"]["request"]["properties"]
    graph_entry = operations["plan"]["request"]["$defs"]["GraphEntry"]
    assert graph_entry["additionalProperties"] is False
    assert set(graph_entry["required"]) == {"task_id", "depends_on"}
    for name in ("create_task_block", "submit_task"):
        assert "depends_on" in operations[name]["request"]["properties"]
        assert "depends_on" not in operations[name]["request"].get("required", [])
    error = call("owner", "create_space", 422, operation_id=op(), name="Safe", actor={"principal": private})
    assert error["code"] == "invalid" and private not in json.dumps(error)
    assert call("owner", "finish_auto", 422, operation_id=op(), result=private)["code"] == "invalid"
    headers = {"Authorization": "Bearer " + tokens["owner"], "Host": "bridge",
               "Accept": "application/json, text/event-stream"}
    response = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = {tool["name"]: tool for tool in response.json()["result"]["tools"]}
    assert {"x_claim", "x_scope", "x_handoff", "x_gate", "x_audit"} <= tools.keys()
    assert "depends_on" in tools["submit_task"]["inputSchema"]["properties"]
    for name in ("create_space", "handoff", "comment", "assign_task", "plan"):
        assert not {"ctx", "arguments", "actor"} & tools[name]["inputSchema"].get("properties", {}).keys()
        assert "operation_id" in tools[name]["inputSchema"]["required"]
    response = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
        "name": "create_space", "arguments": {"operation_id": op(), "name": "Safe", "actor": private}}})
    assert response.json()["result"]["isError"] is True
    assert private not in response.text
    for index, name in enumerate(("list_tasks", "fetch_inbox"), 4):
        response = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": index,
            "method": "tools/call", "params": {"name": name}})
        assert not response.json()["result"].get("isError", False)
    assert private not in capsys.readouterr().out
    if (common.STATE / "audit.jsonl").exists():
        assert private not in (common.STATE / "audit.jsonl").read_text()


def test_assigned_task_only_recipient_can_claim_and_review_is_independent(api, modules, pg_store):
    client, call, identities, tokens, _, _ = api
    space, page = shared_page(call, identities)
    task = call("owner", "submit_task", operation_id=op(), idempotency_key="assigned-journey", prompt="Synthetic assigned task",
                page_id=page["id"], scopes=[{"kind": "path", "value": "src/assigned"}],
                assignee_principal=identities["worker"]["agent"])
    assert task["execution_mode"] == "manual" and task["assignee"] == identities["worker"]["agent"]
    assert task["thread_id"] == task["task_id"]
    assert pg_store.claim_next(modules[1].SERVICE, op()) is None
    assert call("reviewer", "claim", 403, operation_id=op(), task_id=task["task_id"])["code"] == "forbidden"
    inbox = call("worker", "fetch_inbox")
    assert any(message["thread_id"] == task["task_id"] for message in inbox["messages"])
    claim = call("worker", "claim", operation_id=op(), task_id=task["task_id"])
    assert claim["status"] == "working" and claim["state"] == "claimed"
    assert claim["prompt"] == "Synthetic assigned task"
    assert call("worker", "get_task", task_id=task["task_id"])["prompt"] == "Synthetic assigned task"
    candidate = call("worker", "submit_result", operation_id=op(), task_id=task["task_id"], epoch=claim["epoch"],
                     result="Synthetic reviewed result", evidence=["synthetic-evidence"])
    assert candidate["state"] == "awaiting_gate" and candidate["status"] != "completed"
    assert call("worker", "gate", 403, operation_id=op(), task_id=task["task_id"],
                expected_epoch=claim["epoch"], decision="pass")["code"] == "forbidden"
    done = call("owner", "gate", operation_id=op(), task_id=task["task_id"],
                expected_epoch=claim["epoch"], decision="pass", evidence=["synthetic-review"])
    assert done["state"] == "done" and done["status"] == "completed"
    headers = {"Authorization": "Bearer " + tokens["owner"], "Host": "bridge", "Accept": "application/json, text/event-stream"}
    response = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
        "name": "submit_task", "arguments": {"idempotency_key": "assigned-journey", "prompt": "Synthetic assigned task",
          "page_id": page["id"], "scopes": [{"kind": "path", "value": "src/assigned"}],
          "assignee_principal": identities["worker"]["agent"]}}})
    result = response.json()["result"]
    assert not result.get("isError", False)
    replay = result.get("structuredContent") or json.loads(result["content"][0]["text"])
    assert replay["task_id"] == task["task_id"] and replay["replay"] is True
    audit = call("owner", "list_audit", task_id=task["task_id"])
    assert {"task.assigned", "task.claimed", "task.result_submitted"} <= {row["event"] for row in audit}
    assert all(row["task_id"] == task["task_id"] for row in audit)
    assert "Synthetic assigned task" not in json.dumps(audit)


def test_handoff_resolves_registry_and_preserves_base_context_and_evidence(api):
    _, call, identities, _, _, _ = api
    _, page = shared_page(call, identities)
    task = call("owner", "create_task_block", operation_id=op(), page_id=page["id"], prompt="Handoff",
                scopes=[{"kind": "path", "value": "src/handoff"}], reviewers=[identities["owner"]["account"]],
                base_commit="synthetic-base", context=["context-v1"], evidence=["evidence-v1"],
                assignee_principal=identities["worker"]["agent"])
    claim = call("worker", "claim", operation_id=op(), task_id=task["task_id"])
    handoff = call("worker", "handoff", operation_id=op(), task_id=task["task_id"], epoch=claim["epoch"],
                   target_principal=identities["replacement"]["agent"])
    assert handoff["epoch"] > claim["epoch"]
    assert handoff["base_commit"] == "synthetic-base" and handoff["context"] == ["context-v1"]
    assert handoff["evidence"] == ["evidence-v1"] and handoff["thread_id"] == task["thread_id"]
    assert handoff["assignee"] == identities["replacement"]["agent"]
    assert call("worker", "renew", 409, operation_id=op(), task_id=task["task_id"], epoch=claim["epoch"])["code"] == "stale_claim"
    assert call("replacement", "handoff", 422, operation_id=op(), task_id=task["task_id"], epoch=handoff["epoch"],
                target={"principal": identities["worker"]["agent"]})["code"] == "invalid"


def test_inherited_access_and_replay_revocation_are_rechecked(api):
    _, call, identities, _, _, _ = api
    space, page = shared_page(call, identities)
    child = call("owner", "create_page", operation_id=op(), space_id=space["id"], parent_id=page["id"], title="Child")
    comment_op = op()
    comment = call("worker", "comment", operation_id=comment_op, page_id=child["id"], body="Private comment")
    call("owner", "mention", operation_id=op(), comment_id=comment["id"], target_type="agent", target_id=identities["worker"]["agent"])
    assert call("worker", "fetch_inbox")["messages"]
    call("owner", "share", operation_id=op(), resource_type="page", resource_id=child["id"],
         subject_type="person", subject_id=identities["worker"]["account"], role="view")
    call("owner", "share", operation_id=op(), resource_type="page", resource_id=child["id"],
         subject_type="person", subject_id=identities["worker"]["account"], role=None)
    assert call("worker", "effective_role", resource_type="page", resource_id=child["id"])["role"] == "edit"
    call("owner", "share", operation_id=op(), resource_type="space", resource_id=space["id"],
         subject_type="person", subject_id=identities["worker"]["account"], role=None)
    assert call("worker", "comment", 403, operation_id=comment_op, page_id=child["id"], body="Private comment")["code"] == "forbidden"
    assert call("worker", "fetch_inbox")["messages"] == []


def test_token_revocation_and_duplicate_headers_fail_closed(api):
    client, _, _, tokens, registry, records = api
    headers = {"Authorization": "Bearer " + tokens["owner"]}
    assert client.get("/v1/space/schema", headers=headers).status_code == 200
    duplicate = client.get("/v1/space/schema", headers=[("Authorization", "Bearer " + tokens["owner"]),
                                                      ("Authorization", "Bearer " + tokens["worker"])])
    assert duplicate.status_code == 401
    records["owner"]["active"] = False
    registry.write_text(json.dumps(records))
    response = client.get("/v1/space/schema", headers=headers)
    assert response.status_code == 401 and tokens["owner"] not in response.text


def test_storage_failure_is_metadata_only(api, modules, pg_store, monkeypatch, capsys):
    _, call, _, _, _, _ = api
    private = "synthetic-password-and-private-sql"
    def failing(*args, **kwargs):
        raise RuntimeError(private)
    monkeypatch.setattr(pg_store, "create_space", failing)
    error = call("owner", "create_space", 503, operation_id=op(), name=private)
    assert error["code"] == "storage_unavailable" and private not in json.dumps(error)
    assert private not in capsys.readouterr().out


@pytest.mark.asyncio
async def test_unknown_dispatch_is_durable_and_not_automatically_retried(modules, pg_store, make_context, register_identity, monkeypatch):
    common, server, _ = modules
    context = make_context()
    register_identity(context.request_context.request.state.identity)
    task = await server.submit_task("ambiguous", "Synthetic dispatch", context)
    calls = []
    class FailedHttp:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def post(self, *args, **kwargs):
            calls.append(1)
            raise RuntimeError("synthetic-private-upstream-error")
    monkeypatch.setattr(server.httpx, "AsyncClient", FailedHttp)
    assert await server.process_next()
    view = await server.get_task(task["task_id"], make_context())
    assert view["status"] == "interrupted"
    assert not await server.process_next() and len(calls) == 1
    assert "synthetic-private-upstream-error" not in (common.STATE / "audit.jsonl").read_text()


@pytest.mark.asyncio
async def test_expired_dispatch_cannot_commit_success_or_thread_report(modules, pg_store, make_context, register_identity, monkeypatch):
    _, server, _ = modules
    context = make_context()
    register_identity(context.request_context.request.state.identity)
    task = await server.submit_task("late-result", "Synthetic dispatch", context)
    claim = pg_store.claim_next(server.SERVICE, op(), lease_seconds=1)
    class Response:
        def raise_for_status(self):
            pass
        def json(self):
            return {"result": {"id": op(), "status": {"state": "completed"}, "artifacts": [{
                "metadata": {"backend": "demo"}, "parts": [{"kind": "text", "text": "LATE_RESULT"}]}]}}
    class SlowHttp:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def post(self, *args, **kwargs):
            await asyncio.sleep(1.1)
            return Response()
    monkeypatch.setattr(server.httpx, "AsyncClient", SlowHttp)
    with pytest.raises(server.DispatchUncertain):
        await server.dispatch(claim)
    await server.recover_auto()
    view = await server.get_task(task["task_id"], make_context())
    assert view["status"] == "interrupted" and view["result"] is None
    inbox = await server.fetch_inbox(make_context())
    assert not any(message.get("subject", "").startswith("[completed]") for message in inbox["messages"])


def test_gateway_routes_http_api_through_client_acl():
    root = Path(__file__).parents[1]
    template = json.loads((root / "deploy/gateway.template.json").read_text())
    routes = [route for bind in template["binds"] for listener in bind["listeners"] for route in listener["routes"]]
    api_routes = [route for route in routes if any(match.get("path", {}).get("pathPrefix") == "/v1/space/" for match in route["matches"])]
    assert len(api_routes) == 1
    assert api_routes[0]["policies"]["authorization"]["rules"] == ["apiKey.role == 'client'"]
    for filename in ("nginx.conf", "nginx.tls.conf"):
        config = (root / "deploy" / filename).read_text()
        assert "proxy_pass http://gateway:3000;" in config
        assert "$request_body" not in config and "$http_authorization" not in config


def test_graph_plan_and_ready_keep_current_gate_as_claim_authority(api):
    client, call, identities, tokens, _, _ = api
    _, page = shared_page(call, identities)
    tasks = []
    for label in ("prerequisite", "dependent"):
        tasks.append(call("owner", "create_task_block", operation_id=op(), page_id=page["id"],
                          prompt="Synthetic " + label, scopes=[{"kind": "path", "value": "src/" + label}],
                          reviewers=[identities["owner"]["agent"]],
                          assignee_principal=identities["worker"]["agent"]))
    first, second = tasks
    plan_id = op()
    graph = [{"task_id": second["task_id"], "depends_on": [first["task_id"]]}]
    call("owner", "plan", operation_id=plan_id, graph=graph)
    ready = call("worker", "ready", page_id=page["id"])
    assert {task["task_id"] for task in ready} == {first["task_id"]}
    assert call("worker", "claim", 409, operation_id=op(), task_id=second["task_id"])["code"] == "dependency_blocked"
    cycle = [{"task_id": first["task_id"], "depends_on": [second["task_id"]]}]
    assert call("owner", "plan", 409, operation_id=op(), graph=cycle)["code"] == "graph_cycle"
    assert {task["task_id"] for task in call("worker", "ready", page_id=page["id"])} == {first["task_id"]}
    malformed = [{"task_id": second["task_id"], "depends_on": [], "actor": "synthetic-private-graph"}]
    assert call("owner", "plan", 422, operation_id=op(), graph=malformed)["code"] == "invalid"
    headers = {"Authorization": "Bearer " + tokens["owner"], "Host": "bridge", "Accept": "application/json, text/event-stream"}
    response = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 20, "method": "tools/call", "params": {
        "name": "plan", "arguments": {"operation_id": plan_id, "graph": graph}}})
    result = response.json()["result"]
    assert not result.get("isError", False)
    claim = call("worker", "claim", operation_id=op(), task_id=first["task_id"])
    call("worker", "submit_result", operation_id=op(), task_id=first["task_id"], epoch=claim["epoch"], result="Candidate")
    assert call("worker", "ready", page_id=page["id"]) == []
    call("owner", "gate", operation_id=op(), task_id=first["task_id"], expected_epoch=claim["epoch"], decision="pass")
    ready = call("worker", "ready", page_id=page["id"])
    assert {task["task_id"] for task in ready} == {second["task_id"]}
    assert ready[0]["prompt"] == "Synthetic dependent"
    headers["Authorization"] = "Bearer " + tokens["worker"]
    response = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 21, "method": "tools/call", "params": {"name": "ready"}})
    result = response.json()["result"]
    assert not result.get("isError", False)
    content = result.get("structuredContent") or json.loads(result["content"][0]["text"])
    assert content["x_protocol"] == "baton.space.v1"
    assert {task["task_id"] for task in content["result"]} == {second["task_id"]}


def test_person_mentions_use_active_human_principal_not_connected_account(api):
    _, call, identities, _, registry, records = api
    _, page = shared_page(call, identities)
    comment = call("owner", "comment", operation_id=op(), page_id=page["id"], body="For this specific person")
    assert identities["reviewer"]["agent"] != identities["reviewer"]["account"]
    call("owner", "mention", operation_id=op(), comment_id=comment["id"], target_type="person", target_id=identities["reviewer"]["agent"])
    inbox = call("reviewer", "fetch_inbox")
    assert any(message["id"] == comment["id"] for message in inbox["messages"])
    for target_type, target_id in (("person", identities["reviewer"]["account"]),
                                   ("person", identities["worker"]["agent"]),
                                   ("agent", identities["reviewer"]["agent"])):
        assert call("owner", "mention", 422, operation_id=op(), comment_id=comment["id"],
                    target_type=target_type, target_id=target_id)["code"] == "invalid"
    records["reviewer"]["active"] = False
    registry.write_text(json.dumps(records))
    assert call("owner", "mention", 422, operation_id=op(), comment_id=comment["id"],
                target_type="person", target_id=identities["reviewer"]["agent"])["code"] == "invalid"


def test_atomic_dependencies_block_auto_dispatch_until_current_gate_passes(api, modules, pg_store):
    client, call, identities, tokens, _, _ = api
    _, page = shared_page(call, identities)
    parent = call("owner", "create_task_block", operation_id=op(), page_id=page["id"], prompt="Prerequisite",
                  scopes=[{"kind": "path", "value": "src/parent"}], reviewers=[identities["owner"]["agent"]],
                  assignee_principal=identities["worker"]["agent"])
    arguments = {"idempotency_key": "atomic-auto-dependent", "prompt": "Automatic dependent", "page_id": page["id"],
                 "scopes": [{"kind": "path", "value": "src/automatic"}], "depends_on": [parent["task_id"]]}
    headers = {"Authorization": "Bearer " + tokens["owner"], "Host": "bridge", "Accept": "application/json, text/event-stream"}
    response = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 30, "method": "tools/call", "params": {
        "name": "submit_task", "arguments": arguments}})
    result = response.json()["result"]
    assert not result.get("isError", False)
    child = result.get("structuredContent") or json.loads(result["content"][0]["text"])
    assert pg_store.claim_next(modules[1].SERVICE, op()) is None
    claim = call("worker", "claim", operation_id=op(), task_id=parent["task_id"])
    candidate = call("worker", "submit_result", operation_id=op(), task_id=parent["task_id"], epoch=claim["epoch"], result="Candidate")
    assert candidate["state"] == "awaiting_gate"
    assert pg_store.claim_next(modules[1].SERVICE, op()) is None
    call("owner", "gate", operation_id=op(), task_id=parent["task_id"], expected_epoch=claim["epoch"], decision="pass")
    dispatchable = pg_store.claim_next(modules[1].SERVICE, op())
    assert dispatchable["task_id"] == child["task_id"]
    assert call("owner", "submit_task", 409, operation_id=op(), **{**arguments, "depends_on": []})["code"] == "operation_conflict"


@pytest.fixture
def no_a2a(modules, monkeypatch):
    calls = []
    class ForbiddenHttp:
        def __init__(self, **kwargs):
            calls.append("client_created")
            raise AssertionError("A2A must not be reached after identity revocation")
    monkeypatch.setattr(modules[1].httpx, "AsyncClient", ForbiddenHttp)
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["revoked", "account", "kind", "role", "removed"])
async def test_queued_auto_task_rechecks_registered_owner_before_dispatch(
        modules, pg_store, make_context, register_identity, no_a2a, change):
    common, server, _ = modules
    context = make_context()
    who = context.request_context.request.state.identity
    token = register_identity(who)
    private = "synthetic-private-queued-instructions"
    task = await server.submit_task("revocation-" + change, private, context)
    registry = common.CONFIG / "identities.json"
    records = json.loads(registry.read_text())
    if change == "revoked":
        records[who["agent"]]["active"] = False
    elif change == "account":
        records[who["agent"]]["account"] = who["account"] + "-remapped"
    elif change == "kind":
        records[who["agent"]]["kind"] = "human"
    elif change == "role":
        records[who["agent"]].update(role="dispatcher", kind="service")
    else:
        del records[who["agent"]]
    registry.write_text(json.dumps(records))
    assert await server.process_next()
    # Inspect synthetic storage using the original saved owner, independent of
    # the now-revoked public token. This is not a public authentication bypass.
    actor = server.Actor(who["agent"], who["account"], who["kind"])
    view = pg_store.get_task(actor, task["task_id"])
    assert view["status"] in {"failed", "interrupted"} and view["error"] == "cancelled"
    assert view["dispatch_started_at"] is None and no_a2a == []
    assert not await server.process_next()
    events = pg_store.list_audit(actor, task_id=task["task_id"])
    assert any(row["metadata"].get("error_code") == "cancelled" for row in events)
    log = (common.STATE / "audit.jsonl").read_text()
    assert token not in log and private not in log
    assert not any(json.loads(line)["event"] == "task.dispatched" for line in log.splitlines())


@pytest.mark.asyncio
async def test_dispatch_direct_entry_rechecks_identity_changed_after_claim(
        modules, pg_store, make_context, register_identity, no_a2a):
    common, server, _ = modules
    context = make_context()
    who = context.request_context.request.state.identity
    register_identity(who)
    task = await server.submit_task("revoked-after-claim", "Synthetic task", context)
    claim = await server.claim_next()
    assert claim["task_id"] == task["task_id"] and server.dispatch_owner_current(claim)
    registry = common.CONFIG / "identities.json"
    records = json.loads(registry.read_text())
    records[who["agent"]]["active"] = False
    registry.write_text(json.dumps(records))
    assert not server.dispatch_owner_current(claim)
    stopped = await server.dispatch(claim)
    assert stopped["error"] == "cancelled" and stopped["dispatch_started_at"] is None
    assert no_a2a == []


@pytest.mark.asyncio
@pytest.mark.parametrize("identity_change", ["revoked", "account", "kind", "role"])
async def test_dispatch_rechecks_revocation_while_mark_dispatched_waits(
        modules, pg_store, make_context, register_identity, no_a2a, monkeypatch, identity_change):
    common, server, _ = modules
    context = make_context()
    who = context.request_context.request.state.identity
    register_identity(who)
    task = await server.submit_task("revoked-during-mark", "Synthetic task", context)
    claim = await server.claim_next()
    assert claim["task_id"] == task["task_id"] and server.dispatch_owner_current(claim)
    entered = asyncio.Event()
    release = asyncio.Event()
    original_store_call = server.store_call

    async def held_store_call(method, *args, **kwargs):
        if method == "mark_dispatched":
            entered.set()
            await release.wait()
        return await original_store_call(method, *args, **kwargs)

    monkeypatch.setattr(server, "store_call", held_store_call)
    pending = asyncio.create_task(server.dispatch(claim))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        registry = common.CONFIG / "identities.json"
        records = json.loads(registry.read_text())
        record = records[who["agent"]]
        if identity_change == "revoked":
            record["active"] = False
        elif identity_change == "account":
            record["account"] = "synthetic-remapped-account"
        elif identity_change == "kind":
            record["kind"] = "human"
        else:
            record["role"] = "dispatcher"
        registry.write_text(json.dumps(records))
        release.set()
        stopped = await asyncio.wait_for(pending, timeout=5)
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
    assert stopped["status"] == "interrupted" and stopped["error"] == "cancelled"
    assert stopped["dispatch_started_at"] is not None  # The real PG mark committed.
    assert no_a2a == []
    log = (common.STATE / "audit.jsonl").read_text()
    assert not any(json.loads(line)["event"] == "task.dispatched" for line in log.splitlines())


def work_face(**changes):
    return {"environment": ["python 3.12"], "fixtures": ["seed.sql"], "constraints": ["no network"],
            "migration": None, "acceptance_command": "pytest -q", **changes}


def mcp_call(client, token, name, arguments, request_id=40):
    headers = {"Authorization": "Bearer " + token, "Host": "bridge", "Accept": "application/json, text/event-stream"}
    response = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                                                            "params": {"name": name, "arguments": arguments}})
    result = response.json()["result"]
    if result.get("isError", False):
        return None, result
    return result.get("structuredContent") or json.loads(result["content"][0]["text"]), result


def test_set_work_face_http_flow_enforces_gate_authority_and_error_codes(api):
    _, call, identities, _, _, _ = api
    _, page = shared_page(call, identities)
    reviewer = identities["reviewer"]["agent"]
    task = call("owner", "create_task_block", operation_id=op(), page_id=page["id"], prompt="Needs a work face",
                scopes=[{"kind": "path", "value": "src/work-face"}], reviewers=[reviewer],
                assignee_principal=identities["worker"]["agent"], require_work_face=True)
    assert task["require_work_face"] is True and task["task_kind"] == "construction" and task["work_face"] is None
    assert task["work_face_missing"] == ["base_commit", "work_face"]
    assert call("worker", "claim", 409, operation_id=op(), task_id=task["task_id"])["code"] == "work_face_incomplete"
    assert call("worker", "ready", page_id=page["id"]) == []

    for label in ("stranger", "worker"):
        assert call(label, "set_work_face", 403, operation_id=op(), task_id=task["task_id"], work_face=work_face())["code"] == "forbidden"
    for body in ({"work_face": {**work_face(), "extra": 1}}, {"work_face": "text"}, {"work_face": work_face(acceptance_command=" ")},
                 {"work_face": work_face(environment=["x"] * 21)}, {"work_face": work_face(), "base_commit": ""}, {}):
        assert call("owner", "set_work_face", 422, operation_id=op(), task_id=task["task_id"], **body)["code"] == "invalid"
    assert call("owner", "set_work_face", 422, operation_id=op(), task_id=task["task_id"], work_face=work_face(), actor="someone")["code"] == "invalid"

    set_op = op()
    done = call("reviewer", "set_work_face", operation_id=set_op, task_id=task["task_id"], work_face=work_face(), base_commit="http-base")
    assert done["work_face_missing"] == [] and done["work_face"] == work_face() and done["base_commit"] == "http-base"
    assert done["replay"] is False and "idempotency_key" not in done
    assert call("reviewer", "set_work_face", operation_id=set_op, task_id=task["task_id"], work_face=work_face(), base_commit="http-base")["replay"] is True
    assert call("reviewer", "set_work_face", 409, operation_id=set_op, task_id=task["task_id"],
                work_face=work_face(fixtures=["other.sql"]), base_commit="http-base")["code"] == "operation_conflict"
    assert call("owner", "set_work_face", 409, operation_id=op(), task_id=task["task_id"],
                work_face=work_face(), base_commit="other-base")["code"] == "operation_conflict"

    assert {item["task_id"] for item in call("worker", "ready", page_id=page["id"])} == {task["task_id"]}
    claim = call("worker", "claim", operation_id=op(), task_id=task["task_id"])
    assert claim["state"] == "claimed" and claim["work_face"] == work_face()
    assert call("owner", "set_work_face", 409, operation_id=op(), task_id=task["task_id"], work_face=work_face())["code"] == "stale_claim"
    events = [row for row in call("owner", "list_audit", task_id=task["task_id"]) if row["event"] == "task.work_face_set"]
    assert len(events) == 1 and "pytest -q" not in json.dumps(events)


def test_prep_task_gates_construction_over_http(api):
    _, call, identities, _, _, _ = api
    _, page = shared_page(call, identities)
    owner = identities["owner"]["agent"]
    prep = call("owner", "create_task_block", operation_id=op(), page_id=page["id"], prompt="Prepare the site",
                scopes=[{"kind": "migration", "value": "prep-site"}], reviewers=[owner],
                assignee_principal=identities["worker"]["agent"], task_kind="prep")
    build = call("owner", "create_task_block", operation_id=op(), page_id=page["id"], prompt="Build on the prepared site",
                 scopes=[{"kind": "migration", "value": "build-site"}], reviewers=[owner],
                 assignee_principal=identities["replacement"]["agent"], depends_on=[prep["task_id"]], require_work_face=True)
    assert prep["task_kind"] == "prep" and build["depends_on"] == [prep["task_id"]]
    assert call("worker", "set_work_face", 403, operation_id=op(), task_id=build["task_id"], work_face=work_face())["code"] == "forbidden"
    claim = call("worker", "claim", operation_id=op(), task_id=prep["task_id"])
    call("worker", "set_work_face", operation_id=op(), task_id=build["task_id"], work_face=work_face(), base_commit="prepared-base")
    assert call("replacement", "claim", 409, operation_id=op(), task_id=build["task_id"])["code"] == "dependency_blocked"
    call("worker", "submit_result", operation_id=op(), task_id=prep["task_id"], epoch=claim["epoch"], result="Site prepared")
    assert call("replacement", "claim", 409, operation_id=op(), task_id=build["task_id"])["code"] == "dependency_blocked"
    assert call("replacement", "ready", page_id=page["id"]) == []
    call("owner", "gate", operation_id=op(), task_id=prep["task_id"], expected_epoch=claim["epoch"], decision="pass")
    assert {item["task_id"] for item in call("replacement", "ready", page_id=page["id"])} == {build["task_id"]}
    claimed = call("replacement", "claim", operation_id=op(), task_id=build["task_id"])
    assert claimed["state"] == "claimed" and claimed["base_commit"] == "prepared-base" and claimed["work_face"] == work_face()


def test_set_work_face_is_published_through_schema_and_mcp_with_the_same_dispatcher(api):
    client, call, identities, tokens, _, _ = api
    schema = client.get("/v1/space/schema", headers={"Authorization": "Bearer " + tokens["owner"]}).json()["operations"]
    request = schema["set_work_face"]["request"]
    assert schema["set_work_face"]["mutation"] is True and request["additionalProperties"] is False
    assert set(request["required"]) == {"operation_id", "task_id", "work_face"} and "base_commit" in request["properties"]
    for name in ("create_task_block", "submit_task"):
        properties = schema[name]["request"]["properties"]
        assert {"work_face", "require_work_face", "task_kind"} <= properties.keys()
        assert not {"work_face", "require_work_face", "task_kind"} & set(schema[name]["request"].get("required", []))
    headers = {"Authorization": "Bearer " + tokens["owner"], "Host": "bridge", "Accept": "application/json, text/event-stream"}
    listed = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 41, "method": "tools/list"}).json()["result"]["tools"]
    tools = {tool["name"]: tool for tool in listed}
    assert set(tools["set_work_face"]["inputSchema"]["required"]) == {"operation_id", "task_id", "work_face"}
    assert not {"ctx", "arguments", "actor"} & tools["set_work_face"]["inputSchema"]["properties"].keys()
    assert {"work_face", "require_work_face", "task_kind"} <= tools["submit_task"]["inputSchema"]["properties"].keys()

    _, page = shared_page(call, identities)
    submitted, raw = mcp_call(client, tokens["owner"], "submit_task", {
        "idempotency_key": "mcp-work-face", "prompt": "Needs a work face", "page_id": page["id"],
        "scopes": [{"kind": "path", "value": "src/mcp"}], "assignee_principal": identities["worker"]["agent"],
        "require_work_face": True, "task_kind": "construction"})
    assert not raw.get("isError", False) and submitted["work_face_missing"] == ["base_commit", "work_face"]
    assert call("worker", "claim", 409, operation_id=op(), task_id=submitted["task_id"])["code"] == "work_face_incomplete"
    set_op = op()
    arguments = {"operation_id": set_op, "task_id": submitted["task_id"], "work_face": work_face(), "base_commit": "mcp-base"}
    first, raw = mcp_call(client, tokens["owner"], "set_work_face", arguments)
    assert not raw.get("isError", False) and first["x_protocol"] == "baton.space.v1"
    assert first["result"]["work_face_missing"] == [] and first["result"]["base_commit"] == "mcp-base" and first["result"]["replay"] is False
    replay, _ = mcp_call(client, tokens["owner"], "set_work_face", arguments, 42)
    assert replay["result"]["replay"] is True
    conflict, raw = mcp_call(client, tokens["owner"], "set_work_face", {**arguments, "base_commit": "other-base"}, 43)
    assert conflict is None and raw["content"][0]["text"].startswith("operation_conflict")
    for denied in ("worker", "stranger"):
        error, raw = mcp_call(client, tokens[denied], "set_work_face", {**arguments, "operation_id": op()}, 44)
        assert error is None and raw["content"][0]["text"].startswith("forbidden")
    invalid, raw = mcp_call(client, tokens["owner"], "set_work_face", {**arguments, "operation_id": op(), "work_face": {**work_face(), "extra": 1}}, 45)
    assert invalid is None and raw["content"][0]["text"].startswith("invalid")
    rejected, raw = mcp_call(client, tokens["owner"], "submit_task", {"idempotency_key": "mcp-extra", "prompt": "Extra", "unknown": 1}, 46)
    assert rejected is None and raw["content"][0]["text"].startswith("invalid")
    assert call("worker", "claim", operation_id=op(), task_id=submitted["task_id"])["state"] == "claimed"


def test_none_scope_over_http_lets_footprintless_manual_tasks_be_claimed_together(api):
    _, call, identities, _, _, _ = api
    _, page = shared_page(call, identities)
    reviewer = identities["reviewer"]["agent"]
    first = call("owner", "create_task_block", operation_id=op(), page_id=page["id"], prompt="Review A",
                 reviewers=[reviewer], assignee_principal=identities["worker"]["agent"])
    second = call("owner", "submit_task", operation_id=op(), idempotency_key="none-http-" + uuid.uuid4().hex, prompt="Review B",
                  page_id=page["id"], assignee_principal=identities["replacement"]["agent"])
    explicit = call("owner", "create_task_block", operation_id=op(), page_id=page["id"], prompt="Review C",
                    scopes=[{"kind": "none"}], reviewers=[reviewer])
    for task in (first, second, explicit):
        assert task["scopes"] == [{"kind": "none"}]
        assert call("owner", "x_scope", task_id=task["task_id"])["scopes"] == [{"kind": "none"}]
    assert call("worker", "claim", operation_id=op(), task_id=first["task_id"])["state"] == "claimed"
    assert call("replacement", "claim", operation_id=op(), task_id=second["task_id"])["state"] == "claimed"
    assert call("owner", "claim", operation_id=op(), task_id=explicit["task_id"])["state"] == "claimed"
    for scopes in ([{"kind": "none"}, {"kind": "path", "value": "src"}], [{"kind": "none", "value": ""}]):
        assert call("owner", "create_task_block", 422, operation_id=op(), page_id=page["id"], prompt="Bad",
                    scopes=scopes, reviewers=[reviewer])["code"] == "invalid"
