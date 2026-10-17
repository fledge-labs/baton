#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Redeploy a Baton Compose project on the Docker host, smoke-test it and roll back on failure.

Run it on the machine that runs Docker, from the directory your Compose files are addressed from:

    python3 scripts/deploy.py -f deploy/compose.yaml -p baton \\
        --smoke-token-file ~/.config/baton/smoke.token --backup-dir ~/baton-backups

Steps, each with a hard timeout (the recovery path too):
  1. preflight: read the migration SQL (a missing file is an error) and run a textual early warning over it;
     when a build will run, refuse unless every tracked path the build context would include is readable by
     other users (files need o+r, directories o+rx), before any docker command. Paths excluded by
     .dockerignore, including deploy/.env, may stay private. --no-build skips this check. Services that the
     Compose files no longer declare (orphans) must be acknowledged with --allow-remove-orphans;
  2. take an exclusive flock for this project (wait is bounded by --lock-timeout). Two deploys cannot interleave
     tagging, pg_dump, up, or a rollback's docker tag. The lock is held until this process returns;
  3. record the image of every container of the project, running or not, and tag each local image per service as
     `baton-deploy-rollback:<service>-<id>` so it cannot be garbage-collected;
  4. if a database volume exists, make sure the database container is up (even when every application container
     is stopped or gone), then (a) precheck the migration in a separate Postgres container (same image, --network none,
     its own superuser, tmpfs data) loaded from a schema-only dump and a roles-only dump with no passwords; the
     migration runs there the way migrate.py runs it, and the catalog diff includes roles, memberships, privileges,
     row level security and index shape; any breaking change, or any step that fails, aborts (fail closed); and
     (b) back up the database with pg_dump (custom format) into --backup-dir, still before the real migration. The dump file is
     created with O_EXCL|O_NOFOLLOW and fchmod 0600 under an unpredictable name; an existing file or symbolic link
     is left untouched and the backup fails. A failed dump aborts;
  5. build (optionally with --build-network host). The image itself also runs `chmod -R a+rX /app` after COPY,
     so a 0600 checkout cannot leave migrate (uid 999) unable to read the schema;
  6. `docker compose up -d --wait --force-recreate --remove-orphans` (this also runs the migration);
  7. smoke-test the public entry point: GET /board is 200, an unauthenticated POST /mcp is 401, and one real MCP
     initialize + tools/list + fetch_inbox succeeds with the smoke token.
If `up` or a smoke check fails or times out, the previous images are re-tagged and `up` runs again (rollback). The
rollback only reports success when every service is verified to run its previous image again and the smoke checks
pass; otherwise a best-effort recovery `up` (still waiting, still time-limited) is attempted and the report lists
what to restore by hand. A JSON report goes to stdout; progress goes to stderr.

What a rollback does not do: it restores images, not the database and not the Compose topology. The migration has
already been applied when the smoke test runs, so the previous image must work against the new schema: migrations
are expand-only (add nullable or defaulted columns and tables; never drop, rename, retype, tighten, change
privileges, or enable row level security in the same release), which the throwaway-database precheck enforces.
A rollback does not undo GRANT, REVOKE, policies, or row level security that already committed. The backup is for a
manual restore if that discipline was broken. Images pinned by digest come from the Compose files; to roll those back, deploy the previous revision of the
files. Removed orphans are not recreated. If every container was removed there is no previous image to restore.
The smoke token is read from a file, sent only as a bearer header and never printed.

