# SPDX-License-Identifier: Apache-2.0
"""Active health checks use a fake cgroup tree, a temporary SQLite state and a synthetic CLI only."""

import asyncio
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from bridge import worker_health
from bridge.failures import Failure, FailureError

PROBE_1 = "11111111-1111-4111-8111-111111111111"
PROBE_2 = "22222222-2222-4222-8222-222222222222"
PROBE_3 = "33333333-3333-4333-8333-333333333333"
SECRET = "SYNTHETIC-HEALTH-SECRET"


@pytest.fixture
def health_setup(modules, tmp_path):
    common, server, worker = modules
    cgroup, proc = tmp_path / "cgroup", tmp_path / "proc"
    cgroup.mkdir()
    for name, value in {"pids.current": "4", "pids.max": "128", "pids.events": "max 49\n",
                        "memory.current": "100", "memory.max": "1000",
                        "memory.stat": "anon 80\ninactive_file 0\npgmajfault 5\n",
                        "memory.events": "high 0\nmax 0\noom 0\noom_kill 0\n",
                        "cpu.stat": "usage_usec 1000\nuser_usec 600\nsystem_usec 400\nnr_periods 10\n"
                                    "nr_throttled 1\nthrottled_usec 20\n",
                        "io.stat": "8:0 rbytes=4096 wbytes=2 rios=3 wios=4 dbytes=0 dios=0\n"
                                   "8:16 rbytes=1 wbytes=0 rios=1 wios=0 dbytes=0 dios=0\n"}.items():
        (cgroup / name).write_text(value)
    (proc / "self/fd").mkdir(parents=True)
    (proc / "self/status").write_text("Name:\ttest\nThreads:\t4\n")
    now = [1000.0]
    executor = worker.CodexExecutor("codex", cgroup_root=cgroup)
    health = worker_health.WorkerHealth(executor, cgroup_root=cgroup, proc_root=proc,
                                        clock=lambda: now[0], resource_interval=30,
                                        canary_interval=900, canary_timeout=30)
    return SimpleNamespace(common=common, server=server, worker=worker, module=worker_health,
                           health=health, executor=executor, cgroup=cgroup, proc=proc, now=now)


def fake_cli(monkeypatch, setup, outcome=worker_health.CANARY_RESULT):
    calls = []

    async def run(prompt, process_key, *, timeout, **kwargs):
        assert setup.executor.lock.locked()
        assert prompt == setup.module.CANARY_PROMPT
        assert timeout <= 30 and kwargs == {"health_probe": True}
        with setup.common.database("executions.sqlite3") as db:
            row = db.execute("SELECT status FROM health_probes WHERE id=?", (process_key,)).fetchone()
            assert row and row["status"] == "running"  # Durable intent precedes effect.
        calls.append(process_key)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(setup.executor, "run_backend", run)
    return calls


@pytest.mark.asyncio
async def test_unknown_stale_and_missing_samples_never_report_healthy(health_setup, monkeypatch):
    setup = health_setup
    fake_cli(monkeypatch, setup)
    assert setup.health.snapshot()["status"] == "unknown"
    setup.health.sample_resources()
    assert setup.health.snapshot()["status"] == "unknown"
    await setup.health.run_canary(PROBE_1)
    assert setup.health.snapshot()["status"] == "healthy"
    setup.now[0] += 61
    assert setup.health.snapshot()["resources"]["status"] == "unknown"
    setup.health.sample_resources()
    setup.now[0] += 901
    setup.health.sample_resources()
    assert setup.health.snapshot()["canary"]["code"] == "WORKER_CANARY_STALE"
    assert setup.health.snapshot()["status"] != "healthy"
    (setup.cgroup / "pids.current").unlink()
    setup.health.sample_resources()
    assert setup.health.snapshot()["resources"]["status"] == "unknown"


