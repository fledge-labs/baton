# SPDX-License-Identifier: Apache-2.0
"""Notification outbox, wait_inbox and webhook delivery on actual PostgreSQL.

Every identity, prompt, comment and result is synthetic. The webhook receiver is a local
HTTPS server with a throw-away certificate; no external network is used.
"""

import asyncio
import datetime
import hashlib
import hmac
import http.server
import ipaddress
import json
import ssl
import threading
import time
import uuid

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from bridge import webhooks
from bridge.space_store import SERVICE_ACTOR, StoreError, WEBHOOK_BACKOFF_SECONDS
from test_bridge import api, op, shared_page  # noqa: F401  (api is a fixture)

def ip4(*octets):
    return ".".join(str(octet) for octet in octets)


PAYLOAD_KEYS = {"id", "seq", "event", "object_type", "object_id", "thread_id", "created_at", "recipient"}
PROMPT, COMMENT, RESULT = "synthetic-prompt-body", "synthetic-comment-body", "synthetic-result-body"
PRIVATE_TEXT = (PROMPT, COMMENT, RESULT)


def query(store, sql, params=()):
    with store._transaction() as conn:
        return conn.execute(sql, params).fetchall()


def execute(store, sql, params=()):
    with store._transaction(write=True) as conn:
        conn.execute(sql, params)


def notes(call, label, after=0, timeout=0, expected=200, **extra):
    return call(label, "wait_inbox", expected, after_seq=after, timeout_seconds=timeout, **extra)


def events(result):
    return [item["event"] for item in result["notifications"]]


def make_task(call, identities, page, assignee="worker", reviewers=("reviewer",)):
    return call("owner", "create_task_block", operation_id=op(), page_id=page["id"], prompt=PROMPT,
                scopes=[{"kind": "path", "value": "src/notify"}], reviewers=[identities[name]["agent"] for name in reviewers],
                assignee_principal=identities[assignee]["agent"] if assignee else None)


@pytest.fixture
def team(api):
    client, call, identities, tokens, registry, records = api
    space, page = shared_page(call, identities)
    return call, identities, space, page


def test_each_trigger_writes_metadata_only_rows_for_the_right_recipients(team, api, pg_store):
    call, identities, space, page = team
    task = make_task(call, identities, page)
    assert events(notes(call, "worker")) == ["task.assigned"]
    assignment = notes(call, "worker")["notifications"][0]
    assert set(assignment) == PAYLOAD_KEYS
    assert assignment["object_type"] == "task" and assignment["object_id"] == task["task_id"]
    assert assignment["thread_id"] == task["task_id"] and assignment["recipient"] == identities["worker"]["agent"]
    for label in ("owner", "reviewer", "replacement", "stranger"):
        assert notes(call, label)["notifications"] == []

    claim = call("worker", "claim", operation_id=op(), task_id=task["task_id"])
    comment = call("replacement", "comment", operation_id=op(), page_id=page["id"], body=COMMENT, thread_id=task["task_id"])
    assert events(notes(call, "owner")) == ["comment.created"]
    assert events(notes(call, "reviewer")) == ["comment.created"]
    assert events(notes(call, "worker", after=assignment["seq"])) == ["comment.created"]
    assert notes(call, "replacement")["notifications"] == []
    seen = notes(call, "owner")["notifications"][0]
    assert seen["object_type"] == "comment" and seen["object_id"] == comment["id"] and seen["thread_id"] == task["task_id"]
    call("owner", "comment", operation_id=op(), page_id=page["id"], body=COMMENT, thread_id=task["task_id"])
    assert len(notes(call, "owner")["notifications"]) == 1, "the comment author is not notified"
    assert events(notes(call, "reviewer")) == ["comment.created", "comment.created"]

    call("worker", "submit_result", operation_id=op(), task_id=task["task_id"], epoch=claim["epoch"], result=RESULT)
    assert events(notes(call, "reviewer"))[-1] == "task.awaiting_gate"
    assert "task.awaiting_gate" not in events(notes(call, "worker")) + events(notes(call, "owner"))

    call("reviewer", "gate", operation_id=op(), task_id=task["task_id"], expected_epoch=claim["epoch"], decision="pass")
    assert events(notes(call, "owner"))[-1] == "gate.decided"
    assert events(notes(call, "worker"))[-1] == "gate.decided"
    assert "gate.decided" not in events(notes(call, "reviewer"))

    private = json.dumps([notes(call, label) for label in ("owner", "worker", "reviewer", "replacement")])
    assert not any(text in private for text in PRIVATE_TEXT)
    audit = json.dumps(call("owner", "list_audit", task_id=task["task_id"]))
    assert not any(text in audit for text in PRIVATE_TEXT)


