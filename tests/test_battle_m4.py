"""M4-A: 残留タグ+チェイン反応/誓約(制約)/適応進化+歪み/歴史の共鳴/フルオート。"""
from __future__ import annotations

from pathlib import Path

from engine.ai_client import AiClient
from engine.battle import resolve_turn, start_battle
from engine.commands import Command, validate_commands
from engine.enemy_ai import _pattern_action_key
from engine.generation import fallback_evolution, generate_evolution
from engine.issue_parser import parse_generate_body
from engine.models import FieldTag
from engine.spells import constraint_multiplier, effect_cost, validate_spell
from tests.conftest import all_normal_commands

ROOT = Path(__file__).resolve().parent.parent


def _cmds(**overrides):
    cmds = all_normal_commands()
    for role, (action, target) in overrides.items():
        cmds[role] = Command(role, action, target)
    return cmds


def _set_effect(save, role, idx, effects, name="試験技", ct=0):
    ability = save.member_by_role(role).abilities[idx]
    ability.effects = effects
    ability.name = name
    ability.ct = ct
    return ability


# ---- 残留タグとチェイン反応 ----------------------------------------------


def test_field_effect_attaches_tag(battle_save, world, balance):
    _set_effect(battle_save, "support", 0, [{"tag": "field", "name": "濡れ星", "turns": 2, "target": "enemy"}], "星の雫")
    s1, r1 = resolve_turn(battle_save, _cmds(support=("アビ1", "敵1")), balance, world)
    enemy = s1.battle.enemies[0]
    assert any("【濡れ星】が残留した" in l for l in r1.lines)
    assert [t.name for t in enemy.field_tags] == ["濡れ星"]
    assert enemy.field_tags[0].turns_left == 1  # ターン終了時に1減


def test_chain_reaction_consumes_and_multiplies(battle_save, world, balance):
    enemy = battle_save.battle.enemies[0]
    enemy.field_tags.append(FieldTag(name="濡れ星", turns_left=3))
    _set_effect(battle_save, "attacker", 0, [{"tag": "damage", "power": 1.0, "field": "雷紋", "target": "enemy"}], "雷紋の刃")
    s1, r1 = resolve_turn(battle_save, _cmds(attacker=("アビ1", "敵1")), balance, world)
    assert any("【感電】" in l for l in r1.lines)  # world.jsonのチェインログ
    after = s1.battle.enemies[0]
    assert all(t.name != "濡れ星" for t in after.field_tags)  # 素材タグは消費
    assert all(t.name != "雷紋" for t in after.field_tags)  # 反応成立時、incomingは残らない


def test_no_chain_attaches_incoming_tag(battle_save, world, balance):
    _set_effect(battle_save, "attacker", 0, [{"tag": "damage", "power": 1.0, "field": "焔種", "target": "enemy"}], "火種の刃")
    s1, _ = resolve_turn(battle_save, _cmds(attacker=("アビ1", "敵1")), balance, world)
    tags = {t.name for t in s1.battle.enemies[0].field_tags}
    assert "焔種" in tags  # 反応不成立→静かに付与(次の布石になる)


def test_distortion_weakness_multiplier(battle_save, world, balance):
    enemy = battle_save.battle.enemies[0]
    enemy.weaknesses.append({"field": "雷紋", "mult": 1.5})
    enemy.max_hp = enemy.hp = 100000  # 倒さず素ダメージ比較
    _set_effect(battle_save, "attacker", 0, [{"tag": "damage", "power": 1.0, "field": "雷紋", "target": "enemy"}], "雷紋の刃")
    s1, r1 = resolve_turn(battle_save, _cmds(attacker=("アビ1", "敵1")), balance, world)
    assert any("歪みを突いた" in l for l in r1.lines)


def test_field_tag_expires(battle_save, world, balance):
    battle_save.battle.enemies[0].field_tags.append(FieldTag(name="油星", turns_left=1))
    s1, _ = resolve_turn(battle_save, all_normal_commands(), balance, world)
    assert s1.battle.enemies[0].field_tags == []


