# SPDX-License-Identifier: Apache-2.0
"""Baton's own authenticated Space HTTP/MCP API and fenced worker bridge."""

import asyncio
import contextlib
import hashlib
import inspect
import json
import logging
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
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

from bridge import common, webhooks
from bridge.common import AuthMiddleware, audit, redact, safe_id, transport_origins, uuid_id
from bridge.failures import failure_from_a2a
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
    "work_face_incomplete": (409, "Task work face is incomplete"),
    "rate_limited": (429, "Too many requests"),
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
                allowed = {"submit_task": {"idempotency_key", "prompt", "page_id", "scopes", "assignee_principal", "depends_on",
                                           "work_face", "require_work_face", "task_kind", "priority", "escalate_to",
                                           "fallback_assignee"}, "get_task": {"task_id"},
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
    "create_task_block": (True, {"page_id": (str, ...), "prompt": (str, ...), "scopes": (list[dict] | None, None), "reviewers": (list[str], ...), "base_commit": (str | None, None), "context": (Any, None), "evidence": (Any, None), "assignee_principal": (str | None, None), "depends_on": (list[str] | None, None),
                          "work_face": (dict[str, Any] | None, None), "require_work_face": (bool, False), "task_kind": (str, "construction"),
                          "priority": (str, "normal"), "escalate_to": (str | None, None), "fallback_assignee": (str | None, None)}),
    "submit_task": (True, {"idempotency_key": (str, ...), "prompt": (str, ...), "page_id": (str | None, None), "scopes": (list[dict] | None, None), "assignee_principal": (str | None, None), "depends_on": (list[str] | None, None),
                    "work_face": (dict[str, Any] | None, None), "require_work_face": (bool, False), "task_kind": (str, "construction"),
                    "priority": (str, "normal"), "escalate_to": (str | None, None), "fallback_assignee": (str | None, None)}),
    "assign_task": (True, {"task_id": (str, ...), "target_principal": (str, ...)}),
    "set_priority": (True, {"task_id": (str, ...), "priority": (str, ...)}),
    "plan": (True, {"graph": (list[GraphEntry], ...)}),
    "set_work_face": (True, {"task_id": (str, ...), "work_face": (dict[str, Any], ...), "base_commit": (str | None, None)}),
    "ready": (False, {"page_id": (str | None, None), "limit": (int, 50)}),
    "get_task": (False, {"task_id": (str, ...)}),
    "list_tasks": (False, {"limit": (int, 50)}),
    "fetch_inbox": (False, {"limit": (int, 30)}),
    "wait_inbox": (False, {"after_seq": (int, 0), "timeout_seconds": (int, 25), "limit": (int, 50)}),
    "webhook_subscribe": (True, {"url": (str, ...), "events": (list[str] | None, None)}),
    "webhook_list": (False, {}),
    "webhook_delete": (True, {"subscription_id": (str, ...)}),
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


def principal_directory(principal):
    """Resolve a principal name for notification routing; the registry is reread so revocation is immediate."""
    try:
        who = common.resolve_principal(principal)
        return actor_from_identity(who) if who else None
    except (BridgeError, ValueError, TypeError):
        return None


def get_store():
    global _STORE
    if _STORE is None:
        _STORE = SpaceStore.from_env(principal_directory)
    return _STORE


class Wakeup:
    """In-process hint that a mutation committed; waiters still re-read PostgreSQL, which stays the authority."""

    def __init__(self):
        self.event = None

    def ticket(self):
        if self.event is None:
            self.event = asyncio.Event()
        return self.event

    def notify(self):
        event, self.event = self.event, None
        if event is not None:
            event.set()

    @staticmethod
    async def wait(ticket, timeout):
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(ticket.wait(), timeout)


WAKEUP = Wakeup()
WAIT_MAX_SECONDS = 55
WAIT_POLL_SECONDS = 2.0
WAIT_SLOTS = 256
WAIT_SLOTS_PER_PRINCIPAL = 4
_WAITERS = {}
WEBHOOK_BATCH = 20
WEBHOOK_CONCURRENCY = 8
WEBHOOK_IDLE_SECONDS = 1.0
ESCALATION_SECONDS = 30
_WEBHOOK_POOL = ThreadPoolExecutor(max_workers=WEBHOOK_CONCURRENCY, thread_name_prefix="baton-webhook")


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
        result = await asyncio.to_thread(getattr(store, method), *args, **kwargs)
    except StoreError as exc:
        raise BridgeError(exc.code) from None
    except BridgeError:
        raise
    except Exception:
        # SQL, DSNs and driver diagnostics must never cross an error boundary.
        raise BridgeError("storage_unavailable") from None
    if PUBLIC_OPERATIONS[method][0] if method in PUBLIC_OPERATIONS else method == "finish_auto":
        WAKEUP.notify()
    return result


async def wait_inbox(actor, after_seq, timeout_seconds, limit):
    """Long poll: return as soon as a visible notification exists, else wait without busy looping."""
    if after_seq < 0 or not 0 <= timeout_seconds <= WAIT_MAX_SECONDS or not 1 <= limit <= 100:
        raise BridgeError("invalid")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    cursor, slot = after_seq, False
    try:
        while True:
            ticket = WAKEUP.ticket()
            result = await store_call("poll_notifications", actor, after_seq=cursor, limit=limit)
            more = result.pop("more")
            if result["notifications"]:
                return result
            cursor = result["next_seq"]
            remaining = deadline - loop.time()
            if remaining <= 0:
                return result
            if more:
                continue
            if not slot:
                if sum(_WAITERS.values()) >= WAIT_SLOTS or _WAITERS.get(actor.principal, 0) >= WAIT_SLOTS_PER_PRINCIPAL:
                    raise BridgeError("rate_limited")
                _WAITERS[actor.principal] = _WAITERS.get(actor.principal, 0) + 1
                slot = True
            await WAKEUP.wait(ticket, min(remaining, WAIT_POLL_SECONDS))
    finally:
        if slot:
            _WAITERS[actor.principal] -= 1
            if not _WAITERS[actor.principal]:
                del _WAITERS[actor.principal]


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
    if operation == "wait_inbox":
        return await wait_inbox(actor, **arguments)
    result = await store_call(operation, actor, **arguments)
    if operation == "submit_task":
        audit("task.replay" if result.get("replay") else "task.submitted",
              task_id=result.get("task_id", result.get("id")), agent=actor.principal,
              request_id=request.headers.get("x-request-id"),
              input_sha256=None if result.get("replay") else hashlib.sha256(arguments["prompt"].encode()).hexdigest())
        if not result.get("replay"):
            audit("thread.submitted", task_id=result.get("task_id", result.get("id")),
                  thread_id=result.get("thread_id"), agent=actor.principal)
    if operation in {"get_task", "submit_task", "create_task_block", "assign_task", "set_work_face", "set_priority", "claim", "renew", "handoff", "submit_result", "gate"}:
        return public_task(result)
    if operation in {"list_tasks", "ready"}:
        return [public_task(row) for row in result]
    return result


@mcp.tool()
async def submit_task(idempotency_key: str, prompt: str, ctx: Context, page_id: str | None = None,
                      scopes: list[dict] | None = None, assignee_principal: str | None = None,
                      depends_on: list[str] | None = None, work_face: dict[str, Any] | None = None,
                      require_work_face: bool = False, task_kind: str = "construction", priority: str = "normal",
                      escalate_to: str | None = None, fallback_assignee: str | None = None) -> dict:
    """Submit a task; a named assignee uses the manual claim/review path. Reuse keys on retry."""
    request = ctx.request_context.request
    actor = request_actor(request)
    operation_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "baton/submit_task/" + actor.principal + "/" + idempotency_key))
    return await invoke("submit_task", {"operation_id": operation_id, "idempotency_key": idempotency_key,
                        "prompt": prompt, "page_id": page_id, "scopes": scopes,
                        "assignee_principal": assignee_principal, "depends_on": depends_on,
                        "work_face": work_face, "require_work_face": require_work_face, "task_kind": task_kind,
                        "priority": priority, "escalate_to": escalate_to, "fallback_assignee": fallback_assignee}, request)


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


