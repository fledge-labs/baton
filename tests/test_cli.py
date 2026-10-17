# SPDX-License-Identifier: Apache-2.0
"""adapters/baton_cli.py against the real bridge app over a local HTTPS server with a generated CA."""

import asyncio
import base64
import datetime
import hashlib
import importlib.util
import ipaddress
import json
import os
import queue
import re
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "adapters" / "baton_cli.py"
TASK_A = "11111111-1111-4111-8111-111111111111"
TASK_B = "22222222-2222-4222-8222-222222222222"
PAGE = "33333333-3333-4333-8333-333333333333"


def load_cli_module():
    spec = importlib.util.spec_from_file_location("baton_cli_under_test", CLI)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def new_key(kind="ec"):
    if kind == "rsa":
        return rsa.generate_private_key(public_exponent=65537, key_size=2048)
    if kind == "ed25519":
        return ed25519.Ed25519PrivateKey.generate()
    return ec.generate_private_key(ec.SECP256R1())


def spki_pin(certificate):
    der = certificate.public_key().public_bytes(serialization.Encoding.DER,
                                                serialization.PublicFormat.SubjectPublicKeyInfo)
    return base64.b64encode(hashlib.sha256(der).digest()).decode()


def make_ca(name):
    key = new_key()
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
                   .public_key(key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=1))
                   .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                   .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                                content_commitment=False, key_encipherment=False,
                                                data_encipherment=False, key_agreement=False,
                                                encipher_only=False, decipher_only=False), critical=True)
                   .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
                   .sign(key, hashes.SHA256()))
    return key, certificate


def make_leaf(ca_key, ca_certificate, key_kind="ec"):
    """Server certificate valid only for the IP 127.0.0.1 (not for the name localhost)."""
    key = new_key(key_kind)
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (x509.CertificateBuilder()
                   .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "baton-test")]))
                   .issuer_name(ca_certificate.subject).public_key(key.public_key())
                   .serial_number(x509.random_serial_number())
                   .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=1))
                   .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                                  critical=False)
                   .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                   .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                   .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
                   .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
                                  critical=False)
                   .sign(ca_key, hashes.SHA256()))
    return key, certificate


def write_pem(path, certificate=None, key=None):
    if certificate is not None:
        path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    if key is not None:
        path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return path


@pytest.fixture
def pki(tmp_path):
    ca_key, ca_certificate = make_ca("Baton test CA")
    other_key, other_certificate = make_ca("Unrelated CA")
    leaf_key, leaf_certificate = make_leaf(ca_key, ca_certificate)
    directory = tmp_path / "pki"
    directory.mkdir()
    return {
        "ca": write_pem(directory / "ca.pem", ca_certificate),
        "other_ca": write_pem(directory / "other-ca.pem", other_certificate),
        "cert": write_pem(directory / "server.pem", leaf_certificate),
        "key": write_pem(directory / "server.key", key=leaf_key),
        "pin": spki_pin(leaf_certificate),
        "ca_pin": spki_pin(ca_certificate),
    }


class Recorder:
    """ASGI wrapper that records what the bridge actually received."""

    def __init__(self, app):
        self.app, self.requests = app, []

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        chunks = []

        async def spy():
            message = await receive()
            if message["type"] == "http.request":
                chunks.append(message.get("body", b""))
            return message

        headers = {key.decode().lower(): value.decode("latin-1") for key, value in scope["headers"]}
        self.requests.append({"method": scope["method"], "path": scope["path"], "headers": headers,
                              "chunks": chunks})
        await self.app(scope, spy, send)

    @staticmethod
    def body(record):
        return json.loads(b"".join(record["chunks"]) or b"null")


class ServerThread:
    def __init__(self, app, **config):
        import uvicorn
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.port = self.socket.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False,
                                                    log_level="critical", **config))
        self.thread = threading.Thread(target=lambda: asyncio.run(self.server.serve(sockets=[self.socket])),
                                       daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.monotonic() + 20
        while not self.server.started:
            assert self.thread.is_alive() and time.monotonic() < deadline, "test server did not start"
            time.sleep(0.02)
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(15)
        self.socket.close()


def clean_env(home, **extra):
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "LANG": "C.UTF-8"}
    env.update(extra)
    return env


class Result:
    def __init__(self, process):
        self.rc, self.out, self.err = process.returncode, process.stdout, process.stderr

    def json(self):
        return json.loads(self.out)


class Cli:
    """Runs the CLI as a subprocess; every call is checked for token leaks."""

    def __init__(self, tmp_path):
        self.home = tmp_path / "home"
        self.home.mkdir(exist_ok=True)
        self.secrets = []

    def guard(self, *texts):
        for secret in self.secrets:
            for text in texts:
                assert secret not in text, "token leaked into CLI output"

    def run(self, *args, env=None, stdin=None):
        process = subprocess.run([sys.executable, str(CLI), *map(str, args)], capture_output=True, text=True,
                                 input=stdin, env=clean_env(self.home, **(env or {})), timeout=90)
        self.guard(process.stdout, process.stderr)
        return Result(process)


@pytest.fixture
def cli(tmp_path):
    return Cli(tmp_path)


def token_file(directory, name, token):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (name + ".token")
    path.write_text(token + "\n")
    path.chmod(0o600)
    return path


@pytest.fixture
def plain_token(tmp_path, cli):
    token = "synthetic-" + uuid.uuid4().hex
    cli.secrets.append(token)
    return token_file(tmp_path / "plain", "agent", token)


@pytest.fixture
def bridge(modules, pg_store, make_context, register_identity, pki, tmp_path, cli):
    """The real bridge app, recorded, served over HTTPS (generated CA) and plain loopback HTTP."""
    _, server = modules
    recorder = Recorder(server.app)
    identities, files = {}, {}
    for label, kind in (("owner", "human"), ("worker", "agent"), ("reviewer", "human")):
        who = make_context(label, kind=kind).request_context.request.state.identity
        token = register_identity(who)
        cli.secrets.append(token)
        identities[label] = who
        files[label] = token_file(tmp_path / "tokens", label, token)
    tls = ServerThread(recorder, ssl_certfile=str(pki["cert"]), ssl_keyfile=str(pki["key"]), lifespan="on")
    plain = ServerThread(recorder, lifespan="off")
    with tls, plain:
        yield type("Bridge", (), {
            "https": "https://127.0.0.1:%d" % tls.port, "http": "http://127.0.0.1:%d" % plain.port,
            "port": tls.port, "identities": identities, "tokens": files, "requests": recorder.requests,
            "recorder": recorder, "pki": pki,
        })


def run_as(cli, bridge, label, *args, url=None, ca=True, pin=None, env=None, stdin=None):
    options = ["--url", url or bridge.https, "--token-file", bridge.tokens[label]]
    if ca:
        options += ["--ca-file", bridge.pki["ca"]]
    if pin:
        options += ["--pin-sha256", pin]
    return cli.run(*options, *args, env=env, stdin=stdin)


def run_json(cli, bridge, label, *args, **kwargs):
    result = run_as(cli, bridge, label, *args, **kwargs)
    assert result.rc == 0, result.err
    return result.json()


def last_request(bridge):
    record = bridge.requests[-1]
    return record, bridge.recorder.body(record)


# --- Usage and local validation (no server needed) ---------------------------------------------

def test_spki_extraction_matches_cryptography_for_each_key_type():
    module = load_cli_module()
    ca_key, ca_certificate = make_ca("Unit CA")
    for kind in ("ec", "rsa", "ed25519"):
        _, certificate = make_leaf(ca_key, ca_certificate, kind)
        der = certificate.public_bytes(serialization.Encoding.DER)
        expected = certificate.public_key().public_bytes(serialization.Encoding.DER,
                                                         serialization.PublicFormat.SubjectPublicKeyInfo)
        assert module.spki_from_certificate(der) == expected
        assert base64.b64encode(module.spki_sha256(der)).decode() == spki_pin(certificate)
    for broken in (b"", b"\x30", b"\x30\x03\x01\x01", der[:100], b"\x04" + der[1:]):
        with pytest.raises(ValueError):
            module.spki_from_certificate(broken)


@pytest.mark.parametrize("url", [
    "http://example.com", "http://203.0.113.9:8080", "http://127.0.0.1.example.com",
    "http://localhost.example.com", "http://[::2]:80",
])
def test_plain_http_is_refused_for_non_local_hosts(cli, plain_token, url):
    result = cli.run("--url", url, "--token-file", plain_token, "tasks")
    assert result.rc == 2 and result.out == ""
    assert "refusing plain http://" in result.err


