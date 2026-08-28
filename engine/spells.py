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
            "field": {"type": "string", "minLength": 1, "maxLength": 8},
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
    "field": {
        "type": "object",
        "properties": {
            "tag": {"const": "field"},
            "name": {"type": "string", "minLength": 1, "maxLength": 8},
            "turns": {"type": "integer", "minimum": 1, "maximum": 3},
            "target": {"enum": ["enemy"]},
        },
        "required": ["tag", "name", "turns", "target"],
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
        base = float(c["damage_per_power"]) * float(effect["power"]) * int(effect.get("hits", 1))
        if effect.get("field"):  # 添えタグ(不成立時は2ターン付与に相当)の追加コスト
            base += float(balance.get("field", {}).get("cost_per_turn", 6)) * 2
        return base
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
    if tag == "field":
        return float(balance.get("field", {}).get("cost_per_turn", 6)) * int(effect["turns"])
    return float("inf")


def constraint_multiplier(constraints: list[str], balance: dict[str, Any]) -> float:
    """制約(誓約)による予算乗算係数。未知の制約IDは無効(1.0扱いにせずエラーは呼び出し側で)。"""
    table = balance.get("constraints", {})
    cap = float(table.get("total_mult_cap", 3.0))
    mult = 1.0
    for cid in constraints:
        entry = table.get(cid)
        if isinstance(entry, dict):
            mult *= float(entry.get("mult", 1.0))
    return min(cap, mult)


def known_constraints(balance: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {k: v for k, v in balance.get("constraints", {}).items() if isinstance(v, dict)}


def ct_factor(ct: int, balance: dict[str, Any]) -> float:
    """使用頻度によるコスト係数。CTが短い=手数が多いほど高くつく。

    factor = (ct_reference / ct) ^ ct_exponent。基準CT(2)で1.0、CT1は割増、CT3以上は割引。
    戦闘のCT意味論(使用からctターン周期で使用可)と対応した「毎ターン火力」の価格付け。
    """
    sb = balance["spell_budget"]
    ref = float(sb.get("ct_reference", 2))
    exp = float(sb.get("ct_exponent", 0.8))
    return (ref / max(1, int(ct))) ** exp


def spell_cost(ct: int, effects: list[dict[str, Any]], balance: dict[str, Any], is_ult: bool) -> float:
    """技全体のコスト(CT頻度係数込み)。奥義はゲージ制なので係数なし。"""
    total = sum(effect_cost(e, balance) for e in effects)
    if is_ult:
        return total
    return total * ct_factor(ct, balance)


def budget_for(level: int, role: str, balance: dict[str, Any], is_ult: bool) -> float:
    sb = balance["spell_budget"]
    base = (float(sb["base"]) + float(sb["per_level"]) * (max(1, level) - 1)) * float(
        sb["role_coeff"].get(role, 1.0)
    )
    return base * float(sb["ult_mult"]) if is_ult else base


def validate_spell(
    spell: dict[str, Any],
    balance: dict[str, Any],
    level: int,
    role: str,
    is_ult: bool,
    constraints: list[str] | None = None,
) -> list[str]:
    """構造+予算の検証。constraints(制約タグ)があれば予算を乗算(上限×3)して判定する。"""
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
    if not is_ult and int(spell["ct"]) < 1:
        errors.append("アビリティのCTは1以上(毎ターン無制限の技は作れません)")
    cost = spell_cost(int(spell["ct"]), list(spell["effects"]), balance, is_ult)
    budget = budget_for(level, role, balance, is_ult)
    if constraints:
        table = known_constraints(balance)
        for cid in constraints:
            if cid not in table:
                errors.append(f"未知の制約タグ: {cid}")
        budget *= constraint_multiplier(constraints, balance)
    if cost > budget + 1e-9:
        errors.append(f"予算超過: コスト{cost:.1f} > 予算{budget:.1f}")
    return errors
