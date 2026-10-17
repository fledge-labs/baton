# SPDX-License-Identifier: Apache-2.0
"""Webhook URL policy (SSRF guard), Standard Webhooks request signing and a pinned-address HTTPS POST.

Nothing here logs or persists anything. post() reduces every failure to a small fixed (outcome, status) vocabulary
and never returns response bodies, so callers can record it without leaking data. The URL policy functions
(parse_url, resolve, check_url) return only host, port, request target, addresses or the URL they were given.
The signing helpers (sign, format_secret, parse_secret, standard_signature, standard_headers) necessarily take
secrets and return secret text or signatures to their caller, which must not log or persist them.
"""

import base64
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import socket
import ssl
import threading
from urllib.parse import urlsplit

TIMEOUT_SECONDS = 10
MAX_URL_LENGTH = 2048
ALLOW_HOSTS_ENV = "BATON_WEBHOOK_ALLOW_HOSTS"
CA_BUNDLE_ENV = "BATON_WEBHOOK_CA_BUNDLE"
SECRET_PREFIX = "whsec_"
METADATA_ADDRESSES = frozenset(
    [ipaddress.ip_address(".".join(map(str, parts))) for parts in ((169, 254, 169, 254), (169, 254, 170, 2), (100, 100, 100, 200), (192, 0, 0, 192))]
    + [ipaddress.ip_address("fd00:ec2::254")])
NAT64 = ipaddress.ip_network("64:ff9b::/96")

# Indirection so tests can simulate DNS answers (including a rebinding answer).
_getaddrinfo = socket.getaddrinfo


class Refused(Exception):
    """The destination is not allowed by the SSRF policy."""


def allowed_hosts():
    return frozenset(item.strip().lower().rstrip(".") for item in os.environ.get(ALLOW_HOSTS_ENV, "").split(",") if item.strip())


def _embedded_v4(address):
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return address.ipv4_mapped
        if address in NAT64:
            return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
        if address.sixtofour is not None:
            return address.sixtofour
        if address.teredo is not None:
            return address.teredo[1]
    return None


def address_is_public(address):
    """True only for globally routable unicast addresses that are not cloud metadata endpoints."""
    if not isinstance(address, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
        return False
    embedded = _embedded_v4(address)
    if embedded is not None and not address_is_public(embedded):
        return False
    if address in METADATA_ADDRESSES:
        return False
    return not (address.is_loopback or address.is_link_local or address.is_private or address.is_multicast
                or address.is_reserved or address.is_unspecified or not address.is_global)


def parse_url(url):
    """Return (host, port, target) for a syntactically acceptable https URL, else raise Refused."""
    if not isinstance(url, str) or not url or len(url) > MAX_URL_LENGTH or any(ord(c) <= 32 or ord(c) == 127 for c in url):
        raise Refused()
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        raise Refused() from None
    if parts.scheme.lower() != "https" or not host or parts.username is not None or parts.password is not None:
        raise Refused()
    host = host.lower().rstrip(".")
    if not host or port == 0:
        raise Refused()
    target = (parts.path or "/") + ("?" + parts.query if parts.query else "")
    return host, port or 443, target


def resolve(host, port):
    """Resolve host and return the addresses a connection may use; raise Refused when none qualify."""
    try:
        infos = _getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        raise Refused() from None
    exempt = host in allowed_hosts()
    addresses = []
    for family, _, _, _, sockaddr in infos:
        if family not in (socket.AF_INET, socket.AF_INET6):
            continue
        try:
            addresses.append(ipaddress.ip_address(sockaddr[0].split("%")[0]))
        except ValueError:
            raise Refused() from None
    # One forbidden record means the whole name is untrustworthy, so a mixed answer is refused.
    if not addresses or (not exempt and not all(address_is_public(address) for address in addresses)):
        raise Refused()
    return list(dict.fromkeys(addresses))


def check_url(url):
    host, port, _ = parse_url(url)
    resolve(host, port)
    return url


def sign(secret, body):
    """Deprecated X-Baton-Signature value, kept for one release alongside the Standard Webhooks headers."""
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


def format_secret(raw):
    """Standard Webhooks secret text: "whsec_" plus the base64 of the raw signing key."""
    return SECRET_PREFIX + base64.b64encode(raw).decode("ascii")


def parse_secret(text):
    """Raw signing key bytes from a "whsec_<base64>" secret."""
    if not isinstance(text, str) or not text.startswith(SECRET_PREFIX):
        raise ValueError("secret must start with whsec_")
    return base64.b64decode(text[len(SECRET_PREFIX):], validate=True)


def standard_signature(secret, message_id, timestamp, body):
    """One "v1,<base64 HMAC-SHA256>" entry over "{id}.{timestamp}.{body}" (Standard Webhooks)."""
    signed = f"{message_id}.{timestamp}.".encode("ascii") + body
    return "v1," + base64.b64encode(hmac.new(secret, signed, hashlib.sha256).digest()).decode("ascii")


def standard_headers(secrets, message_id, timestamp, body):
    """webhook-id, webhook-timestamp and webhook-signature; one space-separated v1 entry per active secret."""
    return {"webhook-id": message_id, "webhook-timestamp": str(int(timestamp)),
            "webhook-signature": " ".join(standard_signature(secret, message_id, int(timestamp), body) for secret in secrets)}


def encode(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _context():
    bundle = os.environ.get(CA_BUNDLE_ENV)
    context = ssl.create_default_context(cafile=bundle) if bundle else ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


class _PinnedConnection(http.client.HTTPSConnection):
    """Connects only to addresses that pass the policy at connect time and never follows redirects."""

    def connect(self):
        host = self.host
        candidates = resolve(host, self.port)
        last = None
        for address in candidates:
            try:
                sock = socket.create_connection((str(address), self.port), timeout=self.timeout)
            except OSError as exc:
                last = exc
                continue
            peer = ipaddress.ip_address(sock.getpeername()[0].split("%")[0])
            if peer not in candidates:
                sock.close()
                raise Refused()
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.sock = self._context.wrap_socket(sock, server_hostname=host)
            return
        raise last or Refused()


def post(url, body, headers, timeout=TIMEOUT_SECONDS):
    """POST body once. Returns (outcome, http_status) with outcome in delivered/http_error/network_error/refused."""
    try:
        host, port, target = parse_url(url)
    except Refused:
        return "refused", None
    connection = _PinnedConnection(host, port, timeout=timeout, context=_context())
    done = threading.Event()

    def watchdog():
        if not done.wait(timeout):
            try:
                if connection.sock is not None:
                    connection.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    timer = threading.Thread(target=watchdog, daemon=True)
    timer.start()
    try:
        connection.request("POST", target, body=body, headers={**headers, "Content-Length": str(len(body))})
        status = connection.getresponse().status
    except Refused:
        return "refused", None
    except (OSError, http.client.HTTPException, ssl.SSLError, ValueError):
        return "network_error", None
    finally:
        done.set()
        connection.close()
    return ("delivered" if 200 <= status < 300 else "http_error"), status
