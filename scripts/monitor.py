#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""External health monitor for a Baton deployment (standard library only).

Probes, every --interval seconds:

  board      GET  /healthz through the edge with the monitoring token  must answer 200 and status ok
  mcp_unauth POST /mcp without credentials        must answer 401
  mcp_call   POST /mcp initialize + fetch_inbox   must succeed with the monitoring token

Pull mode (the default way to use it): a small HTTP server bound to 127.0.0.1 (default
port 19801) lets an alert relay pull the current state, so the host opens no new port.

  GET /api/v1/alerts  vmalert-compatible JSON; a check is listed as firing after
                      --failures consecutive failures and removed when it recovers
  GET /healthz        last probe time and per-check state; 503 if no probe completed
                      within 3 intervals

Every probe has a hard deadline (--probe-deadline), so a server that trickles bytes or
never closes a stream cannot hang the monitor.

Optionally (off by default) --sink webhook|stdout also pushes one alert after
--failures consecutive failures, one recovery message, and a reminder every
--realert-minutes while still down.

Secrets (the monitoring token, the optional webhook bearer token) are read from files
given by path. They, and the webhook URL (which may itself be a secret), are never
printed or logged; errors are reduced to fixed categories. See
docs/operations/monitoring.md.

Exit codes: 0 clean stop or --once with all probes passing, 1 --once with a failing
probe, 2 usage or configuration error.
"""

import argparse
import http.client
import http.server
import ipaddress
import json
import logging
import math
import os
import re
import signal
import socket
import ssl
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

SOURCE = "baton-monitor"
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
CHECKS = ("board", "mcp_unauth", "mcp_call")
MAX_BODY_BYTES = 4 * 1024 * 1024
MAX_FAILURES = 1_000_000
HARD_DEADLINE_GRACE = 2.0
API_REQUEST_TIMEOUT = 5.0
API_CONNECTION_LIFETIME = 10.0
API_MAX_CONNECTIONS = 16
API_HOST_RE = re.compile(r"(?:127\.0\.0\.1|localhost|\[::1\])(?::[0-9]{1,5})?", re.IGNORECASE)
MCP_PROTOCOL_VERSION = "2025-03-26"
MCP_ACCEPT = "application/json, text/event-stream"
STATUS_ALERT, STATUS_RECOVERED = "alert", "recovered"
EXIT_OK, EXIT_FAILING, EXIT_USAGE = 0, 1, 2
DEFAULT_PORT = 19801
STALE_INTERVALS = 3
LOOPBACK_DEFAULT = "127.0.0.1"
ALERT_TEMPLATES = {
    "board": ("BatonBoardDown", "critical", "Baton board stack is not healthy",
              "GET /healthz with the monitoring token did not return 200 with status ok"),
    "mcp_unauth": ("BatonMcpAuthBroken", "critical", "Baton MCP endpoint does not reject unauthenticated requests",
                   "POST /mcp without credentials did not return 401"),
    "mcp_call": ("BatonMcpCallFailed", "critical", "Baton MCP call with the monitoring token fails",
                 "MCP initialize + fetch_inbox did not succeed"),
}

log = logging.getLogger("baton-monitor")


class UsageError(Exception):
    """Bad invocation or local configuration. The message never contains a secret."""


class ProbeError(Exception):
    """A probe failed; the message is a short fixed category, safe to log and send."""


class SinkError(Exception):
    """Delivery to the alert sink failed; the message is a fixed category."""


def utc_iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_secret(path, what):
    try:
        raw = Path(path).read_text(encoding="ascii")
    except (OSError, ValueError):
        raise UsageError("cannot read " + what) from None
    token = raw.strip()
    if not re.fullmatch(r"[\x21-\x7e]+", token):
        raise UsageError("invalid " + what + ": expected a single printable token")
    return token


class Endpoint:
    """A parsed http(s) URL. repr/str never reveal the URL, which may carry a secret."""

    def __init__(self, url, what):
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError:
            raise UsageError("invalid " + what) from None
        scheme, host = parts.scheme.lower(), parts.hostname
        if (scheme not in ("http", "https") or not host or parts.username is not None
                or parts.password is not None or parts.fragment):
            raise UsageError("invalid " + what + ": expected http(s)://host[:port]/path without userinfo")
        if scheme == "http" and host not in LOCAL_HOSTS:
            raise UsageError("refusing plain http:// for a non-local host in " + what + "; use https://")
        if port == 0:
            raise UsageError("invalid " + what + ": port 0 is not connectable")
        self.scheme, self.host, self.port = scheme, host, port if port is not None else (443 if scheme == "https" else 80)
        self.prefix = parts.path.rstrip("/")
        self.query = ("?" + parts.query) if parts.query else ""

    def __repr__(self):
        return "Endpoint(<redacted>)"

    __str__ = __repr__

    def path(self, suffix=""):
        return (self.prefix + suffix or "/") + self.query

    def host_header(self):
        host = "[" + self.host + "]" if ":" in self.host else self.host
        default = 443 if self.scheme == "https" else 80
        return host if self.port == default else "%s:%d" % (host, self.port)


def tls_context(ca_file):
    try:
        context = ssl.create_default_context(cafile=ca_file or None)
    except (OSError, ssl.SSLError):
        raise UsageError("cannot load --ca-file") from None
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


class Budget:
    """Wall-clock allowance for everything one probe (or one sink delivery) does."""

    def __init__(self, seconds):
        self.end = time.monotonic() + seconds

    def remaining(self):
        return self.end - time.monotonic()


def http_request(endpoint, method, suffix, *, headers=None, body=None, timeout, context, budget):
    """One HTTP exchange. Returns (status, response headers, body bytes).

    --timeout bounds each socket operation; the budget bounds the whole exchange, so a server
    that trickles bytes or never closes a stream cannot hold the probe: a timer shuts the socket
    down when the budget is spent. No redirects are followed and no proxy environment is
    consulted. Failures are reduced to fixed categories so URLs, headers and bodies cannot
    reach the logs.
    """
    remaining = budget.remaining()
    if remaining <= 0:
        raise ProbeError("timeout")
    per_op = min(timeout, remaining)
    if endpoint.scheme == "https":
        conn = http.client.HTTPSConnection(endpoint.host, endpoint.port, timeout=per_op, context=context)
    else:
        conn = http.client.HTTPConnection(endpoint.host, endpoint.port, timeout=per_op)
    merged = {"Host": endpoint.host_header(), "User-Agent": "baton-monitor", "Connection": "close"}
    merged.update(headers or {})
    expired = threading.Event()
    held = []

    def abort():
        expired.set()
        for sock in held:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except (OSError, ValueError):
                pass

    timer = threading.Timer(remaining, abort)
    timer.daemon = True
    timer.start()
    try:
        conn.connect()
        held.append(conn.sock)  # getresponse() detaches conn.sock for "Connection: close" replies
        if expired.is_set():
            raise ProbeError("timeout")
        conn.request(method, endpoint.path(suffix), body=body, headers=merged)
        response = conn.getresponse()
        data = response.read(MAX_BODY_BYTES + 1)
        if expired.is_set():
            raise ProbeError("timeout")
        if len(data) > MAX_BODY_BYTES:
            raise ProbeError("response_too_large")
        return response.status, response, data
    except ProbeError:
        raise
    except (socket.timeout, TimeoutError):
        raise ProbeError("timeout") from None
    except ssl.SSLError:
        raise ProbeError("timeout" if expired.is_set() else "tls_error") from None
    except (OSError, http.client.HTTPException, ValueError):
        raise ProbeError("timeout" if expired.is_set() else "connection_error") from None
    finally:
        timer.cancel()
        conn.close()


def parse_rpc(response, data):
    """Decode a JSON-RPC reply sent either as plain JSON or as a text/event-stream."""
    ctype = (response.getheader("Content-Type") or "").split(";")[0].strip().lower()
    text = data.decode("utf-8", errors="replace")
    try:
        if ctype == "text/event-stream":
            for line in text.splitlines():
                if line.startswith("data:"):
                    message = json.loads(line[5:].strip())
                    if isinstance(message, dict) and ("result" in message or "error" in message):
                        return message
            raise ValueError
        message = json.loads(text)
    except ValueError:
        raise ProbeError("invalid_response") from None
    if not isinstance(message, dict):
        raise ProbeError("invalid_response")
    return message


def probe_board(endpoint, token, timeout, context, deadline):
    headers = {"Authorization": "Bearer " + token}
    status, _, data = http_request(endpoint, "GET", "/healthz", headers=headers, timeout=timeout, context=context,
                                   budget=Budget(deadline))
    if status != 200:
        raise ProbeError("http_%d" % status)
    try:
        body = json.loads(data.decode("utf-8"))
    except ValueError:
        raise ProbeError("invalid_response") from None
    if not isinstance(body, dict) or body.get("status") != "ok":
        raise ProbeError("health_not_ok")


def initialize_body():
    return json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {},
        "clientInfo": {"name": "baton-monitor", "version": "1"}}})


def mcp_headers(token=None, session=None):
    headers = {"Content-Type": "application/json", "Accept": MCP_ACCEPT}
    if token:
        headers["Authorization"] = "Bearer " + token
    if session:
        headers["Mcp-Session-Id"] = session
        headers["MCP-Protocol-Version"] = MCP_PROTOCOL_VERSION
    return headers


def probe_mcp_unauth(endpoint, timeout, context, deadline):
    status, _, _ = http_request(endpoint, "POST", "/mcp", headers=mcp_headers(), body=initialize_body(),
                                timeout=timeout, context=context, budget=Budget(deadline))
    if status != 401:
        raise ProbeError("http_%d_expected_401" % status)


def probe_mcp_call(endpoint, token, timeout, context, deadline):
    budget = Budget(deadline)
    status, response, data = http_request(endpoint, "POST", "/mcp", headers=mcp_headers(token),
                                          body=initialize_body(), timeout=timeout, context=context, budget=budget)
    if status != 200:
        raise ProbeError("initialize_http_%d" % status)
    reply = parse_rpc(response, data)
    if "error" in reply or not isinstance(reply.get("result"), dict):
        raise ProbeError("initialize_rejected")
    session = response.getheader("Mcp-Session-Id")
    headers = mcp_headers(token, session)
    status, _, _ = http_request(endpoint, "POST", "/mcp", headers=headers, timeout=timeout, context=context,
                                budget=budget,
                                body=json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}))
    if status not in (200, 202):
        raise ProbeError("initialized_http_%d" % status)
    call = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                       "params": {"name": "fetch_inbox", "arguments": {}}})
    status, response, data = http_request(endpoint, "POST", "/mcp", headers=headers, body=call,
                                          timeout=timeout, context=context, budget=budget)
    if status != 200:
        raise ProbeError("fetch_inbox_http_%d" % status)
    reply = parse_rpc(response, data)
    result = reply.get("result")
    if "error" in reply or not isinstance(result, dict) or result.get("isError"):
        raise ProbeError("fetch_inbox_failed")


class StdoutSink:
    """One JSON object per line on stdout; for tests and for piping into other tools."""

    def send(self, payload):
        sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
        sys.stdout.flush()


class WebhookSink:
    """POST the JSON payload to a webhook; an optional bearer token is read from a file."""

    def __init__(self, url, token_file, timeout, ca_file, deadline=None):
        self.endpoint = Endpoint(url, "webhook URL")
        self.token = read_secret(token_file, "webhook token file") if token_file else None
        self.timeout = timeout
        self.deadline = timeout if deadline is None else deadline
        self.context = tls_context(ca_file) if self.endpoint.scheme == "https" else None

    def send(self, payload):
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        try:
            status, _, _ = http_request(self.endpoint, "POST", "", headers=headers,
                                        body=json.dumps(payload).encode("utf-8"),
                                        timeout=self.timeout, context=self.context,
                                        budget=Budget(self.deadline))
        except ProbeError as exc:
            raise SinkError(str(exc)) from None
        if not 200 <= status < 300:
            raise SinkError("http_%d" % status)


def valid_time(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


class State:
    """Small JSON state file, written atomically with mode 0600."""

    def __init__(self, path):
        self.path = Path(path) if path else None
        self.checks = {}

    def load(self, now):
        if self.path is None:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            checks = data["checks"]
            if not isinstance(checks, dict):
                raise ValueError
        except FileNotFoundError:
            return
        except (OSError, ValueError, KeyError, TypeError):
            log.warning("state file unreadable; starting with fresh state")
            return
        for name in CHECKS:
            entry = checks.get(name)
            if not isinstance(entry, dict):
                continue
            failures = entry.get("consecutive_failures")
            valid = isinstance(failures, int) and not isinstance(failures, bool) and 0 <= failures <= MAX_FAILURES
            failures = failures if valid else 0
            last_alert, start, error = entry.get("last_alert"), entry.get("streak_start"), entry.get("error")
            if failures and not valid_time(start):
                start = now
            self.checks[name] = {
                "consecutive_failures": failures,
                "streak_start": min(start, now) if failures else None,
                "error": error if failures and isinstance(error, str) and re.fullmatch(r"[a-z0-9_]{1,64}", error) else None,
                "alerting": entry.get("alerting") is True,
                "last_alert": min(last_alert, now) if valid_time(last_alert) else None,
            }

    def entry(self, name):
        return self.checks.setdefault(name, {"consecutive_failures": 0, "streak_start": None, "error": None,
                                             "alerting": False, "last_alert": None})

    def save(self):
        if self.path is None:
            return
        doc = {"version": 2, "checks": self.checks}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, tmp = tempfile.mkstemp(prefix=".monitor-", dir=str(self.path.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(doc, handle, sort_keys=True)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(tmp, 0o600)
                os.replace(tmp, self.path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        except OSError:
            log.error("cannot write state file")


class Runner:
    """Run callables on daemon threads under a hard deadline, at most one in flight per key.

    Python cannot kill a thread, so a call that ignores its own limits (a resolver that never
    answers, for example) is abandoned: it counts as a timeout and no new call for that key
    starts until the old one has finished, which keeps leaked threads bounded.
    """

    def __init__(self):
        self.inflight = {}

    def run(self, key, function, deadline):
        previous = self.inflight.get(key)
        if previous is not None and previous.is_alive():
            raise TimeoutError
        box = {}

        def target():
            try:
                box["value"] = function()
            except BaseException as exc:
                box["error"] = exc

        thread = threading.Thread(target=target, name="baton-monitor-" + key, daemon=True)
        thread.start()
        thread.join(deadline)
        if thread.is_alive():
            self.inflight[key] = thread
            raise TimeoutError
        self.inflight.pop(key, None)
        if "error" in box:
            raise box["error"]
        return box.get("value")


class Monitor:
    def __init__(self, probes, sink, state, *, host_label, failures=3, realert_seconds=float("inf"),
                 interval=60.0, clock=time.time, hard_deadline=32.0):
        self.probes, self.sink, self.state = probes, sink, state
        self.host_label, self.failures = host_label, failures
        self.realert_seconds, self.interval, self.clock = realert_seconds, interval, clock
        self.hard_deadline = hard_deadline
        self.runner = Runner()
        self.lock = threading.Lock()
        self.last_probe_at = None
        self.results = {}

    def run_probe(self, name):
        started = time.monotonic()
        try:
            self.runner.run(name, self.probes[name], self.hard_deadline)
            error = None
        except ProbeError as exc:
            error = str(exc)
        except TimeoutError:
            error = "timeout"
        except Exception:
            error = "probe_error"
        return error, int(round((time.monotonic() - started) * 1000))

    def cycle(self, now=None):
        """Run every probe once and publish each result at once; then notify the optional sink.

        All state changes happen under the lock, which is never held across network I/O.
        """
        summary = []
        for name in CHECKS:
            error, latency = self.run_probe(name)
            at = self.clock() if now is None else now
            with self.lock:
                entry = self.state.entry(name)
                failed_before = entry["consecutive_failures"]
                self.results[name] = (error, latency)
                self.last_probe_at = at
                if error is None:
                    entry["consecutive_failures"], entry["streak_start"], entry["error"] = 0, None, None
                else:
                    if failed_before == 0:
                        entry["streak_start"] = at
                    entry["consecutive_failures"] = failed_before + 1
                    entry["error"] = error
                action = None
                if self.sink is not None:
                    action = (self.plan_recovery(name, entry, failed_before, latency, at) if error is None
                              else self.plan_failure(name, entry, error, latency, at))
            if action is not None:
                payload, what, apply = action
                if self.deliver(payload, what):
                    with self.lock:
                        apply(entry)
            summary.append("%s=%s(%dms)" % (name, "ok" if error is None else error, latency))
        log.info("cycle %s", " ".join(summary))
        with self.lock:
            self.state.save()

    def payload(self, check, status, entry, latency_ms, message, now):
        return {"source": SOURCE, "host_label": self.host_label, "check": check, "status": status,
                "consecutive_failures": entry["consecutive_failures"],
                "latency_ms": latency_ms, "message": message, "ts": utc_iso(now)}

    def deliver(self, payload, what):
        try:
            self.runner.run("sink", lambda: self.sink.send(payload), self.hard_deadline)
        except SinkError as exc:
            log.error("%s not delivered (%s); will retry on a later cycle", what, exc)
            return False
        except TimeoutError:
            log.error("%s not delivered (timeout); will retry on a later cycle", what)
            return False
        except Exception:
            log.error("%s not delivered (sink error); will retry on a later cycle", what)
            return False
        log.info("%s delivered", what)
        return True

    def plan_failure(self, name, entry, error, latency, now):
        """Return (payload, label, apply) for the message due now, or None. Call with the lock held."""
        count = entry["consecutive_failures"]
        if count < self.failures:
            return None
        if not entry["alerting"]:
            message = "%s failing: %s (%d consecutive failures)" % (name, error, count)
        elif now - (entry["last_alert"] or 0) >= self.realert_seconds:
            message = "%s still failing: %s (%d consecutive failures)" % (name, error, count)
        else:
            return None

        def apply(target):
            target["alerting"], target["last_alert"] = True, now

        return self.payload(name, STATUS_ALERT, entry, latency, message, now), "alert for " + name, apply

    def plan_recovery(self, name, entry, failed, latency, now):
        """A recovery is retried on every healthy cycle until the sink accepts it.

        If the check fails again before that, the alert stays the open incident: no recovery is
        sent for the interrupted outage and reminders continue on their normal schedule.
        """
        if not entry["alerting"]:
            return None
        message = "%s recovered" % name + (" after %d failed probes" % failed if failed else "")

        def apply(target):
            target["alerting"], target["last_alert"] = False, None

        return self.payload(name, STATUS_RECOVERED, entry, latency, message, now), "recovery for " + name, apply

    def alerts(self):
        """Firing alerts in vmalert's /api/v1/alerts format; recovered checks are simply absent."""
        items = []
        with self.lock:
            for name in CHECKS:
                entry = self.state.entry(name)
                count = entry["consecutive_failures"]
                if count < self.failures:
                    continue
                alertname, severity, summary, detail = ALERT_TEMPLATES[name]
                description = "%s on %s; last error: %s; %d consecutive failed probes" % (
                    detail, self.host_label, entry["error"] or "unknown", count)
                start = entry["streak_start"] if valid_time(entry["streak_start"]) else self.clock()
                items.append({"labels": {"alertname": alertname, "severity": severity, "service": "baton"},
                              "annotations": {"summary": summary, "description": description},
                              "state": "firing", "activeAt": utc_iso(start), "value": str(count)})
        return {"status": "success", "data": {"alerts": items}}

    def health(self, now=None):
        """(HTTP status, body) for /healthz: 503 unless a probe completed within 3 intervals."""
        now = self.clock() if now is None else now
        with self.lock:
            last = self.last_probe_at
            checks = {}
            for name in CHECKS:
                if name in self.results:
                    error, latency = self.results[name]
                    checks[name] = {"ok": error is None, "error": error, "latency_ms": latency,
                                    "consecutive_failures": self.state.entry(name)["consecutive_failures"]}
        fresh = last is not None and now - last <= STALE_INTERVALS * self.interval
        body = {"status": "ok" if fresh else "stale", "last_probe_at": utc_iso(last) if last is not None else None,
                "checks": checks}
        return (200 if fresh else 503), body

    def failing(self):
        with self.lock:
            return any(error is not None for error, _ in self.results.values())


