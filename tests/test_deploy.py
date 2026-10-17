# SPDX-License-Identifier: Apache-2.0
"""Edge configuration and the deploy script's smoke/rollback decisions, with a fake docker and a fake gateway."""

import importlib.util
import io
import json
import re
import threading
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


class FakeDocker:
    """Scripted docker/compose: tracks the image tag, the running containers and one gateway health flag."""

    def __init__(self, running=True, up_results=(0, 0), build_ok=True, tag_ok=True, hold_ok=True):
        self.calls, self.up_results, self.build_ok, self.tag_ok, self.hold_ok = [], list(up_results), build_ok, tag_ok, hold_ok
        self.tags = {APP_REF: OLD_APP} if running else {}
        self.running = running
        self.serving = OLD_APP if running else None
        self.override_text = None

    def __call__(self, argv, timeout=None):
        self.calls.append(list(argv))
        if "ps" in argv and "-q" in argv:
            return deploy.Result(0, "c1\nc2\n" if self.running else "")
        if argv[:2] == ["docker", "inspect"]:
            items = [self.container("bridge", APP_REF, OLD_APP), self.container("edge", NGINX_REF, "sha256:" + "c" * 64),
                     self.container("initialize", APP_REF, OLD_APP, running=False)]
            return deploy.Result(0, json.dumps(items))
        if argv[:3] == ["docker", "image", "inspect"]:
            ref = argv[-1]
            return deploy.Result(0, self.tags[ref] + "\n") if ref in self.tags else deploy.Result(1, "", "no such image")
        if "config" in argv:
            return deploy.Result(0, json.dumps({"services": {"initialize": {"build": {}}, "bridge": {"build": {}}, "edge": {}}}))
        if argv[:2] == ["docker", "compose"] and argv[-1] == "build":
            self.override_text = next((Path(a).read_text() for a in argv if a.endswith("build-network.yaml")), None)
            if not self.build_ok:
                return deploy.Result(1, "", "build exploded with " + TOKEN)
            self.tags[APP_REF] = NEW_APP
            return deploy.Result(0, "built")
        if argv[:2] == ["docker", "tag"]:
            if argv[3].startswith(deploy.HOLD_REPOSITORY):
                if not self.hold_ok:
                    return deploy.Result(1, "", "tag failed")
            elif not self.tag_ok:
                return deploy.Result(1, "", "No such image: " + argv[2])
            self.tags[argv[3]] = argv[2]
            return deploy.Result(0)
        if argv[:2] == ["docker", "rmi"]:
            self.tags.pop(argv[2], None)
            return deploy.Result(0)
        if "up" in argv:
            code = self.up_results.pop(0)
            if code == 0:
                self.serving = self.tags.get(APP_REF)
                self.running = True
            return deploy.Result(code, "up output", "up failed" if code else "")
        raise AssertionError("unexpected command: %s" % argv)

    @staticmethod
    def container(service, ref, image_id, running=True):
        return {"Id": service * 20, "Image": image_id, "State": {"Running": running},
                "Config": {"Image": ref, "Labels": {deploy.SERVICE_LABEL: service}}}

    def mutating(self):
        return [c for c in self.calls if c[:2] in (["docker", "tag"], ["docker", "rmi"]) or "up" in c
                or c[-1:] == ["build"]]


class FakeGateway:
    """HTTP stand-in; behaves like a healthy stack only while the previous application image serves."""

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


def run(files, docker, gateway, *extra):
    compose, token = files
    out, err = io.StringIO(), io.StringIO()
    argv = ["-f", str(compose), "-p", "demo", "--smoke-token-file", str(token), "--smoke-timeout", "0", *extra]
    code = deploy.main(argv, runner=docker, http=gateway, stdout=out, stderr=err)
    return code, json.loads(out.getvalue()), out.getvalue() + err.getvalue()