Exit codes: 0 deployed; 1 failed, with the previous deployment running (restored by the rollback, or untouched
because preflight, backup or build failed); 2 usage or configuration error (including a checkout other users
cannot read); 3 failed and the previous deployment is
not verified to be running (rollback failed or incomplete, or there was nothing to restore).
Python 3.9+ standard library only.
"""
import argparse
import fcntl
import http.client
import json
import os
import re
import secrets
import signal
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
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
DATA_VOLUME = 'postgres-data'
DUMP_SCRIPT = ('PGPASSWORD="$(cat /run/secrets/database-admin/postgres.password)" '
               'exec pg_dump -U baton_bootstrap -d baton -Fc')
DUMP_MAGIC = b'PGDMP'
SCHEMA_FILE = Path(__file__).resolve().parents[1] / 'bridge/space_schema.sql'
UP_GRACE = 120
TIMEOUT_RETURNCODE = 124
PRIVATE_OUTPUT_ERROR = 'cannot create a private output file'
LOCK_DIR = Path(tempfile.gettempdir()) / 'baton-deploy-locks'
BACKUP_CREATE_ATTEMPTS = 3

# Early warning only: textual patterns for statements a previous image cannot survive. The authoritative check is
# the throwaway-database catalog diff (precheck_migration); this just fails fast on the obvious cases.
IDENT = r'[\w".$]+'
INCOMPATIBLE_SQL = (
    ('contains a DROP other than constraint, trigger, index, policy, default, rule or statistics',
     r'\bDROP\b(?!\s+(?:CONSTRAINT|TRIGGER|INDEX|POLICY|DEFAULT|RULE|STATISTICS)\b)'),
    ('truncates a table', r'(?:^|;)\s*TRUNCATE\b'),
    ('deletes rows', r'\bDELETE\s+FROM\b'),
    # Data rewrites are invisible to a schema-only catalog. Trigger clauses (BEFORE UPDATE ON/OR/OF) are not
    # statements. Foreign-key actions are removed by FK_ACTION before this runs, so ON UPDATE CASCADE / SET
    # NULL / SET DEFAULT / RESTRICT / NO ACTION do not look like UPDATE. ON CONFLICT DO UPDATE still does.
    ('rewrites rows', r'\bUPDATE\s+(?:ONLY\s+)?(?!OR\b|OF\b|ON\b)(?:[\w."]+\.)?[\w."]+'),
    ('merges rows', r'\bMERGE\s+INTO\b'),
    ('renames an object', r'\bRENAME\b'),
    ('changes a column type', r'\bALTER\s+(?:COLUMN\s+)?' + IDENT + r'\s+(?:SET\s+DATA\s+)?TYPE\b'),
    ('makes an existing column NOT NULL', r'\bSET\s+NOT\s+NULL\b'),
    # A rollback re-tags images; it does not undo privileges or row level security that already committed.
    ('changes privileges', r'\b(?:GRANT|REVOKE)\b'),
    ('changes a row level security policy', r'\b(?:CREATE|ALTER|DROP)\s+POLICY\b'),
    ('changes row level security',
     r'\b(?:NO\s+FORCE|ENABLE|DISABLE|FORCE)\s+ROW\s+LEVEL\s+SECURITY\b'),
    ('disables a trigger', r'\bDISABLE\s+TRIGGER\b'),
    # Trust boundary: repository migrations are reviewed code. These are accidental hazards the catalog snapshot
    # does not record (server settings, program side effects, anonymous blocks). Reject them here so the same file
    # is not replayed on the live database. ON CONFLICT DO NOTHING is not a DO block.
    # Any SET/RESET whose target is the role or session authorization, with LOCAL/SESSION in either order.
    # set_config('role'|...) is matched separately, before strings are blanked.
    ('switches role',
     r'(?:^|;)\s*(?:set|reset)\s+(?:(?:local|session)\s+)*?(?:role|session\s+authorization|session_authorization)\b'),
    ('changes server configuration', r'\bALTER\s+SYSTEM\b'),
    ('changes database settings', r'\bALTER\s+DATABASE\b'),
    ('changes roles', r'\b(?:ALTER|CREATE|DROP)\s+ROLE\b'),
    ('runs a program from COPY', r'\bCOPY\b[^;]*\b(?:TO|FROM)\s+PROGRAM\b'),
    ('creates an extension', r'\bCREATE\s+EXTENSION\b'),
    # DO blocks and transaction control are decided by _statement_hazards(), which sees the body literal
    # before strings are blanked and ignores PL/pgSQL inside dollar quotes.
)
ADD_CLAUSE = re.compile(r'\bADD\s+(?:COLUMN\s+)?(?:IF\s+NOT\s+EXISTS\s+)?'
                        r'(?!CONSTRAINT\b|PRIMARY\b|UNIQUE\b|CHECK\b|FOREIGN\b|LIKE\b|EXCLUDE\b)([^;]*?)'
                        r'(?=,\s*(?:ADD|ALTER|DROP)\b|;|$)', re.IGNORECASE | re.DOTALL)
NOISE = re.compile(r"--[^\n]*|/\*.*?\*/|'(?:[^']|'')*'", re.DOTALL)
FK_ACTION = re.compile(
    r'\bON\s+(?:UPDATE|DELETE)\s+(?:CASCADE|RESTRICT|NO\s+ACTION|SET\s+(?:NULL|DEFAULT))\b', re.IGNORECASE)
PRECHECK_PREFIX = 'baton-precheck-'
MAX_SMOKE_BODY = 1024 * 1024
ROLES_DUMP = ('PGPASSWORD="$(cat /run/secrets/database-admin/postgres.password)" '
              'exec pg_dumpall -U baton_bootstrap --roles-only --no-role-passwords')
SCHEMA_DUMP = ('PGPASSWORD="$(cat /run/secrets/database-admin/postgres.password)" '
               'exec pg_dump -U baton_bootstrap -d baton --schema-only')
SYSTEM_SCHEMAS = "n.nspname !~ '^(pg_|information_schema$)'"
CATALOG_SQL = """SELECT json_build_object(
 'columns', (SELECT coalesce(json_agg(json_build_object('t', n.nspname||'.'||c.relname, 'c', a.attname,
     'type', format_type(a.atttypid, a.atttypmod), 'notnull', a.attnotnull,
     'default', pg_get_expr(d.adbin, d.adrelid), 'hasdef', (d.adbin IS NOT NULL OR a.attidentity <> '' OR a.attgenerated <> ''))), '[]'::json)
   FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace
   LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
   WHERE a.attnum > 0 AND NOT a.attisdropped AND c.relkind IN ('r','p','v','m','f') AND %(system)s),
 'relations', (SELECT coalesce(json_agg(n.nspname||'.'||c.relname||':'||c.relkind::text), '[]'::json)
   FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
   WHERE c.relkind IN ('r','p','v','m','S','f') AND %(system)s),
 'constraints', (SELECT coalesce(json_agg(json_build_object('t', n.nspname||'.'||c.relname, 'name', k.conname,
     'def', pg_get_constraintdef(k.oid),
     'cols', (SELECT coalesce(json_agg(a.attname), '[]'::json) FROM pg_attribute a
              WHERE a.attrelid = k.conrelid AND a.attnum = ANY(k.conkey)))), '[]'::json)
   FROM pg_constraint k JOIN pg_class c ON c.oid = k.conrelid JOIN pg_namespace n ON n.oid = c.relnamespace WHERE %(system)s),
 'indexes', (SELECT coalesce(json_agg(json_build_object('t', n.nspname||'.'||c.relname, 'name', ic.relname,
     'def', pg_get_indexdef(i.indexrelid), 'unique', i.indisunique,
     'pred', pg_get_expr(i.indpred, i.indrelid),
     'cols', (SELECT coalesce(json_agg(a.attname), '[]'::json) FROM pg_attribute a
              WHERE a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)))), '[]'::json)
   FROM pg_index i JOIN pg_class ic ON ic.oid = i.indexrelid JOIN pg_class c ON c.oid = i.indrelid
   JOIN pg_namespace n ON n.oid = c.relnamespace WHERE %(system)s),
 'triggers', (SELECT coalesce(json_agg(json_build_object('t', n.nspname||'.'||c.relname, 'name', g.tgname,
     'def', pg_get_triggerdef(g.oid), 'enabled', g.tgenabled, 'internal', g.tgisinternal)), '[]'::json)
   FROM pg_trigger g JOIN pg_class c ON c.oid = g.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace
   WHERE %(system)s),
 'functions', (SELECT coalesce(json_agg(json_build_object('sig', p.oid::regprocedure::text, 'def', pg_get_functiondef(p.oid))), '[]'::json)
   FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE p.prokind IN ('f','p') AND %(system)s),
 'types', (SELECT coalesce(json_agg(json_build_object('name', n.nspname||'.'||t.typname,
     'def', t.typtype::text||':'||coalesce((SELECT string_agg(e.enumlabel, ',' ORDER BY e.enumsortorder) FROM pg_enum e WHERE e.enumtypid = t.oid), ''))), '[]'::json)
   FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace WHERE t.typtype IN ('e','d') AND %(system)s),
 'policies', (SELECT coalesce(json_agg(json_build_object('t', n.nspname||'.'||c.relname, 'name', p.polname,
     'permissive', p.polpermissive, 'cmd', p.polcmd::text, 'roles', coalesce(p.polroles::text, ''),
     'qual', pg_get_expr(p.polqual, p.polrelid), 'with_check', pg_get_expr(p.polwithcheck, p.polrelid))), '[]'::json)
   FROM pg_policy p JOIN pg_class c ON c.oid = p.polrelid JOIN pg_namespace n ON n.oid = c.relnamespace WHERE %(system)s),
 'row_security', (SELECT coalesce(json_agg(json_build_object('t', n.nspname||'.'||c.relname,
     'enabled', c.relrowsecurity, 'forced', c.relforcerowsecurity)), '[]'::json)
   FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
   WHERE c.relkind IN ('r','p') AND %(system)s),
 'privileges', (SELECT coalesce(json_agg(json_build_object('t', n.nspname||'.'||c.relname,
     'acl', coalesce(c.relacl::text, ''))), '[]'::json)
   FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
   WHERE c.relkind IN ('r','p','v','m','S','f') AND %(system)s),
 'column_privileges', (SELECT coalesce(json_agg(json_build_object('t', n.nspname||'.'||c.relname, 'c', a.attname,
     'acl', a.attacl::text)), '[]'::json)
   FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace
   WHERE a.attnum > 0 AND NOT a.attisdropped AND a.attacl IS NOT NULL AND c.relkind IN ('r','p','v','m') AND %(system)s),
 'schema_privileges', (SELECT coalesce(json_agg(json_build_object('n', n.nspname,
     'acl', coalesce(n.nspacl::text, ''))), '[]'::json)
   FROM pg_namespace n WHERE n.nspname !~ '^(pg_|information_schema$)'),
 'function_privileges', (SELECT coalesce(json_agg(json_build_object('sig', p.oid::regprocedure::text,
     'acl', coalesce(p.proacl::text, ''))), '[]'::json)
   FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE p.prokind IN ('f','p') AND %(system)s),
 'database_privileges', (SELECT coalesce(json_agg(json_build_object('acl', coalesce(d.datacl::text, ''))), '[]'::json)
   FROM pg_database d WHERE d.datname = current_database()),
 'roles', (SELECT coalesce(json_agg(json_build_object('name', r.rolname, 'super', r.rolsuper,
     'inherit', r.rolinherit, 'createrole', r.rolcreaterole, 'createdb', r.rolcreatedb, 'login', r.rolcanlogin,
     'replication', r.rolreplication, 'bypassrls', r.rolbypassrls, 'connlimit', r.rolconnlimit,
     'password', md5(r.rolpassword)) ORDER BY r.rolname), '[]'::json)
   FROM pg_roles r WHERE r.rolname !~ '^pg_'),
 'memberships', (SELECT coalesce(json_agg(json_build_object('role', r.rolname, 'member', m.rolname,
     'admin', a.admin_option, 'inherit', a.inherit_option, 'set', a.set_option)
     ORDER BY r.rolname, m.rolname), '[]'::json)
   FROM pg_auth_members a JOIN pg_roles r ON r.oid = a.roleid JOIN pg_roles m ON m.oid = a.member)
);
""" % {'system': SYSTEM_SCHEMAS}
SNAPSHOT_KEYS = ('columns', 'relations', 'constraints', 'indexes', 'triggers', 'functions', 'types',
                 'policies', 'row_security', 'privileges', 'column_privileges', 'schema_privileges',
                 'function_privileges', 'database_privileges', 'roles', 'memberships')


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


REAP_SECONDS = 5


class SubprocessRunner:
    """Runs a command with a hard timeout; on expiry the whole process group is killed.

    Even the wait after the kill is bounded: a descendant that escaped the group and still holds the pipes
    cannot keep the deploy waiting.
    """

    def __call__(self, argv, timeout=None, stdout_path=None, stdin_text=None):
        sink = None
        try:
            if stdout_path:
                try:
                    sink = os.fdopen(open_private_file(stdout_path), 'wb')
                except UsageError:
                    raise
                except OSError as exc:
                    return Result(1, '', '%s: %s' % (PRIVATE_OUTPUT_ERROR, exc.strerror or exc.errno))
            try:
                proc = subprocess.Popen(argv, stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                                        stdout=sink or subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=not sink, start_new_session=True)
            except FileNotFoundError:
                return Result(127, '', 'command not found: ' + argv[0])
            data = stdin_text
            if data is not None and sink:
                data = data.encode()
            try:
                out, err = proc.communicate(input=data, timeout=timeout)
            except subprocess.TimeoutExpired:
                kill_group(proc)
                try:
                    proc.communicate(timeout=REAP_SECONDS)
                except subprocess.TimeoutExpired:
                    for stream in (proc.stdin, proc.stdout, proc.stderr):
                        if stream:
                            stream.close()
                    try:
                        proc.wait(timeout=REAP_SECONDS)
                    except subprocess.TimeoutExpired:
                        pass
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
                 schema_file=None, precheck_timeout=300, base_url='http://127.0.0.1:8080', ca_file=None, smoke_token_file=None,
                 smoke_timeout=60, request_timeout=20, dry_run=False, lock_timeout=None):
        self.compose_files, self.project, self.env_file = compose_files, project, env_file
        self.project_directory, self.profiles, self.build = project_directory, list(profiles), build
        self.build_network, self.wait_timeout, self.build_timeout = build_network, wait_timeout, build_timeout
        self.command_timeout, self.backup_timeout, self.backup_dir = command_timeout, backup_timeout, backup_dir
        self.no_backup, self.allow_remove_orphans = no_backup, allow_remove_orphans
        self.allow_incompatible_migration, self.schema_file = allow_incompatible_migration, schema_file or SCHEMA_FILE
        self.precheck_timeout = precheck_timeout
        self.base_url, self.ca_file, self.smoke_token_file = base_url, ca_file, smoke_token_file
        self.smoke_timeout, self.request_timeout, self.dry_run = smoke_timeout, request_timeout, dry_run
        self.lock_timeout = lock_timeout

    @property
    def lock_wait(self):
        """How long a second deploy may wait for the project lock before it aborts without changing anything."""
        if self.lock_timeout is not None:
            return self.lock_timeout
        return (self.build_timeout + self.backup_timeout + self.precheck_timeout + self.up_timeout
                + self.smoke_timeout + self.command_timeout)

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

    def run(self, argv, mutating=False, timeout=None, stdout_path=None, stdin_text=None):
        timeout = timeout or self.config.command_timeout
        entry = {'argv': list(argv), 'mutating': mutating, 'executed': True, 'timeout': timeout}
        self.commands.append(entry)
        if mutating and self.config.dry_run:
            entry['executed'] = False
            return Result(0)
        self.log('$ ' + ' '.join(argv))
        kwargs = {}
        if stdout_path:
            kwargs['stdout_path'] = stdout_path
        if stdin_text is not None:
            kwargs['stdin_text'] = stdin_text
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


class HttpClient:
    """Minimal HTTP caller: no proxies, no redirects, certificate verification on https.

    ``timeout`` bounds one blocking socket operation. ``set_deadline`` bounds the whole call, including a body
    that keeps arriving in small pieces: a timer closes the socket when the deadline is reached, so the read
    cannot run on past it.
    """

    def __init__(self, base_url, ca_file=None, timeout=20):
        self.base_url, self.ca_file, self.timeout = base_url, ca_file, timeout
        self.deadline, self.clock = None, time.monotonic
        parts = urlsplit(base_url)
        self._secure = parts.scheme == 'https'
        self._host, self._port = parts.hostname, parts.port or (443 if self._secure else 80)
        if self._secure:
            try:
                self._ssl = ssl.create_default_context(cafile=ca_file or None)
            except (OSError, ssl.SSLError):
                raise UsageError('cannot load --ca-file') from None

    def set_deadline(self, deadline, clock):
        """Absolute monotonic deadline for every call, or None to keep only the per-read timeout."""
        self.deadline, self.clock = deadline, clock

    def _budget(self):
        if self.deadline is None:
            return self.timeout
        return min(self.timeout, self.deadline - self.clock())

    def __call__(self, method, path, headers=None, body=None):
        budget = self._budget()
        if budget <= 0:
            raise SmokeError('timed out')
        connection = (http.client.HTTPSConnection(self._host, self._port, timeout=budget, context=self._ssl)
                      if self._secure else http.client.HTTPConnection(self._host, self._port, timeout=budget))
        fired = []

        def abort():
            # close() alone does not unblock a read that is still collecting a chunked body.
            fired.append(True)
            sock = connection.sock
            if sock is None:
                return
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

        timer = None
        try:
            connection.connect()
            if self.deadline is not None:
                delay = self.deadline - self.clock()
                if delay <= 0:
                    raise SmokeError('timed out')
                timer = threading.Timer(delay, abort)
                timer.daemon = True
                timer.start()
            connection.request(method, path, body=body, headers=headers or {})
            remote = connection.getresponse()
            chunks, size = [], 0
            while True:
                if self.deadline is not None and self.clock() >= self.deadline:
                    raise SmokeError('timed out')
                block = remote.read(65536)
                if not block:
                    break
                size += len(block)
                if size > MAX_SMOKE_BODY:
                    raise SmokeError('response too large')
                chunks.append(block)
            payload = b''.join(chunks)
            header_map = dict((key.lower(), value) for key, value in remote.getheaders())
            return Response(remote.status, header_map, payload)
        except SmokeError:
            raise
        except Exception as exc:
            if fired or (self.deadline is not None and self.clock() >= self.deadline):
                raise SmokeError('timed out') from None
            raise SmokeError('request failed: ' + type(exc).__name__) from None
        finally:
            if timer is not None:
                timer.cancel()
            connection.close()


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
    """Repeat the smoke run until it passes or the deadline passes. The last attempt is the verdict.

    The deadline is handed to the HTTP client as well. It is not only checked between rounds: a single
    response that trickles forever is cut off when the deadline is reached.
    """
    deadline = clock() + timeout
    set_deadline = getattr(http, 'set_deadline', None)
    # A zero window still runs one attempt (bounded by the per-request timeout). A positive window is absolute:
    # the client cuts the socket off when it is reached, including in the middle of a body.
    if set_deadline and timeout > 0:
        set_deadline(deadline, clock)
    try:
        while True:
            results = run_smoke_once(http, token)
            if all(item['ok'] for item in results) or clock() >= deadline:
                return {'ok': all(item['ok'] for item in results), 'checks': results}
            remaining = deadline - clock()
            sleep(interval if interval < remaining else remaining)
    finally:
        if set_deadline:
            set_deadline(None, clock)



def _dollar_delimiter(sql, index):
    """The ``$tag$`` starting at index, or None when this dollar sign is not a quote delimiter."""
    if index >= len(sql) or sql[index] != '$':
        return None
    cursor = index + 1
    if cursor < len(sql) and sql[cursor] == '$':
        return '$$'
    while cursor < len(sql) and (sql[cursor].isalnum() or sql[cursor] == '_'):
        cursor += 1
    if cursor > index + 1 and cursor < len(sql) and sql[cursor] == '$':
        return sql[index:cursor + 1]
    return None


def _skip_quoted(sql, index, escape):
    """Index just after a single-quoted string that starts at index. Unclosed quotes consume the rest."""
    index += 1
    length = len(sql)
    while index < length:
        if escape and sql[index] == '\\':
            index += 2
            continue
        if sql[index] == "'":
            if index + 1 < length and sql[index + 1] == "'":
                index += 2
                continue
            return index + 1
        index += 1
    return length


def psql_meta_command(sql):
    """True when psql would treat an unquoted backslash as a meta-command such as ``\\c`` or ``\\!``.

    Comments, quoted strings, identifiers and dollar quotes are skipped the way psql's lexer skips them.
    A backslash inside one of those is data. migrate.py sends SQL through psycopg, which never runs these
    commands; the precheck must not either.
    """
    index, length = 0, len(sql)
    while index < length:
        char = sql[index]
        if char == '-' and index + 1 < length and sql[index + 1] == '-':
            newline = sql.find('\n', index)
            index = length if newline < 0 else newline + 1
            continue
        if char == '/' and index + 1 < length and sql[index + 1] == '*':
            end = sql.find('*/', index + 2)
            index = length if end < 0 else end + 2
            continue
        if char == '$':
            delimiter = _dollar_delimiter(sql, index)
            if delimiter:
                end = sql.find(delimiter, index + len(delimiter))
                index = length if end < 0 else end + len(delimiter)
                continue
        if char == '"':
            index += 1
            while index < length:
                if sql[index] == '"':
                    if index + 1 < length and sql[index + 1] == '"':
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            continue
        if char in 'eEuU' and (index == 0 or not (sql[index - 1].isalnum() or sql[index - 1] == '_')):
            if char in 'eE' and index + 1 < length and sql[index + 1] == "'":
                index = _skip_quoted(sql, index + 1, escape=True)
                continue
            if char in 'uU' and index + 2 < length and sql[index + 1] == '&' and sql[index + 2] == "'":
                index = _skip_quoted(sql, index + 2, escape=False)
                continue
        if char == "'":
            index = _skip_quoted(sql, index, escape=False)
            continue
        if char == '\\':
            return True
        index += 1
    return False


_TXN_WORDS = frozenset(('commit', 'rollback', 'abort', 'savepoint', 'release'))
_PLPGSQL_END = frozenset(('if', 'loop', 'case', 'while'))


def _skip_ws_comments(sql, index):
    length = len(sql)
    while index < length:
        char = sql[index]
        if char.isspace():
            index += 1
            continue
        if char == '-' and index + 1 < length and sql[index + 1] == '-':
            newline = sql.find('\n', index)
            index = length if newline < 0 else newline + 1
            continue
        if char == '/' and index + 1 < length and sql[index + 1] == '*':
            end = sql.find('*/', index + 2)
            index = length if end < 0 else end + 2
            continue
        break
    return index


def _keyword_at(sql, index):
    if index >= len(sql) or not (sql[index].isalpha() or sql[index] == '_'):
        return None, index
    end = index + 1
    while end < len(sql) and (sql[end].isalnum() or sql[end] == '_'):
        end += 1
    return sql[index:end].lower(), end


def _skip_double_quoted(sql, index):
    index += 1
    length = len(sql)
    while index < length:
        if sql[index] == '"':
            if index + 1 < length and sql[index + 1] == '"':
                index += 2
                continue
            return index + 1
        index += 1
    return length


def _starts_string_literal(sql, index):
    """A SQL string body: plain quotes, ``E'...'`` / ``e'...'``, or ``U&'...'``."""
    if index >= len(sql):
        return False
    prev_ok = index == 0 or not (sql[index - 1].isalnum() or sql[index - 1] == '_')
    if sql[index] == "'":
        return True
    if prev_ok and sql[index] in 'eE' and index + 1 < len(sql) and sql[index + 1] == "'":
        return True
    if prev_ok and sql[index] in 'uU' and index + 2 < len(sql) and sql[index + 1] == '&' and sql[index + 2] == "'":
        return True
    return False


def _skip_string_literal(sql, index):
    if sql[index] in 'uU':
        return _skip_quoted(sql, index + 2, escape=False)
    if sql[index] in 'eE':
        return _skip_quoted(sql, index + 1, escape=True)
    return _skip_quoted(sql, index, escape=False)


def _skip_identifier(sql, index):
    index = _skip_ws_comments(sql, index)
    if index < len(sql) and sql[index] == '"':
        return _skip_double_quoted(sql, index)
    _, after = _keyword_at(sql, index)
    return after


def _do_block_at(sql, index):
    """True when ``DO`` is followed by a body literal, before that literal is blanked.

    ``DO E'...'`` and ``DO U&'...'`` survive here. ``ON CONFLICT DO NOTHING`` is not a statement-level ``DO``.
    """
    index = _skip_ws_comments(sql, index)
    word, after = _keyword_at(sql, index)
    if word == 'language':
        index = _skip_ws_comments(sql, _skip_identifier(sql, after))
    return _starts_string_literal(sql, index) or _dollar_delimiter(sql, index) is not None


def _skip_code_atom(sql, index):
    """Move past one comment, quote, dollar quote or character. Used inside ``BEGIN ATOMIC`` bodies."""
    length = len(sql)
    if index >= length:
        return length
    char = sql[index]
    if char == '-' and index + 1 < length and sql[index + 1] == '-':
        newline = sql.find('\n', index)
        return length if newline < 0 else newline + 1
    if char == '/' and index + 1 < length and sql[index + 1] == '*':
        end = sql.find('*/', index + 2)
        return length if end < 0 else end + 2
    if char == '$':
        delimiter = _dollar_delimiter(sql, index)
        if delimiter:
            end = sql.find(delimiter, index + len(delimiter))
            return length if end < 0 else end + len(delimiter)
    if char == '"':
        return _skip_double_quoted(sql, index)
    if _starts_string_literal(sql, index):
        return _skip_string_literal(sql, index)
    return index + 1


def _atomic_body_end(sql, index):
    """Index just after the ``END`` that closes a ``BEGIN ATOMIC`` body starting at index.

    The body is SQL, so internal semicolons are not statement boundaries for the migration. Nested
    ``BEGIN ATOMIC`` blocks are counted. ``END IF`` does not close the body.
    """
    depth = 1
    length = len(sql)
    while index < length and depth:
        index = _skip_ws_comments(sql, index)
        if index >= length:
            return length
        word, after = _keyword_at(sql, index)
        if word == 'begin':
            nxt, nxt_end = _keyword_at(sql, _skip_ws_comments(sql, after))
            if nxt == 'atomic':
                depth += 1
                index = nxt_end
                continue
        if word == 'end':
            nxt, _ = _keyword_at(sql, _skip_ws_comments(sql, after))
            if nxt not in _PLPGSQL_END:
                depth -= 1
                if depth == 0:
                    return after
                index = after
                continue
        index = _skip_code_atom(sql, index)
    return index


def _begin_atomic_at(sql, index):
    """If a ``BEGIN ATOMIC`` starts at index, return the index just after its closing ``END``. Otherwise None."""
    if index > 0 and (sql[index - 1].isalnum() or sql[index - 1] == '_'):
        return None
    word, after = _keyword_at(sql, index)
    if word != 'begin':
        return None
    nxt, nxt_end = _keyword_at(sql, _skip_ws_comments(sql, after))
    if nxt != 'atomic':
        return None
    return _atomic_body_end(sql, nxt_end)


def _string_literal_text(sql, index):
    """The decoded text of a string or dollar literal at index, and the index just after it."""
    if index >= len(sql):
        return None, index
    if sql[index] == '$':
        delimiter = _dollar_delimiter(sql, index)
        if delimiter is None:
            return None, index
        start = index + len(delimiter)
        end = sql.find(delimiter, start)
        if end < 0:
            return sql[start:], len(sql)
        return sql[start:end], end + len(delimiter)
    escape = False
    start = index
    if sql[index] in 'eE' and index + 1 < len(sql) and sql[index + 1] == "'":
        escape = True
        start = index + 1
    elif sql[index] in 'uU' and index + 2 < len(sql) and sql[index + 1] == '&' and sql[index + 2] == "'":
        start = index + 2
    elif sql[index] != "'":
        return None, index
    chars = []
    cursor, length = start + 1, len(sql)
    while cursor < length:
        if escape and sql[cursor] == '\\' and cursor + 1 < length:
            chars.append(sql[cursor + 1])
            cursor += 2
            continue
        if sql[cursor] == "'":
            if cursor + 1 < length and sql[cursor + 1] == "'":
                chars.append("'")
                cursor += 2
                continue
            return ''.join(chars), cursor + 1
        chars.append(sql[cursor])
        cursor += 1
    return ''.join(chars), length


def _set_config_switches_role(sql):
    """True when a call passes ``role`` or ``session_authorization`` to ``set_config``.

    The argument is a string literal, so this looks at the source before strings are blanked. Calls inside
    comments, ordinary strings or dollar quotes are not calls the migration runs while it is applied.
    """
    index, length = 0, len(sql)
    while index < length:
        index = _skip_ws_comments(sql, index)
        if index >= length:
            return False
        word, after = _keyword_at(sql, index)
        if word == 'set_config' and (index == 0 or not (sql[index - 1].isalnum() or sql[index - 1] == '_')):
            nxt = _skip_ws_comments(sql, after)
            if nxt < length and sql[nxt] == '(':
                text, _ = _string_literal_text(sql, _skip_ws_comments(sql, nxt + 1))
                if text is not None and text.lower() in ('role', 'session_authorization'):
                    return True
        index = _skip_code_atom(sql, index)
    return False


def _statement_hazards(sql):
    """Top-level transaction control and ``DO`` blocks.

    Dollar quotes, comments, string literals and ``BEGIN ATOMIC`` bodies are skipped, so ``BEGIN``/``END``
    inside a function or procedure body are not transaction control. A ``COMMIT``/``ROLLBACK``/``END``/``ABORT``
    at statement level ends ``SET LOCAL ROLE``; later statements then run as the superuser. Migrations must
    not manage transactions.
    """
    do_block = False
    manages = False
    index, length = 0, len(sql)
    at_start = True
    while index < length:
        if at_start:
            index = _skip_ws_comments(sql, index)
            if index >= length:
                break
            word, after = _keyword_at(sql, index)
            if word == 'do' and _do_block_at(sql, after):
                do_block = True
            elif word == 'begin':
                atomic_end = _begin_atomic_at(sql, index)
                if atomic_end is not None:
                    index = atomic_end
                    continue
                manages = True
            elif word == 'end':
                nxt, _ = _keyword_at(sql, _skip_ws_comments(sql, after))
                if nxt not in _PLPGSQL_END:
                    manages = True
            elif word == 'start':
                nxt, _ = _keyword_at(sql, _skip_ws_comments(sql, after))
                if nxt == 'transaction':
                    manages = True
            elif word == 'prepare':
                nxt, _ = _keyword_at(sql, _skip_ws_comments(sql, after))
                if nxt == 'transaction':
                    manages = True
            elif word in _TXN_WORDS:
                manages = True
            at_start = False
        atomic_end = _begin_atomic_at(sql, index)
        if atomic_end is not None:
            index = atomic_end
            continue
        char = sql[index]
        if char == ';':
            at_start = True
            index += 1
            continue
        if char == '-' and index + 1 < length and sql[index + 1] == '-':
            newline = sql.find('\n', index)
            index = length if newline < 0 else newline + 1
            continue
        if char == '/' and index + 1 < length and sql[index + 1] == '*':
            end = sql.find('*/', index + 2)
            index = length if end < 0 else end + 2
            continue
        if char == '$':
            delimiter = _dollar_delimiter(sql, index)
            if delimiter:
                end = sql.find(delimiter, index + len(delimiter))
                index = length if end < 0 else end + len(delimiter)
                continue
        if char == '"':
            index = _skip_double_quoted(sql, index)
            continue
        if _starts_string_literal(sql, index):
            index = _skip_string_literal(sql, index)
            continue
        index += 1
    found = []
    if do_block:
        found.append('contains a DO block')
    if manages:
        found.append('manages a transaction')
    return found


def unsafe_migration_statements(sql):
    """Early warning: textual descriptions of statements that a previous image could not survive.

    Comments and string literals are blanked in a single pass, so a comment marker inside one cannot hide a
    statement. A psql meta-command is reported from the raw script, because blanking strings must not hide
    ``\\c`` that sits outside them and must not invent one that sits inside them. Role switches, ``ALTER SYSTEM``,
    ``ALTER DATABASE``, role DDL, ``COPY ... PROGRAM``, ``CREATE EXTENSION``, ``DO`` blocks and transaction
    control are rejected here: the catalog snapshot does not record their server-side side effects, and
    ``deploy/migrate.py`` would replay the same file on the live database. ``DO`` is matched from its body
    literal (quotes, ``E'...'``, ``U&'...'``, dollar quotes) before strings are blanked. ``BEFORE UPDATE ON``
    and ``ON UPDATE`` foreign-key actions are not rewrites. This is not a proof of compatibility;
    precheck_migration() is the authoritative check for what the catalog can see.
    """
    text = FK_ACTION.sub(' ', NOISE.sub(' ', sql))
    found = [label for label, pattern in INCOMPATIBLE_SQL if re.search(pattern, text, re.IGNORECASE)]
    for clause in ADD_CLAUSE.findall(text):
        if re.search(r'\bNOT\s+NULL\b', clause, re.IGNORECASE) and not re.search(r'\bDEFAULT\b', clause, re.IGNORECASE):
            found.append('adds a NOT NULL column without a default')
            break
    if psql_meta_command(sql):
        found.append('contains a psql meta-command')
    if _set_config_switches_role(sql) and 'switches role' not in found:
        found.append('switches role')
    found.extend(_statement_hazards(sql))
    return found


def read_migration(config):
    """The SQL the image being deployed will apply; a missing or unreadable file is an error, never a pass."""
    try:
        sql = Path(config.schema_file).read_text(encoding='utf-8')
    except (OSError, ValueError):
        raise UsageError('migration SQL not found or unreadable: %s (pass --schema-file)' % config.schema_file) from None
    if not sql.strip():
        raise UsageError('migration SQL is empty: %s' % config.schema_file)
    return sql


def check_migration(config):
    sql = read_migration(config)
    return {'checked': True, 'file': str(config.schema_file), 'unsafe': unsafe_migration_statements(sql)}, sql


def schema_breaking_changes(before, after):
    """Differences between two catalog snapshots that a previous application image may not survive.

    Dropped or changed objects, narrowed columns, and constraints, unique indexes or triggers that tighten
    something that already existed are all flagged. Privilege, policy, and row-level-security changes on objects
    the previous image already used are flagged too: a rollback does not undo them. Only genuinely additive
    changes pass. A snapshot missing any section fails closed.
    """
    missing = [key for key in SNAPSHOT_KEYS
               if not isinstance(before.get(key), list) or not isinstance(after.get(key), list)]
    if missing:
        return ['catalog snapshot is missing %s' % ', '.join(missing)]
    changes = []

    def keyed(snapshot, section, key):
        return {key(item): item for item in snapshot[section]}

    relations_after = {relation.rsplit(':', 1)[0] for relation in after['relations']}
    old_tables = {item['t'] for item in before['columns']}
    old_columns = {(item['t'], item['c']) for item in before['columns']}
    for relation in sorted(set(before['relations']) - set(after['relations'])):
        changes.append('drops %s' % relation)
    old = keyed(before, 'columns', lambda i: (i['t'], i['c']))
    new = keyed(after, 'columns', lambda i: (i['t'], i['c']))
    for (table, column), item in sorted(old.items()):
        now = new.get((table, column))
        if now is None:
            if table in relations_after:
                changes.append('drops column %s.%s' % (table, column))
            continue
        if now['type'] != item['type']:
            changes.append('changes the type of %s.%s from %s to %s' % (table, column, item['type'], now['type']))
        if now['notnull'] and not item['notnull']:
            changes.append('makes %s.%s NOT NULL' % (table, column))
        if now['default'] != item['default']:
            changes.append('changes the default of %s.%s' % (table, column))
    for (table, column), item in sorted(new.items()):
        if (table, column) not in old and table in old_tables and item['notnull'] and not item['hasdef']:
            changes.append('adds NOT NULL column %s.%s without a default' % (table, column))

    def tightens(item, existing_tables):
        columns = item['cols']
        return item['t'] in existing_tables and (not columns or any((item['t'], c) in old_columns for c in columns))

    for section, label, key in (('constraints', 'constraint', lambda i: (i['t'], i['name'])),
                                ('indexes', 'index', lambda i: (i['t'], i['name'])),
                                ('triggers', 'trigger', lambda i: (i['t'], i['name']))):
        before_items, after_items = keyed(before, section, key), keyed(after, section, key)
        for name, item in sorted(before_items.items()):
            now = after_items.get(name)
            # Any change to an index that already exists is incompatible, including a non-unique index rebuilt
            # under the same name with different uniqueness, columns or predicate. Dropping a non-unique index
            # and not putting it back does not make old writes fail, so that one case stays compatible.
            if section == 'indexes':
                if now is None:
                    if item['unique']:
                        changes.append('drops index %s on %s' % (name[1], name[0]))
                    continue
                if now['unique'] and not item['unique']:
                    changes.append('replaces non-unique index %s on %s with a unique index' % (name[1], name[0]))
                elif (now['unique'] != item['unique'] or now.get('cols') != item.get('cols')
                      or now.get('pred') != item.get('pred') or now.get('def') != item.get('def')):
                    changes.append('changes index %s on %s' % (name[1], name[0]))
                continue
            # pg_get_triggerdef does not include tgenabled, so DISABLE TRIGGER used to compare equal.
            # 'O' is the normal origin firing mode the bridge health check requires.
            if section == 'triggers':
                if now is None:
                    changes.append('drops trigger %s on %s' % (name[1], name[0]))
                elif now['def'] != item['def']:
                    changes.append('changes trigger %s on %s' % (name[1], name[0]))
                elif now.get('enabled', 'O') != item.get('enabled', 'O'):
                    if now.get('enabled') == 'D':
                        changes.append('disables trigger %s on %s' % (name[1], name[0]))
                    else:
                        changes.append('changes whether trigger %s on %s fires' % (name[1], name[0]))
                continue
            if now is None:
                changes.append('drops %s %s on %s' % (label, name[1], name[0]))
            elif now['def'] != item['def']:
                changes.append('changes %s %s on %s' % (label, name[1], name[0]))
        for name, item in sorted(after_items.items()):
            if name in before_items or (section == 'indexes' and not item['unique']):
                continue
            if section == 'triggers':
                # A new foreign key adds RI_ConstraintTrigger rows on tables that already exist. Only the
                # enabled state of a trigger that was already there is breaking; the new internal row is not.
                if item['t'] in old_tables and not item.get('internal'):
                    changes.append('adds trigger %s to the existing table %s' % (name[1], name[0]))
            elif tightens(item, old_tables):
                changes.append('adds %s %s that constrains existing columns of %s' % (label, name[1], name[0]))
    old_functions, new_functions = keyed(before, 'functions', lambda i: i['sig']), keyed(after, 'functions', lambda i: i['sig'])
    for signature, item in sorted(old_functions.items()):
        if signature not in new_functions:
            changes.append('drops function %s' % signature)
        elif new_functions[signature]['def'] != item['def']:
            changes.append('changes function %s' % signature)
    old_types, new_types = keyed(before, 'types', lambda i: i['name']), keyed(after, 'types', lambda i: i['name'])
    for name, item in sorted(old_types.items()):
        if name not in new_types:
            changes.append('drops type %s' % name)
        elif new_types[name]['def'] != item['def']:
            changes.append('changes type %s' % name)
    changes.extend(privilege_and_rls_changes(before, after, keyed))
    changes.extend(role_changes(before, after))
    return changes


def role_changes(before, after):
    """Role and membership changes are cluster-wide and a database drop does not undo them.

    The precheck applies them only inside the throwaway cluster, then refuses the deploy when this diff is not
    empty. The message names the role and never includes a password or its hash.
    """
    changes = []
    old = {item['name']: item for item in before['roles']}
    new = {item['name']: item for item in after['roles']}
    for name, item in sorted(old.items()):
        now = new.get(name)
        if now is None:
            changes.append('drops role %s' % name)
        elif now != item:
            changes.append('changes role %s' % name)
    old_members = {(item['role'], item['member']): item for item in before['memberships']}
    new_members = {(item['role'], item['member']): item for item in after['memberships']}
    for key, item in sorted(old_members.items()):
        now = new_members.get(key)
        if now is None:
            changes.append('drops membership of %s in %s' % (key[1], key[0]))
        elif now != item:
            changes.append('changes membership of %s in %s' % (key[1], key[0]))
    for key in sorted(set(new_members) - set(old_members)):
        if key[0] in old or key[1] in old:
            changes.append('adds membership of %s in %s' % (key[1], key[0]))
    return changes


def privilege_and_rls_changes(before, after, keyed):
    """Privilege, policy, and row-level-security differences a rollback cannot undo.

    Changes on relations that already existed are breaking. A new table's default ACL is not: the previous image
    never referenced it.
    """
    changes = []
    old_relations = {relation.rsplit(':', 1)[0] for relation in before['relations']}
    old_columns = {(item['t'], item['c']) for item in before['columns']}
    relations_after = {relation.rsplit(':', 1)[0] for relation in after['relations']}

    old_priv, new_priv = keyed(before, 'privileges', lambda i: i['t']), keyed(after, 'privileges', lambda i: i['t'])
    for name, item in sorted(old_priv.items()):
        now = new_priv.get(name)
        if name in old_relations and now is not None and now['acl'] != item['acl']:
            changes.append('changes privileges on %s' % name)
    for name, item in sorted(new_priv.items()):
        if name in old_relations and name not in old_priv and item['acl']:
            changes.append('changes privileges on %s' % name)

    old_cols, new_cols = (keyed(before, 'column_privileges', lambda i: (i['t'], i['c'])),
                          keyed(after, 'column_privileges', lambda i: (i['t'], i['c'])))
    for key, item in sorted(old_cols.items()):
        now = new_cols.get(key)
        if now is None:
            if key[0] in relations_after:
                changes.append('changes privileges on %s.%s' % key)
        elif now['acl'] != item['acl']:
            changes.append('changes privileges on %s.%s' % key)
    for key in sorted(set(new_cols) - set(old_cols)):
        if key in old_columns:
            changes.append('changes privileges on %s.%s' % key)

    old_schema, new_schema = (keyed(before, 'schema_privileges', lambda i: i['n']),
                              keyed(after, 'schema_privileges', lambda i: i['n']))
    for name, item in sorted(old_schema.items()):
        now = new_schema.get(name)
        if now is not None and now['acl'] != item['acl']:
            changes.append('changes privileges on schema %s' % name)

    old_functions = {item['sig'] for item in before['functions']}
    old_fn, new_fn = (keyed(before, 'function_privileges', lambda i: i['sig']),
                      keyed(after, 'function_privileges', lambda i: i['sig']))
    for signature, item in sorted(old_fn.items()):
        now = new_fn.get(signature)
        if now is not None and now['acl'] != item['acl']:
            changes.append('changes privileges on function %s' % signature)
    for signature, item in sorted(new_fn.items()):
        if signature in old_functions and signature not in old_fn and item['acl']:
            changes.append('changes privileges on function %s' % signature)

    if sorted(item.get('acl', '') for item in before['database_privileges']) != \
            sorted(item.get('acl', '') for item in after['database_privileges']):
        changes.append('changes database privileges')

    old_rs, new_rs = keyed(before, 'row_security', lambda i: i['t']), keyed(after, 'row_security', lambda i: i['t'])
    for name, item in sorted(old_rs.items()):
        now = new_rs.get(name)
        if now is not None and (now['enabled'] != item['enabled'] or now['forced'] != item['forced']):
            changes.append('changes row level security on %s' % name)
    for name, item in sorted(new_rs.items()):
        if name in old_relations and name not in old_rs and (item['enabled'] or item['forced']):
            changes.append('changes row level security on %s' % name)

    def policy_signature(item):
        return (item.get('permissive'), item.get('cmd'), item.get('roles'), item.get('qual'), item.get('with_check'))

    old_pol, new_pol = (keyed(before, 'policies', lambda i: (i['t'], i['name'])),
                        keyed(after, 'policies', lambda i: (i['t'], i['name'])))
    for key, item in sorted(old_pol.items()):
        now = new_pol.get(key)
        if now is None:
            changes.append('drops policy %s on %s' % (key[1], key[0]))
        elif policy_signature(now) != policy_signature(item):
            changes.append('changes policy %s on %s' % (key[1], key[0]))
    for key in sorted(set(new_pol) - set(old_pol)):
        if key[0] in old_relations:
            changes.append('adds policy %s to the existing table %s' % (key[1], key[0]))
    return changes


def parse_snapshot(text):
    try:
        snapshot = json.loads(text)
        if not isinstance(snapshot, dict) or any(not isinstance(snapshot.get(key), list) for key in SNAPSHOT_KEYS):
            raise ValueError
        return snapshot
    except ValueError:
        raise UsageError('unreadable catalog snapshot') from None


def database_exec(config, script):
    return config.compose_base() + ['exec', '-T', DATABASE_SERVICE, 'sh', '-c', script]


def migration_script(sql):
    """The migration as ``deploy/migrate.py`` runs it: as ``baton_owner``, then back to the session user.

    ``\\restrict`` is sent first so psql, which ``migrate.py`` does not use, cannot run a meta-command even
    inside the throwaway container. The random key is not in the migration, so ``\\unrestrict`` cannot match it.
    """
    key = secrets.token_hex(16)
    return 'BEGIN;\nSET LOCAL ROLE baton_owner;\n\\restrict %s\n%s\nRESET ROLE;\nCOMMIT;\n' % (key, sql)


def precheck_migration(docker, config, sql, previous, project, clock=time.monotonic, sleep=time.sleep):
    """Apply the pending migration in a separate Postgres container and diff the catalogs.

    The container uses the database service's image, ``--network none``, a private superuser and a tmpfs data
    directory. It is loaded from a schema-only dump plus ``pg_dumpall --roles-only --no-role-passwords`` of the
    live database. Those two dumps are read-only. Before the backup, the migration SQL runs only inside this
    container: it is not executed on the live database. The container is removed before the backup. The text
    rules already reject role switches, ``ALTER SYSTEM``, ``ALTER DATABASE``, role DDL, ``COPY ... PROGRAM``,
    ``CREATE EXTENSION`` and ``DO`` blocks, because this catalog does not record those server-side side effects
    and the same file is later applied to the live database. Role, privilege and trigger-enabled changes
    (including internal triggers such as foreign keys) that do reach this diff are breaking. Fails closed.
    """
    name = PRECHECK_PREFIX + secrets.token_hex(6)
    report = {'database': name, 'network': 'none', 'ok': False, 'changes': [], 'error': None}
    timeout = config.precheck_timeout
    started = False

    def step(label, argv, stdin_text=None):
        result = docker.run(argv, mutating=True, timeout=timeout, stdin_text=stdin_text)
        if config.dry_run:
            return ''
        if result.returncode != 0:
            raise UsageError('%s %s' % (label, 'timed out' if result.timed_out else 'failed: ' + tail(result.stderr, 300)))
        return result.stdout

    def psql(database, statement, rows=False):
        command = ['docker', 'exec', '-i', name, 'psql', '-X', '-q', '-v', 'ON_ERROR_STOP=1', '-U', 'precheck']
        if rows:
            command.append('-At')
        command += ['-d', database]
        return command, statement

    try:
        info = previous.get(DATABASE_SERVICE)
        if not info:
            info = project_snapshot(docker, project).get(DATABASE_SERVICE)
        image = (info or {}).get('image')
        if not image:
            raise UsageError('cannot find the database service image')
        report['image'] = image
        roles = step('dumping roles', database_exec(config, ROLES_DUMP))
        schema = step('dumping the live schema', database_exec(config, SCHEMA_DUMP))
        step('starting the throwaway database', [
            'docker', 'run', '-d', '--rm', '--name', name, '--network', 'none',
            '--tmpfs', '/var/lib/postgresql/data:uid=999,gid=999,mode=0700',
            '-e', 'POSTGRES_HOST_AUTH_METHOD=trust', '-e', 'POSTGRES_USER=precheck', '-e', 'POSTGRES_DB=baton',
            image])
        started = True
        if not config.dry_run:
            deadline = clock() + timeout
            ready = False
            while clock() < deadline:
                probe = docker.run(['docker', 'exec', '-i', name, 'pg_isready', '-U', 'precheck', '-d', 'baton'],
                                   mutating=True, timeout=timeout)
                if probe.returncode == 0:
                    ready = True
                    break
                sleep(0.5)
            if not ready:
                raise UsageError('starting the throwaway database timed out')
        step('loading roles', *psql('postgres', roles))
        step('loading the schema', *psql('baton', schema))
        before = step('reading the schema before the migration', *psql('baton', CATALOG_SQL, rows=True))
        step('applying the migration', *psql('baton', migration_script(sql)))
        after = step('reading the schema after the migration', *psql('baton', CATALOG_SQL, rows=True))
        if config.dry_run:
            report.update(ok=True, planned=True)
        else:
            report['changes'] = schema_breaking_changes(parse_snapshot(before), parse_snapshot(after))
            report['ok'] = not report['changes']
    except UsageError as exc:
        report['error'] = str(exc)
    finally:
        if started:
            removed = docker.run(['docker', 'rm', '-f', name], mutating=True, timeout=timeout)
            if removed.returncode != 0 and not config.dry_run:
                report['cleanup_error'] = 'could not drop %s: %s' % (name, tail(removed.stderr, 300))
    return report


def ensure_database(docker, config, project, previous):
    """Make sure the database can be reached even when every app container is stopped.

    Returns a report with state running, started, or none (no database volume: a first deployment).
    """
    database = previous.get(DATABASE_SERVICE)
    if database and database['running']:
        return {'state': 'running', 'ok': True}
    listed = docker.run(['docker', 'volume', 'ls', '-q', '--filter', 'label=%s=%s' % (PROJECT_LABEL, project),
                         '--filter', 'label=com.docker.compose.volume=%s' % DATA_VOLUME])
    if listed.returncode != 0:
        return {'state': 'unknown', 'ok': False, 'error': 'docker volume ls failed: ' + tail(listed.stderr, 300)}
    if not listed.stdout.strip():
        return {'state': 'none', 'ok': True, 'reason': 'no database volume: first deployment'}
    started = docker.run(config.compose_base() + ['up', '-d', '--no-deps', '--no-recreate', '--wait', '--wait-timeout',
                                                  str(config.wait_timeout), DATABASE_SERVICE],
                         mutating=True, timeout=config.up_timeout)
    if started.returncode != 0 and not config.dry_run:
        return {'state': 'stopped', 'ok': False, 'error': 'the database volume exists but the database could not be '
                'started: ' + ('timed out' if started.timed_out else tail(started.stderr, 300))}
    return {'state': 'started', 'ok': True}


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


def hold_tag(image_id, service):
    return '%s:%s-%s' % (HOLD_REPOSITORY, re.sub(r'[^A-Za-z0-9_.-]', '_', service), image_id.split(':')[-1][:12])


def hold_previous_images(docker, previous):
    """Give the previous local image of every service a tag of its own.

    Recreating containers leaves the old image untagged, and the Docker image store may delete an unreferenced
    image at once, which would make the rollback impossible. Services that share one image reference may still
    have run different images (for example a crashed bridge next to freshly started one-shot services), so the
    protection is per service, not per reference. Digest-pinned references stay referenced by their digest.
    """
    holds = []
    for service, info in sorted(previous.items()):
        if '@' in info['image']:
            continue
        tag = hold_tag(info['image_id'], service)
        result = docker.run(['docker', 'tag', info['image_id'], tag], mutating=True)
        holds.append({'service': service, 'image': info['image'], 'image_id': info['image_id'], 'tag': tag,
                      'ok': result.returncode == 0})
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


def no_follow_flag():
    flag = getattr(os, 'O_NOFOLLOW', None)
    if flag is None:
        raise UsageError('this platform cannot refuse symbolic links')
    return flag


def open_private_file(path):
    """Create a new regular file and force mode 0600. Never follows or truncates an existing path.

    O_EXCL refuses a file that is already there. O_NOFOLLOW refuses a symbolic link. The mode argument of
    open is masked by umask, so fchmod sets 0600 on the fd afterwards. An existing looser file is left unchanged.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | no_follow_flag(), 0o600)
    try:
        os.fchmod(fd, 0o600)
        if stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:
            raise OSError('backup mode could not be set to 0600')
    except OSError:
        os.close(fd)
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return fd


