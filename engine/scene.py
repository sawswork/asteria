"""シーンSVG(戦闘開始時のみ生成)。

素材(assets/parts/)があればbase64内包で合成し、無ければ手続き的プレースホルダ
(星空+ティア別シルエット)で全機能を成立させる。SMILで登場・待機・羽ばたき・
環境演出を標準装備。自己完結(外部リソース参照なし)・上限1MB。
"""
from __future__ import annotations

from typing import Any, Optional

from . import assets
from .commands import ROLE_LABELS
from .models import Enemy, Save

SCENE_MAX_BYTES = 1024 * 1024
W, H = 760, 420

FONT = "'Hiragino Kaku Gothic ProN','Hiragino Sans','Yu Gothic UI','Meiryo','Noto Sans CJK JP',sans-serif"
ROLE_COLORS = {"attacker": "#e05252", "support": "#5ea0ff", "tank": "#c9a15a", "healer": "#3ecf6e"}


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _procedural_background(world: dict[str, Any]) -> str:
    stars = []
    seed = 12345
    for i in range(46):
        seed = (seed * 1103515245 + 12345) % (2**31)
        x = seed % W
        seed = (seed * 1103515245 + 12345) % (2**31)
        y = seed % 230
        r = 0.8 + (i % 3) * 0.5
        dur = 2.0 + (i % 5) * 0.9
        stars.append(
            f'<circle cx="{x}" cy="{y}" r="{r}" fill="#dfe8ff">'
            f'<animate attributeName="opacity" values="0.2;1;0.2" dur="{dur:g}s" '
            f'begin="{(i % 7) * 0.4:g}s" repeatCount="indefinite"/></circle>'
        )
    return (
        '<defs><linearGradient id="sky" x1="0" y1="0" x2="0" y2="1">'
        '<stop offset="0" stop-color="#0a1030"/><stop offset="0.7" stop-color="#1a2348"/>'
        '<stop offset="1" stop-color="#2c3358"/></linearGradient>'
        '<radialGradient id="moon" cx="0.5" cy="0.5" r="0.5">'
        '<stop offset="0" stop-color="#fdf6d8" stop-opacity="0.95"/>'
        '<stop offset="1" stop-color="#fdf6d8" stop-opacity="0"/></radialGradient></defs>'
        f'<rect width="{W}" height="{H}" fill="url(#sky)"/>'
        '<circle cx="120" cy="80" r="60" fill="url(#moon)"/>'
        + "".join(stars)
        + f'<ellipse cx="{W / 2:g}" cy="{H + 40}" rx="{W * 0.75:g}" ry="90" fill="#141a33"/>'
    )


def _enemy_silhouette(enemy: Enemy) -> str:
    """ティア別スケールの獣シルエット+光る眼。素材が無い時の敵ビジュアル。"""
    scale = {"minion": 0.75, "standard": 1.0, "elite": 1.2, "boss": 1.5}.get(enemy.tier, 1.0)
    return (
        f'<g transform="translate(540 258) scale({scale:g})">'
        '<g>'
        '<animateTransform attributeName="transform" type="translate" additive="sum" '
        'values="0 0; 0 -7; 0 0" dur="3.4s" repeatCount="indefinite"/>'
        # 胴体・頭・耳・尾(汎用の獣シルエット)
        '<ellipse cx="0" cy="0" rx="95" ry="58" fill="#0b0e1c"/>'
        '<circle cx="-85" cy="-38" r="40" fill="#0b0e1c"/>'
        '<path d="M -112 -66 L -100 -30 L -78 -58 Z" fill="#0b0e1c"/>'
        '<path d="M -86 -72 L -72 -38 L -52 -60 Z" fill="#0b0e1c"/>'
        '<path d="M 80 -20 Q 150 -60 140 10 Q 120 40 88 22 Z" fill="#0b0e1c"/>'
        # 光る眼
        '<circle cx="-98" cy="-42" r="5" fill="#ffd75e">'
        '<animate attributeName="opacity" values="1;0.35;1" dur="2.6s" repeatCount="indefinite"/></circle>'
        '<circle cx="-76" cy="-44" r="5" fill="#ffd75e">'
        '<animate attributeName="opacity" values="1;0.35;1" dur="2.6s" begin="0.2s" repeatCount="indefinite"/></circle>'
        "</g></g>"
    )


