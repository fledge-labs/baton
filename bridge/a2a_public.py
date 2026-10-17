# SPDX-License-Identifier: Apache-2.0
"""Baton's public A2A v1.0 interface: a signed Agent Card, an agent directory and a work-object-bound JSON-RPC endpoint.

The endpoint is for external agents. Everything here sits in front of the existing Space store: the caller is the authenticated Baton
principal, permissions are the shared-page model of `SpaceStore`, and every call is appended to `baton.x_audit`.

The instance signing key is created by `python -m bridge.a2a_public init-keys` inside the bridge's state volume. It is
never part of the repository and is never logged or printed; only the public JWK is published.
"""

import argparse
import asyncio
import base64
import dataclasses
import hashlib
import json
import os
import stat
import sys
import threading
import time
import uuid
from datetime import datetime, timezone

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature, encode_dss_signature
from fastapi.responses import JSONResponse, Response, StreamingResponse
from psycopg.types.json import Jsonb

from bridge import common
from bridge.common import safe_id, uuid_id
from bridge.space_store import Actor, SpaceStore, StoreError

A2A_VERSION = "1.0"
INTERFACE_VERSION = "1.0.0"
CARD_PATH = "/.well-known/agent-card.json"
JWKS_PATH = "/.well-known/jwks.json"
DIRECTORY_PATH = "/a2a/agents"
RPC_PATH = "/a2a/rpc"
EXTENSION_URI = "urn:fledge-labs:baton:a2a:work-object:v1"
CARD_MAX_AGE_SECONDS = 300
MAX_BODY_BYTES = 256 * 1024
MAX_MESSAGE_ID = 128

# Streams: one transaction per poll, polling backs off from STREAM_POLL_SECONDS to STREAM_MAX_POLL_SECONDS while a task is
# quiet (and is woken early by commits in this process), and every actor and the instance have a cap on open streams.
STREAM_POLL_SECONDS = 1.0
STREAM_MAX_POLL_SECONDS = 5.0
STREAM_MAX_SECONDS = 300.0
STREAM_KEEPALIVE_SECONDS = 15.0
MAX_STREAMS_PER_ACTOR = 4
MAX_STREAMS_TOTAL = 256
# A task's history is capped: a response or a stream start carries at most this many of the newest comments.
MAX_HISTORY = 200
EPOCH_CURSOR = ("1970-01-01T00:00:00+00:00", "00000000-0000-0000-0000-000000000000")

KEY_FILE = "signing-key.pem"
RETIRED_FILE = "retired-keys.json"
SIGNING_ALGORITHM = "ES256"

# Baton task states as one superset (space-replica.md, "Protocols" item 9) and their A2A v1.0 projection. The mapping is a
# bijection onto the eight A2A states a task can be in, so a projected state can always be turned back into the Baton one.
BATON_TO_A2A = {
    "submitted": "TASK_STATE_SUBMITTED",
    "working": "TASK_STATE_WORKING",
    "input_required": "TASK_STATE_INPUT_REQUIRED",
    "auth_required": "TASK_STATE_AUTH_REQUIRED",
    "completed": "TASK_STATE_COMPLETED",
    "failed": "TASK_STATE_FAILED",
    "canceled": "TASK_STATE_CANCELED",
    "rejected": "TASK_STATE_REJECTED",
}
A2A_TO_BATON = {value: key for key, value in BATON_TO_A2A.items()}
TERMINAL = frozenset({"completed", "failed", "canceled", "rejected"})
INTERRUPTED = frozenset({"input_required", "auth_required"})
# Task-store state to the superset. `awaiting_gate` is an open approval request, so it is `input_required` (an Ask);
# `interrupted` is a dead end of the task store (nothing leaves it), hence `failed`.
STORE_TO_BATON = {"queued": "submitted", "claimed": "working", "awaiting_gate": "input_required", "done": "completed",
                  "failed": "failed", "interrupted": "failed"}

# A2A v0.3 method names and their v1.0 replacements; every one is refused with a pointer to the new name.
V03_METHODS = {
    "message/send": "SendMessage",
    "message/stream": "SendStreamingMessage",
    "tasks/get": "GetTask",
    "tasks/cancel": "CancelTask",
    "tasks/resubscribe": "SubscribeToTask",
    "tasks/pushNotificationConfig/set": "CreateTaskPushNotificationConfig",
    "tasks/pushNotificationConfig/get": "GetTaskPushNotificationConfig",
    "tasks/pushNotificationConfig/list": "ListTaskPushNotificationConfigs",
    "tasks/pushNotificationConfig/delete": "DeleteTaskPushNotificationConfig",
    "agent/getAuthenticatedExtendedCard": "GetExtendedAgentCard",
}
PUSH_METHODS = frozenset({"CreateTaskPushNotificationConfig", "GetTaskPushNotificationConfig",
                          "ListTaskPushNotificationConfigs", "DeleteTaskPushNotificationConfig"})
WORK_OBJECT_KEYS = frozenset({"task_id", "page_id", "agent", "scopes"})
CANCEL_GUARDED = frozenset({"claim", "renew", "handoff", "submit_result", "gate", "assign_task"})

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = -32700, -32600, -32601, -32602, -32603
SERVER_ERROR = -32000
TASK_NOT_FOUND, TASK_NOT_CANCELABLE, PUSH_NOT_SUPPORTED, UNSUPPORTED_OPERATION = -32001, -32002, -32003, -32004
CONTENT_TYPE_NOT_SUPPORTED, VERSION_NOT_SUPPORTED = -32005, -32009

ERROR_INFO = "type.googleapis.com/google.rpc.ErrorInfo"
BAD_REQUEST = "type.googleapis.com/google.rpc.BadRequest"
CONFLICT_CODES = frozenset({"operation_conflict", "scope_conflict", "stale_claim", "revision_conflict", "gate_required",
                            "dependency_blocked", "graph_cycle", "work_face_incomplete", "task_canceled"})


class A2AError(Exception):
    """A JSON-RPC error with an optional google.rpc.ErrorInfo reason. Messages are static; request values never enter them."""

    def __init__(self, code, message, reason=None, metadata=None, violations=None):
        super().__init__(message)
        self.code, self.message, self.reason = code, message, reason
        self.metadata, self.violations = metadata or {}, violations or []

    def body(self, rpc_id):
        data = []
        if self.reason:
            data.append({"@type": ERROR_INFO, "reason": self.reason, "domain": "a2a-protocol.org", "metadata": self.metadata})
        if self.violations:
            data.append({"@type": BAD_REQUEST, "fieldViolations": self.violations})
        error = {"code": self.code, "message": self.message, **({"data": data} if data else {})}
        return {"jsonrpc": "2.0", "id": rpc_id, "error": error}


