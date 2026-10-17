# SPDX-License-Identifier: Apache-2.0
"""Offline failure contracts: a synthetic CLI, no model, network or credentials."""

import asyncio
import hashlib
import json
import uuid
from types import SimpleNamespace

import pytest
from a2a.compat.v0_3.conversions import to_compat_task_status
from a2a.types import Message, Role, TaskState, TaskStatus, TaskStatusUpdateEvent

from bridge.failures import (
    CATALOG, TASK_FAILURE_CODES, WORKER_CODES, Failure, FailureError, classify_codex_exit, failure_from_a2a,
    worker_exception_failure,
)

A2A_ID = "12345678-1234-4234-8234-123456789abc"
CONTEXT_ID = "87654321-4321-4321-8321-cba987654321"
SECRET = "SYNTHETIC-PRIVATE-PROMPT-AND-CREDENTIAL"


@pytest.mark.parametrize(("message", "code"), [
    ("HTTP 401 Unauthorized", "CODEX_AUTH_UNAUTHORIZED"),
    ("Your refresh token has already been used. Please log in again.", "CODEX_AUTH_REFRESH_FAILED"),
    ("refresh_token_expired", "CODEX_AUTH_REFRESH_FAILED"),
    ("The model is not supported when using this account", "CODEX_MODEL_FORBIDDEN"),
    ("model_not_found", "CODEX_MODEL_FORBIDDEN"),
    ("HTTP 403 Forbidden", "CODEX_MODEL_FORBIDDEN"),
    ("HTTP 429 Too Many Requests", "CODEX_CAPACITY_LIMITED"),
    ("insufficient_quota", "CODEX_CAPACITY_LIMITED"),
    ("provider is overloaded", "CODEX_CAPACITY_LIMITED"),
    ("ERROR: Selected model is at capacity. Please try a different model.", "MODEL_CAPACITY"),
    ("bwrap: Creating new namespace failed: Operation not permitted", "CODEX_SANDBOX_FAILED"),
    ("error parsing configuration: invalid TOML", "CODEX_CONFIG_INVALID"),
    ("unknown permissions profile baton", "CODEX_CONFIG_INVALID"),
    ("Failed to create session: failed to load AGENTS.md: Resource temporarily unavailable (os error 11)\n"
     "reqwest-internal-sync-runtime: OS can't spawn worker thread: EAGAIN", "WORKER_RESOURCE_EXHAUSTED"),
    ("failed to open session file: Too many open files (os error 24)", "WORKER_FD_EXHAUSTED"),
    ("failed to initialize session: Cannot allocate memory (os error 12)", "WORKER_MEMORY_EXHAUSTED"),
    ("network read: Resource temporarily unavailable (os error 11)", "CODEX_EXECUTION_FAILED"),
    ("opaque failure with " + SECRET, "CODEX_EXECUTION_FAILED"),
])
def test_exit_classification_is_an_allowlist(message, code):
    stdout = json.dumps({"type": "error", "message": message + "\n" + SECRET}).encode()
    failure = classify_codex_exit(b"", stdout=stdout, returncode=1, has_output=False)
    assert failure.code == code
    assert SECRET not in failure.display()
    assert SECRET not in json.dumps(failure.payload())


def test_user_tool_text_and_bubblewrap_fallback_do_not_classify_errors():
    stdout = b"\n".join(json.dumps(event).encode() for event in [
        {"type": "item.completed", "item": {"text": "401 Unauthorized refresh_token_expired Selected model is at capacity"}},
        {"type": "tool.output", "message": "bwrap sandbox failed; " + SECRET},
        {"type": "user", "message": "ERROR: 401 Unauthorized"},
    ])
    stderr = (b"user\nERROR: 401 Unauthorized\nERROR: Selected model is at capacity\n"
              b"WARNING: bundled bubblewrap unavailable, falling back to system bwrap\n"
              b"ERROR: unrecognized execution failure\n")
    assert classify_codex_exit(stderr, stdout=stdout, returncode=1, has_output=False).code == "CODEX_EXECUTION_FAILED"


