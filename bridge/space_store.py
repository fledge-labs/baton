# SPDX-License-Identifier: Apache-2.0
"""Baton Space v1: PostgreSQL authority, authenticated actors and fenced effects.

The schema is installed by a separate migration owner. Runtime connections never
run DDL. The Codex execution-deduplication SQLite journal is a separate worker
journal; Space content, queue, claims, notifications and audit live here.
"""

import hashlib
import json
import os
import re
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

LOCK_ID = 0x4241544F4E
ROLES = {None: 0, "view": 1, "comment": 2, "edit": 3}
MESSAGES = {
    "invalid": "Invalid request", "not_found": "Resource not found", "forbidden": "Operation not permitted",
    "operation_conflict": "Operation identifier was used for different input", "scope_conflict": "Scope is already claimed",
    "stale_claim": "Claim is expired or superseded", "revision_conflict": "Revision has changed",
    "gate_required": "A valid gate decision is required", "storage_unavailable": "Space storage is unavailable",
}


class StoreError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(MESSAGES.get(code, MESSAGES["storage_unavailable"]))


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", value):
        raise StoreError("invalid")
    return value


def _uuid(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value.lower():
            raise ValueError()
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise StoreError("invalid") from None


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


SERVICE_ACTOR = Actor("codex-worker", "baton-service", "agent")


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


def _payload(value):
    if len(_canonical(value)) > 262144:
        raise StoreError("invalid")
    return value


def _text(value, maximum=16000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\x00" in value:
        raise StoreError("invalid")
    return value


def _integer(value, minimum=1, maximum=3600):
    if type(value) is not int or not minimum <= value <= maximum:
        raise StoreError("invalid")
    return value


def _items(value):
    return [] if value is None else _payload(value if isinstance(value, list) else [value])


def normalize_scopes(scopes):
    """Keep declarations for display; v1 cannot prove physical path independence."""
    if scopes is None or scopes == []:
        return [{"kind": "path", "value": ""}]
    if not isinstance(scopes, list) or len(scopes) > 100:
        raise StoreError("invalid")
    result = []
    for scope in scopes:
        if not isinstance(scope, dict) or set(scope) != {"kind", "value"}:
            raise StoreError("invalid")
        kind, value = scope["kind"], scope["value"]
        if not isinstance(kind, str) or kind not in {"path", "module", "migration"} or not isinstance(value, str) or len(value) > 240:
            raise StoreError("invalid")
        if value.startswith("/") or not re.fullmatch(r"[A-Za-z0-9_./-]*", value) or ".." in value.split("/"):
            raise StoreError("invalid")
        value = value.casefold()
        if kind == "path":
            value = "/".join(part for part in value.split("/") if part not in {"", "."})
        else:
            _identifier(value)
            if "/" in value or value in {".", ".."}:
                raise StoreError("invalid")
            if kind == "migration" and value.isdecimal():
                value = str(int(value))
        item = {"kind": kind, "value": value}
        if item not in result:
            result.append(item)
    return sorted(result, key=lambda item: (item["kind"], item["value"]))


def scopes_overlap(left, right):
    for a in left:
        for b in right:
            if "module" in {a["kind"], b["kind"]}:
                return True  # No administrator module map exists in v1.
            if a["kind"] != b["kind"]:
                continue
            # No trusted repository/alias catalogue exists. Two different path
            # strings might name the same file, so every path reserves the root.
            if a["kind"] == "path" or a["value"] == b["value"]:
                return True
    return False


class SpaceStore:
    def __init__(self, config_path):
        self.config_path = Path(config_path)

    @classmethod
    def from_env(cls):
        return cls(os.environ.get("BATON_DATABASE_CONFIG", "/run/secrets/database.json"))

    @contextmanager
    def _transaction(self, write=False):
        try:
            config = json.loads(self.config_path.read_text())
            if set(config) != {"host", "port", "dbname", "user", "password"}:
                raise StoreError("storage_unavailable")
            with psycopg.connect(**config, connect_timeout=5, row_factory=dict_row) as conn:
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
        if not isinstance(actor, Actor) or actor.principal == SERVICE_ACTOR.principal or actor.account == SERVICE_ACTOR.account:
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
            if isinstance(result, dict):
                business_replay = result.pop("_business_replay", False)
                result = {**result, "replay": business_replay}
            conn.execute("INSERT INTO baton.operations(principal,operation_id,request_hash,method,result) VALUES (%s,%s,%s,%s,%s)",
                         (actor.principal, operation_id, digest, method, Jsonb(result)))
            return result

    @staticmethod
    def _row(conn, table, identity):
        # table is an internal constant, never a request parameter.
        row = conn.execute(f"SELECT * FROM baton.{table} WHERE id=%s", (_uuid(identity),)).fetchone()
        if not row:
            raise StoreError("not_found")
        return row

    def _role(self, conn, actor, resource_type, resource_id):
        self._user(actor)
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
        if space["owner_account"] == actor.account:
            return "edit"
        grants = conn.execute("""SELECT g.role FROM baton.grants g WHERE
            ((g.resource_type='space' AND g.resource_id=%s) OR (g.resource_type='page' AND g.resource_id=ANY(%s::uuid[])))
            AND ((g.subject_type='person' AND g.subject_id=%s) OR (g.subject_type='team' AND EXISTS (
                SELECT 1 FROM baton.team_members m WHERE m.team_id::text=g.subject_id AND m.account=%s)))""",
            (space["id"], page_ids, actor.account, actor.account)).fetchall()
        return max((row["role"] for row in grants), key=ROLES.get, default=None)

    def _require(self, conn, actor, resource_type, resource_id, role="view"):
        if ROLES[self._role(conn, actor, resource_type, resource_id)] < ROLES[role]:
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
    def _audit(conn, actor, operation_id, event, space_id=None, task_id=None, requested_by=None, **metadata):
        allowed = {"page_id", "block_id", "thread_id", "epoch", "revision", "proposal_id", "team_id", "subject_type",
                   "role", "decision", "backend", "a2a_task_id", "input_sha256", "output_sha256", "error_code",
                   "count", "target_type", "execution_mode", "old_epoch"}
        if not set(metadata) <= allowed:
            raise StoreError("invalid")
        conn.execute("""INSERT INTO baton.x_audit(id,event,agent,actor_account,actor_kind,requested_by,performed_by,
            operation_id,space_id,task_id,metadata) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (uuid.uuid4(), event, actor.principal, actor.account, actor.kind, requested_by or actor.principal,
             actor.principal, operation_id, space_id, task_id, Jsonb(_json(metadata))))

    def check_schema(self, actor):
        self._service(actor)
        def check(conn):
            version = conn.execute("SELECT version FROM baton.schema_version WHERE singleton").fetchone()
            role = conn.execute("""SELECT r.rolsuper,c.relowner=r.oid AS owns_audit FROM pg_roles r
                CROSS JOIN pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE r.rolname=current_user AND n.nspname='baton' AND c.relname='x_audit'""").fetchone()
            triggers = conn.execute("SELECT count(*) AS n FROM pg_trigger WHERE tgrelid='baton.x_audit'::regclass AND tgname IN ('audit_no_change','audit_no_truncate') AND tgenabled='O'").fetchone()
            required = {
                "spaces": "id name owner_account scope_repository default_for",
                "pages": "id space_id parent_id owner_account title revision",
                "blocks": "id page_id kind content position revision author_principal author_account author_kind",
                "block_revisions": "block_id revision content author_principal operation_id proposal_id",
                "teams": "id name owner_account", "team_members": "team_id account",
                "grants": "resource_type resource_id subject_type subject_id role",
                "comments": "id page_id thread_id body author_principal requested_by",
                "mentions": "id comment_id target_type target_id action_requested requested_by created_at",
                "proposals": "id block_id expected_revision proposed_content status revision",
                "proposal_revisions": "proposal_id revision content author_principal",
                "tasks": "id page_id block_id owner owner_account owner_kind assignee idempotency_key input_hash prompt execution_mode gate_mode reviewers scopes state epoch result_epoch result result_author result_author_account base_commit context evidence a2a_id backend dispatch_started_at error",
                "x_claim": "task_id holder holder_account holder_kind epoch lease_expires_at scopes scope_repository",
                "x_handoff": "task_id old_epoch new_epoch base_commit context evidence",
                "x_gate": "task_id epoch decision reviewer reviewer_account evidence",
                "operations": "principal operation_id request_hash method result",
                "x_audit": "id time event agent actor_account actor_kind requested_by performed_by operation_id space_id task_id metadata",
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
        _text(name, 200)
        def effect(conn):
            identity = str(uuid.uuid4())
            row = conn.execute("INSERT INTO baton.spaces(id,name,owner_account) VALUES (%s,%s,%s) RETURNING *", (identity, name, actor.account)).fetchone()
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
        _text(title, 200)
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
            raise StoreError("invalid")
        _payload(content)
        if position is not None:
            _integer(position, 0, 1000000000)
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
        _integer(expected_revision, 1, 2**63 - 1)
        _payload(content)
        return self._mutate(actor, operation_id, "update_block", {"block_id": block_id, "expected_revision": expected_revision, "content": content},
                            lambda conn: self._page_for_block(conn, actor, block_id, "edit"),
                            lambda conn: self._update_block(conn, actor, operation_id, block_id, expected_revision, content))

    def share(self, actor, operation_id, resource_type, resource_id, subject_type, subject_id, role):
        if (not isinstance(resource_type, str) or not isinstance(subject_type, str)
                or (role is not None and not isinstance(role, str))
                or resource_type not in {"space", "page"} or subject_type not in {"person", "team"} or role not in ROLES):
            raise StoreError("invalid")
        if subject_type == "team":
            _uuid(subject_id)
            if role == "comment":
                raise StoreError("invalid")
        else:
            _identifier(subject_id)
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
        _text(name, 200)
        def effect(conn):
            row = conn.execute("INSERT INTO baton.teams VALUES (%s,%s,%s) RETURNING *", (uuid.uuid4(), name, actor.account)).fetchone()
            conn.execute("INSERT INTO baton.team_members VALUES (%s,%s)", (row["id"], actor.account))
            self._audit(conn, actor, operation_id, "team.created", team_id=row["id"])
            return row
        return self._mutate(actor, operation_id, "create_team", {"name": name}, lambda conn: self._user(actor), effect)

    def set_team_member(self, actor, operation_id, team_id, account, member):
        _identifier(account)
        if type(member) is not bool:
            raise StoreError("invalid")
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
        _text(body)
        _payload(selection)
        if thread_id:
            _uuid(thread_id)
        def effect(conn):
            if thread_id and not conn.execute("SELECT 1 FROM baton.comments WHERE page_id=%s AND thread_id=%s", (page_id, thread_id)).fetchone():
                raise StoreError("not_found")
            row = self._comment(conn, actor, operation_id, page_id, body, thread_id, selection)
            page = self._row(conn, "pages", page_id)
            task = conn.execute("SELECT id FROM baton.tasks WHERE id=%s AND page_id=%s", (row["thread_id"], page_id)).fetchone()
            self._audit(conn, actor, operation_id, "comment.created", space_id=page["space_id"], task_id=task["id"] if task else None,
                        page_id=page_id, thread_id=row["thread_id"])
            return row
        return self._mutate(actor, operation_id, "comment", {"page_id": page_id, "body": body, "thread_id": thread_id, "selection": selection},
                            lambda conn: self._require(conn, actor, "page", page_id, "comment"), effect)

    def list_comments(self, actor, page_id, thread_id=None):
        if thread_id:
            _uuid(thread_id)
        def read(conn):
            self._require(conn, actor, "page", page_id)
            return conn.execute("SELECT * FROM baton.comments WHERE page_id=%s AND (%s::uuid IS NULL OR thread_id=%s::uuid) ORDER BY created_at,id", (page_id, thread_id, thread_id)).fetchall()
        return self._read(actor, read)

    def mention(self, actor, operation_id, comment_id, target_type, target_id):
        if not isinstance(target_type, str) or target_type not in {"person", "agent"}:
            raise StoreError("invalid")
        _identifier(target_id)
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
            return row
        return self._mutate(actor, operation_id, "mention", {"comment_id": comment_id, "target_type": target_type, "target_id": target_id}, authorize, effect)

    def fetch_inbox(self, actor, limit=30):
        _integer(limit, 1, 100)
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
                           "requested_by": request["requested_by"], "action_requested": request["action_requested"], "requests": []}
                threads.setdefault(message["thread_id"], message)["requests"].append(request)
            assigned = conn.execute("SELECT * FROM baton.tasks WHERE assignee=%s ORDER BY created_at DESC", (actor.principal,)).fetchall()
            for task in assigned:
                if not self._role(conn, actor, "page", str(task["page_id"])):
                    continue
                identity = str(task["id"])
                message = threads.setdefault(identity, {"id": identity, "thread_id": identity, "subject": "[assigned] " + identity,
                    "from": task["owner"], "to": [actor.principal], "created_ts": task["created_at"], "body_md": task["prompt"],
                    "requested_by": task["owner"], "action_requested": True, "requests": []})
                message.update(task_id=identity, assignee=task["assignee"], prompt=task["prompt"], state=self._task_view(conn, identity)["state"], kind="assignment")
            for identity, message in threads.items():
                if "task_id" not in message:
                    task = conn.execute("SELECT assignee FROM baton.tasks WHERE id=%s", (identity,)).fetchone()
                    if task:
                        message.update(task_id=identity, assignee=task["assignee"], state=self._task_view(conn, identity)["state"], kind="mention")
            return {"agent_name": actor.principal, "messages": sorted(threads.values(), key=lambda item: item["created_ts"], reverse=True)[:limit]}
        return self._read(actor, read)

    def list_attribution(self, actor, block_id):
        def read(conn):
            self._page_for_block(conn, actor, block_id)
            return conn.execute("SELECT * FROM baton.block_revisions WHERE block_id=%s ORDER BY revision", (block_id,)).fetchall()
        return self._read(actor, read)

    def proposal_create(self, actor, operation_id, block_id, expected_revision, proposed_content, selection=None):
        _integer(expected_revision, 1, 2**63 - 1)
        _payload(proposed_content)
        _payload(selection)
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
            raise StoreError("invalid")
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
        _integer(target_revision, 1, 2**63 - 1)
        _integer(expected_revision, 1, 2**63 - 1)
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

    def _task_view(self, conn, task_id, include_prompt=False):
        row = dict(self._row(conn, "tasks", task_id))
        row.pop("input_hash")
        row.pop("idempotency_key")
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
        row.update(submission_mail_status="sent", mail_status="sent" if row["state"] == "done" else "not_ready", mail_error=None)
        row.update(submission_thread_status="sent", report_status="sent" if row["state"] == "done" else "not_ready")
        return row

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
            space = conn.execute("INSERT INTO baton.spaces(id,name,owner_account,default_for) VALUES (%s,'Tasks',%s,%s) RETURNING *",
                                 (space_id, actor.account, actor.account)).fetchone()
            self._audit(conn, actor, operation_id, "space.created", space_id=space_id)
        page_id = uuid.uuid5(uuid.NAMESPACE_URL, "baton/default-page/" + actor.account)
        if not conn.execute("SELECT 1 FROM baton.pages WHERE id=%s", (page_id,)).fetchone():
            conn.execute("INSERT INTO baton.pages(id,space_id,owner_account,title) VALUES (%s,%s,%s,'Tasks')", (page_id, space["id"], actor.account))
            self._audit(conn, actor, operation_id, "page.created", space_id=space["id"], page_id=page_id, revision=1)
        return str(page_id)

    def _new_task(self, conn, actor, operation_id, page_id, prompt, scopes, mode, reviewers,
                  input_hash, idempotency_key=None, base_commit=None, context=None, evidence=None, assignee=None):
        if assignee is not None:
            if not isinstance(assignee, Actor):
                raise StoreError("invalid")
            self._require(conn, assignee, "page", page_id, "edit")
        task_id = uuid.uuid4()
        block_id = uuid.uuid4()
        content = {"task_id": str(task_id), "prompt": prompt, "execution_mode": mode}
        position = conn.execute("SELECT COALESCE(MAX(position),-1)+1 AS n FROM baton.blocks WHERE page_id=%s", (page_id,)).fetchone()["n"]
        block = conn.execute("""INSERT INTO baton.blocks(id,page_id,kind,content,position,author_principal,author_account,author_kind)
            VALUES (%s,%s,'task',%s,%s,%s,%s,%s) RETURNING *""", (block_id, page_id, Jsonb(content), position, actor.principal, actor.account, actor.kind)).fetchone()
        self._revision(conn, actor, operation_id, block, content)
        conn.execute("UPDATE baton.pages SET revision=revision+1 WHERE id=%s", (page_id,))
        task = conn.execute("""INSERT INTO baton.tasks(id,page_id,block_id,owner,owner_account,owner_kind,assignee,idempotency_key,input_hash,prompt,
            execution_mode,gate_mode,reviewers,scopes,base_commit,context,evidence)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (task_id, page_id, block_id, actor.principal, actor.account, actor.kind, assignee.principal if assignee else None,
             idempotency_key, input_hash, prompt, mode, "worker_success" if mode == "codex" else "reviewer", Jsonb(reviewers),
             Jsonb(scopes), base_commit, Jsonb(_items(context)), Jsonb(_items(evidence)))).fetchone()
        self._task_audit(conn, actor, operation_id, "task.submitted", task, input_sha256=hashlib.sha256(prompt.encode()).hexdigest(), execution_mode=mode)
        self._notify_task(conn, actor, operation_id, task, "submitted", prompt, assignee.principal if assignee else None)
        if assignee:
            self._task_audit(conn, actor, operation_id, "task.assigned", task)
        return self._task_view(conn, str(task_id))

    def submit_task(self, actor, operation_id, idempotency_key, prompt, page_id=None, scopes=None, assignee=None):
        _identifier(idempotency_key)
        _text(prompt)
        normalized = normalize_scopes(scopes)
        if assignee is not None and not isinstance(assignee, Actor):
            raise StoreError("invalid")
        if assignee is not None and assignee.principal == actor.principal:
            raise StoreError("invalid")
        args = {"idempotency_key": idempotency_key, "prompt": prompt, "page_id": page_id, "scopes": normalized,
                "assignee": asdict(assignee) if assignee else None}
        digest = hashlib.sha256(_canonical(args).encode()).hexdigest()
        def authorize(conn):
            self._user(actor)
            if page_id:
                self._require(conn, actor, "page", page_id, "edit")
            prior = conn.execute("SELECT * FROM baton.tasks WHERE owner=%s AND idempotency_key=%s", (actor.principal, idempotency_key)).fetchone()
            if prior:
                self._require(conn, actor, "page", str(prior["page_id"]), "edit")
        def effect(conn):
            prior = conn.execute("SELECT * FROM baton.tasks WHERE owner=%s AND idempotency_key=%s", (actor.principal, idempotency_key)).fetchone()
            if prior:
                if prior["input_hash"] != digest:
                    raise StoreError("operation_conflict")
                return {**self._task_view(conn, str(prior["id"])), "_business_replay": True}
            chosen = page_id or self._default_page(conn, actor, operation_id)
            return self._new_task(conn, actor, operation_id, chosen, prompt, normalized, "manual" if assignee else "codex",
                                  [actor.principal] if assignee else [], digest, idempotency_key=idempotency_key, assignee=assignee)
        def replay(conn, old):
            return self._task_view(conn, old["task_id"])
        return self._mutate(actor, operation_id, "submit_task", args, authorize, effect, replay)

    def create_task_block(self, actor, operation_id, page_id, prompt, scopes, reviewers, base_commit=None, context=None, evidence=None, assignee=None):
        _text(prompt)
        normalized = normalize_scopes(scopes)
        if not isinstance(reviewers, list) or not reviewers or len(reviewers) > 100:
            raise StoreError("invalid")
        for reviewer in reviewers:
            _identifier(reviewer)
        if base_commit is not None:
            _text(base_commit, 200)
        _items(context)
        _items(evidence)
        if assignee is not None and not isinstance(assignee, Actor):
            raise StoreError("invalid")
        args = {"page_id": page_id, "prompt": prompt, "scopes": normalized, "reviewers": reviewers,
                "base_commit": base_commit, "context": context, "evidence": evidence, "assignee": asdict(assignee) if assignee else None}
        digest = hashlib.sha256(_canonical(args).encode()).hexdigest()
        return self._mutate(actor, operation_id, "create_task_block", args,
                            lambda conn: self._require(conn, actor, "page", page_id, "edit"),
                            lambda conn: self._new_task(conn, actor, operation_id, page_id, prompt, normalized, "manual", reviewers, digest,
                                                        base_commit=base_commit, context=context, evidence=evidence, assignee=assignee))

    def get_task(self, actor, task_id):
        def read(conn):
            self._task_for(conn, actor, task_id)
            return self._task_view(conn, task_id)
        return self._read(actor, read)

    def list_tasks(self, actor, limit=50):
        self._user(actor)
        _integer(limit, 1, 100)
        def read(conn):
            rows = conn.execute("SELECT * FROM baton.tasks WHERE owner=%s ORDER BY created_at DESC,id DESC", (actor.principal,)).fetchall()
            return [self._task_view(conn, str(row["id"])) for row in rows if self._role(conn, actor, "page", str(row["page_id"]))][:limit]
        return self._read(actor, read)

    def _expire(self, conn, operation_id, auto_only=False):
        rows = conn.execute("""SELECT t.* FROM baton.tasks t JOIN baton.x_claim c ON c.task_id=t.id
            WHERE c.lease_expires_at<=clock_timestamp() AND (NOT %s OR t.execution_mode='codex')""", (auto_only,)).fetchall()
        for task in rows:
            conn.execute("DELETE FROM baton.x_claim WHERE task_id=%s", (task["id"],))
            state = "interrupted" if task["execution_mode"] == "codex" and task["dispatch_started_at"] else "queued"
            conn.execute("UPDATE baton.tasks SET state=%s,error=%s WHERE id=%s AND state='claimed'", (state, "dispatch_uncertain" if state == "interrupted" else None, task["id"]))
            self._task_audit(conn, SERVICE_ACTOR, operation_id, "claim.expired", task, epoch=task["epoch"])
            if state == "interrupted":
                self._task_audit(conn, SERVICE_ACTOR, operation_id, "task.interrupted", task, epoch=task["epoch"])
        return len(rows)

    def recover_auto(self, actor, operation_id):
        return self._mutate(actor, operation_id, "recover_auto", {}, lambda conn: self._service(actor),
                            lambda conn: {"released": self._expire(conn, operation_id, auto_only=True)})

    @staticmethod
    def _live(conn, actor, task_id, epoch):
        _integer(epoch, 1, 2**63 - 1)
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

    def _auto(self, conn, actor, task_id):
        self._service(actor)
        task = self._row(conn, "tasks", task_id)
        if task["execution_mode"] != "codex" or task["gate_mode"] != "worker_success":
            raise StoreError("forbidden")
        return task

    def _take_claim(self, conn, actor, operation_id, task, lease_seconds):
        if task["state"] != "queued" or conn.execute("SELECT 1 FROM baton.x_claim WHERE task_id=%s AND lease_expires_at>clock_timestamp()", (task["id"],)).fetchone():
            raise StoreError("scope_conflict")
        for other in conn.execute("SELECT scopes FROM baton.x_claim WHERE lease_expires_at>clock_timestamp()"):
            if scopes_overlap(task["scopes"], other["scopes"]):
                raise StoreError("scope_conflict")
        epoch = task["epoch"] + 1
        conn.execute("""INSERT INTO baton.x_claim(task_id,holder,holder_account,holder_kind,epoch,lease_expires_at,scopes)
            VALUES (%s,%s,%s,%s,%s,clock_timestamp()+%s*interval '1 second',%s)""",
            (task["id"], actor.principal, actor.account, actor.kind, epoch, lease_seconds, Jsonb(task["scopes"])))
        conn.execute("UPDATE baton.tasks SET state='claimed',epoch=%s,result_epoch=NULL,error=NULL WHERE id=%s", (epoch, task["id"]))
        self._task_audit(conn, actor, operation_id, "worker.claimed" if task["execution_mode"] == "codex" else "task.claimed", task, epoch=epoch)
        return self._task_view(conn, str(task["id"]), include_prompt=True)

    def claim(self, actor, operation_id, task_id, lease_seconds=300):
        _integer(lease_seconds)
        def authorize(conn):
            task = self._manual(conn, actor, task_id)
            if task["assignee"] is not None and task["assignee"] != actor.principal:
                raise StoreError("forbidden")
        def effect(conn):
            self._expire(conn, operation_id)
            return self._take_claim(conn, actor, operation_id, self._row(conn, "tasks", task_id), lease_seconds)
        def replay(conn, old):
            self._live(conn, actor, task_id, old["epoch"])
            return self._task_view(conn, task_id, include_prompt=True)
        return self._mutate(actor, operation_id, "claim", {"task_id": task_id, "lease_seconds": lease_seconds}, authorize, effect, replay)

    def claim_next(self, actor, operation_id, lease_seconds=300):
        _integer(lease_seconds)
        def effect(conn):
            self._expire(conn, operation_id)
            for task in conn.execute("SELECT * FROM baton.tasks WHERE execution_mode='codex' AND state='queued' ORDER BY created_at,id").fetchall():
                conflicts = any(scopes_overlap(task["scopes"], row["scopes"]) for row in conn.execute("SELECT scopes FROM baton.x_claim WHERE lease_expires_at>clock_timestamp()"))
                if not conflicts:
                    return self._take_claim(conn, actor, operation_id, task, lease_seconds)
            return None
        def replay(conn, old):
            if old is not None:
                self._live(conn, actor, old["task_id"], old["epoch"])
                return self._task_view(conn, old["task_id"], include_prompt=True)
        return self._mutate(actor, operation_id, "claim_next", {"lease_seconds": lease_seconds}, lambda conn: self._service(actor), effect, replay)

    def _renew(self, actor, operation_id, task_id, epoch, lease_seconds, automatic):
        _integer(lease_seconds)
        method = "renew_auto" if automatic else "renew"
        authorize = lambda conn: self._auto(conn, actor, task_id) if automatic else self._manual(conn, actor, task_id)
        def effect(conn):
            self._live(conn, actor, task_id, epoch)
            conn.execute("UPDATE baton.x_claim SET lease_expires_at=clock_timestamp()+%s*interval '1 second' WHERE task_id=%s", (lease_seconds, task_id))
            self._task_audit(conn, actor, operation_id, "claim.renewed", self._row(conn, "tasks", task_id), epoch=epoch)
            return self._task_view(conn, task_id)
        def replay(conn, old):
            self._live(conn, actor, task_id, epoch)
            return self._task_view(conn, task_id)
        return self._mutate(actor, operation_id, method, {"task_id": task_id, "epoch": epoch, "lease_seconds": lease_seconds}, authorize, effect, replay)

    def renew(self, actor, operation_id, task_id, epoch, lease_seconds=300):
        return self._renew(actor, operation_id, task_id, epoch, lease_seconds, False)

    def renew_auto(self, actor, operation_id, task_id, epoch, lease_seconds=300):
        return self._renew(actor, operation_id, task_id, epoch, lease_seconds, True)

    def mark_dispatched(self, actor, operation_id, task_id, epoch):
        def effect(conn):
            self._live(conn, actor, task_id, epoch)
            task = self._row(conn, "tasks", task_id)
            if task["dispatch_started_at"] is not None:
                raise StoreError("operation_conflict")
            conn.execute("UPDATE baton.tasks SET dispatch_started_at=clock_timestamp() WHERE id=%s", (task_id,))
            self._task_audit(conn, actor, operation_id, "task.dispatched", task, epoch=epoch)
            return self._task_view(conn, task_id)
        def replay(conn, old):
            self._live(conn, actor, task_id, epoch)
            return self._task_view(conn, task_id)
        return self._mutate(actor, operation_id, "mark_dispatched", {"task_id": task_id, "epoch": epoch}, lambda conn: self._auto(conn, actor, task_id), effect, replay)

    def handoff(self, actor, operation_id, task_id, epoch, target, base_commit=None, context=None, evidence=None, lease_seconds=300):
        _integer(lease_seconds)
        if not isinstance(target, Actor) or target.principal == actor.principal:
            raise StoreError("invalid")
        _items(context)
        _items(evidence)
        if base_commit is not None:
            _text(base_commit, 200)
        def authorize(conn):
            task = self._manual(conn, actor, task_id)
            self._require(conn, target, "page", str(task["page_id"]), "edit")
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
            conn.execute("INSERT INTO baton.x_handoff(id,task_id,from_holder,to_holder,old_epoch,new_epoch,base_commit,context,evidence) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                         (uuid.uuid4(), task_id, actor.principal, target.principal, epoch, epoch + 1, base, Jsonb(contexts), Jsonb(evidence_items)))
            self._task_audit(conn, actor, operation_id, "task.handed_off", task, old_epoch=epoch, epoch=epoch + 1)
            self._notify_task(conn, actor, operation_id, task, "assigned", "Task handed off", target.principal)
            return self._task_view(conn, task_id, include_prompt=True)
        def replay(conn, old):
            self._live(conn, target, task_id, epoch + 1)
            return self._task_view(conn, task_id, include_prompt=True)
        return self._mutate(actor, operation_id, "handoff", {"task_id": task_id, "epoch": epoch, "target": asdict(target), "base_commit": base_commit,
                            "context": context, "evidence": evidence, "lease_seconds": lease_seconds}, authorize, effect, replay)

    def assign_task(self, actor, operation_id, task_id, target):
        if not isinstance(target, Actor):
            raise StoreError("invalid")
        def authorize(conn):
            task = self._task_for(conn, actor, task_id, "edit")
            self._require(conn, target, "page", str(task["page_id"]), "edit")
        def effect(conn):
            self._expire(conn, operation_id)
            task = self._row(conn, "tasks", task_id)
            if task["state"] != "queued" or task["dispatch_started_at"] is not None:
                raise StoreError("stale_claim")
            reviewers = task["reviewers"] or [actor.principal]
            conn.execute("UPDATE baton.tasks SET assignee=%s,execution_mode='manual',gate_mode='reviewer',reviewers=%s WHERE id=%s", (target.principal, Jsonb(reviewers), task_id))
            self._task_audit(conn, actor, operation_id, "task.assigned", task)
            self._notify_task(conn, actor, operation_id, task, "assigned", "Task assigned", target.principal)
            return self._task_view(conn, task_id)
        def replay(conn, old):
            task = self._row(conn, "tasks", task_id)
            if task["assignee"] != target.principal:
                raise StoreError("stale_claim")
            return self._task_view(conn, task_id)
        return self._mutate(actor, operation_id, "assign_task", {"task_id": task_id, "target": asdict(target)}, authorize, effect, replay)

    def submit_result(self, actor, operation_id, task_id, epoch, result, evidence=None):
        _text(result, 32000)
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
            return self._task_view(conn, task_id)
        def replay(conn, old):
            task = self._row(conn, "tasks", task_id)
            if task["epoch"] != epoch or task["result_epoch"] != epoch or task["state"] not in {"awaiting_gate", "done"}:
                raise StoreError("stale_claim")
            return self._task_view(conn, task_id)
        return self._mutate(actor, operation_id, "submit_result", {"task_id": task_id, "epoch": epoch, "result": result, "evidence": evidence},
                            lambda conn: self._manual(conn, actor, task_id), effect, replay)

    def gate(self, actor, operation_id, task_id, expected_epoch, decision, evidence=None):
        _integer(expected_epoch, 1, 2**63 - 1)
        if not isinstance(decision, str) or decision not in {"pass", "fail"}:
            raise StoreError("invalid")
        _items(evidence)
        def authorize(conn):
            task = self._manual(conn, actor, task_id)
            if actor.principal not in task["reviewers"] or actor.principal == task["result_author"]:
                raise StoreError("forbidden")
        def effect(conn):
            task = self._row(conn, "tasks", task_id)
            if task["state"] != "awaiting_gate" or task["epoch"] != expected_epoch or task["result_epoch"] != expected_epoch:
                raise StoreError("stale_claim")
            conn.execute("INSERT INTO baton.x_gate(id,task_id,epoch,decision,reviewer,reviewer_account,evidence) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                         (uuid.uuid4(), task_id, expected_epoch, decision, actor.principal, actor.account, Jsonb(_items(evidence))))
            conn.execute("UPDATE baton.tasks SET state=%s WHERE id=%s", ("done" if decision == "pass" else "queued", task_id))
            self._task_audit(conn, actor, operation_id, "gate.reviewed", task, epoch=expected_epoch, decision=decision)
            if decision == "pass":
                self._task_audit(conn, actor, operation_id, "task.completed", task, epoch=expected_epoch)
                self._notify_task(conn, actor, operation_id, task, "completed", task["result"])
            return self._task_view(conn, task_id)
        def replay(conn, old):
            task = self._row(conn, "tasks", task_id)
            if task["epoch"] != expected_epoch or task["result_epoch"] != expected_epoch:
                raise StoreError("stale_claim")
            return self._task_view(conn, task_id)
        return self._mutate(actor, operation_id, "gate", {"task_id": task_id, "expected_epoch": expected_epoch, "decision": decision, "evidence": evidence}, authorize, effect, replay)

    def finish_auto(self, actor, operation_id, task_id, epoch, result, a2a_id, backend, a2a_state="completed"):
        _text(result, 32000)
        _uuid(a2a_id)
        if not isinstance(backend, str) or backend not in {"demo", "codex"} or a2a_state != "completed":
            raise StoreError("gate_required")
        def effect(conn):
            self._live(conn, actor, task_id, epoch)
            task = self._row(conn, "tasks", task_id)
            if task["dispatch_started_at"] is None:
                raise StoreError("gate_required")
            conn.execute("""UPDATE baton.tasks SET state='done',result=%s,result_epoch=%s,result_author=%s,result_author_account=%s,
                a2a_id=%s,backend=%s WHERE id=%s""", (result, epoch, actor.principal, actor.account, a2a_id, backend, task_id))
            conn.execute("DELETE FROM baton.x_claim WHERE task_id=%s", (task_id,))
            conn.execute("INSERT INTO baton.x_gate(id,task_id,epoch,decision,reviewer,reviewer_account,evidence) VALUES (%s,%s,%s,'pass',%s,%s,%s)",
                         (uuid.uuid4(), task_id, epoch, actor.principal, actor.account, Jsonb({"a2a_id": a2a_id, "state": a2a_state, "backend": backend})))
            digest = hashlib.sha256(result.encode()).hexdigest()
            self._task_audit(conn, actor, operation_id, "worker.completed", task, epoch=epoch, backend=backend, a2a_task_id=a2a_id, output_sha256=digest)
            self._task_audit(conn, actor, operation_id, "task.completed", task, epoch=epoch, backend=backend, a2a_task_id=a2a_id, output_sha256=digest)
            self._notify_task(conn, actor, operation_id, task, "completed", result)
            return self._task_view(conn, task_id)
        def replay(conn, old):
            task = self._row(conn, "tasks", task_id)
            if task["epoch"] != epoch or task["state"] != "done":
                raise StoreError("stale_claim")
            return self._task_view(conn, task_id)
        return self._mutate(actor, operation_id, "finish_auto", {"task_id": task_id, "epoch": epoch, "result": result, "a2a_id": a2a_id, "backend": backend, "a2a_state": a2a_state},
                            lambda conn: self._auto(conn, actor, task_id), effect, replay)

    def interrupt_auto(self, actor, operation_id, task_id, epoch, error_code="dispatch_uncertain"):
        if not isinstance(error_code, str) or error_code not in {"dispatch_uncertain", "worker_failed", "execution_failed", "lease_lost", "restart", "cancelled"}:
            raise StoreError("invalid")
        def effect(conn):
            self._live(conn, actor, task_id, epoch)
            task = self._row(conn, "tasks", task_id)
            state = "failed" if error_code in {"worker_failed", "execution_failed"} else "interrupted"
            conn.execute("UPDATE baton.tasks SET state=%s,error=%s WHERE id=%s", (state, error_code, task_id))
            conn.execute("DELETE FROM baton.x_claim WHERE task_id=%s", (task_id,))
            self._task_audit(conn, actor, operation_id, "task." + state, task, epoch=epoch, error_code=error_code)
            return self._task_view(conn, task_id)
        def replay(conn, old):
            task = self._row(conn, "tasks", task_id)
            if task["epoch"] != epoch or task["state"] not in {"failed", "interrupted"}:
                raise StoreError("stale_claim")
            return self._task_view(conn, task_id)
        return self._mutate(actor, operation_id, "interrupt_auto", {"task_id": task_id, "epoch": epoch, "error_code": error_code},
                            lambda conn: self._auto(conn, actor, task_id), effect, replay)

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
            return [self._task_view(conn, str(task["id"])) for task in tasks if self._role(conn, actor, "page", str(task["page_id"]))]
        return self._read(actor, read)

    def list_audit(self, actor, task_id=None, space_id=None, limit=100):
        if not task_id and not space_id:
            raise StoreError("invalid")
        _integer(limit, 1, 1000)
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
