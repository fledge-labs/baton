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
