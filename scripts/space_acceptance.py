#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Exercise Baton Space through its authenticated HTTP API, using disposable identities."""
import argparse
import concurrent.futures
import json
import time
from pathlib import Path

from space_client import Client, ROOT


def run(args):
    if args.artifact_dir and args.artifact_dir.resolve().is_relative_to(ROOT):
        raise ValueError('Acceptance artifacts must stay outside the repository')
    owner, peer, reviewer, agent = [Client(args, name) for name in
        ('space-owner', 'space-peer', 'space-reviewer', 'space-agent')]
    passed = []
    space = owner.call('create_space', 'space', name='Space acceptance ' + args.key)
    sid = space['id']
    page = owner.call('create_page', 'parent', space_id=sid, title='Parent')
    child = owner.call('create_page', 'child', space_id=sid, parent_id=page['id'], title='Child')
    pid = child['id']
    block = owner.call('create_block', 'block', page_id=pid, kind='text', content={'text': 'original'})
    bid = block['id']

    def share(label, role, subject='space-peer', resource_type='space', resource_id=sid, subject_type='person'):
        return owner.call('share', label, resource_type=resource_type, resource_id=resource_id,
            subject_type=subject_type, subject_id=subject, role=role)

    share('view', 'view')
    peer.call('get_page', page_id=pid)
    peer.call('comment', 'view-comment', expected=403, page_id=pid, body='must be rejected')
    peer.call('update_block', 'view-edit', expected=403, block_id=bid,
        expected_revision=1, content={'text': 'must be rejected'})
    share('comment', 'comment')
    peer.call('comment', 'allowed-comment', page_id=pid, body='feedback')
    peer.call('update_block', 'comment-edit', expected=403, block_id=bid,
        expected_revision=1, content={'text': 'must be rejected'})
    share('direct-edit', 'edit', resource_type='page', resource_id=pid)
    peer.call('update_block', 'allowed-edit', block_id=bid, expected_revision=1, content={'text': 'peer edit'})
    peer.call('share', 'editor-sharing', expected=403, resource_type='page', resource_id=pid,
        subject_type='person', subject_id='space-reviewer', role='edit')
    share('remove-direct', None, resource_type='page', resource_id=pid)
    assert peer.call('effective_role', resource_type='page', resource_id=pid)['role'] == 'comment'
    share('remove-inherited', None)
    peer.call('get_page', expected=404, page_id=pid)
    peer.call('comment', 'allowed-comment', expected=404, page_id=pid, body='feedback')
    passed.append('roles_inheritance_revocation_and_replay')

    team = owner.call('create_team', 'team', name='Acceptance team')
    owner.call('set_team_member', 'team-add', team_id=team['id'], account='space-peer', member=True)
    share('team-edit', 'edit', subject=team['id'], subject_type='team')
    assert peer.call('effective_role', resource_type='page', resource_id=pid)['role'] == 'edit'
    owner.call('set_team_member', 'team-remove', team_id=team['id'], account='space-peer', member=False)
    peer.call('get_page', expected=404, page_id=pid)
    share('peer-edit', 'edit')
    share('reviewer-edit', 'edit', subject='space-reviewer')
    passed.append('team_membership_is_live')

    # space-agent was issued by the administrator with account=space-owner.
    authored = agent.call('create_block', 'agent-block', page_id=pid, kind='text', content={'text': 'agent draft'})
    assert authored['author_principal'] == 'space-agent' and authored['author_account'] == 'space-owner'
    assert authored['author_kind'] == 'agent'
    agent.call('create_block', 'spoof-author', expected=422, page_id=pid, kind='text',
        content={'text': 'rejected'}, actor={'principal': 'space-owner'})
    proposal = agent.call('proposal_create', 'proposal', block_id=authored['id'],
        expected_revision=1, proposed_content={'text': 'proposed revision'})
    agent.call('proposal_review', 'agent-accept', expected=403, proposal_id=proposal['id'], decision='accept')
    owner.call('proposal_review', 'human-accept', proposal_id=proposal['id'], decision='accept')
    changed = owner.call('get_block', block_id=authored['id'])
    assert changed['revision'] == 2 and changed['content'] == {'text': 'proposed revision'}
    owner.call('update_block', 'stale-version', expected=409, block_id=authored['id'],
        expected_revision=1, content={'text': 'must not overwrite'})
    passed.append('connected_account_attribution_proposal_and_version_fence')

    comment = owner.call('comment', 'request-comment', page_id=pid, body='Please reply in this thread.')
    owner.call('mention', 'request-mention', comment_id=comment['id'], target_type='agent', target_id='space-peer')
    reviewer.call('mention', 'reviewer-request', comment_id=comment['id'], target_type='agent', target_id='space-peer')
    inbox = peer.call('fetch_inbox')['messages']
    request = next(m for m in inbox if m['thread_id'] == comment['thread_id'])
    assert request['action_requested'] is True
    assert {'space-owner', 'space-reviewer'} <= {item['requested_by'] for item in request['requests']}
    reply = peer.call('comment', 'agent-reply', page_id=pid, thread_id=comment['thread_id'], body='Agent reply.')
    assert reply['thread_id'] == comment['thread_id'] and reply['author_principal'] == 'space-peer'
    share('revoke-inbox', None)
    assert not any(m['thread_id'] == comment['thread_id'] for m in peer.call('fetch_inbox')['messages'])
    share('restore-peer', 'edit')
    passed.append('agent_mentions_thread_reply_and_inbox_access')

    def task(label, scope, page_id=pid):
        return owner.call('create_task_block', label, page_id=page_id, prompt='Synthetic coordination check',
            scopes=[{'kind': 'path', 'value': scope}], reviewers=['space-reviewer'],
            base_commit='a' * 40, context=['original context'], evidence=['https://example.com/original'])

    # Separate spaces must not partition the same repository lock domain.
    other = owner.call('create_space', 'other-space', name='Other acceptance space')
    other_page = owner.call('create_page', 'other-page', space_id=other['id'], title='Other work')
    share('other-share', 'edit', resource_id=other['id'])
    first = task('overlap-first', 'src/acceptance')
    second = task('overlap-second', 'src/acceptance/file.py', page_id=other_page['id'])
    def race(client, label, tid):
        return client.call('claim', label, expected=(200, 409), task_id=tid, lease_seconds=2)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(race, owner, 'race-owner', first['id'])
        b = pool.submit(race, peer, 'race-peer', second['id'])
        outcomes = [(owner, first, a.result()), (peer, second, b.result())]
    winners = [entry for entry in outcomes if 'http_status' not in entry[2]]
    assert len(winners) == 1, 'Overlapping concurrent claims both succeeded or both failed'
    holder, previous, claim = winners[0]
    passed.append('concurrent_cross_space_overlap_rejected')
    time.sleep(2.2)
    released = owner.call('get_task', task_id=previous['id'])
    assert not released['lease_valid'] and released['state'] == 'queued'
    holder.call('renew', 'expired-renew', expected=409, task_id=previous['id'], epoch=claim['epoch'], lease_seconds=30)
    holder.call('submit_result', 'expired-result', expected=409, task_id=previous['id'], epoch=claim['epoch'], result='stale')
    other_holder = 'space-owner' if holder.name == 'space-peer' else 'space-peer'
    holder.call('handoff', 'expired-handoff', expected=409, task_id=previous['id'], epoch=claim['epoch'], target_principal=other_holder)
    renewed = owner.call('claim', 'reclaim', task_id=first['id'], lease_seconds=30)
    assert renewed['epoch'] > 0 and renewed['lease_valid']
    if previous['id'] == first['id']:
        assert renewed['epoch'] > claim['epoch']
    passed.append('expired_lease_releases_scope_and_fences_stale_writes')

    handed = owner.call('handoff', 'handoff', task_id=first['id'], epoch=renewed['epoch'],
        target_principal='space-peer', context=['handoff context'], evidence=['https://example.com/handoff'], lease_seconds=30)
    assert handed['base_commit'] == 'a' * 40 and handed['thread_id'] == first['thread_id']
    assert handed['context'] == ['original context', 'handoff context']
    assert handed['evidence'] == ['https://example.com/original', 'https://example.com/handoff']
    assert handed['holder'] == 'space-peer' and handed['epoch'] > renewed['epoch']
    owner.call('renew', 'old-holder-renew', expected=409, task_id=first['id'], epoch=renewed['epoch'])
    candidate = peer.call('submit_result', 'candidate', task_id=first['id'], epoch=handed['epoch'],
        result='Synthetic reviewed result', evidence=['https://example.com/result'])
    assert candidate['state'] == 'awaiting_gate' and candidate['status'] != 'completed'
    peer.call('gate', 'self-approve', expected=403, task_id=first['id'], expected_epoch=candidate['result_epoch'], decision='pass')
    final = reviewer.call('gate', 'review-pass', task_id=first['id'],
        expected_epoch=candidate['result_epoch'], decision='pass', evidence=['https://example.com/review'])
    assert final['state'] == 'done' and final['status'] == 'completed'
    assert final['base_commit'] == 'a' * 40
    audit = owner.call('list_audit', task_id=first['id'])
    assert audit and all(item['task_id'] == first['id'] for item in audit)
    encoded = json.dumps(audit)
    assert 'Synthetic reviewed result' not in encoded and 'original context' not in encoded
    assert all(client.token not in encoded for client in (owner, peer, reviewer, agent))
    passed.append('handoff_preserves_baseline_context_and_evidence')
    passed.append('reviewer_gate_controls_completion_and_audit_is_metadata_only')

    upstream = task('graph-upstream', 'acceptance/graph/upstream')
    downstream = task('graph-downstream', 'acceptance/graph/downstream')
    owner.call('plan', 'graph', graph=[{'task_id': downstream['id'], 'depends_on': [upstream['id']]}])
    owner.call('claim', 'blocked-dependency', expected=409, task_id=downstream['id'], lease_seconds=30)
    assert downstream['id'] not in {item['task_id'] for item in owner.call('ready', page_id=pid)}
    # A cycle is rejected atomically; it must not poison the already valid graph.
    owner.call('plan', 'cycle', expected=409, graph=[{'task_id': upstream['id'], 'depends_on': [downstream['id']]}])
    active = owner.call('claim', 'graph-claim', task_id=upstream['id'], lease_seconds=30)
    pending = owner.call('submit_result', 'graph-result', task_id=upstream['id'], epoch=active['epoch'], result='Upstream candidate')
    assert pending['state'] == 'awaiting_gate'
    assert downstream['id'] not in {item['task_id'] for item in owner.call('ready', page_id=pid)}
    reviewer.call('gate', 'graph-gate', task_id=upstream['id'], expected_epoch=pending['result_epoch'], decision='pass')
    assert downstream['id'] in {item['task_id'] for item in owner.call('ready', page_id=pid)}
    active = owner.call('claim', 'downstream-claim', task_id=downstream['id'], lease_seconds=30)
    pending = owner.call('submit_result', 'downstream-result', task_id=downstream['id'], epoch=active['epoch'], result='Downstream candidate')
    reviewer.call('gate', 'downstream-gate', task_id=downstream['id'], expected_epoch=pending['result_epoch'], decision='pass')
    passed.append('dependency_gate_blocks_claim_until_ready_and_cycles_rollback')

    private_space = owner.call('create_space', 'private-dependency-space', name='Private dependency')
    private_page = owner.call('create_page', 'private-dependency-page', space_id=private_space['id'], title='Private dependency')
    share('private-reviewer', 'edit', subject='space-reviewer', resource_type='page', resource_id=private_page['id'])
    share('private-peer', 'view', resource_type='page', resource_id=private_page['id'])
    private_parent = task('private-parent', 'acceptance/private', page_id=private_page['id'])
    active = owner.call('claim', 'private-parent-claim', task_id=private_parent['id'], lease_seconds=30)
    pending = owner.call('submit_result', 'private-parent-result', task_id=private_parent['id'], epoch=active['epoch'], result='Private candidate')
    reviewer.call('gate', 'private-parent-gate', task_id=private_parent['id'], expected_epoch=pending['result_epoch'], decision='pass')
    public_child = owner.call('create_task_block', 'public-dependent', page_id=pid, prompt='Visible dependent task',
        scopes=[{'kind': 'path', 'value': 'acceptance/public'}], reviewers=['space-reviewer'], depends_on=[private_parent['id']])
    assert public_child['id'] in {item['task_id'] for item in peer.call('ready', page_id=pid)}
    share('private-peer-revoke', None, resource_type='page', resource_id=private_page['id'])
    hidden = peer.call('get_task', task_id=public_child['id'])
    assert private_parent['id'] not in json.dumps(hidden)
    assert public_child['id'] not in {item['task_id'] for item in peer.call('ready', page_id=pid)}
    peer.call('claim', 'hidden-dependency-claim', expected=(403, 409), task_id=public_child['id'])
    peer.call('plan', 'hidden-dependency-plan', expected=(403, 404), graph=[{'task_id': public_child['id'], 'depends_on': []}])
    passed.append('dependency_access_is_rechecked_and_hidden_ids_are_not_disclosed')

    retried = task('previously-executed', 'acceptance/rejected')
    active = owner.call('claim', 'rejected-claim', task_id=retried['id'], lease_seconds=30)
    pending = owner.call('submit_result', 'rejected-result', task_id=retried['id'], epoch=active['epoch'], result='Rejected candidate')
    reviewer.call('gate', 'rejected-gate', task_id=retried['id'], expected_epoch=pending['result_epoch'], decision='fail')
    owner.call('plan', 'rejected-replan', expected=409, graph=[{'task_id': retried['id'], 'depends_on': [upstream['id']]}])
    expired = task('expired-for-plan', 'acceptance/expired-plan')
    owner.call('claim', 'expired-plan-claim', task_id=expired['id'], lease_seconds=1)
    time.sleep(1.2)
    owner.call('plan', 'expired-replan', expected=409, graph=[{'task_id': expired['id'], 'depends_on': [upstream['id']]}])
    passed.append('plan_cannot_rewrite_dependencies_after_execution_or_claim_expiry')
    receipt = {'status': 'PASS', 'x_protocol': 'baton.space.v1', 'checks': passed}
    if args.artifact_dir:
        args.artifact_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        (args.artifact_dir / 'space-acceptance.json').write_text(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps(receipt))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://localhost:8080')
    parser.add_argument('--credentials-dir', type=Path, required=True)
    parser.add_argument('--ca-file', type=Path)
    parser.add_argument('--key', required=True, help='Stable key for this disposable acceptance run')
    parser.add_argument('--artifact-dir', type=Path)
    run(parser.parse_args())