def test_typed_turn_failure_is_classified_but_not_echoed():
    stdout = json.dumps({"type": "turn.failed", "error": {"message": "Selected model is at capacity. " + SECRET}}).encode()
    failure = classify_codex_exit(b"", stdout=stdout, returncode=1, has_output=False)
    assert failure.code == "MODEL_CAPACITY"
    assert SECRET not in failure.display()


def test_native_session_panic_remains_diagnosable():
    stderr = (b"thread 'reqwest-internal-sync-runtime' panicked at library/thread/mod.rs:1:\n"
              b"OS can't spawn worker thread: Resource temporarily unavailable (os error 11)\n")
    assert classify_codex_exit(stderr, returncode=1, has_output=False).code == "WORKER_RESOURCE_EXHAUSTED"


def test_exit_without_output_and_exceptions_map_to_closed_codes():
    assert classify_codex_exit(b"", returncode=0, has_output=False).code == "CODEX_OUTPUT_MISSING"
    assert worker_exception_failure(TimeoutError(SECRET)).code == "CODEX_TIMEOUT"
    assert worker_exception_failure(OSError(11, SECRET)).code == "WORKER_RESOURCE_EXHAUSTED"
    assert worker_exception_failure(OSError(2, SECRET)).code == "CODEX_CONFIG_INVALID"
    assert worker_exception_failure(RuntimeError(SECRET)).code == "CODEX_EXECUTION_FAILED"
    assert worker_exception_failure(FailureError(Failure("MODEL_CAPACITY"))).code == "MODEL_CAPACITY"
    with pytest.raises(ValueError):
        Failure(SECRET)


def test_vocabulary_is_closed_and_has_no_mail_codes():
    assert WORKER_CODES < TASK_FAILURE_CODES <= set(CATALOG)
    assert not any("MAIL" in code for code in CATALOG)
    assert not any(code.startswith("WORKER_CANARY") or code == "WORKER_HEALTHY" for code in WORKER_CODES)
    assert Failure("CODEX_TIMEOUT").payload() == {"version": 1, "code": "CODEX_TIMEOUT", "stage": "execution"}


class Queue:
    def __init__(self):
        self.events = []

    async def enqueue_event(self, event):
        self.events.append(event)


def context(prompt=SECRET, task_id=A2A_ID, owner="alice", stable="12345678-0000-4000-8000-000000000001"):
    data = {"task_id": stable, "owner": owner, "prompt": prompt}
    return SimpleNamespace(task_id=task_id, context_id=CONTEXT_ID,
                           message=Message(message_id="request", role=Role.ROLE_USER),
                           get_user_input=lambda: json.dumps(data))


def terminal_status(queue):
    statuses = [item.status for item in queue.events if isinstance(item, TaskStatusUpdateEvent)]
    assert statuses[-1].state == TaskState.TASK_STATE_FAILED
    return to_compat_task_status(statuses[-1]).model_dump(mode="json", by_alias=True, exclude_none=True)


def wire_failure(status):
    parts = status.get("message", {}).get("parts", [])
    values = [part["data"]["baton_failure"] for part in parts
              if part.get("kind") == "data" and "baton_failure" in part.get("data", {})]
    assert len(values) == 1
    return values[0]


def test_failure_envelope_survives_real_sdk_v03_conversion(modules):
    _, _, worker = modules
    original = Failure("WORKER_RESOURCE_EXHAUSTED")
    status = TaskStatus(state=TaskState.TASK_STATE_FAILED,
                        message=worker.failure_message(original, A2A_ID, CONTEXT_ID))
    wire = to_compat_task_status(status).model_dump(mode="json", by_alias=True, exclude_none=True)
    assert wire_failure(wire) == original.payload()
    assert failure_from_a2a(wire) == original


