# SPDX-License-Identifier: Apache-2.0

import importlib.util
import json
import os
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

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
    assert private not in json.dumps(row) and len(row["mail_thread_ids"]) == 1
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
