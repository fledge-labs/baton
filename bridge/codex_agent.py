# SPDX-License-Identifier: Apache-2.0
"""A2A worker backed by sandboxed Codex, or an explicit protocol-only demo."""

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import signal
import sqlite3
import tempfile
import time
from pathlib import Path

import uvicorn
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import add_a2a_routes_to_fastapi, create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import TaskUpdater
from a2a.server.tasks.database_task_store import DatabaseTaskStore
from a2a.types import (
    AgentCapabilities, AgentCard, AgentInterface, AgentSkill, Message, Part, Role, Task, TaskState, TaskStatus,
)
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from google.protobuf.json_format import ParseDict
from google.protobuf.struct_pb2 import Value
from sqlalchemy.ext.asyncio import create_async_engine

from bridge.common import (
    CODEX_HOME, CONFIG, STATE, AuthMiddleware, audit, database, known_secrets, public_url, redact, safe_id, uuid_id,
)
from bridge.failures import (
    FAILURE_KEY, WORKER_CODES, Failure, FailureError, classify_codex_exit, worker_exception_failure,
)
from bridge.phases import PhaseRecorder, cgroup_counters, encode_phases
from bridge.worker_health import (
    CANARY_RESULT, CGROUP_ROOT, WorkerHealth, init_health_db, sanitize_health_snapshot,
)

CODEX_VERSION = "0.154.0"
WORK_ROOT = Path("/work")
EVENT_LINE_LIMIT = 1_048_576
DIAGNOSTIC_EVENT_LIMIT = 262_144
STDERR_TAIL_LIMIT = 65_536


def worker_mode():
    mode = os.environ.get("WORKER_MODE", "codex")
    if mode not in {"codex", "demo"}:
        raise ValueError("WORKER_MODE must be codex or demo")
    return mode


def init_db():
    with database("executions.sqlite3") as db:
        db.execute("""CREATE TABLE IF NOT EXISTS executions (
            id TEXT PRIMARY KEY, digest TEXT NOT NULL, status TEXT NOT NULL,
            result TEXT, a2a_id TEXT, backend TEXT NOT NULL)""")
        columns = [row[1] for row in db.execute("PRAGMA table_info(executions)")]
        for column in ("error_code", "phases"):
            if column not in columns:
                db.execute("ALTER TABLE executions ADD COLUMN " + column + " TEXT")
        rows = db.execute("SELECT id,a2a_id,backend FROM executions WHERE status='running'").fetchall()
        db.execute("UPDATE executions SET status='interrupted' WHERE status='running'")
    init_health_db(recover=True)
    for row in rows:
        audit("worker.interrupted", task_id=row["id"], a2a_task_id=row["a2a_id"], backend=row["backend"])


def codex_command(wd, final):
    """Only the unique task directory is writable by model-generated commands."""
    denied = {"/data", "/config", "/run/secrets", "/home/agent/.codex", str(STATE), str(CONFIG), str(CODEX_HOME)}
    paths = ",".join(json.dumps(path) + '="deny"' for path in sorted(denied))
    permissions = ('permissions.baton={extends=":read-only",filesystem={' + paths
                   + "," + json.dumps(str(wd)) + '="write"},network={enabled=false}}')
    command = [
        "codex", "exec", "--json", "--ignore-user-config", "--ignore-rules", "--skip-git-repo-check", "--ephemeral",
        "-c", 'default_permissions="baton"', "-c", 'approval_policy="never"', "-c", permissions,
        "-c", 'web_search="disabled"', "--color", "never", "--output-last-message", str(final),
    ]
    model = os.environ.get("CODEX_MODEL", "").strip()
    if model:
        command.extend(["--model", model])
    command.append("-")
    return command


def codex_environment(wd):
    env = {key: value for key, value in os.environ.items()
           if key in {"PATH", "SSL_CERT_FILE", "SSL_CERT_DIR", "LANG"}}
    env.update({"HOME": "/home/agent", "CODEX_HOME": str(CODEX_HOME), "TMPDIR": str(wd / ".tmp")})
    return env


def failure_message(failure, task_id, context_id):
    """The only worker diagnostic that leaves the process: a versioned code and stage."""
    return Message(message_id=task_id + ":failure", task_id=task_id, context_id=context_id,
                   role=Role.ROLE_AGENT, parts=[
                       Part(data=ParseDict({FAILURE_KEY: failure.payload()}, Value())),
                       Part(text=failure.display()),
                   ])