def test_mentions_assignment_paths_and_handoff_notify_the_target(team):
    call, identities, space, page = team
    comment = call("owner", "comment", operation_id=op(), page_id=page["id"], body=COMMENT)
    call("owner", "mention", operation_id=op(), comment_id=comment["id"], target_type="agent", target_id=identities["worker"]["agent"])
    call("owner", "mention", operation_id=op(), comment_id=comment["id"], target_type="person", target_id=identities["reviewer"]["agent"])
    call("owner", "mention", operation_id=op(), comment_id=comment["id"], target_type="agent", target_id=identities["stranger"]["agent"])
    mention = notes(call, "worker")["notifications"][0]
    assert mention["event"] == "mention.created" and mention["object_type"] == "comment"
    assert mention["object_id"] == comment["id"] and mention["thread_id"] == comment["thread_id"]
    assert events(notes(call, "reviewer")) == ["mention.created"]
    assert notes(call, "stranger")["notifications"] == [], "no view permission at insert time means no row"
    call("owner", "mention", operation_id=op(), comment_id=comment["id"], target_type="agent", target_id=identities["worker"]["agent"])
    assert len(notes(call, "worker")["notifications"]) == 1

    submitted = call("owner", "submit_task", operation_id=op(), idempotency_key="notify-submit", prompt=PROMPT,
                     page_id=page["id"], assignee_principal=identities["replacement"]["agent"])
    assert events(notes(call, "replacement")) == ["task.assigned"]
    queued = call("owner", "create_task_block", operation_id=op(), page_id=page["id"], prompt=PROMPT,
                  scopes=[{"kind": "path", "value": "src/other"}], reviewers=[identities["reviewer"]["agent"]])
    assert notes(call, "worker", after=mention["seq"])["notifications"] == []
    call("owner", "assign_task", operation_id=op(), task_id=queued["task_id"], target_principal=identities["worker"]["agent"])
    assert events(notes(call, "worker", after=mention["seq"])) == ["task.assigned"]

    claim = call("worker", "claim", operation_id=op(), task_id=queued["task_id"])
    call("worker", "handoff", operation_id=op(), task_id=queued["task_id"], epoch=claim["epoch"],
         target_principal=identities["replacement"]["agent"])
    handed = notes(call, "replacement", after=notes(call, "replacement")["notifications"][0]["seq"])
    assert events(handed) == ["task.handed_off"] and handed["notifications"][0]["object_id"] == queued["task_id"]
    assert submitted["assignee"] == identities["replacement"]["agent"]


