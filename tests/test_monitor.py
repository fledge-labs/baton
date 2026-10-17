# SPDX-License-Identifier: Apache-2.0
"""scripts/monitor.py against fake HTTP servers; no Baton, database or network is needed."""

import http.client
import http.server
import importlib.util
import ipaddress
import json
import logging
import os
import re
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "monitor.py"

spec = importlib.util.spec_from_file_location("monitor_under_test", SCRIPT)
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)

MONITOR_TOKEN = "mon-token-SECRET-0123456789abcdef"
WEBHOOK_TOKEN = "hook-token-SECRET-fedcba9876543210"
WEBHOOK_PATH_SECRET = "path-SECRET-abc123"
SECRETS = (MONITOR_TOKEN, WEBHOOK_TOKEN, WEBHOOK_PATH_SECRET)


class Server:
    def __init__(self, handler_class):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_class)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class FakeBaton:
    """Just enough of the edge + MCP surface: /board, unauthenticated /mcp -> 401, tools/call."""

    def __init__(self):
        self.board_status = 200
        self.unauth_status = 401
        self.mcp_status = 200
        self.fetch_is_error = False
        self.sse = False
        self.override = {}
        self.calls = []
        self.lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, body=b"", ctype="application/json", extra=()):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for key, value in extra:
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def rpc(self, message):
                if fake.sse:
                    return self.reply(200, ("event: message\ndata: %s\n\n" % json.dumps(message)).encode(),
                                      "text/event-stream")
                self.reply(200, json.dumps(message).encode())

            def do_GET(self):
                with fake.lock:
                    fake.calls.append(("GET", self.path, self.headers.get("Authorization")))
                if self.path == "/board":
                    return self.reply(fake.board_status, b"<html></html>", "text/html")
                self.reply(404)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                auth = self.headers.get("Authorization")
                with fake.lock:
                    fake.calls.append(("POST", self.path, auth, body.get("method")))
                if self.path != "/mcp":
                    return self.reply(404)
                if auth is None:
                    return self.reply(fake.unauth_status, b'{"error":"unauthorized"}')
                if auth != "Bearer " + MONITOR_TOKEN:
                    return self.reply(401, b'{"error":"unauthorized"}')
                if fake.mcp_status != 200:
                    return self.reply(fake.mcp_status)
                method = body.get("method")
                if method in fake.override:
                    status, ctype, raw = fake.override[method]
                    return self.reply(status, raw, ctype)
                if method == "initialize":
                    return self.rpc({"jsonrpc": "2.0", "id": body["id"],
                                     "result": {"protocolVersion": "2025-03-26", "capabilities": {}}})
                if method == "notifications/initialized":
                    return self.reply(202)
                if method == "tools/call" and body["params"]["name"] == "fetch_inbox":
                    return self.rpc({"jsonrpc": "2.0", "id": body["id"],
                                     "result": {"content": [{"type": "text", "text": "{}"}],
                                                "isError": fake.fetch_is_error}})
                self.reply(400)

        self.server = Server(Handler)
        self.url = self.server.url

    def close(self):
        self.server.close()


