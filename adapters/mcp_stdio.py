#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Token-file MCP adapter; optional receipts contain validated metadata only."""

import argparse
import asyncio
import json
import logging
import os
import re
import ssl
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

TOOLS = {"submit_task", "get_task", "list_tasks", "fetch_inbox", "create_space", "get_space", "list_spaces",
         "create_page", "get_page", "list_pages", "create_block", "get_block", "list_blocks", "update_block",
         "revert_block", "share", "effective_role", "create_team", "set_team_member", "comment", "list_comments",
         "mention", "proposal_create", "proposal_review", "get_proposal", "list_proposals", "list_attribution",
         "create_task_block", "assign_task", "plan", "ready", "claim", "renew", "submit_result", "handoff", "gate", "get_claim", "list_claims",
         "get_scopes", "list_audit", "x_claim", "x_scope", "x_handoff", "x_gate", "x_audit"}
STATUSES = {"queued", "working", "completed", "failed", "interrupted", "claimed", "awaiting_gate", "done"}


def safe_agent(value):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", value):
        raise argparse.ArgumentTypeError("Invalid agent identifier")
    return value


def valid_uuid(value):
    try:
        return str(uuid.UUID(value)) if isinstance(value, str) and str(uuid.UUID(value)) == value.lower() else None
    except ValueError:
        return None


def receipt(agent, name, arguments, result):
    row = {"time": datetime.now(timezone.utc).isoformat(), "agent": safe_agent(agent),
           "tool": name if name in TOOLS else "unknown", "is_error": bool(result.isError)}
    data = result.structuredContent
    if data is None:
        try:
            data = json.loads(next(item.text for item in result.content if item.type == "text"))
        except (ValueError, StopIteration):
            data = {}
    if isinstance(data, dict) and data.get("x_protocol") == "baton.space.v1":
        data = data.get("result", {})
    if isinstance(data, dict):
        for key in ("task_id", "a2a_id", "thread_id", "page_id", "block_id"):
            value = valid_uuid(data.get(key))
            if value:
                row[key] = value
        if isinstance(data.get("status"), str) and data["status"] in STATUSES:
            row["status"] = data["status"]
        if type(data.get("replay")) is bool:
            row["replay"] = data["replay"]
    if name == "get_task" and isinstance(arguments, dict):
        task_id = valid_uuid(arguments.get("task_id"))
        if task_id:
            row["task_id"] = task_id
    if name == "fetch_inbox" and isinstance(data, dict):
        messages = data.get("messages", [])
        if isinstance(messages, dict):
            messages = messages.get("result", [])
        if isinstance(messages, list):
            row["thread_ids"] = [thread for item in messages if isinstance(item, dict)
                                      if (thread := valid_uuid(item.get("thread_id")))]
    return row


def write_receipt(path, row):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, (json.dumps(row) + "\n").encode())
    finally:
        os.close(fd)


async def main(args):
    # Libraries can include request URLs or response payloads in debug/error logs.
    logging.disable(logging.CRITICAL)
    token = args.token_file.read_text().strip()
    if not token or any(character.isspace() for character in token):
        raise ValueError("Invalid token file")
    parsed = urlsplit(args.url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Invalid Baton URL")
    context = ssl.create_default_context(cafile=str(args.ca_file) if args.ca_file else None)
    async with httpx.AsyncClient(headers={"Authorization": "Bearer " + token}, verify=context, timeout=60) as http:
        async with streamable_http_client(args.url, http_client=http) as (read, write, _):
            async with ClientSession(read, write) as remote:
                await remote.initialize()
                server = Server("baton-proxy")

                @server.list_tools()
                async def tools():
                    return (await remote.list_tools()).tools

                @server.call_tool()
                async def call(name, arguments):
                    try:
                        result = await remote.call_tool(name, arguments)
                    except Exception:
                        raise RuntimeError("Baton remote tool failed") from None
                    if args.audit_file:
                        write_receipt(args.audit_file, receipt(args.agent_id, name, arguments, result))
                    return result

                async with stdio_server() as (stdio_read, stdio_write):
                    await server.run(stdio_read, stdio_write, server.create_initialization_options())


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent-id", required=True, type=safe_agent)
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--url", default=os.environ.get("BATON_URL", "http://localhost:8080/mcp"))
    parser.add_argument("--ca-file", type=Path, default=os.environ.get("BATON_CA_FILE") or None)
    parser.add_argument("--audit-file", type=Path, default=os.environ.get("BATON_AUDIT_FILE") or None)
    return parser.parse_args()


if __name__ == "__main__":
    try:
        asyncio.run(main(parse_args()))
    except KeyboardInterrupt:
        pass
    except Exception:
        print("Baton adapter stopped; check connectivity and token-file configuration.", file=sys.stderr)
        raise SystemExit(1) from None