@pytest.mark.asyncio
async def test_auth_failure_is_active_alert_and_only_success_recovers(health_setup, monkeypatch, capsys):
    setup = health_setup
    setup.health.sample_resources()
    calls = fake_cli(monkeypatch, setup, FailureError(Failure("CODEX_AUTH_REFRESH_FAILED")))
    await setup.health.run_canary(PROBE_1)
    assert calls == [PROBE_1]
    assert setup.health.snapshot()["status"] == "error"
    async with setup.executor.lock:
        await setup.health.run_canary(PROBE_2)
    busy = setup.health.snapshot()
    assert busy["canary"]["check_status"] == "busy"
    assert busy["status"] == "error"  # Skipping must not hide known auth failure.
    assert calls == [PROBE_1]
    events = [json.loads(line) for line in (setup.common.STATE / "audit.jsonl").read_text().splitlines()]
    assert any(event["event"] == "worker.auth_failed" and event["level"] == "error" for event in events)
    fake_cli(monkeypatch, setup, SECRET)
    await setup.health.run_canary(PROBE_2)
    assert setup.health.snapshot()["canary"]["code"] == "WORKER_CANARY_INVALID_RESULT"
    fake_cli(monkeypatch, setup)
    await setup.health.run_canary(PROBE_3)
    assert setup.health.snapshot()["status"] == "healthy"
    assert SECRET not in capsys.readouterr().out
    assert SECRET not in (setup.common.STATE / "audit.jsonl").read_text()


@pytest.mark.asyncio
async def test_resource_events_require_fresh_execution_before_green(health_setup, monkeypatch):
    setup = health_setup
    fake_cli(monkeypatch, setup)
    setup.health.sample_resources()
    await setup.health.run_canary(PROBE_1)
    assert setup.health.snapshot()["status"] == "healthy"  # Old cumulative events are not a new failure.
    prior_recoveries = (setup.common.STATE / "audit.jsonl").read_text().count("worker.health_recovered")
    (setup.cgroup / "pids.events").write_text("max 50\n")
    setup.health.sample_resources()
    assert setup.health.snapshot()["resources"]["code"] == "WORKER_RESOURCE_EXHAUSTED"
    setup.health.sample_resources()
    assert setup.health.snapshot()["status"] != "healthy"
    assert setup.health.snapshot()["canary"]["code"] == "WORKER_CANARY_REQUIRED"
    assert (setup.common.STATE / "audit.jsonl").read_text().count("worker.health_recovered") == prior_recoveries
    await setup.health.run_canary(PROBE_2)
    assert setup.health.snapshot()["status"] == "healthy"


@pytest.mark.asyncio
async def test_canary_probe_id_replay_and_interrupted_probe_never_rerun(health_setup, monkeypatch):
    setup = health_setup
    calls = fake_cli(monkeypatch, setup)
    await setup.health.run_canary(PROBE_1)
    await setup.health.run_canary(PROBE_1)
    assert calls == [PROBE_1]
    with setup.common.database("executions.sqlite3") as db:
        db.execute("INSERT INTO health_probes(id,status,started) VALUES (?,?,?)", (PROBE_2, "running", setup.now[0]))
    setup.module.init_health_db(recover=True)
    await setup.health.run_canary(PROBE_2)
    assert calls == [PROBE_1]
    assert setup.health.snapshot()["canary"]["code"] == "WORKER_CANARY_INTERRUPTED"
    assert setup.health.snapshot()["status"] != "healthy"


@pytest.mark.asyncio
async def test_inflight_recheck_does_not_hide_known_auth_failure(health_setup, monkeypatch):
    setup = health_setup
    setup.health.sample_resources()
    fake_cli(monkeypatch, setup, FailureError(Failure("CODEX_AUTH_UNAUTHORIZED")))
    await setup.health.run_canary(PROBE_1)
    started, finish = asyncio.Event(), asyncio.Event()

    async def pending(*args, **kwargs):
        started.set()
        await finish.wait()
        return setup.module.CANARY_RESULT

    monkeypatch.setattr(setup.executor, "run_backend", pending)
    task = asyncio.create_task(setup.health.run_canary(PROBE_2))
    await asyncio.wait_for(started.wait(), timeout=1)
    try:
        current = setup.health.snapshot()
        assert current["status"] == "error" and current["code"] == "CODEX_AUTH_UNAUTHORIZED"
        assert current["canary"]["check_status"] == "checking"
    finally:
        finish.set()
        await task
    assert setup.health.snapshot()["status"] == "healthy"


