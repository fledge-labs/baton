# SPDX-License-Identifier: Apache-2.0
"""Baton's own authenticated Space HTTP/MCP API and fenced worker bridge."""

import asyncio
import contextlib
import hashlib
import inspect
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, ConfigDict, ValidationError, create_model

from bridge import common
from bridge.common import AuthMiddleware, audit, redact, safe_id, transport_origins, uuid_id
from bridge.space_store import Actor, SpaceStore, StoreError

PROTOCOL = common.SPACE_PROTOCOL
GATEWAY = os.environ.get("GATEWAY_INTERNAL", "http://gateway:3000").rstrip("/")
SERVICE = Actor("codex-worker", "baton-service", "agent")
LEASE_SECONDS = 300
RENEW_SECONDS = 60
_STORE = None

ERRORS = {
    "invalid": (422, "Invalid request"),
    "not_found": (404, "Resource not found"),
    "forbidden": (403, "Operation is not permitted"),
    "operation_conflict": (409, "Operation conflicts with an earlier request"),
    "scope_conflict": (409, "Requested scope is unavailable"),
    "stale_claim": (409, "Claim is no longer current"),
    "revision_conflict": (409, "Revision is no longer current"),
    "gate_required": (409, "Required gate has not passed"),
    "dependency_blocked": (409, "Task dependencies are not complete"),
    "graph_cycle": (409, "Dependency graph contains a cycle"),
    "storage_unavailable": (503, "Storage is unavailable"),
}


class BridgeError(ValueError):
    def __init__(self, code):
        self.code = code if code in ERRORS else "storage_unavailable"
        super().__init__(ERRORS[self.code][1])


class SafeFastMCP(FastMCP):
    async def call_tool(self, name, arguments):
        # FastMCP's default validation errors include the rejected input. Keep
        # only our stable code, including when validation fails before a tool.
        try:
            if arguments is None:
                arguments = {}
            canonical = ALIASES.get(name, name)
            if canonical not in PUBLIC_OPERATIONS:
                raise BridgeError("invalid")
            if name in {"submit_task", "get_task", "list_tasks", "fetch_inbox"}:
                allowed = {"submit_task": {"idempotency_key", "prompt", "page_id", "scopes", "assignee_principal", "depends_on"}, "get_task": {"task_id"},
                           "list_tasks": set(), "fetch_inbox": set()}[name]
                if not isinstance(arguments, dict) or set(arguments) - allowed:
                    raise BridgeError("invalid")
            else:
                MODELS[canonical].model_validate(arguments)
            return await super().call_tool(name, arguments)
        except Exception as exc:
            code = "invalid"
            current = exc
            for _ in range(8):
                if isinstance(current, BridgeError):
                    code = current.code
                    break
                current = getattr(current, "__cause__", None)
                if current is None:
                    break
            raise ToolError(code + ": " + ERRORS[code][1]) from None


allowed_hosts, allowed_origins = transport_origins()
mcp = SafeFastMCP(
    "Baton Spaces and Tasks", stateless_http=True, json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=allowed_hosts, allowed_origins=allowed_origins,
    ),
)

# This allowlist is the public protocol. Internal service operations are never
# reflected into HTTP or MCP. Parameters describe business targets, not actors.
# A tuple is (annotation, default); Ellipsis marks a required argument.
class GraphEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    task_id: str
    depends_on: list[str]


