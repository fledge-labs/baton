# SPDX-License-Identifier: Apache-2.0
"""Edge configuration and the deploy script's smoke/rollback decisions, with a fake docker and a fake gateway."""

import importlib.util
import io
import json
import re
import shutil
import subprocess
import sys
import threading
import time
import types
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("baton_deploy", ROOT / "scripts/deploy.py")
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)

TOKEN = "smoke-secret-token-0123456789abcdef"
OLD_APP, NEW_APP = "sha256:" + "a" * 64, "sha256:" + "b" * 64
NGINX_REF = "nginx:1.28.0-alpine@sha256:" + "c" * 64
APP_REF = "baton-app:local"


def parse_nginx(text):
    """Parse nginx configuration into [(name, args, children)]; children is None for plain directives."""
    text = re.sub(r"#[^\n]*", "", text)
    tokens = re.findall(r'"[^"]*"|\'[^\']*\'|[{};]|[^\s{};"\']+', text)
    stack, current = [], []
    words = []
    for token in tokens:
        if token == ";":
            current.append((words[0], words[1:], None))
            words = []
        elif token == "{":
            block = (words[0], words[1:], [])
            current.append(block)
            stack.append(current)
            current, words = block[2], []
        elif token == "}":
            current = stack.pop()
        else:
            words.append(token)
    assert not stack and not words, "unbalanced nginx configuration"
    return current


def walk(directives, path=()):
    for name, args, children in directives:
        yield path, name, args
        if children is not None:
            yield from walk(children, path + ((name, tuple(args)),))


def directives_in(config, name):
    return [(path, args) for path, found, args in walk(config) if found == name]


@pytest.fixture(params=["nginx.conf", "nginx.tls.conf"])
def nginx(request):
    return request.param, parse_nginx((ROOT / "deploy" / request.param).read_text())


def test_nginx_proxy_pass_never_uses_a_static_upstream_name(nginx):
    _, config = nginx
    passes = directives_in(config, "proxy_pass")
    assert passes, "the gateway proxy location must exist"
    assert not directives_in(config, "upstream")
    for _, args in passes:
        assert len(args) == 1 and args[0].startswith("$"), "proxy_pass must use a variable: %s" % args
        assert "gateway" not in args[0]
    text = (ROOT / "deploy" / nginx[0]).read_text()
    assert not re.search(r"proxy_pass\s+https?://", text)


def test_nginx_resolves_the_gateway_per_request_through_docker_dns(nginx):
    _, config = nginx
    resolvers = [(path, args) for path, args in directives_in(config, "resolver")]
    assert resolvers == [((("http", ()),), ["127.0.0.11", "valid=10s", "ipv6=off"])]
    ((path, args),) = directives_in(config, "proxy_pass")
    assert path[-1] == ("location", ("/",))
    assert args == ["$upstream$request_uri"], "the client's request URI is forwarded unchanged"
    assert [a for p, a in directives_in(config, "set") if p == path] == [["$upstream", "http://gateway:3000"]]
    in_location = [name for found_path, name, _ in walk(config) if found_path == path and name in ("set", "proxy_pass")]
    assert in_location == ["set", "proxy_pass"], "the variable is set before it is used"


def test_nginx_keeps_every_location_header_limit_and_streaming_setting(nginx):
    _, config = nginx
    locations = [args for _, name, args in walk(config) if name == "location"]
    assert locations == [["=", "/"], ["=", "/board"], ["/"]]
    in_gateway = {(name, tuple(args)) for path, name, args in walk(config) if path and path[-1] == ("location", ("/",))}
    for expected in [
        ("proxy_http_version", ("1.1",)),
        ("proxy_set_header", ("Host", "$host")),
        ("proxy_set_header", ("X-Request-ID", "$baton_request_id")),
        ("proxy_set_header", ("X-Forwarded-Proto", "$scheme")),
        ("proxy_set_header", ("Connection", '""')),
        ("proxy_buffering", ("off",)),
        ("proxy_read_timeout", ("370s",)),
        ("proxy_send_timeout", ("370s",)),
    ]:
        assert expected in in_gateway, expected
    server = {(name, tuple(args)) for path, name, args in walk(config) if path and path[-1][0] == "server"}
    assert ("client_max_body_size", ("128k",)) in server
    assert ("add_header", ("X-Content-Type-Options", "nosniff", "always")) in server
    assert ("add_header", ("Cache-Control", "no-store", "always")) in server
    assert ("return", ("302", "/board")) in {(n, tuple(a)) for _, n, a in walk(config)}
    assert not directives_in(config, "proxy_redirect") and not directives_in(config, "rewrite")


def test_plain_and_tls_nginx_configs_differ_only_in_the_listener():
    plain, tls = (parse_nginx((ROOT / "deploy" / name).read_text()) for name in ("nginx.conf", "nginx.tls.conf"))
    listener_only = {"listen", "ssl_certificate", "ssl_certificate_key", "ssl_protocols"}

    def comparable(config):
        return [(path, name, tuple(args)) for path, name, args in walk(config)
                if name not in listener_only and not (name == "add_header" and args[0] == "Strict-Transport-Security")]

    assert comparable(plain) == comparable(tls)


def test_edge_healthcheck_probes_the_gateway_route_for_401():
    compose = (ROOT / "deploy/compose.yaml").read_text()
    edge = compose.split("\n  edge:\n")[1].split("\n  gateway:\n")[0]
    health = edge.split("healthcheck:")[1]
    assert "CMD-SHELL" in health and "/board" in health
    assert "--post-data" in health and "/mcp" in health and "401" in health
    gateway = compose.split("\n  gateway:\n")[1].split("\n  bridge:\n")[0]
    assert "healthcheck" not in gateway, "the gateway image has no shell, wget or curl to run a probe with"


def test_bridge_healthcheck_calls_healthz_which_reads_the_database_instead_of_opening_a_socket():
    compose = (ROOT / "deploy/compose.yaml").read_text()
    bridge = compose.split("\n  bridge:\n")[1].split("\n  database:\n")[0]
    health = bridge.split("healthcheck:")[1]
    assert "/healthz" in health and "urlopen" in health and "create_connection" not in health
    assert "127.0.0.1:9000" in health


def test_tls_edge_healthcheck_checks_the_listener_the_handshake_and_the_gateway_route():
    health = (ROOT / "deploy/compose.tls.yaml").read_text().split("healthcheck:")[1]
    assert "CMD-SHELL" in health and "nginx -t" not in health.replace("# ", "")
    assert health.count("https://127.0.0.1:8443/") == 2 and "/board" in health and "/mcp" in health
    assert "401" in health and "--post-data" in health


