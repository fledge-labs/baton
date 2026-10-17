# SPDX-License-Identifier: Apache-2.0
"""Per-phase timing of Codex executions, using a synthetic CLI and a fake cgroup tree."""

import asyncio
import inspect
import json
import sqlite3
import time
import uuid

import pytest
from starlette.testclient import TestClient
from test_worker_failures import Queue, context

from bridge import phases as phase_module
from bridge import worker_health

SECRET = "SYNTHETIC-PHASE-PRIVATE-TEXT"
PROBE_1 = "11111111-1111-4111-8111-111111111111"
PROBE_2 = "22222222-2222-4222-8222-222222222222"
NUMBERS = ("spawn_ms", "first_event_ms", "first_item_ms", "turn_end_ms", "exit_ms")
COUNTERS = ("cpu_usage_usec", "cpu_nr_throttled", "cpu_throttled_usec", "io_rbytes", "io_rios", "pgmajfault")


@pytest.fixture
def cgroup(tmp_path):
    root = tmp_path / "cgroup"
    root.mkdir()
    (root / "cpu.stat").write_text("usage_usec 1000\nuser_usec 1\nnr_throttled 2\nthrottled_usec 30\n")
    (root / "io.stat").write_text("8:0 rbytes=100 wbytes=7 rios=10 wios=1\n8:16 rbytes=50 wbytes=0 rios=5 wios=0\n")
    (root / "memory.stat").write_text("anon 1\ninactive_file 0\npgmajfault 7\n")
    return root


@pytest.fixture
def executor(modules, cgroup):
    return modules[2].CodexExecutor("codex", cgroup_root=cgroup)


def turn(*delays):
    events = ({"type": "thread.started", "thread_id": "synthetic"}, {"type": "turn.started"},
              {"type": "item.completed", "item": {"type": "agent_message", "text": SECRET}},
              {"type": "turn.completed", "usage": {}})
    delays = delays or (0.05, 0, 0.05, 0.05)
    return [{"delay": delay, "event": event} for delay, event in zip(delays, events)]


def row(common, table="executions"):
    with common.database("executions.sqlite3") as db:
        value = db.execute(f"SELECT phases FROM {table}").fetchone()[0]
    return json.loads(value)


def audit_lines(common):
    return [json.loads(line) for line in (common.STATE / "audit.jsonl").read_text().splitlines()]


def phase_lines(common):
    return [line for line in audit_lines(common) if line["event"] == "worker.phases"]


async def run_business(executor, common, prompt="synthetic", **kwargs):
    stable = str(uuid.uuid4())
    queue = Queue()
    await executor.execute(context(prompt=prompt, stable=stable, task_id=str(uuid.uuid4())), queue)
    return stable, queue


@pytest.mark.asyncio
async def test_phases_are_ordered_and_stored_on_the_execution_row_and_audit(modules, fake_codex, executor, capsys):
    common, _, _ = modules
    fake_codex(steps=turn(), result="synthetic model result")
    stable, queue = await run_business(executor, common)
    phases = row(common)
    assert phases["version"] == 1 and phases["cold"] is True
    assert all(type(phases[key]) is int for key in NUMBERS)
    assert (phases["spawn_ms"] <= phases["first_event_ms"] <= phases["first_item_ms"]
            <= phases["turn_end_ms"] <= phases["exit_ms"])
    assert phases["first_event_ms"] >= 40 and phases["turn_end_ms"] >= 130
    assert phases["first_event_type"] == "thread.started" and phases["turn_end_type"] == "turn.completed"
    lines = phase_lines(common)
    assert len(lines) == 1
    assert lines[0]["task_id"] == stable and lines[0]["backend"] == "codex" and lines[0]["phases"] == phases
    for text in (json.dumps(phases), (common.STATE / "audit.jsonl").read_text(), capsys.readouterr().out):
        assert SECRET not in text


@pytest.mark.asyncio
async def test_missing_phases_are_null_never_zero(modules, fake_codex, executor):
    common, _, _ = modules
    fake_codex(steps=[{"stderr": "ERROR: boom"}], result=None, exit=3)
    await run_business(executor, common)
    phases = row(common)
    assert phases["spawn_ms"] is not None and phases["exit_ms"] is not None
    for key in ("first_event_ms", "first_event_type", "first_item_ms", "turn_end_ms", "turn_end_type"):
        assert phases[key] is None
    assert len(phase_lines(common)) == 1


