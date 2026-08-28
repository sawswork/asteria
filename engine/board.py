"""戦況ボードSVG生成。

自己完結SVG(外部リソース参照なし)・上限50KB。パーティ4人のHP/CT/奥義ゲージ、
敵HP、挑発ロック、次の強撃予告、直近ログを1枚に描く。毎ターン再生成される軽量ベクター。
"""
from __future__ import annotations

from typing import Any, Optional

from .commands import ROLE_LABELS
from .models import Battle, Enemy, Member, Save

BOARD_MAX_BYTES = 50 * 1024

WIDTH = 760
FONT = "'Hiragino Kaku Gothic ProN','Hiragino Sans','Yu Gothic UI','Meiryo','Noto Sans CJK JP',sans-serif"

BG = "#0d1420"
PANEL = "#161f2e"
PANEL_LINE = "#26344a"
TEXT = "#e8eef7"
SUB = "#8fa1b8"
HP_OK = "#3ecf6e"
HP_MID = "#e6c33b"
HP_LOW = "#e05252"
GAUGE = "#5ea0ff"
GAUGE_FULL = "#ffd75e"
ENEMY_HP = "#d4574f"
ACCENT = "#ffd75e"
CHIP_OK = "#22462f"
CHIP_CT = "#3b2f2f"