def test_outbox_rows_are_atomic_with_the_business_effect_and_replay_safe(team, api, pg_store, modules):
    call, identities, space, page = team
    server = modules[1]
    owner, worker = (server.actor_from_identity(identities[name]) for name in ("owner", "worker"))
    counts = lambda: (query(pg_store, "SELECT count(*) AS n FROM baton.x_notify")[0]["n"],
                      query(pg_store, "SELECT count(*) AS n FROM baton.tasks")[0]["n"],
                      query(pg_store, "SELECT count(*) AS n FROM baton.x_webhook_delivery")[0]["n"])
    before = counts()
    original = pg_store._task_view
    pg_store._task_view = lambda *args, **kwargs: (_ for _ in ()).throw(StoreError("storage_unavailable"))
    try:
        with pytest.raises(StoreError):
            pg_store.create_task_block(owner, op(), page["id"], PROMPT, [{"kind": "path", "value": "x"}],
                                       [identities["reviewer"]["agent"]], assignee=worker)
    finally:
        pg_store._task_view = original
    assert counts() == before
    assert notes(call, "worker")["notifications"] == []

    operation_id = op()
    body = dict(operation_id=operation_id, page_id=page["id"], prompt=PROMPT, scopes=[{"kind": "path", "value": "x"}],
                reviewers=[identities["reviewer"]["agent"]], assignee_principal=identities["worker"]["agent"])
    first = call("owner", "create_task_block", **body)
    assert counts()[0] == before[0] + 1
    assert call("owner", "create_task_block", **body)["task_id"] == first["task_id"]
    assert counts()[0] == before[0] + 1, "an idempotent replay never duplicates outbox rows"
    assert call("owner", "create_task_block", 409, **{**body, "prompt": "different"})["code"] == "operation_conflict"
    assert counts()[0] == before[0] + 1
    stale = call("reviewer", "gate", 409, operation_id=op(), task_id=first["task_id"], expected_epoch=1, decision="pass")
    assert stale["code"] == "stale_claim"
    assert counts()[0] == before[0] + 1


def test_permissions_are_rechecked_when_notifications_are_read(team):
    call, identities, space, page = team
    make_task(call, identities, page)
    first = notes(call, "worker")
    assert events(first) == ["task.assigned"]
    revoke = dict(resource_type="space", resource_id=space["id"], subject_type="person", subject_id=identities["worker"]["account"])
    call("owner", "share", operation_id=op(), role=None, **revoke)
    hidden = notes(call, "worker")
    assert hidden["notifications"] == [] and hidden["next_seq"] == first["next_seq"]
    call("owner", "share", operation_id=op(), role="view", **revoke)
    assert events(notes(call, "worker")) == ["task.assigned"]
    call("owner", "share", operation_id=op(), role="edit", **revoke)


def test_wait_inbox_validation_after_seq_and_clean_timeout(team):
    call, identities, space, page = team
    started = time.monotonic()
    empty = notes(call, "worker", timeout=1)
    assert empty == {"notifications": [], "next_seq": 0} and 0.8 <= time.monotonic() - started < 5
    make_task(call, identities, page)
    started = time.monotonic()
    ready = notes(call, "worker", timeout=30)
    assert time.monotonic() - started < 3 and len(ready["notifications"]) == 1
    assert ready["next_seq"] == ready["notifications"][0]["seq"]
    started = time.monotonic()
    after = notes(call, "worker", after=ready["next_seq"], timeout=1)
    assert after == {"notifications": [], "next_seq": ready["next_seq"]} and time.monotonic() - started >= 0.8
    for second in range(3):
        make_task(call, identities, page)
    paged = notes(call, "worker", after=ready["next_seq"], limit=2)
    assert len(paged["notifications"]) == 2 and paged["next_seq"] == paged["notifications"][-1]["seq"]
    rest = notes(call, "worker", after=paged["next_seq"])
    assert len(rest["notifications"]) == 1 and rest["notifications"][0]["seq"] > paged["next_seq"]
    for bad in ({"timeout_seconds": 56}, {"timeout_seconds": -1}, {"limit": 0}, {"limit": 101}, {"after_seq": -1}):
        body = {"after_seq": 0, "timeout_seconds": 0, "limit": 1, **bad}
        assert call("worker", "wait_inbox", 422, **body)["code"] == "invalid"
    assert call("worker", "wait_inbox", 422, after_seq=0, timeout_seconds=True)["code"] == "invalid"
    assert call("worker", "wait_inbox", 422, after_seq=0, actor="x")["code"] == "invalid"