def path_has_symlink(path):
    """True when path or an existing ancestor is a symbolic link."""
    path = Path(path)
    if path.is_absolute():
        current, parts = Path(path.anchor), path.parts[1:]
    else:
        current, parts = Path('.'), path.parts
    if current.is_symlink():
        return True
    for part in parts:
        current = current / part
        if current.is_symlink():
            return True
        if not current.exists():
            return False
    return False


def private_directory(path, label):
    """Create a real directory and force mode 0700. A symbolic link is refused, never followed."""
    path = Path(path)
    if path_has_symlink(path):
        return '%s is a symbolic link' % label
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError:
        return 'cannot create %s' % label
    if path.is_symlink() or not path.is_dir():
        return '%s is a symbolic link' % label
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | no_follow_flag())
    except OSError:
        return '%s is a symbolic link or cannot be opened' % label
    try:
        try:
            os.fchmod(fd, 0o700)
        except OSError:
            return '%s mode could not be set to 0700' % label
        mode = stat.S_IMODE(os.fstat(fd).st_mode)
    finally:
        os.close(fd)
    if mode != 0o700:
        return '%s mode could not be set to 0700' % label
    return None


def private_output_refused(result):
    return (result.stderr or '').startswith(PRIVATE_OUTPUT_ERROR)


