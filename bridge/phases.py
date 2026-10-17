# SPDX-License-Identifier: Apache-2.0
"""Per-phase timing of one Codex CLI execution: numbers and event type names, never message text."""

import json
import re
import time
from pathlib import Path

PHASE_VERSION = 1
# The CLI's --json event types. Anything else is reported as "other" so a record
# can never carry free text, whatever the CLI or a model emits.
EVENT_TYPES = frozenset({
    "thread.started", "turn.started", "turn.completed", "turn.failed",
    "item.started", "item.updated", "item.completed", "item.other", "error", "other",
})
TURN_END_TYPES = frozenset({"turn.completed", "turn.failed"})
MILLISECOND_FIELDS = ("spawn_ms", "first_event_ms", "first_item_ms", "turn_end_ms", "exit_ms")
TYPE_FIELDS = ("first_event_type", "turn_end_type")
# Deltas over one execution of container-wide cgroup v2 counters.
COUNTER_FIELDS = ("cpu_usage_usec", "cpu_nr_throttled", "cpu_throttled_usec", "io_rbytes", "io_rios", "pgmajfault")
FIELDS = frozenset(("version", "cold") + MILLISECOND_FIELDS + TYPE_FIELDS + COUNTER_FIELDS)
MAX_VALUE = 2 ** 53


def event_name(raw):
    if isinstance(raw, str):
        if raw in EVENT_TYPES:
            return raw
        if raw.startswith("item."):
            return "item.other"
    return "other"


def _number(value):
    return value if type(value) is int and 0 <= value <= MAX_VALUE else None


def sanitize_phases(value):
    """Rebuild a stored or reported record from validated fields only; None if malformed."""
    if not isinstance(value, dict) or set(value) != FIELDS or value["version"] != PHASE_VERSION:
        return None
    if type(value["cold"]) is not bool:
        return None
    result = {"version": PHASE_VERSION, "cold": value["cold"]}
    for key in MILLISECOND_FIELDS + COUNTER_FIELDS:
        if value[key] is not None and _number(value[key]) is None:
            return None
        result[key] = value[key]
    for key in TYPE_FIELDS:
        if value[key] is not None and value[key] not in EVENT_TYPES:
            return None
        result[key] = value[key]
    return result


def encode_phases(phases):
    return json.dumps(phases, sort_keys=True, separators=(",", ":"))


def decode_phases(text):
    try:
        return sanitize_phases(json.loads(text)) if isinstance(text, str) and len(text) <= 4096 else None
    except (ValueError, RecursionError):
        return None


def _keyed_counters(text):
    values = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 2 or not re.fullmatch(r"[0-9]+", parts[1]) or parts[0] in values:
            raise ValueError("Invalid counter")
        values[parts[0]] = int(parts[1])
    return values


def _device_totals(text, keys):
    totals = dict.fromkeys(keys, 0)
    for line in text.splitlines():
        device, *pairs = line.split()
        if not re.fullmatch(r"[0-9]+:[0-9]+", device):
            raise ValueError("Invalid device")
        seen = {}
        for pair in pairs:
            key, separator, number = pair.partition("=")
            if not separator or not re.fullmatch(r"[0-9]+", number) or key in seen:
                raise ValueError("Invalid counter")
            seen[key] = int(number)
        if not set(keys) <= seen.keys():
            raise ValueError("Missing counter")
        for key in keys:
            totals[key] += seen[key]
    return totals


def cgroup_counters(root):
    """Read the timing counters; anything missing or malformed is None, never zero."""
    counters = dict.fromkeys(COUNTER_FIELDS)
    root = Path(root)
    for field, source, key in (
        ("cpu_usage_usec", "cpu.stat", "usage_usec"), ("cpu_nr_throttled", "cpu.stat", "nr_throttled"),
        ("cpu_throttled_usec", "cpu.stat", "throttled_usec"), ("pgmajfault", "memory.stat", "pgmajfault"),
    ):
        try:
            counters[field] = _keyed_counters((root / source).read_text()).get(key)
        except (OSError, ValueError):
            pass
    try:
        totals = _device_totals((root / "io.stat").read_text(), ("rbytes", "rios"))
        counters.update(io_rbytes=totals["rbytes"], io_rios=totals["rios"])
    except (OSError, ValueError):
        pass
    return counters


class PhaseRecorder:
    """Marks the first occurrence of each phase on one monotonic clock, in ms since spawn."""

    def __init__(self, cold, clock=time.monotonic_ns):
        self.cold, self.clock = cold, clock
        self.origin = None
        self.marks = {}
        self.types = {}

    def _mark(self, name):
        if self.origin is not None and name not in self.marks:
            self.marks[name] = self.clock()

    def begin(self):
        self.origin = self.clock()

    def spawned(self):
        self._mark("spawn_ms")

    def event(self, raw_type):
        if self.origin is None:
            return
        if "first_event_ms" not in self.marks:
            self.types["first_event_type"] = event_name(raw_type)
            self._mark("first_event_ms")
        if isinstance(raw_type, str) and raw_type.startswith("item."):
            self._mark("first_item_ms")
        if isinstance(raw_type, str) and raw_type in TURN_END_TYPES and "turn_end_ms" not in self.marks:
            self.types["turn_end_type"] = raw_type
            self._mark("turn_end_ms")

    def exited(self):
        self._mark("exit_ms")

    def record(self, before, after):
        phases = {"version": PHASE_VERSION, "cold": self.cold}
        for key in MILLISECOND_FIELDS:
            mark = self.marks.get(key)
            phases[key] = None if mark is None else max(0, (mark - self.origin) // 1_000_000)
        for key in TYPE_FIELDS:
            phases[key] = self.types.get(key)
        for key in COUNTER_FIELDS:
            start, end = (before or {}).get(key), (after or {}).get(key)
            phases[key] = end - start if start is not None and end is not None and end >= start else None
        return phases