def test_bridge_healthz_is_open_only_to_loopback_inside_the_container_and_reads_the_database(modules, pg_store, monkeypatch):
    from starlette.testclient import TestClient

    common, server = modules
    monkeypatch.setattr(common, "identity", lambda value: None)
    remote = TestClient(server.app)
    with TestClient(server.app, client=("127.0.0.1", 50000)) as local:
        answer = local.get("/healthz")
        assert answer.status_code == 200 and answer.json()["status"] == "ok"
        assert remote.get("/healthz").status_code == 401, "other containers, including the gateway, still need a token"
        assert remote.get("/healthz", headers={"X-Forwarded-For": "127.0.0.1"}).status_code == 401
        for method, path in (("POST", "/healthz"), ("GET", "/board/data"), ("GET", "/v1/space/schema"), ("POST", "/mcp"),
                             ("POST", "/v1/space/list_tasks")):
            assert local.request(method, path).status_code == 401, (method, path)

        async def storage_down(*args, **kwargs):
            raise server.BridgeError("storage_unavailable")

        monkeypatch.setattr(server, "store_call", storage_down)
        assert local.get("/healthz").status_code == 503, "an unreachable database makes the healthcheck fail"


NGINX_IMAGE = re.search(r"image:\s*(nginx:\S+)", (ROOT / "deploy/compose.yaml").read_text()).group(1)
BACKEND_CONF = """pid /tmp/nginx.pid;
events {}
http {
    server {
        listen 3000;
        location / { default_type text/plain; return 200 "$request_uri|$http_host|$http_x_forwarded_proto|$http_connection"; }
    }
}
"""
RAW_URIS = ["/mcp", "/v1/space/a%2Fb/../c//d?x=1&y=%20z", "/v1/space//x/./y?", "/board/data?a=b%2Fc"]


class Docker:
    def __init__(self):
        self.created = []

    def __call__(self, *args, check=True):
        done = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120)
        if check and done.returncode != 0:
            raise AssertionError("docker %s failed: %s" % (" ".join(args[:2]), done.stderr.strip()))
        return done.stdout.strip()


@pytest.fixture
def nginx_lab(tmp_path):
    if shutil.which("docker") is None or subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        pytest.skip("docker is required for the nginx behaviour tests (scripts/test_with_postgres.sh provides it)")
    docker, suffix = Docker(), uuid.uuid4().hex[:8]
    network, names = "baton-nginx-%s" % suffix, []
    docker("network", "create", network)
    backend_conf = tmp_path / "backend.conf"
    backend_conf.write_text(BACKEND_CONF)
    backend_conf.chmod(0o644)

    def start_backend(name):
        names.append(name)
        docker("run", "-d", "--name", name, "--network", network, "--network-alias", "gateway", "-v",
               "%s:/etc/nginx/nginx.conf:ro" % backend_conf, NGINX_IMAGE, "nginx", "-g", "daemon off;")

    def start_edge(name, conf_text):
        conf = tmp_path / (name + ".conf")
        conf.write_text(conf_text)
        conf.chmod(0o644)
        names.append(name)
        docker("run", "-d", "--name", name, "--network", network, "--user", "101:101", "--tmpfs", "/tmp:mode=1777",
               "--tmpfs", "/var/cache/nginx:uid=101,gid=101,mode=0700", "-v", "%s:/etc/nginx/nginx.conf:ro" % conf,
               "-v", "%s:/usr/share/nginx/html:ro" % (ROOT / "board"), NGINX_IMAGE, "nginx", "-g", "daemon off;")
        for _ in range(40):
            probe = subprocess.run(["docker", "exec", name, "wget", "-q", "--spider", "http://127.0.0.1:8080/board"],
                                   capture_output=True)
            if probe.returncode == 0:
                return name
            time.sleep(0.25)
        raise AssertionError("edge did not start: " + docker("logs", name, check=False))

    def raw(name, uri):
        script = "{ printf 'GET %s HTTP/1.1\\r\\nHost: edge.test\\r\\nConnection: close\\r\\n\\r\\n' \"$1\"; sleep 1; } | nc 127.0.0.1 8080"
        answer = docker("exec", name, "sh", "-c", script, "sh", uri, check=False)
        head, _, body = answer.replace("\r\n", "\n").partition("\n\n")
        return int(head.split()[1]) if head.startswith("HTTP/") else 0, body.strip()

    lab = types.SimpleNamespace(docker=docker, network=network, start_backend=start_backend, start_edge=start_edge,
                                raw=raw, names=names, suffix=suffix)
    try:
        yield lab
    finally:
        for name in reversed(names):
            docker("rm", "-f", name, check=False)
        docker("network", "rm", network, check=False)


def legacy_conf(current):
    legacy = current.replace("            set $upstream http://gateway:3000;\n", "")
    legacy = legacy.replace("proxy_pass $upstream$request_uri;", "proxy_pass http://gateway:3000;")
    legacy = re.sub(r"    # Docker's embedded DNS.*?\n    resolver [^\n]*\n    resolver_timeout[^\n]*\n", "", legacy, flags=re.DOTALL)
    assert "resolver" not in legacy and "proxy_pass http://gateway:3000;" in legacy
    return legacy


def test_nginx_forwards_exactly_the_same_request_uri_headers_and_status_as_the_static_proxy_pass(nginx_lab):
    lab = nginx_lab
    lab.start_backend("backend-" + lab.suffix)
    current = (ROOT / "deploy/nginx.conf").read_text()
    new = lab.start_edge("edge-new-" + lab.suffix, current)
    old = lab.start_edge("edge-old-" + lab.suffix, legacy_conf(current))
    for uri in RAW_URIS:
        status_new, body_new = lab.raw(new, uri)
        status_old, body_old = lab.raw(old, uri)
        assert status_new == status_old == 200, uri
        assert body_new == body_old, "routing semantics changed for %s: %r vs %r" % (uri, body_new, body_old)
        assert body_new.split("|")[0] == uri, "the client's URI is forwarded byte for byte, as before"
        assert body_new.split("|")[1:] == ["edge.test", "http", ""], body_new


def test_nginx_follows_a_recreated_gateway_to_its_new_address_while_the_static_config_keeps_502ing(nginx_lab):
    lab = nginx_lab
    current = (ROOT / "deploy/nginx.conf").read_text()
    backend = "backend-" + lab.suffix
    lab.start_backend(backend)
    new = lab.start_edge("edge-new-" + lab.suffix, current)
    old = lab.start_edge("edge-old-" + lab.suffix, legacy_conf(current))
    assert lab.raw(new, "/mcp")[0] == lab.raw(old, "/mcp")[0] == 200

    def address(name):
        return lab.docker("inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", name)

    first = address(backend)
    lab.docker("rm", "-f", backend)
    squatter = "squatter-" + lab.suffix
    lab.names.append(squatter)
    lab.docker("run", "-d", "--name", squatter, "--network", lab.network, NGINX_IMAGE)
    replacement = "backend2-" + lab.suffix
    lab.start_backend(replacement)
    assert address(replacement) != first, "the recreated gateway must have moved"
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline and lab.raw(new, "/mcp")[0] != 200:
        time.sleep(1)
    assert lab.raw(new, "/mcp")[0] == 200, "per-request resolution picked up the new address"
    assert lab.raw(old, "/mcp")[0] in (502, 504, 0), "the startup-resolved config is stuck on the old address"


DB_REF = "postgres:17.11-bookworm@sha256:" + "d" * 64
DB_ID = "sha256:" + "d" * 64
EDGE_ID = "sha256:" + "c" * 64
NEW_EDGE_ID = "sha256:" + "e" * 64


