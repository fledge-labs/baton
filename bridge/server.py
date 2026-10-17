# SPDX-License-Identifier: Apache-2.0
"""Baton's own authenticated Space HTTP/MCP API and its notification and escalation loops."""

import asyncio
import contextlib
import hashlib
import inspect
import json
import logging
import re
import time
import types
import typing
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, ConfigDict, ValidationError, create_model

from bridge import a2a_public, common, webhooks
from bridge.common import AuthMiddleware, audit, safe_id, transport_origins, uuid_id
from bridge.space_store import LEGACY_SERVICE_PRINCIPAL, SERVICE_ACTOR, Actor, StoreError

PROTOCOL = common.SPACE_PROTOCOL
SERVICE = SERVICE_ACTOR
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
    "delegation_required": (403, "An active approver delegation covering the task is required"),
    "dependency_blocked": (409, "Task dependencies are not complete"),
    "graph_cycle": (409, "Dependency graph contains a cycle"),
    "work_face_incomplete": (409, "Task work face is incomplete"),
    "task_canceled": (409, "Task was canceled through A2A"),
    "rate_limited": (429, "Too many requests"),
    "storage_unavailable": (503, "Storage is unavailable"),
    "protocol_incompatible": (409, "Client protocol version is no longer accepted"),
}
# Current space protocol generation and the oldest client generation this server still accepts.
# The window is one protocol version: clients at PROTOCOL_VERSION or PROTOCOL_VERSION-1 (and clients that
# omit the version header, which are treated as the previous generation) keep working. x_protocol stays baton.space.v1.
PROTOCOL_VERSION = 2
MIN_CLIENT_VERSION = 1


class BridgeError(ValueError):
    def __init__(self, code, details=None):
        self.code = code if code in ERRORS else "storage_unavailable"
        # Field-level hints ({"field", "expected"}) for 422 responses; static text only, never request values.
        self.details = list(details or []) if self.code == "invalid" else []
        super().__init__(ERRORS[self.code][1])


MAX_DETAILS = 20
FIELD_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
UUID_FIELDS = frozenset({"operation_id", "space_id", "page_id", "parent_id", "block_id", "task_id", "comment_id",
                         "proposal_id", "team_id", "thread_id", "subscription_id", "resource_id", "request_id"})
TYPE_NAMES = {"string_type": "string", "int_type": "integer", "int_parsing": "integer", "int_from_float": "integer",
              "bool_type": "boolean", "bool_parsing": "boolean", "list_type": "array", "dict_type": "object",
              "model_type": "object", "model_attributes_type": "object", "none_required": "null",
              "float_type": "number", "float_parsing": "number", "too_short": "non-empty value",
              "string_too_long": "shorter string", "string_too_short": "non-empty string"}
UNKNOWN_FIELD = "not a field of this operation; see GET /v1/space/schema"


def describe(annotation):
    """Short, value-free description of a request field's type, derived from its pydantic annotation."""
    origin, arguments = typing.get_origin(annotation), typing.get_args(annotation)
    if annotation is Any:
        return "any JSON value"
    if annotation is type(None):
        return "null"
    if origin in (typing.Union, types.UnionType):
        parts = []
        for part in arguments:
            text = describe(part)
            if text not in parts:
                parts.append(text)
        return " or ".join(parts)
    if origin is list:
        return "array of " + describe(arguments[0]) if arguments else "array"
    if origin is dict or annotation is dict:
        return "object"
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return "object with keys " + ", ".join(annotation.model_fields)
    return {str: "string", int: "integer", bool: "boolean", float: "number"}.get(annotation, "valid value")


def field_path(location):
    path = ""
    for part in location:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            path += ("." if path else "") + (part if isinstance(part, str) and FIELD_NAME.fullmatch(part) else "?")
    return path or "body"


