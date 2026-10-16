# SPDX-License-Identifier: Apache-2.0
"""Authenticated MCP tools, task ledger, and Agent Mail completion reports."""

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from bridge.common import (
    SECRETS, AuthMiddleware, audit, database, redact, safe_id, transport_origins, uuid_id,
)

PROJECT = "/baton"
MAIL_URL = os.environ.get("MAIL_URL", "http://mail:8765/mcp/")
GATEWAY = os.environ.get("GATEWAY_INTERNAL", "http://gateway:3000").rstrip("/")
allowed_hosts, allowed_origins = transport_origins()
mcp = FastMCP(
    "Baton Tasks and Agent Mail", stateless_http=True, json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=allowed_hosts, allowed_origins=allowed_origins,
    ),
)
mail_identity_lock = asyncio.Lock()
TASK_COLUMNS = "id,status,result,a2a_id,error,submission_mail_status,mail_status,mail_error,backend"


def init_db():
    with database("tasks.sqlite3") as db:
        db.execute("""CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY, owner TEXT NOT NULL, idem TEXT NOT NULL, prompt TEXT NOT NULL,
            status TEXT NOT NULL, result TEXT, a2a_id TEXT, error TEXT,
            submission_mail_status TEXT NOT NULL DEFAULT 'pending',
            mail_status TEXT NOT NULL DEFAULT 'not_ready', mail_error TEXT, backend TEXT,
            UNIQUE(owner,idem))""")
        db.execute("CREATE TABLE IF NOT EXISTS agents (id TEXT PRIMARY KEY, name TEXT NOT NULL, token TEXT NOT NULL)")
        rows = db.execute("SELECT id,owner FROM tasks WHERE status='working'").fetchall()
        db.execute("""UPDATE tasks SET status='interrupted',
            error='Dispatch outcome is ambiguous; manual reconciliation required'
            WHERE status='working'""")
        db.execute("UPDATE tasks SET submission_mail_status='interrupted' WHERE submission_mail_status='sending'")
        db.execute("""UPDATE tasks SET mail_status='interrupted',
            mail_error='Mail delivery is unconfirmed; manual reconciliation required'
            WHERE mail_status IN ('pending','sending')""")
    for row in rows:
        audit("task.interrupted", task_id=row["id"], agent=row["owner"])


def owner(ctx):
    return ctx.request_context.request.state.identity["agent"]


def lookup(task_id, agent):
    uuid_id(task_id)
    with database("tasks.sqlite3") as db:
        row = db.execute("SELECT * FROM tasks WHERE id=? AND owner=?", (task_id, agent)).fetchone()
    if not row:
        raise ValueError("Task not found")
    return dict(row)


def public_task(row):
    return {key: row[key] for key in TASK_COLUMNS.split(",")}


