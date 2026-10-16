#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Local named-agent protocol check; this does not attest remote CLI execution."""
import argparse
import json
from pathlib import Path

from space_acceptance import Client, ROOT


def run(args):
    if args.artifact_dir and args.artifact_dir.resolve().is_relative_to(ROOT):
        raise ValueError('Acceptance artifacts must stay outside the repository')
    sender, recipient, other = [Client(args, identity) for identity in ('nangua', 'beigua', 'bystander')]
    space = sender.call('create_space', 'space', name='Named-agent acceptance ' + args.key)
    page = sender.call('create_page', 'page', space_id=space['id'], title='Pull-based assignment')
    for identity in ('beigua', 'bystander'):
        sender.call('share', 'share-' + identity, resource_type='page', resource_id=page['id'],
            subject_type='person', subject_id=identity, role='edit')
    task = sender.call('submit_task', 'assign', idempotency_key=args.key + '-task',
        prompt='Calculate 17 times 23 and report in this task thread.', page_id=page['id'],
        assignee_principal='beigua', scopes=[{'kind': 'path', 'value': 'acceptance/named-agent'}])
    tid = task['task_id']
    assert task['assignee'] == 'beigua' and task['execution_mode'] == 'manual'
    assert task['state'] == 'queued' and not task['holder']
    other.call('claim', 'unauthorized-recipient', expected=403, task_id=tid, lease_seconds=60)
    inbox = recipient.call('fetch_inbox')['messages']
    assert any(message['thread_id'] == tid for message in inbox)
    claimed = recipient.call('claim', 'claim', task_id=tid, lease_seconds=60)
    assert claimed['holder'] == 'beigua' and claimed['assignee'] == 'beigua'
    result = 'BEIGUA_PROTOCOL_OK ' + str(17 * 23)
    reply = recipient.call('comment', 'reply', page_id=page['id'], thread_id=tid, body=result)
    assert reply['thread_id'] == tid and reply['author_principal'] == 'beigua'
    candidate = recipient.call('submit_result', 'result', task_id=tid, epoch=claimed['epoch'], result=result)
    assert candidate['state'] == 'awaiting_gate'
    recipient.call('gate', 'self-gate', expected=403, task_id=tid,
        expected_epoch=candidate['result_epoch'], decision='pass')
    final = sender.call('gate', 'gate', task_id=tid, expected_epoch=candidate['result_epoch'], decision='pass')
    assert final['state'] == 'done' and final['result'] == result
    assert any(message['thread_id'] == tid and message['body_md'].strip() == result
        for message in sender.call('fetch_inbox')['messages'])
    events = sender.call('list_audit', task_id=tid)
    assert events and all(event['task_id'] == tid for event in events)
    names = {event['event'] for event in events}
    required = {'task.assigned', 'task.claimed', 'task.result_submitted', 'task.completed'}
    assert required <= names, 'Missing assignment, claim, result or gate-completion audit event'
    assert any(event['event'] == 'task.assigned' and event['agent'] == 'nangua' for event in events)
    assert any(event['event'] == 'task.claimed' and event['agent'] == 'beigua' for event in events)
    assert any(event['event'] == 'task.result_submitted' and event['agent'] == 'beigua' for event in events)
    encoded = json.dumps(events)
    assert result not in encoded and all(c.token not in encoded for c in (sender, recipient, other))
    receipt = {'status': 'PASS', 'scope': 'local_authenticated_protocol', 'remote_cli_acceptance': 'NOT_RUN',
        'task_id': tid, 'assignee': 'beigua', 'holder': 'beigua', 'thread_id': tid,
        'unassigned_claim_rejected': True, 'self_gate_rejected': True, 'audit_events': sorted(required)}
    if args.artifact_dir:
        args.artifact_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        (args.artifact_dir / 'assignment-acceptance.json').write_text(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps(receipt))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://localhost:8080')
    parser.add_argument('--credentials-dir', type=Path, required=True)
    parser.add_argument('--ca-file', type=Path)
    parser.add_argument('--key', required=True)
    parser.add_argument('--artifact-dir', type=Path)
    run(parser.parse_args())