def dispatch_owner_current(task):
    """Recheck the saved submitting identity at the external-effect boundary."""
    who = common.resolve_principal(task.get("owner"))
    return bool(who and who.get("role") == "client" and who.get("agent") == task.get("owner")
                and who.get("account") == task.get("owner_account")
                and who.get("kind") == task.get("owner_kind"))


async def cancel_dispatch(task):
    """Persist cancellation under the current fence without sending to A2A."""
    try:
        stopped = await store_call("interrupt_auto", SERVICE,
                                   operation_id(task["id"], task["epoch"], "cancelled"),
                                   task["id"], task["epoch"], error_code="cancelled")
    except BridgeError as exc:
        audit("task.dispatch_unresolved", task_id=task["id"], agent=task["owner"],
              epoch=task["epoch"], error_code=exc.code)
        return None
    audit("task.cancelled", task_id=task["id"], agent=task["owner"],
          epoch=task["epoch"], error_code="cancelled")
    return stopped


async def dispatch(task):
    task_id, epoch = task["id"], task["epoch"]
    # Registry revocation/account changes must not inherit the service actor's
    # authority. Page and dependency ACL checks remain solely in the store.
    if not dispatch_owner_current(task):
        return await cancel_dispatch(task)
    # Commit the ambiguity boundary before sending a request to another process.
    await store_call("mark_dispatched", SERVICE, operation_id(task_id, epoch, "start"), task_id, epoch)
    # The database await may have waited on a lock while the registry changed.
    # Recheck before creating the HTTP client; this cannot recall a sent request.
    if not dispatch_owner_current(task):
        return await cancel_dispatch(task)
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
        if result.get("status", {}).get("state") == "failed":
            # Only the worker's versioned code/stage is accepted; its text is never stored.
            failure = failure_from_a2a(result["status"])
            failed = await store_call("fail_auto", SERVICE, operation_id(task_id, epoch, "fail"),
                                      task_id, epoch, failure.code, a2a_id)
            audit("task.failed", task_id=task_id, agent=task["owner"], a2a_task_id=a2a_id, epoch=epoch,
                  error_code=failure.code, stage=failure.stage)
            audit("thread.failed", task_id=task_id, thread_id=task_id, agent=task["owner"])
            return failed
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


