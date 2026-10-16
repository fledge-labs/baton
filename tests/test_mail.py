# SPDX-License-Identifier: Apache-2.0

import json
import uuid

import httpx
import pytest


@pytest.fixture
def fake_mail(modules, monkeypatch):
    _, server, _ = modules
    calls = []

    async def call(name, args):
        calls.append((name, args))
        if name == "register_agent":
            return {"name": "BlueRiver" if args["program"] == "codex-worker" else "GreenPine",
                    "registration_token": "synthetic-registration-" + args["program"]}
        if name == "send_message":
            return {"count": 1, "verified_sender": True, "deliveries": [{"payload": {"id": len(calls)}}]}
        return {}

    monkeypatch.setattr(server, "mail_call", call)
    return calls


@pytest.mark.asyncio
async def test_mail_registration_tokens_thread_and_private_response(modules, make_context, fake_mail):
    common, server, _ = modules
    task = await server.submit_task("mail-chain", "synthetic prompt", make_context())
    row = server.lookup(task["task_id"], "test-client")
    await server.mail_event(row, "submitted", "synthetic prompt")
    await server.mail_event(row, "completed", "synthetic result")
    sends = [args for name, args in fake_mail if name == "send_message"]
    assert len(sends) == 2
    assert all(args["thread_id"] == task["task_id"] and args["sender_token"] for args in sends)
    assert sends[0]["sender_name"] == sends[1]["to"][0]
    assert sends[1]["sender_name"] == sends[0]["to"][0]
    handshakes = [args for name, args in fake_mail if name == "macro_contact_handshake"]
    assert all(args["requester_registration_token"] and args["target_registration_token"] for args in handshakes)
    assert len([name for name, _ in fake_mail if name == "register_agent"]) == 2
    view = await server.get_task(task["task_id"], make_context())
    assert view["mail_status"] == "sent" and view["submission_mail_status"] == "sent"
    assert "synthetic-registration" not in json.dumps(view)
    assert "synthetic-registration" not in (common.STATE / "audit.jsonl").read_text()


@pytest.mark.asyncio
async def test_mail_response_whitelists_fields_and_redacts_tokens(modules, make_context, fake_mail, monkeypatch):
    _, server, _ = modules
    await server.mail_agent("test-client")

    async def inbox(name, args):
        assert name == "fetch_inbox" and args["registration_token"]
        return {"result": [{"id": 1, "thread_id": str(uuid.uuid4()), "body_md": "synthetic-registration-test-client",
                            "registration_token": "must-not-cross-boundary", "subject": "hello"}]}

    monkeypatch.setattr(server, "mail_call", inbox)
    result = await server.fetch_inbox(make_context())
    assert result["messages"][0]["body_md"] == "[REDACTED]"
    assert "registration_token" not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("mail_failure", [False, True])
async def test_completed_task_preserves_result_when_mail_fails(modules, make_context, fake_mail, monkeypatch, mail_failure):
    common, server, _ = modules
    task = await server.submit_task("complete-chain", "synthetic task", make_context())
    row = server.claim_next()
    original_mail = server.mail_call

    async def call(name, args):
        if mail_failure and name == "send_message" and args["subject"].startswith("[completed]"):
            raise RuntimeError("synthetic-do-not-log-error-content")
        return await original_mail(name, args)

    monkeypatch.setattr(server, "mail_call", call)
    a2a_id = str(uuid.uuid4())

    def respond(request):
        payload = json.loads(request.content)
        assert payload["id"] == task["task_id"]
        assert request.headers["Authorization"] == "Bearer synthetic-dispatcher-credential"
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": task["task_id"], "result": {
            "id": a2a_id, "status": {"state": "completed"}, "artifacts": [{
                "metadata": {"backend": "demo"}, "parts": [{"kind": "text", "text": "BATON_DEMO_OK"}],
            }],
        }})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(server.httpx, "AsyncClient", lambda **kwargs: original_client(transport=httpx.MockTransport(respond), **kwargs))
    await server.dispatch(row)
    result = await server.get_task(task["task_id"], make_context())
    assert result["status"] == "completed" and result["result"] == "BATON_DEMO_OK"
    assert result["backend"] == "demo" and result["a2a_id"] == a2a_id
    assert result["mail_status"] == ("failed" if mail_failure else "sent")
    assert bool(result["mail_error"]) == mail_failure
    assert server.claim_next() is None
    assert "synthetic-do-not-log-error-content" not in (common.STATE / "audit.jsonl").read_text()