def test_successful_deploy_records_builds_ups_smokes_and_never_prints_the_token(files):
    docker = FakeDocker()
    gateway = FakeGateway(docker, good_image=NEW_APP)
    code, report, text = run(files, docker, gateway)
    assert code == 0 and report["status"] == "deployed" and report["ok"]
    assert report["previous_images"]["bridge"] == {"container": "bridge" * 2, "image": APP_REF, "image_id": OLD_APP}
    assert "initialize" not in report["previous_images"], "only running containers are recorded"
    assert [c[2:] for c in docker.mutating() if "up" in c] == [["-f", str(files[0]), "-p", "demo", "up", "-d", "--wait",
                                                               "--force-recreate", "--remove-orphans"]]
    assert [item["name"] for item in report["smoke"]["checks"]] == list(deploy.SMOKE_STEPS)
    assert all(item["ok"] for item in report["smoke"]["checks"])
    assert report["rollback"] == {"attempted": False}
    assert TOKEN not in text
    assert not [t for t in docker.tags if t.startswith(deploy.HOLD_REPOSITORY)], "holding tags are released"
    assert (("GET", "/board") in [(m, p) for m, p, _, _ in gateway.requests]
            and ("POST", "/mcp") in [(m, p) for m, p, _, _ in gateway.requests])
    unauthenticated = [h for m, p, h, b in gateway.requests if p == "/mcp" and "Authorization" not in h]
    assert len(unauthenticated) == 1


def test_smoke_failure_restores_the_previous_image_and_exits_nonzero(files):
    docker = FakeDocker()
    code, report, text = run(files, docker, FakeGateway(docker, good_image=OLD_APP))
    assert code == 1 and report["status"] == "rolled_back" and not report["ok"]
    assert not report["smoke"]["ok"] and report["smoke"]["checks"][0]["detail"] == "expected HTTP 200, got 502"
    assert report["rollback"]["ok"] and report["rollback"]["smoke"]["ok"]
    assert report["rollback"]["retagged"] == [{"image": APP_REF, "image_id": OLD_APP, "ok": True, "services": ["bridge"]}]
    assert docker.tags[APP_REF] == OLD_APP
    ups = [c for c in docker.calls if "up" in c]
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
    docker = FakeDocker(up_results=(1, 0))
    gateway = FakeGateway(docker, good_image=OLD_APP)
    code, report, _ = run(files, docker, gateway)
    assert code == 1 and report["status"] == "rolled_back"
    assert report["smoke"] == {"skipped": True, "reason": "docker compose up failed"}
    assert report["up"]["ok"] is False and report["rollback"]["ok"]


def test_first_deploy_failure_has_nothing_to_restore_and_exits_nonzero(files):
    docker = FakeDocker(running=False, up_results=(0,))
    code, report, _ = run(files, docker, FakeGateway(docker, good_image="never"))
    assert code == 3 and report["status"] == "failed"
    assert report["rollback"] == {"attempted": False, "reason": "no previous deployment was running"}
    assert len([c for c in docker.calls if "up" in c]) == 1


def test_rollback_that_cannot_retag_reports_failure_and_keeps_the_holding_tag(files):
    docker = FakeDocker(tag_ok=False)
    code, report, _ = run(files, docker, FakeGateway(docker, good_image=OLD_APP))
    assert code == 3 and report["status"] == "rollback_failed"
    assert "could not re-tag" in report["rollback"]["error"]
    assert len([c for c in docker.calls if "up" in c]) == 1, "no second up after a failed restore"
    assert deploy.hold_tag(OLD_APP) in docker.tags


def test_rollback_that_is_still_unhealthy_is_reported_as_failed(files):
    docker = FakeDocker()
    code, report, _ = run(files, docker, FakeGateway(docker, good_image="neither"))
    assert code == 3 and report["status"] == "rollback_failed"
    assert report["rollback"]["error"] == "the restored deployment also fails the smoke checks"


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
    assert not [c for c in docker.calls if "up" in c]
    assert "[redacted]" in report["build"]["output_tail"] and TOKEN not in text
    assert ["docker", "rmi", deploy.hold_tag(OLD_APP)] in docker.calls