async def deliver_webhook(item, gate):
    body = webhooks.encode(item["payload"])
    headers = {"Content-Type": "application/json", "User-Agent": "baton-webhook/1", "X-Baton-Event": item["payload"]["event"],
               "X-Baton-Delivery": item["notification_id"], "X-Baton-Signature": webhooks.sign(item["secret"], body)}
    started = time.monotonic()
    async with gate:
        try:
            outcome, status = await asyncio.get_running_loop().run_in_executor(_WEBHOOK_POOL, webhooks.post, item["url"], body, headers)
        except Exception:
            outcome, status = "network_error", None
    try:
        await store_call("webhook_record", SERVICE, item["subscription_id"], item["notification_id"], item["attempts"], outcome,
                         http_status=status, duration_ms=int((time.monotonic() - started) * 1000))
    except BridgeError as exc:
        audit("webhook.record_unavailable", error_code=exc.code)


async def webhook_deliver_once():
    items = await store_call("webhook_claim_due", SERVICE, limit=WEBHOOK_BATCH)
    gate = asyncio.Semaphore(WEBHOOK_CONCURRENCY)
    await asyncio.gather(*(deliver_webhook(item, gate) for item in items))
    return len(items)


async def webhook_loop():
    while True:
        busy = 0
        try:
            busy = await webhook_deliver_once()
        except BridgeError as exc:
            audit("webhook.queue_unavailable", error_code=exc.code)
        await asyncio.sleep(0.1 if busy else WEBHOOK_IDLE_SECONDS)


async def escalation_pass():
    try:
        result = await store_call("escalate_overdue", SERVICE)
    except BridgeError as exc:
        audit("task.escalation_unavailable", error_code=exc.code)
        return
    if result["escalated"]:
        WAKEUP.notify()


async def escalation_loop():
    """Escalate overdue assignments; the store's advisory lock keeps replicas from escalating twice."""
    while True:
        await escalation_pass()
        await asyncio.sleep(ESCALATION_SECONDS)


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
        tasks = [asyncio.create_task(worker_loop()), asyncio.create_task(webhook_loop()), asyncio.create_task(escalation_loop())]
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task


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
