#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Redeploy a Baton Compose project on the Docker host, smoke-test it and roll back on failure.

Run it on the machine that runs Docker, from the directory your Compose files are addressed from:

    python3 scripts/deploy.py -f deploy/compose.yaml -p baton --smoke-token-file ~/.config/baton/smoke.token

Steps: record the images of the containers that are running now; build (optionally with --build-network host);
`docker compose up -d --wait --force-recreate --remove-orphans`; then smoke-test the public entry point:
GET /board is 200, an unauthenticated POST /mcp is 401, and one real MCP initialize + tools/list + fetch_inbox
succeeds with the smoke token. If the `up` or any smoke check fails, the previous images are re-tagged, `up` runs
again (rollback) and the exit status is non-zero. A JSON report goes to stdout; progress goes to stderr.

Before the build, each previous local image gets a `baton-deploy-rollback:<id>` tag so it cannot be garbage-collected;
the tags are removed again once the deployment is settled. Only locally built or tagged images are restored. Images pinned by digest are defined by the Compose files, and the
database schema is not rolled back, so migrations must stay compatible with the previous application image.
The smoke token is read from a file, sent only as a bearer header and never printed.

Exit codes: 0 deployed; 1 failed, with the previous deployment running (restored by the rollback, or untouched
because the build failed); 2 usage or configuration error; 3 failed and the previous deployment is not running
(rollback failed, or there was nothing to restore).
Python 3.9+ standard library only.
"""
import argparse
import json
import re
import ssl
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib import error, request
from urllib.parse import urlsplit

EXIT_OK, EXIT_ROLLED_BACK, EXIT_USAGE, EXIT_UNRECOVERED = 0, 1, 2, 3
SERVICE_LABEL = 'com.docker.compose.service'
LOCAL_HOSTS = frozenset({'localhost', '127.0.0.1', '::1'})
SMOKE_STEPS = ('board', 'mcp_unauthenticated', 'mcp_initialize', 'mcp_list_tools', 'mcp_fetch_inbox')
MCP_PROTOCOL = '2025-06-18'
HOLD_REPOSITORY = 'baton-deploy-rollback'
OUTPUT_TAIL = 2000
QUERY_TIMEOUT = 120


class UsageError(Exception):
    """Bad invocation or local configuration; the message never contains secrets."""


class SmokeError(Exception):
    """One smoke step failed; the detail is a short category, never a response body or credential."""


class Result:
    def __init__(self, returncode, stdout='', stderr=''):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class Response:
    def __init__(self, status, headers, body):
        self.status, self.headers, self.body = status, headers, body


class SubprocessRunner:
    def __call__(self, argv, timeout=None):
        try:
            done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            return Result(127, '', 'command not found: ' + argv[0])
        except subprocess.TimeoutExpired:
            return Result(124, '', 'timed out')
        return Result(done.returncode, done.stdout, done.stderr)


class Config:
    def __init__(self, compose_files, project, env_file, project_directory, profiles, build, build_network,
                 wait_timeout, base_url, ca_file, smoke_token_file, smoke_timeout, request_timeout, dry_run):
        self.compose_files, self.project, self.env_file = compose_files, project, env_file
        self.project_directory, self.profiles, self.build = project_directory, profiles, build
        self.build_network, self.wait_timeout, self.base_url = build_network, wait_timeout, base_url
        self.ca_file, self.smoke_token_file, self.smoke_timeout = ca_file, smoke_token_file, smoke_timeout
        self.request_timeout, self.dry_run = request_timeout, dry_run

    def compose_base(self, extra_files=()):
        argv = ['docker', 'compose']
        if self.project_directory:
            argv += ['--project-directory', self.project_directory]
        if self.env_file:
            argv += ['--env-file', self.env_file]
        for path in list(self.compose_files) + list(extra_files):
            argv += ['-f', str(path)]
        if self.project:
            argv += ['-p', self.project]
        for profile in self.profiles:
            argv += ['--profile', profile]
        return argv


class Docker:
    """Runs docker commands and records every one. In dry-run mode only read-only queries execute."""

    def __init__(self, runner, dry_run=False, log=None):
        self.runner, self.dry_run, self.log = runner, dry_run, log or (lambda message: None)
        self.commands = []

    def run(self, argv, mutating=False, timeout=None):
        entry = {'argv': list(argv), 'mutating': mutating, 'executed': True}
        self.commands.append(entry)
        if mutating and self.dry_run:
            entry['executed'] = False
            return Result(0)
        self.log('$ ' + ' '.join(argv))
        result = self.runner(argv, timeout=timeout if timeout is not None else (None if mutating else QUERY_TIMEOUT))
        entry['returncode'] = result.returncode
        return result


def tail(text, limit=OUTPUT_TAIL):
    text = (text or '').strip()
    return text if len(text) <= limit else '...' + text[-limit:]


def command_report(result, secrets=(), dry_run=False):
    if dry_run:
        return {'planned': True}
    out = tail((result.stdout or '') + (result.stderr or ''))
    for secret in secrets:
        if secret:
            out = out.replace(secret, '[redacted]')
    return {'ok': result.returncode == 0, 'returncode': result.returncode, 'output_tail': out}


def read_token(path):
    try:
        raw = Path(path).read_text(encoding='ascii')
    except (OSError, ValueError):
        raise UsageError('cannot read --smoke-token-file') from None
    token = raw.strip()
    if not re.fullmatch(r'[\x21-\x7e]+', token):
        raise UsageError('invalid --smoke-token-file: expected a single printable token')
    return token


def token_file_warnings(path):
    try:
        mode = Path(path).stat().st_mode & 0o777
    except OSError:
        return []
    return ['smoke token file is accessible to group or others; use mode 0600'] if mode & 0o077 else []


def parse_base_url(url):
    try:
        parts = urlsplit(url)
        parts.port
    except ValueError:
        raise UsageError('invalid --base-url') from None
    scheme, host = parts.scheme.lower(), parts.hostname
    if scheme not in ('http', 'https') or not host or parts.username is not None or parts.password is not None \
            or parts.query or parts.fragment:
        raise UsageError('invalid --base-url: expected http(s)://host[:port]')
    if scheme == 'http' and host not in LOCAL_HOSTS:
        raise UsageError('refusing to send the smoke token over plain http to a non-local host; use https://')
    return url.rstrip('/')


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class HttpClient:
    """Minimal HTTP caller: no proxies, no redirects, bounded timeout, certificate verification on https."""

    def __init__(self, base_url, ca_file=None, timeout=20):
        self.base_url, self.timeout = base_url, timeout
        handlers = [request.ProxyHandler({}), NoRedirect()]
        if base_url.startswith('https://'):
            try:
                context = ssl.create_default_context(cafile=ca_file or None)
            except (OSError, ssl.SSLError):
                raise UsageError('cannot load --ca-file') from None
            handlers.append(request.HTTPSHandler(context=context))
        self.opener = request.build_opener(*handlers)

    def __call__(self, method, path, headers=None, body=None):
        req = request.Request(self.base_url + path, data=body, method=method, headers=headers or {})
        try:
            with self.opener.open(req, timeout=self.timeout) as response:
                return Response(response.status, dict((k.lower(), v) for k, v in response.headers.items()), response.read())
        except error.HTTPError as exc:
            body = exc.read()
            return Response(exc.code, dict((k.lower(), v) for k, v in exc.headers.items()), body)
        except (error.URLError, OSError, ValueError) as exc:
            reason = getattr(exc, 'reason', exc)
            raise SmokeError('request failed: ' + type(reason).__name__) from None


def rpc_result(response, request_id):
    """Return the JSON-RPC result for request_id from a JSON or SSE response body."""
    content_type = response.headers.get('content-type', '')
    text = response.body.decode('utf-8', errors='replace')
    candidates = []
    if content_type.startswith('text/event-stream'):
        for line in text.splitlines():
            if line.startswith('data:'):
                candidates.append(line[5:].strip())
    else:
        candidates.append(text)
    for candidate in candidates:
        try:
            message = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(message, dict) and message.get('id') == request_id:
            if 'error' in message:
                raise SmokeError('JSON-RPC error response')
            if 'result' in message:
                return message['result']
    raise SmokeError('no JSON-RPC result in response')


class McpSession:
    def __init__(self, http, token):
        self.http, self.token, self.session_id, self.protocol, self.next_id = http, token, None, None, 0

    def _headers(self):
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream',
                   'Authorization': 'Bearer ' + self.token}
        if self.protocol:
            headers['MCP-Protocol-Version'] = self.protocol
        if self.session_id:
            headers['Mcp-Session-Id'] = self.session_id
        return headers

    def _post(self, payload):
        response = self.http('POST', '/mcp', self._headers(), json.dumps(payload).encode())
        if response.status not in (200, 202):
            raise SmokeError('HTTP %s' % response.status)
        return response

    def call(self, method, params=None):
        self.next_id += 1
        payload = {'jsonrpc': '2.0', 'id': self.next_id, 'method': method}
        if params is not None:
            payload['params'] = params
        response = self._post(payload)
        if response.status != 200:
            raise SmokeError('HTTP %s' % response.status)
        return response, rpc_result(response, self.next_id)

    def notify(self, method):
        self._post({'jsonrpc': '2.0', 'method': method})

    def initialize(self):
        response, result = self.call('initialize', {'protocolVersion': MCP_PROTOCOL, 'capabilities': {},
                                                    'clientInfo': {'name': 'baton-deploy-smoke', 'version': '1'}})
        if not isinstance(result, dict) or 'serverInfo' not in result:
            raise SmokeError('initialize result has no serverInfo')
        self.session_id = response.headers.get('mcp-session-id')
        self.protocol = result.get('protocolVersion') or MCP_PROTOCOL
        self.notify('notifications/initialized')
        return 'protocol ' + str(self.protocol)

    def list_tools(self):
        _, result = self.call('tools/list')
        names = [tool.get('name') for tool in result.get('tools', []) if isinstance(tool, dict)] if isinstance(result, dict) else []
        if 'fetch_inbox' not in names:
            raise SmokeError('tools/list does not include fetch_inbox')
        return '%d tools' % len(names)

    def fetch_inbox(self):
        _, result = self.call('tools/call', {'name': 'fetch_inbox', 'arguments': {}})
        if not isinstance(result, dict) or result.get('isError') or not result.get('content'):
            raise SmokeError('fetch_inbox returned an error')
        return 'ok'


def check_board(http):
    status = http('GET', '/board').status
    if status != 200:
        raise SmokeError('expected HTTP 200, got %s' % status)
    return 'HTTP 200'


def check_unauthenticated(http):
    headers = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream'}
    status = http('POST', '/mcp', headers, b'{}').status
    if status != 401:
        raise SmokeError('expected HTTP 401, got %s' % status)
    return 'HTTP 401'


def smoke_checks(http, token):
    """Ordered (name, callable) pairs; MCP steps share one session and are skipped after the first failure."""
    session = McpSession(http, token)
    return [('board', lambda: check_board(http)),
            ('mcp_unauthenticated', lambda: check_unauthenticated(http)),
            ('mcp_initialize', session.initialize),
            ('mcp_list_tools', session.list_tools),
            ('mcp_fetch_inbox', session.fetch_inbox)]


def run_smoke_once(http, token):
    results, blocked = [], False
    for name, check in smoke_checks(http, token):
        if blocked and name.startswith('mcp_') and name != 'mcp_unauthenticated':
            results.append({'name': name, 'ok': False, 'detail': 'skipped: an earlier MCP step failed'})
            continue
        try:
            detail, ok = check(), True
        except SmokeError as exc:
            detail, ok = str(exc), False
            blocked = blocked or name.startswith('mcp_') and name != 'mcp_unauthenticated'
        results.append({'name': name, 'ok': ok, 'detail': detail})
    return results


def run_smoke(http, token, timeout, clock=time.monotonic, sleep=time.sleep, interval=2):
    """Repeat the whole smoke run until it passes or the window closes; the last attempt is the verdict."""
    deadline = clock() + timeout
    while True:
        results = run_smoke_once(http, token)
        if all(item['ok'] for item in results) or clock() >= deadline:
            return {'ok': all(item['ok'] for item in results), 'checks': results}
        sleep(interval)


def parse_inspect(text):
    """Map service -> {container, image, image_id} for the running containers in `docker inspect` JSON."""
    try:
        items = json.loads(text)
    except ValueError:
        raise UsageError('unexpected docker inspect output') from None
    services = {}
    for item in items if isinstance(items, list) else []:
        config = item.get('Config') or {}
        service = (config.get('Labels') or {}).get(SERVICE_LABEL)
        if not service or not (item.get('State') or {}).get('Running'):
            continue
        services[service] = {'container': item.get('Id', '')[:12], 'image': config.get('Image', ''),
                             'image_id': item.get('Image', '')}
    return services


def plan_rollback(previous, current_ids):
    """Decide which image references to re-tag. current_ids maps image reference -> image ID now.

    Returns (retag, skipped). Digest-pinned references are immutable and come from the Compose files, so they are
    skipped; references whose running services disagreed about the image cannot be restored unambiguously.
    """
    by_ref = {}
    for service, info in sorted(previous.items()):
        by_ref.setdefault(info['image'], {'ids': set(), 'services': []})
        by_ref[info['image']]['ids'].add(info['image_id'])
        by_ref[info['image']]['services'].append(service)
    retag, skipped = [], []
    for ref, entry in sorted(by_ref.items()):
        services = entry['services']
        if '@' in ref:
            skipped.append({'image': ref, 'services': services, 'reason': 'pinned by digest'})
        elif len(entry['ids']) != 1:
            skipped.append({'image': ref, 'services': services, 'reason': 'running services used different images'})
        else:
            image_id = next(iter(entry['ids']))
            if current_ids.get(ref) != image_id:
                retag.append({'image': ref, 'image_id': image_id, 'services': services})
    return retag, skipped


def hold_tag(image_id):
    return '%s:%s' % (HOLD_REPOSITORY, image_id.split(':')[-1][:12])


def hold_previous_images(docker, previous):
    """Give every restorable previous image a tag of its own.

    Recreating containers leaves the old image untagged, and the Docker image store may delete an unreferenced
    image at once, which would make the rollback impossible.
    """
    holds = []
    for item in plan_rollback(previous, {})[0]:
        tag = hold_tag(item['image_id'])
        result = docker.run(['docker', 'tag', item['image_id'], tag], mutating=True)
        holds.append({'tag': tag, 'image_id': item['image_id'], 'ok': result.returncode == 0})
    return holds


def release_holds(docker, holds):
    for hold in holds:
        if hold['ok']:
            hold['released'] = docker.run(['docker', 'rmi', hold['tag']], mutating=True).returncode == 0
    return holds


def decide(up_ok, smoke, previous):
    """One of deployed, rollback, fail (nothing to restore)."""
    if up_ok and smoke is not None and smoke['ok']:
        return 'deployed'
    return 'rollback' if previous else 'fail'


def exit_code(status):
    return {'deployed': EXIT_OK, 'dry_run': EXIT_OK, 'rolled_back': EXIT_ROLLED_BACK,
                                'build_failed': EXIT_ROLLED_BACK, 'aborted': EXIT_ROLLED_BACK}.get(status, EXIT_UNRECOVERED)


def snapshot_running(docker, config):
    listed = docker.run(config.compose_base() + ['ps', '-q'])
    if listed.returncode != 0:
        raise UsageError('docker compose ps failed: ' + tail(listed.stderr, 300))
    ids = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
    if not ids:
        return {}
    inspected = docker.run(['docker', 'inspect'] + ids)
    if inspected.returncode != 0:
        raise UsageError('docker inspect failed: ' + tail(inspected.stderr, 300))
    return parse_inspect(inspected.stdout)


def build_services(docker, config):
    shown = docker.run(config.compose_base() + ['config', '--format', 'json'])
    if shown.returncode != 0:
        raise UsageError('docker compose config failed: ' + tail(shown.stderr, 300))
    try:
        services = json.loads(shown.stdout).get('services', {})
    except ValueError:
        raise UsageError('unexpected docker compose config output') from None
    return sorted(name for name, spec in services.items() if isinstance(spec, dict) and 'build' in spec)


def run_build(docker, config):
    """`docker compose build` has no network flag, so --build-network goes in as a throw-away override file."""
    if not config.build_network:
        return docker.run(config.compose_base() + ['build'], mutating=True)
    names = build_services(docker, config)
    if not names:
        return docker.run(config.compose_base() + ['build'], mutating=True)
    text = 'services:\n' + ''.join('  %s:\n    build:\n      network: %s\n' % (name, config.build_network) for name in names)
    with tempfile.TemporaryDirectory(prefix='baton-deploy-') as directory:
        override = Path(directory) / 'build-network.yaml'
        override.write_text(text)
        return docker.run(config.compose_base([override]) + ['build'], mutating=True)


def up_command(config):
    argv = config.compose_base() + ['up', '-d', '--wait']
    if config.wait_timeout:
        argv += ['--wait-timeout', str(config.wait_timeout)]
    return argv + ['--force-recreate', '--remove-orphans']


def current_image_ids(docker, refs):
    ids = {}
    for ref in refs:
        result = docker.run(['docker', 'image', 'inspect', '--format', '{{.Id}}', ref])
        if result.returncode == 0 and result.stdout.strip():
            ids[ref] = result.stdout.strip()
    return ids


def roll_back(docker, config, previous, http, token, log, clock, sleep):
    """Re-tag previous images, run `up` again and smoke-test the restored deployment."""
    refs = sorted({info['image'] for info in previous.values()})
    retag, skipped = plan_rollback(previous, current_image_ids(docker, refs))
    report = {'attempted': True, 'retagged': [], 'skipped': skipped, 'ok': False}
    for item in retag:
        log('restoring %s -> %s' % (item['image'], item['image_id']))
        result = docker.run(['docker', 'tag', item['image_id'], item['image']], mutating=True)
        report['retagged'].append(dict(item, ok=result.returncode == 0))
        if result.returncode != 0:
            report['error'] = 'could not re-tag ' + item['image'] + ': ' + tail(result.stderr, 300)
            return report
    log('rollback: docker compose up')
    up = docker.run(up_command(config), mutating=True)
    report['up'] = command_report(up, [token])
    if up.returncode != 0:
        report['error'] = 'docker compose up failed after restoring the previous images'
        return report
    report['smoke'] = run_smoke(http, token, config.smoke_timeout, clock, sleep)
    report['ok'] = report['smoke']['ok']
    if not report['ok']:
        report['error'] = 'the restored deployment also fails the smoke checks'
    return report


def deploy(config, runner, http=None, log=None, clock=time.monotonic, sleep=time.sleep):
    log = log or (lambda message: None)
    docker = Docker(runner, config.dry_run, log)
    token = read_token(config.smoke_token_file)
    if http is None and not config.dry_run:
        http = HttpClient(config.base_url, config.ca_file, config.request_timeout)
    report = {'status': None, 'ok': False, 'dry_run': config.dry_run, 'project': config.project,
              'compose_files': [str(path) for path in config.compose_files], 'base_url': config.base_url,
              'warnings': token_file_warnings(config.smoke_token_file)}

    previous = snapshot_running(docker, config)
    report['previous_images'] = previous
    log('recorded %d running service image(s)' % len(previous))

    holds = hold_previous_images(docker, previous)
    report['held_images'] = holds
    if not all(hold['ok'] for hold in holds):
        report.update(status='aborted', ok=False, commands=docker.commands,
                      rollback={'attempted': False, 'reason': 'could not protect the previous images; nothing was changed'})
        return report

    build = None
    if config.build:
        log('building')
        build = run_build(docker, config)
        report['build'] = command_report(build, [token], config.dry_run)
        if build.returncode != 0:
            report.update(status='build_failed', ok=False,
                          rollback={'attempted': False, 'reason': 'the build failed before any container was changed'})
            report['commands'] = docker.commands
            release_holds(docker, holds)
            return report
    else:
        report['build'] = {'skipped': True}

    log('docker compose up --wait --force-recreate --remove-orphans')
    up = docker.run(up_command(config), mutating=True)
    report['up'] = command_report(up, [token], config.dry_run)

    smoke = None
    if config.dry_run:
        retag, skipped = plan_rollback(previous, {})
        report.update(status='dry_run', ok=True, smoke={'skipped': True, 'checks': list(SMOKE_STEPS)},
                      rollback={'attempted': False, 'would_retag_if_changed': retag, 'skipped': skipped})
        report['commands'] = docker.commands
        return report
    if up.returncode == 0:
        log('smoke tests')
        smoke = run_smoke(http, token, config.smoke_timeout, clock, sleep)
        report['smoke'] = smoke
    else:
        report['smoke'] = {'skipped': True, 'reason': 'docker compose up failed'}

    action = decide(up.returncode == 0, smoke, previous)
    if action == 'deployed':
        report.update(status='deployed', ok=True, rollback={'attempted': False})
    elif action == 'fail':
        report.update(status='failed', ok=False, rollback={'attempted': False, 'reason': 'no previous deployment was running'})
    else:
        log('deployment failed; rolling back')
        rollback = roll_back(docker, config, previous, http, token, log, clock, sleep)
        report['rollback'] = rollback
        report.update(status='rolled_back' if rollback['ok'] else 'rollback_failed', ok=False)
    if report['status'] != 'rollback_failed':
        release_holds(docker, holds)
    report['commands'] = docker.commands
    return report


def non_negative(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError('must not be negative')
    return number


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('-f', '--file', dest='compose_files', action='append', required=True, metavar='FILE',
                        help='Compose file; repeat in the same order as for docker compose')
    parser.add_argument('-p', '--project-name', dest='project', help='Compose project name')
    parser.add_argument('--env-file', help='Compose environment file')
    parser.add_argument('--project-directory', help='Compose project directory')
    parser.add_argument('--profile', dest='profiles', action='append', default=[], help='Compose profile; repeatable')
    parser.add_argument('--build-network', metavar='MODE',
                        help='network for image builds, for example host; applied through a temporary Compose override')
    parser.add_argument('--no-build', dest='build', action='store_false', help='skip the build step')
    parser.add_argument('--wait-timeout', type=non_negative, default=0, metavar='SECONDS', help='docker compose up --wait-timeout')
    parser.add_argument('--base-url', default='http://127.0.0.1:8080', help='public entry point to smoke-test')
    parser.add_argument('--ca-file', help='CA bundle for an https --base-url')
    parser.add_argument('--smoke-token-file', required=True, metavar='PATH',
                        help='file holding a client token for the MCP smoke test; never printed')
    parser.add_argument('--smoke-timeout', type=non_negative, default=60, metavar='SECONDS',
                        help='how long smoke checks may keep retrying before they count as failed')
    parser.add_argument('--request-timeout', type=non_negative, default=20, metavar='SECONDS', help='per-request timeout')
    parser.add_argument('--dry-run', action='store_true',
                        help='run only read-only docker queries and report the commands that would run')
    return parser.parse_args(argv)


def main(argv=None, runner=None, http=None, stdout=None, stderr=None, clock=time.monotonic, sleep=time.sleep):
    stdout, stderr = stdout or sys.stdout, stderr or sys.stderr
    args = parse_args(argv)
    log = lambda message: print(message, file=stderr, flush=True)
    try:
        for path in args.compose_files:
            if not Path(path).is_file():
                raise UsageError('Compose file not found: ' + path)
        config = Config(args.compose_files, args.project, args.env_file, args.project_directory, args.profiles,
                        args.build, args.build_network, args.wait_timeout, parse_base_url(args.base_url), args.ca_file,
                        args.smoke_token_file, args.smoke_timeout, args.request_timeout, args.dry_run)
        report = deploy(config, runner or SubprocessRunner(), http, log, clock, sleep)
    except UsageError as exc:
        print(json.dumps({'status': 'error', 'ok': False, 'error': str(exc)}, indent=2), file=stdout)
        return EXIT_USAGE
    print(json.dumps(report, indent=2, sort_keys=True), file=stdout)
    if report['status'] not in ('deployed', 'dry_run'):
        log('DEPLOY FAILED: ' + report['status'])
    return exit_code(report['status'])


if __name__ == '__main__':
    sys.exit(main())
