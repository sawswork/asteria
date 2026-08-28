"""AI応答のJSONスキーマ。検証を通過した応答だけ採用する(不変の制約)。"""
from __future__ import annotations

from typing import Any

from .spells import SPELL_SCHEMA

# 技生成: 技そのもの(spells.SPELL_SCHEMA)を返させる
SPELL_GEN_SCHEMA: dict[str, Any] = SPELL_SCHEMA

# 技アップデート: 進化方向3案
SPELL_UPDATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "options": {
            "type": "array",
            "minItems": 3,
            "maxItems": 3,
            "items": {
                "type": "object",
                "properties": {
                    "direction": {"type": "string", "maxLength": 40},
                    "spell": SPELL_SCHEMA,
                },
                "required": ["direction", "spell"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["options"],
    "additionalProperties": False,
}

# 敵行動の効果はタグ毎に必須フィールドと数値範囲を固定する(プレイヤー側と同じ方式)。
# buff/debuff の mult 範囲は重ならないように分ける(負コストで予算を相殺させない)
_ENEMY_EFFECT_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "object",
        "properties": {
            "tag": {"const": "damage"},
            "power": {"type": "number", "minimum": 0.3, "maximum": 2.5},
            "hits": {"type": "integer", "minimum": 1, "maximum": 3},
            "field": {"type": "string", "minLength": 1, "maxLength": 8},
            "target": {"const": "enemy"},
        },
        "required": ["tag", "power", "target"],
        "additionalProperties": False,
    },
    {
        "type": "object",
        "properties": {
            "tag": {"const": "field"},
            "name": {"type": "string", "minLength": 1, "maxLength": 8},
            "turns": {"type": "integer", "minimum": 1, "maximum": 3},
            "target": {"const": "enemy"},
        },
        "required": ["tag", "name", "turns", "target"],
        "additionalProperties": False,
    },
    {
        "type": "object",
        "properties": {
            "tag": {"const": "dot"},
            "power": {"type": "number", "minimum": 0.2, "maximum": 1.2},
            "turns": {"type": "integer", "minimum": 1, "maximum": 3},
            "target": {"const": "enemy"},
        },
        "required": ["tag", "power", "turns", "target"],
        "additionalProperties": False,
    },
    {
        "type": "object",
        "properties": {
            "tag": {"const": "debuff"},
            "stat": {"enum": ["atk", "def", "agi"]},
            "mult": {"type": "number", "minimum": 0.6, "maximum": 0.95},
            "turns": {"type": "integer", "minimum": 1, "maximum": 3},
            "target": {"const": "enemy"},
        },
        "required": ["tag", "stat", "mult", "turns", "target"],
        "additionalProperties": False,
    },
    {
        "type": "object",
        "properties": {
            "tag": {"const": "stun"},
            "turns": {"type": "integer", "minimum": 1, "maximum": 1},
            "target": {"const": "enemy"},
        },
        "required": ["tag", "turns", "target"],
        "additionalProperties": False,
    },
    {
        "type": "object",
        "properties": {
            "tag": {"const": "buff"},
            "stat": {"enum": ["atk", "def", "agi"]},
            "mult": {"type": "number", "minimum": 1.05, "maximum": 1.5},
            "turns": {"type": "integer", "minimum": 1, "maximum": 3},
            "target": {"const": "self"},
        },
        "required": ["tag", "stat", "mult", "turns", "target"],
        "additionalProperties": False,
    },
]

_ENEMY_ACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1, "maxLength": 14},
        "effects": {
            "type": "array",
            "minItems": 1,
            "maxItems": 2,
            "items": {"oneOf": _ENEMY_EFFECT_SCHEMAS},
        },
    },
    "required": ["name", "effects"],
    "additionalProperties": False,
}

# 敵生成
ENEMY_GEN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1, "maxLength": 14},
        "title": {"type": "string", "maxLength": 20},
        "personality": {"enum": ["狡猾", "凶暴", "臆病", "冷酷", "誇り高い"]},
        "tier": {"enum": ["minion", "standard", "elite", "boss"]},
        "intelligent": {"type": "boolean"},
        "stats": {
            "type": "object",
            "properties": {
                "hp": {"type": "integer", "minimum": 1},
                "atk": {"type": "integer", "minimum": 1},
                "def": {"type": "integer", "minimum": 0},
                "agi": {"type": "integer", "minimum": 1},
            },
            "required": ["hp", "atk", "def", "agi"],
            "additionalProperties": False,
        },
        "actions": {
            "type": "object",
            "properties": {
                "normal": _ENEMY_ACTION_SCHEMA,
                "special": _ENEMY_ACTION_SCHEMA,
            },
            "required": ["normal", "special"],
            "additionalProperties": False,
        },
        "intro": {"type": "string", "maxLength": 80},
    },
    "required": ["name", "personality", "tier", "intelligent", "stats", "actions", "intro"],
    "additionalProperties": False,
}

# 敵の適応進化(演出と進化技のみ。数値ボーナス・歪み弱点はエンジンが決める)
EVOLUTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1, "maxLength": 14},
        "desc": {"type": "string", "maxLength": 60},
        "line": {"type": "string", "maxLength": 60},
        "action": _ENEMY_ACTION_SCHEMA,
    },
    "required": ["name", "action"],
    "additionalProperties": False,
}

# ターン処理(知能層の敵判断+ログ味付け。1ターン1回のAI呼び出しに同梱)
ENEMY_TURN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "enemy_commands": {
            "type": "array",
            "maxItems": 3,
            "items": {
                "type": "object",
                "properties": {
                    "enemy_id": {"type": "string"},
                    "action_key": {"type": "string", "maxLength": 20},
                    "target_role": {"enum": ["attacker", "support", "tank", "healer"]},
                    "line": {"type": "string", "maxLength": 60},
                },
                "required": ["enemy_id", "action_key", "target_role"],
                "additionalProperties": False,
            },
        },
        "flavor": {
            "type": "array",
            "maxItems": 2,
            "items": {"type": "string", "maxLength": 70},
        },
        "fx": {"type": "array", "maxItems": 3, "items": {"type": "string", "maxLength": 40}},
    },
    "required": ["enemy_commands"],
    "additionalProperties": False,
}

# 勧誘(キャラ生成。数値はエンジンが役割テンプレから決める=AIは名前・人格・技のみ)
RECRUIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1, "maxLength": 10},
        "title": {"type": "string", "maxLength": 16},
        "role": {"enum": ["attacker", "support", "tank", "healer"]},
        "personality": {"type": "string", "maxLength": 30},
        "background": {"type": "string", "maxLength": 120},
        "battle_cry": {"type": "string", "maxLength": 40},
        "abilities": {"type": "array", "minItems": 3, "maxItems": 3, "items": SPELL_SCHEMA},
        "ultimate": SPELL_SCHEMA,
    },
    "required": ["name", "role", "personality", "background", "abilities", "ultimate"],
    "additionalProperties": False,
}


# 書籍化: 章の語り(数値や勝敗を変えさせない。あくまで記録を物語として綴らせる)
BOOK_CHAPTER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "minLength": 1, "maxLength": 30},
        "text": {"type": "string", "minLength": 1, "maxLength": 2400},
    },
    "required": ["title", "text"],
    "additionalProperties": False,
}

# 書籍化: 表題・序文・終章
BOOK_FRAME_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "minLength": 1, "maxLength": 30},
        "preface": {"type": "string", "maxLength": 900},
        "epilogue": {"type": "string", "maxLength": 900},
    },
    "required": ["title", "preface", "epilogue"],
    "additionalProperties": False,
}