def validation_details(errors, model=None):
    """Turn pydantic/FastAPI error records into field/expected pairs, reading only locations and error types."""
    fields = model.model_fields if model is not None else {}
    details = []
    for error in errors:
        location, kind = tuple(error.get("loc", ())), error.get("type", "")
        if location and location[0] in {"body", "query", "path"} and len(location) > 1 and location[0] not in fields:
            location = location[1:]
        field = field_path(location)
        top = location[0] if location and isinstance(location[0], str) else None
        known = fields.get(top) if len(location) == 1 else None
        if kind == "extra_forbidden":
            expected = UNKNOWN_FIELD
            if len(location) == 1 and not FIELD_NAME.fullmatch(str(top)):
                field = "<unknown>"
        elif known is not None:
            expected = describe(known.annotation)
            if top in UUID_FIELDS:
                expected = expected.replace("string", "UUID string")
            if kind == "missing":
                expected = "required " + expected
        else:
            expected = ("required value" if kind == "missing" else TYPE_NAMES.get(kind, "valid value"))
        item = {"field": field, "expected": expected}
        if item not in details:
            details.append(item)
    return details[:MAX_DETAILS]


def unknown_field_details(arguments, allowed):
    return [{"field": key if FIELD_NAME.fullmatch(key) else "<unknown>", "expected": UNKNOWN_FIELD}
            for key in sorted(map(str, set(arguments) - allowed))][:MAX_DETAILS]


