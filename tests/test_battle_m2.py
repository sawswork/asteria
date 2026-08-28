"""M2で追加した効果タグ(stun/dot/shield/debuff/scan/dispel)と進行(XP/レベル)のテスト。"""
from engine.battle import resolve_turn, xp_to_next
from engine.commands import Command
from engine.models import Buff
from tests.conftest import all_normal_commands


def _cmds(**overrides):
    cmds = all_normal_commands()
    for role, (action, target) in overrides.items():
        cmds[role] = Command(role, action, target)
    return cmds


def _set_effect(save, role, idx, effects, name="試験技"):
    ability = save.member_by_role(role).abilities[idx]
    ability.effects = effects
    ability.name = name
    ability.ct = 0
    return ability


def test_stun_skips_enemy_and_builds_resist(battle_save, balance):
    _set_effect(battle_save, "attacker", 0, [{"tag": "stun", "turns": 2, "target": "enemy"}], "麻痺弾")
    s1, r1 = resolve_turn(battle_save, _cmds(attacker=("アビ1", "自動")), balance)
    assert any("行動不能" in l for l in r1.lines)
    assert not any(l.startswith("星蝕の仔狼の") for l in r1.lines)  # 敵は行動できない
    assert s1.battle.enemies[0].cc_resist["stun"] == 1
    # 2回目は耐性で1ターンに減衰(2-1=1)
    s1.member_by_role("attacker").abilities[0].ready_in = 0
    s2, r2 = resolve_turn(s1, _cmds(attacker=("アビ1", "自動")), balance)
    assert s2.battle.enemies[0].cc_resist["stun"] == 2
    # 3回目は完全レジスト
    s2.member_by_role("attacker").abilities[0].ready_in = 0
    s2.battle.enemies[0].stunned_turns = 0
    _, r3 = resolve_turn(s2, _cmds(attacker=("アビ1", "自動")), balance)
    assert any("振りほどいた" in l for l in r3.lines)


def test_dot_ticks_and_expires(battle_save, balance):
    _set_effect(battle_save, "attacker", 0, [{"tag": "dot", "power": 0.5, "turns": 2, "target": "enemy"}], "毒星")
    s1, r1 = resolve_turn(battle_save, _cmds(attacker=("アビ1", "自動")), balance)
    assert any("継続ダメージ状態" in l for l in r1.lines)
    assert any("継続ダメージで" in l for l in r1.lines)  # ターン終了時に発火
    assert len(s1.battle.enemies[0].dots) == 1
    s2, _ = resolve_turn(s1, all_normal_commands(), balance)
    assert s2.battle.enemies[0].dots == []  # 2ターンで消滅


def test_shield_absorbs_damage(battle_save, balance):
    _set_effect(battle_save, "support", 0, [{"tag": "shield", "power": 3.0, "target": "party"}], "星の障壁")
    battle_save.member_by_role("tank").hate = 9999  # 敵の攻撃をタンクへ
    s1, r1 = resolve_turn(battle_save, _cmds(support=("アビ1", "自動")), balance)
    assert any("シールド" in l for l in r1.lines)
    tank = s1.member_by_role("tank")
    assert tank.hp == tank.max_hp  # 吸収されてHP無傷(シールド30 > 敵の一撃)
    assert tank.shield < 30  # 減っている


def test_debuff_lowers_enemy_stats(battle_save, balance):
    _set_effect(battle_save, "support", 0, [{"tag": "debuff", "stat": "atk", "mult": 0.7, "turns": 2, "target": "enemy"}], "弱化の歌")
    s1, r1 = resolve_turn(battle_save, _cmds(support=("アビ1", "自動")), balance)
    enemy = s1.battle.enemies[0]
    assert any("低下" in l for l in r1.lines)
    assert enemy.eff_atk() < enemy.atk


def test_dispel_removes_enemy_buffs_keeps_debuffs(battle_save, balance):
    enemy = battle_save.battle.enemies[0]
    enemy.buffs.append(Buff(stat="atk", mult=1.5, turns_left=3))
    enemy.buffs.append(Buff(stat="def", mult=0.8, turns_left=3))  # こちらは味方が付けた弱体
    _set_effect(battle_save, "support", 0, [{"tag": "dispel", "target": "enemy"}], "浄化")
    s1, r1 = resolve_turn(battle_save, _cmds(support=("アビ1", "自動")), balance)
    remaining = s1.battle.enemies[0].buffs
    assert any("打ち消した" in l for l in r1.lines)
    assert all(b.mult <= 1.0 for b in remaining)
    assert any(b.mult == 0.8 for b in remaining)


def test_scan_reveals_enemy(battle_save, balance):
    _set_effect(battle_save, "support", 0, [{"tag": "scan", "target": "enemy"}], "星の瞳")
    s1, r1 = resolve_turn(battle_save, _cmds(support=("アビ1", "敵1")), balance)
    assert s1.battle.enemies[0].id in s1.battle.scanned
    assert any("分析" in l for l in r1.lines)
    assert any("ヘイト" in l for l in r1.lines)


def test_negative_hate_floors_at_zero(battle_save, balance):
    _set_effect(battle_save, "support", 0, [{"tag": "hate", "amount": -50, "target": "self"}], "気配遮断")
    s1, r1 = resolve_turn(battle_save, _cmds(support=("アビ1", "自動")), balance)
    assert s1.member_by_role("support").hate == 0.0
    assert any("気配を消した" in l for l in r1.lines)


def test_stunned_member_skips_action(battle_save, balance):
    battle_save.member_by_role("attacker").stunned_turns = 1
    s1, r1 = resolve_turn(battle_save, all_normal_commands(), balance)
    assert any("ソラは行動不能で動けない" in l for l in r1.lines)
    assert s1.member_by_role("attacker").stunned_turns == 0  # ターン終了で解除


def test_victory_grants_xp_and_levels_up(battle_save, balance):
    battle_save.battle.enemies[0].hp = 1
    battle_save.xp = xp_to_next(1, balance) - 50  # 勝利XP(standard=100)で確実に閾値を超える
    s1, r1 = resolve_turn(battle_save, all_normal_commands(), balance)
    assert r1.result == "victory"
    assert s1.level == 2
    assert s1.spell_tokens == 1
    assert any("レベルアップ" in l for l in r1.lines)
    # 役割別成長が入る
    assert s1.member_by_role("tank").max_hp > battle_save.member_by_role("tank").max_hp


def test_victory_without_levelup_keeps_level(battle_save, balance):
    battle_save.battle.enemies[0].hp = 1
    battle_save.battle.enemies[0].xp = 10
    s1, r1 = resolve_turn(battle_save, all_normal_commands(), balance)
    assert r1.result == "victory"
    assert s1.level == 1
    assert s1.xp == 10
    assert s1.spell_tokens == 0


def test_usage_and_kill_tracking(battle_save, balance):
    battle_save.battle.enemies[0].hp = 1
    s1, _ = resolve_turn(battle_save, _cmds(attacker=("アビ1", "自動")), balance)
    ability = s1.member_by_role("attacker").abilities[0]
    assert ability.usage_count == 1
    assert ability.kills == 1