def resolve_bind(host, unsafe):
    """Return the address to bind; anything but loopback needs an explicit --unsafe-bind."""
    if host == "localhost":
        host = LOOPBACK_DEFAULT
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise UsageError("invalid --bind: expected an IP address") from None
    if not address.is_loopback and not unsafe:
        raise UsageError("refusing to bind a non-loopback address; the alert API is unauthenticated "
                         "(use --unsafe-bind only behind your own access control)")
    return str(address)


class AlertServer:
    """Read-only HTTP API for the alert relay: GET /api/v1/alerts and GET /healthz.

    Loopback only by default. Each request has a socket timeout and a total connection
    lifetime, concurrency is capped, and (unless --unsafe-bind) the Host header must name a
    loopback host so that a web page cannot read the API through DNS rebinding.
    """

    def __init__(self, monitor, host, port, unsafe=False):
        host = resolve_bind(host, unsafe)

        class Handler(http.server.BaseHTTPRequestHandler):
            server_version = "baton-monitor"
            sys_version = ""

            def setup(self):
                self.timeout = API_REQUEST_TIMEOUT
                super().setup()
                self.lifetime = threading.Timer(API_CONNECTION_LIFETIME, self.abort)
                self.lifetime.daemon = True
                self.lifetime.start()

            def abort(self):
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except (OSError, ValueError):
                    pass

            def finish(self):
                self.lifetime.cancel()
                super().finish()

            def log_message(self, *args):
                pass

            def reply(self, status, body):
                data = json.dumps(body).encode("utf-8")
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    self.close_connection = True

            def host_allowed(self):
                return unsafe or API_HOST_RE.fullmatch(self.headers.get("Host") or "") is not None

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                try:
                    if not self.host_allowed():
                        status, body = 403, {"status": "error", "error": "host not allowed"}
                    elif path == "/api/v1/alerts":
                        status, body = 200, monitor.alerts()
                    elif path == "/healthz":
                        status, body = monitor.health()
                    else:
                        status, body = 404, {"status": "error", "error": "not found"}
                except Exception:
                    status, body = 500, {"status": "error", "error": "internal error"}
                self.reply(status, body)

            def method_not_allowed(self):
                self.reply(405, {"status": "error", "error": "method not allowed"})

            do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = method_not_allowed

        class Server(http.server.ThreadingHTTPServer):
            address_family = socket.AF_INET6 if ":" in host else socket.AF_INET
            daemon_threads = True
            slots = threading.BoundedSemaphore(API_MAX_CONNECTIONS)

            def process_request(self, request, client_address):
                if not self.slots.acquire(blocking=False):
                    self.shutdown_request(request)
                    return
                try:
                    super().process_request(request, client_address)
                except BaseException:
                    self.slots.release()
                    raise

            def process_request_thread(self, request, client_address):
                try:
                    super().process_request_thread(request, client_address)
                finally:
                    self.slots.release()

        try:
            self.httpd = Server((host, port), Handler)
        except OSError:
            raise UsageError("cannot bind the alert API to the requested address and port") from None
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self):
        self.thread.start()

    def close(self):
        if self.thread.is_alive():
            self.httpd.shutdown()
        self.httpd.server_close()