def test_field_tag_stack_cap(battle_save, world, balance):
    enemy = battle_save.battle.enemies[0]
    cap = int(balance["field"]["max_stacks_per_target"])
    for i in range(cap):
        enemy.field_tags.append(FieldTag(name=f"タグ{i}", turns_left=3))
    _set_effect(battle_save, "support", 0, [{"tag": "field", "name": "濡れ星", "turns": 2, "target": "enemy"}], "星の雫")
    s1, _ = resolve_turn(battle_save, _cmds(support=("アビ1", "敵1")), balance, world)
    assert len(s1.battle.enemies[0].field_tags) <= cap
    assert all(t.name != "濡れ星" for t in s1.battle.enemies[0].field_tags)


def test_start_battle_clears_field_tags_and_battle_uses(fresh_save, world, balance):
    fresh_save.party[0].field_tags.append(FieldTag(name="濡れ星", turns_left=2))
    fresh_save.party[0].abilities[0].battle_uses = 3
    fresh_save.party[0].ultimate.battle_uses = 1
    s = start_battle(fresh_save, world, balance)
    assert s.party[0].field_tags == []
    assert s.party[0].abilities[0].battle_uses == 0
    assert s.party[0].ultimate.battle_uses == 0


# ---- 誓約(制約タグ) ----------------------------------------------------


def test_constraint_multiplier_capped(balance):
    all_ids = ["hp_below_30", "self_stun_after", "once_per_battle", "first_three_turns", "vs_elite_plus"]
    assert constraint_multiplier(all_ids, balance) == float(balance["constraints"]["total_mult_cap"])
    assert constraint_multiplier(["hp_below_30"], balance) == 1.6


def test_validate_spell_with_constraints_expands_budget(balance):
    # 基本予算(Lv1 attacker=29.4)を超えるが、誓約×1.6なら通る技
    spell = {"name": "背水の一撃", "desc": "強力な一撃", "ct": 2,
             "effects": [{"tag": "damage", "power": 4.0, "target": "enemy"}]}
    assert any("予算超過" in e for e in validate_spell(spell, balance, 1, "attacker", False))
    assert validate_spell(spell, balance, 1, "attacker", False, ["hp_below_30"]) == []
    assert any("未知の制約タグ" in e for e in validate_spell(spell, balance, 1, "attacker", False, ["not_a_real_oath"]))


def test_hp_below_30_blocks_at_full_hp(battle_save, balance):
    ability = _set_effect(battle_save, "attacker", 0, [{"tag": "damage", "power": 1.0, "target": "enemy"}])
    ability.constraints = ["hp_below_30"]
    errors = validate_commands(battle_save, battle_save.battle, _cmds(attacker=("アビ1", "自動")), balance)
    assert any("HP30%以下" in e.reason for e in errors)
    m = battle_save.member_by_role("attacker")
    m.hp = int(m.max_hp * 0.2)
    assert validate_commands(battle_save, battle_save.battle, _cmds(attacker=("アビ1", "自動")), balance) == []


def test_once_per_battle_blocks_second_use(battle_save, balance):
    ability = _set_effect(battle_save, "attacker", 0, [{"tag": "damage", "power": 1.0, "target": "enemy"}])
    ability.constraints = ["once_per_battle"]
    assert validate_commands(battle_save, battle_save.battle, _cmds(attacker=("アビ1", "自動")), balance) == []
    ability.battle_uses = 1
    errors = validate_commands(battle_save, battle_save.battle, _cmds(attacker=("アビ1", "自動")), balance)
    assert any("既に使いました" in e.reason for e in errors)


def test_first_three_turns_blocks_late(battle_save, balance):
    ability = _set_effect(battle_save, "attacker", 0, [{"tag": "damage", "power": 1.0, "target": "enemy"}])
    ability.constraints = ["first_three_turns"]
    battle_save.battle.turn = 4
    errors = validate_commands(battle_save, battle_save.battle, _cmds(attacker=("アビ1", "自動")), balance)
    assert any("ターン3を過ぎています" in e.reason for e in errors)