class Diagnostics:
    """Bounded typed CLI errors and stderr tail, kept only to classify a failed exit."""

    def __init__(self):
        self.errors = bytearray()
        self.stderr = b""

    def add_error(self, event):
        detail = event if event["type"] == "error" else event.get("error")
        if isinstance(detail, dict) and isinstance(detail.get("message"), str):
            message = detail["message"][:16384]
            compact = {"type": "error", "message": message} if event["type"] == "error" else {
                "type": "turn.failed", "error": {"message": message}}
            self.errors += json.dumps(compact).encode() + b"\n"
            del self.errors[:-DIAGNOSTIC_EVENT_LIMIT]


async def follow_events(stream, recorder, diagnostics):
    """Time each --json event; only its type is kept, and error text only for classification."""
    buffer, skipping = bytearray(), False

    def handle(line):
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            return
        if not isinstance(event, dict):
            return
        recorder.event(event.get("type"))
        if event.get("type") in ("error", "turn.failed"):
            diagnostics.add_error(event)

    while chunk := await stream.read(65_536):
        buffer += chunk
        while (newline := buffer.find(b"\n")) >= 0:
            line = bytes(buffer[:newline])
            del buffer[:newline + 1]
            if skipping:
                skipping = False
            else:
                handle(line)
        if len(buffer) > EVENT_LINE_LIMIT:
            # An oversized line is counted as an untyped event and never parsed.
            recorder.event(None)
            buffer.clear()
            skipping = True
    if buffer and not skipping:
        handle(bytes(buffer))


async def follow_stderr(stream, diagnostics):
    tail = bytearray()
    while chunk := await stream.read(65_536):
        tail += chunk
        del tail[:-STDERR_TAIL_LIMIT]
    diagnostics.stderr = bytes(tail)


async def drive(process, data, recorder, diagnostics):
    async def feed():
        try:
            process.stdin.write(data)
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            process.stdin.close()

    async def wait():
        await process.wait()
        recorder.exited()

    async with asyncio.TaskGroup() as group:
        group.create_task(feed())
        group.create_task(follow_events(process.stdout, recorder, diagnostics))
        group.create_task(follow_stderr(process.stderr, diagnostics))
        group.create_task(wait())