PUBLIC_OPERATIONS = {
    "create_space": (True, {"name": (str, ...)}),
    "get_space": (False, {"space_id": (str, ...)}),
    "list_spaces": (False, {}),
    "create_page": (True, {"space_id": (str, ...), "title": (str, ...), "parent_id": (str | None, None)}),
    "get_page": (False, {"page_id": (str, ...)}),
    "list_pages": (False, {"space_id": (str, ...), "parent_id": (str | None, None)}),
    "create_block": (True, {"page_id": (str, ...), "kind": (str, ...), "content": (Any, ...), "position": (int | None, None)}),
    "get_block": (False, {"block_id": (str, ...)}),
    "list_blocks": (False, {"page_id": (str, ...)}),
    "update_block": (True, {"block_id": (str, ...), "expected_revision": (int, ...), "content": (Any, ...)}),
    "revert_block": (True, {"block_id": (str, ...), "expected_revision": (int, ...), "target_revision": (int, ...)}),
    "share": (True, {"resource_type": (str, ...), "resource_id": (str, ...), "subject_type": (str, ...), "subject_id": (str, ...), "role": (str | None, ...)}),
    "effective_role": (False, {"resource_type": (str, ...), "resource_id": (str, ...)}),
    "create_team": (True, {"name": (str, ...)}),
    "set_team_member": (True, {"team_id": (str, ...), "account": (str, ...), "member": (bool, ...)}),
    "comment": (True, {"page_id": (str, ...), "body": (str, ...), "thread_id": (str | None, None), "selection": (Any, None)}),
    "list_comments": (False, {"page_id": (str, ...), "thread_id": (str | None, None)}),
    "mention": (True, {"comment_id": (str, ...), "target_type": (str, ...), "target_id": (str, ...)}),
    "proposal_create": (True, {"block_id": (str, ...), "expected_revision": (int, ...), "proposed_content": (Any, ...), "selection": (Any, None)}),
    "proposal_review": (True, {"proposal_id": (str, ...), "decision": (str, ...), "content": (Any, None)}),
    "get_proposal": (False, {"proposal_id": (str, ...)}),
    "list_proposals": (False, {"block_id": (str, ...)}),
    "list_attribution": (False, {"block_id": (str, ...)}),
    "create_task_block": (True, {"page_id": (str, ...), "prompt": (str, ...), "scopes": (list[dict], ...), "reviewers": (list[str], ...), "base_commit": (str | None, None), "context": (Any, None), "evidence": (Any, None), "assignee_principal": (str | None, None), "depends_on": (list[str] | None, None)}),
    "submit_task": (True, {"idempotency_key": (str, ...), "prompt": (str, ...), "page_id": (str | None, None), "scopes": (list[dict] | None, None), "assignee_principal": (str | None, None), "depends_on": (list[str] | None, None)}),
    "assign_task": (True, {"task_id": (str, ...), "target_principal": (str, ...)}),
    "plan": (True, {"graph": (list[GraphEntry], ...)}),
    "ready": (False, {"page_id": (str | None, None), "limit": (int, 50)}),
    "get_task": (False, {"task_id": (str, ...)}),
    "list_tasks": (False, {"limit": (int, 50)}),
    "fetch_inbox": (False, {"limit": (int, 30)}),
    "claim": (True, {"task_id": (str, ...), "lease_seconds": (int, 300)}),
    "renew": (True, {"task_id": (str, ...), "epoch": (int, ...), "lease_seconds": (int, 300)}),
    "submit_result": (True, {"task_id": (str, ...), "epoch": (int, ...), "result": (str, ...), "evidence": (Any, None)}),
    "handoff": (True, {"task_id": (str, ...), "epoch": (int, ...), "target_principal": (str, ...), "base_commit": (str | None, None), "context": (Any, None), "evidence": (Any, None), "lease_seconds": (int, 300)}),
    "gate": (True, {"task_id": (str, ...), "expected_epoch": (int, ...), "decision": (str, ...), "evidence": (Any, None)}),
    "get_claim": (False, {"task_id": (str, ...)}),
    "list_claims": (False, {}),
    "get_scopes": (False, {"task_id": (str, ...)}),
    "list_audit": (False, {"task_id": (str | None, None), "space_id": (str | None, None), "limit": (int, 100)}),
}
ALIASES = {"x_claim": "claim", "x_scope": "get_scopes", "x_handoff": "handoff", "x_gate": "gate", "x_audit": "list_audit"}
MODELS = {
    name: create_model(name + "Request", __config__=ConfigDict(extra="forbid", strict=True),
                       **({"operation_id": (str, ...)} if mutation else {}), **fields)
    for name, (mutation, fields) in PUBLIC_OPERATIONS.items()
}


def get_store():
    global _STORE
    if _STORE is None:
        _STORE = SpaceStore.from_env()
    return _STORE


def actor_from_identity(who):
    try:
        if who["role"] != "client" or who["kind"] not in {"human", "agent"}:
            raise ValueError("Invalid actor")
        return Actor(safe_id(who["agent"]), safe_id(who["account"]), who["kind"])
    except (ValueError, KeyError, TypeError):
        raise BridgeError("forbidden") from None


