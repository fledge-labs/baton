# SPDX-License-Identifier: Apache-2.0

import importlib.util
import json
import os
import stat
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("mcp_stdio", ROOT / "adapters/mcp_stdio.py")
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


def test_receipt_validates_task_ids_and_drops_arbitrary_response_values():
    private = "synthetic-not-a-uuid-secret"
    result = SimpleNamespace(isError=False, structuredContent={
        "task_id": private, "a2a_id": private, "status": private, "prompt": private, "token": private,
        "messages": [{"thread_id": private}, {"thread_id": str(uuid.uuid4()), "body_md": private}],
    })
    row = adapter.receipt("custom-agent", "fetch_inbox", {}, result)
    assert private not in json.dumps(row) and len(row["thread_ids"]) == 1
    row = adapter.receipt("custom-agent", "get_task", {"task_id": private}, result)
    assert private not in json.dumps(row)
    result.structuredContent = {"task_id": str(uuid.uuid4()), "status": "completed", "replay": True}
    row = adapter.receipt("custom-agent", "submit_task", {}, result)
    assert row["task_id"] == result.structuredContent["task_id"] and row["replay"] is True


def test_launcher_accepts_any_safe_agent_and_explicit_token_path(tmp_path):
    python = tmp_path / "synthetic-python"
    capture = tmp_path / "argv.json"
    python.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$BATON_CAPTURE"\n')
    python.chmod(0o700)
    environment = {**os.environ, "BATON_PYTHON": str(python), "BATON_CAPTURE": str(capture)}
    token_file = tmp_path / "token with spaces"
    subprocess.run([str(ROOT / "adapters/launch_mcp.sh"), "custom.agent-42", str(token_file)], env=environment, check=True)
    assert capture.read_text().splitlines()[-4:] == ["--agent-id", "custom.agent-42", "--token-file", str(token_file)]
    extra = ["--allow-tools", "fetch_inbox,claim,comment,submit_result", "--audit-file", str(tmp_path / "audit with spaces")]
    subprocess.run([str(ROOT / "adapters/launch_mcp.sh"), "custom.agent-42", str(token_file), *extra], env=environment, check=True)
    assert capture.read_text().splitlines()[-8:] == ["--agent-id", "custom.agent-42", "--token-file", str(token_file), *extra]
    for invalid in ("../escape", "-bad", "has space", "x" * 97):
        result = subprocess.run([str(ROOT / "adapters/launch_mcp.sh"), invalid, str(token_file)], env=environment, capture_output=True)
        assert result.returncode == 2


def test_safe_agent_and_optional_ca_audit_environment(monkeypatch):
    with pytest.raises(Exception):
        adapter.safe_agent("../escape")
    monkeypatch.setenv("BATON_CA_FILE", "/synthetic/ca.pem")
    monkeypatch.setenv("BATON_AUDIT_FILE", "/synthetic/receipt.jsonl")
    monkeypatch.setenv("BATON_URL", "https://baton.example.com/mcp")
    monkeypatch.setattr("sys.argv", ["mcp_stdio.py", "--agent-id", "custom-agent", "--token-file", "/synthetic/token"])
    args = adapter.parse_args()
    assert args.ca_file == Path("/synthetic/ca.pem") and args.audit_file == Path("/synthetic/receipt.jsonl")
    assert args.url == "https://baton.example.com/mcp"
    assert args.allow_tools == {"submit_task", "get_task", "list_tasks", "fetch_inbox"}


def test_explicit_allow_tools_replaces_defaults_and_deduplicates():
    args = adapter.parse_args(["--agent-id", "beigua", "--token-file", "/synthetic/token",
                               "--allow-tools", "claim, comment,submit_result,claim"])
    assert args.allow_tools == {"claim", "comment", "submit_result"}


@pytest.mark.parametrize("value", ["", ",", "claim,", "claim,,comment", "*", "claim,synthetic-private-unknown"])
def test_allow_tools_rejects_empty_wildcard_and_unknown_names(value, capsys):
    with pytest.raises(SystemExit) as error:
        adapter.parse_args(["--agent-id", "beigua", "--token-file", "/synthetic/token", "--allow-tools", value])
    assert error.value.code == 2
    output = capsys.readouterr()
    assert "synthetic-private-unknown" not in output.err
    assert output.out == ""


class SyntheticRemote:
    def __init__(self, tools):
        self.tools = tools
        self.list_calls = 0
        self.calls = []

    async def list_tools(self):
        self.list_calls += 1
        return types.ListToolsResult(tools=self.tools)

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return types.CallToolResult(content=[], structuredContent={
            "status": "claimed", "token": "synthetic-private-response", "body": "synthetic-private-response",
        })


def synthetic_tool(name, description="Synthetic tool", schema=None):
    return types.Tool(name=name, description=description, inputSchema=schema or {"type": "object"})


