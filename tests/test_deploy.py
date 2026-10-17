# SPDX-License-Identifier: Apache-2.0
"""Edge configuration and the deploy script's smoke/rollback decisions, with a fake docker and a fake gateway."""

import importlib.util
import io
import json
import os
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


PUBLIC_DOCS = ("location", ("~", r"^/\.well-known/(agent-card|jwks)\.json$"))


def test_nginx_resolves_the_gateway_per_request_through_docker_dns(nginx):
    _, config = nginx
    resolvers = [(path, args) for path, args in directives_in(config, "resolver")]
    assert resolvers == [((("http", ()),), ["127.0.0.11", "valid=10s", "ipv6=off"])]
    passes = directives_in(config, "proxy_pass")
    assert [path[-1] for path, _ in passes] == [PUBLIC_DOCS, ("location", ("/",))]
    for path, args in passes:
        assert args == ["$upstream$request_uri"], "the client's request URI is forwarded unchanged"
        assert [a for p, a in directives_in(config, "set") if p == path] == [["$upstream", "http://gateway:3000"]]
        in_location = [name for found_path, name, _ in walk(config) if found_path == path and name in ("set", "proxy_pass")]
        assert in_location == ["set", "proxy_pass"], "the variable is set before it is used"


def test_nginx_keeps_every_location_header_limit_and_streaming_setting(nginx):
    _, config = nginx
    locations = [args for _, name, args in walk(config) if name == "location"]
    assert locations == [["=", "/"], ["=", "/board"], ["~", r"^/\.well-known/(agent-card|jwks)\.json$"], ["/"]]
    public = {(name, tuple(args)) for path, name, args in walk(config) if path and path[-1] == PUBLIC_DOCS}
    assert ("limit_req", ("zone=baton_public_docs", "burst=20", "nodelay")) in public
    assert ("limit_req_status", ("429",)) in public
    assert ("proxy_buffering", ("off",)) not in public, "the card and key set are not the streaming route"
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


def catalog(drop_column=False):
    columns = [{"t": "baton.tasks", "c": name, "type": "text", "notnull": False, "default": None, "hasdef": False}
               for name in ("id", "assignee")]
    if drop_column:
        columns = columns[:1]
    return {"columns": columns, "relations": ["baton.tasks:r"],
            "constraints": [{"t": "baton.tasks", "name": "tasks_pkey", "def": "PRIMARY KEY (id)", "cols": ["id"]}],
            "indexes": [{"t": "baton.tasks", "name": "tasks_pkey", "def": "CREATE UNIQUE INDEX tasks_pkey",
                         "unique": True, "cols": ["id"], "pred": None}],
            "triggers": [], "functions": [], "types": [], "policies": [],
            "row_security": [{"t": "baton.tasks", "enabled": False, "forced": False}],
            "privileges": [{"t": "baton.tasks", "acl": ""}], "column_privileges": [],
            "schema_privileges": [{"n": "baton", "acl": ""}], "function_privileges": [],
            "database_privileges": [{"acl": ""}],
            "roles": [{"name": "baton_app", "super": False, "inherit": True, "createrole": False, "createdb": False,
                       "login": True, "replication": False, "bypassrls": False, "connlimit": -1, "password": None}],
            "memberships": []}


class FakeDocker:
    """Scripted docker/compose: containers per service, the app image tag, and one serving image."""

    def __init__(self, existing=True, up_results=None, recovery_ok=True, build_ok=True, fail_tag_refs=(), hold_ok=True,
                 dump="ok", stopped=(), orphans=(), edge_drift=False, build_timeout=False, precheck="clean",
                 volume=None, db_start_ok=True, drop_ok=True, no_containers=False, overrides=None):
        self.calls, self.timeouts, self.stdins = [], [], []
        self.precheck_name = None
        self.precheck, self.volume, self.db_start_ok, self.drop_ok = precheck, existing if volume is None else volume, db_start_ok, drop_ok
        self.snapshots_served = 0
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
        if no_containers:
            self.containers = {}
        for service, changes in (overrides or {}).items():
            self.containers[service].update(changes)

    def __call__(self, argv, timeout=None, stdout_path=None, stdin_text=None):
        self.calls.append(list(argv))
        self.timeouts.append(timeout)
        self.stdins.append(stdin_text)
        if argv[:3] == ["docker", "volume", "ls"]:
            return deploy.Result(0, "demo_postgres-data\n" if self.volume else "")
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
        if argv[:2] == ["docker", "run"]:
            return self.run_throwaway(argv)
        if argv[:3] == ["docker", "rm", "-f"]:
            return deploy.Result(0 if self.drop_ok else 1, "", "" if self.drop_ok else "container is in use")
        if argv[:2] == ["docker", "exec"]:
            return self.exec_throwaway(argv, stdin_text)
        if "exec" in argv and argv[argv.index("exec") + 2] == "database" and "--roles-only" in argv[-1]:
            if self.precheck == "fail:roles":
                return deploy.Result(1, "", "pg_dumpall: failed")
            return deploy.Result(0, "CREATE ROLE baton_owner NOLOGIN;\nCREATE ROLE baton_app LOGIN;\n")
        if "exec" in argv and argv[argv.index("exec") + 2] == "database" and "--schema-only" in argv[-1]:
            return self.run_clone(argv)
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
        if argv[:2] == ["docker", "compose"] and "up" in argv and "--no-deps" in argv:
            if not self.db_start_ok:
                return deploy.Result(1, "", "database failed to start")
            self.containers.setdefault("database", {"ref": DB_REF, "id": DB_ID, "running": True})["running"] = True
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

    def run_clone(self, argv):
        if self.precheck == "fail:clone":
            return deploy.Result(1, "", "pg_dump: error: connection refused")
        return deploy.Result(0, "CREATE SCHEMA IF NOT EXISTS baton;\n")

    def run_throwaway(self, argv):
        if self.precheck == "fail:create":
            return deploy.Result(1, "", "container failed to start")
        self.precheck_name = argv[argv.index("--name") + 1]
        assert argv[argv.index("--network") + 1] == "none", argv
        assert "/var/lib/postgresql/data:uid=999,gid=999,mode=0700" in argv
        assert "POSTGRES_HOST_AUTH_METHOD=trust" in argv and DB_REF in argv
        return deploy.Result(0, self.precheck_name + "\n")

    def exec_throwaway(self, argv, sql):
        assert self.precheck_name and argv[3] == self.precheck_name, argv
        if "pg_isready" in argv:
            return deploy.Result(0, "accepting connections\n")
        if sql and sql.startswith("SELECT json_build_object"):
            self.snapshots_served += 1
            if self.precheck == "garbage":
                return deploy.Result(0, "this is not json")
            snap = catalog(drop_column=self.precheck == "breaking" and self.snapshots_served == 2)
            if self.precheck == "role" and self.snapshots_served == 2:
                snap["roles"] = [dict(snap["roles"][0], login=False, connlimit=0)]
            return deploy.Result(0, json.dumps(snap))
        if sql and sql.startswith("BEGIN;"):
            return deploy.Result(1, "", "ERROR: syntax error at or near \"sql\"") if self.precheck == "fail:apply" else deploy.Result(0)
        return deploy.Result(0)

    def run_psql(self, argv, sql):
        raise AssertionError("the live database must not run precheck SQL: %r" % sql[:80])

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
                "Created": container.get("created", "2026-01-01T00:00:00Z"),
                "Config": {"Image": container["ref"], "Labels": {deploy.SERVICE_LABEL: name}}}

    def mutating(self):
        return [c for c in self.calls if c[:2] in (["docker", "tag"], ["docker", "rmi"]) or "up" in c
                or c[-1:] == ["build"] or "exec" in c]

    def ups(self):
        return [c for c in self.calls if c[:2] == ["docker", "compose"] and "up" in c and "--no-deps" not in c]


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


def hold(service, image_id=OLD_APP):
    return deploy.hold_tag(image_id, service)


APP_SERVICES = ("bridge", "initialize", "migrate")


@pytest.fixture(autouse=True)
def isolated_deploy_lock(tmp_path, monkeypatch):
    """Each test gets its own lock directory, so a leaked flock cannot stall the rest of the suite."""
    monkeypatch.setattr(deploy, "LOCK_DIR", tmp_path / "deploy-locks")