def request_actor(request):
    return actor_from_identity(request.state.identity)


async def store_call(method, *args, **kwargs):
    try:
        store = get_store()
        return await asyncio.to_thread(getattr(store, method), *args, **kwargs)
    except StoreError as exc:
        raise BridgeError(exc.code) from None
    except BridgeError:
        raise
    except Exception:
        # SQL, DSNs and driver diagnostics must never cross an error boundary.
        raise BridgeError("storage_unavailable") from None


def public_task(row):
    # Manual recipients need the authorized task instructions. The automatic
    # legacy status view continues to omit prompts and all views omit keys.
    excluded = {"idem", "idempotency_key", "token", "dispatch_started"}
    if row.get("execution_mode") != "manual":
        excluded.add("prompt")
    task = {key: value for key, value in row.items() if key not in excluded}
    for previous, current in (("submission_mail_status", "submission_thread_status"),
                              ("mail_status", "thread_status"), ("mail_error", "thread_error")):
        if previous in task:
            task[current] = task.pop(previous)
    return task


async def invoke(operation, payload, request):
    operation = ALIASES.get(operation, operation)
    if operation not in PUBLIC_OPERATIONS or not isinstance(payload, dict):
        raise BridgeError("invalid")
    try:
        arguments = MODELS[operation].model_validate(payload).model_dump()
        actor = request_actor(request)
        if PUBLIC_OPERATIONS[operation][0]:
            arguments["operation_id"] = uuid_id(arguments["operation_id"])
        if operation in {"handoff", "assign_task"}:
            who = common.resolve_principal(arguments.pop("target_principal"))
            if not who:
                raise BridgeError("invalid")
            arguments["target"] = actor_from_identity(who)
        if operation in {"submit_task", "create_task_block"}:
            principal = arguments.pop("assignee_principal", None)
            who = common.resolve_principal(principal) if principal is not None else None
            if principal is not None and not who:
                raise BridgeError("invalid")
            arguments["assignee"] = actor_from_identity(who) if who else None
        if operation == "mention":
            target = common.resolve_principal(arguments["target_id"])
            expected_kind = {"person": "human", "agent": "agent"}.get(arguments["target_type"])
            if not target or not expected_kind or target["kind"] != expected_kind:
                raise BridgeError("invalid")
    except (ValidationError, ValueError, TypeError, AttributeError) as exc:
        if isinstance(exc, BridgeError):
            raise
        raise BridgeError("invalid") from None
    result = await store_call(operation, actor, **arguments)
    if operation == "submit_task":
        audit("task.replay" if result.get("replay") else "task.submitted",
              task_id=result.get("task_id", result.get("id")), agent=actor.principal,
              request_id=request.headers.get("x-request-id"),
              input_sha256=None if result.get("replay") else hashlib.sha256(arguments["prompt"].encode()).hexdigest())
        if not result.get("replay"):
            audit("thread.submitted", task_id=result.get("task_id", result.get("id")),
                  thread_id=result.get("thread_id"), agent=actor.principal)
    if operation in {"get_task", "submit_task", "create_task_block", "assign_task", "claim", "renew", "handoff", "submit_result", "gate"}:
        return public_task(result)
    if operation in {"list_tasks", "ready"}:
        return [public_task(row) for row in result]
    return result


@mcp.tool()
async def submit_task(idempotency_key: str, prompt: str, ctx: Context, page_id: str | None = None,
                      scopes: list[dict] | None = None, assignee_principal: str | None = None,
                      depends_on: list[str] | None = None) -> dict:
    """Submit a task; a named assignee uses the manual claim/review path. Reuse keys on retry."""
    request = ctx.request_context.request
    actor = request_actor(request)
    operation_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "baton/submit_task/" + actor.principal + "/" + idempotency_key))
    return await invoke("submit_task", {"operation_id": operation_id, "idempotency_key": idempotency_key,
                        "prompt": prompt, "page_id": page_id, "scopes": scopes,
                        "assignee_principal": assignee_principal, "depends_on": depends_on}, request)