@pytest.mark.parametrize("payload", [
    {"version": 1, "code": SECRET, "stage": "authentication"},
    {"version": 1, "code": "CODEX_AUTH_UNAUTHORIZED", "stage": SECRET},
    {"version": 1, "code": "CODEX_AUTH_UNAUTHORIZED", "stage": "execution"},
    {"version": 1, "code": "CODEX_AUTH_UNAUTHORIZED", "stage": "authentication", "message": SECRET},
    {"version": 2, "code": "CODEX_AUTH_UNAUTHORIZED", "stage": "authentication"},
    {"version": 1, "code": ["CODEX_AUTH_UNAUTHORIZED"], "stage": "authentication"},
    {"version": 1, "code": "WORKER_HEALTHY", "stage": "health_check"},
    {"version": 1, "code": "A2A_FAILED_UNCLASSIFIED", "stage": "execution"},
])
def test_untrusted_a2a_diagnostic_is_replaced_with_fixed_fallback(payload):
    status = {"state": "failed", "message": {"parts": [
        {"kind": "data", "data": {"baton_failure": payload}}, {"kind": "text", "text": SECRET}]}}
    assert failure_from_a2a(status).code == "A2A_FAILED_UNCLASSIFIED"


@pytest.mark.parametrize("status", [
    None, "failed", {"state": "failed"}, {"message": {"parts": "text"}},
    {"message": {"parts": [{"kind": "text", "text": "CODEX_AUTH_UNAUTHORIZED"}]}},
    {"message": {"parts": [{"kind": "data", "data": {"lab_failure": {
        "version": 1, "code": "CODEX_AUTH_UNAUTHORIZED", "stage": "authentication"}}}]}},
    {"message": {"parts": [{"kind": "data", "data": {"baton_failure": {
        "version": 1, "code": "CODEX_AUTH_UNAUTHORIZED", "stage": "authentication"}, "extra": 1}}]}},
    {"message": {"parts": [{"kind": "data", "data": {"baton_failure": {
        "version": 1, "code": "CODEX_AUTH_UNAUTHORIZED", "stage": "authentication"}}}] * 2}},
])
def test_missing_or_ambiguous_failure_parts_use_the_fixed_fallback(status):
    assert failure_from_a2a(status).code == "A2A_FAILED_UNCLASSIFIED"


def calls_made(modules):
    path = modules[0].CODEX_HOME / "calls.log"
    return len(path.read_text()) if path.exists() else 0


@pytest.mark.asyncio
@pytest.mark.parametrize(("message", "code"), [
    ("401 Unauthorized " + SECRET, "CODEX_AUTH_UNAUTHORIZED"),
    ("opaque " + SECRET, "CODEX_EXECUTION_FAILED"),
])
async def test_worker_persists_safe_failure_and_replay_never_reruns(modules, fake_codex, capsys, message, code):
    common, _, worker = modules
    fake_codex(steps=[{"event": {"type": "error", "message": message}}, {"stderr": "raw stderr " + SECRET}],
               result=None, exit=1)
    executor = worker.CodexExecutor("codex")
    first, replay = Queue(), Queue()
    await executor.execute(context(), first)
    await executor.execute(context(task_id="22345678-1234-4234-8234-123456789abc"), replay)
    assert calls_made(modules) == 1
    for queue in (first, replay):
        status = terminal_status(queue)
        assert wire_failure(status) == {"version": 1, "code": code, "stage": Failure(code).stage}
        assert SECRET not in json.dumps(status)
    with common.database("executions.sqlite3") as db:
        row = dict(db.execute("SELECT * FROM executions").fetchone())
    assert row["status"] == "failed" and row["error_code"] == code and row["result"] is None
    assert SECRET not in (common.STATE / "audit.jsonl").read_text()
    assert SECRET not in capsys.readouterr().out
    assert SECRET not in json.dumps(row)


@pytest.mark.asyncio
async def test_empty_success_is_a_safe_failure(modules, fake_codex):
    _, _, worker = modules
    fake_codex(result=None, exit=0)
    queue = Queue()
    await worker.CodexExecutor("codex").execute(context(), queue)
    assert wire_failure(terminal_status(queue))["code"] == "CODEX_OUTPUT_MISSING"


@pytest.mark.asyncio
async def test_timeout_is_a_safe_failure_and_stops_the_process_group(modules, fake_codex):
    common, _, worker = modules
    fake_codex(steps=[{"hang": True}], result=None)
    executor = worker.CodexExecutor("codex")
    async with executor.lock:
        with pytest.raises(TimeoutError):
            await executor.run_codex("synthetic task", str(uuid.uuid4()), timeout=0.5)
    assert worker_exception_failure(TimeoutError()).code == "CODEX_TIMEOUT"
    assert not executor.processes
    queue = Queue()

    async def timed_out(*args, **kwargs):
        raise asyncio.TimeoutError()

    executor.run_codex = timed_out
    await executor.execute(context(), queue)
    assert wire_failure(terminal_status(queue))["code"] == "CODEX_TIMEOUT"


