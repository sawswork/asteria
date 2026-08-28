"""セーブの読み書き境界(スキーマv2)。

save/ ディレクトリ構成(v2):
  state.json   乱数・戦闘状態・処理済みIssue・統計(ランタイム)
  player.json  レベル・XP・技生成権・編成
  party/<id>.json   メンバー1人1ファイル(スロット=技ID参照+CT状態)
  spells/<id>.json  技1つ1ファイル(定義・使用回数・撃破数)。差し替えられた技もファイルは残る(魔導書=成長史)
  log.md       旅の記憶(AIプロンプトに同梱する1行サマリの列)

v1(state.json単一ファイル)は読み込み時に透過的に移行し、次の書き込みでv2として保存される。
書き込みは全て tmp→rename のアトミック方式。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .models import SCHEMA_VERSION, Ability, Member, Save, Ultimate

DEFAULT_SEED = 20260827  # 初期シード(セーブに記録され、以後のリプレイ再現の基点になる)
SCHEMA_V2 = 2

_SLOT_KEYS = ("ability1", "ability2", "ability3")


def load_json(path: str | Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _write_json(data: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def _write_text(text: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ---- 読み込み -----------------------------------------------------------


def _member_from_v2(mdict: dict[str, Any], spells: dict[str, dict[str, Any]]) -> Member:
    slots = mdict["slots"]
    slot_state = mdict.get("slot_state", {})
    abilities: list[Ability] = []
    for key in _SLOT_KEYS:
        spell = spells[slots[key]]
        abilities.append(
            Ability(
                id=str(spell["id"]),
                name=str(spell["name"]),
                ct=int(spell["ct"]),
                effects=list(spell["effects"]),
                desc=str(spell.get("desc", "")),
                ready_in=int(slot_state.get(key, {}).get("ready_in", 0)),
                usage_count=int(spell.get("usage_count", 0)),
                kills=int(spell.get("kills", 0)),
                constraints=[str(c) for c in spell.get("constraints", [])],
                battle_uses=int(slot_state.get(key, {}).get("battle_uses", 0)),
            )
        )
    ult_spell = spells[slots["ultimate"]]
    ultimate = Ultimate(
        id=str(ult_spell["id"]),
        name=str(ult_spell["name"]),
        effects=list(ult_spell["effects"]),
        desc=str(ult_spell.get("desc", "")),
        usage_count=int(ult_spell.get("usage_count", 0)),
        kills=int(ult_spell.get("kills", 0)),
        constraints=[str(c) for c in ult_spell.get("constraints", [])],
        battle_uses=int(slot_state.get("ultimate", {}).get("battle_uses", 0)),
    )
    base = dict(mdict)
    base.pop("slots", None)
    base.pop("slot_state", None)
    base["abilities"] = [a.to_dict() for a in abilities]
    base["ultimate"] = ultimate.to_dict()
    return Member.from_dict(base)


def _read_journal(path: Path) -> list[str]:
    if not path.exists():
        return []
    lines: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.startswith("- "):
            lines.append(raw[2:].strip())
    return lines


def load_save(save_dir: str | Path) -> Save:
    """save/ ディレクトリからロードする。v1形式(state.json単一)も透過的に読む。"""
    root = Path(save_dir)
    state = load_json(root / "state.json")
    if int(state.get("schema_version", 1)) < SCHEMA_V2 or "party" in state:
        return Save.from_dict(state)  # v1: 単一ファイルに全て入っている

    player = load_json(root / "player.json")
    spells: dict[str, dict[str, Any]] = {}
    spells_dir = root / "spells"
    if spells_dir.is_dir():
        for f in spells_dir.glob("*.json"):
            d = load_json(f)
            spells[str(d["id"])] = d

    def load_members(ids: list[str]) -> list[Member]:
        return [_member_from_v2(load_json(root / "party" / f"{mid}.json"), spells) for mid in ids]

    from .models import Battle  # 局所import(循環回避ではなく可読性のため)

    return Save(
        schema_version=SCHEMA_V2,
        world_id=str(state["world_id"]),
        rng_seed=int(state["rng"]["seed"]),
        rng_counter=int(state["rng"]["counter"]),
        party=load_members(list(player["active_party"])),
        battle=Battle.from_dict(state["battle"]) if state.get("battle") else None,
        processed_issues=[int(n) for n in state.get("processed_issues", [])],
        journal=_read_journal(root / "log.md"),
        stats={str(k): int(v) for k, v in state.get("stats", {}).items()},
        level=int(player.get("level", 1)),
        xp=int(player.get("xp", 0)),
        spell_tokens=int(player.get("spell_tokens", 0)),
        roster_extra=load_members(list(player.get("roster_extra", []))),
        pending_update=state.get("pending_update"),
        nemesis=state.get("nemesis"),
    )


# ---- 書き込み -----------------------------------------------------------


def _spell_dict(kind: str, owner_id: str, obj: Ability | Ultimate) -> dict[str, Any]:
    d: dict[str, Any] = {
        "schema_version": SCHEMA_V2,
        "id": obj.id,
        "kind": kind,
        "owner": owner_id,
        "name": obj.name,
        "effects": obj.effects,
        "desc": obj.desc,
        "usage_count": obj.usage_count,
        "kills": obj.kills,
        "constraints": obj.constraints,
    }
    d["ct"] = obj.ct if isinstance(obj, Ability) else 0
    return d


def _member_v2_dict(m: Member) -> dict[str, Any]:
    d = m.to_dict()
    d.pop("abilities")
    d.pop("ultimate")
    d["slots"] = {
        "ability1": m.abilities[0].id,
        "ability2": m.abilities[1].id,
        "ability3": m.abilities[2].id,
        "ultimate": m.ultimate.id,
    }
    d["slot_state"] = {
        key: {"ready_in": m.abilities[i].ready_in, "battle_uses": m.abilities[i].battle_uses}
        for i, key in enumerate(_SLOT_KEYS)
    }
    d["slot_state"]["ultimate"] = {"battle_uses": m.ultimate.battle_uses}
    return d


def write_save(save: Save, save_dir: str | Path) -> None:
    root = Path(save_dir)
    _write_json(
        {
            "schema_version": SCHEMA_V2,
            "world_id": save.world_id,
            "rng": {"seed": save.rng_seed, "counter": save.rng_counter},
            "battle": save.battle.to_dict() if save.battle else None,
            "processed_issues": save.processed_issues,
            "stats": save.stats,
            "pending_update": save.pending_update,
            "nemesis": save.nemesis,
        },
        root / "state.json",
    )
    _write_json(
        {
            "schema_version": SCHEMA_V2,
            "level": save.level,
            "xp": save.xp,
            "spell_tokens": save.spell_tokens,
            "active_party": [m.id for m in save.party],
            "roster_extra": [m.id for m in save.roster_extra],
        },
        root / "player.json",
    )
    for m in [*save.party, *save.roster_extra]:
        _write_json(_member_v2_dict(m), root / "party" / f"{m.id}.json")
        for i, key in enumerate(_SLOT_KEYS):
            _write_json(_spell_dict("ability", m.id, m.abilities[i]), root / "spells" / f"{m.abilities[i].id}.json")
        _write_json(_spell_dict("ultimate", m.id, m.ultimate), root / "spells" / f"{m.ultimate.id}.json")
    journal_md = (
        "# 旅の記憶\n\n"
        + "\n".join(f"- {line.replace(chr(10), ' ')}" for line in save.journal)  # 1記録=必ず1行
        + "\n"
    )
    _write_text(journal_md, root / "log.md")


def new_save(world: dict[str, Any], balance: dict[str, Any], seed: int = DEFAULT_SEED) -> Save:
    """world定義からの初期セーブ(戦闘未開始)。"""
    party: list[Member] = []
    for m in world["initial_party"]:
        party.append(
            Member(
                id=str(m["id"]),
                role=str(m["role"]),
                name=str(m["name"]),
                title=str(m.get("title", "")),
                max_hp=int(m["max_hp"]),
                hp=int(m["max_hp"]),
                atk=int(m["atk"]),
                df=int(m["def"]),
                agi=int(m["agi"]),
                abilities=[
                    Ability(
                        id=str(a["id"]),
                        name=str(a["name"]),
                        ct=int(a["ct"]),
                        effects=list(a["effects"]),
                        desc=str(a.get("desc", "")),
                    )
                    for a in m["abilities"]
                ],
                ultimate=Ultimate(
                    id=str(m["ultimate"]["id"]),
                    name=str(m["ultimate"]["name"]),
                    effects=list(m["ultimate"]["effects"]),
                    desc=str(m["ultimate"].get("desc", "")),
                ),
                hate=float(balance["hate"]["initial"]),
            )
        )
    return Save(
        schema_version=SCHEMA_V2,
        world_id=str(world["world_id"]),
        rng_seed=seed,
        rng_counter=0,
        party=party,
        battle=None,
        journal=["旅が始まった。"],
    )