def invalid_params(field, expected, reason="INVALID_PARAMS"):
    return A2AError(INVALID_PARAMS, "Invalid parameters", reason, violations=[{"field": field, "description": expected}])


def translate(exc):
    """Map a store or bridge error (both carry a stable `code`) onto a JSON-RPC error without echoing request values."""
    if isinstance(exc, A2AError):
        return exc
    code = getattr(exc, "code", None)
    details = [{"field": item.get("field"), "description": item.get("expected")}
               for item in getattr(exc, "details", None) or [] if isinstance(item, dict)]
    if code == "invalid":
        return A2AError(INVALID_PARAMS, "Invalid parameters", "INVALID_PARAMS", violations=details)
    if code == "not_found":
        return A2AError(TASK_NOT_FOUND, "Task not found", "TASK_NOT_FOUND")
    if code in {"forbidden", "delegation_required"}:
        return A2AError(SERVER_ERROR, "Operation is not permitted", "PERMISSION_DENIED")
    if code in CONFLICT_CODES:
        return A2AError(SERVER_ERROR, "Operation conflicts with the current state", code.upper())
    if code == "rate_limited":
        return A2AError(SERVER_ERROR, "Too many requests", "RATE_LIMITED")
    return A2AError(INTERNAL_ERROR, "Internal error", "INTERNAL")


# --- RFC 8785 JSON canonicalization and JWS (RFC 7515) over it, A2A section 8.4 ---------------------------------------

