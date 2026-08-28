"""技(スペル)の予算計算・スキーマ検証。

AIが生成した技はここを通過したものだけ採用する:
  1. jsonschema による構造・数値範囲の検証
  2. 効果コストの合計 ≤ 予算(budget = f(level) × role係数、奥義は別枠)

数値の最終決定権は常にこのスクリプト側にある(AIは名前・演出・予算内の配分のみ自由)。
"""
from __future__ import annotations

from typing import Any

try:
    import jsonschema
except ImportError:  # 実行環境に無い場合は構造検証のみ縮退(テスト環境では必ず入れる)
    jsonschema = None  # type: ignore[assignment]

# ---- 生成技のスキーマ(効果タグ辞書) -----------------------------------

_EFFECT_SCHEMAS: dict[str, dict[str, Any]] = {
    "damage": {
        "type": "object",
        "properties": {
            "tag": {"const": "damage"},
            "power": {"type": "number", "minimum": 0.3, "maximum": 4.0},
            "hits": {"type": "integer", "minimum": 1, "maximum": 3},
            "target": {"enum": ["enemy"]},
        },
        "required": ["tag", "power", "target"],
        "additionalProperties": False,
    },
    "heal": {
        "type": "object",
        "properties": {
            "tag": {"const": "heal"},
            "power": {"type": "number", "minimum": 0.5, "maximum": 4.0},
            "target": {"enum": ["ally", "party"]},
        },
        "required": ["tag", "power", "target"],
        "additionalProperties": False,
    },
    "buff": {
        "type": "object",
        "properties": {
            "tag": {"const": "buff"},
            "stat": {"enum": ["atk", "def", "agi"]},
            "mult": {"type": "number", "minimum": 1.05, "maximum": 1.6},
            "turns": {"type": "integer", "minimum": 1, "maximum": 3},
            "target": {"enum": ["self", "ally", "party"]},
        },
        "required": ["tag", "stat", "mult", "turns", "target"],
        "additionalProperties": False,
    },
    "debuff": {
        "type": "object",
        "properties": {
            "tag": {"const": "debuff"},
            "stat": {"enum": ["atk", "def", "agi"]},
            "mult": {"type": "number", "minimum": 0.5, "maximum": 0.95},
            "turns": {"type": "integer", "minimum": 1, "maximum": 3},
            "target": {"enum": ["enemy"]},
        },
        "required": ["tag", "stat", "mult", "turns", "target"],
        "additionalProperties": False,
    },
    "stun": {
        "type": "object",
        "properties": {
            "tag": {"const": "stun"},
            "turns": {"type": "integer", "minimum": 1, "maximum": 2},
            "target": {"enum": ["enemy"]},
        },
        "required": ["tag", "turns", "target"],
        "additionalProperties": False,
    },
    "dot": {
        "type": "object",
        "properties": {
            "tag": {"const": "dot"},
            "power": {"type": "number", "minimum": 0.2, "maximum": 1.5},
            "turns": {"type": "integer", "minimum": 1, "maximum": 3},
            "target": {"enum": ["enemy"]},
        },
        "required": ["tag", "power", "turns", "target"],
        "additionalProperties": False,
    },
    "shield": {
        "type": "object",
        "properties": {
            "tag": {"const": "shield"},
            "power": {"type": "number", "minimum": 0.5, "maximum": 4.0},
            "target": {"enum": ["self", "ally", "party"]},
        },
        "required": ["tag", "power", "target"],
        "additionalProperties": False,
    },
    "scan": {
        "type": "object",
        "properties": {"tag": {"const": "scan"}, "target": {"enum": ["enemy"]}},
        "required": ["tag", "target"],
        "additionalProperties": False,
    },
    "dispel": {
        "type": "object",
        "properties": {"tag": {"const": "dispel"}, "target": {"enum": ["enemy"]}},
        "required": ["tag", "target"],
        "additionalProperties": False,
    },
    "hate": {
        "type": "object",
        "properties": {
            "tag": {"const": "hate"},
            "amount": {"type": "number", "minimum": -60, "maximum": 60},
            "target": {"enum": ["self", "ally"]},
        },
        "required": ["tag", "amount", "target"],
        "additionalProperties": False,
    },
    "taunt": {
        "type": "object",
        "properties": {"tag": {"const": "taunt"}, "target": {"enum": ["self"]}},
        "required": ["tag", "target"],
        "additionalProperties": False,
    },
}

