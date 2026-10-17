#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Zero-dependency Baton command-line client.

Single file, Python 3.9+ standard library only, for agents that cannot install
the MCP SDK. It speaks Baton's HTTP API (POST /v1/space/{operation}), never MCP.

Exit codes: 0 ok, 1 Baton error, 2 usage, 3 network/TLS.
"""

import argparse
import base64
import hashlib
import hmac
import http.client
import json
import os
import re
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

PROTOCOL = "baton.space.v1"
EXIT_OK, EXIT_BATON, EXIT_USAGE, EXIT_NETWORK = 0, 1, 2, 3
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1"})
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
DEFAULT_REQUEST_TIMEOUT = 30
DEFAULT_WAIT_SECONDS = 25
SCOPE_KINDS = ("path", "module", "migration")
AGENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}")
SUMMARY_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
SUMMARY_STATE_RE = re.compile(r"[a-z][a-z_]{0,31}")
ERROR_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
BLOCK_REASONS = ("scope_conflict", "lease_held", "dependency_blocked", "waiting_input", "other")
DETAIL_FIELD_RE = re.compile(r"[A-Za-z0-9_.\[\]<>?-]{1,100}")
DETAIL_EXPECTED_RE = re.compile(r"[\x20-\x7e]{1,200}")
MAX_DETAILS = 20
NOTIFICATION_EVENT_RE = re.compile(r"[a-z]+\.[a-z_]+")
BACKOFF_START, BACKOFF_CAP = 1, 60
EXEC_TIMEOUT = 300


class UsageError(Exception):
    """Bad invocation or local configuration (exit 2)."""


class BatonError(Exception):
    """Baton or an intermediary answered with an error (exit 1)."""

    def __init__(self, code, status, details=()):
        super().__init__(code)
        self.code, self.status, self.details = code, status, tuple(details)


class NetworkError(Exception):
    """The request did not complete; only a fixed category is ever shown (exit 3)."""

    def __init__(self, category):
        super().__init__(category)
        self.category = category


class Endpoint:
    def __init__(self, scheme, host, port, prefix):
        self.scheme, self.host, self.port, self.prefix = scheme, host, port, prefix


def parse_endpoint(url):
    """Validate the base URL. Messages never echo the URL (it may carry userinfo)."""
    if not url:
        raise UsageError("missing Baton URL: pass --url or set BATON_URL")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise UsageError("invalid Baton URL") from None
    scheme, host = parts.scheme.lower(), parts.hostname
    if (scheme not in ("http", "https") or not host or parts.username is not None
            or parts.password is not None or parts.query or parts.fragment):
        raise UsageError("invalid Baton URL: expected https://host[:port]")
    if scheme == "http" and host not in LOCAL_HOSTS:
        raise UsageError("refusing plain http:// for a non-local host; use https://")
    prefix = parts.path.rstrip("/")
    if prefix.endswith("/mcp"):
        # BATON_URL is shared with the MCP adapter, whose endpoint ends in /mcp.
        prefix = prefix[:-len("/mcp")]
    return Endpoint(scheme, host, port or (443 if scheme == "https" else 80), prefix)


def read_token(path):
    try:
        raw = Path(path).read_text(encoding="ascii")
    except OSError:
        raise UsageError("cannot read --token-file") from None
    except ValueError:
        raise UsageError("invalid token file: expected a single printable token") from None
    token = raw.strip()
    if not re.fullmatch(r"[\x21-\x7e]+", token):
        raise UsageError("invalid token file: expected a single printable token")
    return token


def tls_context(ca_file):
    try:
        context = ssl.create_default_context(cafile=ca_file or None)
    except (OSError, ssl.SSLError):
        raise UsageError("cannot load --ca-file") from None
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


def parse_pin(value):
    text = value.strip()
    if text.startswith("sha256//"):
        text = text[len("sha256//"):]
    try:
        pin = base64.b64decode(text, validate=True)
    except ValueError:
        pin = b""
    if len(pin) != hashlib.sha256().digest_size:
        raise UsageError("invalid --pin-sha256: expected base64 of a SHA-256 digest")
    return pin


def _der_read(buf, pos):
    """Return (tag, content_start, end) for the DER TLV at pos."""
    if pos + 2 > len(buf):
        raise ValueError("truncated DER")
    tag, length, pos = buf[pos], buf[pos + 1], pos + 2
    if length & 0x80:
        count = length & 0x7F
        if count == 0 or count > 4 or pos + count > len(buf):
            raise ValueError("bad DER length")
        length, pos = int.from_bytes(buf[pos:pos + count], "big"), pos + count
    if pos + length > len(buf):
        raise ValueError("truncated DER")
    return tag, pos, pos + length


def spki_from_certificate(der):
    """Extract the DER-encoded SubjectPublicKeyInfo from an X.509 certificate.

    The standard library cannot parse certificates, so walk just enough of
    Certificate/TBSCertificate to find that field.
    """
    tag, start, _ = _der_read(der, 0)
    if tag != 0x30:
        raise ValueError("not a certificate")
    tag, pos, _ = _der_read(der, start)
    if tag != 0x30:
        raise ValueError("not a certificate")
    tag, _, after = _der_read(der, pos)
    if tag == 0xA0:  # optional explicit [0] version
        pos = after
    for _ in range(5):  # serialNumber, signature, issuer, validity, subject
        _, _, pos = _der_read(der, pos)
    tag, _, end = _der_read(der, pos)
    if tag != 0x30:
        raise ValueError("no public key info")
    return der[pos:end]


def spki_sha256(der):
    return hashlib.sha256(spki_from_certificate(der)).digest()


class Client:
    def __init__(self, endpoint, token, context, pin, request_timeout):
        self.endpoint, self.token, self.context = endpoint, token, context
        self.pin, self.request_timeout = pin, request_timeout

    def _connect(self):
        endpoint = self.endpoint
        if endpoint.scheme == "https":
            conn = http.client.HTTPSConnection(endpoint.host, endpoint.port,
                                               timeout=self.request_timeout, context=self.context)
        else:
            conn = http.client.HTTPConnection(endpoint.host, endpoint.port, timeout=self.request_timeout)
        conn.connect()
        # Verify the pin before any request bytes, so the token never reaches a
        # server whose key does not match.
        if self.pin is not None:
            try:
                matched = hmac.compare_digest(spki_sha256(conn.sock.getpeercert(binary_form=True)), self.pin)
            except (ValueError, AttributeError, TypeError):
                matched = False
            if not matched:
                conn.close()
                raise NetworkError("tls: server public key does not match --pin-sha256")
        return conn

    def call(self, operation, arguments, read_timeout=None):
        body = json.dumps(arguments, separators=(",", ":")).encode("ascii")
        headers = {"Authorization": "Bearer " + self.token, "Content-Type": "application/json",
                   "Accept": "application/json", "Connection": "close", "User-Agent": "baton-cli/1"}
        conn = None
        try:
            conn = self._connect()
            if read_timeout:
                conn.sock.settimeout(read_timeout)
            conn.request("POST", self.endpoint.prefix + "/v1/space/" + operation, body=body, headers=headers)
            response = conn.getresponse()
            status, data = response.status, response.read(MAX_RESPONSE_BYTES + 1)
        except NetworkError:
            raise
        except ssl.SSLError:
            raise NetworkError("tls: handshake or certificate verification failed") from None
        except socket.timeout:
            raise NetworkError("timeout") from None
        except (OSError, http.client.HTTPException):
            raise NetworkError("connection failed") from None
        finally:
            if conn is not None:
                conn.close()
        return self._result(status, data)

    @staticmethod
    def _result(status, data):
        payload = None
        if len(data) <= MAX_RESPONSE_BYTES:
            try:
                payload = json.loads(data.decode("utf-8"))
            except ValueError:
                payload = None
        if status == 200:
            if (isinstance(payload, dict) and payload.get("x_protocol") == PROTOCOL
                    and "result" in payload and "error" not in payload):
                return payload["result"]
            raise BatonError("bad_response", status)
        code, details = "http_error", []
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict) and isinstance(error.get("code"), str) and ERROR_CODE_RE.fullmatch(error["code"]):
            code = error["code"]
            details = well_formed_details(error.get("details"))
        raise BatonError(code, status, details)


def well_formed_details(value):
    """Keep only field/expected pairs made of plain printable text: server text is never echoed unfiltered."""
    if not isinstance(value, list):
        return []
    result = []
    for item in value[:MAX_DETAILS]:
        field = item.get("field") if isinstance(item, dict) else None
        expected = item.get("expected") if isinstance(item, dict) else None
        if (isinstance(field, str) and DETAIL_FIELD_RE.fullmatch(field)
                and isinstance(expected, str) and DETAIL_EXPECTED_RE.fullmatch(expected)):
            result.append((field, expected))
    return result


def uuid_arg(value):
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise argparse.ArgumentTypeError("invalid UUID") from None


def uuid_csv(value):
    return [uuid_arg(item.strip()) for item in value.split(",")]


def scope_arg(value):
    if value == "none":
        return {"kind": "none"}
    kind, separator, scope = value.partition("=")
    if not separator or kind not in SCOPE_KINDS or not scope:
        raise argparse.ArgumentTypeError("expected kind=value with kind one of " + "/".join(SCOPE_KINDS) + ", or none")
    return {"kind": kind, "value": scope}


def agent_arg(value):
    if not AGENT_RE.fullmatch(value):
        raise argparse.ArgumentTypeError("invalid agent identifier")
    return value


def operation_arg(value):
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", value):
        raise argparse.ArgumentTypeError("invalid operation name")
    return value


def read_text_source(path):
    try:
        if path == "-":
            return sys.stdin.read()
        return Path(path).read_text(encoding="utf-8")
    except (OSError, ValueError):
        raise UsageError("cannot read input file") from None


def text_value(args, name):
    inline, path = getattr(args, name), getattr(args, name + "_file")
    return inline if path is None else read_text_source(path)


class Parser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("allow_abbrev", False)  # --token must never match --token-file
        super().__init__(*args, **kwargs)

    def error(self, message):
        # Never echo argument values: a token passed on argv by mistake must not
        # be copied into logs by our own error text.
        if message.startswith("unrecognized arguments"):
            message = "unrecognized arguments"
        message = re.sub(r": '.*$", "", message)
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, "baton_cli: error: " + message + "\n")


def add_write_options(parser):
    parser.add_argument("--operation-id", type=uuid_arg,
                        help="reuse this UUID to retry an operation exactly once (default: fresh uuid4)")


def text_group(parser, name, flag, help_text, required=True):
    group = parser.add_mutually_exclusive_group(required=required)
    group.add_argument("--" + flag, dest=name, help=help_text)
    group.add_argument("--" + flag + "-file", dest=name + "_file", metavar="PATH",
                       help="read " + flag + " from a UTF-8 file ('-' for stdin)")


def build_parser():
    common = Parser(add_help=False)
    common.add_argument("--url", default=os.environ.get("BATON_URL") or None,
                        help="Baton base URL, e.g. https://host (default: $BATON_URL)")
    common.add_argument("--token-file", metavar="PATH", help="file holding the bearer token (required)")
    common.add_argument("--ca-file", metavar="PATH", default=os.environ.get("BATON_CA_FILE") or None,
                        help="PEM CA bundle for a private CA (default: $BATON_CA_FILE)")
    common.add_argument("--pin-sha256", metavar="B64",
                        help="refuse the connection unless the leaf public key (SPKI) SHA-256 matches")
    common.add_argument("--request-timeout", type=float, default=DEFAULT_REQUEST_TIMEOUT, metavar="SECONDS",
                        help="connect/read timeout (default: %(default)s)")
    parser = Parser(prog="baton_cli.py", parents=[common], description=__doc__,
                    formatter_class=argparse.RawDescriptionHelpFormatter)
    subcommands = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)

    def command(name, help_text, summary=None, summary_help=None, **kwargs):
        sub = subcommands.add_parser(name, help=help_text, description=help_text, **kwargs)
        if summary is not None:
            sub.add_argument("--summary", action=argparse.BooleanOptionalAction, default=summary,
                             help="%s (default: %s)" % (summary_help or "print one 'task <id> state=<state> epoch=<n>' line to stderr",
                                                        "on" if summary else "off"))
        # Connection options are also accepted after the subcommand; SUPPRESS keeps
        # a value given before it (or its default) from being overwritten.
        for flag, dest in (("--url", "url"), ("--token-file", "token_file"), ("--ca-file", "ca_file"),
                           ("--pin-sha256", "pin_sha256"), ("--request-timeout", "request_timeout")):
            sub.add_argument(flag, dest=dest, default=argparse.SUPPRESS, help=argparse.SUPPRESS,
                             type=float if dest == "request_timeout" else str)
        return sub

    sub = command("inbox", "fetch_inbox: mentions and assignments for this identity")
    sub.add_argument("--limit", type=int)
    sub.set_defaults(build=lambda a: ("fetch_inbox", opt(a, limit="limit")))

    sub = command("wait", "wait_inbox: long-poll for notifications after a sequence number")
    sub.add_argument("--after-seq", type=int, default=0)
    sub.add_argument("--timeout", type=int, default=DEFAULT_WAIT_SECONDS, metavar="SECONDS",
                     help="server-side wait, 0-55 (default: %(default)s)")
    sub.add_argument("--limit", type=int)
    sub.set_defaults(build=lambda a: ("wait_inbox", {"after_seq": a.after_seq, "timeout_seconds": a.timeout,
                                                     **opt(a, limit="limit")}),
                     read_timeout=lambda a: a.timeout)

    sub = command("task", "get_task: one task by id", summary=False)
    sub.add_argument("task_id", nargs="?", type=uuid_arg, metavar="TASK_ID")
    sub.add_argument("--task", dest="task_flag", type=uuid_arg)
    sub.set_defaults(build=build_task)

    sub = command("tasks", "list_tasks")
    sub.add_argument("--limit", type=int)
    sub.set_defaults(build=lambda a: ("list_tasks", opt(a, limit="limit")))

    sub = command("ready", "ready: tasks this identity can claim now")
    sub.add_argument("--page", type=uuid_arg)
    sub.add_argument("--limit", type=int)
    sub.set_defaults(build=lambda a: ("ready", opt(a, page_id="page", limit="limit")))

    sub = command("submit", "submit_task: create a task", summary=False)
    add_write_options(sub)
    sub.add_argument("--key", required=True, help="stable idempotency key")
    text_group(sub, "prompt", "prompt", "task instructions")
    sub.add_argument("--page", type=uuid_arg)
    sub.add_argument("--assignee", help="assignee principal (manual pull task)")
    sub.add_argument("--scope", action="append", type=scope_arg, metavar="KIND=VALUE",
                     help="declared scope, repeatable; kind is path, module or migration; "
                          "'none' declares no repository footprint and cannot be combined")
    sub.add_argument("--depends-on", action="extend", type=uuid_csv, metavar="TASK_ID[,TASK_ID]")
    sub.set_defaults(build=build_submit, write=True)

    sub = command("claim", "claim: take a task lease", summary=True)
    add_write_options(sub)
    sub.add_argument("--task", required=True, type=uuid_arg)
    sub.add_argument("--lease", type=int, metavar="SECONDS")
    sub.set_defaults(build=lambda a: ("claim", {"task_id": a.task, **opt(a, lease_seconds="lease")}), write=True)

    sub = command("renew", "renew: extend a lease", summary=True)
    add_write_options(sub)
    sub.add_argument("--task", required=True, type=uuid_arg)
    sub.add_argument("--epoch", required=True, type=int)
    sub.add_argument("--lease", type=int, metavar="SECONDS")
    sub.set_defaults(build=lambda a: ("renew", {"task_id": a.task, "epoch": a.epoch,
                                                **opt(a, lease_seconds="lease")}), write=True)

    sub = command("comment", "comment: write to a page or task thread", summary=False)
    add_write_options(sub)
    sub.add_argument("--page", required=True, type=uuid_arg)
    sub.add_argument("--thread", type=uuid_arg, help="thread id (a task's thread id is its task id)")
    text_group(sub, "body", "body", "comment text")
    sub.set_defaults(build=build_comment, write=True)

    sub = command("comments", "list_comments: comments of a page or thread, or of a task with --task")
    sub.add_argument("--task", type=uuid_arg, help="a task id: reads that task's page and thread (not with --page/--thread)")
    sub.add_argument("--page", type=uuid_arg)
    sub.add_argument("--thread", type=uuid_arg, help="only this thread (a task's thread id is its task id)")
    sub.set_defaults(build=build_comments)

    sub = command("block", "mark_blocked: say why you cannot claim a task you are assigned to or review", summary=True)
    add_write_options(sub)
    sub.add_argument("--task", required=True, type=uuid_arg)
    sub.add_argument("--reason", required=True, choices=BLOCK_REASONS, help="why the task cannot be claimed")
    text_group(sub, "note", "note", "optional free-text detail for the owner", required=False)
    sub.set_defaults(build=build_block, write=True)

    sub = command("result", "submit_result: report a result for a claimed task", summary=True)
    add_write_options(sub)
    sub.add_argument("--task", required=True, type=uuid_arg)
    sub.add_argument("--epoch", required=True, type=int, help="epoch returned by claim")
    text_group(sub, "result", "result", "result text")
    sub.set_defaults(build=lambda a: ("submit_result", {"task_id": a.task, "epoch": a.epoch,
                                                        "result": text_value(a, "result")}), write=True)

    sub = command("gate", "gate: review a submitted result", summary=True)
    add_write_options(sub)
    sub.add_argument("--task", required=True, type=uuid_arg)
    sub.add_argument("--epoch", required=True, type=int, help="epoch of the result under review")
    sub.add_argument("--decision", required=True, choices=("pass", "fail"))
    sub.add_argument("--self-approval", action="store_true",
                     help="the space owner approving work it submitted or executed; refused for everyone else")
    sub.set_defaults(build=lambda a: ("gate", {"task_id": a.task, "expected_epoch": a.epoch, "decision": a.decision,
                                               **({"self_approval": True} if a.self_approval else {})}), write=True)

    sub = command("audit", "list_audit: append-only audit for a task or space")
    sub.add_argument("--task", type=uuid_arg)
    sub.add_argument("--space", type=uuid_arg)
    sub.add_argument("--limit", type=int)
    sub.set_defaults(build=build_audit)

    sub = command("call", "call any operation: call OPERATION --json '{...}'", summary=False)
    sub.add_argument("operation", type=operation_arg)
    group = sub.add_mutually_exclusive_group()
    group.add_argument("--json", dest="json_text", metavar="OBJECT", help="arguments as a JSON object")
    group.add_argument("--json-file", metavar="PATH", help="read the JSON object from a file ('-' for stdin)")
    sub.add_argument("--operation-id", type=uuid_arg,
                     help="set operation_id in the arguments (writes need one; reads must not have one)")
    sub.set_defaults(build=build_call)

    sub = command("watch", "loop on wait_inbox, one JSON line per notification on stdout, resuming from a state file; "
                           "reconnects with exponential backoff (cap 60 s) after network, TLS and 5xx errors",
                  summary=True, summary_help="print one human-readable line per notification to stderr")
    sub.add_argument("--agent", type=agent_arg, help="agent id, names the default state file")
    sub.add_argument("--state", metavar="PATH",
                     help="sequence state file (default: ~/.local/state/baton/<agent>.seq, mode 600)")
    sub.add_argument("--timeout", type=int, default=DEFAULT_WAIT_SECONDS, metavar="SECONDS",
                     help="server-side wait per poll, 1-55 (default: %(default)s)")
    sub.add_argument("--limit", type=int)
    sub.add_argument("--exec", dest="exec_command", metavar="COMMAND",
                     help="run COMMAND once per notification with the notification JSON on stdin; "
                          "no field is ever substituted into the command")
    sub.add_argument("--exec-timeout", type=float, default=EXEC_TIMEOUT, metavar="SECONDS",
                     help="stop a --exec command that runs longer than this (default: %(default)s)")
    sub.set_defaults(run=run_watch)
    return parser


def opt(args, **mapping):
    """Include only the options the user actually gave; the server owns the defaults."""
    return {key: getattr(args, attr) for key, attr in mapping.items() if getattr(args, attr) is not None}


def build_task(args):
    if (args.task_id is None) == (args.task_flag is None):
        raise UsageError("give the task id exactly once, as TASK_ID or --task")
    return "get_task", {"task_id": args.task_id or args.task_flag}


def build_submit(args):
    scopes = args.scope or []
    if any(scope["kind"] == "none" for scope in scopes) and any(scope["kind"] != "none" for scope in scopes):
        raise UsageError("--scope none cannot be combined with other scopes")
    arguments = {"idempotency_key": args.key, "prompt": text_value(args, "prompt")}
    arguments.update(opt(args, page_id="page", assignee_principal="assignee", scopes="scope", depends_on="depends_on"))
    return "submit_task", arguments


def build_comment(args):
    arguments = {"page_id": args.page, "body": text_value(args, "body")}
    arguments.update(opt(args, thread_id="thread"))
    return "comment", arguments


def build_comments(args):
    if args.task is not None:
        if args.page is not None or args.thread is not None:
            raise UsageError("--task cannot be combined with --page or --thread")
        return "list_comments", {"task_id": args.task}
    if args.page is None:
        raise UsageError("comments needs --task or --page")
    return "list_comments", opt(args, page_id="page", thread_id="thread")


def build_block(args):
    arguments = {"task_id": args.task, "reason_code": args.reason}
    if args.note is not None or args.note_file is not None:
        arguments["note"] = text_value(args, "note")
    return "mark_blocked", arguments


def build_audit(args):
    if args.task is None and args.space is None:
        raise UsageError("audit needs --task or --space")
    return "list_audit", opt(args, task_id="task", space_id="space", limit="limit")


def build_call(args):
    text = read_text_source(args.json_file) if args.json_file else (args.json_text or "{}")
    try:
        arguments = json.loads(text)
    except ValueError:
        raise UsageError("--json is not valid JSON") from None
    if not isinstance(arguments, dict):
        raise UsageError("--json must be a JSON object")
    if args.operation_id:
        arguments["operation_id"] = args.operation_id
    return args.operation, arguments


def say(message):
    print("baton_cli: " + message, file=sys.stderr)


def dump_json(value, **options):
    """Serialize without \\uXXXX escapes while staying encodable as UTF-8."""
    text = json.dumps(value, ensure_ascii=False, **options)
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:  # a lone surrogate from a \\ud800-style escape cannot be written as UTF-8
        text = json.dumps(value, ensure_ascii=True, **options)
    return text


def emit_json(value, **options):
    data = (dump_json(value, **options) + "\n").encode("utf-8")
    stream = getattr(sys.stdout, "buffer", None)
    if stream is None:
        sys.stdout.write(data.decode("utf-8"))
        sys.stdout.flush()
    else:
        sys.stdout.flush()
        stream.write(data)
        stream.flush()


def print_summary(result):
    """One human-readable line on stderr; only well-formed fields are echoed."""
    if not isinstance(result, dict):
        return
    task_id = result.get("task_id", result.get("id"))
    state, epoch = result.get("state"), result.get("epoch")
    if (not isinstance(task_id, str) or not SUMMARY_UUID_RE.fullmatch(task_id)
            or not isinstance(state, str) or not SUMMARY_STATE_RE.fullmatch(state)):
        return
    line = "task " + task_id + " state=" + state
    if type(epoch) is int:
        line += " epoch=" + str(epoch)
    print(line, file=sys.stderr, flush=True)


def make_client(args):
    if not args.token_file:
        raise UsageError("missing --token-file (the token is only read from a file)")
    endpoint = parse_endpoint(args.url)
    if not args.request_timeout > 0:
        raise UsageError("--request-timeout must be positive")
    pin = None
    if args.pin_sha256:
        if endpoint.scheme != "https":
            raise UsageError("--pin-sha256 requires an https:// URL")
        pin = parse_pin(args.pin_sha256)
    token = read_token(args.token_file)
    context = tls_context(args.ca_file) if endpoint.scheme == "https" else None
    return Client(endpoint, token, context, pin, args.request_timeout)


def run_command(args):
    operation, arguments = args.build(args)
    if getattr(args, "write", False):
        arguments["operation_id"] = args.operation_id or str(uuid.uuid4())
    client = make_client(args)
    read_timeout = getattr(args, "read_timeout", None)
    try:
        result = client.call(operation, arguments,
                             read_timeout=args.request_timeout + read_timeout(args) if read_timeout else None)
    except (BatonError, NetworkError) as error:
        error.operation_id = arguments.get("operation_id")
        raise
    emit_json(result, indent=2)
    if getattr(args, "summary", False):
        print_summary(result)
    return EXIT_OK


def default_state_path(agent):
    return Path(os.path.expanduser("~")) / ".local" / "state" / "baton" / (agent + ".seq")


def load_seq(path):
    try:
        text = path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        return 0
    except (OSError, ValueError):
        raise UsageError("cannot read state file") from None
    if not re.fullmatch(r"[0-9]{1,18}", text):
        raise UsageError("invalid state file: expected a single non-negative integer")
    return int(text)


def save_seq(path, seq):
    temporary = path.with_name("." + path.name + "." + str(os.getpid()) + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, (str(seq) + "\n").encode("ascii"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, path)
    except OSError:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise UsageError("cannot write state file") from None


class Stopper:
    """Turn SIGINT/SIGTERM into a clean exit, without cutting a batch off mid-way.

    The handler only sets a flag. Blocking work runs where the main thread can notice the flag between short
    waits, so a signal can never raise inside unrelated code (an import, a weakref callback, a state write).
    """

    POLL = 0.2
    STOPPED = object()

    def __init__(self):
        self.stop = False

    def handle(self, signum, frame):
        self.stop = True

    def run(self, work):
        """Run work() in a helper thread; return its result, or STOPPED as soon as a signal arrived."""
        outcome = {}

        def target():
            try:
                outcome["value"] = work()
            except BaseException as error:  # re-raised in the main thread below
                outcome["error"] = error

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        while thread.is_alive():
            thread.join(self.POLL)
            if self.stop and thread.is_alive():
                return self.STOPPED
        if "error" in outcome:
            raise outcome["error"]
        return outcome["value"]

    def sleep(self, seconds):
        deadline = time.monotonic() + seconds
        while not self.stop:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(self.POLL, remaining))


def backoff_delay(failures):
    """Seconds to wait after the nth consecutive failure: 1, 2, 4, ... capped at 60."""
    return min(BACKOFF_CAP, BACKOFF_START * 2 ** min(failures - 1, 16))


def transient(error):
    """Failures worth reconnecting for: the network, TLS, a 5xx from Baton or an intermediary, or 429."""
    return isinstance(error, NetworkError) or (isinstance(error, BatonError) and (error.status >= 500 or error.status == 429))


def plain(text, limit):
    """Printable ASCII only, so another principal's text cannot inject control sequences into a terminal."""
    cleaned = "".join(ch if " " <= ch <= "~" else "?" for ch in str(text))
    return cleaned[:limit]