def build_probes(args):
    endpoint = Endpoint(args.url, "--url")
    if endpoint.query:
        raise UsageError("invalid --url: expected no query string")
    if endpoint.prefix.endswith("/mcp"):
        # BATON_URL is shared with MCP clients, whose endpoint ends in /mcp.
        endpoint.prefix = endpoint.prefix[:-len("/mcp")]
    token = read_secret(args.token_file, "--token-file")
    context = tls_context(args.ca_file) if endpoint.scheme == "https" else None
    timeout, deadline = args.timeout, args.probe_deadline
    return {
        "board": lambda: probe_board(endpoint, token, timeout, context, deadline),
        "mcp_unauth": lambda: probe_mcp_unauth(endpoint, timeout, context, deadline),
        "mcp_call": lambda: probe_mcp_call(endpoint, token, timeout, context, deadline),
    }


def build_sink(args):
    if args.sink == "none":
        if args.webhook_url or args.webhook_token_file:
            raise UsageError("--webhook-url and --webhook-token-file need --sink webhook")
        return None
    if args.sink == "stdout":
        if args.webhook_url or args.webhook_token_file:
            raise UsageError("--webhook-url and --webhook-token-file only apply to --sink webhook")
        return StdoutSink()
    url = args.webhook_url or os.environ.get("BATON_MONITOR_WEBHOOK_URL")
    if not url:
        raise UsageError("--sink webhook needs --webhook-url (or BATON_MONITOR_WEBHOOK_URL)")
    return WebhookSink(url, args.webhook_token_file, args.timeout, args.ca_file, args.probe_deadline)