class FakeDocker:
    """Scripted docker/compose: containers per service, the app image tag, and one serving image."""

    def __init__(self, existing=True, up_results=None, recovery_ok=True, build_ok=True, fail_tag_refs=(), hold_ok=True,
                 dump="ok", stopped=(), orphans=(), edge_drift=False, build_timeout=False):
        self.calls, self.timeouts = [], []
        self.up_results = list(up_results if up_results is not None else [0, 0])
        self.recovery_ok, self.build_ok, self.fail_tag_refs, self.hold_ok = recovery_ok, build_ok, set(fail_tag_refs), hold_ok
        self.dump, self.edge_drift, self.build_timeout = dump, edge_drift, build_timeout
        self.tags = {APP_REF: OLD_APP} if existing else {}
        self.serving = OLD_APP if existing else None
        self.override_text = None
        self.containers = {}
        if existing:
            for service, ref, image_id, running in [("bridge", APP_REF, OLD_APP, True), ("database", DB_REF, DB_ID, True),
                                                    ("edge", NGINX_REF, EDGE_ID, True),
                                                    ("initialize", APP_REF, OLD_APP, False), ("migrate", APP_REF, OLD_APP, False)]:
                self.containers[service] = {"ref": ref, "id": image_id, "running": running and service not in stopped}
        for service in orphans:
            self.containers[service] = {"ref": "orphan:local", "id": "sha256:" + "f" * 64, "running": True}
        self.declared = ["bridge", "database", "edge", "gateway", "initialize", "migrate"]

    def __call__(self, argv, timeout=None, stdout_path=None):
        self.calls.append(list(argv))
        self.timeouts.append(timeout)
        if argv[:2] == ["docker", "compose"] and "config" in argv:
            services = {name: ({"build": {}} if name in ("initialize", "bridge") else {}) for name in self.declared}
            return deploy.Result(0, json.dumps({"name": "demo", "services": services}))
        if argv[:3] == ["docker", "ps", "-a"]:
            return deploy.Result(0, "".join(name + "\n" for name in self.containers))
        if argv[:2] == ["docker", "inspect"]:
            return deploy.Result(0, json.dumps([self.inspect(name) for name in argv[2:]]))
        if argv[:3] == ["docker", "image", "inspect"]:
            ref = argv[-1]
            return deploy.Result(0, self.tags[ref] + "\n") if ref in self.tags else deploy.Result(1, "", "no such image")
        if "exec" in argv and argv[argv.index("exec") + 2] == "database":
            if self.dump == "fail":
                return deploy.Result(1, "", "pg_dump: connection refused")
            if self.dump == "timeout":
                return deploy.Result(124, "", "timed out", timed_out=True)
            Path(stdout_path).write_bytes(b"PGDMP" + b"\0" * 64 if self.dump == "ok" else b"not a dump")
            return deploy.Result(0)
        if argv[:2] == ["docker", "compose"] and argv[-1] == "build":
            self.override_text = next((Path(a).read_text() for a in argv if a.endswith("build-network.yaml")), None)
            if self.build_timeout:
                return deploy.Result(124, "", "timed out", timed_out=True)
            if not self.build_ok:
                return deploy.Result(1, "", "build exploded with " + TOKEN)
            self.tags[APP_REF] = NEW_APP
            return deploy.Result(0, "built")
        if argv[:2] == ["docker", "tag"]:
            target = argv[3]
            if target.startswith(deploy.HOLD_REPOSITORY):
                if not self.hold_ok:
                    return deploy.Result(1, "", "tag failed")
            elif target in self.fail_tag_refs:
                return deploy.Result(1, "", "No such image: " + argv[2])
            self.tags[target] = argv[2]
            return deploy.Result(0)
        if argv[:2] == ["docker", "rmi"]:
            self.tags.pop(argv[2], None)
            return deploy.Result(0)
        if argv[:2] == ["docker", "compose"] and "up" in argv:
            if "--force-recreate" in argv:
                outcome = self.up_results.pop(0)
            else:
                outcome = 0 if self.recovery_ok else 1
            if outcome == "timeout":
                return deploy.Result(124, "", "timed out", timed_out=True)
            if outcome != 0:
                return deploy.Result(outcome, "up output", "up failed")
            self.start(argv)
            return deploy.Result(0, "up output")
        raise AssertionError("unexpected command: %s" % argv)

    def start(self, argv):
        self.serving = self.tags.get(APP_REF)
        if "--remove-orphans" in argv:
            for name in [n for n in self.containers if n not in self.declared]:
                del self.containers[name]
        for name, container in self.containers.items():
            if container["ref"] == APP_REF:
                container["id"] = self.tags[APP_REF]
                container["running"] = name == "bridge"
            if name == "bridge" or name in ("database", "edge"):
                container["running"] = True
        if self.edge_drift and "edge" in self.containers:
            self.containers["edge"]["id"] = NEW_EDGE_ID
        self.containers.setdefault("gateway", {"ref": "gw@sha256:" + "9" * 64, "id": "sha256:" + "9" * 64, "running": True})

    def inspect(self, name):
        container = self.containers[name]
        return {"Id": name * 20, "Image": container["id"], "State": {"Running": container["running"]},
                "Created": "2026-01-01T00:00:0%dZ" % (len(self.calls) % 10),
                "Config": {"Image": container["ref"], "Labels": {deploy.SERVICE_LABEL: name}}}

    def mutating(self):
        return [c for c in self.calls if c[:2] in (["docker", "tag"], ["docker", "rmi"]) or "up" in c
                or c[-1:] == ["build"] or "exec" in c]

    def ups(self):
        return [c for c in self.calls if c[:2] == ["docker", "compose"] and "up" in c]


