# SPDX-License-Identifier: Apache-2.0
"""Codex worker memory limits must be declared in Compose and parametrized via CODEX_MEM_LIMIT."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "deploy" / "compose.yaml"
LIMIT_EXPR = "${CODEX_MEM_LIMIT:-640m}"


def _codex_service() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]["codex"]


def test_codex_compose_memory_limits_are_parametrised_and_match_swap():
    codex = _codex_service()
    assert codex["mem_limit"] == LIMIT_EXPR
    assert codex["memswap_limit"] == LIMIT_EXPR
    assert codex["pids_limit"] == 128


def test_env_example_documents_codex_mem_limit_default():
    assert "CODEX_MEM_LIMIT=640m" in (ROOT / "deploy" / ".env.example").read_text(encoding="utf-8")
