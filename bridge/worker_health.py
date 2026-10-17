# SPDX-License-Identifier: Apache-2.0
"""Active resource and execution checks, sharing the business executor lock."""

import argparse
import asyncio
import json
import math
import os
import re
import time
import uuid
from pathlib import Path

from bridge import common
from bridge.common import audit, database
from bridge.failures import CATALOG, HEALTH_STATES, Failure, FailureError, worker_exception_failure
from bridge.phases import cgroup_counters, decode_phases

CANARY_PROMPT = "Reply with exactly BATON_CANARY_OK. Do not use tools."
CANARY_RESULT = "BATON_CANARY_OK"
# The canary holds the executor lock; 30 s plus the 300 s business budget stays
# below the bridge's 350 s request timeout.
CANARY_TIMEOUT_MAX = 30
CGROUP_ROOT = Path("/sys/fs/cgroup")


def init_health_db(*, recover=False):
    with database("executions.sqlite3") as db:
        db.execute("""CREATE TABLE IF NOT EXISTS health_probes (
            id TEXT PRIMARY KEY, status TEXT NOT NULL, code TEXT, started REAL NOT NULL, finished REAL)""")
        if "phases" not in [row[1] for row in db.execute("PRAGMA table_info(health_probes)")]:
            db.execute("ALTER TABLE health_probes ADD COLUMN phases TEXT")
        db.execute("CREATE TABLE IF NOT EXISTS health_flags (key TEXT PRIMARY KEY, value INTEGER NOT NULL)")
        if recover:
            db.execute("UPDATE health_probes SET status='interrupted',code='WORKER_CANARY_INTERRUPTED',finished=? WHERE status='running'",
                       (time.time(),))


def setting(name, default):
    try:
        value = float(os.environ.get(name, default))
        return value if math.isfinite(value) and value >= 1 else default
    except (TypeError, ValueError):
        return default


def component(status, code, **values):
    return {"status": status, "code": code, **values}


def combine(resources, canary, **values):
    # A skipped check must never hide a known authentication failure.
    ordered = [canary, resources]
    chosen = next((item for item in ordered if item["status"] == "error"), None)
    if chosen is None:
        chosen = next((item for item in ordered if item["status"] == "degraded"), None)
    if chosen is None:
        chosen = next((item for item in ordered if item["status"] == "unknown"), None)
    if chosen is None:
        chosen = next((item for item in ordered if item["status"] in ("busy", "checking")), None)
    if chosen is None:
        chosen = component("healthy", "WORKER_HEALTHY")
    status = "unknown" if chosen["status"] == "checking" else chosen["status"]
    return {"version": 1, "status": status, "code": chosen["code"], **values,
            "resources": resources, "canary": canary}


def sanitize_health_snapshot(value):
    """Reconstruct fixed fields only; a stored or peer description is never copied."""
    def safe_part(name):
        original = value.get(name) if isinstance(value, dict) else None
        unknown = component("unknown", "WORKER_HEALTH_UNAVAILABLE", fresh=False, tested=False)
        if not isinstance(original, dict):
            return unknown
        status, code = original.get("status"), original.get("code")
        if not isinstance(status, str) or status not in HEALTH_STATES or not isinstance(code, str) or code not in CATALOG:
            return unknown
        fresh = original.get("fresh") is True
        tested = original.get("tested") is True
        if status == "healthy" and (code != "WORKER_HEALTHY" or not fresh or (name == "canary" and not tested)):
            return unknown
        if code == "WORKER_HEALTHY" and status != "healthy":
            return unknown
        result = component(status, code, fresh=fresh, tested=tested)
        check_status = original.get("check_status")
        if check_status in ("busy", "checking", "finished", "not_checked"):
            result["check_status"] = check_status
        age = original.get("age_seconds")
        if type(age) in (int, float) and math.isfinite(age) and age >= 0:
            result["age_seconds"] = age
        return result
    return combine(safe_part("resources"), safe_part("canary"))


