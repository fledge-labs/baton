# SPDX-License-Identifier: Apache-2.0
"""DEP-1: document-collaboration operations are marked deprecated without changing what they do."""

import importlib.util
import json
import uuid
from pathlib import Path

import pytest
from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

from test_bridge import api, op, shared_page  # noqa: F401  (fixture reuse)

ROOT = Path(__file__).parents[1]
NOTICE = "[deprecated] Baton no longer develops document collaboration; use tasks, comments and links instead."
LINK = '<docs/api.md#deprecated>; rel="deprecation"'
DEPRECATED_TOOLS = {"create_block", "get_block", "list_blocks", "update_block", "revert_block", "proposal_create",
                    "proposal_review", "get_proposal", "list_proposals", "list_attribution"}

spec = importlib.util.spec_from_file_location("mcp_stdio", ROOT / "adapters/mcp_stdio.py")
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


def make_post(client, tokens):
    def post(label, operation, expected=200, **body):
        response = client.post("/v1/space/" + operation, json=body, headers={"Authorization": "Bearer " + tokens[label]})
        assert response.status_code == expected, response.text
        return response
    return post


def is_marked(response):
    marked = response.headers.get("deprecation") == "true" and response.headers.get("link") == LINK
    assert marked or ("deprecation" not in response.headers and "link" not in response.headers)
    assert "sunset" not in response.headers
    return marked


def mcp_call(client, tokens, label, name, arguments):
    headers = {"Authorization": "Bearer " + tokens[label], "Host": "bridge", "Accept": "application/json, text/event-stream"}
    return client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                      "params": {"name": name, "arguments": arguments}})


def test_deprecated_operations_still_succeed_and_carry_headers(api, capsys):
    client, call, identities, tokens, _, _ = api
    post = make_post(client, tokens)
    _, page = shared_page(call, identities)

    block = post("owner", "create_block", operation_id=op(), page_id=page["id"], kind="text", content={"text": "v1"})
    assert is_marked(block) and block.json()["result"]["kind"] == "text" and block.json()["result"]["revision"] == 1
    block_id = block.json()["result"]["id"]
    assert is_marked(post("worker", "get_block", block_id=block_id))
    listing = post("worker", "list_blocks", page_id=page["id"])
    assert is_marked(listing) and [row["id"] for row in listing.json()["result"]] == [block_id]

    updated = post("owner", "update_block", operation_id=op(), block_id=block_id, expected_revision=1, content={"text": "v2"})
    assert is_marked(updated) and updated.json()["result"]["revision"] == 2
    proposal = post("worker", "proposal_create", operation_id=op(), block_id=block_id, expected_revision=2,
                    proposed_content={"text": "v3"}, selection={"start": 0, "end": 1})
    assert is_marked(proposal) and proposal.json()["result"]["status"] == "pending"
    proposal_id = proposal.json()["result"]["id"]
    assert is_marked(post("worker", "get_proposal", proposal_id=proposal_id))
    assert is_marked(post("worker", "list_proposals", block_id=block_id))
    reviewed = post("owner", "proposal_review", operation_id=op(), proposal_id=proposal_id, decision="accept")
    assert is_marked(reviewed) and reviewed.json()["result"]["status"] == "accepted"
    attribution = post("worker", "list_attribution", block_id=block_id)
    assert is_marked(attribution) and [row["revision"] for row in attribution.json()["result"]] == [1, 2, 3]
    reverted = post("owner", "revert_block", operation_id=op(), block_id=block_id, expected_revision=3, target_revision=1)
    assert is_marked(reverted) and reverted.json()["result"]["revision"] == 4
    assert reverted.json()["result"]["content"] == {"text": "v1"}

    assert is_marked(post("worker", "comment", operation_id=op(), page_id=page["id"], body="Plain")) is False
    selected = post("worker", "comment", operation_id=op(), page_id=page["id"], body="Selected", selection={"start": 0})
    assert is_marked(selected) and selected.json()["result"]["body"] == "Selected"

    failing = post("owner", "update_block", 409, operation_id=op(), block_id=block_id, expected_revision=1, content={"text": "stale"})
    assert is_marked(failing) and failing.json()["error"]["code"] == "revision_conflict"
    assert is_marked(post("owner", "create_block", 422, operation_id=op(), page_id=page["id"], kind="task", content={})) is False


