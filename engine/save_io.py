"""セーブの読み書き境界。書き込みは tmp→rename のアトミック方式。"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .models import SCHEMA_VERSION, Ability, Member, Save, Ultimate

DEFAULT_SEED = 20260827  # 初期シード(セーブに記録され、以後のリプレイ再現の基点になる)


def load_json(path: str | Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_save(path: str | Path) -> Save:
    return Save.from_dict(load_json(path))


def write_save(save: Save, path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(save.to_dict(), f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, p)


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
        schema_version=SCHEMA_VERSION,
        world_id=str(world["world_id"]),
        rng_seed=seed,
        rng_counter=0,
        party=party,
        battle=None,
        journal=["旅が始まった。"],
    )
