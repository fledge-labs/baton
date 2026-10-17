# SPDX-License-Identifier: Apache-2.0
"""Public HTTP/MCP boundaries on actual PostgreSQL."""

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
    common, server = modules
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
    common, _ = modules
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
    assert call("worker", "comment", 404, operation_id=comment_op, page_id=child["id"], body="Private comment")["code"] == "not_found"
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


def test_atomic_dependencies_keep_an_unassigned_task_unclaimable_until_the_current_gate_passes(api):
    client, call, identities, tokens, _, _ = api
    _, page = shared_page(call, identities)
    parent = call("owner", "create_task_block", operation_id=op(), page_id=page["id"], prompt="Prerequisite",
                  scopes=[{"kind": "path", "value": "src/parent"}], reviewers=[identities["owner"]["agent"]],
                  assignee_principal=identities["worker"]["agent"])
    arguments = {"idempotency_key": "atomic-open-dependent", "prompt": "Unassigned dependent", "page_id": page["id"],
                 "scopes": [{"kind": "path", "value": "src/open"}], "depends_on": [parent["task_id"]]}
    headers = {"Authorization": "Bearer " + tokens["owner"], "Host": "bridge", "Accept": "application/json, text/event-stream"}
    response = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 30, "method": "tools/call", "params": {
        "name": "submit_task", "arguments": arguments}})
    result = response.json()["result"]
    assert not result.get("isError", False)
    child = result.get("structuredContent") or json.loads(result["content"][0]["text"])
    assert child["execution_mode"] == "manual" and child["assignee"] is None and child["depends_on"] == [parent["task_id"]]
    assert call("replacement", "claim", 409, operation_id=op(), task_id=child["task_id"])["code"] == "dependency_blocked"
    claim = call("worker", "claim", operation_id=op(), task_id=parent["task_id"])
    candidate = call("worker", "submit_result", operation_id=op(), task_id=parent["task_id"], epoch=claim["epoch"], result="Candidate")
    assert candidate["state"] == "awaiting_gate"
    assert call("replacement", "claim", 409, operation_id=op(), task_id=child["task_id"])["code"] == "dependency_blocked"
    call("owner", "gate", operation_id=op(), task_id=parent["task_id"], expected_epoch=claim["epoch"], decision="pass")
    assert {task["task_id"] for task in call("replacement", "ready", page_id=page["id"])} == {child["task_id"]}
    assert call("replacement", "claim", operation_id=op(), task_id=child["task_id"])["state"] == "claimed"
    assert call("owner", "submit_task", 409, operation_id=op(), **{**arguments, "depends_on": []})["code"] == "operation_conflict"


