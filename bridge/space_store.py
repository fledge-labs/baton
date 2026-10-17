# SPDX-License-Identifier: Apache-2.0
"""Baton Space v1: PostgreSQL authority, authenticated actors and fenced effects.

The schema is installed by a separate migration owner. Runtime connections never
run DDL. Baton never executes tasks: Space content, queue, claims, notifications
and audit live here, and execution belongs to external agents that claim tasks.

Rows written by the removed built-in worker stay readable: ``execution_mode='codex'``,
``gate_mode='worker_success'`` and the ``a2a_id``/``backend``/``dispatch_started_at``
columns are legacy values that nothing writes any more.
"""

import hashlib
import json
import os
import re
import secrets
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from bridge import webhooks

LOCK_ID = 0x4241544F4E
ROLES = {None: 0, "view": 1, "comment": 2, "edit": 3}
MESSAGES = {
    "invalid": "Invalid request", "not_found": "Resource not found", "forbidden": "Operation not permitted",
    "operation_conflict": "Operation identifier was used for different input", "scope_conflict": "Scope is already claimed",
    "stale_claim": "Claim is expired or superseded", "revision_conflict": "Revision has changed",
    "gate_required": "A valid gate decision is required",
    "delegation_required": "An active approver delegation covering the task is required",
    "storage_unavailable": "Space storage is unavailable",
    "dependency_blocked": "Task dependencies are not satisfied", "graph_cycle": "Task graph would contain a cycle",
    "work_face_incomplete": "Task work face is incomplete",
    "rate_limited": "Too many requests",
}
NOTIFY_EVENTS = frozenset({"mention.created", "task.assigned", "comment.created", "task.awaiting_gate",
                           "gate.decided", "task.handed_off", "task.failed", "task.overdue", "task.blocked",
                           "claim.expiring"})
PREVIEW_CHARS = 200
SUBJECT_TITLE_CHARS = 60
EVENT_LABELS = {"task.assigned": "assigned", "task.handed_off": "handed_off", "task.awaiting_gate": "awaiting_gate",
                "gate.decided": "gate_decided", "task.failed": "failed", "task.overdue": "overdue", "task.blocked": "blocked",
                "claim.expiring": "expiring"}
# One claim.expiring per lease grant, once the remaining time is under this fraction of the lease and at least this many seconds.
CLAIM_EXPIRING_FRACTION = 0.2
CLAIM_EXPIRING_MIN_SECONDS = 60
BLOCK_REASONS = ("scope_conflict", "lease_held", "dependency_blocked", "waiting_input", "other")
WEBHOOK_LIMIT = 10
WEBHOOK_MAX_ATTEMPTS = 8
# Delay after failed attempt n (1-based); the seven delays between eight attempts total about an hour.
WEBHOOK_BACKOFF_SECONDS = (30, 60, 120, 240, 480, 960, 1920)
WEBHOOK_LEASE_SECONDS = 60
WEBHOOK_ROTATION_GRACE_ENV = "BATON_WEBHOOK_ROTATION_GRACE_SECONDS"
WEBHOOK_ROTATION_GRACE_SECONDS = 24 * 3600
WEBHOOK_ROTATION_GRACE_MAX_SECONDS = 30 * 24 * 3600
# Expired previous secrets are already ignored when signing; clearing them is only housekeeping, so it runs this rarely.
WEBHOOK_SECRET_SWEEP_SECONDS = 60
NOTIFY_SCAN_BATCH = 200
NOTIFY_SCAN_LIMIT = 1000
PRIORITIES = ("urgent", "normal", "low")
DEFAULT_RESPONSE_MINUTES = {"urgent": 20, "normal": 60, "low": 1440}
RESPONSE_MINUTES_MAX = 525600
# Distinct from LOCK_ID: this one only keeps escalation runners from queueing up behind each other.
ESCALATION_LOCK_ID = 0x4241544F4F
ESCALATION_BATCH = 100


class StoreError(Exception):
    def __init__(self, code, field=None, expected=None):
        self.code = code
        # Static, value-free description of what was wrong; the rejected input itself is never kept.
        self.details = [{"field": field, "expected": expected}] if code == "invalid" and field and expected else []
        super().__init__(MESSAGES.get(code, MESSAGES["storage_unavailable"]))


class _Busy(Exception):
    """Another process holds a single-flight lock; the caller skips this round."""


def parse_response_minutes(value):
    """Merge a "urgent=20,normal=60,low=1440" override into the defaults. Unset or blank keeps the defaults."""
    policy = dict(DEFAULT_RESPONSE_MINUTES)
    if value is None or not value.strip():
        return policy
    seen = set()
    for item in value.split(","):
        name, separator, minutes = (part.strip() for part in item.partition("="))
        if not separator or name not in PRIORITIES or name in seen or not minutes.isascii() or not minutes.isdecimal():
            raise ValueError("Invalid response-time policy")
        if not 1 <= int(minutes) <= RESPONSE_MINUTES_MAX:
            raise ValueError("Invalid response-time policy")
        seen.add(name)
        policy[name] = int(minutes)
    return policy


def parse_rotation_grace(value):
    """Seconds the replaced webhook secret keeps signing. Unset or blank is 24 h; anything else must be a whole number
    of seconds from 0 to 30 days, otherwise ValueError so the bridge refuses to start instead of guessing."""
    if value is None or not value.strip():
        return WEBHOOK_ROTATION_GRACE_SECONDS
    text = value.strip()
    if not text.isascii() or not text.isdecimal() or int(text) > WEBHOOK_ROTATION_GRACE_MAX_SECONDS:
        raise ValueError("Invalid webhook rotation grace period")
    return int(text)


def _identifier(value, field=None):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", value):
        raise StoreError("invalid", field, "string of 1-96 letters, digits, '_', '.' or '-', starting with a letter or digit")
    return value


def _uuid(value, field=None):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value.lower():
            raise ValueError()
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise StoreError("invalid", field, "UUID string in canonical lowercase form") from None


@dataclass(frozen=True)
class Actor:
    principal: str
    account: str
    kind: str

    def __post_init__(self):
        _identifier(self.principal)
        _identifier(self.account)
        if not isinstance(self.kind, str) or self.kind not in {"human", "agent"}:
            raise StoreError("invalid")


SERVICE_ACTOR = Actor("baton-service", "baton-service", "agent")
# The removed worker's service principal. It still appears in old rows, so it stays reserved.
LEGACY_SERVICE_PRINCIPAL = "codex-worker"


def _json(value):
    if isinstance(value, (datetime, uuid.UUID)):
        return value.isoformat() if isinstance(value, datetime) else str(value)
    if isinstance(value, dict):
        return {key: _json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(item) for item in value]
    return value