@pytest.mark.asyncio
async def test_unexpected_cli_version_is_a_configuration_failure(modules, fake_codex, monkeypatch):
    _, _, worker = modules
    monkeypatch.setattr(worker, "CODEX_VERSION", "0.0.0")
    queue = Queue()
    await worker.CodexExecutor("codex").execute(context(), queue)
    assert wire_failure(terminal_status(queue))["code"] == "CODEX_CONFIG_INVALID"
    assert calls_made(modules) == 0


@pytest.mark.asyncio
async def test_conflicting_replay_does_not_replace_original_failure(modules, fake_codex):
    common, _, worker = modules
    fake_codex(steps=[{"event": {"type": "error", "message": "401 Unauthorized"}}], result=None, exit=1)
    executor = worker.CodexExecutor("codex")
    await executor.execute(context(), Queue())
    conflict = Queue()
    await executor.execute(context(prompt="changed input"), conflict)
    assert calls_made(modules) == 1
    assert wire_failure(terminal_status(conflict))["code"] == "CODEX_IDEMPOTENCY_CONFLICT"
    with common.database("executions.sqlite3") as db:
        assert db.execute("SELECT error_code FROM executions").fetchone()[0] == "CODEX_AUTH_UNAUTHORIZED"


@pytest.mark.asyncio
async def test_interrupted_or_unclassified_prior_execution_is_never_rerun(modules, fake_codex):
    common, _, worker = modules
    stable = str(uuid.uuid4())
    digest = hashlib.sha256(json.dumps(["alice", "synthetic"], ensure_ascii=False).encode()).hexdigest()
    with common.database("executions.sqlite3") as db:
        db.execute("INSERT INTO executions(id,digest,status,a2a_id,backend,error_code) VALUES (?,?,?,?,?,?)",
                   (stable, digest, "failed", A2A_ID, "codex", "NOT_A_WORKER_CODE"))
    queue = Queue()
    await worker.CodexExecutor("codex").execute(context(prompt="synthetic", stable=stable), queue)
    assert wire_failure(terminal_status(queue))["code"] == "CODEX_PRIOR_EXECUTION_UNRESOLVED"
    assert calls_made(modules) == 0


class Response:
    def __init__(self, reply):
        self.reply = reply

    def raise_for_status(self):
        pass

    def json(self):
        return self.reply


def failed_reply(a2a_id, parts):
    return {"result": {"id": a2a_id, "status": {"state": "failed", "message": {"parts": parts}}}}


def fake_gateway(monkeypatch, server, reply, delay=0):
    calls = []

    class Http:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, *args, **kwargs):
            calls.append(kwargs["json"])
            await asyncio.sleep(delay)
            return Response(reply)

    monkeypatch.setattr(server.httpx, "AsyncClient", Http)
    return calls


async def submit(modules, make_context, register_identity, key):
    context = make_context()
    register_identity(context.request_context.request.state.identity)
    task = await modules[1].submit_task(key, "Synthetic dispatch prompt", context)
    return context, task


def failure_messages(inbox, task_id):
    return [message for message in inbox["messages"] if message["subject"] == "[failed] " + task_id]