def test_waiting_call_wakes_on_commit_and_concurrency_is_bounded(team, api, modules, monkeypatch):
    call, identities, space, page = team
    result = {}
    started = time.monotonic()
    thread = threading.Thread(target=lambda: result.update(notes(call, "worker", timeout=30)))
    thread.start()
    time.sleep(0.5)
    make_task(call, identities, page)
    thread.join(10)
    assert not thread.is_alive() and events(result) == ["task.assigned"] and time.monotonic() - started < 8

    monkeypatch.setattr(modules[1], "WAIT_SLOTS_PER_PRINCIPAL", 1)
    holder = threading.Thread(target=lambda: notes(call, "stranger", timeout=3))
    holder.start()
    time.sleep(0.7)
    assert notes(call, "stranger", timeout=3, expected=429)["code"] == "rate_limited"
    assert notes(call, "reviewer", timeout=0)["notifications"] == []
    holder.join(10)
    assert notes(call, "stranger", timeout=0)["notifications"] == []


def test_wait_inbox_is_a_schema_operation_and_mcp_tool(team, api):
    client, call, identities, tokens, *_ = api
    schema = client.get("/v1/space/schema", headers={"Authorization": "Bearer " + tokens["owner"]}).json()["operations"]
    assert schema["wait_inbox"]["mutation"] is False
    assert schema["webhook_subscribe"]["mutation"] and schema["webhook_delete"]["mutation"] and not schema["webhook_list"]["mutation"]
    headers = {"Authorization": "Bearer " + tokens["worker"], "Host": "bridge", "Accept": "application/json, text/event-stream"}
    listing = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()["result"]["tools"]
    assert {"wait_inbox", "webhook_subscribe", "webhook_list", "webhook_delete"} <= {tool["name"] for tool in listing}
    make_task(call, team[1], team[3])
    response = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
        "name": "wait_inbox", "arguments": {"after_seq": 0, "timeout_seconds": 0}}}).json()["result"]
    assert not response.get("isError", False)
    payload = response.get("structuredContent") or json.loads(response["content"][0]["text"])
    assert payload["x_protocol"] == "baton.space.v1" and events(payload["result"]) == ["task.assigned"]


# --- webhooks ---------------------------------------------------------------------------------


class Receiver:
    """Local HTTPS endpoint with a self-signed certificate that records every request."""

    def __init__(self, directory):
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
        now = datetime.datetime.now(datetime.timezone.utc)
        certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                       .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
                       .not_valid_after(now + datetime.timedelta(days=1))
                       .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), False)
                       .add_extension(x509.BasicConstraints(ca=True, path_length=None), True).sign(key, hashes.SHA256()))
        self.cert = directory / "receiver.pem"
        self.cert.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        keyfile = directory / "receiver.key"
        keyfile.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        self.requests, self.statuses, self.delay = [], [], 0
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                outer.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                time.sleep(outer.delay)
                status = outer.statuses.pop(0) if outer.statuses else 200
                self.send_response(status)
                if status in {301, 302}:
                    self.send_header("Location", "https://localhost:1/never-followed")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.cert, keyfile)
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.server.handle_error = lambda *args: None
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]

    def url(self, path="/hook"):
        return f"https://localhost:{self.port}{path}"


@pytest.fixture
def receiver(tmp_path, monkeypatch):
    server = Receiver(tmp_path)
    monkeypatch.setenv(webhooks.ALLOW_HOSTS_ENV, "localhost")
    monkeypatch.setenv(webhooks.CA_BUNDLE_ENV, str(server.cert))
    yield server
    server.server.shutdown()
    server.server.server_close()


def deliver(modules):
    return asyncio.run(modules[1].webhook_deliver_once())


def due_now(store):
    execute(store, "UPDATE baton.x_webhook_delivery SET next_attempt_at=clock_timestamp() WHERE state='pending'")


def subscribe(call, label, url, events=None):
    result = call(label, "webhook_subscribe", operation_id=op(), url=url, events=events)
    assert set(result) == {"subscription_id", "signing_secret", "replay"} and len(result["signing_secret"]) == 64
    return result["subscription_id"], bytes.fromhex(result["signing_secret"])


