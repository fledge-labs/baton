# SPDX-License-Identifier: Apache-2.0
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

spec=importlib.util.spec_from_file_location('token_admin',Path(__file__).resolve().parents[1]/'scripts/tokens.py')
tokens=importlib.util.module_from_spec(spec)
spec.loader.exec_module(tokens)

def invoke(root,*args):
    tokens.main(['--runtime',str(root),*args])

def test_issue_rotate_replay_revoke_and_export(tmp_path,capsys):
    root=tmp_path/'runtime';output=tmp_path/'delivery/client.token'
    invoke(root,'init')
    invoke(root,'issue','example-client','--output',str(output))
    old=output.read_text().strip()
    assert output.stat().st_mode & 0o777==0o600
    assert old not in capsys.readouterr().out
    with pytest.raises(SystemExit):invoke(root,'issue','example-client')
    invoke(root,'rotate','example-client','--operation-id','rotate-001','--output',str(output))
    new=output.read_text().strip();assert old!=new
    invoke(root,'rotate','example-client','--operation-id','rotate-001','--output',str(output))
    assert output.read_text().strip()==new
    config=(root/'config/gateway.json').read_text()
    assert old not in config and new not in config
    assert hashlib.sha256(new.encode()).hexdigest() in config
    invoke(root,'revoke','example-client')
    assert hashlib.sha256(new.encode()).hexdigest() not in (root/'config/gateway.json').read_text()
    with pytest.raises(ValueError):invoke(root,'export','example-client','--output',str(output))
    assert new not in capsys.readouterr().out

def test_internal_dispatcher_not_exportable(tmp_path):
    root=tmp_path/'runtime';invoke(root,'init')
    with pytest.raises(ValueError):invoke(root,'export','codex-worker','--output',str(tmp_path/'worker.token'))
    registry=json.loads((root/'config/identities.json').read_text())
    before=registry['codex-worker']['hash']
    invoke(root,'init')
    assert json.loads((root/'config/identities.json').read_text())['codex-worker']['hash']==before


def test_database_files_are_private_stable_and_not_gateway_metadata(tmp_path,capsys):
    root=tmp_path/'runtime';invoke(root,'init')
    admin=root/'secrets/database-admin'
    files=[admin/'postgres.password',admin/'app.password',root/'secrets/database-app/database.json']
    first={path:path.read_bytes() for path in files}
    config=json.loads(files[-1].read_text())
    assert config=={'host':'database','port':5432,'dbname':'baton','user':'baton_app','password':files[1].read_text().strip()}
    assert config['password']!=files[0].read_text().strip()
    assert all(path.stat().st_mode & 0o777==0o600 for path in files)
    assert not (root/'secrets/mail').exists()
    assert not (root/'secrets/bridge/mail.token').exists()
    invoke(root,'init')
    assert {path:path.read_bytes() for path in files}==first
    public=(root/'config/identities.json').read_text()+(root/'config/gateway.json').read_text()+capsys.readouterr().out
    assert files[0].read_text().strip() not in public
    assert config['password'] not in public


def test_missing_database_credentials_cannot_reset_an_existing_database(tmp_path):
    root=tmp_path/'runtime';invoke(root,'init')
    pgdata=root/'data/postgres/pgdata';pgdata.mkdir()
    (pgdata/'PG_VERSION').write_text('17\n')
    password=root/'secrets/database-admin/postgres.password'
    password.unlink()
    with pytest.raises(ValueError,match='restore'):
        invoke(root,'init')
    assert not password.exists()


def test_database_secret_mismatch_fails_closed(tmp_path):
    root=tmp_path/'runtime';invoke(root,'init')
    config=root/'secrets/database-app/database.json'
    broken=json.loads(config.read_text());broken['password']='synthetic-mismatching-password-value'
    config.write_text(json.dumps(broken))
    with pytest.raises(ValueError,match='differs'):
        invoke(root,'init')
    assert json.loads(config.read_text())==broken


def test_admin_principal_mapping_survives_rotation_and_replay(tmp_path):
    root=tmp_path/'runtime';invoke(root,'init')
    invoke(root,'issue','human-token','--kind','human','--account','person-001')
    registry=root/'config/identities.json'
    first=json.loads(registry.read_text())['human-token']
    assert (first['kind'],first['account'])==('human','person-001')
    invoke(root,'rotate','human-token','--operation-id','rotate-identity-1')
    second=json.loads(registry.read_text())['human-token']
    assert (second['kind'],second['account'])==('human','person-001')
    assert first['hash']!=second['hash']
    invoke(root,'rotate','human-token','--operation-id','rotate-identity-1')
    assert json.loads(registry.read_text())['human-token']==second
    with pytest.raises(SystemExit):
        invoke(root,'rotate','human-token','--operation-id','rotate-identity-1','--account','another-person')
    assert json.loads(registry.read_text())['human-token']==second
    invoke(root,'rotate','human-token','--operation-id','rotate-identity-2','--kind','agent','--account','connected-account')
    third=json.loads(registry.read_text())['human-token']
    assert (third['kind'],third['account'])==('agent','connected-account')


def test_only_admin_client_issue_and_rotate_accept_mapping(tmp_path):
    root=tmp_path/'runtime';invoke(root,'init')
    with pytest.raises(SystemExit):invoke(root,'init','--kind','human')
    with pytest.raises(SystemExit):invoke(root,'issue','example','--account','')
    with pytest.raises(SystemExit):invoke(root,'issue','example','--kind','service')
    with pytest.raises(SystemExit):invoke(root,'rotate','codex-worker','--operation-id','no-change','--kind','human')
    assert json.loads((root/'config/identities.json').read_text())['codex-worker']['kind']=='service'


def test_admin_init_explicitly_upgrades_legacy_mapping_without_rotating(tmp_path):
    root=tmp_path/'runtime';invoke(root,'init');invoke(root,'issue','legacy-client')
    registry=root/'config/identities.json';data=json.loads(registry.read_text())
    before=data['legacy-client']['hash']
    for entry in data.values():
        entry.pop('kind');entry.pop('account')
    registry.write_text(json.dumps(data))
    invoke(root,'init')
    entry=json.loads(registry.read_text())['legacy-client']
    assert (entry['kind'],entry['account'],entry['hash'])==('agent','legacy-client',before)