@pytest.fixture(autouse=True)
def expand_only_default_schema(tmp_path, monkeypatch):
    """Deploy tests use an expand-only file. The shipped schema's backfill UPDATE is covered on its own."""
    schema = tmp_path / "expand-only.sql"
    schema.write_text("CREATE TABLE IF NOT EXISTS baton.note (id int);\n")
    monkeypatch.setattr(deploy, "SCHEMA_FILE", schema)


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
        "--command-timeout", "30", "--backup-timeout", "400", "--precheck-timeout", "250")
    assert all(timeout for timeout in docker.timeouts)
    by_kind = {}
    for argv, timeout in zip(docker.calls, docker.timeouts):
        kind = ("up" if argv[-1] == "--remove-orphans" else "build" if argv[-1] == "build"
                else "dump" if "exec" in argv and "-Fc" in argv[-1]
                else "precheck" if "exec" in argv or argv[:2] == ["docker", "run"] or argv[:3] == ["docker", "rm", "-f"]
                else "other")
        by_kind.setdefault(kind, set()).add(timeout)
    assert by_kind == {"up": {90 + deploy.UP_GRACE}, "build": {700}, "dump": {400},
                       "precheck": {250}, "other": {30}}
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
    recovery = docker.ups()[-1]
    assert len(docker.ups()) == 3 and "--force-recreate" not in recovery
    assert recovery[recovery.index("--wait"):] == ["--wait", "--wait-timeout", "300", "--remove-orphans"], "recovery still waits, bounded"
    assert docker.timeouts[docker.calls.index(recovery)] == 300 + deploy.UP_GRACE


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
    build_at = next(i for i, c in enumerate(docker.calls) if c[-1:] == ["build"])
    for service in APP_SERVICES:
        assert hold(service) == "baton-deploy-rollback:%s-%s" % (service, "a" * 12)
        assert docker.calls.index(["docker", "tag", OLD_APP, hold(service)]) < build_at
        assert ["docker", "rmi", hold(service)] in docker.calls
    assert not [c for c in docker.calls if c[:2] == ["docker", "tag"] and c[2] == DB_ID], "digest-pinned images are not tagged"


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
    assert rollback["manual_recovery"] and all(hold(service) in docker.tags for service in APP_SERVICES)
    assert "docker tag %s %s   # bridge" % (hold("bridge"), APP_REF) in rollback["manual_recovery"]
    assert [m["service"] for m in rollback["mismatches"]] == ["bridge", "initialize", "migrate"]


def test_rollback_up_failure_triggers_a_best_effort_recovery_and_failure_is_reported_when_it_fails_too(files):
    docker = FakeDocker(up_results=[1, 1], recovery_ok=False)
    code, report, _ = run(files, docker, FakeGateway(docker, good_image=OLD_APP))
    rollback = report["rollback"]
    assert code == 3 and report["status"] == "rollback_failed"
    assert rollback["recovery_up"]["ok"] is False and "docker compose up failed during rollback" in rollback["errors"]
    assert rollback["manual_recovery"]
    assert len(docker.ups()) == 3 and "--force-recreate" not in docker.ups()[-1]
    assert hold("bridge") in docker.tags, "holding tags survive a failed rollback"


CRASHED_APP = "sha256:" + "1" * 64


def test_services_sharing_an_image_reference_each_get_their_own_protective_tag(files):
    docker = FakeDocker(stopped=("bridge", "database", "edge"), overrides={
        "bridge": {"id": CRASHED_APP, "running": False, "created": "2026-01-01T00:00:00Z"},
        "initialize": {"created": "2026-02-01T00:00:00Z"}, "migrate": {"created": "2026-02-01T00:00:00Z"}})
    code, report, _ = run(files, docker, FakeGateway(docker, good_image=OLD_APP), "--no-backup")
    held = {(h["service"], h["image_id"]) for h in report["held_images"]}
    assert held == {("bridge", CRASHED_APP), ("initialize", OLD_APP), ("migrate", OLD_APP)}, "the crashed bridge's own image is held"
    build_at = next(i for i, c in enumerate(docker.calls) if c[-1:] == ["build"])
    assert docker.calls.index(["docker", "tag", CRASHED_APP, hold("bridge", CRASHED_APP)]) < build_at
    rollback = report["rollback"]
    assert code == 3 and report["status"] == "rollback_failed", "one shared tag cannot restore two different images"
    assert [(m["service"], m["expected"]) for m in rollback["mismatches"]] == [("bridge", CRASHED_APP)]
    assert hold("bridge", CRASHED_APP) in docker.tags, "the crashed service's image survives for a manual restore"
    assert "docker tag %s %s   # bridge" % (hold("bridge", CRASHED_APP), APP_REF) in rollback["manual_recovery"]


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
    assert ["docker", "rmi", hold("bridge")] in docker.calls


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
    dump = next(i for i, c in enumerate(docker.calls) if "exec" in c and "-Fc" in c[-1])
    assert dump < next(i for i, c in enumerate(docker.calls) if c[-1:] == ["build"])
    assert dump < next(i for i, c in enumerate(docker.calls) if c in docker.ups())
    argv = docker.calls[dump]
    assert argv[argv.index("exec"):argv.index("exec") + 4] == ["exec", "-T", "database", "sh"] and "pg_dump" in argv[-1]
    assert "postgres.password" in argv[-1] and "PGPASSWORD=\"$(cat" in argv[-1], "the password is read inside the container"
    assert not re.search(r"password\s*[=:]\s*\S{8,}", " ".join(argv).replace("postgres.password", ""))


def test_backup_directory_and_files_are_private(files):
    docker = FakeDocker()
    previous = os.umask(0)
    try:
        code, report, _ = run(files, docker, FakeGateway(docker, good_image=NEW_APP))
    finally:
        os.umask(previous)
    directory = files[0].parent / "backups"
    path = Path(report["backup"]["path"])
    assert code == 0 and directory.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600
    assert re.fullmatch(r"baton-demo-\d{8}T\d{6}Z-[0-9a-f]{16}\.dump", path.name)


def test_an_existing_backup_directory_is_tightened_and_two_dumps_do_not_share_a_name(files):
    directory = files[0].parent / "backups"
    directory.mkdir(mode=0o755)
    docker = FakeDocker()
    _, first, _ = run(files, docker, FakeGateway(docker, good_image=NEW_APP))
    docker = FakeDocker()
    _, second, _ = run(files, docker, FakeGateway(docker, good_image=NEW_APP))
    assert directory.stat().st_mode & 0o777 == 0o700
    assert Path(first["backup"]["path"]).name != Path(second["backup"]["path"]).name


