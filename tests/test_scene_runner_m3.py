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
