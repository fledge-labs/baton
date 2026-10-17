# SPDX-License-Identifier: Apache-2.0
"""Operator commands for task scopes: `python -m bridge.scopes migrate-manual-root-to-none [--dry-run]`.

Run inside the bridge container, where the database configuration is mounted. The command prints one JSON
summary and never prints task content, SQL or connection details.
"""

import argparse
import json
import sys

from bridge.space_store import SERVICE_ACTOR, SpaceStore, StoreError


def migrate_manual_root_to_none(dry_run, store=None):
    store = store or SpaceStore.from_env()
    return store.migrate_manual_root_scopes(SERVICE_ACTOR, dry_run=dry_run)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m bridge.scopes", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    migrate = commands.add_parser(
        "migrate-manual-root-to-none",
        help="rewrite unclaimed manual tasks that still have the legacy root scope to the explicit none scope")
    migrate.add_argument("--dry-run", action="store_true", help="report what would change and write nothing")
    args = parser.parse_args(argv)
    try:
        summary = migrate_manual_root_to_none(args.dry_run)
    except StoreError as error:
        print("scopes migration failed: " + error.code, file=sys.stderr)
        return 1
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
