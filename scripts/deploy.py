#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Redeploy a Baton Compose project on the Docker host, smoke-test it and roll back on failure.

Run it on the machine that runs Docker, from the directory your Compose files are addressed from:

    python3 scripts/deploy.py -f deploy/compose.yaml -p baton \\
        --smoke-token-file ~/.config/baton/smoke.token --backup-dir ~/baton-backups

Steps, each with a hard timeout:
  1. preflight: the migration SQL must be backward compatible (expand-only); services that the Compose files no
     longer declare (orphans) must be acknowledged with --allow-remove-orphans;
  2. record the image of every container of the project, running or not; tag each restorable local image
     `baton-deploy-rollback:<id>` so it cannot be garbage-collected;
  3. back up the database with pg_dump (custom format, mode 0600) into --backup-dir;
  4. build (optionally with --build-network host);
  5. `docker compose up -d --wait --force-recreate --remove-orphans` (this also runs the migration);
  6. smoke-test the public entry point: GET /board is 200, an unauthenticated POST /mcp is 401, and one real MCP
     initialize + tools/list + fetch_inbox succeeds with the smoke token.
If `up` or a smoke check fails or times out, the previous images are re-tagged and `up` runs again (rollback). The
rollback only reports success when every service is verified to run its previous image again and the smoke checks
pass; otherwise a best-effort recovery `up` is attempted and the report lists what to restore by hand. A JSON
report goes to stdout; progress goes to stderr.

What a rollback does not do: it restores images, not the database and not the Compose topology. The migration has
already been applied when the smoke test runs, so the previous image must work against the new schema: migrations
are expand-only (add nullable or defaulted columns and tables; never drop, rename, retype or tighten in the same
release), which the preflight enforces. The backup is for a manual restore if that discipline was broken. Images
pinned by digest come from the Compose files; to roll those back, deploy the previous revision of the files.
Removed orphans are not recreated. The smoke token is read from a file, sent only as a bearer header and never printed.

