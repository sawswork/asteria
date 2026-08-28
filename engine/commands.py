"""スロット語彙とコマンド検証。

フォームYAMLは固定で、行動・対象は以下の不変語彙のみ。各スロットの中身(技名・CT・ゲージ)は
READMEの戦況ボードが表示する。不正手はここで検知し、エラー返信+ターン不消費とする。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Union

from .models import Ability, Battle, Member, Save, Ultimate

# ---- 不変のスロット語彙 -------------------------------------------------

ACTION_NORMAL = "通常攻撃"
ACTION_ABILITY_1 = "アビ1"
ACTION_ABILITY_2 = "アビ2"
ACTION_ABILITY_3 = "アビ3"
ACTION_ULT = "奥義"
ACTION_WAIT = "待機"
ACTIONS = (
    ACTION_ABILITY_1,
    ACTION_ABILITY_2,
    ACTION_ABILITY_3,
    ACTION_ULT,
    ACTION_NORMAL,
    ACTION_WAIT,
)

TARGET_AUTO = "自動"
TARGET_ENEMIES = ("敵1", "敵2", "敵3")
ROLE_LABELS = {
    "attacker": "アタッカー",
    "support": "サポート",
    "tank": "タンク",
    "healer": "ヒーラー",
}
LABEL_TO_ROLE = {v: k for k, v in ROLE_LABELS.items()}
TARGETS = (TARGET_AUTO,) + TARGET_ENEMIES + tuple(ROLE_LABELS.values())

ABILITY_INDEX = {ACTION_ABILITY_1: 0, ACTION_ABILITY_2: 1, ACTION_ABILITY_3: 2}


@dataclass
class Command:
    role: str  # "attacker" 等
    action: str  # ACTIONS のいずれか
    target: str  # TARGETS のいずれか


@dataclass
class InvalidMove:
    role: str
    reason: str


def normal_attack_effects(balance: dict[str, Any]) -> list[dict[str, Any]]:
    """通常攻撃の効果(係数は balance.json に一元化)。検証と実行で共有する。"""
    power = float(balance["damage"].get("normal_attack_power", 1.0))
    return [{"tag": "damage", "power": power, "target": "enemy"}]


OFFENSE_TAGS = ("damage", "dot", "debuff", "stun", "scan", "dispel", "field")


def _primary_effect_kind(effects: list[dict]) -> str:
    """効果リストの主効果種別: "offense"(敵対象) / "friendly"(味方対象) / "neutral"。"""
    for e in effects:
        if e.get("tag") in OFFENSE_TAGS:
            return "offense"
    for e in effects:
        if e.get("tag") in ("heal", "shield") and e.get("target") == "ally":
            return "friendly_single"
        if e.get("tag") in ("heal", "buff", "taunt", "hate", "shield"):
            return "friendly"
    return "neutral"


def _effects_for_action(member: Member, action: str, balance: dict[str, Any]) -> list[dict]:
    if action in ABILITY_INDEX:
        return member.abilities[ABILITY_INDEX[action]].effects
    if action == ACTION_ULT:
        return member.ultimate.effects
    if action == ACTION_NORMAL:
        return normal_attack_effects(balance)
    return []


def constraint_violations(
    member: Member,
    spell: Union[Ability, Ultimate],
    battle: Battle,
    balance: dict[str, Any],
    cmd_target: str,
) -> list[str]:
    """誓約(制約タグ)の発動条件チェック。条件を満たさない理由のリストを返す。

    しきい値・対象ランク等の数値は balance.constraints の各エントリに置く(係数一元化)。
    """
    table = balance.get("constraints", {})
    reasons: list[str] = []

    def _entry(cid: str) -> dict[str, Any]:
        e = table.get(cid)
        return e if isinstance(e, dict) else {}

    def _label(cid: str) -> str:
        return str(_entry(cid).get("label", cid))

    for cid in spell.constraints:
        if cid == "hp_below_30":
            ratio = float(_entry(cid).get("ratio", 0.3))
            if member.hp > member.max_hp * ratio:
                reasons.append(f"誓約「{_label(cid)}」: 今のHPでは発動できません")
        elif cid == "once_per_battle":
            if spell.battle_uses >= 1:
                reasons.append(f"誓約「{_label(cid)}」: この戦闘では既に使いました")
        elif cid == "first_three_turns":
            turns = int(_entry(cid).get("turns", 3))
            if battle.turn > turns:
                reasons.append(f"誓約「{_label(cid)}」: ターン{turns}を過ぎています")
        elif cid == "vs_elite_plus":
            tiers = [str(t) for t in _entry(cid).get("tiers", ["elite", "boss"])]
            target: Optional[Any] = None
            if cmd_target in TARGET_ENEMIES:
                idx = TARGET_ENEMIES.index(cmd_target)
                if idx < len(battle.enemies) and battle.enemies[idx].alive:
                    target = battle.enemies[idx]
            if target is None:  # 自動/無効指定は実行時と同じく先頭の生存敵
                target = next((e for e in battle.enemies if e.alive), None)
            if target is None or target.tier not in tiers:
                reasons.append(f"誓約「{_label(cid)}」: この敵には向けられません")
        # self_stun_after は発動条件ではなく代償(実行時に battle.py が科す)
    return reasons


def validate_commands(
    save: Save, battle: Battle, commands: dict[str, Command], balance: dict[str, Any]
) -> list[InvalidMove]:
    """戦闘開始時点の状態に対してコマンド一式を検証する。1つでも不正手があればターン不消費。"""
    ult_max = int(balance["ult_gauge"]["max"])
    errors: list[InvalidMove] = []
    alive_enemy_count = sum(1 for e in battle.enemies if e.alive)

    for role in ("attacker", "support", "tank", "healer"):
        member = save.member_by_role(role)
        if member is None:
            errors.append(InvalidMove(role, "パーティに該当役割がいません"))
            continue
        cmd = commands.get(role)
        if cmd is None:
            errors.append(InvalidMove(role, "行動が指定されていません"))
            continue
        if cmd.action not in ACTIONS:
            errors.append(InvalidMove(role, f"不明な行動「{cmd.action}」"))
            continue
        if cmd.target not in TARGETS:
            errors.append(InvalidMove(role, f"不明な対象「{cmd.target}」"))
            continue
        if not member.alive:
            # 戦闘不能メンバーは待機扱い(不正手にはしない)
            continue

        if cmd.action in ABILITY_INDEX:
            ability = member.abilities[ABILITY_INDEX[cmd.action]]
            if ability.ready_in > 0:
                errors.append(
                    InvalidMove(
                        role,
                        f"{cmd.action}「{ability.name}」はCT中(あと{ability.ready_in}ターン)",
                    )
                )
                continue
        if cmd.action == ACTION_ULT and member.ult_gauge < ult_max:
            errors.append(
                InvalidMove(
                    role,
                    f"奥義「{member.ultimate.name}」はゲージ不足({member.ult_gauge}/{ult_max})",
                )
            )
            continue

        spell_obj: Union[Ability, Ultimate, None] = None
        if cmd.action in ABILITY_INDEX:
            spell_obj = member.abilities[ABILITY_INDEX[cmd.action]]
        elif cmd.action == ACTION_ULT:
            spell_obj = member.ultimate
        if spell_obj is not None and spell_obj.constraints:
            violations = constraint_violations(member, spell_obj, battle, balance, cmd.target)
            if violations:
                errors.extend(InvalidMove(role, v) for v in violations)
                continue

        # 対象の整合性チェック
        kind = _primary_effect_kind(_effects_for_action(member, cmd.action, balance))
        if cmd.action == ACTION_WAIT:
            continue  # 待機は対象を無視
        if cmd.target in TARGET_ENEMIES:
            idx = TARGET_ENEMIES.index(cmd.target)
            if idx >= len(battle.enemies) or not battle.enemies[idx].alive:
                errors.append(InvalidMove(role, f"対象「{cmd.target}」は存在しません"))
                continue
            if kind not in ("offense",):
                errors.append(InvalidMove(role, "その行動は敵を対象にできません"))
                continue
        elif cmd.target in LABEL_TO_ROLE:
            if kind == "offense":
                errors.append(InvalidMove(role, "その行動は敵対象です。味方を対象にできません"))
                continue
            target_member = save.member_by_role(LABEL_TO_ROLE[cmd.target])
            if target_member is None or not target_member.alive:
                errors.append(InvalidMove(role, f"対象「{cmd.target}」は行動不能です"))
                continue
        # TARGET_AUTO は常に許可(実行時に解決)

    if alive_enemy_count == 0:
        # ここに来るのは戦闘が既に終わっているのに解決しようとした場合のみ(通常は新戦闘を開始する)
        errors.append(InvalidMove("-", "戦闘中の敵がいません"))
    return errors