def _esc(s: str) -> str:
    return (
        s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def _text(
    x: float,
    y: float,
    s: str,
    size: int = 13,
    fill: str = TEXT,
    weight: str = "normal",
    anchor: str = "start",
    anim: str = "",
) -> str:
    # 注意: 静的な opacity="0" は使わない(SMILが動かない環境で永久に不可視になるため)。
    # 出現演出は「基底=可視、アニメ側が t=0 から 0 を保持して後で 1 へ」方式にする。
    return (
        f'<text x="{x:g}" y="{y:g}" font-size="{size}" fill="{fill}" '
        f'font-weight="{weight}" text-anchor="{anchor}" font-family="{FONT}">{_esc(s)}{anim}</text>'
    )


def _bar(x: float, y: float, w: float, h: float, frac: float, fg: str, bg: str = "#0a0f18") -> str:
    frac = max(0.0, min(1.0, frac))
    return (
        f'<rect x="{x:g}" y="{y:g}" width="{w:g}" height="{h:g}" rx="3" fill="{bg}"/>'
        f'<rect x="{x:g}" y="{y:g}" width="{w * frac:g}" height="{h:g}" rx="3" fill="{fg}"/>'
    )


def _hp_color(frac: float) -> str:
    if frac > 0.5:
        return HP_OK
    if frac > 0.25:
        return HP_MID
    return HP_LOW


def _chip(x: float, y: float, w: float, label: str, ok: bool) -> str:
    bg = CHIP_OK if ok else CHIP_CT
    fg = TEXT if ok else SUB
    return (
        f'<rect x="{x:g}" y="{y:g}" width="{w:g}" height="17" rx="4" fill="{bg}" stroke="{PANEL_LINE}"/>'
        + _text(x + 5, y + 12.5, label, size=10, fill=fg)
    )


def _member_row(m: Member, y: float, ult_max: int) -> list[str]:
    parts: list[str] = []
    hp_frac = m.hp / m.max_hp if m.max_hp else 0.0
    role_label = ROLE_LABELS.get(m.role, m.role)
    name_fill = TEXT if m.alive else SUB
    parts.append(_text(20, y + 16, role_label, size=10, fill=SUB))
    parts.append(_text(20, y + 34, m.name, size=15, fill=name_fill, weight="bold"))
    if not m.alive:
        parts.append(_text(20, y + 50, "戦闘不能", size=10, fill=HP_LOW))
    # HP
    parts.append(_text(150, y + 14, f"HP {m.hp}/{m.max_hp}", size=11, fill=SUB))
    parts.append(_bar(150, y + 20, 170, 12, hp_frac, _hp_color(hp_frac)))
    parts.append(_text(150, y + 50, f"ヘイト {int(m.hate)}", size=10, fill=SUB))
    # 奥義ゲージ
    gauge_frac = m.ult_gauge / ult_max if ult_max else 0.0
    gauge_color = GAUGE_FULL if m.ult_gauge >= ult_max else GAUGE
    parts.append(_text(340, y + 14, f"ゲージ {m.ult_gauge}%", size=11, fill=SUB))
    parts.append(_bar(340, y + 20, 120, 12, gauge_frac, gauge_color))
    buffs = " ".join(
        f"{ {'atk': '攻', 'def': '防', 'agi': '速'}.get(b.stat, b.stat)}×{b.mult:g}({b.turns_left}T)"
        for b in m.buffs
    )
    if buffs:
        parts.append(_text(340, y + 50, buffs, size=10, fill=ACCENT))
    # 技チップ(アビ1〜3+奥義)
    chip_w, cx0, cx1 = 128, 480, 614
    for i, a in enumerate(m.abilities):
        ok = a.ready_in == 0
        state = "✓" if ok else f"CT{a.ready_in}"
        label = f"アビ{i + 1} {a.name} {state}"
        x = cx0 if i % 2 == 0 else cx1
        parts.append(_chip(x, y + 6 + (i // 2) * 21, chip_w, label, ok))
    ult_ok = m.ult_gauge >= ult_max
    parts.append(
        _chip(cx1, y + 27, chip_w, f"奥義 {m.ultimate.name} {'✓' if ult_ok else f'{m.ult_gauge}%'}", ult_ok)
    )
    return parts


def _enemy_block(battle: Battle, save: Save, strong_every: int, y: float) -> list[str]:
    parts: list[str] = []
    for i, e in enumerate(battle.enemies):
        ey = y + i * 64
        hp_frac = e.hp / e.max_hp if e.max_hp else 0.0
        name_fill = TEXT if e.alive else SUB
        parts.append(_text(20, ey + 18, f"敵{i + 1}", size=10, fill=SUB))
        parts.append(_text(20, ey + 38, e.name, size=16, fill=name_fill, weight="bold"))
        if e.title:
            parts.append(_text(20 + 16 * len(e.name) + 14, ey + 38, e.title, size=10, fill=SUB))
        parts.append(_text(340, ey + 18, f"HP {e.hp}/{e.max_hp}", size=11, fill=SUB))
        parts.append(_bar(340, ey + 24, 260, 14, hp_frac, ENEMY_HP))
        if e.alive and battle.active and strong_every > 0:
            until = (strong_every - battle.turn % strong_every) % strong_every
            hint = "⚠ このターン強撃!" if until == 0 else f"次の強撃まで{until}ターン"
            parts.append(_text(620, ey + 34, hint, size=11, fill=ACCENT))
    if battle.taunt_turns_left > 0 and battle.taunt_holder_id:
        holder = save.member_by_id(battle.taunt_holder_id)
        if holder and holder.alive:  # 死亡した保持者のロックはエンジン側で解除される(表示も一致させる)
            parts.append(
                _text(340, y + 54, f"🔒 狙い固定 → {holder.name}(残り{battle.taunt_turns_left}ターン)", size=11, fill=GAUGE)
            )
    return parts


def build_board_svg(save: Save, world: dict[str, Any], balance: dict[str, Any]) -> str:
    ult_max = int(balance["ult_gauge"]["max"])
    strong_every = int(balance["enemy"]["strong_attack_every"])
    battle: Optional[Battle] = save.battle
    world_name = str(world["world_name"])
    gauge_term = str(world["power_system"]["ult_gauge_term"])

    n_enemies = len(battle.enemies) if battle else 1
    header_h = 52
    enemy_h = 14 + n_enemies * 64 + 8
    party_h = 8 + 4 * 66 + 8
    log_lines = (battle.recent_log if battle else ["拠点で休息中。ターンを送信すると新しい戦闘が始まる。"])[-9:]
    log_h = 30 + len(log_lines) * 17 + 8
    y_enemy = header_h + 8
    y_party = y_enemy + enemy_h + 8
    y_log = y_party + party_h + 8
    height = y_log + log_h + 12

    parts: list[str] = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" '
        f'viewBox="0 0 {WIDTH} {height}">'
    )
    parts.append(f'<rect width="{WIDTH}" height="{height}" rx="10" fill="{BG}"/>')

    # ヘッダ
    title = f"🌠 {world_name} · Lv{save.level}"
    parts.append(_text(20, 32, title, size=18, fill=TEXT, weight="bold"))
    if battle:
        status = f"{battle.name} — ターン{battle.turn}"
        if battle.result == "victory":
            status = f"🏆 勝利! {battle.name}"
        elif battle.result == "defeat":
            status = f"💀 敗北…… {battle.name}"
        parts.append(_text(WIDTH - 20, 32, status, size=14, fill=ACCENT, anchor="end"))
    else:
        parts.append(_text(WIDTH - 20, 32, "拠点で休息中", size=14, fill=SUB, anchor="end"))
    parts.append(f'<line x1="16" y1="{header_h}" x2="{WIDTH - 16}" y2="{header_h}" stroke="{PANEL_LINE}"/>')

    # 敵パネル
    parts.append(
        f'<rect x="12" y="{y_enemy}" width="{WIDTH - 24}" height="{enemy_h}" rx="8" fill="{PANEL}" stroke="{PANEL_LINE}"/>'
    )
    if battle:
        parts.extend(_enemy_block(battle, save, strong_every, y_enemy + 4))
    else:
        parts.append(_text(20, y_enemy + 40, "敵影なし — 次の戦いを待っている。", size=13, fill=SUB))

    # パーティパネル
    parts.append(
        f'<rect x="12" y="{y_party}" width="{WIDTH - 24}" height="{party_h}" rx="8" fill="{PANEL}" stroke="{PANEL_LINE}"/>'
    )
    for i, m in enumerate(save.party):
        row_y = y_party + 8 + i * 66
        if i > 0:
            parts.append(
                f'<line x1="16" y1="{row_y - 2}" x2="{WIDTH - 16}" y2="{row_y - 2}" stroke="{PANEL_LINE}" stroke-dasharray="3 3"/>'
            )
        parts.extend(_member_row(m, row_y, ult_max))

    # ログパネル
    parts.append(
        f'<rect x="12" y="{y_log}" width="{WIDTH - 24}" height="{log_h}" rx="8" fill="{PANEL}" stroke="{PANEL_LINE}"/>'
    )
    parts.append(_text(20, y_log + 20, "📜 戦況ログ", size=12, fill=SUB, weight="bold"))
    # ターンリプレイ演出: 直近ターンの行(最後の「——」以降)をSMILで順次表示する
    replay_start = 0
    for i, line in enumerate(log_lines):
        if line.startswith("——"):
            replay_start = i
    replay_count = len(log_lines) - replay_start
    total_dur = max(1.0, replay_count * 0.45 + 0.4)
    for i, line in enumerate(log_lines):
        shown = line if len(line) <= 58 else line[:57] + "…"
        anim = ""
        if battle and battle.active and i >= replay_start:
            delay = (i - replay_start) * 0.45
            a = min(0.999, delay / total_dur)
            b = min(1.0, (delay + 0.35) / total_dur)
            anim = (
                f'<animate attributeName="opacity" values="0;0;1;1" '
                f'keyTimes="0;{a:.3f};{b:.3f};1" dur="{total_dur:.2f}s" begin="0s" fill="freeze"/>'
            )
        parts.append(_text(20, y_log + 40 + i * 17, shown, size=12, fill=TEXT, anim=anim))

    parts.append(
        _text(20, height - 8, f"技生成権 {save.spell_tokens} / 控え {len(save.roster_extra)}人", size=9, fill=SUB)
    )
    parts.append(
        _text(WIDTH - 20, height - 8, f"ゲージ={gauge_term} / チップ✓=使用可 / CTn=あとnターン", size=9, fill=SUB, anchor="end")
    )
    parts.append("</svg>")
    svg = "".join(parts)
    if len(svg.encode("utf-8")) > BOARD_MAX_BYTES:
        raise ValueError(f"board svg exceeds {BOARD_MAX_BYTES} bytes")
    return svg