def test_unassigned_submit_task_is_a_manual_task_any_page_editor_can_claim(api, modules):
    client, call, identities, tokens, _, _ = api
    _, page = shared_page(call, identities)
    owner = identities["owner"]["agent"]
    task = call("owner", "submit_task", operation_id=op(), idempotency_key="open-journey", prompt="Synthetic open task",
                page_id=page["id"])
    assert task["execution_mode"] == "manual" and task["gate_mode"] == "reviewer" and task["state"] == "queued"
    assert task["assignee"] is None and task["reviewers"] == [owner] and task["scopes"] == [{"kind": "none"}]
    assert task["prompt"] == "Synthetic open task" and task["respond_by"] is None
    for label in ("worker", "replacement", "reviewer"):
        assert {item["task_id"] for item in call(label, "ready", page_id=page["id"])} == {task["task_id"]}
    assert call("stranger", "ready") == []
    assert call("stranger", "ready", 404, page_id=page["id"])["code"] == "not_found"
    assert call("stranger", "claim", 404, operation_id=op(), task_id=task["task_id"])["code"] == "not_found"
    claim = call("replacement", "claim", operation_id=op(), task_id=task["task_id"])
    assert claim["state"] == "claimed" and claim["holder"] == identities["replacement"]["agent"]
    assert call("worker", "ready", page_id=page["id"]) == []
    candidate = call("replacement", "submit_result", operation_id=op(), task_id=task["task_id"], epoch=claim["epoch"], result="Open result")
    assert candidate["state"] == "awaiting_gate"
    gate = dict(task_id=task["task_id"], expected_epoch=claim["epoch"], decision="pass")
    assert call("replacement", "gate", 403, operation_id=op(), **gate)["code"] == "forbidden"
    done = call("owner", "gate", operation_id=op(), **gate)
    assert done["state"] == "done" and done["result"] == "Open result"
    events = [row["event"] for row in call("owner", "list_audit", task_id=task["task_id"])]
    assert "task.claimed" in events and not any(event.startswith("worker.") or event == "task.dispatched" for event in events)
    headers = {"Authorization": "Bearer " + tokens["owner"], "Host": "bridge", "Accept": "application/json, text/event-stream"}
    response = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 31, "method": "tools/call", "params": {
        "name": "submit_task", "arguments": {"idempotency_key": "open-mcp", "prompt": "Open over MCP", "page_id": page["id"]}}})
    result = response.json()["result"]
    assert not result.get("isError", False)
    over_mcp = result.get("structuredContent") or json.loads(result["content"][0]["text"])
    assert over_mcp["execution_mode"] == "manual" and over_mcp["assignee"] is None and over_mcp["reviewers"] == [owner]
    assert call("worker", "claim", operation_id=op(), task_id=over_mcp["task_id"])["state"] == "claimed"


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

    assert call("stranger", "set_work_face", 404, operation_id=op(), task_id=task["task_id"], work_face=work_face())["code"] == "not_found"
    assert call("worker", "set_work_face", 403, operation_id=op(), task_id=task["task_id"], work_face=work_face())["code"] == "forbidden"
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


def test_set_work_face_is_published_through_schema_and_mcp(api):
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
    for denied, code in (("worker", "forbidden"), ("stranger", "not_found")):
        error, raw = mcp_call(client, tokens[denied], "set_work_face", {**arguments, "operation_id": op()}, 44)
        assert error is None and raw["content"][0]["text"].startswith(code)
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


def judged_task(call, identities, page, suffix, reviewers=("replacement",), executor="worker", creator="owner", **extra):
    """A submitted candidate, executed by `executor`, whose configured reviewers are the named identities."""
    task = call(creator, "create_task_block", operation_id=op(), page_id=page["id"], prompt="Synthetic reviewed " + suffix,
                scopes=[{"kind": "path", "value": "src/approver-" + suffix}], reviewers=[identities[label]["agent"] for label in reviewers], **extra)
    claim = call(executor, "claim", operation_id=op(), task_id=task["task_id"])
    call(executor, "submit_result", operation_id=op(), task_id=task["task_id"], epoch=claim["epoch"], result="Candidate " + suffix)
    return task, claim