class FakeGateway:
    """HTTP stand-in; behaves like a healthy stack only while the given application image serves."""

    def __init__(self, docker, good_image=OLD_APP):
        self.docker, self.good_image, self.requests = docker, good_image, []

    def __call__(self, method, path, headers=None, body=None):
        headers = headers or {}
        self.requests.append((method, path, dict(headers), body))
        healthy = self.docker.serving == self.good_image
        if path == "/board":
            return deploy.Response(200 if healthy else 502, {}, b"<html>")
        authorized = headers.get("Authorization") == "Bearer " + TOKEN
        if not authorized:
            return deploy.Response(401 if healthy else 502, {}, b"")
        if not healthy:
            return deploy.Response(502, {}, b"")
        message = json.loads(body)
        results = {
            "initialize": {"protocolVersion": "2025-06-18", "serverInfo": {"name": "Baton"}, "capabilities": {}},
            "tools/list": {"tools": [{"name": "submit_task"}, {"name": "fetch_inbox"}]},
            "tools/call": {"content": [{"type": "text", "text": "{}"}], "isError": False},
        }
        if "id" not in message:
            return deploy.Response(202, {}, b"")
        return deploy.Response(200, {"content-type": "application/json"},
                               json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": results[message["method"]]}).encode())


@pytest.fixture
def files(tmp_path):
    compose = tmp_path / "compose.yaml"
    compose.write_text("services: {}\n")
    token = tmp_path / "smoke.token"
    token.write_text(TOKEN + "\n")
    token.chmod(0o600)
    return compose, token


def run(files, docker, gateway, *extra, backup=True):
    compose, token = files
    out, err = io.StringIO(), io.StringIO()
    argv = ["-f", str(compose), "-p", "demo", "--smoke-token-file", str(token), "--smoke-timeout", "0", *extra]
    if backup and "--backup-dir" not in extra and "--no-backup" not in extra:
        argv += ["--backup-dir", str(compose.parent / "backups")]
    code = deploy.main(argv, runner=docker, http=gateway, stdout=out, stderr=err)
    return code, json.loads(out.getvalue()), out.getvalue() + err.getvalue()


def test_successful_deploy_records_backs_up_builds_ups_smokes_and_never_prints_the_token(files):
    docker = FakeDocker()
    gateway = FakeGateway(docker, good_image=NEW_APP)
    code, report, text = run(files, docker, gateway)
    assert code == 0 and report["status"] == "deployed" and report["ok"]
    assert report["previous_images"]["bridge"] == {"container": "bridge" * 2, "image": APP_REF, "image_id": OLD_APP,
                                                   "running": True, "created": report["previous_images"]["bridge"]["created"]}
    assert report["previous_images"]["initialize"]["running"] is False, "stopped containers are recorded too"
    assert [c[2:] for c in docker.ups()] == [["-f", str(files[0]), "-p", "demo", "up", "-d", "--wait", "--wait-timeout", "300",
                                              "--force-recreate", "--remove-orphans"]]
    assert [item["name"] for item in report["smoke"]["checks"]] == list(deploy.SMOKE_STEPS)
    assert all(item["ok"] for item in report["smoke"]["checks"])
    assert report["rollback"] == {"attempted": False}
    assert TOKEN not in text
    assert not [t for t in docker.tags if t.startswith(deploy.HOLD_REPOSITORY)], "holding tags are released"
    unauthenticated = [h for m, p, h, b in gateway.requests if p == "/mcp" and "Authorization" not in h]
    assert len(unauthenticated) == 1


def test_every_docker_command_has_a_hard_timeout_and_up_gets_the_wait_timeout_plus_grace(files):
    docker = FakeDocker()
    run(files, docker, FakeGateway(docker, good_image=NEW_APP), "--wait-timeout", "90", "--build-timeout", "700",
        "--command-timeout", "30", "--backup-timeout", "400")
    assert all(timeout for timeout in docker.timeouts)
    by_kind = {}
    for argv, timeout in zip(docker.calls, docker.timeouts):
        kind = "up" if argv[-1] == "--remove-orphans" else "build" if argv[-1] == "build" else "dump" if "exec" in argv else "other"
        by_kind.setdefault(kind, set()).add(timeout)
    assert by_kind == {"up": {90 + deploy.UP_GRACE}, "build": {700}, "dump": {400}, "other": {30}}
    up = docker.ups()[0]
    assert up[up.index("--wait-timeout") + 1] == "90"


def test_up_that_hangs_is_killed_and_rolled_back(files):
    docker = FakeDocker(up_results=["timeout", 0])
    code, report, _ = run(files, docker, FakeGateway(docker, good_image=OLD_APP))
    assert code == 1 and report["status"] == "rolled_back"
    assert report["up"]["timed_out"] is True and report["smoke"] == {"skipped": True, "reason": "docker compose up timed out"}
    assert report["rollback"]["ok"] and len(docker.ups()) == 2


def test_rollback_up_that_hangs_gets_a_recovery_up_and_a_timed_out_rollback_is_reported(files):
    docker = FakeDocker(up_results=["timeout", "timeout"], recovery_ok=True)
    code, report, _ = run(files, docker, FakeGateway(docker, good_image=OLD_APP))
    rollback = report["rollback"]
    assert rollback["up"]["timed_out"] and rollback["recovery_up"]["ok"]
    assert rollback["warnings"] == ["docker compose up timed out during rollback"] and rollback["ok"]
    assert code == 1 and report["status"] == "rolled_back"
    assert len(docker.ups()) == 3 and "--force-recreate" not in docker.ups()[-1] and "--wait" not in docker.ups()[-1]


def test_build_that_hangs_stops_before_up(files):
    docker = FakeDocker(build_timeout=True)
    code, report, _ = run(files, docker, FakeGateway(docker))
    assert code == 1 and report["status"] == "build_failed" and report["build"]["timed_out"]
    assert not docker.ups()


def test_smoke_failure_restores_the_previous_image_verifies_it_and_exits_nonzero(files):
    docker = FakeDocker()
    code, report, text = run(files, docker, FakeGateway(docker, good_image=OLD_APP))
    assert code == 1 and report["status"] == "rolled_back" and not report["ok"]
    assert not report["smoke"]["ok"] and report["smoke"]["checks"][0]["detail"] == "expected HTTP 200, got 502"
    rollback = report["rollback"]
    assert rollback["ok"] and rollback["smoke"]["ok"] and rollback["mismatches"] == [] and rollback["errors"] == []
    assert rollback["retagged"] == [{"image": APP_REF, "image_id": OLD_APP, "ok": True,
                                     "services": ["bridge", "initialize", "migrate"]}]
    assert docker.tags[APP_REF] == OLD_APP
    ups = docker.ups()
    assert len(ups) == 2 and ups[0] == ups[1], "rollback runs the same up command again"
    assert not [c for c in docker.calls[docker.calls.index(ups[0]) + 1:] if c[-1:] == ["build"]], "rollback never rebuilds"
    assert TOKEN not in text


def test_the_previous_image_is_pinned_by_a_holding_tag_before_the_build(files):
    docker = FakeDocker()
    run(files, docker, FakeGateway(docker, good_image=NEW_APP))
    hold = deploy.hold_tag(OLD_APP)
    assert hold == "baton-deploy-rollback:" + "a" * 12
    tag_at = docker.calls.index(["docker", "tag", OLD_APP, hold])
    build_at = next(i for i, c in enumerate(docker.calls) if c[-1:] == ["build"])
    assert tag_at < build_at
    assert ["docker", "rmi", hold] in docker.calls


def test_unhealthy_up_rolls_back_without_smoking_the_failed_release(files):
    docker = FakeDocker(up_results=[1, 0])
    code, report, _ = run(files, docker, FakeGateway(docker, good_image=OLD_APP))
    assert code == 1 and report["status"] == "rolled_back"
    assert report["smoke"] == {"skipped": True, "reason": "docker compose up failed"}
    assert report["up"]["ok"] is False and report["rollback"]["ok"]


def test_first_deploy_failure_has_nothing_to_restore_and_exits_nonzero(files):
    docker = FakeDocker(existing=False, up_results=[0])
    code, report, _ = run(files, docker, FakeGateway(docker, good_image="never"))
    assert code == 3 and report["status"] == "failed"
    assert report["rollback"] == {"attempted": False, "reason": "no previous deployment existed"}
    assert len(docker.ups()) == 1 and report["backup"]["skipped"]


def test_stopped_or_crashed_containers_are_recorded_so_a_rollback_is_still_possible(files):
    docker = FakeDocker(stopped=("bridge", "database", "edge"), up_results=[1, 0])
    code, report, _ = run(files, docker, FakeGateway(docker, good_image=OLD_APP), "--no-backup")
    assert report["previous_images"]["bridge"]["image_id"] == OLD_APP and not report["previous_images"]["bridge"]["running"]
    assert code == 1 and report["status"] == "rolled_back" and report["rollback"]["attempted"]
    assert docker.tags[APP_REF] == OLD_APP


def test_snapshot_lists_every_container_of_the_project_not_only_running_ones(files):
    docker = FakeDocker()
    run(files, docker, FakeGateway(docker, good_image=NEW_APP))
    listing = next(c for c in docker.calls if c[:3] == ["docker", "ps", "-a"])
    assert "label=com.docker.compose.project=demo" in listing and "-q" in listing


def test_rollback_with_a_failed_retag_still_retags_the_rest_runs_up_and_reports_recovery_steps(files):
    docker = FakeDocker(fail_tag_refs=[APP_REF], up_results=[0, 0])
    code, report, _ = run(files, docker, FakeGateway(docker, good_image=OLD_APP))
    rollback = report["rollback"]
    assert code == 3 and report["status"] == "rollback_failed"
    assert any("could not re-tag" in e for e in rollback["errors"])
    assert len(docker.ups()) == 2, "up still runs after a failed retag"
    assert rollback["manual_recovery"] and deploy.hold_tag(OLD_APP) in docker.tags
    assert [m["service"] for m in rollback["mismatches"]] == ["bridge", "initialize", "migrate"]


def test_rollback_up_failure_triggers_a_best_effort_recovery_and_failure_is_reported_when_it_fails_too(files):
    docker = FakeDocker(up_results=[1, 1], recovery_ok=False)
    code, report, _ = run(files, docker, FakeGateway(docker, good_image=OLD_APP))
    rollback = report["rollback"]
    assert code == 3 and report["status"] == "rollback_failed"
    assert rollback["recovery_up"]["ok"] is False and "docker compose up failed during rollback" in rollback["errors"]
    assert rollback["manual_recovery"]
    assert len(docker.ups()) == 3 and "--force-recreate" not in docker.ups()[-1]
    assert deploy.hold_tag(OLD_APP) in docker.tags, "holding tags survive a failed rollback"


def test_a_rollback_that_leaves_a_service_on_a_new_image_is_not_reported_as_rolled_back(files):
    docker = FakeDocker(edge_drift=True)
    code, report, _ = run(files, docker, FakeGateway(docker, good_image=OLD_APP))
    rollback = report["rollback"]
    assert rollback["smoke"]["ok"], "the smoke test passes, yet the rollback is incomplete"
    assert [m["service"] for m in rollback["mismatches"]] == ["edge"]
    assert rollback["mismatches"][0] == {"service": "edge", "expected": EDGE_ID, "actual": NEW_EDGE_ID}
    assert code == 3 and report["status"] == "rollback_failed" and not rollback["ok"]
    assert {s["image"] for s in rollback["skipped"]} >= {NGINX_REF}


def test_rollback_that_is_still_unhealthy_is_reported_as_failed(files):
    docker = FakeDocker()
    code, report, _ = run(files, docker, FakeGateway(docker, good_image="neither"))
    assert code == 3 and report["status"] == "rollback_failed"
    assert "the restored deployment fails the smoke checks" in report["rollback"]["errors"]


def test_mcp_failure_alone_triggers_rollback_and_skips_later_steps(files):
    docker = FakeDocker()
    new_release = FakeGateway(docker, good_image=NEW_APP)
    previous_release = FakeGateway(docker, good_image=OLD_APP)

    def new_release_cannot_initialize(method, path, headers=None, body=None):
        if docker.serving != NEW_APP:
            return previous_release(method, path, headers, body)
        if body and json.loads(body).get("method") == "initialize":
            return deploy.Response(503, {}, b"")
        return new_release(method, path, headers, body)

    code, report, _ = run(files, docker, new_release_cannot_initialize)
    assert code == 1 and report["status"] == "rolled_back"
    steps = {item["name"]: item for item in report["smoke"]["checks"]}
    assert steps["board"]["ok"] and steps["mcp_unauthenticated"]["ok"]
    assert steps["mcp_initialize"]["detail"] == "HTTP 503"
    assert steps["mcp_list_tools"]["detail"].startswith("skipped") and steps["mcp_fetch_inbox"]["detail"].startswith("skipped")


def test_build_failure_stops_before_up_and_redacts_output(files):
    docker = FakeDocker(build_ok=False)
    code, report, text = run(files, docker, FakeGateway(docker))
    assert code == 1 and report["status"] == "build_failed"
    assert not docker.ups()
    assert "[redacted]" in report["build"]["output_tail"] and TOKEN not in text
    assert ["docker", "rmi", deploy.hold_tag(OLD_APP)] in docker.calls


def test_holding_failure_aborts_before_the_build(files):
    docker = FakeDocker(hold_ok=False)
    code, report, _ = run(files, docker, FakeGateway(docker))
    assert code == 1 and report["status"] == "aborted"
    assert not docker.ups() and not [c for c in docker.calls if c[-1:] == ["build"]]


def test_database_is_backed_up_before_the_build_and_the_migration(files, tmp_path):
    docker = FakeDocker()
    code, report, text = run(files, docker, FakeGateway(docker, good_image=NEW_APP))
    backup = report["backup"]
    path = Path(backup["path"])
    assert code == 0 and backup["ok"] and path.parent == files[0].parent / "backups" and path.name.startswith("baton-demo-")
    assert path.read_bytes().startswith(b"PGDMP")
    dump = next(i for i, c in enumerate(docker.calls) if "exec" in c)
    assert dump < next(i for i, c in enumerate(docker.calls) if c[-1:] == ["build"])
    assert dump < next(i for i, c in enumerate(docker.calls) if c in docker.ups())
    argv = docker.calls[dump]
    assert argv[argv.index("exec"):argv.index("exec") + 4] == ["exec", "-T", "database", "sh"] and "pg_dump" in argv[-1]
    assert "postgres.password" in argv[-1] and "PGPASSWORD=\"$(cat" in argv[-1], "the password is read inside the container"
    assert not re.search(r"password\s*[=:]\s*\S{8,}", " ".join(argv).replace("postgres.password", ""))


def test_backup_directory_and_files_are_private(files):
    docker = FakeDocker()
    run(files, docker, FakeGateway(docker, good_image=NEW_APP))
    directory = files[0].parent / "backups"
    assert directory.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("dump", ["fail", "timeout", "garbage"])
def test_a_failed_or_invalid_backup_aborts_before_anything_changes(files, dump):
    docker = FakeDocker(dump=dump)
    code, report, _ = run(files, docker, FakeGateway(docker))
    assert code == 1 and report["status"] == "aborted" and not report["backup"]["ok"]
    assert report["rollback"]["reason"] == "the database backup failed; nothing was changed"
    assert not docker.ups() and not [c for c in docker.calls if c[-1:] == ["build"]]
    assert not list((files[0].parent / "backups").glob("*.dump")), "a bad dump is not left behind"
    assert ["docker", "rmi", deploy.hold_tag(OLD_APP)] in docker.calls


def test_backup_cannot_be_taken_when_the_database_is_stopped_unless_explicitly_waived(files):
    docker = FakeDocker(stopped=("database",))
    code, report, _ = run(files, docker, FakeGateway(docker))
    assert code == 1 and report["status"] == "aborted" and "not running" in report["backup"]["error"]
    docker = FakeDocker(stopped=("database",))
    code, report, _ = run(files, docker, FakeGateway(docker, good_image=NEW_APP), "--no-backup")
    assert code == 0 and report["backup"] == {"skipped": True, "reason": "disabled by --no-backup"}


def test_backup_dir_is_required_unless_no_backup_is_given(files, capsys):
    compose, token = files
    with pytest.raises(SystemExit) as caught:
        deploy.main(["-f", str(compose), "--smoke-token-file", str(token)], runner=FakeDocker())
    assert caught.value.code == 2 and "--backup-dir is required" in capsys.readouterr().err


@pytest.mark.parametrize("statement", [
    "DROP TABLE baton.old;", "ALTER TABLE baton.t DROP COLUMN c;", "ALTER TABLE baton.t RENAME COLUMN a TO b;",
    "ALTER TABLE baton.t RENAME TO u;", "ALTER TABLE baton.t ALTER COLUMN a TYPE bigint;",
    "ALTER TABLE baton.t ALTER COLUMN a SET DATA TYPE text;", "ALTER TABLE baton.t ALTER COLUMN a SET NOT NULL;",
    "ALTER TABLE baton.t ADD COLUMN IF NOT EXISTS a text NOT NULL;", "TRUNCATE baton.t;", "DELETE FROM baton.t;",
    "drop schema baton cascade;", "DROP FUNCTION baton.f();", "DROP VIEW baton.v;",
])
def test_migration_lint_flags_contract_statements_a_previous_image_cannot_survive(statement):
    assert deploy.unsafe_migration_statements("CREATE TABLE IF NOT EXISTS baton.t(a int);\n" + statement + "\n")


@pytest.mark.parametrize("statement", [
    "ALTER TABLE baton.t ADD COLUMN IF NOT EXISTS a text;",
    "ALTER TABLE baton.t ADD COLUMN IF NOT EXISTS a boolean NOT NULL DEFAULT false;",
    "ALTER TABLE baton.t DROP CONSTRAINT IF EXISTS old_key;",
    "CREATE TRIGGER x BEFORE UPDATE OR DELETE ON baton.t FOR EACH ROW EXECUTE FUNCTION f();",
    "CREATE TRIGGER y BEFORE TRUNCATE ON baton.t FOR EACH STATEMENT EXECUTE FUNCTION f();",
    "-- DROP TABLE baton.commented;\n/* DELETE FROM baton.t; */ UPDATE baton.t SET a = 1;",
])
def test_migration_lint_accepts_expand_only_statements(statement):
    assert deploy.unsafe_migration_statements(statement) == []


def test_the_shipped_migration_sql_is_expand_only():
    schema = ROOT / "bridge/space_schema.sql"
    assert deploy.unsafe_migration_statements(schema.read_text(encoding="utf-8")) == []
    assert deploy.SCHEMA_FILE == schema


def test_an_incompatible_migration_aborts_the_deploy_before_anything_changes_unless_acknowledged(files, tmp_path, monkeypatch):
    schema = tmp_path / "schema.sql"
    schema.write_text("ALTER TABLE baton.t DROP COLUMN c;\n")
    monkeypatch.setattr(deploy, "SCHEMA_FILE", schema)
    docker = FakeDocker()
    code, report, _ = run(files, docker, FakeGateway(docker))
    assert code == 1 and report["status"] == "aborted" and not docker.calls
    assert "not backward compatible" in report["rollback"]["reason"] and "drops a table" in report["rollback"]["reason"]
    docker = FakeDocker()
    code, report, _ = run(files, docker, FakeGateway(docker, good_image=NEW_APP), "--allow-incompatible-migration")
    assert code == 0 and report["migration"]["unsafe"]


def test_orphans_must_be_acknowledged_and_are_excluded_from_rollback_verification(files):
    docker = FakeDocker(orphans=("legacy",))
    code, report, _ = run(files, docker, FakeGateway(docker))
    assert code == 1 and report["status"] == "aborted" and "legacy" in report["rollback"]["reason"]
    assert "--allow-remove-orphans" in report["rollback"]["reason"] and not docker.ups()
    assert [o["service"] for o in report["orphans"]] == ["legacy"]
    docker = FakeDocker(orphans=("legacy",))
    code, report, _ = run(files, docker, FakeGateway(docker, good_image=OLD_APP), "--allow-remove-orphans")
    assert code == 1 and report["status"] == "rolled_back", "the removed orphan does not make the rollback look incomplete"
    assert [o["service"] for o in report["orphans"]] == ["legacy"] and report["rollback"]["mismatches"] == []


def test_dry_run_only_runs_read_only_queries_and_never_touches_the_network(files):
    docker = FakeDocker()

    def no_network(*args, **kwargs):
        raise AssertionError("dry-run must not send requests")

    code, report, _ = run(files, docker, no_network, "--dry-run", "--build-network", "host")
    assert code == 0 and report["status"] == "dry_run" and report["dry_run"]
    assert not docker.mutating()
    planned = [c["argv"] for c in report["commands"] if not c["executed"]]
    assert any(a[-1:] == ["build"] for a in planned) and any("up" in a for a in planned) and any("exec" in a for a in planned)
    assert all(not c["mutating"] for c in report["commands"] if c["executed"])
    assert report["smoke"]["checks"] == list(deploy.SMOKE_STEPS)
    assert report["backup"]["planned"] and not (files[0].parent / "backups").exists()


def test_build_network_is_applied_through_an_override_for_services_with_a_build(files):
    docker = FakeDocker()
    run(files, docker, FakeGateway(docker, good_image=NEW_APP), "--build-network", "host")
    build = next(c for c in docker.calls if c[-1:] == ["build"])
    assert build[:2] == ["docker", "compose"] and build.count("-f") == 2
    assert build[build.index("-f") + 1] == str(files[0]), "the operator's file comes first"
    assert docker.override_text == "services:\n  bridge:\n    build:\n      network: host\n" \
                                   "  initialize:\n    build:\n      network: host\n"
    up = docker.ups()[0]
    assert up.count("-f") == 1 and "build-network.yaml" not in " ".join(up)


def test_without_build_network_the_plain_compose_build_runs_and_no_build_skips_it(files):
    docker = FakeDocker()
    run(files, docker, FakeGateway(docker, good_image=OLD_APP))
    assert [c for c in docker.calls if c[-1:] == ["build"]][0][2:] == ["-f", str(files[0]), "-p", "demo", "build"]
    docker = FakeDocker()
    _, report, _ = run(files, docker, FakeGateway(docker, good_image=OLD_APP), "--no-build")
    assert report["build"] == {"skipped": True} and not [c for c in docker.calls if c[-1:] == ["build"]]


def test_operator_options_are_passed_to_every_compose_command(files, tmp_path):
    docker = FakeDocker()
    env = tmp_path / "env"
    env.write_text("")
    run(files, docker, FakeGateway(docker, good_image=NEW_APP), "--env-file", str(env), "--profile", "ops", "--wait-timeout", "90")
    compose_calls = [c for c in docker.calls if c[:2] == ["docker", "compose"]]
    assert compose_calls
    for call in compose_calls:
        assert call[2:4] == ["--env-file", str(env)] and ["-p", "demo", "--profile", "ops"] == call[call.index("-p"):call.index("-p") + 4]
    config = next(c for c in compose_calls if "config" in c)
    assert config.count("--profile") == 2 and "*" in config, "orphan detection sees every profile's services"
    up = next(c for c in compose_calls if "up" in c)
    assert up[up.index("--wait-timeout"):] == ["--wait-timeout", "90", "--force-recreate", "--remove-orphans"]


def test_plan_rollback_skips_digest_pinned_and_ambiguous_images_and_unchanged_tags():
    previous = {
        "bridge": {"image": APP_REF, "image_id": OLD_APP, "running": True},
        "initialize": {"image": APP_REF, "image_id": OLD_APP, "running": False},
        "edge": {"image": NGINX_REF, "image_id": "sha256:" + "c" * 64, "running": True},
        "odd": {"image": "other:local", "image_id": OLD_APP, "running": True},
        "odd2": {"image": "other:local", "image_id": NEW_APP, "running": True},
        "same": {"image": "same:local", "image_id": OLD_APP, "running": True},
    }
    retag, skipped = deploy.plan_rollback(previous, {APP_REF: NEW_APP, "same:local": OLD_APP})
    assert retag == [{"image": APP_REF, "image_id": OLD_APP, "services": ["bridge", "initialize"]}]
    assert {(s["image"], s["reason"]) for s in skipped} == {(NGINX_REF, "pinned by digest"),
                                                           ("other:local", "running services used different images")}


def test_plan_rollback_prefers_running_services_and_otherwise_the_newest_stopped_one():
    stale = {"image": APP_REF, "image_id": NEW_APP, "running": False, "created": "2026-01-01"}
    running = {"image": APP_REF, "image_id": OLD_APP, "running": True, "created": "2025-01-01"}
    assert deploy.plan_rollback({"a": stale, "b": running}, {})[0][0]["image_id"] == OLD_APP
    older = dict(stale, image_id=OLD_APP, created="2025-01-01")
    assert deploy.plan_rollback({"a": stale, "b": older}, {})[0][0]["image_id"] == NEW_APP


def test_verify_restored_reports_missing_and_different_images():
    previous = {"a": {"image_id": "x"}, "b": {"image_id": "y"}, "c": {"image_id": "z"}}
    assert deploy.verify_restored(previous, {"a": {"image_id": "x"}, "b": {"image_id": "other"}}) == [
        {"service": "b", "expected": "y", "actual": "other"}, {"service": "c", "expected": "z", "actual": None}]


@pytest.mark.parametrize("up_ok,smoke_ok,previous,expected", [
    (True, True, {"a": 1}, "deployed"), (True, True, {}, "deployed"),
    (True, False, {"a": 1}, "rollback"), (False, None, {"a": 1}, "rollback"),
    (True, False, {}, "fail"), (False, None, {}, "fail"),
])
def test_decide(up_ok, smoke_ok, previous, expected):
    smoke = None if smoke_ok is None else {"ok": smoke_ok}
    assert deploy.decide(up_ok, smoke, previous) == expected


def test_exit_codes_distinguish_restored_from_unrecovered():
    assert [deploy.exit_code(s) for s in ("deployed", "dry_run", "rolled_back", "build_failed", "aborted", "failed",
                                          "rollback_failed")] == [0, 0, 1, 1, 1, 3, 3]


def test_parse_inspect_keeps_one_entry_per_service_preferring_running_then_newest_and_skips_run_once_containers():
    def item(service, running, created, image_id=OLD_APP, oneoff="False"):
        return {"Id": service + created, "Image": image_id, "State": {"Running": running}, "Created": created,
                "Config": {"Image": APP_REF, "Labels": {deploy.SERVICE_LABEL: service, deploy.ONEOFF_LABEL: oneoff}}}

    items = [item("bridge", False, "2026-02"), item("bridge", True, "2026-01", NEW_APP), item("migrate", False, "2026-01"),
             item("migrate", False, "2026-03", NEW_APP), item("admin", False, "2026-01", oneoff="True"),
             {"Id": "x", "Image": OLD_APP, "State": {"Running": True}, "Config": {"Image": "stray", "Labels": {}}}]
    services = deploy.parse_inspect(json.dumps(items))
    assert sorted(services) == ["bridge", "migrate"]
    assert services["bridge"]["image_id"] == NEW_APP and services["bridge"]["running"]
    assert services["migrate"]["image_id"] == NEW_APP and services["migrate"]["created"] == "2026-03"


def test_subprocess_runner_kills_a_hung_command_and_its_children_at_the_timeout(tmp_path):
    marker = tmp_path / "child-survived"
    child = tmp_path / "child.py"
    child.write_text("import pathlib, time\ntime.sleep(3)\npathlib.Path(%r).write_text('x')\n" % str(marker))
    parent = tmp_path / "parent.py"
    parent.write_text("import subprocess, sys, time\nsubprocess.Popen([sys.executable, %r])\ntime.sleep(60)\n" % str(child))
    started = time.monotonic()
    result = deploy.SubprocessRunner()([sys.executable, str(parent)], timeout=1)
    assert result.timed_out and result.returncode == deploy.TIMEOUT_RETURNCODE and time.monotonic() - started < 10
    time.sleep(3.5)
    assert not marker.exists(), "the whole process group was killed"


def test_subprocess_runner_streams_stdout_to_a_private_file_and_reports_missing_commands(tmp_path):
    target = tmp_path / "out.bin"
    result = deploy.SubprocessRunner()([sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'PGDMP\\x00\\xff'); sys.stderr.write('warn')"],
                                       timeout=20, stdout_path=str(target))
    assert result.returncode == 0 and result.stderr == "warn" and target.read_bytes() == b"PGDMP\x00\xff"
    assert target.stat().st_mode & 0o777 == 0o600
    assert deploy.SubprocessRunner()(["definitely-not-a-command-xyz"], timeout=5).returncode == 127


def test_rollback_limits_are_documented_next_to_the_commands():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("### 重新部署、冒烟与回滚")[1].split("### 升级旧实例")[0]
    for needle in ("expand", "pg_dump", "--backup-dir", "迁移", "不恢复数据库", "--allow-remove-orphans", "超时", "孤立"):
        assert needle in section, needle
    assert "expand-only" in deploy.__doc__ and "backup" in deploy.__doc__ and "orphans" in deploy.__doc__


def test_run_smoke_retries_until_the_window_closes_or_the_checks_pass():
    state = {"now": 0, "calls": 0}

    def clock():
        return state["now"]

    def sleep(seconds):
        state["now"] += seconds

    def http(method, path, headers=None, body=None):
        state["calls"] += 1
        if path == "/board":
            return deploy.Response(200 if state["now"] >= 4 else 502, {}, b"")
        if "Authorization" not in (headers or {}):
            return deploy.Response(401, {}, b"")
        message = json.loads(body)
        result = {"initialize": {"serverInfo": {}}, "tools/list": {"tools": [{"name": "fetch_inbox"}]},
                  "tools/call": {"content": [{"type": "text", "text": "{}"}]}}
        if "id" not in message:
            return deploy.Response(202, {}, b"")
        return deploy.Response(200, {}, json.dumps({"id": message["id"], "result": result[message["method"]]}).encode())

    assert deploy.run_smoke(http, TOKEN, 30, clock, sleep, interval=2)["ok"] and state["now"] == 4
    state.update(now=0)
    never = deploy.run_smoke(lambda *a, **k: deploy.Response(502, {}, b""), TOKEN, 5, clock, sleep, interval=2)
    assert not never["ok"] and state["now"] >= 5


def test_mcp_session_reads_sse_replies_and_carries_the_session_and_protocol_headers():
    seen = []

    def http(method, path, headers=None, body=None):
        message = json.loads(body)
        seen.append((message.get("method"), dict(headers)))
        if "id" not in message:
            return deploy.Response(202, {}, b"")
        result = {"initialize": {"protocolVersion": "2025-03-26", "serverInfo": {}},
                  "tools/list": {"tools": [{"name": "fetch_inbox"}]},
                  "tools/call": {"content": [{"type": "text", "text": "{}"}]}}[message["method"]]
        event = "event: message\ndata: " + json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}) + "\n\n"
        return deploy.Response(200, {"content-type": "text/event-stream", "mcp-session-id": "sess-1"}, event.encode())

    session = deploy.McpSession(http, TOKEN)
    assert session.initialize() == "protocol 2025-03-26"
    assert session.list_tools() == "1 tools" and session.fetch_inbox() == "ok"
    assert [m for m, _ in seen] == ["initialize", "notifications/initialized", "tools/list", "tools/call"]
    assert "Mcp-Session-Id" not in seen[0][1] and all(h["Mcp-Session-Id"] == "sess-1" for _, h in seen[1:])
    assert all(h["MCP-Protocol-Version"] == "2025-03-26" for _, h in seen[1:])


