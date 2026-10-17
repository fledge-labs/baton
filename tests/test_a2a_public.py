# SPDX-License-Identifier: Apache-2.0
"""Baton's public A2A v1.0 interface on actual PostgreSQL: signed Agent Card, permission-filtered directory,
work-object-bound JSON-RPC, state mapping, cancellation and audit.

Every identity, key, task and message is synthetic.
"""

import asyncio
import copy
import json
import stat
import uuid
from pathlib import Path

import jwt
import pytest
from a2a.types import AgentCard, Task, TaskState
from cryptography.hazmat.primitives.asymmetric import ec
from google.protobuf import json_format

from bridge import a2a_public
from bridge.space_store import Actor
from test_bridge import api, op, shared_page  # noqa: F401  (api is a fixture)

ROOT = Path(__file__).resolve().parents[1]
CARD = "/.well-known/agent-card.json"
JWKS = "/.well-known/jwks.json"
BASE = "http://localhost:8080"


@pytest.fixture
def a2a(api, modules, pg_store):
    """The API fixture plus an initialized instance signing key in this test's state directory."""
    jwk = a2a_public.init_keys()
    client, call, identities, tokens, registry, records = api
    return client, call, identities, tokens, jwk


def actor_of(identity):
    return Actor(identity["agent"], identity["account"], identity["kind"])


def bearer(tokens, label):
    return {"Authorization": "Bearer " + tokens[label]}


def rpc(client, tokens, label, method, params=None, *, rpc_id=1, headers=None):
    body = {"jsonrpc": "2.0", "id": rpc_id, "method": method}
    if params is not None:
        body["params"] = params
    return client.post("/a2a/rpc", json=body, headers={**bearer(tokens, label), **(headers or {})})


def message(text="Synthetic request", **extra):
    return {"messageId": str(uuid.uuid4()), "role": "ROLE_USER", "parts": [{"text": text}], **extra}


def to_agent(page_id, agent, text="Synthetic request", message_id=None, **baton):
    built = message(text, metadata={"baton": {"page_id": page_id, "agent": agent, **baton}})
    if message_id:
        built["messageId"] = message_id
    return built


def send(client, tokens, label, built, **params):
    return rpc(client, tokens, label, "SendMessage", {"message": built, **params})


def result_of(response):
    assert response.status_code == 200, response.text
    data = response.json()
    assert "error" not in data, data
    return data["result"]


def error_of(response, code, reason=None):
    assert response.status_code == 200, response.text
    error = response.json()["error"]
    assert error["code"] == code, error
    if reason:
        assert error["data"][0]["reason"] == reason, error
    return error


def audit_rows(store, task_id):
    with store._transaction() as conn:
        return conn.execute("SELECT * FROM baton.x_audit WHERE task_id=%s ORDER BY time,id", (task_id,)).fetchall()


def assigned_task(client, tokens, call, identities, assignee="worker", **baton):
    _, page = shared_page(call, identities)
    task = result_of(send(client, tokens, "owner", to_agent(page["id"], identities[assignee]["agent"], **baton)))["task"]
    return page, task


def parse_task(task):
    """The official A2A SDK's protobuf types accept the document, so field names and enum values are v1.0."""
    return json_format.ParseDict(task, Task())


# --- Agent Card and signature -----------------------------------------------------------------------------------------