class SafeFastMCP(FastMCP):
    async def call_tool(self, name, arguments):
        # FastMCP's default validation errors include the rejected input. Keep
        # only our stable code, including when validation fails before a tool.
        def tool_error(exc):
            code, details = "invalid", []
            current = exc
            for _ in range(8):
                if isinstance(current, BridgeError):
                    code, details = current.code, current.details
                    break
                if isinstance(current, ValidationError):
                    details = validation_details(current.errors(include_url=False, include_context=False, include_input=False),
                                                 MODELS.get(ALIASES.get(name, name)))
                    break
                current = getattr(current, "__cause__", None)
                if current is None:
                    break
            message = code + ": " + ERRORS[code][1]
            if details:
                message += "; details=" + json.dumps(details, separators=(",", ":"))
            raise ToolError(message) from None

        try:
            if arguments is None:
                arguments = {}
            canonical = ALIASES.get(name, name)
            if canonical not in PUBLIC_OPERATIONS:
                raise BridgeError("invalid")
            if name in {"submit_task", "get_task", "list_tasks", "fetch_inbox"}:
                allowed = {"submit_task": {"idempotency_key", "prompt", "page_id", "scopes", "assignee_principal", "depends_on",
                                           "work_face", "require_work_face", "task_kind", "priority", "escalate_to",
                                           "fallback_assignee", "approvals_required", "reviewers", "role"}, "get_task": {"task_id"},
                           "list_tasks": set(), "fetch_inbox": set()}[name]
                if not isinstance(arguments, dict):
                    raise BridgeError("invalid", [{"field": "arguments", "expected": "object"}])
                if set(arguments) - allowed:
                    raise BridgeError("invalid", unknown_field_details(arguments, allowed))
            else:
                MODELS[canonical].model_validate(arguments)
        except Exception as exc:
            context = self.get_context()
            request = context.request_context.request if context and context.request_context else None
            if request is not None and isinstance(exc, (BridgeError, ValidationError)):
                note_deprecated(ALIASES.get(name, name), arguments if isinstance(arguments, dict) else {}, None, True, request)
            tool_error(exc)
        try:
            return await super().call_tool(name, arguments)
        except Exception as exc:
            tool_error(exc)


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
    "list_comments": (False, {"page_id": (str | None, None), "thread_id": (str | None, None), "task_id": (str | None, None)}),
    "mention": (True, {"comment_id": (str, ...), "target_type": (str, ...), "target_id": (str, ...)}),
    "proposal_create": (True, {"block_id": (str, ...), "expected_revision": (int, ...), "proposed_content": (Any, ...), "selection": (Any, None)}),
    "proposal_review": (True, {"proposal_id": (str, ...), "decision": (str, ...), "content": (Any, None)}),
    "get_proposal": (False, {"proposal_id": (str, ...)}),
    "list_proposals": (False, {"block_id": (str, ...)}),
    "list_attribution": (False, {"block_id": (str, ...)}),
    "create_task_block": (True, {"page_id": (str, ...), "prompt": (str, ...), "scopes": (list[dict] | None, None), "reviewers": (list[str], ...), "base_commit": (str | None, None), "context": (Any, None), "evidence": (Any, None), "assignee_principal": (str | None, None), "depends_on": (list[str] | None, None),
                          "work_face": (dict[str, Any] | None, None), "require_work_face": (bool, False), "task_kind": (str, "construction"),
                          "priority": (str, "normal"), "escalate_to": (str | None, None), "fallback_assignee": (str | None, None),
                          "approvals_required": (int | None, None), "role": (str | None, None)}),
    "submit_task": (True, {"idempotency_key": (str, ...), "prompt": (str, ...), "page_id": (str | None, None), "scopes": (list[dict] | None, None), "assignee_principal": (str | None, None), "depends_on": (list[str] | None, None),
                    "work_face": (dict[str, Any] | None, None), "require_work_face": (bool, False), "task_kind": (str, "construction"),
                    "priority": (str, "normal"), "escalate_to": (str | None, None), "fallback_assignee": (str | None, None),
                    "approvals_required": (int | None, None), "reviewers": (list[str] | None, None), "role": (str | None, None)}),
    "assign_task": (True, {"task_id": (str, ...), "target_principal": (str, ...)}),
    "set_priority": (True, {"task_id": (str, ...), "priority": (str, ...)}),
    "mark_blocked": (True, {"task_id": (str, ...), "reason_code": (str, ...), "note": (str | None, None)}),
    "request_permission": (True, {"task_id": (str, ...), "epoch": (int, ...), "cli": (str, ...), "tool": (str, ...),
                                  "directory": (str | None, None), "reason": (str | None, None), "excerpt": (str | None, None),
                                  "approvers": (list[str] | None, None)}),
    "decide_permission": (True, {"task_id": (str, ...), "request_id": (str, ...), "option": (str, ...), "reason": (str | None, None)}),
    "record_permission": (True, {"task_id": (str, ...), "request_id": (str, ...), "event": (str, ...)}),
    "list_permissions": (False, {"task_id": (str, ...)}),
    "plan": (True, {"graph": (list[GraphEntry], ...)}),
    "set_work_face": (True, {"task_id": (str, ...), "work_face": (dict[str, Any], ...), "base_commit": (str | None, None)}),
    "ready": (False, {"page_id": (str | None, None), "limit": (int, 50)}),
    "ready_for_role": (False, {"role": (str, ...), "limit": (int, 50)}),
    "set_task_role": (True, {"task_id": (str, ...), "role": (str | None, ...)}),
    "set_role_rules": (True, {"space_id": (str, ...), "rules": (list[dict[str, Any]], ...)}),
    "set_role_sla": (True, {"space_id": (str, ...), "sla_minutes": (dict[str, int] | None, ...)}),
    "assign_role": (True, {"space_id": (str, ...), "principal": (str, ...), "role": (str, ...)}),
    "revoke_role": (True, {"space_id": (str, ...), "principal": (str, ...), "role": (str, ...)}),
    "list_roles": (False, {"space_id": (str, ...)}),
    "get_task": (False, {"task_id": (str, ...)}),
    "list_tasks": (False, {"limit": (int, 50)}),
    "fetch_inbox": (False, {"limit": (int, 30)}),
    "wait_inbox": (False, {"after_seq": (int, 0), "timeout_seconds": (int, 25), "limit": (int, 50)}),
    "webhook_subscribe": (True, {"url": (str, ...), "events": (list[str] | None, None), "include_preview": (bool, False)}),
    "webhook_list": (False, {}),
    "webhook_delete": (True, {"subscription_id": (str, ...)}),
    "webhook_rotate_secret": (True, {"subscription_id": (str, ...)}),
    "webhook_revoke_previous_secret": (True, {"subscription_id": (str, ...)}),
    "claim": (True, {"task_id": (str, ...), "lease_seconds": (int, 300)}),
    "renew": (True, {"task_id": (str, ...), "epoch": (int, ...), "lease_seconds": (int, 300)}),
    "submit_result": (True, {"task_id": (str, ...), "epoch": (int, ...), "result": (str, ...), "evidence": (Any, None)}),
    "handoff": (True, {"task_id": (str, ...), "epoch": (int, ...), "target_principal": (str, ...), "base_commit": (str | None, None), "context": (Any, None), "evidence": (Any, None), "lease_seconds": (int, 300)}),
    "gate": (True, {"task_id": (str, ...), "expected_epoch": (int, ...), "decision": (str, ...), "evidence": (Any, None),
                    "self_approval": (bool, False), "edited_result": (str | None, None), "reason": (str | None, None)}),
    "delegation_create": (True, {"delegate_principal": (str, ...), "scope": (str, "approve"), "space_id": (str | None, None),
                                 "expires_at": (str | None, None), "may_delegate": (bool, False), "reason": (str, ...)}),
    "delegation_revoke": (True, {"delegation_id": (str, ...)}),
    "delegation_list": (False, {}),
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