SPELL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1, "maxLength": 14},
        "desc": {"type": "string", "maxLength": 70},
        "ct": {"type": "integer", "minimum": 0, "maximum": 5},
        "effects": {
            "type": "array",
            "minItems": 1,
            "maxItems": 3,
            "items": {"oneOf": list(_EFFECT_SCHEMAS.values())},
        },
    },
    "required": ["name", "desc", "ct", "effects"],
    "additionalProperties": False,
}


def effect_cost(effect: dict[str, Any], balance: dict[str, Any]) -> float:
    """効果1つのコスト。未知タグは無限大(=必ず却下)。"""
    c = balance["effect_costs"]
    tag = effect.get("tag")
    if tag == "damage":
        return float(c["damage_per_power"]) * float(effect["power"]) * int(effect.get("hits", 1))
    if tag == "heal":
        base = float(c["heal_per_power"]) * float(effect["power"])
        return base * float(c["heal_party_mult"]) if effect.get("target") == "party" else base
    if tag == "buff":
        weight = float(c["buff_stat_weight"][effect["stat"]])
        base = weight * (float(effect["mult"]) - 1.0) * int(effect["turns"])
        return base * float(c["buff_party_mult"]) if effect.get("target") == "party" else base
    if tag == "debuff":
        weight = float(c["debuff_stat_weight"][effect["stat"]])
        return weight * (1.0 - float(effect["mult"])) * int(effect["turns"])
    if tag == "stun":
        return float(c["stun_per_turn"]) * int(effect["turns"])
    if tag == "dot":
        return float(c["dot_per_power_turn"]) * float(effect["power"]) * int(effect["turns"]) * 3
    if tag == "shield":
        base = float(c["shield_per_power"]) * float(effect["power"])
        return base * float(c["shield_party_mult"]) if effect.get("target") == "party" else base
    if tag == "scan":
        return float(c["scan_flat"])
    if tag == "dispel":
        return float(c["dispel_flat"])
    if tag == "hate":
        return float(c["hate_per_point"]) * abs(float(effect["amount"]))
    if tag == "taunt":
        return float(c["taunt_flat"])
    return float("inf")


def spell_cost(ct: int, effects: list[dict[str, Any]], balance: dict[str, Any], is_ult: bool) -> float:
    """技全体のコスト(CT割引後)。奥義はCT無しなので割引もない。"""
    total = sum(effect_cost(e, balance) for e in effects)
    if is_ult:
        return total
    sb = balance["spell_budget"]
    ct_eff = min(int(ct), int(sb["ct_discount_max_turns"]))
    return total * (1.0 - float(sb["ct_discount_per_turn"]) * ct_eff)


def budget_for(level: int, role: str, balance: dict[str, Any], is_ult: bool) -> float:
    sb = balance["spell_budget"]
    base = (float(sb["base"]) + float(sb["per_level"]) * (max(1, level) - 1)) * float(
        sb["role_coeff"].get(role, 1.0)
    )
    return base * float(sb["ult_mult"]) if is_ult else base


def validate_spell(
    spell: dict[str, Any], balance: dict[str, Any], level: int, role: str, is_ult: bool
) -> list[str]:
    """構造+予算の検証。エラー文のリストを返す(空=採用可)。"""
    errors: list[str] = []
    if jsonschema is not None:
        validator = jsonschema.Draft202012Validator(SPELL_SCHEMA)
        for err in validator.iter_errors(spell):
            path = "/".join(str(p) for p in err.path) or "(root)"
            errors.append(f"schema: {path}: {err.message[:120]}")
        if errors:
            return errors
    else:  # 縮退時の最低限チェック
        for key in ("name", "desc", "ct", "effects"):
            if key not in spell:
                return [f"schema: missing {key}"]
    if is_ult and int(spell["ct"]) != 0:
        errors.append("奥義にCTは設定できません(ゲージ制)")
    cost = spell_cost(int(spell["ct"]), list(spell["effects"]), balance, is_ult)
    budget = budget_for(level, role, balance, is_ult)
    if cost > budget + 1e-9:
        errors.append(f"予算超過: コスト{cost:.1f} > 予算{budget:.1f}")
    return errors