def test_block_read_and_revert_failures_carry_no_deprecation_marker(api):
    client, call, identities, tokens, _, _ = api
    post = make_post(client, tokens)
    _, page = shared_page(call, identities)
    missing = str(uuid.uuid4())
    for operation, status, body in (
        ("get_block", 404, {"block_id": missing}),
        ("revert_block", 404, {"operation_id": op(), "block_id": missing, "expected_revision": 1, "target_revision": 1}),
        ("list_blocks", 404, {"page_id": missing}),
    ):
        response = post("owner", operation, status, **body)
        assert is_marked(response) is False
    block = post("owner", "create_block", operation_id=op(), page_id=page["id"], kind="text", content={"text": "secret"})
    block_id = block.json()["result"]["id"]
    forbidden = post("stranger", "get_block", 403, block_id=block_id)
    assert is_marked(forbidden) is False
    revert_forbidden = post("worker", "revert_block", 403, operation_id=op(), block_id=block_id,
                            expected_revision=block.json()["result"]["revision"], target_revision=1)
    assert is_marked(revert_forbidden) is False


def test_http_and_mcp_validation_failures_on_deprecated_calls_still_carry_headers(api):
    client, call, identities, tokens, _, _ = api
    post = make_post(client, tokens)
    _, page = shared_page(call, identities)
    invalid_http = post("owner", "update_block", 422, operation_id=op(), block_id=str(uuid.uuid4()))
    assert is_marked(invalid_http) and invalid_http.json()["error"]["code"] == "invalid"
    assert is_marked(post("owner", "list_attribution", 422, block_id="not-a-uuid"))
    assert is_marked(post("worker", "comment", 422, operation_id=op(), page_id=page["id"], selection={"start": 0}))
    assert is_marked(post("worker", "comment", 422, operation_id=op(), page_id=page["id"], body="x", selection={"start": 0}))

    invalid_mcp = mcp_call(client, tokens, "owner", "update_block", {"operation_id": op()})
    assert invalid_mcp.json()["result"]["isError"] is True and is_marked(invalid_mcp)
    assert is_marked(mcp_call(client, tokens, "owner", "proposal_create", {"operation_id": op()}))
    assert is_marked(mcp_call(client, tokens, "owner", "comment", {
        "operation_id": op(), "page_id": page["id"], "body": "x", "selection": {"start": 0}}))
    assert is_marked(mcp_call(client, tokens, "owner", "comment", {
        "operation_id": op(), "page_id": page["id"], "selection": {"start": 0}}))


def test_task_blocks_read_through_get_block_and_list_blocks_have_no_marker(api):
    client, call, identities, tokens, _, _ = api
    post = make_post(client, tokens)
    _, page = shared_page(call, identities)
    task = post("owner", "create_task_block", operation_id=op(), page_id=page["id"], prompt="Synthetic task",
                reviewers=[identities["reviewer"]["agent"]])
    assert is_marked(task) is False
    block_id = task.json()["result"]["block_id"]
    fetched = post("worker", "get_block", block_id=block_id)
    assert fetched.json()["result"]["kind"] == "task" and is_marked(fetched) is False
    listing = post("worker", "list_blocks", page_id=page["id"])
    assert [row["kind"] for row in listing.json()["result"]] == ["task"] and is_marked(listing) is False

    text = post("owner", "create_block", operation_id=op(), page_id=page["id"], kind="text", content={"text": "note"})
    mixed = post("worker", "list_blocks", page_id=page["id"])
    assert is_marked(mixed) and [row["kind"] for row in mixed.json()["result"]] == ["task", "text"]
    assert is_marked(post("worker", "get_block", block_id=text.json()["result"]["id"]))
    assert is_marked(post("worker", "get_block", block_id=block_id)) is False
    assert is_marked(post("owner", "update_block", 403, operation_id=op(), block_id=block_id, expected_revision=1, content={}))


