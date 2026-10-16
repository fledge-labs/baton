# SPDX-License-Identifier: Apache-2.0
"""A2A worker backed by sandboxed Codex, or an explicit protocol-only demo."""

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import signal
import tempfile
from pathlib import Path

import uvicorn
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import add_a2a_routes_to_fastapi, create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import TaskUpdater
from a2a.server.tasks.database_task_store import DatabaseTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill, Part, Task, TaskState, TaskStatus
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine

from bridge.common import (
    CODEX_HOME, CONFIG, STATE, AuthMiddleware, audit, database, known_secrets, public_url, redact, safe_id, uuid_id,
)

CODEX_VERSION = "0.154.0"
WORK_ROOT = Path("/work")


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
        rows = db.execute("SELECT id,a2a_id,backend FROM executions WHERE status='running'").fetchall()
        db.execute("UPDATE executions SET status='interrupted' WHERE status='running'")
    for row in rows:
        audit("worker.interrupted", task_id=row["id"], a2a_task_id=row["a2a_id"], backend=row["backend"])


def codex_command(wd, final):
    """Only the unique task directory is writable by model-generated commands."""
    denied = {"/data", "/config", "/run/secrets", "/home/agent/.codex", str(STATE), str(CONFIG), str(CODEX_HOME)}
    paths = ",".join(json.dumps(path) + '="deny"' for path in sorted(denied))
    permissions = ('permissions.baton={extends=":read-only",filesystem={' + paths
                   + "," + json.dumps(str(wd)) + '="write"},network={enabled=false}}')
    command = [
        "codex", "exec", "--ignore-user-config", "--ignore-rules", "--skip-git-repo-check", "--ephemeral",
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


class CodexExecutor(AgentExecutor):
    def __init__(self, backend=None):
        self.backend = backend or worker_mode()
        if self.backend not in {"demo", "codex"}:
            raise ValueError("Unknown worker backend")
        self.processes = {}
        self.lock = asyncio.Lock()

    async def run_codex(self, prompt, a2a_id):
        before = known_secrets()
        with tempfile.TemporaryDirectory(prefix="task-", dir=WORK_ROOT) as directory:
            wd = Path(directory)
            (wd / ".tmp").mkdir(mode=0o700)
            final = wd / "result.txt"
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
                raise RuntimeError("Unexpected Codex version")
            instructions = (
                "You are a coding worker in Baton. Work only on the supplied task in its isolated directory. "
                "Do not inspect credentials, other tasks, services, or production systems. Never reveal secrets.\n\n"
                + prompt
            )
            process = await asyncio.create_subprocess_exec(
                *codex_command(wd, final), cwd=wd, env=env, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, start_new_session=True,
            )
            self.processes[a2a_id] = process
            try:
                await asyncio.wait_for(process.communicate(instructions.encode()), timeout=300)
            except BaseException:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
                raise
            if process.returncode or not final.is_file():
                raise RuntimeError("Codex execution failed")
            # Bound artifacts before decoding; stderr and tool transcripts are never logged.
            with final.open("rb") as result:
                output = result.read(128000).decode("utf-8", errors="replace")
            output = redact(output, before)[:32000]
            if not output.strip():
                raise RuntimeError("Codex returned no result")
            return output

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
                    db.execute("INSERT INTO executions VALUES (?,?,?,?,?,?)",
                               (task_id, digest, "running", None, context.task_id, self.backend))
            if existing:
                if existing["digest"] != digest or existing["status"] != "completed":
                    await updater.failed()
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
                output = "BATON_DEMO_OK" if self.backend == "demo" else await self.run_codex(prompt, context.task_id)
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
                with database("executions.sqlite3") as db:
                    db.execute("UPDATE executions SET status='failed' WHERE id=? AND status!='completed'", (task_id,))
                await updater.failed()
                audit("worker.failed", task_id=task_id, backend=self.backend, error_type=type(exc).__name__)
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
    handler = DefaultRequestHandler(agent_executor=CodexExecutor(backend), task_store=store, agent_card=card)
    app = FastAPI()
    add_a2a_routes_to_fastapi(
        app, agent_card_routes=create_agent_card_routes(agent_card=card),
        jsonrpc_routes=create_jsonrpc_routes(request_handler=handler, rpc_url="/a2a/codex", enable_v0_3_compat=True),
    )
    return AuthMiddleware(app, role="dispatcher")


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    uvicorn.run(create_app(), host="0.0.0.0", port=9001, access_log=False, log_level="warning")
