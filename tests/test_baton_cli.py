# SPDX-License-Identifier: Apache-2.0
"""DF-17 reviewer warnings, DF-19 lease keepalive and DF-20 error classes for baton_cli."""

import json
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from test_cli import CLI, bridge, clean_env, cli, load_cli_module, pki, plain_token, run_as, run_json, workspace  # noqa: F401

ROOT_HINT = "baton_cli: please upgrade baton_cli to >= "


def warning(principal):
    return "reviewer %s currently cannot approve (no delegation)" % principal


class Once:
    """Loopback HTTP server. Each script step is (status, body, headers)."""

    def __init__(self, steps):
        outer = self
        self.steps = list(steps)

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(length)
                status, body, headers = outer.steps.pop(0) if outer.steps else (500, {"error": {"code": "storage_unavailable"}}, {})
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def test_transport_categories_keep_handshake_certificate_and_network_apart():
    module = load_cli_module()
    assert module.transport_category(ssl.SSLCertVerificationError("certificate verify failed"), True) == "tls: certificate verification failed"
    assert module.transport_category(ssl.SSLError("handshake failure"), True) == "tls: handshake failed"
    assert module.transport_category(ssl.SSLError("unexpected eof"), False) == "connection failed"
    assert module.transport_category(socket.timeout("timed out"), False) == "timeout"
    assert module.transport_category(ConnectionRefusedError(), True) == "connection failed"
    assert module.keepalive_interval(300) == 120
    assert module.keepalive_interval(3600) == 1440
    assert module.keepalive_interval(1) == 0.4
    assert module.CLIENT_VERSION == 2