@pytest.mark.parametrize("reply", [
    {"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": "secret detail"}},
    {"jsonrpc": "2.0", "id": 99, "result": {}},
])
def test_mcp_errors_become_short_details_without_response_bodies(reply):
    http = lambda *a, **k: deploy.Response(200, {}, json.dumps(reply).encode())
    with pytest.raises(deploy.SmokeError) as caught:
        deploy.McpSession(http, TOKEN).initialize()
    assert "secret detail" not in str(caught.value)


def test_list_tools_requires_fetch_inbox_and_fetch_inbox_must_not_be_an_error():
    def reply(result):
        return lambda m, p, h=None, b=None: deploy.Response(200, {}, json.dumps({"id": 1, "result": result}).encode())

    with pytest.raises(deploy.SmokeError, match="fetch_inbox"):
        deploy.McpSession(reply({"tools": [{"name": "submit_task"}]}), TOKEN).list_tools()
    with pytest.raises(deploy.SmokeError, match="error"):
        deploy.McpSession(reply({"isError": True, "content": [{"type": "text", "text": "x"}]}), TOKEN).fetch_inbox()


def test_unauthenticated_check_demands_exactly_401():
    for status in (200, 403, 502, 503):
        with pytest.raises(deploy.SmokeError, match=str(status)):
            deploy.check_unauthenticated(lambda *a, **k: deploy.Response(status, {}, b""))
    assert deploy.check_unauthenticated(lambda *a, **k: deploy.Response(401, {}, b"")) == "HTTP 401"


