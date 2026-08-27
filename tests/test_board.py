import xml.etree.ElementTree as ET

from engine.battle import resolve_turn
from engine.board import BOARD_MAX_BYTES, build_board_svg
from tests.conftest import all_normal_commands


def _assert_self_contained(svg: str) -> None:
    assert "http" not in svg.replace("http://www.w3.org/2000/svg", "")
    assert "href" not in svg
    assert "<image" not in svg
    assert "url(" not in svg


def test_board_svg_valid_and_small(battle_save, world, balance):
    svg = build_board_svg(battle_save, world, balance)
    assert len(svg.encode("utf-8")) < BOARD_MAX_BYTES
    ET.fromstring(svg)  # well-formed XML
    _assert_self_contained(svg)
    assert "アステリア" in svg  # 世界名はworld.json由来
    assert "ソラ" in svg and "ガンテ" in svg
    assert "星蝕の仔狼" in svg


def test_board_svg_no_battle(fresh_save, world, balance):
    svg = build_board_svg(fresh_save, world, balance)
    ET.fromstring(svg)
    assert "拠点で休息中" in svg


def test_board_svg_mid_battle_states(battle_save, world, balance):
    save = battle_save
    for _ in range(3):
        save, report = resolve_turn(save, all_normal_commands(), balance)
        if report.result:
            break
        svg = build_board_svg(save, world, balance)
        ET.fromstring(svg)
        assert len(svg.encode("utf-8")) < BOARD_MAX_BYTES


def test_board_svg_victory_banner(battle_save, world, balance):
    battle_save.battle.enemies[0].hp = 1
    save, report = resolve_turn(battle_save, all_normal_commands(), balance)
    assert report.result == "victory"
    svg = build_board_svg(save, world, balance)
    assert "勝利" in svg


def test_board_hides_taunt_lock_of_dead_holder(battle_save, world, balance):
    tank = battle_save.member_by_role("tank")
    battle_save.battle.taunt_holder_id = tank.id
    battle_save.battle.taunt_turns_left = 2
    svg_alive = build_board_svg(battle_save, world, balance)
    assert "狙い固定" in svg_alive
    tank.hp = 0  # 保持者死亡 → 敵AIはロックを無視するため表示も消す
    svg_dead = build_board_svg(battle_save, world, balance)
    assert "狙い固定" not in svg_dead


def test_board_shows_ct_state(battle_save, world, balance):
    battle_save.member_by_role("attacker").abilities[0].ready_in = 2
    svg = build_board_svg(battle_save, world, balance)
    assert "CT2" in svg