def seal_private_file(path):
    """Force mode 0600 on a regular file and return (size, header). An error string means do not trust the path.

    The open uses O_NOFOLLOW, so a symbolic link is not chmod'd and its target is not read.
    """
    try:
        fd = os.open(path, os.O_RDONLY | no_follow_flag())
    except OSError as exc:
        return exc.strerror or 'cannot open backup'
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return 'backup is not a regular file'
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            return 'backup mode could not be set to 0600'
        if stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:
            return 'backup mode could not be set to 0600'
        return info.st_size, os.read(fd, len(DUMP_MAGIC))
    finally:
        os.close(fd)


def discard_regular(path):
    """Remove a regular file this deploy created. A symbolic link is left in place."""
    path = Path(path)
    if path.is_symlink():
        return
    try:
        if path.is_file():
            path.unlink()
    except OSError:
        pass


def backup_dump_path(backup_dir, project):
    """Dump path whose second-resolution stamp is not enough to predict the name."""
    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    safe = re.sub(r'[^A-Za-z0-9_.-]', '_', project) or 'project'
    return Path(backup_dir) / ('baton-%s-%s-%s.dump' % (safe, stamp, secrets.token_hex(8)))


def backup_database(docker, config, project, database):
    """pg_dump the database straight from its container before the migration can run. Returns (report, ok).

    `database` is the ensure_database() report: the dump happens whenever a database volume exists, including
    when every application container is stopped or removed. The directory is mode 0700 and is not a symlink.
    Each attempt uses a new unpredictable name and O_EXCL|O_NOFOLLOW; an existing file or link is not overwritten,
    not chmod'd, and not removed.
    """
    if config.no_backup:
        return {'skipped': True, 'reason': 'disabled by --no-backup'}, True
    if database['state'] == 'none':
        return {'skipped': True, 'reason': database['reason']}, True
    if not database['ok']:
        return {'ok': False, 'error': database['error']}, False
    argv = config.compose_base() + ['exec', '-T', DATABASE_SERVICE, 'sh', '-c', DUMP_SCRIPT]
    if config.dry_run:
        path = backup_dump_path(config.backup_dir, project)
        docker.run(argv, mutating=True, timeout=config.backup_timeout, stdout_path=str(path))
        return {'planned': True, 'path': str(path)}, True
    error = private_directory(config.backup_dir, 'backup directory')
    if error:
        return {'ok': False, 'error': error}, False
    result, path, refused = None, None, None
    for _attempt in range(BACKUP_CREATE_ATTEMPTS):
        path = backup_dump_path(config.backup_dir, project)
        result = docker.run(argv, mutating=True, timeout=config.backup_timeout, stdout_path=str(path))
        if not private_output_refused(result):
            refused = None
            break
        refused = result
    report = {'path': str(path), 'ok': False}
    if refused is not None:
        report['error'] = 'refusing to follow or overwrite an existing backup path'
        return report, False
    sealed = seal_private_file(path)
    if isinstance(sealed, str) or result.returncode != 0 or sealed[0] == 0 or sealed[1] != DUMP_MAGIC:
        if result.returncode != 0:
            report['error'] = 'pg_dump failed' + (' (timed out)' if result.timed_out else ': ' + tail(result.stderr, 300))
        elif isinstance(sealed, str):
            report['error'] = sealed
        else:
            report['error'] = 'pg_dump output is empty or not a custom-format dump'
        discard_regular(path)
        return report, False
    report.update(ok=True, bytes=sealed[0])
    return report, True