@pytest.mark.asyncio
async def test_cancelled_probe_is_durable_interrupted_and_unlocks(health_setup, monkeypatch):
    setup = health_setup
    started = asyncio.Event()

    async def pending(*args, **kwargs):
        started.set()
        await asyncio.Future()

    monkeypatch.setattr(setup.executor, "run_backend", pending)
    task = asyncio.create_task(setup.health.run_canary(PROBE_1))
    await asyncio.wait_for(started.wait(), timeout=1)
    with setup.common.database("executions.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM health_probes WHERE status='running'").fetchone()[0] == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not setup.executor.lock.locked()
    with setup.common.database("executions.sqlite3") as db:
        assert db.execute("SELECT status FROM health_probes WHERE id=?", (PROBE_1,)).fetchone()[0] == "interrupted"


@pytest.mark.asyncio
async def test_busy_check_does_not_create_probe_or_touch_business_executions(health_setup, monkeypatch):
    setup = health_setup
    calls = fake_cli(monkeypatch, setup)
    with setup.common.database("executions.sqlite3") as db:
        db.execute("INSERT INTO executions(id,digest,status,backend) VALUES ('working','digest','running','codex')")
    async with setup.executor.lock:
        await setup.health.run_canary(PROBE_1)
    assert setup.health.snapshot()["canary"]["status"] == "busy"
    assert calls == []
    with setup.common.database("executions.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM health_probes").fetchone()[0] == 0
        assert db.execute("SELECT status FROM executions WHERE id='working'").fetchone()[0] == "running"
    await setup.health.run_canary(PROBE_1)
    assert calls == [PROBE_1]


@pytest.mark.asyncio
async def test_replaying_old_success_does_not_clear_an_untested_busy_check(health_setup, monkeypatch):
    setup = health_setup
    calls = fake_cli(monkeypatch, setup)
    setup.health.sample_resources()
    await setup.health.run_canary(PROBE_1)
    async with setup.executor.lock:
        await setup.health.run_canary(PROBE_2)
    await setup.health.run_canary(PROBE_1)
    assert calls == [PROBE_1]
    assert setup.health.snapshot()["status"] == "busy"