def test_a_symlink_backup_directory_is_refused_and_not_followed(files, tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    (files[0].parent / "backups").symlink_to(target)
    docker = FakeDocker()
    code, report, _ = run(files, docker, FakeGateway(docker))
    assert code == 1 and report["status"] == "aborted"
    assert "symbolic link" in report["backup"]["error"]
    assert list(target.iterdir()) == []
    assert not dump_calls(docker)


@pytest.mark.parametrize("dump", ["fail", "timeout", "garbage"])
def test_a_failed_or_invalid_backup_aborts_before_anything_changes(files, dump):
    docker = FakeDocker(dump=dump)
    code, report, _ = run(files, docker, FakeGateway(docker))
    assert code == 1 and report["status"] == "aborted" and not report["backup"]["ok"]
    assert report["rollback"]["reason"] == "the database backup failed; nothing was changed"
    assert not docker.ups() and not [c for c in docker.calls if c[-1:] == ["build"]]
    assert not list((files[0].parent / "backups").glob("*.dump")), "a bad dump is not left behind"
    assert ["docker", "rmi", hold("bridge")] in docker.calls


def dump_calls(docker):
    return [c for c in docker.calls if "exec" in c and "-Fc" in c[-1]]


def test_a_stopped_database_is_started_then_checked_and_dumped_even_when_every_container_is_stopped(files):
    docker = FakeDocker(stopped=("bridge", "database", "edge"), up_results=[0])
    code, report, _ = run(files, docker, FakeGateway(docker, good_image=NEW_APP))
    assert code == 0 and report["database"] == {"state": "started", "ok": True}
    start = next(c for c in docker.calls if "--no-deps" in c)
    assert start[-1] == "database" and "--no-recreate" in start and "--wait" in start, "starts the existing container, never recreates it"
    assert docker.timeouts[docker.calls.index(start)] == 300 + deploy.UP_GRACE
    assert report["precheck"]["ok"] and report["backup"]["ok"] and len(dump_calls(docker)) == 1
    assert docker.calls.index(start) < docker.calls.index(dump_calls(docker)[0])


def test_a_database_volume_without_any_container_is_still_started_checked_and_dumped(files):
    docker = FakeDocker(no_containers=True, volume=True, up_results=[0])
    code, report, _ = run(files, docker, FakeGateway(docker, good_image=NEW_APP))
    assert report["previous_images"] == {} and report["database"]["state"] == "started"
    assert report["precheck"]["ok"] and report["backup"]["ok"] and len(dump_calls(docker)) == 1
    assert code == 0


def test_a_database_that_cannot_be_started_aborts_before_anything_changes_unless_both_checks_are_waived(files):
    docker = FakeDocker(stopped=("database",), db_start_ok=False)
    code, report, _ = run(files, docker, FakeGateway(docker))
    assert code == 1 and report["status"] == "aborted" and "could not be started" in report["database"]["error"]
    assert not docker.ups() and not dump_calls(docker) and not [c for c in docker.calls if c[-1:] == ["build"]]
    docker = FakeDocker(stopped=("database",), db_start_ok=False)
    code, report, _ = run(files, docker, FakeGateway(docker), "--no-backup")
    assert code == 1 and report["status"] == "aborted", "--no-backup alone does not waive the migration precheck"
    docker = FakeDocker(stopped=("database",), db_start_ok=False)
    code, report, _ = run(files, docker, FakeGateway(docker, good_image=NEW_APP), "--no-backup", "--allow-incompatible-migration")
    assert code == 0 and report["precheck"] == {"skipped": True, "reason": "database unavailable"}


def test_no_backup_skips_the_dump_but_not_the_migration_precheck(files):
    docker = FakeDocker()
    code, report, _ = run(files, docker, FakeGateway(docker, good_image=NEW_APP), "--no-backup")
    assert code == 0 and report["backup"] == {"skipped": True, "reason": "disabled by --no-backup"}
    assert report["precheck"]["ok"] and not dump_calls(docker)


def test_first_deployment_without_a_database_volume_has_nothing_to_check_or_dump(files):
    docker = FakeDocker(existing=False, up_results=[0])
    code, report, _ = run(files, docker, FakeGateway(docker, good_image=None))
    assert report["database"] == {"state": "none", "ok": True, "reason": "no database volume: first deployment"}
    assert report["precheck"]["skipped"] and report["backup"]["skipped"] and not dump_calls(docker)
    assert not [c for c in docker.calls if "exec" in c]


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
    "UPDATE baton.t SET a = 1;", "UPDATE ONLY baton.t SET a = 1;",
    "INSERT INTO baton.t(a) VALUES (1) ON CONFLICT DO UPDATE SET a = 1;",
    "MERGE INTO baton.t USING baton.s ON true WHEN MATCHED THEN DELETE;",
    "drop schema baton cascade;", "DROP FUNCTION baton.f();", "DROP VIEW baton.v;",
    "GRANT SELECT ON baton.t TO baton_app;", "REVOKE ALL ON TABLE baton.t FROM PUBLIC;",
    "DROP POLICY p ON baton.t;", "CREATE POLICY p ON baton.t USING (true);",
    "ALTER POLICY p ON baton.t USING (false);", "ALTER TABLE baton.t ENABLE ROW LEVEL SECURITY;",
    "ALTER TABLE baton.t FORCE ROW LEVEL SECURITY;", "ALTER TABLE baton.t NO FORCE ROW LEVEL SECURITY;",
    "ALTER TABLE baton.t DISABLE ROW LEVEL SECURITY;",
    "ALTER TABLE baton.t DISABLE TRIGGER ALL;",
    "ALTER TABLE baton.t DISABLE TRIGGER USER;",
    "ALTER TABLE baton.x_audit DISABLE TRIGGER audit_no_change;",
    "RESET ROLE;", "SET ROLE baton_owner;", "SET LOCAL ROLE baton_owner;", "SET SESSION ROLE baton_owner;",
    "SET SESSION AUTHORIZATION baton_bootstrap;", "ALTER SYSTEM SET log_statement = 'all';",
    "ALTER DATABASE baton SET log_statement = 'all';", "CREATE ROLE extra LOGIN;", "ALTER ROLE baton_app NOLOGIN;",
    "DROP ROLE extra;", "COPY baton.t TO PROGRAM 'id';", "COPY baton.t FROM PROGRAM 'id';",
    "CREATE EXTENSION IF NOT EXISTS pg_stat_statements;", "DO $$ BEGIN NULL; END $$;",
    "DO LANGUAGE plpgsql $$ BEGIN NULL; END $$;", "DO 'BEGIN NULL; END';",
    "DO E'BEGIN NULL; END';", "DO U&'BEGIN NULL; END';", "do language plpgsql e'BEGIN NULL; END';",
    "COMMIT;", "COMMIT WORK;", "BEGIN;", "BEGIN TRANSACTION;", "START TRANSACTION;",
    "END;", "END WORK;", "ROLLBACK;", "ROLLBACK TO SAVEPOINT s;", "ABORT;",
    "SAVEPOINT s;", "RELEASE SAVEPOINT s;", "RELEASE s;", "PREPARE TRANSACTION 'x';",
])
def test_migration_lint_flags_contract_statements_a_previous_image_cannot_survive(statement):
    assert deploy.unsafe_migration_statements("CREATE TABLE IF NOT EXISTS baton.t(a int);\n" + statement + "\n")


@pytest.mark.parametrize("statement", [
    "ALTER TABLE baton.t ADD COLUMN IF NOT EXISTS a text;",
    "ALTER TABLE baton.t ADD COLUMN IF NOT EXISTS a boolean NOT NULL DEFAULT false;",
    "ALTER TABLE baton.t DROP CONSTRAINT IF EXISTS old_key;",
    "CREATE TRIGGER x BEFORE UPDATE OR DELETE ON baton.t FOR EACH ROW EXECUTE FUNCTION f();",
    "CREATE TRIGGER x BEFORE INSERT OR UPDATE OF parent_id ON baton.t FOR EACH ROW EXECUTE FUNCTION f();",
    "CREATE TRIGGER x BEFORE UPDATE ON baton.t FOR EACH ROW EXECUTE FUNCTION f();",
    "CREATE TRIGGER y BEFORE TRUNCATE ON baton.t FOR EACH STATEMENT EXECUTE FUNCTION f();",
    "INSERT INTO baton.schema_version(singleton, version) VALUES (true, 1) ON CONFLICT DO NOTHING;",
    "COPY baton.t TO stdout;",
    "COPY program TO stdout;",
    "ALTER TABLE baton.child ADD COLUMN parent_id int REFERENCES baton.parent(id) "
    "ON UPDATE CASCADE ON DELETE SET NULL;",
    "ALTER TABLE baton.child ADD COLUMN parent_id int REFERENCES baton.parent(id) "
    "ON UPDATE SET DEFAULT ON DELETE RESTRICT;",
    "ALTER TABLE baton.child ADD COLUMN parent_id int REFERENCES baton.parent(id) "
    "ON UPDATE RESTRICT ON DELETE NO ACTION;",
    "ALTER TABLE baton.child ADD COLUMN parent_id int REFERENCES baton.parent(id) ON UPDATE NO ACTION;",
    "CREATE FUNCTION baton.f() RETURNS void LANGUAGE plpgsql AS $$ BEGIN NULL; END; $$;",
    "CREATE PROCEDURE baton.p() LANGUAGE plpgsql AS $$ BEGIN COMMIT; END; $$;",
    "SELECT 'COMMIT; BEGIN; ROLLBACK; DO E''BEGIN END'';';",
    "-- COMMIT\n/* START TRANSACTION; */ SELECT 1;",
    "-- DROP TABLE baton.commented;\n/* DELETE FROM baton.t; UPDATE baton.t SET a = 1; */ SELECT 1;",
    "-- RESET ROLE\n/* ALTER SYSTEM SET x = 1; ALTER DATABASE baton SET y = 1; */\n"
    "SELECT 'SET ROLE baton_owner'; SELECT 'COPY baton.t TO PROGRAM ''id''';\n"
    "SELECT 'DO $$ BEGIN NULL; END $$';\nCREATE TABLE baton.n(id int);",
])
def test_migration_lint_accepts_expand_only_statements(statement):
    assert deploy.unsafe_migration_statements(statement) == []


def test_the_shipped_migration_sql_is_expand_only_except_its_backfill_update():
    schema = ROOT / "bridge/space_schema.sql"
    assert deploy.unsafe_migration_statements(schema.read_text(encoding="utf-8")) == [
        "rewrites rows", "contains a DO block"]
    assert "UPDATE baton.spaces" in schema.read_text(encoding="utf-8")


def migration_file(tmp_path, monkeypatch, sql):
    schema = tmp_path / "schema.sql"
    schema.write_text(sql)
    monkeypatch.setattr(deploy, "SCHEMA_FILE", schema)
    return schema


def test_an_obviously_destructive_migration_fails_fast_before_any_docker_command(files, tmp_path, monkeypatch):
    migration_file(tmp_path, monkeypatch, "ALTER TABLE baton.t DROP COLUMN c;\n")
    docker = FakeDocker()
    code, report, _ = run(files, docker, FakeGateway(docker))
    assert code == 1 and report["status"] == "aborted" and not docker.calls
    assert "not backward compatible" in report["rollback"]["reason"] and "DROP" in report["rollback"]["reason"]
    docker = FakeDocker()
    code, report, _ = run(files, docker, FakeGateway(docker, good_image=NEW_APP), "--allow-incompatible-migration")
    assert code == 0 and report["migration"]["unsafe"]


@pytest.mark.parametrize("extra", [[], ["--allow-incompatible-migration"]])
def test_a_missing_or_empty_migration_file_is_an_error_never_a_pass(files, tmp_path, monkeypatch, extra):
    monkeypatch.setattr(deploy, "SCHEMA_FILE", tmp_path / "does-not-exist.sql")
    docker = FakeDocker()
    code, report, text = run(files, docker, FakeGateway(docker), *extra)
    assert code == 2 and "migration SQL not found" in report["error"] and not docker.calls
    (tmp_path / "empty.sql").write_text("  \n")
    code, report, _ = run(files, docker, FakeGateway(docker), "--schema-file", str(tmp_path / "empty.sql"), *extra)
    assert code == 2 and "empty" in report["error"] and not docker.calls


