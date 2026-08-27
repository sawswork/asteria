from engine.screen import render_readme


def test_readme_board_url_relative_with_cache_key(battle_save, world):
    md = render_readme(battle_save, world, "owner/repo", "t3-i12")
    # 相対URL+キャッシュ回避クエリ(非公開リポジトリでも表示され、camoキャッシュも回避される)
    assert "![戦況ボード](assets/board.svg?v=t3-i12)" in md
    assert "raw.githubusercontent.com" not in md
    assert "issues/new?template=turn.yml" in md
    assert "アステリア" in md


def test_readme_status_lines(fresh_save, battle_save, world):
    assert "拠点で休息中" in render_readme(fresh_save, world, "o/r", "x")
    assert "戦闘中" in render_readme(battle_save, world, "o/r", "x")
    battle_save.battle.active = False
    battle_save.battle.result = "victory"
    assert "勝利" in render_readme(battle_save, world, "o/r", "x")


def test_readme_local_sha_uses_relative_path(battle_save, world):
    md = render_readme(battle_save, world, "o/r", "local")
    assert "![戦況ボード](assets/board.svg)" in md
    assert "raw.githubusercontent.com" not in md


def test_readme_prefill_link_is_urlencoded(battle_save, world):
    md = render_readme(battle_save, world, "o/r", "x")
    assert "attacker_action=%E9%80%9A%E5%B8%B8%E6%94%BB%E6%92%83" in md  # 通常攻撃