def test_vs_elite_plus_requires_elite(battle_save, balance):
    ability = _set_effect(battle_save, "attacker", 0, [{"tag": "damage", "power": 1.0, "target": "enemy"}])
    ability.constraints = ["vs_elite_plus"]
    errors = validate_commands(battle_save, battle_save.battle, _cmds(attacker=("アビ1", "自動")), balance)
    assert any("この敵には向けられません" in e.reason for e in errors)
    battle_save.battle.enemies[0].tier = "elite"
    assert validate_commands(battle_save, battle_save.battle, _cmds(attacker=("アビ1", "自動")), balance) == []


def test_self_stun_after_costs_next_turn(battle_save, world, balance):
    ability = _set_effect(battle_save, "attacker", 0, [{"tag": "damage", "power": 1.0, "target": "enemy"}])
    ability.constraints = ["self_stun_after"]
    battle_save.battle.enemies[0].max_hp = battle_save.battle.enemies[0].hp = 100000
    s1, r1 = resolve_turn(battle_save, _cmds(attacker=("アビ1", "自動")), balance, world)
    assert any("誓約の反動" in l for l in r1.lines)
    assert s1.member_by_role("attacker").stunned_turns == 1  # 次ターンを失う
    assert s1.member_by_role("attacker").abilities[0].battle_uses == 1
    s2, r2 = resolve_turn(s1, all_normal_commands(), balance, world)
    assert any("ソラは行動不能で動けない" in l for l in r2.lines)
    assert s2.member_by_role("attacker").stunned_turns == 0


# ---- 適応進化と歪み ------------------------------------------------------


def _make_elite(battle_save):
    enemy = battle_save.battle.enemies[0]
    enemy.tier = "elite"
    enemy.max_hp = enemy.hp = 100000
    return enemy


def test_hp_evolution_telegraph_then_resolve(battle_save, world, balance):
    enemy = _make_elite(battle_save)
    enemy.hp = int(enemy.max_hp * 0.4)  # 50%割れ
    old_atk = enemy.atk
    s1, r1 = resolve_turn(battle_save, all_normal_commands(), balance, world)
    e1 = s1.battle.enemies[0]
    assert e1.evolution_pending == {"reason": "hp"}
    assert e1.hp_evolution_triggered
    assert any("進化の前兆" in l for l in r1.lines)
    # 次ターン開始時に実体化(演出はフォールバック、数値はbalance)
    s2, r2 = resolve_turn(s1, all_normal_commands(), balance, world)
    e2 = s2.battle.enemies[0]
    assert any("進化した" in l for l in r2.lines)
    assert e2.evolution_pending is None
    assert e2.evolutions_used == 1
    assert e2.atk == max(1, round(old_atk * balance["evolution"]["bonus_mult"]))
    assert "evolved" in e2.actions
    assert len(e2.weaknesses) == 1  # 歪み(弱点)が代償として付く
    assert e2.weaknesses[0]["field"] in world["distortion_weaknesses"]
    assert e2.weaknesses[0]["mult"] == balance["evolution"]["weakness_mult"]
    assert e2.evolutions[0]["reason"] == "hp"
    # HP契機は再発火しない
    s3, r3 = resolve_turn(s2, all_normal_commands(), balance, world)
    assert s3.battle.enemies[0].evolution_pending is None


def test_cc_evolution_trigger(battle_save, world, balance):
    enemy = _make_elite(battle_save)
    enemy.cc_resist["stun"] = int(balance["evolution"]["cc_trigger_count"])
    s1, r1 = resolve_turn(battle_save, all_normal_commands(), balance, world)
    assert s1.battle.enemies[0].evolution_pending == {"reason": "cc"}


def test_standard_enemy_never_evolves(battle_save, world, balance):
    enemy = battle_save.battle.enemies[0]  # tier=standard
    enemy.max_hp = enemy.hp = 100000
    enemy.hp = int(enemy.max_hp * 0.3)
    s1, _ = resolve_turn(battle_save, all_normal_commands(), balance, world)
    assert s1.battle.enemies[0].evolution_pending is None