def _canonical(value):
    try:
        return json.dumps(_json(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError):
        raise StoreError("invalid") from None


def _payload(value, field=None):
    if len(_canonical(value)) > 262144:
        raise StoreError("invalid", field, "JSON value of at most 262144 bytes")
    return value


def _sha256(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _text(value, maximum=16000, field=None):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\x00" in value:
        raise StoreError("invalid", field, f"non-blank string of at most {maximum} characters without NUL")
    return value


def _integer(value, minimum=1, maximum=3600, field=None):
    if type(value) is not int or not minimum <= value <= maximum:
        raise StoreError("invalid", field, f"integer between {minimum} and {maximum}")
    return value


def _items(value):
    return [] if value is None else _payload(value if isinstance(value, list) else [value])


SCOPES_EXPECTED = ("array of at most 100 scope objects: {kind: path|module|migration, value: string} "
                   "or {kind: none}")


def normalize_scopes(scopes):
    """Normalize declarations: repo-relative `/` paths keep their case; module and migration identifiers are case-folded."""
    if scopes is None or scopes == []:
        return [{"kind": "path", "value": ""}]
    if not isinstance(scopes, list) or len(scopes) > 100:
        raise StoreError("invalid", "scopes", SCOPES_EXPECTED)
    result = []
    for scope in scopes:
        kind = scope.get("kind") if isinstance(scope, dict) else None
        if not isinstance(kind, str) or kind not in {"path", "module", "migration", "none"}:
            raise StoreError("invalid", "scopes", SCOPES_EXPECTED)
        if scope["kind"] == "none":
            if set(scope) != {"kind"}:
                raise StoreError("invalid", "scopes", SCOPES_EXPECTED)
            item = {"kind": "none"}
            if item not in result:
                result.append(item)
            continue
        if set(scope) != {"kind", "value"}:
            raise StoreError("invalid", "scopes", SCOPES_EXPECTED)
        value = scope["value"]
        if not isinstance(value, str) or len(value) > 240:
            raise StoreError("invalid", "scopes", SCOPES_EXPECTED)
        if value.startswith("/") or not re.fullmatch(r"[A-Za-z0-9_./-]*", value) or ".." in value.split("/"):
            raise StoreError("invalid", "scopes", SCOPES_EXPECTED)
        if kind == "path":
            value = "/".join(part for part in value.split("/") if part not in {"", "."})
        else:
            value = value.casefold()
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", value):
                raise StoreError("invalid", "scopes", SCOPES_EXPECTED)
            if "/" in value or value in {".", ".."}:
                raise StoreError("invalid", "scopes", SCOPES_EXPECTED)
            if kind == "migration" and value.isdecimal():
                value = str(int(value))
        item = {"kind": kind, "value": value}
        if item not in result:
            result.append(item)
    if any(item["kind"] == "none" for item in result) and len(result) > 1:
        raise StoreError("invalid", "scopes", SCOPES_EXPECTED)
    return sorted(result, key=lambda item: (item["kind"], item.get("value", "")))


def _stored_scopes(requested, normalized):
    """Scopes persisted for a task.

    Omitted scopes (None) mean "no repository footprint": the explicit "none" scope. The
    request digest still hashes the legacy placeholder for omitted scopes, so
    requests persisted before the "none" kind existed replay unchanged.
    """
    if requested is None:
        return [{"kind": "none"}]
    return normalized


SCOPE_CASE_INSENSITIVE_ENV = "BATON_SCOPE_CASE_INSENSITIVE"


def _path_parts(value, fold):
    value = value.casefold() if fold else value
    return tuple(part for part in value.split("/") if part not in {"", "."})


def paths_overlap(left, right, case_insensitive=False):
    """True when one normalized path equals the other or is a directory prefix of it on a `/` boundary.

    The empty path is the repository root and overlaps every path. Symlinks, hard links and other aliases are not
    resolved: two declarations that reach the same files through different paths do not overlap.
    """
    a, b = _path_parts(left, case_insensitive), _path_parts(right, case_insensitive)
    shorter = min(len(a), len(b))
    return a[:shorter] == b[:shorter]


def scope_case_insensitive():
    return os.environ.get(SCOPE_CASE_INSENSITIVE_ENV, "").strip() == "1"


def _request_scopes(normalized):
    """Scopes as hashed into request digests: path case is folded, as it was before paths kept their case.

    This keeps the digest of an already-persisted request stable across the upgrade, so a retry of an old
    operation or idempotency key still replays instead of reporting operation_conflict.
    """
    folded = []
    for item in normalized:
        item = {**item, "value": item["value"].casefold()} if item["kind"] == "path" else item
        if item not in folded:
            folded.append(item)
    return sorted(folded, key=lambda item: (item["kind"], item.get("value", "")))


def scopes_overlap(left, right):
    fold = scope_case_insensitive()
    for a in left:
        if a["kind"] == "none":
            continue
        for b in right:
            if b["kind"] == "none":
                continue
            if "module" in {a["kind"], b["kind"]}:
                return True  # No administrator module map exists: a module covers the whole repository.
            if a["kind"] != b["kind"]:
                continue
            if a["kind"] == "path":
                if paths_overlap(a["value"], b["value"], fold):
                    return True
            elif a["value"] == b["value"]:
                return True
    return False


WORK_FACE_LISTS = ("environment", "fixtures", "constraints")
WORK_FACE_KEYS = frozenset((*WORK_FACE_LISTS, "migration", "acceptance_command"))
TASK_KINDS = frozenset({"construction", "prep"})
ROOT_SCOPES = [{"kind": "path", "value": ""}]
NONE_SCOPES = [{"kind": "none"}]
EXECUTION_FIELDS = ("dispatch_started_at", "result_epoch", "result", "result_author", "result_author_account", "a2a_id", "backend")


def _trimmed(value, maximum, field="work_face"):
    expected = f"non-blank UTF-8 string of at most {maximum} characters without NUL"
    if not isinstance(value, str) or "\x00" in value:
        raise StoreError("invalid", field, expected)
    try:
        value.encode()
    except UnicodeEncodeError:
        raise StoreError("invalid", field, expected) from None
    value = value.strip()
    if not value or len(value) > maximum:
        raise StoreError("invalid", field, expected)
    return value


def normalize_work_face(value):
    """Validate a work-face checklist and return its trimmed canonical form.

    Baton stores the acceptance command as reviewer-facing text. It never runs it.
    """
    if not isinstance(value, dict):
        raise StoreError("invalid", "work_face", "object with keys " + ", ".join(sorted(WORK_FACE_KEYS)))
    # Only the names a client is missing are reported; unexpected key names are never echoed.
    missing = sorted(WORK_FACE_KEYS - set(value))
    if missing:
        raise StoreError("invalid", "work_face." + missing[0], "required key of work_face")
    if set(value) != WORK_FACE_KEYS:
        raise StoreError("invalid", "work_face", "object with exactly the keys " + ", ".join(sorted(WORK_FACE_KEYS)))
    result = {}
    for key in WORK_FACE_LISTS:
        items = value[key]
        if not isinstance(items, list) or len(items) > 20:
            raise StoreError("invalid", "work_face." + key, "array of at most 20 non-blank strings")
        result[key] = [_trimmed(item, 500, "work_face." + key) for item in items]
    result["migration"] = None if value["migration"] is None else _trimmed(value["migration"], 200, "work_face.migration")
    result["acceptance_command"] = _trimmed(value["acceptance_command"], 2000, "work_face.acceptance_command")
    return result


def _task_kind(value):
    if not isinstance(value, str) or value not in TASK_KINDS:
        raise StoreError("invalid", "task_kind", "one of " + ", ".join(sorted(TASK_KINDS)))
    return value


def _work_face_request(work_face, require_work_face, task_kind):
    if type(require_work_face) is not bool:
        raise StoreError("invalid", "require_work_face", "boolean")
    face = None if work_face is None else normalize_work_face(work_face)
    return face, require_work_face, _task_kind(task_kind)


def _approvals_request(value):
    """The task-level approvals override: 1 or 2."""
    if value is None:
        return None
    if type(value) is not int or value not in (1, 2):
        raise StoreError("invalid", "approvals_required", "1 or 2")
    return value


def _priority(value):
    if not isinstance(value, str) or value not in PRIORITIES:
        raise StoreError("invalid", "priority", "one of " + ", ".join(PRIORITIES))
    return value


def _response_request(priority, escalate_to, fallback_assignee, owner, assignee):
    """Validate response-time options and return them with the digest entries for non-default values only."""
    priority = _priority(priority)
    if escalate_to is not None:
        _identifier(escalate_to, "escalate_to")
    if fallback_assignee is not None:
        _identifier(fallback_assignee, "fallback_assignee")
        if assignee is not None and fallback_assignee == assignee.principal:
            raise StoreError("invalid", "fallback_assignee", "principal different from the assignee")
    if escalate_to == owner:
        escalate_to = None
    args = {}
    if priority != "normal":
        args["priority"] = priority
    if escalate_to is not None:
        args["escalate_to"] = escalate_to
    if fallback_assignee is not None:
        args["fallback_assignee"] = fallback_assignee
    return priority, escalate_to, fallback_assignee, args


def _dependency_ids(values):
    if values is None:
        return []
    if not isinstance(values, list) or len(values) > 100:
        raise StoreError("invalid", "depends_on", "array of at most 100 task UUID strings")
    return sorted({_uuid(value, "depends_on") for value in values})


def _graph_entries(graph):
    if not isinstance(graph, list) or not graph or len(graph) > 100:
        raise StoreError("invalid", "graph", "array of 1-100 {task_id, depends_on} objects with unique task_id")
    normalized = []
    seen = set()
    for entry in graph:
        if not isinstance(entry, dict) or set(entry) != {"task_id", "depends_on"} or not isinstance(entry["depends_on"], list):
            raise StoreError("invalid", "graph", "array of 1-100 {task_id, depends_on} objects with unique task_id")
        task_id = _uuid(entry["task_id"], "graph.task_id")
        if task_id in seen:
            raise StoreError("invalid", "graph", "array of 1-100 {task_id, depends_on} objects with unique task_id")
        seen.add(task_id)
        dependencies = _dependency_ids(entry["depends_on"])
        if task_id in dependencies:
            raise StoreError("graph_cycle")
        normalized.append({"task_id": task_id, "depends_on": dependencies})
    if sum(len(entry["depends_on"]) for entry in normalized) > 1000:
        raise StoreError("invalid", "graph", "array of 1-100 {task_id, depends_on} objects with unique task_id")
    return sorted(normalized, key=lambda entry: entry["task_id"])


def _expiry(value):
    """An ISO 8601 instant with an explicit UTC offset, or None for no expiry."""
    if value is None:
        return None
    expected = "ISO 8601 timestamp with a UTC offset, in the future"
    if not isinstance(value, str) or len(value) > 64 or "\x00" in value:
        raise StoreError("invalid", "expires_at", expected)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise StoreError("invalid", "expires_at", expected) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise StoreError("invalid", "expires_at", expected)
    return parsed.astimezone(timezone.utc)


def _delegation_request(grantor, delegate, scope, space_id, expires_at, may_delegate, reason):
    if (not isinstance(delegate, Actor) or delegate.principal in {SERVICE_ACTOR.principal, LEGACY_SERVICE_PRINCIPAL}
            or delegate.account == SERVICE_ACTOR.account or delegate.principal == grantor.principal):
        raise StoreError("invalid", "delegate_principal", "a connected principal other than the grantor and the service")
    if not isinstance(scope, str) or scope != APPROVE:
        raise StoreError("invalid", "scope", "approve")
    if type(may_delegate) is not bool:
        raise StoreError("invalid", "may_delegate", "boolean")
    return (delegate, scope, None if space_id is None else _uuid(space_id, "space_id"), _expiry(expires_at), may_delegate,
            _trimmed(reason, 500, "reason"))


APPROVE = "approve"


def _policy_matches(kind, value, scopes):
    """A policy applies when one of the task's declared scopes falls under it.

    The whole-space policy (empty path) applies to every task, including one whose scope is "none". A whole-repository
    scope falls under every path policy. Otherwise a "none" scope declares nothing and matches no narrower policy.
    """
    if kind == "path" and value == "":
        return True
    for scope in scopes:
        if scope["kind"] != kind:
            continue
        if kind == "path":
            declared = scope["value"].casefold()
            if declared in {"", value.casefold()} or declared.startswith(value.casefold() + "/"):
                return True
        elif scope["value"] == value:
            return True
    return False


DELEGATION_DEPTH = 3
DELEGATION_LIST_LIMIT = 500


class SpaceStore:
    def __init__(self, config_path, directory=None, response_minutes=None, rotation_grace_seconds=WEBHOOK_ROTATION_GRACE_SECONDS):
        self.config_path = Path(config_path)
        self.rotation_grace_seconds = rotation_grace_seconds
        self._secret_sweep_at = None
        self.response_minutes = {**DEFAULT_RESPONSE_MINUTES, **(response_minutes or {})}
        # directory(principal) -> Actor | None resolves principals the store only knows by name
        # (reviewers, assignees, mention targets) so notification recipients can be permission-checked.
        self.directory = directory

    @classmethod
    def from_env(cls, directory=None):
        return cls(os.environ.get("BATON_DATABASE_CONFIG", "/run/secrets/database.json"), directory,
                   parse_response_minutes(os.environ.get("BATON_RESPONSE_MINUTES")),
                   parse_rotation_grace(os.environ.get(WEBHOOK_ROTATION_GRACE_ENV)))

    @contextmanager
    def _transaction(self, write=False, single_flight=None):
        try:
            config = json.loads(self.config_path.read_text())
            if set(config) != {"host", "port", "dbname", "user", "password"}:
                raise StoreError("storage_unavailable")
            with psycopg.connect(**config, connect_timeout=5, row_factory=dict_row) as conn:
                # Taken before the global lock so a second runner skips instead of queueing behind the first.
                if single_flight is not None and not conn.execute("SELECT pg_try_advisory_xact_lock(%s) AS ok", (single_flight,)).fetchone()["ok"]:
                    raise _Busy()
                lock = "pg_advisory_xact_lock" if write else "pg_advisory_xact_lock_shared"
                conn.execute(f"SELECT {lock}(%s)", (LOCK_ID,))
                yield conn
        except StoreError:
            raise
        except (psycopg.Error, OSError, ValueError, TypeError):
            # Never propagate libpq diagnostics: they can contain credentials or SQL values.
            raise StoreError("storage_unavailable") from None

    @staticmethod
    def _user(actor):
        if (not isinstance(actor, Actor) or actor.principal in {SERVICE_ACTOR.principal, LEGACY_SERVICE_PRINCIPAL}
                or actor.account == SERVICE_ACTOR.account):
            raise StoreError("forbidden")

    @staticmethod
    def _service(actor):
        if actor != SERVICE_ACTOR:
            raise StoreError("forbidden")

    def _read(self, actor, action):
        if not isinstance(actor, Actor):
            raise StoreError("forbidden")
        with self._transaction() as conn:
            return _json(action(conn))

    def _mutate(self, actor, operation_id, method, args, authorize, effect, replay_guard=None):
        if not isinstance(actor, Actor):
            raise StoreError("forbidden")
        operation_id = _uuid(operation_id)
        digest = hashlib.sha256(_canonical([asdict(actor), method, args]).encode()).hexdigest()
        with self._transaction(write=True) as conn:
            authorize(conn)
            old = conn.execute("SELECT request_hash,result FROM baton.operations WHERE principal=%s AND operation_id=%s",
                               (actor.principal, operation_id)).fetchone()
            if old:
                if old["request_hash"] != digest:
                    raise StoreError("operation_conflict")
                result = old["result"]
                if replay_guard:
                    current = replay_guard(conn, result)
                    if current is not None:
                        result = _json(current)
                if isinstance(result, dict):
                    result = {**result, "replay": True}
                return result
            result = _json(effect(conn))
            once = {}
            if isinstance(result, dict):
                business_replay = result.pop("_business_replay", False)
                # Values under _once reach the first caller only and are never persisted or replayed.
                once = result.pop("_once", {})
                result = {**result, "replay": business_replay}
            conn.execute("INSERT INTO baton.operations(principal,operation_id,request_hash,method,result) VALUES (%s,%s,%s,%s,%s)",
                         (actor.principal, operation_id, digest, method, Jsonb(result)))
            return {**result, **once} if once else result

    @staticmethod
    def _row(conn, table, identity):
        # table is an internal constant, never a request parameter.
        row = conn.execute(f"SELECT * FROM baton.{table} WHERE id=%s", (_uuid(identity),)).fetchone()
        if not row:
            raise StoreError("not_found")
        return row

    def _role(self, conn, actor, resource_type, resource_id):
        self._user(actor)
        return self._account_role(conn, actor.account, resource_type, resource_id)

    def _account_role(self, conn, account, resource_type, resource_id):
        if account == SERVICE_ACTOR.account:
            return None
        if resource_type == "space":
            space = self._row(conn, "spaces", resource_id)
            page_ids = []
        elif resource_type == "page":
            page = self._row(conn, "pages", resource_id)
            space = self._row(conn, "spaces", str(page["space_id"]))
            page_ids = [row["id"] for row in conn.execute("""WITH RECURSIVE ancestors AS (
                SELECT id,parent_id FROM baton.pages WHERE id=%s UNION
                SELECT p.id,p.parent_id FROM baton.pages p JOIN ancestors a ON p.id=a.parent_id
            ) SELECT id FROM ancestors""", (resource_id,))]
        else:
            raise StoreError("invalid")
        if space["owner_account"] == account:
            return "edit"
        grants = conn.execute("""SELECT g.role FROM baton.grants g WHERE
            ((g.resource_type='space' AND g.resource_id=%s) OR (g.resource_type='page' AND g.resource_id=ANY(%s::uuid[])))
            AND ((g.subject_type='person' AND g.subject_id=%s) OR (g.subject_type='team' AND EXISTS (
                SELECT 1 FROM baton.team_members m WHERE m.team_id::text=g.subject_id AND m.account=%s)))""",
            (space["id"], page_ids, account, account)).fetchall()
        return max((row["role"] for row in grants), key=ROLES.get, default=None)

    def _require(self, conn, actor, resource_type, resource_id, role="view", hidden="not_found"):
        """Check the caller's role. An object the caller cannot even view is reported as absent (X7);
        a visible object with too little permission is `forbidden`. `hidden="forbidden"` is for checks
        on someone else (an assignee or handoff target) where the caller can see the object."""
        held = self._role(conn, actor, resource_type, resource_id)
        if held is None:
            raise StoreError(hidden)
        if ROLES[held] < ROLES[role]:
            raise StoreError("forbidden")

    def _page_for_block(self, conn, actor, block_id, role="view"):
        block = self._row(conn, "blocks", block_id)
        self._require(conn, actor, "page", str(block["page_id"]), role)
        return block

    def _task_for(self, conn, actor, task_id, role="view"):
        task = self._row(conn, "tasks", task_id)
        self._require(conn, actor, "page", str(task["page_id"]), role)
        return task

    @staticmethod
    def _audit(conn, actor, operation_id, event, space_id=None, task_id=None, requested_by=None, performed_by=None, **metadata):
        allowed = {"page_id", "block_id", "thread_id", "epoch", "revision", "proposal_id", "team_id", "subject_type",
                   "role", "decision", "input_sha256", "output_sha256", "error_code",
                   "count", "target_type", "execution_mode", "old_epoch", "work_face_keys", "work_face_sha256",
                   "subscription_id", "notification_id", "attempts", "http_status", "notify_event", "object_type",
                   "priority", "escalate_to", "from_assignee", "to_assignee", "reason_code", "delegation_id", "grantor", "scope",
                   "created_via", "chain", "owner_kind", "self_approval", "root", "approvals", "approvals_required",
                   "required_approvals", "root_required", "scope_kind", "approver", "reason", "original_sha256", "edited_sha256", "grace_seconds",
                   "lease_seconds"}
        if not set(metadata) <= allowed:
            raise StoreError("invalid")
        conn.execute("""INSERT INTO baton.x_audit(id,event,agent,actor_account,actor_kind,requested_by,performed_by,
            operation_id,space_id,task_id,metadata) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (uuid.uuid4(), event, actor.principal, actor.account, actor.kind, requested_by or actor.principal,
             performed_by or actor.principal, operation_id, space_id, task_id, Jsonb(_json(metadata))))

    def _directory_party(self, principal):
        try:
            who = self.directory(principal) if self.directory and principal else None
        except Exception:
            who = None
        return (who.principal, who.account) if isinstance(who, Actor) and who.principal == principal else None

    @staticmethod
    def _party(actor):
        return (actor.principal, actor.account)

    def _task_parties(self, conn, task):
        """Principals around a task with the account each is known by. Unknown names go through the directory."""
        claim = conn.execute("""SELECT holder,holder_account FROM baton.x_claim WHERE task_id=%s
            AND lease_expires_at>clock_timestamp()""", (task["id"],)).fetchone()
        known = {task["owner"]: task["owner_account"]}
        if claim:
            known[claim["holder"]] = claim["holder_account"]
        if task["result_author"]:
            known[task["result_author"]] = task["result_author_account"]

        def party(principal):
            if principal is None:
                return None
            return (principal, known[principal]) if principal in known else self._directory_party(principal)
        return {"owner": party(task["owner"]), "assignee": party(task["assignee"]),
                "holder": (claim["holder"], claim["holder_account"]) if claim else None,
                "author": party(task["result_author"]), "reviewers": [party(name) for name in task["reviewers"]]}

    def _enqueue(self, conn, actor, event, object_type, object_id, page_id, parties, thread_id=None, result_epoch=None, result_author=None):
        """Insert outbox rows in the caller's transaction. Metadata only; the actor is never notified of their own act."""
        if event not in NOTIFY_EVENTS or object_type not in {"task", "page", "comment"}:
            raise StoreError("invalid")
        seen = {actor.principal}
        for party in parties:
            if party is None or party[0] in seen:
                continue
            seen.add(party[0])
            principal, account = party
            if self._account_role(conn, account, "page", str(page_id)) is None:
                continue
            row = conn.execute("""INSERT INTO baton.x_notify(id,recipient,recipient_account,event,object_type,object_id,thread_id,actor,actor_kind,
                result_epoch,result_author) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (uuid.uuid4(), principal, account, event, object_type, object_id, thread_id, actor.principal,
                 "service" if actor == SERVICE_ACTOR else actor.kind, result_epoch, result_author)).fetchone()
            for subscription in conn.execute("""SELECT id FROM baton.x_webhook WHERE principal=%s AND account=%s
                AND (events IS NULL OR events @> %s)""", (principal, account, Jsonb([event]))).fetchall():
                conn.execute("INSERT INTO baton.x_webhook_delivery(subscription_id,notification_id) VALUES (%s,%s)",
                             (subscription["id"], row["id"]))

    def check_schema(self, actor):
        self._service(actor)
        def check(conn):
            version = conn.execute("SELECT version FROM baton.schema_version WHERE singleton").fetchone()
            role = conn.execute("""SELECT r.rolsuper,c.relowner=r.oid AS owns_audit FROM pg_roles r
                CROSS JOIN pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE r.rolname=current_user AND n.nspname='baton' AND c.relname='x_audit'""").fetchone()
            triggers = conn.execute("SELECT count(*) AS n FROM pg_trigger WHERE tgrelid='baton.x_audit'::regclass AND tgname IN ('audit_no_change','audit_no_truncate') AND tgenabled='O'").fetchone()
            required = {
                "spaces": "id name owner_account owner_principal owner_kind scope_repository default_for",
                "pages": "id space_id parent_id owner_account title revision",
                "blocks": "id page_id kind content position revision author_principal author_account author_kind",
                "block_revisions": "block_id revision content author_principal operation_id proposal_id",
                "teams": "id name owner_account", "team_members": "team_id account",
                "grants": "resource_type resource_id subject_type subject_id role",
                "comments": "id page_id thread_id body author_principal requested_by",
                "mentions": "id comment_id target_type target_id action_requested requested_by created_at",
                "proposals": "id block_id expected_revision proposed_content status revision",
                "proposal_revisions": "proposal_id revision content author_principal",
                "tasks": "id page_id block_id owner owner_account owner_kind assignee idempotency_key input_hash prompt execution_mode gate_mode reviewers scopes state epoch result_epoch result result_author result_author_account base_commit context evidence a2a_id backend dispatch_started_at error work_face require_work_face task_kind priority respond_by responded_at escalated_at escalate_to fallback_assignee blocked_reason blocked_by blocked_at approvals_required approvals_override_root",
                "task_dependencies": "task_id depends_on created_at",
                "x_claim": "task_id holder holder_account holder_kind epoch lease_expires_at scopes scope_repository",
                "x_handoff": "task_id old_epoch new_epoch base_commit context evidence",
                "x_gate": "task_id epoch decision reviewer reviewer_account evidence delegation_id grantor root self_approval edited original_result edited_result reason",
                "x_approval_policy": "space_id scope_kind scope_value required_approvals root_required updated_at",
                "x_delegation": "id grantor grantor_account grantor_kind delegate delegate_account delegate_kind scope space_id may_delegate parent_id reason created_via created_by created_at expires_at revoked_at revoked_by",
                "x_claim_holder": "task_id epoch holder holder_account created_at",
                "operations": "principal operation_id request_hash method result",
                "x_audit": "id time event agent actor_account actor_kind requested_by performed_by operation_id space_id task_id metadata",
                "x_notify": "id recipient recipient_account event object_type object_id thread_id created_at seq actor actor_kind result_epoch result_author",
                "x_webhook": "id principal account url events secret created_at include_preview previous_secret previous_secret_expires_at",
                "x_webhook_delivery": "subscription_id notification_id state attempts next_attempt_at created_at finished_at",
                "x_webhook_attempt": "id subscription_id notification_id attempt outcome http_status duration_ms created_at",
            }
            columns = {}
            for column in conn.execute("SELECT table_name,column_name FROM information_schema.columns WHERE table_schema='baton'"):
                columns.setdefault(column["table_name"], set()).add(column["column_name"])
            if any(not set(names.split()) <= columns.get(table, set()) for table, names in required.items()):
                raise StoreError("storage_unavailable")
            if not version or version["version"] != 1 or not role or role["rolsuper"] or role["owns_audit"] or triggers["n"] != 2:
                raise StoreError("storage_unavailable")
            return {"status": "ok", "schema_version": 1}
        return self._read(actor, check)

    def effective_role(self, actor, resource_type, resource_id):
        return self._read(actor, lambda conn: {"role": self._role(conn, actor, resource_type, resource_id)})

    def create_space(self, actor, operation_id, name):
        self._user(actor)
        _text(name, 200, field="name")
        def effect(conn):
            identity = str(uuid.uuid4())
            row = conn.execute("INSERT INTO baton.spaces(id,name,owner_account,owner_principal,owner_kind) VALUES (%s,%s,%s,%s,%s) RETURNING *",
                               (identity, name, actor.account, actor.principal, actor.kind)).fetchone()
            self._audit(conn, actor, operation_id, "space.created", space_id=identity)
            return row
        return self._mutate(actor, operation_id, "create_space", {"name": name}, lambda conn: self._user(actor), effect)

    def get_space(self, actor, space_id):
        def read(conn):
            self._require(conn, actor, "space", space_id)
            return self._row(conn, "spaces", space_id)
        return self._read(actor, read)

    def list_spaces(self, actor):
        self._user(actor)
        return self._read(actor, lambda conn: [row for row in conn.execute("SELECT * FROM baton.spaces ORDER BY created_at,id")
                                               if self._role(conn, actor, "space", str(row["id"]))])

    def create_page(self, actor, operation_id, space_id, title, parent_id=None):
        _text(title, 200, field="title")
        def authorize(conn):
            self._require(conn, actor, "page" if parent_id else "space", parent_id or space_id, "edit")
            if parent_id and str(self._row(conn, "pages", parent_id)["space_id"]) != _uuid(space_id):
                raise StoreError("invalid")
        def effect(conn):
            space = self._row(conn, "spaces", space_id)
            row = conn.execute("INSERT INTO baton.pages(id,space_id,parent_id,owner_account,title) VALUES (%s,%s,%s,%s,%s) RETURNING *",
                               (uuid.uuid4(), space_id, parent_id, space["owner_account"], title)).fetchone()
            self._audit(conn, actor, operation_id, "page.created", space_id=space_id, page_id=row["id"], revision=1)
            return row
        return self._mutate(actor, operation_id, "create_page", {"space_id": space_id, "title": title, "parent_id": parent_id}, authorize, effect)

    def get_page(self, actor, page_id):
        def read(conn):
            self._require(conn, actor, "page", page_id)
            return self._row(conn, "pages", page_id)
        return self._read(actor, read)

    def list_pages(self, actor, space_id, parent_id=None):
        def read(conn):
            self._user(actor)
            self._row(conn, "spaces", space_id)
            if parent_id:
                self._require(conn, actor, "page", parent_id)
            rows = conn.execute("SELECT * FROM baton.pages WHERE space_id=%s AND parent_id IS NOT DISTINCT FROM %s::uuid ORDER BY created_at,id", (space_id, parent_id)).fetchall()
            return [row for row in rows if self._role(conn, actor, "page", str(row["id"]))]
        return self._read(actor, read)

    def _revision(self, conn, actor, operation_id, block, content, proposal_id=None):
        conn.execute("""INSERT INTO baton.block_revisions(block_id,revision,content,author_principal,author_account,author_kind,operation_id,proposal_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""", (block["id"], block["revision"], Jsonb(content), actor.principal, actor.account, actor.kind, operation_id, proposal_id))

    def create_block(self, actor, operation_id, page_id, kind, content, position=None):
        if not isinstance(kind, str) or kind not in {"text", "file", "prompt", "agent_instructions"}:
            raise StoreError("invalid", 'kind', 'one of text, file, prompt, agent_instructions')
        _payload(content, "content")
        if position is not None:
            _integer(position, 0, 1000000000, "position")
        def effect(conn):
            order = position if position is not None else conn.execute("SELECT COALESCE(MAX(position),-1)+1 AS n FROM baton.blocks WHERE page_id=%s", (page_id,)).fetchone()["n"]
            row = conn.execute("""INSERT INTO baton.blocks(id,page_id,kind,content,position,author_principal,author_account,author_kind)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""", (uuid.uuid4(), page_id, kind, Jsonb(content), order, actor.principal, actor.account, actor.kind)).fetchone()
            self._revision(conn, actor, operation_id, row, content)
            page = conn.execute("UPDATE baton.pages SET revision=revision+1 WHERE id=%s RETURNING *", (page_id,)).fetchone()
            self._audit(conn, actor, operation_id, "block.created", space_id=page["space_id"], page_id=page_id, block_id=row["id"], revision=1)
            return row
        return self._mutate(actor, operation_id, "create_block", {"page_id": page_id, "kind": kind, "content": content, "position": position},
                            lambda conn: self._require(conn, actor, "page", page_id, "edit"), effect)

    def get_block(self, actor, block_id):
        return self._read(actor, lambda conn: self._page_for_block(conn, actor, block_id))

    def list_blocks(self, actor, page_id):
        def read(conn):
            self._require(conn, actor, "page", page_id)
            return conn.execute("SELECT * FROM baton.blocks WHERE page_id=%s ORDER BY position,id", (page_id,)).fetchall()
        return self._read(actor, read)

    def _update_block(self, conn, actor, operation_id, block_id, expected_revision, content, proposal_id=None):
        block = self._page_for_block(conn, actor, block_id, "edit")
        if block["kind"] == "task":
            raise StoreError("forbidden")
        if block["revision"] != expected_revision:
            raise StoreError("revision_conflict")
        block = conn.execute("""UPDATE baton.blocks SET content=%s,revision=revision+1,author_principal=%s,
            author_account=%s,author_kind=%s WHERE id=%s RETURNING *""", (Jsonb(content), actor.principal, actor.account, actor.kind, block_id)).fetchone()
        self._revision(conn, actor, operation_id, block, content, proposal_id)
        page = conn.execute("UPDATE baton.pages SET revision=revision+1 WHERE id=%s RETURNING *", (block["page_id"],)).fetchone()
        self._audit(conn, actor, operation_id, "block.updated", space_id=page["space_id"], block_id=block_id, revision=block["revision"])
        return block

    def update_block(self, actor, operation_id, block_id, expected_revision, content):
        _integer(expected_revision, 1, 2**63 - 1, "expected_revision")
        _payload(content, "content")
        return self._mutate(actor, operation_id, "update_block", {"block_id": block_id, "expected_revision": expected_revision, "content": content},
                            lambda conn: self._page_for_block(conn, actor, block_id, "edit"),
                            lambda conn: self._update_block(conn, actor, operation_id, block_id, expected_revision, content))

    def share(self, actor, operation_id, resource_type, resource_id, subject_type, subject_id, role):
        if not isinstance(resource_type, str) or resource_type not in {"space", "page"}:
            raise StoreError("invalid", "resource_type", "one of space, page")
        if not isinstance(subject_type, str) or subject_type not in {"person", "team"}:
            raise StoreError("invalid", "subject_type", "one of person, team")
        if role is not None and (not isinstance(role, str) or role not in ROLES):
            raise StoreError("invalid", "role", "one of view, comment, edit, or null to remove the grant")
        if subject_type == "team":
            _uuid(subject_id, "subject_id")
            if role == "comment":
                raise StoreError("invalid", "role", "view or edit for a team (comment is not available), or null")
        else:
            _identifier(subject_id, "subject_id")
        def authorize(conn):
            self._user(actor)
            row = self._row(conn, "spaces" if resource_type == "space" else "pages", resource_id)
            if row["owner_account"] != actor.account:
                raise StoreError("forbidden")
            if subject_type == "team":
                self._row(conn, "teams", subject_id)
        def effect(conn):
            args = (resource_type, resource_id, subject_type, subject_id)
            if role is None:
                conn.execute("DELETE FROM baton.grants WHERE resource_type=%s AND resource_id=%s AND subject_type=%s AND subject_id=%s", args)
            else:
                conn.execute("""INSERT INTO baton.grants VALUES (%s,%s,%s,%s,%s)
                    ON CONFLICT(resource_type,resource_id,subject_type,subject_id) DO UPDATE SET role=EXCLUDED.role""", (*args, role))
            row = self._row(conn, "spaces" if resource_type == "space" else "pages", resource_id)
            self._audit(conn, actor, operation_id, "sharing.changed", space_id=row["id"] if resource_type == "space" else row["space_id"], subject_type=subject_type, role=role)
            return {"resource_type": resource_type, "resource_id": resource_id, "subject_type": subject_type, "subject_id": subject_id, "role": role}
        return self._mutate(actor, operation_id, "share", {"resource_type": resource_type, "resource_id": resource_id, "subject_type": subject_type, "subject_id": subject_id, "role": role}, authorize, effect)

    def create_team(self, actor, operation_id, name):
        _text(name, 200, field="name")
        def effect(conn):
            row = conn.execute("INSERT INTO baton.teams VALUES (%s,%s,%s) RETURNING *", (uuid.uuid4(), name, actor.account)).fetchone()
            conn.execute("INSERT INTO baton.team_members VALUES (%s,%s)", (row["id"], actor.account))
            self._audit(conn, actor, operation_id, "team.created", team_id=row["id"])
            return row
        return self._mutate(actor, operation_id, "create_team", {"name": name}, lambda conn: self._user(actor), effect)

    def set_team_member(self, actor, operation_id, team_id, account, member):
        _identifier(account, "account")
        if type(member) is not bool:
            raise StoreError("invalid", 'member', 'boolean')
        def authorize(conn):
            self._user(actor)
            if self._row(conn, "teams", team_id)["owner_account"] != actor.account:
                raise StoreError("forbidden")
        def effect(conn):
            if member:
                conn.execute("INSERT INTO baton.team_members VALUES (%s,%s) ON CONFLICT DO NOTHING", (team_id, account))
            else:
                if account == self._row(conn, "teams", team_id)["owner_account"]:
                    raise StoreError("invalid")
                conn.execute("DELETE FROM baton.team_members WHERE team_id=%s AND account=%s", (team_id, account))
            self._audit(conn, actor, operation_id, "team.membership_changed", team_id=team_id)
            return {"team_id": team_id, "account": account, "member": member}
        return self._mutate(actor, operation_id, "set_team_member", {"team_id": team_id, "account": account, "member": member}, authorize, effect)

    def _comment(self, conn, actor, operation_id, page_id, body, thread_id=None, selection=None, subject="Comment", requested_by=None):
        identity = uuid.uuid4()
        thread_id = thread_id or str(identity)
        row = conn.execute("""INSERT INTO baton.comments(id,page_id,thread_id,body,selection,subject,author_principal,author_account,author_kind,requested_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (identity, page_id, thread_id, body, Jsonb(selection), subject, actor.principal, actor.account, actor.kind, requested_by or actor.principal)).fetchone()
        return row

    def comment(self, actor, operation_id, page_id, body, thread_id=None, selection=None):
        _text(body, field="body")
        _payload(selection, "selection")
        if thread_id:
            _uuid(thread_id, "thread_id")
        def effect(conn):
            if thread_id and not conn.execute("SELECT 1 FROM baton.comments WHERE page_id=%s AND thread_id=%s", (page_id, thread_id)).fetchone():
                raise StoreError("not_found")
            row = self._comment(conn, actor, operation_id, page_id, body, thread_id, selection)
            page = self._row(conn, "pages", page_id)
            task = conn.execute("SELECT * FROM baton.tasks WHERE id=%s AND page_id=%s", (row["thread_id"], page_id)).fetchone()
            self._audit(conn, actor, operation_id, "comment.created", space_id=page["space_id"], task_id=task["id"] if task else None,
                        page_id=page_id, thread_id=row["thread_id"])
            if task:
                self._record_response(conn, actor, task)
                parties = self._task_parties(conn, task)
                self._enqueue(conn, actor, "comment.created", "comment", row["id"], page_id,
                              [parties["owner"], parties["assignee"], parties["holder"], *parties["reviewers"]], row["thread_id"])
            return row
        return self._mutate(actor, operation_id, "comment", {"page_id": page_id, "body": body, "thread_id": thread_id, "selection": selection},
                            lambda conn: self._require(conn, actor, "page", page_id, "comment"), effect)

    def list_comments(self, actor, page_id=None, thread_id=None, task_id=None):
        """Comments of a page, optionally one thread; `task_id` names a task's page and thread instead."""
        if thread_id:
            _uuid(thread_id, "thread_id")
        if task_id is not None:
            _uuid(task_id, "task_id")
            if page_id is not None or thread_id is not None:
                raise StoreError("invalid", "task_id", "given alone, not together with page_id or thread_id")
        elif page_id is None:
            raise StoreError("invalid", "page_id", "UUID string, or give task_id instead")
        def read(conn):
            nonlocal page_id, thread_id
            if task_id is not None:
                task = self._task_for(conn, actor, task_id)
                page_id, thread_id = str(task["page_id"]), str(task["id"])
            self._require(conn, actor, "page", page_id)
            return conn.execute("SELECT * FROM baton.comments WHERE page_id=%s AND (%s::uuid IS NULL OR thread_id=%s::uuid) ORDER BY created_at,id", (page_id, thread_id, thread_id)).fetchall()
        return self._read(actor, read)

    def mention(self, actor, operation_id, comment_id, target_type, target_id):
        if not isinstance(target_type, str) or target_type not in {"person", "agent"}:
            raise StoreError("invalid", 'target_type', 'one of person, agent')
        _identifier(target_id, "target_id")
        def authorize(conn):
            comment = self._row(conn, "comments", comment_id)
            self._require(conn, actor, "page", str(comment["page_id"]), "comment")
        def effect(conn):
            comment = self._row(conn, "comments", comment_id)
            row = conn.execute("""INSERT INTO baton.mentions AS saved(id,comment_id,target_type,target_id,action_requested,requested_by) VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT(comment_id,target_type,target_id,requested_by) DO UPDATE SET action_requested=true,created_at=clock_timestamp()
                WHERE EXCLUDED.action_requested AND NOT saved.action_requested RETURNING *""",
                (uuid.uuid4(), comment_id, target_type, target_id, target_type == "agent", actor.principal)).fetchone()
            if row is None:
                row = conn.execute("SELECT * FROM baton.mentions WHERE comment_id=%s AND target_type=%s AND target_id=%s AND requested_by=%s",
                                   (comment_id, target_type, target_id, actor.principal)).fetchone()
                return {**row, "_business_replay": True}
            page = self._row(conn, "pages", str(comment["page_id"]))
            task = conn.execute("SELECT id FROM baton.tasks WHERE id=%s AND page_id=%s", (comment["thread_id"], comment["page_id"])).fetchone()
            self._audit(conn, actor, operation_id, "mention.created", space_id=page["space_id"], task_id=task["id"] if task else None,
                        thread_id=comment["thread_id"], target_type=target_type)
            self._enqueue(conn, actor, "mention.created", "comment", comment["id"], comment["page_id"],
                          [self._directory_party(target_id)], comment["thread_id"])
            return row
        return self._mutate(actor, operation_id, "mention", {"comment_id": comment_id, "target_type": target_type, "target_id": target_id}, authorize, effect)

    def fetch_inbox(self, actor, limit=30):
        _integer(limit, 1, 100, "limit")
        self._user(actor)
        def read(conn):
            rows = conn.execute("""SELECT c.*,m.id AS mention_id,m.action_requested AS mention_action_requested,
                m.requested_by AS mention_requested_by,m.target_type,m.target_id,m.created_at AS requested_at,
                GREATEST(c.created_at,m.created_at) AS activity_at
                FROM baton.comments c JOIN baton.mentions m ON m.comment_id=c.id
                WHERE m.target_id=%s AND m.target_type IN ('person','agent')
                ORDER BY activity_at DESC,m.id DESC""", (actor.principal,)).fetchall()
            threads = {}
            for row in rows:
                if not self._role(conn, actor, "page", str(row["page_id"])):
                    continue
                request = {"mention_id": str(row["mention_id"]), "comment_id": str(row["id"]),
                           "requested_by": row["mention_requested_by"], "action_requested": row["mention_action_requested"],
                           "target_principal": row["target_id"], "target_type": row["target_type"], "created_ts": row["requested_at"]}
                message = {"id": str(row["id"]), "thread_id": str(row["thread_id"]), "subject": row["subject"],
                           "from": row["author_principal"], "to": [actor.principal], "created_ts": row["activity_at"],
                           "comment_created_at": row["created_at"], "body_md": row["body"],
                           "actor": {"principal": row["author_principal"], "kind": row["author_kind"]},
                           "body_preview": row["body"][:PREVIEW_CHARS],
                           "requested_by": request["requested_by"], "action_requested": request["action_requested"], "requests": []}
                threads.setdefault(message["thread_id"], message)["requests"].append(request)
            assigned = conn.execute("SELECT * FROM baton.tasks WHERE assignee=%s ORDER BY created_at DESC", (actor.principal,)).fetchall()
            for task in assigned:
                if not self._role(conn, actor, "page", str(task["page_id"])):
                    continue
                identity = str(task["id"])
                message = threads.setdefault(identity, {"id": identity, "thread_id": identity, "subject": "[assigned] " + identity,
                    "from": task["owner"], "to": [actor.principal], "created_ts": task["created_at"], "body_md": task["prompt"],
                    "actor": {"principal": task["owner"], "kind": task["owner_kind"]}, "body_preview": task["prompt"][:PREVIEW_CHARS],
                    "requested_by": task["owner"], "action_requested": True, "requests": []})
                view = self._task_view(conn, actor, identity)
                message.update(task_id=identity, assignee=task["assignee"], prompt=task["prompt"], state=view["state"], kind="assignment",
                               depends_on=view["depends_on"], has_hidden_dependencies=view["has_hidden_dependencies"])
            for identity, message in threads.items():
                if "task_id" not in message:
                    task = conn.execute("SELECT assignee,page_id FROM baton.tasks WHERE id=%s", (identity,)).fetchone()
                    if task and self._role(conn, actor, "page", str(task["page_id"])):
                        view = self._task_view(conn, actor, identity)
                        message.update(task_id=identity, assignee=task["assignee"], state=view["state"], kind="mention",
                                       depends_on=view["depends_on"], has_hidden_dependencies=view["has_hidden_dependencies"])
            return {"agent_name": actor.principal, "messages": sorted(threads.values(), key=lambda item: item["created_ts"], reverse=True)[:limit]}
        return self._read(actor, read)

    def _notification_page(self, conn, row):
        if row["object_type"] == "page":
            return row["object_id"]
        # The table name is chosen from a fixed pair, never from request input.
        table = "tasks" if row["object_type"] == "task" else "comments"
        owner = conn.execute(f"SELECT page_id FROM baton.{table} WHERE id=%s", (row["object_id"],)).fetchone()
        return owner["page_id"] if owner else None

    @staticmethod
    def _title(prompt):
        return " ".join(prompt.split())[:SUBJECT_TITLE_CHARS]

    @staticmethod
    def _notification_result(row):
        """The result epoch and author an awaiting_gate notification was raised for; absent for every other event."""
        if row.get("result_epoch") is None:
            return {}
        return {"result_epoch": row["result_epoch"], "result_author": row["result_author"]}

    def _notification_preview(self, conn, row):
        """Actor, subject and body preview of one notification, read from the live objects.

        Call only after the recipient's current view permission on the object's page was confirmed. Nothing
        here is stored in the outbox, so a revoked or edited object never leaves a stale copy behind.
        Legacy automatic (codex) rows keep their prompts private, as in the task view, so only their id is shown.
        """
        actor = {"principal": row["actor"], "kind": row["actor_kind"]} if row.get("actor") and row.get("actor_kind") else None
        subject = body = None
        if row["object_type"] == "comment":
            comment = conn.execute("SELECT thread_id,body,author_principal,author_kind FROM baton.comments WHERE id=%s", (row["object_id"],)).fetchone()
            if comment:
                actor = actor or {"principal": comment["author_principal"], "kind": comment["author_kind"]}
                task = conn.execute("SELECT 1 FROM baton.tasks WHERE id=%s", (comment["thread_id"],)).fetchone()
                where = str(comment["thread_id"]) if task else "thread " + str(comment["thread_id"])
                subject = ("mention in " if row["event"] == "mention.created" else "comment on ") + where
                body = comment["body"][:PREVIEW_CHARS]
        elif row["object_type"] == "task":
            task = conn.execute("SELECT id,prompt,execution_mode FROM baton.tasks WHERE id=%s", (row["object_id"],)).fetchone()
            if task:
                label = "[" + EVENT_LABELS.get(row["event"], row["event"]) + "] "
                if task["execution_mode"] == "manual":
                    subject, body = label + self._title(task["prompt"]), task["prompt"][:PREVIEW_CHARS]
                else:
                    subject = label + str(task["id"])
        result = {}
        if actor:
            result["actor"] = actor
        if subject is not None:
            result["subject"] = subject
        if body is not None:
            result["body_preview"] = body
        return result

    def poll_notifications(self, actor, after_seq=0, limit=50):
        """One non-blocking pass over the caller's outbox, rechecking view permission on every row.

        Rows hidden by a revoked permission are skipped but still advance next_seq so callers never rescan them.
        """
        _integer(after_seq, 0, 2**63 - 1, "after_seq")
        _integer(limit, 1, 100, "limit")
        self._user(actor)
        def read(conn):
            visible, cursor, scanned, more, roles = [], after_seq, 0, False, {}
            while len(visible) < limit and not more:
                rows = conn.execute("""SELECT * FROM baton.x_notify WHERE recipient=%s AND recipient_account=%s AND seq>%s
                    ORDER BY seq LIMIT %s""", (actor.principal, actor.account, cursor, NOTIFY_SCAN_BATCH)).fetchall()
                if not rows:
                    break
                for row in rows:
                    if len(visible) == limit:
                        break
                    cursor = row["seq"]
                    scanned += 1
                    page = self._notification_page(conn, row)
                    if page not in roles:
                        try:
                            roles[page] = page is not None and self._account_role(conn, actor.account, "page", str(page)) is not None
                        except StoreError:
                            roles[page] = False
                    if roles[page]:
                        visible.append({key: row[key] for key in ("id", "seq", "event", "object_type", "object_id", "thread_id", "created_at")}
                                       | {"recipient": row["recipient"]} | self._notification_result(row) | self._notification_preview(conn, row))
                more = scanned >= NOTIFY_SCAN_LIMIT and len(visible) < limit
            return {"notifications": visible, "next_seq": cursor, "more": more}
        return self._read(actor, read)

    def list_attribution(self, actor, block_id):
        def read(conn):
            self._page_for_block(conn, actor, block_id)
            return conn.execute("SELECT * FROM baton.block_revisions WHERE block_id=%s ORDER BY revision", (block_id,)).fetchall()
        return self._read(actor, read)

    def proposal_create(self, actor, operation_id, block_id, expected_revision, proposed_content, selection=None):
        _integer(expected_revision, 1, 2**63 - 1, "expected_revision")
        _payload(proposed_content, "proposed_content")
        _payload(selection, "selection")
        def effect(conn):
            block = self._page_for_block(conn, actor, block_id, "comment")
            if block["kind"] == "task":
                raise StoreError("forbidden")
            if block["revision"] != expected_revision:
                raise StoreError("revision_conflict")
            row = conn.execute("""INSERT INTO baton.proposals(id,block_id,expected_revision,proposed_content,selection,author_principal,author_account,author_kind)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""", (uuid.uuid4(), block_id, expected_revision, Jsonb(proposed_content), Jsonb(selection), actor.principal, actor.account, actor.kind)).fetchone()
            conn.execute("INSERT INTO baton.proposal_revisions(proposal_id,revision,content,author_principal,author_account,author_kind) VALUES (%s,1,%s,%s,%s,%s)",
                         (row["id"], Jsonb(proposed_content), actor.principal, actor.account, actor.kind))
            page = self._row(conn, "pages", str(block["page_id"]))
            self._audit(conn, actor, operation_id, "proposal.created", space_id=page["space_id"], block_id=block_id, proposal_id=row["id"], revision=expected_revision)
            return row
        return self._mutate(actor, operation_id, "proposal_create", {"block_id": block_id, "expected_revision": expected_revision, "proposed_content": proposed_content, "selection": selection},
                            lambda conn: self._page_for_block(conn, actor, block_id, "comment"), effect)

    def get_proposal(self, actor, proposal_id):
        def read(conn):
            row = self._row(conn, "proposals", proposal_id)
            self._page_for_block(conn, actor, str(row["block_id"]))
            return row
        return self._read(actor, read)

    def list_proposals(self, actor, block_id):
        def read(conn):
            self._page_for_block(conn, actor, block_id)
            return conn.execute("SELECT * FROM baton.proposals WHERE block_id=%s ORDER BY created_at,id", (block_id,)).fetchall()
        return self._read(actor, read)

    def proposal_review(self, actor, operation_id, proposal_id, decision, content=None):
        if not isinstance(decision, str) or decision not in {"accept", "reject", "revise"} or (decision == "revise" and content is None) or (decision != "revise" and content is not None):
            raise StoreError("invalid", 'decision', 'accept or reject (without content), or revise (with content)')
        _payload(content)
        def authorize(conn):
            if actor.kind != "human":
                raise StoreError("forbidden")
            proposal = self._row(conn, "proposals", proposal_id)
            self._page_for_block(conn, actor, str(proposal["block_id"]), "edit")
        def effect(conn):
            proposal = self._row(conn, "proposals", proposal_id)
            if proposal["status"] != "pending":
                raise StoreError("revision_conflict")
            block = self._row(conn, "blocks", str(proposal["block_id"]))
            if decision in {"accept", "revise"} and block["revision"] != proposal["expected_revision"]:
                raise StoreError("revision_conflict")
            if decision == "accept":
                self._update_block(conn, actor, operation_id, str(block["id"]), proposal["expected_revision"], proposal["proposed_content"], proposal_id)
            if decision == "revise":
                conn.execute("UPDATE baton.proposals SET proposed_content=%s,revision=revision+1,last_decision='revise' WHERE id=%s", (Jsonb(content), proposal_id))
                conn.execute("INSERT INTO baton.proposal_revisions(proposal_id,revision,content,author_principal,author_account,author_kind) VALUES (%s,%s,%s,%s,%s,%s)",
                             (proposal_id, proposal["revision"] + 1, Jsonb(content), actor.principal, actor.account, actor.kind))
            else:
                conn.execute("UPDATE baton.proposals SET status=%s,last_decision=%s WHERE id=%s", ("accepted" if decision == "accept" else "rejected", decision, proposal_id))
            page = self._row(conn, "pages", str(block["page_id"]))
            self._audit(conn, actor, operation_id, "proposal.reviewed", space_id=page["space_id"], block_id=block["id"], proposal_id=proposal_id, decision=decision)
            return self._row(conn, "proposals", proposal_id)
        return self._mutate(actor, operation_id, "proposal_review", {"proposal_id": proposal_id, "decision": decision, "content": content}, authorize, effect)

    def revert_block(self, actor, operation_id, block_id, expected_revision, target_revision):
        _integer(target_revision, 1, 2**63 - 1, "target_revision")
        _integer(expected_revision, 1, 2**63 - 1, "expected_revision")
        def authorize(conn):
            if actor.kind != "human":
                raise StoreError("forbidden")
            self._page_for_block(conn, actor, block_id, "edit")
        def effect(conn):
            target = conn.execute("SELECT content FROM baton.block_revisions WHERE block_id=%s AND revision=%s", (block_id, target_revision)).fetchone()
            if not target:
                raise StoreError("not_found")
            return self._update_block(conn, actor, operation_id, block_id, expected_revision, target["content"])
        return self._mutate(actor, operation_id, "revert_block", {"block_id": block_id, "expected_revision": expected_revision, "target_revision": target_revision}, authorize, effect)

    def _task_view(self, conn, actor, task_id, include_prompt=False):
        row = dict(self._row(conn, "tasks", task_id))
        row.pop("input_hash")
        row.pop("idempotency_key")
        reason, blocker, blocked_at = (row.pop(key) for key in ("blocked_reason", "blocked_by", "blocked_at"))
        row["blocked"] = {"reason_code": reason, "by": blocker, "at": blocked_at} if reason else None
        if not include_prompt and row["execution_mode"] != "manual":
            row.pop("prompt")
        claim = conn.execute("SELECT *,lease_expires_at>clock_timestamp() AS lease_valid FROM baton.x_claim WHERE task_id=%s", (task_id,)).fetchone()
        row.update(task_id=row["id"], thread_id=row["id"], holder=None, holder_account=None, lease_expires_at=None, lease_valid=False)
        if claim:
            row.update({key: claim[key] for key in ("holder", "holder_account", "lease_expires_at", "lease_valid")})
        # Expiration releases authority immediately even before housekeeping.
        if row["state"] == "claimed" and not row["lease_valid"]:
            row["state"] = "interrupted" if row["execution_mode"] == "codex" and row["dispatch_started_at"] else "queued"
        row["status"] = {"claimed": "working", "done": "completed"}.get(row["state"], row["state"])
        reported = row["state"] in {"done", "failed"}
        row.update(submission_mail_status="sent", mail_status="sent" if reported else "not_ready", mail_error=None)
        row.update(submission_thread_status="sent", report_status="sent" if reported else "not_ready")
        row["work_face_missing"] = self._work_face_missing(row)
        override_root = row.pop("approvals_override_root")
        row.update(result_edits=[], original_result=None)
        if row["execution_mode"] == "manual" and row["result_epoch"] is not None:
            edits = conn.execute("""SELECT reviewer,reason,original_result,edited_result,created_at FROM baton.x_gate
                WHERE task_id=%s AND epoch=%s AND edited ORDER BY created_at,id""", (task_id, row["result_epoch"])).fetchall()
            if edits:
                row["original_result"] = edits[0]["original_result"]
                row["result_edits"] = [{"approver": edit["reviewer"], "reason": edit["reason"], "at": edit["created_at"],
                                        "original_sha256": _sha256(edit["original_result"]),
                                        "edited_sha256": _sha256(edit["edited_result"])} for edit in edits]
        if row["execution_mode"] == "manual":
            required, root_required = self._approval_requirements(conn, {**row, "approvals_override_root": override_root})
            row.update(approvals_required=required, root_required=root_required,
                       approvals_passed=len(self._passes(conn, task_id, row["result_epoch"])) if row["result_epoch"] is not None else 0)
        else:
            row.update(approvals_required=None, root_required=False, approvals_passed=0)
        row["overdue"] = bool(row["respond_by"] is not None and row["responded_at"] is None and row["state"] not in {"done", "failed"}
                              and conn.execute("SELECT %s<clock_timestamp() AS late", (row["respond_by"],)).fetchone()["late"])
        row["depends_on"], row["has_hidden_dependencies"] = self._visible_dependencies(conn, actor, task_id)
        return row

    @staticmethod
    def _work_face_missing(task):
        """Names of absent work-face parts; always empty unless the task requires a checklist."""
        if not task["require_work_face"]:
            return []
        missing = []
        if not isinstance(task["base_commit"], str) or not task["base_commit"].strip():
            missing.append("base_commit")
        # Neither the root placeholder nor an explicit "none" is a declared repository scope.
        if not task["scopes"] or task["scopes"] in (ROOT_SCOPES, NONE_SCOPES):
            missing.append("scopes")
        try:
            if task["work_face"] is None:
                raise StoreError("invalid")
            normalize_work_face(task["work_face"])
        except StoreError:
            missing.append("work_face")
        return missing

    def _require_work_face(self, task):
        if self._work_face_missing(task):
            raise StoreError("work_face_incomplete")

    @staticmethod
    def _dependencies(conn, task_id):
        return [str(row["depends_on"]) for row in conn.execute(
            "SELECT depends_on FROM baton.task_dependencies WHERE task_id=%s ORDER BY depends_on", (task_id,))]

    def _visible_dependencies(self, conn, actor, task_id):
        visible, hidden = [], False
        for dependency in self._dependencies(conn, task_id):
            try:
                self._task_for(conn, actor, dependency)
            except StoreError as exc:
                if exc.code not in {"forbidden", "not_found"}:
                    raise
                hidden = True
            else:
                visible.append(dependency)
        return visible, hidden

    def _require_dependency_access(self, conn, actor, task_id):
        _, hidden = self._visible_dependencies(conn, actor, task_id)
        if hidden:
            raise StoreError("forbidden")

    def _dependencies_satisfied(self, conn, actor, task_id):
        self._require_dependency_access(conn, actor, task_id)
        blocked = conn.execute("""SELECT 1 FROM baton.task_dependencies e JOIN baton.tasks d ON d.id=e.depends_on
            WHERE e.task_id=%s AND (d.state<>'done' OR d.result_epoch IS DISTINCT FROM d.epoch OR NOT EXISTS (
                SELECT 1 FROM baton.x_gate g WHERE g.task_id=d.id AND g.epoch=d.epoch AND g.decision='pass')) LIMIT 1""",
            (task_id,)).fetchone()
        return blocked is None

    @staticmethod
    def _require_unstarted(task):
        if task["state"] != "queued" or task["epoch"] != 0 or any(task[field] is not None for field in EXECUTION_FIELDS):
            raise StoreError("stale_claim")

    @staticmethod
    def _add_dependencies(conn, task_id, dependencies):
        if task_id in dependencies:
            raise StoreError("graph_cycle")
        added = 0
        for dependency in dependencies:
            row = conn.execute("""INSERT INTO baton.task_dependencies(task_id,depends_on) VALUES (%s,%s)
                ON CONFLICT DO NOTHING RETURNING depends_on""", (task_id, dependency)).fetchone()
            added += row is not None
        return added

    @staticmethod
    def _require_acyclic(conn, task_id):
        cycle = conn.execute("""WITH RECURSIVE reachable(id) AS (
            SELECT depends_on FROM baton.task_dependencies WHERE task_id=%s
            UNION SELECT e.depends_on FROM baton.task_dependencies e JOIN reachable r ON e.task_id=r.id
        ) SELECT 1 FROM reachable WHERE id=%s LIMIT 1""", (task_id, task_id)).fetchone()
        if cycle:
            raise StoreError("graph_cycle")

    def plan(self, actor, operation_id, graph):
        entries = _graph_entries(graph)
        def authorize(conn):
            for entry in entries:
                self._task_for(conn, actor, entry["task_id"], "edit")
                self._require_dependency_access(conn, actor, entry["task_id"])
                for dependency in entry["depends_on"]:
                    self._task_for(conn, actor, dependency)
        def view(conn):
            return {"graph": [{"task_id": entry["task_id"], "depends_on": self._dependencies(conn, entry["task_id"])} for entry in entries]}
        def effect(conn):
            tasks = {}
            for entry in entries:
                task = self._row(conn, "tasks", entry["task_id"])
                self._require_unstarted(task)
                tasks[entry["task_id"]] = task
            additions = {entry["task_id"]: self._add_dependencies(conn, entry["task_id"], entry["depends_on"]) for entry in entries}
            for entry in entries:
                self._require_acyclic(conn, entry["task_id"])
            for task_id, count in additions.items():
                if count:
                    self._task_audit(conn, actor, operation_id, "task.dependencies_added", tasks[task_id], count=count)
            return view(conn)
        return self._mutate(actor, operation_id, "plan", {"graph": entries}, authorize, effect, lambda conn, old: view(conn))

    def ready(self, actor, page_id=None, limit=50):
        self._user(actor)
        _integer(limit, 1, 100, "limit")
        if page_id is not None:
            _uuid(page_id, "page_id")
        def read(conn):
            if page_id is not None:
                self._require(conn, actor, "page", page_id)
            tasks = conn.execute("""SELECT * FROM baton.tasks WHERE execution_mode='manual' AND state IN ('queued','claimed')
                AND (%s::uuid IS NULL OR page_id=%s::uuid) ORDER BY created_at,id""", (page_id, page_id)).fetchall()
            live_scopes = [row["scopes"] for row in conn.execute("SELECT scopes FROM baton.x_claim WHERE lease_expires_at>clock_timestamp()")]
            result = []
            for task in tasks:
                if task["assignee"] is not None and task["assignee"] != actor.principal:
                    continue
                if self._role(conn, actor, "page", str(task["page_id"])) != "edit":
                    continue
                view = self._task_view(conn, actor, str(task["id"]))
                if view["state"] != "queued" or task["dispatch_started_at"] is not None:
                    continue
                try:
                    satisfied = self._dependencies_satisfied(conn, actor, str(task["id"]))
                except StoreError as exc:
                    if exc.code == "forbidden":
                        continue
                    raise
                if not satisfied or view["work_face_missing"]:
                    continue
                if any(scopes_overlap(task["scopes"], scopes) for scopes in live_scopes):
                    continue
                result.append(view)
                if len(result) == limit:
                    break
            return result
        return self._read(actor, read)

    @staticmethod
    def _holds_prep_for(conn, actor, task_id):
        return conn.execute("""SELECT 1 FROM baton.task_dependencies e JOIN baton.tasks p ON p.id=e.depends_on
            JOIN baton.x_claim c ON c.task_id=p.id
            WHERE e.task_id=%s AND p.task_kind='prep' AND p.state='claimed' AND c.epoch=p.epoch
            AND c.holder=%s AND c.holder_account=%s AND c.holder_kind=%s AND c.lease_expires_at>clock_timestamp() LIMIT 1""",
            (task_id, actor.principal, actor.account, actor.kind)).fetchone() is not None

    def set_work_face(self, actor, operation_id, task_id, work_face, base_commit=None):
        face = normalize_work_face(work_face)
        if base_commit is not None:
            _text(base_commit, 200, field="base_commit")
        def authorize(conn):
            task = self._task_for(conn, actor, task_id, "edit")
            if (task["owner"] == actor.principal and task["owner_account"] == actor.account) or actor.principal in task["reviewers"]:
                return
            if not self._holds_prep_for(conn, actor, task_id):
                raise StoreError("forbidden")
        def view(conn):
            return self._task_view(conn, actor, task_id)
        def effect(conn):
            task = self._row(conn, "tasks", task_id)
            self._require_unstarted(task)
            if base_commit is not None and task["base_commit"] is not None and task["base_commit"] != base_commit:
                raise StoreError("operation_conflict")
            conn.execute("UPDATE baton.tasks SET work_face=%s,base_commit=COALESCE(base_commit,%s) WHERE id=%s", (Jsonb(face), base_commit, task_id))
            self._task_audit(conn, actor, operation_id, "task.work_face_set", task, work_face_keys=sorted(face),
                             work_face_sha256=hashlib.sha256(_canonical(face).encode()).hexdigest())
            return view(conn)
        return self._mutate(actor, operation_id, "set_work_face", {"task_id": task_id, "work_face": face, "base_commit": base_commit},
                            authorize, effect, lambda conn, old: view(conn))

    def _task_audit(self, conn, actor, operation_id, event, task, **metadata):
        page = self._row(conn, "pages", str(task["page_id"]))
        self._audit(conn, actor, operation_id, event, space_id=page["space_id"], task_id=task["id"],
                    requested_by=task["owner"], thread_id=task["id"], **metadata)

    def _notify_task(self, conn, actor, operation_id, task, phase, body, recipient=None):
        row = self._comment(conn, actor, operation_id, str(task["page_id"]), body, str(task["id"]),
                            subject=f'[{phase}] {task["id"]}', requested_by=task["owner"])
        conn.execute("INSERT INTO baton.mentions(id,comment_id,target_type,target_id,action_requested,requested_by) VALUES (%s,%s,'agent',%s,false,%s)",
                     (uuid.uuid4(), row["id"], recipient or task["owner"], task["owner"]))
        self._task_audit(conn, actor, operation_id, "thread." + phase, task)

    def _default_page(self, conn, actor, operation_id):
        space = conn.execute("SELECT * FROM baton.spaces WHERE default_for=%s", (actor.account,)).fetchone()
        if not space:
            space_id = uuid.uuid5(uuid.NAMESPACE_URL, "baton/default-space/" + actor.account)
            space = conn.execute("""INSERT INTO baton.spaces(id,name,owner_account,owner_principal,owner_kind,default_for)
                VALUES (%s,'Tasks',%s,%s,%s,%s) RETURNING *""", (space_id, actor.account, actor.principal, actor.kind, actor.account)).fetchone()
            self._audit(conn, actor, operation_id, "space.created", space_id=space_id)
        page_id = uuid.uuid5(uuid.NAMESPACE_URL, "baton/default-page/" + actor.account)
        if not conn.execute("SELECT 1 FROM baton.pages WHERE id=%s", (page_id,)).fetchone():
            conn.execute("INSERT INTO baton.pages(id,space_id,owner_account,title) VALUES (%s,%s,%s,'Tasks')", (page_id, space["id"], actor.account))
            self._audit(conn, actor, operation_id, "page.created", space_id=space["id"], page_id=page_id, revision=1)
        return str(page_id)

    def _new_task(self, conn, actor, operation_id, page_id, prompt, scopes, mode, reviewers,
                  input_hash, idempotency_key=None, base_commit=None, context=None, evidence=None, assignee=None, depends_on=None,
                  work_face=None, require_work_face=False, task_kind="construction",
                  priority="normal", escalate_to=None, fallback_assignee=None, approvals_required=None):
        if assignee is not None:
            if not isinstance(assignee, Actor):
                raise StoreError("invalid")
            self._require(conn, assignee, "page", page_id, "edit", hidden="forbidden")
        task_id = uuid.uuid4()
        block_id = uuid.uuid4()
        content = {"task_id": str(task_id), "prompt": prompt, "execution_mode": mode}
        position = conn.execute("SELECT COALESCE(MAX(position),-1)+1 AS n FROM baton.blocks WHERE page_id=%s", (page_id,)).fetchone()["n"]
        block = conn.execute("""INSERT INTO baton.blocks(id,page_id,kind,content,position,author_principal,author_account,author_kind)
            VALUES (%s,%s,'task',%s,%s,%s,%s,%s) RETURNING *""", (block_id, page_id, Jsonb(content), position, actor.principal, actor.account, actor.kind)).fetchone()
        self._revision(conn, actor, operation_id, block, content)
        conn.execute("UPDATE baton.pages SET revision=revision+1 WHERE id=%s", (page_id,))
        task = conn.execute("""INSERT INTO baton.tasks(id,page_id,block_id,owner,owner_account,owner_kind,assignee,idempotency_key,input_hash,prompt,
            execution_mode,gate_mode,reviewers,scopes,base_commit,context,evidence,work_face,require_work_face,task_kind,
            priority,respond_by,escalate_to,fallback_assignee,approvals_required,approvals_override_root)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,clock_timestamp()+%s*interval '1 minute',%s,%s,%s,%s) RETURNING *""",
            (task_id, page_id, block_id, actor.principal, actor.account, actor.kind, assignee.principal if assignee else None,
             idempotency_key, input_hash, prompt, mode, "reviewer", Jsonb(reviewers),
             Jsonb(scopes), base_commit, Jsonb(_items(context)), Jsonb(_items(evidence)),
             None if work_face is None else Jsonb(work_face), require_work_face, task_kind,
             priority, self.response_minutes[priority] if assignee else None, escalate_to or actor.principal, fallback_assignee,
             approvals_required, self._root_of_page(conn, actor, page_id))).fetchone()
        dependency_count = self._add_dependencies(conn, str(task_id), depends_on or [])
        self._require_acyclic(conn, str(task_id))
        self._task_audit(conn, actor, operation_id, "task.submitted", task, input_sha256=hashlib.sha256(prompt.encode()).hexdigest(), execution_mode=mode)
        if dependency_count:
            self._task_audit(conn, actor, operation_id, "task.dependencies_added", task, count=dependency_count)
        self._notify_task(conn, actor, operation_id, task, "submitted", prompt, assignee.principal if assignee else None)
        if assignee:
            self._task_audit(conn, actor, operation_id, "task.assigned", task)
            self._enqueue(conn, actor, "task.assigned", "task", task_id, page_id, [self._party(assignee)], task_id)
        return self._task_view(conn, actor, str(task_id))

    @staticmethod
    def _is_root(space, principal, account, kind):
        """The root approver is one exact identity. A space with no recorded owner principal has no root."""
        return (space["owner_principal"] is not None
                and (principal, account, kind) == (space["owner_principal"], space["owner_account"], space["owner_kind"]))

    def _root_of_page(self, conn, actor, page_id):
        page = self._row(conn, "pages", str(page_id))
        return self._is_root(self._row(conn, "spaces", str(page["space_id"])), actor.principal, actor.account, actor.kind)

    @staticmethod
    def _work_face_args(work_face, require_work_face, task_kind):
        # Defaults are omitted so digests already persisted for older requests stay valid.
        args = {}
        if work_face is not None:
            args["work_face"] = work_face
        if require_work_face:
            args["require_work_face"] = True
        if task_kind != "construction":
            args["task_kind"] = task_kind
        return args

    def _reviewer_identity(self, name, actor):
        """The Actor a reviewer name denotes: the caller when it is them, otherwise the directory."""
        if name == actor.principal:
            return actor
        if not self.directory:
            return None
        try:
            who = self.directory(name)
        except Exception:
            return None
        return who if isinstance(who, Actor) and who.principal == name else None

    def _reviewer_can_approve(self, conn, space, task, name, actor):
        """True when this reviewer is the space root or holds a covering approver delegation right now."""
        who = self._reviewer_identity(name, actor)
        if who is not None and self._is_root(space, who.principal, who.account, who.kind):
            return True
        if who is not None:
            try:
                self._approver_chain(conn, who, task)
                return True
            except StoreError as exc:
                if exc.code != "delegation_required":
                    raise
                return False
        if space["owner_principal"] == name:
            return True
        rows = conn.execute("""SELECT DISTINCT delegate_account, delegate_kind FROM baton.x_delegation
            WHERE delegate=%s AND scope=%s AND revoked_at IS NULL
            AND (expires_at IS NULL OR expires_at>clock_timestamp())""", (name, APPROVE)).fetchall()
        for row in rows:
            try:
                candidate = Actor(name, row["delegate_account"], row["delegate_kind"])
            except StoreError:
                continue
            try:
                self._approver_chain(conn, candidate, task)
                return True
            except StoreError as exc:
                if exc.code != "delegation_required":
                    raise
        return False

    def _attach_reviewer_warnings(self, actor, result):
        """Warn, without failing, when a configured reviewer cannot approve. Recomputed on every return, including replay."""
        if not isinstance(result, dict) or not result.get("task_id"):
            return result
        task_id = result["task_id"]

        def read(conn):
            task = self._row(conn, "tasks", task_id)
            page = self._row(conn, "pages", str(task["page_id"]))
            space = self._row(conn, "spaces", str(page["space_id"]))
            warnings, seen = [], set()
            for name in task["reviewers"] or []:
                if not isinstance(name, str) or name in seen:
                    continue
                seen.add(name)
                if self._reviewer_can_approve(conn, space, task, name, actor):
                    continue
                warnings.append("reviewer %s currently cannot approve (no delegation)" % name)
            return warnings

        warnings = self._read(actor, read)
        cleaned = {key: value for key, value in result.items() if key != "warnings"}
        if warnings:
            cleaned["warnings"] = warnings
        return cleaned

    def submit_task(self, actor, operation_id, idempotency_key, prompt, page_id=None, scopes=None, assignee=None, depends_on=None,
                    work_face=None, require_work_face=False, task_kind="construction",
                    priority="normal", escalate_to=None, fallback_assignee=None, approvals_required=None, reviewers=None):
        _identifier(idempotency_key, "idempotency_key")
        _text(prompt, field="prompt")
        normalized = normalize_scopes(scopes)
        stored = _stored_scopes(scopes, normalized)
        dependencies = _dependency_ids(depends_on)
        face, require_work_face, task_kind = _work_face_request(work_face, require_work_face, task_kind)
        if assignee is not None and not isinstance(assignee, Actor):
            raise StoreError("invalid")
        if assignee is not None and assignee.principal == actor.principal:
            raise StoreError("invalid", 'assignee_principal', 'principal different from the requester')
        priority, escalate_to, fallback_assignee, response_args = _response_request(priority, escalate_to, fallback_assignee, actor.principal, assignee)
        approvals_required = _approvals_request(approvals_required)
        if reviewers is not None:
            if not isinstance(reviewers, list) or not reviewers or len(reviewers) > 100:
                raise StoreError("invalid", "reviewers", "array of 1-100 principal identifiers")
            for reviewer in reviewers:
                _identifier(reviewer, "reviewers")
        args = {"idempotency_key": idempotency_key, "prompt": prompt, "page_id": page_id, "scopes": _request_scopes(normalized),
                "assignee": asdict(assignee) if assignee else None}
        # Preserve v1 hashes for already-persisted submissions without dependencies.
        # Omitted reviewers stay out of the digest too: the default remains the requester.
        if dependencies:
            args["depends_on"] = dependencies
        args.update(self._work_face_args(face, require_work_face, task_kind))
        args.update(response_args)
        if approvals_required is not None:
            args["approvals_required"] = approvals_required
        if reviewers is not None:
            args["reviewers"] = reviewers
        digest = hashlib.sha256(_canonical(args).encode()).hexdigest()
        def authorize(conn):
            self._user(actor)
            if page_id:
                self._require(conn, actor, "page", page_id, "edit")
            for dependency in dependencies:
                self._task_for(conn, actor, dependency)
            prior = conn.execute("SELECT * FROM baton.tasks WHERE owner=%s AND idempotency_key=%s", (actor.principal, idempotency_key)).fetchone()
            if prior:
                self._require(conn, actor, "page", str(prior["page_id"]), "edit")
        def effect(conn):
            prior = conn.execute("SELECT * FROM baton.tasks WHERE owner=%s AND idempotency_key=%s", (actor.principal, idempotency_key)).fetchone()
            if prior:
                if prior["input_hash"] != digest:
                    raise StoreError("operation_conflict")
                return {**self._task_view(conn, actor, str(prior["id"])), "_business_replay": True}
            chosen = page_id or self._default_page(conn, actor, operation_id)
            return self._new_task(conn, actor, operation_id, chosen, prompt, stored, "manual",
                                  reviewers if reviewers is not None else [actor.principal], digest, idempotency_key=idempotency_key, assignee=assignee, depends_on=dependencies,
                                  work_face=face, require_work_face=require_work_face, task_kind=task_kind,
                                  priority=priority, escalate_to=escalate_to, fallback_assignee=fallback_assignee,
                                  approvals_required=approvals_required)
        def replay(conn, old):
            return self._task_view(conn, actor, old["task_id"])
        return self._attach_reviewer_warnings(actor, self._mutate(actor, operation_id, "submit_task", args, authorize, effect, replay))

    def create_task_block(self, actor, operation_id, page_id, prompt, scopes, reviewers, base_commit=None, context=None, evidence=None, assignee=None, depends_on=None,
                          work_face=None, require_work_face=False, task_kind="construction",
                          priority="normal", escalate_to=None, fallback_assignee=None, approvals_required=None):
        _text(prompt, field="prompt")
        normalized = normalize_scopes(scopes)
        stored = _stored_scopes(scopes, normalized)
        dependencies = _dependency_ids(depends_on)
        face, require_work_face, task_kind = _work_face_request(work_face, require_work_face, task_kind)
        if not isinstance(reviewers, list) or not reviewers or len(reviewers) > 100:
            raise StoreError("invalid", 'reviewers', 'array of 1-100 principal identifiers')
        for reviewer in reviewers:
            _identifier(reviewer, "reviewers")
        if base_commit is not None:
            _text(base_commit, 200, field="base_commit")
        _items(context)
        _items(evidence)
        if assignee is not None and not isinstance(assignee, Actor):
            raise StoreError("invalid")
        priority, escalate_to, fallback_assignee, response_args = _response_request(priority, escalate_to, fallback_assignee, actor.principal, assignee)
        approvals_required = _approvals_request(approvals_required)
        args = {"page_id": page_id, "prompt": prompt, "scopes": _request_scopes(normalized), "reviewers": reviewers,
                "base_commit": base_commit, "context": context, "evidence": evidence, "assignee": asdict(assignee) if assignee else None}
        if dependencies:
            args["depends_on"] = dependencies
        args.update(self._work_face_args(face, require_work_face, task_kind))
        args.update(response_args)
        if approvals_required is not None:
            args["approvals_required"] = approvals_required
        digest = hashlib.sha256(_canonical(args).encode()).hexdigest()
        def authorize(conn):
            self._require(conn, actor, "page", page_id, "edit")
            for dependency in dependencies:
                self._task_for(conn, actor, dependency)
        return self._mutate(actor, operation_id, "create_task_block", args,
                            authorize,
                            lambda conn: self._new_task(conn, actor, operation_id, page_id, prompt, stored, "manual", reviewers, digest,
                                                        base_commit=base_commit, context=context, evidence=evidence, assignee=assignee, depends_on=dependencies,
                                                        work_face=face, require_work_face=require_work_face, task_kind=task_kind,
                                                        priority=priority, escalate_to=escalate_to, fallback_assignee=fallback_assignee,
                                                        approvals_required=approvals_required),
                            lambda conn, old: self._task_view(conn, actor, old["task_id"]))

    def get_task(self, actor, task_id):
        def read(conn):
            self._task_for(conn, actor, task_id)
            return self._task_view(conn, actor, task_id)
        return self._read(actor, read)

    def list_tasks(self, actor, limit=50):
        self._user(actor)
        _integer(limit, 1, 100, "limit")
        def read(conn):
            rows = conn.execute("SELECT * FROM baton.tasks WHERE owner=%s ORDER BY created_at DESC,id DESC", (actor.principal,)).fetchall()
            return [self._task_view(conn, actor, str(row["id"])) for row in rows if self._role(conn, actor, "page", str(row["page_id"]))][:limit]
        return self._read(actor, read)

    def _expire(self, conn, operation_id):
        rows = conn.execute("""SELECT t.* FROM baton.tasks t JOIN baton.x_claim c ON c.task_id=t.id
            WHERE c.lease_expires_at<=clock_timestamp()""").fetchall()
        for task in rows:
            conn.execute("DELETE FROM baton.x_claim WHERE task_id=%s", (task["id"],))
            state = "interrupted" if task["execution_mode"] == "codex" and task["dispatch_started_at"] else "queued"
            conn.execute("UPDATE baton.tasks SET state=%s,error=%s WHERE id=%s AND state='claimed'", (state, "dispatch_uncertain" if state == "interrupted" else None, task["id"]))
            self._task_audit(conn, SERVICE_ACTOR, operation_id, "claim.expired", task, epoch=task["epoch"])
            if state == "interrupted":
                self._task_audit(conn, SERVICE_ACTOR, operation_id, "task.interrupted", task, epoch=task["epoch"])
        return len(rows)

    @staticmethod
    def _live(conn, actor, task_id, epoch):
        _integer(epoch, 1, 2**63 - 1, "epoch")
        claim = conn.execute("""SELECT c.* FROM baton.x_claim c JOIN baton.tasks t ON t.id=c.task_id
            WHERE c.task_id=%s AND c.holder=%s AND c.holder_account=%s AND c.holder_kind=%s AND c.epoch=%s
            AND t.epoch=c.epoch AND t.state='claimed' AND c.lease_expires_at>clock_timestamp()""",
            (task_id, actor.principal, actor.account, actor.kind, epoch)).fetchone()
        if not claim:
            raise StoreError("stale_claim")
        return claim

    def _manual(self, conn, actor, task_id, role="edit"):
        task = self._task_for(conn, actor, task_id, role)
        if task["execution_mode"] != "manual":
            raise StoreError("forbidden")
        return task

    @staticmethod
    def _record_holder(conn, task_id, epoch, who):
        conn.execute("INSERT INTO baton.x_claim_holder(task_id,epoch,holder,holder_account) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                     (task_id, epoch, who.principal, who.account))

    def _take_claim(self, conn, actor, operation_id, task, lease_seconds):
        if task["state"] != "queued" or conn.execute("SELECT 1 FROM baton.x_claim WHERE task_id=%s AND lease_expires_at>clock_timestamp()", (task["id"],)).fetchone():
            raise StoreError("scope_conflict")
        if not self._dependencies_satisfied(conn, actor, str(task["id"])):
            raise StoreError("dependency_blocked")
        self._require_work_face(task)
        for other in conn.execute("SELECT scopes FROM baton.x_claim WHERE lease_expires_at>clock_timestamp()"):
            if scopes_overlap(task["scopes"], other["scopes"]):
                raise StoreError("scope_conflict")
        epoch = task["epoch"] + 1
        conn.execute("""INSERT INTO baton.x_claim(task_id,holder,holder_account,holder_kind,epoch,lease_expires_at,scopes)
            VALUES (%s,%s,%s,%s,%s,clock_timestamp()+%s*interval '1 second',%s)""",
            (task["id"], actor.principal, actor.account, actor.kind, epoch, lease_seconds, Jsonb(task["scopes"])))
        conn.execute("""UPDATE baton.tasks SET state='claimed',epoch=%s,result_epoch=NULL,error=NULL,
            blocked_reason=NULL,blocked_by=NULL,blocked_at=NULL WHERE id=%s""", (epoch, task["id"]))
        self._record_holder(conn, task["id"], epoch, actor)
        if task["assignee"] == actor.principal:
            conn.execute("UPDATE baton.tasks SET responded_at=COALESCE(responded_at,clock_timestamp()) WHERE id=%s", (task["id"],))
        self._task_audit(conn, actor, operation_id, "task.claimed", task, epoch=epoch, lease_seconds=lease_seconds)
        return self._task_view(conn, actor, str(task["id"]), include_prompt=True)

    def claim(self, actor, operation_id, task_id, lease_seconds=300):
        _integer(lease_seconds, field="lease_seconds")
        def authorize(conn):
            task = self._manual(conn, actor, task_id)
            self._require_dependency_access(conn, actor, task_id)
            if task["assignee"] is not None and task["assignee"] != actor.principal:
                raise StoreError("forbidden")
        def effect(conn):
            self._expire(conn, operation_id)
            return self._take_claim(conn, actor, operation_id, self._row(conn, "tasks", task_id), lease_seconds)
        def replay(conn, old):
            self._live(conn, actor, task_id, old["epoch"])
            if not self._dependencies_satisfied(conn, actor, task_id):
                raise StoreError("dependency_blocked")
            self._require_work_face(self._row(conn, "tasks", task_id))
            return self._task_view(conn, actor, task_id, include_prompt=True)
        return self._mutate(actor, operation_id, "claim", {"task_id": task_id, "lease_seconds": lease_seconds}, authorize, effect, replay)

    def renew(self, actor, operation_id, task_id, epoch, lease_seconds=300):
        _integer(lease_seconds, field="lease_seconds")
        def effect(conn):
            self._live(conn, actor, task_id, epoch)
            conn.execute("UPDATE baton.x_claim SET lease_expires_at=clock_timestamp()+%s*interval '1 second' WHERE task_id=%s", (lease_seconds, task_id))
            self._task_audit(conn, actor, operation_id, "claim.renewed", self._row(conn, "tasks", task_id), epoch=epoch, lease_seconds=lease_seconds)
            return self._task_view(conn, actor, task_id)
        def replay(conn, old):
            self._live(conn, actor, task_id, epoch)
            return self._task_view(conn, actor, task_id)
        return self._mutate(actor, operation_id, "renew", {"task_id": task_id, "epoch": epoch, "lease_seconds": lease_seconds},
                            lambda conn: self._manual(conn, actor, task_id), effect, replay)

    def handoff(self, actor, operation_id, task_id, epoch, target, base_commit=None, context=None, evidence=None, lease_seconds=300):
        _integer(lease_seconds, field="lease_seconds")
        if not isinstance(target, Actor) or target.principal == actor.principal:
            raise StoreError("invalid")
        _items(context)
        _items(evidence)
        if base_commit is not None:
            _text(base_commit, 200, field="base_commit")
        def authorize(conn):
            task = self._manual(conn, actor, task_id)
            self._require(conn, target, "page", str(task["page_id"]), "edit", hidden="forbidden")
            self._require_dependency_access(conn, target, task_id)
        def effect(conn):
            self._live(conn, actor, task_id, epoch)
            task = self._row(conn, "tasks", task_id)
            if base_commit is not None and base_commit != task["base_commit"]:
                raise StoreError("revision_conflict")
            base = task["base_commit"]
            contexts, evidence_items = task["context"] + _items(context), task["evidence"] + _items(evidence)
            conn.execute("""UPDATE baton.x_claim SET holder=%s,holder_account=%s,holder_kind=%s,epoch=epoch+1,
                lease_expires_at=clock_timestamp()+%s*interval '1 second' WHERE task_id=%s""", (target.principal, target.account, target.kind, lease_seconds, task_id))
            conn.execute("UPDATE baton.tasks SET epoch=epoch+1,assignee=%s,base_commit=%s,context=%s,evidence=%s WHERE id=%s",
                         (target.principal, base, Jsonb(contexts), Jsonb(evidence_items), task_id))
            if task["assignee"] != target.principal:
                self._start_clock(conn, task)
            # The previous holder contributed to the work, so neither side may approve this epoch's result.
            self._record_holder(conn, task["id"], epoch + 1, actor)
            self._record_holder(conn, task["id"], epoch + 1, target)
            conn.execute("INSERT INTO baton.x_handoff(id,task_id,from_holder,to_holder,old_epoch,new_epoch,base_commit,context,evidence) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                         (uuid.uuid4(), task_id, actor.principal, target.principal, epoch, epoch + 1, base, Jsonb(contexts), Jsonb(evidence_items)))
            self._task_audit(conn, actor, operation_id, "task.handed_off", task, old_epoch=epoch, epoch=epoch + 1, lease_seconds=lease_seconds)
            self._notify_task(conn, actor, operation_id, task, "assigned", "Task handed off", target.principal)
            self._enqueue(conn, actor, "task.handed_off", "task", task["id"], task["page_id"], [self._party(target)], task["id"])
            return self._task_view(conn, actor, task_id, include_prompt=True)
        def replay(conn, old):
            self._live(conn, target, task_id, epoch + 1)
            return self._task_view(conn, actor, task_id, include_prompt=True)
        return self._mutate(actor, operation_id, "handoff", {"task_id": task_id, "epoch": epoch, "target": asdict(target), "base_commit": base_commit,
                            "context": context, "evidence": evidence, "lease_seconds": lease_seconds}, authorize, effect, replay)

    def _start_clock(self, conn, task):
        """Start a new response window for the task's current assignee, using database time."""
        conn.execute("""UPDATE baton.tasks SET respond_by=clock_timestamp()+%s*interval '1 minute',responded_at=NULL,escalated_at=NULL
            WHERE id=%s""", (self.response_minutes[task["priority"]], task["id"]))

    @staticmethod
    def _record_response(conn, actor, task):
        """The first comment in the task thread by its assignee or live holder counts as the response."""
        if task["respond_by"] is None or task["responded_at"] is not None:
            return
        responded = actor.principal == task["assignee"] or conn.execute("""SELECT 1 FROM baton.x_claim WHERE task_id=%s AND holder=%s
            AND holder_account=%s AND lease_expires_at>clock_timestamp()""", (task["id"], actor.principal, actor.account)).fetchone()
        if responded:
            conn.execute("UPDATE baton.tasks SET responded_at=clock_timestamp() WHERE id=%s AND responded_at IS NULL", (task["id"],))

    def _assign(self, conn, actor, operation_id, task, principal, account, event="task.assigned", default_reviewer=None, body="Task assigned", **metadata):
        """Move an unclaimed task to a manual assignee. Shared by assign_task and the escalation runner."""
        reviewers = task["reviewers"] or [default_reviewer or actor.principal]
        conn.execute("""UPDATE baton.tasks SET assignee=%s,execution_mode='manual',gate_mode='reviewer',reviewers=%s,
            blocked_reason=NULL,blocked_by=NULL,blocked_at=NULL WHERE id=%s""", (principal, Jsonb(reviewers), task["id"]))
        if task["assignee"] != principal:
            self._start_clock(conn, task)
        self._task_audit(conn, actor, operation_id, event, task, **metadata)
        self._notify_task(conn, actor, operation_id, task, "assigned", body, principal)
        self._enqueue(conn, actor, "task.assigned", "task", task["id"], task["page_id"], [(principal, account)], task["id"])

    def assign_task(self, actor, operation_id, task_id, target):
        if not isinstance(target, Actor):
            raise StoreError("invalid")
        def authorize(conn):
            task = self._task_for(conn, actor, task_id, "edit")
            self._require(conn, target, "page", str(task["page_id"]), "edit", hidden="forbidden")
        def effect(conn):
            self._expire(conn, operation_id)
            task = self._row(conn, "tasks", task_id)
            if task["state"] != "queued" or task["dispatch_started_at"] is not None:
                raise StoreError("stale_claim")
            self._assign(conn, actor, operation_id, task, target.principal, target.account)
            return self._task_view(conn, actor, task_id)
        def replay(conn, old):
            task = self._row(conn, "tasks", task_id)
            if task["assignee"] != target.principal:
                raise StoreError("stale_claim")
            return self._task_view(conn, actor, task_id)
        return self._mutate(actor, operation_id, "assign_task", {"task_id": task_id, "target": asdict(target)}, authorize, effect, replay)

    def mark_blocked(self, actor, operation_id, task_id, reason_code, note=None):
        """An assignee or reviewer who cannot claim says why, in the thread and to the owner.

        It changes no ownership and claims nothing; the marker lasts until the task is claimed or reassigned.
        """
        if not isinstance(reason_code, str) or reason_code not in BLOCK_REASONS:
            raise StoreError("invalid", "reason_code", "one of " + ", ".join(BLOCK_REASONS))
        if note is not None:
            _text(note, 2000, field="note")
        def authorize(conn):
            task = self._manual(conn, actor, task_id, "comment")
            if task["assignee"] != actor.principal and actor.principal not in task["reviewers"]:
                raise StoreError("forbidden")
        def view(conn):
            return self._task_view(conn, actor, task_id)
        def effect(conn):
            task = self._row(conn, "tasks", task_id)
            state = self._task_view(conn, actor, task_id)
            if state["state"] not in {"queued", "claimed"} or (state["holder"] == actor.principal and state["lease_valid"]):
                raise StoreError("stale_claim")
            conn.execute("UPDATE baton.tasks SET blocked_reason=%s,blocked_by=%s,blocked_at=clock_timestamp() WHERE id=%s",
                         (reason_code, actor.principal, task_id))
            self._task_audit(conn, actor, operation_id, "task.blocked", task, reason_code=reason_code)
            self._notify_task(conn, actor, operation_id, task, "blocked", "[blocked] " + reason_code + (": " + note if note else ""), task["owner"])
            self._record_response(conn, actor, task)
            self._enqueue(conn, actor, "task.blocked", "task", task["id"], task["page_id"], [self._task_parties(conn, task)["owner"]], task["id"])
            return view(conn)
        return self._mutate(actor, operation_id, "mark_blocked", {"task_id": task_id, "reason_code": reason_code, "note": note},
                            authorize, effect, lambda conn, old: view(conn))

    def set_priority(self, actor, operation_id, task_id, priority):
        priority = _priority(priority)
        def authorize(conn):
            task = self._task_for(conn, actor, task_id, "edit")
            if not (task["owner"] == actor.principal and task["owner_account"] == actor.account) and actor.principal not in task["reviewers"]:
                raise StoreError("forbidden")
        def effect(conn):
            task = self._row(conn, "tasks", task_id)
            if task["priority"] != priority:
                # The window keeps its original start: only its length follows the new priority.
                conn.execute("""UPDATE baton.tasks SET priority=%s,respond_by=CASE WHEN respond_by IS NOT NULL AND responded_at IS NULL
                    THEN respond_by+(%s-%s)*interval '1 minute' ELSE respond_by END WHERE id=%s""",
                    (priority, self.response_minutes[priority], self.response_minutes[task["priority"]], task_id))
                self._task_audit(conn, actor, operation_id, "task.priority_set", task, priority=priority)
            return self._task_view(conn, actor, task_id)
        return self._mutate(actor, operation_id, "set_priority", {"task_id": task_id, "priority": priority}, authorize, effect,
                            lambda conn, old: self._task_view(conn, actor, task_id))

    def migrate_manual_root_scopes(self, actor, dry_run=False):
        """Operator migration: manual tasks still carrying the legacy root placeholder get the explicit "none" scope.

        Legacy automatic (codex) tasks keep the root placeholder, and a task with a live claim is
        never touched, so a held scope reservation cannot change under its holder. One audit event per task.
        """
        self._service(actor)
        if type(dry_run) is not bool:
            raise StoreError("invalid", "dry_run", "boolean")
        operation_id = uuid.uuid4()
        with self._transaction(write=not dry_run) as conn:
            rows = conn.execute("""SELECT t.*,EXISTS (SELECT 1 FROM baton.x_claim c WHERE c.task_id=t.id
                AND c.lease_expires_at>clock_timestamp()) AS claimed FROM baton.tasks t
                WHERE t.execution_mode='manual' AND t.scopes=%s ORDER BY t.created_at,t.id""", (Jsonb(ROOT_SCOPES),)).fetchall()
            eligible = [row for row in rows if not row["claimed"]]
            if not dry_run:
                for task in eligible:
                    conn.execute("UPDATE baton.tasks SET scopes=%s WHERE id=%s", (Jsonb(NONE_SCOPES), task["id"]))
                    self._task_audit(conn, actor, operation_id, "task.scopes_migrated", task, execution_mode="manual")
            return {"dry_run": dry_run, "matched": len(eligible), "migrated": 0 if dry_run else len(eligible),
                    "skipped_claimed": len(rows) - len(eligible), "task_ids": [str(task["id"]) for task in eligible]}

    def escalate_overdue(self, actor, limit=ESCALATION_BATCH):
        """One pass over overdue unanswered tasks; safe to run from several bridge replicas at once.

        No operations row is written: escalated_at makes every transition happen once, and the pass has no caller to replay to.
        """
        self._service(actor)
        _integer(limit, 1, 1000)
        operation_id = uuid.uuid4()
        escalated, reassigned = [], []
        try:
            with self._transaction(write=True, single_flight=ESCALATION_LOCK_ID) as conn:
                self._expire(conn, operation_id)
                due = conn.execute("""SELECT * FROM baton.tasks WHERE execution_mode='manual' AND state='queued' AND assignee IS NOT NULL
                    AND respond_by<clock_timestamp() AND responded_at IS NULL AND escalated_at IS NULL
                    ORDER BY respond_by,id LIMIT %s FOR UPDATE""", (limit,)).fetchall()
                for task in due:
                    self._escalate(conn, operation_id, task, escalated, reassigned)
                self._notify_expiring_claims(conn)
        except _Busy:
            return {"skipped": True, "escalated": [], "reassigned": []}
        return {"skipped": False, "escalated": escalated, "reassigned": reassigned}

    def _notify_expiring_claims(self, conn):
        """One claim.expiring to the holder when the live lease is inside its warning window and this grant was not renewed.

        Runs inside escalate_overdue's transaction, so the existing response-SLA sweep is the only timer. A grant is the
        latest task.claimed, claim.renewed or task.handed_off audit for this epoch; one notification is written per grant.
        """
        claims = conn.execute("""SELECT c.task_id, c.holder, c.holder_account, c.epoch, t.page_id,
            EXTRACT(EPOCH FROM (c.lease_expires_at - clock_timestamp())) AS remaining
            FROM baton.x_claim c JOIN baton.tasks t ON t.id=c.task_id
            WHERE t.state='claimed' AND c.lease_expires_at>clock_timestamp()
            ORDER BY c.lease_expires_at, c.task_id""").fetchall()
        for claim in claims:
            grant = conn.execute("""SELECT time, metadata FROM baton.x_audit
                WHERE task_id=%s AND event IN ('task.claimed','claim.renewed','task.handed_off')
                AND metadata->>'epoch'=%s ORDER BY time DESC LIMIT 1""",
                (claim["task_id"], str(claim["epoch"]))).fetchone()
            lease, granted_at = None, None
            if grant is not None:
                granted_at = grant["time"]
                raw = grant["metadata"].get("lease_seconds") if isinstance(grant["metadata"], dict) else None
                if type(raw) is int and raw > 0:
                    lease = raw
            window = CLAIM_EXPIRING_MIN_SECONDS if lease is None else max(CLAIM_EXPIRING_MIN_SECONDS, CLAIM_EXPIRING_FRACTION * lease)
            if float(claim["remaining"]) >= window:
                continue
            if granted_at is not None:
                already = conn.execute("""SELECT 1 FROM baton.x_notify WHERE event='claim.expiring' AND object_id=%s
                    AND recipient=%s AND created_at>=%s""", (claim["task_id"], claim["holder"], granted_at)).fetchone()
            else:
                already = conn.execute("""SELECT 1 FROM baton.x_notify WHERE event='claim.expiring' AND object_id=%s
                    AND recipient=%s""", (claim["task_id"], claim["holder"])).fetchone()
            if already:
                continue
            self._enqueue(conn, SERVICE_ACTOR, "claim.expiring", "task", claim["task_id"], claim["page_id"],
                          [(claim["holder"], claim["holder_account"])], claim["task_id"])

    def _escalate(self, conn, operation_id, task, escalated, reassigned):
        target = task["escalate_to"] or task["owner"]
        conn.execute("UPDATE baton.tasks SET escalated_at=clock_timestamp() WHERE id=%s", (task["id"],))
        owner = (task["owner"], task["owner_account"])
        responsible = owner if target == task["owner"] else self._directory_party(target)
        self._task_audit(conn, SERVICE_ACTOR, operation_id, "task.escalated", task, priority=task["priority"], escalate_to=target)
        self._enqueue(conn, SERVICE_ACTOR, "task.overdue", "task", task["id"], task["page_id"], [responsible, owner], task["id"])
        escalated.append(str(task["id"]))
        fallback = task["fallback_assignee"]
        if not fallback or fallback == task["assignee"]:
            return
        party = self._directory_party(fallback)
        if party is None or self._account_role(conn, party[1], "page", str(task["page_id"])) != "edit":
            return
        if conn.execute("SELECT 1 FROM baton.x_claim WHERE task_id=%s AND lease_expires_at>clock_timestamp()", (task["id"],)).fetchone():
            return
        if task["dispatch_started_at"] is not None:
            return
        self._assign(conn, SERVICE_ACTOR, operation_id, task, party[0], party[1], event="task.reassigned",
                     default_reviewer=task["owner"], body="Task reassigned after the response deadline passed",
                     from_assignee=task["assignee"], to_assignee=party[0])
        reassigned.append(str(task["id"]))

    def submit_result(self, actor, operation_id, task_id, epoch, result, evidence=None):
        _text(result, 32000, field="result")
        _items(evidence)
        def effect(conn):
            self._live(conn, actor, task_id, epoch)
            task = self._row(conn, "tasks", task_id)
            conn.execute("""UPDATE baton.tasks SET state='awaiting_gate',result=%s,result_epoch=%s,result_author=%s,
                result_author_account=%s,evidence=%s WHERE id=%s""", (result, epoch, actor.principal, actor.account, Jsonb(task["evidence"] + _items(evidence)), task_id))
            conn.execute("DELETE FROM baton.x_claim WHERE task_id=%s", (task_id,))
            self._task_audit(conn, actor, operation_id, "task.result_submitted", task, epoch=epoch, output_sha256=hashlib.sha256(result.encode()).hexdigest())
            # This is the holder's reply in the original thread, explicitly a
            # candidate rather than a completed/gate-approved notification.
            self._notify_task(conn, actor, operation_id, task, "result_submitted", result)
            self._enqueue(conn, actor, "task.awaiting_gate", "task", task["id"], task["page_id"],
                          self._task_parties(conn, task)["reviewers"], task["id"], result_epoch=epoch, result_author=actor.principal)
            return self._task_view(conn, actor, task_id)
        def replay(conn, old):
            task = self._row(conn, "tasks", task_id)
            if task["epoch"] != epoch or task["result_epoch"] != epoch or task["state"] not in {"awaiting_gate", "done"}:
                raise StoreError("stale_claim")
            return self._task_view(conn, actor, task_id)
        return self._mutate(actor, operation_id, "submit_result", {"task_id": task_id, "epoch": epoch, "result": result, "evidence": evidence},
                            lambda conn: self._manual(conn, actor, task_id), effect, replay)

    @staticmethod
    def _delegation_chain(conn, leaf):
        """Delegation rows from the leaf up to its root, or None when the chain is missing or too deep."""
        chain = [leaf]
        while chain[-1]["parent_id"] is not None:
            if len(chain) >= DELEGATION_DEPTH:
                return None
            parent = conn.execute("SELECT * FROM baton.x_delegation WHERE id=%s", (chain[-1]["parent_id"],)).fetchone()
            if not parent:
                return None
            chain.append(parent)
        return chain

    @staticmethod
    def _chain_active(conn, chain):
        """Every link is live, rooted at a space owner, and stays within its parent (grantor, may_delegate, space, expiry)."""
        if chain is None:
            return False
        now = conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
        for index, row in enumerate(chain):
            if row["scope"] != APPROVE or row["revoked_at"] is not None or (row["expires_at"] is not None and row["expires_at"] <= now):
                return False
            if index + 1 == len(chain):
                if row["parent_id"] is not None:
                    return False
                if row["space_id"] is not None:
                    owner = conn.execute("SELECT * FROM baton.spaces WHERE id=%s", (row["space_id"],)).fetchone()
                    if not owner or not SpaceStore._is_root(owner, row["grantor"], row["grantor_account"], row["grantor_kind"]):
                        return False
                continue
            parent = chain[index + 1]
            if (not parent["may_delegate"] or (row["grantor"], row["grantor_account"]) != (parent["delegate"], parent["delegate_account"])
                    or (parent["space_id"] is not None and row["space_id"] != parent["space_id"])
                    or (parent["expires_at"] is not None and (row["expires_at"] is None or row["expires_at"] > parent["expires_at"]))):
                return False
        return True

    def _approver_chain(self, conn, actor, task):
        """The active delegation chain (leaf to root) that lets this non-root principal approve the task, or delegation_required.

        Humans and agents alike need one. Most specific space first. The chain must end at a root granted by the
        owner of the task's space, so a delegation never reaches into a space its root does not own.
        """
        page = self._row(conn, "pages", str(task["page_id"]))
        space = self._row(conn, "spaces", str(page["space_id"]))
        rows = conn.execute("""SELECT * FROM baton.x_delegation WHERE delegate=%s AND delegate_account=%s AND delegate_kind=%s
            AND scope=%s AND (space_id IS NULL OR space_id=%s) AND revoked_at IS NULL
            AND (expires_at IS NULL OR expires_at>clock_timestamp()) ORDER BY (space_id IS NULL),created_at,id""",
            (actor.principal, actor.account, actor.kind, APPROVE, page["space_id"])).fetchall()
        for row in rows:
            chain = self._delegation_chain(conn, row)
            if not self._chain_active(conn, chain) or any(link["space_id"] not in (None, page["space_id"]) for link in chain):
                continue
            if self._is_root(space, chain[-1]["grantor"], chain[-1]["grantor_account"], chain[-1]["grantor_kind"]):
                return chain
        raise StoreError("delegation_required")

    @staticmethod
    def _executors(conn, task, epoch):
        """Principals and accounts that submitted or held the claim for this epoch."""
        principals, accounts = set(), set()
        if task["result_author"]:
            principals.add(task["result_author"])
            accounts.add(task["result_author_account"])
        for row in conn.execute("SELECT holder,holder_account FROM baton.x_claim_holder WHERE task_id=%s AND epoch=%s", (task["id"], epoch)):
            principals.add(row["holder"])
            accounts.add(row["holder_account"])
        return principals, accounts

    def _approval_requirements(self, conn, task):
        """(approvals required, root required) for a task: the strictest matching policy, then the task's own override.

        A task can raise the requirement. Only when its creator was the space owner can it replace (lower) it.
        """
        page = self._row(conn, "pages", str(task["page_id"]))
        required, root_required = 1, False
        for policy in conn.execute("SELECT * FROM baton.x_approval_policy WHERE space_id=%s", (page["space_id"],)):
            if _policy_matches(policy["scope_kind"], policy["scope_value"], task["scopes"]):
                required = max(required, policy["required_approvals"])
                root_required = root_required or policy["root_required"]
        override = task["approvals_required"]
        if override is not None:
            required = override if task["approvals_override_root"] else max(required, override)
        return required, root_required

    @staticmethod
    def _passes(conn, task_id, epoch):
        return conn.execute("""SELECT reviewer,reviewer_account,root FROM baton.x_gate WHERE task_id=%s AND epoch=%s AND decision='pass'
            ORDER BY created_at""", (task_id, epoch)).fetchall()

    @staticmethod
    def _authority(approval):
        """How the approver came to hold authority, in the words clients show: root, a marked self-approval, or a delegation."""
        if approval["root"]:
            return "root (self-approval)" if approval["self_approval"] else "root"
        leaf = approval["chain"][0]
        return f'delegated by {leaf["grantor"]} via {leaf["id"]}'

    def gate(self, actor, operation_id, task_id, expected_epoch, decision, evidence=None, self_approval=False,
             edited_result=None, reason=None):
        _integer(expected_epoch, 1, 2**63 - 1, "expected_epoch")
        if not isinstance(decision, str) or decision not in {"pass", "fail", "edit"}:
            raise StoreError("invalid", "decision", "one of pass, fail, edit")
        if type(self_approval) is not bool:
            raise StoreError("invalid", "self_approval", "boolean")
        if decision == "edit":
            _text(edited_result, 32000, "edited_result")
        elif edited_result is not None:
            raise StoreError("invalid", "edited_result", "omitted unless decision is edit")
        if reason is not None:
            reason = _trimmed(reason, 1000, "reason")
        _items(evidence)
        approval = {}
        def authorize(conn):
            task = self._manual(conn, actor, task_id)
            page = self._row(conn, "pages", str(task["page_id"]))
            root = self._is_root(self._row(conn, "spaces", str(page["space_id"])), actor.principal, actor.account, actor.kind)
            principals, accounts = self._executors(conn, task, expected_epoch)
            executed = actor.principal in principals or actor.account in accounts
            if root:
                # The space owner approves inherently, without a delegation or a place in the reviewers list. It may
                # approve work it submitted or executed only by asking for that explicitly.
                if executed and not self_approval:
                    raise StoreError("forbidden")
                approval.update(root=True, chain=[], self_approval=executed)
                return
            # Everyone else: the configured reviewers decide who is asked, a covering approver delegation decides
            # whether they may approve, and nobody approves work they submitted or executed.
            if actor.principal not in task["reviewers"] or executed:
                raise StoreError("forbidden")
            # This also runs before a replay is served, so a delegation that has since expired or been
            # revoked, anywhere in its chain, refuses the retry.
            approval.update(root=False, chain=self._approver_chain(conn, actor, task), self_approval=False)
        def effect(conn):
            task = self._row(conn, "tasks", task_id)
            if task["state"] != "awaiting_gate" or task["epoch"] != expected_epoch or task["result_epoch"] != expected_epoch:
                raise StoreError("stale_claim")
            chain = approval["chain"]
            leaf = chain[0] if chain else None
            earlier = self._passes(conn, task_id, expected_epoch)
            if decision != "fail" and any(row["reviewer_account"] == actor.account or row["reviewer"] == actor.principal for row in earlier):
                raise StoreError("forbidden")
            edited = decision == "edit"
            original = task["result"]
            if edited and edited_result == original:
                raise StoreError("invalid", "edited_result", "text that differs from the submitted result")
            recorded = "pass" if edited else decision
            conn.execute("""INSERT INTO baton.x_gate(id,task_id,epoch,decision,reviewer,reviewer_account,evidence,delegation_id,grantor,root,self_approval,
                edited,original_result,edited_result,reason) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                         (uuid.uuid4(), task_id, expected_epoch, recorded, actor.principal, actor.account, Jsonb(_items(evidence)),
                          leaf["id"] if leaf else None, leaf["grantor"] if leaf else None, approval["root"], approval["self_approval"],
                          edited, original if edited else None, edited_result if edited else None, reason))
            if edited:
                conn.execute("UPDATE baton.tasks SET result=%s WHERE id=%s", (edited_result, task_id))
            required, root_required = self._approval_requirements(conn, task)
            passed = self._passes(conn, task_id, expected_epoch)
            # Two-party confirmation: enough distinct approvers must pass the same result epoch, and when the root is
            # required it must be one of them. Any fail sends the task back, and a root fail is final.
            complete = recorded == "pass" and len(passed) >= required and (not root_required or any(row["root"] for row in passed))
            conn.execute("UPDATE baton.tasks SET state=%s WHERE id=%s", ("queued" if decision == "fail" else "done" if complete else "awaiting_gate", task_id))
            via = {"delegation_id": leaf["id"], "grantor": leaf["grantor"]} if leaf else {}
            self._task_audit(conn, actor, operation_id, "gate.reviewed", task, epoch=expected_epoch, decision=recorded,
                             chain=[str(link["id"]) for link in chain], self_approval=approval["self_approval"], root=approval["root"],
                             approvals=len(passed), approvals_required=required, **via)
            if edited:
                self._task_audit(conn, actor, operation_id, "gate.result_overridden", task, epoch=expected_epoch, decision="edit",
                                 approver=actor.principal, original_sha256=_sha256(original), edited_sha256=_sha256(edited_result),
                                 **({"reason": reason} if reason else {}), self_approval=approval["self_approval"], root=approval["root"],
                                 chain=[str(link["id"]) for link in chain], **via)
            parties = self._task_parties(conn, task)
            self._enqueue(conn, actor, "gate.decided", "task", task["id"], task["page_id"],
                          [parties["owner"], parties["assignee"], parties["holder"], parties["author"]], task["id"])
            if complete:
                self._task_audit(conn, actor, operation_id, "task.completed", task, epoch=expected_epoch)
                self._notify_task(conn, actor, operation_id, task, "completed", edited_result if edited else task["result"])
            summary = {"decision": decision, "counted_as": recorded, "epoch": expected_epoch, "approver": actor.principal,
                       "approver_account": actor.account, "approver_kind": actor.kind, "root": approval["root"],
                       "self_approval": approval["self_approval"], "authority": self._authority(approval),
                       "delegation_id": leaf["id"] if leaf else None, "grantor": leaf["grantor"] if leaf else None,
                       "edited": edited, "approvals": len(passed), "approvals_required": required, "complete": complete}
            return {**self._task_view(conn, actor, task_id), "gate": summary}
        def replay(conn, old):
            task = self._row(conn, "tasks", task_id)
            if task["epoch"] != expected_epoch or task["result_epoch"] != expected_epoch:
                raise StoreError("stale_claim")
            return {**self._task_view(conn, actor, task_id), **({"gate": old["gate"]} if isinstance(old, dict) and "gate" in old else {})}
        args = {"task_id": task_id, "expected_epoch": expected_epoch, "decision": decision, "evidence": evidence, "self_approval": self_approval}
        # New fields join the request digest only when used, so a retry of an operation recorded before they existed still matches.
        args.update({key: value for key, value in (("edited_result", edited_result), ("reason", reason)) if value is not None})
        return self._mutate(actor, operation_id, "gate", args, authorize, effect, replay)

    def get_claim(self, actor, task_id):
        return self.get_task(actor, task_id)

    def get_scopes(self, actor, task_id):
        def read(conn):
            task = self._task_for(conn, actor, task_id)
            return {"task_id": task_id, "scope_repository": "global", "scopes": task["scopes"]}
        return self._read(actor, read)

    def list_claims(self, actor):
        self._user(actor)
        def read(conn):
            tasks = conn.execute("SELECT t.* FROM baton.tasks t JOIN baton.x_claim c ON t.id=c.task_id WHERE c.lease_expires_at>clock_timestamp() ORDER BY t.id").fetchall()
            return [self._task_view(conn, actor, str(task["id"])) for task in tasks if self._role(conn, actor, "page", str(task["page_id"]))]
        return self._read(actor, read)

    def _delegation_view(self, conn, row):
        """The row plus its chain (delegation ids from this one to the root) and whether the whole chain is active."""
        chain = self._delegation_chain(conn, row)
        view = dict(row)
        view["chain"] = [str(link["id"]) for link in chain] if chain else [str(row["id"])]
        view["active"] = self._chain_active(conn, chain)
        return view

    def _delegation_row(self, conn, delegation_id):
        row = conn.execute("SELECT * FROM baton.x_delegation WHERE id=%s", (_uuid(delegation_id, "delegation_id"),)).fetchone()
        if not row:
            raise StoreError("not_found")
        return self._delegation_view(conn, row)

    def _insert_delegation(self, conn, grantor, request, via, created_by, parent=None):
        delegate, scope, space_id, expires, may_delegate, reason = request
        if space_id is not None:
            self._row(conn, "spaces", space_id)
        if expires is not None and not conn.execute("SELECT %s>clock_timestamp() AS ok", (expires,)).fetchone()["ok"]:
            raise StoreError("invalid", "expires_at", "ISO 8601 timestamp with a UTC offset, in the future")
        identity = uuid.uuid4()
        conn.execute("""INSERT INTO baton.x_delegation(id,grantor,grantor_account,grantor_kind,delegate,delegate_account,delegate_kind,
            scope,space_id,may_delegate,parent_id,reason,created_via,created_by,expires_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                     (identity, grantor.principal, grantor.account, grantor.kind, delegate.principal, delegate.account, delegate.kind,
                      scope, space_id, may_delegate, parent["id"] if parent else None, reason, via, created_by, expires))
        return self._delegation_row(conn, str(identity))

    def _owns(self, conn, who, space_id):
        """Whether this exact identity is the root of the space (of any space when none is named): the authority to grant roots."""
        if space_id is not None:
            return self._is_root(self._row(conn, "spaces", space_id), who.principal, who.account, who.kind)
        return conn.execute("SELECT 1 FROM baton.spaces WHERE owner_principal=%s AND owner_account=%s AND owner_kind=%s LIMIT 1",
                            (who.principal, who.account, who.kind)).fetchone() is not None

    @staticmethod
    def _no_cycle(chain, delegate):
        """A delegation may never point at someone already in the granting chain (a cycle), the root above it included."""
        for link in chain:
            if delegate.principal in {link["grantor"], link["delegate"]}:
                raise StoreError("invalid", "delegate_principal", "a principal not already in the granting chain")

    def _parent_delegation(self, conn, actor, request):
        """The caller's own may_delegate delegation that can contain the requested child, as (row, chain).

        Delegations flow only downward from the root. No such delegation at all is forbidden; one that cannot
        contain the request (wider space, later expiry, a chain at the depth limit, or a cycle) is invalid.
        """
        delegate, _, space_id, expires, _, _ = request
        rows = conn.execute("""SELECT * FROM baton.x_delegation WHERE delegate=%s AND delegate_account=%s AND delegate_kind=%s
            AND scope=%s AND may_delegate AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at>clock_timestamp())
            ORDER BY created_at,id""", (actor.principal, actor.account, actor.kind, APPROVE)).fetchall()
        seen = False
        for row in rows:
            chain = self._delegation_chain(conn, row)
            if not self._chain_active(conn, chain):
                continue
            root_link = chain[-1]
            if space_id is not None and not self._is_root(self._row(conn, "spaces", space_id), root_link["grantor"],
                                                          root_link["grantor_account"], root_link["grantor_kind"]):
                continue
            seen = True
            if (row["space_id"] is None or row["space_id"] == (uuid.UUID(space_id) if space_id else None)) \
                    and (row["expires_at"] is None or (expires is not None and expires <= row["expires_at"])) \
                    and len(chain) < DELEGATION_DEPTH:
                self._no_cycle(chain, delegate)
                return row, chain
        if seen:
            raise StoreError("invalid", "delegation", "within the space, expiry and depth limit of the delegation you hold, and not a cycle")
        raise StoreError("forbidden")

    def delegation_create(self, actor, operation_id, delegate, scope=APPROVE, space_id=None, expires_at=None, may_delegate=False, *, reason):
        """Grant the approver role. The space owner creates a root; a principal holding a may_delegate delegation creates a child."""
        self._user(actor)
        request = _delegation_request(actor, delegate, scope, space_id, expires_at, may_delegate, reason)
        parent = {}
        def authorize(conn):
            self._user(actor)
            if not self._owns(conn, actor, request[2]):
                if request[2] is not None:
                    self._row(conn, "spaces", request[2])
                parent["row"], parent["chain"] = self._parent_delegation(conn, actor, request)
        def effect(conn):
            row = self._insert_delegation(conn, actor, request, "api", actor.principal, parent.get("row"))
            self._audit(conn, actor, operation_id, "delegation.created", space_id=row["space_id"], delegation_id=row["id"],
                        scope=row["scope"], created_via="api", chain=row["chain"])
            return row
        args = {"delegate_principal": delegate.principal, "delegate_account": delegate.account, "delegate_kind": delegate.kind,
                "scope": scope, "space_id": request[2], "may_delegate": may_delegate, "reason": request[5],
                "expires_at": None if request[3] is None else request[3].isoformat()}
        return self._mutate(actor, operation_id, "delegation_create", args, authorize, effect,
                            lambda conn, old: self._delegation_row(conn, old["id"]))

    def delegation_revoke(self, actor, operation_id, delegation_id):
        """Only the grantor revokes. Revoking a parent also disables every child below it."""
        self._user(actor)
        delegation_id = _uuid(delegation_id, "delegation_id")
        def authorize(conn):
            self._user(actor)
            row = self._delegation_row(conn, delegation_id)
            if row["grantor"] != actor.principal or row["grantor_account"] != actor.account:
                raise StoreError("forbidden")
        def effect(conn):
            row = self._delegation_row(conn, delegation_id)
            if row["revoked_at"] is None:
                conn.execute("UPDATE baton.x_delegation SET revoked_at=clock_timestamp(),revoked_by=%s WHERE id=%s", (actor.principal, delegation_id))
                row = self._delegation_row(conn, delegation_id)
                self._audit(conn, actor, operation_id, "delegation.revoked", space_id=row["space_id"], delegation_id=row["id"],
                            scope=row["scope"], created_via="api", chain=row["chain"])
            return row
        return self._mutate(actor, operation_id, "delegation_revoke", {"delegation_id": delegation_id}, authorize, effect,
                            lambda conn, old: self._delegation_row(conn, old["id"]))

    def delegation_list(self, actor):
        """Delegations the caller granted or holds, including revoked and expired ones."""
        self._user(actor)
        def read(conn):
            rows = conn.execute("""SELECT * FROM baton.x_delegation WHERE (grantor=%s AND grantor_account=%s)
                OR (delegate=%s AND delegate_account=%s) ORDER BY created_at,id LIMIT %s""",
                (actor.principal, actor.account, actor.principal, actor.account, DELEGATION_LIST_LIMIT)).fetchall()
            return [self._delegation_view(conn, row) for row in rows]
        return self._read(actor, read)

    def operator_delegation_create(self, grantor, delegate, scope, space_id, expires_at, may_delegate, reason, operator):
        """Record the space owner's root delegation as executed by a named operator; audit keeps grantor and operator apart.

        Bounded by the same rules as the API: the grantor must own the named space, and the delegate cannot be the owner.
        """
        if not isinstance(grantor, Actor):
            raise StoreError("invalid", "grantor", "an identity with principal, account and kind")
        operator = _identifier(operator, "operator")
        request = _delegation_request(grantor, delegate, scope, space_id, expires_at, may_delegate, reason)
        with self._transaction(write=True) as conn:
            if not self._owns(conn, grantor, request[2]):
                raise StoreError("invalid", "grantor", "the exact root identity (principal, account, kind) recorded for the space")
            row = self._insert_delegation(conn, grantor, request, "operator", operator)
            self._audit(conn, grantor, uuid.uuid4(), "delegation.created", space_id=row["space_id"], requested_by=grantor.principal,
                        performed_by="operator:" + operator, delegation_id=row["id"], scope=row["scope"], created_via="operator",
                        chain=row["chain"])
            return _json(row)

    def operator_delegation_revoke(self, delegation_id, operator):
        operator = _identifier(operator, "operator")
        with self._transaction(write=True) as conn:
            row = self._delegation_row(conn, delegation_id)
            if row["revoked_at"] is None:
                conn.execute("UPDATE baton.x_delegation SET revoked_at=clock_timestamp(),revoked_by=%s WHERE id=%s",
                             ("operator:" + operator, row["id"]))
                row = self._delegation_row(conn, delegation_id)
                self._audit(conn, Actor(row["grantor"], row["grantor_account"], row["grantor_kind"]), uuid.uuid4(), "delegation.revoked",
                            space_id=row["space_id"], requested_by=row["grantor"], performed_by="operator:" + operator,
                            delegation_id=row["id"], scope=row["scope"], created_via="operator", chain=row["chain"])
            return _json(row)

    def operator_delegation_list(self, active_only=False):
        with self._transaction() as conn:
            rows = [self._delegation_view(conn, row) for row in
                    conn.execute("SELECT * FROM baton.x_delegation ORDER BY created_at,id").fetchall()]
            return _json([row for row in rows if row["active"] or not active_only])

    def set_space_owner(self, space_id, principal, account, kind, operator):
        """Operator-only: record which exact identity is the root approver of a space.

        For spaces whose creator could not be backfilled (owner_principal is NULL and the space has no root). The
        account must be the space's existing owner account; moving ownership to another account is not done here.
        """
        operator = _identifier(operator, "operator")
        who = Actor(principal, account, kind)
        with self._transaction(write=True) as conn:
            space = self._row(conn, "spaces", _uuid(space_id, "space_id"))
            if space["owner_account"] != who.account:
                raise StoreError("invalid", "account", "the space's existing owner account")
            conn.execute("UPDATE baton.spaces SET owner_principal=%s,owner_kind=%s WHERE id=%s", (who.principal, who.kind, space["id"]))
            self._audit(conn, Actor(operator, "baton-operator", "human"), uuid.uuid4(), "space.owner_set", space_id=space["id"],
                        requested_by=who.principal, performed_by="operator:" + operator, owner_kind=who.kind)
            return _json(self._row(conn, "spaces", str(space["id"])))

    @staticmethod
    def _policy_key(space_id, scope_kind, scope_value):
        space_id = _uuid(space_id, "space_id")
        scope = normalize_scopes([{"kind": scope_kind, "value": scope_value}])[0]
        return space_id, scope["kind"], scope["value"].casefold()

    def _policy_audit(self, conn, event, operator, space_id, kind, **metadata):
        self._audit(conn, Actor(operator, "baton-operator", "human"), uuid.uuid4(), event, space_id=space_id, requested_by=operator,
                    performed_by="operator:" + operator, scope_kind=kind, **metadata)

    def set_approval_policy(self, space_id, scope_kind, scope_value, required_approvals, root_required, operator):
        """Operator-only store config: require two approvers (and optionally the root) for tasks touching a scope."""
        operator = _identifier(operator, "operator")
        space_id, kind, value = self._policy_key(space_id, scope_kind, scope_value)
        if type(required_approvals) is not int or required_approvals not in (1, 2):
            raise StoreError("invalid", "required_approvals", "1 or 2")
        if type(root_required) is not bool:
            raise StoreError("invalid", "root_required", "boolean")
        with self._transaction(write=True) as conn:
            self._row(conn, "spaces", space_id)
            row = conn.execute("""INSERT INTO baton.x_approval_policy(space_id,scope_kind,scope_value,required_approvals,root_required)
                VALUES (%s,%s,%s,%s,%s) ON CONFLICT(space_id,scope_kind,scope_value) DO UPDATE SET
                required_approvals=EXCLUDED.required_approvals,root_required=EXCLUDED.root_required,updated_at=clock_timestamp()
                RETURNING *""", (space_id, kind, value, required_approvals, root_required)).fetchone()
            self._policy_audit(conn, "approval_policy.set", operator, space_id, kind,
                               required_approvals=required_approvals, root_required=root_required)
            return _json(row)

    def remove_approval_policy(self, space_id, scope_kind, scope_value, operator):
        operator = _identifier(operator, "operator")
        space_id, kind, value = self._policy_key(space_id, scope_kind, scope_value)
        with self._transaction(write=True) as conn:
            row = conn.execute("DELETE FROM baton.x_approval_policy WHERE space_id=%s AND scope_kind=%s AND scope_value=%s RETURNING *",
                               (space_id, kind, value)).fetchone()
            if not row:
                raise StoreError("not_found")
            self._policy_audit(conn, "approval_policy.removed", operator, space_id, kind)
            return _json(row)

    def list_approval_policies(self, space_id=None):
        with self._transaction() as conn:
            rows = conn.execute("""SELECT * FROM baton.x_approval_policy WHERE (%s::uuid IS NULL OR space_id=%s::uuid)
                ORDER BY space_id,scope_kind,scope_value""", (space_id and _uuid(space_id, "space_id"),) * 2).fetchall()
            return _json(rows)

    def list_audit(self, actor, task_id=None, space_id=None, limit=100):
        if not task_id and not space_id:
            raise StoreError("invalid", 'task_id', 'task_id or space_id is required')
        _integer(limit, 1, 1000, "limit")
        def read(conn):
            if task_id:
                task = self._task_for(conn, actor, task_id)
                if space_id and str(self._row(conn, "pages", str(task["page_id"]))["space_id"]) != _uuid(space_id):
                    raise StoreError("invalid")
            elif space_id:
                self._require(conn, actor, "space", space_id)
            return conn.execute("""SELECT * FROM baton.x_audit WHERE (%s::uuid IS NULL OR task_id=%s::uuid)
                AND (%s::uuid IS NULL OR space_id=%s::uuid) ORDER BY time,id LIMIT %s""", (task_id, task_id, space_id, space_id, limit)).fetchall()
        return self._read(actor, read)

    @staticmethod
    def _events(events):
        if events is None:
            return None
        if not isinstance(events, list) or not events or len(events) > len(NOTIFY_EVENTS) \
                or any(not isinstance(event, str) or event not in NOTIFY_EVENTS for event in events):
            raise StoreError("invalid", 'events', 'non-empty array of known notification event names')
        return sorted(set(events))

    def webhook_subscribe(self, actor, operation_id, url, events=None, include_preview=False):
        self._user(actor)
        events = self._events(events)
        if type(include_preview) is not bool:
            raise StoreError("invalid", "include_preview", "boolean")
        try:
            # DNS happens before the transaction so a slow resolver never holds the write lock.
            webhooks.check_url(url)
        except webhooks.Refused:
            raise StoreError("invalid") from None
        def effect(conn):
            if conn.execute("SELECT count(*) AS n FROM baton.x_webhook WHERE principal=%s", (actor.principal,)).fetchone()["n"] >= WEBHOOK_LIMIT:
                raise StoreError("rate_limited")
            identity, secret = uuid.uuid4(), secrets.token_bytes(32)
            conn.execute("INSERT INTO baton.x_webhook(id,principal,account,url,events,secret,include_preview) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                         (identity, actor.principal, actor.account, url, None if events is None else Jsonb(events), secret, include_preview))
            self._audit(conn, actor, operation_id, "webhook.subscribed", subscription_id=identity)
            return {"subscription_id": identity, "_once": {"signing_secret": webhooks.format_secret(secret)}}
        # The default is left out of the digest so requests recorded before the option existed replay unchanged.
        args = {"url": url, "events": events, **({"include_preview": True} if include_preview else {})}
        return self._mutate(actor, operation_id, "webhook_subscribe", args, lambda conn: self._user(actor), effect)

    def webhook_list(self, actor):
        self._user(actor)
        def read(conn):
            rows = conn.execute("""SELECT w.id,w.url,w.events,w.include_preview,w.created_at,
                count(*) FILTER (WHERE d.state='pending') AS pending, count(*) FILTER (WHERE d.state='delivered') AS delivered,
                count(*) FILTER (WHERE d.state='dead') AS dead
                FROM baton.x_webhook w LEFT JOIN baton.x_webhook_delivery d ON d.subscription_id=w.id
                WHERE w.principal=%s AND w.account=%s GROUP BY w.id ORDER BY w.created_at,w.id""", (actor.principal, actor.account)).fetchall()
            return [{"subscription_id": row.pop("id"), **row} for row in rows]
        return self._read(actor, read)

    def webhook_delete(self, actor, operation_id, subscription_id):
        _uuid(subscription_id, "subscription_id")
        def effect(conn):
            row = conn.execute("DELETE FROM baton.x_webhook WHERE id=%s AND principal=%s AND account=%s RETURNING id",
                               (subscription_id, actor.principal, actor.account)).fetchone()
            if not row:
                raise StoreError("not_found")
            self._audit(conn, actor, operation_id, "webhook.deleted", subscription_id=subscription_id)
            return {"subscription_id": subscription_id, "deleted": True}
        return self._mutate(actor, operation_id, "webhook_delete", {"subscription_id": subscription_id}, lambda conn: self._user(actor), effect)

    def webhook_rotate_secret(self, actor, operation_id, subscription_id):
        """Replace the signing secret. The new one is returned once; the old one keeps signing for the grace period."""
        _uuid(subscription_id, "subscription_id")
        grace = self.rotation_grace_seconds
        def effect(conn):
            secret = secrets.token_bytes(32)
            # A second rotation inside the grace period retires the oldest secret: at most two sign at any time.
            row = conn.execute("""UPDATE baton.x_webhook SET previous_secret=secret,
                previous_secret_expires_at=clock_timestamp()+%s*interval '1 second', secret=%s
                WHERE id=%s AND principal=%s AND account=%s RETURNING id,previous_secret_expires_at""",
                (grace, secret, subscription_id, actor.principal, actor.account)).fetchone()
            if not row:
                raise StoreError("not_found")
            self._audit(conn, actor, operation_id, "webhook.secret_rotated", subscription_id=subscription_id, grace_seconds=grace)
            return {"subscription_id": subscription_id, "previous_secret_expires_at": row["previous_secret_expires_at"],
                    "_once": {"signing_secret": webhooks.format_secret(secret)}}
        return self._mutate(actor, operation_id, "webhook_rotate_secret", {"subscription_id": subscription_id}, lambda conn: self._user(actor), effect)

    def webhook_revoke_previous_secret(self, actor, operation_id, subscription_id):
        """End a rotation grace period now: the replaced secret stops signing with the next delivery attempt."""
        _uuid(subscription_id, "subscription_id")
        def effect(conn):
            row = conn.execute("""UPDATE baton.x_webhook w SET previous_secret=NULL,previous_secret_expires_at=NULL
                FROM baton.x_webhook old WHERE old.id=w.id AND w.id=%s AND w.principal=%s AND w.account=%s
                RETURNING old.previous_secret IS NOT NULL AS had_previous""", (subscription_id, actor.principal, actor.account)).fetchone()
            if not row:
                raise StoreError("not_found")
            self._audit(conn, actor, operation_id, "webhook.previous_secret_revoked", subscription_id=subscription_id)
            return {"subscription_id": subscription_id, "revoked": row["had_previous"]}
        return self._mutate(actor, operation_id, "webhook_revoke_previous_secret", {"subscription_id": subscription_id}, lambda conn: self._user(actor), effect)

    def webhook_signing_secrets(self, actor, subscription_id):
        """Secrets that sign a delivery sent now, newest first, read at send time so a rotation or revocation that
        happened after the claim is honoured. None when the subscription is gone. Internal; never log the result."""
        self._service(actor)
        _uuid(subscription_id, "subscription_id")
        with self._transaction() as conn:
            row = conn.execute("""SELECT secret,
                CASE WHEN previous_secret_expires_at>clock_timestamp() THEN previous_secret END AS previous_secret
                FROM baton.x_webhook WHERE id=%s""", (subscription_id,)).fetchone()
        if not row:
            return None
        return [bytes(row["secret"])] + ([bytes(row["previous_secret"])] if row["previous_secret"] is not None else [])

    def _sweep_expired_secrets(self, conn):
        """Housekeeping at most once per WEBHOOK_SECRET_SWEEP_SECONDS per process, never on every claim."""
        now = time.monotonic()
        if self._secret_sweep_at is not None and now - self._secret_sweep_at < WEBHOOK_SECRET_SWEEP_SECONDS:
            return
        self._secret_sweep_at = now
        conn.execute("""UPDATE baton.x_webhook SET previous_secret=NULL,previous_secret_expires_at=NULL
            WHERE previous_secret_expires_at IS NOT NULL AND previous_secret_expires_at<=clock_timestamp()""")

    def _settle_delivery(self, conn, delivery, outcome, http_status, duration_ms):
        """Record one attempt and move the delivery to its next state. Only final outcomes are audited."""
        attempt = delivery["attempts"] + 1
        conn.execute("""INSERT INTO baton.x_webhook_attempt(id,subscription_id,notification_id,attempt,outcome,http_status,duration_ms)
            VALUES (%s,%s,%s,%s,%s,%s,%s)""", (uuid.uuid4(), delivery["subscription_id"], delivery["notification_id"], attempt,
                                               outcome, http_status, max(0, min(int(duration_ms), 2**31 - 1))))
        final = outcome in {"delivered", "forbidden"} or attempt >= WEBHOOK_MAX_ATTEMPTS
        if not final:
            conn.execute("""UPDATE baton.x_webhook_delivery SET attempts=%s,next_attempt_at=clock_timestamp()+%s*interval '1 second'
                WHERE subscription_id=%s AND notification_id=%s""",
                (attempt, WEBHOOK_BACKOFF_SECONDS[min(attempt, len(WEBHOOK_BACKOFF_SECONDS)) - 1], delivery["subscription_id"], delivery["notification_id"]))
            return "pending"
        state = "delivered" if outcome == "delivered" else "dead"
        conn.execute("UPDATE baton.x_webhook_delivery SET attempts=%s,state=%s,finished_at=clock_timestamp() WHERE subscription_id=%s AND notification_id=%s",
                     (attempt, state, delivery["subscription_id"], delivery["notification_id"]))
        page = self._notification_page(conn, delivery)
        space = conn.execute("SELECT space_id FROM baton.pages WHERE id=%s", (page,)).fetchone() if page else None
        # No URL, header, secret or body is recorded: only identifiers and the final status.
        self._audit(conn, SERVICE_ACTOR, uuid.uuid5(uuid.NAMESPACE_URL, f"baton/webhook/{delivery['subscription_id']}/{delivery['notification_id']}"),
                    "webhook.delivered" if state == "delivered" else "webhook.dead_lettered",
                    space_id=space["space_id"] if space else None, task_id=delivery["object_id"] if delivery["object_type"] == "task" else None,
                    requested_by=delivery["recipient"], subscription_id=delivery["subscription_id"], notification_id=delivery["notification_id"],
                    attempts=attempt, http_status=http_status, notify_event=delivery["event"], object_type=delivery["object_type"],
                    **({"error_code": outcome} if state == "dead" else {}))
        return state

    def webhook_claim_due(self, actor, limit=20):
        """Lease due deliveries to the bridge delivery loop. Secrets are not returned: they are read at send time."""
        self._service(actor)
        _integer(limit, 1, 100)
        with self._transaction(write=True) as conn:
            self._sweep_expired_secrets(conn)
            rows = conn.execute("""SELECT d.subscription_id,d.notification_id,d.attempts,w.url,w.account,w.include_preview,n.seq,n.event,
                n.object_type,n.object_id,n.thread_id,n.created_at,n.recipient,n.recipient_account,n.actor,n.actor_kind,
                n.result_epoch,n.result_author
                FROM baton.x_webhook_delivery d JOIN baton.x_webhook w ON w.id=d.subscription_id
                JOIN baton.x_notify n ON n.id=d.notification_id
                WHERE d.state='pending' AND d.next_attempt_at<=clock_timestamp()
                ORDER BY d.next_attempt_at,n.seq LIMIT %s FOR UPDATE OF d SKIP LOCKED""", (limit,)).fetchall()
            claimed = []
            for row in rows:
                page = self._notification_page(conn, row)
                try:
                    allowed = page is not None and self._account_role(conn, row["recipient_account"], "page", str(page)) is not None
                except StoreError:
                    allowed = False
                if not allowed:
                    self._settle_delivery(conn, row, "forbidden", None, 0)
                    continue
                conn.execute("""UPDATE baton.x_webhook_delivery SET next_attempt_at=clock_timestamp()+%s*interval '1 second'
                    WHERE subscription_id=%s AND notification_id=%s""", (WEBHOOK_LEASE_SECONDS, row["subscription_id"], row["notification_id"]))
                # Previews are opt-in per subscription and read now, after the permission check above, never stored.
                preview = self._notification_preview(conn, row) if row["include_preview"] else {}
                claimed.append({"subscription_id": str(row["subscription_id"]), "notification_id": str(row["notification_id"]),
                                "attempts": row["attempts"], "url": row["url"],
                                "payload": _json({"id": row["notification_id"], "seq": row["seq"], "event": row["event"],
                                                  "object_type": row["object_type"], "object_id": row["object_id"],
                                                  "thread_id": row["thread_id"], "created_at": row["created_at"],
                                                  "recipient": row["recipient"], **self._notification_result(row), **preview})})
            return claimed

    def webhook_record(self, actor, subscription_id, notification_id, expected_attempts, outcome, http_status=None, duration_ms=0):
        self._service(actor)
        if outcome not in {"delivered", "http_error", "network_error", "refused"}:
            raise StoreError("invalid")
        with self._transaction(write=True) as conn:
            delivery = conn.execute("""SELECT d.subscription_id,d.notification_id,d.attempts,n.event,n.object_type,n.object_id,n.recipient
                FROM baton.x_webhook_delivery d JOIN baton.x_notify n ON n.id=d.notification_id
                WHERE d.subscription_id=%s AND d.notification_id=%s AND d.state='pending' AND d.attempts=%s FOR UPDATE OF d""",
                (_uuid(subscription_id), _uuid(notification_id), expected_attempts)).fetchone()
            return self._settle_delivery(conn, delivery, outcome, http_status, duration_ms) if delivery else None