def canonicalize(value):
    """RFC 8785 for the value space Agent Cards use: objects, arrays, strings, booleans, null and integers."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, int):
        if abs(value) > 2 ** 53:
            raise ValueError("Integer outside the interoperable range")
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(canonicalize(item) for item in value) + "]"
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("Object keys must be strings")
        return "{" + ",".join(json.dumps(key, ensure_ascii=False) + ":" + canonicalize(value[key])
                               for key in sorted(value, key=lambda key: key.encode("utf-16-be"))) + "}"
    raise ValueError("Value cannot be canonicalized")


def without_defaults(value):
    """Drop unset and default-valued members (null, empty string, empty array); explicit booleans stay (A2A 8.4.1)."""
    if isinstance(value, dict):
        kept = {}
        for key, item in value.items():
            item = without_defaults(item)
            if item is None or item == "" or item == []:
                continue
            kept[key] = item
        return kept
    if isinstance(value, list):
        return [without_defaults(item) for item in value]
    return value


def signing_payload(card):
    return canonicalize(without_defaults({key: value for key, value in card.items() if key != "signatures"})).encode()


def b64u(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64u_decode(text):
    if not isinstance(text, str) or not text.isascii():
        raise ValueError("Invalid base64url")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def public_jwk(public_key):
    numbers = public_key.public_numbers()
    members = {"crv": "P-256", "kty": "EC", "x": b64u(numbers.x.to_bytes(32, "big")), "y": b64u(numbers.y.to_bytes(32, "big"))}
    kid = b64u(hashlib.sha256(canonicalize(members).encode()).digest())
    return {**members, "alg": SIGNING_ALGORITHM, "kid": kid, "use": "sig"}


def sign_card(card, private_key, kid, jku):
    protected = b64u(json.dumps({"alg": SIGNING_ALGORITHM, "typ": "JOSE", "kid": kid, "jku": jku}, separators=(",", ":")).encode())
    signing_input = (protected + "." + b64u(signing_payload(card))).encode("ascii")
    # Deterministic ECDSA (RFC 6979): the same card always yields the same bytes, which keeps ETags stable.
    der = private_key.sign(signing_input, ec.ECDSA(hashes.SHA256(), deterministic_signing=True))
    r, s = decode_dss_signature(der)
    signature = b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return {**card, "signatures": [{"protected": protected, "signature": signature}]}


def _public_key_from_jwk(jwk):
    if not isinstance(jwk, dict) or jwk.get("kty") != "EC" or jwk.get("crv") != "P-256":
        raise ValueError("Unsupported key")
    x, y = b64u_decode(jwk["x"]), b64u_decode(jwk["y"])
    return ec.EllipticCurvePublicNumbers(int.from_bytes(x, "big"), int.from_bytes(y, "big"), ec.SECP256R1()).public_key()


def verify_card(card, jwks, trusted_jku):
    """True when a signature of `card` verifies against a key of `jwks` chosen by `kid` (A2A 8.4.3) *and* names a trusted `jku`.

    `trusted_jku` is the verifier's trust anchor: the exact URL(s) the JWKS may come from, normally the instance's own
    `<origin>/.well-known/jwks.json` (`instance_jku`) or an explicit allowlist the caller configured. A card is never trusted
    through the `jku` it carries by itself: a signature whose `jku` is absent or not in `trusted_jku` is ignored, so an
    attacker's own card, key and `jku` cannot pass. `jwks` must have been fetched from one of those URLs.

    Only ES256 is accepted; an unknown `kid`, a malformed signature or any change to the signed content gives False.
    """
    try:
        trusted = {trusted_jku} if isinstance(trusted_jku, str) else set(trusted_jku)
        if not trusted or not all(isinstance(url, str) and url for url in trusted):
            return False
        signatures = card.get("signatures")
        keys = {key.get("kid"): key for key in jwks["keys"] if isinstance(key, dict)}
        payload = b64u(signing_payload(card))
        for signature in signatures if isinstance(signatures, list) else []:
            try:
                header = json.loads(b64u_decode(signature["protected"]))
                if header.get("alg") != SIGNING_ALGORITHM or header.get("kid") not in keys or header.get("jku") not in trusted:
                    continue
                raw = b64u_decode(signature["signature"])
                if len(raw) != 64:
                    continue
                der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
                _public_key_from_jwk(keys[header["kid"]]).verify(
                    der, (signature["protected"] + "." + payload).encode("ascii"), ec.ECDSA(hashes.SHA256()))
                return True
            except Exception:
                continue
    except Exception:
        return False
    return False


def instance_jku():
    """The JWKS URL of this instance, the trust anchor for cards it signed."""
    return common.public_url() + JWKS_PATH


def verify_instance_card(card, jwks):
    return verify_card(card, jwks, instance_jku())


# --- instance signing key, stored only in the bridge state volume -----------------------------------------------------

class KeyUnavailable(Exception):
    """The signing key is missing or unusable; the card endpoints answer 503 instead of serving an unsigned card."""


def key_directory():
    return common.STATE / "a2a"


def _write_private(path, data):
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        os.write(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)


def init_keys(force=False):
    """Create the instance signing key (ES256) and return its public JWK. Refuses to replace a key unless `force`.

    A replaced key's public half stays in the published JWKS (retired list), so cards signed before the rotation
    keep verifying until they are refetched. The private key is written with mode 0600 and never returned.
    """
    directory = key_directory()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    path = directory / KEY_FILE
    retired = []
    if path.exists():
        if not force:
            raise FileExistsError("A signing key already exists; pass --force to replace it")
        old = serialization.load_pem_private_key(path.read_bytes(), password=None).public_key()
        retired = [*retired_keys(), public_jwk(old)]
    private_key = ec.generate_private_key(ec.SECP256R1())
    _write_private(path, private_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                    serialization.NoEncryption()))
    if retired:
        _write_private(directory / RETIRED_FILE, json.dumps(retired[-10:]).encode())
    return public_jwk(private_key.public_key())


def retired_keys():
    try:
        keys = json.loads((key_directory() / RETIRED_FILE).read_text())
        return [key for key in keys if isinstance(key, dict)] if isinstance(keys, list) else []
    except (OSError, ValueError):
        return []


def load_signing_key():
    path = key_directory() / KEY_FILE
    try:
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise KeyUnavailable("Signing key permissions are too open")
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    except KeyUnavailable:
        raise
    except (OSError, ValueError, TypeError):
        raise KeyUnavailable("Signing key is not initialized") from None
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        raise KeyUnavailable("Signing key has an unsupported type")
    return key, public_jwk(key.public_key())["kid"]


def jwks_document():
    key, _ = load_signing_key()
    return {"keys": [public_jwk(key.public_key()), *retired_keys()]}


# --- Agent Cards ------------------------------------------------------------------------------------------------------

def _skill(principal=None):
    target = "the named Baton agent" if principal is None else "the Baton agent " + principal
    return {
        "id": "submit-task",
        "name": "Submit a task to a named Baton agent",
        "description": ("Assign work to " + target + ". Send a message that names a work object in "
                        "message.metadata.baton: either {\"page_id\", \"agent\"} to create a task on that page assigned to the agent, "
                        "or {\"task_id\"} to add to an existing task's thread. Messages that name no work object are refused."),
        "tags": ["tasks", "assignment", "baton"],
        "examples": ["Prepare the release notes for the next version"],
        "inputModes": ["text/plain"],
        "outputModes": ["text/plain"],
    }


def _card(name, description, skill, tenant=None):
    interface = {"url": common.public_url() + RPC_PATH, "protocolBinding": "JSONRPC", "protocolVersion": A2A_VERSION}
    if tenant is not None:
        interface["tenant"] = tenant
    return {
        "name": name,
        "description": description,
        "supportedInterfaces": [interface],
        "version": INTERFACE_VERSION,
        "documentationUrl": "https://github.com/fledge-labs/baton/blob/main/docs/api.md",
        "capabilities": {
            "streaming": True,
            "pushNotifications": False,
            "extendedAgentCard": False,
            "extensions": [{
                "uri": EXTENSION_URI,
                "description": "Every message names a Baton work object (task, or page plus target agent) in message.metadata.baton; "
                               "task ids are Baton task ids and the task state is projected from Baton's own, see metadata.baton.state.",
                "required": False,
            }],
        },
        "securitySchemes": {"baton_bearer": {"httpAuthSecurityScheme": {
            "scheme": "Bearer", "description": "A Baton bearer token; the caller is the principal the token belongs to."}}},
        "securityRequirements": [{"schemes": {"baton_bearer": {"list": []}}}],
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [skill],
    }


def instance_card():
    return _card("Baton", "Baton connects people and agents through tasks and threads with permissions and audit. "
                 "This endpoint accepts A2A messages bound to a Baton work object and assigns tasks to named Baton agents.",
                 _skill())


def agent_card(principal):
    return _card(principal, "Baton agent " + principal + ". Work reaches it as a Baton task assigned to it; "
                 "it pulls its inbox, claims the task and reports in the task thread.", _skill(principal), tenant=principal)


# --- public (unauthenticated) discovery documents ---------------------------------------------------------------------

class RateLimit:
    """Token bucket per key; `allow` is cheap and thread-safe. Stale full buckets are pruned so memory stays bounded."""

    def __init__(self, rate, burst, clock=time.monotonic, max_keys=4096):
        self.rate, self.burst, self.clock, self.max_keys = rate, burst, clock, max_keys
        self.buckets = {}
        self.lock = threading.Lock()

    def allow(self, key=""):
        now = self.clock()
        with self.lock:
            tokens, stamp = self.buckets.get(key, (self.burst, now))
            tokens = min(self.burst, tokens + (now - stamp) * self.rate)
            allowed = tokens >= 1
            self.buckets[key] = (tokens - 1 if allowed else tokens, now)
            if len(self.buckets) > self.max_keys:
                horizon = self.burst / self.rate
                self.buckets = {k: v for k, v in self.buckets.items() if now - v[1] < horizon}
            return allowed


# Backstop for the unauthenticated documents (nginx limits per client address in front of it). Serving is a cache hit
# plus two stat calls, so the budget is generous; the point is to bound CPU and the event loop, not to meter clients.
PUBLIC_RATE, PUBLIC_BURST = 200.0, 400.0
_CACHE = {}
_CACHE_LOCK = threading.Lock()
_CACHE_LIMIT = 64


def _stat_signature(path):
    try:
        info = path.stat()
    except OSError:
        return None
    return (info.st_mtime_ns, info.st_size, info.st_ino, stat.S_IMODE(info.st_mode))


def key_version():
    """Fingerprint of the signing key, the retired-key list and the public origin; changes on rotation or reconfiguration.

    The key file's mode is checked here on every call, so a cached document is never served while the key is unusable.
    """
    directory = key_directory()
    key = _stat_signature(directory / KEY_FILE)
    if key is None:
        raise KeyUnavailable("Signing key is not initialized")
    if key[3] & 0o077:
        raise KeyUnavailable("Signing key permissions are too open")
    return (str(directory), key, _stat_signature(directory / RETIRED_FILE), common.public_url())


def cached(kind, build):
    """Build once per key version and content kind: signed cards and JWKS are deterministic, so they are reused."""
    version = key_version()
    with _CACHE_LOCK:
        hit = _CACHE.get((kind, version))
    if hit is not None:
        return hit
    value = build()
    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_LIMIT:
            _CACHE.clear()
        _CACHE[(kind, version)] = value
    return value


def sign(card):
    key, kid = load_signing_key()
    return sign_card(card, key, kid, instance_jku())


def signed_agent_card(principal):
    return cached(("agent", principal), lambda: sign(agent_card(principal)))


def _document(path):
    document = sign(instance_card()) if path == CARD_PATH else jwks_document()
    body = json.dumps(document, separators=(",", ":"), ensure_ascii=False).encode()
    return body, '"' + hashlib.sha256(body).hexdigest()[:32] + '"'


def _error_document(status, code, message, extra=None):
    body = json.dumps({"error": {"code": code, "message": message}}, separators=(",", ":")).encode()
    return body, {"Content-Type": "application/json", "Cache-Control": "no-store", **(extra or {})}, status


def public_document(path):
    """Body, headers and status of one public discovery document. The card carries no secret and needs no token."""
    try:
        body, etag = cached(("public", path), lambda: _document(path))
    except KeyUnavailable:
        return _error_document(503, "agent_card_key_unavailable", "Agent Card signing key is not initialized")
    except ValueError:
        return _error_document(500, "storage_unavailable", "Agent Card is unavailable")
    return body, {"Content-Type": "application/json", "Cache-Control": "public, max-age=%d" % CARD_MAX_AGE_SECONDS, "ETag": etag}, 200


class PublicDiscovery:
    """ASGI wrapper that answers the two public discovery paths before authentication; everything else passes through."""

    def __init__(self, app, limiter=None):
        self.app = app
        self.limiter = limiter or RateLimit(PUBLIC_RATE, PUBLIC_BURST)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("method") in {"GET", "HEAD"} and scope.get("path") in {CARD_PATH, JWKS_PATH}:
            if self.limiter.allow():
                body, headers, status = public_document(scope["path"])
            else:
                body, headers, status = _error_document(429, "rate_limited", "Too many requests", {"Retry-After": "1"})
            match = next((value.decode("latin-1") for key, value in scope.get("headers", []) if key.lower() == b"if-none-match"), None)
            if status == 200 and match is not None and headers["ETag"] in {part.strip() for part in match.split(",")}:
                status, body = 304, b""
            response = Response(b"" if scope["method"] == "HEAD" else body, status_code=status, headers=headers)
            return await response(scope, receive, send)
        await self.app(scope, receive, send)


# --- store access beyond SpaceStore's public methods ------------------------------------------------------------------

def now_text():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def as_timestamp(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return now_text()
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return now_text()


async def database(store, function, write=False):
    def run():
        with store._transaction(write=write) as conn:
            return function(conn)
    return await asyncio.to_thread(run)


def write_audit(conn, actor, event, operation_id, task_id=None, **metadata):
    """Append an x_audit row naming the real caller; space and task ids are resolved inside the same transaction."""
    space_id = None
    if task_id:
        row = conn.execute("SELECT p.space_id FROM baton.tasks t JOIN baton.pages p ON p.id=t.page_id WHERE t.id=%s", (task_id,)).fetchone()
        space_id = row["space_id"] if row else None
    conn.execute("""INSERT INTO baton.x_audit(id,event,agent,actor_account,actor_kind,requested_by,performed_by,operation_id,space_id,task_id,metadata)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                 (uuid.uuid4(), event, actor.principal, actor.account, actor.kind, actor.principal, actor.principal,
                  operation_id, space_id, task_id, Jsonb({"a2a_version": A2A_VERSION, **{k: v for k, v in metadata.items() if v is not None}})))