@pytest.mark.parametrize("url", [
    "http://user:hunter2@127.0.0.1", "https://host/path?x=1", "https://host/#frag", "ftp://host", "https://",
    "https://host:notaport",
])
def test_malformed_urls_are_usage_errors_without_echoing_them(cli, plain_token, url):
    result = cli.run("--url", url, "--token-file", plain_token, "tasks")
    assert result.rc == 2
    assert "hunter2" not in result.err and "notaport" not in result.err and "frag" not in result.err


def test_endpoint_parsing_accepts_local_http_and_strips_mcp_suffix():
    module = load_cli_module()
    for url in ("http://localhost:8080", "http://127.0.0.1", "HTTP://LOCALHOST"):
        assert module.parse_endpoint(url).scheme == "http"
    endpoint = module.parse_endpoint("https://example.com:8443/mcp/")
    assert (endpoint.scheme, endpoint.host, endpoint.port, endpoint.prefix) == ("https", "example.com", 8443, "")
    assert module.parse_endpoint("https://example.com/baton").prefix == "/baton"
    assert module.parse_endpoint("https://example.com").port == 443


def test_missing_url_is_a_usage_error(cli, plain_token):
    assert cli.run("--token-file", plain_token, "tasks").rc == 2


def test_url_comes_from_baton_url_env(cli, plain_token):
    result = cli.run("--token-file", plain_token, "tasks", env={"BATON_URL": "http://example.com"})
    assert result.rc == 2 and "refusing plain http://" in result.err


def test_token_is_only_read_from_a_file(cli, plain_token):
    token = plain_token.read_text().strip()
    env = {"BATON_TOKEN": token, "BATON_TOKEN_FILE": str(plain_token)}
    no_file = cli.run("--url", "https://example.com", "tasks", env=env)
    assert no_file.rc == 2 and "--token-file" in no_file.err
    for flag in ("--token", "--tok"):
        result = cli.run("--url", "https://example.com", flag, token, "tasks", env=env)
        assert result.rc == 2 and token not in result.err and token not in result.out
    result = cli.run("--url", "https://example.com", "tasks", "--token", token)
    assert result.rc == 2 and token not in result.err


@pytest.mark.parametrize("content", ["", "   \n", "two words\n", "tok\x00en", "caf\u00e9"])
def test_bad_token_files_are_rejected_without_echo(cli, tmp_path, content):
    path = tmp_path / "bad.token"
    path.write_text(content, encoding="utf-8")
    result = cli.run("--url", "https://example.com", "--token-file", path, "tasks")
    assert result.rc == 2 and "invalid token file" in result.err
    assert "words" not in result.err
    assert cli.run("--url", "https://example.com", "--token-file", tmp_path / "missing", "tasks").rc == 2


@pytest.mark.parametrize("pin", ["not base64!", base64.b64encode(b"short").decode(),
                                 base64.b64encode(b"x" * 33).decode()])
def test_malformed_pin_is_a_usage_error(cli, plain_token, pin):
    result = cli.run("--url", "https://example.com", "--token-file", plain_token, "--pin-sha256", pin, "tasks")
    assert result.rc == 2 and "--pin-sha256" in result.err


def test_pin_requires_https(cli, plain_token):
    pin = base64.b64encode(b"x" * 32).decode()
    result = cli.run("--url", "http://127.0.0.1:9", "--token-file", plain_token, "--pin-sha256", pin, "tasks")
    assert result.rc == 2 and "https" in result.err


@pytest.mark.parametrize("args", [
    ["submit", "--key", "k"],
    ["submit", "--key", "k", "--prompt", "p", "--prompt-file", "f"],
    ["submit", "--prompt", "p"],
    ["submit", "--key", "k", "--prompt", "p", "--scope", "file=x"],
    ["submit", "--key", "k", "--prompt", "p", "--scope", "path"],
    ["submit", "--key", "k", "--prompt", "p", "--depends-on", "not-a-uuid"],
    ["claim"], ["claim", "--task", "nope"], ["renew", "--task", TASK_A], ["result", "--task", TASK_A, "--epoch", "1"],
    ["gate", "--task", TASK_A, "--epoch", "1", "--decision", "maybe"],
    ["comment", "--page", PAGE], ["audit"], ["task"], ["task", TASK_A, "--task", TASK_B],
    ["call", "bad/op"], ["call", "x", "--json", "[]"], ["call", "x", "--json", "{nope"],
    ["wait", "--after-seq", "x"], ["watch"], ["watch", "--agent", "../x"], ["watch", "--agent", "a", "--timeout", "0"],
    ["claim", "--task", TASK_A, "--operation-id", "nope"],
    ["bogus"],
])
def test_invalid_invocations_exit_2(cli, plain_token, args):
    result = cli.run("--url", "https://example.com", "--token-file", plain_token, *args)
    assert result.rc == 2 and result.out == ""


def test_connection_options_work_before_or_after_the_subcommand(cli, plain_token):
    for args in (["--url", "http://example.com", "--token-file", plain_token, "tasks"],
                 ["tasks", "--url", "http://example.com", "--token-file", plain_token]):
        assert "refusing plain http://" in cli.run(*args).err


def test_connection_refused_is_a_network_error(cli, plain_token):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    result = cli.run("--url", "http://127.0.0.1:%d" % port, "--token-file", plain_token, "tasks")
    assert result.rc == 3 and result.out == ""
    assert result.err == "baton_cli: network error: connection failed\n"


def test_state_file_must_hold_an_integer(cli, plain_token, tmp_path):
    state = tmp_path / "state.seq"
    state.write_text("not a number\n")
    result = cli.run("--url", "https://example.com", "--token-file", plain_token, "watch", "--state", state)
    assert result.rc == 2 and "state file" in result.err


# --- TLS, CA and SPKI pinning against the real bridge ------------------------------------------

def test_request_carries_bearer_token_over_verified_tls(cli, bridge):
    token = bridge.tokens["owner"].read_text().strip()
    assert run_json(cli, bridge, "owner", "tasks") == []
    record, body = last_request(bridge)
    assert (record["method"], record["path"]) == ("POST", "/v1/space/list_tasks")
    assert record["headers"]["authorization"] == "Bearer " + token
    assert record["headers"]["content-type"] == "application/json"
    assert body == {}


def test_tls_verification_is_always_on(cli, bridge):
    before = len(bridge.requests)
    unknown_ca = run_as(cli, bridge, "owner", "tasks", ca=False)
    wrong_ca = cli.run("--url", bridge.https, "--token-file", bridge.tokens["owner"],
                       "--ca-file", bridge.pki["other_ca"], "tasks")
    assert unknown_ca.rc == 3 and wrong_ca.rc == 3
    assert unknown_ca.err == "baton_cli: network error: tls: certificate verification failed\n"
    assert unknown_ca.out == "" and wrong_ca.out == ""
    assert len(bridge.requests) == before, "no request (and no token) may reach an unverified server"


def test_pin_does_not_replace_certificate_verification(cli, bridge):
    result = run_as(cli, bridge, "owner", "tasks", ca=False, pin=bridge.pki["pin"])
    assert result.rc == 3 and bridge.requests == []


def test_hostname_must_match_the_certificate(cli, bridge):
    result = run_as(cli, bridge, "owner", "tasks", url="https://localhost:%d" % bridge.port)
    assert result.rc == 3 and bridge.requests == []


def test_ca_file_can_come_from_the_environment(cli, bridge):
    result = cli.run("--url", bridge.https, "--token-file", bridge.tokens["owner"], "tasks",
                     env={"BATON_CA_FILE": str(bridge.pki["ca"])})
    assert result.rc == 0 and result.json() == []


def test_unreadable_ca_file_is_a_usage_error(cli, bridge, tmp_path):
    junk = tmp_path / "junk.pem"
    junk.write_text("not a certificate\n")
    for ca in (junk, tmp_path / "missing.pem"):
        result = cli.run("--url", bridge.https, "--token-file", bridge.tokens["owner"], "--ca-file", ca, "tasks")
        assert result.rc == 2 and "--ca-file" in result.err


def test_spki_pin_accepts_the_matching_leaf_key(cli, bridge):
    assert run_json(cli, bridge, "owner", "tasks", pin=bridge.pki["pin"]) == []
    assert run_json(cli, bridge, "owner", "tasks", pin="sha256//" + bridge.pki["pin"]) == []
    assert bridge.requests[-1]["headers"]["authorization"].startswith("Bearer ")


@pytest.mark.parametrize("which", ["random", "ca_key"])
def test_spki_pin_refuses_other_keys_before_sending_the_token(cli, bridge, which):
    pin = base64.b64encode(os.urandom(32)).decode() if which == "random" else bridge.pki["ca_pin"]
    result = run_as(cli, bridge, "owner", "tasks", pin=pin)
    assert result.rc == 3 and result.out == ""
    assert result.err == "baton_cli: network error: tls: server public key does not match --pin-sha256\n"
    assert bridge.requests == [], "the pin must be checked before any request is sent"