def public_dns(monkeypatch, mapping=None):
    mapping = mapping or {}
    def fake(host, port, **kwargs):
        try:
            addresses = [str(ipaddress.ip_address(host))]
        except ValueError:
            addresses = mapping.get(host, [ip4(93, 184, 216, 34)])
        return [(10 if ":" in address else 2, 1, 6, "", (address, port)) for address in addresses]
    monkeypatch.setattr(webhooks, "_getaddrinfo", fake)


def test_signed_delivery_carries_metadata_only_and_only_the_callers_notifications(team, receiver, modules, pg_store):
    call, identities, space, page = team
    worker_id, worker_secret = subscribe(call, "worker", receiver.url("/worker"), ["task.assigned", "gate.decided"])
    reviewer_id, reviewer_secret = subscribe(call, "reviewer", receiver.url("/reviewer"))
    task = make_task(call, identities, page)
    claim = call("worker", "claim", operation_id=op(), task_id=task["task_id"])
    call("worker", "submit_result", operation_id=op(), task_id=task["task_id"], epoch=claim["epoch"], result=RESULT)
    call("reviewer", "gate", operation_id=op(), task_id=task["task_id"], expected_epoch=claim["epoch"], decision="pass")
    call("stranger", "comment", 403, operation_id=op(), page_id=page["id"], body=COMMENT, thread_id=task["task_id"])
    assert deliver(modules) == 3

    by_path = {}
    for request in receiver.requests:
        by_path.setdefault(request["path"], []).append(request)
    assert sorted(request["headers"]["X-Baton-Event"] for request in by_path["/worker"]) == ["gate.decided", "task.assigned"]
    assert [request["headers"]["X-Baton-Event"] for request in by_path["/reviewer"]] == ["task.awaiting_gate"]
    for path, secret in (("/worker", worker_secret), ("/reviewer", reviewer_secret)):
        for request in by_path[path]:
            payload = json.loads(request["body"])
            assert set(payload) == PAYLOAD_KEYS and payload["object_id"] == task["task_id"]
            assert request["headers"]["X-Baton-Delivery"] == payload["id"]
            expected = "sha256=" + hmac.new(secret, request["body"], hashlib.sha256).hexdigest()
            assert hmac.compare_digest(request["headers"]["X-Baton-Signature"], expected)
            assert request["headers"]["Content-Type"] == "application/json"
            assert payload["recipient"] == identities["worker" if path == "/worker" else "reviewer"]["agent"]
            assert not any(text.encode() in request["body"] for text in PRIVATE_TEXT)
    wrong = "sha256=" + hmac.new(worker_secret, by_path["/reviewer"][0]["body"], hashlib.sha256).hexdigest()
    assert wrong != by_path["/reviewer"][0]["headers"]["X-Baton-Signature"]

    audit = query(pg_store, "SELECT event,metadata,requested_by,agent FROM baton.x_audit WHERE event='webhook.delivered'")
    assert len(audit) == 3 and {row["agent"] for row in audit} == {SERVICE_ACTOR.principal}
    assert all(row["metadata"]["attempts"] == 1 and row["metadata"]["http_status"] == 200 for row in audit)
    assert deliver(modules) == 0
    assert sorted(item["delivered"] for item in call("worker", "webhook_list") + call("reviewer", "webhook_list")) == [1, 2]
    assert worker_id != reviewer_id