Exit codes: 0 deployed; 1 failed, with the previous deployment running (restored by the rollback, or untouched
because preflight, backup or build failed); 2 usage or configuration error; 3 failed and the previous deployment is
not verified to be running (rollback failed or incomplete, or there was nothing to restore).
Python 3.9+ standard library only.
"""
import argparse
import json
import os
import re
import signal
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
PROJECT_LABEL = 'com.docker.compose.project'
ONEOFF_LABEL = 'com.docker.compose.oneoff'
LOCAL_HOSTS = frozenset({'localhost', '127.0.0.1', '::1'})
SMOKE_STEPS = ('board', 'mcp_unauthenticated', 'mcp_initialize', 'mcp_list_tools', 'mcp_fetch_inbox')
MCP_PROTOCOL = '2025-06-18'
HOLD_REPOSITORY = 'baton-deploy-rollback'
OUTPUT_TAIL = 2000
DATABASE_SERVICE = 'database'
DUMP_SCRIPT = ('PGPASSWORD="$(cat /run/secrets/database-admin/postgres.password)" '
               'exec pg_dump -U baton_bootstrap -d baton -Fc')
DUMP_MAGIC = b'PGDMP'
SCHEMA_FILE = Path(__file__).resolve().parents[1] / 'bridge/space_schema.sql'
UP_GRACE = 120
TIMEOUT_RETURNCODE = 124

# Statements that an already-running previous image cannot tolerate after the migration has run.
INCOMPATIBLE_SQL = (
    ('drops a table, schema, column, view, type, sequence or function',
     r'\bDROP\s+(?:TABLE|SCHEMA|COLUMN|VIEW|MATERIALIZED\s+VIEW|TYPE|SEQUENCE|FUNCTION)\b'),
    ('truncates a table', r'(?:^|;)\s*TRUNCATE\b'),
    ('deletes rows', r'\bDELETE\s+FROM\b'),
    ('renames an object', r'\bRENAME\b'),
    ('changes a column type', r'\bALTER\s+COLUMN\s+\S+\s+(?:SET\s+DATA\s+)?TYPE\b'),
    ('makes an existing column NOT NULL', r'\bALTER\s+COLUMN\s+\S+\s+SET\s+NOT\s+NULL\b'),
)
ADD_COLUMN = re.compile(r'\bADD\s+COLUMN\b([^;]*)', re.IGNORECASE)


class UsageError(Exception):
    """Bad invocation or local configuration; the message never contains secrets."""


class SmokeError(Exception):
    """One smoke step failed; the detail is a short category, never a response body or credential."""


class Result:
    def __init__(self, returncode, stdout='', stderr='', timed_out=False):
        self.returncode, self.stdout, self.stderr, self.timed_out = returncode, stdout, stderr, timed_out


class Response:
    def __init__(self, status, headers, body):
        self.status, self.headers, self.body = status, headers, body


def kill_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()


class SubprocessRunner:
    """Runs a command with a hard timeout; on expiry the whole process group is killed."""

    def __call__(self, argv, timeout=None, stdout_path=None):
        sink = None
        try:
            if stdout_path:
                sink = os.fdopen(os.open(stdout_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'wb')
            try:
                proc = subprocess.Popen(argv, stdout=sink or subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=not sink, start_new_session=True)
            except FileNotFoundError:
                return Result(127, '', 'command not found: ' + argv[0])
            try:
                out, err = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                kill_group(proc)
                proc.communicate()
                return Result(TIMEOUT_RETURNCODE, '', 'timed out after %ss' % timeout, timed_out=True)
            if sink:
                err = err.decode('utf-8', errors='replace')
            return Result(proc.returncode, out or '', err or '')
        finally:
            if sink:
                sink.close()


class Config:
    def __init__(self, compose_files, project=None, env_file=None, project_directory=None, profiles=(), build=True,
                 build_network=None, wait_timeout=300, build_timeout=1800, command_timeout=120, backup_timeout=900,
                 backup_dir=None, no_backup=False, allow_remove_orphans=False, allow_incompatible_migration=False,
                 schema_file=SCHEMA_FILE, base_url='http://127.0.0.1:8080', ca_file=None, smoke_token_file=None,
                 smoke_timeout=60, request_timeout=20, dry_run=False):
        self.compose_files, self.project, self.env_file = compose_files, project, env_file
        self.project_directory, self.profiles, self.build = project_directory, list(profiles), build
        self.build_network, self.wait_timeout, self.build_timeout = build_network, wait_timeout, build_timeout
        self.command_timeout, self.backup_timeout, self.backup_dir = command_timeout, backup_timeout, backup_dir
        self.no_backup, self.allow_remove_orphans = no_backup, allow_remove_orphans
        self.allow_incompatible_migration, self.schema_file = allow_incompatible_migration, schema_file
        self.base_url, self.ca_file, self.smoke_token_file = base_url, ca_file, smoke_token_file
        self.smoke_timeout, self.request_timeout, self.dry_run = smoke_timeout, request_timeout, dry_run

    @property
    def up_timeout(self):
        return self.wait_timeout + UP_GRACE

    def compose_base(self, extra_files=(), extra_profiles=()):
        argv = ['docker', 'compose']
        if self.project_directory:
            argv += ['--project-directory', self.project_directory]
        if self.env_file:
            argv += ['--env-file', self.env_file]
        for path in list(self.compose_files) + list(extra_files):
            argv += ['-f', str(path)]
        if self.project:
            argv += ['-p', self.project]
        for profile in self.profiles + list(extra_profiles):
            argv += ['--profile', profile]
        return argv


class Docker:
    """Runs docker commands with a timeout and records every one. In dry-run only read-only queries execute."""

    def __init__(self, runner, config, log=None):
        self.runner, self.config, self.log = runner, config, log or (lambda message: None)
        self.commands = []

    def run(self, argv, mutating=False, timeout=None, stdout_path=None):
        timeout = timeout or self.config.command_timeout
        entry = {'argv': list(argv), 'mutating': mutating, 'executed': True, 'timeout': timeout}
        self.commands.append(entry)
        if mutating and self.config.dry_run:
            entry['executed'] = False
            return Result(0)
        self.log('$ ' + ' '.join(argv))
        kwargs = {'stdout_path': stdout_path} if stdout_path else {}
        result = self.runner(argv, timeout=timeout, **kwargs)
        entry['returncode'] = result.returncode
        if result.timed_out:
            entry['timed_out'] = True
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
    report = {'ok': result.returncode == 0, 'returncode': result.returncode, 'output_tail': out}
    if result.timed_out:
        report['timed_out'] = True
    return report


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



def unsafe_migration_statements(sql):
    """Return descriptions of statements in the migration SQL that a previous image could not survive."""
    text = re.sub(r'/\*.*?\*/', ' ', re.sub(r'--[^\n]*', ' ', sql), flags=re.DOTALL)
    found = [label for label, pattern in INCOMPATIBLE_SQL if re.search(pattern, text, re.IGNORECASE)]
    for clause in ADD_COLUMN.findall(text):
        if re.search(r'\bNOT\s+NULL\b', clause, re.IGNORECASE) and not re.search(r'\bDEFAULT\b', clause, re.IGNORECASE):
            found.append('adds a NOT NULL column without a default')
            break
    return found


def check_migration(config):
    try:
        sql = Path(config.schema_file).read_text(encoding='utf-8')
    except OSError:
        return {'checked': False, 'reason': 'migration SQL not found', 'unsafe': []}
    unsafe = unsafe_migration_statements(sql)
    return {'checked': True, 'file': str(config.schema_file), 'unsafe': unsafe}


def parse_inspect(text):
    """Map service -> {container, image, image_id, running, created}, one entry per service.

    Running containers win over stopped ones; among stopped ones the newest wins. Run-once containers are skipped.
    """
    try:
        items = json.loads(text)
    except ValueError:
        raise UsageError('unexpected docker inspect output') from None
    services = {}
    for item in items if isinstance(items, list) else []:
        config = item.get('Config') or {}
        labels = config.get('Labels') or {}
        service = labels.get(SERVICE_LABEL)
        if not service or labels.get(ONEOFF_LABEL) == 'True':
            continue
        entry = {'container': item.get('Id', '')[:12], 'image': config.get('Image', ''), 'image_id': item.get('Image', ''),
                 'running': bool((item.get('State') or {}).get('Running')), 'created': item.get('Created', '')}
        known = services.get(service)
        if known is None or (entry['running'], entry['created']) > (known['running'], known['created']):
            services[service] = entry
    return services


def plan_rollback(previous, current_ids):
    """Decide which image references to re-tag. current_ids maps image reference -> image ID now.

    Returns (retag, skipped). Digest-pinned references are immutable and come from the Compose files, so they are
    skipped; references whose running services disagree about the image cannot be restored unambiguously. When no
    service using a reference is running, the newest stopped container decides.
    """
    by_ref = {}
    for service, info in sorted(previous.items()):
        by_ref.setdefault(info['image'], []).append((service, info))
    retag, skipped = [], []
    for ref, members in sorted(by_ref.items()):
        services = [service for service, _ in members]
        running = [info for _, info in members if info.get('running', True)]
        basis = running or [max((info for _, info in members), key=lambda info: info.get('created', ''))]
        ids = {info['image_id'] for info in basis}
        if '@' in ref:
            skipped.append({'image': ref, 'services': services, 'reason': 'pinned by digest'})
        elif len(ids) != 1:
            skipped.append({'image': ref, 'services': services, 'reason': 'running services used different images'})
        else:
            image_id = next(iter(ids))
            if current_ids.get(ref) != image_id:
                retag.append({'image': ref, 'image_id': image_id, 'services': services})
    return retag, skipped


def verify_restored(previous, now):
    """Every previously recorded service must run its previous image again; returns the mismatches."""
    mismatches = []
    for service, info in sorted(previous.items()):
        actual = (now.get(service) or {}).get('image_id')
        if actual != info['image_id']:
            mismatches.append({'service': service, 'expected': info['image_id'], 'actual': actual})
    return mismatches


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


def compose_model(docker, config):
    """Project name, every declared service (all profiles) and the services that have a build section."""
    shown = docker.run(config.compose_base(extra_profiles=['*']) + ['config', '--format', 'json'])
    if shown.returncode != 0:
        raise UsageError('docker compose config failed: ' + tail(shown.stderr, 300))
    try:
        model = json.loads(shown.stdout)
        services = model.get('services', {})
    except (ValueError, AttributeError):
        raise UsageError('unexpected docker compose config output') from None
    project = config.project or model.get('name')
    if not project:
        raise UsageError('cannot determine the Compose project name; pass -p')
    return {'project': project, 'services': sorted(services),
            'build': sorted(name for name, spec in services.items() if isinstance(spec, dict) and 'build' in spec)}


def project_snapshot(docker, project):
    """Images of every container of the project, running or not (run-once `docker compose run` containers excluded)."""
    listed = docker.run(['docker', 'ps', '-a', '-q', '--filter', 'label=%s=%s' % (PROJECT_LABEL, project)])
    if listed.returncode != 0:
        raise UsageError('docker ps failed: ' + tail(listed.stderr, 300))
    ids = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
    if not ids:
        return {}
    inspected = docker.run(['docker', 'inspect'] + ids)
    if inspected.returncode != 0:
        raise UsageError('docker inspect failed: ' + tail(inspected.stderr, 300))
    return parse_inspect(inspected.stdout)


def backup_database(docker, config, project, previous):
    """pg_dump the live database before the migration can run. Returns (report, ok)."""
    if config.no_backup:
        return {'skipped': True, 'reason': 'disabled by --no-backup'}, True
    if not previous:
        return {'skipped': True, 'reason': 'first deployment: no existing containers'}, True
    database = previous.get(DATABASE_SERVICE)
    if not database or not database['running']:
        return {'ok': False, 'error': 'the database service is not running, so it cannot be backed up; start it, '
                                      'or pass --no-backup to deploy without a backup'}, False
    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    path = Path(config.backup_dir) / ('baton-%s-%s.dump' % (re.sub(r'[^A-Za-z0-9_.-]', '_', project), stamp))
    argv = config.compose_base() + ['exec', '-T', DATABASE_SERVICE, 'sh', '-c', DUMP_SCRIPT]
    if config.dry_run:
        docker.run(argv, mutating=True, timeout=config.backup_timeout, stdout_path=str(path))
        return {'planned': True, 'path': str(path)}, True
    try:
        Path(config.backup_dir).mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError:
        return {'ok': False, 'error': 'cannot create --backup-dir'}, False
    result = docker.run(argv, mutating=True, timeout=config.backup_timeout, stdout_path=str(path))
    report = {'path': str(path), 'ok': False}
    try:
        size = path.stat().st_size
        header = path.open('rb').read(len(DUMP_MAGIC))
    except OSError:
        size, header = 0, b''
    if result.returncode != 0 or size == 0 or header != DUMP_MAGIC:
        report['error'] = 'pg_dump failed' + (' (timed out)' if result.timed_out else ': ' + tail(result.stderr, 300))
        path.unlink(missing_ok=True)
        return report, False
    report.update(ok=True, bytes=size)
    return report, True


def run_build(docker, config, model):
    """`docker compose build` has no network flag, so --build-network goes in as a throw-away override file."""
    plain = config.compose_base() + ['build']
    if not config.build_network or not model['build']:
        return docker.run(plain, mutating=True, timeout=config.build_timeout)
    text = 'services:\n' + ''.join('  %s:\n    build:\n      network: %s\n' % (name, config.build_network)
                                   for name in model['build'])
    with tempfile.TemporaryDirectory(prefix='baton-deploy-') as directory:
        override = Path(directory) / 'build-network.yaml'
        override.write_text(text)
        return docker.run(config.compose_base([override]) + ['build'], mutating=True, timeout=config.build_timeout)


def up_command(config):
    return config.compose_base() + ['up', '-d', '--wait', '--wait-timeout', str(config.wait_timeout),
                                    '--force-recreate', '--remove-orphans']


def current_image_ids(docker, refs):
    ids = {}
    for ref in refs:
        result = docker.run(['docker', 'image', 'inspect', '--format', '{{.Id}}', ref])
        if result.returncode == 0 and result.stdout.strip():
            ids[ref] = result.stdout.strip()
    return ids


def manual_recovery(previous, backup):
    steps = ['docker tag <image-id> <image-reference>   # for each image listed under rollback.retagged / held_images',
             'docker compose <your -f and -p options> up -d --wait --force-recreate']
    if backup and backup.get('path') and backup.get('ok'):
        steps.append('restore the database only if the schema is incompatible: pg_restore --clean --if-exists from ' + backup['path'])
    return steps


def roll_back(docker, config, project, previous, orphans, http, token, backup, log, clock, sleep):
    """Re-tag previous images, run `up` again, verify and smoke-test; keep trying to recover on failure."""
    core = {name: info for name, info in previous.items() if name not in orphans}
    refs = sorted({info['image'] for info in core.values()})
    retag, skipped = plan_rollback(core, current_image_ids(docker, refs))
    report = {'attempted': True, 'retagged': [], 'skipped': skipped, 'ok': False, 'errors': [], 'warnings': []}
    for item in retag:
        log('restoring %s -> %s' % (item['image'], item['image_id']))
        result = docker.run(['docker', 'tag', item['image_id'], item['image']], mutating=True)
        report['retagged'].append(dict(item, ok=result.returncode == 0))
        if result.returncode != 0:
            report['errors'].append('could not re-tag %s: %s' % (item['image'], tail(result.stderr, 300)))
    log('rollback: docker compose up')
    up = docker.run(up_command(config), mutating=True, timeout=config.up_timeout)
    report['up'] = command_report(up, [token])
    serving = up.returncode == 0
    if not serving:
        message = 'docker compose up %s during rollback' % ('timed out' if up.timed_out else 'failed')
        log('rollback up failed; trying a best-effort recovery up')
        recovery = docker.run(config.compose_base() + ['up', '-d', '--remove-orphans'], mutating=True,
                              timeout=config.up_timeout)
        report['recovery_up'] = command_report(recovery, [token])
        serving = recovery.returncode == 0
        report['warnings' if serving else 'errors'].append(message)
    try:
        now = project_snapshot(docker, project)
    except UsageError as exc:
        now = {}
        report['errors'].append('cannot verify the restored images: ' + str(exc))
    report['mismatches'] = verify_restored(core, now)
    if report['mismatches']:
        report['errors'].append('services are not running their previous image: ' +
                                ', '.join(item['service'] for item in report['mismatches']))
    if serving:
        report['smoke'] = run_smoke(http, token, config.smoke_timeout, clock, sleep)
        if not report['smoke']['ok']:
            report['errors'].append('the restored deployment fails the smoke checks')
    report['ok'] = serving and not report['errors'] and report['smoke']['ok']
    if not report['ok']:
        report['manual_recovery'] = manual_recovery(previous, backup)
    return report


def deploy(config, runner, http=None, log=None, clock=time.monotonic, sleep=time.sleep):
    log = log or (lambda message: None)
    docker = Docker(runner, config, log)
    token = read_token(config.smoke_token_file)
    if http is None and not config.dry_run:
        http = HttpClient(config.base_url, config.ca_file, config.request_timeout)
    report = {'status': None, 'ok': False, 'dry_run': config.dry_run, 'project': config.project,
              'compose_files': [str(path) for path in config.compose_files], 'base_url': config.base_url,
              'warnings': token_file_warnings(config.smoke_token_file)}
    holds = []

    def stop(status, reason, release=True):
        if release:
            release_holds(docker, holds)
        report.update(status=status, ok=False, commands=docker.commands,
                      rollback={'attempted': False, 'reason': reason})
        return report

    migration = check_migration(config)
    report['migration'] = migration
    if migration['unsafe'] and not config.allow_incompatible_migration:
        return stop('aborted', 'the migration SQL is not backward compatible (%s); nothing was changed; a rollback '
                    'could not restore the previous image. Split it into an expand release and a later contract '
                    'release, or pass --allow-incompatible-migration' % '; '.join(migration['unsafe']))

    model = compose_model(docker, config)
    project = report['project'] = model['project']
    previous = project_snapshot(docker, project)
    report['previous_images'] = previous
    log('recorded %d service image(s), running or stopped' % len(previous))

    orphans = sorted(set(previous) - set(model['services']))
    report['orphans'] = [dict(info, service=name) for name, info in sorted(previous.items()) if name in orphans]
    if orphans and not config.allow_remove_orphans:
        return stop('aborted', 'the Compose files do not declare %s, and up --remove-orphans would delete %s; nothing '
                    'was changed. Check that every -f file is passed, or acknowledge with --allow-remove-orphans'
                    % (', '.join(orphans), 'it' if len(orphans) == 1 else 'them'))

    core = {name: info for name, info in previous.items() if name not in orphans}
    holds = hold_previous_images(docker, core)
    report['held_images'] = holds
    if not all(hold['ok'] for hold in holds):
        return stop('aborted', 'could not protect the previous images; nothing was changed')

    backup, backup_ok = backup_database(docker, config, project, previous)
    report['backup'] = backup
    if not backup_ok:
        return stop('aborted', 'the database backup failed; nothing was changed')

    if config.build:
        log('building')
        build = run_build(docker, config, model)
        report['build'] = command_report(build, [token], config.dry_run)
        if build.returncode != 0:
            return stop('build_failed', 'the build failed or timed out before any container was changed')
    else:
        report['build'] = {'skipped': True}

    log('docker compose up --wait --force-recreate --remove-orphans')
    up = docker.run(up_command(config), mutating=True, timeout=config.up_timeout)
    report['up'] = command_report(up, [token], config.dry_run)

    if config.dry_run:
        retag, skipped = plan_rollback(core, {})
        report.update(status='dry_run', ok=True, smoke={'skipped': True, 'checks': list(SMOKE_STEPS)},
                      rollback={'attempted': False, 'would_retag_if_changed': retag, 'skipped': skipped},
                      commands=docker.commands)
        return report

    smoke = None
    if up.returncode == 0:
        log('smoke tests')
        smoke = run_smoke(http, token, config.smoke_timeout, clock, sleep)
        report['smoke'] = smoke
    else:
        report['smoke'] = {'skipped': True, 'reason': 'docker compose up %s' % ('timed out' if up.timed_out else 'failed')}

    action = decide(up.returncode == 0, smoke, core)
    if action == 'deployed':
        report.update(status='deployed', ok=True, rollback={'attempted': False})
    elif action == 'fail':
        report.update(status='failed', ok=False, rollback={'attempted': False, 'reason': 'no previous deployment existed'})
    else:
        log('deployment failed; rolling back')
        rollback = roll_back(docker, config, project, previous, orphans, http, token, backup, log, clock, sleep)
        report['rollback'] = rollback
        report.update(status='rolled_back' if rollback['ok'] else 'rollback_failed', ok=False)
    if report['status'] != 'rollback_failed':
        release_holds(docker, holds)
    report['commands'] = docker.commands
    return report


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError('must be at least 1')
    return number


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
    parser.add_argument('--wait-timeout', type=positive, default=300, metavar='SECONDS',
                        help='docker compose up --wait-timeout; the script also kills `up` %d s later' % UP_GRACE)
    parser.add_argument('--build-timeout', type=positive, default=1800, metavar='SECONDS', help='hard timeout for the build')
    parser.add_argument('--command-timeout', type=positive, default=120, metavar='SECONDS',
                        help='hard timeout for every other docker command')
    parser.add_argument('--backup-dir', metavar='DIR',
                        help='directory for the pre-deploy pg_dump (created with mode 0700, files 0600)')
    parser.add_argument('--no-backup', action='store_true', help='deploy without a database backup (recorded in the report)')
    parser.add_argument('--backup-timeout', type=positive, default=900, metavar='SECONDS', help='hard timeout for pg_dump')
    parser.add_argument('--allow-remove-orphans', action='store_true',
                        help='acknowledge that containers of services the Compose files no longer declare are deleted')
    parser.add_argument('--allow-incompatible-migration', action='store_true',
                        help='deploy even though the migration SQL has statements the previous image cannot survive')
    parser.add_argument('--base-url', default='http://127.0.0.1:8080', help='public entry point to smoke-test')
    parser.add_argument('--ca-file', help='CA bundle for an https --base-url')
    parser.add_argument('--smoke-token-file', required=True, metavar='PATH',
                        help='file holding a client token for the MCP smoke test; never printed')
    parser.add_argument('--smoke-timeout', type=non_negative, default=60, metavar='SECONDS',
                        help='how long smoke checks may keep retrying before they count as failed')
    parser.add_argument('--request-timeout', type=positive, default=20, metavar='SECONDS', help='per-request timeout')
    parser.add_argument('--dry-run', action='store_true',
                        help='run only read-only docker queries and report the commands that would run')
    args = parser.parse_args(argv)
    if not args.backup_dir and not args.no_backup:
        parser.error('--backup-dir is required (or pass --no-backup to deploy without a database backup)')
    return args


def main(argv=None, runner=None, http=None, stdout=None, stderr=None, clock=time.monotonic, sleep=time.sleep):
    stdout, stderr = stdout or sys.stdout, stderr or sys.stderr
    args = parse_args(argv)
    log = lambda message: print(message, file=stderr, flush=True)
    try:
        for path in args.compose_files:
            if not Path(path).is_file():
                raise UsageError('Compose file not found: ' + path)
        config = Config(args.compose_files, args.project, args.env_file, args.project_directory, args.profiles,
                        args.build, args.build_network, args.wait_timeout, args.build_timeout, args.command_timeout,
                        args.backup_timeout, args.backup_dir, args.no_backup, args.allow_remove_orphans,
                        args.allow_incompatible_migration, SCHEMA_FILE, parse_base_url(args.base_url), args.ca_file,
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
