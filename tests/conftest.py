# SPDX-License-Identifier: Apache-2.0
"""Synthetic identities and isolated PostgreSQL databases; never real accounts."""

import importlib
import hashlib
import json
import os
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def identity_prefix():
    return "test-" + uuid.uuid4().hex[:12]


@pytest.fixture
def modules(tmp_path, monkeypatch):
    monkeypatch.setenv("BATON_STATE", str(tmp_path / "data"))
    monkeypatch.setenv("BATON_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("PUBLIC_URL", "http://localhost:8080")
    monkeypatch.delenv("BATON_DATABASE_CONFIG", raising=False)
    from bridge import common, server
    importlib.reload(common)
    common.CONFIG.mkdir()
    importlib.reload(server)

    async def idle_loop():
        import asyncio
        await asyncio.Event().wait()

    # HTTP boundary tests must never start background delivery or escalation.
    monkeypatch.setattr(server, "webhook_loop", idle_loop)
    monkeypatch.setattr(server, "escalation_loop", idle_loop)
    return common, server


@pytest.fixture
def make_context(identity_prefix):
    def make(agent="test-client", request_id="synthetic-not-a-uuid", *, account=None, kind="agent"):
        return SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(
            state=SimpleNamespace(identity={"agent": identity_prefix + "-" + agent, "role": "client",
                                           "account": account or identity_prefix + "-" + agent,
                                           "kind": kind}),
            headers={"x-request-id": request_id},
        )))
    return make


@pytest.fixture
def register_identity(modules):
    """Register synthetic actors explicitly in the identity registry."""
    common = modules[0]
    def register(who):
        path = common.CONFIG / "identities.json"
        records = json.loads(path.read_text()) if path.exists() else {}
        token = "synthetic-" + uuid.uuid4().hex
        records[who["agent"]] = {**who, "active": True, "hash": hashlib.sha256(token.encode()).hexdigest()}
        path.write_text(json.dumps(records))
        assert common.identity("Bearer " + token) == who
        return token
    return register


@pytest.fixture
def pg_store(modules, tmp_path, monkeypatch):
    """Use a new database in the explicitly supplied local test PG cluster.

    The storage lane uses its own database concurrently. Never truncate or
    change that shared database: only this fixture's UUID database is removed.
    """
    import psycopg
    from psycopg import sql
    app_path = os.environ.get("BATON_TEST_DATABASE_CONFIG")
    admin_path = os.environ.get("BATON_TEST_DATABASE_ADMIN_CONFIG")
    if not app_path or not admin_path:
        pytest.skip("PostgreSQL integration NOT_RUN: set both BATON_TEST_DATABASE_*_CONFIG paths")
    app_config = json.loads(Path(app_path).read_text())
    admin_config = json.loads(Path(admin_path).read_text())
    database = "baton_bridge_" + uuid.uuid4().hex
    with psycopg.connect(**admin_config, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {} OWNER baton_owner").format(sql.Identifier(database)))
    try:
        local_admin = {**admin_config, "dbname": database}
        schema = Path(__file__).parents[1] / "bridge/space_schema.sql"
        with psycopg.connect(**local_admin) as connection:
            connection.execute("SET ROLE baton_owner")
            connection.execute(schema.read_text())
            role = sql.Identifier(app_config["user"])
            connection.execute(sql.SQL("GRANT USAGE ON SCHEMA baton TO {}").format(role))
            connection.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA baton TO {}").format(role))
            connection.execute(sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA baton TO {}").format(role))
            connection.execute(sql.SQL("REVOKE UPDATE, DELETE, TRUNCATE ON baton.x_audit FROM {}").format(role))
            connection.execute(sql.SQL("REVOKE ALL ON baton.x_claim_holder FROM {}").format(role))
            connection.execute(sql.SQL("GRANT SELECT, INSERT ON baton.x_claim_holder TO {}").format(role))
            connection.execute(sql.SQL("REVOKE ALL ON baton.x_delegation FROM {}").format(role))
            connection.execute(sql.SQL("GRANT SELECT, INSERT ON baton.x_delegation TO {}").format(role))
            connection.execute(sql.SQL("GRANT UPDATE (revoked_at, revoked_by) ON baton.x_delegation TO {}").format(role))
        config_path = tmp_path / "database.json"
        config_path.write_text(json.dumps({**app_config, "dbname": database}))
        config_path.chmod(0o600)
        monkeypatch.setenv("BATON_DATABASE_CONFIG", str(config_path))
        server = modules[1]
        server._STORE = None
        server.get_store()
        server._STORE.check_schema(server.SERVICE)
        yield server._STORE
    finally:
        modules[1]._STORE = None
        with psycopg.connect(**admin_config, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))


@pytest.fixture
def legacy_codex(pg_store):
    """Rewrite a task row into the shape the removed built-in worker left behind (nothing writes it any more)."""
    def convert(task_id, *, state="queued", dispatched=False, backend=None, error=None):
        with pg_store._transaction(write=True) as conn:
            conn.execute("""UPDATE baton.tasks SET execution_mode='codex',gate_mode='worker_success',reviewers='[]'::jsonb,
                scopes='[{"kind":"path","value":""}]'::jsonb,assignee=NULL,state=%s,backend=%s,error=%s,
                dispatch_started_at=CASE WHEN %s THEN clock_timestamp() END WHERE id=%s""",
                         (state, backend, error, dispatched, task_id))
        return task_id
    return convert


@pytest.fixture
def approver():
    """Give a principal an approver delegation granted by the space owner `by`, through the API path."""
    from bridge.space_store import Actor

    def actor(who):
        return Actor(who["agent"], who["account"], who["kind"]) if isinstance(who, dict) else who

    def grant(store, delegate, by, **kwargs):
        return store.delegation_create(actor(by), str(uuid.uuid4()), actor(delegate), space_id=kwargs.get("space_id"),
                                       expires_at=kwargs.get("expires_at"), may_delegate=kwargs.get("may_delegate", False),
                                       reason="DECISIONS 291")
    return grant
