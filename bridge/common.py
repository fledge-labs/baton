# SPDX-License-Identifier: Apache-2.0
"""Shared identity, persistence and metadata-only audit boundaries."""

import hashlib
import hmac
import json
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

STATE = Path(os.environ.get("BATON_STATE", "/data"))
CONFIG = Path(os.environ.get("BATON_CONFIG", "/config"))
SECRETS = Path("/run/secrets")
CODEX_HOME = Path("/home/agent/.codex")
SPACE_PROTOCOL = "baton.space.v1"


def safe_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", value):
        raise ValueError("Invalid identifier")
    return value


def uuid_id(value):
    if not isinstance(value, str):
        raise ValueError("Invalid UUID")
    parsed = str(uuid.UUID(value))
    if parsed != value.lower():
        raise ValueError("Invalid UUID")
    return parsed


def public_url():
    value = os.environ.get("PUBLIC_URL", "http://localhost:8080").rstrip("/")
    parts = urlsplit(value)
    if (parts.scheme not in {"http", "https"} or not parts.hostname
            or parts.username or parts.password or parts.path or parts.query or parts.fragment):
        raise ValueError("PUBLIC_URL must be an HTTP(S) origin without credentials or a path")
    # Accessing port also validates its syntax and range.
    parts.port
    return value


def transport_origins():
    origin = public_url()
    hosts = ["bridge", "bridge:9000", "localhost:9000", "127.0.0.1:9000"]
    hosts.append(urlsplit(origin).netloc)
    return sorted(set(hosts)), [origin]


def audit(event, **fields):
    """Reject arbitrary payloads; request IDs must be UUIDs, never bearer material."""
    if not re.fullmatch(r"[a-z]+\.[a-z_]+", event):
        raise ValueError("Invalid audit event")
    record = {"time": datetime.now(timezone.utc).isoformat(), "event": event}
    for key, value in fields.items():
        if value is None:
            continue
        if key in {"task_id", "a2a_task_id", "request_id", "operation_id", "thread_id"}:
            try:
                record[key] = uuid_id(value)
            except (ValueError, TypeError, AttributeError):
                pass
        elif key in {"input_sha256", "output_sha256"}:
            if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value):
                record[key] = value
        elif key in {"agent", "error_type", "error_code"}:
            record[key] = safe_id(value)
        elif key == "backend" and value in {"demo", "codex"}:
            record[key] = value
        elif key in {"status", "exit_code", "epoch"} and type(value) is int:
            record[key] = value
        elif key == "thread_message_ids":
            record[key] = []
            for item in value:
                try:
                    record[key].append(uuid_id(item))
                except (ValueError, TypeError, AttributeError):
                    pass
        else:
            raise ValueError("Unsupported audit field")
    encoded = (json.dumps(record, ensure_ascii=True) + "\n").encode()
    STATE.mkdir(parents=True, exist_ok=True)
    fd = os.open(STATE / "audit.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        # One append syscall per bounded record keeps the two services' lines intact.
        os.write(fd, encoded)
        os.fsync(fd)
    finally:
        os.close(fd)
    print(encoded.decode().rstrip(), flush=True)


def registry_identity(record):
    """Only the administrator's registry defines connected accounts and actor kinds."""
    if record.get("active") is not True or record.get("role") not in {"client", "dispatcher"}:
        return None
    allowed_kinds = {"human", "agent"} if record["role"] == "client" else {"service"}
    if record.get("kind") not in allowed_kinds:
        return None
    return {"agent": safe_id(record["agent"]), "role": record["role"],
            "account": safe_id(record["account"]), "kind": record["kind"]}


def identity(authorization):
    if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
        return None
    digest = hashlib.sha256(authorization[7:].encode()).hexdigest()
    try:
        records = json.loads((CONFIG / "identities.json").read_text())
        for record in records.values():
            if hmac.compare_digest(record.get("hash", ""), digest):
                return registry_identity(record)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        # Missing or partially replaced configuration never grants access.
        return None
    return None


def resolve_principal(principal):
    """Resolve a handoff/mention target without accepting client-supplied accounts."""
    try:
        safe_id(principal)
        records = json.loads((CONFIG / "identities.json").read_text())
        matches = [registry_identity(record) for record in records.values()
                   if record.get("agent") == principal and record.get("active") is True]
        matches = [record for record in matches if record and record["role"] == "client"]
        if matches and all(record == matches[0] for record in matches):
            return matches[0]
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        pass
    return None


class AuthMiddleware:
    def __init__(self, app, role=None):
        self.app, self.role = app, role

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        authorizations = [value for key, value in scope.get("headers", []) if key.lower() == b"authorization"]
        who = identity(authorizations[0].decode("latin-1")) if len(authorizations) == 1 else None
        code = 401 if not who else 403 if self.role and who["role"] != self.role else None
        if code:
            audit("auth.denied", status=code, agent=who["agent"] if who else "unknown")
            await send({"type": "http.response.start", "status": code,
                        "headers": [(b"content-type", b"application/json"), (b"cache-control", b"no-store")]})
            error = {"error": {"code": "unauthorized" if code == 401 else "forbidden",
                               "message": "Authentication required" if code == 401 else "Operation is not permitted"}}
            if scope.get("path", "").startswith("/v1/space/"):
                error["x_protocol"] = SPACE_PROTOCOL
            await send({"type": "http.response.body", "body": json.dumps(error).encode()})
            return
        scope.setdefault("state", {})["identity"] = who

        async def private_send(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).append((b"cache-control", b"no-store"))
            await send(message)

        await self.app(scope, receive, private_send)


@contextmanager
def database(name):
    STATE.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(STATE / name, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def known_secrets():
    """Read only configured credential stores; never expose these values in diagnostics."""
    values = set()
    for path in SECRETS.rglob("*.token"):
        try:
            value = path.read_text().strip()
            if value:
                values.add(value)
        except OSError:
            continue
    database_config = Path(os.environ.get("BATON_DATABASE_CONFIG", str(SECRETS / "database.json")))
    if database_config.exists():
        try:
            password = json.loads(database_config.read_text())["password"]
            if not isinstance(password, str) or not password:
                raise ValueError("Invalid credential")
            values.add(password)
        except (OSError, ValueError, KeyError, TypeError):
            raise RuntimeError("Credential store is unreadable") from None
    auth = CODEX_HOME / "auth.json"
    if auth.exists():
        def walk(obj):
            if isinstance(obj, dict):
                for item in obj.values():
                    yield from walk(item)
            elif isinstance(obj, list):
                for item in obj:
                    yield from walk(item)
            elif isinstance(obj, str) and len(obj) >= 8:
                yield obj
        try:
            values.update(walk(json.loads(auth.read_text())))
        except (OSError, ValueError):
            # A malformed credential file cannot silently disable output filtering.
            raise RuntimeError("Credential store is unreadable") from None
    return values


def redact(text, extra=()):
    for value in sorted(known_secrets() | set(extra), key=len, reverse=True):
        if value:
            text = text.replace(value, "[REDACTED]")
    return text