def test_approver_gate_over_http_root_delegates_and_everyone_else_needs_a_delegation(api):
    client, call, identities, tokens, _, _ = api
    space, page = shared_page(call, identities)
    agent, human = identities["replacement"]["agent"], identities["reviewer"]["agent"]
    task, claim = judged_task(call, identities, page, "http", ("replacement", "reviewer"))
    gate = dict(task_id=task["task_id"], expected_epoch=claim["epoch"], decision="pass")
    for label in ("replacement", "reviewer"):
        assert call(label, "gate", 403, operation_id=op(), **gate)["code"] == "delegation_required"
    assert call("worker", "gate", 403, operation_id=op(), **gate)["code"] == "forbidden"
    for label in ("replacement", "worker", "stranger", "reviewer"):
        target = identities["stranger" if label != "stranger" else "worker"]["agent"]
        assert call(label, "delegation_create", 403, operation_id=op(), delegate_principal=target, reason="DECISIONS 291")["code"] == "forbidden"
    assert call("replacement", "delegation_revoke", 404, operation_id=op(), delegation_id=str(uuid.uuid4()))["code"] == "not_found"
    for broken in (dict(delegate_principal="nobody-registered"), dict(delegate_principal=agent, scope="gate"), dict(delegate_principal=agent, delegate="x"),
                   dict(delegate_principal=agent, may_delegate="yes"), dict(delegate_principal=identities["owner"]["agent"])):
        assert call("owner", "delegation_create", 422, operation_id=op(), reason="DECISIONS 291", **broken)["code"] == "invalid"
    assert call("replacement", "delegation_list") == []
    root = call("owner", "delegation_create", operation_id=op(), delegate_principal=agent, space_id=space["id"], may_delegate=True, reason="DECISIONS 291")
    assert root["delegate"] == agent and root["scope"] == "approve" and root["created_via"] == "api" and root["active"] is True
    assert root["may_delegate"] is True and root["parent_id"] is None and root["chain"] == [root["id"]]
    assert call("replacement", "delegation_revoke", 403, operation_id=op(), delegation_id=root["id"])["code"] == "forbidden"
    child = call("replacement", "delegation_create", operation_id=op(), delegate_principal=human, space_id=space["id"], reason="DECISIONS 291")
    assert child["parent_id"] == root["id"] and child["grantor"] == agent and child["chain"] == [child["id"], root["id"]]
    assert call("replacement", "delegation_create", 422, operation_id=op(), delegate_principal=human, reason="wider than the parent")["code"] == "invalid"
    assert call("replacement", "delegation_create", 422, operation_id=op(), delegate_principal=identities["owner"]["agent"],
                space_id=space["id"], reason="to the root above")["code"] == "invalid"
    assert {row["id"] for row in call("replacement", "delegation_list")} == {root["id"], child["id"]}
    assert [row["id"] for row in call("reviewer", "delegation_list")] == [child["id"]]
    assert call("worker", "delegation_list") == []
    assert call("reviewer", "gate", operation_id=op(), **gate)["state"] == "done"
    reviewed = [row for row in call("owner", "list_audit", task_id=task["task_id"]) if row["event"] == "gate.reviewed"]
    assert reviewed[0]["metadata"]["chain"] == [child["id"], root["id"]] and reviewed[0]["metadata"]["grantor"] == agent
    second, second_claim = judged_task(call, identities, page, "http-revoked", ("replacement", "reviewer"))
    assert call("owner", "delegation_revoke", operation_id=op(), delegation_id=root["id"])["active"] is False
    for label in ("replacement", "reviewer"):
        again = call(label, "gate", 403, operation_id=op(), task_id=second["task_id"], expected_epoch=second_claim["epoch"], decision="pass")
        assert again["code"] == "delegation_required"
    third, third_claim = judged_task(call, identities, page, "http-root", ("replacement",))
    rooted = call("owner", "gate", operation_id=op(), task_id=third["task_id"], expected_epoch=third_claim["epoch"], decision="pass")
    assert rooted["state"] == "done"
    audit = [row for row in call("owner", "list_audit", task_id=third["task_id"]) if row["event"] == "gate.reviewed"]
    assert audit[0]["metadata"]["root"] is True and audit[0]["metadata"]["self_approval"] is False and audit[0]["metadata"]["chain"] == []
    schema = client.get("/v1/space/schema", headers={"Authorization": "Bearer " + tokens["owner"]}).json()["operations"]
    assert {"delegation_create", "delegation_revoke", "delegation_list"} <= schema.keys()
    assert "delegate" not in schema["delegation_create"]["request"]["properties"]
    assert "delegate_principal" in schema["delegation_create"]["request"]["required"]
    assert "self_approval" in schema["gate"]["request"]["properties"] and "approvals_required" in schema["create_task_block"]["request"]["properties"]