@pytest.mark.asyncio
async def test_failed_turn_without_items_leaves_the_item_phase_null(modules, fake_codex, executor):
    common, _, _ = modules
    fake_codex(steps=[{"event": {"type": "thread.started"}},
                      {"event": {"type": "turn.failed", "error": {"message": SECRET}}}], result=None, exit=1)
    await run_business(executor, common)
    phases = row(common)
    assert phases["first_event_type"] == "thread.started" and phases["turn_end_type"] == "turn.failed"
    assert phases["first_item_ms"] is None and phases["turn_end_ms"] <= phases["exit_ms"]
    assert SECRET not in json.dumps(phases)


@pytest.mark.asyncio
async def test_timeout_keeps_the_phases_reached_and_records_the_kill(modules, fake_codex, executor):
    common, _, _ = modules
    worker_health.init_health_db()
    with common.database("executions.sqlite3") as db:
        db.execute("INSERT INTO health_probes(id,status,started) VALUES (?,'running',1)", (PROBE_1,))
    fake_codex(steps=[{"event": {"type": "thread.started"}}, {"hang": True}], result=None)
    async with executor.lock:
        with pytest.raises(TimeoutError):
            await executor.run_codex(worker_health.CANARY_PROMPT, PROBE_1, timeout=0.5, health_probe=True)
    phases = row(common, "health_probes")
    assert phases["first_event_type"] == "thread.started" and phases["first_event_ms"] < 500
    assert phases["first_item_ms"] is None and phases["turn_end_ms"] is None
    assert phases["exit_ms"] >= 400
    line = phase_lines(common)[0]
    assert line["probe_id"] == PROBE_1 and "task_id" not in line and line["phases"] == phases


@pytest.mark.asyncio
async def test_a_spawn_failure_still_records_a_row_with_null_phases(modules, fake_codex, executor, tmp_path, monkeypatch):
    common, _, _ = modules
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    await run_business(executor, common)
    phases = row(common)
    assert phases["cold"] is True and all(phases[key] is None for key in NUMBERS + COUNTERS)
    with common.database("executions.sqlite3") as db:
        assert db.execute("SELECT error_code FROM executions").fetchone()[0] == "CODEX_CONFIG_INVALID"


@pytest.mark.asyncio
async def test_cold_is_true_only_for_the_first_execution_since_start(modules, fake_codex, cgroup):
    common, _, worker = modules
    fake_codex(steps=turn(0, 0, 0, 0), result=worker_health.CANARY_RESULT)
    health_probe = {"health_probe": True}
    first, second = worker.CodexExecutor("codex", cgroup_root=cgroup), worker.CodexExecutor("codex", cgroup_root=cgroup)
    worker_health.init_health_db()
    for key in (PROBE_1, PROBE_2):
        with common.database("executions.sqlite3") as db:
            db.execute("INSERT INTO health_probes(id,status,started) VALUES (?,'running',1)", (key,))
    async with first.lock:
        await first.run_codex("canary", PROBE_1, **health_probe)
        await first.run_codex("canary", PROBE_2, **health_probe)
    async with second.lock:
        await second.run_codex("canary", PROBE_1, **health_probe)
    assert [line["phases"]["cold"] for line in phase_lines(common)] == [True, False, True]


@pytest.mark.asyncio
async def test_business_task_after_a_canary_is_not_cold(modules, fake_codex, executor):
    common, _, _ = modules
    fake_codex(steps=turn(0, 0, 0, 0), result="synthetic")
    worker_health.init_health_db()
    with common.database("executions.sqlite3") as db:
        db.execute("INSERT INTO health_probes(id,status,started) VALUES (?,'running',1)", (PROBE_1,))
    async with executor.lock:
        await executor.run_codex("canary", PROBE_1, health_probe=True)
    await run_business(executor, common)
    assert [line["phases"]["cold"] for line in phase_lines(common)] == [True, False]