def test_plain_http_is_allowed_for_loopback_only(cli, bridge):
    token = bridge.tokens["owner"].read_text().strip()
    for url in (bridge.http, bridge.http.replace("127.0.0.1", "localhost")):
        assert run_json(cli, bridge, "owner", "tasks", url=url, ca=False) == []
        assert bridge.requests[-1]["headers"]["authorization"] == "Bearer " + token
    refused = run_as(cli, bridge, "owner", "tasks", url=bridge.http.replace("127.0.0.1", "example.com"), ca=False)
    assert refused.rc == 2 and len(bridge.requests) == 2


# --- Errors ------------------------------------------------------------------------------------

def test_baton_errors_show_only_stable_code_and_status(cli, bridge, tmp_path):
    cli.secrets.append("synthetic-wrong-token")
    wrong = token_file(tmp_path / "wrong", "x", "synthetic-wrong-token")
    result = cli.run("--url", bridge.https, "--ca-file", bridge.pki["ca"], "--token-file", wrong, "tasks")
    assert (result.rc, result.out) == (1, "")
    assert result.err == "baton_cli: error: unauthorized (HTTP 401)\n"
    missing = run_as(cli, bridge, "owner", "task", TASK_A)
    assert missing.rc == 1 and re.fullmatch(r"baton_cli: error: [a-z_]+ \(HTTP 4\d\d\)\n", missing.err)
    invalid = run_as(cli, bridge, "owner", "call", "create_space", "--json", '{"name": "x", "bogus": 1}')
    assert invalid.rc == 1 and invalid.err.startswith("baton_cli: error: invalid (HTTP 422)")
    unknown = run_as(cli, bridge, "owner", "call", "no_such_operation", "--json", "{}")
    assert unknown.rc == 1 and unknown.err.startswith("baton_cli: error: invalid (HTTP 422)")


def test_failed_write_names_its_operation_id_so_a_retry_reuses_it(cli, bridge):
    first = run_as(cli, bridge, "owner", "claim", "--task", TASK_A)
    assert first.rc == 1
    _, body = last_request(bridge)
    assert "operation_id=" + body["operation_id"] in first.err
    again = run_as(cli, bridge, "owner", "claim", "--task", TASK_A, "--operation-id", body["operation_id"])
    assert again.rc == 1 and last_request(bridge)[1] == body


# --- Request shapes ----------------------------------------------------------------------------

OP = "44444444-4444-4444-8444-444444444444"

SHAPES = [
    (["inbox"], "fetch_inbox", {}),
    (["inbox", "--limit", "5"], "fetch_inbox", {"limit": 5}),
    (["wait", "--after-seq", "3", "--timeout", "0"], "wait_inbox", {"after_seq": 3, "timeout_seconds": 0}),
    (["wait", "--timeout", "0", "--limit", "9"], "wait_inbox", {"after_seq": 0, "timeout_seconds": 0, "limit": 9}),
    (["task", TASK_A], "get_task", {"task_id": TASK_A}),
    (["task", "--task", TASK_A.upper()], "get_task", {"task_id": TASK_A}),
    (["tasks"], "list_tasks", {}),
    (["tasks", "--limit", "7"], "list_tasks", {"limit": 7}),
    (["ready"], "ready", {}),
    (["ready", "--page", PAGE, "--limit", "7"], "ready", {"page_id": PAGE, "limit": 7}),
    (["audit", "--task", TASK_A, "--limit", "10"], "list_audit", {"task_id": TASK_A, "limit": 10}),
    (["audit", "--space", PAGE], "list_audit", {"space_id": PAGE}),
    (["submit", "--key", "k1", "--prompt", "do it"], "submit_task",
     {"idempotency_key": "k1", "prompt": "do it", "operation_id": OP}),
    (["submit", "--key", "k1", "--prompt", "do it", "--page", PAGE, "--assignee", "beigua", "--scope", "path=src/a",
      "--scope", "migration=0001_x", "--depends-on", TASK_A + "," + TASK_B, "--depends-on", TASK_B,
      "--operation-id", OP], "submit_task",
     {"idempotency_key": "k1", "prompt": "do it", "page_id": PAGE, "assignee_principal": "beigua",
      "scopes": [{"kind": "path", "value": "src/a"}, {"kind": "migration", "value": "0001_x"}],
      "depends_on": [TASK_A, TASK_B, TASK_B], "operation_id": OP}),
    (["claim", "--task", TASK_A], "claim", {"task_id": TASK_A, "operation_id": OP}),
    (["claim", "--task", TASK_A, "--lease", "60"], "claim", {"task_id": TASK_A, "lease_seconds": 60, "operation_id": OP}),
    (["renew", "--task", TASK_A, "--epoch", "2", "--lease", "90"], "renew",
     {"task_id": TASK_A, "epoch": 2, "lease_seconds": 90, "operation_id": OP}),
    (["comment", "--page", PAGE, "--body", "hello"], "comment", {"page_id": PAGE, "body": "hello", "operation_id": OP}),
    (["comment", "--page", PAGE, "--thread", TASK_A, "--body", "h\u00e9llo \u4f60\u597d"], "comment",
     {"page_id": PAGE, "thread_id": TASK_A, "body": "h\u00e9llo \u4f60\u597d", "operation_id": OP}),
    (["result", "--task", TASK_A, "--epoch", "3", "--result", "done"], "submit_result",
     {"task_id": TASK_A, "epoch": 3, "result": "done", "operation_id": OP}),
    (["gate", "--task", TASK_A, "--epoch", "3", "--decision", "pass"], "gate",
     {"task_id": TASK_A, "expected_epoch": 3, "decision": "pass", "operation_id": OP}),
    (["gate", "--task", TASK_A, "--epoch", "3", "--decision", "fail"], "gate",
     {"task_id": TASK_A, "expected_epoch": 3, "decision": "fail", "operation_id": OP}),
    (["call", "list_spaces"], "list_spaces", {}),
    (["call", "create_space", "--json", '{"name": "Project"}', "--operation-id", OP], "create_space",
     {"name": "Project", "operation_id": OP}),
    (["call", "x_audit", "--json", '{"task_id": "%s", "limit": 3}' % TASK_A], "x_audit",
     {"task_id": TASK_A, "limit": 3}),
]


@pytest.mark.parametrize("args,operation,expected", SHAPES, ids=[" ".join(s[0][:2]) + str(i) for i, s in enumerate(SHAPES)])
def test_subcommand_request_shape(cli, bridge, args, operation, expected):
    if "--operation-id" not in args and "operation_id" in expected:
        args = [*args, "--operation-id", OP]
    result = run_as(cli, bridge, "owner", *args)
    assert result.rc in (0, 1), result.err
    record, body = last_request(bridge)
    assert (record["method"], record["path"]) == ("POST", "/v1/space/" + operation)
    assert body == expected


@pytest.mark.parametrize("args,operation_key", [
    (["submit", "--key", "k", "--prompt", "p"], "submit_task"), (["claim", "--task", TASK_A], "claim"),
    (["renew", "--task", TASK_A, "--epoch", "1"], "renew"), (["comment", "--page", PAGE, "--body", "b"], "comment"),
    (["result", "--task", TASK_A, "--epoch", "1", "--result", "r"], "submit_result"),
    (["gate", "--task", TASK_A, "--epoch", "1", "--decision", "pass"], "gate"),
])
def test_writes_get_a_fresh_uuid4_operation_id_each_time(cli, bridge, args, operation_key):
    seen = set()
    for _ in range(2):
        run_as(cli, bridge, "owner", *args)
        record, body = last_request(bridge)
        assert record["path"] == "/v1/space/" + operation_key
        parsed = uuid.UUID(body["operation_id"])
        assert parsed.version == 4 and str(parsed) == body["operation_id"]
        seen.add(body["operation_id"])
    assert len(seen) == 2


@pytest.mark.parametrize("args", [["inbox"], ["tasks"], ["ready"], ["task", TASK_A], ["audit", "--task", TASK_A],
                                  ["wait", "--timeout", "0"]])
def test_reads_never_send_an_operation_id(cli, bridge, args):
    run_as(cli, bridge, "owner", *args)
    assert "operation_id" not in last_request(bridge)[1]