def test_gate_edit_decision_and_approver_identity_over_http(api):
    client, call, identities, tokens, _, _ = api
    space, page = shared_page(call, identities)
    owner, reviewer = identities["owner"]["agent"], identities["reviewer"]["agent"]
    task, claim = judged_task(call, identities, page, "edit-http", ("reviewer",))
    gate = dict(task_id=task["task_id"], expected_epoch=claim["epoch"])
    missing = call("owner", "gate", 422, operation_id=op(), decision="edit", **gate)
    assert missing["code"] == "invalid" and missing["details"][0]["field"] == "edited_result"
    stray = call("owner", "gate", 422, operation_id=op(), decision="pass", edited_result="Nope", **gate)
    assert stray["code"] == "invalid" and stray["details"][0]["field"] == "edited_result"
    assert call("owner", "gate", 422, operation_id=op(), decision="edit", edited_result=7, **gate)["code"] == "invalid"
    assert call("owner", "gate", 422, operation_id=op(), decision="approve", **gate)["code"] == "invalid"
    assert call("reviewer", "gate", 403, operation_id=op(), decision="edit", edited_result="Sneaky", **gate)["code"] == "delegation_required"
    assert call("worker", "gate", 403, operation_id=op(), decision="edit", edited_result="Sneaky", **gate)["code"] == "forbidden"
    assert call("owner", "get_task", task_id=task["task_id"])["result"] == "Candidate edit-http"
    edit_op = op()
    done = call("owner", "gate", operation_id=edit_op, decision="edit", edited_result="Edited by the owner", reason="Clarified", **gate)
    assert done["state"] == "done" and done["result"] == "Edited by the owner" and done["original_result"] == "Candidate edit-http"
    assert done["gate"]["authority"] == "root" and done["gate"]["approver"] == owner and done["gate"]["decision"] == "edit"
    assert done["gate"]["counted_as"] == "pass" and done["gate"]["edited"] is True
    again = call("owner", "gate", operation_id=edit_op, decision="edit", edited_result="Edited by the owner", reason="Clarified", **gate)
    assert again["replay"] is True and again["gate"] == done["gate"]
    event = next(row for row in call("owner", "list_audit", task_id=task["task_id"]) if row["event"] == "gate.result_overridden")
    assert event["metadata"]["approver"] == owner and event["metadata"]["reason"] == "Clarified"
    assert event["metadata"]["original_sha256"] != event["metadata"]["edited_sha256"]
    assert "Edited by the owner" not in json.dumps(event) and "Candidate edit-http" not in json.dumps(event)

    call("owner", "delegation_create", operation_id=op(), delegate_principal=reviewer, reason="DECISIONS 291")
    delegated, delegated_claim = judged_task(call, identities, page, "who-http", ("reviewer",))
    passed = call("reviewer", "gate", operation_id=op(), task_id=delegated["task_id"], expected_epoch=delegated_claim["epoch"], decision="pass")
    grant = next(row for row in call("reviewer", "delegation_list") if row["delegate"] == reviewer)
    assert passed["gate"]["authority"] == f"delegated by {owner} via {grant['id']}" and passed["gate"]["approver"] == reviewer
    own, own_claim = judged_task(call, identities, page, "who-self-http", ("reviewer",), executor="owner")
    selfie = call("owner", "gate", operation_id=op(), task_id=own["task_id"], expected_epoch=own_claim["epoch"], decision="pass", self_approval=True)
    assert selfie["gate"]["authority"] == "root (self-approval)"
    properties = client.get("/v1/space/schema", headers={"Authorization": "Bearer " + tokens["owner"]}).json()["operations"]["gate"]["request"]["properties"]
    assert {"edited_result", "reason"} <= properties.keys()


def test_root_self_approval_flag_and_non_root_refusals_over_http(api):
    _, call, identities, _, _, _ = api
    space, page = shared_page(call, identities)
    owner_task, owner_claim = judged_task(call, identities, page, "self", ("reviewer",), executor="owner")
    gate = dict(task_id=owner_task["task_id"], expected_epoch=owner_claim["epoch"], decision="pass")
    assert call("owner", "gate", 403, operation_id=op(), **gate)["code"] == "forbidden"
    assert call("owner", "gate", 403, operation_id=op(), self_approval=False, **gate)["code"] == "forbidden"
    assert call("owner", "gate", 422, operation_id=op(), self_approval="yes", **gate)["code"] == "invalid"
    assert call("owner", "gate", operation_id=op(), self_approval=True, **gate)["state"] == "done"
    event = next(row for row in call("owner", "list_audit", task_id=owner_task["task_id"]) if row["event"] == "gate.reviewed")
    assert event["metadata"]["self_approval"] is True and event["metadata"]["root"] is True
    call("owner", "delegation_create", operation_id=op(), delegate_principal=identities["worker"]["agent"], reason="DECISIONS 291")
    task, claim = judged_task(call, identities, page, "worker-self", ("worker",))
    for flag in (False, True):
        assert call("worker", "gate", 403, operation_id=op(), self_approval=flag, task_id=task["task_id"],
                    expected_epoch=claim["epoch"], decision="pass")["code"] == "forbidden"