def test_card_is_public_signed_and_verifies_against_the_published_key(a2a):
    client, _, _, _, jwk = a2a
    response = client.get(CARD)
    assert response.status_code == 200 and response.headers["content-type"] == "application/json"
    assert response.headers["cache-control"] == "public, max-age=300" and response.headers["etag"]
    card = response.json()
    jwks = client.get(JWKS).json()
    assert jwks == {"keys": [jwk]} and jwk["kty"] == "EC" and jwk["crv"] == "P-256" and "d" not in jwk
    assert a2a_public.verify_card(card, jwks)
    json_format.ParseDict(card, AgentCard())
    header = json.loads(a2a_public.b64u_decode(card["signatures"][0]["protected"]))
    assert header == {"alg": "ES256", "typ": "JOSE", "kid": jwk["kid"], "jku": BASE + JWKS}
    interface = card["supportedInterfaces"][0]
    assert interface == {"url": BASE + "/a2a/rpc", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
    assert card["capabilities"]["streaming"] is True and card["capabilities"]["pushNotifications"] is False
    assert card["securitySchemes"]["baton_bearer"]["httpAuthSecurityScheme"]["scheme"] == "Bearer"
    assert [skill["id"] for skill in card["skills"]] == ["submit-task"]
    assert client.get(CARD, headers={"If-None-Match": response.headers["etag"]}).status_code == 304
    assert client.head(CARD).status_code == 200 and client.head(CARD).content == b""


def test_signature_verifies_with_an_independent_jws_implementation(a2a):
    client, _, _, _, jwk = a2a
    card = client.get(CARD).json()
    signature = card["signatures"][0]
    token = signature["protected"] + "." + a2a_public.b64u(a2a_public.signing_payload(card)) + "." + signature["signature"]
    key = jwt.algorithms.ECAlgorithm.from_jwk(json.dumps(jwk))
    header = jwt.api_jws.PyJWS().get_unverified_header(token)
    assert header["kid"] == jwk["kid"]
    payload = jwt.api_jws.PyJWS().decode(token, key, algorithms=["ES256"])
    assert json.loads(payload) == a2a_public.without_defaults({k: v for k, v in card.items() if k != "signatures"})


def test_tampered_card_fails_verification(a2a):
    client, _, _, _, _ = a2a
    card, jwks = client.get(CARD).json(), client.get(JWKS).json()
    assert a2a_public.verify_card(card, jwks)
    edits = [
        lambda c: c.__setitem__("name", "Not Baton"),
        lambda c: c["supportedInterfaces"][0].__setitem__("url", "https://attacker.example.com/a2a/rpc"),
        lambda c: c["skills"][0].__setitem__("description", "changed"),
        lambda c: c["capabilities"].__setitem__("pushNotifications", True),
        lambda c: c.__setitem__("signatures", []),
        lambda c: c.pop("signatures"),
        lambda c: c["signatures"][0].__setitem__("signature", c["signatures"][0]["signature"][:-4] + "AAAA"),
        lambda c: c["signatures"][0].__setitem__("protected", a2a_public.b64u(json.dumps({"alg": "none", "kid": "x"}).encode())),
    ]
    for edit in edits:
        forged = copy.deepcopy(card)
        edit(forged)
        assert not a2a_public.verify_card(forged, jwks)
    other = ec.generate_private_key(ec.SECP256R1())
    forged = a2a_public.sign_card({k: v for k, v in card.items() if k != "signatures"}, other,
                                  jwks["keys"][0]["kid"], BASE + JWKS)
    assert not a2a_public.verify_card(forged, jwks)
    assert not a2a_public.verify_card(card, {"keys": []}) and not a2a_public.verify_card(card, {})


def test_canonicalization_follows_rfc_8785():
    assert a2a_public.canonicalize({"b": 1, "a": [True, None, "\u00e9\u0001\"\\"], "c": {}}) == \
        '{"a":[true,null,"\u00e9\\u0001\\"\\\\"],"b":1,"c":{}}'
    # Members sort by UTF-16 code units: U+10000 (surrogates D800 DC00) comes before U+E000.
    assert a2a_public.canonicalize({"\ue000": 1, "\U00010000": 2}) == '{"\U00010000":2,"\ue000":1}'
    with pytest.raises(ValueError):
        a2a_public.canonicalize({"x": 1.5})
    assert a2a_public.without_defaults({"a": [], "b": "", "c": None, "d": False, "e": [{"f": []}]}) == {"d": False, "e": [{}]}


def test_card_requires_an_initialized_key_and_never_serves_unsigned(api, modules, pg_store):
    client, call, identities, tokens = api[0], api[1], api[2], api[3]
    for path in (CARD, JWKS):
        response = client.get(path)
        assert response.status_code == 503 and "signatures" not in response.text
        assert response.json()["error"]["code"] == "agent_card_key_unavailable"
    assert client.get("/a2a/agents", headers=bearer(tokens, "owner")).json() == {"agents": []}
    shared_page(call, identities)
    response = client.get("/a2a/agents", headers=bearer(tokens, "owner"))
    assert response.status_code == 503 and "signatures" not in response.text


def test_init_keys_protects_the_private_key_and_rotates_without_breaking_old_cards(a2a, modules, capsys):
    client, _, _, _, jwk = a2a
    key_file = a2a_public.key_directory() / a2a_public.KEY_FILE
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600 and stat.S_IMODE(key_file.parent.stat().st_mode) == 0o700
    assert key_file.parent.parent == modules[0].STATE
    assert a2a_public.main(["init-keys"]) == 1
    old_card = client.get(CARD).json()
    assert a2a_public.main(["init-keys", "--force"]) == 0
    printed = capsys.readouterr()
    private_pem = key_file.read_text()
    secret = private_pem.split("\n")[1]
    assert secret not in printed.out and secret not in printed.err and "PRIVATE KEY" not in printed.out + printed.err
    new = json.loads(printed.out)
    assert new["jwk"]["kid"] != jwk["kid"] and "d" not in new["jwk"]
    jwks = client.get(JWKS).json()
    assert [key["kid"] for key in jwks["keys"]] == [new["kid"], jwk["kid"]]
    fresh = client.get(CARD).json()
    assert a2a_public.verify_card(fresh, jwks) and a2a_public.verify_card(old_card, jwks)
    assert json.loads(a2a_public.b64u_decode(fresh["signatures"][0]["protected"]))["kid"] == new["kid"]
    key_file.chmod(0o644)
    assert client.get(CARD).status_code == 503
    key_file.chmod(0o600)
    assert client.get(CARD).status_code == 200


def test_key_material_is_not_in_the_repository():
    assert not list(ROOT.rglob("signing-key.pem"))
    source = (ROOT / "bridge/a2a_public.py").read_text()
    assert "BEGIN PRIVATE KEY" not in source and "print(private" not in source


# --- directory --------------------------------------------------------------------------------------------------------

def test_directory_lists_only_agents_the_caller_can_see(a2a):
    client, call, identities, tokens, _ = a2a
    assert client.get("/a2a/agents").status_code == 401
    space, page = shared_page(call, identities)
    jwks = client.get(JWKS).json()

    def visible(label):
        response = client.get("/a2a/agents", headers=bearer(tokens, label))
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        cards = response.json()["agents"]
        for card in cards:
            assert a2a_public.verify_card(card, jwks)
            json_format.ParseDict(card, AgentCard())
            assert card["supportedInterfaces"][0]["tenant"] == card["name"]
            assert card["supportedInterfaces"][0]["url"] == BASE + "/a2a/rpc"
        return [card["name"] for card in cards]

    worker, replacement, stranger = (identities[label]["agent"] for label in ("worker", "replacement", "stranger"))
    assert visible("owner") == sorted([worker, replacement])
    assert visible("reviewer") == sorted([worker, replacement])
    assert visible("worker") == sorted([worker, replacement])
    assert visible("stranger") == [stranger]
    call("owner", "share", operation_id=op(), resource_type="space", resource_id=space["id"], subject_type="person",
         subject_id=identities["replacement"]["account"], role=None)
    assert visible("owner") == [worker] and visible("replacement") == [replacement]
    call("owner", "share", operation_id=op(), resource_type="page", resource_id=page["id"], subject_type="person",
         subject_id=identities["stranger"]["account"], role="view")
    assert visible("owner") == sorted([worker, stranger]) and visible("stranger") == sorted([worker, stranger])


def test_directory_visibility_follows_team_grants_and_inherited_page_access(a2a, pg_store):
    client, call, identities, tokens, _ = a2a
    space = call("owner", "create_space", operation_id=op(), name="Synthetic")
    parent = call("owner", "create_page", operation_id=op(), space_id=space["id"], title="Parent")
    child = call("owner", "create_page", operation_id=op(), space_id=space["id"], title="Child", parent_id=parent["id"])
    team = call("owner", "create_team", operation_id=op(), name="Synthetic team")
    call("owner", "set_team_member", operation_id=op(), team_id=team["id"], account=identities["stranger"]["account"], member=True)
    call("owner", "share", operation_id=op(), resource_type="page", resource_id=parent["id"], subject_type="team",
         subject_id=team["id"], role="view")
    call("owner", "share", operation_id=op(), resource_type="page", resource_id=parent["id"], subject_type="person",
         subject_id=identities["worker"]["account"], role="edit")
    cards = client.get("/a2a/agents", headers=bearer(tokens, "owner")).json()["agents"]
    assert sorted(card["name"] for card in cards) == sorted(identities[label]["agent"] for label in ("worker", "stranger"))
    # The set-wise visibility used for the directory agrees with the store's own role computation for every page.
    accounts = {label: identities[label]["account"] for label in identities}
    with pg_store._transaction() as conn:
        pages = [row["id"] for row in conn.execute("SELECT id FROM baton.pages")]
        for label, account in accounts.items():
            expected = {page for page in pages if pg_store._account_role(conn, account, "page", str(page)) is not None}
            assert a2a_public._page_ids_of(conn, account) == expected, label
    assert {str(child["id"])} <= {str(page) for page in pages}


# --- SendMessage: work object binding ---------------------------------------------------------------------------------

def test_send_message_without_a_work_object_is_refused_and_audited(a2a, pg_store):
    client, call, identities, tokens, _ = a2a
    _, page = shared_page(call, identities)
    refused = [
        message(),
        message(metadata={}),
        message(metadata={"baton": {}}),
        message(metadata={"baton": {"agent": identities["worker"]["agent"]}}),
        message(metadata={"other": {"page_id": page["id"]}}),
    ]
    for built in refused:
        error_of(send(client, tokens, "owner", built), -32602, "WORK_OBJECT_REQUIRED")
    for method in ("SendMessage", "SendStreamingMessage"):
        response = rpc(client, tokens, "owner", method, {"message": message()})
        assert response.headers["content-type"].startswith("application/json")
        error_of(response, -32602, "WORK_OBJECT_REQUIRED")
    assert call("owner", "list_tasks") == []
    with pg_store._transaction() as conn:
        rows = conn.execute("SELECT * FROM baton.x_audit WHERE event='a2a.refused'").fetchall()
    assert len(rows) == 7 and {row["agent"] for row in rows} == {identities["owner"]["agent"]}
    assert all(row["task_id"] is None and row["metadata"]["reason"] == "work_object_required" for row in rows)


def test_send_message_validates_the_message_and_work_object(a2a):
    client, call, identities, tokens, _ = a2a
    _, page = shared_page(call, identities)
    worker = identities["worker"]["agent"]
    bad = [
        (message(role="ROLE_AGENT"), -32602), ({**to_agent(page["id"], worker), "messageId": ""}, -32602),
        ({**to_agent(page["id"], worker), "parts": []}, -32602),
        ({**to_agent(page["id"], worker), "parts": [{"text": "a", "url": "https://example.com/x"}]}, -32602),
        ({**to_agent(page["id"], worker), "parts": [{"url": "https://example.com/x"}]}, -32005),
        ({**to_agent(page["id"], worker), "parts": [{"data": {"k": "v"}}]}, -32005),
        (to_agent("not-a-uuid", worker), -32602), (to_agent(page["id"], "unknown-agent"), -32602),
        (to_agent(page["id"], identities["reviewer"]["agent"]), -32602),
        (to_agent(page["id"], identities["owner"]["agent"]), -32602),
        (to_agent(str(uuid.uuid4()), worker), -32602),
        (to_agent(page["id"], worker, scopes=[{"kind": "bogus"}]), -32602),
        (to_agent(page["id"], worker, unexpected=True), -32602),
        ({**to_agent(page["id"], worker), "contextId": str(uuid.uuid4())}, -32602),
    ]
    for built, code in bad:
        error_of(send(client, tokens, "owner", built), code)
    error_of(rpc(client, tokens, "owner", "SendMessage", {"message": to_agent(page["id"], worker),
                                                          "configuration": {"taskPushNotificationConfig": {"url": "https://example.com/hook"}}}), -32003)
    # A page the caller cannot see is absent; with view only the caller may not assign work on it; the target needs edit too.
    error_of(send(client, tokens, "stranger", to_agent(page["id"], worker)), -32602, "WORK_OBJECT_NOT_FOUND")
    call("owner", "share", operation_id=op(), resource_type="page", resource_id=page["id"], subject_type="person",
         subject_id=identities["stranger"]["account"], role="view")
    error_of(send(client, tokens, "stranger", to_agent(page["id"], worker)), -32000, "PERMISSION_DENIED")
    error_of(send(client, tokens, "owner", to_agent(page["id"], identities["stranger"]["agent"])), -32000, "PERMISSION_DENIED")
    assert call("owner", "list_tasks") == []


def test_send_message_to_page_and_agent_creates_an_assigned_task_in_the_assignees_inbox(a2a, pg_store):
    client, call, identities, tokens, _ = a2a
    _, page = shared_page(call, identities)
    worker = identities["worker"]["agent"]
    built = to_agent(page["id"], worker, text="Synthetic assignment through A2A")
    task = result_of(send(client, tokens, "owner", built))["task"]
    parse_task(task)
    assert task["status"]["state"] == "TASK_STATE_SUBMITTED" and task["contextId"] == page["id"]
    assert task["metadata"]["baton"]["state"] == "submitted" and task["metadata"]["baton"]["assignee"] == worker

    view = call("worker", "get_task", task_id=task["id"])
    assert view["id"] == task["id"] and view["assignee"] == worker and view["execution_mode"] == "manual"
    assert view["owner"] == identities["owner"]["agent"] and view["prompt"] == "Synthetic assignment through A2A"
    assert view["scopes"] == [{"kind": "none"}] and view["state"] == "queued"
    inbox = call("worker", "fetch_inbox")
    entry = next(item for item in inbox["messages"] if item["task_id"] == task["id"])
    assert entry["kind"] == "assignment" and entry["assignee"] == worker and entry["from"] == identities["owner"]["agent"]
    notifications = call("worker", "wait_inbox", after_seq=0, timeout_seconds=0)["notifications"]
    assert "task.assigned" in [item["event"] for item in notifications]
    assert task["history"][0]["role"] == "ROLE_USER"
    assert call("stranger", "fetch_inbox")["messages"] == []

    again = result_of(send(client, tokens, "owner", built))["task"]
    assert again["id"] == task["id"] and len(call("owner", "list_tasks")) == 1
    changed = dict(built, parts=[{"text": "Different text, same message id"}])
    error_of(send(client, tokens, "owner", changed), -32000, "OPERATION_CONFLICT")
    assert len(call("owner", "list_tasks")) == 1
    claim = call("worker", "claim", operation_id=op(), task_id=task["id"])
    assert claim["state"] == "claimed"


def test_declared_scopes_and_tenant_target_are_honored(a2a):
    client, call, identities, tokens, _ = a2a
    _, page = shared_page(call, identities)
    worker = identities["worker"]["agent"]
    built = message("Scoped", metadata={"baton": {"page_id": page["id"], "scopes": [{"kind": "path", "value": "src/a2a"}]}})
    task = result_of(send(client, tokens, "owner", built, tenant=worker))["task"]
    assert call("worker", "get_task", task_id=task["id"])["scopes"] == [{"kind": "path", "value": "src/a2a"}]
    mismatch = to_agent(page["id"], worker)
    error_of(send(client, tokens, "owner", mismatch, tenant=identities["replacement"]["agent"]), -32602)


def test_message_on_an_existing_task_becomes_a_thread_comment(a2a, pg_store):
    client, call, identities, tokens, _ = a2a
    page, task = assigned_task(client, tokens, call, identities)
    follow_up = message("Synthetic follow-up from the assignee", metadata={"baton": {"task_id": task["id"]}})
    updated = result_of(send(client, tokens, "worker", follow_up))["task"]
    assert updated["id"] == task["id"] and updated["status"]["state"] == "TASK_STATE_SUBMITTED"
    comments = call("owner", "list_comments", task_id=task["id"])
    mine = [item for item in comments if item["body"] == "Synthetic follow-up from the assignee"]
    assert len(mine) == 1 and mine[0]["author_principal"] == identities["worker"]["agent"]
    assert mine[0]["thread_id"] == task["id"] and mine[0]["page_id"] == page["id"]
    assert [m["parts"][0]["text"] for m in updated["history"]][-1] == "Synthetic follow-up from the assignee"
    assert updated["history"][-1]["role"] == "ROLE_USER" and updated["history"][-1]["metadata"]["baton"]["author"] == identities["worker"]["agent"]
    assert call("owner", "wait_inbox", after_seq=0, timeout_seconds=0)["notifications"][-1]["event"] == "comment.created"
    # The assignee's comment counts as its response in the response-time agreement.
    assert call("worker", "get_task", task_id=task["id"])["responded_at"] is not None

    result_of(send(client, tokens, "worker", follow_up))
    assert len([item for item in call("owner", "list_comments", task_id=task["id"]) if item["id"] == mine[0]["id"]]) == 1
    native = message("Through the native taskId", taskId=task["id"], contextId=page["id"])
    result_of(send(client, tokens, "owner", native))
    assert any(item["body"] == "Through the native taskId" for item in call("owner", "list_comments", task_id=task["id"]))
    history = result_of(rpc(client, tokens, "owner", "GetTask", {"id": task["id"], "historyLength": 1}))["history"]
    assert len(history) == 1 and history[0]["role"] == "ROLE_USER"
    assert "history" not in result_of(rpc(client, tokens, "owner", "GetTask", {"id": task["id"], "historyLength": 0}))

    error_of(send(client, tokens, "owner", message("x", taskId=task["id"], metadata={"baton": {"task_id": str(uuid.uuid4())}})), -32602)
    error_of(send(client, tokens, "owner", message("x", taskId=task["id"], contextId=str(uuid.uuid4()))), -32602)
    error_of(send(client, tokens, "owner", message("x", metadata={"baton": {"task_id": str(uuid.uuid4())}})), -32001)
    error_of(send(client, tokens, "stranger", message("x", metadata={"baton": {"task_id": task["id"]}})), -32001)
    call("owner", "share", operation_id=op(), resource_type="page", resource_id=page["id"], subject_type="person",
         subject_id=identities["stranger"]["account"], role="view")
    error_of(send(client, tokens, "stranger", message("x", metadata={"baton": {"task_id": task["id"]}})), -32000, "PERMISSION_DENIED")


def test_a_terminal_task_takes_no_further_messages(a2a):
    client, call, identities, tokens, _ = a2a
    _, task = assigned_task(client, tokens, call, identities)
    claim = call("worker", "claim", operation_id=op(), task_id=task["id"])
    call("worker", "submit_result", operation_id=op(), task_id=task["id"], epoch=claim["epoch"], result="Synthetic result")
    call("owner", "gate", operation_id=op(), task_id=task["id"], expected_epoch=claim["epoch"], decision="pass")
    error_of(send(client, tokens, "owner", message("late", metadata={"baton": {"task_id": task["id"]}})), -32004, "TASK_TERMINAL")


# --- GetTask and the state mapping ------------------------------------------------------------------------------------

def test_state_mapping_table_is_a_bijection_onto_the_a2a_states():
    assert a2a_public.BATON_TO_A2A == {
        "submitted": "TASK_STATE_SUBMITTED", "working": "TASK_STATE_WORKING", "input_required": "TASK_STATE_INPUT_REQUIRED",
        "auth_required": "TASK_STATE_AUTH_REQUIRED", "completed": "TASK_STATE_COMPLETED", "failed": "TASK_STATE_FAILED",
        "canceled": "TASK_STATE_CANCELED", "rejected": "TASK_STATE_REJECTED"}
    sdk_states = {name for name in TaskState.keys() if name != "TASK_STATE_UNSPECIFIED"}
    assert set(a2a_public.BATON_TO_A2A.values()) == sdk_states
    for baton, a2a_name in a2a_public.BATON_TO_A2A.items():
        assert a2a_public.a2a_state(baton) == a2a_name and a2a_public.baton_from_a2a(a2a_name) == baton
    for a2a_name in sdk_states:
        assert a2a_public.a2a_state(a2a_public.baton_from_a2a(a2a_name)) == a2a_name
    for unknown in ("TASK_STATE_UNSPECIFIED", "working", None, "TASK_STATE_PAUSED"):
        with pytest.raises(ValueError):
            a2a_public.baton_from_a2a(unknown)
    assert a2a_public.TERMINAL == {"completed", "failed", "canceled", "rejected"}
    assert a2a_public.INTERRUPTED == {"input_required", "auth_required"}
    assert set(a2a_public.STORE_TO_BATON) == {"queued", "claimed", "awaiting_gate", "done", "failed", "interrupted"}
    assert set(a2a_public.STORE_TO_BATON.values()) <= set(a2a_public.BATON_TO_A2A)


def test_get_task_projects_every_baton_state_and_reverses_exactly(a2a, pg_store):
    client, call, identities, tokens, _ = a2a
    _, page = shared_page(call, identities)
    worker = identities["worker"]["agent"]

    def new_task(text):
        return result_of(send(client, tokens, "owner", to_agent(page["id"], worker, text=text)))["task"]

    def get(task_id, label="owner"):
        task = result_of(rpc(client, tokens, label, "GetTask", {"id": task_id}))
        parse_task(task)
        return task

    seen = {}

    def expect(task, a2a_name, baton, store=None):
        assert task["status"]["state"] == a2a_name
        assert task["metadata"]["baton"]["state"] == baton
        assert a2a_public.baton_from_a2a(task["status"]["state"]) == task["metadata"]["baton"]["state"]
        if store:
            assert task["metadata"]["baton"]["store_state"] == store
        seen[baton] = a2a_name

    first = new_task("one")
    expect(get(first["id"]), "TASK_STATE_SUBMITTED", "submitted", "queued")
    call("worker", "mark_blocked", operation_id=op(), task_id=first["id"], reason_code="waiting_input", note="Need a decision")
    expect(get(first["id"]), "TASK_STATE_INPUT_REQUIRED", "input_required", "queued")
    claim = call("worker", "claim", operation_id=op(), task_id=first["id"])
    expect(get(first["id"]), "TASK_STATE_WORKING", "working", "claimed")
    call("worker", "submit_result", operation_id=op(), task_id=first["id"], epoch=claim["epoch"], result="Synthetic candidate")
    waiting = get(first["id"])
    expect(waiting, "TASK_STATE_INPUT_REQUIRED", "input_required", "awaiting_gate")
    assert "artifacts" not in waiting
    call("owner", "gate", operation_id=op(), task_id=first["id"], expected_epoch=claim["epoch"], decision="pass")
    done = get(first["id"])
    expect(done, "TASK_STATE_COMPLETED", "completed", "done")
    assert done["artifacts"][0]["parts"][0]["text"] == "Synthetic candidate"
    assert get(first["id"], "worker")["status"]["state"] == "TASK_STATE_COMPLETED"

    second = new_task("two")
    canceled = result_of(rpc(client, tokens, "owner", "CancelTask", {"id": second["id"]}))
    parse_task(canceled)
    expect(get(second["id"]), "TASK_STATE_CANCELED", "canceled", "queued")

    third = new_task("three")
    with pg_store._transaction(write=True) as conn:
        conn.execute("INSERT INTO baton.x_a2a_task_state(task_id,state,set_by,set_by_account,set_by_kind) VALUES (%s,'rejected','x','x','agent')",
                     (third["id"],))
    expect(get(third["id"]), "TASK_STATE_REJECTED", "rejected")

    # The task store's failed and interrupted states both read as failed, with the store state kept.
    for store in ("failed", "interrupted"):
        broken = new_task("broken " + store)
        with pg_store._transaction(write=True) as conn:
            conn.execute("UPDATE baton.tasks SET state=%s WHERE id=%s", (store, broken["id"]))
        expect(get(broken["id"]), "TASK_STATE_FAILED", "failed", store)

    assert set(seen) == {"submitted", "input_required", "working", "completed", "canceled", "rejected", "failed"}
    assert a2a_public.baton_state({"state": "queued", "blocked": {"reason_code": "scope_conflict"}}) == "submitted"
    assert a2a_public.baton_state({"state": "queued", "blocked": None}) == "submitted"
    assert "auth_required" not in seen and a2a_public.a2a_state("auth_required") == "TASK_STATE_AUTH_REQUIRED"


def test_get_task_enforces_view_permission_and_validates_ids(a2a):
    client, call, identities, tokens, _ = a2a
    _, task = assigned_task(client, tokens, call, identities)
    error_of(rpc(client, tokens, "stranger", "GetTask", {"id": task["id"]}), -32001, "TASK_NOT_FOUND")
    error_of(rpc(client, tokens, "owner", "GetTask", {"id": str(uuid.uuid4())}), -32001)
    error_of(rpc(client, tokens, "owner", "GetTask", {"id": "nope"}), -32602)
    error_of(rpc(client, tokens, "owner", "GetTask", {}), -32602)
    error_of(rpc(client, tokens, "owner", "GetTask", {"id": task["id"], "historyLength": -1}), -32602)
    assert result_of(rpc(client, tokens, "reviewer", "GetTask", {"id": task["id"]}))["id"] == task["id"]


# --- method names, versions and transport -----------------------------------------------------------------------------

def test_v03_method_names_are_rejected_with_the_v10_replacement(a2a):
    client, call, identities, tokens, _ = a2a
    _, page = shared_page(call, identities)
    for old, new in a2a_public.V03_METHODS.items():
        error = error_of(rpc(client, tokens, "owner", old, {"message": to_agent(page["id"], identities["worker"]["agent"])}),
                         -32009, "VERSION_NOT_SUPPORTED")
        assert new in error["message"] and "v0.3" in error["message"]
        assert error["data"][0]["metadata"]["replacement"] == new
    assert call("owner", "list_tasks") == []
    error_of(rpc(client, tokens, "owner", "SendMessage", {"message": to_agent(page["id"], identities["worker"]["agent"])},
                 headers={"A2A-Version": "0.3"}), -32009)
    result_of(rpc(client, tokens, "owner", "SendMessage", {"message": to_agent(page["id"], identities["worker"]["agent"])},
                  headers={"A2A-Version": "1.0"}))
    error_of(rpc(client, tokens, "owner", "tasks/nothing"), -32601)
    error_of(rpc(client, tokens, "owner", "sendmessage"), -32601)


def test_unsupported_operations_and_malformed_requests(a2a):
    client, _, _, tokens, _ = a2a
    for method in ("CreateTaskPushNotificationConfig", "GetTaskPushNotificationConfig", "ListTaskPushNotificationConfigs",
                   "DeleteTaskPushNotificationConfig"):
        error_of(rpc(client, tokens, "owner", method, {}), -32003)
    for method in ("ListTasks", "GetExtendedAgentCard"):
        error_of(rpc(client, tokens, "owner", method, {}), -32004)
    headers = bearer(tokens, "owner")
    assert client.post("/a2a/rpc", content=b"{not json", headers=headers).json()["error"]["code"] == -32700
    assert client.post("/a2a/rpc", json=[{"jsonrpc": "2.0", "id": 1, "method": "GetTask"}], headers=headers).json()["error"]["code"] == -32600
    assert client.post("/a2a/rpc", json={"jsonrpc": "1.0", "id": 1, "method": "GetTask"}, headers=headers).json()["error"]["code"] == -32600
    assert client.post("/a2a/rpc", json={"jsonrpc": "2.0", "id": 1, "method": "GetTask", "params": []}, headers=headers).json()["error"]["code"] == -32600
    assert client.post("/a2a/rpc", json={"jsonrpc": "2.0", "id": {"x": 1}, "method": "GetTask"}, headers=headers).json()["error"]["code"] == -32600
    echoed = client.post("/a2a/rpc", json={"jsonrpc": "2.0", "id": "abc", "method": "GetTask", "params": {}}, headers=headers).json()
    assert echoed["id"] == "abc" and echoed["jsonrpc"] == "2.0"
    big = client.post("/a2a/rpc", content=b" " * (a2a_public.MAX_BODY_BYTES + 1), headers=headers).json()
    assert big["error"]["code"] == -32600


def test_rpc_and_directory_require_a_baton_client_token(a2a):
    client, _, _, tokens, _ = a2a
    body = {"jsonrpc": "2.0", "id": 1, "method": "GetTask", "params": {"id": str(uuid.uuid4())}}
    assert client.post("/a2a/rpc", json=body).status_code == 401
    assert client.post("/a2a/rpc", json=body, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/a2a/agents", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/a2a/rpc", headers=bearer(tokens, "owner")).status_code in {404, 405}


# --- streaming --------------------------------------------------------------------------------------------------------

def frames(response):
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]


