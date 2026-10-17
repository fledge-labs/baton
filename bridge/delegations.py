# SPDX-License-Identifier: Apache-2.0
"""Operator path for approver delegations and approval policy.

Run inside the bridge container, which already holds the database configuration:

    python -m bridge.delegations create --grantor <space owner principal> --grantor-account <owner account> \\
        [--grantor-kind human|agent] --delegate <principal> --delegate-account <account> --scope approve \\
        [--may-delegate] [--space <uuid>] [--expires <iso8601>] --reason <text> --operator <name>
    python -m bridge.delegations revoke --id <uuid> --operator <name>
    python -m bridge.delegations list [--active]
    python -m bridge.delegations policy-set --space <uuid> [--scope-kind path --scope-value <value>] \\
        --approvals 1|2 [--root-required] --operator <name>
    python -m bridge.delegations policy-remove --space <uuid> [--scope-kind ... --scope-value ...] --operator <name>
    python -m bridge.delegations policy-list [--space <uuid>]
    python -m bridge.delegations set-space-owner --space <uuid> --principal <p> --account <a> --kind <k> --operator <name>

`create` records the space owner's decision as executed by the named operator, for when the owner is not in
Baton. The grantor needs no Baton token but is bounded by the API's rules: it must be the exact identity
(principal, account and kind) recorded as the root of the named space, the delegate cannot be the owner, and a
delegation is always a root. `set-space-owner` records that identity for a space that has none (the owner could
not be backfilled from the audit); the account must be the space's existing owner account. The row keeps
created_via='operator' and created_by=<operator>; the audit event names the grantor as the requester and the
operator as the performer. `--may-delegate` lets the delegate grant narrower children through the API.
Output is JSON and never contains credentials or database settings.
"""

import argparse
import json
import sys

from bridge import common
from bridge.space_store import Actor, SpaceStore, StoreError


def _registered(principal, account, kind):
    """Reject a name the administrator's registry already knows as a different account or kind."""
    who = common.resolve_principal(principal)
    return who is None or (who["account"] == account and who["kind"] == kind)


def create(store, args):
    # The delegate must be a connected principal: a delegation to a name nobody holds yet could be
    # claimed by whoever registers it later. The grantor may be absent from the registry, but a name
    # the registry knows must be that principal.
    who = common.resolve_principal(args.delegate)
    if not who or who["account"] != args.delegate_account or who["kind"] not in {"human", "agent"}:
        raise StoreError("invalid")
    delegate = Actor(args.delegate, args.delegate_account, who["kind"])
    grantor = Actor(args.grantor, args.grantor_account, args.grantor_kind)
    if not _registered(grantor.principal, grantor.account, grantor.kind):
        raise StoreError("invalid")
    return store.operator_delegation_create(grantor, delegate, args.scope, args.space, args.expires, args.may_delegate,
                                            args.reason, args.operator)


def revoke(store, args):
    return store.operator_delegation_revoke(args.id, args.operator)


def listing(store, args):
    return store.operator_delegation_list(active_only=args.active)


def policy_set(store, args):
    return store.set_approval_policy(args.space, args.scope_kind, args.scope_value, args.approvals, args.root_required, args.operator)


def policy_remove(store, args):
    return store.remove_approval_policy(args.space, args.scope_kind, args.scope_value, args.operator)


def policy_list(store, args):
    return store.list_approval_policies(args.space)


def set_space_owner(store, args):
    if not _registered(args.principal, args.account, args.kind):
        raise StoreError("invalid")
    return store.set_space_owner(args.space, args.principal, args.account, args.kind, args.operator)


def parser():
    root = argparse.ArgumentParser(prog="python -m bridge.delegations", description=__doc__.split("\n\n")[0])
    commands = root.add_subparsers(dest="command", required=True)
    sub = commands.add_parser("create", help="record the owner as root grantor of an approver delegation")
    sub.add_argument("--grantor", required=True, help="principal recorded as the space's root (owner_principal)")
    sub.add_argument("--grantor-account", required=True, help="the space's owner account")
    sub.add_argument("--grantor-kind", choices=["human", "agent"], default="human")
    sub.add_argument("--delegate", required=True, help="connected principal (human or agent) that becomes an approver")
    sub.add_argument("--delegate-account", required=True)
    sub.add_argument("--scope", required=True, choices=["approve"])
    sub.add_argument("--may-delegate", action="store_true", help="let the delegate grant narrower approver delegations")
    sub.add_argument("--space", help="space UUID; omit to cover all spaces")
    sub.add_argument("--expires", help="ISO 8601 instant with a UTC offset; omit for no expiry")
    sub.add_argument("--reason", required=True, help="up to 500 characters, for example the DECISIONS reference")
    sub.add_argument("--operator", required=True, help="name of the person running this command")
    sub.set_defaults(run=create)
    sub = commands.add_parser("revoke", help="revoke a delegation")
    sub.add_argument("--id", required=True)
    sub.add_argument("--operator", required=True)
    sub.set_defaults(run=revoke)
    sub = commands.add_parser("list", help="list delegations")
    sub.add_argument("--active", action="store_true", help="only delegations that are neither revoked nor expired")
    sub.set_defaults(run=listing)
    for name, run, approvals in (("policy-set", policy_set, True), ("policy-remove", policy_remove, False)):
        sub = commands.add_parser(name, help="set or remove two-party confirmation for a scope" if approvals else "remove a policy")
        sub.add_argument("--space", required=True)
        sub.add_argument("--scope-kind", choices=["path", "module", "migration"], default="path")
        sub.add_argument("--scope-value", default="", help="empty (default) with path covers the whole space")
        if approvals:
            sub.add_argument("--approvals", type=int, required=True, choices=[1, 2])
            sub.add_argument("--root-required", action="store_true", help="the space owner must be one of the approvers")
        sub.add_argument("--operator", required=True)
        sub.set_defaults(run=run)
    sub = commands.add_parser("set-space-owner", help="record the exact root approver identity of a space")
    sub.add_argument("--space", required=True)
    sub.add_argument("--principal", required=True)
    sub.add_argument("--account", required=True, help="must be the space's existing owner account")
    sub.add_argument("--kind", required=True, choices=["human", "agent"])
    sub.add_argument("--operator", required=True)
    sub.set_defaults(run=set_space_owner)
    sub = commands.add_parser("policy-list", help="list approval policies")
    sub.add_argument("--space")
    sub.set_defaults(run=policy_list)
    return root


def main(argv=None, store=None):
    args = parser().parse_args(argv)
    try:
        result = args.run(store or SpaceStore.from_env(), args)
    except StoreError as exc:
        print(json.dumps({"error": exc.code, "message": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
