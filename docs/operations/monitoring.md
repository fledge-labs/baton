<!-- SPDX-License-Identifier: Apache-2.0 -->
# External health monitoring and alerting

`scripts/monitor.py` watches a Baton deployment from the outside, the way a client sees it, and exposes the result to an alert relay. It is a single Python standard-library file (no new dependencies) meant to run as a long-lived process, ideally on a machine that is **not** inside Baton's Compose project so that it still works when Baton is down.

It works in **pull mode**: the monitor does not push anything and the host opens no new port. A small read-only HTTP API listens on `127.0.0.1` only, and the relay reaches it over SSH. The dead-man check (what happens if the monitor itself dies) belongs to the relay, which sees `/healthz` fail or time out.

## What it checks

Every `--interval` seconds (default 60) it runs three probes. Each is timed.

| Check | Request | Healthy when | Alert name |
| --- | --- | --- | --- |
| `board` | `GET /healthz` on the public edge with the **monitoring token** (`Authorization: Bearer …` from `--token-file`; the token is never logged) | HTTP `200` and a JSON body whose `status` field is exactly `"ok"` after the request passes the gateway client ACL and reaches the bridge database check | `BatonBoardDown` |
| `mcp_unauth` | `POST /mcp` with an `initialize` body and **no** credentials | status is exactly `401`; anything else means authentication is not enforced at the edge | `BatonMcpAuthBroken` |
| `mcp_call` | `POST /mcp` `initialize`, `notifications/initialized`, then `tools/call` `fetch_inbox`, authenticated with the monitoring token | each step succeeds and the tool result is not an error | `BatonMcpCallFailed` |

Unlike the static `/board` HTML page at the edge, **`/healthz` is not public**: unauthenticated requests are rejected at the gateway and bridge. The `board` probe therefore uses the same monitoring token as `mcp_call`, which proves the path through the edge and gateway to a live bridge and database.

`mcp_call` is read-only: `fetch_inbox` does not change any state. Plain JSON and `text/event-stream` replies are both understood; a stream is read until the server closes it, so a server that keeps a stream open fails the probe with `timeout`.

### Probe deadline

Every probe has a hard wall-clock limit, `--probe-deadline` (default 30 seconds), covering everything it does (all three MCP requests together). `--timeout` (default 10) only limits a single socket operation, so on its own it cannot stop a server that trickles bytes or never closes a stream. When the deadline passes the probe's socket is shut down and the probe counts as one failure with category `timeout`. As a backstop for steps no socket timeout can interrupt (a resolver that never answers), each probe also runs on its own thread that the monitor abandons two seconds after the deadline; at most one such thread per probe is ever left behind, and that probe is reported as `timeout` until it finishes. The optional push sink gets the same deadline, so a stuck webhook cannot stall the probe loop either. A probe cycle therefore takes at most about three times the deadline.

### The monitoring token

Use a dedicated identity so that its use is visible in the audit log and it can be rotated or revoked on its own. Issue it like any other client token (see the README), for example `admin issue baton-monitor`, export it to a `0600` file outside any repository, and pass that file by path:

```sh
# SPDX-License-Identifier: Apache-2.0
docker compose run --rm --no-deps \
  -v "$PWD/monitor-delivery:/delivery" admin issue baton-monitor \
  --output /delivery/baton-monitor.token --uid "$(id -u)" --gid "$(id -g)"
```

The token is only ever read from `--token-file`. It is not accepted as an argument or environment variable, so it cannot leak through `ps` or the process environment. The `board` and `mcp_call` probes send it to the configured Baton URL only.

## Pull-mode contract

The API binds to `127.0.0.1` on port `19801` (`--bind`, `--port`). It has no authentication, so binding anything that is not a loopback address is refused at startup unless `--unsafe-bind` is given explicitly; do not use that flag unless something else restricts access. Only `GET` is served; other methods return `405` and unknown paths `404`. Responses are `application/json` and are never cached. An unexpected internal error is answered with `500` and a fixed body, never a dropped connection.

