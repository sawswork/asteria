"""M3: 素材パイプラインとシーンSVGのテスト(素材はテスト内で合成生成)。"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from engine import assets
from engine.battle import resolve_turn, start_battle
from engine.scene import SCENE_MAX_BYTES, build_scene_svg
from tests.conftest import all_normal_commands

GREEN = (0, 255, 0)


def _green_image(size=(400, 300)) -> Image.Image:
    return Image.new("RGB", size, GREEN)


def _make_raw(tmp_path: Path, with_two_wings: bool = True) -> Path:
    raw = tmp_path / "assets/raw"
    raw.mkdir(parents=True)
    # 背景(緑ではない夜空)
    bg = Image.new("RGB", (900, 500), (16, 20, 48))
    d = ImageDraw.Draw(bg)
    for i in range(40):
        d.point((i * 22 % 900, (i * 37) % 250), fill=(230, 235, 255))
    bg.save(raw / "background.png")
    # 胴体(緑背景+茶色の楕円)
    body = _green_image((420, 360))
    d = ImageDraw.Draw(body)
    d.ellipse([80, 100, 340, 300], fill=(90, 60, 40))
    d.ellipse([150, 40, 270, 160], fill=(90, 60, 40))
    body.save(raw / "body.png")
    # 可動パーツ(緑背景+翼2枚=連結成分2つ)
    part = _green_image((500, 260))
    d = ImageDraw.Draw(part)
    d.polygon([(30, 130), (200, 30), (200, 230)], fill=(60, 40, 80))
    if with_two_wings:
        d.polygon([(470, 130), (300, 30), (300, 230)], fill=(60, 40, 80))
    part.save(raw / "wing.png")
    return tmp_path


def test_chroma_key_removes_green():
    img = _green_image((50, 50))
    d = ImageDraw.Draw(img)
    d.rectangle([10, 10, 30, 30], fill=(120, 50, 60))
    keyed = assets.chroma_key(img)
    arr = np.asarray(keyed)
    assert arr[0, 0, 3] == 0  # 緑は透過
    assert arr[20, 20, 3] == 255  # 被写体は残る


def test_pipeline_generates_parts_and_manifest(tmp_path):
    root = _make_raw(tmp_path)
    manifest = assets.process_raw_assets(root)
    assert manifest is not None
    parts_dir = root / "assets/parts"
    assert (parts_dir / "background.webp").exists()
    assert (parts_dir / "body.webp").exists()
    assert (parts_dir / "part1.webp").exists()
    assert (parts_dir / "part2.webp").exists()  # 翼2枚が連結成分で分離される
    assert manifest["total_b64_bytes"] <= assets.SCENE_BUDGET_MAX
    assert len(manifest["parts"]) == 2
    loaded = assets.load_manifest(root)
    assert loaded == manifest


def test_pipeline_budget_ladder_shrinks_huge_input(tmp_path, monkeypatch):
    raw = tmp_path / "assets/raw"
    raw.mkdir(parents=True)
    # ノイズだらけの巨大背景(圧縮が効きにくい)。梯子が実際に段階を下げることを検証する
    rng = np.random.default_rng(1)
    noisy = rng.integers(0, 255, size=(1400, 2600, 3), dtype=np.uint8)
    Image.fromarray(noisy, "RGB").save(raw / "background.png")
    monkeypatch.setattr(assets, "SCENE_BUDGET_TARGET", 120 * 1024)  # 最上段では収まらない目標
    manifest = assets.process_raw_assets(tmp_path)
    assert manifest is not None
    assert manifest["total_b64_bytes"] <= 120 * 1024
    assert manifest["quality"] < assets.QUALITY_LADDER[0] or manifest["scale"] < 1.0  # 段階ダウンが働いた


def test_pipeline_raises_when_budget_impossible(tmp_path, monkeypatch):
    raw = tmp_path / "assets/raw"
    raw.mkdir(parents=True)
    rng = np.random.default_rng(2)
    noisy = rng.integers(0, 255, size=(900, 1600, 3), dtype=np.uint8)
    Image.fromarray(noisy, "RGB").save(raw / "background.png")
    monkeypatch.setattr(assets, "SCENE_BUDGET_TARGET", 1000)
    monkeypatch.setattr(assets, "SCENE_BUDGET_MAX", 2000)  # 最小設定でも入らない上限
    with pytest.raises(ValueError):
        assets.process_raw_assets(tmp_path)


def test_pipeline_without_raw_returns_none(tmp_path):
    assert assets.process_raw_assets(tmp_path) is None
    assert assets.has_raw_assets(tmp_path) is False


def test_placeholder_scene_is_valid_and_small(battle_save, world, tmp_path):
    svg = build_scene_svg(battle_save, world, str(tmp_path))  # 素材なし=プレースホルダ
    ET.fromstring(svg)
    assert len(svg.encode()) < 60 * 1024
    assert "animate" in svg  # SMIL演出
    assert "星蝕の仔狼" in svg
    assert battle_save.battle.name in svg
    assert "ソラ" in svg  # パーティシルエットの名前
    assert "http" not in svg.replace("http://www.w3.org/2000/svg", "")


def test_scene_with_parts_embeds_base64(battle_save, world, tmp_path):
    manifest = assets.process_raw_assets(_make_raw(tmp_path))
    svg = build_scene_svg(battle_save, world, str(tmp_path))
    ET.fromstring(svg)
    assert "data:image/webp;base64," in svg
    assert svg.count('type="rotate"') >= 2  # 翼2枚がpivot中心で羽ばたく
    assert len(svg.encode()) <= SCENE_MAX_BYTES
    # pivotヒューリスティック: 左向きの翼=右端が付け根 / 右向きの翼=左端が付け根
    pivots = sorted(p["pivot"][0] for p in manifest["parts"])
    assert pivots[0] == 0
    assert pivots[1] >= manifest["parts"][0]["w"] - 2


def test_pipeline_cleans_stale_parts(tmp_path):
    root = _make_raw(tmp_path)
    assets.process_raw_assets(root)
    assert (root / "assets/parts/part2.webp").exists()
    # 翼1枚の素材に差し替えたら、旧part2は残らない
    (root / "assets/raw/wing.png").unlink()
    part = _green_image((300, 200))
    d = ImageDraw.Draw(part)
    d.polygon([(30, 100), (200, 30), (200, 170)], fill=(60, 40, 80))
    part.save(root / "assets/raw/wing.png")
    manifest = assets.process_raw_assets(root)
    assert len(manifest["parts"]) == 1
    assert not (root / "assets/parts/part2.webp").exists()


def test_exif_orientation_respected(tmp_path):
    raw = tmp_path / "assets/raw"
    raw.mkdir(parents=True)
    body = _green_image((400, 200))  # 横長で保存するがEXIFで縦向き指定
    d = ImageDraw.Draw(body)
    d.ellipse([50, 40, 350, 160], fill=(90, 60, 40))
    from PIL import Image as PILImage

    exif = PILImage.Exif()
    exif[0x0112] = 6  # Orientation: 90度回転
    body.save(raw / "body.jpg", exif=exif)
    manifest = assets.process_raw_assets(tmp_path)
    b = manifest["body"]
    assert b["h"] > b["w"]  # 縦向きに正されている


def test_board_has_turn_replay_animation(battle_save, balance, world):
    from engine.board import build_board_svg

    save, _ = resolve_turn(battle_save, all_normal_commands(), balance)
    svg = build_board_svg(save, world, balance)
    assert 'fill="freeze"' in svg  # 直近ターンのログが順次表示される
    ET.fromstring(svg)


def test_scene_requires_battle(fresh_save, world, tmp_path):
    with pytest.raises(ValueError):
        build_scene_svg(fresh_save, world, str(tmp_path))