def test_retries_follow_backoff_then_dead_letter_with_one_final_audit(team, receiver, modules, pg_store):
    call, identities, space, page = team
    subscription, secret = subscribe(call, "worker", receiver.url() + "?token=query-secret-value")
    task = make_task(call, identities, page)
    receiver.statuses = [500] * 20
    for attempt in range(1, 9):
        due_now(pg_store)
        assert deliver(modules) == 1
        row = query(pg_store, """SELECT state,attempts,extract(epoch FROM next_attempt_at-clock_timestamp()) AS wait
                                 FROM baton.x_webhook_delivery""")[0]
        assert row["attempts"] == attempt
        if attempt < 8:
            expected = WEBHOOK_BACKOFF_SECONDS[attempt - 1]
            assert row["state"] == "pending" and expected - 5 <= row["wait"] <= expected + 1
        else:
            assert row["state"] == "dead"
    assert len(receiver.requests) == 8
    assert {request["headers"]["X-Baton-Delivery"] for request in receiver.requests} == {json.loads(receiver.requests[0]["body"])["id"]}
    assert all(request["path"].endswith("?token=query-secret-value") for request in receiver.requests)
    assert sum(WEBHOOK_BACKOFF_SECONDS) <= 3900
    due_now(pg_store)
    assert deliver(modules) == 0
    attempts = query(pg_store, "SELECT attempt,outcome,http_status FROM baton.x_webhook_attempt ORDER BY attempt")
    assert [row["attempt"] for row in attempts] == list(range(1, 9)) and {row["outcome"] for row in attempts} == {"http_error"}
    final = query(pg_store, "SELECT event,metadata,task_id FROM baton.x_audit WHERE event LIKE 'webhook.%%' AND event<>'webhook.subscribed'")
    assert len(final) == 1 and final[0]["event"] == "webhook.dead_lettered" and str(final[0]["task_id"]) == task["task_id"]
    assert final[0]["metadata"]["attempts"] == 8 and final[0]["metadata"]["error_code"] == "http_error"
    assert call("worker", "webhook_list")[0]["dead"] == 1 and subscription


def test_transient_failure_recovers_and_redirects_are_not_followed(team, receiver, modules, pg_store):
    call, identities, space, page = team
    subscribe(call, "worker", receiver.url())
    make_task(call, identities, page)
    receiver.statuses = [302, 503, 204]
    for _ in range(3):
        due_now(pg_store)
        assert deliver(modules) == 1
    assert [request["path"] for request in receiver.requests] == ["/hook"] * 3
    rows = query(pg_store, "SELECT outcome,http_status FROM baton.x_webhook_attempt ORDER BY attempt")
    assert [(row["outcome"], row["http_status"]) for row in rows] == [("http_error", 302), ("http_error", 503), ("delivered", 204)]
    assert query(pg_store, "SELECT state,attempts FROM baton.x_webhook_delivery")[0] == {"state": "delivered", "attempts": 3}
    assert len(query(pg_store, "SELECT 1 FROM baton.x_audit WHERE event='webhook.delivered'")) == 1


def test_delivery_rechecks_view_permission_and_deleted_subscriptions_stop(team, receiver, modules, pg_store):
    call, identities, space, page = team
    subscribe(call, "worker", receiver.url())
    keep, _ = subscribe(call, "reviewer", receiver.url("/other"))
    task = make_task(call, identities, page)
    call("owner", "share", operation_id=op(), resource_type="space", resource_id=space["id"], subject_type="person",
         subject_id=identities["worker"]["account"], role=None)
    assert deliver(modules) == 0 and receiver.requests == []
    rows = query(pg_store, "SELECT d.state,a.outcome FROM baton.x_webhook_delivery d JOIN baton.x_webhook_attempt a USING(subscription_id,notification_id) JOIN baton.x_webhook w ON w.id=d.subscription_id WHERE w.principal=%s",
                 (identities["worker"]["agent"],))
    assert rows == [{"state": "dead", "outcome": "forbidden"}]
    audit = query(pg_store, "SELECT metadata FROM baton.x_audit WHERE event='webhook.dead_lettered'")
    assert audit[0]["metadata"]["error_code"] == "forbidden"
    call("owner", "comment", operation_id=op(), page_id=page["id"], body=COMMENT, thread_id=task["task_id"])
    assert query(pg_store, "SELECT count(*) AS n FROM baton.x_webhook_delivery WHERE state='pending'")[0]["n"] == 1
    listed = call("reviewer", "webhook_list")
    assert call("worker", "webhook_delete", 404, operation_id=op(), subscription_id=listed[0]["subscription_id"])["code"] == "not_found"
    delete = op()
    assert call("reviewer", "webhook_delete", operation_id=delete, subscription_id=keep)["deleted"] is True
    assert call("reviewer", "webhook_delete", operation_id=delete, subscription_id=keep)["replay"] is True
    assert call("reviewer", "webhook_list") == []
    assert query(pg_store, "SELECT count(*) AS n FROM baton.x_webhook_delivery w JOIN baton.x_webhook x ON x.id=w.subscription_id WHERE x.principal=%s",
                 (identities["reviewer"]["agent"],))[0]["n"] == 0