def test_evolution_capped_by_tier(battle_save, world, balance):
    enemy = _make_elite(battle_save)  # elite: 最大1回
    enemy.evolutions_used = 1
    enemy.hp = int(enemy.max_hp * 0.3)
    s1, _ = resolve_turn(battle_save, all_normal_commands(), balance, world)
    assert s1.battle.enemies[0].evolution_pending is None


def test_evolution_uses_ai_override(battle_save, world, balance):
    enemy = _make_elite(battle_save)
    enemy.evolution_pending = {"reason": "hp"}
    enemy.hp_evolution_triggered = True
    spec = {"name": "星蝕の暴走", "desc": "d", "line": "……ォォオ!",
            "action": {"name": "暴走せし星牙", "effects": [{"tag": "damage", "power": 2.0, "target": "enemy"}]}}
    s1, r1 = resolve_turn(battle_save, all_normal_commands(), balance, world, None, {enemy.id: spec})
    e1 = s1.battle.enemies[0]
    assert e1.actions["evolved"]["name"] == "暴走せし星牙"
    assert any("《星蝕の暴走》" in l for l in r1.lines)
    assert any("……ォォオ!" in l for l in r1.lines)


def test_scan_reveals_distortion(battle_save, world, balance):
    enemy = battle_save.battle.enemies[0]
    enemy.weaknesses.append({"field": "油星", "mult": 1.5})
    _set_effect(battle_save, "support", 0, [{"tag": "scan", "target": "enemy"}], "星の瞳")
    s1, r1 = resolve_turn(battle_save, _cmds(support=("アビ1", "敵1")), balance, world)
    assert any("歪み(弱点)" in l and "油星" in l for l in r1.lines)


def test_enemy_ai_prefers_evolved_action(battle_save, balance):
    enemy = battle_save.battle.enemies[0]
    enemy.actions["evolved"] = {"name": "覚醒の一撃", "effects": [{"tag": "damage", "power": 1.8, "target": "enemy"}]}
    battle_save.battle.turn = int(balance["enemy"]["strong_attack_every"])
    assert _pattern_action_key(enemy, battle_save.battle, int(balance["enemy"]["strong_attack_every"])) == "evolved"


def test_generate_evolution_mock_and_fallback(battle_save, world, balance, tmp_path):
    enemy = battle_save.battle.enemies[0]
    enemy.evolution_pending = {"reason": "hp"}
    ai = AiClient(mock=True, fixtures_dir=ROOT / "fixtures/ai")
    spec, used_ai = generate_evolution(battle_save, world, balance, ai, enemy)
    assert used_ai and spec["name"] == "星蝕の暴走"
    empty = tmp_path / "none"
    empty.mkdir()
    broken = AiClient(mock=True, fixtures_dir=empty)
    spec2, used2 = generate_evolution(battle_save, world, balance, broken, enemy)
    assert not used2 and spec2 == fallback_evolution()


# ---- 歴史の共鳴 ----------------------------------------------------------


def test_resonance_fires_once_per_battle(battle_save, world, balance):
    battle_save.battle.enemies[0].max_hp = battle_save.battle.enemies[0].hp = 100000
    atk = battle_save.member_by_role("attacker")
    gen_ability = _set_effect(battle_save, "attacker", 0, [{"tag": "damage", "power": 1.0, "target": "enemy"}], "新星撃", ct=2)
    gen_ability.id = "sora_gen3"  # 生成技(最新世代)を模す
    sup_ability = battle_save.member_by_role("support").abilities[2]  # ryuno_a3 = 初代(gen0)
    s1, r1 = resolve_turn(
        battle_save, _cmds(attacker=("アビ1", "敵1"), support=("アビ3", "敵1")), balance, world
    )
    assert any("歴史の共鳴" in l for l in r1.lines)
    assert s1.battle.resonance_used
    # 2度目は発動しない
    s1.member_by_role("attacker").abilities[0].ready_in = 0
    s1.member_by_role("support").abilities[2].ready_in = 0
    s2, r2 = resolve_turn(
        s1, _cmds(attacker=("アビ1", "敵1"), support=("アビ3", "敵1")), balance, world
    )
    assert not any("歴史の共鳴" in l for l in r2.lines)