class WorkerHealth:
    def __init__(self, executor, *, backend="codex", cgroup_root=CGROUP_ROOT, proc_root=Path("/proc"),
                 clock=time.time, resource_interval=None, canary_interval=None, canary_timeout=None):
        self.executor, self.backend = executor, backend
        self.cgroup_root, self.proc_root, self.clock = Path(cgroup_root), Path(proc_root), clock
        self.resource_interval = resource_interval if resource_interval is not None else setting("BATON_RESOURCE_SAMPLE_SECONDS", 30)
        self.canary_interval = canary_interval if canary_interval is not None else setting("BATON_CANARY_INTERVAL_SECONDS", 900)
        self.canary_timeout = min(CANARY_TIMEOUT_MAX, canary_timeout if canary_timeout is not None
                                  else setting("BATON_CANARY_TIMEOUT_SECONDS", CANARY_TIMEOUT_MAX))
        if not all(math.isfinite(value) and value > 0 for value in (self.resource_interval, self.canary_interval, self.canary_timeout)):
            raise ValueError("Health intervals must be positive")
        self._resources = None
        self._event_counts = None
        self._canary_override = None
        self._transitions = {}
        self._tasks = []
        init_health_db()

    def _record_change(self, source, state):
        signature = (state["status"], state["code"], state.get("check_status"))
        previous = self._transitions.get(source)
        if previous == signature:
            return
        self._transitions[source] = signature
        level = "error" if state["status"] == "error" else "warning" if state["status"] != "healthy" else "info"
        stage = CATALOG[state["code"]][0]
        audit("worker.health_changed", source=source, health=state["status"], error_code=state["code"],
              stage=stage, level=level)
        if state["status"] == "error" and stage == "authentication":
            audit("worker.auth_failed", error_code=state["code"], stage=stage, level="error")
        if source == "canary" and previous and previous[0] != "healthy" and state["status"] == "healthy":
            audit("worker.health_recovered", source=source, error_code=state["code"], level="info")

    def _require_new_canary(self):
        with database("executions.sqlite3") as db:
            seq = db.execute("SELECT COALESCE(MAX(rowid),0) FROM health_probes").fetchone()[0]
            db.execute("INSERT INTO health_flags(key,value) VALUES ('canary_after',?) ON CONFLICT(key) DO UPDATE SET value=MAX(value,excluded.value)", (seq,))

    def sample_resources(self):
        def number(name):
            value = (self.cgroup_root / name).read_text().strip()
            if not re.fullmatch(r"[0-9]+", value):
                raise ValueError("Invalid resource counter")
            return int(value)

        def limit(name):
            return None if (self.cgroup_root / name).read_text().strip() == "max" else number(name)

        def event(name, key):
            for line in (self.cgroup_root / name).read_text().splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[0] == key and parts[1].isdigit():
                    return int(parts[1])
            raise ValueError("Missing resource counter")

        def memory_statistics():
            values = {}
            for line in (self.cgroup_root / "memory.stat").read_text().splitlines():
                parts = line.split()
                if len(parts) != 2 or not re.fullmatch(r"[0-9]+", parts[1]) or parts[0] in values:
                    raise ValueError("Invalid memory statistic")
                values[parts[0]] = int(parts[1])
            if not {"inactive_file", "anon"} <= values.keys():
                raise ValueError("Missing memory statistic")
            return values["inactive_file"], values["anon"]

        try:
            pids, pids_max = number("pids.current"), limit("pids.max")
            memory, memory_max = number("memory.current"), limit("memory.max")
            inactive_file, anon = memory_statistics()
            # Retain charged anon, shmem and kernel memory while discounting
            # inactive file cache. Cross-file sampling can briefly exceed current.
            working_set = max(0, memory - inactive_file)
            counts = (event("pids.events", "max"), event("memory.events", "oom"), event("memory.events", "oom_kill"),
                      event("memory.events", "high"), event("memory.events", "max"))
            thread_match = re.search(r"(?m)^Threads:\s*([0-9]+)$", (self.proc_root / "self/status").read_text())
            if thread_match is None:
                raise ValueError("Missing thread counter")
            threads = int(thread_match[1])
            fds = sum(1 for _ in (self.proc_root / "self/fd").iterdir())
            previous, self._event_counts = self._event_counts, counts
            status, code = "healthy", "WORKER_HEALTHY"
            if previous and (counts[1] > previous[1] or counts[2] > previous[2]):
                status, code = "error", "WORKER_MEMORY_EXHAUSTED"
            elif previous and counts[0] > previous[0]:
                status, code = "error", "WORKER_RESOURCE_EXHAUSTED"
            elif pids_max is not None and pids >= pids_max * 0.8:
                status, code = "degraded", "WORKER_PID_PRESSURE"
            elif memory_max is not None and working_set * 100 >= memory_max * 85:
                status, code = "degraded", "WORKER_MEMORY_PRESSURE"
            elif previous and (counts[3] > previous[3] or counts[4] > previous[4]):
                status, code = "degraded", "WORKER_MEMORY_PRESSURE"
            # CPU and I/O counters are diagnostic only: containers without the io
            # controller have no io.stat, which must not make the worker unhealthy.
            counters = cgroup_counters(self.cgroup_root)
            self._resources = component(
                status, code, sampled=self.clock(), fresh=True,
                pids={"current": pids, "limit": pids_max, "denied_total": counts[0]},
                memory={"current": memory, "working_set": working_set, "anon": anon,
                        "limit": memory_max, "oom_total": counts[1], "oom_kill_total": counts[2],
                        "high_total": counts[3], "max_total": counts[4]},
                cpu={"usage_usec": counters["cpu_usage_usec"], "nr_throttled": counters["cpu_nr_throttled"],
                     "throttled_usec": counters["cpu_throttled_usec"]},
                io={"rbytes": counters["io_rbytes"], "rios": counters["io_rios"]},
                process={"threads": threads, "open_fds": fds})
            if status in ("degraded", "error"):
                self._require_new_canary()
        except Exception:
            self._resources = component("unknown", "WORKER_HEALTH_UNAVAILABLE", sampled=self.clock(), fresh=False)
        self._record_change("resources", self._resource_snapshot())
        self._record_change("canary", self._canary_snapshot())
        self._publish_snapshot()
        return self._resource_snapshot()

    def _resource_snapshot(self):
        if self._resources is None:
            return component("unknown", "WORKER_HEALTH_UNKNOWN", fresh=False)
        result = {key: value for key, value in self._resources.items() if key != "sampled"}
        age = self.clock() - self._resources["sampled"]
        result["age_seconds"] = max(0, age) if math.isfinite(age) else None
        if not math.isfinite(age) or age < 0 or age > self.resource_interval * 2:
            result.update(status="unknown", code="WORKER_RESOURCE_STALE", fresh=False)
        return result

    def _probe_snapshot(self, row):
        if row is None:
            return component("unknown", "WORKER_HEALTH_UNKNOWN", fresh=False, tested=False, check_status="not_checked")
        finished = row["finished"]
        age = self.clock() - finished if finished is not None else None
        valid_age = age is not None and math.isfinite(age) and age >= 0
        shared = {"tested": row["status"] in ("completed", "failed"), "fresh": valid_age and age <= self.canary_interval,
                  "probe_id": row["id"], "age_seconds": age, "check_status": "finished"}
        code = row["code"] if row["code"] in CATALOG else "WORKER_HEALTH_UNAVAILABLE"
        if row["status"] == "failed":
            return component("error", code, **shared)
        if row["status"] == "interrupted":
            return component("unknown", "WORKER_CANARY_INTERRUPTED", **shared)
        if row["status"] != "completed":
            return component("unknown", "WORKER_CANARY_RUNNING", **{**shared, "check_status": "checking"})
        if code != "WORKER_HEALTHY":
            return component("unknown", "WORKER_HEALTH_UNAVAILABLE", **shared)
        if not shared["fresh"]:
            return component("unknown", "WORKER_CANARY_STALE", **shared)
        with database("executions.sqlite3") as db:
            required = db.execute("SELECT value FROM health_flags WHERE key='canary_after'").fetchone()
        if required and row["seq"] <= required["value"]:
            return component("unknown", "WORKER_CANARY_REQUIRED", **shared)
        return component("healthy", "WORKER_HEALTHY", **shared)

    def _canary_snapshot(self):
        with database("executions.sqlite3") as db:
            row = db.execute("SELECT rowid AS seq,* FROM health_probes ORDER BY rowid DESC LIMIT 1").fetchone()
            prior = (db.execute("SELECT rowid AS seq,* FROM health_probes WHERE status IN ('completed','failed') ORDER BY rowid DESC LIMIT 1").fetchone()
                     if row and row["status"] in ("running", "interrupted") else None)
            last = db.execute("SELECT phases FROM health_probes WHERE status!='running' ORDER BY rowid DESC LIMIT 1").fetchone()
        result = self._probe_snapshot(row)
        if prior and prior["status"] == "failed":
            result = {**self._probe_snapshot(prior), "check_status": "checking" if row["status"] == "running" else "not_checked", "tested": False}
        if self._canary_override:
            if result["status"] == "error":
                result = {**result, "check_status": self._canary_override["status"], "tested": False}
            else:
                result = dict(self._canary_override)
        # Timings of the last finished canary, including a failed or timed-out one.
        return {**result, "phases": decode_phases(last["phases"]) if last else None}

    def snapshot(self):
        return combine(self._resource_snapshot(), self._canary_snapshot(), backend=self.backend)

    def _publish_snapshot(self):
        state = self.snapshot()
        now = self.clock()
        remaining = self.resource_interval * 2
        if state["status"] == "healthy":
            remaining = min(remaining - state["resources"]["age_seconds"],
                            self.canary_interval - state["canary"]["age_seconds"])
        payload = {"version": 1, "written_at": now, "valid_until": now + max(0, remaining), "state": state}
        path = common.STATE / "worker-health.json"
        temporary = path.with_suffix(".new")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w") as output:
                os.fchmod(output.fileno(), 0o600)
                json.dump(payload, output, ensure_ascii=False)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    async def run_canary(self, probe_id=None):
        probe_id = str(uuid.UUID(probe_id)) if probe_id else str(uuid.uuid4())
        started = False
        try:
            await asyncio.wait_for(self.executor.lock.acquire(), timeout=0.05)
        except TimeoutError:
            self._canary_override = component("busy", "WORKER_CANARY_BUSY", fresh=False, tested=False, check_status="busy")
            self._record_change("canary", self._canary_snapshot())
            self._publish_snapshot()
            return self._canary_snapshot()
        try:
            with database("executions.sqlite3") as db:
                db.execute("BEGIN IMMEDIATE")
                existing = db.execute("SELECT rowid AS seq,* FROM health_probes WHERE id=?", (probe_id,)).fetchone()
                if existing:
                    return self._probe_snapshot(existing)
                db.execute("INSERT INTO health_probes(id,status,started) VALUES (?,'running',?)", (probe_id, self.clock()))
            started = True
            self._canary_override = component("checking", "WORKER_CANARY_RUNNING", fresh=False, tested=False, check_status="checking")
            self._record_change("canary", self._canary_snapshot())
            self._publish_snapshot()
            try:
                output = await self.executor.run_backend(CANARY_PROMPT, process_key=probe_id,
                                                         timeout=self.canary_timeout, health_probe=True)
                if output.strip() != CANARY_RESULT:
                    raise FailureError(Failure("WORKER_CANARY_INVALID_RESULT"))
                status, code = "completed", "WORKER_HEALTHY"
            except asyncio.CancelledError:
                with database("executions.sqlite3") as db:
                    db.execute("UPDATE health_probes SET status='interrupted',code='WORKER_CANARY_INTERRUPTED',finished=? WHERE id=?",
                               (self.clock(), probe_id))
                raise
            except Exception as error:
                status, code = "failed", worker_exception_failure(error).code
            with database("executions.sqlite3") as db:
                db.execute("UPDATE health_probes SET status=?,code=?,finished=? WHERE id=?", (status, code, self.clock(), probe_id))
            self._canary_override = None
            result = self._canary_snapshot()
            self._record_change("canary", result)
            self._publish_snapshot()
            return result
        finally:
            if started:
                self._canary_override = None
            self.executor.lock.release()

    async def _resource_loop(self):
        while True:
            try:
                self.sample_resources()
            except Exception:
                self._resources = component("unknown", "WORKER_HEALTH_UNAVAILABLE", sampled=self.clock(), fresh=False)
                self._record_change("resources", self._resource_snapshot())
            await asyncio.sleep(self.resource_interval)

    async def _canary_loop(self):
        while True:
            try:
                result = await self.run_canary()
            except Exception:
                self._canary_override = component("unknown", "WORKER_HEALTH_UNAVAILABLE", fresh=False, tested=False)
                result = self._canary_snapshot()
                self._record_change("canary", result)
            delay = min(self.resource_interval, self.canary_interval) if result.get("check_status") == "busy" else self.canary_interval
            await asyncio.sleep(delay)

    def start(self):
        if not self._tasks:
            self._tasks = [asyncio.create_task(self._resource_loop()), asyncio.create_task(self._canary_loop())]

    async def stop(self):
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []


def check_snapshot(*, path=None, now=None):
    """Credential-free container healthcheck; a stale file can never pass."""
    fallback = component("unknown", "WORKER_HEALTH_UNAVAILABLE")
    path = path or common.STATE / "worker-health.json"
    now = time.time() if now is None else now
    try:
        if path.stat().st_size > 65536:
            return fallback
        value = json.loads(path.read_text())
        if not isinstance(value, dict) or value.get("version") != 1:
            return fallback
        written, valid = value.get("written_at"), value.get("valid_until")
        if not all(type(item) in (int, float) and math.isfinite(item) for item in (written, valid, now)):
            return fallback
        if not written <= now <= valid:
            return component("unknown", "WORKER_RESOURCE_STALE")
        state = sanitize_health_snapshot(value.get("state"))
        return {"status": state["status"], "code": state["code"]}
    except Exception:
        return fallback


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["check"])
    parser.parse_args(argv)
    state = check_snapshot()
    print(json.dumps(state))
    return 0 if state["status"] == "healthy" else 1


if __name__ == "__main__":
    raise SystemExit(main())
