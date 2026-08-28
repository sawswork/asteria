from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.save_io import load_json, new_save  # noqa: E402


@pytest.fixture(autouse=True)
def _no_external_keys(monkeypatch):
    """テストが実APIへ出ないよう、外部キーを常に外す。"""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)


@pytest.fixture()
def world() -> dict:
    return load_json(ROOT / "world/world.json")


@pytest.fixture()
def balance() -> dict:
    return load_json(ROOT / "world/balance.json")


@pytest.fixture()
def fresh_save(world, balance):
    return new_save(world, balance, seed=12345)


@pytest.fixture()
def battle_save(world, balance, fresh_save):
    from engine.battle import start_battle

    return start_battle(fresh_save, world, balance)


def all_normal_commands():
    from engine.commands import Command

    return {
        role: Command(role=role, action="通常攻撃", target="自動")
        for role in ("attacker", "support", "tank", "healer")
    }