def test_two_party_confirmation_over_http(api, pg_store):
    _, call, identities, _, _, _ = api
    space, page = shared_page(call, identities)
    for label in ("replacement", "reviewer"):
        call("owner", "delegation_create", operation_id=op(), delegate_principal=identities[label]["agent"], reason="DECISIONS 291")
    task, claim = judged_task(call, identities, page, "two", ("replacement", "reviewer"), approvals_required=2)
    gate = dict(task_id=task["task_id"], expected_epoch=claim["epoch"], decision="pass")
    first = call("replacement", "gate", operation_id=op(), **gate)
    assert first["state"] == "awaiting_gate" and first["approvals_required"] == 2 and first["approvals_passed"] == 1 and first["status"] != "completed"
    assert call("replacement", "gate", 403, operation_id=op(), **gate)["code"] == "forbidden"
    assert call("worker", "get_task", task_id=task["task_id"])["approvals_required"] == 2
    assert call("reviewer", "gate", operation_id=op(), **gate)["state"] == "done"
    assert call("owner", "create_task_block", 422, operation_id=op(), page_id=page["id"], prompt="Synthetic bad", approvals_required=3,
                scopes=[{"kind": "path", "value": "src/bad"}], reviewers=[identities["reviewer"]["agent"]])["code"] == "invalid"
    pg_store.set_approval_policy(space["id"], "path", "src/approver-rooted", 1, True, "operator-a")
    rooted, rooted_claim = judged_task(call, identities, page, "rooted", ("replacement",))
    held = call("replacement", "gate", operation_id=op(), task_id=rooted["task_id"], expected_epoch=rooted_claim["epoch"], decision="pass")
    assert held["state"] == "awaiting_gate" and held["root_required"] is True
    assert call("owner", "gate", operation_id=op(), task_id=rooted["task_id"], expected_epoch=rooted_claim["epoch"], decision="fail")["state"] == "queued"


def run_delegations(*argv):
    import subprocess
    import sys
    done = subprocess.run([sys.executable, "-m", "bridge.delegations", *argv], capture_output=True, text=True,
                          cwd=Path(__file__).parents[1], timeout=60)
    return done.returncode, done.stdout, done.stderr