def test_text_can_come_from_files_and_stdin(cli, bridge, tmp_path):
    prompt = tmp_path / "prompt.md"
    prompt.write_text("line one\nline two \u4f60\u597d\n", encoding="utf-8")
    run_as(cli, bridge, "owner", "submit", "--key", "k", "--prompt-file", prompt)
    assert last_request(bridge)[1]["prompt"] == "line one\nline two \u4f60\u597d\n"
    run_as(cli, bridge, "owner", "comment", "--page", PAGE, "--body-file", "-", stdin="from stdin\n")
    assert last_request(bridge)[1]["body"] == "from stdin\n"
    run_as(cli, bridge, "worker", "result", "--task", TASK_A, "--epoch", "1", "--result-file", prompt)
    assert last_request(bridge)[1]["result"] == prompt.read_text(encoding="utf-8")
    run_as(cli, bridge, "owner", "call", "list_spaces", "--json-file", "-", stdin="{}")
    assert last_request(bridge)[1] == {}
    missing = run_as(cli, bridge, "owner", "submit", "--key", "k", "--prompt-file", tmp_path / "nope")
    assert missing.rc == 2


# --- Named-agent and reviewer flows, watch -----------------------------------------------------

class Watcher:
    def __init__(self, cli, bridge, label, *extra):
        self.lines = queue.Queue()
        self.process = subprocess.Popen(
            [sys.executable, str(CLI), "--url", bridge.https, "--ca-file", str(bridge.pki["ca"]),
             "--token-file", str(bridge.tokens[label]), "watch", *map(str, extra)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=clean_env(cli.home))
        self.cli = cli
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        for line in self.process.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def next(self, timeout=20):
        line = self.lines.get(timeout=timeout)
        assert line is not None, "watch exited early: " + self.process.stderr.read()
        return json.loads(line)

    def interrupt(self):
        self.process.send_signal(signal.SIGINT)
        rc = self.process.wait(timeout=20)
        err = self.process.stderr.read()
        self.cli.guard(err)
        return rc, err


@pytest.fixture
def workspace(cli, bridge):
    space = run_json(cli, bridge, "owner", "call", "create_space", "--json", '{"name": "CLI team"}',
                     "--operation-id", str(uuid.uuid4()))
    page = run_json(cli, bridge, "owner", "call", "create_page", "--operation-id", str(uuid.uuid4()),
                    "--json", json.dumps({"space_id": space["id"], "title": "Work"}))
    for label in ("worker", "reviewer"):
        run_json(cli, bridge, "owner", "call", "share", "--operation-id", str(uuid.uuid4()), "--json",
                 json.dumps({"resource_type": "space", "resource_id": space["id"], "subject_type": "person",
                             "subject_id": bridge.identities[label]["account"], "role": "edit"}))
    return space, page


def test_watch_persists_next_seq_and_resumes_without_replay(cli, bridge, workspace, tmp_path):
    _, page = workspace
    worker = bridge.identities["worker"]["agent"]
    state = tmp_path / "state" / "worker.seq"

    first = Watcher(cli, bridge, "worker", "--state", state, "--timeout", "2")
    task = run_json(cli, bridge, "owner", "submit", "--key", "watch-" + uuid.uuid4().hex, "--prompt", "Do the thing",
                    "--page", page["id"], "--assignee", worker, "--scope", "path=src/watch")
    note = first.next()
    assert note["event"] == "task.assigned" and note["object_id"] == task["task_id"]
    assert note["recipient"] == worker and note["body_preview"] == "Do the thing"
    rc, err = first.interrupt()
    assert rc == 0 and err.splitlines() == [
        "notification seq=%d task.assigned task=%s from=%s(human) - [assigned] Do the thing"
        % (note["seq"], task["task_id"], bridge.identities["owner"]["agent"])]
    assert state.read_text().strip() == str(note["seq"])
    assert stat.S_IMODE(state.stat().st_mode) == 0o600
    assert stat.S_IMODE(state.parent.stat().st_mode) == 0o700

    run_json(cli, bridge, "owner", "comment", "--page", page["id"], "--thread", task["task_id"], "--body", "Second event")
    second = Watcher(cli, bridge, "worker", "--state", state, "--timeout", "2")
    again = second.next()
    assert again["event"] == "comment.created" and again["seq"] > note["seq"]
    assert again["subject"] == "comment on " + task["task_id"] and again["body_preview"] == "Second event"
    rc, err = second.interrupt()
    assert rc == 0 and err.count("\n") == 1 and "comment.created" in err and "seq=%d " % again["seq"] in err
    assert state.read_text().strip() == str(again["seq"])


def test_watch_uses_the_default_state_path_and_tightens_its_mode(cli, bridge, workspace):
    default = cli.home / ".local" / "state" / "baton" / "beigua.seq"
    default.parent.mkdir(parents=True)
    default.write_text("0\n")
    default.chmod(0o644)
    watcher = Watcher(cli, bridge, "worker", "--agent", "beigua", "--timeout", "1")
    deadline = time.monotonic() + 10
    while stat.S_IMODE(default.stat().st_mode) != 0o600:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    assert watcher.interrupt() == (0, "")
    assert default.read_text().strip() == "0"


def test_watch_error_exits_1_with_only_the_code(cli, bridge, tmp_path):
    state = tmp_path / "s.seq"
    wrong = token_file(tmp_path / "w", "x", "synthetic-unknown-token")
    cli.secrets.append("synthetic-unknown-token")
    result = cli.run("--url", bridge.https, "--ca-file", bridge.pki["ca"], "--token-file", wrong,
                     "watch", "--state", state)
    assert result.rc == 1 and result.err == "baton_cli: error: unauthorized (HTTP 401)\n"
    assert state.read_text() == "0\n"


def test_named_agent_flow_wait_claim_comment_result_then_reviewer_gate(cli, bridge, workspace):
    _, page = workspace
    worker = bridge.identities["worker"]["agent"]
    key = "flow-" + uuid.uuid4().hex

    task = run_json(cli, bridge, "owner", "submit", "--key", key, "--prompt", "Write the summary", "--page",
                    page["id"], "--assignee", worker, "--scope", "path=docs/summary")
    task_id = task["task_id"]
    assert task["state"] == "queued" and task["assignee"] == worker

    waited = run_json(cli, bridge, "worker", "wait", "--after-seq", "0", "--timeout", "5")
    event = next(n for n in waited["notifications"] if n["object_id"] == task_id)
    assert event["event"] == "task.assigned" and waited["next_seq"] >= event["seq"]
    assert run_json(cli, bridge, "worker", "wait", "--after-seq", str(waited["next_seq"]), "--timeout", "0") == {
        "notifications": [], "next_seq": waited["next_seq"]}
    inbox = run_json(cli, bridge, "worker", "inbox")
    assert any(message["thread_id"] == task_id for message in inbox["messages"])
    assert [t["task_id"] for t in run_json(cli, bridge, "worker", "ready", "--page", page["id"])] == [task_id]
    assert run_json(cli, bridge, "worker", "task", task_id)["prompt"] == "Write the summary"

    claim = run_json(cli, bridge, "worker", "claim", "--task", task_id, "--lease", "120")
    epoch = claim["epoch"]
    assert claim["state"] == "claimed" and claim["holder"] == worker
    run_json(cli, bridge, "worker", "renew", "--task", task_id, "--epoch", str(epoch), "--lease", "180")
    comment = run_json(cli, bridge, "worker", "comment", "--page", page["id"], "--thread", task_id,
                       "--body", "Started; draft in progress")
    assert comment["thread_id"] == task_id
    candidate = run_json(cli, bridge, "worker", "result", "--task", task_id, "--epoch", str(epoch),
                         "--result", "Summary written")
    assert candidate["state"] == "awaiting_gate"
    view = run_json(cli, bridge, "owner", "task", task_id)
    assert view["result_epoch"] == epoch and view["page_id"] == page["id"] and view["result"] == "Summary written"

    self_review = run_as(cli, bridge, "worker", "gate", "--task", task_id, "--epoch", str(epoch), "--decision", "pass")
    assert self_review.rc == 1 and "forbidden (HTTP 403)" in self_review.err
    stale = run_as(cli, bridge, "owner", "gate", "--task", task_id, "--epoch", str(epoch + 1), "--decision", "pass")
    assert stale.rc == 1 and stale.err.startswith("baton_cli: error: stale_claim (HTTP 409)")
    decided = run_json(cli, bridge, "owner", "gate", "--task", task_id, "--epoch", str(epoch), "--decision", "pass")
    assert decided["state"] == "done"

    assert run_json(cli, bridge, "owner", "task", "--task", task_id)["state"] == "done"
    assert task_id in [t["task_id"] for t in run_json(cli, bridge, "owner", "tasks")]
    audit = run_json(cli, bridge, "owner", "audit", "--task", task_id)
    operations = [row["event"] for row in audit]
    for expected in ("task.assigned", "task.claimed", "task.result_submitted", "gate.reviewed"):
        assert expected in operations
    notified = run_json(cli, bridge, "owner", "wait", "--timeout", "0")
    assert "gate.decided" not in [n["event"] for n in notified["notifications"]], "actors are not notified of themselves"
    worker_view = run_json(cli, bridge, "worker", "wait", "--timeout", "0")
    assert "gate.decided" in [n["event"] for n in worker_view["notifications"]]


def test_failing_gate_and_idempotent_retry_with_the_same_operation_id(cli, bridge, workspace):
    _, page = workspace
    task = run_json(cli, bridge, "owner", "submit", "--key", "retry-" + uuid.uuid4().hex, "--prompt", "Retry me",
                    "--page", page["id"], "--assignee", bridge.identities["worker"]["agent"],
                    "--scope", "path=src/retry")
    operation_id = str(uuid.uuid4())
    claims = [run_json(cli, bridge, "worker", "claim", "--task", task["task_id"], "--operation-id", operation_id)
              for _ in range(2)]
    assert claims[0]["epoch"] == claims[1]["epoch"]
    epoch = claims[0]["epoch"]
    run_json(cli, bridge, "worker", "result", "--task", task["task_id"], "--epoch", str(epoch), "--result", "Bad")
    failed = run_json(cli, bridge, "owner", "gate", "--task", task["task_id"], "--epoch", str(epoch),
                      "--decision", "fail")
    assert failed["state"] != "done"


# --- UTF-8 output, subcommand registration, --summary, --scope none ---------------------------

CHINESE = "\u5ba1\u9605\u6587\u6863\uff1a\u4f60\u597d\u4e16\u754c"
SUMMARY_LINE = re.compile(r"task ([0-9a-f-]{36}) state=([a-z_]+) epoch=(\d+)\n")


def submit_manual(cli, bridge, page, prompt, *extra, assignee="worker", key=None):
    return run_json(cli, bridge, "owner", "submit", "--key", key or "cli-" + uuid.uuid4().hex, "--prompt", prompt,
                    "--page", page["id"], "--assignee", bridge.identities[assignee]["agent"], *extra)


def test_json_output_keeps_chinese_text_unescaped_and_valid_utf8(cli, bridge, workspace, tmp_path):
    _, page = workspace
    result = run_as(cli, bridge, "owner", "submit", "--key", "utf8-" + uuid.uuid4().hex, "--prompt", CHINESE,
                    "--page", page["id"], "--assignee", bridge.identities["worker"]["agent"], "--scope", "none")
    assert result.rc == 0, result.err
    assert CHINESE in result.out and "\\u" not in result.out
    assert result.json()["prompt"] == CHINESE

    # The bytes are UTF-8 whatever the locale says.
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(cli.home), "LANG": "C", "LC_ALL": "C"}
    process = subprocess.run([sys.executable, str(CLI), "--url", bridge.https, "--ca-file", str(bridge.pki["ca"]),
                              "--token-file", str(bridge.tokens["worker"]), "inbox"],
                             capture_output=True, env=env, timeout=90)
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout.decode("utf-8", errors="strict")) is not None
    task_id = json.loads(result.out)["task_id"]
    process = subprocess.run([sys.executable, str(CLI), "--url", bridge.https, "--ca-file", str(bridge.pki["ca"]),
                              "--token-file", str(bridge.tokens["worker"]), "task", task_id],
                             capture_output=True, env=env, timeout=90)
    assert process.returncode == 0, process.stderr
    assert CHINESE.encode("utf-8") in process.stdout and b"\\u" not in process.stdout
    assert json.loads(process.stdout.decode("utf-8"))["prompt"] == CHINESE