class Misbehaving:
    """A server that answers every request in a way that never finishes."""

    def __init__(self, mode):
        self.stop = threading.Event()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def handle_any(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                try:
                    if mode == "silent":
                        fake.stop.wait(60)
                    elif mode == "drip_headers":
                        for byte in b"HTTP/1.1 200 OK\r\n" * 1000:
                            self.wfile.write(bytes([byte]))
                            self.wfile.flush()
                            if fake.stop.wait(0.05):
                                return
                    elif mode == "drip_body":
                        self.send_response(200)
                        self.send_header("Content-Length", "1000000")
                        self.end_headers()
                        while not fake.stop.wait(0.1):
                            self.wfile.write(b"x")
                            self.wfile.flush()
                    elif mode == "sse_forever":
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        self.send_header("Connection", "close")
                        self.end_headers()
                        while True:
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                            if fake.stop.wait(0.1):
                                return
                except OSError:
                    return

            do_GET = do_POST = handle_any

        self.server = Server(Handler)
        self.url = self.server.url

    def close(self):
        self.stop.set()
        self.server.close()


@pytest.fixture
def misbehaving():
    created = []

    def make(mode):
        fake = Misbehaving(mode)
        created.append(fake)
        return fake

    yield make
    for fake in created:
        fake.close()


class FakeWebhook:
    def __init__(self, status=200):
        self.status = status
        self.received = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.received.append({"path": self.path, "auth": self.headers.get("Authorization"),
                                      "ctype": self.headers.get("Content-Type"), "body": body})
                self.send_response(fake.status)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self.server = Server(Handler)
        self.url = self.server.url + "/hook/" + WEBHOOK_PATH_SECRET

    def close(self):
        self.server.close()

    def events(self, status=None):
        return [r["body"] for r in self.received if status is None or r["body"]["status"] == status]


@pytest.fixture
def baton():
    fake = FakeBaton()
    yield fake
    fake.close()


@pytest.fixture
def webhook():
    fake = FakeWebhook()
    yield fake
    fake.close()


@pytest.fixture
def token_file(tmp_path):
    path = tmp_path / "monitor.token"
    path.write_text(MONITOR_TOKEN + "\n")
    path.chmod(0o600)
    return path


@pytest.fixture
def webhook_token_file(tmp_path):
    path = tmp_path / "webhook.token"
    path.write_text(WEBHOOK_TOKEN + "\n")
    path.chmod(0o600)
    return path


class ListSink:
    def __init__(self):
        self.sent = []
        self.fail = False

    def send(self, payload):
        if self.fail:
            raise monitor.SinkError("connection_error")
        self.sent.append(payload)

    def of(self, status):
        return [p for p in self.sent if p["status"] == status]


class Scripted:
    """Probes whose outcome the test sets per check."""

    def __init__(self):
        self.down = set()

    def probes(self):
        def make(name):
            def probe():
                if name in self.down:
                    raise monitor.ProbeError("http_500")
            return probe
        return {name: make(name) for name in monitor.CHECKS}


def make_monitor(tmp_path, sink, scripted=None, **kwargs):
    scripted = scripted or Scripted()
    state = monitor.State(tmp_path / "state.json")
    options = {"host_label": "test-host", "failures": 3, "realert_seconds": 3600.0, "interval": 60.0}
    options.update(kwargs)
    return monitor.Monitor(scripted.probes(), sink, state, **options), scripted, state


def args_for(baton, token_file, tmp_path, *extra):
    return monitor.parse_args(["--url", baton.url, "--token-file", str(token_file), "--state-file",
                               str(tmp_path / "state.json"), "--timeout", "5", *extra])


def real_probes(baton, token_file, tmp_path):
    return monitor.build_probes(args_for(baton, token_file, tmp_path, "--sink", "stdout"))


def test_all_probes_pass_against_a_healthy_deployment(baton, token_file, tmp_path):
    for name, probe in real_probes(baton, token_file, tmp_path).items():
        probe()
    methods = [call[3] for call in baton.calls if call[0] == "POST" and call[2]]
    assert methods == ["initialize", "notifications/initialized", "tools/call"]
    unauth = [call for call in baton.calls if call[0] == "POST" and call[2] is None]
    assert len(unauth) == 1, "the 401 probe sends no credentials"


def test_mcp_call_accepts_an_event_stream_reply(baton, token_file, tmp_path):
    baton.sse = True
    real_probes(baton, token_file, tmp_path)["mcp_call"]()


@pytest.mark.parametrize("setup,check,category", [
    (lambda b: setattr(b, "board_status", 503), "board", "http_503"),
    (lambda b: setattr(b, "unauth_status", 200), "mcp_unauth", "http_200_expected_401"),
    (lambda b: setattr(b, "unauth_status", 403), "mcp_unauth", "http_403_expected_401"),
    (lambda b: setattr(b, "mcp_status", 502), "mcp_call", "initialize_http_502"),
    (lambda b: setattr(b, "fetch_is_error", True), "mcp_call", "fetch_inbox_failed"),
])
def test_each_probe_detects_its_failure(baton, token_file, tmp_path, setup, check, category):
    probes = real_probes(baton, token_file, tmp_path)
    setup(baton)
    for name, probe in probes.items():
        if name == check:
            with pytest.raises(monitor.ProbeError, match="^" + category + "$"):
                probe()
        else:
            probe()


def test_a_rejected_token_fails_only_the_authenticated_probe(baton, tmp_path):
    bad = tmp_path / "bad.token"
    bad.write_text("wrong-token\n")
    probes = real_probes(baton, bad, tmp_path)
    probes["board"]()
    probes["mcp_unauth"]()
    with pytest.raises(monitor.ProbeError, match="initialize_http_401"):
        probes["mcp_call"]()


def test_unreachable_deployment_reports_a_fixed_category(token_file, tmp_path):
    dead = Server(BaseHTTPRequestHandler)
    url = dead.url
    dead.close()
    args = monitor.parse_args(["--url", url, "--token-file", str(token_file), "--timeout", "2",
                               "--state-file", str(tmp_path / "s.json"), "--sink", "stdout"])
    for probe in monitor.build_probes(args).values():
        with pytest.raises(monitor.ProbeError, match="^connection_error$"):
            probe()


def test_latency_is_measured_and_sent(baton, token_file, tmp_path):
    sink = ListSink()
    state = monitor.State(tmp_path / "state.json")
    probes = real_probes(baton, token_file, tmp_path)
    mon = monitor.Monitor(probes, sink, state, host_label="h", failures=1)
    baton.board_status = 500
    mon.cycle(1000.0)
    alert = sink.of("alert")[0]
    assert alert["check"] == "board"
    assert isinstance(alert["latency_ms"], int) and alert["latency_ms"] >= 0


def test_no_alert_before_the_third_consecutive_failure(tmp_path):
    sink = ListSink()
    mon, scripted, _ = make_monitor(tmp_path, sink)
    scripted.down.add("board")
    mon.cycle(0)
    mon.cycle(60)
    assert sink.sent == []
    mon.cycle(120)
    assert [(p["check"], p["status"], p["consecutive_failures"]) for p in sink.sent] == [("board", "alert", 3)]


def test_a_success_resets_the_failure_count(tmp_path):
    sink = ListSink()
    mon, scripted, _ = make_monitor(tmp_path, sink)
    scripted.down.add("mcp_call")
    mon.cycle(0)
    mon.cycle(60)
    scripted.down.clear()
    mon.cycle(120)
    scripted.down.add("mcp_call")
    mon.cycle(180)
    mon.cycle(240)
    assert sink.sent == []


def test_single_alert_while_down_then_single_recovery(tmp_path):
    sink = ListSink()
    mon, scripted, _ = make_monitor(tmp_path, sink)
    scripted.down.add("mcp_unauth")
    for cycle in range(20):
        mon.cycle(cycle * 60)
    assert len(sink.sent) == 1 and sink.sent[0]["status"] == "alert"
    scripted.down.clear()
    for cycle in range(20, 30):
        mon.cycle(cycle * 60)
    assert [(p["check"], p["status"]) for p in sink.sent] == [("mcp_unauth", "alert"), ("mcp_unauth", "recovered")]
    assert sink.sent[1]["consecutive_failures"] == 0


def test_checks_alert_and_recover_independently(tmp_path):
    sink = ListSink()
    mon, scripted, _ = make_monitor(tmp_path, sink)
    scripted.down.update({"board", "mcp_call"})
    for cycle in range(3):
        mon.cycle(cycle * 60)
    scripted.down.discard("board")
    mon.cycle(180)
    assert sorted((p["check"], p["status"]) for p in sink.sent) == [
        ("board", "alert"), ("board", "recovered"), ("mcp_call", "alert")]


def test_realert_every_sixty_minutes_while_still_down(tmp_path):
    sink = ListSink()
    mon, scripted, _ = make_monitor(tmp_path, sink)
    scripted.down.add("board")
    for second in range(0, 3 * 3600 + 1, 60):
        mon.cycle(second)
    alerts = sink.of("alert")
    assert [p["ts"] for p in alerts] == [monitor.utc_iso(t) for t in (120, 120 + 3600, 120 + 7200)]
    assert "still failing" in alerts[1]["message"] and "still failing" not in alerts[0]["message"]


def test_realert_interval_is_configurable_and_zero_disables(tmp_path):
    sink = ListSink()
    mon, scripted, _ = make_monitor(tmp_path, sink, realert_seconds=300.0)
    scripted.down.add("board")
    for second in range(0, 1801, 60):
        mon.cycle(second)
    assert len(sink.of("alert")) == 1 + (1800 - 120) // 300
    off = ListSink()
    mon2, scripted2, _ = make_monitor(tmp_path / "off", off, realert_seconds=float("inf"))
    scripted2.down.add("board")
    for second in range(0, 7 * 3600, 60):
        mon2.cycle(second)
    assert len(off.of("alert")) == 1


def test_undelivered_messages_are_retried_not_forgotten(tmp_path):
    sink = ListSink()
    mon, scripted, _ = make_monitor(tmp_path, sink)
    sink.fail = True
    scripted.down.add("board")
    for cycle in range(5):
        mon.cycle(cycle * 60)
    assert sink.sent == []
    sink.fail = False
    mon.cycle(300)
    assert [p["status"] for p in sink.sent] == ["alert"]
    sink.sent.clear()
    scripted.down.clear()
    sink.fail = True
    mon.cycle(360)
    sink.fail = False
    mon.cycle(420)
    mon.cycle(480)
    assert [p["status"] for p in sink.sent] == ["recovered"]


def test_a_failure_that_clears_before_its_alert_is_delivered_sends_nothing(tmp_path):
    sink = ListSink()
    mon, scripted, _ = make_monitor(tmp_path, sink)
    sink.fail = True
    scripted.down.add("board")
    for cycle in range(4):
        mon.cycle(cycle * 60)
    scripted.down.clear()
    sink.fail = False
    mon.cycle(240)
    assert sink.sent == []


def test_state_survives_restart_without_duplicate_alert(tmp_path):
    sink = ListSink()
    mon, scripted, state = make_monitor(tmp_path, sink)
    scripted.down.add("board")
    for cycle in range(3):
        mon.cycle(cycle * 60)
    assert [p["status"] for p in sink.sent] == ["alert"]

    sink2 = ListSink()
    state2 = monitor.State(tmp_path / "state.json")
    state2.load(240)
    mon2 = monitor.Monitor(scripted.probes(), sink2, state2, host_label="test-host", failures=3,
                           realert_seconds=3600.0)
    assert mon2.alerts()["data"]["alerts"][0]["activeAt"] == monitor.utc_iso(0)
    mon2.cycle(240)
    mon2.cycle(300)
    assert sink2.sent == []
    assert mon2.alerts()["data"]["alerts"][0]["activeAt"] == monitor.utc_iso(0), "the streak survives a restart"
    scripted.down.clear()
    mon2.cycle(360)
    assert [p["status"] for p in sink2.sent] == ["recovered"]
    assert mon2.alerts()["data"]["alerts"] == []

    mode = stat.S_IMODE((tmp_path / "state.json").stat().st_mode)
    assert mode == 0o600
    assert set(json.loads((tmp_path / "state.json").read_text())) == {"version", "checks"}


def test_corrupt_state_file_starts_fresh(tmp_path):
    (tmp_path / "state.json").write_text("{not json")
    state = monitor.State(tmp_path / "state.json")
    state.load(100)
    assert state.entry("board")["consecutive_failures"] == 0 and state.entry("board")["streak_start"] is None


def test_future_timestamps_in_state_are_clamped(tmp_path):
    (tmp_path / "state.json").write_text(json.dumps({
        "checks": {"board": {"consecutive_failures": 5, "alerting": True, "last_alert": 10**12,
                             "streak_start": 10**12, "error": "http_500"}}}))
    state = monitor.State(tmp_path / "state.json")
    state.load(1000)
    assert state.entry("board")["last_alert"] == 1000 and state.entry("board")["streak_start"] == 1000


def test_webhook_sink_posts_the_generic_payload_with_bearer_token(webhook, webhook_token_file):
    sink = monitor.WebhookSink(webhook.url, str(webhook_token_file), 5, None)
    payload = {"source": "baton-monitor", "host_label": "h", "check": "board", "status": "alert",
               "consecutive_failures": 3, "latency_ms": 12, "message": "m", "ts": "2026-01-01T00:00:00Z"}
    sink.send(payload)
    [received] = webhook.received
    assert received["body"] == payload
    assert received["auth"] == "Bearer " + WEBHOOK_TOKEN
    assert received["ctype"] == "application/json"
    assert received["path"] == "/hook/" + WEBHOOK_PATH_SECRET


def test_webhook_sink_without_token_sends_no_authorization(webhook):
    monitor.WebhookSink(webhook.url, None, 5, None).send({"status": "alert"})
    assert webhook.received[0]["auth"] is None


def test_webhook_sink_error_statuses_raise_a_fixed_category(webhook):
    webhook.status = 500
    with pytest.raises(monitor.SinkError, match="^http_500$"):
        monitor.WebhookSink(webhook.url, None, 5, None).send({"status": "alert"})


def test_payload_has_exactly_the_documented_fields(tmp_path):
    sink = ListSink()
    mon, scripted, _ = make_monitor(tmp_path, sink)
    scripted.down.add("board")
    for cycle in range(3):
        mon.cycle(cycle * 60)
    scripted.down.clear()
    mon.cycle(180)
    assert [p["status"] for p in sink.sent] == ["alert", "recovered"]
    for payload in sink.sent:
        assert set(payload) == {"source", "host_label", "check", "status", "consecutive_failures",
                                "latency_ms", "message", "ts"}
        assert payload["ts"].endswith("Z")


def test_end_to_end_run_alerts_recovers_and_never_logs_a_secret(baton, webhook, token_file, webhook_token_file,
                                                                tmp_path, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    args = args_for(baton, token_file, tmp_path, "--sink", "webhook", "--webhook-url", webhook.url,
                    "--webhook-token-file", str(webhook_token_file),
                    "--interval", "0.05", "--failures", "3", "--max-cycles", "6", "--port", "0")
    baton.board_status = 500
    monitor.run(args)
    baton.board_status = 200
    args.max_cycles = 2
    monitor.run(args)
    assert [e["status"] for e in webhook.events()] == ["alert", "recovered"]
    assert all(r["auth"] == "Bearer " + WEBHOOK_TOKEN for r in webhook.received)

    out = capsys.readouterr()
    haystack = caplog.text + out.out + out.err + (tmp_path / "state.json").read_text()
    haystack += "".join(json.dumps(r["body"]) for r in webhook.received)
    for secret in SECRETS:
        assert secret not in haystack


def test_secrets_stay_out_of_logs_when_every_target_is_down(token_file, webhook_token_file, tmp_path, caplog, capsys):
    dead_baton, dead_hook = Server(BaseHTTPRequestHandler), Server(BaseHTTPRequestHandler)
    baton_url, hook_url = dead_baton.url, dead_hook.url + "/hook/" + WEBHOOK_PATH_SECRET
    dead_baton.close()
    dead_hook.close()
    caplog.set_level(logging.DEBUG)
    args = monitor.parse_args(["--url", baton_url, "--token-file", str(token_file), "--sink", "webhook",
                               "--webhook-url", hook_url, "--webhook-token-file", str(webhook_token_file),
                               "--state-file", str(tmp_path / "s.json"), "--failures", "1", "--timeout", "2",
                               "--interval", "0.05", "--max-cycles", "2", "--port", "0"])
    monitor.run(args)
    out = capsys.readouterr()
    haystack = caplog.text + out.out + out.err
    assert "not delivered" in haystack
    for secret in SECRETS:
        assert secret not in haystack
    assert "127.0.0.1" not in haystack, "URLs are never logged"


def test_endpoint_repr_hides_the_url():
    endpoint = monitor.Endpoint("https://example.com/hook/" + WEBHOOK_PATH_SECRET, "webhook URL")
    assert WEBHOOK_PATH_SECRET not in repr(endpoint) and WEBHOOK_PATH_SECRET not in str(endpoint)


@pytest.mark.parametrize("url", ["http://example.com/hook", "ftp://example.com/", "https://user:pw@example.com/",
                                 "https:///nohost", "example.com"])
def test_unsafe_or_malformed_urls_are_refused_without_echoing_them(url):
    with pytest.raises(monitor.UsageError) as caught:
        monitor.Endpoint(url, "--url")
    assert "example.com" not in str(caught.value) and "pw" not in str(caught.value)


def test_token_file_must_hold_a_single_printable_token(tmp_path):
    path = tmp_path / "t"
    path.write_text("two words\n")
    with pytest.raises(monitor.UsageError) as caught:
        monitor.read_secret(path, "--token-file")
    assert "two words" not in str(caught.value)
    with pytest.raises(monitor.UsageError):
        monitor.read_secret(tmp_path / "missing", "--token-file")


def test_url_ending_in_mcp_is_accepted_like_baton_url(baton, token_file, tmp_path):
    args = monitor.parse_args(["--url", baton.url + "/mcp", "--token-file", str(token_file),
                               "--state-file", str(tmp_path / "s.json"), "--sink", "stdout"])
    for probe in monitor.build_probes(args).values():
        probe()


class Clock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def api(tmp_path):
    """A Monitor with scripted probes, no push sink, and its read-only API on an ephemeral loopback port."""
    clock = Clock(1_000_000.0)
    mon, scripted, _ = make_monitor(tmp_path, None, clock=clock)
    server = monitor.AlertServer(mon, "127.0.0.1", 0)
    server.start()
    yield SimpleApi(mon, scripted, clock, server)
    server.close()


class SimpleApi:
    def __init__(self, mon, scripted, clock, server):
        self.mon, self.scripted, self.clock, self.server = mon, scripted, clock, server

    def get(self, path, method="GET", host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        try:
            conn.putrequest(method, path, skip_host=host is not None)
            if host is not None:
                conn.putheader("Host", host)
            conn.endheaders()
            response = conn.getresponse()
            raw = response.read()
            return response.status, json.loads(raw), response.getheader("Content-Type")
        finally:
            conn.close()

    def probe(self, seconds=0):
        self.clock.now += seconds
        self.mon.cycle()

    def alerts(self):
        status, body, _ = self.get("/api/v1/alerts")
        assert status == 200
        return body["data"]["alerts"]


def test_alerts_list_is_empty_when_healthy(api):
    api.probe()
    status, body, ctype = api.get("/api/v1/alerts")
    assert (status, ctype) == (200, "application/json")
    assert body == {"status": "success", "data": {"alerts": []}}


def test_alerts_list_is_empty_before_the_first_probe(api):
    assert api.alerts() == []


def test_alert_appears_after_three_failures_in_vmalert_format_with_stable_active_at(api):
    api.scripted.down.add("board")
    api.probe()
    first_failure = api.clock.now
    api.probe(60)
    assert api.alerts() == [], "two failures do not fire"
    api.probe(60)
    [alert] = api.alerts()
    assert set(alert) == {"labels", "annotations", "state", "activeAt", "value"}
    assert alert["value"] == "3" and isinstance(alert["value"], str)
    assert alert["labels"] == {"alertname": "BatonBoardDown", "severity": "critical", "service": "baton"}
    assert set(alert["annotations"]) == {"summary", "description"}
    assert "http_500" in alert["annotations"]["description"] and "test-host" in alert["annotations"]["description"]
    assert alert["state"] == "firing"
    assert alert["activeAt"] == monitor.utc_iso(first_failure)
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", alert["activeAt"])
    for _ in range(5):
        api.probe(60)
        [again] = api.alerts()
        assert again["activeAt"] == alert["activeAt"], "activeAt stays the start of the streak"


def test_alert_disappears_on_recovery_and_a_new_streak_gets_a_new_active_at(api):
    api.scripted.down.add("mcp_call")
    for _ in range(3):
        api.probe(60)
    [alert] = api.alerts()
    assert alert["labels"]["alertname"] == "BatonMcpCallFailed"
    api.scripted.down.clear()
    api.probe(60)
    assert api.alerts() == []
    api.scripted.down.add("mcp_call")
    for _ in range(3):
        api.probe(60)
    [second] = api.alerts()
    assert second["activeAt"] > alert["activeAt"]


def test_each_check_has_its_own_alertname_and_they_clear_independently(api):
    api.scripted.down.update(monitor.CHECKS)
    for _ in range(3):
        api.probe(60)
    assert [a["labels"]["alertname"] for a in api.alerts()] == [
        "BatonBoardDown", "BatonMcpAuthBroken", "BatonMcpCallFailed"]
    api.scripted.down.discard("mcp_unauth")
    api.probe(60)
    assert [a["labels"]["alertname"] for a in api.alerts()] == ["BatonBoardDown", "BatonMcpCallFailed"]


def test_alerts_do_not_depend_on_a_push_sink(api):
    assert api.mon.sink is None
    api.scripted.down.add("board")
    for _ in range(3):
        api.probe(60)
    assert len(api.alerts()) == 1


def test_healthz_is_503_until_a_probe_completes_then_200_with_checks(api):
    status, body, _ = api.get("/healthz")
    assert status == 503 and body["last_probe_at"] is None
    api.probe()
    status, body, _ = api.get("/healthz")
    assert status == 200
    assert body["last_probe_at"] == monitor.utc_iso(api.clock.now)
    assert set(body["checks"]) == set(monitor.CHECKS)
    assert body["checks"]["board"]["ok"] is True and isinstance(body["checks"]["board"]["latency_ms"], int)


def test_healthz_stays_200_while_a_check_fails_because_the_monitor_itself_is_alive(api):
    api.scripted.down.add("board")
    api.probe()
    status, body, _ = api.get("/healthz")
    assert status == 200
    assert body["checks"]["board"] == {"ok": False, "error": "http_500", "latency_ms": body["checks"]["board"]["latency_ms"],
                                       "consecutive_failures": 1}


def test_healthz_turns_503_when_no_probe_completed_within_three_intervals(api):
    api.probe()
    api.clock.now += 3 * 60
    assert api.get("/healthz")[0] == 200
    api.clock.now += 1
    status, body, _ = api.get("/healthz")
    assert status == 503 and body["status"] == "stale"
    api.probe()
    assert api.get("/healthz")[0] == 200


def test_healthz_is_updated_by_every_probe_not_only_at_the_end_of_a_cycle(tmp_path):
    clock = Clock(500.0)
    seen = []
    mon = None

    def make(name):
        def probe():
            seen.append((name, mon.health(clock.now)[1]["last_probe_at"]))
            clock.now += 5
        return probe

    mon = monitor.Monitor({name: make(name) for name in monitor.CHECKS}, None, monitor.State(None),
                          host_label="h", clock=clock)
    mon.cycle()
    assert [stamp for _, stamp in seen] == [None, monitor.utc_iso(505.0), monitor.utc_iso(510.0)]


def test_unknown_paths_and_methods_are_rejected(api):
    assert api.get("/")[0] == 404 and api.get("/api/v1/rules")[0] == 404
    assert api.get("/api/v1/alerts", method="POST")[0] == 405


def test_api_output_never_contains_a_secret_or_url(api):
    api.scripted.down.update(monitor.CHECKS)
    for _ in range(3):
        api.probe(60)
    text = json.dumps(api.alerts()) + json.dumps(api.get("/healthz")[1])
    for secret in SECRETS:
        assert secret not in text
    assert "http://" not in text and "https://" not in text


def test_server_listens_on_loopback_only(api):
    host, port = api.server.httpd.server_address[:2]
    assert host == "127.0.0.1" and port == api.server.port
    assert api.server.httpd.socket.getsockname()[0] == "127.0.0.1"


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "127.0.0.2"])
def test_loopback_addresses_are_accepted(host):
    assert ipaddress.ip_address(monitor.resolve_bind(host, False)).is_loopback


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.0.2.10", "203.0.113.5", "example.com", "", "baton"])
def test_non_loopback_binds_are_refused_without_unsafe_bind(host):
    with pytest.raises(monitor.UsageError) as caught:
        monitor.resolve_bind(host, False)
    assert host not in str(caught.value) or host == ""


def test_unsafe_bind_flag_allows_a_non_loopback_address(tmp_path):
    assert monitor.resolve_bind("0.0.0.0", True) == "0.0.0.0"
    mon, _, _ = make_monitor(tmp_path, None)
    with pytest.raises(monitor.UsageError):
        monitor.AlertServer(mon, "0.0.0.0", 0)
    server = monitor.AlertServer(mon, "0.0.0.0", 0, unsafe=True)
    server.close()


def test_default_port_and_bind_and_sink_are_pull_mode_defaults(token_file):
    args = monitor.parse_args(["--url", "http://127.0.0.1:1", "--token-file", str(token_file)])
    assert (args.bind, args.port, args.sink, args.unsafe_bind) == ("127.0.0.1", 19801, "none", False)
    assert monitor.build_sink(args) is None
    assert not hasattr(args, "heartbeat_minutes")


def test_a_busy_port_is_a_usage_error(tmp_path):
    mon, _, _ = make_monitor(tmp_path, None)
    first = monitor.AlertServer(mon, "127.0.0.1", 0)
    try:
        with pytest.raises(monitor.UsageError, match="cannot bind"):
            monitor.AlertServer(mon, "127.0.0.1", first.port)
    finally:
        first.close()


def test_run_serves_the_api_for_real_probes_until_stopped(baton, token_file, tmp_path):
    probe_socket = socket.socket()
    probe_socket.bind(("127.0.0.1", 0))
    port = probe_socket.getsockname()[1]
    probe_socket.close()
    args = args_for(baton, token_file, tmp_path, "--port", str(port), "--interval", "0.1", "--failures", "2")
    stop = threading.Event()
    result = []
    thread = threading.Thread(target=lambda: result.append(monitor.run(args, stop)))
    thread.start()

    def fetch(path):
        for _ in range(100):
            try:
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                conn.request("GET", path)
                response = conn.getresponse()
                return response.status, json.loads(response.read())
            except OSError:
                time.sleep(0.05)
        raise AssertionError("alert API did not come up")

    try:
        deadline = time.time() + 20
        while fetch("/healthz")[0] != 200 and time.time() < deadline:
            time.sleep(0.05)
        assert fetch("/api/v1/alerts") == (200, {"status": "success", "data": {"alerts": []}})
        baton.board_status = 500
        alerts = []
        while not alerts and time.time() < deadline:
            time.sleep(0.05)
            alerts = fetch("/api/v1/alerts")[1]["data"]["alerts"]
        assert [a["labels"]["alertname"] for a in alerts] == ["BatonBoardDown"]
        baton.board_status = 200
        while alerts and time.time() < deadline:
            time.sleep(0.05)
            alerts = fetch("/api/v1/alerts")[1]["data"]["alerts"]
        assert alerts == []
    finally:
        stop.set()
        thread.join(30)
    assert result == [monitor.EXIT_OK]
    with pytest.raises(OSError):
        http.client.HTTPConnection("127.0.0.1", port, timeout=2).request("GET", "/healthz")


def deadline_args(url, token_file, tmp_path, *extra):
    return monitor.parse_args(["--url", url, "--token-file", str(token_file), "--state-file",
                               str(tmp_path / "state.json"), "--sink", "stdout", *extra])


def timed(function):
    started = time.monotonic()
    try:
        function()
    except monitor.ProbeError as exc:
        return str(exc), time.monotonic() - started
    raise AssertionError("probe unexpectedly succeeded")


@pytest.mark.parametrize("mode", ["drip_body", "drip_headers", "sse_forever", "silent"])
@pytest.mark.parametrize("check", ["board", "mcp_unauth", "mcp_call"])
def test_a_server_that_never_finishes_cannot_hold_a_probe_past_its_deadline(misbehaving, token_file, tmp_path,
                                                                            mode, check):
    server = misbehaving(mode)
    args = deadline_args(server.url, token_file, tmp_path, "--timeout", "5", "--probe-deadline", "1")
    category, elapsed = timed(monitor.build_probes(args)[check])
    assert category == "timeout"
    assert 0.8 <= elapsed < 3.5, "the hard deadline, not --timeout (5 s), ended the probe"


def test_per_socket_timeout_still_applies_below_the_deadline(misbehaving, token_file, tmp_path):
    server = misbehaving("silent")
    args = deadline_args(server.url, token_file, tmp_path, "--timeout", "0.3", "--probe-deadline", "30")
    category, elapsed = timed(monitor.build_probes(args)["board"])
    assert category == "timeout" and elapsed < 3


def test_the_deadline_covers_all_requests_of_the_mcp_call_together(baton, token_file, tmp_path):
    class Delay(dict):
        def __contains__(self, method):
            if method:
                time.sleep(0.4)
            return False

    baton.override = Delay()
    args = deadline_args(baton.url, token_file, tmp_path, "--timeout", "5", "--probe-deadline", "1")
    category, elapsed = timed(monitor.build_probes(args)["mcp_call"])
    assert category == "timeout" and elapsed < 2.5, "three 0.4 s steps exceed the 1 s budget"


def test_a_blocking_dns_lookup_is_abandoned_by_the_hard_deadline(monkeypatch, token_file, tmp_path):
    release = threading.Event()
    real = socket.getaddrinfo

    def blocked(host, *args, **kwargs):
        if host == "baton.example.com":
            release.wait(30)
            raise socket.gaierror
        return real(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    args = deadline_args("https://baton.example.com", token_file, tmp_path, "--probe-deadline", "30")
    mon = monitor.Monitor(monitor.build_probes(args), None, monitor.State(None), host_label="h", hard_deadline=0.3)
    try:
        started = time.monotonic()
        mon.cycle()
        assert time.monotonic() - started < 5
        assert {mon.results[name][0] for name in monitor.CHECKS} == {"timeout"}
        assert mon.health()[0] == 200, "the cycle finished and published its results"
        threads = threading.active_count()
        mon.cycle()
        assert threading.active_count() == threads, "no new thread while the abandoned lookups are in flight"
    finally:
        release.set()
    for thread in list(mon.runner.inflight.values()):
        thread.join(5)
    mon.cycle()
    assert {mon.results[name][0] for name in monitor.CHECKS} == {"connection_error"}


def test_a_hung_probe_does_not_stop_the_other_probes_or_later_cycles(tmp_path):
    release = threading.Event()
    scripted = Scripted()
    probes = scripted.probes()
    probes["board"] = lambda: release.wait(30)
    mon = monitor.Monitor(probes, None, monitor.State(tmp_path / "s.json"), host_label="h", failures=3,
                          hard_deadline=0.2)
    try:
        for _ in range(3):
            started = time.monotonic()
            mon.cycle()
            assert time.monotonic() - started < 3
        assert mon.results["board"][0] == "timeout"
        assert mon.results["mcp_call"][0] is None, "the other probes still ran"
        assert [a["labels"]["alertname"] for a in mon.alerts()["data"]["alerts"]] == ["BatonBoardDown"]
    finally:
        release.set()


def test_a_hung_sink_does_not_stall_the_cycle_and_the_message_is_retried(tmp_path):
    release = threading.Event()
    sent = []

    class HangingSink:
        def send(self, payload):
            if not release.is_set():
                release.wait(30)
            sent.append(payload)

    scripted = Scripted()
    scripted.down.add("board")
    mon = monitor.Monitor(scripted.probes(), HangingSink(), monitor.State(tmp_path / "s.json"), host_label="h",
                          failures=1, hard_deadline=0.3)
    started = time.monotonic()
    mon.cycle()
    assert time.monotonic() - started < 5
    assert mon.state.entry("board")["alerting"] is False
    release.set()
    for thread in list(mon.runner.inflight.values()):
        thread.join(5)
    mon.cycle()
    assert mon.state.entry("board")["alerting"] is True


def test_the_webhook_sink_has_a_hard_deadline_too(misbehaving, tmp_path):
    server = misbehaving("drip_body")
    sink = monitor.WebhookSink(server.url, None, 5, None, deadline=0.8)
    started = time.monotonic()
    with pytest.raises(monitor.SinkError, match="^timeout$"):
        sink.send({"status": "alert"})
    assert time.monotonic() - started < 3


def test_tls_handshake_failure_is_a_fixed_category(baton, token_file, tmp_path):
    https_url = baton.url.replace("http://", "https://")
    args = deadline_args(https_url, token_file, tmp_path, "--timeout", "3")
    for probe in monitor.build_probes(args).values():
        with pytest.raises(monitor.ProbeError, match="^tls_error$"):
            probe()


def test_oversized_responses_are_rejected(baton, token_file, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "MAX_BODY_BYTES", 100)
    baton.override["initialize"] = (200, "application/json", b"x" * 500)
    probes = real_probes(baton, token_file, tmp_path)
    with pytest.raises(monitor.ProbeError, match="^response_too_large$"):
        probes["mcp_call"]()


@pytest.mark.parametrize("method,reply,category", [
    ("initialize", (200, "application/json", b"not json"), "invalid_response"),
    ("initialize", (200, "application/json", b"[1, 2]"), "invalid_response"),
    ("initialize", (200, "text/event-stream", b"event: message\n\n"), "invalid_response"),
    ("initialize", (200, "text/event-stream", b"data: {broken\n\n"), "invalid_response"),
    ("initialize", (200, "application/json", b'{"jsonrpc":"2.0","id":1,"error":{"code":-1,"message":"no"}}'),
     "initialize_rejected"),
    ("initialize", (200, "application/json", b'{"jsonrpc":"2.0","id":1,"result":"nope"}'), "initialize_rejected"),
    ("initialize", (500, "application/json", b"{}"), "initialize_http_500"),
    ("notifications/initialized", (500, "application/json", b"{}"), "initialized_http_500"),
    ("tools/call", (200, "application/json", b"garbage"), "invalid_response"),
    ("tools/call", (503, "application/json", b"{}"), "fetch_inbox_http_503"),
    ("tools/call", (200, "application/json", b'{"jsonrpc":"2.0","id":2,"error":{"code":-32000,"message":"x"}}'),
     "fetch_inbox_failed"),
])
def test_malformed_or_rejected_mcp_replies_fail_with_fixed_categories(baton, token_file, tmp_path, method, reply,
                                                                      category):
    baton.override[method] = reply
    with pytest.raises(monitor.ProbeError, match="^" + category + "$"):
        real_probes(baton, token_file, tmp_path)["mcp_call"]()


def test_state_write_failure_is_logged_and_does_not_stop_probing_or_serving(tmp_path, caplog):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x")
    clock = Clock(1000.0)
    scripted = Scripted()
    scripted.down.add("board")
    mon = monitor.Monitor(scripted.probes(), None, monitor.State(blocker / "state.json"), host_label="h",
                          clock=clock)
    for _ in range(3):
        clock.now += 60
        mon.cycle()
    assert "cannot write state file" in caplog.text
    assert len(mon.alerts()["data"]["alerts"]) == 1 and mon.health()[0] == 200
    assert str(blocker) not in caplog.text


def test_state_write_failure_when_the_file_cannot_be_replaced(tmp_path, caplog, monkeypatch):
    state = monitor.State(tmp_path / "state.json")
    monkeypatch.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(PermissionError()))
    state.save()
    assert "cannot write state file" in caplog.text
    assert not list(tmp_path.glob(".monitor-*")), "no temporary file is left behind"


def test_a_failed_recovery_is_retried_and_a_new_failure_keeps_the_original_alert_open(tmp_path):
    sink = ListSink()
    mon, scripted, _ = make_monitor(tmp_path, sink, realert_seconds=3600.0)
    scripted.down.add("board")
    for cycle in range(3):
        mon.cycle(cycle * 60)
    assert [p["status"] for p in sink.sent] == ["alert"]
    scripted.down.clear()
    sink.fail = True
    mon.cycle(180)
    mon.cycle(240)
    assert mon.state.entry("board")["alerting"] is True, "recovery keeps being retried"
    scripted.down.add("board")
    for cycle in range(5, 12):
        mon.cycle(cycle * 60)
    assert [p["status"] for p in sink.sent] == ["alert"], "the open alert is not repeated before the re-alert time"
    scripted.down.clear()
    sink.fail = False
    mon.cycle(12 * 60)
    assert [p["status"] for p in sink.sent] == ["alert", "recovered"]
    mon.cycle(13 * 60)
    assert len(sink.sent) == 2


def test_state_is_only_changed_under_the_lock_and_never_while_the_sink_runs(tmp_path):
    seen = {}
    entered = threading.Event()
    release = threading.Event()

    class ProbeSink:
        def send(self, payload):
            seen["locked_during_send"] = mon.lock.locked()
            entered.set()
            release.wait(10)

    scripted = Scripted()
    scripted.down.add("board")
    mon = monitor.Monitor(scripted.probes(), ProbeSink(), monitor.State(tmp_path / "s.json"), host_label="h",
                          failures=1)
    worker = threading.Thread(target=mon.cycle)
    worker.start()
    assert entered.wait(10)
    started = time.monotonic()
    assert len(mon.alerts()["data"]["alerts"]) == 1 and mon.health()[0] in (200, 503)
    assert time.monotonic() - started < 1, "the API is served while a delivery is in progress"
    release.set()
    worker.join(10)
    assert seen["locked_during_send"] is False
    assert mon.state.entry("board")["alerting"] is True


def test_concurrent_readers_never_see_inconsistent_state(tmp_path):
    scripted = Scripted()
    mon = monitor.Monitor(scripted.probes(), None, monitor.State(tmp_path / "s.json"), host_label="h", failures=2)
    errors = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                for alert in mon.alerts()["data"]["alerts"]:
                    assert int(alert["value"]) >= 2 and alert["activeAt"].endswith("Z")
                mon.health()
            except Exception as exc:
                errors.append(exc)

    readers = [threading.Thread(target=reader) for _ in range(4)]
    for thread in readers:
        thread.start()
    for index in range(60):
        scripted.down.symmetric_difference_update({"board"} if index % 7 < 4 else set())
        mon.cycle()
    stop.set()
    for thread in readers:
        thread.join(10)
    assert errors == []


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity", "1e400", "-5", "true", '"x"', "null"])
def test_non_finite_or_invalid_numbers_in_state_are_repaired_on_load(tmp_path, bad):
    (tmp_path / "state.json").write_text(
        '{"checks": {"board": {"consecutive_failures": 5, "alerting": true, "last_alert": %s, '
        '"streak_start": %s, "error": "http_500"}}}' % (bad, bad))
    state = monitor.State(tmp_path / "state.json")
    state.load(5000.0)
    entry = state.entry("board")
    assert entry["streak_start"] == 5000.0, "an unusable start becomes 'now', never 1970"
    assert entry["last_alert"] is None
    mon = monitor.Monitor(Scripted().probes(), None, state, host_label="h", clock=Clock(5000.0))
    [alert] = mon.alerts()["data"]["alerts"]
    assert alert["activeAt"] == monitor.utc_iso(5000.0)


@pytest.mark.parametrize("bad", ['"5"', "NaN", "Infinity", "-1", "true", "1e400", "99999999999"])
def test_invalid_failure_counts_in_state_are_ignored(tmp_path, bad):
    (tmp_path / "state.json").write_text(
        '{"checks": {"board": {"consecutive_failures": %s, "streak_start": 10}}}' % bad)
    state = monitor.State(tmp_path / "state.json")
    state.load(5000.0)
    assert state.entry("board")["consecutive_failures"] == 0 and state.entry("board")["streak_start"] is None


def test_failures_without_a_streak_start_in_state_do_not_become_1970(tmp_path):
    (tmp_path / "state.json").write_text('{"checks": {"board": {"consecutive_failures": 4}}}')
    state = monitor.State(tmp_path / "state.json")
    state.load(7000.0)
    mon = monitor.Monitor(Scripted().probes(), None, state, host_label="h", clock=Clock(7000.0))
    assert mon.alerts()["data"]["alerts"][0]["activeAt"] == monitor.utc_iso(7000.0)


def test_a_failure_inside_the_alerts_handler_gives_500_not_a_dropped_connection(api, monkeypatch):
    monkeypatch.setattr(api.mon, "alerts", lambda: (_ for _ in ()).throw(OverflowError()))
    status, body, _ = api.get("/api/v1/alerts")
    assert status == 500 and body["status"] == "error"
    assert api.get("/healthz")[0] in (200, 503)


@pytest.mark.parametrize("flag,value", [
    ("--interval", "nan"), ("--interval", "inf"), ("--interval", "-inf"), ("--interval", "0"),
    ("--interval", "1e400"), ("--interval", "1e9"),
    ("--timeout", "nan"), ("--timeout", "inf"), ("--timeout", "0"),
    ("--probe-deadline", "nan"), ("--probe-deadline", "inf"), ("--probe-deadline", "-1"),
    ("--realert-minutes", "nan"), ("--realert-minutes", "inf"), ("--realert-minutes", "-1"),
    ("--realert-minutes", "1e300"),
    ("--failures", "0"), ("--failures", "nan"), ("--port", "70000"),
])
def test_numeric_options_reject_nan_inf_and_out_of_range_values(token_file, capsys, flag, value):
    with pytest.raises(SystemExit) as caught:
        monitor.parse_args(["--url", "http://127.0.0.1:1", "--token-file", str(token_file), flag + "=" + value])
    assert caught.value.code == 2
    assert "error" in capsys.readouterr().err


def test_numeric_options_accept_sane_values(token_file):
    args = monitor.parse_args(["--url", "http://127.0.0.1:1", "--token-file", str(token_file), "--interval", "0.5",
                               "--timeout", "2.5", "--probe-deadline", "20", "--realert-minutes", "0"])
    assert (args.interval, args.timeout, args.probe_deadline, args.realert_minutes) == (0.5, 2.5, 20.0, 0.0)
    assert monitor.parse_args(["--url", "http://127.0.0.1:1", "--token-file", str(token_file)]).probe_deadline == 30.0


def test_an_explicit_port_zero_in_a_url_is_rejected_not_replaced_by_the_default():
    for url in ("http://127.0.0.1:0", "https://example.com:0/x"):
        with pytest.raises(monitor.UsageError, match="port 0"):
            monitor.Endpoint(url, "--url")


def test_ports_in_urls_keep_their_value_and_defaults_apply_only_when_absent():
    assert monitor.Endpoint("https://example.com", "u").port == 443
    assert monitor.Endpoint("http://127.0.0.1", "u").port == 80
    assert monitor.Endpoint("http://127.0.0.1:80", "u").port == 80
    assert monitor.Endpoint("https://example.com:8443/x", "u").port == 8443
    with pytest.raises(monitor.UsageError):
        monitor.Endpoint("https://example.com:65536", "u")


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.1:19801", "localhost", "LOCALHOST:5000", "[::1]", "[::1]:7"])
def test_loopback_host_headers_are_served(api, host):
    assert api.get("/healthz", host=host)[0] in (200, 503)
    assert api.get("/api/v1/alerts", host=host)[0] == 200


@pytest.mark.parametrize("host", ["evil.example.com", "evil.example.com:19801", "127.0.0.1.evil.example.com",
                                  "localhost.evil.example.com", "192.0.2.1", "0.0.0.0", "127.0.0.1:99999x", "",
                                  "127.0.0.1@example.com", "[::2]"])
def test_foreign_host_headers_are_refused_against_dns_rebinding(api, host):
    api.scripted.down.update(monitor.CHECKS)
    for _ in range(3):
        api.probe(60)
    for path in ("/api/v1/alerts", "/healthz"):
        status, body, _ = api.get(path, host=host)
        assert status == 403 and body == {"status": "error", "error": "host not allowed"}


def test_a_request_without_a_host_header_is_refused(api):
    with socket.create_connection(("127.0.0.1", api.server.port), timeout=5) as sock:
        sock.sendall(b"GET /api/v1/alerts HTTP/1.0\r\n\r\n")
        data = b""
        while chunk := sock.recv(4096):
            data += chunk
    assert data.startswith(b"HTTP/1.0 403")


def test_unsafe_bind_does_not_enforce_the_host_header(tmp_path):
    mon, _, _ = make_monitor(tmp_path, None)
    server = monitor.AlertServer(mon, "127.0.0.1", 0, unsafe=True)
    server.start()
    try:
        helper = SimpleApi(mon, Scripted(), Clock(), server)
        assert helper.get("/api/v1/alerts", host="relay.example.com")[0] == 200
    finally:
        server.close()


def small_api(monkeypatch, tmp_path, *, request_timeout, lifetime, connections):
    monkeypatch.setattr(monitor, "API_REQUEST_TIMEOUT", request_timeout)
    monkeypatch.setattr(monitor, "API_CONNECTION_LIFETIME", lifetime)
    monkeypatch.setattr(monitor, "API_MAX_CONNECTIONS", connections)
    mon, scripted, _ = make_monitor(tmp_path, None, clock=Clock(1000.0))
    server = monitor.AlertServer(mon, "127.0.0.1", 0)
    server.start()
    return SimpleApi(mon, scripted, Clock(), server)


def closed_by_server(sock, within):
    sock.settimeout(within)
    try:
        return sock.recv(1) == b""
    except (socket.timeout, TimeoutError):
        return False
    except ConnectionError:
        return True


def test_idle_connections_are_timed_out_and_cannot_exhaust_the_server(monkeypatch, tmp_path):
    api = small_api(monkeypatch, tmp_path, request_timeout=0.4, lifetime=5.0, connections=4)
    try:
        idle = [socket.create_connection(("127.0.0.1", api.server.port), timeout=5) for _ in range(40)]
        time.sleep(1.5)
        assert all(closed_by_server(sock, 2) for sock in idle), "every idle connection was dropped"
        for sock in idle:
            sock.close()
        assert api.get("/healthz")[0] == 503
        assert api.alerts() == []
    finally:
        api.server.close()


def test_concurrency_is_capped_and_excess_connections_are_dropped_at_once(monkeypatch, tmp_path):
    api = small_api(monkeypatch, tmp_path, request_timeout=3.0, lifetime=6.0, connections=3)
    held = []
    try:
        for _ in range(3):
            held.append(socket.create_connection(("127.0.0.1", api.server.port), timeout=5))
        time.sleep(0.3)
        started = time.monotonic()
        extra = socket.create_connection(("127.0.0.1", api.server.port), timeout=5)
        extra.sendall(b"GET /healthz HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
        assert closed_by_server(extra, 1.5) and time.monotonic() - started < 1.5
        extra.close()
        for sock in held:
            sock.close()
        time.sleep(0.3)
        assert api.get("/healthz")[0] == 503, "capacity returns once the slow connections are gone"
    finally:
        for sock in held:
            sock.close()
        api.server.close()


def test_a_slow_drip_request_is_cut_off_by_the_connection_lifetime(monkeypatch, tmp_path):
    api = small_api(monkeypatch, tmp_path, request_timeout=5.0, lifetime=0.6, connections=4)
    try:
        sock = socket.create_connection(("127.0.0.1", api.server.port), timeout=5)
        sock.sendall(b"GET /healthz HTTP/1.1\r\n")
        started = time.monotonic()
        cut = False
        while time.monotonic() - started < 4:
            try:
                sock.sendall(b"X-Slow: 1\r\n")
            except OSError:
                cut = True
                break
            time.sleep(0.1)
        assert cut and time.monotonic() - started < 2.5
        sock.close()
        assert api.get("/healthz")[0] == 503
    finally:
        api.server.close()


def test_clients_that_hang_up_early_do_not_produce_errors_or_break_the_server(api, capsys):
    api.probe()
    for _ in range(30):
        sock = socket.create_connection(("127.0.0.1", api.server.port), timeout=5)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        sock.sendall(b"GET /api/v1/alerts HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        sock.close()
    time.sleep(0.3)
    assert api.get("/healthz")[0] == 200
    assert "Traceback" not in capsys.readouterr().err


def test_reply_swallows_a_broken_pipe(api, monkeypatch):
    api.probe()
    original = http.server.BaseHTTPRequestHandler.end_headers

    def broken(self):
        raise BrokenPipeError

    monkeypatch.setattr(http.server.BaseHTTPRequestHandler, "end_headers", broken)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", api.server.port, timeout=3)
        conn.request("GET", "/healthz")
        with pytest.raises(Exception):
            conn.getresponse().read()
    finally:
        monkeypatch.setattr(http.server.BaseHTTPRequestHandler, "end_headers", original)
    assert api.get("/healthz")[0] == 200


def run_cli(*args, **kwargs):
    env = {key: value for key, value in os.environ.items() if key not in ("BATON_URL",)}
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, timeout=60,
                          env=env, **kwargs)


def test_cli_once_with_stdout_sink_exit_codes(baton, token_file, tmp_path):
    base = ["--url", baton.url, "--token-file", str(token_file), "--state-file", str(tmp_path / "s.json"),
            "--sink", "stdout", "--once", "--failures", "1"]
    ok = run_cli(*base)
    assert ok.returncode == 0 and ok.stdout == ""
    baton.board_status = 500
    bad = run_cli(*base)
    assert bad.returncode == 1
    event = json.loads(bad.stdout)
    assert event["check"] == "board" and event["status"] == "alert" and event["source"] == "baton-monitor"
    for secret in SECRETS:
        assert secret not in bad.stdout + bad.stderr


def test_cli_refuses_a_non_loopback_bind(token_file):
    refused = run_cli("--url", "http://127.0.0.1:1", "--token-file", str(token_file), "--bind", "0.0.0.0",
                      "--port", "0")
    assert refused.returncode == 2 and "non-loopback" in refused.stderr


def test_cli_usage_errors_exit_two(token_file, tmp_path):
    missing_url = run_cli("--token-file", str(token_file))
    assert missing_url.returncode == 2
    no_hook = run_cli("--url", "http://127.0.0.1:1", "--token-file", str(token_file), "--sink", "webhook", "--once")
    assert no_hook.returncode == 2 and "webhook" in no_hook.stderr
    bad_token = run_cli("--url", "http://127.0.0.1:1", "--token-file", str(tmp_path / "nope"), "--sink", "stdout")
    assert bad_token.returncode == 2


def test_script_is_stdlib_only_with_spdx_header():
    text = SCRIPT.read_text(encoding="utf-8")
    assert text.splitlines()[1] == "# SPDX-License-Identifier: Apache-2.0"
    assert os.access(SCRIPT, os.X_OK)
    imports = {line.split()[1].split(".")[0] for line in text.splitlines() if line.startswith(("import ", "from "))}
    assert imports <= set(sys.stdlib_module_names)