def test_operator_cli_records_the_space_owner_as_root_grantor_and_round_trips(api, modules, pg_store):
    client, call, identities, tokens, _, _ = api
    space, page = shared_page(call, identities)
    judge = identities["replacement"]
    owner_name, owner_account, operator = identities["owner"]["agent"], identities["owner"]["account"], "operator-anat"
    base = ["--grantor", owner_name, "--grantor-account", owner_account, "--delegate", judge["agent"],
            "--delegate-account", judge["account"], "--scope", "approve", "--reason", "DECISIONS 291", "--operator", operator]
    for broken in (["--delegate", "unregistered-agent"], ["--delegate-account", "wrong-account"], ["--grantor", identities["worker"]["agent"]],
                   ["--grantor", "another-person-on-the-owner-account"], ["--grantor-kind", "agent"],
                   ["--grantor-account", identities["worker"]["account"], "--space", space["id"]], ["--space", str(uuid.uuid4())],
                   ["--expires", "2001-01-01T00:00:00+00:00"], ["--reason", "x" * 501],
                   ["--grantor-account", judge["account"]], ["--grantor-kind", "agent", "--grantor", identities["reviewer"]["agent"]]):
        code, out, err = run_delegations("create", *base, *broken)
        assert code == 1 and out == "" and json.loads(err)["error"] in {"invalid", "not_found"}, broken
    assert run_delegations("create", *base[:-2])[0] == 2
    assert run_delegations("create", *[a if a != "approve" else "gate" for a in base])[0] == 2
    assert run_delegations("list")[1].strip() == "[]"

    code, out, err = run_delegations("create", *base, "--space", space["id"], "--expires", "2099-01-01T00:00:00Z", "--may-delegate")
    assert code == 0 and err == ""
    created = json.loads(out)
    assert created["created_via"] == "operator" and created["created_by"] == operator and created["grantor"] == owner_name
    assert created["grantor_kind"] == "human" and created["parent_id"] is None and created["may_delegate"] is True
    assert created["delegate"] == judge["agent"] and created["space_id"] == space["id"] and created["chain"] == [created["id"]]
    assert created["scope"] == "approve" and created["active"] is True and created["revoked_at"] is None
    for secret in ("password", "token", "hash", "secret"):
        assert secret not in out.lower()
    assert [row["id"] for row in json.loads(run_delegations("list")[1])] == [created["id"]]
    assert [row["id"] for row in call("replacement", "delegation_list")] == [created["id"]]
    assert json.loads(run_delegations("list", "--active")[1])[0]["id"] == created["id"]

    child = call("replacement", "delegation_create", operation_id=op(), delegate_principal=identities["reviewer"]["agent"],
                 space_id=space["id"], expires_at="2098-01-01T00:00:00Z", reason="DECISIONS 291")
    assert child["chain"] == [child["id"], created["id"]]
    task, claim = judged_task(call, identities, page, "operator", ("replacement", "reviewer"))
    assert call("replacement", "gate", operation_id=op(), task_id=task["task_id"], expected_epoch=claim["epoch"], decision="pass")["state"] == "done"
    audit = call("owner", "list_audit", space_id=space["id"], limit=1000)
    gate = next(row for row in audit if row["event"] == "gate.reviewed" and row["task_id"] == task["task_id"])
    assert gate["metadata"]["delegation_id"] == created["id"] and gate["metadata"]["grantor"] == owner_name and gate["metadata"]["chain"] == [created["id"]]
    event = next(row for row in audit if row["event"] == "delegation.created" and row["metadata"]["delegation_id"] == created["id"])
    assert event["requested_by"] == owner_name and event["performed_by"] == "operator:" + operator
    assert event["agent"] == owner_name and event["actor_account"] == owner_account and event["actor_kind"] == "human"
    assert event["metadata"] == {"delegation_id": created["id"], "scope": "approve", "created_via": "operator", "chain": [created["id"]]}

    code, out, err = run_delegations("policy-set", "--space", space["id"], "--scope-value", "src/approver-policy", "--approvals", "2",
                                     "--root-required", "--operator", operator)
    assert code == 0 and json.loads(out)["root_required"] is True
    listed = json.loads(run_delegations("policy-list", "--space", space["id"])[1])
    assert [(row["scope_kind"], row["scope_value"], row["required_approvals"]) for row in listed] == [("path", "src/approver-policy", 2)]
    guarded, guarded_claim = judged_task(call, identities, page, "policy", ("replacement",))
    view = call("owner", "get_task", task_id=guarded["task_id"])
    assert view["approvals_required"] == 2 and view["root_required"] is True
    assert run_delegations("policy-set", "--space", space["id"], "--approvals", "3", "--operator", operator)[0] == 2
    assert run_delegations("policy-remove", "--space", space["id"], "--scope-value", "src/approver-policy", "--operator", operator)[0] == 0
    assert run_delegations("policy-remove", "--space", space["id"], "--scope-value", "src/approver-policy", "--operator", operator)[0] == 1
    assert json.loads(run_delegations("policy-list")[1]) == []

    second, second_claim = judged_task(call, identities, page, "operator-revoked", ("replacement", "reviewer"))
    code, out, err = run_delegations("revoke", "--id", created["id"], "--operator", "operator-bee")
    revoked = json.loads(out)
    assert code == 0 and revoked["active"] is False and revoked["revoked_by"] == "operator:operator-bee"
    assert run_delegations("revoke", "--id", created["id"], "--operator", "operator-bee")[0] == 0
    assert json.loads(run_delegations("list", "--active")[1]) == []
    for label in ("replacement", "reviewer"):
        refused = call(label, "gate", 403, operation_id=op(), task_id=second["task_id"], expected_epoch=second_claim["epoch"], decision="pass")
        assert refused["code"] == "delegation_required"
    audit = call("owner", "list_audit", space_id=space["id"], limit=1000)
    events = [row for row in audit if row["event"] == "delegation.revoked"]
    assert len(events) == 1 and events[0]["performed_by"] == "operator:operator-bee" and events[0]["requested_by"] == owner_name
    assert run_delegations("revoke", "--id", str(uuid.uuid4()), "--operator", operator)[0] == 1
    assert run_delegations("revoke", "--id", "not-a-uuid", "--operator", operator)[0] == 1
    assert run_delegations("revoke", "--id", created["id"])[0] == 2