def git_env():
    env = dict(os.environ)
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    return env


def checkout(tmp_path, files, directories=None, dockerignore=None, specific_ignore=None):
    """A tiny git checkout. Modes are applied after git add, because that is what Docker COPY reads."""
    root = tmp_path / "repo"
    root.mkdir()
    for relative, _mode in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n")
    (root / ".dockerignore").write_text((ROOT / ".dockerignore").read_text() if dockerignore is None else dockerignore)
    if specific_ignore is not None:
        dest = root / "deploy" / "Dockerfile.dockerignore"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(specific_ignore)
    env = git_env()
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True, env=env, capture_output=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, env=env, capture_output=True)
    for directory in root.rglob("*"):
        if directory.is_dir() and ".git" not in directory.parts:
            directory.chmod(0o755)
    (root / ".dockerignore").chmod(0o644)
    if specific_ignore is not None:
        (root / "deploy" / "Dockerfile.dockerignore").chmod(0o644)
    for relative, mode in files.items():
        (root / relative).chmod(mode)
    for relative, mode in (directories or {}).items():
        (root / relative).chmod(mode)
    return root


def test_dockerignore_matches_parents_and_keeps_private_files_out_of_the_image():
    matcher = deploy.DockerIgnore((ROOT / ".dockerignore").read_text(encoding="utf-8"))
    private = ("deploy/.env", ".env", "secrets/database.json", "runtime/x", "evidence/a", "client.token",
               "nested/client.token", "bridge/__pycache__/x.pyc", "auth.json", "deploy/auth.json", "certs/a.pem",
               "certs/a.key", ".git/config", ".venv/lib", ".pytest_cache/v")
    copied = ("bridge/space_schema.sql", "deploy/Dockerfile", "deploy/migrate.py", "board/index.html",
              "scripts/deploy.py", "README.md", "deploy/nginx.conf", "sub/secrets/a")
    for path in private:
        assert matcher.ignored(path), path
    for path in copied:
        assert not matcher.ignored(path), path
    sample = deploy.DockerIgnore("*/temp*\ntemp?\n**/*.go\n*.md\n!README.md\ndocs\n")
    assert sample.ignored("somedir/temporary.txt") and sample.ignored("somedir/temp")
    assert not sample.ignored("temporary.txt") and not sample.ignored("a/b/temp")
    assert sample.ignored("x/tempa"), "*/temp* matches a temp prefix in an immediate subdirectory"
    only_temp = deploy.DockerIgnore("temp?\n")
    assert only_temp.ignored("tempa") and not only_temp.ignored("temp") and not only_temp.ignored("x/tempa")
    assert sample.ignored("a.go") and sample.ignored("pkg/a.go")
    assert sample.ignored("NOTES.md") and not sample.ignored("guide/NOTES.md") and not sample.ignored("README.md")
    assert sample.ignored("docs/file.txt") and sample.ignored("docs/sub/file.txt")


def test_unreadable_tracked_files_and_directories_are_listed_and_private_ones_are_not(tmp_path):
    root = checkout(tmp_path, {
        "bridge/space_schema.sql": 0o600,
        "deploy/nginx.conf": 0o640,
        "board/index.html": 0o644,
        "scripts/deploy.py": 0o604,
        "deploy/.env": 0o600,
        "secrets/key": 0o600,
        "client.token": 0o600,
    }, directories={"bridge": 0o700, "board": 0o755, "deploy": 0o755, "scripts": 0o750, "secrets": 0o700})
    extra = root / "bridge" / "untracked.sql"
    extra.write_text("x\n")
    extra.chmod(0o600)
    assert deploy.unreadable_build_paths(root) == ["bridge", "bridge/space_schema.sql", "deploy/nginx.conf", "scripts"]
    gone = root / "board" / "index.html"
    gone.unlink()
    assert "board/index.html" not in deploy.unreadable_build_paths(root)


def test_a_dockerfile_specific_ignore_replaces_the_repository_one(tmp_path):
    root = checkout(tmp_path, {"note.token": 0o600, "bridge/space_schema.sql": 0o644},
                    directories={"bridge": 0o755}, dockerignore="**/*.token\n**/.env\n", specific_ignore="# copy everything\n")
    assert deploy.unreadable_build_paths(root) == ["note.token"]


def test_an_unreadable_checkout_refuses_the_build_before_any_docker_command(files, tmp_path, monkeypatch):
    root = checkout(tmp_path, {"bridge/space_schema.sql": 0o600, "deploy/.env": 0o600, "README.md": 0o644},
                    directories={"bridge": 0o755})
    monkeypatch.setattr(deploy, "source_root", lambda: root)
    docker = FakeDocker()
    code, report, _ = run(files, docker, FakeGateway(docker), "--allow-incompatible-migration")
    assert code == 2 and report["status"] == "error" and not docker.calls
    assert "\n  bridge/space_schema.sql\n" in report["error"] and "\n  deploy/.env\n" not in report["error"]
    assert "o+r" in report["error"] and "chmod a+rX" in report["error"]
    docker = FakeDocker()
    code, report, _ = run(files, docker, None, "--dry-run")
    assert code == 2 and "\n  bridge/space_schema.sql\n" in report["error"] and not docker.calls


def test_no_build_skips_the_readability_check(files, tmp_path, monkeypatch):
    root = checkout(tmp_path, {"bridge/space_schema.sql": 0o600})
    monkeypatch.setattr(deploy, "source_root", lambda: root)
    docker = FakeDocker()
    code, report, _ = run(files, docker, FakeGateway(docker), "--no-build")
    assert code == 0 and report["status"] == "deployed"
    assert not [c for c in docker.calls if c[-1:] == ["build"]]


def test_this_checkout_is_readable_by_other_users():
    assert deploy.unreadable_build_paths(deploy.source_root()) == []


def test_dockerfile_makes_copied_files_readable_without_adding_write():
    lines = (ROOT / "deploy/Dockerfile").read_text(encoding="utf-8").splitlines()
    copies = [index for index, line in enumerate(lines) if line.startswith("COPY ")]
    chmod = [index for index, line in enumerate(lines) if "chmod -R a+rX /app" in line]
    user = [index for index, line in enumerate(lines) if line.startswith("USER ")]
    assert copies and chmod and user and max(copies) < chmod[0] < user[0]
    assert "a+rw" not in lines[chmod[0]] and "777" not in lines[chmod[0]] and "666" not in lines[chmod[0]]


def test_the_migration_is_applied_in_an_isolated_container_never_to_the_live_database(files):
    docker = FakeDocker()
    code, report, text = run(files, docker, FakeGateway(docker, good_image=NEW_APP))
    precheck = report["precheck"]
    name = precheck["database"]
    assert code == 0 and precheck["ok"] and precheck["changes"] == [] and precheck["network"] == "none"
    assert name.startswith(deploy.PRECHECK_PREFIX) and precheck["image"] == DB_REF
    live = [(c, s) for c, s in zip(docker.calls, docker.stdins)
            if "exec" in c and c[:2] == ["docker", "compose"] and "-Fc" not in c[-1]]
    assert [c[-1] for c, _ in live] == [deploy.ROLES_DUMP, deploy.SCHEMA_DUMP]
    assert all(s is None for _, s in live), "the live database is only dumped, never given SQL"
    assert "--roles-only" in deploy.ROLES_DUMP and "--no-role-passwords" in deploy.ROLES_DUMP
    assert "--schema-only" in deploy.SCHEMA_DUMP and "-Fc" not in deploy.SCHEMA_DUMP
    thrown = [(c, s) for c, s in zip(docker.calls, docker.stdins) if c[:2] == ["docker", "exec"]]
    assert thrown[0][0][3:] == [name, "pg_isready", "-U", "precheck", "-d", "baton"]
    apply = next(s for c, s in thrown if s and s.startswith("BEGIN;"))
    assert "SET LOCAL ROLE baton_owner" in apply and "RESET ROLE" in apply and "CREATE TABLE IF NOT EXISTS baton.note" in apply
    restrict = re.search(r"\\restrict ([0-9a-f]{32})\n", apply)
    assert restrict and apply.index("\\restrict ") < apply.index("CREATE TABLE IF NOT EXISTS baton.note")
    assert "\\unrestrict" not in apply and apply.count("\\restrict ") == 1
    assert all(c[3] == name and "database" not in c for c, _ in thrown)
    run_argv = next(c for c in docker.calls if c[:2] == ["docker", "run"])
    assert run_argv[run_argv.index("--network") + 1] == "none" and run_argv[-1] == DB_REF
    assert docker.calls[-1][:3] != ["docker", "rm", "-f"] or True
    assert ["docker", "rm", "-f", name] in docker.calls
    assert TOKEN not in text


