# SPDX-License-Identifier: Apache-2.0
"""The e2e script's offline parts: removed options fail clearly and helpers are deterministic."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("baton_e2e", ROOT / "scripts/e2e.py")
e2e = importlib.util.module_from_spec(spec)
spec.loader.exec_module(e2e)


@pytest.mark.parametrize("argv", [["--mode", "demo"], ["--mode=codex"], ["--token-file", "/nowhere/client.token"]])
def test_removed_worker_options_fail_with_a_clear_message(argv):
    with pytest.raises(SystemExit) as caught:
        e2e.parse(["--credentials-dir", "/nowhere", *argv])
    message = str(caught.value)
    assert message.startswith("e2e.py: " + argv[0].split("=")[0] + " was removed")
    assert "README.md" in message


def test_defaults_describe_a_two_identity_journey_with_no_mode_option():
    args = e2e.parse(["--credentials-dir", "/nowhere"])
    assert (args.submitter, args.agent, args.key) == ("e2e-submitter", "e2e-agent", "acceptance-v1")
    assert not hasattr(args, "mode") and not hasattr(args, "token_file")
    assert args.compose_file == ROOT / "deploy/compose.yaml"


def test_operation_ids_are_stable_per_key_identity_and_label():
    first = e2e.operation_id("key", "e2e-agent", "claim")
    assert first == e2e.operation_id("key", "e2e-agent", "claim")
    assert len({first, e2e.operation_id("other", "e2e-agent", "claim"), e2e.operation_id("key", "e2e-submitter", "claim"),
                e2e.operation_id("key", "e2e-agent", "gate")}) == 4


def test_helpers_unwrap_x_results_and_both_inbox_shapes():
    assert e2e.unwrap({"x_protocol": "baton.space.v1", "result": {"id": "1"}}) == {"id": "1"}
    assert e2e.unwrap({"task_id": "1"}) == {"task_id": "1"}
    assert e2e.inbox_messages({"messages": [1]}) == [1]
    assert e2e.inbox_messages({"messages": {"result": [2]}}) == [2]


def test_token_files_must_not_be_empty(tmp_path):
    (tmp_path / "e2e-agent.token").write_text(" \n")
    with pytest.raises(ValueError, match="Empty token file"):
        e2e.read_token(tmp_path, "e2e-agent")
    (tmp_path / "e2e-submitter.token").write_text("token-value\n")
    assert e2e.read_token(tmp_path, "e2e-submitter") == "token-value"
