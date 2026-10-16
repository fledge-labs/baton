#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
set -eu

if [ "$#" -ne 2 ]; then
  echo 'Usage: launch_mcp.sh <agent-id> <TOKEN_FILE>' >&2
  exit 2
fi
case "$1" in
  ''|[!A-Za-z0-9]*|*[!A-Za-z0-9_.-]*) echo 'Invalid agent identifier' >&2; exit 2 ;;
esac
if [ "${#1}" -gt 96 ]; then
  echo 'Invalid agent identifier' >&2
  exit 2
fi
baton_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec "${BATON_PYTHON:-$baton_dir/.venv/bin/python}" "$baton_dir/adapters/mcp_stdio.py" \
  --agent-id "$1" --token-file "$2"
