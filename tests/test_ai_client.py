import json

import pytest

from engine.ai_client import AiClient, AiError, _extract_json
from engine.generation import fallback_spell, fallback_update_options, update_budget
from engine.spells import budget_for, spell_cost, validate_spell


def test_extract_json_plain():
    assert _extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_with_fence():
    text = '説明です。\n```json\n{"a": 1, "b": "x"}\n```\n以上。'
    assert _extract_json(text) == {"a": 1, "b": "x"}


def test_extract_json_with_surrounding_text():
    assert _extract_json('答え: {"ok": true} でした') == {"ok": True}


def test_extract_json_failure():
    with pytest.raises(AiError):
        _extract_json("JSONはありません")


def test_mock_missing_fixture_raises(tmp_path):
    ai = AiClient(mock=True, fixtures_dir=tmp_path)
    with pytest.raises(AiError):
        ai.call("nothing", "prompt")


def test_mock_list_fixture_retries_until_valid(tmp_path):
    schema = {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}
    (tmp_path / "seq.json").write_text(
        json.dumps([{"bad": 1}, {"ok": True}]), encoding="utf-8"
    )
    ai = AiClient(mock=True, fixtures_dir=tmp_path)
    assert ai.call("seq", "prompt", schema) == {"ok": True}  # 1回目却下→2回目採用


def test_mock_exhausted_retries_raise(tmp_path):
    schema = {"type": "object", "required": ["ok"]}
    (tmp_path / "bad.json").write_text(json.dumps({"nope": 1}), encoding="utf-8")
    ai = AiClient(mock=True, fixtures_dir=tmp_path)
    with pytest.raises(AiError):
        ai.call("bad", "prompt", schema)


def test_real_call_without_token_fails_fast(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    ai = AiClient(mock=False, config_path=tmp_path / "none.json")
    with pytest.raises(AiError):
        ai.call("turn", "prompt")


# ---- フォールバックの予算適合 --------------------------------------------


def test_fallback_spells_fit_budget_all_roles(fresh_save, balance):
    for role in ("attacker", "support", "tank", "healer"):
        member = fresh_save.member_by_role(role)
        for is_ult in (False, True):
            spell = fallback_spell(fresh_save, balance, member, "テストの詠唱", is_ult)
            errors = validate_spell(spell, balance, fresh_save.level, role, is_ult)
            assert errors == [], f"{role} is_ult={is_ult}: {errors}"


def test_fallback_spell_name_from_incantation(fresh_save, balance):
    member = fresh_save.member_by_role("attacker")
    spell = fallback_spell(fresh_save, balance, member, "とても長い詠唱文でも名前は短く切り取られる", False)
    assert len(spell["name"]) <= 12


def test_fallback_update_options_fit_budget(fresh_save, balance):
    member = fresh_save.member_by_role("attacker")
    ability = member.abilities[0]
    ability.usage_count = 10
    ability.kills = 3
    budget = update_budget(fresh_save, balance, member, ability, False)
    assert budget > budget_for(fresh_save.level, member.role, balance, False)  # ボーナスが乗る
    current = {"name": ability.name, "desc": ability.desc, "ct": ability.ct, "effects": ability.effects}
    options = fallback_update_options(current, budget, balance, False)
    assert len(options) == 3
    for opt in options:
        spell = opt["spell"]
        assert spell_cost(spell["ct"], spell["effects"], balance, False) <= budget + 1e-9