@pytest.mark.asyncio
async def test_background_loops_check_idle_worker_without_business_requests(health_setup, monkeypatch):
    setup = health_setup
    calls = fake_cli(monkeypatch, setup)
    setup.health.resource_interval = 0.01
    setup.health.canary_interval = 0.02
    setup.health.start()
    try:
        for _ in range(50):
            if len(calls) >= 2:
                break
            await asyncio.sleep(0.01)
    finally:
        await setup.health.stop()
    assert len(calls) >= 2
    assert len(set(calls)) == len(calls)
    assert setup.health.snapshot()["status"] == "healthy"
    with setup.common.database("executions.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_clock_rollback_cannot_make_previous_success_green(health_setup, monkeypatch):
    setup = health_setup
    fake_cli(monkeypatch, setup)
    setup.health.sample_resources()
    await setup.health.run_canary(PROBE_1)
    setup.now[0] -= 1
    assert setup.health.snapshot()["status"] == "unknown"


@pytest.mark.asyncio
async def test_canary_uses_the_shared_command_sandbox_and_lock(health_setup, fake_codex):
    setup = health_setup
    fake_codex(result=setup.module.CANARY_RESULT)
    await setup.health.run_canary(PROBE_1)
    invocation = json.loads((setup.common.CODEX_HOME / "invocation.json").read_text())
    command = ["codex", *invocation["argv"]]
    assert command[:3] == ["codex", "exec", "--json"]
    assert 'default_permissions="baton"' in command and 'approval_policy="never"' in command
    assert any("network={enabled=false}" in part for part in command)
    assert setup.health.snapshot()["canary"]["status"] == "healthy"
    with setup.common.database("executions.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 0
    with pytest.raises(RuntimeError, match="lock"):
        await setup.executor.run_backend(setup.module.CANARY_PROMPT, PROBE_2, health_probe=True)


@pytest.mark.asyncio
async def test_a_business_task_blocks_the_canary_on_the_same_lock(health_setup, fake_codex):
    setup = health_setup
    fake_codex(steps=[{"delay": 0.3}], result=setup.module.CANARY_RESULT)
    business = asyncio.create_task(_run_business(setup))
    for _ in range(100):
        if setup.executor.lock.locked():
            break
        await asyncio.sleep(0.01)
    await setup.health.run_canary(PROBE_1)
    assert setup.health.snapshot()["canary"]["check_status"] == "busy"
    await business
    await setup.health.run_canary(PROBE_1)
    assert setup.health.snapshot()["canary"]["tested"] is True


async def _run_business(setup):
    async with setup.executor.lock:
        return await setup.executor.run_backend("synthetic task", "44444444-4444-4444-8444-444444444444",
                                                timeout=5)


def test_resource_sample_includes_cgroup_cpu_and_io_but_they_are_not_required(health_setup):
    setup = health_setup
    state = setup.health.sample_resources()
    assert state["cpu"] == {"usage_usec": 1000, "nr_throttled": 1, "throttled_usec": 20}
    assert state["io"] == {"rbytes": 4097, "rios": 4}
    (setup.cgroup / "io.stat").unlink()
    (setup.cgroup / "cpu.stat").write_text("garbage\n")
    state = setup.health.sample_resources()
    assert state["status"] == "healthy"
    assert state["cpu"] == {"usage_usec": None, "nr_throttled": None, "throttled_usec": None}
    assert state["io"] == {"rbytes": None, "rios": None}


def test_resource_pressure_is_proactive_without_business_task(health_setup):
    setup = health_setup
    (setup.cgroup / "pids.current").write_text("110")
    setup.health.sample_resources()
    assert setup.health.snapshot()["resources"]["code"] == "WORKER_PID_PRESSURE"
    (setup.cgroup / "pids.current").write_text("4")
    (setup.cgroup / "memory.current").write_text("900")
    setup.health.sample_resources()
    assert setup.health.snapshot()["resources"]["code"] == "WORKER_MEMORY_PRESSURE"


def test_memory_limit_events_without_oom_are_pressure_not_exhaustion(health_setup):
    setup = health_setup
    setup.health.sample_resources()
    (setup.cgroup / "memory.events").write_text("high 0\nmax 1\noom 0\noom_kill 0\n")
    setup.health.sample_resources()
    state = setup.health.snapshot()["resources"]
    assert state["status"] == "degraded" and state["code"] == "WORKER_MEMORY_PRESSURE"
    assert state["memory"]["oom_total"] == 0 and state["memory"]["max_total"] == 1


@pytest.mark.asyncio
async def test_high_cache_low_working_set_preserves_valid_canary(health_setup, monkeypatch):
    setup = health_setup
    calls = fake_cli(monkeypatch, setup)
    setup.health.sample_resources()
    await setup.health.run_canary(PROBE_1)
    assert setup.health.snapshot()["status"] == "healthy"
    (setup.cgroup / "memory.current").write_text("950")
    (setup.cgroup / "memory.stat").write_text("anon 80\nfile 850\ninactive_file 850\nshmem 10\nkernel 10\n")
    setup.health.sample_resources()
    state = setup.health.snapshot()
    assert state["status"] == "healthy"
    assert state["resources"]["code"] == "WORKER_HEALTHY"
    assert state["resources"]["memory"] == {
        "current": 950, "working_set": 100, "anon": 80, "limit": 1000,
        "oom_total": 0, "oom_kill_total": 0, "high_total": 0, "max_total": 0,
    }
    assert state["canary"]["probe_id"] == PROBE_1 and calls == [PROBE_1]
    with setup.common.database("executions.sqlite3") as db:
        assert db.execute("SELECT value FROM health_flags WHERE key='canary_after'").fetchone() is None
    published = json.loads((setup.common.STATE / "worker-health.json").read_text())
    assert published["state"]["resources"]["memory"] == state["resources"]["memory"]
    assert setup.module.check_snapshot(now=setup.now[0])["status"] == "healthy"


@pytest.mark.parametrize(("inactive_file", "working_set", "status"), [
    (101, 849, "healthy"), (100, 850, "degraded"), (99, 851, "degraded"),
])
def test_working_set_pressure_threshold_includes_equality(health_setup, inactive_file, working_set, status):
    setup = health_setup
    (setup.cgroup / "memory.current").write_text("950")
    # Low anon must not conceal other charged working-set memory.
    (setup.cgroup / "memory.stat").write_text(f"anon 100\ninactive_file {inactive_file}\n")
    state = setup.health.sample_resources()
    assert state["status"] == status
    assert state["memory"]["working_set"] == working_set
    assert state["memory"]["anon"] == 100
    assert state["code"] == ("WORKER_HEALTHY" if status == "healthy" else "WORKER_MEMORY_PRESSURE")


def test_inactive_file_above_current_clamps_working_set_to_zero(health_setup):
    setup = health_setup
    (setup.cgroup / "memory.stat").write_text("anon 80\ninactive_file 150\n")
    state = setup.health.sample_resources()
    assert state["status"] == "healthy"
    assert state["memory"]["current"] == 100
    assert state["memory"]["working_set"] == 0 and state["memory"]["anon"] == 80


@pytest.mark.parametrize(("event", "status", "code"), [
    ("max", "degraded", "WORKER_MEMORY_PRESSURE"),
    ("high", "degraded", "WORKER_MEMORY_PRESSURE"),
    ("oom", "error", "WORKER_MEMORY_EXHAUSTED"),
    ("oom_kill", "error", "WORKER_MEMORY_EXHAUSTED"),
])
def test_memory_event_deltas_still_alert_with_low_working_set(health_setup, event, status, code):
    setup = health_setup
    (setup.cgroup / "memory.current").write_text("950")
    (setup.cgroup / "memory.stat").write_text("anon 30\ninactive_file 900\n")
    assert setup.health.sample_resources()["status"] == "healthy"
    counts = {"high": 0, "max": 0, "oom": 0, "oom_kill": 0}
    counts[event] = 1
    (setup.cgroup / "memory.events").write_text("".join(f"{name} {value}\n" for name, value in counts.items()))
    state = setup.health.sample_resources()
    assert state["status"] == status and state["code"] == code
    assert state["memory"]["working_set"] == 50
    assert state["memory"][event + "_total"] == 1
    with setup.common.database("executions.sqlite3") as db:
        assert db.execute("SELECT value FROM health_flags WHERE key='canary_after'").fetchone() is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_stat", [
    b"", b"anon 80\n", b"inactive_file 0\n",
    b"anon -1\ninactive_file 0\n", b"anon 80\ninactive_file -1\n",
    b"anon 80\ninactive_file 0.5\n", b"anon 80 extra\ninactive_file 0\n",
    b"anon 80\ninactive_file 0\ninactive_file 1\n",
    b"anon 80\ninactive_file 0\nfile SYNTHETIC-HEALTH-SECRET\n", b"\xff",
], ids=["empty", "missing_inactive", "missing_anon", "negative_anon", "negative_inactive",
        "fractional_inactive", "extra_column", "duplicate_key", "malformed_other_counter", "invalid_encoding"])
