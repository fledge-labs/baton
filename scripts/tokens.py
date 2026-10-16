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
POSTGRES_UID=999
POSTGRES_GID=999

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

def provision(root,identities,agent,role,kind=None,account=None,*,token=None):
    token=secrets.token_urlsafe(48) if token is None else token
    prior=identities.get(agent,{})
    kind=kind or prior.get('kind') or ('service' if role=='dispatcher' else 'agent')
    account=account or prior.get('account') or agent
    identities[agent]={'agent':agent,'role':role,'kind':kind,'account':account,'hash':hashlib.sha256(token.encode()).hexdigest(),'active':True}
    owner=(SERVICE_UID,SERVICE_GID) if role=='dispatcher' else None
    atomic_write(secret_path(root,agent,role),token+'\n',owner=owner)

def render_gateway(identities):
    template=Path(__file__).resolve().parents[1]/'deploy/gateway.template.json'
    config=json.loads(template.read_text());config.pop('_license',None)
    keys=[{'keyHash':'sha256:'+v['hash'],'metadata':{k:v[k] for k in ('agent','role','kind','account')}} for v in identities.values() if v['active']]
    for bind in config['binds']:
        for listener in bind['listeners']:
            for route in listener['routes']:route['policies']['apiKey']['keys']=keys
    return config

def initialize_database(root):
    """Keep database credentials stable across repeated initialization."""
    admin=root/'secrets/database-admin'
    app_config=root/'secrets/database-app/database.json'
    initialized=(root/'data/postgres/pgdata/PG_VERSION').exists()
    passwords={}
    for name in ('postgres','app'):
        path=admin/(name+'.password')
        if not path.exists():
            if initialized or (name=='app' and app_config.exists()):
                raise ValueError('Database credentials missing; restore them before restarting')
            atomic_write(path,secrets.token_urlsafe(48)+'\n',owner=(POSTGRES_UID,POSTGRES_GID))
        value=path.read_text().strip()
        if len(value)<32 or any(c.isspace() for c in value):
            raise ValueError('Invalid database credential file')
        passwords[name]=value
    config={'host':'database','port':5432,'dbname':'baton','user':'baton_app','password':passwords['app']}
    if app_config.exists() and json.loads(app_config.read_text())!=config:
        raise ValueError('Database configuration differs; restore the matching credential files')
    atomic_write(app_config,json.dumps(config)+'\n',owner=(SERVICE_UID,SERVICE_GID))

def initialize(root,identities):
    for name in ['config','secrets/clients','secrets/bridge','secrets/codex-home','secrets/database-admin','secrets/database-app','data/bridge','data/codex','data/postgres']:
        p=root/name;p.mkdir(parents=True,exist_ok=True,mode=0o700)
        if name=='config':os.chmod(p,0o755)
        elif os.geteuid()==0 and name!='secrets/clients':
            uid,gid=(POSTGRES_UID,POSTGRES_GID) if name in ('secrets/database-admin','data/postgres') else (SERVICE_UID,SERVICE_GID)
            os.chown(p,uid,gid);os.chmod(p,0o700)
    initialize_database(root)
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