def notification_line(notification):
    """One human-readable line; only well-formed identifiers are echoed, the subject is sanitised."""
    parts = ["notification"]
    if type(notification.get("seq")) is int:
        parts.append("seq=" + str(notification["seq"]))
    event = notification.get("event")
    if isinstance(event, str) and NOTIFICATION_EVENT_RE.fullmatch(event):
        parts.append(event)
    kind, identity = notification.get("object_type"), notification.get("object_id")
    if kind in ("task", "comment", "page") and isinstance(identity, str) and SUMMARY_UUID_RE.fullmatch(identity):
        parts.append(kind + "=" + identity)
    actor = notification.get("actor")
    principal = actor.get("principal") if isinstance(actor, dict) else None
    if isinstance(principal, str) and AGENT_RE.fullmatch(principal):
        kind_name = actor.get("kind")
        parts.append("from=" + principal + ("(" + kind_name + ")" if kind_name in ("human", "agent", "service") else ""))
    if isinstance(notification.get("subject"), str):
        parts.append("- " + plain(notification["subject"], 100))
    return " ".join(parts)


def run_exec(command, notification, timeout):
    """Run the user's command once with the notification on stdin.

    The command string is passed to the shell exactly as given; no notification field is ever substituted
    into it. Its output goes to stderr so stdout stays one JSON object per line. It runs in its own process
    group so a timeout also stops anything it started.
    """
    payload = (dump_json(notification, separators=(",", ":")) + "\n").encode("utf-8")
    sys.stderr.flush()
    try:
        child = subprocess.Popen(command, shell=True, stdin=subprocess.PIPE, stdout=sys.stderr, stderr=sys.stderr,
                                 start_new_session=True)
    except OSError:
        say("watch: --exec command could not be started")
        return
    try:
        child.communicate(payload, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except OSError:
            child.kill()
        child.wait()
        say("watch: --exec command timed out and was stopped")
    else:
        if child.returncode != 0:
            say("watch: --exec command exited with status " + str(child.returncode))


def run_watch(args):
    if not 1 <= args.timeout <= 55:
        raise UsageError("--timeout must be between 1 and 55 for watch")
    if args.state is None and args.agent is None:
        raise UsageError("watch needs --agent (default state file name) or --state")
    if not args.exec_timeout > 0:
        raise UsageError("--exec-timeout must be positive")
    path = Path(args.state) if args.state else default_state_path(args.agent)
    client = make_client(args)
    seq = load_seq(path)
    save_seq(path, seq)  # fail early if the state cannot be persisted; also tightens the mode to 600
    stopper = Stopper()
    signal.signal(signal.SIGINT, stopper.handle)
    signal.signal(signal.SIGTERM, stopper.handle)
    failures = 0
    while not stopper.stop:
        arguments = {"after_seq": seq, "timeout_seconds": args.timeout}
        if args.limit is not None:
            arguments["limit"] = args.limit
        try:
            result = stopper.run(lambda: client.call("wait_inbox", arguments, read_timeout=args.request_timeout + args.timeout))
        except (NetworkError, BatonError) as error:
            if stopper.stop:
                break
            if not transient(error):
                raise
            failures += 1
            delay = backoff_delay(failures)
            reason = error.category if isinstance(error, NetworkError) else error.code + " (HTTP " + str(error.status) + ")"
            say("watch: " + reason + "; reconnecting in " + str(delay) + "s")
            stopper.sleep(delay)
            continue
        if result is Stopper.STOPPED:
            break
        failures = 0
        notifications = result.get("notifications") if isinstance(result, dict) else None
        next_seq = result.get("next_seq") if isinstance(result, dict) else None
        if not isinstance(notifications, list) or type(next_seq) is not int:
            raise BatonError("bad_response", 200)
        for notification in notifications:
            emit_json(notification, separators=(",", ":"))
            if args.summary and isinstance(notification, dict):
                print(notification_line(notification), file=sys.stderr, flush=True)
            if args.exec_command:
                run_exec(args.exec_command, notification, args.exec_timeout)
        if next_seq > seq:
            # Persist only after the lines are out: a crash replays rather than loses.
            save_seq(path, next_seq)
            seq = next_seq
    return EXIT_OK


def retry_hint(error):
    operation_id = getattr(error, "operation_id", None)
    if operation_id:
        say("operation_id=" + operation_id + " (pass --operation-id to retry exactly this operation)")


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.run(args) if hasattr(args, "run") else run_command(args)
    except UsageError as error:
        say("error: " + str(error))
        return EXIT_USAGE
    except BatonError as error:
        say("error: " + error.code + " (HTTP " + str(error.status) + ")")
        for field, expected in error.details:
            say("  " + field + ": expected " + expected)
        retry_hint(error)
        return EXIT_BATON
    except NetworkError as error:
        say("network error: " + error.category)
        retry_hint(error)
        return EXIT_NETWORK
    except KeyboardInterrupt:
        return EXIT_OK if getattr(args, "run", None) else 130
    except BrokenPipeError:
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
