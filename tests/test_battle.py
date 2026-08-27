import copy

from engine.battle import resolve_turn, start_battle
from engine.commands import Command
from tests.conftest import all_normal_commands


def _cmds(**overrides):
    cmds = all_normal_commands()
    for role, (action, target) in overrides.items():
        cmds[role] = Command(role, action, target)
    return cmds


def test_resolve_turn_is_pure(battle_save, balance):
    before = battle_save.to_dict()
    resolve_turn(battle_save, all_normal_commands(), balance)
    assert battle_save.to_dict() == before


def test_deterministic_given_same_save(battle_save, balance):
    a, ra = resolve_turn(battle_save, all_normal_commands(), balance)
    b, rb = resolve_turn(battle_save, all_normal_commands(), balance)
    assert a.to_dict() == b.to_dict()
    assert ra.lines == rb.lines


def test_agi_order(battle_save, balance):
    # AGI: ソラ14 > リュノ12 > 狼11 > ミオ10 > ガンテ6
    _, report = resolve_turn(battle_save, all_normal_commands(), balance)
    action_lines = [l for l in report.lines if "!" in l]
    order = []
    for line in action_lines:
        order.append(line.split("の")[0])
    names = [n for n in order if n in ("ソラ", "リュノ", "ガンテ", "ミオ", "星蝕")]
    assert names[0] == "ソラ"
    assert names[1] == "リュノ"
    assert names[2] == "星蝕"  # 星蝕の仔狼
    assert names[3] == "ミオ"
    assert names[4] == "ガンテ"


def test_ct_set_and_ticks_down(battle_save, balance):
    new, _ = resolve_turn(battle_save, _cmds(attacker=("アビ1", "自動")), balance)
    ability = new.member_by_role("attacker").abilities[0]
    # ct=2: 使用ターン終了時に1減算 → 残り1(次ターン使用不可、その次で解禁)
    assert ability.ready_in == 1
    new2, _ = resolve_turn(new, all_normal_commands(), balance)
    assert new2.member_by_role("attacker").abilities[0].ready_in == 0


def test_gauge_gains(battle_save, balance):
    new, _ = resolve_turn(battle_save, _cmds(support=("待機", "自動")), balance)
    support = new.member_by_role("support")
    assert support.ult_gauge == 30  # 待機+30
    tank = new.member_by_role("tank")
    assert tank.ult_gauge >= 25  # 通常攻撃+25(被弾があればさらに+10)


def test_ult_consumes_gauge(battle_save, balance):
    battle_save.member_by_role("attacker").ult_gauge = 100
    battle_save.member_by_role("tank").hate = 9999  # 敵の攻撃をタンクへ逸らし被弾ゲージを排除
    new, report = resolve_turn(battle_save, _cmds(attacker=("奥義", "自動")), balance)
    assert new.member_by_role("attacker").ult_gauge == 0
    assert any("彗星烈斬" in l for l in report.lines)


def test_taunt_locks_enemy_on_tank(battle_save, balance):
    # アタッカーのヘイトを圧倒的に高くしても、挑発ロック中はタンクが狙われる
    battle_save.member_by_role("attacker").hate = 9999
    new, report = resolve_turn(battle_save, _cmds(tank=("アビ1", "自動")), balance)
    assert new.battle.taunt_holder_id == "gante"
    # 挑発はタンクの行動(AGI最下位)なので、このターンの敵の攻撃は挑発前=ヘイト最大のアタッカー狙い
    enemy_line = next(l for l in report.lines if l.startswith("星蝕の仔狼"))
    assert "ソラ" in enemy_line
    # 次のターン: ロック有効中(残り1)は必ずタンクを狙う
    new2, report2 = resolve_turn(new, all_normal_commands(), balance)
    enemy_line2 = next(l for l in report2.lines if l.startswith("星蝕の仔狼"))
    assert "ガンテ" in enemy_line2