@mcp.tool()
async def get_task(task_id: str, ctx: Context) -> dict:
    """Read a task with current page permissions; claimed/done map to working/completed."""
    return await invoke("get_task", {"task_id": task_id}, ctx.request_context.request)


@mcp.tool()
async def list_tasks(ctx: Context) -> list[dict]:
    """List tasks submitted by this authenticated principal."""
    return await invoke("list_tasks", {}, ctx.request_context.request)


@mcp.tool()
async def fetch_inbox(ctx: Context) -> dict:
    """Read current mentions and assignments; access is checked at every read."""
    return await invoke("fetch_inbox", {}, ctx.request_context.request)


def register_space_tool(name, operation):
    """Expose the same explicit typed protocol through MCP, with no second ACL."""
    async def tool(ctx, **arguments):
        return {"x_protocol": PROTOCOL, "result": await invoke(operation, arguments, ctx.request_context.request)}
    tool.__name__ = name
    tool.__annotations__ = {"ctx": Context, "return": dict}
    fields = MODELS[ALIASES.get(operation, operation)].model_fields
    parameters = []
    for key, field in fields.items():
        default = inspect.Parameter.empty if field.is_required() else field.default
        parameters.append(inspect.Parameter(key, inspect.Parameter.KEYWORD_ONLY, default=default, annotation=field.annotation))
    parameters.append(inspect.Parameter("ctx", inspect.Parameter.KEYWORD_ONLY, annotation=Context))
    tool.__signature__ = inspect.Signature(parameters, return_annotation=dict)
    mcp.add_tool(tool, name=name, description="Baton x_ extension: " + operation + ". Actor comes from authentication; mutation retries reuse operation_id.")


for tool_name in sorted(PUBLIC_OPERATIONS.keys() | ALIASES.keys()):
    if tool_name not in {"submit_task", "get_task", "list_tasks", "fetch_inbox"}:
        register_space_tool(tool_name, tool_name)


class DispatchUncertain(RuntimeError):
    """A request may have executed remotely; an automatic retry is forbidden."""


def operation_id(task_id, epoch, phase):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"baton/dispatch/{task_id}/{epoch}/{phase}"))


async def dispatch(task):
    task_id, epoch = task["id"], task["epoch"]
    # Commit the ambiguity boundary before sending a request to another process.
    await store_call("mark_dispatched", SERVICE, operation_id(task_id, epoch, "start"), task_id, epoch)
    audit("task.dispatched", task_id=task_id, agent=task["owner"], epoch=epoch)
    lease_lost = asyncio.Event()

    async def heartbeat():
        while True:
            await asyncio.sleep(RENEW_SECONDS)
            try:
                await store_call("renew_auto", SERVICE, str(uuid.uuid4()), task_id, epoch, lease_seconds=LEASE_SECONDS)
            except Exception:
                lease_lost.set()
                return

    renewer = asyncio.create_task(heartbeat())
    try:
        token = (common.SECRETS / "bridge" / "dispatcher.token").read_text().strip()
        payload = {"jsonrpc": "2.0", "id": task_id, "method": "message/send", "params": {
            "message": {"kind": "message", "role": "user", "messageId": task_id,
                        "parts": [{"kind": "text", "text": json.dumps({
                            "task_id": task_id, "owner": task["owner"], "prompt": task["prompt"],
                        })}]}, "configuration": {"blocking": True}}}
        async with httpx.AsyncClient(timeout=350) as http:
            response = await http.post(GATEWAY + "/a2a/codex", json=payload, headers={
                "Authorization": "Bearer " + token, "X-Request-ID": task_id, "A2A-Version": "0.3",
            })
            response.raise_for_status()
            result = response.json()
        if lease_lost.is_set() or "error" in result:
            raise DispatchUncertain("Dispatch requires reconciliation")
        result = result["result"]
        a2a_id = uuid_id(result["id"])
        artifacts = result.get("artifacts", [])
        output = "\n".join(part.get("text", "") for artifact in artifacts for part in artifact.get("parts", []) if part.get("kind") == "text")
        backend = next((artifact.get("metadata", {}).get("backend") for artifact in artifacts
                        if artifact.get("metadata", {}).get("backend") in {"codex", "demo"}), None)
        if result.get("status", {}).get("state") != "completed" or not output or not backend:
            raise DispatchUncertain("Dispatch requires reconciliation")
        output = redact(output)[:32000]
        completed = await store_call("finish_auto", SERVICE, operation_id(task_id, epoch, "finish"),
                                     task_id, epoch, output, a2a_id, backend, a2a_state="completed")
        audit("task.completed", task_id=task_id, agent=task["owner"], a2a_task_id=a2a_id, epoch=epoch,
              backend=backend, output_sha256=hashlib.sha256(output.encode()).hexdigest())
        audit("thread.completed", task_id=task_id, thread_id=task_id, agent=task["owner"])
        return completed
    except Exception:
        # Includes fenced-out results. Do not publish output or blindly retry.
        raise DispatchUncertain("Dispatch outcome requires manual reconciliation") from None
    finally:
        renewer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await renewer


