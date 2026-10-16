#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Administer Baton credentials as files; never emit their values or digests."""
import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
import tempfile
from pathlib import Path

SERVICE_UID=10001
SERVICE_GID=10001

def atomic_write(path, data, mode=0o600, owner=None):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    previous=path.stat() if path.exists() else None
    fd,tmp=tempfile.mkstemp(prefix='.baton-',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as f:
            f.write(data);f.flush();os.fsync(f.fileno())
        os.chmod(tmp,mode)
        if os.geteuid()==0:
            if owner:os.chown(tmp,*owner)
            elif previous:os.chown(tmp,previous.st_uid,previous.st_gid)
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)

def secret_path(root,agent,role):
    return root/'secrets/bridge/dispatcher.token' if role=='dispatcher' else root/'secrets/clients'/f'{agent}.token'

def provision(root,identities,agent,role):
    token=secrets.token_urlsafe(48)
    identities[agent]={'agent':agent,'role':role,'hash':hashlib.sha256(token.encode()).hexdigest(),'active':True}
    owner=(SERVICE_UID,SERVICE_GID) if role=='dispatcher' else None
    atomic_write(secret_path(root,agent,role),token+'\n',owner=owner)

def render_gateway(identities):
    template=Path(__file__).resolve().parents[1]/'deploy/gateway.template.json'
    config=json.loads(template.read_text());config.pop('_license',None)
    keys=[{'keyHash':'sha256:'+v['hash'],'metadata':{'agent':v['agent'],'role':v['role']}} for v in identities.values() if v['active']]
    for bind in config['binds']:
        for listener in bind['listeners']:
            for route in listener['routes']:route['policies']['apiKey']['keys']=keys
    return config

def initialize(root,identities):
    for name in ['config','secrets/clients','secrets/bridge','secrets/mail','secrets/codex-home','data/bridge','data/codex','data/mail']:
        p=root/name;p.mkdir(parents=True,exist_ok=True,mode=0o700)
        if name=='config':os.chmod(p,0o755)
        elif os.geteuid()==0 and name!='secrets/clients':
            os.chown(p,SERVICE_UID,SERVICE_GID);os.chmod(p,0o700)
    mail=root/'secrets/mail/mail.token'
    if not mail.exists():atomic_write(mail,secrets.token_urlsafe(48)+'\n',owner=(SERVICE_UID,SERVICE_GID))
    atomic_write(root/'secrets/bridge/mail.token',mail.read_text(),owner=(SERVICE_UID,SERVICE_GID))
    worker='codex-worker'
    if worker not in identities:provision(root,identities,worker,'dispatcher')
    elif not secret_path(root,worker,'dispatcher').exists():
        raise ValueError('Dispatcher credential missing; restore it or explicitly rotate the dispatcher identity')

def deliver(root,identities,agent,path,uid,gid):
    entry=identities[agent]
    if entry['role']!='client':raise ValueError('Internal dispatcher credentials cannot be exported as client credentials')
    if not entry['active']:raise ValueError('Cannot export a revoked identity')
    raw=secret_path(root,agent,entry['role']).read_text()
    if hashlib.sha256(raw.strip().encode()).hexdigest()!=entry['hash']:raise ValueError('Stored credential does not match registry')
    owner=(uid,gid) if uid is not None and gid is not None else None
    atomic_write(path,raw,owner=owner)

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime',type=Path,default=Path('/runtime'))
    parser.add_argument('action',choices=['init','issue','revoke','rotate','export','list','import-auth','import-api-key'])
    parser.add_argument('agent',nargs='?')
    parser.add_argument('--output',type=Path)
    parser.add_argument('--source',type=Path)
    parser.add_argument('--operation-id')
    parser.add_argument('--uid',type=int)
    parser.add_argument('--gid',type=int)
    args=parser.parse_args(argv);root=args.runtime.resolve()
    if args.agent and not re.fullmatch(r'[a-z][a-z0-9-]{0,40}',args.agent):parser.error('Invalid agent identifier')
    if args.operation_id and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}',args.operation_id):parser.error('Invalid operation identifier')
    root.mkdir(parents=True,exist_ok=True,mode=0o700);(root/'config').mkdir(exist_ok=True,mode=0o755)
    with (root/'config/.tokens.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        registry=root/'config/identities.json';ids=json.loads(registry.read_text()) if registry.exists() else {}
        if args.action=='list':
            print(json.dumps([{k:v[k] for k in ['agent','role','active']} for v in ids.values()]));return
        if args.action=='init':initialize(root,ids)
        elif args.action in ('import-auth','import-api-key'):
            if not args.source:parser.error('--source is required')
            if args.action=='import-auth':
                credential=json.loads(args.source.read_text())
                if not isinstance(credential,dict) or not (credential.get('tokens') or credential.get('OPENAI_API_KEY')):parser.error('Unsupported authentication file')
            else:
                key=args.source.read_text().strip()
                if len(key)<16 or any(c.isspace() for c in key):parser.error('Invalid API key file')
                credential={'auth_mode':'apikey','OPENAI_API_KEY':key}
            atomic_write(root/'secrets/codex-home/auth.json',json.dumps(credential)+'\n',owner=(SERVICE_UID,SERVICE_GID))
            print('Model authentication imported; contents suppressed');return
        elif args.action=='issue':
            if not args.agent:parser.error('Agent identifier required')
            if args.agent in ids:parser.error('Identity already exists; use explicit rotate to replace its credential')
            provision(root,ids,args.agent,'client')
        elif args.action in ('rotate','revoke','export'):
            if args.agent not in ids:parser.error('Unknown identity')
            if args.action=='revoke':ids[args.agent]['active']=False
            elif args.action=='rotate':
                if not args.operation_id:parser.error('rotate requires a stable --operation-id')
                journal=root/'config/credential-operations.json';ops=json.loads(journal.read_text()) if journal.exists() else {}
                op=args.agent+'/'+args.operation_id
                if op in ops:
                    if ops[op]!=ids[args.agent]['hash'] or not ids[args.agent]['active']:parser.error('This rotation was superseded; nothing changed')
                else:
                    role=ids[args.agent]['role'];provision(root,ids,args.agent,role)
                    ops[op]=ids[args.agent]['hash'];atomic_write(journal,json.dumps(ops)+'\n')
            elif not args.output:parser.error('export requires --output')
        atomic_write(registry,json.dumps(ids,indent=2)+'\n',0o644)
        atomic_write(root/'config/gateway.json',json.dumps(render_gateway(ids),indent=2)+'\n',0o644)
        if args.output:
            if args.action not in ('issue','rotate','export'):parser.error('--output only applies to client credential operations')
            deliver(root,ids,args.agent,args.output,args.uid,args.gid)
        print(json.dumps({'action':args.action,'agent':args.agent,'status':'ok','secret_values_displayed':False}))

if __name__=='__main__':
    try:main()
    except (ValueError,KeyError,OSError):
        raise SystemExit('Credential operation failed; inspect configuration and file permissions. Secret contents suppressed.') from None