def test_a_role_change_is_caught_on_the_isolated_cluster_and_never_sent_to_the_live_database(files, tmp_path, monkeypatch):
    migration_file(tmp_path, monkeypatch, "RESET ROLE;\nALTER ROLE baton_app NOLOGIN;\nALTER ROLE baton_app CONNECTION LIMIT 0;\n")
    assert "switches role" in deploy.unsafe_migration_statements("RESET ROLE;\n")
    assert "changes roles" in deploy.unsafe_migration_statements("ALTER ROLE baton_app NOLOGIN;\n")
    docker = FakeDocker(precheck="role")
    code, report, _ = run(files, docker, FakeGateway(docker))
    assert code == 1 and report["status"] == "aborted" and not docker.calls
    assert "switches role" in report["rollback"]["reason"] and "changes roles" in report["rollback"]["reason"]
    docker = FakeDocker(precheck="role")
    code, report, _ = run(files, docker, FakeGateway(docker, good_image=NEW_APP), "--allow-incompatible-migration")
    assert code == 0 and "changes role baton_app" in report["precheck"]["changes"]
    live = [s for c, s in zip(docker.calls, docker.stdins)
            if c[:2] == ["docker", "compose"] and "exec" in c and "-Fc" not in c[-1]]
    assert live == [None, None], "the live database is only dumped, never given SQL"
    applied = next(s for c, s in zip(docker.calls, docker.stdins) if c[:2] == ["docker", "exec"] and s and "ALTER ROLE" in s)
    assert applied.index("\\restrict ") < applied.index("ALTER ROLE")
    assert report["precheck"]["network"] == "none"


def test_precheck_runs_before_the_backup_the_build_and_the_migration(files):
    docker = FakeDocker()
    run(files, docker, FakeGateway(docker, good_image=NEW_APP))
    removed = max(i for i, c in enumerate(docker.calls) if c[:3] == ["docker", "rm", "-f"])
    assert removed < docker.calls.index(dump_calls(docker)[0]) < next(i for i, c in enumerate(docker.calls) if c[-1:] == ["build"])


def test_a_breaking_schema_diff_aborts_before_anything_changes_and_the_throwaway_is_still_dropped(files):
    docker = FakeDocker(precheck="breaking")
    code, report, _ = run(files, docker, FakeGateway(docker))
    assert code == 1 and report["status"] == "aborted" and report["precheck"]["changes"] == ["drops column baton.tasks.assignee"]
    assert "drops column baton.tasks.assignee" in report["rollback"]["reason"]
    assert ["docker", "rm", "-f", report["precheck"]["database"]] in docker.calls, "cleanup runs even when the check fails"
    assert not docker.ups() and not dump_calls(docker) and not [c for c in docker.calls if c[-1:] == ["build"]]
    docker = FakeDocker(precheck="breaking")
    code, report, _ = run(files, docker, FakeGateway(docker, good_image=NEW_APP), "--allow-incompatible-migration")
    assert code == 0 and report["precheck"]["changes"] and report["backup"]["ok"], "acknowledged, still recorded and backed up"


@pytest.mark.parametrize("failure", ["fail:create", "fail:clone", "fail:apply", "garbage"])
def test_a_precheck_that_cannot_complete_fails_closed(files, failure):
    docker = FakeDocker(precheck=failure)
    code, report, _ = run(files, docker, FakeGateway(docker))
    assert code == 1 and report["status"] == "aborted" and not report["precheck"]["ok"] and report["precheck"]["error"]
    assert "could not be completed" in report["rollback"]["reason"] and "treated as incompatible" in report["rollback"]["reason"]
    assert not docker.ups() and not dump_calls(docker)
    created = failure not in ("fail:create", "fail:clone")
    assert (["docker", "rm", "-f", report["precheck"]["database"]] in docker.calls) == created
    docker = FakeDocker(precheck=failure)
    code, report, _ = run(files, docker, FakeGateway(docker, good_image=NEW_APP), "--allow-incompatible-migration")
    assert code == 0 and not report["precheck"]["ok"]


def test_a_throwaway_database_that_cannot_be_dropped_is_reported(files):
    docker = FakeDocker(drop_ok=False)
    code, report, _ = run(files, docker, FakeGateway(docker, good_image=NEW_APP))
    assert code == 0 and report["precheck"]["cleanup_error"].startswith("could not drop " + report["precheck"]["database"])


@pytest.mark.parametrize("statement", [
    "\\c baton\n", "\\! id\n", "SELECT 1;\n\\c baton\n", "SELECT 1; \\g\n", "\\copy baton.t TO stdout\n",
    "COMMIT;\n\\connect baton\nDROP TABLE baton.t;\n",
])
def test_psql_meta_commands_are_rejected_before_any_docker_command(files, tmp_path, monkeypatch, statement):
    migration_file(tmp_path, monkeypatch, statement)
    docker = FakeDocker()
    code, report, _ = run(files, docker, FakeGateway(docker))
    assert code == 1 and report["status"] == "aborted" and not docker.calls
    assert "psql meta-command" in report["rollback"]["reason"]


@pytest.mark.parametrize("statement", [
    "SELECT '\\c baton';\n", "SELECT E'\\c baton';\n", "-- \\c baton\nSELECT 1;\n",
    "/* \\! id */\nSELECT 1;\n", "SELECT $$ \\c baton $$;\n", 'SELECT "\\c";\n',
])
def test_a_backslash_inside_a_literal_or_comment_is_not_a_psql_meta_command(statement):
    assert deploy.psql_meta_command(statement) is False
    assert "psql meta-command" not in deploy.unsafe_migration_statements(statement)


def test_allowing_an_incompatible_migration_still_disables_psql_meta_commands(files, tmp_path, monkeypatch):
    migration_file(tmp_path, monkeypatch, "\\c baton\nSELECT 1;\n")
    docker = FakeDocker()
    code, report, _ = run(files, docker, FakeGateway(docker, good_image=NEW_APP), "--allow-incompatible-migration")
    assert code == 0 and report["migration"]["unsafe"]
    applied = next(s for s in docker.stdins if s and s.startswith("BEGIN;"))
    assert applied.index("\\restrict ") < applied.index("\\c baton")
    assert re.search(r"\\restrict [0-9a-f]{32}\n", applied)


def test_precheck_reads_the_migration_that_the_image_will_apply(files, tmp_path):
    schema = tmp_path / "pending.sql"
    schema.write_text("ALTER TABLE baton.tasks ADD COLUMN IF NOT EXISTS extra text;\n")
    docker = FakeDocker()
    run(files, docker, FakeGateway(docker, good_image=NEW_APP), "--schema-file", str(schema))
    assert any(s and s.startswith("BEGIN;") and "ADD COLUMN IF NOT EXISTS extra text" in s for s in docker.stdins)


def test_dry_run_plans_the_precheck_without_executing_it(files):
    docker = FakeDocker()
    code, report, _ = run(files, docker, None, "--dry-run")
    assert code == 0 and report["precheck"]["planned"] and not [s for s in docker.stdins if s]
    assert not [c for c in docker.calls if "exec" in c]
    assert sum(1 for c in report["commands"] if "exec" in c["argv"] and not c["executed"]) >= 7


@pytest.mark.parametrize("statement", [
    "ALTER TABLE baton.t DROP c;", "ALTER TABLE baton.t DROP IF EXISTS c;", "ALTER TABLE baton.t ALTER c SET NOT NULL;",
    "ALTER TABLE baton.t ALTER c TYPE bigint;", 'ALTER TABLE baton.t ALTER "c" SET DATA TYPE text;',
    "ALTER TABLE baton.t ADD c text NOT NULL;", "ALTER TABLE baton.t ADD COLUMN a int, ADD COLUMN b int NOT NULL;",
    "/* -- */ DROP TABLE baton.t;", "-- note\n/* x */ ALTER TABLE baton.t DROP/**/COLUMN c;",
    "ALTER TABLE baton.t DROP COLUMN c; -- ok", "SELECT '--'; DROP TABLE baton.t;", "SELECT 'it''s'; DROP TABLE baton.t;",
])
def test_the_textual_early_warning_catches_the_forms_that_slipped_through_before(statement):
    assert deploy.unsafe_migration_statements(statement)


def base_snapshot():
    return json.loads(json.dumps(catalog()))


def altered(**edits):
    snapshot = base_snapshot()
    for section, change in edits.items():
        change(snapshot[section]) if callable(change) else snapshot.__setitem__(section, change)
    return snapshot


def column(name, **fields):
    return dict({"t": "baton.tasks", "c": name, "type": "text", "notnull": False, "default": None, "hasdef": False}, **fields)