def test_streaming_message_returns_sse_with_the_task_and_ends_at_the_cap(a2a, monkeypatch):
    client, call, identities, tokens, _ = a2a
    monkeypatch.setattr(a2a_public, "STREAM_POLL_SECONDS", 0.05)
    monkeypatch.setattr(a2a_public, "STREAM_MAX_SECONDS", 0.3)
    _, page = shared_page(call, identities)
    response = rpc(client, tokens, "owner", "SendStreamingMessage", {"message": to_agent(page["id"], identities["worker"]["agent"])}, rpc_id=9)
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-store"
    events = frames(response)
    assert events[0]["id"] == 9 and events[0]["jsonrpc"] == "2.0"
    task = events[0]["result"]["task"]
    parse_task(task)
    assert task["status"]["state"] == "TASK_STATE_SUBMITTED"
    assert len(call("owner", "list_tasks")) == 1
    again = rpc(client, tokens, "worker", "SubscribeToTask", {"id": task["id"]}, rpc_id=10)
    assert again.headers["content-type"].startswith("text/event-stream")
    assert frames(again)[0]["result"]["task"]["id"] == task["id"]
    error_of(rpc(client, tokens, "stranger", "SubscribeToTask", {"id": task["id"]}), -32001)


def test_subscribe_to_a_terminal_task_is_an_unsupported_operation(a2a):
    client, call, identities, tokens, _ = a2a
    _, task = assigned_task(client, tokens, call, identities)
    result_of(rpc(client, tokens, "owner", "CancelTask", {"id": task["id"]}))
    response = rpc(client, tokens, "owner", "SubscribeToTask", {"id": task["id"]})
    assert response.headers["content-type"].startswith("application/json")
    error_of(response, -32004, "TASK_TERMINAL")


