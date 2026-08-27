from engine.battle import resolve_turn, start_battle
from engine.models import Save
from tests.conftest import all_normal_commands


def test_save_roundtrip_fresh(fresh_save):
    assert Save.from_dict(fresh_save.to_dict()).to_dict() == fresh_save.to_dict()


def test_save_roundtrip_mid_battle(battle_save, balance):
    save, _ = resolve_turn(battle_save, all_normal_commands(), balance)
    assert Save.from_dict(save.to_dict()).to_dict() == save.to_dict()


def test_roundtrip_preserves_resolution(battle_save, balance, world):
    """dict化→復元しても以後の戦闘解決が同一(セーブ=完全な状態)。"""
    restored = Save.from_dict(battle_save.to_dict())
    a, _ = resolve_turn(battle_save, all_normal_commands(), balance)
    b, _ = resolve_turn(restored, all_normal_commands(), balance)
    assert a.to_dict() == b.to_dict()