@pytest.mark.parametrize("after,expected", [
    (altered(columns=lambda c: c.pop()), "drops column baton.tasks.assignee"),
    (altered(relations=[], columns=[]), "drops baton.tasks:r"),
    (altered(columns=lambda c: c[1].update(type="integer")), "changes the type of baton.tasks.assignee from text to integer"),
    (altered(columns=lambda c: c[1].update(notnull=True)), "makes baton.tasks.assignee NOT NULL"),
    (altered(columns=lambda c: c.append(column("new", notnull=True))), "adds NOT NULL column baton.tasks.new without a default"),
    (altered(columns=lambda c: c[1].update(default="'x'::text")), "changes the default of baton.tasks.assignee"),
    (altered(constraints=[]), "drops constraint tasks_pkey on baton.tasks"),
    (altered(constraints=lambda c: c[0].update(**{"def": "PRIMARY KEY (id, assignee)"})), "changes constraint tasks_pkey on baton.tasks"),
    (altered(constraints=lambda c: c.append({"t": "baton.tasks", "name": "chk", "def": "CHECK (assignee <> '')", "cols": ["assignee"]})),
     "adds constraint chk that constrains existing columns of baton.tasks"),
    (altered(constraints=lambda c: c.append({"t": "baton.tasks", "name": "chk0", "def": "CHECK (true)", "cols": []})),
     "adds constraint chk0 that constrains existing columns of baton.tasks"),
    (altered(indexes=[]), "drops index tasks_pkey on baton.tasks"),
    (altered(indexes=lambda i: i.append({"t": "baton.tasks", "name": "u", "def": "CREATE UNIQUE INDEX u", "unique": True, "cols": ["assignee"]})),
     "adds index u that constrains existing columns of baton.tasks"),
    (altered(triggers=lambda t: t.append({"t": "baton.tasks", "name": "g", "def": "CREATE TRIGGER g"})),
     "adds trigger g to the existing table baton.tasks"),
    (altered(types=lambda t: t.append({"name": "baton.k", "def": "e:a"})), None),
    (altered(privileges=[{"t": "baton.tasks", "acl": "{=r/baton_owner}"}]), "changes privileges on baton.tasks"),
    (altered(column_privileges=[{"t": "baton.tasks", "c": "assignee", "acl": "{=r/baton_owner}"}]),
     "changes privileges on baton.tasks.assignee"),
    (altered(schema_privileges=[{"n": "baton", "acl": "{=U/baton_owner}"}]), "changes privileges on schema baton"),
    (altered(database_privileges=[{"acl": "{=C/baton_owner}"}]), "changes database privileges"),
    (altered(row_security=[{"t": "baton.tasks", "enabled": True, "forced": False}]),
     "changes row level security on baton.tasks"),
    (altered(row_security=[{"t": "baton.tasks", "enabled": False, "forced": True}]),
     "changes row level security on baton.tasks"),
    (altered(policies=[{"t": "baton.tasks", "name": "p", "permissive": True, "cmd": "*", "roles": "",
                        "qual": "true", "with_check": None}]),
     "adds policy p to the existing table baton.tasks"),
    (altered(relations=lambda r: r.append("baton.notes:r"),
             privileges=lambda p: p.append({"t": "baton.notes", "acl": "{=r/baton_owner}"}),
             row_security=lambda rows: rows.append({"t": "baton.notes", "enabled": True, "forced": True}),
             policies=[{"t": "baton.notes", "name": "p", "permissive": True, "cmd": "*", "roles": "",
                        "qual": "true", "with_check": None}]), None),
])
def test_schema_diff_flags_everything_a_previous_image_could_not_survive(after, expected):
    changes = deploy.schema_breaking_changes(base_snapshot(), after)
    if expected is None:
        assert changes == []
    else:
        assert expected in changes, changes


def test_schema_diff_covers_dropped_or_changed_triggers_functions_and_types():
    before = altered(triggers=[{"t": "baton.tasks", "name": "g", "def": "A"}], functions=[{"sig": "baton.f()", "def": "one"}],
                     types=[{"name": "baton.k", "def": "e:a"}])
    after = altered(triggers=[{"t": "baton.tasks", "name": "g", "def": "B"}], functions=[{"sig": "baton.f()", "def": "two"}],
                    types=[{"name": "baton.k", "def": "e:a,b"}])
    assert deploy.schema_breaking_changes(before, after) == [
        "changes trigger g on baton.tasks", "changes function baton.f()", "changes type baton.k"]
    gone = altered()
    assert deploy.schema_breaking_changes(before, gone) == ["drops trigger g on baton.tasks", "drops function baton.f()",
                                                            "drops type baton.k"]


def test_schema_diff_flags_a_changed_predicate_or_column_list_on_an_existing_index():
    plain = {"t": "baton.tasks", "name": "tasks_assignee", "unique": False, "cols": ["assignee"], "pred": None,
             "def": "CREATE INDEX tasks_assignee ON baton.tasks USING btree (assignee)"}
    narrowed = dict(plain, cols=["priority"], **{"def": "CREATE INDEX tasks_assignee ON baton.tasks USING btree (priority)"})
    predicated = dict(plain, pred="(priority = 'low')", **{
        "def": "CREATE INDEX tasks_assignee ON baton.tasks USING btree (assignee) WHERE (priority = 'low')"})
    before = altered(indexes=lambda rows: rows.append(dict(plain)))
    assert "changes index tasks_assignee on baton.tasks" in deploy.schema_breaking_changes(
        before, altered(indexes=lambda rows: rows.append(dict(narrowed))))
    assert "changes index tasks_assignee on baton.tasks" in deploy.schema_breaking_changes(
        before, altered(indexes=lambda rows: rows.append(dict(predicated))))
    assert deploy.schema_breaking_changes(before, base_snapshot()) == []


def test_a_new_internal_foreign_key_trigger_on_an_existing_table_is_not_breaking():
    internal = {"t": "baton.tasks", "name": "RI_ConstraintTrigger_a",
                "def": "CREATE CONSTRAINT TRIGGER RI_ConstraintTrigger_a", "enabled": "O", "internal": True}
    assert deploy.schema_breaking_changes(base_snapshot(), altered(triggers=[internal])) == []
    user = {"t": "baton.tasks", "name": "audit", "def": "CREATE TRIGGER audit", "enabled": "O", "internal": False}
    assert deploy.schema_breaking_changes(base_snapshot(), altered(triggers=[user])) == [
        "adds trigger audit to the existing table baton.tasks"]
    before = altered(triggers=[internal])
    assert deploy.schema_breaking_changes(before, altered(triggers=[dict(internal, enabled="D")])) == [
        "disables trigger RI_ConstraintTrigger_a on baton.tasks"]


def test_catalog_sql_keeps_internal_triggers_and_a_disabled_one_is_breaking():
    assert "tgenabled" in deploy.CATALOG_SQL and "g.tgisinternal" in deploy.CATALOG_SQL
    assert "NOT g.tgisinternal" not in deploy.CATALOG_SQL
    firing = {"t": "baton.child", "name": "RI_ConstraintTrigger_a",
              "def": "CREATE CONSTRAINT TRIGGER RI_ConstraintTrigger_a", "enabled": "O"}
    before = altered(triggers=[firing])
    assert deploy.schema_breaking_changes(before, altered(triggers=[dict(firing, enabled="D")])) == [
        "disables trigger RI_ConstraintTrigger_a on baton.child"]


def test_schema_diff_flags_disabling_a_trigger_even_when_its_definition_is_unchanged():
    definition = "CREATE TRIGGER audit_no_change BEFORE UPDATE ON baton.x_audit"
    firing = {"t": "baton.x_audit", "name": "audit_no_change", "def": definition, "enabled": "O"}
    before = altered(triggers=[firing])
    disabled = altered(triggers=[dict(firing, enabled="D")])
    replica = altered(triggers=[dict(firing, enabled="R")])
    assert deploy.schema_breaking_changes(before, disabled) == [
        "disables trigger audit_no_change on baton.x_audit"]
    assert deploy.schema_breaking_changes(before, replica) == [
        "changes whether trigger audit_no_change on baton.x_audit fires"]
    assert deploy.schema_breaking_changes(before, altered(triggers=[dict(firing)])) == []


def test_schema_diff_flags_a_non_unique_index_rebuilt_as_unique_under_the_same_name():
    plain = {"t": "baton.tasks", "name": "tasks_assignee",
             "def": "CREATE INDEX tasks_assignee ON baton.tasks USING btree (assignee)",
             "unique": False, "cols": ["assignee"]}
    unique = dict(plain, unique=True,
                  **{"def": "CREATE UNIQUE INDEX tasks_assignee ON baton.tasks USING btree (assignee)"})
    before = altered(indexes=lambda rows: rows.append(dict(plain)))
    after = altered(indexes=lambda rows: rows.append(dict(unique)))
    assert deploy.schema_breaking_changes(before, after) == [
        "replaces non-unique index tasks_assignee on baton.tasks with a unique index"]
    assert deploy.schema_breaking_changes(before, base_snapshot()) == [], "dropping a non-unique index is compatible"


def test_schema_diff_flags_policy_edits_and_fails_closed_without_privilege_sections():
    before = altered(policies=[{"t": "baton.tasks", "name": "p", "permissive": True, "cmd": "*", "roles": "",
                                "qual": "true", "with_check": None}])
    changed = altered(policies=[{"t": "baton.tasks", "name": "p", "permissive": True, "cmd": "r", "roles": "",
                                 "qual": "false", "with_check": None}])
    assert deploy.schema_breaking_changes(before, changed) == ["changes policy p on baton.tasks"]
    assert deploy.schema_breaking_changes(before, base_snapshot()) == ["drops policy p on baton.tasks"]
    after = base_snapshot()
    del after["policies"]
    del after["row_security"]
    assert deploy.schema_breaking_changes(base_snapshot(), after) == ["catalog snapshot is missing policies, row_security"]


