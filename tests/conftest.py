# SPDX-License-Identifier: Apache-2.0
"""Synthetic identities and isolated PostgreSQL databases; never real accounts."""

import importlib
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
    monkeypatch.setenv("WORKER_MODE", "demo")
    monkeypatch.delenv("BATON_DATABASE_CONFIG", raising=False)
    from bridge import common, server, codex_agent
    importlib.reload(common)
    monkeypatch.setattr(common, "SECRETS", tmp_path / "secrets")
    monkeypatch.setattr(common, "CODEX_HOME", tmp_path / "codex-home")
    common.SECRETS.mkdir()
    (common.SECRETS / "bridge").mkdir()
    common.CODEX_HOME.mkdir()
    common.CONFIG.mkdir()
    # The old synthetic name still verifies generic credential redaction. No
    # Agent Mail process, request or dependency is involved.
    (common.SECRETS / "mail.token").write_text("synthetic-mail-credential")
    (common.SECRETS / "bridge/dispatcher.token").write_text("synthetic-dispatcher-credential")
    importlib.reload(server)
    importlib.reload(codex_agent)
    monkeypatch.setattr(codex_agent, "WORK_ROOT", tmp_path / "work")
    codex_agent.WORK_ROOT.mkdir()
    codex_agent.init_db()

    async def idle_worker():
        import asyncio
        await asyncio.Event().wait()

    # HTTP boundary tests drive dispatch explicitly; opening TestClient must
    # never send a request to any external worker.
    monkeypatch.setattr(server, "worker_loop", idle_worker)
    return common, server, codex_agent


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
            connection.execute(sql.SQL("REVOKE UPDATE, DELETE, TRUNCATE ON baton.x_audit FROM {}").format(role))
        config_path = tmp_path / "database.json"
        config_path.write_text(json.dumps({**app_config, "dbname": database}))
        config_path.chmod(0o600)
        monkeypatch.setenv("BATON_DATABASE_CONFIG", str(config_path))
        server = modules[1]
        server._STORE = server.SpaceStore.from_env()
        server._STORE.check_schema(server.SERVICE)
        yield server._STORE
    finally:
        modules[1]._STORE = None
        with psycopg.connect(**admin_config, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))