def read_overlay(conn, task_id):
    return conn.execute("SELECT state,set_by,created_at FROM baton.x_a2a_task_state WHERE task_id=%s", (task_id,)).fetchone()


def canceled_in(conn, task_id):
    """Whether the task was canceled, read inside the caller's transaction.

    The share lock on the task row conflicts with `FOR UPDATE` in `cancel_in_store`, so a cancellation can neither slip in
    between this check and the guarded mutation nor be missed by it: whichever transaction locks the row second waits for
    the first to commit and then sees its outcome.
    """
    conn.execute("SELECT 1 FROM baton.tasks WHERE id=%s FOR SHARE", (task_id,))
    return read_overlay(conn, task_id) is not None


class CancelAwareStore(SpaceStore):
    """A SpaceStore whose task mutations refuse a canceled task inside their own transaction (after authorization, so a
    task the caller cannot see is still reported as not found)."""

    def _mutate(self, actor, operation_id, method, args, authorize, effect, replay_guard=None):
        task_id = args.get("task_id") if method in CANCEL_GUARDED and isinstance(args, dict) else None
        if task_id:
            def guarded(conn):
                authorize(conn)
                if canceled_in(conn, task_id):
                    raise StoreError("task_canceled")
            return super()._mutate(actor, operation_id, method, args, guarded, effect, replay_guard)
        return super()._mutate(actor, operation_id, method, args, authorize, effect, replay_guard)


def _page_ids_of(conn, account):
    """Pages an account can view, computed set-wise with the same rules as `SpaceStore._account_role`."""
    if account == "baton-service":
        return set()
    rows = conn.execute("""WITH RECURSIVE chain(page_id, ancestor_id, parent_id) AS (
            SELECT id, id, parent_id FROM baton.pages
            UNION ALL SELECT c.page_id, p.id, p.parent_id FROM chain c JOIN baton.pages p ON p.id = c.parent_id)
        SELECT DISTINCT pg.id FROM baton.pages pg JOIN baton.spaces s ON s.id = pg.space_id
        WHERE s.owner_account = %(account)s OR EXISTS (
            SELECT 1 FROM baton.grants g WHERE
                ((g.resource_type = 'space' AND g.resource_id = s.id)
                 OR (g.resource_type = 'page' AND g.resource_id IN (SELECT ancestor_id FROM chain WHERE chain.page_id = pg.id)))
                AND ((g.subject_type = 'person' AND g.subject_id = %(account)s)
                     OR (g.subject_type = 'team' AND EXISTS (
                         SELECT 1 FROM baton.team_members m WHERE m.team_id::text = g.subject_id AND m.account = %(account)s))))""",
                        {"account": account}).fetchall()
    return {row["id"] for row in rows}


def registered_agents():
    """Active agent principals of the token registry; a principal with inconsistent records is left out, as for mentions."""
    try:
        records = json.loads((common.CONFIG / "identities.json").read_text())
        names = sorted({record["agent"] for record in records.values() if isinstance(record, dict) and record.get("active") is True})
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return []
    agents = []
    for name in names:
        who = common.resolve_principal(name)
        if who and who["kind"] == "agent":
            agents.append(who)
    return agents


def visible_agents(conn, actor):
    """Agent principals the caller may see: itself, and every agent that shares a page the caller can view."""
    mine = None
    visible = []
    for who in registered_agents():
        if who["agent"] == actor.principal and who["account"] == actor.account:
            visible.append(who["agent"])
            continue
        if mine is None:
            mine = _page_ids_of(conn, actor.account)
        if mine and mine & _page_ids_of(conn, who["account"]):
            visible.append(who["agent"])
    return visible