def test_holding_failure_aborts_before_the_build(files):
    docker = FakeDocker(hold_ok=False)
    code, report, _ = run(files, docker, FakeGateway(docker))
    assert code == 1 and report["status"] == "aborted"
    assert not [c for c in docker.calls if "up" in c or c[-1:] == ["build"]]


def test_dry_run_only_runs_read_only_queries_and_never_touches_the_network(files):
    docker = FakeDocker()

    def no_network(*args, **kwargs):
        raise AssertionError("dry-run must not send requests")

    code, report, _ = run(files, docker, no_network, "--dry-run", "--build-network", "host")
    assert code == 0 and report["status"] == "dry_run" and report["dry_run"]
    assert not docker.mutating()
    planned = [c["argv"] for c in report["commands"] if not c["executed"]]
    assert any(a[-1:] == ["build"] for a in planned) and any("up" in a for a in planned)
    assert all(not c["mutating"] for c in report["commands"] if c["executed"])
    assert report["smoke"]["checks"] == list(deploy.SMOKE_STEPS)


def test_build_network_is_applied_through_an_override_for_services_with_a_build(files):
    docker = FakeDocker()
    run(files, docker, FakeGateway(docker, good_image=NEW_APP), "--build-network", "host")
    build = next(c for c in docker.calls if c[-1:] == ["build"])
    assert build[:2] == ["docker", "compose"] and build.count("-f") == 2
    assert build[build.index("-f") + 1] == str(files[0]), "the operator's file comes first"
    assert docker.override_text == "services:\n  bridge:\n    build:\n      network: host\n" \
                                   "  initialize:\n    build:\n      network: host\n"
    up = next(c for c in docker.calls if "up" in c)
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
    up = next(c for c in compose_calls if "up" in c)
    assert up[up.index("--wait-timeout"):] == ["--wait-timeout", "90", "--force-recreate", "--remove-orphans"]


def test_plan_rollback_skips_digest_pinned_and_ambiguous_images_and_unchanged_tags():
    previous = {
        "bridge": {"image": APP_REF, "image_id": OLD_APP},
        "initialize": {"image": APP_REF, "image_id": OLD_APP},
        "edge": {"image": NGINX_REF, "image_id": "sha256:" + "c" * 64},
        "odd": {"image": "other:local", "image_id": OLD_APP},
        "odd2": {"image": "other:local", "image_id": NEW_APP},
        "same": {"image": "same:local", "image_id": OLD_APP},
    }
    retag, skipped = deploy.plan_rollback(previous, {APP_REF: NEW_APP, "same:local": OLD_APP})
    assert retag == [{"image": APP_REF, "image_id": OLD_APP, "services": ["bridge", "initialize"]}]
    assert {(s["image"], s["reason"]) for s in skipped} == {(NGINX_REF, "pinned by digest"),
                                                           ("other:local", "running services used different images")}


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


def test_parse_inspect_keeps_only_running_compose_services():
    items = [FakeDocker.container("bridge", APP_REF, OLD_APP), FakeDocker.container("migrate", APP_REF, OLD_APP, running=False),
             {"Id": "x", "Image": OLD_APP, "State": {"Running": True}, "Config": {"Image": "stray", "Labels": {}}}]
    assert list(deploy.parse_inspect(json.dumps(items))) == ["bridge"]


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
    (["--base-url", "https://user:pw@baton.example.com"], "invalid --base-url"),
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
    code = deploy.main(["-f", str(compose), "--smoke-token-file", str(token)], runner=docker, http=FakeGateway(docker),
                       stdout=out, stderr=io.StringIO())
    assert code == 2 and "two words" not in out.getvalue() and not docker.calls
    token.unlink()
    assert deploy.main(["-f", str(compose), "--smoke-token-file", str(token)], runner=docker, stdout=io.StringIO(),
                       stderr=io.StringIO()) == 2
    assert deploy.main(["-f", str(compose) + ".missing", "--smoke-token-file", str(token)], runner=docker,
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