def test_operator_cli_sets_the_root_identity_of_a_space_that_has_none(api, modules, pg_store):
    _, call, identities, _, _, _ = api
    owner = identities["owner"]
    space_id = str(uuid.uuid4())
    with pg_store._transaction(write=True) as conn:
        conn.execute("INSERT INTO baton.spaces(id,name,owner_account) VALUES (%s,'Legacy space',%s)", (space_id, owner["account"]))
    assert call("owner", "get_space", space_id=space_id)["owner_principal"] is None
    base = ["--space", space_id, "--operator", "operator-anat"]
    for broken in (["--principal", identities["worker"]["agent"], "--account", owner["account"], "--kind", "agent"],
                   ["--principal", owner["agent"], "--account", identities["reviewer"]["account"], "--kind", "human"],
                   ["--principal", owner["agent"], "--account", owner["account"], "--kind", "agent"]):
        code, out, err = run_delegations("set-space-owner", *base, *broken)
        assert code == 1 and out == "" and json.loads(err)["error"] == "invalid"
    code, out, err = run_delegations("set-space-owner", *base, "--principal", owner["agent"], "--account", owner["account"], "--kind", "human")
    assert code == 0 and err == ""
    updated = json.loads(out)
    assert (updated["owner_principal"], updated["owner_account"], updated["owner_kind"]) == (owner["agent"], owner["account"], "human")
    assert call("owner", "get_space", space_id=space_id)["owner_principal"] == owner["agent"]
    events = [row for row in call("owner", "list_audit", space_id=space_id) if row["event"] == "space.owner_set"]
    assert len(events) == 1 and events[0]["performed_by"] == "operator:operator-anat"


def test_approver_validation_errors_carry_field_details_and_delegation_required_does_not(api):
    _, call, identities, _, _, _ = api
    space, page = shared_page(call, identities)
    agent = identities["replacement"]["agent"]

    def details(label, operation, **body):
        error = call(label, operation, 422, operation_id=op(), **body)
        assert error["code"] == "invalid"
        return {(item["field"], item["expected"]) for item in error["details"]}

    assert ("scope", "approve") in details("owner", "delegation_create", delegate_principal=agent, scope="gate", reason="DECISIONS 291")
    assert any(field == "reason" for field, _ in details("owner", "delegation_create", delegate_principal=agent, reason="x" * 501))
    assert any(field == "expires_at" for field, _ in details("owner", "delegation_create", delegate_principal=agent, reason="r", expires_at="2099-01-01T00:00:00"))
    assert any(field == "delegate_principal" for field, _ in details("owner", "delegation_create", delegate_principal="nobody-registered", reason="r"))
    assert any(field == "delegate_principal" for field, _ in details("owner", "delegation_create", delegate_principal=identities["owner"]["agent"], reason="r"))
    assert any(field == "delegation_id" for field, _ in details("owner", "delegation_revoke", delegation_id="not-a-uuid"))
    assert any(field == "approvals_required" for field, _ in details("owner", "create_task_block", page_id=page["id"], prompt="Synthetic", approvals_required=3,
                                                                     scopes=[{"kind": "path", "value": "src/x"}], reviewers=[agent]))
    task, claim = judged_task(call, identities, page, "details", ("replacement",))
    assert any(field == "self_approval" for field, _ in details("owner", "gate", task_id=task["task_id"], expected_epoch=claim["epoch"],
                                                                 decision="pass", self_approval="yes"))
    refused = call("replacement", "gate", 403, operation_id=op(), task_id=task["task_id"], expected_epoch=claim["epoch"], decision="pass")
    assert refused["code"] == "delegation_required" and "details" not in refused
