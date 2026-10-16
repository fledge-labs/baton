# SPDX-License-Identifier: Apache-2.0

import hashlib
import importlib
import json
import time
import uuid

import pytest
from starlette.testclient import TestClient


@pytest.mark.asyncio
async def test_idempotency_is_stable_and_owner_scoped(modules, pg_store, make_context):
    _, server, _ = modules
    first = await server.submit_task("stable-key", "test prompt", make_context())
    replay = await server.submit_task("stable-key", "test prompt", make_context())
    assert first["task_id"] == replay["task_id"] and replay["replay"]
    with pytest.raises(server.BridgeError) as error:
        await server.submit_task("stable-key", "changed", make_context())
    assert error.value.code == "operation_conflict"
    with pytest.raises(server.BridgeError) as error:
        await server.get_task(first["task_id"], make_context("other-client"))
    assert error.value.code in {"not_found", "forbidden"}
    assert await server.list_tasks(make_context("other-client")) == []
    other = await server.submit_task("stable-key", "test prompt", make_context("other-client"))
    assert other["task_id"] != first["task_id"]
    view = await server.get_task(first["task_id"], make_context())
    assert not {"prompt", "idem", "idempotency_key", "token"} & view.keys()


@pytest.mark.asyncio
async def test_restart_never_reexecutes_ambiguous_pg_dispatch(modules, pg_store, make_context):
    _, server, _ = modules
    task = await server.submit_task("stable-key", "test", make_context())
    claim = pg_store.claim_next(server.SERVICE, str(uuid.uuid4()), lease_seconds=1)
    assert claim["id"] == task["task_id"]
    pg_store.mark_dispatched(server.SERVICE, str(uuid.uuid4()), claim["id"], claim["epoch"])
    time.sleep(1.1)  # The authority is real PostgreSQL time, not a patched clock.
    await server.recover_auto()
    view = await server.get_task(task["task_id"], make_context())
    assert view["status"] == "interrupted"
    assert await server.claim_next() is None
    replay = await server.submit_task("stable-key", "test", make_context())
    assert replay["replay"] and replay["status"] == "interrupted"
    assert await server.claim_next() is None


def test_identity_revocation_reloads_and_returns_no_hash(modules):
    common, _, _ = modules
    token = "synthetic-client-credential"
    records = {"test-client": {"agent": "test-client", "role": "client", "active": True,
                               "account": "connected-account", "kind": "agent",
                               "hash": hashlib.sha256(token.encode()).hexdigest()}}
    path = common.CONFIG / "identities.json"
    path.write_text(json.dumps(records))
    assert common.identity("Bearer " + token) == {"agent": "test-client", "role": "client",
                                                   "account": "connected-account", "kind": "agent"}
    assert common.identity("Bearer wrong") is None
    records["test-client"]["active"] = False
    path.write_text(json.dumps(records))
    assert common.identity("Bearer " + token) is None
    records["test-client"]["active"] = True
    del records["test-client"]["account"]
    path.write_text(json.dumps(records))
    assert common.identity("Bearer " + token) is None
    path.write_text("{")
    assert common.identity("Bearer " + token) is None


@pytest.mark.asyncio
async def test_audit_contains_metadata_not_prompts_or_untrusted_request_ids(modules, pg_store, make_context):
    common, server, _ = modules
    marker = "synthetic-private-prompt-and-token-marker"
    await server.submit_task("private-key", marker, make_context(request_id=marker))
    log = (common.STATE / "audit.jsonl").read_text()
    assert marker not in log and "private-key" not in log
    assert hashlib.sha256(marker.encode()).hexdigest() in log
    assert all("request_id" not in json.loads(line) for line in log.splitlines())
    with pytest.raises(ValueError, match="Unsupported audit field"):
        common.audit("task.test", prompt=marker)
    common.audit("task.test", task_id=marker, request_id=str(uuid.uuid4()))
    assert marker not in (common.STATE / "audit.jsonl").read_text()


def test_mcp_dynamic_origin_host_and_client_role(modules, pg_store, monkeypatch):
    common, server, _ = modules
    monkeypatch.setenv("PUBLIC_URL", "https://baton.example.com:9443")
    importlib.reload(server)
    monkeypatch.setattr(common, "identity", lambda value: {
        "Bearer client": {"agent": "test-client", "role": "client", "account": "test-account", "kind": "agent"},
        "Bearer dispatcher": {"agent": "codex-worker", "role": "dispatcher", "account": "baton-service", "kind": "service"},
    }.get(value))
    request = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "synthetic-test", "version": "1"},
    }}
    headers = {"Authorization": "Bearer client", "Accept": "application/json, text/event-stream", "Origin": "https://baton.example.com:9443"}
    with TestClient(server.app) as client:
        for host in ("bridge", "bridge:9000", "baton.example.com:9443"):
            response = client.post("/mcp", json=request, headers={**headers, "Host": host})
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
        assert client.post("/mcp", json=request, headers={**headers, "Host": "evil.invalid"}).status_code == 421
        assert client.post("/mcp", json=request, headers={**headers, "Host": "bridge", "Origin": "https://evil.invalid"}).status_code == 403
        assert client.get("/healthz").status_code == 401
        assert client.get("/healthz", headers={"Authorization": "Bearer dispatcher"}).status_code == 403


def test_nested_and_database_credentials_are_redacted(modules, monkeypatch):
    common, _, _ = modules
    secret = "synthetic-database-password"
    path = common.SECRETS / "database.json"
    path.write_text(json.dumps({"password": secret}))
    monkeypatch.setenv("BATON_DATABASE_CONFIG", str(path))
    assert common.redact(secret + " synthetic-dispatcher-credential") == "[REDACTED] [REDACTED]"
    path.write_text("{")
    with pytest.raises(RuntimeError, match="Credential store is unreadable"):
        common.redact(secret)


@pytest.mark.parametrize("url", ["ftp://example.com", "https://user:password@example.com", "https://example.com/path", "https://example.com?token=test"])
def test_public_origin_rejects_credentials_and_paths(modules, monkeypatch, url):
    common, _, _ = modules
    monkeypatch.setenv("PUBLIC_URL", url)
    with pytest.raises(ValueError):
        common.public_url()