def test_dump_json_is_unescaped_utf8_with_an_ascii_fallback_for_lone_surrogates():
    module = load_cli_module()
    value = {"text": CHINESE, "emoji": "\U0001f600", "n": [1, 2]}
    for options in ({"indent": 2}, {"separators": (",", ":")}):
        text = module.dump_json(value, **options)
        assert CHINESE in text and "\U0001f600" in text and "\\u" not in text
        assert json.loads(text) == value
        text.encode("utf-8")
    lone = module.dump_json({"bad": json.loads('"\\ud800"')}, separators=(",", ":"))
    lone.encode("utf-8")
    assert lone == '{"bad":"\\ud800"}'


def test_help_lists_watch_and_every_subcommand(cli):
    module = load_cli_module()
    parser = module.build_parser()
    registered = next(action for action in parser._actions if getattr(action, "choices", None)).choices
    assert {"watch", "wait", "claim", "renew", "result", "gate", "submit"} <= set(registered)
    top = cli.run("--help")
    assert top.rc == 0 and re.search(r"^\s+watch\s+\S", top.out, re.M)
    sub = cli.run("watch", "--help")
    assert sub.rc == 0 and "--state" in sub.out and "--agent" in sub.out


def test_summary_line_goes_to_stderr_while_stdout_stays_pure_json(cli, bridge, workspace):
    _, page = workspace
    task = submit_manual(cli, bridge, page, "Summarize", "--scope", "none")
    task_id = task["task_id"]
    quiet = run_as(cli, bridge, "owner", "task", task_id)
    assert quiet.err == ""

    claim = run_as(cli, bridge, "worker", "claim", "--task", task_id)
    assert claim.rc == 0
    assert claim.err == "task %s state=claimed epoch=%d\n" % (task_id, claim.json()["epoch"])
    assert claim.json()["task_id"] == task_id and claim.out.lstrip().startswith("{")
    epoch = claim.json()["epoch"]

    renewed = run_as(cli, bridge, "worker", "renew", "--task", task_id, "--epoch", str(epoch))
    assert renewed.rc == 0 and renewed.err == "task %s state=claimed epoch=%d\n" % (task_id, epoch)
    json.loads(renewed.out)

    submitted = run_as(cli, bridge, "worker", "result", "--task", task_id, "--epoch", str(epoch), "--result", "ok")
    assert submitted.rc == 0 and submitted.err == "task %s state=awaiting_gate epoch=%d\n" % (task_id, epoch)
    json.loads(submitted.out)

    decided = run_as(cli, bridge, "owner", "gate", "--task", task_id, "--epoch", str(epoch), "--decision", "pass")
    owner = bridge.identities["owner"]["agent"]
    assert decided.rc == 0 and decided.err == "task %s state=done epoch=%d gate=pass approver=%s authority=root\n" % (task_id, epoch, owner)
    assert decided.json()["state"] == "done"


def test_summary_can_be_turned_off_or_on_per_command(cli, bridge, workspace):
    _, page = workspace
    task = submit_manual(cli, bridge, page, "Toggle", "--scope", "none")
    task_id = task["task_id"]
    silent = run_as(cli, bridge, "worker", "claim", "--task", task_id, "--no-summary")
    assert silent.rc == 0 and silent.err == "" and silent.json()["state"] == "claimed"
    epoch = silent.json()["epoch"]
    assert run_as(cli, bridge, "worker", "renew", "--task", task_id, "--epoch", str(epoch), "--summary").err.startswith("task ")
    explicit = run_as(cli, bridge, "worker", "task", task_id, "--summary")
    assert explicit.err == "task %s state=claimed epoch=%d\n" % (task_id, epoch)
    no_summary_on_submit = run_as(cli, bridge, "owner", "submit", "--key", "k-" + uuid.uuid4().hex, "--prompt", "x",
                                  "--page", page["id"], "--assignee", bridge.identities["reviewer"]["agent"],
                                  "--scope", "none")
    assert no_summary_on_submit.err == ""
    for args in (["claim", "--task", task_id], ["gate", "--task", task_id, "--epoch", "1", "--decision", "pass"]):
        assert "--summary" in cli.run(*args, "--help").out


def test_summary_is_not_printed_when_the_call_fails_or_has_no_task(cli, bridge, workspace):
    _, page = workspace
    task = submit_manual(cli, bridge, page, "Fail", "--scope", "none")
    failed = run_as(cli, bridge, "reviewer", "claim", "--task", task["task_id"])
    assert failed.rc == 1 and failed.out == ""
    assert "task " not in failed.err.replace("operation_id", "")
    listed = run_as(cli, bridge, "owner", "call", "list_spaces", "--summary")
    assert listed.rc == 0 and listed.err == ""


