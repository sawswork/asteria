from engine.commands import Command, validate_commands
from tests.conftest import all_normal_commands


def test_all_normal_is_valid(battle_save, balance):
    errors = validate_commands(battle_save, battle_save.battle, all_normal_commands(), balance)
    assert errors == []


def test_ability_on_ct_rejected(battle_save, balance):
    member = battle_save.member_by_role("attacker")
    member.abilities[0].ready_in = 2
    cmds = all_normal_commands()
    cmds["attacker"] = Command("attacker", "アビ1", "自動")
    errors = validate_commands(battle_save, battle_save.battle, cmds, balance)
    assert len(errors) == 1
    assert errors[0].role == "attacker"
    assert "CT中" in errors[0].reason


def test_ult_without_gauge_rejected(battle_save, balance):
    cmds = all_normal_commands()
    cmds["attacker"] = Command("attacker", "奥義", "自動")
    errors = validate_commands(battle_save, battle_save.battle, cmds, balance)
    assert len(errors) == 1
    assert "ゲージ不足" in errors[0].reason


def test_ult_with_full_gauge_ok(battle_save, balance):
    battle_save.member_by_role("attacker").ult_gauge = 100
    cmds = all_normal_commands()
    cmds["attacker"] = Command("attacker", "奥義", "自動")
    assert validate_commands(battle_save, battle_save.battle, cmds, balance) == []


def test_nonexistent_enemy_target_rejected(battle_save, balance):
    cmds = all_normal_commands()
    cmds["attacker"] = Command("attacker", "通常攻撃", "敵2")
    errors = validate_commands(battle_save, battle_save.battle, cmds, balance)
    assert len(errors) == 1
    assert "存在しません" in errors[0].reason


def test_attack_on_ally_rejected(battle_save, balance):
    cmds = all_normal_commands()
    cmds["attacker"] = Command("attacker", "通常攻撃", "ヒーラー")
    errors = validate_commands(battle_save, battle_save.battle, cmds, balance)
    assert len(errors) == 1
    assert "味方を対象にできません" in errors[0].reason


def test_heal_on_enemy_rejected(battle_save, balance):
    cmds = all_normal_commands()
    cmds["healer"] = Command("healer", "アビ1", "敵1")
    errors = validate_commands(battle_save, battle_save.battle, cmds, balance)
    assert len(errors) == 1
    assert "敵を対象にできません" in errors[0].reason


def test_unknown_action_rejected(battle_save, balance):
    cmds = all_normal_commands()
    cmds["support"] = Command("support", "みだれうち", "自動")
    errors = validate_commands(battle_save, battle_save.battle, cmds, balance)
    assert len(errors) == 1
    assert "不明な行動" in errors[0].reason


def test_missing_command_rejected(battle_save, balance):
    cmds = all_normal_commands()
    del cmds["tank"]
    errors = validate_commands(battle_save, battle_save.battle, cmds, balance)
    assert len(errors) == 1
    assert errors[0].role == "tank"


def test_dead_member_command_not_error(battle_save, balance):
    battle_save.member_by_role("support").hp = 0
    errors = validate_commands(battle_save, battle_save.battle, all_normal_commands(), balance)
    assert errors == []


def test_wait_ignores_target(battle_save, balance):
    cmds = all_normal_commands()
    cmds["support"] = Command("support", "待機", "敵3")
    assert validate_commands(battle_save, battle_save.battle, cmds, balance) == []