def test_dns_rebinding_to_a_forbidden_address_is_refused_at_connect_time(team, receiver, modules, pg_store, monkeypatch):
    call, identities, space, page = team
    monkeypatch.setenv(webhooks.ALLOW_HOSTS_ENV, "")
    public_dns(monkeypatch)
    subscribe(call, "worker", f"https://rebind.example.test:{receiver.port}/hook")
    make_task(call, identities, page)
    public_dns(monkeypatch, {"rebind.example.test": ["127.0.0.1"]})
    assert deliver(modules) == 1
    assert receiver.requests == []
    assert query(pg_store, "SELECT outcome FROM baton.x_webhook_attempt")[0]["outcome"] == "refused"
    assert query(pg_store, "SELECT state,attempts FROM baton.x_webhook_delivery")[0] == {"state": "pending", "attempts": 1}


@pytest.mark.parametrize("url", [
    "http://example.test/hook", "https://127.0.0.1/hook", "https://" + ip4(10, 1, 2, 3) + "/hook", "https://" + ip4(172, 16, 0, 9) + "/hook",
    "https://" + ip4(192, 168, 1, 1) + "/hook", "https://" + ip4(169, 254, 169, 254) + "/latest/meta-data", "https://[::1]/hook", "https://[fd00::1]/hook",
    "https://[::ffff:127.0.0.1]/hook", "https://" + ip4(224, 0, 0, 1) + "/hook", "https://localhost/hook", "https://user:pw@example.com/hook",
    "https://private.example.test/hook", "https://mixed.example.test/hook", "ftp://example.test/hook", "https:///hook",
    "https://example.test:0/hook", "https://example.test/ho ok", "", "https://" + "a" * 3000 + ".test/",
])
def test_ssrf_guard_refuses_unsafe_destinations(team, monkeypatch, url):
    call = team[0]
    monkeypatch.delenv(webhooks.ALLOW_HOSTS_ENV, raising=False)
    public_dns(monkeypatch, {"localhost": ["127.0.0.1", "::1"], "private.example.test": [ip4(10, 0, 0, 5)], "mixed.example.test": [ip4(93, 184, 216, 34), ip4(10, 0, 0, 5)]})
    assert call("worker", "webhook_subscribe", 422, operation_id=op(), url=url)["code"] == "invalid"
    assert call("worker", "webhook_list") == []


def test_allowlist_overrides_address_checks_but_never_the_https_requirement(team, monkeypatch):
    call = team[0]
    public_dns(monkeypatch)
    monkeypatch.setenv(webhooks.ALLOW_HOSTS_ENV, "127.0.0.1, Hooks.Internal.Test")
    assert subscribe(call, "worker", "https://127.0.0.1:8443/hook")
    public_dns(monkeypatch, {"hooks.internal.test": [ip4(10, 9, 8, 7)]})
    assert subscribe(call, "worker", "https://hooks.internal.test/hook")
    assert call("worker", "webhook_subscribe", 422, operation_id=op(), url="http://127.0.0.1/hook")["code"] == "invalid"
    assert call("worker", "webhook_subscribe", 422, operation_id=op(), url="https://" + ip4(10, 0, 0, 1) + "/hook")["code"] == "invalid"