Hardening, all fixed in the code:

- **Host check.** Unless `--unsafe-bind` is given, the `Host` header must be `127.0.0.1`, `localhost` or `[::1]` (any port, so an SSH port forward on another local port works). Anything else, including a missing header, gets `403`. This stops a web page from reading the alerts through DNS rebinding. With `--unsafe-bind` the check is off, because the clients' host names are then unknown.
- **Slow or idle clients.** Each connection has a 5 second socket timeout and a 10 second total lifetime, so a client that drips bytes cannot hold it open.
- **Connection cap.** At most 16 connections are served at once; further ones are closed immediately. Once the slow ones are gone capacity returns.
- **Hang-ups.** A client that disconnects mid-response is ignored quietly.

### `GET /api/v1/alerts`

A [vmalert](https://docs.victoriametrics.com/vmalert/)-compatible list of the alerts that are firing right now:

```json
{
  "status": "success",
  "data": {
    "alerts": [
      {
        "labels": {"alertname": "BatonBoardDown", "severity": "critical", "service": "baton"},
        "annotations": {
          "summary": "Baton board stack is not healthy",
          "description": "GET /healthz with the monitoring token did not return 200 with status ok on baton-prod; last error: http_503; 3 consecutive failed probes"
        },
        "state": "firing",
        "activeAt": "2026-01-01T12:00:00Z",
        "value": "3"
      }
    ]
  }
}
```

- A check appears after `--failures` (default 3) consecutive failed probes, one alert per check, named as in the table above. Fewer failures never show up.
- It is **removed** from the list as soon as the check passes again. The monitor sends no recovery message; the relay detects the disappearance and sends its own recovery notice.
- `activeAt` is the time (UTC, RFC 3339) of the **first failure of the current streak**, not of the third. It stays the same for as long as the streak lasts, so the relay can use `labels` plus `activeAt` as the identity of an incident. A later outage of the same check gets a new `activeAt`.
- `description` states the host label (`--host-label`), the last failure category (`http_503`, `timeout`, `connection_error`, `tls_error`, `fetch_inbox_failed`, ...) and the current streak length; the streak length therefore changes while an alert is firing, but `labels` and `activeAt` do not. It never contains a URL, header, response body or token.
- `value` is the current number of consecutive failed probes, as a string, the way vmalert reports a sample value.
- The list is empty (`"alerts": []`) when everything is healthy.

### `GET /healthz`

```json
{
  "status": "ok",
  "last_probe_at": "2026-01-01T12:00:30Z",
  "checks": {
    "board": {"ok": true, "error": null, "latency_ms": 12, "consecutive_failures": 0},
    "mcp_unauth": {"ok": true, "error": null, "latency_ms": 9, "consecutive_failures": 0},
    "mcp_call": {"ok": false, "error": "http_502", "latency_ms": 41, "consecutive_failures": 1}
  }
}
```

- `last_probe_at` is updated after **every individual probe** (a probe that hit its deadline counts: the monitor is alive and the failure is in the check).
- The status is `200` while a probe completed within the last 3 intervals (`3 x --interval`), otherwise `503` with `"status": "stale"` (also before the first probe has completed, when `last_probe_at` is `null`).
- `/healthz` answers whether **the monitor** is alive and probing. A failing Baton check does not make it return `503`; that is what `/api/v1/alerts` is for.

### How the relay pulls

The relay connects over SSH and talks to the loopback port on the monitor host. Either run `curl` through SSH:

```sh
# SPDX-License-Identifier: Apache-2.0
ssh relay-reader@example.com curl -fsS http://127.0.0.1:19801/api/v1/alerts
ssh relay-reader@example.com curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:19801/healthz
```

or keep a port forward open and poll `http://127.0.0.1:<local-port>/...` locally:

```sh
# SPDX-License-Identifier: Apache-2.0
ssh -N -L 127.0.0.1:19801:127.0.0.1:19801 relay-reader@example.com
```

Restrict the relay's SSH key to this purpose (a dedicated user, and `command=`/`permitopen="127.0.0.1:19801"` in `authorized_keys`). A relay that cannot reach `/healthz`, gets a non-200, or sees the SSH connection fail should raise its own alert: that is the dead-man check.

## Optional push sink (off by default)

`--sink webhook` or `--sink stdout` additionally pushes messages, independent of the pull API. Nothing is pushed by default.

Per check: after `--failures` consecutive failures **one** alert (`status: "alert"`); nothing more while it keeps failing, except a reminder every `--realert-minutes` (default 60, `0` disables; message begins "still failing"); when it recovers **one** recovery message (`status: "recovered"`). An alert or recovery only counts as sent once the sink accepts it; otherwise it is retried on later cycles and never duplicated after success. If the sink was down when the check recovered, the recovery is retried on every healthy cycle. If the check fails again before a recovery was delivered, the earlier alert simply stays the open incident: no recovery is sent for the interrupted outage, no new alert is sent for the new streak, and reminders continue on their normal schedule. There is no heartbeat.

`--sink webhook` does `POST <webhook-url>` with `Content-Type: application/json` and, when `--webhook-token-file` is given, `Authorization: Bearer <token>`. Any `2xx` is success. Redirects are not followed. The URL comes from `--webhook-url` or `BATON_MONITOR_WEBHOOK_URL`; plain `http://` is only accepted for loopback hosts. The URL may itself be a secret (chat webhooks often embed one), so it is never logged or placed in a message.

```json
{
  "source": "baton-monitor",
  "host_label": "baton-prod",
  "check": "board",
  "status": "alert",
  "consecutive_failures": 3,
  "latency_ms": 1043,
  "message": "board failing: http_503 (3 consecutive failures)",
  "ts": "2026-01-01T12:00:00Z"
}
```

`check` is `board`, `mcp_unauth` or `mcp_call`; `status` is `alert` or `recovered` (`consecutive_failures` is `0` for `recovered`); `latency_ms` is the latest probe in integer milliseconds; `ts` is UTC. `--sink stdout` writes the same object as one JSON line per message to standard output (logs go to standard error); use it for tests.

To change the wire format, subclass `WebhookSink`, override `send(payload)` to reshape the payload, and register it in `build_sink`. A sink raises `SinkError("<fixed category>")` on failure and must never include the URL, token or response body in an exception or log line.

## Running it

```sh
# SPDX-License-Identifier: Apache-2.0
python3 scripts/monitor.py \
  --url https://baton.example.com \
  --token-file /etc/baton-monitor/baton-monitor.token \
  --host-label baton-prod
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--url` / `BATON_URL` | required | Baton base URL; a trailing `/mcp` is accepted. `http://` only for loopback hosts |
| `--token-file` | required | file with the monitoring token |
| `--ca-file` | system store | CA bundle for private PKI (TLS 1.2 or newer is enforced) |
| `--interval` | 60 | seconds between probe cycles (up to 86400) |
| `--timeout` | 10 | timeout in seconds of one socket operation (connect, read) |
| `--probe-deadline` | 30 | hard limit in seconds for everything one probe or one sink delivery does |
| `--failures` | 3 | consecutive failures before an alert fires |
| `--bind` | `127.0.0.1` | address of the pull API; loopback only |
| `--port` | 19801 | port of the pull API |
| `--unsafe-bind` | off | allow a non-loopback `--bind` (the API is unauthenticated) |
| `--host-label` | hostname | label used in alert text and webhook payloads |
| `--state-file` | `$STATE_DIRECTORY/monitor-state.json`, else `./baton-monitor-state.json` | JSON state |
| `--sink` | `none` | optional push sink: `none`, `webhook` or `stdout` |
| `--webhook-url` / `BATON_MONITOR_WEBHOOK_URL` | - | webhook target (`--sink webhook`) |
| `--webhook-token-file` | - | file with the optional webhook bearer token |
| `--realert-minutes` | 60 | sink reminder interval while still down; `0` disables |
| `--once` | off | run one cycle without the API and exit 1 if any probe failed |

Numeric options reject `nan`, `inf`, zero or negative values and absurdly large ones with exit code 2. An explicit port `0` in `--url` or the webhook URL is rejected (it is not connectable) instead of being replaced by the scheme's default port.

Exit codes: `0` clean stop (SIGTERM/SIGINT) or `--once` with all probes passing, `1` `--once` with a failing probe, `2` usage or configuration error (including an unusable bind address or a busy port).

### State file

A small JSON file holds each check's failure streak (count, start time, last failure category) and, for the optional sink, whether an alert is outstanding. It is written atomically with mode `0600` and contains no secrets. Values read back are validated: a non-finite or unusable number is discarded, and a streak without a usable start time restarts at the time of loading rather than at 1970. If the file cannot be written, the monitor logs `cannot write state file` and keeps probing and serving; only restart persistence is lost. Because it survives restarts, a restart during an outage keeps the alert listed with the original `activeAt`; the first probe after the restart refreshes it. A missing or unreadable file starts fresh.

### systemd

`/etc/systemd/system/baton-monitor.service`:

```ini
# SPDX-License-Identifier: Apache-2.0
[Unit]
Description=Baton external health monitor (pull mode)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
DynamicUser=yes
StateDirectory=baton-monitor
LoadCredential=baton-monitor.token:/etc/baton-monitor/baton-monitor.token
ExecStart=/usr/bin/python3 /opt/baton/scripts/monitor.py \
  --url https://baton.example.com \
  --host-label baton-prod \
  --token-file ${CREDENTIALS_DIRECTORY}/baton-monitor.token \
  --bind 127.0.0.1 --port 19801
Restart=always
RestartSec=10
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectControlGroups=yes
RestrictAddressFamilies=AF_INET AF_INET6
IPAddressDeny=any
IPAddressAllow=localhost
MemoryDenyWriteExecute=yes

[Install]
WantedBy=multi-user.target
```

Notes:

- `IPAddressDeny=any` with `IPAddressAllow=localhost` is only suitable when Baton is reached over loopback. When it is remote, as recommended, drop those two lines or allow the Baton address explicitly.
- To also use the optional webhook sink, add `LoadCredential=webhook.token:/etc/baton-monitor/webhook.token`, `--sink webhook --webhook-token-file ${CREDENTIALS_DIRECTORY}/webhook.token`, and put `BATON_MONITOR_WEBHOOK_URL=...` in a root-owned `0600` file referenced by `EnvironmentFile=`.
- `LoadCredential=` and `${CREDENTIALS_DIRECTORY}` need systemd 250 or newer; on older versions, point `--token-file` at a `0600` file owned by a dedicated service user (`User=baton-monitor`) instead of using `DynamicUser` and `LoadCredential`.
- `StateDirectory=baton-monitor` sets `STATE_DIRECTORY`, so the state file lands in `/var/lib/baton-monitor/monitor-state.json` with no extra flag.
- Logs go to the journal through standard error: one summary line per cycle (check name, outcome category, latency), never a token, a URL or a response body.
- Verify with `systemctl status baton-monitor`, `curl -fsS http://127.0.0.1:19801/healthz` on the host, and by stopping Baton briefly on a test deployment: after about three minutes `/api/v1/alerts` lists the alerts, and they disappear once Baton is back.

## Security notes

- The monitoring token is sent only to the configured Baton URL, over TLS unless that URL is loopback.
- The monitor opens no connection to the relay and listens only on loopback; the relay's SSH access is the only way in.
- It never follows redirects and ignores proxy environment variables.
- The `mcp_unauth` probe deliberately sends no credentials; if `BatonMcpAuthBroken` ever fires, treat it as a security incident rather than an availability one.
- Failure categories are fixed strings, so an unexpected server response can never place its content in a log, an alert or the API.
- A monitor on the same host as Baton cannot detect a host outage. Keep the relay, which owns the dead-man check, independent of the Baton deployment.