class CodexExecutor(AgentExecutor):
    def __init__(self, backend=None, *, cgroup_root=CGROUP_ROOT, clock=time.monotonic_ns):
        self.backend = backend or worker_mode()
        if self.backend not in {"demo", "codex"}:
            raise ValueError("Unknown worker backend")
        self.processes = {}
        self.lock = asyncio.Lock()
        self.cgroup_root, self.clock = Path(cgroup_root), clock
        self.cold = True

    async def run_backend(self, prompt, process_key, **options):
        """Business tasks and health canaries both enter here, under the same lock."""
        if not self.lock.locked():
            raise RuntimeError("Executor lock is required")
        if self.backend == "demo":
            return CANARY_RESULT if options.get("health_probe") else "BATON_DEMO_OK"
        return await self.run_codex(prompt, process_key, **options)

    async def run_codex(self, prompt, process_key, *, timeout=300, audit_task_id=None, health_probe=False):
        if not self.lock.locked():
            raise RuntimeError("Executor lock is required")
        # Cold means the first execution since this worker process started.
        recorder = PhaseRecorder(self.cold, self.clock)
        self.cold = False
        before = None
        try:
            before_secrets = known_secrets()
            with (
                tempfile.TemporaryDirectory(prefix="task-", dir=WORK_ROOT) as directory,
                tempfile.TemporaryDirectory(prefix="result-", dir=STATE) as result_directory,
            ):
                wd = Path(directory)
                (wd / ".tmp").mkdir(mode=0o700)
                # The harness writes this outside the tool sandbox. Keep it under
                # the denied state directory so a task cannot redirect that write
                # (or the subsequent read) with a symlink in its writable workdir.
                final = Path(result_directory) / "result.txt"
                env = codex_environment(wd)
                version = await asyncio.create_subprocess_exec(
                    "codex", "--version", env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                )
                try:
                    output, _ = await asyncio.wait_for(version.communicate(), timeout=10)
                except BaseException:
                    with contextlib.suppress(ProcessLookupError):
                        version.kill()
                    await version.wait()
                    raise
                if version.returncode or output.strip() != ("codex-cli " + CODEX_VERSION).encode():
                    raise FailureError(Failure("CODEX_CONFIG_INVALID"))
                instructions = (
                    "You are a coding worker in Baton. Work only on the supplied task in its isolated directory. "
                    "Do not inspect credentials, other tasks, services, or production systems. Never reveal secrets.\n\n"
                    + prompt
                )
                diagnostics = Diagnostics()
                before = cgroup_counters(self.cgroup_root)
                recorder.begin()
                process = await asyncio.create_subprocess_exec(
                    *codex_command(wd, final), cwd=wd, env=env, stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True,
                )
                recorder.spawned()
                self.processes[process_key] = process
                try:
                    await asyncio.wait_for(drive(process, instructions.encode(), recorder, diagnostics), timeout=timeout)
                except BaseException:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    await process.wait()
                    recorder.exited()
                    raise
                has_output = final.is_file() and final.stat().st_size > 0
                if process.returncode or not has_output:
                    failure = classify_codex_exit(diagnostics.stderr, stdout=bytes(diagnostics.errors),
                                                  returncode=process.returncode, has_output=has_output)
                    identity = {"probe_id": process_key} if health_probe else {"task_id": audit_task_id, "a2a_task_id": process_key}
                    audit("worker.exit_error", **identity, exit_code=process.returncode,
                          error_code=failure.code, stage=failure.stage)
                    raise FailureError(failure)
                # Bound artifacts before decoding; stderr and tool transcripts are never logged.
                with final.open("rb") as result:
                    output = result.read(128000).decode("utf-8", errors="replace")
                output = redact(output, before_secrets)[:32000]
                if not output.strip():
                    raise FailureError(Failure("CODEX_OUTPUT_MISSING"))
                return output
        finally:
            self.processes.pop(process_key, None)
            self.record_phases(recorder, before, process_key, audit_task_id, health_probe)

    def record_phases(self, recorder, before, process_key, audit_task_id, health_probe):
        """Store numbers and event type names on the ledger row and as one audit line."""
        after = cgroup_counters(self.cgroup_root) if before is not None else None
        phases = recorder.record(before, after)
        # Diagnostics must never change the outcome of the execution they describe.
        with contextlib.suppress(sqlite3.Error, OSError, ValueError):
            with database("executions.sqlite3") as db:
                if health_probe:
                    db.execute("UPDATE health_probes SET phases=? WHERE id=?", (encode_phases(phases), process_key))
                elif audit_task_id:
                    db.execute("UPDATE executions SET phases=? WHERE id=?", (encode_phases(phases), audit_task_id))
            identity = {"probe_id": process_key} if health_probe else {"task_id": audit_task_id, "a2a_task_id": process_key}
            audit("worker.phases", **identity, backend="codex", phases=phases)

    async def reject(self, updater, context, event, failure, **fields):
        audit(event, a2a_task_id=context.task_id, backend=self.backend, error_code=failure.code,
              stage=failure.stage, **fields)
        await updater.failed(failure_message(failure, context.task_id, context.context_id))

    async def execute(self, context: RequestContext, event_queue: EventQueue):
        await event_queue.enqueue_event(Task(
            id=context.task_id, context_id=context.context_id,
            status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED), history=[context.message],
        ))
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        try:
            data = json.loads(context.get_user_input())
            task_id = uuid_id(data["task_id"])
            owner = safe_id(data["owner"])
            prompt = data["prompt"]
            if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 16000:
                raise ValueError("Invalid prompt")
        except (ValueError, TypeError, KeyError):
            await updater.failed()
            return
        digest = hashlib.sha256(json.dumps([owner, prompt], ensure_ascii=False).encode()).hexdigest()
        async with self.lock:
            with database("executions.sqlite3") as db:
                db.execute("BEGIN IMMEDIATE")
                existing = db.execute("SELECT * FROM executions WHERE id=?", (task_id,)).fetchone()
                if not existing:
                    db.execute("INSERT INTO executions(id,digest,status,result,a2a_id,backend) VALUES (?,?,?,?,?,?)",
                               (task_id, digest, "running", None, context.task_id, self.backend))
            if existing:
                if existing["digest"] != digest or existing["status"] != "completed":
                    code = ("CODEX_IDEMPOTENCY_CONFLICT" if existing["digest"] != digest else
                            existing["error_code"] if existing["error_code"] in WORKER_CODES else
                            "CODEX_PRIOR_EXECUTION_UNRESOLVED")
                    await self.reject(updater, context, "worker.replay", Failure(code), task_id=task_id)
                    return
                output, backend = existing["result"], existing["backend"]
                audit("worker.replay", task_id=task_id, a2a_task_id=context.task_id, backend=backend)
                await updater.add_artifact([Part(text=output)], name="baton-result",
                                           metadata={"backend": backend}, last_chunk=True)
                await updater.complete()
                return
            audit("worker.claimed", task_id=task_id, agent="codex-worker", a2a_task_id=context.task_id, backend=self.backend)
            await updater.start_work()
            try:
                output = await self.run_backend(prompt, context.task_id, audit_task_id=task_id)
                with database("executions.sqlite3") as db:
                    db.execute("UPDATE executions SET status='completed',result=? WHERE id=?", (output, task_id))
                await updater.add_artifact([Part(text=output)], name="baton-result",
                                           metadata={"backend": self.backend}, last_chunk=True)
                await updater.complete()
                audit("worker.completed", task_id=task_id, agent="codex-worker", a2a_task_id=context.task_id,
                      backend=self.backend, output_sha256=hashlib.sha256(output.encode()).hexdigest())
            except asyncio.CancelledError:
                # Persisted running becomes interrupted on restart; no replay runs it again.
                raise
            except Exception as exc:
                failure = worker_exception_failure(exc)
                with database("executions.sqlite3") as db:
                    db.execute("UPDATE executions SET status='failed',error_code=? WHERE id=? AND status!='completed'",
                               (failure.code, task_id))
                await self.reject(updater, context, "worker.failed", failure, task_id=task_id,
                                  error_type=type(exc).__name__)
            finally:
                self.processes.pop(context.task_id, None)

    async def cancel(self, context: RequestContext, event_queue: EventQueue):
        process = self.processes.get(context.task_id)
        if process:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
        await TaskUpdater(event_queue, context.task_id, context.context_id).cancel()