def test_incompatible_server_prints_the_upgrade_line_and_watch_does_not_retry(cli, plain_token, tmp_path):
    module = load_cli_module()
    body = {"x_protocol": "baton.space.v1", "protocol_version": 9, "min_client_version": 9,
            "error": {"code": "protocol_incompatible", "message": "Client protocol version is no longer accepted"}}
    server = Once([(409, body, {"X-Baton-Min-Client-Version": "9", "X-Baton-Protocol-Version": "9"})])
    try:
        result = cli.run("--url", server.url, "--token-file", plain_token, "tasks")
    finally:
        server.close()
    assert result.rc == 1 and result.out == ""
    assert result.err == ROOT_HINT + "9\n"
    assert not module.transient(module.UpgradeRequired(9))
    hold = Once([(409, body, {"X-Baton-Min-Client-Version": "9"})])
    state = tmp_path / "state.seq"
    process = subprocess.Popen(
        [sys.executable, str(CLI), "--url", hold.url, "--token-file", str(plain_token),
         "watch", "--state", str(state), "--timeout", "1"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=clean_env(cli.home))
    try:
        out, err = process.communicate(timeout=15)
    finally:
        hold.close()
    cli.guard(out, err)
    assert process.returncode == 1 and out == ""
    assert err == ROOT_HINT + "9\n"
    assert "reconnecting" not in err


def test_a_successful_body_with_a_higher_minimum_also_asks_for_an_upgrade(cli, plain_token):
    body = {"x_protocol": "baton.space.v1", "protocol_version": 5, "min_client_version": 5, "result": []}
    server = Once([(200, body, {"X-Baton-Min-Client-Version": "5"})])
    try:
        result = cli.run("--url", server.url, "--token-file", plain_token, "tasks")
    finally:
        server.close()
    assert (result.rc, result.out, result.err) == (1, "", ROOT_HINT + "5\n")


def test_submit_warns_when_a_named_reviewer_cannot_approve_and_still_creates_the_task(cli, bridge, workspace):
    _, page = workspace
    worker = bridge.identities["worker"]["agent"]
    owner = bridge.identities["owner"]["agent"]
    reviewer = bridge.identities["reviewer"]["agent"]
    warned = run_as(cli, bridge, "owner", "submit", "--key", "warn-" + uuid.uuid4().hex, "--prompt", "Needs a reviewer",
                    "--page", page["id"], "--assignee", worker, "--reviewer", worker, "--reviewer", owner, "--scope", "none")
    assert warned.rc == 0, warned.err
    assert warned.json()["warnings"] == [warning(worker)]
    assert warning(worker) in warned.err and warning(owner) not in warned.err
    assert run_json(cli, bridge, "owner", "task", warned.json()["task_id"])["state"] == "queued"
    quiet = run_as(cli, bridge, "owner", "submit", "--key", "root-" + uuid.uuid4().hex, "--prompt", "Root reviews",
                   "--page", page["id"], "--reviewer", owner, "--scope", "none")
    assert quiet.rc == 0 and quiet.err == "" and "warnings" not in quiet.json()
    run_json(cli, bridge, "owner", "call", "delegation_create", "--operation-id", str(uuid.uuid4()),
             "--json", json.dumps({"delegate_principal": reviewer, "reason": "DECISIONS 291"}))
    covered = run_as(cli, bridge, "owner", "submit", "--key", "covered-" + uuid.uuid4().hex, "--prompt", "Delegated",
                     "--page", page["id"], "--reviewer", reviewer, "--scope", "none")
    assert covered.rc == 0 and covered.err == "" and "warnings" not in covered.json()


def test_stale_claim_prints_reclaim_or_the_new_holder(cli, bridge, workspace):
    _, page = workspace
    worker = bridge.identities["worker"]["agent"]
    reviewer = bridge.identities["reviewer"]["agent"]
    task = run_json(cli, bridge, "owner", "submit", "--key", "stale-" + uuid.uuid4().hex, "--prompt", "Finish me",
                    "--page", page["id"], "--assignee", worker, "--scope", "none")
    claim = run_json(cli, bridge, "worker", "claim", "--task", task["task_id"], "--no-summary")
    run_json(cli, bridge, "worker", "result", "--task", task["task_id"], "--epoch", str(claim["epoch"]),
             "--result", "done", "--no-summary")
    stale = run_as(cli, bridge, "worker", "renew", "--task", task["task_id"], "--epoch", str(claim["epoch"]), "--no-summary")
    assert stale.rc == 1 and stale.out == ""
    assert "baton_cli: error: stale_claim (HTTP 409)" in stale.err
    assert "baton_cli: recovery: baton_cli.py claim --task " + task["task_id"] in stale.err
    assert bridge.https not in stale.err

    fresh = run_json(cli, bridge, "owner", "submit", "--key", "hand-" + uuid.uuid4().hex, "--prompt", "Hand off",
                     "--page", page["id"], "--assignee", worker, "--scope", "path=docs/handoff-" + uuid.uuid4().hex)
    held = run_json(cli, bridge, "worker", "claim", "--task", fresh["task_id"], "--no-summary")
    run_json(cli, bridge, "worker", "call", "handoff", "--operation-id", str(uuid.uuid4()), "--json",
             json.dumps({"task_id": fresh["task_id"], "epoch": held["epoch"], "target_principal": reviewer}))
    lost = run_as(cli, bridge, "worker", "renew", "--task", fresh["task_id"], "--epoch", str(held["epoch"]), "--no-summary")
    assert lost.rc == 1
    assert "baton_cli: recovery: ask the new holder " + reviewer in lost.err


def test_keepalive_renews_until_the_process_exits(cli, bridge, workspace):
    _, page = workspace
    task = run_json(cli, bridge, "owner", "submit", "--key", "keep-" + uuid.uuid4().hex, "--prompt", "Hold the lease",
                    "--page", page["id"], "--assignee", bridge.identities["worker"]["agent"], "--scope", "none")
    before = len(bridge.requests)
    process = subprocess.Popen(
        [sys.executable, str(CLI), "--url", bridge.https, "--ca-file", str(bridge.pki["ca"]),
         "--token-file", str(bridge.tokens["worker"]), "claim", "--task", task["task_id"],
         "--lease", "8", "--keepalive", "--no-summary"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=clean_env(cli.home))
    try:
        deadline = time.monotonic() + 12
        renews = []
        while time.monotonic() < deadline:
            renews = []
            for record in bridge.requests[before:]:
                if not record["path"].endswith("/renew"):
                    continue
                try:
                    parsed = bridge.recorder.body(record)
                except (ValueError, TypeError):
                    parsed = None
                if isinstance(parsed, dict) and parsed.get("lease_seconds") == 8:
                    renews.append(parsed)
            if renews:
                break
            assert process.poll() is None, "keepalive exited before renewing"
            time.sleep(0.1)
        else:
            pytest.fail("keepalive did not renew")
        claim_body = bridge.recorder.body(bridge.requests[before])
        assert renews[0]["task_id"] == task["task_id"] and renews[0]["operation_id"] != claim_body["operation_id"]
        process.send_signal(signal.SIGTERM)
        out, err = process.communicate(timeout=15)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=15)
    cli.guard(out, err)
    assert process.returncode == 0
    assert json.loads(out)["state"] == "claimed"
    assert err == ""


def test_client_sends_its_version_and_the_server_keeps_a_one_version_window(cli, bridge):
    import http.client
    listed = run_json(cli, bridge, "owner", "tasks")
    assert listed == []
    record = bridge.requests[-1]
    assert record["headers"]["x-baton-client-version"] == "2"
    assert record["headers"]["user-agent"] == "baton-cli/2"
    context = ssl.create_default_context(cafile=str(bridge.pki["ca"]))
    token = bridge.tokens["owner"].read_text().strip()

    def post(headers):
        conn = http.client.HTTPSConnection("127.0.0.1", bridge.port, timeout=10, context=context)
        try:
            conn.request("POST", "/v1/space/list_tasks", body=b"{}", headers=headers)
            response = conn.getresponse()
            return response.status, json.loads(response.read()), response.getheader("X-Baton-Protocol-Version"), response.getheader("X-Baton-Min-Client-Version")
        finally:
            conn.close()

    status, body, protocol, minimum = post({
        "Content-Type": "application/json", "Authorization": "Bearer " + token, "X-Baton-Client-Version": "2"})
    assert status == 200 and body["result"] == [] and body["protocol_version"] == 2 and body["min_client_version"] == 1
    assert body["x_protocol"] == "baton.space.v1" and (protocol, minimum) == ("2", "1")
    status, body, protocol, minimum = post({"Content-Type": "application/json", "X-Baton-Client-Version": "0"})
    assert status == 409 and body["error"]["code"] == "protocol_incompatible"
    assert body["min_client_version"] == 1 and body["protocol_version"] == 2
    assert "0" not in json.dumps(body) and (protocol, minimum) == ("2", "1")
    for version in (None, "1", "99"):
        headers = {"Content-Type": "application/json", "Authorization": "Bearer not-a-real-token"}
        if version is not None:
            headers["X-Baton-Client-Version"] = version
        status, _, protocol, minimum = post(headers)
        assert status == 401 and (protocol, minimum) == ("2", "1")