def valid_uuid(value):
    """An omitted optional id is fine (the store treats None and "" alike); anything else must be a UUID."""
    if not value:
        return True
    try:
        uuid_id(value)
    except (ValueError, TypeError, AttributeError):
        return False
    return True


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
        _STORE = a2a_public.CancelAwareStore.from_env(principal_directory)
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
        raise BridgeError(exc.code, exc.details) from None
    except BridgeError:
        raise
    except Exception:
        # SQL, DSNs and driver diagnostics must never cross an error boundary.
        raise BridgeError("storage_unavailable") from None
    if method in PUBLIC_OPERATIONS and PUBLIC_OPERATIONS[method][0]:
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
    # Manual tasks show their authorized instructions. Legacy automatic (codex) rows
    # stay readable but keep omitting prompts, and all views omit keys.
    excluded = {"idem", "idempotency_key", "token", "dispatch_started"}
    if row.get("execution_mode") != "manual":
        excluded.add("prompt")
    task = {key: value for key, value in row.items() if key not in excluded}
    for previous, current in (("submission_mail_status", "submission_thread_status"),
                              ("mail_status", "thread_status"), ("mail_error", "thread_error")):
        if previous in task:
            task[current] = task.pop(previous)
    return task


DEPRECATION_NOTICE = "[deprecated] Baton no longer develops document collaboration; use tasks, comments and links instead."
DEPRECATION_HEADERS = ((b"deprecation", b"true"), (b"link", b'<docs/api.md#deprecated>; rel="deprecation"'))
# Deprecated for every call; the value is the log "kind" used when the block kind is not known.
DEPRECATED_ALWAYS = {"update_block": "block", "proposal_create": "proposal", "proposal_review": "proposal",
                     "get_proposal": "proposal", "list_proposals": "proposal", "list_attribution": "attribution"}
# Deprecated only when a non-task block is involved (kind='task' blocks stay supported).
DEPRECATED_NON_TASK = frozenset({"create_block", "get_block", "list_blocks", "revert_block"})
DEPRECATED_TOOLS = frozenset(DEPRECATED_ALWAYS) | DEPRECATED_NON_TASK
SAFE_KIND = re.compile(r"[a-z][a-z_]{0,31}")
DEPRECATED_CALLS = {}


def log_kind(value):
    return value if isinstance(value, str) and SAFE_KIND.fullmatch(value) else "other"


def deprecated_kind(operation, payload, result, failed):
    """Return the log kind when this call is deprecated, else None. Reads only operation names and block kinds."""
    arguments = payload if isinstance(payload, dict) else {}
    if operation in DEPRECATED_ALWAYS:
        kind = result.get("kind") if isinstance(result, dict) else None
        return log_kind(kind) if kind is not None else DEPRECATED_ALWAYS[operation]
    if operation == "create_block":
        kind = arguments.get("kind")
        return log_kind(kind) if isinstance(kind, str) and kind != "task" else None
    if operation in {"get_block", "revert_block"} and not failed and isinstance(result, dict):
        return log_kind(result.get("kind")) if result.get("kind") != "task" else None
    if operation == "list_blocks" and not failed and isinstance(result, list):
        kinds = sorted({log_kind(row.get("kind")) for row in result if isinstance(row, dict) and row.get("kind") != "task"})
        return ",".join(kinds) if kinds else None
    if operation == "comment" and arguments.get("selection") is not None:
        return "selection"
    return None