def test_signing_secret_is_returned_once_and_never_listed_stored_in_results_or_audited(team, monkeypatch, pg_store, capsys):
    call, identities, space, page = team
    public_dns(monkeypatch)
    operation_id = op()
    body = dict(operation_id=operation_id, url="https://hooks.example.test/hook?token=query-secret-value", events=["task.assigned"])
    first = call("worker", "webhook_subscribe", **body)
    secret = first["signing_secret"]
    assert len(bytes.fromhex(secret)) == 32 and first["replay"] is False
    replay = call("worker", "webhook_subscribe", **body)
    assert replay == {"subscription_id": first["subscription_id"], "replay": True}
    assert call("worker", "webhook_subscribe", 409, **{**body, "events": None})["code"] == "operation_conflict"
    second = call("worker", "webhook_subscribe", operation_id=op(), url=body["url"])
    assert second["signing_secret"] != secret
    listing = call("worker", "webhook_list")
    assert len(listing) == 2 and secret not in json.dumps(listing) and "signing_secret" not in json.dumps(listing)
    assert all(set(item) == {"subscription_id", "url", "events", "created_at", "pending", "delivered", "dead"} for item in listing)
    assert call("reviewer", "webhook_list") == []
    make_task(call, identities, page)
    persisted = json.dumps([
        query(pg_store, "SELECT result::text AS t FROM baton.operations"),
        query(pg_store, "SELECT metadata::text AS t, event FROM baton.x_audit"),
        query(pg_store, "SELECT row_to_json(a)::text AS t FROM baton.x_webhook_attempt a"),
        query(pg_store, "SELECT row_to_json(d)::text AS t FROM baton.x_webhook_delivery d"),
        call("worker", "list_audit", space_id=space["id"]),
    ])
    assert secret not in persisted and second["signing_secret"] not in persisted and "query-secret-value" not in persisted
    stored = query(pg_store, "SELECT secret FROM baton.x_webhook WHERE id=%s", (first["subscription_id"],))[0]["secret"]
    assert bytes(stored) == bytes.fromhex(secret)
    output = capsys.readouterr()
    assert secret not in output.out + output.err


def test_subscription_limit_is_per_principal(team, monkeypatch):
    call = team[0]
    public_dns(monkeypatch)
    ids = [subscribe(call, "worker", f"https://hooks.example.test/{index}")[0] for index in range(10)]
    assert call("worker", "webhook_subscribe", 429, operation_id=op(), url="https://hooks.example.test/11")["code"] == "rate_limited"
    assert subscribe(call, "reviewer", "https://hooks.example.test/other")
    call("worker", "webhook_delete", operation_id=op(), subscription_id=ids[0])
    assert subscribe(call, "worker", "https://hooks.example.test/again")
    assert call("worker", "webhook_delete", 422, operation_id=op(), subscription_id="not-a-uuid")["code"] == "invalid"
    assert call("worker", "webhook_subscribe", 422, operation_id=op(), url="https://hooks.example.test/x", events=["nope"])["code"] == "invalid"
    assert call("worker", "webhook_subscribe", 422, operation_id=op(), url="https://hooks.example.test/x", events=[])["code"] == "invalid"


def test_delivery_internals_are_service_only_and_timeouts_are_enforced(team, receiver, modules, pg_store):
    call, identities, space, page = team
    user = modules[1].actor_from_identity(identities["worker"])
    for method, args in (("webhook_claim_due", ()), ("webhook_record", (str(uuid.uuid4()), str(uuid.uuid4()), 0, "delivered"))):
        with pytest.raises(StoreError) as caught:
            getattr(pg_store, method)(user, *args)
        assert caught.value.code == "forbidden"
    assert not {"webhook_claim_due", "webhook_record", "poll_notifications"} & set(modules[1].PUBLIC_OPERATIONS)
    receiver.delay = 3
    started = time.monotonic()
    assert webhooks.post(receiver.url(), b"{}", {"Content-Type": "application/json"}, timeout=1) == ("network_error", None)
    assert time.monotonic() - started < 2.5
    receiver.delay = 0
    assert webhooks.post(receiver.url(), b"{}", {"Content-Type": "application/json"}, timeout=5) == ("delivered", 200)
    assert webhooks.sign(b"k", b"body") == "sha256=" + hmac.new(b"k", b"body", hashlib.sha256).hexdigest()