def _enemy_from_parts(root: str, manifest: dict[str, Any]) -> str:
    """胴体+可動パーツを素材の生解像度で組み、全体を一括スケールして枠内に収める。"""
    parts_svg: list[str] = []
    body = manifest.get("body")
    part_list = list(manifest.get("parts", []))

    def flap(pivot_x: float, pivot_y: float, phase: float) -> str:
        return (
            f'<animateTransform attributeName="transform" type="rotate" additive="sum" '
            f'values="0 {pivot_x:g} {pivot_y:g}; -9 {pivot_x:g} {pivot_y:g}; 0 {pivot_x:g} {pivot_y:g}" '
            f'dur="2.2s" begin="{phase:g}s" repeatCount="indefinite"/>'
        )

    # 生解像度での寸法と肩の取り付け点
    bw = body["w"] if body else 200
    bh = body["h"] if body else 180
    attach_points = [(-bw * 0.22, -bh * 0.55 + 40), (bw * 0.22, -bh * 0.55 + 40)]
    max_extent = bw / 2  # 中心からの最大横幅(スケールとX位置の決定用)
    back_parts = [p for p in part_list if p.get("z") == "back"]
    front_parts = [p for p in part_list if p.get("z") != "back"]
    for i, p in enumerate(back_parts + front_parts):
        is_front = p.get("z") != "back"
        uri = assets.part_data_uri(root, p["file"])
        px, py = p.get("pivot", [0, p["h"] // 2])
        ax, ay = attach_points[i % len(attach_points)]
        x, y = ax - px, ay - py
        max_extent = max(max_extent, abs(x) + p["w"], abs(x))
        piece = (
            f'<g transform="translate({x:g} {y:g})">'
            f'<image href="{uri}" width="{p["w"]}" height="{p["h"]}"/>'
            f"{flap(px, py, i * 0.3)}</g>"
        )
        parts_svg.append(("front" if is_front else "back", piece))

    body_svg = ""
    if body:
        uri = assets.part_data_uri(root, body["file"])
        body_svg = f'<image href="{uri}" x="{-bw / 2:g}" y="{-bh + 40:g}" width="{bw}" height="{bh}"/>'

    inner = (
        "".join(svg for z, svg in parts_svg if z == "back")
        + body_svg
        + "".join(svg for z, svg in parts_svg if z == "front")
    )
    # 全体スケール: 高さ260・片側の横幅220に収める
    scale = min(1.0, 260 / max(1, bh), 220 / max(1.0, max_extent))
    cx = min(540.0, W - 20 - max_extent * scale)  # 右端がはみ出すなら中心を左へ寄せる
    return (
        f'<g transform="translate({cx:g} 300)">'
        "<g>"
        '<animateTransform attributeName="transform" type="translate" additive="sum" '
        'values="0 0; 0 -6; 0 0" dur="3.2s" repeatCount="indefinite"/>'
        f'<g transform="scale({scale:g})">{inner}</g></g></g>'
    )


def _party_silhouettes(save: Save) -> str:
    out = []
    for i, m in enumerate(save.party):
        x = 70 + i * 62
        color = ROLE_COLORS.get(m.role, "#8fa1b8")
        sway = f'<animateTransform attributeName="transform" type="translate" additive="sum" values="0 0; 0 -2; 0 0" dur="{2.6 + i * 0.3:g}s" repeatCount="indefinite"/>'
        out.append(
            f'<g transform="translate({x} 330)"><g>{sway}'
            f'<circle cx="0" cy="-34" r="11" fill="#10152b"/>'
            f'<path d="M -14 6 Q -14 -26 0 -26 Q 14 -26 14 6 Z" fill="#10152b"/>'
            f'<rect x="-9" y="-2" width="18" height="3" rx="1.5" fill="{color}"/>'
            f'<text y="22" font-size="10" fill="#c6d2e6" text-anchor="middle" font-family="{FONT}">{_esc(m.name)}</text>'
            "</g></g>"
        )
    return "".join(out)


def build_scene_svg(save: Save, world: dict[str, Any], root: str = ".") -> str:
    """戦闘シーンSVGを構築する。save.battle が必須(開始直後の状態を想定)。"""
    battle = save.battle
    if battle is None:
        raise ValueError("battle is required for scene")
    enemy: Optional[Enemy] = next((e for e in battle.enemies if e.alive), battle.enemies[0] if battle.enemies else None)
    manifest = assets.load_manifest(root)

    parts: list[str] = []
    parts.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">')
    if manifest and manifest.get("background"):
        # 背景画像がある時は手続き的背景を省略(マークアップと容量の節約)
        parts.append(f'<rect width="{W}" height="{H}" fill="#0a1030"/>')
        bg = manifest["background"]
        uri = assets.part_data_uri(root, bg["file"])
        parts.append(f'<image href="{uri}" x="0" y="0" width="{W}" height="{H}" preserveAspectRatio="xMidYMid slice"/>')
    else:
        parts.append(_procedural_background(world))

    # 敵(登場アニメ: フェード+降下)。静的opacity=0は使わない(SMIL非対応環境で不可視になるため)
    parts.append("<g>")
    parts.append(
        '<animate attributeName="opacity" values="0;0;1" keyTimes="0;0.25;1" dur="1.2s" begin="0s" fill="freeze"/>'
    )
    parts.append(
        '<animateTransform attributeName="transform" type="translate" values="0 -26;0 -26;0 0" '
        'keyTimes="0;0.25;1" dur="1.2s" begin="0s" fill="freeze"/>'
    )
    if enemy is not None:
        if manifest and (manifest.get("body") or manifest.get("parts")):
            parts.append(_enemy_from_parts(root, manifest))
        else:
            parts.append(_enemy_silhouette(enemy))
        label = f"{enemy.name}" + (f" ─ {enemy.title}" if enemy.title else "")
        parts.append(
            f'<text x="540" y="392" font-size="15" font-weight="bold" fill="#f2e9c8" text-anchor="middle" font-family="{FONT}">{_esc(label)}</text>'
        )
    parts.append("</g>")

    parts.append(_party_silhouettes(save))

    # 戦闘名バナー(登場演出)
    parts.append(
        f'<text x="{W / 2:g}" y="52" font-size="22" font-weight="bold" fill="#ffffff" text-anchor="middle" '
        f'font-family="{FONT}">{_esc(battle.name)}'
        '<animate attributeName="opacity" values="0;0;1" keyTimes="0;0.06;1" begin="0s" dur="1.6s" fill="freeze"/></text>'
    )
    intro = battle.recent_log[0] if battle.recent_log else ""
    if intro:
        parts.append(
            f'<text x="{W / 2:g}" y="76" font-size="12" fill="#c6d2e6" text-anchor="middle" '
            f'font-family="{FONT}">{_esc(intro[:48])}'
            '<animate attributeName="opacity" values="0;0;1" keyTimes="0;0.5;1" begin="0s" dur="1.8s" fill="freeze"/></text>'
        )
    world_name = str(world["world_name"])
    parts.append(
        f'<text x="14" y="{H - 12}" font-size="10" fill="#8fa1b8" font-family="{FONT}">🌠 {_esc(world_name)}</text>'
    )
    parts.append("</svg>")
    svg = "".join(parts)
    if len(svg.encode("utf-8")) > SCENE_MAX_BYTES:
        raise ValueError("scene svg exceeds 1MB")
    return svg