async def test_malformed_memory_stat_fails_closed_without_error_text(health_setup, monkeypatch, capsys, invalid_stat):
    setup = health_setup
    fake_cli(monkeypatch, setup)
    setup.health.sample_resources()
    await setup.health.run_canary(PROBE_1)
    assert setup.health.snapshot()["status"] == "healthy"
    (setup.cgroup / "memory.stat").write_bytes(invalid_stat)
    setup.health.sample_resources()
    state = setup.health.snapshot()
    assert state["status"] == "unknown"
    assert state["resources"]["code"] == "WORKER_HEALTH_UNAVAILABLE"
    assert state["resources"]["fresh"] is False
    assert setup.module.check_snapshot(now=setup.now[0])["status"] == "unknown"
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err
    assert SECRET not in (setup.common.STATE / "audit.jsonl").read_text()
    assert SECRET not in (setup.common.STATE / "worker-health.json").read_text()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing_file", "read_error"])
async def test_memory_stat_read_failure_does_not_reuse_old_healthy_sample(health_setup, monkeypatch, capsys, failure):
    setup = health_setup
    fake_cli(monkeypatch, setup)
    setup.health.sample_resources()
    await setup.health.run_canary(PROBE_1)
    assert setup.health.snapshot()["status"] == "healthy"
    path = setup.cgroup / "memory.stat"
    if failure == "missing_file":
        path.unlink()
    else:
        original = Path.read_text

        def fail_stat(self, *args, **kwargs):
            if self == path:
                raise PermissionError(SECRET)
            return original(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", fail_stat)
    state = setup.health.sample_resources()
    assert state["status"] == "unknown" and state["fresh"] is False
    assert setup.health.snapshot()["status"] == "unknown"
    assert setup.module.check_snapshot(now=setup.now[0])["status"] == "unknown"
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err
    assert SECRET not in (setup.common.STATE / "audit.jsonl").read_text()
    assert SECRET not in (setup.common.STATE / "worker-health.json").read_text()


def test_snapshot_reconstruction_keeps_only_safe_fields(health_setup):
    setup = health_setup
    forged = {"status": "healthy", "code": "WORKER_HEALTHY", "message": SECRET,
              "resources": {"status": "healthy", "code": "WORKER_HEALTHY", "fresh": True},
              "canary": {"status": "error", "code": "CODEX_AUTH_UNAUTHORIZED", "fresh": True,
                         "tested": True, "message": SECRET, "stderr": SECRET}}
    safe = setup.module.sanitize_health_snapshot(forged)
    assert safe["status"] == "error" and safe["code"] == "CODEX_AUTH_UNAUTHORIZED"
    assert SECRET not in json.dumps(safe)
    forged["canary"] = {"status": "healthy", "code": "WORKER_HEALTHY", "fresh": False}
    assert setup.module.sanitize_health_snapshot(forged)["status"] != "healthy"


def test_intervals_are_configurable_and_canary_timeout_is_capped(health_setup, monkeypatch):
    setup = health_setup
    defaults = setup.module.WorkerHealth(setup.executor)
    assert (defaults.resource_interval, defaults.canary_interval, defaults.canary_timeout) == (30, 900, 30)
    monkeypatch.setenv("BATON_RESOURCE_SAMPLE_SECONDS", "5")
    monkeypatch.setenv("BATON_CANARY_INTERVAL_SECONDS", "20")
    monkeypatch.setenv("BATON_CANARY_TIMEOUT_SECONDS", "999")
    health = setup.module.WorkerHealth(setup.executor)
    assert (health.resource_interval, health.canary_interval, health.canary_timeout) == (5, 20, 30)


@pytest.mark.asyncio
async def test_container_check_is_read_only_and_rejects_stale_or_forged_state(health_setup, monkeypatch, capsys):
    setup = health_setup
    fake_cli(monkeypatch, setup)
    setup.health.sample_resources()
    await setup.health.run_canary(PROBE_1)
    monkeypatch.setattr(setup.module, "time", SimpleNamespace(time=lambda: setup.now[0]))
    capsys.readouterr()
    listing = sorted((path.name, path.stat().st_mtime_ns) for path in setup.common.STATE.iterdir())
    assert setup.module.main(["check"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "healthy"
    assert listing == sorted((path.name, path.stat().st_mtime_ns) for path in setup.common.STATE.iterdir())
    setup.now[0] += 61
    assert setup.module.main(["check"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "unknown"
    path = setup.common.STATE / "worker-health.json"
    path.write_text(SECRET)
    assert setup.module.main(["check"]) == 1
    assert SECRET not in capsys.readouterr().out
    path.unlink()
    assert setup.module.main(["check"]) == 1


def test_container_check_runs_as_a_module_without_state_and_fails(tmp_path):
    root = Path(__file__).parents[1]
    result = subprocess.run([sys.executable, "-m", "bridge.worker_health", "check"], cwd=root, capture_output=True,
                            text=True, env={**os.environ, "BATON_STATE": str(tmp_path / "missing")})
    assert result.returncode == 1 and json.loads(result.stdout)["status"] == "unknown"
    assert not (tmp_path / "missing").exists()


def test_compose_healthcheck_uses_the_read_only_check():
    compose = (Path(__file__).parents[1] / "deploy/compose.yaml").read_text()
    codex = re.search(r"\n  codex:\n(.*?)\n  database:", compose, re.S).group(1)
    assert "test: [CMD, python, -m, bridge.worker_health, check]" in codex
    for setting in ("interval: 30s", "timeout: 5s", "start_period: 90s", "retries: 3"):
        assert setting in codex
    assert "pids_limit: 128" in codex


def test_gateway_does_not_wait_for_the_worker_canary_but_the_worker_stays_strictly_healthchecked():
    compose = (Path(__file__).parents[1] / "deploy/compose.yaml").read_text()
    gateway = re.search(r"\n  gateway:\n(.*?)\n  bridge:\n", compose, re.S).group(1)
    depends = re.search(r"depends_on:\n((?:      .*\n?)+)", gateway).group(1)
    # A cold-start canary timeout must not keep the whole stack from starting.
    assert "codex: {condition: service_started}" in depends
    assert "bridge: {condition: service_healthy}" in depends
    codex = re.search(r"\n  codex:\n(.*?)\n  database:", compose, re.S).group(1)
    assert "test: [CMD, python, -m, bridge.worker_health, check]" in codex
    assert not any(re.search(r"condition:\s*service_healthy", line) and "codex" in line
                   for line in compose.splitlines())


@pytest.mark.parametrize("backend", ["demo", "codex"])
def test_worker_http_health_is_authenticated_and_starts_background_checks(health_setup, monkeypatch, backend):
    setup = health_setup
    monkeypatch.setenv("WORKER_MODE", backend)
    calls = fake_cli(monkeypatch, setup)
    setup.health.backend = backend
    monkeypatch.setattr(setup.worker, "CodexExecutor", lambda _backend: setup.executor)
    monkeypatch.setattr(setup.worker, "WorkerHealth", lambda executor, backend: setup.health)
    monkeypatch.setattr(setup.common, "identity", lambda auth: {
        "Bearer dispatcher": {"agent": "health-check", "role": "dispatcher"},
        "Bearer client": {"agent": "someone", "role": "client"},
    }.get(auth))
    headers = {"Authorization": "Bearer dispatcher"}
    with TestClient(setup.worker.create_app()) as client:
        assert client.get("/healthz").status_code == 401
        assert client.get("/healthz", headers={"Authorization": "Bearer client"}).status_code == 403
        response = client.get("/healthz", headers=headers)
        for _ in range(50):
            if response.status_code == 200:
                break
            response = client.get("/healthz", headers=headers)
            time.sleep(0.02)
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "healthy" and body["backend"] == backend
        assert body["canary"]["phases"] is None
    assert len(calls) >= 1 and not setup.health._tasks


def test_worker_http_health_is_503_until_a_canary_has_run(health_setup, monkeypatch):
    setup = health_setup
    monkeypatch.setattr(setup.worker, "CodexExecutor", lambda _backend: setup.executor)
    monkeypatch.setattr(setup.worker, "WorkerHealth", lambda executor, backend: setup.health)
    monkeypatch.setattr(setup.health, "start", lambda: None)
    monkeypatch.setattr(setup.common, "identity", lambda auth: (
        {"agent": "health-check", "role": "dispatcher"} if auth == "Bearer dispatcher" else None))
    with TestClient(setup.worker.create_app()) as client:
        response = client.get("/healthz", headers={"Authorization": "Bearer dispatcher"})
        assert response.status_code == 503 and response.json()["status"] == "unknown"


def test_demo_backend_is_healthy_only_through_a_synthetic_canary_and_never_runs_the_cli(health_setup, monkeypatch):
    setup = health_setup

    def forbidden(*args, **kwargs):
        pytest.fail("Demo must never spawn a process")

    monkeypatch.setattr(setup.worker.asyncio, "create_subprocess_exec", forbidden)
    monkeypatch.setattr(setup.worker, "WorkerHealth", lambda executor, backend: setup.module.WorkerHealth(
        executor, backend=backend, cgroup_root=setup.cgroup, proc_root=setup.proc))
    monkeypatch.setattr(setup.common, "identity", lambda auth: (
        {"agent": "health-check", "role": "dispatcher"} if auth == "Bearer dispatcher" else None))
    headers = {"Authorization": "Bearer dispatcher"}
    with TestClient(setup.worker.create_app()) as client:
        response = client.get("/healthz", headers=headers)
        for _ in range(100):
            if response.status_code == 200:
                break
            time.sleep(0.02)
            response = client.get("/healthz", headers=headers)
        assert response.status_code == 200 and response.json()["backend"] == "demo"
        assert response.json()["canary"]["phases"] is None