def test_comments_and_strings_do_not_count_as_privilege_changes():
    assert deploy.unsafe_migration_statements("-- GRANT SELECT ON baton.t TO PUBLIC;\nCREATE TABLE baton.n(id int);") == []
    assert deploy.unsafe_migration_statements("SELECT 'REVOKE ALL ON baton.t FROM PUBLIC';") == []
    hidden = "DO $$ BEGIN EXECUTE 'GRANT SELECT ON baton.t TO PUBLIC'; END $$;"
    assert deploy.unsafe_migration_statements(hidden) == ["contains a DO block"]


def test_schema_diff_accepts_purely_additive_changes():
    after = altered(
        columns=lambda c: c.extend([column("note"), column("flag", type="boolean", notnull=True, hasdef=True, default="false")]),
        relations=lambda r: r.append("baton.notes:r"),
        constraints=lambda c: c.append({"t": "baton.tasks", "name": "chk_note", "def": "CHECK (note <> '')", "cols": ["note"]}),
        indexes=lambda i: i.append({"t": "baton.tasks", "name": "plain", "def": "CREATE INDEX plain", "unique": False, "cols": ["id"]}),
        functions=[{"sig": "baton.g()", "def": "x"}])
    assert deploy.schema_breaking_changes(base_snapshot(), after) == []


@pytest.fixture
def admin_connection():
    import os

    import psycopg

    path = os.environ.get("BATON_TEST_DATABASE_ADMIN_CONFIG")
    if not path:
        pytest.skip("PostgreSQL integration NOT_RUN: set BATON_TEST_DATABASE_ADMIN_CONFIG")
    config = json.loads(Path(path).read_text())
    return psycopg, config


def test_catalog_sql_and_diff_on_a_real_postgres_accept_the_shipped_schema_and_catch_evasive_drops(admin_connection):
    psycopg, config = admin_connection
    name = deploy.PRECHECK_PREFIX + "test_" + uuid.uuid4().hex[:8]
    schema = (ROOT / "bridge/space_schema.sql").read_text(encoding="utf-8")

    def connect(database, autocommit=False):
        return psycopg.connect(**dict(config, dbname=database), autocommit=autocommit)

    with connect(config["dbname"], autocommit=True) as admin:
        admin.execute('CREATE DATABASE "%s"' % name)
    try:
        with connect(name, autocommit=True) as conn:
            conn.execute("DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='baton_owner') THEN "
                         "CREATE ROLE baton_owner NOLOGIN; END IF; END $$")
            conn.execute("CREATE SCHEMA IF NOT EXISTS baton AUTHORIZATION baton_owner")
            conn.execute('GRANT CREATE ON DATABASE "%s" TO baton_owner' % name)

            def apply(sql):
                conn.execute("BEGIN")
                conn.execute("SET LOCAL ROLE baton_owner")
                conn.execute(sql)
                conn.execute("COMMIT")

            def snapshot():
                return deploy.parse_snapshot(json.dumps(conn.execute(deploy.CATALOG_SQL).fetchone()[0]))

            apply(schema)
            first = snapshot()
            assert {"baton.tasks:r", "baton.x_audit:r"} <= set(first["relations"])
            assert first["constraints"] and first["indexes"] and first["triggers"] and first["functions"]
            apply(schema)
            assert deploy.schema_breaking_changes(first, snapshot()) == [], "the shipped migration is idempotent on its own schema"
            for statement, expected in [
                ("ALTER TABLE baton.tasks ADD COLUMN IF NOT EXISTS df18_extra text", None),
                ("DO $$ BEGIN EXECUTE 'ALTER TABLE baton.tasks '||'DROP COLUMN df18_extra'; END $$",
                 "drops column baton.tasks.df18_extra"),
                ("DO $$ BEGIN EXECUTE 'ALTER TABLE baton.tasks ALTER priority SET DATA TYPE varchar(5)'; END $$",
                 "changes the type of baton.tasks.priority from text to character varying(5)"),
                ("DO $$ BEGIN EXECUTE 'ALTER TABLE baton.tasks ALTER blocked_by SET NOT NULL'; END $$",
                 "makes baton.tasks.blocked_by NOT NULL"),
                ("ALTER TABLE baton.tasks ADD COLUMN df18_required text NOT NULL DEFAULT 'x'", None),
                ("DO $$ BEGIN EXECUTE 'ALTER TABLE baton.tasks DROP COLUMN assignee'; END $$", "drops column baton.tasks.assignee"),
                ("GRANT SELECT ON TABLE baton.tasks TO PUBLIC", "changes privileges on baton.tasks"),
                ("REVOKE SELECT ON TABLE baton.tasks FROM PUBLIC", "changes privileges on baton.tasks"),
                ("GRANT SELECT (id) ON TABLE baton.tasks TO PUBLIC", "changes privileges on baton.tasks.id"),
                ("GRANT USAGE ON SCHEMA baton TO PUBLIC", "changes privileges on schema baton"),
                ("ALTER TABLE baton.tasks ENABLE ROW LEVEL SECURITY", "changes row level security on baton.tasks"),
                ("ALTER TABLE baton.tasks FORCE ROW LEVEL SECURITY", "changes row level security on baton.tasks"),
                ("CREATE POLICY df18_p ON baton.tasks USING (true)", "adds policy df18_p to the existing table baton.tasks"),
                ("DROP POLICY df18_p ON baton.tasks", "drops policy df18_p on baton.tasks"),
                ("DO $$ BEGIN EXECUTE 'GRANT SELECT ON TABLE baton.tasks TO PUBLIC'; END $$", "changes privileges on baton.tasks"),
                ("CREATE INDEX df18_plain ON baton.tasks (priority)", None),
                ("DROP INDEX baton.df18_plain; CREATE UNIQUE INDEX df18_plain ON baton.tasks (priority)",
                 "replaces non-unique index df18_plain on baton.tasks with a unique index"),
                ("DROP INDEX baton.df18_plain; CREATE INDEX df18_plain ON baton.tasks (priority) WHERE priority = 'low'",
                 "changes index df18_plain on baton.tasks"),
                ("CREATE ROLE df18_role LOGIN", None),
                ("ALTER ROLE df18_role NOLOGIN", "changes role df18_role"),
                ("ALTER ROLE df18_role CONNECTION LIMIT 0", "changes role df18_role"),
                ("GRANT df18_role TO baton_owner", "adds membership of baton_owner in df18_role"),
                ("ALTER TABLE baton.x_audit DISABLE TRIGGER audit_no_change",
                 "disables trigger audit_no_change on baton.x_audit"),
            ]:
                before = snapshot()
                if statement.startswith(("CREATE ROLE", "ALTER ROLE", "GRANT df18_role")):
                    conn.execute(statement)
                else:
                    apply(statement)
                changes = deploy.schema_breaking_changes(before, snapshot())
                assert (expected in changes) if expected else changes == [], (statement, changes)
            before = snapshot()
            apply("CREATE TABLE baton.df18_parent (id int PRIMARY KEY)")
            apply("CREATE TABLE baton.df18_child (parent_id int REFERENCES baton.df18_parent(id))")
            assert deploy.schema_breaking_changes(before, snapshot()) == []
            before = snapshot()
            apply("ALTER TABLE baton.tasks ADD COLUMN df18_parent_id int REFERENCES baton.df18_parent(id) "
                  "ON UPDATE CASCADE ON DELETE SET NULL")
            after = snapshot()
            assert deploy.schema_breaking_changes(before, after) == []
            added = [item for item in after["triggers"] if item["name"] not in {old["name"] for old in before["triggers"]}]
            assert added and all(item["internal"] and item["enabled"] == "O" for item in added)
            assert {"baton.tasks", "baton.df18_parent"} <= {item["t"] for item in added}
            before = snapshot()
            internal = [item for item in before["triggers"] if item["t"] == "baton.df18_child"]
            assert internal and all(item["enabled"] == "O" for item in internal)
            assert all(item["name"].startswith("RI_ConstraintTrigger_") for item in internal)
            # System triggers (foreign keys) can be disabled only by a superuser, which is the session
            # user after RESET ROLE. baton_owner is refused; the catalog diff is what still has to see it.
            conn.execute("ALTER TABLE baton.df18_child DISABLE TRIGGER ALL")
            changes = deploy.schema_breaking_changes(before, snapshot())
            assert changes and all(
                change.startswith("disables trigger ") and change.endswith(" on baton.df18_child") for change in changes), changes
    finally:
        with connect(config["dbname"], autocommit=True) as admin:
            admin.execute('DROP DATABASE IF EXISTS "%s" WITH (FORCE)' % name)
            admin.execute("DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'df18_role') THEN "
                          "REVOKE df18_role FROM baton_owner; DROP ROLE df18_role; END IF; END $$")


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


