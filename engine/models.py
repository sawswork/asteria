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
    usage_count: int = 0  # 使い込みボーナス(技アップデートの予算)に使う
    kills: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "ct": self.ct,
            "effects": self.effects,
            "desc": self.desc,
            "ready_in": self.ready_in,
            "usage_count": self.usage_count,
            "kills": self.kills,
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
            usage_count=int(d.get("usage_count", 0)),
            kills=int(d.get("kills", 0)),
        )


@dataclass
class Ultimate:
    id: str
    name: str
    effects: list[dict[str, Any]]
    desc: str = ""
    usage_count: int = 0
    kills: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "effects": self.effects,
            "desc": self.desc,
            "usage_count": self.usage_count,
            "kills": self.kills,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Ultimate":
        return Ultimate(
            id=str(d["id"]),
            name=str(d["name"]),
            effects=list(d["effects"]),
            desc=str(d.get("desc", "")),
            usage_count=int(d.get("usage_count", 0)),
            kills=int(d.get("kills", 0)),
        )


@dataclass
class Dot:
    """継続ダメージ(詠唱時にダメージ量をスナップショット)。"""

    damage: int
    turns_left: int
    source: str  # 表示用の技名

    def to_dict(self) -> dict[str, Any]:
        return {"damage": self.damage, "turns_left": self.turns_left, "source": self.source}

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Dot":
        return Dot(damage=int(d["damage"]), turns_left=int(d["turns_left"]), source=str(d.get("source", "")))


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
    shield: int = 0
    stunned_turns: int = 0
    dots: list[Dot] = field(default_factory=list)

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
            "shield": self.shield,
            "stunned_turns": self.stunned_turns,
            "dots": [x.to_dict() for x in self.dots],
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
            shield=int(d.get("shield", 0)),
            stunned_turns=int(d.get("stunned_turns", 0)),
            dots=[Dot.from_dict(x) for x in d.get("dots", [])],
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
    shield: int = 0
    stunned_turns: int = 0
    dots: list[Dot] = field(default_factory=list)
    cc_resist: dict[str, int] = field(default_factory=dict)  # CC耐性段階(同一CCは効く度に上昇)
    personality: str = ""  # 知能層AIに渡す性格(狡猾/凶暴/臆病など)
    tier: str = "standard"  # minion | standard | elite | boss
    intelligent: bool = False  # True=知能層(AI判断)、False=ルール層
    xp: int = 0

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
            "shield": self.shield,
            "stunned_turns": self.stunned_turns,
            "dots": [x.to_dict() for x in self.dots],
            "cc_resist": self.cc_resist,
            "personality": self.personality,
            "tier": self.tier,
            "intelligent": self.intelligent,
            "xp": self.xp,
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
            shield=int(d.get("shield", 0)),
            stunned_turns=int(d.get("stunned_turns", 0)),
            dots=[Dot.from_dict(x) for x in d.get("dots", [])],
            cc_resist={str(k): int(v) for k, v in d.get("cc_resist", {}).items()},
            personality=str(d.get("personality", "")),
            tier=str(d.get("tier", "standard")),
            intelligent=bool(d.get("intelligent", False)),
            xp=int(d.get("xp", 0)),
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
    scanned: list[str] = field(default_factory=list)  # スキャン済み敵ID(ボードに詳細表示)

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
            "scanned": self.scanned,
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
            scanned=[str(x) for x in d.get("scanned", [])],
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
    journal: list[str] = field(default_factory=list)  # 旅の記録(1行サマリ。log.mdへ永続化)
    stats: dict[str, int] = field(default_factory=dict)  # 勝利数など
    level: int = 1  # パーティ共有レベル
    xp: int = 0
    spell_tokens: int = 0  # 技生成権(レベルアップで+1)
    roster_extra: list[Member] = field(default_factory=list)  # 勧誘で加入した控えメンバー

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
            "level": self.level,
            "xp": self.xp,
            "spell_tokens": self.spell_tokens,
            "roster_extra": [m.to_dict() for m in self.roster_extra],
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
            level=int(d.get("level", 1)),
            xp=int(d.get("xp", 0)),
            spell_tokens=int(d.get("spell_tokens", 0)),
            roster_extra=[Member.from_dict(m) for m in d.get("roster_extra", [])],
        )
