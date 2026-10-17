#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Run a two-identity named-agent journey over MCP against a running stack and correlate the audit records.

A submitter assigns a task to a named agent principal; the agent claims it, comments in the task thread and submits a
result; the submitter, as the Space root, gates it. Baton executes nothing: the agent here is a second client identity.
"""
import argparse
import asyncio
import contextlib
import hashlib
import json
import ssl
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

REPO = Path(__file__).resolve().parents[1]
REMOVED_OPTIONS = {
    '--mode': 'there is no built-in worker and no demo or codex backend any more',
    '--token-file': 'the journey needs two identities, so pass --credentials-dir with one <name>.token file per identity',
}
REQUIRED_AUDIT = {'task.submitted', 'thread.submitted', 'task.assigned', 'task.claimed', 'task.result_submitted',
                  'gate.reviewed', 'task.completed', 'thread.completed'}
LEGACY_AUDIT_PREFIXES = ('worker.', 'task.dispatched')


def operation_id(key, identity, label):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f'baton-e2e/{key}/{identity}/{label}'))


def read_token(directory, name):
    token = (directory / (name + '.token')).read_text().strip()
    if not token:
        raise ValueError('Empty token file for ' + name)
    return token


def unwrap(payload):
    """x_ tools wrap their value in {x_protocol, result}; submit_task, get_task, list_tasks and fetch_inbox do not."""
    if isinstance(payload, dict) and payload.get('x_protocol') and 'result' in payload:
        return payload['result']
    return payload


def inbox_messages(inbox):
    messages = inbox['messages']
    return messages.get('result', []) if isinstance(messages, dict) else messages


class Agent:
    """One authenticated MCP session; every mutation carries a stable operation id so a retry replays."""

    def __init__(self, name, session, key):
        self.name, self.session, self.key = name, session, key

    async def call(self, tool, label=None, *, expect_error=None, **arguments):
        if label is not None:
            arguments['operation_id'] = operation_id(self.key, self.name, label)
        response = await self.session.call_tool(tool, arguments)
        text = response.content[0].text if response.content else ''
        if expect_error is not None:
            assert response.isError and text.startswith(expect_error), f'{self.name}: {tool} should fail with {expect_error}'
            return None
        assert not response.isError, f'{self.name}: MCP tool failed: {tool}'
        return unwrap(response.structuredContent or json.loads(text))


async def connect(stack, url, name, token, key, tls):
    http = await stack.enter_async_context(httpx.AsyncClient(
        headers={'Authorization': 'Bearer ' + token}, verify=tls, timeout=45, trust_env=False))
    read, write, _ = await stack.enter_async_context(streamable_http_client(url + '/mcp', http_client=http))
    session = await stack.enter_async_context(ClientSession(read, write))
    await session.initialize()
    return Agent(name, session, key)


async def poll(deadline, check, pause=1):
    while True:
        value = await check()
        if value or time.monotonic() >= deadline:
            return value
        await asyncio.sleep(pause)


def correlate(command, tokens, task_id):
    records = []
    for service in ('bridge', 'gateway', 'edge'):
        proc = subprocess.run(command + ['logs', '--no-color', '--no-log-prefix', service],
                              capture_output=True, text=True, check=True, timeout=30)
        raw = proc.stdout + proc.stderr
        assert not any(token in raw for token in tokens), 'Credential found in logs; contents suppressed'
        for line in raw.splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            record['_service'] = service
            records.append(record)
    stages = [r for r in records if r.get('task_id') == task_id]
    assert {'task.submitted', 'thread.submitted'} <= {r.get('event') for r in stages}, 'Missing bridge audit records'
    request_ids = {r['request_id'] for r in stages if r.get('request_id')}
    assert request_ids, 'MCP request ID not recorded'
    assert any(r['_service'] == 'gateway' and r.get('request_id') in request_ids and r.get('http.status') == 200 for r in records), \
        'Gateway submission correlation missing'
    assert any(r['_service'] == 'edge' and r.get('request_id') in request_ids for r in records), 'Ingress correlation missing'
    assert not any(str(r.get('event', '')).startswith(LEGACY_AUDIT_PREFIXES) for r in stages), 'Unexpected worker audit record'
    return len(request_ids)


async def run(args):
    if args.artifact_dir and args.artifact_dir.resolve().is_relative_to(REPO):
        raise ValueError('Acceptance artifacts must be stored outside the repository')
    if args.submitter == args.agent:
        raise ValueError('The submitter and the agent must be two different identities')
    url = args.url.rstrip('/')
    tokens = {name: read_token(args.credentials_dir, name) for name in (args.submitter, args.agent)}
    tls = ssl.create_default_context(cafile=str(args.ca_file)) if args.ca_file else True
    async with httpx.AsyncClient(verify=tls, timeout=30, trust_env=False) as http:
        denied = await http.get(url + '/healthz')
        assert denied.status_code in (401, 403), f'Expected authenticated Baton endpoint (401/403), got HTTP {denied.status_code}'
    deadline = time.monotonic() + args.timeout
    prompt = 'Calculate 17 times 23. Reply exactly: ' + (expected := 'BATON_E2E_OK 391')
    async with contextlib.AsyncExitStack() as stack:
        submitter = await connect(stack, url, args.submitter, tokens[args.submitter], args.key, tls)
        agent = await connect(stack, url, args.agent, tokens[args.agent], args.key, tls)
        space = await submitter.call('create_space', 'space', name='Baton e2e ' + args.key)
        page = await submitter.call('create_page', 'page', space_id=space['id'], title='Named-agent journey')
        await submitter.call('share', 'share-agent', resource_type='space', resource_id=space['id'],
                             subject_type='person', subject_id=args.agent_account or args.agent, role='edit')
        task = await submitter.call('submit_task', idempotency_key=args.key + '-task', prompt=prompt,
                                    page_id=page['id'], assignee_principal=args.agent)
        task_id = task['task_id']
        assert task['execution_mode'] == 'manual' and task['assignee'] == args.agent and task['state'] == 'queued'
        replay = await submitter.call('submit_task', idempotency_key=args.key + '-task', prompt=prompt,
                                      page_id=page['id'], assignee_principal=args.agent)
        assert replay['task_id'] == task_id and replay['replay'], 'Business key did not deduplicate the task'

        async def agent_sees_task():
            return any(m.get('thread_id') == task_id for m in inbox_messages(await agent.call('fetch_inbox')))
        assert await poll(deadline, agent_sees_task), 'The assigned task never reached the agent inbox'
        claim = await agent.call('claim', 'claim', task_id=task_id, lease_seconds=300)
        assert claim['state'] == 'claimed' and claim['holder'] == args.agent and claim['prompt'] == prompt
        comment = await agent.call('comment', 'comment', page_id=page['id'], thread_id=task_id, body='Working on it.')
        assert comment['thread_id'] == task_id
        candidate = await agent.call('submit_result', 'result', task_id=task_id, epoch=claim['epoch'], result=expected)
        assert candidate['state'] == 'awaiting_gate' and candidate['status'] != 'completed'
        await agent.call('gate', 'self-gate', expect_error='forbidden', task_id=task_id,
                         expected_epoch=candidate['result_epoch'], decision='pass')
        final = await submitter.call('gate', 'gate', task_id=task_id, expected_epoch=candidate['result_epoch'], decision='pass')
        assert final['state'] == 'done' and final['status'] == 'completed' and final['result'].strip() == expected
        assert final['gate']['root'] is True, 'The submitter must gate as the Space root'

        async def completed_report():
            return [m for m in inbox_messages(await submitter.call('fetch_inbox'))
                    if m.get('thread_id') == task_id and m.get('subject', '').startswith('[completed]')
                    and m.get('body_md', '').strip() == expected]
        reports = await poll(deadline, completed_report)
        assert reports, 'No same-thread completed comment report'

        events = {row['event'] for row in await submitter.call('list_audit', task_id=task_id)}
        assert REQUIRED_AUDIT <= events, 'Missing audit stages'
        assert not any(event.startswith(LEGACY_AUDIT_PREFIXES) for event in events), 'Unexpected worker audit event'

        open_task = await submitter.call('submit_task', idempotency_key=args.key + '-open', prompt='Unassigned follow-up',
                                         page_id=page['id'])
        assert open_task['assignee'] is None and open_task['execution_mode'] == 'manual'
        assert open_task['reviewers'] == [open_task['owner']] and open_task['scopes'] == [{'kind': 'none'}]
        assert open_task['task_id'] in {t['task_id'] for t in await agent.call('ready', page_id=page['id'])}, \
            'An unassigned task must appear in ready for a page editor'
    command = ['docker', 'compose', '-f', str(args.compose_file)]
    if args.project:
        command += ['-p', args.project]
    request_count = correlate(command, list(tokens.values()), task_id)
    receipt = {'status': 'PASS', 'flow': 'two-identity-named-agent', 'submitter': args.submitter, 'agent': args.agent,
               'task_id': task_id, 'thread_message_ids': [m['id'] for m in reports], 'idempotency_replay': True,
               'self_gate_rejected': True, 'gated_as_root': True, 'unassigned_task_listed_in_ready': True,
               'result_sha256': hashlib.sha256(final['result'].encode()).hexdigest(), 'audit_events': sorted(REQUIRED_AUDIT),
               'correlated_request_ids': request_count, 'credential_log_matches': 0}
    if args.artifact_dir:
        args.artifact_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        (args.artifact_dir / 'acceptance.json').write_text(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps(receipt))


def parse(argv):
    for option, reason in REMOVED_OPTIONS.items():
        if any(item == option or item.startswith(option + '=') for item in argv):
            raise SystemExit(f'e2e.py: {option} was removed: {reason}. See the "端到端验收" section of README.md.')
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--url', default='http://localhost:8080')
    p.add_argument('--credentials-dir', type=Path, required=True,
                   help='private directory holding <identity>.token files for the submitter and the agent')
    p.add_argument('--submitter', default='e2e-submitter', help='identity that submits, shares and gates; it becomes the Space root')
    p.add_argument('--agent', default='e2e-agent', help='identity that the task is assigned to')
    p.add_argument('--agent-account', help='connected account of the agent when it differs from its identity name')
    p.add_argument('--key', default='acceptance-v1')
    p.add_argument('--ca-file', type=Path)
    p.add_argument('--timeout', type=int, default=60)
    p.add_argument('--compose-file', type=Path, default=REPO / 'deploy/compose.yaml')
    p.add_argument('--project')
    p.add_argument('--artifact-dir', type=Path)
    return p.parse_args(argv)


if __name__ == '__main__':
    asyncio.run(run(parse(sys.argv[1:])))