def agent_card(backend):
    description = ("Protocol demonstration only; no model is invoked." if backend == "demo"
                   else "Isolated Codex CLI " + CODEX_VERSION + " over the official A2A SDK.")
    return AgentCard(
        name="Baton " + ("Demo" if backend == "demo" else "Codex") + " Worker",
        description=description + " backend=" + backend, version="1.0.0",
        capabilities=AgentCapabilities(streaming=False, push_notifications=False),
        default_input_modes=["text/plain"], default_output_modes=["text/plain"],
        skills=[AgentSkill(id="baton-task", name="Baton task", description=description, tags=["baton", "backend=" + backend])],
        supported_interfaces=[AgentInterface(protocol_binding="JSONRPC", protocol_version="0.3", url=public_url() + "/a2a/codex")],
    )


def create_app():
    backend = worker_mode()
    init_db()
    card = agent_card(backend)
    engine = create_async_engine("sqlite+aiosqlite:///" + str(STATE / "a2a.sqlite3"))
    store = DatabaseTaskStore(engine)
    executor = CodexExecutor(backend)
    health = WorkerHealth(executor, backend=backend)
    handler = DefaultRequestHandler(agent_executor=executor, task_store=store, agent_card=card)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        health.start()
        try:
            yield
        finally:
            await health.stop()

    app = FastAPI(lifespan=lifespan)
    app.state.worker_health = health

    @app.get("/healthz")
    async def worker_health():
        try:
            result = health.snapshot()
        except Exception:
            result = sanitize_health_snapshot(None)
        return JSONResponse(result, status_code=200 if result["status"] == "healthy" else 503)

    add_a2a_routes_to_fastapi(
        app, agent_card_routes=create_agent_card_routes(agent_card=card),
        jsonrpc_routes=create_jsonrpc_routes(request_handler=handler, rpc_url="/a2a/codex", enable_v0_3_compat=True),
    )
    return AuthMiddleware(app, role="dispatcher")


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    uvicorn.run(create_app(), host="0.0.0.0", port=9001, access_log=False, log_level="warning")