@pytest.mark.asyncio
async def test_mcp_discovery_returns_only_default_four_and_audits_each_filtered_response(capsys, caplog):
    private = 'synthetic-private-remote\n{"event":"forged","url":"https://example.invalid/private"}'
    defaults = {"submit_task", "get_task", "list_tasks", "fetch_inbox"}
    remote = SyntheticRemote([synthetic_tool(name) for name in sorted(defaults)] + [
        synthetic_tool("claim", private), synthetic_tool(private, private),
    ])
    server = adapter.create_proxy_server(remote, agent_id="beigua")
    async with create_connected_server_and_client_session(server) as client:
        for _ in range(2):
            result = await client.list_tools()
            assert {tool.name for tool in result.tools} == defaults
            assert private not in result.model_dump_json()
    output = capsys.readouterr()
    assert output.out == "" and private not in output.err
    assert "synthetic-private" not in caplog.text
    rows = [json.loads(line) for line in output.err.splitlines()]
    assert len(rows) == 2
    for row in rows:
        assert set(row) == {"time", "agent", "event", "rejected_count"}
        assert row["agent"] == "beigua"
        assert row["event"] == "adapter.tools_filtered"
        assert row["rejected_count"] == 2
    assert remote.list_calls == 2 and remote.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["claim", 'synthetic-private-name\n{"event":"forged"}'])
async def test_mcp_direct_call_denied_before_remote_discovery_and_validation(name, capsys, caplog):
    private = "synthetic-private-body https://example.invalid/private Bearer synthetic-private-token"
    remote = SyntheticRemote([synthetic_tool(name, private, {
        "type": "object", "required": ["synthetic-private-required-field"],
    })])
    server = adapter.create_proxy_server(remote, agent_id="beigua")
    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool(name, {"body": private})
    assert result.isError is True
    assert result.content[0].text == "Baton tool is not allowed by this adapter"
    assert remote.calls == [] and remote.list_calls == 0
    output = capsys.readouterr()
    assert output.out == ""
    assert "synthetic-private" not in output.err and "example.invalid" not in output.err
    assert "synthetic-private" not in caplog.text
    row = json.loads(output.err)
    assert set(row) == {"time", "agent", "event", "rejected_count"}
    assert row["event"] == "adapter.tool_denied" and row["rejected_count"] == 1


@pytest.mark.asyncio
async def test_mcp_default_allowed_call_reaches_remote_without_policy_rejection(capsys):
    remote = SyntheticRemote([synthetic_tool("get_task")])
    server = adapter.create_proxy_server(remote, agent_id="beigua")
    arguments = {"task_id": str(uuid.uuid4())}
    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool("get_task", arguments)
    assert result.isError is False and remote.calls == [("get_task", arguments)]
    output = capsys.readouterr()
    assert output.out == "" and output.err == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [False, True])
async def test_mcp_explicit_allowlist_forwards_named_agent_tools_and_keeps_private_audit(tmp_path, capsys, existing):
    audit_file = tmp_path / "audit.jsonl"
    if existing:
        audit_file.write_text("")
        audit_file.chmod(0o644)
    args = adapter.parse_args(["--agent-id", "beigua", "--token-file", "/synthetic/token",
                               "--allow-tools", "claim,comment,submit_result", "--audit-file", str(audit_file)])
    remote = SyntheticRemote([synthetic_tool(name) for name in
                              ("claim", "comment", "submit_result", "get_task", "synthetic-private-unknown")])
    server = adapter.create_proxy_server(remote, agent_id=args.agent_id,
                                         allow_tools=args.allow_tools, audit_file=args.audit_file)
    arguments = {"body": "synthetic-private-request", "credential": "synthetic-private-token"}
    async with create_connected_server_and_client_session(server) as client:
        tools = (await client.list_tools()).tools
        assert {tool.name for tool in tools} == {"claim", "comment", "submit_result"}
        for name in ("claim", "comment", "submit_result"):
            assert (await client.call_tool(name, arguments)).isError is False
        assert (await client.call_tool("get_task", arguments)).isError is True
    assert remote.calls == [(name, arguments) for name in ("claim", "comment", "submit_result")]
    assert stat.S_IMODE(audit_file.stat().st_mode) == 0o600
    text = audit_file.read_text()
    assert "synthetic-private" not in text
    rows = [json.loads(line) for line in text.splitlines()]
    assert rows[0]["event"] == "adapter.tools_filtered" and rows[0]["rejected_count"] == 2
    assert rows[-1]["event"] == "adapter.tool_denied" and rows[-1]["rejected_count"] == 1
    assert [row["tool"] for row in rows[1:-1]] == ["claim", "comment", "submit_result"]
    output = capsys.readouterr()
    assert output.out == "" and output.err == ""


@pytest.mark.asyncio
async def test_mcp_rejection_audit_falls_back_to_stderr_without_leaking_failed_file_path(tmp_path, capsys):
    blocker = tmp_path / "synthetic-private-path"
    blocker.write_text("synthetic obstruction")
    remote = SyntheticRemote([])
    server = adapter.create_proxy_server(remote, agent_id="beigua", audit_file=blocker / "audit.jsonl")
    async with create_connected_server_and_client_session(server) as client:
        assert (await client.call_tool("claim", {"body": "synthetic-private-body"})).isError is True
    assert remote.calls == [] and remote.list_calls == 0
    output = capsys.readouterr()
    assert output.out == "" and "synthetic-private" not in output.err
    row = json.loads(output.err)
    assert set(row) == {"time", "agent", "event", "rejected_count", "audit_file_error"}
    assert row["event"] == "adapter.tool_denied" and row["audit_file_error"] is True