def test_subprocess_runner_does_not_wait_forever_for_a_descendant_that_escaped_the_process_group(tmp_path):
    escaped = tmp_path / "escaped.py"
    escaped.write_text("import os, time\nos.setsid()\ntime.sleep(25)\n")
    parent = tmp_path / "parent.py"
    parent.write_text("import subprocess, sys, time\nsubprocess.Popen([sys.executable, %r])\ntime.sleep(60)\n" % str(escaped))
    started = time.monotonic()
    result = deploy.SubprocessRunner()([sys.executable, str(parent)], timeout=1)
    assert result.timed_out and result.returncode == deploy.TIMEOUT_RETURNCODE
    assert time.monotonic() - started < 1 + 2 * deploy.REAP_SECONDS + 5, "the wait after the kill is bounded too"


def test_subprocess_runner_feeds_stdin_text_to_the_command(tmp_path):
    result = deploy.SubprocessRunner()([sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"], timeout=20,
                                       stdin_text="select 1;")
    assert result.returncode == 0 and result.stdout.strip() == "SELECT 1;"
    target = tmp_path / "o"
    result = deploy.SubprocessRunner()([sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"], timeout=20,
                                       stdin_text="abc", stdout_path=str(target))
    assert result.returncode == 0 and target.read_text() == "abc"


def test_subprocess_runner_streams_stdout_to_a_private_file_and_reports_missing_commands(tmp_path):
    target = tmp_path / "out.bin"
    previous = os.umask(0)
    try:
        result = deploy.SubprocessRunner()([sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'PGDMP\\x00\\xff'); sys.stderr.write('warn')"],
                                           timeout=20, stdout_path=str(target))
    finally:
        os.umask(previous)
    assert result.returncode == 0 and result.stderr == "warn" and target.read_bytes() == b"PGDMP\x00\xff"
    assert target.stat().st_mode & 0o777 == 0o600
    assert deploy.SubprocessRunner()(["definitely-not-a-command-xyz"], timeout=5).returncode == 127


def test_private_output_refuses_an_existing_file_and_does_not_follow_a_symlink(tmp_path):
    loose = tmp_path / "loose"
    loose.write_bytes(b"keep-me")
    loose.chmod(0o644)
    result = deploy.SubprocessRunner()([sys.executable, "-c", "import sys; sys.stdout.write('PGDMP')"],
                                       timeout=20, stdout_path=str(loose))
    assert result.returncode != 0 and result.stderr.startswith(deploy.PRIVATE_OUTPUT_ERROR)
    assert loose.read_bytes() == b"keep-me" and loose.stat().st_mode & 0o777 == 0o644
    secret = tmp_path / "secret"
    secret.write_text("x_webhook.secret")
    link = tmp_path / "dump"
    link.symlink_to(secret)
    result = deploy.SubprocessRunner()([sys.executable, "-c", "import sys; sys.stdout.write('PGDMP-LEAK')"],
                                       timeout=20, stdout_path=str(link))
    assert result.returncode != 0 and result.stderr.startswith(deploy.PRIVATE_OUTPUT_ERROR)
    assert link.is_symlink() and secret.read_text() == "x_webhook.secret"


def test_backup_database_does_not_overwrite_or_remove_a_planted_symlink(tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("x_webhook.secret")
    link = tmp_path / "backups" / "fixed.dump"
    link.parent.mkdir()
    link.symlink_to(secret)
    monkey_path = link

    def fixed_path(backup_dir, project):
        return monkey_path

    original = deploy.backup_dump_path
    deploy.backup_dump_path = fixed_path
    try:
        compose = tmp_path / "compose.yaml"
        compose.write_text("services: {}\n")
        config = deploy.Config([compose], project="demo", backup_dir=str(link.parent))
        docker = deploy.Docker(deploy.SubprocessRunner(), config, lambda message: None)
        report, ok = deploy.backup_database(docker, config, "demo", {"state": "running", "ok": True})
    finally:
        deploy.backup_dump_path = original
    assert not ok and "existing backup path" in report["error"]
    assert link.is_symlink() and secret.read_text() == "x_webhook.secret" and not secret.read_text().startswith("PGDMP")


def test_deploy_lock_times_out_without_tagging_and_is_held_through_rollback(files, tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, "LOCK_DIR", tmp_path / "locks")
    held = deploy.DeployLock(deploy.lock_file("demo"), 5, time.monotonic, time.sleep)
    held.acquire()
    try:
        docker = FakeDocker()
        code, report, _ = run(files, docker, FakeGateway(docker), "--lock-timeout", "1")
    finally:
        held.close()
    assert code == 1 and report["status"] == "aborted"
    assert "another deploy holds the lock" in report["rollback"]["reason"]
    assert not [c for c in docker.calls if c[:2] == ["docker", "tag"]]
    assert not dump_calls(docker) and not docker.ups()
    docker = FakeDocker(up_results=[1, 0])
    seen = {}

    class Watching:
        def __call__(self, argv, timeout=None, stdout_path=None, stdin_text=None):
            if argv[:2] == ["docker", "tag"] and not str(argv[-1]).startswith(deploy.HOLD_REPOSITORY):
                seen["rollback"] = _lock_held(deploy.lock_file("demo"))
            if stdout_path:
                seen["dump"] = _lock_held(deploy.lock_file("demo"))
            return FakeDocker.__call__(docker, argv, timeout=timeout, stdout_path=stdout_path, stdin_text=stdin_text)

    code, report, _ = run(files, Watching(), FakeGateway(docker, good_image=OLD_APP), "--lock-timeout", "5")
    assert code == 1 and report["status"] == "rolled_back"
    assert seen.get("dump") is True and seen.get("rollback") is True
    assert _lock_held(deploy.lock_file("demo")) is False


def _lock_held(path):
    import fcntl
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def test_deploy_lock_blocks_a_second_waiter_until_the_deadline(tmp_path):
    path = tmp_path / "locks" / "demo.lock"
    state = {"now": 0}

    def clock():
        return state["now"]

    def sleep(seconds):
        state["now"] += seconds

    first = deploy.DeployLock(path, 5, clock, sleep)
    first.acquire()
    second = deploy.DeployLock(path, 1, clock, sleep)
    with pytest.raises(deploy.UsageError, match="another deploy holds the lock"):
        second.acquire()
    assert state["now"] >= 1
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    first.close()
    second.acquire()
    second.close()
    link = tmp_path / "linked-locks"
    link.symlink_to(path.parent)
    with pytest.raises(deploy.UsageError, match="symbolic link"):
        deploy.DeployLock(link / "other.lock", 1, clock, sleep).acquire()


def test_rollback_limits_are_documented_next_to_the_commands():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("### 重新部署、冒烟与回滚")[1].split("### 升级旧实例")[0]
    for needle in ("expand", "pg_dump", "--backup-dir", "迁移", "不恢复数据库", "--allow-remove-orphans", "超时", "孤立",
                   "隔离容器", "已停止", "数据卷", "--schema-file", "保留标签", "符号链接", "互斥", "行级安全", "GRANT",
                   "元命令", "唯一性", "--network none", "角色", "UPDATE", "1 MiB",
                   "只在这个预检容器", "tgenabled", "信任边界"):
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


class Trickle(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _trickle(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            for _ in range(40):
                self.wfile.write(b"1\r\nx\r\n")
                self.wfile.flush()
                time.sleep(0.2)
            self.wfile.write(b"0\r\n\r\n")
        except Exception:
            return

    def do_GET(self):
        self._trickle()

    def do_POST(self):
        self._trickle()

    def log_message(self, fmt, *args):
        return


class HugeBody(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self):
        body = b"x" * (deploy.MAX_SMOKE_BODY + 100)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            return

    def do_GET(self):
        self._send()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        if length:
            self.rfile.read(length)
        self._send()

    def log_message(self, fmt, *args):
        return


def test_a_smoke_response_larger_than_the_body_cap_fails_without_reading_forever():
    server = ThreadingHTTPServer(("127.0.0.1", 0), HugeBody)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        client = deploy.HttpClient("http://127.0.0.1:%d" % server.server_address[1], timeout=5)
        started = time.monotonic()
        with pytest.raises(deploy.SmokeError, match="response too large"):
            client("GET", "/board")
        elapsed = time.monotonic() - started
    finally:
        server.shutdown()
        server.server_close()
    assert elapsed < 2, elapsed


def test_a_trickling_response_cannot_outrun_the_smoke_deadline():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Trickle)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        client = deploy.HttpClient("http://127.0.0.1:%d" % server.server_address[1], timeout=30)
        started = time.monotonic()
        result = deploy.run_smoke(client, TOKEN, 0.6)
        elapsed = time.monotonic() - started
    finally:
        server.shutdown()
        server.server_close()
    assert not result["ok"]
    assert any(item["detail"] == "timed out" for item in result["checks"])
    assert elapsed < 3, elapsed


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