def note_deprecated(operation, payload, result, failed, request):
    kind = deprecated_kind(operation, payload, result, failed)
    if kind is None:
        return
    request.state.baton_deprecated = True
    try:
        principal = request_actor(request).principal
    except BridgeError:
        principal = "unknown"
    DEPRECATED_CALLS[operation] = DEPRECATED_CALLS.get(operation, 0) + 1
    print(json.dumps({"time": datetime.now(timezone.utc).isoformat(), "event": "deprecated.call", "operation": operation,
                      "principal": principal, "kind": kind,
                      "metric": "baton_deprecated_calls_total", "value": DEPRECATED_CALLS[operation]}), flush=True)


def deprecated_metrics():
    """Prometheus text exposition of baton_deprecated_calls_total{operation}."""
    lines = ["# TYPE baton_deprecated_calls_total counter"]
    lines += [f'baton_deprecated_calls_total{{operation="{name}"}} {count}' for name, count in sorted(DEPRECATED_CALLS.items())]
    return "\n".join(lines) + "\n"


class DeprecationHeaders:
    """Add Deprecation and Link headers to responses of calls that invoke() marked as deprecated."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def marked_send(message):
            if message["type"] == "http.response.start" and scope.get("state", {}).get("baton_deprecated"):
                message.setdefault("headers", []).extend(DEPRECATION_HEADERS)
            await send(message)

        await self.app(scope, receive, marked_send)


async def invoke(operation, payload, request):
    operation = ALIASES.get(operation, operation)
    try:
        result = await perform(operation, payload, request)
    except BridgeError:
        if operation in PUBLIC_OPERATIONS:
            note_deprecated(operation, payload, None, True, request)
        raise
    note_deprecated(operation, payload, result, False, request)
    return result


async def perform(operation, payload, request):
    if operation not in PUBLIC_OPERATIONS:
        raise BridgeError("invalid", [{"field": "operation", "expected": "an operation listed by GET /v1/space/schema"}])
    if not isinstance(payload, dict):
        raise BridgeError("invalid", [{"field": "body", "expected": "JSON object"}])
    try:
        try:
            arguments = MODELS[operation].model_validate(payload).model_dump()
        except ValidationError as exc:
            raise BridgeError("invalid", validation_details(
                exc.errors(include_url=False, include_context=False, include_input=False), MODELS[operation])) from None
        malformed = [{"field": name, "expected": "UUID string in canonical lowercase form"}
                     for name in sorted(UUID_FIELDS & arguments.keys()) if not valid_uuid(arguments[name])]
        if malformed:
            raise BridgeError("invalid", malformed)
        actor = request_actor(request)
        if PUBLIC_OPERATIONS[operation][0]:
            arguments["operation_id"] = uuid_id(arguments["operation_id"])
        if operation == "delegation_create":
            who = common.resolve_principal(arguments.pop("delegate_principal"))
            if not who:
                raise BridgeError("invalid", [{"field": "delegate_principal", "expected": "a principal registered in Baton"}])
            arguments["delegate"] = actor_from_identity(who)
        if operation in {"assign_role", "revoke_role"}:
            who = common.resolve_principal(arguments.pop("principal"))
            if (not who or who["agent"] in {SERVICE.principal, LEGACY_SERVICE_PRINCIPAL}
                    or who["account"] == SERVICE.account):
                raise BridgeError("invalid", [{"field": "principal", "expected": "a principal registered in Baton"}])
            if operation == "assign_role":
                arguments["target"] = actor_from_identity(who)
            else:
                arguments["principal"] = who["agent"]
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
    if operation in {"get_task", "submit_task", "create_task_block", "assign_task", "set_work_face", "set_priority", "mark_blocked", "set_task_role", "claim", "renew", "handoff", "submit_result", "gate"}:
        return public_task(result)
    if operation in {"list_tasks", "ready", "ready_for_role"}:
        return [public_task(row) for row in result]
    return result


@mcp.tool()
async def submit_task(idempotency_key: str, prompt: str, ctx: Context, page_id: str | None = None,
                      scopes: list[dict] | None = None, assignee_principal: str | None = None,
                      depends_on: list[str] | None = None, work_face: dict[str, Any] | None = None,
                      require_work_face: bool = False, task_kind: str = "construction", priority: str = "normal",
                      escalate_to: str | None = None, fallback_assignee: str | None = None,
                      approvals_required: int | None = None, reviewers: list[str] | None = None,
                      role: str | None = None) -> dict:
    """Create a manual task and return it; Baton never executes it. With assignee_principal only that principal may claim
    it; without one the task is unassigned and any principal with edit on the page sees it in ready and may claim it.
    Reviewers default to the requester; pass reviewers to name them. A reviewer who is not the space root and has no
    approver delegation is listed in warnings and the task is still created. Scopes default to none. An optional role puts an
    unassigned task in that role's queue (only current holders of the role may claim it); without one the space's
    role rules may derive it. Reuse keys on retry."""
    request = ctx.request_context.request
    actor = request_actor(request)
    operation_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "baton/submit_task/" + actor.principal + "/" + idempotency_key))
    return await invoke("submit_task", {"operation_id": operation_id, "idempotency_key": idempotency_key,
                        "prompt": prompt, "page_id": page_id, "scopes": scopes,
                        "assignee_principal": assignee_principal, "depends_on": depends_on,
                        "work_face": work_face, "require_work_face": require_work_face, "task_kind": task_kind,
                        "priority": priority, "escalate_to": escalate_to, "fallback_assignee": fallback_assignee,
                        "approvals_required": approvals_required, "reviewers": reviewers, "role": role}, request)


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
    description = "Baton x_ extension: " + operation + ". Actor comes from authentication; mutation retries reuse operation_id."
    if operation in DEPRECATED_TOOLS:
        description = DEPRECATION_NOTICE + (" Deprecated for non-task blocks only; task blocks (kind='task') stay supported. "
                                            if operation in DEPRECATED_NON_TASK else " ") + description
    elif operation == "comment":
        description += " The selection parameter is deprecated: " + DEPRECATION_NOTICE[len("[deprecated] "):]
    mcp.add_tool(tool, name=name, description=description)


for tool_name in sorted(PUBLIC_OPERATIONS.keys() | ALIASES.keys()):
    if tool_name not in {"submit_task", "get_task", "list_tasks", "fetch_inbox"}:
        register_space_tool(tool_name, tool_name)


def webhook_headers(item, keys, body, now):
    """Standard Webhooks headers, signed for this attempt, plus the deprecated X-Baton-* set (newest secret only)."""
    return {"Content-Type": "application/json", "User-Agent": "baton-webhook/1",
            **webhooks.standard_headers(keys, item["notification_id"], now, body),
            "X-Baton-Event": item["payload"]["event"], "X-Baton-Delivery": item["notification_id"],
            "X-Baton-Signature": webhooks.sign(keys[0], body)}


async def deliver_webhook(item, gate):
    body = webhooks.encode(item["payload"])
    started = time.monotonic()
    async with gate:
        try:
            # Secrets are read now, not at claim time, so a rotation or revocation after the claim applies to this
            # attempt. The remaining window is the time between this read and the request leaving.
            keys = await store_call("webhook_signing_secrets", SERVICE, item["subscription_id"])
        except BridgeError as exc:
            # Nothing was sent and no attempt is recorded: the claim lease expires and the delivery is claimed again.
            audit("webhook.queue_unavailable", error_code=exc.code)
            return
        if keys is None:
            return
        try:
            headers = webhook_headers(item, keys, body, int(time.time()))
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
    if result["escalated"] or result.get("role_sla"):
        WAKEUP.notify()


async def escalation_loop():
    """Escalate overdue assignments; the store's advisory lock keeps replicas from escalating twice."""
    while True:
        await escalation_pass()
        await asyncio.sleep(ESCALATION_SECONDS)