def read_client_token(path):
    # Bound the read and allow only one conventional file-ending newline.
    with path.open('rb') as source:
        raw=source.read(259)
    if not re.fullmatch(rb'[A-Za-z0-9_-]{32,256}(?:\r?\n)?',raw):
        raise ValueError('Invalid client credential file')
    return raw.rstrip(b'\r\n').decode('ascii')

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime',type=Path,default=Path('/runtime'))
    parser.add_argument('action',choices=['init','issue','revoke','rotate','export','list','import-auth','import-api-key','import-client'])
    parser.add_argument('agent',nargs='?')
    parser.add_argument('--output',type=Path)
    parser.add_argument('--source',type=Path)
    parser.add_argument('--operation-id')
    parser.add_argument('--uid',type=int)
    parser.add_argument('--gid',type=int)
    parser.add_argument('--kind',choices=['human','agent'])
    parser.add_argument('--account')
    args=parser.parse_args(argv);root=args.runtime.resolve()
    if args.agent and not re.fullmatch(r'[a-z][a-z0-9-]{0,40}',args.agent):parser.error('Invalid agent identifier')
    if args.operation_id and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}',args.operation_id):parser.error('Invalid operation identifier')
    if args.account is not None and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}',args.account):parser.error('Invalid account identifier')
    if (args.kind is not None or args.account is not None) and args.action not in ('issue','rotate','import-client'):
        parser.error('--kind and --account only apply to issue, rotate or import-client')
    imported_token=None
    if args.action=='import-client':
        if not args.agent or not args.source:parser.error('import-client requires an agent identifier and --source')
        if args.agent=='codex-worker':parser.error('Reserved internal identity cannot be imported')
        if args.output is not None or args.operation_id is not None or args.uid is not None or args.gid is not None:
            parser.error('import-client does not accept export or rotation options')
        imported_token=read_client_token(args.source)
    root.mkdir(parents=True,exist_ok=True,mode=0o700);(root/'config').mkdir(exist_ok=True,mode=0o755)
    with (root/'config/.tokens.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        registry=root/'config/identities.json';ids=json.loads(registry.read_text()) if registry.exists() else {}
        if args.action=='import-client':
            digest=hashlib.sha256(imported_token.encode()).hexdigest()
            kind=args.kind or 'agent';account=args.account or args.agent
            if any(name!=args.agent and entry.get('hash')==digest for name,entry in ids.items()):
                parser.error('Client credential is already assigned to another identity')
            if args.agent in ids:
                entry=ids[args.agent]
                expected={'agent':args.agent,'role':'client','kind':kind,'account':account,'hash':digest,'active':True}
                if any(entry.get(key)!=value for key,value in expected.items()) or entry.get('active') is not True:
                    parser.error('Existing identity differs or is inactive; import made no changes')
                # A replay does not rotate, reactivate, normalize other identities,
                # replace files or update either public configuration file.
                print(json.dumps({'action':'import-client','agent':args.agent,'status':'ok','replay':True,'secret_values_displayed':False}))
                return
        # The offline administrator explicitly upgrades legacy identity records.
        # Runtime authentication must never infer or accept caller-supplied mapping.
        for agent,entry in ids.items():
            entry.setdefault('kind','service' if entry['role']=='dispatcher' else 'agent')
            entry.setdefault('account',agent)
        if args.action=='list':
            print(json.dumps([{k:v[k] for k in ['agent','role','kind','account','active']} for v in ids.values()]));return
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
            provision(root,ids,args.agent,'client',args.kind,args.account)
        elif args.action=='import-client':
            provision(root,ids,args.agent,'client',args.kind or 'agent',args.account or args.agent,token=imported_token)
        elif args.action in ('rotate','revoke','export'):
            if args.agent not in ids:parser.error('Unknown identity')
            if args.action=='revoke':ids[args.agent]['active']=False
            elif args.action=='rotate':
                if not args.operation_id:parser.error('rotate requires a stable --operation-id')
                if ids[args.agent]['role']!='client' and (args.kind is not None or args.account is not None):
                    parser.error('Internal service principal mapping cannot be changed')
                journal=root/'config/credential-operations.json';ops=json.loads(journal.read_text()) if journal.exists() else {}
                op=args.agent+'/'+args.operation_id
                if op in ops:
                    if ops[op]!=ids[args.agent]['hash'] or not ids[args.agent]['active']:parser.error('This rotation was superseded; nothing changed')
                    if (args.kind is not None and args.kind!=ids[args.agent]['kind']) or (args.account is not None and args.account!=ids[args.agent]['account']):
                        parser.error('Rotation operation already used for different principal mapping')
                else:
                    role=ids[args.agent]['role'];provision(root,ids,args.agent,role,args.kind,args.account)
                    ops[op]=ids[args.agent]['hash'];atomic_write(journal,json.dumps(ops)+'\n')
            elif not args.output:parser.error('export requires --output')
        atomic_write(registry,json.dumps(ids,indent=2)+'\n',0o644)
        atomic_write(root/'config/gateway.json',json.dumps(render_gateway(ids),indent=2)+'\n',0o644)
        if args.output:
            if args.action not in ('issue','rotate','export'):parser.error('--output only applies to client credential operations')
            deliver(root,ids,args.agent,args.output,args.uid,args.gid)
        result={'action':args.action,'agent':args.agent,'status':'ok','secret_values_displayed':False}
        if args.action=='import-client':result['replay']=False
        print(json.dumps(result))

if __name__=='__main__':
    try:main()
    except (ValueError,KeyError,OSError):
        raise SystemExit('Credential operation failed; inspect configuration and file permissions. Secret contents suppressed.') from None
