#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Verify a real MCP/A2A/Mail journey and correlate container audit records."""
import argparse
import asyncio
import hashlib
import json
import os
import ssl
import subprocess
import time
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

REPO=Path(__file__).resolve().parents[1]

async def run(args):
    token=args.token_file.read_text().strip()
    if not token:raise ValueError('Empty token file')
    if args.artifact_dir and args.artifact_dir.resolve().is_relative_to(REPO):
        raise ValueError('Acceptance artifacts must be stored outside the repository')
    tls=ssl.create_default_context(cafile=str(args.ca_file)) if args.ca_file else True
    async with httpx.AsyncClient(verify=tls,timeout=30,trust_env=False) as http:
        denied=await http.get(args.url+'/healthz')
        assert denied.status_code in (401,403),'Missing token was accepted'
    async with httpx.AsyncClient(headers={'Authorization':'Bearer '+token},verify=tls,timeout=45,trust_env=False) as http:
        async with streamable_http_client(args.url+'/mcp',http_client=http) as (read,write,_):
            async with ClientSession(read,write) as session:
                await session.initialize()
                async def call(name,params):
                    response=await session.call_tool(name,params)
                    assert not response.isError,'MCP tool failed: '+name
                    return response.structuredContent or json.loads(response.content[0].text)
                expected='BATON_DEMO_OK' if args.mode=='demo' else 'BATON_CODEX_OK 391'
                prompt='Calculate 17 times 23. Reply exactly: '+expected
                task=await call('submit_task',{'idempotency_key':args.key,'prompt':prompt})
                tid=task['task_id']
                replay=await call('submit_task',{'idempotency_key':args.key,'prompt':prompt})
                assert replay['task_id']==tid and replay['replay']
                deadline=time.monotonic()+args.timeout
                while time.monotonic()<deadline:
                    result=await call('get_task',{'task_id':tid})
                    if result['status'] in ('completed','failed','interrupted'):break
                    await asyncio.sleep(2)
                assert result['status']=='completed','Task did not complete: '+result['status']
                assert result['result'].strip()==expected,'Unexpected result'
                matches=[]
                while time.monotonic()<deadline:
                    inbox=await call('fetch_inbox',{})
                    messages=inbox['messages']
                    if isinstance(messages,dict):messages=messages.get('result',[])
                    matches=[m for m in messages if m.get('thread_id')==tid and m.get('subject','').startswith('[completed]') and m.get('body_md','').strip()==expected]
                    if matches:break
                    await asyncio.sleep(1)
                assert matches,'No same-thread completed Mail report'
    command=['docker','compose','-f',str(args.compose_file)]
    if args.project:command+=['-p',args.project]
    records=[]
    for service in ('bridge','codex','gateway','edge'):
        proc=subprocess.run(command+['logs','--no-color','--no-log-prefix',service],capture_output=True,text=True,check=True,timeout=30)
        raw=proc.stdout+proc.stderr
        assert token not in raw,'Credential found in logs; contents suppressed'
        for line in raw.splitlines():
            try:record=json.loads(line)
            except ValueError:continue
            record['_service']=service;records.append(record)
    stages=[r for r in records if r.get('task_id')==tid]
    required={'task.submitted','task.dispatched','task.completed','mail.submitted','mail.completed','worker.claimed','worker.completed'}
    assert required <= {r.get('event') for r in stages},'Missing audit stages'
    claims=[r for r in stages if r.get('event')=='worker.claimed']
    assert len(claims)==1,'Task executed more than once'
    assert claims[0].get('backend')==args.mode,'Worker mode differs from claimed test mode'
    request_ids={r['request_id'] for r in stages if r.get('request_id')}
    assert request_ids,'MCP request ID not recorded'
    assert any(r.get('_service')=='gateway' and r.get('request_id') in request_ids and r.get('http.status')==200 for r in records),'Gateway submission correlation missing'
    assert any(r.get('_service')=='gateway' and r.get('request_id')==tid and r.get('protocol')=='a2a' and r.get('a2a.task.state')=='completed' for r in records),'A2A gateway correlation missing'
    assert any(r.get('_service')=='edge' and r.get('request_id') in request_ids for r in records),'Ingress correlation missing'
    receipt={'status':'PASS','mode':args.mode,'task_id':tid,'a2a_task_id':result['a2a_id'],
             'mail_message_ids':[m['id'] for m in matches],'idempotency_replay':True,'execution_count':1,
             'result_sha256':hashlib.sha256(result['result'].encode()).hexdigest(),
             'audit_stages':sorted(required),'credential_log_matches':0}
    if args.artifact_dir:
        args.artifact_dir.mkdir(parents=True,exist_ok=True,mode=0o700)
        (args.artifact_dir/'acceptance.json').write_text(json.dumps(receipt,indent=2)+'\n')
    print(json.dumps(receipt))

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--url',default='http://localhost:8080');p.add_argument('--token-file',type=Path,required=True)
    p.add_argument('--mode',choices=['codex','demo'],default='codex');p.add_argument('--key',default='acceptance-v1')
    p.add_argument('--ca-file',type=Path);p.add_argument('--timeout',type=int,default=360)
    p.add_argument('--compose-file',type=Path,default=REPO/'deploy/compose.yaml');p.add_argument('--project')
    p.add_argument('--artifact-dir',type=Path)
    asyncio.run(run(p.parse_args()))