async def directory_response(ctx):
    """Signed cards of the agents the caller can see, or 503 until `init-keys` has been run."""
    names = await database(ctx.store, lambda conn: visible_agents(conn, ctx.actor))
    try:
        return JSONResponse({"agents": [signed_agent_card(name) for name in names]})
    except KeyUnavailable:
        return JSONResponse({"error": {"code": "agent_card_key_unavailable", "message": "Agent Card signing key is not initialized"}},
                            status_code=503)


# --- Baton task <-> A2A task ------------------------------------------------------------------------------------------

def baton_state(view, overlay=None):
    """The Baton superset state of a task view (`SpaceStore._task_view`), plus the A2A-only overlay when one exists."""
    if overlay:
        return overlay["state"]
    state = STORE_TO_BATON[view["state"]]
    if state == "submitted" and (view.get("blocked") or {}).get("reason_code") == "waiting_input":
        return "input_required"
    return state


def a2a_state(state):
    return BATON_TO_A2A[state]


def baton_from_a2a(state):
    """Inverse of the projection; TASK_STATE_UNSPECIFIED and unknown values have no Baton counterpart."""
    try:
        return A2A_TO_BATON[state]
    except (KeyError, TypeError):
        raise ValueError("Unknown A2A task state") from None


@dataclasses.dataclass
class Snapshot:
    view: dict
    comments: list
    overlay: dict | None

    @property
    def state(self):
        return baton_state(self.view, self.overlay)

    @property
    def cursor(self):
        """Position of the newest comment, so a stream can ask for what is newer."""
        if self.comments:
            return self.comments[-1]["created_at"], self.comments[-1]["id"]
        return EPOCH_CURSOR


def a2a_message(comment, caller, task):
    return {"messageId": comment["id"], "contextId": task["page_id"], "taskId": task["id"],
            "role": "ROLE_USER" if comment["author_principal"] == caller.principal else "ROLE_AGENT",
            "parts": [{"text": comment["body"], "mediaType": "text/plain"}],
            "metadata": {"baton": {"author": comment["author_principal"], "author_kind": comment["author_kind"],
                                   "subject": comment["subject"], "created_at": as_timestamp(comment["created_at"])}}}


def status_of(snapshot):
    return {"state": a2a_state(snapshot.state),
            "timestamp": as_timestamp(snapshot.overlay["created_at"]) if snapshot.overlay else now_text()}


def baton_metadata(snapshot):
    view = snapshot.view
    return {"baton": {"state": snapshot.state, "store_state": view["state"], "task_id": view["id"], "page_id": view["page_id"],
                      "owner": view["owner"], "assignee": view.get("assignee"), "priority": view.get("priority")}}


def a2a_task(snapshot, caller, history_length=None):
    view = snapshot.view
    task = {"id": view["id"], "contextId": view["page_id"], "status": status_of(snapshot), "metadata": baton_metadata(snapshot)}
    if snapshot.state == "completed" and view.get("result"):
        task["artifacts"] = [{"artifactId": "result", "name": "Result",
                              "parts": [{"text": view["result"], "mediaType": "text/plain"}],
                              "metadata": {"baton": {"result_author": view.get("result_author")}}}]
    history = [a2a_message(comment, caller, view) for comment in snapshot.comments]
    if history_length is not None:
        history = history[-history_length:] if history_length else []
    if history:
        task["history"] = history
    return task


COMMENT_COLUMNS = "id,page_id,thread_id,body,subject,author_principal,author_kind,created_at"


def read_snapshot(store, actor, task_id, limit=MAX_HISTORY, after=None):
    """Task view, overlay and thread comments in one transaction, checking the caller's view permission.

    Without `after`: the newest `limit` comments, oldest first. With `after` (a cursor from `Snapshot.cursor`): only the
    comments newer than it, so a stream never re-reads the thread. Never more than `limit` comments are read.
    """
    def read(conn):
        task = store._task_for(conn, actor, task_id)
        view = store._task_view(conn, actor, task_id)
        if after is None:
            rows = conn.execute(f"SELECT {COMMENT_COLUMNS} FROM baton.comments WHERE page_id=%s AND thread_id=%s "
                                "ORDER BY created_at DESC,id DESC LIMIT %s", (task["page_id"], task_id, limit)).fetchall()[::-1]
        else:
            rows = conn.execute(f"SELECT {COMMENT_COLUMNS} FROM baton.comments WHERE page_id=%s AND thread_id=%s "
                                "AND (created_at,id)>(%s::timestamptz,%s::uuid) ORDER BY created_at,id LIMIT %s",
                                (task["page_id"], task_id, after[0], after[1], limit)).fetchall()
        return {"view": view, "overlay": read_overlay(conn, task_id), "comments": rows}
    return store._read(actor, read)


async def snapshot(ctx, task_id, history_length=None, after=None):
    limit = MAX_HISTORY if history_length is None else min(history_length, MAX_HISTORY)
    found = await asyncio.to_thread(read_snapshot, ctx.store, ctx.actor, task_id, limit, after)
    result = Snapshot(found["view"], found["comments"], found["overlay"])
    if after is None:
        ctx.task_id = task_id
    return result


def history_length_param(params):
    value = params.get("historyLength")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise invalid_params("historyLength", "non-negative integer")
    return value


# --- JSON-RPC methods -------------------------------------------------------------------------------------------------

@dataclasses.dataclass
class Context:
    """One request. `task_id` is set once the caller is known to see the task (it then appears on the audit row);
    `audit` is the success row a handler asks for, `extra` metadata added to a failure row."""
    store: object
    call: object
    actor: Actor
    request_id: str | None = None
    waker: object = None
    method: str = "unknown"
    task_id: str | None = None
    audit: dict | None = None
    audited: bool = False
    extra: dict = dataclasses.field(default_factory=dict)

    def note(self, event, operation_id=None, **metadata):
        self.audit = {"event": event, "operation_id": operation_id, "metadata": {"method": self.method, **metadata}}

    async def pause(self, ticket, timeout):
        if self.waker is None:
            await asyncio.sleep(timeout)
        else:
            await self.waker.wait(ticket, timeout)


def task_id_param(params):
    value = params.get("id")
    try:
        return uuid_id(value)
    except (ValueError, TypeError, AttributeError):
        raise invalid_params("id", "task id: UUID string in canonical lowercase form") from None


def text_of(message):
    parts = message.get("parts")
    if not isinstance(parts, list) or not parts:
        raise invalid_params("message.parts", "non-empty array of parts")
    texts = []
    for part in parts:
        if not isinstance(part, dict):
            raise invalid_params("message.parts", "array of part objects")
        present = [key for key in ("text", "raw", "url", "data") if key in part]
        if len(present) != 1:
            raise invalid_params("message.parts", "each part carries exactly one of text, raw, url, data")
        if present[0] != "text":
            raise A2AError(CONTENT_TYPE_NOT_SUPPORTED, "Only text parts are supported", "CONTENT_TYPE_NOT_SUPPORTED")
        if not isinstance(part["text"], str):
            raise invalid_params("message.parts", "text parts carry a string")
        texts.append(part["text"])
    return "\n".join(texts)