def test_non_deprecated_operations_have_no_deprecation_header(api, modules, a2a, receiver):
    client, call, identities, tokens, _, _ = api
    post = make_post(client, tokens)
    space, page = shared_page(call, identities)
    comment = post("worker", "comment", operation_id=op(), page_id=page["id"], body="Plain comment")
    task = post("owner", "submit_task", operation_id=op(), idempotency_key="dep-" + uuid.uuid4().hex[:8], prompt="Synthetic",
                page_id=page["id"], assignee_principal=identities["worker"]["agent"])
    responses = [
        task, comment,
        post("owner", "create_space", operation_id=op(), name="Other"),
        post("owner", "get_space", space_id=space["id"]),
        post("owner", "list_spaces"),
        post("owner", "create_page", operation_id=op(), space_id=space["id"], title="Channel"),
        post("owner", "get_page", page_id=page["id"]),
        post("owner", "list_pages", space_id=space["id"]),
        post("owner", "share", operation_id=op(), resource_type="page", resource_id=page["id"],
             subject_type="person", subject_id=identities["stranger"]["account"], role="view"),
        post("owner", "effective_role", resource_type="page", resource_id=page["id"]),
        post("owner", "create_team", operation_id=op(), name="Team"),
        post("owner", "list_comments", page_id=page["id"]),
        post("reviewer", "mention", operation_id=op(), comment_id=comment.json()["result"]["id"], target_type="agent",
             target_id=identities["worker"]["agent"]),
        post("owner", "get_task", task_id=task.json()["result"]["task_id"]),
        post("owner", "list_tasks"),
        post("worker", "fetch_inbox"),
        post("owner", "ready"),
        post("owner", "list_audit", space_id=space["id"]),
        post("owner", "webhook_list"),
        post("owner", "delegation_list"),
        post("owner", "get_page", 422, page_id="not-a-uuid"),
        client.get("/v1/space/schema", headers={"Authorization": "Bearer " + tokens["owner"]}),
        client.get("/board/data", headers={"Authorization": "Bearer " + tokens["owner"]}),
    ]
    subscription = call("worker", "webhook_subscribe", operation_id=op(), url=receiver.url(), events=["task.assigned"])
    responses += [
        post("worker", "webhook_rotate_secret", operation_id=op(), subscription_id=subscription["subscription_id"]),
        post("worker", "webhook_revoke_previous_secret", operation_id=op(), subscription_id=subscription["subscription_id"]),
    ]
    a2a_client, _, _, a2a_tokens, _ = a2a
    responses += [
        a2a_client.get("/a2a/agents", headers={"Authorization": "Bearer " + a2a_tokens["owner"]}),
        a2a_client.post("/a2a/rpc", json={"jsonrpc": "2.0", "id": 1, "method": "GetTask", "params": {"id": str(uuid.uuid4())}},
                        headers={"Authorization": "Bearer " + a2a_tokens["owner"]}),
    ]
    for response in responses:
        assert "deprecation" not in response.headers and "link" not in response.headers


def test_mcp_tool_descriptions_and_responses_carry_the_marker(api):
    client, call, identities, tokens, _, _ = api
    headers = {"Authorization": "Bearer " + tokens["owner"], "Host": "bridge", "Accept": "application/json, text/event-stream"}
    response = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert "deprecation" not in response.headers
    tools = {tool["name"]: tool["description"] for tool in response.json()["result"]["tools"]}
    for name in DEPRECATED_TOOLS:
        assert tools[name].startswith(NOTICE), name
    for name in ("create_block", "get_block", "list_blocks", "revert_block"):
        assert "task blocks (kind='task') stay supported" in tools[name]
    assert "selection parameter is deprecated" in tools["comment"] and not tools["comment"].startswith(NOTICE)
    for name, description in tools.items():
        if name not in DEPRECATED_TOOLS and name != "comment":
            assert "deprecated" not in description.lower(), name

    _, page = shared_page(call, identities)
    block = mcp_call(client, tokens, "owner", "create_block",
                     {"operation_id": op(), "page_id": page["id"], "kind": "text", "content": {"text": "mcp"}})
    assert is_marked(block) and block.json()["result"].get("isError", False) is False
    result = block.json()["result"]
    content = (result.get("structuredContent") or json.loads(result["content"][0]["text"]))["result"]
    assert is_marked(mcp_call(client, tokens, "owner", "get_block", {"block_id": content["id"]}))
    assert is_marked(mcp_call(client, tokens, "owner", "update_block", {
        "operation_id": op(), "block_id": content["id"], "expected_revision": 1, "content": {"text": "mcp2"}}))
    assert is_marked(mcp_call(client, tokens, "owner", "comment", {
        "operation_id": op(), "page_id": page["id"], "body": "Selected", "selection": {"start": 0}}))

    assert is_marked(mcp_call(client, tokens, "owner", "comment", {
        "operation_id": op(), "page_id": page["id"], "body": "Plain"})) is False
    assert is_marked(mcp_call(client, tokens, "owner", "list_tasks", {})) is False
    task = call("owner", "create_task_block", operation_id=op(), page_id=page["id"], prompt="Task",
                reviewers=[identities["reviewer"]["agent"]])
    assert is_marked(mcp_call(client, tokens, "owner", "get_block", {"block_id": task["block_id"]})) is False