def test_summary_only_echoes_well_formed_fields(capsys):
    module = load_cli_module()
    module.print_summary({"task_id": TASK_A, "state": "claimed", "epoch": 4})
    module.print_summary({"task_id": TASK_A, "state": "claimed"})
    module.print_summary({"task_id": "x\x1b[31m", "state": "claimed", "epoch": 1})
    module.print_summary({"task_id": TASK_A, "state": "bad state\n", "epoch": 1})
    module.print_summary([TASK_A])
    module.print_summary({"task_id": TASK_A, "state": "done", "epoch": True})
    assert capsys.readouterr().err == ("task %s state=claimed epoch=4\n" % TASK_A + "task %s state=claimed\n" % TASK_A
                                       + "task %s state=done\n" % TASK_A)


def test_scope_none_is_accepted_alone_and_refused_with_other_scopes(cli, bridge, workspace):
    _, page = workspace
    run_as(cli, bridge, "owner", "submit", "--key", "none-shape", "--prompt", "p", "--scope", "none",
           "--assignee", bridge.identities["worker"]["agent"], "--page", page["id"], "--operation-id", OP)
    assert last_request(bridge)[1]["scopes"] == [{"kind": "none"}]
    requests_before = len(bridge.requests)
    for args in (["--scope", "none", "--scope", "path=src"], ["--scope", "path=src", "--scope", "none"],
                 ["--scope", "none=x"], ["--scope", "NONE"]):
        result = run_as(cli, bridge, "owner", "submit", "--key", "k", "--prompt", "p", *args)
        assert result.rc == 2 and result.out == ""
    assert len(bridge.requests) == requests_before


def test_two_footprintless_tasks_can_be_claimed_at_once_through_the_cli(cli, bridge, workspace):
    _, page = workspace
    worker = bridge.identities["worker"]["agent"]
    first = run_json(cli, bridge, "owner", "submit", "--key", "np-" + uuid.uuid4().hex, "--prompt", "Review one",
                     "--page", page["id"], "--assignee", worker)
    second = run_json(cli, bridge, "owner", "submit", "--key", "np-" + uuid.uuid4().hex, "--prompt", "Review two",
                      "--page", page["id"], "--assignee", worker)
    assert first["scopes"] == second["scopes"] == [{"kind": "none"}]
    assert run_json(cli, bridge, "worker", "claim", "--task", first["task_id"])["state"] == "claimed"
    assert run_json(cli, bridge, "worker", "claim", "--task", second["task_id"])["state"] == "claimed"


# --- Dogfooding batch 1: comments --task, block, field details, resilient watch ----------------

def test_comments_task_reads_a_tasks_thread_and_rejects_ambiguous_flags(cli, bridge, workspace):
    _, page = workspace
    worker = bridge.identities["worker"]["agent"]
    task = run_json(cli, bridge, "owner", "submit", "--key", "df2-" + uuid.uuid4().hex, "--prompt", "Thread probe",
                    "--page", page["id"], "--assignee", worker)
    run_json(cli, bridge, "owner", "comment", "--page", page["id"], "--thread", task["task_id"], "--body", "Hello thread")
    by_task = run_json(cli, bridge, "worker", "comments", "--task", task["task_id"])
    assert last_request(bridge)[1] == {"task_id": task["task_id"]}
    by_thread = run_json(cli, bridge, "worker", "comments", "--page", page["id"], "--thread", task["task_id"])
    assert last_request(bridge)[1] == {"page_id": page["id"], "thread_id": task["task_id"]}
    assert by_task == by_thread and "Hello thread" in json.dumps(by_task)
    assert run_as(cli, bridge, "worker", "comments", "--task", task["task_id"], "--page", page["id"]).rc == 2
    assert run_as(cli, bridge, "worker", "comments", "--task", task["task_id"], "--thread", task["task_id"]).rc == 2
    assert run_as(cli, bridge, "worker", "comments").rc == 2
    assert run_as(cli, bridge, "worker", "comments", "--task", "nope").rc == 2
    hidden = run_as(cli, bridge, "owner", "comments", "--task", str(uuid.uuid4()))
    assert hidden.rc == 1 and hidden.err == "baton_cli: error: not_found (HTTP 404)\n"


def test_block_marks_a_task_blocked_with_a_reason_and_note(cli, bridge, workspace, tmp_path):
    _, page = workspace
    worker = bridge.identities["worker"]["agent"]
    task = run_json(cli, bridge, "owner", "submit", "--key", "df7-" + uuid.uuid4().hex, "--prompt", "Block probe",
                    "--page", page["id"], "--assignee", worker)
    operation = str(uuid.uuid4())
    result = run_as(cli, bridge, "worker", "block", "--task", task["task_id"], "--reason", "scope_conflict",
                    "--note", "src is claimed", "--operation-id", operation)
    assert result.rc == 0, result.err
    assert last_request(bridge)[1] == {"task_id": task["task_id"], "reason_code": "scope_conflict",
                                       "note": "src is claimed", "operation_id": operation}
    assert result.json()["blocked"]["reason_code"] == "scope_conflict"
    assert result.err == "task %s state=queued epoch=0\n" % task["task_id"]
    note_file = tmp_path / "note.txt"
    note_file.write_text("from a file\n", encoding="utf-8")
    second = run_as(cli, bridge, "worker", "block", "--task", task["task_id"], "--reason", "waiting_input", "--note-file", note_file)
    assert second.rc == 0 and last_request(bridge)[1]["note"] == "from a file\n"
    bare = run_as(cli, bridge, "worker", "block", "--task", task["task_id"], "--reason", "other", "--no-summary")
    assert bare.rc == 0 and "note" not in last_request(bridge)[1] and bare.err == ""
    thread = run_json(cli, bridge, "owner", "comments", "--task", task["task_id"])
    assert [item["body"] for item in thread if item["body"].startswith("[blocked]")] == [
        "[blocked] scope_conflict: src is claimed", "[blocked] waiting_input: from a file\n", "[blocked] other"]
    assert run_as(cli, bridge, "worker", "block", "--task", task["task_id"], "--reason", "nonsense").rc == 2
    assert run_as(cli, bridge, "worker", "block", "--task", task["task_id"]).rc == 2
    refused = run_as(cli, bridge, "reviewer", "block", "--task", task["task_id"], "--reason", "other")
    assert refused.rc == 1 and "forbidden (HTTP 403)" in refused.err


def test_validation_details_are_printed_with_only_plain_text(cli, bridge):
    result = run_as(cli, bridge, "owner", "call", "create_space", "--json", '{"name": 5, "bogus": "synthetic-input-value"}',
                    "--operation-id", str(uuid.uuid4()))
    assert result.rc == 1
    assert result.err.splitlines()[0] == "baton_cli: error: invalid (HTTP 422)"
    assert "baton_cli:   name: expected string" in result.err
    assert "baton_cli:   bogus: expected not a field of this operation; see GET /v1/space/schema" in result.err
    assert "synthetic-input-value" not in result.err and "operation_id=" in result.err
    module = load_cli_module()
    assert module.well_formed_details([{"field": "ok", "expected": "fine"}, {"field": "a\nb", "expected": "x"},
                                       {"field": "ok", "expected": "bad\x1b[31m"}, "junk", {"field": 1}]) == [("ok", "fine")]
    assert module.well_formed_details("junk") == [] and len(module.well_formed_details([{"field": "f", "expected": "e"}] * 99)) == 20


def test_backoff_doubles_up_to_sixty_seconds_and_only_transient_errors_retry():
    module = load_cli_module()
    assert [module.backoff_delay(n) for n in range(1, 9)] == [1, 2, 4, 8, 16, 32, 60, 60]
    assert module.backoff_delay(10_000) == 60
    assert module.transient(module.NetworkError("timeout")) and module.transient(module.NetworkError("tls: x"))
    for status in (500, 502, 503, 504, 429):
        assert module.transient(module.BatonError("storage_unavailable", status))
    for status in (400, 401, 403, 404, 409, 422, 200):
        assert not module.transient(module.BatonError("x", status))


class Scripted:
    """Plain loopback HTTP server that answers wait_inbox from a script: ('status', n), ('drop',) or ('json', obj)."""

    def __init__(self, script):
        import http.server
        outer = self
        self.script, self.seen, self.hold = list(script), [], threading.Event()

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                outer.seen.append(json.loads(self.rfile.read(length)))
                step = outer.script.pop(0) if outer.script else ("hold",)
                if step[0] == "hold":
                    outer.hold.wait(30)
                    return
                if step[0] == "drop":
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                status, body = (step[1], {"x_protocol": "baton.space.v1", "error": {"code": "storage_unavailable", "message": "x"}}) \
                    if step[0] == "status" else (200, {"x_protocol": "baton.space.v1", "result": step[1]})
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.hold.set()
        self.server.shutdown()
        self.server.server_close()


