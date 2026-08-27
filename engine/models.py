"""型定義とセーブ⇔dict変換。

セーブデータ(save/state.json)の構造をここで一元管理する。schema_version=1(M1)。
効果(effect)は効果タグ辞書のdictをそのまま保持する(M1タグ: damage / heal / buff / taunt / hate)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

SCHEMA_VERSION = 1

# 役割の内部ID(スロット語彙の一部でありエンジン定数。世界の固有名詞ではない)
ROLES = ("attacker", "support", "tank", "healer")


@dataclass
class Buff:
    stat: str  # "atk" | "def" | "agi"
    mult: float
    turns_left: int

    def to_dict(self) -> dict[str, Any]:
        return {"stat": self.stat, "mult": self.mult, "turns_left": self.turns_left}

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Buff":
        return Buff(stat=str(d["stat"]), mult=float(d["mult"]), turns_left=int(d["turns_left"]))


@dataclass
class Ability:
    id: str
    name: str
    ct: int  # クールタイム(使用後、この回数のターン終了を待つ)
    effects: list[dict[str, Any]]
    desc: str = ""
    ready_in: int = 0  # 0なら使用可。使用時に ct をセットし毎ターン終了時に減算

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "ct": self.ct,
            "effects": self.effects,
            "desc": self.desc,
            "ready_in": self.ready_in,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Ability":
        return Ability(
            id=str(d["id"]),
            name=str(d["name"]),
            ct=int(d["ct"]),
            effects=list(d["effects"]),
            desc=str(d.get("desc", "")),
            ready_in=int(d.get("ready_in", 0)),
        )


@dataclass
class Ultimate:
    id: str
    name: str
    effects: list[dict[str, Any]]
    desc: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "effects": self.effects, "desc": self.desc}

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Ultimate":
        return Ultimate(
            id=str(d["id"]),
            name=str(d["name"]),
            effects=list(d["effects"]),
            desc=str(d.get("desc", "")),
        )


@dataclass
class Member:
    """パーティメンバー。"""

    id: str
    role: str  # ROLES のいずれか
    name: str
    title: str
    max_hp: int
    hp: int
    atk: int
    df: int  # "def" は予約語のため df
    agi: int
    abilities: list[Ability]
    ultimate: Ultimate
    ult_gauge: int = 0
    hate: float = 0.0
    buffs: list[Buff] = field(default_factory=list)

    @property
    def alive(self) -> bool:
        return self.hp > 0

    def stat_mult(self, stat: str) -> float:
        m = 1.0
        for b in self.buffs:
            if b.stat == stat and b.turns_left > 0:
                m *= b.mult
        return m

    def eff_atk(self) -> float:
        return self.atk * self.stat_mult("atk")

    def eff_def(self) -> float:
        return self.df * self.stat_mult("def")

    def eff_agi(self) -> float:
        return self.agi * self.stat_mult("agi")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "role": self.role,
            "name": self.name,
            "title": self.title,
            "max_hp": self.max_hp,
            "hp": self.hp,
            "atk": self.atk,
            "def": self.df,
            "agi": self.agi,
            "abilities": [a.to_dict() for a in self.abilities],
            "ultimate": self.ultimate.to_dict(),
            "ult_gauge": self.ult_gauge,
            "hate": self.hate,
            "buffs": [b.to_dict() for b in self.buffs],
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Member":
        return Member(
            id=str(d["id"]),
            role=str(d["role"]),
            name=str(d["name"]),
            title=str(d.get("title", "")),
            max_hp=int(d["max_hp"]),
            hp=int(d["hp"]),
            atk=int(d["atk"]),
            df=int(d["def"]),
            agi=int(d["agi"]),
            abilities=[Ability.from_dict(a) for a in d["abilities"]],
            ultimate=Ultimate.from_dict(d["ultimate"]),
            ult_gauge=int(d.get("ult_gauge", 0)),
            hate=float(d.get("hate", 0.0)),
            buffs=[Buff.from_dict(b) for b in d.get("buffs", [])],
        )


@dataclass
class Enemy:
    id: str
    name: str
    title: str
    max_hp: int
    hp: int
    atk: int
    df: int
    agi: int
    actions: dict[str, Any]  # {"normal": {...}, "strong": {...}}
    buffs: list[Buff] = field(default_factory=list)

    @property
    def alive(self) -> bool:
        return self.hp > 0

    def stat_mult(self, stat: str) -> float:
        m = 1.0
        for b in self.buffs:
            if b.stat == stat and b.turns_left > 0:
                m *= b.mult
        return m

    def eff_atk(self) -> float:
        return self.atk * self.stat_mult("atk")

    def eff_def(self) -> float:
        return self.df * self.stat_mult("def")

    def eff_agi(self) -> float:
        return self.agi * self.stat_mult("agi")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "title": self.title,
            "max_hp": self.max_hp,
            "hp": self.hp,
            "atk": self.atk,
            "def": self.df,
            "agi": self.agi,
            "actions": self.actions,
            "buffs": [b.to_dict() for b in self.buffs],
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Enemy":
        return Enemy(
            id=str(d["id"]),
            name=str(d["name"]),
            title=str(d.get("title", "")),
            max_hp=int(d["max_hp"]),
            hp=int(d["hp"]),
            atk=int(d["atk"]),
            df=int(d["def"]),
            agi=int(d["agi"]),
            actions=dict(d["actions"]),
            buffs=[Buff.from_dict(b) for b in d.get("buffs", [])],
        )


@dataclass
class Battle:
    active: bool
    name: str
    turn: int  # 次に解決するターン番号(1始まり)
    enemies: list[Enemy]
    result: Optional[str] = None  # None | "victory" | "defeat"
    taunt_holder_id: Optional[str] = None
    taunt_turns_left: int = 0
    recent_log: list[str] = field(default_factory=list)  # ボード表示用の直近ログ

    def to_dict(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "name": self.name,
            "turn": self.turn,
            "enemies": [e.to_dict() for e in self.enemies],
            "result": self.result,
            "taunt_holder_id": self.taunt_holder_id,
            "taunt_turns_left": self.taunt_turns_left,
            "recent_log": self.recent_log,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Battle":
        return Battle(
            active=bool(d["active"]),
            name=str(d.get("name", "")),
            turn=int(d["turn"]),
            enemies=[Enemy.from_dict(e) for e in d["enemies"]],
            result=d.get("result"),
            taunt_holder_id=d.get("taunt_holder_id"),
            taunt_turns_left=int(d.get("taunt_turns_left", 0)),
            recent_log=list(d.get("recent_log", [])),
        )


@dataclass
class Save:
    schema_version: int
    world_id: str
    rng_seed: int
    rng_counter: int
    party: list[Member]
    battle: Optional[Battle]
    processed_issues: list[int] = field(default_factory=list)
    journal: list[str] = field(default_factory=list)  # 旅の記録(1行サマリ)
    stats: dict[str, int] = field(default_factory=dict)  # 勝利数など

    def member_by_role(self, role: str) -> Optional[Member]:
        for m in self.party:
            if m.role == role:
                return m
        return None

    def member_by_id(self, member_id: str) -> Optional[Member]:
        for m in self.party:
            if m.id == member_id:
                return m
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "world_id": self.world_id,
            "rng": {"seed": self.rng_seed, "counter": self.rng_counter},
            "party": [m.to_dict() for m in self.party],
            "battle": self.battle.to_dict() if self.battle else None,
            "processed_issues": self.processed_issues,
            "journal": self.journal,
            "stats": self.stats,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Save":
        return Save(
            schema_version=int(d["schema_version"]),
            world_id=str(d["world_id"]),
            rng_seed=int(d["rng"]["seed"]),
            rng_counter=int(d["rng"]["counter"]),
            party=[Member.from_dict(m) for m in d["party"]],
            battle=Battle.from_dict(d["battle"]) if d.get("battle") else None,
            processed_issues=[int(n) for n in d.get("processed_issues", [])],
            journal=list(d.get("journal", [])),
            stats={str(k): int(v) for k, v in d.get("stats", {}).items()},
        )