def lock_file(project):
    safe = re.sub(r'[^A-Za-z0-9_.-]', '_', project) or 'project'
    return Path(LOCK_DIR) / ('%s.lock' % safe)


class DeployLock:
    """Exclusive flock around one project's deploy, including rollback retagging.

    The wait is bounded. The lock file itself is created with O_NOFOLLOW and mode 0600, in a 0700 directory.
    """

    def __init__(self, path, timeout, clock, sleep):
        self.path, self.timeout, self.clock, self.sleep = Path(path), timeout, clock, sleep
        self.fd = None

    def acquire(self):
        error = private_directory(self.path.parent, 'deploy lock directory')
        if error:
            raise UsageError(error)
        try:
            self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT | no_follow_flag(), 0o600)
            os.fchmod(self.fd, 0o600)
        except OSError as exc:
            self.close()
            raise UsageError('cannot open the deploy lock: %s' % (exc.strerror or exc.errno)) from None
        deadline = self.clock() + self.timeout
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if self.clock() >= deadline:
                    self.close()
                    raise UsageError('another deploy holds the lock for this project; timed out after %ss' % self.timeout)
                remaining = deadline - self.clock()
                self.sleep(remaining if remaining < 0.05 else 0.05)

    def close(self):
        fd, self.fd = self.fd, None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


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