def positive(kind, maximum):
    def convert(text):
        try:
            value = kind(text)
        except ValueError:
            raise argparse.ArgumentTypeError("not a number") from None
        if not math.isfinite(value) or value <= 0 or value > maximum:
            raise argparse.ArgumentTypeError("must be a finite number greater than 0 and at most %g" % maximum)
        return value
    return convert


def not_negative(maximum):
    def convert(text):
        try:
            value = float(text)
        except ValueError:
            raise argparse.ArgumentTypeError("not a number") from None
        if not math.isfinite(value) or value < 0 or value > maximum:
            raise argparse.ArgumentTypeError("must be a finite number from 0 to %g" % maximum)
        return value
    return convert


def port_number(text):
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("not a port number") from None
    if not 0 <= value <= 65535:
        raise argparse.ArgumentTypeError("not a port number")
    return value


def default_state_file():
    base = os.environ.get("STATE_DIRECTORY", "").split(":")[0]
    return str(Path(base) / "monitor-state.json") if base else "baton-monitor-state.json"


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=os.environ.get("BATON_URL"), help="Baton base URL (or BATON_URL)")
    parser.add_argument("--token-file", help="file holding the monitoring token (never printed)")
    parser.add_argument("--ca-file", help="CA bundle for https endpoints")
    parser.add_argument("--host-label", default=socket.gethostname(), help="label used in alert text and webhook payloads")
    parser.add_argument("--interval", type=positive(float, 86400), default=60.0, help="seconds between probe cycles (60)")
    parser.add_argument("--timeout", type=positive(float, 3600), default=10.0, help="per-socket-operation timeout seconds (10)")
    parser.add_argument("--probe-deadline", type=positive(float, 3600), default=30.0,
                        help="hard limit in seconds for everything one probe or sink delivery does (30)")
    parser.add_argument("--failures", type=positive(int, MAX_FAILURES), default=3, help="consecutive failures before an alert (3)")
    parser.add_argument("--bind", default=LOOPBACK_DEFAULT, help="address for the alert API; loopback only (127.0.0.1)")
    parser.add_argument("--port", type=port_number, default=DEFAULT_PORT, help="port for the alert API (%d)" % DEFAULT_PORT)
    parser.add_argument("--unsafe-bind", action="store_true",
                        help="allow --bind to be a non-loopback address; the API has no authentication")
    parser.add_argument("--state-file", default=default_state_file(),
                        help="JSON state file ($STATE_DIRECTORY/monitor-state.json or ./baton-monitor-state.json)")
    parser.add_argument("--sink", choices=("none", "webhook", "stdout"), default="none",
                        help="optional push sink for alert/recovery messages (none)")
    parser.add_argument("--webhook-url", help="alert webhook URL (or BATON_MONITOR_WEBHOOK_URL; never logged)")
    parser.add_argument("--webhook-token-file", help="file holding the webhook bearer token (never printed)")
    parser.add_argument("--realert-minutes", type=not_negative(525600), default=60.0,
                        help="sink reminder interval while still down, 0 disables (60)")
    parser.add_argument("--once", action="store_true",
                        help="run one cycle without the API and exit 1 if any probe failed")
    parser.add_argument("--max-cycles", type=positive(int, MAX_FAILURES), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not args.url:
        parser.error("missing Baton URL: pass --url or set BATON_URL")
    if not args.token_file:
        parser.error("missing --token-file")
    if not args.host_label.strip() or len(args.host_label) > 100:
        parser.error("invalid --host-label")
    return args


def run(args, stop=None):
    stop = stop or threading.Event()
    probes = build_probes(args)
    sink = build_sink(args)
    resolve_bind(args.bind, args.unsafe_bind)
    state = State(args.state_file)
    state.load(time.time())
    realert = args.realert_minutes * 60 if args.realert_minutes else float("inf")
    monitor = Monitor(probes, sink, state, host_label=args.host_label, failures=args.failures,
                      realert_seconds=realert, interval=args.interval,
                      hard_deadline=args.probe_deadline + HARD_DEADLINE_GRACE)
    server = None
    if not args.once:
        server = AlertServer(monitor, args.bind, args.port, args.unsafe_bind)
        server.start()
        log.info("alert API listening on port %d", server.port)
    try:
        cycles = 0
        while not stop.is_set():
            started = time.monotonic()
            monitor.cycle()
            cycles += 1
            if args.once:
                return EXIT_FAILING if monitor.failing() else EXIT_OK
            if args.max_cycles and cycles >= args.max_cycles:
                break
            stop.wait(max(0.0, args.interval - (time.monotonic() - started)))
    finally:
        if server is not None:
            server.close()
    return EXIT_OK


def main(argv=None):
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args(argv)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    try:
        return run(args, stop)
    except UsageError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