def start_watch(cli, tmp_path, scripted, *extra):
    token = "synthetic-" + uuid.uuid4().hex
    cli.secrets.append(token)
    process = subprocess.Popen(
        [sys.executable, str(CLI), "--url", scripted.url, "--token-file", str(token_file(tmp_path / "wt", "w", token)),
         "watch", "--state", str(tmp_path / "state.seq"), "--timeout", "1", *map(str, extra)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=clean_env(cli.home))
    return process


def stop_watch(cli, process):
    process.send_signal(signal.SIGTERM)
    out, err = process.communicate(timeout=20)
    cli.guard(out, err)
    return process.returncode, out, err


NOTE = {"id": "44444444-4444-4444-8444-444444444444", "seq": 5, "event": "task.assigned", "object_type": "task",
        "object_id": TASK_A, "thread_id": TASK_A, "created_at": "2026-01-01T00:00:00+00:00", "recipient": "worker",
        "actor": {"principal": "nangua", "kind": "human"}, "subject": "[assigned] Synthetic", "body_preview": "Synthetic"}


def test_watch_reconnects_with_backoff_after_5xx_and_network_drops_then_keeps_running(cli, tmp_path):
    scripted = Scripted([("status", 503), ("drop",), ("json", {"notifications": [NOTE], "next_seq": 5}),
                         ("json", {"notifications": [], "next_seq": 5})])
    started = time.monotonic()
    process = start_watch(cli, tmp_path, scripted)
    try:
        line = process.stdout.readline()
        assert time.monotonic() - started >= 3, "one second, then two seconds of backoff"
        assert json.loads(line) == NOTE
        deadline = time.monotonic() + 10
        while len(scripted.seen) < 5 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert len(scripted.seen) >= 5, "the watch keeps polling after recovering"
        assert [call["after_seq"] for call in scripted.seen[:3]] == [0, 0, 0] and scripted.seen[3]["after_seq"] == 5
        assert process.poll() is None
    finally:
        rc, out, err = stop_watch(cli, process)
        scripted.close()
    assert rc == 0 and out == ""
    lines = err.splitlines()
    assert lines[0] == "baton_cli: watch: storage_unavailable (HTTP 503); reconnecting in 1s"
    assert lines[1] == "baton_cli: watch: connection failed; reconnecting in 2s"
    assert lines[2] == ("notification seq=5 task.assigned task=%s from=nangua(human) - [assigned] Synthetic" % TASK_A)
    assert (tmp_path / "state.seq").read_text() == "5\n"


def test_watch_stops_on_errors_that_retrying_cannot_fix(cli, tmp_path):
    scripted = Scripted([])
    scripted.script.append(("status", 401))
    scripted.server.RequestHandlerClass  # the scripted 401 uses the same error body shape
    process = start_watch(cli, tmp_path, scripted)
    try:
        out, err = process.communicate(timeout=20)
    finally:
        scripted.close()
    assert process.returncode == 1 and out == "" and err.startswith("baton_cli: error: storage_unavailable (HTTP 401)")


def test_watch_summary_can_be_turned_off_and_stdout_stays_pure_json(cli, tmp_path):
    scripted = Scripted([("json", {"notifications": [NOTE], "next_seq": 5})])
    process = start_watch(cli, tmp_path, scripted, "--no-summary")
    try:
        assert json.loads(process.stdout.readline()) == NOTE
    finally:
        rc, out, err = stop_watch(cli, process)
        scripted.close()
    assert rc == 0 and out == "" and err == ""


def test_watch_exec_runs_once_per_notification_with_json_on_stdin_and_never_interpolates_fields(cli, tmp_path):
    hostile = {**NOTE, "subject": "[assigned] $(touch PWNED1) `touch PWNED2`; touch PWNED3 \x1b[31mred"}
    second = {**NOTE, "id": "55555555-5555-4555-8555-555555555555", "seq": 6}
    scripted = Scripted([("json", {"notifications": [hostile, second], "next_seq": 6})])
    collector = tmp_path / "collect.py"
    collector.write_text("import sys\nopen(sys.argv[1], 'a').write(sys.stdin.read())\n")
    received = tmp_path / "received.jsonl"
    command = "%s %s %s" % (sys.executable, collector, received)
    process = start_watch(cli, tmp_path, scripted, "--exec", command)
    try:
        assert json.loads(process.stdout.readline()) == hostile
        assert json.loads(process.stdout.readline()) == second
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and (not received.exists() or len(received.read_text().splitlines()) < 2):
            time.sleep(0.05)
    finally:
        rc, out, err = stop_watch(cli, process)
        scripted.close()
    assert rc == 0
    assert [json.loads(line) for line in received.read_text().splitlines()] == [hostile, second]
    assert not list(tmp_path.glob("PWNED*")) and not list(Path.cwd().glob("PWNED*"))
    assert "\x1b" not in err and "touch PWNED1" in err, "the terminal summary shows the text but not control characters"


def test_watch_exec_failures_are_reported_and_do_not_stop_the_watch(cli, tmp_path):
    scripted = Scripted([("json", {"notifications": [NOTE], "next_seq": 5})])
    process = start_watch(cli, tmp_path, scripted, "--no-summary", "--exec", "exit 3")
    try:
        assert json.loads(process.stdout.readline()) == NOTE
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not (tmp_path / "state.seq").read_text().startswith("5"):
            time.sleep(0.05)
    finally:
        rc, out, err = stop_watch(cli, process)
        scripted.close()
    assert rc == 0 and err == "baton_cli: watch: --exec command exited with status 3\n"
    assert (tmp_path / "state.seq").read_text() == "5\n"
    slow = Scripted([("json", {"notifications": [NOTE], "next_seq": 5})])
    process = start_watch(cli, tmp_path, slow, "--no-summary", "--exec", "sleep 30", "--exec-timeout", "1")
    try:
        assert json.loads(process.stdout.readline()) == NOTE
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not (tmp_path / "state.seq").read_text().startswith("5"):
            time.sleep(0.05)
    finally:
        rc, out, err = stop_watch(cli, process)
        slow.close()
    assert rc == 0 and "timed out" in err


def test_watch_rejects_a_non_positive_exec_timeout(cli, tmp_path):
    scripted = Scripted([])
    try:
        process = start_watch(cli, tmp_path, scripted, "--exec", "true", "--exec-timeout", "0")
        out, err = process.communicate(timeout=20)
    finally:
        scripted.close()
    assert process.returncode == 2 and "--exec-timeout must be positive" in err


# --- DF-14'/DF-15: gate --decision edit, approver line, --epoch defaulting ---------------------

def submitted_for_review(cli, bridge, page, prompt, *, reviewers=("owner",), result="Worker candidate"):
    task = run_json(cli, bridge, "owner", "call", "create_task_block", "--operation-id", str(uuid.uuid4()), "--json", json.dumps({
        "page_id": page["id"], "prompt": prompt, "scopes": [{"kind": "none"}],
        "reviewers": [bridge.identities[label]["agent"] for label in reviewers],
        "assignee_principal": bridge.identities["worker"]["agent"]}))
    claim = run_json(cli, bridge, "worker", "claim", "--task", task["task_id"])
    run_json(cli, bridge, "worker", "result", "--task", task["task_id"], "--epoch", str(claim["epoch"]), "--result", result)
    return task["task_id"], claim["epoch"]


def requests_to(bridge, operation, since):
    return [record for record in bridge.requests[since:] if record["path"].endswith("/v1/space/" + operation)]


def test_gate_without_epoch_reads_the_task_once_and_says_which_epoch_it_used(cli, bridge, workspace):
    _, page = workspace
    task_id, epoch = submitted_for_review(cli, bridge, page, "Default the epoch")
    before = len(bridge.requests)
    decided = run_as(cli, bridge, "owner", "gate", "--task", task_id, "--decision", "pass")
    assert decided.rc == 0, decided.err
    owner = bridge.identities["owner"]["agent"]
    assert decided.err == ("baton_cli: gate: --epoch not given; using result_epoch=%d from get_task\n" % epoch
                           + "task %s state=done epoch=%d gate=pass approver=%s authority=root\n" % (task_id, epoch, owner))
    reads, writes = requests_to(bridge, "get_task", before), requests_to(bridge, "gate", before)
    assert len(reads) == 1 and len(writes) == 1
    assert bridge.recorder.body(reads[0]) == {"task_id": task_id}
    assert bridge.recorder.body(writes[0])["expected_epoch"] == epoch and decided.json()["state"] == "done"


def test_gate_with_an_explicit_epoch_makes_no_extra_read(cli, bridge, workspace):
    _, page = workspace
    task_id, epoch = submitted_for_review(cli, bridge, page, "Explicit epoch")
    before = len(bridge.requests)
    decided = run_as(cli, bridge, "owner", "gate", "--task", task_id, "--epoch", str(epoch), "--decision", "pass")
    assert decided.rc == 0 and "--epoch not given" not in decided.err
    assert requests_to(bridge, "get_task", before) == [] and len(requests_to(bridge, "gate", before)) == 1


def test_gate_without_epoch_on_a_task_with_no_result_is_a_usage_error_and_never_gates(cli, bridge, workspace):
    _, page = workspace
    task = submit_manual(cli, bridge, page, "Nothing submitted yet", "--scope", "none")
    before = len(bridge.requests)
    refused = run_as(cli, bridge, "owner", "gate", "--task", task["task_id"], "--decision", "pass")
    assert refused.rc == 2 and refused.out == ""
    assert refused.err == "baton_cli: error: cannot default --epoch: the task has no submitted result to gate (state=queued)\n"
    assert requests_to(bridge, "gate", before) == [] and len(requests_to(bridge, "get_task", before)) == 1
    missing = run_as(cli, bridge, "owner", "gate", "--task", str(uuid.uuid4()), "--decision", "pass")
    assert missing.rc == 1 and "not_found" in missing.err


def test_gate_edit_sends_the_replacement_and_reason_and_shows_the_approver(cli, bridge, workspace, tmp_path):
    _, page = workspace
    task_id, epoch = submitted_for_review(cli, bridge, page, "Edit inline")
    before = len(bridge.requests)
    decided = run_as(cli, bridge, "owner", "gate", "--task", task_id, "--decision", "edit", "--edited-result", CHINESE,
                     "--reason", "Fixed the wording")
    assert decided.rc == 0, decided.err
    sent = bridge.recorder.body(requests_to(bridge, "gate", before)[0])
    assert sent["decision"] == "edit" and sent["edited_result"] == CHINESE and sent["reason"] == "Fixed the wording"
    assert sent["expected_epoch"] == epoch
    result = decided.json()
    assert result["state"] == "done" and result["result"] == CHINESE and result["original_result"] == "Worker candidate"
    assert result["gate"]["decision"] == "edit" and result["gate"]["authority"] == "root"
    assert decided.err.endswith("task %s state=done epoch=%d gate=edit approver=%s authority=root\n" % (
        task_id, epoch, bridge.identities["owner"]["agent"]))
    event = next(row for row in run_json(cli, bridge, "owner", "audit", "--task", task_id) if row["event"] == "gate.result_overridden")
    assert event["metadata"]["reason"] == "Fixed the wording"

    second_id, _ = submitted_for_review(cli, bridge, page, "Edit from a file")
    edited = tmp_path / "edited.md"
    edited.write_text("# Edited\nfrom a file\n", encoding="utf-8")
    from_file = run_as(cli, bridge, "owner", "gate", "--task", second_id, "--decision", "edit", "--edited-result-file", edited)
    assert from_file.rc == 0 and from_file.json()["result"] == "# Edited\nfrom a file\n"
    third_id, _ = submitted_for_review(cli, bridge, page, "Edit from stdin")
    from_stdin = run_as(cli, bridge, "owner", "gate", "--task", third_id, "--decision", "edit", "--edited-result-file", "-",
                        stdin="piped replacement")
    assert from_stdin.rc == 0 and from_stdin.json()["result"] == "piped replacement"


def test_gate_edit_flags_must_go_together(cli, bridge, workspace):
    _, page = workspace
    task_id, epoch = submitted_for_review(cli, bridge, page, "Bad flags")
    before = len(bridge.requests)
    for extra in (("--decision", "edit"), ("--decision", "pass", "--edited-result", "x"),
                  ("--decision", "fail", "--edited-result-file", "-"), ("--decision", "approve")):
        refused = run_as(cli, bridge, "owner", "gate", "--task", task_id, "--epoch", str(epoch), *extra, stdin="x")
        assert refused.rc == 2 and refused.out == ""
    both = run_as(cli, bridge, "owner", "gate", "--task", task_id, "--decision", "edit", "--edited-result", "a",
                  "--edited-result-file", "-", stdin="b")
    assert both.rc == 2
    assert len(bridge.requests) == before
    unchanged = run_as(cli, bridge, "owner", "gate", "--task", task_id, "--epoch", str(epoch), "--decision", "edit",
                       "--edited-result", "Worker candidate")
    assert unchanged.rc == 1 and "invalid (HTTP 422)" in unchanged.err and "edited_result" in unchanged.err
    assert run_json(cli, bridge, "owner", "task", task_id)["state"] == "awaiting_gate"


def test_gate_summary_names_a_delegated_approver_and_its_grantor(cli, bridge, workspace):
    _, page = workspace
    reviewer = bridge.identities["reviewer"]["agent"]
    owner = bridge.identities["owner"]["agent"]
    task_id, epoch = submitted_for_review(cli, bridge, page, "Delegated approval", reviewers=("reviewer",))
    refused = run_as(cli, bridge, "reviewer", "gate", "--task", task_id, "--decision", "pass")
    assert refused.rc == 1 and "delegation_required (HTTP 403)" in refused.err and "task " not in refused.err
    delegation = run_json(cli, bridge, "owner", "call", "delegation_create", "--operation-id", str(uuid.uuid4()),
                          "--json", json.dumps({"delegate_principal": reviewer, "reason": "DECISIONS 291"}))
    decided = run_as(cli, bridge, "reviewer", "gate", "--task", task_id, "--decision", "edit", "--edited-result", "Delegate edit")
    assert decided.rc == 0, decided.err
    authority = "delegated by %s via %s" % (owner, delegation["id"])
    assert decided.json()["gate"]["authority"] == authority
    assert decided.err.endswith("task %s state=done epoch=%d gate=edit approver=%s authority=%s\n" % (task_id, epoch, reviewer, authority))


def test_root_self_approval_summary_is_marked(cli, bridge, workspace):
    _, page = workspace
    task = run_json(cli, bridge, "owner", "call", "create_task_block", "--operation-id", str(uuid.uuid4()), "--json", json.dumps({
        "page_id": page["id"], "prompt": "Owner does the work", "scopes": [{"kind": "none"}],
        "reviewers": [bridge.identities["reviewer"]["agent"]]}))
    claim = run_json(cli, bridge, "owner", "claim", "--task", task["task_id"])
    run_json(cli, bridge, "owner", "result", "--task", task["task_id"], "--epoch", str(claim["epoch"]), "--result", "mine")
    refused = run_as(cli, bridge, "owner", "gate", "--task", task["task_id"], "--decision", "pass")
    assert refused.rc == 1 and "forbidden (HTTP 403)" in refused.err
    decided = run_as(cli, bridge, "owner", "gate", "--task", task["task_id"], "--decision", "pass", "--self-approval")
    assert decided.rc == 0
    assert decided.err.endswith("gate=pass approver=%s authority=root (self-approval)\n" % bridge.identities["owner"]["agent"])


def test_gate_summary_only_echoes_well_formed_fields(capsys):
    module = load_cli_module()
    base = {"task_id": TASK_A, "state": "done", "epoch": 2}
    delegation = str(uuid.uuid4())
    module.print_summary({**base, "gate": {"decision": "pass", "approver": "judge", "root": False, "self_approval": False,
                                           "grantor": "boss", "delegation_id": delegation}})
    module.print_summary({**base, "gate": {"decision": "edit", "approver": "boss", "root": True, "self_approval": True}})
    module.print_summary({**base, "gate": {"decision": "pass\nforged", "approver": "x\x1b[31m", "root": False,
                                           "grantor": "boss", "delegation_id": "not-a-uuid"}})
    module.print_summary({**base, "gate": "root"})
    assert capsys.readouterr().err == (
        "task %s state=done epoch=2 gate=pass approver=judge authority=delegated by boss via %s\n" % (TASK_A, delegation)
        + "task %s state=done epoch=2 gate=edit approver=boss authority=root (self-approval)\n" % TASK_A
        + "task %s state=done epoch=2\n" % TASK_A + "task %s state=done epoch=2\n" % TASK_A)


def test_watch_summary_shows_the_result_epoch_and_author_of_an_awaiting_gate_notification():
    module = load_cli_module()
    note = {"seq": 7, "event": "task.awaiting_gate", "object_type": "task", "object_id": TASK_A, "result_epoch": 3,
            "result_author": "worker-1", "subject": "[awaiting_gate] x"}
    assert module.notification_line(note) == ("notification seq=7 task.awaiting_gate task=%s result_epoch=3 result_author=worker-1 - [awaiting_gate] x" % TASK_A)
    forged = {**note, "result_author": "x\x1b[31m", "result_epoch": True}
    assert "result_" not in module.notification_line(forged)
    assert "result_author" not in module.notification_line({**note, "result_author": "x\x1b[31m"})