@pytest.mark.asyncio
async def test_cgroup_counter_deltas_cover_only_the_execution(modules, fake_codex, executor, cgroup):
    common, _, _ = modules
    fake_codex(steps=[{"files": {
        str(cgroup / "cpu.stat"): "usage_usec 4500\nuser_usec 1\nnr_throttled 5\nthrottled_usec 130\n",
        str(cgroup / "io.stat"): "8:0 rbytes=1100 wbytes=9 rios=14 wios=1\n8:16 rbytes=60 wbytes=0 rios=6 wios=0\n",
        str(cgroup / "memory.stat"): "anon 1\ninactive_file 0\npgmajfault 19\n",
    }}], result="synthetic")
    await run_business(executor, common)
    phases = row(common)
    assert phases["cpu_usage_usec"] == 3500 and phases["cpu_nr_throttled"] == 3
    assert phases["cpu_throttled_usec"] == 100
    assert phases["io_rbytes"] == 1010 and phases["io_rios"] == 5
    assert phases["pgmajfault"] == 12


@pytest.mark.asyncio
async def test_unreadable_or_reset_counters_are_null_not_zero(modules, fake_codex, executor, cgroup):
    common, _, _ = modules
    (cgroup / "io.stat").unlink()
    fake_codex(steps=[{"files": {
        str(cgroup / "cpu.stat"): "usage_usec 10\nuser_usec 1\nnr_throttled 5\nthrottled_usec 31\n",
        str(cgroup / "memory.stat"): "this is not a counter file\n",
    }}], result="synthetic")
    await run_business(executor, common)
    phases = row(common)
    assert phases["cpu_usage_usec"] is None  # went backwards: the counter was reset
    assert phases["cpu_nr_throttled"] == 3 and phases["cpu_throttled_usec"] == 1
    assert phases["io_rbytes"] is None and phases["io_rios"] is None and phases["pgmajfault"] is None


def test_counter_parsing_requires_complete_well_formed_files(tmp_path):
    (tmp_path / "cpu.stat").write_text("usage_usec 5\nusage_usec 6\n")
    (tmp_path / "io.stat").write_text("8:0 rbytes=1 wbytes=2\n")
    assert set(phase_module.cgroup_counters(tmp_path).values()) == {None}
    (tmp_path / "io.stat").write_text("")
    counters = phase_module.cgroup_counters(tmp_path)
    assert counters["io_rbytes"] == 0 and counters["io_rios"] == 0
    assert phase_module.cgroup_counters(tmp_path / "missing") == dict.fromkeys(COUNTERS)


@pytest.mark.asyncio
async def test_event_types_are_closed_and_message_text_is_never_stored(modules, fake_codex, executor, capsys):
    common, _, _ = modules
    fake_codex(steps=[
        {"raw": "WARN not json " + SECRET},
        {"raw": json.dumps([SECRET])},
        {"event": {"type": "item.created " + SECRET, "message": SECRET}},
        {"event": {"type": ["unhashable", SECRET]}},
        {"event": {"type": "turn.completed", "message": SECRET}},
    ], result="synthetic")
    await run_business(executor, common)
    phases = row(common)
    assert phases["first_event_type"] == "item.other" and phases["turn_end_type"] == "turn.completed"
    assert phases["first_item_ms"] is not None
    text = json.dumps(phases) + (common.STATE / "audit.jsonl").read_text() + capsys.readouterr().out
    assert SECRET not in text
    with common.database("executions.sqlite3") as db:
        assert SECRET not in json.dumps([list(item) for item in db.execute("SELECT * FROM executions")])


@pytest.mark.asyncio
async def test_oversized_event_line_is_untyped_and_does_not_stop_timing(modules, fake_codex, executor):
    common, _, _ = modules
    fake_codex(steps=[{"raw": SECRET * 50_000}, {"event": {"type": "turn.completed"}}], result="synthetic")
    await run_business(executor, common)
    phases = row(common)
    assert phases["first_event_type"] == "other" and phases["turn_end_type"] == "turn.completed"
    assert SECRET not in json.dumps(phases)


def test_recorder_orders_marks_on_one_monotonic_clock():
    now = iter(range(0, 10_000_000_000, 1_000_000))
    recorder = phase_module.PhaseRecorder(False, lambda: next(now))
    recorder.event("thread.started")  # before spawn: ignored
    recorder.begin()
    recorder.spawned()
    recorder.event("thread.started")
    recorder.event("item.started")
    recorder.event("item.completed")
    recorder.event("turn.completed")
    recorder.event("turn.failed")  # only the first turn end counts
    recorder.exited()
    recorder.exited()
    phases = recorder.record({"cpu_usage_usec": 5}, {"cpu_usage_usec": 9})
    assert [phases[key] for key in NUMBERS] == [1, 2, 3, 4, 5]
    assert phases["first_event_type"] == "thread.started" and phases["turn_end_type"] == "turn.completed"
    assert phases["cpu_usage_usec"] == 4 and phases["io_rbytes"] is None and phases["cold"] is False