@asynccontextmanager
async def lifespan(app):
    await store_call("check_schema", SERVICE)
    async with mcp.session_manager.run():
        tasks = [asyncio.create_task(webhook_loop()), asyncio.create_task(escalation_loop())]
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task


app = FastAPI(lifespan=lifespan)


def versioned(body):
    """Space JSON carries the protocol generation and the oldest client generation still accepted."""
    return {"x_protocol": PROTOCOL, "protocol_version": PROTOCOL_VERSION, "min_client_version": MIN_CLIENT_VERSION, **body}


def client_protocol_version(scope):
    """Header absent means the previous generation, which the one-version window still accepts. A present but unusable value does not."""
    raw = None
    for key, value in scope.get("headers", []):
        if key.lower() == b"x-baton-client-version":
            raw = value
            break
    if raw is None:
        return MIN_CLIENT_VERSION
    try:
        text = raw.decode("ascii").strip()
    except UnicodeError:
        return 0
    if not text.isdigit() or not 1 <= len(text) <= 6:
        return 0
    return int(text)


class ProtocolVersion:
    """Report the protocol generation on every /v1/space response, and reject a client older than the compatibility window.

    Sits outside authentication so a too-old client learns the minimum version instead of an auth failure, and so
    401/403 responses carry the same headers. The client's supplied version is never copied into the response.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith("/v1/space"):
            return await self.app(scope, receive, send)
        if client_protocol_version(scope) < MIN_CLIENT_VERSION:
            status, message = ERRORS["protocol_incompatible"]
            body = json.dumps(versioned({"error": {"code": "protocol_incompatible", "message": message}}),
                              separators=(",", ":")).encode()
            await send({"type": "http.response.start", "status": status, "headers": [
                (b"content-type", b"application/json"), (b"cache-control", b"no-store"),
                (b"content-length", str(len(body)).encode()),
                (b"x-baton-protocol-version", str(PROTOCOL_VERSION).encode()),
                (b"x-baton-min-client-version", str(MIN_CLIENT_VERSION).encode()),
            ]})
            await send({"type": "http.response.body", "body": body})
            return

        async def send_with_version(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).extend((
                    (b"x-baton-protocol-version", str(PROTOCOL_VERSION).encode()),
                    (b"x-baton-min-client-version", str(MIN_CLIENT_VERSION).encode()),
                ))
            await send(message)

        await self.app(scope, receive, send_with_version)


@app.exception_handler(BridgeError)
async def bridge_error(request, exc):
    status, message = ERRORS[exc.code]
    error = {"code": exc.code, "message": message, **({"details": exc.details} if exc.details else {})}
    body = {"error": error}
    if request.url.path.startswith("/v1/space"):
        body = versioned(body)
    else:
        body = {"x_protocol": PROTOCOL, **body}
    return JSONResponse(status_code=status, content=body)


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    return await bridge_error(request, BridgeError("invalid", validation_details(exc.errors())))


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
    return versioned({"result": result})


@app.get("/v1/space/schema")
async def space_schema():
    return versioned({"operations": {
        name: {"mutation": mutation, "request": MODELS[name].model_json_schema()}
        for name, (mutation, _) in PUBLIC_OPERATIONS.items()
    }, "aliases": ALIASES})


@app.post(a2a_public.RPC_PATH)
async def a2a_rpc(request: Request):
    return await a2a_public.handle_rpc(request, store=get_store(), call=store_call, actor=request_actor(request), waker=WAKEUP)


@app.get(a2a_public.DIRECTORY_PATH)
async def a2a_directory(request: Request):
    return await a2a_public.directory_response(a2a_public.Context(get_store(), store_call, request_actor(request), waker=WAKEUP))


@app.get("/board/data")
async def board_data(request: Request):
    tasks = await invoke("list_tasks", {}, request)
    inbox = await invoke("fetch_inbox", {}, request)
    roles = await store_call("role_panels", request_actor(request))
    return {"x_protocol": PROTOCOL, "agent": request.state.identity["agent"], "tasks": tasks,
            "inbox": inbox.get("messages", []), "roles": roles}


app.mount("/", mcp.streamable_http_app())
app = a2a_public.PublicDiscovery(ProtocolVersion(AuthMiddleware(DeprecationHeaders(app), role="client")))


if __name__ == "__main__":
    # Audit records are allowlisted metadata; third-party errors can contain
    # SQL, headers, payloads or credential values, so disable dependency logs.
    logging.disable(logging.CRITICAL)
    uvicorn.run(app, host="0.0.0.0", port=9000, access_log=False, log_level="warning")
