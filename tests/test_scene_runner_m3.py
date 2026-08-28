"""M3: ランナー配線(戦闘開始時のシーン生成・README表示・Geminiフォールバック)。"""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path

from engine.gemini import GeminiClient
from engine.models import Enemy
from engine.save_io import load_save, write_save
from tests.test_m2_flows import mock_ai, run
from tests.test_turn_runner import all_normal, body_from, make_issue, make_root


def test_runner_writes_scene_on_battle_start(tmp_path):
    root = make_root(tmp_path)
    run(root, make_issue(1, body_from(all_normal())))
    scene = root / "assets/scene.svg"
    assert scene.exists()
    text = scene.read_text(encoding="utf-8")
    assert "星蝕の仔狼" in text
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert "assets/scene.svg?v=" in readme  # 戦闘中はシーンがREADMEに載る


def test_scene_not_rebuilt_mid_battle(tmp_path):
    root = make_root(tmp_path)
    run(root, make_issue(1, body_from(all_normal())))
    scene = root / "assets/scene.svg"
    first = scene.read_bytes()
    run(root, make_issue(2, body_from(all_normal())))  # 戦闘継続ターン
    assert scene.read_bytes() == first  # シーンは戦闘開始時のみ生成


def test_readme_hides_scene_after_battle_ends(tmp_path):
    root = make_root(tmp_path)
    run(root, make_issue(1, body_from(all_normal())))
    save = load_save(root / "save")
    save.battle.enemies[0].hp = 1
    write_save(save, root / "save")
    run(root, make_issue(2, body_from(all_normal())))  # 勝利
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert "assets/scene.svg" not in readme  # 非戦闘時はボードのみ


def test_new_battle_regenerates_scene(tmp_path):
    root = make_root(tmp_path)
    run(root, make_issue(1, body_from(all_normal())))
    first = (root / "assets/scene.svg").read_text(encoding="utf-8")
    save = load_save(root / "save")
    save.battle.enemies[0].hp = 1
    write_save(save, root / "save")
    run(root, make_issue(2, body_from(all_normal())))  # 勝利
    run(root, make_issue(3, body_from(all_normal())))  # 新戦闘(モック敵: 影喰いの豹)
    second = (root / "assets/scene.svg").read_text(encoding="utf-8")
    assert second != first
    assert "影喰いの豹" in second


def test_gemini_client_without_key_is_unavailable(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert GeminiClient().available() is False


def test_scene_failure_removes_stale_scene(tmp_path, monkeypatch):
    """シーン生成に失敗したら前の戦闘のシーンは消え、READMEにも載らない。"""
    root = make_root(tmp_path)
    run(root, make_issue(1, body_from(all_normal())))
    assert (root / "assets/scene.svg").exists()
    # 勝利→次の戦闘開始時にシーン生成を必ず失敗させる
    save = load_save(root / "save")
    save.battle.enemies[0].hp = 1
    write_save(save, root / "save")
    run(root, make_issue(2, body_from(all_normal())))  # 勝利(戦闘終了)
    import engine.scene as scene_mod

    def boom(*a, **k):
        raise RuntimeError("scene broken")

    monkeypatch.setattr(scene_mod, "build_scene_svg", boom)
    run(root, make_issue(3, body_from(all_normal())))  # 新戦闘(シーン失敗)
    assert not (root / "assets/scene.svg").exists()
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert "assets/scene.svg" not in readme


def test_gemini_regenerates_for_new_enemy_and_respects_user_materials(tmp_path, monkeypatch, world):
    """AI生成素材は敵が変わると作り直す。ユーザー素材(マーカー無し)は温存する。"""
    import base64 as b64
    from engine.models import Enemy
    from engine.turn_runner import _maybe_generate_materials
    from engine.save_io import load_save as _load

    calls = {"n": 0}
    png_1x1 = b64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )

    class FakeResp:
        def read(self):
            calls["n"] += 1
            return json.dumps(
                {"candidates": [{"content": {"parts": [{"inlineData": {"data": b64.b64encode(png_1x1).decode()}}]}}]}
            ).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=0: FakeResp())

    root = make_root(tmp_path)
    save = load_save(root / "save")

    def battle_with(enemy_id: str):
        from engine.battle import start_battle

        enemy = Enemy(id=enemy_id, name="敵", title="", max_hp=10, hp=10, atk=1, df=1, agi=1,
                      actions={"normal": {"name": "n", "effects": []}})
        return start_battle(save, world, {"hate": {"initial": 5}}, enemies=[enemy], battle_name="b")

    s1 = battle_with("enemy_a")
    _maybe_generate_materials(root, s1, world)
    assert calls["n"] == 3  # 3枚生成
    assert (root / "assets/raw/.generated.json").exists()
    _maybe_generate_materials(root, s1, world)
    assert calls["n"] == 3  # 同じ敵では再生成しない
    s2 = battle_with("enemy_b")
    _maybe_generate_materials(root, s2, world)
    assert calls["n"] == 6  # 敵が変われば作り直す
    # ユーザー素材(マーカー削除=手置き扱い)は温存
    (root / "assets/raw/.generated.json").unlink()
    s3 = battle_with("enemy_c")
    _maybe_generate_materials(root, s3, world)
    assert calls["n"] == 6  # 生成しない


def test_gemini_generates_assets_with_mocked_api(monkeypatch, tmp_path, world):
    import base64

    png_1x1 = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )

    class FakeResp:
        def __init__(self) -> None:
            self._body = json.dumps(
                {"candidates": [{"content": {"parts": [{"inlineData": {"data": base64.b64encode(png_1x1).decode()}}]}}]}
            ).encode()

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=0: FakeResp())
    enemy = Enemy(id="e", name="試験獣", title="", max_hp=1, hp=1, atk=1, df=1, agi=1, actions={})
    client = GeminiClient(api_key="test-key")
    count = client.generate_enemy_assets(enemy, world, tmp_path / "raw")
    assert count == 3
    assert (tmp_path / "raw/background.png").exists()
    assert (tmp_path / "raw/body.png").exists()
    assert (tmp_path / "raw/part.png").exists()


def test_gemini_failure_returns_zero(monkeypatch, tmp_path, world):
    def boom(req, timeout=0):
        raise OSError("network down")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    enemy = Enemy(id="e", name="試験獣", title="", max_hp=1, hp=1, atk=1, df=1, agi=1, actions={})
    assert GeminiClient(api_key="k").generate_enemy_assets(enemy, world, tmp_path / "raw") == 0
