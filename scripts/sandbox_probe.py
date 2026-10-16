#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Prove the worker's actual Linux tool sandbox, using only synthetic canaries."""
import argparse
import subprocess
from pathlib import Path

PROGRAM=r'''
import json,os,socket,subprocess,tempfile
from pathlib import Path
from bridge.codex_agent import codex_command,codex_environment
paths=['/data/.baton-sandbox-canary','/home/agent/.codex/.baton-sandbox-canary']
try:
 for path in paths:Path(path).write_text('synthetic-canary')
 with tempfile.TemporaryDirectory(prefix='sandbox-probe-',dir='/work') as work:
  wd=Path(work);(wd/'.tmp').mkdir()
  permissions=next(a for a in codex_command(wd,wd/'result.txt') if a.startswith('permissions.baton='))
  address=socket.gethostbyname('mail')
  probe="""import pathlib,socket,json
out={}
for path in %r:
 try:pathlib.Path(path).read_bytes();out[path]='READ_ALLOWED'
 except (PermissionError,FileNotFoundError):out[path]='DENIED'
pathlib.Path('probe-output').write_text('ok');out['workspace_write']='PASS'
try:socket.create_connection((%r,8765),timeout=2);out['tool_network']='ALLOWED'
except OSError:out['tool_network']='DENIED'
print(json.dumps(out))
""" % (paths+['/config/identities.json'],address)
  result=subprocess.run(['codex','sandbox','-P','baton','-C',work,'-c',permissions,'--','python','-c',probe],capture_output=True,text=True,env=codex_environment(wd),timeout=30)
  assert result.returncode==0,'Sandbox could not start; no bypass is permitted'
  outcomes=json.loads(result.stdout)
  assert outcomes['workspace_write']=='PASS'
  assert all(v=='DENIED' for k,v in outcomes.items() if k!='workspace_write')
  print(json.dumps({'status':'PASS','credential_and_state_reads':'DENIED','workspace_write':'PASS','tool_network':'DENIED'}))
finally:
 for path in paths:Path(path).unlink(missing_ok=True)
'''

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--compose-file',type=Path,default=Path(__file__).resolve().parents[1]/'deploy/compose.yaml')
    p.add_argument('--project')
    a=p.parse_args();cmd=['docker','compose','-f',str(a.compose_file)]
    if a.project:cmd+=['-p',a.project]
    result=subprocess.run(cmd+['exec','-T','codex','python','-'],input=PROGRAM,text=True,capture_output=True,timeout=45)
    if result.returncode:raise SystemExit('Sandbox probe failed; no secret values or raw runtime errors displayed')
    print(result.stdout.strip())
