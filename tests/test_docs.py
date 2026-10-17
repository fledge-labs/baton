# SPDX-License-Identifier: Apache-2.0
"""The first-run block in the client docs must stay copy-pasteable and pinned to a real commit."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RAW = "https://raw.githubusercontent.com/fledge-labs/baton/"
BLOCK = re.compile(r"```sh\n(?P<body># SPDX-License-Identifier: Apache-2\.0\nmkdir -p [^\n]*\\\ncurl -fsSL[^`]*?inbox[^`]*?)```")


def first_run_blocks(path):
    return [match.group("body") for match in BLOCK.finditer((ROOT / path).read_text(encoding="utf-8"))]


@pytest.mark.parametrize("path", ["onboarding/stdlib-cli.md", "README.md"])
def test_first_run_block_downloads_a_pinned_commit_and_runs_inbox_with_placeholders(path):
    blocks = first_run_blocks(path)
    assert len(blocks) == 1, "exactly one first-run block"
    body = blocks[0]
    commits = re.findall(re.escape(RAW) + r"([0-9a-f]{40})/adapters/baton_cli\.py", body)
    assert len(commits) == 1, "downloads adapters/baton_cli.py from one full commit hash, not a branch"
    assert re.search(r"python3 \"\$HOME/baton/adapters/baton_cli\.py\" inbox \\\n  --url <BATON_URL> --token-file <TOKEN_FILE> --ca-file <CA_FILE>\n$", body)
    assert "curl -fsSL -o" in body and "| sh" not in body and "main" not in body.split("baton_cli.py")[0]


def test_both_docs_pin_the_same_commit_and_the_cli_doc_leads_with_it():
    pins = {re.search(r"/([0-9a-f]{40})/", first_run_blocks(path)[0]).group(1) for path in ("onboarding/stdlib-cli.md", "README.md")}
    assert len(pins) == 1
    text = (ROOT / "onboarding/stdlib-cli.md").read_text(encoding="utf-8")
    assert text.index("```sh") < text.index("\n## "), "the block sits at the top, before the first section"
