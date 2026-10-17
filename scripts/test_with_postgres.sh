#!/usr/bin/env sh
# SPDX-License-Identifier: Apache-2.0
#
# Run the full Baton test suite against a throwaway PostgreSQL in Docker.
#
# Schema bootstrap: reuses deploy/migrate.py on the host. migrate.py hardcodes
# host "database" and port 5432 for Compose; this script patches psycopg.connect
# to target the published loopback port instead (migrate.py has no env override).

set -eu

REPO_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$REPO_ROOT"

if ! command -v docker >/dev/null 2>&1; then
  echo "error: docker is not installed or not on PATH" >&2
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "error: docker daemon is not available (start Docker or check permissions)" >&2
  exit 1
fi

read_versions() {
  python3 -c '
import json, sys
data = json.load(open("deploy/versions.json"))
for key in sys.argv[1:]:
    print(data[key])
' "$@"
}

POSTGRES_IMAGE=$(read_versions postgres)

WORK_DIR=$(mktemp -d "${TMPDIR:-/tmp}/baton-pg-test.XXXXXX")
chmod 700 "$WORK_DIR"
ADMIN_SECRETS="$WORK_DIR/secrets/database-admin"
mkdir -p "$ADMIN_SECRETS"
chmod 700 "$WORK_DIR/secrets" "$ADMIN_SECRETS"

CONTAINER="baton-test-pg-$$"
PYTEST_STATUS=0

cleanup() {
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  rm -rf "$WORK_DIR"
}
trap cleanup EXIT INT TERM

python3 -c '
import secrets
from pathlib import Path
root = Path("'$ADMIN_SECRETS'")
for name in ("postgres", "app"):
    path = root / f"{name}.password"
    path.write_text(secrets.token_urlsafe(48) + "\n")
    path.chmod(0o600)
'

PORT=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')

VENV="$WORK_DIR/venv"
python3 -m venv "$VENV"
# shellcheck disable=SC1091
. "$VENV/bin/activate"
pip install --disable-pip-version-check -q \
  --require-hashes --only-binary=:all: \
  -r deploy/requirements.txt
pip install --disable-pip-version-check -q pytest pytest-asyncio

docker run -d \
  --name "$CONTAINER" \
  -p "127.0.0.1:${PORT}:5432" \
  -e POSTGRES_DB=baton \
  -e POSTGRES_USER=baton_bootstrap \
  -e POSTGRES_PASSWORD_FILE=/run/secrets/database-admin/postgres.password \
  -e 'POSTGRES_INITDB_ARGS=--auth-host=scram-sha-256 --auth-local=scram-sha-256' \
  -e PGDATA=/var/lib/postgresql/data/pgdata \
  -v "${ADMIN_SECRETS}:/run/secrets/database-admin:ro" \
  "$POSTGRES_IMAGE" \
  postgres \
  -c log_statement=none \
  -c log_min_error_statement=panic \
  -c log_min_messages=fatal \
  -c log_min_duration_statement=-1 \
  -c log_min_duration_sample=-1 \
  -c log_transaction_sample_rate=0 \
  -c log_parameter_max_length_on_error=0 \
  -c log_error_verbosity=terse \
  >/dev/null

ready=0
i=0
while [ "$i" -lt 90 ]; do
  if docker exec "$CONTAINER" pg_isready -U baton_bootstrap -d baton -q 2>/dev/null; then
    ready=1
    break
  fi
  i=$((i + 1))
  sleep 1
done
if [ "$ready" -ne 1 ]; then
  echo "error: PostgreSQL did not become ready in time" >&2
  exit 1
fi

if ! BATON_TEST_ADMIN_SECRETS="$ADMIN_SECRETS" BATON_TEST_PG_PORT="$PORT" python -c '
import os
from pathlib import Path

import psycopg

import deploy.migrate as migrate

migrate.SECRETS = Path(os.environ["BATON_TEST_ADMIN_SECRETS"])
_pg_port = int(os.environ["BATON_TEST_PG_PORT"])
_connect = psycopg.connect

def connect(*args, **kwargs):
    kwargs["host"] = "127.0.0.1"
    kwargs["port"] = _pg_port
    return _connect(*args, **kwargs)

psycopg.connect = connect
migrate.migrate()
' >/dev/null; then
  echo "error: deploy/migrate.py failed (details suppressed)" >&2
  exit 1
fi

APP_CONFIG="$WORK_DIR/app.json"
ADMIN_CONFIG="$WORK_DIR/admin.json"
python3 -c "
import json
from pathlib import Path

def load_password(name):
    return (Path('$ADMIN_SECRETS') / f'{name}.password').read_text().strip()

common = {'host': '127.0.0.1', 'port': $PORT, 'dbname': 'baton'}
app = {**common, 'user': 'baton_app', 'password': load_password('app')}
admin = {**common, 'user': 'baton_bootstrap', 'password': load_password('postgres')}
for path, payload in (('$APP_CONFIG', app), ('$ADMIN_CONFIG', admin)):
    p = Path(path)
    p.write_text(json.dumps(payload) + '\n')
    p.chmod(0o600)
"

export BATON_TEST_DATABASE_CONFIG="$APP_CONFIG"
export BATON_TEST_DATABASE_ADMIN_CONFIG="$ADMIN_CONFIG"

set +e
"${TEST_PYTHON:-python}" -m pytest -q -rfEs "$@"
PYTEST_STATUS=$?
set -e

python3 scripts/check_public_tree.py

exit "$PYTEST_STATUS"