def recovery_up_command(config):
    """Last-resort `up`: no forced recreate, but still waits for health, with the same bounded wait."""
    return config.compose_base() + ['up', '-d', '--wait', '--wait-timeout', str(config.wait_timeout), '--remove-orphans']


def current_image_ids(docker, refs):
    ids = {}
    for ref in refs:
        result = docker.run(['docker', 'image', 'inspect', '--format', '{{.Id}}', ref])
        if result.returncode == 0 and result.stdout.strip():
            ids[ref] = result.stdout.strip()
    return ids


def source_root():
    """Checkout whose tracked files the image build and the bind mounts ship. Tests replace this."""
    return Path(__file__).resolve().parents[1]


def _clean_ignore_pattern(line):
    """One .dockerignore line, as Docker's patternmatcher reads it: strip, drop comments, honor `!`, drop a leading slash."""
    line = line.strip().lstrip('\ufeff')
    if not line or line.startswith('#'):
        return None
    negate = line.startswith('!')
    body = line[1:].strip() if negate else line
    body = body.lstrip('/')
    parts = []
    for part in body.split('/'):
        if part in ('', '.'):
            continue
        if part == '..':
            if parts:
                parts.pop()
            continue
        parts.append(part)
    cleaned = '/'.join(parts)
    if not cleaned or cleaned == '.':
        return None
    return negate, cleaned