def test_each_deprecated_call_logs_metadata_only_and_counts(api, modules, capsys):
    client, call, identities, tokens, _, _ = api
    _, server = modules
    post = make_post(client, tokens)
    _, page = shared_page(call, identities)
    private = "synthetic-private-document-body"
    capsys.readouterr()
    block = post("owner", "create_block", operation_id=op(), page_id=page["id"], kind="text", content={"text": private})
    block_id = block.json()["result"]["id"]
    post("owner", "get_block", block_id=block_id)
    post("owner", "get_block", block_id=block_id)
    post("owner", "comment", operation_id=op(), page_id=page["id"], body=private, selection={"quote": private})
    post("owner", "comment", operation_id=op(), page_id=page["id"], body=private)
    post("owner", "list_tasks")
    output = capsys.readouterr().out
    assert private not in output and tokens["owner"] not in output
    rows = [json.loads(line) for line in output.splitlines() if '"deprecated.call"' in line]
    assert [(row["operation"], row["kind"]) for row in rows] == [
        ("create_block", "text"), ("get_block", "text"), ("get_block", "text"), ("comment", "selection")]
    for row in rows:
        assert row["principal"] == identities["owner"]["agent"]
        assert set(row) == {"time", "event", "operation", "principal", "kind", "metric", "value"}
        assert row["metric"] == "baton_deprecated_calls_total"
    assert server.DEPRECATED_CALLS == {"create_block": 1, "get_block": 2, "comment": 1}
    assert 'baton_deprecated_calls_total{operation="get_block"} 2' in server.deprecated_metrics()


def test_log_kind_never_echoes_untrusted_values(modules):
    _, server = modules
    assert server.log_kind("text") == "text"
    assert server.log_kind("x\n{\"forged\":1}") == "other" and server.log_kind(None) == "other"
    assert server.deprecated_kind("create_block", {"kind": "task"}, None, True) is None
    assert server.deprecated_kind("create_block", {"kind": "evil\nline"}, None, True) == "other"
    assert server.deprecated_kind("list_blocks", {}, [{"kind": "task"}], False) is None
    assert server.deprecated_kind("list_blocks", {}, [{"kind": "task"}, {"kind": "text"}, {"kind": "file"}], False) == "file,text"


def test_adapter_marks_deprecated_tool_descriptions_and_matches_the_bridge(modules):
    _, server = modules
    assert adapter.DEPRECATION_NOTICE == NOTICE == server.DEPRECATION_NOTICE
    assert adapter.DEPRECATED_TOOLS == server.DEPRECATED_TOOLS == DEPRECATED_TOOLS
    assert DEPRECATED_TOOLS <= adapter.TOOLS


class Remote:
    def __init__(self, tools):
        self.tools = tools

    async def list_tools(self):
        return types.ListToolsResult(tools=self.tools)


@pytest.mark.asyncio
async def test_adapter_discovery_prefixes_deprecated_tools_only_once():
    def tool(name, description):
        return types.Tool(name=name, description=description, inputSchema={"type": "object"})

    remote = Remote([tool("update_block", "Plain remote text"), tool("get_block", NOTICE + " Already marked."),
                     tool("comment", "Comment"), tool("get_task", "Task"), tool("list_attribution", None)])
    allowed = adapter.parse_allow_tools("update_block,get_block,comment,get_task,list_attribution")
    server = adapter.create_proxy_server(remote, agent_id="dep-agent", allow_tools=allowed)
    async with create_connected_server_and_client_session(server) as client:
        descriptions = {item.name: item.description for item in (await client.list_tools()).tools}
    assert descriptions["update_block"] == NOTICE + " Plain remote text"
    assert descriptions["get_block"] == NOTICE + " Already marked."
    assert descriptions["list_attribution"] == NOTICE
    assert descriptions["comment"] == "Comment" and descriptions["get_task"] == "Task"