def test_stored_records_are_revalidated_before_use():
    good = phase_module.PhaseRecorder(True).record(None, None)
    assert phase_module.decode_phases(phase_module.encode_phases(good)) == good
    for bad in (
        {**good, "extra": 1}, {**good, "spawn_ms": -1}, {**good, "spawn_ms": True}, {**good, "spawn_ms": 1.5},
        {**good, "first_event_type": SECRET}, {**good, "cold": 1}, {**good, "version": 2}, {**good, "exit_ms": 2 ** 60},
        {key: value for key, value in good.items() if key != "exit_ms"},
    ):
        assert phase_module.sanitize_phases(bad) is None
    assert phase_module.decode_phases("not json") is None and phase_module.decode_phases(None) is None
    assert phase_module.decode_phases(" " * 5000 + phase_module.encode_phases(good)) is None


def test_audit_accepts_only_validated_timing_records(modules):
    common, _, _ = modules
    good = phase_module.PhaseRecorder(True).record(None, None)
    common.audit("worker.phases", probe_id=PROBE_1, backend="codex", phases=good)
    common.audit("worker.phases", probe_id=PROBE_1, phases={**good, "first_event_type": SECRET})
    common.audit("worker.phases", probe_id=PROBE_1, phases={"message": SECRET})
    lines = audit_lines(common)
    assert lines[0]["phases"] == good
    assert all("phases" not in line for line in lines[1:])
    assert SECRET not in (common.STATE / "audit.jsonl").read_text()


@pytest.mark.asyncio
async def test_recording_failures_never_change_the_execution_outcome(modules, fake_codex, executor, monkeypatch):
    common, _, worker = modules
    fake_codex(steps=turn(0, 0, 0, 0), result="synthetic model result")

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError(SECRET)

    async with executor.lock:
        monkeypatch.setattr(worker, "database", broken)
        assert await executor.run_codex("synthetic", str(uuid.uuid4()), audit_task_id=str(uuid.uuid4())) == "synthetic model result"
    assert not (common.STATE / "audit.jsonl").exists()


def test_old_ledgers_gain_the_new_columns_and_keep_their_rows(modules):
    common, _, worker = modules
    with common.database("executions.sqlite3") as db:
        db.execute("DROP TABLE executions")
        db.execute("DROP TABLE health_probes")
        db.execute("CREATE TABLE executions (id TEXT PRIMARY KEY, digest TEXT NOT NULL, status TEXT NOT NULL, "
                   "result TEXT, a2a_id TEXT, backend TEXT NOT NULL)")
        db.execute("INSERT INTO executions VALUES ('old','digest','completed','result',NULL,'codex')")
        db.execute("CREATE TABLE health_probes (id TEXT PRIMARY KEY, status TEXT NOT NULL, code TEXT, "
                   "started REAL NOT NULL, finished REAL)")
        db.execute("INSERT INTO health_probes(id,status,code,started,finished) VALUES ('old','completed','WORKER_HEALTHY',1,2)")
    worker.init_db()
    worker.init_db()
    with common.database("executions.sqlite3") as db:
        assert {"error_code", "phases"} <= {item[1] for item in db.execute("PRAGMA table_info(executions)")}
        assert "phases" in {item[1] for item in db.execute("PRAGMA table_info(health_probes)")}
        assert dict(db.execute("SELECT * FROM executions").fetchone())["result"] == "result"
        assert db.execute("SELECT phases FROM health_probes").fetchone()[0] is None


@pytest.fixture
def probe_health(modules, fake_codex, executor, cgroup, tmp_path):
    proc = tmp_path / "proc"
    (proc / "self/fd").mkdir(parents=True)
    (proc / "self/status").write_text("Threads:\t4\n")
    for name, value in {"pids.current": "4", "pids.max": "128", "pids.events": "max 0\n", "memory.current": "100",
                        "memory.max": "1000", "memory.events": "high 0\nmax 0\noom 0\noom_kill 0\n"}.items():
        (cgroup / name).write_text(value)
    (cgroup / "memory.stat").write_text("anon 1\ninactive_file 0\npgmajfault 7\n")
    return worker_health.WorkerHealth(executor, cgroup_root=cgroup, proc_root=proc, canary_timeout=30)