@pytest.mark.parametrize("argv,message", [
    (["--base-url", "http://baton.example.com:8080"], "plain http"),
    (["--base-url", "https://user:pw@127.0.0.1"], "invalid --base-url"),
    (["--base-url", "ftp://127.0.0.1"], "invalid --base-url"),
])
def test_unsafe_base_urls_are_refused_before_any_command_runs(files, argv, message):
    docker = FakeDocker()
    code, report, text = run(files, docker, FakeGateway(docker), *argv)
    assert code == 2 and message in report["error"] and not docker.calls
    assert "pw@" not in text


def test_missing_or_malformed_token_file_is_a_usage_error_that_does_not_echo_contents(files):
    compose, token = files
    docker = FakeDocker()
    token.write_text("two words\n")
    out = io.StringIO()
    code = deploy.main(["-f", str(compose), "--smoke-token-file", str(token), "--no-backup"], runner=docker,
                       http=FakeGateway(docker), stdout=out, stderr=io.StringIO())
    assert code == 2 and "two words" not in out.getvalue() and not docker.calls
    token.unlink()
    assert deploy.main(["-f", str(compose), "--smoke-token-file", str(token), "--no-backup"], runner=docker,
                       stdout=io.StringIO(), stderr=io.StringIO()) == 2
    assert deploy.main(["-f", str(compose) + ".missing", "--smoke-token-file", str(token), "--no-backup"], runner=docker,
                       stdout=io.StringIO(), stderr=io.StringIO()) == 2


