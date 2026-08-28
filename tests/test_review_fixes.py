"""M2レビュー指摘の回帰テスト。"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import jsonschema
import pytest

from engine.ai_client import AiClient, AiError, _extract_json
from engine.ai_schemas import ENEMY_GEN_SCHEMA
from engine.battle import resolve_turn
from engine.enemy_ai import EnemyDecision, decide
from engine.generation import (
    _special_within_budget,
    fallback_update_options,
    generate_enemy,
    generate_spell,
)
from engine.models import Enemy
from engine.rng import Rng
from engine.save_io import load_save, write_save
from engine.spells import spell_cost, validate_spell
from tests.conftest import all_normal_commands
from tests.test_m2_flows import gen_body, mock_ai, run, update_body
from tests.test_turn_runner import FakeGhApi, _snapshot, all_normal, body_from, make_issue, make_root

ROOT = Path(__file__).resolve().parent.parent


def _valid_enemy(stats=None, special_effects=None):
    return {
        "name": "検証用の獣",
        "title": "t",
        "personality": "凶暴",
        "tier": "standard",
        "intelligent": True,
        "stats": stats or {"hp": 200, "atk": 12, "def": 8, "agi": 10},
        "actions": {
            "normal": {"name": "爪", "effects": [{"tag": "damage", "power": 1.0, "target": "enemy"}]},
            "special": {"name": "咆哮", "effects": special_effects or [{"tag": "damage", "power": 1.5, "target": "enemy"}]},
        },
        "intro": "現れた。",
    }


# ---- 敵アクションスキーマの穴 --------------------------------------------


def test_enemy_effect_missing_fields_rejected_by_schema():
    bad = _valid_enemy(special_effects=[{"tag": "damage", "target": "enemy"}])  # powerなし
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, ENEMY_GEN_SCHEMA)
    bad2 = _valid_enemy(special_effects=[{"tag": "stun", "target": "enemy"}])  # turnsなし
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad2, ENEMY_GEN_SCHEMA)


def test_enemy_negative_cost_combo_rejected():
    # debuff mult>1 / buff mult<1 はスキーマで排除される(負コストで予算相殺できない)
    bad = _valid_enemy(
        special_effects=[
            {"tag": "damage", "power": 2.5, "hits": 3, "target": "enemy"},
            {"tag": "debuff", "stat": "agi", "mult": 1.6, "turns": 3, "target": "enemy"},
        ]
    )
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, ENEMY_GEN_SCHEMA)


def test_special_budget_clamps_negative_costs(fresh_save, balance):
    # 万一負コスト効果が混ざっても、合算はクランプされて予算超過が露見する
    actions = {
        "normal": {"name": "n", "effects": [{"tag": "damage", "power": 1.0, "target": "enemy"}]},
        "special": {
            "name": "s",
            "effects": [
                {"tag": "damage", "power": 2.5, "hits": 3, "target": "enemy"},
                {"tag": "buff", "stat": "agi", "mult": 0.5, "turns": 3, "target": "self"},
            ],
        },
    }
    assert _special_within_budget(actions, fresh_save, balance) is False


def test_malformed_enemy_response_falls_back_not_crash(tmp_path, fresh_save, world, balance):
    # スキーマは通るが検証中に例外が出るケースでもフォールバックする(ゲームを止めない)
    fixtures = tmp_path / "fx"
    fixtures.mkdir()
    broken = _valid_enemy()
    del broken["actions"]["special"]["effects"][0]["power"]  # スキーマ違反→リトライ→尽きてAiError
    (fixtures / "enemy_gen.json").write_text(json.dumps(broken), encoding="utf-8")
    ai = AiClient(mock=True, fixtures_dir=fixtures)
    enemy, intro, used_ai = generate_enemy(fresh_save, world, balance, ai, Rng(1))
    assert used_ai is False
    assert enemy.name in {"星屑の亡霊", "蝕まれた岩甲獣", "夜哭きの梟"}


# ---- CT価格(手数の悪用) -------------------------------------------------


def test_ct1_nuke_rejected(balance):
    spell = {"name": "毎ターン砲", "desc": "", "ct": 1, "effects": [{"tag": "damage", "power": 3.1, "target": "enemy"}]}
    errors = validate_spell(spell, balance, level=1, role="attacker", is_ult=False)
    assert any("予算超過" in e for e in errors)


def test_ct0_ability_rejected(balance):
    spell = {"name": "常時発動", "desc": "", "ct": 0, "effects": [{"tag": "damage", "power": 1.0, "target": "enemy"}]}
    errors = validate_spell(spell, balance, level=1, role="attacker", is_ult=False)
    assert any("CTは1以上" in e for e in errors)


def test_ct1_costs_more_than_ct3_same_effect(balance):
    effects = [{"tag": "damage", "power": 1.5, "target": "enemy"}]
    assert spell_cost(1, effects, balance, False) > spell_cost(3, effects, balance, False)


# ---- フォールバック3案の予算保証 ------------------------------------------


def test_fallback_update_options_buff_only_spell_fits_budget(balance):
    current = {
        "name": "星の勇歌",
        "desc": "",
        "ct": 2,
        "effects": [{"tag": "buff", "stat": "atk", "mult": 1.19, "turns": 2, "target": "party"}],
    }
    budget = spell_cost(2, current["effects"], balance, False) + 0.5  # ほぼ余裕なし
    options = fallback_update_options(current, budget, balance, False)
    for opt in options:
        sp = opt["spell"]
        assert spell_cost(sp["ct"], sp["effects"], balance, False) <= budget + 1e-9


# ---- pending_update の陳腐化 ----------------------------------------------


def test_generate_invalidates_same_slot_pending(tmp_path):
    root = make_root(tmp_path)
    save = load_save(root / "save")
    save.spell_tokens = 1
    write_save(save, root / "save")
    run(root, make_issue(1, update_body("アタッカー", "アビ1", "提案を見る"), title="[UPDATE] 技アップデート"))
    assert load_save(root / "save").pending_update is not None
    run(root, make_issue(2, gen_body("アタッカー", "アビ1", "新しい技を"), title="[GENERATE] 技生成の儀式"))
    save = load_save(root / "save")
    assert save.pending_update is None  # 生成で古い提案は無効化
    gh = FakeGhApi()
    run(root, make_issue(3, update_body("アタッカー", "アビ1", "案1"), title="[UPDATE] 技アップデート"), gh=gh)
    save = load_save(root / "save")
    assert save.member_by_role("attacker").abilities[0].name == "星穿ちの牙"  # 生成技は守られた
    assert "選択できる提案がありません" in gh.comments[0][1]


# ---- 知能層の制約 ----------------------------------------------------------


def _intelligent_enemy(**kw) -> Enemy:
    base = dict(
        id="enemy_gen1", name="影", title="", max_hp=200, hp=200, atk=12, df=8, agi=11,
        actions={
            "normal": {"name": "爪", "effects": [{"tag": "damage", "power": 1.0, "target": "enemy"}]},
            "special": {"name": "大爪", "effects": [{"tag": "damage", "power": 1.5, "target": "enemy"}]},
        },
        intelligent=True, tier="standard",
    )
    base.update(kw)
    return Enemy(**base)


def test_override_ignored_for_rule_layer_enemy(battle_save, balance):
    # 非知能の敵(初戦の仔狼)はAI応答で乗っ取れない: ヘイト最大狙いのまま
    battle_save.member_by_role("tank").hate = 500
    override = {battle_save.battle.enemies[0].id: EnemyDecision(action_key="normal", target_id="mio")}
    _, report = resolve_turn(battle_save, all_normal_commands(), balance, None, override)
    enemy_line = next(l for l in report.lines if l.startswith("星蝕の仔狼"))
    assert "ガンテ" in enemy_line and "ミオ" not in enemy_line


def test_intelligent_special_cannot_fire_every_turn(battle_save, balance):
    battle_save.battle.enemies = [_intelligent_enemy()]
    override = {"enemy_gen1": EnemyDecision(action_key="special", target_id="gante")}
    s1, r1 = resolve_turn(battle_save, all_normal_commands(), balance, None, dict(override))
    assert any("大爪" in l for l in r1.lines)  # 初回はOK
    assert s1.battle.enemies[0].last_special_turn == 1
    s2, r2 = resolve_turn(s1, all_normal_commands(), balance, None, dict(override))
    assert not any("大爪" in l for l in r2.lines)  # 連発は通常攻撃に格下げ
    assert any("爪" in l for l in r2.lines)


def test_decide_respects_special_cooldown_directly(balance, battle_save):
    enemy = _intelligent_enemy(last_special_turn=2)
    battle_save.battle.turn = 3
    d = decide(enemy, battle_save.battle, battle_save.party, Rng(1), 3,
               override=EnemyDecision(action_key="special", target_id="sora"))
    assert d.action_key == "normal"
    battle_save.battle.turn = 5  # 2 + 3 = 5ターン目からは解禁
    d2 = decide(enemy, battle_save.battle, battle_save.party, Rng(1), 3,
                override=EnemyDecision(action_key="special", target_id="sora"))
    assert d2.action_key == "special"


# ---- レベルアップと蘇生 ----------------------------------------------------


def test_levelup_does_not_revive_dead_members(battle_save, balance):
    from engine.battle import xp_to_next

    battle_save.member_by_role("support").hp = 0
    battle_save.battle.enemies[0].hp = 1
    battle_save.xp = xp_to_next(1, balance) - 10
    s1, r1 = resolve_turn(battle_save, all_normal_commands(), balance)
    assert r1.result == "victory" and s1.level == 2
    assert s1.member_by_role("support").hp == 0  # 戦闘不能のまま
    assert s1.member_by_role("support").max_hp > battle_save.member_by_role("support").max_hp  # 成長はする


# ---- AIクライアント堅牢化 --------------------------------------------------


def test_extract_json_with_backticks_in_string():
    text = '説明。\n```json\n{"name": "x", "desc": "コード```で囲む技"}\n```\n以上'
    assert _extract_json(text)["desc"] == "コード```で囲む技"


def test_extract_json_with_prose_braces():
    text = "前置き {注: これは違う ... 実際の答え: {\"ok\": true} 後書き }"
    # 最初にデコード可能なオブジェクトを拾う
    assert _extract_json(text) == {"ok": True}


def _fake_run(envelope: dict | str, returncode: int = 0):
    def runner(cmd, **kwargs):
        _fake_run.captured_env = kwargs.get("env")
        _fake_run.captured_cmd = cmd
        stdout = envelope if isinstance(envelope, str) else json.dumps(envelope, ensure_ascii=False)
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr="")

    return runner


def test_invoke_cli_success_and_env_hygiene(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "dummy-token")
    monkeypatch.setenv("GITHUB_TOKEN", "gh-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "api-secret")
    monkeypatch.setattr(subprocess, "run", _fake_run({"result": '{"ok": true}'}))
    ai = AiClient(mock=False, config_path=tmp_path / "none.json")
    assert ai.call("t", "p") == {"ok": True}
    env = _fake_run.captured_env
    assert "ANTHROPIC_API_KEY" not in env  # 従量課金キーは絶対に渡さない
    assert "GITHUB_TOKEN" not in env  # リポジトリ権限もAI実行系へ渡さない
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "dummy-token"
    assert "--max-turns" in _fake_run.captured_cmd
    assert "--disallowedTools" in _fake_run.captured_cmd


def test_invoke_cli_error_envelope_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "dummy-token")
    monkeypatch.setattr(subprocess, "run", _fake_run({"is_error": True, "result": "credit exhausted"}))
    ai = AiClient(mock=False, config_path=tmp_path / "none.json")
    with pytest.raises(AiError):
        ai.call("t", "p")


def test_invoke_cli_non_json_stdout_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "dummy-token")
    monkeypatch.setattr(subprocess, "run", _fake_run("garbage output"))
    ai = AiClient(mock=False, config_path=tmp_path / "none.json")
    with pytest.raises(AiError):
        ai.call("t", "p")


# ---- 却下→リトライ/フォールバック経路(過剰適合の防止) --------------------


def test_over_budget_ai_spell_falls_back(tmp_path, fresh_save, world, balance):
    fixtures = tmp_path / "fx"
    fixtures.mkdir()
    nuke = {"name": "禁断の星砲", "desc": "強すぎる", "ct": 1,
            "effects": [{"tag": "damage", "power": 4.0, "hits": 3, "target": "enemy"}]}
    (fixtures / "spell_gen.json").write_text(json.dumps(nuke, ensure_ascii=False), encoding="utf-8")
    ai = AiClient(mock=True, fixtures_dir=fixtures)
    member = fresh_save.member_by_role("attacker")
    spell, used_ai = generate_spell(fresh_save, world, balance, ai, member, "アビ1", "全てを穿て", False)
    assert used_ai is False  # スキーマは通るが予算超過→フォールバック
    errors = validate_spell(spell, balance, fresh_save.level, "attacker", False)
    assert errors == []


def test_out_of_tolerance_enemy_falls_back(tmp_path, fresh_save, world, balance):
    fixtures = tmp_path / "fx"
    fixtures.mkdir()
    giant = _valid_enemy(stats={"hp": 990, "atk": 90, "def": 90, "agi": 90})
    (fixtures / "enemy_gen.json").write_text(json.dumps(giant, ensure_ascii=False), encoding="utf-8")
    ai = AiClient(mock=True, fixtures_dir=fixtures)
    enemy, _intro, used_ai = generate_enemy(fresh_save, world, balance, ai, Rng(1))
    assert used_ai is False
    assert enemy.max_hp < 500  # フォールバックはカーブ準拠