def test_no_resonance_without_gen_spell(battle_save, world, balance):
    s1, r1 = resolve_turn(battle_save, all_normal_commands(), balance, world)
    assert not s1.battle.resonance_used
    assert not any("歴史の共鳴" in l for l in r1.lines)


# ---- コスト・スキーマ ----------------------------------------------------


def test_field_effect_cost(balance):
    per_turn = float(balance["field"]["cost_per_turn"])
    assert effect_cost({"tag": "field", "name": "濡れ星", "turns": 3, "target": "enemy"}, balance) == per_turn * 3
    plain = effect_cost({"tag": "damage", "power": 1.0, "target": "enemy"}, balance)
    carried = effect_cost({"tag": "damage", "power": 1.0, "field": "雷紋", "target": "enemy"}, balance)
    assert carried == plain + per_turn * 2  # 添えタグの追加コスト


def test_spell_schema_accepts_field(balance):
    spell = {"name": "雷紋刻み", "desc": "残留タグを刻む", "ct": 2,
             "effects": [{"tag": "field", "name": "雷紋", "turns": 2, "target": "enemy"}]}
    assert validate_spell(spell, balance, 1, "support", False) == []


# ---- フルオート ----------------------------------------------------------


def test_full_auto_resolves_multiple_turns(tmp_path):
    from engine.save_io import load_save
    from engine.turn_runner import process_issue
    from tests.test_turn_runner import REPO, FakeGhApi, all_normal, body_from, make_issue, make_root

    root = make_root(tmp_path)
    gh = FakeGhApi()
    ai = AiClient(mock=True, fixtures_dir=ROOT / "fixtures/ai")
    body = body_from(all_normal(), free_text="フルオート 3")
    process_issue(make_issue(1, body), REPO, str(root), do_git=False, gh=gh, ai=ai)
    save = load_save(root / "save")
    assert save.battle is not None
    assert save.battle.turn == 4 or save.battle.result is not None  # 3ターン分解決
    reply = gh.comments[0][1]
    assert "フルオート: 3ターンを自動解決" in reply
    assert "ターン1〜3の結果" in reply


def test_full_auto_capped_by_balance(tmp_path):
    from engine.save_io import load_save
    from engine.turn_runner import process_issue
    from tests.test_turn_runner import REPO, FakeGhApi, all_normal, body_from, make_issue, make_root

    root = make_root(tmp_path)
    gh = FakeGhApi()
    ai = AiClient(mock=True, fixtures_dir=ROOT / "fixtures/ai")
    body = body_from(all_normal(), free_text="フルオート 99")
    process_issue(make_issue(1, body), REPO, str(root), do_git=False, gh=gh, ai=ai)
    save = load_save(root / "save")
    assert save.battle.turn <= 9 or save.battle.result is not None  # 上限8ターン


# ---- フォーム(誓約checkbox) --------------------------------------------


def test_parse_generate_body_with_oaths():
    body = (
        "### 対象メンバー\n\nアタッカー\n\n### スロット\n\nアビ1\n\n"
        "### 誓約\n\n- [x] HP30%以下でのみ発動(予算×1.6)\n- [ ] 1戦闘に1回だけ(予算×1.4)\n"
        "- [X] 使用後に自身1ターン行動不能(予算×1.5)\n\n"
        "### 詠唱文\n\n最後の力を束ねる一撃を\n"
    )
    parsed = parse_generate_body(body)
    assert parsed.errors == []
    assert parsed.oath_labels == [
        "HP30%以下でのみ発動(予算×1.6)",
        "使用後に自身1ターン行動不能(予算×1.5)",
    ]