def _text_field(value, field, expected):
    if value is not None and not isinstance(value, str):
        raise invalid_params(field, expected)
    return value


def work_object(params, message):
    """Resolve the Baton work object a message names, or None. Task ids from `taskId` and `metadata.baton.task_id` must agree.

    Every field is type-checked before it is compared, so a JSON object or array where a string belongs is `-32602`.
    """
    metadata = message.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise invalid_params("message.metadata", "object")
    baton = (metadata or {}).get("baton")
    if baton is not None and not isinstance(baton, dict):
        raise invalid_params("message.metadata.baton", "object")
    baton = baton or {}
    if set(baton) - WORK_OBJECT_KEYS:
        raise invalid_params("message.metadata.baton", "only task_id, page_id, agent and scopes")
    baton_task = _text_field(baton.get("task_id"), "message.metadata.baton.task_id", "task id string")
    native_task = _text_field(message.get("taskId"), "message.taskId", "task id string")
    page = _text_field(baton.get("page_id"), "message.metadata.baton.page_id", "page id string")
    agent = _text_field(baton.get("agent"), "message.metadata.baton.agent", "agent principal string")
    tenant = _text_field(params.get("tenant"), "tenant", "agent principal string")
    context_id = _text_field(message.get("contextId"), "message.contextId", "string")
    scopes = baton.get("scopes")
    if scopes is not None and not isinstance(scopes, list):
        raise invalid_params("message.metadata.baton.scopes", "array of scope objects")
    task_ids = {value for value in (baton_task, native_task) if value is not None}
    if len(task_ids) > 1:
        raise invalid_params("message.taskId", "equal to message.metadata.baton.task_id")
    if task_ids:
        try:
            return {"task_id": uuid_id(next(iter(task_ids))), "context_id": context_id}
        except (ValueError, TypeError, AttributeError):
            raise invalid_params("message.metadata.baton.task_id", "task id: UUID string in canonical lowercase form") from None
    if page is None:
        return None
    try:
        page_id = uuid_id(page)
    except (ValueError, TypeError, AttributeError):
        raise invalid_params("message.metadata.baton.page_id", "UUID string in canonical lowercase form") from None
    agents = {value for value in (agent, tenant) if value is not None}
    if len(agents) != 1:
        raise invalid_params("message.metadata.baton.agent", "the target agent principal (equal to tenant when both are given)")
    if context_id is not None and context_id != page_id:
        raise invalid_params("message.contextId", "equal to the page id")
    return {"page_id": page_id, "agent": next(iter(agents)), "scopes": scopes}


def unavailable():
    """The one answer for every way a page-and-agent target can fail: the caller cannot edit the page, the page does not
    exist, the agent is unknown, not an agent, the caller itself, or cannot edit the page. Nothing tells them apart."""
    return A2AError(INVALID_PARAMS, "The work object is not available for this assignment", "WORK_OBJECT_UNAVAILABLE", violations=[{
        "field": "message.metadata.baton", "description": "a page you can edit and a registered agent that can edit it too"}])


def target_agent(agent, caller):
    who = common.resolve_principal(agent)
    if not who or who["kind"] != "agent" or who["agent"] == caller.principal:
        raise unavailable()
    return Actor(safe_id(who["agent"]), safe_id(who["account"]), who["kind"])


def parse_send(params):
    if not isinstance(params, dict) or not isinstance(params.get("message"), dict):
        raise invalid_params("message", "message object")
    message = params["message"]
    message_id = message.get("messageId")
    if not isinstance(message_id, str) or not 0 < len(message_id) <= MAX_MESSAGE_ID:
        raise invalid_params("message.messageId", "non-empty string of at most %d characters" % MAX_MESSAGE_ID)
    if message.get("role") != "ROLE_USER":
        raise invalid_params("message.role", "ROLE_USER")
    configuration = params.get("configuration")
    if configuration is not None and not isinstance(configuration, dict):
        raise invalid_params("configuration", "object")
    if configuration and configuration.get("taskPushNotificationConfig") is not None:
        raise A2AError(PUSH_NOT_SUPPORTED, "Push notifications are not supported", "PUSH_NOTIFICATION_NOT_SUPPORTED")
    return message, message_id, text_of(message), history_length_param(configuration or {})


async def send(ctx, params, event):
    """Shared by SendMessage and SendStreamingMessage: create or extend a task, request its audit row, return the snapshot."""
    message, message_id, text, history = parse_send(params)
    work = work_object(params, message)
    input_hash = ctx.extra["input_sha256"] = hashlib.sha256(text.encode()).hexdigest()
    if work is None:
        raise A2AError(INVALID_PARAMS, "A message must name a Baton work object", "WORK_OBJECT_REQUIRED", violations=[{
            "field": "message.metadata.baton", "description": "task_id, or page_id together with agent"}])
    stamp = str(uuid.uuid5(uuid.NAMESPACE_URL, "baton/a2a/" + ctx.actor.principal + "/" + message_id))
    if "task_id" in work:
        task_id = work["task_id"]
        before = await snapshot(ctx, task_id, history_length=0)
        if work["context_id"] is not None and work["context_id"] != before.view["page_id"]:
            raise invalid_params("message.contextId", "the page id of the task")
        if before.state in TERMINAL:
            raise A2AError(UNSUPPORTED_OPERATION, "The task is in a terminal state", "TASK_TERMINAL")
        comment = await ctx.call("comment", ctx.actor, stamp, before.view["page_id"], text, thread_id=task_id)
        outcome = "replayed" if comment.get("replay") else "comment_appended"
    else:
        # Permission first, then the agent: the caller learns nothing about pages or agents it cannot use.
        try:
            role = (await ctx.call("effective_role", ctx.actor, "page", work["page_id"]))["role"]
        except Exception as exc:
            if getattr(exc, "code", None) == "not_found":
                raise unavailable() from None
            raise
        if role != "edit":
            raise unavailable()
        target = target_agent(work["agent"], ctx.actor)
        try:
            created = await ctx.call("submit_task", ctx.actor, stamp, "a2a-" + hashlib.sha256(message_id.encode()).hexdigest()[:40],
                                     text, page_id=work["page_id"], scopes=work["scopes"], assignee=target)
        except Exception as exc:
            if getattr(exc, "code", None) in {"not_found", "forbidden"}:
                raise unavailable() from None
            raise
        task_id = created["task_id"]
        ctx.task_id = task_id
        outcome = "replayed" if created.get("replay") else "task_created"
        if not created.get("replay"):
            common.audit("task.submitted", task_id=task_id, agent=ctx.actor.principal, request_id=ctx.request_id, input_sha256=input_hash)
            common.audit("thread.submitted", task_id=task_id, thread_id=created.get("thread_id"), agent=ctx.actor.principal)
    after = await snapshot(ctx, task_id)
    ctx.note(event, stamp, outcome=outcome, input_sha256=input_hash, a2a_state=a2a_state(after.state))
    return after, history


