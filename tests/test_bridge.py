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
    assert {"create_space", "comment", "mention", "assign_task", "claim", "renew", "handoff", "gate", "list_audit"} <= operations.keys()
    assert not {"claim_next", "finish_auto", "recover_auto", "check_schema"} & operations.keys()
    assert operations["handoff"]["request"]["properties"]["target_principal"]["type"] == "string"
    assert "target" not in operations["handoff"]["request"]["properties"]
    error = call("owner", "create_space", 422, operation_id=op(), name="Safe", actor={"principal": private})
    assert error["code"] == "invalid" and private not in json.dumps(error)
    assert call("owner", "finish_auto", 422, operation_id=op(), result=private)["code"] == "invalid"
    headers = {"Authorization": "Bearer " + tokens["owner"], "Host": "bridge",
               "Accept": "application/json, text/event-stream"}
    response = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = {tool["name"]: tool for tool in response.json()["result"]["tools"]}
    assert {"x_claim", "x_scope", "x_handoff", "x_gate", "x_audit"} <= tools.keys()
    for name in ("create_space", "handoff", "comment", "assign_task"):
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
async def test_unknown_dispatch_is_durable_and_not_automatically_retried(modules, pg_store, make_context, monkeypatch):
    common, server, _ = modules
    task = await server.submit_task("ambiguous", "Synthetic dispatch", make_context())
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
async def test_expired_dispatch_cannot_commit_success_or_thread_report(modules, pg_store, make_context, monkeypatch):
    _, server, _ = modules
    task = await server.submit_task("late-result", "Synthetic dispatch", make_context())
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