def _dockerignore_regex(pattern):
    """Compile one pattern the way moby/patternmatcher does: `**` crosses directories, `*` and `?` do not."""
    parts = ['^']
    index, length = 0, len(pattern)
    while index < length:
        char = pattern[index]
        if char == '*':
            if index + 1 < length and pattern[index + 1] == '*':
                index += 2
                if index < length and pattern[index] == '/':
                    index += 1
                parts.append('.*' if index >= length else '(?:.*/)?')
            else:
                parts.append('[^/]*')
                index += 1
        elif char == '?':
            parts.append('[^/]')
            index += 1
        elif char == '\\':
            index += 1
            parts.append(re.escape(pattern[index]) if index < length else re.escape('\\'))
            if index < length:
                index += 1
        elif char == '[':
            end = pattern.find(']', index + 1)
            if end == -1:
                parts.append(re.escape(char))
                index += 1
            else:
                parts.append(pattern[index:end + 1])
                index = end + 1
        else:
            parts.append(re.escape(char))
            index += 1
    parts.append('$')
    try:
        return re.compile(''.join(parts))
    except re.error:
        raise UsageError('cannot interpret .dockerignore pattern %r; refusing to build' % pattern) from None


class DockerIgnore:
    """Whether a context-relative path is excluded, including because a parent directory is.

    The last matching pattern wins, so a later `!` can put a file back. This follows
    MatchesOrParentMatches: a file under an excluded directory is excluded too.
    """

    def __init__(self, text):
        self.patterns = []
        for raw in text.splitlines():
            cleaned = _clean_ignore_pattern(raw)
            if cleaned:
                negate, pattern = cleaned
                self.patterns.append((negate, _dockerignore_regex(pattern)))

    def ignored(self, relative):
        relative = relative.strip('/')
        if not relative or relative == '.':
            return False
        parts = relative.split('/')
        parents = ['/'.join(parts[:i]) for i in range(1, len(parts))]
        matched = False
        for negate, regex in self.patterns:
            if negate != matched:
                continue
            if regex.fullmatch(relative) or any(regex.fullmatch(parent) for parent in parents):
                matched = not negate
        return matched