async def send_message(ctx, params, rpc_id):
    after, history = await send(ctx, params, "a2a.message_sent")
    return {"task": a2a_task(after, ctx.actor, history)}


def sse(rpc_id, result):
    return "data: " + json.dumps({"jsonrpc": "2.0", "id": rpc_id, "result": result}, separators=(",", ":")) + "\n\n"


class StreamLimiter:
    """Open-stream budget per actor and overall. A lease expires on its own, so a stream whose client vanished before the
    first byte (and therefore never ran its `finally`) cannot hold a slot forever."""

    def __init__(self, clock=time.monotonic):
        self.clock, self.leases, self.lock, self.serial = clock, {}, threading.Lock(), 0

    def acquire(self, key, lifetime):
        now = self.clock()
        with self.lock:
            self.leases = {k: {t: e for t, e in held.items() if e > now} for k, held in self.leases.items()}
            self.leases = {k: held for k, held in self.leases.items() if held}
            held = self.leases.setdefault(key, {})
            if len(held) >= MAX_STREAMS_PER_ACTOR or sum(len(item) for item in self.leases.values()) >= MAX_STREAMS_TOTAL:
                if not held:
                    del self.leases[key]
                return None
            self.serial += 1
            held[self.serial] = now + lifetime
            return self.serial

    def release(self, key, token):
        with self.lock:
            held = self.leases.get(key, {})
            held.pop(token, None)
            if not held:
                self.leases.pop(key, None)


STREAMS = StreamLimiter()


async def stream_events(ctx, rpc_id, first, lease):
    """Task, then comment and state updates for the task; ends on a terminal state, on entering an interrupted one, or
    after STREAM_MAX_SECONDS (the client can SubscribeToTask again).

    Each poll is one transaction that re-checks the caller's view permission and reads only comments newer than the last
    one sent. Polls are woken early by commits in this process and back off while the task is quiet.
    """
    key = ctx.actor.principal + "/" + ctx.actor.account
    try:
        task_id = first.view["id"]
        yield sse(rpc_id, {"task": a2a_task(first, ctx.actor)})
        state, cursor = first.state, first.cursor
        if state in TERMINAL:
            return
        loop = asyncio.get_running_loop()
        quiet_since = loop.time()
        deadline = quiet_since + STREAM_MAX_SECONDS
        interval = STREAM_POLL_SECONDS
        while loop.time() < deadline:
            ticket = ctx.waker.ticket() if ctx.waker is not None else None
            try:
                current = await snapshot(ctx, task_id, after=cursor)
            except Exception as exc:
                yield "data: " + json.dumps(translate(exc).body(rpc_id), separators=(",", ":")) + "\n\n"
                return
            sent = False
            for comment in current.comments:
                sent = True
                yield sse(rpc_id, {"message": a2a_message(comment, ctx.actor, current.view)})
            if current.comments:
                cursor = current.cursor
            if current.state != state:
                sent = True
                yield sse(rpc_id, {"statusUpdate": {"taskId": task_id, "contextId": current.view["page_id"], "status": status_of(current),
                                                    "metadata": baton_metadata(current)}})
                interrupted_now = current.state in INTERRUPTED and state not in INTERRUPTED
                state = current.state
                if state in TERMINAL or interrupted_now:
                    return
            if sent:
                quiet_since, interval = loop.time(), STREAM_POLL_SECONDS
                if len(current.comments) >= MAX_HISTORY:
                    continue
            else:
                interval = min(STREAM_MAX_POLL_SECONDS, interval * 2)
                if loop.time() - quiet_since >= STREAM_KEEPALIVE_SECONDS:
                    quiet_since = loop.time()
                    yield ": keepalive\n\n"
            await ctx.pause(ticket, max(0.0, min(interval, deadline - loop.time())))
    finally:
        STREAMS.release(key, lease)


def event_stream(ctx, rpc_id, first):
    key = ctx.actor.principal + "/" + ctx.actor.account
    lease = STREAMS.acquire(key, STREAM_MAX_SECONDS + 30)
    if lease is None:
        raise A2AError(SERVER_ERROR, "Too many open streams", "STREAM_LIMIT_EXCEEDED")
    return StreamingResponse(stream_events(ctx, rpc_id, first, lease), media_type="text/event-stream",
                             headers={"X-Accel-Buffering": "no"})


async def send_streaming_message(ctx, params, rpc_id):
    after, _ = await send(ctx, params, "a2a.message_streamed")
    return event_stream(ctx, rpc_id, after)


async def subscribe_to_task(ctx, params, rpc_id):
    current = await snapshot(ctx, task_id_param(params))
    if current.state in TERMINAL:
        raise A2AError(UNSUPPORTED_OPERATION, "The task is in a terminal state", "TASK_TERMINAL")
    ctx.note("a2a.task_streamed", a2a_state=a2a_state(current.state))
    return event_stream(ctx, rpc_id, current)


async def get_task(ctx, params, rpc_id):
    task_id = task_id_param(params)
    history = history_length_param(params)
    current = await snapshot(ctx, task_id, history_length=history)
    ctx.note("a2a.task_read", a2a_state=a2a_state(current.state))
    return a2a_task(current, ctx.actor, history)


def not_cancelable():
    return A2AError(TASK_NOT_CANCELABLE, "The task is not in a cancelable state", "TASK_NOT_CANCELABLE")


def cancel_in_store(conn, actor, task_id, assignee, request_id=None):
    """Record the cancellation, release the claim's scope reservation and audit it, in one transaction.

    The task row is locked `FOR UPDATE` first and the state is checked under that lock, so the check and the cancellation
    are atomic with respect to the guarded mutations (`canceled_in` takes the matching share lock).
    """
    row = conn.execute("SELECT state,execution_mode,assignee FROM baton.tasks WHERE id=%s FOR UPDATE", (task_id,)).fetchone()
    # Only tasks assigned to an agent (the manual path, which A2A creates) can be canceled.
    if (not row or row["state"] in {"done", "failed", "interrupted"} or row["execution_mode"] != "manual"
            or row["assignee"] is None or row["assignee"] != assignee):
        return False
    saved = conn.execute("""INSERT INTO baton.x_a2a_task_state(task_id,state,set_by,set_by_account,set_by_kind) VALUES (%s,'canceled',%s,%s,%s)
        ON CONFLICT (task_id) DO NOTHING RETURNING task_id""", (task_id, actor.principal, actor.account, actor.kind)).fetchone()
    if not saved:
        return False
    conn.execute("UPDATE baton.x_claim SET lease_expires_at=clock_timestamp() WHERE task_id=%s AND lease_expires_at>clock_timestamp()", (task_id,))
    write_audit(conn, actor, "a2a.task_canceled", uuid.uuid4(), task_id, method="CancelTask", outcome="canceled",
                a2a_state="canceled", request_id=request_id)
    return True