@pytest.mark.asyncio
async def test_stream_emits_comments_and_state_updates_then_closes(a2a, pg_store, modules, monkeypatch):
    _, call, identities, _, _ = a2a
    monkeypatch.setattr(a2a_public, "STREAM_POLL_SECONDS", 0.02)
    owner, worker = actor_of(identities["owner"]), actor_of(identities["worker"])
    _, page = shared_page(call, identities)
    ctx = a2a_public.Context(pg_store, modules[1].store_call, owner)
    after, _ = await a2a_public.send(ctx, {"message": to_agent(page["id"], worker.principal)})
    task_id = after.view["id"]
    stream = a2a_public.stream_events(ctx, "s1", after)

    async def next_event():
        raw = await asyncio.wait_for(anext(stream), 5)
        assert raw.startswith("data: ") and raw.endswith("\n\n")
        return json.loads(raw[6:])["result"]

    assert (await next_event())["task"]["id"] == task_id
    pg_store.comment(worker, op(), page["id"], "Working on it, one question", thread_id=task_id)
    event = await next_event()
    assert event["message"]["parts"][0]["text"] == "Working on it, one question"
    assert event["message"]["role"] == "ROLE_AGENT" and event["message"]["taskId"] == task_id
    claim = pg_store.claim(worker, op(), task_id)
    event = await next_event()
    assert event["statusUpdate"]["status"]["state"] == "TASK_STATE_WORKING"
    baton = event["statusUpdate"]["metadata"]["baton"]
    assert baton["state"] == "working" and baton["store_state"] == "claimed" and baton["task_id"] == task_id
    pg_store.submit_result(worker, op(), task_id, claim["epoch"], "Synthetic candidate")
    event = await next_event()
    while "statusUpdate" not in event:
        event = await next_event()
    # The gate request is an interrupted state: the final update is sent and the stream ends.
    assert event["statusUpdate"]["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(stream), 5)


@pytest.mark.asyncio
async def test_stream_ends_on_terminal_state_and_reports_lost_access(a2a, pg_store, modules, monkeypatch):
    _, call, identities, _, _ = a2a
    monkeypatch.setattr(a2a_public, "STREAM_POLL_SECONDS", 0.02)
    monkeypatch.setattr(a2a_public, "STREAM_KEEPALIVE_SECONDS", 0.05)
    owner = actor_of(identities["owner"])
    _, page = shared_page(call, identities)
    ctx = a2a_public.Context(pg_store, modules[1].store_call, owner)
    after, _ = await a2a_public.send(ctx, {"message": to_agent(page["id"], identities["worker"]["agent"])})
    stream = a2a_public.stream_events(ctx, 1, after)
    await anext(stream)
    assert await asyncio.wait_for(anext(stream), 5) == ": keepalive\n\n"
    result = await a2a_public.cancel_task(ctx, {"id": after.view["id"]}, 1)
    assert result["status"]["state"] == "TASK_STATE_CANCELED"
    events = []
    with pytest.raises(StopAsyncIteration):
        while True:
            events.append(json.loads((await asyncio.wait_for(anext(stream), 5))[6:])["result"])
    # The thread note about the cancellation comes first, then the terminal state update.
    assert events[0]["message"]["parts"][0]["text"].startswith("[canceled]")
    assert events[-1]["statusUpdate"]["status"]["state"] == "TASK_STATE_CANCELED"

    stranger = Actor(identities["stranger"]["agent"], identities["stranger"]["account"], "agent")
    fresh, _ = await a2a_public.send(ctx, {"message": to_agent(page["id"], identities["worker"]["agent"], "another")})
    lost = a2a_public.stream_events(a2a_public.Context(pg_store, modules[1].store_call, stranger), 2, fresh)
    await anext(lost)
    error = json.loads((await asyncio.wait_for(anext(lost), 5))[6:])
    assert error["error"]["code"] == -32001


# --- CancelTask -------------------------------------------------------------------------------------------------------

def test_cancel_task_stops_the_work_and_frees_the_scope(a2a, pg_store):
    client, call, identities, tokens, _ = a2a
    _, page = shared_page(call, identities)
    worker, replacement = identities["worker"]["agent"], identities["replacement"]["agent"]
    def scoped(agent, text):
        built = to_agent(page["id"], agent, text, scopes=[{"kind": "path", "value": "src/shared"}])
        return result_of(send(client, tokens, "owner", built))["task"]

    first, second = scoped(worker, "first"), scoped(replacement, "second")
    claim = call("worker", "claim", operation_id=op(), task_id=first["id"])
    assert call("replacement", "claim", 409, operation_id=op(), task_id=second["id"])["code"] == "scope_conflict"

    error_of(rpc(client, tokens, "stranger", "CancelTask", {"id": first["id"]}), -32001)
    error_of(rpc(client, tokens, "worker", "CancelTask", {"id": first["id"]}), -32000, "PERMISSION_DENIED")
    refusals = [row for row in audit_rows(pg_store, first["id"]) if row["event"] == "a2a.refused"]
    assert [(row["agent"], row["metadata"]["reason"]) for row in refusals] == [(identities["worker"]["agent"], "not_task_owner_or_reviewer")]
    error_of(rpc(client, tokens, "owner", "CancelTask", {"id": "bad"}), -32602)
    canceled = result_of(rpc(client, tokens, "owner", "CancelTask", {"id": first["id"]}))
    assert canceled["status"]["state"] == "TASK_STATE_CANCELED" and canceled["metadata"]["baton"]["state"] == "canceled"
    error_of(rpc(client, tokens, "owner", "CancelTask", {"id": first["id"]}), -32002, "TASK_NOT_CANCELABLE")
    assert canceled["history"][-1]["parts"][0]["text"].startswith("[canceled]")

    # Every public surface refuses work on the canceled task; the reservation is released at once.
    assert call("worker", "submit_result", 409, operation_id=op(), task_id=first["id"], epoch=claim["epoch"], result="late")["code"] == "task_canceled"
    assert call("worker", "renew", 409, operation_id=op(), task_id=first["id"], epoch=claim["epoch"])["code"] == "task_canceled"
    assert call("worker", "claim", 409, operation_id=op(), task_id=first["id"])["code"] == "task_canceled"
    assert call("owner", "assign_task", 409, operation_id=op(), task_id=first["id"], target_principal=replacement)["code"] == "task_canceled"
    assert call("stranger", "claim", 404, operation_id=op(), task_id=first["id"])["code"] == "not_found"
    assert call("replacement", "claim", operation_id=op(), task_id=second["id"])["state"] == "claimed"
    assert call("worker", "get_task", task_id=first["id"])["state"] in {"queued", "claimed"}
    assert call("worker", "wait_inbox", after_seq=0, timeout_seconds=0)["notifications"][-1]["event"] == "comment.created"
    with pytest.raises(Exception):
        with pg_store._transaction(write=True) as conn:
            conn.execute("DELETE FROM baton.x_a2a_task_state WHERE task_id=%s", (first["id"],))
    with pytest.raises(Exception):
        with pg_store._transaction(write=True) as conn:
            conn.execute("UPDATE baton.x_a2a_task_state SET state='rejected' WHERE task_id=%s", (first["id"],))


def test_cancel_task_is_refused_for_finished_tasks(a2a):
    client, call, identities, tokens, _ = a2a
    _, task = assigned_task(client, tokens, call, identities)
    claim = call("worker", "claim", operation_id=op(), task_id=task["id"])
    call("worker", "submit_result", operation_id=op(), task_id=task["id"], epoch=claim["epoch"], result="Synthetic result")
    call("owner", "gate", operation_id=op(), task_id=task["id"], expected_epoch=claim["epoch"], decision="pass")
    error_of(rpc(client, tokens, "owner", "CancelTask", {"id": task["id"]}), -32002)


def test_reviewer_may_cancel_and_an_awaiting_gate_task_cannot_be_gated_afterwards(a2a):
    client, call, identities, tokens, _ = a2a
    _, page = shared_page(call, identities)
    created = call("owner", "create_task_block", operation_id=op(), page_id=page["id"], prompt="Synthetic",
                   reviewers=[identities["reviewer"]["agent"]], assignee_principal=identities["worker"]["agent"])
    claim = call("worker", "claim", operation_id=op(), task_id=created["task_id"])
    call("worker", "submit_result", operation_id=op(), task_id=created["task_id"], epoch=claim["epoch"], result="Synthetic result")
    task = result_of(rpc(client, tokens, "reviewer", "CancelTask", {"id": created["task_id"]}))
    assert task["status"]["state"] == "TASK_STATE_CANCELED"
    assert call("reviewer", "gate", 409, operation_id=op(), task_id=created["task_id"], expected_epoch=claim["epoch"],
                decision="pass")["code"] == "task_canceled"


# --- audit ------------------------------------------------------------------------------------------------------------

def test_every_a2a_call_is_audited_on_the_task_with_the_real_caller(a2a, pg_store, monkeypatch):
    client, call, identities, tokens, _ = a2a
    monkeypatch.setattr(a2a_public, "STREAM_POLL_SECONDS", 0.05)
    monkeypatch.setattr(a2a_public, "STREAM_MAX_SECONDS", 0.2)
    page, task = assigned_task(client, tokens, call, identities)
    result_of(send(client, tokens, "worker", message("Question from the assignee", metadata={"baton": {"task_id": task["id"]}})))
    result_of(rpc(client, tokens, "reviewer", "GetTask", {"id": task["id"]}))
    result_of(rpc(client, tokens, "worker", "GetTask", {"id": task["id"]}))
    rpc(client, tokens, "worker", "SubscribeToTask", {"id": task["id"]}, headers={"X-Request-ID": "11111111-2222-4333-8444-555555555555"})
    result_of(rpc(client, tokens, "owner", "CancelTask", {"id": task["id"]}))
    error_of(rpc(client, tokens, "worker", "CancelTask", {"id": task["id"]}), -32002)
    rows = [row for row in audit_rows(pg_store, task["id"]) if row["event"].startswith("a2a.")]
    assert [(row["event"], row["agent"]) for row in rows] == [
        ("a2a.message_sent", identities["owner"]["agent"]),
        ("a2a.message_sent", identities["worker"]["agent"]),
        ("a2a.task_read", identities["reviewer"]["agent"]),
        ("a2a.task_read", identities["worker"]["agent"]),
        ("a2a.task_streamed", identities["worker"]["agent"]),
        ("a2a.task_canceled", identities["owner"]["agent"]),
    ]
    for row in rows:
        who = next(identity for identity in identities.values() if identity["agent"] == row["agent"])
        assert (row["actor_account"], row["actor_kind"]) == (who["account"], who["kind"])
        assert row["requested_by"] == row["performed_by"] == row["agent"] and row["space_id"] is not None
        assert row["metadata"]["a2a_version"] == "1.0"
    assert rows[0]["metadata"]["outcome"] == "task_created" and rows[1]["metadata"]["outcome"] == "comment_appended"
    assert rows[0]["metadata"]["method"] == "SendMessage" and len(rows[0]["metadata"]["input_sha256"]) == 64
    assert rows[4]["metadata"]["request_id"] == "11111111-2222-4333-8444-555555555555"
    assert "Question from the assignee" not in json.dumps([row["metadata"] for row in rows])
    events = [row["event"] for row in audit_rows(pg_store, task["id"])]
    assert {"task.submitted", "task.assigned", "comment.created", "a2a.message_sent"} <= set(events)
    # The same rows are visible through the ordinary audit operation to anyone who can view the task.
    visible = call("reviewer", "list_audit", task_id=task["id"])
    assert [row["event"] for row in visible if row["event"].startswith("a2a.")] == [row["event"] for row in rows]
    with pytest.raises(Exception):
        with pg_store._transaction(write=True) as conn:
            conn.execute("UPDATE baton.x_audit SET agent='someone-else' WHERE task_id=%s", (task["id"],))


def test_a_hidden_task_leaves_no_audit_trail_for_the_prober(a2a, pg_store):
    client, call, identities, tokens, _ = a2a
    _, task = assigned_task(client, tokens, call, identities)
    error_of(rpc(client, tokens, "stranger", "GetTask", {"id": task["id"]}), -32001)
    error_of(rpc(client, tokens, "stranger", "CancelTask", {"id": task["id"]}), -32001)
    assert not [row for row in audit_rows(pg_store, task["id"]) if row["agent"] == identities["stranger"]["agent"]]


# --- deployment routing -----------------------------------------------------------------------------------------------

def gateway_routes():
    template = json.loads((ROOT / "deploy/gateway.template.json").read_text())
    return {route["name"]: route for bind in template["binds"] for listener in bind["listeners"] for route in listener["routes"]}


def route_paths(route):
    return {(kind, value) for match in route["matches"] for kind, value in match["path"].items()}


def test_gateway_routes_the_public_endpoints():
    routes = gateway_routes()
    space = routes["baton-space"]
    assert {("exact", "/a2a/rpc"), ("exact", "/a2a/agents")} <= route_paths(space)
    assert space["policies"]["authorization"]["rules"] == ["apiKey.role == 'client'"] and space["policies"]["apiKey"]["mode"] == "strict"
    assert space["backends"] == [{"host": "bridge:9000"}]
    public = routes["baton-a2a-public"]
    assert route_paths(public) == {("exact", CARD), ("exact", JWKS)}
    assert public["backends"] == [{"host": "bridge:9000"}]
    assert "authorization" not in public["policies"] and public["policies"]["apiKey"]["mode"] == "optional"
    assert "transformations" not in public["policies"]
    for name, route in routes.items():
        assert "keys" in route["policies"]["apiKey"], name
    # The card path must resolve to Baton's own card: no route listed before the public one may match it.
    names = list(routes)
    assert not [name for name in names[:names.index("baton-a2a-public")] if ("exact", CARD) in route_paths(routes[name])]


def test_nginx_serves_the_cards_with_caching_and_keeps_the_rpc_stream_unbuffered():
    for filename in ("nginx.conf", "nginx.tls.conf"):
        config = (ROOT / "deploy" / filename).read_text()
        card = config[config.index("location ~ ^/\\.well-known/(agent-card|jwks)\\.json$"):]
        card = card[:card.index("}") + 1]
        assert "proxy_pass http://gateway:3000;" in card and "add_header X-Content-Type-Options nosniff always;" in card
        # Declaring add_header in the location drops the server-level no-store, so the bridge's caching headers apply.
        assert "Cache-Control" not in card and ("Strict-Transport-Security" in card) == (filename == "nginx.tls.conf")
        assert "add_header Cache-Control no-store always;" in config and "proxy_buffering off;" in config
        assert "$http_authorization" not in config