@pytest.mark.asyncio
async def test_healthz_snapshot_exposes_the_last_canary_phases_including_a_timeout(modules, fake_codex, probe_health):
    common, _, _ = modules
    health = probe_health
    assert health.snapshot()["canary"]["phases"] is None
    health.sample_resources()
    fake_codex(steps=turn(0.05, 0, 0, 0), result=worker_health.CANARY_RESULT)
    await health.run_canary(PROBE_1)
    stored = row(common, "health_probes")
    snapshot = health.snapshot()
    assert snapshot["status"] == "healthy" and snapshot["canary"]["phases"] == stored
    assert stored["cold"] is True and stored["first_event_type"] == "thread.started"
    published = json.loads((common.STATE / "worker-health.json").read_text())
    assert published["state"]["canary"]["phases"] == stored
    assert worker_health.check_snapshot()["status"] == "healthy"

    health.canary_timeout = 0.5
    fake_codex(steps=[{"event": {"type": "thread.started"}}, {"hang": True}], result=None)
    await health.run_canary(PROBE_2)
    failed = health.snapshot()
    assert failed["status"] == "error" and failed["canary"]["code"] == "CODEX_TIMEOUT"
    timeout = failed["canary"]["phases"]
    assert timeout["cold"] is False and timeout["first_item_ms"] is None and timeout["exit_ms"] >= 400
    with common.database("executions.sqlite3") as db:
        assert db.execute("SELECT phases FROM health_probes WHERE id=?", (PROBE_2,)).fetchone()[0]
    assert "phases" not in worker_health.check_snapshot()


@pytest.mark.asyncio
async def test_phases_of_the_previous_canary_remain_visible_while_the_next_one_runs(modules, fake_codex, probe_health):
    health = probe_health
    health.sample_resources()
    fake_codex(steps=turn(0, 0, 0, 0), result=worker_health.CANARY_RESULT)
    await health.run_canary(PROBE_1)
    previous = health.snapshot()["canary"]["phases"]
    assert previous is not None
    fake_codex(steps=[{"hang": True}], result=None)
    health.canary_timeout = 0.5
    task = asyncio.create_task(health.run_canary(PROBE_2))
    await asyncio.sleep(0.2)
    running = health.snapshot()["canary"]
    assert running["check_status"] == "checking" and running["phases"] == previous
    await task


def test_phase_timing_does_not_alter_budgets_limits_or_sandbox(modules, tmp_path, monkeypatch):
    _, _, worker = modules
    monkeypatch.delenv("CODEX_MODEL", raising=False)
    command = worker.codex_command(tmp_path, tmp_path / "result.txt")
    assert command.count("--json") == 1 and command[-1] == "-"
    assert worker_health.CANARY_TIMEOUT_MAX == 30
    assert worker_health.WorkerHealth(worker.CodexExecutor("codex")).canary_timeout == 30
    assert inspect.signature(worker.CodexExecutor.run_codex).parameters["timeout"].default == 300


def test_http_health_reports_phases_to_the_dispatcher_only(modules, fake_codex, probe_health, monkeypatch):
    common, _, worker = modules
    health = probe_health
    health.resource_interval = 30
    fake_codex(steps=turn(0, 0, 0, 0), result=worker_health.CANARY_RESULT)
    monkeypatch.setattr(worker, "CodexExecutor", lambda _backend: health.executor)
    monkeypatch.setattr(worker, "WorkerHealth", lambda executor, backend: health)
    monkeypatch.setattr(common, "identity", lambda auth: (
        {"agent": "health-check", "role": "dispatcher"} if auth == "Bearer dispatcher" else None))
    with TestClient(worker.create_app()) as client:
        for _ in range(100):
            response = client.get("/healthz", headers={"Authorization": "Bearer dispatcher"})
            if response.status_code == 200:
                break
            time.sleep(0.05)
        assert response.status_code == 200
        phases = response.json()["canary"]["phases"]
        assert phases["version"] == 1 and set(phases) == phase_module.FIELDS
        assert client.get("/healthz").status_code == 401