def assigned_to_agent(assignee):
    who = common.resolve_principal(assignee) if isinstance(assignee, str) else None
    return bool(who and who["kind"] == "agent")


async def cancel_task(ctx, params, rpc_id):
    task_id = task_id_param(params)
    current = await snapshot(ctx, task_id, history_length=0)
    view = current.view
    if current.state in TERMINAL or not assigned_to_agent(view.get("assignee")):
        raise not_cancelable()
    owner = view["owner"] == ctx.actor.principal and view["owner_account"] == ctx.actor.account
    role = (await ctx.call("effective_role", ctx.actor, "page", view["page_id"]))["role"]
    if role != "edit" or not (owner or ctx.actor.principal in (view.get("reviewers") or [])):
        raise A2AError(SERVER_ERROR, "Operation is not permitted", "PERMISSION_DENIED")
    if not await database(ctx.store, lambda conn: cancel_in_store(conn, ctx.actor, task_id, view["assignee"], ctx.request_id), write=True):
        raise not_cancelable()
    ctx.audited = True
    if ctx.waker is not None:
        ctx.waker.notify()
    try:
        await ctx.call("comment", ctx.actor, str(uuid.uuid4()), view["page_id"], "[canceled] Canceled through A2A CancelTask", thread_id=task_id)
    except Exception:
        # The cancellation is already recorded and audited; the thread note only informs the assignee sooner.
        pass
    return a2a_task(await snapshot(ctx, task_id), ctx.actor)


METHODS = {"SendMessage": send_message, "SendStreamingMessage": send_streaming_message, "GetTask": get_task,
           "CancelTask": cancel_task, "SubscribeToTask": subscribe_to_task}
KNOWN_METHODS = frozenset(METHODS) | PUSH_METHODS | frozenset(V03_METHODS) | {"ListTasks", "GetExtendedAgentCard"}


async def audit_call(ctx, error=None):
    """The audit row of one call: its success row (unless the handler already wrote it atomically) or an `a2a.refused`
    row with the reason. A refusal row never hides the error it reports: if it cannot be written the error still stands."""
    if error is None and ctx.audited:
        return
    if error is None and ctx.audit is None:
        return
    if error is None:
        event, operation_id, metadata = ctx.audit["event"], ctx.audit["operation_id"], ctx.audit["metadata"]
    else:
        event, operation_id = "a2a.refused", None
        metadata = {"method": ctx.method, "reason": error.reason or "ERROR", "error_code": error.code, **ctx.extra}
    try:
        await database(ctx.store, lambda conn: write_audit(conn, ctx.actor, event, operation_id or uuid.uuid4(), ctx.task_id,
                                                           request_id=ctx.request_id, **metadata), write=True)
    except Exception:
        if error is None:
            raise


async def handle_rpc(request, *, store, call, actor, waker=None):
    """One JSON-RPC 2.0 request from an authenticated Baton principal. Returns a JSON or an SSE response.

    Every call ends in exactly one audit row naming the caller: the success row a handler asked for, or an `a2a.refused`
    row for any error, including malformed requests, version and method refusals, and stream or permission failures.
    """
    try:
        request_id = uuid_id(request.headers.get("x-request-id"))
    except (ValueError, TypeError, AttributeError):
        request_id = None
    ctx = Context(store, call, actor, request_id, waker)
    rpc_id = None
    try:
        raw = await request.body()
        if len(raw) > MAX_BODY_BYTES:
            raise A2AError(INVALID_REQUEST, "Request payload validation error")
        try:
            payload = json.loads(raw)
        except (ValueError, RecursionError):
            raise A2AError(PARSE_ERROR, "Invalid JSON payload") from None
        rpc_id = payload.get("id") if isinstance(payload, dict) else None
        if (not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0" or not isinstance(payload.get("method"), str)
                or isinstance(rpc_id, (bool, float, list, dict)) or not isinstance(payload.get("params", {}), dict)):
            raise A2AError(INVALID_REQUEST, "Request payload validation error")
        method, params = payload["method"], payload.get("params", {})
        ctx.method = method if method in KNOWN_METHODS else "unknown"
        version = (request.headers.get("a2a-version") or "").strip()
        if version and ".".join(version.split(".")[:2]) != A2A_VERSION:
            raise A2AError(VERSION_NOT_SUPPORTED, "A2A version not supported; this endpoint speaks A2A v1.0", "VERSION_NOT_SUPPORTED",
                           {"supported": A2A_VERSION})
        if method in V03_METHODS:
            raise A2AError(VERSION_NOT_SUPPORTED, "A2A v0.3 method names are not supported; use " + V03_METHODS[method] + " (A2A v1.0)",
                           "VERSION_NOT_SUPPORTED", {"supported": A2A_VERSION, "replacement": V03_METHODS[method]})
        if method in PUSH_METHODS:
            raise A2AError(PUSH_NOT_SUPPORTED, "Push notifications are not supported", "PUSH_NOTIFICATION_NOT_SUPPORTED")
        if method in {"ListTasks", "GetExtendedAgentCard"}:
            raise A2AError(UNSUPPORTED_OPERATION, "Operation is not supported", "UNSUPPORTED_OPERATION")
        handler = METHODS.get(method)
        if handler is None:
            raise A2AError(METHOD_NOT_FOUND, "Method not found")
        result = await handler(ctx, params, rpc_id)
        await audit_call(ctx)
    except Exception as exc:
        error = translate(exc)
        await audit_call(ctx, error)
        return JSONResponse(error.body(rpc_id))
    if isinstance(result, StreamingResponse):
        return result
    return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "result": result})


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m bridge.a2a_public", description="Baton A2A public interface administration")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init-keys", help="create the instance Agent Card signing key in the state volume")
    init.add_argument("--force", action="store_true", help="replace an existing key; its public half stays published as retired")
    arguments = parser.parse_args(argv)
    try:
        jwk = init_keys(force=arguments.force)
    except FileExistsError as exc:
        print("error: " + str(exc), file=sys.stderr)
        return 1
    except OSError:
        print("error: the signing key could not be written to the state volume", file=sys.stderr)
        return 1
    print(json.dumps({"kid": jwk["kid"], "jwk": jwk, "directory": str(key_directory())}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
