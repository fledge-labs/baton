# SPDX-License-Identifier: Apache-2.0
"""All test identities, credentials and state are synthetic and temporary."""

import importlib
from types import SimpleNamespace

import pytest


@pytest.fixture
def modules(tmp_path, monkeypatch):
    monkeypatch.setenv("BATON_STATE", str(tmp_path / "data"))
    monkeypatch.setenv("BATON_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("PUBLIC_URL", "http://localhost:8080")
    monkeypatch.setenv("WORKER_MODE", "demo")
    from bridge import common, server, codex_agent
    importlib.reload(common)
    monkeypatch.setattr(common, "SECRETS", tmp_path / "secrets")
    monkeypatch.setattr(common, "CODEX_HOME", tmp_path / "codex-home")
    common.SECRETS.mkdir()
    common.CODEX_HOME.mkdir()
    common.CONFIG.mkdir()
    for name in ("mail", "dispatcher"):
        (common.SECRETS / (name + ".token")).write_text("synthetic-" + name + "-credential")
    importlib.reload(server)
    importlib.reload(codex_agent)
    monkeypatch.setattr(codex_agent, "WORK_ROOT", tmp_path / "work")
    codex_agent.WORK_ROOT.mkdir()
    server.init_db()
    codex_agent.init_db()
    return common, server, codex_agent


@pytest.fixture
def make_context():
    def make(agent="test-client", request_id="synthetic-not-a-uuid"):
        return SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(
            state=SimpleNamespace(identity={"agent": agent, "role": "client"}),
            headers={"x-request-id": request_id},
        )))
    return make