async def claim_next():
    return await store_call("claim_next", SERVICE, str(uuid.uuid4()), lease_seconds=LEASE_SECONDS)


async def recover_auto():
    return await store_call("recover_auto", SERVICE, str(uuid.uuid4()))


async def process_next():
    await recover_auto()
    task = await claim_next()
    if not task:
        return False
    try:
        await dispatch(task)
    except Exception:
        # This CAS can fail if the lease expired or another holder now owns the
        # task. In that case recovery owns the state, never the old dispatcher.
        try:
            await store_call("interrupt_auto", SERVICE, operation_id(task["id"], task["epoch"], "interrupt"),
                             task["id"], task["epoch"], error_code="dispatch_uncertain")
        except BridgeError as exc:
            audit("task.dispatch_unresolved", task_id=task["id"], agent=task["owner"],
                  epoch=task["epoch"], error_code=exc.code)
        else:
            audit("task.interrupted", task_id=task["id"], agent=task["owner"], epoch=task["epoch"], error_code="dispatch_uncertain")
    return True


async def worker_loop():
    while True:
        try:
            await process_next()
        except BridgeError as exc:
            audit("task.queue_unavailable", error_code=exc.code)
        await asyncio.sleep(1)


@asynccontextmanager
async def lifespan(app):
    await store_call("check_schema", SERVICE)
    await recover_auto()
    async with mcp.session_manager.run():
        worker = asyncio.create_task(worker_loop())
        try:
            yield
        finally:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker


app = FastAPI(lifespan=lifespan)


@app.exception_handler(BridgeError)
async def bridge_error(request, exc):
    status, message = ERRORS[exc.code]
    return JSONResponse(status_code=status, content={"x_protocol": PROTOCOL, "error": {"code": exc.code, "message": message}})


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    return await bridge_error(request, BridgeError("invalid"))


@app.get("/healthz")
async def health():
    await store_call("check_schema", SERVICE)
    return {"status": "ok", "service": "baton-bridge", "x_protocol": PROTOCOL}


@app.post("/v1/space/{operation}")
async def space_operation(operation: str, request: Request):
    try:
        payload = await request.json()
    except Exception:
        raise BridgeError("invalid") from None
    result = await invoke(operation, payload, request)
    return {"x_protocol": PROTOCOL, "result": result}


@app.get("/v1/space/schema")
async def space_schema():
    return {"x_protocol": PROTOCOL, "operations": {
        name: {"mutation": mutation, "request": MODELS[name].model_json_schema()}
        for name, (mutation, _) in PUBLIC_OPERATIONS.items()
    }, "aliases": ALIASES}


@app.get("/board/data")
async def board_data(request: Request):
    tasks = await invoke("list_tasks", {}, request)
    inbox = await invoke("fetch_inbox", {}, request)
    return {"x_protocol": PROTOCOL, "agent": request.state.identity["agent"], "tasks": tasks,
            "inbox": inbox.get("messages", [])}


app.mount("/", mcp.streamable_http_app())
app = AuthMiddleware(app, role="client")


if __name__ == "__main__":
    # Audit records are allowlisted metadata; third-party errors can contain
    # SQL, headers, payloads or credential values, so disable dependency logs.
    logging.disable(logging.CRITICAL)
    uvicorn.run(app, host="0.0.0.0", port=9000, access_log=False, log_level="warning")