async def _mail_request(name, args):
    token = (SECRETS / "mail.token").read_text().strip()
    async with httpx.AsyncClient(headers={"Authorization": "Bearer " + token}, timeout=30) as http:
        async with streamable_http_client(MAIL_URL, http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(name, args)
                if result.isError:
                    raise RuntimeError("Mail tool failed")
                if result.structuredContent is not None:
                    return result.structuredContent
                return json.loads("\n".join(c.text for c in result.content if c.type == "text"))


async def mail_call(name, args):
    try:
        return await _mail_request(name, args)
    except Exception:
        # MCP error content is upstream-controlled and can quote submitted
        # registration credentials. Never put it in a client error response.
        raise RuntimeError("Mail request failed") from None


async def mail_agent(agent):
    async with mail_identity_lock:
        with database("tasks.sqlite3") as db:
            row = db.execute("SELECT name,token FROM agents WHERE id=?", (agent,)).fetchone()
        if row:
            return dict(row)
        await mail_call("ensure_project", {"human_key": PROJECT})
        # Upstream generates valid recipient names; our stable identity stays private.
        result = await mail_call("register_agent", {
            "project_key": PROJECT, "program": agent, "model": "baton-worker" if agent == "codex-worker" else "mcp-client",
            "task_description": "Baton task coordination",
        })
        record = {"name": result["name"], "token": result["registration_token"]}
        if not record["name"] or not record["token"]:
            raise RuntimeError("Mail registration failed")
        with database("tasks.sqlite3") as db:
            db.execute("INSERT INTO agents(id,name,token) VALUES (?,?,?)", (agent, record["name"], record["token"]))
        return record


def clean_mail(result):
    """Project only display fields. Registration credentials never cross this API."""
    messages = result.get("result", result.get("messages", [])) if isinstance(result, dict) else result
    if not isinstance(messages, list):
        return []
    with database("tasks.sqlite3") as db:
        tokens = [row[0] for row in db.execute("SELECT token FROM agents")]
    fields = {"id", "thread_id", "subject", "from", "to", "created_ts", "body_md", "importance", "ack_required"}
    cleaned = []
    for message in messages:
        if isinstance(message, dict):
            item = {key: value for key, value in message.items() if key in fields}
            # Redact both scalar text and nested recipient lists without trusting keys.
            cleaned.append(json.loads(redact(json.dumps(item), tokens)))
    return cleaned


async def mail_event(task, phase, body):
    column = "submission_mail_status" if phase == "submitted" else "mail_status"
    with database("tasks.sqlite3") as db:
        db.execute(f"UPDATE tasks SET {column}='sending' WHERE id=?", (task["id"],))
    try:
        client = await mail_agent(task["owner"])
        worker = await mail_agent("codex-worker")
        sender, recipient = (client, worker) if phase == "submitted" else (worker, client)
        await mail_call("macro_contact_handshake", {
            "project_key": PROJECT, "requester": sender["name"], "target": recipient["name"],
            "auto_accept": True, "requester_registration_token": sender["token"],
            "target_registration_token": recipient["token"],
        })
        result = await mail_call("send_message", {
            "project_key": PROJECT, "sender_name": sender["name"], "sender_token": sender["token"],
            "to": [recipient["name"]], "subject": f'[{phase}] {task["id"]}',
            "body_md": redact(body, [sender["token"], recipient["token"]]), "thread_id": task["id"],
        })
        if result.get("count") != 1 or not result.get("verified_sender"):
            raise RuntimeError("Mail delivery not confirmed")
        message_ids = [d.get("payload", {}).get("id") for d in result.get("deliveries", [])]
        with database("tasks.sqlite3") as db:
            db.execute(f"UPDATE tasks SET {column}='sent',mail_error=NULL WHERE id=?", (task["id"],))
        audit("mail." + phase, task_id=task["id"], agent=task["owner"], mail_message_ids=message_ids)
    except Exception as exc:
        with database("tasks.sqlite3") as db:
            db.execute(f"UPDATE tasks SET {column}='failed',mail_error=? WHERE id=?",
                       ("Mail delivery is unconfirmed; manual reconciliation required", task["id"]))
        audit("mail.report_failed", task_id=task["id"], agent=task["owner"], error_type=type(exc).__name__)
        raise


@mcp.tool()
async def submit_task(idempotency_key: str, prompt: str, ctx: Context) -> dict:
    """Queue a task. Reusing the same key and input never executes twice."""
    safe_id(idempotency_key)
    if not prompt.strip() or len(prompt) > 16000:
        raise ValueError("Prompt must contain 1..16000 characters")
    agent = owner(ctx)
    task_id = str(uuid.uuid5(uuid.NAMESPACE_URL, PROJECT + "/" + agent + "/" + idempotency_key))
    with database("tasks.sqlite3") as db:
        db.execute("BEGIN IMMEDIATE")
        prior = db.execute("SELECT prompt,status FROM tasks WHERE owner=? AND idem=?", (agent, idempotency_key)).fetchone()
        if prior:
            if prior["prompt"] != prompt:
                raise ValueError("Idempotency key already used for different input")
            status, replay = prior["status"], True
        else:
            db.execute("INSERT INTO tasks(id,owner,idem,prompt,status) VALUES (?,?,?,?,?)",
                       (task_id, agent, idempotency_key, prompt, "queued"))
            status, replay = "queued", False
    audit("task.replay" if replay else "task.submitted", task_id=task_id, agent=agent,
          request_id=ctx.request_context.request.headers.get("x-request-id"),
          input_sha256=None if replay else hashlib.sha256(prompt.encode()).hexdigest())
    return {"task_id": task_id, "status": status, "replay": replay}


@mcp.tool()
async def get_task(task_id: str, ctx: Context) -> dict:
    """Read your task's execution and Mail delivery states separately."""
    return public_task(lookup(task_id, owner(ctx)))


@mcp.tool()
async def list_tasks(ctx: Context) -> list[dict]:
    """List your most recent tasks and their completion-report delivery states."""
    with database("tasks.sqlite3") as db:
        return [dict(row) for row in db.execute(
            f"SELECT {TASK_COLUMNS} FROM tasks WHERE owner=? ORDER BY rowid DESC LIMIT 50", (owner(ctx),)
        )]


@mcp.tool()
async def fetch_inbox(ctx: Context) -> dict:
    """Read the Agent Mail inbox bound to your bearer-token identity."""
    try:
        who = await mail_agent(owner(ctx))
        result = await mail_call("fetch_inbox", {
            "project_key": PROJECT, "agent_name": who["name"], "registration_token": who["token"],
            "include_bodies": True, "limit": 30,
        })
        return {"agent_name": who["name"], "messages": clean_mail(result)}
    except Exception as exc:
        audit("mail.inbox_failed", agent=owner(ctx), error_type=type(exc).__name__)
        raise RuntimeError("Mail inbox is unavailable") from None


class DispatchUncertain(RuntimeError):
    """The worker may have run; never automatically dispatch again."""


async def dispatch(task):
    task_id = task["id"]
    await mail_event(task, "submitted", task["prompt"])
    token = (SECRETS / "dispatcher.token").read_text().strip()
    payload = {
        "jsonrpc": "2.0", "id": task_id, "method": "message/send",
        "params": {"message": {"kind": "message", "role": "user", "messageId": task_id,
            "parts": [{"kind": "text", "text": json.dumps({
                "task_id": task_id, "owner": task["owner"], "prompt": task["prompt"],
            })}]}, "configuration": {"blocking": True}},
    }
    audit("task.dispatched", task_id=task_id, agent=task["owner"])
    try:
        async with httpx.AsyncClient(timeout=350) as http:
            response = await http.post(GATEWAY + "/a2a/codex", json=payload, headers={
                "Authorization": "Bearer " + token, "X-Request-ID": task_id, "A2A-Version": "0.3",
            })
            response.raise_for_status()
            result = response.json()
        if "error" in result:
            raise RuntimeError("A2A returned protocol error")
        result = result["result"]
        a2a_id = uuid_id(result["id"])
        artifacts = result.get("artifacts", [])
        output = "\n".join(p.get("text", "") for a in artifacts for p in a.get("parts", []) if p.get("kind") == "text")
        backend = next((a.get("metadata", {}).get("backend") for a in artifacts
                        if a.get("metadata", {}).get("backend") in {"codex", "demo"}), None)
        if result.get("status", {}).get("state") != "completed" or not output or not backend:
            raise RuntimeError("A2A task did not complete")
        with database("tasks.sqlite3") as db:
            tokens = [row[0] for row in db.execute("SELECT token FROM agents")]
        output = redact(output, tokens)[:32000]
        with database("tasks.sqlite3") as db:
            db.execute("""UPDATE tasks SET status='completed',result=?,a2a_id=?,backend=?,mail_status='pending'
                WHERE id=?""", (output, a2a_id, backend, task_id))
        audit("task.completed", task_id=task_id, agent=task["owner"], a2a_task_id=a2a_id,
              backend=backend, output_sha256=hashlib.sha256(output.encode()).hexdigest())
    except Exception as exc:
        raise DispatchUncertain("Dispatch outcome requires manual reconciliation") from exc
    try:
        await mail_event(task, "completed", output)
    except Exception:
        # The model result remains completed even if the completion report fails.
        pass


def claim_next():
    with database("tasks.sqlite3") as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM tasks WHERE status='queued' ORDER BY rowid LIMIT 1").fetchone()
        if row:
            db.execute("UPDATE tasks SET status='working' WHERE id=? AND status='queued'", (row["id"],))
            return dict(row)
    return None


async def worker_loop():
    while True:
        task = claim_next()
        if task:
            try:
                await dispatch(task)
            except Exception as exc:
                status = "interrupted" if isinstance(exc, DispatchUncertain) else "failed"
                with database("tasks.sqlite3") as db:
                    db.execute("UPDATE tasks SET status=?,error=? WHERE id=? AND status!='completed'",
                               (status, "Task stopped; manual reconciliation required", task["id"]))
                audit("task." + status, task_id=task["id"], agent=task["owner"], error_type=type(exc).__name__)
        await asyncio.sleep(1)


@asynccontextmanager
async def lifespan(app):
    init_db()
    async with mcp.session_manager.run():
        worker = asyncio.create_task(worker_loop())
        try:
            yield
        finally:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker


app = FastAPI(lifespan=lifespan)


@app.get("/healthz")
async def health():
    return {"status": "ok", "service": "baton-bridge"}


@app.get("/board/data")
async def board_data(request: Request):
    agent = request.state.identity["agent"]
    with database("tasks.sqlite3") as db:
        tasks = [dict(row) for row in db.execute(
            f"SELECT {TASK_COLUMNS} FROM tasks WHERE owner=? ORDER BY rowid DESC LIMIT 50", (agent,)
        )]
    try:
        who = await mail_agent(agent)
        inbox = await mail_call("fetch_inbox", {
            "project_key": PROJECT, "agent_name": who["name"], "registration_token": who["token"],
            "include_bodies": True, "limit": 30,
        })
        return {"agent": agent, "tasks": tasks, "mail": clean_mail(inbox)}
    except Exception as exc:
        audit("mail.inbox_failed", agent=agent, error_type=type(exc).__name__)
        raise HTTPException(status_code=503, detail="Mail inbox is unavailable") from None


app.mount("/", mcp.streamable_http_app())
app = AuthMiddleware(app, role="client")


if __name__ == "__main__":
    # stdout is reserved for our allowlisted audit records; dependency errors may
    # otherwise include request bodies, headers, or upstream response content.
    logging.disable(logging.CRITICAL)
    uvicorn.run(app, host="0.0.0.0", port=9000, access_log=False, log_level="warning")