@pytest.mark.asyncio
async def test_bridge_stores_code_and_reports_failure_in_the_task_thread(
        modules, pg_store, make_context, register_identity, monkeypatch, capsys):
    common, server, _ = modules
    reply = failed_reply(A2A_ID, [
        {"kind": "data", "data": {"baton_failure": {
            "version": 1, "code": "CODEX_AUTH_UNAUTHORIZED", "stage": "authentication"}}},
        {"kind": "text", "text": SECRET},
    ])
    calls = fake_gateway(monkeypatch, server, reply)
    owner, task = await submit(modules, make_context, register_identity, "failure-code")
    assert await server.process_next()
    view = await server.get_task(task["task_id"], owner)
    assert view["status"] == "failed" and view["a2a_id"] == A2A_ID
    assert view["error"] == "CODEX_AUTH_UNAUTHORIZED" and SECRET not in json.dumps(view)
    inbox = await server.fetch_inbox(owner)
    reports = failure_messages(inbox, task["task_id"])
    assert len(reports) == 1 and reports[0]["thread_id"] == task["task_id"]
    assert "CODEX_AUTH_UNAUTHORIZED" in reports[0]["body_md"] and "authentication" in reports[0]["body_md"]
    assert SECRET not in json.dumps(inbox)
    log = (common.STATE / "audit.jsonl").read_text()
    events = [json.loads(line) for line in log.splitlines()]
    failed = next(event for event in events if event["event"] == "task.failed")
    assert failed["error_code"] == "CODEX_AUTH_UNAUTHORIZED" and failed["stage"] == "authentication"
    assert any(event["event"] == "thread.failed" for event in events)
    assert SECRET not in log + capsys.readouterr().out
    # A failed task is final: neither the queue nor a replayed submission dispatches it again.
    assert not await server.process_next() and len(calls) == 1
    replay = await server.submit_task("failure-code", "Synthetic dispatch prompt", owner)
    assert replay["replay"] and replay["status"] == "failed" and replay["error"] == "CODEX_AUTH_UNAUTHORIZED"
    assert not await server.process_next() and len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"version": 1, "code": SECRET, "stage": "authentication"},
    {"version": 1, "code": "CODEX_AUTH_UNAUTHORIZED", "stage": SECRET},
    {"version": 1, "code": "CODEX_AUTH_UNAUTHORIZED", "stage": "authentication", "message": SECRET},
    {"version": 1, "code": "WORKER_HEALTHY", "stage": "health_check"},
])
async def test_bridge_replaces_untrusted_worker_diagnostics_with_fixed_fallback(
        modules, pg_store, make_context, register_identity, monkeypatch, payload):
    common, server, _ = modules
    reply = failed_reply(A2A_ID, [{"kind": "data", "data": {"baton_failure": payload}}, {"kind": "text", "text": SECRET}])
    fake_gateway(monkeypatch, server, reply)
    owner, task = await submit(modules, make_context, register_identity, "untrusted")
    assert await server.process_next()
    view = await server.get_task(task["task_id"], owner)
    assert view["status"] == "failed" and view["error"] == "A2A_FAILED_UNCLASSIFIED" and view["a2a_id"] == A2A_ID
    assert SECRET not in json.dumps(await server.fetch_inbox(owner))
    assert SECRET not in (common.STATE / "audit.jsonl").read_text()


@pytest.mark.asyncio
async def test_expired_dispatch_cannot_commit_a_failure_or_report_it(
        modules, pg_store, make_context, register_identity, monkeypatch):
    _, server, _ = modules
    owner, task = await submit(modules, make_context, register_identity, "late-failure")
    claim = pg_store.claim_next(server.SERVICE, str(uuid.uuid4()), lease_seconds=1)
    reply = failed_reply(A2A_ID, [{"kind": "data", "data": {"baton_failure": {
        "version": 1, "code": "CODEX_TIMEOUT", "stage": "execution"}}}])
    fake_gateway(monkeypatch, server, reply, delay=1.1)
    with pytest.raises(server.DispatchUncertain):
        await server.dispatch(claim)
    await server.recover_auto()
    view = await server.get_task(task["task_id"], owner)
    assert view["status"] == "interrupted" and view["error"] == "dispatch_uncertain"
    assert not failure_messages(await server.fetch_inbox(owner), task["task_id"])


def test_store_accepts_only_closed_failure_codes(modules, pg_store):
    _, server, _ = modules
    for code in (SECRET, "WORKER_HEALTHY", "WORKER_CANARY_BUSY", "cancelled", None):
        with pytest.raises(server.StoreError) as caught:
            pg_store.fail_auto(server.SERVICE, str(uuid.uuid4()), str(uuid.uuid4()), 1, code, A2A_ID)
        assert caught.value.code == "invalid"