def dockerignore_for(root):
    """The ignore file Docker would use for `deploy/Dockerfile` with the repository as the context.

    A `deploy/Dockerfile.dockerignore` next to the Dockerfile replaces `.dockerignore` entirely.
    """
    root = Path(root)
    specific = root / 'deploy' / 'Dockerfile.dockerignore'
    path = specific if specific.is_file() else root / '.dockerignore'
    if not path.exists():
        return DockerIgnore('')
    try:
        return DockerIgnore(path.read_text(encoding='utf-8'))
    except OSError:
        raise UsageError('cannot read %s; refusing to build' % path) from None


def tracked_paths(root):
    """Paths git will ship from this checkout. Untracked files are not part of the check."""
    try:
        result = subprocess.run(['git', '-C', str(root), '-c', 'safe.directory=*', 'ls-files', '-z'],
                                capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        raise UsageError('cannot list tracked files; refusing to build') from None
    if result.returncode != 0:
        raise UsageError('cannot list tracked files; refusing to build') from None
    return [item.decode('utf-8', 'surrogateescape') for item in result.stdout.split(b'\0') if item]


def _mode(path):
    """(mode bits, is_directory) of path, following symlinks. None when the path cannot be stat'ed."""
    try:
        info = path.stat()
    except OSError:
        return None
    return stat.S_IMODE(info.st_mode), stat.S_ISDIR(info.st_mode)


def _others_can_read(mode, directory):
    needed = 0o005 if directory else 0o004
    return mode & needed == needed


def unreadable_build_paths(root):
    """Tracked paths the build context would include that other users cannot read.

    Files need o+r. Directories on the way to such a file need o+rx, because COPY and the bind mounts
    keep that mode and migrate (uid 999) and edge (uid 101) are not the owner. A missing worktree file
    is not copied, so it is skipped. A path that exists but cannot be stat'ed is refused.
    """
    root = Path(root)
    matcher = dockerignore_for(root)
    offending, seen_dirs = [], set()
    for relative in tracked_paths(root):
        posix = Path(relative).as_posix()
        if matcher.ignored(posix):
            continue
        location = root / relative
        if not location.exists() and not location.is_symlink():
            continue
        found = _mode(location)
        if found is None or not _others_can_read(found[0], found[1]):
            offending.append(posix)
        parent = Path(posix).parent
        while parent != Path('.'):
            key = parent.as_posix()
            parent = parent.parent
            if key in seen_dirs or matcher.ignored(key):
                continue
            seen_dirs.add(key)
            found = _mode(root / key)
            if found is None or not found[1] or not _others_can_read(found[0], True):
                offending.append(key)
    return sorted(set(offending))


def build_permission_error(paths):
    listing = '\n'.join('  ' + path for path in paths)
    return ('refusing to build: these tracked paths are not readable by other users '
            '(files need o+r, directories o+rx). COPY and the bind mounts keep that mode, so migrate '
            '(uid 999) cannot read them:\n%s\n'
            'chmod a+rX each path. Files excluded by .dockerignore are not copied and may stay private, '
            'including deploy/.env, token files, keys and secrets/.' % listing)


def manual_recovery(previous, backup, holds):
    steps = ['docker tag %s %s   # %s' % (hold['tag'], hold['image'], hold['service']) for hold in holds if hold['ok']]
    steps.append('docker compose <your -f and -p options> up -d --wait --force-recreate')
    if backup and backup.get('path') and backup.get('ok'):
        steps.append('restore the database only if the schema is incompatible: pg_restore --clean --if-exists from ' + backup['path'])
    return steps


def roll_back(docker, config, project, previous, orphans, http, token, backup, holds, log, clock, sleep):
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
        recovery = docker.run(recovery_up_command(config), mutating=True, timeout=config.up_timeout)
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
        report['manual_recovery'] = manual_recovery(previous, backup, holds)
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

    migration, migration_sql = check_migration(config)
    report['migration'] = migration
    if migration['unsafe'] and not config.allow_incompatible_migration:
        return stop('aborted', 'the migration SQL is not backward compatible (%s); nothing was changed; a rollback '
                    'could not restore the previous image. Split it into an expand release and a later contract '
                    'release, or pass --allow-incompatible-migration' % '; '.join(migration['unsafe']))

    if config.build:
        offending = unreadable_build_paths(source_root())
        if offending:
            raise UsageError(build_permission_error(offending))

    model = compose_model(docker, config)
    project = report['project'] = model['project']
    lock = DeployLock(lock_file(project), config.lock_wait, clock, sleep)
    acquired = False
    try:
        try:
            lock.acquire()
            acquired = True
        except UsageError as exc:
            return stop('aborted', str(exc))
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

        database = ensure_database(docker, config, project, previous)
        report['database'] = database
        if not database['ok'] and not (config.no_backup and config.allow_incompatible_migration):
            return stop('aborted', 'the database cannot be reached, so it can be neither checked nor backed up (%s); '
                        'nothing else was changed' % database['error'])
        if not database['ok'] or database['state'] == 'none':
            report['precheck'] = {'skipped': True, 'reason': database.get('reason') or 'database unavailable'}
        else:
            log('prechecking the migration in an isolated database container')
            precheck = report['precheck'] = precheck_migration(
                docker, config, migration_sql, previous, project, clock, sleep)
            if not precheck['ok'] and not config.allow_incompatible_migration:
                detail = ('the precheck could not be completed (%s); treated as incompatible' % precheck['error']
                          if precheck['error'] else 'it would break the previous image: ' + '; '.join(precheck['changes']))
                return stop('aborted', 'the migration is not backward compatible: %s; nothing was changed. Split it into an '
                            'expand release and a later contract release, or pass --allow-incompatible-migration' % detail)

        backup, backup_ok = backup_database(docker, config, project, database)
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
            rollback = roll_back(docker, config, project, previous, orphans, http, token, backup, holds, log, clock, sleep)
            report['rollback'] = rollback
            report.update(status='rolled_back' if rollback['ok'] else 'rollback_failed', ok=False)
        if report['status'] != 'rollback_failed':
            release_holds(docker, holds)
        report['commands'] = docker.commands
        return report
    finally:
        if acquired:
            lock.close()


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
                        help='directory for the pre-deploy pg_dump (mode 0700, files 0600, unpredictable names; '
                             'an existing file or symbolic link is refused)')
    parser.add_argument('--lock-timeout', type=positive, default=None, metavar='SECONDS',
                        help='how long to wait for another deploy of this project (default: the sum of the '
                             'build, backup, precheck, up, smoke and command budgets)')
    parser.add_argument('--no-backup', action='store_true', help='deploy without a database backup (recorded in the report)')
    parser.add_argument('--backup-timeout', type=positive, default=900, metavar='SECONDS', help='hard timeout for pg_dump')
    parser.add_argument('--allow-remove-orphans', action='store_true',
                        help='acknowledge that containers of services the Compose files no longer declare are deleted')
    parser.add_argument('--allow-incompatible-migration', action='store_true',
                        help='deploy even though the migration precheck found changes the previous image cannot survive '
                             '(or could not run)')
    parser.add_argument('--schema-file', metavar='FILE',
                        help='the migration SQL the image being built applies (default: bridge/space_schema.sql of this checkout)')
    parser.add_argument('--precheck-timeout', type=positive, default=300, metavar='SECONDS',
                        help='hard timeout for each step of the throwaway-database migration precheck')
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
                        args.allow_incompatible_migration, args.schema_file, args.precheck_timeout, parse_base_url(args.base_url), args.ca_file,
                        args.smoke_token_file, args.smoke_timeout, args.request_timeout, args.dry_run, args.lock_timeout)
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