def test_group_readable_token_file_is_reported_as_a_warning(files):
    files[1].chmod(0o644)
    docker = FakeDocker()
    _, report, _ = run(files, docker, FakeGateway(docker, good_image=NEW_APP))
    assert report["warnings"] and TOKEN not in json.dumps(report)


class StubStack(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *args):
        pass

    def reply(self, status, body=b"", content_type="application/json", extra=()):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in extra:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.seen.append(("GET", self.path, self.headers.get("Authorization")))
        if self.path == "/board":
            self.reply(200, b"<html>", "text/html")
        else:
            self.reply(302, b"", extra=[("Location", "/board")])

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        self.seen.append(("POST", self.path, self.headers.get("Authorization")))
        if self.headers.get("Authorization") != "Bearer " + TOKEN:
            return self.reply(401)
        if "id" not in body:
            return self.reply(202)
        result = {"initialize": {"protocolVersion": "2025-06-18", "serverInfo": {"name": "stub"}},
                  "tools/list": {"tools": [{"name": "fetch_inbox"}]},
                  "tools/call": {"content": [{"type": "text", "text": "{}"}], "isError": False}}[body["method"]]
        event = "data: " + json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result}) + "\n\n"
        self.reply(200, event.encode(), "text/event-stream")


def test_http_client_runs_the_whole_smoke_against_a_real_local_server():
    StubStack.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), StubStack)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = "http://127.0.0.1:%d" % server.server_address[1]
        client = deploy.HttpClient(base, timeout=5)
        assert client("GET", "/redirect").status == 302, "redirects are reported, never followed"
        result = deploy.run_smoke(client, TOKEN, 0)
        assert result["ok"], result
        assert [(m, p) for m, p, _ in StubStack.seen if p == "/mcp"][0] == ("POST", "/mcp")
        assert TOKEN not in json.dumps(result)
        server.shutdown()
        with pytest.raises(deploy.SmokeError, match="request failed"):
            client("GET", "/board")
    finally:
        server.server_close()