def test_taunt_lock_expires(battle_save, balance):
    battle_save.member_by_role("attacker").hate = 9999
    s1, _ = resolve_turn(battle_save, _cmds(tank=("アビ1", "自動")), balance)
    assert s1.battle.taunt_turns_left == 1
    s2, _ = resolve_turn(s1, all_normal_commands(), balance)
    assert s2.battle.taunt_turns_left == 0
    assert s2.battle.taunt_holder_id is None
    # ロックが切れればヘイト比較に戻る(挑発のヘイトスパイク自体は残るため、上回れば狙いが外れる)
    s2.member_by_role("attacker").hate = s2.member_by_role("tank").hate + 1000
    _, report3 = resolve_turn(s2, all_normal_commands(), balance)
    enemy_line = next(l for l in report3.lines if l.startswith("星蝕の仔狼"))
    assert "ソラ" in enemy_line


def test_enemy_targets_max_hate(battle_save, balance):
    battle_save.member_by_role("healer").hate = 500
    _, report = resolve_turn(battle_save, all_normal_commands(), balance)
    enemy_line = next(l for l in report.lines if l.startswith("星蝕の仔狼"))
    assert "ミオ" in enemy_line


def test_hate_accumulates_from_damage(battle_save, balance):
    initial = battle_save.member_by_role("attacker").hate
    new, _ = resolve_turn(battle_save, all_normal_commands(), balance)
    assert new.member_by_role("attacker").hate > initial


def test_heal_restores_hp(battle_save, balance):
    healer = battle_save.member_by_role("healer")
    hurt = battle_save.member_by_role("attacker")
    hurt.hp = 40
    new, report = resolve_turn(battle_save, _cmds(healer=("アビ1", "自動")), balance)
    healed = new.member_by_role("attacker")
    # 自動対象=HP割合最小のアタッカー。敵に殴られた分を差し引いても回復が上回るはず
    assert any("星灯の癒し" in l for l in report.lines)
    assert healed.hp > 40 - 20


def test_buff_applies_and_expires(battle_save, balance):
    s1, _ = resolve_turn(battle_save, _cmds(support=("アビ1", "自動")), balance)
    attacker = s1.member_by_role("attacker")
    assert any(b.stat == "atk" for b in attacker.buffs)
    assert attacker.eff_atk() > attacker.atk
    s2, _ = resolve_turn(s1, all_normal_commands(), balance)
    assert not s2.member_by_role("attacker").buffs  # turns=2 → 2ターン目終了で消滅


def test_strong_attack_on_schedule(battle_save, balance):
    save = battle_save
    for expected_turn in (1, 2, 3):
        save, report = resolve_turn(save, all_normal_commands(), balance)
        strong = any("星蝕の牙" in l for l in report.lines)
        assert strong == (expected_turn == 3)


def test_victory(battle_save, balance):
    battle_save.battle.enemies[0].hp = 5
    new, report = resolve_turn(battle_save, all_normal_commands(), balance)
    assert report.result == "victory"
    assert new.battle.active is False
    assert new.stats["victories"] == 1
    assert any("勝利" in line for line in new.journal)


def test_defeat(battle_save, balance):
    for m in battle_save.party:
        m.hp = 1
        m.agi = 1  # 敵が先手を取り全員は倒せないが…
    battle_save.battle.enemies[0].atk = 999
    # 敵1体は1ターンに1回しか行動しないため、複数ターン回す
    save = battle_save
    result = None
    for _ in range(6):
        save, report = resolve_turn(save, all_normal_commands(), balance)
        result = report.result
        if result:
            break
    assert result == "defeat"
    assert save.stats["defeats"] == 1


def test_dead_member_does_not_act(battle_save, balance):
    battle_save.member_by_role("support").hp = 0
    _, report = resolve_turn(battle_save, all_normal_commands(), balance)
    assert not any(l.startswith("リュノ") for l in report.lines)


def test_start_battle_restores_party(world, balance, battle_save):
    for m in battle_save.party:
        m.hp = 10
        m.abilities[0].ready_in = 2
    battle_save.battle.result = "victory"
    battle_save.battle.active = False
    renewed = start_battle(battle_save, world, balance)
    for m in renewed.party:
        assert m.hp == m.max_hp
        assert all(a.ready_in == 0 for a in m.abilities)
        assert m.buffs == []
    assert renewed.battle.active
    assert renewed.battle.turn == 1


def test_rng_counter_advances_and_recorded(battle_save, balance):
    new, _ = resolve_turn(battle_save, all_normal_commands(), balance)
    assert new.rng_counter > battle_save.rng_counter
    assert new.rng_seed == battle_save.rng_seed
