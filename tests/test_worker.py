# SPDX-License-Identifier: Apache-2.0

import json
import tomllib
import uuid
from types import SimpleNamespace

import pytest
from a2a.types import Message, Role, TaskState
from starlette.testclient import TestClient


class Queue:
    def __init__(self):
        self.events = []

    async def enqueue_event(self, event):
        self.events.append(event)


def context(task_id, owner="test-client", prompt="synthetic task"):
    return SimpleNamespace(
        task_id=str(uuid.uuid4()), context_id=str(uuid.uuid4()),
        message=Message(message_id=str(uuid.uuid4()), role=Role.ROLE_USER),
        get_user_input=lambda: json.dumps({"task_id": task_id, "owner": owner, "prompt": prompt}),
    )


@pytest.mark.asyncio
async def test_demo_is_explicit_audited_and_replay_does_not_execute(modules, monkeypatch):
    common, _, worker = modules
    executor = worker.CodexExecutor("demo")

    async def forbidden(*args):
        pytest.fail("Demo must not invoke Codex")

    monkeypatch.setattr(executor, "run_codex", forbidden)
    task_id = str(uuid.uuid4())
    first = Queue()
    await executor.execute(context(task_id), first)
    artifacts = [event.artifact for event in first.events if hasattr(event, "artifact")]
    assert artifacts[0].parts[0].text == "BATON_DEMO_OK"
    assert artifacts[0].metadata["backend"] == "demo"
    assert first.events[-1].status.state == TaskState.TASK_STATE_COMPLETED
    await executor.execute(context(task_id), Queue())
    changed = Queue()
    await executor.execute(context(task_id, owner="other-client"), changed)
    assert changed.events[-1].status.state == TaskState.TASK_STATE_FAILED
    events = [json.loads(line) for line in (common.STATE / "audit.jsonl").read_text().splitlines()]
    assert [event["event"] for event in events] == ["worker.claimed", "worker.completed", "worker.replay"]
    assert all(event["backend"] == "demo" for event in events)


@pytest.mark.asyncio
async def test_running_worker_becomes_interrupted_and_never_reexecutes(modules, monkeypatch):
    common, _, worker = modules
    task_id = str(uuid.uuid4())
    with common.database("executions.sqlite3") as db:
        db.execute("INSERT INTO executions VALUES (?,?,?,?,?,?)", (task_id, "synthetic-digest", "running", None, str(uuid.uuid4()), "codex"))
    worker.init_db()
    executor = worker.CodexExecutor("codex")

    async def forbidden(*args):
        pytest.fail("Ambiguous execution must not run again")

    monkeypatch.setattr(executor, "run_codex", forbidden)
    queue = Queue()
    await executor.execute(context(task_id), queue)
    assert queue.events[-1].status.state == TaskState.TASK_STATE_FAILED
    with common.database("executions.sqlite3") as db:
        assert db.execute("SELECT status FROM executions WHERE id=?", (task_id,)).fetchone()[0] == "interrupted"


def test_codex_command_denies_credentials_and_only_writes_task_directory(modules, monkeypatch, tmp_path):
    _, _, worker = modules
    monkeypatch.delenv("CODEX_MODEL", raising=False)
    command = worker.codex_command(tmp_path, tmp_path / "result.txt")
    configs = [command[index + 1] for index, item in enumerate(command) if item == "-c"]
    config = tomllib.loads("\n".join(configs))
    profile = config["permissions"]["baton"]
    assert profile["extends"] == ":read-only"
    assert {path for path, mode in profile["filesystem"].items() if mode == "write"} == {str(tmp_path)}
    assert all(profile["filesystem"][path] == "deny" for path in ("/data", "/config", "/run/secrets", "/home/agent/.codex"))
    assert profile["network"]["enabled"] is False
    assert config["approval_policy"] == "never" and config["web_search"] == "disabled"
    assert "--model" not in command and not any("bypass" in item for item in command)
    monkeypatch.setenv("CODEX_MODEL", "synthetic-model")
    assert worker.codex_command(tmp_path, tmp_path / "result.txt")[-3:] == ["--model", "synthetic-model", "-"]


def test_worker_environment_drops_credentials_and_proxy_values(modules, monkeypatch, tmp_path):
    _, _, worker = modules
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-private-value")
    monkeypatch.setenv("HTTP_PROXY", "http://example.com:8888")
    environment = worker.codex_environment(tmp_path)
    assert "OPENAI_API_KEY" not in environment and "HTTP_PROXY" not in environment
    assert "synthetic-private-value" not in json.dumps(environment)


def test_redaction_covers_token_files_and_nested_auth(modules):
    common, _, _ = modules
    (common.CODEX_HOME / "auth.json").write_text(json.dumps({"tokens": {"access_token": "synthetic-model-credential"}}))
    value = common.redact("synthetic-model-credential synthetic-mail-credential synthetic-dispatcher-credential")
    assert value == "[REDACTED] [REDACTED] [REDACTED]"


def test_a2a_http_demo_role_and_backend_metadata(modules, monkeypatch):
    common, _, worker = modules
    monkeypatch.setattr(common, "identity", lambda value: {
        "Bearer client": {"agent": "test-client", "role": "client"},
        "Bearer dispatcher": {"agent": "codex-worker", "role": "dispatcher"},
    }.get(value))
    app = worker.create_app()
    task_id = str(uuid.uuid4())
    payload = {"jsonrpc": "2.0", "id": task_id, "method": "message/send", "params": {
        "message": {"kind": "message", "role": "user", "messageId": task_id,
                    "parts": [{"kind": "text", "text": json.dumps({"task_id": task_id, "owner": "test-client", "prompt": "synthetic task"})}]},
        "configuration": {"blocking": True},
    }}
    with TestClient(app) as client:
        assert client.post("/a2a/codex", json=payload).status_code == 401
        assert client.post("/a2a/codex", json=payload, headers={"Authorization": "Bearer client"}).status_code == 403
        response = client.post("/a2a/codex", json=payload, headers={"Authorization": "Bearer dispatcher", "A2A-Version": "0.3"})
        assert response.status_code == 200, response.text
        result = response.json()["result"]
        assert result["status"]["state"] == "completed"
        assert result["artifacts"][0]["parts"][0]["text"] == "BATON_DEMO_OK"
        assert result["artifacts"][0]["metadata"]["backend"] == "demo"
    assert "backend=demo" in worker.agent_card("demo").description
    assert "backend=codex" in worker.agent_card("codex").description


def test_worker_backend_defaults_to_codex_and_rejects_unknown(modules, monkeypatch):
    _, _, worker = modules
    monkeypatch.delenv("WORKER_MODE", raising=False)
    assert worker.worker_mode() == "codex"
    monkeypatch.setenv("WORKER_MODE", "unrecognized")
    with pytest.raises(ValueError):
        worker.worker_mode()
